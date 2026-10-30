"""Independent verifier for a staged Alpaca capture run.

It trusts nothing the runner reported: it re-hashes every staged file against
the hash-bound manifest, re-checks every recorded request against the allow-list
catalog, replays the pagination chain from the raw bytes, and re-derives the
normalized outputs from raw bytes to compare them byte for byte.  Findings are
stable codes only; no value from the evidence is ever emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from quant_engine.alpaca.capture.catalog import (
    PAGE_CURSOR,
    authorize_request,
    catalog_sha256,
)
from quant_engine.alpaca.capture.errors import CaptureError, NormalizationError
from quant_engine.alpaca.capture.normalize import parse_json_bytes
from quant_engine.alpaca.capture.outputs import derive_outputs
from quant_engine.alpaca.capture.run import (
    MANIFEST_DIGEST_FILE,
    MANIFEST_FILE,
    REQUEST_MANIFEST_FILE,
)
from quant_engine.alpaca.capture.util import sha256_bytes

_SENSITIVE = re.compile(
    r"(?i)(authorization|cookie|credential|password|private.?key|secret|signature|api.?key|key.?id|token)"
)
_BINDING = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE = frozenset({MANIFEST_FILE, MANIFEST_DIGEST_FILE, REQUEST_MANIFEST_FILE})


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    findings: tuple[str, ...]
    request_count: int
    activity_count: int


def _staged_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def _load_requests(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise CaptureError("a request record is not an object")
        rows.append(value)
    return rows


def _scan_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _SENSITIVE.search(str(key)) or _scan_sensitive(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_scan_sensitive(item) for item in value)
    return False


def verify_run(run_directory: str | Path) -> VerificationReport:
    root = Path(run_directory)
    findings: list[str] = []
    manifest_path = root / MANIFEST_FILE
    digest_path = root / MANIFEST_DIGEST_FILE
    if not manifest_path.is_file() or not digest_path.is_file():
        return VerificationReport(False, ("manifest_missing",), 0, 0)
    manifest_bytes = manifest_path.read_bytes()
    if digest_path.read_text(encoding="ascii").strip() != sha256_bytes(manifest_bytes):
        return VerificationReport(False, ("manifest_digest_mismatch",), 0, 0)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        raw_files: dict[str, str] = dict(manifest["raw_files"])
        normalized_files: dict[str, str] = dict(manifest["normalized_files"])
        terminal = manifest["terminal"]
        window = manifest["window"]
        binding = manifest["account_binding_sha256"]
        request_count = int(manifest["request_count"])
        activity_count = int(manifest["activity_row_count"])
        page_size = int(window["page_size"])
        include_positions = bool(window["include_positions"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return VerificationReport(False, ("manifest_malformed",), 0, 0)
    if manifest.get("provider") != "alpaca" or manifest.get("schema_version") != 1:
        findings.append("manifest_identity")
    if manifest.get("classification") != "restricted-private":
        findings.append("classification_missing")
    if not isinstance(binding, str) or _BINDING.fullmatch(binding) is None:
        findings.append("binding_malformed")
    if manifest.get("catalog_sha256") != catalog_sha256():
        findings.append("catalog_changed")
    if not isinstance(terminal, dict) or terminal.get("complete") is not True:
        findings.append("run_incomplete")

    expected = set(raw_files) | set(normalized_files) | _ENVELOPE
    if _staged_files(root) != expected:
        findings.append("staged_file_set_mismatch")
    for relative, digest in {**raw_files, **normalized_files}.items():
        target = root / relative
        if not target.is_file() or sha256_bytes(target.read_bytes()) != digest:
            findings.append("staged_file_hash_mismatch")
            break

    request_path = root / REQUEST_MANIFEST_FILE
    requests: list[dict[str, Any]] = []
    if not request_path.is_file():
        findings.append("request_manifest_missing")
    elif sha256_bytes(request_path.read_bytes()) != manifest.get(
        "request_manifest_sha256"
    ):
        findings.append("request_manifest_hash_mismatch")
    else:
        try:
            requests = _load_requests(request_path)
        except (CaptureError, ValueError, UnicodeDecodeError):
            findings.append("request_manifest_malformed")
    if len(requests) != request_count:
        findings.append("request_count_mismatch")

    bodies: dict[str, list[bytes]] = {"account": [], "positions": [], "activities": []}
    previous_last_id: str | None = None
    previous_rows: int | None = None
    activity_rows = 0
    for index, record in enumerate(requests, start=1):
        try:
            endpoint = authorize_request(
                str(record.get("method")),
                str(record.get("path")),
                record.get("safe_params") or {},
            )
        except CaptureError:
            findings.append("request_outside_allow_list")
            continue
        if record.get("method") != "GET" or record.get("sequence") != index:
            findings.append("request_sequence_or_method")
        if _scan_sensitive({k: v for k, v in record.items() if k != "safe_params"}):
            findings.append("sensitive_key_in_request_record")
        raw_name = record.get("raw_relpath")
        if raw_files.get(str(raw_name)) != record.get("response_sha256"):
            findings.append("request_raw_binding_mismatch")
            continue
        if record.get("status") != 200:
            findings.append("request_not_ok")
            continue
        try:
            body = (root / str(raw_name)).read_bytes()
        except OSError:
            findings.append("staged_file_unreadable")
            continue
        bodies.setdefault(endpoint.report, []).append(body)
        if endpoint.report != "activities":
            continue
        params = record.get("safe_params") or {}
        try:
            page = parse_json_bytes(body)
        except NormalizationError:
            findings.append("activities_page_unparseable")
            continue
        if not isinstance(page, list) or record.get("row_count") != len(page):
            findings.append("activities_row_count_mismatch")
            continue
        activity_rows += len(page)
        cursor = params.get(PAGE_CURSOR)
        if previous_rows is None:
            if cursor is not None:
                findings.append("first_page_has_cursor")
        else:
            if previous_rows != page_size or cursor != previous_last_id:
                findings.append("pagination_chain_broken")
        previous_rows = len(page)
        last = page[-1] if page else None
        previous_last_id = last.get("id") if isinstance(last, dict) else None
    if previous_rows is not None and previous_rows >= page_size:
        findings.append("pagination_not_terminated")
    if not bodies["account"]:
        findings.append("account_request_missing")
    if include_positions != bool(bodies["positions"]):
        findings.append("positions_request_mismatch")
    if not bodies["activities"]:
        findings.append("activities_request_missing")
    if activity_rows != activity_count:
        findings.append("activity_count_mismatch")

    if bodies["account"] and bodies["activities"] and _BINDING.fullmatch(str(binding)):
        try:
            derived = derive_outputs(
                binding_sha256=str(binding),
                account_body=bodies["account"][0],
                positions_body=bodies["positions"][0]
                if include_positions and bodies["positions"]
                else None,
                activity_bodies=bodies["activities"],
            )
        except NormalizationError:
            findings.append("rederivation_refused")
        else:
            on_disk = {
                relative: (root / relative).read_bytes()
                for relative in normalized_files
                if (root / relative).is_file()
            }
            if on_disk != derived:
                findings.append("normalized_output_mismatch")
    unique = tuple(dict.fromkeys(findings))
    return VerificationReport(not unique, unique, len(requests), activity_rows)

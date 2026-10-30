"""Governed, create-only intake of an owner-supplied Quantbase browser HAR export.

The capture is the owner's own browser export. This tool never logs in, replays a
session, or opens a network connection: it stages the supplied HAR files byte for
byte, hash-binds them, parses them through the reviewed Quantbase web contract and
verifies the result independently. Expected file names, per-capability record
counts, the date window, the cutoff and the NAV publication grid all come from one
intake manifest, never from constants in this file.

Output is value-free: reports carry counts, hashes and dates only.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Final
from urllib.parse import urlsplit

from quant_engine.web_contracts import (
    QUANTBASE_CONTRACT,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_quantbase_har,
)


SCHEMA_VERSION: Final = 1
PROVIDER_ID: Final = "quantbase"
PROVENANCE_KIND: Final = "OWNER_BROWSER_HAR_EXPORT"
HAR_SCHEMA: Final = ("quantbase-native-har", "1")
INTAKE_SCHEMA: Final = ("quantbase-har-intake-manifest", "1")
_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
_APPROVAL_REFERENCE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[0-9]+#issuecomment-[0-9]+$"
)
_HAR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.har$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s]*$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PATH_KEY = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
_BASE_ORIGINS: Final = frozenset({"https://qbapi3.com", "https://app.getquantbase.com"})
_SESSION_HEADERS: Final = frozenset({"jwt", "authorization", "cookie", "set-cookie"})
_MANIFEST_KEYS: Final = frozenset(
    {
        "schema_version",
        "provider_id",
        "provenance",
        "capture_files",
        "session",
        "window",
        "window_fields",
        "cutoff",
        "nav_grid",
        "capabilities",
        "ignored_origins",
    }
)
_RAW = "raw"
_MANIFEST_FILE = "manifest.json"
_INTAKE_FILE = "intake-manifest.json"
_RECORDS_FILE = "normalized/records.json"
_REPORT_FILE = "parse-report.json"
_NATIVE_FILE = "native-artifacts.json"


class QuantbaseHarIntakeError(ValueError):
    """The owner-supplied HAR intake failed closed."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QuantbaseHarIntakeError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise QuantbaseHarIntakeError(f"{name} must be an array")
    return value


def _count(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QuantbaseHarIntakeError(f"{name} must be an integer >= {minimum}")
    return value


def _iso_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise QuantbaseHarIntakeError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise QuantbaseHarIntakeError(f"{name} must be an ISO date") from exc


def _iso_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise QuantbaseHarIntakeError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuantbaseHarIntakeError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise QuantbaseHarIntakeError(f"{name} must be timezone-aware")
    return parsed


def _absolute_directory(value: Path, name: str) -> Path:
    if not value.is_absolute() or value.is_symlink() or not value.is_dir():
        raise QuantbaseHarIntakeError(f"{name} must be a real absolute directory")
    return value.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)


class Manifest:
    """The validated intake manifest; every governed expectation lives here."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if set(document) != _MANIFEST_KEYS:
            raise QuantbaseHarIntakeError("manifest keys differ from the contract")
        if document["schema_version"] != SCHEMA_VERSION:
            raise QuantbaseHarIntakeError("manifest schema version is unsupported")
        if document["provider_id"] != PROVIDER_ID:
            raise QuantbaseHarIntakeError("manifest provider is not quantbase")
        provenance = _object(document["provenance"], "provenance")
        if set(provenance) != {"kind", "approval_reference"}:
            raise QuantbaseHarIntakeError("provenance keys differ from the contract")
        if provenance["kind"] != PROVENANCE_KIND:
            raise QuantbaseHarIntakeError("capture must be an owner browser HAR export")
        reference = provenance["approval_reference"]
        if not isinstance(reference, str) or not _APPROVAL_REFERENCE.fullmatch(
            reference
        ):
            raise QuantbaseHarIntakeError("owner approval reference is invalid")
        files = _array(document["capture_files"], "capture_files")
        if not files:
            raise QuantbaseHarIntakeError("capture_files must not be empty")
        self.file_hashes: dict[str, str | None] = {}
        for item in files:
            entry = _object(item, "capture_files[]")
            if not set(entry) <= {"name", "sha256"} or "name" not in entry:
                raise QuantbaseHarIntakeError("capture file keys differ")
            name = entry["name"]
            if not isinstance(name, str) or not _HAR_NAME.fullmatch(name):
                raise QuantbaseHarIntakeError("capture file name is invalid")
            digest = entry.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            ):
                raise QuantbaseHarIntakeError("capture file sha256 is invalid")
            self.file_hashes[name] = digest
        if len(self.file_hashes) != len(files) or len(
            {name.casefold() for name in self.file_hashes}
        ) != len(files):
            raise QuantbaseHarIntakeError("capture file names must be unique")
        session = _object(document["session"], "session")
        if set(session) != {"captured_at", "expires_at"}:
            raise QuantbaseHarIntakeError("session keys differ from the contract")
        self.session = SessionEnvelope(
            captured_at=_iso_time(session["captured_at"], "session.captured_at"),
            expires_at=_iso_time(session["expires_at"], "session.expires_at"),
        )
        window = _object(document["window"], "window")
        if set(window) != {"start", "end"}:
            raise QuantbaseHarIntakeError("window keys differ from the contract")
        self.window_start = _iso_date(window["start"], "window.start")
        self.window_end = _iso_date(window["end"], "window.end")
        self.cutoff = _iso_date(document["cutoff"], "cutoff")
        if not self.window_start <= self.cutoff <= self.window_end:
            raise QuantbaseHarIntakeError("cutoff must lie inside the date window")
        fields = _array(document["window_fields"], "window_fields")
        if not fields or any(
            not isinstance(item, str) or not _FIELD_NAME.fullmatch(item)
            for item in fields
        ):
            raise QuantbaseHarIntakeError("window_fields are invalid")
        self.window_fields = frozenset(fields)
        grid = _object(document["nav_grid"], "nav_grid")
        if set(grid) != {"anchor", "step_days", "capabilities"}:
            raise QuantbaseHarIntakeError("nav_grid keys differ from the contract")
        self.grid_anchor = _iso_date(grid["anchor"], "nav_grid.anchor")
        self.grid_step = _count(grid["step_days"], "nav_grid.step_days", minimum=1)
        nav = _array(grid["capabilities"], "nav_grid.capabilities")
        if not nav or any(not isinstance(item, str) for item in nav):
            raise QuantbaseHarIntakeError("nav_grid.capabilities are invalid")
        self.nav_capabilities = frozenset(nav)
        known = {item.capability for item in QUANTBASE_CONTRACT.endpoints}
        capabilities = _object(document["capabilities"], "capabilities")
        if not capabilities or not set(capabilities) <= known:
            raise QuantbaseHarIntakeError("capabilities include an unreviewed route")
        if not self.nav_capabilities <= set(capabilities):
            raise QuantbaseHarIntakeError("NAV capabilities must be declared")
        self.expected: dict[str, tuple[int, int, str | None]] = {}
        for name, value in capabilities.items():
            spec = _object(value, f"capabilities.{name}")
            if set(spec) != {"expected_responses", "expected_records", "records_key"}:
                raise QuantbaseHarIntakeError(
                    "capability keys differ from the contract"
                )
            key = spec["records_key"]
            if key is not None and (
                not isinstance(key, str) or not _PATH_KEY.fullmatch(key)
            ):
                raise QuantbaseHarIntakeError("records_key is invalid")
            self.expected[name] = (
                _count(spec["expected_responses"], "expected_responses", minimum=1),
                _count(spec["expected_records"], "expected_records"),
                key,
            )
        origins = _array(document["ignored_origins"], "ignored_origins")
        if any(
            not isinstance(item, str) or not _ORIGIN.fullmatch(item) for item in origins
        ):
            raise QuantbaseHarIntakeError("ignored_origins are invalid")
        self.permitted_origins = _BASE_ORIGINS | frozenset(origins)


def _load_manifest(data: bytes) -> Manifest:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuantbaseHarIntakeError("manifest is not valid UTF-8 JSON") from exc
    return Manifest(_object(document, "manifest"))


def _read_file(path: Path, name: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise QuantbaseHarIntakeError(f"{name} is missing or linked")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise QuantbaseHarIntakeError(f"{name} is unreadable") from exc


def stage_intake(
    manifest_path: Path, capture_dir: Path, staging_root: Path
) -> dict[str, object]:
    """Copy the owner's HAR files create-only into a hash-bound intake directory."""

    manifest_bytes = _read_file(manifest_path, "manifest")
    manifest = _load_manifest(manifest_bytes)
    capture = _absolute_directory(capture_dir, "capture_dir")
    staging = _absolute_directory(staging_root, "staging_root")
    if (
        _is_within(staging, _REPOSITORY_ROOT)
        or _is_within(staging, capture)
        or _is_within(capture, staging)
    ):
        raise QuantbaseHarIntakeError(
            "staging root must be disjoint from the repository and the capture"
        )
    children = {path.name: path for path in capture.iterdir()}
    if set(children) != set(manifest.file_hashes):
        raise QuantbaseHarIntakeError("capture directory differs from the manifest")
    files: list[dict[str, object]] = []
    for name in sorted(children):
        path = children[name]
        if path.is_symlink() or not path.is_file():
            raise QuantbaseHarIntakeError("capture file is missing or linked")
        digest, size = _hash_path(path)
        declared = manifest.file_hashes[name]
        if declared is not None and declared != digest:
            raise QuantbaseHarIntakeError(f"capture file hash differs: {name}")
        if size == 0:
            raise QuantbaseHarIntakeError(f"capture file is empty: {name}")
        files.append({"bytes": size, "name": name, "sha256": digest})
    manifest_sha = _sha256(manifest_bytes)
    intake_id = _sha256(_canonical({"files": files, "manifest_sha256": manifest_sha}))[
        :32
    ]
    intake = staging / "quantbase-har-intake" / intake_id
    if intake.exists():
        raise QuantbaseHarIntakeError("intake already exists; staging is create-only")
    intake.mkdir(parents=True)
    try:
        for entry in files:
            name = str(entry["name"])
            target = intake / _RAW / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with children[name].open("rb") as source, target.open("xb") as handle:
                shutil.copyfileobj(source, handle, 1 << 20)
            if _hash_path(target) != (entry["sha256"], entry["bytes"]):
                raise QuantbaseHarIntakeError(f"staged copy differs: {name}")
        _write_new(intake / _MANIFEST_FILE, manifest_bytes)
        record = {
            "files": files,
            "intake_id": intake_id,
            "manifest_sha256": manifest_sha,
            "provider_id": PROVIDER_ID,
            "schema_version": SCHEMA_VERSION,
            "state": "STAGED",
        }
        _write_new(intake / _INTAKE_FILE, _canonical(record))
    except BaseException:
        shutil.rmtree(intake, ignore_errors=True)
        raise
    return {
        "file_count": len(files),
        "intake_dir": str(intake),
        "intake_id": intake_id,
        "state": "STAGED",
    }


def _load_intake(intake_dir: Path) -> tuple[Path, Manifest, dict[str, Any]]:
    root = _absolute_directory(intake_dir, "intake_dir")
    manifest_bytes = _read_file(root / _MANIFEST_FILE, "staged manifest")
    record = _object(
        json.loads(_read_file(root / _INTAKE_FILE, "intake manifest")), "intake"
    )
    if record.get("manifest_sha256") != _sha256(manifest_bytes):
        raise QuantbaseHarIntakeError("staged manifest differs from the intake")
    manifest = _load_manifest(manifest_bytes)
    files = _array(record.get("files"), "intake.files")
    raw = root / _RAW
    if raw.is_symlink() or not raw.is_dir():
        raise QuantbaseHarIntakeError("raw directory is missing or linked")
    actual = {path.name for path in raw.iterdir()}
    if actual != {str(item["name"]) for item in files} or actual != set(
        manifest.file_hashes
    ):
        raise QuantbaseHarIntakeError("raw inventory differs from the manifest")
    for item in files:
        path = raw / str(item["name"])
        if path.is_symlink() or not path.is_file():
            raise QuantbaseHarIntakeError("raw file is missing or linked")
        if _hash_path(path) != (item["sha256"], item["bytes"]):
            raise QuantbaseHarIntakeError(
                f"raw file differs from its hash: {path.name}"
            )
    return root, manifest, record


def _parsed_date(value: object) -> date | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            stamp = float(value)
        elif isinstance(value, str) and value.strip():
            raw = value.strip()
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
                stamp = float(raw)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed.date() if 2000 <= parsed.year <= 2100 else None
        else:
            return None
        if stamp > 10_000_000_000:
            stamp /= 1000
        result = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None
    return result if 2000 <= result.year <= 2100 else None


def _walk_dates(
    value: object, fields: frozenset[str], found: list[date], unparsed: list[int]
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields and isinstance(item, (str, int, float)):
                parsed = _parsed_date(item)
                if parsed is None:
                    if not (isinstance(item, str) and not item.strip()):
                        unparsed[0] += 1
                else:
                    found.append(parsed)
            else:
                _walk_dates(item, fields, found, unparsed)
    elif isinstance(value, list):
        for item in value:
            _walk_dates(item, fields, found, unparsed)


def _record_count(response: object, path: str | None) -> int:
    if path is None:
        if isinstance(response, list):
            return len(response)
        if isinstance(response, dict):
            return 1
        raise QuantbaseHarIntakeError("response shape is not an object or array")
    node: object = response
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise QuantbaseHarIntakeError("response lacks the declared records_key")
        node = node[part]
    if not isinstance(node, list):
        raise QuantbaseHarIntakeError("declared records_key is not an array")
    return len(node)


def _combined_document(raw: Path, names: Sequence[str]) -> tuple[dict[str, Any], int]:
    entries: list[Any] = []
    session_files = 0
    for name in sorted(names):
        try:
            document = _object(json.loads((raw / name).read_bytes()), name)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuantbaseHarIntakeError(
                f"capture is not valid HAR JSON: {name}"
            ) from exc
        log = _object(document.get("log"), "log")
        batch = _array(log.get("entries"), "log.entries")
        entries.extend(batch)
        if _session_values(batch):
            session_files += 1
    if not entries:
        raise QuantbaseHarIntakeError("capture has no entries")
    return {"log": {"entries": entries}}, session_files


def _session_values(entries: Sequence[Any]) -> set[str]:
    values: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for side in ("request", "response"):
            part = entry.get(side)
            headers = part.get("headers") if isinstance(part, dict) else None
            if not isinstance(headers, list):
                continue
            for header in headers:
                if (
                    isinstance(header, dict)
                    and isinstance(header.get("name"), str)
                    and header["name"].casefold() in _SESSION_HEADERS
                    and isinstance(header.get("value"), str)
                    and len(header["value"]) >= 8
                ):
                    values.add(header["value"])
    return values


def _grid(manifest: Manifest, newest: date) -> tuple[dict[str, object], list[str]]:
    def aligned(day: date) -> bool:
        return (day - manifest.grid_anchor).days % manifest.grid_step == 0

    def prior(day: date) -> date:
        return day - timedelta(
            days=(day - manifest.grid_anchor).days % manifest.grid_step
        )

    flags: list[str] = []
    if not aligned(newest):
        flags.append("NEWEST_NAV_OFF_GRID")
    if not aligned(manifest.cutoff):
        flags.append("CUTOFF_OFF_NAV_GRID")
    block: dict[str, object] = {
        "anchor": manifest.grid_anchor.isoformat(),
        "cutoff": manifest.cutoff.isoformat(),
        "cutoff_on_grid": aligned(manifest.cutoff),
        "latest_grid_date_at_or_before_cutoff": prior(manifest.cutoff).isoformat(),
        "latest_grid_date_at_or_before_newest": prior(newest).isoformat(),
        "newest_nav_date": newest.isoformat(),
        "newest_nav_on_grid": aligned(newest),
        "step_days": manifest.grid_step,
    }
    return block, flags


def _parse(
    root: Path, manifest: Manifest, record: Mapping[str, Any]
) -> dict[str, bytes]:
    raw = root / _RAW
    names = [str(item["name"]) for item in record["files"]]
    document, session_files = _combined_document(raw, names)
    entries: list[Any] = document["log"]["entries"]
    origins: Counter[str] = Counter()
    for entry in entries:
        request = entry.get("request") if isinstance(entry, dict) else None
        url = request.get("url") if isinstance(request, dict) else None
        if not isinstance(url, str):
            raise QuantbaseHarIntakeError("entry request URL must be text")
        parts = urlsplit(url)
        origins[f"{parts.scheme}://{parts.netloc}"] += 1
    unknown = sorted(set(origins) - manifest.permitted_origins)
    if unknown:
        raise QuantbaseHarIntakeError(
            f"capture contains {len(unknown)} origin(s) outside the manifest"
        )
    try:
        extracted = extract_quantbase_har(document, session=manifest.session)
    except WebContractError as exc:
        raise QuantbaseHarIntakeError(
            f"web contract refused the capture: {exc}"
        ) from exc
    responses: Counter[str] = Counter()
    records: Counter[str] = Counter()
    unparsed = [0]
    dates: dict[str, list[date]] = {}
    for item in extracted:
        if item.capability not in manifest.expected:
            raise QuantbaseHarIntakeError(
                "capture holds a capability the manifest omits"
            )
        responses[item.capability] += 1
        records[item.capability] += _record_count(
            item.response, manifest.expected[item.capability][2]
        )
        _walk_dates(
            item.response,
            manifest.window_fields,
            dates.setdefault(item.capability, []),
            unparsed,
        )
    capabilities: dict[str, object] = {}
    for name, (want_responses, want_records, _) in sorted(manifest.expected.items()):
        if responses[name] != want_responses:
            raise QuantbaseHarIntakeError(f"response count differs for {name}")
        if records[name] != want_records:
            raise QuantbaseHarIntakeError(f"record count differs for {name}")
        found = dates.get(name, [])
        if found and (
            min(found) < manifest.window_start or max(found) > manifest.window_end
        ):
            raise QuantbaseHarIntakeError(f"dates fall outside the window for {name}")
        capabilities[name] = {
            "dated_values": len(found),
            "observed_end": max(found).isoformat() if found else None,
            "observed_start": min(found).isoformat() if found else None,
            "records": records[name],
            "responses": responses[name],
        }
    nav_dates = [
        day for name in manifest.nav_capabilities for day in dates.get(name, [])
    ]
    if not nav_dates:
        raise QuantbaseHarIntakeError("NAV capabilities hold no parseable dates")
    newest = max(nav_dates)
    if manifest.cutoff > newest:
        raise QuantbaseHarIntakeError("cutoff is after the newest captured NAV date")
    grid, flags = _grid(manifest, newest)
    if unparsed[0]:
        flags.append("UNPARSED_DATE_VALUES")
    if session_files:
        flags.append("RAW_HAR_HOLDS_SESSION_MATERIAL")
    records_bytes = canonical_extraction(extracted)
    report = {
        "capabilities": capabilities,
        "cutoff": manifest.cutoff.isoformat(),
        "disposed_entries": len(entries) - len(extracted),
        "entries_total": len(entries),
        "extracted_responses": len(extracted),
        "files": record["files"],
        "flags": sorted(flags),
        "intake_id": record["intake_id"],
        "manifest_sha256": record["manifest_sha256"],
        "nav_grid": grid,
        "origins": dict(sorted(origins.items())),
        "provider_id": PROVIDER_ID,
        "raw_files_with_session_material": session_files,
        "records_sha256": _sha256(records_bytes),
        "schema_version": SCHEMA_VERSION,
        "state": "PARSED_WITH_FLAGS" if flags else "PARSED",
        "unparsed_date_values": unparsed[0],
        "window": {
            "end": manifest.window_end.isoformat(),
            "start": manifest.window_start.isoformat(),
        },
    }
    native = {
        "native_artifacts": [
            *(
                {
                    "relative_path": f"har/{name}",
                    "source_path": str(raw / name),
                }
                for name in sorted(names)
            ),
            {
                "relative_path": "capture/intake-manifest.json",
                "source_path": str(root / _INTAKE_FILE),
            },
        ],
        "native_contract": {
            **{f"har/{name}": list(HAR_SCHEMA) for name in sorted(names)},
            "capture/intake-manifest.json": list(INTAKE_SCHEMA),
        },
    }
    return {
        _RECORDS_FILE: records_bytes,
        _REPORT_FILE: _canonical(report),
        _NATIVE_FILE: _canonical(native),
    }


def parse_intake(intake_dir: Path) -> dict[str, object]:
    """Parse a staged intake through the reviewed contract; create-only outputs."""

    root, manifest, record = _load_intake(intake_dir)
    if (root / _REPORT_FILE).exists() or (root / "normalized").exists():
        raise QuantbaseHarIntakeError("intake is already parsed; output is create-only")
    outputs = _parse(root, manifest, record)
    for relative, content in outputs.items():
        _write_new(root / relative, content)
    report = json.loads(outputs[_REPORT_FILE])
    return {
        "flags": report["flags"],
        "intake_id": record["intake_id"],
        "native_artifacts": str(root / _NATIVE_FILE),
        "report": str(root / _REPORT_FILE),
        "state": report["state"],
    }


def verify_intake(intake_dir: Path) -> dict[str, object]:
    """Independently re-derive every output from the raw bytes and compare."""

    root, manifest, record = _load_intake(intake_dir)
    expected = _parse(root, manifest, record)
    for relative, content in expected.items():
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise QuantbaseHarIntakeError(f"derived output differs: {relative}")
    allowed = {
        _RAW,
        "normalized",
        _MANIFEST_FILE,
        _INTAKE_FILE,
        _REPORT_FILE,
        _NATIVE_FILE,
    }
    if {path.name for path in root.iterdir()} != allowed:
        raise QuantbaseHarIntakeError("intake directory holds undeclared paths")
    document, _ = _combined_document(
        root / _RAW, [str(item["name"]) for item in record["files"]]
    )
    outputs = b"".join(content for content in expected.values())
    for value in _session_values(document["log"]["entries"]):
        if value.encode("utf-8") in outputs:
            raise QuantbaseHarIntakeError("derived output leaks session material")
    report = json.loads(expected[_REPORT_FILE])
    return {
        "flags": report["flags"],
        "intake_id": record["intake_id"],
        "state": "VERIFIED_WITH_FLAGS" if report["flags"] else "VERIFIED",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--capture-dir", type=Path, required=True)
    stage.add_argument("--staging-root", type=Path, required=True)
    for name in ("parse", "verify"):
        child = commands.add_parser(name)
        child.add_argument("--intake", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage_intake(args.manifest, args.capture_dir, args.staging_root)
        elif args.command == "parse":
            result = parse_intake(args.intake)
        else:
            result = verify_intake(args.intake)
    except (OSError, QuantbaseHarIntakeError, ValueError) as exc:
        print(f"capture-quantbase-har: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

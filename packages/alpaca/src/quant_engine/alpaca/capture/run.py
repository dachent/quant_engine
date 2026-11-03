"""The governed capture run: account snapshot, positions, paginated activities.

Order of effects for every request: policy check, durable governance
reservation, one GET, exact raw bytes staged create-only, then the request is
recorded.  Nothing is retried: a rate-limit or error response stops the run with
its raw evidence and a manifest that says why (``terminal.complete`` false), and
a later run needs fresh governance evidence.  Normalization runs only after the
raw bytes are durable, so an unknown shape never loses evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from quant_engine.alpaca.capture.catalog import PAGE_CURSOR, catalog_sha256
from quant_engine.alpaca.capture.config import CaptureConfig
from quant_engine.alpaca.capture.errors import (
    CaptureError,
    CaptureIncomplete,
    CaptureRefused,
    GovernanceError,
    NormalizationError,
)
from quant_engine.alpaca.capture.fetch import GovernedFetcher, RawResponse
from quant_engine.alpaca.capture.governance import LiveReadGovernance
from quant_engine.alpaca.capture.normalize import (
    account_binding_matches,
    parse_json_bytes,
)
from quant_engine.alpaca.capture.outputs import derive_outputs
from quant_engine.alpaca.capture.staging import RunDirectory
from quant_engine.alpaca.capture.util import canonical_json, sha256_bytes, sha256_text

DRIVER_ID = "alpaca-capture-v1"
MANIFEST_FILE = "manifest.json"
MANIFEST_DIGEST_FILE = "MANIFEST.sha256"
REQUEST_MANIFEST_FILE = "request_manifest.jsonl"
DIRECTION = "asc"


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RunResult:
    run_directory: Path
    manifest_sha256: str
    request_count: int
    activity_count: int


def _retry_after(headers: dict[str, str] | Any) -> int | None:
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    return int(value) if isinstance(value, str) and value.isdigit() else None


class _Recorder:
    def __init__(
        self,
        run: RunDirectory,
        fetcher: GovernedFetcher,
        clock: Callable[[], datetime],
    ) -> None:
        self.run = run
        self._fetcher = fetcher
        self._clock = clock
        self.requests: list[dict[str, Any]] = []
        self.raw_files: dict[str, str] = {}
        self.bodies: dict[str, list[bytes]] = {}

    def get(
        self, report: str, path: str, query: dict[str, str]
    ) -> tuple[RawResponse, int]:
        requested_at = self._clock().astimezone(timezone.utc).isoformat()
        try:
            _, response = self._fetcher.get("GET", path, query)
        except GovernanceError:
            raise _Stop("governance_refused") from None
        except CaptureRefused:
            raise _Stop("request_refused") from None
        except CaptureError:
            raise _Stop("transport_failure") from None
        sequence = len(self.requests) + 1
        relative = f"raw/{sequence:04d}-{report}.bin"
        digest = self.run.write_new(relative, response.body)
        self.raw_files[relative] = digest
        record: dict[str, Any] = {
            "sequence": sequence,
            "report": report,
            "method": "GET",
            "path": path,
            "safe_params": dict(sorted(query.items())),
            "status": response.status,
            "response_sha256": digest,
            "response_bytes": len(response.body),
            "raw_relpath": relative,
            "requested_at": requested_at,
        }
        retry = _retry_after(response.headers)
        if response.status == 429:
            record["retry_after_seconds"] = retry
        self.requests.append(record)
        if response.status == 429:
            raise _Stop("rate_limited")
        if response.status != 200:
            raise _Stop(f"http_status_{response.status}")
        self.bodies.setdefault(report, []).append(response.body)
        return response, sequence

    def set_row_count(self, sequence: int, rows: int) -> None:
        self.requests[sequence - 1]["row_count"] = rows


def window_of(config: CaptureConfig) -> dict[str, Any]:
    return {
        "after": config.after,
        "until": config.until,
        "activity_types": list(config.activity_types),
        "direction": DIRECTION,
        "page_size": config.page_size,
        "max_pages": config.max_pages,
        "include_positions": config.include_positions,
    }


def activity_query(window: Mapping[str, Any], cursor: str | None) -> dict[str, str]:
    """The exact activities query a declared window implies (shared with verify)."""

    query = {
        "direction": str(window["direction"]),
        "page_size": str(window["page_size"]),
    }
    if window["activity_types"]:
        query["activity_types"] = ",".join(window["activity_types"])
    if window["after"] is not None:
        query["after"] = str(window["after"])
    if window["until"] is not None:
        query["until"] = str(window["until"])
    if cursor is not None:
        query[PAGE_CURSOR] = cursor
    return query


def _capture(config: CaptureConfig, recorder: _Recorder) -> None:
    """Run every request; return nothing, leaving bodies in the recorder."""

    binding = sha256_text(config.account_id)
    recorder.get("account", "/v2/account", {})
    try:
        account = parse_json_bytes(recorder.bodies["account"][0])
    except NormalizationError:
        raise _Stop("unparseable_response") from None
    if not account_binding_matches(account, binding):
        raise _Stop("account_binding_mismatch")
    if config.include_positions:
        recorder.get("positions", "/v2/positions", {})
    cursor: str | None = None
    seen: set[str] = set()
    for _page in range(config.max_pages):
        _, sequence = recorder.get(
            "activities",
            "/v2/account/activities",
            activity_query(window_of(config), cursor),
        )
        try:
            page = parse_json_bytes(recorder.bodies["activities"][-1])
        except NormalizationError:
            raise _Stop("unparseable_response") from None
        if not isinstance(page, list):
            raise _Stop("unparseable_response")
        recorder.set_row_count(sequence, len(page))
        if len(page) < config.page_size:
            return None
        last = page[-1]
        next_cursor = last.get("id") if isinstance(last, dict) else None
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor == cursor
            or next_cursor in seen
        ):
            raise _Stop("cursor_not_advancing")
        seen.add(next_cursor)
        cursor = next_cursor
    raise _Stop("max_pages_reached")


def run_capture(
    *,
    config: CaptureConfig,
    fetcher: GovernedFetcher,
    governance: LiveReadGovernance,
    staging_root: str | Path,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> RunResult:
    """Capture, stage, normalize, and hash-bind one run; raise if it is partial."""

    run = RunDirectory(staging_root, run_id)
    recorder = _Recorder(run, fetcher, clock or (lambda: datetime.now(timezone.utc)))
    reason = "complete"
    detail = ""
    try:
        _capture(config, recorder)
    except _Stop as stop:
        reason = stop.reason
    normalized: dict[str, bytes] = {}
    if reason == "complete":
        try:
            normalized = derive_outputs(
                binding_sha256=sha256_text(config.account_id),
                account_body=recorder.bodies["account"][0],
                positions_body=(
                    recorder.bodies["positions"][0]
                    if config.include_positions
                    else None
                ),
                activity_bodies=recorder.bodies["activities"],
            )
        except NormalizationError as error:
            reason = "normalization_refused"
            detail = str(error)
    normalized_files = {
        relative: run.write_new(relative, data) for relative, data in normalized.items()
    }
    request_lines = b"".join(
        (canonical_json(record) + "\n").encode("utf-8") for record in recorder.requests
    )
    request_digest = run.write_new(REQUEST_MANIFEST_FILE, request_lines)
    activity_rows = sum(
        int(record.get("row_count", 0))
        for record in recorder.requests
        if record["report"] == "activities"
    )
    manifest = {
        "schema_version": 1,
        "provider": "alpaca",
        "driver": DRIVER_ID,
        "classification": "restricted-private",
        "environment": config.environment,
        "account_binding_sha256": sha256_text(config.account_id),
        "catalog_sha256": catalog_sha256(),
        "governance_evidence_sha256": governance.evidence_sha256,
        "window": window_of(config),
        "request_count": len(recorder.requests),
        "request_manifest_sha256": request_digest,
        "raw_files": dict(sorted(recorder.raw_files.items())),
        "normalized_files": dict(sorted(normalized_files.items())),
        "activity_row_count": activity_rows,
        "terminal": {
            "complete": reason == "complete",
            "reason": reason,
            "detail": detail,
        },
    }
    manifest_bytes = (canonical_json(manifest) + "\n").encode("utf-8")
    run.write_new(MANIFEST_FILE, manifest_bytes)
    manifest_digest = sha256_bytes(manifest_bytes)
    run.write_new(MANIFEST_DIGEST_FILE, f"{manifest_digest}\n".encode("ascii"))
    if reason != "complete":
        raise CaptureIncomplete(reason, str(run.root))
    return RunResult(
        run_directory=run.root,
        manifest_sha256=manifest_digest,
        request_count=len(recorder.requests),
        activity_count=activity_rows,
    )


def read_manifest(run_directory: Path) -> dict[str, Any]:
    value = json.loads((run_directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CaptureError("the manifest is not an object")
    return value

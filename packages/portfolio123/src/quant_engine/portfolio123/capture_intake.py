"""Create-only staging, normalization, and independent verification of owner exports.

The owner signs in and exports a HAR (or saves a file) by hand; nothing here logs
in, fetches, or calls the provider.  One run stages exactly one source artifact
under a caller-supplied staging root outside any repository:

    <staging-root>/p123-capture/<run-id>/
        raw/source.<ext>        byte copy of the owner export (create-only)
        inventory.json          route templates and dispositions, no bodies
        normalized/records.json canonical shape-checked records
        manifest.json           hash-bound index, written last

Parsers fail closed: an unreviewed route is only inventoried, never parsed; a
reviewed route whose response is not exactly the catalog shape refuses the run.
Real provider shapes are UNVERIFIED (see ``capture_catalog``).  Nothing in a run
is ever printed except counts, hashes, and stable reason codes.
"""

from __future__ import annotations

import base64
import binascii
import csv
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from quant_engine.core import (
    StrictJSONError,
    canonical_json_bytes,
    strict_json_object,
)
from quant_engine.portfolio123.capture_catalog import (
    FORM_FILE,
    FORM_HAR,
    ORIGIN,
    CaptureRefusal,
    CaptureRoute,
    capture_contract,
    catalog_sha256,
    evidence_status,
    html_routes,
    match_html_route,
    route_for,
)
from quant_engine.portfolio123.capture_html import (
    ROWS_PER_PAGE,
    HtmlRefusal,
    parse_transactions_page,
)
from quant_engine.portfolio123.capture_shapes import (
    ShapeError,
    Spec,
    canonical_bytes,
    redact_keys,
    validate_shape,
)
from quant_engine.web_contracts import (
    PORTFOLIO123_CONTRACT,
    ExtractedResponse,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_har,
)


SCHEMA_VERSION = 1
KIND = "p123-capture-run"
RUN_DIRECTORY = "p123-capture"
INVENTORY_DIRECTORY = "p123-capture-inventory"
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_CSV_ROWS = 2_000_000
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Only structural words already present in reviewed or catalogued routes survive in an
# inventory; every other segment is redacted unless the owner opts in to reveal it.
_STRUCTURAL = frozenset(
    part
    for path in (
        *(item.path for item in capture_contract().endpoints),
        *(item.path for item in html_routes() if item.path is not None),
        *(item.path for item in PORTFOLIO123_CONTRACT.endpoints),
    )
    for part in path.split("/")
    if part and not part.startswith("{")
) | frozenset({"api", "app", "spr", "static", "v1", "v2"})
_FOREIGN = "FOREIGN_ORIGIN"
_REVEALABLE = re.compile(r"^[A-Za-z][A-Za-z_.-]{0,59}$")
_MATCHED = "MATCHED"
_UNREVIEWED = "UNREVIEWED_ROUTE"
_REPARSE_POINT = 0x0400
_BINARY = getattr(os, "O_BINARY", 0)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "form",
        "catalog_sha256",
        "evidence_status",
        "scope",
        "captured_at",
        "session_expires_at",
        "source",
        "files",
        "capabilities",
        "inventory_totals",
        "complete",
        "allow_partial",
        "raw_contains_session_material",
        "restricted",
    }
)


@dataclass(frozen=True)
class StageRequest:
    staging_root: Path
    run_id: str
    form: str
    source: Path
    capabilities: tuple[str, ...]
    captured_at: datetime | None = None
    session_expires_at: datetime | None = None
    allow_partial: bool = False
    strategy_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunSummary:
    """Aggregate facts only: counts, hashes, and flags."""

    run_id: str
    manifest_sha256: str
    source_sha256: str
    records: int
    complete: bool
    capabilities: Mapping[str, Mapping[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "source_sha256": self.source_sha256,
            "records": self.records,
            "complete": self.complete,
            "capabilities": {
                key: dict(self.capabilities[key]) for key in sorted(self.capabilities)
            },
        }


@dataclass(frozen=True)
class _Derived:
    """Everything derived from the raw bytes (re-derived by the verifier)."""

    normalized: bytes
    inventory: bytes
    capabilities: dict[str, dict[str, object]]
    totals: dict[str, int]
    complete: bool


# --------------------------------------------------------------------------- filesystem


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        raise CaptureRefusal("PATH_UNREADABLE") from None
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT)


def _read_regular_file(path: Path, limit: int) -> bytes:
    if not path.is_absolute():
        raise CaptureRefusal("PATH_NOT_ABSOLUTE")
    if not path.exists() or _is_link(path) or not path.is_file():
        raise CaptureRefusal("SOURCE_NOT_A_REGULAR_FILE")
    if path.stat().st_size > limit:
        raise CaptureRefusal("SOURCE_TOO_LARGE")
    try:
        data = path.read_bytes()
    except OSError:
        raise CaptureRefusal("PATH_UNREADABLE") from None
    if not data:
        raise CaptureRefusal("SOURCE_EMPTY")
    return data


def _write_new(path: Path, data: bytes) -> None:
    """Create-only write: refuses to replace anything that already exists."""

    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise CaptureRefusal("DESTINATION_EXISTS") from None
    except OSError:
        raise CaptureRefusal("WRITE_FAILED") from None


def _private_directory(path: Path) -> None:
    """Create a directory (if absent) readable by the owner only on POSIX."""

    path.mkdir(mode=0o700, exist_ok=True)


def _check_staging_root(root: Path) -> None:
    if not root.is_absolute():
        raise CaptureRefusal("PATH_NOT_ABSOLUTE")
    if not root.is_dir() or _is_link(root):
        raise CaptureRefusal("STAGING_ROOT_INVALID")
    if os.name != "nt" and root.stat().st_mode & 0o077:
        raise CaptureRefusal("STAGING_ROOT_NOT_PRIVATE")
    for parent in (root, *root.parents):
        if (parent / ".git").exists():
            raise CaptureRefusal("STAGING_ROOT_IN_REPOSITORY")


def _run_directory(root: Path, run_id: str) -> Path:
    if _RUN_ID.fullmatch(run_id) is None:
        raise CaptureRefusal("RUN_ID_INVALID")
    return root / RUN_DIRECTORY / run_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- HAR


def _template(url: str, reveal: bool = False) -> tuple[str, str, tuple[str, ...]]:
    """Origin class, id-free path template, and sorted query keys."""

    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    origin_class = "PROVIDER" if origin == ORIGIN else _FOREIGN
    if origin_class == _FOREIGN:
        return origin_class, "", ()
    segments = []
    for part in parsed.path.split("/"):
        if not part:
            continue
        if part.isdigit():
            segments.append("{n}")
        elif part in _STRUCTURAL or (reveal and _REVEALABLE.fullmatch(part)):
            segments.append(part)
        else:
            segments.append("{x}")
    keys = sorted({key.casefold() for key, _ in parse_qsl(parsed.query)})
    return origin_class, "/" + "/".join(segments), tuple(keys)


def _har_entries(source: bytes) -> list[dict[str, Any]]:
    try:
        document = strict_json_object(source, label="har")
    except StrictJSONError:
        raise CaptureRefusal("SOURCE_NOT_STRICT_JSON") from None
    log = document.get("log")
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list) or not entries:
        raise CaptureRefusal("HAR_MALFORMED")
    if not all(isinstance(item, dict) for item in entries):
        raise CaptureRefusal("HAR_MALFORMED")
    return [item for item in entries if isinstance(item, dict)]


def _entry_facts(entry: dict[str, Any]) -> tuple[str, str, int, str]:
    request = entry.get("request")
    response = entry.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise CaptureRefusal("HAR_MALFORMED")
    method, url, status = (
        request.get("method"),
        request.get("url"),
        response.get("status"),
    )
    content = response.get("content")
    mime = content.get("mimeType", "") if isinstance(content, dict) else ""
    if not isinstance(method, str) or not isinstance(url, str):
        raise CaptureRefusal("HAR_MALFORMED")
    if isinstance(status, bool) or not isinstance(status, int):
        raise CaptureRefusal("HAR_MALFORMED")
    return (
        method.upper(),
        url,
        status,
        mime.split(";")[0].strip().casefold() if isinstance(mime, str) else "",
    )


def _check_page_set(
    capability: str, responses: Sequence[ExtractedResponse]
) -> dict[str, object]:
    pages: dict[int, int] = {}
    counts: set[tuple[int, int]] = set()
    for item in responses:
        body = item.response
        if not isinstance(body, dict):
            raise CaptureRefusal("SHAPE_REFUSED", capability)
        rows = body["transactions"]
        pages[int(body["page"])] = len(rows) if isinstance(rows, list) else 0
        counts.add((int(body["page_count"]), int(body["total"])))
    if len(counts) != 1:
        raise CaptureRefusal("PAGINATION_INCONSISTENT", capability)
    ((page_count, total),) = counts
    complete = (
        set(pages) == set(range(1, page_count + 1)) and sum(pages.values()) == total
    )
    return {
        "records": len(responses),
        "pages_seen": len(pages),
        "page_count": page_count,
        "coverage": "COMPLETE" if complete else "PARTIAL",
    }


_InventoryKey = tuple[str, str, str, tuple[str, ...], int, str]


def _classify(
    source: bytes, reveal: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[_InventoryKey]]:
    """Split HAR entries into reviewed-route matches and an id-free inventory."""

    contract = capture_contract()
    entries = _har_entries(source)
    matched: list[dict[str, Any]] = []
    inventory: Counter[_InventoryKey] = Counter()
    for entry in entries:
        method, url, status, mime = _entry_facts(entry)
        origin_class, template, keys = _template(url, reveal)
        try:
            contract.route(method, url)
            disposition = _MATCHED
            matched.append(entry)
        except WebContractError:
            if match_html_route(method, url) is not None:
                disposition = _MATCHED
                matched.append(entry)
            else:
                disposition = _FOREIGN if origin_class == _FOREIGN else _UNREVIEWED
        inventory[(disposition, method, template, keys, status, mime)] += 1
    return entries, matched, inventory


def _inventory_rows(
    inventory: Counter[_InventoryKey], entries: int, matched: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = [
        {
            "disposition": key[0],
            "method": key[1],
            "path_template": key[2],
            "query_keys": list(key[3]),
            "status": key[4],
            "mime": key[5],
            "count": count,
        }
        for key, count in sorted(inventory.items())
    ]
    totals = {
        "entries": entries,
        "matched": matched,
        "duplicates_dropped": 0,
        "unreviewed": sum(n for key, n in inventory.items() if key[0] == _UNREVIEWED),
        "foreign": sum(n for key, n in inventory.items() if key[0] == _FOREIGN),
    }
    return rows, totals


def inventory_har(
    staging_root: Path,
    run_id: str,
    source_path: Path,
    *,
    reveal_segments: bool = False,
) -> dict[str, object]:
    """Write a create-only, id-free route inventory for route review.

    No body is parsed, no source copy is kept, and nothing is normalized.  The
    inventory is the input to the catalog and contract review that must precede
    any body parsing.
    """

    _check_staging_root(staging_root)
    if _RUN_ID.fullmatch(run_id) is None:
        raise CaptureRefusal("RUN_ID_INVALID")
    entries, matched, inventory = _classify(
        _read_regular_file(source_path, MAX_SOURCE_BYTES), reveal_segments
    )
    rows, totals = _inventory_rows(inventory, len(entries), len(matched))
    data = canonical_bytes(
        {
            "form": FORM_HAR,
            "routes": rows,
            "segments_revealed": reveal_segments,
            "totals": totals,
        }
    )
    directory = staging_root / INVENTORY_DIRECTORY
    _private_directory(directory)
    _write_new(directory / f"{run_id}.json", data)
    return {"inventory_sha256": _sha256(data), "routes": len(rows), **totals}


def _header_values(request: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    headers = request.get("headers")
    if not isinstance(headers, list):
        raise CaptureRefusal("HAR_ENTRY_REJECTED", "request headers")
    for item in headers:
        name = item.get("name") if isinstance(item, dict) else None
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(name, str) or not isinstance(value, str):
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "request headers")
        lowered = name.casefold()
        if lowered == "cookie" and lowered in result:
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "duplicate session header")
        result.setdefault(lowered, value)
    return result


def _html_body(response: Mapping[str, Any]) -> bytes:
    content = response.get("content")
    if not isinstance(content, dict):
        raise CaptureRefusal("HAR_ENTRY_REJECTED", "response content")
    mime = content.get("mimeType")
    text = content.get("text")
    if (
        not isinstance(mime, str)
        or mime.split(";")[0].strip().casefold() != "text/html"
        or not isinstance(text, str)
    ):
        raise CaptureRefusal("HAR_ENTRY_REJECTED", "html content")
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "html encoding") from None
    return text.encode("utf-8")


def strategy_hash(strategy_id: str) -> str:
    """The identifier-free binding of a strategy id (never the id itself)."""

    return _sha256(b"p123-strategy:" + strategy_id.encode())


def _html_records(
    entries: Sequence[dict[str, Any]], *, captured_at: datetime, expires_at: datetime
) -> tuple[list[ExtractedResponse], dict[str, tuple[str, ...]]]:
    """Admit HTML list pages: same gates as the JSON extractor, then the parser."""

    try:
        SessionEnvelope(captured_at, expires_at).validate()
    except WebContractError as exc:
        raise CaptureRefusal("HAR_ENTRY_REJECTED", str(exc)) from None
    records: list[ExtractedResponse] = []
    sequences: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        request, response = entry["request"], entry["response"]
        url = str(request["url"])
        matched = match_html_route(str(request["method"]), url)
        if matched is None:
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "route")
        route, strategy, offset = matched
        if len(_header_values(request).get("cookie", "").strip()) < 16:
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "session evidence")
        status = response["status"]
        if status < 200 or status >= 300:
            raise CaptureRefusal("HAR_ENTRY_REJECTED", "status")
        try:
            page = parse_transactions_page(
                _html_body(response), strategy=strategy, offset=offset
            )
        except HtmlRefusal as exc:
            raise CaptureRefusal(exc.code, route.capability) from None
        request_sha = _sha256(
            canonical_json_bytes(
                {
                    "capability": route.capability,
                    "method": "GET",
                    "origin": ORIGIN,
                    "path": urlsplit(url).path,
                    "query": {"offset": str(offset), "portid": strategy},
                }
            )
        )
        sequences[request_sha] = page.sequences
        records.append(
            ExtractedResponse(
                capability=route.capability,
                captured_at=captured_at,
                request_sha256=request_sha,
                response={
                    "strategy_sha256": strategy_hash(strategy),
                    "offset": offset,
                    "first": page.first,
                    "last": page.last,
                    "total": page.total,
                    "rows": list(page.rows),
                },
                status=status,
            )
        )
    return records, sequences


def _check_html_pages(
    capability: str,
    responses: Sequence[ExtractedResponse],
    sequences: Mapping[str, tuple[str, ...]],
    declared: tuple[str, ...],
) -> dict[str, object]:
    """Offset pagination: consistent totals, 50-row steps, no gaps, no repeats.

    The strategies found must be exactly the ones the owner declared (by hash).
    """

    by_strategy: dict[str, list[ExtractedResponse]] = {}
    for item in responses:
        body = item.response
        if not isinstance(body, dict):
            raise CaptureRefusal("SHAPE_REFUSED", capability)
        by_strategy.setdefault(str(body["strategy_sha256"]), []).append(item)
    if not declared:
        raise CaptureRefusal("STRATEGIES_REQUIRED", capability)
    if set(by_strategy) != set(declared):
        raise CaptureRefusal("STRATEGY_MISMATCH", capability)
    pages = rows = page_count = 0
    complete = True
    for group in by_strategy.values():
        bodies = [item.response for item in group if isinstance(item.response, dict)]
        totals = {int(body["total"]) for body in bodies}
        if len(totals) != 1:
            raise CaptureRefusal("PAGINATION_INCONSISTENT", capability)
        (total,) = totals
        offsets = {int(body["offset"]) for body in bodies}
        expected = set(range(0, total, ROWS_PER_PAGE))
        if len(offsets) != len(bodies) or not offsets <= expected:
            raise CaptureRefusal("PAGINATION_INCONSISTENT", capability)
        seen = [seq for item in group for seq in sequences[item.request_sha256]]
        if len(set(seen)) != len(seen):
            raise CaptureRefusal("DUPLICATE_ROWS", capability)
        pages += len(bodies)
        rows += sum(len(body["rows"]) for body in bodies)
        page_count += len(expected)
        complete = complete and offsets == expected
    return {
        "records": pages,
        "pages_seen": pages,
        "page_count": page_count,
        "strategies": len(by_strategy),
        "strategy_sha256s": sorted(by_strategy),
        "rows": rows,
        "coverage": "COMPLETE" if complete else "PARTIAL",
    }


def _normalize_har(
    source: bytes,
    *,
    capabilities: tuple[str, ...],
    captured_at: datetime,
    expires_at: datetime,
    allow_partial: bool,
    strategy_hashes: tuple[str, ...] = (),
) -> _Derived:
    contract = capture_contract()
    entries, matched, inventory = _classify(source)
    if not matched:
        raise CaptureRefusal("NO_REVIEWED_ROUTES")
    html_entries: list[dict[str, Any]] = []
    json_entries: list[dict[str, Any]] = []
    for entry in matched:
        method, url = _entry_facts(entry)[:2]
        target = html_entries if match_html_route(method, url) else json_entries
        target.append(entry)
    extracted: list[ExtractedResponse] = []
    if json_entries:
        try:
            extracted.extend(
                extract_har(
                    {"log": {"entries": json_entries}},
                    provider=contract,
                    session=SessionEnvelope(captured_at, expires_at),
                )
            )
        except WebContractError as exc:
            raise CaptureRefusal("HAR_ENTRY_REJECTED", str(exc)) from None
    sequences: dict[str, tuple[str, ...]] = {}
    if html_entries:
        pages, sequences = _html_records(
            html_entries, captured_at=captured_at, expires_at=expires_at
        )
        extracted.extend(pages)
    unique: dict[tuple[str, str], ExtractedResponse] = {}
    duplicates = 0
    for item in extracted:
        key = (item.capability, item.request_sha256)
        if key in unique:
            if canonical_bytes(unique[key].to_dict()) != canonical_bytes(
                item.to_dict()
            ):
                raise CaptureRefusal("CONFLICTING_DUPLICATE", item.capability)
            duplicates += 1
            continue
        unique[key] = item
    records = tuple(unique.values())
    present = {item.capability for item in records}
    if present != set(capabilities):
        raise CaptureRefusal(
            "SCOPE_MISMATCH",
            "missing" if set(capabilities) - present else "unexpected",
        )
    summary: dict[str, dict[str, object]] = {}
    for capability in sorted(present):
        route = route_for(capability)
        group = [item for item in records if item.capability == capability]
        for item in group:
            try:
                validate_shape(item.response, _shape(route))
            except ShapeError as exc:
                raise CaptureRefusal("SHAPE_REFUSED", f"{capability} {exc}") from None
        if route.html is not None:
            summary[capability] = _check_html_pages(
                capability, group, sequences, strategy_hashes
            )
        elif route.paginated:
            summary[capability] = _check_page_set(capability, group)
        else:
            summary[capability] = {"records": len(group), "coverage": "COMPLETE"}
    complete = all(item["coverage"] == "COMPLETE" for item in summary.values())
    if not complete and not allow_partial:
        raise CaptureRefusal("COVERAGE_PARTIAL")
    rows, totals = _inventory_rows(inventory, len(entries), len(matched))
    totals["duplicates_dropped"] = duplicates
    return _Derived(
        normalized=canonical_extraction(records),
        inventory=canonical_bytes({"form": FORM_HAR, "routes": rows, "totals": totals}),
        capabilities=summary,
        totals=totals,
        complete=complete,
    )


# --------------------------------------------------------------------------- files


def _shape(route: CaptureRoute) -> Spec:
    if route.shape is None:
        raise CaptureRefusal("CATALOG_INVALID", "route has no shape")
    return route.shape


def _media_type(route: CaptureRoute, suffix: str, source: bytes) -> str:
    media = suffix.lstrip(".").casefold()
    if media not in route.file_media:
        raise CaptureRefusal("MEDIA_TYPE_REFUSED")
    if media == "pdf" and not source.startswith(b"%PDF-"):
        raise CaptureRefusal("MEDIA_SIGNATURE_MISMATCH")
    return media


def _csv_record(route: CaptureRoute, source: bytes) -> dict[str, Any]:
    try:
        text = source.decode("utf-8-sig")
        table = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error):
        raise CaptureRefusal("CSV_UNREADABLE") from None
    if len(table) < 2:
        raise CaptureRefusal("EXPORT_HAS_NO_ROWS")
    header, rows = table[0], table[1:]
    if tuple(header) not in route.csv_columns:
        raise CaptureRefusal("CSV_COLUMNS_UNREVIEWED")
    if len(rows) > MAX_CSV_ROWS or any(len(row) != len(header) for row in rows):
        raise CaptureRefusal("CSV_SHAPE_REFUSED")
    return {"columns": header, "rows": rows}


def _normalize_file(
    source: bytes, *, capability: str, suffix: str, captured_at: datetime
) -> _Derived:
    route = route_for(capability)
    if FORM_FILE not in route.forms:
        raise CaptureRefusal("FORM_NOT_ACCEPTED")
    media = _media_type(route, suffix, source)
    record: dict[str, Any] = {
        "bytes": len(source),
        "capability": capability,
        "captured_at": captured_at.astimezone(timezone.utc).isoformat(),
        "form": FORM_FILE,
        "media_type": media,
        "sha256": _sha256(source),
    }
    if media == "json":
        try:
            parsed = redact_keys(
                strict_json_object(source, label="export"), route.redact
            )
            validate_shape(parsed, _shape(route))
        except StrictJSONError:
            raise CaptureRefusal("SOURCE_NOT_STRICT_JSON") from None
        except ShapeError as exc:
            raise CaptureRefusal("SHAPE_REFUSED", f"{capability} {exc}") from None
        record["response"] = parsed
    elif media == "csv":
        record.update(_csv_record(route, source))
    return _Derived(
        normalized=canonical_bytes({"records": [record]}),
        inventory=canonical_bytes({"form": FORM_FILE, "routes": [], "totals": {}}),
        capabilities={capability: {"records": 1, "coverage": "COMPLETE"}},
        totals={},
        complete=True,
    )


# --------------------------------------------------------------------------- staging


def _derive(
    source: bytes,
    *,
    form: str,
    capabilities: tuple[str, ...],
    captured_at: datetime,
    expires_at: datetime | None,
    allow_partial: bool,
    suffix: str,
    strategy_hashes: tuple[str, ...] = (),
) -> _Derived:
    if form == FORM_HAR:
        if expires_at is None:
            raise CaptureRefusal("SESSION_BOUNDS_REQUIRED")
        return _normalize_har(
            source,
            capabilities=capabilities,
            captured_at=captured_at,
            expires_at=expires_at,
            allow_partial=allow_partial,
            strategy_hashes=strategy_hashes,
        )
    if form == FORM_FILE and len(capabilities) == 1:
        return _normalize_file(
            source, capability=capabilities[0], suffix=suffix, captured_at=captured_at
        )
    raise CaptureRefusal("FORM_INVALID")


def _aware(value: datetime | None, name: str) -> datetime:
    if value is None or value.tzinfo is None:
        raise CaptureRefusal("TIME_REQUIRED", name)
    return value.astimezone(timezone.utc)


def stage_run(request: StageRequest) -> RunSummary:
    """Create one immutable run from an owner export; refuse to overwrite."""

    if not request.capabilities or len(set(request.capabilities)) != len(
        request.capabilities
    ):
        raise CaptureRefusal("SCOPE_REQUIRED")
    for capability in request.capabilities:
        route_for(capability)
    captured_at = _aware(request.captured_at, "captured_at")
    expires_at = (
        _aware(request.session_expires_at, "session_expires_at")
        if request.form == FORM_HAR
        else None
    )
    _check_staging_root(request.staging_root)
    source = _read_regular_file(request.source, MAX_SOURCE_BYTES)
    suffix = ".har" if request.form == FORM_HAR else request.source.suffix.casefold()
    scope = tuple(sorted(request.capabilities))
    html_scope = any(route_for(item).html is not None for item in scope)
    if request.strategy_ids and not (html_scope and request.form == FORM_HAR):
        raise CaptureRefusal("STRATEGY_IDS_UNEXPECTED")
    if any(
        re.fullmatch(r"[1-9][0-9]{0,11}", item) is None for item in request.strategy_ids
    ) or len(set(request.strategy_ids)) != len(request.strategy_ids):
        raise CaptureRefusal("STRATEGY_IDS_INVALID")
    derived = _derive(
        source,
        form=request.form,
        capabilities=scope,
        captured_at=captured_at,
        expires_at=expires_at,
        allow_partial=request.allow_partial,
        suffix=suffix,
        strategy_hashes=tuple(sorted(strategy_hash(i) for i in request.strategy_ids)),
    )
    run_dir = _run_directory(request.staging_root, request.run_id)
    _private_directory(request.staging_root / RUN_DIRECTORY)
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise CaptureRefusal("RUN_EXISTS") from None
    try:
        (run_dir / "raw").mkdir(mode=0o700)
        (run_dir / "normalized").mkdir(mode=0o700)
        raw_name = f"raw/source{suffix}"
        parts = {
            raw_name: ("raw", source),
            "inventory.json": ("inventory", derived.inventory),
            "normalized/records.json": ("normalized", derived.normalized),
        }
        files = []
        for relative, (role, data) in sorted(parts.items()):
            _write_new(run_dir / relative, data)
            files.append(
                {
                    "path": relative,
                    "role": role,
                    "sha256": _sha256(data),
                    "bytes": len(data),
                }
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "run_id": request.run_id,
            "form": request.form,
            "catalog_sha256": catalog_sha256(),
            "evidence_status": evidence_status(scope),
            "scope": list(scope),
            "captured_at": captured_at.isoformat(),
            "session_expires_at": None
            if expires_at is None
            else expires_at.isoformat(),
            "source": {
                "sha256": _sha256(source),
                "bytes": len(source),
                "suffix": suffix,
            },
            "files": files,
            "capabilities": derived.capabilities,
            "inventory_totals": derived.totals,
            "complete": derived.complete,
            "allow_partial": request.allow_partial,
            "raw_contains_session_material": request.form == FORM_HAR,
            "restricted": True,
        }
        manifest_bytes = canonical_bytes(manifest)
        _write_new(run_dir / "manifest.json", manifest_bytes)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return RunSummary(
        run_id=request.run_id,
        manifest_sha256=_sha256(manifest_bytes),
        source_sha256=_sha256(source),
        records=sum(
            int(str(item["records"])) for item in derived.capabilities.values()
        ),
        complete=derived.complete,
        capabilities=derived.capabilities,
    )


# --------------------------------------------------------------------------- verification


def _parse_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise CaptureRefusal("MANIFEST_INVALID") from None
    if parsed.tzinfo is None:
        raise CaptureRefusal("MANIFEST_INVALID")
    return parsed


def _unknown(capability: str) -> bool:
    try:
        route_for(capability)
    except CaptureRefusal:
        return True
    return False


def _check_records(normalized: bytes, scope: tuple[str, ...], form: str) -> None:
    """Independent structural re-validation of the stored records."""

    try:
        document = strict_json_object(normalized, label="records")
    except StrictJSONError:
        raise CaptureRefusal("RECORDS_INVALID") from None
    records = document.get("records")
    if set(document) != {"records"} or not isinstance(records, list) or not records:
        raise CaptureRefusal("RECORDS_INVALID")
    seen: set[str] = set()
    for record in records:
        capability = record.get("capability") if isinstance(record, dict) else None
        if not isinstance(capability, str) or capability not in scope:
            raise CaptureRefusal("RECORDS_INVALID")
        route = route_for(capability)
        if form not in route.forms or record.get("form", form) != form:
            raise CaptureRefusal("RECORDS_INVALID")
        try:
            if form == FORM_HAR:
                validate_shape(record.get("response"), _shape(route))
            elif record.get("media_type") == "json":
                validate_shape(record.get("response"), _shape(route))
            elif record.get("media_type") == "csv":
                columns, rows = record.get("columns"), record.get("rows")
                if (
                    tuple(columns if isinstance(columns, list) else ())
                    not in route.csv_columns
                    or not isinstance(rows, list)
                    or any(len(row) != len(columns) for row in rows)
                ):
                    raise CaptureRefusal("RECORDS_INVALID")
        except ShapeError:
            raise CaptureRefusal("RECORDS_INVALID") from None
        seen.add(capability)
    if seen != set(scope):
        raise CaptureRefusal("RECORDS_INVALID")


def _manifest_strategy_hashes(
    manifest: Mapping[str, Any], scope: Sequence[object]
) -> tuple[str, ...]:
    """The declared strategy hashes of an HTML capability, from its own summary."""

    found: set[str] = set()
    capabilities = manifest["capabilities"]
    for item in scope:
        if route_for(str(item)).html is None:
            continue
        info = capabilities.get(str(item)) if isinstance(capabilities, dict) else None
        hashes = info.get("strategy_sha256s") if isinstance(info, dict) else None
        if (
            not isinstance(hashes, list)
            or not hashes
            or not all(
                isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes
            )
        ):
            raise CaptureRefusal("MANIFEST_INVALID")
        found.update(str(h) for h in hashes)
    return tuple(sorted(found))


def verify_run(
    staging_root: Path,
    run_id: str,
    *,
    expect_manifest_sha256: str | None = None,
    require_complete: bool = True,
) -> RunSummary:
    """Re-check a run from bytes alone; never trusts the stager's own result."""

    _check_staging_root(staging_root)
    run_dir = _run_directory(staging_root, run_id)
    if not run_dir.is_dir() or _is_link(run_dir):
        raise CaptureRefusal("RUN_MISSING")
    manifest_bytes = _read_regular_file(run_dir / "manifest.json", 16 * 1024 * 1024)
    manifest_sha = _sha256(manifest_bytes)
    if expect_manifest_sha256 is not None and expect_manifest_sha256 != manifest_sha:
        raise CaptureRefusal("MANIFEST_HASH_MISMATCH")
    try:
        manifest = strict_json_object(manifest_bytes, label="manifest")
    except StrictJSONError:
        raise CaptureRefusal("MANIFEST_INVALID") from None
    if (
        set(manifest) != _MANIFEST_KEYS
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest["kind"] != KIND
        or manifest["run_id"] != run_id
        or manifest["restricted"] is not True
        or manifest["raw_contains_session_material"]
        is not (manifest["form"] == FORM_HAR)
        or not isinstance(manifest["allow_partial"], bool)
        or canonical_bytes(manifest) != manifest_bytes
    ):
        raise CaptureRefusal("MANIFEST_INVALID")
    if manifest["catalog_sha256"] != catalog_sha256():
        raise CaptureRefusal("CATALOG_DRIFT")
    files = manifest["files"]
    if not isinstance(files, list):
        raise CaptureRefusal("MANIFEST_INVALID")
    declared = set()
    contents: dict[str, bytes] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "role",
            "sha256",
            "bytes",
        }:
            raise CaptureRefusal("MANIFEST_INVALID")
        relative = str(item["path"])
        if (
            relative.startswith("/")
            or ".." in relative.split("/")
            or relative in declared
        ):
            raise CaptureRefusal("MANIFEST_INVALID")
        declared.add(relative)
        data = _read_regular_file(run_dir / relative, MAX_SOURCE_BYTES)
        if _sha256(data) != item["sha256"] or len(data) != item["bytes"]:
            raise CaptureRefusal("FILE_HASH_MISMATCH")
        contents[relative] = data
    present = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() or _is_link(path)
    }
    if present != declared | {"manifest.json"}:
        raise CaptureRefusal("UNDECLARED_FILES")
    source_info = manifest["source"]
    if not isinstance(source_info, dict):
        raise CaptureRefusal("MANIFEST_INVALID")
    raw_name = f"raw/source{source_info['suffix']}"
    source = contents.get(raw_name)
    if source is None or _sha256(source) != source_info["sha256"]:
        raise CaptureRefusal("SOURCE_HASH_MISMATCH")
    scope = manifest["scope"]
    if not isinstance(scope, list) or scope != sorted(scope):
        raise CaptureRefusal("MANIFEST_INVALID")
    if not all(isinstance(item, str) for item in scope) or any(
        _unknown(str(item)) for item in scope
    ):
        raise CaptureRefusal("MANIFEST_INVALID")
    if manifest["evidence_status"] != evidence_status(scope):
        raise CaptureRefusal("MANIFEST_INVALID")
    expires = manifest["session_expires_at"]
    _check_records(
        contents["normalized/records.json"],
        tuple(str(item) for item in scope),
        str(manifest["form"]),
    )
    derived = _derive(
        source,
        form=str(manifest["form"]),
        capabilities=tuple(str(item) for item in scope),
        captured_at=_parse_time(manifest["captured_at"]),
        expires_at=None if expires is None else _parse_time(expires),
        allow_partial=bool(manifest["allow_partial"]),
        suffix=str(source_info["suffix"]),
        strategy_hashes=_manifest_strategy_hashes(manifest, scope),
    )
    if (
        derived.normalized != contents.get("normalized/records.json")
        or derived.inventory != contents.get("inventory.json")
        or derived.capabilities != manifest["capabilities"]
        or derived.totals != manifest["inventory_totals"]
        or derived.complete is not manifest["complete"]
    ):
        raise CaptureRefusal("REDERIVATION_MISMATCH")
    if require_complete and not derived.complete:
        raise CaptureRefusal("COVERAGE_PARTIAL")
    return RunSummary(
        run_id=run_id,
        manifest_sha256=manifest_sha,
        source_sha256=str(source_info["sha256"]),
        records=sum(
            int(str(item["records"])) for item in derived.capabilities.values()
        ),
        complete=derived.complete,
        capabilities=derived.capabilities,
    )

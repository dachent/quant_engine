"""Governed, offline intake of an owner-exported Fidelity activity file.

No Fidelity API route is evidenced, and this module contains no network or
login code.  The owner signs in and exports on their own; this module only
(1) publishes the catalog of accepted report types, (2) writes a request
manifest that tells the owner what to export for which window, (3) stages the
received bytes create-only and hash-bound, and (4) normalizes them with the
existing static-history parser.  ``intake_verify`` re-checks the result without
importing this module or the parser.

Everything is written under a caller-supplied staging root, outside Git.  The
real export shape is only OBSERVED for the 18-column Accounts History CSV used
by the static history; every other report type is catalogued as unaccepted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any

from quant_engine.core import SourceArtifact
from quant_engine.fidelity import generation as _generation


INTAKE_SCHEMA_VERSION = 1
ACCEPTED_REPORT_TYPE = "accounts-history-csv"
REQUEST_MANIFEST_NAME = "request-manifest.json"
RAW_MANIFEST_NAME = "raw-manifest.json"
NORMALIZED_CATALOG_NAME = "normalized/fidelity-intake-catalog.json"
NORMALIZED_MANIFEST_NAME = "normalized/normalized-manifest.json"
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_FILES = 64
GOVERNANCE_SCHEMA = "fidelity-export-intake-governance-v1"
TERMS_TEXT = (
    "Fidelity export intake requires one current, reviewed owner approval; the "
    "owner performs the export in their own session; no credential is supplied "
    "to automation; files are staged create-only and hash-bound."
)
TERMS_SHA256 = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
_REVIEW_REFERENCE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^fidreq-[0-9a-f]{24}$")
_ARTIFACT_ID = re.compile(r"^raw/accounts-history-[0-9a-f]{16}\.csv$")
EXPECTED_HEADER: tuple[str, ...] = _generation._HEADER
LEADING_BLANK_ROWS = 2
FOOTER_ROWS = 12


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    """The repository's canonical JSON encoding (same bytes as core's)."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


class IntakeError(ValueError):
    """The intake request, evidence, file, or staging area failed closed."""


@dataclass(frozen=True)
class ReportType:
    report_type_id: str
    title: str
    accepted: bool
    shape_status: str
    reason: str
    owner_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "owner_steps": list(self.owner_steps),
            "reason": self.reason,
            "report_type_id": self.report_type_id,
            "shape_status": self.shape_status,
            "title": self.title,
        }


_UNOBSERVED = (
    "export shape has never been observed in repository evidence; add it only "
    "after the owner supplies a redacted sample and a reviewed parser"
)
REPORT_CATALOG: tuple[ReportType, ...] = (
    ReportType(
        report_type_id=ACCEPTED_REPORT_TYPE,
        title="Accounts History activity CSV",
        accepted=True,
        shape_status="OBSERVED_STATIC_HISTORY_2022_2024",
        reason="18-column activity export already ingested by the static history",
        owner_steps=(
            "Sign in to Fidelity yourself; do not share credentials or sessions.",
            "Open the account activity history and choose the custom date range "
            "named in the request manifest (menu names UNVERIFIED and may have "
            "changed since the static history was exported).",
            "Download the history as CSV. If the site limits the range or rows "
            "per file, split the window into consecutive files with no repeated "
            "rows; cover every account named in the request.",
            "Do not edit, re-save, or open-and-save the files in a spreadsheet "
            "program; hand over the downloaded bytes exactly as received.",
        ),
    ),
    ReportType(
        report_type_id="positions-csv",
        title="Positions export",
        accepted=False,
        shape_status="UNOBSERVED",
        reason=_UNOBSERVED,
        owner_steps=(),
    ),
    ReportType(
        report_type_id="balances-csv",
        title="Balances export",
        accepted=False,
        shape_status="UNOBSERVED",
        reason=_UNOBSERVED,
        owner_steps=(),
    ),
    ReportType(
        report_type_id="statement-pdf",
        title="Account statement PDF",
        accepted=False,
        shape_status="UNOBSERVED",
        reason=_UNOBSERVED,
        owner_steps=(),
    ),
)


def catalog_rows() -> list[dict[str, object]]:
    return [item.to_dict() for item in REPORT_CATALOG]


def catalog_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(catalog_rows())).hexdigest()


def get_report_type(report_type_id: str) -> ReportType:
    for item in REPORT_CATALOG:
        if item.report_type_id == report_type_id:
            return item
    raise IntakeError("report type is not in the catalog")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _obj(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise IntakeError(f"{name} has an invalid field set")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntakeError(f"{name} must be nonempty text")
    return value


def _iso_date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise IntakeError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise IntakeError(f"{name} must be a canonical ISO date")
    return parsed


def _timestamp(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntakeError(f"{name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise IntakeError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise IntakeError(f"{name} is missing or linked")
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise IntakeError(f"{name} must be an object")
    return value, payload


def _write_once(path: Path, payload: bytes) -> None:
    """Create ``path`` exclusively; never overwrite or verify-and-skip."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise IntakeError("staging is create-only and the target exists") from exc


def validate_governance(
    evidence_path: Path,
    *,
    report_type_id: str,
    window_start: date,
    window_end: date,
    now: datetime,
) -> str:
    """Validate owner-supplied evidence for one request; return its digest.

    The evidence lives outside the repository and is never copied into a run
    artifact; only its canonical digest is bound into the request manifest.
    """

    value, _ = _read_json(evidence_path, "governance evidence")
    evidence = _obj(
        value,
        "governance evidence",
        {
            "approved_at",
            "expires_at",
            "catalog_sha256",
            "owner_export",
            "review",
            "schema",
            "scope",
            "terms_sha256",
        },
    )
    if evidence["schema"] != GOVERNANCE_SCHEMA:
        raise IntakeError("governance schema is unsupported")
    if evidence["terms_sha256"] != TERMS_SHA256:
        raise IntakeError("governance terms differ")
    if evidence["catalog_sha256"] != catalog_sha256():
        raise IntakeError("governance catalog digest differs")
    review = _obj(evidence["review"], "governance review", {"identity", "reference"})
    _text(review["identity"], "review identity")
    if (
        _REVIEW_REFERENCE.fullmatch(_text(review["reference"], "review reference"))
        is None
    ):
        raise IntakeError("governance review reference is not an issue comment")
    owner = _obj(
        evidence["owner_export"],
        "governance owner_export",
        {"automation_credentials", "performed_by"},
    )
    if owner != {"automation_credentials": "NONE", "performed_by": "owner"}:
        raise IntakeError("governance must state owner export and no credentials")
    scope = _obj(
        evidence["scope"],
        "governance scope",
        {"report_types", "window_end", "window_start"},
    )
    if scope["report_types"] != [report_type_id]:
        raise IntakeError("governance scope differs from the request")
    if (
        _iso_date(scope["window_start"], "scope window_start") > window_start
        or _iso_date(scope["window_end"], "scope window_end") < window_end
    ):
        raise IntakeError("requested window exceeds the governed window")
    approved = _timestamp(evidence["approved_at"], "approved_at")
    expires = _timestamp(evidence["expires_at"], "expires_at")
    if not approved <= now < expires:
        raise IntakeError("governance evidence is not current")
    return _sha(canonical_json_bytes(evidence))


def build_request_manifest(
    *,
    report_type_id: str,
    window_start: date,
    window_end: date,
    governance_sha256: str,
    max_files: int = MAX_FILES,
) -> dict[str, object]:
    report = get_report_type(report_type_id)
    if not report.accepted:
        raise IntakeError("report type is not accepted for intake")
    if window_start > window_end:
        raise IntakeError("request window is inverted")
    if not 1 <= max_files <= MAX_FILES:
        raise IntakeError("max_files is out of range")
    body: dict[str, object] = {
        "catalog_sha256": catalog_sha256(),
        "expected": {
            "currency": "USD",
            "date_format": "MM/DD/YYYY",
            "footer_rows": FOOTER_ROWS,
            "header": list(EXPECTED_HEADER),
            "leading_blank_rows": LEADING_BLANK_ROWS,
            "max_bytes_per_file": MAX_EXPORT_BYTES,
            "max_files": max_files,
        },
        "governance_sha256": governance_sha256,
        "intake_schema_version": INTAKE_SCHEMA_VERSION,
        "owner_steps": list(report.owner_steps),
        "report_type": report_type_id,
        "shape_status": report.shape_status,
        "status": "AWAITING_OWNER_EXPORT",
        "window_end": window_end.isoformat(),
        "window_start": window_start.isoformat(),
    }
    request_id = "fidreq-" + _sha(canonical_json_bytes(body))[:24]
    return {"request_id": request_id, **body}


def write_request(
    staging_root: Path,
    *,
    evidence_path: Path,
    report_type_id: str,
    window_start: date,
    window_end: date,
    now: datetime | None = None,
    max_files: int = MAX_FILES,
) -> dict[str, object]:
    """Gate on governance evidence, then create the request manifest once."""

    moment = now or datetime.now(timezone.utc)
    digest = validate_governance(
        evidence_path,
        report_type_id=report_type_id,
        window_start=window_start,
        window_end=window_end,
        now=moment,
    )
    manifest = build_request_manifest(
        report_type_id=report_type_id,
        window_start=window_start,
        window_end=window_end,
        governance_sha256=digest,
        max_files=max_files,
    )
    root = _staging_root(staging_root)
    request_dir = root / str(manifest["request_id"])
    _write_once(request_dir / REQUEST_MANIFEST_NAME, canonical_json_bytes(manifest))
    return manifest


def _staging_root(value: Path) -> Path:
    if not value.is_absolute() or value.is_symlink() or not value.is_dir():
        raise IntakeError("staging root must be a real absolute directory")
    return value


def _request_dir(staging_root: Path, request_id: str) -> Path:
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise IntakeError("request id is malformed")
    path = _staging_root(staging_root) / request_id
    if path.is_symlink() or not path.is_dir():
        raise IntakeError("request directory is missing or linked")
    return path


def _load_request(request_dir: Path) -> tuple[dict[str, Any], bytes]:
    manifest, payload = _read_json(
        request_dir / REQUEST_MANIFEST_NAME, "request manifest"
    )
    body = {key: item for key, item in manifest.items() if key != "request_id"}
    expected = "fidreq-" + _sha(canonical_json_bytes(body))[:24]
    if manifest.get("request_id") != expected or request_dir.name != expected:
        raise IntakeError("request manifest identity differs")
    if manifest.get("report_type") != ACCEPTED_REPORT_TYPE:
        raise IntakeError("request report type is not accepted")
    return manifest, payload


def inspect_export(payload: bytes) -> dict[str, int]:
    """Fail closed unless the bytes match the one observed export shape."""

    if not payload or len(payload) > MAX_EXPORT_BYTES:
        raise IntakeError("export size is outside the accepted bounds")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeError("export is not UTF-8") from exc

    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise IntakeError("export is not parseable CSV") from exc
    if len(rows) < LEADING_BLANK_ROWS + 1 + FOOTER_ROWS:
        raise IntakeError("export is too short for the accepted shape")
    if any(cell.strip() for row in rows[:LEADING_BLANK_ROWS] for cell in row):
        raise IntakeError("export leading blank rows differ")
    header = tuple(rows[LEADING_BLANK_ROWS])
    if header != EXPECTED_HEADER:
        missing = sorted(set(EXPECTED_HEADER) - set(header))
        unknown = sorted(set(header) - set(EXPECTED_HEADER))
        raise IntakeError(
            f"export header differs: missing={missing}, unknown={unknown}, "
            f"same_columns_reordered={not missing and not unknown}"
        )
    activity = 0
    for row in rows[LEADING_BLANK_ROWS + 1 :]:
        if not row or not _generation._DATE.fullmatch(row[0].strip()):
            break
        activity += 1
    if not _generation._footer_is_valid(rows[LEADING_BLANK_ROWS + 1 + activity :]):
        raise IntakeError("export footer differs")
    return {"activity_rows": activity}


def stage_raw(
    staging_root: Path, request_id: str, files: Sequence[Path]
) -> dict[str, object]:
    """Stage owner files exactly as received; create-only, one call per request."""

    request_dir = _request_dir(staging_root, request_id)
    request, request_bytes = _load_request(request_dir)
    expected = _obj(
        request["expected"],
        "request expected",
        {
            "currency",
            "date_format",
            "footer_rows",
            "header",
            "leading_blank_rows",
            "max_bytes_per_file",
            "max_files",
        },
    )
    if not files or len(files) > int(expected["max_files"]):
        raise IntakeError("file count is outside the request")
    if (request_dir / RAW_MANIFEST_NAME).exists() or (request_dir / "raw").exists():
        raise IntakeError("raw staging already exists for this request")
    received: dict[str, bytes] = {}
    for source in files:
        if source.is_symlink() or not source.is_file():
            raise IntakeError("export file is missing or linked")
        payload = source.read_bytes()
        inspect_export(payload)
        digest = _sha(payload)
        if digest in received:
            raise IntakeError("the same export file was supplied twice")
        received[digest] = payload
    artifacts: list[dict[str, object]] = []
    written: list[Path] = []
    try:
        for digest in sorted(received):
            artifact_id = f"raw/accounts-history-{digest[:16]}.csv"
            target = request_dir.joinpath(*artifact_id.split("/"))
            _write_once(target, received[digest])
            written.append(target)
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "sha256": digest,
                    "size_bytes": len(received[digest]),
                }
            )
        manifest: dict[str, object] = {
            "artifacts": artifacts,
            "intake_schema_version": INTAKE_SCHEMA_VERSION,
            "request_id": request_id,
            "request_manifest_sha256": _sha(request_bytes),
        }
        _write_once(request_dir / RAW_MANIFEST_NAME, canonical_json_bytes(manifest))
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {"artifact_count": len(artifacts), "request_id": request_id}


def load_crosswalk(path: Path) -> dict[str, str]:
    """Read the owner's private provider-account to internal-id map."""

    if path.is_symlink() or not path.is_file():
        raise IntakeError("crosswalk is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeError("crosswalk is not valid UTF-8 JSON") from exc
    try:
        return _generation._crosswalk(value)
    except _generation.FidelityGenerationError as exc:
        raise IntakeError(str(exc)) from exc


def _load_raw(
    request_dir: Path, request_bytes: bytes
) -> list[tuple[dict[str, Any], Path]]:
    raw, _ = _read_json(request_dir / RAW_MANIFEST_NAME, "raw manifest")
    raw = _obj(
        raw,
        "raw manifest",
        {"artifacts", "intake_schema_version", "request_id", "request_manifest_sha256"},
    )
    if raw["request_manifest_sha256"] != _sha(request_bytes):
        raise IntakeError("raw manifest is not bound to the request manifest")
    items = raw["artifacts"]
    if not isinstance(items, list) or not items:
        raise IntakeError("raw manifest has no artifacts")
    result: list[tuple[dict[str, Any], Path]] = []
    for item in items:
        record = _obj(item, "raw artifact", {"artifact_id", "sha256", "size_bytes"})
        artifact_id = _text(record["artifact_id"], "artifact_id")
        if _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise IntakeError("raw artifact id is malformed")
        path = request_dir.joinpath(*artifact_id.split("/"))
        if path.is_symlink() or not path.is_file():
            raise IntakeError("raw artifact is missing or linked")
        payload = path.read_bytes()
        if _sha(payload) != record["sha256"] or len(payload) != record["size_bytes"]:
            raise IntakeError("raw artifact differs from its manifest hash")
        result.append((record, path))
    return result


def normalize(
    staging_root: Path, request_id: str, crosswalk_path: Path
) -> dict[str, object]:
    """Normalize staged raw files with the static-history parser; create-only."""

    request_dir = _request_dir(staging_root, request_id)
    request, request_bytes = _load_request(request_dir)
    window_start = _iso_date(request["window_start"], "window_start")
    window_end = _iso_date(request["window_end"], "window_end")
    crosswalk = load_crosswalk(crosswalk_path)
    raws = _load_raw(request_dir, request_bytes)
    raw_manifest_bytes = (request_dir / RAW_MANIFEST_NAME).read_bytes()

    transactions: list[dict[str, object]] = []
    cash_flows: list[dict[str, object]] = []
    corporate_actions: list[dict[str, object]] = []
    securities: set[tuple[str, str]] = set()
    families: dict[str, int] = {}
    dates: list[date] = []
    artifacts: list[SourceArtifact] = []
    for record, path in raws:
        artifact = SourceArtifact(
            artifact_id=str(record["artifact_id"]),
            sha256=str(record["sha256"]),
            size_bytes=int(record["size_bytes"]),
            as_of_date=window_end,
            schema_name="fidelity-accounts-history-csv",
            schema_version="1",
        )
        artifacts.append(artifact)
        try:
            parsed = _generation._parse_file(artifact, path, crosswalk)
        except _generation.FidelityGenerationError as exc:
            raise IntakeError(f"normalization failed closed: {exc}") from exc
        transactions.extend(parsed[0])
        cash_flows.extend(parsed[1])
        corporate_actions.extend(parsed[2])
        securities.update(parsed[3])
        for family, count in parsed[4].items():
            families[family] = families.get(family, 0) + count
        dates.extend(parsed[5])
    if any(item < window_start or item > window_end for item in dates):
        raise IntakeError("activity falls outside the requested window")

    def sort_key(item: Mapping[str, object]) -> tuple[str, str, str]:
        return (
            str(item["event_date"]),
            str(item["account_id"]),
            str(item["record_id"]),
        )

    for collection in (transactions, cash_flows, corporate_actions):
        collection.sort(key=sort_key)
    accounts = sorted(
        {
            str(item["account_id"])
            for collection in (transactions, cash_flows, corporate_actions)
            for item in collection
        }
    )
    activity_count = len(transactions) + len(cash_flows) + len(corporate_actions)
    catalog: dict[str, object] = {
        "accounts": [
            {"account_id": item, "base_currency": "USD", "provider": "FIDELITY"}
            for item in accounts
        ],
        "activity_disposition": {
            "cash_flows": len(cash_flows),
            "corporate_actions": len(corporate_actions),
            "family_counts": dict(sorted(families.items())),
            "source_rows": activity_count,
            "transactions": len(transactions),
            "unclassified": 0,
        },
        "cash_flows": cash_flows,
        "catalog_schema_version": INTAKE_SCHEMA_VERSION,
        "corporate_actions": corporate_actions,
        "coverage": {
            "observed_end": max(dates).isoformat() if dates else None,
            "observed_start": min(dates).isoformat() if dates else None,
            "requested_end": window_end.isoformat(),
            "requested_start": window_start.isoformat(),
            "status": "INTAKE_STAGED_NOT_ADMITTED",
            "unverified": [
                "completeness of the owner export against the provider is not provable from the file",
                "no NAV, position, or FX observations",
                "all activity remains strategy-unmapped",
            ],
        },
        "request_id": request_id,
        "securities": [
            {"currency": "USD", "security_id": security_id, "symbol": symbol}
            for security_id, symbol in sorted(securities)
        ],
        "source_artifacts": [item.to_dict() for item in artifacts],
        "source_provenance": {
            "acquisition": "OWNER_EXPORTED_FIDELITY_ACTIVITY_FILE",
            "coverage_class": "OWNER_WINDOW_EXPORT",
            "source_file_count": len(artifacts),
        },
        "transactions": transactions,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    manifest: dict[str, object] = {
        "activity_count": activity_count,
        "catalog_sha256": _sha(catalog_bytes),
        "catalog_size_bytes": len(catalog_bytes),
        "intake_schema_version": INTAKE_SCHEMA_VERSION,
        "published": False,
        "raw_manifest_sha256": _sha(raw_manifest_bytes),
        "request_id": request_id,
    }
    _write_once(request_dir / NORMALIZED_CATALOG_NAME, catalog_bytes)
    _write_once(request_dir / NORMALIZED_MANIFEST_NAME, canonical_json_bytes(manifest))
    return {
        "account_count": len(accounts),
        "activity_count": activity_count,
        "catalog_sha256": manifest["catalog_sha256"],
        "published": False,
        "request_id": request_id,
    }

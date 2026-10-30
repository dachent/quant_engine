"""Independent verifier for a staged Fidelity export intake.

Deliberately imports neither ``intake`` nor ``generation``: it re-reads the
request manifest, the raw bytes, and the normalized catalog with its own small
parser and compares aggregates.  Failures report stable reason codes and counts
only, never row values, account numbers, or names.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from collections.abc import Mapping, Sequence
from typing import Any


_HEADER = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Type,"
    "Exchange Quantity,Exchange Currency,Currency,Price,Quantity,Exchange Rate,"
    "Commission,Fees,Accrued Interest,Amount,Settlement Date"
).split(",")
_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_REQUEST_ID = re.compile(r"^fidreq-[0-9a-f]{24}$")
_RAW_NAME = re.compile(r"^accounts-history-[0-9a-f]{16}\.csv$")
_FOOTER_SHAPE = (0, 1, 1, 1, 1, 1, 0, 1, 1, 1, 0, 1)
_EXPECTED_FILES = {
    "request-manifest.json",
    "raw-manifest.json",
    "raw",
    "normalized",
}


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    """The repository's canonical JSON encoding (same bytes as core's)."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


class IntakeVerificationError(ValueError):
    """A reason-coded verification failure."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(path: Path) -> tuple[Any, bytes] | None:
    if path.is_symlink() or not path.is_file():
        return None
    payload = path.read_bytes()
    try:
        return json.loads(payload.decode("utf-8")), payload
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _amount(text: str) -> Decimal | None:
    raw = text.strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _read_raw(payload: bytes) -> tuple[list[list[str]], list[list[str]]] | None:
    try:
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
    except (UnicodeDecodeError, csv.Error):
        return None
    if len(rows) < 15 or any(cell.strip() for row in rows[:2] for cell in row):
        return None
    if rows[2] != _HEADER:
        return None
    body = rows[3:]
    end = 0
    while end < len(body) and body[end] and _DATE.fullmatch(body[end][0].strip()):
        end += 1
    footer = body[end:]
    if tuple(len(row) for row in footer) != _FOOTER_SHAPE:
        return None
    return body[:end], footer


def verify_intake(staging_root: Path, request_id: str) -> dict[str, object]:
    """Verify request, raw, and normalized stages; return aggregates only."""

    reasons: list[str] = []
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise IntakeVerificationError(["request_id_malformed"])
    base = staging_root / request_id
    if staging_root.is_symlink() or base.is_symlink() or not base.is_dir():
        raise IntakeVerificationError(["request_dir_missing_or_linked"])
    extra = {item.name for item in base.iterdir()} - _EXPECTED_FILES
    if extra:
        reasons.append(f"unexpected_entries:{len(extra)}")

    loaded = _json(base / "request-manifest.json")
    if loaded is None or not isinstance(loaded[0], dict):
        raise IntakeVerificationError([*reasons, "request_manifest_unreadable"])
    request, request_bytes = loaded
    body = {key: item for key, item in request.items() if key != "request_id"}
    expected_id = "fidreq-" + _sha(canonical_json_bytes(body))[:24]
    if request.get("request_id") != expected_id or expected_id != request_id:
        reasons.append("request_identity_differs")
    if request.get("report_type") != "accounts-history-csv":
        reasons.append("request_report_type_not_accepted")
    expected = request.get("expected")
    if not isinstance(expected, dict) or expected.get("header") != _HEADER:
        reasons.append("request_header_differs")
    window_start = _iso(request.get("window_start"))
    window_end = _iso(request.get("window_end"))
    if window_start is None or window_end is None or window_start > window_end:
        raise IntakeVerificationError([*reasons, "request_window_invalid"])

    raw_loaded = _json(base / "raw-manifest.json")
    if raw_loaded is None or not isinstance(raw_loaded[0], dict):
        raise IntakeVerificationError([*reasons, "raw_manifest_unreadable"])
    raw_manifest, raw_manifest_bytes = raw_loaded
    if raw_manifest.get("request_manifest_sha256") != _sha(request_bytes):
        reasons.append("raw_manifest_not_bound_to_request")
    if raw_manifest.get("request_id") != request_id:
        reasons.append("raw_manifest_request_differs")
    listed = raw_manifest.get("artifacts")
    if not isinstance(listed, list) or not listed:
        raise IntakeVerificationError([*reasons, "raw_artifacts_missing"])
    on_disk = (
        {item.name for item in (base / "raw").iterdir()}
        if (base / "raw").is_dir()
        else set()
    )
    if on_disk != {
        str(item.get("artifact_id", "")).removeprefix("raw/")
        for item in listed
        if isinstance(item, dict)
    }:
        reasons.append("raw_directory_differs_from_manifest")

    row_count = 0
    amount_sum = Decimal(0)
    days: list[date] = []
    accounts: set[str] = set()
    private_values: set[str] = set()
    seen_rows: dict[tuple[str, ...], str] = {}
    for item in listed:
        if not isinstance(item, dict):
            reasons.append("raw_artifact_entry_malformed")
            continue
        name = str(item.get("artifact_id", "")).removeprefix("raw/")
        if _RAW_NAME.fullmatch(name) is None:
            reasons.append("raw_artifact_name_malformed")
            continue
        path = base / "raw" / name
        if path.is_symlink() or not path.is_file():
            reasons.append("raw_artifact_missing_or_linked")
            continue
        payload = path.read_bytes()
        digest = _sha(payload)
        if (
            digest != item.get("sha256")
            or len(payload) != item.get("size_bytes")
            or name != f"accounts-history-{digest[:16]}.csv"
        ):
            reasons.append("raw_hash_differs")
            continue
        parsed = _read_raw(payload)
        if parsed is None:
            reasons.append("raw_shape_differs")
            continue
        for row in parsed[0]:
            if len(row) != len(_HEADER):
                reasons.append("raw_row_width_differs")
                break
            day = _day(row[0])
            value = _amount(row[16])
            if day is None or value is None or row[9].strip() != "USD":
                reasons.append("raw_row_value_differs")
                break
            if not window_start <= day <= window_end:
                reasons.append("raw_row_outside_window")
                break
            key = tuple(row)
            if seen_rows.setdefault(key, digest) != digest:
                reasons.append("rows_repeat_across_files")
                break
            row_count += 1
            amount_sum += value
            days.append(day)
            accounts.add(row[2].strip())
            private_values.add(row[2].strip())
            if len(row[1].strip()) >= 4:
                private_values.add(row[1].strip())

    catalog_loaded = _json(base / "normalized" / "fidelity-intake-catalog.json")
    manifest_loaded = _json(base / "normalized" / "normalized-manifest.json")
    if catalog_loaded is None or manifest_loaded is None:
        raise IntakeVerificationError([*reasons, "normalized_stage_unreadable"])
    catalog, catalog_bytes = catalog_loaded
    manifest, _ = manifest_loaded
    if not isinstance(catalog, dict) or not isinstance(manifest, dict):
        raise IntakeVerificationError([*reasons, "normalized_stage_malformed"])
    if manifest.get("catalog_sha256") != _sha(catalog_bytes):
        reasons.append("catalog_hash_differs")
    if manifest.get("raw_manifest_sha256") != _sha(raw_manifest_bytes):
        reasons.append("catalog_not_bound_to_raw_manifest")
    if manifest.get("published") is not False:
        reasons.append("intake_claims_publication")
    if catalog.get("request_id") != request_id:
        reasons.append("catalog_request_differs")

    records: list[dict[str, Any]] = []
    for group_name in ("transactions", "cash_flows", "corporate_actions"):
        group = catalog.get(group_name)
        if not isinstance(group, list) or not all(isinstance(x, dict) for x in group):
            reasons.append("catalog_group_malformed")
            continue
        records.extend(group)
    ids = [str(item.get("record_id")) for item in records]
    if len(set(ids)) != len(ids):
        reasons.append("catalog_record_ids_repeat")
    if len(records) != row_count or manifest.get("activity_count") != row_count:
        reasons.append("activity_count_differs_from_raw")
    try:
        catalog_sum = sum((Decimal(x["amount"]["value"]) for x in records), Decimal(0))
    except (KeyError, TypeError, InvalidOperation):
        catalog_sum = None
        reasons.append("catalog_amount_malformed")
    if catalog_sum is not None and catalog_sum != amount_sum:
        reasons.append("amount_sum_differs_from_raw")
    event_days = sorted(str(x.get("event_date")) for x in records)
    if days and (
        event_days[0] != min(days).isoformat()
        or event_days[-1] != max(days).isoformat()
    ):
        reasons.append("date_range_differs_from_raw")
    if len({str(x.get("account_id")) for x in records}) != len(accounts):
        reasons.append("account_count_differs_from_raw")
    leaked = sum(
        1
        for value in private_values
        if value and json.dumps(value).encode() in catalog_bytes
    )
    if leaked:
        reasons.append(f"private_values_in_catalog:{leaked}")
    if reasons:
        raise IntakeVerificationError(reasons)
    return {
        "account_count": len(accounts),
        "activity_count": row_count,
        "raw_file_count": len(listed),
        "request_id": request_id,
        "status": "VERIFIED",
    }


def _iso(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None

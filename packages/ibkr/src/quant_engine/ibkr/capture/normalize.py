"""Normalize one staged Flex run into a restricted, masked summary document.

The normalized document carries no raw account id, no execution detail and no
cash amount: accounts are masked (SHA-256[:16]), and per statement only the
base-currency NAV, row counts and the hashed security keys with their
symbol / assetCategory evidence are kept.  The raw XML in the run directory
remains the evidence of record.  Parsers fail closed on unknown shapes.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from quant_engine.ibkr.flex import FlexValidationError, parse_flex_file

from ._util import (
    account_label,
    canonical_json_bytes,
    reject_declarations,
    sha256_hex,
)
from .catalog import KIND_ACTIVITY, KIND_SECURITY, get_query
from .plan import PlanError, RequestPlan, plan_from_document
from .securities import SecurityScanError, aggregate_rows, scan_securities
from .staging import MANIFEST_NAME, write_new_file

NORMALIZED_SCHEMA = "ibkr-capture-normalized-v1"
STATUS_COMPLETE = "COMPLETE"
STATUS_GAPS = "GAPS_PRESENT"
STATEMENT_FILE = "statement.xml"
STATUS_STAGED = "STAGED"


class NormalizationError(ValueError):
    """The staged run cannot be normalized without guessing."""


def weekdays(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def load_staged_run(run_dir: Path) -> tuple[dict[str, Any], bytes, RequestPlan, int]:
    """Read and hash-check a staged run; return manifest, manifest bytes, plan."""

    manifest_path = run_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise NormalizationError("the run has no capture manifest")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NormalizationError("the capture manifest is not UTF-8 JSON") from None
    if not isinstance(manifest, dict) or manifest.get("status") != STATUS_STAGED:
        raise NormalizationError("only a STAGED run can be normalized")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise NormalizationError("the capture manifest has no file list")
    listed: dict[str, dict[str, Any]] = {}
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise NormalizationError("the capture manifest file list is malformed")
        listed[row["name"]] = row
    present = {item.name for item in run_dir.iterdir()} - {MANIFEST_NAME}
    if present != set(listed):
        raise NormalizationError("the run directory differs from its manifest")
    for name, row in listed.items():
        data = (run_dir / name).read_bytes()
        if sha256_hex(data) != row.get("sha256") or len(data) != row.get("bytes"):
            raise NormalizationError("a staged file differs from its manifest hash")
    if STATEMENT_FILE not in listed:
        raise NormalizationError("the run has no staged statement")
    try:
        plan = plan_from_document(manifest["plan"])
    except (PlanError, KeyError):
        raise NormalizationError("the manifest plan is invalid") from None
    if plan.sha256 != manifest.get("plan_sha256"):
        raise NormalizationError("the manifest plan hash differs from its plan")
    index = manifest.get("request_index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise NormalizationError("the manifest request index is invalid")
    if not 0 <= index < len(plan.requests):
        raise NormalizationError("the manifest request index is out of range")
    if manifest.get("query") != plan.requests[index].query:
        raise NormalizationError("the manifest query differs from its plan")
    return manifest, manifest_bytes, plan, index


def _activity_statements(path: Path, artifact_id: str) -> list[dict[str, Any]]:
    try:
        document = parse_flex_file(path, artifact_id=artifact_id)
    except FlexValidationError as exc:
        raise NormalizationError(f"statement rejected: {exc}") from None
    rows = []
    for statement in document.statements:
        rows.append(
            {
                "account_label": account_label(statement.raw_account_id),
                "report_date": statement.report_date.isoformat(),
                "base_currency": statement.base_currency,
                "nav": str(statement.nav),
                "counts": {
                    "positions": len(statement.positions),
                    "trades": len(statement.trades),
                    "cash_transactions": len(statement.cash_transactions),
                    "transfers": len(statement.transfers),
                    "corporate_actions": len(statement.corporate_actions),
                    "conversion_rates": len(statement.rates),
                },
            }
        )
    return rows


def _security_statements(path: Path, required: tuple[str, ...]) -> list[dict[str, Any]]:
    reject_declarations(path.read_bytes())
    rows: list[dict[str, Any]] = []
    try:
        for _, element in ET.iterparse(path, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] != "FlexStatement":
                continue
            account = (element.attrib.get("accountId") or "").strip()
            start = (element.attrib.get("fromDate") or "").strip()
            end = (element.attrib.get("toDate") or "").strip()
            if not account or len(start) < 8 or len(end) < 8:
                raise NormalizationError("a FlexStatement lacks account or dates")
            present = {item.tag.rsplit("}", 1)[-1] for item in element.iter()}
            if not set(required) <= present:
                raise NormalizationError("security statement sections missing")
            try:
                start_day = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:8]}")
                end_day = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:8]}")
            except ValueError:
                raise NormalizationError("a FlexStatement date is invalid") from None
            rows.append(
                {
                    "account_label": account_label(account),
                    "from_date": start_day.isoformat(),
                    "to_date": end_day.isoformat(),
                }
            )
    except ET.ParseError:
        raise NormalizationError("Flex XML is malformed or truncated") from None
    if not rows:
        raise NormalizationError("Flex response contains no statements")
    return rows


def build_normalized(run_dir: Path) -> tuple[dict[str, Any], bytes]:
    manifest, manifest_bytes, plan, index = load_staged_run(run_dir)
    request = plan.requests[index]
    query = get_query(request.query)
    statement_path = run_dir / STATEMENT_FILE
    if query.kind == KIND_ACTIVITY:
        statements = _activity_statements(statement_path, str(manifest["run_id"]))
        day_key = "report_date"
        dates = [
            (row["account_label"], date.fromisoformat(row[day_key]))
            for row in statements
        ]
        spans = [(day, day) for _, day in dates]
    elif query.kind == KIND_SECURITY:
        statements = _security_statements(statement_path, query.sections)
        dates = [
            (row["account_label"], date.fromisoformat(row["from_date"]))
            for row in statements
        ]
        spans = [
            (date.fromisoformat(row["from_date"]), date.fromisoformat(row["to_date"]))
            for row in statements
        ]
    else:  # pragma: no cover - the catalog has no other kind
        raise NormalizationError("unknown query kind")
    for start, end in spans:
        if start > end:
            raise NormalizationError("a statement has reversed dates")
        if start < request.window_from or end > request.window_to:
            raise NormalizationError("a statement lies outside the requested window")
    labels = sorted({label for label, _ in dates})
    if len(dates) != len(set(dates)) and query.kind == KIND_ACTIVITY:
        raise NormalizationError("an account-day appears more than once")
    expected = set(plan.expected_account_labels)
    if expected and not set(labels) <= expected:
        raise NormalizationError("a statement carries an unexpected account scope")
    gaps: dict[str, list[str]] = {}
    if query.kind == KIND_ACTIVITY:
        wanted = [
            day
            for day in weekdays(request.window_from, request.window_to)
            if day not in set(plan.known_non_trading_days)
        ]
        for label in sorted(set(labels) | expected):
            have = {day for owner, day in dates if owner == label}
            missing = [day.isoformat() for day in wanted if day not in have]
            if missing:
                gaps[label] = missing
    try:
        securities = aggregate_rows(scan_securities(statement_path))
    except SecurityScanError as exc:
        raise NormalizationError(str(exc)) from None
    statements.sort(
        key=lambda row: (row["account_label"], json.dumps(row, sort_keys=True))
    )
    first = min(start for start, _ in spans)
    last = max(end for _, end in spans)
    document: dict[str, Any] = {
        "schema": NORMALIZED_SCHEMA,
        "kind": query.kind,
        "query": query.name,
        "run_id": manifest["run_id"],
        "plan_sha256": plan.sha256,
        "request_index": index,
        "window": {
            "from": request.window_from.isoformat(),
            "to": request.window_to.isoformat(),
        },
        "raw": {
            "statement_sha256": next(
                row["sha256"]
                for row in manifest["files"]
                if row["name"] == STATEMENT_FILE
            ),
            "manifest_sha256": sha256_hex(manifest_bytes),
        },
        "statements": statements,
        "coverage": {
            "status": STATUS_GAPS if gaps else STATUS_COMPLETE,
            "statement_count": len(statements),
            "account_labels": labels,
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "weekday_gaps": gaps,
        },
        "securities": securities,
    }
    return document, canonical_json_bytes(document)


def normalize_run(run_dir: Path, output_path: Path) -> dict[str, Any]:
    """Normalize a staged run and create ``output_path`` (never overwritten)."""

    document, data = build_normalized(run_dir)
    write_new_file(output_path, data)
    return document

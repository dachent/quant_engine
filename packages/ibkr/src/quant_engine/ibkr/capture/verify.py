"""Independent verifier for a staged Flex run and its normalized document.

This module deliberately shares no parsing or normalization code with
``normalize``: it re-reads the manifest, re-hashes every staged byte, scans the
raw XML with its own element walk, recomputes the whole normalized document and
requires byte equality with the document under test.  It reports stable reason
codes only (never a value, an account id or a path).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ._util import canonical_json_bytes
from .catalog import get_query
from .plan import PlanError, plan_from_document

_MANIFEST = "capture-manifest.json"
_STATEMENT = "statement.xml"
_ID_TAGS = frozenset(
    {
        "OpenPosition",
        "Trade",
        "CashTransaction",
        "CorporateAction",
        "Transfer",
        "SecurityInfo",
        "Lot",
    }
)
_COUNTED = {
    "CashTransaction": "cash_transactions",
    "Transfer": "transfers",
    "CorporateAction": "corporate_actions",
    "ConversionRate": "conversion_rates",
}


class VerificationError(ValueError):
    """Verification failed; ``reason`` is a stable, value-free code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerificationReport:
    status: str
    checks: tuple[str, ...]
    statement_count: int
    security_count: int
    coverage_status: str


def _fail(reason: str) -> VerificationError:
    return VerificationError(reason)


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _label(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _day(raw: str) -> date | None:
    text = raw.strip()
    digits = text.replace("-", "")[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def _scan_raw(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    data = path.read_bytes()
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise _fail("raw-declaration")
    statements: list[dict[str, Any]] = []
    securities: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    seen_root = False
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            name = _tag(element)
            if event == "start":
                if not seen_root:
                    seen_root = True
                    if name != "FlexQueryResponse" or element.get("type") != "AF":
                        raise _fail("raw-root")
                if name == "FlexStatement":
                    current = {
                        "account": (element.get("accountId") or "").strip(),
                        "from": (element.get("fromDate") or "").strip(),
                        "to": (element.get("toDate") or "").strip(),
                        "positions": 0,
                        "trades": 0,
                        "cash_transactions": 0,
                        "transfers": 0,
                        "corporate_actions": 0,
                        "conversion_rates": 0,
                        "nav": None,
                        "nav_date": None,
                        "currency": None,
                    }
                continue
            if current is not None:
                if name == "OpenPosition" and element.get("levelOfDetail") == "SUMMARY":
                    current["positions"] += 1
                elif name == "Trade" and element.get("levelOfDetail") == "EXECUTION":
                    current["trades"] += 1
                elif name in _COUNTED:
                    current[_COUNTED[name]] += 1
                elif name == "EquitySummaryByReportDateInBase":
                    current["nav"] = element.get("total")
                    current["nav_date"] = element.get("reportDate")
                elif name == "AccountInformation":
                    current["currency"] = (element.get("currency") or "").strip()
            if name in _ID_TAGS:
                conid = (element.get("conid") or "").strip()
                if conid:
                    if not (conid.isascii() and conid.isdigit() and len(conid) <= 18):
                        raise _fail("raw-conid")
                    key = hashlib.sha256(conid.encode()).hexdigest()[:24]
                    entry = securities.setdefault(
                        key,
                        {
                            "symbols": {},
                            "cats": set(),
                            "rows": 0,
                            "miss": 0,
                            "days": [],
                        },
                    )
                    symbol = (element.get("symbol") or "").strip()
                    category = (element.get("assetCategory") or "").strip()
                    day = None
                    for attribute in ("reportDate", "tradeDate", "dateTime", "date"):
                        day = _day(element.get(attribute) or "")
                        if day is not None:
                            break
                    entry["rows"] += 1
                    if category:
                        entry["cats"].add(category)
                    if symbol:
                        entry["symbols"].setdefault(symbol, []).append(day)
                    else:
                        entry["miss"] += 1
                    if day is not None:
                        entry["days"].append(day)
            if name == "FlexStatement" and current is not None:
                statements.append(current)
                current = None
            element.clear()
    except ET.ParseError:
        raise _fail("raw-malformed") from None
    if not statements:
        raise _fail("raw-empty")
    return statements, securities


def _security_rows(securities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(securities):
        entry = securities[key]
        spans = []
        for symbol in sorted(entry["symbols"]):
            days = [item for item in entry["symbols"][symbol] if item is not None]
            spans.append(
                {
                    "symbol": symbol,
                    "first_seen": min(days).isoformat() if days else None,
                    "last_seen": max(days).isoformat() if days else None,
                }
            )
        days = entry["days"]
        rows.append(
            {
                "key": key,
                "symbols": spans,
                "asset_categories": sorted(entry["cats"]),
                "rows": entry["rows"],
                "rows_missing_symbol": entry["miss"],
                "first_seen": min(days).isoformat() if days else None,
                "last_seen": max(days).isoformat() if days else None,
            }
        )
    return rows


def verify_run(run_dir: Path, normalized_path: Path) -> VerificationReport:
    checks: list[str] = []
    manifest_bytes = (
        (run_dir / _MANIFEST).read_bytes() if (run_dir / _MANIFEST).is_file() else b""
    )
    if not manifest_bytes:
        raise _fail("manifest-missing")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail("manifest-json") from None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "ibkr-capture-manifest-v1"
    ):
        raise _fail("manifest-schema")
    if manifest.get("status") != "STAGED":
        raise _fail("manifest-not-staged")
    checks.append("manifest")

    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise _fail("manifest-files")
    names = set()
    for row in listed:
        if not isinstance(row, dict):
            raise _fail("manifest-files")
        name = row.get("name")
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise _fail("manifest-files")
        target = run_dir / name
        if not target.is_file():
            raise _fail("file-missing")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != row.get("sha256") or len(
            data
        ) != row.get("bytes"):
            raise _fail("file-hash")
        names.add(name)
    if {item.name for item in run_dir.iterdir()} != names | {_MANIFEST}:
        raise _fail("file-set")
    if _STATEMENT not in names:
        raise _fail("statement-missing")
    checks.append("file-hashes")

    try:
        plan = plan_from_document(manifest["plan"])
    except (PlanError, KeyError):
        raise _fail("plan-invalid") from None
    if plan.sha256 != manifest.get("plan_sha256"):
        raise _fail("plan-hash")
    index = manifest.get("request_index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(plan.requests)
    ):
        raise _fail("request-index")
    request = plan.requests[index]
    if manifest.get("query") != request.query:
        raise _fail("query-mismatch")
    query = get_query(request.query)
    checks.append("plan")

    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or len(attempts) < 2 or len(attempts) > 10:
        raise _fail("attempt-log")
    sends = [
        item
        for item in attempts
        if isinstance(item, dict) and item.get("step") == "send"
    ]
    gets = [
        item
        for item in attempts
        if isinstance(item, dict) and item.get("step") == "get"
    ]
    if len(sends) != 1 or sends[0].get("kind") != "SEND_ACCEPTED" or not gets:
        raise _fail("attempt-log")
    if gets[-1].get("kind") != "REPORT" or any(
        g.get("kind") != "POLL_AGAIN" for g in gets[:-1]
    ):
        raise _fail("attempt-log")
    checks.append("attempts")

    statements, securities = _scan_raw(run_dir / _STATEMENT)
    rows: list[dict[str, Any]] = []
    spans: list[tuple[date, date]] = []
    for item in statements:
        start = _day(item["from"])
        end = _day(item["to"])
        if not item["account"] or start is None or end is None or start > end:
            raise _fail("statement-dates")
        spans.append((start, end))
        if query.kind == "activity-statement":
            if (
                start != end
                or _day(item["nav_date"] or "") != end
                or item["nav"] is None
            ):
                raise _fail("statement-shape")
            try:
                nav = str(Decimal(item["nav"]))
            except InvalidOperation:
                raise _fail("statement-nav") from None
            rows.append(
                {
                    "account_label": _label(item["account"]),
                    "report_date": end.isoformat(),
                    "base_currency": item["currency"],
                    "nav": nav,
                    "counts": {
                        "positions": item["positions"],
                        "trades": item["trades"],
                        "cash_transactions": item["cash_transactions"],
                        "transfers": item["transfers"],
                        "corporate_actions": item["corporate_actions"],
                        "conversion_rates": item["conversion_rates"],
                    },
                }
            )
        else:
            rows.append(
                {
                    "account_label": _label(item["account"]),
                    "from_date": start.isoformat(),
                    "to_date": end.isoformat(),
                }
            )
    for start, end in spans:
        if start < request.window_from or end > request.window_to:
            raise _fail("window")
    owners = [row["account_label"] for row in rows]
    keyed = [(row["account_label"], row.get("report_date")) for row in rows]
    if query.kind == "activity-statement" and len(set(keyed)) != len(keyed):
        raise _fail("duplicate-account-day")
    labels = sorted(set(owners))
    expected = set(plan.expected_account_labels)
    if expected and not set(labels) <= expected:
        raise _fail("account-scope")
    gaps: dict[str, list[str]] = {}
    if query.kind == "activity-statement":
        skip = set(plan.known_non_trading_days)
        wanted = []
        cursor = request.window_from
        while cursor <= request.window_to:
            if cursor.weekday() < 5 and cursor not in skip:
                wanted.append(cursor)
            cursor += timedelta(days=1)
        for label in sorted(set(labels) | expected):
            have = {row["report_date"] for row in rows if row["account_label"] == label}
            missing = [d.isoformat() for d in wanted if d.isoformat() not in have]
            if missing:
                gaps[label] = missing
    rows.sort(key=lambda row: (row["account_label"], json.dumps(row, sort_keys=True)))
    expected_document = {
        "schema": "ibkr-capture-normalized-v1",
        "kind": query.kind,
        "query": query.name,
        "run_id": manifest.get("run_id"),
        "plan_sha256": plan.sha256,
        "request_index": index,
        "window": {
            "from": request.window_from.isoformat(),
            "to": request.window_to.isoformat(),
        },
        "raw": {
            "statement_sha256": hashlib.sha256(
                (run_dir / _STATEMENT).read_bytes()
            ).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "statements": rows,
        "coverage": {
            "status": "GAPS_PRESENT" if gaps else "COMPLETE",
            "statement_count": len(rows),
            "account_labels": labels,
            "first_date": min(s for s, _ in spans).isoformat(),
            "last_date": max(e for _, e in spans).isoformat(),
            "weekday_gaps": gaps,
        },
        "securities": _security_rows(securities),
    }
    if not normalized_path.is_file():
        raise _fail("normalized-missing")
    if normalized_path.read_bytes() != canonical_json_bytes(expected_document):
        raise _fail("normalized-differs")
    checks.append("recomputed")
    coverage = expected_document["coverage"]
    assert isinstance(coverage, dict)
    return VerificationReport(
        status="VERIFIED",
        checks=tuple(checks),
        statement_count=len(rows),
        security_count=len(securities),
        coverage_status=str(coverage["status"]),
    )

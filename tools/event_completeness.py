"""Event-completeness declarations and the ledger closure that decides them.

A declaration names an account, a fact (``flows``) and the interval over which the owner
states the provider's events are complete.  A declaration is never evidence: the ledger
evaluates the closure it names against the ledger's own rows and reports
``DECLARED_COMPLETE`` only when every check passes.  Any failed check is reported with its
counts as ``CLOSURE_FAILED``; the declaration alone never completes a fact.

Closure ``IBKR_CHANGE_IN_NAV_FLOW_TIE_OUT`` (flows): over the declared interval

* the account has a statement change-in-NAV row on every NAV day (no missing day),
* each day's ledger flows (by NAV recognition date) equal the statement's
  deposits/withdrawals plus asset transfers, exactly,
* no flow amount is unavailable and every flow type is in the closed set,
* each internal-transfer leg has a counterpart leg of the same size within one day.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Final

from leverage_disclosure import market_holidays

SCHEMA_VERSION: Final = 1
DECLARED_COMPLETE: Final = "DECLARED_COMPLETE"
CLOSURE_FAILED: Final = "CLOSURE_FAILED"
CLOSURE_IBKR_FLOWS: Final = "IBKR_CHANGE_IN_NAV_FLOW_TIE_OUT"
_CLOSURES: Final = {CLOSURE_IBKR_FLOWS: ("ibkr", "flows")}
_FLOW_TYPES: Final = frozenset(
    {
        "DEPOSIT_OR_WITHDRAWAL",
        "TRANSFER:INTERNAL:IN",
        "TRANSFER:INTERNAL:OUT",
    }
)
_STATEMENT_FLOW_KEYS: Final = ("depositsWithdrawals", "assetTransfers")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY_KEYS: Final = {
    "account_id",
    "fact_type",
    "declared_start",
    "declared_end",
    "closure",
    "evidence_refs",
    "ruling",
}


class EventCompletenessError(ValueError):
    """A declaration or the closure input is malformed."""


@dataclass(frozen=True)
class Declaration:
    account_id: str
    fact_type: str
    declared_start: date
    declared_end: date
    closure: str
    evidence_refs: tuple[tuple[str, str], ...]
    ruling: str


def _day(value: object, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise EventCompletenessError(f"{name} is not an ISO date") from exc


def load_declaration(raw: bytes) -> tuple[Declaration, ...]:
    """Strict loader: exact keys, one entry per (account, fact), ordered interval."""

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventCompletenessError("declaration is not JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "declarations"}
        or document["schema_version"] != SCHEMA_VERSION
        or not isinstance(document["declarations"], list)
    ):
        raise EventCompletenessError("declaration has an invalid field set")
    out: list[Declaration] = []
    seen: set[tuple[str, str]] = set()
    for entry in document["declarations"]:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise EventCompletenessError("declaration entry has an invalid field set")
        account = entry["account_id"]
        fact = entry["fact_type"]
        closure = entry["closure"]
        if not isinstance(account, str) or not account:
            raise EventCompletenessError("declaration account_id is invalid")
        if closure not in _CLOSURES or fact != _CLOSURES[closure][1]:
            raise EventCompletenessError("declaration closure does not fit its fact")
        if (account, fact) in seen:
            raise EventCompletenessError("declaration repeats an account and fact")
        seen.add((account, fact))
        start = _day(entry["declared_start"], "declared_start")
        end = _day(entry["declared_end"], "declared_end")
        if start > end:
            raise EventCompletenessError("declared interval is not ordered")
        refs = entry["evidence_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(item, dict)
                or set(item) != {"path", "sha256"}
                or not isinstance(item["path"], str)
                or not item["path"]
                or not isinstance(item["sha256"], str)
                or _SHA256.fullmatch(item["sha256"]) is None
                for item in refs
            )
        ):
            raise EventCompletenessError("declaration evidence refs are invalid")
        ruling = entry["ruling"]
        if not isinstance(ruling, str) or not ruling.strip():
            raise EventCompletenessError("declaration needs a ruling citation")
        out.append(
            Declaration(
                account,
                fact,
                start,
                end,
                closure,
                tuple((item["path"], item["sha256"]) for item in refs),
                ruling,
            )
        )
    return tuple(out)


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EventCompletenessError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise EventCompletenessError(f"{name} is not finite")
    return result


def _statement_flow_by_day(
    change_in_nav: Sequence[Mapping[str, Any]], account_id: str
) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for row in change_in_nav:
        if row.get("account_id") != account_id:
            continue
        day = _day(row.get("accounting_date"), "change_in_nav.accounting_date")
        if day in out:
            raise EventCompletenessError("change-in-NAV repeats a statement day")
        total = Decimal(0)
        for component in row.get("components", []):
            if component.get("source_component_key") in _STATEMENT_FLOW_KEYS:
                total += _decimal(component.get("value"), "change-in-NAV component")
        out[day] = total
    return out


def _unpaired_internal_legs(
    flows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Internal-transfer legs with no opposite leg of the same size within one day."""

    ins: dict[Decimal, list[Mapping[str, Any]]] = defaultdict(list)
    outs: dict[Decimal, list[Mapping[str, Any]]] = defaultdict(list)
    for leg in flows:
        kind = str(leg.get("flow_type", ""))
        if not kind.startswith("TRANSFER:INTERNAL:"):
            continue
        amount = abs(_decimal(leg["amount"]["value"], "flow amount"))
        (ins if kind.endswith(":IN") else outs)[amount].append(leg)

    def when(leg: Mapping[str, Any]) -> date:
        return _day(leg.get("event_date"), "flow event_date")

    unpaired: list[Mapping[str, Any]] = []
    for amount in sorted(set(ins) | set(outs)):
        waiting = sorted(outs.get(amount, []), key=when)
        for leg in sorted(ins.get(amount, []), key=when):
            match = next(
                (o for o in waiting if abs((when(o) - when(leg)).days) <= 1), None
            )
            if match is None:
                unpaired.append(leg)
            else:
                waiting.remove(match)
        unpaired.extend(waiting)
    return unpaired


def _evaluate_ibkr_flows(
    declaration: Declaration,
    flows: Sequence[Mapping[str, Any]],
    change_in_nav: Sequence[Mapping[str, Any]],
    nav_days: set[date],
    as_of_date: date,
) -> list[dict[str, Any]]:
    start, end = declaration.declared_start, declaration.declared_end
    account = declaration.account_id
    checks: list[dict[str, Any]] = []

    def record(name: str, failures: int, detail: dict[str, Any] | None = None) -> None:
        checks.append(
            {
                "check": name,
                "status": "PASS" if failures == 0 else "FAIL",
                "failures": failures,
                **(detail or {}),
            }
        )

    record("INTERVAL_REACHES_CUTOFF", 0 if end >= as_of_date else 1)
    interval_nav = {day for day in nav_days if start <= day <= end}
    holidays: set[date] = set()
    for year in range(start.year, end.year + 1):
        holidays |= set(market_holidays(year))
    expected = [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
        and (start + timedelta(days=offset)) not in holidays
    ]
    missing_calendar = [day for day in expected if day not in interval_nav]
    record(
        "NAV_ON_EVERY_MARKET_DAY",
        len(missing_calendar),
        {"missing_days": [day.isoformat() for day in missing_calendar[:5]]},
    )
    statement = _statement_flow_by_day(change_in_nav, account)
    missing_statement = sorted(day for day in interval_nav if day not in statement)
    record(
        "STATEMENT_ROW_ON_EVERY_NAV_DAY",
        len(missing_statement) + (0 if interval_nav else 1),
        {"nav_days": len(interval_nav)},
    )
    own = [f for f in flows if f.get("account_id") == account]
    in_interval = [
        f
        for f in own
        if start
        <= _day(f.get("nav_recognition_date") or f.get("event_date"), "flow date")
        <= end
    ]
    unavailable = [
        f for f in in_interval if f.get("amount", {}).get("status") != "AVAILABLE"
    ]
    unknown_type = [f for f in in_interval if f.get("flow_type") not in _FLOW_TYPES]
    record("FLOW_AMOUNTS_AVAILABLE", len(unavailable))
    record("FLOW_TYPES_CLASSIFIED", len(unknown_type))
    ledger_by_day: dict[date, Decimal] = defaultdict(Decimal)
    for f in in_interval:
        if f.get("amount", {}).get("status") == "AVAILABLE":
            ledger_by_day[
                _day(f.get("nav_recognition_date") or f.get("event_date"), "flow date")
            ] += _decimal(f["amount"]["value"], "flow amount")
    days = {day for day in statement if start <= day <= end} | set(ledger_by_day)
    breaks = [
        day
        for day in sorted(days)
        if statement.get(day, Decimal(0)) != ledger_by_day.get(day, Decimal(0))
    ]
    record(
        "STATEMENT_TOTALS_EQUAL_LEDGER_FLOWS",
        len(breaks),
        {
            "days_compared": len(days),
            "break_days": [day.isoformat() for day in breaks[:5]],
        },
    )
    unpaired = [
        f
        for f in _unpaired_internal_legs(flows)
        if f.get("account_id") == account
        and start <= _day(f.get("event_date"), "flow event_date") <= end
    ]
    record("INTERNAL_TRANSFERS_PAIRED", len(unpaired))
    return checks


def evaluate(
    declarations: Sequence[Declaration],
    *,
    flows: Sequence[Mapping[str, Any]],
    change_in_nav_by_provider: Mapping[str, Sequence[Mapping[str, Any]]],
    nav_days_by_account: Mapping[str, set[date]],
    account_provider: Mapping[str, str],
    as_of_date: date,
) -> list[dict[str, Any]]:
    """One row per declaration: ``DECLARED_COMPLETE`` only if every closure check passes."""

    rows: list[dict[str, Any]] = []
    for declaration in sorted(declarations, key=lambda d: (d.account_id, d.fact_type)):
        provider, fact = _CLOSURES[declaration.closure]
        if account_provider.get(declaration.account_id) != provider:
            checks = [
                {
                    "check": "ACCOUNT_BELONGS_TO_CLOSURE_PROVIDER",
                    "status": "FAIL",
                    "failures": 1,
                }
            ]
        else:
            checks = _evaluate_ibkr_flows(
                declaration,
                flows,
                change_in_nav_by_provider.get(provider, ()),
                nav_days_by_account.get(declaration.account_id, set()),
                as_of_date,
            )
        passed = all(item["status"] == "PASS" for item in checks)
        rows.append(
            {
                "account_id": declaration.account_id,
                "checks": checks,
                "closure": declaration.closure,
                "declared_end": declaration.declared_end.isoformat(),
                "declared_start": declaration.declared_start.isoformat(),
                "evidence_refs": [
                    {"path": path, "sha256": digest}
                    for path, digest in declaration.evidence_refs
                ],
                "fact_type": fact,
                "provider_id": provider,
                "ruling": declaration.ruling,
                "status": DECLARED_COMPLETE if passed else CLOSURE_FAILED,
            }
        )
    return rows

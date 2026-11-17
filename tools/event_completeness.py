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
from collections.abc import Collection, Mapping, Sequence
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
CLOSURE_SCHWAB_EMPTY: Final = "SCHWAB_EMPTY_POSITION_SET"
_CLOSURES: Final = {
    CLOSURE_IBKR_FLOWS: ("ibkr", "flows"),
    CLOSURE_SCHWAB_EMPTY: ("schwab", "positions"),
}
# Typed outcomes that are NOT completeness: the evidence files are cited by hash and the ledger
# verifies each file, but nothing is proved about the events themselves.
OUTCOME_CAPTURED_NOT_ADMITTED: Final = "CAPTURED_NOT_ADMITTED"
OUTCOME_EXCLUDED_BY_RULING: Final = "EXCLUDED_BY_RULING"
OUTCOMES: Final = frozenset({OUTCOME_CAPTURED_NOT_ADMITTED, OUTCOME_EXCLUDED_BY_RULING})
_FACT_TYPES: Final = frozenset({"flows", "nav", "positions", "transactions"})
_FLOW_TYPES: Final = frozenset(
    {
        "DEPOSIT_OR_WITHDRAWAL",
        "TRANSFER:INTERNAL:IN",
        "TRANSFER:INTERNAL:OUT",
    }
)
_STATEMENT_FLOW_KEYS: Final = (
    "depositsWithdrawals",
    "assetTransfers",
    "internalCashTransfers",
)
# A closed account is derived, never declared: its NAV settles to dust (at most this many dollars)
# and stays there with no position, transaction or flow after the settling day, for a stretch long
# enough that a missing observation cannot pass as a closure.
CLOSED_NAV_TOLERANCE: Final = Decimal("0.01")
CLOSED_MIN_ZERO_MARKET_DAYS: Final = 20
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY_KEYS: Final = {
    "account_id",
    "fact_type",
    "declared_start",
    "declared_end",
    "closure",
    "evidence_refs",
    "citation",
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
    citation: str


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
        if closure in OUTCOMES:
            if fact not in _FACT_TYPES:
                raise EventCompletenessError("declaration fact type is unknown")
        elif closure not in _CLOSURES or fact != _CLOSURES[closure][1]:
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
        citation = entry["citation"]
        if not isinstance(citation, str) or not citation.strip():
            raise EventCompletenessError(
                "declaration needs a citation naming its authority"
            )
        out.append(
            Declaration(
                account,
                fact,
                start,
                end,
                closure,
                tuple((item["path"], item["sha256"]) for item in refs),
                citation,
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


def _market_days(start: date, end: date) -> list[date]:
    holidays: set[date] = set()
    for year in range(start.year, end.year + 1):
        holidays |= set(market_holidays(year))
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
        and (start + timedelta(days=offset)) not in holidays
    ]


def closed_since(
    account_id: str,
    nav_values: Mapping[date, Decimal | None],
    *,
    positions: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    flows: Sequence[Mapping[str, Any]],
) -> tuple[date, date] | None:
    """(settled day, last observed day) when the account is derivably closed, else None.

    The trailing run of NAV observations is at most ``CLOSED_NAV_TOLERANCE`` in size; the run
    starts on the settled day Z and covers at least ``CLOSED_MIN_ZERO_MARKET_DAYS`` market
    days; and the ledger holds no position, transaction or flow of the account after Z.  An
    account whose NAV is merely missing has no trailing zero run and is never closed.
    """

    days = sorted(nav_values)
    if not days:
        return None
    run: list[date] = []
    for day in reversed(days):
        value = nav_values[day]
        if value is None or abs(value) > CLOSED_NAV_TOLERANCE:
            break
        run.append(day)
    if not run:
        return None
    run.reverse()
    settled, last = run[0], run[-1]
    market = _market_days(settled, last)
    if len(market) < CLOSED_MIN_ZERO_MARKET_DAYS:
        return None
    observed = set(run)
    if any(day not in observed for day in market):
        return None  # the zero run must be unbroken: every market day observed

    def after(rows: Sequence[Mapping[str, Any]], field: str) -> bool:
        return any(
            row.get("account_id") == account_id
            and row.get(field) is not None
            and _day(row[field], field) > settled
            for row in rows
        )

    if (
        after(positions, "as_of_date")
        or after(transactions, "event_date")
        or after(flows, "event_date")
    ):
        return None
    return settled, last


def _amount_text(amount: Any) -> str | None:
    """A canonical decimal text of an AVAILABLE amount (1.0, 1.00 and 1E0 compare equal)."""

    if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
        return None
    value = _decimal(amount.get("value"), "amount")
    return format(value.normalize(), "f") if value != 0 else "0"


def _evaluate_schwab_empty(
    declaration: Declaration,
    nav_values: Mapping[date, Decimal | None],
    positions: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    as_of_date: date,
    empty_position_accounts: Collection[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """The position set is empty from the settled day to the cutoff.

    Derived from the ledger rows alone: the account's last security activity (any transaction
    that names a security) is not after the day its NAV froze; the NAV is one unchanged
    observed value on every market day from that day to the last observation, for at least
    ``CLOSED_MIN_ZERO_MARKET_DAYS`` market days (a held position would move it); and the ledger
    holds no position row of the account on or after the last security activity.  The cited
    evidence files (for example a live balances capture) are hash-verified by the ledger but are
    not read here.
    """

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
    own_tx = [
        _day(t["event_date"], "transaction event_date")
        for t in transactions
        if t.get("account_id") == account
        and t.get("security_id") is not None
        and t.get("event_date") is not None
    ]
    last_security = max(own_tx) if own_tx else None
    days = sorted(nav_values)
    run: list[date] = []
    for day in reversed(days):
        value = nav_values[day]
        if value is None or (run and value != nav_values[run[-1]]):
            break
        run.append(day)
    run.reverse()
    settled = run[0] if run else None
    last_observed = run[-1] if run else None
    market = _market_days(settled, last_observed) if settled and last_observed else []
    unbroken = bool(run) and all(day in set(run) for day in market)
    record(
        "NAV_FROZEN_UNBROKEN",
        0 if run and unbroken and len(market) >= CLOSED_MIN_ZERO_MARKET_DAYS else 1,
        {
            "frozen_since": settled.isoformat() if settled else None,
            "market_days": len(market),
        },
    )
    record(
        "NAV_OBSERVED_TO_DECLARED_END",
        0 if last_observed is not None and last_observed >= end else 1,
    )
    record(
        "NO_SECURITY_ACTIVITY_AFTER_NAV_FROZE",
        0
        if settled is not None and (last_security is None or last_security <= settled)
        else 1,
        {
            "last_security_activity": last_security.isoformat()
            if last_security
            else None
        },
    )
    record(
        "INTERVAL_STARTS_AT_OR_AFTER_FREEZE",
        0 if settled is not None and start >= settled else 1,
    )
    floor = last_security or start
    stale_positions = [
        p
        for p in positions
        if p.get("account_id") == account
        and p.get("as_of_date") is not None
        and _day(p["as_of_date"], "position as_of_date") >= floor
    ]
    record("NO_POSITION_ROWS_SINCE_LAST_SECURITY_ACTIVITY", len(stale_positions))
    # absence of position rows proves nothing where the provider's position snapshot does not
    # cover the account (an account holding securities may be missing from it too): the provider
    # catalog must positively list the account as holding no position (its liquidation value
    # closed to the native cash balance with no holdings)
    record(
        "POSITION_SET_POSITIVELY_EMPTY_IN_PROVIDER_CATALOG",
        0 if account in set(empty_position_accounts) else 1,
    )
    return checks, []


def _evaluate_ibkr_flows(
    declaration: Declaration,
    flows: Sequence[Mapping[str, Any]],
    change_in_nav: Sequence[Mapping[str, Any]],
    nav_values: Mapping[date, Decimal | None],
    positions: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]],
    as_of_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
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
    nav_days = set(nav_values)
    interval_nav = {day for day in nav_days if start <= day <= end}
    closure = closed_since(
        account, nav_values, positions=positions, transactions=transactions, flows=flows
    )
    coverage_end = end
    if closure is not None and start <= closure[1] < end:
        # derived closure: market days after the last observation of a settled, inactive
        # account carry no NAV by design; the rule's evidence is on the row
        coverage_end = closure[1]
        record(
            "CLOSED_ACCOUNT_DERIVED",
            0,
            {
                "settled_on": closure[0].isoformat(),
                "last_observed": closure[1].isoformat(),
            },
        )
    expected = _market_days(start, coverage_end)
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
    # a flow dated before the declared start, where the statements already covered the account, is
    # a hole in the claim's boundary: the declaration must start at or before it (refused, named).
    # Without any statement day before the start the earlier flows lie outside statement
    # coverage: not refused, disclosed (flows_before_start) and left out of the claim.
    earlier = sorted(
        str(f.get("record_id"))
        for f in own
        if _day(f.get("nav_recognition_date") or f.get("event_date"), "flow date")
        < start
    )
    covered_before = sum(1 for day in statement if day < start)
    record(
        "NO_FLOWS_BEFORE_DECLARED_START_WITH_STATEMENT_COVERAGE",
        len(earlier) if covered_before else 0,
        {
            "flows_before_start": len(earlier),
            "statement_days_before_start": covered_before,
            "record_ids": earlier[:5],
        },
    )
    proven = sorted(str(f["record_id"]) for f in in_interval if f.get("record_id"))
    return checks, proven


def evaluate(
    declarations: Sequence[Declaration],
    *,
    flows: Sequence[Mapping[str, Any]],
    change_in_nav_by_provider: Mapping[str, Sequence[Mapping[str, Any]]],
    nav_values_by_account: Mapping[str, Mapping[date, Decimal | None]],
    account_provider: Mapping[str, str],
    as_of_date: date,
    positions: Sequence[Mapping[str, Any]] = (),
    transactions: Sequence[Mapping[str, Any]] = (),
    empty_position_accounts: Collection[str] = (),
) -> list[dict[str, Any]]:
    """One row per declaration: ``DECLARED_COMPLETE`` only if every closure check passes."""

    rows: list[dict[str, Any]] = []
    for declaration in sorted(declarations, key=lambda d: (d.account_id, d.fact_type)):
        if declaration.closure in OUTCOMES:
            rows.append(
                {
                    "account_id": declaration.account_id,
                    "checks": [],
                    "closure": declaration.closure,
                    "declared_end": declaration.declared_end.isoformat(),
                    "declared_start": declaration.declared_start.isoformat(),
                    "evidence_refs": [
                        {"path": path, "sha256": digest}
                        for path, digest in declaration.evidence_refs
                    ],
                    "fact_type": declaration.fact_type,
                    "provider_id": account_provider.get(declaration.account_id),
                    "citation": declaration.citation,
                    "status": declaration.closure,
                }
            )
            continue
        provider, fact = _CLOSURES[declaration.closure]
        proven_ids: list[str] = []
        if account_provider.get(declaration.account_id) != provider:
            checks = [
                {
                    "check": "ACCOUNT_BELONGS_TO_CLOSURE_PROVIDER",
                    "status": "FAIL",
                    "failures": 1,
                }
            ]
        elif declaration.closure == CLOSURE_SCHWAB_EMPTY:
            checks, proven_ids = _evaluate_schwab_empty(
                declaration,
                nav_values_by_account.get(declaration.account_id, {}),
                positions,
                transactions,
                as_of_date,
                empty_position_accounts,
            )
        else:
            checks, proven_ids = _evaluate_ibkr_flows(
                declaration,
                flows,
                change_in_nav_by_provider.get(provider, ()),
                nav_values_by_account.get(declaration.account_id, {}),
                positions,
                transactions,
                as_of_date,
            )
        passed = all(item["status"] == "PASS" for item in checks)
        by_id = {str(f.get("record_id")): f for f in flows}
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
                "citation": declaration.citation,
                "proven_record_ids": proven_ids,
                "proven_records": [
                    {
                        "record_id": rid,
                        "event_date": str(by_id[rid].get("event_date")),
                        "amount": _amount_text(by_id[rid].get("amount")),
                    }
                    for rid in proven_ids
                    if rid in by_id
                ],
                "status": DECLARED_COMPLETE if passed else CLOSURE_FAILED,
            }
        )
    return rows

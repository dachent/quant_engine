"""Independent derivation of the sleeve-basis figures of a declared strategy (#624, B).

A ``strategy-basis-declaration/1`` document names the strategies whose row is stated on
their derived sleeve (the sleeve epoch) instead of the parent account.  This module
re-derives every figure such a row carries from the two inputs a validator may trust and
nothing the producer published:

* the sleeve facts (``sleeve-account-facts``): the sleeve's daily NAV after allocated
  costs, the ``TRANSFER:INTERNAL:SLEEVE`` legs, the account's unallocated bucket;
* the governed external flows and the other strategies' current values, passed in by the
  caller (the baseline's own capital rows and governed events).

What is independent: this file imports nothing from ``tools/`` or ``packages/`` and
nothing from ``acceptance/derive_a`` or ``derive_b``; it reads no report or panel field
of the producer (``account_live_*``, the epoch row's ``twr``, a trace's ``factor`` or
cumulative fields) while deriving.  Published fields are read only afterwards, by
``compare_published``, to be compared with what was derived.

Convention (ADR 0002, unchanged): the epoch opens at its first closing NAV, so the entry
day is excluded; a date-only transfer is an external flow at the end of its day; the
return window runs from the opening NAV date to the end date, annualised ACT/365.

Every refusal carries a stable ``code`` so a tamper test (and an operator) can tell
*which* input failed.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
from typing import Any

PRECISION = 60
DECLARATION_SCHEMA = "strategy-basis-declaration/1"
BASIS_SLEEVE = "SLEEVE_EPOCH"
BASIS_PARENT = "PARENT_ACCOUNT_LIVE_SERIES"
FACTS_ARTIFACT = "sleeve-account-facts"
SLEEVE_LEG = "TRANSFER:INTERNAL:SLEEVE"
# the governed event classes that are external capital flows of the portfolio
FLOW_CLASSES = frozenset(
    {"OPENING_CONTRIBUTED_BASIS", "EXTERNAL_CONTRIBUTION", "EXTERNAL_WITHDRAWAL"}
)
CLOSURE_TOLERANCE = Decimal("1e-9")
LEG_TOLERANCE = Decimal("1e-6")
USD_TOLERANCE = Decimal("0.01")
RATE_TOLERANCE = Decimal("1e-15")

# refusal codes
DECLARATION_INVALID = "DECLARATION_INVALID"
FACTS_INVALID = "FACTS_INVALID"
STRATEGY_NO_SLEEVE = "STRATEGY_NO_SLEEVE"
STRATEGY_SLEEVE_RETIRED = "STRATEGY_SLEEVE_RETIRED"
STRATEGY_SLEEVE_AMBIGUOUS = "STRATEGY_SLEEVE_AMBIGUOUS"
NAV_CONTINUITY = "NAV_CONTINUITY"
NAV_ALLOCATION_CLOSURE = "NAV_ALLOCATION_CLOSURE"
NAV_ROLL_FORWARD = "NAV_ROLL_FORWARD"
NAV_NONPOSITIVE_BASE = "NAV_NONPOSITIVE_BASE"
LEG_PAIR_UNBALANCED = "LEG_PAIR_UNBALANCED"
LEG_PAIR_DATES_DIFFER = "LEG_PAIR_DATES_DIFFER"
LEG_DATE_NOT_NAV_DAY = "LEG_DATE_NOT_NAV_DAY"
LEG_ROW_MISMATCH = "LEG_ROW_MISMATCH"
BUCKET_CHAIN = "BUCKET_CHAIN"
PARENT_EQUITY_CLOSURE = "PARENT_EQUITY_CLOSURE"
WINDOW_EMPTY = "WINDOW_EMPTY"
MWR_NO_FLOWS = "MWR_NO_FLOWS"
MWR_NO_ROOT = "MWR_NO_ROOT"
MWR_ROOT_NOT_UNIQUE = "MWR_ROOT_NOT_UNIQUE"
BASELINE_NO_EPOCH = "BASELINE_NO_EPOCH"
BASELINE_ROW_MISSING = "BASELINE_ROW_MISSING"


class Refusal(ValueError):
    """An input is not usable or is internally inconsistent; ``code`` says which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Finding:
    """One disagreement between a derived value and a published or expected one."""

    code: str
    where: str
    derived: Any
    published: Any
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "where": self.where,
            "derived": _plain(self.derived),
            "published": _plain(self.published),
            "message": self.message,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


# ------------------------------------------------------------------- declaration


def parse_declaration(raw: bytes) -> dict[str, str]:
    """``{strategy_id: basis}`` from a ``strategy-basis-declaration/1`` document."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refusal(DECLARATION_INVALID, "not JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "strategies"}:
        raise Refusal(DECLARATION_INVALID, "keys differ from schema and strategies")
    if document["schema"] != DECLARATION_SCHEMA:
        raise Refusal(DECLARATION_INVALID, "schema differs")
    entries = document["strategies"]
    if not isinstance(entries, list) or not entries:
        raise Refusal(DECLARATION_INVALID, "names no strategy")
    declared: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"strategy_id", "basis"}:
            raise Refusal(DECLARATION_INVALID, "an entry's keys differ")
        strategy_id, basis = entry["strategy_id"], entry["basis"]
        if not isinstance(strategy_id, str) or not strategy_id:
            raise Refusal(DECLARATION_INVALID, "an entry has no strategy id")
        if basis != BASIS_SLEEVE:
            raise Refusal(DECLARATION_INVALID, f"basis is not restatable: {basis!r}")
        if strategy_id in declared:
            raise Refusal(DECLARATION_INVALID, f"{strategy_id} is declared twice")
        declared[strategy_id] = basis
    return declared


# ------------------------------------------------------------------------ helpers


def _dec(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise Refusal(FACTS_INVALID, f"{where} is not a number")
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError as exc:
        raise Refusal(FACTS_INVALID, f"{where} is not a number") from exc
    if not number.is_finite():
        raise Refusal(FACTS_INVALID, f"{where} is not finite")
    return number


def _day(value: Any, where: str) -> date:
    if not isinstance(value, str):
        raise Refusal(FACTS_INVALID, f"{where} is not a date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise Refusal(FACTS_INVALID, f"{where} is not a date") from exc


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Refusal(FACTS_INVALID, f"{where} is not an object")
    return value


def _sequence(value: Any, where: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise Refusal(FACTS_INVALID, f"{where} is not a list")
    return value


# ----------------------------------------------------------------- the derivation


@dataclass(frozen=True)
class SleeveFigures:
    """Everything a sleeve-basis row states, derived from the sleeve facts."""

    strategy_id: str
    account_id: str
    parent_account_id: str
    opening_date: date
    end_date: date
    window_days: int
    nav_observations: int
    opening_nav: Decimal
    value: Decimal
    twr: Decimal
    annualised: Decimal | None
    max_drawdown: Decimal  # a non-positive fraction
    current_drawdown: Decimal  # a non-positive fraction
    daily_returns: tuple[tuple[date, Decimal], ...]
    external_flows: tuple[tuple[date, Decimal], ...]  # (day, signed amount in)
    liability_line: Decimal  # the unallocated bucket NAV at the end (negative = margin)
    parent_equity: Decimal  # value of every live sleeve plus the bucket
    other_live_sleeves: tuple[tuple[str, Decimal], ...] = field(default=())


def select_sleeve(facts: Mapping[str, Any], strategy_id: str) -> Mapping[str, Any]:
    """The one sleeve of ``strategy_id`` that is live on the facts' last day."""

    window = _mapping(facts.get("window"), "window")
    last = _day(window.get("last_day"), "window.last_day")
    matching = [
        sleeve
        for sleeve in _sequence(facts.get("sleeves"), "sleeves")
        if _mapping(sleeve, "sleeve").get("strategy_id") == strategy_id
    ]
    if not matching:
        raise Refusal(
            STRATEGY_NO_SLEEVE,
            f"the declaration names {strategy_id!r} but the sleeve facts hold no "
            "sleeve of that strategy",
        )
    live = [
        sleeve
        for sleeve in matching
        if _day(sleeve.get("last_day"), "sleeve.last_day") == last
    ]
    if not live:
        raise Refusal(
            STRATEGY_SLEEVE_RETIRED,
            f"every sleeve of {strategy_id!r} ended before {last}: no sleeve epoch is "
            "open at the cutoff",
        )
    if len(live) > 1:
        raise Refusal(
            STRATEGY_SLEEVE_AMBIGUOUS,
            f"{len(live)} sleeves of {strategy_id!r} are live at {last}",
        )
    return live[0]


def _check_nav_rows(account: str, days: Sequence[Mapping[str, Any]]) -> None:
    """The facts' own closure: each NAV row rolls forward and carries its allocation."""

    cumulative = Decimal(0)
    previous: Mapping[str, Any] | None = None
    seen: date | None = None
    for row in days:
        day = _day(row.get("day"), f"{account} day")
        if seen is not None and day <= seen:
            raise Refusal(FACTS_INVALID, f"{account}: NAV days do not advance at {day}")
        seen = day
        nav_end = _dec(row.get("nav_end"), f"{account} {day} nav_end")
        after = _dec(row.get("nav_end_after_alloc"), f"{account} {day} after_alloc")
        cumulative += _dec(row.get("alloc_total"), f"{account} {day} alloc_total")
        if abs(after - (nav_end + cumulative)) > CLOSURE_TOLERANCE:
            raise Refusal(
                NAV_ALLOCATION_CLOSURE,
                f"{account} {day}: NAV after allocated costs {after} differs from "
                f"nav_end {nav_end} plus cumulative allocation {cumulative} by "
                f"{after - (nav_end + cumulative)}",
            )
        prior = _dec(row.get("nav_prev"), f"{account} {day} nav_prev")
        if previous is not None:
            before = _dec(previous.get("nav_end"), f"{account} nav_end")
            if abs(prior - before) > CLOSURE_TOLERANCE:
                raise Refusal(
                    NAV_CONTINUITY,
                    f"{account} {day}: opening NAV {prior} is not the previous "
                    f"closing NAV {before}",
                )
        rolled = (
            prior
            + _dec(row.get("own_pnl"), f"{account} {day} own_pnl")
            + _dec(row.get("flow_in"), f"{account} {day} flow_in")
            - _dec(row.get("flow_out"), f"{account} {day} flow_out")
        )
        if abs(nav_end - rolled) > CLOSURE_TOLERANCE:
            raise Refusal(
                NAV_ROLL_FORWARD,
                f"{account} {day}: nav_end {nav_end} differs from opening NAV plus "
                f"P&L plus transfers {rolled} by {nav_end - rolled}",
            )
        previous = row


def _sleeve_legs(
    facts: Mapping[str, Any],
) -> tuple[dict[str, list[tuple[date, Decimal]]], list[Mapping[str, Any]]]:
    """Every sleeve transfer leg by account, after proving each pair balances."""

    legs: dict[str, list[tuple[date, Decimal]]] = {}
    pairs: dict[str, list[Mapping[str, Any]]] = {}
    for leg in _sequence(facts.get("flows"), "flows"):
        entry = _mapping(leg, "flow")
        if entry.get("flow_type") != SLEEVE_LEG:
            continue
        pairs.setdefault(str(entry.get("pair_id")), []).append(entry)
    kept: list[Mapping[str, Any]] = []
    for pair_id in sorted(pairs):
        members = pairs[pair_id]
        inflow = Decimal(0)
        outflow = Decimal(0)
        days: set[date] = set()
        for entry in members:
            amount = _dec(entry.get("amount"), f"leg {pair_id} amount")
            days.add(_day(entry.get("nav_recognition_date"), f"leg {pair_id} date"))
            if entry.get("class") == "INTERNAL_IN":
                inflow += amount
            elif entry.get("class") == "INTERNAL_OUT":
                outflow += amount
            else:
                raise Refusal(FACTS_INVALID, f"leg {pair_id} has an unknown class")
        if abs(inflow - outflow) > LEG_TOLERANCE:
            raise Refusal(
                LEG_PAIR_UNBALANCED,
                f"transfer {pair_id}: legs in {inflow} differ from legs out "
                f"{outflow} (a leg is missing or altered)",
            )
        if len(days) != 1:
            raise Refusal(
                LEG_PAIR_DATES_DIFFER,
                f"transfer {pair_id}: its legs are recognised on "
                f"{sorted(day.isoformat() for day in days)}",
            )
        for entry in members:
            amount = _dec(entry.get("amount"), f"leg {pair_id} amount")
            signed = amount if entry.get("class") == "INTERNAL_IN" else -amount
            legs.setdefault(str(entry.get("account_id")), []).append(
                (_day(entry.get("nav_recognition_date"), "leg date"), signed)
            )
            kept.append(entry)
    return legs, kept


def _bucket_line(facts: Mapping[str, Any], last: date) -> Decimal:
    """The unallocated bucket's closing NAV, after proving its own roll-forward."""

    bucket = _mapping(facts.get("perimeter_account"), "perimeter_account")
    rows = _sequence(bucket.get("days"), "perimeter_account.days")
    if not rows:
        raise Refusal(FACTS_INVALID, "the unallocated bucket has no days")
    level = _dec(bucket.get("opening_nav"), "perimeter_account.opening_nav")
    day: date | None = None
    for row in rows:
        entry = _mapping(row, "perimeter day")
        day = _day(entry.get("day"), "perimeter day")
        level += (
            _dec(entry.get("internal_flow_net"), f"bucket {day} internal flow")
            + _dec(entry.get("external_flow"), f"bucket {day} external flow")
            + _dec(entry.get("residual_pnl"), f"bucket {day} residual")
        )
        published = _dec(entry.get("nav_end"), f"bucket {day} nav_end")
        if abs(published - level) > CLOSURE_TOLERANCE:
            raise Refusal(
                BUCKET_CHAIN,
                f"unallocated bucket {day}: nav_end {published} differs from the "
                f"rolled-forward {level} by {published - level}",
            )
        level = published
    if day != last:
        raise Refusal(
            BUCKET_CHAIN, f"the unallocated bucket ends {day}, not on the window end"
        )
    return level


def derive_sleeve(
    facts: Mapping[str, Any],
    strategy_id: str,
    *,
    parent_equity: Decimal | None = None,
) -> SleeveFigures:
    """Derive one declared strategy's sleeve figures from the sleeve facts alone."""

    with localcontext() as context:
        context.prec = PRECISION
        return _derive_sleeve(facts, strategy_id, parent_equity)


def _derive_sleeve(
    facts: Mapping[str, Any], strategy_id: str, parent_equity: Decimal | None
) -> SleeveFigures:
    if facts.get("artifact_id") != FACTS_ARTIFACT:
        raise Refusal(FACTS_INVALID, f"artifact_id is not {FACTS_ARTIFACT!r}")
    sleeve = select_sleeve(facts, strategy_id)
    account = str(sleeve.get("account_id"))
    parent = str(facts.get("parent_account_id"))
    rows = [
        _mapping(row, "sleeve day") for row in _sequence(sleeve.get("days"), "days")
    ]
    if len(rows) < 2:
        raise Refusal(WINDOW_EMPTY, f"{account}: fewer than two NAV observations")
    _check_nav_rows(account, rows)
    legs_by_account, _ = _sleeve_legs(facts)
    nav = {
        _day(row.get("day"), "day"): _dec(row.get("nav_end_after_alloc"), "nav")
        for row in rows
    }
    row_transfer = {
        _day(row.get("day"), "day"): _dec(row.get("flow_in"), "flow_in")
        - _dec(row.get("flow_out"), "flow_out")
        for row in rows
    }
    days = sorted(nav)
    opening, end = days[0], days[-1]
    flows: dict[date, Decimal] = {}
    for day, amount in legs_by_account.get(account, []):
        if day not in nav:
            raise Refusal(
                LEG_DATE_NOT_NAV_DAY,
                f"{account}: a transfer leg of {amount} is dated {day}, which has no "
                "NAV observation of the sleeve",
            )
        flows[day] = flows.get(day, Decimal(0)) + amount
    for day in days:
        carried = flows.get(day, Decimal(0))
        if abs(row_transfer[day] - carried) > LEG_TOLERANCE:
            raise Refusal(
                LEG_ROW_MISMATCH,
                f"{account} {day}: the NAV row carries transfers of "
                f"{row_transfer[day]} but the transfer legs sum to {carried}",
            )
    index = Decimal(1)
    peak = Decimal(1)
    drawdown = Decimal(0)
    worst = Decimal(0)
    returns: list[tuple[date, Decimal]] = []
    for before, day in zip(days, days[1:], strict=False):
        base = nav[before]
        if base <= 0:
            raise Refusal(
                NAV_NONPOSITIVE_BASE,
                f"{account}: NAV {base} on {before} is not positive",
            )
        # the transfer of ``day`` is an external flow at the end of that day
        daily = (nav[day] - flows.get(day, Decimal(0))) / base - 1
        returns.append((day, daily))
        index *= 1 + daily
        peak = max(peak, index)
        drawdown = index / peak - 1
        worst = min(worst, drawdown)
    span = (end - opening).days
    twr = index - 1
    annualised: Decimal | None = None
    if span > 0 and index > 0:
        annualised = index ** (Decimal(365) / Decimal(span)) - 1
    bucket = _bucket_line(facts, end)
    live = [
        (
            str(other.get("account_id")),
            _dec(other["days"][-1]["nav_end_after_alloc"], "nav"),
        )
        for other in _sequence(facts.get("sleeves"), "sleeves")
        if _day(_mapping(other, "sleeve").get("last_day"), "last_day") == end
    ]
    equity = sum((value for _, value in live), Decimal(0)) + bucket
    if parent_equity is not None and abs(equity - parent_equity) > USD_TOLERANCE:
        raise Refusal(
            PARENT_EQUITY_CLOSURE,
            f"live sleeves {equity - bucket} plus the unallocated bucket {bucket} "
            f"is {equity}, not the account equity {parent_equity}",
        )
    return SleeveFigures(
        strategy_id=strategy_id,
        account_id=account,
        parent_account_id=parent,
        opening_date=opening,
        end_date=end,
        window_days=span,
        nav_observations=len(nav),
        opening_nav=nav[opening],
        value=nav[end],
        twr=twr,
        annualised=annualised,
        max_drawdown=worst,
        current_drawdown=drawdown,
        daily_returns=tuple(returns),
        external_flows=tuple(
            (day, amount) for day, amount in sorted(flows.items()) if day > opening
        ),
        liability_line=bucket,
        parent_equity=equity,
        other_live_sleeves=tuple(
            (name, value) for name, value in live if name != account
        ),
    )


# ---------------------------------------------------------- perimeter and weight


def perimeter_and_weights(
    figures: Sequence[SleeveFigures], other_values: Mapping[str, Decimal]
) -> tuple[Decimal, dict[str, Decimal]]:
    """The systematic perimeter and every strategy's weight on the restated basis.

    ``other_values`` is every systematic strategy's current value on its own basis; a
    declared strategy's entry is replaced by its derived sleeve value.
    """

    with localcontext() as context:
        context.prec = PRECISION
        values = dict(other_values)
        for item in figures:
            values[item.strategy_id] = item.value
        perimeter = sum(values.values(), Decimal(0))
        if perimeter <= 0:
            raise Refusal(WINDOW_EMPTY, "the systematic perimeter is not positive")
        return perimeter, {key: value / perimeter for key, value in values.items()}


# ------------------------------------------------------------------ portfolio MWR


@dataclass(frozen=True)
class MwrFigures:
    rate: Decimal | None  # annualised ACT/365, None under a year
    window_equivalent: Decimal
    window_start: date
    window_end: date
    window_days: int
    flow_count: int
    terminal_value: Decimal


def _present_value(
    flows: Sequence[tuple[date, Decimal]], log_rate: Decimal, origin: date
) -> Decimal:
    return sum(
        (
            amount * (-log_rate * Decimal((day - origin).days) / Decimal(365)).exp()
            for day, amount in flows
        ),
        Decimal(0),
    )


def portfolio_mwr(
    events: Iterable[Mapping[str, Any]], terminal: Decimal, end: date
) -> MwrFigures:
    """Money-weighted return of the governed external flows to the perimeter value.

    The root is found on the continuously compounded rate ``x`` (``r = e^x - 1``): a
    grid scan counts sign changes of the present value, then the one bracket is
    bisected.  More than one root is a refusal, never a pick.
    """

    with localcontext() as context:
        context.prec = PRECISION
        net: dict[date, Decimal] = {}
        for event in events:
            kind = event.get("effective_classification") or event.get("classification")
            if kind not in FLOW_CLASSES:
                continue
            day = _day(event.get("date"), "governed event date")
            net[day] = net.get(day, Decimal(0)) - _dec(
                event.get("invested_capital_effect_usd"), "governed event amount"
            )
        flows = sorted((day, amount) for day, amount in net.items() if amount != 0)
        if not flows or end <= flows[0][0]:
            raise Refusal(MWR_NO_FLOWS, "no governed external flow before the end date")
        origin = flows[0][0]
        series = [*flows, (end, terminal)]
        grid = [Decimal(step) / Decimal(20) for step in range(-120, 121)]
        values = [_present_value(series, point, origin) for point in grid]
        brackets = [
            (grid[i], grid[i + 1])
            for i in range(len(grid) - 1)
            if values[i] == 0 or (values[i] < 0) != (values[i + 1] < 0)
        ]
        if not brackets:
            raise Refusal(MWR_NO_ROOT, "the governed flows have no money-weighted rate")
        if len(brackets) > 1:
            raise Refusal(
                MWR_ROOT_NOT_UNIQUE,
                f"{len(brackets)} rates solve the governed flows",
            )
        low, high = brackets[0]
        f_low = _present_value(series, low, origin)
        for _ in range(220):
            middle = (low + high) / 2
            f_middle = _present_value(series, middle, origin)
            if (f_middle < 0) == (f_low < 0):
                low, f_low = middle, f_middle
            else:
                high = middle
        log_rate = (low + high) / 2
        span = (end - origin).days
        equivalent = (log_rate * Decimal(span) / Decimal(365)).exp() - 1
        rate = log_rate.exp() - 1 if span >= 365 else None
        return MwrFigures(
            rate=rate,
            window_equivalent=equivalent,
            window_start=origin,
            window_end=end,
            window_days=span,
            flow_count=len(flows),
            terminal_value=terminal,
        )


# ------------------------------------------------------------ published comparison


def _close(left: Any, right: Decimal, tolerance: Decimal) -> bool:
    try:
        number = Decimal(str(left))
    except ArithmeticError:
        return False
    return number.is_finite() and abs(number - right) <= tolerance


def _row(rows: Any, strategy_id: str) -> Mapping[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, Mapping) and row.get("strategy_id") == strategy_id:
            return row
    return None


def compare_published(
    figures: SleeveFigures,
    perimeter: Decimal,
    weight: Decimal,
    panel: Mapping[str, Any],
    mwr: MwrFigures | None = None,
) -> list[Finding]:
    """Findings where the panel's row for a declared strategy is not the derived one."""

    found: list[Finding] = []
    row = _row(panel.get("strategies"), figures.strategy_id)
    where = f"$.strategies[{figures.strategy_id}]"
    if row is None:
        return [
            Finding(
                "PUBLISHED_ROW_MISSING",
                where,
                figures.strategy_id,
                None,
                "the panel has no row for the declared strategy",
            )
        ]
    scope = row.get("return_scope")
    basis = scope.get("basis") if isinstance(scope, Mapping) else None
    if basis != BASIS_SLEEVE:
        found.append(
            Finding(
                "PUBLISHED_BASIS_NOT_SLEEVE",
                f"{where}.return_scope.basis",
                BASIS_SLEEVE,
                basis,
                "the declaration states this row on the sleeve basis; it is "
                "published on another basis (parent rows swapped for sleeve rows)",
            )
        )
    drawdown_published = ("account_live_max_drawdown", "hwm_max_drawdown")
    checks: list[tuple[str, Decimal, Decimal]] = [
        ("account_live_cumulative_return", figures.twr, RATE_TOLERANCE),
        ("current_value_usd", figures.value, USD_TOLERANCE),
        ("cost_rate_denominator_usd", figures.value, USD_TOLERANCE),
        ("systematic_weight", weight, RATE_TOLERANCE),
        ("hwm_current_drawdown", figures.current_drawdown, RATE_TOLERANCE),
    ]
    if figures.annualised is not None:
        checks.append(("account_live_cagr", figures.annualised, RATE_TOLERANCE))
    checks.extend(
        (name, figures.max_drawdown, RATE_TOLERANCE) for name in drawdown_published
    )
    for name, derived, tolerance in checks:
        published = row.get(name)
        if published is None:
            if name in (
                "hwm_max_drawdown",
                "hwm_current_drawdown",
                "cost_rate_denominator_usd",
            ):
                continue
            found.append(
                Finding(
                    "PUBLISHED_FIELD_MISSING",
                    f"{where}.{name}",
                    derived,
                    None,
                    f"{name} is not published for the sleeve-basis row",
                )
            )
        elif not _close(published, derived, tolerance):
            found.append(
                Finding(
                    "PUBLISHED_FIGURE_DIFFERS",
                    f"{where}.{name}",
                    derived,
                    published,
                    f"{name} differs from the independent sleeve derivation",
                )
            )
    for name, derived_day in (
        ("account_live_observed_start", figures.opening_date),
        ("account_live_observed_end", figures.end_date),
    ):
        published_day = row.get(name)
        if (
            published_day is not None
            and str(published_day)[:10] != derived_day.isoformat()
        ):
            found.append(
                Finding(
                    "PUBLISHED_WINDOW_DIFFERS",
                    f"{where}.{name}",
                    derived_day,
                    published_day,
                    "the return window is not the sleeve's opening NAV date to the end",
                )
            )
    systematic = (
        (panel.get("capital") or {}).get("systematic")
        if isinstance(panel.get("capital"), Mapping)
        else None
    )
    if systematic is not None and not _close(systematic, perimeter, USD_TOLERANCE):
        found.append(
            Finding(
                "PUBLISHED_PERIMETER_DIFFERS",
                "$.capital.systematic",
                perimeter,
                systematic,
                "the systematic perimeter is not the sum of the restated values",
            )
        )
    if mwr is not None:
        block = panel.get("portfolio_mwr_annualised")
        if not isinstance(block, Mapping) or block.get("status") != "AVAILABLE":
            found.append(
                Finding(
                    "PUBLISHED_MWR_UNAVAILABLE",
                    "$.portfolio_mwr_annualised",
                    mwr.rate,
                    None if not isinstance(block, Mapping) else block.get("reason"),
                    "the portfolio MWR is derivable on the restated perimeter but is "
                    "not published",
                )
            )
        else:
            if mwr.rate is not None and not _close(
                block.get("value"), mwr.rate, RATE_TOLERANCE
            ):
                found.append(
                    Finding(
                        "PUBLISHED_MWR_DIFFERS",
                        "$.portfolio_mwr_annualised.value",
                        mwr.rate,
                        block.get("value"),
                        "the portfolio MWR differs from the independent derivation",
                    )
                )
            for name, derived_count in (
                ("window_days", mwr.window_days),
                ("flow_count", mwr.flow_count),
            ):
                if block.get(name) != derived_count:
                    found.append(
                        Finding(
                            "PUBLISHED_MWR_DIFFERS",
                            f"$.portfolio_mwr_annualised.{name}",
                            derived_count,
                            block.get(name),
                            f"portfolio MWR {name} differs",
                        )
                    )
    return found


# ------------------------------------------------------------- expected baseline

TWR_METRIC = "twr.strategy"
CAPITAL_METRIC = "capital.strategy.cutoff"
WEIGHT_METRIC = "weight.strategy.cutoff"
SYSTEMATIC_METRIC = "capital.portfolio.systematic.cutoff"
EPOCH_IDS_METRIC = "universe.derived_epoch_ids"
ACTIVE_IDS_METRIC = "universe.active_strategy_ids"
RESTATED_FAMILIES = (TWR_METRIC, CAPITAL_METRIC, WEIGHT_METRIC, SYSTEMATIC_METRIC)
_RATIO_QUANTUM = Decimal("1e-12")
_CENT = Decimal("0.01")

RowKey = tuple[str, str, str]


def _key(row: Mapping[str, Any]) -> RowKey:
    return (str(row["metric_id"]), str(row["entity"]), str(row.get("window") or ""))


def _rows(document: Mapping[str, Any]) -> dict[RowKey, Mapping[str, Any]]:
    return {_key(row): row for row in document.get("metrics", [])}


@dataclass(frozen=True)
class RowChange:
    metric_id: str
    entity: str
    window: str
    before: str
    after: str
    reason: str


def restated_rows(
    document: Mapping[str, Any], declared: Mapping[str, str]
) -> list[RowChange]:
    """The expected rows a baseline states differently once ``declared`` is in force.

    Computed from the baseline's own derived-epoch rows: the declared strategy's
    ``twr.strategy`` is its open sleeve epoch's ``twr.account``, its capital the epoch's
    closing NAV, and the systematic total and every weight follow from those.  A weight
    of any other strategy moves only because the perimeter does.
    """

    with localcontext() as context:
        context.prec = PRECISION
        rows = _rows(document)
        epoch_ids = rows.get((EPOCH_IDS_METRIC, "portfolio", ""))
        listed = epoch_ids.get("expected") if epoch_ids is not None else None
        if not isinstance(listed, list):
            raise Refusal(
                BASELINE_NO_EPOCH, "the baseline carries no derived epoch identities"
            )
        changes: list[RowChange] = []
        capital_before: dict[str, Decimal] = {}
        capital_after: dict[str, Decimal] = {}
        for key, row in rows.items():
            if key[0] == CAPITAL_METRIC:
                capital_before[key[1]] = _dec(row.get("expected"), "expected capital")
        capital_after.update(capital_before)
        for strategy_id in sorted(declared):
            open_epochs = [
                parts[0]
                for parts in (str(item).split("|") for item in listed)
                if len(parts) == 4 and parts[1] == strategy_id and parts[3] == "open"
            ]
            if len(open_epochs) != 1:
                raise Refusal(
                    BASELINE_NO_EPOCH,
                    f"{strategy_id!r}: {len(open_epochs)} derived epochs open at the "
                    "cutoff in the baseline",
                )
            account = open_epochs[0]
            twr = rows.get(("twr.account", account, "ITD"))
            closing = rows.get(("capital.epoch.closing", account, ""))
            before_twr = rows.get((TWR_METRIC, strategy_id, "ITD"))
            if twr is None or closing is None or before_twr is None:
                raise Refusal(
                    BASELINE_ROW_MISSING,
                    f"{strategy_id!r}: the epoch {account} lacks its twr or closing row",
                )
            changes.append(
                RowChange(
                    TWR_METRIC,
                    strategy_id,
                    "ITD",
                    str(before_twr.get("expected")),
                    str(twr.get("expected")),
                    "sleeve basis declared: the strategy return is its open sleeve "
                    "epoch's return",
                )
            )
            capital_after[strategy_id] = _dec(closing.get("expected"), "closing")
            changes.append(
                RowChange(
                    CAPITAL_METRIC,
                    strategy_id,
                    "",
                    str(capital_before.get(strategy_id)),
                    str(closing.get("expected")),
                    "sleeve basis declared: the strategy value is its sleeve NAV",
                )
            )
        old_total = rows.get((SYSTEMATIC_METRIC, "portfolio", ""))
        if old_total is None:
            raise Refusal(BASELINE_ROW_MISSING, "no systematic capital row")
        before_sum = _dec(old_total.get("expected"), "systematic")
        perimeter = before_sum + sum(
            (capital_after[s] - capital_before[s] for s in declared), Decimal(0)
        )
        changes.append(
            RowChange(
                SYSTEMATIC_METRIC,
                "portfolio",
                "",
                str(old_total.get("expected")),
                format(perimeter.quantize(_CENT), "f"),
                "a declared strategy is valued as its sleeve: the perimeter moves by "
                "the difference of the values",
            )
        )
        for key, row in rows.items():
            if key[0] != WEIGHT_METRIC:
                continue
            share = (capital_after[key[1]] / perimeter).quantize(_RATIO_QUANTUM)
            reason = (
                "sleeve basis declared: sleeve NAV over the restated perimeter"
                if key[1] in declared
                else "perimeter changed by a declared strategy's restatement"
            )
            changes.append(
                RowChange(
                    WEIGHT_METRIC,
                    key[1],
                    key[2],
                    str(row.get("expected")),
                    format(share, "f"),
                    reason,
                )
            )
        return changes


def _tolerance(row: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(row.get("tolerance")))
    except ArithmeticError:
        return USD_TOLERANCE if row.get("unit") == "USD" else RATE_TOLERANCE


_COMPARED = ("expected", "unit", "tolerance", "as_of", "window", "entity", "metric_id")


def diff_expected(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    declared: Mapping[str, str] | None,
) -> list[Finding]:
    """Findings where two expected baselines differ other than as the declaration says.

    With no declaration the two must carry identical rows.  With one, only the rows of
    ``restated_rows`` may differ, and each must equal its computed value; every other
    row and every not-derivable row must be identical.
    """

    found: list[Finding] = []
    old, new = _rows(before), _rows(after)
    allowed: dict[RowKey, RowChange] = {}
    if declared:
        allowed = {
            (c.metric_id, c.entity, c.window): c
            for c in restated_rows(before, declared)
        }
    for key in sorted(set(old) | set(new)):
        label = "|".join(key)
        if key not in new:
            found.append(
                Finding(
                    "ROW_REMOVED",
                    label,
                    old[key].get("expected"),
                    None,
                    "a row vanished",
                )
            )
            continue
        if key not in old:
            found.append(
                Finding(
                    "ROW_ADDED", label, None, new[key].get("expected"), "a row appeared"
                )
            )
            continue
        change = allowed.get(key)
        if change is None:
            for name in _COMPARED:
                if old[key].get(name) != new[key].get(name):
                    found.append(
                        Finding(
                            "ROW_CHANGED_WITHOUT_REASON",
                            f"{label}.{name}",
                            old[key].get(name),
                            new[key].get(name),
                            "no declaration moves this row",
                        )
                    )
            continue
        limit = _tolerance(new[key])
        if not _close(new[key].get("expected"), _dec(change.after, "computed"), limit):
            found.append(
                Finding(
                    "ROW_NOT_THE_RESTATED_VALUE",
                    label,
                    change.after,
                    new[key].get("expected"),
                    f"the declaration moves this row to {change.after} ({change.reason})",
                )
            )
        for name in ("unit", "window", "entity", "metric_id"):
            if old[key].get(name) != new[key].get(name):
                found.append(
                    Finding(
                        "ROW_SHAPE_CHANGED",
                        f"{label}.{name}",
                        old[key].get(name),
                        new[key].get(name),
                        "a restated row keeps its unit, window and identity",
                    )
                )
    unavailable_before = sorted(_key(r) for r in before.get("not_derivable", []))
    unavailable_after = sorted(_key(r) for r in after.get("not_derivable", []))
    if unavailable_before != unavailable_after:
        found.append(
            Finding(
                "NOT_DERIVABLE_CHANGED",
                "not_derivable",
                unavailable_before,
                unavailable_after,
                "the set of not-derivable rows differs",
            )
        )
    return found


def apply_restatement(
    document: Mapping[str, Any], declared: Mapping[str, str]
) -> dict[str, Any]:
    """A copy of ``document`` with only the restated rows' expected values moved."""

    result: dict[str, Any] = json.loads(json.dumps(document))
    changes = {
        (c.metric_id, c.entity, c.window): c for c in restated_rows(document, declared)
    }
    for row in result["metrics"]:
        change = changes.get(_key(row))
        if change is not None:
            row["expected"] = change.after
    return result


# --------------------------------------------------------------------- non-diminish

# the fields of a strategy row that state its account-basis figures: schema vocabulary,
# not data.  Nothing names a strategy; the strategy comes from the declaration.
FIGURE_FIELDS = (
    "account_live_cumulative_return",
    "ytd_twr",
    "calendar_year_twr",
    "account_live_cagr",
    "account_live_max_drawdown",
    "hwm_max_drawdown",
    "hwm_current_drawdown",
    "current_value_usd",
    "cost_rate_denominator_usd",
    "systematic_weight",
    "account_live_return_series_sha256",
    "account_live_observed_start",
    "account_live_observed_end",
    "account_live_return_observations",
)
EVIDENCE_FIELDS = (
    "cumulative_return",
    "current_drawdown",
    "max_drawdown",
    "maximum_drawdown",
    "return_series_sha256",
    "observed_start",
    "observed_end",
    "return_observations",
)
HOME_KEY = "account_basis"
LABEL_KEYS = ("basis", "basis_label", "label")


@dataclass(frozen=True)
class Figure:
    path: str
    value: Any


def _leaves(value: Any, path: str) -> Iterable[Figure]:
    if isinstance(value, Mapping):
        for key in value:
            yield from _leaves(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for position, item in enumerate(value):
            yield from _leaves(item, f"{path}[{position}]")
    elif value is not None and not isinstance(value, bool):
        yield Figure(path, value)


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str) and left == right:
        return True
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
    except ArithmeticError:
        return False
    return a.is_finite() and b.is_finite() and abs(a - b) <= Decimal("1e-12")


def account_basis_figures(panel: Mapping[str, Any], strategy_id: str) -> list[Figure]:
    """The account-basis figure set a strategy row states, by path and value."""

    row = _row(panel.get("strategies"), strategy_id)
    if row is None:
        return []
    base = f"$.strategies[{strategy_id}]"
    found: list[Figure] = []
    for name in FIGURE_FIELDS:
        if name in row:
            found.extend(_leaves(row[name], f"{base}.{name}"))
    evidence = row.get("provider_evidence")
    account = evidence.get("account") if isinstance(evidence, Mapping) else None
    if isinstance(account, Mapping):
        for name in EVIDENCE_FIELDS:
            if name in account:
                found.extend(
                    _leaves(account[name], f"{base}.provider_evidence.account.{name}")
                )
    return found


def _resolve(panel: Mapping[str, Any], path: str) -> Any:
    """The value at a ``$.strategies[id].a.b`` path (a missing step is ``None``)."""

    head = "$.strategies["
    if not path.startswith(head):
        return None
    close = path.index("]")
    strategy_id = path[len(head) : close]
    node: Any = _row(panel.get("strategies"), strategy_id)
    for step in path[close + 2 :].split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(step)
    return node


def _home(
    panel: Mapping[str, Any], parent_ids: Sequence[str]
) -> list[Mapping[str, Any]]:
    homes: list[Mapping[str, Any]] = []
    summary = panel.get("account_summary")
    if isinstance(summary, list):
        for row in summary:
            if isinstance(row, Mapping) and row.get("account_id") in parent_ids:
                block = row.get(HOME_KEY)
                if isinstance(block, Mapping):
                    homes.append(block)
    return homes


def check_non_diminish(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    declared: Mapping[str, str],
) -> list[Finding]:
    """No account-basis figure of a declared strategy may be absent after B.

    A figure present before is kept when the same path still states the same value, or
    when the declared new home -- ``account_summary[<parent account>].account_basis`` --
    states it.  The home must carry a basis label and the strategy row's
    ``return_scope`` must reference it.
    """

    found: list[Finding] = []
    for strategy_id in sorted(declared):
        figures = account_basis_figures(before, strategy_id)
        if not figures:
            found.append(
                Finding(
                    "NO_FIGURES_BEFORE",
                    f"$.strategies[{strategy_id}]",
                    None,
                    None,
                    "the before panel states no account-basis figure for the strategy",
                )
            )
            continue
        row = _row(before.get("strategies"), strategy_id) or {}
        scope = row.get("return_scope")
        parents = (
            [str(item) for item in scope.get("parent_account_ids", [])]
            if isinstance(scope, Mapping)
            else []
        )
        homes = _home(after, parents)
        home_values = [leaf for block in homes for leaf in _leaves(block, "")]
        for figure in figures:
            if _same(_resolve(after, figure.path), figure.value):
                continue
            if any(_same(leaf.value, figure.value) for leaf in home_values):
                continue
            found.append(
                Finding(
                    "ACCOUNT_BASIS_FIGURE_ABSENT",
                    figure.path,
                    figure.value,
                    _resolve(after, figure.path),
                    "the figure is neither at its path nor in "
                    f"account_summary[...].{HOME_KEY}",
                )
            )
        if homes:
            if not any(
                isinstance(block.get(key), str) and block.get(key)
                for block in homes
                for key in LABEL_KEYS
            ):
                found.append(
                    Finding(
                        "ACCOUNT_BASIS_NO_LABEL",
                        f"$.account_summary[...].{HOME_KEY}",
                        None,
                        None,
                        "the account-basis block carries no basis label",
                    )
                )
            after_row = _row(after.get("strategies"), strategy_id) or {}
            after_scope = after_row.get("return_scope")
            if not (
                isinstance(after_scope, Mapping)
                and any(HOME_KEY in str(key) for key in after_scope)
            ):
                found.append(
                    Finding(
                        "ACCOUNT_BASIS_NOT_REFERENCED",
                        f"$.strategies[{strategy_id}].return_scope",
                        None,
                        None,
                        "the strategy row's return_scope does not reference the "
                        "account-basis block",
                    )
                )
    return found


# ------------------------------------------------------------------------------ CLI


def _load(path: Path) -> Any:
    return json.loads(path.read_bytes())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    derive = sub.add_parser("derive", help="derive the sleeve figures of a declaration")
    derive.add_argument("--facts", type=Path, required=True)
    derive.add_argument("--declaration", type=Path, required=True)
    diff = sub.add_parser("diff-baseline", help="diff two expected baselines")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--declaration", type=Path)
    keep = sub.add_parser("non-diminish", help="account-basis figures kept")
    keep.add_argument("before", type=Path)
    keep.add_argument("after", type=Path)
    keep.add_argument("--declaration", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        declared = (
            parse_declaration(args.declaration.read_bytes())
            if args.declaration is not None
            else None
        )
        findings: list[Finding] = []
        if args.command == "derive":
            facts = _load(args.facts)
            assert declared is not None
            for strategy_id in sorted(declared):
                item = derive_sleeve(facts, strategy_id)
                print(
                    json.dumps(
                        {
                            "strategy_id": strategy_id,
                            "account_id": item.account_id,
                            "opening_date": item.opening_date.isoformat(),
                            "end_date": item.end_date.isoformat(),
                            "window_days": item.window_days,
                            "twr": format(item.twr, "f"),
                            "annualised": None
                            if item.annualised is None
                            else format(item.annualised, "f"),
                            "max_drawdown": format(item.max_drawdown, "f"),
                            "current_drawdown": format(item.current_drawdown, "f"),
                            "value": format(item.value, "f"),
                            "liability_line": format(item.liability_line, "f"),
                            "parent_equity": format(item.parent_equity, "f"),
                        },
                        indent=1,
                    )
                )
        elif args.command == "diff-baseline":
            findings = diff_expected(_load(args.before), _load(args.after), declared)
        else:
            assert declared is not None
            findings = check_non_diminish(
                _load(args.before), _load(args.after), declared
            )
    except Refusal as exc:
        print(f"sleeve_basis: REFUSED {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"sleeve_basis: cannot read {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(json.dumps(finding.as_dict()))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

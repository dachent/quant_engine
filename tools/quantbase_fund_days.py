"""Quantbase fund-day values after same-day fills (#909).

The Quantbase dashboard value of day D is the fund's holdings *before* D's fills
valued at D's close: the fund trades a few minutes before the close, so a day with a
fill understates (or overstates) the end-of-day value by the change of holdings times
that close.  Against the next day's dashboard value the rebalance then appears as a
return on the wrong day.

``corrected_values`` rebuilds holdings from the order legs, proves the rebuild against
the dashboard on every day it can value (pre-fill holdings x close + a constant cash
residual must equal the dashboard), and only then restates a fill day to its post-fill
holdings x close.  A fill day whose closes are missing, or whose pre-fill rebuild does
not close, is never silently restated: it keeps the dashboard value and says why.
A fill moves holdings, not value: the restated value of a fill day must lie within
``MAX_RESTATEMENT_CHANGE`` (3 %) of the dashboard value, else the restatement is refused and the
dashboard value kept (a close series in other units than the legs, for example split-adjusted
closes against as-traded quantities, values the post-fill holdings at a fraction of the fund:
TQQQ 2025-08-06, 245.08 shares x 44.42 adjusted instead of 88.84 as traded).
A restatement also needs the NEXT dashboard day to reproduce: that day's pre-fill value is this
day's post-fill holdings at its close, so a unit or scale error in the closes (split-adjusted
closes against as-traded quantities), or value that entered outside the order legs (a deposit),
shows there as a rebuild that does not close and the restatement is refused.
A pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

CASH: Final = "$USD"
ABS_TOLERANCE: Final = Decimal("25")
REL_TOLERANCE: Final = Decimal("0.0005")
# the largest change of a fill day's value a restatement may make (fill-to-close market move):
# 3 %, from the real 75 restated days (largest 2.41 %, 2026-04-21, then 1.92 %, 1.37 %; median 0.23 %)
# with headroom; a larger move keeps the dashboard value (conservative)
MAX_RESTATEMENT_CHANGE: Final = Decimal("0.03")

RESTATED: Final = "RESTATED_POST_FILL"
DASHBOARD: Final = "DASHBOARD_NO_FILL"
UNRESTATED_NO_CLOSES: Final = "DASHBOARD_PRE_FILL_NO_CLOSES"


UNRESTATED_NO_CLOSURE: Final = "DASHBOARD_REBUILD_DOES_NOT_CLOSE"
UNRESTATED_IMPLAUSIBLE: Final = "DASHBOARD_RESTATEMENT_EXCEEDS_TOLERANCE"
UNRESTATED_NEXT_DAY_UNPROVEN: Final = "DASHBOARD_NEXT_DAY_DOES_NOT_CLOSE"
# a non-fill day on which the series trades and a held symbol has a pinned series with no close that day
UNVALUED_HELD_CLOSE_MISSING: Final = "DASHBOARD_HELD_CLOSE_MISSING"
# share of the valued non-fill days that may fail the rebuild before the close series itself is
# distrusted (a split-adjusted price file fails most days the fund holds the split symbol)
MAX_NON_FILL_CLOSURE_FAILURE: Final = Decimal("0.05")
MIN_NON_FILL_DAYS: Final = 20


@dataclass(frozen=True)
class FundDay:
    value: Decimal
    status: str
    residual: Decimal | None


Leg = tuple[date, str, Decimal]


def _value(
    holdings: Mapping[str, Decimal],
    closes: Mapping[str, Mapping[date, Decimal]],
    day: date,
) -> Decimal | None:
    total = holdings.get(CASH, Decimal(0))
    for symbol, quantity in holdings.items():
        if symbol == CASH or quantity == 0:
            continue
        close = closes.get(symbol, {}).get(day)
        if close is None:
            return None
        total += quantity * close
    return total


def _held_close_hole(
    holdings: Mapping[str, Decimal],
    closes: Mapping[str, Mapping[date, Decimal]],
    day: date,
) -> bool:
    """A pinned series trades on ``day`` and a held symbol's own series, which spans ``day``, lacks it.

    A weekend, a holiday, a day outside the series, or a held symbol with no pinned series at all is
    not a hole: nothing was expected there.
    """

    if not any(day in series for series in closes.values()):
        return False
    for symbol, quantity in holdings.items():
        series = closes.get(symbol)
        if symbol == CASH or quantity == 0 or not series or day in series:
            continue
        if min(series) <= day <= max(series):
            return True
    return False


def corrected_values(
    dashboard: Mapping[date, Decimal],
    legs: Sequence[Leg],
    closes: Mapping[str, Mapping[date, Decimal]],
) -> dict[date, FundDay]:
    """Per dashboard day: the end-of-day value with the day's fills included."""

    by_day: dict[date, list[Leg]] = {}
    for leg in legs:
        by_day.setdefault(leg[0], []).append(leg)
    holdings: dict[str, Decimal] = {}
    result: dict[date, FundDay] = {}
    for day in sorted(set(dashboard) | set(by_day)):
        before = dict(holdings)
        for _day, symbol, quantity in by_day.get(day, []):
            holdings[symbol] = holdings.get(symbol, Decimal(0)) + quantity
        if day not in dashboard:
            continue
        nav = dashboard[day]
        pre = _value(before, closes, day)
        if pre is None:
            if day in by_day:
                status = UNRESTATED_NO_CLOSES
            elif _held_close_hole(before, closes, day):
                status = UNVALUED_HELD_CLOSE_MISSING
            else:
                # no hole in a pinned series (weekend, holiday, outside the series, or a held
                # symbol the pins do not cover): no information
                status = DASHBOARD
            result[day] = FundDay(nav, status, None)
            continue
        residual = nav - pre
        if abs(residual) > max(ABS_TOLERANCE, abs(nav) * REL_TOLERANCE):
            result[day] = FundDay(nav, UNRESTATED_NO_CLOSURE, residual)
            continue
        if day not in by_day:
            result[day] = FundDay(nav, DASHBOARD, residual)
            continue
        post = _value(holdings, closes, day)
        if post is None:
            result[day] = FundDay(nav, UNRESTATED_NO_CLOSES, residual)
            continue
        restated = post + residual
        if abs(restated - nav) > abs(nav) * MAX_RESTATEMENT_CHANGE:
            result[day] = FundDay(nav, UNRESTATED_IMPLAUSIBLE, residual)
            continue
        result[day] = FundDay(restated, RESTATED, residual)
    # every day the holdings could have been evaluated or changed: the dashboard days and every
    # fill day (a fill day without a dashboard row is a candidate too: the ledger drops rows whose
    # value is missing or zero)
    candidates = sorted(set(result) | set(by_day))
    for position, day in enumerate(candidates):
        if day not in result or result[day].status != RESTATED:
            continue
        # The proving day is the next day the holdings were evaluated or changed: a day with a
        # valued pre-fill rebuild, or a day with fills.  A trading day without closes (or without a
        # dashboard row) is never skipped (it would let a later day "prove" holdings it had already
        # sold); weekend and holiday days (no fills, no close) carry no information and are passed
        # over.
        proving = next(
            (
                later
                for later in candidates[position + 1 :]
                if later in by_day
                or (later in result and result[later].residual is not None)
            ),
            None,
        )
        proved = (
            proving is not None
            and proving in result
            and result[proving].residual is not None
            and result[proving].status != UNRESTATED_NO_CLOSURE
        )
        if not proved:
            result[day] = FundDay(
                dashboard[day], UNRESTATED_NEXT_DAY_UNPROVEN, result[day].residual
            )
    return result


def non_fill_closure_failures(
    result: Mapping[date, FundDay], legs: Sequence[Leg]
) -> tuple[int, int]:
    """(valued non-fill days, of those the days whose pre-fill rebuild does not close)."""

    fill_days = {leg[0] for leg in legs}
    valued = [
        day
        for day, fund_day in result.items()
        if day not in fill_days and fund_day.residual is not None
    ]
    failed = [day for day in valued if result[day].status == UNRESTATED_NO_CLOSURE]
    return len(valued), len(failed)


def non_fill_unvalued_days(result: Mapping[date, FundDay], legs: Sequence[Leg]) -> int:
    """Non-fill dashboard days on which the series trades but a held symbol has no close."""

    fill_days = {leg[0] for leg in legs}
    return sum(
        1
        for day, fund_day in result.items()
        if day not in fill_days and fund_day.status == UNVALUED_HELD_CLOSE_MISSING
    )


def closes_reproduce_the_dashboard(
    result: Mapping[date, FundDay], legs: Sequence[Leg]
) -> bool:
    """False when too few non-fill days are valued, or too many fail the rebuild, to trust the closes."""

    valued, failed = non_fill_closure_failures(result, legs)
    # a non-fill day on which the series trades but lacks a held close is a day the closes did not
    # reproduce: it counts as a failure in the ratio, so a close file with holes cannot pass on the
    # few days it does cover (days no symbol has a close on carry no information and do not count)
    unvalued = non_fill_unvalued_days(result, legs)
    # too few valued non-fill days cannot show that the closes reproduce the dashboard: refused,
    # not trusted (a short history of halved days must not pass)
    return valued >= MIN_NON_FILL_DAYS and Decimal(failed + unvalued) <= Decimal(
        valued + unvalued
    ) * (MAX_NON_FILL_CLOSURE_FAILURE)

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

RESTATED: Final = "RESTATED_POST_FILL"
DASHBOARD: Final = "DASHBOARD_NO_FILL"
UNRESTATED_NO_CLOSES: Final = "DASHBOARD_PRE_FILL_NO_CLOSES"


UNRESTATED_NO_CLOSURE: Final = "DASHBOARD_REBUILD_DOES_NOT_CLOSE"


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
            status = UNRESTATED_NO_CLOSES if day in by_day else DASHBOARD
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
        result[day] = FundDay(post + residual, RESTATED, residual)
    return result

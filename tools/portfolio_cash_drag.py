"""Cash drag of one strategy window, derived from a complete daily cash path.

Cash drag is the return the benchmark earned on the part of the account that sat in
cash, less what the cash itself earned: ``sum_t w[t-1] * (r_bench[t] - r_cash[t])``
with ``w[t-1] = max(cash[t-1], 0) / nav[t-1]`` (a negative balance is borrowing,
leverage, not cash: the leverage rows disclose it, it is never netted here; prior-close weights, so a day's own flows
never decide it).  It is derived only when the path is complete:

* every observation day of the window carries a NAV and a cash balance, with no gap;
* every benchmark return of the window is present on the days it is used;
* the cash path closes against the broker statement: each statement cash figure
  supplied equals the path's balance on its date to the cent.

Anything else is ``UNAVAILABLE`` with the reason naming the first failure; no value is
carried, filled or estimated.  Inputs are plain mappings of ISO date to decimal text so
the caller binds them to hashed captures.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from quant_engine.core.market_calendar import is_nyse_trading_day

STATUS_EVIDENCED: Final = "EVIDENCED"
STATUS_UNAVAILABLE: Final = "UNAVAILABLE"
CENT: Final = Decimal("0.01")


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": STATUS_UNAVAILABLE, "reason": reason, "cash_drag": None}


def derive_cash_drag(
    *,
    start: str,
    end: str,
    nav: Mapping[str, str],
    cash: Mapping[str, str],
    benchmark_returns: Mapping[str, str],
    cash_returns: Mapping[str, str] | None = None,
    statement_cash: Mapping[str, str] | None = None,
    opening_day: str | None = None,
) -> dict[str, Any]:
    """Cash drag over ``start``..``end`` (the days whose returns are compared).

    ``nav`` and ``cash`` are close balances and must cover ``start - 1 day`` through
    ``end``; ``benchmark_returns`` must cover ``start``..``end``.  ``cash_returns``
    defaults to no cash yield only when the caller states it is zero by passing an
    all-zero mapping; absent means the cash yield is unknown and the result is
    unavailable.
    """

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last <= first:
        return _unavailable("the window has fewer than two days")
    if cash_returns is None:
        return _unavailable("the cash yield is not stated")
    # The observation days are the benchmark's own dates in the window (calendar days
    # for a seven-day market, exchange days otherwise); each needs a close balance on
    # the day and on the observation before it, so a weekend return is weighted by the
    # Friday close.
    days = sorted(
        d
        for d in (date.fromisoformat(k) for k in benchmark_returns)
        if first <= d <= last
    )
    if len(days) < 2:
        return _unavailable(
            "the benchmark has fewer than two observations in the window"
        )
    if not statement_cash:
        return _unavailable("no statement cash figure to close the path against")
    # The balance a return is weighted by is the close of the previous observation
    # day (never an intervening weekend row), and observations must be consecutive
    # exchange days: a weekly or sparse reference is refused, not weighted by one day.
    nav_days = sorted(date.fromisoformat(k) for k in nav)
    # ``opening_day`` is the observation before the window (the reference's own previous
    # date); without it the nearest earlier balance row stands in.
    before_first = (
        [date.fromisoformat(opening_day)]
        if opening_day is not None
        else [n for n in nav_days if n < days[0]]
    )
    if not before_first:
        return _unavailable(f"no nav before {days[0].isoformat()}")
    if before_first[-1] >= days[0]:
        return _unavailable("the opening day is not before the first observation")
    prior = [before_first[-1], *days[:-1]]
    # every exchange day between the opening day and the end is an observation: a
    # trading day the reference has no return for is a hole, never skipped
    observed = {*days}
    cursor = prior[0] + timedelta(days=1)
    while cursor <= days[-1]:
        if cursor not in observed and is_nyse_trading_day(cursor):
            return _unavailable(
                f"the reference has no return on trading day {cursor.isoformat()}"
            )
        cursor += timedelta(days=1)
    window = {
        k: v
        for k, v in statement_cash.items()
        if prior[0] <= date.fromisoformat(k) <= days[-1]
    }
    if not window:
        return _unavailable("no statement cash figure inside the window")
    statement_cash = window
    for label, series, needed in (
        ("nav", nav, [*prior, *days]),
        ("cash", cash, [*prior, *days]),
        ("cash return", cash_returns, days),
    ):
        missing = sorted({x.isoformat() for x in needed if x.isoformat() not in series})
        if missing:
            return _unavailable(
                f"no daily {label} path: {len(missing)} day(s) missing, first {missing[0]}"
            )
    for needed_day in sorted({*prior, *days}):
        if Decimal(nav[needed_day.isoformat()]) <= 0:
            return _unavailable(f"nav is not positive on {needed_day.isoformat()}")
    gap = [
        (b.isoformat(), d.isoformat())
        for b, d in zip(prior, days, strict=True)
        if (d - b).days > 4
    ]
    if gap:
        return _unavailable(f"nav path has a gap before {gap[0][1]}")
    for day, figure in sorted((statement_cash or {}).items()):
        if day not in cash:
            return _unavailable(f"statement cash on {day} has no path balance")
        if abs(Decimal(cash[day]) - Decimal(figure)) > CENT / 2:
            return _unavailable(f"cash path does not close to the statement on {day}")
    drag = Decimal(0)
    for today, before in zip(days, prior, strict=True):
        navs = Decimal(nav[before.isoformat()])
        if navs <= 0:
            return _unavailable(f"nav is not positive on {before.isoformat()}")
        weight = max(Decimal(cash[before.isoformat()]), Decimal(0)) / navs
        drag += weight * (
            Decimal(benchmark_returns[today.isoformat()])
            - Decimal(cash_returns[today.isoformat()])
        )
    return {
        "status": STATUS_EVIDENCED,
        "reason": (
            f"derived on {len(days)} complete daily observations, closed to "
            f"{len(statement_cash)} statement cash figure(s), settled-cash basis"
        ),
        "cash_drag": format(drag, "f"),
    }

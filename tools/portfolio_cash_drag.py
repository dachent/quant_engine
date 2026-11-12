"""Cash drag of one strategy window, derived from a complete daily cash path.

Cash drag is the return the benchmark earned on the part of the account that sat in
cash, less what the cash itself earned: ``sum_t w[t-1] * (r_bench[t] - r_cash[t])``
with ``w[t-1] = cash[t-1] / nav[t-1]`` (prior-close weights, so a day's own flows
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
from datetime import date
from decimal import Decimal
from typing import Any, Final

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
    nav_days = sorted(date.fromisoformat(k) for k in nav)
    prior: list[date] = []
    for d in days:
        earlier = [n for n in nav_days if n < d]
        if not earlier:
            return _unavailable(f"no nav before {d.isoformat()}")
        prior.append(earlier[-1])
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
        weight = Decimal(cash[before.isoformat()]) / navs
        drag += weight * (
            Decimal(benchmark_returns[today.isoformat()])
            - Decimal(cash_returns[today.isoformat()])
        )
    return {
        "status": STATUS_EVIDENCED,
        "reason": f"derived on {len(days)} complete daily observations, closed to the statement",
        "cash_drag": format(drag, "f"),
    }

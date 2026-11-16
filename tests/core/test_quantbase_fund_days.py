from __future__ import annotations

from datetime import date
from decimal import Decimal

import quantbase_fund_days as q

D0, D1, D2, D3 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
CLOSES = {
    "SHV": {D1: Decimal("100"), D2: Decimal("100"), D3: Decimal("100")},
    "TQQQ": {D1: Decimal("50"), D2: Decimal("40"), D3: Decimal("40")},
}
# D1 opens in SHV; D2 sells it all for TQQQ at 45 (fills a few minutes before the close at 40).
LEGS = [
    (D0, "SHV", Decimal("10")),
    (D2, "SHV", Decimal("-10")),
    (D2, "TQQQ", Decimal("22")),
]
# Dashboard: pre-fill holdings at each close.


def test_a_fill_day_is_restated_to_post_fill_holdings_and_matches_the_next_day() -> (
    None
):
    """Kill test (#909): the dashboard's D2 value is the pre-fill SHV; D3 is the TQQQ."""

    dashboard = {
        D1: Decimal("1000"),
        D2: Decimal("1000"),
        D3: Decimal("880"),
    }
    out = q.corrected_values(dashboard, LEGS, CLOSES)
    assert out[D2].status == q.RESTATED
    assert out[D2].value == Decimal("880")
    assert out[D3].value == dashboard[D3]
    assert out[D2].value == out[D3].value  # no phantom return on the fill day


def test_a_rebuild_that_does_not_close_is_never_restated() -> None:
    dashboard = {D1: Decimal("1000"), D2: Decimal("1300"), D3: Decimal("880")}
    out = q.corrected_values(dashboard, LEGS, CLOSES)
    assert out[D2].status == q.UNRESTATED_NO_CLOSURE
    assert out[D2].value == Decimal("1300")


def test_a_fill_day_without_closes_keeps_the_dashboard_and_says_so() -> None:
    closes = {"SHV": CLOSES["SHV"], "TQQQ": {}}
    out = q.corrected_values(
        {D1: Decimal("1000"), D2: Decimal("1000")},
        LEGS,
        closes,
    )
    assert out[D2].status == q.UNRESTATED_NO_CLOSES
    assert out[D2].value == Decimal("1000")

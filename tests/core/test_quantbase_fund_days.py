from __future__ import annotations

from datetime import date
from decimal import Decimal

import quantbase_fund_days as q

D0, D1, D2, D3 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
CLOSES = {
    "SHV": {D1: Decimal("100"), D2: Decimal("100"), D3: Decimal("100")},
    "TQQQ": {D1: Decimal("50"), D2: Decimal("44"), D3: Decimal("44")},
}
# D1 opens in SHV; D2 sells it all for TQQQ at 45 (fills a few minutes before the close at 44).
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
        D3: Decimal("968"),
    }
    out = q.corrected_values(dashboard, LEGS, CLOSES)
    assert out[D2].status == q.RESTATED
    assert out[D2].value == Decimal("968")
    assert out[D3].value == dashboard[D3]
    assert out[D2].value == out[D3].value  # no phantom return on the fill day


def test_a_rebuild_that_does_not_close_is_never_restated() -> None:
    dashboard = {D1: Decimal("1000"), D2: Decimal("1300"), D3: Decimal("968")}
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


# --- real 2025-08-06 Volatis fill day (B blocker): split-adjusted closes against as-traded legs ----
# The fund held 197.4573 SHV + 20.0592 cash before the fill (dashboard 21,771.95) and sold all SHV
# for 245.0800 TQQQ (as-traded, pre 2025-11-20 split units) at 88.76.  A close series that is
# split-adjusted (TQQQ 2025-08-06 = 44.42, the as-traded close being 88.84) values the post-fill
# holdings at about half the fund: the restatement must be refused, never published.

E0, E1, E2, E3 = date(2025, 8, 4), date(2025, 8, 5), date(2025, 8, 6), date(2025, 8, 7)
REAL_LEGS = [
    (E0, "SHV", Decimal("197.4572805340")),
    (E0, "$USD", Decimal("20.0592372037")),
    (E2, "SHV", Decimal("-197.4572805340")),
    (E2, "TQQQ", Decimal("245.0799626930")),
    (E2, "$USD", Decimal("-20.0592372037")),
    (E2, "$USD", Decimal("17.2060885064")),
]
REAL_DASHBOARD = {
    E1: Decimal("21766.45"),
    E2: Decimal("21771.95"),
    E3: Decimal("22010.68"),
}


def _real_closes(tqqq: tuple[str, str, str]) -> dict[str, dict[date, Decimal]]:
    return {
        "SHV": {
            E1: Decimal("110.13"),
            E2: Decimal("110.16"),
            E3: Decimal("110.17"),
        },
        "TQQQ": {
            E1: Decimal(tqqq[0]),
            E2: Decimal(tqqq[1]),
            E3: Decimal(tqqq[2]),
        },
    }


def test_split_adjusted_closes_halve_the_fill_day_so_the_restatement_is_refused() -> (
    None
):
    out = q.corrected_values(
        REAL_DASHBOARD, REAL_LEGS, _real_closes(("42.82", "44.42", "44.87"))
    )
    assert out[E2].status == q.UNRESTATED_IMPLAUSIBLE
    assert out[E2].value == REAL_DASHBOARD[E2]  # the dashboard value is kept
    assert out[E2].residual is not None


def test_the_same_day_with_as_traded_closes_restates_within_the_tolerance() -> None:
    out = q.corrected_values(
        REAL_DASHBOARD, REAL_LEGS, _real_closes(("85.64", "88.84", "89.74"))
    )
    assert out[E2].status == q.RESTATED
    assert abs(out[E2].value - REAL_DASHBOARD[E2]) / REAL_DASHBOARD[E2] < Decimal(
        "0.05"
    )


def test_a_restatement_just_inside_and_just_outside_the_five_percent_gate() -> None:
    # post-fill value = 17.2061 + 245.0800 x close: inside at 4.9% above, outside at 5.1%
    base = REAL_DASHBOARD[E2]
    inside = (base * Decimal("1.049") - Decimal("17.2060885064")) / Decimal(
        "245.0799626930"
    )
    outside = (base * Decimal("1.051") - Decimal("17.2060885064")) / Decimal(
        "245.0799626930"
    )
    for close, status in ((inside, q.RESTATED), (outside, q.UNRESTATED_IMPLAUSIBLE)):
        out = q.corrected_values(
            REAL_DASHBOARD,
            REAL_LEGS,
            _real_closes(("85.64", str(close), "89.74")),
        )
        assert out[E2].status == status


def test_a_fill_to_close_move_beyond_the_gate_is_refused_even_when_the_rebuild_closes() -> (
    None
):
    # the same fill day with TQQQ closing at 40 (-11 % from the 45 fill): a 12 % restatement exceeds
    # the five percent gate, so the dashboard value is kept (conservative, never a halved level)
    closes = {"SHV": CLOSES["SHV"], "TQQQ": {D1: Decimal("50"), D2: Decimal("40")}}
    out = q.corrected_values({D1: Decimal("1000"), D2: Decimal("1000")}, LEGS, closes)
    assert out[D2].status == q.UNRESTATED_IMPLAUSIBLE
    assert out[D2].value == Decimal("1000")


def test_a_fill_day_whose_next_day_does_not_reproduce_is_refused() -> None:
    # value that entered outside the order legs (a deposit the night of the fill): the restated
    # day is inside 5 % but the next dashboard day is 5,000 above the rebuilt holdings
    dashboard = dict(REAL_DASHBOARD)
    dashboard[E3] = REAL_DASHBOARD[E3] + Decimal("5000")
    out = q.corrected_values(
        dashboard, REAL_LEGS, _real_closes(("85.64", "88.84", "89.74"))
    )
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == dashboard[E2]


def test_the_next_day_proof_catches_a_scale_error_the_five_percent_gate_misses() -> (
    None
):
    # closes 3 % low (a mis-scaled series): the fill day moves 3 % (inside the gate) and the
    # following day's rebuild is off by 3 % (outside the 0.05 % tolerance)
    out = q.corrected_values(
        REAL_DASHBOARD, REAL_LEGS, _real_closes(("83.0", "86.2", "87.1"))
    )
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN


def test_a_fill_day_with_no_following_dashboard_day_is_unproven() -> None:
    dashboard = {E1: REAL_DASHBOARD[E1], E2: REAL_DASHBOARD[E2]}
    out = q.corrected_values(
        dashboard, REAL_LEGS, _real_closes(("85.64", "88.84", "89.74"))
    )
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN


def test_a_close_series_that_fails_the_rebuild_on_many_non_fill_days_is_distrusted() -> (
    None
):
    # 30 non-fill days holding TQQQ with closes at half the as-traded level
    days = [date(2025, 9, 1 + i) for i in range(30)]
    legs = [(days[0], "TQQQ", Decimal("100"))]
    dashboard = {day: Decimal("8800") for day in days}
    halved = {"TQQQ": {day: Decimal("44") for day in days}}
    good = {"TQQQ": {day: Decimal("88") for day in days}}
    bad = q.corrected_values(dashboard, legs, halved)
    assert not q.closes_reproduce_the_dashboard(bad, legs)
    ok = q.corrected_values(dashboard, legs, good)
    assert q.closes_reproduce_the_dashboard(ok, legs)

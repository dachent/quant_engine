from __future__ import annotations

from datetime import date
from decimal import Decimal

import quantbase_fund_days as q

D0, D1, D2, D3 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
CLOSES = {
    "SHV": {D1: Decimal("100"), D2: Decimal("100"), D3: Decimal("100")},
    "TQQQ": {D1: Decimal("50"), D2: Decimal("44.5"), D3: Decimal("44.5")},
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
        D3: Decimal("979"),
    }
    out = q.corrected_values(dashboard, LEGS, CLOSES)
    assert out[D2].status == q.RESTATED
    assert out[D2].value == Decimal("979")
    assert out[D3].value == dashboard[D3]
    assert out[D2].value == out[D3].value  # no phantom return on the fill day


def test_a_rebuild_that_does_not_close_is_never_restated() -> None:
    dashboard = {D1: Decimal("1000"), D2: Decimal("1300"), D3: Decimal("979")}
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


# --- synthetic fill day (B blocker): split-adjusted closes against as-traded legs ----
# The fund holds 200 SHV + 20 cash before the fill (dashboard 22,020.00) and sells all SHV for
# 250 TQQQ (as-traded, pre-split units) at 87.96.  A close series that is split-adjusted (the
# fill-day TQQQ close 44.00, the as-traded close being 88.00) values the post-fill holdings at about
# half the fund: the restatement must be refused, never published.  All figures are synthetic.

E0, E1, E2, E3 = date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5), date(2025, 3, 6)
FILL_LEGS = [
    (E0, "SHV", Decimal("200")),
    (E0, "$USD", Decimal("20")),
    (E2, "SHV", Decimal("-200")),
    (E2, "TQQQ", Decimal("250")),
    (E2, "$USD", Decimal("-20")),
    (E2, "$USD", Decimal("30")),
]
FILL_DASHBOARD = {
    E1: Decimal("22020.00"),
    E2: Decimal("22020.00"),
    E3: Decimal("22280.00"),
}


def _fill_closes(tqqq: tuple[str, str, str]) -> dict[str, dict[date, Decimal]]:
    return {
        "SHV": {
            E1: Decimal("110.00"),
            E2: Decimal("110.00"),
            E3: Decimal("110.00"),
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
        FILL_DASHBOARD, FILL_LEGS, _fill_closes(("43.00", "44.00", "44.50"))
    )
    assert out[E2].status == q.UNRESTATED_IMPLAUSIBLE
    assert out[E2].value == FILL_DASHBOARD[E2]  # the dashboard value is kept
    assert out[E2].residual is not None


def test_the_same_day_with_as_traded_closes_restates_within_the_tolerance() -> None:
    out = q.corrected_values(
        FILL_DASHBOARD, FILL_LEGS, _fill_closes(("86.00", "88.00", "89.00"))
    )
    assert out[E2].status == q.RESTATED
    assert abs(out[E2].value - FILL_DASHBOARD[E2]) / FILL_DASHBOARD[E2] < Decimal(
        "0.03"
    )


def test_a_restatement_just_inside_and_just_outside_the_five_percent_gate() -> None:
    # post-fill value = 30 + 250 x close: inside at 2.9% above, outside at 3.1%
    base = FILL_DASHBOARD[E2]
    inside = (base * Decimal("1.029") - Decimal("30")) / Decimal("250")
    outside = (base * Decimal("1.031") - Decimal("30")) / Decimal("250")
    for close, status in ((inside, q.RESTATED), (outside, q.UNRESTATED_IMPLAUSIBLE)):
        out = q.corrected_values(
            FILL_DASHBOARD,
            FILL_LEGS,
            _fill_closes(("86.00", str(close), "89.00")),
        )
        assert out[E2].status == status


def test_a_fill_to_close_move_beyond_the_gate_is_refused_even_when_the_rebuild_closes() -> (
    None
):
    # the same fill day with TQQQ closing at 40 (-11 % from the 45 fill): a 12 % restatement exceeds
    # the three percent gate, so the dashboard value is kept (conservative, never a halved level)
    closes = {"SHV": CLOSES["SHV"], "TQQQ": {D1: Decimal("50"), D2: Decimal("40")}}
    out = q.corrected_values({D1: Decimal("1000"), D2: Decimal("1000")}, LEGS, closes)
    assert out[D2].status == q.UNRESTATED_IMPLAUSIBLE
    assert out[D2].value == Decimal("1000")


def test_a_fill_day_whose_next_day_does_not_reproduce_is_refused() -> None:
    # value that entered outside the order legs (a deposit the night of the fill): the restated
    # day is inside 3 % but the next dashboard day is 5,000 above the rebuilt holdings
    dashboard = dict(FILL_DASHBOARD)
    dashboard[E3] = FILL_DASHBOARD[E3] + Decimal("5000")
    out = q.corrected_values(
        dashboard, FILL_LEGS, _fill_closes(("86.00", "88.00", "89.00"))
    )
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == dashboard[E2]


def test_the_next_day_proof_catches_a_scale_error_the_five_percent_gate_misses() -> (
    None
):
    # closes 3 % low (a mis-scaled series): the fill day moves 3 % (inside the gate) and the
    # following day's rebuild is off by 3 % (outside the 0.05 % tolerance)
    out = q.corrected_values(
        FILL_DASHBOARD, FILL_LEGS, _fill_closes(("83.4", "85.4", "86.3"))
    )
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN


def test_a_fill_day_with_no_following_dashboard_day_is_unproven() -> None:
    dashboard = {E1: FILL_DASHBOARD[E1], E2: FILL_DASHBOARD[E2]}
    out = q.corrected_values(
        dashboard, FILL_LEGS, _fill_closes(("86.00", "88.00", "89.00"))
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


def test_a_next_day_without_closes_that_trades_the_position_away_does_not_prove_the_fill() -> (
    None
):
    """Opus P1: E3 sells the TQQQ for SHV but its TQQQ close row is absent; the proof must not skip
    E3 and let E4 (SHV only) prove E2 (the series is 3 % low: a scale error the proof catches
    whenever E3's close is present)."""

    e4 = date(2025, 3, 7)
    legs = [
        *FILL_LEGS,
        (E3, "TQQQ", Decimal("-250")),
        (E3, "SHV", Decimal("199.0")),
    ]
    low = _fill_closes(("83.4", "85.4", "86.3"))
    shv = {**low["SHV"], e4: Decimal("110.01")}
    dashboard = {
        E1: FILL_DASHBOARD[E1],
        E2: FILL_DASHBOARD[E2],
        E3: FILL_DASHBOARD[E3],
        e4: Decimal("30") + Decimal("199.0") * Decimal("110.01"),
    }
    # E3's TQQQ close is present: the 3 % scale error is caught on E3's pre-fill rebuild
    present = q.corrected_values(dashboard, legs, {"SHV": shv, "TQQQ": low["TQQQ"]})
    assert present[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    # E3's TQQQ close is absent: E3 trades, so it is the proving day and cannot prove
    missing = {"SHV": shv, "TQQQ": {E1: low["TQQQ"][E1], E2: low["TQQQ"][E2]}}
    out = q.corrected_values(dashboard, legs, missing)
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == FILL_DASHBOARD[E2]


def test_too_few_valued_non_fill_days_do_not_show_that_the_closes_reproduce() -> None:
    days = [date(2025, 9, 1 + i) for i in range(10)]
    legs = [(days[0], "TQQQ", Decimal("100"))]
    halved = q.corrected_values(
        {d: Decimal("8800") for d in days},
        legs,
        {"TQQQ": {d: Decimal("44") for d in days}},
    )
    assert not q.closes_reproduce_the_dashboard(halved, legs)  # 9 valued days: refused
    good = q.corrected_values(
        {d: Decimal("8800") for d in days},
        legs,
        {"TQQQ": {d: Decimal("88") for d in days}},
    )
    assert not q.closes_reproduce_the_dashboard(
        good, legs
    )  # also unproven: too short to judge


def test_a_fill_day_without_a_dashboard_row_is_a_proving_day_that_cannot_prove() -> (
    None
):
    """Opus P1 variant: E3 sells the TQQQ for SHV, its closes are present but the dashboard has no
    row for E3 (the ledger drops rows with a missing or zero value): the fill day itself is the
    proving day, there is no dashboard value on it, so E2 stays unproven and unrestated."""

    e4 = date(2025, 3, 7)
    legs = [
        *FILL_LEGS,
        (E3, "TQQQ", Decimal("-250")),
        (E3, "SHV", Decimal("199.0")),
    ]
    low = _fill_closes(("83.4", "85.4", "86.3"))
    closes = {
        "SHV": {**low["SHV"], e4: Decimal("110.01")},
        "TQQQ": low["TQQQ"],
    }
    dashboard = {
        E1: FILL_DASHBOARD[E1],
        E2: FILL_DASHBOARD[E2],
        e4: Decimal("30") + Decimal("199.0") * Decimal("110.01"),
    }
    out = q.corrected_values(dashboard, legs, closes)
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == FILL_DASHBOARD[E2]

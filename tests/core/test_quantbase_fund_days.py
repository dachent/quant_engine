from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import quantbase_fund_days as q

Leg = q.Leg
Closes = dict[str, dict[date, Decimal]]

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
        "0.03"
    )


def test_a_restatement_just_inside_and_just_outside_the_five_percent_gate() -> None:
    # post-fill value = 17.2061 + 245.0800 x close: inside at 2.9% above, outside at 3.1%
    base = REAL_DASHBOARD[E2]
    inside = (base * Decimal("1.029") - Decimal("17.2060885064")) / Decimal(
        "245.0799626930"
    )
    outside = (base * Decimal("1.031") - Decimal("17.2060885064")) / Decimal(
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
    # the three percent gate, so the dashboard value is kept (conservative, never a halved level)
    closes = {"SHV": CLOSES["SHV"], "TQQQ": {D1: Decimal("50"), D2: Decimal("40")}}
    out = q.corrected_values({D1: Decimal("1000"), D2: Decimal("1000")}, LEGS, closes)
    assert out[D2].status == q.UNRESTATED_IMPLAUSIBLE
    assert out[D2].value == Decimal("1000")


def test_a_fill_day_whose_next_day_does_not_reproduce_is_refused() -> None:
    # value that entered outside the order legs (a deposit the night of the fill): the restated
    # day is inside 3 % but the next dashboard day is 5,000 above the rebuilt holdings
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


def test_a_next_day_without_closes_that_trades_the_position_away_does_not_prove_the_fill() -> (
    None
):
    """Opus P1: E3 sells the TQQQ for SHV but its TQQQ close row is absent; the proof must not skip
    E3 and let E4 (SHV only) prove E2 (the series is 3 % low: a scale error the proof catches
    whenever E3's close is present)."""

    e4 = date(2025, 8, 8)
    legs = [
        *REAL_LEGS,
        (E3, "TQQQ", Decimal("-245.0799626930")),
        (E3, "SHV", Decimal("199.0")),
    ]
    low = _real_closes(("83.0", "86.2", "87.1"))
    shv = {**low["SHV"], e4: Decimal("110.18")}
    dashboard = {
        E1: REAL_DASHBOARD[E1],
        E2: REAL_DASHBOARD[E2],
        E3: REAL_DASHBOARD[E3],
        e4: Decimal("17.2060885064") + Decimal("199.0") * Decimal("110.18"),
    }
    # E3's TQQQ close is present: the 3 % scale error is caught on E3's pre-fill rebuild
    present = q.corrected_values(dashboard, legs, {"SHV": shv, "TQQQ": low["TQQQ"]})
    assert present[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    # E3's TQQQ close is absent: E3 trades, so it is the proving day and cannot prove
    missing = {"SHV": shv, "TQQQ": {E1: low["TQQQ"][E1], E2: low["TQQQ"][E2]}}
    out = q.corrected_values(dashboard, legs, missing)
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == REAL_DASHBOARD[E2]


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

    e4 = date(2025, 8, 8)
    legs = [
        *REAL_LEGS,
        (E3, "TQQQ", Decimal("-245.0799626930")),
        (E3, "SHV", Decimal("199.0")),
    ]
    low = _real_closes(("83.0", "86.2", "87.1"))
    closes = {
        "SHV": {**low["SHV"], e4: Decimal("110.18")},
        "TQQQ": low["TQQQ"],
    }
    dashboard = {
        E1: REAL_DASHBOARD[E1],
        E2: REAL_DASHBOARD[E2],
        e4: Decimal("17.2060885064") + Decimal("199.0") * Decimal("110.18"),
    }
    out = q.corrected_values(dashboard, legs, closes)
    assert out[E2].status == q.UNRESTATED_NEXT_DAY_UNPROVEN
    assert out[E2].value == REAL_DASHBOARD[E2]


def test_only_a_missing_held_close_on_a_day_the_series_covers_counts_against_it() -> (
    None
):
    """F2 item 23: a non-fill day is unvalued only when the close series has a close for some symbol
    on that day (it trades) but not for a held symbol.  Pre-window days, weekends, holidays and days
    beyond the series' end carry no close for any symbol and must not count."""

    trading = [date(2025, 9, 1) + timedelta(days=i) for i in range(40)]
    trading = [d for d in trading if d.weekday() < 5]  # 28 weekdays
    pre_window = [date(2025, 8, 28), date(2025, 8, 29)]
    weekend = [d for d in (date(2025, 9, 6), date(2025, 9, 7)) if d not in trading]
    holiday = date(
        2025, 9, 1
    )  # Labor Day: in the weekday list, but the series has no close
    covered = [d for d in trading if d != holiday]
    legs = [
        (date(2025, 8, 1), "TQQQ", Decimal("100"))
    ]  # held before every dashboard day
    dashboard = {d: Decimal("8800") for d in [*pre_window, *weekend, *trading]}

    def trusted(held: dict[date, Decimal], other: dict[date, Decimal]) -> bool:
        out = q.corrected_values(dashboard, legs, {"TQQQ": held, "SHV": other})
        return q.closes_reproduce_the_dashboard(out, legs)

    shv = {d: Decimal("100") for d in covered}
    full = {d: Decimal("88") for d in covered}
    assert trusted(full, shv)  # pre-window, weekend and holiday rows do not count
    one_hole = {d: c for d, c in full.items() if d != covered[5]}
    assert trusted(
        one_hole, shv
    )  # 1 of 27 covered days (3.7 %) is inside the 5 % bound
    holes = {d: c for d, c in full.items() if d not in set(covered[5:9])}
    assert not trusted(holes, shv)  # 4 genuine missing held closes (14.8 %) are refused


def test_a_held_symbol_the_pins_do_not_cover_is_not_a_hole() -> None:
    """Real shape: the fund also held symbols with no pinned series (e.g. a hedge); a day it held
    only those cannot be rebuilt, but nothing was expected from the series there."""

    days = [date(2025, 9, 1 + i) for i in range(30)]
    legs = [
        (date(2025, 8, 1), "TQQQ", Decimal("100")),
        (date(2025, 8, 1), "QQQ", Decimal("5")),
    ]
    dashboard = {day: Decimal("8800") for day in days}
    closes = {
        "TQQQ": {day: Decimal("88") for day in days},
        "SHV": {day: Decimal("100") for day in days},
    }
    out = q.corrected_values(dashboard, legs, closes)
    assert q.non_fill_unvalued_days(out, legs) == 0


def _truncated(first: int, last: int) -> tuple[list[date], list[Leg], Closes, dict]:
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(30)]
    legs = [(date(2025, 8, 1), "TQQQ", Decimal("100"))]
    dashboard = {d: Decimal("8800") for d in days}
    closes = {
        "TQQQ": {d: Decimal("88") for d in days[first : 30 - last]},
        "SHV": {d: Decimal("100") for d in days},
    }
    return days, legs, closes, dashboard


def test_a_pin_truncated_at_either_end_counts_the_missing_days() -> None:
    """Validator tightening: the hole test has no own-span clause, so a held symbol's pinned series
    that stops early (tail) or starts late (head) cannot hide behind its own date range."""

    for first, last in ((0, 8), (8, 0)):
        _, legs, closes, dashboard = _truncated(first, last)
        out = q.corrected_values(dashboard, legs, closes)
        assert q.non_fill_unvalued_days(out, legs) == 8


def test_coverage_counts_valued_unpinned_and_no_information_days() -> None:
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(30)]
    legs = [
        (date(2025, 8, 1), "TQQQ", Decimal("100")),
        (days[20], "QQQ", Decimal("5")),  # an unpinned symbol held on day 21 only
        (days[22], "QQQ", Decimal("-5")),
    ]
    dashboard = {d: Decimal("8800") for d in days}
    closes = {
        "TQQQ": {d: Decimal("88") for d in days[:25]},
        "SHV": {d: Decimal("100") for d in days[:25]},
    }
    out = q.corrected_values(dashboard, legs, closes)
    cov = q.closes_coverage(out, legs)
    assert cov == {
        "non_fill_days": 28,
        "valued_days": 22,
        "coverage_ratio": Decimal(22) / Decimal(28),
        "unvalued_held_close_missing_days": 0,
        "unpinned_held_symbol_days": 1,
        "no_information_days": 5,
    }


def test_known_limit_a_co_held_unpinned_symbol_does_not_mask_a_pinned_hole() -> None:
    """Documented limit (zero such days in the real data): the day is counted as a hole because
    the pinned symbol lacks its close, whatever else is held."""

    days, legs, closes, dashboard = _truncated(0, 8)
    legs = [*legs, (date(2025, 8, 2), "QQQ", Decimal("5"))]
    out = q.corrected_values(dashboard, legs, closes)
    assert q.non_fill_unvalued_days(out, legs) == 8


def test_known_limit_a_sole_pinned_series_that_ends_early_is_not_seen() -> None:
    """Documented blind spot: when the only pinned series ends, no pinned series trades on the later
    days, so they read as no information rather than as holes.  The coverage ratio shows it."""

    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(30)]
    legs = [(date(2025, 8, 1), "TQQQ", Decimal("100"))]
    dashboard = {d: Decimal("8800") for d in days}
    out = q.corrected_values(
        dashboard, legs, {"TQQQ": {d: Decimal("88") for d in days[:22]}}
    )
    assert q.non_fill_unvalued_days(out, legs) == 0
    assert q.closes_coverage(out, legs)["coverage_ratio"] == Decimal(22) / Decimal(30)

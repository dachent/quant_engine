"""Factor lens status: final-review items on PR 1058 (ceiling pin, key patterns, allowlists).

Synthetic fixtures only; every rule is exercised through build_status where it is wired there.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import factor_lens_status as fls  # noqa: E402

EVAL = date(2026, 11, 19)
DAYS = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]
WINDOWS = [{"week_start": "2026-11-09", "days": [d.isoformat() for d in DAYS]}]
FACTORS = list(fls.FRENCH_FACTORS["US"])


def french(last: str, evaluation: date = EVAL) -> dict:
    return fls.french_panel(
        panel_id="french-us",
        region="US",
        last_observation=last,
        evaluation_date=evaluation,
        factors=FACTORS,
    )


def status(**kw: object) -> dict:
    base: dict = {"evaluation_date": EVAL, "windows": WINDOWS, "trading_days": DAYS}
    return fls.build_status(**{**base, **kw})  # type: ignore[arg-type]


# --- (1) the 75-day ceiling is defence in depth, pinned equivalent to the month rule ----


def test_age_ceiling_never_changes_the_outcome_of_the_required_month_rule() -> None:
    """Month-rule COMPLIANT always has age <= 75, for every day 2020-2030 and every month end.

    So the ceiling can never turn a month-compliant series stale: 75 is not an independent
    tunable gate (the converse does not hold on some dates, where the month rule is stricter).
    """
    month_ends = []
    m = date(2019, 1, 31)
    while m < date(2031, 1, 1):
        month_ends.append(m)
        m = fls._next_month_end(m)
    max_compliant_age = 0
    day = date(2020, 1, 1)
    while day <= date(2030, 12, 31):
        required = fls.required_french_month(day)
        for me in month_ends:
            if me >= required:
                age = max((day - me).days, 0)
                assert age <= fls.FRENCH_HEADLINE_LAG_DAYS, (day, me)
                max_compliant_age = max(max_compliant_age, age)
        day += timedelta(days=1)
    assert max_compliant_age == fls.FRENCH_HEADLINE_LAG_DAYS


def test_panel_state_matches_the_month_rule_alone() -> None:
    for evaluation in (
        date(2026, 5, 14),
        date(2026, 5, 15),
        date(2026, 12, 14),
        date(2026, 12, 15),
    ):
        required = fls.required_french_month(evaluation)
        for months_back in range(0, 5):
            me = required
            for _ in range(months_back):
                me = fls._prev_month_end(me)
            panel = french(me.isoformat(), evaluation)
            assert (panel["state"] == "COMPLIANT") == (me >= required)


def test_docstring_states_ceiling_is_defence_in_depth_and_month_level_compliance() -> (
    None
):
    doc = fls.__doc__ or ""
    assert "defence in depth" in doc
    assert "month-level" in doc


# --- (2) 'star' only as a whole token ---------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "window_start",
        "week_start",
        "started_at",
        "restart",
        "strategy_start",
        "startDate",
        "mid_start_date",
    ],
)
def test_start_like_keys_are_not_significance_marks(key: str) -> None:
    cmp = {
        "strategy": "S",
        "window": "live",
        "gap": 0.1,
        "noise_threshold": 0.2,
        key: "2026-01-05",
    }
    assert fls.validate_comparisons([cmp]) == []
    assert status(panels=[french("2026-09-30")], comparisons=[cmp])["comparisons"]


@pytest.mark.parametrize(
    "key", ["star", "stars", "p_star", "stars_count", "asterisk", "asterisks"]
)
def test_star_tokens_still_flagged(key: str) -> None:
    cmp = {
        "strategy": "S",
        "window": "live",
        "gap": 0.1,
        "noise_threshold": 0.2,
        key: 1,
    }
    with pytest.raises(fls.StatusError, match="significance"):
        status(panels=[french("2026-09-30")], comparisons=[cmp])


# --- (3) wider key patterns -------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "p-value",
        "p_value",
        "pval",
        "pvalue",
        "prob",
        "pr_gt_t",
        "tstat",
        "t-stat",
        "t_stat",
        "tval",
        "z",
        "z_score",
        "zstat",
        "adj_p",
        "q_value",
        "fdr",
        "holm_adj",
        "sig",
        "asterisk",
        "mark",
        "pValue",
        "tStat",
        "Significant",
        "wild_bootstrap_p",
        "bootstrap_p",
        "p_nw",
        "t",
        "p",
    ],
)
def test_significance_key_patterns_fail_through_build_status(key: str) -> None:
    cmp = {
        "strategy": "S",
        "window": "live",
        "gap": 0.1,
        "noise_threshold": 0.2,
        key: 0.03,
    }
    with pytest.raises(fls.StatusError, match="significance"):
        status(panels=[french("2026-09-30")], comparisons=[cmp])


@pytest.mark.parametrize(
    "key",
    [
        "gap",
        "interval_note",
        "noise_threshold",
        "strategy",
        "weeks",
        "lens",
        "benchmark",
        "design",
    ],
)
def test_ordinary_keys_pass(key: str) -> None:
    base = {"strategy": "S", "window": "live", "gap": 0.1, "noise_threshold": 0.2}
    assert fls.validate_comparisons([{**base, key: base.get(key, "x")}]) == []


# --- (4) unknown keys rejected, nested loadings stripped, window completeness -----------


def test_panel_extra_key_is_rejected() -> None:
    for extra in ({"note": "free text"}, {"spliced_note": "x"}):
        with pytest.raises(fls.StatusError, match="undeclared"):
            status(panels=[{**french("2026-09-30"), **extra}])


def test_etf_panel_extra_key_is_rejected_and_clean_one_passes() -> None:
    etf = fls.etf_bridge_panel(
        panel_id="e",
        region="Europe",
        last_observation="2026-11-18",
        evaluation_date=EVAL,
        bridge_ids=["VGK-RF"],
        configured_ids=["VGK-RF"],
    )
    assert status(panels=[etf])
    with pytest.raises(fls.StatusError, match="undeclared"):
        status(panels=[{**etf, "note": "as good as French"}])


def test_bridge_extra_key_is_rejected() -> None:
    rec = fls.bridge_record(region="Europe", enlarged_configured=False)
    for extra in ({"note": "x"}, {"spliced_note": "spliced"}):
        with pytest.raises(fls.StatusError, match="undeclared"):
            status(panels=[french("2026-09-30")], bridges=[{**rec, **extra}])


def test_not_applicable_strips_nested_loadings_recursively() -> None:
    doc = status(
        panels=[french("2026-09-30")],
        strategies=[
            {
                "strategy": "crypto",
                "adj_r2": {"french": -0.01},
                "exposures": {"a": 1},
                "weights": [1],
                "factor_t": {"a": 2.1},
                "lenses": {
                    "french": {"loadings": {"a": 1}, "adj_r2": -0.01, "betas": [1]}
                },
                "detail": [{"coef": 1, "keep": "yes"}],
            }
        ],
    )
    row = doc["strategies"][0]
    assert row["attribution"]["state"] == "NOT_APPLICABLE"
    flat = repr(row)
    for gone in ("exposures", "weights", "factor_t", "loadings", "betas", "coef"):
        assert gone not in flat, gone
    assert row["lenses"]["french"]["adj_r2"] == -0.01
    assert row["detail"] == [{"keep": "yes"}]


def test_applicable_strategy_keeps_nested_loadings() -> None:
    doc = status(
        panels=[french("2026-09-30")],
        strategies=[
            {
                "strategy": "real",
                "adj_r2": {"french": 0.6},
                "lenses": {"french": {"loadings": {"a": 1}}},
            }
        ],
    )
    assert doc["strategies"][0]["lenses"]["french"]["loadings"] == {"a": 1}


def test_windows_must_cover_every_week_of_the_run() -> None:
    two_weeks = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
    first = {"week_start": "2026-11-02", "days": [d.isoformat() for d in two_weeks[:5]]}
    second = {
        "week_start": "2026-11-09",
        "days": [d.isoformat() for d in two_weeks[5:]],
    }
    assert status(
        panels=[french("2026-09-30")], windows=[first, second], trading_days=two_weeks
    )
    with pytest.raises(fls.StatusError, match="missing"):
        status(panels=[french("2026-09-30")], windows=[second], trading_days=two_weeks)
    with pytest.raises(fls.StatusError, match="duplicate"):
        status(
            panels=[french("2026-09-30")],
            windows=[first, first, second],
            trading_days=two_weeks,
        )
    extra = {"week_start": "2026-11-16", "days": []}
    with pytest.raises(fls.StatusError, match="no trading day"):
        status(
            panels=[french("2026-09-30")],
            windows=[first, second, extra],
            trading_days=two_weeks,
        )

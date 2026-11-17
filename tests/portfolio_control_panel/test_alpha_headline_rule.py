"""#904 items 10 and 11: when an alpha may stand as a headline.

An alpha is a headline only with a standard error (so a t-statistic) and with no
hypothetical backtest in the series it was fitted on.  |t| < 2 reads "not
distinguishable from zero".  The rule is over each fit's own composition, never a
strategy name.  Figures are recomputed here (t = alpha / standard error).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "panel_alpha_rule", REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


panel = _load()


def _row(
    net: str | None, se: str | None, backtest: int, used: int = 200
) -> dict[str, Any]:
    return {
        "status": "AVAILABLE",
        "alpha": {
            "net_annualized": net,
            "gross_annualized": None,
            "annualized_standard_error": se,
            "net_status": "AVAILABLE_OBSERVED_BASIS",
            "confidence_status": "AVAILABLE_OLS",
        },
        "coverage": {
            "observed_start": "2025-01-02",
            "observed_end": "2026-11-06",
            "aligned_observations": used,
        },
        "series_composition": {
            "backtest_observations_used": backtest,
            "live_observations": used - backtest,
            "calculation_observations_used": used,
        },
    }


def test_a_live_fit_with_a_clear_t_statistic_is_a_headline() -> None:
    ctx = panel._alpha_context(_row("0.20", "0.05", 0))
    assert ctx["headline_status"] == "HEADLINE_ALLOWED"
    assert ctx["inference"] == "DISTINGUISHABLE_FROM_ZERO"
    assert round(float(ctx["t_stat"]), 6) == round(0.20 / 0.05, 6)


def test_an_alpha_inside_two_standard_errors_is_not_distinguishable_from_zero() -> None:
    """AI Factor static lens: -1.6% with a 15.5% standard error."""

    ctx = panel._alpha_context(_row("-0.0158", "0.155", 0))
    assert ctx["inference"] == "NOT_DISTINGUISHABLE_FROM_ZERO"
    assert round(float(ctx["t_stat"]), 6) == round(-0.0158 / 0.155, 6)


def test_an_alpha_without_a_standard_error_is_not_a_headline() -> None:
    """The default-panel alphas (+20.5%, -40.5%) carried no standard error."""

    ctx = panel._alpha_context(_row("0.2045", None, 0))
    assert ctx["inference"] == "NO_STANDARD_ERROR"
    assert ctx["headline_status"] == "NOT_A_HEADLINE_NO_STANDARD_ERROR"
    assert panel._alpha_headline(_row("0.2045", None, 0)) is False


def test_a_fit_on_a_series_with_backtest_is_never_a_headline() -> None:
    """GrizzlyBears hybrid +81.7%: more than 95% vendor backtest."""

    row = _row("0.8174", "0.05", 190)
    ctx = panel._alpha_context(row)
    assert ctx["headline_status"] == "NOT_A_HEADLINE_INCLUDES_BACKTEST"
    assert panel._alpha_headline(row) is False
    # one backtest observation is enough: the rule is composition, not a share
    assert (
        panel._alpha_context(_row("0.8", "0.05", 1))["headline_status"]
        == "NOT_A_HEADLINE_INCLUDES_BACKTEST"
    )


def test_an_unknown_or_malformed_composition_fails_closed() -> None:
    """Review finding: a missing backtest count fell through to HEADLINE_ALLOWED."""

    row = _row("0.8174", "0.05", 190)
    row["series_composition"] = {}
    assert panel._alpha_context(row)["headline_status"] == (
        "NOT_A_HEADLINE_COMPOSITION_UNKNOWN"
    )
    row["series_composition"] = {"backtest_observations_used": "190"}
    assert panel._alpha_context(row)["headline_status"] == (
        "NOT_A_HEADLINE_COMPOSITION_UNKNOWN"
    )
    # the fit's own evidence layers count even when the composition says zero
    row = _row("0.8174", "0.05", 0)
    row["evidence_layers"] = ["ACCOUNT_LIVE", "BACKTEST"]
    assert panel._alpha_context(row)["headline_status"] == (
        "NOT_A_HEADLINE_INCLUDES_BACKTEST"
    )


def test_a_negative_standard_error_gives_no_t_statistic() -> None:
    ctx = panel._alpha_context(_row("0.20", "-0.05", 0))
    assert ctx["t_stat"] is None
    assert ctx["headline_status"] == "NOT_A_HEADLINE_NO_STANDARD_ERROR"


def test_no_alpha_at_all_is_its_own_status() -> None:
    assert (
        panel._alpha_context(_row(None, None, 0))["headline_status"]
        == "UNAVAILABLE_NO_ALPHA"
    )


def test_only_the_allowed_fit_is_the_headline_lens() -> None:
    assert panel._alpha_context(_row("0.2", "0.05", 0))["lens_role"] == "HEADLINE"
    assert panel._alpha_context(_row("0.2", None, 0))["lens_role"] == "SECONDARY"
    assert panel._alpha_context(_row("0.2", "0.05", 5))["lens_role"] == "SECONDARY"


def test_a_live_fit_under_a_year_is_not_a_headline() -> None:
    """The layer helper requires a year of live window (sprint-p123 headline_allowed)."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_start"] = "2026-03-01"  # 250 days to 2026-11-06
    ctx = panel._alpha_context(row)
    assert ctx["headline_status"] == "NOT_A_HEADLINE_UNDER_365_DAYS_OR_NOT_LIVE"
    assert ctx["lens_role"] == "SECONDARY"
    assert round(float(ctx["t_stat"]), 6) == round(0.20 / 0.05, 6)


def test_the_layer_and_the_opening_close_decide_the_headline() -> None:
    """Review findings: evidence_layer was never read; a daily year was 364 days."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_start"] = "2025-11-07"  # 364 days to 2026-11-06
    assert panel._alpha_context(row)["headline_status"] == "HEADLINE_ALLOWED"
    row["coverage"]["observed_start"] = "2025-11-08"  # 363 days: still short
    assert panel._alpha_context(row)["headline_status"] == (
        "NOT_A_HEADLINE_UNDER_365_DAYS_OR_NOT_LIVE"
    )
    row["coverage"]["observed_start"] = "2025-01-02"
    row["evidence_layer"] = "PROVIDER_LIVE"
    assert panel._alpha_context(row)["headline_status"] == "HEADLINE_ALLOWED"
    for layer in ("BACKTEST", "HYBRID", "SOMETHING_NEW"):
        row["evidence_layer"] = layer
        assert panel._alpha_context(row)["headline_status"] == (
            "NOT_A_HEADLINE_UNDER_365_DAYS_OR_NOT_LIVE"
        )


def test_a_fit_the_sealed_gate_rejected_is_never_a_headline() -> None:
    """Factor-model audit D3: a DIAGNOSTIC_ONLY fit with a standard error stood as a headline."""

    row = _row("0.349", "0.05", 0)
    row["status"] = "DIAGNOSTIC_ONLY"
    ctx = panel._alpha_context(row)
    assert ctx["headline_status"] == "NOT_A_HEADLINE_MODEL_GATE_FAILED"
    assert ctx["lens_role"] == "SECONDARY"
    assert panel._alpha_headline(row) is False


def test_a_fit_window_that_stops_before_the_live_series_is_not_a_headline() -> None:
    """Factor-model audit D1: the default lens ended 2026-08-31 beside 2026-11-06 figures."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_end"] = "2026-08-31"
    row["coverage"]["canonical_live_observed_end"] = "2026-11-06"
    ctx = panel._alpha_context(row)
    assert ctx["window_lag_days"] == 67
    assert ctx["headline_status"] == "NOT_A_HEADLINE_STALE_FACTOR_WINDOW"
    # a few days of lag is not stale
    row["coverage"]["observed_end"] = "2026-11-02"
    assert panel._alpha_context(row)["headline_status"] == "HEADLINE_ALLOWED"


def test_extreme_loadings_and_a_negative_train_r_squared_are_flagged() -> None:
    """Factor-model audit D7/D8: betas of -6.4 / +5.3 and a train R2 of -0.014 were unflagged."""

    row = _row("0.20", "0.05", 0)
    assert panel._alpha_context(row)["quality_flags"] == []
    row["exposures"] = [{"beta": "3.49"}, {"beta": "-6.36"}]
    row["diagnostics"] = {"train": {"r_squared": "-0.0139837"}}
    assert panel._alpha_context(row)["quality_flags"] == [
        "LARGE_EXPOSURE_CHECK_SCALE",
        "NEGATIVE_TRAIN_R_SQUARED",
    ]


def test_the_alpha_window_is_the_training_window_not_the_whole_sample() -> None:
    """Annualization matrix D2: a 77-observation train fit was labelled with the 277-day sample."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_start"] = "2025-01-02"
    row["interval_association"] = {
        "buckets": [
            {"partition": "TRAIN", "factor_anchor": "2025-01-02"},
            {"partition": "TRAIN", "factor_anchor": "2025-04-11"},
            {"partition": "HOLDOUT", "factor_anchor": "2026-11-06"},
        ]
    }
    ctx = panel._alpha_context(row)
    assert ctx["train_window_start"] == "2025-01-02"
    assert ctx["train_window_end"] == "2025-04-11"
    assert ctx["train_window_days"] == 99
    # fitted on 99 days, so not a headline although the sample spans years
    assert ctx["headline_status"] == "NOT_A_HEADLINE_UNDER_365_DAYS_OR_NOT_LIVE"


def test_the_default_lens_is_a_cutoff_current_lens() -> None:
    """Factor-model audit D1: ECONOMIC_FACTOR (French files, monthly) ends before the cutoff."""

    lenses = [{"lens_id": "ECONOMIC_FACTOR"}, {"lens_id": "ETF_STYLE"}]
    assert panel._current_default_lens(lenses) == "ETF_STYLE"
    assert panel._current_default_lens([{"lens_id": "ECONOMIC_FACTOR"}]) == (
        "ECONOMIC_FACTOR"
    )
    assert panel._current_default_lens(
        [{"lens_id": "ECONOMIC_FACTOR"}, {"lens_id": "RETURN_SECTOR"}]
    ) == ("RETURN_SECTOR")


def test_a_stale_lens_carries_the_research_lens_label() -> None:
    """Factor-model audit D1: French-file lenses end at the last published month."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_end"] = "2026-09-30"
    row["coverage"]["canonical_live_observed_end"] = "2026-11-06"
    ctx = panel._alpha_context(row)
    assert ctx["research_lens_label"] == (
        "research lens: factor data through 2026-09-30, not a cutoff-current headline"
    )
    row["coverage"]["observed_end"] = "2026-11-06"
    assert panel._alpha_context(row)["research_lens_label"] is None


def test_the_stale_gate_uses_the_accepted_history_end_real_rows_carry_08_31_as_live_end() -> (
    None
):
    """Review P1-1: real ECONOMIC_FACTOR rows put 2026-08-31 in canonical_live_observed_end."""

    row = _row("0.20", "0.05", 0)
    row["coverage"]["observed_end"] = "2026-08-31"
    row["coverage"]["canonical_live_observed_end"] = "2026-08-31"
    row["series_composition"]["accepted_history_complete_through"] = "2026-11-06"
    ctx = panel._alpha_context(row)
    assert ctx["window_lag_days"] == 67
    assert ctx["headline_status"] == "NOT_A_HEADLINE_STALE_FACTOR_WINDOW"
    assert ctx["research_lens_label"] is not None


def test_a_gate_rejected_row_with_its_alpha_withheld_still_says_why() -> None:
    """Review follow-on: withheld alpha gave UNAVAILABLE_NO_ALPHA, hiding the gate."""

    row = _row(None, None, 0)
    row["status"] = "DIAGNOSTIC_ONLY"
    assert panel._alpha_context(row)["headline_status"] == (
        "NOT_A_HEADLINE_MODEL_GATE_FAILED"
    )


def test_large_loadings_on_a_gate_rejected_row_are_flagged_from_the_detail() -> None:
    """Review P1-2: gate-rejected rows publish exposures [] and keep betas in diagnostic_detail."""

    row = _row(None, None, 0)
    row["status"] = "DIAGNOSTIC_ONLY"
    row["exposures"] = []
    row["diagnostic_detail"] = {
        "exposures": [{"beta": "3.49465923275"}, {"beta": "-6.35912095865"}]
    }
    assert "LARGE_EXPOSURE_CHECK_SCALE" in panel._alpha_context(row)["quality_flags"]

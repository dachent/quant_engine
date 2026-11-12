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

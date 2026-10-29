"""The alpha tile context: window, n, SE, t, backtest share, basis and annualisation.

Every figure is derived from fields the analytics already carry; nothing is added to
``alpha`` and the annualisation multiplier is the governed ``periods_per_year``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


panel = _load(
    "portfolio_control_panel_for_alpha_context", "tools/portfolio_control_panel.py"
)


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": "AVAILABLE",
        "cost_coverage": "INCOMPLETE",
        "alpha": {
            "annualized_standard_error": "0.2",
            "confidence_status": "AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION",
            "gross_annualized": None,
            "net_annualized": "-0.1",
            "net_status": "AVAILABLE_OBSERVED_BASIS",
        },
        "coverage": {
            "observed_start": "2026-01-22",
            "observed_end": "2026-07-31",
            "aligned_observations": 132,
            "train_observations": 105,
            "factor_observed_end": "2026-07-31",
        },
        "series_composition": {
            "backtest_observations_used": 0,
            "live_observations": 171,
            "calculation_observations_used": 171,
            "accepted_history_complete_through": "2026-09-17",
        },
        "interval_association": {"periods_per_year": 252},
        "unavailable_reasons": [],
    }
    row.update(overrides)
    return row


def test_a_live_alpha_carries_window_n_se_t_basis_and_annualisation() -> None:
    context = panel._alpha_context(_row())
    assert context["status"] == "AVAILABLE" and context["unavailable_reason"] is None
    assert (context["window_start"], context["window_end"]) == (
        "2026-01-22",
        "2026-07-31",
    )
    assert context["window_days"] == 190 and context["n_obs"] == 132
    assert context["train_observations"] == 105
    assert context["se"] == "0.2" and context["t_stat"].startswith("-0.5")
    assert context["t_status"] == "AVAILABLE_AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION"
    assert context["backtest_share"] == "0.000000000000"
    assert context["basis_status"] == "AVAILABLE_OBSERVED_BASIS"
    assert context["periods_per_year"] == 252
    assert context["annualisation_basis"] == "ANNUALISED_FROM_DAYS"
    assert context["factor_as_of_date"] == "2026-07-31"
    assert context["factor_staleness_days"] == 48
    assert context["basis_label"] == (
        "AVAILABLE_OBSERVED_BASIS · 0% backtest · cost coverage INCOMPLETE"
    )


def test_a_hybrid_alpha_states_the_backtest_share_and_has_no_t_without_a_se() -> None:
    row = _row(
        alpha={
            "annualized_standard_error": None,
            "confidence_status": "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS",
            "gross_annualized": None,
            "net_annualized": "0.36",
            "net_status": "DIAGNOSTIC_ONLY_MIXED_MODEL_BASIS",
        },
        series_composition={
            "backtest_observations_used": 1486,
            "live_observations": 171,
            "calculation_observations_used": 1657,
            "accepted_history_complete_through": "2026-09-17",
        },
        coverage={
            "observed_start": "2020-05-13",
            "observed_end": "2026-07-31",
            "aligned_observations": 1562,
            "factor_observed_end": "2026-07-31",
        },
    )
    context = panel._alpha_context(row)
    assert context["se"] is None and context["t_stat"] is None
    assert context["t_status"] == "UNAVAILABLE_NO_STANDARD_ERROR"
    assert context["backtest_share"] == "0.896801448401"
    assert context["annualisation_basis"] == "ANNUALISED"
    assert context["basis_label"].startswith(
        "DIAGNOSTIC_ONLY_MIXED_MODEL_BASIS · 90% backtest"
    )


def test_a_series_without_alpha_is_unavailable_with_its_reason() -> None:
    context = panel._alpha_context(
        {
            "alpha": None,
            "status": "UNAVAILABLE",
            "unavailable_reasons": ["NOT_APPLICABLE_METHOD_SCOPE"],
            "cost_coverage": "INCOMPLETE",
        }
    )
    assert context["status"] == "UNAVAILABLE"
    assert context["unavailable_reason"] == "NOT_APPLICABLE_METHOD_SCOPE"
    assert context["window_days"] is None and context["periods_per_year"] is None
    assert context["t_stat"] is None and context["backtest_share"] is None
    unavailable = panel._unavailable_factor_row("S", "NO_SOURCE")
    assert unavailable["alpha_context"]["unavailable_reason"] == "NO_SOURCE"


def test_the_multiplier_is_the_governed_one_not_a_literal() -> None:
    context = panel._alpha_context(_row(interval_association={"periods_per_year": 52}))
    assert context["periods_per_year"] == 52
    assert (
        panel._alpha_context(_row(interval_association=None))["periods_per_year"]
        is None
    )

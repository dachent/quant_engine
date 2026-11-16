"""#904 regression case alpha_headline_without_standard_error: Alpha tile published as a headline with no standard error, or on a backtest-dominated fit (#904 item 11)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import importlib.util  # noqa: E402


def _panel():
    spec = importlib.util.spec_from_file_location(
        "panel_regression_alpha", ROOT / "tools" / "portfolio_control_panel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alpha_headline_without_standard_error() -> None:
    row = {
        "status": "AVAILABLE",
        "alpha": {
            "net_annualized": "0.20",
            "annualized_standard_error": None,
            "net_status": "AVAILABLE_OBSERVED_BASIS",
        },
        "coverage": {
            "observed_start": "2025-01-02",
            "observed_end": "2026-11-06",
            "aligned_observations": 200,
        },
        "series_composition": {
            "backtest_observations_used": 0,
            "live_observations": 200,
            "calculation_observations_used": 200,
        },
    }
    panel = _panel()
    assert (
        panel._alpha_context(row)["headline_status"]
        == "NOT_A_HEADLINE_NO_STANDARD_ERROR"
    )
    row["alpha"]["annualized_standard_error"] = "0.05"
    row["series_composition"]["backtest_observations_used"] = 190
    assert (
        panel._alpha_context(row)["headline_status"]
        == "NOT_A_HEADLINE_INCLUDES_BACKTEST"
    )

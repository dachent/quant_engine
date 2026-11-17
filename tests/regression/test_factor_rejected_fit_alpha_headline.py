"""Factor-model audit regression case factor_rejected_fit_alpha_headline: A fit the sealed holdout gate rejected (DIAGNOSTIC_ONLY) stood as a headline alpha."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))


def _panel():
    spec = importlib.util.spec_from_file_location(
        "panel_regression_fm", ROOT / "tools" / "portfolio_control_panel.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(status="AVAILABLE", observed_end="2026-11-06"):
    return {
        "status": status,
        "alpha": {
            "net_annualized": "0.349",
            "annualized_standard_error": "0.05",
            "net_status": "AVAILABLE_OBSERVED_BASIS",
        },
        "coverage": {
            "observed_start": "2025-01-02",
            "observed_end": observed_end,
            "canonical_live_observed_end": "2026-11-06",
            "aligned_observations": 200,
        },
        "series_composition": {
            "backtest_observations_used": 0,
            "live_observations": 200,
            "calculation_observations_used": 200,
        },
    }


def test_factor_rejected_fit_alpha_headline() -> None:
    ctx = _panel()._alpha_context(_row(status="DIAGNOSTIC_ONLY"))
    assert ctx["headline_status"] == "NOT_A_HEADLINE_MODEL_GATE_FAILED"

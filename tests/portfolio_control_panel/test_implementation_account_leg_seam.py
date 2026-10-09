"""The control panel reads a version-2 implementation row as it reads version 1.

The panel passes the report's implementation row through as the underwriting
``detail``; the account leg and the basis labels ride along without changing any
gap the panel derives (QC-1 N66).
"""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS_ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


panel = _load("portfolio_control_panel_leg_seam", "portfolio_control_panel.py")
reconciliation = _load(
    "portfolio_implementation_reconciliation",
    "portfolio_implementation_reconciliation.py",
)


def _producer_row() -> dict[str, Any]:
    row = {
        "return_reconciliation": {
            "account_cumulative_return": 1.001 * 1.0017 - 1,
            "account_minus_model_cumulative": -0.2724919,
            "model_authority": "DIAGNOSTIC_SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE",
            "model_cumulative_return": 0.2752115,
            "observations": 2,
            "observed_end": "2026-01-06",
            "observed_start": "2026-01-05",
            "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
        },
        "strategy_id": "grizzlybears-vtma",
    }
    return reconciliation.with_account_leg(
        row,
        [
            {
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.001",
                "strategy_id": "grizzlybears-vtma",
            },
            {
                "date": "2026-01-06",
                "evidence_layer": "ACCOUNT_LIVE",
                "return": "0.0017",
                "strategy_id": "grizzlybears-vtma",
            },
        ],
        strategy_id="grizzlybears-vtma",
        accepted_as_of=date(2026, 8, 3),
        model_basis="SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE_PROXY",
    )


def test_panel_gap_is_unchanged_by_the_account_leg_and_basis_labels() -> None:
    policy = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-strategy-underwriting-v1.json"
        ).read_text(encoding="utf-8")
    )["model_account_gap"]
    strategy = {
        "cash_drag_status": "UNAVAILABLE",
        "fees_status": "PARTIAL",
        "provider_account_cumulative_return": "0.0027196",
        "provider_beta": "0.9713",
        "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
        "provider_comparison_version_compatibility": "UNRESOLVED",
        "provider_correlation": "0.9847",
        "provider_cumulative_gap": "-0.05",
        "provider_reference_cumulative_return": "0.05",
        "provider_tracking_error_annualized": "0.0297",
        "strategy_id": "grizzlybears-vtma",
        "strategy_name": "GrizzlyBears VTMA",
    }

    without = panel._model_account_gap_underwriting(strategy, policy)
    with_leg = panel._model_account_gap_underwriting(
        {**strategy, "implementation_reconciliation": _producer_row()}, policy
    )

    assert with_leg == without
    row = _producer_row()
    assert row["account_leg"]["basis"] == "BASELINE_ACCOUNT_LIVE"
    assert row["return_reconciliation"]["account_basis"] == "BASELINE_ACCOUNT_LIVE"

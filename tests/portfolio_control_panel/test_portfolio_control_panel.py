from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
import runpy
import sys
from typing import Any

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import decimal_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio control-panel builder")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)

HWM_TEST_NAMESPACE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_ledger" / "test_hwm.py")
)


def _portable_hwm_bundle(
    *, schema_version: int = 2
) -> tuple[dict[str, Any], bytes, bytes]:
    fixture_name = (
        "_as_held_direct_net_return_fixture"
        if schema_version == 3
        else "_as_held_hwm_fixture"
    )
    fixture = HWM_TEST_NAMESPACE[fixture_name]()
    return (
        HWM_TEST_NAMESPACE["_compute_as_held"](fixture),
        fixture["input_generation_bytes"],
        fixture["calendar_bytes"],
    )


def _portable_hwm_receipt(*, schema_version: int = 2) -> dict[str, Any]:
    return _portable_hwm_bundle(schema_version=schema_version)[0]


STRATEGY_NAMES = (
    "CAOS current strategy",
    "Strategy 1",
    "Strategy 2",
    "Strategy 3",
    "Strategy 4",
    "Strategy 5",
    "Strategy 6",
    "Strategy 7",
    "Strategy 8",
)


def test_report_panel_expectations_follow_accepted_rollforward() -> None:
    report = {
        "account_summary": [{}, {}],
        "allocation_history": [{}, {}, {}, {}],
        "portfolio_summary": [
            {"metric": "Reserve", "value": "125000.00"},
            {"metric": "Systematic capital", "value": "480000.00"},
            {"metric": "Total liquid", "value": "605000.00"},
        ],
        "strategy_summary": [{}, {}, {}],
    }

    assert panel._report_panel_expectations(report) == {
        "capital": {
            "reserve": "125000.00",
            "systematic": "480000.00",
            "total_liquid": "605000.00",
        },
        "counts": {"accounts": 2, "allocations": 4, "strategies": 3},
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report() -> dict[str, object]:
    strategies = [
        {
            "account_live_cumulative_return": "0.01" if index < 5 else None,
            "account_live_max_drawdown": "-0.02" if index < 5 else None,
            "account_live_status": "AVAILABLE" if index < 5 else "UNAVAILABLE",
            "cost_status": "known_fixed" if index < 4 else "unknown",
            "current_value_usd": str(1000 + index),
            "economic_family": f"Synthetic family {index}",
            "implementation_gap_status": "tested" if index < 4 else "UNAVAILABLE",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_status": "AVAILABLE" if index < 6 else "UNAVAILABLE",
            "strategy_id": f"strategy-{index}",
            "strategy_name": STRATEGY_NAMES[index],
            "systematic_weight": str(1 / 9),
            "version_ref": None,
            "version_compatibility_reason": "Synthetic version remains unresolved.",
            "version_compatibility_status": "UNRESOLVED",
            "live_plus_backtest_reason": "UNRESOLVED: Synthetic version remains unresolved.",
            "live_plus_backtest_status": "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE",
        }
        for index in range(9)
    ]
    accounts = [
        {
            "account_id": f"account-{index:02d}",
            "provider": ("SCHWAB", "IBKR", "ALPACA")[index % 3],
        }
        for index in range(19)
    ]
    allocations = [
        {
            "as_of_date": "2026-07-30",
            "current_value_usd": "100043.62",
            "scope": "RESERVE",
            "source_key": "synthetic-summary",
            "strategy_id": None,
            "systematic_weight": None,
        },
        *[
            {
                "as_of_date": "2026-07-30",
                "current_value_usd": str(1000 + index),
                "scope": "SYSTEMATIC",
                "source_key": "synthetic-allocations",
                "strategy_id": f"strategy-{index}",
                "systematic_weight": str(1 / 9),
            }
            for index in range(9)
        ],
    ]
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "assignments",
            "known_gap": "one synthetic gap",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": "three synthetic strategies missing",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "flows",
            "known_gap": "synthetic partial",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "positions",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "FACT_COVERAGE",
            "item": "fx",
            "known_gap": "synthetic unavailable",
            "status": "FAILED",
            "value": None,
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "positions-unavailable",
            "known_gap": "BLOCKING",
            "status": "UNAVAILABLE",
            "value": None,
        },
    ]
    for row in coverage:
        row["disposition"] = "RETAINED_SYNTHETIC_TEST_GAP"
        if row["category"] == "FACT_COVERAGE":
            row["evidence_authority"] = "synthetic test fixture"
        else:
            row["resolution_authority"] = "synthetic test fixture"
    return {
        "account_summary": accounts,
        "allocation_history": allocations,
        "as_of_date": "2026-07-30",
        "baseline": {"generation_id": "b" * 64},
        "coverage_data_quality": coverage,
        "fact_coverage_matrix": [
            {
                "account_id": "account-00",
                "fact_type": "nav",
                "status": "AVAILABLE",
            }
        ],
        "portfolio_summary": [
            {"metric": "Total liquid", "value": "510316.01"},
            {"metric": "Systematic capital", "value": "410272.39"},
            {"metric": "Reserve", "value": "100043.62"},
            {"metric": "NAV coverage", "value": "0.7175692227302938908465178464"},
            {"metric": "Correlation-one stress", "value": "0.37249726899852387"},
            {
                "metric": "Full-systematic HWM",
                "value": "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV",
            },
            {"metric": "Advisory readiness", "value": "DO_NOT_EXPAND_SYSTEMATIC_RISK"},
        ],
        "positions": [
            {
                "account_id": "account-00",
                "as_of_date": "2026-07-30",
                "evidence_class": "WCH_CURRENT",
                "market_value_usd": "30",
                "quantity": "3",
                "security_id": "security-synthetic",
                "status": "AVAILABLE_MAPPED",
                "strategy_id": "strategy-0",
                "symbol": "SYNTH",
            }
        ],
        "provenance": [
            {
                "artifact": "synthetic-baseline",
                "as_of_date": "2026-07-30",
                "schema": "synthetic/1",
                "sha256": "c" * 64,
            }
        ],
        "reconciliation_checks": [
            {"check_id": "synthetic-total", "status": "PASS", "value_usd": "510316.01"}
        ],
        "report_schema_version": 1,
        "strategy_summary": strategies,
        "twr_mwr": [
            {
                "account_id": "account-00",
                "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
                "mwr_xirr": None,
                "nav_observations": 20,
                "observed_end": "2026-07-30",
                "observed_start": "2026-07-01",
                "strategy_id": "strategy-0",
                "twr": "0.01",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
            {
                "account_id": "account-01",
                "mwr_status": "UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE",
                "mwr_xirr": None,
                "nav_observations": 0,
                "strategy_id": None,
                "twr": None,
                "twr_status": "UNMAPPED",
            },
        ],
    }


def _decomposition() -> dict[str, object]:
    selections = []
    models = []
    for index, series_id in enumerate(STRATEGY_NAMES):
        model_id = f"model-{index}" if index < 5 else None
        selections.append(
            {
                "model_id": model_id,
                "series_id": series_id,
                "status": (
                    "SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL"
                    if index == 4
                    else "SELECTED_PARSIMONIOUS_TRAIN_CV_MODEL"
                    if model_id
                    else "UNAVAILABLE_NO_VALIDATED_MODEL"
                ),
            }
        )
        coverage = {
            "aligned_observations": 80 if model_id else 0,
            "alignment_ratio": "1" if model_id else "0",
            "canonical_live_series_sha256": "9" * 64,
            "factor_missing_target_dates": 0,
            "factor_missing_weekdays": 0,
            "factor_missing_weekends": 0,
            "observed_end": "2026-07-30" if model_id else None,
            "observed_start": "2026-01-02" if model_id else None,
            "target_observations": 80 if model_id else 0,
            "test_observations": 2 if model_id else 0,
            "train_observations": 78 if model_id else 0,
        }
        if model_id:
            estimator = "DYNAMIC_FLS" if index == 0 else "STATIC_RBSA"
            diagnostics: dict[str, object] = {
                "holdout": {
                    "adjusted_r_squared": "0.39",
                    "observation_count": 2,
                    "predicted_r_squared": "-0.1" if index == 4 else "0.4",
                    "r_squared": "0.4",
                    "rmse": "0.01",
                    "tracking_error_annualized": "0.15",
                },
                "rank": 2,
                "rank_status": "FULL_RANK",
                "train": {
                    "adjusted_r_squared": "0.38",
                    "observation_count": 78,
                    "r_squared": "0.39",
                    "rmse": "0.011",
                    "tracking_error_annualized": "0.16",
                },
            }
            if estimator == "DYNAMIC_FLS":
                diagnostics["dynamic"] = {
                    "exposure_path": [
                        {
                            "date": "2026-07-29",
                            "estimation_state": "TRAIN_SMOOTHED",
                            "exposure_change_l2": None,
                            "exposures": {"SPY_XS": "0.7"},
                        },
                        {
                            "date": "2026-07-30",
                            "estimation_state": "HOLDOUT_FILTERED_CAUSAL",
                            "exposure_change_l2": "0.1",
                            "exposures": {"SPY_XS": "0.8"},
                        },
                    ],
                    "holdout_comparison": {
                        "dynamic_predicted_r_squared": "0.4",
                        "dynamic_minus_static_predicted_r_squared": "0.1",
                        "selection_effect": "DIAGNOSTIC_ONLY_HOLDOUT_NOT_USED_FOR_SELECTION",
                        "static_predicted_r_squared": "0.3",
                        "status": "OUTPERFORMS_STATIC_SAME_FACTORS_HOLDOUT",
                    },
                    "largest_exposure_changes": [
                        {"date": "2026-07-30", "exposure_change_l2": "0.1"}
                    ],
                    "selected_smoothness": "0.0001",
                    "selection_predicted_r_squared": "0.45",
                    "selection_rule": "MAX_TRAIN_LOO_PREDICTED_R2_THEN_SMOOTHEST_WITHIN_TOLERANCE",
                    "state_transition": "IDENTITY_RANDOM_WALK",
                    "validation": "EXACT_LEAVE_ONE_OUT_TRAIN_ONLY",
                }
            models.append(
                {
                    "alpha": {
                        "annualized_standard_error": "0.01",
                        "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                        "gross_annualized": None,
                        "gross_status": "UNAVAILABLE_INCOMPLETE_COST_COVERAGE",
                        "net_annualized": "0.05",
                        "net_status": "AVAILABLE_OBSERVED_BASIS",
                        "periodic_intercept": "0.001",
                    },
                    "contributions": [
                        {
                            "actual_excess_return": "0.004",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-29",
                            "factor_contributions": {"SPY_XS": "0.002"},
                            "fitted_excess_return": "0.003",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                        {
                            "actual_excess_return": "0.005",
                            "alpha_contribution": "0.001",
                            "date": "2026-07-30",
                            "factor_contributions": {"SPY_XS": "0.003"},
                            "fitted_excess_return": "0.004",
                            "partition": "TEST",
                            "residual": "0.001",
                        },
                    ],
                    "cost_coverage": "INCOMPLETE",
                    "computational_status": "AVAILABLE",
                    "coverage": coverage,
                    "diagnostics": diagnostics,
                    "estimator": estimator,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "exposures": [
                        {
                            "confidence_status": "AVAILABLE_CLASSICAL_OLS",
                            "exposure": "0.8",
                            "factor_id": "SPY_XS",
                            "holdout_mean_period_contribution": "0.0025",
                            "standard_error": "0.1",
                        }
                    ],
                    "factor_ids": ["SPY_XS"],
                    "model_id": model_id,
                    "model_quality": {
                        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
                        "decision_status": (
                            "DIAGNOSTIC_ONLY" if index == 4 else "PM_VALID"
                        ),
                        "gate_reason": (
                            "NON_POSITIVE_SEALED_HOLDOUT_PREDICTED_R2"
                            if index == 4
                            else "POSITIVE_SEALED_HOLDOUT_PREDICTED_R2"
                        ),
                        "holdout_predicted_r_squared": (
                            "-0.1" if index == 4 else "0.4"
                        ),
                    },
                    "series_id": series_id,
                    "status": "DIAGNOSTIC_ONLY" if index == 4 else "AVAILABLE",
                }
            )
        else:
            models.append(
                {
                    "cost_coverage": "INCOMPLETE",
                    "coverage": coverage,
                    "estimator": "STATIC_RBSA",
                    "model_id": f"attempt-{index}",
                    "series_id": series_id,
                    "status": "UNAVAILABLE_UNKNOWN_TARGET_SERIES",
                }
            )
    return {
        "analytics_hash": "d" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "method_label": "MPI_INFORMED_NON_EQUIVALENT",
        "methodology_manifest_sha256": "e" * 64,
        "methodology_version": "ANL-DECOMP-CLEANROOM-2",
        "model_results": models,
        "model_selections": selections,
        "sources": {
            "factor": {"artifact_id": "synthetic-factor", "sha256": "f" * 64},
            "target": {"artifact_id": "synthetic-target", "sha256": "a" * 64},
        },
    }


def _correlation() -> dict[str, object]:
    size = len(STRATEGY_NAMES)
    observed_pairs = {
        (left, right)
        for index, left in enumerate(STRATEGY_NAMES[:5])
        for right in STRATEGY_NAMES[index + 1 : 5]
    }

    def matrix(off_diagonal: str | None) -> list[list[str | None]]:
        return [
            ["1" if row == column else off_diagonal for column in range(size)]
            for row in range(size)
        ]

    observed = matrix(None)
    spearman = matrix(None)
    cells = []
    blends = []
    implementation = []
    for left_index, left in enumerate(STRATEGY_NAMES):
        for right_index in range(left_index + 1, size):
            right = STRATEGY_NAMES[right_index]
            available = (left, right) in observed_pairs
            if available:
                observed[left_index][right_index] = "0.5"
                observed[right_index][left_index] = "0.5"
                spearman[left_index][right_index] = "0.45"
                spearman[right_index][left_index] = "0.45"
            cells.append(
                {
                    "coverage": {
                        "common_observations": 80 if available else 0,
                        "evidence_layer": "ACCOUNT_LIVE" if available else None,
                        "frequency": "DAILY_EXACT_DATE",
                        "left_observations": 80 if available else 0,
                        "missing_from_left": 0,
                        "missing_from_right": 0,
                        "observed_end": "2026-07-30" if available else None,
                        "observed_start": "2026-01-02" if available else None,
                        "right_observations": 80 if available else 0,
                    },
                    "downside": {
                        "correlation": "0.4" if available else None,
                        "observations": 30 if available else 0,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "drawdown_regimes": {
                        "both_strategies_underwater": {
                            "conditioning_rule": "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
                            "correlation": "0.35" if available else None,
                            "observations": 25 if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-02-02" if available else None,
                            "status": "AVAILABLE" if available else "UNAVAILABLE",
                        },
                        "either_strategy_underwater": {
                            "conditioning_rule": "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
                            "correlation": "0.4" if available else None,
                            "observations": 40 if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-01-15" if available else None,
                            "status": "AVAILABLE" if available else "UNAVAILABLE",
                        },
                    },
                    "horizons": {
                        str(horizon): {
                            "correlation": (
                                "0.5" if available and horizon <= 63 else None
                            ),
                            "observations": min(80, horizon) if available else 0,
                            "observed_end": "2026-07-30" if available else None,
                            "observed_start": "2026-01-02" if available else None,
                            "status": (
                                "AVAILABLE"
                                if available and horizon <= 63
                                else "UNAVAILABLE_INSUFFICIENT_HISTORY"
                            ),
                            "window_observations": horizon,
                        }
                        for horizon in (5, 21, 22, 63, 126, 130, 252, 260)
                    },
                    "joint_tail": {
                        "joint_days": 4 if available else 0,
                        "left_threshold": "-0.02" if available else None,
                        "multiple_vs_independence": "5" if available else None,
                        "right_threshold": "-0.02" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                    },
                    "left": left,
                    "pearson": "0.5" if available else None,
                    "right": right,
                    "rolling_pearson": {
                        "available_windows": 21 if available else 0,
                        "latest": "0.55" if available else None,
                        "maximum": "0.6" if available else None,
                        "mean": "0.5" if available else None,
                        "minimum": "0.4" if available else None,
                        "status": "AVAILABLE" if available else "UNAVAILABLE",
                        "window_observations": 60 if available else None,
                    },
                    "spearman": "0.45" if available else None,
                    "status": (
                        "AVAILABLE" if available else "UNAVAILABLE_MISSING_SERIES"
                    ),
                }
            )
            blends.append(
                {
                    "conceptual_prior": "0.25",
                    "conceptual_prior_note": "synthetic conceptual prior",
                    "decision_correlation": "0.3" if available else "1",
                    "decision_status": (
                        "OBSERVED_SUPPORTED_POSTERIOR"
                        if available
                        else "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION"
                    ),
                    "left": left,
                    "observations": 80 if available else 0,
                    "observed_correlation": "0.5" if available else None,
                    "observed_weight": "0.16" if available else "0",
                    "posterior_pre_psd": "0.3" if available else "0.25",
                    "right": right,
                    "stress_pre_psd": "0.65" if available else "0.4",
                }
            )
            implementation.append(
                {
                    "left": left,
                    "right": right,
                    "shared_instruments": [],
                    "shared_providers": [],
                    "shared_venues": [],
                    "status": "UNAVAILABLE_MAPPING_MISSING",
                }
            )
    return {
        "analytics_hash": "b" * 64,
        "analytics_schema_version": 1,
        "as_of_date": "2026-07-30",
        "builder_version": "synthetic",
        "dependence": {
            "blend_cells": blends,
            "central_case": {
                "classification": "MEASURED_MAX_DRAWDOWN_PSD_PROJECTED",
                "components": [
                    {
                        "series_id": series_id,
                        "status": "MEASURED",
                    }
                    for series_id in STRATEGY_NAMES
                ],
                "dependence_matrix": {"missing_pair_rule": "GOVERNED_CONCEPTUAL_PRIOR"},
                "governs_readiness": False,
                "loss": "0.2",
                "measured_component_count": 9,
                "measured_pair_count": 10,
                "method": "SQRT_V_TRANSPOSE_CORRELATION_V",
                "prior_pair_count": 26,
                "prior_component_count": 0,
                "status": "AVAILABLE_NOT_A_PROBABILITY_FORECAST",
            },
            "correlation_one": {
                "components": [
                    {
                        "allocation_weight": str(1 / 9),
                        "conservative_loss_fraction": "0.37249726899852387",
                        "loss_contribution": str(
                            Decimal("0.37249726899852387") / Decimal(9)
                        ),
                        "series_id": series_id,
                    }
                    for series_id in STRATEGY_NAMES
                ],
                "declared_loss": "0.37249726899852387",
                "governs_readiness": True,
                "loss": "0.37249726899852387",
                "method": "SUM_WEIGHT_TIMES_GOVERNED_CONSERVATIVE_BOUND_INPUT",
                "reconciliation_difference": "0",
                "status": "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND",
            },
            "implementation_cells": implementation,
            "drawdown_matrices": {
                "both_strategies_underwater": {
                    "classification": "MEASURED_CONDITIONED_ON_PAIR_WEALTH_DRAWDOWN_STATE",
                    "conditioning_rule": "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
                    "values": matrix("0.35"),
                },
                "either_strategy_underwater": {
                    "classification": "MEASURED_CONDITIONED_ON_PAIR_WEALTH_DRAWDOWN_STATE",
                    "conditioning_rule": "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
                    "values": matrix("0.4"),
                },
            },
            "horizon_matrices": {
                str(horizon): {
                    "classification": "MEASURED_WEEKDAY_OBSERVATION_WINDOW",
                    "values": matrix("0.5" if horizon <= 63 else None),
                    "window_observations": horizon,
                }
                for horizon in (5, 21, 22, 63, 126, 130, 252, 260)
            },
            "matrices": {
                "conceptual_prior": {
                    "classification": "CONCEPTUAL_NOT_MEASURED",
                    "values": matrix("0.25"),
                },
                "decision_no_missing_diversification_credit": {
                    "classification": "CONSERVATIVE_DECISION_ONLY_NOT_PSD_COVARIANCE_INPUT",
                    "values": matrix("1"),
                },
                "observed_pearson": {
                    "classification": "MEASURED_COMPATIBLE_EXACT_DATE_ONLY",
                    "values": observed,
                },
                "observed_spearman": {
                    "classification": "MEASURED_ROBUST_RANK_CHECK",
                    "values": spearman,
                },
                "posterior": {
                    "classification": "CONCEPTUAL_OBSERVED_BLEND_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.7",
                        "minimum_eigenvalue_pre_projection": "0.7",
                    },
                    "values": matrix("0.3"),
                },
                "stress": {
                    "classification": "STRESS_ASSUMPTION_PSD_PROJECTED",
                    "projection": {
                        "maximum_absolute_adjustment": "0",
                        "minimum_eigenvalue_post_projection": "0.55",
                        "minimum_eigenvalue_pre_projection": "0.55",
                    },
                    "values": matrix("0.45"),
                },
            },
            "observed_cells": cells,
            "series_ids": list(STRATEGY_NAMES),
        },
        "policy": {},
        "source": {},
    }


def _correlation_v2() -> dict[str, object]:
    value = json.loads(json.dumps(_correlation()))
    value["analytics_schema_version"] = 2
    live = value["dependence"]
    live["evidence_mode"] = "LIVE_ONLY"
    live["series_composition"] = {
        series_id: {
            "backtest_evidence_layer": "BACKTEST",
            "backtest_observations_excluded_at_or_after_live_inception": 20,
            "backtest_observations_used": 0,
            "evidence_mode": "LIVE_ONLY",
            "ignored_other_version_observations": 0,
            "live_evidence_layer": "ACCOUNT_LIVE",
            "live_inception": "2026-01-02",
            "live_observations": 80,
            "observed_end": "2026-07-30",
            "observed_start": "2026-01-02",
            "series_id": series_id,
            "status": "AVAILABLE_LIVE_ONLY",
            "strategy_version": f"synthetic-version:strategy-{index}",
        }
        for index, series_id in enumerate(STRATEGY_NAMES)
    }
    combined = json.loads(json.dumps(live))
    combined["evidence_mode"] = "LIVE_PLUS_BACKTEST"
    for row in combined["series_composition"].values():
        row["backtest_observations_used"] = 60
        row["evidence_mode"] = "LIVE_PLUS_BACKTEST"
        row["observed_start"] = "2025-10-01"
        row["status"] = "AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE"
    for row in combined["observed_cells"]:
        row["evidence_mode"] = "LIVE_PLUS_BACKTEST"
        row["left_evidence_composition"] = combined["series_composition"][row["left"]]
        row["right_evidence_composition"] = combined["series_composition"][row["right"]]
        if row["status"] == "AVAILABLE":
            row["coverage"]["common_observations"] = 140
            row["coverage"]["observed_start"] = "2025-10-01"
    value["default_evidence_mode"] = "LIVE_ONLY"
    value["dependence_modes"] = {
        "LIVE_ONLY": live,
        "LIVE_PLUS_BACKTEST": combined,
    }
    return value


def _publish_analytics(root: Path, filename: str, value: object, adapter: str) -> None:
    source = root.parent / f"{adapter}.json"
    _write_json(source, value)
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity(adapter, "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {adapter}",
            fact_types=(adapter,),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id=f"synthetic/{filename}",
                as_of_date=date(2026, 7, 30),
                schema_name=adapter,
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / filename).write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)


def _publish_report(root: Path, source: Path, value: object) -> None:
    _write_json(source, value)
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-report-builder", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control-panel test report",
            fact_types=("synthetic report",),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/portfolio-report-data.json",
                as_of_date=date(2026, 7, 30),
                schema_name="synthetic-portfolio-report",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-report-data.json").write_bytes(
        source.read_bytes()
    )
    publish_generation(root, manifest.generation_id)


def _report_with_baseline(inputs: dict[str, Any]) -> dict[str, Any]:
    """A fresh fixture report bound to the fixture's baseline generation."""
    report = _report()
    report["baseline"] = inputs["baseline_identity"]
    return report


def _with_hwm_summary(
    report: dict[str, Any],
    freshness_path: Path,
    baseline_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Attach the governed HWM summary the panel will recompute, derived from
    the report's strategy rows, the given freshness assessment file, and the
    governed baseline catalog (the panel's reconciliation authority).

    The panel requires and unconditionally reconciles the accepted report's
    hwm_summary, so fixture reports must carry the expected summary.
    """
    freshness_rows = json.loads(freshness_path.read_text(encoding="utf-8"))[
        "assessments"
    ]
    policy, _policy_hash = panel._hwm_policy(
        date.fromisoformat(freshness_rows[0]["evaluated_on"])
    )
    _strategies, coverage_summary = panel._governed_hwm_coverage(
        report["strategy_summary"],
        policy,
        freshness_rows,
        require_freshness=True,
    )
    report["hwm_summary"] = {
        **coverage_summary,
        **panel._compute_panel_numerical_hwm(baseline_catalog),
    }
    return report


def _panel_baseline_catalog(
    envelope: dict[str, Any] | None = None,
    crosswalk: list[dict[str, Any]] | None = None,
    extra_source_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The governed baseline catalog matching the panel fixture report."""
    from decimal import Decimal

    # Compute matching allocations from the envelope and crosswalk so the
    # endpoint recomputation produces delta=0.  When no envelope is provided,
    # use the original fixed-value allocations (enough for perimeter tests).
    matching_allocations: list[dict[str, Any]] = [
        {
            "as_of_date": "2026-07-30",
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "100043.62",
            },
            "scope": "RESERVE",
            "strategy_id": None,
        }
    ]
    if envelope is not None and crosswalk is not None:
        by_day: dict[str, dict[str, Decimal]] = {}
        for rw in crosswalk:
            sid = str(rw["strategy_id"])
            day = str(rw["date"])
            ret = Decimal(str(rw.get("return", "0")))
            by_day.setdefault(day, {})[sid] = ret
        endpoint_date = "2026-07-30"
        for row in envelope.get("periods", []):
            if (
                row.get("disposition") != "AVAILABLE"
                or row.get("lane") != "ACCOUNT"
                or str(row.get("date")) != endpoint_date
            ):
                continue
            sid = str(row["strategy_id"])
            capital = Decimal(str(row["beginning_capital_usd"]))
            period_return = by_day.get(endpoint_date, {}).get(sid, Decimal(0))
            endpoint_val = str(capital * (Decimal(1) + period_return))
            matching_allocations.append(
                {
                    "as_of_date": endpoint_date,
                    "current_value": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": endpoint_val,
                    },
                    "scope": "SYSTEMATIC",
                    "strategy_id": sid,
                }
            )
    else:
        # No envelope — use default SYSTEMATIC allocations for perimeter.
        for index in range(9):
            matching_allocations.append(
                {
                    "as_of_date": "2026-07-30",
                    "current_value": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": str(1000 + index),
                    },
                    "scope": "SYSTEMATIC",
                    "strategy_id": f"strategy-{index}",
                }
            )

    catalog: dict[str, Any] = {
        "allocations": matching_allocations,
        "as_of_date": "2026-07-30",
        "baseline_schema_version": 1,
        "policy_version": "PORT-PERF-1",
        "source_artifacts": [],
        "strategy_returns": list(crosswalk or []),
    }
    if envelope is not None:
        catalog["hwm_inputs"] = envelope
        anchor = envelope.get("anchor", {})
        catalog["hwm_acceptance_record"] = {
            "anchor_date": anchor.get("anchor_date"),
            "index_base_value": anchor.get("index_base_value"),
            "hwm_policy_id": anchor.get("hwm_policy_id"),
        }
        catalog["source_artifacts"] = [
            {
                "artifact_id": "anchor-acceptance-1",
                "as_of_date": "2026-07-30",
                "schema_name": "hwm-anchor-acceptance",
                "schema_version": "1",
                "sha256": "a" * 64,
                "source_key": "hwm-anchor-acceptance",
            },
            {
                "artifact_id": "calendar-account-1",
                "as_of_date": "2026-07-30",
                "schema_name": "hwm-calendar-authority-account",
                "schema_version": "1",
                "sha256": "b" * 64,
                "source_key": "hwm-calendar-authority-account",
            },
            *(extra_source_artifacts or []),
        ]
    return catalog


def _publish_baseline(
    root: Path, source: Path, catalog: dict[str, Any]
) -> dict[str, str]:
    """Publish a governed baseline generation; returns the report-facing
    baseline identity (exact generation id + catalog hash)."""
    content = canonical_json_bytes(catalog)
    source.write_bytes(content)
    artifact = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        as_of_date=date(2026, 7, 30),
        schema_name="portfolio-baseline-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-baseline", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic control-panel test baseline",
            fact_types=("portfolio",),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-baseline.json").write_bytes(content)
    publish_generation(root, manifest.generation_id)
    return {
        "catalog_sha256": artifact.sha256,
        "generation_id": manifest.generation_id,
    }


def _lens_inputs(tmp_path: Path) -> tuple[Path, Path]:
    lens_root = tmp_path / "analytics" / "decomposition-lenses"
    assignments = {
        "europe-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[1]}),
        "multi-asset-style": (
            "ETF_STYLE",
            set(STRATEGY_NAMES[3:7]),
        ),
        "us-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[0]}),
        "us-equity-style": ("ETF_STYLE", {STRATEGY_NAMES[2]}),
        "us-industry": ("RETURN_INDUSTRY", {STRATEGY_NAMES[8]}),
        "us-sector": ("RETURN_SECTOR", {STRATEGY_NAMES[7]}),
    }
    for source_key, (analysis_lens, selected_names) in assignments.items():
        value = _decomposition()
        value["analysis_lens"] = analysis_lens
        value["model_selections"] = [
            row
            for row in value["model_selections"]
            if row["series_id"] in selected_names
        ]
        value["model_results"] = [
            row for row in value["model_results"] if row["series_id"] in selected_names
        ]
        _publish_analytics(
            lens_root / source_key,
            "decomposition.json",
            value,
            f"synthetic-{source_key}",
        )

    plan_bundle = tmp_path / ("a" * 64)
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    plan = {
        "schema_version": 1,
        "strategies": [
            {
                "default_disposition": (
                    "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                    if index == 7
                    else "OUT_OF_SCOPE_UNDECLARED_LENS"
                ),
                "series_id": name,
                "strategy_id": f"strategy-{index}",
            }
            for index, name in enumerate(STRATEGY_NAMES)
        ],
    }
    _write_json(plan_path, plan)
    _write_json(
        dispositions_path,
        {"candidate_dispositions": [], "policy": "synthetic", "schema_version": 1},
    )
    manifest = {
        "bundle_id": plan_bundle.name,
        "inputs": [
            {
                "path": "inputs/governance/current-strategy-decomposition-plan.json",
                "sha256": _sha(plan_path),
            }
        ],
        "outputs": [
            {
                "path": "candidate-dispositions.json",
                "sha256": _sha(dispositions_path),
            }
        ],
        "schema_version": 1,
    }
    _write_json(plan_bundle / "plan-manifest.json", manifest)
    return lens_root, plan_bundle


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    report_root = tmp_path / "reporting"
    source_report = tmp_path / "synthetic-portfolio-report-data.json"
    decomposition_root = tmp_path / "analytics" / "decomposition"
    correlation_root = tmp_path / "analytics" / "correlation"
    _publish_analytics(
        decomposition_root,
        "decomposition.json",
        _decomposition(),
        "synthetic-decomposition",
    )
    _publish_analytics(
        correlation_root,
        "correlation.json",
        _correlation(),
        "synthetic-correlation",
    )
    legacy = tmp_path / "legacy"
    source_content = {
        "index.html": b"synthetic legacy index",
        "styles.css": b":root { --accent: #5cc8c0; }",
        "app.js": b"synthetic legacy app",
        "control_panel_validation.json": b'{"status":"PASS"}',
    }
    for relative, content in source_content.items():
        path = legacy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    providers = [
        {
            "candidate": "CAOS" if index == 0 else f"Candidate {index}",
            "cadence": "daily",
            "category": "synthetic",
            "execution": "fund",
            "philosophy_module": "synthetic",
            "portfolio_role": "synthetic research",
            "principal_gap": "synthetic gap",
            "ticker": "CAOS" if index == 0 else f"S{index}",
            "tier": "Tier 1" if index < 5 else f"Tier {(index % 3) + 2}",
        }
        for index in range(33)
    ]
    _write_json(legacy / "data" / "dashboard_data.json", {"providers": providers})
    contract = {
        "contract_version": 1,
        "frozen_appendix": {
            "as_of_date": "2026-07-17",
            "candidate_count": 33,
            "data_file": "data/dashboard_data.json",
            "data_sha256": _sha(legacy / "data" / "dashboard_data.json"),
            "status": "FROZEN_AUDIT_APPENDIX",
        },
        "source_files": {
            relative: _sha(legacy / relative) for relative in source_content
        },
    }
    contract_path = tmp_path / "source-contract.json"
    contract_path.write_bytes(canonical_json_bytes(contract))
    monkeypatch.setattr(panel, "SOURCE_CONTRACT", contract_path)
    cluster_contract_path = tmp_path / "economic-driver-clusters.json"
    _write_json(
        cluster_contract_path,
        {
            "as_of_date": "2026-07-30",
            "budget_rule": "ONE_AGGREGATE_ADVERSE_LOSS_BUDGET_PER_ECONOMIC_DRIVER_CLUSTER_NO_STRATEGY_LABEL_DIVERSIFICATION_CREDIT",
            "clusters": [
                {
                    "cluster_id": "SYNTHETIC_CLUSTER",
                    "label": "Synthetic shared driver",
                    "members": list(STRATEGY_NAMES),
                    "rationale": "Synthetic cluster used to verify aggregate budget rendering.",
                }
            ],
            "historical_audit_reference": {
                "classification": "SYNTHETIC_REFERENCE",
                "full_systematic_loss": "0.3725",
                "after_rejected_sleeves_loss": "0.2715",
                "note": "Synthetic test reference.",
            },
            "schema_version": 1,
            "systematic_loss_limit": "0.15",
        },
    )
    monkeypatch.setattr(panel, "ECONOMIC_DRIVER_CLUSTERS", cluster_contract_path)
    freshness = tmp_path / "freshness-health.json"
    _write_json(
        freshness,
        {
            "assessment_schema_version": 2,
            "as_of_date": "2026-07-30",
            "assessments": [
                {
                    "acquired_at": "2026-07-30T12:00:00+00:00",
                    "blocks_current_consumers": False,
                    "escalation_required": False,
                    "evaluated_on": "2026-07-30",
                    "fact_type": "strategy-returns",
                    "last_accepted_date": "2026-07-30",
                    "next_expected_date": "2026-07-31",
                    "policy_id": "daily-current",
                    "provider_id": "synthetic",
                    "reason": "accepted observation is within cadence and grace",
                    "revision_state": "ORIGINAL",
                    "state": "FRESH",
                    "subject_id": f"strategy-{index}",
                }
                for index in range(9)
            ],
            "policy": {
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "daily-current",
                "stale_after_days": 5,
            },
        },
    )
    # The governed baseline generation is the panel's reconciliation
    # authority for the envelope, crosswalk, and perimeter; the accepted
    # report binds to it by exact generation id and catalog hash, and
    # carries the governed HWM summary the panel recomputes.
    baseline_root = tmp_path / "baseline"
    baseline_catalog = _panel_baseline_catalog()
    baseline_identity = _publish_baseline(
        baseline_root, tmp_path / "baseline-catalog.json", baseline_catalog
    )
    report = _report()
    report["baseline"] = baseline_identity
    _with_hwm_summary(report, freshness, baseline_catalog)
    _publish_report(report_root, source_report, report)
    return {
        "baseline": baseline_root,
        "baseline_identity": baseline_identity,
        "correlation": correlation_root,
        "decomposition": decomposition_root,
        "freshness": freshness,
        "legacy": legacy,
        "output": tmp_path / "control-panel",
        "report": report_root,
    }


def _qa(candidate: Path, root: Path) -> Path:
    qa = root / "qa"
    for name in panel.CAPTURES:
        folder = qa / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "screenshot.png").write_bytes(b"synthetic-png-evidence")
        _write_json(folder / "console-events.json", [])
        _write_json(folder / "request-failures.json", [])
        _write_json(
            folder / "manifest.json",
            {"target": f"file:///{candidate.name}/index.html#{name}"},
        )
    for name in panel.LINT_CAPTURES:
        _write_json(
            qa / name / "visual-lint.json", {"finding_count": 0, "findings": []}
        )
    (qa / "desktop-command" / "page.pdf").write_bytes(b"synthetic-pdf-evidence")
    checks = [
        {"check": f"synthetic interaction {index}", "status": "PASS"}
        for index in range(6)
    ]
    _write_json(
        qa / "interaction-results.json",
        {"all_passed": True, "checks": checks, "panel_id": candidate.name},
    )
    return qa


def test_build_is_deterministic_and_verifies(inputs: dict[str, Path]) -> None:
    first = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    second = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert first["panel_id"] == second["panel_id"]
    assert (
        Path(first["candidate_dir"]) / "data" / "freshness-health.json"
    ).read_bytes() == inputs["freshness"].read_bytes()
    result = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        Path(first["candidate_dir"]),
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert result["all_passed"] is True
    assert result["checks_passed"] == result["checks_total"]
    data = json.loads(
        (Path(first["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["panel_schema_version"] == panel.PANEL_SCHEMA_VERSION
    assert len(data["account_summary"]) == 19
    assert len({row["account_id"] for row in data["account_summary"]}) == 19
    assert {
        row["account_id"] for row in [*data["performance"], *data["positions"]]
    } <= {row["account_id"] for row in data["account_summary"]}
    assert data["meta"]["freshness_evaluated_on"] == "2026-07-30"
    assert data["meta"]["hwm_status_code_version"] == 2
    assert data["meta"]["hwm_status_semantics"] == "EXPLICIT_GOVERNED_HWM_PERIMETER"
    assert data["advisory"]["hwm_status_code_version"] == 2
    assert data["advisory"]["hwm_status_semantics"] == "EXPLICIT_GOVERNED_HWM_PERIMETER"
    assert data["positions"] == [
        {
            "account_id": "account-00",
            "as_of_date": "2026-07-30",
            "evidence_class": "WCH_CURRENT",
            "market_value_usd": "30",
            "quantity": "3",
            "security_id": "security-synthetic",
            "status": "AVAILABLE_MAPPED",
            "strategy_id": "strategy-0",
            "strategy_name": STRATEGY_NAMES[0],
            "symbol": "SYNTH",
        }
    ]
    assert data["factor_analysis"]["available_count"] == 4
    assert data["factor_analysis"]["diagnostic_count"] == 1
    assert data["factor_analysis"]["dynamic_available_count"] == 1
    assert data["factor_analysis"]["unavailable_count"] == 4
    assert data["dependence_analysis"]["available_pair_count"] == 10
    assert data["dependence_analysis"]["pair_count"] == 36
    assert data["dependence_analysis"]["central_case"]["loss"] == "0.2"
    assert data["advisory"]["central_case_loss"] == "0.2"
    assert data["advisory"]["readiness_loss_measure"] == (
        "CORRELATION_ONE_CONSERVATIVE_BOUND"
    )
    assert "central case is descriptive" in data["advisory"]["reason"]
    cluster_budget = data["dependence_analysis"]["economic_driver_cluster_budget"]
    assert cluster_budget["central_case_loss"] == "0.2"
    assert cluster_budget["governing_loss_measure"] == (
        "CORRELATION_ONE_CONSERVATIVE_BOUND"
    )
    assert cluster_budget["systematic_loss_limit"] == "0.15"
    assert cluster_budget["status"] == "BREACHES_SYSTEMATIC_LOSS_LIMIT"
    assert float(cluster_budget["limit_breach"]) == pytest.approx(0.22249726899852387)
    assert cluster_budget["clusters"][0]["members"] == list(STRATEGY_NAMES)
    assert float(cluster_budget["clusters"][0]["loss_contribution"]) == (
        pytest.approx(0.37249726899852387)
    )
    assert data["default_dependence_mode"] == "LIVE_ONLY"
    assert list(data["dependence_modes"]) == ["LIVE_ONLY"]
    assert data["hybrid_mode_gate"]["status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )
    assert len(data["hybrid_mode_gate"]["blocked_strategies"]) == 9
    assert data["hybrid_mode_gate"]["eligible_strategies"] == []
    underwriting = data["strategy_underwriting"]
    assert underwriting["policy_id"] == "PORT-UNDERWRITE-1"
    assert underwriting["counts"]["strategies"] == 9
    assert underwriting["authority_boundary"] == {
        "allocation_authority": "NONE",
        "automatic_drop_authority": "NONE",
        "execution_authority": "NONE",
        "readiness_gate_effect": "NONE",
        "use": "ADVISORY_UNDERWRITING_AND_EVIDENCE_PRIORITIZATION",
    }
    assert all(
        set(row["evidence_layers"])
        == {
            "account_live",
            "backtest",
            "design",
            "implementation_reconciliation",
            "provider_or_model_live",
        }
        for row in underwriting["rows"]
    )


def test_underwriting_gap_signatures_are_diagnostic_not_fabricated() -> None:
    policy = json.loads(
        Path("contracts/portfolio-strategy-underwriting-v1.json").read_text(
            encoding="utf-8"
        )
    )["model_account_gap"]
    aligned = panel._model_account_gap_underwriting(
        {
            "strategy_name": "TT Portfolio",
            "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "provider_cumulative_gap": "-0.05124933645",
            "provider_reference_cumulative_return": "0.049011",
            "provider_account_cumulative_return": "-0.002239",
            "provider_beta": "0.9713",
            "provider_correlation": "0.9847",
            "provider_tracking_error_annualized": "0.0297",
            "provider_comparison_version_compatibility": "UNRESOLVED",
            "cash_drag_status": "UNAVAILABLE",
            "fees_status": "PARTIAL",
        },
        policy,
    )
    divergent = panel._model_account_gap_underwriting(
        {
            "strategy_name": "AI Factor [P123]",
            "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "provider_cumulative_gap": "-0.168283912268",
            "provider_reference_cumulative_return": "0.175664",
            "provider_account_cumulative_return": "0.00738",
            "provider_beta": "0.9864",
            "provider_correlation": "0.8293",
            "provider_tracking_error_annualized": "0.11785",
            "provider_comparison_version_compatibility": "UNRESOLVED",
            "cash_drag_status": "UNAVAILABLE",
            "fees_status": "PARTIAL",
        },
        policy,
    )

    assert aligned["signature"] == "PATH_ALIGNED_PERSISTENT_LEVEL_DIFFERENCE"
    assert divergent["signature"] == "PARTIAL_PATH_DIVERGENCE"
    for result in (aligned, divergent):
        assert result["causally_bound_components"] == []
        assert result["unresolved_residual"] == result["total_gap"]
        assert result["reconciliation_error"] == "0"
        assert (
            "COMPLETE_DAILY_ACCOUNT_CASH_PATH_UNAVAILABLE"
            in result["unresolved_causes"]
        )


def test_renderer_exposes_underwriting_and_all_orphaned_evidence_sections() -> None:
    renderer = Path("apps/portfolio-control-panel/app.js").read_text(encoding="utf-8")
    template = Path("apps/portfolio-control-panel/index.html").read_text(
        encoding="utf-8"
    )

    for payload_key in (
        "fact_coverage_matrix",
        "attribution_dispositions",
        "observation_windows",
        "selection_boundary",
    ):
        assert payload_key in renderer
    assert "data.strategy_underwriting" in renderer
    assert "causally_bound_components" in renderer
    assert "return_attribution" in renderer
    assert "Causal return waterfall" in renderer
    assert "Correct connected-model comparator" in renderer
    assert "NO_AUTOMATIC_DROP_AUTHORITY" not in renderer
    for element_id in (
        "underwriting-table",
        "fact-coverage-matrix-table",
        "observation-windows-table",
        "attribution-dispositions-table",
    ):
        assert f'id="{element_id}"' in template


def test_control_panel_keeps_factor_style_sector_and_industry_lenses_separate(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lens_root, plan_bundle = _lens_inputs(tmp_path)
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        candidate,
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert verification["all_passed"] is True
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    lenses = {row["lens_id"]: row for row in data["decomposition_lenses"]["lenses"]}
    assert set(lenses) == {
        "DYNAMIC_STYLE",
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    }
    assert all(len(row["series"]) == 9 for row in lenses.values())
    robuxio = next(
        row
        for row in lenses["ECONOMIC_FACTOR"]["series"]
        if row["series_id"] == STRATEGY_NAMES[7]
    )
    assert robuxio["unavailable_reasons"] == [
        "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
    ]
    assert lenses["ECONOMIC_FACTOR"]["return_basis"] == (
        "EXCESS_AND_LONG_SHORT_FACTOR_RETURN"
    )
    assert lenses["ETF_STYLE"]["return_basis"] == "TOTAL_RETURN_ETF_PROXY"
    assert lenses["DYNAMIC_STYLE"]["evidence_classification"] == (
        "MPI_INFORMED_NON_EQUIVALENT"
    )
    assert lenses["DYNAMIC_STYLE"]["dynamic_available_count"] == 1


def _current_surface_lens_inputs(base: Path) -> tuple[Path, Path]:
    """Schema-2 (0.17) plan bundle + lens generations in the new
    <root>/<evidence_mode>/<surface>/<source_key> release layout.

    `base` MUST be a short path (see caller): the deepest file lands near the
    260-char Windows limit once <64-hex generation ids are appended."""
    lens_root = base / "l" / "decomposition-lenses"
    assignments = {
        "europe-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[1]}),
        "us-economic": ("ECONOMIC_FACTOR", {STRATEGY_NAMES[0]}),
        "us-equity-style": ("ETF_STYLE", {STRATEGY_NAMES[2]}),
        "us-industry": ("RETURN_INDUSTRY", {STRATEGY_NAMES[8]}),
        "us-sector": ("RETURN_SECTOR", {STRATEGY_NAMES[7]}),
    }
    for source_key, (analysis_lens, selected_names) in assignments.items():
        value = _decomposition()
        value["analysis_lens"] = analysis_lens
        value["model_selections"] = [
            row
            for row in value["model_selections"]
            if row["series_id"] in selected_names
        ]
        value["model_results"] = [
            row for row in value["model_results"] if row["series_id"] in selected_names
        ]
        for surface in ("STATIC", "DYNAMIC_FLS"):
            _publish_analytics(
                lens_root / "LIVE_ONLY" / surface / source_key,
                "decomposition.json",
                value,
                f"synthetic-{source_key}-{surface.lower()}",
            )

    plan_bundle = base / ("a" * 64)
    plan_path = (
        plan_bundle
        / "inputs"
        / "governance"
        / "current-strategy-decomposition-plan.json"
    )
    dispositions_path = plan_bundle / "candidate-dispositions.json"
    plan = {
        "schema_version": 3,
        "strategies": [
            {
                "series_id": name,
                "source_dispositions": {
                    source_key: {
                        "authority": "SYNTHETIC_GOVERNED_PLAN",
                        "readiness": (
                            "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                            if index == 7 and source_key == "us-sector"
                            else "APPLICABLE_SYNTHETIC_GOVERNED_ASSIGNMENT"
                        ),
                        "reason": (
                            "UNAVAILABLE_MISSING_ACCEPTED_CRYPTO_CANDIDATE"
                            if index == 7 and source_key == "us-sector"
                            else "OUT_OF_SCOPE_UNDECLARED_LENS"
                        ),
                        "requirement": "REQUIRED",
                    }
                    for source_key in assignments
                },
                "strategy_id": f"strategy-{index}",
            }
            for index, name in enumerate(STRATEGY_NAMES)
        ],
    }
    _write_json(plan_path, plan)
    _write_json(
        dispositions_path,
        {"candidate_dispositions": [], "policy": "synthetic", "schema_version": 1},
    )
    manifest = {
        "assurance_profile": "DEVELOPMENT",
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": "0.17.0",
        "bundle_id": plan_bundle.name,
        "evaluation_date": "2026-08-03",
        "inputs": [
            {
                "path": "inputs/governance/current-strategy-decomposition-plan.json",
                "sha256": _sha(plan_path),
            }
        ],
        "outputs": [
            {
                "path": "candidate-dispositions.json",
                "sha256": _sha(dispositions_path),
            }
        ],
        "schema_version": 2,
    }
    _write_json(plan_bundle / "plan-manifest.json", manifest)
    return lens_root, plan_bundle


def test_panel_consumes_current_surface_lens_generations(
    inputs: dict[str, Path], tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """P1 regression for PR #363 review: a schema-2 current-surface bundle with
    generations under LIVE_ONLY/<surface>/<source_key> must build a panel, and a
    legacy layout must still be rejected as ambiguous rather than silently read."""
    # Short base: the deepest fixture path sits near the 260-char Windows limit.
    base = tmp_path_factory.getbasetemp() / "s2lens"
    lens_root, plan_bundle = _current_surface_lens_inputs(base)
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(result["candidate_dir"])
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    lenses = {row["lens_id"]: row for row in data["decomposition_lenses"]["lenses"]}
    assert set(lenses) >= {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    }
    assert all(len(row["series"]) == 9 for row in lenses.values())
    economic = lenses["ECONOMIC_FACTOR"]
    us_economic_rows = [
        row for row in economic["series"] if row["series_id"] == STRATEGY_NAMES[0]
    ]
    assert len(us_economic_rows) == 1
    assert us_economic_rows[0]["status"] == "AVAILABLE"

    # A schema-2 bundle over a legacy FLAT lens tree must be rejected (the
    # generations are not where the current-plan contract requires them);
    # legacy bundles with flat trees remain supported (see the separate-lenses
    # regression above) and must not silently read surface-layout roots.
    legacy_base = tmp_path_factory.getbasetemp() / "s2legacy"
    legacy_tree_root, _ = _lens_inputs(legacy_base)
    with pytest.raises(panel.ControlPanelValidationError, match="LIVE_ONLY"):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "legacy-output",
            decomposition_lenses_root=legacy_tree_root,
            decomposition_plan_bundle=plan_bundle,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_layered_dependence_modes_are_preserved_without_changing_default_authority(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    compatible_report = _report()
    compatible_report["baseline"] = inputs["baseline_identity"]
    for strategy in compatible_report["strategy_summary"]:
        strategy.update(
            {
                "live_plus_backtest_reason": (
                    "Effective-dated registry marks the current strategy version PROVEN_COMPATIBLE."
                ),
                "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
                "version_compatibility_reason": "Synthetic compatibility is proven.",
                "version_compatibility_status": "PROVEN_COMPATIBLE",
                "version_ref": f"synthetic-version:{strategy['strategy_id']}",
            }
        )
    report_root = tmp_path / "compatible-reporting"
    _publish_report(
        report_root,
        tmp_path / "compatible-portfolio-report-data.json",
        _with_hwm_summary(
            compatible_report, inputs["freshness"], _panel_baseline_catalog()
        ),
    )
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        _correlation_v2(),
        "synthetic-correlation-v2",
    )
    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["default_dependence_mode"] == "LIVE_ONLY"
    assert data["dependence_analysis"] == data["dependence_modes"]["LIVE_ONLY"]
    assert set(data["dependence_modes"]) == {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}
    assert (
        data["dependence_modes"]["LIVE_PLUS_BACKTEST"]["series_composition"][
            STRATEGY_NAMES[0]
        ]["backtest_observations_used"]
        == 60
    )
    assert data["advisory"]["correlation_one_stress"] == "0.37249726899852387"
    assert data["hybrid_mode_gate"]["status"] == "ELIGIBLE_PROVEN_COMPATIBLE"
    assert len(data["hybrid_mode_gate"]["eligible_strategies"]) == 9


def test_hybrid_analytics_are_enabled_independently_per_compatible_strategy(
    inputs: dict[str, Path],
    tmp_path: Path,
) -> None:
    partially_compatible_report = _report()
    partially_compatible_report["baseline"] = inputs["baseline_identity"]
    eligible = partially_compatible_report["strategy_summary"][0]
    eligible.update(
        {
            "live_plus_backtest_reason": (
                "Effective-dated registry proves the current strategy version compatible."
            ),
            "live_plus_backtest_status": "ELIGIBLE_PROVEN_COMPATIBLE",
            "version_compatibility_reason": "Synthetic compatibility is proven.",
            "version_compatibility_status": "PROVEN_COMPATIBLE",
            "version_ref": "synthetic-version:strategy-0",
        }
    )
    report_root = tmp_path / "partially-compatible-reporting"
    _publish_report(
        report_root,
        tmp_path / "partially-compatible-portfolio-report-data.json",
        _with_hwm_summary(
            partially_compatible_report, inputs["freshness"], _panel_baseline_catalog()
        ),
    )
    correlation = _correlation_v2()
    hybrid_composition = correlation["dependence_modes"]["LIVE_PLUS_BACKTEST"][
        "series_composition"
    ]
    for strategy_name in STRATEGY_NAMES[1:]:
        row = hybrid_composition[strategy_name]
        row["backtest_observations_used"] = 0
        row["observed_start"] = row["live_inception"]
        row["status"] = "AVAILABLE_LIVE_ONLY_NO_COMPATIBLE_BACKTEST"
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2-partial",
    )

    result = panel.build_candidate(
        report_root,
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        report_root,
        inputs["legacy"],
        candidate,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert verification["all_passed"] is True
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    gate = data["hybrid_mode_gate"]
    assert gate["status"] == "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY"
    assert [row["strategy_name"] for row in gate["eligible_strategies"]] == [
        STRATEGY_NAMES[0]
    ]
    assert len(gate["blocked_strategies"]) == 8
    assert set(data["dependence_modes"]) == {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}
    assert data["weekly_returns"]["hybrid_mode_status"] == (
        "UNAVAILABLE_NOT_CONFIGURED"
    )
    governed_weekly = panel._govern_weekly_hybrid_modes(
        {
            "generation_id": "a" * 64,
            "series": [
                {
                    "calendar_basis_by_mode": {
                        "LIVE_ONLY": "NYSE",
                        "LIVE_PLUS_BACKTEST": "NYSE",
                    },
                    "display_name": strategy_name,
                    "mode_coverage": {
                        "LIVE_ONLY": {
                            "strategy_version": f"synthetic-version:strategy-{index}"
                        },
                        "LIVE_PLUS_BACKTEST": {
                            "strategy_version": f"synthetic-version:strategy-{index}"
                        },
                    },
                    "observation_meta_by_mode": {
                        "LIVE_ONLY": {"2026-01-05": [5]},
                        "LIVE_PLUS_BACKTEST": {"2026-01-05": [5]},
                    },
                    "primary_group": "STRATEGY",
                    "series_id": strategy_name,
                    "values_by_mode": {
                        "LIVE_ONLY": {"2026-01-05": "0.01"},
                        "LIVE_PLUS_BACKTEST": {"2026-01-05": "0.01"},
                    },
                }
                for index, strategy_name in enumerate(STRATEGY_NAMES)
            ],
            "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
        },
        gate,
    )
    assert governed_weekly["strategy_evidence_modes"] == [
        "LIVE_ONLY",
        "LIVE_PLUS_BACKTEST",
    ]
    weekly_by_id = {
        row["series_id"]: row
        for row in governed_weekly["series"]
        if row["primary_group"] == "STRATEGY"
    }
    assert "LIVE_PLUS_BACKTEST" in weekly_by_id[STRATEGY_NAMES[0]]["values_by_mode"]
    assert list(weekly_by_id[STRATEGY_NAMES[1]]["values_by_mode"]) == ["LIVE_ONLY"]
    assert list(weekly_by_id[STRATEGY_NAMES[1]]["mode_coverage"]) == ["LIVE_ONLY"]


def test_weekly_hybrid_rejects_a_registry_version_mismatch() -> None:
    gate = {
        "blocked_strategies": [],
        "eligible_strategies": [
            {
                "compatibility_status": "PROVEN_COMPATIBLE",
                "reason": "Synthetic compatibility is proven.",
                "strategy_id": "strategy-0",
                "strategy_name": STRATEGY_NAMES[0],
                "version_ref": "registry-version",
            }
        ],
        "reason": "Synthetic compatibility is proven.",
        "status": "ELIGIBLE_PROVEN_COMPATIBLE",
    }
    weekly = {
        "generation_id": "a" * 64,
        "series": [
            {
                "calendar_basis_by_mode": {
                    "LIVE_ONLY": "NYSE",
                    "LIVE_PLUS_BACKTEST": "NYSE",
                },
                "display_name": STRATEGY_NAMES[0],
                "mode_coverage": {
                    "LIVE_ONLY": {"strategy_version": "registry-version"},
                    "LIVE_PLUS_BACKTEST": {"strategy_version": "different-version"},
                },
                "observation_meta_by_mode": {
                    "LIVE_ONLY": {"2026-01-05": [5]},
                    "LIVE_PLUS_BACKTEST": {"2026-01-05": [5]},
                },
                "primary_group": "STRATEGY",
                "series_id": f"strategy:{STRATEGY_NAMES[0]}",
                "values_by_mode": {
                    "LIVE_ONLY": {"2026-01-05": "0.01"},
                    "LIVE_PLUS_BACKTEST": {"2026-01-05": "0.01"},
                },
            }
        ],
        "strategy_evidence_modes": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"],
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="eligible weekly hybrid version differs from registry",
    ):
        panel._govern_weekly_hybrid_modes(weekly, gate)


def test_unresolved_versions_reject_hybrid_backtest_observations(
    inputs: dict[str, Path],
) -> None:
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        _correlation_v2(),
        "synthetic-correlation-v2",
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="unresolved strategy uses backtest history in hybrid dependence",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_unresolved_versions_retain_zero_backtest_hybrid_as_live_only_equivalent(
    inputs: dict[str, Path],
) -> None:
    correlation = _correlation_v2()
    for row in correlation["dependence_modes"]["LIVE_PLUS_BACKTEST"][
        "series_composition"
    ].values():
        row["backtest_observations_used"] = 0
        row["observed_start"] = row["live_inception"]
        row["status"] = "AVAILABLE_LIVE_ONLY_NO_COMPATIBLE_BACKTEST"
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        correlation,
        "synthetic-correlation-v2-zero-backtest",
    )
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(result["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(data["dependence_modes"]) == ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]
    assert data["weekly_returns"]["strategy_evidence_modes"] == ["LIVE_ONLY"]
    assert data["hybrid_mode_gate"]["status"] == (
        "BLOCKED_VERSION_NOT_PROVEN_COMPATIBLE"
    )
    assert len(data["hybrid_mode_gate"]["blocked_strategies"]) == 9
    assert data["hybrid_mode_gate"]["eligible_strategies"] == []


def test_template_line_endings_do_not_change_identity(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    lf_template = tmp_path / "template-lf"
    crlf_template = tmp_path / "template-crlf"
    for name in ("index.html", "analytics.css", "app.js"):
        normalized = (
            (panel.DEFAULT_TEMPLATE_ROOT / name)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        lf_template.mkdir(parents=True, exist_ok=True)
        crlf_template.mkdir(parents=True, exist_ok=True)
        (lf_template / name).write_bytes(normalized.encode("utf-8"))
        (crlf_template / name).write_bytes(
            normalized.replace("\n", "\r\n").encode("utf-8")
        )
    lf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "lf-output",
        lf_template,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    crlf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "crlf-output",
        crlf_template,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    assert lf["panel_id"] == crlf["panel_id"]


def test_provider_evidence_overlay_preserves_separate_account_and_provider_dates(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    evidence = tmp_path / "strategy-evidence.json"
    _write_json(
        evidence,
        {
            "as_of_date": "2026-07-31",
            "schema_version": 1,
            "strategies": [
                {
                    "account": {
                        "observed_start": "2026-01-22",
                        "observed_end": "2026-07-14",
                        "return_observations": 124,
                    },
                    "current_snapshot": {"observed_date": "2026-07-27"},
                    "evidence_layer": "PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE",
                    "generation_id": "e" * 64,
                    "generation_as_of_date": "2026-07-31",
                    "overlap": {
                        "account_minus_provider_cumulative": "-0.1642",
                        "annualized_tracking_error": "0.1177",
                        "daily_return_correlation": "0.8267",
                        "status": "TESTED_EXACT_DATE_PATHS",
                    },
                    "provider": {
                        "observed_start": "2025-05-12",
                        "observed_end": "2026-07-24",
                        "return_observations": 315,
                    },
                    "provider_id": "portfolio123",
                    "sources": {},
                    "strategy_id": "strategy-0",
                    "strategy_name": STRATEGY_NAMES[0],
                }
            ],
        },
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        strategy_evidence_path=evidence,
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    attached = data["strategies"][0]["provider_evidence"]
    assert attached["account"]["observed_end"] == "2026-07-14"
    assert attached["provider"]["observed_end"] == "2026-07-24"
    assert data["meta"]["strategy_evidence_as_of"] == "2026-07-31"
    assert any(
        row["artifact"] == "current-strategy-provider-evidence"
        for row in data["provenance"]
    )


def test_source_drift_fails_closed(inputs: dict[str, Path]) -> None:
    (inputs["legacy"] / "styles.css").write_text("changed", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="source hash differs"):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_governed_panel_rollforward_reuses_verified_frozen_appendix(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    contract = json.loads(panel.SOURCE_CONTRACT.read_text(encoding="utf-8"))
    raw = json.loads(
        (inputs["legacy"] / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    candidate_appendix = {
        "as_of_date": contract["frozen_appendix"]["as_of_date"],
        "providers": raw["providers"],
        "source_sha256": contract["frozen_appendix"]["data_sha256"],
        "status": contract["frozen_appendix"]["status"],
        "tier_counts": {},
    }
    contract["frozen_appendix"]["normalized_sha256"] = hashlib.sha256(
        canonical_json_bytes(candidate_appendix)
    ).hexdigest()
    panel.SOURCE_CONTRACT.write_bytes(canonical_json_bytes(contract))

    rolled = tmp_path / "rolled-panel"
    payload_path = rolled / "data" / "dashboard_data.json"
    _write_json(
        payload_path,
        {
            "candidate_appendix": candidate_appendix,
            "meta": {"source_shell_hashes": contract["source_files"]},
        },
    )
    _write_json(
        rolled / "candidate.json",
        {"artifact_sha256": {"data/dashboard_data.json": _sha(payload_path)}},
    )

    accepted, source = panel._source_inputs(rolled)

    assert accepted == candidate_appendix
    assert source["source_mode"] == "GOVERNED_PANEL_ROLLFORWARD"
    assert source["observed_hashes"] == contract["source_files"]


def test_freshness_health_is_visible_with_dates_and_reason(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    health = tmp_path / "freshness-health.json"
    _write_json(
        health,
        {
            "assessment_schema_version": 2,
            "as_of_date": "2026-07-30",
            "assessments": [
                {
                    "acquired_at": "2026-07-30T12:00:00+00:00",
                    "blocks_current_consumers": False,
                    "escalation_required": True,
                    "evaluated_on": "2026-07-30",
                    "fact_type": "nav",
                    "last_accepted_date": "2026-07-28",
                    "next_expected_date": "2026-07-29",
                    "policy_id": "daily-current",
                    "provider_id": "synthetic",
                    "reason": "expected refresh is late but not yet stale",
                    "revision_state": "REVISED",
                    "state": "LATE",
                    "subject_id": "account-internal-1",
                },
                *[
                    {
                        "acquired_at": "2026-07-30T12:00:00+00:00",
                        "blocks_current_consumers": False,
                        "escalation_required": False,
                        "evaluated_on": "2026-07-30",
                        "fact_type": "strategy-returns",
                        "last_accepted_date": "2026-07-30",
                        "next_expected_date": "2026-07-31",
                        "policy_id": "daily-current",
                        "provider_id": "synthetic",
                        "reason": "accepted observation is within cadence and grace",
                        "revision_state": "ORIGINAL",
                        "state": "FRESH",
                        "subject_id": f"strategy-{index}",
                    }
                    for index in range(9)
                ],
            ],
            "policy": {
                "expected_interval_days": 1,
                "grace_days": 0,
                "mode": "RECURRING",
                "policy_id": "daily-current",
                "stale_after_days": 5,
            },
        },
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=health,
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )
    freshness = [
        row
        for row in data["coverage"]
        if row.get("item") == "synthetic:account-internal-1:nav"
    ]

    assert freshness == [
        {
            "acquired_at": "2026-07-30T12:00:00+00:00",
            "blocks_current_consumers": False,
            "category": "DATA_FRESHNESS",
            "escalation_required": True,
            "evaluated_on": "2026-07-30",
            "fact_type": "nav",
            "expected_interval_days": 1,
            "freshness_calendar": "CALENDAR_DAY",
            "freshness_mode": "RECURRING",
            "grace_days": 0,
            "item": "synthetic:account-internal-1:nav",
            "known_gap": (
                "next expected 2026-07-29; expected refresh is late but not yet stale"
            ),
            "last_accepted_date": "2026-07-28",
            "next_expected_date": "2026-07-29",
            "policy_id": "daily-current",
            "provider_id": "synthetic",
            "reconciled_on": "2026-07-30",
            "reason": "expected refresh is late but not yet stale",
            "revision_state": "REVISED",
            "stale_after_days": 5,
            "status": "LATE",
            "subject_id": "account-internal-1",
            "value": "2026-07-28",
        }
    ]
    assert freshness[0] in data["blocking_items"]


def test_schema_v3_nonrequired_stale_lane_does_not_block_panel(
    tmp_path: Path,
) -> None:
    policy = {
        "calendar": "CALENDAR_DAY",
        "expected_interval_days": 1,
        "grace_days": 2,
        "mode": "RECURRING",
        "policy_id": "daily-calendar-24x7",
        "stale_after_days": 5,
    }
    computed = (
        panel.FreshnessPolicy.from_dict(policy)
        .assess(date(2026, 7, 20), date(2026, 8, 3))
        .to_dict()
    )
    computed["blocks_current_consumers"] = False
    computed["escalation_required"] = False
    health = tmp_path / "freshness-v3.json"
    _write_json(
        health,
        {
            "assessment_schema_version": 3,
            "as_of_date": "2026-08-03",
            "assessments": [
                {
                    **computed,
                    "acquired_at": None,
                    "coverage_status": "AVAILABLE",
                    "fact_type": "strategy-returns",
                    "lane": "PROVIDER",
                    "last_accepted_date": "2026-07-20",
                    "provider_id": "synthetic",
                    "required_for_current": False,
                    "revision_state": "ORIGINAL",
                    "subject_id": "strategy-provider-only",
                    "subject_type": "STRATEGY",
                }
            ],
            "policies": [policy],
            "provider_ledger": {
                "as_of_date": "2026-08-03",
                "catalog_sha256": "a" * 64,
                "catalog_size_bytes": 100,
                "generation_id": "b" * 64,
                "manifest_sha256": "c" * 64,
                "manifest_size_bytes": 200,
            },
        },
    )

    rows = panel._freshness_health(health, "2026-08-03")

    assert rows[0]["status"] == "STALE"
    assert rows[0]["blocks_current_consumers"] is False
    assert rows[0]["escalation_required"] is False
    assert rows[0]["freshness_mode"] == "RECURRING"
    assert rows[0]["freshness_calendar"] == "CALENDAR_DAY"
    assert rows[0]["expected_interval_days"] == 1
    assert rows[0]["grace_days"] == 2
    assert rows[0]["stale_after_days"] == 5
    assert rows[0]["reconciled_on"] == "2026-08-03"

    replay = json.loads(health.read_text(encoding="utf-8"))
    replay["provider_ledger"]["as_of_date"] = "2026-08-04"
    invalid = tmp_path / "freshness-v3-future-ledger.json"
    _write_json(invalid, replay)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="ledger postdates the evaluation",
    ):
        panel._freshness_health(invalid, "2026-08-03")


def test_current_panel_fails_closed_without_freshness(inputs: dict[str, Path]) -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="freshness health is mandatory"
    ):
        panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])


def test_current_panel_fails_closed_without_fact_coverage_matrix(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    report = _report()
    report["baseline"] = inputs["baseline_identity"]
    report["fact_coverage_matrix"] = []
    report_root = tmp_path / "reporting-without-fact-matrix"
    _publish_report(report_root, tmp_path / "report-without-fact-matrix.json", report)

    with pytest.raises(
        panel.ControlPanelValidationError, match="fact coverage matrix is unavailable"
    ):
        panel.build_candidate(
            report_root,
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_stale_governed_strategy_blocks_hwm_claim(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    payload = json.loads(inputs["freshness"].read_text(encoding="utf-8"))
    payload["assessments"][0].update(
        {
            "blocks_current_consumers": True,
            "escalation_required": True,
            "last_accepted_date": "2026-07-17",
            "next_expected_date": "2026-07-18",
            "reason": "accepted observation exceeded the stale boundary",
            "state": "STALE",
        }
    )
    _write_json(inputs["freshness"], payload)

    # The panel reconciles the report's HWM summary unconditionally, so the
    # accepted report must be republished against the mutated freshness.
    _republish_report(
        inputs["report"],
        tmp_path,
        _with_hwm_summary(
            _report_with_baseline(inputs),
            inputs["freshness"],
            _panel_baseline_catalog(),
        ),
    )

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = json.loads(
        (Path(built["candidate_dir"]) / "data" / "dashboard_data.json").read_text(
            encoding="utf-8"
        )
    )

    assert data["hwm_summary"]["status"] == (
        "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV"
    )
    assert Decimal(data["hwm_summary"]["nav_coverage"]) < Decimal("1")
    assert data["strategies"][0]["hwm_freshness_status"] == "BLOCKED"
    assert STRATEGY_NAMES[0] not in data["advisory"]["reason"].split(";", 1)[0]
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'row.hwm_freshness_status !== "ACCEPTED"' in renderer


def test_candidate_tamper_fails_closed(inputs: dict[str, Path]) -> None:
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(result["candidate_dir"])
    (candidate / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="verification failed"):
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            candidate,
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def test_browser_qa_and_publication(inputs: dict[str, Path], tmp_path: Path) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    qa_result = panel.validate_qa(candidate, qa)
    assert qa_result["all_passed"] is True
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    pointer = read_current(inputs["output"])
    assert published["publication_state"] == "PUBLISHED"
    assert pointer.generation_id == published["generation_id"]
    generation = inputs["output"] / "generations" / pointer.generation_id
    assert (generation / "index.html").is_file()
    assert (generation / "qa" / "qa-validation.json").is_file()
    semantic = json.loads(
        (generation / "qa" / "semantic-validation.json").read_text(encoding="utf-8")
    )
    assert semantic["all_passed"] is True
    assert semantic["checks_total"] >= 16
    assert semantic["panel_id"] == built["panel_id"]
    launcher = inputs["output"] / "index.html"
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert f"generations/{pointer.generation_id}/index.html" in launcher_text
    assert '"+location.hash' in launcher_text
    assert published["stable_launcher"] == str(launcher)
    assert (
        published["launcher_sha256"]
        == hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    manifest = json.loads((generation / "generation.json").read_text(encoding="utf-8"))
    freshness_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["artifact_id"] == "input/freshness-health.json"
    )
    assert freshness_artifact["schema"]["version"] == "2"


def test_stable_launcher_browser_qa_proves_redirect(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "candidate")
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    launcher = inputs["output"] / "index.html"
    launcher_qa = _qa(candidate, tmp_path / "launcher")
    launcher_url = launcher.resolve().as_uri()
    generation_url = (
        (
            inputs["output"]
            / "generations"
            / str(published["generation_id"])
            / "index.html"
        )
        .resolve()
        .as_uri()
    )
    for name in panel.CAPTURES:
        manifest_path = launcher_qa / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"] = f"{launcher_url}#{name}"
        manifest["final_url"] = f"{generation_url}#{name}"
        _write_json(manifest_path, manifest)

    result = panel.validate_stable_launcher_qa(
        launcher, launcher_qa, str(published["generation_id"])
    )

    assert result["all_passed"] is True
    assert result["checks_total"] == 5
    assert (
        result["launcher_sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    persisted = json.loads(
        (launcher_qa / "stable-launcher-validation.json").read_text(encoding="utf-8")
    )
    assert persisted == result


def test_stable_launcher_browser_qa_rejects_lost_view_fragment(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path / "candidate")
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    launcher = inputs["output"] / "index.html"
    launcher_qa = _qa(candidate, tmp_path / "launcher")
    launcher_url = launcher.resolve().as_uri()
    generation_url = (
        (
            inputs["output"]
            / "generations"
            / str(published["generation_id"])
            / "index.html"
        )
        .resolve()
        .as_uri()
    )
    for name in panel.CAPTURES:
        manifest_path = launcher_qa / name / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"] = f"{launcher_url}#{name}"
        manifest["final_url"] = generation_url
        _write_json(manifest_path, manifest)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="stable launcher browser QA failed",
    ):
        panel.validate_stable_launcher_qa(
            launcher, launcher_qa, str(published["generation_id"])
        )


def test_publication_manifest_uses_later_operational_freshness_date(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    freshness = json.loads(inputs["freshness"].read_text(encoding="utf-8"))
    freshness["as_of_date"] = "2026-07-31"
    for assessment in freshness["assessments"]:
        assessment["evaluated_on"] = "2026-07-31"
    _write_json(inputs["freshness"], freshness)
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)

    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )

    generation = inputs["output"] / "generations" / str(published["generation_id"])
    manifest = json.loads((generation / "generation.json").read_text(encoding="utf-8"))
    freshness_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["artifact_id"] == "input/freshness-health.json"
    )
    assert manifest["as_of_date"] == "2026-07-31"
    assert manifest["coverage"]["observed_start"] == "2026-07-30"
    assert manifest["coverage"]["observed_end"] == "2026-07-31"
    assert freshness_artifact["as_of_date"] == "2026-07-31"


def test_launcher_failure_restores_prior_pointer_and_launcher(
    inputs: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = GenerationManifest.build(
        as_of_date=date(2026, 7, 1),
        adapter=AdapterIdentity("prior-panel", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted prior control panel",
            fact_types=("portfolio control panel",),
            observed_start=date(2026, 7, 1),
            observed_end=date(2026, 7, 1),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="panel/prior.html",
                sha256=hashlib.sha256(b"prior accepted launcher\n").hexdigest(),
                size_bytes=len(b"prior accepted launcher\n"),
                as_of_date=date(2026, 7, 1),
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation(inputs["output"], previous)
    publish_generation(inputs["output"], previous.generation_id)
    launcher = inputs["output"] / "index.html"
    launcher.write_bytes(b"prior accepted launcher\n")

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)

    original = panel._publish_current_launcher

    def fail_after_launcher(root: Path, generation_id: str) -> str:
        original(root, generation_id)
        raise OSError("injected launcher publication failure")

    monkeypatch.setattr(panel, "_publish_current_launcher", fail_after_launcher)
    with pytest.raises(OSError, match="injected launcher"):
        panel.publish_panel(
            inputs["report"],
            inputs["legacy"],
            candidate,
            qa,
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )

    assert read_current(inputs["output"]).generation_id == previous.generation_id
    assert launcher.read_bytes() == b"prior accepted launcher\n"


def test_browser_qa_rejects_console_error(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    candidate = Path(built["candidate_dir"])
    qa = _qa(candidate, tmp_path)
    _write_json(qa / "desktop-risk" / "console-events.json", [{"text": "boom"}])
    with pytest.raises(panel.ControlPanelValidationError, match="browser QA failed"):
        panel.validate_qa(candidate, qa)


def test_governed_hwm_perimeter_excludes_composer_and_accepts_provider_nav() -> None:
    strategies = [
        {
            "strategy_id": "composer-holy-grail",
            "strategy_name": "[Composer] - The Holy Grail (Buy Copy)",
            "current_value_usd": "1",
            "nav_status": "UNAVAILABLE",
        },
        {
            "strategy_id": "robuxio-crypto",
            "strategy_name": "Robuxio Crypto (High Sharpe High Vol)",
            "current_value_usd": "80",
            "nav_status": "AVAILABLE",
            "nav_evidence_layer": "PROVIDER_LIVE",
            "nav_observed_start": "2025-08-14",
            "nav_observed_end": "2026-07-26",
            "nav_return_observations": 331,
        },
        {
            "strategy_id": "volatis-strategy",
            "strategy_name": "Volatis Strategy",
            "current_value_usd": "20",
            "nav_status": "AVAILABLE",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_observed_start": "2024-10-16",
            "nav_observed_end": "2026-07-29",
            "nav_return_observations": 650,
        },
    ]
    policy = {
        "accepted_daily_nav_evidence": [
            "ACCOUNT_LIVE",
            "PROVIDER_LIVE",
        ],
        "accepted_provider_live_strategy_ids": ["robuxio-crypto"],
        "excluded_strategy_ids": ["composer-holy-grail"],
    }

    rows, summary = panel._governed_hwm_coverage(strategies, policy)

    by_id = {row["strategy_id"]: row for row in rows}
    assert by_id["composer-holy-grail"]["nav_status"] == "EXCLUDED_DE_MINIMIS"
    assert by_id["robuxio-crypto"]["nav_status"] == "AVAILABLE"
    assert by_id["robuxio-crypto"]["hwm_source"] == "PORTFOLIO_REPORT"
    assert by_id["robuxio-crypto"]["nav_return_observations"] == 331
    assert by_id["volatis-strategy"]["nav_status"] == "AVAILABLE"
    assert by_id["volatis-strategy"]["nav_return_observations"] == 650
    assert summary == {
        "covered_value_usd": "100",
        "excluded_details": [
            {
                "classification": "EXCLUDED_DE_MINIMIS",
                "current_value_usd": "1",
                "effective_date": None,
                "reason": "Policy exclusion retained without a supplied reason.",
                "strategy_id": "composer-holy-grail",
                "strategy_name": "[Composer] - The Holy Grail (Buy Copy)",
            }
        ],
        "excluded_value_usd": "1",
        "excluded_strategies": ["[Composer] - The Holy Grail (Buy Copy)"],
        "governed_perimeter_value_usd": "100",
        "nav_coverage": "1",
        "policy_id": "PORT-HWM-2",
        "status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
        "status_code_version": 2,
        "status_semantics": "EXPLICIT_GOVERNED_HWM_PERIMETER",
        "uncovered_details": [],
        "uncovered_strategies": [],
        "uncovered_value_usd": "0",
    }


def test_hwm_policy_enforces_exclusion_effective_date() -> None:
    before, _ = panel._hwm_policy(date(2026, 7, 31))
    effective, _ = panel._hwm_policy(date(2026, 8, 1))
    echo_effective, _ = panel._hwm_policy(date(2026, 8, 6))

    assert before["excluded_strategy_ids"] == []
    assert effective["excluded_strategy_ids"] == ["composer-holy-grail"]
    assert echo_effective["excluded_strategy_ids"] == [
        "composer-holy-grail",
        "echotrade-growth-pivots",
    ]


def test_hwm_coverage_rejects_unaccepted_nav_evidence_class() -> None:
    rows, summary = panel._governed_hwm_coverage(
        [
            {
                "current_value_usd": "100",
                "nav_evidence_layer": "BACKTEST",
                "nav_status": "AVAILABLE",
                "strategy_id": "strategy-one",
                "strategy_name": "Strategy One",
            }
        ],
        {
            "accepted_daily_nav_evidence": ["ACCOUNT_LIVE"],
            "accepted_provider_live_strategy_ids": [],
            "excluded_strategy_ids": [],
        },
    )

    assert rows[0]["hwm_evidence_status"] == "UNACCEPTED"
    assert summary["covered_value_usd"] == "0"
    assert summary["uncovered_strategies"] == ["Strategy One"]
    assert summary["uncovered_details"][0]["qualification_gap"] == (
        "UNACCEPTED_EVIDENCE"
    )


def test_hwm_coverage_names_freshness_block_without_denigrating_nav() -> None:
    rows, summary = panel._governed_hwm_coverage(
        [
            {
                "current_value_usd": "0.19",
                "nav_evidence_layer": "ACCOUNT_LIVE",
                "nav_status": "AVAILABLE",
                "strategy_id": "echo-trade",
                "strategy_name": "EchoTrade",
                "systematic_weight": "0.000000463",
            }
        ],
        {
            "accepted_daily_nav_evidence": ["ACCOUNT_LIVE"],
            "accepted_provider_live_strategy_ids": [],
            "excluded_strategy_ids": [],
        },
        freshness_health=[
            {
                "blocks_current_consumers": True,
                "fact_type": "strategy-returns",
                "status": "STALE",
                "subject_id": "echo-trade",
            }
        ],
        require_freshness=True,
    )

    assert rows[0]["nav_status"] == "AVAILABLE"
    assert rows[0]["hwm_evidence_status"] == "ACCEPTED"
    assert rows[0]["hwm_freshness_status"] == "BLOCKED"
    assert rows[0]["hwm_freshness_state"] == "STALE"
    assert summary["uncovered_details"][0]["qualification_gap"] == "FRESHNESS_STALE"

    reconciled = panel._reconcile_hwm_coverage(
        [
            {
                "category": "FACT_COVERAGE",
                "item": "nav",
                "known_gap": None,
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                "value": None,
            }
        ],
        summary,
    )
    nav = next(row for row in reconciled if row["item"] == "nav")
    assert "not HWM-qualified" in nav["known_gap"]
    assert "EchoTrade ($0.19; FRESHNESS_STALE" in nav["known_gap"]
    assert "lacks accepted daily NAV" not in nav["known_gap"]


def test_risk_template_does_not_hard_code_provider_coverage() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "covers Robuxio and Volatis" not in template
    assert (
        "qualifies only while its governed freshness and evidence gates pass"
        in template
    )


def test_source_contract_tracks_frozen_legacy_renderer_not_current_template() -> None:
    contract = json.loads(panel.SOURCE_CONTRACT.read_text(encoding="utf-8"))

    assert contract["source_files"]["app.js"] == (
        "15e406b2b9df40f5e8b2e6205eaf18669dfd5119a73f796353039ed7f3ab929c"
    )
    assert contract["source_files"]["app.js"] != _sha(
        panel.DEFAULT_TEMPLATE_ROOT / "app.js"
    )


def test_renderer_derives_provider_lineage_labels_from_provider_payload() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "P123 live book" not in renderer
    assert "P123 account snapshot" not in renderer
    assert "providerName(provider.provider_id || evidence.provider_id)" in renderer
    assert 'matrixport: "Matrixport"' in renderer
    assert "if (account) parts.push" in renderer


def test_renderer_distinguishes_nav_presence_from_hwm_qualification() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'row.nav_status !== "AVAILABLE"' in renderer
    assert 'row.hwm_evidence_status !== "ACCEPTED"' in renderer
    assert 'row.hwm_freshness_status === "BLOCKED"' in renderer
    assert 'row.hwm_freshness_status === "BOUNDARY_MISMATCH"' in renderer
    assert "UNAVAILABLE_EVIDENCE_BOUNDARY_MISMATCH" in renderer
    assert "row.hwm_freshness_state" in renderer
    assert "Evidence ${statusText(row.nav_status)}" in renderer
    assert "HWM ${statusText(qualification.status)}" in renderer
    assert "uncovered material sleeve" not in renderer
    assert "lacks accepted daily NAV" not in renderer
    assert '["Material NAV coverage"' not in renderer
    assert "governed HWM daily NAV is stale or incomplete" in renderer


def test_renderer_exposes_effective_dated_strategy_version_dispositions() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "row.version_disposition" in renderer
    assert "row.version_effective_start" in renderer
    assert "row.design_status" in renderer
    assert "row.backtest_status" in renderer
    assert "row.live_boundary_status" in renderer
    assert "row.version_compatibility_status" in renderer
    assert "data.hybrid_mode_gate.reason" in renderer
    assert "weekly?.strategy_evidence_modes" in renderer


def test_renderer_exposes_exact_date_attribution_cost_and_benchmark_dispositions() -> (
    None
):
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    for field in (
        "provider_comparison_observations",
        "provider_cumulative_gap",
        "provider_active_return_annualized",
        "provider_tracking_error_annualized",
        "provider_information_ratio",
        "provider_beta",
        "provider_upside_capture",
        "provider_downside_capture",
        "provider_relative_drawdown",
        "cash_drag_status",
        "fees_status",
        "annual_external_cost_rate_pre_tax",
        "after_tax_status",
        "benchmark_assignment_status",
        "benchmark_effective_start",
        "benchmark_rationale",
    ):
        assert f"row.{field}" in renderer
    assert "Exact common dates" in renderer
    assert "No admitted benchmark" in renderer


def test_renderer_keeps_exact_hwm_dollars_and_accessible_table_headers() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const moneyExact = new Intl.NumberFormat" in renderer
    assert "currencyExact(data.hwm_summary.uncovered_value_usd)" in renderer
    assert "no numeric HWM is calculated while unavailable" in renderer
    assert '<th scope="col"' in renderer
    assert '<th scope="row"' in renderer
    assert "0.00x" not in renderer
    assert "data.panel_schema_version !== 18" in renderer
    assert "corrOne.expected_loss" not in renderer
    assert "const centralCase = dependence.central_case" in renderer
    assert "Central case" in renderer
    assert "Descriptive only" in renderer


def test_candidate_driver_map_requires_fixed_cardinality_and_no_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "driver-map.json"
    cluster_ids = sorted(panel.DRIVER_MAP_CLUSTER_IDS)
    payload = {
        "schema_version": 1,
        "as_of_date": "2026-07-24",
        "research_status": "FROZEN_EVIDENCE_NOT_CURRENT",
        "coverage": {
            "candidate_count": 33,
            "measured_candidate_count": 33,
            "unavailable_candidate_count": 0,
            "refresh_status": "UNAVAILABLE_RAW_RETURN_INPUTS_NOT_RETAINED",
        },
        "portfolio": {"systematic_loss_limit": "0.15"},
        "candidates": [
            {
                "candidate_id": f"candidate-{index}",
                "ticker": f"C{index:02d}",
                "category": "Test category",
                "tier": "Tier 1",
                "portfolio_role": "Research only",
                "cost": {
                    "status": "AVAILABLE_COMPONENTS_NO_MODELED_NOTIONAL",
                    "expense_ratio": "0.01",
                    "annual_fixed_cost": "0",
                },
                "live_metrics": {
                    "status": "AVAILABLE_FROZEN_LIVE_VEHICLE_METRICS",
                    "start_date": "2020-01-01",
                    "end_date": "2026-07-24",
                    "observations": 1000,
                },
                "cluster_scores": [
                    {
                        "cluster_id": cluster_id,
                        "status": "AVAILABLE_DESCRIPTIVE_FROZEN",
                        "missing_members": [],
                        "downside_correlation_mean": "0.2",
                        "pearson_mean": "0.1",
                        "worst_decile_candidate_return_minimum": "0.01",
                        "descriptive_rank": index + 1,
                        "window_start": "2026-01-01",
                        "window_end": "2026-07-17",
                    }
                    for cluster_id in cluster_ids
                ],
            }
            for index in range(33)
        ],
        "driver_clusters": [
            {
                "cluster_id": cluster_id,
                "members": [f"member-{index}"],
                "allocation_weight": "0.2",
                "adverse_loss_budget": "0.03",
                "budget_status": "WITHIN_LIMIT",
                "observed_pair_count": 1,
                "observed_pair_correlation_minimum": "0.1",
                "observed_pair_correlation_maximum": "0.1",
            }
            for index, cluster_id in enumerate(cluster_ids)
        ],
        "tactical_leave_one_out": [
            {
                "series_id": f"strategy-{index}",
                "correlation_one_loss_reduction": "0.01",
                "central_case_loss_reduction": "0.005",
                "cluster_loss_without": "0.1",
                "cluster_budget_status_without": "WITHIN_LIMIT",
            }
            for index in range(4)
        ],
        "authority_boundary": {
            "allocation_effect": "NONE",
            "execution_effect": "NONE",
            "readiness_effect": "NONE",
            "automatic_add_drop_authority": "NONE",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    accepted, identity = panel._candidate_driver_map(path)

    assert accepted == payload
    assert identity is not None
    assert identity["as_of_date"] == "2026-07-24"
    assert len(identity["sha256"]) == 64

    del payload["candidates"][0]["cluster_scores"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="cluster_scores must be an array"
    ):
        panel._candidate_driver_map(path)
    payload["candidates"][0]["cluster_scores"] = [
        dict(payload["candidates"][1]["cluster_scores"][0])
        for _ in range(len(cluster_ids))
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="cluster score identity differs"
    ):
        panel._candidate_driver_map(path)
    payload["candidates"][0]["cluster_scores"] = [
        dict(score) for score in payload["candidates"][1]["cluster_scores"]
    ]

    payload["authority_boundary"]["allocation_effect"] = "SUGGEST"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="driver-map authority differs"
    ):
        panel._candidate_driver_map(path)

    payload["authority_boundary"]["allocation_effect"] = "NONE"
    payload["authority_boundary"]["unexpected_effect"] = "NONE"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        panel.ControlPanelValidationError, match="driver-map authority differs"
    ):
        panel._candidate_driver_map(path)


def test_published_panel_files_include_optional_driver_map() -> None:
    without_driver_map = panel._published_panel_files(None)
    with_driver_map = panel._published_panel_files(Path("driver-map.json"))

    assert panel.DRIVER_MAP_PANEL_FILE not in without_driver_map
    assert panel.DRIVER_MAP_PANEL_FILE in with_driver_map
    assert with_driver_map[-1] == "candidate.json"


def test_renderer_discloses_operational_freshness_and_provider_authority() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="freshness-as-of"' in template
    assert "data.meta.freshness_evaluated_on" in renderer
    assert "Freshness evaluated" in renderer
    assert 'row.category === "DATA_FRESHNESS"' in renderer
    assert "Last accepted" in renderer
    assert "evaluated ${formatDate(row.evaluated_on)}" in renderer
    assert "governed provider-ledger classification" in renderer
    assert "not brokerage-account authority" in renderer


def test_renderer_qualifies_provider_live_availability_separately() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'statusText("PROVIDER_LIVE_AVAILABLE")' in renderer
    assert "Account-live unavailable" in renderer
    assert 'row.nav_evidence_layer === "PROVIDER_LIVE"' in renderer
    assert "evidenceAvailability(row)" in renderer


def test_mobile_layout_retains_dates_and_single_column_hwm() -> None:
    css = (panel.DEFAULT_TEMPLATE_ROOT / "analytics.css").read_text(encoding="utf-8")
    qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    assert ".topbar-meta {\n    display: grid;" in css
    assert ".validation-card { grid-template-columns: 1fr; }" in css
    assert ".metric-block .metric-note" in css
    assert "overflow-wrap: anywhere;" in css
    assert "Mobile header retains portfolio date, freshness date, and coverage" in qa
    assert "Mobile HWM governor uses one readable column" in qa


def test_interaction_qa_exercises_every_analytic_selector_state() -> None:
    qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    for lens in (
        "DYNAMIC_STYLE",
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
        "RETURN_SECTOR",
    ):
        assert f'  "{lens}",' in qa
    assert (
        'const requiredCorrelationWindows = ["5", "21", "22", "63", '
        '"126", "130", "252", "260"]'
    ) in qa
    assert 'const requiredDrawdownRegimes = ["either_drawdown", "both_drawdown"]' in qa
    for group in (
        "ALL",
        "STRATEGY",
        "ECONOMIC_FACTOR_US",
        "ECONOMIC_FACTOR_EUROPE",
        "STYLE_US_EQUITY",
        "STYLE_MULTI_ASSET",
        "SECTOR",
        "INDUSTRY",
        "CORE_BENCHMARK",
    ):
        assert f'  "{group}",' in qa
    assert "All five production decomposition lenses are rendered and exercised" in qa
    assert (
        "Factor analyses expose live-only and compatible-history modes without "
        "inventing prehistory"
    ) in qa
    assert (
        'const requiredFactorEvidenceModes = ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]' in qa
    )
    assert '/[1-9][0-9,]* backtest/.test(row.evidence ?? "")' in qa
    assert "const disabled = await button.isDisabled();" in qa
    assert (
        "item.requested_beta_rows.every((row) => row.beta_visible || "
        "row.model_unavailable)" in qa
    )
    assert "Clean-room dynamic FLS path and same-factor validation are rendered" in qa
    assert "All observed windows and both true drawdown regimes are exercised" in qa
    assert "Every weekly display group is rendered and exercised" in qa
    assert "Dependence live and hybrid evidence disclosures are exercised" in qa
    assert "Weekly returns disclose live and hybrid strategy evidence" in qa
    assert "const weeklyHybridDisabled = await weeklyHybridMode.isDisabled();" in qa
    assert 'weeklyHybridTitle?.includes("not PROVEN_COMPATIBLE")' in qa
    assert (
        "Privacy-safe account context filters lifecycle, positions, and performance"
        in qa
    )


def test_control_panel_schema_version_matches_renderer_contract() -> None:
    schema = json.loads(
        (Path("schemas/portfolio/control-panel-data.schema.json")).read_text(
            encoding="utf-8"
        )
    )

    assert schema["properties"]["panel_schema_version"]["const"] == (
        panel.PANEL_SCHEMA_VERSION
    )
    assert "canonical_strategy_series" in schema["required"]
    assert "hybrid_mode_gate" in schema["required"]
    assert "strategy_underwriting" in schema["required"]
    assert "central_case" in schema["properties"]["dependence_analysis"]["required"]
    assert "central_case_loss" in schema["properties"]["advisory"]["required"]
    assert "underwriting_policy_sha256" in schema["properties"]["meta"]["required"]
    # The advisory and hwm_summary carry the numerical composite block with
    # conditional rules: drawdown fields required iff AVAILABLE, reason iff
    # UNAVAILABLE, and the opposite fields are forbidden in each case.
    advisory = schema["properties"]["advisory"]
    assert "hwm_numerical_status" in advisory["required"]
    advisory_available = next(
        rule
        for rule in advisory["allOf"]
        if rule.get("if", {}).get("properties", {}).get("hwm_numerical_status")
        == {"const": "AVAILABLE"}
    )
    assert advisory_available["then"]["required"] == [
        "hwm_index",
        "hwm_current_drawdown",
        "hwm_maximum_drawdown",
    ]
    assert advisory_available["then"]["not"] == {"required": ["hwm_numerical_reason"]}
    advisory_unavailable = next(
        rule
        for rule in advisory["allOf"]
        if rule.get("if", {}).get("properties", {}).get("hwm_numerical_status")
        == {"const": "UNAVAILABLE"}
    )
    assert advisory_unavailable["then"]["required"] == ["hwm_numerical_reason"]
    assert "not" in advisory_unavailable["then"]
    hwm_summary = schema["properties"]["hwm_summary"]
    assert "hwm_numerical_status" in hwm_summary["required"]
    assert any(
        rule.get("then", {}).get("required")
        == ["hwm_index", "hwm_current_drawdown", "hwm_maximum_drawdown"]
        and "not" in rule.get("then", {})
        for rule in hwm_summary["allOf"]
    )
    blocked = schema["properties"]["hybrid_mode_gate"]["properties"][
        "blocked_strategies"
    ]["items"]
    assert blocked["additionalProperties"] is False
    eligible = schema["properties"]["hybrid_mode_gate"]["properties"][
        "eligible_strategies"
    ]["items"]
    assert eligible["additionalProperties"] is False
    assert eligible["properties"]["compatibility_status"] == {
        "const": "PROVEN_COMPATIBLE"
    }
    assert schema["properties"]["weekly_returns"]["properties"][
        "strategy_evidence_modes"
    ]["oneOf"] == [
        {"const": ["LIVE_ONLY"]},
        {"const": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]},
    ]
    decomposition = schema["properties"]["decomposition_lenses"]
    assert decomposition["properties"]["default_evidence_mode"] == {
        "const": "LIVE_ONLY"
    }
    assert decomposition["properties"]["evidence_modes"]["items"] == {
        "enum": ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]
    }
    assert "replication_analysis" in schema["required"]
    canonical = schema["properties"]["canonical_strategy_series"]["items"]
    assert canonical["additionalProperties"] is False
    assert set(canonical["required"]) == {
        "canonical_live_series_sha256",
        "evidence_layer",
        "generation_id",
        "observed_end",
        "observed_start",
        "return_observations",
        "series_id",
        "strategy_id",
        "strategy_version",
    }
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "Forward-only research tracker" in renderer
    assert "holdout recalibration is NONE" in renderer
    assert "no readiness, allocation, HWM, execution, or order authority" in renderer


def test_replication_disposition_counts_derive_from_accepted_rows() -> None:
    rows = [
        *[{"availability_status": "AVAILABLE_RESEARCH_REPLICATION"} for _ in range(4)],
        *[{"availability_status": "DIAGNOSTIC_ONLY_SOURCE_MODEL"} for _ in range(3)],
        *[
            {"availability_status": "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL"}
            for _ in range(2)
        ],
    ]

    assert panel._replication_disposition_counts(rows) == {
        "available_count": 4,
        "diagnostic_count": 3,
        "unavailable_count": 2,
    }


def test_hwm_prose_uses_qualified_evidence_instead_of_legacy_source_label() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "ACCOUNT_LIVE_PROVIDER_OVERLAY" not in renderer
    assert "acceptedHwmEvidence" in renderer
    assert "hwmEvidenceLabel(row)" in renderer
    assert "Accepted account-live/provider-overlay evidence:" in renderer
    assert "Provider-custodied account NAV evidence is unavailable" not in renderer


def test_hwm_coverage_removes_superseded_exception_when_material_perimeter_is_complete() -> (
    None
):
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": "legacy gap",
            "status": "PARTIAL",
            "value": None,
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "full-systematic-hwm-unavailable",
            "known_gap": "BLOCKING_FOR_AUTOMATED_HWM",
            "status": "UNAVAILABLE",
            "value": None,
        },
    ]
    summary = {
        "excluded_strategies": ["[Composer] - The Holy Grail (Buy Copy)"],
        "uncovered_strategies": [],
    }

    accepted = panel._reconcile_hwm_coverage(coverage, summary)

    nav = next(row for row in accepted if row["item"] == "nav")
    assert nav["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert "Every included HWM-perimeter strategy is qualified" in nav["known_gap"]
    assert "1 strategy is explicitly excluded de minimis by policy" in nav["known_gap"]
    assert not any(row["item"] == "full-systematic-hwm-unavailable" for row in accepted)


def test_hwm_coverage_adds_named_exception_when_material_nav_is_missing() -> None:
    coverage = [
        {
            "category": "FACT_COVERAGE",
            "item": "nav",
            "known_gap": None,
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "value": None,
        }
    ]
    summary = {
        "excluded_strategies": [],
        "uncovered_strategies": ["Missing Strategy"],
    }

    accepted = panel._reconcile_hwm_coverage(coverage, summary)

    exception = next(
        row for row in accepted if row["item"] == "full-systematic-hwm-unavailable"
    )
    assert exception["status"] == "UNAVAILABLE"
    assert "Missing Strategy ($UNKNOWN; UNQUALIFIED" in exception["known_gap"]


def test_mvp_exposes_seven_primary_surfaces_and_keeps_appendices_separate() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert template.count('class="nav-item') == 7
    for view in (
        "command",
        "capital",
        "risk",
        "attribution",
        "dependence",
        "weekly",
        "data",
    ):
        assert f'data-nav="{view}"' in template
    assert 'data-nav="signals"' not in template
    assert 'data-nav="candidates"' not in template
    assert 'data-view="signals"' in template
    assert 'data-view="candidates"' in template
    assert 'id="candidate-appendix-button"' in template


def test_allocation_target_and_drift_fail_closed_without_admitted_target() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "TARGET_UNAVAILABLE_NOT_ADMITTED" in renderer
    assert "row.accepted_target_weight ?? row.target_weight" in renderer
    assert "Actual allocation is not treated as its own target" in template
    assert "target = actual" not in renderer.lower()


def test_factor_surface_separates_source_availability_from_model_acceptance() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert "SOURCE_DATA_AVAILABLE" in renderer
    assert "MODEL_COMPUTED_NOT_VALIDATED" in renderer
    assert "It does not relabel accepted Volatis, Robuxio" in renderer
    assert "${dynamicAnalysisMarkup(row)}" in renderer
    assert "MPI-informed and non-equivalent" in renderer
    assert "clean-room dynamic FLS path" in template


def test_replication_surface_discloses_simple_rule_without_claiming_provider_logic() -> (
    None
):
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "Replication rule:" in renderer
    assert "SPY excess return" in renderer
    assert "IWM excess return" in renderer
    assert "HYG excess return" in renderer
    assert "use only the prior model state" in renderer
    assert "clip each factor" in renderer
    assert "cap gross factor exposure" in renderer
    assert "zero-excess-return cash" in renderer
    assert "not a recovered copy of the provider's hidden trading rules" in renderer


def test_regime_weekly_values_exclude_partial_boundaries_in_every_mode() -> None:
    complete = [
        5,
        "2026-07-06",
        "2026-07-10",
        "COMPLETE_WITHIN_ACCEPTED_CALENDAR",
        [],
    ]
    partial = [
        3,
        "2026-07-13",
        "2026-07-15",
        "PARTIAL_BOUNDARY",
        ["CURRENT_AS_OF"],
    ]
    row = {
        "values": {"2026-07-06": "0.01", "2026-07-13": "0.25"},
        "observation_meta": {
            "2026-07-06": complete,
            "2026-07-13": partial,
        },
        "values_by_mode": {"LIVE_ONLY": {"2026-07-06": "0.02", "2026-07-13": "0.50"}},
        "observation_meta_by_mode": {
            "LIVE_ONLY": {
                "2026-07-06": complete,
                "2026-07-13": partial,
            }
        },
    }

    assert panel._weekly_values(row) == {"2026-07-06": 0.01}
    assert panel._weekly_values(row, evidence_mode="LIVE_ONLY") == {"2026-07-06": 0.02}


def test_context_and_material_exceptions_have_operator_routes() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="context-account"' in template
    assert 'id="context-strategy"' in template
    assert "function applyContext()" in renderer
    assert 'data-open-view="${safe(route.view)}"' in renderer
    assert 'data-focus-coverage="${safe(item.item)}"' in renderer
    assert 'data-coverage-item="${safe(row.item)}"' in renderer
    assert "target.scrollIntoView" in renderer
    assert 'row.strategy_mapping_disposition === "RESERVE_NOT_APPLICABLE"' in renderer
    assert "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH" in renderer
    assert 'id="remediation-table"' in template
    assert "TTL / reconciled" in renderer
    assert "Escalation required" in renderer
    assert 'item.category === "DATA_FRESHNESS"' in renderer


def test_accessibility_contract_covers_reflow_touch_motion_and_glossary() -> None:
    template = (panel.DEFAULT_TEMPLATE_ROOT / "index.html").read_text(encoding="utf-8")
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    styles = (panel.DEFAULT_TEMPLATE_ROOT / "analytics.css").read_text(encoding="utf-8")
    browser_qa = (panel.DEFAULT_TEMPLATE_ROOT / "browser_qa.mjs").read_text(
        encoding="utf-8"
    )
    interaction_qa = (panel.DEFAULT_TEMPLATE_ROOT / "interaction_qa.mjs").read_text(
        encoding="utf-8"
    )

    for term in (
        "HWM",
        "TWR / MWR",
        "Evidence state",
        "Model state",
        "Gate state",
        "MPI-informed, non-equivalent",
    ):
        assert term in template
    assert 'id="operator-glossary"' in template
    assert "min-height: 44px" in styles
    assert ".weekly-cell" in styles
    assert ".weekly-toggle" in styles
    assert "#factor-model-table" in styles
    assert "#factor-detail .callout" in styles
    assert "#performance-table" in styles
    assert "#coverage-table" in styles
    assert "#underwriting-table [data-label]::before" in styles
    assert "#fact-coverage-matrix-table [data-label]::before" in styles
    assert 'data-label="Long-history statistical membership"' in renderer
    assert 'data-label="Named missing interval"' in renderer
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ":focus-visible" in styles
    assert 'button.setAttribute("aria-controls", view.id)' in renderer
    assert "$$('[data-open-view]').forEach" in renderer
    assert 'group.setAttribute("role", "group")' in renderer
    assert "function prepareScrollableTableRegions()" in renderer
    assert '$$(".table-wrap, .matrix-shell, .weekly-table-shell")' in renderer
    assert 'region.setAttribute("role", "region")' in renderer
    assert 'region.setAttribute("aria-label", caption' in renderer
    assert 'class="exposure-track" aria-hidden="true"' in renderer
    assert renderer.count("prepareScrollableTableRegions();") >= 4
    assert 'options["exclude-candidates"] === "true"' in browser_qa
    assert 'scope: excludeCandidates ? "NON_CANDIDATE" : "FULL"' in browser_qa
    assert 'options["exclude-candidates"] !== "true"' in interaction_qa
    assert 'scope: includeCandidates ? "FULL" : "NON_CANDIDATE"' in interaction_qa
    assert (
        "All eight non-Candidate mobile surfaces meet the 44px target" in interaction_qa
    )


# ── hwm_inputs numerical composite (HWM-001) ─────────────────────────────

_PANEL_HWM_DAYS = ("2026-07-28", "2026-07-29", "2026-07-30")
_PANEL_HWM_CAPITAL = {
    "strategy-0": {"2026-07-29": "100", "2026-07-30": "110"},
    "strategy-1": {"2026-07-29": "300", "2026-07-30": "285"},
}
_PANEL_HWM_RETURNS = {
    "2026-07-29": {"strategy-0": "0.10", "strategy-1": "-0.05"},
    "2026-07-30": {"strategy-0": "0.02", "strategy-1": "0.04"},
}
# Numerical keys the composite block contributes to an hwm_summary.
_NUMERICAL_KEYS = {
    "hwm_index",
    "hwm_current_drawdown",
    "hwm_maximum_drawdown",
    "hwm_numerical_status",
    "hwm_numerical_reason",
}
_FLOW_ATTESTATION_ARTIFACT = {
    "artifact_id": "flow-attestation-1",
    "as_of_date": "2026-07-30",
    "schema_name": "hwm-flow-attestation",
    "schema_version": "1",
    "sha256": "c" * 64,
    "source_key": "hwm-flow-attestation",
}


def _panel_hwm_envelope(crosswalk: list[dict[str, object]]) -> dict[str, object]:
    """Synthetic governed envelope over the panel fixture strategies,
    referencing the hash-bound rollforward crosswalk rows.  Artifact ids
    resolve against the fixture catalog's source_artifacts."""
    references = {
        (str(row["strategy_id"]), str(row["date"])): str(
            row["account_reconciliation_sha256"]
        )
        for row in crosswalk
    }
    periods: list[dict[str, object]] = []
    for day in _PANEL_HWM_DAYS:
        for index in range(9):
            strategy_id = f"strategy-{index}"
            if day == "2026-07-28":
                periods.append(
                    {
                        "strategy_id": strategy_id,
                        "date": day,
                        "lane": "ACCOUNT",
                        "disposition": "EXPECTED_NON_TRADING",
                        "beginning_capital_usd": None,
                        "return_source_record_id": None,
                        "flow_status": "MISSING",
                        "fx_status": "MISSING",
                    }
                )
            else:
                periods.append(
                    {
                        "strategy_id": strategy_id,
                        "date": day,
                        "lane": "ACCOUNT",
                        "disposition": "AVAILABLE",
                        "beginning_capital_usd": _PANEL_HWM_CAPITAL.get(
                            strategy_id, {}
                        ).get(day, "100"),
                        "return_source_record_id": references[(strategy_id, day)],
                        "component_account_ids": [f"account-{index:02d}"],
                        "flow_status": "OBSERVED",
                        "fx_status": "NOT_REQUIRED",
                        "flow_source_record_ids": [],
                        "fx_source_record_ids": [],
                    }
                )
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-07-28",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-07-30T00:00:00Z",
                "accepted_by": "synthetic",
                "source_artifact_id": "anchor-acceptance-1",
                "source_artifact_sha256": "a" * 64,
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": "calendar-account-1",
            }
        ],
        "periods": periods,
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
        },
    }


def _panel_hwm_crosswalk() -> list[dict[str, object]]:
    """Hash-bound rollforward crosswalk rows, shaped exactly as the provider
    ledger's account-live rollforward emits them (the admitted-envelope
    evidence path)."""
    rows: list[dict[str, object]] = []
    for day in ("2026-07-29", "2026-07-30"):
        for index in range(9):
            strategy_id = f"strategy-{index}"
            reference = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "account_ids": [f"account-{index:02d}"],
                        "date": day,
                        "source_record_ids": [f"nav-{strategy_id}-{day}"],
                        "strategy_id": strategy_id,
                    }
                )
            ).hexdigest()
            rows.append(
                {
                    "account_reconciliation_sha256": reference,
                    "beginning_capital": _PANEL_HWM_CAPITAL.get(strategy_id, {}).get(
                        day, "100"
                    ),
                    "component_account_count": 1,
                    "component_account_ids": [f"account-{index:02d}"],
                    "date": day,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "flow_timing": "DATE_ONLY_END_OF_DAY",
                    "provider_id": "synthetic",
                    "return": _PANEL_HWM_RETURNS[day].get(strategy_id, "0"),
                    "return_basis": "NET",
                    "return_type": "TOTAL",
                    "source_record_ids": [f"nav-{strategy_id}-{day}"],
                    "strategy_id": strategy_id,
                }
            )
    return rows


def _republish_report(root: Path, tmp_path: Path, value: dict[str, object]) -> None:
    """Publish a new report generation carrying extra fields."""
    _publish_report(root, tmp_path / "updated-report.json", value)


def _candidate_data(candidate: dict[str, object]) -> dict[str, object]:
    return json.loads(
        (
            Path(str(candidate["candidate_dir"])) / "data" / "dashboard_data.json"
        ).read_text(encoding="utf-8")
    )


def _expected_hwm_numerics() -> dict[str, str]:
    """Hand-computed composite: day 1 (100*0.10 + 300*(-0.05) + 700*0)/1100,
    day 2 (110*0.02 + 285*0.04 + 700*0)/1095, geometrically linked."""
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        r1 = (Decimal(100) * Decimal("0.10") + Decimal(300) * Decimal("-0.05")) / (
            Decimal(1100)
        )
        r2 = (Decimal(110) * Decimal("0.02") + Decimal(285) * Decimal("0.04")) / (
            Decimal(1095)
        )
        index = Decimal("1.0")
        peak = index
        max_drawdown = Decimal("0")
        for r in (r1, r2):
            index = index * (1 + r)
            peak = max(peak, index)
            max_drawdown = max(max_drawdown, 1 - index / peak)
    return {
        "hwm_index": decimal_text(index),
        "hwm_current_drawdown": decimal_text(Decimal(1) - index / peak),
        "hwm_maximum_drawdown": decimal_text(max_drawdown),
    }


def _envelope_report(
    inputs: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish a governed baseline generation carrying the envelope and
    crosswalk, and build the matching report (copies equal to the catalog,
    hwm_summary computed from the catalog).  Returns (report, catalog); the
    report is NOT yet republished."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    catalog = _panel_baseline_catalog(envelope, crosswalk)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-envelope.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    return report, catalog


def _expect_panel_rejection(
    inputs: dict[str, Any], tmp_path: Path, report: dict[str, Any], match: str
) -> None:
    _republish_report(inputs["report"], tmp_path, report)
    with pytest.raises(panel.ControlPanelValidationError, match=match):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "output-2",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )


def _expect_catalog_contract_probe(
    inputs: dict[str, Any],
    tmp_path: Path,
    report: dict[str, Any],
    catalog: dict[str, Any],
    mutate: Any,
    match: str,
) -> None:
    """Tamper the CATALOG's envelope (the reconciliation authority), publish
    it as a new baseline generation, rebind the report, and expect the
    panel's full-contract validator to reject.  The report's carried copy
    mirrors the tampered catalog, so the validator — not the copy-equality
    check — is what fires."""
    tampered = json.loads(json.dumps(catalog))
    mutate(tampered)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "tampered-baseline.json", tampered
    )
    report["baseline"] = identity
    report["hwm_inputs"] = tampered["hwm_inputs"]
    _expect_panel_rejection(inputs, tmp_path, report, match)


def test_advisory_carries_numerical_hwm_when_envelope_present(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)

    expected = _expected_hwm_numerics()
    advisory = data["advisory"]
    assert advisory["hwm_numerical_status"] == "AVAILABLE"
    assert "hwm_numerical_reason" not in advisory
    assert advisory["hwm_index"] == expected["hwm_index"]
    assert advisory["hwm_current_drawdown"] == expected["hwm_current_drawdown"]
    assert advisory["hwm_maximum_drawdown"] == expected["hwm_maximum_drawdown"]
    assert data["hwm_summary"]["hwm_numerical_status"] == "AVAILABLE"
    assert (
        data["hwm_summary"]["hwm_maximum_drawdown"]
        == (expected["hwm_maximum_drawdown"])
    )


def test_panel_reconciles_portable_proof_without_replacing_governed_hwm(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)
    governed_candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "governed-panel-output",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    governed_data = _candidate_data(governed_candidate)
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle(
        schema_version=3
    )
    proof = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    report["hwm_portable_proof"] = proof
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_portable_receipt=receipt,
        hwm_portable_generation_bytes=input_generation_bytes,
        hwm_portable_calendar_bytes=calendar_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_portable_proof"] == proof
    assert data["hwm_summary"] == governed_data["hwm_summary"]
    assert data["advisory"] == governed_data["advisory"]
    assert data["advisory"]["hwm_index"] != proof["current_index"]
    assert data["hwm_portable_proof"]["current_index"] == "93.6"
    assert data["hwm_portable_proof"]["current_hwm"] == "104"
    assert data["hwm_portable_proof"]["current_drawdown"] == "-0.1"
    assert data["hwm_portable_proof"]["maximum_drawdown"] == "-0.1"
    assert data["hwm_portable_proof"]["age_status"] == ("COMPLETE_DATA_AGE_WARNING")
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(candidate["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_portable_receipt=receipt,
            hwm_portable_generation_bytes=input_generation_bytes,
            hwm_portable_calendar_bytes=calendar_bytes,
        )["all_passed"]
        is True
    )
    renderer = (REPOSITORY_ROOT / "apps/portfolio-control-panel/app.js").read_text(
        encoding="utf-8"
    )
    assert "DEVELOPMENT — NOT FINANCIAL ACCEPTANCE" in renderer
    assert 'join(" → ")' in renderer
    assert "governed financial HWM remains unavailable" in renderer
    assert "governed financial HWM remains independently available" in renderer
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    portable_schema = schema["$defs"]["portable_hwm_proof"]
    assert set(portable_schema["required"]) == set(proof)
    assert portable_schema["properties"]["financial_acceptance"] == {
        "const": "NOT_FINANCIAL_ACCEPTANCE"
    }
    assert portable_schema["properties"]["assurance_profile"] == {
        "const": "DEVELOPMENT"
    }


def test_panel_portable_proof_does_not_upgrade_missing_governed_hwm(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    governed_candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "governed-panel-output",
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    governed_data = _candidate_data(governed_candidate)
    report = _report_with_baseline(inputs)
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle()
    report["hwm_portable_proof"] = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_portable_receipt=receipt,
        hwm_portable_generation_bytes=input_generation_bytes,
        hwm_portable_calendar_bytes=calendar_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_summary"] == governed_data["hwm_summary"]
    assert data["advisory"] == governed_data["advisory"]
    assert data["hwm_portable_proof"]["financial_acceptance"] == (
        "NOT_FINANCIAL_ACCEPTANCE"
    )


def test_panel_rejects_missing_or_tampered_portable_hwm_authority(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    receipt, input_generation_bytes, calendar_bytes = _portable_hwm_bundle()
    report["hwm_portable_proof"] = panel._panel_portable_hwm_proof(
        receipt, input_generation_bytes, calendar_bytes
    )
    _republish_report(inputs["report"], tmp_path, report)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="portable HWM authority is not supplied independently",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
        )

    tampered = HWM_TEST_NAMESPACE["as_held_hwm_receipt"](receipt)
    tampered["status"] = "AVAILABLE"
    unsigned = {
        key: value for key, value in tampered.items() if key != "receipt_sha256"
    }
    tampered["receipt_sha256"] = HWM_TEST_NAMESPACE["_fixture_sha256"](unsigned)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="receipt identity is invalid",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            tmp_path / "tampered-output",
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_portable_receipt=tampered,
            hwm_portable_generation_bytes=input_generation_bytes,
            hwm_portable_calendar_bytes=calendar_bytes,
        )


def test_advisory_omits_drawdown_fields_when_envelope_absent(
    inputs: dict[str, Any],
) -> None:
    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)

    advisory = data["advisory"]
    assert advisory["hwm_numerical_status"] == "UNAVAILABLE"
    assert advisory["hwm_numerical_reason"] == "NO_HWM_INPUTS"
    assert "hwm_index" not in advisory
    assert "hwm_current_drawdown" not in advisory
    assert "hwm_maximum_drawdown" not in advisory


def test_panel_rejects_tampered_numerical_hwm_in_report(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)
    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    computed = _candidate_data(candidate)["hwm_summary"]

    # Republish the report with a tampered numerical field: the panel's
    # independent recomputation from the catalog must reject it.
    tampered = dict(report)
    tampered["hwm_summary"] = {**computed, "hwm_maximum_drawdown": "9.99"}
    _expect_panel_rejection(
        inputs,
        tmp_path,
        tampered,
        "governed HWM summary differs from the accepted portfolio report",
    )


def test_panel_rejects_report_without_hwm_summary(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Deletion tamper: a report that omits the governed HWM summary is
    rejected unconditionally — the comparison cannot be bypassed."""
    report = _report_with_baseline(inputs)
    assert "hwm_summary" not in report
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "accepted portfolio report lacks the governed HWM summary",
    )


def test_panel_rejects_permissive_readiness_when_numerical_hwm_unavailable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Readiness tamper: with the numerical composite UNAVAILABLE the report
    must gate advisory readiness to DO_NOT_EXPAND_SYSTEMATIC_RISK; a
    permissive row is rejected against the panel's recomputed state."""
    report = _report_with_baseline(inputs)
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    assert report["hwm_summary"]["hwm_numerical_status"] == "UNAVAILABLE"
    report["portfolio_summary"] = [
        (
            {**row, "value": "ELIGIBLE_FOR_REVIEW"}
            if row["metric"] == "Advisory readiness"
            else row
        )
        for row in report["portfolio_summary"]
    ]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report advisory readiness ignores the numerical HWM gate",
    )


def test_panel_all_unavailable_day_fails_closed(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Regression: a required day whose members are ALL UNAVAILABLE is a
    coverage gap.  The catalog's envelope carries the gap; the panel
    recomputes INCOMPLETE_PERIODS from the catalog and the advisory shows
    the composite as UNAVAILABLE — the day is never silently skipped."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    for row in envelope["periods"]:
        if row["date"] == "2026-07-29":
            row["disposition"] = "UNAVAILABLE"
            row["beginning_capital_usd"] = None
            row["return_source_record_id"] = None
    catalog = _panel_baseline_catalog(envelope, crosswalk)
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-gap.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "UNAVAILABLE"
    assert data["advisory"]["hwm_numerical_reason"] == "INCOMPLETE_PERIODS"
    assert "hwm_index" not in data["advisory"]


def test_panel_rejects_coherently_tampered_report_copies(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Coordinated tamper: the report's envelope, crosswalk, and claimed
    hwm_summary are coherently rewritten (capital 100 -> 999 everywhere in
    the report's copies), but the catalog is pristine.  The panel's
    authority is the catalog, so the forged report is rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)
    tampered_envelope = json.loads(json.dumps(report["hwm_inputs"]))
    tampered_crosswalk = json.loads(json.dumps(report["strategy_returns"]))
    for row in tampered_envelope["periods"]:
        if row["disposition"] == "AVAILABLE":
            row["beginning_capital_usd"] = "999"
    for row in tampered_crosswalk:
        row["beginning_capital"] = "999"
    # Compute allocations matching the tampered endpoint so the forgery is
    # internally coherent (endpoint recomputation passes).
    from decimal import Decimal as _Decimal

    forged_allocations: list[dict[str, Any]] = []
    by_day: dict[str, dict[str, _Decimal]] = {}
    for rw in tampered_crosswalk:
        sid = str(rw["strategy_id"])
        day = str(rw["date"])
        ret = _Decimal(str(rw.get("return", "0")))
        by_day.setdefault(day, {})[sid] = ret
    endpoint_date = "2026-07-30"
    for row in tampered_envelope.get("periods", []):
        if (
            row.get("disposition") != "AVAILABLE"
            or row.get("lane") != "ACCOUNT"
            or str(row.get("date")) != endpoint_date
        ):
            continue
        sid = str(row["strategy_id"])
        capital = _Decimal(str(row["beginning_capital_usd"]))
        period_return = by_day.get(endpoint_date, {}).get(sid, _Decimal(0))
        endpoint_val = str(capital * (_Decimal(1) + period_return))
        forged_allocations.append(
            {
                "as_of_date": endpoint_date,
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": endpoint_val,
                },
                "scope": "SYSTEMATIC",
                "strategy_id": sid,
            }
        )
    forged_view = {
        "as_of_date": catalog["as_of_date"],
        "allocations": forged_allocations,
        "hwm_acceptance_record": catalog.get("hwm_acceptance_record"),
        "hwm_inputs": tampered_envelope,
        "source_artifacts": catalog["source_artifacts"],
        "strategy_returns": tampered_crosswalk,
    }
    forged_numerical = panel._compute_panel_numerical_hwm(forged_view)
    assert forged_numerical["hwm_numerical_status"] == "AVAILABLE"
    assert forged_numerical["hwm_index"] != report["hwm_summary"]["hwm_index"]
    report["hwm_inputs"] = tampered_envelope
    report["strategy_returns"] = tampered_crosswalk
    report["hwm_summary"] = {
        **{
            key: value
            for key, value in report["hwm_summary"].items()
            if key not in _NUMERICAL_KEYS
        },
        **forged_numerical,
    }
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs from the governed baseline catalog",
    )


def test_panel_rejects_truncated_envelope(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A truncated report envelope copy (one period row dropped) no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["hwm_inputs"]["periods"] = report["hwm_inputs"]["periods"][:-1]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs from the governed baseline catalog",
    )


def test_panel_rejects_envelope_missing_date(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A whole required day missing from the report's envelope copy no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["hwm_inputs"]["periods"] = [
        row for row in report["hwm_inputs"]["periods"] if row["date"] != "2026-07-29"
    ]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs from the governed baseline catalog",
    )


def test_panel_rejects_envelope_capital_mismatch(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A tampered crosswalk copy (capital 100 -> 999 in the report's
    strategy_returns) no longer matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    report["strategy_returns"][0]["beginning_capital"] = "999"
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report strategy_returns differ from the governed baseline catalog",
    )


def test_panel_rejects_envelope_component_mismatch(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A tampered component-account copy in the report's envelope no longer
    matches the governed catalog."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    available_row = report["hwm_inputs"]["periods"][9]
    assert available_row["disposition"] == "AVAILABLE"
    available_row["component_account_ids"] = ["account-08"]
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "report hwm_inputs differs from the governed baseline catalog",
    )


def test_panel_fails_closed_without_baseline_root(inputs: dict[str, Any]) -> None:
    """The baseline generation is a mandatory input: no root, no panel."""
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="baseline root is required for governed HWM reconciliation",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
        )


def test_panel_fails_closed_when_baseline_generation_is_unresolvable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A report binding to a baseline generation that does not exist fails
    closed."""
    report = _report()
    report["baseline"] = {
        "catalog_sha256": "f" * 64,
        "generation_id": "e" * 64,
    }
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "governed baseline generation is missing or failed integrity",
    )


def test_panel_rejects_anchor_base_value_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"].update(
            {"index_base_value": "999"}
        ),
        "hwm_inputs index_base_value must be 1.0",
    )


def test_panel_rejects_anchor_policy_id_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"].update(
            {"hwm_policy_id": "PORT-HWM-2"}
        ),
        "hwm_inputs hwm_policy_id must be PORT-HWM-3",
    )


def test_panel_rejects_arbitrary_provenance_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["anchor"]["acceptance_provenance"].update(
            {"source_artifact_id": "arbitrary-core-artifact"}
        ),
        "does not resolve to an admitted hwm-anchor-acceptance artifact",
    )


def test_panel_rejects_empty_calendar_authorities_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"].update({"calendar_authorities": []}),
        "hwm_inputs calendar_authorities must have at least one entry",
    )


def test_panel_rejects_invalid_reconciliation_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)
    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        lambda catalog: catalog["hwm_inputs"]["reconciliation"].update(
            {"period_capital_method": "SNAPSHOT_BACKFILL"}
        ),
        "hwm_inputs period_capital_method must be NAV_ROLLFORWARD",
    )


def test_panel_rejects_confirmed_zero_with_empty_flow_evidence(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """The full contract requires non-empty attestation evidence arrays:
    CONFIRMED_ZERO with an empty flow evidence list is rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "CONFIRMED_ZERO flow_status requires non-empty flow_source_record_ids",
    )


def test_panel_rejects_confirmed_one_with_empty_fx_evidence(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["fx_status"] = "CONFIRMED_ONE"
        row["fx_source_record_ids"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "CONFIRMED_ONE fx_status requires non-empty fx_source_record_ids",
    )


def test_panel_rejects_non_string_component_account_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["component_account_ids"] = [7]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs component_account_ids must be a non-empty list",
    )


def test_panel_rejects_empty_periods(inputs: dict[str, Any], tmp_path: Path) -> None:
    """An envelope with no periods cannot be the governed artifact, even
    before keyspace reconciliation."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["periods"] = []

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs periods must have at least one entry",
    )


def test_panel_rejects_available_with_missing_flow_status(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """An AVAILABLE row whose flow evidence is MISSING was never admitted;
    the panel rejects it."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["flow_status"] = "MISSING"

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs AVAILABLE period must not have MISSING flow_status",
    )


def test_panel_rejects_available_with_missing_fx_status(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        available = catalog["hwm_inputs"]["periods"][9]
        assert available["disposition"] == "AVAILABLE"
        available["fx_status"] = "MISSING"

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs AVAILABLE period must not have MISSING fx_status",
    )


def test_panel_rejects_fabricated_attestation_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A non-empty but fabricated CONFIRMED_ZERO evidence id does not
    resolve to an admitted attestation artifact."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = ["fabricated-flow-attestation"]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "does not resolve to an admitted hwm-flow-attestation artifact",
    )


def test_panel_rejects_wrong_role_attestation_id(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """A CONFIRMED_ZERO evidence id resolving to a non-attestation role is
    rejected."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        row = catalog["hwm_inputs"]["periods"][0]
        row["flow_status"] = "CONFIRMED_ZERO"
        row["flow_source_record_ids"] = ["calendar-account-1"]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "does not resolve to an admitted hwm-flow-attestation artifact",
    )


def test_panel_rejects_available_confirmed_attestation_payload_unverifiable(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """An AVAILABLE row carrying CONFIRMED_ZERO is rejected at the panel: the
    attestation payload content is not reachable through the catalog
    binding, so the assertion cannot be trusted for the composite."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    available = envelope["periods"][9]
    assert available["disposition"] == "AVAILABLE"
    available["flow_status"] = "CONFIRMED_ZERO"
    available["flow_source_record_ids"] = ["flow-attestation-1"]
    catalog = _panel_baseline_catalog(
        envelope, crosswalk, extra_source_artifacts=[_FLOW_ATTESTATION_ARTIFACT]
    )
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-attested.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], _panel_baseline_catalog())
    report["hwm_summary"]["hwm_numerical_status"] = "UNAVAILABLE"
    report["hwm_summary"]["hwm_numerical_reason"] = "NO_HWM_INPUTS"
    _expect_panel_rejection(
        inputs,
        tmp_path,
        report,
        "hwm_inputs AVAILABLE period attestation payload is not verifiable "
        "through the catalog binding",
    )


def test_panel_accepts_non_available_confirmed_attestation(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Positive control: a non-AVAILABLE row with a CONFIRMED_ZERO flow
    attestation resolving to an admitted role artifact passes validation;
    the day becomes a required-but-unavailable period."""
    crosswalk = _panel_hwm_crosswalk()
    envelope = _panel_hwm_envelope(crosswalk)
    row = envelope["periods"][0]
    assert row["disposition"] == "EXPECTED_NON_TRADING"
    row["disposition"] = "UNAVAILABLE"
    row["flow_status"] = "CONFIRMED_ZERO"
    row["flow_source_record_ids"] = ["flow-attestation-1"]
    catalog = _panel_baseline_catalog(
        envelope, crosswalk, extra_source_artifacts=[_FLOW_ATTESTATION_ARTIFACT]
    )
    identity = _publish_baseline(
        inputs["baseline"], tmp_path / "baseline-attested.json", catalog
    )
    report = _report()
    report["baseline"] = identity
    report["hwm_inputs"] = envelope
    report["strategy_returns"] = crosswalk
    _with_hwm_summary(report, inputs["freshness"], catalog)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "UNAVAILABLE"
    assert data["advisory"]["hwm_numerical_reason"] == "INCOMPLETE_PERIODS"


def test_panel_rejects_policy_exclusion_fraud_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: catalog envelope rows relabeled EXCLUDED_BY_POLICY without an
    admitted policy exclusion are rejected by the shared contract validator
    at the panel boundary."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        for row in catalog["hwm_inputs"]["periods"]:
            if row["strategy_id"] == "strategy-1":
                row["disposition"] = "EXCLUDED_BY_POLICY"
                row["beginning_capital_usd"] = None
                row["return_source_record_id"] = None

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "is not named in the admitted PORT-HWM-3 exclusions",
    )


def test_panel_rejects_unreconciled_endpoint_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: a failed or non-zero allocation endpoint check blocks the
    panel build — matched: false / delta 999 is never reconciled."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["reconciliation"]["allocation_endpoint_check"] = {
            "matched": False,
            "delta_usd": "999",
        }

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "delta_usd differs from the recomputed delta",
    )


def test_panel_rejects_incomplete_envelope_probe(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """Probe: a structurally incomplete envelope (only a periods key) is
    rejected by the closed-shape check at the panel boundary."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"] = {"periods": catalog["hwm_inputs"]["periods"]}

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs envelope shape differs",
    )


# ---------------------------------------------------------------------------
# QC8 regression: full consumption-boundary context enforcement
# ---------------------------------------------------------------------------


def test_panel_rejects_later_anchor_with_truncated_history(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: a fabricated later anchor (acceptance record says
    2026-07-28, envelope claims 2026-07-29) is rejected by the panel's
    anchor-content cross-check."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog["hwm_inputs"]["anchor"]["anchor_date"] = "2026-07-29"
        # Remove 2026-07-28 periods (would be outside new keyspace).
        catalog["hwm_inputs"]["periods"] = [
            p for p in catalog["hwm_inputs"]["periods"] if p["date"] != "2026-07-28"
        ]

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "anchor anchor_date disagrees with the admitted hwm-anchor-acceptance record",
    )


def test_panel_rejects_inflated_allocation_unchanged_declaration(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: an allocation inflated 10x with the envelope still declaring
    {matched: true, delta_usd: "0"} is rejected by the panel's endpoint
    recomputation."""
    from decimal import Decimal as _D

    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        for alloc in catalog["allocations"]:
            if alloc.get("scope") == "SYSTEMATIC" and alloc.get("current_value"):
                alloc["current_value"]["value"] = str(
                    _D(alloc["current_value"]["value"]) * 10
                )

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "allocation endpoint check does not reconcile",
    )


def test_panel_rejects_missing_acceptance_record_fails_closed(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8: if the catalog lacks hwm_acceptance_record, the panel
    consumer fails closed — never falls back to declaration-only mode."""
    report, catalog = _envelope_report(inputs, tmp_path)

    def mutate(catalog: dict[str, Any]) -> None:
        catalog.pop("hwm_acceptance_record", None)

    _expect_catalog_contract_probe(
        inputs,
        tmp_path,
        report,
        catalog,
        mutate,
        "hwm_inputs consumption context is incomplete",
    )


def test_panel_valid_envelope_with_full_context_passes(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    """QC8 parity: a valid envelope with full consumption context
    (acceptance_record + allocations) passes the panel's boundary check
    and produces an AVAILABLE index."""
    report, _catalog = _envelope_report(inputs, tmp_path)
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(candidate)
    assert data["advisory"]["hwm_numerical_status"] == "AVAILABLE"
    assert "hwm_index" in data["advisory"]


# ---------------------------------------------------------------------------
# Series-identity parity (strategy_version) — unit tests for
# _canonical_strategy_series_contracts
# ---------------------------------------------------------------------------

_VERSION = "abc123def4567890abcdef0123456789abcdef01"


def _parity_dependence(
    series_id: str,
    version: str | None = _VERSION,
    live_version: str | None = _VERSION,
) -> dict[str, Any]:
    """Minimal valid dependence-series-composition row."""
    return {
        "canonical_live_series_sha256": "e" * 64,
        "live_evidence_layer": "ACCOUNT_LIVE",
        "live_observations": 80,
        "live_version_ref": live_version,
        "observed_end": "2026-07-30",
        "observed_start": "2026-01-02",
        "series_id": series_id,
        "strategy_version": version,
    }


def _parity_evidence(
    strategy_id: str,
    layer: str = "ACCOUNT_LIVE",
    version: str | None = _VERSION,
) -> dict[str, Any]:
    """Minimal valid evidence row carrying strategy_version.

    Mirrors the production semantics: the evidence row describes the FULL
    accepted live path (223 observations through 2026-08-13), which extends
    beyond the factor-limited canonical evaluation window (80 observations
    through 2026-07-30) that the dependence/factor/weekly fixtures describe.
    """
    return {
        "evidence_layer": layer,
        "generation_id": "d" * 64,
        "generation_as_of_date": "2026-08-13",
        "provider_id": "synthetic",
        "strategy_id": strategy_id,
        "strategy_version": version,
        "account" if layer == "ACCOUNT_LIVE" else "provider": {
            "observed_end": "2026-08-13",
            "observed_start": "2026-01-02",
            "return_observations": 223,
        },
    }


def _parity_weekly(version: str | None = _VERSION) -> dict[str, Any]:
    return {
        "generation_id": "w" * 64,
        "series": [
            {
                "calendar_basis_by_mode": {"LIVE_ONLY": "NYSE"},
                "display_name": "test-series",
                "mode_coverage": {
                    "LIVE_ONLY": {
                        "canonical_live_series_sha256": "e" * 64,
                        "live_daily_observations": 196,
                        "strategy_version": version,
                    }
                },
                "observation_meta_by_mode": {
                    "LIVE_ONLY": {
                        "2026-07-30": ["2026-07-30", "2026-01-02", "2026-07-30"]
                    }
                },
                "primary_group": "STRATEGY",
                "series_id": "test-series",
                "values_by_mode": {"LIVE_ONLY": {"2026-07-29": "0.01"}},
            }
        ],
        "strategy_evidence_modes": ["LIVE_ONLY"],
    }


def _parity_factor() -> dict[str, Any]:
    return {
        "series": [
            {
                "series_id": "test-series",
                "coverage": {
                    "canonical_live_series_sha256": "e" * 64,
                    "canonical_live_observed_end": "2026-07-30",
                    "canonical_live_observed_start": "2026-01-02",
                    "canonical_live_observations": 80,
                },
            }
        ]
    }


def test_canonical_series_identity_same_version_passes() -> None:
    """Same strategy_version on both evidence and dependence passes."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }
    factor = _parity_factor()
    weekly = _parity_weekly()

    contracts = panel._canonical_strategy_series_contracts(
        evidence, names, factor, dependence, weekly
    )
    assert len(contracts) == 1
    assert contracts[0]["strategy_version"] == _VERSION
    # Observation counts are still present in the contract (display-only).
    assert contracts[0]["return_observations"] == 223


def test_canonical_series_identity_different_version_fails() -> None:
    """Mismatched live versions fail closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version="version-a")}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", live_version="version-b")
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="dependence canonical-series version parity failed",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, _parity_factor(), dependence, _parity_weekly()
        )


def test_canonical_series_identity_full_version_divergence_passes() -> None:
    """The dependence full-series version may bind a declared history while the
    live identity still matches — parity is checked on live_version_ref."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence(
                "test-series",
                version="deprecated-no-history:2026-08-08",
                live_version=_VERSION,
            )
        }
    }

    contracts = panel._canonical_strategy_series_contracts(
        evidence,
        names,
        _parity_factor(),
        dependence,
        _parity_weekly(version="deprecated-no-history:2026-08-08"),
    )
    assert len(contracts) == 1
    assert contracts[0]["strategy_version"] == _VERSION


def test_canonical_series_identity_missing_live_version_fails() -> None:
    """Absent live_version_ref on dependence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", live_version=None)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="dependence live version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, _parity_factor(), dependence, _parity_weekly()
        )


def test_canonical_series_identity_missing_evidence_version_fails() -> None:
    """Absent version on evidence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=None)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="evidence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, _parity_factor(), dependence, _parity_weekly()
        )


def test_canonical_series_identity_malformed_evidence_version_fails() -> None:
    """A non-string evidence version fails closed, never coerced to a match.

    The dependence live identity is set to "123" so the ONLY guard that can
    catch this is the type check: a str() coercion would silently pass.
    """
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=123)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence(
                "test-series", version="123", live_version="123"
            )
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="evidence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, _parity_factor(), dependence, _parity_weekly()
        )


def test_canonical_series_identity_missing_dependence_version_fails() -> None:
    """Absent version on dependence fails closed with a named reason."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=None)
        }
    }

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="dependence strategy version is absent",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, _parity_factor(), dependence, _parity_weekly()
        )


def test_canonical_series_identity_counts_still_displayed() -> None:
    """Observation counts remain in the contract for display — the 223-vs-196
    case that was previously a false failure now passes because versions match."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence = {
        "series_composition": {
            "test-series": _parity_dependence("test-series", version=_VERSION)
        }
    }

    contracts = panel._canonical_strategy_series_contracts(
        evidence, names, _parity_factor(), dependence, _parity_weekly()
    )
    assert len(contracts) == 1
    # Evidence raw count 223, decomposition lens live_observations 80.
    # Both are preserved as metadata — no equality claimed.
    assert contracts[0]["return_observations"] == 223
    assert contracts[0]["strategy_version"] == _VERSION


def test_canonical_series_dependence_beyond_evidence_fails() -> None:
    """The canonical window extending past the evidenced live path fails closed."""
    evidence = {"strategy-0": _parity_evidence("strategy-0", version=_VERSION)}
    names = {"strategy-0": "test-series"}
    dependence_row = _parity_dependence("test-series", version=_VERSION)
    dependence_row["observed_end"] = "2026-08-14"  # one day past the evidence end
    factor = _parity_factor()
    factor["series"][0]["coverage"]["canonical_live_observed_end"] = "2026-08-14"
    weekly = _parity_weekly()
    weekly["series"][0]["observation_meta_by_mode"]["LIVE_ONLY"] = {
        "2026-08-14": ["2026-08-14", "2026-01-02", "2026-08-14"]
    }
    dependence = {"series_composition": {"test-series": dependence_row}}

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="extends beyond the evidenced live path",
    ):
        panel._canonical_strategy_series_contracts(
            evidence, names, factor, dependence, weekly
        )


def _governed_hwm_bundle() -> tuple[dict[str, Any], bytes, bytes, bytes]:
    """A real governed result + its three evidence artifacts.

    Uses mode='committed' so the bundle verifies against the production
    policy bytes the panel reads — exactly like the real derivation.
    """
    import json as _json
    import sys as _sys

    repo_tests = REPOSITORY_ROOT / "tests" / "portfolio_ledger"
    if str(repo_tests) not in _sys.path:
        _sys.path.insert(0, str(repo_tests))
    from test_hwm_governed_execution import (  # noqa: E402
        _fixture as _governed_fixture,
        _run as _governed_run,
    )

    fx = _governed_fixture(Path(tempfile.mkdtemp()), mode="committed")
    result = _governed_run(fx)
    return (
        json.loads(_json.dumps(result, default=str)),
        _json.dumps(fx["generation"], sort_keys=True).encode(),
        fx["calendar_authority_bytes"],
        fx["input_receipt_bytes"],
    )


def test_panel_reconciles_governed_proof(
    inputs: dict[str, Any], tmp_path: Path
) -> None:
    report, _catalog = _envelope_report(inputs, tmp_path)
    result, generation_bytes, calendar_bytes, receipt_bytes = _governed_hwm_bundle()
    proof = panel._panel_governed_hwm_proof(
        result, generation_bytes, calendar_bytes, receipt_bytes
    )
    report["hwm_governed_proof"] = proof
    _republish_report(inputs["report"], tmp_path, report)

    candidate = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        hwm_governed_result=result,
        hwm_governed_generation_bytes=generation_bytes,
        hwm_governed_calendar_bytes=calendar_bytes,
        hwm_governed_receipt_bytes=receipt_bytes,
    )
    data = _candidate_data(candidate)

    assert data["hwm_governed_proof"] == proof
    assert data["hwm_governed_proof"]["status"] == "AVAILABLE"
    assert data["hwm_governed_proof"]["input_authority_class"] == "ACCEPTED_GOVERNED"
    assert data["advisory"]["hwm_index"] != proof["current_index"]
    # Candidate verification reproduces the governed bundle end-to-end.
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(candidate["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            hwm_governed_result=result,
            hwm_governed_generation_bytes=generation_bytes,
            hwm_governed_calendar_bytes=calendar_bytes,
            hwm_governed_receipt_bytes=receipt_bytes,
        )["all_passed"]
        is True
    )


# ---------------------------------------------------------------------------
# Account x strategy epoch manifest (owner-mandated epoch continuity rule)
# ---------------------------------------------------------------------------


def _manifest_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": "account-manifest-test",
        "account_number": "U" + "9" * 8,
        "provider": "IBKR",
        "strategy_id": "strategy-a",
        "effective_start": "2026-01-01",
        "effective_end": None,
        "status": "OK",
        "start": "2026-01-02",
        "end": "2026-08-13",
        "opening_capital": "50000.00",
        "deposits": "0.00",
        "internal_in": "0.00",
        "invested": "50000.00",
        "external_withdrawals": "0.00",
        "internal_out": "0.00",
        "withdrawn": "0.00",
        "net_capital": "50000.00",
        "current_value": "56832.54",
        "gain": "6832.54",
        "mwr": "0.1367",
        "annualized_mwr": "0.2237",
        "twr": "0.1367",
        "hwm_index": "113.67",
        "max_drawdown": "-0.2289",
        "nav_observations": 160,
    }
    row.update(overrides)
    return row


def _write_manifest(path: Path, rows: list[dict[str, Any]]) -> Path:
    payload = {
        "schema_version": 1,
        "artifact": "account-strategy-manifest",
        "ledger_packet": "27cdeb832c0f36a05ff58d84b61a5b4f6aeb0aba80e28f696234fe58920f2a84",
        "epoch_continuity_rule": (
            "closing NAV of an ended assignment opens the successor assignment "
            "on the same account; flows on the first funded day belong to the "
            "opening, never double-counted"
        ),
        "reconciliation_source": "raw-ledger classifier (see reconciliation_disclosure)",
        "reconciliation_disclosure": [],
        "rows": rows,
    }
    path.write_bytes(canonical_json_bytes(payload))
    return path


def test_account_manifest_flows_through_build_and_verify(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    manifest_path = _write_manifest(tmp_path / "manifest.json", [_manifest_row()])
    built_with = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
    )
    data = _candidate_data(built_with)
    assert data["account_manifest"]["rows"][0]["gain"] == "6832.54"
    assert data["meta"]["account_manifest_sha256"] == panel._hash(manifest_path)
    # Determinism: identical artifact -> identical panel id.
    again = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
    )
    assert again["panel_id"] == built_with["panel_id"]

    # Verification reproduces the bound artifact.
    assert (
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            Path(built_with["candidate_dir"]),
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )["all_passed"]
        is True
    )

    # Renderer consumes the embedded manifest.
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "data.account_manifest" in renderer
    assert "renderAccountManifest" in renderer


def test_account_manifest_absent_leaves_panel_neutral(
    inputs: dict[str, Path],
) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
    )
    data = _candidate_data(built)
    assert data["account_manifest"] is None
    assert data["meta"]["account_manifest_sha256"] is None


def test_account_manifest_money_identity_fails_closed(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    broken_row = _manifest_row(gain="999.99")
    manifest_path = _write_manifest(tmp_path / "broken-manifest.json", [broken_row])
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="money identity differs",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )


def test_account_manifest_epoch_continuity_fails_closed(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    first = _manifest_row(strategy_id="strategy-a", current_value="56832.54")
    successor = _manifest_row(
        strategy_id="strategy-b",
        effective_start="2026-08-14",
        **{"opening_capital": "40000.00"},
        invested="40000.00",
        net_capital="40000.00",
        current_value="41000.00",
        gain="1000.00",
    )
    manifest_path = _write_manifest(
        tmp_path / "discontinuous-manifest.json", [first, successor]
    )
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="epoch continuity differs",
    ):
        panel.build_candidate(
            inputs["report"],
            inputs["legacy"],
            inputs["output"],
            freshness_health_path=inputs["freshness"],
            baseline_root=inputs["baseline"],
            account_manifest_path=manifest_path,
        )


def test_account_manifest_epoch_continuity_accepts_rollover(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    first = _manifest_row(strategy_id="strategy-a", current_value="56832.54")
    successor = _manifest_row(
        strategy_id="strategy-b",
        effective_start="2026-08-14",
        **{"opening_capital": "56832.54"},
        invested="56832.54",
        net_capital="56832.54",
        current_value="57100.00",
        gain="267.46",
    )
    manifest_path = _write_manifest(
        tmp_path / "continuous-manifest.json", [first, successor]
    )
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
    )
    data = _candidate_data(built)
    assert len(data["account_manifest"]["rows"]) == 2


def _produce_manifest(target_dir: Path, closing: str) -> Path:
    """Run the real producer CLI over synthetic governed inputs."""
    import subprocess

    target_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = target_dir / "ledger.json"
    _write_json(
        ledger_path,
        {
            "nav_observations": [
                {
                    "account_id": "account-seam",
                    "as_of_date": day,
                    "nav": {"status": "AVAILABLE", "value": value},
                }
                for day, value in (
                    ("2026-01-02", "50000"),
                    ("2026-03-01", "50500"),
                    ("2026-06-01", "51000"),
                    ("2026-08-13", closing),
                )
            ],
            "flows": [],
        },
    )
    baseline_root = target_dir / "baseline"
    generation_dir = baseline_root / "generations" / "seam-generation"
    generation_dir.mkdir(parents=True)
    (baseline_root / "CURRENT.json").write_text(
        '{"generation_id": "seam-generation"}', encoding="utf-8"
    )
    _write_json(
        generation_dir / "portfolio-baseline.json",
        {
            "accounts": [{"account_id": "account-seam", "provider": "SCHWAB"}],
            "assignments": [
                {
                    "account_id": "account-seam",
                    "strategy_id": "strategy-a",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                }
            ],
        },
    )
    flows_path = target_dir / "flow-classes.json"
    _write_json(flows_path, {"events": []})
    proc = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "tools" / "account_strategy_manifest.py"),
            "--ledger-json",
            str(ledger_path),
            "--baseline-root",
            str(baseline_root),
            "--flow-classes-json",
            str(flows_path),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    manifest_path = target_dir / "produced-manifest.json"
    manifest_path.write_text(proc.stdout, encoding="utf-8")
    return manifest_path


def test_account_manifest_producer_to_publish_seam(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    """Reviewer P1: the producer's OWN CLI output must survive build ->
    verify -> publish, and distinct manifests must yield distinct panels."""
    manifest_path = _produce_manifest(tmp_path / "primary", "56832.54")
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
    )
    candidate = Path(built["candidate_dir"])
    row = _candidate_data(built)["account_manifest"]["rows"][0]
    assert row["status"] == "OK"
    assert Decimal(row["invested"]) == Decimal("50000")
    assert Decimal(row["current_value"]) == Decimal("56832.54")
    assert Decimal(row["gain"]) == Decimal("56832.54") - Decimal("50000")

    qa = _qa(candidate, tmp_path / "qa-primary")
    published = panel.publish_panel(
        inputs["report"],
        inputs["legacy"],
        candidate,
        qa,
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=manifest_path,
    )
    assert published["publication_state"] == "PUBLISHED"

    # Distinct manifest bytes must produce a distinct panel id.
    other = _produce_manifest(tmp_path / "alternate", "57999.99")
    rebuilt = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
        baseline_root=inputs["baseline"],
        account_manifest_path=other,
    )
    assert rebuilt["panel_id"] != built["panel_id"]

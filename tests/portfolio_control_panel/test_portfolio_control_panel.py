from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
SPEC = importlib.util.spec_from_file_location("portfolio_control_panel", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio control-panel builder")
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)

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
                "value": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
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
            "correlation_one": {
                "components": [],
                "expected_loss": "0.37249726899852387",
                "loss": "0.37249726899852387",
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
            "strategy_version": "v1",
        }
        for series_id in STRATEGY_NAMES
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
                artifact_id="synthetic/portfolio-report-data.json",
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
        "us-sector": ("RETURN_INDUSTRY", {STRATEGY_NAMES[7]}),
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
    _publish_report(report_root, source_report, _report())
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
    return {
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
    )
    second = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
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
    assert data["default_dependence_mode"] == "LIVE_ONLY"
    assert list(data["dependence_modes"]) == ["LIVE_ONLY"]


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
    )
    candidate = Path(result["candidate_dir"])
    verification = panel.verify_candidate(
        inputs["report"],
        inputs["legacy"],
        candidate,
        decomposition_lenses_root=lens_root,
        decomposition_plan_bundle=plan_bundle,
        freshness_health_path=inputs["freshness"],
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


def test_layered_dependence_modes_are_preserved_without_changing_default_authority(
    inputs: dict[str, Path],
) -> None:
    _publish_analytics(
        inputs["correlation"],
        "correlation.json",
        _correlation_v2(),
        "synthetic-correlation-v2",
    )
    result = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
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
    )
    crlf = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        tmp_path / "crlf-output",
        crlf_template,
        freshness_health_path=inputs["freshness"],
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
        )


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
            "item": "synthetic:account-internal-1:nav",
            "known_gap": (
                "next expected 2026-07-29; expected refresh is late but not yet stale"
            ),
            "last_accepted_date": "2026-07-28",
            "next_expected_date": "2026-07-29",
            "policy_id": "daily-current",
            "provider_id": "synthetic",
            "reason": "expected refresh is late but not yet stale",
            "revision_state": "REVISED",
            "status": "LATE",
            "subject_id": "account-internal-1",
            "value": "2026-07-28",
        }
    ]


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
        },
    )

    rows = panel._freshness_health(health, "2026-08-03")

    assert rows[0]["status"] == "STALE"
    assert rows[0]["blocks_current_consumers"] is False
    assert rows[0]["escalation_required"] is False


def test_current_panel_fails_closed_without_freshness(inputs: dict[str, Path]) -> None:
    with pytest.raises(
        panel.ControlPanelValidationError, match="freshness health is mandatory"
    ):
        panel.build_candidate(inputs["report"], inputs["legacy"], inputs["output"])


def test_current_panel_fails_closed_without_fact_coverage_matrix(
    inputs: dict[str, Path], tmp_path: Path
) -> None:
    report = _report()
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
        )


def test_stale_governed_strategy_blocks_hwm_claim(inputs: dict[str, Path]) -> None:
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

    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
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
    )
    candidate = Path(result["candidate_dir"])
    (candidate / "app.js").write_text("tampered", encoding="utf-8")
    with pytest.raises(panel.ControlPanelValidationError, match="verification failed"):
        panel.verify_candidate(
            inputs["report"],
            inputs["legacy"],
            candidate,
            freshness_health_path=inputs["freshness"],
        )


def test_browser_qa_and_publication(inputs: dict[str, Path], tmp_path: Path) -> None:
    built = panel.build_candidate(
        inputs["report"],
        inputs["legacy"],
        inputs["output"],
        freshness_health_path=inputs["freshness"],
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


def test_renderer_keeps_exact_hwm_dollars_and_accessible_table_headers() -> None:
    renderer = (panel.DEFAULT_TEMPLATE_ROOT / "app.js").read_text(encoding="utf-8")

    assert "const moneyExact = new Intl.NumberFormat" in renderer
    assert "currencyExact(data.hwm_summary.uncovered_value_usd)" in renderer
    assert "no numeric HWM is calculated while unavailable" in renderer
    assert '<th scope="col"' in renderer
    assert '<th scope="row"' in renderer
    assert "0.00x" not in renderer
    assert "data.panel_schema_version !== 10" in renderer


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
    assert "Clean-room dynamic FLS path and same-factor validation are rendered" in qa
    assert "All observed windows and both true drawdown regimes are exercised" in qa
    assert "Every weekly display group is rendered and exercised" in qa
    assert "Dependence live and hybrid evidence disclosures are exercised" in qa
    assert "Weekly returns disclose live and hybrid strategy evidence" in qa
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

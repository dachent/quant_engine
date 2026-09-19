from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, cast

import numpy as np
import pytest

from quant_engine.core import read_current


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_decomposition.py"
SPEC = importlib.util.spec_from_file_location("portfolio_decomposition", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio decomposition builder")
decomposition = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = decomposition
SPEC.loader.exec_module(decomposition)


def _days(count: int, start: date = date(2025, 1, 2)) -> list[date]:
    accepted: list[date] = []
    current = start
    while len(accepted) < count:
        if current.weekday() < 5:
            accepted.append(current)
        current += timedelta(days=1)
    return accepted


def _hash(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _write_sources(
    root: Path,
    *,
    count: int = 140,
    start: date = date(2025, 1, 2),
    formula: str = "unconstrained",
    identical_factors: bool = False,
    target_only_dates: tuple[date, ...] = (),
    mixed_layer: bool = False,
    complete_costs: bool = False,
) -> tuple[Path, list[date]]:
    root.mkdir(parents=True)
    days = _days(count, start)
    target_path = root / "targets.csv"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(decomposition.TARGET_HEADERS)
        for index, day in enumerate([*days, *target_only_dates]):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = f1 if identical_factors else 0.006 * math.cos(index / 7.0)
            if formula == "simplex":
                excess = 0.0002 + 0.7 * f1 + 0.3 * f2
            elif formula == "single":
                excess = 0.0001 + 0.8 * f1
            else:
                excess = 0.0001 + 0.6 * f1 - 0.2 * f2
            layer = "BACKTEST" if mixed_layer and index == count - 1 else "ACCOUNT_LIVE"
            writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + 0.00005, ".17g"),
                    layer,
                    "NET",
                    "TOTAL",
                    "0.00001" if complete_costs else "",
                ]
            )
    factor_path = root / "factors.csv"
    with factor_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = f1 if identical_factors else 0.006 * math.cos(index / 7.0)
            writer.writerow(
                [day.isoformat(), "0.00005", format(f1, ".17g"), format(f2, ".17g")]
            )
    return target_path, days


def _write_later_target_source(
    root: Path,
) -> tuple[Path, list[date], list[date]]:
    """Keep target lifetime later than the selected factor history."""
    target_path, days = _write_sources(root, start=date(2026, 1, 2))
    factor_path = root / "factors.csv"
    historical_days = _days(40, date(2024, 1, 2))
    factor_rows = list(csv.reader(factor_path.read_text(encoding="utf-8").splitlines()))
    factor_rows[1:1] = [
        [
            observed.isoformat(),
            "0.00005",
            "0.001",
            "0.002",
        ]
        for observed in historical_days
    ]
    with factor_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(factor_rows)
    return target_path, days, historical_days


def _model(
    days: list[date],
    *,
    model_id: str = "model-two-factor",
    factors: list[str] | None = None,
    constraint: str = "UNCONSTRAINED",
    cost_coverage: str = "INCOMPLETE",
    minimum_train: int = 40,
    minimum_test: int = 20,
) -> dict[str, object]:
    return {
        "constraint": constraint,
        "cost_coverage": cost_coverage,
        "cv_folds": 3,
        "factor_ids": factors or ["factor-one", "factor-two"],
        "minimum_test_observations": minimum_test,
        "minimum_train_observations": minimum_train,
        "model_id": model_id,
        "parsimony_tolerance": "0.000001",
        "periods_per_year": 252,
        "ridge_candidates": ["0", "0.000001", "0.0001"],
        "series_id": "synthetic-strategy",
        "test_start": days[90].isoformat(),
        "train_end": days[89].isoformat(),
    }


def _dynamic_model(days: list[date]) -> dict[str, object]:
    model = _model(
        days,
        model_id="dynamic-fls",
        minimum_train=60,
        minimum_test=20,
    )
    model.update(
        {
            "estimator": "DYNAMIC_FLS",
            "ridge_candidates": ["0.0000000001"],
            "smoothness_candidates": [
                "0.00000001",
                "0.0000001",
                "0.000001",
                "0.00001",
                "0.0001",
                "0.001",
            ],
            "test_start": days[105].isoformat(),
            "train_end": days[104].isoformat(),
        }
    )
    return model


def _write_dynamic_sources(
    root: Path,
) -> tuple[Path, list[date], list[tuple[float, float]]]:
    root.mkdir(parents=True)
    days = _days(150)
    truth: list[tuple[float, float]] = []
    target_path = root / "targets.csv"
    factor_path = root / "factors.csv"
    with (
        target_path.open("w", encoding="utf-8", newline="") as target_handle,
        factor_path.open("w", encoding="utf-8", newline="") as factor_handle,
    ):
        target_writer = csv.writer(target_handle)
        factor_writer = csv.writer(factor_handle)
        target_writer.writerow(decomposition.TARGET_HEADERS)
        factor_writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.012 * math.sin(index / 3.7) + 0.004 * math.cos(index / 13.0)
            f2 = 0.010 * math.cos(index / 5.1) - 0.003 * math.sin(index / 17.0)
            beta_one = 0.15 + 0.70 * index / (len(days) - 1)
            beta_two = -0.35 + 0.30 * math.sin(index / 28.0)
            alpha = 0.00015
            risk_free = 0.00005
            excess = alpha + beta_one * f1 + beta_two * f2
            truth.append((beta_one, beta_two))
            target_writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + risk_free, ".17g"),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "",
                ]
            )
            factor_writer.writerow(
                [
                    day.isoformat(),
                    format(risk_free, ".17g"),
                    format(f1, ".17g"),
                    format(f2, ".17g"),
                ]
            )
    return target_path, days, truth


def _spec(
    root: Path,
    target_path: Path,
    days: list[date],
    *,
    models: list[dict[str, object]] | None = None,
    maximum_staleness_days: int = 7,
    analysis_lens: str | None = None,
) -> Path:
    factor_path = root / "factors.csv"
    target_hash, target_size = _hash(target_path)
    factor_hash, factor_size = _hash(factor_path)
    value = {
        "as_of_date": max(days).isoformat(),
        "factor_source": {
            "artifact_id": "frozen/factors.csv",
            "interval_policy": {
                "available_through_anchor": max(days).isoformat(),
                "currency": "USD",
                "finality": "COMPLETE_SELECTED_SNAPSHOT",
                "native_calendar": "SOURCE_LABELLED_DAILY",
                "native_label_convention": "PUBLISHED_DAILY_LABEL",
                "return_basis": "NET",
                "source_vintage": "SELECTED_SNAPSHOT",
            },
            "date_column": "date",
            "expected_sha256": factor_hash,
            "expected_size_bytes": factor_size,
            "factors": [
                {
                    "column": "F1",
                    "factor_id": "factor-one",
                    "return_type": "EXCESS",
                },
                {
                    "column": "F2",
                    "factor_id": "factor-two",
                    "return_type": "EXCESS",
                },
            ],
            "relative_path": "factors.csv",
            "risk_free_column": "RF",
            "schema_name": "synthetic-factor-returns",
            "schema_version": "1",
        },
        "maximum_factor_staleness_days": maximum_staleness_days,
        "models": models or [_model(days)],
        "schema_version": 1,
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "expected_sha256": target_hash,
            "expected_size_bytes": target_size,
            "relative_path": "targets.csv",
            "schema_name": "synthetic-target-returns",
            "schema_version": "1",
            "interval_policy": {
                "currency": "USD",
                "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
                "finality": "FINALIZED_THROUGH_DECLARED_LABEL",
                "finalized_through": max(days).isoformat(),
                "gap_disposition": "REJECT_BUCKET",
                "native_calendar": "SESSION_LABELLED",
                "native_label_convention": "SYNTHETIC_EXCHANGE_SESSION_DATE",
                "return_basis": "NET",
                "session_schedule_labels": {
                    "synthetic-strategy": [
                        item.isoformat() for item in sorted(set(days))
                    ]
                },
                "source_vintage": "SELECTED_SNAPSHOT",
            },
        },
    }
    if analysis_lens is not None:
        value["analysis_lens"] = analysis_lens
    spec_path = root / "spec.json"
    spec_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_24_7_factor_anchor_association_compounds_weekends_and_emits_receipt(
    tmp_path: Path,
) -> None:
    """The real producer must retain Sat/Sun under Monday's one factor row."""
    source = tmp_path / "source"
    source.mkdir()
    anchors = _days(18, date(2026, 1, 2))
    target_path = source / "targets.csv"
    factor_path = source / "factors.csv"
    factor_values = {
        observed: (
            0.012 * math.sin(index / 2.0) + 0.003 * math.cos(index / 5.0),
            0.009 * math.cos(index / 3.0) - 0.002 * math.sin(index / 7.0),
        )
        for index, observed in enumerate(anchors)
    }
    target_returns: dict[date, float] = {}
    for left, right in zip(anchors, anchors[1:]):
        target_labels = [
            left + timedelta(days=offset)
            for offset in range(1, (right - left).days + 1)
        ]
        for label in target_labels[:-1]:
            target_returns.setdefault(label, 0.0)
        if right == date(2026, 1, 5):
            target_returns[date(2026, 1, 3)] = 0.02
            target_returns[date(2026, 1, 4)] = -0.01
        desired_excess = (
            0.0005 + 0.55 * factor_values[right][0] - 0.30 * factor_values[right][1]
        )
        preceding_growth = math.prod(
            1.0 + target_returns[label] for label in target_labels[:-1]
        )
        target_returns[right] = (1.0 + desired_excess + 0.001) / preceding_growth - 1.0
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(decomposition.TARGET_HEADERS)
        for observed, value in sorted(target_returns.items()):
            writer.writerow(
                [
                    "synthetic-strategy",
                    observed.isoformat(),
                    str(value),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "",
                ]
            )
    with factor_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "RF", "F1", "F2"])
        for observed in anchors:
            writer.writerow(
                [
                    observed.isoformat(),
                    "0.001",
                    format(factor_values[observed][0], ".17g"),
                    format(factor_values[observed][1], ".17g"),
                ]
            )
    models = [
        {
            "constraint": "UNCONSTRAINED",
            "cost_coverage": "INCOMPLETE",
            "cv_folds": 2,
            "factor_ids": ["factor-one", "factor-two"],
            "minimum_test_observations": 1,
            "minimum_train_observations": 10,
            "model_id": "anchor-model",
            "parsimony_tolerance": "0.000001",
            "periods_per_year": 252,
            "ridge_candidates": ["0.0001"],
            "series_id": "synthetic-strategy",
            "test_start": anchors[-5].isoformat(),
            "train_end": anchors[-6].isoformat(),
        }
    ]
    spec_path = _spec(source, target_path, anchors, models=models)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["target_source"]["interval_policy"].update(
        {
            "native_calendar": "CALENDAR_DAY_24_7",
            "native_label_convention": "UTC_CALENDAR_DATE",
        }
    )
    spec["target_source"]["interval_policy"].pop("session_schedule_labels")
    spec_path.write_text(json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n")

    result, _manifest, _content = decomposition.build_decomposition(spec_path, source)

    model = _available(result)
    interval = model["interval_association"]
    monday = next(
        item for item in interval["buckets"] if item["factor_anchor"] == "2026-01-05"
    )
    assert monday["target_labels"] == ["2026-01-03", "2026-01-04", "2026-01-05"]
    assert float(monday["target_total_return"]) == pytest.approx(
        math.prod(
            1.0 + target_returns[date.fromisoformat(label)]
            for label in monday["target_labels"]
        )
        - 1.0
    )
    assert float(monday["anchor_adjusted_target_return"]) == pytest.approx(
        float(monday["target_total_return"]) - 0.001
    )
    assert monday["partition"] == "TRAIN"
    assert interval["policy_id"] == "SOURCE_LABELLED_FACTOR_ANCHOR_ASSOCIATION_V1"
    assert interval["holdout_display_claim"] == (
        "holdout association using selected historical snapshot"
    )


def test_later_target_lifetime_uses_prior_factor_anchor_without_early_buckets(
    tmp_path: Path,
) -> None:
    """Earlier factor history is not an implied earlier target lifetime."""
    source = tmp_path / "source"
    target_path, days, historical_days = _write_later_target_source(source)
    spec_path = _spec(source, target_path, days)

    result, _manifest, _content = decomposition.build_decomposition(spec_path, source)

    interval = _available(result)["interval_association"]
    buckets = cast(list[dict[str, Any]], interval["buckets"])
    assert buckets[0]["prior_factor_anchor"] == historical_days[-1].isoformat()
    assert buckets[0]["target_labels"] == [days[0].isoformat()]
    assert all(item["factor_anchor"] >= days[0].isoformat() for item in buckets)
    assert interval["pre_anchor_labels"] == []
    assert interval["pending_factor_anchor_labels"] == []
    assert interval["unassignable_leading_labels"] == []
    assert interval["unassignable_tail_labels"] == []
    assert interval["target_source_lifetime_start"] == days[0].isoformat()
    assert interval["target_source_lifetime_end"] == days[-1].isoformat()


def test_later_target_lifetime_rejects_gap_inside_declared_source_bucket(
    tmp_path: Path,
) -> None:
    """A source boundary does not excuse a missing declared target label."""
    source = tmp_path / "source"
    target_path, days, _historical_days = _write_later_target_source(source)
    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    missing_day = days[20].isoformat()
    rows = [row for row in rows if len(row) < 2 or row[1] != missing_day]
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    spec_path = _spec(source, target_path, days)

    with pytest.raises(
        decomposition.DecompositionError,
        match=f"missing expected target label\\(s\\).*{missing_day}",
    ):
        decomposition.build_decomposition(spec_path, source)


def _available(result: dict[str, object]) -> dict[str, Any]:
    models = cast(list[dict[str, Any]], result["model_results"])
    selected = [item for item in models if item["selected_for_series"]]
    assert len(selected) == 1
    assert selected[0]["status"] == "AVAILABLE"
    return selected[0]


def _selected(result: dict[str, object]) -> dict[str, Any]:
    models = cast(list[dict[str, Any]], result["model_results"])
    selected = [item for item in models if item["selected_for_series"]]
    assert len(selected) == 1
    return selected[0]


def _independent_dynamic_loo_predictions(
    matrix: np.ndarray,
    target: np.ndarray,
    *,
    smoothness: float,
    ridge: float,
) -> np.ndarray:
    """Solve each penalized leave-one-out problem without production helpers."""
    observation_count, factor_count = matrix.shape
    parameter_count = 1 + observation_count * factor_count
    design = np.zeros((observation_count, parameter_count), dtype=np.float64)
    design[:, 0] = 1.0
    for index, factor_row in enumerate(matrix):
        start = 1 + index * factor_count
        design[index, start : start + factor_count] = factor_row

    ridge_penalty = np.zeros((observation_count * factor_count, parameter_count))
    ridge_penalty[:, 1:] = math.sqrt(ridge) * np.eye(observation_count * factor_count)
    smoothness_penalty = np.zeros(
        ((observation_count - 1) * factor_count, parameter_count)
    )
    for index in range(observation_count - 1):
        start = 1 + index * factor_count
        next_start = start + factor_count
        for factor in range(factor_count):
            row = index * factor_count + factor
            smoothness_penalty[row, start + factor] = math.sqrt(smoothness)
            smoothness_penalty[row, next_start + factor] = -math.sqrt(smoothness)

    predictions = []
    for omitted in range(observation_count):
        retained = np.ones(observation_count, dtype=bool)
        retained[omitted] = False
        coefficients, *_ = np.linalg.lstsq(
            np.vstack(
                (
                    design[retained],
                    ridge_penalty,
                    smoothness_penalty,
                )
            ),
            np.concatenate(
                (
                    target[retained],
                    np.zeros(len(ridge_penalty) + len(smoothness_penalty)),
                )
            ),
            rcond=None,
        )
        predictions.append(float(design[omitted] @ coefficients))
    return np.asarray(predictions, dtype=np.float64)


def test_dynamic_press_predictions_equal_direct_leave_one_out() -> None:
    matrix = np.asarray(
        [[math.sin(index / 2), math.cos(index / 3)] for index in range(12)],
        dtype=np.float64,
    )
    target = np.asarray(
        [0.001 + 0.4 * row[0] - 0.2 * row[1] for row in matrix],
        dtype=np.float64,
    )
    _score, optimized = decomposition._dynamic_loo_score(
        matrix,
        target,
        smoothness=0.01,
        ridge=0.0001,
    )
    expected = _independent_dynamic_loo_predictions(
        matrix,
        target,
        smoothness=0.01,
        ridge=0.0001,
    )

    assert optimized == pytest.approx(expected, abs=1e-10)


def test_unconstrained_truth_alpha_and_contributions_reconcile(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target_path, days = _write_sources(source)
    spec_path = _spec(source, target_path, days)

    first, manifest, first_bytes = decomposition.build_decomposition(spec_path, source)
    second, second_manifest, second_bytes = decomposition.build_decomposition(
        spec_path, source
    )

    assert first_bytes == second_bytes
    assert manifest.generation_id == second_manifest.generation_id
    assert first["analysis_lens"] == "HISTORICAL_MINIMUM_PROOF"
    model = _available(first)
    exposures = {
        item["factor_id"]: float(item["exposure"]) for item in model["exposures"]
    }
    assert exposures == pytest.approx({"factor-one": 0.6, "factor-two": -0.2}, abs=1e-9)
    assert {
        item["factor_id"]: float(item["beta"]) for item in model["exposures"]
    } == pytest.approx(exposures, abs=1e-12)
    assert float(model["alpha"]["periodic_intercept"]) == pytest.approx(
        0.0001, abs=1e-10
    )
    assert model["alpha"]["net_status"] == "AVAILABLE_OBSERVED_BASIS"
    assert model["alpha"]["gross_status"] == "UNAVAILABLE_INCOMPLETE_COST_COVERAGE"
    assert decomposition._annualize_coefficient_standard_error(
        0.0004, 252
    ) == pytest.approx(0.1008)
    assert float(
        model["diagnostics"]["holdout"]["predicted_r_squared"]
    ) == pytest.approx(1.0, abs=1e-9)
    training_design = model["diagnostics"]["training_design"]
    assert training_design["basis"] == "TRAIN_ONLY"
    assert training_design["matrix_rank"] == 2
    assert training_design["unregularized_status"] == "ELIGIBLE"
    assert training_design["vif_status"] == "AVAILABLE"
    assert training_design["transformation"]["fitted_basis"] == (
        "RAW_CONFIGURED_FACTOR_RETURNS"
    )
    assert model["method_label"] == "MPI_INFORMED_NON_EQUIVALENT"
    inference = model["diagnostics"]["inference"]
    assert inference["status"] == (
        "AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION_NO_CLASSICAL_CONFIDENCE"
    )
    assert inference["default_lag"] == decomposition._newey_west_lag(90)
    assert {row["lag"] for row in inference["lag_sensitivity"]} >= {0}
    assert model["alpha"]["confidence_status"] == inference["status"]
    # The first factor label is a named pre-anchor tail, not a fitted bucket.
    assert model["diagnostics"]["residuals"]["train"]["observation_count"] == 89
    assert model["coverage"]["target_observed_start"] == days[0].isoformat()
    assert model["coverage"]["target_cumulative_return"] is not None
    assert model["coverage"]["target_observed_end"] == days[-1].isoformat()
    assert model["coverage"]["factor_observed_start"] == days[0].isoformat()
    assert model["coverage"]["factor_observed_end"] == days[-1].isoformat()
    assert model["coverage"]["factor_observations"] == len(days)
    assert model["coverage"]["limiting_input"] == "COMMON_END_DATE"
    assert model["coverage"]["source_lag_calendar_days"] == 0
    assert 0 <= float(model["coverage"]["target_current_drawdown"]) < 1
    assert float(model["coverage"]["target_max_drawdown"]) >= float(
        model["coverage"]["target_current_drawdown"]
    )
    for row in model["contributions"]:
        reconstructed = (
            float(row["alpha_contribution"])
            + sum(float(value) for value in row["factor_contributions"].values())
            + float(row["residual"])
        )
        assert reconstructed == pytest.approx(
            float(row["actual_excess_return"]), abs=5e-11
        )


def test_selected_static_model_emits_separate_train_only_orthogonal_attribution(
    tmp_path: Path,
) -> None:
    """Removing the post-fit surface must not change the raw model receipt."""
    source = tmp_path / "source"
    target_path, days = _write_sources(source)

    result, _manifest, _content = decomposition.build_decomposition(
        _spec(source, target_path, days), source
    )

    model = _available(result)
    orthogonal = model["orthogonal_attribution"]
    assert orthogonal["status"] == "AVAILABLE"
    assert orthogonal["method_id"] == "TRAINING_ONLY_SYMMETRIC_COVARIANCE_WHITENING_V1"
    assert orthogonal["raw_factor_ids"] == ["factor-one", "factor-two"]
    assert orthogonal["mode_ids"] == [
        "symmetric_mode:factor-one",
        "symmetric_mode:factor-two",
    ]
    assert orthogonal["training_row_identity"]["partition"] == "TRAIN"
    assert orthogonal["train_fitted_risk_budget"]["interpretation"] == (
        "EQUAL_FACTOR_ANCHOR_FITTED_COMPONENT_SAMPLE_VARIANCE"
    )
    assert (
        orthogonal["train_fitted_risk_budget"]["cross_mode_covariance_present"] is False
    )
    assert all(
        float(row["fitted_excess_return"])
        == pytest.approx(float(row["fitted_excess_return"]), abs=5e-11)
        for row in model["contributions"]
    )
    for raw, projected in zip(
        model["contributions"], orthogonal["per_date"], strict=True
    ):
        assert projected["date"] == raw["date"]
        assert projected["fitted_excess_return"] == raw["fitted_excess_return"]


def test_manifest_binds_exact_returned_decomposition_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target_path, days = _write_sources(source)
    spec_path = _spec(source, target_path, days)

    result, manifest, result_bytes = decomposition.build_decomposition(
        spec_path, source
    )

    artifacts = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "analytics/decomposition.json"
    ]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.sha256 == hashlib.sha256(result_bytes).hexdigest()
    assert artifact.size_bytes == len(result_bytes)
    assert artifact.as_of_date == date.fromisoformat(str(result["as_of_date"]))
    assert artifact.schema_name == "portfolio-factor-decomposition"
    assert artifact.schema_version == str(result["analytics_schema_version"])


def test_hac_and_residual_diagnostics_capture_serial_and_scale_dependence() -> None:
    count = 240
    index = np.arange(count, dtype=np.float64)
    matrix = np.column_stack((np.sin(index / 9.0), np.cos(index / 17.0))).astype(
        np.float64
    )
    innovations = 0.0002 * (1.0 + np.abs(matrix[:, 0])) * np.sin(index / 2.7)
    residual = np.zeros(count, dtype=np.float64)
    for position in range(1, count):
        residual[position] = 0.75 * residual[position - 1] + innovations[position]
    target = 0.0001 + matrix @ np.asarray([0.5, -0.25]) + residual
    fit = decomposition._fit(matrix, target, constraint="UNCONSTRAINED", ridge=0.0)

    inference, standard_errors = decomposition._hac_inference(
        matrix, target, fit, 0.0, "UNCONSTRAINED"
    )
    diagnostics = decomposition._residual_diagnostics(
        target, decomposition._predict(fit, matrix)
    )

    assert inference["status"] == (
        "AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION_NO_CLASSICAL_CONFIDENCE"
    )
    assert standard_errors is not None
    assert len(inference["lag_sensitivity"]) >= 2
    assert abs(float(diagnostics["autocorrelation"]["1"])) > 0.5
    assert diagnostics["durbin_watson"] is not None

    unavailable, absent = decomposition._hac_inference(
        matrix, target, fit, 0.0001, "UNCONSTRAINED"
    )
    assert unavailable["status"] == "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS"
    assert absent is None


def test_explicit_analysis_lens_is_hash_bound_and_validated(tmp_path: Path) -> None:
    source = tmp_path / "lens"
    target_path, days = _write_sources(source)
    style_spec = _spec(
        source,
        target_path,
        days,
        analysis_lens="ETF_STYLE",
    )
    style, style_manifest, _content = decomposition.build_decomposition(
        style_spec, source
    )
    assert style["analysis_lens"] == "ETF_STYLE"

    economic_spec = _spec(
        source,
        target_path,
        days,
        analysis_lens="ECONOMIC_FACTOR",
    )
    economic, economic_manifest, _content = decomposition.build_decomposition(
        economic_spec, source
    )
    assert economic["analysis_lens"] == "ECONOMIC_FACTOR"
    assert economic_manifest.generation_id != style_manifest.generation_id

    invalid = json.loads(economic_spec.read_text(encoding="utf-8"))
    invalid["analysis_lens"] = "MEGA_MODEL"
    economic_spec.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(decomposition.DecompositionError, match="analysis_lens"):
        decomposition.build_decomposition(economic_spec, source)


def test_simplex_and_parsimony_selection_recover_known_truth(tmp_path: Path) -> None:
    source = tmp_path / "simplex"
    target_path, days = _write_sources(source, formula="simplex")
    models = [
        _model(days, model_id="one-factor", factors=["factor-one"]),
        _model(
            days,
            model_id="simplex-two-factor",
            constraint="LONG_ONLY_SUM_TO_ONE",
        ),
    ]
    spec_path = _spec(source, target_path, days, models=models)

    result, _manifest, _content = decomposition.build_decomposition(spec_path, source)
    model = next(
        item
        for item in cast(list[dict[str, Any]], result["model_results"])
        if item["model_id"] == "simplex-two-factor"
    )
    exposures = [float(item["exposure"]) for item in model["exposures"]]
    assert exposures == pytest.approx([0.7, 0.3], abs=1e-8)
    assert sum(exposures) == pytest.approx(1.0, abs=1e-12)
    assert model["alpha"]["confidence_status"] == "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS"

    single_source = tmp_path / "parsimonious"
    single_target, single_days = _write_sources(single_source, formula="single")
    single_models = [
        _model(single_days, model_id="one-factor", factors=["factor-one"]),
        _model(single_days, model_id="two-factor"),
    ]
    single_spec = _spec(single_source, single_target, single_days, models=single_models)
    single_result, _manifest, _content = decomposition.build_decomposition(
        single_spec, single_source
    )
    selection = cast(list[dict[str, Any]], single_result["model_selections"])[0]
    assert selection["model_id"] == "one-factor"
    assert selection["factor_count"] == 1


def test_holdout_is_not_used_to_fit_or_select_model_or_ridge(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target_path, days = _write_sources(source)
    models = [
        _model(days, model_id="one-factor", factors=["factor-one"]),
        _model(days, model_id="two-factor"),
    ]
    spec_path = _spec(source, target_path, days, models=models)
    original, _manifest, _content = decomposition.build_decomposition(spec_path, source)
    original_model = _available(original)

    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    test_start = days[90].isoformat()
    for row in rows[1:]:
        if row[1] >= test_start:
            row[2] = format(-float(row[2]), ".17g")
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    changed_spec = _spec(source, target_path, days, models=models)
    changed, _manifest, _content = decomposition.build_decomposition(
        changed_spec, source
    )
    changed_model = _selected(changed)

    assert changed_model["model_id"] == original_model["model_id"]
    assert changed_model["selected_ridge"] == original_model["selected_ridge"]
    assert (
        changed_model["diagnostics"]["training_design"]
        == (original_model["diagnostics"]["training_design"])
    )
    assert changed_model["status"] == "DIAGNOSTIC_ONLY"
    assert changed_model["computational_status"] == "AVAILABLE"
    assert changed_model["model_quality"] == {
        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
        "decision_status": "DIAGNOSTIC_ONLY",
        "gate_reason": "NON_POSITIVE_SEALED_HOLDOUT_PREDICTED_R2",
        "holdout_predicted_r_squared": changed_model["diagnostics"]["holdout"][
            "predicted_r_squared"
        ],
    }
    assert changed_model["exposures"] == []
    assert changed_model["alpha"]["net_annualized"] is None
    assert changed_model["alpha"]["net_status"] == (
        "DIAGNOSTIC_ONLY_MODEL_QUALITY_GATE"
    )
    assert [
        item["exposure"] for item in changed_model["diagnostic_outputs"]["exposures"]
    ] == [item["exposure"] for item in original_model["exposures"]]
    assert float(changed_model["diagnostics"]["holdout"]["predicted_r_squared"]) < 0
    changed_selection = cast(list[dict[str, Any]], changed["model_selections"])[0]
    assert changed_selection["status"] == ("SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL")


def test_design_thresholds_match_the_governed_candidate_catalog() -> None:
    catalog = json.loads(
        (
            REPOSITORY_ROOT / "docs" / "analytics" / "decomposition-source-catalog.json"
        ).read_text(encoding="utf-8")
    )
    policy = catalog["selection_policy"]
    assert decomposition.NEAR_DUPLICATE_ABSOLUTE_CORRELATION == float(
        policy["near_duplicate_absolute_correlation"]
    )
    assert decomposition.UNREGULARIZED_VIF_LIMIT == float(
        policy["unregularized_vif_limit"]
    )
    assert decomposition.UNREGULARIZED_CONDITION_NUMBER_LIMIT == float(
        policy["unregularized_condition_number_limit"]
    )


def test_coverage_separates_missing_weekday_and_weekend_and_short_history(
    tmp_path: Path,
) -> None:
    missing_weekday = date(2025, 7, 21)
    missing_weekend = date(2025, 7, 19)
    source = tmp_path / "source"
    target_path, days = _write_sources(
        source, target_only_dates=(missing_weekday, missing_weekend)
    )
    models = [
        _model(days, model_id="available"),
        _model(days, model_id="short", minimum_train=200),
    ]
    spec_path = _spec(
        source,
        target_path,
        [*days, missing_weekday, missing_weekend],
        models=models,
    )
    result, _manifest, _content = decomposition.build_decomposition(spec_path, source)
    by_id = {
        item["model_id"]: item
        for item in cast(list[dict[str, Any]], result["model_results"])
    }
    # Under the declared session schedule, both labels belong to their right
    # factor-anchor bucket; neither is an exact-date factor-missing observation.
    assert by_id["available"]["coverage"]["factor_missing_weekdays"] == 0
    assert by_id["available"]["coverage"]["factor_missing_weekends"] == 0
    assert by_id["short"]["status"] == "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY"


def test_singular_stale_cost_and_evidence_fail_closed(tmp_path: Path) -> None:
    singular_root = tmp_path / "singular"
    singular_target, days = _write_sources(singular_root, identical_factors=True)
    singular_spec = _spec(singular_root, singular_target, days)
    singular_result, _manifest, _content = decomposition.build_decomposition(
        singular_spec, singular_root
    )
    singular_model = _available(singular_result)
    assert singular_model["diagnostics"]["rank_status"] == "SINGULAR_PSEUDOINVERSE"
    assert float(singular_model["selected_ridge"]) > 0
    singular_design = singular_model["diagnostics"]["training_design"]
    assert singular_design["matrix_rank"] == 1
    assert singular_design["unregularized_status"] == "BLOCKED_COLLINEAR_DESIGN"
    assert singular_design["near_duplicate_pairs"][0]["factor_ids"] == [
        "factor-one",
        "factor-two",
    ]
    assert singular_model["ridge_sensitivity"][0]["eligibility"] == (
        "BLOCKED_COLLINEAR_UNREGULARIZED_DESIGN"
    )
    assert (
        singular_model["alpha"]["confidence_status"]
        == "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS"
    )

    blocked_root = tmp_path / "blocked-singular"
    blocked_target, blocked_days = _write_sources(blocked_root, identical_factors=True)
    blocked_model = _model(blocked_days)
    blocked_model["ridge_candidates"] = ["0"]
    blocked_spec = _spec(
        blocked_root,
        blocked_target,
        blocked_days,
        models=[blocked_model],
    )
    blocked_result, _manifest, _content = decomposition.build_decomposition(
        blocked_spec, blocked_root
    )
    blocked = cast(list[dict[str, Any]], blocked_result["model_results"])[0]
    assert blocked["status"] == "UNAVAILABLE_COLLINEAR_UNREGULARIZED_DESIGN"
    assert blocked["diagnostics"]["training_design"]["vif_status"] == (
        "UNAVAILABLE_SINGULAR_OR_CONSTANT_DESIGN"
    )

    stale_root = tmp_path / "stale"
    stale_target, stale_days = _write_sources(
        stale_root,
        target_only_dates=(date(2026, 2, 2),),
    )
    stale_spec = _spec(
        stale_root,
        stale_target,
        [*stale_days, date(2026, 2, 2)],
        maximum_staleness_days=1,
    )
    stale_result, _manifest, _content = decomposition.build_decomposition(
        stale_spec, stale_root
    )
    assert cast(list[dict[str, Any]], stale_result["model_results"])[0]["status"] == (
        "UNAVAILABLE_STALE_FACTORS"
    )

    costs_root = tmp_path / "costs"
    costs_target, cost_days = _write_sources(costs_root)
    costs_spec = _spec(
        costs_root,
        costs_target,
        cost_days,
        models=[_model(cost_days, cost_coverage="COMPLETE")],
    )
    with pytest.raises(decomposition.DecompositionError, match="COMPLETE costs"):
        decomposition.build_decomposition(costs_spec, costs_root)

    mixed_root = tmp_path / "mixed"
    mixed_target, mixed_days = _write_sources(mixed_root, mixed_layer=True)
    mixed_spec = _spec(mixed_root, mixed_target, mixed_days)
    with pytest.raises(decomposition.DecompositionError, match="cannot mix"):
        decomposition.build_decomposition(mixed_spec, mixed_root)


def test_missing_risk_free_and_candidate_drift_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target_path, days = _write_sources(source)
    factor_path = source / "factors.csv"
    rows = list(csv.reader(factor_path.read_text(encoding="utf-8").splitlines()))
    rows[5][1] = ""
    with factor_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    spec_path = _spec(source, target_path, days)
    with pytest.raises(decomposition.DecompositionError, match="risk free"):
        decomposition.build_decomposition(spec_path, source)

    clean_root = tmp_path / "clean"
    clean_target, clean_days = _write_sources(clean_root, complete_costs=True)
    clean_spec = _spec(
        clean_root,
        clean_target,
        clean_days,
        models=[_model(clean_days, cost_coverage="COMPLETE")],
    )
    output = tmp_path / "analytics"
    staged = decomposition.stage_candidate(clean_spec, clean_root, output)
    published = decomposition.publish(clean_spec, clean_root, output)
    pointer = read_current(output)
    assert published["publication_state"] == "PUBLISHED"
    assert pointer.generation_id == staged["generation_id"]

    candidate = Path(cast(str, staged["candidate"])) / "decomposition.json"
    candidate.write_text("{}\n", encoding="utf-8")
    with pytest.raises(decomposition.DecompositionError, match="exact build"):
        decomposition.stage_candidate(clean_spec, clean_root, output)


def test_dynamic_fls_recovers_time_varying_path_and_walks_forward(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dynamic"
    target_path, days, truth = _write_dynamic_sources(source)
    spec_path = _spec(
        source,
        target_path,
        days,
        models=[_dynamic_model(days)],
    )

    first, first_manifest, first_bytes = decomposition.build_decomposition(
        spec_path, source
    )
    second, second_manifest, second_bytes = decomposition.build_decomposition(
        spec_path, source
    )
    assert first_bytes == second_bytes
    assert first_manifest.generation_id == second_manifest.generation_id
    model = _available(first)
    assert model["estimator"] == "DYNAMIC_FLS"
    orthogonal = model["orthogonal_attribution"]
    assert orthogonal["status"] == "AVAILABLE"
    budget = orthogonal["train_fitted_risk_budget"]
    assert budget["cross_mode_covariance_present"] is True
    assert budget["intercept_time_varying"] is True
    assert sum(
        float(value) for value in budget["euler_allocations"].values()
    ) == pytest.approx(float(budget["fitted_variance"]), abs=5e-11)
    assert model["method_label"] == "MPI_INFORMED_NON_EQUIVALENT"
    dynamic = model["diagnostics"]["dynamic"]
    assert dynamic["validation"] == "EXACT_LEAVE_ONE_OUT_TRAIN_ONLY"
    assert dynamic["state_transition"] == "IDENTITY_RANDOM_WALK"
    assert float(dynamic["selected_smoothness"]) in {
        0.00000001,
        0.0000001,
        0.000001,
        0.00001,
        0.0001,
        0.001,
    }
    path = dynamic["exposure_path"]
    assert all(row["betas"] == row["exposures"] for row in path)
    recovered = [
        (
            float(row["exposures"]["factor-one"]),
            float(row["exposures"]["factor-two"]),
        )
        for row in path[:105]
    ]
    mean_absolute_error = sum(
        abs(actual[0] - expected[0]) + abs(actual[1] - expected[1])
        for actual, expected in zip(recovered, truth[:105], strict=True)
    ) / (2 * len(recovered))
    assert mean_absolute_error < 0.16
    assert float(model["diagnostics"]["holdout"]["predicted_r_squared"]) > 0.5
    comparison = dynamic["holdout_comparison"]
    assert comparison["selection_effect"] == (
        "DIAGNOSTIC_ONLY_HOLDOUT_NOT_USED_FOR_SELECTION"
    )
    assert float(comparison["dynamic_minus_static_predicted_r_squared"]) > 0
    assert model["diagnostics"]["holdout"]["prediction_mode"] == (
        "CAUSAL_WALK_FORWARD_PRIOR_STATE"
    )
    for row in model["contributions"]:
        reconstructed = (
            float(row["alpha_contribution"])
            + sum(float(value) for value in row["factor_contributions"].values())
            + float(row["residual"])
        )
        assert reconstructed == pytest.approx(
            float(row["actual_excess_return"]), abs=5e-11
        )


def test_dynamic_selection_and_training_path_do_not_consume_holdout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dynamic"
    target_path, days, _truth = _write_dynamic_sources(source)
    model_definition = _dynamic_model(days)
    spec_path = _spec(
        source,
        target_path,
        days,
        models=[model_definition],
    )
    original, _manifest, _bytes = decomposition.build_decomposition(spec_path, source)
    original_model = _available(original)

    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    changed_index = 0
    for row in rows[1:]:
        if row[1] >= cast(str, model_definition["test_start"]):
            row[2] = format(0.05 if changed_index % 2 == 0 else -0.05, ".17g")
            changed_index += 1
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    changed_spec = _spec(
        source,
        target_path,
        days,
        models=[model_definition],
    )
    changed, _manifest, _bytes = decomposition.build_decomposition(changed_spec, source)
    changed_model = _selected(changed)

    assert changed_model["selected_smoothness"] == original_model["selected_smoothness"]
    original_train = [
        row
        for row in original_model["diagnostics"]["dynamic"]["exposure_path"]
        if row["date"] <= model_definition["train_end"]
    ]
    changed_train = [
        row
        for row in changed_model["diagnostics"]["dynamic"]["exposure_path"]
        if row["date"] <= model_definition["train_end"]
    ]
    assert changed_train == original_train
    assert float(changed_model["diagnostics"]["holdout"]["predicted_r_squared"]) < 0
    assert changed_model["status"] == "DIAGNOSTIC_ONLY"
    assert changed_model["exposures"] == []
    assert changed_model["model_quality"]["decision_status"] == "DIAGNOSTIC_ONLY"


def test_live_plus_backtest_accepts_declared_model_return_basis(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hybrid-model-basis"
    target_path, days = _write_sources(source)
    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    for row in rows[1:21]:
        row[3] = "BACKTEST"
        row[4] = "MODEL"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    spec_path = _spec(source, target_path, days)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["evidence_mode"] = "LIVE_PLUS_BACKTEST"
    spec["target_source"]["interval_policy"]["return_basis"] = "MIXED_NET_MODEL"
    spec_path.write_text(
        json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result, _manifest, _content = decomposition.build_decomposition(
        spec_path,
        source,
    )
    model = _available(result)

    assert model["evidence_layer"] == "BACKTEST"
    assert model["alpha"]["periodic_intercept"] is not None
    # A stream that mixes realized and model-basis rows has no observed alpha,
    # so neither side may claim one. The intercept is still published, on the
    # live basis, named by what makes it a diagnostic: withholding it left the
    # hybrid mode with no alpha at all for every strategy carrying backtest
    # history, which is the only reason anyone builds the hybrid mode.
    assert model["alpha"]["net_annualized"] is not None
    assert model["alpha"]["net_status"] == "DIAGNOSTIC_ONLY_MIXED_MODEL_BASIS"
    assert model["alpha"]["gross_annualized"] is None
    assert model["alpha"]["gross_status"] == "UNAVAILABLE_MODEL_RETURN_BASIS"


def test_mixed_basis_source_layer_boundary_is_retained_but_not_fitted(
    tmp_path: Path,
) -> None:
    """A complete MODEL-to-NET anchor is a rejected receipt, never a mixed fit."""
    source = tmp_path / "mixed-basis-boundary"
    weekend = (date(2025, 1, 4), date(2025, 1, 5))
    weekday_days = _days(140)
    calendar_only_days = tuple(
        observed
        for observed in (
            weekday_days[0] + timedelta(days=offset)
            for offset in range((weekday_days[-1] - weekday_days[0]).days + 1)
        )
        if observed not in set(weekday_days)
    )
    target_path, days = _write_sources(
        source,
        count=len(weekday_days),
        target_only_dates=calendar_only_days,
    )
    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    for row in rows[1:]:
        if row[1] <= weekend[0].isoformat():
            row[3] = "BACKTEST"
            row[4] = "MODEL"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    spec_path = _spec(source, target_path, days)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["evidence_mode"] = "LIVE_PLUS_BACKTEST"
    policy = spec["target_source"]["interval_policy"]
    policy.update(
        native_calendar="CALENDAR_DAY_24_7",
        native_label_convention="UTC_CALENDAR_DATE",
        return_basis="MIXED_NET_MODEL",
    )
    policy.pop("session_schedule_labels")
    spec_path.write_text(
        json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result, _manifest, _content = decomposition.build_decomposition(spec_path, source)

    interval = _available(result)["interval_association"]
    assert all(item["factor_anchor"] != "2025-01-06" for item in interval["buckets"])
    rejected_buckets = interval["rejected_buckets"]
    assert len(rejected_buckets) == 1
    rejected = rejected_buckets[0]
    assert {
        field: rejected[field]
        for field in (
            "factor_anchor",
            "factor_source_snapshot_identity",
            "prior_factor_anchor",
            "reason",
            "target_labels",
            "target_return_bases",
            "target_source_snapshot_identity",
            "target_source_layers",
        )
    } == {
        "factor_anchor": "2025-01-06",
        "factor_source_snapshot_identity": "SELECTED_SNAPSHOT",
        "prior_factor_anchor": "2025-01-03",
        "reason": "INCOMPATIBLE_RETURN_BASIS_SOURCE_LAYER_BOUNDARY",
        "target_labels": ["2025-01-04", "2025-01-05", "2025-01-06"],
        "target_return_bases": ["MODEL", "NET"],
        "target_source_snapshot_identity": "SELECTED_SNAPSHOT",
        "target_source_layers": ["BACKTEST", "ACCOUNT_LIVE"],
    }
    assert _available(result)["coverage"]["aligned_observations"] == len(
        interval["buckets"]
    )


def test_canonical_live_series_digest_has_a_literal_live_series_oracle(
    tmp_path: Path,
) -> None:
    expected_digest = "832f69b180eacf3ab88848736a1d3dc99eca33215a51bea851f02382d3331917"
    target_path = tmp_path / "targets.csv"
    target_path.write_text(
        "\n".join(
            (
                ",".join(decomposition.TARGET_HEADERS),
                "small-live,2026-01-02,0.0100,ACCOUNT_LIVE,NET,TOTAL,",
                "small-live,2026-01-05,-0.002500,ACCOUNT_LIVE,NET,TOTAL,",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    live_rows = decomposition._read_targets(target_path, date(2026, 1, 31))[
        "small-live"
    ]

    assert decomposition._canonical_live_series_sha256(live_rows) == expected_digest

    target_path.write_text(
        "\n".join(
            (
                ",".join(decomposition.TARGET_HEADERS),
                "small-live,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,",
                "small-live,2026-01-05,-0.0025,ACCOUNT_LIVE,NET,TOTAL,",
                "small-live,2025-12-31,0.25,BACKTEST,NET,TOTAL,",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    normalized_and_backtest_rows = decomposition._read_targets(
        target_path, date(2026, 1, 31), evidence_mode="LIVE_PLUS_BACKTEST"
    )["small-live"]

    assert (
        decomposition._canonical_live_series_sha256(normalized_and_backtest_rows)
        == expected_digest
    )

    target_path.write_text(
        "\n".join(
            (
                ",".join(decomposition.TARGET_HEADERS),
                "small-live,2026-01-02,0.02,ACCOUNT_LIVE,NET,TOTAL,",
                "small-live,2026-01-05,-0.0025,ACCOUNT_LIVE,NET,TOTAL,",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    changed_live_rows = decomposition._read_targets(target_path, date(2026, 1, 31))[
        "small-live"
    ]

    assert (
        decomposition._canonical_live_series_sha256(changed_live_rows)
        != expected_digest
    )


@pytest.mark.parametrize(
    ("holdout", "expected_status", "canonical"),
    [
        (2.220446049250313e-16, "DIAGNOSTIC_ONLY", "0"),
        (-2.220446049250313e-16, "DIAGNOSTIC_ONLY", "0"),
        (1e-12, "AVAILABLE", "1e-12"),
    ],
)
def test_model_quality_gate_uses_the_published_holdout_value(
    holdout: float, expected_status: str, canonical: str
) -> None:
    # A real source-built STATIC model produced this positive machine epsilon
    # while the bound diagnostic serialized zero. Consumers correctly require
    # positive published holdout evidence before using alpha or exposures.
    result = decomposition._apply_model_quality_gate(
        {
            "alpha": {"periodic_intercept": "0.001"},
            "exposures": [{"factor_id": "SYNTH"}],
        },
        holdout,
    )
    assert result["model_quality"]["holdout_predicted_r_squared"] == canonical
    assert result["status"] == expected_status
    if expected_status == "DIAGNOSTIC_ONLY":
        assert result["exposures"] == []
        assert result["alpha"]["periodic_intercept"] is None
        assert result["diagnostic_outputs"]["alpha"]["periodic_intercept"] == "0.001"


def _loo_reference(module, matrix, target, *, smoothness, ridge):
    """Recompute leave-one-out the way the quadratic implementation did.

    One right-hand side per observation, materializing the whole smoother. It
    is the definition the linear-time scorer has to reproduce, and it is only
    tractable at these sizes.
    """

    import numpy as np

    fit = module._dynamic_fit(matrix, target, smoothness=smoothness, ridge=ridge)
    observations, factors = matrix.shape
    identity = np.eye(factors, dtype=np.float64)
    diagonal = [
        np.outer(matrix[i], matrix[i])
        + (ridge + smoothness * (1.0 if i in {0, observations - 1} else 2.0)) * identity
        for i in range(observations)
    ]
    off_diagonal = [(-smoothness * identity).copy() for _ in range(observations - 1)]
    right = np.zeros((observations, factors, observations + 1), dtype=np.float64)
    for i in range(observations):
        right[i, :, i] = matrix[i]
        right[i, :, -1] = matrix[i]
    solved, _status = module._block_tridiagonal_solve(diagonal, off_diagonal, right)
    influence = solved[:, :, :-1]
    intercept_solution = solved[:, :, -1]
    denominator = float(observations) - float(np.sum(matrix * intercept_solution))
    projected = np.einsum("tk,tkn->n", matrix, influence)
    intercept_influence = (1.0 - projected) / denominator
    local = np.einsum("ik,iki->i", matrix, influence, optimize=False)
    local_projection = np.sum(matrix * intercept_solution, axis=1)
    leverage = local + intercept_influence * (1.0 - local_projection)
    predictions = (fit.fitted - leverage * target) / (1.0 - leverage)
    return module._r_squared(target, predictions, float(np.mean(target))), predictions


@pytest.mark.parametrize("smoothness", [0.05, 0.5])
@pytest.mark.parametrize("shape", [(40, 3), (120, 5)])
def test_dynamic_loo_matches_the_explicit_smoother(shape, smoothness):
    """The linear-time scorer reproduces the per-observation refit exactly."""

    import numpy as np

    observations, factors = shape
    rng = np.random.default_rng(20260912)
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(
        scale=0.1, size=observations
    )

    score, predictions = decomposition._dynamic_loo_score(
        matrix, target, smoothness=smoothness, ridge=1e-6
    )
    expected_score, expected_predictions = _loo_reference(
        decomposition, matrix, target, smoothness=smoothness, ridge=1e-6
    )

    assert score == pytest.approx(expected_score, abs=1e-9)
    assert predictions == pytest.approx(expected_predictions, abs=1e-8)


def test_inverse_diagonal_matches_a_dense_inverse():
    """The Takahashi recursion returns the true diagonal blocks."""

    import numpy as np

    rng = np.random.default_rng(11)
    count, size = 7, 3
    identity = np.eye(size)
    diagonal = []
    for _ in range(count):
        block = rng.normal(size=(size, size))
        diagonal.append(block @ block.T + 4.0 * identity)
    off_diagonal = [(-0.3 * identity).copy() for _ in range(count - 1)]

    dense = np.zeros((count * size, count * size))
    for i, block in enumerate(diagonal):
        dense[i * size : (i + 1) * size, i * size : (i + 1) * size] = block
    for i, block in enumerate(off_diagonal):
        dense[i * size : (i + 1) * size, (i + 1) * size : (i + 2) * size] = block
        dense[(i + 1) * size : (i + 2) * size, i * size : (i + 1) * size] = block.T
    inverse = np.linalg.inv(dense)

    blocks, status = decomposition._block_tridiagonal_inverse_diagonal(
        diagonal, off_diagonal
    )

    assert status == "BLOCK_TRIDIAGONAL_FULL_RANK"
    for i, block in enumerate(blocks):
        expected = inverse[i * size : (i + 1) * size, i * size : (i + 1) * size]
        assert block == pytest.approx(expected, abs=1e-10)


def test_dynamic_loo_stays_linear_in_the_series_length():
    """A spliced series must not cost the square of its length.

    The previous implementation allocated one smoother column per observation;
    at this length that array alone is 18 GB, so the guard is that the call
    completes at all rather than any particular duration.
    """

    import numpy as np

    rng = np.random.default_rng(3)
    observations, factors = 6000, 8
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(
        scale=0.1, size=observations
    )

    score, predictions = decomposition._dynamic_loo_score(
        matrix, target, smoothness=0.2, ridge=1e-6
    )

    assert score is not None
    assert len(predictions) == observations


def test_dynamic_fit_repeats_exactly_so_the_holdout_prior_can_be_carried():
    """The next step's prior is the step's posterior, bit for bit.

    The holdout walk used to solve for both. They are the same call on the same
    arrays with the same smoothness, ridge and fixed intercept, so the walk now
    carries the posterior forward and solves once per step instead of twice.
    If `_dynamic_fit` ever stops being pure -- an internal cache, a random
    initialisation, an in-place write into the arrays it is handed -- that
    optimisation would silently change published exposures, and this fails.
    """

    import numpy as np

    rng = np.random.default_rng(11)
    observations, factors = 60, 3
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(
        scale=0.05, size=observations
    )
    intercept = 0.004

    posterior = decomposition._dynamic_fit(
        matrix, target, smoothness=0.3, ridge=1e-6, fixed_intercept=intercept
    )
    matrix_after = matrix.copy()
    target_after = target.copy()
    next_prior = decomposition._dynamic_fit(
        matrix, target, smoothness=0.3, ridge=1e-6, fixed_intercept=intercept
    )

    # The inputs survive the call, so the second caller really does hand over
    # the same arrays the first one did.
    assert np.array_equal(matrix, matrix_after)
    assert np.array_equal(target, target_after)
    # And the result is identical, not merely close.
    assert next_prior.intercept == posterior.intercept
    assert next_prior.rank_status == posterior.rank_status
    assert np.array_equal(next_prior.exposures, posterior.exposures)
    assert np.array_equal(next_prior.fitted, posterior.fitted)


def test_carried_holdout_prior_reproduces_the_double_solve_walk():
    """Carrying the posterior forward changes cost, never a single forecast.

    The walk the scorer runs is reproduced here twice on the same arrays: once
    solving for the prior at every step, as it did before, and once carrying
    the previous step's posterior, as it does now. Every forecast exposure and
    every holdout prediction must agree exactly -- not approximately -- because
    the optimisation claims to be the same computation rather than a cheaper
    estimate of it.
    """

    import numpy as np

    def walk(matrix, target, split, *, carry):
        train_matrix, train_target = matrix[:split], target[:split]
        test_matrix, test_target = matrix[split:], target[split:]
        train_fit = decomposition._dynamic_fit(
            train_matrix, train_target, smoothness=1e-5, ridge=1e-6
        )
        history_matrix = train_matrix.copy()
        history_target = train_target.copy()
        carried = None
        predictions, forecasts = [], []
        for index in range(len(test_target)):
            if carry and carried is not None:
                prior_fit = carried
            else:
                prior_fit = decomposition._dynamic_fit(
                    history_matrix,
                    history_target,
                    smoothness=1e-5,
                    ridge=1e-6,
                    fixed_intercept=train_fit.intercept,
                )
            forecast_exposure = prior_fit.exposures[-1].copy()
            forecasts.append(forecast_exposure)
            predictions.append(
                float(train_fit.intercept + test_matrix[index] @ forecast_exposure)
            )
            history_matrix = np.vstack((history_matrix, test_matrix[index]))
            history_target = np.append(history_target, test_target[index])
            carried = decomposition._dynamic_fit(
                history_matrix,
                history_target,
                smoothness=1e-5,
                ridge=1e-6,
                fixed_intercept=train_fit.intercept,
            )
        return np.array(predictions), np.array(forecasts)

    rng = np.random.default_rng(7)
    observations, factors, split = 260, 4, 200
    matrix = rng.normal(size=(observations, factors))
    # Exposures that actually move, so a stale carried prior would show up.
    drift = np.cumsum(rng.normal(scale=0.02, size=(observations, factors)), axis=0)
    target = np.einsum("ij,ij->i", matrix, 0.5 + drift) + rng.normal(
        scale=0.05, size=observations
    )

    resolved_predictions, resolved_forecasts = walk(matrix, target, split, carry=False)
    carried_predictions, carried_forecasts = walk(matrix, target, split, carry=True)

    assert np.array_equal(resolved_predictions, carried_predictions)
    assert np.array_equal(resolved_forecasts, carried_forecasts)


def test_staging_reuses_a_solved_decomposition_for_unchanged_inputs(
    tmp_path, monkeypatch
):
    """The same inputs must not be solved twice, and must not change hands.

    The spec pins every source's digest, so identical spec bytes mean identical
    inputs; the solver's own bytes are the only other variable. A second stage
    therefore has nothing new to compute, and the release builder's separate
    stage and publish steps used to solve the same lens twice per run.
    """

    calls = {"count": 0}
    original = decomposition.build_decomposition

    def counted(spec_path, source_root):
        calls["count"] += 1
        return original(spec_path, source_root)

    monkeypatch.setattr(decomposition, "build_decomposition", counted)

    source_root = tmp_path / "source"
    target_path, days = _write_sources(source_root, complete_costs=True)
    spec_path = _spec(
        source_root,
        target_path,
        days,
        models=[_model(days, cost_coverage="COMPLETE")],
    )
    output_root = tmp_path / "out"

    first = decomposition.stage_candidate(spec_path, source_root, output_root)
    second = decomposition.stage_candidate(spec_path, source_root, output_root)

    assert first["generation_id"] == second["generation_id"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert calls["count"] == 1


def test_staging_re_solves_when_the_indexed_result_does_not_verify(
    tmp_path, monkeypatch
):
    """A stored result is served only while it still hashes to its manifest."""

    source_root = tmp_path / "source"
    target_path, days = _write_sources(source_root, complete_costs=True)
    spec_path = _spec(
        source_root,
        target_path,
        days,
        models=[_model(days, cost_coverage="COMPLETE")],
    )
    output_root = tmp_path / "out"
    first = decomposition.stage_candidate(spec_path, source_root, output_root)

    candidate = output_root / "candidates" / str(first["generation_id"])
    (candidate / "decomposition.json").write_text("{}", encoding="utf-8")

    calls = {"count": 0}
    original = decomposition.build_decomposition

    def counted(spec_path_, source_root_):
        calls["count"] += 1
        return original(spec_path_, source_root_)

    monkeypatch.setattr(decomposition, "build_decomposition", counted)

    with pytest.raises(decomposition.DecompositionError):
        decomposition.stage_candidate(spec_path, source_root, output_root)
    assert calls["count"] == 1

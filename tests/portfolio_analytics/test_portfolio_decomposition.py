from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, cast

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
    formula: str = "unconstrained",
    identical_factors: bool = False,
    target_only_dates: tuple[date, ...] = (),
    mixed_layer: bool = False,
    complete_costs: bool = False,
) -> tuple[Path, list[date]]:
    root.mkdir(parents=True)
    days = _days(count)
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
    assert model["coverage"]["target_observed_start"] == days[0].isoformat()
    assert model["coverage"]["target_cumulative_return"] is not None
    assert model["coverage"]["target_observed_end"] == days[-1].isoformat()
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
    assert by_id["available"]["coverage"]["factor_missing_weekdays"] == 1
    assert by_id["available"]["coverage"]["factor_missing_weekends"] == 1
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
    original_train = original_model["diagnostics"]["dynamic"]["exposure_path"][:105]
    changed_train = changed_model["diagnostics"]["dynamic"]["exposure_path"][:105]
    assert changed_train == original_train
    assert float(changed_model["diagnostics"]["holdout"]["predicted_r_squared"]) < 0
    assert changed_model["status"] == "DIAGNOSTIC_ONLY"
    assert changed_model["exposures"] == []
    assert changed_model["model_quality"]["decision_status"] == "DIAGNOSTIC_ONLY"


def test_canonical_live_series_digest_normalizes_decimal_spelling(
    tmp_path: Path,
) -> None:
    source = tmp_path / "canonical"
    target_path, days = _write_sources(source)
    first_spec = _spec(source, target_path, days)
    first, _manifest, _content = decomposition.build_decomposition(first_spec, source)
    first_digest = _selected(first)["coverage"]["canonical_live_series_sha256"]

    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    for row in rows[1:]:
        row[2] = f"{Decimal(row[2]):.20f}"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    second_spec = _spec(source, target_path, days)
    second, _manifest, _content = decomposition.build_decomposition(second_spec, source)

    assert _selected(second)["coverage"]["canonical_live_series_sha256"] == (
        first_digest
    )

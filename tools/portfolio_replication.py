"""Build a governed, clean-room dynamic-style research replication tracker."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    ManifestValidationError,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


BUILDER_VERSION: Final = "0.1.0"
SCHEMA_VERSION: Final = 1
METHOD_LABEL: Final = "MPI_INFORMED_NON_EQUIVALENT_RESEARCH_REPLICATION"
ALLOWED_SOURCE_METHOD_LABEL: Final = "MPI_INFORMED_NON_EQUIVALENT"
EXPECTED_ESTIMATOR: Final = "DYNAMIC_FLS"
EXPECTED_VALIDATION: Final = "EXACT_LEAVE_ONE_OUT_TRAIN_ONLY"
EXPECTED_SELECTION_RULE: Final = (
    "MAX_TRAIN_LOO_PREDICTED_R2_THEN_SMOOTHEST_WITHIN_TOLERANCE"
)
EXPECTED_PREDICTION_MODE: Final = "CAUSAL_WALK_FORWARD_PRIOR_STATE"
AVAILABILITY_COMPLETE: Final = "AVAILABLE_RESEARCH_REPLICATION"
AVAILABILITY_DIAGNOSTIC: Final = "DIAGNOSTIC_ONLY_SOURCE_MODEL"


class ReplicationError(ValueError):
    """An input or output violates the research-replication contract."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReplicationError(f"{label} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplicationError(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ReplicationError(f"{label} must be an object")
    return value


def _objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReplicationError(f"{label} must be an array")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise ReplicationError(f"{label} contains a non-object")
        rows.append(item)
    return rows


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplicationError(f"{label} must be non-empty text")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReplicationError(f"{label} must be an integer >= {minimum}")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ReplicationError(f"{label} must be numeric")
    try:
        accepted = Decimal(str(value))
    except InvalidOperation as exc:
        raise ReplicationError(f"{label} must be numeric") from exc
    if not accepted.is_finite():
        raise ReplicationError(f"{label} must be finite")
    return accepted


def _decimal_text(value: Decimal | float) -> str:
    accepted = value if isinstance(value, Decimal) else Decimal(str(value))
    if accepted == 0:
        return "0"
    return format(accepted, ".12g")


def _parse_date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ReplicationError(f"{label} must be an ISO date") from exc


def _resolve_regular(root: Path, relative: object, label: str) -> Path:
    text = _text(relative, f"{label}.path")
    relative_path = Path(text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ReplicationError(f"{label}.path must stay beneath source_root")
    root_resolved = root.resolve()
    path = (root_resolved / relative_path).resolve()
    if root_resolved not in path.parents:
        raise ReplicationError(f"{label}.path escapes source_root")
    if path.is_symlink() or not path.is_file():
        raise ReplicationError(f"{label} is missing or linked")
    return path


def _validate_source(
    source_root: Path, definition: object, label: str
) -> tuple[Path, str, int]:
    if not isinstance(definition, dict):
        raise ReplicationError(f"{label} must be an object")
    path = _resolve_regular(source_root, definition.get("path"), label)
    expected_hash = _text(definition.get("sha256"), f"{label}.sha256")
    expected_size = _integer(definition.get("size_bytes"), f"{label}.size_bytes")
    actual_hash, actual_size = hash_file(path)
    if actual_hash != expected_hash:
        raise ReplicationError(f"{label} hash differs from the specification")
    if actual_size != expected_size:
        raise ReplicationError(f"{label} size differs from the specification")
    return path, actual_hash, actual_size


def _load_factor_returns(path: Path) -> tuple[dict[date, dict[str, Decimal]], set[str]]:
    rows: dict[date, dict[str, Decimal]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise ReplicationError("factor source must contain date")
        factor_ids = {name for name in reader.fieldnames if name not in {"date", "RF"}}
        if not factor_ids:
            raise ReplicationError("factor source contains no excess-return factors")
        for index, raw in enumerate(reader, start=2):
            observed = _parse_date(raw.get("date"), f"factor row {index}.date")
            if observed in rows:
                raise ReplicationError(f"duplicate factor date: {observed}")
            parsed: dict[str, Decimal] = {}
            for factor_id in factor_ids:
                value = raw.get(factor_id)
                if value in {None, ""}:
                    continue
                parsed[factor_id] = _decimal(value, f"factor row {index}.{factor_id}")
            rows[observed] = parsed
    return rows, factor_ids


def _compound(values: Sequence[Decimal]) -> Decimal:
    wealth = Decimal("1")
    for value in values:
        if value <= Decimal("-1"):
            raise ReplicationError("return observations must be greater than -1")
        wealth *= Decimal("1") + value
    return wealth - Decimal("1")


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / Decimal(
        len(values) - 1
    )
    return Decimal(str(math.sqrt(float(variance))))


def _correlation(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_std = _sample_std(left)
    right_std = _sample_std(right)
    if left_std in {None, Decimal("0")} or right_std in {None, Decimal("0")}:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    covariance = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
    ) / Decimal(len(left) - 1)
    return covariance / (left_std * right_std)


def _max_drawdown_loss(values: Sequence[Decimal]) -> Decimal:
    wealth = Decimal("1")
    peak = wealth
    worst = Decimal("0")
    for value in values:
        wealth *= Decimal("1") + value
        peak = max(peak, wealth)
        worst = max(worst, Decimal("1") - wealth / peak)
    return worst


def _relative_drawdown_loss(
    actual: Sequence[Decimal], tracker: Sequence[Decimal]
) -> Decimal:
    actual_wealth = Decimal("1")
    tracker_wealth = Decimal("1")
    peak_ratio = Decimal("1")
    worst = Decimal("0")
    for actual_value, tracker_value in zip(actual, tracker, strict=True):
        actual_wealth *= Decimal("1") + actual_value
        tracker_wealth *= Decimal("1") + tracker_value
        ratio = actual_wealth / tracker_wealth
        peak_ratio = max(peak_ratio, ratio)
        worst = max(worst, Decimal("1") - ratio / peak_ratio)
    return worst


def _metrics(
    actual: Sequence[Decimal],
    gross: Sequence[Decimal],
    net: Sequence[Decimal],
    costs: Sequence[Decimal],
    turnover: Sequence[Decimal],
    periods_per_year: int,
) -> dict[str, object]:
    residuals = [a - n for a, n in zip(actual, net, strict=True)]
    residual_std = _sample_std(residuals)
    annual_root = Decimal(str(math.sqrt(periods_per_year)))
    tracking_error = None if residual_std is None else residual_std * annual_root
    information_ratio = (
        None
        if residual_std in {None, Decimal("0")}
        else _mean(residuals) / residual_std * annual_root
    )
    return {
        "annualized_descriptive_residual_alpha": _decimal_text(
            _mean(residuals) * Decimal(periods_per_year)
        ),
        "annualized_tracking_error": (
            None if tracking_error is None else _decimal_text(tracking_error)
        ),
        "correlation_actual_to_net_tracker": (
            None
            if (correlation := _correlation(actual, net)) is None
            else _decimal_text(correlation)
        ),
        "cumulative_actual_excess_return": _decimal_text(_compound(actual)),
        "cumulative_cost_drag": _decimal_text(sum(costs, Decimal("0"))),
        "cumulative_gross_tracker_excess_return": _decimal_text(_compound(gross)),
        "cumulative_net_tracker_excess_return": _decimal_text(_compound(net)),
        "information_ratio_actual_minus_net_tracker": (
            None if information_ratio is None else _decimal_text(information_ratio)
        ),
        "max_actual_drawdown_loss": _decimal_text(_max_drawdown_loss(actual)),
        "max_net_tracker_drawdown_loss": _decimal_text(_max_drawdown_loss(net)),
        "max_relative_wealth_ratio_drawdown_loss": _decimal_text(
            _relative_drawdown_loss(actual, net)
        ),
        "maximum_one_period_turnover": _decimal_text(max(turnover)),
        "mean_one_period_turnover": _decimal_text(_mean(turnover)),
        "observation_count": len(actual),
        "total_turnover": _decimal_text(sum(turnover, Decimal("0"))),
    }


def _implementable_weights(
    raw: Mapping[str, Decimal], max_weight: Decimal, max_gross: Decimal
) -> tuple[dict[str, Decimal], list[str]]:
    reasons: list[str] = []
    clipped = {
        factor_id: max(-max_weight, min(max_weight, weight))
        for factor_id, weight in raw.items()
    }
    if clipped != dict(raw):
        reasons.append("PER_FACTOR_ABSOLUTE_LIMIT")
    gross = sum((abs(weight) for weight in clipped.values()), Decimal("0"))
    if gross > max_gross:
        scale = max_gross / gross
        clipped = {factor_id: weight * scale for factor_id, weight in clipped.items()}
        reasons.append("GROSS_FACTOR_EXPOSURE_LIMIT")
    return clipped, reasons


def _unavailable(selection: dict[str, Any]) -> dict[str, object]:
    series_id = _text(selection.get("series_id"), "model selection.series_id")
    return {
        "availability_status": "UNAVAILABLE_NO_SELECTED_DYNAMIC_MODEL",
        "blocking_reasons": [
            _text(selection.get("status"), f"{series_id}.selection.status")
        ],
        "model_id": None,
        "series_id": series_id,
    }


def _replicate_model(
    model: dict[str, Any],
    factor_returns: Mapping[date, Mapping[str, Decimal]],
    available_factor_ids: set[str],
    *,
    selected_cost_bps: Decimal,
    cost_sensitivities_bps: Sequence[Decimal],
    max_weight: Decimal,
    max_gross: Decimal,
    periods_per_year: int,
) -> dict[str, object]:
    series_id = _text(model.get("series_id"), "model.series_id")
    model_id = _text(model.get("model_id"), f"{series_id}.model_id")
    if model.get("estimator") != EXPECTED_ESTIMATOR:
        raise ReplicationError(
            f"{series_id} selected model is not {EXPECTED_ESTIMATOR}"
        )
    if model.get("method_label") != ALLOWED_SOURCE_METHOD_LABEL:
        raise ReplicationError(f"{series_id} source method label is not accepted")
    if model.get("selected_for_series") is not True:
        raise ReplicationError(f"{series_id} source model is not marked selected")
    factor_ids = [
        _text(value, f"{series_id}.factor_id") for value in model.get("factor_ids", [])
    ]
    if not factor_ids or len(factor_ids) != len(set(factor_ids)):
        raise ReplicationError(f"{series_id} factor_ids are empty or duplicated")
    if not set(factor_ids) <= available_factor_ids:
        raise ReplicationError(
            f"{series_id} factor source is missing a selected factor"
        )
    diagnostics = model.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise ReplicationError(f"{series_id} diagnostics are missing")
    dynamic = diagnostics.get("dynamic")
    holdout = diagnostics.get("holdout")
    if not isinstance(dynamic, dict) or not isinstance(holdout, dict):
        raise ReplicationError(
            f"{series_id} dynamic or holdout diagnostics are missing"
        )
    if dynamic.get("validation") != EXPECTED_VALIDATION:
        raise ReplicationError(
            f"{series_id} parameter selection was not train-only LOO"
        )
    if dynamic.get("selection_rule") != EXPECTED_SELECTION_RULE:
        raise ReplicationError(f"{series_id} parameter selection rule differs")
    if holdout.get("prediction_mode") != EXPECTED_PREDICTION_MODE:
        raise ReplicationError(f"{series_id} holdout was not causal prior-state")
    forecasts = _objects(
        dynamic.get("holdout_forecasts"), f"{series_id}.holdout_forecasts"
    )
    if len(forecasts) != _integer(
        holdout.get("observation_count"),
        f"{series_id}.holdout.observation_count",
        minimum=1,
    ):
        raise ReplicationError(f"{series_id} holdout forecast count differs")
    alpha = model.get("alpha")
    if not isinstance(alpha, dict):
        raise ReplicationError(f"{series_id} alpha evidence is missing")
    intercept_value = alpha.get("periodic_intercept")
    if intercept_value is None:
        diagnostic_outputs = model.get("diagnostic_outputs")
        diagnostic_alpha = (
            diagnostic_outputs.get("alpha")
            if isinstance(diagnostic_outputs, dict)
            else None
        )
        if not isinstance(diagnostic_alpha, dict):
            raise ReplicationError(f"{series_id} diagnostic alpha evidence is missing")
        intercept_value = diagnostic_alpha.get("periodic_intercept")
    intercept = _decimal(intercept_value, f"{series_id}.periodic_intercept")

    actual_values: list[Decimal] = []
    gross_values: list[Decimal] = []
    selected_net_values: list[Decimal] = []
    selected_costs: list[Decimal] = []
    turnover_values: list[Decimal] = []
    rows: list[dict[str, object]] = []
    breakpoints: list[dict[str, object]] = []
    previous_weights = {factor_id: Decimal("0") for factor_id in factor_ids}
    previous_cash = Decimal("1")
    previous_date: date | None = None
    sensitivity_returns: dict[Decimal, list[Decimal]] = {
        basis: [] for basis in cost_sensitivities_bps
    }
    for index, forecast in enumerate(forecasts):
        observed = _parse_date(
            forecast.get("date"), f"{series_id}.forecast[{index}].date"
        )
        if previous_date is not None and observed <= previous_date:
            raise ReplicationError(
                f"{series_id} holdout forecasts are not strictly ordered"
            )
        factor_row = factor_returns.get(observed)
        if factor_row is None or not set(factor_ids) <= set(factor_row):
            raise ReplicationError(
                f"{series_id} lacks factor observations on {observed}"
            )
        raw_exposures_value = forecast.get("forecast_exposures")
        if not isinstance(raw_exposures_value, dict) or set(raw_exposures_value) != set(
            factor_ids
        ):
            raise ReplicationError(
                f"{series_id} forecast exposures differ from factor_ids"
            )
        raw_exposures = {
            factor_id: _decimal(
                raw_exposures_value[factor_id], f"{series_id}.{observed}.{factor_id}"
            )
            for factor_id in factor_ids
        }
        source_predicted = _decimal(
            forecast.get("predicted_excess_return"), f"{series_id}.{observed}.predicted"
        )
        independently_recomputed = intercept + sum(
            (
                raw_exposures[factor_id] * factor_row[factor_id]
                for factor_id in factor_ids
            ),
            Decimal("0"),
        )
        if abs(source_predicted - independently_recomputed) > Decimal("1e-9"):
            raise ReplicationError(
                f"{series_id} source forecast does not reproduce on {observed}"
            )
        weights, binding = _implementable_weights(raw_exposures, max_weight, max_gross)
        cash_weight = Decimal("1") - sum(weights.values(), Decimal("0"))
        turnover = (
            sum(
                (
                    abs(weights[factor_id] - previous_weights[factor_id])
                    for factor_id in factor_ids
                ),
                Decimal("0"),
            )
            + abs(cash_weight - previous_cash)
        ) / Decimal("2")
        gross_return = sum(
            (weights[factor_id] * factor_row[factor_id] for factor_id in factor_ids),
            Decimal("0"),
        )
        cost = turnover * selected_cost_bps / Decimal("10000")
        net_return = gross_return - cost
        actual = _decimal(
            forecast.get("actual_excess_return"), f"{series_id}.{observed}.actual"
        )
        change_l2 = Decimal(
            str(
                math.sqrt(
                    sum(
                        float(weights[factor_id] - previous_weights[factor_id]) ** 2
                        for factor_id in factor_ids
                    )
                )
            )
        )
        breakpoints.append(
            {
                "date": observed.isoformat(),
                "exposure_change_l2": _decimal_text(change_l2),
            }
        )
        rows.append(
            {
                "actual_excess_return": _decimal_text(actual),
                "cash_residual_weight": _decimal_text(cash_weight),
                "constraint_bindings": binding,
                "date": observed.isoformat(),
                "factor_returns": {
                    factor_id: _decimal_text(factor_row[factor_id])
                    for factor_id in factor_ids
                },
                "gross_tracker_excess_return": _decimal_text(gross_return),
                "implementable_factor_weights": {
                    factor_id: _decimal_text(weights[factor_id])
                    for factor_id in factor_ids
                },
                "information_set": "MODEL_STATE_ESTIMATED_THROUGH_PRIOR_OBSERVATION",
                "net_tracker_excess_return": _decimal_text(net_return),
                "one_period_turnover": _decimal_text(turnover),
                "raw_forecast_factor_weights": {
                    factor_id: _decimal_text(raw_exposures[factor_id])
                    for factor_id in factor_ids
                },
                "selected_cost": _decimal_text(cost),
                "source_predicted_excess_return": _decimal_text(source_predicted),
            }
        )
        for basis in cost_sensitivities_bps:
            sensitivity_returns[basis].append(
                gross_return - turnover * basis / Decimal("10000")
            )
        actual_values.append(actual)
        gross_values.append(gross_return)
        selected_net_values.append(net_return)
        selected_costs.append(cost)
        turnover_values.append(turnover)
        previous_weights = weights
        previous_cash = cash_weight
        previous_date = observed
    quality = model.get("model_quality")
    if not isinstance(quality, dict):
        raise ReplicationError(f"{series_id} model quality is missing")
    decision = _text(quality.get("decision_status"), f"{series_id}.decision_status")
    availability = (
        AVAILABILITY_COMPLETE if decision == "PM_VALID" else AVAILABILITY_DIAGNOSTIC
    )
    worst_indices = sorted(range(len(rows)), key=lambda idx: actual_values[idx])[:5]
    return {
        "availability_status": availability,
        "cost_sensitivity": [
            {
                "cost_bps": _decimal_text(basis),
                "cumulative_net_tracker_excess_return": _decimal_text(
                    _compound(sensitivity_returns[basis])
                ),
            }
            for basis in cost_sensitivities_bps
        ],
        "factor_ids": factor_ids,
        "holdout_end": rows[-1]["date"],
        "holdout_start": rows[0]["date"],
        "investability_policy": {
            "cash_residual_treatment": "ZERO_EXCESS_RETURN_CASH_RESIDUAL",
            "max_absolute_factor_weight": _decimal_text(max_weight),
            "max_gross_factor_exposure": _decimal_text(max_gross),
            "selected_cost_bps_per_one_way_turnover": _decimal_text(selected_cost_bps),
        },
        "largest_exposure_changes": sorted(
            breakpoints,
            key=lambda row: Decimal(str(row["exposure_change_l2"])),
            reverse=True,
        )[:5],
        "metrics": _metrics(
            actual_values,
            gross_values,
            selected_net_values,
            selected_costs,
            turnover_values,
            periods_per_year,
        ),
        "model_id": model_id,
        "parameter_selection": {
            "holdout_recalibration": "NONE",
            "nested_evaluation": "TRAIN_SELECT_THEN_CAUSAL_HOLDOUT_REPLAY",
            "ridge": _text(model.get("selected_ridge"), f"{series_id}.selected_ridge"),
            "selection_rule": EXPECTED_SELECTION_RULE,
            "smoothness": _text(
                model.get("selected_smoothness"), f"{series_id}.selected_smoothness"
            ),
            "validation": EXPECTED_VALIDATION,
        },
        "series_id": series_id,
        "source_model_decision_status": decision,
        "stress_worst_actual_dates": [rows[index] for index in worst_indices],
        "tracker_rows": rows,
    }


def build_replication(
    spec_path: Path, source_root: Path
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    spec = _load_json(spec_path, "replication specification")
    if spec.get("schema_version") != 1:
        raise ReplicationError("replication specification schema_version must be 1")
    if spec.get("method_label") != METHOD_LABEL:
        raise ReplicationError("replication specification method_label differs")
    as_of = _parse_date(spec.get("as_of_date"), "specification.as_of_date")
    sources = spec.get("sources")
    if not isinstance(sources, dict):
        raise ReplicationError("specification.sources must be an object")
    decomposition_path, decomposition_hash, decomposition_size = _validate_source(
        source_root, sources.get("decomposition"), "sources.decomposition"
    )
    manifest_path, manifest_hash, manifest_size = _validate_source(
        source_root,
        sources.get("decomposition_generation"),
        "sources.decomposition_generation",
    )
    factor_path, factor_hash, factor_size = _validate_source(
        source_root, sources.get("factors"), "sources.factors"
    )
    decomposition = _load_json(decomposition_path, "decomposition source")
    try:
        generation = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    except (OSError, ManifestValidationError) as exc:
        raise ReplicationError("decomposition generation manifest is invalid") from exc
    expected_generation_id = _text(
        sources.get("decomposition_generation_id"),
        "sources.decomposition_generation_id",
    )
    if generation.generation_id != expected_generation_id:
        raise ReplicationError("decomposition generation id differs")
    if generation.verification is not VerificationStatus.VERIFIED:
        raise ReplicationError("decomposition generation is not VERIFIED")
    if (
        generation.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    ):
        raise ReplicationError("decomposition generation coverage is not complete")
    if generation.as_of_date != as_of:
        raise ReplicationError("decomposition generation as_of_date differs")
    if generation.adapter.name != "portfolio-decomposition":
        raise ReplicationError("decomposition generation adapter differs")
    if decomposition.get("method_label") != ALLOWED_SOURCE_METHOD_LABEL:
        raise ReplicationError("decomposition method label is not accepted")
    if decomposition.get("as_of_date") != as_of.isoformat():
        raise ReplicationError("decomposition as_of_date differs from specification")
    policy = spec.get("policy")
    if not isinstance(policy, dict):
        raise ReplicationError("specification.policy must be an object")
    periods_per_year = _integer(
        policy.get("periods_per_year"), "policy.periods_per_year", minimum=1
    )
    selected_cost_bps = _decimal(
        policy.get("selected_cost_bps"), "policy.selected_cost_bps"
    )
    max_weight = _decimal(
        policy.get("max_absolute_factor_weight"), "policy.max_absolute_factor_weight"
    )
    max_gross = _decimal(
        policy.get("max_gross_factor_exposure"), "policy.max_gross_factor_exposure"
    )
    if (
        min(selected_cost_bps, max_weight, max_gross) < 0
        or max_weight == 0
        or max_gross == 0
    ):
        raise ReplicationError(
            "replication policy limits must be positive and costs non-negative"
        )
    sensitivity_value = policy.get("cost_sensitivities_bps")
    if not isinstance(sensitivity_value, list) or not sensitivity_value:
        raise ReplicationError(
            "policy.cost_sensitivities_bps must be a non-empty array"
        )
    sensitivities = sorted(
        {
            _decimal(value, "policy.cost_sensitivities_bps")
            for value in sensitivity_value
        }
    )
    if (
        any(value < 0 for value in sensitivities)
        or selected_cost_bps not in sensitivities
    ):
        raise ReplicationError(
            "cost sensitivities must be non-negative and include selected cost"
        )
    factor_returns, available_factor_ids = _load_factor_returns(factor_path)
    models = {
        _text(row.get("model_id"), "model_result.model_id"): row
        for row in _objects(
            decomposition.get("model_results"), "decomposition.model_results"
        )
    }
    results: list[dict[str, object]] = []
    observed_dates: list[date] = []
    for selection in sorted(
        _objects(
            decomposition.get("model_selections"), "decomposition.model_selections"
        ),
        key=lambda row: _text(row.get("series_id"), "model selection.series_id"),
    ):
        model_id = selection.get("model_id")
        if model_id is None:
            results.append(_unavailable(selection))
            continue
        model = models.get(_text(model_id, "model selection.model_id"))
        if model is None:
            raise ReplicationError("selected model is absent from model_results")
        result = _replicate_model(
            model,
            factor_returns,
            available_factor_ids,
            selected_cost_bps=selected_cost_bps,
            cost_sensitivities_bps=sensitivities,
            max_weight=max_weight,
            max_gross=max_gross,
            periods_per_year=periods_per_year,
        )
        observed_dates.extend(
            (
                _parse_date(result["holdout_start"], "result.holdout_start"),
                _parse_date(result["holdout_end"], "result.holdout_end"),
            )
        )
        results.append(result)
    if not results or not observed_dates:
        raise ReplicationError("no selected dynamic model can be replicated")
    payload: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "authority_boundary": {
            "allocation_authority": "NONE",
            "execution_authority": "NONE",
            "hwm_authority": "NONE",
            "readiness_gate_effect": "NONE",
            "use": "RESEARCH_DIAGNOSTIC_ONLY",
        },
        "builder_version": BUILDER_VERSION,
        "clean_room_boundary": (
            "Openly described time-varying style-analysis concepts are reimplemented "
            "without MPI code, software defaults, proprietary search heuristics, or branding claims."
        ),
        "method_label": METHOD_LABEL,
        "replication_results": results,
        "replication_schema_version": SCHEMA_VERSION,
        "sources": {
            "decomposition_generation_id": expected_generation_id,
            "decomposition_generation_sha256": manifest_hash,
            "decomposition_sha256": decomposition_hash,
            "factor_source_sha256": factor_hash,
            "specification_sha256": hash_file(spec_path)[0],
        },
    }
    identity = dict(payload)
    payload["analytics_hash"] = hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    result_bytes = canonical_json_bytes(payload)
    artifacts = (
        SourceArtifact(
            artifact_id="analytics/dynamic-replication.json",
            sha256=hashlib.sha256(result_bytes).hexdigest(),
            size_bytes=len(result_bytes),
            as_of_date=as_of,
            schema_name="portfolio-dynamic-replication",
            schema_version=str(SCHEMA_VERSION),
        ),
        SourceArtifact(
            artifact_id="inputs/decomposition-generation.json",
            sha256=manifest_hash,
            size_bytes=manifest_size,
            as_of_date=as_of,
            schema_name="generation-manifest",
            schema_version=str(generation.manifest_schema_version),
        ),
        SourceArtifact(
            artifact_id="inputs/decomposition.json",
            sha256=decomposition_hash,
            size_bytes=decomposition_size,
            as_of_date=as_of,
            schema_name="portfolio-decomposition",
            schema_version=str(decomposition.get("analytics_schema_version", "1")),
        ),
        SourceArtifact(
            artifact_id="inputs/factors.csv",
            sha256=factor_hash,
            size_bytes=factor_size,
            as_of_date=as_of,
            schema_name="excess-factor-return-series",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="methodology/replication-spec.json",
            sha256=hash_file(spec_path)[0],
            size_bytes=spec_path.stat().st_size,
            as_of_date=as_of,
            schema_name="portfolio-replication-spec",
            schema_version="1",
        ),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-dynamic-replication", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Every decomposition series is dispositioned; selected dynamic models "
                "are replayed over their sealed holdout and unavailable models stay explicit."
            ),
            fact_types=(
                "cost-sensitivity",
                "dynamic-exposure",
                "out-of-sample-replication",
                "replication-drawdown",
                "replication-stress",
                "tracker-turnover",
            ),
            observed_start=min(observed_dates),
            observed_end=max(observed_dates),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    return payload, manifest, result_bytes


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_exact(
    candidate: Path, manifest: GenerationManifest, result_bytes: bytes
) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise ReplicationError("candidate must be a regular directory")
    expected = {
        "generation.json": manifest.to_json_bytes(),
        "replication.json": result_bytes,
    }
    if {path.name for path in candidate.iterdir()} != set(expected):
        raise ReplicationError("candidate file set differs from the contract")
    for name, content in expected.items():
        path = candidate / name
        if path.is_symlink() or path.read_bytes() != content:
            raise ReplicationError(f"candidate {name} differs from exact rebuild")


def stage_candidate(
    spec_path: Path, source_root: Path, output_root: Path
) -> dict[str, object]:
    payload, manifest, result_bytes = build_replication(spec_path, source_root)
    candidates = output_root.resolve() / "candidates"
    if output_root.exists() and output_root.is_symlink():
        raise ReplicationError("output root must not be linked")
    candidates.mkdir(parents=True, exist_ok=True)
    candidate = candidates / manifest.generation_id
    if candidate.exists():
        _verify_exact(candidate, manifest, result_bytes)
    else:
        candidate.mkdir()
        try:
            _write_new(candidate / "replication.json", result_bytes)
            _write_new(candidate / "generation.json", manifest.to_json_bytes())
        except BaseException:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
    return {
        "analytics_hash": payload["analytics_hash"],
        "candidate": str(candidate),
        "generation_id": manifest.generation_id,
        "series_count": len(
            _objects(payload.get("replication_results"), "replication results")
        ),
    }


def verify_candidate(
    spec_path: Path, source_root: Path, candidate: Path
) -> dict[str, object]:
    payload, manifest, result_bytes = build_replication(spec_path, source_root)
    _verify_exact(candidate, manifest, result_bytes)
    return {
        "analytics_hash": payload["analytics_hash"],
        "candidate_id": manifest.generation_id,
        "verification": "VERIFIED_EXACT_REBUILD",
    }


def publish(
    spec_path: Path, source_root: Path, candidate: Path, output_root: Path
) -> dict[str, object]:
    payload, manifest, result_bytes = build_replication(spec_path, source_root)
    _verify_exact(candidate, manifest, result_bytes)
    manifest_path = stage_generation(output_root, manifest)
    result_path = manifest_path.parent / "replication.json"
    if result_path.exists():
        if result_path.is_symlink() or result_path.read_bytes() != result_bytes:
            raise GenerationConflictError("published replication artifact differs")
    else:
        _write_new(result_path, result_bytes)
    pointer = publish_generation(output_root, manifest.generation_id)
    return {
        "analytics_hash": payload["analytics_hash"],
        "generation_id": manifest.generation_id,
        "publication_state": pointer.state.value,
        "series_count": len(
            _objects(payload.get("replication_results"), "replication results")
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--spec", type=Path, required=True)
        command.add_argument("--source-root", type=Path, required=True)
        if name in {"build", "publish"}:
            command.add_argument("--output-root", type=Path, required=True)
        if name in {"verify", "publish"}:
            command.add_argument("--candidate-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = stage_candidate(args.spec, args.source_root, args.output_root)
        elif args.command == "verify":
            result = verify_candidate(args.spec, args.source_root, args.candidate_dir)
        else:
            result = publish(
                args.spec,
                args.source_root,
                args.candidate_dir,
                args.output_root,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-dynamic-replication: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

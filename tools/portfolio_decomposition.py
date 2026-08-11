"""Build and publish deterministic static and dynamic return decompositions."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any, Final, Mapping, Sequence, cast

import numpy as np
import numpy.typing as npt

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    rename_directory_with_retry,
)
from quant_engine.core.contracts import canonical_json_bytes


ANALYTICS_SCHEMA_VERSION: Final = 2
BUILDER_VERSION: Final = "1.10.0"
METHODOLOGY_VERSION: Final = "ANL-DECOMP-CLEANROOM-3"
METHOD_LABEL: Final = "MPI_INFORMED_NON_EQUIVALENT"
ANALYSIS_LENSES: Final = {
    "ECONOMIC_FACTOR",
    "ETF_STYLE",
    "HISTORICAL_MINIMUM_PROOF",
    "RETURN_INDUSTRY",
    "RETURN_SECTOR",
}
NEAR_DUPLICATE_ABSOLUTE_CORRELATION: Final = 0.98
UNREGULARIZED_CONDITION_NUMBER_LIMIT: Final = 1_000.0
UNREGULARIZED_VIF_LIMIT: Final = 10.0
METHODOLOGY_PATH: Final = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "analytics"
    / "decomposition-methodology.json"
)
TARGET_HEADERS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)
EVIDENCE_LAYERS: Final = {
    "ACCOUNT_LIVE",
    "PROVIDER_LIVE",
    "OFFICIAL_VEHICLE",
    "PREDECESSOR",
    "SHADOW_OOS",
    "BACKTEST",
    "CONCEPTUAL_STRESS",
}
RETURN_BASES: Final = {"GROSS", "NET"}
RETURN_TYPES: Final = {"EXCESS", "TOTAL"}
CONSTRAINTS: Final = {"UNCONSTRAINED", "LONG_ONLY_SUM_TO_ONE"}
COST_COVERAGE: Final = {"COMPLETE", "INCOMPLETE"}
ESTIMATORS: Final = {"STATIC_RBSA", "DYNAMIC_FLS"}
EVIDENCE_MODES: Final = {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}

FloatArray = npt.NDArray[np.float64]


class DecompositionError(ValueError):
    """The specification, source data, or generated evidence is invalid."""


@dataclass(frozen=True)
class SourceDefinition:
    artifact_id: str
    relative_path: str
    expected_sha256: str
    expected_size_bytes: int
    schema_name: str
    schema_version: str


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    column: str
    return_type: str
    economic_family: str | None = None
    effective_start: date | None = None
    effective_end: date | None = None
    raw_orthogonal: bool | None = None
    redundancy_policy: str | None = None
    return_source_id: str | None = None
    selection_rationale: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class TargetRow:
    day: date
    value: float
    canonical_return: str
    evidence_layer: str
    return_basis: str
    return_type: str
    cost: float | None


@dataclass(frozen=True)
class FactorRow:
    day: date
    risk_free: float
    values: Mapping[str, float | None]


@dataclass(frozen=True)
class ModelDefinition:
    model_id: str
    series_id: str
    factor_ids: tuple[str, ...]
    constraint: str
    cost_coverage: str
    ridge_candidates: tuple[float, ...]
    train_end: date
    test_start: date
    minimum_train_observations: int
    minimum_test_observations: int
    cv_folds: int
    periods_per_year: int
    parsimony_tolerance: float
    estimator: str
    smoothness_candidates: tuple[float, ...]


@dataclass(frozen=True)
class Specification:
    analysis_lens: str
    as_of_date: date
    evidence_mode: str
    maximum_factor_staleness_days: int
    target_source: SourceDefinition
    factor_source: SourceDefinition
    factor_date_column: str
    risk_free_column: str
    factors: tuple[FactorDefinition, ...]
    models: tuple[ModelDefinition, ...]
    provenance_sources: tuple[SourceDefinition, ...]
    series_composition: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class Fit:
    intercept: float
    exposures: FloatArray
    rank: int
    rank_status: str


@dataclass(frozen=True)
class DynamicFit:
    """One clean-room FLS solution for a disclosed smoothness value."""

    intercept: float
    exposures: FloatArray
    fitted: FloatArray
    rank_status: str


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise DecompositionError(
            f"{name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecompositionError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DecompositionError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecompositionError(f"{name} must be non-empty text")
    return value.strip()


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DecompositionError(f"{name} must be an integer >= {minimum}")
    return value


def _day(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise DecompositionError(f"{name} must be a canonical ISO date") from exc
    if parsed.isoformat() != accepted:
        raise DecompositionError(f"{name} must be a canonical ISO date")
    return parsed


def _finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DecompositionError(f"{name} must be finite numeric text or a number")
    try:
        accepted = float(value)
    except (TypeError, ValueError) as exc:
        raise DecompositionError(
            f"{name} must be finite numeric text or a number"
        ) from exc
    if not math.isfinite(accepted) or (minimum is not None and accepted < minimum):
        raise DecompositionError(f"{name} is outside its accepted range")
    return accepted


def _portable(value: object, name: str) -> str:
    accepted = _text(value, name)
    path = PurePosixPath(accepted)
    if (
        path.is_absolute()
        or "\\" in accepted
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DecompositionError(f"{name} must be a portable relative path")
    return accepted


def _source_definition(value: object, name: str) -> SourceDefinition:
    source = _object(value, name)
    _strict_keys(
        source,
        {
            "artifact_id",
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "schema_name",
            "schema_version",
        },
        name,
    )
    digest = _text(source["expected_sha256"], f"{name}.expected_sha256")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise DecompositionError(f"{name}.expected_sha256 must be lower-case SHA-256")
    return SourceDefinition(
        artifact_id=_portable(source["artifact_id"], f"{name}.artifact_id"),
        relative_path=_portable(source["relative_path"], f"{name}.relative_path"),
        expected_sha256=digest,
        expected_size_bytes=_integer(
            source["expected_size_bytes"], f"{name}.expected_size_bytes"
        ),
        schema_name=_text(source["schema_name"], f"{name}.schema_name"),
        schema_version=_text(source["schema_version"], f"{name}.schema_version"),
    )


def _factor_definition(value: Mapping[str, Any], index: int) -> FactorDefinition:
    name = f"factor_source.factors[{index}]"
    base_keys = {"column", "factor_id", "return_type"}
    governance_keys = {
        "economic_family",
        "effective_end",
        "effective_start",
        "raw_orthogonal",
        "redundancy_policy",
        "return_source_id",
        "selection_rationale",
        "source_id",
    }
    if set(value) != base_keys:
        _strict_keys(value, base_keys | governance_keys, name)
    return_type = _text(value["return_type"], f"{name}.return_type")
    if return_type not in RETURN_TYPES:
        raise DecompositionError(f"{name}.return_type is unsupported")
    raw_orthogonal = value.get("raw_orthogonal")
    if raw_orthogonal is not None and not isinstance(raw_orthogonal, bool):
        raise DecompositionError(f"{name}.raw_orthogonal must be boolean")
    effective_start = (
        None
        if value.get("effective_start") is None
        else _day(value["effective_start"], f"{name}.effective_start")
    )
    effective_end = (
        None
        if value.get("effective_end") is None
        else _day(value["effective_end"], f"{name}.effective_end")
    )
    if (
        effective_start is not None
        and effective_end is not None
        and effective_start > effective_end
    ):
        raise DecompositionError(f"{name} effective interval is reversed")
    return FactorDefinition(
        factor_id=_text(value["factor_id"], f"{name}.factor_id"),
        column=_text(value["column"], f"{name}.column"),
        return_type=return_type,
        economic_family=(
            None
            if value.get("economic_family") is None
            else _text(value["economic_family"], f"{name}.economic_family")
        ),
        effective_start=effective_start,
        effective_end=effective_end,
        raw_orthogonal=raw_orthogonal,
        redundancy_policy=(
            None
            if value.get("redundancy_policy") is None
            else _text(value["redundancy_policy"], f"{name}.redundancy_policy")
        ),
        return_source_id=(
            None
            if value.get("return_source_id") is None
            else _text(value["return_source_id"], f"{name}.return_source_id")
        ),
        selection_rationale=(
            None
            if value.get("selection_rationale") is None
            else _text(value["selection_rationale"], f"{name}.selection_rationale")
        ),
        source_id=(
            None
            if value.get("source_id") is None
            else _text(value["source_id"], f"{name}.source_id")
        ),
    )


def _model_definition(value: Mapping[str, Any], index: int) -> ModelDefinition:
    name = f"models[{index}]"
    base_keys = {
        "constraint",
        "cost_coverage",
        "cv_folds",
        "factor_ids",
        "minimum_test_observations",
        "minimum_train_observations",
        "model_id",
        "parsimony_tolerance",
        "periods_per_year",
        "ridge_candidates",
        "series_id",
        "test_start",
        "train_end",
    }
    estimator = _text(value.get("estimator", "STATIC_RBSA"), f"{name}.estimator")
    if estimator not in ESTIMATORS:
        raise DecompositionError(f"{name}.estimator is unsupported")
    expected_keys = set(base_keys)
    if "estimator" in value:
        expected_keys.add("estimator")
    if estimator == "DYNAMIC_FLS":
        expected_keys.add("smoothness_candidates")
    _strict_keys(
        value,
        expected_keys,
        name,
    )
    constraint = _text(value["constraint"], f"{name}.constraint")
    if constraint not in CONSTRAINTS:
        raise DecompositionError(f"{name}.constraint is unsupported")
    cost_coverage = _text(value["cost_coverage"], f"{name}.cost_coverage")
    if cost_coverage not in COST_COVERAGE:
        raise DecompositionError(f"{name}.cost_coverage is unsupported")
    raw_factors = value["factor_ids"]
    if not isinstance(raw_factors, list):
        raise DecompositionError(f"{name}.factor_ids must be an array")
    factor_ids = tuple(_text(item, f"{name}.factor_ids[]") for item in raw_factors)
    if not factor_ids or len(factor_ids) != len(set(factor_ids)):
        raise DecompositionError(f"{name}.factor_ids must be unique and non-empty")
    raw_ridge = value["ridge_candidates"]
    if not isinstance(raw_ridge, list):
        raise DecompositionError(f"{name}.ridge_candidates must be an array")
    ridge = tuple(
        sorted(
            {
                _finite(item, f"{name}.ridge_candidates[]", minimum=0.0)
                for item in raw_ridge
            }
        )
    )
    if not ridge:
        raise DecompositionError(f"{name}.ridge_candidates must be non-empty")
    smoothness: tuple[float, ...] = ()
    if estimator == "DYNAMIC_FLS":
        if constraint != "UNCONSTRAINED":
            raise DecompositionError(
                f"{name}.DYNAMIC_FLS currently requires UNCONSTRAINED"
            )
        if len(ridge) != 1:
            raise DecompositionError(
                f"{name}.DYNAMIC_FLS requires one disclosed state ridge value"
            )
        raw_smoothness = value["smoothness_candidates"]
        if not isinstance(raw_smoothness, list):
            raise DecompositionError(f"{name}.smoothness_candidates must be an array")
        smoothness = tuple(
            sorted(
                {
                    _finite(
                        item,
                        f"{name}.smoothness_candidates[]",
                        minimum=1e-12,
                    )
                    for item in raw_smoothness
                }
            )
        )
        if not smoothness:
            raise DecompositionError(f"{name}.smoothness_candidates must be non-empty")
    train_end = _day(value["train_end"], f"{name}.train_end")
    test_start = _day(value["test_start"], f"{name}.test_start")
    if test_start <= train_end:
        raise DecompositionError(f"{name}.test_start must follow train_end")
    return ModelDefinition(
        model_id=_text(value["model_id"], f"{name}.model_id"),
        series_id=_text(value["series_id"], f"{name}.series_id"),
        factor_ids=factor_ids,
        constraint=constraint,
        cost_coverage=cost_coverage,
        ridge_candidates=ridge,
        train_end=train_end,
        test_start=test_start,
        minimum_train_observations=_integer(
            value["minimum_train_observations"],
            f"{name}.minimum_train_observations",
            minimum=3,
        ),
        minimum_test_observations=_integer(
            value["minimum_test_observations"],
            f"{name}.minimum_test_observations",
            minimum=1,
        ),
        cv_folds=_integer(value["cv_folds"], f"{name}.cv_folds", minimum=2),
        periods_per_year=_integer(
            value["periods_per_year"], f"{name}.periods_per_year", minimum=1
        ),
        parsimony_tolerance=_finite(
            value["parsimony_tolerance"],
            f"{name}.parsimony_tolerance",
            minimum=0.0,
        ),
        estimator=estimator,
        smoothness_candidates=smoothness,
    )


def _load_spec(path: Path) -> Specification:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecompositionError("specification is not readable UTF-8 JSON") from exc
    root = _object(decoded, "specification")
    root_keys = {
        "as_of_date",
        "factor_source",
        "maximum_factor_staleness_days",
        "models",
        "schema_version",
        "target_source",
    }
    if "analysis_lens" in root:
        root_keys.add("analysis_lens")
    if "evidence_mode" in root:
        root_keys.add("evidence_mode")
    if "provenance_sources" in root:
        root_keys.add("provenance_sources")
    if "series_composition" in root:
        root_keys.add("series_composition")
    _strict_keys(root, root_keys, "specification")
    if root["schema_version"] != 1:
        raise DecompositionError("specification schema_version is unsupported")
    analysis_lens = _text(
        root.get("analysis_lens", "HISTORICAL_MINIMUM_PROOF"),
        "specification.analysis_lens",
    )
    if analysis_lens not in ANALYSIS_LENSES:
        raise DecompositionError("specification.analysis_lens is unsupported")
    evidence_mode = _text(
        root.get("evidence_mode", "LIVE_ONLY"), "specification.evidence_mode"
    )
    if evidence_mode not in EVIDENCE_MODES:
        raise DecompositionError("specification.evidence_mode is unsupported")
    factor_source_object = _object(root["factor_source"], "factor_source")
    _strict_keys(
        factor_source_object,
        {
            "artifact_id",
            "date_column",
            "expected_sha256",
            "expected_size_bytes",
            "factors",
            "relative_path",
            "risk_free_column",
            "schema_name",
            "schema_version",
        },
        "factor_source",
    )
    common = {
        key: factor_source_object[key]
        for key in (
            "artifact_id",
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "schema_name",
            "schema_version",
        )
    }
    factors = tuple(
        _factor_definition(item, index)
        for index, item in enumerate(
            _objects(factor_source_object["factors"], "factor_source.factors")
        )
    )
    if not factors or len({item.factor_id for item in factors}) != len(factors):
        raise DecompositionError("factor IDs must be unique and non-empty")
    if len({item.column for item in factors}) != len(factors):
        raise DecompositionError("factor columns must be unique")
    models = tuple(
        _model_definition(item, index)
        for index, item in enumerate(_objects(root["models"], "models"))
    )
    if not models or len({item.model_id for item in models}) != len(models):
        raise DecompositionError("model IDs must be unique and non-empty")
    known_factors = {item.factor_id for item in factors}
    for model in models:
        unknown = set(model.factor_ids) - known_factors
        if unknown:
            raise DecompositionError(
                f"model {model.model_id} references unknown factors {sorted(unknown)}"
            )
    raw_composition = root.get("series_composition", {})
    if not isinstance(raw_composition, dict) or not all(
        isinstance(key, str) and isinstance(value, dict)
        for key, value in raw_composition.items()
    ):
        raise DecompositionError("specification.series_composition must be an object")
    return Specification(
        analysis_lens=analysis_lens,
        as_of_date=_day(root["as_of_date"], "as_of_date"),
        evidence_mode=evidence_mode,
        maximum_factor_staleness_days=_integer(
            root["maximum_factor_staleness_days"],
            "maximum_factor_staleness_days",
        ),
        target_source=_source_definition(root["target_source"], "target_source"),
        factor_source=_source_definition(common, "factor_source"),
        factor_date_column=_text(
            factor_source_object["date_column"], "factor_source.date_column"
        ),
        risk_free_column=_text(
            factor_source_object["risk_free_column"],
            "factor_source.risk_free_column",
        ),
        factors=factors,
        models=models,
        provenance_sources=tuple(
            _source_definition(item, f"provenance_sources[{index}]")
            for index, item in enumerate(
                _objects(root.get("provenance_sources", []), "provenance_sources")
            )
        ),
        series_composition=cast(Mapping[str, Mapping[str, object]], raw_composition),
    )


def _source_path(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / PurePosixPath(relative_path)).resolve()
    if not path.is_relative_to(resolved_root):
        raise DecompositionError("source path escapes source root")
    if path.is_symlink() or not path.is_file():
        raise DecompositionError("source path must be a regular local file")
    return path


def _verify_source(root: Path, definition: SourceDefinition) -> Path:
    path = _source_path(root, definition.relative_path)
    digest, size = hash_file(path)
    if digest != definition.expected_sha256 or size != definition.expected_size_bytes:
        raise DecompositionError(
            f"source drift for {definition.artifact_id}: expected hash/size differ"
        )
    return path


def _read_targets(
    path: Path, as_of_date: date, evidence_mode: str = "LIVE_ONLY"
) -> dict[str, list[TargetRow]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != TARGET_HEADERS:
            raise DecompositionError(
                "target CSV headers differ from the closed contract"
            )
        grouped: dict[str, list[TargetRow]] = {}
        seen: set[tuple[str, date]] = set()
        metadata: dict[str, tuple[str, str]] = {}
        layers: dict[str, set[str]] = {}
        for line_number, raw in enumerate(reader, start=2):
            series_id = _text(raw["series_id"], f"target line {line_number} series_id")
            day = _day(raw["date"], f"target line {line_number} date")
            if day > as_of_date:
                raise DecompositionError("target observation follows as_of_date")
            raw_return = raw["return"].strip()
            value = _finite(raw_return, f"target line {line_number} return")
            canonical_return = _canonical_decimal_text(
                raw_return, f"target line {line_number} return"
            )
            layer = _text(
                raw["evidence_layer"], f"target line {line_number} evidence_layer"
            )
            basis = _text(
                raw["return_basis"], f"target line {line_number} return_basis"
            )
            return_type = _text(
                raw["return_type"], f"target line {line_number} return_type"
            )
            if layer not in EVIDENCE_LAYERS:
                raise DecompositionError("target evidence layer is unsupported")
            if basis not in RETURN_BASES or return_type not in RETURN_TYPES:
                raise DecompositionError("target return basis/type is unsupported")
            key = (series_id, day)
            if key in seen:
                raise DecompositionError("target series contains a duplicate date")
            seen.add(key)
            signature = (basis, return_type)
            if series_id in metadata and metadata[series_id] != signature:
                raise DecompositionError(
                    "one target series cannot mix return semantics"
                )
            metadata[series_id] = signature
            layers.setdefault(series_id, set()).add(layer)
            raw_cost = raw["cost"].strip()
            cost = (
                None if not raw_cost else _finite(raw_cost, "target cost", minimum=0.0)
            )
            grouped.setdefault(series_id, []).append(
                TargetRow(
                    day,
                    value,
                    canonical_return,
                    layer,
                    basis,
                    return_type,
                    cost,
                )
            )
    if not grouped:
        raise DecompositionError("target source contains no observations")
    for rows in grouped.values():
        rows.sort(key=lambda item: item.day)
    for series_id, rows in grouped.items():
        observed_layers = layers[series_id]
        live_layers = observed_layers - {"BACKTEST"}
        if evidence_mode == "LIVE_ONLY":
            if "BACKTEST" in observed_layers or len(live_layers) != 1:
                raise DecompositionError(
                    "LIVE_ONLY target series cannot mix evidence layers or include backtest"
                )
            continue
        if evidence_mode != "LIVE_PLUS_BACKTEST":
            raise DecompositionError("target evidence mode is unsupported")
        if len(live_layers) != 1:
            raise DecompositionError(
                "LIVE_PLUS_BACKTEST requires exactly one accepted live evidence layer"
            )
        if "BACKTEST" not in observed_layers:
            continue
        live_inception = min(
            row.day for row in rows if row.evidence_layer != "BACKTEST"
        )
        if any(
            row.day >= live_inception
            for row in rows
            if row.evidence_layer == "BACKTEST"
        ):
            raise DecompositionError(
                "LIVE_PLUS_BACKTEST may use backtest only before live inception"
            )
    return grouped


def _read_factors(
    path: Path, spec: Specification
) -> tuple[dict[date, FactorRow], tuple[str, ...]]:
    expected = (
        spec.factor_date_column,
        spec.risk_free_column,
        *(item.column for item in spec.factors),
    )
    if len(set(expected)) != len(expected):
        raise DecompositionError(
            "factor date, risk-free, and factor columns must differ"
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != expected:
            raise DecompositionError(
                "factor CSV headers differ from the closed contract"
            )
        rows: dict[date, FactorRow] = {}
        for line_number, raw in enumerate(reader, start=2):
            day = _day(raw[spec.factor_date_column], f"factor line {line_number} date")
            if day > spec.as_of_date:
                raise DecompositionError("factor observation follows as_of_date")
            if day in rows:
                raise DecompositionError("factor source contains a duplicate date")
            risk_free = _finite(
                raw[spec.risk_free_column], f"factor line {line_number} risk free"
            )
            values: dict[str, float | None] = {}
            for factor in spec.factors:
                raw_value = raw[factor.column].strip()
                values[factor.factor_id] = (
                    None
                    if not raw_value
                    else _finite(
                        raw_value, f"factor line {line_number} {factor.factor_id}"
                    )
                )
            rows[day] = FactorRow(day, risk_free, values)
    if not rows:
        raise DecompositionError("factor source contains no observations")
    return rows, expected


def _number(value: float | np.floating[Any] | None) -> str | None:
    if value is None:
        return None
    accepted = float(value)
    if not math.isfinite(accepted):
        raise DecompositionError("analytics result is non-finite")
    if abs(accepted) < 5e-16:
        accepted = 0.0
    return format(accepted, ".12g")


def _canonical_decimal_text(value: str, name: str) -> str:
    try:
        accepted = Decimal(value)
    except InvalidOperation as error:
        raise DecompositionError(f"{name} must be a decimal") from error
    if not accepted.is_finite():
        raise DecompositionError(f"{name} must be finite")
    if accepted == 0:
        return "0"
    return format(accepted.normalize(), "f")


def _canonical_live_series_sha256(rows: Sequence[TargetRow]) -> str:
    ordered = [
        {"date": row.day.isoformat(), "return": row.canonical_return}
        for row in sorted(rows, key=lambda item: item.day)
        if row.evidence_layer != "BACKTEST"
    ]
    encoded = (
        json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _apply_model_quality_gate(
    result: dict[str, object], holdout_predicted_r_squared: float | None
) -> dict[str, object]:
    """Separate a successful fit from a PM-usable model conclusion.

    The sealed holdout is never used to fit or choose the computational model.
    It is used only after training-only selection as a fail-closed decision gate.
    Non-positive or unavailable predicted R-squared retains the reproducible
    computational diagnostics, but removes alpha and exposures from the accepted
    output surface.
    """

    result["computational_status"] = "AVAILABLE"
    result["model_quality"] = {
        "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
        "holdout_predicted_r_squared": _number(holdout_predicted_r_squared),
    }
    quality = cast(dict[str, object], result["model_quality"])
    if holdout_predicted_r_squared is not None and holdout_predicted_r_squared > 0:
        quality.update(
            {
                "decision_status": "PM_VALID",
                "gate_reason": "POSITIVE_SEALED_HOLDOUT_PREDICTED_R2",
            }
        )
        result["status"] = "AVAILABLE"
        return result

    reason = (
        "NON_POSITIVE_SEALED_HOLDOUT_PREDICTED_R2"
        if holdout_predicted_r_squared is not None
        else "UNAVAILABLE_SEALED_HOLDOUT_PREDICTED_R2"
    )
    quality.update({"decision_status": "DIAGNOSTIC_ONLY", "gate_reason": reason})
    result["diagnostic_outputs"] = {
        "alpha": result["alpha"],
        "exposures": result["exposures"],
        "warning": ("COMPUTED_NOT_ACCEPTED_FOR_PM_ALPHA_OR_EXPOSURE_CONCLUSIONS"),
    }
    result["alpha"] = {
        "annualized_standard_error": None,
        "confidence_status": "DIAGNOSTIC_ONLY_MODEL_QUALITY_GATE",
        "gross_annualized": None,
        "gross_status": "DIAGNOSTIC_ONLY_MODEL_QUALITY_GATE",
        "net_annualized": None,
        "net_status": "DIAGNOSTIC_ONLY_MODEL_QUALITY_GATE",
        "periodic_intercept": None,
    }
    result["exposures"] = []
    result["status"] = "DIAGNOSTIC_ONLY"
    return result


def _project_simplex(values: FloatArray) -> FloatArray:
    ordered = np.sort(values)[::-1]
    cumulative = np.cumsum(ordered) - 1.0
    indices = np.arange(1, len(values) + 1, dtype=np.float64)
    eligible = np.nonzero(ordered - cumulative / indices > 0.0)[0]
    if eligible.size == 0:
        raise DecompositionError("simplex projection failed")
    rho = int(eligible[-1])
    threshold = cumulative[rho] / float(rho + 1)
    return cast(FloatArray, np.maximum(values - threshold, 0.0).astype(np.float64))


def _fit(
    matrix: FloatArray,
    target: FloatArray,
    *,
    constraint: str,
    ridge: float,
) -> Fit:
    if constraint == "UNCONSTRAINED":
        design = np.column_stack((np.ones(len(target), dtype=np.float64), matrix))
        penalty = np.diag(
            np.array([0.0, *([ridge] * matrix.shape[1])], dtype=np.float64)
        )
        normal = design.T @ design + penalty
        right = design.T @ target
        rank = int(np.linalg.matrix_rank(design))
        try:
            coefficients = np.linalg.solve(normal, right)
        except np.linalg.LinAlgError:
            coefficients = np.linalg.pinv(normal) @ right
        status = "FULL_RANK" if rank == design.shape[1] else "SINGULAR_PSEUDOINVERSE"
        return Fit(
            float(coefficients[0]), coefficients[1:].astype(np.float64), rank, status
        )

    centered_matrix = matrix - np.mean(matrix, axis=0)
    centered_target = target - np.mean(target)
    quadratic = centered_matrix.T @ centered_matrix + ridge * np.eye(
        matrix.shape[1], dtype=np.float64
    )
    linear = centered_matrix.T @ centered_target
    eigenvalues = np.linalg.eigvalsh(quadratic)
    lipschitz = max(2.0 * float(eigenvalues[-1]), 1e-12)
    exposures = np.full(matrix.shape[1], 1.0 / matrix.shape[1], dtype=np.float64)
    for _ in range(20_000):
        gradient = 2.0 * (quadratic @ exposures - linear)
        updated = _project_simplex(exposures - gradient / lipschitz)
        if float(np.max(np.abs(updated - exposures))) <= 1e-12:
            exposures = updated
            break
        exposures = updated
    else:
        raise DecompositionError("constrained regression did not converge")
    intercept = float(np.mean(target - matrix @ exposures))
    rank = int(np.linalg.matrix_rank(centered_matrix))
    return Fit(intercept, exposures, rank, "CONSTRAINED_SIMPLEX")


def _predict(fit: Fit, matrix: FloatArray) -> FloatArray:
    return (fit.intercept + matrix @ fit.exposures).astype(np.float64)


def _training_design_diagnostics(
    matrix: FloatArray, factor_ids: Sequence[str]
) -> dict[str, object]:
    """Describe collinearity using training observations only."""

    if matrix.ndim != 2 or matrix.shape[1] != len(factor_ids):
        raise DecompositionError("training design differs from configured factors")
    means = np.mean(matrix, axis=0)
    standard_deviations = np.std(matrix, axis=0)
    constant_indices = [
        index
        for index, value in enumerate(standard_deviations)
        if float(value) <= 1e-15
    ]
    standardized = np.zeros_like(matrix, dtype=np.float64)
    for index in range(matrix.shape[1]):
        if index not in constant_indices:
            standardized[:, index] = (
                matrix[:, index] - means[index]
            ) / standard_deviations[index]

    rank = int(np.linalg.matrix_rank(standardized))
    condition_raw = float(np.linalg.cond(standardized))
    condition_number = condition_raw if math.isfinite(condition_raw) else None
    correlation = np.full((matrix.shape[1], matrix.shape[1]), np.nan, dtype=np.float64)
    for left in range(matrix.shape[1]):
        if left in constant_indices:
            continue
        for right in range(matrix.shape[1]):
            if right in constant_indices:
                continue
            correlation[left, right] = float(
                np.mean(standardized[:, left] * standardized[:, right])
            )

    near_duplicates: list[dict[str, object]] = []
    for left in range(matrix.shape[1]):
        for right in range(left + 1, matrix.shape[1]):
            value = correlation[left, right]
            if math.isfinite(float(value)) and abs(float(value)) >= (
                NEAR_DUPLICATE_ABSOLUTE_CORRELATION
            ):
                near_duplicates.append(
                    {
                        "absolute_correlation": _number(abs(float(value))),
                        "correlation": _number(float(value)),
                        "factor_ids": [factor_ids[left], factor_ids[right]],
                    }
                )

    vif_values: list[float | None]
    if constant_indices or rank < matrix.shape[1]:
        vif_values = [None] * matrix.shape[1]
        vif_status = "UNAVAILABLE_SINGULAR_OR_CONSTANT_DESIGN"
    elif matrix.shape[1] == 1:
        vif_values = [1.0]
        vif_status = "AVAILABLE"
    else:
        try:
            inverse = np.linalg.inv(correlation)
        except np.linalg.LinAlgError:
            vif_values = [None] * matrix.shape[1]
            vif_status = "UNAVAILABLE_SINGULAR_OR_CONSTANT_DESIGN"
        else:
            vif_values = [
                float(inverse[index, index]) for index in range(matrix.shape[1])
            ]
            vif_status = "AVAILABLE"
    finite_vifs = [value for value in vif_values if value is not None]
    maximum_vif = max(finite_vifs) if finite_vifs else None

    blocking_reasons: list[str] = []
    if constant_indices:
        blocking_reasons.append("CONSTANT_FACTOR")
    if rank < matrix.shape[1]:
        blocking_reasons.append("RANK_DEFICIENT")
    if condition_number is None or condition_number > (
        UNREGULARIZED_CONDITION_NUMBER_LIMIT
    ):
        blocking_reasons.append("CONDITION_NUMBER_ABOVE_LIMIT")
    if maximum_vif is None or maximum_vif > UNREGULARIZED_VIF_LIMIT:
        blocking_reasons.append("VIF_ABOVE_LIMIT_OR_UNAVAILABLE")

    result: dict[str, object] = {
        "basis": "TRAIN_ONLY",
        "correlation_matrix": [
            {
                "factor_id": factor_id,
                "values": {
                    other_id: (
                        None
                        if not math.isfinite(float(correlation[row, column]))
                        else _number(float(correlation[row, column]))
                    )
                    for column, other_id in enumerate(factor_ids)
                },
            }
            for row, factor_id in enumerate(factor_ids)
        ],
        "design_observations": matrix.shape[0],
        "factor_count": matrix.shape[1],
        "factor_standard_deviations": {
            factor_id: _number(float(standard_deviations[index]))
            for index, factor_id in enumerate(factor_ids)
        },
        "matrix_condition_number": _number(condition_number),
        "matrix_rank": rank,
        "near_duplicate_absolute_correlation_threshold": _number(
            NEAR_DUPLICATE_ABSOLUTE_CORRELATION
        ),
        "near_duplicate_pairs": near_duplicates,
        "transformation": {
            "diagnostic_basis": "TRAIN_MEAN_ZERO_UNIT_POPULATION_STD",
            "fitted_basis": "RAW_CONFIGURED_FACTOR_RETURNS",
            "inverse_mapping": [
                {
                    "factor_id": factor_id,
                    "raw_equals": factor_id,
                    "status": "IDENTITY_NO_MODEL_TRANSFORMATION",
                }
                for factor_id in factor_ids
            ],
        },
        "unregularized_blocking_reasons": blocking_reasons,
        "unregularized_condition_number_limit": _number(
            UNREGULARIZED_CONDITION_NUMBER_LIMIT
        ),
        "unregularized_status": (
            "BLOCKED_COLLINEAR_DESIGN" if blocking_reasons else "ELIGIBLE"
        ),
        "unregularized_vif_limit": _number(UNREGULARIZED_VIF_LIMIT),
        "vif": [
            {"factor_id": factor_id, "value": _number(vif_values[index])}
            for index, factor_id in enumerate(factor_ids)
        ],
        "vif_status": vif_status,
    }
    return result


def _linear_solve(matrix: FloatArray, right: FloatArray) -> tuple[FloatArray, bool]:
    """Solve a small deterministic system and disclose pseudoinverse fallback."""

    try:
        solved = np.linalg.solve(matrix, right)
        return cast(FloatArray, solved.astype(np.float64)), False
    except np.linalg.LinAlgError:
        solved = np.linalg.pinv(matrix) @ right
        return cast(FloatArray, solved.astype(np.float64)), True


def _block_tridiagonal_solve(
    diagonal: Sequence[FloatArray],
    off_diagonal: Sequence[FloatArray],
    right: FloatArray,
) -> tuple[FloatArray, str]:
    """Solve a symmetric block-tridiagonal system using a block Thomas pass."""

    count = len(diagonal)
    if count == 0 or right.ndim != 3 or right.shape[0] != count:
        raise DecompositionError("dynamic system dimensions are invalid")
    if len(off_diagonal) != count - 1:
        raise DecompositionError("dynamic off-diagonal dimensions are invalid")
    transformed_right: list[FloatArray] = []
    transformed_upper: list[FloatArray] = []
    used_pseudoinverse = False
    for index in range(count):
        effective = diagonal[index].copy()
        rhs = right[index].copy()
        if index:
            lower = off_diagonal[index - 1].T
            effective -= lower @ transformed_upper[index - 1]
            rhs -= lower @ transformed_right[index - 1]
        solved_rhs, fallback = _linear_solve(effective, rhs)
        used_pseudoinverse = used_pseudoinverse or fallback
        transformed_right.append(solved_rhs)
        if index < count - 1:
            solved_upper, fallback = _linear_solve(effective, off_diagonal[index])
            used_pseudoinverse = used_pseudoinverse or fallback
            transformed_upper.append(solved_upper)
    solution = np.empty_like(right, dtype=np.float64)
    solution[-1] = transformed_right[-1]
    for index in range(count - 2, -1, -1):
        solution[index] = (
            transformed_right[index] - transformed_upper[index] @ solution[index + 1]
        )
    status = (
        "BLOCK_TRIDIAGONAL_PSEUDOINVERSE"
        if used_pseudoinverse
        else "BLOCK_TRIDIAGONAL_FULL_RANK"
    )
    return solution, status


def _dynamic_fit(
    matrix: FloatArray,
    target: FloatArray,
    *,
    smoothness: float,
    ridge: float,
    weights: FloatArray | None = None,
    fixed_intercept: float | None = None,
) -> DynamicFit:
    """Minimize residual error plus adjacent-state FLS smoothness.

    The independent implementation uses identity state transitions and an
    isotropic disclosed ridge. It does not reproduce undisclosed MPI defaults,
    constraints, breakpoint relaxation, or factor-search heuristics.
    """

    if matrix.ndim != 2 or target.ndim != 1 or len(matrix) != len(target):
        raise DecompositionError("dynamic fit arrays are incompatible")
    observation_count, factor_count = matrix.shape
    if observation_count < 2 or factor_count < 1:
        raise DecompositionError("dynamic fit requires two rows and one factor")
    accepted_weights = (
        np.ones(observation_count, dtype=np.float64)
        if weights is None
        else weights.astype(np.float64)
    )
    if accepted_weights.shape != (observation_count,) or np.any(
        (accepted_weights != 0.0) & (accepted_weights != 1.0)
    ):
        raise DecompositionError("dynamic observation weights must be zero or one")
    if float(np.sum(accepted_weights)) < 2.0:
        raise DecompositionError("dynamic fit has insufficient included observations")

    identity = np.eye(factor_count, dtype=np.float64)
    diagonal: list[FloatArray] = []
    off_diagonal = [
        (-smoothness * identity).copy() for _ in range(observation_count - 1)
    ]
    data_right = np.empty((observation_count, factor_count), dtype=np.float64)
    intercept_right = np.empty((observation_count, factor_count), dtype=np.float64)
    for index in range(observation_count):
        factor_row = matrix[index]
        weight = float(accepted_weights[index])
        neighbor_penalty = smoothness * (
            1.0 if index in {0, observation_count - 1} else 2.0
        )
        diagonal.append(
            cast(
                FloatArray,
                (
                    weight * np.outer(factor_row, factor_row)
                    + (ridge + neighbor_penalty) * identity
                ).astype(np.float64),
            )
        )
        data_right[index] = weight * factor_row * target[index]
        intercept_right[index] = weight * factor_row

    if fixed_intercept is None:
        combined_right = np.stack((data_right, intercept_right), axis=2)
        solved, rank_status = _block_tridiagonal_solve(
            diagonal, off_diagonal, combined_right
        )
        data_solution = solved[:, :, 0]
        intercept_solution = solved[:, :, 1]
        weighted_factors = accepted_weights[:, None] * matrix
        denominator = float(np.sum(accepted_weights)) - float(
            np.sum(weighted_factors * intercept_solution)
        )
        if abs(denominator) <= 1e-12:
            raise DecompositionError("dynamic intercept is not identifiable")
        intercept = (
            float(np.sum(accepted_weights * target))
            - float(np.sum(weighted_factors * data_solution))
        ) / denominator
        exposures = data_solution - intercept_solution * intercept
    else:
        adjusted_right = data_right - intercept_right * fixed_intercept
        solved, rank_status = _block_tridiagonal_solve(
            diagonal, off_diagonal, adjusted_right[:, :, None]
        )
        intercept = fixed_intercept
        exposures = solved[:, :, 0]
    fitted = intercept + np.sum(matrix * exposures, axis=1)
    return DynamicFit(
        intercept=float(intercept),
        exposures=cast(FloatArray, exposures.astype(np.float64)),
        fitted=cast(FloatArray, fitted.astype(np.float64)),
        rank_status=rank_status,
    )


def _dynamic_loo_score(
    matrix: FloatArray,
    target: FloatArray,
    *,
    smoothness: float,
    ridge: float,
) -> tuple[float | None, FloatArray]:
    """Compute exact leave-one-out predictions from linear-smoother leverage.

    The penalized FLS estimator is linear in the target for fixed smoothness
    and ridge values.  The standard PRESS identity therefore reproduces the
    result of omitting and refitting every observation without n separate
    block solves.
    """

    fit = _dynamic_fit(
        matrix,
        target,
        smoothness=smoothness,
        ridge=ridge,
    )
    observation_count, factor_count = matrix.shape
    identity = np.eye(factor_count, dtype=np.float64)
    diagonal = [
        cast(
            FloatArray,
            (
                np.outer(matrix[index], matrix[index])
                + (
                    ridge
                    + smoothness * (1.0 if index in {0, observation_count - 1} else 2.0)
                )
                * identity
            ).astype(np.float64),
        )
        for index in range(observation_count)
    ]
    off_diagonal = [
        (-smoothness * identity).copy() for _ in range(observation_count - 1)
    ]
    right = np.zeros(
        (observation_count, factor_count, observation_count + 1),
        dtype=np.float64,
    )
    for index in range(observation_count):
        right[index, :, index] = matrix[index]
        right[index, :, -1] = matrix[index]
    solved, _rank_status = _block_tridiagonal_solve(
        diagonal,
        off_diagonal,
        cast(FloatArray, right),
    )
    influence = solved[:, :, :-1]
    intercept_solution = solved[:, :, -1]
    denominator = float(observation_count) - float(np.sum(matrix * intercept_solution))
    if abs(denominator) <= 1e-12:
        raise DecompositionError("dynamic intercept is not identifiable")
    projected_influence = np.einsum("tk,tkn->n", matrix, influence)
    intercept_influence = (1.0 - projected_influence) / denominator
    local_influence = np.einsum("ik,iki->i", matrix, influence, optimize=False)
    local_intercept_projection = np.sum(matrix * intercept_solution, axis=1)
    leverage = local_influence + intercept_influence * (
        1.0 - local_intercept_projection
    )
    remaining = 1.0 - leverage
    if np.any(np.abs(remaining) <= 1e-12):
        return None, cast(FloatArray, np.full(observation_count, np.nan))
    predictions = (fit.fitted - leverage * target) / remaining
    return (
        _r_squared(target, predictions, float(np.mean(target))),
        cast(FloatArray, predictions.astype(np.float64)),
    )


def _r_squared(
    actual: FloatArray, predicted: FloatArray, baseline: float
) -> float | None:
    denominator = float(np.sum((actual - baseline) ** 2))
    if denominator <= 1e-24:
        return None
    return 1.0 - float(np.sum((actual - predicted) ** 2)) / denominator


def _cv_score(
    matrix: FloatArray,
    target: FloatArray,
    model: ModelDefinition,
    ridge: float,
) -> tuple[float | None, int]:
    remaining = len(target) - model.minimum_train_observations
    if remaining < model.cv_folds:
        return None, 0
    quotient, remainder = divmod(remaining, model.cv_folds)
    sizes = [
        quotient + (1 if index < remainder else 0) for index in range(model.cv_folds)
    ]
    cursor = model.minimum_train_observations
    squared_error = 0.0
    baseline_error = 0.0
    predicted_count = 0
    for size in sizes:
        validation_end = cursor + size
        fit = _fit(
            matrix[:cursor],
            target[:cursor],
            constraint=model.constraint,
            ridge=ridge,
        )
        actual = target[cursor:validation_end]
        predicted = _predict(fit, matrix[cursor:validation_end])
        baseline = float(np.mean(target[:cursor]))
        squared_error += float(np.sum((actual - predicted) ** 2))
        baseline_error += float(np.sum((actual - baseline) ** 2))
        predicted_count += len(actual)
        cursor = validation_end
    return (
        None if baseline_error <= 1e-24 else 1.0 - squared_error / baseline_error,
        predicted_count,
    )


def _newey_west_lag(observation_count: int) -> int:
    """Return the disclosed Newey-West automatic lag, bounded to observed data."""
    if observation_count <= 1:
        return 0
    proposed = int(math.floor(4.0 * (observation_count / 100.0) ** (2.0 / 9.0)))
    return min(max(proposed, 0), observation_count - 1)


def _newey_west_standard_errors(
    design: FloatArray, residual: FloatArray, lag: int
) -> FloatArray:
    observation_count, parameter_count = design.shape
    if lag < 0 or lag >= observation_count:
        raise DecompositionError("Newey-West lag is outside the observed sample")
    bread = np.linalg.inv(design.T @ design)
    meat = np.zeros((parameter_count, parameter_count), dtype=np.float64)
    for index in range(observation_count):
        vector = design[index]
        meat += residual[index] ** 2 * np.outer(vector, vector)
    for offset in range(1, lag + 1):
        weight = 1.0 - offset / (lag + 1.0)
        cross = np.zeros_like(meat)
        for index in range(offset, observation_count):
            cross += (
                residual[index]
                * residual[index - offset]
                * np.outer(design[index], design[index - offset])
            )
        meat += weight * (cross + cross.T)
    finite_sample_scale = observation_count / (observation_count - parameter_count)
    covariance = finite_sample_scale * bread @ meat @ bread
    covariance = 0.5 * (covariance + covariance.T)
    return np.sqrt(np.maximum(np.diag(covariance), 0.0)).astype(np.float64)


def _hac_inference(
    matrix: FloatArray, target: FloatArray, fit: Fit, ridge: float, constraint: str
) -> tuple[dict[str, object], FloatArray | None]:
    policy = "DESCRIPTIVE_HAC_ONLY_NO_P_VALUES_OR_CONFIDENCE_INTERVALS"
    unavailable: dict[str, object] = {
        "default_lag": None,
        "finite_sample_scaling": "N_OVER_N_MINUS_K",
        "lag_rule": "FLOOR_4_TIMES_N_OVER_100_POWER_2_OVER_9",
        "lag_sensitivity": [],
        "policy": policy,
        "post_selection_caveat": (
            "FACTOR_SET_AND_RIDGE_WERE_SELECTED_ON_TRAINING_DATA; "
            "STANDARD_ERRORS_ARE_DESCRIPTIVE_NOT_SELECTION_ADJUSTED"
        ),
        "status": "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS",
    }
    if constraint != "UNCONSTRAINED" or ridge != 0.0 or fit.rank != matrix.shape[1] + 1:
        return unavailable, None
    design = np.column_stack((np.ones(len(target), dtype=np.float64), matrix))
    degrees = len(target) - design.shape[1]
    if degrees <= 0:
        unavailable["status"] = "UNAVAILABLE_INSUFFICIENT_DEGREES_OF_FREEDOM"
        return unavailable, None
    residual = target - _predict(fit, matrix)
    default_lag = _newey_west_lag(len(target))
    lags = sorted({0, default_lag, min(default_lag * 2, len(target) - 1)})
    sensitivity: list[dict[str, object]] = []
    default_standard_errors: FloatArray | None = None
    for lag in lags:
        standard_errors = _newey_west_standard_errors(design, residual, lag)
        if lag == default_lag:
            default_standard_errors = standard_errors
        sensitivity.append(
            {
                "lag": lag,
                "standard_errors": [_number(float(value)) for value in standard_errors],
            }
        )
    if default_standard_errors is None:
        raise DecompositionError("Newey-West default lag was not evaluated")
    return (
        {
            **unavailable,
            "default_lag": default_lag,
            "lag_sensitivity": sensitivity,
            "status": "AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION_NO_CLASSICAL_CONFIDENCE",
        },
        default_standard_errors,
    )


def _serial_correlation(values: FloatArray, lag: int) -> float | None:
    if lag <= 0 or len(values) <= lag:
        return None
    centered = values - float(np.mean(values))
    denominator = float(centered @ centered)
    if denominator <= 1e-24:
        return None
    return float(centered[lag:] @ centered[:-lag]) / denominator


def _residual_diagnostics(
    actual: FloatArray, predicted: FloatArray
) -> dict[str, object]:
    residual = actual - predicted
    count = len(residual)
    centered = residual - float(np.mean(residual))
    second_moment = float(np.mean(centered**2))
    standard_deviation = math.sqrt(max(second_moment, 0.0))
    skewness = None
    excess_kurtosis = None
    if standard_deviation > 1e-12:
        scaled = centered / standard_deviation
        skewness = float(np.mean(scaled**3))
        excess_kurtosis = float(np.mean(scaled**4)) - 3.0
    squared = residual**2
    denominator = float(residual @ residual)
    durbin_watson = (
        None
        if denominator <= 1e-24 or count < 2
        else float(np.sum(np.diff(residual) ** 2)) / denominator
    )
    lags = (1, 5, 10)
    return {
        "autocorrelation": {
            str(lag): _number(_serial_correlation(residual, lag)) for lag in lags
        },
        "durbin_watson": _number(durbin_watson),
        "excess_kurtosis": _number(excess_kurtosis),
        "maximum_absolute_residual": _number(float(np.max(np.abs(residual)))),
        "mean": _number(float(np.mean(residual))),
        "observation_count": count,
        "skewness": _number(skewness),
        "squared_residual_autocorrelation": {
            str(lag): _number(_serial_correlation(squared, lag)) for lag in lags
        },
        "standard_deviation": _number(standard_deviation),
        "status": (
            "AVAILABLE"
            if standard_deviation > 1e-12
            else "AVAILABLE_CONSTANT_RESIDUAL_LIMITED"
        ),
    }


def _metrics(
    actual: FloatArray,
    predicted: FloatArray,
    *,
    baseline: float,
    factor_count: int,
    periods_per_year: int,
) -> dict[str, object]:
    residual = actual - predicted
    r2 = _r_squared(actual, predicted, baseline)
    adjusted = None
    if r2 is not None and len(actual) > factor_count + 1:
        adjusted = 1.0 - (1.0 - r2) * (len(actual) - 1) / (
            len(actual) - factor_count - 1
        )
    tracking_error = (
        None
        if len(residual) < 2
        else float(np.std(residual, ddof=1)) * math.sqrt(periods_per_year)
    )
    result: dict[str, object] = {
        "adjusted_r_squared": _number(adjusted),
        "observation_count": len(actual),
        "r_squared": _number(r2),
        "rmse": _number(float(np.sqrt(np.mean(residual**2)))),
        "tracking_error_annualized": _number(tracking_error),
    }
    return result


def _annualize_coefficient_standard_error(
    periodic_standard_error: float | None, periods_per_year: int
) -> float | None:
    """Apply the same linear transform used for an annualized coefficient."""
    if periodic_standard_error is None:
        return None
    return periodic_standard_error * periods_per_year


def _dynamic_model_result(
    model: ModelDefinition,
    target_rows: Sequence[TargetRow],
    aligned: Sequence[tuple[TargetRow, FactorRow, tuple[float, ...]]],
    train: Sequence[tuple[TargetRow, FactorRow, tuple[float, ...]]],
    test: Sequence[tuple[TargetRow, FactorRow, tuple[float, ...]]],
    aligned_matrix: FloatArray,
    aligned_target: FloatArray,
    train_matrix: FloatArray,
    train_target: FloatArray,
    test_matrix: FloatArray,
    test_target: FloatArray,
    coverage: Mapping[str, object],
    training_design: Mapping[str, object],
) -> dict[str, object]:
    ridge = model.ridge_candidates[0]
    sensitivity: list[dict[str, object]] = []
    scored: list[tuple[float, float]] = []
    for smoothness in model.smoothness_candidates:
        predicted_r_squared, _predictions = _dynamic_loo_score(
            train_matrix,
            train_target,
            smoothness=smoothness,
            ridge=ridge,
        )
        fit = _dynamic_fit(
            train_matrix,
            train_target,
            smoothness=smoothness,
            ridge=ridge,
        )
        differences = np.diff(fit.exposures, axis=0)
        roughness = float(np.sum(differences**2))
        turnover = float(np.sum(np.abs(differences)))
        if predicted_r_squared is not None:
            scored.append((predicted_r_squared, smoothness))
        sensitivity.append(
            {
                "leave_one_out_observations": len(train_target),
                "predicted_r_squared": _number(predicted_r_squared),
                "roughness_l2_squared": _number(roughness),
                "smoothness": _number(smoothness),
                "turnover_l1": _number(turnover),
            }
        )
    if not scored:
        return _unavailable_result(
            model,
            target_rows,
            "UNAVAILABLE_DYNAMIC_CROSS_VALIDATION",
            coverage,
        )
    best_score = max(item[0] for item in scored)
    eligible = [
        item for item in scored if best_score - item[0] <= model.parsimony_tolerance
    ]
    selected_score, selected_smoothness = max(eligible, key=lambda item: item[1])
    train_fit = _dynamic_fit(
        train_matrix,
        train_target,
        smoothness=selected_smoothness,
        ridge=ridge,
    )

    history_matrix = train_matrix.copy()
    history_target = train_target.copy()
    test_predictions: list[float] = []
    filtered_exposures: dict[date, FloatArray] = {}
    holdout_forecasts: list[dict[str, object]] = []
    for index, observation in enumerate(test):
        prior_fit = _dynamic_fit(
            history_matrix,
            history_target,
            smoothness=selected_smoothness,
            ridge=ridge,
            fixed_intercept=train_fit.intercept,
        )
        forecast_exposure = prior_fit.exposures[-1].copy()
        prediction = float(train_fit.intercept + test_matrix[index] @ forecast_exposure)
        test_predictions.append(prediction)
        history_matrix = np.vstack((history_matrix, test_matrix[index]))
        history_target = np.append(history_target, test_target[index])
        posterior_fit = _dynamic_fit(
            history_matrix,
            history_target,
            smoothness=selected_smoothness,
            ridge=ridge,
            fixed_intercept=train_fit.intercept,
        )
        filtered_exposure = posterior_fit.exposures[-1].copy()
        day = observation[0].day
        filtered_exposures[day] = filtered_exposure
        holdout_forecasts.append(
            {
                "actual_excess_return": _number(test_target[index]),
                "date": day.isoformat(),
                "forecast_exposures": {
                    factor_id: _number(forecast_exposure[column])
                    for column, factor_id in enumerate(model.factor_ids)
                },
                "predicted_excess_return": _number(prediction),
            }
        )
    test_predicted = np.asarray(test_predictions, dtype=np.float64)
    holdout_r_squared = _r_squared(
        test_target, test_predicted, float(np.mean(train_target))
    )
    static_benchmark_fit = _fit(
        train_matrix,
        train_target,
        constraint="UNCONSTRAINED",
        ridge=ridge,
    )
    static_benchmark_predictions = _predict(static_benchmark_fit, test_matrix)
    static_benchmark_r_squared = _r_squared(
        test_target,
        static_benchmark_predictions,
        float(np.mean(train_target)),
    )
    holdout_delta_vs_static = (
        None
        if holdout_r_squared is None or static_benchmark_r_squared is None
        else holdout_r_squared - static_benchmark_r_squared
    )
    benchmark_status = (
        "UNAVAILABLE_STATIC_BENCHMARK"
        if holdout_delta_vs_static is None
        else "OUTPERFORMS_STATIC_SAME_FACTORS_HOLDOUT"
        if holdout_delta_vs_static > 0.0
        else "DOES_NOT_OUTPERFORM_STATIC_SAME_FACTORS_HOLDOUT"
    )

    train_exposures = {
        observation[0].day: train_fit.exposures[index].copy()
        for index, observation in enumerate(train)
    }
    last_train_exposure = train_fit.exposures[-1].copy()
    exposure_path: list[dict[str, object]] = []
    contributions: list[dict[str, object]] = []
    previous_exposure: FloatArray | None = None
    change_rows: list[tuple[float, str]] = []
    for index, observation in enumerate(aligned):
        target_row = observation[0]
        if target_row.day in train_exposures:
            exposure = train_exposures[target_row.day]
            estimation_state = "TRAIN_SMOOTHED"
            partition = "TRAIN"
        elif target_row.day in filtered_exposures:
            exposure = filtered_exposures[target_row.day]
            estimation_state = "HOLDOUT_FILTERED_CAUSAL"
            partition = "TEST"
        else:
            exposure = last_train_exposure
            estimation_state = "EMBARGO_FORECAST_NO_UPDATE"
            partition = "GAP"
        fitted = float(train_fit.intercept + aligned_matrix[index] @ exposure)
        factor_contributions = {
            factor_id: _number(exposure[column] * aligned_matrix[index, column])
            for column, factor_id in enumerate(model.factor_ids)
        }
        residual = float(aligned_target[index] - fitted)
        reconstructed = (
            train_fit.intercept
            + sum(float(cast(str, value)) for value in factor_contributions.values())
            + residual
        )
        if not math.isclose(
            reconstructed,
            float(aligned_target[index]),
            rel_tol=0.0,
            abs_tol=5e-11,
        ):
            raise DecompositionError("dynamic factor contributions do not reconcile")
        change_norm = (
            None
            if previous_exposure is None
            else float(np.linalg.norm(exposure - previous_exposure))
        )
        if change_norm is not None:
            change_rows.append((change_norm, target_row.day.isoformat()))
        exposure_path.append(
            {
                "betas": {
                    factor_id: _number(exposure[column])
                    for column, factor_id in enumerate(model.factor_ids)
                },
                "date": target_row.day.isoformat(),
                "estimation_state": estimation_state,
                "exposure_change_l2": _number(change_norm),
                "exposures": {
                    factor_id: _number(exposure[column])
                    for column, factor_id in enumerate(model.factor_ids)
                },
            }
        )
        contributions.append(
            {
                "actual_excess_return": _number(aligned_target[index]),
                "alpha_contribution": _number(train_fit.intercept),
                "date": target_row.day.isoformat(),
                "factor_contributions": factor_contributions,
                "factor_exposures": {
                    factor_id: _number(exposure[column])
                    for column, factor_id in enumerate(model.factor_ids)
                },
                "fitted_excess_return": _number(fitted),
                "partition": partition,
                "residual": _number(residual),
            }
        )
        previous_exposure = exposure

    latest_exposure = previous_exposure
    if latest_exposure is None:
        raise DecompositionError("dynamic exposure path is empty")
    evaluation_means = np.mean(test_matrix, axis=0)
    exposures = [
        {
            "beta": _number(latest_exposure[index]),
            "confidence_status": "UNAVAILABLE_DYNAMIC_PATH_INFERENCE",
            "exposure": _number(latest_exposure[index]),
            "factor_id": factor_id,
            "holdout_mean_period_contribution": _number(
                latest_exposure[index] * evaluation_means[index]
            ),
            "standard_error": None,
        }
        for index, factor_id in enumerate(model.factor_ids)
    ]

    periodic_cost = (
        None
        if model.cost_coverage != "COMPLETE"
        else float(np.mean([cast(float, item[0].cost) for item in train]))
    )
    observed_basis = target_rows[0].return_basis
    gross_alpha = None
    net_alpha = None
    gross_status = "UNAVAILABLE_INCOMPLETE_COST_COVERAGE"
    net_status = "UNAVAILABLE_INCOMPLETE_COST_COVERAGE"
    annualized_observed = train_fit.intercept * model.periods_per_year
    if observed_basis == "GROSS":
        gross_alpha = annualized_observed
        gross_status = "AVAILABLE_OBSERVED_BASIS"
        if periodic_cost is not None:
            net_alpha = (train_fit.intercept - periodic_cost) * model.periods_per_year
            net_status = "AVAILABLE_COMPLETE_COST_CONVERSION"
    else:
        net_alpha = annualized_observed
        net_status = "AVAILABLE_OBSERVED_BASIS"
        if periodic_cost is not None:
            gross_alpha = (train_fit.intercept + periodic_cost) * model.periods_per_year
            gross_status = "AVAILABLE_COMPLETE_COST_CONVERSION"

    largest_changes = [
        {"date": day, "exposure_change_l2": _number(value)}
        for value, day in sorted(change_rows, key=lambda item: (-item[0], item[1]))[:5]
    ]
    result: dict[str, object] = {
        "alpha": {
            "annualized_standard_error": None,
            "confidence_status": "UNAVAILABLE_DYNAMIC_PATH_INFERENCE",
            "gross_annualized": _number(gross_alpha),
            "gross_status": gross_status,
            "net_annualized": _number(net_alpha),
            "net_status": net_status,
            "periodic_intercept": _number(train_fit.intercept),
        },
        "constraint": model.constraint,
        "contributions": contributions,
        "cost_coverage": model.cost_coverage,
        "coverage": dict(coverage),
        "diagnostics": {
            "dynamic": {
                "exposure_path": exposure_path,
                "holdout_forecasts": holdout_forecasts,
                "holdout_comparison": {
                    "dynamic_predicted_r_squared": _number(holdout_r_squared),
                    "dynamic_minus_static_predicted_r_squared": _number(
                        holdout_delta_vs_static
                    ),
                    "selection_effect": "DIAGNOSTIC_ONLY_HOLDOUT_NOT_USED_FOR_SELECTION",
                    "static_predicted_r_squared": _number(static_benchmark_r_squared),
                    "status": benchmark_status,
                },
                "largest_exposure_changes": largest_changes,
                "rank_status": train_fit.rank_status,
                "selected_smoothness": _number(selected_smoothness),
                "selection_predicted_r_squared": _number(selected_score),
                "selection_rule": (
                    "MAX_TRAIN_LOO_PREDICTED_R2_THEN_SMOOTHEST_WITHIN_TOLERANCE"
                ),
                "state_transition": "IDENTITY_RANDOM_WALK",
                "validation": "EXACT_LEAVE_ONE_OUT_TRAIN_ONLY",
            },
            "holdout": {
                **_metrics(
                    test_target,
                    test_predicted,
                    baseline=float(np.mean(train_target)),
                    factor_count=len(model.factor_ids),
                    periods_per_year=model.periods_per_year,
                ),
                "predicted_r_squared": _number(holdout_r_squared),
                "prediction_mode": "CAUSAL_WALK_FORWARD_PRIOR_STATE",
            },
            "rank": None,
            "rank_status": train_fit.rank_status,
            "inference": {
                "default_lag": None,
                "finite_sample_scaling": "N_OVER_N_MINUS_K",
                "lag_rule": "FLOOR_4_TIMES_N_OVER_100_POWER_2_OVER_9",
                "lag_sensitivity": [],
                "policy": "DESCRIPTIVE_HAC_ONLY_NO_P_VALUES_OR_CONFIDENCE_INTERVALS",
                "post_selection_caveat": (
                    "DYNAMIC_PATH_AND_SMOOTHNESS_SELECTION_REQUIRE "
                    "METHOD_SPECIFIC_INFERENCE_NOT_CLAIMED_HERE"
                ),
                "status": "UNAVAILABLE_METHOD_NOT_CLASSICAL_OLS",
            },
            "residuals": {
                "holdout": _residual_diagnostics(test_target, test_predicted),
                "train": _residual_diagnostics(train_target, train_fit.fitted),
            },
            "training_design": dict(training_design),
            "train": _metrics(
                train_target,
                train_fit.fitted,
                baseline=float(np.mean(train_target)),
                factor_count=len(model.factor_ids),
                periods_per_year=model.periods_per_year,
            ),
        },
        "estimator": model.estimator,
        "evidence_layer": target_rows[0].evidence_layer,
        "exposures": exposures,
        "factor_ids": list(model.factor_ids),
        "method_label": METHOD_LABEL,
        "model_id": model.model_id,
        "parsimony_tolerance": _number(model.parsimony_tolerance),
        "ridge_sensitivity": [
            {
                "cv_observations": len(train_target),
                "cv_predicted_r_squared": _number(selected_score),
                "holdout_predicted_r_squared": _number(holdout_r_squared),
                "ridge": _number(ridge),
            }
        ],
        "selected_for_series": False,
        "selected_ridge": _number(ridge),
        "selected_smoothness": _number(selected_smoothness),
        "series_id": model.series_id,
        "smoothness_sensitivity": sensitivity,
        "status": "AVAILABLE",
    }
    return _apply_model_quality_gate(result, holdout_r_squared)


def _unavailable_result(
    model: ModelDefinition,
    rows: Sequence[TargetRow] | None,
    status: str,
    coverage: Mapping[str, object],
) -> dict[str, object]:
    first = rows[0] if rows else None
    return {
        "alpha": {
            "gross_annualized": None,
            "gross_status": "UNAVAILABLE_MODEL",
            "net_annualized": None,
            "net_status": "UNAVAILABLE_MODEL",
            "periodic_intercept": None,
        },
        "constraint": model.constraint,
        "computational_status": "UNAVAILABLE",
        "contributions": [],
        "cost_coverage": model.cost_coverage,
        "coverage": dict(coverage),
        "diagnostics": {},
        "estimator": model.estimator,
        "evidence_layer": first.evidence_layer if first else None,
        "exposures": [],
        "factor_ids": list(model.factor_ids),
        "method_label": METHOD_LABEL,
        "model_id": model.model_id,
        "model_quality": {
            "decision_rule": "SEALED_HOLDOUT_PREDICTED_R2_GT_ZERO",
            "decision_status": "UNAVAILABLE",
            "gate_reason": status,
            "holdout_predicted_r_squared": None,
        },
        "ridge_sensitivity": [],
        "selected_for_series": False,
        "selected_ridge": None,
        "series_id": model.series_id,
        "status": status,
    }


def _model_result(
    model: ModelDefinition,
    target_rows: Sequence[TargetRow] | None,
    factor_rows: Mapping[date, FactorRow],
    factor_definitions: Mapping[str, FactorDefinition],
    maximum_staleness_days: int,
) -> dict[str, object]:
    live_target_rows = [
        row for row in (target_rows or ()) if row.evidence_layer != "BACKTEST"
    ]
    complete_factor_dates = [
        observed
        for observed, factor_row in factor_rows.items()
        if all(
            factor_row.values.get(factor_id) is not None
            for factor_id in model.factor_ids
        )
    ]
    factor_observed_start = (
        min(complete_factor_dates).isoformat() if complete_factor_dates else None
    )
    factor_observed_end = (
        max(complete_factor_dates).isoformat() if complete_factor_dates else None
    )
    empty_coverage: dict[str, object] = {
        "aligned_observations": 0,
        "alignment_ratio": "0",
        "canonical_live_series_sha256": (
            _canonical_live_series_sha256(live_target_rows)
            if live_target_rows
            else None
        ),
        "canonical_live_observed_end": (
            live_target_rows[-1].day.isoformat() if live_target_rows else None
        ),
        "canonical_live_observed_start": (
            live_target_rows[0].day.isoformat() if live_target_rows else None
        ),
        "canonical_live_observations": len(live_target_rows),
        "factor_missing_target_dates": 0,
        "factor_missing_weekdays": 0,
        "factor_missing_weekends": 0,
        "factor_observations": len(complete_factor_dates),
        "factor_observed_end": factor_observed_end,
        "factor_observed_start": factor_observed_start,
        "limiting_input": "UNAVAILABLE_NO_ALIGNED_OBSERVATIONS",
        "observed_end": None,
        "observed_start": None,
        "source_lag_calendar_days": None,
        "target_observations": len(target_rows or ()),
        "target_cumulative_return": None,
        "target_current_drawdown": None,
        "target_max_drawdown": None,
        "target_observed_end": target_rows[-1].day.isoformat() if target_rows else None,
        "target_observed_start": target_rows[0].day.isoformat()
        if target_rows
        else None,
        "test_observations": 0,
        "train_observations": 0,
    }
    if not target_rows:
        return _unavailable_result(
            model, None, "UNAVAILABLE_UNKNOWN_TARGET_SERIES", empty_coverage
        )

    aligned: list[tuple[TargetRow, FactorRow, tuple[float, ...]]] = []
    missing_dates: list[date] = []
    for target_row in target_rows:
        factor_row = factor_rows.get(target_row.day)
        if factor_row is None:
            missing_dates.append(target_row.day)
            continue
        values = tuple(factor_row.values[factor_id] for factor_id in model.factor_ids)
        if any(value is None for value in values):
            missing_dates.append(target_row.day)
            continue
        aligned.append(
            (
                target_row,
                factor_row,
                tuple(cast(float, value) for value in values),
            )
        )

    train = [item for item in aligned if item[0].day <= model.train_end]
    test = [item for item in aligned if item[0].day >= model.test_start]
    current_drawdown: float | None = None
    maximum_drawdown: float | None = None
    cumulative_return: float | None = None
    if all(row.return_type == "TOTAL" for row in target_rows):
        wealth = 1.0
        peak = 1.0
        maximum_drawdown = 0.0
        for row in target_rows:
            wealth *= 1.0 + row.value
            peak = max(peak, wealth)
            maximum_drawdown = max(maximum_drawdown, 1.0 - wealth / peak)
        current_drawdown = 1.0 - wealth / peak
        cumulative_return = wealth - 1.0
    latest_target = target_rows[-1].day
    latest_factor = max(complete_factor_dates) if complete_factor_dates else None
    aligned_end = aligned[-1][0].day if aligned else None
    natural_common_end = (
        min(latest_target, latest_factor) if latest_factor is not None else None
    )
    if latest_factor is None:
        limiting_input = "FACTOR_SOURCE_NO_COMPLETE_MODEL_ROW"
    elif (
        aligned_end is not None
        and natural_common_end is not None
        and aligned_end < natural_common_end
    ):
        limiting_input = "EXACT_DATE_ALIGNMENT_GAP"
    elif latest_target > latest_factor:
        limiting_input = "FACTOR_SOURCE_PUBLICATION_LAG"
    elif latest_target < latest_factor:
        limiting_input = "STRATEGY_TARGET"
    else:
        limiting_input = "COMMON_END_DATE"
    coverage = {
        "aligned_observations": len(aligned),
        "alignment_ratio": _number(len(aligned) / len(target_rows)),
        "canonical_live_series_sha256": _canonical_live_series_sha256(live_target_rows),
        "canonical_live_observed_end": live_target_rows[-1].day.isoformat(),
        "canonical_live_observed_start": live_target_rows[0].day.isoformat(),
        "canonical_live_observations": len(live_target_rows),
        "factor_missing_target_dates": len(missing_dates),
        "factor_missing_weekdays": sum(day.weekday() < 5 for day in missing_dates),
        "factor_missing_weekends": sum(day.weekday() >= 5 for day in missing_dates),
        "factor_observations": len(complete_factor_dates),
        "factor_observed_end": factor_observed_end,
        "factor_observed_start": factor_observed_start,
        "limiting_input": limiting_input,
        "observed_end": aligned[-1][0].day.isoformat() if aligned else None,
        "observed_start": aligned[0][0].day.isoformat() if aligned else None,
        "source_lag_calendar_days": (
            max(0, (latest_target - latest_factor).days)
            if latest_factor is not None
            else None
        ),
        "target_observations": len(target_rows),
        "target_cumulative_return": _number(cumulative_return),
        "target_current_drawdown": _number(current_drawdown),
        "target_max_drawdown": _number(maximum_drawdown),
        "target_observed_end": target_rows[-1].day.isoformat(),
        "target_observed_start": target_rows[0].day.isoformat(),
        "test_observations": len(test),
        "train_observations": len(train),
    }
    if not aligned:
        return _unavailable_result(
            model, target_rows, "UNAVAILABLE_NO_ALIGNED_OBSERVATIONS", coverage
        )
    if latest_factor is None:
        return _unavailable_result(
            model, target_rows, "UNAVAILABLE_NO_COMPLETE_FACTOR_OBSERVATIONS", coverage
        )
    if (latest_target - latest_factor).days > maximum_staleness_days:
        return _unavailable_result(
            model, target_rows, "UNAVAILABLE_STALE_FACTORS", coverage
        )
    if len(train) < model.minimum_train_observations:
        return _unavailable_result(
            model, target_rows, "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY", coverage
        )
    if len(test) < model.minimum_test_observations:
        return _unavailable_result(
            model, target_rows, "UNAVAILABLE_INSUFFICIENT_TEST_HISTORY", coverage
        )
    if model.cost_coverage == "COMPLETE" and any(
        item[0].cost is None for item in aligned
    ):
        raise DecompositionError(
            f"model {model.model_id} declares COMPLETE costs but has missing costs"
        )

    factor_return_types = tuple(
        factor_definitions[factor_id].return_type for factor_id in model.factor_ids
    )

    def arrays(
        observations: Sequence[tuple[TargetRow, FactorRow, tuple[float, ...]]],
    ) -> tuple[FloatArray, FloatArray]:
        matrix_rows: list[list[float]] = []
        target_values: list[float] = []
        for target_row, factor_row, values in observations:
            target_value = target_row.value
            if target_row.return_type == "TOTAL":
                target_value -= factor_row.risk_free
            factor_values = [
                value - factor_row.risk_free if return_type == "TOTAL" else value
                for value, return_type in zip(values, factor_return_types, strict=True)
            ]
            target_values.append(target_value)
            matrix_rows.append(factor_values)
        return (
            np.asarray(matrix_rows, dtype=np.float64),
            np.asarray(target_values, dtype=np.float64),
        )

    train_matrix, train_target = arrays(train)
    test_matrix, test_target = arrays(test)
    aligned_matrix, aligned_target = arrays(aligned)
    training_design = _training_design_diagnostics(train_matrix, model.factor_ids)
    if model.estimator == "DYNAMIC_FLS":
        return _dynamic_model_result(
            model,
            target_rows,
            aligned,
            train,
            test,
            aligned_matrix,
            aligned_target,
            train_matrix,
            train_target,
            test_matrix,
            test_target,
            coverage,
            training_design,
        )
    sensitivity: list[dict[str, object]] = []
    scored: list[tuple[float, float]] = []
    for ridge in model.ridge_candidates:
        if ridge == 0.0 and training_design["unregularized_status"] != "ELIGIBLE":
            sensitivity.append(
                {
                    "cv_observations": 0,
                    "cv_predicted_r_squared": None,
                    "eligibility": "BLOCKED_COLLINEAR_UNREGULARIZED_DESIGN",
                    "holdout_predicted_r_squared": None,
                    "ridge": _number(ridge),
                }
            )
            continue
        cv_score, cv_observations = _cv_score(train_matrix, train_target, model, ridge)
        test_score: float | None = None
        if cv_score is not None:
            candidate_fit = _fit(
                train_matrix,
                train_target,
                constraint=model.constraint,
                ridge=ridge,
            )
            test_score = _r_squared(
                test_target,
                _predict(candidate_fit, test_matrix),
                float(np.mean(train_target)),
            )
            scored.append((cv_score, ridge))
        sensitivity.append(
            {
                "cv_observations": cv_observations,
                "cv_predicted_r_squared": _number(cv_score),
                "eligibility": "ELIGIBLE",
                "holdout_predicted_r_squared": _number(test_score),
                "ridge": _number(ridge),
            }
        )
    if not scored:
        status = (
            "UNAVAILABLE_COLLINEAR_UNREGULARIZED_DESIGN"
            if training_design["unregularized_status"] != "ELIGIBLE"
            and all(ridge == 0.0 for ridge in model.ridge_candidates)
            else "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV"
        )
        unavailable = _unavailable_result(model, target_rows, status, coverage)
        unavailable["diagnostics"] = {"training_design": training_design}
        unavailable["ridge_sensitivity"] = sensitivity
        return unavailable
    selected_ridge = min(scored, key=lambda item: (-item[0], item[1]))[1]
    fit = _fit(
        train_matrix,
        train_target,
        constraint=model.constraint,
        ridge=selected_ridge,
    )
    train_predicted = _predict(fit, train_matrix)
    test_predicted = _predict(fit, test_matrix)
    holdout_r2 = _r_squared(test_target, test_predicted, float(np.mean(train_target)))
    inference, standard_errors = _hac_inference(
        train_matrix, train_target, fit, selected_ridge, model.constraint
    )
    confidence_status = cast(str, inference["status"])

    aligned_predicted = _predict(fit, aligned_matrix)
    contributions: list[dict[str, object]] = []
    for index, (target_row, _factor_row, _values) in enumerate(aligned):
        factor_contributions = {
            factor_id: _number(fit.exposures[column] * aligned_matrix[index, column])
            for column, factor_id in enumerate(model.factor_ids)
        }
        residual = float(aligned_target[index] - aligned_predicted[index])
        reconciled = (
            fit.intercept
            + sum(float(cast(str, value)) for value in factor_contributions.values())
            + residual
        )
        if not math.isclose(
            reconciled, float(aligned_target[index]), rel_tol=0.0, abs_tol=5e-11
        ):
            raise DecompositionError("factor contributions do not reconcile")
        partition = (
            "TRAIN"
            if target_row.day <= model.train_end
            else "TEST"
            if target_row.day >= model.test_start
            else "GAP"
        )
        contributions.append(
            {
                "actual_excess_return": _number(aligned_target[index]),
                "alpha_contribution": _number(fit.intercept),
                "date": target_row.day.isoformat(),
                "factor_contributions": factor_contributions,
                "fitted_excess_return": _number(aligned_predicted[index]),
                "partition": partition,
                "residual": _number(residual),
            }
        )

    evaluation_means = np.mean(test_matrix, axis=0)
    exposures: list[dict[str, object]] = []
    for index, factor_id in enumerate(model.factor_ids):
        standard_error = (
            None if standard_errors is None else float(standard_errors[index + 1])
        )
        exposures.append(
            {
                "beta": _number(fit.exposures[index]),
                "confidence_status": confidence_status,
                "exposure": _number(fit.exposures[index]),
                "factor_id": factor_id,
                "holdout_mean_period_contribution": _number(
                    fit.exposures[index] * evaluation_means[index]
                ),
                "standard_error": _number(standard_error),
            }
        )

    periodic_cost = (
        None
        if model.cost_coverage != "COMPLETE"
        else float(np.mean([cast(float, item[0].cost) for item in train]))
    )
    observed_basis = target_rows[0].return_basis
    gross_alpha = None
    net_alpha = None
    gross_status = "UNAVAILABLE_INCOMPLETE_COST_COVERAGE"
    net_status = "UNAVAILABLE_INCOMPLETE_COST_COVERAGE"
    annualized_observed = fit.intercept * model.periods_per_year
    if observed_basis == "GROSS":
        gross_alpha = annualized_observed
        gross_status = "AVAILABLE_OBSERVED_BASIS"
        if periodic_cost is not None:
            net_alpha = (fit.intercept - periodic_cost) * model.periods_per_year
            net_status = "AVAILABLE_COMPLETE_COST_CONVERSION"
    else:
        net_alpha = annualized_observed
        net_status = "AVAILABLE_OBSERVED_BASIS"
        if periodic_cost is not None:
            gross_alpha = (fit.intercept + periodic_cost) * model.periods_per_year
            gross_status = "AVAILABLE_COMPLETE_COST_CONVERSION"

    alpha_standard_error = _annualize_coefficient_standard_error(
        None if standard_errors is None else float(standard_errors[0]),
        model.periods_per_year,
    )
    result: dict[str, object] = {
        "alpha": {
            "annualized_standard_error": _number(alpha_standard_error),
            "confidence_status": confidence_status,
            "gross_annualized": _number(gross_alpha),
            "gross_status": gross_status,
            "net_annualized": _number(net_alpha),
            "net_status": net_status,
            "periodic_intercept": _number(fit.intercept),
        },
        "constraint": model.constraint,
        "contributions": contributions,
        "cost_coverage": model.cost_coverage,
        "coverage": coverage,
        "diagnostics": {
            "holdout": {
                **_metrics(
                    test_target,
                    test_predicted,
                    baseline=float(np.mean(train_target)),
                    factor_count=len(model.factor_ids),
                    periods_per_year=model.periods_per_year,
                ),
                "predicted_r_squared": _number(holdout_r2),
            },
            "rank": fit.rank,
            "rank_status": fit.rank_status,
            "inference": inference,
            "residuals": {
                "holdout": _residual_diagnostics(test_target, test_predicted),
                "train": _residual_diagnostics(train_target, train_predicted),
            },
            "training_design": training_design,
            "train": _metrics(
                train_target,
                train_predicted,
                baseline=float(np.mean(train_target)),
                factor_count=len(model.factor_ids),
                periods_per_year=model.periods_per_year,
            ),
        },
        "estimator": model.estimator,
        "evidence_layer": target_rows[0].evidence_layer,
        "exposures": exposures,
        "factor_ids": list(model.factor_ids),
        "method_label": METHOD_LABEL,
        "model_id": model.model_id,
        "parsimony_tolerance": _number(model.parsimony_tolerance),
        "ridge_sensitivity": sensitivity,
        "selected_for_series": False,
        "selected_ridge": _number(selected_ridge),
        "series_id": model.series_id,
        "status": "AVAILABLE",
    }
    return _apply_model_quality_gate(result, holdout_r2)


def _select_models(
    results: list[dict[str, object]], models: Sequence[ModelDefinition]
) -> list[dict[str, object]]:
    definitions = {item.model_id: item for item in models}
    selections: list[dict[str, object]] = []
    for series_id in sorted({item.series_id for item in models}):
        computable: list[tuple[dict[str, object], float]] = []
        for result in results:
            if (
                result["series_id"] != series_id
                or result.get("computational_status") != "AVAILABLE"
            ):
                continue
            selected_ridge = result["selected_ridge"]
            sensitivity = cast(list[dict[str, Any]], result["ridge_sensitivity"])
            selected_cv = next(
                (
                    item["cv_predicted_r_squared"]
                    for item in sensitivity
                    if item["ridge"] == selected_ridge
                ),
                None,
            )
            if selected_cv is not None:
                computable.append((result, float(selected_cv)))
        if not computable:
            selections.append(
                {
                    "model_id": None,
                    "series_id": series_id,
                    "status": "UNAVAILABLE_NO_VALIDATED_MODEL",
                }
            )
            continue
        best_score = max(item[1] for item in computable)
        eligible = [
            item
            for item in computable
            if best_score - item[1]
            <= definitions[cast(str, item[0]["model_id"])].parsimony_tolerance
        ]
        selected, selection_score = min(
            eligible,
            key=lambda item: (
                len(cast(list[object], item[0]["factor_ids"])),
                cast(str, item[0]["model_id"]),
            ),
        )
        selected["selected_for_series"] = True
        diagnostics = cast(dict[str, Any], selected["diagnostics"])
        holdout = cast(dict[str, Any], diagnostics["holdout"])
        decision_status = cast(dict[str, object], selected["model_quality"])[
            "decision_status"
        ]
        selections.append(
            {
                "factor_count": len(cast(list[object], selected["factor_ids"])),
                "holdout_predicted_r_squared": holdout["predicted_r_squared"],
                "model_id": selected["model_id"],
                "selection_cv_predicted_r_squared": _number(selection_score),
                "series_id": series_id,
                "status": (
                    "SELECTED_PARSIMONIOUS_TRAIN_CV_MODEL"
                    if decision_status == "PM_VALID"
                    else "SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL"
                ),
            }
        )
    return selections


def _proxy_governance(
    spec: Specification, results: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    selected = [item for item in results if item.get("selected_for_series") is True]
    proxies: list[dict[str, object]] = []
    for factor in sorted(spec.factors, key=lambda item: item.factor_id):
        requested_models = sorted(
            model.model_id
            for model in spec.models
            if factor.factor_id in model.factor_ids
        )
        selected_models = sorted(
            cast(str, item["model_id"])
            for item in selected
            if factor.factor_id in cast(list[str], item["factor_ids"])
        )
        selected_series = sorted(
            cast(str, item["series_id"])
            for item in selected
            if factor.factor_id in cast(list[str], item["factor_ids"])
        )
        governed = all(
            value is not None
            for value in (
                factor.economic_family,
                factor.effective_start,
                factor.effective_end,
                factor.raw_orthogonal,
                factor.redundancy_policy,
                factor.return_source_id,
                factor.selection_rationale,
                factor.source_id,
            )
        )
        if not governed:
            disposition = "UNAVAILABLE_LEGACY_SPEC_NO_PROXY_GOVERNANCE"
        elif selected_models:
            disposition = "SELECTED_BY_TRAIN_ONLY_MODEL_SELECTION"
        elif requested_models:
            disposition = "NOT_SELECTED_BY_TRAIN_ONLY_MODEL_SELECTION"
        else:
            disposition = "CANDIDATE_NOT_REQUESTED_BY_SPEC"
        proxies.append(
            {
                "candidate_disposition": disposition,
                "economic_family": factor.economic_family,
                "effective_end": (
                    None
                    if factor.effective_end is None
                    else factor.effective_end.isoformat()
                ),
                "effective_start": (
                    None
                    if factor.effective_start is None
                    else factor.effective_start.isoformat()
                ),
                "factor_id": factor.factor_id,
                "raw_orthogonal": factor.raw_orthogonal,
                "redundancy_policy": factor.redundancy_policy,
                "requested_model_ids": requested_models,
                "return_source_id": factor.return_source_id,
                "selected_model_ids": selected_models,
                "selected_series_ids": selected_series,
                "selection_rationale": factor.selection_rationale,
                "source_id": factor.source_id,
            }
        )
    return {
        "candidate_count": len(proxies),
        "orthogonality_claim": "RAW_CANDIDATES_NOT_ASSUMED_ORTHOGONAL",
        "proxies": proxies,
        "selection_boundary": (
            "TRAIN_ONLY_TIME_ORDERED_VALIDATION_WITH_COLLINEARITY_AND_PARSIMONY_GATES"
        ),
    }


def build_decomposition(
    spec_path: Path, source_root: Path
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    spec = _load_spec(spec_path)
    target_path = _verify_source(source_root, spec.target_source)
    factor_path = _verify_source(source_root, spec.factor_source)
    provenance_paths = tuple(
        (definition, _verify_source(source_root, definition))
        for definition in spec.provenance_sources
    )
    targets = _read_targets(target_path, spec.as_of_date, spec.evidence_mode)
    factors, _headers = _read_factors(factor_path, spec)
    definitions = {item.factor_id: item for item in spec.factors}
    results = [
        _model_result(
            model,
            targets.get(model.series_id),
            factors,
            definitions,
            spec.maximum_factor_staleness_days,
        )
        for model in spec.models
    ]
    for model_result in results:
        model_result["evidence_mode"] = spec.evidence_mode
        model_result["evidence_layers"] = sorted(
            {
                row.evidence_layer
                for row in targets.get(str(model_result.get("series_id")), [])
            }
        )
        model_result["series_composition"] = dict(
            spec.series_composition.get(str(model_result.get("series_id")), {})
        )
    selections = _select_models(results, spec.models)
    methodology_hash, methodology_size = hash_file(METHODOLOGY_PATH)
    source_summary = {
        "factor": {
            "artifact_id": spec.factor_source.artifact_id,
            "sha256": spec.factor_source.expected_sha256,
            "size_bytes": spec.factor_source.expected_size_bytes,
        },
        "target": {
            "artifact_id": spec.target_source.artifact_id,
            "sha256": spec.target_source.expected_sha256,
            "size_bytes": spec.target_source.expected_size_bytes,
        },
        "provenance": [
            {
                "artifact_id": definition.artifact_id,
                "sha256": definition.expected_sha256,
                "size_bytes": definition.expected_size_bytes,
            }
            for definition, _path in provenance_paths
        ],
    }
    payload: dict[str, object] = {
        "analysis_lens": spec.analysis_lens,
        "analytics_schema_version": ANALYTICS_SCHEMA_VERSION,
        "as_of_date": spec.as_of_date.isoformat(),
        "builder_version": BUILDER_VERSION,
        "evidence_mode": spec.evidence_mode,
        "method_label": METHOD_LABEL,
        "methodology_manifest_sha256": methodology_hash,
        "methodology_version": METHODOLOGY_VERSION,
        "model_results": results,
        "model_selections": selections,
        "proxy_governance": _proxy_governance(spec, results),
        "series_composition": {
            key: dict(value) for key, value in sorted(spec.series_composition.items())
        },
        "sources": source_summary,
    }
    analytics_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    result: dict[str, object] = {"analytics_hash": analytics_hash, **payload}
    result_bytes = canonical_json_bytes(result)

    artifacts = (
        SourceArtifact.from_path(
            spec_path,
            artifact_id="spec/decomposition-spec.json",
            as_of_date=spec.as_of_date,
            schema_name="portfolio-decomposition-spec",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id=spec.target_source.artifact_id,
            sha256=spec.target_source.expected_sha256,
            size_bytes=spec.target_source.expected_size_bytes,
            as_of_date=spec.as_of_date,
            schema_name=spec.target_source.schema_name,
            schema_version=spec.target_source.schema_version,
        ),
        SourceArtifact(
            artifact_id=spec.factor_source.artifact_id,
            sha256=spec.factor_source.expected_sha256,
            size_bytes=spec.factor_source.expected_size_bytes,
            as_of_date=spec.as_of_date,
            schema_name=spec.factor_source.schema_name,
            schema_version=spec.factor_source.schema_version,
        ),
        *(
            SourceArtifact(
                artifact_id=definition.artifact_id,
                sha256=definition.expected_sha256,
                size_bytes=definition.expected_size_bytes,
                as_of_date=spec.as_of_date,
                schema_name=definition.schema_name,
                schema_version=definition.schema_version,
            )
            for definition, _path in provenance_paths
        ),
        SourceArtifact(
            artifact_id="methodology/decomposition-methodology.json",
            sha256=methodology_hash,
            size_bytes=methodology_size,
            as_of_date=spec.as_of_date,
            schema_name="decomposition-methodology-catalog",
            schema_version="1",
        ),
    )
    manifest = GenerationManifest.build(
        as_of_date=spec.as_of_date,
        adapter=AdapterIdentity("portfolio-decomposition", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "All models explicitly requested by the hash-bound specification; "
                "unavailable underlying evidence remains an explicit model result."
            ),
            fact_types=(
                "analysis-lens",
                "alpha",
                "coverage",
                "factor-composition",
                "factor-contribution",
                "model-diagnostics",
                "residual",
            ),
            observed_start=min(row.day for rows in targets.values() for row in rows),
            observed_end=max(row.day for rows in targets.values() for row in rows),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    return result, manifest, result_bytes


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_candidate(
    candidate: Path, manifest: GenerationManifest, result_bytes: bytes
) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise DecompositionError("candidate must be a regular directory")
    expected = {
        "decomposition.json": result_bytes,
        "generation.json": manifest.to_json_bytes(),
    }
    if {item.name for item in candidate.iterdir()} != set(expected):
        raise DecompositionError("candidate file set differs from the contract")
    for name, content in expected.items():
        path = candidate / name
        if path.is_symlink() or path.read_bytes() != content:
            raise DecompositionError(f"candidate {name} differs from the exact build")


def stage_candidate(
    spec_path: Path, source_root: Path, output_root: Path
) -> dict[str, object]:
    result, manifest, result_bytes = build_decomposition(spec_path, source_root)
    candidates = output_root.resolve() / "candidates"
    if output_root.exists() and output_root.is_symlink():
        raise DecompositionError("output root must not be a symlink")
    candidates.mkdir(parents=True, exist_ok=True)
    candidate = candidates / manifest.generation_id
    if candidate.exists():
        _verify_candidate(candidate, manifest, result_bytes)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=candidates))
        try:
            _write_new(temporary / "decomposition.json", result_bytes)
            _write_new(temporary / "generation.json", manifest.to_json_bytes())
            rename_directory_with_retry(temporary, candidate)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {
        "analytics_hash": result["analytics_hash"],
        "candidate": str(candidate),
        "generation_id": manifest.generation_id,
        "model_count": len(cast(list[object], result["model_results"])),
    }


def _publish_candidate(
    candidate: Path,
    output_root: Path,
    manifest: GenerationManifest,
    result_bytes: bytes,
) -> CurrentPointer:
    _verify_candidate(candidate, manifest, result_bytes)
    generations = output_root.resolve() / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    destination = generations / manifest.generation_id
    if destination.exists():
        _verify_candidate(destination, manifest, result_bytes)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=generations))
        try:
            _write_new(temporary / "decomposition.json", result_bytes)
            _write_new(temporary / "generation.json", manifest.to_json_bytes())
            rename_directory_with_retry(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return publish_generation(output_root, manifest.generation_id)


def publish(spec_path: Path, source_root: Path, output_root: Path) -> dict[str, object]:
    result, manifest, result_bytes = build_decomposition(spec_path, source_root)
    candidate = output_root.resolve() / "candidates" / manifest.generation_id
    pointer = _publish_candidate(candidate, output_root, manifest, result_bytes)
    return {
        "analytics_hash": result["analytics_hash"],
        "generation_id": manifest.generation_id,
        "model_count": len(cast(list[object], result["model_results"])),
        "publication_state": pointer.state.value,
    }


def _summary(spec_path: Path, source_root: Path) -> dict[str, object]:
    result, manifest, _result_bytes = build_decomposition(spec_path, source_root)
    selections = cast(list[dict[str, object]], result["model_selections"])
    return {
        "analytics_hash": result["analytics_hash"],
        "generation_id": manifest.generation_id,
        "model_count": len(cast(list[object], result["model_results"])),
        "selected_model_count": sum(
            item["model_id"] is not None for item in selections
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "stage", "publish"):
        item = subparsers.add_parser(command)
        item.add_argument("--spec", required=True, type=Path)
        item.add_argument("--source-root", required=True, type=Path)
        if command != "build":
            item.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = _summary(args.spec, args.source_root)
    elif args.command == "stage":
        result = stage_candidate(args.spec, args.source_root, args.output_root)
    else:
        result = publish(args.spec, args.source_root, args.output_root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

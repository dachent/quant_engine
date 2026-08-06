"""Build and publish coverage-aware portfolio dependence matrices."""

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
from quant_engine.portfolio_ledger import decimal_text


BUILDER_VERSION: Final = "1.3.0"
TRADING_OBSERVATION_HORIZONS: Final = (5, 21, 22, 63, 126, 130, 252, 260)
RETURN_HEADERS: Final = ("series_id", "date", "return", "evidence_layer")
RETURN_HEADERS_V2: Final = (*RETURN_HEADERS, "strategy_version")
EVIDENCE_LAYERS: Final = {
    "ACCOUNT_LIVE",
    "PROVIDER_LIVE",
    "OFFICIAL_VEHICLE",
    "PREDECESSOR",
    "SHADOW_OOS",
    "BACKTEST",
    "CONCEPTUAL_STRESS",
}

FloatArray = npt.NDArray[np.float64]


class CorrelationError(ValueError):
    """The specification, input evidence, or dependence output is invalid."""


@dataclass(frozen=True)
class SourceDefinition:
    artifact_id: str
    relative_path: str
    expected_sha256: str
    expected_size_bytes: int
    schema_name: str
    schema_version: str


@dataclass(frozen=True)
class ReturnRow:
    day: date
    value: float
    evidence_layer: str
    strategy_version: str | None = None
    canonical_value: str | None = None


@dataclass(frozen=True)
class EvidenceModeSeries:
    series_id: str
    live_evidence_layer: str
    strategy_version: str


@dataclass(frozen=True)
class Prior:
    left: str
    right: str
    value: float
    evidence_note: str


@dataclass(frozen=True)
class ImplementationMapping:
    series_id: str
    instruments: tuple[str, ...]
    providers: tuple[str, ...]
    venues: tuple[str, ...]


@dataclass(frozen=True)
class StressComponent:
    series_id: str
    allocation_weight: float
    adverse_loss_fraction: float


@dataclass(frozen=True)
class Specification:
    schema_version: int
    as_of_date: date
    source: SourceDefinition
    series_ids: tuple[str, ...]
    minimum_observations: int
    minimum_downside_observations: int
    rolling_window: int
    minimum_rolling_windows: int
    tail_quantile: float
    full_observed_weight_observations: int
    maximum_observed_weight: float
    stress_uplift: float
    stress_cap: float
    eigenvalue_floor: float
    priors: tuple[Prior, ...]
    implementation_mappings: tuple[ImplementationMapping, ...]
    stress_components: tuple[StressComponent, ...]
    expected_correlation_one_loss: float
    correlation_one_tolerance: float
    evidence_mode_series: tuple[EvidenceModeSeries, ...]


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CorrelationError(
            f"{name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorrelationError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CorrelationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorrelationError(f"{name} must be non-empty text")
    return value.strip()


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CorrelationError(f"{name} must be an integer >= {minimum}")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CorrelationError(f"{name} must be finite numeric text or a number")
    try:
        accepted = float(value)
    except ValueError as exc:
        raise CorrelationError(f"{name} must be numeric") from exc
    if not math.isfinite(accepted):
        raise CorrelationError(f"{name} must be finite")
    return accepted


def _canonical_decimal(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CorrelationError(f"{name} must be finite numeric text or a number")
    try:
        accepted = Decimal(str(value))
    except InvalidOperation as exc:
        raise CorrelationError(f"{name} must be numeric") from exc
    if not accepted.is_finite():
        raise CorrelationError(f"{name} must be finite")
    return decimal_text(accepted)


def _canonical_series_sha256(rows: Sequence[ReturnRow]) -> str:
    payload: list[dict[str, str]] = []
    for row in sorted(rows, key=lambda item: item.day):
        if row.canonical_value is None:
            raise CorrelationError("canonical return value is unavailable")
        payload.append({"date": row.day.isoformat(), "return": row.canonical_value})
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
    lower_inclusive: bool = True,
) -> float:
    accepted = _finite(value, name)
    lower_ok = accepted >= minimum if lower_inclusive else accepted > minimum
    if not lower_ok or accepted > maximum:
        raise CorrelationError(f"{name} is outside its accepted range")
    return accepted


def _day(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise CorrelationError(f"{name} must be a canonical ISO date") from exc
    if parsed.isoformat() != accepted:
        raise CorrelationError(f"{name} must be a canonical ISO date")
    return parsed


def _portable(value: object, name: str) -> str:
    accepted = _text(value, name)
    path = PurePosixPath(accepted)
    if (
        path.is_absolute()
        or "\\" in accepted
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CorrelationError(f"{name} must be a portable relative path")
    return accepted


def _source_definition(value: object) -> SourceDefinition:
    source = _object(value, "return_source")
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
        "return_source",
    )
    digest = _text(source["expected_sha256"], "return_source.expected_sha256")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise CorrelationError(
            "return_source.expected_sha256 must be lower-case SHA-256"
        )
    return SourceDefinition(
        artifact_id=_portable(source["artifact_id"], "return_source.artifact_id"),
        relative_path=_portable(source["relative_path"], "return_source.relative_path"),
        expected_sha256=digest,
        expected_size_bytes=_integer(
            source["expected_size_bytes"], "return_source.expected_size_bytes"
        ),
        schema_name=_text(source["schema_name"], "return_source.schema_name"),
        schema_version=_text(source["schema_version"], "return_source.schema_version"),
    )


def _prior(value: Mapping[str, Any], index: int) -> Prior:
    name = f"conceptual_priors[{index}]"
    _strict_keys(value, {"evidence_note", "left", "right", "value"}, name)
    left = _text(value["left"], f"{name}.left")
    right = _text(value["right"], f"{name}.right")
    if left >= right:
        raise CorrelationError(f"{name} must use canonical left < right ordering")
    return Prior(
        left=left,
        right=right,
        value=_bounded(value["value"], f"{name}.value", minimum=-1.0, maximum=1.0),
        evidence_note=_text(value["evidence_note"], f"{name}.evidence_note"),
    )


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CorrelationError(f"{name} must be an array")
    accepted = tuple(sorted({_text(item, f"{name}[]") for item in value}))
    return accepted


def _implementation_mapping(
    value: Mapping[str, Any], index: int
) -> ImplementationMapping:
    name = f"implementation_mappings[{index}]"
    _strict_keys(value, {"instruments", "providers", "series_id", "venues"}, name)
    return ImplementationMapping(
        series_id=_text(value["series_id"], f"{name}.series_id"),
        instruments=_strings(value["instruments"], f"{name}.instruments"),
        providers=_strings(value["providers"], f"{name}.providers"),
        venues=_strings(value["venues"], f"{name}.venues"),
    )


def _stress_component(value: Mapping[str, Any], index: int) -> StressComponent:
    name = f"correlation_one.components[{index}]"
    _strict_keys(
        value, {"adverse_loss_fraction", "allocation_weight", "series_id"}, name
    )
    return StressComponent(
        series_id=_text(value["series_id"], f"{name}.series_id"),
        allocation_weight=_bounded(
            value["allocation_weight"],
            f"{name}.allocation_weight",
            minimum=0.0,
            maximum=1.0,
        ),
        adverse_loss_fraction=_bounded(
            value["adverse_loss_fraction"],
            f"{name}.adverse_loss_fraction",
            minimum=0.0,
            maximum=1.0,
        ),
    )


def _evidence_mode_definition(
    value: Mapping[str, Any], index: int
) -> EvidenceModeSeries:
    name = f"evidence_mode_series[{index}]"
    _strict_keys(
        value,
        {"live_evidence_layer", "series_id", "strategy_version"},
        name,
    )
    live_layer = _text(value["live_evidence_layer"], f"{name}.live_evidence_layer")
    if live_layer not in {"ACCOUNT_LIVE", "PROVIDER_LIVE", "OFFICIAL_VEHICLE"}:
        raise CorrelationError(
            f"{name}.live_evidence_layer is not an accepted live layer"
        )
    return EvidenceModeSeries(
        series_id=_text(value["series_id"], f"{name}.series_id"),
        live_evidence_layer=live_layer,
        strategy_version=_text(value["strategy_version"], f"{name}.strategy_version"),
    )


def _load_spec(path: Path) -> Specification:
    if path.is_symlink() or not path.is_file():
        raise CorrelationError("specification must be a regular local file")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrelationError("specification is not readable UTF-8 JSON") from exc
    root = _object(decoded, "specification")
    raw_schema_version = root.get("schema_version")
    if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
        raise CorrelationError("specification schema_version is unsupported")
    schema_version = raw_schema_version
    expected_keys = {
        "as_of_date",
        "conceptual_priors",
        "correlation_one",
        "eigenvalue_floor",
        "full_observed_weight_observations",
        "implementation_mappings",
        "maximum_observed_weight",
        "minimum_downside_observations",
        "minimum_observations",
        "minimum_rolling_windows",
        "return_source",
        "rolling_window",
        "schema_version",
        "series_ids",
        "stress_cap",
        "stress_uplift",
        "tail_quantile",
    }
    if schema_version == 2:
        expected_keys.add("evidence_mode_series")
    _strict_keys(root, expected_keys, "specification")
    if schema_version not in {1, 2}:
        raise CorrelationError("specification schema_version is unsupported")
    raw_series = root["series_ids"]
    if not isinstance(raw_series, list):
        raise CorrelationError("series_ids must be an array")
    series_ids = tuple(_text(item, "series_ids[]") for item in raw_series)
    if len(series_ids) < 2 or len(set(series_ids)) != len(series_ids):
        raise CorrelationError("series_ids must contain at least two unique values")
    priors = tuple(
        _prior(item, index)
        for index, item in enumerate(
            _objects(root["conceptual_priors"], "conceptual_priors")
        )
    )
    prior_keys = {(item.left, item.right) for item in priors}
    if len(prior_keys) != len(priors):
        raise CorrelationError("conceptual prior pairs must be unique")
    expected_pairs = {
        tuple(sorted((left, right)))
        for index, left in enumerate(series_ids)
        for right in series_ids[index + 1 :]
    }
    if prior_keys != expected_pairs:
        raise CorrelationError("conceptual priors must cover every requested pair")
    mappings = tuple(
        _implementation_mapping(item, index)
        for index, item in enumerate(
            _objects(root["implementation_mappings"], "implementation_mappings")
        )
    )
    if len({item.series_id for item in mappings}) != len(mappings):
        raise CorrelationError("implementation mappings must have unique series IDs")
    unknown_mappings = {item.series_id for item in mappings} - set(series_ids)
    if unknown_mappings:
        raise CorrelationError("implementation mapping references an unknown series")
    correlation_one = _object(root["correlation_one"], "correlation_one")
    _strict_keys(
        correlation_one, {"components", "expected_loss", "tolerance"}, "correlation_one"
    )
    components = tuple(
        _stress_component(item, index)
        for index, item in enumerate(
            _objects(correlation_one["components"], "correlation_one.components")
        )
    )
    if len({item.series_id for item in components}) != len(components):
        raise CorrelationError("correlation-one components must have unique series IDs")
    if {item.series_id for item in components} != set(series_ids):
        raise CorrelationError(
            "correlation-one components must cover all requested series"
        )
    if not math.isclose(
        sum(item.allocation_weight for item in components),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise CorrelationError("correlation-one allocation weights must sum to one")
    rolling_window = _integer(root["rolling_window"], "rolling_window", minimum=2)
    minimum_observations = _integer(
        root["minimum_observations"], "minimum_observations", minimum=3
    )
    if rolling_window < minimum_observations:
        raise CorrelationError("rolling_window cannot be below minimum_observations")
    evidence_mode_series = tuple(
        _evidence_mode_definition(item, index)
        for index, item in enumerate(
            _objects(root.get("evidence_mode_series", []), "evidence_mode_series")
        )
    )
    if schema_version == 2:
        if len({item.series_id for item in evidence_mode_series}) != len(
            evidence_mode_series
        ):
            raise CorrelationError("evidence mode series IDs must be unique")
        if {item.series_id for item in evidence_mode_series} != set(series_ids):
            raise CorrelationError(
                "evidence mode definitions must cover every requested series"
            )
    return Specification(
        schema_version=schema_version,
        as_of_date=_day(root["as_of_date"], "as_of_date"),
        source=_source_definition(root["return_source"]),
        series_ids=series_ids,
        minimum_observations=minimum_observations,
        minimum_downside_observations=_integer(
            root["minimum_downside_observations"],
            "minimum_downside_observations",
            minimum=3,
        ),
        rolling_window=rolling_window,
        minimum_rolling_windows=_integer(
            root["minimum_rolling_windows"], "minimum_rolling_windows", minimum=1
        ),
        tail_quantile=_bounded(
            root["tail_quantile"],
            "tail_quantile",
            minimum=0.0,
            maximum=0.5,
            lower_inclusive=False,
        ),
        full_observed_weight_observations=_integer(
            root["full_observed_weight_observations"],
            "full_observed_weight_observations",
            minimum=1,
        ),
        maximum_observed_weight=_bounded(
            root["maximum_observed_weight"],
            "maximum_observed_weight",
            minimum=0.0,
            maximum=1.0,
        ),
        stress_uplift=_bounded(
            root["stress_uplift"], "stress_uplift", minimum=0.0, maximum=2.0
        ),
        stress_cap=_bounded(
            root["stress_cap"], "stress_cap", minimum=-1.0, maximum=1.0
        ),
        eigenvalue_floor=_bounded(
            root["eigenvalue_floor"],
            "eigenvalue_floor",
            minimum=0.0,
            maximum=0.01,
            lower_inclusive=False,
        ),
        priors=priors,
        implementation_mappings=mappings,
        stress_components=components,
        expected_correlation_one_loss=_bounded(
            correlation_one["expected_loss"],
            "correlation_one.expected_loss",
            minimum=0.0,
            maximum=1.0,
        ),
        correlation_one_tolerance=_bounded(
            correlation_one["tolerance"],
            "correlation_one.tolerance",
            minimum=0.0,
            maximum=0.1,
        ),
        evidence_mode_series=evidence_mode_series,
    )


def _source_path(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / PurePosixPath(relative_path)).resolve()
    if not path.is_relative_to(resolved_root):
        raise CorrelationError("source path escapes source root")
    if path.is_symlink() or not path.is_file():
        raise CorrelationError("source path must be a regular local file")
    return path


def _verify_source(root: Path, definition: SourceDefinition) -> Path:
    path = _source_path(root, definition.relative_path)
    digest, size = hash_file(path)
    if digest != definition.expected_sha256 or size != definition.expected_size_bytes:
        raise CorrelationError(
            "return source hash or size differs from the specification"
        )
    return path


def _read_returns(
    path: Path, as_of_date: date, schema_version: int = 1
) -> dict[str, list[ReturnRow]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_headers = RETURN_HEADERS if schema_version == 1 else RETURN_HEADERS_V2
        if tuple(reader.fieldnames or ()) != expected_headers:
            raise CorrelationError("return CSV headers differ from the closed contract")
        grouped: dict[str, list[ReturnRow]] = {}
        layers: dict[str, str] = {}
        seen: set[tuple[str, date, str]] = set()
        for line_number, row in enumerate(reader, start=2):
            series_id = _text(row["series_id"], f"return line {line_number} series_id")
            day = _day(row["date"], f"return line {line_number} date")
            if day > as_of_date:
                raise CorrelationError("return observation follows as_of_date")
            value_name = f"return line {line_number} return"
            value = _finite(row["return"], value_name)
            canonical_value = _canonical_decimal(row["return"], value_name)
            layer = _text(
                row["evidence_layer"], f"return line {line_number} evidence_layer"
            )
            if layer not in EVIDENCE_LAYERS:
                raise CorrelationError("return evidence layer is unsupported")
            if (
                schema_version == 1
                and series_id in layers
                and layers[series_id] != layer
            ):
                raise CorrelationError("one series cannot mix evidence layers")
            layers[series_id] = layer
            strategy_version = (
                None
                if schema_version == 1
                else _text(
                    row["strategy_version"],
                    f"return line {line_number} strategy_version",
                )
            )
            key = (series_id, day, layer)
            if key in seen:
                raise CorrelationError(
                    "return series contains a duplicate date within an evidence layer"
                )
            seen.add(key)
            grouped.setdefault(series_id, []).append(
                ReturnRow(day, value, layer, strategy_version, canonical_value)
            )
    if not grouped:
        raise CorrelationError("return source contains no observations")
    for rows in grouped.values():
        rows.sort(key=lambda item: item.day)
    return grouped


def _mode_composition(
    *,
    definition: EvidenceModeSeries,
    mode: str,
    status: str,
    live_rows: Sequence[ReturnRow],
    backtest_rows_used: Sequence[ReturnRow],
    backtest_rows_excluded: int,
    ignored_version_observations: int,
) -> dict[str, object]:
    combined = sorted([*live_rows, *backtest_rows_used], key=lambda item: item.day)
    return {
        "backtest_evidence_layer": "BACKTEST",
        "backtest_observations_excluded_at_or_after_live_inception": (
            backtest_rows_excluded
        ),
        "backtest_observations_used": len(backtest_rows_used),
        "evidence_mode": mode,
        "ignored_other_version_observations": ignored_version_observations,
        "canonical_live_series_sha256": _canonical_series_sha256(live_rows)
        if live_rows
        else None,
        "live_evidence_layer": definition.live_evidence_layer,
        "live_inception": live_rows[0].day.isoformat() if live_rows else None,
        "live_observations": len(live_rows),
        "observed_end": combined[-1].day.isoformat() if combined else None,
        "observed_start": combined[0].day.isoformat() if combined else None,
        "series_id": definition.series_id,
        "status": status,
        "strategy_version": definition.strategy_version,
    }


def _evidence_mode_returns(
    spec: Specification,
    raw_returns: Mapping[str, list[ReturnRow]],
) -> tuple[
    dict[str, dict[str, list[ReturnRow]]],
    dict[str, dict[str, dict[str, object]]],
]:
    modes: dict[str, dict[str, list[ReturnRow]]] = {
        "LIVE_ONLY": {},
        "LIVE_PLUS_BACKTEST": {},
    }
    compositions: dict[str, dict[str, dict[str, object]]] = {
        "LIVE_ONLY": {},
        "LIVE_PLUS_BACKTEST": {},
    }
    for definition in spec.evidence_mode_series:
        raw = raw_returns.get(definition.series_id, [])
        version_rows = [
            item for item in raw if item.strategy_version == definition.strategy_version
        ]
        ignored_versions = len(raw) - len(version_rows)
        live = sorted(
            (
                item
                for item in version_rows
                if item.evidence_layer == definition.live_evidence_layer
            ),
            key=lambda item: item.day,
        )
        if not live:
            unavailable = _mode_composition(
                definition=definition,
                mode="LIVE_ONLY",
                status="UNAVAILABLE_MISSING_VERSION_MATCHED_LIVE",
                live_rows=(),
                backtest_rows_used=(),
                backtest_rows_excluded=0,
                ignored_version_observations=ignored_versions,
            )
            compositions["LIVE_ONLY"][definition.series_id] = unavailable
            compositions["LIVE_PLUS_BACKTEST"][definition.series_id] = {
                **unavailable,
                "evidence_mode": "LIVE_PLUS_BACKTEST",
            }
            continue
        live_derived = [
            ReturnRow(
                item.day,
                item.value,
                "LIVE_ONLY",
                item.strategy_version,
                item.canonical_value,
            )
            for item in live
        ]
        modes["LIVE_ONLY"][definition.series_id] = live_derived
        compositions["LIVE_ONLY"][definition.series_id] = _mode_composition(
            definition=definition,
            mode="LIVE_ONLY",
            status="AVAILABLE_LIVE_ONLY",
            live_rows=live,
            backtest_rows_used=(),
            backtest_rows_excluded=0,
            ignored_version_observations=ignored_versions,
        )

        first_live = live[0].day
        backtest = sorted(
            (item for item in version_rows if item.evidence_layer == "BACKTEST"),
            key=lambda item: item.day,
        )
        eligible_backtest = [item for item in backtest if item.day < first_live]
        excluded_backtest = len(backtest) - len(eligible_backtest)
        if not eligible_backtest:
            modes["LIVE_PLUS_BACKTEST"][definition.series_id] = [
                ReturnRow(
                    item.day,
                    item.value,
                    "LIVE_PLUS_OPTIONAL_BACKTEST",
                    item.strategy_version,
                    item.canonical_value,
                )
                for item in live
            ]
            compositions["LIVE_PLUS_BACKTEST"][definition.series_id] = (
                _mode_composition(
                    definition=definition,
                    mode="LIVE_PLUS_BACKTEST",
                    status="AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY",
                    live_rows=live,
                    backtest_rows_used=(),
                    backtest_rows_excluded=excluded_backtest,
                    ignored_version_observations=ignored_versions,
                )
            )
            continue
        combined = [
            *(
                ReturnRow(
                    item.day,
                    item.value,
                    "LIVE_PLUS_OPTIONAL_BACKTEST",
                    item.strategy_version,
                    item.canonical_value,
                )
                for item in eligible_backtest
            ),
            *(
                ReturnRow(
                    item.day,
                    item.value,
                    "LIVE_PLUS_OPTIONAL_BACKTEST",
                    item.strategy_version,
                    item.canonical_value,
                )
                for item in live
            ),
        ]
        modes["LIVE_PLUS_BACKTEST"][definition.series_id] = combined
        compositions["LIVE_PLUS_BACKTEST"][definition.series_id] = _mode_composition(
            definition=definition,
            mode="LIVE_PLUS_BACKTEST",
            status="AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE",
            live_rows=live,
            backtest_rows_used=eligible_backtest,
            backtest_rows_excluded=excluded_backtest,
            ignored_version_observations=ignored_versions,
        )
    return modes, compositions


def _number(value: float | np.floating[Any] | None) -> str | None:
    if value is None:
        return None
    accepted = float(value)
    if not math.isfinite(accepted):
        raise CorrelationError("dependence result is non-finite")
    if abs(accepted) < 5e-16:
        accepted = 0.0
    return format(accepted, ".12g")


def _pearson(left: FloatArray, right: FloatArray) -> float | None:
    if len(left) < 2 or float(np.std(left)) <= 1e-15 or float(np.std(right)) <= 1e-15:
        return None
    return float(np.corrcoef(left, right)[0, 1])


def _ranks(values: FloatArray) -> FloatArray:
    order = np.argsort(values, kind="mergesort")
    result = np.empty(len(values), dtype=np.float64)
    cursor = 0
    while cursor < len(values):
        end = cursor + 1
        while end < len(values) and values[order[end]] == values[order[cursor]]:
            end += 1
        average = (cursor + 1 + end) / 2.0
        result[order[cursor:end]] = average
        cursor = end
    return result


def _spearman(left: FloatArray, right: FloatArray) -> float | None:
    return _pearson(_ranks(left), _ranks(right))


def _quantile(values: FloatArray, probability: float) -> float:
    return float(np.quantile(values, probability, method="linear"))


def _rolling(
    left: FloatArray, right: FloatArray, spec: Specification
) -> dict[str, object]:
    if len(left) < spec.rolling_window:
        return {
            "available_windows": 0,
            "latest": None,
            "maximum": None,
            "mean": None,
            "minimum": None,
            "status": "UNAVAILABLE_INSUFFICIENT_HISTORY",
            "window_observations": spec.rolling_window,
        }
    values = [
        value
        for start in range(0, len(left) - spec.rolling_window + 1)
        if (
            value := _pearson(
                left[start : start + spec.rolling_window],
                right[start : start + spec.rolling_window],
            )
        )
        is not None
    ]
    if len(values) < spec.minimum_rolling_windows:
        return {
            "available_windows": len(values),
            "latest": None,
            "maximum": None,
            "mean": None,
            "minimum": None,
            "status": "UNAVAILABLE_INSUFFICIENT_VALID_WINDOWS",
            "window_observations": spec.rolling_window,
        }
    return {
        "available_windows": len(values),
        "latest": _number(values[-1]),
        "maximum": _number(max(values)),
        "mean": _number(float(np.mean(values))),
        "minimum": _number(min(values)),
        "status": "AVAILABLE",
        "window_observations": spec.rolling_window,
    }


def _horizon_results(
    dates: Sequence[date], left: FloatArray, right: FloatArray
) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    for horizon in TRADING_OBSERVATION_HORIZONS:
        if len(dates) < horizon:
            results[str(horizon)] = {
                "correlation": None,
                "observations": len(dates),
                "observed_end": dates[-1].isoformat() if dates else None,
                "observed_start": dates[0].isoformat() if dates else None,
                "status": "UNAVAILABLE_INSUFFICIENT_HISTORY",
                "window_observations": horizon,
            }
            continue
        window_left = left[-horizon:]
        window_right = right[-horizon:]
        correlation = _pearson(window_left, window_right)
        results[str(horizon)] = {
            "correlation": _number(correlation),
            "observations": horizon,
            "observed_end": dates[-1].isoformat(),
            "observed_start": dates[-horizon].isoformat(),
            "status": (
                "AVAILABLE"
                if correlation is not None
                else "UNAVAILABLE_CONSTANT_SERIES"
            ),
            "window_observations": horizon,
        }
    return results


def _underwater_dates(rows: Sequence[ReturnRow]) -> set[date]:
    wealth = 1.0
    high_water_mark = 1.0
    underwater: set[date] = set()
    for row in rows:
        wealth *= 1.0 + row.value
        if wealth > high_water_mark:
            high_water_mark = wealth
        elif wealth < high_water_mark - 1e-14:
            underwater.add(row.day)
    return underwater


def _conditioned_correlation(
    dates: Sequence[date],
    left: FloatArray,
    right: FloatArray,
    mask: npt.NDArray[np.bool_],
    minimum_observations: int,
    definition: str,
) -> dict[str, object]:
    conditioned_dates = [
        day for day, accepted in zip(dates, mask, strict=True) if accepted
    ]
    conditioned_left = left[mask]
    conditioned_right = right[mask]
    correlation = (
        None
        if len(conditioned_dates) < minimum_observations
        else _pearson(conditioned_left, conditioned_right)
    )
    return {
        "conditioning_rule": definition,
        "correlation": _number(correlation),
        "observations": len(conditioned_dates),
        "observed_end": conditioned_dates[-1].isoformat()
        if conditioned_dates
        else None,
        "observed_start": conditioned_dates[0].isoformat()
        if conditioned_dates
        else None,
        "status": (
            "AVAILABLE"
            if correlation is not None
            else "UNAVAILABLE_INSUFFICIENT_OR_CONSTANT_DRAWDOWN"
        ),
    }


def _unavailable_cell(
    left: str,
    right: str,
    status: str,
    left_rows: Sequence[ReturnRow] | None,
    right_rows: Sequence[ReturnRow] | None,
) -> dict[str, object]:
    return {
        "coverage": {
            "common_observations": 0,
            "frequency": "DAILY_EXACT_DATE",
            "left_observations": len(left_rows or ()),
            "missing_from_left": 0,
            "missing_from_right": 0,
            "observed_end": None,
            "observed_start": None,
            "right_observations": len(right_rows or ()),
        },
        "downside": {
            "correlation": None,
            "observations": 0,
            "status": "UNAVAILABLE",
        },
        "joint_tail": {
            "joint_days": 0,
            "left_threshold": None,
            "multiple_vs_independence": None,
            "right_threshold": None,
            "status": "UNAVAILABLE",
        },
        "drawdown_regimes": {
            "both_strategies_underwater": {
                "conditioning_rule": "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
                "correlation": None,
                "observations": 0,
                "observed_end": None,
                "observed_start": None,
                "status": "UNAVAILABLE",
            },
            "either_strategy_underwater": {
                "conditioning_rule": "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
                "correlation": None,
                "observations": 0,
                "observed_end": None,
                "observed_start": None,
                "status": "UNAVAILABLE",
            },
        },
        "horizons": {
            str(horizon): {
                "correlation": None,
                "observations": 0,
                "observed_end": None,
                "observed_start": None,
                "status": "UNAVAILABLE",
                "window_observations": horizon,
            }
            for horizon in TRADING_OBSERVATION_HORIZONS
        },
        "left": left,
        "pearson": None,
        "right": right,
        "rolling_pearson": {
            "available_windows": 0,
            "latest": None,
            "maximum": None,
            "mean": None,
            "minimum": None,
            "status": "UNAVAILABLE",
            "window_observations": None,
        },
        "spearman": None,
        "status": status,
    }


def _observed_cell(
    left: str,
    right: str,
    left_rows: Sequence[ReturnRow] | None,
    right_rows: Sequence[ReturnRow] | None,
    spec: Specification,
) -> dict[str, object]:
    if not left_rows or not right_rows:
        return _unavailable_cell(
            left, right, "UNAVAILABLE_MISSING_SERIES", left_rows, right_rows
        )
    if left_rows[0].evidence_layer != right_rows[0].evidence_layer:
        return _unavailable_cell(
            left,
            right,
            "UNAVAILABLE_INCOMPATIBLE_EVIDENCE_LAYERS",
            left_rows,
            right_rows,
        )
    left_by_day = {item.day: item.value for item in left_rows}
    right_by_day = {item.day: item.value for item in right_rows}
    dates = sorted(
        day for day in set(left_by_day) & set(right_by_day) if day.weekday() < 5
    )
    coverage = {
        "common_observations": len(dates),
        "evidence_layer": left_rows[0].evidence_layer,
        "frequency": "WEEKDAY_OBSERVED_EXACT_DATE",
        "left_observations": len(left_rows),
        "missing_from_left": len(set(right_by_day) - set(left_by_day)),
        "missing_from_right": len(set(left_by_day) - set(right_by_day)),
        "observed_end": dates[-1].isoformat() if dates else None,
        "observed_start": dates[0].isoformat() if dates else None,
        "right_observations": len(right_rows),
    }
    if len(dates) < spec.minimum_observations:
        result = _unavailable_cell(
            left,
            right,
            "UNAVAILABLE_INSUFFICIENT_COMMON_HISTORY",
            left_rows,
            right_rows,
        )
        result["coverage"] = coverage
        return result
    left_values = np.asarray([left_by_day[day] for day in dates], dtype=np.float64)
    right_values = np.asarray([right_by_day[day] for day in dates], dtype=np.float64)
    pearson = _pearson(left_values, right_values)
    spearman = _spearman(left_values, right_values)
    if pearson is None:
        status = "UNAVAILABLE_CONSTANT_SERIES"
    else:
        status = "AVAILABLE"
    downside_mask = (left_values < 0.0) | (right_values < 0.0)
    downside_left = left_values[downside_mask]
    downside_right = right_values[downside_mask]
    downside_corr = (
        None
        if len(downside_left) < spec.minimum_downside_observations
        else _pearson(downside_left, downside_right)
    )
    left_threshold = _quantile(left_values, spec.tail_quantile)
    right_threshold = _quantile(right_values, spec.tail_quantile)
    joint_days = int(
        np.sum((left_values <= left_threshold) & (right_values <= right_threshold))
    )
    expected_joint = len(dates) * spec.tail_quantile**2
    left_underwater = _underwater_dates(left_rows)
    right_underwater = _underwater_dates(right_rows)
    left_drawdown = np.asarray(
        [day in left_underwater for day in dates], dtype=np.bool_
    )
    right_drawdown = np.asarray(
        [day in right_underwater for day in dates], dtype=np.bool_
    )
    return {
        "coverage": coverage,
        "downside": {
            "correlation": _number(downside_corr),
            "observations": len(downside_left),
            "status": (
                "AVAILABLE"
                if downside_corr is not None
                else "UNAVAILABLE_INSUFFICIENT_OR_CONSTANT_DOWNSIDE"
            ),
        },
        "joint_tail": {
            "joint_days": joint_days,
            "left_threshold": _number(left_threshold),
            "multiple_vs_independence": _number(
                None if expected_joint <= 0.0 else joint_days / expected_joint
            ),
            "right_threshold": _number(right_threshold),
            "status": "AVAILABLE",
        },
        "drawdown_regimes": {
            "both_strategies_underwater": _conditioned_correlation(
                dates,
                left_values,
                right_values,
                left_drawdown & right_drawdown,
                spec.minimum_downside_observations,
                "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM",
            ),
            "either_strategy_underwater": _conditioned_correlation(
                dates,
                left_values,
                right_values,
                left_drawdown | right_drawdown,
                spec.minimum_downside_observations,
                "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM",
            ),
        },
        "horizons": _horizon_results(dates, left_values, right_values),
        "left": left,
        "pearson": _number(pearson),
        "right": right,
        "rolling_pearson": _rolling(left_values, right_values, spec),
        "spearman": _number(spearman),
        "status": status,
    }


def _nearest_correlation(matrix: FloatArray, floor: float) -> FloatArray:
    symmetric = (matrix + matrix.T) / 2.0
    values, vectors = np.linalg.eigh(symmetric)
    clipped = np.maximum(values, floor)
    positive = vectors @ np.diag(clipped) @ vectors.T
    scale = np.sqrt(np.diag(positive))
    correlation = positive / np.outer(scale, scale)
    correlation = np.clip(correlation, -1.0, 1.0)
    np.fill_diagonal(correlation, 1.0)
    return cast(FloatArray, correlation.astype(np.float64))


def _matrix(values: FloatArray) -> list[list[str]]:
    return [[cast(str, _number(item)) for item in row] for row in values]


def _matrix_summary(
    pre_projection: FloatArray, projected: FloatArray
) -> dict[str, object]:
    return {
        "maximum_absolute_adjustment": _number(
            float(np.max(np.abs(projected - pre_projection)))
        ),
        "minimum_eigenvalue_post_projection": _number(
            float(np.min(np.linalg.eigvalsh(projected)))
        ),
        "minimum_eigenvalue_pre_projection": _number(
            float(np.min(np.linalg.eigvalsh(pre_projection)))
        ),
    }


def _implementation_cells(
    spec: Specification,
) -> list[dict[str, object]]:
    mappings = {item.series_id: item for item in spec.implementation_mappings}
    results: list[dict[str, object]] = []
    for index, left in enumerate(spec.series_ids):
        for right in spec.series_ids[index + 1 :]:
            left_map = mappings.get(left)
            right_map = mappings.get(right)
            if left_map is None or right_map is None:
                results.append(
                    {
                        "left": left,
                        "right": right,
                        "shared_instruments": [],
                        "shared_providers": [],
                        "shared_venues": [],
                        "status": "UNAVAILABLE_MAPPING_MISSING",
                    }
                )
                continue
            results.append(
                {
                    "left": left,
                    "right": right,
                    "shared_instruments": sorted(
                        set(left_map.instruments) & set(right_map.instruments)
                    ),
                    "shared_providers": sorted(
                        set(left_map.providers) & set(right_map.providers)
                    ),
                    "shared_venues": sorted(
                        set(left_map.venues) & set(right_map.venues)
                    ),
                    "status": "AVAILABLE",
                }
            )
    return results


def _dependence_payload(
    spec: Specification,
    returns: Mapping[str, list[ReturnRow]],
    *,
    evidence_mode: str | None = None,
    series_composition: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    observed_cells: list[dict[str, object]] = []
    cell_index: dict[tuple[str, str], dict[str, object]] = {}
    for index, left in enumerate(spec.series_ids):
        for right in spec.series_ids[index + 1 :]:
            cell = _observed_cell(
                left, right, returns.get(left), returns.get(right), spec
            )
            if evidence_mode is not None and series_composition is not None:
                cell["evidence_mode"] = evidence_mode
                cell["left_evidence_composition"] = dict(
                    series_composition.get(left, {})
                )
                cell["right_evidence_composition"] = dict(
                    series_composition.get(right, {})
                )
            observed_cells.append(cell)
            cell_index[(left, right)] = cell

    size = len(spec.series_ids)
    prior_matrix = np.eye(size, dtype=np.float64)
    observed_matrix: list[list[str | None]] = [
        ["1" if row == column else None for column in range(size)]
        for row in range(size)
    ]
    spearman_matrix: list[list[str | None]] = [
        ["1" if row == column else None for column in range(size)]
        for row in range(size)
    ]
    horizon_matrices: dict[str, list[list[str | None]]] = {
        str(horizon): [
            ["1" if row == column else None for column in range(size)]
            for row in range(size)
        ]
        for horizon in TRADING_OBSERVATION_HORIZONS
    }
    drawdown_matrices: dict[str, list[list[str | None]]] = {
        regime: [
            ["1" if row == column else None for column in range(size)]
            for row in range(size)
        ]
        for regime in (
            "either_strategy_underwater",
            "both_strategies_underwater",
        )
    }
    prior_index = {(item.left, item.right): item for item in spec.priors}
    posterior_pre = np.eye(size, dtype=np.float64)
    stress_pre = np.eye(size, dtype=np.float64)
    decision_matrix = np.eye(size, dtype=np.float64)
    blend_cells: list[dict[str, object]] = []
    for left_index, left in enumerate(spec.series_ids):
        for right_index in range(left_index + 1, size):
            right = spec.series_ids[right_index]
            key = tuple(sorted((left, right)))
            prior = prior_index[cast(tuple[str, str], key)]
            prior_matrix[left_index, right_index] = prior.value
            prior_matrix[right_index, left_index] = prior.value
            cell = cell_index[(left, right)]
            observed_text = cell["pearson"]
            spearman_text = cell["spearman"]
            observed = (
                None if observed_text is None else float(cast(str, observed_text))
            )
            observed_matrix[left_index][right_index] = cast(str | None, observed_text)
            observed_matrix[right_index][left_index] = cast(str | None, observed_text)
            spearman_matrix[left_index][right_index] = cast(str | None, spearman_text)
            spearman_matrix[right_index][left_index] = cast(str | None, spearman_text)
            for horizon, horizon_result in cast(
                dict[str, dict[str, object]], cell["horizons"]
            ).items():
                correlation_text = cast(str | None, horizon_result["correlation"])
                horizon_matrices[horizon][left_index][right_index] = correlation_text
                horizon_matrices[horizon][right_index][left_index] = correlation_text
            for regime, regime_result in cast(
                dict[str, dict[str, object]], cell["drawdown_regimes"]
            ).items():
                correlation_text = cast(str | None, regime_result["correlation"])
                drawdown_matrices[regime][left_index][right_index] = correlation_text
                drawdown_matrices[regime][right_index][left_index] = correlation_text
            common = cast(dict[str, Any], cell["coverage"])["common_observations"]
            weight = (
                0.0
                if observed is None
                else min(
                    spec.maximum_observed_weight,
                    int(common) / spec.full_observed_weight_observations,
                )
            )
            posterior_value = (
                prior.value
                if observed is None
                else weight * observed + (1.0 - weight) * prior.value
            )
            stress_base = (
                prior.value if observed is None else max(prior.value, observed)
            )
            stress_value = min(spec.stress_cap, stress_base + spec.stress_uplift)
            posterior_pre[left_index, right_index] = posterior_value
            posterior_pre[right_index, left_index] = posterior_value
            stress_pre[left_index, right_index] = stress_value
            stress_pre[right_index, left_index] = stress_value
            decision_value = 1.0 if observed is None else posterior_value
            decision_matrix[left_index, right_index] = decision_value
            decision_matrix[right_index, left_index] = decision_value
            blend_cells.append(
                {
                    "conceptual_prior": _number(prior.value),
                    "conceptual_prior_note": prior.evidence_note,
                    "decision_correlation": _number(decision_value),
                    "decision_status": (
                        "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION"
                        if observed is None
                        else "OBSERVED_SUPPORTED_POSTERIOR"
                    ),
                    "left": left,
                    "observations": common,
                    "observed_correlation": _number(observed),
                    "observed_weight": _number(weight),
                    "posterior_pre_psd": _number(posterior_value),
                    "right": right,
                    "stress_pre_psd": _number(stress_value),
                }
            )

    posterior = _nearest_correlation(posterior_pre, spec.eigenvalue_floor)
    stress = _nearest_correlation(stress_pre, spec.eigenvalue_floor)
    loss = sum(
        item.allocation_weight * item.adverse_loss_fraction
        for item in spec.stress_components
    )
    difference = loss - spec.expected_correlation_one_loss
    if abs(difference) > spec.correlation_one_tolerance:
        raise CorrelationError(
            "correlation-one stress does not reconcile to expected loss"
        )
    stress_contributions = [
        {
            "adverse_loss_fraction": _number(item.adverse_loss_fraction),
            "allocation_weight": _number(item.allocation_weight),
            "loss_contribution": _number(
                item.allocation_weight * item.adverse_loss_fraction
            ),
            "series_id": item.series_id,
        }
        for item in spec.stress_components
    ]
    result: dict[str, object] = {
        "blend_cells": blend_cells,
        "correlation_one": {
            "components": stress_contributions,
            "expected_loss": _number(spec.expected_correlation_one_loss),
            "loss": _number(loss),
            "reconciliation_difference": _number(difference),
            "status": "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND",
        },
        "implementation_cells": _implementation_cells(spec),
        "drawdown_matrices": {
            regime: {
                "classification": (
                    "MEASURED_CONDITIONED_ON_PAIR_WEALTH_DRAWDOWN_STATE"
                ),
                "conditioning_rule": (
                    "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM"
                    if regime == "either_strategy_underwater"
                    else "BOTH_WEALTH_INDICES_BELOW_PRIOR_RUNNING_HWM"
                ),
                "values": values,
            }
            for regime, values in drawdown_matrices.items()
        },
        "horizon_matrices": {
            horizon: {
                "classification": "MEASURED_WEEKDAY_OBSERVATION_WINDOW",
                "values": values,
                "window_observations": int(horizon),
            }
            for horizon, values in horizon_matrices.items()
        },
        "matrices": {
            "conceptual_prior": {
                "classification": "CONCEPTUAL_NOT_MEASURED",
                "values": _matrix(prior_matrix),
            },
            "decision_no_missing_diversification_credit": {
                "classification": "CONSERVATIVE_DECISION_ONLY_NOT_PSD_COVARIANCE_INPUT",
                "values": _matrix(decision_matrix),
            },
            "observed_pearson": {
                "classification": "MEASURED_COMPATIBLE_EXACT_DATE_ONLY",
                "values": observed_matrix,
            },
            "observed_spearman": {
                "classification": "MEASURED_ROBUST_RANK_CHECK",
                "values": spearman_matrix,
            },
            "posterior": {
                "classification": "CONCEPTUAL_OBSERVED_BLEND_PSD_PROJECTED",
                "projection": _matrix_summary(posterior_pre, posterior),
                "values": _matrix(posterior),
            },
            "stress": {
                "classification": "STRESS_ASSUMPTION_PSD_PROJECTED",
                "projection": _matrix_summary(stress_pre, stress),
                "values": _matrix(stress),
            },
        },
        "observed_cells": observed_cells,
        "series_ids": list(spec.series_ids),
    }
    if evidence_mode is not None and series_composition is not None:
        result["evidence_mode"] = evidence_mode
        result["series_composition"] = {
            series_id: dict(series_composition.get(series_id, {}))
            for series_id in spec.series_ids
        }
    return result


def build_correlation(
    spec_path: Path, source_root: Path
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    spec = _load_spec(spec_path)
    source_path = _verify_source(source_root, spec.source)
    returns = _read_returns(source_path, spec.as_of_date, spec.schema_version)
    dependence_modes: dict[str, dict[str, object]] | None = None
    default_dependence: dict[str, object]
    if spec.schema_version == 2:
        mode_returns, compositions = _evidence_mode_returns(spec, returns)
        dependence_modes = {
            mode: _dependence_payload(
                spec,
                mode_returns[mode],
                evidence_mode=mode,
                series_composition=compositions[mode],
            )
            for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
        }
        default_dependence = dependence_modes["LIVE_ONLY"]
    else:
        default_dependence = _dependence_payload(spec, returns)
    payload: dict[str, object] = {
        "analytics_schema_version": spec.schema_version,
        "as_of_date": spec.as_of_date.isoformat(),
        "builder_version": BUILDER_VERSION,
        "dependence": default_dependence,
        "policy": {
            "evidence_layer_rule": (
                "EXPLICIT_MODE_DERIVATION_NO_POST_INCEPTION_BACKTEST_FILL"
                if spec.schema_version == 2
                else "PAIRWISE_SAME_LAYER_ONLY"
            ),
            "frequency": "WEEKDAY_OBSERVED_EXACT_DATE_NO_HOLIDAY_INFERENCE",
            "requested_horizons": list(TRADING_OBSERVATION_HORIZONS),
            "minimum_downside_observations": spec.minimum_downside_observations,
            "minimum_observations": spec.minimum_observations,
            "missing_diversification_credit": "ZERO",
            "rolling_window": spec.rolling_window,
            "tail_quantile": _number(spec.tail_quantile),
        },
        "source": {
            "artifact_id": spec.source.artifact_id,
            "sha256": spec.source.expected_sha256,
            "size_bytes": spec.source.expected_size_bytes,
        },
    }
    if dependence_modes is not None:
        payload["default_evidence_mode"] = "LIVE_ONLY"
        payload["dependence_modes"] = dependence_modes
        cast(dict[str, object], payload["policy"])["evidence_modes"] = [
            "LIVE_ONLY",
            "LIVE_PLUS_BACKTEST",
        ]
        cast(dict[str, object], payload["policy"])["live_authority"] = (
            "LIVE_WINS_ON_AND_AFTER_FIRST_LIVE_OBSERVATION"
        )
    analytics_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    result: dict[str, object] = {"analytics_hash": analytics_hash, **payload}
    result_bytes = canonical_json_bytes(result)
    observed_rows = [
        row for series_id in spec.series_ids for row in returns.get(series_id, [])
    ]
    manifest = GenerationManifest.build(
        as_of_date=spec.as_of_date,
        adapter=AdapterIdentity("portfolio-correlation", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "All requested pair cells and separately labeled conceptual, posterior, "
                "stress, implementation, and correlation-one views."
            ),
            fact_types=(
                "conceptual-correlation",
                "correlation-one-stress",
                "implementation-coincidence",
                "observed-correlation",
                "posterior-correlation",
                "rank-dependence",
                "rolling-correlation",
                "stress-correlation",
            ),
            observed_start=min(item.day for item in observed_rows),
            observed_end=max(item.day for item in observed_rows),
        ),
        artifacts=(
            SourceArtifact.from_path(
                spec_path,
                artifact_id="spec/correlation-spec.json",
                as_of_date=spec.as_of_date,
                schema_name="portfolio-correlation-spec",
                schema_version=str(spec.schema_version),
            ),
            SourceArtifact(
                artifact_id=spec.source.artifact_id,
                sha256=spec.source.expected_sha256,
                size_bytes=spec.source.expected_size_bytes,
                as_of_date=spec.as_of_date,
                schema_name=spec.source.schema_name,
                schema_version=spec.source.schema_version,
            ),
        ),
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
        raise CorrelationError("candidate must be a regular directory")
    expected = {
        "correlation.json": result_bytes,
        "generation.json": manifest.to_json_bytes(),
    }
    if {item.name for item in candidate.iterdir()} != set(expected):
        raise CorrelationError("candidate file set differs from the contract")
    for name, content in expected.items():
        path = candidate / name
        if path.is_symlink() or path.read_bytes() != content:
            raise CorrelationError(f"candidate {name} differs from the exact build")


def stage_candidate(
    spec_path: Path, source_root: Path, output_root: Path
) -> dict[str, object]:
    result, manifest, result_bytes = build_correlation(spec_path, source_root)
    if output_root.exists() and output_root.is_symlink():
        raise CorrelationError("output root must not be a symlink")
    candidates = output_root.resolve() / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    candidate = candidates / manifest.generation_id
    if candidate.exists():
        _verify_candidate(candidate, manifest, result_bytes)
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=candidates))
        try:
            _write_new(temporary / "correlation.json", result_bytes)
            _write_new(temporary / "generation.json", manifest.to_json_bytes())
            rename_directory_with_retry(temporary, candidate)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    dependence = cast(dict[str, Any], result["dependence"])
    return {
        "analytics_hash": result["analytics_hash"],
        "candidate": str(candidate),
        "generation_id": manifest.generation_id,
        "pair_count": len(cast(list[object], dependence["observed_cells"])),
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
            _write_new(temporary / "correlation.json", result_bytes)
            _write_new(temporary / "generation.json", manifest.to_json_bytes())
            rename_directory_with_retry(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return publish_generation(output_root, manifest.generation_id)


def publish(spec_path: Path, source_root: Path, output_root: Path) -> dict[str, object]:
    result, manifest, result_bytes = build_correlation(spec_path, source_root)
    candidate = output_root.resolve() / "candidates" / manifest.generation_id
    pointer = _publish_candidate(candidate, output_root, manifest, result_bytes)
    dependence = cast(dict[str, Any], result["dependence"])
    return {
        "analytics_hash": result["analytics_hash"],
        "generation_id": manifest.generation_id,
        "pair_count": len(cast(list[object], dependence["observed_cells"])),
        "publication_state": pointer.state.value,
    }


def _summary(spec_path: Path, source_root: Path) -> dict[str, object]:
    result, manifest, _content = build_correlation(spec_path, source_root)
    dependence = cast(dict[str, Any], result["dependence"])
    cells = cast(list[dict[str, object]], dependence["observed_cells"])
    return {
        "analytics_hash": result["analytics_hash"],
        "available_pair_count": sum(item["status"] == "AVAILABLE" for item in cells),
        "generation_id": manifest.generation_id,
        "pair_count": len(cells),
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

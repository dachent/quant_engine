"""Build and publish compact governed weekly-return analytics."""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import Any, Final, Iterable, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import decimal_text


BUILDER_VERSION: Final = "0.3.0"
SCHEMA_VERSION: Final = 2
STRATEGY_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
PUBLIC_SOURCES: Final = {
    "us-economic": (
        "inputs/lenses/us-economic-factors.csv",
        "ECONOMIC_FACTOR_US",
        "LONG_SHORT_OR_EXCESS_FACTOR_RETURN",
        {"date", "rf"},
    ),
    "europe-economic": (
        "inputs/lenses/europe-economic-factors.csv",
        "ECONOMIC_FACTOR_EUROPE",
        "LONG_SHORT_OR_EXCESS_FACTOR_RETURN",
        {"date", "rf"},
    ),
    "us-equity-style": (
        "inputs/lenses/us-equity-style-factors.csv",
        "STYLE_US_EQUITY",
        "TOTAL_RETURN_ETF_PROXY",
        {"date", "BIL"},
    ),
    "multi-asset-style": (
        "inputs/lenses/multi-asset-style-factors.csv",
        "STYLE_MULTI_ASSET",
        "TOTAL_RETURN_ETF_PROXY",
        {"date"},
    ),
    "us-sector": (
        "inputs/lenses/us-sector-factors.csv",
        "SECTOR",
        "TOTAL_RETURN_ETF_PROXY",
        {"date", "BIL"},
    ),
    "us-industry": (
        "inputs/lenses/us-industry-factors.csv",
        "INDUSTRY",
        "TOTAL_RETURN_INDUSTRY_PORTFOLIO",
        {"date", "rf"},
    ),
}
CORE_BENCHMARKS: Final = {
    "BIL": "Cash / Treasury bills",
    "DBC": "Broad commodities",
    "GLD": "Gold",
    "HYG": "High-yield credit",
    "IEF": "Intermediate Treasuries",
    "IEFA": "Developed ex-U.S. equity",
    "IEMG": "Emerging-markets equity",
    "ITOT": "U.S. total equity",
    "UUP": "U.S. dollar",
    "SPY": "U.S. large-cap equity (S&P 500)",
    "SPXL": "U.S. large-cap equity, 3x daily leveraged",
}
# SPY and SPXL are the dollar-matched comparison benchmarks.  Their governed
# history reaches back before the cash proxy that bounds every factor lens, so the
# weekly and daily series are read from the unprojected market input of the plan
# bundle (the cash-proxy-gated lens copy would truncate them) and never from a lens.
FULL_HISTORY_BENCHMARKS: Final = ("SPY", "SPXL")
MARKET_BENCHMARK_SOURCE: Final = (
    "inputs/market/style_returns.csv",
    "STYLE_MULTI_ASSET",
    "TOTAL_RETURN_ETF_PROXY",
)
DAILY_BENCHMARK_SOURCE_ID: Final = "yahoo-adjusted-etf-daily"


class WeeklyReturnError(ValueError):
    """An input or output violates the weekly-return contract."""


def _filesystem_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if len(value) < 248:
        return path
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{value[2:]}")
    return Path(f"\\\\?\\{value}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    candidate = _filesystem_path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise WeeklyReturnError(f"{label} is missing or linked")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeeklyReturnError(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise WeeklyReturnError(f"{label} must be an object")
    return value


def _objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WeeklyReturnError(f"{label} must be an array")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise WeeklyReturnError(f"{label} contains a non-object")
        rows.append(item)
    return rows


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeeklyReturnError(f"{label} must be non-empty text")
    return value.strip()


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise WeeklyReturnError(f"{label} must be numeric")
    try:
        accepted = Decimal(str(value))
    except InvalidOperation as exc:
        raise WeeklyReturnError(f"{label} must be numeric") from exc
    if not accepted.is_finite() or accepted <= Decimal("-1"):
        raise WeeklyReturnError(f"{label} must be finite and greater than -1")
    return accepted


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _current_week(as_of: date) -> dict[str, Any]:
    start = _week_start(as_of)
    end = start + timedelta(days=6)
    return {
        "data_through_date": as_of.isoformat(),
        "default_visible": True,
        "is_partial": as_of < end,
        "week_end": end.isoformat(),
        "week_start": start.isoformat(),
    }


def _weekly_series(
    observations: Iterable[tuple[date, Decimal]], *, as_of: date
) -> tuple[dict[str, str], dict[str, list[Any]], str]:
    daily: dict[date, Decimal] = {}
    for observed_date, observed_return in observations:
        if observed_date > as_of:
            raise WeeklyReturnError("weekly observation is after correlation as_of")
        if observed_date in daily:
            raise WeeklyReturnError(f"duplicate daily observation: {observed_date}")
        daily[observed_date] = observed_return
    if not daily:
        return {}, {}, "UNAVAILABLE_NO_CALENDAR_EVIDENCE"
    calendar_basis = (
        "CALENDAR_7_DAY" if any(day.weekday() >= 5 for day in daily) else "WEEKDAY"
    )
    grouped: dict[date, list[tuple[date, Decimal]]] = {}
    for observed_date, observed_return in sorted(daily.items()):
        start = _week_start(observed_date)
        grouped.setdefault(start, []).append((observed_date, observed_return))
    first_observed = min(daily)
    last_observed = max(daily)
    values: dict[str, str] = {}
    metadata: dict[str, list[Any]] = {}
    for start, rows in sorted(grouped.items()):
        value = Decimal("1")
        for _, observed_return in rows:
            value *= Decimal("1") + observed_return
        actual_start = rows[0][0]
        actual_end = rows[-1][0]
        boundary_reasons: list[str] = []
        if actual_start == first_observed and actual_start > start:
            boundary_reasons.append("SOURCE_START")
        expected_end = start + timedelta(
            days=6 if calendar_basis == "CALENDAR_7_DAY" else 4
        )
        if (
            actual_end == last_observed
            and actual_end < min(expected_end, as_of)
            and start <= _week_start(as_of)
        ):
            boundary_reasons.append("SOURCE_END")
        if start == _week_start(as_of) and as_of < start + timedelta(days=6):
            boundary_reasons.append("CURRENT_AS_OF")
        status = (
            "PARTIAL_BOUNDARY"
            if boundary_reasons
            else "COMPLETE_WITHIN_ACCEPTED_CALENDAR"
        )
        key = start.isoformat()
        values[key] = _decimal_text(value - Decimal("1"))
        metadata[key] = [
            len(rows),
            actual_start.isoformat(),
            actual_end.isoformat(),
            status,
            boundary_reasons,
        ]
    return values, metadata, calendar_basis


def _weekly_values(observations: Iterable[tuple[date, Decimal]]) -> dict[str, str]:
    rows = list(observations)
    as_of = max((row[0] for row in rows), default=date.min)
    return _weekly_series(rows, as_of=as_of)[0]


def _canonical_series_sha256(observations: Iterable[tuple[date, Decimal]]) -> str:
    payload = [
        {"date": observed_date.isoformat(), "return": decimal_text(observed_return)}
        for observed_date, observed_return in sorted(observations)
    ]
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _coverage(values: Mapping[str, str]) -> dict[str, Any]:
    if not values:
        return {
            "first_week": None,
            "internal_missing_weeks": 0,
            "last_week": None,
            "status": "UNAVAILABLE_NO_WEEKLY_OBSERVATIONS",
            "weekly_observations": 0,
        }
    first = date.fromisoformat(min(values))
    last = date.fromisoformat(max(values))
    possible = ((last - first).days // 7) + 1
    return {
        "first_week": first.isoformat(),
        "internal_missing_weeks": possible - len(values),
        "last_week": last.isoformat(),
        "status": "AVAILABLE_SPARSE_MAP_ABSENT_IS_UNAVAILABLE",
        "weekly_observations": len(values),
    }


def _correlation_inputs(
    correlation_root: Path,
) -> tuple[dict[str, Any], GenerationManifest, Path, Path]:
    try:
        pointer = read_current(correlation_root)
        manifest = read_generation(correlation_root, pointer.generation_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise WeeklyReturnError("correlation pointer is not accepted") from exc
    generation = correlation_root / "generations" / pointer.generation_id
    correlation_path = generation / "correlation.json"
    manifest_path = generation / "generation.json"
    expected = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "normalized/correlation.json"
    ]
    if correlation_path.is_symlink() or not correlation_path.is_file():
        raise WeeklyReturnError("correlation artifact binding differs")
    actual_hash, actual_size = hash_file(correlation_path)
    if (
        len(expected) != 1
        or expected[0].sha256 != actual_hash
        or expected[0].size_bytes != actual_size
    ):
        raise WeeklyReturnError("correlation artifact binding differs")
    correlation = _load_json(correlation_path, "correlation analytics")
    if correlation.get("analytics_schema_version") != 2:
        raise WeeklyReturnError("layered correlation schema version 2 is required")
    if correlation.get("default_evidence_mode") != "LIVE_ONLY":
        raise WeeklyReturnError("weekly strategy authority must default to LIVE_ONLY")
    return correlation, manifest, correlation_path, manifest_path


def _correlation_cutoff(
    correlation: Mapping[str, Any], generation_manifest: GenerationManifest
) -> date:
    """Return the data cutoff the correlation generation covers.

    The generation manifest date is the date the correlation was produced, which is
    later than its data whenever the run executes after the cutoff.  The current week
    is anchored to the data, so the cutoff is the declared coverage end; the
    correlation's own ``complete_through``, when it carries one, must agree.
    """

    observed_end = generation_manifest.coverage.observed_end
    if observed_end is None:
        raise WeeklyReturnError("correlation coverage declares no observed end")
    if observed_end > generation_manifest.as_of_date:
        raise WeeklyReturnError("correlation coverage follows its generation date")
    declared = correlation.get("complete_through")
    if declared is not None:
        try:
            declared_day = date.fromisoformat(_text(declared, "complete_through"))
        except ValueError as exc:
            raise WeeklyReturnError("correlation complete_through is invalid") from exc
        if declared_day != observed_end:
            raise WeeklyReturnError("correlation cutoff differs from its coverage")
    return observed_end


def _strategy_rows(
    strategy_returns: Path,
    correlation: Mapping[str, Any],
    generation_manifest: GenerationManifest,
    as_of: date,
) -> list[dict[str, Any]]:
    raw_source = correlation.get("source")
    if not isinstance(raw_source, dict) or not all(
        isinstance(key, str) for key in raw_source
    ):
        raise WeeklyReturnError("correlation source must be an object")
    source = raw_source
    artifact_id = _text(source.get("artifact_id"), "correlation source artifact_id")
    expected = [
        item
        for item in generation_manifest.artifacts
        if item.artifact_id == artifact_id
    ]
    actual_hash, actual_size = hash_file(strategy_returns)
    if (
        len(expected) != 1
        or expected[0].sha256 != actual_hash
        or expected[0].size_bytes != actual_size
        or source.get("sha256") != actual_hash
        or source.get("size_bytes") != actual_size
    ):
        raise WeeklyReturnError("strategy return source hash differs from correlation")
    with strategy_returns.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != [
            "series_id",
            "date",
            "return",
            "evidence_layer",
            "strategy_version",
        ]:
            raise WeeklyReturnError("strategy return columns differ")
        raw = list(reader)

    dependence_modes = correlation.get("dependence_modes")
    if not isinstance(dependence_modes, dict) or set(dependence_modes) != set(
        STRATEGY_MODES
    ):
        raise WeeklyReturnError("strategy evidence modes differ")
    compositions: dict[str, dict[str, dict[str, Any]]] = {}
    for mode in STRATEGY_MODES:
        payload = dependence_modes[mode]
        if not isinstance(payload, dict):
            raise WeeklyReturnError(f"{mode} dependence payload differs")
        composition = payload.get("series_composition")
        if not isinstance(composition, dict):
            raise WeeklyReturnError(f"{mode} composition is missing")
        compositions[mode] = {
            _text(key, f"{mode} series_id"): value
            for key, value in composition.items()
            if isinstance(value, dict)
        }
    if set(compositions["LIVE_ONLY"]) != set(compositions["LIVE_PLUS_BACKTEST"]):
        raise WeeklyReturnError("strategy mode boundaries differ")
    if not compositions["LIVE_ONLY"]:
        raise WeeklyReturnError("weekly strategy boundary is empty")

    indexed: dict[tuple[str, str, str], list[tuple[date, Decimal]]] = {}
    for row in raw:
        series_id = _text(row.get("series_id"), "strategy series_id")
        evidence_layer = _text(row.get("evidence_layer"), "strategy evidence_layer")
        version = _text(row.get("strategy_version"), "strategy version")
        try:
            observed_date = date.fromisoformat(_text(row.get("date"), "strategy date"))
        except ValueError as exc:
            raise WeeklyReturnError("strategy return date is invalid") from exc
        key = (series_id, evidence_layer, version)
        indexed.setdefault(key, []).append(
            (observed_date, _decimal(row.get("return"), "strategy return"))
        )

    series_rows: list[dict[str, Any]] = []
    for series_id in sorted(compositions["LIVE_ONLY"]):
        values_by_mode: dict[str, dict[str, str]] = {}
        observation_meta_by_mode: dict[str, dict[str, list[Any]]] = {}
        calendar_by_mode: dict[str, str] = {}
        mode_coverage: dict[str, dict[str, Any]] = {}
        for mode in STRATEGY_MODES:
            item = compositions[mode][series_id]
            version = _text(item.get("strategy_version"), "strategy version")
            live_inception_raw = item.get("live_inception")
            live_inception = (
                date.fromisoformat(_text(live_inception_raw, "live inception"))
                if live_inception_raw is not None
                else None
            )
            live = indexed.get((series_id, "ACCOUNT_LIVE", version), [])
            if live_inception is not None:
                live = [row for row in live if row[0] >= live_inception]
            backtest = indexed.get((series_id, "BACKTEST", version), [])
            if live_inception is not None:
                backtest = [row for row in backtest if row[0] < live_inception]
            else:
                backtest = []
            selected = live if mode == "LIVE_ONLY" else [*backtest, *live]
            expected_live = item.get("live_observations")
            expected_backtest = item.get("backtest_observations_used")
            if expected_live != len(live):
                raise WeeklyReturnError(
                    f"live observation count differs for {series_id}: {mode}"
                )
            if mode == "LIVE_PLUS_BACKTEST" and expected_backtest != len(backtest):
                raise WeeklyReturnError(
                    f"backtest observation count differs for {series_id}"
                )
            values, observation_meta, calendar_basis = _weekly_series(
                selected, as_of=as_of
            )
            values_by_mode[mode] = values
            observation_meta_by_mode[mode] = observation_meta
            calendar_by_mode[mode] = calendar_basis
            mode_coverage[mode] = {
                **_coverage(values),
                "backtest_daily_observations": len(backtest)
                if mode != "LIVE_ONLY"
                else 0,
                "canonical_live_series_sha256": _canonical_series_sha256(live),
                "live_daily_observations": len(live),
                "strategy_version": version,
            }
        series_rows.append(
            {
                "absent_value_status": "UNAVAILABLE_NO_OBSERVATION_IN_WEEK",
                "calendar_basis_by_mode": calendar_by_mode,
                "display_groups": ["STRATEGY"],
                "display_name": series_id,
                "mode_coverage": mode_coverage,
                "observation_meta_by_mode": observation_meta_by_mode,
                "primary_group": "STRATEGY",
                "return_basis": "STRATEGY_TOTAL_RETURN",
                "series_id": f"strategy:{series_id}",
                "source_key": "accepted-layered-strategy-returns",
                "values_by_mode": values_by_mode,
            }
        )
    return series_rows


def _manifest_hashes(plan_bundle: Path) -> dict[str, str]:
    manifest = _load_json(plan_bundle / "plan-manifest.json", "planning manifest")
    schema_version = manifest.get("schema_version")
    if manifest.get("bundle_id") != plan_bundle.name:
        raise WeeklyReturnError("planning bundle identity differs")
    if schema_version == 2:
        if manifest.get("builder") != "portfolio-current-decomposition-plan" or (
            manifest.get("builder_version"),
            manifest.get("schema_version"),
        ) not in {("0.17.0", 2), ("0.18.0", 2)}:
            raise WeeklyReturnError("current planning builder identity differs")
        identity = dict(manifest)
        identity.pop("bundle_id", None)
        if (
            hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
            != plan_bundle.name
        ):
            raise WeeklyReturnError("current planning bundle hash differs")
    elif schema_version != 1:
        raise WeeklyReturnError("planning bundle schema is unsupported")
    rows = _objects(manifest.get("inputs"), "planning inputs") + _objects(
        manifest.get("outputs"), "planning outputs"
    )
    expected: dict[str, str] = {}
    for item in rows:
        relative = _text(item.get("path"), "planning path")
        portable = PurePosixPath(relative)
        if (
            portable.is_absolute()
            or portable.as_posix() != relative
            or not portable.parts
            or any(part in {"", ".", ".."} for part in portable.parts)
            or "\\" in relative
            or ":" in relative
            or relative in expected
        ):
            raise WeeklyReturnError(f"planning path is invalid: {relative}")
        path = plan_bundle.joinpath(*portable.parts)
        candidate = _filesystem_path(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise WeeklyReturnError(
                f"planning artifact is missing or linked: {relative}"
            )
        expected_sha256 = _text(item.get("sha256"), "planning sha256")
        content = candidate.read_bytes()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        actual_size = len(content)
        if actual_sha256 != expected_sha256:
            raise WeeklyReturnError(f"planning input hash differs: {relative}")
        if schema_version == 2:
            expected_size = item.get("size_bytes")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
                or actual_size != expected_size
            ):
                raise WeeklyReturnError(f"planning input size differs: {relative}")
        expected[relative] = expected_sha256
    return expected


def _wide_return_columns(
    path: Path, excluded: set[str]
) -> dict[str, list[tuple[date, Decimal]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "date" not in reader.fieldnames:
            raise WeeklyReturnError(f"wide return source lacks date: {path.name}")
        columns = [column for column in reader.fieldnames if column not in excluded]
        if not columns:
            raise WeeklyReturnError(f"wide return source has no series: {path.name}")
        values: dict[str, list[tuple[date, Decimal]]] = {
            column: [] for column in columns
        }
        observed_dates: set[date] = set()
        for row in reader:
            try:
                observed_date = date.fromisoformat(
                    _text(row.get("date"), "return date")
                )
            except ValueError as exc:
                raise WeeklyReturnError(f"invalid return date: {path.name}") from exc
            if observed_date in observed_dates:
                raise WeeklyReturnError(f"duplicate return date: {path.name}")
            observed_dates.add(observed_date)
            for column in columns:
                raw_value = row.get(column)
                if raw_value is None or not str(raw_value).strip():
                    continue
                values[column].append(
                    (observed_date, _decimal(raw_value, f"{path.name}.{column}"))
                )
    return values


def _selected_return_columns(
    path: Path, selected: Iterable[str], as_of: date
) -> dict[str, list[tuple[date, Decimal]]]:
    wanted = tuple(selected)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = csv.DictReader(handle).fieldnames
    if header is None or "date" not in header:
        raise WeeklyReturnError(f"wide return source lacks date: {path.name}")
    absent = [column for column in wanted if column not in header]
    if absent:
        raise WeeklyReturnError(f"market benchmark column is absent: {absent[0]}")
    columns = _wide_return_columns(
        path, {name for name in header if name not in wanted}
    )
    return {
        column: [item for item in columns[column] if item[0] <= as_of]
        for column in wanted
    }


def _daily_benchmark_block(
    selected: Mapping[str, list[tuple[date, Decimal]]],
    relative: str,
    sha256: str,
) -> dict[str, Any]:
    series = []
    for column in FULL_HISTORY_BENCHMARKS:
        observations = sorted(selected[column])
        series.append(
            {
                "dates": [day.isoformat() for day, _ in observations],
                "first_date": observations[0][0].isoformat(),
                "last_date": observations[-1][0].isoformat(),
                "observation_count": len(observations),
                "returns": [_decimal_text(value) for _, value in observations],
                "series_id": f"market:{column}",
                "ticker": column,
            }
        )
    return {
        "first_price_policy": "DROPPED_NO_PREDECESSOR",
        "return_basis": MARKET_BENCHMARK_SOURCE[2],
        "series": series,
        "source_artifact": {"path": relative, "sha256": sha256},
        "source_id": DAILY_BENCHMARK_SOURCE_ID,
        "status": "AVAILABLE",
        "unit": "DECIMAL_DAILY_RETURN",
    }


def _public_rows(
    plan_bundle: Path, as_of: date
) -> tuple[list[dict[str, Any]], list[Path], dict[str, Any]]:
    expected_hashes = _manifest_hashes(plan_bundle)
    rows_by_id: dict[str, dict[str, Any]] = {}
    input_paths: list[Path] = [plan_bundle / "plan-manifest.json"]
    for source_key, (
        relative,
        primary_group,
        return_basis,
        excluded,
    ) in PUBLIC_SOURCES.items():
        path = plan_bundle / Path(relative)
        if expected_hashes.get(relative) != hash_file(path)[0]:
            raise WeeklyReturnError(f"planning input hash differs: {relative}")
        input_paths.append(path)
        for column, observations in _wide_return_columns(path, excluded).items():
            if column.upper() in FULL_HISTORY_BENCHMARKS:
                continue
            if source_key.startswith("us-economic"):
                canonical_id = f"factor:us:{column.upper()}"
                display_name = f"U.S. {column.upper()}"
            elif source_key.startswith("europe-economic"):
                canonical_id = f"factor:europe:{column.upper()}"
                display_name = f"Europe {column.upper()}"
            else:
                canonical_id = f"market:{column.upper()}"
                display_name = column.upper()
            values, observation_meta, calendar_basis = _weekly_series(
                observations, as_of=as_of
            )
            display_groups = [primary_group]
            if column.upper() in CORE_BENCHMARKS:
                display_groups.append("CORE_BENCHMARK")
                display_name = f"{column.upper()} · {CORE_BENCHMARKS[column.upper()]}"
            item = {
                "absent_value_status": "UNAVAILABLE_NO_OBSERVATION_IN_WEEK",
                "calendar_basis": calendar_basis,
                "coverage": _coverage(values),
                "display_groups": display_groups,
                "display_name": display_name,
                "primary_group": primary_group,
                "observation_meta": observation_meta,
                "return_basis": return_basis,
                "series_id": canonical_id,
                "source_key": source_key,
                "values": values,
            }
            if canonical_id in rows_by_id:
                raise WeeklyReturnError(
                    f"canonical weekly series is duplicated: {canonical_id}"
                )
            rows_by_id[canonical_id] = item
    market_relative, market_group, market_basis = MARKET_BENCHMARK_SOURCE
    market_path = plan_bundle / Path(market_relative)
    market_sha256 = hash_file(market_path)[0]
    if expected_hashes.get(market_relative) != market_sha256:
        raise WeeklyReturnError(f"planning input hash differs: {market_relative}")
    input_paths.append(market_path)
    market_columns = _selected_return_columns(
        market_path, FULL_HISTORY_BENCHMARKS, as_of
    )
    for column, observations in market_columns.items():
        if not observations:
            raise WeeklyReturnError(f"market benchmark has no observations: {column}")
        values, observation_meta, calendar_basis = _weekly_series(
            observations, as_of=as_of
        )
        canonical_id = f"market:{column}"
        if canonical_id in rows_by_id:
            raise WeeklyReturnError(
                f"canonical weekly series is duplicated: {canonical_id}"
            )
        rows_by_id[canonical_id] = {
            "absent_value_status": "UNAVAILABLE_NO_OBSERVATION_IN_WEEK",
            "calendar_basis": calendar_basis,
            "coverage": _coverage(values),
            "display_groups": [market_group, "CORE_BENCHMARK"],
            "display_name": f"{column} · {CORE_BENCHMARKS[column]}",
            "primary_group": market_group,
            "observation_meta": observation_meta,
            "return_basis": market_basis,
            "series_id": canonical_id,
            "source_key": "core-benchmark-market",
            "values": values,
        }
    daily_block = _daily_benchmark_block(market_columns, market_relative, market_sha256)
    benchmark_ids = {
        row["series_id"].split(":", 1)[1]
        for row in rows_by_id.values()
        if "CORE_BENCHMARK" in row["display_groups"]
    }
    if benchmark_ids != set(CORE_BENCHMARKS):
        raise WeeklyReturnError("core benchmark boundary differs")
    return [rows_by_id[key] for key in sorted(rows_by_id)], input_paths, daily_block


def build_weekly_returns(
    correlation_root: Path,
    strategy_returns: Path,
    plan_bundle: Path,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    correlation, generation_manifest, correlation_path, manifest_path = (
        _correlation_inputs(correlation_root)
    )
    as_of = _correlation_cutoff(correlation, generation_manifest)
    strategy_rows = _strategy_rows(
        strategy_returns, correlation, generation_manifest, as_of
    )
    public_rows, public_paths, daily_benchmarks = _public_rows(plan_bundle, as_of)
    rows = [*strategy_rows, *public_rows]
    series_ids = [row["series_id"] for row in rows]
    if len(series_ids) != len(set(series_ids)):
        raise WeeklyReturnError("weekly series identity is duplicated")
    weeks: set[str] = set()
    for row in rows:
        if "values" in row:
            weeks.update(row["values"])
        else:
            for values in row["values_by_mode"].values():
                weeks.update(values)
    current_week = _current_week(as_of)
    current_week_start = date.fromisoformat(current_week["week_start"])
    weeks.add(current_week_start.isoformat())
    payload: dict[str, Any] = {
        "analytics_schema_version": SCHEMA_VERSION,
        "as_of_date": as_of.isoformat(),
        "builder_version": BUILDER_VERSION,
        "current_week": current_week,
        "daily_benchmark_returns": daily_benchmarks,
        "default_evidence_mode": "LIVE_ONLY",
        "display_groups": [
            "STRATEGY",
            "ECONOMIC_FACTOR_US",
            "ECONOMIC_FACTOR_EUROPE",
            "STYLE_US_EQUITY",
            "STYLE_MULTI_ASSET",
            "SECTOR",
            "INDUSTRY",
            "CORE_BENCHMARK",
        ],
        "observation_meta_fields": [
            "daily_observation_count",
            "actual_observed_start",
            "actual_observed_end",
            "coverage_status",
            "boundary_reasons",
        ],
        "series": rows,
        "strategy_evidence_modes": list(STRATEGY_MODES),
        "week_policy": {
            "absent_value": "UNAVAILABLE_NOT_ZERO",
            "aggregation": "PRODUCT_OF_ONE_PLUS_DAILY_RETURN_MINUS_ONE",
            "boundary": "ISO_MONDAY_THROUGH_SUNDAY",
            "non_trading_dates": "NO_OBSERVATION_REQUIRED",
        },
        "weeks": sorted(weeks),
    }
    payload["analytics_hash"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return (
        payload,
        tuple([correlation_path, manifest_path, strategy_returns, *public_paths]),
    )


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise GenerationConflictError(f"artifact differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def build_candidate(
    correlation_root: Path,
    strategy_returns: Path,
    plan_bundle: Path,
    output_root: Path,
) -> dict[str, Any]:
    payload, _ = build_weekly_returns(correlation_root, strategy_returns, plan_bundle)
    content = canonical_json_bytes(payload)
    candidate_id = hashlib.sha256(content).hexdigest()
    candidate = output_root / "candidates" / candidate_id
    _write_exact(candidate / "weekly-returns.json", content)
    return {
        "candidate_dir": str(candidate),
        "candidate_id": candidate_id,
        "series_count": len(payload["series"]),
        "week_count": len(payload["weeks"]),
    }


def verify_candidate(
    correlation_root: Path,
    strategy_returns: Path,
    plan_bundle: Path,
    candidate_dir: Path,
) -> dict[str, Any]:
    payload, _ = build_weekly_returns(correlation_root, strategy_returns, plan_bundle)
    correlation, _manifest, _correlation_path, _manifest_path = _correlation_inputs(
        correlation_root
    )
    dependence_modes = correlation.get("dependence_modes")
    live_dependence = (
        dependence_modes.get("LIVE_ONLY")
        if isinstance(dependence_modes, dict)
        else None
    )
    live_composition = (
        live_dependence.get("series_composition")
        if isinstance(live_dependence, dict)
        else None
    )
    expected_strategies = (
        set(live_composition) if isinstance(live_composition, dict) else set()
    )
    observed_strategies = {
        str(row.get("display_name"))
        for row in payload["series"]
        if row.get("primary_group") == "STRATEGY"
    }
    expected = canonical_json_bytes(payload)
    expected_id = hashlib.sha256(expected).hexdigest()
    path = candidate_dir / "weekly-returns.json"
    checks = {
        "candidate_identity": candidate_dir.name == expected_id,
        "content_exact": path.is_file()
        and not path.is_symlink()
        and path.read_bytes() == expected,
        "current_week_visible_by_default": payload["current_week"]["default_visible"]
        is True,
        "current_week_source_cutoff": payload["current_week"]["data_through_date"]
        == payload["as_of_date"]
        and payload["current_week"]["is_partial"]
        == (
            date.fromisoformat(payload["as_of_date"])
            < date.fromisoformat(payload["current_week"]["week_end"])
        ),
        "evidence_modes_exact": payload["strategy_evidence_modes"]
        == list(STRATEGY_MODES),
        "strategy_boundary": bool(expected_strategies)
        and observed_strategies == expected_strategies,
        "core_benchmarks_exact": sum(
            "CORE_BENCHMARK" in row["display_groups"] for row in payload["series"]
        )
        == len(CORE_BENCHMARKS),
    }
    if not all(checks.values()):
        raise WeeklyReturnError("weekly candidate verification failed")
    return {"all_passed": True, "candidate_id": expected_id, "checks": checks}


def publish(
    correlation_root: Path,
    strategy_returns: Path,
    plan_bundle: Path,
    candidate_dir: Path,
    output_root: Path,
) -> dict[str, Any]:
    verification = verify_candidate(
        correlation_root, strategy_returns, plan_bundle, candidate_dir
    )
    payload, input_paths = build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )
    as_of = date.fromisoformat(payload["as_of_date"])
    artifacts = [
        SourceArtifact.from_path(
            path,
            artifact_id=f"input/{index:02d}-{path.name}",
            as_of_date=as_of,
            schema_name="portfolio-weekly-return-input",
            schema_version="1",
        )
        for index, path in enumerate(input_paths)
    ]
    artifacts.append(
        SourceArtifact.from_path(
            candidate_dir / "weekly-returns.json",
            artifact_id="output/weekly-returns.json",
            as_of_date=as_of,
            schema_name="portfolio-weekly-returns",
            schema_version="2",
        )
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-weekly-returns", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Compact weekly strategy, factor, style, sector, industry, and "
                "core-benchmark returns with absent weeks explicitly unavailable"
            ),
            fact_types=(
                "weekly strategy returns",
                "weekly factor returns",
                "weekly style returns",
                "weekly sector returns",
                "weekly industry returns",
                "weekly benchmark returns",
            ),
            observed_start=date.fromisoformat(payload["weeks"][0]),
            observed_end=as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    destination = manifest_path.parent / "weekly-returns.json"
    if destination.exists():
        if (
            destination.read_bytes()
            != (candidate_dir / "weekly-returns.json").read_bytes()
        ):
            raise GenerationConflictError("weekly generation artifact differs")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            (candidate_dir / "weekly-returns.json").open("rb") as source,
            destination.open("xb") as target,
        ):
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    pointer = publish_generation(output_root, manifest.generation_id)
    return {
        "analytics_hash": payload["analytics_hash"],
        "candidate_id": verification["candidate_id"],
        "generation_id": manifest.generation_id,
        "publication_state": pointer.state.value,
        "series_count": len(payload["series"]),
        "week_count": len(payload["weeks"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--correlation-root", type=Path, required=True)
        command.add_argument("--strategy-returns", type=Path, required=True)
        command.add_argument("--plan-bundle", type=Path, required=True)
        if name in {"build", "publish"}:
            command.add_argument("--output-root", type=Path, required=True)
        if name in {"verify", "publish"}:
            command.add_argument("--candidate-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(
                args.correlation_root,
                args.strategy_returns,
                args.plan_bundle,
                args.output_root,
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.correlation_root,
                args.strategy_returns,
                args.plan_bundle,
                args.candidate_dir,
            )
        else:
            result = publish(
                args.correlation_root,
                args.strategy_returns,
                args.plan_bundle,
                args.candidate_dir,
                args.output_root,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-weekly-returns: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

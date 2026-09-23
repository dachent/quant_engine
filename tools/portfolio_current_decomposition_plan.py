"""Materialize current-strategy factor/style/industry decomposition inputs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import Any, Mapping, Sequence, cast


BUILDER_VERSION = "0.18.0"
PUBLIC_FACTOR_BUILDER = "portfolio-public-decomposition-sources"
PUBLIC_FACTOR_BUILDER_VERSION = "0.3.0"
PUBLIC_FACTOR_FRESHNESS_POLICY = {
    "policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1",
    "observation_cadence": "DAILY",
    "publication_cadence": "MONTHLY",
    "deadline_anchor": "CALENDAR_MONTH_END",
    "publication_lag_days": 45,
    "calendar": "CALENDAR_DAY",
    "deadline_inclusive": True,
    "boundary_rule": "LATEST_PROVIDER_VALID_DAY_IN_ELIGIBLE_MONTH",
    "receipt_rule": "CAPTURED_WITHIN_CURRENT_RUN",
}
PUBLIC_FACTOR_INPUT_KEYS = ("us_factors", "europe_factors", "us_industries")
PUBLIC_FACTOR_OUTPUT_NAMES = {
    "us_factors": "us_factors_daily.csv",
    "europe_factors": "europe_factors_daily.csv",
    "us_industries": "us_49_industries_value_weighted_daily.csv",
}
PUBLIC_FACTOR_RECEIPT_KEYS = {
    "us_factors",
    "us_momentum",
    "europe_factors",
    "europe_momentum",
    "us_industries",
}
ASSURANCE_PROFILES = ("DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC")
LENSES = (
    "ECONOMIC_FACTOR",
    "ETF_STYLE",
    "RETURN_INDUSTRY",
    "RETURN_SECTOR",
)
SURFACES = ("STATIC", "DYNAMIC_FLS")
INPUT_KEYS = (
    "us_factors",
    "europe_factors",
    "style_returns",
    "sector_returns",
    "us_industries",
)
MARKET_CONTRACT_SOURCE_IDS = {
    "style_returns": "public-etf-style-sector-and-market-prices",
    "sector_returns": "public-etf-style-sector-and-market-prices",
}
METHOD_SCOPE_NON_APPLICABLE_SOURCES = {
    "p123-ai-factor": frozenset(("europe-economic", "multi-asset-style")),
    "p123-eu": frozenset(
        (
            "us-economic",
            "us-equity-style",
            "multi-asset-style",
            "us-sector",
            "us-industry",
        )
    ),
    "qsi": frozenset(("europe-economic",)),
    "tt-portfolio": frozenset(("europe-economic",)),
    "volatis-strategy": frozenset(("europe-economic",)),
    "grizzlybears-vtma": frozenset(("europe-economic",)),
    "robuxio-crypto": frozenset(
        (
            "us-economic",
            "europe-economic",
            "us-equity-style",
            "us-sector",
            "us-industry",
        )
    ),
}
METHOD_SCOPE_NON_APPLICABLE_DISPOSITION = {
    "authority": "ISSUE_328_ACCEPTED_STRATEGY_TO_LENS_ROUTING",
    "readiness": "NOT_APPLICABLE_METHOD_SCOPE",
    "reason": "NOT_APPLICABLE_METHOD_SCOPE",
    "requirement": "NOT_APPLICABLE_METHOD_SCOPE",
}
TARGET_HEADERS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)
LAYERED_PROJECTION_HEADERS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)
LAYERED_SEGMENT_HEADERS = (
    "strategy_id",
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
    "segment_id",
    "source_id",
    "source_generation_id",
    "mode_admission",
    "return_basis",
    "return_type",
    "calendar",
    "frequency",
)


class CurrentPlanError(ValueError):
    """The current-strategy plan or a hash-bound input is invalid."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise CurrentPlanError(f"{name} must be a regular file")
    content = path.read_bytes()
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentPlanError(f"{name} must be UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise CurrentPlanError(f"{name} must be an object")
    return cast(dict[str, Any], decoded), content


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CurrentPlanError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CurrentPlanError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurrentPlanError(f"{name} must be non-empty text")
    return value.strip()


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CurrentPlanError(f"{name} must be an integer >= {minimum}")
    return value


def _decimal(value: object, name: str, minimum: Decimal) -> Decimal:
    if isinstance(value, bool):
        raise CurrentPlanError(f"{name} must be numeric")
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CurrentPlanError(f"{name} must be numeric") from exc
    if not accepted.is_finite() or accepted < minimum:
        raise CurrentPlanError(f"{name} is outside its accepted range")
    return accepted


def _portable(value: object, name: str) -> str:
    accepted = _text(value, name)
    path = PurePosixPath(accepted)
    if (
        path.is_absolute()
        or "\\" in accepted
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CurrentPlanError(f"{name} must be a portable relative path")
    return accepted


def _source_path(root: Path, relative: object, name: str) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / PurePosixPath(_portable(relative, name))).resolve()
    if (
        not path.is_relative_to(resolved_root)
        or path.is_symlink()
        or not path.is_file()
    ):
        raise CurrentPlanError(f"{name} does not resolve to a regular source file")
    return path


def _verify_definition(root: Path, definition: Mapping[str, Any], name: str) -> Path:
    path = _source_path(root, definition.get("relative_path"), f"{name}.relative_path")
    content = path.read_bytes()
    if _text(definition.get("expected_sha256"), f"{name}.expected_sha256") != _sha256(
        content
    ) or _integer(
        definition.get("expected_size_bytes"), f"{name}.expected_size_bytes"
    ) != len(content):
        raise CurrentPlanError(f"{name} source hash or size differs")
    return path


def _day(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise CurrentPlanError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != accepted:
        raise CurrentPlanError(f"{name} must be a canonical ISO date")
    return parsed


def _age_disposition(
    *,
    complete_through: date,
    evaluated_on: date,
    warning_after_days: int,
    limiting_inputs: Sequence[str],
) -> dict[str, object]:
    """Describe data age without converting complete history into unavailability."""

    if complete_through > evaluated_on:
        raise CurrentPlanError("complete data boundary exceeds the evaluation date")
    inputs = sorted(set(limiting_inputs))
    if not inputs:
        raise CurrentPlanError("data age disposition has no limiting input")
    age_days = (evaluated_on - complete_through).days
    return {
        "age_days": age_days,
        "complete_through": complete_through.isoformat(),
        "evaluated_on": evaluated_on.isoformat(),
        "limiting_inputs": inputs,
        "status": (
            "WARN_COMPLETE_DATA_AGE"
            if age_days > warning_after_days
            else "CURRENT_WITHIN_AGE_THRESHOLD"
        ),
        "warning_after_days": warning_after_days,
    }


def _latest_common_complete_date(
    date_sets: Sequence[set[date]], *, evaluated_on: date, name: str
) -> date:
    """Return the latest date present in every required input without filling."""

    if not date_sets or any(not values for values in date_sets):
        raise CurrentPlanError(f"{name} has no complete accepted observations")
    common = set.intersection(*date_sets)
    eligible = [observed for observed in common if observed <= evaluated_on]
    if not eligible:
        raise CurrentPlanError(f"{name} has no common complete accepted date")
    return max(eligible)


def _utc_datetime(value: object, name: str) -> datetime:
    accepted = _text(value, name)
    if not accepted.endswith("Z"):
        raise CurrentPlanError(f"{name} must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(accepted.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise CurrentPlanError(f"{name} must be RFC3339 UTC") from exc
    if parsed.isoformat().replace("+00:00", "Z") != accepted:
        raise CurrentPlanError(f"{name} must be canonical RFC3339 UTC")
    return parsed


def _month_end(value: date) -> date:
    next_month = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def _required_observation_month_end(evaluated_on: date) -> date:
    candidate = _month_end(evaluated_on)
    while candidate + timedelta(days=45) > evaluated_on:
        candidate = _month_end(candidate.replace(day=1) - timedelta(days=1))
    return candidate


def _read_csv(path: Path, name: str) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = tuple(reader.fieldnames or ())
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise CurrentPlanError(f"{name} is not readable CSV") from exc
    if not headers or not rows or len(headers) != len(set(headers)):
        raise CurrentPlanError(f"{name} must have unique headers and rows")
    if any(set(row) != set(headers) for row in rows):
        raise CurrentPlanError(f"{name} row schema differs")
    return headers, rows


def _csv_bytes(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _layered_history_module() -> Any:
    module_name = "portfolio_target_series_for_current_decomposition_plan"
    module_path = Path(__file__).resolve().with_name("portfolio_target_series.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CurrentPlanError("layered history verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise CurrentPlanError("layered history verifier cannot be loaded") from exc
    return module


def _layered_history_targets(
    *,
    generation_root: Path,
    contract_path: Path,
    strategies: Sequence[Mapping[str, Any]],
    evidence_mode: str,
    evaluated_on: date,
    assurance_profile: str = "DEVELOPMENT",
) -> dict[str, Any]:
    if assurance_profile not in ASSURANCE_PROFILES:
        raise CurrentPlanError("assurance_profile is unsupported")
    planned = [
        (
            _text(strategy.get("strategy_id"), "strategy_id"),
            _text(strategy.get("series_id"), "series_id"),
        )
        for strategy in strategies
        if _objects(strategy.get("assignments", []), "assignments")
    ]
    if not planned:
        raise CurrentPlanError("current plan has no assigned strategies")
    if len({item[0] for item in planned}) != len(planned) or len(
        {item[1] for item in planned}
    ) != len(planned):
        raise CurrentPlanError("planned strategy or series identity is duplicated")

    verifier = _layered_history_module()
    try:
        verification = verifier.verify_layered_history_generation(
            generation_root, contract_path
        )
    except (OSError, ValueError) as exc:
        raise CurrentPlanError(f"layered history verification failed: {exc}") from exc
    if verification.get("verification") != "VALID":
        raise CurrentPlanError("layered history verification is not valid")

    manifest_path = generation_root / "generation.json"
    coverage_path = generation_root / "coverage.json"
    segments_path = generation_root / "segments.csv"
    projection_name = (
        "live-only.csv" if evidence_mode == "LIVE_ONLY" else "live-plus-backtest.csv"
    )
    projection_path = generation_root / projection_name
    manifest, manifest_content = _load_json(manifest_path, "layered generation")
    coverage, coverage_content = _load_json(coverage_path, "layered coverage")
    contract, contract_content = _load_json(contract_path, "financial MVP contract")
    product = _object(contract.get("product"), "financial MVP product")
    as_of_policy = _object(product.get("as_of_policy"), "financial MVP as_of_policy")
    profiles = _object(
        as_of_policy.get("execution_profiles"), "financial MVP execution profiles"
    )
    profile_policy = _text(
        profiles.get(assurance_profile),
        f"financial MVP {assurance_profile} execution profile",
    )
    contract_sources: dict[str, dict[str, Any]] = {}
    for source in _objects(contract.get("source_segments"), "financial MVP sources"):
        source_id = _text(source.get("source_id"), "financial MVP source_id")
        if source_id in contract_sources:
            raise CurrentPlanError("financial MVP source identity is duplicated")
        contract_sources[source_id] = source
    receipt_outputs = _object(manifest.get("outputs"), "layered generation outputs")
    receipt_schemas: dict[str, dict[str, str]] = {}
    layered_output_contents: dict[str, bytes] = {}
    for name in sorted(receipt_outputs):
        portable_name = PurePosixPath(name)
        if len(portable_name.parts) != 1:
            raise CurrentPlanError("layered history output name differs")
        output = _object(receipt_outputs.get(name), f"layered output {name}")
        receipt_schemas[name] = {
            "schema_name": _text(output.get("schema_name"), f"{name} schema_name"),
            "schema_version": _text(
                output.get("schema_version"), f"{name} schema_version"
            ),
        }
        layered_output_contents[name] = (generation_root / name).read_bytes()
    projection_headers, projection_rows = _read_csv(
        projection_path, "layered history projection"
    )
    segment_headers, segment_rows = _read_csv(segments_path, "layered history segments")
    if projection_headers != LAYERED_PROJECTION_HEADERS:
        raise CurrentPlanError("layered history projection headers differ")
    if segment_headers != LAYERED_SEGMENT_HEADERS:
        raise CurrentPlanError("layered history segment headers differ")
    generation_id = _text(manifest.get("generation_id"), "layered generation_id")
    if verification.get("generation_id") != generation_id:
        raise CurrentPlanError("layered history generation identity differs")
    history_as_of = _day(manifest.get("as_of_date"), "layered history as_of_date")
    if history_as_of > evaluated_on:
        raise CurrentPlanError("layered history exceeds the evaluation date")
    # A receipt older than the evaluation date is stated, not rejected: the
    # identity carries generation_as_of_date, and calculation_boundary plus
    # source_age_dispositions carry complete_through, age_days and the
    # WARN_COMPLETE_DATA_AGE label. Missing history, undispositioned gaps and
    # version conflicts continue to fail for every assurance profile.

    coverage_rows = _objects(coverage.get("strategies"), "layered strategies")
    coverage_by_strategy: dict[str, dict[str, Any]] = {}
    for row in coverage_rows:
        strategy_id = _text(row.get("strategy_id"), "layered strategy_id")
        if strategy_id in coverage_by_strategy:
            raise CurrentPlanError("layered strategy coverage is duplicated")
        coverage_by_strategy[strategy_id] = row

    planned_series: dict[str, str] = {}
    composition: dict[str, dict[str, object]] = {}
    source_age_dispositions: list[dict[str, object]] = []
    selected_admissions = (
        {"INCLUDE_SELECTED_LIVE"}
        if evidence_mode == "LIVE_ONLY"
        else {"INCLUDE_SELECTED_LIVE", "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"}
    )
    for strategy_id, series_id in planned:
        coverage_row = coverage_by_strategy.get(strategy_id)
        if coverage_row is None:
            raise CurrentPlanError(
                f"layered history lacks planned strategy: {strategy_id}"
            )
        if coverage_row.get("series_id") != series_id:
            raise CurrentPlanError(
                f"layered history series differs for planned strategy: {strategy_id}"
            )
        modes = _object(coverage_row.get("modes"), "layered strategy modes")
        mode = _object(modes.get(evidence_mode), f"layered mode {evidence_mode}")
        live_mode = _object(modes.get("LIVE_ONLY"), "layered mode LIVE_ONLY")
        no_matched_prehistory = (
            evidence_mode == "LIVE_PLUS_BACKTEST"
            and mode.get("status") == "REQUIRED_NOT_READY"
            and mode.get("reason")
            in {
                "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED",
                "REQUIRED_EXACT_VERSION_BACKTEST_NOT_ADMITTED",
            }
            and live_mode.get("status") == "AVAILABLE"
        )
        if mode.get("status") != "AVAILABLE" and not no_matched_prehistory:
            reason = _text(mode.get("reason"), "layered history readiness reason")
            raise CurrentPlanError(
                f"layered history {evidence_mode} is not ready for "
                f"{strategy_id}: {reason}"
            )
        planned_series[series_id] = strategy_id
        summaries = _objects(coverage_row.get("segments"), "layered segment summaries")
        selected_summaries = [
            summary
            for summary in summaries
            if summary.get("mode_admission") in selected_admissions
        ]
        if not selected_summaries:
            raise CurrentPlanError(
                f"layered history has no selected segment for {strategy_id}"
            )
        if no_matched_prehistory and any(
            summary.get("evidence_layer") != "ACCOUNT_LIVE"
            for summary in selected_summaries
        ):
            raise CurrentPlanError(
                "unmatched prehistory fallback contains a non-live segment for "
                f"{strategy_id}"
            )
        expected_observations = sum(
            _integer(summary.get("return_observations"), "segment observations", 1)
            for summary in selected_summaries
        )
        live_summaries = [
            summary
            for summary in selected_summaries
            if summary.get("evidence_layer") == "ACCOUNT_LIVE"
        ]
        if not live_summaries:
            raise CurrentPlanError(
                f"layered history has no selected account-live segment for {strategy_id}"
            )
        for summary in live_summaries:
            source_id = _text(summary.get("source_id"), "live source_id")
            live_source = contract_sources.get(source_id)
            if live_source is None:
                raise CurrentPlanError(
                    f"layered history live source is absent from the contract: {source_id}"
                )
            freshness = _object(live_source.get("freshness"), f"{source_id} freshness")
            max_age_days = _integer(
                freshness.get("max_age_days"), f"{source_id} max_age_days"
            )
            observed_end = _day(summary.get("observed_end"), "live observed_end")
            age = _age_disposition(
                complete_through=observed_end,
                evaluated_on=evaluated_on,
                warning_after_days=max_age_days,
                limiting_inputs=(f"{strategy_id}:{source_id}",),
            )
            source_age_dispositions.append(
                {
                    **age,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "source_id": source_id,
                    "strategy_id": strategy_id,
                }
            )
        backtest_observations = sum(
            _integer(summary.get("return_observations"), "backtest observations", 1)
            for summary in selected_summaries
            if summary.get("evidence_layer") == "BACKTEST"
        )
        version = _object(
            coverage_row.get("strategy_version"), "layered strategy_version"
        )
        composition[series_id] = {
            "backtest_observations_used": backtest_observations,
            "evidence_mode": evidence_mode,
            "expected_observations": expected_observations,
            "live_evidence_layer": "ACCOUNT_LIVE",
            "live_inception": min(
                _text(summary.get("observed_start"), "live observed_start")
                for summary in live_summaries
            ),
            "live_observations": sum(
                _integer(summary.get("return_observations"), "live observations", 1)
                for summary in live_summaries
            ),
            "series_id": series_id,
            "status": (
                "AVAILABLE_LIVE_ONLY"
                if evidence_mode == "LIVE_ONLY"
                else (
                    "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
                    if no_matched_prehistory
                    else "AVAILABLE_LIVE_PLUS_BACKTEST"
                )
            ),
            "strategy_version": _text(
                version.get("history_generation_ref"), "history strategy version"
            ),
        }

    allowed_layers = (
        {"ACCOUNT_LIVE"}
        if evidence_mode == "LIVE_ONLY"
        else {"ACCOUNT_LIVE", "BACKTEST"}
    )
    selected_projection = [
        row for row in projection_rows if row["series_id"] in planned_series
    ]
    seen_dates: set[tuple[str, str]] = set()
    selected_counts: dict[str, int] = {series_id: 0 for series_id in planned_series}
    for row in selected_projection:
        key = (row["series_id"], row["date"])
        if key in seen_dates:
            raise CurrentPlanError("layered history contains a duplicate series date")
        seen_dates.add(key)
        selected_counts[row["series_id"]] += 1
        if row["evidence_layer"] not in allowed_layers:
            raise CurrentPlanError("layered history evidence layer differs from mode")
        if (
            composition[row["series_id"]]["status"]
            == "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
            and row["evidence_layer"] != "ACCOUNT_LIVE"
        ):
            raise CurrentPlanError(
                "unmatched prehistory fallback contains a non-live row"
            )
        if _day(row["date"], "layered target date") > evaluated_on:
            raise CurrentPlanError(
                "target observations extend beyond the evaluation date"
            )
        expected_version = composition[row["series_id"]]["strategy_version"]
        if row["strategy_version"] != expected_version:
            raise CurrentPlanError("layered target strategy version differs")
    if any(
        selected_counts[series_id] != composition[series_id]["expected_observations"]
        for series_id in planned_series
    ):
        raise CurrentPlanError("layered projection and coverage observations differ")

    projection_dates_by_series = {
        series_id: {
            _day(row["date"], "layered target date")
            for row in selected_projection
            if row["series_id"] == series_id
        }
        for series_id in planned_series
    }
    common_complete_through = _latest_common_complete_date(
        list(projection_dates_by_series.values()),
        evaluated_on=evaluated_on,
        name="layered history",
    )
    limiting_series = [
        f"{planned_series[series_id]}:{series_id}"
        for series_id, observed in projection_dates_by_series.items()
        if max(observed) == common_complete_through
    ]
    if not limiting_series:
        limiting_series = [
            f"{planned_series[series_id]}:{series_id}"
            for series_id in sorted(planned_series)
        ]
    warning_after_days = min(
        cast(int, row["warning_after_days"]) for row in source_age_dispositions
    )
    calculation_age = _age_disposition(
        complete_through=common_complete_through,
        evaluated_on=evaluated_on,
        warning_after_days=warning_after_days,
        limiting_inputs=limiting_series,
    )

    segment_index: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = {}
    for row in segment_rows:
        segment_key = (
            row["series_id"],
            row["date"],
            row["return"],
            row["evidence_layer"],
            row["strategy_version"],
        )
        segment_index.setdefault(segment_key, []).append(row)
    calculation_complete_through = common_complete_through.isoformat()
    target_rows: list[dict[str, str]] = []
    interval_segments: dict[str, list[dict[str, str]]] = {
        series_id: [] for series_id in planned_series
    }
    for row in selected_projection:
        projection_key = (
            row["series_id"],
            row["date"],
            row["return"],
            row["evidence_layer"],
            row["strategy_version"],
        )
        matches = segment_index.get(projection_key, [])
        if (
            len(matches) != 1
            or matches[0]["strategy_id"] != planned_series[row["series_id"]]
        ):
            raise CurrentPlanError("layered projection does not match one segment")
        segment = matches[0]
        if row["date"] > calculation_complete_through:
            # The calculation window is the latest date common to every planned
            # series.  Rows after it stay in the accepted history -- and in
            # accepted_history_complete_through below -- but must not enter the
            # factor target series: portfolio_correlation windows the dependence
            # lens to the same common date, and the control panel requires the
            # factor window to be contained by the dependence window.  Without
            # this the two lenses silently describe different periods whenever
            # one strategy reports past the common date.
            continue
        target_rows.append(
            {
                "series_id": row["series_id"],
                "date": row["date"],
                "return": row["return"],
                "evidence_layer": row["evidence_layer"],
                "return_basis": segment["return_basis"],
                "return_type": segment["return_type"],
                "cost": "",
            }
        )
        interval_segments[row["series_id"]].append(segment)
    target_rows.sort(key=lambda item: (item["series_id"], item["date"]))
    series_interval_policies: dict[str, dict[str, str]] = {}
    for series_id, value in composition.items():
        value.pop("expected_observations", None)
        value["accepted_history_complete_through"] = max(
            projection_dates_by_series[series_id]
        ).isoformat()
        value["calculation_observations_used"] = sum(
            row["series_id"] == series_id for row in target_rows
        )
        segments = interval_segments[series_id]
        source_ids = {segment["source_id"] for segment in segments}
        calendars = {segment["calendar"] for segment in segments}
        frequencies = {segment["frequency"] for segment in segments}
        source_temporal_matches = True
        for segment in segments:
            contract_source = contract_sources.get(segment["source_id"])
            if contract_source is None:
                source_temporal_matches = False
                break
            temporal = _object(contract_source.get("temporal"), "source temporal")
            if (
                temporal["calendar"] != segment["calendar"]
                or temporal["cadence"] != segment["frequency"]
            ):
                source_temporal_matches = False
                break
        if (
            calendars == {"ALWAYS_ON"}
            and frequencies == {"DAILY"}
            and source_temporal_matches
            and {segment["return_type"] for segment in segments} == {"TOTAL"}
        ):
            series_interval_policies[series_id] = {
                "finality": "FINALIZED_THROUGH_DECLARED_LABEL",
                "finalized_through": calculation_complete_through,
                "gap_disposition": "REJECT_BUCKET",
                "native_calendar": "CALENDAR_DAY_24_7",
                "native_label_convention": "SOURCE_DECLARED_DAILY_LABEL",
                "return_basis": "MIXED_NET_MODEL",
                "source_vintage": _sha256("|".join(sorted(source_ids)).encode("utf-8")),
            }
            value["interval_association_status"] = "AVAILABLE_SOURCE_LABELLED"
        else:
            value["interval_association_status"] = (
                "UNAVAILABLE_INTERVAL_CALENDAR_AUTHORITY"
            )
    target_content = _csv_bytes(
        TARGET_HEADERS,
        [[row[header] for header in TARGET_HEADERS] for row in target_rows],
    )
    return {
        "composition": composition,
        "series_interval_policies": series_interval_policies,
        "contents": {
            "financial-mvp-contract.json": contract_content,
            "generation.json": manifest_content,
            **layered_output_contents,
            "selected-targets.csv": target_content,
        },
        "identity": {
            "as_of_date": max(
                max(observed) for observed in projection_dates_by_series.values()
            ).isoformat(),
            "assurance_profile": assurance_profile,
            "assurance_profile_policy": profile_policy,
            "calculation_boundary": calculation_age,
            "contract_sha256": _sha256(contract_content),
            "coverage_sha256": _sha256(coverage_content),
            "generation_as_of_date": history_as_of.isoformat(),
            "generation_id": generation_id,
            "generation_receipt_sha256": _sha256(manifest_content),
            "mode": evidence_mode,
            "projection": projection_name,
            "projection_sha256": _sha256(projection_path.read_bytes()),
            "segments_sha256": _sha256(segments_path.read_bytes()),
            "selected_strategy_ids": sorted(strategy_id for strategy_id, _ in planned),
            "selected_targets_sha256": _sha256(target_content),
            "source_age_dispositions": sorted(
                source_age_dispositions,
                key=lambda row: (
                    cast(str, row["strategy_id"]),
                    cast(str, row["source_id"]),
                ),
            ),
        },
        "rows": target_rows,
        "target_content": target_content,
        "contract_schema_version": _integer(
            contract.get("contract_schema_version"),
            "financial MVP contract schema",
            1,
        ),
        "schema_definitions": {
            "financial-mvp-contract.json": {
                "schema_name": "financial-mvp-source-contract",
                "schema_version": str(contract["contract_schema_version"]),
            },
            "generation.json": {
                "schema_name": "layered-history-generation",
                "schema_version": str(
                    _integer(
                        manifest.get("schema_version"), "layered schema_version", 1
                    )
                ),
            },
            **receipt_schemas,
            "selected-targets.csv": {
                "schema_name": "portfolio-decomposition-target-series",
                "schema_version": "1",
            },
        },
    }


def _catalog_universes(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for universe in _objects(catalog.get("universes"), "catalog.universes"):
        universe_id = _text(universe.get("universe_id"), "catalog.universe_id")
        if universe_id in result:
            raise CurrentPlanError("catalog universe IDs must be unique")
        result[universe_id] = universe
    return result


def _candidates(universe: Mapping[str, Any], name: str) -> tuple[str, ...]:
    values = tuple(_candidate_records(universe, name))
    if not values or len(values) != len(set(values)):
        raise CurrentPlanError(f"{name} candidate IDs must be unique and non-empty")
    return values


def _candidate_records(
    universe: Mapping[str, Any], name: str
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for item in _objects(universe.get("candidates"), f"{name}.candidates"):
        candidate_id = _text(item.get("candidate_id"), f"{name}.candidate_id")
        if candidate_id in records:
            raise CurrentPlanError(f"{name} candidate IDs must be unique")
        records[candidate_id] = item
    if not records:
        raise CurrentPlanError(f"{name} candidates must be non-empty")
    return records


def _proxy_rationale(candidate_id: str, candidate: Mapping[str, Any], lens: str) -> str:
    family = str(candidate.get("economic_family") or "UNCLASSIFIED").strip()
    source_id = str(candidate.get("source_id") or "UNAVAILABLE_NOT_DECLARED").strip()
    return (
        f"{candidate_id} represents the declared {family} family for the {lens} "
        f"candidate universe using {source_id}; raw orthogonality is not assumed, "
        "and final inclusion is determined only by training-period validation, "
        "collinearity diagnostics, and the declared parsimony rule."
    )


def _column(candidate: str, policy: str) -> str:
    if policy == "CANDIDATE_ID":
        return candidate
    if policy == "LOWER_CANDIDATE_ID":
        return candidate.lower()
    if policy == "REMOVE_EU_PREFIX_THEN_LOWER" and candidate.startswith("EU_"):
        return candidate.removeprefix("EU_").lower()
    raise CurrentPlanError(f"unsupported source column policy {policy!r}")


def _verify_market_manifest(
    path: Path, manifest_path: Path, catalog_sha256: str
) -> None:
    manifest, _content = _load_json(manifest_path, "market source manifest")
    catalog = _object(manifest.get("catalog"), "market manifest catalog")
    if catalog.get("sha256") != catalog_sha256:
        raise CurrentPlanError(
            "market manifest catalog differs from the current catalog"
        )
    expected: dict[str, Any] | None = None
    if isinstance(manifest.get("outputs"), list):
        expected = next(
            (
                item
                for item in _objects(manifest["outputs"], "market outputs")
                if item.get("path") == path.name
            ),
            None,
        )
        size_key = "bytes"
    else:
        output = manifest.get("output")
        expected = _object(output, "market output") if output is not None else None
        size_key = "size_bytes"
    if expected is None:
        raise CurrentPlanError(f"market manifest has no output for {path.name}")
    content = path.read_bytes()
    if expected.get("sha256") != _sha256(content) or _integer(
        expected.get(size_key), f"market manifest {path.name} size"
    ) != len(content):
        raise CurrentPlanError(f"market input {path.name} differs from its manifest")


def _verify_public_factor_manifest(
    manifest_path: Path,
    market_paths: Mapping[str, Path],
    catalog_sha256: str,
    planning_evaluation_date: date,
    assurance_profile: str,
) -> tuple[dict[str, date], dict[str, object]]:
    manifest, _content = _load_json(manifest_path, "public factor source manifest")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("builder") != PUBLIC_FACTOR_BUILDER
        or manifest.get("builder_version") != PUBLIC_FACTOR_BUILDER_VERSION
    ):
        raise CurrentPlanError("public factor manifest contract is unsupported")
    catalog = _object(manifest.get("catalog"), "public factor manifest catalog")
    if catalog.get("sha256") != catalog_sha256:
        raise CurrentPlanError(
            "public factor manifest catalog differs from the current catalog"
        )
    if manifest.get("freshness_policy") != PUBLIC_FACTOR_FRESHNESS_POLICY:
        raise CurrentPlanError("public factor freshness policy differs")

    run = _object(manifest.get("acquisition_run"), "public factor acquisition run")
    if run.get("acquisition_requirement") != "EVERY_SOURCE_CAPTURED_WITHIN_CURRENT_RUN":
        raise CurrentPlanError("public factor acquisition requirement differs")
    capture_evaluation_date = _day(
        run.get("evaluation_date"), "public factor evaluation date"
    )
    if capture_evaluation_date > planning_evaluation_date:
        raise CurrentPlanError(
            "public factor evaluation date exceeds the current planning run"
        )
    if (
        assurance_profile == "RELEASE_QC"
        and capture_evaluation_date != planning_evaluation_date
    ):
        raise CurrentPlanError(
            "release QC requires public factors captured for the planning run"
        )
    run_started = _utc_datetime(
        run.get("run_started_at_utc"), "public factor run start"
    )
    run_ended = _utc_datetime(run.get("run_ended_at_utc"), "public factor run end")
    capture_first = _utc_datetime(
        run.get("capture_window_first_utc"), "public factor first capture"
    )
    capture_last = _utc_datetime(
        run.get("capture_window_last_utc"), "public factor last capture"
    )
    if not run_started <= capture_first <= capture_last <= run_ended:
        raise CurrentPlanError("public factor captures fall outside the current run")
    receipts = _object(
        run.get("receipt_manifest_sha256s"), "public factor receipt hashes"
    )
    if set(receipts) != PUBLIC_FACTOR_RECEIPT_KEYS or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in receipts.values()
    ):
        raise CurrentPlanError("public factor receipt hash set differs")
    run_payload = {
        "evaluation_date": capture_evaluation_date.isoformat(),
        "run_started_at_utc": run.get("run_started_at_utc"),
        "run_ended_at_utc": run.get("run_ended_at_utc"),
        "receipt_manifest_sha256s": receipts,
    }
    if run.get("run_id") != _sha256(_canonical_json(run_payload)):
        raise CurrentPlanError("public factor acquisition run identity differs")

    source_rows = {
        _text(item.get("source_key"), "public factor source key"): item
        for item in _objects(manifest.get("sources"), "public factor sources")
    }
    if (
        len(source_rows) != len(manifest.get("sources", []))
        or set(source_rows) != PUBLIC_FACTOR_RECEIPT_KEYS
    ):
        raise CurrentPlanError("public factor source receipt set differs")
    source_capture_times: dict[str, datetime] = {}
    for key, source in source_rows.items():
        if source.get("source_manifest_sha256") != receipts[key]:
            raise CurrentPlanError(
                f"public factor {key} source receipt identity differs"
            )
        response = _object(source.get("response"), f"public factor {key} response")
        captured_at = _utc_datetime(
            response.get("captured_at_utc"), f"public factor {key} capture"
        )
        if captured_at < run_started or captured_at > run_ended:
            raise CurrentPlanError(
                f"public factor {key} capture falls outside the current run"
            )
        source_capture_times[key] = captured_at
    if capture_first != min(source_capture_times.values()) or capture_last != max(
        source_capture_times.values()
    ):
        raise CurrentPlanError("public factor capture window differs from source rows")

    declared_bundle_id = manifest.get("bundle_id")
    manifest_without_id = dict(manifest)
    manifest_without_id.pop("bundle_id", None)
    if declared_bundle_id != _sha256(_canonical_json(manifest_without_id)):
        raise CurrentPlanError("public factor bundle identity differs")

    outputs = {
        _text(item.get("path"), "public factor output path"): item
        for item in _objects(manifest.get("outputs"), "public factor outputs")
    }
    if len(outputs) != len(manifest.get("outputs", [])):
        raise CurrentPlanError("public factor output paths must be unique")
    admitted_boundaries: dict[str, date] = {}
    output_calendars: dict[str, tuple[date, ...]] = {}
    for key in PUBLIC_FACTOR_INPUT_KEYS:
        path = market_paths[key]
        expected = outputs.get(PUBLIC_FACTOR_OUTPUT_NAMES[key])
        if expected is None:
            raise CurrentPlanError(
                f"public factor manifest has no output for {path.name}"
            )
        content = path.read_bytes()
        if expected.get("sha256") != _sha256(content) or _integer(
            expected.get("bytes"), f"public factor manifest {path.name} size"
        ) != len(content):
            raise CurrentPlanError(
                f"public factor input {path.name} differs from its manifest"
            )
        freshness = _object(
            expected.get("freshness"), f"public factor {path.name} freshness"
        )
        if freshness.get("state") != "CURRENT":
            raise CurrentPlanError(f"public factor {path.name} is not current")
        accepted_end = _day(
            freshness.get("accepted_observation_end"),
            f"public factor {path.name} accepted observation end",
        )
        if expected.get("last_date") != accepted_end.isoformat():
            raise CurrentPlanError(
                f"public factor {path.name} does not end at its admitted boundary"
            )
        headers, rows = _read_csv(path, f"public factor {path.name}")
        dates = tuple(
            _day(row.get("date"), f"public factor {path.name} date") for row in rows
        )
        if dates != tuple(sorted(set(dates))):
            raise CurrentPlanError(
                f"public factor {path.name} dates are not unique and increasing"
            )
        expected_coverage = (
            "PARTIAL" if key == "us_industries" else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        )
        missing_cells = sum(
            not row.get(column, "").strip()
            for row in rows
            for column in headers
            if column != "date"
        )
        if (
            expected.get("coverage") != expected_coverage
            or expected.get("declared_boundary") != "EXACT_DATE_INTERSECTION"
            or expected.get("first_date") != dates[0].isoformat()
            or _integer(
                expected.get("row_count"), f"public factor {path.name} row_count", 1
            )
            != len(rows)
            or _integer(
                expected.get("missing_cells"),
                f"public factor {path.name} missing_cells",
            )
            != missing_cells
        ):
            raise CurrentPlanError(
                f"public factor {path.name} coverage identity differs"
            )
        if key != "us_industries" and missing_cells:
            raise CurrentPlanError(
                f"public factor {path.name} has a missing required cell"
            )
        observed_end = dates[-1]
        if observed_end != accepted_end:
            raise CurrentPlanError(
                f"public factor {path.name} bytes exceed their admitted boundary"
            )
        eligible_month_end = _day(
            freshness.get("eligible_month_end"),
            f"public factor {path.name} eligible month end",
        )
        if (
            eligible_month_end
            != _required_observation_month_end(capture_evaluation_date)
            or freshness.get("eligible_month") != eligible_month_end.strftime("%Y-%m")
            or (accepted_end.year, accepted_end.month)
            != (eligible_month_end.year, eligible_month_end.month)
        ):
            raise CurrentPlanError(
                f"public factor {path.name} admitted publication month differs"
            )
        deadline = _day(
            freshness.get("publication_deadline"),
            f"public factor {path.name} publication deadline",
        )
        if (
            deadline != eligible_month_end + timedelta(days=45)
            or deadline > capture_evaluation_date
        ):
            raise CurrentPlanError(
                f"public factor {path.name} publication deadline has not elapsed"
            )
        admitted_boundaries[key] = accepted_end
        output_calendars[key] = dates
    if output_calendars["us_factors"] != output_calendars["us_industries"]:
        raise CurrentPlanError("public US factor and industry calendars differ")
    europe_dates = output_calendars["europe_factors"]
    expected_europe_dates: list[date] = []
    current = europe_dates[0]
    while current <= europe_dates[-1]:
        if current.weekday() < 5:
            expected_europe_dates.append(current)
        current += timedelta(days=1)
    if europe_dates != tuple(expected_europe_dates):
        raise CurrentPlanError("public Europe factor weekday calendar has a gap")
    capture_age = _age_disposition(
        complete_through=capture_evaluation_date,
        evaluated_on=planning_evaluation_date,
        warning_after_days=0,
        limiting_inputs=("public-factor-acquisition-run",),
    )
    return admitted_boundaries, capture_age


def _resolved_strategies(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    strategies = _objects(plan.get("strategies"), "plan.strategies")
    by_id = {
        _text(item.get("strategy_id"), "strategy.strategy_id"): item
        for item in strategies
    }
    if len(by_id) != len(strategies):
        raise CurrentPlanError("strategy IDs must be unique")
    resolved: list[dict[str, Any]] = []
    for strategy in strategies:
        inherited = strategy.get("inherit_assignments_from")
        if inherited is None:
            assignments = _objects(strategy.get("assignments", []), "assignments")
        else:
            parent = by_id.get(_text(inherited, "inherit_assignments_from"))
            if parent is None or parent.get("inherit_assignments_from") is not None:
                raise CurrentPlanError(
                    "assignment inheritance must reference a direct plan"
                )
            assignments = _objects(parent.get("assignments", []), "assignments")
        accepted = dict(strategy)
        accepted["assignments"] = assignments
        resolved.append(accepted)
    return resolved


def _source_dispositions(
    strategy: Mapping[str, Any], source_keys: Sequence[str]
) -> dict[str, dict[str, str]]:
    strategy_id = _text(strategy.get("strategy_id"), "strategy_id")
    raw = _object(
        strategy.get("source_dispositions"),
        f"{strategy_id}.source_dispositions",
    )
    if set(raw) != set(source_keys):
        missing = sorted(set(source_keys) - set(raw))
        extra = sorted(set(raw) - set(source_keys))
        raise CurrentPlanError(
            f"strategy source disposition keys differ: {strategy_id}; "
            f"missing={missing}, extra={extra}"
        )
    result: dict[str, dict[str, str]] = {}
    expected_fields = {"requirement", "readiness", "reason", "authority"}
    for source_key in source_keys:
        cell = _object(raw[source_key], f"{strategy_id}.{source_key}")
        if set(cell) != expected_fields:
            raise CurrentPlanError(
                f"strategy source disposition fields differ: {strategy_id}.{source_key}"
            )
        result[source_key] = {
            field: _text(cell.get(field), f"{strategy_id}.{source_key}.{field}")
            for field in sorted(expected_fields)
        }
    return result


def _is_accepted_method_scope_non_applicable(
    strategy_id: str, source_key: str, cell: Mapping[str, str]
) -> bool:
    return (
        source_key in METHOD_SCOPE_NON_APPLICABLE_SOURCES.get(strategy_id, frozenset())
        and dict(cell) == METHOD_SCOPE_NON_APPLICABLE_DISPOSITION
    )


def _validate_plan_authority(
    plan: Mapping[str, Any],
    plan_content: bytes,
    financial_mvp_contract: Mapping[str, Any],
) -> None:
    """Bind the operational plan to the product contract's governed bytes."""

    matching = [
        source
        for source in _objects(
            financial_mvp_contract.get("source_segments"),
            "financial MVP source segments",
        )
        if _text(source.get("source_id"), "financial MVP source_id")
        == "current-strategy-registry"
    ]
    if len(matching) != 1:
        raise CurrentPlanError(
            "current strategy plan authority is missing or duplicated"
        )
    authority = matching[0]
    plan_schema = plan.get("schema_version")
    if not isinstance(plan_schema, int) or plan_schema < 1:
        raise CurrentPlanError("plan.schema_version must be a positive integer")
    schema = _object(authority.get("schema"), "current strategy plan schema")
    if (
        authority.get("source_version") != f"decomposition-plan-v{plan_schema}"
        or schema.get("name") != "current-strategy-decomposition-plan"
        or schema.get("version") != str(plan_schema)
        or authority.get("endpoint_or_artifact_selector")
        != "docs/analytics/current-strategy-decomposition-plan.json"
        or authority.get("existence_status") != "EXISTS"
        or authority.get("capture_status") != "CAPTURED_GOVERNED"
        or authority.get("product_requirement") != "REQUIRED"
    ):
        raise CurrentPlanError("current strategy plan authority differs")
    consumers = authority.get("consumers")
    if not isinstance(consumers, list) or "DECOMPOSITION" not in consumers:
        raise CurrentPlanError("current strategy plan authority differs")
    data_classes = authority.get("data_classes")
    admission = _object(authority.get("admission"), "current strategy plan admission")
    facts = admission.get("facts")
    if (
        not isinstance(data_classes, list)
        or "DECOMPOSITION_APPLICABILITY" not in data_classes
        or not isinstance(facts, list)
        or "DECOMPOSITION_APPLICABILITY" not in facts
    ):
        raise CurrentPlanError("current strategy plan applicability authority differs")
    generation = _object(
        authority.get("current_generation"), "current strategy plan generation"
    )
    plan_sha256 = _sha256(plan_content)
    if (
        generation.get("generation_id") != plan_sha256
        or generation.get("sha256") != plan_sha256
    ):
        raise CurrentPlanError("current strategy plan authority digest differs")


def _required_decomposition_surfaces(
    financial_mvp_contract: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return the exact product-required surface set from the #330 authority."""

    matching = [
        outcome
        for outcome in _objects(
            financial_mvp_contract.get("financial_outcomes"),
            "financial MVP outcomes",
        )
        if outcome.get("outcome_id") == "DECOMPOSITION"
    ]
    if len(matching) != 1:
        raise CurrentPlanError("financial MVP decomposition outcome is missing")
    calculation = _object(
        matching[0].get("calculation_contract"),
        "financial MVP decomposition calculation contract",
    )
    raw_surfaces = calculation.get("surfaces")
    if not isinstance(raw_surfaces, list):
        raise CurrentPlanError("financial MVP decomposition surfaces must be an array")
    surfaces = tuple(
        _text(item, "financial MVP decomposition surface") for item in raw_surfaces
    )
    if len(surfaces) != len(set(surfaces)) or set(surfaces) != set(SURFACES):
        raise CurrentPlanError("financial MVP decomposition surfaces differ")
    return tuple(surface for surface in SURFACES if surface in surfaces)


def _reporting_currency(financial_mvp_contract: Mapping[str, Any]) -> str:
    """Read the reporting currency from the governed capital policy.

    The financial-MVP contract deliberately has no top-level ``currency``:
    currency is an owner-selected capital-policy treatment.  Decomposition
    return inputs are labelled with that reporting denomination only after the
    treatment explicitly names it; this does not assert that every raw source
    is itself a net-return series in that denomination.
    """

    matching = [
        outcome
        for outcome in _objects(
            financial_mvp_contract.get("financial_outcomes"),
            "financial MVP outcomes",
        )
        if outcome.get("outcome_id") == "INVESTED_CAPITAL"
    ]
    if len(matching) != 1:
        raise CurrentPlanError("financial MVP invested-capital outcome is missing")
    calculation = _object(
        matching[0].get("calculation_contract"),
        "financial MVP invested-capital calculation contract",
    )
    fx_policy = _object(
        calculation.get("fx_base_currency"),
        "financial MVP fx base currency policy",
    )
    selected = _text(
        fx_policy.get("selected_treatment"),
        "financial MVP fx base currency selected treatment",
    )
    if not selected.startswith("USD_REPORTING_CURRENCY"):
        raise CurrentPlanError(
            "financial MVP fx base currency policy does not declare USD reporting currency"
        )
    return "USD"


def _declared_factor_anchor_periods_per_year(
    base_spec: Mapping[str, Any],
) -> dict[str, int]:
    """Collect the factor-anchor annualization declared by the base spec."""

    declared: dict[str, set[int]] = {}
    for index, model in enumerate(
        _objects(base_spec.get("models"), "base specification models")
    ):
        series_id = _text(model.get("series_id"), f"base model {index} series_id")
        periods_per_year = _integer(
            model.get("periods_per_year"),
            f"base model {index} periods_per_year",
            1,
        )
        declared.setdefault(series_id, set()).add(periods_per_year)
    ambiguous = sorted(
        series_id for series_id, values in declared.items() if len(values) != 1
    )
    if ambiguous:
        raise CurrentPlanError(
            f"base factor methodology has conflicting periods_per_year: {ambiguous}"
        )
    return {series_id: next(iter(values)) for series_id, values in declared.items()}


def _validate_strategy_perimeter(
    strategies: Sequence[Mapping[str, Any]],
    financial_mvp_contract: Mapping[str, Any],
    source_keys: Sequence[str],
) -> None:
    contract_rows = _objects(
        financial_mvp_contract.get("strategies"),
        "financial MVP strategies",
    )
    contract_by_id = {
        _text(row.get("strategy_id"), "financial MVP strategy_id"): row
        for row in contract_rows
    }
    if len(contract_by_id) != len(contract_rows):
        raise CurrentPlanError("financial MVP strategy IDs are duplicated")
    plan_by_id = {
        _text(row.get("strategy_id"), "plan strategy_id"): row for row in strategies
    }
    if len(plan_by_id) != len(strategies):
        raise CurrentPlanError("plan strategy IDs are duplicated")
    if set(plan_by_id) != set(contract_by_id):
        missing = sorted(set(contract_by_id) - set(plan_by_id))
        extra = sorted(set(plan_by_id) - set(contract_by_id))
        raise CurrentPlanError(
            f"plan strategy perimeter differs; missing={missing}, extra={extra}"
        )

    for strategy_id, contract_row in sorted(contract_by_id.items()):
        plan_row = plan_by_id[strategy_id]
        contract_series_id = contract_row.get("history_series_id")
        if contract_series_id is not None and _text(
            plan_row.get("series_id"), f"{strategy_id}.series_id"
        ) != _text(contract_series_id, f"{strategy_id}.history_series_id"):
            raise CurrentPlanError(
                f"plan history series differs from the financial MVP contract: "
                f"{strategy_id}"
            )
        product_requirement = _text(
            contract_row.get("product_requirement"),
            f"{strategy_id}.product_requirement",
        )
        outcome_dispositions = _object(
            contract_row.get("outcome_dispositions"),
            f"{strategy_id}.outcome_dispositions",
        )
        decomposition_disposition = _text(
            outcome_dispositions.get("DECOMPOSITION"),
            f"{strategy_id}.DECOMPOSITION",
        )
        assignments = _objects(plan_row.get("assignments", []), "assignments")
        assignment_keys = {
            _text(assignment.get("source_key"), f"{strategy_id}.assignment.source_key")
            for assignment in assignments
        }
        if len(assignment_keys) != len(assignments):
            raise CurrentPlanError(
                f"strategy assignment source keys are duplicated: {strategy_id}"
            )
        if assignment_keys - set(source_keys):
            raise CurrentPlanError(
                f"strategy assignment source key is unknown: {strategy_id}"
            )
        cells = _source_dispositions(plan_row, source_keys)
        if (
            product_requirement == "REQUIRED"
            and decomposition_disposition == "REQUIRED"
        ):
            ready_sources: set[str] = set()
            for source_key, cell in cells.items():
                if _is_accepted_method_scope_non_applicable(
                    strategy_id, source_key, cell
                ):
                    if source_key in assignment_keys:
                        raise CurrentPlanError(
                            "method-scope non-applicable cell has an assignment: "
                            f"{strategy_id}.{source_key}"
                        )
                    continue
                if cell["requirement"] != "REQUIRED":
                    raise CurrentPlanError(
                        "required decomposition cell requirement differs: "
                        f"{strategy_id}.{source_key}"
                    )
                if cell["readiness"] == "READY_TO_MODEL":
                    if not cell["reason"].startswith("APPLICABLE_"):
                        raise CurrentPlanError(
                            "ready decomposition cell reason differs: "
                            f"{strategy_id}.{source_key}"
                        )
                    ready_sources.add(source_key)
                elif cell["readiness"] == "REQUIRED_NOT_READY":
                    reason = cell["reason"]
                    if not reason.startswith("REQUIRED_NOT_READY_") or any(
                        token in reason
                        for token in ("DROPPED", "OUT_OF_SCOPE", "NOT_APPLICABLE")
                    ):
                        raise CurrentPlanError(
                            "required-not-ready decomposition reason differs: "
                            f"{strategy_id}.{source_key}"
                        )
                else:
                    raise CurrentPlanError(
                        "required decomposition cell readiness differs: "
                        f"{strategy_id}.{source_key}"
                    )
            if assignment_keys != ready_sources:
                raise CurrentPlanError(
                    "strategy assignments differ from source disposition authority: "
                    f"{strategy_id}"
                )
        elif (
            product_requirement == "EXCLUDED"
            and decomposition_disposition == "EXCLUDED_BY_ACCEPTED_POLICY"
        ):
            if assignments or any(
                cell["requirement"] != "NOT_APPLICABLE_OWNER_APPROVED"
                or cell["readiness"] != "NOT_APPLICABLE_OWNER_APPROVED"
                or cell["reason"] != "NOT_APPLICABLE_OWNER_APPROVED"
                for cell in cells.values()
            ):
                raise CurrentPlanError(
                    "owner-approved decomposition non-applicability differs: "
                    f"{strategy_id}"
                )
        else:
            raise CurrentPlanError(
                "financial MVP decomposition disposition is unsupported: "
                f"{strategy_id}={decomposition_disposition}"
            )


def _factor_sets(
    assignment: Mapping[str, Any], candidates: Sequence[str], name: str
) -> tuple[tuple[str, ...], ...]:
    sets: tuple[tuple[str, ...], ...]
    if assignment.get("model_policy") == "EACH_CANDIDATE_ONE_FACTOR":
        sets = tuple((candidate,) for candidate in candidates)
    else:
        raw_sets = assignment.get("factor_sets")
        if not isinstance(raw_sets, list):
            raise CurrentPlanError(f"{name}.factor_sets must be an array")
        sets = tuple(
            tuple(_text(item, f"{name}.factor_sets[]") for item in raw)
            for raw in raw_sets
            if isinstance(raw, list)
        )
        if len(sets) != len(raw_sets):
            raise CurrentPlanError(f"{name}.factor_sets entries must be arrays")
    if not sets or any(not item or len(item) != len(set(item)) for item in sets):
        raise CurrentPlanError(f"{name} factor sets must be unique and non-empty")
    if any(set(item) - set(candidates) for item in sets):
        raise CurrentPlanError(f"{name} factor set references an ineligible candidate")
    modeled = set().union(*(set(item) for item in sets))
    raw_screened = assignment.get("screened_out_candidates", [])
    if not isinstance(raw_screened, list):
        raise CurrentPlanError(f"{name}.screened_out_candidates must be an array")
    screened = {
        _text(item, f"{name}.screened_out_candidates[]") for item in raw_screened
    }
    if len(screened) != len(raw_screened) or modeled & screened:
        raise CurrentPlanError(f"{name} modeled and screened candidates must differ")
    if modeled | screened != set(candidates):
        raise CurrentPlanError(
            f"{name} must disposition every candidate in its universe"
        )
    return sets


def _complete_dates(
    rows: Sequence[Mapping[str, str]],
    required: Sequence[str],
    source_name: str,
    *,
    allowed_missing_after_start: frozenset[str] = frozenset(),
    always_required: frozenset[str] = frozenset(),
) -> tuple[date, ...]:
    seen: set[date] = set()
    result: list[date] = []
    previous: date | None = None
    completeness_started = False
    for index, row in enumerate(rows, 2):
        observed = _day(row.get("date"), f"{source_name} line {index} date")
        if observed in seen:
            raise CurrentPlanError(f"{source_name} has a duplicate date")
        if previous is not None and observed <= previous:
            raise CurrentPlanError(f"{source_name} dates are not increasing")
        seen.add(observed)
        previous = observed
        missing = [column for column in required if not row.get(column, "").strip()]
        if missing:
            missing_columns = frozenset(missing)
            if missing_columns & always_required:
                raise CurrentPlanError(
                    f"{source_name} has a missing unconditionally required observation: "
                    f"{observed.isoformat()}"
                )
            if completeness_started and not missing_columns.issubset(
                allowed_missing_after_start
            ):
                raise CurrentPlanError(
                    f"{source_name} has an incomplete required observation after "
                    f"its completeness boundary: {observed.isoformat()}"
                )
            continue
        completeness_started = True
        result.append(observed)
    if not result:
        raise CurrentPlanError(f"{source_name} has no complete observations")
    return tuple(result)


def _target_dates(rows: Sequence[Mapping[str, str]]) -> dict[str, tuple[date, ...]]:
    grouped: dict[str, list[date]] = {}
    seen: set[tuple[str, date]] = set()
    for index, row in enumerate(rows, 2):
        series_id = _text(row.get("series_id"), f"targets line {index} series_id")
        observed = _day(row.get("date"), f"targets line {index} date")
        key = (series_id, observed)
        if key in seen:
            raise CurrentPlanError("targets contain a duplicate series/date")
        seen.add(key)
        grouped.setdefault(series_id, []).append(observed)
    return {key: tuple(sorted(value)) for key, value in grouped.items()}


def _copy_definition(
    definition: Mapping[str, Any], relative_path: str
) -> dict[str, object]:
    return {
        "artifact_id": _portable(definition.get("artifact_id"), "artifact_id"),
        "expected_sha256": _text(definition.get("expected_sha256"), "sha256"),
        "expected_size_bytes": _integer(definition.get("expected_size_bytes"), "size"),
        "relative_path": relative_path,
        "schema_name": _text(definition.get("schema_name"), "schema_name"),
        "schema_version": _text(definition.get("schema_version"), "schema_version"),
    }


def _provenance_definition(
    artifact_id: str,
    relative_path: str,
    content: bytes,
    schema_name: str,
    schema_version: str = "1",
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "expected_sha256": _sha256(content),
        "expected_size_bytes": len(content),
        "relative_path": relative_path,
        "schema_name": schema_name,
        "schema_version": schema_version,
    }


def build_bundle(
    *,
    plan_path: Path,
    catalog_path: Path,
    financial_mvp_contract_path: Path,
    layered_history_generation_path: Path,
    base_spec_path: Path,
    base_source_root: Path,
    market_paths: Mapping[str, Path],
    public_factor_manifest_path: Path,
    style_manifest_path: Path,
    sector_manifest_path: Path,
    evaluation_date: str,
    evidence_mode: str = "LIVE_ONLY",
    assurance_profile: str = "DEVELOPMENT",
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build a deterministic current-strategy planning bundle without writing."""

    plan, plan_content = _load_json(plan_path, "current strategy plan")
    catalog, catalog_content = _load_json(catalog_path, "source catalog")
    base_spec, base_spec_content = _load_json(base_spec_path, "base specification")
    if plan.get("schema_version") != 3:
        raise CurrentPlanError("current strategy plan schema is unsupported")
    evaluated_on = _day(evaluation_date, "evaluation_date")
    if assurance_profile not in ASSURANCE_PROFILES:
        raise CurrentPlanError("assurance_profile is unsupported")
    if evidence_mode not in {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
        raise CurrentPlanError("evidence_mode is unsupported")
    if set(market_paths) != set(INPUT_KEYS):
        raise CurrentPlanError("market input keys differ from the closed contract")
    legacy_target = _object(base_spec.get("target_source"), "base.target_source")
    _portable(legacy_target.get("relative_path"), "base.target_source.relative_path")
    for path in market_paths.values():
        if path.is_symlink() or not path.is_file():
            raise CurrentPlanError("market input must be a regular file")
    catalog_sha256 = _sha256(catalog_content)
    public_factor_boundaries, public_factor_capture_age = (
        _verify_public_factor_manifest(
            public_factor_manifest_path,
            market_paths,
            catalog_sha256,
            evaluated_on,
            assurance_profile,
        )
    )
    _verify_market_manifest(
        market_paths["style_returns"], style_manifest_path, catalog_sha256
    )
    _verify_market_manifest(
        market_paths["sector_returns"], sector_manifest_path, catalog_sha256
    )
    strategies = _resolved_strategies(plan)
    declared_periods_per_year = _declared_factor_anchor_periods_per_year(base_spec)
    layered_history = _layered_history_targets(
        generation_root=layered_history_generation_path,
        contract_path=financial_mvp_contract_path,
        strategies=strategies,
        evidence_mode=evidence_mode,
        evaluated_on=evaluated_on,
        assurance_profile=assurance_profile,
    )
    target_rows = cast(list[dict[str, str]], layered_history["rows"])
    series_interval_policies = cast(
        dict[str, dict[str, str]], layered_history["series_interval_policies"]
    )
    targets_by_series = _target_dates(target_rows)

    outputs: dict[str, bytes] = {
        "inputs/governance/current-strategy-decomposition-plan.json": plan_content,
        "inputs/governance/decomposition-source-catalog.json": catalog_content,
        "inputs/governance/base-decomposition-spec.json": base_spec_content,
    }
    history_contents = cast(dict[str, bytes], layered_history["contents"])
    try:
        financial_mvp_contract = json.loads(
            history_contents["financial-mvp-contract.json"]
        )
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentPlanError(
            "verified financial MVP contract is unavailable"
        ) from exc
    required_surfaces = _required_decomposition_surfaces(financial_mvp_contract)
    reporting_currency = _reporting_currency(financial_mvp_contract)
    contract_sources = {
        _text(source.get("source_id"), "financial MVP source_id"): source
        for source in _objects(
            financial_mvp_contract.get("source_segments"),
            "financial MVP source segments",
        )
    }
    if len(contract_sources) != len(financial_mvp_contract.get("source_segments", [])):
        raise CurrentPlanError("financial MVP source IDs are duplicated")
    source_plans = {
        key: _object(value, f"plan.sources.{key}")
        for key, value in _object(plan.get("sources"), "plan.sources").items()
    }
    _validate_strategy_perimeter(
        strategies, financial_mvp_contract, tuple(source_plans)
    )
    for name, content in history_contents.items():
        outputs[f"inputs/history/{name}"] = content
    generated_provenance: list[dict[str, object]] = []
    for index, definition in enumerate(
        _objects(base_spec.get("provenance_sources", []), "base.provenance_sources")
    ):
        path = _verify_definition(
            base_source_root, definition, f"base provenance {index}"
        )
        relative = f"inputs/base/provenance/{index:02d}-{path.name}"
        outputs[relative] = path.read_bytes()
        generated_provenance.append(_copy_definition(definition, relative))

    factor_manifest_content = public_factor_manifest_path.read_bytes()
    style_manifest_content = style_manifest_path.read_bytes()
    sector_manifest_content = sector_manifest_path.read_bytes()
    outputs["inputs/governance/public-factor-source-manifest.json"] = (
        factor_manifest_content
    )
    outputs["inputs/governance/style-source-manifest.json"] = style_manifest_content
    outputs["inputs/governance/sector-source-manifest.json"] = sector_manifest_content
    generated_provenance.extend(
        (
            _provenance_definition(
                "plan/current-strategy-decomposition-plan.json",
                "inputs/governance/current-strategy-decomposition-plan.json",
                plan_content,
                "current-strategy-decomposition-plan",
                str(plan["schema_version"]),
            ),
            _provenance_definition(
                "catalog/decomposition-source-catalog.json",
                "inputs/governance/decomposition-source-catalog.json",
                catalog_content,
                "decomposition-source-catalog",
            ),
            _provenance_definition(
                "market/public-factor-source-manifest.json",
                "inputs/governance/public-factor-source-manifest.json",
                factor_manifest_content,
                "portfolio-public-decomposition-source-manifest",
            ),
            _provenance_definition(
                "market/style-source-manifest.json",
                "inputs/governance/style-source-manifest.json",
                style_manifest_content,
                "portfolio-public-style-source-manifest",
            ),
            _provenance_definition(
                "market/sector-source-manifest.json",
                "inputs/governance/sector-source-manifest.json",
                sector_manifest_content,
                "portfolio-public-sector-source-manifest",
            ),
        )
    )
    history_schema_definitions = cast(
        dict[str, dict[str, str]], layered_history["schema_definitions"]
    )
    generated_provenance.extend(
        _provenance_definition(
            f"history/{name}",
            f"inputs/history/{name}",
            content,
            history_schema_definitions[name]["schema_name"],
            history_schema_definitions[name]["schema_version"],
        )
        for name, content in sorted(history_contents.items())
    )

    market_rows: dict[str, list[dict[str, str]]] = {}
    market_headers: dict[str, tuple[str, ...]] = {}
    for key, path in market_paths.items():
        relative = f"inputs/market/{key}.csv"
        content = path.read_bytes()
        outputs[relative] = content
        headers, rows = _read_csv(path, f"market input {key}")
        market_headers[key] = headers
        market_rows[key] = rows
    style_dates = tuple(
        _day(row.get("date"), f"style_returns line {index} date")
        for index, row in enumerate(market_rows["style_returns"], 2)
    )
    sector_dates = tuple(
        _day(row.get("date"), f"sector_returns line {index} date")
        for index, row in enumerate(market_rows["sector_returns"], 2)
    )
    if not style_dates or not sector_dates:
        raise CurrentPlanError("style and sector market calendars must be non-empty")
    shared_start = max(style_dates[0], sector_dates[0])
    shared_end = min(style_dates[-1], sector_dates[-1])
    if shared_start > shared_end:
        raise CurrentPlanError("style and sector market calendars do not overlap")
    shared_style_dates = tuple(
        observed for observed in style_dates if shared_start <= observed <= shared_end
    )
    shared_sector_dates = tuple(
        observed for observed in sector_dates if shared_start <= observed <= shared_end
    )
    if shared_style_dates != shared_sector_dates:
        raise CurrentPlanError("style and sector shared market calendars differ")

    universes = _catalog_universes(catalog)
    split = _object(plan.get("split_policy"), "plan.split_policy")
    holdout_fraction = _decimal(
        split.get("holdout_fraction"), "split.holdout_fraction", Decimal("0.01")
    )
    if holdout_fraction >= 1:
        raise CurrentPlanError("split.holdout_fraction must be below one")
    minimum_holdout = _integer(
        split.get("minimum_holdout_observations"), "split.minimum_holdout", 1
    )
    minimum_per_factor = _integer(
        split.get("minimum_training_observations_per_factor"),
        "split.minimum_per_factor",
        1,
    )
    cv_folds = _integer(split.get("cv_folds"), "split.cv_folds", 2)
    tolerance = _text(split.get("parsimony_tolerance"), "split.tolerance")
    ridge = split.get("ridge_candidates")
    if not isinstance(ridge, list) or not ridge:
        raise CurrentPlanError("split.ridge_candidates must be a non-empty array")
    normalized_ridge = tuple(
        _decimal(candidate, f"split.ridge_candidates[{index}]", Decimal("0"))
        for index, candidate in enumerate(ridge)
    )
    if len(normalized_ridge) != len(set(normalized_ridge)):
        raise CurrentPlanError("split.ridge_candidates are numerically duplicated")
    dynamic = _object(split.get("dynamic"), "split.dynamic")
    if set(dynamic) != {"constraint", "smoothness_candidates", "state_ridge"}:
        raise CurrentPlanError("split.dynamic fields differ")
    dynamic_constraint = _text(dynamic.get("constraint"), "split.dynamic.constraint")
    if dynamic_constraint != "UNCONSTRAINED":
        raise CurrentPlanError("split.dynamic.constraint must be UNCONSTRAINED")
    dynamic_state_ridge = _text(dynamic.get("state_ridge"), "split.dynamic.state_ridge")
    _decimal(dynamic_state_ridge, "split.dynamic.state_ridge", Decimal("0"))
    raw_smoothness = dynamic.get("smoothness_candidates")
    if not isinstance(raw_smoothness, list) or not raw_smoothness:
        raise CurrentPlanError(
            "split.dynamic.smoothness_candidates must be a non-empty array"
        )
    dynamic_smoothness = tuple(
        _text(value, f"split.dynamic.smoothness_candidates[{index}]")
        for index, value in enumerate(raw_smoothness)
    )
    normalized_smoothness = tuple(
        _decimal(
            candidate,
            f"split.dynamic.smoothness_candidates[{index}]",
            Decimal("0.000000000001"),
        )
        for index, candidate in enumerate(dynamic_smoothness)
    )
    if len(normalized_smoothness) != len(set(normalized_smoothness)):
        raise CurrentPlanError(
            "split.dynamic.smoothness_candidates are numerically duplicated"
        )

    assignments_by_source: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    candidate_dispositions: list[dict[str, object]] = []
    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "strategy_id")
        series_id = _text(strategy.get("series_id"), "series_id")
        if strategy.get("assignments") and series_id not in targets_by_series:
            raise CurrentPlanError(
                f"planned series is absent from targets: {series_id}"
            )
        assignments = {
            _text(item.get("source_key"), "assignment.source_key"): item
            for item in _objects(strategy.get("assignments", []), "assignments")
        }
        if len(assignments) != len(strategy.get("assignments", [])):
            raise CurrentPlanError("a strategy may have only one assignment per source")
        source_dispositions = _source_dispositions(strategy, tuple(source_plans))
        for source_key, source_plan in source_plans.items():
            universe_id = _text(source_plan.get("universe_id"), "source.universe_id")
            universe = universes.get(universe_id)
            if universe is None:
                raise CurrentPlanError(f"unknown catalog universe {universe_id}")
            candidates = _candidates(universe, universe_id)
            assignment = assignments.get(source_key)
            cell = source_dispositions[source_key]
            if assignment is None:
                disposition = cell["readiness"]
            else:
                sets = _factor_sets(
                    assignment, candidates, f"{strategy_id}.{source_key}"
                )
                assignment["resolved_factor_sets"] = sets
                assignments_by_source.setdefault(source_key, []).append(
                    (strategy, assignment)
                )
                disposition = "PLANNED_MODEL_CANDIDATE"
            for candidate in candidates:
                for surface in required_surfaces:
                    surface_readiness = cell["readiness"]
                    surface_reason = cell["reason"]
                    surface_disposition = (
                        "SCREENED_OUT_PREDECLARED_DIMENSION_CONTROL"
                        if assignment is not None
                        and candidate
                        in set(assignment.get("screened_out_candidates", []))
                        else disposition
                    )
                    candidate_dispositions.append(
                        {
                            "candidate_id": candidate,
                            "authority": cell["authority"],
                            "disposition": surface_disposition,
                            "lens": _text(source_plan.get("lens"), "source.lens"),
                            "readiness": surface_readiness,
                            "reason": surface_reason,
                            "requirement": cell["requirement"],
                            "source_key": source_key,
                            "strategy_id": strategy_id,
                            "surface": surface,
                        }
                    )

    specs: list[dict[str, object]] = []
    market_source_ages: dict[str, dict[str, object]] = {}
    for source_key, source_assignments in sorted(assignments_by_source.items()):
        source_plan = source_plans[source_key]
        lens = _text(source_plan.get("lens"), "source.lens")
        if lens not in LENSES:
            raise CurrentPlanError(f"source lens is unsupported: {lens}")
        input_key = _text(source_plan.get("input_key"), "source.input_key")
        if input_key not in INPUT_KEYS:
            raise CurrentPlanError("source input_key is unsupported")
        universe_id = _text(source_plan.get("universe_id"), "source.universe_id")
        universe = universes[universe_id]
        candidate_records = _candidate_records(universe, universe_id)
        candidates = tuple(candidate_records)
        policy = _text(source_plan.get("column_policy"), "source.column_policy")
        columns = {candidate: _column(candidate, policy) for candidate in candidates}
        risk_free = _text(source_plan.get("risk_free_column"), "source.risk_free")
        required = (risk_free, *columns.values())
        if not set(("date", *required)).issubset(market_headers[input_key]):
            raise CurrentPlanError(f"market input {input_key} lacks planned columns")
        complete_dates = _complete_dates(
            market_rows[input_key],
            required,
            f"market input {input_key}",
            allowed_missing_after_start=(
                frozenset(columns.values())
                if input_key == "us_industries"
                else frozenset()
            ),
            always_required=(
                frozenset((risk_free,)) if input_key == "us_industries" else frozenset()
            ),
        )
        contract_source_id = MARKET_CONTRACT_SOURCE_IDS.get(input_key)
        if contract_source_id is not None:
            contract_source = contract_sources.get(contract_source_id)
            if contract_source is None:
                raise CurrentPlanError(
                    f"financial MVP source is absent: {contract_source_id}"
                )
            source_freshness = _object(
                contract_source.get("freshness"),
                f"financial MVP {contract_source_id} freshness",
            )
            source_max_age_days = _integer(
                source_freshness.get("max_age_days"),
                f"financial MVP {contract_source_id} max_age_days",
            )
            factor_source_age = {
                **_age_disposition(
                    complete_through=complete_dates[-1],
                    evaluated_on=evaluated_on,
                    warning_after_days=source_max_age_days,
                    limiting_inputs=(f"factor:{input_key}",),
                ),
                "input_key": input_key,
                "source_id": contract_source_id,
            }
            prior_age = market_source_ages.get(input_key)
            if prior_age is not None and prior_age != factor_source_age:
                raise CurrentPlanError(
                    f"market source age differs across plans: {input_key}"
                )
            market_source_ages[input_key] = factor_source_age
            if (
                assurance_profile == "RELEASE_QC"
                and contract_source.get("readiness") != "READY"
            ):
                raise CurrentPlanError(
                    f"release QC market source is not READY: {contract_source_id}"
                )
        assigned_series = {
            _text(strategy.get("series_id"), "series_id")
            for strategy, _assignment in source_assignments
        }
        missing_periods = sorted(assigned_series - set(declared_periods_per_year))
        if missing_periods:
            raise CurrentPlanError(
                f"base factor methodology lacks periods_per_year for: {missing_periods}"
            )
        complete_date_set = set(complete_dates)
        if input_key in public_factor_boundaries:
            if (
                source_plan.get("freshness_policy_id")
                != PUBLIC_FACTOR_FRESHNESS_POLICY["policy_id"]
            ):
                raise CurrentPlanError(
                    f"source {source_key} public factor freshness policy differs"
                )
            admitted_boundary = public_factor_boundaries[input_key]
            if market_rows[input_key][-1].get("date") != admitted_boundary.isoformat():
                raise CurrentPlanError(
                    f"source {source_key} does not end at the admitted boundary"
                )
            if admitted_boundary not in complete_date_set:
                raise CurrentPlanError(
                    f"source {source_key} is incomplete at the admitted boundary"
                )
        maximum_factor_staleness_days = 0

        model_date_cache: dict[tuple[str, ...], tuple[date, ...]] = {}
        model_boundaries: dict[str, date] = {}
        model_calculation_boundaries: dict[str, dict[str, object]] = {}
        model_blueprints: list[dict[str, object]] = []
        for strategy, assignment in source_assignments:
            strategy_id = _text(strategy.get("strategy_id"), "strategy_id")
            series_id = _text(strategy.get("series_id"), "series_id")
            factor_sets = cast(
                tuple[tuple[str, ...], ...], assignment["resolved_factor_sets"]
            )
            constraint = _text(assignment.get("constraint"), "assignment.constraint")
            for index, factors in enumerate(factor_sets, 1):
                model_id = f"{strategy_id}-{source_key}-{index:02d}"
                factor_columns = tuple(columns[factor] for factor in factors)
                model_required = (risk_free, *factor_columns)
                model_dates = model_date_cache.get(model_required)
                if model_dates is None:
                    model_dates = _complete_dates(
                        market_rows[input_key],
                        model_required,
                        f"market input {input_key} model {model_id}",
                        allowed_missing_after_start=(
                            frozenset(factor_columns)
                            if input_key == "us_industries"
                            else frozenset()
                        ),
                        always_required=(
                            frozenset((risk_free,))
                            if input_key == "us_industries"
                            else frozenset()
                        ),
                    )
                    model_date_cache[model_required] = model_dates
                model_as_of = _latest_common_complete_date(
                    [set(targets_by_series[series_id]), set(model_dates)],
                    evaluated_on=evaluated_on,
                    name=f"model {model_id}",
                )
                aligned = sorted(
                    set(targets_by_series[series_id]).intersection(model_dates)
                )
                aligned = [observed for observed in aligned if observed <= model_as_of]
                proposed_test = int(
                    (Decimal(len(aligned)) * holdout_fraction).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                test_count = max(minimum_holdout, proposed_test)
                if len(aligned) - test_count < 3:
                    raise CurrentPlanError(
                        f"{series_id} has insufficient aligned history for {model_id}"
                    )
                train_end = aligned[-test_count - 1]
                test_start = aligned[-test_count]
                limiting_inputs = []
                if targets_by_series[series_id][-1] == model_as_of:
                    limiting_inputs.append(f"target:{series_id}")
                if model_dates[-1] == model_as_of:
                    limiting_inputs.append(
                        f"factor:{input_key}:{','.join(factors)}:{risk_free}"
                    )
                if not limiting_inputs:
                    limiting_inputs = [
                        f"target:{series_id}",
                        f"factor:{input_key}:{','.join(factors)}:{risk_free}",
                    ]
                model_boundaries[model_id] = model_as_of
                model_calculation_boundaries[model_id] = _age_disposition(
                    complete_through=model_as_of,
                    evaluated_on=evaluated_on,
                    warning_after_days=0,
                    limiting_inputs=limiting_inputs,
                )
                model_blueprints.append(
                    {
                        "as_of_date": model_as_of.isoformat(),
                        "constraint": constraint,
                        "cost_coverage": "INCOMPLETE",
                        "cv_folds": cv_folds,
                        "factor_ids": list(factors),
                        "minimum_test_observations": minimum_holdout,
                        "minimum_train_observations": max(
                            3, minimum_per_factor * len(factors)
                        ),
                        "model_id": model_id,
                        "parsimony_tolerance": tolerance,
                        "periods_per_year": declared_periods_per_year[series_id],
                        "ridge_candidates": list(ridge),
                        "series_id": series_id,
                        "test_start": test_start.isoformat(),
                        "train_end": train_end.isoformat(),
                    }
                )
        if not model_blueprints:
            raise CurrentPlanError(f"source {source_key} has no planned models")
        analysis_as_of = max(model_boundaries.values())
        oldest_model_boundary = min(model_boundaries.values())
        calculation_age = _age_disposition(
            complete_through=oldest_model_boundary,
            evaluated_on=evaluated_on,
            warning_after_days=0,
            limiting_inputs=sorted(
                f"model:{model_id}"
                for model_id, boundary in model_boundaries.items()
                if boundary == oldest_model_boundary
            ),
        )
        factor_input_start = min(
            min(observed) for observed in model_date_cache.values()
        )
        projected_factor_rows = [
            [row["date"], *(row[column] for column in required)]
            for row in market_rows[input_key]
            if (observed := _day(row["date"], f"market input {input_key} date"))
            >= factor_input_start
            and observed <= analysis_as_of
            and row[risk_free].strip()
        ]
        projected_factor_content = _csv_bytes(
            ("date", *required), projected_factor_rows
        )
        factor_relative = f"inputs/lenses/{source_key}-factors.csv"
        outputs[factor_relative] = projected_factor_content
        projected_target_rows = [
            [row[header] for header in TARGET_HEADERS]
            for row in target_rows
            if row["series_id"] in assigned_series
            and _day(row["date"], "target date") <= analysis_as_of
        ]
        projected_target_content = _csv_bytes(TARGET_HEADERS, projected_target_rows)
        target_relative = f"inputs/lenses/{source_key}-targets.csv"
        outputs[target_relative] = projected_target_content
        layered_composition = cast(
            dict[str, dict[str, object]], layered_history["composition"]
        )
        series_composition: dict[str, object] = {}
        for series_id in sorted(assigned_series):
            value = layered_composition.get(series_id)
            if value is None:
                raise CurrentPlanError(f"layered composition is absent for {series_id}")
            series_composition[series_id] = dict(value)
        generated_target = _provenance_definition(
            f"target/{source_key}.csv",
            target_relative,
            projected_target_content,
            "portfolio-decomposition-target-series",
        )
        factor_definitions: list[dict[str, object]] = []
        for candidate_id in candidates:
            candidate_record = candidate_records[candidate_id]
            candidate_column = columns[candidate_id]
            candidate_effective_dates = [
                _day(row["date"], f"market input {input_key} date")
                for row in market_rows[input_key]
                if row[candidate_column].strip()
                and row[risk_free].strip()
                and factor_input_start
                <= _day(row["date"], f"market input {input_key} date")
                <= analysis_as_of
            ]
            if not candidate_effective_dates:
                raise CurrentPlanError(
                    f"source {source_key} candidate has no effective observations: "
                    f"{candidate_id}"
                )
            source_id = str(
                candidate_record.get("source_id") or "UNAVAILABLE_NOT_DECLARED"
            ).strip()
            return_source_id = str(
                candidate_record.get("return_source_id") or source_id
            ).strip()
            raw_orthogonal = candidate_record.get("raw_orthogonal", False)
            if not isinstance(raw_orthogonal, bool):
                raise CurrentPlanError(
                    f"{universe_id}.{candidate_id}.raw_orthogonal must be boolean"
                )
            factor_definitions.append(
                {
                    "column": candidate_column,
                    "economic_family": str(
                        candidate_record.get("economic_family") or "UNCLASSIFIED"
                    ).strip(),
                    "effective_end": candidate_effective_dates[-1].isoformat(),
                    "effective_start": candidate_effective_dates[0].isoformat(),
                    "factor_id": candidate_id,
                    "raw_orthogonal": raw_orthogonal,
                    "redundancy_policy": (
                        "TRAIN_ONLY_NEAR_DUPLICATE_VIF_CONDITION_NUMBER_AND_"
                        "PARSIMONY_GATES"
                    ),
                    "return_source_id": return_source_id,
                    "return_type": _text(source_plan.get("return_type"), "return_type"),
                    "selection_rationale": _proxy_rationale(
                        candidate_id, candidate_record, lens
                    ),
                    "source_id": source_id,
                }
            )
        for surface in required_surfaces:
            surface_slug = surface.lower().replace("_", "-")
            surface_models: list[dict[str, object]] = []
            surface_boundaries: dict[str, dict[str, object]] = {}
            for blueprint in model_blueprints:
                base_model_id = cast(str, blueprint["model_id"])
                model_id = f"{base_model_id}-{surface_slug}"
                model = {**blueprint, "model_id": model_id}
                if surface == "STATIC":
                    model["estimator"] = "STATIC_RBSA"
                else:
                    model.update(
                        {
                            "constraint": dynamic_constraint,
                            "estimator": "DYNAMIC_FLS",
                            "ridge_candidates": [dynamic_state_ridge],
                            "smoothness_candidates": list(dynamic_smoothness),
                        }
                    )
                surface_models.append(model)
                surface_boundaries[model_id] = model_calculation_boundaries[
                    base_model_id
                ]
            spec = {
                "analysis_lens": lens,
                "as_of_date": analysis_as_of.isoformat(),
                "evidence_mode": evidence_mode,
                "factor_source": {
                    "artifact_id": f"market/{source_key}.csv",
                    "date_column": "date",
                    "expected_sha256": _sha256(projected_factor_content),
                    "expected_size_bytes": len(projected_factor_content),
                    "factors": factor_definitions,
                    "interval_policy": {
                        "available_through_anchor": analysis_as_of.isoformat(),
                        "currency": reporting_currency,
                        "finality": "COMPLETE_SELECTED_SNAPSHOT",
                        "native_calendar": "SOURCE_LABELLED_DAILY",
                        "native_label_convention": "SOURCE_PUBLISHED_DAILY_LABEL",
                        "return_basis": "SOURCE_PUBLISHED",
                        "source_vintage": _sha256(projected_factor_content),
                    },
                    "relative_path": factor_relative,
                    "risk_free_column": risk_free,
                    "schema_name": f"portfolio-{source_key}-returns",
                    "schema_version": "1",
                },
                "maximum_factor_staleness_days": maximum_factor_staleness_days,
                "models": surface_models,
                "provenance_sources": generated_provenance,
                "schema_version": 1,
                "series_composition": series_composition,
                "target_source": generated_target,
            }
            target_policy = cast(dict[str, object], spec["target_source"])
            target_policy["interval_policy"] = {
                "currency": reporting_currency,
                "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
                "series_policies": {
                    series_id: policy
                    for series_id, policy in sorted(series_interval_policies.items())
                    if series_id in assigned_series
                },
            }
            relative = (
                f"specs/{source_key}.json"
                if surface == "STATIC"
                else f"specs/{source_key}-{surface_slug}.json"
            )
            content = _canonical_json(spec)
            outputs[relative] = content
            spec_id = f"{source_key}:{surface}"
            specs.append(
                {
                    "analysis_lens": spec["analysis_lens"],
                    "as_of_date": spec["as_of_date"],
                    "data_boundary": calculation_age,
                    "model_count": len(surface_models),
                    "model_calculation_boundaries": surface_boundaries,
                    "path": relative,
                    "sha256": _sha256(content),
                    "size_bytes": len(content),
                    "source_key": source_key,
                    "spec_id": spec_id,
                    "surface": surface,
                }
            )

    _validate_plan_authority(plan, plan_content, financial_mvp_contract)
    disposition_result = {
        "schema_version": 2,
        "candidate_dispositions": sorted(
            candidate_dispositions,
            key=lambda item: (
                cast(str, item["strategy_id"]),
                cast(str, item["source_key"]),
                cast(str, item["surface"]),
                cast(str, item["candidate_id"]),
            ),
        ),
        "policy": "EVERY_CURRENT_STRATEGY_BY_EVERY_PLANNED_CANDIDATE_UNIVERSE",
    }
    disposition_content = _canonical_json(disposition_result)
    outputs["candidate-dispositions.json"] = disposition_content
    manifest: dict[str, object] = {
        "schema_version": 2,
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": BUILDER_VERSION,
        "assurance_profile": assurance_profile,
        "evaluation_date": evaluated_on.isoformat(),
        "layered_history": layered_history["identity"],
        "data_age": {
            "market_sources": [
                market_source_ages[key] for key in sorted(market_source_ages)
            ],
            "public_factor_capture": public_factor_capture_age,
            "spec_calculation_boundaries": {
                cast(str, row["spec_id"]): row["data_boundary"] for row in specs
            },
        },
        "inputs": [
            {
                "path": path,
                "sha256": _sha256(content),
                "size_bytes": len(content),
            }
            for path, content in sorted(outputs.items())
            if path.startswith("inputs/")
        ],
        "outputs": [
            {
                "path": path,
                "sha256": _sha256(content),
                "size_bytes": len(content),
            }
            for path, content in sorted(outputs.items())
            if not path.startswith("inputs/")
        ],
        "specs": specs,
        "release_qc_eligibility": (
            "REQUIRES_DOWNSTREAM_RELEASE_QC"
            if assurance_profile == "RELEASE_QC"
            else "NOT_ELIGIBLE_NON_RELEASE_PROFILE"
        ),
        "policies": {
            "calendar": "EXACT_DATE_COMPLETE_CANDIDATE_UNIVERSE_NO_FILL",
            "holdout": {
                "fraction": str(holdout_fraction),
                "minimum_observations": minimum_holdout,
                "selection": "TRAILING_SEALED_HOLDOUT",
            },
            "model_dimension": {
                "absolute_minimum_training_observations": 3,
                "dynamic_constraint": dynamic_constraint,
                "dynamic_smoothness_candidates": list(dynamic_smoothness),
                "dynamic_state_ridge": dynamic_state_ridge,
                "minimum_training_observations_per_factor": minimum_per_factor,
                "required_surfaces": list(required_surfaces),
            },
            "publication": "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE",
            "strategy_evidence_mode": evidence_mode,
            "data_age": "WARN_WITH_COMPLETE_THROUGH_DATE_AGE_DAYS_THRESHOLD_AND_LIMITING_INPUTS",
        },
    }
    manifest["bundle_id"] = _sha256(_canonical_json(manifest))
    return outputs, manifest


def write_bundle(output_root: Path, **kwargs: Any) -> dict[str, object]:
    outputs, manifest = build_bundle(**kwargs)
    bundle_id = _text(manifest.get("bundle_id"), "bundle_id")
    destination = output_root.resolve() / bundle_id
    expected = {**outputs, "plan-manifest.json": _canonical_json(manifest)}
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise CurrentPlanError("existing bundle is not a regular directory")
        if any(path.is_symlink() for path in destination.rglob("*")):
            raise CurrentPlanError("existing bundle may not contain symlinks")
        actual = {
            path.relative_to(destination).as_posix(): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }
        if actual != expected:
            raise CurrentPlanError("existing plan bundle differs from exact rebuild")
    else:
        destination.mkdir(parents=True)
        try:
            for relative, content in expected.items():
                path = destination / PurePosixPath(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except Exception:
            shutil.rmtree(destination)
            raise
    return {
        "bundle_id": bundle_id,
        "destination": str(destination),
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--financial-mvp-contract", type=Path, required=True)
    parser.add_argument("--layered-history-generation", type=Path, required=True)
    parser.add_argument("--base-spec", type=Path, required=True)
    parser.add_argument("--base-source-root", type=Path, required=True)
    parser.add_argument("--us-factors", type=Path, required=True)
    parser.add_argument("--europe-factors", type=Path, required=True)
    parser.add_argument("--style-returns", type=Path, required=True)
    parser.add_argument("--sector-returns", type=Path, required=True)
    parser.add_argument("--us-industries", type=Path, required=True)
    parser.add_argument("--public-factor-manifest", type=Path, required=True)
    parser.add_argument("--style-manifest", type=Path, required=True)
    parser.add_argument("--sector-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-date", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--evidence-mode",
        choices=("LIVE_ONLY", "LIVE_PLUS_BACKTEST"),
        default="LIVE_ONLY",
    )
    parser.add_argument(
        "--assurance-profile",
        choices=ASSURANCE_PROFILES,
        default="DEVELOPMENT",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_bundle(
        output_root=args.output_root,
        plan_path=args.plan,
        catalog_path=args.catalog,
        financial_mvp_contract_path=args.financial_mvp_contract,
        layered_history_generation_path=args.layered_history_generation,
        base_spec_path=args.base_spec,
        base_source_root=args.base_source_root,
        market_paths={
            "us_factors": args.us_factors,
            "europe_factors": args.europe_factors,
            "style_returns": args.style_returns,
            "sector_returns": args.sector_returns,
            "us_industries": args.us_industries,
        },
        public_factor_manifest_path=args.public_factor_manifest,
        style_manifest_path=args.style_manifest,
        sector_manifest_path=args.sector_manifest,
        evaluation_date=args.evaluation_date,
        evidence_mode=args.evidence_mode,
        assurance_profile=args.assurance_profile,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

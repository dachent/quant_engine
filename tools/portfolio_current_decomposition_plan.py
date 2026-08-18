"""Materialize current-strategy factor/style/industry decomposition inputs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Mapping, Sequence, cast


BUILDER_VERSION = "0.4.0"
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
PUBLIC_FACTOR_RECEIPT_KEYS = {
    "us_factors",
    "us_momentum",
    "europe_factors",
    "europe_momentum",
    "us_industries",
}
LENSES = (
    "ECONOMIC_FACTOR",
    "ETF_STYLE",
    "RETURN_INDUSTRY",
    "RETURN_SECTOR",
)
INPUT_KEYS = (
    "us_factors",
    "europe_factors",
    "style_returns",
    "sector_returns",
    "us_industries",
)
TARGET_HEADERS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
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
    evaluation_date: date,
) -> dict[str, date]:
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
    evaluated_on = _day(run.get("evaluation_date"), "public factor evaluation date")
    if evaluated_on != evaluation_date:
        raise CurrentPlanError(
            "public factor evaluation date differs from the current planning run"
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
        "evaluation_date": evaluated_on.isoformat(),
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
    for key in PUBLIC_FACTOR_INPUT_KEYS:
        path = market_paths[key]
        expected = outputs.get(path.name)
        if expected is None:
            raise CurrentPlanError(f"public factor manifest has no output for {path.name}")
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
        _headers, rows = _read_csv(path, f"public factor {path.name}")
        observed_end = _day(rows[-1].get("date"), f"public factor {path.name} end")
        if observed_end != accepted_end:
            raise CurrentPlanError(
                f"public factor {path.name} bytes exceed their admitted boundary"
            )
        eligible_month_end = _day(
            freshness.get("eligible_month_end"),
            f"public factor {path.name} eligible month end",
        )
        if (
            eligible_month_end != _required_observation_month_end(evaluated_on)
            or freshness.get("eligible_month")
            != eligible_month_end.strftime("%Y-%m")
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
        if deadline != eligible_month_end + timedelta(days=45) or deadline > evaluated_on:
            raise CurrentPlanError(
                f"public factor {path.name} publication deadline has not elapsed"
            )
        admitted_boundaries[key] = accepted_end
    return admitted_boundaries


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
    rows: Sequence[Mapping[str, str]], required: Sequence[str], source_name: str
) -> tuple[date, ...]:
    seen: set[date] = set()
    result: list[date] = []
    for index, row in enumerate(rows, 2):
        observed = _day(row.get("date"), f"{source_name} line {index} date")
        if observed in seen:
            raise CurrentPlanError(f"{source_name} has a duplicate date")
        seen.add(observed)
        if all(row.get(column, "").strip() for column in required):
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
    artifact_id: str, relative_path: str, content: bytes, schema_name: str
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "expected_sha256": _sha256(content),
        "expected_size_bytes": len(content),
        "relative_path": relative_path,
        "schema_name": schema_name,
        "schema_version": "1",
    }


def build_bundle(
    *,
    plan_path: Path,
    catalog_path: Path,
    base_spec_path: Path,
    base_source_root: Path,
    market_paths: Mapping[str, Path],
    public_factor_manifest_path: Path,
    style_manifest_path: Path,
    sector_manifest_path: Path,
    evaluation_date: str,
    evidence_mode: str = "LIVE_ONLY",
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build a deterministic current-strategy planning bundle without writing."""

    plan, plan_content = _load_json(plan_path, "current strategy plan")
    catalog, catalog_content = _load_json(catalog_path, "source catalog")
    base_spec, base_spec_content = _load_json(base_spec_path, "base specification")
    if plan.get("schema_version") != 1:
        raise CurrentPlanError("current strategy plan schema is unsupported")
    evaluated_on = _day(evaluation_date, "evaluation_date")
    if evidence_mode not in {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}:
        raise CurrentPlanError("evidence_mode is unsupported")
    if set(market_paths) != set(INPUT_KEYS):
        raise CurrentPlanError("market input keys differ from the closed contract")
    for path in market_paths.values():
        if path.is_symlink() or not path.is_file():
            raise CurrentPlanError("market input must be a regular file")
    catalog_sha256 = _sha256(catalog_content)
    public_factor_boundaries = _verify_public_factor_manifest(
        public_factor_manifest_path, market_paths, catalog_sha256, evaluated_on
    )
    _verify_market_manifest(
        market_paths["style_returns"], style_manifest_path, catalog_sha256
    )
    _verify_market_manifest(
        market_paths["sector_returns"], sector_manifest_path, catalog_sha256
    )

    target_definition = _object(base_spec.get("target_source"), "base.target_source")
    target_path = _verify_definition(base_source_root, target_definition, "base target")
    target_headers, target_rows = _read_csv(target_path, "base targets")
    if target_headers != TARGET_HEADERS:
        raise CurrentPlanError(
            "base target headers differ from the decomposition contract"
        )
    targets_by_series = _target_dates(target_rows)
    if any(days[-1] > evaluated_on for days in targets_by_series.values()):
        raise CurrentPlanError("target observations extend beyond the evaluation date")

    outputs: dict[str, bytes] = {
        "inputs/governance/current-strategy-decomposition-plan.json": plan_content,
        "inputs/governance/decomposition-source-catalog.json": catalog_content,
        "inputs/governance/base-decomposition-spec.json": base_spec_content,
        "inputs/base/targets.csv": target_path.read_bytes(),
    }
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

    market_rows: dict[str, list[dict[str, str]]] = {}
    market_headers: dict[str, tuple[str, ...]] = {}
    for key, path in market_paths.items():
        relative = f"inputs/market/{key}.csv"
        content = path.read_bytes()
        outputs[relative] = content
        headers, rows = _read_csv(path, f"market input {key}")
        market_headers[key] = headers
        market_rows[key] = rows

    universes = _catalog_universes(catalog)
    source_plans = {
        key: _object(value, f"plan.sources.{key}")
        for key, value in _object(plan.get("sources"), "plan.sources").items()
    }
    strategies = _resolved_strategies(plan)
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

    assignments_by_source: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    candidate_dispositions: list[dict[str, object]] = []
    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "strategy_id")
        series_id = _text(strategy.get("series_id"), "series_id")
        if series_id not in targets_by_series and strategy_id not in {
            "composer-holy-grail"
        }:
            raise CurrentPlanError(
                f"planned series is absent from targets: {series_id}"
            )
        assignments = {
            _text(item.get("source_key"), "assignment.source_key"): item
            for item in _objects(strategy.get("assignments", []), "assignments")
        }
        if len(assignments) != len(strategy.get("assignments", [])):
            raise CurrentPlanError("a strategy may have only one assignment per source")
        for source_key, source_plan in source_plans.items():
            universe_id = _text(source_plan.get("universe_id"), "source.universe_id")
            universe = universes.get(universe_id)
            if universe is None:
                raise CurrentPlanError(f"unknown catalog universe {universe_id}")
            candidates = _candidates(universe, universe_id)
            assignment = assignments.get(source_key)
            if assignment is None:
                disposition = _text(
                    strategy.get("default_disposition"), "default_disposition"
                )
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
                candidate_dispositions.append(
                    {
                        "candidate_id": candidate,
                        "disposition": (
                            "SCREENED_OUT_PREDECLARED_DIMENSION_CONTROL"
                            if assignment is not None
                            and candidate
                            in set(assignment.get("screened_out_candidates", []))
                            else disposition
                        ),
                        "lens": _text(source_plan.get("lens"), "source.lens"),
                        "source_key": source_key,
                        "strategy_id": strategy_id,
                    }
                )

    specs: list[dict[str, object]] = []
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
            market_rows[input_key], required, f"market input {input_key}"
        )
        assigned_series = {
            _text(strategy.get("series_id"), "series_id")
            for strategy, _assignment in source_assignments
        }
        target_last = max(
            targets_by_series[series_id][-1] for series_id in assigned_series
        )
        analysis_as_of = target_last
        complete_date_set = set(complete_dates)
        if input_key in public_factor_boundaries:
            if source_plan.get("freshness_policy_id") != PUBLIC_FACTOR_FRESHNESS_POLICY[
                "policy_id"
            ]:
                raise CurrentPlanError(
                    f"source {source_key} public factor freshness policy differs"
                )
            admitted_boundary = public_factor_boundaries[input_key]
            if market_rows[input_key][-1].get("date") != admitted_boundary.isoformat():
                raise CurrentPlanError(
                    f"source {source_key} does not end at the admitted boundary"
                )
            if analysis_as_of >= admitted_boundary:
                if admitted_boundary not in complete_date_set:
                    raise CurrentPlanError(
                        f"source {source_key} is incomplete at the admitted boundary"
                    )
                factor_as_of = admitted_boundary
            else:
                factor_as_of = min(complete_dates[-1], analysis_as_of)
            maximum_factor_staleness_days = max(
                0, (analysis_as_of - admitted_boundary).days
            )
        else:
            factor_as_of = min(complete_dates[-1], analysis_as_of)
            maximum_factor_staleness_days = _integer(
                source_plan.get("maximum_factor_staleness_days", 10),
                f"source {source_key} maximum_factor_staleness_days",
            )
        projected_factor_rows = [
            [row["date"], *(row[column] for column in required)]
            for row in market_rows[input_key]
            if (observed := _day(row["date"], f"market input {input_key} date"))
            in complete_date_set
            and observed <= factor_as_of
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
        base_composition = base_spec.get("series_composition", {})
        if not isinstance(base_composition, dict):
            raise CurrentPlanError("base series_composition must be an object")
        series_composition: dict[str, object] = {}
        for series_id in sorted(assigned_series):
            accepted_rows = [
                row
                for row in target_rows
                if row["series_id"] == series_id
                and _day(row["date"], "target date") <= analysis_as_of
            ]
            if evidence_mode == "LIVE_PLUS_BACKTEST":
                value = base_composition.get(series_id)
                if not isinstance(value, dict):
                    raise CurrentPlanError(
                        f"hybrid composition is absent for {series_id}"
                    )
                series_composition[series_id] = dict(value)
            else:
                live_rows = [
                    row for row in accepted_rows if row["evidence_layer"] != "BACKTEST"
                ]
                series_composition[series_id] = {
                    "backtest_observations_used": 0,
                    "evidence_mode": "LIVE_ONLY",
                    "live_evidence_layer": (
                        live_rows[0]["evidence_layer"] if live_rows else None
                    ),
                    "live_inception": live_rows[0]["date"] if live_rows else None,
                    "live_observations": len(live_rows),
                    "series_id": series_id,
                    "status": "AVAILABLE_LIVE_ONLY" if live_rows else "UNAVAILABLE",
                    "strategy_version": None,
                }
        generated_target = _provenance_definition(
            f"target/{source_key}.csv",
            target_relative,
            projected_target_content,
            "portfolio-decomposition-target-series",
        )
        models: list[dict[str, object]] = []
        for strategy, assignment in source_assignments:
            series_id = _text(strategy.get("series_id"), "series_id")
            aligned = sorted(
                set(targets_by_series[series_id]).intersection(complete_dates)
            )
            aligned = [observed for observed in aligned if observed <= analysis_as_of]
            proposed_test = int(
                (Decimal(len(aligned)) * holdout_fraction).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            test_count = max(minimum_holdout, proposed_test)
            if len(aligned) - test_count < 3:
                raise CurrentPlanError(f"{series_id} has insufficient aligned history")
            train_end = aligned[-test_count - 1]
            test_start = aligned[-test_count]
            factor_sets = cast(
                tuple[tuple[str, ...], ...], assignment["resolved_factor_sets"]
            )
            constraint = _text(assignment.get("constraint"), "assignment.constraint")
            for index, factors in enumerate(factor_sets, 1):
                models.append(
                    {
                        "constraint": constraint,
                        "cost_coverage": "INCOMPLETE",
                        "cv_folds": cv_folds,
                        "factor_ids": list(factors),
                        "minimum_test_observations": minimum_holdout,
                        "minimum_train_observations": max(
                            3, minimum_per_factor * len(factors)
                        ),
                        "model_id": (
                            f"{strategy['strategy_id']}-{source_key}-{index:02d}"
                        ),
                        "parsimony_tolerance": tolerance,
                        "periods_per_year": 252,
                        "ridge_candidates": list(ridge),
                        "series_id": series_id,
                        "test_start": test_start.isoformat(),
                        "train_end": train_end.isoformat(),
                    }
                )
        factor_definitions: list[dict[str, object]] = []
        for candidate_id in candidates:
            candidate_record = candidate_records[candidate_id]
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
                    "column": columns[candidate_id],
                    "economic_family": str(
                        candidate_record.get("economic_family") or "UNCLASSIFIED"
                    ).strip(),
                    "effective_end": str(projected_factor_rows[-1][0]),
                    "effective_start": str(projected_factor_rows[0][0]),
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
                "relative_path": factor_relative,
                "risk_free_column": risk_free,
                "schema_name": f"portfolio-{source_key}-returns",
                "schema_version": "1",
            },
            "maximum_factor_staleness_days": maximum_factor_staleness_days,
            "models": models,
            "provenance_sources": generated_provenance,
            "schema_version": 1,
            "series_composition": series_composition,
            "target_source": generated_target,
        }
        relative = f"specs/{source_key}.json"
        content = _canonical_json(spec)
        outputs[relative] = content
        specs.append(
            {
                "analysis_lens": spec["analysis_lens"],
                "as_of_date": spec["as_of_date"],
                "model_count": len(models),
                "path": relative,
                "sha256": _sha256(content),
                "size_bytes": len(content),
                "source_key": source_key,
            }
        )

    disposition_result = {
        "schema_version": 1,
        "candidate_dispositions": sorted(
            candidate_dispositions,
            key=lambda item: (
                cast(str, item["strategy_id"]),
                cast(str, item["source_key"]),
                cast(str, item["candidate_id"]),
            ),
        ),
        "policy": "EVERY_CURRENT_STRATEGY_BY_EVERY_PLANNED_CANDIDATE_UNIVERSE",
    }
    disposition_content = _canonical_json(disposition_result)
    outputs["candidate-dispositions.json"] = disposition_content
    manifest: dict[str, object] = {
        "schema_version": 1,
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": BUILDER_VERSION,
        "evaluation_date": evaluated_on.isoformat(),
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
        "policies": {
            "calendar": "EXACT_DATE_COMPLETE_CANDIDATE_UNIVERSE_NO_FILL",
            "holdout": "LAST_20_PERCENT_MINIMUM_20_OBSERVATIONS",
            "model_dimension": "MINIMUM_20_TRAINING_OBSERVATIONS_PER_FACTOR",
            "publication": "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE",
            "strategy_evidence_mode": evidence_mode,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_bundle(
        output_root=args.output_root,
        plan_path=args.plan,
        catalog_path=args.catalog,
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
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_current_decomposition_plan.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_current_decomposition_plan", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load current decomposition planner")
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

ENGINE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_decomposition.py"
ENGINE_SPEC = importlib.util.spec_from_file_location(
    "portfolio_decomposition_for_current_plan", ENGINE_PATH
)
if ENGINE_SPEC is None or ENGINE_SPEC.loader is None:
    raise RuntimeError("unable to load portfolio decomposition engine")
engine = importlib.util.module_from_spec(ENGINE_SPEC)
sys.modules[ENGINE_SPEC.name] = engine
ENGINE_SPEC.loader.exec_module(engine)

HISTORY_TEST_PATH = (
    REPOSITORY_ROOT
    / "tests"
    / "portfolio_analytics"
    / ("test_portfolio_layered_history.py")
)
HISTORY_TEST_SPEC = importlib.util.spec_from_file_location(
    "portfolio_layered_history_fixture_for_current_plan", HISTORY_TEST_PATH
)
if HISTORY_TEST_SPEC is None or HISTORY_TEST_SPEC.loader is None:
    raise RuntimeError("unable to load layered history fixture")
history_tests = importlib.util.module_from_spec(HISTORY_TEST_SPEC)
sys.modules[HISTORY_TEST_SPEC.name] = history_tests
HISTORY_TEST_SPEC.loader.exec_module(history_tests)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> Path:
    path.write_bytes(_canonical(value))
    return path


def _release_builder_module(name: str) -> Any:
    release_path = REPOSITORY_ROOT / "tools" / "portfolio_release_builder.py"
    release_spec = importlib.util.spec_from_file_location(name, release_path)
    if release_spec is None or release_spec.loader is None:
        raise RuntimeError("unable to load portfolio release builder")
    release_builder = importlib.util.module_from_spec(release_spec)
    sys.modules[release_spec.name] = release_builder
    release_spec.loader.exec_module(release_builder)
    return release_builder


def _definition(path: Path, artifact_id: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_size_bytes": len(content),
        "relative_path": path.name,
        "schema_name": "fixture",
        "schema_version": "1",
    }


def _weekdays(count: int) -> list[date]:
    result: list[date] = []
    current = date(2026, 1, 2)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _weekdays_through(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _month_end(value: date) -> date:
    next_month = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def _write_market(path: Path, columns: tuple[str, ...], days: list[date]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("date", *columns))
        for index, observed in enumerate(days, 1):
            writer.writerow(
                (
                    observed.isoformat(),
                    *(
                        f"{index / (position + 100000):.12f}"
                        for position, _ in enumerate(columns)
                    ),
                )
            )


def _public_factor_manifest(
    catalog_sha256: str,
    market_paths: dict[str, Path],
    evaluation_date: date,
) -> dict[str, object]:
    run_started = f"{evaluation_date.isoformat()}T11:55:00Z"
    run_ended = f"{evaluation_date.isoformat()}T12:05:00Z"
    capture_time = f"{evaluation_date.isoformat()}T12:00:00Z"
    receipts = {
        "us_factors": "1" * 64,
        "us_momentum": "2" * 64,
        "europe_factors": "3" * 64,
        "europe_momentum": "4" * 64,
        "us_industries": "5" * 64,
    }
    run_payload = {
        "evaluation_date": evaluation_date.isoformat(),
        "run_started_at_utc": run_started,
        "run_ended_at_utc": run_ended,
        "receipt_manifest_sha256s": receipts,
    }
    outputs = []
    for key in planner.PUBLIC_FACTOR_INPUT_KEYS:
        path = market_paths[key]
        rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
        accepted_end = date.fromisoformat(rows[-1]["date"])
        eligible_month_end = _month_end(accepted_end)
        content = path.read_bytes()
        missing_cells = sum(
            not value.strip()
            for row in rows
            for column, value in row.items()
            if column != "date"
        )
        outputs.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "coverage": (
                    "PARTIAL"
                    if key == "us_industries"
                    else "COMPLETE_WITHIN_DECLARED_BOUNDARY"
                ),
                "declared_boundary": "EXACT_DATE_INTERSECTION",
                "first_date": rows[0]["date"],
                "last_date": accepted_end.isoformat(),
                "row_count": len(rows),
                "missing_cells": missing_cells,
                "freshness": {
                    "state": "CURRENT",
                    "eligible_month": accepted_end.strftime("%Y-%m"),
                    "eligible_month_end": eligible_month_end.isoformat(),
                    "publication_deadline": (
                        eligible_month_end + timedelta(days=45)
                    ).isoformat(),
                    "provider_available_end": accepted_end.isoformat(),
                    "accepted_observation_end": accepted_end.isoformat(),
                },
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 2,
        "builder": planner.PUBLIC_FACTOR_BUILDER,
        "builder_version": planner.PUBLIC_FACTOR_BUILDER_VERSION,
        "catalog": {"sha256": catalog_sha256},
        "acquisition_run": {
            "run_id": hashlib.sha256(_canonical(run_payload)).hexdigest(),
            "evaluation_date": evaluation_date.isoformat(),
            "run_started_at_utc": run_started,
            "run_ended_at_utc": run_ended,
            "acquisition_requirement": "EVERY_SOURCE_CAPTURED_WITHIN_CURRENT_RUN",
            "capture_window_first_utc": capture_time,
            "capture_window_last_utc": capture_time,
            "receipt_manifest_sha256s": receipts,
        },
        "freshness_policy": planner.PUBLIC_FACTOR_FRESHNESS_POLICY,
        "sources": [
            {
                "source_key": key,
                "source_manifest_sha256": receipts[key],
                "response": {"captured_at_utc": capture_time},
            }
            for key in sorted(receipts)
        ],
        "outputs": outputs,
    }
    manifest["bundle_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    return manifest


def _reidentify_public_manifest(manifest: dict[str, Any]) -> None:
    manifest.pop("bundle_id", None)
    manifest["bundle_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()


def _bind_fixture_source_dispositions(plan: dict[str, Any]) -> None:
    source_keys = tuple(plan["sources"])
    owner_excluded = {"composer-holy-grail", "echotrade-growth-pivots"}
    for strategy in plan["strategies"]:
        if strategy["strategy_id"] in owner_excluded:
            strategy["assignments"] = []
            strategy["source_dispositions"] = {
                source_key: {
                    "requirement": "NOT_APPLICABLE_OWNER_APPROVED",
                    "readiness": "NOT_APPLICABLE_OWNER_APPROVED",
                    "reason": "NOT_APPLICABLE_OWNER_APPROVED",
                    "authority": "OWNER_302_SYNTHETIC_FIXTURE_BINDING",
                }
                for source_key in source_keys
            }
            continue
        assigned = {
            assignment["source_key"] for assignment in strategy.get("assignments", [])
        }
        strategy["source_dispositions"] = {
            source_key: {
                "requirement": "REQUIRED",
                "readiness": (
                    "READY_TO_MODEL" if source_key in assigned else "REQUIRED_NOT_READY"
                ),
                "reason": (
                    "APPLICABLE_SYNTHETIC_FIXTURE_ASSIGNMENT"
                    if source_key in assigned
                    else "REQUIRED_NOT_READY_SYNTHETIC_FIXTURE"
                ),
                "authority": "SYNTHETIC_FIXTURE",
            }
            for source_key in source_keys
        }


def _fixture(
    tmp_path: Path,
    plan_mutator: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    days = _weekdays(80)
    history_fixture = history_tests._history_fixture(tmp_path / "layered-source")
    base_root = tmp_path / "base"
    base_root.mkdir()
    targets = base_root / "targets.csv"
    with targets.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(planner.TARGET_HEADERS)
        for series in ("strategy-us", "strategy-style"):
            for index, observed in enumerate(days, 1):
                writer.writerow(
                    (
                        series,
                        observed.isoformat(),
                        f"{index / 100000:.12f}",
                        "ACCOUNT_LIVE",
                        "NET",
                        "TOTAL",
                        "",
                    )
                )
    base_spec = _write_json(
        tmp_path / "base-spec.json",
        {
            "schema_version": 1,
            "target_source": _definition(targets, "target-fixture"),
            "provenance_sources": [],
        },
    )
    catalog = _write_json(
        tmp_path / "catalog.json",
        {
            "universes": [
                {
                    "universe_id": "economic",
                    "candidates": [
                        {"candidate_id": "MKT_RF"},
                        {"candidate_id": "SMB"},
                    ],
                },
                {
                    "universe_id": "style",
                    "candidates": [
                        {"candidate_id": "VALUE"},
                        {"candidate_id": "GROWTH"},
                    ],
                },
                {
                    "universe_id": "sector",
                    "candidates": [
                        {"candidate_id": "TECH"},
                        {"candidate_id": "HEALTH"},
                    ],
                },
            ]
        },
    )
    plan_document = {
        "schema_version": 3,
        "split_policy": {
            "holdout_fraction": "0.20",
            "minimum_holdout_observations": 1,
            "minimum_training_observations_per_factor": 1,
            "cv_folds": 3,
            "parsimony_tolerance": "0.01",
            "ridge_candidates": ["0", "0.0001"],
            "dynamic": {
                "constraint": "UNCONSTRAINED",
                "state_ridge": "0.0000000001",
                "smoothness_candidates": ["0.00000001", "0.000001"],
            },
        },
        "sources": {
            "economic": {
                "lens": "ECONOMIC_FACTOR",
                "universe_id": "economic",
                "input_key": "us_factors",
                "freshness_policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1",
                "risk_free_column": "rf",
                "return_type": "EXCESS",
                "column_policy": "LOWER_CANDIDATE_ID",
            },
            "style": {
                "lens": "ETF_STYLE",
                "universe_id": "style",
                "input_key": "style_returns",
                "risk_free_column": "BIL",
                "return_type": "TOTAL",
                "column_policy": "CANDIDATE_ID",
            },
            "sector": {
                "lens": "RETURN_SECTOR",
                "universe_id": "sector",
                "input_key": "sector_returns",
                "risk_free_column": "BIL",
                "return_type": "TOTAL",
                "column_policy": "CANDIDATE_ID",
            },
        },
        "strategies": [
            {
                "strategy_id": "qsi",
                "series_id": "QSI",
                "assignments": [
                    {
                        "source_key": "economic",
                        "constraint": "UNCONSTRAINED",
                        "factor_sets": [["MKT_RF"], ["MKT_RF", "SMB"]],
                    },
                    {
                        "source_key": "sector",
                        "constraint": "UNCONSTRAINED",
                        "model_policy": "EACH_CANDIDATE_ONE_FACTOR",
                    },
                ],
            },
            {
                "strategy_id": "tt-portfolio",
                "series_id": "TT Portfolio",
                "assignments": [
                    {
                        "source_key": "style",
                        "constraint": "LONG_ONLY_SUM_TO_ONE",
                        "factor_sets": [["VALUE"], ["VALUE", "GROWTH"]],
                    }
                ],
            },
        ],
    }
    contract_document = json.loads(
        history_fixture["contract_path"].read_text(encoding="utf-8")
    )
    planned_strategy_ids = {row["strategy_id"] for row in plan_document["strategies"]}
    for contract_strategy in contract_document["strategies"]:
        strategy_id = contract_strategy["strategy_id"]
        if strategy_id in planned_strategy_ids:
            continue
        plan_document["strategies"].append(
            {
                "strategy_id": strategy_id,
                "series_id": contract_strategy["history_series_id"]
                or f"UNRESOLVED::{strategy_id}",
                "assignments": [],
            }
        )
    if plan_mutator is not None:
        plan_mutator(plan_document)
    _bind_fixture_source_dispositions(plan_document)
    plan = _write_json(tmp_path / "plan.json", plan_document)
    plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    registry = next(
        source
        for source in contract_document["source_segments"]
        if source["source_id"] == "current-strategy-registry"
    )
    registry.update(
        source_version="decomposition-plan-v3",
        data_classes=[
            "STRATEGY_VERSION",
            "DECOMPOSITION_APPLICABILITY",
            "DECOMPOSITION_SURFACE_POLICY",
        ],
        endpoint_or_artifact_selector=(
            "docs/analytics/current-strategy-decomposition-plan.json"
        ),
        schema={"name": "current-strategy-decomposition-plan", "version": "3"},
        current_generation={
            "generation_id": plan_sha256,
            "sha256": plan_sha256,
            "as_of": "2026-08-18",
        },
    )
    registry["admission"] = {
        "transform": "portfolio-current-product-strategy-census",
        "version": "3",
        "facts": [
            "STRATEGY_ID",
            "STRATEGY_ARCHETYPE",
            "STRATEGY_VERSION_REQUIREMENT",
            "DECOMPOSITION_APPLICABILITY",
            "DECOMPOSITION_SURFACE_POLICY",
        ],
    }
    _write_json(history_fixture["contract_path"], contract_document)
    history_artifacts, history_manifest = history_tests._build(history_fixture)
    history_generation = history_tests.write_layered_history_generation(
        tmp_path / "layered-generations", history_artifacts, history_manifest
    )
    market_paths = {
        "us_factors": tmp_path / "us.csv",
        "europe_factors": tmp_path / "eu.csv",
        "style_returns": tmp_path / "style.csv",
        "sector_returns": tmp_path / "sector.csv",
        "us_industries": tmp_path / "industries.csv",
    }
    _write_market(market_paths["us_factors"], ("rf", "mkt_rf", "smb"), days)
    _write_market(market_paths["europe_factors"], ("rf", "unused"), days)
    _write_market(market_paths["style_returns"], ("BIL", "VALUE", "GROWTH"), days)
    _write_market(market_paths["sector_returns"], ("BIL", "TECH", "HEALTH"), days)
    _write_market(market_paths["us_industries"], ("rf", "unused"), days)
    catalog_sha256 = hashlib.sha256(catalog.read_bytes()).hexdigest()
    public_end = days[-1]
    public_evaluation_date = _month_end(public_end) + timedelta(days=45)
    factor_manifest = _write_json(
        tmp_path / "factor-manifest.json",
        _public_factor_manifest(catalog_sha256, market_paths, public_evaluation_date),
    )
    style_content = market_paths["style_returns"].read_bytes()
    style_manifest = _write_json(
        tmp_path / "style-manifest.json",
        {
            "catalog": {"sha256": catalog_sha256},
            "output": {
                "path": market_paths["style_returns"].name,
                "sha256": hashlib.sha256(style_content).hexdigest(),
                "size_bytes": len(style_content),
            },
        },
    )
    sector_content = market_paths["sector_returns"].read_bytes()
    sector_manifest = _write_json(
        tmp_path / "sector-manifest.json",
        {
            "catalog": {"sha256": catalog_sha256},
            "output": {
                "path": market_paths["sector_returns"].name,
                "sha256": hashlib.sha256(sector_content).hexdigest(),
                "size_bytes": len(sector_content),
            },
        },
    )
    return {
        "plan_path": plan,
        "catalog_path": catalog,
        "financial_mvp_contract_path": history_fixture["contract_path"],
        "layered_history_generation_path": history_generation,
        "base_spec_path": base_spec,
        "base_source_root": base_root,
        "market_paths": market_paths,
        "public_factor_manifest_path": factor_manifest,
        "style_manifest_path": style_manifest,
        "sector_manifest_path": sector_manifest,
        "evaluation_date": public_evaluation_date.isoformat(),
        "plan_document": plan_document,
    }


def test_build_covers_candidates_and_emits_separate_lens_specs(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )

    assert manifest["builder_version"] == "0.17.0"
    assert manifest["schema_version"] == 2
    assert manifest["policies"]["publication"] == "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE"
    assert manifest["policies"]["holdout"] == {
        "fraction": "0.20",
        "minimum_observations": 1,
        "selection": "TRAILING_SEALED_HOLDOUT",
    }
    assert manifest["policies"]["model_dimension"] == {
        "absolute_minimum_training_observations": 3,
        "dynamic_constraint": "UNCONSTRAINED",
        "dynamic_smoothness_candidates": ["0.00000001", "0.000001"],
        "dynamic_state_ridge": "0.0000000001",
        "minimum_training_observations_per_factor": 1,
        "required_surfaces": ["STATIC", "DYNAMIC_FLS"],
    }
    assert manifest["evaluation_date"] == inputs["evaluation_date"]
    assert manifest["layered_history"]["mode"] == "LIVE_ONLY"
    assert manifest["layered_history"]["generation_id"] == (
        inputs["layered_history_generation_path"].name
    )
    assert "inputs/base/targets.csv" not in outputs
    selected_targets = list(
        csv.DictReader(
            outputs["inputs/history/selected-targets.csv"].decode("utf-8").splitlines()
        )
    )
    assert {row["series_id"] for row in selected_targets} == {"QSI", "TT Portfolio"}
    assert {row["evidence_layer"] for row in selected_targets} == {"ACCOUNT_LIVE"}
    assert {item["analysis_lens"] for item in manifest["specs"]} == {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_SECTOR",
    }
    assert {item["surface"] for item in manifest["specs"]} == {
        "STATIC",
        "DYNAMIC_FLS",
    }
    assert len(manifest["specs"]) == 6
    history_provenance = {
        item["artifact_id"]: item
        for item in json.loads(outputs["specs/economic.json"])["provenance_sources"]
        if item["artifact_id"].startswith("history/")
    }
    assert history_provenance["history/live-only.csv"]["schema_name"] == (
        "portfolio-layered-strategy-returns"
    )
    assert history_provenance["history/live-only.csv"]["schema_version"] == "2"
    assert history_provenance["history/segments.csv"]["schema_name"] == (
        "portfolio-layered-history-segments"
    )
    assert history_provenance["history/segments.csv"]["schema_version"] == "1"
    plan_provenance = {
        item["artifact_id"]: item
        for item in json.loads(outputs["specs/economic.json"])["provenance_sources"]
        if item["artifact_id"].startswith("plan/")
    }
    assert (
        plan_provenance["plan/current-strategy-decomposition-plan.json"][
            "schema_version"
        ]
        == "3"
    )
    dispositions = json.loads(outputs["candidate-dispositions.json"])
    assert dispositions["schema_version"] == 2
    assert len(dispositions["candidate_dispositions"]) == 108
    assert {row["surface"] for row in dispositions["candidate_dispositions"]} == {
        "STATIC",
        "DYNAMIC_FLS",
    }
    assert {row["requirement"] for row in dispositions["candidate_dispositions"]} == {
        "NOT_APPLICABLE_OWNER_APPROVED",
        "REQUIRED",
    }
    required_rows = [
        row
        for row in dispositions["candidate_dispositions"]
        if row["requirement"] == "REQUIRED"
    ]
    assert not any(
        token in row["disposition"] or token in row["reason"]
        for row in required_rows
        for token in ("DROPPED", "OUT_OF_SCOPE", "NOT_APPLICABLE")
    )
    excluded_rows = [
        row
        for row in dispositions["candidate_dispositions"]
        if row["requirement"] == "NOT_APPLICABLE_OWNER_APPROVED"
    ]
    assert {row["strategy_id"] for row in excluded_rows} == {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    assert {row["disposition"] for row in excluded_rows} == {
        "NOT_APPLICABLE_OWNER_APPROVED"
    }
    economic = json.loads(outputs["specs/economic.json"])
    dynamic_economic = json.loads(outputs["specs/economic-dynamic-fls.json"])
    assert {model["estimator"] for model in economic["models"]} == {"STATIC_RBSA"}
    assert {model["estimator"] for model in dynamic_economic["models"]} == {
        "DYNAMIC_FLS"
    }
    assert {model["constraint"] for model in dynamic_economic["models"]} == {
        "UNCONSTRAINED"
    }
    assert all(
        model["smoothness_candidates"] == ["0.00000001", "0.000001"]
        for model in dynamic_economic["models"]
    )
    assert economic["models"][0]["test_start"] > economic["models"][0]["train_end"]
    assert {factor["factor_id"] for factor in economic["factor_source"]["factors"]} == {
        "MKT_RF",
        "SMB",
    }
    for factor in economic["factor_source"]["factors"]:
        assert factor["effective_start"] <= factor["effective_end"]
        assert factor["raw_orthogonal"] is False
        assert factor["selection_rationale"]
        assert factor["redundancy_policy"].startswith("TRAIN_ONLY")
        assert factor["source_id"] == "UNAVAILABLE_NOT_DECLARED"
    economic_targets = list(
        csv.DictReader(
            outputs["inputs/lenses/economic-targets.csv"].decode("utf-8").splitlines()
        )
    )
    assert economic_targets == [
        row for row in selected_targets if row["series_id"] == "QSI"
    ]
    coverage = json.loads(outputs["inputs/history/coverage.json"])
    coverage_by_strategy = {row["strategy_id"]: row for row in coverage["strategies"]}
    assert coverage_by_strategy["composer-holy-grail"]["modes"]["LIVE_ONLY"] == {
        "reason": "NOT_APPLICABLE_OWNER_APPROVED_IMMATERIAL",
        "status": "NOT_APPLICABLE_OWNER_APPROVED",
    }
    assert coverage_by_strategy["echotrade-growth-pivots"]["modes"]["LIVE_ONLY"] == {
        "reason": "NOT_APPLICABLE_OWNER_APPROVED_DEPRECATED",
        "status": "NOT_APPLICABLE_OWNER_APPROVED",
    }


def test_dynamic_surface_policy_and_grid_fail_closed(tmp_path: Path) -> None:
    missing = _fixture(
        tmp_path / "missing",
        plan_mutator=lambda plan: plan["split_policy"].pop("dynamic"),
    )
    with pytest.raises(planner.CurrentPlanError, match="split.dynamic"):
        planner.build_bundle(
            **{key: value for key, value in missing.items() if key != "plan_document"}
        )

    duplicated = _fixture(
        tmp_path / "duplicated",
        plan_mutator=lambda plan: plan["split_policy"]["dynamic"].update(
            smoothness_candidates=["0.000001", "0.000001"]
        ),
    )
    with pytest.raises(planner.CurrentPlanError, match="duplicated"):
        planner.build_bundle(
            **{
                key: value
                for key, value in duplicated.items()
                if key != "plan_document"
            }
        )

    numerically_duplicated = _fixture(
        tmp_path / "numerically-duplicated",
        plan_mutator=lambda plan: plan["split_policy"]["dynamic"].update(
            smoothness_candidates=["0.000001", "1e-6"]
        ),
    )
    with pytest.raises(planner.CurrentPlanError, match="numerically duplicated"):
        planner.build_bundle(
            **{
                key: value
                for key, value in numerically_duplicated.items()
                if key != "plan_document"
            }
        )

    numerically_duplicated_ridge = _fixture(
        tmp_path / "numerically-duplicated-ridge",
        plan_mutator=lambda plan: plan["split_policy"].update(
            ridge_candidates=["0.000001", "1e-6"]
        ),
    )
    with pytest.raises(planner.CurrentPlanError, match="numerically duplicated"):
        planner.build_bundle(
            **{
                key: value
                for key, value in numerically_duplicated_ridge.items()
                if key != "plan_document"
            }
        )

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    outcome = next(
        row
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "DECOMPOSITION"
    )
    outcome["calculation_contract"]["surfaces"] = ["STATIC"]
    with pytest.raises(planner.CurrentPlanError, match="surfaces differ"):
        planner._required_decomposition_surfaces(contract)


def test_required_decomposition_perimeter_cannot_be_dropped_or_omitted() -> None:
    plan = json.loads(
        (
            REPOSITORY_ROOT
            / "docs"
            / "analytics"
            / "current-strategy-decomposition-plan.json"
        ).read_text(encoding="utf-8")
    )
    contract = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )
    strategies = planner._resolved_strategies(plan)
    source_keys = tuple(plan["sources"])
    planner._validate_strategy_perimeter(strategies, contract, source_keys)
    by_id = {row["strategy_id"]: row for row in strategies}
    for strategy_id in ("composer-holy-grail", "echotrade-growth-pivots"):
        cells = by_id[strategy_id]["source_dispositions"]
        assert {cell["reason"] for cell in cells.values()} == {
            "NOT_APPLICABLE_OWNER_APPROVED"
        }
    for strategy_id, reason in (
        ("robuxio-crypto", "REQUIRED_NOT_READY_MISSING_ACCEPTED_CRYPTO_CANDIDATE"),
    ):
        cells = by_id[strategy_id]["source_dispositions"]
        assert {cell["reason"] for cell in cells.values()} == {reason}

    dropped = json.loads(json.dumps(plan))
    dropped_row = next(
        row
        for row in dropped["strategies"]
        if row["strategy_id"] == "composer-holy-grail"
    )
    dropped_row["source_dispositions"]["us-economic"]["reason"] = (
        "REQUIRED_NOT_READY_DROPPED_IMMATERIAL_CURRENT_SCOPE"
    )
    with pytest.raises(
        planner.CurrentPlanError,
        match="owner-approved decomposition non-applicability differs",
    ):
        planner._validate_strategy_perimeter(
            planner._resolved_strategies(dropped), contract, source_keys
        )

    omitted = json.loads(json.dumps(plan))
    omitted["strategies"] = [
        row
        for row in omitted["strategies"]
        if row["strategy_id"] != "echotrade-growth-pivots"
    ]
    with pytest.raises(planner.CurrentPlanError, match="strategy perimeter differs"):
        planner._validate_strategy_perimeter(
            planner._resolved_strategies(omitted), contract, source_keys
        )


def test_full_build_rejects_assignment_and_cell_authority_rewrites(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    planner.build_bundle(**kwargs)
    baseline = inputs["plan_document"]

    missing_assignment = json.loads(json.dumps(baseline))
    qsi = next(
        row for row in missing_assignment["strategies"] if row["strategy_id"] == "qsi"
    )
    qsi["assignments"] = []
    _write_json(inputs["plan_path"], missing_assignment)
    with pytest.raises(
        planner.CurrentPlanError,
        match="assignments differ from source disposition authority",
    ):
        planner.build_bundle(**kwargs)

    false_reason = json.loads(json.dumps(baseline))
    qsi = next(row for row in false_reason["strategies"] if row["strategy_id"] == "qsi")
    qsi["assignments"] = []
    for cell in qsi["source_dispositions"].values():
        cell["readiness"] = "REQUIRED_NOT_READY"
        cell["reason"] = "REQUIRED_NOT_READY_DROPPED_IMMATERIAL_CURRENT_SCOPE"
    _write_json(inputs["plan_path"], false_reason)
    with pytest.raises(
        planner.CurrentPlanError,
        match="required-not-ready decomposition reason differs",
    ):
        planner.build_bundle(**kwargs)

    unauthorized_nonapp = json.loads(json.dumps(baseline))
    qsi = next(
        row for row in unauthorized_nonapp["strategies"] if row["strategy_id"] == "qsi"
    )
    cell = qsi["source_dispositions"]["style"]
    cell.update(
        requirement="NOT_APPLICABLE_OWNER_APPROVED",
        readiness="NOT_APPLICABLE_OWNER_APPROVED",
        reason="NOT_APPLICABLE_OWNER_APPROVED",
    )
    _write_json(inputs["plan_path"], unauthorized_nonapp)
    with pytest.raises(
        planner.CurrentPlanError,
        match="required decomposition cell requirement differs",
    ):
        planner.build_bundle(**kwargs)

    unknown_source = json.loads(json.dumps(baseline))
    qsi = next(
        row for row in unknown_source["strategies"] if row["strategy_id"] == "qsi"
    )
    qsi["assignments"].append(
        {
            "source_key": "unknown-source",
            "constraint": "UNCONSTRAINED",
            "factor_sets": [["UNKNOWN"]],
        }
    )
    _write_json(inputs["plan_path"], unknown_source)
    with pytest.raises(
        planner.CurrentPlanError,
        match="assignment source key is unknown",
    ):
        planner.build_bundle(**kwargs)

    coherent_false_rnr = json.loads(json.dumps(baseline))
    qsi = next(
        row for row in coherent_false_rnr["strategies"] if row["strategy_id"] == "qsi"
    )
    qsi["assignments"] = []
    for cell in qsi["source_dispositions"].values():
        cell.update(
            readiness="REQUIRED_NOT_READY",
            reason="REQUIRED_NOT_READY_APPLICABILITY_NOT_ACCEPTED",
            authority="UNAUTHORIZED_SEMANTIC_REWRITE",
        )
    _write_json(inputs["plan_path"], coherent_false_rnr)
    with pytest.raises(
        planner.CurrentPlanError,
        match="current strategy plan authority digest differs",
    ):
        planner.build_bundle(**kwargs)

    deleted_source = json.loads(json.dumps(baseline))
    deleted_source["sources"].pop("economic")
    for strategy in deleted_source["strategies"]:
        strategy["source_dispositions"].pop("economic")
        if "assignments" in strategy:
            strategy["assignments"] = [
                assignment
                for assignment in strategy["assignments"]
                if assignment["source_key"] != "economic"
            ]
    _write_json(inputs["plan_path"], deleted_source)
    with pytest.raises(
        planner.CurrentPlanError,
        match="current strategy plan authority digest differs",
    ):
        planner.build_bundle(**kwargs)


def test_write_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    output_root = tmp_path / "output"
    first = planner.write_bundle(output_root=output_root, **kwargs)
    second = planner.write_bundle(output_root=output_root, **kwargs)
    assert first == second
    destination = Path(first["destination"])
    assert (destination / "plan-manifest.json").is_file()

    (destination / "candidate-dispositions.json").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(planner.CurrentPlanError, match="differs from exact rebuild"):
        planner.write_bundle(output_root=output_root, **kwargs)


def test_release_builder_admits_exact_planner_bundle(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_path = REPOSITORY_ROOT / "tools" / "portfolio_release_builder.py"
    release_spec = importlib.util.spec_from_file_location(
        "portfolio_release_builder_for_current_plan", release_path
    )
    if release_spec is None or release_spec.loader is None:
        raise RuntimeError("unable to load portfolio release builder")
    release_builder = importlib.util.module_from_spec(release_spec)
    sys.modules[release_spec.name] = release_builder
    release_spec.loader.exec_module(release_builder)

    admitted = release_builder._current_release_plan_specs(destination, manifest)

    assert {row["source_key"] for row in admitted} == {"economic", "sector", "style"}
    assert {row["surface"] for row in admitted} == {"STATIC", "DYNAMIC_FLS"}


def test_release_builder_accepts_planner_valid_manifest_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    public_manifest = json.loads(
        inputs["public_factor_manifest_path"].read_text(encoding="utf-8")
    )
    public_manifest["outputs"].reverse()
    public_names = {
        "eu.csv": "z-europe-identical.csv",
        "industries.csv": "a-industries-identical.csv",
    }
    for output in public_manifest["outputs"]:
        output["path"] = public_names.get(output["path"], output["path"])
    for index in range(256):
        public_manifest["outputs"].append(
            {
                **public_manifest["outputs"][0],
                "path": f"unused-identical-output-{index:03d}.csv",
            }
        )
    public_manifest["outputs"].append(
        {**public_manifest["outputs"][0], "path": "../unbound-identical-output.csv"}
    )
    for input_key, filename in (
        ("europe_factors", "z-europe-identical.csv"),
        ("us_industries", "a-industries-identical.csv"),
    ):
        source_root = tmp_path / f"{input_key}-public-source"
        source_root.mkdir()
        renamed_path = source_root / filename
        shutil.copyfile(inputs["market_paths"][input_key], renamed_path)
        inputs["market_paths"][input_key] = renamed_path
    public_manifest.pop("bundle_id")
    public_manifest["bundle_id"] = hashlib.sha256(
        _canonical(public_manifest)
    ).hexdigest()
    _write_json(inputs["public_factor_manifest_path"], public_manifest)
    for key, input_key in (
        ("style_manifest_path", "style_returns"),
        ("sector_manifest_path", "sector_returns"),
    ):
        market_manifest = json.loads(inputs[key].read_text(encoding="utf-8"))
        output = market_manifest.pop("output")
        output["bytes"] = output.pop("size_bytes")
        output["path"] = "returns.csv"
        market_manifest["outputs"] = [output]
        if input_key == "style_returns":
            market_manifest["outputs"].append(
                {**output, "path": "duplicate-identical-returns.csv"}
            )
        _write_json(inputs[key], market_manifest)
        source_root = tmp_path / f"{input_key}-source"
        source_root.mkdir()
        same_name_path = source_root / "returns.csv"
        shutil.copyfile(inputs["market_paths"][input_key], same_name_path)
        inputs["market_paths"][input_key] = same_name_path
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_builder = _release_builder_module(
        "portfolio_release_builder_for_manifest_forms"
    )
    original_copyfile = release_builder.shutil.copyfile
    public_alias_copies = 0

    def _counted_copyfile(source: object, target: object) -> object:
        nonlocal public_alias_copies
        target_path = Path(target)
        if "public" in target_path.parts and any(
            part.startswith("quant-engine-plan-verifier-") for part in target_path.parts
        ):
            public_alias_copies += 1
        return original_copyfile(source, target)

    monkeypatch.setattr(release_builder.shutil, "copyfile", _counted_copyfile)

    admitted = release_builder._current_release_plan_specs(destination, manifest)

    assert len(admitted) == 6
    assert public_alias_copies == len(planner.PUBLIC_FACTOR_INPUT_KEYS)


def test_release_builder_ignores_unbound_singular_alias_escape_without_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    escape_path = (tmp_path / "escaped" / "returns.csv").resolve()
    style_manifest = json.loads(
        inputs["style_manifest_path"].read_text(encoding="utf-8")
    )
    style_manifest["output"]["path"] = str(escape_path)
    _write_json(inputs["style_manifest_path"], style_manifest)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_builder = _release_builder_module(
        "portfolio_release_builder_for_windows_alias_escape"
    )
    original_copyfile = release_builder.shutil.copyfile

    def _guarded_copyfile(source: object, target: object) -> object:
        if Path(target).resolve() == escape_path:
            raise AssertionError("alias copy escaped the verifier temp directory")
        return original_copyfile(source, target)

    monkeypatch.setattr(release_builder.shutil, "copyfile", _guarded_copyfile)

    admitted = release_builder._current_release_plan_specs(destination, manifest)

    assert len(admitted) == 6
    assert not escape_path.exists()


@pytest.mark.parametrize("location", ("manifest", "policies", "model_dimension"))
def test_release_builder_rejects_rehashed_unknown_plan_fields(
    tmp_path: Path, location: str
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    target = manifest if location == "manifest" else manifest["policies"]
    if location == "model_dimension":
        target = target["model_dimension"]
    target["unknown"] = "REHASHED_BUT_NOT_EMITTED"
    manifest.pop("bundle_id")
    bundle_id = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest["bundle_id"] = bundle_id
    _write_json(destination / "plan-manifest.json", manifest)
    rebound_destination = destination.with_name(bundle_id)
    destination.rename(rebound_destination)
    release_builder = _release_builder_module(
        f"portfolio_release_builder_for_unknown_{location}"
    )

    with pytest.raises(
        release_builder.VerticalBuildError,
        match="current decomposition plan .* differ",
    ):
        release_builder._current_release_plan_specs(rebound_destination, manifest)


def test_release_builder_rejects_self_consistent_unverified_history_receipt(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_builder = _release_builder_module(
        "portfolio_release_builder_for_unverified_history"
    )
    frozen_inputs = release_builder._manifest_artifacts(
        destination, manifest["inputs"], "attribution plan inputs"
    )
    generation_path = destination / "inputs/history/generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["verification"] = "BOGUS"
    generation.pop("generation_id")
    generation_id = hashlib.sha256(_canonical(generation)).hexdigest()
    generation["generation_id"] = generation_id
    _write_json(generation_path, generation)
    generation_content = generation_path.read_bytes()
    generation_sha256 = hashlib.sha256(generation_content).hexdigest()
    frozen_inputs["inputs/history/generation.json"] = (
        generation_sha256,
        len(generation_content),
    )
    manifest["layered_history"] = {
        **manifest["layered_history"],
        "generation_id": generation_id,
        "generation_receipt_sha256": generation_sha256,
    }

    with pytest.raises(
        release_builder.VerticalBuildError,
        match="current decomposition upstream evidence differs",
    ):
        release_builder._verify_current_plan_upstream_evidence(
            destination,
            frozen_inputs,
            manifest,
            evaluation_date=manifest["evaluation_date"],
            evidence_mode=manifest["layered_history"]["mode"],
            assurance_profile=manifest["assurance_profile"],
        )


def test_release_builder_rejects_rehashed_layered_return_substitution(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_builder = _release_builder_module(
        "portfolio_release_builder_for_rehashed_history"
    )
    frozen_inputs = release_builder._manifest_artifacts(
        destination, manifest["inputs"], "attribution plan inputs"
    )
    history_root = destination / "inputs/history"
    projection_name = manifest["layered_history"]["projection"]
    projection_path = history_root / projection_name
    selected_targets_path = history_root / "selected-targets.csv"
    selected = next(
        csv.DictReader(io.StringIO(selected_targets_path.read_text(encoding="utf-8")))
    )
    substituted_return = "0.123456789012"

    def _substitute(path: Path) -> None:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or ())
            rows = list(reader)
        changed = 0
        for row in rows:
            if (
                row["series_id"] == selected["series_id"]
                and row["date"] == selected["date"]
            ):
                row["return"] = substituted_return
                changed += 1
        assert changed == 1
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        path.write_bytes(buffer.getvalue().encode())

    generation_path = history_root / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    for name in (
        "segments.csv",
        "live-only.csv",
        "live-plus-backtest.csv",
        "returns.csv",
    ):
        path = history_root / name
        _substitute(path)
        content = path.read_bytes()
        generation["outputs"][name]["sha256"] = hashlib.sha256(content).hexdigest()
        generation["outputs"][name]["size_bytes"] = len(content)
        frozen_inputs[f"inputs/history/{name}"] = (
            generation["outputs"][name]["sha256"],
            len(content),
        )
    _substitute(selected_targets_path)
    selected_targets_content = selected_targets_path.read_bytes()
    frozen_inputs["inputs/history/selected-targets.csv"] = (
        hashlib.sha256(selected_targets_content).hexdigest(),
        len(selected_targets_content),
    )
    generation.pop("generation_id")
    generation_id = hashlib.sha256(_canonical(generation)).hexdigest()
    generation["generation_id"] = generation_id
    _write_json(generation_path, generation)
    generation_content = generation_path.read_bytes()
    generation_sha256 = hashlib.sha256(generation_content).hexdigest()
    frozen_inputs["inputs/history/generation.json"] = (
        generation_sha256,
        len(generation_content),
    )
    manifest["layered_history"] = {
        **manifest["layered_history"],
        "generation_id": generation_id,
        "generation_receipt_sha256": generation_sha256,
        "projection_sha256": hashlib.sha256(projection_path.read_bytes()).hexdigest(),
        "segments_sha256": hashlib.sha256(
            (history_root / "segments.csv").read_bytes()
        ).hexdigest(),
        "selected_targets_sha256": hashlib.sha256(selected_targets_content).hexdigest(),
    }

    with pytest.raises(
        release_builder.VerticalBuildError,
        match="current decomposition upstream evidence differs",
    ):
        release_builder._verify_current_plan_upstream_evidence(
            destination,
            frozen_inputs,
            manifest,
            evaluation_date=manifest["evaluation_date"],
            evidence_mode=manifest["layered_history"]["mode"],
            assurance_profile=manifest["assurance_profile"],
        )


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    (
        ("inputs/market/us_factors.csv", b"tampered\n"),
        ("inputs/history/selected-targets.csv", b"tampered\n"),
    ),
)
def test_release_builder_rejects_substituted_upstream_evidence(
    tmp_path: Path, relative_path: str, mutation: bytes
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    written = planner.write_bundle(output_root=tmp_path / "output", **kwargs)
    destination = Path(written["destination"])
    manifest = json.loads(
        (destination / "plan-manifest.json").read_text(encoding="utf-8")
    )
    release_path = REPOSITORY_ROOT / "tools" / "portfolio_release_builder.py"
    release_spec = importlib.util.spec_from_file_location(
        "portfolio_release_builder_for_upstream_mutation", release_path
    )
    if release_spec is None or release_spec.loader is None:
        raise RuntimeError("unable to load portfolio release builder")
    release_builder = importlib.util.module_from_spec(release_spec)
    sys.modules[release_spec.name] = release_builder
    release_spec.loader.exec_module(release_builder)
    frozen_inputs = release_builder._manifest_artifacts(
        destination, manifest["inputs"], "attribution plan inputs"
    )

    path = destination / relative_path
    path.write_bytes(path.read_bytes() + mutation)

    with pytest.raises(
        release_builder.VerticalBuildError,
        match="current decomposition .* differ",
    ):
        release_builder._verify_current_plan_upstream_evidence(
            destination,
            frozen_inputs,
            manifest,
            evaluation_date=manifest["evaluation_date"],
            evidence_mode=manifest["layered_history"]["mode"],
            assurance_profile=manifest["assurance_profile"],
        )


def test_layered_history_tampering_and_uncontrolled_contract_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    strategies = planner._resolved_strategies(inputs["plan_document"])
    generation_root = inputs["layered_history_generation_path"]

    for name in ("projection", "coverage", "receipt", "inventory"):
        tampered_root = tmp_path / f"tampered-{name}"
        shutil.copytree(generation_root, tampered_root)
        if name == "projection":
            path = tampered_root / "live-only.csv"
            path.write_bytes(path.read_bytes() + b"tampered\n")
        elif name == "coverage":
            payload = json.loads((tampered_root / "coverage.json").read_text())
            payload["uncontrolled"] = True
            _write_json(tampered_root / "coverage.json", payload)
        elif name == "receipt":
            payload = json.loads((tampered_root / "generation.json").read_text())
            payload["builder_version"] = "uncontrolled"
            _write_json(tampered_root / "generation.json", payload)
        else:
            (tampered_root / "extra.txt").write_text("uncontrolled", encoding="utf-8")
        with pytest.raises(
            planner.CurrentPlanError, match="layered history verification failed"
        ):
            planner._layered_history_targets(
                generation_root=tampered_root,
                contract_path=inputs["financial_mvp_contract_path"],
                strategies=strategies,
                evidence_mode="LIVE_ONLY",
                evaluated_on=date.fromisoformat(inputs["evaluation_date"]),
            )

    contract_path = tmp_path / "uncontrolled-contract.json"
    contract = json.loads(
        inputs["financial_mvp_contract_path"].read_text(encoding="utf-8")
    )
    contract["uncontrolled_policy"] = True
    _write_json(contract_path, contract)

    class _CachedBypassVerifier:
        @staticmethod
        def verify_layered_history_generation(
            _generation_root: Path, _contract_path: Path
        ) -> dict[str, str]:
            return {
                "generation_id": generation_root.name,
                "verification": "VALID",
            }

    monkeypatch.setitem(
        sys.modules,
        "portfolio_target_series_for_current_decomposition_plan",
        _CachedBypassVerifier,
    )
    with pytest.raises(
        planner.CurrentPlanError, match="layered history verification failed"
    ):
        planner._layered_history_targets(
            generation_root=generation_root,
            contract_path=contract_path,
            strategies=strategies,
            evidence_mode="LIVE_ONLY",
            evaluated_on=date.fromisoformat(inputs["evaluation_date"]),
        )


def test_live_plus_backtest_requires_exact_available_mode(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    with pytest.raises(
        planner.CurrentPlanError,
        match=(
            "LIVE_PLUS_BACKTEST is not ready for qsi: "
            "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED"
        ),
    ):
        planner.build_bundle(
            evidence_mode="LIVE_PLUS_BACKTEST",
            **{key: value for key, value in inputs.items() if key != "plan_document"},
        )


def test_layered_history_age_warns_in_development_and_fails_release_qc(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    contract = json.loads(
        inputs["financial_mvp_contract_path"].read_text(encoding="utf-8")
    )
    owner_history = next(
        source
        for source in contract["source_segments"]
        if source["source_id"] == "owner-account-live-history"
    )
    max_age_days = owner_history["freshness"]["max_age_days"]
    coverage = json.loads(
        (inputs["layered_history_generation_path"] / "coverage.json").read_text(
            encoding="utf-8"
        )
    )
    qsi = next(row for row in coverage["strategies"] if row["strategy_id"] == "qsi")
    live = next(
        row
        for row in qsi["segments"]
        if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    )
    stale_date = date.fromisoformat(live["observed_end"]) + timedelta(
        days=max_age_days + 1
    )
    result = planner._layered_history_targets(
        generation_root=inputs["layered_history_generation_path"],
        contract_path=inputs["financial_mvp_contract_path"],
        strategies=planner._resolved_strategies(inputs["plan_document"]),
        evidence_mode="LIVE_ONLY",
        evaluated_on=stale_date,
        assurance_profile="DEVELOPMENT",
    )
    qsi_age = next(
        row
        for row in result["identity"]["source_age_dispositions"]
        if row["strategy_id"] == "qsi"
    )
    assert qsi_age == {
        "age_days": max_age_days + 1,
        "complete_through": live["observed_end"],
        "evaluated_on": stale_date.isoformat(),
        "limiting_inputs": ["qsi:owner-account-live-history"],
        "status": "WARN_COMPLETE_DATA_AGE",
        "warning_after_days": max_age_days,
        "evidence_layer": "ACCOUNT_LIVE",
        "source_id": "owner-account-live-history",
        "strategy_id": "qsi",
    }
    assert result["identity"]["calculation_boundary"]["status"] == (
        "WARN_COMPLETE_DATA_AGE"
    )

    with pytest.raises(
        planner.CurrentPlanError,
        match="release QC requires a layered history receipt for the evaluation date",
    ):
        planner._layered_history_targets(
            generation_root=inputs["layered_history_generation_path"],
            contract_path=inputs["financial_mvp_contract_path"],
            strategies=planner._resolved_strategies(inputs["plan_document"]),
            evidence_mode="LIVE_ONLY",
            evaluated_on=stale_date,
            assurance_profile="RELEASE_QC",
        )


def test_release_qc_rejects_prior_layered_receipt_even_within_age_limit(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    generation = json.loads(
        (inputs["layered_history_generation_path"] / "generation.json").read_text(
            encoding="utf-8"
        )
    )
    evaluated_on = date.fromisoformat(generation["as_of_date"]) + timedelta(days=1)
    kwargs = {
        "generation_root": inputs["layered_history_generation_path"],
        "contract_path": inputs["financial_mvp_contract_path"],
        "strategies": planner._resolved_strategies(inputs["plan_document"]),
        "evidence_mode": "LIVE_ONLY",
        "evaluated_on": evaluated_on,
    }

    development = planner._layered_history_targets(
        assurance_profile="DEVELOPMENT", **kwargs
    )
    assert development["identity"]["generation_as_of_date"] == generation["as_of_date"]
    with pytest.raises(
        planner.CurrentPlanError,
        match="release QC requires a layered history receipt for the evaluation date",
    ):
        planner._layered_history_targets(assurance_profile="RELEASE_QC", **kwargs)


def test_latest_common_complete_date_uses_exact_intersection_not_minimum_tail() -> None:
    first = {date(2026, 1, 1), date(2026, 1, 3)}
    second = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 4)}

    assert planner._latest_common_complete_date(
        [first, second],
        evaluated_on=date(2026, 1, 4),
        name="disconfirm minimum tail",
    ) == date(2026, 1, 1)


def test_models_use_independent_common_boundaries_without_global_target_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _assign_economic(plan_document: dict[str, Any]) -> None:
        tt = next(
            row
            for row in plan_document["strategies"]
            if row["strategy_id"] == "tt-portfolio"
        )
        tt["assignments"].append(
            {
                "source_key": "economic",
                "constraint": "UNCONSTRAINED",
                "factor_sets": [["MKT_RF"], ["MKT_RF", "SMB"]],
            }
        )

    inputs = _fixture(tmp_path, plan_mutator=_assign_economic)
    factor_rows = list(
        csv.DictReader(
            inputs["market_paths"]["us_factors"]
            .read_text(encoding="utf-8")
            .splitlines()
        )
    )
    factor_dates = [date.fromisoformat(row["date"]) for row in factor_rows]
    qsi_dates = factor_dates[:-5]
    tt_dates = factor_dates
    original = planner._layered_history_targets

    def _different_target_tails(**kwargs: Any) -> dict[str, Any]:
        result = original(**kwargs)
        dates_by_series = {"QSI": qsi_dates, "TT Portfolio": tt_dates}
        rows = [
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": f"{index / 100000:.12f}",
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
            for series_id, observed_dates in dates_by_series.items()
            for index, observed in enumerate(observed_dates, 1)
        ]
        content = planner._csv_bytes(
            planner.TARGET_HEADERS,
            [[row[header] for header in planner.TARGET_HEADERS] for row in rows],
        )
        result["rows"] = rows
        result["target_content"] = content
        result["contents"]["selected-targets.csv"] = content
        result["identity"]["selected_targets_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        for series_id, observed_dates in dates_by_series.items():
            value = result["composition"][series_id]
            value["accepted_history_complete_through"] = observed_dates[-1].isoformat()
            value["calculation_observations_used"] = len(observed_dates)
            value["live_observations"] = len(observed_dates)
        return result

    monkeypatch.setattr(planner, "_layered_history_targets", _different_target_tails)
    outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    economic = json.loads(outputs["specs/economic.json"])
    boundaries = {row["series_id"]: row["as_of_date"] for row in economic["models"]}
    assert boundaries == {
        "QSI": qsi_dates[-1].isoformat(),
        "TT Portfolio": tt_dates[-1].isoformat(),
    }
    assert economic["as_of_date"] == tt_dates[-1].isoformat()
    selected = list(
        csv.DictReader(
            outputs["inputs/history/selected-targets.csv"].decode("utf-8").splitlines()
        )
    )
    assert max(row["date"] for row in selected if row["series_id"] == "QSI") == (
        qsi_dates[-1].isoformat()
    )
    assert (
        max(row["date"] for row in selected if row["series_id"] == "TT Portfolio")
        == tt_dates[-1].isoformat()
    )
    per_model = next(
        row
        for row in manifest["specs"]
        if row["source_key"] == "economic" and row["surface"] == "STATIC"
    )["model_calculation_boundaries"]
    assert per_model["qsi-economic-01-static"]["complete_through"] == (
        qsi_dates[-1].isoformat()
    )
    assert per_model["tt-portfolio-economic-01-static"]["complete_through"] == (
        tt_dates[-1].isoformat()
    )

    source_root = tmp_path / "per-model-materialized"
    for relative, content in outputs.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result, _generation, _content = engine.build_decomposition(
        source_root / "specs/economic.json", source_root
    )
    result_boundaries = {
        row["series_id"]: row["model_as_of_date"] for row in result["model_results"]
    }
    assert result_boundaries == boundaries
    assert {
        row["series_id"]: row["coverage"]["target_observed_end"]
        for row in result["model_results"]
    } == boundaries


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("duplicate", "duplicate series date"),
        ("wrong_layer", "evidence layer differs from mode"),
        ("future", "beyond the evaluation date"),
    ),
)
def test_planner_independently_rejects_invalid_selected_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    generation_id = "1" * 64
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    _write_json(
        generation_root / "generation.json",
        {
            "schema_version": 1,
            "as_of_date": "2026-01-10",
            "generation_id": generation_id,
            "outputs": {
                "coverage.json": {
                    "schema_name": "portfolio-layered-history-coverage",
                    "schema_version": "1",
                },
                "live-only.csv": {
                    "schema_name": "portfolio-layered-strategy-returns",
                    "schema_version": "2",
                },
                "segments.csv": {
                    "schema_name": "portfolio-layered-history-segments",
                    "schema_version": "1",
                },
            },
        },
    )
    _write_json(
        generation_root / "coverage.json",
        {
            "strategies": [
                {
                    "strategy_id": "qsi",
                    "series_id": "QSI",
                    "strategy_version": {"history_generation_ref": "qsi:v1"},
                    "modes": {
                        "LIVE_ONLY": {"status": "AVAILABLE", "reason": "AVAILABLE"}
                    },
                    "segments": [
                        {
                            "evidence_layer": "ACCOUNT_LIVE",
                            "mode_admission": "INCLUDE_SELECTED_LIVE",
                            "source_id": "provider-ledger-account-live",
                            "observed_start": "2026-01-09",
                            "observed_end": "2026-01-09",
                            "return_observations": 1,
                        }
                    ],
                }
            ]
        },
    )
    _write_json(
        tmp_path / "contract.json",
        {
            "contract_schema_version": 5,
            "product": {
                "as_of_policy": {
                    "execution_profiles": {
                        "DEVELOPMENT": (
                            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_"
                            "STRUCTURED_AGE_WARNING"
                        )
                    }
                }
            },
            "source_segments": [
                {
                    "source_id": "provider-ledger-account-live",
                    "freshness": {"max_age_days": 10},
                }
            ],
        },
    )
    selected_date = "2026-01-11" if mutation == "future" else "2026-01-09"
    selected_layer = "BACKTEST" if mutation == "wrong_layer" else "ACCOUNT_LIVE"
    projection_rows = [["QSI", selected_date, "0.001", selected_layer, "qsi:v1"]]
    if mutation == "duplicate":
        projection_rows.append(list(projection_rows[0]))
    (generation_root / "live-only.csv").write_bytes(
        planner._csv_bytes(planner.LAYERED_PROJECTION_HEADERS, projection_rows)
    )
    segment_row = [
        "qsi",
        "QSI",
        selected_date,
        "0.001",
        selected_layer,
        "qsi:v1",
        "qsi-live",
        "provider-ledger-account-live",
        "2" * 64,
        "INCLUDE_SELECTED_LIVE",
        "NET",
        "TOTAL",
        "WEEKDAY_SOURCE_CALENDAR",
        "DAILY",
    ]
    (generation_root / "segments.csv").write_bytes(
        planner._csv_bytes(planner.LAYERED_SEGMENT_HEADERS, [segment_row])
    )

    class _AcceptedVerifier:
        @staticmethod
        def verify_layered_history_generation(
            _generation_root: Path, _contract_path: Path
        ) -> dict[str, str]:
            return {"generation_id": generation_id, "verification": "VALID"}

    monkeypatch.setattr(planner, "_layered_history_module", _AcceptedVerifier)
    with pytest.raises(planner.CurrentPlanError, match=message):
        planner._layered_history_targets(
            generation_root=generation_root,
            contract_path=tmp_path / "contract.json",
            strategies=[
                {
                    "strategy_id": "qsi",
                    "series_id": "QSI",
                    "assignments": [{"source_key": "economic"}],
                }
            ],
            evidence_mode="LIVE_ONLY",
            evaluated_on=date(2026, 1, 10),
        )


def test_available_hybrid_projection_is_selected_without_legacy_override(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    baseline_outputs, baseline_manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )

    history_fixture = history_tests._history_fixture(tmp_path / "hybrid-source")
    contract = json.loads(history_fixture["contract_path"].read_text(encoding="utf-8"))
    version = "synthetic-qsi:v1"
    strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "qsi"
    )
    strategy["strategy_version"] = {"status": "EVIDENCED", "version_ref": version}
    returns_path = tmp_path / "hybrid-strategy-history.csv"
    returns_content = history_tests._csv_bytes(
        history_tests.target_series.CORRELATION_FIELDS_V2,
        [
            {
                "date": "2024-01-02",
                "evidence_layer": "BACKTEST",
                "return": "0.01",
                "series_id": "QSI",
                "strategy_version": version,
            },
            {
                "date": "2024-01-03",
                "evidence_layer": "BACKTEST",
                "return": "-0.005",
                "series_id": "QSI",
                "strategy_version": version,
            },
        ],
    )
    returns_path.write_bytes(returns_content)
    strategy_manifest = {
        "as_of_date": history_fixture["as_of_date"],
        "builder": "portfolio-strategy-history",
        "builder_version": "1.4.0",
        "history_dispositions": [],
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(returns_content).hexdigest(),
            "size_bytes": len(returns_content),
        },
        "schema_version": 1,
        "series": [
            {
                "compatibility_basis": "SYNTHETIC_OWNER_ACCEPTED_VERSION",
                "frequency": "DAILY_TRADING",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "implementation_reconciliation_status": "RECONCILED_SYNTHETIC",
                "observed_end": "2024-01-03",
                "observed_start": "2024-01-02",
                "required_for_analysis": True,
                "return_observations": 2,
                "series_id": "QSI",
                "strategy_version": version,
            }
        ],
        "verification": "VERIFIED",
    }
    strategy_manifest["bundle_id"] = hashlib.sha256(
        _canonical(strategy_manifest)
    ).hexdigest()
    strategy_manifest_path = _write_json(
        tmp_path / "hybrid-strategy-history.json", strategy_manifest
    )
    history_fixture["strategy_history_manifest_path"] = strategy_manifest_path
    history_fixture["strategy_history_returns_path"] = returns_path

    backtest_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "strategy-backtests"
    )
    backtest_source["strategy_version"] = {
        "binding": "EXACT_VERSION_COMPATIBILITY_REQUIRED",
        "status": "MIXED",
        "version_refs": {"qsi": version},
    }
    backtest_source["current_generation"] = {
        "as_of": history_fixture["as_of_date"],
        "generation_id": strategy_manifest["bundle_id"],
        "sha256": hashlib.sha256(strategy_manifest_path.read_bytes()).hexdigest(),
    }
    backtest_source["existence_status"] = "EXISTS"
    backtest_source["capture_status"] = "CAPTURED_GOVERNED"
    backtest_source["interval"]["observed_start"] = "2024-01-02"
    backtest_source["interval"]["observed_end"] = "2024-01-03"
    live_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "owner-account-live-history"
    )
    live_source["strategy_version"]["version_refs"]["qsi"] = version
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        if identity["strategy_id"] == "qsi":
            identity["strategy_version"] = version
    for plan_strategy in inputs["plan_document"]["strategies"][1:]:
        plan_strategy.pop("inherit_assignments_from", None)
        plan_strategy["assignments"] = []
    _bind_fixture_source_dispositions(inputs["plan_document"])
    _write_json(inputs["plan_path"], inputs["plan_document"])
    plan_sha256 = hashlib.sha256(inputs["plan_path"].read_bytes()).hexdigest()
    registry = next(
        source
        for source in contract["source_segments"]
        if source["source_id"] == "current-strategy-registry"
    )
    registry["current_generation"] = {
        "as_of": history_fixture["as_of_date"],
        "generation_id": plan_sha256,
        "sha256": plan_sha256,
    }
    _write_json(history_fixture["contract_path"], contract)
    history_tests._refresh_materialized_segment_identities(history_fixture)
    history_artifacts, history_manifest_content = history_tests._build(history_fixture)
    hybrid_generation = history_tests.write_layered_history_generation(
        tmp_path / "hybrid-generations", history_artifacts, history_manifest_content
    )
    assert (
        history_tests.verify_layered_history_generation(
            hybrid_generation, history_fixture["contract_path"]
        )["verification"]
        == "VALID"
    )

    inputs["financial_mvp_contract_path"] = history_fixture["contract_path"]
    inputs["layered_history_generation_path"] = hybrid_generation
    outputs, manifest = planner.build_bundle(
        evidence_mode="LIVE_PLUS_BACKTEST",
        **{key: value for key, value in inputs.items() if key != "plan_document"},
    )
    economic = json.loads(outputs["specs/economic.json"])
    targets = list(
        csv.DictReader(
            outputs["inputs/lenses/economic-targets.csv"].decode("utf-8").splitlines()
        )
    )
    assert {row["evidence_layer"] for row in targets} == {
        "ACCOUNT_LIVE",
        "BACKTEST",
    }
    assert economic["series_composition"]["QSI"]["backtest_observations_used"] == 2
    assert manifest["layered_history"]["mode"] == "LIVE_PLUS_BACKTEST"
    assert "inputs/history/live-plus-backtest.csv" in outputs
    assert "inputs/history/live-only.csv" in outputs
    assert (
        hashlib.sha256(outputs["inputs/history/live-plus-backtest.csv"]).hexdigest()
        == manifest["layered_history"]["projection_sha256"]
    )
    assert (
        hashlib.sha256(outputs["inputs/history/coverage.json"]).hexdigest()
        == (manifest["layered_history"]["coverage_sha256"])
    )
    assert (
        hashlib.sha256(outputs["inputs/history/generation.json"]).hexdigest()
        == (manifest["layered_history"]["generation_receipt_sha256"])
    )
    provenance = {item["artifact_id"]: item for item in economic["provenance_sources"]}
    for name in (
        "coverage.json",
        "excluded.csv",
        "generation.json",
        "live-only.csv",
        "live-plus-backtest.csv",
        "returns.csv",
        "segments.csv",
        "selected-targets.csv",
    ):
        relative = f"inputs/history/{name}"
        assert provenance[f"history/{name}"]["relative_path"] == relative
        assert (
            provenance[f"history/{name}"]["expected_sha256"]
            == hashlib.sha256(outputs[relative]).hexdigest()
        )
    assert (
        outputs["inputs/history/selected-targets.csv"]
        != baseline_outputs["inputs/history/selected-targets.csv"]
    )
    assert manifest["bundle_id"] != baseline_manifest["bundle_id"]


def test_legacy_target_substitution_cannot_change_selected_history(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    kwargs = {key: value for key, value in inputs.items() if key != "plan_document"}
    baseline_outputs, baseline_manifest = planner.build_bundle(**kwargs)

    legacy_path = inputs["base_source_root"] / "targets.csv"
    legacy_path.write_text(
        ",".join(planner.TARGET_HEADERS)
        + "\nlegacy-poison,2099-01-01,9,ACCOUNT_LIVE,GROSS,PRICE,999\n",
        encoding="utf-8",
    )
    base_spec = json.loads(inputs["base_spec_path"].read_text(encoding="utf-8"))
    base_spec["target_source"] = _definition(legacy_path, "legacy-poison")
    _write_json(inputs["base_spec_path"], base_spec)

    changed_outputs, changed_manifest = planner.build_bundle(**kwargs)
    assert (
        changed_outputs["inputs/history/selected-targets.csv"]
        == (baseline_outputs["inputs/history/selected-targets.csv"])
    )
    assert (
        changed_outputs["specs/economic.json"]
        == baseline_outputs["specs/economic.json"]
    )
    assert changed_manifest["layered_history"] == baseline_manifest["layered_history"]
    assert changed_manifest["bundle_id"] != baseline_manifest["bundle_id"]
    assert all(
        b"legacy-poison" not in content
        for path, content in changed_outputs.items()
        if path != "inputs/governance/base-decomposition-spec.json"
    )

    base_spec["target_source"]["relative_path"] = r"C:\private\targets.csv"
    _write_json(inputs["base_spec_path"], base_spec)
    with pytest.raises(planner.CurrentPlanError, match="portable relative path"):
        planner.build_bundle(**kwargs)


def test_generated_specs_execute_under_the_closed_engine_contract(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    outputs, _manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    source_root = tmp_path / "materialized"
    for relative, content in outputs.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    for relative in (
        "specs/economic.json",
        "specs/economic-dynamic-fls.json",
        "specs/sector.json",
        "specs/sector-dynamic-fls.json",
        "specs/style.json",
        "specs/style-dynamic-fls.json",
    ):
        result, manifest, _content = engine.build_decomposition(
            source_root / relative, source_root
        )
        assert manifest.generation_id
        assert len(result["model_selections"]) == 1


def test_plan_preserves_target_end_beyond_publication_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    factor_days = _weekdays_through(date(2026, 1, 2), date(2026, 6, 30))
    target_days = _weekdays_through(date(2026, 1, 2), date(2026, 8, 17))
    _write_market(
        inputs["market_paths"]["us_factors"], ("rf", "mkt_rf", "smb"), factor_days
    )
    _write_market(
        inputs["market_paths"]["europe_factors"], ("rf", "unused"), factor_days
    )
    _write_market(
        inputs["market_paths"]["us_industries"], ("rf", "unused"), factor_days
    )
    catalog_sha256 = hashlib.sha256(inputs["catalog_path"].read_bytes()).hexdigest()
    _write_json(
        inputs["public_factor_manifest_path"],
        _public_factor_manifest(
            catalog_sha256, inputs["market_paths"], date(2026, 8, 17)
        ),
    )
    inputs["evaluation_date"] = "2026-08-17"

    original = planner._layered_history_targets

    def _long_history(**kwargs: Any) -> dict[str, Any]:
        result = original(**kwargs)
        rows = [
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": f"{index / 100000:.12f}",
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
            for series_id in ("QSI", "TT Portfolio")
            for index, observed in enumerate(target_days, 1)
        ]
        content = planner._csv_bytes(
            planner.TARGET_HEADERS,
            [[row[header] for header in planner.TARGET_HEADERS] for row in rows],
        )
        result["rows"] = rows
        result["target_content"] = content
        result["contents"]["selected-targets.csv"] = content
        result["identity"]["selected_targets_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        for value in result["composition"].values():
            value["live_observations"] = len(target_days)
        return result

    monkeypatch.setattr(planner, "_layered_history_targets", _long_history)

    outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    economic = json.loads(outputs["specs/economic.json"])
    target_rows = list(
        csv.DictReader(
            outputs["inputs/lenses/economic-targets.csv"].decode("utf-8").splitlines()
        )
    )
    factor_rows = list(
        csv.DictReader(
            outputs["inputs/lenses/economic-factors.csv"].decode("utf-8").splitlines()
        )
    )
    retained_history_rows = list(
        csv.DictReader(
            outputs["inputs/history/selected-targets.csv"].decode("utf-8").splitlines()
        )
    )

    assert economic["as_of_date"] == target_rows[-1]["date"]
    assert factor_rows[-1]["date"] == "2026-06-30"
    assert target_rows[-1]["date"] == "2026-06-30"
    assert retained_history_rows[-1]["date"] == "2026-08-17"
    assert economic["maximum_factor_staleness_days"] == 0
    expected_boundary = {
        "age_days": 48,
        "complete_through": "2026-06-30",
        "evaluated_on": "2026-08-17",
        "limiting_inputs": ["model:qsi-economic-01", "model:qsi-economic-02"],
        "status": "WARN_COMPLETE_DATA_AGE",
        "warning_after_days": 0,
    }
    boundaries = manifest["data_age"]["spec_calculation_boundaries"]
    assert set(boundaries) == {
        "economic:DYNAMIC_FLS",
        "economic:STATIC",
        "sector:DYNAMIC_FLS",
        "sector:STATIC",
        "style:DYNAMIC_FLS",
        "style:STATIC",
    }
    assert boundaries["economic:STATIC"] == expected_boundary
    assert boundaries["economic:DYNAMIC_FLS"] == expected_boundary

    source_root = tmp_path / "publication-lag-materialized"
    for relative, content in outputs.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result, _manifest, _content = engine.build_decomposition(
        source_root / "specs/economic.json", source_root
    )
    assert all(
        model["status"] != "UNAVAILABLE_STALE_FACTORS"
        for model in result["model_results"]
    )


def test_build_rejects_manifest_drift(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    inputs["market_paths"]["us_factors"].write_text("changed", encoding="utf-8")
    with pytest.raises(planner.CurrentPlanError, match="differs from its manifest"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_public_manifest_binds_receipts_sources_captures_and_bundle(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    manifest_path = inputs["public_factor_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipts = manifest["acquisition_run"]["receipt_manifest_sha256s"]
    receipts["us_factors"] = "0" * 64
    run = manifest["acquisition_run"]
    run_payload = {
        "evaluation_date": run["evaluation_date"],
        "run_started_at_utc": run["run_started_at_utc"],
        "run_ended_at_utc": run["run_ended_at_utc"],
        "receipt_manifest_sha256s": receipts,
    }
    run["run_id"] = hashlib.sha256(_canonical(run_payload)).hexdigest()
    _reidentify_public_manifest(manifest)
    _write_json(manifest_path, manifest)
    with pytest.raises(planner.CurrentPlanError, match="source receipt identity"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )

    inputs = _fixture(tmp_path / "capture")
    manifest_path = inputs["public_factor_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["response"]["captured_at_utc"] = (
        f"{inputs['evaluation_date']}T11:00:00Z"
    )
    _reidentify_public_manifest(manifest)
    _write_json(manifest_path, manifest)
    with pytest.raises(planner.CurrentPlanError, match="outside the current run"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )

    inputs = _fixture(tmp_path / "bundle")
    manifest_path = inputs["public_factor_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unbound_edit"] = True
    _write_json(manifest_path, manifest)
    with pytest.raises(planner.CurrentPlanError, match="bundle identity"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_public_factor_coherent_interior_gap_is_rejected(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    path = inputs["market_paths"]["us_factors"]
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    rows.pop(2)
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    manifest_path = inputs["public_factor_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = next(row for row in manifest["outputs"] if row["path"] == path.name)
    content = path.read_bytes()
    output["sha256"] = hashlib.sha256(content).hexdigest()
    output["bytes"] = len(content)
    output["row_count"] -= 1
    _reidentify_public_manifest(manifest)
    _write_json(manifest_path, manifest)

    with pytest.raises(planner.CurrentPlanError, match="US factor and industry"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_public_manifest_reuse_warns_in_development_and_fails_release_qc(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    original_evaluation_date = inputs["evaluation_date"]
    inputs["evaluation_date"] = (
        date.fromisoformat(inputs["evaluation_date"]) + timedelta(days=1)
    ).isoformat()
    _outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    assert manifest["data_age"]["public_factor_capture"] == {
        "age_days": 1,
        "complete_through": original_evaluation_date,
        "evaluated_on": inputs["evaluation_date"],
        "limiting_inputs": ["public-factor-acquisition-run"],
        "status": "WARN_COMPLETE_DATA_AGE",
        "warning_after_days": 0,
    }
    assert manifest["release_qc_eligibility"] == ("NOT_ELIGIBLE_NON_RELEASE_PROFILE")

    with pytest.raises(planner.CurrentPlanError, match="planning run"):
        planner.build_bundle(
            assurance_profile="RELEASE_QC",
            **{key: value for key, value in inputs.items() if key != "plan_document"},
        )


def test_market_source_age_warns_in_development_and_fails_release_qc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    original = planner._layered_history_targets

    def _current_history(**kwargs: Any) -> dict[str, Any]:
        kwargs["assurance_profile"] = "DEVELOPMENT"
        return original(**kwargs)

    monkeypatch.setattr(planner, "_layered_history_targets", _current_history)
    _outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    ages = {row["input_key"]: row for row in manifest["data_age"]["market_sources"]}
    assert set(ages) == {"sector_returns", "style_returns"}
    assert all(row["warning_after_days"] == 10 for row in ages.values())
    assert all(row["status"] == "WARN_COMPLETE_DATA_AGE" for row in ages.values())

    with pytest.raises(planner.CurrentPlanError, match="current market source"):
        planner.build_bundle(
            assurance_profile="RELEASE_QC",
            **{key: value for key, value in inputs.items() if key != "plan_document"},
        )


def test_style_and_sector_may_have_different_pre_history(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    sector_path = inputs["market_paths"]["sector_returns"]
    rows = list(csv.reader(sector_path.read_text(encoding="utf-8").splitlines()))
    first_day = date.fromisoformat(rows[1][0])
    rows.insert(1, [(first_day - timedelta(days=1)).isoformat(), *rows[1][1:]])
    with sector_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    manifest_path = inputs["sector_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = sector_path.read_bytes()
    manifest["output"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["output"]["size_bytes"] = len(content)
    _write_json(manifest_path, manifest)

    planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )


def test_style_and_sector_shared_calendar_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    sector_path = inputs["market_paths"]["sector_returns"]
    rows = list(csv.reader(sector_path.read_text(encoding="utf-8").splitlines()))
    rows.pop(2)
    with sector_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    manifest_path = inputs["sector_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = sector_path.read_bytes()
    manifest["output"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["output"]["size_bytes"] = len(content)
    _write_json(manifest_path, manifest)

    with pytest.raises(
        planner.CurrentPlanError, match="shared market calendars differ"
    ):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_market_pre_inception_missing_cells_are_excluded_from_projection(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    style_path = inputs["market_paths"]["style_returns"]
    rows = list(csv.reader(style_path.read_text(encoding="utf-8").splitlines()))
    rows[1][2] = ""
    with style_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    manifest_path = inputs["style_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = style_path.read_bytes()
    manifest["output"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["output"]["size_bytes"] = len(content)
    _write_json(manifest_path, manifest)

    outputs, _manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )
    projected = list(
        csv.DictReader(outputs["inputs/lenses/style-factors.csv"].decode().splitlines())
    )
    assert projected[0]["date"] == rows[2][0]


@pytest.mark.parametrize("row_index", (2, -1))
def test_market_missing_cell_after_completeness_boundary_is_rejected(
    tmp_path: Path, row_index: int
) -> None:
    inputs = _fixture(tmp_path)
    style_path = inputs["market_paths"]["style_returns"]
    rows = list(csv.reader(style_path.read_text(encoding="utf-8").splitlines()))
    rows[row_index][2] = ""
    with style_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)

    manifest_path = inputs["style_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = style_path.read_bytes()
    manifest["output"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["output"]["size_bytes"] = len(content)
    _write_json(manifest_path, manifest)

    with pytest.raises(
        planner.CurrentPlanError,
        match="incomplete required observation after its completeness boundary",
    ):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_declared_industry_provider_missing_cell_uses_exact_date_intersection() -> None:
    rows = [
        {"date": "2026-01-05", "industry": "0.01", "rf": "0.001"},
        {"date": "2026-01-06", "industry": "", "rf": "0.001"},
        {"date": "2026-01-07", "industry": "0.02", "rf": "0.001"},
    ]

    assert planner._complete_dates(
        rows,
        ("industry", "rf"),
        "declared industry",
        allowed_missing_after_start=frozenset(("industry",)),
        always_required=frozenset(("rf",)),
    ) == (date(2026, 1, 5), date(2026, 1, 7))
    with pytest.raises(
        planner.CurrentPlanError,
        match="incomplete required observation after its completeness boundary",
    ):
        planner._complete_dates(rows, ("industry", "rf"), "undeclared market source")

    rows[1] = {"date": "2026-01-06", "industry": "0.01", "rf": ""}
    with pytest.raises(
        planner.CurrentPlanError,
        match="missing unconditionally required observation",
    ):
        planner._complete_dates(
            rows,
            ("industry", "rf"),
            "declared industry",
            allowed_missing_after_start=frozenset(("industry",)),
            always_required=frozenset(("rf",)),
        )


def test_public_factor_required_columns_must_be_complete_at_admitted_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture(tmp_path)
    target_days = _weekdays(80)
    original = planner._layered_history_targets

    def _history_through_boundary(**kwargs: Any) -> dict[str, Any]:
        result = original(**kwargs)
        rows = [
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": f"{index / 100000:.12f}",
                "evidence_layer": "ACCOUNT_LIVE",
                "return_basis": "NET",
                "return_type": "TOTAL",
                "cost": "",
            }
            for series_id in ("QSI", "TT Portfolio")
            for index, observed in enumerate(target_days, 1)
        ]
        content = planner._csv_bytes(
            planner.TARGET_HEADERS,
            [[row[header] for header in planner.TARGET_HEADERS] for row in rows],
        )
        result["rows"] = rows
        result["target_content"] = content
        result["contents"]["selected-targets.csv"] = content
        result["identity"]["selected_targets_sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        return result

    monkeypatch.setattr(planner, "_layered_history_targets", _history_through_boundary)
    market_path = inputs["market_paths"]["us_factors"]
    rows = list(csv.reader(market_path.read_text(encoding="utf-8").splitlines()))
    rows[-1][2] = ""
    with market_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    catalog_sha256 = hashlib.sha256(inputs["catalog_path"].read_bytes()).hexdigest()
    _write_json(
        inputs["public_factor_manifest_path"],
        _public_factor_manifest(
            catalog_sha256,
            inputs["market_paths"],
            date.fromisoformat(inputs["evaluation_date"]),
        ),
    )
    with pytest.raises(planner.CurrentPlanError, match="missing required cell"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_target_observations_cannot_extend_beyond_evaluation_date(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    with pytest.raises(planner.CurrentPlanError, match="exceeds the evaluation date"):
        planner._layered_history_targets(
            generation_root=inputs["layered_history_generation_path"],
            contract_path=inputs["financial_mvp_contract_path"],
            strategies=planner._resolved_strategies(inputs["plan_document"]),
            evidence_mode="LIVE_ONLY",
            evaluated_on=date(2026, 1, 9),
        )


def test_build_rejects_missing_candidate_disposition_and_unknown_lens(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    document = inputs["plan_document"]
    document["strategies"][0]["assignments"][0]["factor_sets"] = [["MKT_RF"]]
    _write_json(inputs["plan_path"], document)
    with pytest.raises(planner.CurrentPlanError, match="disposition every candidate"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )

    inputs = _fixture(tmp_path / "second")
    document = inputs["plan_document"]
    document["sources"]["economic"]["lens"] = "MEGA_MODEL"
    _write_json(inputs["plan_path"], document)
    with pytest.raises(planner.CurrentPlanError, match="source lens is unsupported"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )

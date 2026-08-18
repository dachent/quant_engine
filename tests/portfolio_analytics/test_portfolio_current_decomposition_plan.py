from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
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


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> Path:
    path.write_bytes(_canonical(value))
    return path


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
        outputs.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "last_date": accepted_end.isoformat(),
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


def _fixture(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    days = _weekdays(80)
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
        "schema_version": 1,
        "split_policy": {
            "holdout_fraction": "0.20",
            "minimum_holdout_observations": 20,
            "minimum_training_observations_per_factor": 20,
            "cv_folds": 3,
            "parsimony_tolerance": "0.01",
            "ridge_candidates": ["0", "0.0001"],
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
                "strategy_id": "us",
                "series_id": "strategy-us",
                "default_disposition": "OUT_OF_SCOPE",
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
                "strategy_id": "style",
                "series_id": "strategy-style",
                "default_disposition": "OUT_OF_SCOPE",
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
    plan = _write_json(tmp_path / "plan.json", plan_document)
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

    assert manifest["policies"]["publication"] == "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE"
    assert manifest["evaluation_date"] == inputs["evaluation_date"]
    assert {item["analysis_lens"] for item in manifest["specs"]} == {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_SECTOR",
    }
    dispositions = json.loads(outputs["candidate-dispositions.json"])
    assert len(dispositions["candidate_dispositions"]) == 12
    economic = json.loads(outputs["specs/economic.json"])
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
        "specs/sector.json",
        "specs/style.json",
    ):
        result, manifest, _content = engine.build_decomposition(
            source_root / relative, source_root
        )
        assert manifest.generation_id
        assert len(result["model_selections"]) == 1


def test_plan_preserves_target_end_beyond_publication_lag(tmp_path: Path) -> None:
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

    target_path = inputs["base_source_root"] / "targets.csv"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(planner.TARGET_HEADERS)
        for series in ("strategy-us", "strategy-style"):
            for index, observed in enumerate(target_days, 1):
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
    base_spec = json.loads(inputs["base_spec_path"].read_text(encoding="utf-8"))
    base_spec["target_source"] = _definition(target_path, "target-fixture")
    _write_json(inputs["base_spec_path"], base_spec)

    outputs, _manifest = planner.build_bundle(
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

    assert economic["as_of_date"] == target_rows[-1]["date"]
    assert factor_rows[-1]["date"] == "2026-06-30"
    assert target_rows[-1]["date"] == "2026-08-17"
    assert economic["maximum_factor_staleness_days"] == 48

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


def test_public_manifest_cannot_be_reused_by_a_later_planning_run(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    inputs["evaluation_date"] = (
        date.fromisoformat(inputs["evaluation_date"]) + timedelta(days=1)
    ).isoformat()
    with pytest.raises(planner.CurrentPlanError, match="current planning run"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_public_factor_required_columns_must_be_complete_at_admitted_boundary(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
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
    with pytest.raises(planner.CurrentPlanError, match="incomplete at the admitted"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
        )


def test_target_observations_cannot_extend_beyond_evaluation_date(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    target_path = inputs["base_source_root"] / "targets.csv"
    rows = list(csv.reader(target_path.read_text(encoding="utf-8").splitlines()))
    rows[-1][1] = (
        date.fromisoformat(inputs["evaluation_date"]) + timedelta(days=1)
    ).isoformat()
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    base_spec = json.loads(inputs["base_spec_path"].read_text(encoding="utf-8"))
    base_spec["target_source"] = _definition(target_path, "target-fixture")
    _write_json(inputs["base_spec_path"], base_spec)
    with pytest.raises(planner.CurrentPlanError, match="beyond the evaluation date"):
        planner.build_bundle(
            **{key: value for key, value in inputs.items() if key != "plan_document"}
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

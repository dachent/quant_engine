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
                "lens": "RETURN_INDUSTRY",
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
    factor_outputs = []
    for key in ("us_factors", "europe_factors", "us_industries"):
        content = market_paths[key].read_bytes()
        factor_outputs.append(
            {
                "path": market_paths[key].name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    catalog_sha256 = hashlib.sha256(catalog.read_bytes()).hexdigest()
    factor_manifest = _write_json(
        tmp_path / "factor-manifest.json",
        {"catalog": {"sha256": catalog_sha256}, "outputs": factor_outputs},
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
        "plan_document": plan_document,
    }


def test_build_covers_candidates_and_emits_separate_lens_specs(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    outputs, manifest = planner.build_bundle(
        **{key: value for key, value in inputs.items() if key != "plan_document"}
    )

    assert manifest["policies"]["publication"] == "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE"
    assert {item["analysis_lens"] for item in manifest["specs"]} == {
        "ECONOMIC_FACTOR",
        "ETF_STYLE",
        "RETURN_INDUSTRY",
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


def test_build_rejects_manifest_drift(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    inputs["market_paths"]["us_factors"].write_text("changed", encoding="utf-8")
    with pytest.raises(planner.CurrentPlanError, match="differs from its manifest"):
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

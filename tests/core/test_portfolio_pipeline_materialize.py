from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Callable

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    inventory_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_pipeline_materialize.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_pipeline_materialize", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
materializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materializer)

AS_OF = date(2026, 8, 3)
ROLES = ("ledger", "baseline", "report", "panel")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload))


def _definition(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _dir_definition(path: Path) -> dict[str, object]:
    return {"path": str(path), **materializer._directory_identity(path)}


def _generation(root: Path, role: str, files: dict[str, object]) -> Path:
    seed = f"{role}\n".encode()
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity(f"synthetic-{role}", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {role}",
            fact_types=(role,),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{role}.txt",
                sha256=hashlib.sha256(seed).hexdigest(),
                size_bytes=len(seed),
                as_of_date=AS_OF,
                schema_name="synthetic",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = root / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    for relative, payload in files.items():
        destination = generation / relative
        if isinstance(payload, bytes):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        else:
            _write_json(destination, payload)
    return generation


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    output_root = tmp_path / "candidate"
    stages = output_root / "stages"
    hwm = {"nav_coverage": "1", "status": "AVAILABLE_GOVERNED_HWM_PERIMETER"}
    ledger = _generation(
        stages / "ledger",
        "ledger",
        {"provider-ledger.json": {"as_of_date": AS_OF.isoformat()}},
    )
    ledger_sha = hashlib.sha256(
        (ledger / "provider-ledger.json").read_bytes()
    ).hexdigest()
    allocations = [
        {
            "current_value": {"value": "410272.39"},
            "pool_id": "systematic-capital",
            "strategy_id": "strategy-1",
            "systematic_weight": "1",
        },
        {
            "current_value": {"value": "100043.62"},
            "pool_id": "reserve-capital",
            "strategy_id": None,
            "systematic_weight": None,
        },
    ]
    assignments = [
        {
            "effective_end": None,
            "pool_id": "systematic-capital",
            "strategy_id": "strategy-1",
            "strategy_mapping_disposition": "MAPPED",
        },
        {
            "effective_end": None,
            "pool_id": "reserve-capital",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
        },
    ]
    baseline_payload = {
        "allocations": allocations,
        "assignments": assignments,
        "provenance": {
            "catalog_sha256": ledger_sha,
            "generation_id": ledger.name,
        },
        "replay": {
            "reserve_total_usd": "100043.62",
            "systematic_total_usd": "410272.39",
            "total_liquid_usd": "510316.01",
        },
    }
    baseline = _generation(
        stages / "baseline",
        "baseline",
        {"portfolio-baseline.json": baseline_payload},
    )
    baseline_sha = hashlib.sha256(
        (baseline / "portfolio-baseline.json").read_bytes()
    ).hexdigest()
    report_payload = {
        "baseline": {
            "catalog_sha256": baseline_sha,
            "generation_id": baseline.name,
        },
        "hwm_summary": hwm,
        "strategy_summary": [
            {"strategy_id": f"strategy-{index}"} for index in range(9)
        ],
    }
    report = _generation(
        stages / "report",
        "report",
        {
            "portfolio-report-data.json": report_payload,
            "portfolio-report.xlsx": b"synthetic workbook",
            "workbook-validation.json": {"all_passed": True},
            "xlsx-win-result.json": {"final_state": "SUCCEEDED", "ok": True},
        },
    )
    analytics_ids = {
        "decomposition_generation_id": "d" * 64,
        "correlation_generation_id": "c" * 64,
        "weekly_returns_generation_id": "w" * 64,
    }
    panel_payload = {
        "hwm_summary": hwm,
        "meta": {
            "baseline_generation_id": baseline.name,
            "report_generation_id": report.name,
            **analytics_ids,
        },
        "strategies": [{"strategy_id": f"strategy-{index}"} for index in range(9)],
    }
    panel = _generation(
        stages / "panel",
        "panel",
        {
            "data/dashboard_data.json": panel_payload,
            "qa/interaction-results.json": {"all_passed": True},
            "qa/qa-validation.json": {"checks_passed": 8, "checks_total": 8},
        },
    )
    launcher = stages / "panel" / "index.html"
    launcher.write_text("<!doctype html><title>synthetic</title>", encoding="utf-8")
    _write_json(
        stages / "ledger" / "freshness-health.json",
        {"as_of_date": AS_OF.isoformat(), "assessments": []},
    )
    for key, root_name in (
        ("decomposition_generation_id", "decomposition"),
        ("correlation_generation_id", "correlation"),
        ("weekly_returns_generation_id", "weekly-returns"),
    ):
        _write_json(
            stages / "analytics" / root_name / "CURRENT.json",
            {"generation_id": analytics_ids[key]},
        )

    qa_dir = tmp_path / "qa"
    _write_json(qa_dir / "qa-validation.json", {"all_passed": True})
    source_files = []
    for name in ("provider.json", "baseline.json", "legacy.json"):
        path = tmp_path / name
        _write_json(path, {"synthetic": True})
        source_files.append(path)
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    _write_json(plan / "manifest.json", {"synthetic": True})
    _write_json(correlation / "spec.json", {"synthetic": True})
    vertical = {
        "as_of_date": AS_OF.isoformat(),
        "base_factor_file": _definition(source_files[0]),
        "baseline_spec": _definition(source_files[1]),
        "correlation_source_root": _dir_definition(correlation),
        "decomposition_plan_bundle": _dir_definition(plan),
        "legacy_panel": _definition(source_files[2]),
        "pipeline_id": "synthetic-vertical",
        "provider_ledger_spec": _definition(source_files[0]),
        "release_commit": "a" * 40,
        "schema_version": 2,
    }
    vertical_path = tmp_path / "vertical-recipe.json"
    _write_json(vertical_path, vertical)
    generations = {}
    for role, generation in zip(ROLES, (ledger, baseline, report, panel), strict=True):
        inventory = inventory_generation(generation)
        generations[role] = {
            "files": inventory["files"],
            "generation_id": inventory["generation_id"],
            "manifest_sha256": inventory["manifest_sha256"],
        }
    recipe = {
        "as_of_date": AS_OF.isoformat(),
        "expected_generations": generations,
        "panel_launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "pipeline_id": "synthetic-source-to-panel",
        "qa_dir": _dir_definition(qa_dir),
        "release_commit": "a" * 40,
        "schema_version": 2,
        "vertical_recipe": _definition(vertical_path),
    }
    recipe_path = tmp_path / "recipe.json"
    _write_json(recipe_path, recipe)
    return recipe_path, output_root


def test_semantic_reconciliation_calculates_all_six_bridges(tmp_path: Path) -> None:
    recipe, output_root = _fixture(tmp_path)

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"
    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text()
    )
    assert details["verdict"] == "PASS"
    assert set(details["checks"]) == {
        "analytics_report_panel_equality",
        "financial_identities",
        "freshness",
        "generation_invariants",
        "ledger_bridges",
        "mappings",
    }
    assert {row["status"] for row in details["checks"].values()} == {"PASS"}


Mutator = Callable[[Path], None]


def _mutate_json(path: Path, update: Callable[[dict[str, object]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    update(payload)
    _write_json(path, payload)


def _financial_failure(root: Path) -> None:
    baseline = next((root / "stages" / "baseline" / "generations").iterdir())
    _mutate_json(
        baseline / "portfolio-baseline.json",
        lambda payload: payload["replay"].update({"total_liquid_usd": "1"}),
    )


def _mapping_failure(root: Path) -> None:
    baseline = next((root / "stages" / "baseline" / "generations").iterdir())
    _mutate_json(
        baseline / "portfolio-baseline.json",
        lambda payload: payload["assignments"].append(
            {
                "effective_end": None,
                "pool_id": "systematic-capital",
                "strategy_id": None,
                "strategy_mapping_disposition": "CURRENT_POOL_ONLY",
            }
        ),
    )


def _ledger_failure(root: Path) -> None:
    baseline = next((root / "stages" / "baseline" / "generations").iterdir())
    _mutate_json(
        baseline / "portfolio-baseline.json",
        lambda payload: payload["provenance"].update({"catalog_sha256": "0" * 64}),
    )


def _equality_failure(root: Path) -> None:
    panel = next((root / "stages" / "panel" / "generations").iterdir())
    _mutate_json(
        panel / "data" / "dashboard_data.json",
        lambda payload: payload.update({"hwm_summary": {"status": "UNAVAILABLE"}}),
    )


def _freshness_failure(root: Path) -> None:
    _mutate_json(
        root / "stages" / "ledger" / "freshness-health.json",
        lambda payload: payload.update(
            {"assessments": [{"blocks_current_consumers": True}]}
        ),
    )


def _generation_failure(root: Path) -> None:
    panel = next((root / "stages" / "panel" / "generations").iterdir())
    (panel / "unexpected.txt").write_text("drift", encoding="utf-8")


@pytest.mark.parametrize(
    ("mutator", "failed_check"),
    [
        (_financial_failure, "financial_identities"),
        (_mapping_failure, "mappings"),
        (_ledger_failure, "ledger_bridges"),
        (_equality_failure, "analytics_report_panel_equality"),
        (_freshness_failure, "freshness"),
        (_generation_failure, "generation_invariants"),
    ],
)
def test_each_semantic_bridge_fails_closed(
    tmp_path: Path, mutator: Mutator, failed_check: str
) -> None:
    recipe, output_root = _fixture(tmp_path)
    mutator(output_root)

    with pytest.raises(materializer.MaterializationError, match=failed_check):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )

    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text()
    )
    assert details["checks"][failed_check]["status"] == "FAIL"
    assert not (output_root / "evidence" / "post-build-semantic.json").exists()


def test_source_drift_fails_closed_before_acquisition(tmp_path: Path) -> None:
    recipe_path, _ = _fixture(tmp_path)
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    vertical = Path(recipe["vertical_recipe"]["path"])
    vertical.write_text("changed", encoding="utf-8")

    with pytest.raises(materializer.MaterializationError, match="identity differs"):
        materializer.acquire(recipe_path, tmp_path / "candidate" / "runs" / "run")


def test_output_path_budget_rejects_an_overlong_release_root(tmp_path: Path) -> None:
    recipe_path, _ = _fixture(tmp_path)
    recipe = materializer._recipe(recipe_path)

    with pytest.raises(materializer.MaterializationError, match="path budget"):
        materializer._validate_output_path_budget(tmp_path / ("x" * 200), recipe)

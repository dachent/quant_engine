from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
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
    path.write_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )


def _generation(root: Path, role: str) -> GenerationManifest:
    content = f"{role}\n".encode()
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
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = root / role / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "payload.txt").write_bytes(content)
    if role == "report":
        workbook = generation / "portfolio-report.xlsx"
        workbook.write_bytes(b"synthetic workbook")
        _write_json(
            generation / "report-verification.json",
            {
                "report_id": "synthetic-report",
                "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            },
        )
        _write_json(
            generation / "workbook-validation.json",
            {"all_passed": True, "invariants": [{"passed": True}]},
        )
        native_result = '{"final_state":"SUCCEEDED","ok":true}'
        _write_json(
            generation / "xlsx-win-result.json",
            {str(index): character for index, character in enumerate(native_result)},
        )
    if role == "panel":
        _write_json(
            generation / "qa" / "qa-validation.json",
            {"checks_passed": 8, "checks_total": 8},
        )
        _write_json(
            generation / "qa" / "interaction-results.json",
            {"all_passed": True, "checks": [{"status": "PASS"}]},
        )
    return manifest


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    sources = tmp_path / "sources"
    manifests = {role: _generation(sources, role) for role in ROLES}
    source_file = tmp_path / "source.json"
    source_file.write_bytes(b'{"synthetic":true}')
    freshness = tmp_path / "freshness.json"
    _write_json(
        freshness,
        {
            "as_of_date": AS_OF.isoformat(),
            "assessments": [{"blocks_current_consumers": False}],
        },
    )
    launcher = tmp_path / "index.html"
    launcher.write_text("<!doctype html><title>synthetic</title>", encoding="utf-8")
    recipe: dict[str, object] = {
        "as_of_date": AS_OF.isoformat(),
        "freshness_health": str(freshness),
        "generations": {
            role: {
                "generation_id": manifests[role].generation_id,
                "source_dir": str(sources / role / manifests[role].generation_id),
            }
            for role in ROLES
        },
        "panel_launcher": str(launcher),
        "panel_launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "pipeline_id": "synthetic-portfolio-pipeline",
        "release_commit": "a" * 40,
        "schema_version": 1,
        "source_artifacts": [
            {
                "artifact_id": "source",
                "path": str(source_file),
                "sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "size_bytes": source_file.stat().st_size,
            }
        ],
    }
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_bytes(canonical_json_bytes(recipe))
    return recipe_path, recipe


def test_materialization_produces_exact_semantically_verified_outputs(
    tmp_path: Path,
) -> None:
    recipe_path, recipe = _fixture(tmp_path)
    output_root = tmp_path / "candidate"
    run_dir = output_root / "runs" / "run"

    materializer.acquire(recipe_path, run_dir)
    materializer.authority(
        recipe_path, run_dir / "source-run.json", output_root / "stages" / "authority"
    )
    for role in ROLES:
        materializer.materialize(recipe_path, role, output_root / "stages" / role)
    result = materializer.semantic(
        recipe_path, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result == {"state": "PASS"}
    evidence = json.loads(
        (output_root / "evidence" / "post-build-semantic.json").read_text()
    )
    assert evidence["verdict"] == "PASS"
    assert evidence["evidence"]["materialized_generation_count"] == 4
    panel_id = recipe["generations"]["panel"]["generation_id"]
    assert (output_root / "stages" / "panel" / "generations" / str(panel_id)).is_dir()


def test_source_drift_fails_closed_before_acquisition(tmp_path: Path) -> None:
    recipe_path, recipe = _fixture(tmp_path)
    source_path = Path(recipe["source_artifacts"][0]["path"])
    source_path.write_bytes(b"changed")

    with pytest.raises(
        materializer.MaterializationError, match="source artifact differs"
    ):
        materializer.acquire(recipe_path, tmp_path / "candidate" / "runs" / "run")


def test_output_path_budget_rejects_an_overlong_release_root(tmp_path: Path) -> None:
    _, recipe = _fixture(tmp_path)
    long_root = tmp_path / ("x" * 200)

    with pytest.raises(materializer.MaterializationError, match="path budget"):
        materializer._validate_output_path_budget(long_root, recipe)


def test_semantic_gate_rejects_failed_panel_qa(tmp_path: Path) -> None:
    recipe_path, recipe = _fixture(tmp_path)
    output_root = tmp_path / "candidate"
    run_dir = output_root / "runs" / "run"
    panel_source = Path(str(recipe["generations"]["panel"]["source_dir"]))
    _write_json(
        panel_source / "qa" / "qa-validation.json",
        {"checks_passed": 7, "checks_total": 8},
    )
    materializer.acquire(recipe_path, run_dir)
    materializer.authority(
        recipe_path, run_dir / "source-run.json", output_root / "stages" / "authority"
    )
    for role in ROLES:
        materializer.materialize(recipe_path, role, output_root / "stages" / role)
    with pytest.raises(materializer.MaterializationError, match="browser QA"):
        materializer.semantic(
            recipe_path, output_root / "evidence" / "post-build-semantic.json"
        )

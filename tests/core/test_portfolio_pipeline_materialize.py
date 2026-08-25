from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Callable

import pytest

from quant_engine.core import pipeline as core_pipeline
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    PipelineSpec,
    PipelineValidationError,
    SourceArtifact,
    VerificationStatus,
    inventory_generation,
    pipeline_status,
    plan_pipeline,
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
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "financial_mvp_fixture",
    ROOT / "tests" / "core" / "financial_mvp_fixture.py",
)
assert FIXTURE_SPEC is not None and FIXTURE_SPEC.loader is not None
financial_fixture = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = financial_fixture
FIXTURE_SPEC.loader.exec_module(financial_fixture)

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
    ledger_manifest_sha = hashlib.sha256(
        (ledger / "generation.json").read_bytes()
    ).hexdigest()
    allocations = [
        {
            "current_value": {"value": "410272.39"},
            "pool_id": "systematic-capital",
            "strategy_id": "strategy-1",
            "systematic_weight": "1.00000000000000003060016",
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
            "catalog_sha256": "e" * 64,
            "generation_id": "f" * 64,
        },
        "replay": {
            "reserve_total_usd": "100043.62",
            "systematic_total_usd": "410272.39",
            "total_liquid_usd": "510316.01",
        },
        "source_artifacts": [
            {
                "schema_name": "generation-manifest",
                "sha256": ledger_manifest_sha,
                "source_key": "provider-ledger-generation",
            },
            {
                "schema_name": "provider-ledger-catalog",
                "sha256": ledger_sha,
                "source_key": "provider-ledger-generation",
            },
        ],
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
    for name in (
        "provider.json",
        "baseline.json",
        "implementation.json",
        "implementation-return-attribution.json",
    ):
        path = tmp_path / name
        _write_json(path, {"synthetic": True})
        source_files.append(path)
    legacy = tmp_path / "legacy"
    _write_json(legacy / "data" / "dashboard_data.json", {"synthetic": True})
    (legacy / "app.js").write_text("// synthetic shell", encoding="utf-8")
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    attribution = tmp_path / "attribution"
    _write_json(plan / "manifest.json", {"synthetic": True})
    _write_json(correlation / "spec.json", {"synthetic": True})
    _write_json(attribution / "catalog.json", {"synthetic": True})
    vertical = {
        "as_of_date": AS_OF.isoformat(),
        "base_factor_file": _definition(source_files[0]),
        "baseline_spec": _definition(source_files[1]),
        "correlation_source_root": _dir_definition(correlation),
        "decomposition_plan_bundle": _dir_definition(plan),
        "implementation_reconciliation": _definition(source_files[2]),
        "implementation_return_attribution": _definition(source_files[3]),
        "legacy_panel": _dir_definition(legacy),
        "pipeline_id": "synthetic-vertical",
        "provider_ledger_spec": _definition(source_files[0]),
        "release_commit": "a" * 40,
        "schema_version": 6,
        "strategy_attribution_root": _dir_definition(attribution),
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
    for role, generation in zip(ROLES, (ledger, baseline, report, panel), strict=True):
        _write_json(
            output_root
            / "stage-results"
            / f"{materializer.GENERATION_STAGE[role]}.json",
            {"generation_id": generation.name},
        )
    return recipe_path, output_root


def _install_real_epoch_proof(proof_root: Path, target: Path) -> dict[str, Path]:
    """Install a real producer manifest and content-bound synthetic panel generation."""

    target.mkdir(parents=True, exist_ok=True)
    assignments = [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy",
            "effective_start": AS_OF.isoformat(),
            "effective_end": None,
        }
    ]
    old_baseline_id = json.loads(
        (proof_root / "stage-results" / "baseline.json").read_text(encoding="utf-8")
    )["generation_id"]
    old_baseline = json.loads(
        (
            proof_root
            / "stages"
            / "baseline"
            / "generations"
            / old_baseline_id
            / "portfolio-baseline.json"
        ).read_text(encoding="utf-8")
    )
    old_baseline.update(
        {
            "accounts": [{"account_id": "synthetic-account", "provider": "SYNTHETIC"}],
            "as_of_date": AS_OF.isoformat(),
            "assignments": assignments,
            "baseline_schema_version": 1,
        }
    )
    baseline_generation = _generation(
        proof_root / "stages" / "baseline",
        "epoch-baseline",
        {"portfolio-baseline.json": old_baseline},
    )
    old_baseline_generation = (
        proof_root / "stages" / "baseline" / "generations" / old_baseline_id
    )
    if old_baseline_generation != baseline_generation:
        shutil.rmtree(old_baseline_generation)
    _write_json(
        proof_root / "stage-results" / "baseline.json",
        {"generation_id": baseline_generation.name},
    )

    ledger_path = target / "account_strategy_ledger.json"
    _write_json(
        ledger_path,
        {
            "accounting_currency": "USD",
            "flows": [],
            "nav_observations": [
                {
                    "account_id": "synthetic-account",
                    "as_of_date": AS_OF.isoformat(),
                    "nav": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": "125.50",
                    },
                }
            ],
        },
    )
    ledger_identity_path = target / "account_strategy_ledger_identity.json"
    ledger_bytes = ledger_path.read_bytes()
    ledger_identity = {
        "schema_version": 1,
        "generation_id": "1" * 64,
        "artifact": {
            "artifact_id": "normalized/provider-ledger.json",
            "as_of_date": AS_OF.isoformat(),
            "schema": {"name": "provider-ledger", "version": "1"},
            "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
            "size_bytes": len(ledger_bytes),
        },
    }
    _write_json(ledger_identity_path, ledger_identity)
    classification_policy = json.loads(
        materializer.ACCOUNT_STRATEGY_CLASSIFICATION_POLICY.read_text(encoding="utf-8")
    )
    classified_flows_path = target / "account_strategy_classified_flows.json"
    _write_json(
        classified_flows_path,
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": AS_OF.isoformat(),
            "ledger_identity": {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            "classification_policy": {
                "policy_id": classification_policy["policy_id"],
                "policy_version": classification_policy["policy_version"],
                "policy_as_of_date": classification_policy["as_of_date"],
                "policy_sha256": hashlib.sha256(
                    materializer.ACCOUNT_STRATEGY_CLASSIFICATION_POLICY.read_bytes()
                ).hexdigest(),
            },
            "events": [],
        },
    )
    admission_policy_path = target / "account_strategy_admission_policy.json"
    assignment_sha256 = hashlib.sha256(
        json.dumps(assignments, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_json(
        admission_policy_path,
        {
            "schema_version": 1,
            "policy_id": "SYNTHETIC-ADMISSION",
            "policy_version": "1",
            "artifact_id": "policy/synthetic-admission.json",
            "as_of_date": AS_OF.isoformat(),
            "assignment_sha256": assignment_sha256,
            "admissions": [
                {
                    **assignments[0],
                    "status": "FUNDED",
                    "authority": "SYNTHETIC_TEST_AUTHORITY",
                    "continuity_to_successor": False,
                }
            ],
        },
    )
    inputs = {
        "account_strategy_ledger": ledger_path,
        "account_strategy_ledger_identity": ledger_identity_path,
        "account_strategy_classified_flows": classified_flows_path,
        "account_strategy_admission_policy": admission_policy_path,
    }
    producer = materializer._account_strategy_manifest_module()
    manifest = producer.build_manifest_candidate(
        ledger_path=ledger_path,
        ledger_identity_path=ledger_identity_path,
        baseline_root=proof_root / "stages" / "baseline",
        baseline_generation_id=baseline_generation.name,
        classified_flows_path=classified_flows_path,
        classification_policy_path=(
            materializer.ACCOUNT_STRATEGY_CLASSIFICATION_POLICY
        ),
        admission_policy_path=admission_policy_path,
    )
    staged_path = proof_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged_bytes = canonical_json_bytes(manifest)
    staged_path.write_bytes(staged_bytes)
    verification = {
        "method": "EXACT_REBUILD_REPORT_BASELINE_CANONICAL_POLICY_AND_TEMPLATE_BOUND",
        "producer_readiness": manifest["authority_readiness"],
        "publication_readiness": "READY",
        "status": "PANEL_INDEPENDENT_EXACT_REBUILD_VERIFIED",
    }
    dashboard = {
        "account_manifest": manifest,
        "meta": {
            "account_manifest_authority_profile": "INDEPENDENT_EXACT_REBUILD",
            "account_manifest_sha256": hashlib.sha256(staged_bytes).hexdigest(),
            "account_manifest_size_bytes": len(staged_bytes),
            "account_manifest_verification": verification,
        },
    }
    dashboard_bytes = canonical_json_bytes(dashboard)
    candidate = {
        "account_manifest_authority_profile": "INDEPENDENT_EXACT_REBUILD",
        "account_manifest_verification": verification,
        "artifact_sha256": {
            "data/dashboard_data.json": hashlib.sha256(dashboard_bytes).hexdigest()
        },
    }
    candidate_bytes = canonical_json_bytes(candidate)
    artifacts = [
        SourceArtifact(
            artifact_id="input/account-strategy-manifest.json",
            sha256=hashlib.sha256(staged_bytes).hexdigest(),
            size_bytes=len(staged_bytes),
            as_of_date=AS_OF,
            schema_name="account-strategy-manifest",
            schema_version="4",
        ),
        SourceArtifact(
            artifact_id="panel/data/dashboard_data.json",
            sha256=hashlib.sha256(dashboard_bytes).hexdigest(),
            size_bytes=len(dashboard_bytes),
            as_of_date=AS_OF,
            schema_name="portfolio-control-panel-artifact",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="panel/candidate.json",
            sha256=hashlib.sha256(candidate_bytes).hexdigest(),
            size_bytes=len(candidate_bytes),
            as_of_date=AS_OF,
            schema_name="portfolio-control-panel-artifact",
            schema_version="1",
        ),
    ]
    for key, artifact_id in {
        "account_strategy_ledger": "input-bound/manifest_ledger",
        "account_strategy_ledger_identity": "input-bound/manifest_ledger_identity",
        "account_strategy_classified_flows": "input-bound/manifest_classified_flows",
        "account_strategy_admission_policy": "input-bound/manifest_admission_policy",
    }.items():
        content = inputs[key].read_bytes()
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="portfolio-control-panel-bound-input",
                schema_version="1",
            )
        )
    panel_manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("portfolio-control-panel-builder", "2.11.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="honest synthetic epoch seam without provider or native Excel",
            fact_types=("portfolio control panel",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    generation = (
        proof_root / "stages" / "panel" / "generations" / panel_manifest.generation_id
    )
    for existing in generation.parent.iterdir():
        if existing != generation:
            shutil.rmtree(existing)
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(panel_manifest.to_json_bytes())
    (generation / "data").mkdir()
    (generation / "data" / "dashboard_data.json").write_bytes(dashboard_bytes)
    (generation / "candidate.json").write_bytes(candidate_bytes)
    _write_json(
        proof_root / "stage-results" / "panel.json",
        {"generation_id": panel_manifest.generation_id},
    )
    return inputs


def test_create_recipe_rejects_current_schema_v6_without_product_authority(
    tmp_path: Path,
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    output = tmp_path / "created-pipeline-recipe.json"

    with pytest.raises(
        materializer.MaterializationError,
        match="requires financial MVP contract and source matrix",
    ):
        materializer.create_recipe(
            vertical_recipe=Path(source["vertical_recipe"]["path"]),
            proof_root=proof_root,
            qa_dir=Path(source["qa_dir"]["path"]),
            pipeline_id="schema-v6-release",
            output=output,
        )


def test_schema_v7_provider_generation_is_discoverable_but_not_executable_without_product(
    tmp_path: Path,
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    provider_generation = tmp_path / "provider-ledger-generation"
    _write_json(provider_generation / "catalog.json", {"synthetic": True})
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(provider_generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    source["vertical_recipe"] = _definition(vertical_path)
    _write_json(source_recipe, source)
    output = tmp_path / "created-schema-v7-pipeline-recipe.json"

    with pytest.raises(
        materializer.MaterializationError,
        match="requires financial MVP contract and source matrix",
    ):
        materializer.create_recipe(
            vertical_recipe=vertical_path,
            proof_root=proof_root,
            qa_dir=Path(source["qa_dir"]["path"]),
            pipeline_id="schema-v7-release",
            output=output,
        )
    paths = materializer._vertical_source_paths(vertical_path)
    assert provider_generation in paths
    assert Path(vertical["base_factor_file"]["path"]) in paths


def test_create_recipe_binds_financial_mvp_authority_and_admission_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    generation = product.generation
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    output = tmp_path / "financial-mvp-pipeline-recipe.json"

    result = materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="financial-mvp-composition",
        output=output,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )

    accepted = materializer._recipe(output)
    assert result["recipe"] == str(output)
    assert accepted["schema_version"] == 3
    assert accepted["product_authority"]["conformance"]["readiness"] == (
        "REQUIRED_NOT_READY"
    )
    assert (
        accepted["product_authority"]["conformance"]["provider_admission"][
            "receipt_sha256"
        ]
        == hashlib.sha256(
            (generation / "admit-or-declare-receipt.json").read_bytes()
        ).hexdigest()
    )
    monkeypatch.setenv("QE_PIPELINE_PYTHON", sys.executable)
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        materializer, "_validate_output_path_budget", lambda _root, _recipe: None
    )
    spec_path = tmp_path / "financial-mvp-dag" / "pipeline-spec.json"

    materializer.build_spec(output, tmp_path / "financial-mvp-dag", spec_path)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    PipelineSpec.from_json_bytes(spec_path.read_bytes())
    assert {
        "financial-mvp-contract-authority",
        "financial-mvp-hwm-input-authority",
        "financial-mvp-product-conformance",
        "financial-mvp-provider-admission-authority",
        "financial-mvp-provider-catalog-authority",
        "financial-mvp-provider-manifest-authority",
        "financial-mvp-source-matrix-authority",
        "portfolio-orchestration-contract",
        "post-build-semantic",
    } <= {row["gate_id"] for row in spec["gates"]}
    hwm_gate = next(
        gate
        for gate in spec["gates"]
        if gate["gate_id"] == "financial-mvp-hwm-input-authority"
    )
    assessment = json.loads(Path(hwm_gate["path"]).read_text(encoding="utf-8"))
    assert assessment["schema_version"] == 1
    assert assessment["assessment_scope"] == (
        "CURRENT_PRODUCT_AND_PROVIDER_ADMISSION_ONLY"
    )
    assert assessment["outcome_id"] == "AS_HELD_COMPOSITE_HWM"
    assert assessment["state"] == "REQUIRED_NOT_READY"
    assert assessment["input_generation_sha256"] is None
    assert assessment["calendar_authority_sha256"] is None
    assert assessment["outcome_readiness"] == "NOT_DECLARED"
    assert assessment["required_strategy_ids"] == ["strategy-1"]
    assert assessment["unresolved_source_ids"] == []
    assert assessment["blocking_reasons"] == [
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
        "OUTCOME:AS_HELD_COMPOSITE_HWM",
    ]
    assert (
        assessment["authority_hashes"]["provider_receipt_sha256"]
        == (accepted["product_authority"]["provider_generation"]["receipt"]["sha256"])
    )
    assert (
        hashlib.sha256(canonical_json_bytes(assessment)).hexdigest()
        == hwm_gate["sha256"]
    )
    missing_source_authority = json.loads(
        canonical_json_bytes(accepted["product_authority"])
    )
    missing_source = missing_source_authority["conformance"]["sources"][0]
    missing_source["consumers"] = sorted(
        {*missing_source["consumers"], "AS_HELD_COMPOSITE_HWM"}
    )
    missing_source["capture_status"] = "NOT_CAPTURED"
    missing_source["existence_status"] = "DOES_NOT_EXIST"
    missing_source["readiness"] = "REQUIRED_NOT_READY"
    missing_source["blockers"] = [324]
    missing_source["current_generation"] = {
        "as_of": None,
        "generation_id": None,
        "sha256": None,
    }
    missing_source_assessment = materializer._hwm_input_authority_assessment(
        missing_source_authority
    )
    assert missing_source_assessment["unresolved_source_ids"] == [
        missing_source["source_id"]
    ]
    assert missing_source_assessment["blocking_reasons"] == [
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
        "OUTCOME:AS_HELD_COMPOSITE_HWM",
        f"SOURCE:{missing_source['source_id']}",
    ]
    orchestration = json.loads(
        spec_path.with_name("portfolio-orchestration-contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert orchestration["schema_version"] == 2
    assert orchestration["product_authority"]["schema_version"] == 3
    assert orchestration["product_authority"] == materializer._product_gate_payload(
        accepted["product_authority"]
    )
    assert str(tmp_path) not in json.dumps(
        orchestration["product_authority"], sort_keys=True
    )


def test_schema_v8_rebinds_exact_epoch_manifest_inputs_into_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The materializer must not let the builder substitute financial inputs."""

    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 8
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v8-pipeline-recipe.json"

    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v8-financial-manifest",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )

    accepted = materializer._recipe(pipeline_recipe)
    assert accepted["financial_epoch_manifest_inputs"] == {
        key: _definition(path) for key, path in manifest_inputs.items()
    }
    with pytest.raises(
        materializer.MaterializationError,
        match="post-build epoch-manifest proof is required",
    ):
        materializer._semantic_gate_payload(accepted)
    post_build_proof = materializer._financial_epoch_manifest_proof(
        accepted, proof_root
    )
    assert post_build_proof == accepted["financial_epoch_manifest_proof"]
    semantic_gate = materializer._semantic_gate_payload(
        accepted, epoch_manifest_proof=post_build_proof
    )
    assert semantic_gate["checks"]["epoch_manifest_input_authority"] == "PASS"
    assert (
        semantic_gate["financial_epoch_manifest_proof_sha256"]
        == hashlib.sha256(canonical_json_bytes(post_build_proof)).hexdigest()
    )
    manifest_inputs_sha256 = hashlib.sha256(
        canonical_json_bytes(accepted["financial_epoch_manifest_inputs"])
    ).hexdigest()
    source_payload = materializer._source_payload(accepted)
    assert {str(path) for path in manifest_inputs.values()} <= {
        str(row["path"]) for row in source_payload["artifacts"]
    }
    acquisition_dir = tmp_path / "source-run"
    materializer.acquire(pipeline_recipe, acquisition_dir)
    materializer.authority(
        pipeline_recipe,
        acquisition_dir / "source-run.json",
        tmp_path / "authority",
    )
    authority_snapshot = json.loads(
        (tmp_path / "authority" / "authority-snapshot.json").read_text(encoding="utf-8")
    )
    assert (
        authority_snapshot["financial_epoch_manifest_inputs_sha256"]
        == manifest_inputs_sha256
    )

    monkeypatch.setenv("QE_PIPELINE_PYTHON", sys.executable)
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        materializer, "_validate_output_path_budget", lambda _root, _recipe: None
    )
    output_root = tmp_path / "schema-v8-dag"
    spec_path = output_root / "pipeline-spec.json"
    materializer.build_spec(pipeline_recipe, output_root, spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    nodes = {node["node_id"]: node for node in spec["nodes"]}
    expected_flags = {
        "--manifest-ledger": manifest_inputs["account_strategy_ledger"],
        "--manifest-ledger-identity": manifest_inputs[
            "account_strategy_ledger_identity"
        ],
        "--manifest-classified-flows": manifest_inputs[
            "account_strategy_classified_flows"
        ],
        "--manifest-admission-policy": manifest_inputs[
            "account_strategy_admission_policy"
        ],
    }
    for role in materializer.STAGES:
        command = nodes[f"build-{role}"]
        for flag, path in expected_flags.items():
            index = command["argv"].index(flag)
            assert command["argv"][index + 1] == str(path)
            assert str(path) in command["declared_reads"]
    assert {str(path) for path in manifest_inputs.values()} <= set(
        nodes["acquire-local-sources"]["declared_reads"]
    )
    assert "financial-epoch-manifest-input-authority" in {
        row["gate_id"] for row in spec["gates"]
    }
    manifest_gate = json.loads(
        (output_root / "financial-epoch-manifest-input-authority.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        manifest_gate["post_build_proof_sha256"]
        == semantic_gate["financial_epoch_manifest_proof_sha256"]
    )
    built_spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    monkeypatch.setattr(
        core_pipeline,
        "_git_output",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    core_pipeline.plan_pipeline(built_spec)
    orchestration_gate_row = next(
        row
        for row in spec["gates"]
        if row["gate_id"] == "portfolio-orchestration-contract"
    )
    orchestration_path = Path(orchestration_gate_row["path"])
    orchestration_bytes = orchestration_path.read_bytes()
    orchestration = json.loads(orchestration_bytes)
    assert orchestration["schema_version"] == 3
    orchestration["financial_epoch_manifest_proof_sha256"] = "f" * 64
    tampered_orchestration_bytes = canonical_json_bytes(orchestration)
    orchestration_path.write_bytes(tampered_orchestration_bytes)
    tampered_spec_payload = json.loads(spec_path.read_text(encoding="utf-8"))
    next(
        row
        for row in tampered_spec_payload["gates"]
        if row["gate_id"] == "portfolio-orchestration-contract"
    )["sha256"] = hashlib.sha256(tampered_orchestration_bytes).hexdigest()
    tampered_spec = PipelineSpec.from_json_bytes(
        canonical_json_bytes(tampered_spec_payload)
    )
    with pytest.raises(
        PipelineValidationError,
        match="portfolio financial epoch manifest authority differs",
    ):
        core_pipeline.plan_pipeline(tampered_spec)
    orchestration_path.write_bytes(orchestration_bytes)
    built_semantic_gate = next(
        gate for gate in built_spec.gates if gate.gate_id == "post-build-semantic"
    )
    _write_json(built_semantic_gate.path, semantic_gate)
    core_pipeline._verify_post_build_semantic_gate(  # noqa: SLF001
        built_spec, built_semantic_gate.path
    )
    tampered_semantic = dict(semantic_gate)
    tampered_semantic["financial_epoch_manifest_proof_sha256"] = "f" * 64
    _write_json(built_semantic_gate.path, tampered_semantic)
    with pytest.raises(
        PipelineValidationError,
        match="financial epoch manifest semantic identities differ",
    ):
        core_pipeline._verify_post_build_semantic_gate(  # noqa: SLF001
            built_spec, built_semantic_gate.path
        )
    _write_json(built_semantic_gate.path, semantic_gate)

    staged_path = proof_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged_bytes = staged_path.read_bytes()
    staged = json.loads(staged_bytes)
    staged["rows"][0]["current_value"] = "999.99"
    staged_path.write_bytes(canonical_json_bytes(staged))
    with pytest.raises(
        materializer.MaterializationError,
        match="panel artifact binding differs|staged.*rebuild",
    ):
        materializer._financial_epoch_manifest_proof(accepted, proof_root)
    staged_path.write_bytes(staged_bytes)

    staged = json.loads(staged_bytes)
    staged["source_identities"]["ledger"]["generation_id"] = "f" * 64
    staged_path.write_bytes(canonical_json_bytes(staged))
    with pytest.raises(
        materializer.MaterializationError,
        match="panel artifact binding differs|staged.*rebuild",
    ):
        materializer._financial_epoch_manifest_proof(accepted, proof_root)
    staged_path.write_bytes(staged_bytes)

    panel_generation = materializer._generation_dir(proof_root, accepted, "panel")
    dashboard_path = panel_generation / "data" / "dashboard_data.json"
    dashboard_bytes = dashboard_path.read_bytes()
    dashboard = json.loads(dashboard_bytes)
    dashboard["account_manifest"]["rows"][0]["status"] = "BLOCKED"
    dashboard_path.write_bytes(canonical_json_bytes(dashboard))
    with pytest.raises(
        materializer.MaterializationError, match="panel artifact binding differs"
    ):
        materializer._financial_epoch_manifest_proof(accepted, proof_root)
    dashboard_path.write_bytes(dashboard_bytes)

    generation_path = panel_generation / "generation.json"
    generation_bytes = generation_path.read_bytes()
    generation = json.loads(generation_bytes)
    generation["verification"] = "UNVERIFIED"
    generation_path.write_bytes(canonical_json_bytes(generation))
    with pytest.raises(materializer.MaterializationError, match="generation"):
        materializer._financial_epoch_manifest_proof(accepted, proof_root)
    generation_path.write_bytes(generation_bytes)

    forged_proof = json.loads(json.dumps(post_build_proof))
    forged_proof["epoch_row_count"] += 1
    with pytest.raises(
        materializer.MaterializationError,
        match="post-build epoch-manifest proof differs",
    ):
        materializer._semantic_gate_payload(accepted, epoch_manifest_proof=forged_proof)


def test_schema_v8_declares_the_authority_snapshot_it_emits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 8
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v8-pipeline-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v8-authority-snapshot",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )
    acquisition_dir = tmp_path / "source-run"
    materializer.acquire(pipeline_recipe, acquisition_dir)
    authority_dir = tmp_path / "authority"
    materializer.authority(
        pipeline_recipe, acquisition_dir / "source-run.json", authority_dir
    )
    monkeypatch.setenv("QE_PIPELINE_PYTHON", sys.executable)
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        materializer, "_validate_output_path_budget", lambda _root, _recipe: None
    )
    output_root = tmp_path / "schema-v8-dag"
    spec_path = output_root / "pipeline-spec.json"
    materializer.build_spec(pipeline_recipe, output_root, spec_path)

    declared = next(
        node
        for node in json.loads(spec_path.read_text(encoding="utf-8"))["nodes"]
        if node["node_id"] == "authority-snapshot"
    )
    emitted = (authority_dir / "authority-snapshot.json").read_bytes()
    assert declared["sha256"] == hashlib.sha256(emitted).hexdigest()
    assert declared["size_bytes"] == len(emitted)


def test_schema_v8_rejects_epoch_manifest_input_drift_after_recipe_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 8
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v8-pipeline-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v8-financial-manifest",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )
    _write_json(
        tmp_path / "epoch-manifest" / "account_strategy_classified_flows.json",
        {"input": "account_strategy_classified_flows", "version": 2},
    )

    with pytest.raises(
        materializer.MaterializationError,
        match="financial epoch manifest account_strategy_classified_flows identity differs",
    ):
        materializer._recipe(pipeline_recipe)


def test_schema_v8_rejects_missing_required_epoch_manifest_input(
    tmp_path: Path,
) -> None:
    source_recipe, _proof_root = _fixture(tmp_path)
    vertical_path = Path(
        json.loads(source_recipe.read_text(encoding="utf-8"))["vertical_recipe"]["path"]
    )
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    provider_generation = tmp_path / "provider-ledger-generation"
    _write_json(provider_generation / "catalog.json", {"synthetic": True})
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(provider_generation)
    vertical["schema_version"] = 8

    _write_json(vertical_path, vertical)

    with pytest.raises(
        materializer.MaterializationError,
        match="financial epoch manifest schema-8 inputs differ",
    ):
        materializer._vertical_source_paths(vertical_path)


def test_schema_v8_rejects_any_extra_financial_input_definition(
    tmp_path: Path,
) -> None:
    source_recipe, _proof_root = _fixture(tmp_path)
    vertical_path = Path(
        json.loads(source_recipe.read_text(encoding="utf-8"))["vertical_recipe"]["path"]
    )
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    provider_generation = tmp_path / "provider-ledger-generation"
    _write_json(provider_generation / "catalog.json", {"synthetic": True})
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(provider_generation)
    for key in materializer.EPOCH_MANIFEST_INPUTS:
        path = tmp_path / "epoch-manifest" / f"{key}.json"
        _write_json(path, {"input": key})
        vertical[key] = _definition(path)
    extra = tmp_path / "epoch-manifest" / "unapproved.json"
    _write_json(extra, {"input": "unapproved"})
    vertical["unapproved_financial_input"] = _definition(extra)
    vertical["schema_version"] = 8
    _write_json(vertical_path, vertical)

    with pytest.raises(
        materializer.MaterializationError,
        match="financial epoch manifest schema-8 inputs differ",
    ):
        materializer._vertical_source_paths(vertical_path)


def test_financial_mvp_authority_rejects_receipt_outside_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_recipe, _proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    generation = product.generation
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    (generation / "admit-or-declare-receipt.json").write_bytes(
        canonical_json_bytes({"admission_gate_status": "PASS"})
    )

    with pytest.raises(materializer.MaterializationError, match="identity differs"):
        materializer._derive_product_authority(
            vertical_path,
            product.contract_path,
            product.matrix_path,
            {
                "ledger": product.generation.name,
                "baseline": "b" * 64,
                "report": "c" * 64,
                "panel": "d" * 64,
            },
        )


def _runtime_registry(
    tmp_path: Path,
    *,
    name: str,
    semantic_sha256: str,
    census_sha256: str,
    records: list[dict[str, object]],
) -> dict[str, object]:
    schema_name, record_key = {
        "corrections": ("portfolio-correction-registry", "corrections"),
        "rederive": ("portfolio-rederive-receipt-registry", "receipts"),
    }[name]
    payload: dict[str, object] = {
        "census_sha256": census_sha256,
        "contract_semantic_sha256": semantic_sha256,
        record_key: records,
        "schema_name": schema_name,
        "schema_version": 1,
    }
    payload["registry_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    path = tmp_path / f"{name}.json"
    _write_json(path, payload)
    return materializer._authority_registry(
        path, schema_name=schema_name, record_key=record_key
    )


def _fixture_conformance(
    product: object,
    correction_registry: dict[str, object],
    rederive_registry: dict[str, object],
) -> dict[str, object]:
    contract = product.contract
    contract_bytes = product.contract_path.read_bytes()
    semantic_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    census_sha256 = hashlib.sha256(
        canonical_json_bytes(contract["current_census"])
    ).hexdigest()
    admission = materializer._provider_admission_authority(
        product.generation,
        contract,
        contract_raw_sha256=semantic_sha256,
        contract_semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
    )
    return materializer._product_conformance(
        contract,
        admission,
        correction_registry,
        rederive_registry,
        candidate_generation_ids={
            "ledger": product.generation.name,
            "baseline": "b" * 64,
            "report": "c" * 64,
            "panel": "d" * 64,
        },
        contract_raw_sha256=semantic_sha256,
        contract_semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        source_matrix_sha256=hashlib.sha256(product.matrix_bytes).hexdigest(),
        invested_capital_policy_sha256=hashlib.sha256(
            canonical_json_bytes({"policy_id": "SYNTHETIC_INVESTED_CAPITAL"})
        ).hexdigest(),
    )


def test_correction_identity_invalidates_only_its_dag_closure(tmp_path: Path) -> None:
    product = financial_fixture.build_financial_mvp_fixture(
        tmp_path / "product", ready=True
    )
    semantic_sha256 = hashlib.sha256(product.contract_path.read_bytes()).hexdigest()
    census_sha256 = hashlib.sha256(
        canonical_json_bytes(product.contract["current_census"])
    ).hexdigest()
    empty = _runtime_registry(
        tmp_path / "empty",
        name="corrections",
        semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        records=[],
    )
    correction = _runtime_registry(
        tmp_path / "changed",
        name="corrections",
        semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        records=[
            {
                "applicability": "NOT_APPLICABLE",
                "correction_id": "unrelated-report-correction",
                "disposition_sha256": None,
                "evidence_sha256": "1" * 64,
                "invalidated_outputs": ["REPORT"],
                "rebuild_boundary": "REPORT",
                "status": "OPEN",
                "subject_ids": ["unrelated-strategy"],
            }
        ],
    )
    absent_rederive = materializer._authority_registry(
        None,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    baseline = _fixture_conformance(product, empty, absent_rederive)
    changed = _fixture_conformance(product, correction, absent_rederive)
    baseline_ids = {
        row["stage"]: row["semantic_input_sha256"]
        for row in baseline["stage_identities"]
    }
    changed_ids = {
        row["stage"]: row["semantic_input_sha256"]
        for row in changed["stage_identities"]
    }

    assert changed["readiness"] == "READY_FOR_EXECUTION"
    assert [
        stage
        for stage in materializer.STAGES
        if baseline_ids[stage] != changed_ids[stage]
    ] == ["report", "workbook", "panel"]
    assert all(
        baseline_ids[stage] == changed_ids[stage]
        for stage in ("ledger", "baseline", "attribution", "analytics")
    )


def test_applicable_correction_and_rederive_receipt_are_runtime_authorities(
    tmp_path: Path,
) -> None:
    product = financial_fixture.build_financial_mvp_fixture(
        tmp_path / "product", ready=True
    )
    semantic_sha256 = hashlib.sha256(product.contract_path.read_bytes()).hexdigest()
    census_sha256 = hashlib.sha256(
        canonical_json_bytes(product.contract["current_census"])
    ).hexdigest()
    open_registry = _runtime_registry(
        tmp_path / "open",
        name="corrections",
        semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        records=[
            {
                "applicability": "APPLICABLE",
                "correction_id": "correction-1",
                "disposition_sha256": None,
                "evidence_sha256": "2" * 64,
                "invalidated_outputs": ["REPORT"],
                "rebuild_boundary": "REPORT",
                "status": "OPEN",
                "subject_ids": ["strategy-1"],
            }
        ],
    )
    absent_rederive = materializer._authority_registry(
        None,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    open_conformance = _fixture_conformance(product, open_registry, absent_rederive)
    assert "OPEN_CORRECTION:correction-1" in open_conformance["blocking_reasons"]

    resolved_records = [
        {
            **open_registry["records"][0],
            "disposition_sha256": "3" * 64,
            "status": "RESOLVED",
        }
    ]
    resolved_registry = _runtime_registry(
        tmp_path / "resolved",
        name="corrections",
        semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        records=resolved_records,
    )
    unresolved = _fixture_conformance(product, resolved_registry, absent_rederive)
    assert "REDERIVE_AUTHORITY_REQUIRED" in unresolved["blocking_reasons"]
    assert "MISSING_REDERIVATION:correction-1" in unresolved["blocking_reasons"]

    rederive_payload = {
        "correction_id": "correction-1",
        "input_generation_id": product.generation.name,
        "invalidated_outputs": ["REPORT"],
        "output_generation_id": "c" * 64,
        "status": "VERIFIED",
    }
    rederive = _runtime_registry(
        tmp_path / "rederived",
        name="rederive",
        semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
        records=[
            {
                **rederive_payload,
                "receipt_sha256": hashlib.sha256(
                    canonical_json_bytes(rederive_payload)
                ).hexdigest(),
            }
        ],
    )
    accepted = _fixture_conformance(product, resolved_registry, rederive)
    assert accepted["blocking_reasons"] == []
    assert accepted["readiness"] == "READY_FOR_EXECUTION"

    for field, replacement in (
        ("input_generation_id", "7" * 64),
        ("output_generation_id", "8" * 64),
        ("receipt_sha256", "9" * 64),
    ):
        forged_record = dict(rederive["records"][0])
        forged_record[field] = replacement
        forged = {
            **rederive,
            "records": [forged_record],
        }
        with pytest.raises(
            materializer.MaterializationError,
            match="re-derivation receipt candidate binding differs",
        ):
            _fixture_conformance(product, resolved_registry, forged)


def test_generation_date_rule_allows_dated_ledger_only() -> None:
    earlier = date(2026, 7, 31)

    assert materializer._generation_as_of_allowed("ledger", earlier, AS_OF)
    assert not materializer._generation_as_of_allowed("baseline", earlier, AS_OF)
    assert not materializer._generation_as_of_allowed("ledger", date(2026, 8, 4), AS_OF)


def _hybrid_panel_lenses(analytics_ids: dict[str, str]) -> dict[str, object]:
    return {
        "modes": {
            "LIVE_PLUS_BACKTEST": [
                {
                    "generation_id": analytics_ids["hybrid-dynamic-style"],
                    "lens_id": "DYNAMIC_STYLE",
                },
                {
                    "lens_id": "ECONOMIC_FACTOR",
                    "sources": [
                        {
                            "generation_id": analytics_ids["hybrid-europe-economic"],
                            "source_key": "europe-economic",
                        },
                        {
                            "generation_id": analytics_ids["hybrid-us-economic"],
                            "source_key": "us-economic",
                        },
                    ],
                },
                {
                    "lens_id": "ETF_STYLE",
                    "sources": [
                        {
                            "generation_id": analytics_ids["hybrid-us-equity-style"],
                            "source_key": "us-equity-style",
                        }
                    ],
                },
                {
                    "lens_id": "RETURN_INDUSTRY",
                    "sources": [
                        {
                            "generation_id": analytics_ids["hybrid-us-industry"],
                            "source_key": "us-industry",
                        }
                    ],
                },
                {
                    "lens_id": "RETURN_SECTOR",
                    "sources": [
                        {
                            "generation_id": analytics_ids["hybrid-us-sector"],
                            "source_key": "us-sector",
                        }
                    ],
                },
            ]
        }
    }


def test_panel_hybrid_authority_requires_every_consumed_generation() -> None:
    analytics_ids = {
        root_id: str(index) * 64
        for index, root_id in enumerate(
            materializer.HYBRID_PANEL_ROOT_IDS.values(), start=1
        )
    }
    lenses = _hybrid_panel_lenses(analytics_ids)
    payload = {"decomposition_lenses": lenses}

    assert materializer._panel_hybrid_generation_ids(payload) == {
        root_id: analytics_ids[root_id]
        for root_id in materializer.HYBRID_PANEL_ROOT_IDS.values()
    }

    modes = lenses["modes"]
    assert isinstance(modes, dict)
    hybrid = modes["LIVE_PLUS_BACKTEST"]
    assert isinstance(hybrid, list)
    hybrid.pop()
    with pytest.raises(
        materializer.MaterializationError,
        match="prepared panel hybrid analytics authority differs",
    ):
        materializer._panel_hybrid_generation_ids(payload)


def test_production_spec_binds_stable_twenty_root_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_stage_roots = dict(materializer.PRODUCTION_STAGE_ROOTS)
    analytics_root_ids = [
        root_id
        for root_id in short_stage_roots
        if not root_id.startswith("portfolio-") and root_id != "provider-ledger"
    ]
    for index, root_id in enumerate(analytics_root_ids):
        short_stage_roots[root_id] = Path("stages") / "a" / str(index)
    monkeypatch.setattr(materializer, "PRODUCTION_STAGE_ROOTS", short_stage_roots)
    recipe_path, prepared_root = _fixture(tmp_path)
    recipe_value = json.loads(recipe_path.read_text(encoding="utf-8"))
    vertical_path = Path(recipe_value["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    admission_generation = product.generation
    staged_ledger_root = prepared_root / "stages" / "ledger" / "generations"
    for existing in staged_ledger_root.iterdir():
        shutil.rmtree(existing)
    staged_admission_generation = staged_ledger_root / admission_generation.name
    shutil.copytree(admission_generation, staged_admission_generation)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(admission_generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    recipe_value["vertical_recipe"] = _definition(vertical_path)
    recipe_value["product_authority"] = materializer._derive_product_authority(
        vertical_path,
        product.contract_path,
        product.matrix_path,
        {
            role: (
                admission_generation.name
                if role == "ledger"
                else str(recipe_value["expected_generations"][role]["generation_id"])
            )
            for role in materializer.RELEASE_ROLES
        },
    )
    recipe_value["schema_version"] = 3
    staged_admission_inventory = inventory_generation(staged_admission_generation)
    recipe_value["expected_generations"]["ledger"] = {
        "files": staged_admission_inventory["files"],
        "generation_id": staged_admission_inventory["generation_id"],
        "manifest_sha256": staged_admission_inventory["manifest_sha256"],
    }
    _write_json(recipe_path, recipe_value)
    analytics_ids: dict[str, str] = {}
    for root_id, relative in materializer.PRODUCTION_STAGE_ROOTS.items():
        if root_id.startswith("portfolio-") or root_id == "provider-ledger":
            continue
        staged_root = prepared_root / relative
        generation = _generation(
            staged_root,
            root_id,
            {"analytics.json": {"root_id": root_id}},
        )
        _write_json(staged_root / "CURRENT.json", {"generation_id": generation.name})
        analytics_ids[root_id] = generation.name
    panel = next((prepared_root / "stages" / "panel" / "generations").iterdir())

    def bind_panel_analytics(payload: dict[str, object]) -> None:
        meta = payload["meta"]
        assert isinstance(meta, dict)
        meta.update(
            {
                "correlation_generation_id": analytics_ids["correlation"],
                "decomposition_generation_id": analytics_ids["factor-decomposition"],
                "weekly_returns_generation_id": analytics_ids["weekly-returns"],
            }
        )
        payload["decomposition_lenses"] = _hybrid_panel_lenses(analytics_ids)

    _mutate_json(
        panel / "data" / "dashboard_data.json",
        bind_panel_analytics,
    )
    _refresh_expected_generation(recipe_path, prepared_root, "panel")
    _write_json(
        prepared_root / "evidence" / "post-build-semantic.json",
        materializer._semantic_gate_payload(materializer._recipe(recipe_path)),
    )
    _write_json(
        prepared_root / "portfolio-orchestration-contract.json",
        {"release_commit": "a" * 40, "state": "PASS"},
    )
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    production_root = tmp_path / "production"
    spec_path = tmp_path / "release" / "production-spec.json"

    result = materializer.build_production_spec(
        recipe_path,
        prepared_root,
        prepared_root / "evidence" / "post-build-semantic.json",
        production_root,
        tmp_path / "production-state",
        spec_path,
    )

    spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    assert result["root_count"] == 20
    assert set(spec.roots) == set(materializer.PRODUCTION_ROOTS)
    assert spec.coordination_root == production_root / "releases"
    assert (
        spec.release_generations["portfolio-panel"]
        == next((prepared_root / "stages" / "panel" / "generations").iterdir()).name
    )
    assert spec.release_generations["correlation"] == analytics_ids["correlation"]
    assert (
        spec.release_generations["hybrid-dynamic-style"]
        == analytics_ids["hybrid-dynamic-style"]
    )
    semantic_gate = next(
        gate for gate in spec.gates if gate.gate_id == "post-build-semantic"
    )
    semantic_payload = json.loads(semantic_gate.path.read_text(encoding="utf-8"))
    assert semantic_payload["release_generations"] == spec.release_generations
    product_gate = next(
        gate
        for gate in spec.gates
        if gate.gate_id == "financial-mvp-product-conformance"
    )
    production_authority = json.loads(product_gate.path.read_text(encoding="utf-8"))
    accepted_recipe = materializer._recipe(recipe_path)
    assert (
        production_authority["conformance"]
        == accepted_recipe["product_authority"]["conformance"]
    )
    assert "provider_generation_path" not in production_authority
    assert str(prepared_root) not in json.dumps(production_authority, sort_keys=True)
    production_contract = json.loads(
        Path(result["contract"]).read_text(encoding="utf-8")
    )
    assert production_contract["schema_version"] == 2
    assert production_contract["product_authority_sha256"] == product_gate.sha256
    production_plan = plan_pipeline(spec)
    assert production_plan["node_count"] == 0
    assert production_plan["commit_effects"] == []
    assert production_plan["financial_mvp"]["ready_for_execution"] is False

    pre_hwm = json.loads(spec_path.read_text(encoding="utf-8"))
    pre_hwm_product = dict(production_authority)
    pre_hwm_product["schema_version"] = 2
    pre_hwm_product_bytes = canonical_json_bytes(pre_hwm_product)
    product_gate.path.write_bytes(pre_hwm_product_bytes)
    pre_hwm_product_sha256 = hashlib.sha256(pre_hwm_product_bytes).hexdigest()
    next(
        row
        for row in pre_hwm["gates"]
        if row["gate_id"] == "financial-mvp-product-conformance"
    )["sha256"] = pre_hwm_product_sha256
    pre_hwm["gates"] = [
        row
        for row in pre_hwm["gates"]
        if row["gate_id"] != "financial-mvp-hwm-input-authority"
    ]
    pre_hwm_production_contract = dict(production_contract)
    pre_hwm_production_contract["product_authority_sha256"] = pre_hwm_product_sha256
    pre_hwm_production_bytes = canonical_json_bytes(pre_hwm_production_contract)
    Path(result["contract"]).write_bytes(pre_hwm_production_bytes)
    next(
        row
        for row in pre_hwm["gates"]
        if row["gate_id"] == "port-underwrite-production-rebind"
    )["sha256"] = hashlib.sha256(pre_hwm_production_bytes).hexdigest()
    pre_hwm_spec = PipelineSpec.from_json_bytes(canonical_json_bytes(pre_hwm))
    pre_hwm_status = pipeline_status(pre_hwm_spec)
    assert pre_hwm_status["financial_mvp"]["ready_for_execution"] is False
    assert "hwm_input_authority" not in pre_hwm_status["financial_mvp"]
    with pytest.raises(PipelineValidationError, match="recovery-only"):
        plan_pipeline(pre_hwm_spec)
    product_gate.path.write_bytes(canonical_json_bytes(production_authority))
    Path(result["contract"]).write_bytes(canonical_json_bytes(production_contract))

    rewritten = json.loads(spec_path.read_text(encoding="utf-8"))
    rewritten_contract = dict(production_contract)
    rewritten_contract["product_authority_sha256"] = "0" * 64
    rewritten_contract_bytes = canonical_json_bytes(rewritten_contract)
    Path(result["contract"]).write_bytes(rewritten_contract_bytes)
    next(
        row
        for row in rewritten["gates"]
        if row["gate_id"] == "port-underwrite-production-rebind"
    )["sha256"] = hashlib.sha256(rewritten_contract_bytes).hexdigest()
    with pytest.raises(
        PipelineValidationError, match="financial MVP production binding differs"
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(canonical_json_bytes(rewritten)))
    Path(result["contract"]).write_bytes(canonical_json_bytes(production_contract))

    rewritten = json.loads(spec_path.read_text(encoding="utf-8"))
    rewritten_contract = dict(production_contract)
    rewritten_contract["generation_ids"] = {
        **rewritten_contract["generation_ids"],
        "provider-ledger": "e" * 64,
    }
    next(
        row
        for row in rewritten["nodes"]
        if row.get("kind") == "GENERATION" and row.get("root_id") == "provider-ledger"
    )["generation_id"] = "e" * 64
    rewritten_contract_bytes = canonical_json_bytes(rewritten_contract)
    Path(result["contract"]).write_bytes(rewritten_contract_bytes)
    next(
        row
        for row in rewritten["gates"]
        if row["gate_id"] == "port-underwrite-production-rebind"
    )["sha256"] = hashlib.sha256(rewritten_contract_bytes).hexdigest()
    with pytest.raises(
        PipelineValidationError, match="financial MVP production binding differs"
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(canonical_json_bytes(rewritten)))
    Path(result["contract"]).write_bytes(canonical_json_bytes(production_contract))

    stripped = json.loads(spec_path.read_text(encoding="utf-8"))
    stripped["gates"] = [
        row
        for row in stripped["gates"]
        if not row["gate_id"].startswith("financial-mvp-")
    ]
    stripped_spec = PipelineSpec.from_json_bytes(canonical_json_bytes(stripped))
    with pytest.raises(PipelineValidationError, match="recovery-only"):
        plan_pipeline(stripped_spec)
    with pytest.raises(PipelineValidationError, match="recovery-only"):
        pipeline_status(stripped_spec)


def test_schema_v8_production_spec_binds_real_epoch_manifest_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-8 producer evidence must survive the production rebind seam."""

    short_stage_roots = dict(materializer.PRODUCTION_STAGE_ROOTS)
    analytics_root_ids = [
        root_id
        for root_id in short_stage_roots
        if not root_id.startswith("portfolio-") and root_id != "provider-ledger"
    ]
    for index, root_id in enumerate(analytics_root_ids):
        short_stage_roots[root_id] = Path("stages") / "a" / str(index)
    monkeypatch.setattr(materializer, "PRODUCTION_STAGE_ROOTS", short_stage_roots)

    source_recipe, prepared_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(
        prepared_root, tmp_path / "epoch-manifest"
    )
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 8
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    recipe_path = tmp_path / "schema-v8-production-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=prepared_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v8-production-seam",
        output=recipe_path,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )

    staged_ledger_root = prepared_root / "stages" / "ledger" / "generations"
    for existing in staged_ledger_root.iterdir():
        shutil.rmtree(existing)
    staged_admission_generation = staged_ledger_root / product.generation.name
    shutil.copytree(product.generation, staged_admission_generation)
    recipe = materializer._recipe(recipe_path)
    recipe["product_authority"] = materializer._derive_product_authority(
        vertical_path,
        product.contract_path,
        product.matrix_path,
        {
            role: (
                product.generation.name
                if role == "ledger"
                else str(recipe["expected_generations"][role]["generation_id"])
            )
            for role in materializer.RELEASE_ROLES
        },
    )
    staged_admission_inventory = inventory_generation(staged_admission_generation)
    recipe["expected_generations"]["ledger"] = {
        "files": staged_admission_inventory["files"],
        "generation_id": staged_admission_inventory["generation_id"],
        "manifest_sha256": staged_admission_inventory["manifest_sha256"],
    }
    _write_json(recipe_path, recipe)

    analytics_ids: dict[str, str] = {}
    for root_id, relative in materializer.PRODUCTION_STAGE_ROOTS.items():
        if root_id.startswith("portfolio-") or root_id == "provider-ledger":
            continue
        staged_root = prepared_root / relative
        generation = _generation(
            staged_root,
            root_id,
            {"analytics.json": {"root_id": root_id}},
        )
        _write_json(staged_root / "CURRENT.json", {"generation_id": generation.name})
        analytics_ids[root_id] = generation.name
    panel = next((prepared_root / "stages" / "panel" / "generations").iterdir())

    def bind_panel_analytics(payload: dict[str, object]) -> None:
        meta = payload["meta"]
        assert isinstance(meta, dict)
        meta.update(
            {
                "correlation_generation_id": analytics_ids["correlation"],
                "decomposition_generation_id": analytics_ids["factor-decomposition"],
                "weekly_returns_generation_id": analytics_ids["weekly-returns"],
            }
        )
        payload["decomposition_lenses"] = _hybrid_panel_lenses(analytics_ids)

    _mutate_json(panel / "data" / "dashboard_data.json", bind_panel_analytics)
    dashboard_path = panel / "data" / "dashboard_data.json"
    candidate_path = panel / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["artifact_sha256"]["data/dashboard_data.json"] = hashlib.sha256(
        dashboard_path.read_bytes()
    ).hexdigest()
    _write_json(candidate_path, candidate)
    panel_manifest = GenerationManifest.from_json_bytes(
        (panel / "generation.json").read_bytes()
    )
    rebound_artifacts = tuple(
        SourceArtifact(
            artifact_id=artifact.artifact_id,
            sha256=(
                hashlib.sha256(dashboard_path.read_bytes()).hexdigest()
                if artifact.artifact_id == "panel/data/dashboard_data.json"
                else hashlib.sha256(candidate_path.read_bytes()).hexdigest()
                if artifact.artifact_id == "panel/candidate.json"
                else artifact.sha256
            ),
            size_bytes=(
                len(dashboard_path.read_bytes())
                if artifact.artifact_id == "panel/data/dashboard_data.json"
                else len(candidate_path.read_bytes())
                if artifact.artifact_id == "panel/candidate.json"
                else artifact.size_bytes
            ),
            as_of_date=artifact.as_of_date,
            schema_name=artifact.schema_name,
            schema_version=artifact.schema_version,
        )
        for artifact in panel_manifest.artifacts
    )
    rebound_manifest = GenerationManifest.build(
        as_of_date=panel_manifest.as_of_date,
        adapter=panel_manifest.adapter,
        coverage=panel_manifest.coverage,
        artifacts=rebound_artifacts,
        verification=panel_manifest.verification,
    )
    (panel / "generation.json").write_bytes(rebound_manifest.to_json_bytes())
    rebound_panel = panel.parent / rebound_manifest.generation_id
    panel.rename(rebound_panel)
    _write_json(
        prepared_root / "stage-results" / "panel.json",
        {"generation_id": rebound_manifest.generation_id},
    )
    _refresh_expected_generation(recipe_path, prepared_root, "panel")
    recipe = materializer._recipe(recipe_path)
    prepared_proof = materializer._financial_epoch_manifest_proof(recipe, prepared_root)
    assert prepared_proof is not None
    recipe["financial_epoch_manifest_proof"] = prepared_proof
    _write_json(recipe_path, recipe)
    accepted_recipe = materializer._recipe(recipe_path)
    _write_json(
        prepared_root / "evidence" / "post-build-semantic.json",
        materializer._semantic_gate_payload(
            accepted_recipe, epoch_manifest_proof=prepared_proof
        ),
    )
    _write_json(
        prepared_root / "portfolio-orchestration-contract.json",
        {"release_commit": "a" * 40, "state": "PASS"},
    )
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    production_root = tmp_path / "production"
    spec_path = tmp_path / "release" / "production-spec.json"
    result = materializer.build_production_spec(
        recipe_path,
        prepared_root,
        prepared_root / "evidence" / "post-build-semantic.json",
        production_root,
        tmp_path / "production-state",
        spec_path,
    )

    spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    production_contract = json.loads(Path(result["contract"]).read_text(encoding="utf-8"))
    assert production_contract["schema_version"] == 3
    assert (
        production_contract["financial_epoch_manifest_inputs_sha256"]
        == hashlib.sha256(
            canonical_json_bytes(accepted_recipe["financial_epoch_manifest_inputs"])
        ).hexdigest()
    )
    assert (
        production_contract["financial_epoch_manifest_proof_sha256"]
        == hashlib.sha256(canonical_json_bytes(prepared_proof)).hexdigest()
    )
    core_pipeline.plan_pipeline(spec)

    forged = json.loads(spec_path.read_text(encoding="utf-8"))
    forged_contract = dict(production_contract)
    forged_contract["financial_epoch_manifest_inputs_sha256"] = "0" * 64
    forged_contract["financial_epoch_manifest_proof_sha256"] = "f" * 64
    forged_contract_bytes = canonical_json_bytes(forged_contract)
    Path(result["contract"]).write_bytes(forged_contract_bytes)
    next(
        row
        for row in forged["gates"]
        if row["gate_id"] == "port-underwrite-production-rebind"
    )["sha256"] = hashlib.sha256(forged_contract_bytes).hexdigest()
    with pytest.raises(
        PipelineValidationError, match="financial MVP production binding differs"
    ):
        core_pipeline.plan_pipeline(
            PipelineSpec.from_json_bytes(canonical_json_bytes(forged))
        )
    Path(result["contract"]).write_bytes(canonical_json_bytes(production_contract))


def test_semantic_accepts_explicitly_rendered_freshness_blocker(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path)
    assessment = {
        "blocks_current_consumers": True,
        "escalation_required": True,
        "evaluated_on": AS_OF.isoformat(),
        "fact_type": "strategy-returns",
        "lane": "ACCOUNT",
        "provider_id": "schwab",
        "reason": "accepted observation exceeded the stale boundary",
        "required_for_current": True,
        "state": "STALE",
        "subject_id": "strategy-1",
    }
    _write_json(
        output_root / "stages" / "ledger" / "freshness-health.json",
        {"as_of_date": AS_OF.isoformat(), "assessments": [assessment]},
    )
    panel = next((output_root / "stages" / "panel" / "generations").iterdir())
    _mutate_json(
        panel / "data" / "dashboard_data.json",
        lambda payload: payload.update(
            {
                "coverage": [
                    {
                        **assessment,
                        "category": "DATA_FRESHNESS",
                        "status": assessment["state"],
                    }
                ]
            }
        ),
    )
    _refresh_expected_generation(recipe, output_root, "panel")

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"


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


def _refresh_expected_generation(
    recipe_path: Path, output_root: Path, role: str
) -> None:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    generation = next(
        (
            output_root / "stages" / materializer.GENERATION_ROOT[role] / "generations"
        ).iterdir()
    )
    inventory = inventory_generation(generation)
    recipe["expected_generations"][role] = {
        "files": inventory["files"],
        "generation_id": inventory["generation_id"],
        "manifest_sha256": inventory["manifest_sha256"],
    }
    _write_json(recipe_path, recipe)


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

    def corrupt_provider_catalog(payload: dict[str, object]) -> None:
        artifacts = payload["source_artifacts"]
        assert isinstance(artifacts, list)
        catalog = next(
            row
            for row in artifacts
            if isinstance(row, dict)
            and row.get("schema_name") == "provider-ledger-catalog"
        )
        catalog["sha256"] = "0" * 64

    _mutate_json(
        baseline / "portfolio-baseline.json",
        corrupt_provider_catalog,
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


def test_generated_command_timeout_satisfies_core_pipeline_contract(
    tmp_path: Path,
) -> None:
    node = materializer._command(
        node_id="bounded-command",
        runtime=tmp_path / "python.exe",
        entrypoint=tmp_path / "tool.py",
        lock=tmp_path / "uv.lock",
        recipe=tmp_path / "recipe.json",
        depends_on=(),
        args=("run",),
        reads=(tmp_path / "input",),
        writes=(tmp_path / "output",),
    )

    assert node["timeout_seconds"] == 3600


def test_generated_semantic_command_declares_exact_gate_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_path, _ = _fixture(tmp_path)
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    vertical_path = Path(recipe["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    recipe["vertical_recipe"] = _definition(vertical_path)
    recipe["product_authority"] = materializer._derive_product_authority(
        vertical_path,
        product.contract_path,
        product.matrix_path,
        {
            role: str(recipe["expected_generations"][role]["generation_id"])
            for role in materializer.RELEASE_ROLES
        },
    )
    recipe["schema_version"] = 3
    _write_json(recipe_path, recipe)
    release_commit = "a" * 40
    monkeypatch.setenv("QE_PIPELINE_PYTHON", sys.executable)
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: release_commit if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        materializer, "_validate_output_path_budget", lambda _root, _recipe: None
    )
    output_root = tmp_path / "dag"
    spec_path = output_root / "pipeline-spec.json"

    materializer.build_spec(recipe_path, output_root, spec_path)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    semantic = next(
        node
        for node in spec["nodes"]
        if node.get("node_id") == "post-build-semantic-reconciliation"
    )
    gate = next(
        item for item in spec["gates"] if item["gate_id"] == "post-build-semantic"
    )
    assert gate["path"] in semantic["declared_writes"]
    assert (
        str(output_root / "evidence" / "semantic-details.json")
        in semantic["declared_writes"]
    )

    nodes = {node["node_id"]: node for node in spec["nodes"]}
    assert nodes["build-workbook"]["retry_limit"] == 1
    assert all(
        node["retry_limit"] == 0
        for node_id, node in nodes.items()
        if node.get("kind") == "COMMAND" and node_id != "build-workbook"
    )
    vertical = json.loads(
        Path(json.loads(recipe_path.read_text())["vertical_recipe"]["path"]).read_text()
    )
    vertical_sources = {
        str(Path(definition["path"]))
        for key, definition in vertical.items()
        if key
        in {
            "base_factor_file",
            "baseline_spec",
            "correlation_source_root",
            "decomposition_plan_bundle",
            "legacy_panel",
            "provider_ledger_generation",
            "provider_ledger_spec",
        }
    }
    vertical_sources.add(
        str(Path(json.loads(recipe_path.read_text())["vertical_recipe"]["path"]))
    )
    for role in materializer.STAGES:
        assert vertical_sources <= set(nodes[f"build-{role}"]["declared_reads"])

    assert (
        str(output_root / "stages" / "ledger")
        in nodes["build-attribution"]["declared_reads"]
    )
    for role in ("report", "workbook"):
        assert (
            str(output_root / "stages" / "baseline")
            in nodes[f"build-{role}"]["declared_reads"]
        )
    report_id = next(
        row["sha256"]
        for row in nodes["report-generation"]["files"]
        if row["relative_path"] == "portfolio-report-data.json"
    )
    report_candidate = (
        output_root / "stages" / "report-candidate" / "candidates" / report_id
    )
    assert (
        str(output_root / "stages" / "report-candidate")
        not in nodes["build-report"]["declared_writes"]
    )
    assert {
        str(report_candidate / "portfolio-report-data.json"),
        str(report_candidate / "portfolio-report.xlsx"),
        str(report_candidate / "workbook-contract.json"),
        str(report_candidate / "xlsx-job.json"),
    } <= set(nodes["build-report"]["declared_writes"])
    assert (
        str(output_root / "stages" / "report") in nodes["build-panel"]["declared_reads"]
    )
    assert (
        str(output_root / "stage-results" / "workbook.json")
        in nodes["build-panel"]["declared_reads"]
    )
    assert (
        str(output_root / "stages" / "report-candidate")
        in nodes["build-workbook"]["declared_writes"]
    )

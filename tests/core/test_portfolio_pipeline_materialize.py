from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from quant_engine.core import pipeline as core_pipeline
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationManifest,
    PipelineGate,
    PipelineSpec,
    PipelineValidationError,
    SourceArtifact,
    VerificationStatus,
    inventory_generation,
    pipeline_status,
    plan_pipeline,
    publish_generation,
    read_generation_bundle,
    stage_generation,
    stage_generation_bundle,
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
HWM_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "hwm_governed_execution_fixture",
    ROOT / "tests" / "portfolio_ledger" / "test_hwm_governed_execution.py",
)
assert HWM_FIXTURE_SPEC is not None and HWM_FIXTURE_SPEC.loader is not None
hwm_fixture = importlib.util.module_from_spec(HWM_FIXTURE_SPEC)
sys.modules[HWM_FIXTURE_SPEC.name] = hwm_fixture
HWM_FIXTURE_SPEC.loader.exec_module(hwm_fixture)
SELECTED_HWM_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "selected_hwm_input_fixture",
    ROOT / "tests" / "portfolio_ledger" / "test_hwm_selected_inputs.py",
)
assert (
    SELECTED_HWM_FIXTURE_SPEC is not None
    and SELECTED_HWM_FIXTURE_SPEC.loader is not None
)
selected_hwm_fixture = importlib.util.module_from_spec(SELECTED_HWM_FIXTURE_SPEC)
sys.modules[SELECTED_HWM_FIXTURE_SPEC.name] = selected_hwm_fixture
SELECTED_HWM_FIXTURE_SPEC.loader.exec_module(selected_hwm_fixture)

AS_OF = date(2026, 8, 3)
ROLES = ("ledger", "baseline", "report", "panel")
PREWIRE_MANIFEST_BYTES = b'{"fixture":"synthetic-task-6","version":8}\n'
PREWIRE_REPORT_BYTES = b'{"fixture":"synthetic-task-6-report"}\n'
PREWIRE_MANIFEST_SHA256 = (
    "810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834"
)
PREWIRE_REPORT_SHA256 = (
    "eee8288e0f9ac185d75cf3c7d36947cf1521bf0561e39d178649ba69b865829e"
)


def _available_hwm_assessment_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, Path], dict[str, object]]:
    fixture = hwm_fixture._fixture(tmp_path, mode="committed")
    result = hwm_fixture._run(fixture)
    paths = {
        "governed_result": tmp_path / "governed-hwm-result.json",
        "input_generation": tmp_path / "governed-hwm-input-generation.json",
        "calendar_authority": tmp_path / "governed-hwm-calendar.json",
        "provider_admission_receipt": tmp_path / "admit-or-declare-receipt.json",
    }
    paths["governed_result"].write_bytes(
        json.dumps(result, default=str, sort_keys=True, separators=(",", ":")).encode()
    )
    paths["input_generation"].write_bytes(canonical_json_bytes(fixture["generation"]))
    paths["calendar_authority"].write_bytes(fixture["calendar_authority_bytes"])
    paths["provider_admission_receipt"].write_bytes(fixture["input_receipt_bytes"])
    repository_contract = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_bytes(b"{}")
    authority: dict[str, object] = {
        "contract": materializer._file_definition(repository_contract),
        "conformance": {
            "census_sha256": "1" * 64,
            "contract_semantic_sha256": hashlib.sha256(
                canonical_json_bytes(json.loads(repository_contract.read_bytes()))
            ).hexdigest(),
            "outcomes": [
                {
                    "outcome_id": "AS_HELD_COMPOSITE_HWM",
                    "readiness": "REQUIRED_NOT_READY",
                }
            ],
            "provider_admission": {"product_readiness": "READY_FOR_COMPOSITION"},
            "sources": [],
            "strategies": [
                {"product_requirement": "REQUIRED", "strategy_id": "a-daily"},
                {"product_requirement": "REQUIRED", "strategy_id": "b-session"},
            ],
        },
        "provider_generation": {
            "catalog": materializer._file_definition(placeholder),
            "generation_id": "a" * 64,
            "manifest": materializer._file_definition(placeholder),
            "receipt": materializer._file_definition(
                paths["provider_admission_receipt"]
            ),
        },
        "source_matrix": materializer._file_definition(placeholder),
    }
    return authority, paths, result


def test_assessment_two_available_requires_real_selected_source_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_paths = selected_hwm_fixture._fixture(tmp_path / "selected", daily=True)
    generation = selected_hwm_fixture._build(selected_paths)
    calendar_bytes = selected_paths["calendar"].read_bytes()
    calendar = json.loads(calendar_bytes)
    calendar_root = calendar["authority_evidence_sha256"]
    start = min(
        date.fromisoformat(row["effective_start"]) for row in generation["memberships"]
    )
    complete = max(
        date.fromisoformat(row["interval_end"])
        for row in generation["component_periods"]
    )
    accepted_intervals = []
    cursor = start
    index = 0
    while cursor < complete:
        following = cursor + timedelta(days=1)
        accepted_intervals.append(
            {
                "interval_id": f"selected-{index:04d}",
                "interval_start": cursor.isoformat(),
                "interval_end": following.isoformat(),
                "interval_evidence_sha256": hashlib.sha256(
                    f"{cursor.isoformat()}|{following.isoformat()}|{calendar_root}".encode()
                ).hexdigest(),
            }
        )
        cursor = following
        index += 1
    input_bytes = canonical_json_bytes(generation)
    policy_bytes = selected_paths["policy"].read_bytes()
    receipt_bytes = selected_paths["receipt"].read_bytes()
    result = hwm_fixture.compute_governed_as_held_composite_hwm(
        accepted_intervals,
        generation["memberships"],
        generation["component_periods"],
        policy_bytes=policy_bytes,
        product_contract_bytes=selected_paths["contract"].read_bytes(),
        performance_policy_bytes=(
            ROOT / "contracts" / "portfolio-performance-policy-v1.json"
        ).read_bytes(),
        legacy_policy_bytes=(
            ROOT / "contracts" / "portfolio-hwm-policy-v3.json"
        ).read_bytes(),
        calendar_authority_bytes=calendar_bytes,
        input_generation_bytes=input_bytes,
        input_receipt_bytes=receipt_bytes,
        calendar_accepted_sha256=calendar_root,
        evaluated_on=(complete + timedelta(days=1)).isoformat(),
        complete_through=complete.isoformat(),
        maximum_age_days=30,
        assurance_profile="WORKSTATION_QC",
    )
    # Independent oracle: add account NAV changes directly, without using the
    # adapter's grouped returns, weights, observation factors or linked index.
    ledger = json.loads(
        (
            selected_paths["provider_generation"].parent
            / "normalized/provider-ledger.json"
        ).read_bytes()
    )
    navs = {
        (row["account_id"], row["as_of_date"]): Decimal(row["nav"]["value"])
        for row in ledger["nav_observations"]
    }
    census = json.loads(selected_paths["contract"].read_bytes())["current_census"]
    with localcontext() as context:
        context.prec = 50
        index_value = peak = Decimal("100")
        worst = Decimal("0")
        for period in result["periods"]:
            left, right = period["interval_start"], period["interval_end"]
            accounts = {
                f"synthetic-account-{account_index:02d}"
                for account_index, sleeve in enumerate(census["account_sleeves"])
                for assignment in sleeve["assignments"]
                if assignment["status"] in {"FUNDED_MEMBERSHIP", "ASSIGNMENT_ONLY"}
                # Derived epochs exist only in the account manifest.
                and assignment["authority"]
                != "R2_DERIVED_SLEEVE_EPOCH_FUNDED_MEMBERSHIP"
                and assignment["effective_start"] <= left
                and (
                    assignment["effective_end"] is None
                    or right <= assignment["effective_end"]
                )
            }
            opening = sum(navs[(account, left)] for account in accounts)
            closing = sum(navs[(account, right)] for account in accounts)
            expected_return = closing / opening - 1
            assert abs(period["composite_return"] - expected_return) < Decimal("1e-30")
            index_value *= 1 + expected_return
            peak = max(peak, index_value)
            drawdown = index_value / peak - 1
            worst = min(worst, drawdown)
        for key, expected in {
            "current_index": index_value,
            "current_hwm": peak,
            "current_drawdown": drawdown,
            "maximum_drawdown": worst,
        }.items():
            assert abs(result[key] - expected) < Decimal("1e-27"), key
        assert worst < 0
    paths = {
        "governed_result": tmp_path / "source-bound-hwm-result.json",
        "input_generation": tmp_path / "source-bound-hwm-input.json",
        "calendar_authority": selected_paths["calendar"],
        "provider_admission_receipt": selected_paths["receipt"],
    }
    paths["governed_result"].write_bytes(
        json.dumps(result, default=str, sort_keys=True, separators=(",", ":")).encode()
    )
    paths["input_generation"].write_bytes(input_bytes)
    static_sources = {
        "portfolio-calendars",
        "portfolio-fx-rates",
        "portfolio-security-master",
    }
    placeholder = tmp_path / "source-bound-placeholder.json"
    placeholder.write_bytes(b"{}")
    authority = {
        "contract": materializer._file_definition(selected_paths["contract"]),
        "conformance": {
            "census_sha256": "1" * 64,
            "contract_semantic_sha256": (
                selected_hwm_fixture.financial_contract.contract_digest(
                    json.loads(selected_paths["contract"].read_bytes())
                )
            ),
            "outcomes": [
                {
                    "outcome_id": "AS_HELD_COMPOSITE_HWM",
                    "readiness": "REQUIRED_NOT_READY",
                }
            ],
            "provider_admission": {"product_readiness": "READY_FOR_COMPOSITION"},
            "sources": [
                {
                    "source_id": source_id,
                    "consumers": ["AS_HELD_COMPOSITE_HWM"],
                    "current_generation": {"generation_id": None, "sha256": None},
                }
                for source_id in sorted(static_sources)
            ],
            "strategies": [
                {
                    "product_requirement": "REQUIRED",
                    "strategy_id": row["strategy_id"],
                }
                for row in generation["memberships"]
            ],
        },
        "provider_generation": {
            "catalog": materializer._file_definition(placeholder),
            "generation_id": selected_paths["provider_generation"].parent.name,
            "manifest": materializer._file_definition(placeholder),
            "receipt": materializer._file_definition(selected_paths["receipt"]),
        },
        "source_matrix": materializer._file_definition(placeholder),
    }
    source_paths = {
        "account_manifest_generation_path": selected_paths["generation"],
        "account_manifest_admission_policy_path": selected_paths["admission_policy"],
        "account_manifest_flow_classification_policy_path": selected_paths[
            "flow_classification_policy"
        ],
        "provider_ledger_generation_path": selected_paths["provider_generation"],
        "baseline_generation_path": selected_paths["baseline_generation"],
        "membership_census_path": selected_paths["membership_census"],
    }
    import portfolio_pipeline_materialize as imported_materializer

    monkeypatch.setattr(materializer, "HWM_GOVERNED_POLICY", selected_paths["policy"])
    monkeypatch.setattr(
        imported_materializer, "HWM_GOVERNED_POLICY", selected_paths["policy"]
    )

    assessment = materializer._hwm_input_authority_assessment(
        authority,
        hwm_bindings=paths,
        hwm_source_paths=source_paths,
    )

    assert assessment["schema_version"] == 2
    assert assessment["assessment_scope"] == "VERIFIED_GOVERNED_HWM_RESULT"
    assert assessment["state"] == "AVAILABLE"
    assert assessment["outcome_readiness"] == "REQUIRED_NOT_READY"
    assert assessment["required_strategy_ids"] == sorted(
        row["strategy_id"] for row in generation["memberships"]
    )
    assert assessment["blocking_reasons"] == []
    assert assessment["unresolved_source_ids"] == []
    assert (
        assessment["result_sha256"]
        == hashlib.sha256(paths["governed_result"].read_bytes()).hexdigest()
    )
    assert (
        assessment["input_generation_sha256"]
        == hashlib.sha256(paths["input_generation"].read_bytes()).hexdigest()
    )
    assert (
        assessment["calendar_authority_sha256"]
        == hashlib.sha256(paths["calendar_authority"].read_bytes()).hexdigest()
    )
    assert set(assessment["bindings"]) == set(paths)
    tampered_policy = tmp_path / "tampered-flow-classification-policy.json"
    tampered_policy.write_bytes(
        selected_paths["flow_classification_policy"].read_bytes() + b"\n"
    )
    tampered_source_paths = dict(source_paths)
    tampered_source_paths["account_manifest_flow_classification_policy_path"] = (
        tampered_policy
    )
    with pytest.raises(
        materializer.MaterializationError, match="selected HWM source replay"
    ):
        materializer._hwm_input_authority_assessment(
            authority,
            hwm_bindings=paths,
            hwm_source_paths=tampered_source_paths,
        )


def test_assessment_two_rejects_result_self_reference(tmp_path: Path) -> None:
    authority, paths, _result = _available_hwm_assessment_fixture(tmp_path)
    product_path = tmp_path / "financial-mvp-product-conformance.json"
    paths["governed_result"] = product_path.with_name(
        "financial-mvp-hwm-input-authority.json"
    )

    with pytest.raises(materializer.MaterializationError, match="self-reference"):
        materializer._product_gate_rows(
            authority,
            product_path,
            hwm_bindings=paths,
            hwm_source_paths={"synthetic": tmp_path},
        )


def _stage_prewire_generation(root: Path) -> tuple[GenerationManifest, object]:
    """Build the real detached-control envelope around hand-authored bytes."""

    as_of = date(2026, 8, 24)
    manifest_artifact = SourceArtifact(
        artifact_id="outputs/account-strategy-manifest.json",
        sha256=PREWIRE_MANIFEST_SHA256,
        size_bytes=43,
        as_of_date=as_of,
        schema_name="account-strategy-manifest",
        schema_version="8",
    )
    report_artifact = SourceArtifact(
        artifact_id="verification/independent-verification.json",
        sha256=PREWIRE_REPORT_SHA256,
        size_bytes=38,
        as_of_date=as_of,
        schema_name="independent-verification",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-task-6-prewire", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic Task-6 prewire fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(manifest_artifact, report_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        root,
        manifest,
        {
            "outputs/account-strategy-manifest.json": PREWIRE_MANIFEST_BYTES,
            "verification/independent-verification.json": PREWIRE_REPORT_BYTES,
        },
    )
    return manifest, read_generation_bundle(root, manifest.generation_id)


def _verified_materialization_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, Path]:
    source_root = tmp_path / "epoch-source"
    account_manifest = {
        "accounting_currency": "USD",
        "artifact": "account-strategy-manifest",
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "rows": [],
        "schema_version": 8,
    }
    account_bytes = canonical_json_bytes(account_manifest)
    report_bytes = canonical_json_bytes(
        {"status": "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"}
    )
    source_generation = GenerationManifest.build(
        as_of_date=date(2026, 8, 24),
        adapter=AdapterIdentity("synthetic-schema10-source", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic schema-10 materialization source",
            fact_types=("synthetic",),
            observed_start=date(2026, 8, 24),
            observed_end=date(2026, 8, 24),
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="outputs/account-strategy-manifest.json",
                sha256=hashlib.sha256(account_bytes).hexdigest(),
                size_bytes=len(account_bytes),
                as_of_date=date(2026, 8, 24),
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=date(2026, 8, 24),
                schema_name="independent-verification",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        source_root,
        source_generation,
        {
            "outputs/account-strategy-manifest.json": account_bytes,
            "verification/independent-verification.json": report_bytes,
        },
    )
    source_bundle = read_generation_bundle(source_root, source_generation.generation_id)
    financial_module = materializer._financial_mvp_module()
    source = financial_module.epoch_manifest_source_from_bundle(source_bundle)
    output_root = tmp_path / "materialized"
    bound_contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    bound_contract["contract_schema_version"] = 7
    bound_contract["epoch_manifest_source"] = source
    registry = json.loads(
        materializer.CURRENT_STRATEGY_REGISTRY.read_text(encoding="utf-8")
    )
    financial_module.validate_contract(
        bound_contract,
        registry=registry,
        epoch_manifest_bundle=source_bundle,
    )
    bound_contract_path = output_root / "financial-mvp-bound-contract.json"
    bound_contract_path.parent.mkdir(parents=True, exist_ok=True)
    bound_contract_path.write_bytes(canonical_json_bytes(bound_contract))

    verification = {
        "epoch_manifest_source": source,
        "independent_report": {"status": "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"},
        "method": "READ_GENERATION_BUNDLE_EXACT_BYTES_AND_FINANCIAL_MVP_BINDER",
        "producer_readiness": account_manifest["authority_readiness"],
        "publication_readiness": "NOT_READY",
        "status": "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES",
    }
    dashboard = {
        "account_manifest": account_manifest,
        "meta": {
            "account_manifest_authority_profile": (
                "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES"
            ),
            "account_manifest_sha256": hashlib.sha256(account_bytes).hexdigest(),
            "account_manifest_size_bytes": len(account_bytes),
            "account_manifest_verification": verification,
        },
    }
    dashboard_bytes = canonical_json_bytes(dashboard)
    candidate = {
        "account_manifest_authority_profile": (
            "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES"
        ),
        "account_manifest_verification": verification,
        "artifact_sha256": {
            "data/dashboard_data.json": hashlib.sha256(dashboard_bytes).hexdigest()
        },
    }
    candidate_bytes = canonical_json_bytes(candidate)
    staged = output_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(account_bytes)
    panel_manifest = GenerationManifest.build(
        as_of_date=date(2026, 8, 24),
        adapter=AdapterIdentity("synthetic-schema10-panel", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic schema-10 materialized panel",
            fact_types=("synthetic",),
            observed_start=date(2026, 8, 24),
            observed_end=date(2026, 8, 24),
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="input/account-strategy-manifest.json",
                sha256=hashlib.sha256(account_bytes).hexdigest(),
                size_bytes=len(account_bytes),
                as_of_date=date(2026, 8, 24),
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="panel/data/dashboard_data.json",
                sha256=hashlib.sha256(dashboard_bytes).hexdigest(),
                size_bytes=len(dashboard_bytes),
                as_of_date=date(2026, 8, 24),
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="panel/candidate.json",
                sha256=hashlib.sha256(candidate_bytes).hexdigest(),
                size_bytes=len(candidate_bytes),
                as_of_date=date(2026, 8, 24),
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    panel_manifest_path = stage_generation(
        output_root / "stages" / "panel", panel_manifest
    )
    panel_generation = panel_manifest_path.parent
    for relative, content in {
        "input/account-strategy-manifest.json": account_bytes,
        "data/dashboard_data.json": dashboard_bytes,
        "candidate.json": candidate_bytes,
    }.items():
        target = panel_generation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    inventory = inventory_generation(panel_generation)
    recipe = {
        "epoch_manifest_generation": {
            "runtime_root": str(source_root.resolve()),
            "generation_id": source_generation.generation_id,
            "source": source,
        },
        "expected_generations": {
            "panel": {
                "files": inventory["files"],
                "generation_id": inventory["generation_id"],
                "manifest_sha256": inventory["manifest_sha256"],
            }
        },
    }
    return recipe, output_root, panel_generation


def test_epoch_manifest_bundle_prewire_binds_once_and_stages_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-five rebuild, duplicate read, alias, or re-encoding breaks this seam."""

    generation_root = tmp_path / "accepted"
    manifest, expected_bundle = _stage_prewire_generation(generation_root)
    real_reader = read_generation_bundle
    reader_calls: list[tuple[Path, str]] = []
    reader_results: list[object] = []

    def read_once(root: Path, generation_id: str) -> object:
        reader_calls.append((root, generation_id))
        result = real_reader(root, generation_id)
        reader_results.append(result)
        return result

    real_financial_module = materializer._financial_mvp_module()
    binder_calls: list[object] = []
    validator_calls: list[tuple[object, object, object]] = []

    class ExactBinder:
        def epoch_manifest_source_from_bundle(self, bundle: object) -> object:
            binder_calls.append(bundle)
            return real_financial_module.epoch_manifest_source_from_bundle(bundle)

        def validate_contract(
            self,
            contract: object,
            *,
            registry: object,
            epoch_manifest_bundle: object,
        ) -> object:
            validator_calls.append((contract, registry, epoch_manifest_bundle))
            real_financial_module.validate_epoch_manifest_source(
                contract["epoch_manifest_source"], bundle=epoch_manifest_bundle
            )
            return contract

    producer_calls: list[bool] = []

    def forbidden_producer() -> object:
        producer_calls.append(True)
        raise AssertionError("raw-five epoch producer must be unreachable")

    monkeypatch.setattr(materializer, "read_generation_bundle", read_once)
    monkeypatch.setattr(materializer, "_financial_mvp_module", ExactBinder)
    monkeypatch.setattr(
        materializer, "_account_strategy_manifest_module", forbidden_producer
    )
    supplied_contract = {
        "contract_schema_version": 6,
        "contract_id": "synthetic-prewire-contract",
        "nested": {"rows": [{"value": "17.25"}]},
        "schema_version": 3,
    }
    original_contract = json.loads(json.dumps(supplied_contract))
    output_root = tmp_path / "output"

    augmented = materializer._bind_epoch_manifest_bundle(
        generation_root,
        manifest.generation_id,
        supplied_contract,
    )

    literal_source = {
        "generation_id": (
            "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8"
        ),
        "generation_manifest_sha256": (
            "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d"
        ),
        "generation_receipt_sha256": (
            "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f"
        ),
        "full_directory_inventory_sha256": (
            "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1"
        ),
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": PREWIRE_MANIFEST_SHA256,
            "size_bytes": 43,
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
    }
    assert reader_calls == [(generation_root, manifest.generation_id)]
    assert len(binder_calls) == 1
    assert binder_calls[0] is reader_results[0]
    assert binder_calls[0] == expected_bundle
    assert type(binder_calls[0]) is type(expected_bundle)
    assert producer_calls == []
    assert supplied_contract == original_contract
    assert set(augmented) == {*original_contract, "epoch_manifest_source"}
    assert augmented["contract_schema_version"] == 7
    assert augmented["epoch_manifest_source"] == literal_source
    assert len(validator_calls) == 1
    assert validator_calls[0][0] is augmented
    assert validator_calls[0][2] is reader_results[0]
    assert augmented["nested"] is not supplied_contract["nested"]
    # Binding stages nothing: the panel command writes the manifest itself
    # from this same verified bundle, and `build-panel` declares that
    # directory as its own output.
    assert not (output_root / "stages").exists()
    augmented["nested"]["rows"][0]["value"] = "changed"
    assert supplied_contract == original_contract


def test_epoch_manifest_bundle_prewire_passes_real_financial_contract_validator(
    tmp_path: Path,
) -> None:
    """The production validator, not a fixture echo, authorizes the v7 contract."""

    generation_root = tmp_path / "accepted"
    manifest, bundle = _stage_prewire_generation(generation_root)
    contract = json.loads(
        (
            materializer.REPOSITORY_ROOT
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )

    bound = materializer._bind_epoch_manifest_bundle(
        generation_root,
        manifest.generation_id,
        contract,
    )

    assert bound["contract_schema_version"] == 7
    module = materializer._financial_mvp_module()
    assert (
        module.validate_contract(
            bound,
            registry=json.loads(
                materializer.CURRENT_STRATEGY_REGISTRY.read_text(encoding="utf-8")
            ),
            epoch_manifest_bundle=bundle,
        )
        == bound
    )
    assert bound["epoch_manifest_source"]["artifact"]["sha256"] == (
        PREWIRE_MANIFEST_SHA256
    )


def test_real_financial_v7_contract_rejects_shape_only_or_invented_authority(
    tmp_path: Path,
) -> None:
    generation_root = tmp_path / "accepted"
    _manifest, bundle = _stage_prewire_generation(generation_root)
    module = materializer._financial_mvp_module()
    contract = json.loads(
        (
            materializer.REPOSITORY_ROOT
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )
    contract["contract_schema_version"] = 7
    contract["epoch_manifest_source"] = module.epoch_manifest_source_from_bundle(bundle)
    registry = json.loads(
        materializer.CURRENT_STRATEGY_REGISTRY.read_text(encoding="utf-8")
    )

    with pytest.raises(
        module.FinancialMvpContractError,
        match="requires a verified epoch-manifest bundle",
    ):
        module.validate_contract(contract, registry=registry)

    contract["epoch_manifest_source"]["generation_manifest_sha256"] = "0" * 64
    with pytest.raises(module.FinancialMvpContractError, match="verified bundle"):
        module.validate_contract(
            contract,
            registry=registry,
            epoch_manifest_bundle=bundle,
        )


def test_schema10_materialization_proof_reopens_bundle_and_panel_bytes(
    tmp_path: Path,
) -> None:
    recipe, output_root, _panel_generation = _verified_materialization_fixture(tmp_path)

    proof = materializer._verified_epoch_manifest_materialization_proof(
        recipe, output_root
    )

    assert proof is not None
    assert proof["status"] == "PASS"
    assert proof["authority_profile"] == "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES"
    assert (
        proof["epoch_manifest_source_sha256"]
        == hashlib.sha256(
            canonical_json_bytes(recipe["epoch_manifest_generation"]["source"])
        ).hexdigest()
    )
    assert (
        proof["financial_mvp_bound_contract_sha256"]
        == hashlib.sha256(
            (output_root / "financial-mvp-bound-contract.json").read_bytes()
        ).hexdigest()
    )


def test_schema10_materialization_proof_rejects_one_byte_stage_drift(
    tmp_path: Path,
) -> None:
    recipe, output_root, _panel_generation = _verified_materialization_fixture(tmp_path)
    staged = output_root / "stages" / "panel" / "account-strategy-manifest.json"
    staged.write_bytes(staged.read_bytes() + b" ")

    with pytest.raises(materializer.MaterializationError, match="verified bundle"):
        materializer._verified_epoch_manifest_materialization_proof(recipe, output_root)


def test_schema10_materialization_proof_rejects_physical_panel_input_drift(
    tmp_path: Path,
) -> None:
    recipe, output_root, panel_generation = _verified_materialization_fixture(tmp_path)
    input_path = panel_generation / "input" / "account-strategy-manifest.json"
    input_path.write_bytes(input_path.read_bytes() + b" ")

    with pytest.raises(materializer.MaterializationError, match="physical panel input"):
        materializer._verified_epoch_manifest_materialization_proof(recipe, output_root)


def test_schema10_materialization_proof_rejects_invented_bound_contract_authority(
    tmp_path: Path,
) -> None:
    recipe, output_root, _panel_generation = _verified_materialization_fixture(tmp_path)
    bound_path = output_root / "financial-mvp-bound-contract.json"
    bound = json.loads(bound_path.read_text(encoding="utf-8"))
    bound["epoch_manifest_source"]["generation_receipt_sha256"] = "0" * 64
    bound_path.write_bytes(canonical_json_bytes(bound))

    with pytest.raises(materializer.MaterializationError, match="bound financial"):
        materializer._verified_epoch_manifest_materialization_proof(recipe, output_root)


@pytest.mark.parametrize("include_raw_five", [False, True])
def test_epoch_manifest_bundle_prewire_rejects_missing_detached_controls(
    tmp_path: Path, include_raw_five: bool
) -> None:
    """A manifest, even beside the historical raw-five inputs, is not a bundle."""

    manifest, _bundle = _stage_prewire_generation(tmp_path / "valid")
    generation = tmp_path / "broken" / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    if include_raw_five:
        for filename in (
            "account-strategy-ledger.json",
            "account-strategy-ledger-identity.json",
            "account-strategy-classified-flows.json",
            "account-strategy-flow-coverage.json",
            "account-strategy-admission-policy.json",
        ):
            (generation / filename).write_bytes(b"{}\n")
    output_root = tmp_path / "output"

    with pytest.raises(materializer.MaterializationError, match="bundle"):
        materializer._bind_epoch_manifest_bundle(
            tmp_path / "broken",
            manifest.generation_id,
            {"schema_version": 3},
        )

    assert not output_root.exists()


def test_epoch_manifest_bundle_prewire_rejects_bare_generation_manifest(
    tmp_path: Path,
) -> None:
    """Accepting a GenerationManifest where a bundle ID belongs breaks authority."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_prewire_generation(generation_root)
    output_root = tmp_path / "output"

    with pytest.raises(materializer.MaterializationError, match="bundle"):
        materializer._bind_epoch_manifest_bundle(
            generation_root,
            manifest,  # type: ignore[arg-type]
            {"schema_version": 3},
        )

    assert not output_root.exists()


@pytest.mark.parametrize("mutation", ["payload", "receipt"])
def test_epoch_manifest_bundle_prewire_rejects_mutation_before_staging(
    tmp_path: Path, mutation: str
) -> None:
    """Trusting manifest metadata without immutable readback would stage drift."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_prewire_generation(generation_root)
    generation = generation_root / "generations" / manifest.generation_id
    if mutation == "payload":
        target = generation / "outputs" / "account-strategy-manifest.json"
        target.write_bytes(target.read_bytes().replace(b'"version":8', b'"version":9'))
    else:
        target = generation / "generation-receipt.json"
        changed = bytearray(target.read_bytes())
        changed[0] = ord("[")
        target.write_bytes(changed)
    output_root = tmp_path / "output"

    with pytest.raises(materializer.MaterializationError, match="bundle"):
        materializer._bind_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            {"schema_version": 3},
        )

    assert not output_root.exists()


def test_epoch_manifest_bundle_prewire_wraps_binder_failure_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected bundle-to-contract binding must not emit panel bytes."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_prewire_generation(generation_root)

    class RejectingBinder:
        @staticmethod
        def epoch_manifest_source_from_bundle(_bundle: object) -> object:
            raise ValueError("synthetic binder rejection")

    monkeypatch.setattr(materializer, "_financial_mvp_module", RejectingBinder)
    output_root = tmp_path / "output"

    with pytest.raises(materializer.MaterializationError, match="binding"):
        materializer._bind_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            {"schema_version": 3},
        )

    assert not output_root.exists()


def test_schema10_pipeline_recipe_accepts_exact_epoch_manifest_generation(
    tmp_path: Path,
) -> None:
    source_recipe, _proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_prewire_generation(generation_root)
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    vertical["schema_version"] = 10
    vertical["epoch_manifest_generation"] = {
        "runtime_root": str(generation_root.resolve()),
        "generation_id": manifest.generation_id,
        "source": {
            "generation_id": "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8",
            "generation_manifest_sha256": "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d",
            "generation_receipt_sha256": "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f",
            "full_directory_inventory_sha256": "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1",
            "artifact": {
                "path": "outputs/account-strategy-manifest.json",
                "sha256": PREWIRE_MANIFEST_SHA256,
                "size_bytes": 43,
                "schema": {"name": "account-strategy-manifest", "version": "8"},
            },
        },
    }
    _write_json(vertical_path, vertical)
    pipeline = json.loads(source_recipe.read_text(encoding="utf-8"))
    pipeline["vertical_recipe"] = _definition(vertical_path)
    pipeline["epoch_manifest_generation"] = vertical["epoch_manifest_generation"]
    pipeline_path = tmp_path / "schema10-pipeline-recipe.json"
    _write_json(pipeline_path, pipeline)

    accepted = materializer._recipe(pipeline_path)

    assert (
        accepted["epoch_manifest_generation"] == vertical["epoch_manifest_generation"]
    )


def test_schema10_pipeline_recipe_rejects_declared_source_drift(
    tmp_path: Path,
) -> None:
    source_recipe, _proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_prewire_generation(generation_root)
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    vertical["schema_version"] = 10
    vertical["epoch_manifest_generation"] = {
        "runtime_root": str(generation_root.resolve()),
        "generation_id": manifest.generation_id,
        "source": {"forged": True},
    }
    _write_json(vertical_path, vertical)
    pipeline = json.loads(source_recipe.read_text(encoding="utf-8"))
    pipeline["vertical_recipe"] = _definition(vertical_path)
    pipeline["epoch_manifest_generation"] = vertical["epoch_manifest_generation"]
    pipeline_path = tmp_path / "schema10-pipeline-recipe.json"
    _write_json(pipeline_path, pipeline)

    with pytest.raises(materializer.MaterializationError, match="source identity"):
        materializer._recipe(pipeline_path)


def test_schema10_build_spec_materializes_bound_manifest_and_contract_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public spec path must invoke the prewire seam exactly once."""
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    generation_root = tmp_path / "accepted"
    epoch_manifest, epoch_bundle = _stage_prewire_generation(generation_root)
    epoch_source = (
        materializer._financial_mvp_module().epoch_manifest_source_from_bundle(
            epoch_bundle
        )
    )
    contract = json.loads(product.contract_path.read_text(encoding="utf-8"))
    real_financial_module = materializer._financial_mvp_module()
    product_module = financial_fixture.SimpleNamespace(
        **{
            **vars(product.financial_module),
            "load_contract": lambda _path, _registry, _schema: contract,
            "epoch_manifest_source_from_bundle": real_financial_module.epoch_manifest_source_from_bundle,
            "validate_contract": lambda value, **_kwargs: value,
        }
    )
    monkeypatch.setattr(materializer, "_financial_mvp_module", lambda: product_module)
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 7
    _write_json(vertical_path, vertical)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 10
    vertical["epoch_manifest_generation"] = {
        "runtime_root": str(generation_root.resolve()),
        "generation_id": epoch_manifest.generation_id,
        "source": epoch_source,
    }
    _write_json(vertical_path, vertical)
    pipeline_recipe = tmp_path / "schema10-pipeline-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema10-public-materialization",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )
    calls: list[tuple[Path, str]] = []
    real_prewire = materializer._bind_epoch_manifest_bundle

    def observed_prewire(*args: object, **kwargs: object) -> object:
        calls.append((args[0], args[1]))  # type: ignore[arg-type]
        return real_prewire(*args, **kwargs)

    monkeypatch.setattr(materializer, "_bind_epoch_manifest_bundle", observed_prewire)
    monkeypatch.setenv("QE_PIPELINE_PYTHON", sys.executable)
    monkeypatch.setattr(
        materializer,
        "_git",
        lambda _repo, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        materializer, "_validate_output_path_budget", lambda _root, _recipe: None
    )
    output_root = tmp_path / "schema10-dag"
    spec_path = output_root / "pipeline-spec.json"

    governed = {
        name: _write_json(tmp_path / "governed" / f"{name}.json", {"name": name})
        or (tmp_path / "governed" / f"{name}.json")
        for name in (
            "as-held-hwm",
            "hwm-input",
            "calendar",
            "admission",
            "invested-capital",
            "capital-input",
        )
    }
    materializer.build_spec(
        pipeline_recipe,
        output_root,
        spec_path,
        governed["as-held-hwm"],
        governed["hwm-input"],
        governed["calendar"],
        governed["admission"],
        portfolio_root=tmp_path / "portfolio",
        invested_capital_result_path=governed["invested-capital"],
        invested_capital_input_generation_path=governed["capital-input"],
    )

    assert calls == [(generation_root, epoch_manifest.generation_id)]
    # The panel command writes the manifest from the same verified bundle, so
    # the spec build leaves that node's declared output untouched.
    assert not (output_root / "stages" / "panel").exists()
    # The report and panel stages are private financial stages and must carry
    # the production root disjointness proof; workbook is permanently held.
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    argv_by_node = {row["node_id"]: row.get("argv", []) for row in spec["nodes"]}
    for node_id in ("build-report", "build-panel"):
        argv = argv_by_node[node_id]
        assert "--portfolio-root" in argv
        assert argv[argv.index("--portfolio-root") + 1] == str(tmp_path / "portfolio")
    for node_id in ("build-ledger", "build-baseline"):
        assert "--portfolio-root" not in argv_by_node[node_id]
    # `build_report` also refuses a governed staging run without the root, so a
    # run that binds the governed results has to carry it on the report node.
    assert "--portfolio-root" in argv_by_node["build-report"]
    # The governed numerical results are stage inputs for every role that reads
    # them, and each has to be a declared absolute read or the plan refuses.
    reads_by_node = {
        row["node_id"]: row.get("declared_reads", []) for row in spec["nodes"]
    }
    for node_id in ("build-report", "build-panel"):
        argv = argv_by_node[node_id]
        for flag, path in (
            ("--hwm-governed-result", governed["as-held-hwm"]),
            ("--hwm-governed-input-generation", governed["hwm-input"]),
            ("--hwm-governed-calendar-authority", governed["calendar"]),
            ("--hwm-governed-admission-receipt", governed["admission"]),
            ("--invested-capital-result", governed["invested-capital"]),
            ("--invested-capital-input-generation", governed["capital-input"]),
        ):
            assert argv[argv.index(flag) + 1] == str(path.resolve())
            assert str(path.resolve()) in reads_by_node[node_id]
    for node_id in ("build-ledger", "build-baseline"):
        assert "--hwm-governed-result" not in argv_by_node[node_id]
    orchestration = json.loads(
        spec_path.with_name("portfolio-orchestration-contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert orchestration["epoch_manifest_generation"]["source"] == epoch_source
    assert (
        orchestration["epoch_manifest_source_sha256"]
        == hashlib.sha256(canonical_json_bytes(epoch_source)).hexdigest()
    )
    bound_contract = json.loads(
        (output_root / "financial-mvp-bound-contract.json").read_text(encoding="utf-8")
    )
    assert bound_contract["contract_schema_version"] == 7
    assert bound_contract["epoch_manifest_source"] == epoch_source
    assert (
        orchestration["financial_mvp_bound_contract_sha256"]
        == hashlib.sha256(
            (output_root / "financial-mvp-bound-contract.json").read_bytes()
        ).hexdigest()
    )


def test_governed_results_reach_every_role_that_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report and panel roles each read the governed results.

    Binding them into the proof but not into the pipeline's own nodes made the
    panel node recompute a semantic validation that differed from the one the
    proof had already written into the shared QA directory, which failed the
    run at `build-panel` with a create-only conflict.
    """

    flags = {
        "--hwm-governed-result": tmp_path / "as-held-hwm.json",
        "--hwm-governed-input-generation": tmp_path / "hwm-input.json",
        "--hwm-governed-calendar-authority": tmp_path / "calendar.json",
        "--hwm-governed-admission-receipt": tmp_path / "admission.json",
        "--invested-capital-result": tmp_path / "invested-capital.json",
        "--invested-capital-input-generation": tmp_path / "capital-input.json",
    }
    for path in flags.values():
        _write_json(path, {"present": True})

    resolved = materializer._governed_stage_paths(*flags.values())
    assert set(resolved) == set(flags)
    assert all(resolved[flag] == path.resolve() for flag, path in flags.items())

    assert materializer._governed_stage_paths(*([None] * 6)) == {}

    partial = [*flags.values()]
    partial[-1] = None
    with pytest.raises(
        materializer.MaterializationError,
        match="governed numerical results must be supplied together",
    ):
        materializer._governed_stage_paths(*partial)

    missing = [*flags.values()]
    missing[0] = tmp_path / "absent.json"
    with pytest.raises(
        materializer.MaterializationError,
        match="governed result is absent or linked",
    ):
        materializer._governed_stage_paths(*missing)

    assert materializer._GOVERNED_OUTCOME_STAGES == ("report", "panel")


def test_financial_spec_without_a_portfolio_root_fails_before_the_dag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting it used to surface four nodes in, as an opaque node failure."""

    recipe_path = tmp_path / "recipe.json"
    _write_json(recipe_path, {"schema_version": 3})

    with pytest.raises(
        materializer.MaterializationError,
        match="financial pipeline specification requires a portfolio root",
    ):
        materializer.build_spec(
            recipe_path, tmp_path / "out", tmp_path / "out" / "pipeline-spec.json"
        )


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


def _generation(
    root: Path,
    role: str,
    files: dict[str, object],
    *,
    as_of_date: date = AS_OF,
) -> Path:
    # `as_of_date` is the generation's own provenance stamp. It defaults to the fixture clock
    # so every existing caller is unchanged, and the coverage boundary stays at AS_OF because
    # that is the financial observation window, which provenance may legitimately outrun.
    seed = f"{role}\n".encode()
    manifest = GenerationManifest.build(
        as_of_date=as_of_date,
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


CURRENT_STRATEGY_NAV = "400000.5"
CURRENT_RESERVE_NAV = "12189.55"


def _cc_amount(value: str | None) -> dict[str, object]:
    return {
        "currency": "USD",
        "status": "AVAILABLE" if value is not None else "UNAVAILABLE",
        "value": value,
    }


def _current_capital_fixture(*, reserve_ready: bool = False) -> dict[str, object]:
    """Hand-written ``current_capital`` for the one-strategy synthetic baseline.

    strategy-1 is account-1's NAV 400000.5 @ AS_OF (the audit table below says
    410272.39, so the two figures can never be confused).  The reserve account's
    only NAV is a 2026-07-14 legacy row: it is capital authority only when
    ``reserve_ready`` admits it AUTHORITATIVE, otherwise the reserve is NOT_READY.
    """

    as_of = AS_OF.isoformat()
    strategy = {
        "as_of_date": as_of,
        "current_value": _cc_amount(CURRENT_STRATEGY_NAV),
        "current_weight": "1",
        "members": [
            {
                "account_id": "account-1",
                "as_of_date": as_of,
                "assignment_ids": ["assignment-1"],
                "effective_end": None,
                "effective_start": "2026-01-01",
                "nav_record_id": "nav-account-1",
                "source_key": "provider-ledger",
                "status": "AVAILABLE",
                "value": _cc_amount(CURRENT_STRATEGY_NAV),
            }
        ],
        "reason": None,
        "scope": "SYSTEMATIC",
        "status": "AVAILABLE",
        "strategy_id": "strategy-1",
    }
    systematic = {
        "as_of_date": as_of,
        "components": ["strategy-1"],
        "current_value": _cc_amount(CURRENT_STRATEGY_NAV),
        "reason": None,
        "status": "AVAILABLE",
    }
    retained = {
        "as_of_date": None,
        "components": [],
        "current_value": _cc_amount("0"),
        "reason": None,
        "status": "AVAILABLE",
    }
    reserve_nav_day = "2026-07-14"
    reserve_member = {
        "account_id": "account-reserve",
        "as_of_date": reserve_nav_day,
        "authority": "AUTHORITATIVE" if reserve_ready else "NAV_NOT_AUTHORITATIVE",
        "nav_record_id": "nav-account-reserve",
        "provider": "IBKR",
        "status": "AVAILABLE" if reserve_ready else "NOT_READY_AT_CUTOFF",
        "value": _cc_amount(CURRENT_RESERVE_NAV if reserve_ready else None),
    }
    if reserve_ready:
        reserve = {
            "as_of_date": reserve_nav_day,
            "components": ["account-reserve"],
            "current_value": _cc_amount(CURRENT_RESERVE_NAV),
            "members": [reserve_member],
            "reason": None,
            "status": "AVAILABLE",
        }
        total_liquid = {
            "as_of_date": reserve_nav_day,
            "components": ["systematic", "retained_excluded", "reserve"],
            "current_value": _cc_amount("412190.05"),
            "reason": None,
            "status": "AVAILABLE",
        }
    else:
        reserve = {
            "as_of_date": None,
            "components": ["account-reserve"],
            "current_value": _cc_amount(None),
            "members": [reserve_member],
            "reason": "reserve accounts without an authoritative admitted NAV",
            "status": "NOT_READY_AT_CUTOFF",
        }
        total_liquid = {
            "as_of_date": None,
            "components": ["systematic", "retained_excluded", "reserve"],
            "current_value": _cc_amount(None),
            "reason": "total liquid requires every component to be current",
            "status": "NOT_READY_AT_CUTOFF",
        }
    return {
        "admissions": [
            {
                "account_id": "account-reserve",
                "account_reconciliation_sha256": None,
                "disposition": "AUTHORITATIVE" if reserve_ready else "REFERENCE_ONLY",
                "effective_end": as_of,
                "effective_start": "2026-01-01",
                "evidence_layer": "ACCOUNT_LIVE",
                "fact_type": "nav_observations",
                "pool_id": None,
                "provider_id": "ibkr",
                "strategy_id": None,
            }
        ],
        "current_capital": {
            "basis": "LATEST_ADMITTED_USD_NAV_ON_OR_BEFORE_VALUATION_DATE",
            "currency": "USD",
            "retained_excluded": retained,
            "reserve": reserve,
            "strategies": [strategy],
            "systematic": systematic,
            "total_liquid": total_liquid,
            "valuation_date": as_of,
            "weight_basis": "CURRENT_CUTOFF",
        },
        "nav_observations": [
            {
                "account_id": "account-1",
                "as_of_date": as_of,
                "nav": _cc_amount(CURRENT_STRATEGY_NAV),
                "record_id": "nav-account-1",
                "source_key": "provider-ledger",
            },
            {
                "account_id": "account-reserve",
                "as_of_date": reserve_nav_day,
                "nav": _cc_amount(CURRENT_RESERVE_NAV),
                "record_id": "nav-account-reserve",
                "source_key": "provider-ledger" if reserve_ready else "ibkr-ledger",
            },
        ],
    }


def _fixture(
    tmp_path: Path,
    *,
    evaluated_on: date | None = None,
    reserve_ready: bool = False,
    panel_as_of: date | None = None,
) -> tuple[Path, Path]:
    output_root = tmp_path / "candidate"
    stages = output_root / "stages"
    hwm = {"nav_coverage": "1", "status": "AVAILABLE_GOVERNED_HWM_PERIMETER"}
    # A real ledger generation carries source provenance that may outrun the cutoff --
    # `provider_ledger._generation_as_of_date` returns max(cutoff, artifact dates) -- while its
    # financial boundary stays at the cutoff. The baseline inherits that stamp.
    provenance = AS_OF if evaluated_on is None else AS_OF + timedelta(days=1)
    ledger = _generation(
        stages / "ledger",
        "ledger",
        {"provider-ledger.json": {"as_of_date": AS_OF.isoformat()}},
        as_of_date=provenance,
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
            "account_id": "account-1",
            "assignment_id": "assignment-1",
            "effective_end": None,
            "effective_start": "2026-01-01",
            "mapping_status": "MAPPED",
            "pool_id": "systematic-capital",
            "strategy_id": "strategy-1",
            "strategy_mapping_disposition": "MAPPED",
        },
        {
            "account_id": "account-reserve",
            "assignment_id": "assignment-reserve",
            "effective_end": None,
            "effective_start": "2025-09-15",
            "mapping_status": "UNMAPPED",
            "pool_id": "reserve-capital",
            "strategy_id": None,
            "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
        },
    ]
    replay = {
        "reserve_total_usd": "100043.62",
        "systematic_total_usd": "410272.39",
        "total_liquid_usd": "510316.01",
    }
    capital = _current_capital_fixture(reserve_ready=reserve_ready)
    baseline_payload = {
        "allocations": allocations,
        "as_of_date": AS_OF.isoformat(),
        "assignments": assignments,
        "audit_snapshot": {
            "as_of_date": "2026-07-25",
            "replay": dict(replay),
            "status": "HISTORICAL_REFERENCE",
        },
        "current_capital": capital["current_capital"],
        "nav_observations": capital["nav_observations"],
        "provider_admissions": capital["admissions"],
        "provenance": {
            "catalog_sha256": "e" * 64,
            "generation_id": "f" * 64,
        },
        "replay": replay,
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
        # The registry the current capital rows are checked against: one row per
        # registered strategy, none more, none fewer.
        "strategies": [{"strategy_id": "strategy-1"}],
    }
    baseline = _generation(
        stages / "baseline",
        "baseline",
        {"portfolio-baseline.json": baseline_payload},
        as_of_date=provenance,
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
    # The panel manifest is dated at the financial cutoff: readers take it as the data date.
    panel = _generation(
        stages / "panel",
        "panel",
        as_of_date=panel_as_of or AS_OF,
        files={
            "data/dashboard_data.json": panel_payload,
            "qa/interaction-results.json": {"all_passed": True},
            "qa/qa-validation.json": {"checks_passed": 8, "checks_total": 8},
        },
    )
    launcher = stages / "panel" / "index.html"
    launcher.write_text("<!doctype html><title>synthetic</title>", encoding="utf-8")
    _write_json(
        stages / "ledger" / "freshness-health.json",
        {
            "assessment_schema_version": 4,
            "as_of_date": AS_OF.isoformat(),
            "assessments": [],
            "evaluated_on": (evaluated_on or AS_OF).isoformat(),
        },
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
        **(
            {"evaluated_on": evaluated_on.isoformat()}
            if evaluated_on is not None
            else {}
        ),
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


@pytest.mark.parametrize(
    "relative_path",
    (
        "portfolio-report.xlsx",
        "nested/portfolio-report.XLSX",
        "deliveries/smuggled.json",
        "nested/deliveries/smuggled.json",
        "Deliveries/smuggled.json",
    ),
)
def test_report_generation_inventory_rejects_non_authority_delivery_paths(
    tmp_path: Path, relative_path: str
) -> None:
    """An immutable report target must contain authority JSON/evidence only."""

    recipe_path, _output_root = _fixture(tmp_path)
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    report_files = recipe["expected_generations"]["report"]["files"]
    assert isinstance(report_files, list)
    report_files.append(
        {
            "relative_path": relative_path,
            "sha256": hashlib.sha256(b"untrusted workbook").hexdigest(),
            "size_bytes": len(b"untrusted workbook"),
        }
    )
    _write_json(recipe_path, recipe)

    with pytest.raises(
        materializer.MaterializationError,
        match="report generation cannot inventory non-authority workbook delivery",
    ):
        materializer._recipe(recipe_path)


def _install_real_epoch_proof(proof_root: Path, target: Path) -> dict[str, Path]:
    """Install a real producer manifest and content-bound synthetic panel generation."""

    target.mkdir(parents=True, exist_ok=True)
    assignments = [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
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
                    "record_id": "synthetic-nav-as-of",
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
    flow_coverage_path = target / "account_strategy_flow_coverage.json"
    classified_bytes = classified_flows_path.read_bytes()
    _write_json(
        flow_coverage_path,
        {
            "schema_version": 2,
            "artifact_id": "coverage/synthetic-account-flow-coverage.json",
            "as_of_date": AS_OF.isoformat(),
            "ledger_identity": {
                "generation_id": ledger_identity["generation_id"],
                "artifact": ledger_identity["artifact"],
            },
            "classified_flows_artifact": {
                "artifact_id": "classified/synthetic-flows.json",
                "as_of_date": AS_OF.isoformat(),
                "schema": {"name": "classified-flows", "version": "3"},
                "sha256": hashlib.sha256(classified_bytes).hexdigest(),
                "size_bytes": len(classified_bytes),
            },
            "mapped_assignments_identity": {
                "sha256": hashlib.sha256(
                    json.dumps(
                        [
                            {
                                key: assignments[0][key]
                                for key in (
                                    "account_id",
                                    "strategy_id",
                                    "effective_start",
                                    "effective_end",
                                )
                            }
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "count": 1,
            },
            "receipt_artifacts": [],
            "rows": [
                {
                    "assignment": {
                        key: assignments[0][key]
                        for key in (
                            "account_id",
                            "strategy_id",
                            "effective_start",
                            "effective_end",
                        )
                    },
                    "requested_start": AS_OF.isoformat(),
                    "requested_end": AS_OF.isoformat(),
                    "status": "BLOCKED",
                    "authority_kind": "NONE",
                    "receipt_artifact_id": None,
                    "blocker_code": "LEGACY_RECEIPT_UNVERIFIABLE",
                    "blocker_detail": "synthetic direct-consumer fixture has no retained v2 receipt",
                }
            ],
        },
    )
    admission_policy_path = target / "account_strategy_admission_policy.json"
    assignment_sha256 = hashlib.sha256(
        json.dumps(
            [
                {
                    key: assignment[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                }
                for assignment in assignments
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    _write_json(
        admission_policy_path,
        {
            "schema_version": 2,
            "policy_id": "SYNTHETIC-ADMISSION",
            "policy_version": "1",
            "artifact_id": "policy/synthetic-admission.json",
            "as_of_date": AS_OF.isoformat(),
            "assignment_sha256": assignment_sha256,
            "admissions": [
                {
                    **{
                        key: assignments[0][key]
                        for key in (
                            "account_id",
                            "strategy_id",
                            "effective_start",
                            "effective_end",
                        )
                    },
                    "status": "FUNDED",
                    "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "funded_nav_record_ids": ["synthetic-nav-as-of"],
                    "continuity_to_successor": False,
                }
            ],
        },
    )
    membership_census_path = target / "account_strategy_membership_census.json"
    _write_json(
        membership_census_path,
        {
            "census_id": "synthetic-membership-census",
            "current_census": {
                "account_sleeves": [
                    {
                        "provider_id": "SYNTHETIC",
                        "assignments": [
                            {
                                "status": "FUNDED_MEMBERSHIP",
                                "strategy_id": "synthetic-strategy",
                                "effective_start": AS_OF.isoformat(),
                                "effective_end": None,
                            }
                        ],
                    }
                ]
            },
        },
    )
    inputs = {
        "account_strategy_ledger": ledger_path,
        "account_strategy_ledger_identity": ledger_identity_path,
        "account_strategy_classified_flows": classified_flows_path,
        "account_strategy_flow_coverage": flow_coverage_path,
        "account_strategy_admission_policy": admission_policy_path,
    }
    producer = materializer._account_strategy_manifest_module()
    manifest = producer.build_manifest_candidate(
        ledger_path=ledger_path,
        ledger_identity_path=ledger_identity_path,
        baseline_root=proof_root / "stages" / "baseline",
        baseline_generation_id=baseline_generation.name,
        classified_flows_path=classified_flows_path,
        flow_coverage_path=flow_coverage_path,
        classification_policy_path=(
            materializer.ACCOUNT_STRATEGY_CLASSIFICATION_POLICY
        ),
        admission_policy_path=admission_policy_path,
        membership_census_path=membership_census_path,
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
            schema_version="7",
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
        "account_strategy_flow_coverage": "input-bound/manifest_flow_coverage",
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

    materializer.build_spec(
        output,
        tmp_path / "financial-mvp-dag",
        spec_path,
        portfolio_root=tmp_path / "portfolio",
    )

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
    assert assessment["schema_version"] == 2
    assert assessment["assessment_scope"] == "PENDING_GOVERNED_HWM_RESULT"
    assert assessment["outcome_id"] == "AS_HELD_COMPOSITE_HWM"
    assert assessment["state"] == "REQUIRED_NOT_READY"
    assert assessment["input_generation_sha256"] is None
    assert assessment["calendar_authority_sha256"] is None
    assert assessment["result_sha256"] is None
    assert assessment["bindings"] == {}
    assert assessment["outcome_readiness"] == "NOT_DECLARED"
    assert assessment["required_strategy_ids"] == ["strategy-1"]
    assert assessment["unresolved_source_ids"] == []
    assert assessment["blocking_reasons"] == [
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
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


def test_schema_v9_rebinds_exact_epoch_manifest_inputs_into_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The materializer must not let the builder substitute financial inputs."""

    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS",
        tmp_path / "epoch-manifest" / "account_strategy_membership_census.json",
    )
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v9-pipeline-recipe.json"

    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v9-financial-manifest",
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
    output_root = tmp_path / "schema-v9-dag"
    spec_path = output_root / "pipeline-spec.json"
    materializer.build_spec(
        pipeline_recipe,
        output_root,
        spec_path,
        portfolio_root=tmp_path / "portfolio",
    )
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
        "--manifest-flow-coverage": manifest_inputs["account_strategy_flow_coverage"],
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


def test_epoch_manifest_proof_rebuild_binds_the_governed_membership_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The materialization proof rebuild must supply the census like the delivered path."""

    assert materializer.ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS == (
        ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
    )
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS",
        tmp_path / "epoch-manifest" / "account_strategy_membership_census.json",
    )
    rebuild_kwargs: list[dict[str, object]] = []
    producer = materializer._account_strategy_manifest_module()
    original_build = producer.build_manifest_candidate

    def capture_build(**kwargs: object) -> dict[str, object]:
        rebuild_kwargs.append(kwargs)
        return original_build(**kwargs)  # type: ignore[arg-type]

    producer.build_manifest_candidate = capture_build
    monkeypatch.setattr(
        materializer, "_account_strategy_manifest_module", lambda: producer
    )
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v9-pipeline-recipe.json"

    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v9-financial-manifest",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )

    assert len(rebuild_kwargs) == 2
    assert (
        rebuild_kwargs[1]["membership_census_path"]
        == materializer.ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS
    )


def test_core_pipeline_rejects_legacy_provider_admission_adapter_v4(
    tmp_path: Path,
) -> None:
    """Core product authority accepts only the current provider-admission adapter."""

    receipt_bytes = canonical_json_bytes({"fixture": "receipt"})
    catalog_bytes = canonical_json_bytes({"catalog_schema_version": 2})
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("quant-engine-provider-ledger", "4"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic legacy provider-admission adapter",
            fact_types=("provider-admission-receipt",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/admit-or-declare-receipt.json",
                sha256=hashlib.sha256(receipt_bytes).hexdigest(),
                size_bytes=len(receipt_bytes),
                as_of_date=AS_OF,
                schema_name="provider-admission-receipt",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                size_bytes=len(catalog_bytes),
                as_of_date=AS_OF,
                schema_name="provider-ledger-catalog",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = tmp_path / "generation.json"
    receipt_path = tmp_path / "receipt.json"
    catalog_path = tmp_path / "catalog.json"
    contract_path = tmp_path / "contract.json"
    matrix_path = tmp_path / "matrix.json"
    manifest_path.write_bytes(manifest.to_json_bytes())
    receipt_path.write_bytes(receipt_bytes)
    catalog_path.write_bytes(catalog_bytes)
    contract_path.write_bytes(canonical_json_bytes({}))
    matrix_path.write_bytes(canonical_json_bytes({}))

    authority_paths = {
        "financial-mvp-contract-authority": contract_path,
        "financial-mvp-provider-catalog-authority": catalog_path,
        "financial-mvp-provider-manifest-authority": manifest_path,
        "financial-mvp-provider-admission-authority": receipt_path,
        "financial-mvp-source-matrix-authority": matrix_path,
    }
    authority_hashes = {
        "contract_raw_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "provider_catalog_sha256": hashlib.sha256(
            catalog_path.read_bytes()
        ).hexdigest(),
        "provider_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "provider_receipt_sha256": hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest(),
        "source_matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
    }
    product_path = tmp_path / "product.json"
    product_path.write_bytes(
        canonical_json_bytes(
            {
                "authority_hashes": authority_hashes,
                "conformance": {},
                "schema_version": 3,
            }
        )
    )
    gates = tuple(
        PipelineGate(
            gate_id=gate_id,
            path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for gate_id, path in {
            **authority_paths,
            "financial-mvp-product-conformance": product_path,
        }.items()
    )
    spec = PipelineSpec(
        pipeline_id="legacy-provider-admission-adapter",
        as_of_date=AS_OF,
        code_commit="a" * 40,
        created_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        coordination_root=tmp_path,
        state_root=tmp_path,
        roots={},
        nodes=(),
        gates=gates,
        companions=(),
    )

    with pytest.raises(
        PipelineValidationError,
        match="provider admission manifest authority differs",
    ):
        core_pipeline._financial_mvp_product_authority(spec)  # noqa: SLF001


def test_schema_v9_declares_the_authority_snapshot_it_emits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS",
        tmp_path / "epoch-manifest" / "account_strategy_membership_census.json",
    )
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v9-pipeline-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v9-authority-snapshot",
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
    output_root = tmp_path / "schema-v9-dag"
    spec_path = output_root / "pipeline-spec.json"
    materializer.build_spec(
        pipeline_recipe,
        output_root,
        spec_path,
        portfolio_root=tmp_path / "portfolio",
    )

    declared = next(
        node
        for node in json.loads(spec_path.read_text(encoding="utf-8"))["nodes"]
        if node["node_id"] == "authority-snapshot"
    )
    emitted = (authority_dir / "authority-snapshot.json").read_bytes()
    assert declared["sha256"] == hashlib.sha256(emitted).hexdigest()
    assert declared["size_bytes"] == len(emitted)


@pytest.mark.parametrize(
    "input_key",
    (
        "account_strategy_classified_flows",
        "account_strategy_flow_coverage",
    ),
)
def test_schema_v9_rejects_epoch_manifest_input_drift_after_recipe_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_key: str
) -> None:
    source_recipe, proof_root = _fixture(tmp_path)
    source = json.loads(source_recipe.read_text(encoding="utf-8"))
    vertical_path = Path(source["vertical_recipe"]["path"])
    vertical = json.loads(vertical_path.read_text(encoding="utf-8"))
    product = financial_fixture.build_financial_mvp_fixture(tmp_path / "product")
    manifest_inputs = _install_real_epoch_proof(proof_root, tmp_path / "epoch-manifest")
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS",
        tmp_path / "epoch-manifest" / "account_strategy_membership_census.json",
    )
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    pipeline_recipe = tmp_path / "schema-v9-pipeline-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=proof_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v9-financial-manifest",
        output=pipeline_recipe,
        financial_mvp_contract=product.contract_path,
        financial_mvp_source_matrix=product.matrix_path,
    )
    _write_json(manifest_inputs[input_key], {"input": input_key, "version": 2})

    with pytest.raises(
        materializer.MaterializationError,
        match=f"financial epoch manifest {input_key} identity differs",
    ):
        materializer._recipe(pipeline_recipe)


def test_schema_v9_rejects_missing_required_epoch_manifest_input(
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
        if key == "account_strategy_flow_coverage":
            continue
        path = tmp_path / "epoch-manifest" / f"{key}.json"
        _write_json(path, {"input": key})
        vertical[key] = _definition(path)
    vertical["schema_version"] = 9

    _write_json(vertical_path, vertical)

    with pytest.raises(
        materializer.MaterializationError,
        match="financial epoch manifest schema-9 inputs differ",
    ):
        materializer._vertical_source_paths(vertical_path)


def test_schema_v9_rejects_any_extra_financial_input_definition(
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
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)

    with pytest.raises(
        materializer.MaterializationError,
        match="financial epoch manifest schema-9 inputs differ",
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


def test_current_admission_reopens_sources_and_rejects_coherent_omissions(
    tmp_path: Path,
) -> None:
    """Real small stage output crosses the materializer's independent boundary."""
    fixture_spec = importlib.util.spec_from_file_location(
        "current_source_stage_fixture",
        ROOT / "tests/provider_ledger/test_admit_or_declare.py",
    )
    assert fixture_spec is not None and fixture_spec.loader is not None
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    generation, authority, _ = fixture._source_bound_stage(tmp_path)
    arguments = {
        key: authority[key]
        for key in ("contract_raw_sha256", "contract_semantic_sha256", "census_sha256")
    }
    accepted = materializer._provider_admission_authority(
        generation, authority["contract"], **arguments
    )
    assert accepted["provider_count"] == 7
    assert accepted["inventory_count"] == accepted["disposition_count"]
    original_receipt = json.loads(
        (generation / "admit-or-declare-receipt.json").read_bytes()
    )
    original_catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    original_manifest = GenerationManifest.from_json_bytes(
        (generation / "generation.json").read_bytes()
    )
    provider_generations = {
        row["provider_id"]: row for row in original_receipt["provider_generations"]
    }
    inventory, _ = fixture.ledger.validate_admit_or_declare_receipt(
        original_receipt,
        product_authority=authority,
        source_segments=authority["sources"],
        provider_generations=provider_generations,
    )
    for attack in (
        "omitted_dataset",
        "changed_materialized_nav",
        "changed_finality",
        "unselected_provider_return",
        "future_effective_date",
        "future_nav_recognition_date",
        "provider_future_nav_recognition_date",
    ):
        receipt = copy.deepcopy(original_receipt)
        catalog = copy.deepcopy(original_catalog)
        if attack == "omitted_dataset":
            omitted = next(
                item
                for item in inventory
                if item.provider_id == "schwab"
                and item.item_id == "unreferenced_collection"
            )
            for field in ("inventory", "dispositions"):
                receipt[field] = [
                    row
                    for row in receipt[field]
                    if not (
                        row["provider_id"] == omitted.provider_id
                        and row["item_kind"] == omitted.item_kind.value
                        and row["item_id"] == omitted.item_id
                    )
                ]
            observed = next(
                row
                for row in receipt["run_observation"]["providers"]
                if row["provider_id"] == "schwab"
            )
            observed["observed_inventory_count"] -= 1
            observed["observed_inventory_sha256"] = fixture.ledger._inventory_digest(
                [
                    item
                    for item in inventory
                    if item.provider_id == "schwab" and item != omitted
                ]
            )
            fixture._rehash_receipt(receipt)
        elif attack == "changed_materialized_nav":
            row = next(
                row
                for row in catalog["nav_observations"]
                if row["provider_id"] == "matrixport"
            )
            row["nav"]["value"] = "101"
        elif attack == "unselected_provider_return":
            catalog["provider_strategy_returns"].append(
                {
                    "provider_id": "synthetic-unselected-provider",
                    "strategy_id": "synthetic-unselected-strategy",
                    "date": "2026-08-14",
                    "return": "0.99",
                }
            )
        elif "future_" in attack:
            row = next(
                row
                for row in catalog["capital_anchors"]
                if row["provider_id"] == "quantbase"
            )
            field = attack.removeprefix("provider_").removeprefix("future_")
            row[field] = (
                "2026-08-15" if attack.startswith("provider_") else "2026-09-18"
            )
        else:
            observed = next(
                row
                for row in receipt["run_observation"]["providers"]
                if row["provider_id"] == "schwab"
            )
            observed["finalized_through"] = "2026-08-13"
        # All candidate-only receipt checks still pass. Rehash both emitted
        # artifacts and the outer generation so rejection must use the selected
        # source bytes and the actual materialized rows, not a stale outer hash.
        fixture.ledger.validate_admit_or_declare_receipt(
            receipt,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )
        contents = {
            "normalized/admit-or-declare-receipt.json": canonical_json_bytes(receipt),
            "normalized/provider-ledger.json": canonical_json_bytes(catalog),
        }
        manifest = GenerationManifest.build(
            as_of_date=original_manifest.as_of_date,
            adapter=original_manifest.adapter,
            coverage=original_manifest.coverage,
            verification=original_manifest.verification,
            artifacts=tuple(
                replace(
                    artifact,
                    sha256=hashlib.sha256(contents[artifact.artifact_id]).hexdigest(),
                    size_bytes=len(contents[artifact.artifact_id]),
                )
                if artifact.artifact_id in contents
                else artifact
                for artifact in original_manifest.artifacts
            ),
        )
        forged = tmp_path / attack / manifest.generation_id
        forged.mkdir(parents=True)
        (forged / "generation.json").write_bytes(manifest.to_json_bytes())
        for artifact_id, content in contents.items():
            (forged / Path(artifact_id).name).write_bytes(content)
        reason = (
            "materialized provider fact scope differs"
            if attack == "unselected_provider_return"
            else "materialized provider fact cutoff or generation differs"
            if "future_" in attack
            else "independent source inventory or materialized facts"
        )
        with pytest.raises(materializer.MaterializationError, match=reason):
            materializer._provider_admission_authority(
                forged, authority["contract"], **arguments
            )
    assert not (tmp_path / "staged/CURRENT.json").exists()


def test_current_admission_separates_financial_cutoff_from_generation_date(
    tmp_path: Path,
) -> None:
    fixture_spec = importlib.util.spec_from_file_location(
        "current_source_stage_date_fixture",
        ROOT / "tests/provider_ledger/test_admit_or_declare.py",
    )
    assert fixture_spec is not None and fixture_spec.loader is not None
    fixture = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixture)
    generation, authority, _ = fixture._source_bound_stage(tmp_path)
    arguments = {
        key: authority[key]
        for key in ("contract_raw_sha256", "contract_semantic_sha256", "census_sha256")
    }
    original = GenerationManifest.from_json_bytes(
        (generation / "generation.json").read_bytes()
    )
    later = original.as_of_date + timedelta(days=1)
    rebound = GenerationManifest.build(
        as_of_date=later,
        adapter=original.adapter,
        coverage=original.coverage,
        artifacts=original.artifacts,
        verification=original.verification,
    )
    forged = tmp_path / "later-generation" / rebound.generation_id
    forged.mkdir(parents=True)
    (forged / "generation.json").write_bytes(rebound.to_json_bytes())
    for name in (
        "admit-or-declare-receipt.json",
        "provider-ledger.json",
    ):
        (forged / name).write_bytes((generation / name).read_bytes())

    with pytest.raises(
        materializer.MaterializationError,
        match="provider ledger generation date differs",
    ):
        materializer._provider_admission_authority(
            forged, authority["contract"], **arguments
        )

    catalog = json.loads((forged / "provider-ledger.json").read_bytes())
    catalog["as_of_date"] = later.isoformat()
    tampered_catalog = canonical_json_bytes(catalog)
    tampered_manifest = GenerationManifest.build(
        as_of_date=later,
        adapter=rebound.adapter,
        coverage=rebound.coverage,
        artifacts=tuple(
            replace(
                artifact,
                sha256=hashlib.sha256(tampered_catalog).hexdigest(),
                size_bytes=len(tampered_catalog),
            )
            if artifact.artifact_id == "normalized/provider-ledger.json"
            else artifact
            for artifact in rebound.artifacts
        ),
        verification=rebound.verification,
    )
    tampered = tmp_path / "tampered-generation" / tampered_manifest.generation_id
    tampered.mkdir(parents=True)
    (tampered / "generation.json").write_bytes(tampered_manifest.to_json_bytes())
    (tampered / "provider-ledger.json").write_bytes(tampered_catalog)
    (tampered / "admit-or-declare-receipt.json").write_bytes(
        (generation / "admit-or-declare-receipt.json").read_bytes()
    )
    with pytest.raises(
        materializer.MaterializationError,
        match="provider ledger catalog authority differs",
    ):
        materializer._provider_admission_authority(
            tampered, authority["contract"], **arguments
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
    ] == ["report", "panel"]
    assert all(
        baseline_ids[stage] == changed_ids[stage]
        for stage in ("ledger", "baseline", "attribution", "analytics")
    )


@pytest.mark.parametrize("actual_blocker", [None, "provider", "correction"])
def test_candidate_eligibility_separates_frozen_readiness_from_admission(
    tmp_path: Path, actual_blocker: str | None
) -> None:
    product = financial_fixture.build_financial_mvp_fixture(
        tmp_path / "product", ready=True
    )
    registry = _runtime_registry(
        tmp_path / "registry",
        name="corrections",
        semantic_sha256=hashlib.sha256(product.contract_path.read_bytes()).hexdigest(),
        census_sha256=hashlib.sha256(
            canonical_json_bytes(product.contract["current_census"])
        ).hexdigest(),
        records=[],
    )
    rederive = materializer._authority_registry(
        None,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    conformance = _fixture_conformance(product, registry, rederive)
    source = conformance["sources"][0]
    source["product_requirement"] = "REQUIRED"
    source["readiness"] = "REQUIRED_NOT_READY"
    source["current_generation"] = None
    frozen_blocker = f"SOURCE:{source['source_id']}"
    conformance["blocking_reasons"] = [frozen_blocker]
    if actual_blocker == "provider":
        conformance["provider_admission"]["product_readiness"] = "REQUIRED_NOT_READY"
    elif actual_blocker == "correction":
        conformance["correction_authority"]["applicable_open_correction_ids"] = [
            "restated-nav"
        ]
    summary = core_pipeline._financial_mvp_summary(
        {
            "conformance": conformance,
            "hwm_input_authority": {
                "schema_version": 2,
                "state": "UNAVAILABLE",
                "blocking_reasons": ["HWM_INPUTS_NOT_BUILT"],
            },
        }
    )

    assert summary is not None
    assert summary["ready_for_execution"] is (actual_blocker is None)
    expected = {
        None: [],
        "provider": ["PROVIDER_ADMISSION"],
        "correction": ["OPEN_CORRECTION:restated-nav"],
    }
    assert summary["blocking_reasons"] == expected[actual_blocker]
    assert frozen_blocker in summary["completion_blocking_reasons"]
    assert "HWM_INPUTS_NOT_BUILT" in summary["completion_blocking_reasons"]
    assert source["source_id"] in summary["required_not_ready_sources"]


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


def test_generation_date_rule_separates_data_date_from_calculation_date() -> None:
    """Data-dated roles may lag the release; the panel is generated at release.

    The ledger, the baseline and the report all carry the date of the market data
    they cover, which precedes the date the release is calculated -- the
    release-QC planning date is pinned to the public factor capture date, and the
    plan records `complete_through` and `evaluated_on` separately with an age
    threshold between them.  Requiring the baseline and report to equal the
    release date, while exempting the ledger they are computed from, made every
    release unsatisfiable whenever the market data was even one day behind.
    """

    earlier = date(2026, 7, 31)
    later = date(2026, 8, 4)

    for role in ("ledger", "baseline", "report"):
        assert materializer._generation_as_of_allowed(role, earlier, AS_OF)
        assert materializer._generation_as_of_allowed(role, AS_OF, AS_OF)
        # A generation dated after the release it belongs to is still refused.
        assert not materializer._generation_as_of_allowed(role, later, AS_OF)

    # With no cutoff the panel is held to the release date, as before.
    assert materializer._generation_as_of_allowed("panel", AS_OF, AS_OF)
    assert not materializer._generation_as_of_allowed("panel", earlier, AS_OF)


def test_panel_manifest_must_be_dated_at_the_financial_cutoff() -> None:
    """R2-3: the panel manifest date is read as the data date, so it is the cutoff.

    Dating it at the later execution clock made weekly, retained-cutoff and ledger
    readers take the execution date for data; the gate now refuses that, and refuses a
    panel older than the cutoff just as firmly.
    """

    cutoff = date(2026, 7, 31)
    execution = date(2026, 8, 4)

    assert materializer._generation_as_of_allowed("panel", cutoff, execution, cutoff)
    assert not materializer._generation_as_of_allowed(
        "panel", execution, execution, cutoff
    )
    assert not materializer._generation_as_of_allowed(
        "panel", date(2026, 7, 30), execution, cutoff
    )
    # Regression guard: the data-dated roles keep their on-or-before-release rule.
    for role in ("ledger", "baseline", "report"):
        assert materializer._generation_as_of_allowed(role, cutoff, execution, cutoff)
        assert materializer._generation_as_of_allowed(
            role, cutoff + timedelta(days=1), execution, cutoff
        )


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


def _current_lens_release_fixture(tmp_path: Path) -> tuple[dict, dict]:
    contract = json.loads(
        (ROOT / "contracts/portfolio-financial-mvp-source-v1.json").read_bytes()
    )
    plan = json.loads(materializer.CURRENT_STRATEGY_REGISTRY.read_bytes())
    rows = []
    modes = {}
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        surfaces = {}
        for surface in ("STATIC", "DYNAMIC_FLS"):
            sources = []
            for source_key in plan["sources"]:
                root = (
                    tmp_path
                    / "stages"
                    / "analytics"
                    / "decomposition-lenses"
                    / mode
                    / surface
                    / source_key
                )
                spec_id = f"{mode}-{surface}-{source_key}"
                generation = _generation(
                    root, spec_id, {"decomposition.json": {"synthetic": spec_id}}
                )
                _write_json(root / "CURRENT.json", {"generation_id": generation.name})
                rows.append(
                    {
                        "evidence_mode": mode,
                        "surface": surface,
                        "source_key": source_key,
                        "spec_id": spec_id,
                        "generation_id": generation.name,
                        "root": str(root),
                    }
                )
                sources.append(
                    {"source_key": source_key, "generation_id": generation.name}
                )
            surfaces[surface] = {"sources": sources}
        modes[mode] = [{"surfaces": surfaces}]
    _write_json(
        tmp_path / "stage-results" / "analytics.json",
        {
            "schema_version": 2,
            "lens_generation_bindings": rows,
        },
    )
    return contract, {"decomposition_lenses": {"modes": modes}}


DECOMPOSITION_AUTHORITY = "d" * 64


def test_current_lens_generations_share_existing_release_roots(tmp_path: Path) -> None:
    if sys.platform == "win32":
        tmp_path = Path("\\\\?\\" + str(tmp_path.resolve()))
    contract, panel = _current_lens_release_fixture(tmp_path)
    nodes = materializer._production_lens_nodes(tmp_path, contract)
    assert len(nodes) == 24
    assert len({row["root_id"] for row in nodes.values()}) == 12
    assert {row["root_id"] for row in nodes.values()} <= set(
        materializer.PRODUCTION_ROOTS
    )
    assert sum(row["release_target"] for row in nodes.values()) == 12
    materializer._verify_panel_lens_nodes(panel, nodes, DECOMPOSITION_AUTHORITY)
    for (mode, surface, source_key), row in nodes.items():
        assert row["release_target"] is (surface == "STATIC")
        assert row["root_id"] == (
            f"hybrid-{source_key}" if mode == "LIVE_PLUS_BACKTEST" else source_key
        )
        assert row["files"] == inventory_generation(Path(row["source_dir"]))["files"]
    panel["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]["surfaces"]["DYNAMIC_FLS"][
        "sources"
    ][0]["generation_id"] = "0" * 64
    with pytest.raises(materializer.MaterializationError, match="panel lens"):
        materializer._verify_panel_lens_nodes(panel, nodes, DECOMPOSITION_AUTHORITY)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "pointer", "root"])
def test_current_lens_generation_bindings_reject_drift(
    tmp_path: Path, mutation: str
) -> None:
    if sys.platform == "win32":
        tmp_path = Path("\\\\?\\" + str(tmp_path.resolve()))
    contract, _panel = _current_lens_release_fixture(tmp_path)
    path = tmp_path / "stage-results" / "analytics.json"
    value = json.loads(path.read_bytes())
    rows = value["lens_generation_bindings"]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "pointer":
        _write_json(Path(rows[0]["root"]) / "CURRENT.json", {"generation_id": "0" * 64})
    else:
        rows[0]["root"] = str(tmp_path / "wrong-root")
    _write_json(path, value)
    with pytest.raises(materializer.MaterializationError, match="lens"):
        materializer._production_lens_nodes(tmp_path, contract)


WORK_COUNTER_KEYS = (
    "correlation_build_calls",
    "full_rederivation_count",
    "grid_search_calls",
    "model_fit_calls",
    "receipt_reuse_count",
)


def _stub_financial_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    counters: dict[str, int] | None = None,
) -> list[str]:
    """Local stand-in for the numerical verifier, with the ruled call interface.

    The real verifier is exercised without stubs by test_portfolio_mvp2_vertical and
    by the verifier's own tests. This stand-in only records which caller label each
    call carried and returns a proof shaped like the real one: `work_counters` with
    exactly the five keys.

    Both the ordinary and deferred-ledger entry points are stubbed, since
    production-spec building calls the deferred one and other call sites (the
    semantic gate, portfolio_qc) call the ordinary one.
    """

    import portfolio_financial_outcomes

    calls: list[str] = []
    reported = counters or {key: 0 for key in WORK_COUNTER_KEYS}

    def validate(
        _spec: object, *, allow_synthetic: bool = False, caller: str = "unattributed"
    ) -> dict[str, object]:
        assert allow_synthetic is False
        calls.append(caller)
        return {"status": "PASS", "work_counters": dict(reported)}

    def validate_with_deferred_ledger(
        _spec: object, *, allow_synthetic: bool = False, caller: str = "unattributed"
    ) -> tuple[dict[str, object], Any]:
        assert allow_synthetic is False
        calls.append(caller)

        def finalize(_final_spec: object) -> None:
            # This stand-in never touches the real on-disk ledger (callers.append
            # above is the only record of this call); a dedicated test exercises
            # the real deferred-ledger writer and its on-disk identity directly.
            return None

        return {"status": "PASS", "work_counters": dict(reported)}, finalize

    monkeypatch.setattr(
        portfolio_financial_outcomes, "validate_financial_outcomes", validate
    )
    monkeypatch.setattr(
        portfolio_financial_outcomes,
        "validate_financial_outcomes_with_deferred_ledger",
        validate_with_deferred_ledger,
    )
    return calls


def _bind_attended_run(prepared_root: Path, retained_cache_root: Path) -> None:
    """Write the attended-run receipt a real attended run leaves in its root.

    The request identity is produced by the materializer's own function, so the
    receipt has the shape `--execute` writes, with `retained_cache_root` set to the
    given root (an overlay root when a retained cache base is used).
    """

    retained_cache_root.mkdir(parents=True, exist_ok=True)
    python = prepared_root.parent / "attended-python.exe"
    python.write_bytes(b"python")
    source_run = prepared_root.parent / "attended-source-run.json"
    source_run.write_bytes(b"{}")
    identity = materializer._attended_request_identity(
        repo_root=Path(materializer.__file__).resolve().parents[1],
        python=python,
        workspace_root=prepared_root.parent / "workspace",
        output_root=prepared_root,
        retained_cache_root=retained_cache_root,
        requested_as_of="2026-09-17",
        evaluated_on=None,
        source_run=source_run,
        financial_stage_inputs={},
        source_phase={"status": "PASS"},
        runtime={"identity": {"runtime": "test"}},
    )
    receipt = materializer._attended_run_receipt(prepared_root)
    receipt.write_bytes(
        canonical_json_bytes({"schema_version": 1, "request_identity": identity})
    )


@pytest.mark.parametrize("current_surfaces", [False, True])
def test_production_spec_binds_stable_twenty_root_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current_surfaces: bool
) -> None:
    if sys.platform == "win32":
        tmp_path = Path("\\\\?\\" + str(tmp_path.resolve()))
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
    current_nodes = {}
    current_panel = {}
    if current_surfaces:
        lens_contract, current_panel = _current_lens_release_fixture(prepared_root)
        current_nodes = materializer._production_lens_nodes(
            prepared_root, lens_contract
        )
        # The older provider fixture has only a capital outcome. Keep this
        # regression scoped to release routing; the lens helper still checks
        # the real current contract and actual immutable fixture generations.
        lens_node_builder = materializer._production_lens_nodes
        monkeypatch.setattr(
            materializer,
            "_production_lens_nodes",
            lambda root, _contract: lens_node_builder(root, lens_contract),
        )
        analytics_ids.update(
            {
                row["root_id"]: row["generation_id"]
                for row in current_nodes.values()
                if row["release_target"]
            }
        )
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
        payload["decomposition_lenses"] = (
            current_panel["decomposition_lenses"]
            if current_surfaces
            else _hybrid_panel_lenses(analytics_ids)
        )

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

    # This test isolates the established root/recovery mapping. Numerical
    # completion is exercised without stubs by test_portfolio_mvp2_vertical.
    _stub_financial_outcomes(monkeypatch)
    state_root = tmp_path / "retained-cache"
    _bind_attended_run(prepared_root, state_root)
    financial_artifacts = tmp_path / "financial-artifacts.json"
    supplied_lenses = []
    if current_surfaces:
        for index, lens in enumerate(current_nodes.values()):
            supplied = {
                **lens,
                "release_target": False,
                "node_id": f"existing-lens-{index}",
            }
            supplied_lenses.append(supplied)
            definition = _definition(Path(lens["source_dir"]) / "decomposition.json")
            supplied_lenses.append(
                {
                    **definition,
                    "kind": "ARTIFACT",
                    "node_id": f"bound-lens-{index}",
                    "depends_on": [supplied["node_id"]],
                }
            )
    _write_json(
        financial_artifacts,
        [
            {
                **_definition(vertical_path),
                "node_id": "root-mapping-fixture-source",
                "depends_on": [],
                "kind": "ARTIFACT",
            },
            *supplied_lenses,
        ],
    )
    # A published generation an external agent rewrote can only be superseded
    # through the pipeline's own `predecessor-authority-repair` gate, and the
    # production specification is the only place that gate can enter. The gate
    # id has to be the one the pipeline verifies, so it is asserted against
    # that constant rather than against a copy of the string.
    repair_gate_path = tmp_path / "predecessor-authority-repair.json"
    repair_gate_path.write_bytes(
        canonical_json_bytes(
            {
                "authority_sha256": "a" * 64,
                "disposition": "SUPERSEDE_WITH_VERIFIED_GENERATIONS",
                "expected_inventory_drifts": [
                    {
                        "generation_id": "b" * 64,
                        "observed_inventory_sha256": "c" * 64,
                        "recorded_inventory_sha256": "d" * 64,
                        "root_id": "portfolio-report",
                    }
                ],
                "release_id": "e" * 64,
                "release_manifest_sha256": "f" * 64,
                "schema_version": 1,
            }
        )
    )
    result = materializer.build_production_spec(
        recipe_path,
        prepared_root,
        prepared_root / "evidence" / "post-build-semantic.json",
        production_root,
        state_root,
        spec_path,
        financial_artifacts_path=financial_artifacts,
        predecessor_repair_gate_path=repair_gate_path,
    )

    spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    repair_gates = [
        gate
        for gate in spec.gates
        if gate.gate_id == core_pipeline._PREDECESSOR_REPAIR_GATE
    ]
    assert len(repair_gates) == 1
    assert repair_gates[0].path == repair_gate_path
    assert (
        repair_gates[0].sha256
        == hashlib.sha256(repair_gate_path.read_bytes()).hexdigest()
    )

    # Omitting it must leave the gate list exactly as it was; the repair is an
    # explicit, evidenced act and never a default.
    plain_spec_path = tmp_path / "production-spec-no-repair.json"
    materializer.build_production_spec(
        recipe_path,
        prepared_root,
        prepared_root / "evidence" / "post-build-semantic.json",
        production_root,
        state_root,
        plain_spec_path,
        financial_artifacts_path=financial_artifacts,
    )
    plain = PipelineSpec.from_json_bytes(plain_spec_path.read_bytes())
    assert not [
        gate
        for gate in plain.gates
        if gate.gate_id == core_pipeline._PREDECESSOR_REPAIR_GATE
    ]
    assert [gate.gate_id for gate in spec.gates] == [
        *[gate.gate_id for gate in plain.gates],
        core_pipeline._PREDECESSOR_REPAIR_GATE,
    ]

    assert result["root_count"] == 20
    assert set(spec.roots) == set(materializer.PRODUCTION_ROOTS)

    # REL-PROVIDER-ROOTS-001: the provider and epoch-manifest roots join the same
    # release as targets. Opt-in, and every generation must be the ledger's own.
    if not current_surfaces:
        from test_provider_release_roots import (
            _Fixture as _ProviderFixture,
            _manifest as _provider_manifest,
        )

        providers = _ProviderFixture(tmp_path / "providers")
        monkeypatch.setattr(
            materializer,
            "_ledger_provider_bindings",
            lambda _ledger: {row["provider_id"]: row for row in providers.bindings},
        )
        real_recipe = materializer._recipe
        monkeypatch.setattr(
            materializer,
            "_recipe",
            lambda path: {
                **real_recipe(path),
                "epoch_manifest_generation": providers.recipe[
                    "epoch_manifest_generation"
                ],
            },
        )
        # A provider pointer that dangles must travel through the recovery gate.
        dangling_root = production_root.parent / "dataSchwab"
        stage_generation(dangling_root, _provider_manifest("gone"))
        (dangling_root / "CURRENT.json").write_bytes(
            CurrentPointer(
                generation_id="0" * 64,
                manifest_sha256="1" * 64,
                artifact_inventory_sha256="2" * 64,
                pointer_schema_version=2,
            ).to_json_bytes()
        )
        provider_spec_path = tmp_path / "with-providers" / "production-spec.json"
        provider_result = materializer.build_production_spec(
            recipe_path,
            prepared_root,
            prepared_root / "evidence" / "post-build-semantic.json",
            production_root,
            state_root,
            provider_spec_path,
            financial_artifacts_path=financial_artifacts,
            provider_generations_path=providers.listing(),
        )
        provider_spec = PipelineSpec.from_json_bytes(provider_spec_path.read_bytes())
        assert provider_result["root_count"] == 20 + len(materializer.PROVIDER_ROOTS)
        assert set(provider_spec.roots) == set(materializer.PRODUCTION_ROOTS) | set(
            materializer.PROVIDER_ROOTS
        )
        # the seven providers less the PARTIAL IBKR evidence, plus the epoch manifest
        assert set(provider_spec.release_generations) - set(
            spec.release_generations
        ) == set(materializer.PROVIDER_ROOTS) - {"provider-ibkr"}
        assert "provider-ibkr" not in provider_spec.release_generations
        assert (
            provider_spec.roots["provider-ibkr"] == production_root.parent / "dataIBKR"
        )
        retained = next(
            node
            for node in provider_spec.nodes
            if node.node_id == "retain-provider-ibkr"
        )
        assert retained.release_target is False
        recovery = [
            gate
            for gate in provider_spec.gates
            if gate.gate_id == core_pipeline._POINTER_RECOVERY_GATE
        ]
        assert len(recovery) == 1
        recovery_rows = json.loads(recovery[0].path.read_text(encoding="utf-8"))[
            "unusable_pointers"
        ]
        assert [row["root_id"] for row in recovery_rows] == ["provider-schwab"]
        # a plain build never grows the gate list or the root set
        assert not [
            gate
            for gate in plain.gates
            if gate.gate_id == core_pipeline._POINTER_RECOVERY_GATE
        ]
    if current_surfaces:
        selected = {node.node_id: node for node in spec.nodes}
        assert len(spec.release_generations) == 20
        for lens in current_nodes.values():
            matches = [
                node
                for node in spec.nodes
                if getattr(node, "generation_id", None) == lens["generation_id"]
                and getattr(node, "root_id", None) == lens["root_id"]
            ]
            assert len(matches) == 1
            node = matches[0]
            assert node.generation_id == lens["generation_id"]
            assert node.source_dir == Path(lens["source_dir"])
            assert node.release_target == lens["release_target"]
        for index, lens in enumerate(current_nodes.values()):
            artifact = selected[f"bound-lens-{index}"]
            assert artifact.depends_on == (
                lens["node_id"] if lens["release_target"] else f"existing-lens-{index}",
            )
    assert spec.coordination_root == production_root / "releases-refresh-2026"
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
    # Structural binding cannot make this unfinished HWM fixture executable.
    with pytest.raises(PipelineValidationError, match="available governed HWM"):
        plan_pipeline(spec)
    assert pipeline_status(spec)["financial_mvp"]["ready_for_execution"] is False

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


def _production_spec_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    counters: dict[str, int] | None = None,
) -> SimpleNamespace:
    """A prepared attended run whose production specification is ready to build.

    The numerical verifier is the local stand-in of `_stub_financial_outcomes`; the
    prepared root carries a real attended-run receipt that names the retained cache
    root, which is the only state root `production-spec` accepts.
    """

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
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_GOVERNED_MEMBERSHIP_CENSUS",
        tmp_path / "epoch-manifest" / "account_strategy_membership_census.json",
    )
    for key, path in manifest_inputs.items():
        vertical[key] = _definition(path)
    vertical.pop("provider_ledger_spec")
    vertical["provider_ledger_generation"] = _dir_definition(product.generation)
    vertical["schema_version"] = 9
    _write_json(vertical_path, vertical)
    monkeypatch.setattr(
        materializer, "_financial_mvp_module", lambda: product.financial_module
    )
    recipe_path = tmp_path / "schema-v9-production-recipe.json"
    materializer.create_recipe(
        vertical_recipe=vertical_path,
        proof_root=prepared_root,
        qa_dir=Path(source["qa_dir"]["path"]),
        pipeline_id="schema-v9-production-seam",
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

    # This fixture proves epoch/root rebinding only. The complete production
    # vertical supplies and verifies the actual four numerical outcomes.
    monkeypatch.setattr(materializer, "_production_lens_nodes", lambda *_args: {})
    verifier_calls = _stub_financial_outcomes(monkeypatch, counters)
    financial_artifacts = tmp_path / "epoch-rebind-source-artifacts.json"
    _write_json(
        financial_artifacts,
        [
            {
                **_definition(vertical_path),
                "node_id": "epoch-rebind-fixture-source",
                "depends_on": [],
                "kind": "ARTIFACT",
            }
        ],
    )
    state_root = tmp_path / "retained-cache"
    _bind_attended_run(prepared_root, state_root)
    return SimpleNamespace(
        recipe_path=recipe_path,
        prepared_root=prepared_root,
        semantic_gate=prepared_root / "evidence" / "post-build-semantic.json",
        production_root=production_root,
        state_root=state_root,
        spec_path=spec_path,
        financial_artifacts=financial_artifacts,
        accepted_recipe=accepted_recipe,
        prepared_proof=prepared_proof,
        verifier_calls=verifier_calls,
    )


def test_schema_v9_production_spec_binds_real_epoch_manifest_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-v9 five-input producer evidence survives the production rebind seam."""
    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    recipe_path = inputs.recipe_path
    prepared_root = inputs.prepared_root
    spec_path = inputs.spec_path
    accepted_recipe = inputs.accepted_recipe
    prepared_proof = inputs.prepared_proof
    result = materializer.build_production_spec(
        recipe_path,
        prepared_root,
        inputs.semantic_gate,
        inputs.production_root,
        inputs.state_root,
        spec_path,
        financial_artifacts_path=inputs.financial_artifacts,
    )

    spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    production_contract = json.loads(
        Path(result["contract"]).read_text(encoding="utf-8")
    )
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
    assert core_pipeline._FINANCIAL_EPOCH_MANIFEST_INPUTS == {
        "account_strategy_admission_policy",
        "account_strategy_classified_flows",
        "account_strategy_flow_coverage",
        "account_strategy_ledger",
        "account_strategy_ledger_identity",
    }
    with pytest.raises(PipelineValidationError, match="available governed HWM"):
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


def _production_spec_argv(inputs: SimpleNamespace, state_root: Path) -> list[str]:
    return [
        "production-spec",
        "--recipe",
        str(inputs.recipe_path),
        "--prepared-root",
        str(inputs.prepared_root),
        "--semantic-gate",
        str(inputs.semantic_gate),
        "--portfolio-root",
        str(inputs.production_root),
        "--state-root",
        str(state_root),
        "--spec",
        str(inputs.spec_path),
        "--financial-artifacts",
        str(inputs.financial_artifacts),
    ]


def _build_production_spec(inputs: SimpleNamespace, state_root: Path) -> dict[str, Any]:
    return materializer.build_production_spec(
        inputs.recipe_path,
        inputs.prepared_root,
        inputs.semantic_gate,
        inputs.production_root,
        state_root,
        inputs.spec_path,
        financial_artifacts_path=inputs.financial_artifacts,
    )


COLD_WORK_COUNTERS = {
    "correlation_build_calls": 2,
    "full_rederivation_count": 3,
    "grid_search_calls": 5,
    "model_fit_calls": 7,
    "receipt_reuse_count": 11,
}
WARM_WORK_COUNTERS = {
    "correlation_build_calls": 0,
    "full_rederivation_count": 0,
    "grid_search_calls": 0,
    "model_fit_calls": 0,
    "receipt_reuse_count": 13,
}


def test_production_spec_stdout_carries_the_verifier_work_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Z1/M8: the counters of the verifier call reach the command's own stdout."""

    inputs = _production_spec_inputs(tmp_path, monkeypatch, COLD_WORK_COUNTERS)

    assert materializer.main(_production_spec_argv(inputs, inputs.state_root)) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"contract", "nodes", "root_count", "spec", "work_counters"}
    assert tuple(sorted(result["work_counters"])) == WORK_COUNTER_KEYS
    assert result["work_counters"] == COLD_WORK_COUNTERS
    # The one verifier call this command makes names itself, so the verifier's own
    # ledger can attribute it.
    assert inputs.verifier_calls == ["production-spec"]


def _hashed_outputs(spec_path: Path) -> dict[str, bytes]:
    """Every file the command wrote beside its specification.

    The specification carries a wall-clock `created_at`, which no input determines, so
    it is compared without that one field.
    """

    files = {
        path.relative_to(spec_path.parent).as_posix(): path.read_bytes()
        for path in sorted(spec_path.parent.rglob("*"))
        if path.is_file()
    }
    spec = json.loads(files[spec_path.name])
    del spec["created_at"]
    files[spec_path.name] = canonical_json_bytes(spec)
    return files


def test_production_spec_gate_contract_and_spec_do_not_depend_on_the_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z1 control: cold and warm counters give byte-identical hashed outputs.

    Mutation check: putting the counters into the semantic payload makes the two
    semantic gates, and so the contract and the specification that bind their hash,
    differ.
    """

    inputs = _production_spec_inputs(tmp_path, monkeypatch, COLD_WORK_COUNTERS)
    cold = _build_production_spec(inputs, inputs.state_root)
    cold_outputs = _hashed_outputs(inputs.spec_path)
    assert {"production-post-build-semantic.json", "production-spec.json"} <= set(
        cold_outputs
    )
    assert "portfolio-production-rebind-contract.json" in cold_outputs
    shutil.rmtree(inputs.spec_path.parent)

    _stub_financial_outcomes(monkeypatch, WARM_WORK_COUNTERS)
    warm = _build_production_spec(inputs, inputs.state_root)

    assert cold["work_counters"] == COLD_WORK_COUNTERS
    assert warm["work_counters"] == WARM_WORK_COUNTERS
    assert _hashed_outputs(inputs.spec_path) == cold_outputs


@pytest.mark.parametrize(
    "counters",
    [
        {"model_fit_calls": 0},
        {**COLD_WORK_COUNTERS, "extra_calls": 0},
        {**COLD_WORK_COUNTERS, "model_fit_calls": -1},
        {**COLD_WORK_COUNTERS, "model_fit_calls": True},
    ],
    ids=["missing-keys", "extra-key", "negative", "boolean"],
)
def test_production_spec_refuses_a_proof_without_the_five_work_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counters: dict[str, int]
) -> None:
    """Z1: a verifier proof that lost its counters cannot be emitted as evidence."""

    inputs = _production_spec_inputs(tmp_path, monkeypatch, counters)

    with pytest.raises(materializer.MaterializationError, match="work counters"):
        _build_production_spec(inputs, inputs.state_root)


def test_production_spec_state_root_must_be_the_prepared_retained_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z2: receipts live in the retained cache, so no other state root is accepted."""

    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    other = tmp_path / "per-cycle-production-state"
    other.mkdir()
    assert inputs.verifier_calls == []

    with pytest.raises(
        materializer.MaterializationError,
        match="production state root must be the prepared run's retained cache root",
    ):
        _build_production_spec(inputs, other)

    # The refusal comes before the verifier, so nothing was verified or written.
    assert inputs.verifier_calls == []
    assert not inputs.spec_path.exists()

    # The retained cache root, spelled through a parent hop, is the same root.
    spelled = inputs.state_root / ".." / inputs.state_root.name
    result = _build_production_spec(inputs, spelled)
    assert inputs.verifier_calls == ["production-spec"]
    spec = PipelineSpec.from_json_bytes(Path(result["spec"]).read_bytes())
    assert spec.state_root == inputs.state_root.resolve()


def test_production_spec_state_root_follows_the_overlay_of_a_shared_cache_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z2: with a retained cache base the run's overlay is its retained cache root."""

    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    overlay = tmp_path / "overlay-cache"
    _bind_attended_run(inputs.prepared_root, overlay)

    with pytest.raises(materializer.MaterializationError, match="retained cache root"):
        _build_production_spec(inputs, inputs.state_root)
    assert inputs.verifier_calls == []

    result = _build_production_spec(inputs, overlay)
    spec = PipelineSpec.from_json_bytes(Path(result["spec"]).read_bytes())
    assert spec.state_root == overlay.resolve()


@pytest.mark.parametrize("damage", ["missing", "not-json", "no-identity", "no-root"])
def test_production_spec_refuses_a_prepared_root_without_a_readable_run_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Z2: the retained cache root is read from the receipt, never assumed."""

    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    receipt = materializer._attended_run_receipt(inputs.prepared_root)
    if damage == "missing":
        receipt.unlink()
    elif damage == "not-json":
        receipt.write_bytes(b"not-json")
    elif damage == "no-identity":
        receipt.write_bytes(canonical_json_bytes({"schema_version": 1}))
    else:
        receipt.write_bytes(
            canonical_json_bytes(
                {"schema_version": 1, "request_identity": {"output_root": "x"}}
            )
        )

    with pytest.raises(materializer.MaterializationError, match="attended run receipt"):
        _build_production_spec(inputs, inputs.state_root)
    assert inputs.verifier_calls == []


def _ledger_callers(state_root: Path) -> list[str]:
    """The caller of every entry in the verifier's own per-call ledger."""

    ledger = state_root / "financial-verification-ledger"
    if not ledger.is_dir():
        return []
    return [
        json.loads(entry.read_bytes())["caller"] for entry in sorted(ledger.iterdir())
    ]


def test_production_spec_verifier_call_is_recorded_under_its_own_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real verifier, not the stand-in, records the production-spec caller.

    The fixture holds no real financial artifacts, so the real verifier refuses it; its
    ledger is written on failure too, which is what makes a refused call countable.
    """

    import portfolio_financial_outcomes as outcomes

    real = outcomes.validate_financial_outcomes_with_deferred_ledger
    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        outcomes, "validate_financial_outcomes_with_deferred_ledger", real
    )

    with pytest.raises(outcomes.FinancialOutcomeError):
        _build_production_spec(inputs, inputs.state_root)

    assert _ledger_callers(inputs.state_root) == ["production-spec"]


def test_production_spec_ledger_entry_names_the_emitted_spec_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production-spec ledger entry must name the spec file actually emitted.

    Astra's review (pr519-final.astra.review.md, tools/portfolio_pipeline_materialize.py:5352)
    found the ledger entry recorded against `completed_spec` before the final
    semantic/contract hashes replace `spec["gates"][0:2]`, so the emitted
    production-spec.json never has the identity the ledger names: T0-d P-Z1's
    `E_Z` caller filter (ledger entries with `spec_sha256` in the set of
    production-spec identities) then finds nothing. Reproduce that identity
    check directly: the one ledger entry this call writes must carry the
    sha256 of the bytes `production-spec.json` actually holds, and must still
    carry the real counters from the one real verifier call.
    """

    import portfolio_financial_outcomes as outcomes

    real_deferred = outcomes.validate_financial_outcomes_with_deferred_ledger
    inputs = _production_spec_inputs(tmp_path, monkeypatch, COLD_WORK_COUNTERS)

    # Exercise the real deferred-ledger entry point -- the actual ledger seam
    # `_write_verification_ledger`/`_spec_identity` go through in production --
    # with only the expensive numerical verification itself stubbed, since this
    # fixture carries no real financial artifacts for it to verify.
    monkeypatch.setattr(
        outcomes, "validate_financial_outcomes_with_deferred_ledger", real_deferred
    )

    def fake_validate(
        _spec: object,
        _allow_synthetic: bool,
        work_counters: dict[str, int],
        _calls: list[dict[str, object]],
    ) -> dict[str, object]:
        work_counters.update(COLD_WORK_COUNTERS)
        return {"status": "PASS", "work_counters": dict(COLD_WORK_COUNTERS)}

    monkeypatch.setattr(outcomes, "_validate_financial_outcomes", fake_validate)

    built = _build_production_spec(inputs, inputs.state_root)

    spec_path = Path(built["spec"])
    emitted_spec_sha256 = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    emitted_spec = PipelineSpec.from_json_bytes(spec_path.read_bytes())
    assert emitted_spec.spec_sha256 == emitted_spec_sha256

    ledger_dir = inputs.state_root / "financial-verification-ledger"
    entries = [
        json.loads(path.read_bytes()) for path in sorted(ledger_dir.glob("*.json"))
    ]
    (entry,) = [row for row in entries if row["caller"] == "production-spec"]
    assert entry["spec_sha256"] == emitted_spec_sha256
    assert entry["work_counters"] == COLD_WORK_COUNTERS


def test_semantic_verifier_call_is_recorded_under_its_own_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`semantic --financial-spec` names itself in the verifier's per-call ledger.

    The epoch-rebind fixture's own prepared root carries no panel QA artifacts (it
    exists only to prove epoch/root rebinding), so the semantic reconciliation itself
    is exercised against the ordinary `_fixture` output, which does carry them; the
    financial spec handed to it is the real completed production spec, naming the
    same retained-cache `state_root` the production-spec call above already wrote to.
    """

    import portfolio_financial_outcomes as outcomes

    real = outcomes.validate_financial_outcomes
    inputs = _production_spec_inputs(tmp_path, monkeypatch)
    built = _build_production_spec(inputs, inputs.state_root)
    assert _ledger_callers(inputs.state_root) == []

    recipe, output_root = _fixture(tmp_path / "semantic-check")
    monkeypatch.setattr(outcomes, "validate_financial_outcomes", real)

    with pytest.raises(outcomes.FinancialOutcomeError):
        materializer.semantic(
            recipe,
            output_root / "evidence" / "label-check.json",
            Path(built["spec"]),
        )

    assert _ledger_callers(inputs.state_root) == ["materialize.semantic"]


def test_semantic_accepts_three_distinct_clocks(tmp_path: Path) -> None:
    """The gate must read the evaluation date, not the financial cutoff, as the release date.

    Cutoff, source-generation provenance and evaluation are three different clocks. A real run
    stamps the ledger and baseline manifests with provenance that can outrun the cutoff --
    `provider_ledger._generation_as_of_date` returns `max(cutoff, artifact dates)` and its
    docstring says that date "is provenance ... not a request to advance the financial
    projection or the execution clock" -- while the panel is generated at release time and the
    freshness rows are evaluated then. Comparing any of those to the cutoff rejected every
    honest run, and the previous release passed only by stamping everything with the data date.
    """

    cutoff = AS_OF
    provenance = AS_OF + timedelta(days=1)
    evaluation = AS_OF + timedelta(days=4)
    recipe, output_root = _fixture(tmp_path, evaluated_on=evaluation)

    vertical = json.loads(
        Path(
            json.loads(recipe.read_text("utf-8"))["vertical_recipe"]["path"]
        ).read_text("utf-8")
    )
    assert vertical["as_of_date"] == cutoff.isoformat()
    assert vertical["evaluated_on"] == evaluation.isoformat()

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"
    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text("utf-8")
    )
    # The two predicates that read a clock, named rather than inferred from the verdict.
    assert details["checks"]["generation_invariants"]["status"] == "PASS"
    assert details["checks"]["freshness"]["status"] == "PASS"
    # Provenance outran the cutoff and was allowed, not relabelled onto the cutoff.
    assert provenance > cutoff
    assert details["checks"]["freshness"]["evidence"]["as_of_date"] == (
        cutoff.isoformat()
    )
    assert details["checks"]["freshness"]["evidence"]["evaluated_on"] == (
        evaluation.isoformat()
    )
    panel_row = details["checks"]["generation_invariants"]["evidence"]["generations"][
        "panel"
    ]
    assert panel_row["as_of_rule"] == "EXACT_FINANCIAL_CUTOFF"
    assert panel_row["generation_as_of_date"] == cutoff.isoformat()
    # Both receipts must say which clock decided the verdict. Recording only the cutoff would
    # leave an auditor unable to tell a correct PASS from one produced by the old conflation --
    # and the two dates are equal in exactly the runs where that distinction is cheapest to lose.
    assert details["as_of_date"] == cutoff.isoformat()
    assert details["evaluated_on"] == evaluation.isoformat()
    gate = json.loads(
        (output_root / "evidence" / "post-build-semantic.json").read_text("utf-8")
    )
    assert gate["as_of_date"] == cutoff.isoformat()
    assert gate["evaluated_on"] == evaluation.isoformat()


def test_semantic_refuses_a_panel_manifest_dated_at_the_execution_clock(
    tmp_path: Path,
) -> None:
    """R2-3: a panel manifest dated after the cutoff is read as data newer than it is."""

    evaluation = AS_OF + timedelta(days=4)
    recipe, output_root = _fixture(
        tmp_path, evaluated_on=evaluation, panel_as_of=evaluation
    )

    with pytest.raises(
        materializer.MaterializationError, match="generation_invariants"
    ):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )

    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text("utf-8")
    )
    row = details["checks"]["generation_invariants"]["evidence"]["generations"]["panel"]
    assert row["status"] == "FAIL"
    assert row["generation_as_of_date"] == evaluation.isoformat()
    assert row["as_of_rule"] == "EXACT_FINANCIAL_CUTOFF"


@pytest.mark.parametrize(
    "mutation",
    (
        {"evaluated_on": "2026-01-01"},
        {"as_of_date": "2026-01-01"},
    ),
)
def test_semantic_binds_schema_four_freshness_to_both_clocks(
    tmp_path: Path, mutation: dict[str, str]
) -> None:
    """Schema 4 freshness is dated at the cutoff and names the release's execution date."""

    evaluation = AS_OF + timedelta(days=4)
    recipe, output_root = _fixture(tmp_path, evaluated_on=evaluation)
    _mutate_json(
        output_root / "stages" / "ledger" / "freshness-health.json",
        lambda payload: payload.update(mutation),
    )

    with pytest.raises(materializer.MaterializationError, match="freshness"):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )


def test_semantic_binds_schema_four_assessment_rows_to_the_cutoff(
    tmp_path: Path,
) -> None:
    evaluation = AS_OF + timedelta(days=4)
    recipe, output_root = _fixture(tmp_path, evaluated_on=evaluation)
    _mutate_json(
        output_root / "stages" / "ledger" / "freshness-health.json",
        lambda payload: payload.update(
            {
                "assessments": [
                    {
                        "blocks_current_consumers": False,
                        "evaluated_on": evaluation.isoformat(),
                    }
                ]
            }
        ),
    )

    with pytest.raises(materializer.MaterializationError, match="freshness"):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )


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


def _financial_check(output_root: Path) -> dict[str, object]:
    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text(encoding="utf-8")
    )
    check = details["checks"]["financial_identities"]
    assert isinstance(check, dict)
    return check


def test_semantic_labels_the_snapshot_historical_and_verifies_current_capital(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path)

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"
    check = _financial_check(output_root)
    assert check["status"] == "PASS"
    evidence = check["evidence"]
    assert isinstance(evidence, dict)
    # The dated audit identity keeps its semantics but is labeled historical.
    snapshot = evidence["audit_snapshot"]
    assert snapshot["status"] == "HISTORICAL_REFERENCE"
    assert snapshot["as_of_date"] == "2026-07-25"
    assert snapshot["systematic_total_usd"] == "410272.39"
    assert snapshot["reserve_total_usd"] == "100043.62"
    assert snapshot["total_liquid_usd"] == "510316.01"
    # The current block is recomputed from nav_observations x assignments.
    current = evidence["current_capital"]
    assert current["valuation_date"] == AS_OF.isoformat()
    assert current["recomputed_strategy_count"] == 1
    assert current["strategy_mismatches"] == []
    assert current["systematic_total_usd"] == CURRENT_STRATEGY_NAV
    assert current["systematic_total_usd"] != snapshot["systematic_total_usd"]
    # Reserve and total liquid are not current: reported, never stamped PASS.
    assert current["identity_status"] == "NOT_READY"
    assert current["not_ready_components"] == ["reserve", "total_liquid"]


def test_semantic_current_identity_passes_when_every_component_is_current(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path, reserve_ready=True)

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"
    evidence = _financial_check(output_root)["evidence"]
    assert isinstance(evidence, dict)
    current = evidence["current_capital"]
    assert current["identity_status"] == "PASS"
    assert current["not_ready_components"] == []
    assert current["total_liquid_usd"] == "412190.05"


def _corrupt_strategy_value(payload: dict[str, object]) -> None:
    capital = payload["current_capital"]
    assert isinstance(capital, dict)
    row = capital["strategies"][0]
    row["current_value"]["value"] = "400001.5"
    capital["systematic"]["current_value"]["value"] = "400001.5"


def _drift_a_nav_fact(payload: dict[str, object]) -> None:
    # The published block is untouched; the NAV fact it claims to sum moved.
    navs = payload["nav_observations"]
    assert isinstance(navs, list)
    navs[0]["nav"]["value"] = "1"


def _paint_the_audit_reserve(payload: dict[str, object]) -> None:
    capital = payload["current_capital"]
    assert isinstance(capital, dict)
    capital["reserve"]["status"] = "AVAILABLE"
    capital["reserve"]["current_value"] = _cc_amount("100043.62")
    capital["reserve"]["as_of_date"] = "2026-07-25"


def _paint_total_liquid(payload: dict[str, object]) -> None:
    capital = payload["current_capital"]
    assert isinstance(capital, dict)
    capital["total_liquid"]["status"] = "AVAILABLE"
    capital["total_liquid"]["current_value"] = _cc_amount("510316.01")
    capital["total_liquid"]["as_of_date"] = "2026-07-25"


def _relabel_the_snapshot_current(payload: dict[str, object]) -> None:
    snapshot = payload["audit_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["status"] = "CURRENT"


def _stale_valuation_date(payload: dict[str, object]) -> None:
    capital = payload["current_capital"]
    assert isinstance(capital, dict)
    capital["valuation_date"] = "2026-07-25"


def _wrong_weight(payload: dict[str, object]) -> None:
    capital = payload["current_capital"]
    assert isinstance(capital, dict)
    capital["strategies"][0]["current_weight"] = "0.5"


def _snapshot_replay_drift(payload: dict[str, object]) -> None:
    snapshot = payload["audit_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["replay"] = {**snapshot["replay"], "reserve_total_usd": "1"}


def _registry_names_a_strategy_the_block_lacks(payload: dict[str, object]) -> None:
    # A required strategy is missing from the published rows while the baseline's
    # own registry still names it.
    registry = payload["strategies"]
    assert isinstance(registry, list)
    registry.append({"strategy_id": "strategy-2"})


@pytest.mark.parametrize(
    "mutate",
    [
        _corrupt_strategy_value,
        _drift_a_nav_fact,
        _paint_the_audit_reserve,
        _paint_total_liquid,
        _relabel_the_snapshot_current,
        _stale_valuation_date,
        _wrong_weight,
        _snapshot_replay_drift,
        _registry_names_a_strategy_the_block_lacks,
    ],
)
def test_semantic_financial_identities_fail_on_a_false_current_capital(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    """Paired with the good run above: each lie about current capital is caught."""
    recipe, output_root = _fixture(tmp_path)
    baseline = next((output_root / "stages" / "baseline" / "generations").iterdir())
    _mutate_json(baseline / "portfolio-baseline.json", mutate)

    with pytest.raises(materializer.MaterializationError, match="financial_identities"):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )

    check = _financial_check(output_root)
    assert check["status"] == "FAIL"
    assert not (output_root / "evidence" / "post-build-semantic.json").exists()


def test_semantic_requires_the_current_capital_and_audit_snapshot_blocks(
    tmp_path: Path,
) -> None:
    for missing in ("current_capital", "audit_snapshot"):
        recipe, output_root = _fixture(tmp_path / missing)
        baseline = next((output_root / "stages" / "baseline" / "generations").iterdir())

        def drop(payload: dict[str, object], key: str = missing) -> None:
            payload.pop(key)

        _mutate_json(baseline / "portfolio-baseline.json", drop)

        with pytest.raises(
            materializer.MaterializationError, match="baseline current capital differs"
        ):
            materializer.semantic(
                recipe, output_root / "evidence" / "post-build-semantic.json"
            )


def test_semantic_reports_a_registered_strategy_the_block_lacks(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path)
    baseline = next((output_root / "stages" / "baseline" / "generations").iterdir())
    _mutate_json(
        baseline / "portfolio-baseline.json", _registry_names_a_strategy_the_block_lacks
    )

    with pytest.raises(materializer.MaterializationError, match="financial_identities"):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )

    evidence = _financial_check(output_root)["evidence"]
    assert isinstance(evidence, dict)
    current = evidence["current_capital"]
    assert current["identity_status"] == "FAIL"
    assert current["problems"] == ["missing strategy rows: strategy-2"]


def test_semantic_requires_the_strategy_registry_the_rows_are_checked_against(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path)
    baseline = next((output_root / "stages" / "baseline" / "generations").iterdir())

    def drop_the_registry(payload: dict[str, object]) -> None:
        payload.pop("strategies")

    _mutate_json(baseline / "portfolio-baseline.json", drop_the_registry)

    with pytest.raises(
        materializer.MaterializationError, match="baseline current capital differs"
    ):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )


# The strategy universe of the current capital block is the baseline's own
# registry (`strategies`), not whatever rows the block publishes.  The baseline
# below is hand-written, figure by figure, so no expectation comes from the
# baseline builder.

_GATE_DAY = "2026-09-17"


def _gate_row(
    strategy_id: str, account_id: str, value: str, weight: str
) -> dict[str, Any]:
    """One published strategy row with a single member account."""

    return {
        "as_of_date": _GATE_DAY,
        "current_value": _cc_amount(value),
        "current_weight": weight,
        "members": [
            {
                "account_id": account_id,
                "as_of_date": _GATE_DAY,
                "assignment_ids": [f"assignment-{account_id}"],
                "effective_end": None,
                "effective_start": "2026-01-01",
                "nav_record_id": f"nav-{account_id}",
                "source_key": "provider-ledger",
                "status": "AVAILABLE",
                "value": _cc_amount(value),
            }
        ],
        "reason": None,
        "scope": "SYSTEMATIC",
        "status": "AVAILABLE",
        "strategy_id": strategy_id,
    }


def _three_strategy_baseline() -> dict[str, Any]:
    """strategy-a, -b and -c own one account each, valued 200, 200 and 600.

    The systematic total is 1000 and the weights are 0.2, 0.2 and 0.6.  No
    reserve account is admitted, so reserve and total liquid are honestly
    NOT_READY.
    """

    book = [
        ("strategy-a", "account-a", "200", "0.2"),
        ("strategy-b", "account-b", "200", "0.2"),
        ("strategy-c", "account-c", "600", "0.6"),
    ]
    not_ready = {
        "as_of_date": None,
        "components": [],
        "current_value": _cc_amount(None),
        "reason": "not every component is current",
        "status": "NOT_READY_AT_CUTOFF",
    }
    return {
        "as_of_date": _GATE_DAY,
        "assignments": [
            {
                "account_id": account_id,
                "assignment_id": f"assignment-{account_id}",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "pool_id": "systematic-capital",
                "strategy_id": strategy_id,
                "strategy_mapping_disposition": "MAPPED",
            }
            for strategy_id, account_id, _, _ in book
        ],
        "current_capital": {
            "basis": "LATEST_ADMITTED_USD_NAV_ON_OR_BEFORE_VALUATION_DATE",
            "currency": "USD",
            "reserve": dict(not_ready),
            "retained_excluded": {
                "as_of_date": None,
                "components": [],
                "current_value": _cc_amount("0"),
                "reason": None,
                "status": "AVAILABLE",
            },
            "strategies": [_gate_row(*entry) for entry in book],
            "systematic": {
                "as_of_date": _GATE_DAY,
                "components": [entry[0] for entry in book],
                "current_value": _cc_amount("1000"),
                "reason": None,
                "status": "AVAILABLE",
            },
            "total_liquid": dict(not_ready),
            "valuation_date": _GATE_DAY,
            "weight_basis": "CURRENT_CUTOFF",
        },
        "nav_observations": [
            {
                "account_id": account_id,
                "as_of_date": _GATE_DAY,
                "nav": _cc_amount(value),
                "record_id": f"nav-{account_id}",
                "source_key": "provider-ledger",
            }
            for _, account_id, value, _ in book
        ],
        "provider_admissions": [],
        "strategies": [{"strategy_id": entry[0]} for entry in book],
    }


def _remove_strategy_b_with_its_assignments(
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Corrupt the block the way the review did, leaving the registry alone.

    strategy-b's row and every assignment of it go; the systematic total drops
    from 1000 to 800 and the two remaining weights are recomputed (0.25, 0.75),
    so the block reconciles with itself and with the remaining facts.
    """

    corrupted = copy.deepcopy(baseline)
    capital = corrupted["current_capital"]
    capital["strategies"] = [
        row for row in capital["strategies"] if row["strategy_id"] != "strategy-b"
    ]
    weights = {"strategy-a": "0.25", "strategy-c": "0.75"}
    for row in capital["strategies"]:
        row["current_weight"] = weights[row["strategy_id"]]
    capital["systematic"]["components"] = ["strategy-a", "strategy-c"]
    capital["systematic"]["current_value"] = _cc_amount("800")
    corrupted["assignments"] = [
        item for item in corrupted["assignments"] if item["strategy_id"] != "strategy-b"
    ]
    return corrupted


def test_capital_gate_accepts_the_hand_written_three_strategy_baseline() -> None:
    accepted, evidence = materializer._current_capital_identity(
        _three_strategy_baseline()
    )

    assert accepted is True
    assert evidence["problems"] == []
    assert evidence["strategy_mismatches"] == []
    assert evidence["component_mismatches"] == []
    assert evidence["recomputed_strategy_count"] == 3
    assert evidence["systematic_total_usd"] == "1000"
    # Reserve and total liquid are not current: reported, never stamped PASS.
    assert evidence["identity_status"] == "NOT_READY"
    assert evidence["not_ready_components"] == ["reserve", "total_liquid"]


def test_capital_gate_rejects_a_strategy_removed_with_its_assignments() -> None:
    """The review's mutation: a corrupted baseline drops one required strategy,
    its row and all its assignments, lowers the systematic total and recomputes
    the remaining weights.  The registry still names the strategy, so the gate
    must fail closed rather than reconcile the smaller universe."""

    accepted, evidence = materializer._current_capital_identity(
        _remove_strategy_b_with_its_assignments(_three_strategy_baseline())
    )

    assert accepted is False
    assert evidence["identity_status"] == "FAIL"
    assert evidence["problems"] == ["missing strategy rows: strategy-b"]
    # Nothing is recomputed over the wrong universe, so no total is reported.
    assert evidence["recomputed_strategy_count"] == 0
    assert evidence["systematic_total_usd"] is None
    # The refusal reports the same evidence shape as an accepted block.
    good = materializer._current_capital_identity(_three_strategy_baseline())[1]
    assert set(evidence) == set(good)


def test_capital_gate_follows_the_registry_not_a_fixed_strategy_count() -> None:
    """Paired with the rejection above: the identical corrupted block is accepted
    when the registry itself holds the smaller universe, so the registry equality
    is the only thing that separated the two runs, and no count is assumed."""

    baseline = _remove_strategy_b_with_its_assignments(_three_strategy_baseline())
    baseline["strategies"] = [
        {"strategy_id": "strategy-a"},
        {"strategy_id": "strategy-c"},
    ]

    accepted, evidence = materializer._current_capital_identity(baseline)

    assert accepted is True
    assert evidence["problems"] == []
    assert evidence["recomputed_strategy_count"] == 2
    assert evidence["systematic_total_usd"] == "800"


@pytest.mark.parametrize(
    ("registry_ids", "problems"),
    [
        (
            ["strategy-a", "strategy-b"],
            ["unregistered strategy rows: strategy-c"],
        ),
        (
            [],
            ["unregistered strategy rows: strategy-a, strategy-b, strategy-c"],
        ),
        (
            # The same number of ids is not the same ids.
            ["strategy-a", "strategy-b", "strategy-x"],
            [
                "missing strategy rows: strategy-x",
                "unregistered strategy rows: strategy-c",
            ],
        ),
    ],
    ids=["extra-row", "empty-registry", "same-count-different-ids"],
)
def test_capital_gate_rejects_published_rows_the_registry_does_not_name(
    registry_ids: list[str], problems: list[str]
) -> None:
    baseline = _three_strategy_baseline()
    baseline["strategies"] = [{"strategy_id": item} for item in registry_ids]

    accepted, evidence = materializer._current_capital_identity(baseline)

    assert accepted is False
    assert evidence["identity_status"] == "FAIL"
    assert evidence["problems"] == problems
    assert evidence["recomputed_strategy_count"] == 0


@pytest.mark.parametrize(
    ("registry", "message"),
    [
        (None, "baseline current capital differs"),
        ("strategy-a", "baseline current capital differs"),
        (["strategy-a"], "baseline current capital differs"),
        ([{"name": "no identity"}], "registry strategy_id must be non-empty text"),
    ],
    ids=["absent", "not-a-list", "rows-not-objects", "row-without-an-id"],
)
def test_capital_gate_fails_closed_on_an_unreadable_registry(
    registry: object, message: str
) -> None:
    baseline = _three_strategy_baseline()
    if registry is None:
        del baseline["strategies"]
    else:
        baseline["strategies"] = registry

    with pytest.raises(materializer.MaterializationError, match=message):
        materializer._current_capital_identity(baseline)


# The 2026-09-17 cutoff book of baseline 347ed499 (the release tail's prepared
# generation): seven SYSTEMATIC strategies, their member NAVs, and the OWNER_318
# derived custody cash lines the producer adds to robuxio-crypto and
# volatis-strategy (portfolio_baseline._build_current_capital).  The published
# strategy value is NAV + cash; the systematic total is 426101.61285957407210153065004
# and the NAV-only sum is exactly the cash total (1973.918694572) lower.
_CUSTODY_BOOK: tuple[tuple[str, tuple[tuple[str, str], ...], str | None], ...] = (
    ("grizzlybears-vtma", (("account-61092059d066f056", "51233.9"),), None),
    ("p123-ai-factor", (("account-1b44c987969b9e47", "57200.875514128"),), None),
    ("p123-eu", (("account-d69701da9143c430", "45464.686880028"),), None),
    (
        "qsi",
        (
            ("account-38b653b91ff43a10", "48309.42"),
            ("account-98f5986706ed53c5", "10628.97"),
        ),
        None,
    ),
    (
        "robuxio-crypto",
        (("account-d2e5821e608e25f1", "51126.09177084639756872065004"),),
        "93.97474435567453281",
    ),
    (
        "tt-portfolio",
        (
            ("account-702c489650e59956", "3.81"),
            ("account-a13937e26b8ae46d", "14520.86"),
            ("account-c63d2a487e019790", "74492.29"),
            ("account-cff10c44aedcf5e8", "12054.51"),
        ),
        None,
    ),
    (
        "volatis-strategy",
        (("account-58e75e8d952fa1b8", "59092.28"),),
        "1879.943950216",
    ),
)
# The weights baseline 347ed499 publishes, verbatim.
_CUSTODY_PUBLISHED_WEIGHTS = {
    "grizzlybears-vtma": "0.1202386906169365514680187219538178",
    "p123-ai-factor": "0.1342423351328151495973869009638075",
    "p123-eu": "0.1066991663676507355609811033524524",
    "qsi": "0.1383200349899255585789323077457636",
    "robuxio-crypto": "0.1202062253917873407377712384742153",
    "tt-portfolio": "0.2372003929337603452714571839230204",
    "volatis-strategy": "0.143093154567124318785452543586923",
}
_CUSTODY_SYSTEMATIC = "426101.61285957407210153065004"
_CUSTODY_NAV_ONLY_SYSTEMATIC = "424127.69416500239756872065004"


def _custody_cash_line(account_id: str, value: str) -> dict[str, Any]:
    """One OWNER_318 derived cash line, shaped as the producer emits it."""

    return {
        "account_id": account_id,
        "as_of_date": _GATE_DAY,
        "basis": "DERIVED_ADMITTED_EXTERNAL_FLOWS_MINUS_SLEEVE_ALLOCATIONS_PLUS_REDEMPTIONS",
        "coverage_end": "2026-09-18",
        "current_value": _cc_amount(value),
        "events": [
            {
                "event_date": "2025-12-05",
                "event_sha256": "a" * 64,
                "event_time_fx": None,
                "native_currency": "USD",
                "native_value": value,
                "record_id": f"flow-{account_id}",
                "signed_usd": value,
            }
        ],
        "last_event_date": "2025-12-05",
        "provider_id": "custody",
        "status": "AVAILABLE",
    }


def _custody_cash_baseline(*, with_cash: bool = True) -> dict[str, Any]:
    """The 347ed499 cutoff book as a gate input.

    ``with_cash`` publishes it as the producer does since 91a88b5: each strategy's
    value is its member NAVs plus its ``derived_cash`` lines, under the selected
    ``custody_authority``.  Without it, the same NAVs are published NAV-only (the
    pre-OWNER_318 profile) with weights recomputed over the NAV-only total.
    """

    from quant_engine.portfolio_ledger.models import decimal_text

    def exact(values: list[Decimal]) -> Decimal:
        with localcontext() as context:
            context.prec = 100
            return sum(values, Decimal(0))

    values = {
        strategy_id: exact(
            [Decimal(nav) for _, nav in members]
            + ([Decimal(cash)] if cash is not None and with_cash else [])
        )
        for strategy_id, members, cash in _CUSTODY_BOOK
    }
    total = exact(list(values.values()))
    rows: list[dict[str, Any]] = []
    for strategy_id, members, cash in _CUSTODY_BOOK:
        if with_cash:
            weight = _CUSTODY_PUBLISHED_WEIGHTS[strategy_id]
        else:
            with localcontext() as context:
                context.prec = 34
                weight = decimal_text(values[strategy_id] / total)
        row: dict[str, Any] = {
            "as_of_date": _GATE_DAY,
            "current_value": _cc_amount(decimal_text(values[strategy_id])),
            "current_weight": weight,
            "members": [
                {
                    "account_id": account_id,
                    "as_of_date": _GATE_DAY,
                    "assignment_ids": [f"assignment-{account_id}"],
                    "effective_end": None,
                    "effective_start": "2026-01-01",
                    "nav_record_id": f"nav-{account_id}",
                    "source_key": "provider-ledger",
                    "status": "AVAILABLE",
                    "value": _cc_amount(nav),
                }
                for account_id, nav in members
            ],
            "reason": None,
            "scope": "SYSTEMATIC",
            "status": "AVAILABLE",
            "strategy_id": strategy_id,
        }
        if cash is not None and with_cash:
            row["derived_cash"] = [_custody_cash_line(members[0][0], cash)]
        rows.append(row)
    not_ready = {
        "as_of_date": None,
        "components": [],
        "current_value": _cc_amount(None),
        "reason": "not every component is current",
        "status": "NOT_READY_AT_CUTOFF",
    }
    capital: dict[str, Any] = {
        "basis": "LATEST_ADMITTED_USD_NAV_ON_OR_BEFORE_VALUATION_DATE",
        "currency": "USD",
        "reserve": dict(not_ready),
        "retained_excluded": {
            "as_of_date": None,
            "components": [],
            "current_value": _cc_amount("0"),
            "reason": None,
            "status": "AVAILABLE",
        },
        "strategies": rows,
        "systematic": {
            "as_of_date": _GATE_DAY,
            "components": [entry[0] for entry in _CUSTODY_BOOK],
            "current_value": _cc_amount(decimal_text(total)),
            "reason": None,
            "status": "AVAILABLE",
        },
        "total_liquid": dict(not_ready),
        "valuation_date": _GATE_DAY,
        "weight_basis": "CURRENT_CUTOFF",
    }
    if with_cash:
        capital["custody_authority"] = {"census_id": "PORTFOLIO_CURRENT_CENSUS_V1"}
    return {
        "as_of_date": _GATE_DAY,
        "assignments": [
            {
                "account_id": account_id,
                "assignment_id": f"assignment-{account_id}",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "pool_id": "systematic-capital",
                "strategy_id": strategy_id,
                "strategy_mapping_disposition": "MAPPED",
            }
            for strategy_id, members, _ in _CUSTODY_BOOK
            for account_id, _ in members
        ],
        "current_capital": capital,
        "nav_observations": [
            {
                "account_id": account_id,
                "as_of_date": _GATE_DAY,
                "nav": _cc_amount(nav),
                "record_id": f"nav-{account_id}",
                "source_key": "provider-ledger",
            }
            for _, members, _ in _CUSTODY_BOOK
            for account_id, nav in members
        ],
        "provider_admissions": [],
        "strategies": [{"strategy_id": entry[0]} for entry in _CUSTODY_BOOK],
    }


def test_capital_gate_adds_the_owner_318_derived_custody_cash_lines() -> None:
    """The release tail's refusal, on its own numbers: the producer publishes each
    strategy at NAV + derived custody cash (OWNER_318), so the gate's recompute
    must add the same cash lines.  Unmodified, it recomputed NAV only and refused
    426101.61285957407210153065004 against 424127.69416500239756872065004."""

    baseline = _custody_cash_baseline()
    assert (
        baseline["current_capital"]["systematic"]["current_value"]["value"]
        == _CUSTODY_SYSTEMATIC
    )

    accepted, evidence = materializer._current_capital_identity(baseline)

    assert evidence["problems"] == []
    assert evidence["strategy_mismatches"] == []
    assert evidence["component_mismatches"] == []
    assert evidence["systematic_total_usd"] == _CUSTODY_SYSTEMATIC
    assert evidence["derived_cash_usd"] == {
        "robuxio-crypto": "93.97474435567453281",
        "volatis-strategy": "1879.943950216",
    }
    assert accepted is True
    assert evidence["identity_status"] == "NOT_READY"
    assert evidence["not_ready_components"] == ["reserve", "total_liquid"]


def test_capital_gate_still_accepts_a_nav_only_baseline_without_cash_lines() -> None:
    """The pre-OWNER_318 profile is unchanged: no cash lines, NAV-only values."""

    accepted, evidence = materializer._current_capital_identity(
        _custody_cash_baseline(with_cash=False)
    )

    assert accepted is True
    assert evidence["problems"] == []
    assert evidence["strategy_mismatches"] == []
    assert evidence["component_mismatches"] == []
    assert evidence["systematic_total_usd"] == _CUSTODY_NAV_ONLY_SYSTEMATIC
    assert evidence["derived_cash_usd"] == {}


def _set_volatis_cash(baseline: dict[str, Any], value: str) -> dict[str, Any]:
    (line,) = next(
        row
        for row in baseline["current_capital"]["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )["derived_cash"]
    line["current_value"]["value"] = value
    return line


def test_capital_gate_refuses_a_cash_line_the_published_value_does_not_carry() -> None:
    """A cash line one unit in the last place off the value it was added into."""

    baseline = _custody_cash_baseline()
    _set_volatis_cash(baseline, "1879.943950217")

    accepted, evidence = materializer._current_capital_identity(baseline)

    assert accepted is False
    assert evidence["identity_status"] == "FAIL"
    assert evidence["strategy_mismatches"] == ["volatis-strategy"]


def test_capital_gate_refuses_a_cash_line_its_own_events_do_not_sum_to() -> None:
    """A consistently tampered book: the cash line, the strategy value, the
    systematic total and every weight all move together, so only the line's own
    signed events can expose it."""

    honest = _custody_cash_baseline()
    tampered = copy.deepcopy(honest)
    line = _set_volatis_cash(tampered, "1979.943950216")
    assert line["events"][0]["signed_usd"] == "1879.943950216"
    capital = tampered["current_capital"]
    for row in capital["strategies"]:
        if row["strategy_id"] == "volatis-strategy":
            row["current_value"]["value"] = "61072.223950216"
    capital["systematic"]["current_value"]["value"] = "426201.61285957407210153065004"
    with localcontext() as context:
        context.prec = 34
        for row in capital["strategies"]:
            row["current_weight"] = str(
                Decimal(row["current_value"]["value"])
                / Decimal("426201.61285957407210153065004")
            )

    accepted, evidence = materializer._current_capital_identity(tampered)

    assert accepted is False
    assert evidence["identity_status"] == "FAIL"
    assert evidence["problems"] == [
        "derived cash for volatis-strategy differs from its events"
    ]


def test_capital_gate_refuses_cash_lines_without_the_selected_custody_authority() -> (
    None
):
    """The producer's own validator refuses cash with no selected authority."""

    baseline = _custody_cash_baseline()
    del baseline["current_capital"]["custody_authority"]

    accepted, evidence = materializer._current_capital_identity(baseline)

    assert accepted is False
    assert evidence["problems"] == ["derived cash lacks the selected custody authority"]


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
    retained_cache_root = tmp_path / "retained-decomposition"
    materializer.build_spec(
        recipe_path,
        output_root,
        spec_path,
        portfolio_root=tmp_path / "portfolio",
        retained_cache_root=retained_cache_root,
    )

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
    analytics_command = nodes["build-analytics"]
    retained_index = analytics_command["argv"].index("--retained-cache-root")
    assert analytics_command["argv"][retained_index + 1] == str(retained_cache_root)
    assert str(retained_cache_root) in analytics_command["declared_writes"]
    assert spec["state_root"] == str(retained_cache_root)
    assert all(
        node["retry_limit"] == 0
        for node_id, node in nodes.items()
        if node.get("kind") == "COMMAND"
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
    for role in ("report",):
        assert (
            str(output_root / "stages" / "baseline")
            in nodes[f"build-{role}"]["declared_reads"]
        )
    assert (
        str(output_root / "stages" / "report")
        in nodes["build-report"]["declared_writes"]
    )
    assert str(output_root / "stages" / "report") in set(
        nodes["build-report"]["declared_writes"]
    )
    report_generation_paths = {
        row["relative_path"] for row in nodes["report-generation"]["files"]
    }
    assert "portfolio-report.xlsx" not in report_generation_paths
    assert not any(path.startswith("deliveries/") for path in report_generation_paths)
    assert (
        str(output_root / "stages" / "report") in nodes["build-panel"]["declared_reads"]
    )
    assert (
        str(output_root / "stage-results" / "report.json")
        in nodes["build-panel"]["declared_reads"]
    )
    assert "build-workbook" not in nodes


def test_governed_hwm_assessment_binds_the_admitted_input_contract() -> None:
    """The HWM kernel is bound to the contract the ledger was admitted under.

    A contract can never pin a ledger built from itself, so the admitted input
    contract and the product contract legitimately differ.  The admission receipt
    names the former; `portfolio_report` and `portfolio_control_panel` already
    reconcile this by retargeting the current policy onto that input.  This pins
    the same reconciliation in the materialiser, which previously passed the
    product contract and the raw policy and so could never verify a governed
    result built against the admitted input.
    """
    import inspect

    source = inspect.getsource(materializer._hwm_input_authority_assessment)
    assert "governed_hwm_input_policy(" in source
    assert "product_contract_bytes=input_contract_bytes" in source
    assert "policy_bytes=governed_policy_bytes" in source

    signature = inspect.signature(materializer._hwm_input_authority_assessment)
    assert "input_contract_path" in signature.parameters
    # Absent a separate input contract the behaviour is unchanged: the product
    # contract is still what gets verified.
    assert signature.parameters["input_contract_path"].default is None
    for entry in (materializer.build_spec, materializer.build_production_spec):
        assert "financial_input_contract_path" in inspect.signature(entry).parameters


def test_panel_lens_authority_admits_the_dynamic_style_path(tmp_path: Path) -> None:
    """The dynamic style path is a lens, not a surface split, so it has no surfaces.

    It is bound through `meta.decomposition_generation_id`, which the caller
    verifies separately; requiring a `surfaces` mapping from it made the gate
    unsatisfiable for any panel that publishes it.
    """

    if sys.platform == "win32":
        tmp_path = Path("\\\\?\\" + str(tmp_path.resolve()))
    contract, panel = _current_lens_release_fixture(tmp_path)
    nodes = materializer._production_lens_nodes(tmp_path, contract)
    panel["decomposition_lenses"]["modes"]["LIVE_ONLY"].append(
        {"lens_id": "DYNAMIC_STYLE", "generation_id": DECOMPOSITION_AUTHORITY}
    )

    materializer._verify_panel_lens_nodes(panel, nodes, DECOMPOSITION_AUTHORITY)

    # The skip is not a licence to carry any unbound lens: a group with no
    # surfaces that is not the decomposition authority is still a mismatch.
    panel["decomposition_lenses"]["modes"]["LIVE_ONLY"][-1]["generation_id"] = "0" * 64
    with pytest.raises(materializer.MaterializationError, match="panel lens"):
        materializer._verify_panel_lens_nodes(panel, nodes, DECOMPOSITION_AUTHORITY)


def _attended_preflight_fixture(tmp_path: Path) -> dict[str, object]:
    """A child-bound run fixture with no financial inputs or publication seam."""

    repo = tmp_path / "repo"
    builder = repo / "tools" / "portfolio_release_builder.py"
    builder.parent.mkdir(parents=True)
    builder.write_text("# child boundary\n", encoding="utf-8")
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"python")
    descriptor = tmp_path / "workbook-runtime.json"
    descriptor.write_text("{}", encoding="utf-8")
    recipe = tmp_path / "recipe.json"
    recipe.write_text("{}", encoding="utf-8")
    source_run = tmp_path / "source-run.json"
    source_run.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "dataPortfolio").mkdir(parents=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    qa_dir = tmp_path / "qa"
    qa_dir.mkdir()
    capital_result = tmp_path / "capital-result.json"
    capital_generation = tmp_path / "capital-generation.json"
    capital_result.write_text("{}", encoding="utf-8")
    capital_generation.write_text("{}", encoding="utf-8")
    return {
        "repo_root": repo,
        "python": runtime,
        "workspace_root": workspace,
        "portfolio_root": workspace / "dataPortfolio",
        "retained_cache_root": cache,
        "requested_as_of": "2026-09-19",
        "source_run": source_run,
        "runtime": {"descriptor": descriptor, "identity": {"runtime": "test"}},
        "recipe": recipe,
        "financial_inputs": {},
        "financial_stage_inputs": {
            "--invested-capital-result": capital_result,
            "--invested-capital-input-generation": capital_generation,
        },
        "qa_dir": qa_dir,
        "request_identity": {"fixture": "attended-child-boundary"},
        "required_stages": materializer.ATTENDED_STAGES,
        "source_phase": {"status": "PASS"},
    }


def _record_attended_stage(output_root: Path, role: str) -> None:
    stage_root = (
        output_root
        / "stages"
        / (
            "report"
            if role == "report"
            else "report"
            if role == "workbook"
            else "panel"
            if role in {"panel-candidate", "panel"}
            else role
        )
    )
    stage_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"role": role}
    if role == "ledger":
        product = financial_fixture.build_financial_mvp_fixture(
            output_root / "attended-ledger-source"
        )
        generation = stage_root / "generations" / product.generation.name
        shutil.copytree(product.generation, generation)
        result.update(
            {
                "catalog_sha256": hashlib.sha256(
                    (generation / "provider-ledger.json").read_bytes()
                ).hexdigest(),
                "generation_id": product.generation.name,
                "root": str(stage_root),
            }
        )
    elif role == "baseline":
        catalog = canonical_json_bytes(
            {"baseline_schema_version": 1, "policy_version": "PORT-PERF-1"}
        )
        artifact = SourceArtifact(
            artifact_id="normalized/portfolio-baseline.json",
            sha256=hashlib.sha256(catalog).hexdigest(),
            size_bytes=len(catalog),
            as_of_date=AS_OF,
            schema_name="portfolio-baseline-catalog",
            schema_version="1",
        )
        manifest = GenerationManifest.build(
            as_of_date=AS_OF,
            adapter=AdapterIdentity("attended-baseline", "1"),
            coverage=CoverageClaim(
                status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
                boundary="attended baseline test artifact",
                fact_types=("baseline",),
                observed_start=AS_OF,
                observed_end=AS_OF,
            ),
            artifacts=(artifact,),
            verification=VerificationStatus.VERIFIED,
        )
        generation = stage_root / "generations" / manifest.generation_id
        generation.mkdir(parents=True)
        (generation / "generation.json").write_bytes(manifest.to_json_bytes())
        (generation / "portfolio-baseline.json").write_bytes(catalog)
        result.update(
            {
                "catalog_sha256": artifact.sha256,
                "generation_id": manifest.generation_id,
                "root": str(stage_root),
            }
        )
    _write_json(output_root / "stage-results" / f"{role}.json", result)


def _attended_argv(
    preflight: dict[str, object],
    output_root: Path,
    *,
    execute: bool,
    resume: bool = False,
) -> list[str]:
    argv = [
        "attended",
        "--repo-root",
        str(preflight["repo_root"]),
        "--python",
        str(preflight["python"]),
        "--workspace-root",
        str(preflight["workspace_root"]),
        "--output-root",
        str(output_root),
        "--retained-cache-root",
        str(preflight["retained_cache_root"]),
        "--requested-as-of",
        str(preflight["requested_as_of"]),
        "--source-run",
        str(preflight["source_run"]),
    ]
    if execute:
        # These runs stage no QA evidence; the hold is the declared plan for that.
        argv.extend(["--qa-hold", "--execute"])
    if resume:
        argv.append("--resume")
    return argv


def test_attended_view_separates_plan_from_verified_completed_artifacts(
    tmp_path: Path,
) -> None:
    """Dropping a result receipt must make a completed stage disappear from the view."""

    output_root = tmp_path / "output"
    _record_attended_stage(output_root, "ledger")

    view = materializer._attended_view(
        output_root, materializer.ATTENDED_STAGES, {"source": "test"}
    )

    assert view["planned_stages"] == list(materializer.ATTENDED_STAGES)
    assert view["verified_completed_stages"] == ["ledger"]
    assert view["post_proof_pipeline_spec"] is None


def test_attended_completed_ledger_requires_a_bound_verified_generation(
    tmp_path: Path,
) -> None:
    """A receipt alone is not a completed ledger artifact for resume purposes."""

    output_root = tmp_path / "output"
    ledger_root = output_root / "stages" / "ledger"
    ledger_root.mkdir(parents=True)
    _write_json(
        output_root / "stage-results" / "ledger.json",
        {
            "catalog_sha256": "0" * 64,
            "generation_id": "0" * 64,
            "root": str(ledger_root),
        },
    )

    with pytest.raises(
        materializer.MaterializationError,
        match="attended completed ledger",
    ):
        materializer._attended_completed_stages(
            output_root, materializer.ATTENDED_STAGES
        )


def test_attended_execute_never_starts_a_coordinated_publication_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a stage child with publication must fail this boundary assertion."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"
    calls: list[list[str]] = []

    def stage_only(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        role = argv[argv.index("--role") + 1]
        _record_attended_stage(output_root, role)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", stage_only)

    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 0
    assert len(calls) == len(preflight["required_stages"])
    assert all(command[2] == "stage" for command in calls)
    assert all("publish" not in command for command in calls)


def test_attended_resume_skips_only_verified_completed_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a stage directory must turn a would-be resume skip into rejection."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", complete)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 0

    def no_child(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a verified completed stage must be skipped")

    monkeypatch.setattr(materializer.subprocess, "run", no_child)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 0
    )

    shutil.rmtree(output_root / "stages" / "ledger")
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 1
    )


def test_attended_resume_of_a_completed_panel_needs_no_qa_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose panel completed under --qa-dir resumes without repeating it.

    Only the ledger and baseline stages have a verifier a fixture can satisfy, so the
    completion probe is reduced to "the stage receipt exists" for the later stages.
    """

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    qa_dir = preflight["qa_dir"]
    (qa_dir / "evidence.json").write_bytes(b"{}")
    output_root = tmp_path / "output"

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", complete)
    first = [
        *_attended_argv(preflight, output_root, execute=True),
        "--qa-dir",
        str(qa_dir),
    ]
    assert materializer.main(first) == 0
    for role in materializer.ATTENDED_STAGES:
        if not (output_root / "stage-results" / f"{role}.json").exists():
            _write_json(output_root / "stage-results" / f"{role}.json", {"role": role})
    monkeypatch.setattr(
        materializer,
        "_attended_completed_stages",
        lambda root, stages, **_kw: [
            role
            for role in stages
            if (root / "stage-results" / f"{role}.json").exists()
        ],
    )

    def no_child(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a completed run must not start a child")

    monkeypatch.setattr(materializer.subprocess, "run", no_child)
    resumed = {
        **preflight,
        "qa_dir": None,
        "required_stages": materializer.ATTENDED_STAGES,
    }
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: resumed)
    argv = [
        a
        for a in _attended_argv(preflight, output_root, execute=True, resume=True)
        if a != "--qa-hold"
    ]
    assert materializer.main(argv) == 0

    # A different QA directory on resume is still refused.
    other = tmp_path / "other-qa"
    other.mkdir()
    (other / "evidence.json").write_bytes(b"{}")
    monkeypatch.setattr(
        materializer,
        "_attended_preflight",
        lambda _args: {
            **preflight,
            "qa_dir": other,
            "required_stages": materializer.ATTENDED_STAGES,
        },
    )
    assert materializer.main([*argv, "--qa-dir", str(other)]) == 1


def test_attended_resume_rejects_a_corrupt_completed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a JSON receipt with arbitrary bytes must stop before another child."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", complete)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 0
    (output_root / "stage-results" / "ledger.json").write_bytes(b"not-json")

    def no_child(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("corrupt completion must fail before a child starts")

    monkeypatch.setattr(materializer.subprocess, "run", no_child)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 1
    )


def test_attended_resume_rejects_a_corrupt_completed_baseline_before_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ledger receipt may not make a changed generation skippable."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", complete)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 0
    baseline = (
        next((output_root / "stages" / "baseline" / "generations").iterdir())
        / "portfolio-baseline.json"
    )
    baseline.write_bytes(baseline.read_bytes() + b"\n")

    def no_child(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("corrupt baseline must fail before a child starts")

    monkeypatch.setattr(materializer.subprocess, "run", no_child)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 1
    )


def test_attended_preserves_failed_partial_child_and_retries_it_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed child retains pending output and keeps its original exit code."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"
    calls: list[str] = []

    def fail_baseline(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        calls.append(role)
        if role == "ledger":
            _record_attended_stage(output_root, role)
            return subprocess.CompletedProcess(argv, 0, "", "")
        (output_root / "stages" / "baseline").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 83, "", "failed baseline")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", fail_baseline)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 83

    def retry_baseline(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        calls.append(role)
        assert role == "baseline"
        shutil.rmtree(output_root / "stages" / "baseline")
        _record_attended_stage(output_root, role)
        return subprocess.CompletedProcess(argv, 97, "", "next boundary")

    monkeypatch.setattr(materializer.subprocess, "run", retry_baseline)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 97
    )
    assert calls == ["ledger", "baseline", "baseline"]


PANEL_SIDE_ROLES = ("panel-candidate", "panel")
ANALYTICS_SIDE_ROLES = ("analytics", "report")


def _stage_digest(root: Path) -> str:
    """One digest over the names and bytes of every file below a stage root."""

    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stage_snapshot(output_root: Path) -> dict[str, object]:
    """Bytes and write times of everything the analytics and report stages own."""

    files = {}
    for role in ANALYTICS_SIDE_ROLES:
        owned = [
            output_root / "stage-results" / f"{role}.json",
            *sorted(
                item
                for item in (output_root / "stages" / role).rglob("*")
                if item.is_file()
            ),
        ]
        for path in owned:
            files[path.relative_to(output_root).as_posix()] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
    return files


class _PanelFailureRun:
    """A four-stage attended run whose panel child can be made to fail.

    The children are real files written under the real stage roots. The two native
    readers that need a published analytics or report generation are replaced by a
    reader that binds each stage's receipt to the digest of the bytes below its root,
    so a changed or rewritten stage is still refused or detected. Everything under
    test, the resume ordering and the failed-stage preservation, is the real code.
    """

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, panel_fails: bool
    ) -> None:
        self.preflight = _attended_preflight_fixture(tmp_path)
        self.preflight["required_stages"] = (
            *ANALYTICS_SIDE_ROLES,
            *PANEL_SIDE_ROLES,
        )
        self.output_root = tmp_path / "output"
        self.panel_fails = panel_fails
        self.calls: list[str] = []

        def verify_bytes(
            receipt: dict[str, object], stage_root: Path, role: str
        ) -> None:
            if receipt.get("stage_sha256") != _stage_digest(stage_root):
                raise materializer.MaterializationError(
                    f"attended completed {role} artifact identity differs"
                )

        def verify_candidate(_receipt: dict[str, object], stage_root: Path) -> None:
            if not (stage_root / "panel-candidate.bin").is_file():
                raise materializer.MaterializationError(
                    "attended completed panel-candidate artifact identity differs"
                )

        monkeypatch.setattr(
            materializer, "_attended_completed_stage_root", verify_bytes
        )
        monkeypatch.setattr(
            materializer, "_attended_completed_panel_candidate", verify_candidate
        )
        monkeypatch.setattr(
            materializer, "_attended_preflight", lambda _args: self.preflight
        )
        monkeypatch.setattr(materializer.subprocess, "run", self.child)

    def child(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        self.calls.append(role)
        root = materializer._attended_stage_root(self.output_root, role)
        root.mkdir(parents=True, exist_ok=True)
        if role == "panel" and self.panel_fails:
            (root / "partial.bin").write_bytes(b"partial panel")
            return subprocess.CompletedProcess(argv, 71, "", "panel failed")
        (root / f"{role}.bin").write_bytes(f"bytes of {role}".encode() * 4)
        _write_json(
            self.output_root / "stage-results" / f"{role}.json",
            {"role": role, "stage_sha256": _stage_digest(root)},
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def execute(self, *, resume: bool = False) -> int:
        return materializer.main(
            _attended_argv(
                self.preflight, self.output_root, execute=True, resume=resume
            )
        )


def test_a_panel_stage_failure_resumes_without_rerunning_analytics_or_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M9: a presentation-stage failure does not regenerate the financial stages."""

    run = _PanelFailureRun(tmp_path, monkeypatch, panel_fails=True)
    assert run.execute() == 71
    assert run.calls == ["analytics", "report", "panel-candidate", "panel"]
    before = _stage_snapshot(run.output_root)
    assert len(before) == 4

    run.calls.clear()
    run.panel_fails = False
    assert run.execute(resume=True) == 0

    # Only the panel side ran again. `panel-candidate` is included because it shares
    # `stages/panel` with the failed `panel`, so its outputs went aside with it.
    assert run.calls == list(PANEL_SIDE_ROLES)
    assert _stage_snapshot(run.output_root) == before
    # The failed attempt's bytes are kept for diagnosis, and both panel receipts are
    # present again for the final view.
    preserved = run.output_root / "failed-stage-evidence" / "panel" / "attempt-1"
    assert (preserved / "stage-root" / "partial.bin").read_bytes() == b"partial panel"
    view = materializer._attended_view(
        run.output_root, run.preflight["required_stages"], {"fixture": "view"}
    )
    assert view["verified_completed_stages"] == list(run.preflight["required_stages"])


def test_a_changed_financial_stage_blocks_the_panel_resume_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M9 control: the unchanged-bytes claim is real, because a change is detected."""

    run = _PanelFailureRun(tmp_path, monkeypatch, panel_fails=True)
    assert run.execute() == 71
    analytics = run.output_root / "stages" / "analytics" / "analytics.bin"
    analytics.write_bytes(analytics.read_bytes() + b"x")

    run.calls.clear()
    run.panel_fails = False
    assert run.execute(resume=True) == 1
    assert run.calls == []
    assert not (run.output_root / "stage-results" / "panel.json").exists()


TOOLS_DIRECTORY = Path(materializer.__file__).resolve().parent
ENTRYPOINT_PROBE_SECONDS = 180


def _attended_child_entrypoints() -> list[str]:
    """The scripts an attended run launches as children, read from the launcher itself.

    The attended runner starts `portfolio_release_builder.py`, and that builder starts
    each numerical, report and panel producer it names. Reading the names from the
    builder's source keeps the list from drifting when a producer is added.
    """

    builder = TOOLS_DIRECTORY / "portfolio_release_builder.py"
    named = set(re.findall(r'"(portfolio_[a-z_]+\.py)"', builder.read_text("utf-8")))
    return sorted(
        {"portfolio_release_builder.py", "portfolio_pipeline_materialize.py"}
        | {name for name in named if (TOOLS_DIRECTORY / name).is_file()}
    )


def _probe_entrypoint(
    script: Path, cwd: Path
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Launch one script as a child would be: a fresh interpreter, no PYTHONPATH."""

    environment = {
        name: value for name, value in os.environ.items() if name != "PYTHONPATH"
    }

    def launch(argument: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), argument],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=ENTRYPOINT_PROBE_SECONDS,
            check=False,
        )

    return launch("--help"), launch("--not-an-argument-of-any-entrypoint")


def _entrypoint_violations(
    helped: subprocess.CompletedProcess[str],
    refused: subprocess.CompletedProcess[str],
) -> list[str]:
    violations = []
    if helped.returncode != 0 or "Error" in helped.stderr:
        violations.append(f"--help exits {helped.returncode}: {helped.stderr[-200:]}")
    elif not helped.stdout.strip():
        violations.append("--help prints no usage")
    if refused.returncode == 0:
        violations.append("an invalid argument exits 0")
    elif not refused.stderr.strip():
        violations.append("an invalid argument exits nonzero without a message")
    elif "ModuleNotFoundError" in refused.stderr or "ImportError" in refused.stderr:
        violations.append(f"import failure: {refused.stderr[-200:]}")
    return violations


def test_attended_child_entrypoints_include_every_stage_producer() -> None:
    entrypoints = _attended_child_entrypoints()

    assert {
        "portfolio_baseline.py",
        "portfolio_control_panel.py",
        "portfolio_correlation.py",
        "portfolio_decomposition.py",
        "portfolio_release_builder.py",
        "portfolio_report.py",
        "portfolio_weekly_returns.py",
    } <= set(entrypoints)


@pytest.mark.parametrize("entrypoint", _attended_child_entrypoints())
def test_every_attended_child_entrypoint_launches_and_propagates_failure(
    entrypoint: str, tmp_path: Path
) -> None:
    """M6: a child launched without PYTHONPATH imports, and a bad call fails loudly."""

    helped, refused = _probe_entrypoint(TOOLS_DIRECTORY / entrypoint, tmp_path)

    assert _entrypoint_violations(helped, refused) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import module_that_does_not_exist\n", "--help exits"),
        ("print('usage: ignores every argument')\n", "an invalid argument exits 0"),
        (
            "import sys\nprint('usage')\nsys.exit(0 if '--help' in sys.argv else 3)\n",
            "without a message",
        ),
    ],
    ids=["import-failure", "swallowed-failure", "silent-failure"],
)
def test_entrypoint_probe_flags_a_child_that_cannot_propagate_failure(
    source: str, expected: str, tmp_path: Path
) -> None:
    """M6 control: the probe fails on the shapes it exists to catch."""

    script = tmp_path / "broken_child.py"
    script.write_bytes(source.encode())

    helped, refused = _probe_entrypoint(script, tmp_path)

    violations = _entrypoint_violations(helped, refused)
    assert violations
    assert any(expected in violation for violation in violations)


def test_attended_preserve_moves_the_receipt_sharing_the_stage_root(
    tmp_path: Path,
) -> None:
    """A receipt whose bytes left with the shared root must leave with them."""

    output_root = tmp_path / "output"
    stage_root = output_root / "stages" / "panel"
    stage_root.mkdir(parents=True)
    (stage_root / "candidate.bin").write_bytes(b"candidate")
    results = output_root / "stage-results"
    results.mkdir()
    (results / "panel.json").write_bytes(b'{"role": "panel"}')
    (results / "panel-candidate.json").write_bytes(b'{"role": "panel-candidate"}')
    # A role with its own root must not drag anything else along.
    (results / "ledger.json").write_bytes(b'{"role": "ledger"}')

    materializer._attended_preserve_failed_stage(output_root, "panel")

    preserved = output_root / "failed-stage-evidence" / "panel" / "attempt-1"
    assert (preserved / "stage-root" / "candidate.bin").read_bytes() == b"candidate"
    assert (preserved / "stage-result.json").read_bytes() == b'{"role": "panel"}'
    # panel-candidate's outputs went with the root, so its receipt cannot stay behind and
    # claim they are still there.
    assert (preserved / "panel-candidate.json").read_bytes() == (
        b'{"role": "panel-candidate"}'
    )
    assert not (results / "panel-candidate.json").exists()
    assert (results / "ledger.json").is_file()
    assert sorted(item.name for item in stage_root.iterdir()) == []


def test_attended_shared_stage_roles_are_derived_not_named(tmp_path: Path) -> None:
    """The pair sharing a root is read from the mapping, so a new one cannot be missed."""

    output_root = tmp_path / "output"
    assert materializer._attended_shared_stage_roles(output_root, "panel") == (
        "panel-candidate",
    )
    assert materializer._attended_shared_stage_roles(
        output_root, "panel-candidate"
    ) == ("panel",)
    assert materializer._attended_shared_stage_roles(output_root, "ledger") == ()


def test_attended_resume_records_one_attempt_for_one_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserving a failed stage must not also preserve the empty root it recreates.

    This one guards the partial-root recovery below rather than the defect it fixes: the
    preserve recreates its stage root empty, so a recovery keyed on existence alone preserves
    a second time and leaves an empty `attempt-2`. It passes on code that has no recovery at
    all, which is why it is documented as a regression guard and not as proof of the fix.
    """

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def fail_baseline(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        if role == "ledger":
            _record_attended_stage(output_root, role)
            return subprocess.CompletedProcess(argv, 0, "", "")
        (output_root / "stages" / "baseline").mkdir(parents=True, exist_ok=True)
        (output_root / "stages" / "baseline" / "partial.bin").write_bytes(b"partial")
        return subprocess.CompletedProcess(argv, 83, "", "failed baseline")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", fail_baseline)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 83

    def retry(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        _record_attended_stage(output_root, role)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer.subprocess, "run", retry)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 0
    )
    attempts = sorted((output_root / "failed-stage-evidence" / "baseline").iterdir())
    assert [item.name for item in attempts] == ["attempt-1"]
    assert (attempts[0] / "stage-root" / "partial.bin").read_bytes() == b"partial"


def test_attended_resume_preserves_a_partial_root_left_without_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption before the parent records the failure still leaves partial bytes."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def fail_baseline(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        if role == "ledger":
            _record_attended_stage(output_root, role)
            return subprocess.CompletedProcess(argv, 0, "", "")
        (output_root / "stages" / "baseline").mkdir(parents=True, exist_ok=True)
        (output_root / "stages" / "baseline" / "partial.bin").write_bytes(b"partial")
        return subprocess.CompletedProcess(argv, 83, "", "failed baseline")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", fail_baseline)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 83

    # Emulate a kill that took the process before it could record which stage failed: the
    # partial bytes are on disk and the receipt says nothing about them.
    receipt = output_root / "attended-run.json"
    value = json.loads(receipt.read_text("utf-8"))
    receipt.write_bytes(
        canonical_json_bytes(
            {"request_identity": value["request_identity"], "schema_version": 1}
        )
    )

    seen: list[list[str]] = []

    def retry(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        if role == "baseline":
            seen.append(
                sorted(
                    item.name
                    for item in (output_root / "stages" / "baseline").iterdir()
                )
            )
        _record_attended_stage(output_root, role)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer.subprocess, "run", retry)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 0
    )
    # The retried child sees an empty root, not its own half-written output.
    assert seen == [[]]
    preserved = output_root / "failed-stage-evidence" / "baseline" / "attempt-1"
    assert (preserved / "stage-root" / "partial.bin").read_bytes() == b"partial"


def test_attended_request_identity_binds_the_evaluation_clock(tmp_path: Path) -> None:
    """A changed evaluation date must change the identity a resume is validated against."""

    preflight = _attended_preflight_fixture(tmp_path)
    shared: dict[str, object] = {
        "repo_root": preflight["repo_root"],
        "python": preflight["python"],
        "workspace_root": preflight["workspace_root"],
        "output_root": tmp_path / "output",
        "retained_cache_root": preflight["retained_cache_root"],
        "requested_as_of": "2026-09-17",
        "source_run": preflight["source_run"],
        "financial_stage_inputs": {},
        "source_phase": {},
        "runtime": {},
    }
    first = materializer._attended_request_identity(evaluated_on="2026-09-21", **shared)
    second = materializer._attended_request_identity(
        evaluated_on="2026-09-22", **shared
    )

    assert first["evaluated_on"] == "2026-09-21"
    assert canonical_json_bytes(first) != canonical_json_bytes(second)
    # The financial cutoff is a separate clock and must not move with it.
    assert first["requested_as_of"] == second["requested_as_of"] == "2026-09-17"


def test_attended_preserves_original_exit_when_child_leaves_partial_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed receipt from a failed child remains pending, not completed."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"

    def fail_ledger(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        (output_root / "stages" / "ledger").mkdir(parents=True)
        (output_root / "stage-results").mkdir(parents=True)
        (output_root / "stage-results" / "ledger.json").write_bytes(b"{")
        return subprocess.CompletedProcess(argv, 97, "", "partial ledger")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", fail_ledger)

    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 97
    assert (output_root / "stage-results" / "ledger.json").read_bytes() == b"{"

    calls: list[str] = []

    def retry_pending_ledger(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        calls.append(role)
        if role == "ledger":
            import portfolio_release_builder as release

            expected_root = tmp_path / "expected-ledger"
            _record_attended_stage(expected_root, role)
            expected = json.loads(
                (expected_root / "stage-results" / "ledger.json").read_text(
                    encoding="utf-8"
                )
            )
            expected["root"] = str(output_root / "stages" / "ledger")
            shutil.copytree(
                expected_root / "stages" / "ledger",
                output_root / "stages" / "ledger",
                dirs_exist_ok=True,
            )
            release._record_stage(output_root, role, expected)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 97, "", "next boundary")

    monkeypatch.setattr(materializer.subprocess, "run", retry_pending_ledger)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 97
    )
    assert calls == ["ledger", "baseline"]
    assert json.loads((output_root / "stage-results" / "ledger.json").read_text())
    assert any(
        path.read_bytes() == b"{"
        for path in (output_root / "failed-stage-evidence").rglob("stage-result.json")
    )
    checkpoint = json.loads((output_root / "attended-run.json").read_text())
    assert checkpoint["schema_version"] == 2
    assert checkpoint["failed_stage"] == {"returncode": 97, "role": "baseline"}


def test_attended_execution_observations_preserve_cold_and_resumed_attempt_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execution timing is an operational receipt, not a financial identity input."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger",)
    output_root = tmp_path / "output"
    times = iter(
        (
            "2026-09-19T12:00:00+00:00",
            "2026-09-19T12:00:01+00:00",
            "2026-09-19T12:01:00+00:00",
            "2026-09-19T12:01:01+00:00",
        )
    )
    monkeypatch.setattr(materializer, "_execution_timestamp", lambda: next(times))

    def fail_once(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 97, "", "ledger failed")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", fail_once)
    assert materializer.main(_attended_argv(preflight, output_root, execute=True)) == 97

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, "ledger")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer.subprocess, "run", complete)
    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 0
    )

    receipt = json.loads(
        (output_root / "attended-execution-observations.json").read_text(
            encoding="utf-8"
        )
    )
    storage = [attempt.pop("storage") for attempt in receipt["execution_attempts"]]
    assert receipt == {
        "execution_attempts": [
            {
                "attempt": 1,
                "ended_at": "2026-09-19T12:00:01+00:00",
                "pid": os.getpid(),
                "reason": "stage ledger exited 97",
                "started_at": "2026-09-19T12:00:00+00:00",
                "status": "FAILED",
            },
            {
                "attempt": 2,
                "ended_at": "2026-09-19T12:01:01+00:00",
                "pid": os.getpid(),
                "started_at": "2026-09-19T12:01:00+00:00",
                "status": "PASSED",
            },
        ],
        "preparation_attempts": [],
        "schema_version": 3,
    }
    # Bytes are recorded per stage child, including the one that failed.
    assert [row["role"] for row in storage[0]["stages"]] == ["ledger"]
    assert [row["role"] for row in storage[1]["stages"]] == ["ledger"]
    assert storage[1]["stages"][0]["bytes_written"] > 0
    assert "end" in storage[1] and "end" not in storage[0]
    assert (
        json.loads((output_root / "attended-run.json").read_text())["request_identity"]
        == (preflight["request_identity"])
    )


def test_attended_execution_observations_mark_interrupted_attempt_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run records interruption without inventing a terminal instant."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger",)
    output_root = tmp_path / "output"
    times = iter(
        (
            "2026-09-19T12:00:00+00:00",
            "2026-09-19T12:01:00+00:00",
            "2026-09-19T12:01:01+00:00",
        )
    )
    monkeypatch.setattr(materializer, "_execution_timestamp", lambda: next(times))

    def interrupted(
        _argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(materializer.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        materializer._attended_execute(preflight, output_root, resume=False)

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _record_attended_stage(output_root, "ledger")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer.subprocess, "run", complete)
    assert materializer._attended_execute(preflight, output_root, resume=True)[0] == 0
    attempts = json.loads(
        (output_root / "attended-execution-observations.json").read_text(
            encoding="utf-8"
        )
    )["execution_attempts"]
    # Interrupted is CANCELLED (formerly INCOMPLETE), still without an ending instant.
    assert attempts[0] == {
        "attempt": 1,
        "pid": os.getpid(),
        "started_at": "2026-09-19T12:00:00+00:00",
        "status": "CANCELLED",
    }
    assert attempts[1]["status"] == "PASSED"


@pytest.mark.parametrize(
    "role",
    ("attribution", "analytics", "report", "panel-candidate", "panel"),
)
def test_attended_completed_nonledger_stage_requires_bound_native_artifacts(
    tmp_path: Path, role: str
) -> None:
    """A non-ledger result receipt alone is never a verified resume artifact."""

    output_root = tmp_path / "output"
    materializer._attended_stage_root(output_root, role).mkdir(parents=True)
    _write_json(output_root / "stage-results" / f"{role}.json", {"role": role})

    with pytest.raises(materializer.MaterializationError, match=f"completed {role}"):
        materializer._attended_completed_stages(output_root, (role,))


@pytest.mark.parametrize("mutation", ("tamper", "missing"))
def test_attended_completed_panel_verifies_retained_qa_generation(
    tmp_path: Path, mutation: str
) -> None:
    """Panel resume must verify the generation's retained QA payloads."""

    root = tmp_path / "output" / "stages" / "panel"
    as_of = date(2026, 9, 17)
    payloads = {
        "panel/index.html": b"panel",
        "verification/independent-verification.json": canonical_json_bytes({}),
        "evidence/qa-validation.json": canonical_json_bytes({"all_passed": True}),
        "evidence/semantic-validation.json": canonical_json_bytes({"all_passed": True}),
    }
    artifacts = tuple(
        SourceArtifact(
            artifact_id=relative,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            as_of_date=as_of,
            schema_name="attended-panel-test",
            schema_version="1",
        )
        for relative, content in payloads.items()
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("attended-panel-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="attended panel QA resume test",
            fact_types=("panel", "browser QA"),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    # The real panel producer stages with the bare `stage_generation` and then
    # writes each payload itself, QA under `qa/<name>` while declaring the
    # artifact id `evidence/<name>` (portfolio_control_panel.py:12812,12856).
    # Building this fixture with `stage_generation_bundle` produced a shape
    # production never emits, so the gate passed here and failed on every real
    # run.
    manifest_path = stage_generation(root, manifest)
    generation_dir = manifest_path.parent
    for artifact_id, content in payloads.items():
        relative = artifact_id.split("/", 1)[1]
        written = (
            generation_dir / "qa" / relative
            if artifact_id.startswith("evidence/")
            else generation_dir / relative
        )
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_bytes(content)
    publish_generation(root, manifest.generation_id)
    receipt = {
        "generation_id": manifest.generation_id,
        "root": str(root),
    }
    _write_json(tmp_path / "output" / "stage-results" / "panel.json", receipt)

    materializer._attended_completed_stages(  # noqa: SLF001
        tmp_path / "output", ("panel",)
    )
    target = root / "generations" / manifest.generation_id / "qa" / "qa-validation.json"
    if mutation == "tamper":
        target.write_bytes(b'{"all_passed":false}')
    else:
        target.unlink()
    with pytest.raises(
        materializer.MaterializationError, match="attended completed panel"
    ):
        materializer._attended_completed_stage_root(  # noqa: SLF001
            receipt, root, "panel"
        )


@pytest.mark.parametrize("mutation", ("tamper", "missing"))
def test_attended_completed_report_verifies_the_published_generation(
    tmp_path: Path, mutation: str
) -> None:
    """Report resume must bind the published report bytes to their manifest.

    Built with the real producer's shape: `portfolio_report.publish_report`
    stages with the bare `stage_generation` and writes the report data at
    `portfolio-report-data.json` while declaring the artifact id
    `normalized/portfolio-report-data.json`. The detached-bundle verifier
    cannot read that -- no sidecars, and the declared ids are logical rather
    than on-disk paths -- so this gate failed on every real run and had no
    test at all.
    """

    root = tmp_path / "output" / "stages" / "report"
    as_of = date(2026, 9, 17)
    report = {"report_schema_version": 1, "as_of_date": as_of.isoformat()}
    report_bytes = canonical_json_bytes(report)
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("attended-report-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="attended report resume test",
            fact_types=("portfolio report",),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/portfolio-report-data.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=as_of,
                schema_name="portfolio-report-data",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    generation_dir = manifest_path.parent
    (generation_dir / "portfolio-report-data.json").write_bytes(report_bytes)
    publish_generation(root, manifest.generation_id)

    candidate = root / "candidates" / hashlib.sha256(report_bytes).hexdigest()
    candidate.mkdir(parents=True)
    (candidate / "portfolio-report-data.json").write_bytes(report_bytes)

    receipt = {
        "generation_id": manifest.generation_id,
        "root": str(root),
        "candidate_dir": str(candidate),
    }
    _write_json(tmp_path / "output" / "stage-results" / "report.json", receipt)

    materializer._attended_completed_stages(  # noqa: SLF001
        tmp_path / "output", ("report",)
    )

    target = generation_dir / "portfolio-report-data.json"
    if mutation == "tamper":
        target.write_bytes(canonical_json_bytes({"report_schema_version": 1, "x": 1}))
    else:
        target.unlink()
    with pytest.raises(
        materializer.MaterializationError, match="attended completed report"
    ):
        materializer._attended_completed_stages(  # noqa: SLF001
            tmp_path / "output", ("report",)
        )


def test_attended_forwards_complete_governed_financial_families_and_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attended boundary keeps all governed stage inputs intact."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"
    qa_dir = tmp_path / "qa"
    supplied = {
        "--hwm-governed-result": tmp_path / "hwm-result.json",
        "--hwm-governed-input-generation": tmp_path / "hwm-generation.json",
        "--hwm-governed-calendar-authority": tmp_path / "hwm-calendar.json",
        "--hwm-governed-admission-receipt": tmp_path / "hwm-receipt.json",
        "--invested-capital-result": tmp_path / "capital-result.json",
        "--invested-capital-input-generation": tmp_path / "capital-generation.json",
    }
    for path in supplied.values():
        path.write_text("{}", encoding="utf-8")
    preflight["financial_stage_inputs"] = supplied
    preflight["qa_dir"] = qa_dir
    calls: list[list[str]] = []

    def complete(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        _record_attended_stage(output_root, argv[argv.index("--role") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", complete)
    argv = _attended_argv(preflight, output_root, execute=True)
    argv.extend(item for flag, path in supplied.items() for item in (flag, str(path)))
    argv.extend(("--qa-dir", str(qa_dir)))
    assert materializer.main(argv) == 0
    assert len(calls) == len(preflight["required_stages"])
    for command in calls:
        for flag, path in supplied.items():
            assert command[command.index(flag) + 1] == str(path)
    panel = materializer._attended_stage_command(preflight, output_root, "panel")
    assert panel[panel.index("--qa-dir") + 1] == str(qa_dir)


class _NoopRelease:
    """A release builder whose own family checks accept anything."""

    @staticmethod
    def _governed_hwm_arguments(**_kwargs: object) -> None:
        return None

    @staticmethod
    def _invested_capital_arguments(**_kwargs: object) -> None:
        return None


def _financial_args(tmp_path: Path, supplied: dict[str, str]) -> argparse.Namespace:
    values: dict[str, object] = {
        attribute: None
        for attribute in (
            *materializer.ATTENDED_HWM_INPUTS.values(),
            *materializer.ATTENDED_CAPITAL_INPUTS.values(),
        )
    }
    for attribute in supplied:
        path = tmp_path / f"{attribute}.json"
        path.write_text("{}", encoding="utf-8")
        values[attribute] = path
    return argparse.Namespace(**values)


def test_attended_preflight_refuses_a_run_without_the_invested_capital_family(
    tmp_path: Path,
) -> None:
    """N36/B7: the contract marks INVESTED_CAPITAL REQUIRED, so none is not optional."""

    for supplied in (
        {},
        {"invested_capital_result": ""},
        {"invested_capital_input_generation": ""},
    ):
        with pytest.raises(
            materializer.MaterializationError,
            match="requires the governed invested-capital",
        ):
            materializer._attended_stage_financial_inputs(  # noqa: SLF001
                _financial_args(tmp_path, supplied), _NoopRelease()
            )


def test_attended_preflight_accepts_the_capital_family_without_hwm(
    tmp_path: Path,
) -> None:
    accepted = materializer._attended_stage_financial_inputs(  # noqa: SLF001
        _financial_args(
            tmp_path,
            {"invested_capital_result": "", "invested_capital_input_generation": ""},
        ),
        _NoopRelease(),
    )

    assert set(accepted) == set(materializer.ATTENDED_CAPITAL_INPUTS)


def test_attended_stage_command_requires_the_capital_family(tmp_path: Path) -> None:
    preflight = _attended_preflight_fixture(tmp_path)
    preflight["financial_stage_inputs"] = {}

    with pytest.raises(
        materializer.MaterializationError, match="financial input family"
    ):
        materializer._attended_stage_command(preflight, tmp_path / "output", "ledger")


def test_attended_rejects_partial_governed_financial_family_before_child(
    tmp_path: Path,
) -> None:
    """A lone governed HWM path cannot enter an attended stage command."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["financial_stage_inputs"] = {
        "--hwm-governed-result": tmp_path / "hwm-result.json"
    }

    with pytest.raises(
        materializer.MaterializationError, match="financial input family"
    ):
        materializer._attended_stage_command(preflight, tmp_path / "output", "ledger")


def test_attended_binds_late_qa_without_replacing_final_panel_evidence(
    tmp_path: Path,
) -> None:
    """QA may arrive for an incomplete run but is immutable after panel completion."""

    initial = _attended_preflight_fixture(tmp_path)
    initial["required_stages"] = ("ledger", "baseline")
    initial["qa_dir"] = None
    output_root = tmp_path / "output"
    materializer._attended_start_or_resume(initial, output_root, resume=False)

    qa_dir = tmp_path / "qa"
    late = dict(initial)
    late["qa_dir"] = qa_dir
    materializer._attended_start_or_resume(late, output_root, resume=True)
    bound = json.loads((output_root / "attended-qa.json").read_text(encoding="utf-8"))
    assert bound["path"] == str(qa_dir)

    for role in materializer.ATTENDED_STAGES:
        _record_attended_stage(output_root, role)
    replacement = tmp_path / "replacement-qa"
    replacement.mkdir()
    changed = dict(initial)
    changed["qa_dir"] = replacement
    with pytest.raises(materializer.MaterializationError, match="QA identity"):
        materializer._attended_start_or_resume(changed, output_root, resume=True)


def test_attended_source_only_resume_still_rejects_a_missing_run_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy source-only resume cannot bootstrap a downstream receipt."""

    preflight = _attended_preflight_fixture(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    calls: list[list[str]] = []

    def no_child(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", no_child)

    assert (
        materializer.main(
            _attended_argv(preflight, output_root, execute=True, resume=True)
        )
        == 1
    )
    assert calls == []


def test_attended_rejects_missing_run_receipt_after_execution_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost downstream receipt never restarts verified completed producers."""

    preflight = _attended_preflight_fixture(tmp_path)
    preflight["required_stages"] = ("ledger", "baseline")
    output_root = tmp_path / "output"
    output_root.mkdir()
    clock = {"selected_financial_cutoff": "2026-09-17"}
    source_run = preflight["source_run"]
    assert isinstance(source_run, Path)
    source_run.write_bytes(canonical_json_bytes({"attended": {"data_clock": clock}}))
    (output_root / "preparation-handoff.json").write_bytes(
        canonical_json_bytes(
            {
                "data_clock": clock,
                "financial_stage_inputs": {
                    flag: materializer._file_definition(path)
                    for flag, path in preflight["financial_stage_inputs"].items()  # type: ignore[attr-defined]
                },
                "schema_version": 1,
                "source_phase": preflight["source_phase"],
                "source_run": materializer._file_definition(source_run),
            }
        )
    )
    materializer._attended_start_or_resume(preflight, output_root, resume=False)
    _record_attended_stage(output_root, "ledger")
    materializer._attended_start_execution_attempt(output_root)
    (output_root / "attended-run.json").unlink()
    calls: list[list[str]] = []

    def no_child(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(materializer.subprocess, "run", no_child)

    with pytest.raises(materializer.MaterializationError, match="attended run receipt"):
        materializer._attended_execute(preflight, output_root, resume=True)

    assert calls == []
    assert (output_root / "stage-results" / "ledger.json").is_file()


# The tool must be the thing being run, not merely named. `docs/ci.md:108`
# passes it to mypy inside a long file list, where the following token is the
# next path rather than a subcommand.
_MATERIALIZE = re.compile(
    r"(?:\S*[Pp]ython\S*|\"?\$\{?PY\}?\"?)\s+"
    r"\S*portfolio_pipeline_materialize\.py\s+([a-z][a-z-]*)"
)


def _subcommand_parsers() -> dict[str, argparse.ArgumentParser]:
    parser = materializer._parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the materializer parser declares no subcommands")


def _required_options(parser: argparse.ArgumentParser) -> list[str]:
    return [
        action.option_strings[0]
        for action in parser._actions
        if action.required and action.option_strings
    ]


def _documented_invocations() -> list[tuple[Path, int, str, str]]:
    """Every checked-in `portfolio_pipeline_materialize.py <subcommand>` call.

    Continuations are joined by fence language -- PowerShell continues a
    command with a trailing backtick, sh with a trailing backslash -- and
    prose is skipped entirely. Markdown prose ends in a backtick whenever it
    closes an inline code span, so a scan that ignored fences would splice an
    unrelated sentence onto a command and check the wrong string.
    """

    found: list[tuple[Path, int, str, str]] = []
    targets = sorted((ROOT / "docs").rglob("*.md")) + sorted(
        (ROOT / "ops").rglob("*.sh")
    )
    for path in targets:
        lines = path.read_text(encoding="utf-8").splitlines()
        if path.suffix == ".sh":
            languages: list[str | None] = ["sh"] * len(lines)
        else:
            languages = []
            language: str | None = None
            for line in lines:
                if line.startswith("```"):
                    language = (
                        None if language is not None else line[3:].strip().lower()
                    )
                    languages.append(None)
                else:
                    languages.append(language)
        for index, line in enumerate(lines):
            language = languages[index]
            if language is None or not _MATERIALIZE.search(line):
                continue
            terminator = "`" if language in {"powershell", "pwsh"} else "\\"
            block = [line]
            cursor = index
            while block[-1].rstrip().endswith(terminator) and cursor + 1 < len(lines):
                cursor += 1
                block.append(lines[cursor])
            command = " ".join(part.rstrip().removesuffix(terminator) for part in block)
            match = _MATERIALIZE.search(command)
            assert match is not None
            found.append((path, index + 1, match.group(1), command))
    return found


def test_documented_cli_invocations_supply_every_required_option() -> None:
    """A checked-in procedure that omits a required option cannot start.

    Both rollforward runbooks invoked `spec` without `--portfolio-root` from
    2026-08 onward. `build_spec` refuses unconditionally without that root, but
    the CLI declared the option optional, so the two contracts disagreed and
    only a live run could tell. The requirement is read off the real parser
    here rather than a hand-maintained list, so a newly required option cannot
    land without its documented callers moving with it.
    """

    parsers = _subcommand_parsers()
    invocations = _documented_invocations()
    assert invocations, "no documented invocation was found, so this proves nothing"

    missing: list[str] = []
    for path, line, subcommand, command in invocations:
        assert subcommand in parsers, (
            f"{path.name}:{line} names unknown subcommand {subcommand!r}"
        )
        tokens = set(command.split())
        for option in _required_options(parsers[subcommand]):
            if option in tokens or any(
                token.startswith(f"{option}=") for token in tokens
            ):
                continue
            location = path.relative_to(ROOT).as_posix()
            missing.append(f"{location}:{line} `{subcommand}` omits {option}")

    assert not missing, "documented invocations omit required options: " + "; ".join(
        missing
    )


def test_admission_policy_artifact_resolves_a_non_canonical_selected_input(tmp_path):
    """The selected admission policy travels as its own production node, not inside the generation.

    This is the property the old resolver could not satisfy. The captured policy is
    pretty-printed, and every generation artifact must be canonical JSON, so the selected
    input's own bytes can never appear among the generation's artifacts -- the generation
    stores a canonicalised *projection* instead. Searching the generation for the declared
    digest therefore found zero matches and refused every schema-10 release.

    The fixture payload is deliberately not canonical. A canonical one would pass under both
    the old and the new resolver and prove nothing.
    """

    payload = b'{\n  "policy_id": "epoch-admission",\n  "admissions": []\n}\n'
    assert canonical_json_bytes(json.loads(payload)) != payload
    path = tmp_path / "mapped-epoch-admission-current-v10-dated.json"
    path.write_bytes(payload)
    row = {
        "kind": "ARTIFACT",
        "node_id": "financial-account-manifest-admission-policy",
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }

    assert materializer._admission_policy_artifact([row]) == path

    with pytest.raises(materializer.MaterializationError, match="absent or ambiguous"):
        materializer._admission_policy_artifact([])
    with pytest.raises(materializer.MaterializationError, match="absent or ambiguous"):
        materializer._admission_policy_artifact([row, dict(row)])

    other = dict(row)
    other["node_id"] = "financial-account-manifest-generation"
    with pytest.raises(materializer.MaterializationError, match="absent or ambiguous"):
        materializer._admission_policy_artifact([other])

    path.write_bytes(payload + b"\n")
    with pytest.raises(materializer.MaterializationError, match="binding differs"):
        materializer._admission_policy_artifact([row])


# The retained baseline's audit snapshot is sealed: its replay, and the materializer's
# identity check against it, add the allocation table the way the audit did (the default
# 28-digit context).  Adding it exactly gives a different figure for a wide table and the
# retained generation would no longer verify.
_WIDE_ALLOCATION = "10000000000000000000000000000"
_HISTORICAL_SUM = Decimal("10000000000000000000000000000")


def _allocation_rows(values: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "pool_id": "systematic-capital",
            "current_value": {"currency": "USD", "status": "AVAILABLE", "value": value},
            "systematic_weight": "0.1111111111111111111111111111111111",
        }
        for value in values
    ]


@pytest.mark.parametrize(
    "values",
    [
        [_WIDE_ALLOCATION, "0.4", "0.4"],
        ["0.4", "0.4", _WIDE_ALLOCATION],
    ],
)
def test_snapshot_sums_keep_the_historical_arithmetic_in_either_row_order(
    values: list[str],
) -> None:
    reserve = Decimal("0.4")

    allocation_sum, weight_sum, total = materializer._financial_snapshot_sums(
        _allocation_rows(values), _HISTORICAL_SUM, reserve
    )

    assert allocation_sum == _HISTORICAL_SUM
    assert total == _HISTORICAL_SUM
    assert weight_sum == Decimal("0.3333333333333333333333333333")


def test_snapshot_sums_do_not_depend_on_the_callers_precision() -> None:
    results = []
    for precision in (28, 50):
        with localcontext() as context:
            context.prec = precision
            results.append(
                materializer._financial_snapshot_sums(
                    _allocation_rows([_WIDE_ALLOCATION, "0.4", "0.4"]),
                    _HISTORICAL_SUM,
                    Decimal("0.4"),
                )
            )

    assert results[0] == results[1]


def _add_panel_strategy_rows(root: Path, *rows: dict[str, object]) -> None:
    panel = next((root / "stages" / "panel" / "generations").iterdir())
    _mutate_json(
        panel / "data" / "dashboard_data.json",
        lambda payload: payload.update(
            {"strategies": [*payload.get("strategies", []), *rows]}
        ),
    )


def test_semantic_equality_ignores_retained_history_only_panel_rows(
    tmp_path: Path,
) -> None:
    """D18: a retained history-only record is not a product strategy (one shared rule)."""

    recipe, output_root = _fixture(tmp_path)
    _add_panel_strategy_rows(
        output_root,
        {"strategy_id": "retired-one", "record_kind": "RETAINED_HISTORY_ONLY"},
        {"strategy_id": "retired-two", "record_kind": "RETAINED_HISTORY_ONLY"},
    )
    _refresh_expected_generation(recipe, output_root, "panel")

    result = materializer.semantic(
        recipe, output_root / "evidence" / "post-build-semantic.json"
    )

    assert result["state"] == "PASS"
    details = json.loads(
        (output_root / "evidence" / "semantic-details.json").read_text()
    )
    equality = details["checks"]["analytics_report_panel_equality"]["evidence"]
    assert equality["panel_retained_history_only_count"] == 2
    assert equality["panel_strategy_count"] == equality["report_strategy_count"]


def test_semantic_equality_still_refuses_a_product_panel_row_the_report_lacks(
    tmp_path: Path,
) -> None:
    """A panel row without the retained marker must still match the report."""

    recipe, output_root = _fixture(tmp_path)
    _add_panel_strategy_rows(
        output_root,
        {"strategy_id": "retired-one", "record_kind": "RETAINED_HISTORY_ONLY"},
        {"strategy_id": "not-in-the-report"},
    )
    _refresh_expected_generation(recipe, output_root, "panel")

    with pytest.raises(
        materializer.MaterializationError,
        match="analytics_report_panel_equality",
    ):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )


def test_semantic_equality_still_refuses_a_report_row_the_panel_lacks(
    tmp_path: Path,
) -> None:
    recipe, output_root = _fixture(tmp_path)
    panel = next((output_root / "stages" / "panel" / "generations").iterdir())
    _mutate_json(
        panel / "data" / "dashboard_data.json",
        lambda payload: payload.update({"strategies": payload["strategies"][:-1]}),
    )
    _refresh_expected_generation(recipe, output_root, "panel")

    with pytest.raises(
        materializer.MaterializationError,
        match="analytics_report_panel_equality",
    ):
        materializer.semantic(
            recipe, output_root / "evidence" / "post-build-semantic.json"
        )


def test_the_product_strategy_rule_is_one_shared_helper() -> None:
    import lifecycle_fields

    rows = [
        {"strategy_id": "a"},
        {"strategy_id": "b", "record_kind": "RETAINED_HISTORY_ONLY"},
        {"strategy_id": "c", "record_kind": "SOMETHING_ELSE"},
        "not a row",
    ]
    assert lifecycle_fields.product_strategy_rows(rows) == [rows[0], rows[2], rows[3]]
    assert lifecycle_fields.is_retained_history_only(rows[1])
    assert not lifecycle_fields.is_retained_history_only(rows[0])
    assert not lifecycle_fields.is_retained_history_only(rows[3])


def test_release_identity_binds_commit_or_tree_and_refuses_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head, tree = "a" * 40, "b" * 40
    answers = {("rev-parse", "HEAD"): head, ("rev-parse", "HEAD^{tree}"): tree}
    monkeypatch.setattr(
        materializer, "_git", lambda _repo, *args: answers.get(args, "")
    )
    repo = Path(".")
    assert materializer._release_identity(repo, head) == (head, "git-commit-v1")
    assert materializer._release_identity(repo, tree) == (tree, "git-tree-v2")
    for wrong in ("c" * 40, "", None):
        with pytest.raises(materializer.MaterializationError):
            materializer._release_identity(repo, wrong)
    assert materializer._spec_identity_fields("git-commit-v1") == {}
    assert materializer._spec_identity_fields("git-tree-v2") == {
        "code_identity_scheme": "git-tree-v2"
    }

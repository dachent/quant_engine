from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    PipelineExecutionError,
    PipelineSpec,
    PipelineValidationError,
    SourceArtifact,
    VerificationStatus,
    execute_pipeline,
    pipeline_status,
    plan_pipeline,
    publish_generation,
    read_current,
    runtime_environment_sha256,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MATERIALIZER_SPEC = importlib.util.spec_from_file_location(
    "portfolio_pipeline_materialize_for_orchestration",
    ROOT / "tools" / "portfolio_pipeline_materialize.py",
)
assert MATERIALIZER_SPEC is not None and MATERIALIZER_SPEC.loader is not None
materializer = importlib.util.module_from_spec(MATERIALIZER_SPEC)
MATERIALIZER_SPEC.loader.exec_module(materializer)
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "financial_mvp_fixture_for_orchestration",
    ROOT / "tests" / "core" / "financial_mvp_fixture.py",
)
assert FIXTURE_SPEC is not None and FIXTURE_SPEC.loader is not None
financial_fixture = importlib.util.module_from_spec(FIXTURE_SPEC)
sys.modules[FIXTURE_SPEC.name] = financial_fixture
FIXTURE_SPEC.loader.exec_module(financial_fixture)


AS_OF = date(2026, 8, 3)
STAGES = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "workbook",
    "panel",
)


def _manifest(label: str) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("portfolio-orchestration-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=label,
            fact_types=("synthetic",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _command(
    *,
    node_id: str,
    runtime: Path,
    lock: Path,
    entrypoint: Path,
    depends_on: list[str],
    reads: list[Path] | None = None,
    writes: list[Path],
    retry_limit: int = 0,
) -> dict[str, object]:
    return {
        "argv": [str(runtime), str(entrypoint), node_id],
        "cwd": str(lock.parent),
        "declared_reads": [
            str(lock),
            str(entrypoint),
            *(str(path) for path in reads or []),
        ],
        "declared_writes": [str(path) for path in writes],
        "depends_on": depends_on,
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": node_id,
        "request_identity": None,
        "retry_limit": retry_limit,
        "timeout_seconds": 30,
    }


def _portfolio_spec(
    tmp_path: Path,
    *,
    semantic_verdict: str = "PASS",
    financial_mvp_ready: bool | None = True,
) -> tuple[dict[str, object], dict[str, bytes | Path], GenerationManifest]:
    runtime = Path(sys.executable).resolve()
    runtime_bytes = runtime.read_bytes()
    lock = tmp_path / "repo" / "uv.lock"
    lock.parent.mkdir(parents=True)
    lock_bytes = b"synthetic lock\n"
    lock.write_bytes(lock_bytes)
    entrypoint = lock.parent / "runner.py"
    entrypoint_bytes = b"print('synthetic pipeline entrypoint')\n"
    entrypoint.write_bytes(entrypoint_bytes)
    subprocess.run(["git", "init", "-q", str(lock.parent)], check=True)
    subprocess.run(
        ["git", "-C", str(lock.parent), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(lock.parent), "config", "user.name", "Pipeline Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(lock.parent), "add", "uv.lock", "runner.py"], check=True
    )
    subprocess.run(
        ["git", "-C", str(lock.parent), "commit", "-q", "-m", "fixture"],
        check=True,
    )
    release_commit = subprocess.run(
        ["git", "-C", str(lock.parent), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    target = tmp_path / "published" / "reporting"
    previous = _manifest("previous")
    proposed = _manifest("proposed")
    stage_generation(target, previous)
    publish_generation(target, previous.generation_id)

    source_run = tmp_path / "sources" / "runs" / ("c" * 64)
    authority_dir = tmp_path / "candidate" / "authority"
    authority_path = authority_dir / "authority-snapshot.json"
    authority_bytes = canonical_json_bytes(
        {
            "adapter_commit": release_commit,
            "as_of_date": AS_OF.isoformat(),
            "source_run_id": "c" * 64,
        }
    )
    stage_outputs = {
        role: tmp_path / "candidate" / role for role in STAGES if role != "report"
    }
    report_output = (
        tmp_path / "candidate" / "report" / "generations" / proposed.generation_id
    )
    semantic_path = tmp_path / "candidate" / "post-build-semantic.json"
    semantic_payload = {
        "as_of_date": AS_OF.isoformat(),
        "checks": {
            "analytics_report_panel_equality": "PASS",
            "financial_identities": "PASS",
            "freshness": "PASS",
            "generation_invariants": "PASS",
            "ledger_bridges": "PASS",
            "mappings": "PASS",
        },
        "release_commit": release_commit,
        "release_generations": {"reporting": proposed.generation_id},
        "schema_version": 1,
        "unwaived_blocking_exceptions": 0,
        "verdict": semantic_verdict,
    }
    semantic_bytes = canonical_json_bytes(semantic_payload)

    acquisition = _command(
        node_id="acquire-source",
        runtime=runtime,
        lock=lock,
        entrypoint=entrypoint,
        depends_on=[],
        writes=[source_run],
    )
    nodes: list[dict[str, object]] = [
        acquisition,
        _command(
            node_id="build-authority-snapshot",
            runtime=runtime,
            lock=lock,
            entrypoint=entrypoint,
            depends_on=["acquire-source"],
            reads=[source_run],
            writes=[authority_dir],
        ),
        {
            "depends_on": ["build-authority-snapshot"],
            "kind": "ARTIFACT",
            "node_id": "authority-snapshot",
            "path": str(authority_path),
            "sha256": _sha(authority_bytes),
            "size_bytes": len(authority_bytes),
        },
    ]
    previous_node = "authority-snapshot"
    previous_output = authority_path
    for role in STAGES:
        command_id = f"build-{role}"
        output = report_output if role == "report" else stage_outputs[role]
        nodes.append(
            _command(
                node_id=command_id,
                runtime=runtime,
                lock=lock,
                entrypoint=entrypoint,
                depends_on=[previous_node],
                reads=[previous_output],
                writes=[output],
                retry_limit=1 if role == "workbook" else 0,
            )
        )
        previous_node = command_id
        previous_output = output
        if role == "report":
            manifest_bytes = proposed.to_json_bytes()
            nodes.append(
                {
                    "depends_on": [command_id],
                    "files": [
                        {
                            "relative_path": "generation.json",
                            "sha256": _sha(manifest_bytes),
                            "size_bytes": len(manifest_bytes),
                        }
                    ],
                    "generation_id": proposed.generation_id,
                    "kind": "GENERATION",
                    "manifest_sha256": _sha(manifest_bytes),
                    "node_id": "report-generation",
                    "release_target": True,
                    "root_id": "reporting",
                    "source_dir": str(report_output),
                }
            )
    nodes.append(
        _command(
            node_id="post-build-semantic-reconciliation",
            runtime=runtime,
            lock=lock,
            entrypoint=entrypoint,
            depends_on=["build-panel", "report-generation"],
            reads=[authority_path, report_output, stage_outputs["panel"]],
            writes=[semantic_path],
        )
    )

    contract = {
        "authority_snapshot": {
            "artifact_node": "authority-snapshot",
            "command_node": "build-authority-snapshot",
        },
        "command_sources": [
            {
                "entrypoint_path": str(entrypoint),
                "entrypoint_sha256": _sha(entrypoint_bytes),
                "node_id": str(row["node_id"]),
            }
            for row in nodes
            if row["kind"] == "COMMAND"
        ],
        "profile": "PORTFOLIO_SOURCE_TO_PANEL_V1",
        "release_commit": release_commit,
        "runtime": {
            "environment_sha256": runtime_environment_sha256(runtime),
            "executable_path": str(runtime),
            "executable_sha256": _sha(runtime_bytes),
            "lock_path": str(lock),
            "lock_sha256": _sha(lock_bytes),
            "repo_root": str(lock.parent),
        },
        "schema_version": 1,
        "semantic_reconciliation_node": "post-build-semantic-reconciliation",
        "source_acquisitions": [
            {"adapter_commit": release_commit, "node_id": "acquire-source"}
        ],
        "stages": {
            role: {
                "command_node": f"build-{role}",
                "generation_nodes": ["report-generation"] if role == "report" else [],
            }
            for role in STAGES
        },
    }
    contract_path = tmp_path / "portfolio-orchestration-contract.json"
    contract_bytes = canonical_json_bytes(contract)
    contract_path.write_bytes(contract_bytes)
    raw = {
        "as_of_date": AS_OF.isoformat(),
        "code_commit": release_commit,
        "companions": [],
        "coordination_root": str(tmp_path / "coordination"),
        "created_at": datetime(2026, 8, 3, 12, tzinfo=UTC).isoformat(),
        "gates": [
            {
                "gate_id": "portfolio-orchestration-contract",
                "path": str(contract_path),
                "sha256": _sha(contract_bytes),
            },
            {
                "gate_id": "post-build-semantic",
                "path": str(semantic_path),
                "sha256": _sha(semantic_bytes),
            },
        ],
        "nodes": nodes,
        "pipeline_id": "portfolio-source-to-panel-test",
        "roots": {"reporting": str(target)},
        "schema_version": 1,
        "state_root": str(tmp_path / "state"),
    }
    outputs: dict[str, bytes | Path] = {
        "authority_bytes": authority_bytes,
        "authority_dir": authority_dir,
        "contract_path": contract_path,
        "entrypoint": entrypoint,
        "lock": lock,
        "report_output": report_output,
        "runtime": runtime,
        "semantic_bytes": semantic_bytes,
        "semantic_path": semantic_path,
        "source_run": source_run,
        "target": target,
    }
    outputs.update({f"stage_{key}": value for key, value in stage_outputs.items()})
    if financial_mvp_ready is not None:
        _attach_required_not_ready_product(
            raw, outputs, tmp_path, ready=financial_mvp_ready
        )
    return raw, outputs, proposed


def _runner(outputs: dict[str, bytes | Path], calls: list[str]):
    def run(node: object) -> tuple[int, bytes, bytes]:
        node_id = str(getattr(node, "node_id"))
        calls.append(node_id)
        writes = tuple(getattr(node, "declared_writes"))
        if node_id == "build-authority-snapshot":
            authority_dir = Path(outputs["authority_dir"])
            authority_dir.mkdir(parents=True)
            (authority_dir / "authority-snapshot.json").write_bytes(
                bytes(outputs["authority_bytes"])
            )
        elif node_id == "build-report":
            report_output = Path(outputs["report_output"])
            report_output.mkdir(parents=True)
            proposed = _manifest("proposed")
            (report_output / "generation.json").write_bytes(proposed.to_json_bytes())
        elif node_id == "post-build-semantic-reconciliation":
            Path(outputs["semantic_path"]).write_bytes(bytes(outputs["semantic_bytes"]))
        else:
            for write in writes:
                write.mkdir(parents=True)
                (write / "result.txt").write_text(f"{node_id}\n", encoding="utf-8")
        return 0, b"ok", b""

    return run


def _attach_required_not_ready_product(
    raw: dict[str, object],
    outputs: dict[str, bytes | Path],
    tmp_path: Path,
    *,
    ready: bool = False,
) -> Path:
    product = financial_fixture.build_financial_mvp_fixture(
        tmp_path / "product", ready=ready
    )
    contract = product.contract
    contract_bytes = product.contract_path.read_bytes()
    semantic_sha = _sha(contract_bytes)
    census_sha = _sha(canonical_json_bytes(contract["current_census"]))
    admission = materializer._provider_admission_authority(
        product.generation,
        contract,
        contract_raw_sha256=semantic_sha,
        contract_semantic_sha256=semantic_sha,
        census_sha256=census_sha,
    )
    correction_path = None
    if ready:
        correction_payload = {
            "census_sha256": census_sha,
            "contract_semantic_sha256": semantic_sha,
            "corrections": [],
            "schema_name": "portfolio-correction-registry",
            "schema_version": 1,
        }
        correction_payload["registry_sha256"] = _sha(
            canonical_json_bytes(correction_payload)
        )
        correction_path = tmp_path / "portfolio-correction-registry.json"
        correction_path.write_bytes(canonical_json_bytes(correction_payload))
    correction_registry = materializer._authority_registry(
        correction_path,
        schema_name="portfolio-correction-registry",
        record_key="corrections",
    )
    rederive_registry = materializer._authority_registry(
        None,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    conformance = materializer._product_conformance(
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
        contract_raw_sha256=semantic_sha,
        contract_semantic_sha256=semantic_sha,
        census_sha256=census_sha,
        source_matrix_sha256=_sha(product.matrix_bytes),
        invested_capital_policy_sha256=_sha(
            canonical_json_bytes({"policy_id": "SYNTHETIC_INVESTED_CAPITAL"})
        ),
    )
    authority = {
        "contract": materializer._file_definition(product.contract_path),
        "conformance": conformance,
        "correction_registry": correction_registry,
        "provider_generation": {
            "catalog": materializer._file_definition(
                product.generation / "provider-ledger.json"
            ),
            "generation_id": product.generation.name,
            "manifest": materializer._file_definition(
                product.generation / "generation.json"
            ),
            "receipt": materializer._file_definition(
                product.generation / "admit-or-declare-receipt.json"
            ),
        },
        "rederive_registry": rederive_registry,
        "source_matrix": materializer._file_definition(product.matrix_path),
    }
    authority_path = tmp_path / "financial-mvp-product-conformance.json"
    gates = raw["gates"]
    assert isinstance(gates, list)
    gates.extend(materializer._product_gate_rows(authority, authority_path))
    orchestration_path = Path(outputs["contract_path"])
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    orchestration["product_authority"] = materializer._product_gate_payload(authority)
    orchestration["schema_version"] = 2
    orchestration_bytes = canonical_json_bytes(orchestration)
    orchestration_path.write_bytes(orchestration_bytes)
    orchestration_gate = next(
        row for row in gates if row["gate_id"] == "portfolio-orchestration-contract"
    )
    orchestration_gate["sha256"] = _sha(orchestration_bytes)
    semantic = json.loads(bytes(outputs["semantic_bytes"]))
    semantic["checks"]["product_conformance"] = "PASS"
    semantic["schema_version"] = 2
    semantic_bytes = canonical_json_bytes(semantic)
    outputs["semantic_bytes"] = semantic_bytes
    next(row for row in gates if row["gate_id"] == "post-build-semantic")["sha256"] = (
        _sha(semantic_bytes)
    )
    return product.generation


def test_portfolio_pipeline_builds_and_resumes_from_command_receipts(
    tmp_path: Path,
) -> None:
    raw, outputs, proposed = _portfolio_spec(tmp_path)
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())
    calls: list[str] = []

    plan = plan_pipeline(spec)
    first = execute_pipeline(spec, command_runner=_runner(outputs, calls))
    second = execute_pipeline(spec, command_runner=_runner(outputs, calls))

    assert plan["node_count"] == len(raw["nodes"])
    assert first["release_state"] == "PREPARED"
    assert second["release_id"] == first["release_id"]
    assert len(calls) == 10
    assert (Path(outputs["target"]) / "generations" / proposed.generation_id).is_dir()


def test_legacy_portfolio_spec_is_readable_only_for_status_and_recovery(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=None)
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())

    status = pipeline_status(spec)
    with pytest.raises(PipelineValidationError, match="recovery-only"):
        plan_pipeline(spec)
    with pytest.raises(PipelineValidationError, match="recovery-only"):
        execute_pipeline(spec, command_runner=_runner(outputs, []))

    assert "financial_mvp" not in status


def test_required_not_ready_financial_mvp_blocks_before_every_effect(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=False)
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())
    calls: list[str] = []

    plan = plan_pipeline(spec)
    status = pipeline_status(spec)
    with pytest.raises(PipelineExecutionError, match="required-not-ready"):
        execute_pipeline(spec, command_runner=_runner(outputs, calls))

    assert plan["node_count"] == 0
    assert plan["commit_effects"] == []
    assert plan["financial_mvp"]["ready_for_execution"] is False
    assert plan["financial_mvp"]["candidate_artifacts"]
    assert len(plan["financial_mvp"]["data_class_dispositions"]) == 7
    assert {
        row["provider_id"] for row in plan["financial_mvp"]["data_class_dispositions"]
    } == {
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    }
    assert status["financial_mvp"] == plan["financial_mvp"]
    assert str(tmp_path) not in json.dumps(plan, sort_keys=True)
    assert calls == []
    assert not Path(raw["coordination_root"]).exists()


def test_financial_mvp_contract_drift_rejects_coherent_gate_rehash(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=None)
    _attach_required_not_ready_product(raw, outputs, tmp_path)
    gates = raw["gates"]
    assert isinstance(gates, list)
    contract_gate = next(
        row for row in gates if row["gate_id"] == "financial-mvp-contract-authority"
    )
    contract_path = Path(contract_gate["path"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["product"]["product_version"] = "forged-v2"
    changed = canonical_json_bytes(contract)
    contract_path.write_bytes(changed)
    contract_gate["sha256"] = _sha(changed)

    with pytest.raises(
        PipelineValidationError, match="authority file identities differ"
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_financial_mvp_rejects_admission_receipt_detached_from_manifest(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=None)
    generation = _attach_required_not_ready_product(raw, outputs, tmp_path)
    changed = canonical_json_bytes({"admission_gate_status": "PASS"})
    receipt_path = generation / "admit-or-declare-receipt.json"
    receipt_path.write_bytes(changed)
    gates = raw["gates"]
    assert isinstance(gates, list)
    receipt_gate = next(
        row
        for row in gates
        if row["gate_id"] == "financial-mvp-provider-admission-authority"
    )
    receipt_gate["sha256"] = _sha(changed)
    product_gate = next(
        row for row in gates if row["gate_id"] == "financial-mvp-product-conformance"
    )
    product_path = Path(product_gate["path"])
    authority = json.loads(product_path.read_text(encoding="utf-8"))
    authority["authority_hashes"]["provider_receipt_sha256"] = _sha(changed)
    authority_bytes = canonical_json_bytes(authority)
    product_path.write_bytes(authority_bytes)
    product_gate["sha256"] = _sha(authority_bytes)
    orchestration_path = Path(outputs["contract_path"])
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    orchestration["product_authority"] = authority
    orchestration_bytes = canonical_json_bytes(orchestration)
    orchestration_path.write_bytes(orchestration_bytes)
    next(row for row in gates if row["gate_id"] == "portfolio-orchestration-contract")[
        "sha256"
    ] = _sha(orchestration_bytes)

    with pytest.raises(PipelineValidationError, match="not manifest-bound"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_core_rejects_structurally_invalid_receipt_after_coherent_rehash(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=None)
    generation = _attach_required_not_ready_product(raw, outputs, tmp_path)
    gates = raw["gates"]
    assert isinstance(gates, list)
    by_id = {row["gate_id"]: row for row in gates}
    receipt_path = generation / "admit-or-declare-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del receipt["schema_version"]
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_path.write_bytes(receipt_bytes)
    by_id["financial-mvp-provider-admission-authority"]["sha256"] = _sha(receipt_bytes)

    original = GenerationManifest.from_json_bytes(
        (generation / "generation.json").read_bytes()
    )
    artifacts = tuple(
        SourceArtifact(
            artifact_id=item.artifact_id,
            sha256=_sha(receipt_bytes),
            size_bytes=len(receipt_bytes),
            as_of_date=item.as_of_date,
            schema_name=item.schema_name,
            schema_version=item.schema_version,
        )
        if item.artifact_id == "normalized/admit-or-declare-receipt.json"
        else item
        for item in original.artifacts
    )
    rewritten_manifest = GenerationManifest.build(
        as_of_date=original.as_of_date,
        adapter=original.adapter,
        coverage=original.coverage,
        artifacts=artifacts,
        verification=original.verification,
    )
    manifest_bytes = rewritten_manifest.to_json_bytes()
    (generation / "generation.json").write_bytes(manifest_bytes)
    by_id["financial-mvp-provider-manifest-authority"]["sha256"] = _sha(manifest_bytes)

    contract_path = Path(by_id["financial-mvp-contract-authority"]["path"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_bytes = contract_path.read_bytes()
    semantic_sha = _sha(canonical_json_bytes(contract))
    census_sha = _sha(canonical_json_bytes(contract["current_census"]))
    product_path = Path(by_id["financial-mvp-product-conformance"]["path"])
    product_gate = json.loads(product_path.read_text(encoding="utf-8"))
    prior_admission = product_gate["conformance"]["provider_admission"]
    admission = {
        **prior_admission,
        "generation_id": rewritten_manifest.generation_id,
        "manifest_sha256": _sha(manifest_bytes),
        "receipt": receipt,
        "receipt_sha256": _sha(receipt_bytes),
    }
    absent_corrections = materializer._authority_registry(
        None,
        schema_name="portfolio-correction-registry",
        record_key="corrections",
    )
    absent_rederive = materializer._authority_registry(
        None,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
    )
    conformance = materializer._product_conformance(
        contract,
        admission,
        absent_corrections,
        absent_rederive,
        candidate_generation_ids={
            "ledger": rewritten_manifest.generation_id,
            "baseline": "b" * 64,
            "report": "c" * 64,
            "panel": "d" * 64,
        },
        contract_raw_sha256=_sha(contract_bytes),
        contract_semantic_sha256=semantic_sha,
        census_sha256=census_sha,
        source_matrix_sha256=product_gate["authority_hashes"]["source_matrix_sha256"],
        invested_capital_policy_sha256=product_gate["conformance"][
            "invested_capital_policy_sha256"
        ],
    )
    product_gate["authority_hashes"]["provider_receipt_sha256"] = _sha(receipt_bytes)
    product_gate["authority_hashes"]["provider_manifest_sha256"] = _sha(manifest_bytes)
    product_gate["conformance"] = conformance
    product_bytes = canonical_json_bytes(product_gate)
    product_path.write_bytes(product_bytes)
    by_id["financial-mvp-product-conformance"]["sha256"] = _sha(product_bytes)
    orchestration_path = Path(outputs["contract_path"])
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    orchestration["product_authority"] = product_gate
    orchestration_bytes = canonical_json_bytes(orchestration)
    orchestration_path.write_bytes(orchestration_bytes)
    by_id["portfolio-orchestration-contract"]["sha256"] = _sha(orchestration_bytes)

    with pytest.raises(
        PipelineValidationError, match="provider admission receipt is invalid"
    ):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_financial_mvp_rejects_coherent_invalidation_route_rewrite(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, financial_mvp_ready=None)
    _attach_required_not_ready_product(raw, outputs, tmp_path)
    gates = raw["gates"]
    assert isinstance(gates, list)
    product_gate = next(
        row for row in gates if row["gate_id"] == "financial-mvp-product-conformance"
    )
    product_path = Path(product_gate["path"])
    authority = json.loads(product_path.read_text(encoding="utf-8"))
    authority["conformance"]["sources"][0]["invalidated_outputs"] = ["PANEL"]
    product_bytes = canonical_json_bytes(authority)
    product_path.write_bytes(product_bytes)
    product_gate["sha256"] = _sha(product_bytes)
    orchestration_path = Path(outputs["contract_path"])
    orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    orchestration["product_authority"] = authority
    orchestration_bytes = canonical_json_bytes(orchestration)
    orchestration_path.write_bytes(orchestration_bytes)
    orchestration_gate = next(
        row for row in gates if row["gate_id"] == "portfolio-orchestration-contract"
    )
    orchestration_gate["sha256"] = _sha(orchestration_bytes)

    with pytest.raises(PipelineValidationError, match="full conformance differs"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_portfolio_pipeline_allows_one_workbook_resume_attempt(
    tmp_path: Path,
) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path)
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())
    calls: list[str] = []
    successful_runner = _runner(outputs, calls)
    workbook_attempts = 0

    def fail_workbook_once(node: object) -> tuple[int, bytes, bytes]:
        nonlocal workbook_attempts
        if str(getattr(node, "node_id")) == "build-workbook":
            workbook_attempts += 1
            if workbook_attempts == 1:
                calls.append("build-workbook")
                return 1, b"", b"desktop handoff required"
        return successful_runner(node)

    with pytest.raises(PipelineExecutionError, match="build-workbook"):
        execute_pipeline(spec, command_runner=fail_workbook_once)
    resumed = execute_pipeline(spec, command_runner=fail_workbook_once)

    assert resumed["release_state"] == "PREPARED"
    assert workbook_attempts == 2
    assert calls.count("build-workbook") == 2


def test_portfolio_pipeline_rejects_a_prebuilt_generation(tmp_path: Path) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path)
    report_output = Path(outputs["report_output"])
    report_output.mkdir(parents=True)
    (report_output / "generation.json").write_bytes(
        _manifest("proposed").to_json_bytes()
    )

    with pytest.raises(PipelineValidationError, match="predates its pipeline receipt"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_portfolio_pipeline_rejects_ambient_current(tmp_path: Path) -> None:
    raw, _, _ = _portfolio_spec(tmp_path)
    nodes = raw["nodes"]
    assert isinstance(nodes, list)
    baseline = next(row for row in nodes if row["node_id"] == "build-baseline")
    reads = baseline["declared_reads"]
    assert isinstance(reads, list)
    reads.append(str(tmp_path / "baseline" / "CURRENT.json"))

    with pytest.raises(PipelineValidationError, match="ambient CURRENT"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_portfolio_pipeline_requires_explicit_stage_inputs(tmp_path: Path) -> None:
    raw, _, _ = _portfolio_spec(tmp_path)
    nodes = raw["nodes"]
    assert isinstance(nodes, list)
    baseline = next(row for row in nodes if row["node_id"] == "build-baseline")
    reads = baseline["declared_reads"]
    assert isinstance(reads, list)
    baseline["declared_reads"] = reads[:1]

    with pytest.raises(PipelineValidationError, match="predecessor output"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_post_build_semantic_failure_blocks_preparation(tmp_path: Path) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path, semantic_verdict="FAIL")
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())
    previous = read_current(Path(outputs["target"])).generation_id

    with pytest.raises(PipelineValidationError, match="semantic gate differs"):
        execute_pipeline(spec, command_runner=_runner(outputs, []))

    assert read_current(Path(outputs["target"])).generation_id == previous
    assert not (Path(raw["coordination_root"]) / "CURRENT_RELEASE.json").exists()


def test_runtime_drift_blocks_before_the_first_command(tmp_path: Path) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path)
    Path(outputs["lock"]).write_bytes(b"drifted lock\n")
    calls: list[str] = []

    with pytest.raises(PipelineValidationError, match="runtime lock differs"):
        execute_pipeline(
            PipelineSpec.from_json_bytes(json.dumps(raw).encode()),
            command_runner=_runner(outputs, calls),
        )

    assert calls == []


def test_dirty_repository_blocks_before_the_first_command(tmp_path: Path) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path)
    Path(outputs["entrypoint"]).with_name("untracked.py").write_text(
        "print('untracked')\n", encoding="utf-8"
    )

    with pytest.raises(PipelineValidationError, match="repository is not clean"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_entrypoint_hash_is_verified_from_the_repository(tmp_path: Path) -> None:
    raw, outputs, _ = _portfolio_spec(tmp_path)
    contract_path = Path(outputs["contract_path"])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["command_sources"][0]["entrypoint_sha256"] = "0" * 64
    contract_bytes = canonical_json_bytes(contract)
    contract_path.write_bytes(contract_bytes)
    gates = raw["gates"]
    assert isinstance(gates, list)
    gate = next(
        row for row in gates if row["gate_id"] == "portfolio-orchestration-contract"
    )
    gate["sha256"] = _sha(contract_bytes)

    with pytest.raises(PipelineValidationError, match="entrypoint differs"):
        plan_pipeline(PipelineSpec.from_json_bytes(json.dumps(raw).encode()))


def test_failed_source_acquisition_is_not_retried(tmp_path: Path) -> None:
    raw, _, _ = _portfolio_spec(tmp_path)
    spec = PipelineSpec.from_json_bytes(json.dumps(raw).encode())
    calls = 0

    def fail(_node: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        return 1, b"", b"failed"

    with pytest.raises(PipelineExecutionError, match="acquire-source"):
        execute_pipeline(spec, command_runner=fail)
    with pytest.raises(PipelineExecutionError, match="attempt limit"):
        execute_pipeline(spec, command_runner=fail)

    assert calls == 1

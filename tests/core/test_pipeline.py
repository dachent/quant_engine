from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    FreshnessMode,
    FreshnessPolicy,
    GenerationManifest,
    PipelineExecutionError,
    PipelineSpec,
    PipelineUnknownOutcomeError,
    PipelineValidationError,
    SourceArtifact,
    VerificationStatus,
    execute_pipeline,
    inventory_generation,
    plan_pipeline,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


AS_OF = date(2026, 8, 2)
COMMIT = "d" * 40


def _manifest(label: str) -> GenerationManifest:
    content = (label + "\n").encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("pipeline-test", "1"),
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


def _file(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "relative_path": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _spec_dict(
    tmp_path: Path,
    *,
    command: dict[str, object] | None = None,
) -> tuple[dict[str, object], GenerationManifest, GenerationManifest, Path]:
    target = tmp_path / "target"
    source = tmp_path / "source"
    previous = _manifest("previous")
    proposed = _manifest("proposed")
    stage_generation(target, previous)
    publish_generation(target, previous.generation_id)
    manifest_path = stage_generation(source, proposed)
    evidence = tmp_path / "source-evidence.txt"
    evidence.write_bytes(b"accepted source\n")
    gate = tmp_path / "gate.json"
    gate.write_bytes(b'{"status":"PASS"}\n')
    launcher = tmp_path / "launcher.html"
    launcher.write_bytes(b"new launcher\n")
    nodes: list[dict[str, object]] = []
    if command is not None:
        nodes.append(command)
    nodes.extend(
        [
            {
                "depends_on": ([str(command["node_id"])] if command else []),
                "kind": "ARTIFACT",
                "node_id": "source-evidence",
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "size_bytes": evidence.stat().st_size,
            },
            {
                "depends_on": ["source-evidence"],
                "files": [_file(manifest_path)],
                "generation_id": proposed.generation_id,
                "kind": "GENERATION",
                "manifest_sha256": hashlib.sha256(proposed.to_json_bytes()).hexdigest(),
                "node_id": "portfolio-report",
                "release_target": True,
                "root_id": "reporting",
                "source_dir": str(manifest_path.parent),
            },
        ]
    )
    spec = {
        "as_of_date": AS_OF.isoformat(),
        "code_commit": COMMIT,
        "companions": [
            {
                "companion_id": "stable-launcher",
                "relative_path": "index.html",
                "root_id": "reporting",
                "sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "source_path": str(launcher),
            }
        ],
        "coordination_root": str(tmp_path / "coordination"),
        "created_at": datetime(2026, 8, 2, 12, tzinfo=UTC).isoformat(),
        "gates": [
            {
                "gate_id": "offline-tests",
                "path": str(gate),
                "sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
            }
        ],
        "nodes": nodes,
        "pipeline_id": "test-rollforward",
        "roots": {"reporting": str(target)},
        "schema_version": 1,
        "state_root": str(tmp_path / "state"),
    }
    return spec, previous, proposed, target


def _parse(value: dict[str, object]) -> PipelineSpec:
    return PipelineSpec.from_json_bytes(json.dumps(value).encode())


def _freshness_gate(path: Path, evaluated_on: date) -> Path:
    policy = FreshnessPolicy(
        policy_id="daily-current-strategy",
        mode=FreshnessMode.RECURRING,
        expected_interval_days=1,
        grace_days=2,
        stale_after_days=5,
    )
    assessment = policy.assess(date(2026, 7, 28), evaluated_on).to_dict()
    assessment.update(
        {
            "fact_type": "strategy-returns",
            "provider_id": "synthetic-provider",
            "subject_id": "synthetic-strategy",
        }
    )
    path.write_bytes(
        canonical_json_bytes(
            {
                "assessment_schema_version": 2,
                "as_of_date": evaluated_on.isoformat(),
                "assessments": [assessment],
                "policy": policy.to_dict(),
            }
        )
    )
    return path


def _freshness_gate_with_optional_gap(path: Path, evaluated_on: date) -> Path:
    policy = FreshnessPolicy(
        policy_id="daily-current-strategy",
        mode=FreshnessMode.RECURRING,
        expected_interval_days=1,
        grace_days=2,
        stale_after_days=5,
    )
    required = policy.assess(date(2026, 7, 28), evaluated_on).to_dict()
    required.update(
        {
            "fact_type": "strategy-returns",
            "provider_id": "synthetic-provider",
            "required_for_current": True,
            "subject_id": "required-strategy",
        }
    )
    optional = policy.assess(None, evaluated_on).to_dict()
    optional.update(
        {
            "blocks_current_consumers": False,
            "escalation_required": False,
            "fact_type": "costs",
            "provider_id": "synthetic-provider",
            "required_for_current": False,
            "subject_id": "optional-costs",
        }
    )
    path.write_bytes(
        canonical_json_bytes(
            {
                "assessment_schema_version": 3,
                "as_of_date": evaluated_on.isoformat(),
                "assessments": [required, optional],
                "policies": [policy.to_dict()],
            }
        )
    )
    return path


def test_plan_is_exact_and_non_mutating(tmp_path: Path) -> None:
    raw, previous, proposed, target = _spec_dict(tmp_path)
    spec = _parse(raw)

    plan = plan_pipeline(spec)

    assert plan["node_count"] == 2
    assert plan["commit_effects"] == [
        {
            "generation_id": proposed.generation_id,
            "root_id": "reporting",
            "type": "CURRENT_POINTER",
        },
        {
            "companion_id": "stable-launcher",
            "relative_path": "index.html",
            "root_id": "reporting",
            "type": "COMPANION",
        },
    ]
    assert read_current(target).generation_id == previous.generation_id
    assert not spec.state_root.exists()
    assert not (target / "generations" / proposed.generation_id).exists()


def test_execute_prepares_without_pointer_change_then_commits_atomically(
    tmp_path: Path,
) -> None:
    raw, previous, proposed, target = _spec_dict(tmp_path)
    spec = _parse(raw)

    prepared = execute_pipeline(spec)

    assert prepared["release_state"] == "PREPARED"
    assert read_current(target).generation_id == previous.generation_id
    assert (target / "generations" / proposed.generation_id).is_dir()
    assert not (target / "index.html").exists()

    committed = execute_pipeline(spec, commit=True)

    assert committed["release_id"] == prepared["release_id"]
    assert committed["release_state"] == "COMMITTED"
    assert read_current(target).generation_id == proposed.generation_id
    assert (target / "index.html").read_bytes() == b"new launcher\n"

    resumed = execute_pipeline(spec)
    assert resumed["release_state"] == "COMMITTED"


def test_freshness_gate_is_reaged_on_prepare_and_commit(tmp_path: Path) -> None:
    raw, previous, _, target = _spec_dict(tmp_path)
    gate = _freshness_gate(tmp_path / "freshness.json", AS_OF)
    raw["gates"].append(
        {
            "gate_id": "freshness-health",
            "path": str(gate),
            "sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        }
    )
    spec = _parse(raw)

    prepared = execute_pipeline(spec, evaluation_date=AS_OF)

    assert prepared["release_state"] == "PREPARED"
    with pytest.raises(PipelineValidationError, match="operational date"):
        execute_pipeline(spec, commit=True, evaluation_date=date(2026, 8, 3))
    assert read_current(target).generation_id == previous.generation_id


def test_freshness_gate_rejects_forged_current_state(tmp_path: Path) -> None:
    raw, previous, _, target = _spec_dict(tmp_path)
    gate = _freshness_gate(tmp_path / "freshness.json", AS_OF)
    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload["assessments"][0]["state"] = "FRESH"
    gate.write_bytes(canonical_json_bytes(payload))
    raw["gates"].append(
        {
            "gate_id": "freshness-health",
            "path": str(gate),
            "sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        }
    )

    with pytest.raises(PipelineValidationError, match="assessment is stale"):
        execute_pipeline(_parse(raw), evaluation_date=AS_OF)
    assert read_current(target).generation_id == previous.generation_id


def test_freshness_gate_preserves_optional_nonblocking_gaps(tmp_path: Path) -> None:
    raw, previous, _, target = _spec_dict(tmp_path)
    gate = _freshness_gate_with_optional_gap(tmp_path / "freshness.json", AS_OF)
    raw["gates"].append(
        {
            "gate_id": "freshness-health",
            "path": str(gate),
            "sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        }
    )

    prepared = execute_pipeline(_parse(raw), evaluation_date=AS_OF)

    assert prepared["release_state"] == "PREPARED"
    assert read_current(target).generation_id == previous.generation_id


def test_generation_inventory_is_exact(tmp_path: Path) -> None:
    raw, _, proposed, _ = _spec_dict(tmp_path)
    generation = Path(raw["nodes"][-1]["source_dir"])

    result = inventory_generation(generation)

    assert result["generation_id"] == proposed.generation_id
    assert result["files"] == raw["nodes"][-1]["files"]


def test_resume_does_not_repeat_completed_command(tmp_path: Path) -> None:
    command = {
        "argv": ["synthetic", "build"],
        "cwd": str(tmp_path),
        "declared_reads": [],
        "declared_writes": [],
        "depends_on": [],
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": "local-build",
        "request_identity": None,
        "retry_limit": 1,
        "timeout_seconds": 10,
    }
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)
    calls = 0

    def runner(_node: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        return 0, b"ok", b""

    first = execute_pipeline(spec, command_runner=runner)
    second = execute_pipeline(spec, command_runner=runner)

    assert first["release_id"] == second["release_id"]
    assert calls == 1


def test_unknown_external_read_is_never_retried(tmp_path: Path) -> None:
    command = {
        "argv": ["synthetic", "external-read"],
        "cwd": str(tmp_path),
        "declared_reads": [],
        "declared_writes": [],
        "depends_on": [],
        "effect": "READ_ONLY_EXTERNAL",
        "kind": "COMMAND",
        "node_id": "provider-read",
        "request_identity": "e" * 64,
        "retry_limit": 0,
        "timeout_seconds": 10,
    }
    raw, previous, proposed, target = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)
    calls = 0

    def unknown(_node: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        raise TimeoutError("unknown provider outcome")

    with pytest.raises(PipelineUnknownOutcomeError):
        execute_pipeline(spec, allow_external_read=True, command_runner=unknown)
    with pytest.raises(PipelineUnknownOutcomeError):
        execute_pipeline(
            spec,
            allow_external_read=True,
            command_runner=lambda _node: (0, b"must not run", b""),
        )

    assert calls == 1
    assert read_current(target).generation_id == previous.generation_id
    assert not (target / "generations" / proposed.generation_id).exists()


def test_external_read_requires_explicit_execution_authority(tmp_path: Path) -> None:
    command = {
        "argv": ["synthetic", "external-read"],
        "cwd": str(tmp_path),
        "declared_reads": [],
        "declared_writes": [],
        "depends_on": [],
        "effect": "READ_ONLY_EXTERNAL",
        "kind": "COMMAND",
        "node_id": "provider-read",
        "request_identity": "e" * 64,
        "retry_limit": 0,
        "timeout_seconds": 10,
    }
    raw, previous, _, target = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)

    with pytest.raises(PipelineExecutionError, match="explicit external-read"):
        execute_pipeline(spec)

    assert read_current(target).generation_id == previous.generation_id


def test_dependency_cycle_fails_before_any_write(tmp_path: Path) -> None:
    raw, _, _, _ = _spec_dict(tmp_path)
    nodes = raw["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["depends_on"] = ["portfolio-report"]

    with pytest.raises(PipelineValidationError, match="cycle"):
        _parse(raw)

    assert not (tmp_path / "state").exists()


def test_receipt_path_is_bounded_for_windows_workspace(tmp_path: Path) -> None:
    raw, _, _, _ = _spec_dict(tmp_path)
    padding = "x" * max(1, 110 - len(str(tmp_path)) - 1)
    long_root = tmp_path / padding
    raw["state_root"] = str(long_root)
    spec = _parse(raw)

    result = execute_pipeline(spec)

    receipts = list(long_root.rglob("*.json"))
    assert result["release_state"] == "PREPARED"
    assert any(path.parent.name == "receipts" for path in receipts)
    assert max(len(str(path)) for path in receipts) < 240


def test_command_receipt_retains_hashed_reproducibility_evidence_without_secrets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "declared-input.txt"
    output = tmp_path / "declared-output.txt"
    source.write_bytes(b"input\n")
    command = {
        "argv": ["synthetic", "--token", "super-secret-value", "build"],
        "cwd": str(tmp_path),
        "declared_reads": [str(source)],
        "declared_writes": [str(output)],
        "depends_on": [],
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": "local-build",
        "request_identity": None,
        "retry_limit": 0,
        "timeout_seconds": 10,
    }
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)

    def runner(_node: object) -> tuple[int, bytes, bytes]:
        output.write_bytes(b"output\n")
        return 0, b"stdout evidence", b"stderr evidence"

    execute_pipeline(spec, command_runner=runner)

    receipt_path = next(
        path
        for path in spec.state_root.rglob("local-build.json")
        if path.parent.name == "receipts"
    )
    raw_receipt = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(raw_receipt)
    evidence = receipt["command_evidence"]
    attempt = evidence["attempts"][-1]
    assert "super-secret-value" not in raw_receipt
    assert evidence["cwd"] == str(tmp_path.resolve())
    assert (
        evidence["argv_sha256"]
        == hashlib.sha256(canonical_json_bytes({"argv": command["argv"]})).hexdigest()
    )
    assert attempt["return_code"] == 0
    assert attempt["stdout_sha256"] == hashlib.sha256(b"stdout evidence").hexdigest()
    assert attempt["stderr_sha256"] == hashlib.sha256(b"stderr evidence").hexdigest()
    assert attempt["pre_io"]["declared_writes"][0]["state"] == "MISSING"
    assert (
        attempt["post_io"]["declared_writes"][0]["sha256"]
        == hashlib.sha256(b"output\n").hexdigest()
    )
    assert evidence["tool_identity"]["executable_name"] == "synthetic"


def test_completed_command_receipt_rejects_declared_output_drift(
    tmp_path: Path,
) -> None:
    output = tmp_path / "declared-output.txt"
    command = {
        "argv": ["synthetic", "build"],
        "cwd": str(tmp_path),
        "declared_reads": [],
        "declared_writes": [str(output)],
        "depends_on": [],
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": "local-build",
        "request_identity": None,
        "retry_limit": 0,
        "timeout_seconds": 10,
    }
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)

    def runner(_node: object) -> tuple[int, bytes, bytes]:
        output.write_bytes(b"accepted\n")
        return 0, b"", b""

    execute_pipeline(spec, command_runner=runner)
    output.write_bytes(b"drifted\n")

    with pytest.raises(PipelineValidationError, match="command outputs drifted"):
        execute_pipeline(spec, command_runner=runner)

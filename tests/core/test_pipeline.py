from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path
import sys

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
    ReleasePublicationUnavailableError,
    SourceArtifact,
    VerificationStatus,
    execute_pipeline,
    generation_inventory_sha256,
    inventory_generation,
    plan_pipeline,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core import release as release_module


AS_OF = date(2026, 8, 2)
COMMIT = "d" * 40


@pytest.fixture(autouse=True)
def _skip_posix_pipeline_behavior_on_windows(
    request: pytest.FixtureRequest
) -> None:
    if (
        os.name == "nt"
        and "windows" not in request.node.name
        and "default_runner_child" not in request.node.name
    ):
        pytest.skip("POSIX coordinated pipeline behavior is verified on POSIX")


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


def _provider_ledger_generation(
    tmp_path: Path,
) -> tuple[Path, GenerationManifest, dict[str, object]]:
    root = tmp_path / "provider-ledger"
    source = tmp_path / "provider-ledger.json"
    source.write_bytes(canonical_json_bytes({"reconciliation_checks": []}))
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("portfolio-facts",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact.from_path(
                source,
                artifact_id="normalized/provider-ledger.json",
                as_of_date=AS_OF,
                schema_name="provider-ledger-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    catalog_path = manifest_path.parent / "provider-ledger.json"
    catalog_path.write_bytes(source.read_bytes())
    publish_generation(root, manifest.generation_id)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return (
        root,
        manifest,
        {
            "as_of_date": manifest.as_of_date.isoformat(),
            "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
            "catalog_size_bytes": catalog_path.stat().st_size,
            "generation_id": manifest.generation_id,
            "manifest_sha256": manifest_sha256,
            "manifest_size_bytes": manifest_path.stat().st_size,
        },
    )


def _bound_freshness_spec(
    tmp_path: Path,
) -> tuple[dict[str, object], GenerationManifest, Path]:
    raw, previous, _, target = _spec_dict(tmp_path)
    ledger_root, ledger, identity = _provider_ledger_generation(tmp_path)
    inventory = inventory_generation(ledger_root / "generations" / ledger.generation_id)
    raw["roots"]["provider-ledger"] = str(ledger_root)
    raw["nodes"].append(
        {
            "depends_on": [],
            "files": inventory["files"],
            "generation_id": ledger.generation_id,
            "kind": "GENERATION",
            "manifest_sha256": inventory["manifest_sha256"],
            "node_id": "provider-ledger",
            "release_target": True,
            "root_id": "provider-ledger",
            "source_dir": None,
        }
    )
    gate = _freshness_gate_with_optional_gap(tmp_path / "freshness.json", AS_OF)
    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload["provider_ledger"] = identity
    gate.write_bytes(canonical_json_bytes(payload))
    raw["gates"].append(
        {
            "gate_id": "freshness-health",
            "path": str(gate),
            "sha256": hashlib.sha256(gate.read_bytes()).hexdigest(),
        }
    )
    return raw, previous, target


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


def test_pipeline_repairs_only_the_exact_bound_predecessor_inventory_drift(
    tmp_path: Path,
) -> None:
    raw, _, accepted, target = _spec_dict(tmp_path)
    first = _parse(raw)
    execute_pipeline(first, commit=True)
    drift = target / "generations" / accepted.generation_id / "drift.bin"
    drift.write_bytes(b"known predecessor drift\n")

    replacement_source = tmp_path / "replacement-source"
    replacement = _manifest("replacement")
    replacement_manifest = stage_generation(replacement_source, replacement)
    replacement_raw = json.loads(json.dumps(raw))
    replacement_raw["pipeline_id"] = "test-repair-rollforward"
    replacement_raw["created_at"] = datetime(2026, 8, 2, 13, tzinfo=UTC).isoformat()
    replacement_raw["state_root"] = str(tmp_path / "repair-state")
    generation_node = replacement_raw["nodes"][-1]
    generation_node.update(
        {
            "files": [_file(replacement_manifest)],
            "generation_id": replacement.generation_id,
            "manifest_sha256": hashlib.sha256(replacement.to_json_bytes()).hexdigest(),
            "source_dir": str(replacement_manifest.parent),
        }
    )
    authority_path = Path(replacement_raw["coordination_root"]) / (
        "CURRENT_RELEASE.json"
    )
    authority_bytes = authority_path.read_bytes()
    authority = json.loads(authority_bytes)
    release_path = (
        authority_path.parent / "releases" / authority["release_id"] / "release.json"
    )
    predecessor = json.loads(release_path.read_bytes())
    predecessor_target = predecessor["targets"][0]
    repair_gate = tmp_path / "predecessor-repair.json"
    repair_gate.write_bytes(
        canonical_json_bytes(
            {
                "authority_sha256": hashlib.sha256(authority_bytes).hexdigest(),
                "disposition": "SUPERSEDE_WITH_VERIFIED_GENERATIONS",
                "expected_inventory_drifts": [
                    {
                        "generation_id": accepted.generation_id,
                        "observed_inventory_sha256": generation_inventory_sha256(
                            target, accepted.generation_id
                        ),
                        "recorded_inventory_sha256": predecessor_target[
                            "proposed_inventory_sha256"
                        ],
                        "root_id": "reporting",
                    }
                ],
                "release_id": authority["release_id"],
                "release_manifest_sha256": authority["release_manifest_sha256"],
                "schema_version": 1,
            }
        )
    )
    replacement_raw["gates"].append(
        {
            "gate_id": "predecessor-authority-repair",
            "path": str(repair_gate),
            "sha256": hashlib.sha256(repair_gate.read_bytes()).hexdigest(),
        }
    )
    wrong_gate = tmp_path / "wrong-predecessor-repair.json"
    wrong_payload = json.loads(repair_gate.read_bytes())
    wrong_payload["expected_inventory_drifts"][0]["observed_inventory_sha256"] = (
        "f" * 64
    )
    wrong_gate.write_bytes(canonical_json_bytes(wrong_payload))
    wrong_raw = json.loads(json.dumps(replacement_raw))
    wrong_raw["pipeline_id"] = "test-wrong-repair-rollforward"
    wrong_raw["state_root"] = str(tmp_path / "wrong-repair-state")
    wrong_raw["gates"][-1].update(
        {
            "path": str(wrong_gate),
            "sha256": hashlib.sha256(wrong_gate.read_bytes()).hexdigest(),
        }
    )
    with pytest.raises(PipelineValidationError, match="drift set differs"):
        execute_pipeline(_parse(wrong_raw))
    reuse_raw = json.loads(json.dumps(replacement_raw))
    reuse_raw["pipeline_id"] = "test-reused-repair-rollforward"
    reuse_raw["state_root"] = str(tmp_path / "reused-repair-state")
    reused_inventory = inventory_generation(
        target / "generations" / accepted.generation_id
    )
    reuse_raw["nodes"][-1].update(
        {
            "files": reused_inventory["files"],
            "generation_id": accepted.generation_id,
            "manifest_sha256": reused_inventory["manifest_sha256"],
            "source_dir": None,
        }
    )
    with pytest.raises(PipelineValidationError, match="reuses drifted generation"):
        execute_pipeline(_parse(reuse_raw))
    repair = _parse(replacement_raw)

    prepared = execute_pipeline(repair)

    assert prepared["release_state"] == "PREPARED"
    assert json.loads((target / "CURRENT.json").read_bytes())["generation_id"] == (
        accepted.generation_id
    )
    committed = execute_pipeline(repair, commit=True)
    assert committed["release_state"] == "COMMITTED"
    assert read_current(target).generation_id == replacement.generation_id
    assert execute_pipeline(repair)["release_state"] == "COMMITTED"


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


def test_freshness_gate_accepts_release_bound_provider_ledger(tmp_path: Path) -> None:
    raw, previous, target = _bound_freshness_spec(tmp_path)

    prepared = execute_pipeline(_parse(raw), evaluation_date=AS_OF)

    assert prepared["release_state"] == "PREPARED"
    assert read_current(target).generation_id == previous.generation_id


def test_freshness_gate_rejects_different_provider_ledger_identity(
    tmp_path: Path,
) -> None:
    raw, previous, target = _bound_freshness_spec(tmp_path)
    gate = Path(raw["gates"][-1]["path"])
    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload["provider_ledger"]["catalog_sha256"] = "f" * 64
    gate.write_bytes(canonical_json_bytes(payload))
    raw["gates"][-1]["sha256"] = hashlib.sha256(gate.read_bytes()).hexdigest()

    with pytest.raises(PipelineValidationError, match="identity differs"):
        execute_pipeline(_parse(raw), evaluation_date=AS_OF)
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


def test_local_retry_requires_an_explicit_resume(tmp_path: Path) -> None:
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

    def fail_once(_node: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        return (1, b"", b"failed") if calls == 1 else (0, b"ok", b"")

    with pytest.raises(PipelineExecutionError, match="local-build"):
        execute_pipeline(spec, command_runner=fail_once)
    assert calls == 1

    resumed = execute_pipeline(spec, command_runner=fail_once)

    assert resumed["release_state"] == "PREPARED"
    assert calls == 2


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


def test_external_read_persists_unknown_before_spawn_and_never_replays(
    tmp_path: Path,
) -> None:
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
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)
    calls = 0

    def terminate_after_spawn(_node: object) -> tuple[int, bytes, bytes]:
        nonlocal calls
        calls += 1
        receipt_path = next(
            path
            for path in spec.state_root.rglob("provider-read.json")
            if path.parent.name == "receipts"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["state"] == "UNKNOWN"
        assert receipt["attempts"] == 1
        assert receipt["command_evidence"]["attempts"][-1]["outcome"] == "IN_FLIGHT"
        raise KeyboardInterrupt("synthetic abrupt termination")

    with pytest.raises(KeyboardInterrupt, match="abrupt termination"):
        execute_pipeline(
            spec,
            allow_external_read=True,
            command_runner=terminate_after_spawn,
        )
    with pytest.raises(PipelineUnknownOutcomeError, match="unknown external-read"):
        execute_pipeline(
            spec,
            allow_external_read=True,
            command_runner=lambda _node: (0, b"must not run", b""),
        )

    assert calls == 1


def test_default_runner_child_observes_unknown_in_flight_receipt_before_spawn(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    child = (
        "import json;"
        "from pathlib import Path;"
        f"paths=list(Path({str(state_root)!r}).rglob('provider-read.json'));"
        "assert len(paths)==1;"
        "receipt=json.loads(paths[0].read_text(encoding='utf-8'));"
        "assert receipt['state']=='UNKNOWN';"
        "assert receipt['attempts']==1;"
        "assert receipt['command_evidence']['attempts'][-1]['outcome']=='IN_FLIGHT'"
    )
    command = {
        "argv": [sys.executable, "-c", child],
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
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)

    execute_pipeline(spec, allow_external_read=True)

    receipt_path = next(spec.state_root.rglob("provider-read.json"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "COMPLETED"
    assert receipt["command_evidence"]["attempts"][-1]["outcome"] == "COMPLETED"


def test_pipeline_stops_after_prepare_when_windows_publication_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, previous, proposed, target = _spec_dict(tmp_path)
    spec = _parse(raw)
    prepared = execute_pipeline(spec)
    journal_path = (
        spec.coordination_root / "releases" / str(prepared["release_id"]) / "journal.json"
    )
    before_journal = journal_path.read_bytes()

    monkeypatch.setattr(release_module, "_is_windows_platform", lambda: True)
    with pytest.raises(ReleasePublicationUnavailableError) as raised:
        execute_pipeline(spec, commit=True)

    assert raised.value.operation == "commit_release"
    assert journal_path.read_bytes() == before_journal
    assert read_current(target).generation_id == previous.generation_id
    assert not (target / "index.html").exists()
    assert (target / "generations" / proposed.generation_id).is_dir()


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


def test_default_runner_caps_stream_capture_and_retains_hash_only_evidence(
    tmp_path: Path,
) -> None:
    stdout = b"x" * (128 * 1024)
    stderr = b"y" * (96 * 1024)
    command = {
        "argv": [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.buffer.write(b'x'*(128*1024));"
                "sys.stderr.buffer.write(b'y'*(96*1024))"
            ),
        ],
        "cwd": str(tmp_path),
        "declared_reads": [],
        "declared_writes": [],
        "depends_on": [],
        "effect": "LOCAL_ONLY",
        "kind": "COMMAND",
        "node_id": "bounded-output",
        "request_identity": None,
        "retry_limit": 0,
        "timeout_seconds": 10,
    }
    raw, _, _, _ = _spec_dict(tmp_path, command=command)
    spec = _parse(raw)

    execute_pipeline(spec)

    receipt_path = next(
        path
        for path in spec.state_root.rglob("bounded-output.json")
        if path.parent.name == "receipts"
    )
    raw_receipt = receipt_path.read_text(encoding="utf-8")
    attempt = json.loads(raw_receipt)["command_evidence"]["attempts"][-1]
    assert attempt["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert attempt["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
    assert attempt["stdout_size_bytes"] == len(stdout)
    assert attempt["stderr_size_bytes"] == len(stderr)
    assert attempt["stdout_captured_bytes"] == 64 * 1024
    assert attempt["stderr_captured_bytes"] == 64 * 1024
    assert attempt["stdout_truncated"] is True
    assert attempt["stderr_truncated"] is True
    assert "x" * 128 not in raw_receipt
    assert "y" * 128 not in raw_receipt


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

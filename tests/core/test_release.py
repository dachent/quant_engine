from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread

from jsonschema import Draft202012Validator
import pytest
from referencing import Registry, Resource

from quant_engine.core import (
    AdapterIdentity,
    CompanionInput,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GateEvidence,
    GenerationManifest,
    PointerIntegrityError,
    ReleaseCommitError,
    ReleaseConflictError,
    ReleaseManifest,
    ReleaseState,
    ReleaseTarget,
    ReleaseValidationError,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    read_current,
    read_release,
    recover_release,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.release import (
    _DirectoryLease,
    _path_identity,
    read_current_release,
)


AS_OF = date(2026, 8, 2)
COMMIT = "b" * 40


def _manifest(label: str) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-release", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=f"synthetic {label}",
            fact_types=("release-fixture",),
            observed_start=AS_OF,
            observed_end=AS_OF,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=AS_OF,
                schema_name="synthetic-release",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _stage_pair(
    root: Path, prefix: str
) -> tuple[GenerationManifest, GenerationManifest]:
    previous = _manifest(f"{prefix}-previous")
    proposed = _manifest(f"{prefix}-proposed")
    stage_generation(root, previous)
    stage_generation(root, proposed)
    publish_generation(root, previous.generation_id)
    return previous, proposed


def _gate() -> tuple[GateEvidence, ...]:
    return (GateEvidence("bounded-tests", hashlib.sha256(b"pass\n").hexdigest()),)


def _legacy_migration_gates() -> tuple[GateEvidence, ...]:
    return (
        *_gate(),
        GateEvidence(
            "predecessor-authority-repair",
            hashlib.sha256(b"legacy inventory reviewed\n").hexdigest(),
        ),
    )


def _link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlink creation unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junction creation unavailable: {result.stderr}")


def test_prepare_is_non_mutating_and_manifest_round_trips(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_previous, first_proposed = _stage_pair(first_root, "first")
    second_previous, second_proposed = _stage_pair(second_root, "second")
    launcher = first_root / "index.html"
    launcher.write_bytes(b"old launcher\n")

    manifest = prepare_release(
        coordination_root=tmp_path / "coordination",
        roots={"first": first_root, "second": second_root},
        proposed_generations={
            "first": first_proposed.generation_id,
            "second": second_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(CompanionInput("panel-launcher", "first", "index.html", b"new\n"),),
    )

    assert read_current(first_root).generation_id == first_previous.generation_id
    assert read_current(second_root).generation_id == second_previous.generation_id
    assert launcher.read_bytes() == b"old launcher\n"
    assert ReleaseManifest.from_json_bytes(manifest.to_json_bytes()) == manifest
    reread, journal = read_release(tmp_path / "coordination", manifest.release_id)
    assert reread == manifest
    assert journal.state is ReleaseState.PREPARED


@pytest.mark.parametrize("mutation", ("null", "missing"))
def test_schema_three_release_rejects_unbound_proposed_inventory(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, f"inventory-{mutation}")
    manifest = prepare_release(
        coordination_root=tmp_path / "coordination",
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    decoded = json.loads(manifest.to_json_bytes())
    if mutation == "null":
        decoded["targets"][0]["proposed_inventory_sha256"] = None
    else:
        del decoded["targets"][0]["proposed_inventory_sha256"]
    identity = {key: value for key, value in decoded.items() if key != "release_id"}
    decoded["release_id"] = hashlib.sha256(
        json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()

    with pytest.raises(
        ReleaseValidationError,
        match="release_target keys differ|proposed inventory.*mandatory",
    ):
        ReleaseManifest.from_json_bytes(
            json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )


def test_release_manifest_schema_has_disjoint_legacy_and_current_versions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "schema-versions")
    manifest = prepare_release(
        coordination_root=tmp_path / "coordination",
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "schemas"
            / "core"
            / "release-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    authority_schema = json.loads(
        (
            Path(__file__).parents[2]
            / "schemas"
            / "core"
            / "release-authority.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(
        schema,
        registry=Registry().with_resource(
            authority_schema["$id"], Resource.from_contents(authority_schema)
        ),
    )
    v3 = json.loads(manifest.to_json_bytes())
    v2 = json.loads(json.dumps(v3))
    v2["release_schema_version"] = 2
    v2["targets"][0].pop("root_identity")
    v2["targets"][0].pop("previous_inventory_sha256")
    v2["companions"] = []
    v1 = json.loads(json.dumps(v2))
    v1["release_schema_version"] = 1
    v1.pop("previous_authority")
    v1["targets"][0].pop("proposed_inventory_sha256")

    for valid in (v1, v2, v3):
        assert not list(validator.iter_errors(valid))

    cross_version = json.loads(json.dumps(v2))
    cross_version["targets"][0]["root_identity"] = "d" * 64
    missing = json.loads(json.dumps(v3))
    missing["targets"][0].pop("root_identity")
    extra = json.loads(json.dumps(v3))
    extra["unexpected"] = True
    wrong_type = json.loads(json.dumps(v3))
    wrong_type["release_schema_version"] = True

    for invalid in (cross_version, missing, extra, wrong_type):
        assert list(validator.iter_errors(invalid))

    legacy_with_v2_target_field = json.loads(json.dumps(v1))
    legacy_with_v2_target_field["targets"][0]["proposed_inventory_sha256"] = "e" * 64
    legacy_identity = json.loads(json.dumps(legacy_with_v2_target_field))
    legacy_identity["targets"][0].pop("proposed_inventory_sha256")
    legacy_identity.pop("release_id")
    legacy_with_v2_target_field["release_id"] = hashlib.sha256(
        canonical_json_bytes(legacy_identity)
    ).hexdigest()
    with pytest.raises(ReleaseValidationError, match="release_target keys differ"):
        ReleaseManifest.from_json_bytes(
            canonical_json_bytes(legacy_with_v2_target_field)
        )


def test_release_manifest_constructor_rejects_boolean_schema_version() -> None:
    target = ReleaseTarget(
        root_id="root",
        root_identity="a" * 64,
        proposed_generation_id="b" * 64,
        proposed_manifest_sha256="c" * 64,
        proposed_inventory_sha256="d" * 64,
        previous_pointer=None,
    )
    created_at = datetime(2026, 8, 2, 10, tzinfo=UTC)
    identity = {
        "code_commit": COMMIT,
        "companions": [],
        "created_at": created_at.isoformat(),
        "gates": [GateEvidence("bounded-tests", "e" * 64).to_dict()],
        "release_schema_version": True,
        "targets": [target.to_dict(release_schema_version=True)],
    }

    with pytest.raises(ReleaseValidationError, match="schema version is unsupported"):
        ReleaseManifest(
            release_id=hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
            code_commit=COMMIT,
            created_at=created_at,
            targets=(target,),
            gates=(GateEvidence("bounded-tests", "e" * 64),),
            companions=(),
            release_schema_version=True,
        )


def test_commit_updates_all_roots_and_companion(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _, first_proposed = _stage_pair(first_root, "first")
    _, second_proposed = _stage_pair(second_root, "second")
    (first_root / "index.html").write_bytes(b"old\n")
    coordination = tmp_path / "coordination"
    content = b"new launcher\n"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"first": first_root, "second": second_root},
        proposed_generations={
            "first": first_proposed.generation_id,
            "second": second_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(CompanionInput("panel-launcher", "first", "index.html", content),),
    )

    journal = commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"first": first_root, "second": second_root},
        companion_contents={"panel-launcher": content},
    )

    assert journal.state is ReleaseState.COMMITTED
    assert read_current(first_root).generation_id == first_proposed.generation_id
    assert read_current(second_root).generation_id == second_proposed.generation_id
    assert (first_root / "index.html").read_bytes() == content
    assert not (coordination / "release.lock").exists()
    assert (
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"first": first_root, "second": second_root},
            companion_contents={"panel-launcher": content},
        ).state
        is ReleaseState.COMMITTED
    )


def test_failure_after_first_pointer_restores_predecessor_graph(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_previous, first_proposed = _stage_pair(first_root, "first")
    second_previous, second_proposed = _stage_pair(second_root, "second")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"first": first_root, "second": second_root},
        proposed_generations={
            "first": first_proposed.generation_id,
            "second": second_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def fail(step: str, item: str) -> None:
        if step == "after-target" and item == "first":
            raise OSError("injected pointer boundary failure")

    with pytest.raises(ReleaseCommitError, match="predecessor graph was restored"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"first": first_root, "second": second_root},
            fault_hook=fail,
        )

    assert read_current(first_root).generation_id == first_previous.generation_id
    assert read_current(second_root).generation_id == second_previous.generation_id
    _, journal = read_release(coordination, manifest.release_id)
    assert journal.state is ReleaseState.ROLLED_BACK
    assert journal.applied_targets == ("first",)


def test_failure_after_companion_restores_pointers_and_launcher(tmp_path: Path) -> None:
    root = tmp_path / "panel"
    previous, proposed = _stage_pair(root, "panel")
    launcher = root / "index.html"
    launcher.write_bytes(b"old launcher\n")
    coordination = tmp_path / "coordination"
    new = b"new launcher\n"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"panel": root},
        proposed_generations={"panel": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(CompanionInput("stable-launcher", "panel", "index.html", new),),
    )

    def fail(step: str, item: str) -> None:
        if step == "after-companion" and item == "stable-launcher":
            raise OSError("injected launcher boundary failure")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"panel": root},
            companion_contents={"stable-launcher": new},
            fault_hook=fail,
        )

    assert read_current(root).generation_id == previous.generation_id
    assert launcher.read_bytes() == b"old launcher\n"
    _, journal = read_release(coordination, manifest.release_id)
    assert journal.state is ReleaseState.ROLLED_BACK


@pytest.mark.parametrize(
    ("failure_step", "failure_item"),
    (
        ("before-target", "first"),
        ("after-target", "first"),
        ("before-target", "second"),
        ("after-target", "second"),
        ("before-companion", "stable-launcher"),
        ("after-companion", "stable-launcher"),
    ),
)
def test_every_commit_boundary_restores_complete_predecessor_graph(
    tmp_path: Path, failure_step: str, failure_item: str
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_previous, first_proposed = _stage_pair(first_root, "first")
    second_previous, second_proposed = _stage_pair(second_root, "second")
    launcher = first_root / "index.html"
    launcher.write_bytes(b"accepted predecessor launcher\n")
    proposed_launcher = b"proposed launcher\n"
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"first": first_root, "second": second_root},
        proposed_generations={
            "first": first_proposed.generation_id,
            "second": second_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(
            CompanionInput("stable-launcher", "first", "index.html", proposed_launcher),
        ),
    )

    def fail(step: str, item: str) -> None:
        if step == failure_step and item == failure_item:
            raise OSError(f"injected failure at {step}:{item}")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"first": first_root, "second": second_root},
            companion_contents={"stable-launcher": proposed_launcher},
            fault_hook=fail,
        )

    assert read_current(first_root).generation_id == first_previous.generation_id
    assert read_current(second_root).generation_id == second_previous.generation_id
    assert launcher.read_bytes() == b"accepted predecessor launcher\n"
    _, journal = read_release(coordination, manifest.release_id)
    assert journal.state is ReleaseState.ROLLED_BACK


def test_changed_pointer_after_prepare_blocks_commit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "root")
    unexpected = _manifest("unexpected")
    stage_generation(root, unexpected)
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    publish_generation(root, unexpected.generation_id)

    with pytest.raises(ReleaseConflictError, match="changed after prepare"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )

    assert read_current(root).generation_id == unexpected.generation_id


def test_existing_release_lock_blocks_commit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "root")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    coordination.mkdir(exist_ok=True)
    (coordination / "release.lock").write_text("another\n", encoding="ascii")

    with pytest.raises(ReleaseConflictError, match="coordination lock"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )


class _HardCrash(BaseException):
    pass


@pytest.mark.parametrize("crash_step", ("after-target-intent", "after-target"))
def test_explicit_recovery_closes_target_crash_windows(
    tmp_path: Path, crash_step: str
) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "crash-window")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def crash(step: str, item: str) -> None:
        if step == crash_step and item == "root":
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=crash,
        )
    _, interrupted = read_release(coordination, manifest.release_id)
    assert interrupted.state is ReleaseState.COMMITTING
    assert interrupted.applied_targets == ("root",)

    # A real process termination can leave this durable lock behind.
    (coordination / "release.lock").write_text(
        manifest.release_id + "\n", encoding="ascii"
    )
    with pytest.raises(ReleaseConflictError, match="explicit confirmation"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        clear_stale_lock=True,
    )
    assert recovered.state is ReleaseState.ROLLED_BACK
    assert read_current(root).generation_id == previous.generation_id


def test_interrupted_rollback_is_idempotently_resumed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "rollback-crash")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def crash_commit(step: str, item: str) -> None:
        if step == "after-target" and item == "root":
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=crash_commit,
        )

    def crash_rollback(step: str, item: str) -> None:
        if step == "after-target-restore" and item == "root":
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=crash_rollback,
        )
    assert read_current(root).generation_id == previous.generation_id
    _, interrupted = read_release(coordination, manifest.release_id)
    assert interrupted.state is ReleaseState.ROLLING_BACK

    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )
    assert recovered.state is ReleaseState.ROLLED_BACK
    assert read_current(root).generation_id == previous.generation_id


def test_post_mutation_conflict_is_rolled_back(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "conflict")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def conflict(step: str, item: str) -> None:
        if step == "after-target" and item == "root":
            raise ReleaseConflictError("injected post-mutation conflict")

    with pytest.raises(ReleaseCommitError, match="predecessor graph was restored"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=conflict,
        )
    assert read_current(root).generation_id == previous.generation_id


def test_recovery_refuses_stale_lock_for_another_release(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "wrong-lock")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def crash(step: str, item: str) -> None:
        if step == "after-target-intent":
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=crash,
        )
    (coordination / "release.lock").write_text("f" * 64 + "\n", encoding="ascii")
    with pytest.raises(ReleaseConflictError, match="identity differs"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            clear_stale_lock=True,
        )


def test_recovery_clears_exact_prepared_stale_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "prepared-lock")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    lock = coordination / "release.lock"
    lock.write_text(manifest.release_id + "\n", encoding="ascii")

    with pytest.raises(ReleaseConflictError, match="explicit confirmation"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        clear_stale_lock=True,
    )

    assert recovered.state is ReleaseState.PREPARED
    assert not lock.exists()
    assert (
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        ).state
        is ReleaseState.COMMITTED
    )


def test_recovery_clears_exact_committed_stale_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "committed-lock")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )
    lock = coordination / "release.lock"
    lock.write_text(manifest.release_id + "\n", encoding="ascii")

    with pytest.raises(ReleaseConflictError, match="explicit confirmation"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        clear_stale_lock=True,
    )

    assert recovered.state is ReleaseState.COMMITTED
    assert not lock.exists()


def test_recovery_clears_exact_rolled_back_stale_lock(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "rolled-back-lock")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def fail(step: str, item: str) -> None:
        if step == "before-target" and item == "root":
            raise OSError("synthetic caught failure")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=fail,
        )
    lock = coordination / "release.lock"
    lock.write_text(manifest.release_id + "\n", encoding="ascii")

    with pytest.raises(ReleaseConflictError, match="explicit confirmation"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        clear_stale_lock=True,
    )

    assert recovered.state is ReleaseState.ROLLED_BACK
    assert read_current(root).generation_id == previous.generation_id
    assert not lock.exists()


@pytest.mark.parametrize("conflict_kind", ("pointer", "companion"))
def test_recovery_failed_preserves_conflict_and_can_resume(
    tmp_path: Path, conflict_kind: str
) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, f"recovery-failed-{conflict_kind}")
    unexpected = _manifest(f"recovery-failed-{conflict_kind}-unexpected")
    stage_generation(root, unexpected)
    launcher = root / "index.html"
    launcher.write_bytes(b"previous launcher\n")
    proposed_launcher = b"proposed launcher\n"
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(
            CompanionInput("panel-launcher", "root", "index.html", proposed_launcher),
        ),
    )

    def crash(step: str, item: str) -> None:
        expected_step = (
            "after-target" if conflict_kind == "pointer" else "after-companion"
        )
        expected_item = "root" if conflict_kind == "pointer" else "panel-launcher"
        if (step, item) == (expected_step, expected_item):
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            companion_contents={"panel-launcher": proposed_launcher},
            fault_hook=crash,
        )
    if conflict_kind == "pointer":
        publish_generation(root, unexpected.generation_id)
    else:
        launcher.write_bytes(b"external conflicting launcher\n")

    with pytest.raises(ReleaseCommitError, match="recovery failed"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    _, failed = read_release(coordination, manifest.release_id)
    assert failed.state is ReleaseState.RECOVERY_FAILED
    if conflict_kind == "pointer":
        assert read_current(root).generation_id == unexpected.generation_id
        publish_generation(root, proposed.generation_id)
    else:
        assert launcher.read_bytes() == b"external conflicting launcher\n"
        launcher.write_bytes(proposed_launcher)

    recovered = recover_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )

    assert recovered.state is ReleaseState.ROLLED_BACK
    assert read_current(root).generation_id == previous.generation_id
    assert launcher.read_bytes() == b"previous launcher\n"


def test_committed_release_publishes_one_validated_graph_authority(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _, first_proposed = _stage_pair(first_root, "first-authority")
    _, second_proposed = _stage_pair(second_root, "second-authority")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"first": first_root, "second": second_root},
        proposed_generations={
            "first": first_proposed.generation_id,
            "second": second_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"first": first_root, "second": second_root},
    )

    assert (coordination / "CURRENT_RELEASE.json").is_file()
    assert (
        read_current_release(coordination, {"first": first_root, "second": second_root})
        == manifest
    )


def test_release_reader_fails_closed_on_mixed_root_graph(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "accepted-authority")
    unexpected = _manifest("mixed-unexpected")
    stage_generation(root, unexpected)
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )
    publish_generation(root, unexpected.generation_id)

    with pytest.raises(ReleaseConflictError, match="committed pointer graph differs"):
        read_current_release(coordination, {"root": root})


def test_release_reader_fails_closed_on_generation_artifact_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "accepted-artifact-inventory")
    workbook = root / "generations" / proposed.generation_id / "portfolio-report.xlsx"
    workbook.write_bytes(b"accepted workbook\n")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )

    workbook.write_bytes(b"drifted workbook\n")

    with pytest.raises(PointerIntegrityError, match="inventory hash differs"):
        read_current_release(coordination, {"root": root})


def test_explicit_inventory_repair_supersedes_drifted_predecessor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, accepted = _stage_pair(root, "accepted-artifact-inventory")
    workbook = root / "generations" / accepted.generation_id / "report.xlsx"
    workbook.write_bytes(b"accepted workbook\n")
    coordination = tmp_path / "coordination"
    first = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": accepted.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=first.release_id,
        roots={"root": root},
    )
    workbook.write_bytes(b"drifted workbook\n")
    replacement = _manifest("replacement-after-drift")
    stage_generation(root, replacement)

    with pytest.raises(PointerIntegrityError, match="inventory hash differs"):
        prepare_release(
            coordination_root=coordination,
            roots={"root": root},
            proposed_generations={"root": replacement.generation_id},
            code_commit=COMMIT,
            created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
            gates=_gate(),
        )

    repaired = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": replacement.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
        gates=(
            *_gate(),
            GateEvidence(
                "predecessor-authority-repair",
                hashlib.sha256(b"exact drift\n").hexdigest(),
            ),
        ),
        allow_invalid_previous_artifact_inventory=True,
    )
    commit_release(
        coordination_root=coordination,
        release_id=repaired.release_id,
        roots={"root": root},
    )

    assert read_current_release(coordination, {"root": root}) == repaired


def test_inventory_repair_rollback_restores_the_recorded_invalid_predecessor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, accepted = _stage_pair(root, "accepted-repair-rollback")
    workbook = root / "generations" / accepted.generation_id / "report.xlsx"
    workbook.write_bytes(b"accepted workbook\n")
    coordination = tmp_path / "coordination"
    first = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": accepted.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=first.release_id,
        roots={"root": root},
    )
    previous_authority = (coordination / "CURRENT_RELEASE.json").read_bytes()
    workbook.write_bytes(b"drifted workbook\n")
    replacement = _manifest("replacement-repair-rollback")
    stage_generation(root, replacement)
    repair = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": replacement.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
        gates=(
            *_gate(),
            GateEvidence(
                "predecessor-authority-repair",
                hashlib.sha256(b"exact rollback drift\n").hexdigest(),
            ),
        ),
        allow_invalid_previous_artifact_inventory=True,
    )

    def fail(step: str, _item: str) -> None:
        if step == "after-authority":
            raise RuntimeError("injected after authority")

    with pytest.raises(ReleaseCommitError, match="predecessor graph was restored"):
        commit_release(
            coordination_root=coordination,
            release_id=repair.release_id,
            roots={"root": root},
            fault_hook=fail,
        )

    _, journal = read_release(coordination, repair.release_id)
    pointer = CurrentPointer.from_json_bytes((root / "CURRENT.json").read_bytes())
    assert journal.state is ReleaseState.ROLLED_BACK
    assert pointer.generation_id == accepted.generation_id
    assert (coordination / "CURRENT_RELEASE.json").read_bytes() == previous_authority


def test_repeat_commit_revalidates_terminal_graph(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "repeat-validation")
    coordination = tmp_path / "coordination"
    launcher = root / "index.html"
    launcher.write_bytes(b"previous\n")
    accepted = b"accepted\n"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(CompanionInput("launcher", "root", "index.html", accepted),),
    )
    commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        companion_contents={"launcher": accepted},
    )
    launcher.write_bytes(b"tampered\n")

    with pytest.raises(ReleaseConflictError, match="committed companion differs"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            companion_contents={"launcher": accepted},
        )


def test_release_rejects_root_aliases(tmp_path: Path) -> None:
    root = tmp_path / "same-root"
    _, proposed = _stage_pair(root, "alias")

    with pytest.raises(ReleaseValidationError, match="alias the same location"):
        prepare_release(
            coordination_root=tmp_path / "coordination",
            roots={"first": root, "second": root / "."},
            proposed_generations={
                "first": proposed.generation_id,
                "second": proposed.generation_id,
            },
            code_commit=COMMIT,
            created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            gates=_gate(),
        )


def test_prepare_rejects_linked_companion_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "linked-parent-prepare")
    external = tmp_path / "external-launcher"
    external.mkdir()
    _link_directory(root / "launcher", external)

    with pytest.raises(ReleaseConflictError, match="companion parent.*reparse"):
        prepare_release(
            coordination_root=tmp_path / "coordination",
            roots={"root": root},
            proposed_generations={"root": proposed.generation_id},
            code_commit=COMMIT,
            created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            gates=_gate(),
            companions=(
                CompanionInput(
                    "launcher", "root", "launcher/index.html", b"proposed\n"
                ),
            ),
        )


def test_commit_rejects_companion_parent_replaced_by_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "linked-parent-commit")
    launcher_dir = root / "launcher"
    launcher_dir.mkdir()
    (launcher_dir / "index.html").write_bytes(b"previous\n")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(
            CompanionInput("launcher", "root", "launcher/index.html", b"proposed\n"),
        ),
    )
    preserved = tmp_path / "preserved-launcher"
    launcher_dir.rename(preserved)
    _link_directory(launcher_dir, preserved)

    with pytest.raises(ReleaseConflictError, match="companion parent.*reparse"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            companion_contents={"launcher": b"proposed\n"},
        )

    assert read_current(root).generation_id == previous.generation_id
    assert (preserved / "index.html").read_bytes() == b"previous\n"


def test_commit_rejects_same_path_release_root_replacement(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous, proposed = _stage_pair(root, "root-replacement")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    displaced = tmp_path / "displaced-root"
    root.rename(displaced)
    shutil.copytree(displaced, root)

    with pytest.raises(ReleaseConflictError, match="release root identity changed"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )

    assert read_current(root).generation_id == previous.generation_id


def _assert_windows_directory_lease_does_not_block_rename(
    path: Path, displaced: Path
) -> None:
    replacement_started = Event()
    replacement_finished = Event()
    replacement_errors: list[OSError] = []

    def attack() -> None:
        replacement_started.set()
        try:
            path.rename(displaced)
        except OSError as exc:
            replacement_errors.append(exc)
        finally:
            replacement_finished.set()

    lease = _DirectoryLease.acquire(path)
    attacker = Thread(target=attack)
    try:
        attacker.start()
        assert replacement_started.wait(timeout=1)
        assert replacement_finished.wait(timeout=5)
    finally:
        lease.close()
        attacker.join()
    assert not replacement_errors
    assert displaced.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing lease contract")
def test_windows_directory_lease_does_not_prevent_release_root_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _assert_windows_directory_lease_does_not_block_rename(
        root, tmp_path / "displaced-by-attacker"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing lease contract")
def test_windows_directory_lease_does_not_prevent_companion_parent_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    launcher_dir = root / "launcher"
    launcher_dir.mkdir(parents=True)
    _assert_windows_directory_lease_does_not_block_rename(
        launcher_dir, tmp_path / "displaced-launcher"
    )


def test_recovery_rejects_root_replacement_before_rollback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "recovery-root-replacement")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    def crash(step: str, item: str) -> None:
        if (step, item) == ("after-target-intent", "root"):
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=crash,
        )
    displaced = tmp_path / "interrupted-root"
    root.rename(displaced)
    shutil.copytree(displaced, root)

    with pytest.raises(ReleaseConflictError, match="release root identity changed"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )


def test_recovery_rejects_companion_parent_replaced_by_link(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "recovery-linked-parent")
    launcher_dir = root / "launcher"
    launcher_dir.mkdir()
    (launcher_dir / "index.html").write_bytes(b"previous\n")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
        companions=(
            CompanionInput("launcher", "root", "launcher/index.html", b"proposed\n"),
        ),
    )

    def crash(step: str, item: str) -> None:
        if (step, item) == ("after-companion-intent", "launcher"):
            raise _HardCrash()

    with pytest.raises(_HardCrash):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            companion_contents={"launcher": b"proposed\n"},
            fault_hook=crash,
        )
    preserved = tmp_path / "recovery-launcher"
    launcher_dir.rename(preserved)
    _link_directory(launcher_dir, preserved)

    with pytest.raises(ReleaseConflictError, match="companion parent.*reparse"):
        recover_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    _, interrupted = read_release(coordination, manifest.release_id)
    assert interrupted.state is ReleaseState.COMMITTING
    assert (preserved / "index.html").read_bytes() == b"previous\n"


def test_failure_after_authority_switch_restores_prior_release_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _, first_generation = _stage_pair(root, "first-release")
    coordination = tmp_path / "coordination"
    first = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": first_generation.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=first.release_id,
        roots={"root": root},
    )
    second_generation = _manifest("second-release")
    stage_generation(root, second_generation)
    second = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": second_generation.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
        gates=_gate(),
    )

    def fail(step: str, _item: str) -> None:
        if step == "after-authority":
            raise OSError("synthetic post-authority failure")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=coordination,
            release_id=second.release_id,
            roots={"root": root},
            fault_hook=fail,
        )

    assert read_current(root).generation_id == first_generation.generation_id
    assert read_current_release(coordination, {"root": root}) == first


def test_explicit_legacy_pointer_migration_preserves_governed_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    previous = _manifest("legacy-previous")
    proposed = _manifest("legacy-proposed")
    stage_generation(root, previous)
    stage_generation(root, proposed)
    publish_generation(root, previous.generation_id)
    coordination = tmp_path / "coordination"
    baseline = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": previous.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
        gates=_gate(),
    )
    commit_release(
        coordination_root=coordination,
        release_id=baseline.release_id,
        roots={"root": root},
    )
    legacy = CurrentPointer(
        generation_id=previous.generation_id,
        manifest_sha256=hashlib.sha256(previous.to_json_bytes()).hexdigest(),
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "CURRENT.json").write_bytes(legacy.to_json_bytes())
    with pytest.raises(PointerIntegrityError, match="legacy pointer schema"):
        prepare_release(
            coordination_root=coordination,
            roots={"root": root},
            proposed_generations={"root": proposed.generation_id},
            code_commit=COMMIT,
            created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            gates=_gate(),
        )

    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_legacy_migration_gates(),
        allow_invalid_previous_artifact_inventory=True,
    )

    def fail(step: str, _item: str) -> None:
        if step == "after-target":
            raise OSError("synthetic rollback")

    with pytest.raises(ReleaseCommitError):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
            fault_hook=fail,
        )

    assert (root / "CURRENT.json").read_bytes() == legacy.to_json_bytes()
    with pytest.raises(PointerIntegrityError, match="legacy pointer schema"):
        read_current(root)


def test_directory_lease_verifies_the_identity_it_acquired(tmp_path) -> None:
    """`verify` must re-derive identity exactly as `acquire` did.

    On Windows `acquire` reads the volume serial and 64-bit file index through
    GetFileInformationByHandle.  Re-deriving from `Path.stat()` yields different
    quantities, so every coordinated release failed to commit on Windows with
    "security directory lease identity changed".
    """

    target = tmp_path / "target"
    target.mkdir()
    lease = _DirectoryLease.acquire(target)
    try:
        lease.verify()
        assert lease.identity == _path_identity(target)
    finally:
        lease.close()


def _write_release_validation_findings(
    root: Path,
    generation_id: str,
    *,
    findings: tuple[dict[str, object], ...] = (),
    declared_generation_id: str | None = None,
    path_generation_id: str | None = None,
    baseline: str | None = "b" * 64,
) -> None:
    """A minimal well-formed findings.json per state/briefs/findings-contract.md.

    Written at the path keyed by ``path_generation_id`` (default: ``generation_id``,
    the real path the gate looks up), declaring ``declared_generation_id``
    (default: ``generation_id``) inside the document -- distinct knobs so a test
    can put a findings document at the right path while it names the wrong
    generation, exercising the gate's content check rather than its path lookup.

    ``baseline`` defaults to a non-null digest: since T2.8, the publication
    gate refuses a null ``expected_baseline_sha256``, so a "clean"/publishable
    document now needs one.
    """
    declared_generation_id = declared_generation_id or generation_id
    path_generation_id = path_generation_id or generation_id
    summary = {
        "BLOCKER_INVARIANT": 0,
        "BLOCKER_AUTHORITY": 0,
        "BLOCKER_REGRESSION": 0,
        "NOT_READY": 0,
        "WARNING": 0,
    }
    for entry in findings:
        summary[str(entry["class"])] += 1
    path = root / "validation" / path_generation_id / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema": "qe-validate-generation/1",
                "generation_id": declared_generation_id,
                "generation_dir": str(root / "generations" / declared_generation_id),
                "validator_commit": "c" * 40,
                "expected_baseline_sha256": baseline,
                "summary": summary,
                "findings": list(findings),
            }
        ).encode("utf-8")
    )


def _write_derived_release_findings(
    root: Path,
    generation_id: str,
    *,
    panel_generation_id: str,
    panel_findings_sha256: str,
    findings: tuple[dict[str, object], ...] = (),
    baseline: str | None = "b" * 64,
) -> None:
    """A minimal well-formed ``qe-release-findings/1`` document (ruling B):
    the schema ``tools/validate_release_targets.py`` writes for a non-panel
    release target, binding it to a named panel target's findings bytes."""
    summary = {
        "BLOCKER_INVARIANT": 0,
        "BLOCKER_AUTHORITY": 0,
        "BLOCKER_REGRESSION": 0,
        "NOT_READY": 0,
        "WARNING": 0,
    }
    for entry in findings:
        summary[str(entry["class"])] += 1
    path = root / "validation" / generation_id / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema": "qe-release-findings/1",
                "generation_id": generation_id,
                "generation_dir": str(root / "generations" / generation_id),
                "validator_commit": "c" * 40,
                "expected_baseline_sha256": baseline,
                "summary": summary,
                "findings": list(findings),
                "panel_validation": {
                    "generation_id": panel_generation_id,
                    "findings_sha256": panel_findings_sha256,
                },
            }
        ).encode("utf-8")
    )


def _findings_sha256(root: Path, generation_id: str) -> str:
    path = root / "validation" / generation_id / "findings.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _enable_real_release_validation(
    monkeypatch, real_verify_release_validation
) -> None:
    import quant_engine.core.release as release_module

    monkeypatch.setattr(
        release_module, "_verify_release_validation", real_verify_release_validation
    )


def test_commit_release_refuses_without_release_validation_findings(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """The gate: a real (non-rehearsal, non-mechanics) commit must refuse
    without a clean validate_generation findings file for the exact generation
    being published -- the property the corrective program's T1.4 gate exists
    to enforce at the actual publication sink, not only in an operator script.
    """
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    with pytest.raises(ReleaseValidationError, match="release validation refuses"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )

    # Refused before any commit side effect: no journal transition, no publish.
    _, journal = read_release(coordination, manifest.release_id)
    assert journal.state is ReleaseState.PREPARED
    assert read_current(root).generation_id != proposed.generation_id


def test_commit_release_succeeds_with_clean_release_validation_findings(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate-clean")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_release_validation_findings(root, proposed.generation_id)

    journal = commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
    )

    assert journal.state is ReleaseState.COMMITTED
    assert read_current(root).generation_id == proposed.generation_id


def test_commit_release_refuses_with_a_null_expected_baseline(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """T2.8 (PR #487 bot finding 4113723374): a findings document produced
    without ``--expected`` (``expected_baseline_sha256: null``) has zero
    ``BLOCKER_*`` findings -- the skipped regression suite is only
    ``NOT_READY`` -- so it must still refuse at the real publication sink."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate-no-baseline")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_release_validation_findings(root, proposed.generation_id, baseline=None)

    with pytest.raises(ReleaseValidationError, match="expected_baseline_sha256"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    assert read_current(root).generation_id != proposed.generation_id


def test_commit_release_refuses_with_a_blocking_release_validation_finding(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate-blocked")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    # An applicable open correction maps to BLOCKER_AUTHORITY, check_id prefix
    # `correction.` (state/briefs/findings-contract.md, #326).
    _write_release_validation_findings(
        root,
        proposed.generation_id,
        findings=(
            {
                "check_id": "correction.applicable_open",
                "class": "BLOCKER_AUTHORITY",
                "entity": "root",
                "observed": "open",
                "expected": "resolved",
                "pointer": "corrections/1.json",
                "defect_ids": ["DEF-1"],
            },
        ),
    )

    with pytest.raises(ReleaseValidationError, match="blocking findings"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    assert read_current(root).generation_id != proposed.generation_id


def test_commit_release_refuses_when_findings_generation_id_differs(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate-mismatch")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_release_validation_findings(
        root,
        proposed.generation_id,
        declared_generation_id="a-different-generation-id",
    )

    with pytest.raises(ReleaseValidationError, match="generation_id differs"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"root": root},
        )
    assert read_current(root).generation_id != proposed.generation_id


def test_commit_release_skip_release_validation_bypasses_the_gate(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """The explicit, narrow escape hatch: only for a deliberately non-financial
    commit (recovery rehearsal); documented on `commit_release` itself."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    root = tmp_path / "root"
    _, proposed = _stage_pair(root, "gate-skip")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"root": root},
        proposed_generations={"root": proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    journal = commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"root": root},
        skip_release_validation=True,
    )

    assert journal.state is ReleaseState.COMMITTED


def test_commit_release_accepts_a_clean_derived_release_findings_target(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """Ruling B's two-pass gate: a target with a derived
    ``qe-release-findings/1`` document, correctly bound (by sha256) to a
    sibling target's clean panel findings, may publish alongside it."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    panel_root = tmp_path / "panel"
    other_root = tmp_path / "other"
    _, panel_proposed = _stage_pair(panel_root, "panel")
    _, other_proposed = _stage_pair(other_root, "other")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"panel": panel_root, "other": other_root},
        proposed_generations={
            "panel": panel_proposed.generation_id,
            "other": other_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_release_validation_findings(panel_root, panel_proposed.generation_id)
    panel_findings_sha256 = _findings_sha256(panel_root, panel_proposed.generation_id)
    _write_derived_release_findings(
        other_root,
        other_proposed.generation_id,
        panel_generation_id=panel_proposed.generation_id,
        panel_findings_sha256=panel_findings_sha256,
    )

    journal = commit_release(
        coordination_root=coordination,
        release_id=manifest.release_id,
        roots={"panel": panel_root, "other": other_root},
    )

    assert journal.state is ReleaseState.COMMITTED
    assert read_current(panel_root).generation_id == panel_proposed.generation_id
    assert read_current(other_root).generation_id == other_proposed.generation_id


def test_commit_release_refuses_a_stale_derived_release_findings_target(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """A derived document's ``panel_validation.findings_sha256`` must name the
    panel's *current* clean findings bytes exactly; one byte different (e.g.
    the panel was re-validated) must refuse the whole release, not just warn."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    panel_root = tmp_path / "panel"
    other_root = tmp_path / "other"
    _, panel_proposed = _stage_pair(panel_root, "panel-stale")
    _, other_proposed = _stage_pair(other_root, "other-stale")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"panel": panel_root, "other": other_root},
        proposed_generations={
            "panel": panel_proposed.generation_id,
            "other": other_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_release_validation_findings(panel_root, panel_proposed.generation_id)
    _write_derived_release_findings(
        other_root,
        other_proposed.generation_id,
        panel_generation_id=panel_proposed.generation_id,
        panel_findings_sha256="f" * 64,  # does not match the panel's real bytes
    )

    with pytest.raises(ReleaseValidationError, match="stale"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"panel": panel_root, "other": other_root},
        )
    assert read_current(panel_root).generation_id != panel_proposed.generation_id
    assert read_current(other_root).generation_id != other_proposed.generation_id


def test_commit_release_refuses_when_no_target_has_clean_panel_findings(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """Ruling B: at least one target must carry a clean judged (panel,
    ``qe-validate-generation/1``) document -- a release made entirely of
    derived documents, with nothing judged, has nothing for the bound set to
    bind to. With a named (but absent/unclean) panel generation, the only
    target reports the exact binding it can't satisfy -- there is no separate,
    reachable "nothing was judged at all" message once any target names a
    reason, since every derived document that cannot resolve to a clean panel
    already reports itself stale."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    other_root = tmp_path / "other"
    _, other_proposed = _stage_pair(other_root, "other-unjudged")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"other": other_root},
        proposed_generations={"other": other_proposed.generation_id},
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )
    _write_derived_release_findings(
        other_root,
        other_proposed.generation_id,
        panel_generation_id="some-panel-gen",
        panel_findings_sha256="f" * 64,
    )

    with pytest.raises(ReleaseValidationError, match="panel_validation is stale"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"other": other_root},
        )
    assert read_current(other_root).generation_id != other_proposed.generation_id


def test_commit_release_refuses_an_override_that_restores_an_older_panel_anchor(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """Astra (release.py:1863): derive ``other``'s findings from panel document
    A, then re-validate the panel to clean document B at its default path (A
    and B differ byte-for-byte). A plain commit refuses (stale, already
    covered by ``test_commit_release_refuses_a_stale_derived_release_findings_target``).
    But ``release_validation_findings={"panel": <path to the saved A bytes>}``
    must not silently restore A as "the current panel anchor" either -- an
    override whose bytes differ from the root's actual current (default-path)
    findings is exactly the freshness-anchor substitution the gate exists to
    prevent, so it must refuse too, not just skip the staleness check."""
    _enable_real_release_validation(monkeypatch, real_verify_release_validation)
    panel_root = tmp_path / "panel"
    other_root = tmp_path / "other"
    _, panel_proposed = _stage_pair(panel_root, "panel-anchor")
    _, other_proposed = _stage_pair(other_root, "other-anchor")
    coordination = tmp_path / "coordination"
    manifest = prepare_release(
        coordination_root=coordination,
        roots={"panel": panel_root, "other": other_root},
        proposed_generations={
            "panel": panel_proposed.generation_id,
            "other": other_proposed.generation_id,
        },
        code_commit=COMMIT,
        created_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        gates=_gate(),
    )

    # Document A: the panel's first clean findings. `other`'s derived document
    # is bound to A's exact bytes.
    _write_release_validation_findings(panel_root, panel_proposed.generation_id)
    panel_path = (
        panel_root / "validation" / panel_proposed.generation_id / "findings.json"
    )
    saved_document_a = tmp_path / "saved-document-a.json"
    saved_document_a.write_bytes(panel_path.read_bytes())
    document_a_sha256 = _findings_sha256(panel_root, panel_proposed.generation_id)
    _write_derived_release_findings(
        other_root,
        other_proposed.generation_id,
        panel_generation_id=panel_proposed.generation_id,
        panel_findings_sha256=document_a_sha256,
    )

    # The panel is re-validated: document B (different bytes/sha256) replaces
    # A at the real, default findings path. `other`'s derived document --
    # still citing A -- is now stale against the real current panel findings.
    _write_release_validation_findings(
        panel_root,
        panel_proposed.generation_id,
        findings=(
            {
                "check_id": "example.warning",
                "class": "WARNING",
                "entity": "root",
                "observed": "x",
                "expected": "y",
                "pointer": "generations/abc/report.json",
                "defect_ids": [],
            },
        ),
    )
    assert panel_path.read_bytes() != saved_document_a.read_bytes()

    with pytest.raises(ReleaseValidationError, match="stale"):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"panel": panel_root, "other": other_root},
        )

    # The exploit: supplying the override restores A as "the" panel findings
    # for this call, even though the real, default-path findings are now B.
    with pytest.raises(ReleaseValidationError):
        commit_release(
            coordination_root=coordination,
            release_id=manifest.release_id,
            roots={"panel": panel_root, "other": other_root},
            release_validation_findings={"panel": saved_document_a},
        )

    assert read_current(panel_root).generation_id != panel_proposed.generation_id
    assert read_current(other_root).generation_id != other_proposed.generation_id


def test_commit_release_refuses_when_only_findings_file_is_missing_entirely(
    tmp_path: Path, monkeypatch, real_verify_release_validation
) -> None:
    """The "nothing was judged" fallback message in `_verify_release_validation`
    for the one case the per-target loop leaves with no reason to report: when
    `evaluate_release_findings_set` itself reports no refusal reason for any
    target (practically unreachable with a non-empty manifest -- see the test
    above -- but exercised here directly against the function, not the CLI
    path, as a safety-net unit test of the fallback branch)."""
    from quant_engine.core.publication import evaluate_release_findings_set

    may_publish, per_root_reason = evaluate_release_findings_set([], {})

    assert may_publish is False
    assert per_root_reason == {}

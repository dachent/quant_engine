from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CompanionInput,
    CoverageClaim,
    CoverageStatus,
    GateEvidence,
    GenerationManifest,
    ReleaseCommitError,
    ReleaseConflictError,
    ReleaseManifest,
    ReleaseState,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    read_current,
    read_release,
    stage_generation,
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

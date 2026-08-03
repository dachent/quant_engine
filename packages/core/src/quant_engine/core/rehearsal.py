"""Isolated fault-injection and dated-rollforward recovery rehearsal."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from pathlib import Path, PurePosixPath
from typing import Final, Mapping

from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)
from quant_engine.core.pipeline import PipelineSpec
from quant_engine.core.publication import (
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.release import (
    CompanionInput,
    GateEvidence,
    ReleaseCommitError,
    ReleaseConflictError,
    ReleaseManifest,
    ReleaseState,
    commit_release,
    prepare_release,
    read_release,
    recover_release,
)


_SCHEMA_VERSION: Final = 1


class RehearsalError(RuntimeError):
    """The recovery rehearsal did not restore the required graph."""


def _file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RehearsalError(f"expected regular file is missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_dir():
        raise RehearsalError(f"expected generation directory is missing: {path}")
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise RehearsalError("accepted generation contains a link")
        if item.is_file():
            rows.append(
                {
                    "relative_path": item.relative_to(path).as_posix(),
                    "sha256": _file_hash(item),
                    "size_bytes": item.stat().st_size,
                }
            )
    return hashlib.sha256(canonical_json_bytes({"files": rows})).hexdigest()


def _source_state(
    spec: PipelineSpec, release_id: str
) -> tuple[dict[str, object], ReleaseManifest]:
    manifest, journal = read_release(spec.coordination_root, release_id)
    targets: dict[str, object] = {}
    for target in manifest.targets:
        root = spec.roots[target.root_id]
        generations = {target.proposed_generation_id}
        if target.previous_pointer is not None:
            generations.add(target.previous_pointer.generation_id)
        targets[target.root_id] = {
            "current_sha256": _file_hash(root / "CURRENT.json"),
            "generations": {
                generation_id: _tree_hash(root / "generations" / generation_id)
                for generation_id in sorted(generations)
            },
        }
    companions = {}
    for companion in manifest.companions:
        path = spec.roots[companion.root_id] / PurePosixPath(companion.relative_path)
        companions[companion.companion_id] = _file_hash(path) if path.exists() else None
    return {
        "companions": companions,
        "journal_state": journal.state.value,
        "targets": targets,
    }, manifest


def _copy_manifest(source_root: Path, generation_id: str, target_root: Path) -> None:
    source = source_root / "generations" / generation_id / "generation.json"
    manifest = GenerationManifest.from_json_bytes(source.read_bytes())
    if manifest.generation_id != generation_id:
        raise RehearsalError("source generation identity differs")
    staged = stage_generation(target_root, manifest)
    if _file_hash(staged) != _file_hash(source):
        raise RehearsalError("rehearsal manifest copy differs")


def _restore_companion(
    spec: PipelineSpec,
    release_id: str,
    companion_id: str,
    root: Path,
    relative_path: str,
    previous_sha256: str | None,
) -> None:
    destination = root / PurePosixPath(relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if previous_sha256 is None:
        destination.unlink(missing_ok=True)
        return
    recovery = (
        spec.coordination_root
        / "releases"
        / release_id
        / "recovery"
        / f"{companion_id}.bin"
    )
    content = recovery.read_bytes()
    if hashlib.sha256(content).hexdigest() != previous_sha256:
        raise RehearsalError("source recovery companion differs")
    destination.write_bytes(content)


def rehearse_release_boundaries(
    *, spec: PipelineSpec, release_id: str, output_root: Path
) -> dict[str, object]:
    """Prove caught-failure and process-termination recovery at every boundary."""

    source_before, manifest = _source_state(spec, release_id)
    if manifest.release_id != release_id:
        raise RehearsalError("release identity differs")
    if source_before["journal_state"] != ReleaseState.COMMITTED.value:
        raise RehearsalError("source release must be committed")
    for target in manifest.targets:
        if (
            read_current(spec.roots[target.root_id]).generation_id
            != target.proposed_generation_id
        ):
            raise RehearsalError("source release pointer graph is not current")
    proposed_companions = {
        item.companion_id: item.source_path.read_bytes() for item in spec.companions
    }
    caught_boundaries = [
        *(("before-target", item.root_id) for item in manifest.targets),
        *(("after-target-intent", item.root_id) for item in manifest.targets),
        *(("after-target", item.root_id) for item in manifest.targets),
        *(("before-companion", item.companion_id) for item in manifest.companions),
        *(
            ("after-companion-intent", item.companion_id)
            for item in manifest.companions
        ),
        *(("after-companion", item.companion_id) for item in manifest.companions),
    ]
    crash_boundaries = [
        *(("after-target-intent", item.root_id) for item in manifest.targets),
        *(("after-target", item.root_id) for item in manifest.targets),
        *(
            ("after-companion-intent", item.companion_id)
            for item in manifest.companions
        ),
        *(("after-companion", item.companion_id) for item in manifest.companions),
    ]
    rows = []
    matrix_root = output_root / "f"

    def prepare_scenario(scenario: Path) -> tuple[dict[str, Path], ReleaseManifest]:
        roots: dict[str, Path] = {}
        for target in manifest.targets:
            source_root = spec.roots[target.root_id]
            rehearsal_root = scenario / "r" / target.root_id
            roots[target.root_id] = rehearsal_root
            _copy_manifest(source_root, target.proposed_generation_id, rehearsal_root)
            if target.previous_pointer is not None:
                _copy_manifest(
                    source_root,
                    target.previous_pointer.generation_id,
                    rehearsal_root,
                )
                publish_generation(
                    rehearsal_root, target.previous_pointer.generation_id
                )
        for companion in manifest.companions:
            _restore_companion(
                spec,
                release_id,
                companion.companion_id,
                roots[companion.root_id],
                companion.relative_path,
                companion.previous_sha256,
            )
        prepared = prepare_release(
            coordination_root=scenario / "c",
            roots=roots,
            proposed_generations={
                item.root_id: item.proposed_generation_id for item in manifest.targets
            },
            code_commit=manifest.code_commit,
            created_at=manifest.created_at,
            gates=(GateEvidence("recovery-rehearsal", "f" * 64),),
            companions=tuple(
                CompanionInput(
                    item.companion_id,
                    item.root_id,
                    item.relative_path,
                    proposed_companions[item.companion_id],
                )
                for item in manifest.companions
            ),
        )
        return roots, prepared

    boundary_cases = [
        *(("CAUGHT_EXCEPTION", *item) for item in caught_boundaries),
        *(("PROCESS_TERMINATION", *item) for item in crash_boundaries),
    ]
    for index, (failure_mode, failure_step, failure_item) in enumerate(
        boundary_cases, start=1
    ):
        scenario = matrix_root / f"{index:02d}"
        roots, prepared = prepare_scenario(scenario)

        def fail(step: str, item: str) -> None:
            if (step, item) == (failure_step, failure_item):
                if failure_mode == "PROCESS_TERMINATION":
                    raise _ProcessTermination()
                raise OSError(f"injected failure at {step}:{item}")

        try:
            commit_release(
                coordination_root=scenario / "c",
                release_id=prepared.release_id,
                roots=roots,
                companion_contents=proposed_companions,
                fault_hook=fail,
            )
        except ReleaseCommitError:
            if failure_mode != "CAUGHT_EXCEPTION":
                raise RehearsalError("process termination was caught as an exception")
        except _ProcessTermination:
            if failure_mode != "PROCESS_TERMINATION":
                raise RehearsalError("caught failure escaped as process termination")
            lock = scenario / "c" / "release.lock"
            lock.write_text(prepared.release_id + "\n", encoding="ascii")
            try:
                recover_release(
                    coordination_root=scenario / "c",
                    release_id=prepared.release_id,
                    roots=roots,
                )
            except ReleaseConflictError:
                pass
            else:
                raise RehearsalError("stale lock did not require confirmation")
            recover_release(
                coordination_root=scenario / "c",
                release_id=prepared.release_id,
                roots=roots,
                clear_stale_lock=True,
            )
        else:
            raise RehearsalError("injected boundary did not fail")
        _, journal = read_release(scenario / "c", prepared.release_id)
        if journal.state is not ReleaseState.ROLLED_BACK:
            raise RehearsalError("failed release did not roll back")
        for target in manifest.targets:
            pointer_path = roots[target.root_id] / "CURRENT.json"
            if target.previous_pointer is None:
                if pointer_path.exists():
                    raise RehearsalError("absent predecessor pointer was not restored")
            elif read_current(roots[target.root_id]) != target.previous_pointer:
                raise RehearsalError("predecessor pointer graph was not restored")
        for companion in manifest.companions:
            path = roots[companion.root_id] / PurePosixPath(companion.relative_path)
            actual = _file_hash(path) if path.exists() else None
            if actual != companion.previous_sha256:
                raise RehearsalError("predecessor companion was not restored")
        rows.append(
            {
                "failure_item": failure_item,
                "failure_mode": failure_mode,
                "failure_step": failure_step,
                "journal_state": journal.state.value,
                "restored": True,
            }
        )

    # Prove rollback can itself be interrupted after an effect and resumed.
    rollback_scenario = output_root / "rollback-interruption"
    index += 1
    roots, prepared = prepare_scenario(rollback_scenario)
    first_target = manifest.targets[0].root_id

    def terminate_commit(step: str, item: str) -> None:
        if (step, item) == ("after-target", first_target):
            raise _ProcessTermination()

    try:
        commit_release(
            coordination_root=rollback_scenario / "c",
            release_id=prepared.release_id,
            roots=roots,
            companion_contents=proposed_companions,
            fault_hook=terminate_commit,
        )
    except _ProcessTermination:
        pass
    else:
        raise RehearsalError("rollback interruption setup did not terminate")

    def terminate_rollback(step: str, item: str) -> None:
        if (step, item) == ("after-target-restore", first_target):
            raise _ProcessTermination()

    try:
        recover_release(
            coordination_root=rollback_scenario / "c",
            release_id=prepared.release_id,
            roots=roots,
            fault_hook=terminate_rollback,
        )
    except _ProcessTermination:
        pass
    else:
        raise RehearsalError("rollback interruption did not terminate")
    _, interrupted = read_release(rollback_scenario / "c", prepared.release_id)
    if interrupted.state is not ReleaseState.ROLLING_BACK:
        raise RehearsalError("interrupted rollback journal state differs")
    resumed = recover_release(
        coordination_root=rollback_scenario / "c",
        release_id=prepared.release_id,
        roots=roots,
    )
    if resumed.state is not ReleaseState.ROLLED_BACK:
        raise RehearsalError("interrupted rollback did not resume")
    source_after, _ = _source_state(spec, release_id)
    if source_after != source_before:
        raise RehearsalError("accepted source state changed during rehearsal")
    return {
        "boundary_count": len(rows),
        "boundaries": rows,
        "caught_boundary_count": len(caught_boundaries),
        "crash_boundary_count": len(crash_boundaries),
        "rollback_interruption": {
            "interrupted_state": ReleaseState.ROLLING_BACK.value,
            "resumed_state": resumed.state.value,
            "status": "PASS",
        },
        "source_state_sha256": hashlib.sha256(
            canonical_json_bytes(source_before)
        ).hexdigest(),
        "source_unchanged": True,
        "status": "PASS",
    }


class _ProcessTermination(BaseException):
    """Fault used to model interpreter/process termination, not a caught error."""


def _synthetic_manifest(label: str, as_of_date: date) -> GenerationManifest:
    payload = (label + "\n").encode()
    return GenerationManifest.build(
        as_of_date=as_of_date,
        adapter=AdapterIdentity("recovery-rehearsal", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=label,
            fact_types=("synthetic-release",),
            observed_start=as_of_date,
            observed_end=as_of_date,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"synthetic/{label}.txt",
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                as_of_date=as_of_date,
                schema_name="recovery-rehearsal",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _publish_synthetic_release(
    *,
    coordination_root: Path,
    roots: Mapping[str, Path],
    generations: Mapping[str, GenerationManifest],
    launcher: bytes,
    created_at: datetime,
) -> str:
    for root_id, manifest in generations.items():
        stage_generation(roots[root_id], manifest)
    release = prepare_release(
        coordination_root=coordination_root,
        roots=roots,
        proposed_generations={
            root_id: manifest.generation_id for root_id, manifest in generations.items()
        },
        code_commit="d" * 40,
        created_at=created_at,
        gates=(GateEvidence("synthetic-rehearsal", "e" * 64),),
        companions=(CompanionInput("panel-launcher", "panel", "index.html", launcher),),
    )
    journal = commit_release(
        coordination_root=coordination_root,
        release_id=release.release_id,
        roots=roots,
        companion_contents={"panel-launcher": launcher},
    )
    if journal.state is not ReleaseState.COMMITTED:
        raise RehearsalError("synthetic release did not commit")
    return release.release_id


def rehearse_dated_rollforwards(output_root: Path) -> dict[str, object]:
    """Prove two rollforwards, append-only correction, and explicit restore."""

    root_ids = ("account-ledger", "excel-report", "analytics", "panel")
    roots = {root_id: output_root / "dated" / root_id for root_id in root_ids}
    coordination = output_root / "dated" / "coordination"
    day0 = date(2026, 7, 30)
    day1 = date(2026, 7, 31)
    day2 = date(2026, 8, 1)
    predecessor = {
        root_id: _synthetic_manifest(f"{root_id}-predecessor", day0)
        for root_id in root_ids
    }
    for root_id, manifest in predecessor.items():
        stage_generation(roots[root_id], manifest)
        publish_generation(roots[root_id], manifest.generation_id)
    old_launcher = b"accepted predecessor launcher\n"
    (roots["panel"] / "index.html").write_bytes(old_launcher)
    predecessor_hashes = {
        root_id: _file_hash(
            roots[root_id] / "generations" / manifest.generation_id / "generation.json"
        )
        for root_id, manifest in predecessor.items()
    }
    first = {
        root_id: _synthetic_manifest(f"{root_id}-day-1", day1) for root_id in root_ids
    }
    release_one = _publish_synthetic_release(
        coordination_root=coordination,
        roots=roots,
        generations=first,
        launcher=b"day 1 launcher\n",
        created_at=datetime(2026, 7, 31, 23, tzinfo=UTC),
    )
    second = {
        root_id: _synthetic_manifest(f"{root_id}-day-2", day2) for root_id in root_ids
    }
    launcher_two = b"day 2 launcher\n"
    release_two = _publish_synthetic_release(
        coordination_root=coordination,
        roots=roots,
        generations=second,
        launcher=launcher_two,
        created_at=datetime(2026, 8, 1, 23, tzinfo=UTC),
    )
    correction = {
        root_id: _synthetic_manifest(f"{root_id}-day-2-correction", day2)
        for root_id in root_ids
    }
    correction_release = _publish_synthetic_release(
        coordination_root=coordination,
        roots=roots,
        generations=correction,
        launcher=b"day 2 corrected launcher\n",
        created_at=datetime(2026, 8, 2, 1, tzinfo=UTC),
    )
    restore_release = _publish_synthetic_release(
        coordination_root=coordination,
        roots=roots,
        generations=second,
        launcher=launcher_two,
        created_at=datetime(2026, 8, 2, 2, tzinfo=UTC),
    )
    for root_id in root_ids:
        if read_current(roots[root_id]).generation_id != second[root_id].generation_id:
            raise RehearsalError("explicit restoration did not restore release two")
        predecessor_path = (
            roots[root_id]
            / "generations"
            / predecessor[root_id].generation_id
            / "generation.json"
        )
        if _file_hash(predecessor_path) != predecessor_hashes[root_id]:
            raise RehearsalError("predecessor generation was overwritten")
        if correction[root_id].generation_id == second[root_id].generation_id:
            raise RehearsalError("correction did not create a new generation")
    if (roots["panel"] / "index.html").read_bytes() != launcher_two:
        raise RehearsalError("restored launcher differs")
    releases = (release_one, release_two, correction_release, restore_release)
    if len(set(releases)) != len(releases):
        raise RehearsalError("dated release identities are not unique")
    return {
        "append_only_correction": True,
        "as_of_dates": {
            "correction": day2.isoformat(),
            "release_one": day1.isoformat(),
            "release_two": day2.isoformat(),
        },
        "correction_release_id": correction_release,
        "predecessors_unchanged": True,
        "release_one_id": release_one,
        "release_two_id": release_two,
        "restore_release_id": restore_release,
        "restored_release_two": True,
        "status": "PASS",
    }


def run_recovery_rehearsal(
    *, spec: PipelineSpec, release_id: str, output_root: Path
) -> dict[str, object]:
    resolved = output_root.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise RehearsalError("rehearsal output root must be absent or empty")
    resolved.mkdir(parents=True, exist_ok=True)
    boundary = rehearse_release_boundaries(
        spec=spec, release_id=release_id, output_root=resolved
    )
    dated = rehearse_dated_rollforwards(resolved)
    result = {
        "boundary_rehearsal": boundary,
        "dated_rollforwards": dated,
        "release_id": release_id,
        "schema_version": _SCHEMA_VERSION,
        "source_release_spec_sha256": spec.spec_sha256,
        "status": "PASS",
    }
    evidence = resolved / "recovery-rehearsal.json"
    evidence.write_bytes(canonical_json_bytes(result))
    return {
        **result,
        "evidence_path": str(evidence),
        "evidence_sha256": _file_hash(evidence),
    }

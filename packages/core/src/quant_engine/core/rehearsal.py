"""Isolated fault-injection and dated-rollforward recovery rehearsal."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import multiprocessing
from pathlib import Path, PurePosixPath
from typing import Callable, Final, Mapping, Protocol

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
    ReleaseJournal,
    ReleaseManifest,
    ReleaseState,
    commit_release,
    prepare_release,
    read_release,
    recover_release,
)


_SCHEMA_VERSION: Final = 2


class RehearsalError(RuntimeError):
    """The recovery rehearsal did not restore the required graph."""


class _EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


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


def _commit_until_terminated(
    coordination_root: Path,
    release_id: str,
    roots: dict[str, Path],
    companion_contents: dict[str, bytes],
    failure_step: str,
    failure_item: str,
    ready: _EventLike,
    hold: _EventLike,
) -> None:
    def pause(step: str, item: str) -> None:
        if (step, item) == (failure_step, failure_item):
            ready.set()
            hold.wait()

    commit_release(
        coordination_root=coordination_root,
        release_id=release_id,
        roots=roots,
        companion_contents=companion_contents,
        fault_hook=pause,
    )


def _recover_until_terminated(
    coordination_root: Path,
    release_id: str,
    roots: dict[str, Path],
    clear_stale_lock: bool,
    failure_step: str,
    failure_item: str,
    ready: _EventLike,
    hold: _EventLike,
) -> None:
    def pause(step: str, item: str) -> None:
        if (step, item) == (failure_step, failure_item):
            ready.set()
            hold.wait()

    recover_release(
        coordination_root=coordination_root,
        release_id=release_id,
        roots=roots,
        clear_stale_lock=clear_stale_lock,
        fault_hook=pause,
    )


def _terminate_at_checkpoint(
    target: Callable[..., None], args: tuple[object, ...]
) -> tuple[int, int]:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    hold = context.Event()
    process = context.Process(target=target, args=(*args, ready, hold))
    process.start()
    process_id = process.pid
    if process_id is None:
        process.terminate()
        process.join(30)
        raise RehearsalError("terminated worker did not report a process ID")
    if not ready.wait(30):
        process.terminate()
        process.join(30)
        raise RehearsalError(
            f"termination checkpoint was not reached; exit={process.exitcode}"
        )
    process.terminate()
    process.join(30)
    if process.is_alive():
        process.kill()
        process.join(30)
    exitcode = process.exitcode
    process.close()
    if exitcode in {None, 0}:
        raise RehearsalError("terminated worker did not exit abnormally")
    return process_id, exitcode


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
    process_terminations: list[dict[str, object]] = []
    matrix_root = output_root / "f"

    def prepare_scenario(
        scenario: Path, *, preserve_companion_predecessors: bool = True
    ) -> tuple[dict[str, Path], ReleaseManifest]:
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
                if target.previous_pointer.pointer_schema_version == 1:
                    # Preserve a legacy predecessor contract exactly. Calling
                    # publish_generation here would silently upgrade v1 to v2.
                    (rehearsal_root / "CURRENT.json").write_bytes(
                        target.previous_pointer.to_json_bytes()
                    )
                    if read_current(rehearsal_root) != target.previous_pointer:
                        raise RehearsalError(
                            "legacy rehearsal predecessor could not be reproduced"
                        )
                else:
                    # Rehearsal roots intentionally contain only compact manifests.
                    # Publish a locally valid v2 pointer and let prepare_release
                    # record that exact scenario predecessor for rollback checks.
                    publish_generation(
                        rehearsal_root, target.previous_pointer.generation_id
                    )
        for companion in manifest.companions:
            if preserve_companion_predecessors:
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

    def terminate_case(
        *,
        case_id: str,
        operation: str,
        checkpoint: str,
        item: str,
        target: Callable[..., None],
        args: tuple[object, ...],
    ) -> dict[str, object]:
        process_id, exit_code = _terminate_at_checkpoint(target, args)
        row: dict[str, object] = {
            "case_id": case_id,
            "checkpoint": checkpoint,
            "exit_code": exit_code,
            "item": item,
            "operation": operation,
            "process_id": process_id,
        }
        process_terminations.append(row)
        return row

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
                raise OSError(f"injected failure at {step}:{item}")

        if failure_mode == "PROCESS_TERMINATION":
            termination = terminate_case(
                case_id=f"commit-boundary-{index:02d}",
                operation="COMMIT",
                checkpoint=failure_step,
                item=failure_item,
                target=_commit_until_terminated,
                args=(
                    scenario / "c",
                    prepared.release_id,
                    roots,
                    proposed_companions,
                    failure_step,
                    failure_item,
                ),
            )
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
            try:
                commit_release(
                    coordination_root=scenario / "c",
                    release_id=prepared.release_id,
                    roots=roots,
                    companion_contents=proposed_companions,
                    fault_hook=fail,
                )
            except ReleaseCommitError:
                pass
            else:
                raise RehearsalError("injected boundary did not fail")
        _, journal = read_release(scenario / "c", prepared.release_id)
        if journal.state is not ReleaseState.ROLLED_BACK:
            raise RehearsalError("failed release did not roll back")
        for target in prepared.targets:
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
        row: dict[str, object] = {
            "failure_item": failure_item,
            "failure_mode": failure_mode,
            "failure_step": failure_step,
            "journal_state": journal.state.value,
            "restored": True,
        }
        if failure_mode == "PROCESS_TERMINATION":
            row.update(
                {
                    "exit_code": termination["exit_code"],
                    "process_id": termination["process_id"],
                }
            )
        rows.append(row)

    # Prove rollback can itself be interrupted with effects still outstanding and
    # resumed. Keep scenario directories deliberately short. Generation identifiers
    # are already 64 characters, and descriptive names can exceed legacy MAX_PATH.
    if len(manifest.targets) < 2:
        raise RehearsalError("recovery rehearsal requires at least two targets")
    if not manifest.companions:
        raise RehearsalError("recovery rehearsal requires a companion boundary")

    def resume_interrupted_rollback(
        scenario: Path,
        release: ReleaseManifest,
        scenario_roots: dict[str, Path],
    ) -> ReleaseJournal:
        _, interrupted = read_release(scenario / "c", release.release_id)
        if interrupted.state is not ReleaseState.ROLLING_BACK:
            raise RehearsalError("interrupted rollback journal state differs")
        try:
            recover_release(
                coordination_root=scenario / "c",
                release_id=release.release_id,
                roots=scenario_roots,
            )
        except ReleaseConflictError:
            pass
        else:
            raise RehearsalError("rollback stale lock did not require confirmation")
        resumed_journal = recover_release(
            coordination_root=scenario / "c",
            release_id=release.release_id,
            roots=scenario_roots,
            clear_stale_lock=True,
        )
        if resumed_journal.state is not ReleaseState.ROLLED_BACK:
            raise RehearsalError("interrupted rollback did not resume")
        return resumed_journal

    def require_predecessor_graph(
        release: ReleaseManifest, scenario_roots: dict[str, Path]
    ) -> None:
        for target in release.targets:
            root = scenario_roots[target.root_id]
            actual = read_current(root) if (root / "CURRENT.json").exists() else None
            if actual != target.previous_pointer:
                raise RehearsalError("resumed rollback pointer graph differs")
        for companion in release.companions:
            path = scenario_roots[companion.root_id] / PurePosixPath(
                companion.relative_path
            )
            actual_hash = _file_hash(path) if path.exists() else None
            if actual_hash != companion.previous_sha256:
                raise RehearsalError("resumed rollback companion graph differs")

    rollback_rows: list[dict[str, object]] = []

    # Apply two targets, restore the later one, terminate, and prove that the earlier
    # target remains proposed until the resumed rollback completes it.
    # Keep the scenario directory deliberately short.  Generation identifiers are
    # already 64 characters, and a descriptive directory name can push otherwise
    # valid rehearsal paths beyond the legacy Windows MAX_PATH boundary.
    rollback_scenario = output_root / "rb"
    index += 1
    roots, prepared = prepare_scenario(rollback_scenario)
    first_target = manifest.targets[0].root_id
    second_target = manifest.targets[1].root_id
    terminate_case(
        case_id="rollback-target-setup",
        operation="COMMIT",
        checkpoint="after-target",
        item=second_target,
        target=_commit_until_terminated,
        args=(
            rollback_scenario / "c",
            prepared.release_id,
            roots,
            proposed_companions,
            "after-target",
            second_target,
        ),
    )
    target_termination = terminate_case(
        case_id="rollback-target-with-work-remaining",
        operation="RECOVER",
        checkpoint="after-target-restore",
        item=second_target,
        target=_recover_until_terminated,
        args=(
            rollback_scenario / "c",
            prepared.release_id,
            roots,
            True,
            "after-target-restore",
            second_target,
        ),
    )
    second_pointer_path = roots[second_target] / "CURRENT.json"
    second_actual = (
        read_current(roots[second_target]) if second_pointer_path.exists() else None
    )
    first_pointer_path = roots[first_target] / "CURRENT.json"
    first_actual = (
        read_current(roots[first_target]) if first_pointer_path.exists() else None
    )
    if second_actual != next(
        item.previous_pointer
        for item in prepared.targets
        if item.root_id == second_target
    ) or first_actual is None or first_actual.generation_id != next(
        item.proposed_generation_id
        for item in prepared.targets
        if item.root_id == first_target
    ):
        raise RehearsalError(
            "target rollback interruption left the wrong partial graph"
        )
    resumed = resume_interrupted_rollback(rollback_scenario, prepared, roots)
    require_predecessor_graph(prepared, roots)
    rollback_rows.append(
        {
            "case_id": "TARGET_WITH_WORK_REMAINING",
            "exit_code": target_termination["exit_code"],
            "interrupted_state": ReleaseState.ROLLING_BACK.value,
            "process_id": target_termination["process_id"],
            "remaining_effect": first_target,
            "resumed_state": resumed.state.value,
            "status": "PASS",
        }
    )

    # Exercise both companion predecessor branches. In each case the companion is
    # restored first, all target pointers remain proposed, and a fresh process must
    # finish their rollback.
    companion_plan = manifest.companions[0]
    for branch, preserves_predecessor in (
        ("RESTORE_PREVIOUS", True),
        ("DELETE_PREVIOUSLY_ABSENT", False),
    ):
        scenario = output_root / "rc" / ("p" if preserves_predecessor else "a")
        scenario_roots, scenario_release = prepare_scenario(
            scenario,
            preserve_companion_predecessors=preserves_predecessor,
        )
        terminate_case(
            case_id=f"rollback-companion-{branch.lower()}-setup",
            operation="COMMIT",
            checkpoint="after-companion",
            item=companion_plan.companion_id,
            target=_commit_until_terminated,
            args=(
                scenario / "c",
                scenario_release.release_id,
                scenario_roots,
                proposed_companions,
                "after-companion",
                companion_plan.companion_id,
            ),
        )
        companion_termination = terminate_case(
            case_id=f"rollback-companion-{branch.lower()}-with-targets-remaining",
            operation="RECOVER",
            checkpoint="after-companion-restore",
            item=companion_plan.companion_id,
            target=_recover_until_terminated,
            args=(
                scenario / "c",
                scenario_release.release_id,
                scenario_roots,
                True,
                "after-companion-restore",
                companion_plan.companion_id,
            ),
        )
        scenario_companion = next(
            item
            for item in scenario_release.companions
            if item.companion_id == companion_plan.companion_id
        )
        companion_path = scenario_roots[scenario_companion.root_id] / PurePosixPath(
            scenario_companion.relative_path
        )
        actual_companion_hash = (
            _file_hash(companion_path) if companion_path.exists() else None
        )
        targets_still_proposed = all(
            read_current(scenario_roots[item.root_id]).generation_id
            == item.proposed_generation_id
            for item in scenario_release.targets
        )
        if (
            actual_companion_hash != scenario_companion.previous_sha256
            or not targets_still_proposed
        ):
            raise RehearsalError(
                "companion rollback interruption left the wrong partial graph"
            )
        companion_resumed = resume_interrupted_rollback(
            scenario, scenario_release, scenario_roots
        )
        require_predecessor_graph(scenario_release, scenario_roots)
        rollback_rows.append(
            {
                "case_id": f"COMPANION_{branch}_WITH_TARGETS_REMAINING",
                "exit_code": companion_termination["exit_code"],
                "interrupted_state": ReleaseState.ROLLING_BACK.value,
                "process_id": companion_termination["process_id"],
                "remaining_effects": len(scenario_release.targets),
                "resumed_state": companion_resumed.state.value,
                "status": "PASS",
            }
        )

    def require_stale_confirmation(
        scenario: Path, release: ReleaseManifest, scenario_roots: dict[str, Path]
    ) -> None:
        try:
            recover_release(
                coordination_root=scenario / "c",
                release_id=release.release_id,
                roots=scenario_roots,
            )
        except ReleaseConflictError:
            return
        raise RehearsalError("terminal stale lock did not require confirmation")

    lock_rows = []
    prepared_scenario = output_root / "l" / "p"
    prepared_roots, prepared_release = prepare_scenario(prepared_scenario)
    prepared_termination = terminate_case(
        case_id="stale-lock-prepared",
        operation="COMMIT",
        checkpoint="after-lock-acquire",
        item=prepared_release.release_id,
        target=_commit_until_terminated,
        args=(
            prepared_scenario / "c",
            prepared_release.release_id,
            prepared_roots,
            proposed_companions,
            "after-lock-acquire",
            prepared_release.release_id,
        ),
    )
    _, prepared_journal = read_release(
        prepared_scenario / "c", prepared_release.release_id
    )
    if prepared_journal.state is not ReleaseState.PREPARED:
        raise RehearsalError("pre-commit termination journal differs")
    require_stale_confirmation(prepared_scenario, prepared_release, prepared_roots)
    prepared_recovered = recover_release(
        coordination_root=prepared_scenario / "c",
        release_id=prepared_release.release_id,
        roots=prepared_roots,
        clear_stale_lock=True,
    )
    lock_rows.append(
        {
            "exit_code": prepared_termination["exit_code"],
            "process_id": prepared_termination["process_id"],
            "recovered_state": prepared_recovered.state.value,
            "stale_state": ReleaseState.PREPARED.value,
            "status": "PASS",
        }
    )

    committed_scenario = output_root / "l" / "c"
    committed_roots, committed_release = prepare_scenario(committed_scenario)
    committed_termination = terminate_case(
        case_id="stale-lock-committed",
        operation="COMMIT",
        checkpoint="after-committed-journal",
        item=committed_release.release_id,
        target=_commit_until_terminated,
        args=(
            committed_scenario / "c",
            committed_release.release_id,
            committed_roots,
            proposed_companions,
            "after-committed-journal",
            committed_release.release_id,
        ),
    )
    require_stale_confirmation(committed_scenario, committed_release, committed_roots)
    committed_recovered = recover_release(
        coordination_root=committed_scenario / "c",
        release_id=committed_release.release_id,
        roots=committed_roots,
        clear_stale_lock=True,
    )
    lock_rows.append(
        {
            "exit_code": committed_termination["exit_code"],
            "process_id": committed_termination["process_id"],
            "recovered_state": committed_recovered.state.value,
            "stale_state": ReleaseState.COMMITTED.value,
            "status": "PASS",
        }
    )

    rolled_scenario = output_root / "l" / "r"
    rolled_roots, rolled_release = prepare_scenario(rolled_scenario)
    terminate_case(
        case_id="stale-lock-rolled-back-setup",
        operation="COMMIT",
        checkpoint="after-target",
        item=first_target,
        target=_commit_until_terminated,
        args=(
            rolled_scenario / "c",
            rolled_release.release_id,
            rolled_roots,
            proposed_companions,
            "after-target",
            first_target,
        ),
    )
    rolled_termination = terminate_case(
        case_id="stale-lock-rolled-back",
        operation="RECOVER",
        checkpoint="after-rolled-back-journal",
        item=rolled_release.release_id,
        target=_recover_until_terminated,
        args=(
            rolled_scenario / "c",
            rolled_release.release_id,
            rolled_roots,
            True,
            "after-rolled-back-journal",
            rolled_release.release_id,
        ),
    )
    require_stale_confirmation(rolled_scenario, rolled_release, rolled_roots)
    rolled_recovered = recover_release(
        coordination_root=rolled_scenario / "c",
        release_id=rolled_release.release_id,
        roots=rolled_roots,
        clear_stale_lock=True,
    )
    lock_rows.append(
        {
            "exit_code": rolled_termination["exit_code"],
            "process_id": rolled_termination["process_id"],
            "recovered_state": rolled_recovered.state.value,
            "stale_state": ReleaseState.ROLLED_BACK.value,
            "status": "PASS",
        }
    )

    conflict_rows = []
    pointer_scenario = output_root / "x" / "p"
    pointer_roots, pointer_release = prepare_scenario(pointer_scenario)
    pointer_termination = terminate_case(
        case_id="recovery-failed-pointer-setup",
        operation="COMMIT",
        checkpoint="after-target",
        item=first_target,
        target=_commit_until_terminated,
        args=(
            pointer_scenario / "c",
            pointer_release.release_id,
            pointer_roots,
            proposed_companions,
            "after-target",
            first_target,
        ),
    )
    unexpected = _synthetic_manifest("third-pointer-state", manifest.created_at.date())
    stage_generation(pointer_roots[first_target], unexpected)
    publish_generation(pointer_roots[first_target], unexpected.generation_id)
    try:
        recover_release(
            coordination_root=pointer_scenario / "c",
            release_id=pointer_release.release_id,
            roots=pointer_roots,
            clear_stale_lock=True,
        )
    except ReleaseCommitError:
        pass
    else:
        raise RehearsalError("third pointer state did not fail recovery")
    _, pointer_failed = read_release(pointer_scenario / "c", pointer_release.release_id)
    if (
        pointer_failed.state is not ReleaseState.RECOVERY_FAILED
        or read_current(pointer_roots[first_target]).generation_id
        != unexpected.generation_id
    ):
        raise RehearsalError("pointer conflict was not preserved")
    publish_generation(
        pointer_roots[first_target],
        next(
            item.proposed_generation_id
            for item in pointer_release.targets
            if item.root_id == first_target
        ),
    )
    pointer_resumed = recover_release(
        coordination_root=pointer_scenario / "c",
        release_id=pointer_release.release_id,
        roots=pointer_roots,
    )
    conflict_rows.append(
        {
            "conflict_kind": "POINTER",
            "exit_code": pointer_termination["exit_code"],
            "failed_state": pointer_failed.state.value,
            "process_id": pointer_termination["process_id"],
            "resumed_state": pointer_resumed.state.value,
            "status": "PASS",
        }
    )

    companion_scenario = output_root / "x" / "c"
    companion_roots, companion_release = prepare_scenario(companion_scenario)
    conflict_termination = terminate_case(
        case_id="recovery-failed-companion-setup",
        operation="COMMIT",
        checkpoint="after-companion",
        item=companion_plan.companion_id,
        target=_commit_until_terminated,
        args=(
            companion_scenario / "c",
            companion_release.release_id,
            companion_roots,
            proposed_companions,
            "after-companion",
            companion_plan.companion_id,
        ),
    )
    companion_path = (
        companion_roots[companion_plan.root_id] / companion_plan.relative_path
    )
    conflicting_content = b"external third companion state\n"
    companion_path.write_bytes(conflicting_content)
    try:
        recover_release(
            coordination_root=companion_scenario / "c",
            release_id=companion_release.release_id,
            roots=companion_roots,
            clear_stale_lock=True,
        )
    except ReleaseCommitError:
        pass
    else:
        raise RehearsalError("third companion state did not fail recovery")
    _, companion_failed = read_release(
        companion_scenario / "c", companion_release.release_id
    )
    if (
        companion_failed.state is not ReleaseState.RECOVERY_FAILED
        or companion_path.read_bytes() != conflicting_content
    ):
        raise RehearsalError("companion conflict was not preserved")
    companion_path.write_bytes(proposed_companions[companion_plan.companion_id])
    companion_resumed = recover_release(
        coordination_root=companion_scenario / "c",
        release_id=companion_release.release_id,
        roots=companion_roots,
    )
    conflict_rows.append(
        {
            "conflict_kind": "COMPANION",
            "exit_code": conflict_termination["exit_code"],
            "failed_state": companion_failed.state.value,
            "process_id": conflict_termination["process_id"],
            "resumed_state": companion_resumed.state.value,
            "status": "PASS",
        }
    )

    source_after, _ = _source_state(spec, release_id)
    if source_after != source_before:
        raise RehearsalError("accepted source state changed during rehearsal")
    return {
        "boundary_count": len(rows),
        "boundaries": rows,
        "caught_boundary_count": len(caught_boundaries),
        "crash_boundary_count": len(crash_boundaries),
        "lock_lifecycle": lock_rows,
        "process_termination_count": len(process_terminations),
        "process_termination_method": "OPERATING_SYSTEM_PROCESS_TERMINATION",
        "process_terminations": process_terminations,
        "recovery_failed_conflicts": conflict_rows,
        "rollback_interruption": {
            "case_count": len(rollback_rows),
            "interrupted_state": ReleaseState.ROLLING_BACK.value,
            "resumed_state": resumed.state.value,
            "status": "PASS",
        },
        "rollback_interruptions": rollback_rows,
        "source_state_sha256": hashlib.sha256(
            canonical_json_bytes(source_before)
        ).hexdigest(),
        "source_unchanged": True,
        "status": "PASS",
    }


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

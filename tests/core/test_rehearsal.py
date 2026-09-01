from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CompanionInput,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationManifest,
    GateEvidence,
    PipelineCompanion,
    PipelineSpec,
    PointerIntegrityError,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core import rehearsal as rehearsal_module
from quant_engine.core.rehearsal import (
    RehearsalError,
    rehearse_dated_rollforwards,
    rehearse_release_boundaries,
    run_recovery_rehearsal,
)


_HARNESS_IDENTITY = {
    "code_commit": "c" * 40,
    "core_module_sha256": "d" * 64,
    "entrypoint_sha256": "e" * 64,
    "python_executable_sha256": "f" * 64,
    "python_version": "3.11.15",
    "runtime_platform": "synthetic-test-runtime",
}


@pytest.fixture(autouse=True)
def _skip_posix_rehearsal_behavior_on_windows(request: pytest.FixtureRequest) -> None:
    if os.name == "nt" and "windows" not in request.node.name:
        pytest.skip("POSIX coordinated rehearsal behavior is verified on POSIX")


def test_windows_rehearsal_entry_points_return_unavailable_without_output_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "rehearsal-output"
    monkeypatch.setattr(rehearsal_module, "_is_windows_platform", lambda: True)

    results = (
        rehearse_release_boundaries(
            spec=object(), release_id="a" * 64, output_root=output
        ),
        rehearse_dated_rollforwards(output),
        run_recovery_rehearsal(
            spec=object(),
            release_id="a" * 64,
            output_root=output,
            harness_identity={},
        ),
    )

    assert all(
        result
        == {
            "reason": "WINDOWS_NAMESPACE_UNTRUSTED",
            "status": "PUBLICATION_UNAVAILABLE",
        }
        for result in results
    )
    assert not output.exists()


def _manifest(label: str, day: date) -> GenerationManifest:
    content = (label + "\n").encode()
    return GenerationManifest.build(
        as_of_date=day,
        adapter=AdapterIdentity("rehearsal-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=label,
            fact_types=("synthetic",),
            observed_start=day,
            observed_end=day,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=day,
                schema_name="synthetic",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _accepted_release(
    tmp_path: Path,
    *,
    legacy_predecessor_pointer: bool = False,
    with_predecessors: bool = True,
) -> tuple[PipelineSpec, str]:
    roots = {"excel": tmp_path / "excel", "panel": tmp_path / "panel"}
    previous = {
        root_id: _manifest(f"{root_id}-previous", date(2026, 7, 30))
        for root_id in roots
    }
    proposed = {
        root_id: _manifest(f"{root_id}-proposed", date(2026, 7, 31))
        for root_id in roots
    }
    coordination = tmp_path / "coordination"
    for root_id in roots:
        if with_predecessors:
            stage_generation(roots[root_id], previous[root_id])
            publish_generation(roots[root_id], previous[root_id].generation_id)
        stage_generation(roots[root_id], proposed[root_id])
    if with_predecessors:
        (roots["panel"] / "index.html").write_bytes(b"previous launcher\n")
    proposed_launcher = tmp_path / "proposed-launcher.html"
    proposed_launcher.write_bytes(b"proposed launcher\n")
    if legacy_predecessor_pointer:
        baseline = prepare_release(
            coordination_root=coordination,
            roots=roots,
            proposed_generations={
                root_id: manifest.generation_id
                for root_id, manifest in previous.items()
            },
            code_commit="a" * 40,
            created_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
            gates=(GateEvidence("test", "b" * 64),),
        )
        commit_release(
            coordination_root=coordination,
            release_id=baseline.release_id,
            roots=roots,
        )
        for root_id in roots:
            current = read_current(roots[root_id])
            (roots[root_id] / "CURRENT.json").write_bytes(
                CurrentPointer(
                    generation_id=current.generation_id,
                    manifest_sha256=current.manifest_sha256,
                ).to_json_bytes()
            )
    release = prepare_release(
        coordination_root=coordination,
        roots=roots,
        proposed_generations={
            root_id: manifest.generation_id for root_id, manifest in proposed.items()
        },
        code_commit="a" * 40,
        created_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
        gates=(
            GateEvidence("test", "b" * 64),
            *(
                (GateEvidence("predecessor-authority-repair", "c" * 64),)
                if legacy_predecessor_pointer
                else ()
            ),
        ),
        companions=(
            CompanionInput(
                "panel-launcher",
                "panel",
                "index.html",
                proposed_launcher.read_bytes(),
            ),
        ),
        allow_invalid_previous_artifact_inventory=legacy_predecessor_pointer,
    )
    commit_release(
        coordination_root=coordination,
        release_id=release.release_id,
        roots=roots,
        companion_contents={"panel-launcher": proposed_launcher.read_bytes()},
    )
    spec = PipelineSpec(
        pipeline_id="accepted-test-release",
        as_of_date=date(2026, 7, 31),
        code_commit="a" * 40,
        created_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
        coordination_root=coordination,
        state_root=tmp_path / "state",
        roots=roots,
        nodes=(),
        gates=(),
        companions=(
            PipelineCompanion(
                "panel-launcher",
                "panel",
                "index.html",
                proposed_launcher,
                hashlib.sha256(proposed_launcher.read_bytes()).hexdigest(),
            ),
        ),
    )
    return spec, release.release_id


def test_complete_recovery_rehearsal_passes(tmp_path: Path) -> None:
    spec, release_id = _accepted_release(tmp_path)
    output = tmp_path / "rehearsal"

    result = run_recovery_rehearsal(
        spec=spec,
        release_id=release_id,
        output_root=output,
        harness_identity=_HARNESS_IDENTITY,
    )

    assert result["status"] == "PASS"
    assert result["schema_version"] == 3
    assert result["harness_identity"] == _HARNESS_IDENTITY
    boundary = result["boundary_rehearsal"]
    assert isinstance(boundary, dict)
    assert boundary["boundary_count"] == 15
    assert boundary["caught_boundary_count"] == 9
    assert boundary["crash_boundary_count"] == 6
    assert boundary["rollback_interruption"]["status"] == "PASS"
    assert boundary["rollback_interruption"]["case_count"] == 3
    assert {item["case_id"] for item in boundary["rollback_interruptions"]} == {
        "TARGET_WITH_WORK_REMAINING",
        "COMPANION_RESTORE_PREVIOUS_WITH_TARGETS_REMAINING",
        "COMPANION_DELETE_PREVIOUSLY_ABSENT_WITH_TARGETS_REMAINING",
    }
    assert all(
        item["interrupted_state"] == "ROLLING_BACK"
        and item["resumed_state"] == "ROLLED_BACK"
        and item["process_id"] > 0
        and item["exit_code"] != 0
        for item in boundary["rollback_interruptions"]
    )
    assert (
        boundary["process_termination_method"] == "OPERATING_SYSTEM_PROCESS_TERMINATION"
    )
    terminations = boundary["process_terminations"]
    assert boundary["process_termination_count"] == len(terminations) == 18
    assert all(
        item["process_id"] > 0 and item["exit_code"] != 0 for item in terminations
    )
    assert all(
        item["process_id"] > 0 and item["exit_code"] != 0
        for item in boundary["boundaries"]
        if item["failure_mode"] == "PROCESS_TERMINATION"
    )
    assert {
        (item["stale_state"], item["recovered_state"])
        for item in boundary["lock_lifecycle"]
    } == {
        ("PREPARED", "PREPARED"),
        ("COMMITTED", "COMMITTED"),
        ("ROLLED_BACK", "ROLLED_BACK"),
    }
    assert {
        (item["conflict_kind"], item["failed_state"], item["resumed_state"])
        for item in boundary["recovery_failed_conflicts"]
    } == {
        ("POINTER", "RECOVERY_FAILED", "ROLLED_BACK"),
        ("COMPANION", "RECOVERY_FAILED", "ROLLED_BACK"),
    }
    assert boundary["source_unchanged"] is True
    dated = result["dated_rollforwards"]
    assert isinstance(dated, dict)
    assert dated["append_only_correction"] is True
    assert dated["restored_release_two"] is True
    assert Path(str(result["evidence_path"])).is_file()
    durable = json.loads(
        (output / "recovery-rehearsal.json").read_text(encoding="utf-8")
    )
    assert durable["boundary_rehearsal"]["process_terminations"] == terminations
    assert (
        durable["boundary_rehearsal"]["rollback_interruptions"]
        == boundary["rollback_interruptions"]
    )
    assert (output / "rb").is_dir()
    assert (output / "rc" / "p").is_dir()
    assert (output / "rc" / "a").is_dir()
    assert not (output / "rollback-interruption").exists()


def test_rehearsal_refuses_nonempty_output(tmp_path: Path) -> None:
    spec, release_id = _accepted_release(tmp_path)
    output = tmp_path / "rehearsal"
    output.mkdir()
    (output / "existing.txt").write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(RehearsalError, match="absent or empty"):
        run_recovery_rehearsal(
            spec=spec,
            release_id=release_id,
            output_root=output,
            harness_identity=_HARNESS_IDENTITY,
        )


def test_recovery_rehearsal_fails_closed_on_legacy_predecessor_pointer(
    tmp_path: Path,
) -> None:
    spec, release_id = _accepted_release(tmp_path, legacy_predecessor_pointer=True)

    with pytest.raises(PointerIntegrityError, match="legacy pointer schema"):
        run_recovery_rehearsal(
            spec=spec,
            release_id=release_id,
            output_root=tmp_path / "legacy-pointer-rehearsal",
            harness_identity=_HARNESS_IDENTITY,
        )


def test_recovery_rehearsal_supports_release_without_predecessors(
    tmp_path: Path,
) -> None:
    spec, release_id = _accepted_release(tmp_path, with_predecessors=False)

    result = run_recovery_rehearsal(
        spec=spec,
        release_id=release_id,
        output_root=tmp_path / "no-predecessor-rehearsal",
        harness_identity=_HARNESS_IDENTITY,
    )

    assert result["status"] == "PASS"
    assert result["schema_version"] == 3
    assert result["harness_identity"] == _HARNESS_IDENTITY
    assert result["boundary_rehearsal"]["source_unchanged"] is True
    assert result["boundary_rehearsal"]["rollback_interruption"]["status"] == "PASS"

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
    GenerationManifest,
    GateEvidence,
    PipelineCompanion,
    PipelineSpec,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    publish_generation,
    stage_generation,
)
from quant_engine.core.rehearsal import RehearsalError, run_recovery_rehearsal


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


def _accepted_release(tmp_path: Path) -> tuple[PipelineSpec, str]:
    roots = {"excel": tmp_path / "excel", "panel": tmp_path / "panel"}
    previous = {
        root_id: _manifest(f"{root_id}-previous", date(2026, 7, 30))
        for root_id in roots
    }
    proposed = {
        root_id: _manifest(f"{root_id}-proposed", date(2026, 7, 31))
        for root_id in roots
    }
    for root_id in roots:
        stage_generation(roots[root_id], previous[root_id])
        publish_generation(roots[root_id], previous[root_id].generation_id)
        stage_generation(roots[root_id], proposed[root_id])
    (roots["panel"] / "index.html").write_bytes(b"previous launcher\n")
    proposed_launcher = tmp_path / "proposed-launcher.html"
    proposed_launcher.write_bytes(b"proposed launcher\n")
    coordination = tmp_path / "coordination"
    release = prepare_release(
        coordination_root=coordination,
        roots=roots,
        proposed_generations={
            root_id: manifest.generation_id for root_id, manifest in proposed.items()
        },
        code_commit="a" * 40,
        created_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
        gates=(GateEvidence("test", "b" * 64),),
        companions=(
            CompanionInput(
                "panel-launcher",
                "panel",
                "index.html",
                proposed_launcher.read_bytes(),
            ),
        ),
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
    )

    assert result["status"] == "PASS"
    boundary = result["boundary_rehearsal"]
    assert isinstance(boundary, dict)
    assert boundary["boundary_count"] == 15
    assert boundary["caught_boundary_count"] == 9
    assert boundary["crash_boundary_count"] == 6
    assert boundary["rollback_interruption"]["status"] == "PASS"
    assert boundary["source_unchanged"] is True
    dated = result["dated_rollforwards"]
    assert isinstance(dated, dict)
    assert dated["append_only_correction"] is True
    assert dated["restored_release_two"] is True
    assert Path(str(result["evidence_path"])).is_file()
    assert (output / "rb").is_dir()
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
        )

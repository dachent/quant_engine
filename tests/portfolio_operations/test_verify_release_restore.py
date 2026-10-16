"""Clean-room restore of a committed release reproduces every bound generation by hash."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from pathlib import Path
import sys

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GateEvidence,
    GenerationManifest,
    PipelineSpec,
    SourceArtifact,
    VerificationStatus,
    commit_release,
    prepare_release,
    stage_generation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import verify_release_restore as tool  # noqa: E402

DAY = date(2026, 10, 16)


def _manifest(label: str) -> GenerationManifest:
    content = f"{label}\n".encode()
    return GenerationManifest.build(
        as_of_date=DAY,
        adapter=AdapterIdentity("restore-test", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=label,
            fact_types=("synthetic",),
            observed_start=DAY,
            observed_end=DAY,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=f"inputs/{label}.txt",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=DAY,
                schema_name="synthetic",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )


def _committed(tmp_path: Path) -> tuple[PipelineSpec, dict[str, Path]]:
    roots = {"alpha": tmp_path / "alpha", "beta": tmp_path / "beta"}
    generations = {}
    for name, root in roots.items():
        manifest = _manifest(name)
        directory = stage_generation(root, manifest).parent
        (directory / "catalog.json").write_bytes(f"catalog {name}\n".encode())
        generations[name] = manifest.generation_id
    coordination = tmp_path / "coordination"
    release = prepare_release(
        coordination_root=coordination,
        roots=roots,
        proposed_generations=generations,
        code_commit="a" * 40,
        created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
        gates=(GateEvidence("test", "b" * 64),),
    )
    commit_release(
        coordination_root=coordination,
        release_id=release.release_id,
        roots=roots,
        skip_release_validation=True,
    )
    spec = PipelineSpec(
        pipeline_id="restore-test",
        as_of_date=DAY,
        code_commit="a" * 40,
        created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
        coordination_root=coordination,
        state_root=tmp_path / "state",
        roots=roots,
        nodes=(),
        gates=(),
        companions=(),
    )
    return spec, {
        name: roots[name] / "generations" / generations[name] for name in roots
    }


def test_a_committed_release_restores_in_a_clean_room_by_hash(tmp_path: Path) -> None:
    spec, directories = _committed(tmp_path)
    catalog = directories["alpha"] / "catalog.json"
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(
        f"[alpha] {hashlib.sha256(catalog.read_bytes()).hexdigest()} *catalog.json\n",
        encoding="utf-8",
    )

    result = tool.restore(spec, tmp_path / "clean", sums)

    assert result["read_current"] == "PASS"
    assert {row["root_id"] for row in result["targets"]} == {"alpha", "beta"}
    assert result["sums_matched"] == ["alpha/catalog.json -> alpha/catalog.json"]
    assert (tmp_path / "clean" / "roots" / "alpha" / "CURRENT.json").is_file()


def test_a_tampered_durable_generation_does_not_restore(tmp_path: Path) -> None:
    spec, directories = _committed(tmp_path)
    (directories["beta"] / "catalog.json").write_bytes(b"tampered\n")
    try:
        tool.restore(spec, tmp_path / "clean")
    except tool.RestoreError as error:
        assert "inventory hash differs" in str(error)
    else:
        raise AssertionError("a tampered generation restored")


def test_an_absent_generation_and_a_dirty_clean_room_are_refused(
    tmp_path: Path,
) -> None:
    spec, directories = _committed(tmp_path)
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "x").write_text("x")
    try:
        tool.restore(spec, dirty)
    except tool.RestoreError as error:
        assert "must be empty" in str(error)
    else:
        raise AssertionError("a dirty clean room was accepted")
    import shutil

    shutil.rmtree(directories["alpha"])
    try:
        tool.restore(spec, tmp_path / "clean2")
    except tool.RestoreError as error:
        assert "absent from the durable store" in str(error)
    else:
        raise AssertionError("an absent generation restored")


def test_sums_missing_from_the_restore_are_refused(tmp_path: Path) -> None:
    spec, _ = _committed(tmp_path)
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"[alpha] {'0' * 64} *catalog.json\n", encoding="utf-8")
    try:
        tool.restore(spec, tmp_path / "clean", sums)
    except tool.RestoreError as error:
        assert "not restored" in str(error)
    else:
        raise AssertionError("an unreproduced sum passed")


def test_a_release_companion_is_restored_and_checked_by_hash(tmp_path: Path) -> None:
    from quant_engine.core import CompanionInput

    roots = {"alpha": tmp_path / "alpha"}
    manifest = _manifest("alpha")
    stage_generation(roots["alpha"], manifest)
    coordination = tmp_path / "coordination"
    content = b"launcher\n"
    release = prepare_release(
        coordination_root=coordination,
        roots=roots,
        proposed_generations={"alpha": manifest.generation_id},
        code_commit="a" * 40,
        created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
        gates=(GateEvidence("test", "b" * 64),),
        companions=(CompanionInput("note", "alpha", "RELEASE_NOTE.json", content),),
    )
    commit_release(
        coordination_root=coordination,
        release_id=release.release_id,
        roots=roots,
        companion_contents={"note": content},
        skip_release_validation=True,
    )
    spec = PipelineSpec(
        pipeline_id="restore-companion-test",
        as_of_date=DAY,
        code_commit="a" * 40,
        created_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
        coordination_root=coordination,
        state_root=tmp_path / "state",
        roots=roots,
        nodes=(),
        gates=(),
        companions=(),
    )
    tool.restore(spec, tmp_path / "clean")
    assert (
        tmp_path / "clean" / "roots" / "alpha" / "RELEASE_NOTE.json"
    ).read_bytes() == content
    (roots["alpha"] / "RELEASE_NOTE.json").write_bytes(b"tampered\n")
    try:
        tool.restore(spec, tmp_path / "clean2")
    except tool.RestoreError as error:
        assert "hash differs" in str(error)
    else:
        raise AssertionError("a tampered companion restored")

from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

import quant_engine.core.publication as publication
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    CurrentPointer,
    GenerationConflictError,
    GenerationManifest,
    ManifestValidationError,
    PointerIntegrityError,
    PublicationRejectedError,
    PublicationState,
    SourceArtifact,
    VerificationStatus,
    publication_state,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)


AS_OF = date(2026, 7, 30)


def _artifact(identifier: str, content: bytes = b"synthetic\n") -> SourceArtifact:
    return SourceArtifact(
        artifact_id=identifier,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        as_of_date=AS_OF,
        schema_name="synthetic-portfolio-input",
        schema_version="1",
    )


def _coverage(
    status: CoverageStatus = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
) -> CoverageClaim:
    gaps = (
        () if status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY else ("NAV",)
    )
    return CoverageClaim(
        status=status,
        boundary="synthetic accounts on the stated as-of date",
        fact_types=("positions", "transactions"),
        observed_start=date(2026, 1, 1),
        observed_end=AS_OF,
        known_gaps=gaps,
    )


def _manifest(
    *,
    coverage: CoverageStatus = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    artifacts: tuple[SourceArtifact, ...] | None = None,
) -> GenerationManifest:
    return GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("synthetic-baseline", "1.0.0"),
        coverage=_coverage(coverage),
        artifacts=artifacts or (_artifact("inputs/tracker.json"),),
        verification=verification,
    )


def test_generation_identity_and_bytes_are_deterministic() -> None:
    first = _artifact("inputs/a.json", b"a")
    second = _artifact("inputs/b.json", b"b")
    left = _manifest(artifacts=(second, first))
    right = _manifest(artifacts=(first, second))

    assert left == right
    assert left.to_json_bytes() == right.to_json_bytes()
    assert (
        left.generation_id
        == hashlib.sha256(
            json.dumps(
                left.identity_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            + b"\n"
        ).hexdigest()
    )
    assert GenerationManifest.from_json_bytes(left.to_json_bytes()) == left


def test_source_artifact_hashes_content_without_recording_a_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-input.json"
    source.write_bytes(b'{"synthetic":true}\n')

    artifact = SourceArtifact.from_path(
        source,
        artifact_id="inputs/tracker.json",
        as_of_date=AS_OF,
        schema_name="synthetic-portfolio-input",
        schema_version="1",
    )

    assert artifact.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert artifact.size_bytes == source.stat().st_size
    assert str(tmp_path) not in artifact.to_dict().values()


def test_manifest_rejects_tampering_unknown_keys_and_private_style_ids() -> None:
    manifest = _manifest()
    decoded = json.loads(manifest.to_json_bytes())
    decoded["adapter"]["version"] = "changed"
    with pytest.raises(ManifestValidationError, match="generation_id"):
        GenerationManifest.from_json_bytes(json.dumps(decoded).encode())

    decoded = json.loads(manifest.to_json_bytes())
    decoded["unexpected"] = True
    with pytest.raises(ManifestValidationError, match="keys differ"):
        GenerationManifest.from_json_bytes(json.dumps(decoded).encode())

    with pytest.raises(ManifestValidationError, match="portable relative"):
        replace(_artifact("safe.json"), artifact_id="C:\\private\\account.csv")


def test_coverage_claims_fail_closed() -> None:
    with pytest.raises(ManifestValidationError, match="must declare at least one gap"):
        CoverageClaim(
            status=CoverageStatus.PARTIAL,
            boundary="synthetic",
            fact_types=("positions",),
        )
    with pytest.raises(ManifestValidationError, match="cannot declare gaps"):
        CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("positions",),
            known_gaps=("missing day",),
        )


def test_stage_is_create_only_and_idempotent(tmp_path: Path) -> None:
    manifest = _manifest()
    first = stage_generation(tmp_path, manifest)
    second = stage_generation(tmp_path, manifest)

    assert first == second
    assert first.read_bytes() == manifest.to_json_bytes()
    assert read_generation(tmp_path, manifest.generation_id) == manifest
    assert (
        publication_state(tmp_path, manifest.generation_id) is PublicationState.STAGED
    )

    first.write_text("different", encoding="utf-8")
    with pytest.raises(GenerationConflictError, match="different"):
        stage_generation(tmp_path, manifest)


def test_stage_retries_transient_directory_rename_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest()
    original_rename = Path.rename
    attempts = 0

    def transient_denial(source: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic sync-provider denial")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", transient_denial)
    monkeypatch.setattr(publication.time, "sleep", lambda _: None)

    staged = stage_generation(tmp_path, manifest)

    assert attempts == 2
    assert staged.read_bytes() == manifest.to_json_bytes()


def test_complete_verified_generation_publishes_atomically(tmp_path: Path) -> None:
    manifest = _manifest()
    stage_generation(tmp_path, manifest)

    pointer = publish_generation(tmp_path, manifest.generation_id)

    assert read_current(tmp_path) == pointer
    assert pointer.generation_id == manifest.generation_id
    assert (
        pointer.manifest_sha256 == hashlib.sha256(manifest.to_json_bytes()).hexdigest()
    )
    assert pointer.state is PublicationState.PUBLISHED
    assert (
        publication_state(tmp_path, manifest.generation_id)
        is PublicationState.PUBLISHED
    )
    before = (tmp_path / "CURRENT.json").read_bytes()
    assert publish_generation(tmp_path, manifest.generation_id) == pointer
    assert (tmp_path / "CURRENT.json").read_bytes() == before


def test_pointer_replace_failure_preserves_previous_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = _manifest()
    replacement = _manifest(
        artifacts=(_artifact("inputs/replacement.json", b"replacement"),)
    )
    stage_generation(tmp_path, accepted)
    stage_generation(tmp_path, replacement)
    publish_generation(tmp_path, accepted.generation_id)
    before = (tmp_path / "CURRENT.json").read_bytes()

    def fail_replace(source: object, target: object) -> None:
        raise OSError("synthetic atomic replace failure")

    monkeypatch.setattr(publication.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic atomic replace failure"):
        publish_generation(tmp_path, replacement.generation_id)

    assert (tmp_path / "CURRENT.json").read_bytes() == before


@pytest.mark.parametrize(
    ("coverage", "verification"),
    [
        (CoverageStatus.PARTIAL, VerificationStatus.VERIFIED),
        (CoverageStatus.FAILED, VerificationStatus.FAILED),
        (
            CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            VerificationStatus.PENDING,
        ),
        (
            CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            VerificationStatus.FAILED,
        ),
    ],
)
def test_unacceptable_generation_cannot_advance_current(
    tmp_path: Path,
    coverage: CoverageStatus,
    verification: VerificationStatus,
) -> None:
    accepted = _manifest()
    rejected = _manifest(
        coverage=coverage,
        verification=verification,
        artifacts=(_artifact("inputs/rejected.json", b"rejected"),),
    )
    stage_generation(tmp_path, accepted)
    publish_generation(tmp_path, accepted.generation_id)
    current_before = (tmp_path / "CURRENT.json").read_bytes()
    stage_generation(tmp_path, rejected)

    with pytest.raises(PublicationRejectedError):
        publish_generation(tmp_path, rejected.generation_id)

    assert (tmp_path / "CURRENT.json").read_bytes() == current_before
    assert read_current(tmp_path).generation_id == accepted.generation_id


def test_current_pointer_detects_manifest_tampering(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest_path = stage_generation(tmp_path, manifest)
    publish_generation(tmp_path, manifest.generation_id)
    manifest_path.write_bytes(manifest.to_json_bytes() + b" ")

    with pytest.raises(PointerIntegrityError, match="hash differs"):
        read_current(tmp_path)


def test_current_pointer_rejects_manually_selected_partial_generation(
    tmp_path: Path,
) -> None:
    partial = _manifest(coverage=CoverageStatus.PARTIAL)
    stage_generation(tmp_path, partial)
    pointer = CurrentPointer(
        generation_id=partial.generation_id,
        manifest_sha256=hashlib.sha256(partial.to_json_bytes()).hexdigest(),
    )
    (tmp_path / "CURRENT.json").write_bytes(pointer.to_json_bytes())

    with pytest.raises(PointerIntegrityError, match="not publication eligible"):
        read_current(tmp_path)


def test_current_pointer_binds_complete_generation_file_inventory(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest_path = stage_generation(tmp_path, manifest)
    payload = manifest_path.parent / "normalized" / "portfolio.json"
    payload.parent.mkdir()
    payload.write_bytes(b'{"accepted":true}\n')

    pointer = publish_generation(tmp_path, manifest.generation_id)

    assert pointer.pointer_schema_version == 2
    assert pointer.artifact_inventory_sha256 is not None
    assert read_current(tmp_path) == pointer

    payload.write_bytes(b'{"accepted":false}\n')
    with pytest.raises(PointerIntegrityError, match="inventory hash differs"):
        read_current(tmp_path)


def test_current_pointer_detects_undeclared_file_addition(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest_path = stage_generation(tmp_path, manifest)
    publish_generation(tmp_path, manifest.generation_id)

    (manifest_path.parent / "late-file.txt").write_bytes(b"not accepted\n")

    with pytest.raises(PointerIntegrityError, match="inventory hash differs"):
        read_current(tmp_path)


def test_read_generation_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    """A valid manifest staged under one identity must not resolve under a
    different requested generation id."""
    manifest = _manifest()
    staged = stage_generation(tmp_path, manifest)
    wrong_id = "f" * 64
    wrong_dir = tmp_path / "generations" / wrong_id
    wrong_dir.mkdir(parents=True)
    (wrong_dir / "generation.json").write_bytes(staged.read_bytes())

    with pytest.raises(
        GenerationConflictError,
        match="identity differs from the requested generation",
    ):
        read_generation(tmp_path, wrong_id)


def test_read_generation_rejects_linked_generation_path_components(
    tmp_path: Path,
) -> None:
    """Linked generations root or generation directory must fail closed."""
    manifest = _manifest()
    stage_generation(tmp_path, manifest)

    linked_root = tmp_path / "linked-root"
    linked_root.mkdir()
    try:
        (linked_root / "generations").symlink_to(
            tmp_path / "generations", target_is_directory=True
        )
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(
        GenerationConflictError, match="generations root must not be a symlink"
    ):
        read_generation(linked_root, manifest.generation_id)

    linked_generation = tmp_path / "linked-generation"
    (linked_generation / "generations").mkdir(parents=True)
    (linked_generation / "generations" / manifest.generation_id).symlink_to(
        tmp_path / "generations" / manifest.generation_id,
        target_is_directory=True,
    )
    with pytest.raises(
        GenerationConflictError, match="generation directory must not be a symlink"
    ):
        read_generation(linked_generation, manifest.generation_id)

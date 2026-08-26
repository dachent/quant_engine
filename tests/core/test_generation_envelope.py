from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

import quant_engine.core.publication as publication
from quant_engine.core.strict_json import StrictJSONError
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
    generation_inventory_sha256,
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


def _envelope_manifest_and_payloads() -> tuple[GenerationManifest, dict[str, bytes]]:
    """A valid two-payload subject set with the required independent report."""
    facts = publication.canonical_json_bytes({"account_count": 18, "status": "BLOCKED"})
    report = publication.canonical_json_bytes(
        {"status": "VERIFIED", "verification_schema_version": 1}
    )
    artifacts = (
        _artifact("authority/selected-input-identities.json", facts),
        _artifact("verification/independent-verification.json", report),
    )
    return _manifest(artifacts=artifacts), {
        artifacts[1].artifact_id: report,
        artifacts[0].artifact_id: facts,
    }


def test_prepare_generation_bundle_is_deterministic_non_circular_and_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payloads = _envelope_manifest_and_payloads()

    def filesystem_trap(*_: object, **__: object) -> object:
        raise AssertionError("in-memory preparation must not call the filesystem")

    monkeypatch.setattr(publication, "Path", filesystem_trap)
    monkeypatch.setattr("builtins.open", filesystem_trap)
    for dependency in ("os", "shutil", "tempfile", "time"):
        monkeypatch.setattr(publication, dependency, filesystem_trap)
    for helper in (
        "_generation_path",
        "_sync_directory",
        "_validate_root",
        "_write_and_sync",
        "generation_inventory_sha256",
        "read_generation",
        "stage_generation",
    ):
        monkeypatch.setattr(publication, helper, filesystem_trap)

    prepared = publication._prepare_generation_bundle(manifest, payloads)

    expected_payload_paths = tuple(sorted(payloads))
    assert tuple(prepared.final_files) == (
        "authority/selected-input-identities.json",
        "generation-inventory.json",
        "generation-receipt.json",
        "generation.json",
        "verification/independent-verification.json",
    )
    assert prepared.final_files["generation.json"] == manifest.to_json_bytes()
    assert prepared.final_files["authority/selected-input-identities.json"] == payloads[
        "authority/selected-input-identities.json"
    ]
    prepared_payload = prepared.final_files["authority/selected-input-identities.json"]
    payloads["authority/selected-input-identities.json"] = b'{"changed":true}\n'
    assert prepared.final_files["authority/selected-input-identities.json"] == prepared_payload
    with pytest.raises(TypeError):
        prepared.final_files["later.json"] = b"{}\n"  # type: ignore[index]

    inventory = json.loads(prepared.final_files["generation-inventory.json"])
    assert inventory["generation_id"] == manifest.generation_id
    assert inventory["inventory_schema_version"] == 1
    assert [entry["relative_path"] for entry in inventory["files"]] == [
        "authority/selected-input-identities.json",
        "generation.json",
        "verification/independent-verification.json",
    ]
    assert "generation-inventory.json" not in {
        entry["relative_path"] for entry in inventory["files"]
    }
    assert "generation-receipt.json" not in {
        entry["relative_path"] for entry in inventory["files"]
    }
    expected_inventory_bytes = {
        "generation.json": manifest.to_json_bytes(),
        "authority/selected-input-identities.json": prepared_payload,
        "verification/independent-verification.json": payloads[
            "verification/independent-verification.json"
        ],
    }
    for entry in inventory["files"]:
        content = expected_inventory_bytes[entry["relative_path"]]
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size_bytes"] == len(content)

    receipt = json.loads(prepared.final_files["generation-receipt.json"])
    report_artifact = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "verification/independent-verification.json"
    )
    assert receipt["generation_id"] == manifest.generation_id
    assert receipt["final_path_count"] == len(expected_payload_paths) + 3
    assert receipt["independent_report"] == report_artifact.to_dict()
    assert receipt["inventory"]["relative_path"] == "generation-inventory.json"
    assert receipt["manifest"]["relative_path"] == "generation.json"
    assert receipt["inventory"]["sha256"] == hashlib.sha256(
        prepared.final_files["generation-inventory.json"]
    ).hexdigest()
    assert receipt["inventory"]["size_bytes"] == len(
        prepared.final_files["generation-inventory.json"]
    )
    assert receipt["manifest"]["sha256"] == hashlib.sha256(
        manifest.to_json_bytes()
    ).hexdigest()
    assert receipt["manifest"]["size_bytes"] == len(manifest.to_json_bytes())
    assert "generation-receipt.json" not in receipt

    _, reordered_payloads = _envelope_manifest_and_payloads()
    prepared_again = publication._prepare_generation_bundle(
        manifest, dict(reversed(tuple(reordered_payloads.items())))
    )
    assert prepared_again.final_files == prepared.final_files


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payloads: payloads.pop("authority/selected-input-identities.json"), "keys"),
        (lambda payloads: payloads.__setitem__("extra.json", b"{}\n"), "keys"),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", bytearray(b"{}\n")
            ),
            "bytes",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b'{"account_count":19,"status":"BLOCKED"}\n'
            ),
            "sha256",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b'{"account_count":180,"status":"BLOCKED"}\n'
            ),
            "size_bytes",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b'{"status":"BLOCKED", "account_count":18}\n'
            ),
            "canonical",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b'{"account_count":18,"account_count":18,"status":"BLOCKED"}\n'
            ),
            "JSON",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b'{"account_count":NaN,"status":"BLOCKED"}\n'
            ),
            "JSON",
        ),
        (
            lambda payloads: payloads.__setitem__(
                "authority/selected-input-identities.json", b"\xff"
            ),
            "JSON",
        ),
    ],
)
def test_prepare_generation_bundle_rejects_payload_identity_and_json_drift(
    mutate: object, message: str
) -> None:
    manifest, payloads = _envelope_manifest_and_payloads()
    mutate(payloads)  # type: ignore[operator]

    with pytest.raises(ManifestValidationError, match=message):
        publication._prepare_generation_bundle(manifest, payloads)


def test_prepare_generation_bundle_requires_a_mapping_payload_input() -> None:
    manifest, payloads = _envelope_manifest_and_payloads()

    with pytest.raises(ManifestValidationError, match="mapping"):
        publication._prepare_generation_bundle(manifest, list(payloads.items()))


def test_prepare_generation_bundle_treats_json_suffix_case_insensitively() -> None:
    manifest, payloads = _envelope_manifest_and_payloads()
    report = manifest.artifacts[1]
    ambiguous_json = b'{"duplicate":1,"duplicate":2}\n'
    upper_json = _artifact("authority/selected-input-identities.JSON", ambiguous_json)
    manifest = _manifest(artifacts=(upper_json, report))
    payloads = {
        upper_json.artifact_id: ambiguous_json,
        report.artifact_id: payloads[report.artifact_id],
    }

    with pytest.raises(ManifestValidationError, match="strict JSON"):
        publication._prepare_generation_bundle(manifest, payloads)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute.json",
        "C:drive.json",
        "directory\\file.json",
        "directory//file.json",
        "directory/./file.json",
        "directory/../file.json",
        "control\x01.json",
        "directory/report.",
        "directory/report ",
        "directory/report.json:receipt",
        "directory/question?.json",
        "NUL",
        "evidence/CON.txt",
        "evidence/com1.json",
        "evidence/COM¹.json",
        "evidence/Lpt9.data",
        "evidence/LPT³.data",
        "evidence/CONIN$",
        "generation.json",
        "generation-inventory.json",
        "generation-receipt.json",
        "GENERATION.JSON",
    ],
)
def test_prepare_generation_bundle_rejects_unsafe_raw_artifact_paths(path: str) -> None:
    manifest, payloads = _envelope_manifest_and_payloads()
    replacement = _artifact("safe.json", b"{}\n")
    object.__setattr__(replacement, "artifact_id", path)
    object.__setattr__(manifest, "artifacts", (replacement, *manifest.artifacts[1:]))
    payloads.pop("authority/selected-input-identities.json")
    payloads[path] = b"{}\n"

    with pytest.raises(ManifestValidationError, match="path"):
        publication._prepare_generation_bundle(manifest, payloads)


def test_prepare_generation_bundle_rejects_case_aliases_and_report_substitutes() -> None:
    manifest, payloads = _envelope_manifest_and_payloads()
    report = next(artifact for artifact in manifest.artifacts if artifact.artifact_id.startswith("verification/"))
    case_alias = _artifact("VERIFICATION/independent-verification.json", payloads[report.artifact_id])
    object.__setattr__(manifest, "artifacts", (manifest.artifacts[0], case_alias))
    payloads.pop(report.artifact_id)
    payloads[case_alias.artifact_id] = payloads["verification/independent-verification.json"] if "verification/independent-verification.json" in payloads else b"{}\n"

    with pytest.raises(ManifestValidationError, match="independent report"):
        publication._prepare_generation_bundle(manifest, payloads)

    manifest, payloads = _envelope_manifest_and_payloads()
    duplicate = _artifact("AUTHORITY/selected-input-identities.json", b"{}\n")
    object.__setattr__(manifest, "artifacts", (*manifest.artifacts, duplicate))
    payloads[duplicate.artifact_id] = b"{}\n"
    with pytest.raises(ManifestValidationError, match="case-insensitive"):
        publication._prepare_generation_bundle(manifest, payloads)


def test_generation_envelope_schemas_are_closed_and_validate_prepared_controls() -> None:
    try:
        import jsonschema
    except ImportError as exc:
        pytest.skip(f"jsonschema runtime is unavailable: {exc}")
    manifest, payloads = _envelope_manifest_and_payloads()
    prepared = publication._prepare_generation_bundle(manifest, payloads)
    schema_root = Path(__file__).parents[2] / "schemas" / "core"
    inventory_schema = json.loads(
        (schema_root / "generation-inventory.schema.json").read_bytes()
    )
    receipt_schema = json.loads(
        (schema_root / "generation-receipt.schema.json").read_bytes()
    )
    jsonschema.Draft202012Validator.check_schema(inventory_schema)
    jsonschema.Draft202012Validator.check_schema(receipt_schema)
    jsonschema.Draft202012Validator(inventory_schema).validate(
        json.loads(prepared.final_files["generation-inventory.json"])
    )
    jsonschema.Draft202012Validator(receipt_schema).validate(
        json.loads(prepared.final_files["generation-receipt.json"])
    )


@pytest.mark.parametrize(
    ("control_name", "mutate"),
    [
        ("inventory", lambda value: value.__setitem__("unexpected", True)),
        (
            "inventory",
            lambda value: value.__setitem__("inventory_schema_version", 2),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__("extra", True),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__("sha256", "A" * 64),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__("size_bytes", -1),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__("relative_path", "/unsafe"),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__(
                "relative_path", "authority/"
            ),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__(
                "relative_path", "authority/report."
            ),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__(
                "relative_path", "authority/report.json:receipt"
            ),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__(
                "relative_path", "authority/NUL.json"
            ),
        ),
        (
            "inventory",
            lambda value: value["files"][0].__setitem__(
                "relative_path", "authority/COM¹.json"
            ),
        ),
        (
            "inventory",
            lambda value: value["files"].append(value["files"][0]),
        ),
        ("receipt", lambda value: value.__setitem__("unexpected", True)),
        (
            "receipt",
            lambda value: value.__setitem__("envelope_schema_version", 2),
        ),
        ("receipt", lambda value: value.__setitem__("final_path_count", 3)),
        (
            "receipt",
            lambda value: value["inventory"].__setitem__("relative_path", "other.json"),
        ),
        (
            "receipt",
            lambda value: value["manifest"].__setitem__("sha256", "A" * 64),
        ),
        (
            "receipt",
            lambda value: value["independent_report"].__setitem__(
                "artifact_id", "verification/other.json"
            ),
        ),
        (
            "receipt",
            lambda value: value["independent_report"]["schema"].__setitem__(
                "unexpected", True
            ),
        ),
        (
            "receipt",
            lambda value: value["independent_report"].__setitem__(
                "as_of_date", "not-an-iso-date"
            ),
        ),
        (
            "receipt",
            lambda value: value["independent_report"].__setitem__("size_bytes", -1),
        ),
    ],
)
def test_generation_envelope_schemas_reject_closed_contract_mutations(
    control_name: str, mutate: object
) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        pytest.skip(f"jsonschema runtime is unavailable: {exc}")
    manifest, payloads = _envelope_manifest_and_payloads()
    prepared = publication._prepare_generation_bundle(manifest, payloads)
    schema_root = Path(__file__).parents[2] / "schemas" / "core"
    schema = json.loads((schema_root / f"generation-{control_name}.schema.json").read_bytes())
    value = json.loads(prepared.final_files[f"generation-{control_name}.json"])
    mutate(value)  # type: ignore[operator]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        ).validate(value)


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


@pytest.mark.parametrize(
    "contents",
    [
        b'{"generation_id":"x","generation_id":"y"}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999999}',
        b'{"value":1} trailing',
        b'[]',
        b'\xff',
    ],
)
def test_generation_manifest_uses_the_strict_json_boundary(contents: bytes) -> None:
    """Replacing strict parsing would silently rewrite a manifest authority."""
    with pytest.raises(ManifestValidationError, match="manifest") as excinfo:
        GenerationManifest.from_json_bytes(contents)

    assert isinstance(excinfo.value.__cause__, StrictJSONError)


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
        artifact_inventory_sha256=generation_inventory_sha256(
            tmp_path, partial.generation_id
        ),
        pointer_schema_version=2,
    )
    (tmp_path / "CURRENT.json").write_bytes(pointer.to_json_bytes())

    with pytest.raises(PointerIntegrityError, match="not publication eligible"):
        read_current(tmp_path)


def test_security_sensitive_current_read_rejects_legacy_pointer_schema(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    stage_generation(tmp_path, manifest)
    legacy = CurrentPointer(
        generation_id=manifest.generation_id,
        manifest_sha256=hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
    )
    (tmp_path / "CURRENT.json").write_bytes(legacy.to_json_bytes())

    with pytest.raises(PointerIntegrityError, match="legacy pointer schema"):
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

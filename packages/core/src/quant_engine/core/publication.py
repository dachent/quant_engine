"""Create-only generation staging and atomic accepted-pointer publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from quant_engine.core.contracts import (
    CoverageStatus,
    GenerationManifest,
    ManifestValidationError,
    VerificationStatus,
    canonical_json_bytes,
)


_GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_POINTER_SCHEMA_VERSION = 1
_POINTER_SCHEMA_VERSION = 2
_INVENTORY_SCHEMA_VERSION = 1
_STAGE_RENAME_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


class GenerationConflictError(RuntimeError):
    """A create-only generation path already contains different evidence."""


class PublicationRejectedError(RuntimeError):
    """A generation is not eligible to become current authority."""


class PointerIntegrityError(RuntimeError):
    """The current pointer or referenced manifest failed integrity checks."""


class PublicationState(StrEnum):
    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True)
class CurrentPointer:
    generation_id: str
    manifest_sha256: str
    artifact_inventory_sha256: str | None = None
    state: PublicationState = PublicationState.PUBLISHED
    pointer_schema_version: int = _LEGACY_POINTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _GENERATION_ID.fullmatch(self.generation_id) is None:
            raise PointerIntegrityError("pointer generation_id is invalid")
        if _GENERATION_ID.fullmatch(self.manifest_sha256) is None:
            raise PointerIntegrityError("pointer manifest_sha256 is invalid")
        if self.state is not PublicationState.PUBLISHED:
            raise PointerIntegrityError("current pointer state must be PUBLISHED")
        if self.pointer_schema_version not in {
            _LEGACY_POINTER_SCHEMA_VERSION,
            _POINTER_SCHEMA_VERSION,
        }:
            raise PointerIntegrityError("pointer schema version is unsupported")
        if self.pointer_schema_version == _LEGACY_POINTER_SCHEMA_VERSION:
            if self.artifact_inventory_sha256 is not None:
                raise PointerIntegrityError(
                    "legacy pointer must not contain an artifact inventory hash"
                )
        elif (
            self.artifact_inventory_sha256 is None
            or _GENERATION_ID.fullmatch(self.artifact_inventory_sha256) is None
        ):
            raise PointerIntegrityError(
                "current pointer artifact inventory hash is invalid"
            )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "generation_id": self.generation_id,
            "manifest_sha256": self.manifest_sha256,
            "pointer_schema_version": self.pointer_schema_version,
            "state": self.state.value,
        }
        if self.pointer_schema_version == _POINTER_SCHEMA_VERSION:
            value["artifact_inventory_sha256"] = self.artifact_inventory_sha256
        return value

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_json_bytes(cls, value: bytes) -> CurrentPointer:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PointerIntegrityError(
                "current pointer is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise PointerIntegrityError("current pointer root must be an object")
        schema_version = decoded.get("pointer_schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise PointerIntegrityError("pointer_schema_version must be an integer")
        expected = {
            "generation_id",
            "manifest_sha256",
            "pointer_schema_version",
            "state",
        }
        if schema_version == _POINTER_SCHEMA_VERSION:
            expected.add("artifact_inventory_sha256")
        if set(decoded) != expected:
            raise PointerIntegrityError("current pointer keys differ from the contract")
        generation_id = decoded["generation_id"]
        manifest_sha256 = decoded["manifest_sha256"]
        state = decoded["state"]
        if not isinstance(generation_id, str) or not isinstance(manifest_sha256, str):
            raise PointerIntegrityError("current pointer hashes must be strings")
        if not isinstance(state, str):
            raise PointerIntegrityError("current pointer state must be a string")
        try:
            parsed_state = PublicationState(state)
        except ValueError as exc:
            raise PointerIntegrityError("current pointer state is unknown") from exc
        return cls(
            generation_id=generation_id,
            manifest_sha256=manifest_sha256,
            artifact_inventory_sha256=(
                decoded.get("artifact_inventory_sha256")
                if isinstance(decoded.get("artifact_inventory_sha256"), str)
                else None
            ),
            state=parsed_state,
            pointer_schema_version=schema_version,
        )


def _validate_root(root: Path) -> Path:
    resolved = root.resolve()
    if root.exists() and root.is_symlink():
        raise PublicationRejectedError("publication root must not be a symlink")
    return resolved


def _generation_path(root: Path, generation_id: str) -> Path:
    if _GENERATION_ID.fullmatch(generation_id) is None:
        raise ManifestValidationError("generation_id must be 64 lower-case hex")
    return root / "generations" / generation_id / "generation.json"


def _sync_directory(path: Path) -> None:
    """Best-effort directory durability after a rename or replace.

    POSIX permits opening and fsyncing directories. Windows commonly rejects
    that operation; in that case the file itself is still flushed and the
    directory sync is skipped rather than weakening portability.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def generation_inventory_sha256(root: Path, generation_id: str) -> str:
    """Hash the complete regular-file inventory of a staged generation.

    The inventory binds portable relative paths, content hashes, and byte
    counts. Links and non-regular files fail closed so authority cannot escape
    the generation directory or silently depend on mutable external content.
    """

    root = _validate_root(root)
    manifest_path = _generation_path(root, generation_id)
    generation_dir = manifest_path.parent
    if not generation_dir.is_dir() or generation_dir.is_symlink():
        raise GenerationConflictError("generation directory is missing or linked")
    files: list[dict[str, object]] = []
    for directory_name, directory_names, file_names in os.walk(
        generation_dir, followlinks=False
    ):
        directory = Path(directory_name)
        for name in directory_names:
            child = directory / name
            if child.is_symlink():
                raise GenerationConflictError(
                    "generation inventory contains a linked directory"
                )
        for name in file_names:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise GenerationConflictError(
                    "generation inventory contains a linked or irregular file"
                )
            relative = path.relative_to(generation_dir).as_posix()
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
            files.append(
                {
                    "relative_path": relative,
                    "sha256": digest.hexdigest(),
                    "size_bytes": size,
                }
            )
    if "generation.json" not in {str(item["relative_path"]) for item in files}:
        raise GenerationConflictError("generation inventory lacks generation.json")
    files.sort(key=lambda item: str(item["relative_path"]))
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "files": files,
                "inventory_schema_version": _INVENTORY_SCHEMA_VERSION,
            }
        )
    ).hexdigest()


def _write_and_sync(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def stage_generation(root: Path, manifest: GenerationManifest) -> Path:
    """Create an immutable manifest directory, idempotently for identical bytes."""

    root = _validate_root(root)
    generations = root / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    final_manifest = _generation_path(root, manifest.generation_id)
    content = manifest.to_json_bytes()
    if final_manifest.exists():
        if final_manifest.is_symlink() or final_manifest.read_bytes() != content:
            raise GenerationConflictError(
                "generation path already contains different or linked evidence"
            )
        return final_manifest

    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=generations))
    try:
        _write_and_sync(temporary / "generation.json", content)
        for attempt in range(len(_STAGE_RENAME_RETRY_DELAYS) + 1):
            try:
                temporary.rename(final_manifest.parent)
                _sync_directory(generations)
                break
            except FileExistsError:
                if (
                    final_manifest.exists()
                    and not final_manifest.is_symlink()
                    and final_manifest.read_bytes() == content
                ):
                    return final_manifest
                raise GenerationConflictError(
                    "generation path appeared with different evidence"
                ) from None
            except PermissionError:
                if attempt == len(_STAGE_RENAME_RETRY_DELAYS):
                    raise
                time.sleep(_STAGE_RENAME_RETRY_DELAYS[attempt])
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final_manifest


def read_generation(root: Path, generation_id: str) -> GenerationManifest:
    root = _validate_root(root)
    path = _generation_path(root, generation_id)
    for directory, label in (
        (path.parent.parent, "generations root"),
        (path.parent, "generation directory"),
    ):
        if directory.is_symlink():
            raise GenerationConflictError(f"{label} must not be a symlink")
        if directory.exists() and not directory.is_dir():
            raise GenerationConflictError(f"{label} must be a regular directory")
    if path.is_symlink():
        raise GenerationConflictError("generation manifest must not be a symlink")
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise GenerationConflictError("generation manifest is missing") from exc
    try:
        manifest = GenerationManifest.from_json_bytes(content)
    except ManifestValidationError as exc:
        raise GenerationConflictError("generation manifest failed integrity") from exc
    if manifest.generation_id != generation_id:
        raise GenerationConflictError(
            "generation manifest identity differs from the requested generation"
        )
    return manifest


def publication_state(root: Path, generation_id: str) -> PublicationState:
    read_generation(root, generation_id)
    try:
        pointer = read_current(root)
    except FileNotFoundError:
        return PublicationState.STAGED
    return (
        PublicationState.PUBLISHED
        if pointer.generation_id == generation_id
        else PublicationState.STAGED
    )


def publish_generation(root: Path, generation_id: str) -> CurrentPointer:
    """Atomically make one complete, verified generation current authority."""

    root = _validate_root(root)
    manifest = read_generation(root, generation_id)
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise PublicationRejectedError("generation verification is not VERIFIED")
    if manifest.coverage.status is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY:
        raise PublicationRejectedError("generation coverage is not complete")

    manifest_bytes = manifest.to_json_bytes()
    inventory_sha256 = generation_inventory_sha256(root, generation_id)
    pointer = CurrentPointer(
        generation_id=generation_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        artifact_inventory_sha256=inventory_sha256,
        pointer_schema_version=_POINTER_SCHEMA_VERSION,
    )
    content = pointer.to_json_bytes()
    root.mkdir(parents=True, exist_ok=True)
    current_path = root / "CURRENT.json"
    if current_path.is_symlink():
        raise PointerIntegrityError("CURRENT.json must not be a symlink")
    if current_path.exists() and current_path.read_bytes() == content:
        return pointer

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".CURRENT-", suffix=".tmp", dir=root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, current_path)
        _sync_directory(root)
    finally:
        if temporary.exists():
            temporary.unlink()
    return pointer


def read_current(root: Path) -> CurrentPointer:
    root = _validate_root(root)
    current_path = root / "CURRENT.json"
    if current_path.is_symlink():
        raise PointerIntegrityError("CURRENT.json must not be a symlink")
    content = current_path.read_bytes()
    pointer = CurrentPointer.from_json_bytes(content)
    manifest_path = _generation_path(root, pointer.generation_id)
    if manifest_path.is_symlink():
        raise PointerIntegrityError("current generation manifest must not be a symlink")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise PointerIntegrityError("current generation manifest is missing") from exc
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if actual != pointer.manifest_sha256:
        raise PointerIntegrityError("current generation manifest hash differs")
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except ManifestValidationError as exc:
        raise PointerIntegrityError("current generation manifest is invalid") from exc
    if manifest.generation_id != pointer.generation_id:
        raise PointerIntegrityError("current generation identity differs")
    if (
        manifest.verification is not VerificationStatus.VERIFIED
        or manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    ):
        raise PointerIntegrityError("current generation is not publication eligible")
    if pointer.artifact_inventory_sha256 is not None:
        try:
            actual_inventory = generation_inventory_sha256(root, pointer.generation_id)
        except GenerationConflictError as exc:
            raise PointerIntegrityError(
                "current generation artifact inventory is invalid"
            ) from exc
        if actual_inventory != pointer.artifact_inventory_sha256:
            raise PointerIntegrityError(
                "current generation artifact inventory hash differs"
            )
    return pointer

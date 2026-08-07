"""Journaled multi-root publication with deterministic rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Callable, Final, Mapping, Self

from quant_engine.core.contracts import (
    CoverageStatus,
    VerificationStatus,
    canonical_json_bytes,
)
from quant_engine.core.publication import (
    CurrentPointer,
    PointerIntegrityError,
    PublicationRejectedError,
    generation_inventory_sha256,
    publish_generation,
    read_current,
    read_generation,
)


_LEGACY_SCHEMA_VERSION: Final = 1
_SCHEMA_VERSION: Final = 2
_AUTHORITY_SCHEMA_VERSION: Final = 1
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PREDECESSOR_REPAIR_GATE_ID: Final = "predecessor-authority-repair"


class ReleaseValidationError(ValueError):
    """A release manifest, journal, or root mapping is invalid."""


class ReleaseConflictError(RuntimeError):
    """The current global state differs from the prepared release."""


class ReleaseCommitError(RuntimeError):
    """A release failed and rollback was attempted."""


class ReleaseState(StrEnum):
    PREPARED = "PREPARED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_FAILED = "RECOVERY_FAILED"


class ReleaseAuthorityState(StrEnum):
    COMMITTED = "COMMITTED"


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if _ID.fullmatch(normalized) is None:
        raise ReleaseValidationError(f"{field_name} is not a portable identifier")
    return normalized


def _hash(value: str, field_name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ReleaseValidationError(f"{field_name} must be 64 lower-case hex")
    return value


def _relative(value: str) -> str:
    portable = PurePosixPath(value)
    if (
        portable.is_absolute()
        or "\\" in value
        or any(item in {"", ".", ".."} for item in portable.parts)
    ):
        raise ReleaseValidationError("companion path must be portable and relative")
    return portable.as_posix()


def _strict(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ReleaseValidationError(f"{name} keys differ from the contract")


def _sync_directory(path: Path) -> None:
    """Best-effort directory durability after replace/unlink operations."""

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


def _normalize_roots(roots: Mapping[str, Path]) -> dict[str, Path]:
    normalized: dict[str, Path] = {}
    for key, value in roots.items():
        root_id = _identifier(key, "root_id")
        if root_id in normalized:
            raise ReleaseValidationError("release root IDs collide after normalization")
        normalized[root_id] = value.resolve()
    aliases: dict[str, str] = {}
    for root_id, path in normalized.items():
        identity = os.path.normcase(str(path))
        prior = aliases.setdefault(identity, root_id)
        if prior != root_id:
            raise ReleaseValidationError(
                f"release roots alias the same location: {prior}, {root_id}"
            )
    return normalized


def _reject_coordination_alias(
    coordination_root: Path, roots: Mapping[str, Path]
) -> None:
    if os.path.normcase(str(coordination_root.resolve())) in {
        os.path.normcase(str(path.resolve())) for path in roots.values()
    }:
        raise ReleaseValidationError(
            "coordination root must not alias a release target root"
        )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReleaseConflictError(f"release target must not be a symlink: {path.name}")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _create_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ReleaseConflictError(
                f"create-only release evidence differs: {path.name}"
            )
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


@dataclass(frozen=True)
class GateEvidence:
    gate_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_id", _identifier(self.gate_id, "gate_id"))
        _hash(self.evidence_sha256, "evidence_sha256")

    def to_dict(self) -> dict[str, str]:
        return {"evidence_sha256": self.evidence_sha256, "gate_id": self.gate_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict(value, {"evidence_sha256", "gate_id"}, "gate")
        if not isinstance(value["gate_id"], str) or not isinstance(
            value["evidence_sha256"], str
        ):
            raise ReleaseValidationError("gate fields must be strings")
        return cls(value["gate_id"], value["evidence_sha256"])


@dataclass(frozen=True)
class ReleaseAuthorityPointer:
    """Single reader-visible authority for one validated coordinated graph."""

    release_id: str
    release_manifest_sha256: str
    state: ReleaseAuthorityState = ReleaseAuthorityState.COMMITTED
    authority_schema_version: int = _AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _hash(self.release_id, "release_id")
        _hash(self.release_manifest_sha256, "release_manifest_sha256")
        if self.state is not ReleaseAuthorityState.COMMITTED:
            raise ReleaseValidationError("release authority must be COMMITTED")
        if self.authority_schema_version != _AUTHORITY_SCHEMA_VERSION:
            raise ReleaseValidationError("release authority schema is unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "authority_schema_version": self.authority_schema_version,
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "state": self.state.value,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError(
                "release authority is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ReleaseValidationError("release authority root must be an object")
        _strict(
            decoded,
            {
                "authority_schema_version",
                "release_id",
                "release_manifest_sha256",
                "state",
            },
            "release_authority",
        )
        if not isinstance(decoded["release_id"], str) or not isinstance(
            decoded["release_manifest_sha256"], str
        ):
            raise ReleaseValidationError("release authority hashes must be strings")
        schema_version = decoded["authority_schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ReleaseValidationError("authority_schema_version must be an integer")
        try:
            state = ReleaseAuthorityState(str(decoded["state"]))
        except ValueError as exc:
            raise ReleaseValidationError("release authority state is unknown") from exc
        return cls(
            release_id=decoded["release_id"],
            release_manifest_sha256=decoded["release_manifest_sha256"],
            state=state,
            authority_schema_version=schema_version,
        )


@dataclass(frozen=True)
class ReleaseTarget:
    root_id: str
    proposed_generation_id: str
    proposed_manifest_sha256: str
    proposed_inventory_sha256: str | None
    previous_pointer: CurrentPointer | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _identifier(self.root_id, "root_id"))
        _hash(self.proposed_generation_id, "proposed_generation_id")
        _hash(self.proposed_manifest_sha256, "proposed_manifest_sha256")
        if self.proposed_inventory_sha256 is not None:
            _hash(self.proposed_inventory_sha256, "proposed_inventory_sha256")

    def to_dict(
        self, *, release_schema_version: int = _SCHEMA_VERSION
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "previous_pointer": (
                self.previous_pointer.to_dict()
                if self.previous_pointer is not None
                else None
            ),
            "proposed_generation_id": self.proposed_generation_id,
            "proposed_manifest_sha256": self.proposed_manifest_sha256,
            "root_id": self.root_id,
        }
        if release_schema_version >= _SCHEMA_VERSION:
            value["proposed_inventory_sha256"] = self.proposed_inventory_sha256
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = {
            "previous_pointer",
            "proposed_generation_id",
            "proposed_manifest_sha256",
            "root_id",
        }
        if "proposed_inventory_sha256" in value:
            expected.add("proposed_inventory_sha256")
        _strict(value, expected, "release_target")
        previous = value["previous_pointer"]
        if previous is not None and not isinstance(previous, Mapping):
            raise ReleaseValidationError("previous_pointer must be an object or null")
        text = (
            value["root_id"],
            value["proposed_generation_id"],
            value["proposed_manifest_sha256"],
        )
        if not all(isinstance(item, str) for item in text):
            raise ReleaseValidationError("release target identifiers must be strings")
        return cls(
            root_id=value["root_id"],
            proposed_generation_id=value["proposed_generation_id"],
            proposed_manifest_sha256=value["proposed_manifest_sha256"],
            proposed_inventory_sha256=(
                value.get("proposed_inventory_sha256")
                if isinstance(value.get("proposed_inventory_sha256"), str)
                else None
            ),
            previous_pointer=(
                None
                if previous is None
                else CurrentPointer.from_json_bytes(canonical_json_bytes(previous))
            ),
        )


@dataclass(frozen=True)
class CompanionInput:
    companion_id: str
    root_id: str
    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "companion_id", _identifier(self.companion_id, "companion_id")
        )
        object.__setattr__(self, "root_id", _identifier(self.root_id, "root_id"))
        object.__setattr__(self, "relative_path", _relative(self.relative_path))


@dataclass(frozen=True)
class CompanionPlan:
    companion_id: str
    root_id: str
    relative_path: str
    proposed_sha256: str
    previous_sha256: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "companion_id", _identifier(self.companion_id, "companion_id")
        )
        object.__setattr__(self, "root_id", _identifier(self.root_id, "root_id"))
        object.__setattr__(self, "relative_path", _relative(self.relative_path))
        _hash(self.proposed_sha256, "proposed_sha256")
        if self.previous_sha256 is not None:
            _hash(self.previous_sha256, "previous_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "companion_id": self.companion_id,
            "previous_sha256": self.previous_sha256,
            "proposed_sha256": self.proposed_sha256,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict(
            value,
            {
                "companion_id",
                "previous_sha256",
                "proposed_sha256",
                "relative_path",
                "root_id",
            },
            "companion_plan",
        )
        previous = value["previous_sha256"]
        text = (
            value["companion_id"],
            value["root_id"],
            value["relative_path"],
            value["proposed_sha256"],
        )
        if not all(isinstance(item, str) for item in text):
            raise ReleaseValidationError("companion identifiers must be strings")
        if previous is not None and not isinstance(previous, str):
            raise ReleaseValidationError("previous_sha256 must be a string or null")
        return cls(
            companion_id=value["companion_id"],
            root_id=value["root_id"],
            relative_path=value["relative_path"],
            proposed_sha256=value["proposed_sha256"],
            previous_sha256=previous,
        )


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    code_commit: str
    created_at: datetime
    targets: tuple[ReleaseTarget, ...]
    gates: tuple[GateEvidence, ...]
    companions: tuple[CompanionPlan, ...]
    previous_authority: ReleaseAuthorityPointer | None = None
    release_schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _hash(self.release_id, "release_id")
        if _COMMIT.fullmatch(self.code_commit) is None:
            raise ReleaseValidationError("code_commit must be 40 lower-case hex")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ReleaseValidationError("created_at must be timezone-aware")
        if self.release_schema_version not in {
            _LEGACY_SCHEMA_VERSION,
            _SCHEMA_VERSION,
        }:
            raise ReleaseValidationError("release schema version is unsupported")
        if (
            self.release_schema_version == _LEGACY_SCHEMA_VERSION
            and self.previous_authority is not None
        ):
            raise ReleaseValidationError(
                "legacy release cannot contain previous authority"
            )
        targets = tuple(sorted(self.targets, key=lambda item: item.root_id))
        gates = tuple(sorted(self.gates, key=lambda item: item.gate_id))
        companions = tuple(sorted(self.companions, key=lambda item: item.companion_id))
        if not targets or not gates:
            raise ReleaseValidationError("release needs at least one target and gate")
        if len({item.root_id for item in targets}) != len(targets):
            raise ReleaseValidationError("release root IDs must be unique")
        if len({item.gate_id for item in gates}) != len(gates):
            raise ReleaseValidationError("release gate IDs must be unique")
        if len({item.companion_id for item in companions}) != len(companions):
            raise ReleaseValidationError("companion IDs must be unique")
        root_ids = {item.root_id for item in targets}
        if any(item.root_id not in root_ids for item in companions):
            raise ReleaseValidationError("companion references an unknown root")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "gates", gates)
        object.__setattr__(self, "companions", companions)
        expected = hashlib.sha256(
            canonical_json_bytes(self.identity_payload())
        ).hexdigest()
        if self.release_id != expected:
            raise ReleaseValidationError("release_id does not match manifest content")

    def identity_payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "code_commit": self.code_commit,
            "companions": [item.to_dict() for item in self.companions],
            "created_at": self.created_at.isoformat(),
            "gates": [item.to_dict() for item in self.gates],
            "release_schema_version": self.release_schema_version,
            "targets": [
                item.to_dict(release_schema_version=self.release_schema_version)
                for item in self.targets
            ],
        }
        if self.release_schema_version >= _SCHEMA_VERSION:
            value["previous_authority"] = (
                self.previous_authority.to_dict()
                if self.previous_authority is not None
                else None
            )
        return value

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(
            {"release_id": self.release_id, **self.identity_payload()}
        )

    @classmethod
    def build(
        cls,
        *,
        code_commit: str,
        created_at: datetime,
        targets: tuple[ReleaseTarget, ...],
        gates: tuple[GateEvidence, ...],
        companions: tuple[CompanionPlan, ...] = (),
        previous_authority: ReleaseAuthorityPointer | None = None,
    ) -> Self:
        ordered_targets = tuple(sorted(targets, key=lambda item: item.root_id))
        ordered_gates = tuple(sorted(gates, key=lambda item: item.gate_id))
        ordered_companions = tuple(
            sorted(companions, key=lambda item: item.companion_id)
        )
        payload = {
            "code_commit": code_commit,
            "companions": [item.to_dict() for item in ordered_companions],
            "created_at": created_at.isoformat(),
            "gates": [item.to_dict() for item in ordered_gates],
            "release_schema_version": _SCHEMA_VERSION,
            "targets": [
                item.to_dict(release_schema_version=_SCHEMA_VERSION)
                for item in ordered_targets
            ],
            "previous_authority": (
                previous_authority.to_dict() if previous_authority is not None else None
            ),
        }
        release_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return cls(
            release_id=release_id,
            code_commit=code_commit,
            created_at=created_at,
            targets=ordered_targets,
            gates=ordered_gates,
            companions=ordered_companions,
            previous_authority=previous_authority,
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError("release is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ReleaseValidationError("release root must be an object")
        schema_version = decoded.get("release_schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ReleaseValidationError("release_schema_version must be an integer")
        expected = {
            "code_commit",
            "companions",
            "created_at",
            "gates",
            "release_id",
            "release_schema_version",
            "targets",
        }
        if schema_version >= _SCHEMA_VERSION:
            expected.add("previous_authority")
        _strict(decoded, expected, "release")
        if not isinstance(decoded["release_id"], str) or not isinstance(
            decoded["code_commit"], str
        ):
            raise ReleaseValidationError("release identifiers must be strings")
        if not isinstance(decoded["created_at"], str):
            raise ReleaseValidationError("created_at must be a string")
        try:
            created_at = datetime.fromisoformat(decoded["created_at"])
        except ValueError as exc:
            raise ReleaseValidationError("created_at is not an ISO datetime") from exc
        targets = decoded["targets"]
        gates = decoded["gates"]
        companions = decoded["companions"]
        if not all(isinstance(item, list) for item in (targets, gates, companions)):
            raise ReleaseValidationError("release collections must be lists")
        previous_authority = decoded.get("previous_authority")
        if previous_authority is not None and not isinstance(
            previous_authority, Mapping
        ):
            raise ReleaseValidationError("previous_authority must be an object or null")
        return cls(
            release_id=decoded["release_id"],
            code_commit=decoded["code_commit"],
            created_at=created_at,
            targets=tuple(ReleaseTarget.from_dict(item) for item in targets),
            gates=tuple(GateEvidence.from_dict(item) for item in gates),
            companions=tuple(CompanionPlan.from_dict(item) for item in companions),
            previous_authority=(
                None
                if previous_authority is None
                else ReleaseAuthorityPointer.from_json_bytes(
                    canonical_json_bytes(previous_authority)
                )
            ),
            release_schema_version=schema_version,
        )


@dataclass(frozen=True)
class ReleaseJournal:
    release_id: str
    state: ReleaseState
    applied_targets: tuple[str, ...] = ()
    applied_companions: tuple[str, ...] = ()
    last_error: str | None = None

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "applied_companions": list(self.applied_companions),
                "applied_targets": list(self.applied_targets),
                "last_error": self.last_error,
                "release_id": self.release_id,
                "state": self.state.value,
            }
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError("journal is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ReleaseValidationError("journal root must be an object")
        _strict(
            decoded,
            {
                "applied_companions",
                "applied_targets",
                "last_error",
                "release_id",
                "state",
            },
            "journal",
        )
        targets = decoded["applied_targets"]
        companions = decoded["applied_companions"]
        if not isinstance(targets, list) or not all(
            isinstance(item, str) for item in targets
        ):
            raise ReleaseValidationError("applied_targets must be strings")
        if not isinstance(companions, list) or not all(
            isinstance(item, str) for item in companions
        ):
            raise ReleaseValidationError("applied_companions must be strings")
        if not isinstance(decoded["release_id"], str) or not isinstance(
            decoded["state"], str
        ):
            raise ReleaseValidationError("journal identifiers must be strings")
        last_error = decoded["last_error"]
        if last_error is not None and not isinstance(last_error, str):
            raise ReleaseValidationError("last_error must be a string or null")
        try:
            state = ReleaseState(decoded["state"])
        except ValueError as exc:
            raise ReleaseValidationError("journal state is unknown") from exc
        return cls(
            release_id=decoded["release_id"],
            state=state,
            applied_targets=tuple(targets),
            applied_companions=tuple(companions),
            last_error=last_error,
        )


def _release_dir(coordination_root: Path, release_id: str) -> Path:
    _hash(release_id, "release_id")
    return coordination_root.resolve() / "releases" / release_id


def _authority_path(coordination_root: Path) -> Path:
    return coordination_root.resolve() / "CURRENT_RELEASE.json"


def _expected_authority(manifest: ReleaseManifest) -> ReleaseAuthorityPointer:
    return ReleaseAuthorityPointer(
        release_id=manifest.release_id,
        release_manifest_sha256=hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
    )


def _read_optional_authority(
    coordination_root: Path,
) -> ReleaseAuthorityPointer | None:
    path = _authority_path(coordination_root)
    if path.is_symlink():
        raise ReleaseConflictError("CURRENT_RELEASE.json must not be a symlink")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    pointer = ReleaseAuthorityPointer.from_json_bytes(content)
    release_path = _release_dir(coordination_root, pointer.release_id) / "release.json"
    if release_path.is_symlink() or not release_path.is_file():
        raise ReleaseConflictError(
            "authoritative release manifest is missing or linked"
        )
    if hashlib.sha256(release_path.read_bytes()).hexdigest() != (
        pointer.release_manifest_sha256
    ):
        raise ReleaseConflictError("authoritative release manifest hash differs")
    return pointer


def _read_optional_current(root: Path) -> CurrentPointer | None:
    try:
        return read_current(root)
    except FileNotFoundError:
        return None


def _read_recorded_current(root: Path) -> CurrentPointer | None:
    """Read pointer bytes without accepting the referenced generation inventory."""

    path = root.resolve() / "CURRENT.json"
    if path.is_symlink():
        raise ReleaseConflictError("CURRENT.json must not be a symlink")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    return CurrentPointer.from_json_bytes(content)


def _validate_invalid_previous_artifact_inventory(
    manifest: ReleaseManifest,
    roots: Mapping[str, Path],
    *,
    coordination_root: Path,
) -> None:
    """Accept only a committed predecessor whose pointer graph is still exact.

    This deliberately ignores generation *artifact inventory* equality and no
    other part of the predecessor contract.  The caller must separately bind
    the exact expected inventory drift as governed evidence.
    """

    if set(roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    drifted = False
    for target in manifest.targets:
        root = roots[target.root_id].resolve()
        pointer = _read_recorded_current(root)
        if pointer is None or (
            pointer.generation_id != target.proposed_generation_id
            or pointer.manifest_sha256 != target.proposed_manifest_sha256
            or pointer.artifact_inventory_sha256 != target.proposed_inventory_sha256
        ):
            raise ReleaseConflictError(
                f"recorded predecessor pointer graph differs: {target.root_id}"
            )
        generation = read_generation(root, target.proposed_generation_id)
        if (
            hashlib.sha256(generation.to_json_bytes()).hexdigest()
            != target.proposed_manifest_sha256
        ):
            raise ReleaseConflictError(
                f"recorded predecessor manifest differs: {target.root_id}"
            )
        if (
            target.proposed_inventory_sha256 is not None
            and generation_inventory_sha256(root, target.proposed_generation_id)
            != target.proposed_inventory_sha256
        ):
            drifted = True
    if not drifted:
        raise ReleaseConflictError(
            "predecessor repair requires an artifact inventory mismatch"
        )
    for companion in manifest.companions:
        path = roots[companion.root_id].resolve() / companion.relative_path
        if _companion_hash(path) != companion.proposed_sha256:
            raise ReleaseConflictError(
                f"recorded predecessor companion differs: {companion.companion_id}"
            )
    if _read_optional_authority(coordination_root) != _expected_authority(manifest):
        raise ReleaseConflictError("recorded predecessor authority differs")


def _repairs_invalid_previous_inventory(manifest: ReleaseManifest) -> bool:
    return any(item.gate_id == _PREDECESSOR_REPAIR_GATE_ID for item in manifest.gates)


def prepare_release(
    *,
    coordination_root: Path,
    roots: Mapping[str, Path],
    proposed_generations: Mapping[str, str],
    code_commit: str,
    created_at: datetime,
    gates: tuple[GateEvidence, ...],
    companions: tuple[CompanionInput, ...] = (),
    allow_invalid_previous_artifact_inventory: bool = False,
) -> ReleaseManifest:
    """Create a hash-bound release and recovery state without changing pointers."""

    if allow_invalid_previous_artifact_inventory and not any(
        item.gate_id == _PREDECESSOR_REPAIR_GATE_ID for item in gates
    ):
        raise ReleaseValidationError(
            "predecessor inventory repair requires its governed gate"
        )
    normalized_roots = _normalize_roots(roots)
    coordination_root = coordination_root.resolve()
    _reject_coordination_alias(coordination_root, normalized_roots)
    normalized_proposed: dict[str, str] = {}
    for key, value in proposed_generations.items():
        root_id = _identifier(key, "root_id")
        if root_id in normalized_proposed:
            raise ReleaseValidationError(
                "proposed root IDs collide after normalization"
            )
        normalized_proposed[root_id] = _hash(value, "generation_id")
    if set(normalized_roots) != set(normalized_proposed):
        raise ReleaseValidationError("roots and proposed generation IDs must match")
    targets: list[ReleaseTarget] = []
    for root_id in sorted(normalized_roots):
        root = normalized_roots[root_id]
        generation_manifest = read_generation(root, normalized_proposed[root_id])
        if generation_manifest.verification is not VerificationStatus.VERIFIED:
            raise PublicationRejectedError("release target is not verified")
        if (
            generation_manifest.coverage.status
            is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        ):
            raise PublicationRejectedError("release target coverage is not complete")
        try:
            previous_pointer = _read_optional_current(root)
        except PointerIntegrityError:
            if not allow_invalid_previous_artifact_inventory:
                raise
            previous_pointer = _read_recorded_current(root)
        targets.append(
            ReleaseTarget(
                root_id=root_id,
                proposed_generation_id=generation_manifest.generation_id,
                proposed_manifest_sha256=hashlib.sha256(
                    generation_manifest.to_json_bytes()
                ).hexdigest(),
                proposed_inventory_sha256=generation_inventory_sha256(
                    root, generation_manifest.generation_id
                ),
                previous_pointer=previous_pointer,
            )
        )
    plans: list[CompanionPlan] = []
    for item in companions:
        if item.root_id not in normalized_roots:
            raise ReleaseValidationError("companion references an unknown root")
        companion_path = normalized_roots[item.root_id] / item.relative_path
        if companion_path.is_symlink():
            raise ReleaseConflictError("companion target must not be a symlink")
        previous = companion_path.read_bytes() if companion_path.exists() else None
        plans.append(
            CompanionPlan(
                companion_id=item.companion_id,
                root_id=item.root_id,
                relative_path=item.relative_path,
                proposed_sha256=hashlib.sha256(item.content).hexdigest(),
                previous_sha256=(
                    hashlib.sha256(previous).hexdigest()
                    if previous is not None
                    else None
                ),
            )
        )
    previous_authority = _read_optional_authority(coordination_root)
    if allow_invalid_previous_artifact_inventory and previous_authority is None:
        raise ReleaseConflictError(
            "predecessor inventory repair requires coordinated authority"
        )
    if previous_authority is not None:
        try:
            read_current_release(coordination_root, normalized_roots)
        except PointerIntegrityError:
            if not allow_invalid_previous_artifact_inventory:
                raise
            previous_manifest, previous_journal = read_release(
                coordination_root, previous_authority.release_id
            )
            if previous_journal.state is not ReleaseState.COMMITTED:
                raise ReleaseConflictError(
                    "authoritative release journal is not COMMITTED"
                )
            _validate_invalid_previous_artifact_inventory(
                previous_manifest,
                normalized_roots,
                coordination_root=coordination_root,
            )
    release_manifest = ReleaseManifest.build(
        code_commit=code_commit,
        created_at=created_at,
        targets=tuple(targets),
        gates=gates,
        companions=tuple(plans),
        previous_authority=previous_authority,
    )
    directory = _release_dir(coordination_root, release_manifest.release_id)
    _create_exact(directory / "release.json", release_manifest.to_json_bytes())
    for plan in release_manifest.companions:
        companion_path = normalized_roots[plan.root_id] / plan.relative_path
        if plan.previous_sha256 is not None:
            _create_exact(
                directory / "recovery" / f"{plan.companion_id}.bin",
                companion_path.read_bytes(),
            )
    _create_exact(
        directory / "journal.json",
        ReleaseJournal(
            release_manifest.release_id, ReleaseState.PREPARED
        ).to_json_bytes(),
    )
    return release_manifest


def read_release(
    coordination_root: Path, release_id: str
) -> tuple[ReleaseManifest, ReleaseJournal]:
    directory = _release_dir(coordination_root, release_id)
    manifest = ReleaseManifest.from_json_bytes(
        (directory / "release.json").read_bytes()
    )
    journal = ReleaseJournal.from_json_bytes((directory / "journal.json").read_bytes())
    if manifest.release_id != release_id or journal.release_id != release_id:
        raise ReleaseValidationError("release identity differs")
    return manifest, journal


def _write_journal(coordination_root: Path, journal: ReleaseJournal) -> None:
    _atomic_write(
        _release_dir(coordination_root, journal.release_id) / "journal.json",
        journal.to_json_bytes(),
    )


def _validate_prepared_state(
    manifest: ReleaseManifest,
    roots: Mapping[str, Path],
    *,
    coordination_root: Path | None = None,
) -> None:
    if set(roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    for target in manifest.targets:
        root = roots[target.root_id].resolve()
        current = (
            _read_recorded_current(root)
            if _repairs_invalid_previous_inventory(manifest)
            else _read_optional_current(root)
        )
        if current != target.previous_pointer:
            raise ReleaseConflictError(
                f"current pointer changed after prepare: {target.root_id}"
            )
        proposed = read_generation(root, target.proposed_generation_id)
        if (
            hashlib.sha256(proposed.to_json_bytes()).hexdigest()
            != target.proposed_manifest_sha256
        ):
            raise ReleaseConflictError(f"proposed manifest changed: {target.root_id}")
        if (
            target.proposed_inventory_sha256 is not None
            and generation_inventory_sha256(root, target.proposed_generation_id)
            != target.proposed_inventory_sha256
        ):
            raise ReleaseConflictError(
                f"proposed generation inventory changed: {target.root_id}"
            )
    for companion in manifest.companions:
        path = roots[companion.root_id].resolve() / companion.relative_path
        if _companion_hash(path) != companion.previous_sha256:
            raise ReleaseConflictError(
                f"companion changed after prepare: {companion.companion_id}"
            )
    if manifest.release_schema_version >= _SCHEMA_VERSION:
        if coordination_root is None:
            raise ReleaseValidationError(
                "coordination root is required for release authority validation"
            )
        if _read_optional_authority(coordination_root) != manifest.previous_authority:
            raise ReleaseConflictError("release authority changed after prepare")


def _validate_committed_state(
    manifest: ReleaseManifest,
    roots: Mapping[str, Path],
    *,
    coordination_root: Path | None = None,
) -> None:
    if set(roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    for target in manifest.targets:
        pointer = read_current(roots[target.root_id].resolve())
        if (
            pointer.generation_id != target.proposed_generation_id
            or pointer.manifest_sha256 != target.proposed_manifest_sha256
            or (
                target.proposed_inventory_sha256 is not None
                and pointer.artifact_inventory_sha256
                != target.proposed_inventory_sha256
            )
        ):
            raise ReleaseConflictError(
                f"committed pointer graph differs: {target.root_id}"
            )
    for companion in manifest.companions:
        path = roots[companion.root_id].resolve() / companion.relative_path
        if _companion_hash(path) != companion.proposed_sha256:
            raise ReleaseConflictError(
                f"committed companion differs: {companion.companion_id}"
            )
    if manifest.release_schema_version >= _SCHEMA_VERSION:
        if coordination_root is None:
            raise ReleaseValidationError(
                "coordination root is required for release authority validation"
            )
        if _read_optional_authority(coordination_root) != _expected_authority(manifest):
            raise ReleaseConflictError("coordinated release authority differs")


def _validate_rolled_back_state(
    manifest: ReleaseManifest,
    roots: Mapping[str, Path],
    *,
    coordination_root: Path | None = None,
) -> None:
    if set(roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    for target in manifest.targets:
        root = roots[target.root_id].resolve()
        current = (
            _read_recorded_current(root)
            if _repairs_invalid_previous_inventory(manifest)
            else _read_optional_current(root)
        )
        if current != target.previous_pointer:
            raise ReleaseConflictError(
                f"rolled-back pointer graph differs: {target.root_id}"
            )
    for companion in manifest.companions:
        path = roots[companion.root_id].resolve() / companion.relative_path
        if _companion_hash(path) != companion.previous_sha256:
            raise ReleaseConflictError(
                f"rolled-back companion differs: {companion.companion_id}"
            )
    if manifest.release_schema_version >= _SCHEMA_VERSION:
        if coordination_root is None:
            raise ReleaseValidationError(
                "coordination root is required for release authority validation"
            )
        if _read_optional_authority(coordination_root) != manifest.previous_authority:
            raise ReleaseConflictError("rolled-back release authority differs")


def read_current_release(
    coordination_root: Path, roots: Mapping[str, Path]
) -> ReleaseManifest:
    """Return the sole committed graph authority after complete validation.

    Readers that require a coherent multi-root snapshot use this entry point
    before consuming target generations. During a commit or recovery window it
    fails closed if the central authority and root graph do not agree, so a
    mixed graph is never represented as accepted authority.
    """

    normalized_roots = _normalize_roots(roots)
    pointer = _read_optional_authority(coordination_root)
    if pointer is None:
        raise ReleaseConflictError("no coordinated release authority is published")
    manifest, journal = read_release(coordination_root, pointer.release_id)
    if journal.state is not ReleaseState.COMMITTED:
        raise ReleaseConflictError("authoritative release journal is not COMMITTED")
    if _expected_authority(manifest) != pointer:
        raise ReleaseConflictError("coordinated release authority differs")
    _validate_committed_state(
        manifest,
        normalized_roots,
        coordination_root=coordination_root,
    )
    return manifest


FaultHook = Callable[[str, str], None]


def _companion_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ReleaseConflictError(
            f"release companion is not a regular file: {path.name}"
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rollback_release(
    *,
    coordination_root: Path,
    manifest: ReleaseManifest,
    journal: ReleaseJournal,
    roots: Mapping[str, Path],
    fault_hook: FaultHook | None = None,
) -> ReleaseJournal:
    """Idempotently restore every recorded release intent in reverse order."""

    rolling = ReleaseJournal(
        manifest.release_id,
        ReleaseState.ROLLING_BACK,
        journal.applied_targets,
        journal.applied_companions,
        journal.last_error,
    )
    _write_journal(coordination_root, rolling)
    try:
        companions = {item.companion_id: item for item in manifest.companions}
        for companion_id in reversed(rolling.applied_companions):
            plan = companions[companion_id]
            companion_path = roots[plan.root_id].resolve() / plan.relative_path
            current_hash = _companion_hash(companion_path)
            if current_hash == plan.previous_sha256:
                pass
            elif current_hash == plan.proposed_sha256:
                if plan.previous_sha256 is None:
                    companion_path.unlink(missing_ok=True)
                else:
                    recovery = (
                        _release_dir(coordination_root, manifest.release_id)
                        / "recovery"
                        / f"{companion_id}.bin"
                    ).read_bytes()
                    if hashlib.sha256(recovery).hexdigest() != plan.previous_sha256:
                        raise ReleaseConflictError("companion recovery hash differs")
                    _atomic_write(companion_path, recovery)
            elif plan.previous_sha256 is not None:
                recovery = (
                    _release_dir(coordination_root, manifest.release_id)
                    / "recovery"
                    / f"{companion_id}.bin"
                ).read_bytes()
                if hashlib.sha256(recovery).hexdigest() != plan.previous_sha256:
                    raise ReleaseConflictError("companion recovery hash differs")
                raise ReleaseConflictError(
                    f"cannot safely roll back changed companion: {companion_id}"
                )
            else:
                raise ReleaseConflictError(
                    f"cannot safely roll back changed companion: {companion_id}"
                )
            if fault_hook is not None:
                fault_hook("after-companion-restore", companion_id)
        targets = {item.root_id: item for item in manifest.targets}
        for root_id in reversed(rolling.applied_targets):
            release_target = targets[root_id]
            root = roots[root_id].resolve()
            current = _read_optional_current(root)
            if current == release_target.previous_pointer:
                pass
            elif (
                current is not None
                and current.generation_id == release_target.proposed_generation_id
            ):
                if release_target.previous_pointer is None:
                    current_path = root / "CURRENT.json"
                    current_path.unlink(missing_ok=True)
                    _sync_directory(root)
                else:
                    _atomic_write(
                        root / "CURRENT.json",
                        release_target.previous_pointer.to_json_bytes(),
                    )
                    if read_current(root) != release_target.previous_pointer:
                        raise ReleaseConflictError(
                            f"restored pointer differs: {root_id}"
                        )
            else:
                raise ReleaseConflictError(
                    f"cannot safely roll back changed root: {root_id}"
                )
            if fault_hook is not None:
                fault_hook("after-target-restore", root_id)
        if manifest.release_schema_version >= _SCHEMA_VERSION:
            current_authority = _read_optional_authority(coordination_root)
            proposed_authority = _expected_authority(manifest)
            if current_authority == manifest.previous_authority:
                pass
            elif current_authority == proposed_authority:
                authority_path = _authority_path(coordination_root)
                if manifest.previous_authority is None:
                    authority_path.unlink(missing_ok=True)
                    _sync_directory(authority_path.parent)
                else:
                    _atomic_write(
                        authority_path,
                        manifest.previous_authority.to_json_bytes(),
                    )
            else:
                raise ReleaseConflictError(
                    "cannot safely roll back coordinated release authority"
                )
            if fault_hook is not None:
                fault_hook("after-authority-restore", manifest.release_id)
        recovered = ReleaseJournal(
            manifest.release_id,
            ReleaseState.ROLLED_BACK,
            rolling.applied_targets,
            rolling.applied_companions,
            rolling.last_error,
        )
        _write_journal(coordination_root, recovered)
        if fault_hook is not None:
            fault_hook("after-rolled-back-journal", manifest.release_id)
        return recovered
    except Exception as exc:
        failed = ReleaseJournal(
            manifest.release_id,
            ReleaseState.RECOVERY_FAILED,
            rolling.applied_targets,
            rolling.applied_companions,
            f"{type(exc).__name__}: {exc}",
        )
        _write_journal(coordination_root, failed)
        raise ReleaseCommitError("release recovery failed") from exc


def _acquire_lock(lock: Path, release_id: str) -> None:
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ReleaseConflictError(
            "another release holds the coordination lock"
        ) from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((release_id + "\n").encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())


def _clear_confirmed_stale_lock(
    lock: Path, release_id: str, *, confirmed: bool
) -> bool:
    present = lock.exists() or lock.is_symlink()
    if not present:
        return False
    if not confirmed:
        raise ReleaseConflictError(
            "interrupted release lock requires explicit confirmation"
        )
    if lock.is_symlink() or not lock.is_file():
        raise ReleaseConflictError("interrupted release lock is not a file")
    try:
        lock_release_id = lock.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseConflictError("interrupted release lock is unreadable") from exc
    if lock_release_id != release_id:
        raise ReleaseConflictError("interrupted release lock identity differs")
    lock.unlink()
    return True


def recover_release(
    *,
    coordination_root: Path,
    release_id: str,
    roots: Mapping[str, Path],
    clear_stale_lock: bool = False,
    fault_hook: FaultHook | None = None,
) -> ReleaseJournal:
    """Explicitly resume rollback for an interrupted release.

    A surviving lock is never guessed to be stale. The caller must explicitly
    confirm its removal, and its contents must name this exact release.
    """

    manifest, journal = read_release(coordination_root, release_id)
    normalized_roots = _normalize_roots(roots)
    if set(normalized_roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    coordination_root = coordination_root.resolve()
    _reject_coordination_alias(coordination_root, normalized_roots)
    lock = coordination_root / "release.lock"
    lock_present = lock.exists() or lock.is_symlink()
    if journal.state is ReleaseState.PREPARED:
        if not lock_present:
            raise ReleaseConflictError("release is not recoverable: PREPARED")
        _validate_prepared_state(
            manifest,
            normalized_roots,
            coordination_root=coordination_root,
        )
        _clear_confirmed_stale_lock(lock, release_id, confirmed=clear_stale_lock)
        return journal
    if journal.state is ReleaseState.COMMITTED:
        _validate_committed_state(
            manifest,
            normalized_roots,
            coordination_root=coordination_root,
        )
        if lock_present:
            _clear_confirmed_stale_lock(lock, release_id, confirmed=clear_stale_lock)
        return journal
    if journal.state is ReleaseState.ROLLED_BACK:
        _validate_rolled_back_state(
            manifest,
            normalized_roots,
            coordination_root=coordination_root,
        )
        if lock_present:
            _clear_confirmed_stale_lock(lock, release_id, confirmed=clear_stale_lock)
        return journal
    if journal.state not in {
        ReleaseState.COMMITTING,
        ReleaseState.ROLLING_BACK,
        ReleaseState.RECOVERY_FAILED,
    }:
        raise ReleaseConflictError(f"release is not recoverable: {journal.state.value}")
    if lock_present:
        _clear_confirmed_stale_lock(lock, release_id, confirmed=clear_stale_lock)
    _acquire_lock(lock, release_id)
    try:
        return rollback_release(
            coordination_root=coordination_root,
            manifest=manifest,
            journal=journal,
            roots=normalized_roots,
            fault_hook=fault_hook,
        )
    finally:
        lock.unlink(missing_ok=True)


def commit_release(
    *,
    coordination_root: Path,
    release_id: str,
    roots: Mapping[str, Path],
    companion_contents: Mapping[str, bytes] | None = None,
    fault_hook: FaultHook | None = None,
) -> ReleaseJournal:
    """Commit every pointer and companion or restore the prepared predecessor graph."""

    manifest, journal = read_release(coordination_root, release_id)
    normalized_roots = _normalize_roots(roots)
    coordination_root = coordination_root.resolve()
    _reject_coordination_alias(coordination_root, normalized_roots)
    if journal.state is ReleaseState.COMMITTED:
        _validate_committed_state(
            manifest,
            normalized_roots,
            coordination_root=coordination_root,
        )
        return journal
    if journal.state is not ReleaseState.PREPARED:
        raise ReleaseConflictError(f"release is not prepared: {journal.state.value}")
    coordination_root.mkdir(parents=True, exist_ok=True)
    lock = coordination_root / "release.lock"
    _acquire_lock(lock, manifest.release_id)
    if fault_hook is not None:
        fault_hook("after-lock-acquire", manifest.release_id)

    current: ReleaseJournal | None = None
    try:
        _validate_prepared_state(
            manifest,
            normalized_roots,
            coordination_root=coordination_root,
        )
        companion_contents = companion_contents or {}
        if set(companion_contents) != {
            item.companion_id for item in manifest.companions
        }:
            raise ReleaseValidationError(
                "runtime companion content differs from release plan"
            )
        for item in manifest.companions:
            actual = hashlib.sha256(companion_contents[item.companion_id]).hexdigest()
            if actual != item.proposed_sha256:
                raise ReleaseConflictError("companion proposed hash differs")

        current = ReleaseJournal(manifest.release_id, ReleaseState.COMMITTING)
        _write_journal(coordination_root, current)
        for target in manifest.targets:
            if fault_hook is not None:
                fault_hook("before-target", target.root_id)
            current = ReleaseJournal(
                manifest.release_id,
                ReleaseState.COMMITTING,
                (*current.applied_targets, target.root_id),
                current.applied_companions,
            )
            _write_journal(coordination_root, current)
            if fault_hook is not None:
                fault_hook("after-target-intent", target.root_id)
            publish_generation(
                normalized_roots[target.root_id], target.proposed_generation_id
            )
            if fault_hook is not None:
                fault_hook("after-target", target.root_id)
        for companion in manifest.companions:
            if fault_hook is not None:
                fault_hook("before-companion", companion.companion_id)
            current = ReleaseJournal(
                manifest.release_id,
                ReleaseState.COMMITTING,
                current.applied_targets,
                (*current.applied_companions, companion.companion_id),
            )
            _write_journal(coordination_root, current)
            if fault_hook is not None:
                fault_hook("after-companion-intent", companion.companion_id)
            _atomic_write(
                normalized_roots[companion.root_id] / companion.relative_path,
                companion_contents[companion.companion_id],
            )
            if fault_hook is not None:
                fault_hook("after-companion", companion.companion_id)
        for target in manifest.targets:
            pointer = read_current(normalized_roots[target.root_id])
            if (
                pointer.generation_id != target.proposed_generation_id
                or pointer.manifest_sha256 != target.proposed_manifest_sha256
                or (
                    target.proposed_inventory_sha256 is not None
                    and pointer.artifact_inventory_sha256
                    != target.proposed_inventory_sha256
                )
            ):
                raise ReleaseConflictError(
                    f"published pointer differs: {target.root_id}"
                )
        for companion in manifest.companions:
            content = (
                normalized_roots[companion.root_id] / companion.relative_path
            ).read_bytes()
            if hashlib.sha256(content).hexdigest() != companion.proposed_sha256:
                raise ReleaseConflictError("published companion hash differs")
        if manifest.release_schema_version >= _SCHEMA_VERSION:
            _atomic_write(
                _authority_path(coordination_root),
                _expected_authority(manifest).to_json_bytes(),
            )
            if fault_hook is not None:
                fault_hook("after-authority", manifest.release_id)
            _validate_committed_state(
                manifest,
                normalized_roots,
                coordination_root=coordination_root,
            )
        committed = ReleaseJournal(
            manifest.release_id,
            ReleaseState.COMMITTED,
            current.applied_targets,
            current.applied_companions,
        )
        _write_journal(coordination_root, committed)
        if manifest.release_schema_version >= _SCHEMA_VERSION:
            read_current_release(coordination_root, normalized_roots)
        if fault_hook is not None:
            fault_hook("after-committed-journal", manifest.release_id)
        return committed
    except Exception as exc:
        if current is None:
            raise
        failed = ReleaseJournal(
            manifest.release_id,
            ReleaseState.COMMITTING,
            current.applied_targets,
            current.applied_companions,
            f"{type(exc).__name__}: {exc}",
        )
        _write_journal(coordination_root, failed)
        rollback_release(
            coordination_root=coordination_root,
            manifest=manifest,
            journal=failed,
            roots=normalized_roots,
        )
        raise ReleaseCommitError(
            "release failed and predecessor graph was restored"
        ) from exc
    finally:
        lock.unlink(missing_ok=True)

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
    PublicationRejectedError,
    publish_generation,
    read_current,
    read_generation,
)


_SCHEMA_VERSION: Final = 1
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


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
class ReleaseTarget:
    root_id: str
    proposed_generation_id: str
    proposed_manifest_sha256: str
    previous_pointer: CurrentPointer | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _identifier(self.root_id, "root_id"))
        _hash(self.proposed_generation_id, "proposed_generation_id")
        _hash(self.proposed_manifest_sha256, "proposed_manifest_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "previous_pointer": (
                self.previous_pointer.to_dict()
                if self.previous_pointer is not None
                else None
            ),
            "proposed_generation_id": self.proposed_generation_id,
            "proposed_manifest_sha256": self.proposed_manifest_sha256,
            "root_id": self.root_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict(
            value,
            {
                "previous_pointer",
                "proposed_generation_id",
                "proposed_manifest_sha256",
                "root_id",
            },
            "release_target",
        )
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
    release_schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _hash(self.release_id, "release_id")
        if _COMMIT.fullmatch(self.code_commit) is None:
            raise ReleaseValidationError("code_commit must be 40 lower-case hex")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ReleaseValidationError("created_at must be timezone-aware")
        if self.release_schema_version != _SCHEMA_VERSION:
            raise ReleaseValidationError("release schema version is unsupported")
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
        return {
            "code_commit": self.code_commit,
            "companions": [item.to_dict() for item in self.companions],
            "created_at": self.created_at.isoformat(),
            "gates": [item.to_dict() for item in self.gates],
            "release_schema_version": self.release_schema_version,
            "targets": [item.to_dict() for item in self.targets],
        }

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
            "targets": [item.to_dict() for item in ordered_targets],
        }
        release_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return cls(
            release_id=release_id,
            code_commit=code_commit,
            created_at=created_at,
            targets=ordered_targets,
            gates=ordered_gates,
            companions=ordered_companions,
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError("release is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ReleaseValidationError("release root must be an object")
        _strict(
            decoded,
            {
                "code_commit",
                "companions",
                "created_at",
                "gates",
                "release_id",
                "release_schema_version",
                "targets",
            },
            "release",
        )
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
        schema_version = decoded["release_schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ReleaseValidationError("release_schema_version must be an integer")
        return cls(
            release_id=decoded["release_id"],
            code_commit=decoded["code_commit"],
            created_at=created_at,
            targets=tuple(ReleaseTarget.from_dict(item) for item in targets),
            gates=tuple(GateEvidence.from_dict(item) for item in gates),
            companions=tuple(CompanionPlan.from_dict(item) for item in companions),
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


def _read_optional_current(root: Path) -> CurrentPointer | None:
    try:
        return read_current(root)
    except FileNotFoundError:
        return None


def prepare_release(
    *,
    coordination_root: Path,
    roots: Mapping[str, Path],
    proposed_generations: Mapping[str, str],
    code_commit: str,
    created_at: datetime,
    gates: tuple[GateEvidence, ...],
    companions: tuple[CompanionInput, ...] = (),
) -> ReleaseManifest:
    """Create a hash-bound release and recovery state without changing pointers."""

    normalized_roots = {
        _identifier(key, "root_id"): value.resolve() for key, value in roots.items()
    }
    normalized_proposed = {
        _identifier(key, "root_id"): _hash(value, "generation_id")
        for key, value in proposed_generations.items()
    }
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
        targets.append(
            ReleaseTarget(
                root_id=root_id,
                proposed_generation_id=generation_manifest.generation_id,
                proposed_manifest_sha256=hashlib.sha256(
                    generation_manifest.to_json_bytes()
                ).hexdigest(),
                previous_pointer=_read_optional_current(root),
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
    release_manifest = ReleaseManifest.build(
        code_commit=code_commit,
        created_at=created_at,
        targets=tuple(targets),
        gates=gates,
        companions=tuple(plans),
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
    manifest: ReleaseManifest, roots: Mapping[str, Path]
) -> None:
    if set(roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    for target in manifest.targets:
        root = roots[target.root_id].resolve()
        if _read_optional_current(root) != target.previous_pointer:
            raise ReleaseConflictError(
                f"current pointer changed after prepare: {target.root_id}"
            )
        proposed = read_generation(root, target.proposed_generation_id)
        if (
            hashlib.sha256(proposed.to_json_bytes()).hexdigest()
            != target.proposed_manifest_sha256
        ):
            raise ReleaseConflictError(f"proposed manifest changed: {target.root_id}")


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
                    (root / "CURRENT.json").unlink(missing_ok=True)
                else:
                    restored = publish_generation(
                        root, release_target.previous_pointer.generation_id
                    )
                    if restored != release_target.previous_pointer:
                        raise ReleaseConflictError(
                            f"restored pointer differs: {root_id}"
                        )
            else:
                raise ReleaseConflictError(
                    f"cannot safely roll back changed root: {root_id}"
                )
            if fault_hook is not None:
                fault_hook("after-target-restore", root_id)
        recovered = ReleaseJournal(
            manifest.release_id,
            ReleaseState.ROLLED_BACK,
            rolling.applied_targets,
            rolling.applied_companions,
            rolling.last_error,
        )
        _write_journal(coordination_root, recovered)
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
    if journal.state is ReleaseState.COMMITTED:
        return journal
    if journal.state is ReleaseState.ROLLED_BACK:
        return journal
    if journal.state not in {
        ReleaseState.COMMITTING,
        ReleaseState.ROLLING_BACK,
        ReleaseState.RECOVERY_FAILED,
    }:
        raise ReleaseConflictError(f"release is not recoverable: {journal.state.value}")
    normalized_roots = {
        _identifier(key, "root_id"): value for key, value in roots.items()
    }
    if set(normalized_roots) != {item.root_id for item in manifest.targets}:
        raise ReleaseValidationError("runtime roots differ from release roots")
    coordination_root = coordination_root.resolve()
    lock = coordination_root / "release.lock"
    if lock.exists():
        if not clear_stale_lock:
            raise ReleaseConflictError(
                "interrupted release lock requires explicit confirmation"
            )
        if lock.is_symlink() or not lock.is_file():
            raise ReleaseConflictError("interrupted release lock is not a file")
        try:
            lock_release_id = lock.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ReleaseConflictError(
                "interrupted release lock is unreadable"
            ) from exc
        if lock_release_id != release_id:
            raise ReleaseConflictError("interrupted release lock identity differs")
        lock.unlink()
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
    if journal.state is ReleaseState.COMMITTED:
        return journal
    if journal.state is not ReleaseState.PREPARED:
        raise ReleaseConflictError(f"release is not prepared: {journal.state.value}")
    normalized_roots = {
        _identifier(key, "root_id"): value for key, value in roots.items()
    }
    coordination_root = coordination_root.resolve()
    coordination_root.mkdir(parents=True, exist_ok=True)
    lock = coordination_root / "release.lock"
    _acquire_lock(lock, manifest.release_id)

    current: ReleaseJournal | None = None
    try:
        _validate_prepared_state(manifest, normalized_roots)
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
            if pointer.generation_id != target.proposed_generation_id:
                raise ReleaseConflictError(
                    f"published pointer differs: {target.root_id}"
                )
        for companion in manifest.companions:
            content = (
                normalized_roots[companion.root_id] / companion.relative_path
            ).read_bytes()
            if hashlib.sha256(content).hexdigest() != companion.proposed_sha256:
                raise ReleaseConflictError("published companion hash differs")
        committed = ReleaseJournal(
            manifest.release_id,
            ReleaseState.COMMITTED,
            current.applied_targets,
            current.applied_companions,
        )
        _write_journal(coordination_root, committed)
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

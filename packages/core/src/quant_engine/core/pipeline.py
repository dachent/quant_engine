"""Explicit-ID, resumable source-to-release pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Final, Mapping, Self

from quant_engine.core.authority import FreshnessPolicy
from quant_engine.core.contracts import (
    CoverageStatus,
    GenerationManifest,
    VerificationStatus,
    canonical_json_bytes,
)
from quant_engine.core.publication import (
    CurrentPointer,
    generation_inventory_sha256,
    read_current,
    read_generation,
)
from quant_engine.core.release import (
    CompanionInput,
    GateEvidence,
    ReleaseAuthorityPointer,
    ReleaseState,
    commit_release,
    prepare_release,
    read_release,
    recover_release,
)


_SCHEMA_VERSION: Final = 1
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PORTFOLIO_CONTRACT_GATE: Final = "portfolio-orchestration-contract"
_POST_BUILD_SEMANTIC_GATE: Final = "post-build-semantic"
_PREDECESSOR_REPAIR_GATE: Final = "predecessor-authority-repair"
_PORTFOLIO_PROFILE: Final = "PORTFOLIO_SOURCE_TO_PANEL_V1"
_PORTFOLIO_STAGE_ORDER: Final = (
    "ledger",
    "baseline",
    "attribution",
    "analytics",
    "report",
    "workbook",
    "panel",
)
_POST_BUILD_CHECKS: Final = {
    "analytics_report_panel_equality",
    "financial_identities",
    "freshness",
    "generation_invariants",
    "ledger_bridges",
    "mappings",
}


class PipelineValidationError(ValueError):
    """A pipeline specification, node, or receipt violates its contract."""


class PipelineExecutionError(RuntimeError):
    """A pipeline operation failed without changing release authority."""


class PipelineUnknownOutcomeError(PipelineExecutionError):
    """An external read outcome is unknown and must never be retried."""


class NodeKind(StrEnum):
    ARTIFACT = "ARTIFACT"
    GENERATION = "GENERATION"
    COMMAND = "COMMAND"


class CommandEffect(StrEnum):
    LOCAL_ONLY = "LOCAL_ONLY"
    READ_ONLY_EXTERNAL = "READ_ONLY_EXTERNAL"


class ReceiptState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise PipelineValidationError(f"{name} must be text")
    normalized = value.strip().lower()
    if _ID.fullmatch(normalized) is None:
        raise PipelineValidationError(f"{name} is not a portable identifier")
    return normalized


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PipelineValidationError(f"{name} must be 64 lower-case hex")
    return value


def _absolute(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PipelineValidationError(f"{name} must be a path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PipelineValidationError(f"{name} must be absolute")
    return path.resolve()


def _relative(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PipelineValidationError(f"{name} must be a relative path")
    portable = PurePosixPath(value)
    if (
        portable.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in portable.parts)
    ):
        raise PipelineValidationError(f"{name} must be portable and relative")
    return portable.as_posix()


def _strict(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise PipelineValidationError(f"{name} keys differ from the contract")


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PipelineValidationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PipelineValidationError(f"{name} must be an array")
    return [_object(item, name) for item in value]


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PipelineValidationError(f"{name} must be a text array")
    return tuple(value)


def _file_hash(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError(f"expected regular file is missing: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _sync_directory(path: Path) -> None:
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


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PipelineExecutionError(f"pipeline output is a link: {path}")
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
            raise PipelineExecutionError(f"create-only output differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


@dataclass(frozen=True)
class FileExpectation:
    relative_path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _relative(self.relative_path, "file.relative_path"),
        )
        _hash(self.sha256, "file.sha256")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise PipelineValidationError("file.size_bytes must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict(value, {"relative_path", "sha256", "size_bytes"}, "file")
        if not isinstance(value["size_bytes"], int):
            raise PipelineValidationError("file.size_bytes must be an integer")
        return cls(
            relative_path=_relative(value["relative_path"], "file.relative_path"),
            sha256=_hash(value["sha256"], "file.sha256"),
            size_bytes=value["size_bytes"],
        )


@dataclass(frozen=True)
class ArtifactNode:
    node_id: str
    depends_on: tuple[str, ...]
    path: Path
    sha256: str
    size_bytes: int
    kind: NodeKind = NodeKind.ARTIFACT

    def to_dict(self) -> dict[str, object]:
        return {
            "depends_on": list(self.depends_on),
            "kind": self.kind.value,
            "node_id": self.node_id,
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class GenerationNode:
    node_id: str
    depends_on: tuple[str, ...]
    root_id: str
    generation_id: str
    manifest_sha256: str
    files: tuple[FileExpectation, ...]
    source_dir: Path | None
    release_target: bool
    kind: NodeKind = NodeKind.GENERATION

    def to_dict(self) -> dict[str, object]:
        return {
            "depends_on": list(self.depends_on),
            "files": [item.to_dict() for item in self.files],
            "generation_id": self.generation_id,
            "kind": self.kind.value,
            "manifest_sha256": self.manifest_sha256,
            "node_id": self.node_id,
            "release_target": self.release_target,
            "root_id": self.root_id,
            "source_dir": str(self.source_dir) if self.source_dir is not None else None,
        }


@dataclass(frozen=True)
class CommandNode:
    node_id: str
    depends_on: tuple[str, ...]
    argv: tuple[str, ...]
    cwd: Path
    effect: CommandEffect
    retry_limit: int
    timeout_seconds: int
    request_identity: str | None
    declared_reads: tuple[Path, ...]
    declared_writes: tuple[Path, ...]
    kind: NodeKind = NodeKind.COMMAND

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "declared_reads": [str(item) for item in self.declared_reads],
            "declared_writes": [str(item) for item in self.declared_writes],
            "depends_on": list(self.depends_on),
            "effect": self.effect.value,
            "kind": self.kind.value,
            "node_id": self.node_id,
            "request_identity": self.request_identity,
            "retry_limit": self.retry_limit,
            "timeout_seconds": self.timeout_seconds,
        }


PipelineNode = ArtifactNode | GenerationNode | CommandNode


@dataclass(frozen=True)
class PipelineGate:
    gate_id: str
    path: Path
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"gate_id": self.gate_id, "path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class PipelineCompanion:
    companion_id: str
    root_id: str
    relative_path: str
    source_path: Path
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "companion_id": self.companion_id,
            "relative_path": self.relative_path,
            "root_id": self.root_id,
            "sha256": self.sha256,
            "source_path": str(self.source_path),
        }


@dataclass(frozen=True)
class PipelineSpec:
    pipeline_id: str
    as_of_date: date
    code_commit: str
    created_at: datetime
    coordination_root: Path
    state_root: Path
    roots: Mapping[str, Path]
    nodes: tuple[PipelineNode, ...]
    gates: tuple[PipelineGate, ...]
    companions: tuple[PipelineCompanion, ...]
    schema_version: int = _SCHEMA_VERSION

    @property
    def spec_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()

    @property
    def ordered_nodes(self) -> tuple[PipelineNode, ...]:
        by_id = {item.node_id: item for item in self.nodes}
        pending = set(by_id)
        completed: set[str] = set()
        ordered: list[PipelineNode] = []
        while pending:
            ready = sorted(
                node_id
                for node_id in pending
                if set(by_id[node_id].depends_on) <= completed
            )
            if not ready:
                raise PipelineValidationError("pipeline dependency graph has a cycle")
            for node_id in ready:
                ordered.append(by_id[node_id])
                completed.add(node_id)
                pending.remove(node_id)
        return tuple(ordered)

    @property
    def release_generations(self) -> dict[str, str]:
        return {
            item.root_id: item.generation_id
            for item in self.nodes
            if isinstance(item, GenerationNode) and item.release_target
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "code_commit": self.code_commit,
            "companions": [item.to_dict() for item in self.companions],
            "coordination_root": str(self.coordination_root),
            "created_at": self.created_at.isoformat(),
            "gates": [item.to_dict() for item in self.gates],
            "nodes": [item.to_dict() for item in self.nodes],
            "pipeline_id": self.pipeline_id,
            "roots": {key: str(value) for key, value in sorted(self.roots.items())},
            "schema_version": self.schema_version,
            "state_root": str(self.state_root),
        }

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineValidationError("pipeline spec is not JSON") from exc
        raw = _object(decoded, "pipeline spec")
        _strict(
            raw,
            {
                "as_of_date",
                "code_commit",
                "companions",
                "coordination_root",
                "created_at",
                "gates",
                "nodes",
                "pipeline_id",
                "roots",
                "schema_version",
                "state_root",
            },
            "pipeline spec",
        )
        if raw["schema_version"] != _SCHEMA_VERSION:
            raise PipelineValidationError("pipeline schema version is unsupported")
        try:
            as_of = date.fromisoformat(str(raw["as_of_date"]))
            created_at = datetime.fromisoformat(str(raw["created_at"]))
        except ValueError as exc:
            raise PipelineValidationError("pipeline date is invalid") from exc
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise PipelineValidationError("created_at must be timezone-aware")
        code_commit = str(raw["code_commit"])
        if _COMMIT.fullmatch(code_commit) is None:
            raise PipelineValidationError("code_commit must be 40 lower-case hex")
        root_rows = _object(raw["roots"], "roots")
        roots = {
            _identifier(key, "root_id"): _absolute(path, f"roots.{key}")
            for key, path in root_rows.items()
        }
        if not roots:
            raise PipelineValidationError("pipeline needs at least one root")
        nodes = tuple(
            _parse_node(item, roots) for item in _objects(raw["nodes"], "nodes")
        )
        node_ids = {item.node_id for item in nodes}
        if len(node_ids) != len(nodes):
            raise PipelineValidationError("pipeline node IDs must be unique")
        if any(set(item.depends_on) - node_ids for item in nodes):
            raise PipelineValidationError(
                "pipeline node references an unknown dependency"
            )
        targets = [
            item.root_id
            for item in nodes
            if isinstance(item, GenerationNode) and item.release_target
        ]
        if not targets or len(targets) != len(set(targets)):
            raise PipelineValidationError(
                "release targets must be non-empty and unique"
            )
        gates = tuple(_parse_gate(item) for item in _objects(raw["gates"], "gates"))
        if not gates or len({item.gate_id for item in gates}) != len(gates):
            raise PipelineValidationError("pipeline gates must be non-empty and unique")
        companions = tuple(
            _parse_companion(item, roots)
            for item in _objects(raw["companions"], "companions")
        )
        if len({item.companion_id for item in companions}) != len(companions):
            raise PipelineValidationError("pipeline companion IDs must be unique")
        spec = cls(
            pipeline_id=_identifier(raw["pipeline_id"], "pipeline_id"),
            as_of_date=as_of,
            code_commit=code_commit,
            created_at=created_at,
            coordination_root=_absolute(raw["coordination_root"], "coordination_root"),
            state_root=_absolute(raw["state_root"], "state_root"),
            roots=roots,
            nodes=nodes,
            gates=gates,
            companions=companions,
        )
        spec.ordered_nodes
        return spec


def _parse_dependencies(value: object, node_id: str) -> tuple[str, ...]:
    dependencies = tuple(
        _identifier(item, f"{node_id}.depends_on")
        for item in _strings(value, f"{node_id}.depends_on")
    )
    if len(set(dependencies)) != len(dependencies) or node_id in dependencies:
        raise PipelineValidationError(f"{node_id} dependencies are invalid")
    return tuple(sorted(dependencies))


def _parse_node(value: Mapping[str, Any], roots: Mapping[str, Path]) -> PipelineNode:
    node_id = _identifier(value.get("node_id"), "node_id")
    try:
        kind = NodeKind(str(value.get("kind")))
    except ValueError as exc:
        raise PipelineValidationError(f"{node_id} kind is unsupported") from exc
    dependencies = _parse_dependencies(value.get("depends_on"), node_id)
    if kind is NodeKind.ARTIFACT:
        _strict(
            value,
            {"depends_on", "kind", "node_id", "path", "sha256", "size_bytes"},
            node_id,
        )
        if not isinstance(value["size_bytes"], int):
            raise PipelineValidationError(f"{node_id}.size_bytes must be an integer")
        return ArtifactNode(
            node_id=node_id,
            depends_on=dependencies,
            path=_absolute(value["path"], f"{node_id}.path"),
            sha256=_hash(value["sha256"], f"{node_id}.sha256"),
            size_bytes=value["size_bytes"],
        )
    if kind is NodeKind.GENERATION:
        _strict(
            value,
            {
                "depends_on",
                "files",
                "generation_id",
                "kind",
                "manifest_sha256",
                "node_id",
                "release_target",
                "root_id",
                "source_dir",
            },
            node_id,
        )
        root_id = _identifier(value["root_id"], f"{node_id}.root_id")
        if root_id not in roots:
            raise PipelineValidationError(f"{node_id} references an unknown root")
        files = tuple(
            sorted(
                (
                    FileExpectation.from_dict(item)
                    for item in _objects(value["files"], f"{node_id}.files")
                ),
                key=lambda item: item.relative_path,
            )
        )
        if not files or len({item.relative_path for item in files}) != len(files):
            raise PipelineValidationError(f"{node_id} generation inventory is invalid")
        if "generation.json" not in {item.relative_path for item in files}:
            raise PipelineValidationError(f"{node_id} inventory lacks generation.json")
        source = value["source_dir"]
        if source is not None and not isinstance(source, str):
            raise PipelineValidationError(f"{node_id}.source_dir must be text or null")
        if not isinstance(value["release_target"], bool):
            raise PipelineValidationError(f"{node_id}.release_target must be boolean")
        return GenerationNode(
            node_id=node_id,
            depends_on=dependencies,
            root_id=root_id,
            generation_id=_hash(value["generation_id"], f"{node_id}.generation_id"),
            manifest_sha256=_hash(
                value["manifest_sha256"], f"{node_id}.manifest_sha256"
            ),
            files=files,
            source_dir=(
                None if source is None else _absolute(source, f"{node_id}.source_dir")
            ),
            release_target=value["release_target"],
        )
    _strict(
        value,
        {
            "argv",
            "cwd",
            "declared_reads",
            "declared_writes",
            "depends_on",
            "effect",
            "kind",
            "node_id",
            "request_identity",
            "retry_limit",
            "timeout_seconds",
        },
        node_id,
    )
    argv = _strings(value["argv"], f"{node_id}.argv")
    if not argv or any(not item for item in argv):
        raise PipelineValidationError(f"{node_id}.argv is empty")
    try:
        effect = CommandEffect(str(value["effect"]))
    except ValueError as exc:
        raise PipelineValidationError(f"{node_id}.effect is unsupported") from exc
    retry_limit = value["retry_limit"]
    timeout_seconds = value["timeout_seconds"]
    if (
        not isinstance(retry_limit, int)
        or isinstance(retry_limit, bool)
        or retry_limit < 0
        or retry_limit > 2
    ):
        raise PipelineValidationError(f"{node_id}.retry_limit must be 0..2")
    if (
        not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > 3600
    ):
        raise PipelineValidationError(f"{node_id}.timeout_seconds is invalid")
    request = value["request_identity"]
    if effect is CommandEffect.READ_ONLY_EXTERNAL:
        request = _hash(request, f"{node_id}.request_identity")
        if retry_limit != 0:
            raise PipelineValidationError("external reads cannot be retried")
    elif request is not None:
        raise PipelineValidationError("local commands cannot declare request_identity")
    return CommandNode(
        node_id=node_id,
        depends_on=dependencies,
        argv=argv,
        cwd=_absolute(value["cwd"], f"{node_id}.cwd"),
        effect=effect,
        retry_limit=retry_limit,
        timeout_seconds=timeout_seconds,
        request_identity=request,
        declared_reads=tuple(
            _absolute(item, f"{node_id}.declared_reads")
            for item in _strings(value["declared_reads"], f"{node_id}.declared_reads")
        ),
        declared_writes=tuple(
            _absolute(item, f"{node_id}.declared_writes")
            for item in _strings(value["declared_writes"], f"{node_id}.declared_writes")
        ),
    )


def _parse_gate(value: Mapping[str, Any]) -> PipelineGate:
    _strict(value, {"gate_id", "path", "sha256"}, "gate")
    return PipelineGate(
        gate_id=_identifier(value["gate_id"], "gate_id"),
        path=_absolute(value["path"], "gate.path"),
        sha256=_hash(value["sha256"], "gate.sha256"),
    )


def _parse_companion(
    value: Mapping[str, Any], roots: Mapping[str, Path]
) -> PipelineCompanion:
    _strict(
        value,
        {"companion_id", "relative_path", "root_id", "sha256", "source_path"},
        "companion",
    )
    root_id = _identifier(value["root_id"], "companion.root_id")
    if root_id not in roots:
        raise PipelineValidationError("companion references an unknown root")
    return PipelineCompanion(
        companion_id=_identifier(value["companion_id"], "companion_id"),
        root_id=root_id,
        relative_path=_relative(value["relative_path"], "companion.relative_path"),
        source_path=_absolute(value["source_path"], "companion.source_path"),
        sha256=_hash(value["sha256"], "companion.sha256"),
    )


def load_pipeline_spec(path: Path) -> PipelineSpec:
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("pipeline spec must be a regular file")
    return PipelineSpec.from_json_bytes(path.read_bytes())


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _within_path(path: Path, boundary: Path) -> bool:
    try:
        path.resolve().relative_to(boundary.resolve())
    except ValueError:
        return False
    return True


def _contains_ambient_current(value: object) -> bool:
    parts = str(value).replace("\\", "/").split("/")
    return any(part.strip().lower() in {"current", "current.json"} for part in parts)


def _portfolio_contract(spec: PipelineSpec) -> dict[str, Any] | None:
    matches = [item for item in spec.gates if item.gate_id == _PORTFOLIO_CONTRACT_GATE]
    if not matches:
        return None
    if len(matches) != 1:
        raise PipelineValidationError(
            "portfolio orchestration contract gate is ambiguous"
        )
    gate = matches[0]
    digest, _ = _file_hash(gate.path)
    if digest != gate.sha256:
        raise PipelineValidationError("portfolio orchestration contract differs")
    try:
        raw = _object(
            json.loads(gate.path.read_text(encoding="utf-8")),
            "portfolio orchestration contract",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError(
            "portfolio orchestration contract is unreadable"
        ) from exc
    _strict(
        raw,
        {
            "authority_snapshot",
            "command_sources",
            "profile",
            "release_commit",
            "runtime",
            "schema_version",
            "semantic_reconciliation_node",
            "source_acquisitions",
            "stages",
        },
        "portfolio orchestration contract",
    )
    if raw["schema_version"] != 1 or raw["profile"] != _PORTFOLIO_PROFILE:
        raise PipelineValidationError("portfolio orchestration profile is unsupported")
    release_commit = str(raw["release_commit"])
    if _COMMIT.fullmatch(release_commit) is None:
        raise PipelineValidationError("portfolio release_commit is invalid")
    if release_commit != spec.code_commit:
        raise PipelineValidationError("portfolio release commit differs from pipeline")

    runtime = _object(raw["runtime"], "portfolio runtime")
    _strict(
        runtime,
        {
            "environment_sha256",
            "executable_path",
            "executable_sha256",
            "lock_path",
            "lock_sha256",
            "repo_root",
        },
        "portfolio runtime",
    )
    executable_path = _absolute(
        runtime["executable_path"], "portfolio runtime.executable_path"
    )
    lock_path = _absolute(runtime["lock_path"], "portfolio runtime.lock_path")
    executable_sha256 = _hash(
        runtime["executable_sha256"], "portfolio runtime.executable_sha256"
    )
    lock_sha256 = _hash(runtime["lock_sha256"], "portfolio runtime.lock_sha256")
    environment_sha256 = _hash(
        runtime["environment_sha256"], "portfolio runtime.environment_sha256"
    )
    repo_root = _absolute(runtime["repo_root"], "portfolio runtime.repo_root")

    command_source_rows = _objects(raw["command_sources"], "portfolio command_sources")
    if not command_source_rows:
        raise PipelineValidationError("portfolio command sources are empty")
    command_sources: list[dict[str, object]] = []
    for row in command_source_rows:
        _strict(
            row,
            {"entrypoint_path", "entrypoint_sha256", "node_id"},
            "portfolio command source",
        )
        command_sources.append(
            {
                "entrypoint_path": _absolute(
                    row["entrypoint_path"], "command source.entrypoint_path"
                ),
                "entrypoint_sha256": _hash(
                    row["entrypoint_sha256"], "command source.entrypoint_sha256"
                ),
                "node_id": _identifier(row["node_id"], "command source.node_id"),
            }
        )
    if len({str(row["node_id"]) for row in command_sources}) != len(command_sources):
        raise PipelineValidationError("portfolio command source IDs must be unique")

    acquisition_rows = _objects(
        raw["source_acquisitions"], "portfolio source_acquisitions"
    )
    if not acquisition_rows:
        raise PipelineValidationError("portfolio source acquisitions are empty")
    acquisitions: list[dict[str, str]] = []
    for row in acquisition_rows:
        _strict(row, {"adapter_commit", "node_id"}, "portfolio source acquisition")
        adapter_commit = str(row["adapter_commit"])
        if _COMMIT.fullmatch(adapter_commit) is None:
            raise PipelineValidationError("source adapter_commit is invalid")
        acquisitions.append(
            {
                "adapter_commit": adapter_commit,
                "node_id": _identifier(row["node_id"], "source acquisition.node_id"),
            }
        )
    if len({row["node_id"] for row in acquisitions}) != len(acquisitions):
        raise PipelineValidationError("portfolio source acquisition IDs must be unique")

    authority = _object(raw["authority_snapshot"], "portfolio authority_snapshot")
    _strict(
        authority,
        {"artifact_node", "command_node"},
        "portfolio authority_snapshot",
    )
    authority_nodes = {
        "artifact_node": _identifier(
            authority["artifact_node"], "authority_snapshot.artifact_node"
        ),
        "command_node": _identifier(
            authority["command_node"], "authority_snapshot.command_node"
        ),
    }

    raw_stages = _object(raw["stages"], "portfolio stages")
    if set(raw_stages) != set(_PORTFOLIO_STAGE_ORDER):
        raise PipelineValidationError("portfolio stages differ from the required DAG")
    stages: dict[str, dict[str, object]] = {}
    for role in _PORTFOLIO_STAGE_ORDER:
        stage = _object(raw_stages[role], f"portfolio stage {role}")
        _strict(stage, {"command_node", "generation_nodes"}, f"portfolio stage {role}")
        stages[role] = {
            "command_node": _identifier(
                stage["command_node"], f"portfolio stage {role}.command_node"
            ),
            "generation_nodes": tuple(
                _identifier(item, f"portfolio stage {role}.generation_nodes")
                for item in _strings(
                    stage["generation_nodes"],
                    f"portfolio stage {role}.generation_nodes",
                )
            ),
        }
    semantic_node = _identifier(
        raw["semantic_reconciliation_node"],
        "portfolio semantic_reconciliation_node",
    )
    return {
        "acquisitions": tuple(acquisitions),
        "authority": authority_nodes,
        "command_sources": tuple(command_sources),
        "contract_path": gate.path,
        "release_commit": release_commit,
        "runtime": {
            "environment_sha256": environment_sha256,
            "executable_path": executable_path,
            "executable_sha256": executable_sha256,
            "lock_path": lock_path,
            "lock_sha256": lock_sha256,
            "repo_root": repo_root,
        },
        "semantic_node": semantic_node,
        "stages": stages,
    }


def _node_ancestors(spec: PipelineSpec) -> dict[str, frozenset[str]]:
    by_id = {item.node_id: item for item in spec.nodes}
    result: dict[str, frozenset[str]] = {}

    def visit(node_id: str) -> frozenset[str]:
        if node_id in result:
            return result[node_id]
        direct = by_id[node_id].depends_on
        expanded = set(direct)
        for dependency in direct:
            expanded.update(visit(dependency))
        result[node_id] = frozenset(expanded)
        return result[node_id]

    for node_id in by_id:
        visit(node_id)
    return result


def runtime_environment_sha256(executable: Path) -> str:
    """Hash the installed distribution inventory of one Python runtime."""

    script = (
        "import hashlib,importlib.metadata,json;"
        "rows=sorted({((d.metadata.get('Name') or '').lower(),d.version) "
        "for d in importlib.metadata.distributions()});"
        "payload=json.dumps(rows,separators=(',',':'),ensure_ascii=True).encode();"
        "print(hashlib.sha256(payload).hexdigest())"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-c", script],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineValidationError(
            "portfolio runtime environment cannot be inspected"
        ) from exc
    value = completed.stdout.decode("ascii", errors="strict").strip()
    return _hash(value, "portfolio runtime environment inventory")


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineValidationError(
            "portfolio Git identity cannot be inspected"
        ) from exc
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _verify_portfolio_runtime(contract: Mapping[str, Any], spec: PipelineSpec) -> None:
    runtime = _object(contract["runtime"], "portfolio runtime")
    executable = runtime["executable_path"]
    lock = runtime["lock_path"]
    if not isinstance(executable, Path) or not isinstance(lock, Path):
        raise PipelineValidationError("portfolio runtime paths are invalid")
    if _file_hash(executable)[0] != runtime["executable_sha256"]:
        raise PipelineValidationError("portfolio runtime executable differs")
    if _file_hash(lock)[0] != runtime["lock_sha256"]:
        raise PipelineValidationError("portfolio runtime lock differs")
    if runtime_environment_sha256(executable) != runtime["environment_sha256"]:
        raise PipelineValidationError("portfolio runtime environment differs")
    repo_root = runtime["repo_root"]
    if not isinstance(repo_root, Path) or not repo_root.is_dir():
        raise PipelineValidationError("portfolio repository root is invalid")
    head = _git_output(repo_root, "rev-parse", "HEAD")
    if head != spec.code_commit or head != contract["release_commit"]:
        raise PipelineValidationError("portfolio repository HEAD differs")
    if _git_output(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PipelineValidationError("portfolio repository is not clean")


def _validate_portfolio_orchestration(
    spec: PipelineSpec, *, verify_runtime: bool
) -> dict[str, Any] | None:
    contract = _portfolio_contract(spec)
    if contract is None:
        return None
    if verify_runtime:
        _verify_portfolio_runtime(contract, spec)
    by_id = {item.node_id: item for item in spec.nodes}
    ancestors = _node_ancestors(spec)
    runtime = _object(contract["runtime"], "portfolio runtime")
    executable = runtime["executable_path"]
    lock = runtime["lock_path"]
    if not isinstance(executable, Path) or not isinstance(lock, Path):
        raise PipelineValidationError("portfolio runtime paths are invalid")
    command_sources = tuple(contract["command_sources"])
    source_by_node = {str(row["node_id"]): row for row in command_sources}

    def command(node_id: str, name: str, *, local_only: bool) -> CommandNode:
        node = by_id.get(node_id)
        if not isinstance(node, CommandNode):
            raise PipelineValidationError(f"{name} must identify a command node")
        if local_only and node.effect is not CommandEffect.LOCAL_ONLY:
            raise PipelineValidationError(f"{name} must be local-only")
        expected_retry_limit = 1 if node_id == "build-workbook" else 0
        if node.retry_limit != expected_retry_limit:
            raise PipelineValidationError(
                f"{name} must use retry limit {expected_retry_limit}"
            )
        if not node.declared_writes:
            raise PipelineValidationError(f"{name} must declare create-only outputs")
        if not _same_path(Path(node.argv[0]), executable):
            raise PipelineValidationError(f"{name} runtime differs")
        if not any(_same_path(path, lock) for path in node.declared_reads):
            raise PipelineValidationError(f"{name} does not bind the runtime lock")
        receipt = _read_receipt(_receipt_path(spec, node.node_id))
        if receipt is None and any(path.exists() for path in node.declared_writes):
            raise PipelineValidationError(
                f"{name} output predates its pipeline receipt"
            )
        return node

    def reads_output(node: CommandNode, output: Path) -> bool:
        return any(
            _same_path(path, output) or _within_path(path, output)
            for path in node.declared_reads
        )

    for node in spec.nodes:
        values: list[object] = []
        if isinstance(node, ArtifactNode):
            values.append(node.path)
        elif isinstance(node, GenerationNode):
            if node.source_dir is not None:
                values.append(node.source_dir)
        else:
            values.extend((*node.argv, *node.declared_reads, *node.declared_writes))
        if any(_contains_ambient_current(value) for value in values):
            raise PipelineValidationError(
                f"{node.node_id} uses ambient CURRENT authority"
            )

    acquisition_rows = tuple(contract["acquisitions"])
    acquisition_ids = tuple(row["node_id"] for row in acquisition_rows)
    acquisition_commands = {
        node_id: command(
            node_id,
            f"source acquisition {node_id}",
            local_only=False,
        )
        for node_id in acquisition_ids
    }
    for row in acquisition_rows:
        node_id = row["node_id"]
        if row["adapter_commit"] != spec.code_commit:
            raise PipelineValidationError(
                f"source acquisition {node_id} adapter commit differs"
            )
        node = acquisition_commands[node_id]
        if any(
            "runs" not in {part.lower() for part in path.parts}
            for path in node.declared_writes
        ):
            raise PipelineValidationError(
                f"source acquisition {node_id} does not write an immutable run"
            )

    authority = _object(contract["authority"], "portfolio authority")
    authority_command_id = str(authority["command_node"])
    authority_command = command(
        authority_command_id, "authority snapshot command", local_only=True
    )
    if not set(acquisition_ids) <= ancestors[authority_command_id]:
        raise PipelineValidationError("authority snapshot omits a source acquisition")
    if any(
        not reads_output(authority_command, output)
        for acquisition in acquisition_commands.values()
        for output in acquisition.declared_writes
    ):
        raise PipelineValidationError(
            "authority snapshot does not read every source acquisition"
        )
    authority_artifact_id = str(authority["artifact_node"])
    authority_artifact = by_id.get(authority_artifact_id)
    if not isinstance(authority_artifact, ArtifactNode):
        raise PipelineValidationError(
            "authority snapshot must identify an artifact node"
        )
    if authority_command_id not in ancestors[authority_artifact_id]:
        raise PipelineValidationError(
            "authority snapshot artifact is not command-built"
        )
    if not any(
        _within_path(authority_artifact.path, path)
        for path in authority_command.declared_writes
    ):
        raise PipelineValidationError(
            "authority snapshot artifact is outside its output"
        )

    stage_contract = _object(contract["stages"], "portfolio stages")
    stage_commands: dict[str, CommandNode] = {}
    previous_id = authority_artifact_id
    previous_outputs: tuple[Path, ...] = (authority_artifact.path,)
    covered_generations: set[str] = set()
    for role in _PORTFOLIO_STAGE_ORDER:
        stage = _object(stage_contract[role], f"portfolio stage {role}")
        command_id = str(stage["command_node"])
        stage_command = command(command_id, f"portfolio stage {role}", local_only=True)
        if previous_id not in ancestors[command_id]:
            raise PipelineValidationError(
                f"portfolio stage {role} is disconnected from its predecessor"
            )
        if any(not reads_output(stage_command, output) for output in previous_outputs):
            raise PipelineValidationError(
                f"portfolio stage {role} does not read its predecessor output"
            )
        stage_commands[role] = stage_command
        previous_id = command_id
        generation_ids = stage["generation_nodes"]
        if not isinstance(generation_ids, tuple):
            raise PipelineValidationError(f"portfolio stage {role} is invalid")
        for generation_node_id in generation_ids:
            generation = by_id.get(str(generation_node_id))
            if (
                not isinstance(generation, GenerationNode)
                or not generation.release_target
            ):
                raise PipelineValidationError(
                    f"portfolio stage {role} output is not a release generation"
                )
            if command_id not in ancestors[generation.node_id]:
                raise PipelineValidationError(
                    f"portfolio stage {role} generation is not command-built"
                )
            if generation.source_dir is None or not any(
                _within_path(generation.source_dir, path)
                for path in stage_command.declared_writes
            ):
                raise PipelineValidationError(
                    f"portfolio stage {role} generation is outside its output"
                )
            if generation.node_id in covered_generations:
                raise PipelineValidationError(
                    "portfolio release generation is duplicated"
                )
            covered_generations.add(generation.node_id)
        generated_outputs = tuple(
            generation.source_dir
            for generation_node_id in generation_ids
            if isinstance(
                (generation := by_id.get(str(generation_node_id))), GenerationNode
            )
            and generation.source_dir is not None
        )
        previous_outputs = generated_outputs or stage_command.declared_writes
    release_target_nodes = {
        node.node_id
        for node in spec.nodes
        if isinstance(node, GenerationNode) and node.release_target
    }
    if covered_generations != release_target_nodes:
        raise PipelineValidationError(
            "portfolio stage outputs differ from release generations"
        )

    semantic_id = str(contract["semantic_node"])
    semantic = command(
        semantic_id, "post-build semantic reconciliation", local_only=True
    )
    if not release_target_nodes <= ancestors[semantic_id]:
        raise PipelineValidationError(
            "post-build semantic reconciliation omits a release generation"
        )
    if stage_commands["panel"].node_id not in ancestors[semantic_id]:
        raise PipelineValidationError(
            "post-build semantic reconciliation precedes the panel"
        )
    release_generation_sources = tuple(
        node.source_dir
        for node in spec.nodes
        if isinstance(node, GenerationNode)
        and node.release_target
        and node.source_dir is not None
    )
    semantic_inputs = (
        authority_artifact.path,
        *release_generation_sources,
        *stage_commands["panel"].declared_writes,
    )
    if any(not reads_output(semantic, output) for output in semantic_inputs):
        raise PipelineValidationError(
            "post-build semantic reconciliation omits an explicit input"
        )
    semantic_gates = [
        item for item in spec.gates if item.gate_id == _POST_BUILD_SEMANTIC_GATE
    ]
    if len(semantic_gates) != 1:
        raise PipelineValidationError(
            "post-build semantic gate is missing or ambiguous"
        )
    if not any(
        _same_path(semantic_gates[0].path, path) for path in semantic.declared_writes
    ):
        raise PipelineValidationError("post-build semantic gate is not command-built")

    required_command_ids = {
        *acquisition_ids,
        authority_command_id,
        *(item.node_id for item in stage_commands.values()),
        semantic_id,
    }
    if set(source_by_node) != required_command_ids:
        raise PipelineValidationError(
            "portfolio command sources differ from the executable DAG"
        )
    repo_root = runtime["repo_root"]
    if not isinstance(repo_root, Path):
        raise PipelineValidationError("portfolio repository root is invalid")
    for node_id, row in source_by_node.items():
        node = by_id[node_id]
        if not isinstance(node, CommandNode):
            raise PipelineValidationError(f"{node_id} command source is not executable")
        entrypoint = row["entrypoint_path"]
        if not isinstance(entrypoint, Path) or not _within_path(entrypoint, repo_root):
            raise PipelineValidationError(
                f"{node_id} entrypoint is outside the repository"
            )
        if _file_hash(entrypoint)[0] != row["entrypoint_sha256"]:
            raise PipelineValidationError(f"{node_id} entrypoint differs")
        if not any(_same_path(path, entrypoint) for path in node.declared_reads):
            raise PipelineValidationError(f"{node_id} does not declare its entrypoint")
        if not any(
            _same_path(Path(token), entrypoint)
            for token in node.argv[1:]
            if Path(token).is_absolute()
        ):
            raise PipelineValidationError(f"{node_id} does not execute its entrypoint")
    return contract


def _verify_post_build_semantic_gate(spec: PipelineSpec, path: Path) -> None:
    try:
        payload = _object(
            json.loads(path.read_text(encoding="utf-8")),
            "post-build semantic gate",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError("post-build semantic gate is unreadable") from exc
    _strict(
        payload,
        {
            "as_of_date",
            "checks",
            "release_commit",
            "release_generations",
            "schema_version",
            "unwaived_blocking_exceptions",
            "verdict",
        },
        "post-build semantic gate",
    )
    checks = _object(payload["checks"], "post-build semantic checks")
    if set(checks) != _POST_BUILD_CHECKS or any(
        value != "PASS" for value in checks.values()
    ):
        raise PipelineValidationError("post-build semantic checks did not pass")
    if (
        payload["schema_version"] != 1
        or payload["verdict"] != "PASS"
        or payload["unwaived_blocking_exceptions"] != 0
        or payload["as_of_date"] != spec.as_of_date.isoformat()
        or payload["release_commit"] != spec.code_commit
        or payload["release_generations"] != spec.release_generations
    ):
        raise PipelineValidationError(
            "post-build semantic gate differs from the release"
        )


def _run_root(spec: PipelineSpec) -> Path:
    # Windows still encounters MAX_PATH-sensitive APIs in otherwise long-path-aware
    # processes. The full specification hash remains inside every receipt and
    # evidence payload; a 16-hex path key is sufficient only for directory routing.
    run_key = f"{spec.pipeline_id}-{spec.spec_sha256[:16]}"
    return spec.state_root / "pipelines" / run_key


def _receipt_path(spec: PipelineSpec, node_id: str) -> Path:
    return _run_root(spec) / "receipts" / f"{node_id}.json"


def _node_hash(node: PipelineNode) -> str:
    return hashlib.sha256(canonical_json_bytes(node.to_dict())).hexdigest()


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("pipeline receipt is not a regular file")
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), "receipt")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError("pipeline receipt is unreadable") from exc


def _write_receipt(
    spec: PipelineSpec,
    node: PipelineNode,
    state: ReceiptState,
    attempts: int,
    detail_sha256: str | None,
    command_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = {
        "attempts": attempts,
        "command_evidence": (
            dict(command_evidence) if command_evidence is not None else None
        ),
        "detail_sha256": detail_sha256,
        "node_hash": _node_hash(node),
        "node_id": node.node_id,
        "request_identity": (
            node.request_identity if isinstance(node, CommandNode) else None
        ),
        "spec_sha256": spec.spec_sha256,
        "state": state.value,
    }
    _atomic_write(_receipt_path(spec, node.node_id), canonical_json_bytes(receipt))
    return receipt


def _path_evidence(path: Path) -> dict[str, object]:
    """Hash one declared command boundary without retaining its content."""

    resolved = path.resolve()
    base: dict[str, object] = {"path": str(resolved)}
    if path.is_symlink():
        raise PipelineValidationError(f"declared command path is a link: {path}")
    if not path.exists():
        return {**base, "state": "MISSING"}
    if path.is_file():
        digest, size = _file_hash(path)
        return {
            **base,
            "sha256": digest,
            "size_bytes": size,
            "state": "FILE",
        }
    if not path.is_dir():
        raise PipelineValidationError(
            f"declared command path is not a regular file or directory: {path}"
        )
    files: list[dict[str, object]] = []
    total_size = 0
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise PipelineValidationError(
                f"declared command directory contains a link: {item}"
            )
        if item.is_file():
            digest, size = _file_hash(item)
            total_size += size
            files.append(
                {
                    "relative_path": item.relative_to(path).as_posix(),
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
    inventory = canonical_json_bytes({"files": files})
    return {
        **base,
        "file_count": len(files),
        "sha256": hashlib.sha256(inventory).hexdigest(),
        "size_bytes": total_size,
        "state": "DIRECTORY",
    }


def _io_evidence(node: CommandNode) -> dict[str, object]:
    return {
        "declared_reads": [_path_evidence(path) for path in node.declared_reads],
        "declared_writes": [_path_evidence(path) for path in node.declared_writes],
    }


def _argv_evidence(argv: tuple[str, ...]) -> dict[str, object]:
    """Retain argv identity and ordering without copying possible credentials."""

    tokens = []
    for index, value in enumerate(argv):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        tokens.append(
            {
                "index": index,
                "sha256": digest,
                "value": Path(value).name if index == 0 else f"<sha256:{digest}>",
            }
        )
    return {
        "argv": tokens,
        "argv_sha256": hashlib.sha256(
            canonical_json_bytes({"argv": list(argv)})
        ).hexdigest(),
    }


def _tool_identity(node: CommandNode) -> dict[str, object]:
    resolved_text = shutil.which(node.argv[0])
    resolved = Path(resolved_text).resolve() if resolved_text is not None else None
    result: dict[str, object] = {
        "executable_name": Path(node.argv[0]).name,
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "resolved_path": str(resolved) if resolved is not None else None,
    }
    if resolved is not None and resolved.is_file() and not resolved.is_symlink():
        digest, size = _file_hash(resolved)
        result.update({"executable_sha256": digest, "executable_size_bytes": size})
    if resolved is not None and os.path.normcase(str(resolved)) == os.path.normcase(
        str(Path(sys.executable).resolve())
    ):
        result["python_executable"] = True
    return result


def _command_evidence_base(node: CommandNode) -> dict[str, object]:
    return {
        **_argv_evidence(node.argv),
        "cwd": str(node.cwd.resolve()),
        "effect": node.effect.value,
        "tool_identity": _tool_identity(node),
    }


def _completed_command_outputs_match(
    node: CommandNode, receipt: Mapping[str, Any]
) -> bool:
    command = receipt.get("command_evidence")
    if not isinstance(command, Mapping):
        return False
    attempts = command.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    final = attempts[-1]
    if not isinstance(final, Mapping):
        return False
    return final.get("post_io") == _io_evidence(node)


def _inventory(path: Path) -> tuple[FileExpectation, ...]:
    if path.is_symlink() or not path.is_dir():
        raise PipelineValidationError(f"generation directory is missing: {path}")
    rows: list[FileExpectation] = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise PipelineValidationError("generation inventory contains a link")
        if item.is_file():
            digest, size = _file_hash(item)
            rows.append(
                FileExpectation(item.relative_to(path).as_posix(), digest, size)
            )
    return tuple(rows)


def inventory_generation(path: Path) -> dict[str, object]:
    """Return the exact immutable inventory needed by a generation node."""

    resolved = path.expanduser().resolve()
    files = _inventory(resolved)
    manifest_path = resolved / "generation.json"
    manifest_sha256, _ = _file_hash(manifest_path)
    manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    return {
        "files": [item.to_dict() for item in files],
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_sha256,
        "source_dir": str(resolved),
    }


def _verify_generation(
    spec: PipelineSpec, node: GenerationNode, *, materialize: bool
) -> str:
    destination = spec.roots[node.root_id] / "generations" / node.generation_id
    source = node.source_dir or destination
    expected = node.files
    if _inventory(source) != expected:
        raise PipelineValidationError(f"{node.node_id} source inventory differs")
    manifest_path = source / "generation.json"
    manifest_hash, _ = _file_hash(manifest_path)
    if manifest_hash != node.manifest_sha256:
        raise PipelineValidationError(f"{node.node_id} manifest hash differs")
    manifest = GenerationManifest.from_json_bytes(manifest_path.read_bytes())
    if manifest.generation_id != node.generation_id:
        raise PipelineValidationError(f"{node.node_id} generation identity differs")
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise PipelineValidationError(f"{node.node_id} generation is not verified")
    if (
        node.release_target
        and manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    ):
        raise PipelineValidationError(f"{node.node_id} release coverage is incomplete")
    if source.resolve() != destination.resolve() and materialize:
        if destination.exists():
            if _inventory(destination) != expected:
                raise PipelineExecutionError(
                    f"{node.node_id} destination generation differs"
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{node.node_id}-", dir=destination.parent)
            )
            try:
                if temporary.parent.resolve() != destination.parent.resolve():
                    raise PipelineExecutionError("temporary generation path escaped")
                for item in expected:
                    source_file = source / PurePosixPath(item.relative_path)
                    target_file = temporary / PurePosixPath(item.relative_path)
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, target_file)
                if _inventory(temporary) != expected:
                    raise PipelineExecutionError(
                        f"{node.node_id} staged inventory differs"
                    )
                temporary.rename(destination)
                _sync_directory(destination.parent)
            finally:
                if (
                    temporary.exists()
                    and temporary.parent.resolve() == destination.parent.resolve()
                ):
                    shutil.rmtree(temporary)
    if materialize and _inventory(destination) != expected:
        raise PipelineExecutionError(f"{node.node_id} destination verification failed")
    return manifest_hash


CommandRunner = Callable[[CommandNode], tuple[int, bytes, bytes]]


def _default_command_runner(node: CommandNode) -> tuple[int, bytes, bytes]:
    completed = subprocess.run(
        node.argv,
        cwd=node.cwd,
        check=False,
        capture_output=True,
        timeout=node.timeout_seconds,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _execute_node(
    spec: PipelineSpec,
    node: PipelineNode,
    *,
    allow_external_read: bool,
    command_runner: CommandRunner,
) -> dict[str, Any]:
    path = _receipt_path(spec, node.node_id)
    existing = _read_receipt(path)
    if existing is not None:
        if (
            existing.get("node_hash") != _node_hash(node)
            or existing.get("spec_sha256") != spec.spec_sha256
        ):
            raise PipelineValidationError(f"{node.node_id} receipt identity differs")
        state = ReceiptState(str(existing.get("state")))
        if state is ReceiptState.UNKNOWN:
            raise PipelineUnknownOutcomeError(
                f"{node.node_id} has an unknown external-read outcome"
            )
        if state is ReceiptState.COMPLETED:
            if isinstance(node, ArtifactNode):
                if _file_hash(node.path) != (node.sha256, node.size_bytes):
                    raise PipelineValidationError(f"{node.node_id} artifact drifted")
            elif isinstance(node, GenerationNode):
                _verify_generation(spec, node, materialize=True)
            elif not _completed_command_outputs_match(node, existing):
                raise PipelineValidationError(
                    f"{node.node_id} declared command outputs drifted"
                )
            return existing
        if state is ReceiptState.FAILED and int(existing.get("attempts", 0)) >= (
            node.retry_limit + 1 if isinstance(node, CommandNode) else 1
        ):
            raise PipelineExecutionError(
                f"{node.node_id} exhausted its declared attempt limit"
            )
    attempts = int(existing.get("attempts", 0)) if existing else 0
    if isinstance(node, ArtifactNode):
        if _file_hash(node.path) != (node.sha256, node.size_bytes):
            raise PipelineValidationError(f"{node.node_id} artifact differs")
        return _write_receipt(spec, node, ReceiptState.COMPLETED, 1, node.sha256)
    if isinstance(node, GenerationNode):
        detail = _verify_generation(spec, node, materialize=True)
        return _write_receipt(spec, node, ReceiptState.COMPLETED, 1, detail)
    if node.effect is CommandEffect.READ_ONLY_EXTERNAL and not allow_external_read:
        raise PipelineExecutionError(
            f"{node.node_id} requires explicit external-read authorization"
        )
    if node.effect is CommandEffect.READ_ONLY_EXTERNAL and existing is not None:
        raise PipelineUnknownOutcomeError(
            f"{node.node_id} external request was already attempted"
        )
    allowed_attempts = node.retry_limit + 1
    prior_command = existing.get("command_evidence") if existing else None
    prior_attempts = (
        list(prior_command.get("attempts", []))
        if isinstance(prior_command, Mapping)
        else []
    )
    while attempts < allowed_attempts:
        attempts += 1
        pre_io = _io_evidence(node)
        try:
            return_code, stdout, stderr = command_runner(node)
        except Exception as exc:
            attempt_evidence = {
                "attempt": attempts,
                "exception_type": type(exc).__name__,
                "outcome": "UNKNOWN"
                if node.effect is CommandEffect.READ_ONLY_EXTERNAL
                else "FAILED",
                "post_io": _io_evidence(node),
                "pre_io": pre_io,
                "return_code": None,
                "stderr_sha256": None,
                "stdout_sha256": None,
            }
            prior_attempts.append(attempt_evidence)
            command_evidence = {
                **_command_evidence_base(node),
                "attempts": prior_attempts,
            }
            state = (
                ReceiptState.UNKNOWN
                if node.effect is CommandEffect.READ_ONLY_EXTERNAL
                else ReceiptState.FAILED
            )
            _write_receipt(
                spec,
                node,
                state,
                attempts,
                None,
                command_evidence,
            )
            if state is ReceiptState.UNKNOWN:
                raise PipelineUnknownOutcomeError(
                    f"{node.node_id} external-read outcome is unknown"
                ) from exc
            raise PipelineExecutionError(f"{node.node_id} command failed") from exc
        detail = hashlib.sha256(
            canonical_json_bytes(
                {
                    "return_code": return_code,
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                }
            )
        ).hexdigest()
        attempt_evidence = {
            "attempt": attempts,
            "exception_type": None,
            "outcome": "COMPLETED" if return_code == 0 else "FAILED",
            "post_io": _io_evidence(node),
            "pre_io": pre_io,
            "return_code": return_code,
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        }
        prior_attempts.append(attempt_evidence)
        command_evidence = {
            **_command_evidence_base(node),
            "attempts": prior_attempts,
        }
        if return_code == 0:
            return _write_receipt(
                spec,
                node,
                ReceiptState.COMPLETED,
                attempts,
                detail,
                command_evidence,
            )
        state = (
            ReceiptState.UNKNOWN
            if node.effect is CommandEffect.READ_ONLY_EXTERNAL
            else ReceiptState.FAILED
        )
        _write_receipt(
            spec,
            node,
            state,
            attempts,
            detail,
            command_evidence,
        )
        if state is ReceiptState.UNKNOWN:
            raise PipelineUnknownOutcomeError(
                f"{node.node_id} external-read outcome is unknown"
            )
        raise PipelineExecutionError(
            f"{node.node_id} command exited with {return_code}"
        )
    raise AssertionError("bounded command loop did not terminate")


def plan_pipeline(spec: PipelineSpec) -> dict[str, object]:
    """Return the exact non-mutating execution and release plan."""

    _validate_portfolio_orchestration(spec, verify_runtime=True)
    nodes: list[dict[str, object]] = []
    for node in spec.ordered_nodes:
        row: dict[str, object] = {
            "depends_on": list(node.depends_on),
            "effect": (
                node.effect.value
                if isinstance(node, CommandNode)
                else "LOCAL_READ_ONLY"
                if isinstance(node, ArtifactNode)
                else "LOCAL_CREATE_ONLY_GENERATION"
            ),
            "kind": node.kind.value,
            "node_id": node.node_id,
        }
        if isinstance(node, ArtifactNode):
            row.update({"reads": [str(node.path)], "sha256": node.sha256, "writes": []})
        elif isinstance(node, GenerationNode):
            destination = spec.roots[node.root_id] / "generations" / node.generation_id
            row.update(
                {
                    "generation_id": node.generation_id,
                    "reads": [str(node.source_dir or destination)],
                    "release_target": node.release_target,
                    "root_id": node.root_id,
                    "writes": ([] if node.source_dir is None else [str(destination)]),
                }
            )
        else:
            row.update(
                {
                    "argv": list(node.argv),
                    "reads": [str(item) for item in node.declared_reads],
                    "request_identity": node.request_identity,
                    "retry_limit": node.retry_limit,
                    "writes": [str(item) for item in node.declared_writes],
                }
            )
        nodes.append(row)
    return {
        "commit_effects": [
            *[
                {
                    "generation_id": generation_id,
                    "root_id": root_id,
                    "type": "CURRENT_POINTER",
                }
                for root_id, generation_id in sorted(spec.release_generations.items())
            ],
            *[
                {
                    "companion_id": item.companion_id,
                    "relative_path": item.relative_path,
                    "root_id": item.root_id,
                    "type": "COMPANION",
                }
                for item in spec.companions
            ],
        ],
        "node_count": len(nodes),
        "nodes": nodes,
        "pipeline_id": spec.pipeline_id,
        "spec_sha256": spec.spec_sha256,
    }


def _verify_freshness_gate(path: Path, evaluation_date: date) -> None:
    try:
        payload = _object(
            json.loads(path.read_text(encoding="utf-8")), "freshness gate"
        )
        schema_version = payload.get("assessment_schema_version")
        expected_keys = (
            {
                "assessment_schema_version",
                "as_of_date",
                "assessments",
                "policy",
            }
            if schema_version == 2
            else {
                "assessment_schema_version",
                "as_of_date",
                "assessments",
                "policies",
            }
            if schema_version == 3
            else set()
        )
        if set(payload) != expected_keys:
            raise PipelineValidationError("freshness gate schema differs")
        assessed_on = date.fromisoformat(str(payload.get("as_of_date")))
        if assessed_on != evaluation_date:
            raise PipelineValidationError(
                "freshness gate was not evaluated on the operational date"
            )
        if schema_version == 2:
            parsed = FreshnessPolicy.from_dict(_object(payload.get("policy"), "policy"))
            policies = {parsed.policy_id: parsed}
        else:
            policies = {
                parsed.policy_id: parsed
                for parsed in (
                    FreshnessPolicy.from_dict(item)
                    for item in _objects(payload.get("policies"), "policies")
                )
            }
            if not policies:
                raise PipelineValidationError("freshness gate policies are unavailable")
        raw_assessments = payload.get("assessments")
        if not isinstance(raw_assessments, list) or not raw_assessments:
            raise PipelineValidationError("freshness gate assessments are unavailable")
        for raw in raw_assessments:
            item = _object(raw, "freshness assessment")
            policy_id = item.get("policy_id")
            if not isinstance(policy_id, str) or policy_id not in policies:
                raise PipelineValidationError(
                    "freshness gate policy reference is invalid"
                )
            policy = policies[policy_id]
            observed_text = item.get("last_accepted_date")
            observed = (
                None
                if observed_text is None
                else date.fromisoformat(str(observed_text))
            )
            expected = policy.assess(observed, evaluation_date).to_dict()
            required_for_current = item.get("required_for_current", True)
            if not isinstance(required_for_current, bool):
                raise PipelineValidationError(
                    "freshness gate required-for-current flag is invalid"
                )
            if not required_for_current:
                expected = {
                    **expected,
                    "blocks_current_consumers": False,
                    "escalation_required": False,
                }
            for field in (
                "blocks_current_consumers",
                "escalation_required",
                "evaluated_on",
                "next_expected_date",
                "policy_id",
                "reason",
                "state",
            ):
                if item.get(field) != expected[field]:
                    raise PipelineValidationError(
                        f"freshness gate assessment is stale: {item.get('subject_id')}"
                    )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, PipelineValidationError):
            raise
        raise PipelineValidationError("freshness gate is invalid") from exc


def _recorded_pointer(root: Path) -> CurrentPointer:
    path = root / "CURRENT.json"
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("predecessor CURRENT pointer is unavailable")
    try:
        return CurrentPointer.from_json_bytes(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise PipelineValidationError("predecessor CURRENT pointer is invalid") from exc


def _verify_predecessor_repair_gate(spec: PipelineSpec, path: Path) -> None:
    """Bind the exact artifact-inventory drift of one invalid predecessor graph."""

    try:
        payload = _object(
            json.loads(path.read_text(encoding="utf-8")),
            "predecessor repair gate",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError("predecessor repair gate is invalid") from exc
    _strict(
        payload,
        {
            "authority_sha256",
            "disposition",
            "expected_inventory_drifts",
            "release_id",
            "release_manifest_sha256",
            "schema_version",
        },
        "predecessor repair gate",
    )
    if payload["schema_version"] != 1 or payload["disposition"] != (
        "SUPERSEDE_WITH_VERIFIED_GENERATIONS"
    ):
        raise PipelineValidationError("predecessor repair disposition differs")
    authority_path = spec.coordination_root / "CURRENT_RELEASE.json"
    if authority_path.is_symlink() or not authority_path.is_file():
        raise PipelineValidationError("predecessor authority is unavailable")
    authority_bytes = authority_path.read_bytes()
    authority_sha256 = hashlib.sha256(authority_bytes).hexdigest()
    if authority_sha256 != _hash(
        payload["authority_sha256"], "predecessor authority_sha256"
    ):
        raise PipelineValidationError("predecessor authority bytes differ")
    authority = ReleaseAuthorityPointer.from_json_bytes(authority_bytes)
    if authority.release_id != _hash(payload["release_id"], "predecessor release_id"):
        raise PipelineValidationError("predecessor release identity differs")
    if authority.release_manifest_sha256 != _hash(
        payload["release_manifest_sha256"],
        "predecessor release_manifest_sha256",
    ):
        raise PipelineValidationError("predecessor manifest identity differs")
    manifest, journal = read_release(spec.coordination_root, authority.release_id)
    if journal.state is not ReleaseState.COMMITTED:
        raise PipelineValidationError("predecessor release is not COMMITTED")
    targets = {item.root_id: item for item in manifest.targets}
    if set(targets) != set(spec.release_generations):
        raise PipelineValidationError("predecessor release root graph differs")
    actual_drifts: list[dict[str, str]] = []
    for root_id, target in sorted(targets.items()):
        root = spec.roots[root_id]
        pointer = _recorded_pointer(root)
        if pointer.to_dict() != {
            "artifact_inventory_sha256": target.proposed_inventory_sha256,
            "generation_id": target.proposed_generation_id,
            "manifest_sha256": target.proposed_manifest_sha256,
            "pointer_schema_version": 2,
            "state": "PUBLISHED",
        }:
            raise PipelineValidationError(
                f"predecessor pointer graph differs: {root_id}"
            )
        generation = read_generation(root, target.proposed_generation_id)
        if hashlib.sha256(generation.to_json_bytes()).hexdigest() != (
            target.proposed_manifest_sha256
        ):
            raise PipelineValidationError(
                f"predecessor generation manifest differs: {root_id}"
            )
        observed = generation_inventory_sha256(root, target.proposed_generation_id)
        if observed != target.proposed_inventory_sha256:
            actual_drifts.append(
                {
                    "generation_id": target.proposed_generation_id,
                    "observed_inventory_sha256": observed,
                    "recorded_inventory_sha256": str(target.proposed_inventory_sha256),
                    "root_id": root_id,
                }
            )
    if not actual_drifts:
        raise PipelineValidationError(
            "predecessor repair gate found no artifact inventory drift"
        )
    expected_rows = _objects(
        payload["expected_inventory_drifts"],
        "expected predecessor inventory drifts",
    )
    expected_drifts: list[dict[str, str]] = []
    for row in expected_rows:
        _strict(
            row,
            {
                "generation_id",
                "observed_inventory_sha256",
                "recorded_inventory_sha256",
                "root_id",
            },
            "expected predecessor inventory drift",
        )
        expected_drifts.append(
            {
                "generation_id": _hash(
                    row["generation_id"], "expected predecessor generation_id"
                ),
                "observed_inventory_sha256": _hash(
                    row["observed_inventory_sha256"],
                    "expected predecessor observed inventory",
                ),
                "recorded_inventory_sha256": _hash(
                    row["recorded_inventory_sha256"],
                    "expected predecessor recorded inventory",
                ),
                "root_id": _identifier(row["root_id"], "expected predecessor root_id"),
            }
        )
    if sorted(expected_drifts, key=lambda item: item["root_id"]) != actual_drifts:
        raise PipelineValidationError("predecessor inventory drift set differs")


def _verify_gates(
    spec: PipelineSpec,
    evaluation_date: date,
    *,
    verify_predecessor_repair: bool = True,
) -> tuple[GateEvidence, ...]:
    _validate_portfolio_orchestration(spec, verify_runtime=True)
    result = []
    for gate in spec.gates:
        digest, _ = _file_hash(gate.path)
        if digest != gate.sha256:
            raise PipelineValidationError(f"pipeline gate differs: {gate.gate_id}")
        if gate.gate_id == "freshness-health":
            _verify_freshness_gate(gate.path, evaluation_date)
        elif gate.gate_id == _POST_BUILD_SEMANTIC_GATE:
            _verify_post_build_semantic_gate(spec, gate.path)
        elif gate.gate_id == _PREDECESSOR_REPAIR_GATE and verify_predecessor_repair:
            _verify_predecessor_repair_gate(spec, gate.path)
        result.append(GateEvidence(gate.gate_id, gate.sha256))
    return tuple(result)


def _pipeline_evidence(spec: PipelineSpec) -> tuple[Path, str]:
    receipts = []
    for node in spec.ordered_nodes:
        path = _receipt_path(spec, node.node_id)
        receipt = _read_receipt(path)
        if receipt is None or receipt.get("state") != ReceiptState.COMPLETED.value:
            raise PipelineExecutionError(f"pipeline node is incomplete: {node.node_id}")
        digest, size = _file_hash(path)
        receipts.append(
            {"node_id": node.node_id, "receipt_sha256": digest, "size_bytes": size}
        )
    payload = {
        "as_of_date": spec.as_of_date.isoformat(),
        "code_commit": spec.code_commit,
        "companions": [item.to_dict() for item in spec.companions],
        "gates": [item.to_dict() for item in spec.gates],
        "pipeline_id": spec.pipeline_id,
        "receipts": receipts,
        "release_generations": spec.release_generations,
        "spec_sha256": spec.spec_sha256,
    }
    content = canonical_json_bytes(payload)
    path = _run_root(spec) / "pipeline-evidence.json"
    _create_exact(path, content)
    return path, hashlib.sha256(content).hexdigest()


def _prepared_path(spec: PipelineSpec) -> Path:
    return _run_root(spec) / "prepared.json"


def _read_prepared(spec: PipelineSpec) -> str | None:
    path = _prepared_path(spec)
    if not path.exists():
        return None
    prepared = _object(json.loads(path.read_text(encoding="utf-8")), "prepared")
    if (
        set(prepared) != {"release_id", "spec_sha256"}
        or prepared.get("spec_sha256") != spec.spec_sha256
    ):
        raise PipelineValidationError("prepared release identity differs")
    return _hash(prepared.get("release_id"), "prepared.release_id")


def execute_pipeline(
    spec: PipelineSpec,
    *,
    commit: bool = False,
    allow_external_read: bool = False,
    command_runner: CommandRunner = _default_command_runner,
    fault_hook: Callable[[str, str], None] | None = None,
    evaluation_date: date | None = None,
) -> dict[str, object]:
    """Build, verify, prepare, and optionally commit one explicit-ID DAG."""

    _validate_portfolio_orchestration(spec, verify_runtime=True)
    existing_release = _read_prepared(spec)
    existing_state = None
    if existing_release is not None:
        _, existing_journal = read_release(spec.coordination_root, existing_release)
        existing_state = existing_journal.state
    for node in spec.ordered_nodes:
        _execute_node(
            spec,
            node,
            allow_external_read=allow_external_read,
            command_runner=command_runner,
        )
    operational_date = evaluation_date or datetime.now().astimezone().date()
    gate_evidence = _verify_gates(
        spec,
        operational_date,
        verify_predecessor_repair=existing_state is not ReleaseState.COMMITTED,
    )
    _evidence_path, evidence_hash = _pipeline_evidence(spec)
    if any(item.gate_id == "pipeline-evidence" for item in gate_evidence):
        raise PipelineValidationError("pipeline-evidence gate ID is reserved")
    companions = []
    for item in spec.companions:
        digest, _ = _file_hash(item.source_path)
        if digest != item.sha256:
            raise PipelineValidationError(
                f"pipeline companion differs: {item.companion_id}"
            )
        companions.append(
            CompanionInput(
                item.companion_id,
                item.root_id,
                item.relative_path,
                item.source_path.read_bytes(),
            )
        )
    if existing_release is None:
        manifest = prepare_release(
            coordination_root=spec.coordination_root,
            roots={key: spec.roots[key] for key in spec.release_generations},
            proposed_generations=spec.release_generations,
            code_commit=spec.code_commit,
            created_at=spec.created_at,
            gates=(*gate_evidence, GateEvidence("pipeline-evidence", evidence_hash)),
            companions=tuple(companions),
            allow_invalid_previous_artifact_inventory=any(
                item.gate_id == _PREDECESSOR_REPAIR_GATE for item in gate_evidence
            ),
        )
        _create_exact(
            _prepared_path(spec),
            canonical_json_bytes(
                {"release_id": manifest.release_id, "spec_sha256": spec.spec_sha256}
            ),
        )
        release_id = manifest.release_id
    else:
        release_id = existing_release
        manifest, _ = read_release(spec.coordination_root, release_id)
        if manifest.code_commit != spec.code_commit:
            raise PipelineValidationError("prepared release code commit differs")
    _, existing_journal = read_release(spec.coordination_root, release_id)
    state = existing_journal.state
    if commit:
        companion_contents = {
            item.companion_id: item.source_path.read_bytes() for item in spec.companions
        }
        journal = commit_release(
            coordination_root=spec.coordination_root,
            release_id=release_id,
            roots={key: spec.roots[key] for key in spec.release_generations},
            companion_contents=companion_contents,
            fault_hook=fault_hook,
        )
        state = journal.state
        for root_id, generation_id in spec.release_generations.items():
            if read_current(spec.roots[root_id]).generation_id != generation_id:
                raise PipelineExecutionError(f"committed root differs: {root_id}")
    return {
        "completed_nodes": len(spec.nodes),
        "pipeline_id": spec.pipeline_id,
        "release_id": release_id,
        "release_state": state.value,
        "spec_sha256": spec.spec_sha256,
        "target_count": len(spec.release_generations),
    }


def pipeline_status(spec: PipelineSpec) -> dict[str, object]:
    receipts = {
        node.node_id: (
            (_read_receipt(_receipt_path(spec, node.node_id)) or {}).get("state")
            or "PENDING"
        )
        for node in spec.ordered_nodes
    }
    release_id = _read_prepared(spec)
    release_state = None
    if release_id is not None:
        _, journal = read_release(spec.coordination_root, release_id)
        release_state = journal.state.value
    return {
        "pipeline_id": spec.pipeline_id,
        "receipts": receipts,
        "release_id": release_id,
        "release_state": release_state,
        "spec_sha256": spec.spec_sha256,
    }


def recover_pipeline(
    spec: PipelineSpec, *, clear_stale_lock: bool = False
) -> dict[str, object]:
    """Explicitly resume rollback for this specification's prepared release."""

    release_id = _read_prepared(spec)
    if release_id is None:
        raise PipelineValidationError("pipeline has no prepared release")
    journal = recover_release(
        coordination_root=spec.coordination_root,
        release_id=release_id,
        roots={key: spec.roots[key] for key in spec.release_generations},
        clear_stale_lock=clear_stale_lock,
    )
    return {
        "pipeline_id": spec.pipeline_id,
        "release_id": release_id,
        "release_state": journal.state.value,
        "spec_sha256": spec.spec_sha256,
    }

"""Explicit-ID, resumable source-to-release pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Final, Mapping, Self

from quant_engine.core.contracts import (
    CoverageStatus,
    GenerationManifest,
    VerificationStatus,
    canonical_json_bytes,
)
from quant_engine.core.publication import read_current
from quant_engine.core.release import (
    CompanionInput,
    GateEvidence,
    commit_release,
    prepare_release,
    read_release,
)


_SCHEMA_VERSION: Final = 1
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


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
) -> dict[str, Any]:
    receipt = {
        "attempts": attempts,
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
            return existing
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
    while attempts < allowed_attempts:
        attempts += 1
        try:
            return_code, stdout, stderr = command_runner(node)
        except Exception as exc:
            state = (
                ReceiptState.UNKNOWN
                if node.effect is CommandEffect.READ_ONLY_EXTERNAL
                else ReceiptState.FAILED
            )
            _write_receipt(spec, node, state, attempts, None)
            if state is ReceiptState.UNKNOWN:
                raise PipelineUnknownOutcomeError(
                    f"{node.node_id} external-read outcome is unknown"
                ) from exc
            if attempts >= allowed_attempts:
                raise PipelineExecutionError(f"{node.node_id} command failed") from exc
            continue
        detail = hashlib.sha256(
            canonical_json_bytes(
                {
                    "return_code": return_code,
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                }
            )
        ).hexdigest()
        if return_code == 0:
            return _write_receipt(spec, node, ReceiptState.COMPLETED, attempts, detail)
        state = (
            ReceiptState.UNKNOWN
            if node.effect is CommandEffect.READ_ONLY_EXTERNAL
            else ReceiptState.FAILED
        )
        _write_receipt(spec, node, state, attempts, detail)
        if state is ReceiptState.UNKNOWN:
            raise PipelineUnknownOutcomeError(
                f"{node.node_id} external-read outcome is unknown"
            )
        if attempts >= allowed_attempts:
            raise PipelineExecutionError(
                f"{node.node_id} command exited with {return_code}"
            )
    raise AssertionError("bounded command loop did not terminate")


def plan_pipeline(spec: PipelineSpec) -> dict[str, object]:
    """Return the exact non-mutating execution and release plan."""

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


def _verify_gates(spec: PipelineSpec) -> tuple[GateEvidence, ...]:
    result = []
    for gate in spec.gates:
        digest, _ = _file_hash(gate.path)
        if digest != gate.sha256:
            raise PipelineValidationError(f"pipeline gate differs: {gate.gate_id}")
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
) -> dict[str, object]:
    """Build, verify, prepare, and optionally commit one explicit-ID DAG."""

    existing_release = _read_prepared(spec)
    for node in spec.ordered_nodes:
        _execute_node(
            spec,
            node,
            allow_external_read=allow_external_read,
            command_runner=command_runner,
        )
    gate_evidence = _verify_gates(spec)
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

"""Explicit-ID, resumable source-to-release pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, BinaryIO, Callable, Final, Mapping, Self, cast

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
_FINANCIAL_MVP_PRODUCT_GATE: Final = "financial-mvp-product-conformance"
_FINANCIAL_MVP_HWM_INPUT_AUTHORITY_GATE: Final = "financial-mvp-hwm-input-authority"
_OWNER_PUBLICATION_AUTHORIZATION_GATE: Final = "owner-publication-authorization"
_FINANCIAL_EPOCH_MANIFEST_GATE: Final = "financial-epoch-manifest-input-authority"
_FINANCIAL_MVP_BOUND_CONTRACT_GATE: Final = "financial-mvp-bound-contract"
_FINANCIAL_EPOCH_MANIFEST_INPUTS: Final = {
    "account_strategy_admission_policy",
    "account_strategy_classified_flows",
    "account_strategy_flow_coverage",
    "account_strategy_ledger",
    "account_strategy_ledger_identity",
}
_FINANCIAL_MVP_AUTHORITY_GATES: Final = {
    "contract": "financial-mvp-contract-authority",
    "provider_catalog": "financial-mvp-provider-catalog-authority",
    "provider_manifest": "financial-mvp-provider-manifest-authority",
    "provider_receipt": "financial-mvp-provider-admission-authority",
    "source_matrix": "financial-mvp-source-matrix-authority",
}
_CORRECTION_REGISTRY_GATE: Final = "portfolio-correction-registry"
_REDERIVE_REGISTRY_GATE: Final = "portfolio-rederive-receipt-registry"
_PORTFOLIO_PRODUCTION_REBIND_GATE: Final = "port-underwrite-production-rebind"
_POST_BUILD_SEMANTIC_GATE: Final = "post-build-semantic"
_PREDECESSOR_REPAIR_GATE: Final = "predecessor-authority-repair"
_COMMAND_OUTPUT_CAPTURE_LIMIT_BYTES: Final = 64 * 1024
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
_PORTFOLIO_STAGE_DEPENDENCIES: Final = {
    "ledger": (),
    "baseline": ("ledger",),
    "attribution": ("baseline", "ledger"),
    "analytics": ("attribution",),
    "report": ("baseline", "ledger", "analytics"),
    "workbook": ("baseline", "ledger", "report"),
    "panel": ("ledger", "attribution", "analytics", "workbook"),
}
_FINANCIAL_MVP_CANDIDATE_ROOT_IDS: Final = {
    "ledger": ("provider-ledger",),
    "baseline": ("baseline", "portfolio-baseline"),
    "report": ("reporting", "portfolio-report"),
    "panel": ("control-panel", "portfolio-panel"),
}
_FINANCIAL_MVP_GENERATION_STAGE: Final = {
    "ledger": "ledger",
    "baseline": "baseline",
    "report": "workbook",
    "panel": "panel",
}
_SEMANTIC_FIRST_STAGE: Final = {
    "AS_HELD_COMPOSITE_HWM": "analytics",
    "COMPOSITION": "ledger",
    "PROVIDER_LEDGER": "ledger",
    "LAYERED_HISTORY": "attribution",
    "LAYERED_HISTORY_LIVE_PLUS_BACKTEST": "attribution",
    "DECOMPOSITION": "attribution",
    "HWM": "analytics",
    "INVESTED_CAPITAL": "analytics",
    "DEPENDENCE": "analytics",
    "REPORT": "report",
    "WORKBOOK": "workbook",
    "PANEL": "panel",
    "RELEASE_RECEIPT": "panel",
}
_POST_BUILD_CHECKS: Final = {
    "analytics_report_panel_equality",
    "financial_identities",
    "freshness",
    "generation_invariants",
    "ledger_bridges",
    "mappings",
}
_HWM_BINDING_KEYS: Final = {
    "calendar_authority",
    "governed_result",
    "input_generation",
    "provider_admission_receipt",
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


def _file_definition_value(value: object, name: str) -> tuple[Path, str, int]:
    definition = _object(value, name)
    _strict(definition, {"path", "sha256", "size_bytes"}, name)
    path = _absolute(definition["path"], f"{name}.path")
    sha256 = _hash(definition["sha256"], f"{name}.sha256")
    size = definition["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise PipelineValidationError(f"{name}.size_bytes must be positive")
    if _file_hash(path) != (sha256, size):
        raise PipelineValidationError(f"{name} identity differs")
    return path, sha256, size


def _financial_mvp_product_authority_legacy(
    spec: PipelineSpec,
) -> dict[str, Any] | None:
    matches = [
        item for item in spec.gates if item.gate_id == _FINANCIAL_MVP_PRODUCT_GATE
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise PipelineValidationError("financial MVP product gate is ambiguous")
    gate = matches[0]
    if _file_hash(gate.path)[0] != gate.sha256:
        raise PipelineValidationError("financial MVP product gate differs")
    try:
        authority = _object(
            json.loads(gate.path.read_text(encoding="utf-8")),
            "financial MVP product authority",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError(
            "financial MVP product authority is unreadable"
        ) from exc
    _strict(
        authority,
        {
            "contract",
            "conformance",
            "provider_generation_path",
            "source_matrix",
        },
        "financial MVP product authority",
    )
    contract_path, contract_raw_sha256, _ = _file_definition_value(
        authority["contract"], "financial MVP contract"
    )
    matrix_path, matrix_sha256, _ = _file_definition_value(
        authority["source_matrix"], "financial MVP source matrix"
    )
    try:
        contract = _object(
            json.loads(contract_path.read_text(encoding="utf-8")),
            "financial MVP contract",
        )
        matrix = _object(
            json.loads(matrix_path.read_text(encoding="utf-8")),
            "financial MVP source matrix",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError("financial MVP authority is unreadable") from exc
    conformance = _object(authority["conformance"], "financial MVP conformance")
    _strict(
        conformance,
        {
            "blocking_reasons",
            "census_memberships",
            "census_sha256",
            "contract_raw_sha256",
            "contract_semantic_sha256",
            "correction_authority",
            "history_corrections",
            "invested_capital_policy_sha256",
            "outcomes",
            "product_id",
            "product_version",
            "provider_admission",
            "readiness",
            "rederive_authority",
            "schema_version",
            "source_matrix_sha256",
            "sources",
            "strategies",
        },
        "financial MVP conformance",
    )
    semantic_sha256 = hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
    census = _object(contract.get("current_census"), "financial MVP current census")
    census_sha256 = hashlib.sha256(canonical_json_bytes(census)).hexdigest()
    product = _object(contract.get("product"), "financial MVP product")
    if (
        conformance.get("schema_version") != 1
        or conformance.get("contract_raw_sha256") != contract_raw_sha256
        or conformance.get("contract_semantic_sha256") != semantic_sha256
        or conformance.get("census_sha256") != census_sha256
        or conformance.get("source_matrix_sha256") != matrix_sha256
        or conformance.get("product_id") != product.get("product_id")
        or conformance.get("product_version") != product.get("product_version")
        or matrix.get("contract_sha256") != semantic_sha256
        or matrix.get("census_sha256") != census_sha256
        or matrix.get("product_version") != product.get("product_version")
    ):
        raise PipelineValidationError("financial MVP authority identities differ")
    sleeve_rows = _objects(census.get("account_sleeves", []), "census account sleeves")
    expected_memberships = []
    for sleeve in sleeve_rows:
        assignments = _objects(sleeve.get("assignments"), "census assignments")
        expected_memberships.append(
            {
                "assignments": sorted(
                    (
                        {
                            "effective_end": row.get("effective_end"),
                            "effective_start": row.get("effective_start"),
                            "status": row.get("status"),
                            "strategy_id": row.get("strategy_id"),
                            "transition_group_id": row.get("transition_group_id"),
                        }
                        for row in assignments
                    ),
                    key=lambda row: (
                        str(row["effective_start"]),
                        str(row["strategy_id"]),
                        str(row["effective_end"]),
                    ),
                ),
                "current_strategy_id": sleeve.get("current_strategy_id"),
                "provider_id": sleeve.get("provider_id"),
                "role": sleeve.get("role"),
                "sleeve_alias": sleeve.get("sleeve_alias"),
                "source_segment_ids": sorted(
                    _strings(sleeve.get("source_segment_ids"), "sleeve sources")
                ),
            }
        )
    expected_memberships.sort(key=lambda row: str(row["sleeve_alias"]))
    if conformance.get("census_memberships") != expected_memberships:
        raise PipelineValidationError("financial MVP census conformance differs")

    def source_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "consumers": sorted(_strings(row.get("consumers"), "source consumers")),
            "data_classes": sorted(
                _strings(row.get("data_classes"), "source data_classes")
            ),
            "invalidated_outputs": sorted(
                _strings(row.get("invalidated_outputs"), "invalidated outputs")
            ),
            "product_requirement": str(row.get("product_requirement")),
            "readiness": str(row.get("readiness")),
            "source_id": str(row.get("source_id")),
        }

    source_rows = _objects(contract.get("source_segments"), "source segments")
    expected_sources = sorted(
        (source_projection(row) for row in source_rows),
        key=lambda row: row["source_id"],
    )
    if conformance.get("sources") != expected_sources or len(
        {row["source_id"] for row in expected_sources}
    ) != len(expected_sources):
        raise PipelineValidationError("financial MVP source conformance differs")
    outcome_rows = _objects(contract.get("financial_outcomes"), "financial outcomes")
    expected_outcomes = sorted(
        (
            {
                "blockers": sorted(int(value) for value in row.get("blockers", [])),
                "consumers": sorted(
                    _strings(row.get("consumers"), "outcome consumers")
                ),
                "outcome_id": str(row.get("outcome_id")),
                "product_requirement": str(row.get("product_requirement")),
                "readiness": str(row.get("readiness")),
            }
            for row in outcome_rows
        ),
        key=lambda row: str(row["outcome_id"]),
    )
    if conformance.get("outcomes") != expected_outcomes:
        raise PipelineValidationError("financial MVP outcome conformance differs")
    strategy_rows = _objects(contract.get("strategies"), "financial MVP strategies")
    expected_strategies = sorted(
        (
            {
                "product_requirement": str(row.get("product_requirement")),
                "strategy_id": str(row.get("strategy_id")),
            }
            for row in strategy_rows
        ),
        key=lambda row: row["strategy_id"],
    )
    if conformance.get("strategies") != expected_strategies:
        raise PipelineValidationError("financial MVP strategy conformance differs")
    history = _object(contract.get("history_corrections"), "history corrections")
    correction_rows = _objects(history.get("corrections"), "history correction rows")
    expected_corrections = sorted(
        (
            {
                "correction_id": str(row.get("correction_id")),
                "evidence_layer": str(row.get("evidence_layer")),
                "readiness": str(row.get("readiness")),
            }
            for row in correction_rows
        ),
        key=lambda row: row["correction_id"],
    )
    if conformance.get("history_corrections") != expected_corrections:
        raise PipelineValidationError("financial MVP correction conformance differs")
    invested_rows = [
        row for row in outcome_rows if row.get("outcome_id") == "INVESTED_CAPITAL"
    ]
    if len(invested_rows) != 1 or not isinstance(
        invested_rows[0].get("calculation_contract"), dict
    ):
        raise PipelineValidationError("invested-capital policy authority differs")
    invested_sha256 = hashlib.sha256(
        canonical_json_bytes(invested_rows[0]["calculation_contract"])
    ).hexdigest()
    if conformance.get("invested_capital_policy_sha256") != invested_sha256:
        raise PipelineValidationError("invested-capital policy identity differs")

    generation = _absolute(
        authority["provider_generation_path"], "provider generation path"
    )
    if generation.is_symlink() or not generation.is_dir():
        raise PipelineValidationError("provider generation is unavailable")
    expected_files = {
        "admit-or-declare-receipt.json",
        "generation.json",
        "provider-ledger.json",
    }
    actual_files = {
        item.relative_to(generation).as_posix()
        for item in generation.rglob("*")
        if item.is_file()
    }
    if actual_files != expected_files:
        raise PipelineValidationError("provider generation file boundary differs")
    try:
        manifest_bytes = (generation / "generation.json").read_bytes()
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
        receipt_bytes = (generation / "admit-or-declare-receipt.json").read_bytes()
        receipt = _object(
            json.loads(receipt_bytes.decode("utf-8")),
            "provider admission receipt",
        )
        catalog_bytes = (generation / "provider-ledger.json").read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PipelineValidationError(
            "provider generation authority is invalid"
        ) from exc
    if (
        manifest.generation_id != generation.name
        or manifest.verification is not VerificationStatus.VERIFIED
        or manifest.adapter.name != "quant-engine-provider-ledger"
        or manifest.adapter.version != "5"
    ):
        raise PipelineValidationError("provider generation manifest differs")
    admission = _object(conformance.get("provider_admission"), "provider admission")
    _strict(
        admission,
        {
            "admission_gate_status",
            "catalog_sha256",
            "generation_id",
            "manifest_sha256",
            "product_readiness",
            "receipt_sha256",
        },
        "provider admission",
    )
    artifact_by_id = {item.artifact_id: item for item in manifest.artifacts}
    receipt_artifact = artifact_by_id.get("normalized/admit-or-declare-receipt.json")
    catalog_artifact = artifact_by_id.get("normalized/provider-ledger.json")
    if (
        receipt_artifact is None
        or catalog_artifact is None
        or receipt_artifact.sha256 != hashlib.sha256(receipt_bytes).hexdigest()
        or receipt_artifact.size_bytes != len(receipt_bytes)
        or catalog_artifact.sha256 != hashlib.sha256(catalog_bytes).hexdigest()
        or catalog_artifact.size_bytes != len(catalog_bytes)
        or admission.get("generation_id") != manifest.generation_id
        or admission.get("manifest_sha256")
        != hashlib.sha256(manifest_bytes).hexdigest()
        or admission.get("receipt_sha256") != hashlib.sha256(receipt_bytes).hexdigest()
        or admission.get("catalog_sha256") != hashlib.sha256(catalog_bytes).hexdigest()
        or admission.get("admission_gate_status")
        != receipt.get("admission_gate_status")
        or admission.get("product_readiness") != receipt.get("product_readiness")
        or receipt.get("contract_raw_sha256") != contract_raw_sha256
        or receipt.get("contract_semantic_sha256") != semantic_sha256
        or receipt.get("census_sha256") != census_sha256
        or receipt.get("product_id") != product.get("product_id")
        or receipt.get("product_version") != product.get("product_version")
    ):
        raise PipelineValidationError("provider admission authority differs")
    blockers = conformance.get("blocking_reasons")
    if not isinstance(blockers, list) or blockers != sorted(set(blockers)):
        raise PipelineValidationError("financial MVP blockers differ")
    expected_blockers = sorted(
        {
            *(
                f"SOURCE:{row['source_id']}"
                for row in expected_sources
                if row["product_requirement"] == "REQUIRED"
                and row["readiness"] != "READY"
            ),
            *(
                f"OUTCOME:{row['outcome_id']}"
                for row in expected_outcomes
                if row["product_requirement"] == "REQUIRED"
                and row["readiness"] != "READY"
            ),
            *(
                ("PROVIDER_ADMISSION",)
                if admission.get("product_readiness") != "READY_FOR_COMPOSITION"
                else ()
            ),
            "CORRECTION_AUTHORITY_PENDING_326",
        }
    )
    if (
        blockers != expected_blockers
        or conformance.get("correction_authority") != "REQUIRED_NOT_READY_PENDING_326"
        or conformance.get("rederive_authority")
        != "REFERENCE_ISSUE_322_NO_ACCEPTED_RECEIPT"
        or admission.get("admission_gate_status") != "PASS"
    ):
        raise PipelineValidationError("financial MVP blocker authority differs")
    expected_readiness = "READY_FOR_EXECUTION" if not blockers else "REQUIRED_NOT_READY"
    if conformance.get("readiness") != expected_readiness:
        raise PipelineValidationError("financial MVP readiness differs")
    return {"conformance": dict(conformance), "raw": dict(authority)}


def _single_gate(spec: PipelineSpec, gate_id: str) -> PipelineGate:
    matches = [item for item in spec.gates if item.gate_id == gate_id]
    if len(matches) != 1:
        raise PipelineValidationError(f"pipeline gate is ambiguous: {gate_id}")
    gate = matches[0]
    if _file_hash(gate.path)[0] != gate.sha256:
        raise PipelineValidationError(f"pipeline gate differs: {gate_id}")
    return gate


def _gate_json(spec: PipelineSpec, gate_id: str) -> tuple[dict[str, Any], bytes]:
    gate = _single_gate(spec, gate_id)
    try:
        content = gate.path.read_bytes()
        value = _object(json.loads(content.decode("utf-8")), gate_id)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineValidationError(
            f"pipeline gate is unreadable: {gate_id}"
        ) from exc
    return value, content


def _verified_epoch_manifest_orchestration(
    spec: PipelineSpec, raw: Mapping[str, Any]
) -> None:
    """Bind the contract's epoch-manifest identity to the bound contract's own.

    A schema-10 vertical recipe reuses one verified epoch-manifest generation
    rather than the five raw role files, so there is no epoch-manifest input
    gate to compare against. What the run does have is the bound financial-MVP
    contract, which carries the same `epoch_manifest_source` the release
    builder read out of that generation's bundle. Requiring the two to agree,
    and the contract's own digests to be digests of what it states, is what
    stops an orchestration contract naming one epoch manifest while the build
    consumes another.
    """

    generation = _object(
        raw["epoch_manifest_generation"], "portfolio epoch manifest generation"
    )
    _strict(
        generation,
        {"generation_id", "runtime_root", "source"},
        "portfolio epoch manifest generation",
    )
    source = _object(generation["source"], "portfolio epoch manifest source")
    declared = _hash(
        raw["epoch_manifest_source_sha256"], "portfolio epoch manifest source sha256"
    )
    if hashlib.sha256(canonical_json_bytes(source)).hexdigest() != declared:
        raise PipelineValidationError("portfolio epoch manifest source sha256 differs")
    bound_contract, bound_bytes = _gate_json(spec, _FINANCIAL_MVP_BOUND_CONTRACT_GATE)
    if (
        _hash(
            raw["financial_mvp_bound_contract_sha256"],
            "portfolio financial MVP bound contract sha256",
        )
        != hashlib.sha256(bound_bytes).hexdigest()
    ):
        raise PipelineValidationError(
            "portfolio financial MVP bound contract sha256 differs"
        )
    bound_source = bound_contract.get("epoch_manifest_source")
    if not isinstance(bound_source, dict) or canonical_json_bytes(
        bound_source
    ) != canonical_json_bytes(source):
        raise PipelineValidationError(
            "portfolio epoch manifest source differs from the bound contract"
        )


def _financial_epoch_manifest_authority(
    spec: PipelineSpec,
) -> dict[str, object] | None:
    matches = [
        gate for gate in spec.gates if gate.gate_id == _FINANCIAL_EPOCH_MANIFEST_GATE
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise PipelineValidationError("financial epoch manifest gate is ambiguous")
    payload, _ = _gate_json(spec, _FINANCIAL_EPOCH_MANIFEST_GATE)
    _strict(
        payload,
        {"inputs", "post_build_proof_sha256", "schema_version"},
        "financial epoch manifest gate",
    )
    inputs = _object(payload["inputs"], "financial epoch manifest inputs")
    _strict(
        inputs,
        _FINANCIAL_EPOCH_MANIFEST_INPUTS,
        "financial epoch manifest inputs",
    )
    for name in sorted(_FINANCIAL_EPOCH_MANIFEST_INPUTS):
        _file_definition_value(inputs[name], f"financial epoch manifest {name}")
    if payload["schema_version"] != 2:
        raise PipelineValidationError("financial epoch manifest gate is unsupported")
    return {
        "inputs": dict(inputs),
        "inputs_sha256": hashlib.sha256(canonical_json_bytes(inputs)).hexdigest(),
        "proof_sha256": _hash(
            payload["post_build_proof_sha256"],
            "financial epoch manifest proof sha256",
        ),
    }


def _provider_admission_validator() -> Callable[..., None]:
    """Load the exact repository #303 validator without trusting module cache state."""

    path = (
        Path(__file__).resolve().parents[5]
        / "packages"
        / "portfolio-ledger"
        / "src"
        / "quant_engine"
        / "portfolio_ledger"
        / "provider_admission.py"
    )
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("provider admission validator is unavailable")
    module_name = "quant_engine_core_pipeline_provider_admission_validator"
    previous = sys.modules.pop(module_name, None)
    try:
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None or module_spec.loader is None:
            raise PipelineValidationError("provider admission validator is unavailable")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        module_spec.loader.exec_module(module)
        validator = getattr(module, "validate_admit_or_declare_receipt", None)
        if not callable(validator):
            raise PipelineValidationError("provider admission validator is unavailable")
        return cast(Callable[..., None], validator)
    finally:
        sys.modules.pop(module_name, None)
        if previous is not None:
            sys.modules[module_name] = previous


def _verify_financial_outcomes(spec: PipelineSpec) -> dict[str, Any]:
    """Run the repository's single product completion verifier at this boundary."""
    tools_root = Path(__file__).resolve().parents[5] / "tools"
    path = tools_root / "portfolio_financial_outcomes.py"
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("financial outcome verifier is unavailable")
    module_spec = importlib.util.spec_from_file_location(
        "quant_engine_pipeline_financial_outcomes", path
    )
    if module_spec is None or module_spec.loader is None:
        raise PipelineValidationError("financial outcome verifier is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(tools_root))
        module_spec.loader.exec_module(module)
        proof = module.validate_financial_outcomes(spec)
        if not isinstance(proof, dict) or proof.get("status") != "PASS":
            raise PipelineValidationError("financial outcomes did not pass")
        return cast(dict[str, Any], proof)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PipelineValidationError(f"financial outcomes failed: {exc}") from exc
    finally:
        sys.path[:] = previous_path


def _governed_hwm_input_policy(
    current_policy_bytes: bytes,
    input_contract_bytes: bytes,
    expected_policy_sha256: str,
) -> bytes:
    """Retarget the repository HWM policy onto the exact admitted input contract."""

    tools_root = Path(__file__).resolve().parents[5] / "tools"
    path = tools_root / "portfolio_invested_capital.py"
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("governed HWM policy authority is unavailable")
    module_spec = importlib.util.spec_from_file_location(
        "quant_engine_pipeline_invested_capital_policy", path
    )
    if module_spec is None or module_spec.loader is None:
        raise PipelineValidationError("governed HWM policy authority is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(tools_root))
        module_spec.loader.exec_module(module)
        retargeted = module.governed_hwm_input_policy(
            current_policy_bytes, input_contract_bytes, expected_policy_sha256
        )
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        raise PipelineValidationError(
            "governed HWM policy differs from the admitted input authority"
        ) from exc
    finally:
        sys.path[:] = previous_path
    if not isinstance(retargeted, bytes):
        raise PipelineValidationError("governed HWM policy authority is invalid")
    return retargeted


def _verify_selected_hwm_sources(
    spec: PipelineSpec,
    *,
    contract_path: Path,
    bindings: Mapping[str, Path],
    input_contract_path: Path | None = None,
    policy_bytes: bytes | None = None,
) -> set[str]:
    """Replay the selected account-ledger HWM inputs through the product verifier."""

    tools_root = Path(__file__).resolve().parents[5] / "tools"
    path = tools_root / "portfolio_financial_outcomes.py"
    if path.is_symlink() or not path.is_file():
        raise PipelineValidationError("financial outcome verifier is unavailable")
    module_spec = importlib.util.spec_from_file_location(
        "quant_engine_pipeline_financial_outcomes_hwm", path
    )
    if module_spec is None or module_spec.loader is None:
        raise PipelineValidationError("financial outcome verifier is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(tools_root))
        module_spec.loader.exec_module(module)
        source_paths = module.selected_hwm_source_paths(spec)
        resolved = module.verify_selected_hwm_generation(
            source_paths,
            contract_path=contract_path,
            bindings=bindings,
            input_contract_path=input_contract_path,
            policy_bytes=policy_bytes,
        )
        if not isinstance(resolved, set) or any(
            not isinstance(source_id, str) for source_id in resolved
        ):
            raise PipelineValidationError(
                "financial HWM selected-source result is invalid"
            )
        return cast(set[str], resolved)
    except PipelineValidationError:
        raise
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        raise PipelineValidationError(
            f"financial HWM selected-source replay failed: {exc}"
        ) from exc
    finally:
        sys.path[:] = previous_path


def _semantic_route(output_role: object) -> dict[str, Any]:
    role = _identifier(str(output_role).lower().replace("_", "-"), "semantic output")
    canonical_role = role.upper().replace("-", "_")
    first_stage = _SEMANTIC_FIRST_STAGE.get(canonical_role)
    if first_stage is None:
        raise PipelineValidationError(
            f"semantic output has no rebuild route: {canonical_role}"
        )
    affected = {first_stage}
    changed = True
    while changed:
        changed = False
        for stage in _PORTFOLIO_STAGE_ORDER:
            if stage in affected:
                continue
            if any(
                dependency in affected
                for dependency in _PORTFOLIO_STAGE_DEPENDENCIES[stage]
            ):
                affected.add(stage)
                changed = True
    return {
        "affected_stages": [
            stage for stage in _PORTFOLIO_STAGE_ORDER if stage in affected
        ],
        "first_stage": first_stage,
        "output_role": canonical_role,
    }


def _registry_boundary(
    spec: PipelineSpec,
    boundary: Mapping[str, Any],
    *,
    gate_id: str,
    schema_name: str,
    record_key: str,
    contract_semantic_sha256: str,
    census_sha256: str,
) -> list[dict[str, Any]]:
    state = boundary.get("authority_state")
    matches = [item for item in spec.gates if item.gate_id == gate_id]
    if state == "REQUIRED_NOT_READY_NOT_PROVIDED":
        if matches:
            raise PipelineValidationError(f"unexpected authority gate: {gate_id}")
        if (
            boundary.get("records") != []
            or boundary.get("registry_sha256") is not None
            or boundary.get("schema_name") != schema_name
            or boundary.get("schema_version") != 1
        ):
            raise PipelineValidationError(f"{schema_name} absence differs")
        return []
    if state != "PROVIDED" or len(matches) != 1:
        raise PipelineValidationError(f"{schema_name} authority differs")
    payload, _ = _gate_json(spec, gate_id)
    expected_keys = {
        "census_sha256",
        "contract_semantic_sha256",
        record_key,
        "registry_sha256",
        "schema_name",
        "schema_version",
    }
    _strict(payload, expected_keys, schema_name)
    declared_digest = _hash(
        payload["registry_sha256"], f"{schema_name}.registry_sha256"
    )
    digest_payload = dict(payload)
    digest_payload.pop("registry_sha256")
    if (
        hashlib.sha256(canonical_json_bytes(digest_payload)).hexdigest()
        != declared_digest
        or payload.get("schema_name") != schema_name
        or payload.get("schema_version") != 1
        or payload.get("contract_semantic_sha256") != contract_semantic_sha256
        or payload.get("census_sha256") != census_sha256
        or boundary.get("contract_semantic_sha256") != contract_semantic_sha256
        or boundary.get("census_sha256") != census_sha256
        or boundary.get("registry_sha256") != declared_digest
        or boundary.get("records") != payload.get(record_key)
    ):
        raise PipelineValidationError(f"{schema_name} identity differs")
    return _objects(payload.get(record_key), f"{schema_name} records")


def _financial_mvp_candidate_generations(spec: PipelineSpec) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for role, root_ids in _FINANCIAL_MVP_CANDIDATE_ROOT_IDS.items():
        matches = [
            spec.release_generations[root_id]
            for root_id in root_ids
            if root_id in spec.release_generations
        ]
        if len(matches) != 1:
            raise PipelineValidationError(
                f"financial MVP {role} candidate generation is ambiguous"
            )
        candidates[role] = matches[0]
    return candidates


def _validate_financial_mvp_production_binding(
    spec: PipelineSpec,
    *,
    product_gate_sha256: str,
    provider_generation_id: str,
) -> None:
    matches = [
        item for item in spec.gates if item.gate_id == _PORTFOLIO_PRODUCTION_REBIND_GATE
    ]
    if not matches:
        return
    contract, _ = _gate_json(spec, _PORTFOLIO_PRODUCTION_REBIND_GATE)
    contract_schema_version = contract.get("schema_version")
    expected_keys = {
        "code_commit",
        "generation_ids",
        "orchestration_contract_sha256",
        "pipeline_id",
        "prepared_release_commit",
        "production_roots",
        "production_semantic_gate_sha256",
        "product_authority_sha256",
        "schema_version",
        "semantic_gate_sha256",
        "source_generations",
    }
    if contract_schema_version == 3:
        expected_keys.update(
            {
                "financial_epoch_manifest_inputs_sha256",
                "financial_epoch_manifest_proof_sha256",
            }
        )
    _strict(contract, expected_keys, "portfolio production rebind contract")
    generation_ids = _object(
        contract["generation_ids"], "production generation identities"
    )
    production_roots = _object(contract["production_roots"], "production roots")
    source_generations = _object(
        contract["source_generations"], "production source generations"
    )
    product_gate = _single_gate(spec, _FINANCIAL_MVP_PRODUCT_GATE)
    semantic_gate = _single_gate(spec, _POST_BUILD_SEMANTIC_GATE)
    epoch_authority = _financial_epoch_manifest_authority(spec)
    expected_contract_schema_version = 3 if epoch_authority is not None else 2
    epoch_identity_differs = False
    if contract_schema_version == 3:
        epoch_identity_differs = epoch_authority is None or (
            _hash(
                contract["financial_epoch_manifest_inputs_sha256"],
                "production financial epoch manifest inputs sha256",
            )
            != epoch_authority["inputs_sha256"]
            or _hash(
                contract["financial_epoch_manifest_proof_sha256"],
                "production financial epoch manifest proof sha256",
            )
            != epoch_authority["proof_sha256"]
        )
    release_sources = {
        node.root_id: node.source_dir
        for node in spec.nodes
        if isinstance(node, GenerationNode)
        and node.release_target
        and node.source_dir is not None
    }
    if (
        contract_schema_version != expected_contract_schema_version
        or epoch_identity_differs
        or contract["code_commit"] != spec.code_commit
        or contract["prepared_release_commit"] != spec.code_commit
        or contract["pipeline_id"] + "-production" != spec.pipeline_id
        or generation_ids != spec.release_generations
        or production_roots
        != {root_id: str(path) for root_id, path in spec.roots.items()}
        or set(source_generations) != set(spec.release_generations)
        or set(release_sources) != set(spec.release_generations)
        or contract["product_authority_sha256"] != product_gate_sha256
        or product_gate.sha256 != product_gate_sha256
        or contract["production_semantic_gate_sha256"] != semantic_gate.sha256
        or spec.release_generations.get("provider-ledger") != provider_generation_id
    ):
        raise PipelineValidationError("financial MVP production binding differs")
    for root_id, generation_id in spec.release_generations.items():
        source = _absolute(source_generations[root_id], f"production source {root_id}")
        if (
            source.name != generation_id
            or release_sources[root_id] is None
            or not _same_path(source, release_sources[root_id])
        ):
            raise PipelineValidationError(
                f"financial MVP production source differs: {root_id}"
            )


def _expected_hwm_input_authority(
    conformance: Mapping[str, Any],
    authority_hashes: Mapping[str, Any],
    assessment: Mapping[str, Any],
    spec: PipelineSpec,
) -> dict[str, Any]:
    """Independently rederive and verify the staged PORT-HWM-4 assessment."""

    sources = _objects(conformance.get("sources"), "financial MVP HWM sources")
    outcomes = _objects(conformance.get("outcomes"), "financial MVP HWM outcomes")
    strategies = _objects(conformance.get("strategies"), "financial MVP HWM strategies")
    hwm_outcomes = [
        row for row in outcomes if row.get("outcome_id") == "AS_HELD_COMPOSITE_HWM"
    ]
    if len(hwm_outcomes) > 1:
        raise PipelineValidationError("financial MVP HWM outcome is ambiguous")
    unresolved_source_ids = sorted(
        str(row["source_id"])
        for row in sources
        if isinstance(row.get("consumers"), list)
        and "AS_HELD_COMPOSITE_HWM" in row["consumers"]
        and isinstance(row.get("current_generation"), Mapping)
        and (
            row["current_generation"].get("generation_id") is None
            or row["current_generation"].get("sha256") is None
        )
    )
    required_strategy_ids = sorted(
        str(row["strategy_id"])
        for row in strategies
        if row.get("product_requirement") == "REQUIRED"
    )
    outcome_readiness = (
        str(hwm_outcomes[0].get("readiness")) if hwm_outcomes else "NOT_DECLARED"
    )
    legacy_blockers = {
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
        *({"OUTCOME:AS_HELD_COMPOSITE_HWM"} if outcome_readiness != "READY" else set()),
        *(f"SOURCE:{source_id}" for source_id in unresolved_source_ids),
    }
    provider_admission = _object(
        conformance.get("provider_admission"), "financial MVP HWM provider authority"
    )
    if provider_admission.get("product_readiness") != "READY_FOR_COMPOSITION":
        legacy_blockers.add("PROVIDER_ADMISSION")
    expected_authority_hashes = {
        "census_sha256": conformance["census_sha256"],
        "contract_raw_sha256": authority_hashes["contract_raw_sha256"],
        "contract_semantic_sha256": conformance["contract_semantic_sha256"],
        "provider_catalog_sha256": authority_hashes["provider_catalog_sha256"],
        "provider_manifest_sha256": authority_hashes["provider_manifest_sha256"],
        "provider_receipt_sha256": authority_hashes["provider_receipt_sha256"],
        "source_matrix_sha256": authority_hashes["source_matrix_sha256"],
    }
    legacy = {
        "assessment_scope": "CURRENT_PRODUCT_AND_PROVIDER_ADMISSION_ONLY",
        "authority_hashes": expected_authority_hashes,
        "blocking_reasons": sorted(legacy_blockers),
        "calendar_authority_sha256": None,
        "input_generation_sha256": None,
        "outcome_id": "AS_HELD_COMPOSITE_HWM",
        "outcome_readiness": outcome_readiness,
        "required_strategy_ids": required_strategy_ids,
        "schema_version": 1,
        "state": "REQUIRED_NOT_READY",
        "unresolved_source_ids": unresolved_source_ids,
    }
    if assessment.get("schema_version") == 1:
        return legacy
    if assessment.get("schema_version") != 2:
        raise PipelineValidationError("financial MVP HWM assessment is unsupported")
    _strict(
        assessment,
        {
            "assessment_scope",
            "authority_hashes",
            "bindings",
            "blocking_reasons",
            "calendar_authority_sha256",
            "input_generation_sha256",
            "outcome_id",
            "outcome_readiness",
            "required_strategy_ids",
            "result_sha256",
            "schema_version",
            "state",
            "unresolved_source_ids",
        },
        "financial MVP HWM assessment",
    )
    pending_blockers = {
        "ACCEPTED_HWM_INPUT_GENERATION_ABSENT",
        "ACCEPTED_MIXED_CALENDAR_AUTHORITY_ABSENT",
        *(f"SOURCE:{source_id}" for source_id in unresolved_source_ids),
    }
    if provider_admission.get("product_readiness") != "READY_FOR_COMPOSITION":
        pending_blockers.add("PROVIDER_ADMISSION")
    pending = {
        "assessment_scope": "PENDING_GOVERNED_HWM_RESULT",
        "authority_hashes": expected_authority_hashes,
        "bindings": {},
        "blocking_reasons": sorted(pending_blockers),
        "calendar_authority_sha256": None,
        "input_generation_sha256": None,
        "outcome_id": "AS_HELD_COMPOSITE_HWM",
        "outcome_readiness": outcome_readiness,
        "required_strategy_ids": required_strategy_ids,
        "result_sha256": None,
        "schema_version": 2,
        "state": "REQUIRED_NOT_READY",
        "unresolved_source_ids": unresolved_source_ids,
    }
    if assessment.get("state") == "REQUIRED_NOT_READY":
        return pending
    if assessment.get("state") != "AVAILABLE":
        raise PipelineValidationError("financial MVP HWM assessment state differs")
    bindings = _object(assessment["bindings"], "financial MVP HWM bindings")
    if set(bindings) != _HWM_BINDING_KEYS:
        raise PipelineValidationError("financial MVP HWM binding roles differ")
    # A provider ledger ADMITTED under a retained input contract carries that
    # contract here.  The replay below must use the authority the observations
    # were built under, not the later product contract this spec also gates.
    admitted_contract = assessment.get("input_contract")
    admitted_contract_path = (
        None
        if admitted_contract is None
        else _file_definition_value(
            admitted_contract, "financial MVP HWM input contract"
        )[0]
    )
    resolved: dict[str, tuple[Path, str, int]] = {
        key: _file_definition_value(bindings[key], f"financial MVP HWM {key}")
        for key in sorted(_HWM_BINDING_KEYS)
    }
    assessment_gate = _single_gate(spec, _FINANCIAL_MVP_HWM_INPUT_AUTHORITY_GATE)
    if any(
        _same_path(path, assessment_gate.path)
        for path, _digest, _size in resolved.values()
    ):
        raise PipelineValidationError("financial MVP HWM assessment self-reference")
    provider_gate = _single_gate(
        spec, _FINANCIAL_MVP_AUTHORITY_GATES["provider_receipt"]
    )
    provider_identity = _file_hash(provider_gate.path)
    provider_path, provider_sha256, provider_size = resolved[
        "provider_admission_receipt"
    ]
    if (
        not _same_path(provider_path, provider_gate.path)
        or (provider_sha256, provider_size) != provider_identity
    ):
        raise PipelineValidationError("financial MVP HWM admission receipt differs")
    for field, role in (
        ("calendar_authority_sha256", "calendar_authority"),
        ("input_generation_sha256", "input_generation"),
        ("result_sha256", "governed_result"),
    ):
        if assessment[field] != resolved[role][1]:
            raise PipelineValidationError(f"financial MVP HWM {field} differs")
    result_path = resolved["governed_result"][0]
    input_path = resolved["input_generation"][0]
    calendar_path = resolved["calendar_authority"][0]
    contract_gate = _single_gate(spec, _FINANCIAL_MVP_AUTHORITY_GATES["contract"])
    repository_root = Path(__file__).resolve().parents[5]
    try:
        result = _object(
            json.loads(result_path.read_text(encoding="utf-8")),
            "governed HWM result",
        )
        from quant_engine.portfolio_ledger.hwm_governed_execution import (
            verify_governed_hwm_result,
        )

        input_contract_bytes = (
            contract_gate.path
            if admitted_contract_path is None
            else admitted_contract_path
        ).read_bytes()
        governed_policy_bytes = _governed_hwm_input_policy(
            (
                repository_root / "contracts" / "portfolio-hwm-policy-v4.json"
            ).read_bytes(),
            input_contract_bytes,
            _hash(result.get("policy_sha256"), "governed HWM policy sha256"),
        )
        verified = verify_governed_hwm_result(
            result,
            input_path.read_bytes(),
            calendar_bytes=calendar_path.read_bytes(),
            input_receipt_bytes=provider_path.read_bytes(),
            policy_bytes=governed_policy_bytes,
            product_contract_bytes=input_contract_bytes,
            performance_policy_bytes=(
                repository_root / "contracts" / "portfolio-performance-policy-v1.json"
            ).read_bytes(),
            legacy_policy_bytes=(
                repository_root / "contracts" / "portfolio-hwm-policy-v3.json"
            ).read_bytes(),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise PipelineValidationError(
            "financial MVP governed HWM result verification failed"
        ) from exc
    memberships = _object(
        verified.get("input_evidence"), "governed HWM input evidence"
    ).get("memberships")
    verified_strategy_ids = sorted(
        str(row["strategy_id"])
        for row in _objects(memberships, "governed HWM memberships")
    )
    if verified.get("status") != "AVAILABLE" or verified_strategy_ids != (
        required_strategy_ids
    ):
        raise PipelineValidationError("financial MVP governed HWM coverage differs")
    resolved_source_ids = _verify_selected_hwm_sources(
        spec,
        contract_path=contract_gate.path,
        bindings={key: value[0] for key, value in resolved.items()},
        input_contract_path=admitted_contract_path,
        policy_bytes=governed_policy_bytes,
    )
    remaining_source_ids = sorted(set(unresolved_source_ids) - set(resolved_source_ids))
    if remaining_source_ids or provider_admission.get("product_readiness") != (
        "READY_FOR_COMPOSITION"
    ):
        raise PipelineValidationError("financial MVP HWM required authority is absent")
    return {
        **pending,
        "assessment_scope": "VERIFIED_GOVERNED_HWM_RESULT",
        "bindings": dict(bindings),
        "blocking_reasons": [],
        "calendar_authority_sha256": resolved["calendar_authority"][1],
        "input_generation_sha256": resolved["input_generation"][1],
        "result_sha256": resolved["governed_result"][1],
        "state": "AVAILABLE",
        "unresolved_source_ids": remaining_source_ids,
        **({} if admitted_contract is None else {"input_contract": admitted_contract}),
    }


def _financial_mvp_product_authority(spec: PipelineSpec) -> dict[str, Any] | None:
    product_matches = [
        item for item in spec.gates if item.gate_id == _FINANCIAL_MVP_PRODUCT_GATE
    ]
    if not product_matches:
        if any(
            item.gate_id
            in {
                *_FINANCIAL_MVP_AUTHORITY_GATES.values(),
                _FINANCIAL_MVP_HWM_INPUT_AUTHORITY_GATE,
            }
            for item in spec.gates
        ):
            raise PipelineValidationError("financial MVP authority gates are detached")
        return None
    if len(product_matches) != 1:
        raise PipelineValidationError("financial MVP product gate is ambiguous")
    gate_payload, product_gate_bytes = _gate_json(spec, _FINANCIAL_MVP_PRODUCT_GATE)
    product_schema_version = gate_payload.get("schema_version")
    hwm_gate_matches = [
        item
        for item in spec.gates
        if item.gate_id == _FINANCIAL_MVP_HWM_INPUT_AUTHORITY_GATE
    ]
    if product_schema_version != 3 and hwm_gate_matches:
        raise PipelineValidationError(
            "financial MVP HWM input authority requires product schema 3"
        )
    if product_schema_version not in {2, 3}:
        return _financial_mvp_product_authority_legacy(spec)
    _strict(
        gate_payload,
        {"authority_hashes", "conformance", "schema_version"},
        "financial MVP product gate",
    )
    authority_hashes = _object(
        gate_payload["authority_hashes"], "financial MVP authority hashes"
    )
    _strict(
        authority_hashes,
        {
            "contract_raw_sha256",
            "provider_catalog_sha256",
            "provider_manifest_sha256",
            "provider_receipt_sha256",
            "source_matrix_sha256",
        },
        "financial MVP authority hashes",
    )
    payloads: dict[str, tuple[dict[str, Any], bytes]] = {}
    for key, gate_id in _FINANCIAL_MVP_AUTHORITY_GATES.items():
        payloads[key] = _gate_json(spec, gate_id)
    contract, contract_bytes = payloads["contract"]
    matrix, matrix_bytes = payloads["source_matrix"]
    manifest_raw, manifest_bytes = payloads["provider_manifest"]
    catalog, catalog_bytes = payloads["provider_catalog"]
    receipt, receipt_bytes = payloads["provider_receipt"]
    expected_hashes = {
        "contract_raw_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "provider_catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "provider_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "provider_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "source_matrix_sha256": hashlib.sha256(matrix_bytes).hexdigest(),
    }
    if authority_hashes != expected_hashes:
        raise PipelineValidationError("financial MVP authority file identities differ")
    try:
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
    except ValueError as exc:
        raise PipelineValidationError("provider admission manifest is invalid") from exc
    if (
        manifest.verification is not VerificationStatus.VERIFIED
        or manifest.adapter.name != "quant-engine-provider-ledger"
        or manifest.adapter.version != "5"
        or catalog.get("catalog_schema_version") != 2
    ):
        raise PipelineValidationError("provider admission manifest authority differs")
    artifact_by_id = {item.artifact_id: item for item in manifest.artifacts}
    receipt_artifact = artifact_by_id.get("normalized/admit-or-declare-receipt.json")
    catalog_artifact = artifact_by_id.get("normalized/provider-ledger.json")
    if (
        receipt_artifact is None
        or catalog_artifact is None
        or receipt_artifact.sha256 != expected_hashes["provider_receipt_sha256"]
        or receipt_artifact.size_bytes != len(receipt_bytes)
        or catalog_artifact.sha256 != expected_hashes["provider_catalog_sha256"]
        or catalog_artifact.size_bytes != len(catalog_bytes)
    ):
        raise PipelineValidationError(
            "provider admission artifacts are not manifest-bound"
        )

    conformance = _object(gate_payload["conformance"], "financial MVP conformance")
    expected_conformance_keys = {
        "blocking_reasons",
        "census_memberships",
        "census_sha256",
        "contract_raw_sha256",
        "contract_semantic_sha256",
        "correction_authority",
        "correction_invalidation_routes",
        "history_corrections",
        "invested_capital_policy_sha256",
        "outcomes",
        "product",
        "product_id",
        "product_version",
        "provider_admission",
        "readiness",
        "rederive_authority",
        "schema_version",
        "source_invalidation_routes",
        "source_matrix_sha256",
        "sources",
        "stage_identities",
        "stage_order",
        "strategies",
    }
    _strict(conformance, expected_conformance_keys, "financial MVP conformance")
    semantic_sha256 = hashlib.sha256(canonical_json_bytes(contract)).hexdigest()
    census = _object(contract.get("current_census"), "financial MVP current census")
    census_sha256 = hashlib.sha256(canonical_json_bytes(census)).hexdigest()
    product = _object(contract.get("product"), "financial MVP product")
    if (
        conformance.get("schema_version") != 2
        or conformance.get("contract_raw_sha256")
        != expected_hashes["contract_raw_sha256"]
        or conformance.get("contract_semantic_sha256") != semantic_sha256
        or conformance.get("census_sha256") != census_sha256
        or conformance.get("source_matrix_sha256")
        != expected_hashes["source_matrix_sha256"]
        or conformance.get("product") != product
        or conformance.get("product_id") != product.get("product_id")
        or conformance.get("product_version") != product.get("product_version")
        or matrix.get("contract_sha256") != semantic_sha256
        or matrix.get("census_sha256") != census_sha256
        or matrix.get("product_version") != product.get("product_version")
    ):
        raise PipelineValidationError("financial MVP authority identities differ")

    source_rows = sorted(
        _objects(contract.get("source_segments"), "source segments"),
        key=lambda row: str(row.get("source_id")),
    )
    required_source_fields = {
        "absence_behavior",
        "acquisition",
        "admission",
        "authority",
        "blockers",
        "capture_status",
        "consumers",
        "current_generation",
        "data_classes",
        "endpoint_or_artifact_selector",
        "evidence_layer",
        "existence_status",
        "freshness",
        "interval",
        "invalidated_outputs",
        "owner_policy_decision",
        "product_requirement",
        "readiness",
        "schema",
        "source_id",
        "source_version",
        "strategy_version",
        "subjects",
        "temporal",
        "units",
    }
    source_ids: set[str] = set()
    for row in source_rows:
        if not required_source_fields <= set(row):
            raise PipelineValidationError(
                "financial MVP source authority is incomplete"
            )
        source_id = _identifier(row["source_id"], "financial MVP source_id")
        if source_id in source_ids:
            raise PipelineValidationError("financial MVP source IDs are not unique")
        source_ids.add(source_id)
        for field in (
            "acquisition",
            "admission",
            "current_generation",
            "freshness",
            "interval",
            "schema",
            "subjects",
            "temporal",
            "units",
        ):
            _object(row[field], f"financial MVP source {source_id}.{field}")
        for field in (
            "blockers",
            "consumers",
            "data_classes",
            "invalidated_outputs",
        ):
            if not isinstance(row[field], list):
                raise PipelineValidationError(
                    f"financial MVP source {source_id}.{field} must be an array"
                )
    strategy_rows = sorted(
        _objects(contract.get("strategies"), "financial MVP strategies"),
        key=lambda row: str(row.get("strategy_id")),
    )
    outcome_rows = sorted(
        _objects(contract.get("financial_outcomes"), "financial outcomes"),
        key=lambda row: str(row.get("outcome_id")),
    )
    history = _object(contract.get("history_corrections"), "history corrections")
    correction_rows = sorted(
        _objects(history.get("corrections"), "history correction rows"),
        key=lambda row: str(row.get("correction_id")),
    )
    if (
        conformance.get("sources") != source_rows
        or conformance.get("strategies") != strategy_rows
        or conformance.get("outcomes") != outcome_rows
        or conformance.get("history_corrections") != correction_rows
    ):
        raise PipelineValidationError("financial MVP full conformance differs")

    sleeve_rows = _objects(census.get("account_sleeves"), "census account sleeves")
    expected_memberships = []
    for sleeve in sleeve_rows:
        assignments = _objects(sleeve.get("assignments"), "census assignments")
        expected_memberships.append(
            {
                "assignments": sorted(
                    (
                        {
                            "effective_end": row.get("effective_end"),
                            "effective_start": row.get("effective_start"),
                            "status": row.get("status"),
                            "strategy_id": row.get("strategy_id"),
                            "transition_group_id": row.get("transition_group_id"),
                        }
                        for row in assignments
                    ),
                    key=lambda row: (
                        str(row["effective_start"]),
                        str(row["strategy_id"]),
                        str(row["effective_end"]),
                    ),
                ),
                "current_strategy_id": sleeve.get("current_strategy_id"),
                "provider_id": sleeve.get("provider_id"),
                "role": sleeve.get("role"),
                "sleeve_alias": sleeve.get("sleeve_alias"),
                "source_segment_ids": sorted(
                    _strings(sleeve.get("source_segment_ids"), "sleeve sources")
                ),
            }
        )
    expected_memberships.sort(key=lambda row: str(row["sleeve_alias"]))
    if conformance.get("census_memberships") != expected_memberships:
        raise PipelineValidationError("financial MVP census conformance differs")

    admission = _object(conformance.get("provider_admission"), "provider admission")
    expected_admission = {
        "admission_gate_status": receipt.get("admission_gate_status"),
        "catalog_sha256": expected_hashes["provider_catalog_sha256"],
        "disposition_count": len(_objects(receipt.get("dispositions"), "dispositions")),
        "generation_id": manifest.generation_id,
        "inventory_count": len(_objects(receipt.get("inventory"), "inventory")),
        "manifest_sha256": expected_hashes["provider_manifest_sha256"],
        "product_readiness": receipt.get("product_readiness"),
        "provider_count": len(
            _objects(receipt.get("provider_generations"), "provider generations")
        ),
        "receipt": receipt,
        "receipt_sha256": expected_hashes["provider_receipt_sha256"],
    }
    if admission != expected_admission:
        raise PipelineValidationError("provider admission conformance differs")
    provider_rows = _objects(
        receipt.get("provider_generations"), "provider generations"
    )
    provider_generations = {str(row.get("provider_id")): row for row in provider_rows}
    source_segments = {str(row.get("source_id")): row for row in source_rows}
    if set(provider_generations) != {
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    }:
        raise PipelineValidationError("provider admission family set differs")
    try:
        _provider_admission_validator()(
            receipt,
            product_authority={
                "census_id": census.get("census_id"),
                "census_sha256": census_sha256,
                "contract_raw_sha256": expected_hashes["contract_raw_sha256"],
                "contract_semantic_sha256": semantic_sha256,
                "product_id": product.get("product_id"),
                "product_version": product.get("product_version"),
            },
            source_segments=source_segments,
            provider_generations=provider_generations,
        )
    except (RuntimeError, ValueError) as exc:
        raise PipelineValidationError(
            f"provider admission receipt is invalid: {exc}"
        ) from exc
    for row in provider_rows:
        provider_id = str(row.get("provider_id"))
        prefix = f"providers/{provider_id}/"
        hashes = {
            item.sha256
            for item in manifest.artifacts
            if item.artifact_id.startswith(prefix)
        }
        if (
            row.get("manifest_sha256") not in hashes
            or row.get("catalog_sha256") not in hashes
        ):
            raise PipelineValidationError(
                f"provider admission generation is not manifest-bound: {provider_id}"
            )

    correction_boundary = _object(
        conformance.get("correction_authority"), "correction authority"
    )
    rederive_boundary = _object(
        conformance.get("rederive_authority"), "re-derivation authority"
    )
    runtime_corrections = _registry_boundary(
        spec,
        correction_boundary,
        gate_id=_CORRECTION_REGISTRY_GATE,
        schema_name="portfolio-correction-registry",
        record_key="corrections",
        contract_semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
    )
    runtime_rederivations = _registry_boundary(
        spec,
        rederive_boundary,
        gate_id=_REDERIVE_REGISTRY_GATE,
        schema_name="portfolio-rederive-receipt-registry",
        record_key="receipts",
        contract_semantic_sha256=semantic_sha256,
        census_sha256=census_sha256,
    )

    source_routes = []
    for row in source_rows:
        outputs = sorted(
            _strings(row.get("invalidated_outputs"), "invalidated outputs")
        )
        rebuild_paths = [_semantic_route(output) for output in outputs]
        source_routes.append(
            {
                "affected_stages": [
                    stage
                    for stage in _PORTFOLIO_STAGE_ORDER
                    if any(stage in route["affected_stages"] for route in rebuild_paths)
                ],
                "invalidated_outputs": list(outputs),
                "rebuild_paths": rebuild_paths,
                "source_id": row.get("source_id"),
            }
        )
    correction_routes = []
    for row in runtime_corrections:
        outputs = sorted(
            _strings(row.get("invalidated_outputs"), "correction invalidated outputs")
        )
        rebuild_paths = [_semantic_route(output) for output in outputs]
        correction_routes.append(
            {
                "affected_stages": [
                    stage
                    for stage in _PORTFOLIO_STAGE_ORDER
                    if any(stage in route["affected_stages"] for route in rebuild_paths)
                ],
                "correction_id": row.get("correction_id"),
                "invalidated_outputs": list(outputs),
                "rebuild_paths": rebuild_paths,
            }
        )
    source_routes.sort(key=lambda row: str(row["source_id"]))
    correction_routes.sort(key=lambda row: str(row["correction_id"]))
    if (
        conformance.get("source_invalidation_routes") != source_routes
        or conformance.get("correction_invalidation_routes") != correction_routes
        or conformance.get("stage_order") != list(_PORTFOLIO_STAGE_ORDER)
    ):
        raise PipelineValidationError("financial MVP invalidation routes differ")

    upstream_identity: dict[str, str] = {}
    stage_identities = []
    source_affected = {
        str(route["source_id"]): cast(list[str], route["affected_stages"])
        for route in source_routes
    }
    correction_affected = {
        str(route["correction_id"]): cast(list[str], route["affected_stages"])
        for route in correction_routes
    }
    for stage in _PORTFOLIO_STAGE_ORDER:
        semantic_sources = [
            row
            for row in source_rows
            if stage in source_affected[str(row["source_id"])]
        ]
        semantic_corrections = [
            row
            for row in runtime_corrections
            if stage in correction_affected[str(row["correction_id"])]
        ]
        stage_payload = {
            "corrections": semantic_corrections,
            "outcomes": [
                row
                for row in outcome_rows
                if stage in _semantic_route(row["outcome_id"])["affected_stages"]
            ],
            "provider_admission_receipt_sha256": expected_hashes[
                "provider_receipt_sha256"
            ],
            "sources": semantic_sources,
            "stage": stage,
            "upstream_stage_identities": {
                dependency: upstream_identity[dependency]
                for dependency in _PORTFOLIO_STAGE_DEPENDENCIES[stage]
            },
        }
        identity = hashlib.sha256(canonical_json_bytes(stage_payload)).hexdigest()
        upstream_identity[stage] = identity
        stage_identities.append(
            {
                "affected_by_corrections": sorted(
                    str(row["correction_id"]) for row in semantic_corrections
                ),
                "semantic_input_sha256": identity,
                "source_ids": sorted(str(row["source_id"]) for row in semantic_sources),
                "stage": stage,
                "upstream_stages": list(_PORTFOLIO_STAGE_DEPENDENCIES[stage]),
            }
        )
    if conformance.get("stage_identities") != stage_identities:
        raise PipelineValidationError("financial MVP stage identities differ")

    applicable_open = sorted(
        str(row["correction_id"])
        for row in runtime_corrections
        if row.get("status") == "OPEN" and row.get("applicability") == "APPLICABLE"
    )
    resolved_applicable = {
        str(row["correction_id"]): set(
            _strings(row.get("invalidated_outputs"), "correction outputs")
        )
        for row in runtime_corrections
        if row.get("status") in {"RESOLVED", "DISPOSITIONED"}
        and row.get("applicability") == "APPLICABLE"
    }
    candidate_generations = (
        _financial_mvp_candidate_generations(spec) if runtime_rederivations else {}
    )
    rederived: dict[str, set[str]] = {}
    for row in runtime_rederivations:
        _strict(
            row,
            {
                "correction_id",
                "input_generation_id",
                "invalidated_outputs",
                "output_generation_id",
                "receipt_sha256",
                "status",
            },
            "re-derivation receipt",
        )
        correction_id = _identifier(row["correction_id"], "re-derivation correction_id")
        outputs = list(_strings(row["invalidated_outputs"], "re-derivation outputs"))
        if (
            correction_id in rederived
            or row["status"] != "VERIFIED"
            or outputs != sorted(set(outputs))
        ):
            raise PipelineValidationError(
                f"re-derivation receipt state differs: {correction_id}"
            )
        affected_stages = {
            stage
            for output in outputs
            for stage in _semantic_route(output)["affected_stages"]
        }
        output_role = next(
            (
                role
                for role in _FINANCIAL_MVP_CANDIDATE_ROOT_IDS
                if _FINANCIAL_MVP_GENERATION_STAGE[role] in affected_stages
            ),
            None,
        )
        receipt_payload = {
            "correction_id": correction_id,
            "input_generation_id": row["input_generation_id"],
            "invalidated_outputs": outputs,
            "output_generation_id": row["output_generation_id"],
            "status": row["status"],
        }
        if (
            output_role is None
            or row["input_generation_id"] != candidate_generations["ledger"]
            or row["output_generation_id"] != candidate_generations[output_role]
            or row["receipt_sha256"]
            != hashlib.sha256(canonical_json_bytes(receipt_payload)).hexdigest()
        ):
            raise PipelineValidationError(
                f"re-derivation receipt candidate binding differs: {correction_id}"
            )
        rederived[correction_id] = set(outputs)
    missing_rederivations = sorted(
        correction_id
        for correction_id, outputs in resolved_applicable.items()
        if rederived.get(correction_id) != outputs
    )
    if (
        correction_boundary.get("applicable_open_correction_ids") != applicable_open
        or rederive_boundary.get("missing_correction_ids") != missing_rederivations
    ):
        raise PipelineValidationError("correction readiness projection differs")
    expected_blockers = sorted(
        {
            *(
                f"SOURCE:{row['source_id']}"
                for row in source_rows
                if row.get("product_requirement") == "REQUIRED"
                and row.get("readiness") != "READY"
            ),
            *(
                f"OUTCOME:{row['outcome_id']}"
                for row in outcome_rows
                if row.get("product_requirement") == "REQUIRED"
                and row.get("readiness") != "READY"
            ),
            *(
                ("PROVIDER_ADMISSION",)
                if receipt.get("product_readiness") != "READY_FOR_COMPOSITION"
                else ()
            ),
            *(
                ("CORRECTION_AUTHORITY_REQUIRED",)
                if correction_boundary.get("authority_state") != "PROVIDED"
                else ()
            ),
            *(f"OPEN_CORRECTION:{value}" for value in applicable_open),
            *(
                ("REDERIVE_AUTHORITY_REQUIRED",)
                if resolved_applicable
                and rederive_boundary.get("authority_state") != "PROVIDED"
                else ()
            ),
            *(f"MISSING_REDERIVATION:{value}" for value in missing_rederivations),
        }
    )
    if (
        conformance.get("blocking_reasons") != expected_blockers
        or conformance.get("readiness")
        != ("READY_FOR_EXECUTION" if not expected_blockers else "REQUIRED_NOT_READY")
        or receipt.get("admission_gate_status") != "PASS"
    ):
        raise PipelineValidationError("financial MVP readiness differs")
    hwm_input_authority = None
    if product_schema_version == 3:
        hwm_input_authority, _ = _gate_json(
            spec, _FINANCIAL_MVP_HWM_INPUT_AUTHORITY_GATE
        )
        expected_hwm_input_authority = _expected_hwm_input_authority(
            conformance, expected_hashes, hwm_input_authority, spec
        )
        if hwm_input_authority != expected_hwm_input_authority:
            raise PipelineValidationError("financial MVP HWM input authority differs")
    _validate_financial_mvp_production_binding(
        spec,
        product_gate_sha256=hashlib.sha256(product_gate_bytes).hexdigest(),
        provider_generation_id=manifest.generation_id,
    )
    result = {
        "conformance": dict(conformance),
        "raw": dict(gate_payload),
    }
    if hwm_input_authority is not None:
        result["hwm_input_authority"] = dict(hwm_input_authority)
    return result


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
    schema_version = raw.get("schema_version")
    expected_keys = {
        "authority_snapshot",
        "command_sources",
        "profile",
        "release_commit",
        "runtime",
        "schema_version",
        "semantic_reconciliation_node",
        "source_acquisitions",
        "stages",
    }
    if schema_version in {2, 3, 4}:
        expected_keys.add("product_authority")
    if schema_version == 3:
        expected_keys.update(
            {
                "financial_epoch_manifest_inputs",
                "financial_epoch_manifest_proof_sha256",
            }
        )
    if schema_version == 4:
        # A schema-10 vertical recipe reuses one verified epoch-manifest
        # generation instead of the five raw role files, so the contract
        # carries that generation's identity rather than the input set.
        expected_keys.update(
            {
                "epoch_manifest_generation",
                "epoch_manifest_source_sha256",
                "financial_mvp_bound_contract_sha256",
            }
        )
    _strict(raw, expected_keys, "portfolio orchestration contract")
    if schema_version not in {1, 2, 3, 4} or raw["profile"] != _PORTFOLIO_PROFILE:
        raise PipelineValidationError("portfolio orchestration profile is unsupported")
    product_authority = _financial_mvp_product_authority(spec)
    if schema_version in {2, 3, 4}:
        if product_authority is None or canonical_json_bytes(
            raw["product_authority"]
        ) != canonical_json_bytes(product_authority["raw"]):
            raise PipelineValidationError(
                "portfolio product authority differs from product gate"
            )
    elif product_authority is not None:
        raise PipelineValidationError(
            "financial MVP product gate requires portfolio contract schema 2"
        )
    if schema_version == 4:
        _verified_epoch_manifest_orchestration(spec, raw)
    epoch_authority = _financial_epoch_manifest_authority(spec)
    if schema_version == 3:
        if epoch_authority is None or (
            canonical_json_bytes(raw["financial_epoch_manifest_inputs"])
            != canonical_json_bytes(cast(Mapping[str, Any], epoch_authority["inputs"]))
            or _hash(
                raw["financial_epoch_manifest_proof_sha256"],
                "portfolio financial epoch manifest proof sha256",
            )
            != epoch_authority["proof_sha256"]
        ):
            raise PipelineValidationError(
                "portfolio financial epoch manifest authority differs"
            )
    elif epoch_authority is not None:
        raise PipelineValidationError(
            "financial epoch manifest gate requires portfolio contract schema 3"
        )
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
        "product_conformance": (
            None if product_authority is None else product_authority["conformance"]
        ),
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
    epoch_gates = [
        gate for gate in spec.gates if gate.gate_id == _FINANCIAL_EPOCH_MANIFEST_GATE
    ]
    if len(epoch_gates) > 1:
        raise PipelineValidationError("financial epoch manifest gate is ambiguous")
    expected_payload_keys = {
        "as_of_date",
        "checks",
        "release_commit",
        "release_generations",
        "schema_version",
        "unwaived_blocking_exceptions",
        "verdict",
    }
    if epoch_gates:
        expected_payload_keys.update(
            {
                "financial_epoch_manifest_inputs_sha256",
                "financial_epoch_manifest_proof_sha256",
            }
        )
    financial_check = "financial_outcomes_sha256" in payload
    if financial_check:
        expected_payload_keys.add("financial_outcomes_sha256")
    elif _requires_production_product(spec):
        raise PipelineValidationError("production financial outcomes check is missing")
    _strict(payload, expected_payload_keys, "post-build semantic gate")
    checks = _object(payload["checks"], "post-build semantic checks")
    product_authority = _financial_mvp_product_authority(spec)
    expected_checks = set(_POST_BUILD_CHECKS)
    if financial_check:
        expected_checks.add("financial_outcomes")
        proof = _verify_financial_outcomes(spec)
        if (
            payload["financial_outcomes_sha256"]
            != hashlib.sha256(canonical_json_bytes(proof)).hexdigest()
        ):
            raise PipelineValidationError(
                "financial outcomes semantic identity differs"
            )
    if product_authority is not None:
        expected_checks.add("product_conformance")
    if epoch_gates:
        expected_checks.add("epoch_manifest_input_authority")
        epoch_gate = epoch_gates[0]
        try:
            epoch_payload = _object(
                json.loads(epoch_gate.path.read_text(encoding="utf-8")),
                "financial epoch manifest gate",
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineValidationError(
                "financial epoch manifest gate is unreadable"
            ) from exc
        _strict(
            epoch_payload,
            {"inputs", "post_build_proof_sha256", "schema_version"},
            "financial epoch manifest gate",
        )
        inputs = _object(epoch_payload["inputs"], "financial epoch manifest inputs")
        _strict(
            inputs,
            _FINANCIAL_EPOCH_MANIFEST_INPUTS,
            "financial epoch manifest inputs",
        )
        for name in sorted(_FINANCIAL_EPOCH_MANIFEST_INPUTS):
            _file_definition_value(inputs[name], f"financial epoch manifest {name}")
        input_sha256 = hashlib.sha256(canonical_json_bytes(inputs)).hexdigest()
        proof_sha256 = _hash(
            epoch_payload["post_build_proof_sha256"],
            "financial epoch manifest proof sha256",
        )
        if (
            epoch_payload["schema_version"] != 2
            or payload["financial_epoch_manifest_inputs_sha256"] != input_sha256
            or payload["financial_epoch_manifest_proof_sha256"] != proof_sha256
        ):
            raise PipelineValidationError(
                "financial epoch manifest semantic identities differ"
            )
    if set(checks) != expected_checks or any(
        value != "PASS" for value in checks.values()
    ):
        raise PipelineValidationError("post-build semantic checks did not pass")
    if (
        payload["schema_version"]
        != (3 if epoch_gates else 2 if product_authority is not None else 1)
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


@dataclass(frozen=True)
class _CapturedStream:
    sha256: str
    size_bytes: int
    captured_bytes: int
    truncated: bool


@dataclass(frozen=True)
class _CommandRun:
    return_code: int
    stdout: _CapturedStream
    stderr: _CapturedStream


CommandRunnerResult = tuple[int, bytes, bytes] | _CommandRun
CommandRunner = Callable[[CommandNode], CommandRunnerResult]


def _capture_bytes(value: bytes) -> _CapturedStream:
    size = len(value)
    return _CapturedStream(
        sha256=hashlib.sha256(value).hexdigest(),
        size_bytes=size,
        captured_bytes=min(size, _COMMAND_OUTPUT_CAPTURE_LIMIT_BYTES),
        truncated=size > _COMMAND_OUTPUT_CAPTURE_LIMIT_BYTES,
    )


def _capture_stream(stream: BinaryIO) -> _CapturedStream:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(64 * 1024), b""):
        digest.update(block)
        size += len(block)
    return _CapturedStream(
        sha256=digest.hexdigest(),
        size_bytes=size,
        captured_bytes=min(size, _COMMAND_OUTPUT_CAPTURE_LIMIT_BYTES),
        truncated=size > _COMMAND_OUTPUT_CAPTURE_LIMIT_BYTES,
    )


def _normalize_command_run(result: CommandRunnerResult) -> _CommandRun:
    if isinstance(result, _CommandRun):
        return result
    return_code, stdout, stderr = result
    return _CommandRun(
        return_code=return_code,
        stdout=_capture_bytes(stdout),
        stderr=_capture_bytes(stderr),
    )


def _stream_evidence(run: _CommandRun) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, stream in (("stdout", run.stdout), ("stderr", run.stderr)):
        result.update(
            {
                f"{name}_captured_bytes": stream.captured_bytes,
                f"{name}_sha256": stream.sha256,
                f"{name}_size_bytes": stream.size_bytes,
                f"{name}_truncated": stream.truncated,
            }
        )
    return result


def _default_command_runner(node: CommandNode) -> _CommandRun:
    process = subprocess.Popen(
        node.argv,
        cwd=node.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise PipelineExecutionError("command output pipes are unavailable")
    captured: dict[str, _CapturedStream] = {}
    failures: list[BaseException] = []

    def drain(name: str, stream: BinaryIO) -> None:
        try:
            captured[name] = _capture_stream(stream)
        except BaseException as exc:
            failures.append(exc)

    stdout_thread = threading.Thread(
        target=drain, args=("stdout", process.stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=drain, args=("stderr", process.stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=node.timeout_seconds)
    except BaseException:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise
    stdout_thread.join()
    stderr_thread.join()
    if failures:
        raise PipelineExecutionError("command output capture failed") from failures[0]
    return _CommandRun(return_code, captured["stdout"], captured["stderr"])


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
        in_flight = node.effect is CommandEffect.READ_ONLY_EXTERNAL
        if in_flight:
            prior_attempts.append(
                {
                    "attempt": attempts,
                    "exception_type": None,
                    "outcome": "IN_FLIGHT",
                    "post_io": None,
                    "pre_io": pre_io,
                    "return_code": None,
                    "stderr_captured_bytes": None,
                    "stderr_sha256": None,
                    "stderr_size_bytes": None,
                    "stderr_truncated": None,
                    "stdout_captured_bytes": None,
                    "stdout_sha256": None,
                    "stdout_size_bytes": None,
                    "stdout_truncated": None,
                }
            )
            _write_receipt(
                spec,
                node,
                ReceiptState.UNKNOWN,
                attempts,
                None,
                {
                    **_command_evidence_base(node),
                    "attempts": prior_attempts,
                },
            )
        try:
            run = _normalize_command_run(command_runner(node))
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
                "stderr_captured_bytes": None,
                "stderr_sha256": None,
                "stderr_size_bytes": None,
                "stderr_truncated": None,
                "stdout_captured_bytes": None,
                "stdout_sha256": None,
                "stdout_size_bytes": None,
                "stdout_truncated": None,
            }
            if in_flight:
                prior_attempts[-1] = attempt_evidence
            else:
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
                    "return_code": run.return_code,
                    "stderr_sha256": run.stderr.sha256,
                    "stdout_sha256": run.stdout.sha256,
                }
            )
        ).hexdigest()
        attempt_evidence = {
            "attempt": attempts,
            "exception_type": None,
            "outcome": "COMPLETED" if run.return_code == 0 else "FAILED",
            "post_io": _io_evidence(node),
            "pre_io": pre_io,
            "return_code": run.return_code,
            **_stream_evidence(run),
        }
        if in_flight:
            prior_attempts[-1] = attempt_evidence
        else:
            prior_attempts.append(attempt_evidence)
        command_evidence = {
            **_command_evidence_base(node),
            "attempts": prior_attempts,
        }
        if run.return_code == 0:
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
            f"{node.node_id} command exited with {run.return_code}"
        )
    raise AssertionError("bounded command loop did not terminate")


def _financial_mvp_summary(
    authority: Mapping[str, Any] | None,
    spec: PipelineSpec | None = None,
) -> dict[str, object] | None:
    if authority is None:
        return None
    conformance = _object(authority["conformance"], "financial MVP conformance")
    hwm_input_authority_raw = authority.get("hwm_input_authority")
    hwm_input_authority = (
        _object(hwm_input_authority_raw, "financial MVP HWM input authority")
        if hwm_input_authority_raw is not None
        else None
    )
    admission = _object(conformance["provider_admission"], "provider admission")
    sources = _objects(conformance["sources"], "financial MVP sources")
    outcomes = _objects(conformance["outcomes"], "financial MVP outcomes")
    strategies = _objects(conformance["strategies"], "financial MVP strategies")
    memberships = _objects(
        conformance["census_memberships"], "financial MVP census memberships"
    )
    current_member_strategy_ids = sorted(
        {
            str(assignment["strategy_id"])
            for membership in memberships
            for assignment in _objects(
                membership.get("assignments"), "financial MVP membership assignments"
            )
            if assignment.get("status") == "FUNDED_MEMBERSHIP"
            and assignment.get("effective_end") is None
        }
    )
    conformance_blocking_reasons = _strings(
        conformance["blocking_reasons"], "financial MVP blocking reasons"
    )
    hwm_blocking_reasons = (
        _strings(
            hwm_input_authority["blocking_reasons"],
            "financial MVP HWM blocking reasons",
        )
        if hwm_input_authority is not None
        else ("HWM_INPUT_AUTHORITY_REQUIRED",)
    )
    correction_authority = _object(
        conformance["correction_authority"], "financial MVP correction authority"
    )
    # Frozen source and outcome readiness describes the accepted execution
    # baseline. The candidate must be built before its financial outputs can
    # satisfy that baseline. Actual admission and applicable corrections gate
    # construction; canonical output verification gates production below.
    candidate_blockers = {
        *(
            ("PROVIDER_ADMISSION",)
            if admission.get("product_readiness") != "READY_FOR_COMPOSITION"
            else ()
        ),
        *(
            ("CORRECTION_AUTHORITY_REQUIRED",)
            if correction_authority.get("authority_state") != "PROVIDED"
            else ()
        ),
        *(
            f"OPEN_CORRECTION:{value}"
            for value in _strings(
                correction_authority.get("applicable_open_correction_ids"),
                "applicable open correction IDs",
            )
        ),
    }
    blocking_reasons = sorted(candidate_blockers)
    completion_blocking_reasons = sorted(
        {*conformance_blocking_reasons, *hwm_blocking_reasons}
    )
    ready_for_execution = (
        not blocking_reasons
        and hwm_input_authority is not None
        and hwm_input_authority.get("schema_version") == 2
    )
    receipt = _object(admission["receipt"], "provider admission receipt")
    provider_rows = _objects(
        receipt["provider_generations"], "provider admission generations"
    )
    dispositions = _objects(receipt["dispositions"], "provider dispositions")
    provider_summaries = []
    for provider in sorted(provider_rows, key=lambda row: str(row["provider_id"])):
        provider_id = str(provider["provider_id"])
        provider_dispositions = [
            row for row in dispositions if row.get("provider_id") == provider_id
        ]
        provider_summaries.append(
            {
                "admit_count": sum(
                    row.get("action") == "ADMIT" for row in provider_dispositions
                ),
                "catalog_sha256": provider["catalog_sha256"],
                "exclude_count": sum(
                    row.get("action") == "EXCLUDE" for row in provider_dispositions
                ),
                "generation_id": provider["generation_id"],
                "manifest_sha256": provider["manifest_sha256"],
                "provider_id": provider_id,
                "qualification": provider["qualification"],
            }
        )
    data_class_dispositions = sorted(
        (
            {
                "action": row["action"],
                "data_class": row["data_class"],
                "evidence_layer": row["evidence_layer"],
                "invalidated_outputs": row["invalidated_outputs"],
                "provider_id": row["provider_id"],
                "readiness_impact": row["readiness_impact"],
                "reason": row["reason"],
                "source_segment_id": row["source_segment_id"],
                "target_facts": row["target_facts"],
            }
            for row in dispositions
            if row.get("item_kind") == "PRODUCT_DATA_CLASS"
        ),
        key=lambda row: (
            str(row["provider_id"]),
            str(row["source_segment_id"]),
            str(row["data_class"]),
        ),
    )
    source_routes = {
        str(row["source_id"]): row
        for row in _objects(
            conformance["source_invalidation_routes"], "source invalidation routes"
        )
    }
    source_summaries = []
    for row in sources:
        acquisition = _object(row["acquisition"], "source acquisition")
        admission_row = _object(row["admission"], "source admission")
        source_id = str(row["source_id"])
        source_summaries.append(
            {
                "absence_behavior": row["absence_behavior"],
                "acquisition_adapter": acquisition["adapter"],
                "acquisition_mode": acquisition["mode"],
                "admission_transform": admission_row["transform"],
                "admission_version": admission_row["version"],
                "blockers": row["blockers"],
                "consumers": row["consumers"],
                "current_generation": row["current_generation"],
                "data_classes": row["data_classes"],
                "evidence_layer": row["evidence_layer"],
                "freshness": row["freshness"],
                "interval": row["interval"],
                "invalidated_outputs": row["invalidated_outputs"],
                "readiness": row["readiness"],
                "rebuild_route": source_routes[source_id],
                "remediation": row["owner_policy_decision"],
                "schema": row["schema"],
                "source_id": source_id,
                "temporal": row["temporal"],
                "units": row["units"],
            }
        )
    candidate_artifacts: list[dict[str, object]] = []
    if spec is not None:
        for node in spec.ordered_nodes:
            if isinstance(node, GenerationNode):
                candidate_artifacts.append(
                    {
                        "generation_id": node.generation_id,
                        "manifest_sha256": node.manifest_sha256,
                        "node_id": node.node_id,
                        "release_target": node.release_target,
                        "root_id": node.root_id,
                    }
                )
    composition_digest = hashlib.sha256(canonical_json_bytes(conformance)).hexdigest()
    result = {
        "blocking_reasons": list(blocking_reasons),
        "candidate_artifacts": candidate_artifacts,
        "census_sha256": conformance["census_sha256"],
        "composition_digest": composition_digest,
        "completion_blocking_reasons": completion_blocking_reasons,
        "contract_semantic_sha256": conformance["contract_semantic_sha256"],
        "correction_authority": conformance["correction_authority"],
        "current_member_strategy_ids": current_member_strategy_ids,
        "census_sleeve_count": len(memberships),
        "data_class_dispositions": data_class_dispositions,
        "outcome_count": len(outcomes),
        "outcomes": outcomes,
        "product_id": conformance["product_id"],
        "product_version": conformance["product_version"],
        "provider_admission_gate_status": admission["admission_gate_status"],
        "provider_product_readiness": admission["product_readiness"],
        "readiness": (
            "READY_FOR_EXECUTION" if ready_for_execution else "REQUIRED_NOT_READY"
        ),
        "rederive_authority": conformance["rederive_authority"],
        "ready_for_execution": ready_for_execution,
        "required_not_ready_outcomes": sorted(
            str(row["outcome_id"])
            for row in outcomes
            if row.get("product_requirement") == "REQUIRED"
            and row.get("readiness") != "READY"
        ),
        "required_not_ready_sources": sorted(
            str(row["source_id"])
            for row in sources
            if row.get("product_requirement") == "REQUIRED"
            and row.get("readiness") != "READY"
        ),
        "source_count": len(sources),
        "sources": source_summaries,
        "stage_order": conformance["stage_order"],
        "stages": conformance["stage_identities"],
        "strategy_count": len(strategies),
        "providers": provider_summaries,
    }
    if hwm_input_authority is not None:
        result["hwm_input_authority"] = dict(hwm_input_authority)
    return result


def _current_product_authority(authority: Mapping[str, Any] | None) -> bool:
    if authority is None:
        return False
    raw = authority.get("raw")
    hwm = authority.get("hwm_input_authority")
    return (
        isinstance(raw, Mapping)
        and raw.get("schema_version") == 3
        and isinstance(hwm, Mapping)
        and hwm.get("schema_version") == 2
    )


def _requires_current_product(spec: PipelineSpec) -> bool:
    return any(
        gate.gate_id in {_PORTFOLIO_CONTRACT_GATE, _PORTFOLIO_PRODUCTION_REBIND_GATE}
        for gate in spec.gates
    )


def _requires_production_product(spec: PipelineSpec) -> bool:
    return any(gate.gate_id == _PORTFOLIO_PRODUCTION_REBIND_GATE for gate in spec.gates)


def _production_hwm_available(authority: Mapping[str, Any] | None) -> bool:
    if authority is None:
        return False
    hwm = authority.get("hwm_input_authority")
    return isinstance(hwm, Mapping) and hwm.get("state") == "AVAILABLE"


def plan_pipeline(spec: PipelineSpec) -> dict[str, object]:
    """Return the exact non-mutating execution and release plan."""

    _validate_portfolio_orchestration(spec, verify_runtime=True)
    product_authority = _financial_mvp_product_authority(spec)
    if (_requires_current_product(spec) and product_authority is None) or (
        product_authority is not None
        and not _current_product_authority(product_authority)
    ):
        raise PipelineValidationError(
            "legacy portfolio specification is recovery-only and cannot be planned"
        )
    if _requires_production_product(spec) and not _production_hwm_available(
        product_authority
    ):
        raise PipelineValidationError(
            "financial MVP production requires available governed HWM assessment"
        )
    product_summary = _financial_mvp_summary(product_authority, spec)
    if product_summary is not None and not product_summary["ready_for_execution"]:
        return {
            "commit_effects": [],
            "financial_mvp": product_summary,
            "node_count": 0,
            "nodes": [],
            "pipeline_id": spec.pipeline_id,
            "spec_sha256": spec.spec_sha256,
        }
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
    result: dict[str, object] = {
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
    if product_summary is not None:
        result["financial_mvp"] = product_summary
    return result


def _verify_freshness_provider_ledger(
    spec: PipelineSpec,
    value: object,
    evaluation_date: date,
) -> None:
    ledger = _object(value, "freshness provider ledger")
    _strict(
        ledger,
        {
            "as_of_date",
            "catalog_sha256",
            "catalog_size_bytes",
            "generation_id",
            "manifest_sha256",
            "manifest_size_bytes",
        },
        "freshness provider ledger",
    )
    ledger_as_of = date.fromisoformat(str(ledger.get("as_of_date")))
    if ledger_as_of > evaluation_date:
        raise PipelineValidationError(
            "freshness provider ledger postdates the evaluation"
        )
    generation_id = _hash(
        ledger.get("generation_id"), "freshness provider ledger generation_id"
    )
    for field in ("catalog_sha256", "manifest_sha256"):
        _hash(ledger.get(field), f"freshness provider ledger {field}")
    for field in ("catalog_size_bytes", "manifest_size_bytes"):
        size = ledger.get(field)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise PipelineValidationError(
                f"freshness provider ledger {field} must be a positive integer"
            )
    root_id = "provider-ledger"
    if root_id not in spec.roots or root_id not in spec.release_generations:
        raise PipelineValidationError(
            "freshness provider ledger is absent from the release"
        )
    if generation_id != spec.release_generations[root_id]:
        raise PipelineValidationError(
            "freshness provider ledger generation differs from the release"
        )
    root = spec.roots[root_id]
    manifest = read_generation(root, generation_id)
    artifacts = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == "normalized/provider-ledger.json"
    ]
    if len(artifacts) != 1:
        raise PipelineValidationError(
            "freshness provider ledger catalog artifact is absent or ambiguous"
        )
    generation = root / "generations" / generation_id
    catalog_sha256, catalog_size = _file_hash(generation / "provider-ledger.json")
    expected_catalog = artifacts[0]
    if (catalog_sha256, catalog_size) != (
        expected_catalog.sha256,
        expected_catalog.size_bytes,
    ):
        raise PipelineValidationError(
            "freshness provider ledger catalog differs from its manifest"
        )
    manifest_sha256, manifest_size = _file_hash(generation / "generation.json")
    observed = {
        "as_of_date": manifest.as_of_date.isoformat(),
        "catalog_sha256": catalog_sha256,
        "catalog_size_bytes": catalog_size,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_sha256,
        "manifest_size_bytes": manifest_size,
    }
    if ledger != observed:
        raise PipelineValidationError(
            "freshness provider ledger identity differs from the release"
        )


def _verify_freshness_gate(
    path: Path, evaluation_date: date, spec: PipelineSpec
) -> None:
    try:
        payload = _object(
            json.loads(path.read_text(encoding="utf-8")), "freshness gate"
        )
        schema_version = payload.get("assessment_schema_version")
        base_keys = (
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
        accepted_keys = {frozenset(base_keys)}
        if schema_version == 3:
            accepted_keys.add(frozenset(base_keys | {"provider_ledger"}))
        if frozenset(payload) not in accepted_keys:
            raise PipelineValidationError("freshness gate schema differs")
        assessed_on = date.fromisoformat(str(payload.get("as_of_date")))
        if assessed_on != evaluation_date:
            raise PipelineValidationError(
                "freshness gate was not evaluated on the operational date"
            )
        if "provider_ledger" in payload:
            _verify_freshness_provider_ledger(
                spec, payload.get("provider_ledger"), assessed_on
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
    for drift in actual_drifts:
        root_id = drift["root_id"]
        if spec.release_generations[root_id] == drift["generation_id"]:
            raise PipelineValidationError(
                f"predecessor repair reuses drifted generation: {root_id}"
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
            _verify_freshness_gate(gate.path, evaluation_date, spec)
        elif gate.gate_id == _POST_BUILD_SEMANTIC_GATE:
            _verify_post_build_semantic_gate(spec, gate.path)
        elif gate.gate_id == _FINANCIAL_MVP_PRODUCT_GATE:
            _financial_mvp_product_authority(spec)
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


def _require_executable_product(spec: PipelineSpec) -> dict[str, object] | None:
    product_authority = _financial_mvp_product_authority(spec)
    if (_requires_current_product(spec) and product_authority is None) or (
        product_authority is not None
        and not _current_product_authority(product_authority)
    ):
        raise PipelineValidationError(
            "legacy portfolio specification is recovery-only and cannot execute"
        )
    if _requires_production_product(spec) and not _production_hwm_available(
        product_authority
    ):
        raise PipelineValidationError(
            "financial MVP production requires available governed HWM assessment"
        )
    product_summary = _financial_mvp_summary(product_authority, spec)
    if product_summary is not None and not product_summary["ready_for_execution"]:
        blocking_reasons = product_summary["blocking_reasons"]
        if not isinstance(blocking_reasons, list):
            raise PipelineValidationError("financial MVP blockers are invalid")
        raise PipelineExecutionError(
            "financial MVP is required-not-ready: "
            + ",".join(str(item) for item in blocking_reasons)
        )
    if _requires_production_product(spec):
        _verify_financial_outcomes(spec)
    return product_summary


def _validate_commit_authorization(
    path: Path | None, spec: PipelineSpec
) -> GateEvidence:
    if path is None:
        raise PipelineValidationError("commit authorization is required")
    try:
        if path.is_symlink() or not path.is_file():
            raise PipelineValidationError("commit authorization is not a regular file")
        decoded = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise PipelineValidationError("commit authorization root must be an object")
        authorization = GateEvidence.from_dict(decoded)
    except PipelineValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PipelineValidationError("commit authorization is invalid") from exc
    if authorization.gate_id != _OWNER_PUBLICATION_AUTHORIZATION_GATE:
        raise PipelineValidationError("owner publication authorization is required")
    if authorization.evidence_sha256 != spec.spec_sha256:
        raise PipelineValidationError(
            "commit authorization covers a different pipeline spec"
        )
    return authorization


def execute_pipeline(
    spec: PipelineSpec,
    *,
    commit: bool = False,
    commit_authorization: Path | None = None,
    allow_external_read: bool = False,
    command_runner: CommandRunner = _default_command_runner,
    fault_hook: Callable[[str, str], None] | None = None,
    evaluation_date: date | None = None,
) -> dict[str, object]:
    """Build, verify, prepare, and optionally commit one explicit-ID DAG."""

    _validate_portfolio_orchestration(spec, verify_runtime=True)
    _require_executable_product(spec)
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
        _validate_portfolio_orchestration(spec, verify_runtime=True)
        _require_executable_product(spec)
        _validate_commit_authorization(commit_authorization, spec)
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
    _validate_portfolio_orchestration(spec, verify_runtime=False)
    product_authority = _financial_mvp_product_authority(spec)
    status_authority = (
        product_authority.get("raw") if isinstance(product_authority, Mapping) else None
    )
    if _requires_production_product(spec) and (
        not isinstance(status_authority, Mapping)
        or status_authority.get("schema_version") not in {2, 3}
    ):
        raise PipelineValidationError(
            "financial MVP production authority is recovery-only"
        )
    product_summary = _financial_mvp_summary(product_authority, spec)
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
    result: dict[str, object] = {
        "pipeline_id": spec.pipeline_id,
        "receipts": receipts,
        "release_id": release_id,
        "release_state": release_state,
        "spec_sha256": spec.spec_sha256,
    }
    if product_summary is not None:
        result["financial_mvp"] = product_summary
    return result


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

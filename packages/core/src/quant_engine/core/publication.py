"""Create-only generation staging and atomic accepted-pointer publication."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from types import MappingProxyType, ModuleType
from typing import Callable, Final, Mapping, Protocol, Sequence, cast

from quant_engine.core.contracts import (
    CoverageStatus,
    GenerationManifest,
    ManifestValidationError,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)
from quant_engine.core.strict_json import (
    StrictJSONError,
    strict_json_loads,
    strict_json_object,
)


_GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_POINTER_SCHEMA_VERSION = 1
_POINTER_SCHEMA_VERSION = 2
_INVENTORY_SCHEMA_VERSION = 1
_ENVELOPE_SCHEMA_VERSION = 1
_BUNDLE_INVENTORY_SCHEMA_VERSION = 1
_STAGE_RENAME_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)
_GENERATION_MANIFEST_PATH = "generation.json"
_GENERATION_INVENTORY_PATH = "generation-inventory.json"
_GENERATION_RECEIPT_PATH = "generation-receipt.json"
_INDEPENDENT_REPORT_PATH = "verification/independent-verification.json"
_CONTROL_PATHS = frozenset(
    {
        _GENERATION_MANIFEST_PATH,
        _GENERATION_INVENTORY_PATH,
        _GENERATION_RECEIPT_PATH,
    }
)
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_COMPONENT_STEMS = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"com{suffix}" for suffix in ("¹", "²", "³")),
        *(f"lpt{index}" for index in range(1, 10)),
        *(f"lpt{suffix}" for suffix in ("¹", "²", "³")),
    }
)
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_MOVEFILE_WRITE_THROUGH = 0x00000008


class _WindowsFunction(Protocol):
    """Minimal callable surface exposed by dynamically loaded Win32 APIs."""

    argtypes: object
    restype: object

    def __call__(self, *args: object) -> object: ...


class _WindowsDLL(Protocol):
    def __getattr__(self, name: str) -> _WindowsFunction: ...


def _windows_kernel32(ctypes_module: ModuleType) -> _WindowsDLL:
    """Load kernel32 without requiring non-Windows ctypes stubs to expose it."""

    win_dll = cast(Callable[..., _WindowsDLL], getattr(ctypes_module, "WinDLL"))
    return win_dll("kernel32", use_last_error=True)


def _windows_last_error(ctypes_module: ModuleType) -> int:
    get_last_error = cast(Callable[[], int], getattr(ctypes_module, "get_last_error"))
    return get_last_error()


def _windows_error(ctypes_module: ModuleType) -> OSError:
    win_error = cast(Callable[[int], OSError], getattr(ctypes_module, "WinError"))
    return win_error(_windows_last_error(ctypes_module))


def _windows_open_osfhandle(msvcrt_module: ModuleType, handle: int, flags: int) -> int:
    open_osfhandle = cast(
        Callable[[int, int], int], getattr(msvcrt_module, "open_osfhandle")
    )
    return open_osfhandle(handle, flags)


class GenerationConflictError(RuntimeError):
    """A create-only generation path already contains different evidence."""


class PublicationRejectedError(RuntimeError):
    """A generation is not eligible to become current authority."""


class PointerIntegrityError(RuntimeError):
    """The current pointer or referenced manifest failed integrity checks."""


@dataclass(frozen=True)
class GenerationFileIdentity:
    relative_path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _validate_raw_artifact_path(
            self.relative_path, seen_casefolded=set(), allow_control=True
        )
        if (
            not isinstance(self.sha256, str)
            or _GENERATION_ID.fullmatch(self.sha256) is None
        ):
            raise ManifestValidationError("generation file sha256 is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ManifestValidationError(
                "generation file size_bytes must be a non-negative integer"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> GenerationFileIdentity:
        if not isinstance(value, dict) or set(value) != {
            "relative_path",
            "sha256",
            "size_bytes",
        }:
            raise ManifestValidationError(
                "generation file identity keys differ from the contract"
            )
        relative_path = value["relative_path"]
        sha256 = value["sha256"]
        size_bytes = value["size_bytes"]
        if not isinstance(relative_path, str) or not isinstance(sha256, str):
            raise ManifestValidationError(
                "generation file identity text fields must be strings"
            )
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ManifestValidationError(
                "generation file size_bytes must be an integer"
            )
        return cls(relative_path, sha256, size_bytes)


@dataclass(frozen=True)
class GenerationInventory:
    generation_id: str
    files: tuple[GenerationFileIdentity, ...]
    inventory_schema_version: int = _BUNDLE_INVENTORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation_id, str)
            or _GENERATION_ID.fullmatch(self.generation_id) is None
        ):
            raise ManifestValidationError("inventory generation_id is invalid")
        if (
            isinstance(self.inventory_schema_version, bool)
            or self.inventory_schema_version != _BUNDLE_INVENTORY_SCHEMA_VERSION
        ):
            raise ManifestValidationError("inventory schema version is unsupported")
        if type(self.files) is not tuple or not all(
            isinstance(item, GenerationFileIdentity) for item in self.files
        ):
            raise ManifestValidationError(
                "inventory files must be a tuple of generation file identities"
            )
        paths = tuple(item.relative_path for item in self.files)
        if len(paths) < 2:
            raise ManifestValidationError("inventory must contain at least two files")
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ManifestValidationError(
                "inventory file identities must be sorted and unique"
            )
        if len({path.casefold() for path in paths}) != len(paths):
            raise ManifestValidationError(
                "inventory paths must be unique case-insensitively"
            )
        if (
            _GENERATION_MANIFEST_PATH not in paths
            or _INDEPENDENT_REPORT_PATH not in paths
        ):
            raise ManifestValidationError(
                "inventory lacks a required manifest or independent report"
            )
        if _GENERATION_INVENTORY_PATH in paths or _GENERATION_RECEIPT_PATH in paths:
            raise ManifestValidationError("inventory contains a detached control path")
        _validate_file_path_prefixes(paths)

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "files": [item.to_dict() for item in self.files],
                "generation_id": self.generation_id,
                "inventory_schema_version": self.inventory_schema_version,
            }
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> GenerationInventory:
        if type(value) is not bytes:
            raise ManifestValidationError("inventory source must have exact type bytes")
        try:
            decoded = strict_json_loads(value, label="generation inventory")
        except StrictJSONError as exc:
            raise ManifestValidationError(
                "generation inventory is not strict JSON"
            ) from exc
        if not isinstance(decoded, dict) or set(decoded) != {
            "files",
            "generation_id",
            "inventory_schema_version",
        }:
            raise ManifestValidationError("inventory keys differ from the contract")
        generation_id = decoded["generation_id"]
        schema_version = decoded["inventory_schema_version"]
        files = decoded["files"]
        if not isinstance(generation_id, str):
            raise ManifestValidationError("inventory generation_id must be a string")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ManifestValidationError("inventory schema version must be an integer")
        if not isinstance(files, list):
            raise ManifestValidationError("inventory files must be an array")
        result = cls(
            generation_id=generation_id,
            files=tuple(GenerationFileIdentity.from_dict(item) for item in files),
            inventory_schema_version=schema_version,
        )
        if result.to_json_bytes() != value:
            raise ManifestValidationError(
                "generation inventory bytes are not canonical"
            )
        return result


@dataclass(frozen=True)
class GenerationReceipt:
    generation_id: str
    independent_report: SourceArtifact
    inventory: GenerationFileIdentity
    manifest: GenerationFileIdentity
    final_path_count: int
    envelope_schema_version: int = _ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation_id, str)
            or _GENERATION_ID.fullmatch(self.generation_id) is None
        ):
            raise ManifestValidationError("receipt generation_id is invalid")
        if not isinstance(self.independent_report, SourceArtifact):
            raise ManifestValidationError("receipt independent_report is invalid")
        if self.independent_report.artifact_id != _INDEPENDENT_REPORT_PATH:
            raise ManifestValidationError("receipt independent_report path is invalid")
        if not isinstance(self.inventory, GenerationFileIdentity) or (
            self.inventory.relative_path != _GENERATION_INVENTORY_PATH
        ):
            raise ManifestValidationError("receipt inventory identity is invalid")
        if not isinstance(self.manifest, GenerationFileIdentity) or (
            self.manifest.relative_path != _GENERATION_MANIFEST_PATH
        ):
            raise ManifestValidationError("receipt manifest identity is invalid")
        if (
            isinstance(self.final_path_count, bool)
            or not isinstance(self.final_path_count, int)
            or self.final_path_count < 4
        ):
            raise ManifestValidationError(
                "receipt final_path_count must be an integer of at least four"
            )
        if (
            isinstance(self.envelope_schema_version, bool)
            or self.envelope_schema_version != _ENVELOPE_SCHEMA_VERSION
        ):
            raise ManifestValidationError("receipt schema version is unsupported")

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "envelope_schema_version": self.envelope_schema_version,
                "final_path_count": self.final_path_count,
                "generation_id": self.generation_id,
                "independent_report": self.independent_report.to_dict(),
                "inventory": self.inventory.to_dict(),
                "manifest": self.manifest.to_dict(),
            }
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> GenerationReceipt:
        if type(value) is not bytes:
            raise ManifestValidationError("receipt source must have exact type bytes")
        try:
            decoded = strict_json_loads(value, label="generation receipt")
        except StrictJSONError as exc:
            raise ManifestValidationError(
                "generation receipt is not strict JSON"
            ) from exc
        if not isinstance(decoded, dict) or set(decoded) != {
            "envelope_schema_version",
            "final_path_count",
            "generation_id",
            "independent_report",
            "inventory",
            "manifest",
        }:
            raise ManifestValidationError("receipt keys differ from the contract")
        generation_id = decoded["generation_id"]
        final_path_count = decoded["final_path_count"]
        schema_version = decoded["envelope_schema_version"]
        if not isinstance(generation_id, str):
            raise ManifestValidationError("receipt generation_id must be a string")
        if isinstance(final_path_count, bool) or not isinstance(final_path_count, int):
            raise ManifestValidationError("receipt final_path_count must be an integer")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ManifestValidationError("receipt schema version must be an integer")
        try:
            report = SourceArtifact.from_dict(decoded["independent_report"])
        except (KeyError, TypeError, ManifestValidationError) as exc:
            raise ManifestValidationError(
                "receipt independent_report is invalid"
            ) from exc
        result = cls(
            generation_id=generation_id,
            independent_report=report,
            inventory=GenerationFileIdentity.from_dict(decoded["inventory"]),
            manifest=GenerationFileIdentity.from_dict(decoded["manifest"]),
            final_path_count=final_path_count,
            envelope_schema_version=schema_version,
        )
        if result.to_json_bytes() != value:
            raise ManifestValidationError("generation receipt bytes are not canonical")
        return result


@dataclass(frozen=True)
class GenerationBundle:
    manifest: GenerationManifest
    payloads: Mapping[str, bytes]
    inventory: GenerationInventory
    inventory_bytes: bytes
    receipt: GenerationReceipt
    receipt_bytes: bytes
    full_directory_inventory_sha256: str

    def __post_init__(self) -> None:
        if type(self.manifest) is not GenerationManifest:
            raise ManifestValidationError("bundle manifest type is invalid")
        if not isinstance(self.payloads, Mapping) or not all(
            isinstance(path, str) and type(value) is bytes
            for path, value in self.payloads.items()
        ):
            raise ManifestValidationError(
                "bundle payloads must map paths to exact bytes"
            )
        payloads = dict(sorted(self.payloads.items()))
        if type(self.inventory) is not GenerationInventory:
            raise ManifestValidationError("bundle inventory type is invalid")
        if type(self.receipt) is not GenerationReceipt:
            raise ManifestValidationError("bundle receipt type is invalid")
        if (
            type(self.inventory_bytes) is not bytes
            or type(self.receipt_bytes) is not bytes
        ):
            raise ManifestValidationError(
                "bundle control bytes must have exact type bytes"
            )
        if (
            not isinstance(self.full_directory_inventory_sha256, str)
            or _GENERATION_ID.fullmatch(self.full_directory_inventory_sha256) is None
        ):
            raise ManifestValidationError(
                "bundle directory inventory sha256 is invalid"
            )
        try:
            prepared = _prepare_generation_bundle(self.manifest, payloads)
            expected_inventory_bytes = prepared.final_files[_GENERATION_INVENTORY_PATH]
            expected_receipt_bytes = prepared.final_files[_GENERATION_RECEIPT_PATH]
            expected_inventory = GenerationInventory.from_json_bytes(
                expected_inventory_bytes
            )
            expected_receipt = GenerationReceipt.from_json_bytes(expected_receipt_bytes)
            expected_digest = _snapshot_inventory_sha256(prepared.final_files)
        except (KeyError, ManifestValidationError, TypeError):
            raise ManifestValidationError(
                "bundle payload contract is invalid"
            ) from None
        if (
            self.inventory_bytes != expected_inventory_bytes
            or self.inventory != expected_inventory
        ):
            raise ManifestValidationError("bundle inventory binding is invalid")
        if (
            self.receipt_bytes != expected_receipt_bytes
            or self.receipt != expected_receipt
        ):
            raise ManifestValidationError("bundle receipt binding is invalid")
        if self.full_directory_inventory_sha256 != expected_digest:
            raise ManifestValidationError(
                "bundle directory inventory digest is invalid"
            )
        object.__setattr__(
            self,
            "payloads",
            MappingProxyType(payloads),
        )


@dataclass(frozen=True)
class _PreparedGenerationBundle:
    """Exact, portable generation bytes validated before any staging operation."""

    final_files: Mapping[str, bytes]


def _sha256_and_size(value: bytes) -> tuple[str, int]:
    return hashlib.sha256(value).hexdigest(), len(value)


def _validate_raw_artifact_path(
    path: object, *, seen_casefolded: set[str], allow_control: bool = False
) -> str:
    """Reject unsafe raw strings before any path construction or normalization."""

    if not isinstance(path, str) or not path:
        raise ManifestValidationError("artifact path must be a non-empty string")
    if (
        path.startswith("/")
        or re.match(r"^[A-Za-z]:", path) is not None
        or "\\" in path
        or any(character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS for character in path)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ManifestValidationError("artifact path is not a portable raw path")
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ManifestValidationError("artifact path has an unsafe component")
    if any(component.endswith((" ", ".")) for component in components):
        raise ManifestValidationError(
            "artifact path has a Windows-normalized trailing character"
        )
    if any(
        component.split(".", maxsplit=1)[0].casefold()
        in _WINDOWS_RESERVED_COMPONENT_STEMS
        for component in components
    ):
        raise ManifestValidationError("artifact path has a reserved Windows component")
    folded = path.casefold()
    if folded in seen_casefolded:
        raise ManifestValidationError(
            "artifact paths must be unique case-insensitively"
        )
    if not allow_control and folded in {
        control.casefold() for control in _CONTROL_PATHS
    }:
        raise ManifestValidationError(
            "artifact path collides with a generation control path"
        )
    seen_casefolded.add(folded)
    return path


def _canonical_json_payload(value: bytes, *, artifact_id: str) -> None:
    try:
        parsed = strict_json_loads(value, label=f"payload {artifact_id}")
    except StrictJSONError as exc:
        raise ManifestValidationError("payload JSON is not strict JSON") from exc
    if canonical_json_bytes(parsed) != value:  # type: ignore[arg-type]
        raise ManifestValidationError("payload JSON bytes are not canonical")


def _validate_file_path_prefixes(paths: tuple[str, ...]) -> None:
    """Reject one final file path serving as another file's parent directory."""

    component_paths = tuple(
        (path, tuple(component.casefold() for component in path.split("/")))
        for path in paths
    )
    for left_path, left_components in component_paths:
        for right_path, right_components in component_paths:
            if (
                len(left_components) < len(right_components)
                and right_components[: len(left_components)] == left_components
            ):
                raise ManifestValidationError(
                    "generation file paths have a file/directory prefix collision: "
                    f"{left_path!r} and {right_path!r}"
                )


def _prepare_generation_bundle(
    manifest: GenerationManifest, payloads: Mapping[str, bytes]
) -> _PreparedGenerationBundle:
    """Build the non-circular final generation file set entirely in memory."""

    if not isinstance(payloads, Mapping):
        raise ManifestValidationError("generation bundle payloads must be a mapping")
    artifacts = tuple(manifest.artifacts)
    seen_casefolded: set[str] = set()
    artifact_ids = tuple(
        _validate_raw_artifact_path(
            artifact.artifact_id, seen_casefolded=seen_casefolded
        )
        for artifact in artifacts
    )
    _validate_file_path_prefixes((*artifact_ids, *sorted(_CONTROL_PATHS)))
    report_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.artifact_id == _INDEPENDENT_REPORT_PATH
    ]
    if len(report_artifacts) != 1:
        raise ManifestValidationError(
            "generation must declare exactly one independent report artifact"
        )
    try:
        manifest = GenerationManifest.from_json_bytes(manifest.to_json_bytes())
    except ManifestValidationError as exc:
        raise ManifestValidationError(
            "generation manifest failed in-memory integrity validation"
        ) from exc
    artifacts = tuple(manifest.artifacts)
    report_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.artifact_id == _INDEPENDENT_REPORT_PATH
    ]
    if set(payloads) != set(artifact_ids):
        raise ManifestValidationError("payload keys must equal manifest artifact IDs")

    payload_bytes: dict[str, bytes] = {}
    for artifact in artifacts:
        value = payloads[artifact.artifact_id]
        if type(value) is not bytes:
            raise ManifestValidationError("payload values must have exact type bytes")
        if artifact.artifact_id.casefold().endswith(".json"):
            _canonical_json_payload(value, artifact_id=artifact.artifact_id)
        actual_sha256, actual_size = _sha256_and_size(value)
        if actual_size != artifact.size_bytes:
            raise ManifestValidationError("payload size_bytes differs from manifest")
        if actual_sha256 != artifact.sha256:
            raise ManifestValidationError("payload sha256 differs from manifest")
        payload_bytes[artifact.artifact_id] = value

    manifest_bytes = manifest.to_json_bytes()
    manifest_sha256, manifest_size = _sha256_and_size(manifest_bytes)
    inventory_files = [
        {
            "relative_path": _GENERATION_MANIFEST_PATH,
            "sha256": manifest_sha256,
            "size_bytes": manifest_size,
        },
        *[
            {
                "relative_path": artifact.artifact_id,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in artifacts
        ],
    ]
    inventory_files.sort(key=lambda item: str(item["relative_path"]))
    inventory_bytes = canonical_json_bytes(
        {
            "files": inventory_files,
            "generation_id": manifest.generation_id,
            "inventory_schema_version": _BUNDLE_INVENTORY_SCHEMA_VERSION,
        }
    )
    inventory_sha256, inventory_size = _sha256_and_size(inventory_bytes)
    report = report_artifacts[0]
    receipt_bytes = canonical_json_bytes(
        {
            "envelope_schema_version": _ENVELOPE_SCHEMA_VERSION,
            "final_path_count": len(artifacts) + 3,
            "generation_id": manifest.generation_id,
            "independent_report": report.to_dict(),
            "inventory": {
                "relative_path": _GENERATION_INVENTORY_PATH,
                "sha256": inventory_sha256,
                "size_bytes": inventory_size,
            },
            "manifest": {
                "relative_path": _GENERATION_MANIFEST_PATH,
                "sha256": manifest_sha256,
                "size_bytes": manifest_size,
            },
        }
    )
    final_files = {
        _GENERATION_MANIFEST_PATH: manifest_bytes,
        **payload_bytes,
        _GENERATION_INVENTORY_PATH: inventory_bytes,
        _GENERATION_RECEIPT_PATH: receipt_bytes,
    }
    return _PreparedGenerationBundle(
        final_files=MappingProxyType(dict(sorted(final_files.items())))
    )


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        candidate = Path(_windows_api_path(path)) if os.name == "nt" else path
        return candidate.lstat()
    except FileNotFoundError:
        return None


def _stat_is_link_like(value: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & reparse_flag
    )


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    identity: tuple[int, ...] = (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )
    # CPython 3.12 reports different Windows ``st_ctime`` semantics for a
    # path stat and a stat of an open handle to the same object.  It is not a
    # stable object-identity field there; dev/inode plus the remaining
    # snapshot fields still detect replacement and content drift.
    if os.name != "nt":
        identity = (*identity, value.st_ctime_ns)
    attributes = getattr(value, "st_file_attributes", None)
    if isinstance(attributes, int):
        return (*identity, attributes)
    return identity


def _windows_api_path(path: Path) -> str:
    """Return an absolute extended-length path for native Windows APIs."""

    value = str(Path(path).absolute())
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return f"\\\\?\\UNC\\{value[2:]}"
    return f"\\\\?\\{value}"


def _require_directory_stat(value: os.stat_result) -> None:
    if _stat_is_link_like(value) or not stat.S_ISDIR(value.st_mode):
        raise GenerationConflictError(
            "generation bundle boundary is linked or irregular"
        )


def _require_file_stat(value: os.stat_result) -> None:
    if (
        _stat_is_link_like(value)
        or not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
    ):
        raise GenerationConflictError("generation bundle file is linked or irregular")


def _read_open_file(descriptor: int, expected: os.stat_result) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    value = b"".join(chunks)
    if len(value) != expected.st_size:
        raise GenerationConflictError("generation bundle file read was incomplete")
    if _stat_identity(os.fstat(descriptor)) != _stat_identity(expected):
        raise GenerationConflictError("generation bundle file identity changed")
    return value


@dataclass(frozen=True)
class _CapturedGeneration:
    files: Mapping[str, bytes]
    directories: frozenset[str]


def _capture_posix_directory(
    descriptor: int,
    *,
    prefix: str,
    files: dict[str, bytes],
    directories: set[str],
) -> None:
    nofollow: int = getattr(os, "O_NOFOLLOW")
    directory: int = getattr(os, "O_DIRECTORY")
    directory_flags = os.O_RDONLY | directory | nofollow
    file_flags = os.O_RDONLY | nofollow
    for name in sorted(os.listdir(descriptor)):
        relative = f"{prefix}/{name}" if prefix else name
        observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            _require_directory_stat(observed)
            child = os.open(name, directory_flags, dir_fd=descriptor)
            try:
                if _stat_identity(os.fstat(child)) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle directory identity changed"
                    )
                directories.add(relative)
                _capture_posix_directory(
                    child,
                    prefix=relative,
                    files=files,
                    directories=directories,
                )
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _stat_identity(os.fstat(child)) != _stat_identity(
                    observed
                ) or _stat_identity(after) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle directory identity changed"
                    )
            finally:
                os.close(child)
        elif stat.S_ISREG(observed.st_mode):
            _require_file_stat(observed)
            child = os.open(name, file_flags, dir_fd=descriptor)
            try:
                if _stat_identity(os.fstat(child)) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle file identity changed"
                    )
                files[relative] = _read_open_file(child, observed)
                after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _stat_identity(after) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle file identity changed"
                    )
            finally:
                os.close(child)
        else:
            raise GenerationConflictError("generation bundle entry is irregular")


def _open_windows_nofollow(path: Path, *, directory: bool) -> int:
    import msvcrt

    kernel32 = _windows_kernel32(ctypes)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = cast(
        int | None,
        create_file(
            _windows_api_path(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            flags,
            None,
        ),
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        raise _windows_error(ctypes)
    try:
        return _windows_open_osfhandle(
            msvcrt, handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError:
        kernel32.CloseHandle(handle)
        raise


def _capture_windows_directory(
    path: Path,
    descriptor: int,
    *,
    prefix: str,
    files: dict[str, bytes],
    directories: set[str],
) -> None:
    with os.scandir(path) as entries:
        ordered = sorted(entries, key=lambda item: item.name)
    for entry in ordered:
        relative = f"{prefix}/{entry.name}" if prefix else entry.name
        child_path = path / entry.name
        observed = child_path.lstat()
        if stat.S_ISDIR(observed.st_mode):
            _require_directory_stat(observed)
            child = _open_windows_nofollow(child_path, directory=True)
            try:
                if _stat_identity(os.fstat(child)) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle directory identity changed"
                    )
                directories.add(relative)
                _capture_windows_directory(
                    child_path,
                    child,
                    prefix=relative,
                    files=files,
                    directories=directories,
                )
                after = child_path.lstat()
                if _stat_identity(os.fstat(child)) != _stat_identity(
                    observed
                ) or _stat_identity(after) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle directory identity changed"
                    )
            finally:
                os.close(child)
        elif stat.S_ISREG(observed.st_mode):
            _require_file_stat(observed)
            child = _open_windows_nofollow(child_path, directory=False)
            try:
                if _stat_identity(os.fstat(child)) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle file identity changed"
                    )
                files[relative] = _read_open_file(child, observed)
                if _stat_identity(child_path.lstat()) != _stat_identity(observed):
                    raise GenerationConflictError(
                        "generation bundle file identity changed"
                    )
            finally:
                os.close(child)
        else:
            raise GenerationConflictError("generation bundle entry is irregular")
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise GenerationConflictError("generation bundle directory became irregular")


def _capture_generation_directory(
    root: Path, generation_id: str
) -> _CapturedGeneration:
    root = Path(root).absolute()
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    if os.name == "nt":
        root = Path(_windows_api_path(root))
        root_stat = root.lstat()
        _require_directory_stat(root_stat)
        root_descriptor = _open_windows_nofollow(root, directory=True)
        generations_descriptor: int | None = None
        final_descriptor: int | None = None
        try:
            if _stat_identity(os.fstat(root_descriptor)) != _stat_identity(root_stat):
                raise GenerationConflictError("publication root identity changed")
            generations = root / "generations"
            generations_stat = generations.lstat()
            _require_directory_stat(generations_stat)
            generations_descriptor = _open_windows_nofollow(generations, directory=True)
            if _stat_identity(os.fstat(generations_descriptor)) != _stat_identity(
                generations_stat
            ):
                raise GenerationConflictError("generations root identity changed")
            final = generations / generation_id
            final_stat = final.lstat()
            _require_directory_stat(final_stat)
            final_descriptor = _open_windows_nofollow(final, directory=True)
            if _stat_identity(os.fstat(final_descriptor)) != _stat_identity(final_stat):
                raise GenerationConflictError("generation directory identity changed")
            _capture_windows_directory(
                final,
                final_descriptor,
                prefix="",
                files=files,
                directories=directories,
            )
            for path, descriptor, observed, label in (
                (final, final_descriptor, final_stat, "generation directory"),
                (
                    generations,
                    generations_descriptor,
                    generations_stat,
                    "generations root",
                ),
                (root, root_descriptor, root_stat, "publication root"),
            ):
                if _stat_identity(os.fstat(descriptor)) != _stat_identity(
                    observed
                ) or _stat_identity(path.lstat()) != _stat_identity(observed):
                    raise GenerationConflictError(f"{label} identity changed")
        finally:
            if final_descriptor is not None:
                os.close(final_descriptor)
            if generations_descriptor is not None:
                os.close(generations_descriptor)
            os.close(root_descriptor)
    else:
        directory: int = getattr(os, "O_DIRECTORY")
        nofollow: int = getattr(os, "O_NOFOLLOW")
        directory_flags = os.O_RDONLY | directory | nofollow
        root_stat = root.lstat()
        _require_directory_stat(root_stat)
        root_descriptor = os.open(root, directory_flags)
        generations_descriptor = None
        final_descriptor = None
        try:
            if _stat_identity(os.fstat(root_descriptor)) != _stat_identity(root_stat):
                raise GenerationConflictError("publication root identity changed")
            generations_stat = os.stat(
                "generations", dir_fd=root_descriptor, follow_symlinks=False
            )
            _require_directory_stat(generations_stat)
            generations_descriptor = os.open(
                "generations", directory_flags, dir_fd=root_descriptor
            )
            if _stat_identity(os.fstat(generations_descriptor)) != _stat_identity(
                generations_stat
            ):
                raise GenerationConflictError("generations root identity changed")
            final_stat = os.stat(
                generation_id,
                dir_fd=generations_descriptor,
                follow_symlinks=False,
            )
            _require_directory_stat(final_stat)
            final_descriptor = os.open(
                generation_id, directory_flags, dir_fd=generations_descriptor
            )
            if _stat_identity(os.fstat(final_descriptor)) != _stat_identity(final_stat):
                raise GenerationConflictError("generation directory identity changed")
            _capture_posix_directory(
                final_descriptor,
                prefix="",
                files=files,
                directories=directories,
            )
            final_after = os.stat(
                generation_id,
                dir_fd=generations_descriptor,
                follow_symlinks=False,
            )
            generations_after = os.stat(
                "generations", dir_fd=root_descriptor, follow_symlinks=False
            )
            if _stat_identity(os.fstat(final_descriptor)) != _stat_identity(
                final_stat
            ) or _stat_identity(final_after) != _stat_identity(final_stat):
                raise GenerationConflictError("generation directory identity changed")
            if _stat_identity(os.fstat(generations_descriptor)) != _stat_identity(
                generations_stat
            ) or _stat_identity(generations_after) != _stat_identity(generations_stat):
                raise GenerationConflictError("generations root identity changed")
            if _stat_identity(os.fstat(root_descriptor)) != _stat_identity(
                root_stat
            ) or _stat_identity(root.lstat()) != _stat_identity(root_stat):
                raise GenerationConflictError("publication root identity changed")
        finally:
            if final_descriptor is not None:
                os.close(final_descriptor)
            if generations_descriptor is not None:
                os.close(generations_descriptor)
            os.close(root_descriptor)
    return _CapturedGeneration(
        files=MappingProxyType(dict(sorted(files.items()))),
        directories=frozenset(directories),
    )


def _expected_bundle_directories(paths: Mapping[str, bytes]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        components = path.split("/")
        directories.update(
            "/".join(components[:index]) for index in range(1, len(components))
        )
    return directories


def _directory_matches_exact_files(
    directory: Path, expected_files: Mapping[str, bytes]
) -> bool:
    if os.name == "nt":
        directory = Path(_windows_api_path(directory))
    root_stat = _lstat_or_none(directory)
    if (
        root_stat is None
        or _stat_is_link_like(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        return False
    expected_directories = _expected_bundle_directories(expected_files)
    actual_directories: set[str] = set()
    actual_files: set[str] = set()
    pending = [(directory, "")]
    try:
        while pending:
            current, prefix = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    entry_stat = Path(entry.path).lstat()
                    if _stat_is_link_like(entry_stat):
                        return False
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if relative not in expected_directories:
                            return False
                        actual_directories.add(relative)
                        pending.append((Path(entry.path), relative))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        expected = expected_files.get(relative)
                        if (
                            entry_stat.st_nlink != 1
                            or expected is None
                            or Path(entry.path).read_bytes() != expected
                        ):
                            return False
                        actual_files.add(relative)
                    else:
                        return False
    except OSError:
        return False
    return (
        actual_files == set(expected_files)
        and actual_directories == expected_directories
    )


def _tree_contains_only_regular_entries(directory: Path) -> bool:
    if os.name == "nt":
        directory = Path(_windows_api_path(directory))
    root_stat = _lstat_or_none(directory)
    if (
        root_stat is None
        or _stat_is_link_like(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        return False
    pending = [directory]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if _stat_is_link_like(entry_stat):
                        return False
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
                    elif not stat.S_ISREG(entry_stat.st_mode):
                        return False
    except OSError:
        return False
    return True


def _ensure_bundle_directory(path: Path, *, label: str, parents: bool) -> bool:
    entry_stat = _lstat_or_none(path)
    created = False
    if entry_stat is None:
        try:
            path.mkdir(parents=parents, exist_ok=False)
            created = True
        except FileExistsError:
            pass
        entry_stat = _lstat_or_none(path)
    if entry_stat is None:
        raise PublicationRejectedError(f"{label} could not be created")
    if _stat_is_link_like(entry_stat):
        raise PublicationRejectedError(f"{label} must not be linked or reparse-backed")
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise PublicationRejectedError(f"{label} must be a regular directory")
    return created


def _ensure_durable_bundle_directory_tree(path: Path, *, label: str) -> bool:
    """Create each missing directory level and durably record it in its parent."""

    missing: list[Path] = []
    current = path
    while _lstat_or_none(current) is None:
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise PublicationRejectedError(f"{label} has no existing directory anchor")
        current = parent
    _ensure_bundle_directory(current, label=f"{label} anchor", parents=False)
    for directory in reversed(missing):
        _ensure_bundle_directory(directory, label=label, parents=False)
        _sync_bundle_directory(directory.parent)
    return bool(missing)


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Commit one same-filesystem directory without replacing a destination."""

    if os.name == "nt":
        kernel32 = _windows_kernel32(ctypes)
        move_file = kernel32.MoveFileExW
        move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move_file.restype = ctypes.c_int
        if not move_file(
            _windows_api_path(source),
            _windows_api_path(target),
            _MOVEFILE_WRITE_THROUGH,
        ):
            raise _windows_error(ctypes)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise PublicationRejectedError(
                "atomic no-replace directory commit is unavailable"
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        if (
            renameat2(
                _AT_FDCWD,
                os.fsencode(source),
                _AT_FDCWD,
                os.fsencode(target),
                _RENAME_NOREPLACE,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(source), str(target))
        return
    raise PublicationRejectedError("atomic no-replace directory commit is unavailable")


def _require_atomic_noreplace_support() -> None:
    if os.name == "nt":
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if getattr(libc, "renameat2", None) is not None:
            return
    raise PublicationRejectedError("atomic no-replace directory commit is unavailable")


def _remove_owned_stage(temporary: Path, generations: Path) -> None:
    if (
        temporary.parent == generations
        and temporary.name.startswith(".stage-")
        and _tree_contains_only_regular_entries(temporary)
    ):
        owned_stage = (
            Path(_windows_api_path(temporary)) if os.name == "nt" else temporary
        )
        shutil.rmtree(owned_stage)


def _sync_bundle_directory(path: Path) -> None:
    """Durably sync a bundle directory, failing closed where POSIX supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


def _sync_prepared_bundle_directories(
    temporary: Path, expected_files: Mapping[str, bytes]
) -> None:
    directories = _expected_bundle_directories(expected_files)
    for relative in sorted(directories, key=lambda value: (-value.count("/"), value)):
        _sync_bundle_directory(temporary.joinpath(*relative.split("/")))
    _sync_bundle_directory(temporary)


def stage_generation_bundle(
    root: Path,
    manifest: GenerationManifest,
    payloads: Mapping[str, bytes],
) -> Path:
    """Durably stage one exact bundle through an atomic no-replace commit."""

    prepared = _prepare_generation_bundle(manifest, payloads)
    _require_atomic_noreplace_support()
    generation_id = GenerationManifest.from_json_bytes(
        prepared.final_files[_GENERATION_MANIFEST_PATH]
    ).generation_id
    root = Path(root).absolute()
    root_created = _ensure_durable_bundle_directory_tree(root, label="publication root")
    generations = root / "generations"
    generations_created = _ensure_bundle_directory(
        generations, label="generations root", parents=False
    )
    if root_created or generations_created:
        _sync_bundle_directory(root)
    final_directory = generations / generation_id
    if _lstat_or_none(final_directory) is not None:
        if _directory_matches_exact_files(final_directory, prepared.final_files):
            _sync_bundle_directory(generations)
            return final_directory
        raise GenerationConflictError(
            "generation bundle path already contains different or irregular evidence"
        )

    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=generations))
    cleanup_permitted = True
    try:
        for relative, content in prepared.final_files.items():
            destination = temporary.joinpath(*relative.split("/"))
            destination_parent = (
                Path(_windows_api_path(destination.parent))
                if os.name == "nt"
                else destination.parent
            )
            destination_parent.mkdir(parents=True, exist_ok=True)
            _write_and_sync(destination, content)
        if not _directory_matches_exact_files(temporary, prepared.final_files):
            raise GenerationConflictError(
                "temporary generation bundle differs from prepared bytes"
            )
        _sync_prepared_bundle_directories(temporary, prepared.final_files)

        for attempt in range(len(_STAGE_RENAME_RETRY_DELAYS) + 1):
            cleanup_permitted = False
            try:
                _rename_directory_noreplace(temporary, final_directory)
            except (OSError, PublicationRejectedError) as exc:
                source_stat = _lstat_or_none(temporary)
                source_present = source_stat is not None
                final_exact = _directory_matches_exact_files(
                    final_directory, prepared.final_files
                )
                if final_exact:
                    cleanup_permitted = source_present
                    _sync_bundle_directory(generations)
                    return final_directory
                if not source_present:
                    raise GenerationConflictError(
                        "generation bundle commit outcome is ambiguous"
                    ) from exc
                cleanup_permitted = True
                if _lstat_or_none(final_directory) is not None:
                    raise GenerationConflictError(
                        "generation bundle path appeared with different evidence"
                    ) from exc
                if isinstance(exc, PermissionError):
                    if attempt == len(_STAGE_RENAME_RETRY_DELAYS):
                        raise
                    time.sleep(_STAGE_RENAME_RETRY_DELAYS[attempt])
                    continue
                raise
            _sync_bundle_directory(generations)
            return final_directory
        raise AssertionError("bundle rename retry loop did not terminate")
    finally:
        if cleanup_permitted and _lstat_or_none(temporary) is not None:
            _remove_owned_stage(temporary, generations)


def _snapshot_inventory_sha256(files: Mapping[str, bytes]) -> str:
    identities = [
        GenerationFileIdentity(
            relative_path=relative_path,
            sha256=hashlib.sha256(value).hexdigest(),
            size_bytes=len(value),
        ).to_dict()
        for relative_path, value in sorted(files.items())
    ]
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "files": identities,
                "inventory_schema_version": _INVENTORY_SCHEMA_VERSION,
            }
        )
    ).hexdigest()


def verify_generation_bundle(root: Path, generation_id: str) -> GenerationBundle:
    """Return one exact immutable snapshot after full bundle verification."""

    if (
        not isinstance(generation_id, str)
        or _GENERATION_ID.fullmatch(generation_id) is None
    ):
        raise ManifestValidationError("generation_id must be 64 lower-case hex")
    try:
        captured = _capture_generation_directory(root, generation_id)
        manifest_bytes = captured.files[_GENERATION_MANIFEST_PATH]
        inventory_bytes = captured.files[_GENERATION_INVENTORY_PATH]
        receipt_bytes = captured.files[_GENERATION_RECEIPT_PATH]
        manifest = GenerationManifest.from_json_bytes(manifest_bytes)
        if manifest.generation_id != generation_id:
            raise GenerationConflictError(
                "generation manifest identity differs from the requested generation"
            )
        payload_paths = tuple(artifact.artifact_id for artifact in manifest.artifacts)
        payloads = {path: captured.files[path] for path in payload_paths}
        prepared = _prepare_generation_bundle(manifest, payloads)
        expected_directories = _expected_bundle_directories(prepared.final_files)
        if (
            dict(captured.files) != dict(prepared.final_files)
            or set(captured.directories) != expected_directories
        ):
            raise GenerationConflictError(
                "generation bundle path set or bytes differ from the contract"
            )

        inventory = GenerationInventory.from_json_bytes(inventory_bytes)
        receipt = GenerationReceipt.from_json_bytes(receipt_bytes)
        expected_inventory = GenerationInventory.from_json_bytes(
            prepared.final_files[_GENERATION_INVENTORY_PATH]
        )
        expected_receipt = GenerationReceipt.from_json_bytes(
            prepared.final_files[_GENERATION_RECEIPT_PATH]
        )
        if inventory != expected_inventory or receipt != expected_receipt:
            raise GenerationConflictError(
                "generation bundle detached controls differ from the contract"
            )
        return GenerationBundle(
            manifest=manifest,
            payloads=payloads,
            inventory=inventory,
            inventory_bytes=inventory_bytes,
            receipt=receipt,
            receipt_bytes=receipt_bytes,
            full_directory_inventory_sha256=_snapshot_inventory_sha256(captured.files),
        )
    except GenerationConflictError:
        raise
    except (KeyError, ManifestValidationError, OSError, StrictJSONError, TypeError):
        raise GenerationConflictError(
            "generation bundle failed immutable verification"
        ) from None


def read_generation_bundle(root: Path, generation_id: str) -> GenerationBundle:
    """Read a generation only through the complete immutable verifier."""

    return verify_generation_bundle(root, generation_id)


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


def _inventory_file_refusal(path: Path, directory: Path, name: str) -> str:
    """Say why this entry is inadmissible, distinguishing unopenable from irregular."""
    if path.is_symlink():
        return "generation inventory contains a linked or irregular file"
    listed = False
    try:
        listed = name in os.listdir(directory)
    except OSError:
        listed = False
    if listed:
        # The directory names it but it cannot be opened. On Windows that is almost always
        # the 260-character path limit; report the length so the reader can check it.
        return (
            "generation inventory entry cannot be opened although its directory lists it: "
            f"{len(str(path))}-character path, which exceeds the Windows limit of 260 "
            "unless long paths are enabled"
        )
    return "generation inventory contains a linked or irregular file"


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
                # `is_file()` swallows the OSError and answers False, so a path the
                # platform cannot open is indistinguishable here from an irregular one.
                # On Windows a generated artifact path can exceed MAX_PATH while its
                # directory listing still names it -- reporting that as a linked file sends
                # the reader looking for a symlink that does not exist, which is exactly
                # what happened when a decomposition published at 261 characters.
                raise GenerationConflictError(
                    _inventory_file_refusal(path, directory, name)
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
    destination = Path(_windows_api_path(path)) if os.name == "nt" else path
    with destination.open("xb") as handle:
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
    if pointer.pointer_schema_version == _LEGACY_POINTER_SCHEMA_VERSION:
        raise PointerIntegrityError(
            "legacy pointer schema is not accepted as current authority"
        )
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


# --- Release-validation-findings precondition ---------------------------------------
#
# Cross-lane contract with the `acc-validator` lane (`state/briefs/findings-contract.md`,
# corrective program): `tools/validate_generation.py` writes a findings document for a
# generation; release publication refuses to commit that generation unless the document
# exists, matches this contract, names exactly that generation, and carries zero
# `BLOCKER_*` findings. This lives here (not only in an operator script) because the
# refusal is meant to be a property of release publication itself -- every caller of
# `commit_release`, not one wrapper around it.

_RELEASE_VALIDATION_SCHEMA: Final = "qe-validate-generation/1"
_RELEASE_VALIDATION_BLOCKER_PREFIX: Final = "BLOCKER_"
_RELEASE_VALIDATION_FINDING_CLASSES: Final = frozenset(
    {
        "BLOCKER_INVARIANT",
        "BLOCKER_AUTHORITY",
        "BLOCKER_REGRESSION",
        "NOT_READY",
        "WARNING",
    }
)
_RELEASE_VALIDATION_FINDING_KEYS: Final = frozenset(
    {"check_id", "class", "entity", "observed", "expected", "pointer", "defect_ids"}
)
_RELEASE_VALIDATION_DOCUMENT_KEYS: Final = frozenset(
    {
        "schema",
        "generation_id",
        "generation_dir",
        "validator_commit",
        "expected_baseline_sha256",
        "summary",
        "findings",
    }
)

# --- Release-findings-set precondition (ruling B, findings-contract.md) -------------
#
# `tools/validate_generation.py` judges the one generation a user reads (the panel
# target, `_RELEASE_VALIDATION_SCHEMA` above). `tools/validate_release_targets.py`
# derives a narrower binding judgement for every *other* release target, schema
# `qe-release-findings/1`: is this target's generation exactly the input the panel's
# own (already-clean) findings cite? Its document carries every key the panel schema
# does, plus `panel_validation` naming the panel target and the exact findings bytes
# (by sha256) it was derived against -- so a re-validated panel (new findings bytes)
# invalidates every derived document still citing the old digest.

_RELEASE_FINDINGS_SCHEMA: Final = "qe-release-findings/1"
_RELEASE_FINDINGS_PANEL_VALIDATION_KEYS: Final = frozenset(
    {"generation_id", "findings_sha256"}
)
_RELEASE_FINDINGS_DOCUMENT_KEYS: Final = frozenset(
    {*_RELEASE_VALIDATION_DOCUMENT_KEYS, "panel_validation"}
)

# Windows FILE_ATTRIBUTE_* flags for cloud-sync (OneDrive/SharePoint Files-On-Demand)
# placeholders: metadata only, no local bytes. Reading one hydrates it through the sync
# provider's filter driver; AGENT-CONSTRAINTS requires checking residency first (`stat`
# never hydrates) and refusing rather than reading through a placeholder.
_CLOUD_PLACEHOLDER_ATTRIBUTES: Final = 0x00001000 | 0x00040000 | 0x00400000


class ReleaseValidationFindingsError(ValueError):
    """A validate_generation findings document does not match the cross-lane contract."""


def _is_cloud_placeholder(path: Path) -> bool:
    try:
        attributes = path.stat().st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & _CLOUD_PLACEHOLDER_ATTRIBUTES)


def default_release_validation_findings_path(root: Path, generation_id: str) -> Path:
    """Where `validate_generation` writes findings for a generation under ``root``.

    A ``validation/`` sibling of ``generations/``, per the cross-lane contract: the
    validator never writes inside a sealed, content-addressed generation directory.
    """
    return root / "validation" / generation_id / "findings.json"


def _release_validation_required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseValidationFindingsError(f"{label} must be a non-empty string")
    return value


def _release_validation_finding(entry: object, *, index: int) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise ReleaseValidationFindingsError(f"findings[{index}] is not an object")
    if set(entry) != _RELEASE_VALIDATION_FINDING_KEYS:
        raise ReleaseValidationFindingsError(f"findings[{index}] shape differs")
    _release_validation_required_string(
        entry["check_id"], f"findings[{index}].check_id"
    )
    finding_class = entry["class"]
    if (
        not isinstance(finding_class, str)
        or finding_class not in _RELEASE_VALIDATION_FINDING_CLASSES
    ):
        raise ReleaseValidationFindingsError(
            f"findings[{index}].class is not a recognized class"
        )
    _release_validation_required_string(entry["entity"], f"findings[{index}].entity")
    _release_validation_required_string(entry["pointer"], f"findings[{index}].pointer")
    defect_ids = entry["defect_ids"]
    if not isinstance(defect_ids, list) or not all(
        isinstance(item, str) for item in defect_ids
    ):
        raise ReleaseValidationFindingsError(
            f"findings[{index}].defect_ids must be a list of strings"
        )
    return entry


def _validated_findings_core(
    payload: dict[str, object], *, schema: str, keys: frozenset[str]
) -> dict[str, object]:
    """The validation shared by every findings document schema (panel or derived).

    Factored out of the original single-schema `_release_validation_document` so
    `_release_findings_document` (schema `qe-release-findings/1`, ruling B) can
    reuse the exact same field-by-field checks instead of re-deriving them.
    """
    if set(payload) != keys:
        raise ReleaseValidationFindingsError("findings document shape differs")
    if payload["schema"] != schema:
        raise ReleaseValidationFindingsError("findings document schema differs")
    _release_validation_required_string(payload["generation_id"], "generation_id")
    _release_validation_required_string(payload["generation_dir"], "generation_dir")
    _release_validation_required_string(payload["validator_commit"], "validator_commit")
    baseline = payload["expected_baseline_sha256"]
    if baseline is not None and not (isinstance(baseline, str) and baseline):
        raise ReleaseValidationFindingsError(
            "expected_baseline_sha256 must be a string or null"
        )
    summary = payload["summary"]
    if (
        not isinstance(summary, dict)
        or set(summary) != _RELEASE_VALIDATION_FINDING_CLASSES
    ):
        raise ReleaseValidationFindingsError("summary shape differs")
    for key, value in summary.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReleaseValidationFindingsError(
                f"summary.{key} must be a non-negative integer"
            )
    findings_list = payload["findings"]
    if not isinstance(findings_list, list):
        raise ReleaseValidationFindingsError("findings must be a list")
    findings = [
        _release_validation_finding(entry, index=index)
        for index, entry in enumerate(findings_list)
    ]
    counted = {key: 0 for key in _RELEASE_VALIDATION_FINDING_CLASSES}
    for entry in findings:
        counted[cast(str, entry["class"])] += 1
    if counted != summary:
        raise ReleaseValidationFindingsError("summary disagrees with findings")
    return payload


def _release_validation_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ReleaseValidationFindingsError("findings document shape differs")
    return _validated_findings_core(
        payload,
        schema=_RELEASE_VALIDATION_SCHEMA,
        keys=_RELEASE_VALIDATION_DOCUMENT_KEYS,
    )


def _release_findings_panel_validation(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != _RELEASE_FINDINGS_PANEL_VALIDATION_KEYS
    ):
        raise ReleaseValidationFindingsError("panel_validation shape differs")
    _release_validation_required_string(
        value["generation_id"], "panel_validation.generation_id"
    )
    _release_validation_required_string(
        value["findings_sha256"], "panel_validation.findings_sha256"
    )
    return value


def _release_findings_document(payload: object) -> dict[str, object]:
    """Validate a derived (non-panel) target's ``qe-release-findings/1`` document."""
    if not isinstance(payload, dict):
        raise ReleaseValidationFindingsError("findings document shape differs")
    document = _validated_findings_core(
        payload, schema=_RELEASE_FINDINGS_SCHEMA, keys=_RELEASE_FINDINGS_DOCUMENT_KEYS
    )
    document["panel_validation"] = _release_findings_panel_validation(
        document["panel_validation"]
    )
    return document


def _load_findings_file(path: Path) -> tuple[bytes | None, str | None]:
    """``(raw_bytes, None)`` or ``(None, refusal_reason)`` -- the IO/residency axes
    shared by every findings document, panel or derived (ruling B)."""
    if not path.is_file():
        return None, f"no release-validation findings file at {path}"
    if _is_cloud_placeholder(path):
        return (
            None,
            (
                f"release-validation findings file is not resident (cloud placeholder): {path}"
            ),
        )
    try:
        return path.read_bytes(), None
    except OSError as exc:
        return None, f"release-validation findings file cannot be read: {exc}"


def _evaluate_findings_document(
    document: dict[str, object], *, expected_generation_id: str
) -> tuple[bool, list[dict[str, object]], str | None]:
    """The checks shared by a panel (``/1``) and a derived (``qe-release-findings/1``)
    document once its shape is already known-good: generation identity, a recorded
    expected-baseline digest, and zero ``BLOCKER_*`` findings."""
    if document["generation_id"] != expected_generation_id:
        return (
            False,
            [],
            (
                "release-validation findings generation_id differs: expected "
                f"{expected_generation_id!r}, found {document['generation_id']!r}"
            ),
        )
    if document["expected_baseline_sha256"] is None:
        return (
            False,
            [],
            (
                "release-validation findings expected_baseline_sha256 is null "
                "(the validator ran without --expected, so no fixed-baseline "
                "regression comparison occurred); a generation cannot be "
                "authorized for publication without one"
            ),
        )
    findings = cast(list[dict[str, object]], document["findings"])
    blockers = [
        entry
        for entry in findings
        if cast(str, entry["class"]).startswith(_RELEASE_VALIDATION_BLOCKER_PREFIX)
    ]
    return not blockers, blockers, None


def read_release_validation_findings_bytes(
    path: Path,
) -> tuple[bytes | None, str | None]:
    """Public ``(raw_bytes, None)`` / ``(None, refusal_reason)`` IO/residency read.

    Shared, single-read entry point for a findings file's bytes (ruling B):
    a caller that must both validate a findings document and separately bind
    its sha256 (e.g. ``tools/validate_release_targets.py``, deriving every
    other target's ``panel_validation.findings_sha256``) reads the bytes
    exactly once here and reuses them for both, rather than reading the path
    a second time -- a second read is a TOCTOU window in which the file could
    change between the validated bytes and the hashed bytes.
    """
    return _load_findings_file(path)


def evaluate_release_validation_findings_bytes(
    raw: bytes, *, expected_generation_id: str
) -> tuple[bool, list[dict[str, object]], str | None]:
    """Like :func:`evaluate_release_validation_findings`, given bytes already read
    (via :func:`read_release_validation_findings_bytes`) instead of a path."""
    try:
        payload = strict_json_object(raw, label="release-validation findings")
        document = _release_validation_document(payload)
    except (StrictJSONError, ReleaseValidationFindingsError) as exc:
        return False, [], str(exc)
    return _evaluate_findings_document(
        document, expected_generation_id=expected_generation_id
    )


def evaluate_release_validation_findings(
    path: Path, *, expected_generation_id: str
) -> tuple[bool, list[dict[str, object]], str | None]:
    """Return ``(may_publish, blocking_findings, refusal_reason)`` for one generation.

    Fails closed on every axis the contract names: missing, unreadable, a
    non-resident cloud placeholder, malformed, naming a different generation,
    or declaring no expected baseline (``expected_baseline_sha256: null``, the
    shape a validator run without ``--expected`` produces) all refuse. Only a
    well-formed document for exactly ``expected_generation_id``, with a
    recorded expected-baseline digest and zero ``BLOCKER_*`` findings, may
    publish. ``refusal_reason`` is set only for those non-finding refusals; a
    well-formed document with blockers returns them instead so the caller can
    report exactly what refused it.
    """
    raw, reason = _load_findings_file(path)
    if reason is not None:
        return False, [], reason
    assert raw is not None
    return evaluate_release_validation_findings_bytes(
        raw, expected_generation_id=expected_generation_id
    )


def evaluate_findings_document_at_path(
    path: Path, *, expected_generation_id: str
) -> tuple[bool, list[dict[str, object]], str | None]:
    """Like ``evaluate_release_validation_findings``, but also accepts a single
    ``qe-release-findings/1`` document (ruling B).

    ``tools/check_release_validation_findings.py`` checks one findings file at a
    time, by path -- a standalone, operator-facing echo of one axis
    ``evaluate_release_findings_set`` enforces at the publication sink. For a
    panel (``/1``) document the two agree exactly. For a derived
    (``qe-release-findings/1``) document this function validates its own shape
    and blockers the same way, but -- having only one path, not the full
    manifest ``evaluate_release_findings_set`` needs -- it cannot check whether
    its ``panel_validation`` still matches the release's current panel findings;
    that cross-target staleness check is only possible at the publication sink,
    which sees every target's path at once.
    """
    raw, reason = _load_findings_file(path)
    if reason is not None:
        return False, [], reason
    assert raw is not None
    try:
        payload = strict_json_object(raw, label="release-validation findings")
    except StrictJSONError as exc:
        return False, [], str(exc)
    schema = payload.get("schema") if isinstance(payload, dict) else None
    try:
        if schema == _RELEASE_FINDINGS_SCHEMA:
            document = _release_findings_document(payload)
        else:
            document = _release_validation_document(payload)
    except ReleaseValidationFindingsError as exc:
        return False, [], str(exc)
    return _evaluate_findings_document(
        document, expected_generation_id=expected_generation_id
    )


class _FindingsTarget(Protocol):
    """Duck-typed subset of ``release.ReleaseTarget`` this module needs.

    Kept as a structural `Protocol` (not an import of `release.ReleaseTarget`)
    because `release.py` imports this module -- importing it back here would be
    circular.
    """

    @property
    def root_id(self) -> str: ...

    @property
    def proposed_generation_id(self) -> str: ...


def evaluate_release_findings_set(
    manifest_targets: Sequence[_FindingsTarget], paths: Mapping[str, Path]
) -> tuple[bool, dict[str, str | None]]:
    """``(may_publish, per_root_reason)`` for every target of one release (ruling B).

    Accepts both findings schemas: ``qe-validate-generation/1`` (the panel's own
    judged document) and ``qe-release-findings/1`` (every other target's derived
    binding document, from ``tools/validate_release_targets.py``). Every refusal
    axis ``evaluate_release_validation_findings`` already enforces for a single
    panel document applies identically here, per target, plus two more: at least
    one target in this manifest must carry a clean ``/1`` document -- the judged
    surface itself must be clean for anything to bind to -- and every
    ``qe-release-findings/1`` document's ``panel_validation`` must name a target
    of *this* manifest whose own clean ``/1`` document's bytes hash to exactly
    ``panel_validation.findings_sha256``. A re-validated panel (new findings
    bytes, hence a new sha256) therefore invalidates every derived document
    still citing the old digest; a stale derived document refuses.

    ``per_root_reason[root_id]`` is ``None`` for a clean, bound root, else its
    refusal reason or a ``blocking findings [...]`` summary, matching the text
    a single-target caller would have reported.
    """
    per_root_reason: dict[str, str | None] = {}
    clean_documents: dict[str, dict[str, object]] = {}
    clean_raw_by_root: dict[str, bytes] = {}

    for target in manifest_targets:
        root_id = target.root_id
        path = paths[root_id]
        raw, reason = _load_findings_file(path)
        if reason is not None:
            per_root_reason[root_id] = reason
            continue
        assert raw is not None
        try:
            payload = strict_json_object(raw, label="release-validation findings")
        except StrictJSONError as exc:
            per_root_reason[root_id] = str(exc)
            continue
        schema = payload.get("schema") if isinstance(payload, dict) else None
        try:
            if schema == _RELEASE_VALIDATION_SCHEMA:
                document = _release_validation_document(payload)
            elif schema == _RELEASE_FINDINGS_SCHEMA:
                document = _release_findings_document(payload)
            else:
                raise ReleaseValidationFindingsError("findings document schema differs")
        except ReleaseValidationFindingsError as exc:
            per_root_reason[root_id] = str(exc)
            continue
        may_publish, blockers, reason = _evaluate_findings_document(
            document, expected_generation_id=target.proposed_generation_id
        )
        if not may_publish:
            if reason is not None:
                per_root_reason[root_id] = reason
            else:
                summary = ", ".join(
                    f"{entry['class']}:{entry['check_id']}" for entry in blockers
                )
                per_root_reason[root_id] = f"blocking findings [{summary}]"
            continue
        per_root_reason[root_id] = None
        clean_documents[root_id] = document
        clean_raw_by_root[root_id] = raw

    clean_panel_root_ids = [
        root_id
        for root_id, document in clean_documents.items()
        if document["schema"] == _RELEASE_VALIDATION_SCHEMA
    ]
    if len(clean_panel_root_ids) > 1:
        # GLM (publication.py:2193): the clean `/1` anchor every derived
        # document binds to must be unambiguous. Without this, any root that
        # happens to also carry a clean qe-validate-generation/1 document
        # could serve as (or be confused for) the panel's own binding anchor.
        # Refuse outright -- a manifest with more than one clean judged
        # document has no single panel findings digest to bind to.
        reason = (
            "release validation refuses: more than one target in this release "
            "carries a clean judged (qe-validate-generation/1) findings "
            f"document ({', '.join(sorted(clean_panel_root_ids))}); exactly "
            "one /1 document per manifest is required"
        )
        for root_id in clean_panel_root_ids:
            per_root_reason[root_id] = reason
        return False, per_root_reason

    clean_panel_by_generation_id: dict[str, str] = {
        cast(str, document["generation_id"]): hashlib.sha256(
            clean_raw_by_root[root_id]
        ).hexdigest()
        for root_id, document in clean_documents.items()
        if document["schema"] == _RELEASE_VALIDATION_SCHEMA
    }
    judged_clean = bool(clean_panel_by_generation_id)

    for root_id, document in clean_documents.items():
        if document["schema"] != _RELEASE_FINDINGS_SCHEMA:
            continue
        panel_validation = cast(dict[str, object], document["panel_validation"])
        panel_generation_id = cast(str, panel_validation["generation_id"])
        panel_findings_sha256 = clean_panel_by_generation_id.get(panel_generation_id)
        if panel_findings_sha256 is None or (
            panel_findings_sha256 != panel_validation["findings_sha256"]
        ):
            per_root_reason[root_id] = (
                "release findings panel_validation is stale: it names panel "
                f"generation {panel_generation_id!r} with findings_sha256 "
                f"{panel_validation['findings_sha256']!r}, which is not this "
                "manifest's current clean panel findings"
            )

    if not judged_clean:
        return False, per_root_reason
    may_publish = all(reason is None for reason in per_root_reason.values())
    return may_publish, per_root_reason

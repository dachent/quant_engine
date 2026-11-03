"""Create-only raw staging under a caller-supplied root.

Every file is created exclusively (``O_EXCL``), fsynced, and recorded with its
SHA-256 and size.  A run directory is created once and is never reused.  The
final manifest lists every other file in the run directory and is itself
created exclusively; nothing is ever overwritten, appended to, or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from ._util import canonical_json_bytes, sha256_hex

MANIFEST_NAME = "capture-manifest.json"
_REPARSE_POINT = 0x0400
_RUN_ID = re.compile(r"^flex-\d{8}T\d{6}Z-[0-9a-f]{8}-\d{2}$")
_FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class StagingError(RuntimeError):
    """A staging request would overwrite, escape, or follow a link."""


def _is_link(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise StagingError("a staging path cannot be inspected") from None
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


@dataclass(frozen=True)
class FileRecord:
    name: str
    sha256: str
    bytes: int

    def row(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "bytes": self.bytes}


class RunDirectory:
    """One create-only run directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, FileRecord] = {}
        self._finalized = False

    @property
    def records(self) -> tuple[FileRecord, ...]:
        return tuple(self._records[name] for name in sorted(self._records))

    def write_new(self, name: str, data: bytes) -> FileRecord:
        if self._finalized:
            raise StagingError("the run directory is already finalized")
        if _FILE_NAME.fullmatch(name) is None or name == MANIFEST_NAME:
            raise StagingError("the staged file name is not allowed")
        if name in self._records:
            raise StagingError("a staged file is never overwritten")
        target = self.path / name
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            raise StagingError("a staged file is never overwritten") from None
        except OSError:
            raise StagingError("a staged file could not be created") from None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        record = FileRecord(name, sha256_hex(data), len(data))
        self._records[name] = record
        return record

    def finalize(self, manifest: dict[str, Any]) -> FileRecord:
        """Create the hash-bound manifest listing every staged file."""

        if "files" in manifest:
            raise StagingError("the manifest file list is derived, not supplied")
        document = dict(manifest)
        document["files"] = [record.row() for record in self.records]
        record = self.write_new_manifest(canonical_json_bytes(document))
        self._finalized = True
        return record

    def write_new_manifest(self, data: bytes) -> FileRecord:
        target = self.path / MANIFEST_NAME
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            raise StagingError("a manifest is never overwritten") from None
        except OSError:
            raise StagingError("the manifest could not be created") from None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return FileRecord(MANIFEST_NAME, sha256_hex(data), len(data))


class RawStage:
    """A staging root supplied by the caller; the driver never invents one."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise StagingError("the staging root must be an absolute path")
        if not root.is_dir() or _is_link(root):
            raise StagingError("the staging root must be a plain existing directory")
        self.root = root

    def open_run(self, run_id: str) -> RunDirectory:
        if _RUN_ID.fullmatch(run_id) is None:
            raise StagingError("the run id is not valid")
        path = self.root / run_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            raise StagingError("a run directory is never reused") from None
        except OSError:
            raise StagingError("the run directory could not be created") from None
        return RunDirectory(path)


def write_new_file(path: Path, data: bytes) -> FileRecord:
    """Create one standalone file exclusively (for normalized output)."""

    if path.parent.is_symlink() or not path.parent.is_dir():
        raise StagingError("the output directory must be a plain existing directory")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        raise StagingError("an output file is never overwritten") from None
    except OSError:
        raise StagingError("the output file could not be created") from None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return FileRecord(path.name, sha256_hex(data), len(data))

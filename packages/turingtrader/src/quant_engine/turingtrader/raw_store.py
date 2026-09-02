"""Create-only run storage for raw bytes and sanitized metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .errors import RawStoreError


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_run_dir(run_dir: Path) -> None:
    """Create the run directory once; an existing directory fails the run."""
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise RawStoreError(f"run directory already exists: {run_dir}") from None
    except OSError as error:
        raise RawStoreError(f"cannot create run directory: {run_dir}") from error


def write_create_only(path: Path, data: bytes) -> None:
    """Persist ``data`` at ``path``; never overwrite an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise RawStoreError(f"immutable file already exists: {path}") from None


def assert_redacted(data: bytes, token_values: Iterable[str]) -> None:
    """Fail the run if any configured token value leaks into metadata."""
    text = data.decode("utf-8", errors="replace")
    for token in token_values:
        if token and token in text:
            raise RawStoreError("token value leaked into persisted metadata")


def write_metadata(path: Path, value: Any, token_values: Iterable[str]) -> None:
    data = (canonical_json(value) + "\n").encode("utf-8")
    assert_redacted(data, token_values)
    write_create_only(path, data)

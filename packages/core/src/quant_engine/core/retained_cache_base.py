"""A sealed last-good retained-cache base with a sibling provenance file.

``promote_cache_base`` hard-links a finished attended-run overlay into a new base and
records, beside it (``<base>.provenance.json``), the commit the overlay's run was frozen at
and a sha256 manifest of every file. ``verify_cache_base`` proves the base is byte for byte
what was promoted; the attended runner calls ``verify_if_promoted`` before it links an
overlay to a base, so a promoted base that was modified refuses the run instead of
becoming the new baseline.

The provenance file is written before the base is published, so an interrupted promotion
is retried by running it again (the base does not exist yet; the stale provenance is
overwritten).
"""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import json

from .contracts import canonical_json_bytes
from .run_storage import OVERLAY_MARKER

PROVENANCE_KIND = "quant-engine-retained-cache-base"


class CacheBaseError(RuntimeError):
    """A named refusal."""


def _long(path: Path | str) -> str:
    text = os.path.abspath(path)
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def iter_cache_files(root: Path) -> Iterator[tuple[str, str, os.stat_result]]:
    """(relative posix path, absolute path, lstat) of every regular file; links skipped."""

    stack = [("", _long(root))]
    while stack:
        rel, cur = stack.pop()
        with os.scandir(cur) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            info = os.lstat(entry.path)
            child = f"{rel}/{entry.name}" if rel else entry.name
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 0x400
            ):
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append((child, entry.path))
            elif stat.S_ISREG(info.st_mode):
                yield child, entry.path, info


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_base_manifest(root: Path) -> dict[str, Any]:
    rows: list[list[Any]] = [
        [rel, info.st_size, _sha256(path)]
        for rel, path, info in iter_cache_files(root)
        if rel != OVERLAY_MARKER
    ]
    return {
        "bytes": sum(int(r[1]) for r in rows),
        "files": len(rows),
        "sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
    }


def provenance_path(base: Path) -> Path:
    return base.with_name(base.name + ".provenance.json")


def _remove(path: Path) -> None:
    if not os.path.isdir(_long(path)):
        return
    for root, dirs, files in os.walk(_long(path), topdown=False):
        for name in files:
            full = os.path.join(root, name)
            os.chmod(full, stat.S_IWUSR | stat.S_IRUSR)
            os.remove(full)
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    os.rmdir(_long(path))


def promote_cache_base(overlay: Path, base: Path, source_commit: str) -> dict[str, Any]:
    if len(source_commit) != 40 or any(
        c not in "0123456789abcdef" for c in source_commit
    ):
        raise CacheBaseError("source commit must be a 40-hex commit")
    if not (overlay / OVERLAY_MARKER).is_file():
        raise CacheBaseError(
            f"not a retained-cache overlay (no {OVERLAY_MARKER}): {overlay}"
        )
    if base.exists():
        raise CacheBaseError(f"base already exists, promote to a new path: {base}")
    staging = base.with_name(f".{base.name}.building-{os.getpid()}")
    try:
        os.makedirs(_long(staging))
        for rel, path, info in iter_cache_files(overlay):
            if rel == OVERLAY_MARKER:
                continue
            target = os.path.join(_long(staging), *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                os.link(path, target)
            except OSError as exc:
                raise CacheBaseError(
                    f"hard link failed ({exc}); the base must share the overlay's "
                    "volume, bytes are never copied"
                ) from exc
            if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                os.chmod(target, stat.S_IMODE(info.st_mode) & ~0o222)
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "kind": PROVENANCE_KIND,
            "manifest": cache_base_manifest(staging),
            "schema_version": 1,
            "source_commit": source_commit,
            "source_overlay": str(overlay.resolve()),
        }
        # Provenance first: a crash before the publish below leaves no base, so a retry
        # simply rewrites this file; a crash after leaves a complete, verifiable base.
        provenance_path(base).write_bytes(canonical_json_bytes(record))
        os.replace(_long(staging), _long(base))
    except BaseException:
        _remove(staging)
        raise
    return record


def read_cache_base_provenance(base: Path) -> dict[str, Any]:
    path = provenance_path(base)
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CacheBaseError(f"base has no readable provenance file: {path}") from exc
    if record.get("kind") != PROVENANCE_KIND or record.get("schema_version") != 1:
        raise CacheBaseError(f"base provenance differs: {path}")
    return dict(record)


def verify_cache_base(base: Path) -> dict[str, Any]:
    record = read_cache_base_provenance(base)
    if cache_base_manifest(base) != record["manifest"]:
        raise CacheBaseError(
            "base bytes differ from its provenance manifest; do not seed a run from it"
        )
    return record


def verify_if_promoted(base: Path) -> dict[str, Any] | None:
    """Verify ``base`` when it carries provenance (a promoted base); other bases pass."""

    if not provenance_path(base).is_file():
        return None
    return verify_cache_base(base)

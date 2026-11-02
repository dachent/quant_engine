"""Hash many regular files concurrently, returning results in input order.

Digests and sizes are exactly what a serial read-and-hash loop returns; only the
wall time changes. ``hashlib`` releases the GIL while it digests a block, so
threads overlap both the digest and the (often cloud-synced) file reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path

from quant_engine.core import stage_timing

WORKERS_ENV = "QE_HASH_WORKERS"
_MAX_WORKERS = 16
_MIN_PARALLEL_FILES = 8


def hash_worker_count() -> int:
    raw = os.environ.get(WORKERS_ENV)
    if raw is None:
        return min(8, max(1, (os.cpu_count() or 2) // 2))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{WORKERS_ENV} must be an integer: {raw!r}") from exc
    if not 1 <= value <= _MAX_WORKERS:
        raise ValueError(f"{WORKERS_ENV} must be 1..{_MAX_WORKERS}: {value}")
    return value


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    stage_timing.note_hash(size)
    return digest.hexdigest(), size


def hash_files(paths: Sequence[Path]) -> list[tuple[str, int]]:
    """Return ``(sha256, size)`` for each path, in the order given.

    The first failure (in input order) is raised, as a serial loop would.
    """
    workers = hash_worker_count()
    if workers == 1 or len(paths) < _MIN_PARALLEL_FILES:
        return [hash_file(path) for path in paths]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(hash_file, paths))

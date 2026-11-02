"""Stage wall times and work counters for the release ceremony.

Timings are observations about a run, never inputs to it: they go to stderr and,
when ``QE_STAGE_TIMING_LOG`` names a file, to that JSON-lines file, and nothing
here is hashed into a generation, receipt or journal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import functools
import json
import os
import sys
import threading
import time
from typing import ParamSpec, TypeVar

TIMING_LOG_ENV = "QE_STAGE_TIMING_LOG"

_lock = threading.Lock()
_counters = {"hash_calls": 0, "hash_bytes": 0}

P = ParamSpec("P")
R = TypeVar("R")


def note_hash(size_bytes: int) -> None:
    """Count one whole-file read-and-hash of ``size_bytes``."""
    with _lock:
        _counters["hash_calls"] += 1
        _counters["hash_bytes"] += size_bytes


def counters() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def _emit(record: dict[str, object]) -> None:
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    print("timing " + line, file=sys.stderr, flush=True)
    path = os.environ.get(TIMING_LOG_ENV)
    if path:
        try:
            with _lock, open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


@contextlib.contextmanager
def stage(name: str, **fields: object) -> Iterator[dict[str, object]]:
    """Time one stage; the body may add result fields to the yielded dict."""
    extra: dict[str, object] = {}
    before = counters()
    started = time.monotonic()
    try:
        yield extra
    finally:
        after = counters()
        _emit(
            {
                "stage": name,
                "seconds": round(time.monotonic() - started, 3),
                "hash_calls": after["hash_calls"] - before["hash_calls"],
                "hash_bytes": after["hash_bytes"] - before["hash_bytes"],
                "pid": os.getpid(),
                **fields,
                **extra,
            }
        )


def timed(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Time every call of the decorated function as stage ``name``."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with stage(name):
                return function(*args, **kwargs)

        return wrapper

    return decorate

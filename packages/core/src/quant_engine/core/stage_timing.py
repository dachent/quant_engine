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
                "t_end": round(time.time(), 3),
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


VERIFIER_STAGES = ("financial_outcomes", "product_authority")


def summarize(
    path: str | os.PathLike[str],
    started: float,
    ended: float,
    *,
    silent_over: float = 60.0,
) -> dict[str, object]:
    """Digest one stage's timing log: verifier call counts and silent intervals.

    A silent interval is a stretch of ``[started, ended]`` (epoch seconds) that no
    logged stage, in any process of the stage, covers. Observation only.
    """

    rows: list[dict[str, object]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    calls = {name: 0 for name in VERIFIER_STAGES}
    spans: list[tuple[float, float]] = []
    for row in rows:
        if row.get("stage") in calls:
            calls[str(row["stage"])] += 1
        end, seconds = row.get("t_end"), row.get("seconds")
        if isinstance(end, (int, float)) and isinstance(seconds, (int, float)):
            spans.append((float(end) - float(seconds), float(end)))
    spans.sort()
    gaps: list[float] = []
    cursor = started
    for begin, end in spans:
        if begin > cursor:
            gaps.append(begin - cursor)
        cursor = max(cursor, end)
    if ended > cursor:
        gaps.append(ended - cursor)
    silent = [round(gap, 1) for gap in gaps if gap > silent_over]
    return {
        "logged_stages": len(rows),
        "verifier_calls": calls,
        "silent_intervals_over_limit": silent,
        "max_silent_seconds": round(max(gaps), 1) if gaps else 0.0,
    }

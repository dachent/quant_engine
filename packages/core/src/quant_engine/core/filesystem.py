"""Bounded filesystem operations for synchronized Windows worktrees."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Final, Sequence


SYNC_RENAME_RETRY_DELAYS: Final = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def rename_directory_with_retry(
    source: Path,
    destination: Path,
    *,
    retry_delays: Sequence[float] = SYNC_RENAME_RETRY_DELAYS,
) -> None:
    """Atomically rename a directory with bounded sync-provider lock retries."""

    for attempt in range(len(retry_delays) + 1):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == len(retry_delays):
                raise
            time.sleep(retry_delays[attempt])

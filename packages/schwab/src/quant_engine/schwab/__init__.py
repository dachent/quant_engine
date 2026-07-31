"""Read-only Schwab current-state, history, and generation contracts."""

from quant_engine.schwab.api import (
    CurrentAccount,
    CurrentSnapshot,
    SchwabReadError,
    current_snapshot_bytes,
    read_current_snapshot,
    write_current_snapshot,
)
from quant_engine.schwab.generation import (
    SchwabGenerationError,
    build_generation,
    stage_generation_candidate,
)

__version__ = "0.2.0"

__all__ = [
    "CurrentAccount",
    "CurrentSnapshot",
    "SchwabGenerationError",
    "SchwabReadError",
    "build_generation",
    "current_snapshot_bytes",
    "read_current_snapshot",
    "stage_generation_candidate",
    "write_current_snapshot",
]

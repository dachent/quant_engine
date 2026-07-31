"""Read-only Schwab history and generation contracts."""

from quant_engine.schwab.api import (
    CurrentAccount,
    CurrentSnapshot,
    SchwabReadError,
    read_current_snapshot,
)
from quant_engine.schwab.generation import (
    SchwabGenerationError,
    build_generation,
    stage_generation_candidate,
)

__version__ = "0.1.0"

__all__ = [
    "CurrentAccount",
    "CurrentSnapshot",
    "SchwabGenerationError",
    "SchwabReadError",
    "build_generation",
    "read_current_snapshot",
    "stage_generation_candidate",
]

"""Read-only Schwab current-state, history, and generation contracts."""

from quant_engine.schwab.api import (
    CurrentAccount,
    CurrentSnapshot,
    SchwabReadError,
    current_snapshot_bytes,
    read_current_snapshot,
    read_holdings_v2_current_snapshot,
    read_web_current_snapshot,
    write_current_snapshot,
)
from quant_engine.schwab.derived_positions import (
    DERIVED_BASIS,
    SchwabDerivedPositionError,
    admit_derived_position_rows,
)
from quant_engine.schwab.generation import (
    SchwabGenerationError,
    build_generation,
    publish_generation_candidate,
    stage_generation_candidate,
)

__version__ = "0.3.0"

__all__ = [
    "DERIVED_BASIS",
    "CurrentAccount",
    "CurrentSnapshot",
    "SchwabDerivedPositionError",
    "SchwabGenerationError",
    "SchwabReadError",
    "admit_derived_position_rows",
    "build_generation",
    "current_snapshot_bytes",
    "publish_generation_candidate",
    "read_current_snapshot",
    "read_holdings_v2_current_snapshot",
    "read_web_current_snapshot",
    "stage_generation_candidate",
    "write_current_snapshot",
]

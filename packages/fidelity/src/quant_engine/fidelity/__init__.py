"""Finite, static Fidelity activity ingestion contracts."""

from quant_engine.fidelity.generation import (
    FidelityGenerationError,
    build_generation,
    stage_generation_candidate,
)
from quant_engine.fidelity.intake import (
    IntakeError,
    REPORT_CATALOG,
    catalog_rows,
    normalize,
    stage_raw,
    write_request,
)
from quant_engine.fidelity.intake_verify import IntakeVerificationError, verify_intake

__version__ = "0.1.0"

__all__ = [
    "FidelityGenerationError",
    "IntakeError",
    "IntakeVerificationError",
    "REPORT_CATALOG",
    "build_generation",
    "catalog_rows",
    "normalize",
    "stage_generation_candidate",
    "stage_raw",
    "verify_intake",
    "write_request",
]

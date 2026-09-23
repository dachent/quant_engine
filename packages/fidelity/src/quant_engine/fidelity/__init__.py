"""Finite, static Fidelity activity ingestion contracts."""

from quant_engine.fidelity.generation import (
    FidelityGenerationError,
    build_generation,
    stage_generation_candidate,
)

__version__ = "0.1.0"

__all__ = [
    "FidelityGenerationError",
    "build_generation",
    "stage_generation_candidate",
]

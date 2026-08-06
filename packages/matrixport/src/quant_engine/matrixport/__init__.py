"""Read-only Matrixport data extraction, export, and frozen-run acceptance."""

from quant_engine.matrixport.generation import (
    MatrixportGenerationError,
    build_generation,
    draft_generation_spec,
    stage_generation_candidate,
)

__version__ = "0.2.1"

__all__ = [
    "MatrixportGenerationError",
    "build_generation",
    "draft_generation_spec",
    "stage_generation_candidate",
]

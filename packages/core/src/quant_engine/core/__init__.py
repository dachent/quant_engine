"""Minimal provenance and generation envelope for quant_engine."""

from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    ManifestValidationError,
    SourceArtifact,
    VerificationStatus,
    hash_file,
)
from quant_engine.core.publication import (
    CurrentPointer,
    GenerationConflictError,
    PointerIntegrityError,
    PublicationRejectedError,
    PublicationState,
    publication_state,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)

__version__ = "0.1.0"

__all__ = [
    "AdapterIdentity",
    "CoverageClaim",
    "CoverageStatus",
    "CurrentPointer",
    "GenerationConflictError",
    "GenerationManifest",
    "ManifestValidationError",
    "PointerIntegrityError",
    "PublicationRejectedError",
    "PublicationState",
    "SourceArtifact",
    "VerificationStatus",
    "hash_file",
    "publication_state",
    "publish_generation",
    "read_current",
    "read_generation",
    "stage_generation",
]

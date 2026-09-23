"""Web Contract Harvester: fail-closed offline web evidence handling."""

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)
from quant_engine.web_contracts.extract import (
    ExtractedResponse,
    REDACTION,
    SessionEnvelope,
    canonical_extraction,
    extract_har,
)
from quant_engine.web_contracts.provenance import SOURCE_ANCHORS
from quant_engine.web_contracts.portfolio123 import PORTFOLIO123_CONTRACT
from quant_engine.web_contracts.quantbase import (
    QUANTBASE_CONTRACT,
    extract_quantbase_har,
)
from quant_engine.web_contracts.schwab import SCHWAB_CONTRACT

__version__ = "0.1.0"

__all__ = [
    "EndpointContract",
    "ExtractedResponse",
    "ProviderContract",
    "PORTFOLIO123_CONTRACT",
    "QUANTBASE_CONTRACT",
    "REDACTION",
    "SCHWAB_CONTRACT",
    "SOURCE_ANCHORS",
    "SemanticEffect",
    "SessionEnvelope",
    "WebContractError",
    "canonical_extraction",
    "extract_har",
    "extract_quantbase_har",
]

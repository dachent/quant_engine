"""Web Contract Harvester: fail-closed offline web evidence handling."""

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)
from quant_engine.web_contracts.evidence import (
    HarExtractionReceipt,
    HarParseStatus,
    extract_har,
)
from quant_engine.web_contracts.provenance import RECOVERED_SOURCE_ANCHORS
from quant_engine.web_contracts.portfolio123 import PORTFOLIO123_CONTRACT
from quant_engine.web_contracts.quantbase import QUANTBASE_CONTRACT
from quant_engine.web_contracts.schwab import SCHWAB_CONTRACT
from quant_engine.web_contracts._version import VERSION

__version__ = VERSION

__all__ = [
    "EndpointContract",
    "HarExtractionReceipt",
    "HarParseStatus",
    "ProviderContract",
    "PORTFOLIO123_CONTRACT",
    "QUANTBASE_CONTRACT",
    "SCHWAB_CONTRACT",
    "RECOVERED_SOURCE_ANCHORS",
    "SemanticEffect",
    "WebContractError",
    "extract_har",
]

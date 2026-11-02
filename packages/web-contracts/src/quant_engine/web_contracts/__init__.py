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
from quant_engine.web_contracts.grizzlybulls import (
    GRIZZLYBULLS_CONTRACT,
    ORIGIN,
    SLUG_PATTERN,
    SignalSeries,
    build_contract,
    classify,
    parse_json_bytes,
    parse_trade_history_csv,
    sanitization_audit,
    summarize_capability,
)
from quant_engine.web_contracts.provenance import SOURCE_ANCHORS
from quant_engine.web_contracts.portfolio123 import PORTFOLIO123_CONTRACT
from quant_engine.web_contracts.quantbase import (
    QUANTBASE_CONTRACT,
    build_quantbase_contract,
    extract_quantbase_har,
)
from quant_engine.web_contracts.schwab import SCHWAB_CONTRACT

__version__ = "0.1.0"

__all__ = [
    "EndpointContract",
    "ExtractedResponse",
    "GRIZZLYBULLS_CONTRACT",
    "ORIGIN",
    "SLUG_PATTERN",
    "SignalSeries",
    "build_contract",
    "classify",
    "parse_json_bytes",
    "parse_trade_history_csv",
    "sanitization_audit",
    "summarize_capability",
    "ProviderContract",
    "PORTFOLIO123_CONTRACT",
    "QUANTBASE_CONTRACT",
    "REDACTION",
    "SCHWAB_CONTRACT",
    "SOURCE_ANCHORS",
    "SemanticEffect",
    "SessionEnvelope",
    "WebContractError",
    "build_quantbase_contract",
    "canonical_extraction",
    "extract_har",
    "extract_quantbase_har",
]

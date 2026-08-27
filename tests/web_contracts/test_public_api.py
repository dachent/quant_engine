from __future__ import annotations

import importlib

import pytest

import quant_engine.web_contracts as web_contracts
from quant_engine.web_contracts import evidence


def test_public_api_is_the_generalized_lossless_extractor_surface() -> None:
    assert web_contracts.__version__ == "0.2.0"
    assert web_contracts.extract_har is evidence.extract_har
    assert web_contracts.HarExtractionReceipt is evidence.HarExtractionReceipt
    assert web_contracts.HarParseStatus is evidence.HarParseStatus
    assert set(web_contracts.__all__) == {
        "EndpointContract",
        "HarExtractionReceipt",
        "HarParseStatus",
        "PORTFOLIO123_CONTRACT",
        "ProviderContract",
        "QUANTBASE_CONTRACT",
        "SCHWAB_CONTRACT",
        "RECOVERED_SOURCE_ANCHORS",
        "SemanticEffect",
        "WebContractError",
        "extract_har",
    }
    assert web_contracts.RECOVERED_SOURCE_ANCHORS == {
        "general-capture": "f609fb85e9eb41a05c3b4b14ac07ca519de39d53a0e875268ac57d479ab4fe8d",
        "general-extraction": "d4832db553155b8ee54a3fb100fdb76ad9d4fbbcf87bb8a878f2e61717a100cb",
        "general-runbook": "45ccbae0e5940d96ff3f50e5f233be284afe34ff0b04b107c22fc7215d099263",
    }
    for removed in (
        "ExtractedResponse",
        "SessionEnvelope",
        "REDACTION",
        "SOURCE_ANCHORS",
        "canonical_extraction",
        "extract_quantbase_har",
    ):
        assert not hasattr(web_contracts, removed)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("quant_engine.web_contracts.extract")

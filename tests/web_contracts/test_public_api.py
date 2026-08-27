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
    assert set(web_contracts.RECOVERED_SOURCE_ANCHORS) == {
        "general-capture",
        "general-extraction",
        "general-runbook",
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

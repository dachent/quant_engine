from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "strategy-return-sources-v1.json"
REGISTRY_PATH = (
    REPOSITORY_ROOT
    / "docs"
    / "analytics"
    / "current-strategy-decomposition-plan.json"
)
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "strategy-return-sources.schema.json"
)

VALID_SOURCE_TYPES = {
    "service_intermediation",
    "risk_premium",
    "information_edge",
    "structural_edge",
    "behavioral_constraint",
}

VALID_CONFIDENCE = {"strong", "moderate", "weak", "none"}

REQUIRED_STRATEGY_FIELDS = {
    "strategy_id",
    "sources",
    "no_identified_source",
    "mechanism",
    "persistence",
    "alternative_explanation",
    "falsification",
    "confidence",
}

REQUIRED_SOURCES_FIELDS = {"primary", "secondary"}

OUT_OF_SCOPE_STRATEGIES = {
    "p123-eu",
    "echotrade-growth-pivots",
    "composer-holy-grail",
}

ANALYZABLE_STRATEGIES = {
    "p123-ai-factor",
    "qsi",
    "tt-portfolio",
    "volatis-strategy",
    "grizzlybears-vtma",
    "robuxio-crypto",
}

TEXT_FIELDS = ["mechanism", "persistence", "alternative_explanation"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry_strategy_ids() -> list[str]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return [s["strategy_id"] for s in registry["strategies"]]


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _is_placeholder(text: str) -> bool:
    """True for values that look like stub or placeholder content."""
    stripped = text.strip()
    if stripped == "":
        return True
    if len(stripped) <= 2 and len(stripped) < len(text):
        return True
    return stripped in ("x", "X", "?", "TODO", "FIXME", "TBD", "N/A", "n/a")


def _assert_strategy_ids(contract: dict) -> None:
    registry_ids = _registry_strategy_ids()
    contract_ids = [s["strategy_id"] for s in contract["strategies"]]
    assert len(contract_ids) == 9, f"expected 9 strategies, got {len(contract_ids)}"
    assert contract_ids == registry_ids, (
        f"contract order {contract_ids} does not match registry order {registry_ids}"
    )


def _assert_required_fields_present(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy.get("strategy_id", "?")
        for field in REQUIRED_STRATEGY_FIELDS:
            assert field in strategy, f"strategy {sid} missing field: {field}"
        sources = strategy.get("sources", {})
        for field in REQUIRED_SOURCES_FIELDS:
            assert field in sources, f"strategy {sid} sources missing field: {field}"


def _assert_text_fields_valid(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        is_analyzable = sid in ANALYZABLE_STRATEGIES

        for field in TEXT_FIELDS:
            val = strategy[field]
            assert isinstance(val, str), (
                f"strategy {sid} field {field} is not a string"
            )
            stripped = val.strip()
            assert len(stripped) > 0, (
                f"strategy {sid} field {field} is empty or whitespace-only"
            )
            assert not _is_placeholder(val), (
                f"strategy {sid} field {field} is a placeholder: {val!r}"
            )

            if is_analyzable:
                assert len(stripped) >= 20, (
                    f"strategy {sid} field {field} is too short "
                    f"({len(stripped)} chars) for an analyzable strategy"
                )
                # analyzable strategies must never use N/A-style placeholders
                assert not val.lstrip().startswith("N/A"), (
                    f"strategy {sid} field {field} uses N/A placeholder: {val!r}"
                )


def _assert_confidence_valid(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        conf = strategy["confidence"]
        assert conf in VALID_CONFIDENCE, (
            f"strategy {sid} confidence {conf!r} not in {VALID_CONFIDENCE}"
        )
        if sid in ANALYZABLE_STRATEGIES:
            assert conf == "weak", (
                f"strategy {sid} should be 'weak', got {conf!r}"
            )
        else:
            assert conf == "none", (
                f"strategy {sid} should be 'none', got {conf!r}"
            )


def _assert_sources_valid(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        sources = strategy["sources"]
        for key in ("primary", "secondary"):
            val = sources[key]
            assert val is None or val in VALID_SOURCE_TYPES, (
                f"strategy {sid} sources.{key} {val!r} not valid"
            )


def _assert_consistency_rule(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        no_source = strategy["no_identified_source"]
        primary_null = strategy["sources"]["primary"] is None
        assert no_source == primary_null, (
            f"strategy {sid}: no_identified_source={no_source} but "
            f"sources.primary is {'null' if primary_null else 'not null'}"
        )
        if no_source:
            assert strategy["sources"]["secondary"] is None, (
                f"strategy {sid} has no_identified_source=true "
                f"but sources.secondary is not null"
            )


def _assert_falsification_valid(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        falsification = strategy["falsification"]
        assert isinstance(falsification, list) and len(falsification) >= 1, (
            f"strategy {sid} falsification must be non-empty array"
        )
        seen: set[str] = set()
        for item in falsification:
            assert isinstance(item, str) and item.strip(), (
                f"strategy {sid} falsification item empty or whitespace"
            )
            assert not _is_placeholder(item), (
                f"strategy {sid} falsification item is a placeholder: {item!r}"
            )
            assert item not in seen, (
                f"strategy {sid} falsification contains duplicate: {item!r}"
            )
            seen.add(item)


def _assert_valid_contract(contract: dict) -> None:
    """Run every positive assertion against *contract*.  Raises AssertionError on first
    failure, so the caller can wrap it in ``pytest.raises`` for negative tests."""
    assert isinstance(contract, dict)
    assert contract.get("schema_version") == 1
    assert "strategies" in contract

    _assert_strategy_ids(contract)
    _assert_required_fields_present(contract)
    _assert_text_fields_valid(contract)
    _assert_confidence_valid(contract)
    _assert_sources_valid(contract)
    _assert_consistency_rule(contract)
    _assert_falsification_valid(contract)


# ---------------------------------------------------------------------------
# Positive assertions (real contract)
# ---------------------------------------------------------------------------

def test_contract_and_schema_exist() -> None:
    assert CONTRACT_PATH.is_file(), f"contract not found at {CONTRACT_PATH}"
    assert SCHEMA_PATH.is_file(), f"schema not found at {SCHEMA_PATH}"


def test_contract_loads_valid_json() -> None:
    contract = _contract()
    assert isinstance(contract, dict)
    assert contract.get("schema_version") == 1
    assert "strategies" in contract


def test_positive_full_validity() -> None:
    """The real contract must pass every assertion."""
    _assert_valid_contract(_contract())


# ---------------------------------------------------------------------------
# Schema alignment checks
# ---------------------------------------------------------------------------

def test_schema_is_valid_json_and_matches_contract_conventions() -> None:
    schema = _schema()
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.get("title") == "Strategy return-source attribution contract"
    assert "strategies" in schema.get("properties", {})
    assert "strategy_id" in schema.get("$defs", {})
    assert "source_type" in schema.get("$defs", {})
    assert "confidence_level" in schema.get("$defs", {})


def test_schema_strategy_ids_enum_matches_registry() -> None:
    registry_ids = set(_registry_strategy_ids())
    schema = _schema()
    schema_ids = set(schema["$defs"]["strategy_id"]["enum"])
    assert schema_ids == registry_ids, (
        f"schema enum {schema_ids ^ registry_ids} "
        f"differ from registry IDs"
    )


def test_schema_enforces_no_identified_source_consistency() -> None:
    schema = _schema()
    assert "allOf" in schema["$defs"]["strategy"], (
        "strategy $def should have allOf for consistency rule"
    )
    block = schema["$defs"]["strategy"]["allOf"]
    assert len(block) >= 1
    if_block = block[0]
    assert "if" in if_block
    assert "then" in if_block
    assert "else" in if_block


# ---------------------------------------------------------------------------
# Negative fixtures
# ---------------------------------------------------------------------------

def test_contract_with_missing_strategy_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"] = contract["strategies"][:8]
    with pytest.raises(AssertionError, match="expected 9 strategies"):
        _assert_valid_contract(contract)


def test_contract_with_extra_strategy_fails() -> None:
    contract = copy.deepcopy(_contract())
    extra = copy.deepcopy(contract["strategies"][0])
    extra["strategy_id"] = "bogus-strategy"
    contract["strategies"].append(extra)
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)


def test_whitespace_only_text_field_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["mechanism"] = "   "
    with pytest.raises(AssertionError, match="empty or whitespace"):
        _assert_valid_contract(contract)


def test_tiny_placeholder_text_field_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["mechanism"] = "x"
    with pytest.raises(AssertionError, match="placeholder"):
        _assert_valid_contract(contract)


def test_analyzable_strategy_na_placeholder_fails() -> None:
    contract = copy.deepcopy(_contract())
    # Set an analyzable strategy's persistence to an N/A reference
    contract["strategies"][0]["persistence"] = "N/A — no barrier identified."
    with pytest.raises(AssertionError, match="N/A"):
        _assert_valid_contract(contract)


def test_analyzable_strategy_short_text_field_fails() -> None:
    contract = copy.deepcopy(_contract())
    # Replace mechanism with a real-looking but too-short sentence
    contract["strategies"][0]["mechanism"] = "Not long enough."
    with pytest.raises(AssertionError, match="too short"):
        _assert_valid_contract(contract)


def test_no_source_with_non_null_primary_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["no_identified_source"] = True
    contract["strategies"][0]["sources"]["primary"] = "risk_premium"
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)


def test_unknown_confidence_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["confidence"] = "high"
    with pytest.raises(AssertionError, match="not in"):
        _assert_valid_contract(contract)


def test_confidence_enum_case_matters() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["confidence"] = "Weak"
    with pytest.raises(AssertionError, match="not in"):
        _assert_valid_contract(contract)


def test_no_source_false_with_null_primary_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["no_identified_source"] = False
    contract["strategies"][0]["sources"]["primary"] = None
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)

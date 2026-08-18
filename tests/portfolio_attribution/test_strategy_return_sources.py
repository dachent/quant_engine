from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "strategy-return-sources-v1.json"
FINANCIAL_MVP_CONTRACT_PATH = (
    REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
)
REGISTRY_PATH = (
    REPOSITORY_ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
)
SCHEMA_PATH = (
    REPOSITORY_ROOT / "schemas" / "portfolio" / "strategy-return-sources.schema.json"
)

NO_CURRENT_SOURCE_HYPOTHESIS_STRATEGIES = {
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


# ---------------------------------------------------------------------------
# Schema-derived recursive validator
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, root_schema: dict) -> dict:
    """Resolve a ``#/$defs/<name>`` reference against the schema root."""
    assert ref.startswith("#/"), f"unexpected $ref format: {ref}"
    parts = ref[2:].split("/")
    node: dict = root_schema
    for part in parts:
        node = node[part]
    return node


def _schema_validate(
    node: dict,
    value: object,
    root_schema: dict,
    defs: dict,
    path: str = "$",
) -> list[str]:
    """Recursively validate *value* against JSON Schema fragment *node*.

    Returns a list of error strings (empty = valid).
    Handles the subset used by strategy-return-sources.schema.json:
    ``$ref``, ``const``, ``type`` (null/boolean/string/array/object),
    ``properties``, ``required``, ``additionalProperties``, ``enum``,
    ``minLength``, ``minItems``, ``maxItems``, ``anyOf``.
    Nodes using ``if/then/else`` or ``allOf`` are *not* handled here —
    conditional constraints are enforced via explicit assertions below.
    """
    errors: list[str] = []

    if "$ref" in node:
        target = _resolve_ref(node["$ref"], root_schema)
        return _schema_validate(target, value, root_schema, defs, path)

    if "const" in node:
        if value != node["const"]:
            errors.append(f"{path}: expected const {node['const']!r}, got {value!r}")
        return errors

    if "anyOf" in node:
        for alt in node["anyOf"]:
            if not _schema_validate(alt, value, root_schema, defs, path):
                return []
        errors.append(f"{path}: did not match any anyOf alternative (value={value!r})")
        return errors

    if "enum" in node:
        if value not in node["enum"]:
            errors.append(f"{path}: {value!r} not in allowed values {node['enum']}")
        return errors

    if "type" not in node:
        return errors

    t = node["type"]

    if t == "null":
        if value is not None:
            errors.append(
                f"{path}: expected null, got {type(value).__name__} {value!r}"
            )
        return errors

    if t == "boolean":
        if not isinstance(value, bool):
            errors.append(
                f"{path}: expected boolean, got {type(value).__name__} {value!r}"
            )
        return errors

    if t == "string":
        if not isinstance(value, str):
            errors.append(
                f"{path}: expected string, got {type(value).__name__} {value!r}"
            )
        else:
            if "minLength" in node and len(value) < node["minLength"]:
                errors.append(
                    f"{path}: string length {len(value)} < minLength {node['minLength']}"
                )
        return errors

    if t == "array":
        if not isinstance(value, list):
            errors.append(
                f"{path}: expected array, got {type(value).__name__} {value!r}"
            )
        else:
            if "minItems" in node and len(value) < node["minItems"]:
                errors.append(
                    f"{path}: array length {len(value)} < minItems {node['minItems']}"
                )
            if "maxItems" in node and len(value) > node["maxItems"]:
                errors.append(
                    f"{path}: array length {len(value)} > maxItems {node['maxItems']}"
                )
            if "items" in node:
                items_schema: dict = node["items"]
                for i, item in enumerate(value):
                    errors.extend(
                        _schema_validate(
                            items_schema, item, root_schema, defs, f"{path}[{i}]"
                        )
                    )
        return errors

    if t == "object":
        if not isinstance(value, dict):
            errors.append(
                f"{path}: expected object, got {type(value).__name__} {value!r}"
            )
            return errors

        props: dict = node.get("properties", {})

        for req in node.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field {req!r}")

        extra_allowed = node.get("additionalProperties", True)
        if extra_allowed is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected property {key!r}")

        for key, prop_schema in props.items():
            if key in value:
                errors.extend(
                    _schema_validate(
                        prop_schema, value[key], root_schema, defs, f"{path}.{key}"
                    )
                )
        return errors

    return errors


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _assert_strategy_ids(contract: dict) -> None:
    registry_ids = _registry_strategy_ids()
    contract_ids = [s["strategy_id"] for s in contract["strategies"]]
    assert len(contract_ids) == 9, f"expected 9 strategies, got {len(contract_ids)}"
    assert contract_ids == registry_ids, (
        f"contract order {contract_ids} does not match registry order {registry_ids}"
    )


def _assert_text_fields_quality(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        is_analyzable = sid in ANALYZABLE_STRATEGIES

        for field in TEXT_FIELDS:
            val = strategy[field]
            assert isinstance(val, str), f"strategy {sid} field {field} is not a string"
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
                assert not val.lstrip().startswith("N/A"), (
                    f"strategy {sid} field {field} uses N/A placeholder: {val!r}"
                )


def _assert_confidence_by_category(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        conf = strategy["confidence"]
        if sid in ANALYZABLE_STRATEGIES:
            assert conf == "weak", f"strategy {sid} should be 'weak', got {conf!r}"
        else:
            assert conf == "none", f"strategy {sid} should be 'none', got {conf!r}"


def _assert_required_product_scope_is_preserved(contract: dict) -> None:
    product = json.loads(FINANCIAL_MVP_CONTRACT_PATH.read_text(encoding="utf-8"))
    required = {
        strategy["strategy_id"]
        for strategy in product["strategies"]
        if strategy["product_requirement"] == "REQUIRED"
    }
    source_entries = {
        strategy["strategy_id"]: strategy for strategy in contract["strategies"]
    }
    for strategy_id in required:
        text = json.dumps(source_entries[strategy_id], sort_keys=True).lower()
        assert "out of scope" not in text, (
            f"required strategy {strategy_id} is described as out of scope"
        )
        assert "dropped" not in text, (
            f"required strategy {strategy_id} is described as dropped"
        )


def _assert_consistency_rule(contract: dict) -> None:
    """Enforce the if/then/else constraint documented in the schema."""
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


def _assert_falsification_distinct(contract: dict) -> None:
    for strategy in contract["strategies"]:
        sid = strategy["strategy_id"]
        falsification = strategy["falsification"]
        assert len(set(falsification)) == len(falsification), (
            f"strategy {sid} falsification contains duplicates"
        )


# ---------------------------------------------------------------------------
# Main contract-assertion entry point
# ---------------------------------------------------------------------------


def _assert_valid_contract(
    contract: dict,
    schema: dict | None = None,
) -> None:
    """Run every positive assertion against *contract*.

    When *schema* is provided it is used for the schema-derived checks
    (e.g. to validate that a modified schema still fails).  Otherwise the
    on-disk schema is loaded.

    Raises ``AssertionError`` on the first failure, so callers can wrap
    it in ``pytest.raises`` for negative tests.
    """
    if schema is None:
        schema = _schema()

    defs = schema.get("$defs", {})

    # --- Schema-derived validation: contract root -> strategies -> each item
    errors = _schema_validate(schema, contract, schema, defs, "$")
    assert not errors, (
        f"Schema validation failed ({len(errors)} error(s)):\n" + "\n".join(errors)
    )

    # --- Extra Pythonic assertions (beyond what the schema mechanically
    #     enforces, or that the schema's if/then/else documents only)

    _assert_strategy_ids(contract)
    _assert_text_fields_quality(contract)
    _assert_confidence_by_category(contract)
    _assert_required_product_scope_is_preserved(contract)
    _assert_consistency_rule(contract)
    _assert_falsification_distinct(contract)


# ===================================================================
# Positive assertions (real contract)
# ===================================================================


def test_contract_and_schema_exist() -> None:
    assert CONTRACT_PATH.is_file(), f"contract not found at {CONTRACT_PATH}"
    assert SCHEMA_PATH.is_file(), f"schema not found at {SCHEMA_PATH}"


def test_contract_loads_valid_json() -> None:
    contract = _contract()
    assert isinstance(contract, dict)
    assert contract.get("schema_version") == 1
    assert "strategies" in contract


def test_positive_full_validity() -> None:
    """The real contract must pass every assertion (schema-derived + extra)."""
    _assert_valid_contract(_contract())


# ===================================================================
# Schema alignment checks
# ===================================================================


def test_schema_metadata_is_valid() -> None:
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
        f"schema enum {schema_ids ^ registry_ids} differ from registry IDs"
    )


def test_schema_contains_conditional_block() -> None:
    schema = _schema()
    assert "allOf" in schema["$defs"]["strategy"], (
        "strategy $def should have allOf for consistency rule"
    )
    block = schema["$defs"]["strategy"]["allOf"]
    assert len(block) >= 1
    conditional = block[0]
    assert "if" in conditional
    assert "then" in conditional
    assert "else" in conditional


# ===================================================================
# Negative fixtures — contract mutations
# ===================================================================


def test_contract_with_missing_strategy_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"] = contract["strategies"][:8]
    with pytest.raises(AssertionError, match="minItems"):
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
    contract["strategies"][0]["persistence"] = "N/A \u2014 no barrier identified."
    with pytest.raises(AssertionError, match="N/A"):
        _assert_valid_contract(contract)


def test_analyzable_strategy_short_text_field_fails() -> None:
    contract = copy.deepcopy(_contract())
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
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)


def test_confidence_enum_case_matters() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["confidence"] = "Weak"
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)


def test_no_source_false_with_null_primary_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["strategies"][0]["no_identified_source"] = False
    contract["strategies"][0]["sources"]["primary"] = None
    with pytest.raises(AssertionError):
        _assert_valid_contract(contract)


# ===================================================================
# Negative fixture — schema mutation
# ===================================================================


def test_schema_extra_required_field_causes_failure() -> None:
    """If the schema gains a new required field the current contract must fail."""
    schema = copy.deepcopy(_schema())
    schema["$defs"]["strategy"]["required"].append("category")
    with pytest.raises(AssertionError, match="missing required field"):
        _assert_valid_contract(_contract(), schema=schema)

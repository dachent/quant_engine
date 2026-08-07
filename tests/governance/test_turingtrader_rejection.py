from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = (
    REPOSITORY_ROOT
    / "contracts"
    / "providers"
    / "turingtrader"
    / "source-rejection.v1.json"
)
DOCUMENT_PATH = REPOSITORY_ROOT / "docs" / "providers" / "turingtrader.md"
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "providers"
    / "turingtrader-source-rejection.schema.json"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _assert_approved_shape(
    value: Any, schema: dict[str, Any], root: dict[str, Any]
) -> None:
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        assert name in root["$defs"]
        _assert_approved_shape(value, root["$defs"][name], root)
        return
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    value_type = schema.get("type")
    if value_type == "object":
        assert isinstance(value, dict)
        properties = schema["properties"]
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(value)
        assert not (set(value) - set(properties))
        for key, item in value.items():
            _assert_approved_shape(item, properties[key], root)
    elif value_type == "array":
        assert isinstance(value, list)
        assert len(value) >= schema.get("minItems", 0)
        assert len(value) <= schema.get("maxItems", len(value))
        if schema.get("uniqueItems"):
            assert len({json.dumps(item, sort_keys=True) for item in value}) == len(value)
        if "items" in schema:
            for item in value:
                _assert_approved_shape(item, schema["items"], root)
    elif value_type == "string":
        assert isinstance(value, str)
        assert len(value) >= schema.get("minLength", 0)
        if "pattern" in schema:
            assert re.fullmatch(schema["pattern"], value)
    elif value_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= schema.get("minimum", value)


def test_manifest_uses_the_approved_sanitized_derivative_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    _assert_approved_shape(_manifest(), schema, schema)
    assert schema["additionalProperties"] is False

    forbidden = _manifest()
    forbidden["verified_source_metadata"][0]["source_locator"] = "private"
    with pytest.raises(AssertionError):
        _assert_approved_shape(forbidden, schema, schema)


def test_verified_metadata_and_unhydrated_capture_boundary_are_exact() -> None:
    manifest = _manifest()
    metadata = manifest["verified_source_metadata"]
    captures = manifest["unhydrated_capture_set"]

    assert manifest["provider_id"] == "turingtrader"
    assert manifest["decision"] == "REJECTED_NO_ACCEPTED_PROVIDER_ROUTE"
    assert [item["artifact_id"] for item in metadata] == [
        "metadata-001",
        "metadata-002",
    ]
    assert all(SHA256.fullmatch(item["sha256"]) for item in metadata)
    assert sum(item["bytes"] for item in metadata) == 8780
    assert captures["artifact_count"] == 5
    assert captures["total_bytes"] == 687784294
    assert captures["principal_navigation_artifact_count"] == 3
    assert captures["principal_navigation_total_bytes"] == 687176414
    assert captures["sha256_state"] == "UNAVAILABLE_HYDRATION_NOT_AUTHORIZED"
    assert captures["content_inspected"] is False
    assert captures["hydration_attempted"] is False


def test_rejected_branch_is_not_self_contained_or_promotable() -> None:
    manifest = _manifest()
    branch = manifest["rejected_branch"]

    assert branch["head_commit"] == ("ccf1b5626006f93cd27df9890da6c4a5aa6510f8")
    assert branch["head_tree"] == "f57633721a0005211bb7967e77b947f5ef8eeb57"
    assert branch["changed_file_count"] == 6
    assert branch["self_contained"] is False
    assert (
        branch["opaque_provenance_anchor"] != (branch["tracked_provider_module_sha256"])
    )
    assert branch["disposition"] == "PRESERVE_UNMERGED_AS_REJECTED_EVIDENCE"
    assert manifest["accepted_routes"] == []
    assert manifest["accepted_outputs"] == []
    assert {item["route_class"] for item in manifest["explicit_route_rejections"]} == {
        "AUTHENTICATED_PROFILE",
        "PUBLIC_FIREBASE_CANDIDATES",
    }


def test_strategy_handoff_is_explicitly_unavailable() -> None:
    handoff = _manifest()["strategy_handoff"]

    assert handoff == {
        "strategy_id": "tt-portfolio",
        "provider_evidence_state": "UNAVAILABLE",
        "version_ref": None,
        "version_disposition": "UNRESOLVED_NO_AUTHORITATIVE_MAPPING",
        "account_live_authority_unchanged": True,
        "provider_series_may_replace_account_live": False,
    }


def test_manifest_and_document_disclose_no_private_source_coordinate() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8") + DOCUMENT_PATH.read_text(
        encoding="utf-8"
    )

    assert "C:\\" not in text
    assert "BorisVaisman" not in text
    assert "Playground" not in text
    assert "testfolio.py" not in text.casefold()
    assert "profile value" in text


def test_document_preserves_fail_closed_boundary() -> None:
    text = DOCUMENT_PATH.read_text(encoding="utf-8")

    for required in (
        "REJECTED_NO_ACCEPTED_PROVIDER_ROUTE",
        "All observed provider routes and derived outputs are therefore unaccepted",
        "UNRESOLVED_NO_AUTHORITATIVE_MAPPING",
        "not hydrate a capture",
        "approved provider issue",
    ):
        assert required in text

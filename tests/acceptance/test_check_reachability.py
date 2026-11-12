"""Reachability control for the validator (Ruling C, F1; ticket #895).

Every check id must either be proven to pass on builder-class input (``PROVEN``, each
with the test that proves it) or be registered in ``acceptance/validate/unreachable.json``
with an issue, an owner and a ``close_by`` date.  An expired entry fails this test, so a
permanent row cannot hide as "known" any more.  The vocabulary tests pin the validator's
fixture values to the producers' own schema, so a fixture cannot drift to values no
producer emits (Ruling C, M8).
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import checks

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "acceptance" / "validate" / "unreachable.json"

# check id -> the test that passes real builder output through it with zero findings.
PROVEN = {
    "identity.publication_state": "test_d8_builder_output_validates.py",
    "identity.release_pinned": "test_d8_builder_output_validates.py",
    "layers.series_blocks": "test_layer_labels_check.py",
}
CLASSES = {
    "BUILDER_BUG",
    "BUILDER_NOT_WIRED",
    "BUILDER_NOT_WIRED_AND_DATA_GAP",
    "BY_DESIGN",
    "NEVER_SATISFIABLE",
    "RULE_COMPARES_DIFFERENT_QUANTITIES",
    "RULE_IGNORES_LABEL",
    "RULE_SCOPE_TOO_WIDE",
    "TWO_COMPONENTS_DISAGREE",
    "UNPROVEN_NO_BUILDER_FIXTURE",
}


def _entries() -> list[dict[str, Any]]:
    document = json.loads(REGISTER.read_bytes())
    assert document["schema"] == "validator-unreachable-register-v1"
    return document["entries"]


def expired(entries: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    return [e for e in entries if date.fromisoformat(e["close_by"]) < today]


def test_register_entries_are_complete() -> None:
    for entry in _entries():
        assert type(entry["issue"]) is int and entry["issue"] > 0, entry
        assert entry["owner"].strip() and entry["reason"].strip(), entry
        assert entry["class"] in CLASSES, entry
        date.fromisoformat(entry["close_by"])


def test_every_check_is_proven_or_registered() -> None:
    ids = {name for name, _check in checks.CHECKS}
    registered = {e["check_id"] for e in _entries()}
    assert not ids - set(PROVEN) - registered, "unregistered unproven checks"
    assert not registered - ids, "register names a check that does not exist"
    assert not set(PROVEN) & registered, "a proven check must leave the register"
    assert set(PROVEN) <= ids


def test_proof_tests_exist_and_name_their_checks() -> None:
    for check_id, proof in PROVEN.items():
        path = ROOT / "tests" / "acceptance" / proof
        assert check_id in path.read_text(encoding="utf-8"), (check_id, proof)


CLOSE_BY_CEILING = date(2027, 3, 31)


def test_close_by_dates_have_a_ceiling() -> None:
    """Lengthening an entry past the ceiling needs a new ruling, not a one-line edit."""

    late = [
        e for e in _entries() if date.fromisoformat(e["close_by"]) > CLOSE_BY_CEILING
    ]
    assert late == []


def test_no_register_entry_has_expired() -> None:
    assert expired(_entries(), date.today()) == []


def test_the_expiry_rule_bites() -> None:
    """Kill test: an entry past its close_by is reported."""

    assert expired(_entries(), date(2100, 1, 1)) == _entries()
    assert expired(_entries(), date(2000, 1, 1)) == []


def _schema(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "portfolio" / name).read_bytes())


def _panel_branch(profile: str) -> dict[str, Any]:
    root = _schema("control-panel-data.schema.json")
    for branch in root["allOf"]:
        guard = branch.get("if", {}).get("properties", {}).get("meta", {})
        const = (
            guard.get("properties", {})
            .get("account_manifest_authority_profile", {})
            .get("const")
        )
        if const == profile:
            return {
                "$schema": root["$schema"],
                "$defs": root["$defs"],
                **branch["then"],
            }
    raise AssertionError(profile)


def test_validator_fixture_verification_record_is_in_the_producer_vocabulary() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from test_validate_generation import (
        EPOCH_MANIFEST_SHA,
        PRODUCER_READINESS,
        verified_bundle_record,
    )

    meta = {
        "account_manifest_authority_profile": "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES",
        "account_manifest_sha256": EPOCH_MANIFEST_SHA,
        "account_manifest_size_bytes": 1000,
        "account_manifest_verification": verified_bundle_record(3),
    }
    branch = _panel_branch("VERIFIED_GENERATION_BUNDLE_EXACT_BYTES")
    jsonschema.Draft202012Validator(branch).validate({"meta": meta})
    readiness = _schema("account-strategy-manifest.schema.json")
    pinned = json.dumps(readiness)
    for key, value in PRODUCER_READINESS.items():
        assert f'"{key}"' in pinned and f'"{value}"' in pinned


def test_a_value_outside_the_producer_vocabulary_is_rejected() -> None:
    """Kill test: the old fixture's READY/PERFORMED/VERIFIED values do not validate."""

    jsonschema = pytest.importorskip("jsonschema")
    from test_validate_generation import EPOCH_MANIFEST_SHA, verified_bundle_record

    record = verified_bundle_record(3)
    record["publication_readiness"] = "READY"
    meta = {
        "account_manifest_authority_profile": "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES",
        "account_manifest_sha256": EPOCH_MANIFEST_SHA,
        "account_manifest_size_bytes": 1000,
        "account_manifest_verification": record,
    }
    branch = _panel_branch("VERIFIED_GENERATION_BUNDLE_EXACT_BYTES")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(branch).validate({"meta": meta})


def test_check_ids_are_unique() -> None:
    """Duplicate ids make a row-diff gate ambiguous (Fable plan review)."""

    ids = [name for name, _check in checks.CHECKS]
    assert len(ids) == len(set(ids))

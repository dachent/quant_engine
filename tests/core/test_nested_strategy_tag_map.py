"""The nested-strategy tag map: policy validation and tag resolution (#567)."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import nested_strategy_tag_map as tag_map_module
from nested_strategy_tag_map import TagMapError, sleeve_accounts, validate_tag_map


def _payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-09-17",
        "parents": [
            {
                "parent_account_id": "account-parent",
                "provider_id": "provider-a",
                "tag_field": "orderReference",
                "tag_normalization": "STRIP_LEADING_DIGITS_AND_DASH",
                "sleeves": [
                    {
                        "sleeve_id": "s1",
                        "strategy_id": "strategy-one",
                        "label": "First sleeve",
                        "effective_start": "2026-01-26",
                        "effective_end": "2026-06-23",
                        "tags": [{"match": "startswith", "pattern": "FirstBook"}],
                    },
                    {
                        "sleeve_id": "s2",
                        "strategy_id": "strategy-two",
                        "label": "Primary sleeve",
                        "effective_start": "2026-01-30",
                        "effective_end": None,
                        "tags": [{"match": "startswith", "pattern": "PrimaryBook"}],
                    },
                    {
                        "sleeve_id": "s5",
                        "strategy_id": "strategy-five",
                        "label": "Two streams",
                        "effective_start": "2026-07-22",
                        "effective_end": "2026-07-27",
                        "tags": [
                            {"match": "contains", "pattern": "StreamA"},
                            {"match": "exact", "pattern": "StreamB"},
                        ],
                    },
                ],
                "unmatched": {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s2",
                },
            }
        ],
    }


def _bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_the_map_validates_against_its_schema_and_the_validator() -> None:
    payload = _payload()
    schema = json.loads(tag_map_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(payload))
    model = validate_tag_map(_bytes(payload))
    assert [item[0] for item in sleeve_accounts(model)] == [
        "account-parent-s1",
        "account-parent-s2",
        "account-parent-s5",
    ]
    assert model.sha256 == validate_tag_map(_bytes(payload)).sha256


def test_tags_resolve_after_stripping_the_leading_reference_and_by_date() -> None:
    parent = validate_tag_map(_bytes(_payload())).parent("account-parent")

    first = parent.resolve("1234567-FirstBookLive", date(2026, 3, 1))
    assert first is not None and first.sleeve_id == "s1"
    assert parent.resolve("FirstBookLive", date(2026, 6, 24)) is None
    primary = parent.resolve("99-PrimaryBook125", date(2026, 9, 1))
    assert primary is not None and primary.strategy_id == "strategy-two"
    streams = {
        parent.resolve("5-xStreamAx", date(2026, 7, 22)).sleeve_id,  # type: ignore[union-attr]
        parent.resolve("StreamB", date(2026, 7, 24)).sleeve_id,  # type: ignore[union-attr]
    }
    assert streams == {"s5"}
    assert parent.resolve("StreamBx", date(2026, 7, 24)) is None
    assert parent.resolve("unrelated", date(2026, 3, 1)) is None


def test_a_tag_matching_two_sleeves_on_one_day_is_refused_not_guessed() -> None:
    payload = _payload()
    payload["parents"][0]["sleeves"][1]["tags"].append(
        {"match": "contains", "pattern": "FirstBook"}
    )
    parent = validate_tag_map(_bytes(payload)).parent("account-parent")
    with pytest.raises(TagMapError, match="several sleeves"):
        parent.resolve("FirstBookLive", date(2026, 3, 1))
    # Outside the first sleeve's interval only one sleeve matches.
    assert parent.resolve("FirstBookLive", date(2026, 9, 1)).sleeve_id == "s2"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["parents"][0]["sleeves"][0].update(sleeve_id="s2"), "repeats"),
        (
            lambda p: p["parents"][0]["sleeves"][0].update(strategy_id="strategy-two"),
            "repeats",
        ),
        (
            lambda p: p["parents"][0]["sleeves"][0].update(effective_end="2026-01-01"),
            "reversed",
        ),
        (lambda p: p["parents"][0]["sleeves"][0].update(tags=[]), "no tags"),
        (
            lambda p: p["parents"][0]["sleeves"][0]["tags"][0].update(match="regex"),
            "tag is invalid",
        ),
        (
            lambda p: p["parents"][0].update(
                unmatched={
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s9",
                }
            ),
            "fallback sleeve",
        ),
        (
            lambda p: p["parents"][0].update(tag_normalization="LOWER"),
            "tag_normalization",
        ),
        (lambda p: p["parents"].append(deepcopy(p["parents"][0])), "duplicate parent"),
        (lambda p: p.update(extra=1), "tag map keys"),
        (lambda p: p["parents"][0].update(parent_account_id="Bad Id"), "identity"),
        (
            lambda p: p["parents"][0].update(parent_account_id="a" * 63),
            "too long",
        ),
    ],
)
def test_invalid_maps_are_refused(mutate: Any, message: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(TagMapError, match=message):
        validate_tag_map(_bytes(payload))
    assert not isinstance(payload, bytes)


def test_duplicate_json_members_are_refused_not_last_one_wins() -> None:
    raw = json.dumps(_payload()).replace(
        '"pattern": "FirstBook"', '"pattern": "FirstBook", "pattern": "Other"'
    )
    assert raw.count('"pattern": "Other"') == 1
    with pytest.raises(TagMapError, match="duplicate JSON member"):
        validate_tag_map(raw.encode("utf-8"))


def test_the_loader_enforces_the_schema_limits_too() -> None:
    long_field = _payload()
    long_field["parents"][0]["tag_field"] = "x" * 65
    with pytest.raises(TagMapError, match="tag_field"):
        validate_tag_map(_bytes(long_field))
    boolean_version = _payload()
    boolean_version["schema_version"] = True
    with pytest.raises(TagMapError, match="identity"):
        validate_tag_map(_bytes(boolean_version))


def test_an_unknown_sleeve_has_no_derived_account() -> None:
    parent = validate_tag_map(_bytes(_payload())).parent("account-parent")
    with pytest.raises(TagMapError, match="unknown sleeve"):
        parent.sleeve_account_id("s9")
    with pytest.raises(TagMapError, match="unknown parent"):
        validate_tag_map(_bytes(_payload())).parent("other")

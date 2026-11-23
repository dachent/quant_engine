"""The account-financing-declaration schema and tools/account_financing.load_declaration agree.

The schema is a source-bound input of the report and panel stages (controller graph); the loader is
the only runtime reader.  Every sample below must be accepted by both or refused by both, so a v2
declaration (settled_debit_interest, #894 owner ruling) can never validate in one and fail in the
other.  The pinned v1 declaration lives in the retained records, not here: a v1-shaped sample with
every key a v1 row carries stands in for it, and the v1 shape must stay valid unchanged."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import account_financing as af

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas/portfolio/account-financing-declaration.schema.json"
)
VALIDATOR = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
RET = "account-" + "1" * 16
MARGIN = "account-" + "2" * 16
UNUSED = "account-" + "3" * 16


def _row(account: str, facility: str, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": account,
        "scope": "ACTIVE",
        "facility": facility,
        "verification": "VERIFIED",
        "evidence_ref": "owner",
        **extra,
    }


V2 = {
    "settled_debit_interest": "NONE_BY_ACCOUNT_TYPE",
    "settled_debit_interest_refs": ["schwab-stmt-aaaaaaaa-2026-02-28.pdf"],
}


def _document(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "account-financing-declaration",
        "as_of_date": "2026-11-19",
        "accounts": list(rows),
    }


def _schema_ok(document: dict[str, Any]) -> bool:
    return VALIDATOR.is_valid(document)


def _loader_ok(document: dict[str, Any]) -> bool:
    try:
        af.load_declaration(json.dumps(document).encode())
    except af.FinancingDeclarationError:
        return False
    return True


def _both(document: dict[str, Any]) -> tuple[bool, bool]:
    return _schema_ok(document), _loader_ok(document)


def test_the_schema_is_itself_valid() -> None:
    Draft202012Validator.check_schema(json.loads(SCHEMA.read_text(encoding="utf-8")))


def test_the_v1_shape_is_unchanged_and_valid_for_both() -> None:
    document = _document(
        _row(MARGIN, "MARGIN_USED"),
        _row(RET, "NO_MARGIN_FACILITY"),
        _row(UNUSED, "MARGIN_AVAILABLE_UNUSED", verification="UNVERIFIED"),
    )
    assert _both(document) == (True, True)


def test_a_v2_row_on_a_no_margin_account_is_valid_for_both() -> None:
    document = _document(
        _row(RET, "NO_MARGIN_FACILITY", **V2), _row(MARGIN, "MARGIN_USED")
    )
    assert _both(document) == (True, True)
    declared = af.load_declaration(json.dumps(document).encode())
    assert declared[RET].settled_debit_interest == "NONE_BY_ACCOUNT_TYPE"
    assert declared[MARGIN].settled_debit_interest is None


@pytest.mark.parametrize("facility", ["MARGIN_USED", "MARGIN_AVAILABLE_UNUSED"])
def test_a_margin_row_with_the_field_is_refused_by_both(facility: str) -> None:
    assert _both(_document(_row(MARGIN, facility, **V2))) == (False, False)


@pytest.mark.parametrize(
    "extra",
    [
        {"settled_debit_interest": "NONE_BY_ACCOUNT_TYPE"},  # no refs
        {"settled_debit_interest_refs": ["x.pdf"]},  # no statement
        {**V2, "settled_debit_interest": "SOMETHING_ELSE"},
        {**V2, "settled_debit_interest": None},
        {**V2, "settled_debit_interest_refs": []},
        {**V2, "settled_debit_interest_refs": [" "]},
        {**V2, "settled_debit_interest_refs": [""]},
        {**V2, "settled_debit_interest_refs": "x.pdf"},
        {**V2, "settled_debit_interest_refs": [3]},
        {**V2, "settled_debit_interest_refs": None},
        {**V2, "unknown_key": 1},
    ],
)
def test_a_malformed_settled_debit_declaration_is_refused_by_both(
    extra: dict[str, Any],
) -> None:
    assert _both(_document(_row(RET, "NO_MARGIN_FACILITY", **extra))) == (False, False)


def test_the_v1_only_keys_stay_required_and_closed() -> None:
    base = _row(RET, "NO_MARGIN_FACILITY", **V2)
    for key in ("account_id", "scope", "facility", "verification", "evidence_ref"):
        broken = {k: v for k, v in base.items() if k != key}
        assert _both(_document(broken)) == (False, False), key
    assert _both(_document({**base, "extra": 1})) == (False, False)


def test_the_unverified_v2_row_is_schema_valid_and_the_loader_reads_it_unverified() -> (
    None
):
    document = _document(
        _row(RET, "NO_MARGIN_FACILITY", verification="UNVERIFIED", **V2)
    )
    assert _both(document) == (True, True)
    assert af.load_declaration(json.dumps(document).encode())[RET].verified is False


def test_the_two_validators_never_disagree_on_a_mutation_sweep() -> None:
    base = _document(_row(RET, "NO_MARGIN_FACILITY", **V2), _row(MARGIN, "MARGIN_USED"))
    mutations: list[dict[str, Any]] = []
    for account in (0, 1):
        for field, values in (
            (
                "facility",
                ["MARGIN_USED", "MARGIN_AVAILABLE_UNUSED", "NO_MARGIN_FACILITY", "X"],
            ),
            ("scope", ["ACTIVE", "RESERVE", "X"]),
            ("verification", ["VERIFIED", "UNVERIFIED", "X"]),
            ("settled_debit_interest", ["NONE_BY_ACCOUNT_TYPE", "X", None]),
        ):
            for value in values:
                document = copy.deepcopy(base)
                document["accounts"][account][field] = value
                mutations.append(document)
    assert len(mutations) == 2 * (4 + 3 + 3 + 3)
    for document in mutations:
        assert _schema_ok(document) == _loader_ok(document), document["accounts"]

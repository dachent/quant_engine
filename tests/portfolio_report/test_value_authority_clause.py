"""The FX/valuation authority clause follows the value_basis labels the payload carries.

Synthetic rows only. One generator (`_restate_value_authority`) rewrites the baseline's
"remain provider-reported USD authority" clause in the report coverage rows, which the panel
gates, coverage and limitations copy.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

import portfolio_report as reporter

DERIVED = "DERIVED_FROM_OFFICIAL_CLOSES"
CLAUSE = (
    "account NAV, current positions, allocations, and HWM current values remain "
    "provider-reported USD authority"
)
CUTOFF = "2026-09-17"
LATEST = "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT"
HISTORICAL = "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT"


def _coverage() -> list[dict[str, Any]]:
    return [
        {"category": "FACT_COVERAGE", "item": "OBSERVED", "known_gap": "nothing here"},
        {
            "category": "FACT_COVERAGE",
            "item": "FX",
            "known_gap": f"no independent FX series covers 3 records; {CLAUSE}",
        },
    ]


def _basis(label: str = DERIVED) -> dict[str, Any]:
    return {"basis": label, "provider_finality_guarantee": False}


def _position(index: int, **fields: Any) -> dict[str, Any]:
    return {
        "security_id": f"s{index}",
        "as_of_date": CUTOFF,
        "snapshot_status": LATEST,
        **fields,
    }


def _rows(
    accounts: int, positions: int, label: str = DERIVED
) -> tuple[list[Any], list[Any]]:
    account_rows = [
        {"account_id": f"a{i}", "value_basis": _basis(label)} for i in range(accounts)
    ]
    account_rows.append({"account_id": "native"})
    position_rows = [_position(i, value_basis=_basis(label)) for i in range(positions)]
    position_rows.append(_position(positions))
    return account_rows, position_rows


def _restate(accounts: list[Any], positions: list[Any]) -> list[dict[str, Any]]:
    return reporter._restate_value_authority(_coverage(), accounts, positions, CUTOFF)


def test_the_clause_states_the_derived_counts_from_the_rows() -> None:
    accounts, positions = _rows(2, 5)
    out = _restate(accounts, positions)
    text = out[1]["known_gap"]
    assert "remain provider-reported USD authority" not in text
    assert (
        "remain USD authority and are provider-reported except 7 values labelled "
        f"{DERIVED} (2 account NAVs and 5 current positions)"
    ) in text
    assert out[0] == _coverage()[0]  # rows without the clause are untouched


def test_the_counts_follow_the_data_and_the_plurals_follow_the_counts() -> None:
    accounts, positions = _rows(1, 1)
    text = _restate(accounts, positions)[1]["known_gap"]
    assert f"2 values labelled {DERIVED} (1 account NAV and 1 current position)" in text
    accounts, positions = _rows(6, 38)
    text = _restate(accounts, positions)[1]["known_gap"]
    assert (
        f"44 values labelled {DERIVED} (6 account NAVs and 38 current positions)"
        in text
    )


def test_a_historical_or_pre_cutoff_snapshot_is_not_a_current_position() -> None:
    accounts, positions = _rows(1, 2)
    positions.append(_position(90, value_basis=_basis(), snapshot_status=HISTORICAL))
    positions.append(_position(91, value_basis=_basis(), as_of_date="2026-09-10"))
    text = _restate(accounts, positions)[1]["known_gap"]
    assert (
        f"3 values labelled {DERIVED} (1 account NAV and 2 current positions)" in text
    )


def test_every_label_in_the_payload_is_named_with_its_own_counts() -> None:
    accounts, positions = _rows(2, 3)
    accounts.append({"account_id": "x", "value_basis": _basis("OTHER_BASIS")})
    text = _restate(accounts, positions)[1]["known_gap"]
    assert (
        f"5 values labelled {DERIVED} (2 account NAVs and 3 current positions)" in text
    )
    assert (
        "1 value labelled OTHER_BASIS (1 account NAV and 0 current positions)" in text
    )


def test_without_any_labelled_value_the_text_is_unchanged() -> None:
    coverage = _coverage()
    before = copy.deepcopy(coverage)
    accounts = [{"account_id": "native"}]
    positions = [_position(0)]
    assert (
        reporter._restate_value_authority(coverage, accounts, positions, CUTOFF)
        == before
    )
    assert coverage == before  # the input is not modified in place


def test_a_derived_nav_row_does_not_claim_to_be_a_latest_observation() -> None:
    index = [
        {
            "accounts": {"acct-derived": frozenset({"2026-09-17"})},
            "value_basis": {"basis": DERIVED},
        }
    ]
    catalog = {
        "accounts": [
            {
                "account_id": a,
                "role": "INVESTMENT",
                "provider": "P",
                "base_currency": "USD",
            }
            for a in ("acct-derived", "acct-native")
        ],
        "nav_observations": [
            {
                "account_id": a,
                "as_of_date": "2026-09-17",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "5"},
            }
            for a in ("acct-derived", "acct-native")
        ],
    }
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        reporter,
        "_current_capital",
        lambda _c: {
            "reserve": {
                "status": "NOT_READY_AT_CUTOFF",
                "as_of_date": None,
                "current_value": {
                    "currency": "USD",
                    "status": "UNAVAILABLE",
                    "value": None,
                },
            }
        },
    )
    try:
        rows = {
            r["account_id"]: r
            for r in reporter._accounts(catalog, derived_values=index)
        }
    finally:
        monkey.undo()
    assert rows["acct-derived"]["status"] == f"AVAILABLE_{DERIVED}"
    assert rows["acct-native"]["status"] == "AVAILABLE_LATEST_OBSERVATION"

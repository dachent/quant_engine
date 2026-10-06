"""N29: a strategy publishes one MWR over its funded members' pooled cash flows.

Plan v5 4.5 D lists MWR per account and per strategy.  tt-portfolio stands on
four funded accounts plus two assignment-only predecessor epochs; no single
account row equals the strategy, so the report pools the funded epochs' investor
cash flows (the same pooling both oracles use) and classifies the result with
the one XIRR classifier.  A funded member without an available MWR makes the
strategy MWR unavailable with a named reason rather than a partial number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import portfolio_report as reporter


def _member(
    account_id: str,
    cashflows: list[tuple[str, str]],
    *,
    admission: str = "FUNDED",
    mwr_status: str = "AVAILABLE",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "strategy_id": "pooled",
        "epoch_admission_status": admission,
        "mwr_status": mwr_status,
        "mwr_cashflows": [
            {"date": day, "amount": amount, "source_record_ids": ["source"]}
            for day, amount in cashflows
        ],
    }


A = [("2026-01-01", "-100"), ("2026-12-31", "110")]
B = [("2026-01-01", "-200"), ("2026-12-31", "230")]


def test_two_funded_members_pool_their_cash_flows_by_date() -> None:
    result = reporter._strategy_mwr([_member("a", A), _member("b", B)])

    assert result["mwr_status"] == "AVAILABLE"
    assert result["mwr_reason"] is None
    # Pooled flows are -300 on the opening date and +340 at the end.
    pooled = (Decimal(340) / Decimal(300)) ** (Decimal(365) / Decimal(364)) - 1
    assert abs(Decimal(result["mwr_xirr"]) - pooled) < Decimal("1e-9")
    assert result["mwr_member_account_ids"] == ["a", "b"]
    assert result["mwr_opening_date"] == "2026-01-01"
    assert result["mwr_terminal_date"] == "2026-12-31"


def test_assignment_only_predecessor_epochs_do_not_enter_the_pool() -> None:
    predecessor = _member(
        "old", [], admission="BLOCKED", mwr_status="BLOCKED_ADMISSION"
    )

    with_predecessor = reporter._strategy_mwr(
        [_member("a", A), predecessor, _member("b", B)]
    )

    assert with_predecessor == reporter._strategy_mwr(
        [_member("a", A), _member("b", B)]
    )
    assert with_predecessor["mwr_member_account_ids"] == ["a", "b"]


def test_a_funded_member_without_an_available_mwr_blocks_the_strategy_number() -> None:
    result = reporter._strategy_mwr(
        [_member("a", A), _member("b", [], mwr_status="UNIQUENESS_NOT_PROVED")]
    )

    assert result["mwr_status"] == "UNAVAILABLE_MEMBER_MWR_NOT_AVAILABLE"
    assert result["mwr_reason"] == "MEMBER_MWR_NOT_AVAILABLE"
    assert result["mwr_xirr"] is None
    assert result["mwr_member_account_ids"] == []


def test_a_strategy_without_funded_members_publishes_an_honest_status() -> None:
    result = reporter._strategy_mwr([])

    assert result["mwr_status"] == "UNAVAILABLE_NO_FUNDED_EPOCH_MWR"
    assert result["mwr_reason"] == "NO_FUNDED_EPOCH_ROWS"
    assert result["mwr_xirr"] is None


def test_pooled_flows_use_the_one_classifier_for_non_unique_streams() -> None:
    two_roots = [
        _member(
            "a",
            [("2025-01-01", "-100"), ("2026-01-01", "230"), ("2027-01-01", "-132")],
        )
    ]

    result = reporter._strategy_mwr(two_roots)

    assert result["mwr_status"] == "UNIQUENESS_NOT_PROVED"
    assert result["mwr_xirr"] is None

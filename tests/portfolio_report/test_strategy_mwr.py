"""N29: a strategy publishes one MWR over its funded members' pooled cash flows.

Plan v5 4.5 D lists MWR per account and per strategy.  tt-portfolio stands on
four funded accounts plus two assignment-only predecessor epochs; no single
account row equals the strategy, so the report pools the funded epochs' investor
cash flows (the same pooling both oracles use) and classifies the result with
the one XIRR classifier.  A funded member whose cash flows are not available makes
the strategy MWR unavailable with a named reason rather than a partial number; a
member whose own stream has no unique root still contributes its cash flows, because
uniqueness is a property of the pooled stream being solved.
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
    coverage_status: str | None = None,
) -> dict[str, Any]:
    extra = {} if coverage_status is None else {"mwr_coverage_status": coverage_status}
    return {
        **extra,
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


def test_a_funded_member_without_cash_flows_blocks_the_strategy_number() -> None:
    result = reporter._strategy_mwr(
        [_member("a", A), _member("b", [], mwr_status="UNIQUENESS_NOT_PROVED")]
    )

    assert result["mwr_status"] == "UNAVAILABLE_MEMBER_MWR_NOT_AVAILABLE"
    assert result["mwr_reason"] == "MEMBER_CASHFLOWS_NOT_AVAILABLE"
    assert result["mwr_xirr"] is None
    assert result["mwr_member_account_ids"] == []


def test_a_blocked_funded_member_keeps_the_strategy_number_withheld() -> None:
    blocked = _member("a", [], admission="FUNDED", mwr_status="BLOCKED_ADMISSION")
    unavailable = _member("c", A, mwr_status="UNAVAILABLE_INCOMPLETE_FLOW_COVERAGE")

    for members in ([blocked, _member("b", B)], [unavailable, _member("b", B)]):
        result = reporter._strategy_mwr(members)

        assert result["mwr_status"] == "UNAVAILABLE_MEMBER_MWR_NOT_AVAILABLE"
        assert result["mwr_reason"] == "MEMBER_CASHFLOWS_NOT_AVAILABLE"
        assert result["mwr_xirr"] is None


# Two member streams over the same December 2025 to August 2026 interval.  The first
# has three sign changes, so its own IRR is not provably unique; the pooled stream
# nets to -1100 on the opening date, +1500 plus 100 at the end and has one root.
NON_UNIQUE_MEMBER = [
    ("2025-12-01", "-100"),
    ("2026-03-01", "350"),
    ("2026-05-30", "-350"),
    ("2026-08-28", "100"),
]
AVAILABLE_MEMBER = [("2025-12-01", "-1000"), ("2026-08-28", "1500")]


def test_a_member_without_a_unique_root_still_pools_into_a_unique_strategy_root() -> (
    None
):
    result = reporter._strategy_mwr(
        [
            _member("a", NON_UNIQUE_MEMBER, mwr_status="UNIQUENESS_NOT_PROVED"),
            _member("b", AVAILABLE_MEMBER),
        ]
    )

    assert result["mwr_status"] == "AVAILABLE"
    assert result["mwr_reason"] is None
    assert result["mwr_member_account_ids"] == ["a", "b"]
    assert result["mwr_basis"] == "POOLED_FUNDED_EPOCH_CASHFLOWS"
    expected = Decimal("0.742652946403885272270078155844433615707")
    assert abs(Decimal(result["mwr_xirr"]) - expected) < Decimal("1e-12")


def test_every_classifier_outcome_member_with_cash_flows_is_pooled() -> None:
    for status in (
        "UNIQUENESS_NOT_PROVED",
        "NO_ROOT",
        "INDETERMINATE",
        "NUMERICAL_SEARCH_UNRESOLVED",
    ):
        result = reporter._strategy_mwr(
            [_member("a", A, mwr_status=status), _member("b", B)]
        )

        assert result["mwr_status"] == "AVAILABLE", status
        assert result["mwr_member_account_ids"] == ["a", "b"], status


def test_pooling_does_not_gate_on_the_member_receipt_coverage_status() -> None:
    """R1 Q3: the receipt status also reads COMPLETE on blocked rows, so it gates nothing."""

    for coverage in ("COMPLETE", "PARTIAL", "UNKNOWN"):
        result = reporter._strategy_mwr(
            [_member("a", A, coverage_status=coverage), _member("b", B)]
        )

        assert result["mwr_status"] == "AVAILABLE", coverage


def test_a_pool_whose_netted_stream_is_not_unique_publishes_that_status() -> None:
    result = reporter._strategy_mwr(
        [
            _member("a", [("2025-01-01", "-100"), ("2026-01-01", "230")]),
            _member("b", [("2027-01-01", "-132")]),
        ]
    )

    assert result["mwr_status"] == "UNIQUENESS_NOT_PROVED"
    assert result["mwr_xirr"] is None
    assert result["mwr_member_account_ids"] == ["a", "b"]


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

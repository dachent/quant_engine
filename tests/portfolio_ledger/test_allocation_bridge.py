from datetime import date
from decimal import Decimal

import pytest

from quant_engine.portfolio_ledger import (
    Account,
    AccountPoolAssignment,
    AccountRole,
    AllocationDecision,
    CapitalPool,
    CapitalPoolRole,
    LedgerValidationError,
    reconcile_allocation_bridge,
)


AS_OF = date(2026, 7, 30)


def _inputs():
    accounts = (
        Account("investment-a", "Investment A", "USD", AccountRole.INVESTMENT),
        Account("investment-b", "Investment B", "USD", AccountRole.INVESTMENT),
        Account("reserve", "Reserve", "USD", AccountRole.RESERVE),
    )
    pools = (
        CapitalPool("systematic", "Systematic", "USD", CapitalPoolRole.SYSTEMATIC),
        CapitalPool("reserve", "Reserve", "USD", CapitalPoolRole.RESERVE),
    )
    assignments = (
        AccountPoolAssignment(
            "ap-1", "investment-a", "systematic", None, None, Decimal("1")
        ),
        AccountPoolAssignment(
            "ap-2", "investment-b", "systematic", None, None, Decimal("1")
        ),
        AccountPoolAssignment("ap-3", "reserve", "reserve", None, None, Decimal("1")),
    )
    decisions = (
        AllocationDecision(
            "ad-1",
            "systematic",
            "strategy-a",
            Decimal("0.6"),
            date(2026, 7, 25),
            date(2026, 7, 25),
            AS_OF,
        ),
        AllocationDecision(
            "ad-2",
            "systematic",
            "strategy-b",
            Decimal("0.4"),
            date(2026, 7, 25),
            date(2026, 7, 25),
            AS_OF,
        ),
        AllocationDecision(
            "ad-3",
            "reserve",
            None,
            Decimal("1"),
            date(2026, 7, 25),
            date(2026, 7, 25),
            AS_OF,
        ),
    )
    values = {
        "investment-a": Decimal("250"),
        "investment-b": Decimal("150"),
        "reserve": Decimal("100"),
    }
    return accounts, pools, assignments, decisions, values


def test_account_pool_strategy_and_reserve_totals_reconcile() -> None:
    accounts, pools, assignments, decisions, values = _inputs()
    result = reconcile_allocation_bridge(
        as_of_date=AS_OF,
        accounts=accounts,
        pools=pools,
        account_values=values,
        account_pool_assignments=assignments,
        allocation_decisions=decisions,
    )

    assert result.to_dict() == {
        "account_total": "500",
        "as_of_date": "2026-07-30",
        "pool_total": "500",
        "reconciled": True,
        "reserve_total": "100",
        "strategy_values": [
            {"strategy_id": "strategy-a", "value": "240"},
            {"strategy_id": "strategy-b", "value": "160"},
        ],
        "systematic_total": "400",
    }


def test_incomplete_current_pool_or_strategy_weights_fail_closed() -> None:
    accounts, pools, assignments, decisions, values = _inputs()
    with pytest.raises(LedgerValidationError, match="account-pool weights"):
        reconcile_allocation_bridge(
            as_of_date=AS_OF,
            accounts=accounts,
            pools=pools,
            account_values=values,
            account_pool_assignments=assignments[:-1],
            allocation_decisions=decisions,
        )
    with pytest.raises(LedgerValidationError, match="allocation weights"):
        reconcile_allocation_bridge(
            as_of_date=AS_OF,
            accounts=accounts,
            pools=pools,
            account_values=values,
            account_pool_assignments=assignments,
            allocation_decisions=decisions[:-1],
        )


def test_source_valuation_and_publication_dates_cannot_be_laundered() -> None:
    with pytest.raises(LedgerValidationError, match="publication date"):
        AllocationDecision(
            "ad-invalid",
            "systematic",
            "strategy-a",
            Decimal("1"),
            date(2026, 7, 25),
            date(2026, 7, 30),
            date(2026, 7, 25),
        )

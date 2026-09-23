"""Coverage-aware arithmetic reconciliation for canonical position snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from quant_engine.portfolio_ledger.models import (
    AccountRole,
    LedgerSlice,
    LedgerValidationError,
    MappingStatus,
    ValueStatus,
    decimal_text,
)


class ReconciliationStatus(StrEnum):
    RECONCILED = "RECONCILED"
    DIFFERENCE = "DIFFERENCE"
    UNAVAILABLE = "UNAVAILABLE"
    UNMAPPED = "UNMAPPED"


@dataclass(frozen=True)
class ReconciliationException:
    code: str
    detail: str
    record_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "detail": self.detail,
            "record_ids": list(self.record_ids),
        }


@dataclass(frozen=True)
class StrategyValue:
    strategy_id: str
    value: Decimal

    def to_dict(self) -> dict[str, str]:
        return {"strategy_id": self.strategy_id, "value": decimal_text(self.value)}


@dataclass(frozen=True)
class AccountPositionReconciliation:
    account_id: str
    as_of_date: date
    currency: str
    status: ReconciliationStatus
    account_position_value: Decimal | None
    mapped_strategy_value: Decimal | None
    nav_value: Decimal | None
    nav_difference: Decimal | None
    strategy_values: tuple[StrategyValue, ...]
    exceptions: tuple[ReconciliationException, ...]

    def to_dict(self) -> dict[str, object]:
        def rendered(value: Decimal | None) -> str | None:
            return decimal_text(value) if value is not None else None

        return {
            "account_id": self.account_id,
            "account_position_value": rendered(self.account_position_value),
            "as_of_date": self.as_of_date.isoformat(),
            "currency": self.currency,
            "exceptions": [item.to_dict() for item in self.exceptions],
            "mapped_strategy_value": rendered(self.mapped_strategy_value),
            "nav_difference": rendered(self.nav_difference),
            "nav_value": rendered(self.nav_value),
            "status": self.status.value,
            "strategy_values": [item.to_dict() for item in self.strategy_values],
        }


def reconcile_position_values(
    ledger: LedgerSlice, as_of_date: date
) -> tuple[AccountPositionReconciliation, ...]:
    """Reconcile position values to strategy totals and the same-day account NAV.

    This is exact arithmetic, not a valuation policy. Missing values or mappings
    stay explicit, and a non-zero NAV difference remains a blocking difference.
    """

    accounts = {item.account_id: item for item in ledger.accounts}
    nav_by_account = {
        item.account_id: item
        for item in ledger.nav_observations
        if item.as_of_date == as_of_date
    }
    results: list[AccountPositionReconciliation] = []
    for account_id in sorted(accounts):
        account = accounts[account_id]
        positions = tuple(
            item
            for item in ledger.positions
            if item.account_id == account_id and item.as_of_date == as_of_date
        )
        exceptions: list[ReconciliationException] = []
        unavailable = tuple(
            item.record_id
            for item in positions
            if item.market_value.status is ValueStatus.UNAVAILABLE
        )
        unmapped = tuple(
            item.record_id
            for item in positions
            if item.mapping_status is MappingStatus.UNMAPPED
        )
        if unavailable:
            exceptions.append(
                ReconciliationException(
                    code="UNAVAILABLE_POSITION_VALUE",
                    detail="At least one position has no accepted market value.",
                    record_ids=unavailable,
                )
            )
        if unmapped:
            exceptions.append(
                ReconciliationException(
                    code="UNMAPPED_STRATEGY",
                    detail="At least one investment position lacks an accepted strategy mapping.",
                    record_ids=unmapped,
                )
            )

        all_values_available = not unavailable
        account_value: Decimal | None = None
        if all_values_available:
            account_value = Decimal("0")
            for position in positions:
                if position.market_value.value is None:
                    raise LedgerValidationError(
                        "AVAILABLE position unexpectedly has no Decimal value"
                    )
                account_value += position.market_value.value

        strategy_totals: dict[str, Decimal] = {}
        if all_values_available:
            for position in positions:
                if position.strategy_id is not None:
                    assert position.market_value.value is not None
                    strategy_totals[position.strategy_id] = (
                        strategy_totals.get(position.strategy_id, Decimal("0"))
                        + position.market_value.value
                    )
        strategy_values = tuple(
            StrategyValue(strategy_id=identifier, value=strategy_totals[identifier])
            for identifier in sorted(strategy_totals)
        )
        mapped_value = (
            sum((item.value for item in strategy_values), Decimal("0"))
            if all_values_available
            else None
        )

        nav = nav_by_account.get(account_id)
        nav_value: Decimal | None = None
        if nav is None or nav.nav.status is ValueStatus.UNAVAILABLE:
            record_ids = () if nav is None else (nav.record_id,)
            exceptions.append(
                ReconciliationException(
                    code="UNAVAILABLE_NAV",
                    detail="No accepted same-day account NAV is available.",
                    record_ids=record_ids,
                )
            )
        else:
            nav_value = nav.nav.value

        nav_difference = (
            account_value - nav_value
            if account_value is not None and nav_value is not None
            else None
        )
        if nav_difference not in {None, Decimal("0")}:
            exceptions.append(
                ReconciliationException(
                    code="ACCOUNT_NAV_DIFFERENCE",
                    detail="Position value does not equal the accepted same-day NAV.",
                    record_ids=tuple(item.record_id for item in positions)
                    + ((nav.record_id,) if nav is not None else ()),
                )
            )

        if unavailable or nav_value is None:
            status = ReconciliationStatus.UNAVAILABLE
        elif unmapped and account.role is AccountRole.INVESTMENT:
            status = ReconciliationStatus.UNMAPPED
        elif nav_difference != Decimal("0"):
            status = ReconciliationStatus.DIFFERENCE
        else:
            status = ReconciliationStatus.RECONCILED

        results.append(
            AccountPositionReconciliation(
                account_id=account_id,
                as_of_date=as_of_date,
                currency=account.base_currency,
                status=status,
                account_position_value=account_value,
                mapped_strategy_value=mapped_value,
                nav_value=nav_value,
                nav_difference=nav_difference,
                strategy_values=strategy_values,
                exceptions=tuple(exceptions),
            )
        )
    return tuple(results)

"""Effective-dated account, capital-pool, and allocation-decision bridge."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from quant_engine.portfolio_ledger.models import (
    Account,
    AccountRole,
    LedgerValidationError,
    decimal_text,
)


class CapitalPoolRole(StrEnum):
    SYSTEMATIC = "SYSTEMATIC"
    RESERVE = "RESERVE"


class StrategyMappingDisposition(StrEnum):
    STRATEGY_MAPPED = "STRATEGY_MAPPED"
    CURRENT_POOL_ONLY = "CURRENT_POOL_ONLY"
    HISTORICAL_POOL_ONLY = "HISTORICAL_POOL_ONLY"


def _text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise LedgerValidationError(f"{name} must not be empty")
    return normalized


def _weight(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 < value <= 1:
        raise LedgerValidationError(f"{name} must be a Decimal in (0, 1]")
    return value


@dataclass(frozen=True)
class CapitalPool:
    pool_id: str
    name: str
    base_currency: str
    role: CapitalPoolRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", _text(self.pool_id, "pool_id"))
        object.__setattr__(self, "name", _text(self.name, "pool.name"))
        currency = self.base_currency.strip()
        if len(currency) != 3 or currency.upper() != currency:
            raise LedgerValidationError("pool currency must be three uppercase letters")


@dataclass(frozen=True)
class AccountPoolAssignment:
    record_id: str
    account_id: str
    pool_id: str
    effective_start: date | None
    effective_end: date | None
    weight: Decimal

    def __post_init__(self) -> None:
        for field in ("record_id", "account_id", "pool_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        _weight(self.weight, "account-pool weight")
        if (
            self.effective_start is not None
            and self.effective_end is not None
            and self.effective_end < self.effective_start
        ):
            raise LedgerValidationError("account-pool interval is reversed")

    def active_on(self, day: date) -> bool:
        return not (
            (self.effective_start is not None and day < self.effective_start)
            or (self.effective_end is not None and day > self.effective_end)
        )


@dataclass(frozen=True)
class AllocationDecision:
    record_id: str
    pool_id: str
    strategy_id: str | None
    target_weight: Decimal
    source_effective_date: date
    valuation_date: date
    publication_date: date
    effective_end: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _text(self.record_id, "record_id"))
        object.__setattr__(self, "pool_id", _text(self.pool_id, "pool_id"))
        if self.strategy_id is not None:
            object.__setattr__(
                self, "strategy_id", _text(self.strategy_id, "strategy_id")
            )
        _weight(self.target_weight, "allocation target_weight")
        if self.valuation_date < self.source_effective_date:
            raise LedgerValidationError("valuation date precedes source-effective date")
        if self.publication_date < self.valuation_date:
            raise LedgerValidationError("publication date precedes valuation date")
        if self.effective_end is not None and self.effective_end < self.valuation_date:
            raise LedgerValidationError("allocation effective interval is reversed")

    def active_on(self, day: date) -> bool:
        return self.valuation_date <= day and (
            self.effective_end is None or day <= self.effective_end
        )


@dataclass(frozen=True)
class AllocationBridgeReconciliation:
    as_of_date: date
    account_total: Decimal
    pool_total: Decimal
    systematic_total: Decimal
    reserve_total: Decimal
    strategy_values: tuple[tuple[str, Decimal], ...]

    @property
    def reconciled(self) -> bool:
        return (
            self.account_total
            == self.pool_total
            == (self.systematic_total + self.reserve_total)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_total": decimal_text(self.account_total),
            "as_of_date": self.as_of_date.isoformat(),
            "pool_total": decimal_text(self.pool_total),
            "reconciled": self.reconciled,
            "reserve_total": decimal_text(self.reserve_total),
            "strategy_values": [
                {"strategy_id": key, "value": decimal_text(value)}
                for key, value in self.strategy_values
            ],
            "systematic_total": decimal_text(self.systematic_total),
        }


def reconcile_allocation_bridge(
    *,
    as_of_date: date,
    accounts: Iterable[Account],
    pools: Iterable[CapitalPool],
    account_values: Mapping[str, Decimal],
    account_pool_assignments: Iterable[AccountPoolAssignment],
    allocation_decisions: Iterable[AllocationDecision],
) -> AllocationBridgeReconciliation:
    """Require a complete current account-to-pool-to-strategy/reserve identity."""

    account_by_id = {item.account_id: item for item in accounts}
    pool_by_id = {item.pool_id: item for item in pools}
    if set(account_values) != set(account_by_id):
        raise LedgerValidationError("account values differ from the account boundary")
    for value in account_values.values():
        if not isinstance(value, Decimal) or not value.is_finite():
            raise LedgerValidationError("account values must be finite Decimals")

    pool_values = {pool_id: Decimal("0") for pool_id in pool_by_id}
    assignments = tuple(account_pool_assignments)
    for account_id, account in account_by_id.items():
        active = [
            item
            for item in assignments
            if item.account_id == account_id and item.active_on(as_of_date)
        ]
        if not active or sum((item.weight for item in active), Decimal("0")) != 1:
            raise LedgerValidationError("active account-pool weights must sum to one")
        for assignment in active:
            pool = pool_by_id.get(assignment.pool_id)
            if pool is None:
                raise LedgerValidationError(
                    "account assignment references unknown pool"
                )
            expected_role = (
                CapitalPoolRole.RESERVE
                if account.role is AccountRole.RESERVE
                else CapitalPoolRole.SYSTEMATIC
            )
            if pool.role is not expected_role:
                raise LedgerValidationError("account role conflicts with capital pool")
            pool_values[pool.pool_id] += account_values[account_id] * assignment.weight

    decisions = tuple(
        item for item in allocation_decisions if item.active_on(as_of_date)
    )
    strategy_values: dict[str, Decimal] = {}
    systematic = Decimal("0")
    reserve = Decimal("0")
    for pool_id, pool in pool_by_id.items():
        decisions_for_pool = [item for item in decisions if item.pool_id == pool_id]
        if (
            not decisions_for_pool
            or sum((item.target_weight for item in decisions_for_pool), Decimal("0"))
            != 1
        ):
            raise LedgerValidationError(
                "active pool allocation weights must sum to one"
            )
        for decision in decisions_for_pool:
            value = pool_values[pool_id] * decision.target_weight
            if pool.role is CapitalPoolRole.RESERVE:
                if decision.strategy_id is not None:
                    raise LedgerValidationError(
                        "reserve allocation cannot name a strategy"
                    )
                reserve += value
            else:
                if decision.strategy_id is None:
                    raise LedgerValidationError(
                        "systematic allocation requires a strategy"
                    )
                systematic += value
                strategy_values[decision.strategy_id] = (
                    strategy_values.get(decision.strategy_id, Decimal("0")) + value
                )
    result = AllocationBridgeReconciliation(
        as_of_date=as_of_date,
        account_total=sum(account_values.values(), Decimal("0")),
        pool_total=sum(pool_values.values(), Decimal("0")),
        systematic_total=systematic,
        reserve_total=reserve,
        strategy_values=tuple(sorted(strategy_values.items())),
    )
    if not result.reconciled:
        raise LedgerValidationError("allocation bridge totals do not reconcile")
    return result

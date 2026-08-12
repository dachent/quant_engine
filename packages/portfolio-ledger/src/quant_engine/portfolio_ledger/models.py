"""Minimal immutable records for the canonical portfolio ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import re
from typing import TypeVar

from quant_engine.core import GenerationManifest


_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


class LedgerValidationError(ValueError):
    """A ledger record or aggregate violates the canonical contract."""


class AccountRole(StrEnum):
    """The only account roles needed by the first portfolio baseline."""

    INVESTMENT = "INVESTMENT"
    RESERVE = "RESERVE"


class ValueStatus(StrEnum):
    """Whether a reported amount exists in the accepted source evidence."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class MappingStatus(StrEnum):
    """Whether an account fact has an accepted strategy identity."""

    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise LedgerValidationError(f"{field_name} must not be empty")
    return normalized


def _currency(value: str, field_name: str) -> str:
    normalized = value.strip()
    if _CURRENCY.fullmatch(normalized) is None:
        raise LedgerValidationError(
            f"{field_name} must be a three-letter uppercase currency code"
        )
    return normalized


def _decimal(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LedgerValidationError(f"{field_name} must be a finite Decimal")
    return value


def decimal_text(value: Decimal) -> str:
    """Return one deterministic, non-exponent decimal representation."""

    checked = _decimal(value, "decimal")
    rendered = format(checked, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


@dataclass(frozen=True)
class ObservedAmount:
    """A source-observed money amount or an explicit absence."""

    currency: str
    status: ValueStatus
    value: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))
        if self.status is ValueStatus.AVAILABLE:
            if self.value is None:
                raise LedgerValidationError("AVAILABLE amount requires a value")
            _decimal(self.value, "amount.value")
        elif self.value is not None:
            raise LedgerValidationError("UNAVAILABLE amount cannot contain a value")

    @classmethod
    def available(cls, value: Decimal, currency: str) -> ObservedAmount:
        return cls(currency=currency, status=ValueStatus.AVAILABLE, value=value)

    @classmethod
    def unavailable(cls, currency: str) -> ObservedAmount:
        return cls(currency=currency, status=ValueStatus.UNAVAILABLE, value=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "status": self.status.value,
            "value": decimal_text(self.value) if self.value is not None else None,
        }


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    base_currency: str
    role: AccountRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "name", _text(self.name, "account.name"))
        object.__setattr__(
            self, "base_currency", _currency(self.base_currency, "base_currency")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "base_currency": self.base_currency,
            "name": self.name,
            "role": self.role.value,
        }


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _text(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "name", _text(self.name, "strategy.name"))

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "strategy_id": self.strategy_id}


@dataclass(frozen=True)
class Security:
    security_id: str
    name: str
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "security_id", _text(self.security_id, "security_id"))
        object.__setattr__(self, "name", _text(self.name, "security.name"))
        object.__setattr__(self, "currency", _currency(self.currency, "currency"))

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "name": self.name,
            "security_id": self.security_id,
        }


@dataclass(frozen=True)
class AccountStrategyAssignment:
    record_id: str
    account_id: str
    strategy_id: str
    effective_start: date
    effective_end: date | None
    weight: Decimal

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id", "strategy_id"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        _decimal(self.weight, "assignment.weight")
        if self.weight <= 0 or self.weight > 1:
            raise LedgerValidationError(
                "assignment.weight must be greater than 0 and at most 1"
            )
        if self.effective_end is not None and self.effective_end < self.effective_start:
            raise LedgerValidationError("assignment effective interval is reversed")

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "effective_end": (
                self.effective_end.isoformat()
                if self.effective_end is not None
                else None
            ),
            "effective_start": self.effective_start.isoformat(),
            "record_id": self.record_id,
            "strategy_id": self.strategy_id,
            "weight": decimal_text(self.weight),
        }


def _validate_strategy_mapping(
    mapping_status: MappingStatus, strategy_id: str | None
) -> str | None:
    if strategy_id is not None:
        strategy_id = _text(strategy_id, "strategy_id")
    if mapping_status is MappingStatus.MAPPED and strategy_id is None:
        raise LedgerValidationError("MAPPED fact requires strategy_id")
    if mapping_status is not MappingStatus.MAPPED and strategy_id is not None:
        raise LedgerValidationError(
            f"{mapping_status.value} fact cannot contain strategy_id"
        )
    return strategy_id


@dataclass(frozen=True)
class PositionSnapshot:
    record_id: str
    account_id: str
    security_id: str
    as_of_date: date
    quantity: Decimal
    market_value: ObservedAmount
    mapping_status: MappingStatus
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id", "security_id"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        _decimal(self.quantity, "position.quantity")
        object.__setattr__(
            self,
            "strategy_id",
            _validate_strategy_mapping(self.mapping_status, self.strategy_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "as_of_date": self.as_of_date.isoformat(),
            "mapping_status": self.mapping_status.value,
            "market_value": self.market_value.to_dict(),
            "quantity": decimal_text(self.quantity),
            "record_id": self.record_id,
            "security_id": self.security_id,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class Transaction:
    record_id: str
    account_id: str
    event_date: date
    kind: str
    amount: ObservedAmount
    mapping_status: MappingStatus
    strategy_id: str | None = None
    security_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id", "kind"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        if self.security_id is not None:
            object.__setattr__(
                self, "security_id", _text(self.security_id, "security_id")
            )
        object.__setattr__(
            self,
            "strategy_id",
            _validate_strategy_mapping(self.mapping_status, self.strategy_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "amount": self.amount.to_dict(),
            "event_date": self.event_date.isoformat(),
            "kind": self.kind,
            "mapping_status": self.mapping_status.value,
            "record_id": self.record_id,
            "security_id": self.security_id,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class CashFlow:
    record_id: str
    account_id: str
    event_date: date
    flow_type: str
    amount: ObservedAmount
    mapping_status: MappingStatus
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id", "flow_type"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "strategy_id",
            _validate_strategy_mapping(self.mapping_status, self.strategy_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "amount": self.amount.to_dict(),
            "event_date": self.event_date.isoformat(),
            "flow_type": self.flow_type,
            "mapping_status": self.mapping_status.value,
            "record_id": self.record_id,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class CorporateAction:
    """A source-observed security event without provider-specific semantics."""

    record_id: str
    account_id: str
    event_date: date
    kind: str
    amount: ObservedAmount
    mapping_status: MappingStatus
    strategy_id: str | None = None
    security_id: str | None = None
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id", "kind"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )
        if self.security_id is not None:
            object.__setattr__(
                self, "security_id", _text(self.security_id, "security_id")
            )
        if self.quantity is not None:
            _decimal(self.quantity, "corporate_action.quantity")
        object.__setattr__(
            self,
            "strategy_id",
            _validate_strategy_mapping(self.mapping_status, self.strategy_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "amount": self.amount.to_dict(),
            "event_date": self.event_date.isoformat(),
            "kind": self.kind,
            "mapping_status": self.mapping_status.value,
            "quantity": (
                decimal_text(self.quantity) if self.quantity is not None else None
            ),
            "record_id": self.record_id,
            "security_id": self.security_id,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True)
class NavObservation:
    record_id: str
    account_id: str
    as_of_date: date
    nav: ObservedAmount

    def __post_init__(self) -> None:
        for field_name in ("record_id", "account_id"):
            object.__setattr__(
                self, field_name, _text(getattr(self, field_name), field_name)
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "as_of_date": self.as_of_date.isoformat(),
            "nav": self.nav.to_dict(),
            "record_id": self.record_id,
        }


def _deduplicate(
    values: Iterable[_T], *, key: Callable[[_T], str], object_name: str
) -> tuple[_T, ...]:
    accepted: dict[str, _T] = {}
    for value in values:
        identifier = key(value)
        prior = accepted.get(identifier)
        if prior is not None and prior != value:
            raise LedgerValidationError(
                f"conflicting {object_name} identity: {identifier}"
            )
        accepted[identifier] = value
    return tuple(accepted[identifier] for identifier in sorted(accepted))


def _logical_unique(
    values: Iterable[_T], *, key: Callable[[_T], object], object_name: str
) -> None:
    observed: dict[object, _T] = {}
    for value in values:
        logical_key = key(value)
        if logical_key in observed:
            raise LedgerValidationError(f"multiple authoritative {object_name} records")
        observed[logical_key] = value


def _validate_assignment_intervals(
    accounts: Mapping[str, Account], assignments: tuple[AccountStrategyAssignment, ...]
) -> None:
    by_account: dict[str, list[AccountStrategyAssignment]] = {}
    for assignment in assignments:
        account = accounts[assignment.account_id]
        if account.role is AccountRole.RESERVE:
            raise LedgerValidationError(
                "reserve accounts cannot have strategy assignments"
            )
        by_account.setdefault(assignment.account_id, []).append(assignment)

    for account_id, records in by_account.items():
        by_strategy: dict[str, list[AccountStrategyAssignment]] = {}
        boundaries: set[date] = {record.effective_start for record in records}
        for record in records:
            by_strategy.setdefault(record.strategy_id, []).append(record)
            if record.effective_end is not None and record.effective_end < date.max:
                boundaries.add(record.effective_end + timedelta(days=1))
        for strategy_records in by_strategy.values():
            ordered = sorted(strategy_records, key=lambda item: item.effective_start)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if (
                    previous.effective_end is None
                    or current.effective_start <= previous.effective_end
                ):
                    raise LedgerValidationError(
                        f"overlapping assignment intervals for account {account_id}"
                    )
        for boundary in sorted(boundaries):
            active = [
                record
                for record in records
                if record.effective_start <= boundary
                and (record.effective_end is None or boundary <= record.effective_end)
            ]
            if active and sum(
                (record.weight for record in active), Decimal("0")
            ) != Decimal("1"):
                raise LedgerValidationError(
                    f"active assignment weights for account {account_id} must sum to 1"
                )


@dataclass(frozen=True)
class LedgerSlice:
    """One deterministic, provenance-bound portfolio fact slice."""

    as_of_date: date
    source_generation_ids: tuple[str, ...]
    accounts: tuple[Account, ...]
    strategies: tuple[Strategy, ...]
    securities: tuple[Security, ...]
    assignments: tuple[AccountStrategyAssignment, ...]
    positions: tuple[PositionSnapshot, ...]
    transactions: tuple[Transaction, ...]
    cash_flows: tuple[CashFlow, ...]
    corporate_actions: tuple[CorporateAction, ...]
    nav_observations: tuple[NavObservation, ...]

    @classmethod
    def build(
        cls,
        *,
        as_of_date: date,
        source_generations: Iterable[GenerationManifest],
        accounts: Iterable[Account],
        strategies: Iterable[Strategy],
        securities: Iterable[Security],
        assignments: Iterable[AccountStrategyAssignment] = (),
        positions: Iterable[PositionSnapshot] = (),
        transactions: Iterable[Transaction] = (),
        cash_flows: Iterable[CashFlow] = (),
        corporate_actions: Iterable[CorporateAction] = (),
        nav_observations: Iterable[NavObservation] = (),
    ) -> LedgerSlice:
        generations = _deduplicate(
            source_generations,
            key=lambda item: item.generation_id,
            object_name="source generation",
        )
        if not generations:
            raise LedgerValidationError(
                "ledger requires at least one source generation"
            )
        accepted_accounts = _deduplicate(
            accounts, key=lambda item: item.account_id, object_name="account"
        )
        accepted_strategies = _deduplicate(
            strategies, key=lambda item: item.strategy_id, object_name="strategy"
        )
        accepted_securities = _deduplicate(
            securities, key=lambda item: item.security_id, object_name="security"
        )
        accepted_assignments = _deduplicate(
            assignments, key=lambda item: item.record_id, object_name="assignment"
        )
        accepted_positions = _deduplicate(
            positions, key=lambda item: item.record_id, object_name="position"
        )
        accepted_transactions = _deduplicate(
            transactions, key=lambda item: item.record_id, object_name="transaction"
        )
        accepted_flows = _deduplicate(
            cash_flows, key=lambda item: item.record_id, object_name="cash flow"
        )
        accepted_actions = _deduplicate(
            corporate_actions,
            key=lambda item: item.record_id,
            object_name="corporate action",
        )
        accepted_nav = _deduplicate(
            nav_observations, key=lambda item: item.record_id, object_name="NAV"
        )

        account_map = {item.account_id: item for item in accepted_accounts}
        strategy_ids = {item.strategy_id for item in accepted_strategies}
        security_ids = {item.security_id for item in accepted_securities}

        def require_account(account_id: str) -> Account:
            try:
                return account_map[account_id]
            except KeyError as exc:
                raise LedgerValidationError(
                    f"fact references unknown account: {account_id}"
                ) from exc

        def validate_mapping(
            account: Account, mapping_status: MappingStatus, strategy_id: str | None
        ) -> None:
            if account.role is AccountRole.RESERVE:
                if mapping_status is not MappingStatus.NOT_APPLICABLE:
                    raise LedgerValidationError(
                        "reserve account facts must use NOT_APPLICABLE strategy mapping"
                    )
            elif mapping_status is MappingStatus.NOT_APPLICABLE:
                raise LedgerValidationError(
                    "investment account facts cannot use NOT_APPLICABLE strategy mapping"
                )
            if strategy_id is not None and strategy_id not in strategy_ids:
                raise LedgerValidationError(
                    f"fact references unknown strategy: {strategy_id}"
                )

        for assignment in accepted_assignments:
            require_account(assignment.account_id)
            if assignment.strategy_id not in strategy_ids:
                raise LedgerValidationError(
                    f"assignment references unknown strategy: {assignment.strategy_id}"
                )
        _validate_assignment_intervals(account_map, accepted_assignments)

        for position in accepted_positions:
            account = require_account(position.account_id)
            if position.security_id not in security_ids:
                raise LedgerValidationError(
                    f"position references unknown security: {position.security_id}"
                )
            if position.market_value.currency != account.base_currency:
                raise LedgerValidationError(
                    "position market value currency must equal account base currency"
                )
            validate_mapping(account, position.mapping_status, position.strategy_id)
            if position.as_of_date > as_of_date:
                raise LedgerValidationError("position is later than ledger as_of_date")
        _logical_unique(
            accepted_positions,
            key=lambda item: (item.account_id, item.security_id, item.as_of_date),
            object_name="account/security/date position",
        )

        for transaction in accepted_transactions:
            account = require_account(transaction.account_id)
            validate_mapping(
                account, transaction.mapping_status, transaction.strategy_id
            )
            if (
                transaction.security_id is not None
                and transaction.security_id not in security_ids
            ):
                raise LedgerValidationError(
                    f"transaction references unknown security: {transaction.security_id}"
                )
            if transaction.event_date > as_of_date:
                raise LedgerValidationError(
                    "transaction is later than ledger as_of_date"
                )

        for flow in accepted_flows:
            account = require_account(flow.account_id)
            validate_mapping(account, flow.mapping_status, flow.strategy_id)
            if flow.event_date > as_of_date:
                raise LedgerValidationError("cash flow is later than ledger as_of_date")

        for action in accepted_actions:
            account = require_account(action.account_id)
            validate_mapping(account, action.mapping_status, action.strategy_id)
            if (
                action.security_id is not None
                and action.security_id not in security_ids
            ):
                raise LedgerValidationError(
                    f"corporate action references unknown security: {action.security_id}"
                )
            if action.event_date > as_of_date:
                raise LedgerValidationError(
                    "corporate action is later than ledger as_of_date"
                )

        for nav in accepted_nav:
            account = require_account(nav.account_id)
            if nav.nav.currency != account.base_currency:
                raise LedgerValidationError(
                    "NAV currency must equal account base currency"
                )
            if nav.as_of_date > as_of_date:
                raise LedgerValidationError("NAV is later than ledger as_of_date")
        _logical_unique(
            accepted_nav,
            key=lambda item: (item.account_id, item.as_of_date),
            object_name="account/date NAV",
        )

        return cls(
            as_of_date=as_of_date,
            source_generation_ids=tuple(
                sorted(item.generation_id for item in generations)
            ),
            accounts=accepted_accounts,
            strategies=accepted_strategies,
            securities=accepted_securities,
            assignments=accepted_assignments,
            positions=accepted_positions,
            transactions=accepted_transactions,
            cash_flows=accepted_flows,
            corporate_actions=accepted_actions,
            nav_observations=accepted_nav,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "accounts": [item.to_dict() for item in self.accounts],
            "as_of_date": self.as_of_date.isoformat(),
            "assignments": [item.to_dict() for item in self.assignments],
            "cash_flows": [item.to_dict() for item in self.cash_flows],
            "corporate_actions": [item.to_dict() for item in self.corporate_actions],
            "nav_observations": [item.to_dict() for item in self.nav_observations],
            "positions": [item.to_dict() for item in self.positions],
            "securities": [item.to_dict() for item in self.securities],
            "source_generation_ids": list(self.source_generation_ids),
            "strategies": [item.to_dict() for item in self.strategies],
            "transactions": [item.to_dict() for item in self.transactions],
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            + "\n"
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

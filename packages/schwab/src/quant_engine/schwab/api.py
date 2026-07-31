"""Narrow read-only adapter over an authenticated Schwab SDK client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Protocol

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    MappingStatus,
    ObservedAmount,
    PositionSnapshot,
)


class SchwabReadError(ValueError):
    """A read response is incomplete, ambiguous, or outside the account allowlist."""


class ResponseLike(Protocol):
    """The response surface used from httpx-backed SDK methods."""

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class SdkReadClient(Protocol):
    """Only the two SDK calls admitted by the current-state adapter."""

    def get_account_numbers(self) -> ResponseLike: ...

    def get_account(
        self, account_hash: str, *, fields: Sequence[str] | None = None
    ) -> ResponseLike: ...


@dataclass(frozen=True)
class CurrentAccount:
    """Current account balances identified only by the internal account ID."""

    account_id: str
    cash_available: ObservedAmount
    liquidation_value: ObservedAmount
    observed_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "cash_available": self.cash_available.to_dict(),
            "liquidation_value": self.liquidation_value.to_dict(),
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True)
class CurrentSnapshot:
    """One atomic read-side snapshot with explicit sweep exclusions."""

    accounts: tuple[CurrentAccount, ...]
    positions: tuple[dict[str, object], ...]
    observed_at: datetime
    sweep_vehicle_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "accounts": [item.to_dict() for item in self.accounts],
            "observed_at": self.observed_at.isoformat(),
            "positions": list(self.positions),
            "sweep_vehicle_count": self.sweep_vehicle_count,
        }


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SchwabReadError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SchwabReadError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchwabReadError(f"{name} must be nonempty text")
    return value.strip()


def _decimal(value: object, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise SchwabReadError(f"{name} is not decimal") from exc
    if not parsed.is_finite():
        raise SchwabReadError(f"{name} must be finite")
    return parsed


def _amount(value: object, name: str) -> ObservedAmount:
    parsed = _decimal(value, name)
    return (
        ObservedAmount.available(parsed, "USD")
        if parsed is not None
        else ObservedAmount.unavailable("USD")
    )


def _response(response: ResponseLike, name: str) -> object:
    try:
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise SchwabReadError(f"{name} failed") from exc


def _security_id(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def read_current_snapshot(
    client: SdkReadClient,
    *,
    crosswalk: Mapping[str, str],
    observed_at: datetime,
) -> CurrentSnapshot:
    """Read current balances and positions without exposing provider account IDs."""

    if observed_at.tzinfo is None:
        raise SchwabReadError("observed_at must be timezone-aware")
    if not crosswalk or len(crosswalk) != len(set(crosswalk.values())):
        raise SchwabReadError("crosswalk must be nonempty and one-to-one")
    linked = _objects(
        _response(client.get_account_numbers(), "linked accounts"), "linked"
    )
    account_hashes: dict[str, str] = {}
    for record in linked:
        external = _text(record.get("accountNumber"), "linked account")
        account_hash = _text(record.get("hashValue"), "linked account hash")
        if external not in crosswalk:
            raise SchwabReadError("linked account is outside the configured allowlist")
        if external in account_hashes or account_hash in account_hashes.values():
            raise SchwabReadError("linked account identity is duplicated")
        account_hashes[external] = account_hash
    if set(account_hashes) != set(crosswalk):
        raise SchwabReadError("configured and linked account scopes differ")

    accounts: list[CurrentAccount] = []
    positions: list[dict[str, object]] = []
    sweep_vehicle_count = 0
    for external in sorted(account_hashes, key=lambda value: crosswalk[value]):
        account_hash = account_hashes[external]
        details = _object(
            _response(
                client.get_account(account_hash, fields=["positions"]),
                "account snapshot",
            ),
            "account snapshot",
        )
        account = _object(details.get("securitiesAccount"), "securities account")
        observed_external = account.get("accountNumber")
        if observed_external is not None and observed_external != external:
            raise SchwabReadError("account response scope differs")
        balances = _object(account.get("currentBalances"), "current balances")
        cash_balance = _decimal(balances.get("cashBalance"), "cash balance")
        available = _decimal(
            balances.get("cashAvailableForTrading"), "cash available for trading"
        )
        available_cash = [
            item for item in (cash_balance, available) if item is not None
        ]
        cash = max(available_cash) if available_cash else None
        internal = crosswalk[external]
        accounts.append(
            CurrentAccount(
                account_id=internal,
                cash_available=(
                    ObservedAmount.available(cash, "USD")
                    if cash is not None
                    else ObservedAmount.unavailable("USD")
                ),
                liquidation_value=_amount(
                    balances.get("liquidationValue"), "liquidation value"
                ),
                observed_at=observed_at,
            )
        )
        raw_positions = account.get("positions", [])
        for raw in _objects(raw_positions, "positions"):
            instrument = _object(raw.get("instrument"), "position instrument")
            if instrument.get("type") == "SWEEP_VEHICLE":
                sweep_vehicle_count += 1
                continue
            symbol = _text(instrument.get("symbol"), "position symbol")
            long_quantity = _decimal(
                raw.get("longQuantity"), "long quantity"
            ) or Decimal(0)
            short_quantity = _decimal(
                raw.get("shortQuantity"), "short quantity"
            ) or Decimal(0)
            quantity = long_quantity - short_quantity
            market_value = _decimal(raw.get("marketValue"), "market value")
            identity = {
                "account_id": internal,
                "as_of_date": observed_at.date().isoformat(),
                "security_id": _security_id(symbol),
            }
            record = PositionSnapshot(
                record_id=(
                    f"position-{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24]}"
                ),
                account_id=internal,
                security_id=_security_id(symbol),
                as_of_date=observed_at.date(),
                quantity=quantity,
                market_value=(
                    ObservedAmount.available(market_value, "USD")
                    if market_value is not None
                    else ObservedAmount.unavailable("USD")
                ),
                mapping_status=MappingStatus.UNMAPPED,
            ).to_dict()
            record.update(
                {
                    "average_price": (
                        str(value)
                        if (value := _decimal(raw.get("averagePrice"), "average price"))
                        is not None
                        else None
                    ),
                    "evidence_class": "DEVELOPER_API_CURRENT",
                    "observed_at": observed_at.isoformat(),
                    "symbol": symbol,
                }
            )
            positions.append(record)
    accounts.sort(key=lambda item: item.account_id)
    positions.sort(key=lambda item: (str(item["account_id"]), str(item["security_id"])))
    return CurrentSnapshot(
        accounts=tuple(accounts),
        positions=tuple(positions),
        observed_at=observed_at,
        sweep_vehicle_count=sweep_vehicle_count,
    )

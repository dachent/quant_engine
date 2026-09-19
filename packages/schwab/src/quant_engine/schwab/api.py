"""Narrow read-only adapter over an authenticated Schwab SDK client."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
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
    transport: str

    def to_dict(self) -> dict[str, object]:
        return {
            "accounts": [item.to_dict() for item in self.accounts],
            "observed_at": self.observed_at.isoformat(),
            "positions": list(self.positions),
            "sweep_vehicle_count": self.sweep_vehicle_count,
            "transport": self.transport,
        }


CURRENT_SNAPSHOT_SCHEMA_VERSION = 2

_HOLDINGS_V2_GROUP_KINDS = {
    ("equity", 1): "position",
    ("etf", 2): "position",
    ("cash", 9): "cash",
}


def current_snapshot_bytes(snapshot: CurrentSnapshot) -> bytes:
    """Serialize one sanitized, provider-ID-free current snapshot."""

    return canonical_json_bytes(
        {
            **snapshot.to_dict(),
            "provider": "SCHWAB",
            "schema_version": CURRENT_SNAPSHOT_SCHEMA_VERSION,
        }
    )


def write_current_snapshot(snapshot: CurrentSnapshot, path: Path) -> tuple[str, int]:
    """Write an immutable sanitized snapshot and return its hash and size."""

    payload = current_snapshot_bytes(snapshot)
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise SchwabReadError("snapshot destination must have a real parent directory")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise SchwabReadError("snapshot destination conflicts with existing bytes")
    else:
        try:
            with path.open("xb") as handle:
                handle.write(payload)
        except OSError as exc:
            raise SchwabReadError("snapshot write failed") from exc
    return hashlib.sha256(payload).hexdigest(), len(payload)


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


def _aware_datetime(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchwabReadError(f"{name} is not an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchwabReadError(f"{name} must be timezone-aware")
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
        transport="DEVELOPER_API",
    )


def read_web_current_snapshot(
    document: object,
    *,
    crosswalk: Mapping[str, str],
) -> CurrentSnapshot:
    """Normalize one reviewed Schwab all-accounts web position response.

    The response proves current non-cash position rows but does not prove
    current account liquidation or cash balances. Those amounts therefore
    remain explicitly unavailable.
    """

    if not crosswalk or len(crosswalk) != len(set(crosswalk.values())):
        raise SchwabReadError("crosswalk must be nonempty and one-to-one")
    payload = _object(document, "web current positions")
    groups = _objects(payload.get("securityGroupings"), "security groupings")
    declared_count = payload.get("positionCount")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count < 1
    ):
        raise SchwabReadError("web position count is invalid")

    observed_accounts: set[str] = set()
    identities: set[tuple[str, str]] = set()
    timestamps: list[datetime] = []
    accepted: list[tuple[str, str, Decimal, Decimal | None, Decimal | None]] = []
    sweep_vehicle_count = 0
    for group in groups:
        security_type = _text(group.get("securityType"), "security type")
        for raw in _objects(group.get("positions"), "web positions"):
            raw_accounts = _objects(raw.get("accounts"), "web position accounts")
            if security_type == "CashAndMoneyMarket":
                sweep_vehicle_count += len(raw_accounts)
                continue
            symbol_value = (
                raw.get("displaySymbol")
                or raw.get("defaultSymbol")
                or raw.get("quoteSymbol")
            )
            symbol = _text(symbol_value, "web position symbol")
            for raw_account in raw_accounts:
                external = _text(
                    raw_account.get("accountNumberDisplayFull"),
                    "web position account",
                )
                if external not in crosswalk:
                    raise SchwabReadError(
                        "web position account is outside the configured allowlist"
                    )
                internal = crosswalk[external]
                identity = (internal, _security_id(symbol))
                if identity in identities:
                    raise SchwabReadError("web position identity is duplicated")
                identities.add(identity)
                observed_accounts.add(external)
                timestamps.append(
                    _aware_datetime(
                        raw_account.get("currentTimeStamp"),
                        "web position observed_at",
                    )
                )
                quantity = _decimal(raw_account.get("quantity"), "web quantity")
                if quantity is None:
                    raise SchwabReadError("web quantity is missing")
                market_value = _decimal(
                    raw_account.get("marketValue"), "web market value"
                )
                average_price = _decimal(
                    raw_account.get("costPerShare"), "web cost per share"
                )
                accepted.append(
                    (internal, symbol, quantity, market_value, average_price)
                )
    if not accepted or not observed_accounts:
        raise SchwabReadError("web response contains no accepted position rows")
    observed_dates = {item.date() for item in timestamps}
    if len(observed_dates) != 1:
        raise SchwabReadError("web position dates differ")
    observed_at = max(timestamps)

    positions: list[dict[str, object]] = []
    for internal, symbol, quantity, market_value, average_price in accepted:
        security_id = _security_id(symbol)
        record_identity = {
            "account_id": internal,
            "as_of_date": observed_at.date().isoformat(),
            "security_id": security_id,
        }
        record = PositionSnapshot(
            record_id=(
                f"position-{hashlib.sha256(canonical_json_bytes(record_identity)).hexdigest()[:24]}"
            ),
            account_id=internal,
            security_id=security_id,
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
                    str(average_price) if average_price is not None else None
                ),
                "evidence_class": "WCH_CURRENT",
                "observed_at": observed_at.isoformat(),
                "symbol": symbol,
            }
        )
        positions.append(record)

    unavailable = ObservedAmount.unavailable("USD")
    accounts = tuple(
        CurrentAccount(
            account_id=internal,
            cash_available=unavailable,
            liquidation_value=unavailable,
            observed_at=observed_at,
        )
        for internal in sorted(crosswalk.values())
    )
    positions.sort(key=lambda item: (str(item["account_id"]), str(item["security_id"])))
    return CurrentSnapshot(
        accounts=accounts,
        positions=tuple(positions),
        observed_at=observed_at,
        sweep_vehicle_count=sweep_vehicle_count,
        transport="WEB_CONTRACT",
    )


def read_holdings_v2_current_snapshot(
    document: object,
    *,
    crosswalk: Mapping[str, str],
    captured_at: object,
) -> CurrentSnapshot:
    """Normalize a retained Holdings-V2 response without inferring currency facts."""

    if not crosswalk or len(crosswalk) != len(set(crosswalk.values())):
        raise SchwabReadError("crosswalk must be nonempty and one-to-one")
    observed_at = _aware_datetime(captured_at, "captured_at")
    if observed_at.utcoffset() != timedelta(0):
        raise SchwabReadError("captured_at must be UTC")

    payload = _object(document, "holdings v2 response")
    observed_accounts: set[str] = set()
    identities: set[tuple[str, str]] = set()
    positions: list[dict[str, object]] = []
    sweep_vehicle_count = 0
    for account in _objects(payload.get("accounts"), "holdings v2 accounts"):
        external = _text(account.get("accountId"), "holdings v2 account")
        if external not in crosswalk:
            raise SchwabReadError(
                "holdings v2 account is outside the configured allowlist"
            )
        if external in observed_accounts:
            raise SchwabReadError("holdings v2 account identity is duplicated")
        observed_accounts.add(external)
        internal = crosswalk[external]
        for group in _objects(
            account.get("groupedPositions"), "holdings v2 position groups"
        ):
            group_name = _text(group.get("groupName"), "holdings v2 group name")
            security_type = group.get("securityType")
            if isinstance(security_type, bool) or not isinstance(security_type, int):
                raise SchwabReadError("holdings v2 group is unsupported")
            group_kind = _HOLDINGS_V2_GROUP_KINDS.get(
                (group_name.casefold(), security_type)
            )
            if group_kind is None:
                raise SchwabReadError("holdings v2 group is unsupported")
            rows = _objects(group.get("holdingsRows"), "holdings v2 holdings rows")
            declared_count = group.get("positionsCount")
            if (
                isinstance(declared_count, bool)
                or not isinstance(declared_count, int)
                or declared_count != len(rows)
            ):
                raise SchwabReadError(
                    "holdings v2 declared count differs from holdings rows"
                )
            if group_kind == "cash":
                sweep_vehicle_count += len(rows)
                continue
            for raw in rows:
                symbol_value = raw.get("dataSymbol")
                if not isinstance(symbol_value, str) or not symbol_value.strip():
                    symbol_value = _object(
                        raw.get("symbol"), "holdings v2 nested symbol"
                    ).get("symbol")
                symbol = _text(symbol_value, "holdings v2 symbol")
                security_id = _security_id(symbol)
                identity = (internal, security_id)
                if identity in identities:
                    raise SchwabReadError("holdings v2 position identity is duplicated")
                identities.add(identity)
                quantity = _decimal(
                    _object(raw.get("qty"), "holdings v2 quantity").get("qty"),
                    "holdings v2 quantity",
                )
                if quantity is None:
                    raise SchwabReadError("holdings v2 quantity is missing")
                record_identity = {
                    "account_id": internal,
                    "as_of_date": observed_at.date().isoformat(),
                    "security_id": security_id,
                }
                record = PositionSnapshot(
                    record_id=(
                        "position-"
                        f"{hashlib.sha256(canonical_json_bytes(record_identity)).hexdigest()[:24]}"
                    ),
                    account_id=internal,
                    security_id=security_id,
                    as_of_date=observed_at.date(),
                    quantity=quantity,
                    market_value=ObservedAmount.unavailable("USD"),
                    mapping_status=MappingStatus.UNMAPPED,
                ).to_dict()
                record.update(
                    {
                        "average_price": None,
                        "evidence_class": "WCH_CURRENT",
                        "observed_at": observed_at.isoformat(),
                        "symbol": symbol,
                    }
                )
                positions.append(record)
    if observed_accounts != set(crosswalk):
        raise SchwabReadError("holdings v2 account scope differs from crosswalk")

    unavailable = ObservedAmount.unavailable("USD")
    accounts = tuple(
        CurrentAccount(
            account_id=internal,
            cash_available=unavailable,
            liquidation_value=unavailable,
            observed_at=observed_at,
        )
        for internal in sorted(crosswalk.values())
    )
    positions.sort(key=lambda item: (str(item["account_id"]), str(item["security_id"])))
    return CurrentSnapshot(
        accounts=accounts,
        positions=tuple(positions),
        observed_at=observed_at,
        sweep_vehicle_count=sweep_vehicle_count,
        transport="WEB_CONTRACT",
    )

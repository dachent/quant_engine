"""Provider-neutral TWS current-state boundary with no socket implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Protocol

from quant_engine.ibkr.flex import FlexValidationError


@dataclass(frozen=True)
class TwsPosition:
    raw_account_id: str
    conid: str
    quantity: Decimal
    market_value_base: Decimal | None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not self.raw_account_id.strip() or not self.conid.strip():
            raise FlexValidationError("TWS position identity must not be empty")
        if not self.quantity.is_finite():
            raise FlexValidationError("TWS position quantity must be finite")
        if (
            self.market_value_base is not None
            and not self.market_value_base.is_finite()
        ):
            raise FlexValidationError("TWS position market value must be finite")
        if self.currency is not None and not self.currency.strip():
            raise FlexValidationError("TWS position currency must not be blank")


@dataclass(frozen=True)
class TwsAccountValue:
    raw_account_id: str
    tag: str
    currency: str
    value: Decimal

    def __post_init__(self) -> None:
        if (
            not self.raw_account_id.strip()
            or not self.tag.strip()
            or not self.currency.strip()
        ):
            raise FlexValidationError("TWS account value identity must not be empty")
        if not self.value.is_finite():
            raise FlexValidationError("TWS account value must be finite")


@dataclass(frozen=True)
class TwsExecution:
    raw_account_id: str
    execution_id: str
    conid: str | None
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.raw_account_id.strip() or not self.execution_id.strip():
            raise FlexValidationError("TWS execution identity must not be empty")
        if self.conid is not None and not self.conid.strip():
            raise FlexValidationError("TWS execution conid must not be blank")
        if not self.quantity.is_finite():
            raise FlexValidationError("TWS execution quantity must be finite")


@dataclass(frozen=True)
class TwsSnapshot:
    observed_at: datetime
    raw_account_ids: tuple[str, ...]
    positions: tuple[TwsPosition, ...]
    executions: tuple[TwsExecution, ...]
    source_sha256: str
    account_values: tuple[TwsAccountValue, ...] = ()
    open_order_count: int = 0
    order_status_count: int = 0

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise FlexValidationError("TWS snapshot timestamp must be timezone-aware")
        if self.observed_at.utcoffset() != timezone.utc.utcoffset(self.observed_at):
            raise FlexValidationError("TWS snapshot timestamp must be UTC")
        if len(self.source_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.source_sha256
        ):
            raise FlexValidationError("TWS snapshot source_sha256 is invalid")
        accounts = tuple(sorted(set(self.raw_account_ids)))
        if not accounts or len(accounts) != len(self.raw_account_ids):
            raise FlexValidationError(
                "TWS snapshot accounts must be unique and nonempty"
            )
        if any(not account.strip() for account in accounts):
            raise FlexValidationError("TWS snapshot account must not be empty")
        visible = set(accounts)
        if any(item.raw_account_id not in visible for item in self.positions):
            raise FlexValidationError("TWS position references an unknown account")
        if any(item.raw_account_id not in visible for item in self.executions):
            raise FlexValidationError("TWS execution references an unknown account")
        if len({item.execution_id for item in self.executions}) != len(self.executions):
            raise FlexValidationError("TWS execution IDs must be unique")
        if self.open_order_count < 0 or self.order_status_count < 0:
            raise FlexValidationError("TWS order evidence counts must be nonnegative")
        if any(not item.raw_account_id.strip() for item in self.account_values):
            raise FlexValidationError("TWS account value scope must not be blank")
        object.__setattr__(self, "raw_account_ids", accounts)


class TwsReadSource(Protocol):
    """A separately authorized implementation may supply one current snapshot."""

    def capture(self) -> TwsSnapshot: ...


@dataclass(frozen=True)
class TwsReconciliation:
    account_identity_status: str
    flex_account_count: int
    flex_only_account_count: int
    flex_as_of: date
    overlap_account_count: int
    snapshot_date: date
    snapshot_account_count: int
    snapshot_only_account_count: int
    execution_overlap_count: int
    position_count: int
    overlap_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "account_identity_status": self.account_identity_status,
            "execution_overlap_count": self.execution_overlap_count,
            "flex_account_count": self.flex_account_count,
            "flex_only_account_count": self.flex_only_account_count,
            "flex_as_of": self.flex_as_of.isoformat(),
            "overlap_account_count": self.overlap_account_count,
            "overlap_status": self.overlap_status,
            "position_count": self.position_count,
            "snapshot_date": self.snapshot_date.isoformat(),
            "snapshot_account_count": self.snapshot_account_count,
            "snapshot_only_account_count": self.snapshot_only_account_count,
        }


def reconcile_tws_snapshot(
    snapshot: TwsSnapshot,
    *,
    accepted_raw_account_ids: frozenset[str],
    flex_execution_ids: frozenset[str],
    flex_as_of: date,
) -> TwsReconciliation:
    """Reconcile only boundaries that are meaningful without live broker state."""

    snapshot_accounts = frozenset(snapshot.raw_account_ids)
    overlap_accounts = snapshot_accounts & accepted_raw_account_ids
    account_identity_status = (
        "PASS"
        if snapshot_accounts == accepted_raw_account_ids
        else "PARTIAL"
        if overlap_accounts
        else "FAILED"
    )
    observed_execution_ids = {item.execution_id for item in snapshot.executions}
    overlap = len(observed_execution_ids & flex_execution_ids)
    snapshot_date = snapshot.observed_at.date()
    overlap_status = (
        "SAME_DAY_EXECUTION_BOUNDARY"
        if snapshot_date == flex_as_of
        else "NO_SAME_DAY_BOUNDARY"
    )
    return TwsReconciliation(
        account_identity_status=account_identity_status,
        flex_account_count=len(accepted_raw_account_ids),
        flex_only_account_count=len(accepted_raw_account_ids - snapshot_accounts),
        flex_as_of=flex_as_of,
        overlap_account_count=len(overlap_accounts),
        snapshot_date=snapshot_date,
        snapshot_account_count=len(snapshot_accounts),
        snapshot_only_account_count=len(snapshot_accounts - accepted_raw_account_ids),
        execution_overlap_count=overlap,
        position_count=len(snapshot.positions),
        overlap_status=overlap_status,
    )


_ACCOUNT_VALUE_TAGS = {
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
    "GrossPositionValue",
    "InitMarginReq",
    "MaintMarginReq",
    "NetLiquidation",
    "TotalCashValue",
}
_REQUEST_NAMES = {
    "account_summary",
    "all_api_open_orders",
    "current_day_executions",
    "positions",
}
_INFORMATIONAL_ERROR_CODES = {2104, 2107, 2158}


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FlexValidationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise FlexValidationError(f"{name} must be a list")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FlexValidationError(f"{name} must be nonempty text")
    return value.strip()


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(_text(value, name))
    except ArithmeticError as exc:
        raise FlexValidationError(f"{name} must be a decimal") from exc
    if not parsed.is_finite():
        raise FlexValidationError(f"{name} must be finite")
    return parsed


def _timestamp(value: object) -> datetime:
    raw = _text(value, "snapshot.captured_at_utc")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FlexValidationError("snapshot timestamp is invalid") from exc
    return parsed


def load_tws_snapshot(path: Path, *, source_sha256: str) -> TwsSnapshot:
    """Load one immutable snapshot produced by the bounded IB Gateway capture."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlexValidationError("TWS snapshot is not valid UTF-8 JSON") from exc
    root = _object(payload, "snapshot")
    if root.get("schema_version") != 1:
        raise FlexValidationError("TWS snapshot schema version is unsupported")
    connection = _object(root.get("connection"), "snapshot.connection")
    if connection.get("connected") is not True:
        raise FlexValidationError("TWS snapshot was not captured from a connection")

    requests = _objects(root.get("request_results"), "snapshot.request_results")
    completed: dict[str, bool] = {}
    for item in requests:
        name = _text(item.get("name"), "snapshot.request.name")
        status = item.get("completed")
        if not isinstance(status, bool) or name in completed:
            raise FlexValidationError("TWS request completion evidence is invalid")
        completed[name] = status
    if set(completed) != _REQUEST_NAMES or not all(completed.values()):
        raise FlexValidationError("TWS snapshot did not complete every bounded read")

    for item in _objects(root.get("errors"), "snapshot.errors"):
        code = item.get("error_code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise FlexValidationError("TWS error code is invalid")
        if code not in _INFORMATIONAL_ERROR_CODES:
            raise FlexValidationError("TWS snapshot contains a blocking provider error")

    managed_raw = root.get("managed_accounts")
    if not isinstance(managed_raw, list):
        raise FlexValidationError("TWS managed accounts must be a list")
    managed_accounts = tuple(
        sorted(_text(item, "snapshot.managed_account") for item in managed_raw)
    )

    positions: list[TwsPosition] = []
    for item in _objects(root.get("positions"), "snapshot.positions"):
        positions.append(
            TwsPosition(
                raw_account_id=_text(item.get("account"), "position.account"),
                conid=_text(item.get("conid"), "position.conid"),
                quantity=_decimal(item.get("quantity"), "position.quantity"),
                market_value_base=None,
                currency=_text(item.get("currency"), "position.currency"),
            )
        )

    executions: list[TwsExecution] = []
    for item in _objects(
        root.get("current_day_executions"), "snapshot.current_day_executions"
    ):
        executions.append(
            TwsExecution(
                raw_account_id=_text(item.get("account"), "execution.account"),
                execution_id=_text(item.get("execution_id"), "execution.execution_id"),
                conid=(
                    _text(item.get("conid"), "execution.conid")
                    if item.get("conid") is not None
                    else None
                ),
                quantity=_decimal(item.get("quantity"), "execution.quantity"),
            )
        )

    account_values: list[TwsAccountValue] = []
    for item in _objects(root.get("account_summaries"), "snapshot.account_summaries"):
        tag = _text(item.get("tag"), "account_summary.tag")
        if tag not in _ACCOUNT_VALUE_TAGS:
            continue
        account_values.append(
            TwsAccountValue(
                raw_account_id=_text(item.get("account"), "account_summary.account"),
                tag=tag,
                currency=_text(item.get("currency"), "account_summary.currency"),
                value=_decimal(item.get("value"), "account_summary.value"),
            )
        )

    open_orders = _objects(root.get("open_orders"), "snapshot.open_orders")
    order_statuses = _objects(root.get("order_statuses"), "snapshot.order_statuses")
    return TwsSnapshot(
        observed_at=_timestamp(root.get("captured_at_utc")),
        raw_account_ids=managed_accounts,
        positions=tuple(positions),
        executions=tuple(executions),
        source_sha256=source_sha256,
        account_values=tuple(account_values),
        open_order_count=len(open_orders),
        order_status_count=len(order_statuses),
    )

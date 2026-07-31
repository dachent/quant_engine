"""Provider-neutral TWS current-state boundary with no socket implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from quant_engine.ibkr.flex import FlexValidationError


@dataclass(frozen=True)
class TwsPosition:
    raw_account_id: str
    conid: str
    quantity: Decimal
    market_value_base: Decimal | None

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
        object.__setattr__(self, "raw_account_ids", accounts)


class TwsReadSource(Protocol):
    """A separately authorized implementation may supply one current snapshot."""

    def capture(self) -> TwsSnapshot: ...


@dataclass(frozen=True)
class TwsReconciliation:
    account_identity_status: str
    flex_as_of: date
    snapshot_date: date
    execution_overlap_count: int
    position_count: int
    overlap_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "account_identity_status": self.account_identity_status,
            "execution_overlap_count": self.execution_overlap_count,
            "flex_as_of": self.flex_as_of.isoformat(),
            "overlap_status": self.overlap_status,
            "position_count": self.position_count,
            "snapshot_date": self.snapshot_date.isoformat(),
        }


def reconcile_tws_snapshot(
    snapshot: TwsSnapshot,
    *,
    accepted_raw_account_ids: frozenset[str],
    flex_execution_ids: frozenset[str],
    flex_as_of: date,
) -> TwsReconciliation:
    """Reconcile only boundaries that are meaningful without live broker state."""

    if frozenset(snapshot.raw_account_ids) != accepted_raw_account_ids:
        raise FlexValidationError(
            "TWS snapshot account set differs from Flex authority"
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
        account_identity_status="PASS",
        flex_as_of=flex_as_of,
        snapshot_date=snapshot_date,
        execution_overlap_count=overlap,
        position_count=len(snapshot.positions),
        overlap_status=overlap_status,
    )

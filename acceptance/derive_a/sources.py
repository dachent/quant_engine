"""Parsers for the upstream inputs derivation_a is allowed to read.

Every reader works on bytes that were hashed first, so a recorded sha256 always
describes exactly the bytes that were parsed. Only stdlib parsing is used.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

INCLUDED = "INCLUDED_PRODUCT_EPOCH"


class InputError(RuntimeError):
    """An input is missing, unreadable, or does not match its pinned identity."""


@dataclass(frozen=True)
class InputFile:
    path: str
    sha256: str
    size: int
    data: bytes


def read_input(
    qe_root: Path, relative: str, expected_sha256: Mapping[str, str]
) -> InputFile:
    target = qe_root.joinpath(*relative.split("/"))
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read {relative}: {type(exc).__name__}") from exc
    digest = hashlib.sha256(data).hexdigest()
    pinned = expected_sha256.get(relative)
    if pinned is not None and pinned != digest:
        raise InputError(f"{relative} sha256 {digest} differs from pinned {pinned}")
    return InputFile(relative, digest, len(data), data)


def iso_date(value: object) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, str, Decimal)):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation:
            return None
    return None


def csv_rows(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


# --------------------------------------------------------------------------- crosswalk


@dataclass(frozen=True)
class Assignment:
    account_id: str
    strategy_id: str
    provider: str
    label: str
    effective_start: dt.date
    effective_end: dt.date | None
    disposition: str


@dataclass(frozen=True)
class Crosswalk:
    calculation_cutoff_date: dt.date | None
    as_of_date: dt.date | None
    rows: tuple[Assignment, ...]


def parse_crosswalk(data: bytes) -> Crosswalk:
    document = json.loads(data.decode("utf-8"))
    rows: list[Assignment] = []
    for raw in document["rows"]:
        start = iso_date(raw.get("effective_start"))
        if start is None:
            raise InputError(f"crosswalk row {raw.get('assignment_id')} lacks a start")
        rows.append(
            Assignment(
                account_id=str(raw["account_id"]),
                strategy_id=str(raw["strategy_id"]),
                provider=str(raw["provider"]),
                label=str(raw.get("account_label") or ""),
                effective_start=start,
                effective_end=iso_date(raw.get("effective_end")),
                disposition=str(raw["product_epoch_disposition"]),
            )
        )
    rows.sort(key=lambda row: (row.account_id, row.effective_start))
    return Crosswalk(
        calculation_cutoff_date=iso_date(document.get("calculation_cutoff_date")),
        as_of_date=iso_date(document.get("as_of_date")),
        rows=tuple(rows),
    )


# ------------------------------------------------------------------------------ ledger


@dataclass(frozen=True)
class Flow:
    """An external capital movement for one account, in USD.

    ``timing`` is ``EOD`` (date-only: contained in the first value observation
    on or after ``effective_date``, after that day's return) or ``BOD`` (exact
    timestamp after the observation dated ``effective_date`` and before the next
    one, so the flow is invested for the next sub-period).
    """

    effective_date: dt.date
    amount: Decimal
    event_date: dt.date
    kind: str
    record_id: str
    timing: str = "EOD"


@dataclass(frozen=True)
class UnusableFlow:
    effective_date: dt.date | None
    record_id: str
    reason: str


@dataclass
class AccountFlows:
    flows: list[Flow] = field(default_factory=list)
    unusable: list[UnusableFlow] = field(default_factory=list)


@dataclass(frozen=True)
class Anchor:
    capital_type: str
    event_date: dt.date | None
    recognition_date: dt.date | None
    amount: Decimal | None


@dataclass(frozen=True)
class Ledger:
    as_of_date: dt.date | None
    nav: Mapping[str, Mapping[dt.date, Decimal]]
    nav_rows: Mapping[str, Mapping[dt.date, Mapping[str, Any]]]
    nav_issues: Mapping[str, Sequence[str]]
    flows: Mapping[str, AccountFlows]
    anchors: Mapping[str, Sequence[Anchor]]
    generations: Mapping[str, str]


def parse_ledger(data: bytes) -> Ledger:
    document = json.loads(data.decode("utf-8"))
    nav: dict[str, dict[dt.date, Decimal]] = {}
    nav_rows: dict[str, dict[dt.date, Mapping[str, Any]]] = {}
    issues: dict[str, list[str]] = {}
    for row in document["nav_observations"]:
        account = str(row["account_id"])
        day = iso_date(row.get("as_of_date"))
        value_block = row.get("nav") or {}
        value = decimal_or_none(value_block.get("value"))
        if (
            day is None
            or value is None
            or value_block.get("status") != "AVAILABLE"
            or value_block.get("currency") != "USD"
        ):
            issues.setdefault(account, []).append(
                f"unusable nav row {row.get('record_id')}"
            )
            continue
        series = nav.setdefault(account, {})
        if day in series and series[day] != value:
            issues.setdefault(account, []).append(
                f"conflicting nav rows on {day.isoformat()}"
            )
        series[day] = value
        nav_rows.setdefault(account, {})[day] = row
    flows: dict[str, AccountFlows] = {}
    for row in document["flows"]:
        account = str(row["account_id"])
        bucket = flows.setdefault(account, AccountFlows())
        event = iso_date(row.get("event_date"))
        recognized = iso_date(row.get("nav_recognition_date")) or event
        amount_block = row.get("amount") or {}
        amount = decimal_or_none(amount_block.get("value"))
        record = str(row.get("record_id"))
        if (
            event is None
            or recognized is None
            or amount is None
            or amount_block.get("status") != "AVAILABLE"
        ):
            bucket.unusable.append(
                UnusableFlow(recognized, record, "amount unavailable")
            )
            continue
        if amount_block.get("currency") != "USD":
            bucket.unusable.append(
                UnusableFlow(
                    recognized, record, f"currency {amount_block.get('currency')}"
                )
            )
            continue
        bucket.flows.append(
            Flow(recognized, amount, event, str(row.get("flow_type")), record)
        )
    anchors: dict[str, list[Anchor]] = {}
    for row in document.get("capital_anchors", []):
        amount_block = row.get("amount") or {}
        anchors.setdefault(str(row["account_id"]), []).append(
            Anchor(
                capital_type=str(row.get("capital_type")),
                event_date=iso_date(row.get("event_date")),
                recognition_date=iso_date(row.get("nav_recognition_date")),
                amount=decimal_or_none(amount_block.get("value")),
            )
        )
    generations = {
        str(item.get("provider_id")): str(item.get("generation_id"))
        for item in document.get("provider_generations", [])
    }
    return Ledger(
        as_of_date=iso_date(document.get("as_of_date")),
        nav=nav,
        nav_rows=nav_rows,
        nav_issues=issues,
        flows=flows,
        anchors=anchors,
        generations=generations,
    )


# ---------------------------------------------------------------------------- Quantbase

QUANTBASE_FLOW_TYPES = {"BUY": 1, "SELL": -1, "MAX_S": -1}


@dataclass(frozen=True)
class QuantbaseSleeve:
    fund_id: str
    fund_title: str
    flows: AccountFlows
    fee_sales: tuple[tuple[dt.date, Decimal], ...]
    rebalances: int
    coverage_end: dt.date | None
    snapshot_value: Decimal | None
    other_types: tuple[str, ...]


def _utc_day(value: str) -> dt.date | None:
    return iso_date(value)


def parse_quantbase_sleeve(
    activity: bytes, accounts: bytes, fund_title: str
) -> QuantbaseSleeve:
    """Strategy-sleeve flows for one Quantbase fund, from raw account activity.

    A fund BUY moves account cash into the sleeve (+), a SELL/MAX_S moves it out
    (-). Sales flagged ``created_to_cover_fees`` pay the provider's subscription
    fee from the sleeve: an internal expense, not a flow. Rebalances are
    internal trades. A fill with last ``filled_at`` on UTC date F is absent from
    the dashboard value dated F and present, valued at the next close, in the
    value dated F + 1: it is a ``BOD`` flow dated F (exact-timestamp boundary).
    """

    rows = csv_rows(activity)
    fund_ids = sorted(
        {
            row["fund"]
            for row in rows
            if row.get("activity_type") == "filled_txn"
            and row.get("fund_title") == fund_title
        }
    )
    if len(fund_ids) != 1:
        raise InputError(f"Quantbase fund title {fund_title!r} maps to {fund_ids}")
    fund_id = fund_ids[0]
    fills: dict[str, list[dt.date]] = {}
    for row in rows:
        if row.get("activity_type") == "order" and row.get("executed") == "Y":
            day = _utc_day(row.get("filled_at", ""))
            if day is not None:
                fills.setdefault(row.get("transaction", ""), []).append(day)
    bucket = AccountFlows()
    fee_sales: list[tuple[dt.date, Decimal]] = []
    rebalances = 0
    other: set[str] = set()
    coverage_end: dt.date | None = None
    for row in rows:
        for key in ("time", "created_at", "date", "filled_at", "completed_at"):
            day = _utc_day(row.get(key, ""))
            if day is not None and (coverage_end is None or day > coverage_end):
                coverage_end = day
        if row.get("activity_type") != "filled_txn" or row.get("fund") != fund_id:
            continue
        kind = row.get("type", "")
        record = f"quantbase:filled_txn:{row.get('id')}"
        if row.get("is_rebalance") == "True" or kind == "RBLNC":
            rebalances += 1
            continue
        amount = decimal_or_none(row.get("amount"))
        filled_days = fills.get(row.get("id", ""), [])
        if kind not in QUANTBASE_FLOW_TYPES:
            other.add(kind)
            bucket.unusable.append(UnusableFlow(None, record, f"unknown type {kind}"))
            continue
        if amount is None or row.get("filled") != "Y" or not filled_days:
            bucket.unusable.append(UnusableFlow(None, record, "unfilled or no fills"))
            continue
        if (amount > 0) != (QUANTBASE_FLOW_TYPES[kind] > 0):
            bucket.unusable.append(UnusableFlow(None, record, "sign differs from type"))
            continue
        effective = max(filled_days)
        if row.get("created_to_cover_fees") == "True":
            fee_sales.append((effective, amount))
            continue
        event = _utc_day(row.get("time", "")) or effective
        bucket.flows.append(Flow(effective, amount, event, kind, record, "BOD"))
    snapshot: Decimal | None = None
    for row in csv_rows(accounts):
        try:
            funds = json.loads(row.get("funds") or "[]")
        except json.JSONDecodeError:
            continue
        for fund in funds:
            if str(fund.get("fund_id")) == fund_id:
                snapshot = decimal_or_none(fund.get("value"))
    bucket.flows.sort(key=lambda flow: (flow.effective_date, flow.record_id))
    return QuantbaseSleeve(
        fund_id=fund_id,
        fund_title=fund_title,
        flows=bucket,
        fee_sales=tuple(sorted(fee_sales)),
        rebalances=rebalances,
        coverage_end=coverage_end,
        snapshot_value=snapshot,
        other_types=tuple(sorted(other)),
    )


# ---------------------------------------------------------------------------- Matrixport


@dataclass(frozen=True)
class MatrixportWallet:
    deposits: tuple[tuple[dt.date, Decimal, str], ...]
    bills: tuple[tuple[dt.date, Decimal, str, str, str], ...]
    rejected_currencies: tuple[str, ...]
    nav_per_unit: Mapping[dt.date, Decimal]
    coverage_end: dt.date | None


def parse_matrixport_wallet(
    deposits: bytes, bills: bytes, dispositions: bytes, nav_daily: bytes
) -> MatrixportWallet:
    deposit_rows: list[tuple[dt.date, Decimal, str]] = []
    for row in csv_rows(deposits):
        day = _utc_day(row.get("created_at_utc", ""))
        amount = decimal_or_none(row.get("amount"))
        if row.get("state") == "confirmed" and day is not None and amount is not None:
            deposit_rows.append((day, amount, row.get("currency", "")))
    bill_rows: list[tuple[dt.date, Decimal, str, str, str]] = []
    for row in csv_rows(bills):
        day = _utc_day(row.get("timestamp_utc", ""))
        amount = decimal_or_none(row.get("amount"))
        if day is None or amount is None:
            raise InputError("unparseable Matrixport wallet bill")
        bill_rows.append(
            (
                day,
                amount,
                row.get("currency", ""),
                row.get("direction", ""),
                row.get("tx_type", ""),
            )
        )
    rejected = sorted(
        {
            row.get("currency", "")
            for row in csv_rows(dispositions)
            if row.get("dataset") in {"wallet_deposits", "wallet_withdrawals"}
        }
    )
    per_unit: dict[dt.date, Decimal] = {}
    for row in csv_rows(nav_daily):
        day = _utc_day(row.get("day_timestamp_utc", ""))
        value = decimal_or_none(row.get("nav"))
        if day is not None and value is not None:
            per_unit[day] = value
    return MatrixportWallet(
        deposits=tuple(sorted(deposit_rows)),
        bills=tuple(sorted(bill_rows)),
        rejected_currencies=tuple(rejected),
        nav_per_unit=per_unit,
        coverage_end=max(per_unit) if per_unit else None,
    )

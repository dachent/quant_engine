"""Build the hash-bound facts of the retired Quantbase funds (#567, stage 4b).

Quantbase runs several funds inside one linked brokerage account.  The account
keeps one strategy per account in the R1 ledger (the live fund); the funds the
owner has since closed are history-only nested strategies.  This builder turns
the admitted Quantbase source bytes into the same family of hash-bound
artifact the IBKR sleeve builder emits (``build_sleeve_account_facts``): one
derived account ``<parent>-<sleeve>`` per retired fund, paired
INTERNAL_OUT / INTERNAL_IN legs with ``flow_type`` ``TRANSFER:INTERNAL:SLEEVE``
for every fund funding step, and the perimeter pseudo-account
``<parent>-unallocated`` for what cannot be paired.  No flow class is added.

Inputs (all bound by hash into the artifact): the owner's tag map
(effective-dated fund id -> sleeve), each fund's dashboard value history, the
account activity, the accounts snapshot and the owner's declared inputs.

Conventions, all stated from the bytes and never guessed:

* A fund's NAV is the dashboard dollar value.  A fill dated F (UTC date of the
  last fill of the fund order) is absent from the value dated F and present in
  the value dated F + 1, so its ``nav_recognition_date`` is F + 1.  The first
  fund purchase is the opening observation: the first non-zero value already
  contains it and is recognized the same day.
* The interval return of a day is ``own_pnl / nav_prev`` where ``own_pnl`` is
  ``nav_end - nav_prev - flow_in - flow_out`` (``flow_out`` is negative).  The
  exit day therefore returns ``(0 + proceeds) / nav_prev - 1``, never -100%.
* Deposits are account-level facts and are never attributed to a fund; fees
  stay account-level; fee-funding sells are in-fund cost, not flows.
* A reallocation is an exit and a buy of equal amount made seconds apart.  Only
  an unambiguous equal-amount, equal-day, near-in-time pair is a REALLOCATION.
  Every other amount is an UNALLOCATED_REMAINDER against the perimeter
  pseudo-account: which deposit or proceeds funded which purchase is not in the
  source bytes and is declared, not inferred.

Closure is fail-closed: the fund's net fills must equal the vendor cost basis,
the three funds must sum to the vendor total value on every day, and the
account cash derived from the activity must equal the provider's settled cash
up to the declared residual.  Anything else stops the build.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Final, Mapping, Sequence

import nested_strategy_tag_map as tag_map_module

SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "quantbase-fund-facts.schema.json"
)
ARTIFACT_ID: Final = "quantbase-fund-facts"
DECLARED_ARTIFACT_ID: Final = "quantbase-fund-declared-inputs"
# Kept equal to the IBKR sleeve builder's constants; a test pins the equality.
FLOW_TYPE: Final = "TRANSFER:INTERNAL:SLEEVE"
UNALLOCATED_SUFFIX: Final = "unallocated"
_ZERO: Final = Decimal(0)
_PRECISION: Final = 28
_GENERATION_ID: Final = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FUND_ID: Final = re.compile(r"^[0-9]{1,12}$")
_SOURCE_MODULES: Final = ("build_quantbase_fund_facts", "nested_strategy_tag_map")
_EVIDENCE_CLASSES: Final = (
    "NOT_IN_SOURCE_BYTES",
    "UNEXPLAINED_RESIDUAL",
    "OWNER_RULED",
)
_FLOW_KINDS: Final = {"BUY": 1, "SELL": -1, "MAX_S": -1}
_TOLERANCE_KEYS: Final = (
    "cost_basis",
    "nav_total",
    "pairing_amount",
    "exit_relative",
)
_ACTIVITY_COLUMNS: Final = (
    "activity_type",
    "amount",
    "created_at",
    "created_to_cover_fees",
    "date",
    "executed",
    "filled",
    "filled_at",
    "fund",
    "fund_title",
    "id",
    "is_rebalance",
    "status",
    "symbol",
    "time",
    "transaction",
    "type",
)
_ACCOUNT_COLUMNS: Final = ("id", "type", "settled_balance")
_DASHBOARD_COLUMNS: Final = ("time", "value", "cost_basis")


class QuantbaseFundFactsError(ValueError):
    """The fund facts cannot be built from the inputs; nothing was written."""


@dataclass(frozen=True)
class FundDecl:
    sleeve_id: str
    fund_id: str
    fund_title: str
    dashboard: str
    first_nonzero_day: date
    last_nonzero_day: date
    vendor_cost_basis: Decimal
    opening_tolerance: Decimal


@dataclass(frozen=True)
class OutOfScopeDecl:
    fund_id: str
    fund_title: str
    dashboard: str
    counterparty_account_id: str


@dataclass(frozen=True)
class OpenItem:
    evidence_class: str
    owner_ruling_ref: str | None
    note: str
    treatment: str | None
    amount: Decimal | None
    tolerance: Decimal | None


@dataclass(frozen=True)
class Declared:
    sha256: str
    parent_account_id: str
    base_currency: str
    data_cutoff: date
    tolerances: Mapping[str, Decimal]
    pairing_window_seconds: int
    total_dashboard: str
    nav_total_exempt_days: Mapping[date, str]
    funds: tuple[FundDecl, ...]
    out_of_scope: tuple[OutOfScopeDecl, ...]
    funding_attribution: OpenItem
    cash_residual: OpenItem
    dividend_nav_treatment: OpenItem


@dataclass(frozen=True)
class DashboardRow:
    value: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class Fill:
    """One filled fund transaction that moves cash into or out of a fund."""

    txn_id: str
    fund_id: str
    kind: str
    amount: Decimal
    time: datetime
    fill_at: datetime
    fee_sell: bool

    @property
    def fill_day(self) -> date:
        return self.fill_at.date()


def _dec(value: Decimal) -> str:
    """Canonical plain decimal text: no exponent, no trailing zeros."""

    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise QuantbaseFundFactsError(f"{name} must be decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise QuantbaseFundFactsError(f"{name} is not a number: {value!r}") from error
    if not number.is_finite():
        raise QuantbaseFundFactsError(f"{name} is not finite")
    return number


def _day(value: object, name: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise QuantbaseFundFactsError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise QuantbaseFundFactsError(f"{name} must be an ISO date") from error


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QuantbaseFundFactsError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicates)
    except QuantbaseFundFactsError:
        raise
    except ValueError as error:
        raise QuantbaseFundFactsError(f"{name} is not JSON") from error


def _strict(value: object, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise QuantbaseFundFactsError(f"{name} keys are invalid")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuantbaseFundFactsError(f"{name} must be non-empty text")
    return value


def _open_item(
    value: object, name: str, extra: set[str], treatments: Sequence[str] | None
) -> OpenItem:
    item = _strict(
        value,
        {"evidence_class", "owner_ruling_ref", "note"} | extra,
        f"open item {name}",
    )
    evidence_class = item["evidence_class"]
    if evidence_class not in _EVIDENCE_CLASSES:
        raise QuantbaseFundFactsError(f"open item {name} evidence class is invalid")
    ruling = item["owner_ruling_ref"]
    if ruling is not None and (not isinstance(ruling, str) or not ruling.strip()):
        raise QuantbaseFundFactsError(f"open item {name} ruling reference is invalid")
    if (evidence_class == "OWNER_RULED") != (ruling is not None):
        raise QuantbaseFundFactsError(
            f"open item {name}: an owner ruling reference goes with OWNER_RULED only"
        )
    treatment: str | None = None
    if treatments is not None:
        treatment = item["treatment"]
        if treatment not in treatments:
            raise QuantbaseFundFactsError(f"open item {name} treatment is invalid")
    amount: Decimal | None = None
    tolerance: Decimal | None = None
    if "amount" in extra:
        amount = _decimal(item["amount"], f"open item {name} amount")
        tolerance = _decimal(item["tolerance"], f"open item {name} tolerance")
        if tolerance < 0:
            raise QuantbaseFundFactsError(f"open item {name} tolerance is negative")
    return OpenItem(
        evidence_class=str(evidence_class),
        owner_ruling_ref=None if ruling is None else str(ruling),
        note=_text(item["note"], f"open item {name} note"),
        treatment=treatment,
        amount=amount,
        tolerance=tolerance,
    )


def load_declared_inputs(raw: bytes, tag_map: tag_map_module.TagMap) -> Declared:
    """Parse the owner's declared inputs strictly; refuse anything unstated."""

    top = _strict(
        _load_json(raw, "declared inputs"),
        {
            "schema_version",
            "artifact_id",
            "parent_account_id",
            "base_currency",
            "data_cutoff",
            "closure_tolerance",
            "pairing",
            "total_dashboard",
            "nav_total_exempt_days",
            "funds",
            "out_of_scope_funds",
            "open_items",
        },
        "declared inputs",
    )
    version = top["schema_version"]
    if (
        type(version) is not int
        or version != 1
        or top["artifact_id"] != DECLARED_ARTIFACT_ID
    ):
        raise QuantbaseFundFactsError("declared inputs identity is invalid")
    parent_id = top["parent_account_id"]
    if not isinstance(parent_id, str):
        raise QuantbaseFundFactsError("declared parent_account_id is invalid")
    try:
        parent = tag_map.parent(parent_id)
    except tag_map_module.TagMapError as error:
        raise QuantbaseFundFactsError(str(error)) from error
    currency = top["base_currency"]
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise QuantbaseFundFactsError("declared base_currency is invalid")
    sleeve_ids = {sleeve.sleeve_id for sleeve in parent.sleeves}
    if UNALLOCATED_SUFFIX in sleeve_ids:
        raise QuantbaseFundFactsError("the unallocated id must not be a sleeve")
    if len(f"{parent_id}-{UNALLOCATED_SUFFIX}") > 64:
        raise QuantbaseFundFactsError("unallocated account id is too long")
    tolerance_raw = _strict(
        top["closure_tolerance"], set(_TOLERANCE_KEYS), "closure_tolerance"
    )
    tolerances = {
        key: _decimal(tolerance_raw[key], f"closure_tolerance.{key}")
        for key in _TOLERANCE_KEYS
    }
    if any(value < 0 for value in tolerances.values()):
        raise QuantbaseFundFactsError("closure tolerance must not be negative")
    pairing = _strict(top["pairing"], {"rule", "window_seconds"}, "pairing")
    window_seconds = pairing["window_seconds"]
    if (
        pairing["rule"] != "EXACT_AMOUNT_DAY_AND_TIME_ELSE_UNALLOCATED"
        or type(window_seconds) is not int
        or not 0 < window_seconds <= 3600
    ):
        raise QuantbaseFundFactsError("pairing declaration is invalid")
    total = top["total_dashboard"]
    if not isinstance(total, str) or _KEY.fullmatch(total) is None:
        raise QuantbaseFundFactsError("total_dashboard key is invalid")
    exempt_raw = top["nav_total_exempt_days"]
    if not isinstance(exempt_raw, Mapping):
        raise QuantbaseFundFactsError("nav_total_exempt_days is invalid")
    exempt = {
        _day(key, "nav_total_exempt_days day"): _text(note, f"exemption {key} note")
        for key, note in exempt_raw.items()
    }
    funds_raw = top["funds"]
    if not isinstance(funds_raw, Mapping) or set(funds_raw) != sleeve_ids:
        raise QuantbaseFundFactsError("declared funds must name exactly the sleeves")
    funds: list[FundDecl] = []
    seen_funds: set[str] = set()
    keys: set[str] = {total}
    for sleeve in parent.sleeves:
        item = _strict(
            funds_raw[sleeve.sleeve_id],
            {
                "fund_id",
                "fund_title",
                "dashboard",
                "first_nonzero_day",
                "last_nonzero_day",
                "vendor_cost_basis",
                "opening_tolerance",
            },
            f"fund {sleeve.sleeve_id}",
        )
        fund_id = item["fund_id"]
        dashboard = item["dashboard"]
        if (
            not isinstance(fund_id, str)
            or _FUND_ID.fullmatch(fund_id) is None
            or not isinstance(dashboard, str)
            or _KEY.fullmatch(dashboard) is None
        ):
            raise QuantbaseFundFactsError(
                f"fund {sleeve.sleeve_id} identity is invalid"
            )
        if fund_id in seen_funds or dashboard in keys:
            raise QuantbaseFundFactsError(f"fund {sleeve.sleeve_id} repeats an id")
        seen_funds.add(fund_id)
        keys.add(dashboard)
        first = _day(item["first_nonzero_day"], f"{sleeve.sleeve_id} first_nonzero_day")
        last = _day(item["last_nonzero_day"], f"{sleeve.sleeve_id} last_nonzero_day")
        if last < first:
            raise QuantbaseFundFactsError(f"{sleeve.sleeve_id} epoch is reversed")
        opening = _decimal(item["opening_tolerance"], f"{sleeve.sleeve_id} opening")
        if opening < 0:
            raise QuantbaseFundFactsError("opening tolerance must not be negative")
        funds.append(
            FundDecl(
                sleeve_id=sleeve.sleeve_id,
                fund_id=fund_id,
                fund_title=_text(item["fund_title"], f"{sleeve.sleeve_id} fund_title"),
                dashboard=dashboard,
                first_nonzero_day=first,
                last_nonzero_day=last,
                vendor_cost_basis=_decimal(
                    item["vendor_cost_basis"], f"{sleeve.sleeve_id} vendor_cost_basis"
                ),
                opening_tolerance=opening,
            )
        )
    out_raw = top["out_of_scope_funds"]
    if not isinstance(out_raw, Mapping):
        raise QuantbaseFundFactsError("out_of_scope_funds is invalid")
    out_of_scope: list[OutOfScopeDecl] = []
    for fund_id in sorted(out_raw):
        item = _strict(
            out_raw[fund_id],
            {"fund_title", "dashboard", "counterparty_account_id"},
            f"out-of-scope fund {fund_id}",
        )
        dashboard = item["dashboard"]
        counterparty = item["counterparty_account_id"]
        if (
            _FUND_ID.fullmatch(fund_id) is None
            or fund_id in seen_funds
            or not isinstance(dashboard, str)
            or _KEY.fullmatch(dashboard) is None
            or dashboard in keys
            or not isinstance(counterparty, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", counterparty) is None
        ):
            raise QuantbaseFundFactsError(f"out-of-scope fund {fund_id} is invalid")
        keys.add(dashboard)
        out_of_scope.append(
            OutOfScopeDecl(
                fund_id=fund_id,
                fund_title=_text(item["fund_title"], f"fund {fund_id} fund_title"),
                dashboard=dashboard,
                counterparty_account_id=counterparty,
            )
        )
    if not out_of_scope:
        raise QuantbaseFundFactsError("no counterparty fund is declared")
    items = _strict(
        top["open_items"],
        {"funding_attribution", "cash_residual", "dividend_nav_treatment"},
        "open_items",
    )
    return Declared(
        sha256=hashlib.sha256(raw).hexdigest(),
        parent_account_id=parent_id,
        base_currency=currency,
        data_cutoff=_day(top["data_cutoff"], "data_cutoff"),
        tolerances=tolerances,
        pairing_window_seconds=window_seconds,
        total_dashboard=total,
        nav_total_exempt_days=exempt,
        funds=tuple(funds),
        out_of_scope=tuple(out_of_scope),
        funding_attribution=_open_item(
            items["funding_attribution"],
            "funding_attribution",
            {"treatment"},
            ("UNALLOCATED_REMAINDER",),
        ),
        cash_residual=_open_item(
            items["cash_residual"], "cash_residual", {"amount", "tolerance"}, None
        ),
        dividend_nav_treatment=_open_item(
            items["dividend_nav_treatment"],
            "dividend_nav_treatment",
            {"treatment"},
            ("NOT_FOLDED_INTO_NAV",),
        ),
    )


def _csv_rows(raw: bytes, name: str, required: Sequence[str]) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise QuantbaseFundFactsError(f"{name} is not UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = reader.fieldnames or []
    missing = [column for column in required if column not in fields]
    if missing:
        raise QuantbaseFundFactsError(f"{name} lacks columns: {', '.join(missing)}")
    rows: list[dict[str, str]] = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise QuantbaseFundFactsError(f"{name} has a ragged row")
        rows.append({key: str(value) for key, value in row.items()})
    return rows


def _timestamp(value: str, name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise QuantbaseFundFactsError(
            f"{name} is not a timestamp: {value!r}"
        ) from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise QuantbaseFundFactsError(f"{name} has no timezone")
    return moment.astimezone(timezone.utc)


def _money(value: str, name: str) -> Decimal:
    try:
        number = Decimal(value.strip())
    except InvalidOperation as error:
        raise QuantbaseFundFactsError(f"{name} is not a number: {value!r}") from error
    if not number.is_finite():
        raise QuantbaseFundFactsError(f"{name} is not finite")
    return number


def _parse_dashboard(raw: bytes, key: str) -> dict[date, DashboardRow]:
    name = f"dashboard {key}"
    series: dict[date, DashboardRow] = {}
    for row in _csv_rows(raw, name, _DASHBOARD_COLUMNS):
        try:
            day = datetime.strptime(row["time"], "%m/%d/%Y").date()
        except ValueError as error:
            raise QuantbaseFundFactsError(f"{name} has an unparseable date") from error
        value = _money(row["value"], f"{name} value")
        cost = _money(row["cost_basis"], f"{name} cost_basis")
        if day in series:
            raise QuantbaseFundFactsError(f"{name} repeats {day.isoformat()}")
        series[day] = DashboardRow(value, cost)
    if not series:
        raise QuantbaseFundFactsError(f"{name} is empty")
    return series


@dataclass(frozen=True)
class Activity:
    fills: tuple[Fill, ...]
    fund_titles: Mapping[str, str]
    rebalances: Mapping[str, tuple[int, Decimal]]
    deposits: tuple[tuple[date, Decimal, str], ...]
    ignored_transfers: int
    journals: tuple[tuple[date, str, Decimal, str], ...]
    dividends: tuple[tuple[date, str, Decimal, str], ...]
    funds_by_symbol: Mapping[str, frozenset[str]]
    after_cutoff: int


def _parse_activity(raw: bytes, cutoff: date) -> Activity:
    rows = _csv_rows(raw, "account activity", _ACTIVITY_COLUMNS)
    seen: dict[tuple[str, str], dict[str, str]] = {}
    unique: list[dict[str, str]] = []
    for row in rows:
        key = (row["activity_type"], row["id"])
        if row["activity_type"] in {"filled_txn", "order"} and row["id"]:
            if key in seen:
                if seen[key] != row:
                    raise QuantbaseFundFactsError(f"conflicting source identity {key}")
                continue
            seen[key] = row
        unique.append(row)
    # (filled_at, the order only pays a fee) per fund transaction.  A fund's
    # later fee-paying orders attach to its original purchase; they are fee
    # cost, so they never date the purchase itself.
    fills_at: dict[str, list[tuple[datetime, bool]]] = {}
    funds_by_symbol: dict[str, set[str]] = {}
    for row in unique:
        if row["activity_type"] != "order" or not row["filled_at"].strip():
            continue
        filled = _timestamp(row["filled_at"], "order filled_at")
        if row["executed"] == "Y":
            fills_at.setdefault(row["transaction"], []).append(
                (filled, row["created_to_cover_fees"].strip() == "True")
            )
        symbol = row["symbol"].strip()
        if symbol and symbol != "$USD" and filled.date() <= cutoff:
            funds_by_symbol.setdefault(symbol, set()).add(row["fund"].strip())
    fills: list[Fill] = []
    titles: dict[str, str] = {}
    rebalances: dict[str, tuple[int, Decimal]] = {}
    deposits: list[tuple[date, Decimal, str]] = []
    journals: list[tuple[date, str, Decimal, str]] = []
    dividends: list[tuple[date, str, Decimal, str]] = []
    ignored = 0
    after = 0
    for row in unique:
        kind = row["activity_type"]
        if kind == "filled_txn":
            fund_id = row["fund"].strip()
            if _FUND_ID.fullmatch(fund_id) is None:
                raise QuantbaseFundFactsError("a fund transaction has no fund id")
            title = row["fund_title"].strip()
            if title:
                if titles.setdefault(fund_id, title) != title:
                    raise QuantbaseFundFactsError(f"fund {fund_id} has two titles")
            txn_type = row["type"].strip()
            moment = _timestamp(row["time"], "fund transaction time")
            if txn_type == "RBLNC" or row["is_rebalance"] == "True":
                if txn_type != "RBLNC":
                    raise QuantbaseFundFactsError("a rebalance has the wrong type")
                if moment.date() > cutoff:
                    after += 1
                    continue
                # In-fund trades: never a flow, never in the vendor cost basis.
                count, net = rebalances.get(fund_id, (0, _ZERO))
                rebalances[fund_id] = (
                    count + 1,
                    net + _money(row["amount"], "rebalance amount"),
                )
                continue
            if txn_type not in _FLOW_KINDS:
                raise QuantbaseFundFactsError(f"unknown fund transaction {txn_type!r}")
            amount = _money(row["amount"], f"fund transaction {row['id']} amount")
            if row["filled"] != "Y" or (amount > 0) != (_FLOW_KINDS[txn_type] > 0):
                raise QuantbaseFundFactsError(
                    f"fund transaction {row['id']} is unfilled or has the wrong sign"
                )
            fee_flag = row["created_to_cover_fees"].strip()
            if fee_flag not in {"True", "False", ""}:
                raise QuantbaseFundFactsError("created_to_cover_fees is invalid")
            moments = [
                moment_at
                for moment_at, pays_fee in fills_at.get(row["id"], [])
                if fee_flag == "True" or not pays_fee
            ]
            if not moments:
                raise QuantbaseFundFactsError(
                    f"fund transaction {row['id']} has no executed order fill"
                )
            fills.append(
                Fill(
                    txn_id=row["id"],
                    fund_id=fund_id,
                    kind=txn_type,
                    amount=amount,
                    time=moment,
                    fill_at=max(moments),
                    fee_sell=fee_flag == "True",
                )
            )
        elif kind == "transfer":
            transfer_type = row["type"].strip().upper()
            if transfer_type not in {"DEPOSIT", "WITHDRAWAL"} or row["status"] != "C":
                ignored += 1
                continue
            when = _timestamp(row["created_at"], "transfer created_at").date()
            if when > cutoff:
                after += 1
                continue
            amount = _money(row["amount"], "transfer amount")
            if amount <= 0:
                raise QuantbaseFundFactsError("a transfer amount is not positive")
            deposits.append(
                (
                    when,
                    amount if transfer_type == "DEPOSIT" else -amount,
                    row["id"],
                )
            )
        elif kind == "journal":
            journal_type = row["type"].strip().upper()
            if journal_type not in {"AUM_FEE", "PARTNER_SUB_FEE"}:
                raise QuantbaseFundFactsError(f"unknown journal type {journal_type!r}")
            if row["status"] != "COMPLETE":
                raise QuantbaseFundFactsError("a fee journal is not complete")
            when = _timestamp(row["created_at"], "journal created_at").date()
            if when > cutoff:
                after += 1
                continue
            amount = _money(row["amount"], "journal amount")
            if amount <= 0:
                raise QuantbaseFundFactsError("a fee journal amount is not positive")
            journals.append((when, journal_type, amount, row["id"]))
        elif kind == "dividend":
            when = _timestamp(row["date"], "dividend date").date()
            if when > cutoff:
                after += 1
                continue
            amount = _money(row["amount"], "dividend amount")
            if amount <= 0 or not row["symbol"].strip():
                raise QuantbaseFundFactsError("a dividend is invalid")
            dividends.append((when, row["symbol"].strip(), amount, row["id"]))
        elif kind != "order":
            raise QuantbaseFundFactsError(f"unknown activity type {kind!r}")
    return Activity(
        fills=tuple(sorted(fills, key=lambda item: (item.fill_at, item.txn_id))),
        fund_titles=titles,
        rebalances=rebalances,
        deposits=tuple(sorted(deposits)),
        ignored_transfers=ignored,
        journals=tuple(sorted(journals)),
        dividends=tuple(sorted(dividends)),
        funds_by_symbol={k: frozenset(v) for k, v in funds_by_symbol.items()},
        after_cutoff=after,
    )


def _provider_cash(raw: bytes) -> Decimal:
    matches = [
        row
        for row in _csv_rows(raw, "accounts", _ACCOUNT_COLUMNS)
        if row["type"] == "alpaca"
    ]
    if len(matches) != 1:
        raise QuantbaseFundFactsError("accounts must hold exactly one linked account")
    return _money(matches[0]["settled_balance"], "settled_balance")


def _builder_code(modules: Sequence[str]) -> dict[str, Any]:
    """Digest of the builder and the modules it derives with (LF-normalized)."""

    files: dict[str, str] = {}
    for name in modules:
        module = sys.modules.get(name)
        path = Path(getattr(module, "__file__", "") or "")
        if not path.is_file():
            path = Path(__file__).resolve().with_name(f"{name}.py")
        text = path.read_bytes().replace(b"\r\n", b"\n")
        files[f"tools/{name}.py"] = hashlib.sha256(text).hexdigest()
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"files": files, "sha256": digest}


@dataclass(frozen=True)
class SleeveFlow:
    """A sleeve fund fill that is a flow, with the day its value shows it."""

    sleeve_id: str
    fill: Fill
    recognition_day: date
    opening: bool


@dataclass(frozen=True)
class FundBuild:
    decl: FundDecl
    account_id: str
    strategy_id: str
    rows: tuple[dict[str, Any], ...]
    flows: tuple[SleeveFlow, ...]
    fee_sells: tuple[Fill, ...]
    closure: dict[str, Any]
    summary: dict[str, Any]


def _value(series: Mapping[date, DashboardRow], day: date, name: str) -> Decimal:
    row = series.get(day)
    if row is None:
        raise QuantbaseFundFactsError(f"{name} has no value on {day.isoformat()}")
    return row.value


def _build_fund(
    decl: FundDecl,
    parent: tag_map_module.ParentMap,
    fills: Sequence[Fill],
    series: Mapping[date, DashboardRow],
    declared: Declared,
) -> FundBuild:
    name = f"fund {decl.sleeve_id}"
    sleeve = next(s for s in parent.sleeves if s.sleeve_id == decl.sleeve_id)
    cutoff = declared.data_cutoff
    if max(series) < cutoff:
        raise QuantbaseFundFactsError(
            f"{name} dashboard ends {max(series).isoformat()} before the cutoff"
        )
    nonzero = [
        day for day in sorted(series) if series[day].value != 0 and day <= cutoff
    ]
    if not nonzero:
        raise QuantbaseFundFactsError(f"{name} has no non-zero value")
    if (nonzero[0], nonzero[-1]) != (decl.first_nonzero_day, decl.last_nonzero_day):
        raise QuantbaseFundFactsError(
            f"{name} non-zero values run {nonzero[0].isoformat()}.."
            f"{nonzero[-1].isoformat()} but the declared epoch is "
            f"{decl.first_nonzero_day.isoformat()}..{decl.last_nonzero_day.isoformat()}"
        )
    exit_day = decl.last_nonzero_day + timedelta(days=1)
    day = decl.first_nonzero_day
    while day <= cutoff:
        if day not in series:
            raise QuantbaseFundFactsError(f"{name} has no row for {day.isoformat()}")
        if day <= decl.last_nonzero_day and series[day].value <= 0:
            raise QuantbaseFundFactsError(
                f"{name} is not positive on {day.isoformat()}"
            )
        if day > decl.last_nonzero_day and series[day].value != 0:
            raise QuantbaseFundFactsError(f"{name} has value after its exit")
        day += timedelta(days=1)
    if exit_day > cutoff:
        raise QuantbaseFundFactsError(f"{name} exit falls after the cutoff")

    flows: list[SleeveFlow] = []
    fee_sells: list[Fill] = []
    net_total = _ZERO
    for fill in fills:
        if fill.fill_day > cutoff or fill.time.date() > cutoff:
            raise QuantbaseFundFactsError(f"{name} has a fill after the cutoff")
        resolved = parent.resolve(fill.fund_id, fill.fill_day)
        if resolved is None or resolved.sleeve_id != decl.sleeve_id:
            raise QuantbaseFundFactsError(
                f"{name} fill {fill.txn_id} is outside the tag map's effective dates"
            )
        net_total += fill.amount
        if fill.fee_sell:
            if fill.kind != "SELL":
                raise QuantbaseFundFactsError("a fee-funding fill is not a SELL")
            fee_sells.append(fill)
            continue
        opening = fill.fill_day == decl.first_nonzero_day
        if opening and fill.kind != "BUY":
            raise QuantbaseFundFactsError(f"{name} opens with a sale")
        flows.append(
            SleeveFlow(
                sleeve_id=decl.sleeve_id,
                fill=fill,
                recognition_day=(
                    fill.fill_day if opening else fill.fill_day + timedelta(days=1)
                ),
                opening=opening,
            )
        )
        if (
            fill.fill_day < decl.first_nonzero_day
            or fill.fill_day > decl.last_nonzero_day
        ):
            raise QuantbaseFundFactsError(
                f"{name} fill {fill.txn_id} falls outside the fund's epoch"
            )
    if not flows or not any(flow.opening for flow in flows):
        raise QuantbaseFundFactsError(f"{name} has no opening purchase")
    exits = [flow for flow in flows if flow.fill.kind == "MAX_S"]
    if len(exits) != 1 or exits[0].recognition_day != exit_day:
        raise QuantbaseFundFactsError(
            f"{name} needs exactly one MAX_S exit recognized on {exit_day.isoformat()}"
        )
    if any(flow.recognition_day > exit_day for flow in flows):
        raise QuantbaseFundFactsError(f"{name} has a flow after its exit")
    opening_value = series[decl.first_nonzero_day].value
    opening_flow = sum((f.fill.amount for f in flows if f.opening), _ZERO)
    if abs(opening_value - opening_flow) > decl.opening_tolerance:
        raise QuantbaseFundFactsError(
            f"{name} opening value {_dec(opening_value)} differs from its opening "
            f"purchase {_dec(opening_flow)}"
        )

    # Closure of the fund's own fills to the vendor cost basis.
    dashboard_cost = series[cutoff].cost_basis
    net_external = sum((f.fill.amount for f in flows), _ZERO)
    fee_net = sum((f.amount for f in fee_sells), _ZERO)
    tolerance = declared.tolerances["cost_basis"]
    if (
        abs(net_total - dashboard_cost) > tolerance
        or abs(decl.vendor_cost_basis - dashboard_cost) > tolerance
    ):
        raise QuantbaseFundFactsError(
            f"{name} net fills {_dec(net_total)} do not close to the vendor cost "
            f"basis {_dec(dashboard_cost)} (declared {_dec(decl.vendor_cost_basis)})"
        )
    last_value = series[decl.last_nonzero_day].value
    exit_amount = abs(exits[0].fill.amount)
    if (
        abs(exit_amount - last_value)
        > declared.tolerances["exit_relative"] * last_value
    ):
        raise QuantbaseFundFactsError(
            f"{name} exit proceeds {_dec(exit_amount)} differ from the last value "
            f"{_dec(last_value)} by more than the declared relative tolerance"
        )

    by_day_in: dict[date, Decimal] = {}
    by_day_out: dict[date, Decimal] = {}
    for flow in flows:
        target = by_day_in if flow.fill.amount > 0 else by_day_out
        target[flow.recognition_day] = (
            target.get(flow.recognition_day, _ZERO) + flow.fill.amount
        )
    rows: list[dict[str, Any]] = []
    previous = _ZERO
    growth = Decimal(1)
    own_total = _ZERO
    day = decl.first_nonzero_day
    while day <= exit_day:
        value = series[day].value
        flow_in = by_day_in.get(day, _ZERO)
        flow_out = by_day_out.get(day, _ZERO)
        own = value - previous - flow_in - flow_out
        interval: str | None = None
        if previous > 0:
            growth *= Decimal(1) + own / previous
            interval = _dec(own / previous)
        own_total += own
        if day == decl.first_nonzero_day:
            event = "OPEN"
        elif day == exit_day:
            event = "EXIT"
        elif flow_in and flow_out:
            event = "BOTH"
        elif flow_in:
            event = "IN"
        elif flow_out:
            event = "OUT"
        else:
            event = ""
        rows.append(
            {
                "day": day.isoformat(),
                "event": event,
                "nav_prev": _dec(previous),
                "flow_in": _dec(flow_in),
                "flow_out": _dec(flow_out),
                "nav_end": _dec(value),
                "own_pnl": _dec(own),
                "interval_return": interval,
            }
        )
        previous = value
        day += timedelta(days=1)
    if previous != 0:
        raise QuantbaseFundFactsError(f"{name} does not return to zero at its exit")
    summary = {
        "first_day": decl.first_nonzero_day.isoformat(),
        "last_nonzero_day": decl.last_nonzero_day.isoformat(),
        "exit_recognition_day": exit_day.isoformat(),
        "net_external_flow": _dec(net_external),
        "fee_sells_net": _dec(fee_net),
        "own_pnl_total": _dec(own_total),
        "twr": _dec(growth - 1),
        "rebalance_count": 0,
        "rebalance_net_amount": "0",
    }
    closure = {
        "sleeve_id": decl.sleeve_id,
        "fund_id": decl.fund_id,
        "net_fills": _dec(net_total),
        "net_external_flow": _dec(net_external),
        "fee_sells_net": _dec(fee_net),
        "dashboard_cost_basis": _dec(dashboard_cost),
        "declared_cost_basis": _dec(decl.vendor_cost_basis),
        "difference": _dec(net_total - dashboard_cost),
        "exit_proceeds": _dec(exit_amount),
        "last_nonzero_value": _dec(last_value),
    }
    return FundBuild(
        decl=decl,
        account_id=parent.sleeve_account_id(decl.sleeve_id),
        strategy_id=sleeve.strategy_id,
        rows=tuple(rows),
        flows=tuple(flows),
        fee_sells=tuple(fee_sells),
        closure=closure,
        summary=summary,
    )


def _ref(fill: Fill) -> str:
    return f"quantbase:filled_txn:{fill.txn_id}"


def _pair_flows(
    funds: Sequence[FundBuild],
    external: Sequence[tuple[OutOfScopeDecl, Fill]],
    declared: Declared,
    parent: tag_map_module.ParentMap,
) -> list[dict[str, Any]]:
    """Paired INTERNAL legs for every sleeve funding step.

    An outflow and an inflow of two different funds pair only when they fall on
    the same fill day, within the declared window of each other and within the
    declared amount tolerance, and the match is unique on both sides.  The
    paired amount is the smaller one; whatever remains of either side is paired
    with a leg on the perimeter pseudo-account.
    """

    unallocated = f"{parent.parent_account_id}-{UNALLOCATED_SUFFIX}"
    window = timedelta(seconds=declared.pairing_window_seconds)
    tolerance = declared.tolerances["pairing_amount"]
    counterparty = {item.fund_id: item for item in declared.out_of_scope}
    # (fund id, fill, sleeve flow or None, account id)
    inflows: list[tuple[Fill, SleeveFlow | None, str]] = []
    outflows: list[tuple[Fill, SleeveFlow | None, str]] = []
    for fund in funds:
        for flow in fund.flows:
            target = inflows if flow.fill.amount > 0 else outflows
            target.append((flow.fill, flow, fund.account_id))
    for item, fill in external:
        target = inflows if fill.amount > 0 else outflows
        target.append((fill, None, counterparty[item.fund_id].counterparty_account_id))

    def compatible(out: Fill, into: Fill) -> bool:
        return (
            out.fund_id != into.fund_id
            and out.fill_day == into.fill_day
            and abs(out.time - into.time) <= window
            and abs(abs(out.amount) - into.amount) <= tolerance
        )

    candidates: dict[int, list[int]] = {}
    reverse: dict[int, list[int]] = {}
    for i, (out, out_flow, _out_account) in enumerate(outflows):
        for j, (into, in_flow, _in_account) in enumerate(inflows):
            if out_flow is None and in_flow is None:
                continue
            if compatible(out, into):
                candidates.setdefault(i, []).append(j)
                reverse.setdefault(j, []).append(i)
    if any(len(v) > 1 for v in candidates.values()) or any(
        len(v) > 1 for v in reverse.values()
    ):
        raise QuantbaseFundFactsError(
            "a fund exit or purchase pairs with more than one counterpart"
        )
    pair_of_out = {i: v[0] for i, v in candidates.items()}

    legs: list[dict[str, Any]] = []
    counter = 0

    def emit(
        day: date,
        recognition: date,
        out_account: str,
        in_account: str,
        amount: Decimal,
        kind: str,
        pairing: str,
        out_sleeve: str | None,
        in_sleeve: str | None,
        refs: Sequence[str],
        difference: Decimal,
        out_owned: bool,
        in_owned: bool,
    ) -> None:
        nonlocal counter
        if amount <= 0:
            return
        counter += 1
        pair_id = f"{day.isoformat()}:{counter:04d}"
        trace = {
            "kind": kind,
            "pairing": pairing,
            "from_sleeve": out_sleeve,
            "to_sleeve": in_sleeve,
            "evidence_refs": sorted(set(refs)),
            "pair_amount_difference": _dec(difference),
        }
        for account, flow_class, other, owned in (
            (out_account, "INTERNAL_OUT", in_account, out_owned),
            (in_account, "INTERNAL_IN", out_account, in_owned),
        ):
            legs.append(
                {
                    "pair_id": pair_id,
                    "account_id": account,
                    "class": flow_class,
                    "flow_type": FLOW_TYPE,
                    "event_date": day.isoformat(),
                    "nav_recognition_date": recognition.isoformat(),
                    "amount": _dec(amount),
                    "counterparty_account_id": other,
                    "leg_source": "ARTIFACT" if owned else "PARENT_LEDGER_REFERENCE",
                    "trace": trace,
                }
            )

    def sleeve_of(account: str) -> str | None:
        for fund in funds:
            if fund.account_id == account:
                return fund.decl.sleeve_id
        return None

    order = sorted(
        range(len(outflows)),
        key=lambda i: (outflows[i][0].fill_at, outflows[i][0].txn_id),
    )
    # Paired amounts first, so the remainders see what is left of each side.
    left_out = {i: abs(outflows[i][0].amount) for i in range(len(outflows))}
    left_in = {j: inflows[j][0].amount for j in range(len(inflows))}
    for i in order:
        partner = pair_of_out.get(i)
        if partner is None:
            continue
        out, out_flow, out_account = outflows[i]
        into, in_flow, in_account = inflows[partner]
        amount = min(left_out[i], left_in[partner])
        left_out[i] -= amount
        left_in[partner] -= amount
        recognition = (
            out_flow.recognition_day if out_flow else out.fill_day + timedelta(days=1)
        )
        emit(
            out.fill_day,
            recognition,
            out_account,
            in_account,
            amount,
            "REALLOCATION",
            "SLEEVE_PAIR" if out_flow and in_flow else "COUNTERPARTY_PAIR",
            sleeve_of(out_account),
            sleeve_of(in_account),
            [_ref(out), _ref(into)],
            abs(abs(out.amount) - into.amount),
            out_flow is not None,
            in_flow is not None,
        )
    for i in order:
        out, out_flow, out_account = outflows[i]
        if out_flow is None or left_out[i] <= 0:
            continue
        emit(
            out.fill_day,
            out_flow.recognition_day,
            out_account,
            unallocated,
            left_out[i],
            "UNALLOCATED_CASH",
            "UNALLOCATED_REMAINDER",
            out_flow.sleeve_id,
            None,
            [_ref(out)],
            _ZERO,
            True,
            True,
        )
    for j in sorted(
        range(len(inflows)), key=lambda k: (inflows[k][0].fill_at, inflows[k][0].txn_id)
    ):
        into, in_flow, in_account = inflows[j]
        if in_flow is None or left_in[j] <= 0:
            continue
        emit(
            into.fill_day,
            in_flow.recognition_day,
            unallocated,
            in_account,
            left_in[j],
            "UNALLOCATED_CASH",
            "UNALLOCATED_REMAINDER",
            None,
            in_flow.sleeve_id,
            [_ref(into)],
            _ZERO,
            True,
            True,
        )
    legs.sort(
        key=lambda leg: (
            leg["event_date"],
            leg["pair_id"],
            leg["class"] != "INTERNAL_OUT",
            leg["account_id"],
        )
    )
    # The pair ids follow emission order; renumber in the final order so the
    # identifier is a function of the content, not of the emission order.
    renumbered: dict[str, str] = {}
    for leg in legs:
        old = str(leg["pair_id"])
        if old not in renumbered:
            renumbered[old] = f"{leg['event_date']}:{len(renumbered) + 1:04d}"
    for leg in legs:
        leg["pair_id"] = renumbered[str(leg["pair_id"])]
    return legs


def _account_closure(
    declared: Declared,
    activity: Activity,
    funds: Sequence[FundBuild],
    dashboards: Mapping[str, Mapping[date, DashboardRow]],
    provider_cash: Decimal,
    sleeve_by_fund: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cutoff = declared.data_cutoff
    # Value closure: the three funds sum to the vendor total on every day.
    component_keys = [item.decl.dashboard for item in funds] + [
        item.dashboard for item in declared.out_of_scope
    ]
    total = dashboards[declared.total_dashboard]
    worst = _ZERO
    days = 0
    missing_as_zero = 0
    exempted: list[dict[str, Any]] = []
    last_retired_value = max(item.decl.last_nonzero_day for item in funds)
    covered = [day for day in total if day <= cutoff]
    component_days = [
        day for key in component_keys for day in dashboards[key] if day <= cutoff
    ]
    if not covered or max(covered) < max(component_days, default=max(covered)):
        raise QuantbaseFundFactsError(
            "the vendor total dashboard ends before the component dashboards"
        )
    for day in sorted(total):
        if day > cutoff:
            continue
        accumulated = _ZERO
        for key in component_keys:
            row = dashboards[key].get(day)
            if row is None:
                if day > min(dashboards[key]):
                    raise QuantbaseFundFactsError(
                        f"dashboard {key} has no row for {day.isoformat()}"
                    )
                missing_as_zero += 1
                continue
            accumulated += row.value
        gap = abs(accumulated - total[day].value)
        if day in declared.nav_total_exempt_days:
            # A declared day on which the vendor's two captures disagree.  It
            # must lie after every retired fund's last value and must really
            # exceed the tolerance, so a stale exemption cannot hide a gap.
            if day <= last_retired_value or gap <= declared.tolerances["nav_total"]:
                raise QuantbaseFundFactsError(
                    f"exemption {day.isoformat()} is not a post-exit disagreement"
                )
            exempted.append(
                {
                    "day": day.isoformat(),
                    "difference": _dec(accumulated - total[day].value),
                    "note": declared.nav_total_exempt_days[day],
                }
            )
            continue
        worst = max(worst, gap)
        days += 1
    if len(exempted) != len(declared.nav_total_exempt_days):
        raise QuantbaseFundFactsError("an exempt day is not in the vendor total")
    if days == 0 or worst > declared.tolerances["nav_total"]:
        raise QuantbaseFundFactsError(
            f"fund values differ from the vendor total by up to {_dec(worst)} "
            f"(tolerance {_dec(declared.tolerances['nav_total'])})"
        )
    nav_total = {
        "days": days,
        "max_abs_difference": _dec(worst),
        "component_days_before_first_row_treated_as_zero": missing_as_zero,
        "exempt_days": exempted,
    }

    # Fund flows of every fund, so the account's cash closes once.
    known = set(sleeve_by_fund) | {item.fund_id for item in declared.out_of_scope}
    net_by_fund: dict[str, Decimal] = {}
    for fill in activity.fills:
        if fill.fund_id not in known:
            raise QuantbaseFundFactsError(
                f"fund {fill.fund_id} is neither a sleeve nor a declared "
                "out-of-scope fund"
            )
        if fill.time.date() > cutoff:
            continue
        net_by_fund[fill.fund_id] = net_by_fund.get(fill.fund_id, _ZERO) + fill.amount
    deposits = sum((amount for _day, amount, _id in activity.deposits), _ZERO)
    aum = sum((a for _d, t, a, _i in activity.journals if t == "AUM_FEE"), _ZERO)
    sub = sum(
        (a for _d, t, a, _i in activity.journals if t == "PARTNER_SUB_FEE"), _ZERO
    )
    dividends = sum((a for _d, _s, a, _i in activity.dividends), _ZERO)
    net_funds = sum(net_by_fund.values(), _ZERO)
    derived_cash = deposits - net_funds + dividends - aum - sub
    residual = derived_cash - provider_cash
    declared_residual = declared.cash_residual.amount
    cash_tolerance = declared.cash_residual.tolerance
    assert declared_residual is not None and cash_tolerance is not None
    if abs(residual - declared_residual) > cash_tolerance:
        raise QuantbaseFundFactsError(
            f"account cash residual {_dec(residual)} is not the declared "
            f"{_dec(declared_residual)} (tolerance {_dec(cash_tolerance)})"
        )

    # Dividends are booked once: by the one fund that traded the symbol.
    in_sleeves: dict[str, Decimal] = {}
    in_external: dict[str, Decimal] = {}
    unattributed = _ZERO
    income: list[dict[str, Any]] = []
    for day, symbol, amount, record in activity.dividends:
        holders = activity.funds_by_symbol.get(symbol, frozenset())
        owner = next(iter(holders)) if len(holders) == 1 else None
        if owner is not None and owner in sleeve_by_fund:
            sleeve_id = sleeve_by_fund[owner]
            in_sleeves[sleeve_id] = in_sleeves.get(sleeve_id, _ZERO) + amount
            fund = next(item for item in funds if item.decl.sleeve_id == sleeve_id)
            income.append(
                {
                    "sleeve_id": sleeve_id,
                    "day": day.isoformat(),
                    "symbol": symbol,
                    "amount": _dec(amount),
                    "record_id": f"quantbase:dividend:{record}",
                    "after_exit": day > fund.decl.last_nonzero_day,
                }
            )
        elif owner is not None and owner in known:
            in_external[owner] = in_external.get(owner, _ZERO) + amount
        else:
            unattributed += amount
    booked = (
        sum(in_sleeves.values(), _ZERO)
        + sum(in_external.values(), _ZERO)
        + unattributed
    )
    if booked != dividends:
        raise QuantbaseFundFactsError("dividends are not booked exactly once")
    closure = {
        "nav_total": nav_total,
        "cash": {
            "scope": "ACCOUNT_LEVEL",
            "deposits": _dec(deposits),
            "deposit_count": len(activity.deposits),
            "ignored_transfers": activity.ignored_transfers,
            "net_fund_flows_by_fund": {
                key: _dec(net_by_fund[key]) for key in sorted(net_by_fund)
            },
            "net_fund_flows": _dec(net_funds),
            "dividends": _dec(dividends),
            "aum_fees": _dec(aum),
            "partner_subscription_fees": _dec(sub),
            "derived_cash": _dec(derived_cash),
            "provider_settled_cash": _dec(provider_cash),
            "residual": _dec(residual),
            "declared_residual": _dec(declared_residual),
            "residual_tolerance": _dec(cash_tolerance),
        },
        "dividends": {
            "total": _dec(dividends),
            "attributed_to_sleeves": {
                key: _dec(in_sleeves[key]) for key in sorted(in_sleeves)
            },
            "attributed_to_counterparty_funds": {
                key: _dec(in_external[key]) for key in sorted(in_external)
            },
            "unattributed": _dec(unattributed),
            "booked_once": True,
        },
        "rows_after_cutoff_excluded": activity.after_cutoff,
    }
    income.sort(key=lambda item: (item["day"], item["record_id"]))
    return closure, income


def build_quantbase_fund_facts(
    *,
    tag_map_bytes: bytes,
    declared_inputs_bytes: bytes,
    account_activity: bytes,
    accounts: bytes,
    dashboards: Mapping[str, bytes],
    parent_ledger_generation_id: str,
) -> bytes:
    """The canonical bytes of the fund-facts artifact, or a refusal."""

    if _GENERATION_ID.fullmatch(parent_ledger_generation_id) is None:
        raise QuantbaseFundFactsError("parent ledger generation id is invalid")
    try:
        tag_map = tag_map_module.validate_tag_map(tag_map_bytes)
    except tag_map_module.TagMapError as error:
        raise QuantbaseFundFactsError(str(error)) from error
    declared = load_declared_inputs(declared_inputs_bytes, tag_map)
    parent = tag_map.parent(declared.parent_account_id)
    if (
        parent.unmatched_mode != "BLOCK"
        or parent.tag_field != "fund_id"
        or parent.tag_normalization != "NONE"
    ):
        raise QuantbaseFundFactsError(
            "the tag map must tag by fund_id, unnormalized, and BLOCK unmatched tags"
        )
    needed = (
        {item.dashboard for item in declared.funds}
        | {item.dashboard for item in declared.out_of_scope}
        | {declared.total_dashboard}
    )
    if set(dashboards) != needed:
        raise QuantbaseFundFactsError(
            f"dashboards must be exactly {sorted(needed)}, got {sorted(dashboards)}"
        )
    for decl in declared.funds:
        sleeve = next(s for s in parent.sleeves if s.sleeve_id == decl.sleeve_id)
        found = parent.resolve(decl.fund_id, decl.first_nonzero_day)
        if found is None or found.sleeve_id != decl.sleeve_id:
            raise QuantbaseFundFactsError(
                f"tag map does not resolve fund {decl.fund_id} to {decl.sleeve_id}"
            )
        if sleeve.effective_end != decl.last_nonzero_day:
            raise QuantbaseFundFactsError(
                f"tag map interval of {decl.sleeve_id} does not end on the "
                "declared last non-zero day"
            )
    with localcontext() as context:
        context.prec = _PRECISION
        series = {key: _parse_dashboard(raw, key) for key, raw in dashboards.items()}
        activity = _parse_activity(account_activity, declared.data_cutoff)
        provider_cash = _provider_cash(accounts)
        sleeve_by_fund = {item.fund_id: item.sleeve_id for item in declared.funds}
        for decl in declared.funds:
            title = activity.fund_titles.get(decl.fund_id)
            if title != decl.fund_title:
                raise QuantbaseFundFactsError(
                    f"fund {decl.fund_id} is titled {title!r} in the activity, "
                    f"not {decl.fund_title!r}"
                )
        for item in declared.out_of_scope:
            if activity.fund_titles.get(item.fund_id) != item.fund_title:
                raise QuantbaseFundFactsError(
                    f"fund {item.fund_id} title differs from the declaration"
                )
        builds = [
            _build_fund(
                decl,
                parent,
                [fill for fill in activity.fills if fill.fund_id == decl.fund_id],
                series[decl.dashboard],
                declared,
            )
            for decl in declared.funds
        ]
        for build in builds:
            count, net = activity.rebalances.get(build.decl.fund_id, (0, _ZERO))
            build.summary["rebalance_count"] = count
            build.summary["rebalance_net_amount"] = _dec(net)
        account_closure, income = _account_closure(
            declared, activity, builds, series, provider_cash, sleeve_by_fund
        )
        external = [
            (item, fill)
            for item in declared.out_of_scope
            for fill in activity.fills
            if fill.fund_id == item.fund_id
            and not fill.fee_sell
            and fill.fill_day <= declared.data_cutoff
        ]
        flows = _pair_flows(builds, external, declared, parent)
        artifact = _artifact(
            tag_map,
            declared,
            parent,
            builds,
            flows,
            account_closure,
            income,
            activity,
            {
                "account_activity": hashlib.sha256(account_activity).hexdigest(),
                "accounts": hashlib.sha256(accounts).hexdigest(),
                "dashboards": {
                    key: hashlib.sha256(dashboards[key]).hexdigest()
                    for key in sorted(dashboards)
                },
            },
            parent_ledger_generation_id,
        )
    return (
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _open_items(
    declared: Declared, flows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    remainder_pairs = {
        leg["pair_id"]
        for leg in flows
        if leg["trace"]["pairing"] == "UNALLOCATED_REMAINDER"
    }
    funding = declared.funding_attribution
    cash = declared.cash_residual
    dividends = declared.dividend_nav_treatment
    return {
        "funding_attribution": {
            "evidence_class": funding.evidence_class,
            "owner_ruling_ref": funding.owner_ruling_ref,
            "treatment": funding.treatment,
            "note": funding.note,
            "unallocated_remainder_pairs": len(remainder_pairs),
        },
        "cash_residual": {
            "evidence_class": cash.evidence_class,
            "owner_ruling_ref": cash.owner_ruling_ref,
            "declared_amount": None if cash.amount is None else _dec(cash.amount),
            "tolerance": None if cash.tolerance is None else _dec(cash.tolerance),
            "note": cash.note,
        },
        "dividend_nav_treatment": {
            "evidence_class": dividends.evidence_class,
            "owner_ruling_ref": dividends.owner_ruling_ref,
            "treatment": dividends.treatment,
            "note": dividends.note,
        },
    }


def _artifact(
    tag_map: tag_map_module.TagMap,
    declared: Declared,
    parent: tag_map_module.ParentMap,
    builds: Sequence[FundBuild],
    flows: list[dict[str, Any]],
    account_closure: dict[str, Any],
    income: list[dict[str, Any]],
    activity: Activity,
    source_hashes: dict[str, Any],
    generation_id: str,
) -> dict[str, Any]:
    unallocated = f"{parent.parent_account_id}-{UNALLOCATED_SUFFIX}"
    sleeves = [
        {
            "sleeve_id": build.decl.sleeve_id,
            "account_id": build.account_id,
            "strategy_id": build.strategy_id,
            "fund_id": build.decl.fund_id,
            "fund_title": build.decl.fund_title,
            "first_day": build.rows[0]["day"],
            "last_day": build.rows[-1]["day"],
            "last_nonzero_day": build.decl.last_nonzero_day.isoformat(),
            "days": list(build.rows),
        }
        for build in sorted(builds, key=lambda item: item.decl.sleeve_id)
    ]
    signed: dict[str, Decimal] = {}
    for leg in flows:
        if leg["account_id"] == unallocated:
            amount = Decimal(leg["amount"])
            signed[leg["event_date"]] = signed.get(leg["event_date"], _ZERO) + (
                amount if leg["class"] == "INTERNAL_IN" else -amount
            )
    return {
        "schema_version": 1,
        "artifact_id": ARTIFACT_ID,
        "parent_account_id": parent.parent_account_id,
        "provider_id": parent.provider_id,
        "base_currency": declared.base_currency,
        "data_cutoff": declared.data_cutoff.isoformat(),
        "window": {
            "first_day": min(item["first_day"] for item in sleeves),
            "last_day": max(item["last_day"] for item in sleeves),
        },
        "bindings": {
            "tag_map_sha256": tag_map.sha256,
            "parent_ledger_generation_id": generation_id,
            "declared_inputs_sha256": declared.sha256,
            "source_sha256": source_hashes,
            "builder_code": _builder_code(_SOURCE_MODULES),
        },
        "conventions": {
            "nav": "DASHBOARD_VALUE",
            "recognition": "FILL_DAY_PLUS_ONE_OPENING_SAME_DAY",
            "interval_return": "OWN_PNL_OVER_PRIOR_NAV",
            "pairing_rule": "EXACT_AMOUNT_DAY_AND_TIME_ELSE_UNALLOCATED",
            "pairing_window_seconds": declared.pairing_window_seconds,
            "deposits": "ACCOUNT_LEVEL_NOT_ATTRIBUTED_TO_A_FUND",
            "fees": "ACCOUNT_LEVEL_NOT_ATTRIBUTED_TO_A_FUND",
            "fee_funding_sells": "IN_FUND_COST_NOT_A_FLOW",
        },
        "sleeves": sleeves,
        "perimeter_account": {
            "account_id": unallocated,
            "scope": "LEG_COUNTERPARTY_ONLY",
            "days": [
                {"day": day, "internal_flow_net": _dec(signed[day])}
                for day in sorted(signed)
            ],
        },
        "flows": flows,
        "closure": {
            "tolerances": {
                key: _dec(value) for key, value in sorted(declared.tolerances.items())
            },
            "funds": [
                build.closure
                for build in sorted(builds, key=lambda item: item.decl.sleeve_id)
            ],
            "fund_summaries": {
                build.decl.sleeve_id: build.summary
                for build in sorted(builds, key=lambda item: item.decl.sleeve_id)
            },
            "account": account_closure,
        },
        "open_items": _open_items(declared, flows),
        "disclosures": {
            "fee_funding_sells": [
                {
                    "sleeve_id": build.decl.sleeve_id,
                    "day": sell.fill_day.isoformat(),
                    "amount": _dec(sell.amount),
                    "record_id": _ref(sell),
                }
                for build in sorted(builds, key=lambda item: item.decl.sleeve_id)
                for sell in build.fee_sells
            ],
            "attributed_income": income,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-map", type=Path, required=True)
    parser.add_argument("--declared-inputs", type=Path, required=True)
    parser.add_argument("--account-activity", type=Path, required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument(
        "--dashboard",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="a dashboard value history, by the key the declared inputs use",
    )
    parser.add_argument("--parent-ledger-generation-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dashboards: dict[str, bytes] = {}
    for item in args.dashboard:
        key, separator, path = str(item).partition("=")
        if not separator or key in dashboards:
            print(
                f"quantbase fund facts refused: bad --dashboard {item!r}",
                file=sys.stderr,
            )
            return 1
        dashboards[key] = Path(path).read_bytes()
    try:
        data = build_quantbase_fund_facts(
            tag_map_bytes=args.tag_map.read_bytes(),
            declared_inputs_bytes=args.declared_inputs.read_bytes(),
            account_activity=args.account_activity.read_bytes(),
            accounts=args.accounts.read_bytes(),
            dashboards=dashboards,
            parent_ledger_generation_id=args.parent_ledger_generation_id,
        )
    except QuantbaseFundFactsError as error:
        print(f"quantbase fund facts refused: {error}", file=sys.stderr)
        return 1
    args.output.write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

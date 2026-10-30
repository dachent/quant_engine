"""Fail-closed normalization of Alpaca account, position, and activity payloads.

Response shapes below follow the Alpaca documentation; none was observed against
a live account (UNVERIFIED).  Activities are the cash-bearing evidence, so the
activity parser is strict: an unknown ``activity_type``, an unknown field, a
missing required field, a malformed number or timestamp, or a duplicate id
raises :class:`NormalizationError` rather than being skipped.  The raw bytes are
always staged before this module runs, so a refusal loses nothing: extend the
tables and re-normalize.  Account and position snapshots extract only the named
fields and tolerate additional keys, because they are not a cash ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Final

from quant_engine.alpaca.capture.errors import NormalizationError
from quant_engine.alpaca.capture.util import sha256_text

# activity_type -> category.  Categories requested for the residual closure are
# fill, dividend, transfer, fee, and interest; the remaining known types are
# classified explicitly so that only genuinely unknown types fail closed.
ACTIVITY_CATEGORIES: Final[Mapping[str, str]] = {
    "FILL": "fill",
    "DIV": "dividend",
    "DIVCGL": "dividend",
    "DIVCGS": "dividend",
    "DIVFT": "dividend",
    "DIVNRA": "dividend",
    "DIVROC": "dividend",
    "DIVTW": "dividend",
    "DIVTXEX": "dividend",
    "CSD": "transfer",
    "CSR": "transfer",
    "JNL": "transfer",
    "JNLC": "transfer",
    "TRANS": "transfer",
    "ACATC": "transfer",
    "JNLS": "transfer_security",
    "ACATS": "transfer_security",
    "FEE": "fee",
    "DIVFEE": "fee",
    "PTC": "fee",
    "PTR": "fee",
    "INT": "interest",
    "INTNRA": "interest",
    "INTTW": "interest",
    "MA": "corporate_action",
    "NC": "corporate_action",
    "REORG": "corporate_action",
    "SC": "corporate_action",
    "SSO": "corporate_action",
    "SSP": "corporate_action",
    "OPASN": "option_event",
    "OPEXP": "option_event",
    "OPXRC": "option_event",
    "MISC": "other",
}
KNOWN_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset(ACTIVITY_CATEGORIES)

# Types whose cash effect may legitimately be absent (security-only movements).
_CASH_OPTIONAL: Final[frozenset[str]] = frozenset(
    {
        "JNLS",
        "ACATS",
        "MA",
        "NC",
        "REORG",
        "SC",
        "SSO",
        "SSP",
        "OPASN",
        "OPEXP",
        "OPXRC",
    }
)

_FILL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "activity_type",
        "transaction_time",
        "type",
        "price",
        "qty",
        "side",
        "symbol",
        "leaves_qty",
        "order_id",
        "cum_qty",
        "order_status",
    }
)
_FILL_REQUIRED: Final[frozenset[str]] = frozenset(
    {
        "id",
        "activity_type",
        "transaction_time",
        "type",
        "price",
        "qty",
        "side",
        "symbol",
    }
)
_NONTRADE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "activity_type",
        "date",
        "net_amount",
        "symbol",
        "qty",
        "per_share_amount",
        "status",
        "description",
    }
)
_NONTRADE_REQUIRED: Final[frozenset[str]] = frozenset({"id", "activity_type", "date"})
_FILL_SIDES: Final[frozenset[str]] = frozenset({"buy", "sell", "sell_short"})
_FILL_KINDS: Final[frozenset[str]] = frozenset({"fill", "partial_fill"})
_DECIMAL_TEXT = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./ _-]{0,31}$")

ACCOUNT_REQUIRED: Final[tuple[str, ...]] = (
    "status",
    "currency",
    "cash",
    "portfolio_value",
    "equity",
)
ACCOUNT_OPTIONAL: Final[tuple[str, ...]] = (
    "long_market_value",
    "short_market_value",
    "last_equity",
    "buying_power",
)
POSITION_REQUIRED: Final[tuple[str, ...]] = (
    "symbol",
    "asset_class",
    "qty",
    "side",
    "avg_entry_price",
    "market_value",
    "cost_basis",
)
_POSITION_SIDES: Final[frozenset[str]] = frozenset({"long", "short"})


def _fail(reason: str) -> NormalizationError:
    return NormalizationError(f"normalization refused: {reason}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("duplicate key in a response object")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise _fail("non-finite number in a response")


def parse_json_bytes(body: bytes) -> Any:
    """Decode a response body; numbers become Decimal, duplicates are refused."""

    try:
        return json.loads(
            body.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except NormalizationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation):
        raise _fail("the response is not valid UTF-8 JSON") from None


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise _fail(f"{field} is not a number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value):
        result = Decimal(value)
    else:
        raise _fail(f"{field} is not a plain decimal")
    if not result.is_finite():
        raise _fail(f"{field} is not finite")
    return result


def _text(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(f"{field} is not a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _fail(f"{field} has an unexpected format")
    return value


def _utc_timestamp(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"{field} is not an RFC3339 timestamp") from None
    if parsed.tzinfo is None:
        raise _fail(f"{field} has no timezone")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _calendar_date(value: Any, field: str) -> str:
    raw = _text(value, field)
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise _fail(f"{field} is not a calendar date") from None


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _check_keys(
    raw: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str]
) -> None:
    keys = set(raw)
    if not required <= keys:
        raise _fail("an activity lacks a required field")
    if not keys <= allowed:
        raise _fail("an activity carries an unknown field")


def normalize_activity(raw: Any) -> dict[str, Any]:
    """Normalize one activity row, or refuse it."""

    if not isinstance(raw, Mapping):
        raise _fail("an activity is not an object")
    activity_type = raw.get("activity_type")
    if not isinstance(activity_type, str) or activity_type not in ACTIVITY_CATEGORIES:
        raise _fail("unknown activity type")
    category = ACTIVITY_CATEGORIES[activity_type]
    record: dict[str, Any] = {
        "activity_type": activity_type,
        "category": category,
        "fill_kind": None,
        "occurred_at": None,
        "occurred_date": None,
        "symbol": None,
        "side": None,
        "quantity": None,
        "price": None,
        "per_share_amount": None,
        "net_amount": None,
        "cash_amount": None,
        "cash_amount_basis": None,
        "status": None,
        "description": None,
        "order_id": None,
    }
    if activity_type == "FILL":
        _check_keys(raw, _FILL_KEYS, _FILL_REQUIRED)
        fill_kind = _text(raw["type"], "type")
        side = _text(raw["side"], "side")
        if fill_kind not in _FILL_KINDS:
            raise _fail("unknown fill kind")
        if side not in _FILL_SIDES:
            raise _fail("unknown fill side")
        quantity = _decimal(raw["qty"], "qty")
        price = _decimal(raw["price"], "price")
        if quantity <= 0 or price <= 0:
            raise _fail("a fill quantity or price is not positive")
        notional = quantity * price
        occurred_at = _utc_timestamp(raw["transaction_time"], "transaction_time")
        record.update(
            activity_id=_text(raw["id"], "id", _ID),
            fill_kind=fill_kind,
            occurred_at=occurred_at,
            occurred_date=occurred_at[:10],
            symbol=_text(raw["symbol"], "symbol", _SYMBOL),
            side=side,
            quantity=_plain(quantity),
            price=_plain(price),
            cash_amount=_plain(-notional if side == "buy" else notional),
            cash_amount_basis="derived_qty_x_price",
            status=(
                _text(raw["order_status"], "order_status")
                if "order_status" in raw
                else None
            ),
            order_id=(
                _text(raw["order_id"], "order_id", _ID) if "order_id" in raw else None
            ),
        )
        for optional in ("leaves_qty", "cum_qty"):
            if optional in raw:
                _decimal(raw[optional], optional)
        return record

    _check_keys(raw, _NONTRADE_KEYS, _NONTRADE_REQUIRED)
    record["activity_id"] = _text(raw["id"], "id", _ID)
    record["occurred_date"] = _calendar_date(raw["date"], "date")
    if "net_amount" in raw:
        net = _decimal(raw["net_amount"], "net_amount")
        record["net_amount"] = _plain(net)
        record["cash_amount"] = _plain(net)
        record["cash_amount_basis"] = "reported"
    elif activity_type not in _CASH_OPTIONAL:
        raise _fail("a cash-bearing activity lacks net_amount")
    if "symbol" in raw:
        record["symbol"] = _text(raw["symbol"], "symbol", _SYMBOL)
    if "qty" in raw:
        record["quantity"] = _plain(_decimal(raw["qty"], "qty"))
    if "per_share_amount" in raw:
        record["per_share_amount"] = _plain(
            _decimal(raw["per_share_amount"], "per_share_amount")
        )
    if "status" in raw:
        record["status"] = _text(raw["status"], "status")
    if "description" in raw:
        record["description"] = _text(raw["description"], "description")
    return record


def normalize_activities(pages: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize every page, refuse duplicates, and return a deterministic order."""

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        if not isinstance(page, list):
            raise _fail("an activities page is not an array")
        for raw in page:
            record = normalize_activity(raw)
            identifier = record["activity_id"]
            if identifier in seen:
                raise _fail("duplicate activity id")
            seen.add(identifier)
            records.append(record)
    records.sort(
        key=lambda r: (r["occurred_date"], r["occurred_at"] or "", r["activity_id"])
    )
    return records


def summarize_activities(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate counts and cash totals by category (the residual-closure view)."""

    categories: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    without_cash = 0
    first: str | None = None
    last: str | None = None
    for record in records:
        bucket = categories.setdefault(
            record["category"], {"count": 0, "cash_amount": Decimal(0)}
        )
        bucket["count"] += 1
        if record["cash_amount"] is None:
            without_cash += 1
        else:
            amount = Decimal(record["cash_amount"])
            bucket["cash_amount"] += amount
            total += amount
        day = record["occurred_date"]
        first = day if first is None or day < first else first
        last = day if last is None or day > last else last
    return {
        "activity_count": len(records),
        "activities_without_cash_amount": without_cash,
        "first_date": first,
        "last_date": last,
        "net_cash_amount": total,
        "by_category": {
            name: {"count": data["count"], "cash_amount": data["cash_amount"]}
            for name, data in sorted(categories.items())
        },
    }


def account_binding_matches(raw: Any, binding_sha256: str) -> bool:
    """Whether the response ``id`` or ``account_number`` hashes to the binding.

    Which of the two a given account exposes is UNVERIFIED, so either may match.
    """

    if not isinstance(raw, Mapping):
        return False
    return any(
        isinstance(raw.get(key), str) and sha256_text(raw[key]) == binding_sha256
        for key in ("id", "account_number")
    )


def normalize_account(raw: Any, binding_sha256: str) -> dict[str, Any]:
    """Extract the balance snapshot; refuse it unless it matches the binding.

    Only the SHA-256 of the configured account identifier is retained.
    """

    if not isinstance(raw, Mapping):
        raise _fail("the account response is not an object")
    if not account_binding_matches(raw, binding_sha256):
        raise _fail("the account response does not match the configured account")
    snapshot: dict[str, Any] = {"account_binding_sha256": binding_sha256}
    snapshot["status"] = _text(raw.get("status"), "status")
    snapshot["currency"] = _text(raw.get("currency"), "currency")
    for field in ACCOUNT_REQUIRED[2:]:
        snapshot[field] = _plain(_decimal(raw.get(field), field))
    for field in ACCOUNT_OPTIONAL:
        if field in raw and raw[field] is not None:
            snapshot[field] = _plain(_decimal(raw[field], field))
    return snapshot


def normalize_positions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise _fail("the positions response is not an array")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _fail("a position is not an object")
        if not set(POSITION_REQUIRED) <= set(item):
            raise _fail("a position lacks a required field")
        side = _text(item["side"], "side")
        if side not in _POSITION_SIDES:
            raise _fail("unknown position side")
        row = {
            "symbol": _text(item["symbol"], "symbol", _SYMBOL),
            "asset_class": _text(item["asset_class"], "asset_class"),
            "side": side,
            "quantity": _plain(_decimal(item["qty"], "qty")),
            "avg_entry_price": _plain(
                _decimal(item["avg_entry_price"], "avg_entry_price")
            ),
            "market_value": _plain(_decimal(item["market_value"], "market_value")),
            "cost_basis": _plain(_decimal(item["cost_basis"], "cost_basis")),
        }
        key = (row["symbol"], side)
        if key in seen:
            raise _fail("duplicate position")
        seen.add(key)
        rows.append(row)
    rows.sort(key=lambda r: (r["symbol"], r["side"]))
    return rows

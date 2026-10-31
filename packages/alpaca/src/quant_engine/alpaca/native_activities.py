"""Hash-bound native Alpaca activities package: the read side of the ingest.

``quant_engine.alpaca.capture.ingest`` turns a verified capture run into one
directory holding the activities in the generation's native form, an account
snapshot, an optional positions snapshot, a spec, and an ingest binding written
last.  :func:`load_native_package` is the only reader.  The frozen Alpaca
generation calls it for a plan that carries a ``native_activities`` block, and
the ingest adapter calls it over its own output before declaring success, so
the producer and the consumer cannot disagree about the shape.

Nothing here reads a network, a credential, or a raw provider response.  Every
file is re-hashed against the spec, and every row is re-validated: exact key
set, USD only, unique activity digests, a bounded window that contains every
event date, canonical order, and a summary that equals the rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final

from quant_engine.alpaca.capture.normalize import ACTIVITY_CATEGORIES

NATIVE_SCHEMA_VERSION: Final = 1
NATIVE_KIND: Final = "alpaca-native-activities"
ACTIVITIES_FILE: Final = "native-activities.jsonl"
ACCOUNT_FILE: Final = "native-account.json"
POSITIONS_FILE: Final = "native-positions.json"
SPEC_FILE: Final = "native-activities-spec.json"
BINDING_FILE: Final = "ingest-binding.json"
CURRENCY: Final = "USD"

ROW_KEYS: Final[tuple[str, ...]] = (
    "activity_id_sha256",
    "activity_type",
    "cash_amount",
    "cash_amount_basis",
    "category",
    "currency",
    "event_date",
    "fill_kind",
    "occurred_at",
    "order_id_sha256",
    "per_share_amount",
    "price",
    "quantity",
    "side",
    "status",
    "symbol",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_BASES: Final = frozenset({"reported", "derived_qty_x_price"})
_FILL_SIDES: Final = frozenset({"buy", "sell", "sell_short"})
# Activity types whose reported cash can only have one sign (a deposit cannot
# remove cash).  The sign convention itself is UNVERIFIED against a live
# response, so only these two are enforced; the rest are summarized as observed.
_SIGNED: Final[Mapping[str, int]] = {"CSD": 1, "CSW": -1}


class NativeActivitiesError(ValueError):
    """The native activities package is absent, altered, or malformed."""


def _fail(reason: str) -> NativeActivitiesError:
    return NativeActivitiesError(f"native activities refused: {reason}")


def canonical_bytes(value: Any) -> bytes:
    """Sorted-key, whitespace-free, ASCII JSON plus one newline."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decimal_of(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise _fail(f"{field} is not a plain decimal text")
    return Decimal(value)


def window_dates(after: object, until: object) -> tuple[date, date]:
    """The inclusive calendar bounds of a bounded declared window."""

    if not isinstance(after, str) or not isinstance(until, str):
        raise _fail("the window must be bounded on both sides")
    first = _bound(after, end=False)
    last = _bound(until, end=True)
    if first > last:
        raise _fail("the window start is after its end")
    return first.date(), last.date()


def _bound(value: str, *, end: bool) -> datetime:
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            return start + timedelta(days=1, microseconds=-1) if end else start
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail("a window bound is not a date or timestamp") from None
    if parsed.tzinfo is None:
        raise _fail("a window bound has no timezone")
    return parsed.astimezone(timezone.utc)


def window_instants(after: str, until: str) -> tuple[datetime, datetime]:
    return _bound(after, end=False), _bound(until, end=True)


def row_in_window(row: Mapping[str, Any], after: str, until: str) -> bool:
    """Whether one row lies inside the declared window.

    Fills carry an instant and are compared as instants; dated rows are
    compared as calendar dates, so a bound with a time of day never excludes a
    same-day non-trade row on a technicality.
    """

    first, last = window_dates(after, until)
    day = date.fromisoformat(str(row["event_date"]))
    if not first <= day <= last:
        return False
    occurred = row["occurred_at"]
    if occurred is None:
        return True
    start, end = window_instants(after, until)
    instant = datetime.strptime(str(occurred), "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return start <= instant <= end


def _optional_decimal(row: Mapping[str, Any], key: str) -> Decimal | None:
    value = row[key]
    return None if value is None else decimal_of(value, key)


def validate_row(row: object, after: str, until: str) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != set(ROW_KEYS):
        raise _fail("an activity row does not have the exact native key set")
    activity_type = row["activity_type"]
    if (
        not isinstance(activity_type, str)
        or activity_type not in ACTIVITY_CATEGORIES
        or row["category"] != ACTIVITY_CATEGORIES[activity_type]
    ):
        raise _fail("an activity type or category is not recognized")
    if row["currency"] != CURRENCY:
        raise _fail("an activity is not denominated in USD")
    for key in ("activity_id_sha256",):
        if not isinstance(row[key], str) or _SHA256.fullmatch(row[key]) is None:
            raise _fail(f"{key} is not a SHA-256")
    order = row["order_id_sha256"]
    if order is not None and (
        not isinstance(order, str) or _SHA256.fullmatch(order) is None
    ):
        raise _fail("order_id_sha256 is not a SHA-256")
    try:
        date.fromisoformat(str(row["event_date"]))
    except ValueError:
        raise _fail("an event date is not a calendar date") from None
    occurred = row["occurred_at"]
    if occurred is not None and (
        not isinstance(occurred, str)
        or _TIMESTAMP.fullmatch(occurred) is None
        or occurred[:10] != row["event_date"]
    ):
        raise _fail("an occurred_at is not a UTC timestamp on its event date")
    cash = _optional_decimal(row, "cash_amount")
    basis = row["cash_amount_basis"]
    if (cash is None) != (basis is None) or (basis is not None and basis not in _BASES):
        raise _fail("a cash amount and its basis disagree")
    for key in ("per_share_amount", "price", "quantity"):
        _optional_decimal(row, key)
    for key in ("symbol", "status", "fill_kind", "side"):
        if row[key] is not None and (not isinstance(row[key], str) or not row[key]):
            raise _fail(f"{key} is not text")
    if activity_type == "FILL":
        if (
            occurred is None
            or row["side"] not in _FILL_SIDES
            or basis != "derived_qty_x_price"
            or cash is None
        ):
            raise _fail("a fill lacks its instant, side, or derived cash leg")
        quantity = decimal_of(row["quantity"], "quantity")
        price = decimal_of(row["price"], "price")
        if quantity <= 0 or price <= 0:
            raise _fail("a fill quantity or price is not positive")
        notional = quantity * price
        if cash != (-notional if row["side"] == "buy" else notional):
            raise _fail("a fill cash leg differs from quantity times price")
    else:
        if occurred is not None or basis == "derived_qty_x_price":
            raise _fail("a non-fill activity carries fill-only fields")
        if row["fill_kind"] is not None or row["side"] is not None:
            raise _fail("a non-fill activity carries fill-only fields")
    sign = _SIGNED.get(activity_type)
    if sign is not None and cash is not None and cash * sign < 0:
        raise _fail("a deposit or withdrawal carries the opposite sign")
    if not row_in_window(row, after, until):
        raise _fail("an activity lies outside the declared window")
    return dict(row)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and exact cash totals by category and type (the closure view)."""

    by_category: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    without_cash = 0
    for row in rows:
        amount = None if row["cash_amount"] is None else Decimal(row["cash_amount"])
        if amount is None:
            without_cash += 1
        else:
            total += amount
        for table, name in (
            (by_category, row["category"]),
            (by_type, row["activity_type"]),
        ):
            bucket = table.setdefault(
                name,
                {"count": 0, "cash_amount": Decimal(0), "positive": 0, "negative": 0},
            )
            bucket["count"] += 1
            if amount is not None:
                bucket["cash_amount"] += amount
                bucket["positive"] += amount > 0
                bucket["negative"] += amount < 0

    def render(table: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            name: {
                "cash_amount": format(data["cash_amount"], "f"),
                "count": data["count"],
                "negative": data["negative"],
                "positive": data["positive"],
            }
            for name, data in sorted(table.items())
        }

    days = [row["event_date"] for row in rows]
    return {
        "activities_without_cash_amount": without_cash,
        "activity_count": len(rows),
        "by_category": render(by_category),
        "by_type": render(by_type),
        "first_date": min(days) if days else None,
        "last_date": max(days) if days else None,
        "net_cash_amount": format(total, "f"),
    }


def row_order(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row["event_date"]),
        str(row["occurred_at"] or ""),
        str(row["activity_id_sha256"]),
    )


@dataclass(frozen=True)
class NativeFile:
    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class NativePackage:
    directory: Path
    binding_sha256: str
    binding: dict[str, Any]
    spec: dict[str, Any]
    internal_account_id: str
    after: str
    until: str
    rows: list[dict[str, Any]]
    account: dict[str, Any]
    positions: list[dict[str, Any]] | None
    files: tuple[NativeFile, ...]

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self.spec["summary"])


def _json_object(data: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _fail(f"{name} is not UTF-8 JSON") from None
    if not isinstance(value, dict) or canonical_bytes(value) != data:
        raise _fail(f"{name} is not a canonical JSON object")
    return value


def _read(directory: Path, name: str) -> bytes:
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise _fail(f"{name} is missing or linked")
    return path.read_bytes()


def load_native_package(
    directory: str | Path, *, binding_sha256: str | None = None
) -> NativePackage:
    """Load and fully re-validate one native activities package."""

    root = Path(directory)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise _fail("the package directory must be a real absolute directory")
    binding_bytes = _read(root, BINDING_FILE)
    actual_binding = sha256_hex(binding_bytes)
    if binding_sha256 is not None and actual_binding != binding_sha256:
        raise _fail("the ingest binding differs from the pinned digest")
    binding = _json_object(binding_bytes, BINDING_FILE)
    spec_bytes = _read(root, SPEC_FILE)
    if (
        binding.get("schema_version") != NATIVE_SCHEMA_VERSION
        or binding.get("kind") != NATIVE_KIND
        or binding.get("spec_sha256") != sha256_hex(spec_bytes)
    ):
        raise _fail("the ingest binding does not bind the spec")
    spec = _json_object(spec_bytes, SPEC_FILE)
    if (
        spec.get("schema_version") != NATIVE_SCHEMA_VERSION
        or spec.get("kind") != NATIVE_KIND
    ):
        raise _fail("the spec identity is wrong")
    account_id = spec.get("internal_account_id")
    if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
        raise _fail("the internal account id is invalid")
    if binding.get("internal_account_id") != account_id:
        raise _fail("the binding and spec name different accounts")
    window = spec.get("window")
    if not isinstance(window, dict) or set(window) != {"after", "until"}:
        raise _fail("the spec window is malformed")
    after, until = window["after"], window["until"]
    window_dates(after, until)
    if spec.get("currency") != CURRENCY:
        raise _fail("the spec currency is not USD")
    files = spec.get("files")
    if not isinstance(files, dict) or not {ACTIVITIES_FILE, ACCOUNT_FILE} <= set(files):
        raise _fail("the spec file table is malformed")
    allowed = {ACTIVITIES_FILE, ACCOUNT_FILE, POSITIONS_FILE}
    if not set(files) <= allowed:
        raise _fail("the spec lists an unknown file")
    present = {p.name for p in root.iterdir()}
    if present != set(files) | {SPEC_FILE, BINDING_FILE}:
        raise _fail("the package directory has missing or extra files")
    blobs: dict[str, bytes] = {}
    listed: list[NativeFile] = []
    for name in sorted(files):
        entry = files[name]
        blob = _read(root, name)
        if (
            not isinstance(entry, dict)
            or entry.get("sha256") != sha256_hex(blob)
            or entry.get("bytes") != len(blob)
        ):
            raise _fail(f"{name} differs from the spec")
        blobs[name] = blob
        listed.append(NativeFile(name, sha256_hex(blob), len(blob)))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in blobs[ACTIVITIES_FILE].splitlines(keepends=True):
        value = _json_object(line, "an activity row")
        row = validate_row(value, after, until)
        if row["activity_id_sha256"] in seen:
            raise _fail("a duplicate activity digest")
        seen.add(row["activity_id_sha256"])
        rows.append(row)
    if rows != sorted(rows, key=row_order):
        raise _fail("the activity rows are not in canonical order")
    if files[ACTIVITIES_FILE].get("row_count") != len(rows):
        raise _fail("the activity row count differs from the spec")
    if spec.get("summary") != summarize(rows):
        raise _fail("the summary differs from the rows")
    account = _json_object(blobs[ACCOUNT_FILE], ACCOUNT_FILE)
    if account.get("currency") != CURRENCY or account.get(
        "account_binding_sha256"
    ) != spec.get("account_binding_sha256"):
        raise _fail("the account snapshot is not USD or not bound to the account")
    for key in ("cash", "equity", "portfolio_value"):
        try:
            Decimal(str(account[key]))
        except (KeyError, InvalidOperation):
            raise _fail("the account snapshot lacks a balance") from None
    positions: list[dict[str, Any]] | None = None
    if POSITIONS_FILE in blobs:
        try:
            parsed = json.loads(blobs[POSITIONS_FILE].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _fail("the positions snapshot is not JSON") from None
        if (
            not isinstance(parsed, list)
            or canonical_bytes(parsed) != blobs[POSITIONS_FILE]
        ):
            raise _fail("the positions snapshot is not a canonical array")
        positions = [dict(item) for item in parsed if isinstance(item, dict)]
        if len(positions) != len(parsed):
            raise _fail("a position is not an object")
    return NativePackage(
        directory=root,
        binding_sha256=actual_binding,
        binding=binding,
        spec=spec,
        internal_account_id=account_id,
        after=str(after),
        until=str(until),
        rows=rows,
        account=account,
        positions=positions,
        files=(
            *listed,
            NativeFile(SPEC_FILE, sha256_hex(spec_bytes), len(spec_bytes)),
            NativeFile(BINDING_FILE, actual_binding, len(binding_bytes)),
        ),
    )

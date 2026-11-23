"""Derive the Schwab cutoff-day NAV from the verified holdings and the official closes.

The native ``balances.historical`` response can carry a row for the cutoff day D that is a stale carry
of the day before (R2.1: every non-flat account, captured after the New York close). The value this
tool derives is the one the provider ledger replays with no tolerance
(``provider_ledger._replay_schwab_patched_navs``): the sum of quantity x official close, rounded to
cents (half even), plus the cash rows. It reads a verified driver run directory and writes the
derivation record (the ``official-nav-derivation.json`` shape: per account ``official_<MMDD>``,
``official_<prior MMDD>``, ``nav_file_<prior MMDD>`` and ``cash``) create-only, and refuses on any
closure failure:

* every account of the crosswalk is in the holdings capture, and every held symbol has an official
  close for D and for the day before (a missing close is a refusal, never a skipped symbol);
* the official value of the day before equals the captured NAV of that day within ``--tolerance``;
* a flat (cash-only) account carries no patch: its native D and prior rows must both equal its cash;
* a non-flat account whose native D equals its native prior row is a stale carry and is PATCHED; one
  whose native D differs must agree with the derived value within the tolerance, else it refuses.

python tools/schwab_nav_derive.py derive --run-dir R --crosswalk C --closes-dir D --day 2026-11-06 \\
    --prior-day 2026-11-05 --out derivation.json [--report report.json] [--tolerance 0.05]

The ingest then writes the derived cutoff row into the NAV CSVs (``--derivation-record``), and ``declare``
writes the source-branch finality evidence (the composed spec's roots and NAV sources plus the pinned
``derived_patch`` that ``provider_ledger`` replays with no tolerance), create-only; it re-derives the
record from the run and refuses when the file on disk differs:

python tools/schwab_nav_derive.py declare --run-dir R --crosswalk C --closes-dir D --day 2026-11-06 \\
    --prior-day 2026-11-05 --derivation derivation.json --spec composed-spec.json --out finality.json
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass
import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
import io
import json
from pathlib import Path
import sys
from typing import Any

from quant_engine.core import check_entry_interpreter, strict_json_loads

import schwab_capture_driver as driver
from schwab_rollback import (
    STRADDLERS_IN_D,
    STRADDLERS_OUT_D,
    RolledBack,
    RollbackError,
    roll_back,
)

CASH_SECURITY_TYPE = 9
DEFAULT_TOLERANCE = Decimal("0.05")
STATUS_FLAT = "FLAT_NO_PATCH"
STATUS_PATCH = "STALE_CARRY_PATCHED"
STATUS_AGREES = "NATIVE_AGREES_WITH_OFFICIAL"
PRIOR_CLOSURE_WAIVED = "WAIVED_ACTIVE_ON_DAY"
# the ledger replay's rounding label (provider_ledger._SCHWAB_REPLAY_ROUNDING); a test pins the two together
REPLAY_ROUNDING = "SUM_QUANTITY_X_CLOSE_QUANTIZE_0.01_HALF_EVEN_PLUS_CASH"


class DeriveError(ValueError):
    """The derivation cannot be closed against the captured evidence."""


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DeriveError(f"{name} is not a number")
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise DeriveError(f"{name} is not a number") from exc


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DeriveError(f"{name} is not an object")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeriveError(f"{name} is not a list")
    return value


def _is_zero(row: Mapping[str, Any]) -> bool:
    quantity = row.get("qty")
    return isinstance(quantity, dict) and quantity.get("qty") in (0, "0", 0.0)


def rows_after_exist(run_dir: Path, accounts: Sequence[str], day: date) -> bool:
    """Whether any captured transaction row is posted or effective after ``day``."""

    for account in accounts:
        for row in account_rows(run_dir, account):
            for key in ("transactionDate", "effectiveDate"):
                if (
                    str(row.get(key) or "").strip()
                    and _row_date(account, row[key]) > day
                ):
                    return True
    return False


def _row_date(account: str, text: object) -> date:
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):  # the driver reads both
        try:
            return datetime.strptime(str(text).split()[0], pattern).date()
        except (ValueError, IndexError):
            continue
    raise DeriveError(f"{account}: a captured transaction row has an unreadable date")


def read_holdings(
    raw: object, crosswalk: Mapping[str, str]
) -> dict[str, tuple[dict[str, Decimal], Decimal]]:
    """Internal account id to (quantity by symbol, cash), exactly as the ledger replay reads it."""

    held: dict[str, tuple[dict[str, Decimal], Decimal]] = {}
    for account in _list(
        _mapping(raw, "holdings").get("accounts"), "holdings accounts"
    ):
        external = _mapping(account, "holdings account").get("accountId")
        internal = crosswalk.get(str(external))
        if internal is None or internal in held:
            raise DeriveError("holdings account is outside the crosswalk or repeated")
        quantities: dict[str, Decimal] = {}
        cash = Decimal(0)
        for group in _list(account.get("groupedPositions"), "holdings groups"):
            for row in _list(
                _mapping(group, "holdings group").get("holdingsRows"), "holdings rows"
            ):
                if group.get("securityType") == CASH_SECURITY_TYPE:
                    if row.get("marketValue") is None and _is_zero(row):
                        continue  # an emptied cash-group row carries no value
                    cash += _decimal(
                        _mapping(row.get("marketValue"), "market value").get("val"),
                        "cash value",
                    )
                else:
                    # the frozen holdings reader prefers dataSymbol over the nested symbol
                    symbol = row.get("dataSymbol")
                    if not isinstance(symbol, str) or not symbol.strip():
                        symbol = _mapping(row.get("symbol"), "symbol").get("symbol")
                    if not isinstance(symbol, str) or not symbol:
                        raise DeriveError("holdings row has no symbol")
                    quantities[symbol] = quantities.get(symbol, Decimal(0)) + _decimal(
                        _mapping(row.get("qty"), "quantity").get("qty"), "quantity"
                    )
        held[internal] = (quantities, cash)
    missing = sorted(set(crosswalk.values()) - set(held))
    if missing:
        raise DeriveError(f"holdings capture lacks accounts: {','.join(missing)}")
    return held


def close_file(closes_dir: Path, symbol: str) -> Path:
    """The Yahoo close file of a held symbol: the Schwab spelling with '/' written as '.'
    (BRK/B -> BRK.B_yahoo_daily.csv), as the Yahoo driver names it."""

    return closes_dir / f"{symbol.replace('/', '.')}_yahoo_daily.csv"


def read_closes(closes_dir: Path, symbol: str) -> dict[date, Decimal]:
    path = close_file(closes_dir, symbol)
    if not path.is_file():
        raise DeriveError(f"no official close file for {symbol}")
    rows = csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig")))
    return {
        date.fromisoformat(row["date"]): _decimal(row["close"], f"{symbol} close")
        for row in rows
    }


def official_value(
    quantities: Mapping[str, Decimal],
    cash: Decimal,
    closes: Mapping[str, Mapping[date, Decimal]],
    day: date,
    account: str,
) -> Decimal:
    total = Decimal(0)
    for symbol, quantity in sorted(quantities.items()):
        close = closes[symbol].get(day)
        if close is None:
            raise DeriveError(f"{account}: {symbol} has no official close for {day}")
        total += quantity * close
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) + cash


def transactions_on(run_dir: Path, account: str, day: date) -> int:
    """Rows of the run's captured transaction history dated ``day`` (0 when the run holds none)."""

    path = run_dir / f"transactions/normalized/wch/transactions/{account}.json"
    if not path.is_file():
        return 0
    rows = _list(strict_json_loads(path.read_bytes(), label="transactions"), "rows")
    stamp = f"{day:%m/%d/%Y}"
    return sum(
        1
        for row in rows
        if isinstance(row, dict) and str(row.get("transactionDate")) == stamp
    )


def account_rows(run_dir: Path, account: str) -> list[dict[str, Any]]:
    """The run's captured transaction rows of one account (empty when the run holds none)."""

    path = run_dir / f"transactions/normalized/wch/transactions/{account}.json"
    if not path.is_file():
        return []
    rows = _list(strict_json_loads(path.read_bytes(), label="transactions"), "rows")
    return [row for row in rows if isinstance(row, dict)]


READING_IN_D = (
    "IN_D"  # a straddler (posted after D, effective on or before D) belongs to D
)
READING_OUT_D = "OUT_D"  # ... belongs after D


@dataclass(frozen=True)
class Placement:
    """An account's holdings at the close of the day and how its straddlers were placed."""

    rolled: RolledBack
    reading: str | None  # None when the account has no straddler
    official: Mapping[str, Decimal]  # reading -> official value of the day under it


def decide_rolled(
    run_dir: Path,
    held: Mapping[str, tuple[dict[str, Decimal], Decimal]],
    closes_dir: Path,
    day: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> dict[str, Placement]:
    """Every account's holdings at the close of ``day``, rolled back from the capture.

    The retained bytes cannot say whether a row posted after ``day`` with an effective date on or
    before it is part of the close of ``day`` (none of the 68 such rows straddles a snapshot date).
    An account with such rows is valued under both readings and the reading whose value matches the
    native balance of ``day`` within ``tolerance`` is taken. It stops, with both values, when neither
    or both match, or when the native row cannot decide (it repeats the row before it, or is absent).
    """

    placements: dict[str, Placement] = {}
    for account, (quantities, cash) in held.items():
        rows = account_rows(run_dir, account)
        try:
            inside = roll_back(account, rows, quantities, cash, day, STRADDLERS_IN_D)
            if not inside.straddlers:
                placements[account] = Placement(inside, None, {})
                continue
            outside = roll_back(account, rows, quantities, cash, day, STRADDLERS_OUT_D)
        except RollbackError as exc:
            raise DeriveError(str(exc)) from exc
        history = driver.parse_balance_history(
            strict_json_loads(
                (run_dir / f"raw/balances.historical/{account}.json").read_bytes(),
                label="raw balances",
            )
        )
        earlier = sorted(d for d in history if d < day)
        listing = ", ".join(f"{k} x{v}" for k, v in sorted(inside.straddlers.items()))
        if day not in history or not earlier or history[earlier[-1]] == history[day]:
            raise DeriveError(
                f"{account}: rows posted after {day} with an effective date on or before it "
                f"({listing}) cannot be placed: the native balance of {day} is absent or "
                "repeats the day before, so it cannot decide"
            )
        values: dict[str, Decimal] = {}
        for reading, option in ((READING_IN_D, inside), (READING_OUT_D, outside)):
            closes = {s: read_closes(closes_dir, s) for s in sorted(option.quantities)}
            values[reading] = official_value(
                option.quantities, option.cash, closes, day, account
            )
        native = history[day]
        close = {k: abs(v - native) <= tolerance for k, v in values.items()}
        if close[READING_IN_D] == close[READING_OUT_D]:
            raise DeriveError(
                f"{account}: rows posted after {day} with an effective date on or before it "
                f"({listing}) are ambiguous: the native balance {native} of {day} "
                f"{'matches both' if all(close.values()) else 'matches neither'} readings "
                f"(in the day {values[READING_IN_D]}, after it {values[READING_OUT_D]}, "
                f"tolerance {tolerance})"
            )
        reading = READING_IN_D if close[READING_IN_D] else READING_OUT_D
        placements[account] = Placement(
            inside if reading == READING_IN_D else outside, reading, values
        )
    return placements


def _like(original: object, value: Decimal) -> object:
    """``value`` written the way the response wrote its number (string, int or float)."""

    if isinstance(original, str):
        return format(value, "f")
    if value == value.to_integral_value() and not isinstance(original, float):
        return int(value)
    return float(value)


ROLLBACK_SOURCE = "ROLLED_BACK_ABSENT_FROM_CAPTURE"
# the value-bearing and identity fields of a sibling row that must not be carried to a derived row
_DERIVED_ROW_BLANKED = (
    "canStream",
    "closePrice",
    "costBasis",
    "dayChange",
    "dayChngPerc",
    "description",
    "gainLoss",
    "intraDayCostBasis",
    "lastTrade",
    "marketValue",
    "nxtStps",
    "pctOfAcct",
    "priceChng",
    "priceChngPrc",
    "ratings",
    "reinvestDtl",
    "tradeTimeMs",
)


def absent_rolled_positions(
    account: Mapping[str, Any], after: RolledBack
) -> dict[str, Decimal]:
    """The symbols held at the close of the day that the capture has no row for (sold in full after it)."""

    seen: set[str] = set()
    for group in _list(account.get("groupedPositions"), "holdings groups"):
        if group.get("securityType") == CASH_SECURITY_TYPE:
            continue
        for row in _list(group.get("holdingsRows"), "holdings rows"):
            symbol = row.get("dataSymbol")
            if not isinstance(symbol, str) or not symbol.strip():
                symbol = _mapping(row.get("symbol"), "symbol").get("symbol")
            seen.add(str(symbol))
    return {s: q for s, q in sorted(after.quantities.items()) if s not in seen}


def _derived_row(
    account: Mapping[str, Any],
    internal: str,
    symbol: str,
    quantity: Decimal,
    close: Decimal,
) -> tuple[dict[str, Any], dict[str, Any], Mapping[str, Any]]:
    """A row for a position the capture lacks, shaped from its account's own position rows.

    Refuses unless the account has position rows and every position row of every position group
    has the same set of fields (the shape is then not ambiguous). The row carries only the symbol,
    the rolled-back quantity and the official close; every value field of the sibling is blanked.
    Returns the row, its flag and the group it goes into.
    """

    groups = [
        g
        for g in _list(account.get("groupedPositions"), "holdings groups")
        if g.get("securityType") != CASH_SECURITY_TYPE
    ]
    rows = [r for g in groups for r in _list(g.get("holdingsRows"), "holdings rows")]
    if not rows:
        raise DeriveError(
            f"{internal}: {symbol} was held at the close but the capture has no position row "
            "of the account to take a shape from"
        )
    shapes = {frozenset(_mapping(r, "holdings row")) for r in rows}
    if len(shapes) != 1:
        raise DeriveError(
            f"{internal}: {symbol} was held at the close but the account's position rows have "
            "different shapes, so the shape of its row is ambiguous"
        )
    group = groups[0]
    template = _mapping(_list(group.get("holdingsRows"), "holdings rows")[0], "row")
    row = copy.deepcopy(dict(template))
    for key in _DERIVED_ROW_BLANKED:
        if key in row:
            row[key] = None
    row["dataSymbol"] = symbol
    if isinstance(row.get("symbol"), dict):
        row["symbol"] = {**{k: None for k in row["symbol"]}, "symbol": symbol}
    if isinstance(row.get("qty"), dict):
        row["qty"] = {
            **{k: 0 for k in row["qty"]},
            "qty": _like(template["qty"].get("qty"), quantity),
            "qtyBeforeSplt": _like(template["qty"].get("qtyBeforeSplt"), quantity),
        }
    if isinstance(row.get("price"), dict):
        row["price"] = {**row["price"], "price": float(close)}
    row["rollbackSynthesized"] = ROLLBACK_SOURCE
    flag = {
        "account_id": internal,
        "symbol": symbol,
        "quantity": format(quantity, "f"),
        "official_close": format(close, "f"),
        "group": str(group.get("groupName")),
        "source": ROLLBACK_SOURCE,
    }
    return row, flag, group


def apply_rollback(
    raw: object,
    crosswalk: Mapping[str, str],
    rolled: Mapping[str, RolledBack],
    *,
    closes_dir: Path | None = None,
    day: date | None = None,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """A copy of the raw holdings response whose quantities and cash are the rolled-back ones.

    Rows keep their shape; a position rolled to zero keeps a zero-quantity row (counted in the second
    result, for the caller to reconcile). A position the capture never listed (sold in full after the
    day) is given a derived row, flagged in the third result, only when the caller supplies the
    official closes and the day, the rolled-back quantity is positive, the symbol has an official
    close of the day and the account's position rows share one shape; otherwise it refuses. The cash
    change needs the account's one quantity-less cash summary row, else it refuses.
    """

    document = copy.deepcopy(dict(_mapping(raw, "holdings")))
    zeroed = 0
    derived_flags: list[dict[str, Any]] = []
    for account in _list(document.get("accounts"), "holdings accounts"):
        internal = crosswalk[
            str(_mapping(account, "holdings account").get("accountId"))
        ]
        after = rolled[internal]
        symbols_seen: set[str] = set()
        summary: list[dict[str, Any]] = []
        cash_now = Decimal(0)
        for group in _list(account.get("groupedPositions"), "holdings groups"):
            for row in _list(group.get("holdingsRows"), "holdings rows"):
                if group.get("securityType") == CASH_SECURITY_TYPE:
                    if row.get("marketValue") is None and _is_zero(row):
                        continue
                    cash_now += _decimal(row["marketValue"]["val"], "cash value")
                    if "qty" not in row and row.get("rowType") == 3:
                        summary.append(row)
                    continue
                symbol = row.get("dataSymbol")
                if not isinstance(symbol, str) or not symbol.strip():
                    symbol = _mapping(row.get("symbol"), "symbol").get("symbol")
                key = after.quantities.get(str(symbol), Decimal(0))
                symbols_seen.add(str(symbol))
                if _decimal(row["qty"]["qty"], "quantity") != key:
                    row["qty"]["qty"] = _like(row["qty"]["qty"], key)
                    if key == 0:
                        zeroed += 1
        absent = sorted(set(after.quantities) - symbols_seen)
        if absent:
            if closes_dir is None or day is None:
                raise DeriveError(
                    f"{internal}: held at the close but absent from the capture (sold in full "
                    f"after it): {','.join(absent)}; the capture has no row to bind them to"
                )
            for symbol in absent:
                quantity = after.quantities[symbol]
                if quantity <= 0:
                    raise DeriveError(
                        f"{internal}: {symbol} rolls back to {quantity}, not a long position; "
                        "no row is derived for it"
                    )
                close = read_closes(closes_dir, symbol).get(day)
                if close is None:
                    raise DeriveError(
                        f"{internal}: {symbol} was sold in full after {day} but has no official "
                        f"close for {day}, so its row cannot be derived"
                    )
                row, flag, group = _derived_row(
                    account, internal, symbol, quantity, close
                )
                _list(group.get("holdingsRows"), "holdings rows").append(row)
                derived_flags.append(flag)
        delta = after.cash - cash_now
        if delta:
            if len(summary) != 1:
                raise DeriveError(
                    f"{internal}: the cash changed by {delta} since the close but the "
                    f"capture has {len(summary)} cash summary rows, not one"
                )
            value = summary[0]["marketValue"]
            value["val"] = _like(
                value["val"], _decimal(value["val"], "cash value") + delta
            )
    return document, zeroed, derived_flags


def derive(
    *,
    run_dir: Path,
    crosswalk: Mapping[str, str],
    closes_dir: Path,
    day: date,
    prior_day: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    waivers: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    """The derivation record and the per-account closure report.

    The derived value of day D is quantity x official close + cash of the captured (end of D) holdings,
    correct whatever traded on D. The prior-day closure only cross-checks the holdings, closes and
    crosswalk against the native NAV of D-1, and assumes D-1 held what D holds. An account with
    transactions dated D can fail that cross-check honestly; the operator then names it in ``waivers``
    (account to reason), which is accepted only for an account with transactions on D, skips the prior
    closure and the flat-account cash equality for it, and is recorded in the record and the report.
    """

    waivers = dict(waivers or {})
    unknown = sorted(set(waivers) - set(crosswalk.values()))
    if unknown:
        raise DeriveError(f"a waiver names accounts outside the crosswalk: {unknown}")
    if prior_day >= day:
        raise DeriveError("the prior day must precede the day")
    raw_holdings = strict_json_loads(
        (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
    )
    held = read_holdings(raw_holdings, crosswalk)
    raw_accounts = {
        crosswalk[str(_mapping(a, "holdings account").get("accountId"))]: a
        for a in _list(_mapping(raw_holdings, "holdings").get("accounts"), "accounts")
    }
    placements = decide_rolled(run_dir, held, closes_dir, day, tolerance)
    rolled_by_account = {a: p.rolled for a, p in placements.items()}
    symbols = sorted(
        {s for rolled in rolled_by_account.values() for s in rolled.quantities}
    )
    closes = {symbol: read_closes(closes_dir, symbol) for symbol in symbols}
    day_tag, prior_tag = f"{day:%m%d}", f"{prior_day:%m%d}"
    record: dict[str, dict[str, str]] = {}
    report: list[dict[str, str]] = []
    for account, (quantities, cash) in held.items():
        history = driver.parse_balance_history(
            strict_json_loads(
                (run_dir / f"raw/balances.historical/{account}.json").read_bytes(),
                label="raw balances",
            )
        )
        if day not in history or prior_day not in history:
            raise DeriveError(
                f"{account}: the native history lacks {prior_day} or {day}"
            )
        native_day, native_prior = history[day], history[prior_day]
        rolled = rolled_by_account[account]
        quantities, cash = rolled.quantities, rolled.cash
        after_day = rolled.informational
        official_day = official_value(quantities, cash, closes, day, account)
        official_prior = official_value(quantities, cash, closes, prior_day, account)
        rows_on_day = transactions_on(run_dir, account, day)
        waived = account in waivers
        if waived and rows_on_day == 0:
            raise DeriveError(
                f"{account}: a waiver needs transactions dated {day}, the run holds none"
            )
        hint = (
            f"; {rows_on_day} transaction row(s) are dated {day}, so the account may have changed "
            f"since {prior_day}: review them and name the account in --waive-prior-closure"
            if rows_on_day and not waived
            else ""
        )
        if not waived and abs(official_prior - native_prior) > tolerance:
            raise DeriveError(
                f"{account}: the official value {official_prior} of {prior_day} differs from the "
                f"captured NAV {native_prior} by more than {tolerance}{hint}"
            )
        if not quantities and not waived:
            if not (native_day == native_prior == cash):
                raise DeriveError(
                    f"{account}: a flat account's native NAV of {prior_day} and {day} "
                    f"({native_prior}, {native_day}) must both equal its cash {cash}{hint}"
                )
            status = STATUS_FLAT
        elif not quantities and native_day == cash and native_prior == cash:
            status = STATUS_FLAT
        elif native_day == native_prior and official_day != native_day:
            status = STATUS_PATCH
        elif abs(official_day - native_day) <= tolerance:
            status = STATUS_AGREES
        else:
            raise DeriveError(
                f"{account}: the native NAV {native_day} of {day} differs from the official value "
                f"{official_day} by more than {tolerance} and is not a carry of {prior_day}"
            )
        entry = {
            f"official_{day_tag}": str(official_day),
            f"official_{prior_tag}": str(official_prior),
            f"nav_file_{prior_tag}": str(native_prior),
            "cash": str(cash),
        }
        row = {
            "account_id": account,
            "native_day": str(native_day),
            "official_day": str(official_day),
            "status": status,
        }
        placement = placements[account]
        if placement.reading is not None:
            entry["straddler_reading"] = placement.reading
            entry["straddler_rows"] = ", ".join(
                f"{k} x{v}" for k, v in sorted(rolled.straddlers.items())
            )
            for name, value in sorted(placement.official.items()):
                entry[f"straddler_official_{name.lower()}"] = str(value)
        if rolled.rows_rolled_back:
            entry["rolled_back_rows"] = ", ".join(
                f"{k} x{v}" for k, v in sorted(rolled.rows_rolled_back.items())
            )
        absent = absent_rolled_positions(raw_accounts[account], rolled)
        if absent:
            entry["derived_rows_absent_from_capture"] = "; ".join(
                f"{symbol} quantity={format(quantity, 'f')} "
                f"official_close={format(closes[symbol][day], 'f')} source={ROLLBACK_SOURCE}"
                for symbol, quantity in absent.items()
            )
        if after_day:
            entry["after_day_informational_rows"] = ", ".join(
                f"{k} x{v}" for k, v in sorted(after_day.items())
            )
        if waived:
            entry["prior_closure"] = PRIOR_CLOSURE_WAIVED
            entry["waiver_reason"] = waivers[account]
            entry["transaction_rows_on_day"] = str(rows_on_day)
            row["prior_closure"] = PRIOR_CLOSURE_WAIVED
            row["waiver_reason"] = waivers[account]
        record[account] = entry
        report.append(row)
    return record, report


def _create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)


def _pin(path: Path) -> dict[str, Any]:
    path = path.resolve()
    data = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def declare(
    *,
    run_dir: Path,
    crosswalk: Mapping[str, str],
    closes_dir: Path,
    day: date,
    prior_day: date,
    tolerance: Decimal,
    derivation: Path,
    spec: Path,
    waivers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The source-branch finality evidence: the spec's roots and NAV sources plus the derived patch.

    The record on disk must equal the one re-derived from the run, so the pinned file is never
    trusted over the captured evidence it came from.
    """

    record, report = derive(
        run_dir=run_dir,
        crosswalk=crosswalk,
        closes_dir=closes_dir,
        day=day,
        prior_day=prior_day,
        tolerance=tolerance,
        waivers=waivers,
    )
    if strict_json_loads(derivation.read_bytes(), label="derivation record") != record:
        raise DeriveError(
            "the derivation record differs from the one derived from the run"
        )
    spec_doc = _mapping(
        strict_json_loads(spec.read_bytes(), label="generation spec"), "generation spec"
    )
    patched = [row for row in report if row["status"] == STATUS_PATCH]
    held = read_holdings(
        strict_json_loads(
            (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
        ),
        crosswalk,
    )
    rolled = {
        a: p.rolled
        for a, p in decide_rolled(run_dir, held, closes_dir, day, tolerance).items()
    }
    symbols = sorted(
        {s for row in patched for s in rolled[row["account_id"]].quantities}
    )
    # the files the roll-back and the after-D rows were read from: pin them
    transactions = [
        {"account_id": account, **_pin(path)}
        for account in sorted(held)
        if (
            path := run_dir / f"transactions/normalized/wch/transactions/{account}.json"
        ).is_file()
    ]
    return {
        "roots": spec_doc["roots"],
        "sources": [
            item
            for item in _list(spec_doc["sources"], "spec sources")
            if _mapping(item, "spec source").get("role") == "WCH_NAV_CSV"
        ],
        "derived_patch": {
            "schema": "schwab-nav-source-derived-patch/1",
            "derivation_basis": "DERIVED_FROM_OFFICIAL_CLOSES",
            "patched_dates": [day.isoformat()],
            "derivation_record": _pin(derivation),
            "replay": {
                "positions_capture": _pin(run_dir / "raw/holdings.v2.json"),
                "closes": [
                    {"symbol": symbol, **_pin(close_file(closes_dir, symbol))}
                    for symbol in symbols
                ],
                "cash_security_type": CASH_SECURITY_TYPE,
                "rounding": REPLAY_ROUNDING,
                **({"transactions": transactions} if transactions else {}),
            },
            "native_balances": [
                {
                    "account_id": row["account_id"],
                    **_pin(
                        run_dir / f"raw/balances.historical/{row['account_id']}.json"
                    ),
                }
                for row in patched
            ],
            "patches": [
                {
                    "account_id": row["account_id"],
                    "original_values": {day.isoformat(): row["native_day"]},
                    "patched_values": {day.isoformat(): row["official_day"]},
                }
                for row in patched
            ],
        },
    }


def _waivers(items: Sequence[str]) -> dict[str, str]:
    waivers: dict[str, str] = {}
    for item in items:
        account, separator, reason = item.partition("=")
        if not separator or not account.strip() or not reason.strip():
            raise DeriveError("a waiver is ACCOUNT=REASON with a stated reason")
        if account in waivers:
            raise DeriveError(f"the waiver for {account} is given twice")
        waivers[account] = reason.strip()
    return waivers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("derive", "declare"):
        run = commands.add_parser(name)
        run.add_argument("--run-dir", type=Path, required=True)
        run.add_argument("--crosswalk", type=Path, required=True)
        run.add_argument("--closes-dir", type=Path, required=True)
        run.add_argument("--day", required=True)
        run.add_argument("--prior-day", required=True)
        run.add_argument("--tolerance", default=str(DEFAULT_TOLERANCE))
        run.add_argument(
            "--waive-prior-closure",
            action="append",
            default=[],
            metavar="ACCOUNT=REASON",
            help="skip the prior-day closure for an account with transactions dated D (recorded)",
        )
        run.add_argument("--out", type=Path, required=True)
        if name == "derive":
            run.add_argument("--report", type=Path)
        else:
            run.add_argument("--derivation", type=Path, required=True)
            run.add_argument("--spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    refused = check_entry_interpreter("the Schwab NAV derivation", "schwab-nav-derive")
    if refused is not None:
        return refused
    report: list[dict[str, str]] = []
    try:
        common: dict[str, Any] = {
            "run_dir": args.run_dir,
            "crosswalk": driver.parse_crosswalk(
                strict_json_loads(args.crosswalk.read_bytes(), label="crosswalk")
            ),
            "closes_dir": args.closes_dir,
            "day": date.fromisoformat(args.day),
            "prior_day": date.fromisoformat(args.prior_day),
            "tolerance": _decimal(args.tolerance, "tolerance"),
            "waivers": _waivers(args.waive_prior_closure),
        }
        if args.command == "declare":
            evidence = declare(**common, derivation=args.derivation, spec=args.spec)
            _create_only(
                args.out,
                (json.dumps(evidence, indent=1, sort_keys=True) + "\n").encode("utf-8"),
            )
            return 0
        record, report = derive(**common)
        _create_only(args.out, (json.dumps(record, indent=1) + "\n").encode("utf-8"))
        if args.report is not None:
            _create_only(
                args.report, (json.dumps(report, indent=1) + "\n").encode("utf-8")
            )
    except (DeriveError, driver.DriverError, OSError, ValueError) as exc:
        print(f"schwab-nav-derive: FAIL: {exc}", file=sys.stderr)
        return 1
    for row in report:
        print(
            f"{row['account_id']}: {row['status']} native {row['native_day']} official {row['official_day']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

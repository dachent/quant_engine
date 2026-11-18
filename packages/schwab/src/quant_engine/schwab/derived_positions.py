"""A Schwab account's positions at an earlier cutoff, derived from a later export.

The Schwab "Export Positions Data" file lists the non-cash holdings, a symbol-less
``Cash & Cash Investments`` row and a ``Positions Total`` row.  A position set captured
after the cutoff stands for the cutoff only when nothing moved in the account between
them, which is checked here and refused otherwise:

* the export closes: the holdings plus the cash row equal the printed Positions Total;
* optionally the account value of the balances page equals that total;
* no transaction of the account is dated after the cutoff (history is read through the
  capture day, ``history_through``);
* an earlier independent observation of the market value (the balances page of a
  prior capture) equals the export's, so the holdings did not change in between.

The rows are typed ``DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT`` and carry the capture date;
nothing is valued at the cutoff by price movement: a holding whose value moves (anything
but a $1.00 money-market fund) cannot be carried back and refuses.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
from typing import Any, Final

DERIVED_BASIS: Final = "DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT"
_CASH_ROW: Final = "Cash & Cash Investments"
_TOTAL_ROW: Final = "Positions Total"
_MONEY_MARKET: Final = "Cash and Money Market"


class SchwabDerivedPositionError(ValueError):
    """The later snapshot cannot stand for the cutoff, or the export does not close."""


def _money(value: str, name: str) -> Decimal:
    text = value.strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise SchwabDerivedPositionError(f"{name} is not a number") from exc
    if not number.is_finite():
        raise SchwabDerivedPositionError(f"{name} is not finite")
    return -number if negative else number


def _security_id(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def _read_export(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.replace("\r\n", "\n").split("\n")]
    start = next(
        (n for n, line in enumerate(lines) if line.startswith('"Symbol"')), None
    )
    if start is None:
        raise SchwabDerivedPositionError("positions export header is absent")
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    if not rows:
        raise SchwabDerivedPositionError("positions export has no rows")
    return rows


def derive_positions_at_cutoff(
    *,
    account_id: str,
    export_text: str,
    cutoff: date,
    capture_date: date,
    history_transactions: Sequence[Mapping[str, Any]],
    history_through: date,
    earlier_market_value: Mapping[str, Decimal],
    balances_account_value: Decimal | None = None,
) -> list[dict[str, Any]]:
    """The cutoff position rows of ``account_id``, or an error saying why not."""

    if not cutoff <= history_through:
        raise SchwabDerivedPositionError("history does not reach the cutoff")
    later = [
        t
        for t in history_transactions
        if str(t.get("account_id")) == account_id
        and cutoff < date.fromisoformat(str(t["event_date"])) <= history_through
    ]
    if later:
        raise SchwabDerivedPositionError(
            "the account has transactions after the cutoff: the snapshot is not the cutoff"
        )
    if history_through < capture_date and not earlier_market_value:
        raise SchwabDerivedPositionError(
            "no independent observation covers the days after the history"
        )
    rows = _read_export(export_text)
    holdings: list[dict[str, str]] = []
    cash = Decimal(0)
    total: Decimal | None = None
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if symbol == _TOTAL_ROW:
            total = _money(row["Mkt Val (Market Value)"], "positions total")
        elif symbol == _CASH_ROW:
            cash += _money(row["Mkt Val (Market Value)"], "cash market value")
        elif symbol:
            holdings.append(row)
    if total is None:
        raise SchwabDerivedPositionError("positions export carries no total row")
    held = sum(
        (_money(h["Mkt Val (Market Value)"], "holding market value") for h in holdings),
        Decimal(0),
    )
    if held + cash != total:
        raise SchwabDerivedPositionError("positions export does not close to its total")
    if balances_account_value is not None and balances_account_value != total:
        raise SchwabDerivedPositionError(
            "positions total differs from the balances account value"
        )
    for observed_day, value in earlier_market_value.items():
        if value != held:
            raise SchwabDerivedPositionError(
                f"market value on {observed_day} differs from the snapshot: "
                "the holdings moved"
            )
    out: list[dict[str, Any]] = []
    for holding in holdings:
        symbol = holding["Symbol"].strip()
        price = _money(holding["Price"], "price")
        quantity = _money(holding["Qty (Quantity)"], "quantity")
        value = _money(holding["Mkt Val (Market Value)"], "holding market value")
        if holding.get("Asset Type", "").strip() != _MONEY_MARKET or price != 1:
            raise SchwabDerivedPositionError(
                f"{symbol} is not a $1.00 money-market fund: its value moves, so the "
                "snapshot cannot be carried back"
            )
        if quantity != value:
            raise SchwabDerivedPositionError(
                f"{symbol} quantity differs from its value at $1.00"
            )
        out.append(
            {
                "account_id": account_id,
                "as_of_date": cutoff.isoformat(),
                "as_of_basis": DERIVED_BASIS,
                "capture_date": capture_date.isoformat(),
                "closure": {
                    "cash_usd": format(cash, "f"),
                    "positions_total_usd": format(total, "f"),
                    "holdings_usd": format(held, "f"),
                    "history_through": history_through.isoformat(),
                    "earlier_market_value_days": sorted(earlier_market_value),
                },
                "evidence_class": DERIVED_BASIS,
                "mapping_status": "UNMAPPED",
                "market_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": format(value, "f"),
                },
                "quantity": format(quantity, "f"),
                "security_id": _security_id(symbol),
                "strategy_id": None,
                "symbol": symbol,
            }
        )
    return out

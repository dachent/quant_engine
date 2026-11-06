"""Roll a Schwab account's captured holdings back to an earlier close, from its own transactions.

The capture is a snapshot of the account at capture time T. Positions and cash at the close of an
earlier day D are the captured quantities and cash minus the signed share and cash effect of every
transaction row after D. The rule was fitted against retained bytes, not assumed (2026-01-19,
2026-04-12, 2026-07-17 and 2026-09-04 position exports against the 3,299 retained transaction rows,
all eight accounts, tolerances 1e-6 shares and 0.005 cash):

* a row is after D when its posting date (the first date of ``transactionDate``) is after D;
* Buy and Sell move shares and cash on the trade date; the snapshot cash is trade-date cash, not
  settled cash (a Buy on 2026-04-10 that settles 2026-04-13 is already inside the 04-12 cash row);
* a row's cash effect is its signed ``amount`` (fees are netted into it); its share effect is the
  signed ``shareQuantity`` for journals and security transfers, ``+|q|`` for Buy and Reinvest
  Shares and ``-|q|`` for Sell;
* symbols are joined with ``.`` and ``/`` removed (the snapshot says ``BRK.B``, the rows ``BRKB``).

What the retained bytes cannot decide is refused instead of guessed: a row posted after D whose
effective ("as of") date is on or before D, or the reverse; any action outside the table; a stock
split or a spin-off (Yahoo closes are split-adjusted, so a quantity added after D cannot be priced at
D); a row whose quantity or amount does not fit its action; a negative rolled-back quantity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class RollbackError(ValueError):
    """A row or an account that the roll-back cannot place; the message names the numbers."""


# action -> how its shares move (None: the row moves cash only)
_SHARES_ADD = "ADD_ABS"
_SHARES_SUB = "SUB_ABS"
_SHARES_SIGNED = "SIGNED"
SHARE_ACTIONS: dict[str, str] = {
    "Buy": _SHARES_ADD,
    "Reinvest Shares": _SHARES_ADD,
    "Sell": _SHARES_SUB,
    "Journal": _SHARES_SIGNED,
    "Journaled Shares": _SHARES_SIGNED,
    "Security Transfer": _SHARES_SIGNED,
}
CASH_ONLY_ACTIONS = frozenset(
    {
        "Bank Interest",
        "Bank Transfer",
        "Cash Dividend",
        "Cash In Lieu",
        "Credit Interest",
        "Div Adjustment",
        "Funds Received",
        "Interest Adj",
        "Long Term Cap Gain Reinvest",
        "Margin Interest",
        "Misc Cash Entry",
        "MoneyLink Adj",
        "MoneyLink Deposit",
        "MoneyLink Transfer",
        "Non-Qualified Div",
        "Pr Yr Cash Div",
        "Pr Yr Div Reinvest",
        "Qual Div Reinvest",
        "Qualified Dividend",
        "Reinvest Dividend",
        "Service Fee",
        "Short Term Cap Gain Reinvest",
        "Special Dividend",
        "Special Non Qual Div",
        "Special Qual Div",
        "Wire Sent",
    }
)
# A quantity that is added after D is not a price-able holding at D.
REFUSED_ACTIONS = frozenset({"Stock Split", "Spin-off"})
SUPPORTED_ACTIONS = frozenset(SHARE_ACTIONS) | CASH_ONLY_ACTIONS
# The only actions recorded rather than undone when they carry neither a quantity nor an amount.
# None occurs in the retained history; any other action with blank values goes through
# ``parse_effect`` and is refused (a blank Stock Split must not pass as a note).
INFORMATIONAL_ACTIONS = frozenset({"Memo"})


@dataclass(frozen=True)
class Effect:
    """The forward effect of one row on one account."""

    action: str
    symbol: str | None
    shares: Decimal
    cash: Decimal
    posted: date
    effective: date


@dataclass(frozen=True)
class RolledBack:
    quantities: dict[str, Decimal]
    cash: Decimal
    rows_rolled_back: dict[str, int]  # action -> count of rows undone
    informational: dict[
        str, int
    ]  # action -> rows after D with no quantity and no amount
    straddlers: dict[
        str, int
    ]  # action -> rows posted after D but effective on or before D


def symbol_key(symbol: str) -> str:
    """The join key of a symbol: ``.`` and ``/`` removed, upper case."""

    return symbol.replace(".", "").replace("/", "").upper()


def _date(account: str, text: object) -> date:
    first = str(text or "").split()
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):  # the driver reads both
        try:
            return datetime.strptime(first[0], pattern).date()
        except (ValueError, IndexError):
            continue
    raise RollbackError(f"{account}: a captured transaction row has an unreadable date")


def _number(account: str, action: str, name: str, text: object) -> Decimal | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    negative = raw.startswith("-")
    cleaned = raw.lstrip("-").replace("$", "").replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise RollbackError(
            f"{account}: a {action} row has an unreadable {name}"
        ) from exc
    return -value if negative else value


def parse_effect(account: str, row: Mapping[str, Any]) -> Effect:
    """The forward effect of ``row``; refuses an action the table does not cover."""

    action = str(row.get("action") or "?")
    posted = _date(account, row.get("transactionDate"))
    effective = (
        _date(account, row["effectiveDate"])
        if str(row.get("effectiveDate") or "").strip()
        else posted
    )
    if action in REFUSED_ACTIONS or action not in SUPPORTED_ACTIONS:
        raise RollbackError(
            f"{account}: a {action} row dated {posted} is outside the roll-back table "
            "(unknown or corporate action): the capture cannot be rolled back across it"
        )
    quantity = _number(account, action, "quantity", row.get("shareQuantity"))
    amount = _number(account, action, "amount", row.get("amount"))
    symbol = str(row.get("symbol") or "").strip() or None
    rule = SHARE_ACTIONS.get(action)
    shares = Decimal(0)
    if rule is None:
        if quantity is not None or amount is None:
            raise RollbackError(
                f"{account}: a {action} row dated {posted} must carry an amount and no quantity"
            )
    elif quantity is not None:
        if symbol is None:
            raise RollbackError(
                f"{account}: a {action} row dated {posted} has no symbol"
            )
        shares = {
            _SHARES_ADD: abs(quantity),
            _SHARES_SUB: -abs(quantity),
            _SHARES_SIGNED: quantity,
        }[rule]
    elif action not in ("Journal", "Security Transfer"):
        raise RollbackError(f"{account}: a {action} row dated {posted} has no quantity")
    elif amount is None:
        raise RollbackError(
            f"{account}: a {action} row dated {posted} has neither a quantity nor an amount"
        )
    if action in ("Buy", "Sell", "Reinvest Shares") and amount is None:
        raise RollbackError(f"{account}: a {action} row dated {posted} has no amount")
    return Effect(action, symbol, shares, amount or Decimal(0), posted, effective)


STRADDLERS_REFUSED = "refuse"
STRADDLERS_IN_D = (
    "in_d"  # a row posted after D but effective on or before D belongs to D
)
STRADDLERS_OUT_D = "out_d"  # ... belongs after D


def roll_back(
    account: str,
    rows: Sequence[Mapping[str, Any]],
    quantities: Mapping[str, Decimal],
    cash: Decimal,
    day: date,
    straddlers: str = STRADDLERS_REFUSED,
) -> RolledBack:
    """Quantities and cash of ``account`` at the close of ``day``, from the capture's own state.

    A row posted and effective after ``day`` is undone and one posted and effective on or before
    it is kept. A straddler (the two dates fall on different sides of ``day``) is placed by
    ``straddlers``: refused by default, because the retained bytes do not say which date the broker's
    balances follow; the caller that can test both placements against an independent value passes
    ``in_d`` or ``out_d`` (see ``tools/schwab_nav_derive.py``).
    """

    keyed = {symbol_key(symbol): symbol for symbol in quantities}
    result = dict(quantities)
    undone: dict[str, int] = {}
    inert: dict[str, int] = {}
    straddled: dict[str, int] = {}
    for row in rows:
        posted = _date(account, row.get("transactionDate"))
        effective = (
            _date(account, row["effectiveDate"])
            if str(row.get("effectiveDate") or "").strip()
            else posted
        )
        if posted <= day and effective <= day:
            continue
        if posted <= day or effective <= day:
            action = str(row.get("action") or "?")
            if straddlers == STRADDLERS_REFUSED:
                raise RollbackError(
                    f"{account}: a {action} row posted {posted} with effective date "
                    f"{effective} straddles {day}; the retained bytes do not say which date "
                    "the capture follows, so it is placed by the closure against the native "
                    "balance (see schwab_nav_derive), not here"
                )
            straddled[action] = straddled.get(action, 0) + 1
            if straddlers == STRADDLERS_IN_D:
                continue
        if (
            str(row.get("action") or "?") in INFORMATIONAL_ACTIONS
            and not str(row.get("shareQuantity") or "").strip()
            and not str(row.get("amount") or "").strip()
        ):
            action = str(row.get("action"))
            inert[action] = inert.get(action, 0) + 1
            continue
        effect = parse_effect(account, row)
        cash -= effect.cash
        if effect.shares:
            assert effect.symbol is not None
            symbol = keyed.setdefault(symbol_key(effect.symbol), effect.symbol)
            result[symbol] = result.get(symbol, Decimal(0)) - effect.shares
        undone[effect.action] = undone.get(effect.action, 0) + 1
    negative = {s: q for s, q in sorted(result.items()) if q < 0}
    if negative:
        listing = ", ".join(f"{s} {q}" for s, q in negative.items())
        raise RollbackError(
            f"{account}: rolling back to {day} leaves a negative quantity ({listing}): "
            "the capture and its transactions do not agree"
        )
    return RolledBack(
        {s: q for s, q in result.items() if q != 0}, cash, undone, inert, straddled
    )

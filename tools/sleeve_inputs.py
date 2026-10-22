"""Turn a provider account's ledger rows into sleeve-ledger inputs (#567).

The sleeve ledger and its verifier take plain executions, end-of-day positions,
marks and dividends.  This adapter reads them from the provider ledger's
projected account facts (IBKR catalog schema 7) and resolves each execution's
``order_reference`` with the owner's tag map.  Securities stay keyed by their
``security_id``; no symbol is needed.

Nothing is guessed.  The run stops, with a count, on:

* a trade without quantity, price, commission or direction;
* a held position without a mark price, or one listed twice on a day;
* any amount, price or commission in a currency other than the account's base
  currency, and any security that is not a plain share (a contract multiplier or
  another asset category), because the ledger values prices at face;
* a trade, dividend or position on a day the caller did not declare as an
  observation day (an empty book on a declared day is a verified empty book);
* a trade the tag map's ``BLOCK`` policy leaves unassigned;
* an unassigned trade on a day that also holds an execution without a time of
  day, because which of them came first would decide the attribution.

The caller supplies de-duplicated rows for one account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from nested_strategy_tag_map import ParentMap
from sleeve_ledger import Dividend, SleevePolicy, TaggedTrade

_DIVIDEND_KINDS = frozenset(
    {
        "CASH_ACTIVITY:Dividends",
        "CASH_ACTIVITY:Payment In Lieu Of Dividends",
        "CASH_ACTIVITY:Withholding Tax",
    }
)
_PLAIN_SHARE = "STK"


class SleeveInputError(ValueError):
    """The account's rows cannot be turned into sleeve-ledger inputs."""


@dataclass(frozen=True)
class SleeveInputs:
    trades: tuple[TaggedTrade, ...]
    positions: dict[str, dict[str, Decimal]]
    marks: dict[str, dict[str, Decimal]]
    dividends: tuple[Dividend, ...]
    resolve: Callable[[str | None, str], str | None]
    parent: ParentMap
    # The owner's fallback sleeve for each security an unmatched execution opens.
    unheld_fallback: Mapping[str, str]

    def policy(
        self,
        residual_id: str,
        *,
        aliases: Mapping[str, str] | None = None,
        alias_from_day: str | None = None,
    ) -> SleevePolicy:
        """The ledger policy: the map's sleeves, its fallback, the declared extras."""

        return SleevePolicy(
            sleeve_ids=tuple(sleeve.sleeve_id for sleeve in self.parent.sleeves),
            residual_id=residual_id,
            aliases=dict(aliases or {}),
            alias_from_day=alias_from_day,
            unheld_fallback=dict(self.unheld_fallback),
        )

    def verifier_inputs(
        self,
        policy: SleevePolicy,
        funding: Mapping[str, Any],
        account: Sequence[Mapping[str, Any]],
        days: Sequence[str],
    ) -> dict[str, Any]:
        """The same inputs, as the clean-room verifier reads them."""

        return {
            "tag_map": {
                "tag_normalization": self.parent.tag_normalization,
                "sleeves": [
                    {
                        "sleeve_id": sleeve.sleeve_id,
                        "effective_start": sleeve.effective_start.isoformat(),
                        "effective_end": None
                        if sleeve.effective_end is None
                        else sleeve.effective_end.isoformat(),
                        "tags": [
                            {"match": tag.match, "pattern": tag.pattern}
                            for tag in sleeve.tags
                        ],
                    }
                    for sleeve in self.parent.sleeves
                ],
            },
            "day_dates": {day: day for day in self.positions},
            "policy": {
                "sleeve_ids": list(policy.sleeve_ids),
                "residual_id": policy.residual_id,
                "aliases": dict(policy.aliases),
                "alias_from_day": policy.alias_from_day,
                "unheld_fallback": dict(policy.unheld_fallback),
            },
            "funding": dict(funding),
            "trades": [
                {
                    "day": trade.day,
                    "timestamp": trade.timestamp,
                    "symbol": trade.symbol,
                    "quantity": str(trade.quantity),
                    "price": str(trade.price),
                    "close_price": None
                    if trade.close_price is None
                    else str(trade.close_price),
                    "commission": str(trade.commission),
                    "tag": trade.tag,
                }
                for trade in self.trades
            ],
            "positions": {
                day: {key: str(value) for key, value in book.items()}
                for day, book in self.positions.items()
            },
            "marks": {
                day: {key: str(value) for key, value in book.items()}
                for day, book in self.marks.items()
            },
            "dividends": [
                {"day": item.day, "symbol": item.symbol, "amount": str(item.amount)}
                for item in self.dividends
            ],
            "account": [dict(item) for item in account],
            "days": list(days),
        }


def _decimal(value: object, name: str, currency: str | None = None) -> Decimal:
    """A catalog figure: plain text, or an AVAILABLE observed amount.

    With ``currency`` the amount must be stated in it; a plain figure carries no
    currency and is read as stated.
    """

    if isinstance(value, Mapping):
        if value.get("status") != "AVAILABLE":
            raise SleeveInputError(f"{name} is not available")
        if currency is not None and value.get("currency") != currency:
            raise SleeveInputError(
                f"{name} is in {value.get('currency')!r}, not the base {currency!r}"
            )
        value = value.get("value")
    if not isinstance(value, str):
        raise SleeveInputError(f"{name} is missing")
    try:
        return Decimal(value)
    except ArithmeticError as error:
        raise SleeveInputError(f"{name} is not a number: {value!r}") from error


def _day(value: object, name: str, declared: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise SleeveInputError(f"{name} is missing")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise SleeveInputError(f"{name} is not an ISO date: {value!r}") from error
    if value not in declared:
        raise SleeveInputError(f"{name} {value} is not a declared observation day")
    return value


def _resolver(parent: ParentMap) -> Callable[[str | None, str], str | None]:
    def resolve(tag: str | None, day: str) -> str | None:
        if tag is None:
            return None
        found = parent.resolve(tag, date.fromisoformat(day))
        return None if found is None else found.sleeve_id

    return resolve


def _unsupported_securities(positions: Sequence[Mapping[str, Any]]) -> set[str]:
    """Securities the ledger cannot value at face: not shares, or with a multiplier."""

    out: set[str] = set()
    for row in positions:
        security = row.get("security_id")
        if not isinstance(security, str):
            continue
        category = row.get("asset_category")
        multiplier = row.get("multiplier")
        if category not in (None, _PLAIN_SHARE) or (
            multiplier is not None and _decimal(multiplier, "multiplier") != 1
        ):
            out.add(security)
    return out


def sleeve_inputs(
    transactions: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    parent: ParentMap,
    *,
    base_currency: str,
    days: Sequence[str],
) -> SleeveInputs:
    """Executions, positions, marks and dividends of one account.

    ``days`` are the account's declared observation days (ISO dates); each has a
    book, empty when the provider reported no open position that day.
    """

    declared = frozenset(days)
    if not declared:
        raise SleeveInputError("no observation days are declared")
    resolve = _resolver(parent)
    unsupported = _unsupported_securities(positions)
    trades: list[TaggedTrade] = []
    dividends: list[Dividend] = []
    untimed_days: set[str] = set()
    unmatched_days: set[str] = set()
    fallback: dict[str, str] = {}
    gaps = {
        "quantity": 0,
        "price": 0,
        "commission": 0,
        "unassigned": 0,
        "unsupported security": 0,
    }
    for row in transactions:
        kind = row.get("kind")
        if not isinstance(kind, str):
            continue
        security = row.get("security_id")
        if kind.startswith("TRADE:"):
            if not isinstance(security, str):
                raise SleeveInputError("a trade has no security")
            day = _day(row.get("event_date"), "trade event_date", declared)
            if row.get("quantity") is None:
                gaps["quantity"] += 1
                continue
            if row.get("execution_price") is None:
                gaps["price"] += 1
                continue
            if row.get("commission") is None:
                gaps["commission"] += 1
                continue
            if security in unsupported:
                gaps["unsupported security"] += 1
                continue
            # The trade's own amount states its currency; price, close price and
            # commission are in it, so all of it must be the base currency.
            _decimal(row.get("amount"), "trade amount", base_currency)
            tag = row.get("order_reference")
            assigned = resolve(tag if isinstance(tag, str) else None, day)
            if assigned is None:
                if parent.unmatched_mode == "BLOCK":
                    gaps["unassigned"] += 1
                    continue
                unmatched_days.add(day)
                if parent.fallback_sleeve_id is not None:
                    fallback[security] = parent.fallback_sleeve_id
            if not row.get("executed_time"):
                untimed_days.add(day)
            close = row.get("close_price")
            trades.append(
                TaggedTrade(
                    day=day,
                    timestamp=f"{day};{row.get('executed_time') or ''}",
                    symbol=security,
                    quantity=_decimal(row["quantity"], "trade quantity"),
                    price=_decimal(row["execution_price"], "trade price"),
                    close_price=None
                    if close is None
                    else _decimal(close, "trade close price"),
                    commission=_decimal(
                        row["commission"], "trade commission", base_currency
                    ),
                    tag=tag if isinstance(tag, str) else None,
                )
            )
        elif kind in _DIVIDEND_KINDS and isinstance(security, str):
            if security in unsupported:
                gaps["unsupported security"] += 1
                continue
            dividends.append(
                Dividend(
                    day=_day(
                        row.get("nav_recognition_date") or row.get("event_date"),
                        "dividend date",
                        declared,
                    ),
                    symbol=security,
                    amount=_decimal(
                        row.get("amount"), "dividend amount", base_currency
                    ),
                )
            )
    missing = {name: count for name, count in gaps.items() if count}
    if missing:
        raise SleeveInputError(f"trades the sleeve ledger cannot use: {missing}")
    ambiguous = sorted(untimed_days & unmatched_days)
    if ambiguous:
        raise SleeveInputError(
            "unassigned trades on days with an execution that has no time of day "
            f"(their order decides the attribution): {ambiguous}"
        )
    trades.sort(key=lambda trade: (trade.day, trade.timestamp, trade.symbol))

    held: dict[str, dict[str, Decimal]] = {day: {} for day in sorted(declared)}
    marks: dict[str, dict[str, Decimal]] = {day: {} for day in sorted(declared)}
    unvalued = 0
    for row in positions:
        day = _day(row.get("as_of_date"), "position as_of_date", declared)
        security = row.get("security_id")
        if not isinstance(security, str):
            raise SleeveInputError("a position has no security")
        quantity = _decimal(row.get("quantity"), "position quantity")
        if security in unsupported and quantity != 0:
            raise SleeveInputError(f"{security} is not a plain share")
        if security in held[day]:
            raise SleeveInputError(f"{security} is positioned twice on {day}")
        held[day][security] = quantity
        if quantity == 0:
            continue
        if row.get("mark_price") is None:
            unvalued += 1
            continue
        marks[day][security] = _decimal(row["mark_price"], "position mark price")
    if unvalued:
        raise SleeveInputError(f"{unvalued} held positions have no mark price")
    return SleeveInputs(
        trades=tuple(trades),
        positions=held,
        marks=marks,
        dividends=tuple(dividends),
        resolve=resolve,
        parent=parent,
        unheld_fallback=fallback,
    )

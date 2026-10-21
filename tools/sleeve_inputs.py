"""Turn a provider account's ledger rows into sleeve-ledger inputs (#567).

The sleeve ledger and its verifier take plain executions, end-of-day positions,
marks and dividends.  This adapter reads them from the provider ledger's
projected account facts (IBKR catalog schema 7) and resolves each execution's
``order_reference`` with the owner's tag map.  Securities stay keyed by their
``security_id``; no symbol is needed.

Nothing is guessed: a trade that lacks a quantity, price, commission or
direction, a held position without a mark price, or a trade the tag map's ``BLOCK`` policy
leaves unassigned stops the run with a count, never a default.  The caller
supplies de-duplicated rows for one account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from nested_strategy_tag_map import ParentMap
from sleeve_ledger import Dividend, TaggedTrade

_DIVIDEND_KINDS = frozenset(
    {
        "CASH_ACTIVITY:Dividends",
        "CASH_ACTIVITY:Payment In Lieu Of Dividends",
        "CASH_ACTIVITY:Withholding Tax",
    }
)


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

    def verifier_inputs(
        self,
        policy: Mapping[str, Any],
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
            "policy": dict(policy),
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


def _decimal(value: object, name: str) -> Decimal:
    """A catalog figure: plain text, or an AVAILABLE observed amount."""

    if isinstance(value, Mapping):
        if value.get("status") != "AVAILABLE":
            raise SleeveInputError(f"{name} is not available")
        value = value.get("value")
    if not isinstance(value, str):
        raise SleeveInputError(f"{name} is missing")
    try:
        return Decimal(value)
    except ArithmeticError as error:
        raise SleeveInputError(f"{name} is not a number: {value!r}") from error


def _day(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise SleeveInputError(f"{name} is missing")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise SleeveInputError(f"{name} is not an ISO date: {value!r}") from error
    return value


def _resolver(parent: ParentMap) -> Callable[[str | None, str], str | None]:
    def resolve(tag: str | None, day: str) -> str | None:
        if tag is None:
            return None
        found = parent.resolve(tag, date.fromisoformat(day))
        return None if found is None else found.sleeve_id

    return resolve


def sleeve_inputs(
    transactions: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    parent: ParentMap,
) -> SleeveInputs:
    """Executions, positions, marks and dividends of one account."""

    resolve = _resolver(parent)
    trades: list[TaggedTrade] = []
    dividends: list[Dividend] = []
    gaps = {"quantity": 0, "price": 0, "commission": 0, "unassigned": 0}
    for row in transactions:
        kind = row.get("kind")
        if not isinstance(kind, str):
            continue
        security = row.get("security_id")
        if kind.startswith("TRADE:"):
            if not isinstance(security, str):
                raise SleeveInputError("a trade has no security")
            day = _day(row.get("event_date"), "trade event_date")
            if row.get("quantity") is None:
                gaps["quantity"] += 1
                continue
            if row.get("execution_price") is None:
                gaps["price"] += 1
                continue
            if row.get("commission") is None:
                gaps["commission"] += 1
                continue
            tag = row.get("order_reference")
            if parent.unmatched_mode == "BLOCK" and resolve(tag, day) is None:
                gaps["unassigned"] += 1
                continue
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
                    commission=_decimal(row["commission"], "trade commission"),
                    tag=tag if isinstance(tag, str) else None,
                )
            )
        elif kind in _DIVIDEND_KINDS and isinstance(security, str):
            dividends.append(
                Dividend(
                    day=_day(
                        row.get("nav_recognition_date") or row.get("event_date"),
                        "dividend date",
                    ),
                    symbol=security,
                    amount=_decimal(row.get("amount"), "dividend amount"),
                )
            )
    missing = {name: count for name, count in gaps.items() if count}
    if missing:
        raise SleeveInputError(f"trades the sleeve ledger cannot use: {missing}")
    trades.sort(key=lambda trade: (trade.day, trade.timestamp, trade.symbol))

    held: dict[str, dict[str, Decimal]] = {}
    marks: dict[str, dict[str, Decimal]] = {}
    unvalued = 0
    for row in positions:
        day = _day(row.get("as_of_date"), "position as_of_date")
        security = row.get("security_id")
        if not isinstance(security, str):
            raise SleeveInputError("a position has no security")
        quantity = _decimal(row.get("quantity"), "position quantity")
        if security in held.get(day, {}):
            raise SleeveInputError(f"{security} is positioned twice on {day}")
        held.setdefault(day, {})[security] = quantity
        marks.setdefault(day, {})
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
    )

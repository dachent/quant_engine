"""Order a rebalance batch: sells first, then only the buys cash can pay for.

Pure and offline.  Amounts are Decimal dollar notionals.  A buy is accepted
whole or rejected whole with a reason; it is never partially filled and never
silently resized.  Refs #609 (a buy submitted ahead of the sells that fund it
is rejected for buying power).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Sequence

INSUFFICIENT_CASH: Final = "INSUFFICIENT_CASH"


@dataclass(frozen=True)
class Order:
    symbol: str
    notional: Decimal


@dataclass(frozen=True)
class Rejection:
    order: Order
    reason: str
    available_cash: Decimal


@dataclass(frozen=True)
class OrderBatch:
    """``sequence`` is the submission order: every sell, then accepted buys."""

    sequence: tuple[tuple[str, Order], ...]
    rejected_buys: tuple[Rejection, ...]
    cash_after: Decimal


def _checked(orders: Sequence[Order], side: str) -> None:
    for order in orders:
        if not order.symbol.strip():
            raise ValueError(f"{side} order has no symbol")
        if order.notional <= 0:
            raise ValueError(f"{side} {order.symbol} notional must be positive")


def order_batch(
    sells: Sequence[Order],
    buys: Sequence[Order],
    cash: Decimal,
    *,
    count_sell_proceeds: bool = True,
) -> OrderBatch:
    """Plan submission order and report every buy that cannot be afforded.

    ``count_sell_proceeds=False`` models accounts where sale proceeds are not
    spendable until settlement: buys then draw on ``cash`` alone.  Buys are
    considered in the order given and each draws down the remaining cash.
    """
    if cash < 0:
        raise ValueError("cash must not be negative")
    _checked(sells, "sell")
    _checked(buys, "buy")
    sold = {order.symbol for order in sells}
    both = sorted(sold & {order.symbol for order in buys})
    if both:
        raise ValueError(f"symbols appear on both sides: {both}")
    available = cash + (
        sum((order.notional for order in sells), Decimal(0))
        if count_sell_proceeds
        else Decimal(0)
    )
    sequence: list[tuple[str, Order]] = [("SELL", order) for order in sells]
    rejected: list[Rejection] = []
    for order in buys:
        if order.notional > available:
            rejected.append(Rejection(order, INSUFFICIENT_CASH, available))
            continue
        available -= order.notional
        sequence.append(("BUY", order))
    return OrderBatch(tuple(sequence), tuple(rejected), available)

"""Derive per-sleeve holdings and price P&L from tagged executions (#567).

Each nested strategy of one provider account is a sleeve.  Every execution
carries a provider tag that the owner's tag map resolves to a sleeve; what no
tag resolves is handled by a declared policy, never by a guess.  Sleeve
holdings are rebuilt from the executions, reconciled each day to the account's
own positions (corporate actions scale, a vanished position closes, an
unexplained difference goes to a named residual sleeve and is reported), and
valued at the account's marks.  The result is a pure function of its inputs:
no file access, no provider names, no amounts of its own.

This is the holdings and price-P&L layer.  NAV, funding steps, cost allocation
and the closure to the account follow in later stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Sequence

_ZERO = Decimal(0)
_EPSILON = Decimal("1e-9")
_RECONCILE_TOLERANCE = Decimal("1e-6")
_DIVIDEND_TYPES = frozenset(
    {"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"}
)


class SleeveLedgerError(ValueError):
    """The sleeve ledger inputs are inconsistent."""


@dataclass(frozen=True)
class TaggedTrade:
    day: str
    timestamp: str
    symbol: str
    quantity: Decimal
    price: Decimal
    close_price: Decimal | None
    commission: Decimal
    tag: str | None


@dataclass(frozen=True)
class Dividend:
    day: str
    symbol: str
    amount: Decimal


@dataclass(frozen=True)
class SleevePolicy:
    """Declared inputs the executions cannot supply themselves."""

    # Real sleeves in a fixed order; unmatched tags are assigned among them.
    sleeve_ids: tuple[str, ...]
    # The residual sleeve for what no rule assigns (never a real strategy).
    residual_id: str
    # old symbol -> new symbol, applied from ``alias_from_day`` on, or from the
    # day ``alias_days`` states for that old symbol (a provider that re-issues a
    # security id on each reverse split renames many securities on many days).
    aliases: Mapping[str, str] = field(default_factory=dict)
    alias_from_day: str | None = None
    alias_days: Mapping[str, str] = field(default_factory=dict)
    # symbol -> sleeve for an unmatched execution in a symbol no sleeve holds.
    unheld_fallback: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SleeveDay:
    day: str
    sleeve_id: str
    value: Decimal
    prev_value: Decimal
    price_pnl: Decimal
    buys: Decimal
    net_trade: Decimal
    gross_traded: Decimal
    commission: Decimal
    dividend: Decimal
    symbol_count: int


@dataclass(frozen=True)
class SleeveLedger:
    rows: tuple[SleeveDay, ...]
    unallocated_dividends: Mapping[str, Decimal]
    # Every judgement the derivation made, for the reader to audit.
    manual_assignments: tuple[tuple[str, str, Decimal, str, str], ...]
    reconciliation_gaps: tuple[tuple[str, str, Decimal, Decimal], ...]
    reconciliation_scalings: tuple[tuple[str, str, Decimal], ...]
    # Offsetting fractions closed because the account holds nothing.
    offsetting_residues: tuple[tuple[str, str, tuple[tuple[str, Decimal], ...]], ...]


Resolver = Callable[[str | None, str], str | None]


def derive_sleeve_ledger(
    trades: Sequence[TaggedTrade],
    positions: Mapping[str, Mapping[str, Decimal]],
    marks: Mapping[str, Mapping[str, Decimal]],
    dividends: Sequence[Dividend],
    resolve: Resolver,
    policy: SleevePolicy,
) -> SleeveLedger:
    """Rebuild each sleeve's holdings day by day and value them at the marks.

    ``positions[day][symbol]`` and ``marks[day][symbol]`` are the account's
    own end-of-day figures; their dates are the ledger's dates.  ``resolve``
    maps an execution's tag and day to a sleeve id or None.
    """

    if set(positions) != set(marks):
        raise SleeveLedgerError("positions and marks cover different days")
    real = policy.sleeve_ids
    residual = policy.residual_id
    if residual in real or len(set(real)) != len(real):
        raise SleeveLedgerError("sleeve ids must be distinct from the residual")
    known = set(real) | {residual}
    for target in policy.unheld_fallback.values():
        if target not in real:
            raise SleeveLedgerError("fallback names an unknown sleeve")
    every = (*real, residual)
    days = sorted(positions)
    for day in days:
        missing = sorted(
            symbol
            for symbol, amount in positions[day].items()
            if amount != 0 and symbol not in marks[day]
        )
        if missing:
            raise SleeveLedgerError(
                f"held positions without a mark on {day}: {', '.join(missing)}"
            )
    by_day: dict[str, list[TaggedTrade]] = {}
    for trade in trades:
        if trade.day not in positions:
            raise SleeveLedgerError(
                f"execution on a day outside the ledger days: {trade.day}"
            )
        by_day.setdefault(trade.day, []).append(trade)
    dividends_by_day: dict[str, list[Dividend]] = {}
    for dividend in dividends:
        dividends_by_day.setdefault(dividend.day, []).append(dividend)
    inverse_alias = {new: old for old, new in policy.aliases.items()}

    quantity: dict[str, dict[str, Decimal]] = {sleeve: {} for sleeve in every}
    rows: list[SleeveDay] = []
    unallocated: dict[str, Decimal] = {}
    manual: list[tuple[str, str, Decimal, str, str]] = []
    gaps: list[tuple[str, str, Decimal, Decimal]] = []
    scalings: list[tuple[str, str, Decimal]] = []
    offsets: list[tuple[str, str, tuple[tuple[str, Decimal], ...]]] = []

    for index, day in enumerate(days):
        previous_day = days[index - 1] if index else None
        for old, new in policy.aliases.items():
            alias_start = policy.alias_days.get(old, policy.alias_from_day)
            if alias_start is None or day < alias_start:
                continue
            for member in every:
                if old in quantity[member]:
                    quantity[member][new] = quantity[member].get(new, _ZERO) + quantity[
                        member
                    ].pop(old)
        previous_quantity = {
            sleeve: {k: v for k, v in quantity[sleeve].items() if abs(v) > _EPSILON}
            for sleeve in every
        }
        legs: list[tuple[str, TaggedTrade, Decimal, Decimal]] = []
        # Executions are stamped to the second, so several share a timestamp.
        # Their order must not depend on the input's row order: within one
        # timestamp the tag-resolved executions go first (their sleeve is
        # evidenced), then the unassigned ones, which take whoever holds the
        # symbol by then; the remaining keys only make the order total.
        ordered = sorted(
            by_day.get(day, ()),
            key=lambda item: (
                item.timestamp,
                resolve(item.tag, day) is None,
                item.symbol,
                item.quantity,
                item.price,
                item.commission,
            ),
        )
        for trade in ordered:
            sleeve: str | None = resolve(trade.tag, day)
            if sleeve is not None and sleeve not in known:
                raise SleeveLedgerError(f"tag resolves to an unknown sleeve: {sleeve}")
            symbol = trade.symbol
            if sleeve is None:
                holders = [
                    item
                    for item in real
                    if quantity[item].get(symbol, _ZERO) * trade.quantity < 0
                ]
                if len(holders) == 1:
                    sleeve = holders[0]
                    manual.append((day, symbol, trade.quantity, "assigned", sleeve))
                elif len(holders) > 1:
                    total = sum(
                        (abs(quantity[item][symbol]) for item in holders), _ZERO
                    )
                    for item in holders:
                        share = abs(quantity[item][symbol]) / total
                        legs.append(
                            (
                                item,
                                trade,
                                trade.quantity * share,
                                trade.commission * share,
                            )
                        )
                        quantity[item][symbol] += trade.quantity * share
                    manual.append(
                        (day, symbol, trade.quantity, "split", ",".join(holders))
                    )
                    continue
                else:
                    sleeve = policy.unheld_fallback.get(symbol, residual)
                    manual.append((day, symbol, trade.quantity, "unheld", sleeve))
            legs.append((sleeve, trade, trade.quantity, trade.commission))
            quantity[sleeve][symbol] = (
                quantity[sleeve].get(symbol, _ZERO) + trade.quantity
            )

        # Reconcile to the account's own positions: a corporate action scales,
        # a position the account no longer holds closes, a difference nobody
        # can attribute goes to the residual sleeve.
        scale: dict[str, Decimal] = {}
        account_positions = positions[day]
        # Sorted, so the reported gaps and scalings never depend on hash order.
        for symbol in sorted(
            set(account_positions)
            | {key for sleeve in every for key in quantity[sleeve]}
        ):
            total = sum(
                (quantity[sleeve].get(symbol, _ZERO) for sleeve in every), _ZERO
            )
            actual = account_positions.get(symbol, _ZERO)
            if abs(total - actual) <= _RECONCILE_TOLERANCE:
                if actual == 0 and any(
                    abs(quantity[sleeve].get(symbol, _ZERO)) > _EPSILON
                    for sleeve in every
                ):
                    # The sleeves net to the account's nothing but carry
                    # offsetting fractions (a pro-rata split artefact): close
                    # them and say so; none of it is a position.
                    offsets.append(
                        (
                            day,
                            symbol,
                            tuple(
                                (sleeve, quantity[sleeve][symbol])
                                for sleeve in every
                                if abs(quantity[sleeve].get(symbol, _ZERO)) > _EPSILON
                            ),
                        )
                    )
                    for sleeve in every:
                        quantity[sleeve].pop(symbol, None)
                continue
            gaps.append((day, symbol, total, actual))
            if abs(total) > _EPSILON and abs(actual) > _EPSILON and total * actual > 0:
                factor = actual / total
                scale[symbol] = factor
                for sleeve in every:
                    if symbol in quantity[sleeve]:
                        quantity[sleeve][symbol] *= factor
                scalings.append((day, symbol, factor))
            elif abs(total) > _EPSILON and actual == 0:
                for sleeve in every:
                    quantity[sleeve].pop(symbol, None)
            else:
                quantity[residual][symbol] = quantity[residual].get(symbol, _ZERO) + (
                    actual - total
                )

        dividend_share: dict[str, Decimal] = {}
        for dividend in dividends_by_day.get(day, ()):
            held = {
                sleeve: previous_quantity[sleeve].get(dividend.symbol, _ZERO)
                for sleeve in every
                if previous_quantity[sleeve].get(dividend.symbol, _ZERO) > 0
            } or {
                sleeve: quantity[sleeve].get(dividend.symbol, _ZERO)
                for sleeve in every
                if quantity[sleeve].get(dividend.symbol, _ZERO) > 0
            }
            total_held = sum(held.values(), _ZERO)
            if total_held == 0:
                unallocated[day] = unallocated.get(day, _ZERO) + dividend.amount
                continue
            for sleeve, amount_held in held.items():
                dividend_share[sleeve] = dividend_share.get(sleeve, _ZERO) + (
                    dividend.amount * amount_held / total_held
                )

        day_marks = marks[day]
        for sleeve in every:
            value = _ZERO
            for symbol, amount in quantity[sleeve].items():
                if abs(amount) <= _EPSILON:
                    continue
                if symbol not in day_marks:
                    if abs(amount) <= _RECONCILE_TOLERANCE:
                        continue  # reconciliation dust, worth nothing
                    raise SleeveLedgerError(
                        f"sleeve {sleeve} holds {symbol} without a mark on {day}"
                    )
                value += amount * day_marks[symbol]
            previous_value = _ZERO
            if previous_day is not None:
                previous_marks = marks[previous_day]
                for symbol, amount in previous_quantity[sleeve].items():
                    mark = previous_marks.get(symbol)
                    if mark is None:
                        mark = previous_marks.get(inverse_alias.get(symbol, ""))
                    if mark is None:
                        if abs(amount) <= _RECONCILE_TOLERANCE:
                            continue  # reconciliation dust, worth nothing
                        raise SleeveLedgerError(
                            f"sleeve {sleeve} held {symbol} without a mark on {previous_day}"
                        )
                    previous_value += amount * mark
            pnl = buys = commission = net = gross = _ZERO
            # The residual sleeve books its own executions exactly like a real
            # sleeve; what only the reconciliation moved stays in the value
            # change, not in price P&L.
            for symbol, amount in previous_quantity[sleeve].items():
                first = marks[previous_day].get(symbol) if previous_day else None
                if first is None and symbol in policy.aliases.values():
                    first = (
                        marks[previous_day].get(inverse_alias[symbol])
                        if previous_day
                        else None
                    )
                last = day_marks.get(symbol)
                if last is None:
                    closes = [
                        leg[1].close_price
                        for leg in legs
                        if leg[1].symbol == symbol and leg[1].close_price is not None
                    ]
                    last = closes[0] if closes else first
                if first is None or last is None:
                    continue
                factor = scale.get(symbol, Decimal(1))
                pnl += amount * factor * (last - first / factor)
            for leg_sleeve, trade, leg_quantity, leg_commission in legs:
                if leg_sleeve != sleeve:
                    continue
                mark = day_marks.get(trade.symbol)
                if mark is None:
                    mark = (
                        trade.close_price
                        if trade.close_price is not None
                        else trade.price
                    )
                pnl += leg_quantity * (mark - trade.price)
                commission += leg_commission
                net += leg_quantity * trade.price
                gross += abs(leg_quantity * trade.price)
                if leg_quantity > 0:
                    buys += leg_quantity * trade.price
            rows.append(
                SleeveDay(
                    day=day,
                    sleeve_id=sleeve,
                    value=value,
                    prev_value=previous_value,
                    price_pnl=pnl,
                    buys=buys,
                    net_trade=net,
                    gross_traded=gross,
                    commission=commission,
                    dividend=dividend_share.get(sleeve, _ZERO),
                    symbol_count=sum(
                        1
                        for amount in quantity[sleeve].values()
                        if abs(amount) > _EPSILON
                    ),
                )
            )
    return SleeveLedger(
        rows=tuple(rows),
        unallocated_dividends=dict(unallocated),
        manual_assignments=tuple(manual),
        reconciliation_gaps=tuple(gaps),
        reconciliation_scalings=tuple(scalings),
        offsetting_residues=tuple(offsets),
    )


def dividend_rows(
    cash_transactions: Sequence[Mapping[str, str]],
) -> list[Dividend]:
    """The dividend-type cash transactions that name a symbol."""

    return [
        Dividend(row["reportDate"], row["symbol"], Decimal(row["amount"]))
        for row in cash_transactions
        if row.get("type") in _DIVIDEND_TYPES and row.get("symbol")
    ]

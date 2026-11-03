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
from decimal import ROUND_FLOOR, Decimal
import hashlib
from typing import Callable, Final, Mapping, Sequence

# The order of executions that share one timestamp (the provider stamps to the
# second).  It decides who holds a symbol when an unassigned execution lands, so
# the artifact states it and binds its hash into the code digest.
SAME_SECOND_TIE_BREAK: Final = (
    "TAG_RESOLVED_FIRST>SECURITY_ID>QUANTITY>PRICE>COMMISSION"
)
SAME_SECOND_TIE_BREAK_SHA256: Final = hashlib.sha256(
    SAME_SECOND_TIE_BREAK.encode("ascii")
).hexdigest()
_ZERO = Decimal(0)
_EPSILON = Decimal("1e-9")
_RECONCILE_TOLERANCE = Decimal("1e-6")
_WHOLE_SNAP = Decimal("1e-12")
# A cash-in-lieu execution follows its corporate action within this many ledger
# days (counted from the first ledger day on or after the action's date).
CASH_IN_LIEU_WINDOW_DAYS: Final = 5
CASH_IN_LIEU_HOW: Final = "corporate_action_fraction"
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
    # The provider's own record of the execution (disclosed with a cash in lieu).
    reference: str | None = None


@dataclass(frozen=True)
class CorporateAction:
    """One corporate action of the account, as its ledger rows state it.

    ``new_security`` receives ``new_quantity`` shares; when the action also
    surrenders a security (a reverse split re-issues the security id) the old
    security and the shares it gave up are stated too.  A row set that does not
    identify the surrendered security leaves both None.
    """

    day: str
    kind: str
    new_security: str
    new_quantity: Decimal
    old_security: str | None = None
    old_quantity: Decimal | None = None


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
class SleeveEntitlement:
    """One sleeve's own share of a corporate action's entitlement."""

    sleeve_id: str
    held_quantity: Decimal
    entitlement: Decimal
    whole_shares: Decimal
    fraction: Decimal
    charged_quantity: Decimal
    cash_credited: Decimal
    commission: Decimal


@dataclass(frozen=True)
class CashInLieu:
    """An untagged execution that liquidated a corporate-action fraction."""

    day: str
    security_id: str
    action: CorporateAction
    ratio: Decimal
    reference: str | None
    timestamp: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    sleeves: tuple[SleeveEntitlement, ...]
    entitled_total: Decimal
    whole_total: Decimal
    fraction_total: Decimal
    account_position: Decimal


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
    # Corporate-action fractions each sleeve was charged by its own entitlement.
    cash_in_lieu: tuple[CashInLieu, ...] = ()
    # End-of-day holdings per sleeve (reconciled), for closure checks.
    holdings: Mapping[str, Mapping[str, Mapping[str, Decimal]]] = field(
        default_factory=dict, compare=False, repr=False
    )


Resolver = Callable[[str | None, str], str | None]


def derive_sleeve_ledger(
    trades: Sequence[TaggedTrade],
    positions: Mapping[str, Mapping[str, Decimal]],
    marks: Mapping[str, Mapping[str, Decimal]],
    dividends: Sequence[Dividend],
    resolve: Resolver,
    policy: SleevePolicy,
    corporate_actions: Sequence[CorporateAction] = (),
) -> SleeveLedger:
    """Rebuild each sleeve's holdings day by day and value them at the marks.

    ``positions[day][symbol]`` and ``marks[day][symbol]`` are the account's
    own end-of-day figures; their dates are the ledger's dates.  ``resolve``
    maps an execution's tag and day to a sleeve id or None.  An untagged sale
    of a security that several sleeves hold is split pro rata by held quantity,
    except when it liquidates the fraction of a corporate action's entitlement
    (``corporate_actions``): then each sleeve is charged its own fractional
    entitlement, the only split that leaves every sleeve whole shares.
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
    actions_by_new: dict[str, list[CorporateAction]] = {}
    for action in corporate_actions:
        actions_by_new.setdefault(action.new_security, []).append(action)
    used_actions: set[int] = set()
    cash_in_lieu: list[CashInLieu] = []
    holdings_by_day: dict[str, dict[str, dict[str, Decimal]]] = {}

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
                    matched = _matching_action(
                        trade, day, days, actions_by_new.get(symbol, ()), used_actions
                    )
                    if matched is not None:
                        record = _assign_fraction(
                            trade,
                            matched,
                            quantity,
                            every,
                            residual,
                            positions[day].get(symbol, _ZERO),
                            legs,
                            day,
                        )
                        used_actions.add(id(matched))
                        cash_in_lieu.append(record)
                        manual.append(
                            (
                                day,
                                symbol,
                                trade.quantity,
                                CASH_IN_LIEU_HOW,
                                ",".join(
                                    item.sleeve_id
                                    for item in record.sleeves
                                    if item.fraction
                                ),
                            )
                        )
                        continue
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

        holdings_by_day[day] = {
            sleeve: {
                key: value
                for key, value in quantity[sleeve].items()
                if abs(value) > _EPSILON
            }
            for sleeve in every
        }
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
        cash_in_lieu=tuple(cash_in_lieu),
        holdings=holdings_by_day,
    )


def _fraction_of(amount: Decimal) -> Decimal:
    return amount - amount.to_integral_value(rounding=ROUND_FLOOR)


def _matching_action(
    trade: TaggedTrade,
    day: str,
    days: Sequence[str],
    candidates: Sequence[CorporateAction],
    used: set[int],
) -> CorporateAction | None:
    """The corporate action whose entitlement fraction this untagged sale liquidates.

    From data only: the sale's security is the one the action delivered, the
    sale is untagged, falls within the cash-in-lieu window after the action, and
    its size is exactly the fractional part of the action's aggregate entitlement.
    """

    if trade.quantity >= 0:
        return None
    for action in candidates:
        if id(action) in used or day < action.day:
            continue
        first = next(
            (index for index, item in enumerate(days) if item >= action.day), None
        )
        if first is None or days.index(day) - first > CASH_IN_LIEU_WINDOW_DAYS:
            continue
        fraction = _fraction_of(action.new_quantity)
        if fraction > 0 and abs(-trade.quantity - fraction) <= _RECONCILE_TOLERANCE:
            return action
    return None


def _assign_fraction(
    trade: TaggedTrade,
    action: CorporateAction,
    quantity: dict[str, dict[str, Decimal]],
    every: Sequence[str],
    residual: str,
    account_position: Decimal,
    legs: list[tuple[str, TaggedTrade, Decimal, Decimal]],
    day: str,
) -> CashInLieu:
    """Charge each sleeve its own fractional entitlement; fail closed otherwise."""

    symbol = trade.symbol
    where = f"cash in lieu of {symbol} on {day}"
    if action.old_security is None or not action.old_quantity:
        raise SleeveLedgerError(
            f"{where}: the corporate action does not identify the security it "
            "surrendered, so no sleeve's entitlement can be derived"
        )
    held = {
        sleeve: quantity[sleeve][symbol]
        for sleeve in every
        if abs(quantity[sleeve].get(symbol, _ZERO)) > _EPSILON
    }
    if any(value < 0 for value in held.values()) or residual in held:
        raise SleeveLedgerError(
            f"{where}: a short or residual holding has no entitlement"
        )
    total_held = sum(held.values(), _ZERO)
    old_quantity, new_quantity = action.old_quantity, action.new_quantity
    if abs(total_held - old_quantity) <= _RECONCILE_TOLERANCE:
        # Holdings are still in the surrendered security's shares.
        in_old_units = True
    elif abs(total_held - new_quantity) <= _RECONCILE_TOLERANCE:
        in_old_units = False
    else:
        raise SleeveLedgerError(
            f"{where}: the sleeves hold {total_held}, neither the {old_quantity} "
            f"surrendered nor the {new_quantity} delivered"
        )
    entitlements: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for sleeve, amount in held.items():
        entitled = amount * new_quantity / old_quantity if in_old_units else amount
        nearest = entitled.to_integral_value()
        if abs(entitled - nearest) <= _WHOLE_SNAP:
            entitled = nearest
        whole = entitled.to_integral_value(rounding=ROUND_FLOOR)
        entitlements[sleeve] = (entitled, whole, entitled - whole)
    fraction_total = sum((item[2] for item in entitlements.values()), _ZERO)
    sold = -trade.quantity
    if abs(fraction_total - sold) > _RECONCILE_TOLERANCE:
        raise SleeveLedgerError(
            f"{where}: the sleeves' own fractional entitlements total "
            f"{fraction_total}, the execution sold {sold}"
        )
    whole_total = sum((item[1] for item in entitlements.values()), _ZERO)
    if abs(whole_total - account_position) > _RECONCILE_TOLERANCE:
        raise SleeveLedgerError(
            f"{where}: the sleeves keep {whole_total} whole shares, the account "
            f"holds {account_position}"
        )
    rows: list[SleeveEntitlement] = []
    for sleeve in every:
        if sleeve not in held:
            continue
        entitled, whole, fraction = entitlements[sleeve]
        charged = trade.quantity * fraction / fraction_total if fraction else _ZERO
        commission = trade.commission * fraction / fraction_total if fraction else _ZERO
        rows.append(
            SleeveEntitlement(
                sleeve_id=sleeve,
                held_quantity=held[sleeve],
                entitlement=entitled,
                whole_shares=whole,
                fraction=fraction,
                charged_quantity=charged,
                cash_credited=-charged * trade.price,
                commission=commission,
            )
        )
        if not fraction:
            continue
        legs.append((sleeve, trade, charged, commission))
        quantity[sleeve][symbol] += (
            charged * old_quantity / new_quantity if in_old_units else charged
        )
    return CashInLieu(
        day=day,
        security_id=symbol,
        action=action,
        ratio=new_quantity / old_quantity,
        reference=trade.reference,
        timestamp=trade.timestamp,
        quantity=trade.quantity,
        price=trade.price,
        commission=trade.commission,
        sleeves=tuple(rows),
        entitled_total=sum((item.entitlement for item in rows), _ZERO),
        whole_total=whole_total,
        fraction_total=fraction_total,
        account_position=account_position,
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

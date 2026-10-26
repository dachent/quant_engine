"""Sleeve NAV, flows, allocated costs and closure to the account (#567).

Stage 3b of the nested-strategy ledger.  It takes the holdings and price-P&L
ledger of stage 3a, the owner's declared funding events and the account's own
daily figures, and derives each sleeve's NAV, the flows that fund and drain it,
its return, the share of the account-level costs it carries, and how the sleeves
close back to the account.  A pure function of its inputs: no files, no
provider names, no amounts of its own.

Two inputs are declarations, not derivations, and are carried with their
evidence status so nothing unresolved is presented as settled:

* funding events (a sleeve is funded, drained, swept or liquidated on a day);
  an unresolved event on a live sleeve is refused with the provider pull that
  would resolve it;
* the exposure-share convention that spreads account-level interest, fees and
  dividend accruals over the sleeves.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping, Sequence

from sleeve_ledger import SleeveDay, SleeveLedger

_ZERO = Decimal(0)
_ONE = Decimal(1)
_NAV_FLOOR = Decimal(1)  # below this a base or exposure is "nothing held"

KINDS = ("IN", "OUT", "SWEEP", "EXIT")
# EVIDENCED: stated by two independent sources (the provider's own cash and
# executions, plus the model platform's value series) rather than inferred.
EVIDENCE = ("DERIVED", "EVIDENCED", "DECLARED_UNRESOLVED")
# The provider holds one cash balance per account.  Cash no sleeve owns sits in
# this bucket, which is the counterparty of a reallocation between a sleeve and
# the rest of the account (never an external flow).
UNALLOCATED_ACCOUNT_CASH = "UNALLOCATED_ACCOUNT_CASH"
COUNTERPARTIES = (UNALLOCATED_ACCOUNT_CASH,)


class SleeveNavError(ValueError):
    """The sleeve NAV inputs are inconsistent."""


@dataclass(frozen=True)
class FundingEvent:
    """A declared funding step: IN at the open, OUT/SWEEP/EXIT at the close."""

    kind: str
    evidence: str
    note: str | None = None
    counterparty: str | None = None
    # Where the owner's evidence for this step is recorded (never inferred).
    evidence_ref: str | None = None
    # A live sleeve feeds a current outcome: an unresolved step cannot stand.
    live: bool = False


@dataclass(frozen=True)
class AccountDay:
    """The account's own figures for one day, as the provider states them."""

    day: str
    start_nav: Decimal
    end_nav: Decimal
    external_flow: Decimal
    interest: Decimal
    fees: Decimal
    dividend_accrual: Decimal


@dataclass(frozen=True)
class SleeveNavDay:
    day: str
    sleeve_id: str
    event: str
    event_evidence: str
    counterparty: str
    exposure: Decimal
    nav_prev: Decimal
    flow_in: Decimal
    flow_out: Decimal
    value: Decimal
    cash: Decimal
    nav_end: Decimal
    price_pnl: Decimal
    valuation_residual: Decimal
    commission: Decimal
    dividend: Decimal
    own_pnl: Decimal
    base: Decimal
    r_own: Decimal
    net_trade: Decimal
    gross_traded: Decimal
    exposure_share: Decimal
    alloc_interest: Decimal
    alloc_fees: Decimal
    alloc_dividend_accrual: Decimal
    alloc_total: Decimal
    r_alloc: Decimal


@dataclass(frozen=True)
class ClosureDay:
    day: str
    account_pnl: Decimal
    sleeves_own_plus_alloc: Decimal
    residual_and_post_exit: Decimal
    unexplained: Decimal
    # Cash that moved between sleeves and the account's unallocated cash.
    unallocated_cash_flow: Decimal


@dataclass(frozen=True)
class SleeveNav:
    rows: tuple[SleeveNavDay, ...]
    closure: tuple[ClosureDay, ...]


@dataclass(frozen=True)
class SleeveMetrics:
    sleeve_id: str
    start: str
    end: str
    days: int
    start_nav: Decimal
    flow_in: Decimal
    flow_out: Decimal
    end_nav: Decimal
    price_pnl: Decimal
    commission: Decimal
    dividend: Decimal
    own_pnl: Decimal
    alloc_total: Decimal
    identity_check: Decimal
    twr_own: Decimal
    twr_after_alloc: Decimal
    mwr: Decimal | None
    unresolved_events: int


def _validate_events(
    funding: Mapping[str, Mapping[str, FundingEvent]],
    real: Sequence[str],
    *,
    account_label: str,
    window: str,
) -> None:
    for sleeve, events in funding.items():
        if sleeve not in real:
            raise SleeveNavError(f"funding names an unknown sleeve: {sleeve}")
        if not events:
            raise SleeveNavError(f"sleeve {sleeve} has no funding events")
        for day, event in events.items():
            if event.kind not in KINDS or event.evidence not in EVIDENCE:
                raise SleeveNavError(f"invalid funding event: {sleeve} {day}")
            if event.counterparty is not None and event.counterparty not in (
                COUNTERPARTIES
            ):
                raise SleeveNavError(f"unknown counterparty: {sleeve} {day}")
            if event.live and event.evidence == "DECLARED_UNRESOLVED":
                raise SleeveNavError(
                    f"live sleeve {sleeve} has an unresolved funding step on {day}; "
                    "resolve it from the IBKR Flex cash transactions and trades "
                    f"for account {account_label}, window {window}"
                )
        opening = events[min(events)]
        if opening.kind != "IN":
            raise SleeveNavError(f"sleeve {sleeve} does not open with an IN event")


def derive_sleeve_nav(
    ledger: SleeveLedger,
    funding: Mapping[str, Mapping[str, FundingEvent]],
    account: Sequence[AccountDay],
    days: Sequence[str],
    real_sleeves: Sequence[str],
    residual_id: str,
    *,
    account_label: str = "the parent account",
) -> SleeveNav:
    """Each sleeve's NAV path, allocated costs and the closure to the account.

    ``days`` are the days the account reports a NAV for (a subset of the
    ledger's days is allowed, as for a market holiday); ``funding`` declares
    each sleeve's funding events by day.
    """

    _validate_events(
        funding,
        real_sleeves,
        account_label=account_label,
        window=f"{min(days)}..{max(days)}" if days else "(no NAV days)",
    )
    by_key: dict[tuple[str, str], SleeveDay] = {
        (row.day, row.sleeve_id): row for row in ledger.rows
    }
    selected = set(days)
    if days:
        low, high = min(days), max(days)
        for ledger_row in ledger.rows:
            if low <= ledger_row.day <= high and ledger_row.day not in selected:
                if (
                    ledger_row.price_pnl
                    or ledger_row.net_trade
                    or ledger_row.commission
                    or ledger_row.dividend
                    or ledger_row.value != ledger_row.prev_value
                ):
                    raise SleeveNavError(
                        f"ledger day {ledger_row.day} carries activity but is not a NAV day"
                    )
    for declared_sleeve, declared in funding.items():
        if declared and min(declared) not in selected:
            raise SleeveNavError(f"sleeve {declared_sleeve} opens outside the NAV days")
    account_by_day = {item.day: item for item in account}
    for day in days:
        if day not in account_by_day:
            raise SleeveNavError(f"no account figures for {day}")
        for sleeve in (*real_sleeves, residual_id):
            if (day, sleeve) not in by_key:
                raise SleeveNavError(f"no ledger row for {sleeve} on {day}")

    daily: dict[str, list[SleeveNavDay]] = {}
    for sleeve in real_sleeves:
        events = funding.get(sleeve)
        if not events:
            continue
        first = min(events)
        nav_previous = _ZERO
        cash = _ZERO
        ended = False
        rows: list[SleeveNavDay] = []
        for day in days:
            if day < first or ended:
                continue
            item = by_key[(day, sleeve)]
            residual_value = (
                item.value - item.prev_value - item.price_pnl - item.net_trade
            )
            price_pnl = item.price_pnl + residual_value
            event = events.get(day)
            kind = event.kind if event else ""
            flow_in = item.net_trade if kind == "IN" else _ZERO
            # Gross: a short sleeve carries exposure too, never a negative share.
            exposure = abs(item.prev_value) + max(item.net_trade, _ZERO)
            base = nav_previous + flow_in
            own_pnl = price_pnl + item.commission + item.dividend
            nav_pre = nav_previous + flow_in + own_pnl
            flow_out = _ZERO
            if kind == "OUT":
                flow_out = item.net_trade
            elif kind == "EXIT":
                flow_out = -nav_pre
            elif kind == "SWEEP":
                flow_out = item.value - nav_pre
            cash = (
                cash
                - item.net_trade
                + item.commission
                + item.dividend
                + (flow_in if kind == "IN" else _ZERO)
                + (flow_out if kind == "OUT" else _ZERO)
            )
            if kind in ("EXIT", "SWEEP"):
                cash = _ZERO
            nav_end = nav_pre + flow_out
            value = item.value
            if kind == "EXIT":
                nav_end = _ZERO
                value = _ZERO
            rows.append(
                SleeveNavDay(
                    day=day,
                    sleeve_id=sleeve,
                    event=kind,
                    event_evidence=event.evidence if event else "",
                    counterparty=(event.counterparty or "") if event else "",
                    exposure=exposure,
                    nav_prev=nav_previous,
                    flow_in=flow_in,
                    flow_out=flow_out,
                    value=value,
                    cash=cash,
                    nav_end=nav_end,
                    price_pnl=price_pnl,
                    valuation_residual=residual_value,
                    commission=item.commission,
                    dividend=item.dividend,
                    own_pnl=own_pnl,
                    base=base,
                    r_own=own_pnl / base if base > _NAV_FLOOR else _ZERO,
                    net_trade=item.net_trade,
                    gross_traded=item.gross_traded,
                    exposure_share=_ZERO,
                    alloc_interest=_ZERO,
                    alloc_fees=_ZERO,
                    alloc_dividend_accrual=_ZERO,
                    alloc_total=_ZERO,
                    r_alloc=_ZERO,
                )
            )
            nav_previous = nav_end
            if kind == "EXIT":
                ended = True
        daily[sleeve] = rows

    exposure_total = {day: _ZERO for day in days}
    for rows in daily.values():
        for row in rows:
            exposure_total[row.day] += row.exposure

    out: list[SleeveNavDay] = []
    for sleeve in real_sleeves:
        for row in daily.get(sleeve, ()):
            figures = account_by_day[row.day]
            share = (
                row.exposure / exposure_total[row.day]
                if exposure_total[row.day] > 0
                else _ZERO
            )
            alloc_interest = figures.interest * share
            alloc_fees = figures.fees * share
            alloc_divacc = figures.dividend_accrual * share
            alloc_total = alloc_interest + alloc_fees + alloc_divacc
            out.append(
                replace(
                    row,
                    exposure_share=share,
                    alloc_interest=alloc_interest,
                    alloc_fees=alloc_fees,
                    alloc_dividend_accrual=alloc_divacc,
                    alloc_total=alloc_total,
                    r_alloc=(
                        (row.own_pnl + alloc_total) / row.base
                        if row.base > _NAV_FLOOR
                        else _ZERO
                    ),
                )
            )

    closure: list[ClosureDay] = []
    own_alloc = {day: _ZERO for day in days}
    for nav_row in out:
        own_alloc[nav_row.day] += nav_row.own_pnl + nav_row.alloc_total
    on_nav = {(nav_row.day, nav_row.sleeve_id) for nav_row in out}
    for day in days:
        figures = account_by_day[day]
        account_pnl = figures.end_nav - figures.start_nav - figures.external_flow
        other = _ZERO
        # The residual sleeve, post-exit ghosts of the real sleeves and the
        # dividends no sleeve held are not sleeve P&L but are part of the account.
        residual_row = by_key[(day, residual_id)]
        other += (
            residual_row.price_pnl
            + (
                residual_row.value
                - residual_row.prev_value
                - residual_row.price_pnl
                - residual_row.net_trade
            )
            + residual_row.commission
            + residual_row.dividend
        )
        for sleeve in real_sleeves:
            if (day, sleeve) in on_nav:
                continue
            ghost = by_key[(day, sleeve)]
            other += (
                ghost.price_pnl
                + (ghost.value - ghost.prev_value - ghost.price_pnl - ghost.net_trade)
                + ghost.commission
                + ghost.dividend
            )
        other += ledger.unallocated_dividends.get(day, _ZERO)
        closure.append(
            ClosureDay(
                day=day,
                account_pnl=account_pnl,
                sleeves_own_plus_alloc=own_alloc[day],
                residual_and_post_exit=other,
                unexplained=account_pnl - own_alloc[day] - other,
                unallocated_cash_flow=sum(
                    (
                        -nav_row.flow_in - nav_row.flow_out
                        for nav_row in out
                        if nav_row.day == day
                        and nav_row.counterparty == UNALLOCATED_ACCOUNT_CASH
                    ),
                    _ZERO,
                ),
            )
        )
    return SleeveNav(rows=tuple(out), closure=tuple(closure))


def closure_within(
    nav: SleeveNav, first_day: str, last_day: str, tolerance: Decimal
) -> tuple[Decimal, bool]:
    """The summed unexplained account P&L over a window and whether it is within tolerance."""

    total = sum(
        (item.unexplained for item in nav.closure if first_day <= item.day <= last_day),
        _ZERO,
    )
    return total, abs(total) <= tolerance


def split_epochs(rows: Sequence[SleeveNavDay]) -> list[list[SleeveNavDay]]:
    """An inflow after the first day starts an epoch; an outflow or sweep ends one."""

    epochs: list[list[SleeveNavDay]] = []
    current: list[SleeveNavDay] = []
    for row in rows:
        if row.event == "IN" and current and row.day != current[0].day:
            epochs.append(current)
            current = []
        current.append(row)
        if row.event in ("OUT", "EXIT", "SWEEP"):
            epochs.append(current)
            current = []
    if current:
        epochs.append(current)
    return epochs


def _chain(values: Sequence[Decimal]) -> Decimal:
    product = _ONE
    for value in values:
        product *= _ONE + value
    return product - _ONE


def _money_weighted(rows: Sequence[SleeveNavDay], start_nav: Decimal) -> Decimal | None:
    """The daily internal rate that equates the flows and the ending NAV, compounded."""

    periods = len(rows)
    flows: list[tuple[int, Decimal]] = []
    for index, row in enumerate(rows, 1):
        if row.flow_in:
            flows.append((index - 1, row.flow_in))
        if row.flow_out:
            flows.append((index, row.flow_out))
    ending = rows[-1].nav_end

    def residual(rate: Decimal) -> Decimal:
        growth = _ONE + rate
        return (
            start_nav * growth**periods
            + sum((amount * growth ** (periods - at) for at, amount in flows), _ZERO)
            - ending
        )

    low, high = Decimal("-0.5"), Decimal("0.5")
    f_low, f_high = residual(low), residual(high)
    if f_low * f_high > 0:
        return None
    for _ in range(120):
        mid = (low + high) / 2
        f_mid = residual(mid)
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (_ONE + (low + high) / 2) ** periods - _ONE


def sleeve_metrics(rows: Sequence[SleeveNavDay]) -> SleeveMetrics:
    """Return, flows and P&L over a run of one sleeve's days."""

    if not rows:
        raise SleeveNavError("no rows")
    start_nav = rows[0].nav_prev
    flow_in = sum((row.flow_in for row in rows), _ZERO)
    flow_out = sum((row.flow_out for row in rows), _ZERO)
    price = sum((row.price_pnl for row in rows), _ZERO)
    commission = sum((row.commission for row in rows), _ZERO)
    dividend = sum((row.dividend for row in rows), _ZERO)
    own = price + commission + dividend
    return SleeveMetrics(
        sleeve_id=rows[0].sleeve_id,
        start=rows[0].day,
        end=rows[-1].day,
        days=len(rows),
        start_nav=start_nav,
        flow_in=flow_in,
        flow_out=flow_out,
        end_nav=rows[-1].nav_end,
        price_pnl=price,
        commission=commission,
        dividend=dividend,
        own_pnl=own,
        alloc_total=sum((row.alloc_total for row in rows), _ZERO),
        identity_check=start_nav + flow_in + flow_out + own - rows[-1].nav_end,
        twr_own=_chain([row.r_own for row in rows]),
        twr_after_alloc=_chain([row.r_alloc for row in rows]),
        mwr=_money_weighted(rows, start_nav),
        unresolved_events=sum(
            1 for row in rows if row.event_evidence == "DECLARED_UNRESOLVED"
        ),
    )

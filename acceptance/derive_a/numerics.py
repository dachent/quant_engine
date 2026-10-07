"""Exact decimal return arithmetic: sub-period TWR, chain-linking, and XIRR.

Conventions (see ``contracts/portfolio-performance-policy-v1.json``):

* ``twr_cases[date_only_end_of_day]``: a sub-period that contains a date-only
  external flow returns ``(ending - net_flow) / beginning - 1`` (the flow enters
  at end of day, after that day's return). Such flows are timed ``EOD``.
* ``twr_cases[exact_timestamp_segments]``: segment returns chain-link as
  ``prod(1 + r_i) - 1``; a flow whose exact timestamp falls after one value
  observation and before the next starts the next segment, so it is added to
  that sub-period's beginning value. Such flows are timed ``BOD``.
* ``composite_twr_cases[beginning_capital_weighted]``: a composite sub-period
  return is the beginning-capital-weighted mean of member returns, which equals
  the sub-period return of the summed member values and flows.
* ``mwr_cases``: XIRR on investor cash flows (paid in negative) with
  ``day_count = ACTUAL_365``; no root -> ``NO_ROOT``; a root that cannot be
  proved unique -> ``UNIQUENESS_NOT_PROVED``.
* Orchestrator ruling R3: a sub-period with zero beginning and zero ending value
  (net of end-of-day flows) is neutral (return 0). A sub-period cannot start at
  zero capital without a same-day inflow embedded in its opening (a beginning-of-
  period flow): zero capital with a non-zero result, or negative capital, is
  undefined and the TWR fails closed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

PRECISION = 60
ZERO = Decimal(0)
ONE = Decimal(1)
DAYS_PER_YEAR = Decimal(365)

STATUS_UNIQUE = "UNIQUE"
STATUS_NO_ROOT = "NO_ROOT"
STATUS_NOT_PROVED = "UNIQUENESS_NOT_PROVED"
STATUS_INSUFFICIENT = "INSUFFICIENT_CASH_FLOWS"

EOD = "EOD"
BOD = "BOD"


class UndefinedReturnError(ValueError):
    """A sub-period return is undefined (no positive beginning capital)."""


class CalendarMisalignmentError(ValueError):
    """A member lacks an observation on a composite grid date (no filling)."""


@dataclass(frozen=True)
class SubPeriod:
    start: dt.date
    end: dt.date
    beginning_value: Decimal
    ending_value: Decimal
    net_flow: Decimal
    beginning_flow: Decimal = ZERO


@dataclass(frozen=True)
class Member:
    """One contiguous membership segment of an account in an entity.

    ``values`` holds the member's observations; the segment spans
    ``[open_date, close_date]``. ``flows`` are ``(date, amount, timing)``: an
    ``EOD`` flow is contained in the first observation on or after its date
    (``open_date < date <= close_date``); a ``BOD`` flow occurs after the
    observation on or before its date and is invested for the next sub-period
    (``open_date <= date < close_date``).
    """

    key: str
    values: Mapping[dt.date, Decimal]
    open_date: dt.date
    close_date: dt.date
    flows: Sequence[tuple[dt.date, Decimal, str]]


@dataclass(frozen=True)
class IrrResult:
    status: str
    rate: Decimal | None
    root_bound: int
    cash_flow_count: int


def subperiod_return(
    beginning: Decimal,
    ending: Decimal,
    net_flow: Decimal,
    beginning_flow: Decimal = ZERO,
) -> Decimal:
    """``(ending - EOD flows) / (beginning + BOD flows) - 1`` (PORT-PERF-1).

    Ruling R3: zero capital with a zero result is neutral; zero capital with a
    non-zero result (no same-day inflow) or negative capital is undefined.
    """

    with localcontext() as context:
        context.prec = PRECISION
        result = ending - net_flow
        capital = beginning + beginning_flow
        if capital == ZERO and result == ZERO:
            return ZERO
        if capital <= ZERO:
            raise UndefinedReturnError(
                f"beginning capital {capital} with ending-less-flow {result}"
            )
        return result / capital - ONE


def chain_link(returns: Iterable[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = PRECISION
        growth = ONE
        for value in returns:
            growth *= ONE + value
        return growth - ONE


def composite_subperiods(members: Sequence[Member]) -> list[SubPeriod]:
    """Sub-periods of the summed member values over the union date grid.

    A member is active in ``(previous, current]`` when
    ``open_date <= previous`` and ``close_date >= current``. Joining at
    ``open_date`` and leaving after ``close_date`` are capital transfers at
    those end-of-day points, so they never enter a sub-period return.
    """

    grid: set[dt.date] = set()
    for member in members:
        grid.update(
            day for day in member.values if member.open_date <= day <= member.close_date
        )
    ordered = sorted(grid)
    periods: list[SubPeriod] = []
    with localcontext() as context:
        context.prec = PRECISION
        for previous, current in zip(ordered, ordered[1:]):
            active = [
                member
                for member in members
                if member.open_date <= previous and member.close_date >= current
            ]
            if not active:
                continue
            beginning = ZERO
            ending = ZERO
            flow = ZERO
            beginning_flow = ZERO
            for member in active:
                for day in (previous, current):
                    if day not in member.values:
                        raise CalendarMisalignmentError(
                            f"{member.key} has no observation on {day.isoformat()}"
                        )
                beginning += member.values[previous]
                ending += member.values[current]
                for day, amount, timing in member.flows:
                    if timing == BOD and previous <= day < current:
                        beginning_flow += amount
                    elif timing != BOD and previous < day <= current:
                        flow += amount
            periods.append(
                SubPeriod(previous, current, beginning, ending, flow, beginning_flow)
            )
    return periods


def time_weighted_return(periods: Sequence[SubPeriod]) -> Decimal:
    return chain_link(
        subperiod_return(
            period.beginning_value,
            period.ending_value,
            period.net_flow,
            period.beginning_flow,
        )
        for period in periods
    )


def investor_cash_flows(members: Sequence[Member]) -> list[tuple[dt.date, Decimal]]:
    """Investor-perspective cash flows (paid in negative) for XIRR."""

    flows: list[tuple[dt.date, Decimal]] = []
    for member in members:
        # copy_negate, not unary minus: minus rounds a wide amount to the caller's
        # context, and this oracle's arithmetic is independent of that context.
        flows.append((member.open_date, member.values[member.open_date].copy_negate()))
        flows.extend((day, amount.copy_negate()) for day, amount, _ in member.flows)
        flows.append((member.close_date, member.values[member.close_date]))
    return flows


def net_by_date(
    flows: Iterable[tuple[dt.date, Decimal]],
) -> list[tuple[dt.date, Decimal]]:
    totals: dict[dt.date, Decimal] = {}
    with localcontext() as context:
        context.prec = PRECISION
        for day, amount in flows:
            totals[day] = totals.get(day, ZERO) + amount
    return [(day, totals[day]) for day in sorted(totals) if totals[day] != ZERO]


def sign_changes(values: Iterable[Decimal]) -> int:
    changes = 0
    previous = 0
    for value in values:
        sign = (value > ZERO) - (value < ZERO)
        if sign == 0:
            continue
        if previous and sign != previous:
            changes += 1
        previous = sign
    return changes


def _partial_sums(values: Sequence[Decimal]) -> list[Decimal]:
    sums: list[Decimal] = []
    running = ZERO
    with localcontext() as context:
        context.prec = PRECISION
        for value in values:
            running += value
            sums.append(running)
    return sums


def irr_root_bound(amounts: Sequence[Decimal]) -> int:
    """Upper bound on the number of IRRs in ``(-1, inf)``.

    Laguerre's rule (valid for real exponents through the Laplace-transform
    representation) bounds roots with ``r > 0`` by the sign changes of the
    chronological partial sums and roots with ``-1 < r < 0`` by those of the
    reverse-chronological partial sums; ``r = 0`` is a root iff the total is 0.
    """

    forward = sign_changes(_partial_sums(amounts))
    backward = sign_changes(_partial_sums(list(reversed(amounts))))
    with localcontext() as context:
        context.prec = PRECISION
        total_is_zero = 1 if sum(amounts, ZERO) == ZERO else 0
    return forward + backward + total_is_zero


def _sign(value: Decimal) -> int:
    return (value > ZERO) - (value < ZERO)


def xirr(
    flows: Iterable[tuple[dt.date, Decimal]],
    *,
    max_bisections: int = 400,
) -> IrrResult:
    """ACTUAL_365 XIRR with a proof of uniqueness or an explicit refusal."""

    netted = net_by_date(flows)
    if len(netted) < 2:
        return IrrResult(STATUS_INSUFFICIENT, None, 0, len(netted))
    amounts = [amount for _, amount in netted]
    bound = irr_root_bound(amounts)
    first_sign = _sign(amounts[0])
    last_sign = _sign(amounts[-1])
    if bound == 0 or (bound == 1 and first_sign == last_sign):
        return IrrResult(STATUS_NO_ROOT, None, bound, len(netted))
    if bound > 1:
        return IrrResult(STATUS_NOT_PROVED, None, bound, len(netted))
    origin = netted[0][0]
    with localcontext() as context:
        context.prec = PRECISION
        points = [
            (Decimal((day - origin).days) / DAYS_PER_YEAR, amount)
            for day, amount in netted
        ]

        def npv(rate: Decimal) -> Decimal:
            log_growth = (ONE + rate).ln()
            return sum(
                (amount * (-(years * log_growth)).exp() for years, amount in points),
                ZERO,
            )

        # Unique root r*: npv has last_sign on (-1, r*) and first_sign on (r*, inf).
        low = Decimal("-0.5")
        high = Decimal(1)
        low_value = npv(low)
        high_value = npv(high)
        for _ in range(200):
            if low_value == ZERO:
                return IrrResult(STATUS_UNIQUE, low, bound, len(netted))
            if high_value == ZERO:
                return IrrResult(STATUS_UNIQUE, high, bound, len(netted))
            if _sign(low_value) != _sign(high_value):
                break
            if _sign(high_value) == last_sign:
                low, low_value = high, high_value
                high = high * 16 + ONE
                high_value = npv(high)
            else:
                high, high_value = low, low_value
                low = -ONE + (ONE + low) / 16
                low_value = npv(low)
        else:
            return IrrResult(STATUS_NOT_PROVED, None, bound, len(netted))
        for _ in range(max_bisections):
            middle = (low + high) / 2
            middle_value = npv(middle)
            if middle_value == ZERO:
                return IrrResult(STATUS_UNIQUE, middle, bound, len(netted))
            if _sign(middle_value) == _sign(low_value):
                low, low_value = middle, middle_value
            else:
                high, high_value = middle, middle_value
        return IrrResult(STATUS_UNIQUE, (low + high) / 2, bound, len(netted))

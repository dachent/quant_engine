"""One return engine for account and strategy performance (PORT-PERF-1).

Every performance path (the account-strategy manifest epoch engine, the
provider-ledger daily series, the report, attribution and correlation) is meant
to call these pure functions instead of carrying its own linking rule.  The
module holds no I/O and no provider knowledge; callers translate their records
into ``FlowLeg`` values and observation mappings.

Conventions (``contracts/portfolio-performance-policy-v1.json`` vectors and the
corrective-program acceptance conventions R1-R4):

* R1 opening point: the last observation strictly before the funded start ``S``
  when its value is positive, otherwise the first positive observation on or
  after ``S``.  The opening value is that observation's value; flows recognized
  on or before the opening point are embedded in it.
* Flow timing: an ``EXACT_TIMESTAMP`` leg is added to the beginning capital of
  the sub-period that ends at its NAV recognition date; a ``DATE_ONLY`` leg is
  removed from the ending value of that sub-period (end of day, flagged
  ``INFERRED_TIMING``).  Per sub-period
  ``factor = (V_end - F_eod) / (V_begin + F_bod)`` and ``r = factor - 1``.
* R3 zero capital: a sub-period with zero beginning capital and zero ending
  value net of end-of-day flows is neutral; zero capital with any other result
  is undefined and the epoch is unavailable.
* R4 MWR dating: ``DATE_ONLY`` legs at their NAV recognition date,
  ``EXACT_TIMESTAMP`` legs at their economic fill date (fail closed without it).

Decimal precision: linking, compounding, composites and span splicing run in
the caller's active decimal context, so each producer keeps the precision its
published digits are bound to (the ledger's 28-digit daily path, the manifest's
40-digit epoch context).  ``xirr``/``classify_xirr`` run in an isolated 40-digit
context and ``cagr`` in a 34-digit local context, exactly as the code they were
moved from.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Context, Decimal, getcontext, localcontext
from functools import wraps
from typing import ParamSpec, TypeVar


EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
DATE_ONLY = "DATE_ONLY"
TIMING_CLASSES = frozenset({EXACT_TIMESTAMP, DATE_ONLY})
FLOW_CLASSES = frozenset({"EXTERNAL_IN", "EXTERNAL_OUT", "INTERNAL_IN", "INTERNAL_OUT"})
INFERRED_TIMING = "INFERRED_TIMING"

OPENING_LAST_OBSERVATION_BEFORE_START = "LAST_OBSERVATION_BEFORE_START"
OPENING_FIRST_POSITIVE_ON_OR_AFTER_START = (
    "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
)
_OPENING_BASES = frozenset(
    {OPENING_LAST_OBSERVATION_BEFORE_START, OPENING_FIRST_POSITIVE_ON_OR_AFTER_START}
)

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
NO_SUBPERIOD_AFTER_OPENING = "NO_SUBPERIOD_AFTER_OPENING"
FLOW_AFTER_CLOSING_OBSERVATION = "FLOW_AFTER_CLOSING_OBSERVATION"
NEGATIVE_CAPITAL_BASE = "NEGATIVE_CAPITAL_BASE"
ZERO_CAPITAL_RETURN_UNDEFINED = "ZERO_CAPITAL_RETURN_UNDEFINED"
NONPOSITIVE_LINK_FACTOR = "NONPOSITIVE_LINK_FACTOR"

_XIRR_CONTEXT = Context(prec=40)
_P = ParamSpec("_P")
_R = TypeVar("_R")


class ReturnsError(ValueError):
    """Inputs violate the return-engine contract (fail closed)."""


def _isolated_decimal_context(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run numeric work deterministically without mutating caller process state."""

    @wraps(function)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with localcontext(_XIRR_CONTEXT):
            return function(*args, **kwargs)

    return run


def _require_date(value: object, field: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ReturnsError(f"{field} must be a date")
    return value


def _require_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ReturnsError(f"{field} must be a finite Decimal")
    return value


def _checked_observations(observations: Mapping[date, Decimal]) -> None:
    for day, value in observations.items():
        _require_date(day, "observation date")
        _require_decimal(value, "observation value")


@dataclass(frozen=True)
class FlowLeg:
    """One capital movement into (+) or out of (-) the measured entity."""

    amount: Decimal
    flow_class: str
    event_date: date
    nav_recognition_date: date
    timing: str
    economic_date: date | None = None
    source_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        amount = _require_decimal(self.amount, "flow amount")
        if amount == 0:
            raise ReturnsError("flow amount must be non-zero")
        if self.flow_class not in FLOW_CLASSES:
            raise ReturnsError("flow class is invalid")
        if (amount > 0) != self.flow_class.endswith("_IN"):
            raise ReturnsError("flow amount sign disagrees with its class")
        if self.timing not in TIMING_CLASSES:
            raise ReturnsError("flow timing class is invalid")
        event_day = _require_date(self.event_date, "flow event_date")
        recognition_day = _require_date(
            self.nav_recognition_date, "flow nav_recognition_date"
        )
        if recognition_day < event_day:
            raise ReturnsError("flow recognition precedes its economic event")
        if self.economic_date is not None:
            economic_day = _require_date(self.economic_date, "flow economic_date")
            if self.timing != EXACT_TIMESTAMP:
                raise ReturnsError("only exact-timestamp legs carry an economic date")
            if economic_day > recognition_day:
                raise ReturnsError("flow economic date follows its NAV recognition")
        if not isinstance(self.source_record_ids, tuple) or not all(
            isinstance(item, str) and item for item in self.source_record_ids
        ):
            raise ReturnsError("flow source_record_ids must be non-empty strings")


@dataclass(frozen=True)
class OpeningPoint:
    """The R1 performance opening: one observation and its value."""

    as_of: date
    value: Decimal
    basis: str

    def __post_init__(self) -> None:
        _require_date(self.as_of, "opening date")
        if _require_decimal(self.value, "opening value") <= 0:
            raise ReturnsError("opening value must be positive")
        if self.basis not in _OPENING_BASES:
            raise ReturnsError("opening basis is invalid")


def opening_point(
    observations: Mapping[date, Decimal], start: date
) -> OpeningPoint | None:
    """Select the R1 opening observation for an entity funded from ``start``.

    Capital enters at the beginning of day ``start``, so the last observation
    before it is the opening when positive; otherwise the first positive
    observation on or after ``start`` opens the epoch.  Earlier observations
    never open it.  ``None`` means no positive opening exists.
    """

    _require_date(start, "funded start")
    _checked_observations(observations)
    before = [day for day in observations if day < start]
    if before:
        last = max(before)
        if observations[last] > 0:
            return OpeningPoint(
                last, observations[last], OPENING_LAST_OBSERVATION_BEFORE_START
            )
    for day in sorted(day for day in observations if day >= start):
        if observations[day] > 0:
            return OpeningPoint(
                day, observations[day], OPENING_FIRST_POSITIVE_ON_OR_AFTER_START
            )
    return None


@dataclass(frozen=True)
class SubperiodLink:
    """One linked sub-period between adjacent observations."""

    start_date: date
    end_date: date
    beginning_value: Decimal
    beginning_flow: Decimal
    ending_value: Decimal
    ending_flow: Decimal
    beginning_capital: Decimal
    factor: Decimal
    cumulative_index: Decimal
    running_hwm_index: Decimal
    drawdown: Decimal
    zero_capital_neutral: bool

    @property
    def period_return(self) -> Decimal:
        return self.factor - Decimal(1)


@dataclass(frozen=True)
class EpochLink:
    """Result of linking one epoch; numerics are ``None`` when unavailable.

    ``cumulative_index`` starts at 1 (the manifest's 100-based index has the same
    significant digits); ``drawdown`` values are non-negative magnitudes
    ``1 - index / running_hwm``.
    """

    status: str
    reason: str | None
    opening: OpeningPoint
    links: tuple[SubperiodLink, ...]
    linked_legs: tuple[FlowLeg, ...]
    embedded_legs: tuple[FlowLeg, ...]
    flags: tuple[str, ...]
    twr: Decimal | None
    hwm_index: Decimal | None
    max_drawdown: Decimal | None
    failed_subperiod: tuple[date, date] | None = None

    @property
    def end_date(self) -> date | None:
        return self.links[-1].end_date if self.links else None


def _unavailable(
    opening: OpeningPoint,
    reason: str,
    failed: tuple[date, date] | None = None,
) -> EpochLink:
    return EpochLink(
        status=UNAVAILABLE,
        reason=reason,
        opening=opening,
        links=(),
        linked_legs=(),
        embedded_legs=(),
        flags=(),
        twr=None,
        hwm_index=None,
        max_drawdown=None,
        failed_subperiod=failed,
    )


def link_epoch(
    observations: Mapping[date, Decimal],
    legs: Sequence[FlowLeg],
    opening: OpeningPoint,
) -> EpochLink:
    """Chain-link every observation after ``opening`` into one epoch TWR.

    Each leg belongs to the sub-period ending at the first observation on or
    after its NAV recognition date.  Legs recognized on or before the opening
    are embedded in the opening value; a leg recognized after the last
    observation makes the epoch unavailable (its money identity would be
    incomplete).  Observations before the opening are ignored.
    """

    _checked_observations(observations)
    if not isinstance(opening, OpeningPoint):
        raise ReturnsError("opening must be an OpeningPoint")
    if observations.get(opening.as_of) != opening.value:
        raise ReturnsError("opening point is not an observation of this series")
    if not all(isinstance(leg, FlowLeg) for leg in legs):
        raise ReturnsError("legs must be FlowLeg values")
    days = sorted(day for day in observations if day > opening.as_of)
    embedded = tuple(leg for leg in legs if leg.nav_recognition_date <= opening.as_of)
    linked = tuple(leg for leg in legs if leg.nav_recognition_date > opening.as_of)
    if not days:
        return _unavailable(opening, NO_SUBPERIOD_AFTER_OPENING)
    if any(leg.nav_recognition_date > days[-1] for leg in linked):
        return _unavailable(opening, FLOW_AFTER_CLOSING_OBSERVATION)

    index = Decimal(1)
    hwm = Decimal(1)
    max_drawdown = Decimal(0)
    links: list[SubperiodLink] = []
    prior_day = opening.as_of
    prior_value = opening.value
    for day in days:
        period_legs = [
            leg for leg in linked if prior_day < leg.nav_recognition_date <= day
        ]
        beginning_legs = [leg for leg in period_legs if leg.timing == EXACT_TIMESTAMP]
        beginning_flow = sum((leg.amount for leg in beginning_legs), Decimal(0))
        ending_flow = sum(
            (leg.amount for leg in period_legs if leg.timing == DATE_ONLY),
            Decimal(0),
        )
        ending_value = observations[day]
        # Adding a zero would only re-express the operand; keep the producer's
        # exact Decimal so a flow-free period reproduces its published digits.
        base = prior_value + beginning_flow if beginning_legs else prior_value
        numerator = ending_value - ending_flow
        neutral = False
        if base < 0:
            return _unavailable(opening, NEGATIVE_CAPITAL_BASE, (prior_day, day))
        if base == 0:
            if numerator != 0:
                return _unavailable(
                    opening, ZERO_CAPITAL_RETURN_UNDEFINED, (prior_day, day)
                )
            neutral = True
            factor = Decimal(1)
        else:
            factor = numerator / base
            if factor <= 0:
                return _unavailable(opening, NONPOSITIVE_LINK_FACTOR, (prior_day, day))
            index *= factor
        hwm = max(hwm, index)
        drawdown = Decimal(1) - index / hwm
        max_drawdown = max(max_drawdown, drawdown)
        links.append(
            SubperiodLink(
                start_date=prior_day,
                end_date=day,
                beginning_value=prior_value,
                beginning_flow=beginning_flow,
                ending_value=ending_value,
                ending_flow=ending_flow,
                beginning_capital=base,
                factor=factor,
                cumulative_index=index,
                running_hwm_index=hwm,
                drawdown=drawdown,
                zero_capital_neutral=neutral,
            )
        )
        prior_day = day
        prior_value = ending_value
    return EpochLink(
        status=AVAILABLE,
        reason=None,
        opening=opening,
        links=tuple(links),
        linked_legs=linked,
        embedded_legs=embedded,
        flags=(
            (INFERRED_TIMING,) if any(leg.timing == DATE_ONLY for leg in linked) else ()
        ),
        twr=index - Decimal(1),
        hwm_index=hwm,
        max_drawdown=max_drawdown,
    )


@dataclass(frozen=True)
class MemberReturn:
    """One member's sub-period return ending on ``end_date``."""

    member_id: str
    end_date: date
    beginning_capital: Decimal
    period_return: Decimal


@dataclass(frozen=True)
class CompositeReturn:
    """A beginning-capital weighted composite sub-period return."""

    end_date: date
    beginning_capital: Decimal
    period_return: Decimal
    member_ids: tuple[str, ...]


def member_returns(member_id: str, epoch: EpochLink) -> tuple[MemberReturn, ...]:
    """Express an available member epoch as composite inputs.

    A member enters with its opening value and leaves after its last
    sub-period; in a summed series those are transfers at the membership
    boundaries, so the member simply contributes no weight outside its links.
    Neutral zero-capital links carry no capital and are omitted.
    """

    if not isinstance(member_id, str) or not member_id:
        raise ReturnsError("member_id must be a non-empty string")
    if epoch.status != AVAILABLE:
        raise ReturnsError("an unavailable epoch cannot enter a composite")
    return tuple(
        MemberReturn(
            member_id, link.end_date, link.beginning_capital, link.period_return
        )
        for link in epoch.links
        if not link.zero_capital_neutral
    )


def composite(members: Iterable[MemberReturn]) -> tuple[CompositeReturn, ...]:
    """Beginning-capital weight member returns on the union of their dates.

    With capital ``V_begin + F_bod`` per member this equals linking the summed
    member values and flows when member sub-periods share their dates.  A lone
    member's return is passed through unchanged so no second rounding touches
    it.  Dates without capital at risk are omitted.
    """

    by_day: dict[date, list[MemberReturn]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    for member in members:
        if not isinstance(member, MemberReturn):
            raise ReturnsError("composite members must be MemberReturn values")
        _require_date(member.end_date, "member end_date")
        capital = _require_decimal(member.beginning_capital, "member capital")
        value = _require_decimal(member.period_return, "member return")
        key = (member.member_id, member.end_date)
        if key in seen:
            raise ReturnsError("composite member repeats a date")
        seen.add(key)
        if capital < 0:
            raise ReturnsError("composite member capital is negative")
        if capital == 0 and value != 0:
            raise ReturnsError("composite member return on zero capital")
        if value <= -1:
            raise ReturnsError("composite member return is invalid")
        by_day[member.end_date].append(member)
    result: list[CompositeReturn] = []
    for day in sorted(by_day):
        items = [item for item in by_day[day] if item.beginning_capital > 0]
        if not items:
            continue
        denominator = sum((item.beginning_capital for item in items), Decimal(0))
        value = (
            items[0].period_return
            if len(items) == 1
            else sum(
                (item.beginning_capital * item.period_return for item in items),
                Decimal(0),
            )
            / denominator
        )
        result.append(
            CompositeReturn(
                day,
                denominator,
                value,
                tuple(sorted(item.member_id for item in items)),
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class ReturnSpan:
    """A holding-period return replacing a declared missing daily point."""

    start_date: date
    end_date: date
    period_return: Decimal


@dataclass(frozen=True)
class CompoundedPath:
    """A daily return path compounded with its admitted gap spans.

    Drawdowns are non-negative magnitudes and are ``None`` whenever a span is
    spliced: a missing daily point can hide the peak.
    """

    cumulative_return: Decimal
    daily_cumulative_return: Decimal
    link_count: int
    observed_start: date
    observed_end: date
    maximum_drawdown: Decimal | None
    current_drawdown: Decimal | None

    @property
    def path_dependent_metrics_available(self) -> bool:
        return self.maximum_drawdown is not None


def splice_spans(
    daily: Sequence[tuple[date, Decimal]],
    spans: Sequence[ReturnSpan] = (),
) -> CompoundedPath:
    """Compound a daily path, splicing each span after its start date.

    A span must start on an observed daily date and no daily observation may
    fall inside it, so it replaces the declared gap instead of double counting.
    With no spans this is the plain geometric compound of the daily path.
    """

    if not daily:
        raise ReturnsError("return path is empty")
    days: list[date] = []
    for day, value in daily:
        _require_date(day, "return date")
        if _require_decimal(value, "daily return") <= -1:
            raise ReturnsError("daily return is invalid")
        if days and day <= days[-1]:
            raise ReturnsError("return path dates must strictly increase")
        days.append(day)
    observed = set(days)
    spans_by_start: dict[date, ReturnSpan] = {}
    for span in spans:
        if not isinstance(span, ReturnSpan):
            raise ReturnsError("spans must be ReturnSpan values")
        start = _require_date(span.start_date, "span start_date")
        end = _require_date(span.end_date, "span end_date")
        if _require_decimal(span.period_return, "span return") <= -1:
            raise ReturnsError("span return is invalid")
        if end <= start or start not in observed or start in spans_by_start:
            raise ReturnsError("span does not start on a distinct observed date")
        if any(start < day <= end for day in days):
            raise ReturnsError("span does not replace a gap")
        spans_by_start[start] = span
    wealth = Decimal("1")
    daily_wealth = Decimal("1")
    peak = wealth
    maximum_drawdown = Decimal("0")
    path_dependent = not spans_by_start
    for day, value in daily:
        wealth *= Decimal("1") + value
        daily_wealth *= Decimal("1") + value
        if path_dependent:
            peak = max(peak, wealth)
            maximum_drawdown = max(maximum_drawdown, Decimal("1") - wealth / peak)
        spliced = spans_by_start.get(day)
        if spliced is not None:
            wealth *= Decimal("1") + spliced.period_return
    return CompoundedPath(
        cumulative_return=wealth - Decimal("1"),
        daily_cumulative_return=daily_wealth - Decimal("1"),
        link_count=len(days) + len(spans_by_start),
        observed_start=days[0],
        observed_end=days[-1],
        maximum_drawdown=maximum_drawdown if path_dependent else None,
        current_drawdown=Decimal("1") - wealth / peak if path_dependent else None,
    )


def cagr(cumulative_return: Decimal, start: date, end: date) -> Decimal | None:
    """Annualize a cumulative return on ACTUAL_365 days (34-digit context)."""

    elapsed_days = (_require_date(end, "end") - _require_date(start, "start")).days
    cumulative = _require_decimal(cumulative_return, "cumulative return")
    if elapsed_days <= 0 or cumulative <= Decimal("-1"):
        return None
    with localcontext() as context:
        context.prec = 34
        annualized = context.power(
            Decimal("1") + cumulative, Decimal("365") / Decimal(elapsed_days)
        ) - Decimal("1")
    return annualized


def mwr_cashflows(
    opening: OpeningPoint,
    legs: Sequence[FlowLeg],
    terminal_date: date,
    terminal_value: Decimal,
) -> tuple[tuple[date, Decimal], ...]:
    """Investor cash flows for XIRR, dated per R4 and netted by date.

    The opening value is paid in at the opening point, every leg after it is
    paid in (+ legs) or received (- legs), and the terminal value is received.
    Legs embedded in the opening value are not repeated.
    """

    if not isinstance(opening, OpeningPoint):
        raise ReturnsError("opening must be an OpeningPoint")
    terminal_day = _require_date(terminal_date, "terminal date")
    value = _require_decimal(terminal_value, "terminal value")
    if terminal_day <= opening.as_of:
        raise ReturnsError("terminal date must follow the opening point")
    totals: dict[date, Decimal] = defaultdict(Decimal)
    totals[opening.as_of] += -opening.value
    for leg in legs:
        if not isinstance(leg, FlowLeg):
            raise ReturnsError("legs must be FlowLeg values")
        if leg.nav_recognition_date <= opening.as_of:
            continue
        if leg.nav_recognition_date > terminal_day:
            raise ReturnsError("flow is recognized after the terminal valuation")
        if leg.timing == EXACT_TIMESTAMP:
            if leg.economic_date is None:
                raise ReturnsError("exact-timestamp leg lacks its economic fill date")
            if leg.economic_date < opening.as_of:
                raise ReturnsError("flow economic date precedes the opening point")
            cash_date = leg.economic_date
        else:
            cash_date = leg.nav_recognition_date
        totals[cash_date] += -leg.amount
    totals[terminal_day] += value
    return tuple((day, amount) for day, amount in sorted(totals.items()) if amount)


@_isolated_decimal_context
def xirr(cashflows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Unique-root bisection XIRR; None when uniqueness or convergence fails."""
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for cashflow_date, amount in cashflows:
        by_date[cashflow_date] += amount
    ordered = [(day, amount) for day, amount in sorted(by_date.items()) if amount]
    signs = [amount > 0 for _, amount in ordered]
    sign_changes = sum(left != right for left, right in zip(signs, signs[1:]))
    # More than one chronological sign change can produce multiple valid IRRs.
    # A diagnostic must fail closed rather than select an arbitrary root.
    if len(ordered) < 2 or sign_changes != 1:
        return None
    t0 = ordered[0][0]

    def npv(rate: Decimal) -> Decimal:
        total = Decimal(0)
        for dt, a in ordered:
            total += a / (
                (Decimal(1) + rate) ** (Decimal((dt - t0).days) / Decimal(365))
            )
        return total

    tolerance = max(abs(amount) for _, amount in ordered) * Decimal("1e-28")
    zero = Decimal(0)
    fzero = npv(zero)
    if abs(fzero) <= tolerance:
        return zero

    bracket: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
    right, fright = zero, fzero
    for exponent in range(1, getcontext().prec + 1):
        left = Decimal(-1) + (Decimal(10) ** -exponent)
        if left == right:
            break
        fleft = npv(left)
        if abs(fleft) <= tolerance:
            return left
        if fleft * fright < 0:
            bracket = (left, right, fleft, fright)
            break
        right, fright = left, fleft

    if bracket is None:
        left, fleft = zero, fzero
        for exponent in range(1, getcontext().prec + 1):
            right = (Decimal(10) ** exponent) - Decimal(1)
            fright = npv(right)
            if abs(fright) <= tolerance:
                return right
            if fleft * fright < 0:
                bracket = (left, right, fleft, fright)
                break
            left, fleft = right, fright

    if bracket is None:
        return None
    lo, hi, flo, _fhi = bracket
    for _ in range(400):
        mid = (lo + hi) / 2
        fmid = npv(mid)
        if (fmid < 0) == (flo < 0):
            lo, flo = mid, fmid
        else:
            hi = mid
    rate = (lo + hi) / 2
    if abs(npv(rate)) > tolerance:
        return None
    # Bisection around an exact zero can leave a vanishing numerical residue.
    # Canonicalize that residue so JSON never carries an exponent-form pseudo-rate.
    return Decimal(0) if abs(rate) < Decimal("1e-30") else rate


@_isolated_decimal_context
def classify_xirr(cashflows: Sequence[tuple[date, Decimal]]) -> dict[str, object]:
    """Classify the existing conservative ACTUAL_365 Decimal XIRR result.

    ``xirr`` intentionally returns ``None`` for several fail-closed outcomes.
    Keep that numerical method unchanged and make the admission reason explicit
    for the producer/consumer contract.
    """
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for cashflow_date, amount in cashflows:
        by_date[cashflow_date] += amount
    ordered = [(day, amount) for day, amount in sorted(by_date.items()) if amount]
    if not ordered:
        return {
            "status": "INDETERMINATE",
            "reason": "ALL_CASHFLOWS_ZERO",
            "rate": None,
        }
    signs = [amount > 0 for _, amount in ordered]
    sign_changes = sum(left != right for left, right in zip(signs, signs[1:]))
    if sign_changes == 0:
        return {
            "status": "NO_ROOT",
            "reason": "NO_CHRONOLOGICAL_SIGN_CHANGE",
            "rate": None,
        }
    if sign_changes > 1:
        return {
            "status": "UNIQUENESS_NOT_PROVED",
            "reason": "MULTIPLE_CHRONOLOGICAL_SIGN_CHANGES",
            "rate": None,
        }
    rate = xirr(ordered)
    if rate is None:
        return {
            "status": "NUMERICAL_SEARCH_UNRESOLVED",
            "reason": "UNIQUE_ROOT_SEARCH_UNRESOLVED",
            "rate": None,
        }
    return {"status": "AVAILABLE", "reason": None, "rate": rate}

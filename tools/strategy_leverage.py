"""Levered and unlevered return of a derived sleeve, from the sleeve facts (issue #624).

A sleeve that borrows on margin is carried on a gross basis: its NAV includes assets
bought with the account's shared margin loan.  This module derives two returns for one
sleeve from ``sleeve-account-facts`` alone, with nothing hardcoded and no strategy named:

* unlevered: the return on gross sleeve assets, the daily sleeve P&L (NAV after allocated
  costs, transfers external, end-of-day flows) over the prior-close sleeve NAV.  Chained,
  it is the sleeve's own observation-trace path, and when the trace links are given the
  two are compared (a required closure).
* levered: the return on equity after margin financing, the same P&L (allocated interest
  and fees already inside it) over the prior-close sleeve NAV less the sleeve's loan at
  that close.  Loan changes and transfers are flows, not P&L.

The sleeve loan is the account loan allocated by exposure share.  The account loan is the
debit of the account's unallocated cash-and-margin bucket (``perimeter_account.days``),
the only account balance that sits outside every sleeve NAV: each sleeve NAV already nets
that sleeve's own cash, so the sleeve NAVs plus the bucket are the account NAV.

Every closure fails closed (``SleeveLeverageError`` with a stable reason code); a figure is
never produced from facts that do not close.  ``interest_consistency`` tests the loan
against the interest the broker actually charged.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
import hashlib
import json
from typing import Any, Final

from quant_engine.portfolio_ledger import decimal_text

FACTS_ARTIFACT: Final = "sleeve-account-facts"
LOAN_RULE: Final = "UNALLOCATED_BUCKET_DEBIT"
ALLOCATION_RULE: Final = "EXPOSURE_SHARE_AT_CLOSE"
UNLEVERED_BASIS: Final = "GROSS_SLEEVE_ASSETS_SLEEVE_TWR"
LEVERED_BASIS: Final = "SLEEVE_EQUITY_AFTER_MARGIN_FINANCING"
ANNUALISATION_DAYS: Final = 365
_PRECISION: Final = 60
_SERIES_PLACES: Final = Decimal("0.000000000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
# a share that is computed in the working precision sums to one to far below this
CLOSURE_TOLERANCE: Final = Decimal("1e-20")
# the bucket roll-forward and the flow mirror are sums of 4-decimal ledger amounts
LEDGER_TOLERANCE: Final = Decimal("0.0005")
TRACE_TOLERANCE: Final = Decimal("1e-20")
ACCOUNT_NAV_TOLERANCE: Final = Decimal("0.005")

# interest consistency: stated tolerances
INTEREST_DAY_RELATIVE_TOLERANCE: Final = Decimal("0.05")
INTEREST_DAY_ABSOLUTE_TOLERANCE: Final = Decimal(100)
INTEREST_DAY_SHARE_REQUIRED: Final = Decimal("0.90")
INTEREST_TOTAL_RATIO_BAND: Final = Decimal("0.02")
INTEREST_DAY_COUNT_BASIS: Final = Decimal(360)
INTEREST_ROUNDING_TOLERANCE: Final = Decimal("0.0051")
DEBIT_INTEREST_CLASSIFICATION: Final = "BROKER_DEBIT_INTEREST"


class SleeveLeverageError(ValueError):
    """The facts do not close, or do not carry what the derivation needs."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class SleeveLeverage:
    """One sleeve's unlevered and levered figures, with the series they come from."""

    sleeve_id: str
    opening_date: str
    end_date: str
    window_days: int
    loan_rule: str
    allocation_rule: str
    unlevered_cumulative: Decimal
    unlevered_annualised: Decimal | None
    unlevered_max_drawdown: Decimal
    unlevered_current_drawdown: Decimal
    levered_cumulative: Decimal
    levered_annualised: Decimal | None
    levered_max_drawdown: Decimal
    levered_current_drawdown: Decimal
    peak_gross_to_equity: Decimal
    peak_gross_to_equity_day: str
    current_gross_to_equity: Decimal
    end_nav: Decimal
    end_loan: Decimal
    end_equity: Decimal
    loan_days: int
    max_loan: Decimal
    account_loan_days: int
    account_max_loan: Decimal
    series: tuple[dict[str, str], ...]
    series_sha256: str
    closure: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        """A JSON-ready view: decimals as deterministic text."""

        out: dict[str, Any] = {}
        for name, value in self.__dict__.items():
            if isinstance(value, Decimal):
                out[name] = decimal_text(value)
            elif name == "series":
                out[name] = [dict(row) for row in self.series]
            else:
                out[name] = value
        out["unlevered_basis"] = UNLEVERED_BASIS
        out["levered_basis"] = LEVERED_BASIS
        return out


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - InvalidOperation and TypeError alike
        raise SleeveLeverageError("FACT_NOT_DECIMAL", name) from exc
    if not result.is_finite():
        raise SleeveLeverageError("FACT_NOT_DECIMAL", name)
    return result


@dataclass(frozen=True)
class _Book:
    days: tuple[str, ...]
    bucket_nav: dict[str, Decimal]
    bucket_flow: dict[str, tuple[Decimal, Decimal, Decimal]]
    bucket_opening: Decimal
    nav: dict[str, dict[str, Decimal]]
    flow: dict[str, dict[str, Decimal]]
    exposure: dict[str, dict[str, Decimal]]
    value: dict[str, dict[str, Decimal]]
    first_day: dict[str, str]
    last_day: dict[str, str]
    parent_account_id: str


def _read(facts: Mapping[str, Any]) -> _Book:
    try:
        if facts["artifact_id"] != FACTS_ARTIFACT or facts["schema_version"] != 1:
            raise SleeveLeverageError("FACTS_IDENTITY", "not sleeve-account-facts/1")
        perimeter = facts["perimeter_account"]
        bucket_nav: dict[str, Decimal] = {}
        bucket_flow: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        for row in perimeter["days"]:
            day = str(row["day"])
            if day in bucket_nav:
                raise SleeveLeverageError("DUPLICATE_DAY", f"bucket {day}")
            bucket_nav[day] = _decimal(row["nav_end"], f"bucket nav {day}")
            bucket_flow[day] = (
                _decimal(row["external_flow"], f"bucket external {day}"),
                _decimal(row["internal_flow_net"], f"bucket internal {day}"),
                _decimal(row["residual_pnl"], f"bucket residual {day}"),
            )
        opening = _decimal(perimeter["opening_nav"], "bucket opening nav")
        nav: dict[str, dict[str, Decimal]] = {}
        flow: dict[str, dict[str, Decimal]] = {}
        exposure: dict[str, dict[str, Decimal]] = {}
        value: dict[str, dict[str, Decimal]] = {}
        first: dict[str, str] = {}
        last: dict[str, str] = {}
        for sleeve in facts["sleeves"]:
            sid = str(sleeve["sleeve_id"])
            if sid in nav:
                raise SleeveLeverageError("DUPLICATE_SLEEVE", sid)
            nav[sid], flow[sid], exposure[sid], value[sid] = {}, {}, {}, {}
            for row in sleeve["days"]:
                day = str(row["day"])
                if day in nav[sid]:
                    raise SleeveLeverageError("DUPLICATE_DAY", f"{sid} {day}")
                nav[sid][day] = _decimal(row["nav_end_after_alloc"], f"{sid} nav {day}")
                flow_in = _decimal(row["flow_in"], f"{sid} in {day}")
                flow_out = _decimal(row["flow_out"], f"{sid} out {day}")
                # the facts carry an outflow as a negative amount
                if flow_in < 0 or flow_out > 0:
                    raise SleeveLeverageError(
                        "FLOW_SIGN", f"{sid} {day} in {flow_in} out {flow_out}"
                    )
                flow[sid][day] = flow_in + flow_out
                exposure[sid][day] = _decimal(row["exposure"], f"{sid} exposure {day}")
                value[sid][day] = _decimal(row["value"], f"{sid} value {day}")
                if exposure[sid][day] < 0:
                    raise SleeveLeverageError(
                        "NEGATIVE_EXPOSURE", f"{sid} {day} {exposure[sid][day]}"
                    )
            first[sid] = str(sleeve["first_day"])
            last[sid] = str(sleeve["last_day"])
        parent = str(facts["parent_account_id"])
    except (KeyError, TypeError) as exc:
        raise SleeveLeverageError(
            "FACTS_SHAPE", f"missing or malformed: {exc!r}"
        ) from exc
    if not bucket_nav or not nav:
        raise SleeveLeverageError("FACTS_SHAPE", "no bucket days or no sleeves")
    days = tuple(sorted(bucket_nav))
    for sid, rows in nav.items():
        if not rows:
            raise SleeveLeverageError("FACTS_SHAPE", f"sleeve has no days: {sid}")
        outside = sorted(set(rows) - set(days))
        if outside:
            raise SleeveLeverageError(
                "SLEEVE_DAY_NOT_IN_ACCOUNT", f"{sid} {outside[0]}"
            )
        if first[sid] != min(rows) or last[sid] != max(rows):
            raise SleeveLeverageError(
                "SLEEVE_WINDOW", f"{sid} first/last day differ from its rows"
            )
    return _Book(
        days,
        bucket_nav,
        bucket_flow,
        opening,
        nav,
        flow,
        exposure,
        value,
        first,
        last,
        parent,
    )


def _account_loan(book: _Book, day: str) -> Decimal:
    return max(_ZERO, -book.bucket_nav[day])


def _exposure(book: _Book, sleeve: str, day: str) -> Decimal:
    return book.exposure[sleeve].get(day, _ZERO)


def _nav(book: _Book, sleeve: str, day: str) -> Decimal:
    return book.nav[sleeve].get(day, _ZERO)


def _check_ledger(book: _Book) -> dict[str, Any]:
    """The bucket rolls forward and mirrors the sleeves' transfers, every day."""

    previous = book.bucket_opening
    for day in book.days:
        external, internal, residual = book.bucket_flow[day]
        expected = previous + external + internal + residual
        if abs(book.bucket_nav[day] - expected) > LEDGER_TOLERANCE:
            raise SleeveLeverageError(
                "BUCKET_ROLLFORWARD",
                f"{day} nav {book.bucket_nav[day]} != previous+flows {expected}",
            )
        sleeve_net = sum((book.flow[s].get(day, _ZERO) for s in book.nav), _ZERO)
        if abs(sleeve_net + internal) > LEDGER_TOLERANCE:
            raise SleeveLeverageError(
                "TRANSFER_MIRROR",
                f"{day} sleeves net {sleeve_net} vs bucket internal {internal}",
            )
        previous = book.bucket_nav[day]
    return {"days": len(book.days)}


def _loans(book: _Book) -> dict[str, dict[str, Decimal]]:
    """Per sleeve per account day: the allocated loan, after the closure checks."""

    loans: dict[str, dict[str, Decimal]] = {s: {} for s in book.nav}
    with localcontext() as context:
        context.prec = _PRECISION
        for day in book.days:
            total = _account_loan(book, day)
            exposures = {s: _exposure(book, s, day) for s in book.nav}
            live = sum(exposures.values(), _ZERO)
            if total > 0 and live <= 0:
                raise SleeveLeverageError(
                    "LOAN_WITHOUT_EXPOSURE",
                    f"{day} account loan {total} and no live exposure",
                )
            allocated = _ZERO
            for sleeve, exposure in exposures.items():
                loan = total * exposure / live if live > 0 else _ZERO
                if loan > 0 and exposure <= 0:
                    raise SleeveLeverageError(
                        "LOAN_WITHOUT_EXPOSURE", f"{sleeve} {day}"
                    )
                loans[sleeve][day] = loan
                allocated += loan
            if abs(allocated - total) > CLOSURE_TOLERANCE:
                raise SleeveLeverageError(
                    "LOAN_ALLOCATION_DOES_NOT_CLOSE",
                    f"{day} sleeve loans {allocated} != account loan {total}",
                )
    return loans


def _drawdown(index: Decimal, peak: Decimal) -> Decimal:
    return index / peak - _ONE


def _annualise(index: Decimal, window_days: int, name: str) -> Decimal | None:
    """ACT/365 geometric, from 365 days only: one short-window rule (#904).

    Under a year the window return is the figure; an annual rate extrapolated from
    it is not published (``None``), as ``twr_annualised`` is not.
    """

    if index <= 0 or window_days <= 0:
        raise SleeveLeverageError("RETURN_NOT_ANNUALISABLE", f"{name} index {index}")
    if window_days < ANNUALISATION_DAYS:
        return None
    years = Decimal(window_days) / Decimal(ANNUALISATION_DAYS)
    return (index.ln() / years).exp() - _ONE


def derive_sleeve_leverage(
    facts: Mapping[str, Any],
    sleeve_id: str,
    *,
    ledger_account_nav: Decimal | str,
    links: Sequence[Mapping[str, Any]] | None = None,
    account_nav_tolerance: Decimal = ACCOUNT_NAV_TOLERANCE,
) -> SleeveLeverage:
    """Unlevered and levered figures for one sleeve; fails closed on any open closure.

    ``ledger_account_nav`` is the ledger's NAV of the parent account at the facts' last day
    (the figure the account basis publishes); the facts alone cannot supply it.  ``links``
    are the sleeve epoch's observation-trace links; when given, the unlevered path must
    equal them to ``TRACE_TOLERANCE``.
    """

    book = _read(facts)
    if sleeve_id not in book.nav:
        raise SleeveLeverageError("UNKNOWN_SLEEVE", sleeve_id)
    ledger_nav = _decimal(ledger_account_nav, "ledger account nav")
    with localcontext() as context:
        context.prec = _PRECISION
        context.rounding = ROUND_HALF_EVEN
        closure: dict[str, Any] = {}
        closure["ledger"] = _check_ledger(book)
        loans = _loans(book)
        closure["loan_allocation"] = {
            "days": len(book.days),
            "rule": ALLOCATION_RULE,
            "max_loan_account": decimal_text(
                max(_account_loan(book, d) for d in book.days)
            ),
        }

        cutoff = book.days[-1]
        nav_sum = sum((_nav(book, s, cutoff) for s in book.nav), _ZERO)
        identity = nav_sum + book.bucket_nav[cutoff]
        if abs(identity - ledger_nav) > account_nav_tolerance:
            raise SleeveLeverageError(
                "ACCOUNT_NAV_IDENTITY",
                f"{cutoff} sleeve NAVs {nav_sum} + bucket {book.bucket_nav[cutoff]} "
                f"= {identity} != ledger account NAV {ledger_nav}",
            )
        equity_sum = sum(
            (_nav(book, s, cutoff) - loans[s][cutoff] for s in book.nav), _ZERO
        )
        positive_cash = max(_ZERO, book.bucket_nav[cutoff])
        if abs(equity_sum + positive_cash - ledger_nav) > account_nav_tolerance:
            raise SleeveLeverageError(
                "ACCOUNT_EQUITY_IDENTITY",
                f"{cutoff} sleeve equities {equity_sum} + bucket cash {positive_cash} "
                f"!= ledger account NAV {ledger_nav}",
            )
        closure["account_nav"] = {
            "day": cutoff,
            "sleeve_navs": decimal_text(nav_sum),
            "bucket": decimal_text(book.bucket_nav[cutoff]),
            "sleeve_equities": decimal_text(equity_sum),
            "positive_bucket_cash": decimal_text(positive_cash),
            "ledger_account_nav": decimal_text(ledger_nav),
        }

        opening = book.first_day[sleeve_id]
        end = book.last_day[sleeve_id]
        window = [d for d in book.days if opening <= d <= end]
        window_days = (date.fromisoformat(end) - date.fromisoformat(opening)).days
        idx_u = peak_u = idx_l = peak_l = _ONE
        dd_u = dd_l = _ZERO
        peak_ge = _ZERO
        peak_ge_day = opening
        series: list[dict[str, str]] = []
        daily_unlevered: dict[str, Decimal] = {}
        loan_days = 0
        max_loan = _ZERO
        previous: str | None = None
        for day in window:
            nav_day = _nav(book, sleeve_id, day)
            loan_day = loans[sleeve_id][day]
            equity = nav_day - loan_day
            if day not in book.nav[sleeve_id] or nav_day <= 0:
                raise SleeveLeverageError(
                    "SLEEVE_NAV_GAP", f"{sleeve_id} {day} nav {nav_day}"
                )
            if equity <= 0:
                raise SleeveLeverageError(
                    "NON_POSITIVE_EQUITY",
                    f"{sleeve_id} {day} nav {nav_day} loan {loan_day}",
                )
            gross_to_equity = nav_day / equity
            if gross_to_equity > peak_ge:
                peak_ge, peak_ge_day = gross_to_equity, day
            series.append(
                {
                    "day": day,
                    "nav": decimal_text(nav_day.quantize(_SERIES_PLACES)),
                    "loan": decimal_text(loan_day.quantize(_SERIES_PLACES)),
                    "equity": decimal_text(equity.quantize(_SERIES_PLACES)),
                }
            )
            if loan_day > 0:
                loan_days += 1
                max_loan = max(max_loan, loan_day)
            if previous is not None:
                base_nav = _nav(book, sleeve_id, previous)
                base_equity = base_nav - loans[sleeve_id][previous]
                pnl = nav_day - base_nav - book.flow[sleeve_id].get(day, _ZERO)
                unlevered = pnl / base_nav
                levered = pnl / base_equity
                daily_unlevered[day] = unlevered
                idx_u *= _ONE + unlevered
                peak_u = max(peak_u, idx_u)
                dd_u = min(dd_u, _drawdown(idx_u, peak_u))
                idx_l *= _ONE + levered
                if idx_l <= 0:
                    raise SleeveLeverageError("EQUITY_WIPED_OUT", f"{sleeve_id} {day}")
                peak_l = max(peak_l, idx_l)
                dd_l = min(dd_l, _drawdown(idx_l, peak_l))
            previous = day
        if len(window) < 2:
            raise SleeveLeverageError("WINDOW_TOO_SHORT", sleeve_id)

        if links is not None:
            closure["trace"] = _check_trace(links, daily_unlevered, idx_u, sleeve_id)

        digest = hashlib.sha256(
            json.dumps(series, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return SleeveLeverage(
            sleeve_id=sleeve_id,
            opening_date=opening,
            end_date=end,
            window_days=window_days,
            loan_rule=LOAN_RULE,
            allocation_rule=ALLOCATION_RULE,
            unlevered_cumulative=idx_u - _ONE,
            unlevered_annualised=_annualise(idx_u, window_days, "unlevered"),
            unlevered_max_drawdown=dd_u,
            unlevered_current_drawdown=_drawdown(idx_u, peak_u),
            levered_cumulative=idx_l - _ONE,
            levered_annualised=_annualise(idx_l, window_days, "levered"),
            levered_max_drawdown=dd_l,
            levered_current_drawdown=_drawdown(idx_l, peak_l),
            peak_gross_to_equity=peak_ge,
            peak_gross_to_equity_day=peak_ge_day,
            current_gross_to_equity=_nav(book, sleeve_id, end)
            / (_nav(book, sleeve_id, end) - loans[sleeve_id][end]),
            end_nav=_nav(book, sleeve_id, end),
            end_loan=loans[sleeve_id][end],
            end_equity=_nav(book, sleeve_id, end) - loans[sleeve_id][end],
            loan_days=loan_days,
            max_loan=max_loan,
            account_loan_days=sum(1 for d in book.days if _account_loan(book, d) > 0),
            account_max_loan=max(_account_loan(book, d) for d in book.days),
            series=tuple(series),
            series_sha256=digest,
            closure=closure,
        )


def _check_trace(
    links: Sequence[Mapping[str, Any]],
    daily: Mapping[str, Decimal],
    index: Decimal,
    sleeve_id: str,
) -> dict[str, Any]:
    """The unlevered daily returns and their chain equal the sleeve trace links."""

    if not links:
        raise SleeveLeverageError("TRACE_EMPTY", sleeve_id)
    trace_index = _ONE
    seen: set[str] = set()
    worst = _ZERO
    for link in links:
        end = str(link["end_date"])
        if end not in daily:
            raise SleeveLeverageError(
                "TRACE_DAY_MISSING", f"{sleeve_id} trace day {end}"
            )
        factor = _decimal(link["factor"], "trace factor")
        gap = abs(daily[end] - (factor - _ONE))
        worst = max(worst, gap)
        if gap > TRACE_TOLERANCE:
            raise SleeveLeverageError(
                "TRACE_MISMATCH",
                f"{sleeve_id} {end} facts {daily[end]} vs trace {factor - _ONE}",
            )
        trace_index *= factor
        seen.add(end)
    if seen != set(daily):
        raise SleeveLeverageError(
            "TRACE_DAYS_DIFFER", f"{sleeve_id} {sorted(set(daily) ^ seen)[:3]}"
        )
    chain_gap = abs(index - trace_index)
    if chain_gap > TRACE_TOLERANCE:
        raise SleeveLeverageError(
            "TRACE_CHAIN_MISMATCH", f"{sleeve_id} gap {chain_gap}"
        )
    return {
        "links": len(links),
        "max_daily_gap": decimal_text(worst),
        "chain_gap": decimal_text(chain_gap),
    }


# ----------------------------------------------------------------------------------------
# interest consistency: loan x broker debit rate against the interest actually charged
# ----------------------------------------------------------------------------------------


def _sleeve_cash(book: _Book, sleeve: str, day: str) -> Decimal:
    """Cash inside the sleeve NAV: NAV after allocated costs less the stock value."""

    if day not in book.nav[sleeve]:
        return _ZERO
    return book.nav[sleeve][day] - book.value[sleeve][day]


def _candidate_totals(book: _Book) -> dict[str, Callable[[str], Decimal]]:
    sleeves = tuple(book.nav)

    def bucket_debit(day: str) -> Decimal:
        return max(_ZERO, -book.bucket_nav[day])

    def net_cash_debit(day: str) -> Decimal:
        cash = book.bucket_nav[day] + sum(
            (_sleeve_cash(book, s, day) for s in sleeves), _ZERO
        )
        return max(_ZERO, -cash)

    def gross_debits(day: str) -> Decimal:
        own = sum((max(_ZERO, -_sleeve_cash(book, s, day)) for s in sleeves), _ZERO)
        return bucket_debit(day) + own

    return {
        "A_BUCKET_DEBIT": bucket_debit,
        "C_ACCOUNT_NET_CASH_DEBIT": net_cash_debit,
        "G_BUCKET_DEBIT_PLUS_SLEEVE_OWN_DEBITS": gross_debits,
    }


def _charged(
    rows: Sequence[Mapping[str, Any]], account_id: str, currency: str
) -> tuple[dict[str, tuple[Decimal, Decimal, Decimal]], dict[str, Any]]:
    """Net principal, interest and rate per value date for one account and currency.

    A posted row, its reversal and the re-post of a date net by signed sum, which is the
    broker's final figure for the date.  Debit principal and interest are returned positive.
    """

    principal: dict[str, Decimal] = defaultdict(Decimal)
    interest: dict[str, Decimal] = defaultdict(Decimal)
    rate: dict[str, tuple[str, Decimal]] = {}
    other_currency: dict[str, Decimal] = defaultdict(Decimal)
    used = 0
    for row in rows:
        if row.get("classification") != DEBIT_INTEREST_CLASSIFICATION:
            continue
        if row.get("account_id") != account_id:
            continue
        amount = row.get("amount") or {}
        if amount.get("status") != "AVAILABLE":
            continue
        row_currency = str(amount.get("currency"))
        if row_currency != currency:
            other_currency[row_currency] += -_decimal(row["total_interest"], "interest")
            continue
        value_date = str(row["value_date"])
        principal[value_date] += -_decimal(row["total_principal"], "principal")
        interest[value_date] += -_decimal(row["total_interest"], "interest")
        if row.get("code") == "Po":
            accounting = str(row["accounting_date"])
            if value_date not in rate or accounting >= rate[value_date][0]:
                rate[value_date] = (accounting, _decimal(row["rate"], "rate"))
        used += 1
    if not used:
        raise SleeveLeverageError("NO_DEBIT_INTEREST_ROWS", f"{account_id} {currency}")
    dates = sorted(principal)
    missing_rate = [d for d in dates if d not in rate]
    if missing_rate:
        raise SleeveLeverageError("DEBIT_RATE_MISSING", missing_rate[0])
    series = {d: (principal[d], interest[d], rate[d][1]) for d in dates}
    return series, {
        "rows": used,
        "value_dates": len(dates),
        "other_currencies_untested": {
            k: decimal_text(v) for k, v in other_currency.items()
        },
    }


def _loan_at(
    days: Sequence[str], loan: Callable[[str], Decimal], value_date: str, lag: int
) -> Decimal:
    cut = (
        bisect.bisect_right(days, value_date)
        if lag == 0
        else bisect.bisect_left(days, value_date)
    )
    return loan(days[cut - 1]) if cut > 0 else _ZERO


def interest_consistency(
    facts: Mapping[str, Any],
    leverage_rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Whether a candidate account loan reproduces the interest the broker charged.

    ``leverage_rows`` are the control panel's ``leverage_disclosures``; only the
    ``BROKER_DEBIT_INTEREST`` rows of the facts' base currency are tested (other currencies
    are named, not dropped).  For each candidate loan and each timing (the close of the value
    date, or the prior trading close) the report gives the debit principal reproduced per
    value date, and the interest ``loan x rate / 360`` against the interest charged.  A
    candidate reproduces the charge when, within the stated tolerances, at least
    ``INTEREST_DAY_SHARE_REQUIRED`` of value dates are reproduced and the total lies within
    ``INTEREST_TOTAL_RATIO_BAND`` of one.
    """

    book = _read(facts)
    account = account_id or book.parent_account_id
    currency = str(facts.get("base_currency", "USD"))
    charged, scope = _charged(leverage_rows, account, currency)
    dates = sorted(charged)
    with localcontext() as context:
        context.prec = _PRECISION
        # method check: the charge is principal x rate / 360 to the cent
        method_gap = max(
            abs(interest - principal * rate / Decimal(100) / INTEREST_DAY_COUNT_BASIS)
            for principal, interest, rate in charged.values()
        )
        method_ok = method_gap <= INTEREST_ROUNDING_TOLERANCE
        total_charged = sum((v[1] for v in charged.values()), _ZERO)
        report: dict[str, Any] = {
            "account_id": account,
            "currency": currency,
            "scope": scope,
            "first_value_date": dates[0],
            "last_value_date": dates[-1],
            "charged_interest_total": decimal_text(total_charged),
            "day_count_basis": int(INTEREST_DAY_COUNT_BASIS),
            "day_count_max_gap": decimal_text(method_gap),
            "day_count_confirmed": method_ok,
            "tolerance": {
                "day_relative": decimal_text(INTEREST_DAY_RELATIVE_TOLERANCE),
                "day_absolute": decimal_text(INTEREST_DAY_ABSOLUTE_TOLERANCE),
                "day_share_required": decimal_text(INTEREST_DAY_SHARE_REQUIRED),
                "total_ratio_band": decimal_text(INTEREST_TOTAL_RATIO_BAND),
            },
            "candidates": {},
        }
        for name, loan in _candidate_totals(book).items():
            for lag in (0, 1):
                implied_total = _ZERO
                reproduced = 0
                errors: list[Decimal] = []
                worst: list[tuple[Decimal, str, Decimal, Decimal]] = []
                for value_date in dates:
                    principal, _charge, rate = charged[value_date]
                    candidate = _loan_at(book.days, loan, value_date, lag)
                    implied_total += (
                        candidate * rate / Decimal(100) / INTEREST_DAY_COUNT_BASIS
                    )
                    gap = abs(candidate - principal)
                    if gap <= max(
                        INTEREST_DAY_RELATIVE_TOLERANCE * principal,
                        INTEREST_DAY_ABSOLUTE_TOLERANCE,
                    ):
                        reproduced += 1
                    else:
                        worst.append((gap, value_date, principal, candidate))
                    if principal > 0:
                        errors.append(gap / principal)
                errors.sort()
                worst.sort(reverse=True)
                ratio = implied_total / total_charged
                share = Decimal(reproduced) / Decimal(len(dates))
                report["candidates"][f"{name}|lag{lag}"] = {
                    "implied_interest_total": decimal_text(
                        implied_total.quantize(_SERIES_PLACES)
                    ),
                    "ratio_to_charged": decimal_text(
                        ratio.quantize(Decimal("0.000001"))
                    ),
                    "value_dates_reproduced": reproduced,
                    "value_dates": len(dates),
                    "median_relative_principal_error": decimal_text(
                        errors[len(errors) // 2].quantize(Decimal("0.000001"))
                    ),
                    "unreproduced_dates": [
                        {
                            "value_date": day,
                            "broker_principal": decimal_text(principal),
                            "candidate_loan": decimal_text(
                                candidate.quantize(Decimal("0.01"))
                            ),
                        }
                        for _gap, day, principal, candidate in worst[:12]
                    ],
                    "reproduces_charge": bool(
                        share >= INTEREST_DAY_SHARE_REQUIRED
                        and abs(ratio - _ONE) <= INTEREST_TOTAL_RATIO_BAND
                    ),
                }
        report["sleeve_allocations"] = _allocation_report(facts, book, charged)
    return report


def _allocation_report(
    facts: Mapping[str, Any],
    book: _Book,
    charged: Mapping[str, tuple[Decimal, Decimal, Decimal]],
) -> dict[str, Any]:
    """Candidate per-sleeve loans against the interest the facts allocated to each sleeve.

    The allocated interest is itself an exposure-share allocation of the account's booked
    interest (``allocated_cost.convention``), so agreement here is a consistency check and
    not independent evidence: only the account totals are independent.  The sleeve loan is
    taken at the prior trading close, as the return of the day uses it.
    """

    totals = _candidate_totals(book)
    sleeves = tuple(book.nav)
    booked: dict[str, Decimal] = {s: _ZERO for s in sleeves}
    try:
        convention = str(facts["allocated_cost"]["convention"])
        for row in facts["allocated_cost"]["sleeve_allocations"]:
            if str(row["sleeve_id"]) in booked:
                booked[str(row["sleeve_id"])] -= _decimal(
                    row["alloc_interest"], "alloc interest"
                )
    except (KeyError, TypeError) as exc:
        raise SleeveLeverageError("FACTS_SHAPE", f"allocated cost: {exc!r}") from exc

    def share(sleeve: str, day: str) -> Decimal:
        live = sum((_exposure(book, t, day) for t in sleeves), _ZERO)
        return _exposure(book, sleeve, day) / live if live > 0 else _ZERO

    def own_debit_plus_bucket_share(sleeve: str, day: str) -> Decimal:
        return max(_ZERO, -_sleeve_cash(book, sleeve, day)) + share(sleeve, day) * max(
            _ZERO, -book.bucket_nav[day]
        )

    rules: dict[str, Callable[[str, str], Decimal]] = {
        "EXPOSURE_SHARE_OF_A_BUCKET_DEBIT": lambda s, d: (
            share(s, d) * totals["A_BUCKET_DEBIT"](d)
        ),
        "EXPOSURE_SHARE_OF_C_ACCOUNT_NET_CASH_DEBIT": lambda s, d: (
            share(s, d) * totals["C_ACCOUNT_NET_CASH_DEBIT"](d)
        ),
        "OWN_NEGATIVE_CASH_PLUS_EXPOSURE_SHARE_OF_BUCKET_DEBIT": own_debit_plus_bucket_share,
    }
    out: dict[str, Any] = {
        "allocated_interest_convention": convention,
        "independent": False,
    }
    for name, rule in rules.items():
        implied = {s: _ZERO for s in sleeves}
        for value_date, (_principal, _charge, rate) in charged.items():
            cut = bisect.bisect_left(book.days, value_date)
            if cut == 0:
                continue
            for sleeve in sleeves:
                implied[sleeve] += (
                    rule(sleeve, book.days[cut - 1])
                    * rate
                    / Decimal(100)
                    / INTEREST_DAY_COUNT_BASIS
                )
        out[name] = {
            s: {
                "implied_interest": decimal_text(implied[s].quantize(Decimal("0.01"))),
                "allocated_interest": decimal_text(booked[s].quantize(Decimal("0.01"))),
            }
            for s in sleeves
        }
    return out


# ----------------------------------------------------------------------------------------
# settlement basis: the broker charges debit interest on settled cash, not trade-date cash
# ----------------------------------------------------------------------------------------

SETTLED_CASH_CLOSURE_TOLERANCE: Final = Decimal("0.01")
INTEREST_CENT_TOLERANCE: Final = Decimal("0.01")
_TRADE_DATE_BASIS: Final = "TRADE_DATE_NET_CASH_DEBIT_PRIOR_CLOSE"
_FACTS_SETTLED_BASIS: Final = "FACTS_CASH_LESS_UNSETTLED_TRADES"
_BROKER_SETTLED_BASIS: Final = "BROKER_ENDING_SETTLED_CASH"


def _trade_table(
    trades: Sequence[tuple[str, str, Decimal | str]],
) -> tuple[tuple[str, str, Decimal], ...]:
    table = []
    for trade_date, settle_date, proceeds in trades:
        if settle_date < trade_date:
            raise SleeveLeverageError(
                "TRADE_SETTLE_BEFORE_TRADE", f"{trade_date} {settle_date}"
            )
        table.append(
            (str(trade_date), str(settle_date), _decimal(proceeds, "trade proceeds"))
        )
    return tuple(table)


def unsettled_proceeds(trades: Sequence[tuple[str, str, Decimal]], day: str) -> Decimal:
    """Trade proceeds executed on or before ``day`` whose cash settles after it.

    Commissions are cash on the trade date and are not part of this amount.
    """

    return sum((p for td, sd, p in trades if td <= day < sd), _ZERO)


def settlement_interest_consistency(
    facts: Mapping[str, Any],
    leverage_rows: Sequence[Mapping[str, Any]],
    trades: Sequence[tuple[str, str, Decimal | str]],
    *,
    broker_cash: Mapping[str, tuple[Decimal | str, Decimal | str]] | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Interest reproduced on a settlement-date cash basis, for every value date.

    ``trades`` is ``(trade_date, settle_date, proceeds)`` per execution (the Flex
    ``tradeDate``, ``settleDateTarget`` and ``proceeds``).  ``broker_cash`` is the broker's
    own ``{day: (endingCash, endingSettledCash)}`` (Flex ``CashReportCurrency``); when given
    it is tested too, and closed against the trades and against the facts' derived cash.

    Tested on the value date's own close (carried over days without a statement): the
    facts' account cash less unsettled trades, and the broker's settled cash; against them
    the trade-date prior-close net cash debit of ``interest_consistency``.  Interest is
    ``principal x rate / 360``; a date is reproduced when its principal is within the
    stated tolerance, and cent-exact when the rounded interest equals the charged interest.
    The residuals of the dates the trade-date basis does not reproduce are listed.
    """

    book = _read(facts)
    account = account_id or book.parent_account_id
    currency = str(facts.get("base_currency", "USD"))
    charged, scope = _charged(leverage_rows, account, currency)
    table = _trade_table(trades)
    sleeves = tuple(book.nav)
    cent = Decimal("0.01")

    def facts_cash(day: str) -> Decimal:
        return book.bucket_nav[day] + sum(
            (_sleeve_cash(book, s, day) for s in sleeves), _ZERO
        )

    def facts_settled_debit(day: str) -> Decimal:
        return max(_ZERO, -(facts_cash(day) - unsettled_proceeds(table, day)))

    broker: dict[str, tuple[Decimal, Decimal]] = {}
    for day, (ending, settled) in (broker_cash or {}).items():
        broker[str(day)] = (
            _decimal(ending, "ending cash"),
            _decimal(settled, "settled cash"),
        )
    if broker_cash is not None and not broker:
        raise SleeveLeverageError("BROKER_CASH_EMPTY", account)
    broker_days = sorted(broker)

    def broker_debit(day: str) -> Decimal:
        return max(_ZERO, -broker[day][1])

    bases: dict[str, tuple[Sequence[str], Callable[[str], Decimal], int]] = {
        _TRADE_DATE_BASIS: (
            book.days,
            _candidate_totals(book)["C_ACCOUNT_NET_CASH_DEBIT"],
            1,
        ),
        _FACTS_SETTLED_BASIS: (book.days, facts_settled_debit, 0),
    }
    if broker:
        bases[_BROKER_SETTLED_BASIS] = (broker_days, broker_debit, 0)
    dates = sorted(charged)
    total_charged = sum((v[1] for v in charged.values()), _ZERO)
    report: dict[str, Any] = {
        "account_id": account,
        "scope": scope,
        "value_dates": len(dates),
        "charged_interest_total": decimal_text(total_charged),
        "trades": len(table),
        "bases": {},
    }
    per_date: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    with localcontext() as context:
        context.prec = _PRECISION
        for name, (days, debit, lag) in bases.items():
            reproduced = exact = outside = 0
            implied_total = _ZERO
            unreproduced: list[str] = []
            rows: dict[str, tuple[Decimal, Decimal]] = {}
            for value_date in dates:
                principal, charge, rate = charged[value_date]
                if name != _TRADE_DATE_BASIS and (
                    value_date < days[0] or value_date > days[-1]
                ):
                    outside += 1
                candidate = _loan_at(days, debit, value_date, lag)
                implied = candidate * rate / Decimal(100) / INTEREST_DAY_COUNT_BASIS
                implied_total += implied
                rows[value_date] = (candidate, implied)
                if abs(candidate - principal) <= max(
                    INTEREST_DAY_RELATIVE_TOLERANCE * principal,
                    INTEREST_DAY_ABSOLUTE_TOLERANCE,
                ):
                    reproduced += 1
                else:
                    unreproduced.append(value_date)
                if abs(implied.quantize(cent) - charge) <= INTEREST_CENT_TOLERANCE:
                    exact += 1
            per_date[name] = rows
            report["bases"][name] = {
                "lag": lag,
                "value_dates_reproduced": reproduced,
                "value_dates_cent_exact": exact,
                "implied_interest_total": decimal_text(implied_total.quantize(cent)),
                "ratio_to_charged": decimal_text(
                    (implied_total / total_charged).quantize(Decimal("0.000001"))
                ),
                "unreproduced_dates": unreproduced,
                "value_dates_outside_statement_coverage": outside,
            }
        residuals = []
        for value_date in report["bases"][_TRADE_DATE_BASIS]["unreproduced_dates"]:
            principal, charge, _rate = charged[value_date]
            entry: dict[str, Any] = {
                "value_date": value_date,
                "broker_principal": decimal_text(principal),
                "charged_interest": decimal_text(charge),
            }
            for name, rows in per_date.items():
                candidate, implied = rows[value_date]
                entry[name] = {
                    "principal": decimal_text(candidate.quantize(cent)),
                    "principal_residual": decimal_text(
                        (candidate - principal).quantize(cent)
                    ),
                    "implied_interest": decimal_text(
                        implied.quantize(Decimal("0.0001"))
                    ),
                    "interest_residual_cents": decimal_text(
                        (implied.quantize(cent) - charge).quantize(cent)
                    ),
                }
            residuals.append(entry)
        report["trade_date_unreproduced_dates"] = residuals
        if broker:
            gaps = sorted(
                (
                    (abs(broker[d][0] - unsettled_proceeds(table, d) - broker[d][1]), d)
                    for d in broker_days
                ),
                reverse=True,
            )
            open_days = [(g, d) for g, d in gaps if g > SETTLED_CASH_CLOSURE_TOLERANCE]
            facts_gaps = sorted(
                abs(facts_cash(d) - unsettled_proceeds(table, d) - broker[d][1])
                for d in broker_days
                if d in book.bucket_nav
            )
            report["settled_cash_closure"] = {
                "statement_days": len(broker_days),
                "first_day": broker_days[0],
                "last_day": broker_days[-1],
                "tolerance": decimal_text(SETTLED_CASH_CLOSURE_TOLERANCE),
                "closed_days": len(broker_days) - len(open_days),
                "open_days": [
                    {"day": d, "gap": decimal_text(g.quantize(cent))}
                    for g, d in open_days
                ],
                "facts_cash_vs_broker_settled_median_gap": (
                    decimal_text(facts_gaps[len(facts_gaps) // 2].quantize(cent))
                    if facts_gaps
                    else None
                ),
                "facts_cash_vs_broker_settled_max_gap": (
                    decimal_text(facts_gaps[-1].quantize(cent)) if facts_gaps else None
                ),
            }
    return report

"""Closure checks that decide whether leverage observations are covered (#894).

A pure function of provider-ledger rows: no file access, no account values of its own.
Each check returns a ``Closure`` that carries its evidence (counts, residual, bound) so
a reader can see why an account is, or is not, covered.

* ``financing_cost_closure``: per currency, the accrual balance rolls forward
  (``ending = previous ending + accrued + reversal``) across every observed day; a gap
  between two observed days (the provider omits a currency line on a day it has no
  activity) may carry a change only up to ``missing days x largest observed daily
  accrual``; every posting of the window is MAPPED or dated before the strategy epoch.
* ``borrowed_principal``: the base-level net cash series has one observation per
  statement day from its first day through the cutoff; the value at the cutoff is
  ``max(0, -net cash)``.
* ``gross_exposure``: the cutoff has one MAPPED gross position value and one NAV.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

BASE_LINE: Final = "BASE_SUMMARY"
_ACCRUAL_BASES: Final = ("ACCRUAL_BALANCE", "PERIOD_ACCRUAL", "ACCRUAL_REVERSAL")
_ZERO = Decimal(0)


# Row class RESERVE_WINDOW: a reserve account has no strategy interval.  Its UNMAPPED rows
# are attributed to the account's declared RESERVE window (the account financing
# declaration's sleeve_roles) and only through it: every day the row touches must lie
# inside a declared window, the row must carry no strategy, and the ledger's reason must
# be "no validated interval" (an ambiguous or epoch-crossing row means strategy windows
# exist and stays blocked).  No declared window, or days outside it, stays UNMAPPED.
RESERVE_WINDOW_REASONS: Final = (None, "PERIOD_MAPPING_MISSING_VALIDATED_INTERVAL")
_ROW_DAY_FIELDS: Final = (
    "period_start",
    "period_end",
    "value_date",
    "accounting_date",
    "event_date",
    "as_of_date",
)


def in_reserve_window(
    row: Mapping[str, Any],
    windows: Sequence[tuple[date, date, str]] | None,
) -> bool:
    if (
        not windows
        or row.get("mapping_status") != "UNMAPPED"
        or row.get("strategy_id")
        or row.get("mapping_reason") not in RESERVE_WINDOW_REASONS
    ):
        return False
    try:
        days = [date.fromisoformat(str(row[f])) for f in _ROW_DAY_FIELDS if row.get(f)]
    except ValueError:
        return False
    if not days:
        return False

    def covered(day: date) -> bool:
        return any(start <= day <= end for start, end, _basis in windows)

    first, last = min(days), max(days)
    if last - first > timedelta(days=370):
        return False
    return all(
        covered(first + timedelta(days=n)) for n in range((last - first).days + 1)
    )


@dataclass(frozen=True)
class Closure:
    check_id: str
    covered: bool
    evidence: Mapping[str, Any]


def _value(row: Mapping[str, Any], field: str = "source_amount") -> Decimal | None:
    amount = row.get(field)
    if not isinstance(amount, Mapping) or amount.get("status") != "AVAILABLE":
        return None
    return Decimal(str(amount["value"]))


def _day(text: object) -> date:
    return date.fromisoformat(str(text))


def financing_cost_closure(
    cost_rows: Iterable[Mapping[str, Any]],
    statement_days: Sequence[str],
    cutoff: str,
    epoch_start: str | None,
    reserve_windows: Sequence[tuple[date, date, str]] | None = None,
) -> Closure:
    """Interest accrual closure for one account, window = its statement days."""

    days = sorted(day for day in set(statement_days) if day <= cutoff)
    rows = [
        r
        for r in cost_rows
        if str(r.get("as_of_date") or r.get("period_end") or "") <= cutoff
    ]
    accruals = [r for r in rows if r.get("measurement_basis") in _ACCRUAL_BASES]
    by_currency: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    unavailable = 0
    for row in accruals:
        value = _value(row)
        currency = (row.get("source_amount") or {}).get("currency")
        if value is None or not isinstance(currency, str):
            unavailable += 1
            continue
        by_currency[currency][str(row["period_end"])][str(row["measurement_basis"])] = (
            value
        )
    if len(by_currency) > 1:
        by_currency.pop(BASE_LINE, None)  # the base line is the sum of the others
    unexplained: list[str] = []
    checked = 0
    last_day = days[-1] if days else None
    for currency, series in sorted(by_currency.items()):
        observed = sorted(series)
        daily = [
            abs(v["PERIOD_ACCRUAL"]) for v in series.values() if "PERIOD_ACCRUAL" in v
        ]
        largest = max(daily, default=_ZERO)
        previous: tuple[str, Decimal] | None = None
        for day in observed:
            entry = series[day]
            if "ACCRUAL_BALANCE" not in entry:
                unexplained.append(f"{currency}:{day}:NO_BALANCE")
                continue
            if previous is not None:
                missing = sum(1 for d in days if previous[0] < d < day)
                residual = (
                    entry["ACCRUAL_BALANCE"]
                    - previous[1]
                    - entry.get("PERIOD_ACCRUAL", _ZERO)
                    - entry.get("ACCRUAL_REVERSAL", _ZERO)
                )
                checked += 1
                if abs(residual) > Decimal("0.01") + largest * missing:
                    unexplained.append(f"{currency}:{day}:RESIDUAL")
            previous = (day, entry["ACCRUAL_BALANCE"])
        # days after the last observed row are allowed only when the accrual is closed out
        if (
            previous is not None
            and last_day is not None
            and previous[0] < last_day
            and previous[1] != _ZERO
        ):
            unexplained.append(f"{currency}:{last_day}:TRAILING_UNOBSERVED")
    reserve_placed = [
        r
        for r in rows
        if r.get("mapping_status") != "MAPPED" and in_reserve_window(r, reserve_windows)
    ]
    unmapped = [
        r
        for r in rows
        if r.get("mapping_status") != "MAPPED"
        and not in_reserve_window(r, reserve_windows)
        and (
            epoch_start is None
            or str(
                r.get("value_date")
                or r.get("accounting_date")
                or r.get("period_end")
                or r.get("event_date")
                or ""
            )
            >= epoch_start
        )
    ]
    # A row the strategy mapping cannot place is a mapping gap, and an account whose cost
    # has one is not covered; the reasons stay in the evidence so the gap is explainable.
    covered = (
        bool(accruals)
        and not unavailable
        and not unexplained
        and not unmapped
        and checked > 0
    )
    return Closure(
        "FINANCING_COST_ACCRUAL_ROLLFORWARD",
        covered,
        {
            "accrual_rows": len(accruals),
            "currencies": sorted(by_currency),
            "rollforward_steps_checked": checked,
            "unavailable_rows": unavailable,
            "unexplained_steps": sorted(unexplained)[:20],
            "unexplained_step_count": len(unexplained),
            "unmapped_rows_in_epoch": len(unmapped),
            "reserve_window_rows": len(reserve_placed),
            "unmapped_reasons": dict(
                sorted(Counter(str(r.get("mapping_reason")) for r in unmapped).items())
            ),
            "statement_days": len(days),
        },
    )


def borrowed_principal(
    liability_rows: Iterable[Mapping[str, Any]],
    statement_days: Sequence[str],
    cutoff: str,
    account_currency: str,
) -> tuple[Closure, Decimal | None]:
    """Net base-level cash: one observation per statement day; value at the cutoff."""

    base = [
        r
        for r in liability_rows
        if r.get("aggregation_level") == "BASE_SUMMARY"
        and r.get("measurement_basis") == "TRADE_DATE_CASH_BALANCE"
        and str(r.get("as_of_date")) <= cutoff
    ]
    series: dict[str, Decimal] = {}
    bad_days: set[str] = set()
    for row in base:
        value = _value(row, "amount")
        if (
            value is not None
            and (row.get("amount") or {}).get("currency") == account_currency
        ):
            series[str(row["as_of_date"])] = value
        else:
            bad_days.add(str(row["as_of_date"]))
    if not series:
        return Closure(
            "BORROWED_PRINCIPAL_BASE_CASH_SERIES", False, {"observations": 0}
        ), None
    first = min(set(series) | bad_days)
    expected = [d for d in sorted(set(statement_days)) if first <= d <= cutoff]
    missing = [d for d in expected if d not in series or d in bad_days]
    at_cutoff = series.get(cutoff)
    covered = not missing and at_cutoff is not None
    principal = max(_ZERO, -at_cutoff) if at_cutoff is not None else None
    return (
        Closure(
            "BORROWED_PRINCIPAL_BASE_CASH_SERIES",
            covered,
            {
                "observations": len(series),
                "first_day": first,
                "statement_days_in_span": len(expected),
                "missing_days": len(missing),
                "negative_days": sum(1 for v in series.values() if v < 0),
            },
        ),
        principal,
    )


def gross_exposure(
    exposure_rows: Iterable[Mapping[str, Any]],
    nav_rows: Iterable[Mapping[str, Any]],
    cutoff: str,
    reserve_windows: Sequence[tuple[date, date, str]] | None = None,
) -> tuple[Closure, Decimal | None, Decimal | None]:
    """Cutoff gross position value (MAPPED, or placed in a declared RESERVE window,
    AVAILABLE) and NAV."""

    def placed(r: Mapping[str, Any]) -> bool:
        return r.get("mapping_status") == "MAPPED" or in_reserve_window(
            r, reserve_windows
        )

    gross = [
        _value(r, "amount")
        for r in exposure_rows
        if str(r.get("as_of_date")) == cutoff and placed(r)
    ]
    navs = [
        _value(r, "nav")
        for r in nav_rows
        if str(r.get("as_of_date")) == cutoff and placed(r)
    ]
    gross_values = [v for v in gross if v is not None]
    nav_values = [v for v in navs if v is not None]
    covered = len(gross_values) == 1 and len(nav_values) == 1 and nav_values[0] != 0
    gross_value = gross_values[0] if len(gross_values) == 1 else None
    nav_value = nav_values[0] if len(nav_values) == 1 else None
    return (
        Closure(
            "GROSS_EXPOSURE_CUTOFF",
            covered,
            {"gross_rows": len(gross_values), "nav_rows": len(nav_values)},
        ),
        gross_value,
        nav_value,
    )

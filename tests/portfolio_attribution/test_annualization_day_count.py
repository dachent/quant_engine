"""#904 items 6 and 7: a comparison is annualised with the series' own calendar.

Robuxio is calendar-daily crypto (329 observations in 334 days) and was scaled
with a declared 252; the P123 and TT provider series keep market holidays on a
weekday calendar (about 261 a year) and were scaled with 252 too.  The kernel now
derives the periods a year from the common dates, and the frequency guard refuses
a declaration in both directions.  Every expected figure is recomputed here from
the series, to six decimals, not read back from the producer.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import math
from pathlib import Path
import statistics
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import portfolio_attribution as attribution  # noqa: E402


def _series(days: list[date], scale: str) -> dict[date, Decimal]:
    return {
        day: Decimal(scale) * (Decimal((index * 7) % 11) - 5)
        for index, day in enumerate(days)
    }


def _calendar(first: date, count: int) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(count)]


def _weekdays(first: date, last: date) -> list[date]:
    return [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if (first + timedelta(days=offset)).weekday() < 5
    ]


def _expected(
    account: dict[date, Decimal], reference: dict[date, Decimal], periods: int
) -> tuple[float, float]:
    active = [float(account[day] - reference[day]) for day in sorted(account)]
    return (
        statistics.fmean(active) * periods,
        statistics.stdev(active) * math.sqrt(periods),
    )


def _metrics(
    account: dict[date, Decimal], reference: dict[date, Decimal], declared: int
) -> dict[str, Any]:
    return attribution._comparison_metrics(account, reference, declared)


def test_calendar_daily_series_declared_252_is_scaled_with_365() -> None:
    """Robuxio shape: 329 daily rows in 334 days.  252 gave TE 24.0%, not 28.7%."""

    days = _calendar(date(2025, 12, 7), 329)
    account = _series(days, "0.0011")
    reference = _series(days, "0.0009")
    # shift one so the active series is not constant
    reference = {day: value / 2 for day, value in reference.items()}

    metrics = _metrics(account, reference, 252)

    active_return, tracking_error = _expected(account, reference, 365)
    assert round(float(metrics["active_return_annualized"]), 6) == round(
        active_return, 6
    )
    assert round(float(metrics["tracking_error_annualized"]), 6) == round(
        tracking_error, 6
    )
    assert metrics["periods_per_year"] == 365
    assert metrics["periods_per_year_declared"] == 252
    assert metrics["periods_per_year_basis"] == "DERIVED_FROM_DATES"
    # the 252 figure is a different number: the kill is real
    assert round(tracking_error, 6) != round(_expected(account, reference, 252)[1], 6)


def test_weekday_series_with_holidays_is_scaled_with_its_observed_dates() -> None:
    """P123 shape: weekdays including market holidays, about 261 a year."""

    days = _weekdays(date(2026, 2, 2), date(2026, 11, 6))
    account = _series(days, "0.0013")
    reference = {day: value / 3 for day, value in _series(days, "0.0013").items()}

    metrics = _metrics(account, reference, 252)

    span = (days[-1] - days[0]).days
    derived = round((len(days) - 1) * 365 / span)
    assert derived in (261, 262)  # weekdays a year, holidays kept
    assert derived != 252
    active_return, tracking_error = _expected(account, reference, derived)
    assert round(float(metrics["active_return_annualized"]), 6) == round(
        active_return, 6
    )
    assert round(float(metrics["tracking_error_annualized"]), 6) == round(
        tracking_error, 6
    )
    assert metrics["periods_per_year"] == derived


def test_a_trading_calendar_series_at_its_declared_frequency_is_unchanged() -> None:
    """Exchange-calendar dates (252 a year) keep their declared scaling and shape."""

    days = _weekdays(date(2025, 1, 6), date(2025, 12, 31))
    days = [day for index, day in enumerate(days) if index % 25 != 24]  # 10 holidays
    account = _series(days, "0.0012")
    reference = {day: value / 2 for day, value in _series(days, "0.0012").items()}
    metrics = _metrics(account, reference, 252)
    span = (days[-1] - days[0]).days
    assert abs(round((len(days) - 1) * 365 / span) - 252) <= 5
    assert "periods_per_year_declared" not in metrics
    _, tracking_error = _expected(account, reference, 252)
    assert round(float(metrics["tracking_error_annualized"]), 6) == round(
        tracking_error, 6
    )


@pytest.mark.parametrize("declared", [52, 12])
def test_too_many_observations_for_a_coarse_declaration_is_refused(
    declared: int,
) -> None:
    """Two-sided guard: daily dates declared weekly or monthly are not paired."""

    days = _calendar(date(2026, 1, 1), 200)
    account = _series(days, "0.001")
    reference = {day: value / 2 for day, value in _series(days, "0.001").items()}

    with pytest.raises(attribution.AnnualizationFrequencyError) as refusal:
        _metrics(account, reference, declared)

    assert refusal.value.detail["periods_per_year"] == declared
    assert "maximum_observations" in refusal.value.detail
    assert "minimum_observations" not in refusal.value.detail


def test_the_lower_bound_still_refuses_weekly_dates_declared_daily() -> None:
    days = [date(2026, 1, 2) + timedelta(days=7 * index) for index in range(27)]
    account = _series(days, "0.001")
    reference = {day: value / 2 for day, value in _series(days, "0.001").items()}

    with pytest.raises(attribution.AnnualizationFrequencyError) as refusal:
        _metrics(account, reference, 252)

    assert "minimum_observations" in refusal.value.detail


@pytest.mark.parametrize("declared", [52, 12])
def test_a_short_window_does_not_exempt_a_coarse_declaration(declared: int) -> None:
    """Review finding: 28 daily observations in 27 days declared weekly or monthly."""

    days = _calendar(date(2026, 1, 1), 28)
    account = _series(days, "0.001")
    reference = {day: value / 2 for day, value in _series(days, "0.001").items()}
    with pytest.raises(attribution.AnnualizationFrequencyError):
        _metrics(account, reference, declared)


def test_the_refusal_sentence_reads_once_for_each_direction() -> None:
    days = [date(2026, 1, 2) + timedelta(days=7 * index) for index in range(27)]
    weekly = attribution.AnnualizationFrequencyError(
        periods_per_year=252,
        observations=27,
        observed_start=days[0].isoformat(),
        observed_end=days[-1].isoformat(),
        minimum_observations=101,
    )
    row = attribution._frequency_refused_comparison({}, weekly)
    assert "that 252 that" not in row["disposition"]
    assert row["disposition"].endswith("are fewer than the 101 that frequency implies.")
    dense = attribution.AnnualizationFrequencyError(
        periods_per_year=12,
        observations=200,
        observed_start="2026-01-01",
        observed_end="2026-07-19",
        maximum_observations=8,
    )
    assert attribution._frequency_refused_comparison({}, dense)[
        "disposition"
    ].endswith("are more than the 8 that frequency implies.")

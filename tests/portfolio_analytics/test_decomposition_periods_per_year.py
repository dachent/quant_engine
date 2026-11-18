"""Annualization matrix D3: a fit is annualised with the periods a year its dates carry."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_decomposition as decomposition  # noqa: E402


def _weekdays(start: date, count: int) -> list[date]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def test_a_weekday_series_with_the_usual_holidays_keeps_252() -> None:
    days = _weekdays(date(2025, 1, 2), 560)
    kept = [d for index, d in enumerate(days) if index % 29 != 0]  # about nine a year
    assert decomposition._effective_periods_per_year(252, kept) == 252


def test_europe_economic_dates_without_holidays_are_260_not_252() -> None:
    """Every weekday, no holiday: 260-261 a year, 3.2% off the declared 252."""

    days = _weekdays(date(2025, 1, 1), 520)
    assert decomposition._effective_periods_per_year(252, days) in (260, 261)


def test_a_series_with_gaps_uses_its_observed_count() -> None:
    """VTMA market lenses: dates imply 242 a year (4.1% under the declared 252)."""

    days = _weekdays(date(2025, 1, 1), 520)
    kept = [d for index, d in enumerate(days) if index % 15 != 0]  # drop one date in 15
    got = decomposition._effective_periods_per_year(252, kept)
    assert got == round((len(kept) - 1) * 365 / (kept[-1] - kept[0]).days)
    assert got < 252 * 0.98


def test_a_calendar_daily_series_is_365() -> None:
    days = [date(2025, 1, 1) + timedelta(days=n) for n in range(400)]
    assert decomposition._effective_periods_per_year(252, days) == 365


@pytest.mark.parametrize("count", [2, 10, 18])
def test_under_four_weeks_the_declaration_stands(count: int) -> None:
    assert (
        decomposition._effective_periods_per_year(
            252, _weekdays(date(2026, 1, 5), count)
        )
        == 252
    )


def test_a_coarser_declaration_stands() -> None:
    assert (
        decomposition._effective_periods_per_year(52, _weekdays(date(2025, 1, 1), 400))
        == 52
    )


def test_the_fits_annualise_with_the_effective_count_not_the_declaration() -> None:
    """Wiring guard: the declaration is read only to echo identity and to derive the effective count."""

    source = (ROOT / "tools" / "portfolio_decomposition.py").read_text(encoding="utf-8")
    assert source.count("model.periods_per_year") == 4
    assert (
        source.count("_effective_periods_per_year(") >= 4
    )  # def + two fits + association

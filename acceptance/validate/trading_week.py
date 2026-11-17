"""The last NYSE trading day of an ISO week, restated for the validator.

The acceptance validator imports no production code, so the week-completeness rule of the weekly
source ("partial only while data stops before the last trading day of the week") is re-derived
here from the published NYSE schedule: weekends, and the regular full-day closures (New Year,
Martin Luther King, Presidents, Good Friday, Memorial, Juneteenth from 2022, Independence,
Labor, Thanksgiving, Christmas).  Ad hoc closures are not modelled.
"""

from __future__ import annotations

from datetime import date, timedelta


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    return date(year, (h + el - 7 * m + 114) // 31, (h + el - 7 * m + 114) % 31 + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    if nth > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _closures(year: int) -> set[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _nth_weekday(year, 5, 0, -1),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:  # a Saturday New Year is not observed on Friday
        days.add(_observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return days


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _closures(day.year)


def last_trading_day_of_week(week_start: date) -> date:
    """The last trading day of the ISO week starting ``week_start`` (a Friday, or earlier on a holiday)."""

    day = week_start + timedelta(days=4)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day

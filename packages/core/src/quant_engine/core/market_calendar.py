"""The regular NYSE trading calendar: weekends and the full-day closures of a year.

Ad hoc closures (a day of national mourning, a storm) are not modelled; a check built on this
calendar then refuses loudly rather than passing.
"""

from __future__ import annotations

from datetime import date, timedelta


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""

    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = (h + el - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The nth (1-based) weekday of a month; nth -1 is the last."""

    if nth > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    following = date(year + (month == 12), month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> frozenset[date]:
    """Regular NYSE full-day closures of one year (ad hoc closures are not modelled)."""

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
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year is not observed on Friday
        days.add(_observed(new_year))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return frozenset(days)


def is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def last_nyse_trading_day(day: date) -> date:
    """``day`` when it is a trading day, else the last trading day before it."""

    while not is_nyse_trading_day(day):
        day -= timedelta(days=1)
    return day

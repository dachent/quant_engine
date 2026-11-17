"""The validator's restated trading-week rule agrees with the production calendar."""

from __future__ import annotations

from datetime import date, timedelta

from acceptance.validate import trading_week
from quant_engine.core import is_nyse_trading_day, last_nyse_trading_day


def test_every_day_from_2018_to_2032_agrees_with_the_production_calendar() -> None:
    day = date(2018, 1, 1)
    while day <= date(2032, 12, 31):
        assert trading_week.is_trading_day(day) == is_nyse_trading_day(day), day
        day += timedelta(days=1)


def test_the_last_trading_day_of_a_week_is_holiday_aware() -> None:
    assert trading_week.last_trading_day_of_week(date(2026, 11, 2)) == date(2026, 11, 6)
    # Good Friday 2026-04-03
    assert trading_week.last_trading_day_of_week(date(2026, 3, 30)) == date(2026, 4, 2)
    # Thanksgiving week stays Friday (a half day is still a trading day)
    assert trading_week.last_trading_day_of_week(date(2026, 11, 23)) == date(
        2026, 11, 27
    )


def test_every_week_agrees_with_the_production_last_trading_day() -> None:
    monday = date(2018, 1, 1)
    while monday <= date(2032, 12, 27):
        assert trading_week.last_trading_day_of_week(monday) == last_nyse_trading_day(
            monday + timedelta(days=4)
        ), monday
        monday += timedelta(days=7)

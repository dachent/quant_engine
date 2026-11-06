from __future__ import annotations

from datetime import date

import pytest

from quant_engine.core import is_nyse_trading_day, last_nyse_trading_day, nyse_holidays


@pytest.mark.parametrize(
    "closed",
    [
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),  # July 4 is a Saturday: observed Friday
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    ],
)
def test_the_regular_2026_closures(closed: date) -> None:
    assert closed in nyse_holidays(2026)
    assert not is_nyse_trading_day(closed)


def test_a_new_year_on_a_saturday_is_not_observed_on_friday() -> None:
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2022, 1, 1) not in nyse_holidays(2022)


def test_the_last_trading_day_on_or_before() -> None:
    assert last_nyse_trading_day(date(2026, 11, 6)) == date(2026, 11, 6)
    assert last_nyse_trading_day(date(2026, 11, 7)) == date(2026, 11, 6)
    assert last_nyse_trading_day(date(2026, 4, 3)) == date(2026, 4, 2)
    assert last_nyse_trading_day(date(2026, 4, 5)) == date(2026, 4, 2)

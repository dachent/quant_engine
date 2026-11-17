"""Factor-model audit D2/D3: proxy factors are total returns with a spliced risk-free."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import base_proxy_factors as proxies  # noqa: E402

START = date(2026, 1, 1)
N = 120


def _days() -> list[date]:
    out, day = [], START
    while len(out) < N:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _first_weekday_indices() -> list[int]:
    out: list[int] = []
    previous_month = None
    for index, day in enumerate(_days()):
        if day.month != previous_month:
            out.append(index)
            previous_month = day.month
    return out


# first weekday of Feb..May, the last one (June) is the capture's latest distribution
_FIRSTS = _first_weekday_indices()
EARLIER = tuple(_FIRSTS[1:5])
LAST = _FIRSTS[5]


def _rows(
    *,
    bil_dividend_on: int | None = LAST,
    hyg_dividend_on: int | None = LAST,
    bil_skip: tuple[int, ...] = (),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    days = _days()
    for ticker, drift, pay, amount in (
        ("BIL", Decimal("0.000150"), bil_dividend_on, Decimal("0.27")),
        ("HYG", Decimal("0.000300"), hyg_dividend_on, Decimal("0.40")),
        ("SPY", Decimal("0.000400"), None, Decimal(0)),
        ("IWM", Decimal("0.000350"), None, Decimal(0)),
    ):
        price = Decimal(91)
        for n, day in enumerate(days):
            earlier = (
                ticker in ("BIL", "HYG")
                and n in EARLIER
                and not (ticker == "BIL" and n in bil_skip)
            )
            dividend = (
                amount if (pay is not None and n == pay) or earlier else Decimal(0)
            )
            # the close falls by the dividend on the ex-date, otherwise drifts up
            price = price * (1 + drift) - dividend if n else price
            rows.append(
                {
                    "Ticker": ticker,
                    "Date": f"{day.isoformat()}T00:00:00",
                    "Close": str(price),
                    "Dividends": str(dividend),
                }
            )
    return rows


def _french(upto: int) -> dict[date, Decimal]:
    return {day: Decimal("0.000150") for day in _days()[1:upto]}


def test_a_distribution_day_is_not_a_negative_excess_or_risk_free() -> None:
    """Kill test: the price-return build gave BIL about -0.3% on the ex-date."""

    built, _receipt = proxies.build_factors(_rows(), _french(100))
    ex_date = _days()[LAST].isoformat()
    row = next(item for item in built if item["date"] == ex_date)
    # total return adds the dividend back: BIL on its ex-date is about its drift
    assert Decimal(row["RF"]) > 0
    assert abs(Decimal(row["RF"]) - Decimal("0.00015")) < Decimal("0.0001")
    # HYG's own distribution does not show up as a loss either
    assert Decimal(row["HYG_XS"]) > Decimal("-0.001")
    assert all(Decimal(item["RF"]) >= 0 for item in built)


def test_the_risk_free_is_french_through_its_last_date_then_bil_total_return() -> None:
    built, receipt = proxies.build_factors(_rows(), _french(100))
    days = _days()
    assert receipt["risk_free_splice_after"] == days[99].isoformat()
    assert receipt["overlap_days"] == proxies.OVERLAP_DAYS
    row_before = next(i for i in built if i["date"] == days[50].isoformat())
    assert Decimal(row_before["RF"]) == Decimal("0.000150")  # French value verbatim
    row_after = next(i for i in built if i["date"] == days[115].isoformat())
    expected = Decimal("0.000150")  # BIL drifts at the same rate in the fixture
    assert abs(Decimal(row_after["RF"]) - expected) < Decimal("0.000001")


def test_an_unrecorded_latest_distribution_is_refused() -> None:
    """The real capture lacked the 2026-11-02 BIL and HYG distributions."""

    with pytest.raises(proxies.BaseProxyError, match="not posted"):
        proxies.build_factors(_rows(bil_dividend_on=None), _french(100))
    with pytest.raises(proxies.BaseProxyError, match="HYG"):
        proxies.build_factors(_rows(hyg_dividend_on=None), _french(100))


def test_a_risk_free_that_disagrees_with_bil_over_the_overlap_is_refused() -> None:
    wrong = {day: Decimal("0.0005") for day in _days()[1:100]}
    with pytest.raises(proxies.BaseProxyError, match="differs from French RF"):
        proxies.build_factors(_rows(), wrong)


def test_too_short_an_overlap_is_refused() -> None:
    with pytest.raises(proxies.BaseProxyError, match="overlap"):
        proxies.build_factors(_rows(), _french(30))


def test_the_french_rf_column_is_read_as_decimal_daily_returns() -> None:
    text = (
        "This file was created by CMPT_ME_BEME_RETS_DAILY\n\n"
        ",Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "20260930,  0.10, 0.2, 0.3, 0.4, 0.5, 0.017\n"
        "20260929,  0.10, 0.2, 0.3, 0.4, 0.5, 0.016\n"
        "\nAnnual Factors: January-December\n"
    )
    rates = proxies.french_rf_from_text(text)
    assert rates == {
        date(2026, 9, 30): Decimal("0.00017"),
        date(2026, 9, 29): Decimal("0.00016"),
    }
    with pytest.raises(proxies.BaseProxyError, match="no RF"):
        proxies.french_rf_from_text("nothing here")


def test_a_latest_distribution_missing_for_one_payer_is_refused_in_both_directions() -> (
    None
):
    """Review: BIL posted and HYG unposted (or the reverse) must refuse; 36 days is not the test."""

    for kwargs in ({"bil_dividend_on": None}, {"hyg_dividend_on": None}):
        with pytest.raises(proxies.BaseProxyError, match="no distribution recorded"):
            proxies.build_factors(_rows(**kwargs), _french(100))


def test_a_risk_free_gap_over_a_few_hundredths_of_a_percent_a_year_is_refused() -> None:
    off = {day: Decimal("0.000190") for day in _days()[1:100]}  # 4e-5/day above BIL
    with pytest.raises(proxies.BaseProxyError, match="differs from French RF"):
        proxies.build_factors(_rows(), off)


def test_the_overlap_tolerance_is_exactly_the_stated_bound() -> None:
    """Astra: the test must prove the 3e-5/day tolerance it names, both sides."""

    below = {day: Decimal("0.000179") for day in _days()[1:100]}  # gap 2.9e-5
    proxies.build_factors(_rows(), below)
    above = {day: Decimal("0.000181") for day in _days()[1:100]}  # gap 3.1e-5
    with pytest.raises(proxies.BaseProxyError, match="differs from French RF"):
        proxies.build_factors(_rows(), above)


def test_an_interior_french_gap_is_refused_not_filled_from_bil() -> None:
    """Astra: BIL is used only after French's last date, never inside its range."""

    french = _french(100)
    del french[_days()[50]]
    with pytest.raises(proxies.BaseProxyError, match="inside its range"):
        proxies.build_factors(_rows(), french)


def test_an_earlier_missing_monthly_distribution_is_refused() -> None:
    """Astra: a posted latest month must not hide ONE earlier unposted month."""

    with pytest.raises(proxies.BaseProxyError, match="no distribution recorded in"):
        proxies.build_factors(_rows(bil_skip=(EARLIER[1],)), _french(100))


def test_a_missing_calendar_month_is_refused_by_the_month_rule() -> None:
    """Review: a gap threshold can hide a month between short neighbours; months cannot."""

    rows = _rows(bil_skip=(EARLIER[2],))
    with pytest.raises(proxies.BaseProxyError, match="no distribution recorded in"):
        proxies.build_factors(rows, _french(100))

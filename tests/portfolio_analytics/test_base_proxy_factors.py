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


def _rows(
    *, bil_dividend_on: int | None = 110, hyg_dividend_on: int | None = 110
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
            dividend = amount if pay is not None and n == pay else Decimal(0)
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
    ex_date = _days()[110].isoformat()
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

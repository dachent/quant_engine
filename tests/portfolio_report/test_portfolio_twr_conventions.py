"""#904 item 1: the portfolio TWR is published on both bases, with trailing windows.

The governed index was published only as a cumulative figure (+60.6% over 752 days)
beside an annual MWR.  It now also carries its ACT/365 geometric rate (from 365 days
only), the trailing-12-month and the year-to-date index returns.  Every expected
figure is recomputed here from the index path to 6 decimals.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_totals as totals  # noqa: E402


def _proof(
    days: int, daily: float
) -> tuple[dict[str, object], list[tuple[date, float]]]:
    start = date(2024, 10, 15)
    path: list[tuple[date, float]] = []
    index = 100.0
    for offset in range(days + 1):
        path.append((start + timedelta(days=offset), index))
        index *= 1 + daily
    proof = {
        "status": "AVAILABLE",
        "index_path": [
            {"as_of": day.isoformat(), "index": format(value, ".15f")}
            for day, value in path
        ],
        "current_index": format(path[-1][1], ".15f"),
        "financial_acceptance": "CANDIDATE",
        "return_basis": "TEST",
        "policy_id": "test",
        "receipt_sha256": "0" * 64,
    }
    return proof, path


def _at_or_before(path: list[tuple[date, float]], day: date) -> float:
    return [value for when, value in path if when <= day][-1]


def test_the_cumulative_figure_is_published_with_its_annual_rate_and_trailing_windows() -> (
    None
):
    proof, path = _proof(752, 0.0006)
    block = totals.build_portfolio_twr(proof, [])
    end_day, end_value = path[-1]

    assert round(float(block["value"]), 6) == round(end_value / 100 - 1, 6)
    assert block["window_days"] == 752
    # ACT/365 geometric over the window
    assert round(float(block["annualised_act_365"]), 6) == round(
        (end_value / 100) ** (365 / 752) - 1, 6
    )
    # last 12 months: from the index on or before one year earlier
    base = _at_or_before(path, end_day - timedelta(days=365))
    assert round(float(block["trailing_12_months"]), 6) == round(
        end_value / base - 1, 6
    )
    # year to date: from the last observation of the previous year
    ytd_base = _at_or_before(path, date(end_day.year - 1, 12, 31))
    assert round(float(block["year_to_date"]), 6) == round(end_value / ytd_base - 1, 6)


def test_a_window_under_a_year_has_no_annual_rate_and_no_trailing_year() -> None:
    proof, path = _proof(200, 0.001)
    block = totals.build_portfolio_twr(proof, [])
    assert block["annualised_act_365"] is None
    # the index starts less than a year before its end: a trailing year is never
    # filled with a shorter window
    assert block["trailing_12_months"] is None
    assert block["value"] is not None


def test_a_stale_or_unordered_base_gives_no_trailing_window() -> None:
    """Review finding: the base was the last observation however old, from an unsorted path."""

    path = [
        {"as_of": "2025-06-30", "index": "100"},
        {"as_of": "2026-03-01", "index": "120"},
        {"as_of": "2026-03-02", "index": "121"},
    ]
    # year-to-date anchor is 2025-12-31 but the base would be 184 days old
    assert totals._index_return_since(path, date(2025, 12, 31), Decimal("121")) is None
    # an unordered path has no trailing window
    assert (
        totals._index_return_since(
            [path[1], path[0], path[2]], date(2026, 1, 1), Decimal("121")
        )
        is None
    )
    # 29 February: the anniversary falls back to the 28th
    assert totals._one_year_before(date(2028, 2, 29)) == date(2027, 2, 28)

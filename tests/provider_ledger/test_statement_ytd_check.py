"""John Hancock YTD contributions cross-check: refusing when covered, disclosed otherwise."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import statement_ytd_check as ytd  # noqa: E402

PAGE = """My Contributions
as of 11/10/2026
Year-to-Date Contributions
Roth
$24,500.00
View / Change Contribution Rate
"""


def _flow(day: str, amount: str, kind: str = "CONTRIBUTION") -> dict[str, object]:
    return {
        "event_date": day,
        "flow_type": kind,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
    }


def test_the_page_year_total_equals_the_flows_of_that_year_and_other_years_are_disclosed() -> (
    None
):
    result = ytd.ytd_contribution_check(
        PAGE,
        [_flow("2025-03-31", "23500.00"), _flow("2026-03-31", "24500.00")],
        date(2026, 9, 30),
    )

    assert result == {
        "flows_total": "24500.00",
        "not_covered_years": [2025],
        "page_as_of": "2026-11-10",
        "page_total": "24500.00",
        "status": "COVERED_EQUAL",
        "year": 2026,
    }


@pytest.mark.parametrize(
    ("flows", "message"),
    (
        ([_flow("2026-03-31", "24400.00")], "differ from the plan page"),
        ([_flow("2026-03-31", "24500.00"), _flow("2026-06-30", "1.00")], "exceed"),
    ),
)
def test_a_year_that_does_not_reconcile_is_refused(
    flows: list[dict[str, object]], message: str
) -> None:
    coverage_end = date(2026, 11, 30) if "exceed" not in message else date(2026, 9, 30)
    with pytest.raises(ytd.YtdContributionError, match=message):
        ytd.ytd_contribution_check(PAGE, flows, coverage_end)


def test_a_page_taken_after_the_last_statement_may_be_higher_and_says_so() -> None:
    result = ytd.ytd_contribution_check(
        PAGE, [_flow("2026-03-31", "20000.00")], date(2026, 9, 30)
    )
    assert result["status"] == "COVERED_PAGE_AFTER_COVERAGE_HIGHER"


def test_other_flow_kinds_are_not_contributions_and_a_bad_page_is_refused() -> None:
    result = ytd.ytd_contribution_check(
        PAGE,
        [_flow("2026-03-31", "24500.00"), _flow("2026-04-30", "-9.00", "FEE")],
        date(2026, 9, 30),
    )
    assert result["flows_total"] == "24500.00"
    with pytest.raises(ytd.YtdContributionError, match="layout"):
        ytd.ytd_contribution_check("nothing here", [], date(2026, 9, 30))

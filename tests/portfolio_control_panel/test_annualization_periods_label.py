"""The annualization label prints the periods per year it used (and says so in a title).

``benchmark_annualization`` and ``provider_annualization`` carry ``periods_per_year`` (a trading-day
calendar differs per provider: 252, 259, 261, 262, 365, 52) but their label text prints only the
observation count.  The panel adds ``×N periods a year`` beside the label and a title with the
object's own figures; it adds nothing when the label already prints the periods, and nothing visible
when the basis is cash-flow dates or calendar days (``periods_per_year`` null).  Synthetic rows.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from test_shell_qc1_labels import _capital_payload, _cost_strategy, _flat, _run


def _annualization(
    label: str,
    periods: int | None,
    observations: int | None = 251,
    window_days: int = 350,
    basis: str = "PERIODS_PER_YEAR_SCALED",
) -> dict[str, Any]:
    return {
        "basis": basis,
        "frequency": "DAILY",
        "label": label,
        "observations": observations,
        "periods_per_year": periods,
        "short_window": True,
        "window_days": window_days,
        "window_end": "2026-11-06",
        "window_start": "2025-11-21",
    }


def _table(tmp_path: Path, function: str, selector: str, **fields: Any) -> str:
    strategy = _cost_strategy(**fields)
    payload = _capital_payload(strategy)
    payload["reconciliation_checks"] = []
    return _run(tmp_path, payload, function)[selector]["innerHTML"]


def test_provider_and_benchmark_labels_print_the_periods_per_year(
    tmp_path: Path,
) -> None:
    table = _table(
        tmp_path,
        "renderCapital",
        "#strategy-table",
        provider_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_RESOLVED",
        provider_active_return_annualized="0.011",
        provider_annualization=_annualization(
            "annualized from 251 daily observations, 2025-11-21..2026-11-06 (<1 year)",
            261,
        ),
        benchmark_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_RESOLVED",
        benchmark_active_return_annualized="-0.004",
        benchmark_annualization=_annualization(
            "annualized from 241 daily observations, 2025-11-21..2026-11-06 (<1 year)",
            252,
            observations=241,
        ),
    )
    text = html.unescape(re.sub(r"<[^>]+>", " ", table))
    text = re.sub(r"\s+", " ", text)
    assert "annualized from 251 daily observations" in text
    assert "×261 periods a year" in text
    assert "×252 periods a year" in text
    # the title carries the object's own figures
    assert 'title="periods per year 261; 251 observations; window 350 days' in table
    assert 'title="periods per year 252; 241 observations; window 350 days' in table


def test_a_label_that_already_prints_the_periods_gets_nothing_added(
    tmp_path: Path,
) -> None:
    table = _table(
        tmp_path,
        "renderCapital",
        "#strategy-table",
        benchmark_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_RESOLVED",
        benchmark_active_return_annualized="-0.004",
        benchmark_annualization=_annualization(
            "annualized from 241 daily observations (×252 periods a year), 2025-11-21..2026-11-06 (<1 year)",
            252,
        ),
    )
    assert (
        _flat({"#t": {"innerHTML": table, "textContent": ""}}, "#t").count("×252") == 1
    )


def test_a_calendar_or_cash_flow_basis_adds_no_visible_periods_but_says_why_in_the_title(
    tmp_path: Path,
) -> None:
    table = _table(
        tmp_path,
        "renderSignals",
        "#implementation-table",
        mwr_xirr="0.061",
        mwr_annualization=_annualization(
            "annualized from cash flows, 2025-10-01..2026-09-17",
            None,
            observations=None,
            window_days=351,
            basis="XIRR_ACT_365",
        ),
    )
    assert "periods a year" not in table.replace('title="', "")  # not in visible text
    assert "not used: cash-flow dates or calendar days" in table
    assert "annualized from cash flows, 2025-10-01..2026-09-17" in table

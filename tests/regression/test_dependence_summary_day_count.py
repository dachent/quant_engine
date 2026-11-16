"""#904 regression case dependence_summary_day_count: Dependence summary annualised on 365.25 days and one period short (#904 item 8)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_correlation as correlation  # noqa: E402


def test_dependence_summary_day_count() -> None:
    rows = [
        correlation.ReturnRow(date(2025, 1, 1), 0.1, "ACCOUNT_LIVE", "v1", "0.1"),
        correlation.ReturnRow(date(2026, 1, 1), -0.1, "ACCOUNT_LIVE", "v1", "-0.1"),
    ]
    summary = correlation._performance_summary(rows, 0)
    assert summary["window_days"] == 730
    assert round(float(summary["cagr"]), 6) == round(0.99 ** (365 / 730) - 1, 6)

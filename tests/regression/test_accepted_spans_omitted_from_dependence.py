"""#904 regression case accepted_spans_omitted_from_dependence: Robuxio accepted multi-day return spans omitted from the dependence summary (#904 item 9)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_correlation as correlation  # noqa: E402


def test_accepted_spans_omitted_from_dependence() -> None:
    rows = [
        correlation.ReturnRow(date(2026, 1, 2), 0.01, "ACCOUNT_LIVE", "v1", "0.01"),
        correlation.ReturnRow(date(2026, 1, 12), 0.02, "ACCOUNT_LIVE", "v1", "0.02"),
    ]
    span = correlation.AcceptedSpan(date(2026, 1, 2), date(2026, 1, 9), -0.05, "-0.05")
    summary = correlation._performance_summary(rows, 0, [span])
    assert round(float(summary["cumulative_return"]), 6) == round(
        1.01 * 0.95 * 1.02 - 1, 6
    )

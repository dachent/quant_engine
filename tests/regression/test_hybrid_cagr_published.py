"""#904 regression case hybrid_cagr_published: CAGR published on a hybrid series that is mostly vendor backtest (#904 item 10)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_correlation as correlation  # noqa: E402


def test_hybrid_cagr_published() -> None:
    rows = [
        correlation.ReturnRow(date(2024, 1, 1), 0.1, "BACKTEST", "v1", "0.1"),
        correlation.ReturnRow(date(2026, 1, 1), 0.1, "ACCOUNT_LIVE", "v1", "0.1"),
    ]
    summary = correlation._performance_summary(rows, 0)
    assert summary["cagr"] is None
    assert summary["annualization_status"] == "SUPPRESSED_INCLUDES_BACKTEST"

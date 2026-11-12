"""#904 regression case short_window_account_cagr: Account CAGR published for a window under 365 days (#904 items 2-3)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_report as report  # noqa: E402


def test_short_window_account_cagr() -> None:
    row = {
        "account_live_cumulative_return": "0.10",
        "account_live_observed_start": "2026-01-01",
        "account_live_observed_end": "2026-07-30",
    }
    out = report._strategy_period_fields(row, None)
    assert "SUPPRESSED_UNDER_365D" in str(out["annualisation_basis"])
    assert out.get("twr_annualised") is None

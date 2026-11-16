"""#904 regression case calendar_series_scaled_with_252: Calendar-daily series annualised with 252 periods a year (#904 item 6)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_attribution as attribution  # noqa: E402


def test_calendar_series_scaled_with_252() -> None:
    days = [date(2026, 1, 1) + timedelta(days=n) for n in range(120)]
    assert attribution._effective_periods_per_year(days, 252)[0] == 365
    trading = [d for d in days if d.weekday() < 5]
    assert attribution._effective_periods_per_year(trading, 252)[0] == 261

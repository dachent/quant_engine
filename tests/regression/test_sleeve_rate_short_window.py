"""#904 regression case sleeve_rate_short_window: Sleeve return per year published from a 280-day window (#904 items 3-4)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import strategy_leverage as leverage  # noqa: E402


def test_sleeve_rate_short_window() -> None:
    assert leverage._annualise(Decimal("1.0721"), 280, "unlevered") is None
    value = leverage._annualise(Decimal("1.30"), 730, "levered")
    assert round(float(value), 6) == round(1.30**0.5 - 1, 6)

"""#904 regression case reconciliation_tracking_error_252: Reconciliation tracking error scaled with a hard-coded 252 on any calendar (#904 item 7)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import math  # noqa: E402
import statistics  # noqa: E402

import portfolio_implementation_reconciliation as reconciliation  # noqa: E402


def test_reconciliation_tracking_error_252() -> None:
    days = [date(2026, 1, 1) + timedelta(days=n) for n in range(120)]
    model = {d: 0.001 * ((n * 7) % 11 - 5) for n, d in enumerate(days)}
    actual = {d: v * 0.9 + 0.0003 * (n % 3) for n, (d, v) in enumerate(model.items())}
    metrics = reconciliation.relative_metrics(model, actual, accepted_as_of=days[-1])
    expected = statistics.stdev([actual[d] - model[d] for d in days]) * math.sqrt(365)
    assert round(metrics["annualized_tracking_error"], 6) == round(expected, 6)

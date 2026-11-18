"""Factor-model audit regression case risk_free_distribution_drop: The proxy risk-free column (a cash-ETF price return) carried a -0.316% distribution drop into every fit."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_decomposition as decomposition  # noqa: E402
import pytest  # noqa: E402


def test_risk_free_distribution_drop() -> None:
    rows = {
        date(2026, 11, 2): decomposition.FactorRow(date(2026, 11, 2), -0.003162413, {})
    }
    with pytest.raises(decomposition.DecompositionError, match="risk-free"):
        decomposition._check_risk_free(rows)

"""Factor-model audit D2: the fit refuses a risk-free column that is not a financing rate.

The proxy file's RF is a cash-ETF price return.  On a distribution day it drops by
about 0.3% (-0.003162 on 2026-11-02 in the release payload), and that value was
subtracted from the target and the proxies in every fit.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_decomposition as decomposition  # noqa: E402


def _rows(*values: float) -> dict[date, decomposition.FactorRow]:
    return {
        date(2026, 10, 1 + n): decomposition.FactorRow(date(2026, 10, 1 + n), v, {})
        for n, v in enumerate(values)
    }


def test_a_plausible_risk_free_column_passes() -> None:
    decomposition._check_risk_free(_rows(0.00017, 0.0, 0.00052, -0.0001094))


@pytest.mark.parametrize("bad", [-0.003162413, 0.0051, -0.0006])
def test_an_implausible_daily_risk_free_value_is_refused(bad: float) -> None:
    with pytest.raises(decomposition.DecompositionError, match="risk-free"):
        decomposition._check_risk_free(_rows(0.00017, bad, 0.00017))


def test_the_refusal_names_the_first_bad_date() -> None:
    with pytest.raises(decomposition.DecompositionError, match="2026-10-02"):
        decomposition._check_risk_free(_rows(0.0001, -0.003, -0.003))

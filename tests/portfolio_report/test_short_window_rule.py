"""#904 items 2, 3 and 5: one short-window rule for every annualised return.

An annual rate exists from 365 days.  Under it the window return and its day count
are the figure, and no annual rate is published: not the account CAGR, not the sleeve
"return per year", not the return on net capital (which scaled linearly under a year
and geometrically above).  Each expected figure is recomputed here from the index,
to six decimals.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import account_strategy_manifest as manifest  # noqa: E402
import strategy_leverage as leverage  # noqa: E402
import verify_account_manifest_generation as verifier  # noqa: E402


@pytest.mark.parametrize("days", [5, 41, 280, 364])
def test_sleeve_return_per_year_is_not_published_under_365_days(days: int) -> None:
    """AI Factor: 7.21% over 280 days was printed as a 9.50% yearly rate."""

    assert leverage._annualise(Decimal("1.0721"), days, "unlevered") is None


def test_sleeve_return_per_year_is_geometric_act_365_from_365_days() -> None:
    value = leverage._annualise(Decimal("1.30"), 730, "levered")
    assert value is not None
    # 30% over two years: 1.30 ** (365 / 730) - 1
    assert round(float(value), 6) == round(1.30**0.5 - 1, 6)
    one_year = leverage._annualise(Decimal("1.08"), 365, "levered")
    assert one_year is not None and round(float(one_year), 6) == 0.08


@pytest.mark.parametrize("days", [1, 40, 364, 365, 400])
def test_return_on_net_capital_is_geometric_act_365_at_every_window(days: int) -> None:
    """2.44% in 40 days was printed as 22.27% (linear); geometric is 26.9%."""

    value = manifest._annualize_return_on_net_capital(Decimal("0.0244"), days)
    assert round(float(value), 6) == round(1.0244 ** (365 / days) - 1, 6)
    # the verifier mirror agrees
    assert value == verifier._annualize_return_on_net_capital(Decimal("0.0244"), days)

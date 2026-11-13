"""#904 regression case ronc_linear_short_window: Return on net capital scaled linearly under a year and geometrically above (#904 item 5)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import account_strategy_manifest as manifest  # noqa: E402


def test_ronc_linear_short_window() -> None:
    value = manifest._annualize_return_on_net_capital(Decimal("0.0244"), 40)
    assert round(float(value), 6) == round(1.0244 ** (365 / 40) - 1, 6)

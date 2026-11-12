"""#904 regression case short_window_account_cagr: no account CAGR under 365 days.

Runs the end-to-end report check from the labels suite (its fixtures included):
a row with a CAGR must carry its basis and window, and a suppressed one must say
SUPPRESSED_UNDER_365D.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "portfolio_report"))

import test_n55_annualization_labels as labels  # noqa: E402

for _name in dir(labels):
    if _name != "test_cagr_and_mwr_carry_their_basis_and_window_at_version_four" and (
        _name.startswith("test_") is False
    ):
        globals().setdefault(_name, getattr(labels, _name))


def test_short_window_account_cagr(world_with_attribution) -> None:  # type: ignore[no-untyped-def]
    labels.test_cagr_and_mwr_carry_their_basis_and_window_at_version_four(
        world_with_attribution
    )

"""#904 regression case portfolio_twr_no_annual_rate: Portfolio TWR published only as a cumulative figure beside an annual MWR (#904 item 1)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))

import portfolio_totals as totals  # noqa: E402


def test_portfolio_twr_no_annual_rate() -> None:
    start, index, path = date(2024, 10, 15), 100.0, []
    for n in range(753):
        path.append(
            {
                "as_of": (start + timedelta(days=n)).isoformat(),
                "index": format(index, ".15f"),
            }
        )
        index *= 1.0006
    proof = {
        "status": "AVAILABLE",
        "index_path": path,
        "current_index": path[-1]["index"],
        "financial_acceptance": "CANDIDATE",
        "return_basis": "T",
        "policy_id": "t",
        "receipt_sha256": "0" * 64,
    }
    block = totals.build_portfolio_twr(proof, [])
    end = float(path[-1]["index"]) / 100
    assert round(float(block["annualised_act_365"]), 6) == round(
        end ** (365 / 752) - 1, 6
    )
    assert block["trailing_12_months"] is not None
    assert block["year_to_date"] is not None

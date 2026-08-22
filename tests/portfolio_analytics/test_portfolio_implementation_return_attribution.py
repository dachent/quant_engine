from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
MODULE_PATH = TOOLS_ROOT / "portfolio_implementation_return_attribution.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_implementation_return_attribution", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load implementation return attribution")
attribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attribution
SPEC.loader.exec_module(attribution)


def test_frongello_link_closes_exactly_to_cumulative_return() -> None:
    first = date(2026, 1, 2)
    second = date(2026, 1, 5)
    returns = {first: 0.10, second: -0.05}
    contributions = {
        first: {"model": 0.06, "other": 0.04},
        second: {"model": -0.02, "other": -0.03},
    }

    linked = attribution._frongello_link(returns, contributions)

    assert sum(linked.values()) == pytest.approx((1.10 * 0.95) - 1.0)
    assert linked["model"] == pytest.approx(0.06 * 0.95 - 0.02)
    assert linked["other"] == pytest.approx(0.04 * 0.95 - 0.03)


def test_ordered_bridge_retains_unsupported_amount_as_residual() -> None:
    result = attribution._ordered_bridge(
        0.10,
        [
            ("MISSED", 0.08, "test", "TESTED"),
            ("DELAY", 0.07, "test", "TESTED"),
        ],
        0.04,
        residual_name="RESIDUAL",
        residual_status="UNRESOLVED",
    )

    assert result["gap_pp"] == pytest.approx(-6.0)
    assert [row["effect_pp"] for row in result["categories"]] == pytest.approx(
        [-2.0, -1.0, -3.0]
    )
    assert result["categories"][-1]["status"] == "UNRESOLVED"


def test_canonical_symbol_joins_schwab_and_market_aliases() -> None:
    assert attribution._canonical_symbol("BRKB") == "BRK.B"
    assert attribution._canonical_symbol("BRK-B") == "BRK.B"
    assert attribution._canonical_symbol("SPY") == "SPY"


def test_scale_at_uses_latest_effective_date_not_insertion_order() -> None:
    schedules = {
        "MODEL": {
            date(2026, 2, 9): 0.01,
            date(2026, 3, 9): 0.02,
            date(2026, 1, 28): 0.005,
        }
    }

    assert attribution._scale_at(schedules, "MODEL", date(2026, 2, 1)) == 0.005
    assert attribution._scale_at(schedules, "MODEL", date(2026, 2, 20)) == 0.01
    assert attribution._scale_at(schedules, "MODEL", date(2026, 4, 1)) == 0.02


def test_authoritative_nav_providers_come_only_from_authoritative_admissions() -> None:
    ledger = {
        "admissions": [
            {
                "disposition": "REFERENCE_ONLY",
                "fact_type": "nav_observations",
                "provider_id": "alpaca",
            },
            {
                "disposition": "AUTHORITATIVE",
                "fact_type": "nav_observations",
                "provider_id": "schwab",
            },
            {
                "disposition": "AUTHORITATIVE",
                "fact_type": "positions",
                "provider_id": "ibkr",
            },
        ]
    }

    assert attribution._authoritative_nav_provider_ids(ledger) == {"schwab"}


def test_grizzly_transaction_ledger_keeps_costs_and_income_separate() -> None:
    rows = [
        {
            "event_date": "2026-01-02",
            "kind": "Journal",
            "amount": {"status": "AVAILABLE", "value": "50000"},
        },
        {
            "event_date": "2026-01-05",
            "kind": "Buy",
            "symbol": "SPXL",
            "quantity": "100",
            "execution_price": "100",
        },
        {
            "event_date": "2026-01-06",
            "kind": "Sell",
            "symbol": "SPXL",
            "quantity": "100",
            "execution_price": "110",
            "fees_and_commission": "1",
        },
        {
            "event_date": "2026-01-06",
            "kind": "Credit Interest",
            "amount": {"status": "AVAILABLE", "value": "2"},
        },
    ]
    prices = {date(2026, 1, 6): {"Close": 110.0}}

    gross = attribution._grizzly_actual_ledger_return(
        rows,
        prices,
        date(2026, 1, 6),
        include_income_and_fees=False,
    )
    net = attribution._grizzly_actual_ledger_return(
        rows,
        prices,
        date(2026, 1, 6),
        include_income_and_fees=True,
    )

    assert gross == pytest.approx(0.02)
    assert net == pytest.approx(0.02002)

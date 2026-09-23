"""Leader-owned synthetic B1 acceptance examples; no provider identifiers."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "b1_provider_ledger", _ROOT / "tools" / "provider_ledger.py"
)
assert _SPEC and _SPEC.loader
ledger = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ledger
_SPEC.loader.exec_module(ledger)


@pytest.mark.parametrize("withdrawal,expected", [("-35000", "0"), ("-5000", "30000")])
def test_all_pre_epoch_capital_is_reconciled(withdrawal: str, expected: str) -> None:
    account = "synthetic-b1-account"
    strategy = "synthetic-b1-strategy"
    flows = []
    navs = []
    for day, amount, nav in [
        ("2026-01-05", "35000", "35000"),
        ("2026-01-12", withdrawal, expected),
    ]:
        flows.append({
            "account_id": account,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
            "event_date": day,
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "nav_recognition_date": day,
            "provider_account_scope": "STATEMENT_ACCOUNT",
            "record_id": f"synthetic-flow-{day}",
            "source_artifact_id": "synthetic/statements.xml",
        })
        navs.append({
            "account_id": account,
            "as_of_date": day,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": nav},
            "record_id": f"synthetic-nav-{day}",
            "source_artifact_id": "synthetic/statements.xml",
            "value_basis": "ACCOUNT_TOTAL",
        })
    anchors = ledger._ibkr_capital_anchors(
        "synthetic-generation",
        {"flows": flows, "nav_observations": navs},
        {account}, {strategy},
        {account: [{"effective_start": "2026-01-22", "mapping_status": "MAPPED",
                    "strategy_id": strategy}]},
    )
    if expected == "0":
        assert anchors == []
    else:
        assert len(anchors) == 1
        assert anchors[0]["amount"]["value"] == expected
        assert anchors[0]["opening_nav"]["value"] == expected
        assert anchors[0]["capital_type"] == "OPENING_CAPITAL_ANCHOR"

"""#905 / Ruling C: a last observation before the cutoff never carries an AVAILABLE label."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report", ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = sys.modules.get("portfolio_report") or importlib.util.module_from_spec(SPEC)
if "portfolio_report" not in sys.modules:
    with pytest.MonkeyPatch.context() as _registry:
        _registry.setitem(sys.modules, "portfolio_report", reporter)
        SPEC.loader.exec_module(reporter)


def test_stale_last_observation_is_historical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reporter,
        "_current_capital",
        lambda _c: {
            "reserve": {
                "status": "NOT_READY_AT_CUTOFF",
                "as_of_date": None,
                "current_value": {
                    "currency": "USD",
                    "status": "UNAVAILABLE",
                    "value": None,
                },
            }
        },
    )
    catalog = {
        "as_of_date": "2026-09-17",
        "accounts": [
            {
                "account_id": "a",
                "role": "INVESTMENT",
                "provider": "P",
                "base_currency": "USD",
            }
        ],
        "nav_observations": [
            {
                "account_id": "a",
                "as_of_date": "2025-11-24",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "5"},
            }
        ],
    }
    (row,) = reporter._accounts(catalog)
    assert row["status"] == "HISTORICAL_LAST_OBSERVATION"

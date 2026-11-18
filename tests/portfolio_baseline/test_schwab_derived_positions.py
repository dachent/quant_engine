"""Schwab positions derived at the cutoff keep their evidence class through the baseline."""

from __future__ import annotations

import copy
from datetime import date
import hashlib
from typing import Any

import pytest

import portfolio_baseline as baseline
from quant_engine.core import canonical_json_bytes

ACCOUNT = "account-schwab"
CUTOFF = "2026-11-06"
DERIVED = "DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT"
ARTIFACT = "evidence/positions.csv"


def _derived_row() -> dict[str, Any]:
    security = "security-schwab-" + hashlib.sha256(b"SWVXX").hexdigest()[:24]
    identity = {
        "account_id": ACCOUNT,
        "as_of_date": CUTOFF,
        "basis": DERIVED,
        "security_id": security,
    }
    return {
        "account_id": ACCOUNT,
        "as_of_basis": DERIVED,
        "as_of_date": CUTOFF,
        "capture_date": "2026-11-17",
        "closure": {
            "cash_usd": "5.73",
            "earlier_market_value_days": ["2026-11-14"],
            "history_through": "2026-11-08",
            "holdings_usd": "100",
            "positions_total_usd": "105.73",
        },
        "evidence_class": DERIVED,
        "mapping_status": "UNMAPPED",
        "market_value": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
        "quantity": "100",
        "record_id": "position-"
        + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:24],
        "security_id": security,
        "source_artifact_id": ARTIFACT,
        "strategy_id": None,
        "symbol": "SWVXX",
    }


def _facts(row: dict[str, Any], **catalog_over: Any) -> list[dict[str, Any]]:
    catalog: dict[str, Any] = {
        "positions": [row],
        "transactions": [],
        "nav_observations": [
            {
                "account_id": ACCOUNT,
                "as_of_date": "2026-11-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
            },
            {
                "account_id": ACCOUNT,
                "as_of_date": CUTOFF,
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "105.73"},
            },
        ],
        "coverage": {
            "known_gaps": [],
            "transaction_history": {
                "accounts": {
                    ACCOUNT: {
                        "effective_start": "2022-11-08",
                        "effective_end": "2026-11-08",
                    }
                }
            },
        },
        "position_snapshot_as_of": {
            "observed_at": "2026-11-08T23:15:39+00:00",
            "valuation_cutoff": CUTOFF,
        },
        "source_artifacts": [
            {"artifact_id": ARTIFACT, "schema": {"name": "schwab-positions-export"}},
            {
                "artifact_id": "evidence/balances-2026-11-14.txt",
                "schema": {"name": "schwab-live-balances-evidence"},
            },
        ],
    }
    catalog.update(catalog_over)
    return baseline._schwab_facts(
        catalog,
        [{"account_id": ACCOUNT, "provider": "SCHWAB"}],
        [],
        [{"account_id": ACCOUNT, "as_of_date": "2026-11-01", "nav": {"value": "1"}}],
        date(2026, 11, 8),
    )[0]


def test_a_derived_position_keeps_its_evidence_class_in_the_baseline() -> None:
    (row,) = _facts(_derived_row())
    assert row["evidence_class"] == row["as_of_basis"] == DERIVED
    assert row["capture_date"] == "2026-11-17"
    assert row["as_of_date"] == CUTOFF


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(evidence_class="WCH_CURRENT"),
        lambda r: r["closure"].update(positions_total_usd="106.00"),
        lambda r: r["closure"].update(earlier_market_value_days=[]),
        lambda r: r.update(observed_at="2026-11-08T23:15:39+00:00"),
        lambda r: r.update(average_price="1"),
        lambda r: r["market_value"].update(currency="EUR"),
        lambda r: r.update(strategy_id="strategy-x"),
        lambda r: r.update(source_artifact_id="evidence/other.csv"),
    ],
)
def test_a_derived_position_that_does_not_re_verify_is_refused(mutate: Any) -> None:
    row = copy.deepcopy(_derived_row())
    mutate(row)
    with pytest.raises(baseline.BaselineValidationError):
        _facts(row)


def test_a_derived_position_needs_the_snapshot_cutoff() -> None:
    with pytest.raises(baseline.BaselineValidationError):
        _facts(_derived_row(), position_snapshot_as_of={})


def test_an_unlabelled_non_current_position_is_still_refused() -> None:
    row = _derived_row()
    for key in ("as_of_basis",):
        row.pop(key)
    row["evidence_class"] = "OBSERVED"
    with pytest.raises(
        baseline.BaselineValidationError, match="evidence class differs"
    ):
        _facts(row)


def test_a_transaction_after_the_cutoff_refuses_the_derived_position() -> None:
    late = {"account_id": ACCOUNT, "event_date": "2026-11-07", "record_id": "t-late"}
    with pytest.raises(baseline.BaselineValidationError, match="after the cutoff"):
        _facts(_derived_row(), transactions=[late])


def test_the_baseline_row_names_the_residual_reallocation_window() -> None:
    (row,) = _facts(_derived_row())
    assert row["residual_reallocation_window_days"] == ["2026-11-08", "2026-11-14"]


def test_a_missing_cutoff_day_nav_refuses_the_derived_position() -> None:
    nav = [
        {
            "account_id": ACCOUNT,
            "as_of_date": "2026-11-01",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
        }
    ]
    with pytest.raises(baseline.BaselineValidationError, match="exactly one NAV"):
        _facts(_derived_row(), nav_observations=nav)

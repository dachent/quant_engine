"""D1/D2: mwr_coverage_status follows the flow-path status, never a literal.

The real d2e5 row (robuxio-crypto) published `mwr_coverage_status` COMPLETE with
zero flow legs beside a flows coverage row saying
NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF.  The MWR number is right (it matches
both oracles); only the coverage claim was unproven.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import portfolio_report as reporter

ROOT = Path(__file__).resolve().parents[2]
EPOCH = ("account-zero-legs", "strategy-owned", "2026-01-01", None)


def _payload(coverage: str) -> dict[str, Any]:
    return {
        "mwr_cashflows": [],
        "mwr_coverage_status": coverage,
        "mwr_day_count": "ACTUAL_365",
        "mwr_label": "ACCOUNT_EPOCH_MWR",
        "mwr_opening_date": "2026-01-01",
        "mwr_opening_value": "100",
        "mwr_reason": None,
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
        "mwr_status": "AVAILABLE",
        "mwr_terminal_date": "2026-01-10",
        "mwr_terminal_value": "125",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_valuation_timing": "NAV_RECOGNITION_DATE_OR_EXACT_FILL_DATE",
        "mwr_value_basis": "ACCOUNT_TOTAL",
        "mwr_xirr": "0.9",
        "flow_count": 0,
        "flow_timing": None,
        "nav_observations": 9,
        "observed_end": "2026-01-10",
        "observed_start": "2026-01-02",
        "twr": "0.25",
        "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
    }


def _rows(flows_status: str | None, coverage: str = "COMPLETE") -> list[dict[str, Any]]:
    details = {
        EPOCH: {
            "account_label": "Zero legs",
            "last_four": "0000",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "performance_payload": _payload(coverage),
        }
    }
    catalog: dict[str, Any] = {
        "assignments": [
            {
                "account_id": EPOCH[0],
                "assignment_id": "assignment-zero-legs",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "pool_id": "systematic-capital",
                "pool_mapping_status": "MAPPED",
                "strategy_id": EPOCH[1],
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_returns": [],
    }
    if flows_status is not None:
        catalog["fact_coverage_matrix"] = [
            {
                "account_id": EPOCH[0],
                "fact_type": "nav",
                "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
            },
            {"account_id": EPOCH[0], "fact_type": "flows", "status": flows_status},
        ]
    return reporter._account_strategy_performance(catalog, None, set(details), details)


def test_zero_leg_mwr_is_not_complete_beside_an_unproven_flows_row() -> None:
    row = _rows("NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF")[0]

    assert row["mwr_status"] == "AVAILABLE"
    assert row["mwr_xirr"] == "0.9"
    assert row["mwr_coverage_status"] == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"


def test_mwr_over_an_account_with_no_accepted_flow_path_is_not_complete() -> None:
    row = _rows("NO_ACCEPTED_FACT_PATH")[0]

    assert row["mwr_coverage_status"] == "NO_ACCEPTED_FACT_PATH"


def test_complete_stays_when_the_flows_row_does_not_deny_it() -> None:
    assert _rows("OBSERVED_NOT_COMPLETENESS_PROOF")[0]["mwr_coverage_status"] == (
        "COMPLETE"
    )
    assert _rows(None)[0]["mwr_coverage_status"] == "COMPLETE"


def test_a_blocked_coverage_claim_is_never_widened() -> None:
    row = _rows("NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF", coverage="BLOCKED")[0]

    assert row["mwr_coverage_status"] == "BLOCKED"


def test_report_schema_admits_the_derived_coverage_values() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "portfolio" / "report-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    enum = schema["$defs"]["performance"]["properties"]["mwr_coverage_status"]["enum"]

    assert set(enum) >= {
        "COMPLETE",
        "BLOCKED",
        None,
        "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF",
        "NO_ACCEPTED_FACT_PATH",
    }
    assert reporter.MWR_COVERAGE_DENIED_BY_FLOW_PATH <= set(enum)

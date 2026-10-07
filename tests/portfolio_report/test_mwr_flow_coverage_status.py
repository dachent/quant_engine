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


def _rows(
    flows_status: str | None,
    coverage: str = "COMPLETE",
    *,
    matrix: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
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
    if matrix is not None:
        catalog["fact_coverage_matrix"] = matrix
    elif flows_status is not None:
        catalog["fact_coverage_matrix"] = [
            {
                "account_id": EPOCH[0],
                "fact_type": "nav",
                "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
            },
            {"account_id": EPOCH[0], "fact_type": "flows", "status": flows_status},
        ]
    return reporter._account_strategy_performance(catalog, None, set(details), details)


def test_zero_leg_mwr_keeps_the_receipt_status_and_names_the_unproven_flow_path() -> (
    None
):
    row = _rows("NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF")[0]

    assert row["mwr_status"] == "AVAILABLE"
    assert row["mwr_xirr"] == "0.9"
    # The manifest's flow-coverage receipt status is copied, never overwritten.
    assert row["mwr_coverage_status"] == "COMPLETE"
    assert row["mwr_flow_path_status"] == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"


def test_mwr_over_an_account_with_no_accepted_flow_path_says_so() -> None:
    row = _rows("NO_ACCEPTED_FACT_PATH")[0]

    assert row["mwr_coverage_status"] == "COMPLETE"
    assert row["mwr_flow_path_status"] == "NO_ACCEPTED_FACT_PATH"


def test_an_ordinary_flows_row_is_published_as_is_and_a_missing_row_is_null() -> None:
    assert _rows("OBSERVED_NOT_COMPLETENESS_PROOF")[0]["mwr_flow_path_status"] == (
        "OBSERVED_NOT_COMPLETENESS_PROOF"
    )
    assert _rows(None)[0]["mwr_flow_path_status"] is None


def test_a_blocked_receipt_status_is_never_widened() -> None:
    row = _rows("NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF", coverage="BLOCKED")[0]

    assert row["mwr_coverage_status"] == "BLOCKED"


def test_report_schema_keeps_the_receipt_enum_and_adds_the_flow_path_field() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "portfolio" / "report-data.schema.json").read_text(
            encoding="utf-8"
        )
    )
    properties = schema["$defs"]["performance"]["properties"]

    assert set(properties["mwr_coverage_status"]["enum"]) == {
        "COMPLETE",
        "BLOCKED",
        None,
    }
    assert properties["mwr_flow_path_status"] == {"type": ["string", "null"]}
    assert "mwr_flow_path_status" not in schema["$defs"]["performance"]["required"]
    assert reporter.MWR_FLOW_PATH_UNPROVEN == {
        "NO_ACCEPTED_FACT_PATH",
        "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF",
    }


def test_the_baselines_own_flow_coverage_row_drives_the_report_disclosure() -> None:
    """Real producer: the matrix row comes from ``_coverage_evidence``, not a literal.

    An account with an effective-dated flow admission but no reported flow events
    gets the status the report must disclose beside a COMPLETE receipt.
    """

    from datetime import date

    import portfolio_baseline as baseline

    account_id = EPOCH[0]
    matrix = baseline._coverage_evidence(
        [{"account_id": account_id, "provider": "SYNTHETIC", "role": "INVESTMENT"}],
        [],
        [
            {
                "account_id": account_id,
                "disposition": "AUTHORITATIVE",
                "effective_end": "2026-01-10",
                "effective_start": "2026-01-01",
                "fact_type": "flows",
            }
        ],
        [
            {
                "coverage": {
                    "boundary": "synthetic",
                    "observed_end": "2026-01-10",
                    "observed_start": "2026-01-01",
                    "status": "PARTIAL",
                },
                "provider_id": "SYNTHETIC",
            }
        ],
        [],
        [],
        [],
        [],
        date(2026, 1, 10),
    )[0]
    flows_row = next(
        row
        for row in matrix
        if row["account_id"] == account_id and row["fact_type"] == "flows"
    )
    assert flows_row["status"] == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"

    row = _rows(None, matrix=matrix)[0]

    assert row["mwr_coverage_status"] == "COMPLETE"
    assert row["mwr_flow_path_status"] == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"

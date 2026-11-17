"""Implementation-gap status is derived from the row's own evidence, not read from the audit CSV.

The retained ``implementation-gap`` input is a dated hand audit (a token and a note per
strategy).  The status shown beside every strategy must come from the evidence the row
carries at build time: a verified implementation reconciliation, or an exact-date provider
comparison with observations, is "tested"; no comparator at all is "not_testable".  The
audit token survives only as provenance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


import portfolio_report as reporter  # noqa: E402


def _merge(
    row: dict[str, Any], comparison: dict[str, Any], metrics: dict[str, Any] | None
) -> dict[str, Any]:
    sid = "syn-strategy"
    return reporter._merge_attribution(  # noqa: SLF001
        [{"strategy_id": sid, **row}],
        {
            "benchmark_assignments": [{"strategy_id": sid, "status": "EVIDENCED"}],
            "benchmark_comparisons": [{"strategy_id": sid, "status": "UNAVAILABLE"}],
            "comparisons": [
                {
                    "strategy_id": sid,
                    **comparison,
                    **({} if metrics is None else {"metrics": metrics}),
                }
            ],
            "costs": [{"strategy_id": sid}],
        },
    )[0]


def test_a_verified_implementation_reconciliation_is_tested_whatever_the_audit_says() -> (
    None
):
    row = _merge(
        {
            "implementation_gap_status": "not_same_vintage",
            "implementation_reconciliation_status": "VERIFIED_CAUSAL_RETURN_ATTRIBUTION",
        },
        {"status": "UNAVAILABLE"},
        None,
    )

    assert row["implementation_gap_status"] == "tested"
    assert row["implementation_gap_basis"] == "VERIFIED_IMPLEMENTATION_RECONCILIATION"
    assert row["implementation_gap_audit_status"] == "not_same_vintage"


def test_an_exact_date_provider_comparison_with_observations_is_tested() -> None:
    row = _merge(
        {
            "implementation_gap_status": "not_testable",
            "implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE",
        },
        {
            "status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            "version_compatibility_status": "UNRESOLVED",
        },
        {"observations": 251},
    )

    assert row["implementation_gap_status"] == "tested"
    assert row["implementation_gap_basis"] == "EXACT_DATE_PROVIDER_COMPARISON"
    assert row["implementation_gap_audit_status"] == "not_testable"
    # the unresolved version stays visible beside the status
    assert row["provider_comparison_version_compatibility"] == "UNRESOLVED"


def test_a_stale_tested_audit_token_does_not_make_a_row_tested() -> None:
    row = _merge(
        {
            "implementation_gap_status": "tested",
            "implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE",
        },
        {"status": "UNAVAILABLE"},
        None,
    )

    assert row["implementation_gap_status"] == "not_testable"
    assert row["implementation_gap_basis"] == "NO_COMPARATOR_ADMITTED"
    assert row["implementation_gap_audit_status"] == "tested"


def test_no_comparator_at_all_is_not_testable_not_partial_or_vintage() -> None:
    row = _merge(
        {
            "implementation_gap_status": "partial_only",
            "implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE",
        },
        {"status": "UNAVAILABLE"},
        None,
    )

    assert row["implementation_gap_status"] == "not_testable"


def test_a_comparison_without_observations_is_not_tested() -> None:
    for observations in (0, None, "251", True):
        row = _merge(
            {"implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE"},
            {"status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"},
            None if observations is None else {"observations": observations},
        )
        assert row["implementation_gap_status"] == "not_testable", observations

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
            "implementation_reconciliation": {
                "return_reconciliation": {
                    "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
                    "observations": 213,
                }
            },
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


def test_a_verified_contract_with_an_unavailable_return_reconciliation_is_not_tested() -> (
    None
):
    unavailable = {"return_reconciliation": {"status": "UNAVAILABLE_NO_COMMON_DATES"}}
    for implementation in (
        unavailable,
        {"return_reconciliation": {"status": "X", "observations": 12}},
        {"return_reconciliation": {"status": "PARTIAL", "observations": 12}},
        None,
    ):
        row = _merge(
            {
                "implementation_reconciliation_status": "VERIFIED_TRADE_AND_RETURN_DECONSTRUCTION",
                **(
                    {}
                    if implementation is None
                    else {"implementation_reconciliation": implementation}
                ),
            },
            {"status": "UNAVAILABLE"},
            None,
        )
        assert row["implementation_gap_status"] == "not_testable", implementation


def test_a_verified_contract_is_tested_from_a_usable_return_reconciliation() -> None:
    usable = {"status": "TESTED_EXACT_COMMON_DATES", "observations": 12}
    for implementation in (
        {"return_reconciliation": usable},
        {"account_basis": {"return_reconciliation": usable}},
    ):
        row = _merge(
            {
                "implementation_reconciliation_status": "VERIFIED_CAUSAL_RETURN_ATTRIBUTION",
                "implementation_reconciliation": implementation,
            },
            {"status": "UNAVAILABLE"},
            None,
        )
        assert row["implementation_gap_status"] == "tested", implementation


def test_only_the_emitted_verified_statuses_count_as_verified() -> None:
    usable = {"status": "TESTED_EXACT_COMMON_DATES", "observations": 12}
    for status, expected in (
        ("VERIFIED_CAUSAL_RETURN_ATTRIBUTION", "tested"),
        ("VERIFIED_TRADE_AND_RETURN_DECONSTRUCTION", "tested"),
        ("VERIFIED_SOMETHING_ELSE", "not_testable"),
        ("VERIFIED", "not_testable"),
    ):
        row = _merge(
            {
                "implementation_reconciliation_status": status,
                "implementation_reconciliation": {"return_reconciliation": usable},
            },
            {"status": "UNAVAILABLE"},
            None,
        )
        assert row["implementation_gap_status"] == expected, status


def test_a_sleeve_does_not_borrow_the_parent_account_reconciliation() -> None:
    usable = {"status": "TESTED_EXACT_COMMON_DATES", "observations": 207}
    row = _merge(
        {
            "implementation_reconciliation_status": "VERIFIED_CAUSAL_RETURN_ATTRIBUTION",
            "implementation_reconciliation": {
                "return_reconciliation": {"status": "UNAVAILABLE_NO_COMMON_DATES"},
                "account_basis": {"return_reconciliation": usable},
            },
        },
        {"status": "UNAVAILABLE"},
        None,
    )

    assert row["implementation_gap_status"] == "not_testable"


def test_a_failed_return_reconciliation_with_observations_is_not_tested() -> None:
    row = _merge(
        {
            "implementation_reconciliation_status": "VERIFIED_BROKEN",
            "implementation_reconciliation": {
                "return_reconciliation": {"status": "FAILED", "observations": 1}
            },
        },
        {"status": "UNAVAILABLE"},
        None,
    )

    assert row["implementation_gap_status"] == "not_testable"


def test_only_the_enumerated_exact_date_comparison_statuses_are_tested() -> None:
    for status, expected in (
        ("DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE", "tested"),
        ("DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED", "tested"),
        ("DESCRIPTIVE_EXACT_DATE_ANYTHING", "not_testable"),
    ):
        row = _merge(
            {"implementation_reconciliation_status": "ACCEPTED_ACCOUNT_LIVE"},
            {"status": status},
            {"observations": 30},
        )
        assert row["implementation_gap_status"] == expected, status

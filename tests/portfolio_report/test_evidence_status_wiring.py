"""#901 (a): cost and benchmark evidence statuses read the evidence the report holds.

The two fields used to read ``strategy_version_catalog[*].costs`` and ``.benchmark``,
slots that say "deferred to PORT-ATTRIB-002" and were never updated, while the same
row showed benchmarks assigned and fees evidenced.  The fields now derive from the
row's own ``fees_status`` and its benchmark assignment and comparison statuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


import portfolio_report as reporter  # noqa: E402
from acceptance.validate import checks as validate_checks  # noqa: E402


def _merge(cost: dict[str, Any], assignment: str, comparison: str) -> dict[str, Any]:
    sid = "tt-portfolio"
    return reporter._merge_attribution(  # noqa: SLF001
        [{"strategy_id": sid}],
        {
            "benchmark_assignments": [{"strategy_id": sid, "status": assignment}],
            "benchmark_comparisons": [{"strategy_id": sid, "status": comparison}],
            "comparisons": [{"strategy_id": sid}],
            "costs": [{"strategy_id": sid, **cost}],
        },
    )[0]


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        (
            {"fees_status": "EVIDENCED_PRE_TAX", "annual_external_cost_usd": "798"},
            "EVIDENCED",
        ),
        (
            {"fees_status": "DOCUMENTED_RATE_NOT_DOLLAR"},
            "EVIDENCED_DOCUMENTED_RATE_NOT_DOLLAR",
        ),
        (
            {"fees_status": "UPPER_BOUND_CONTRACT_CAP_PRE_TAX"},
            "UNAVAILABLE_UPPER_BOUND_ONLY",
        ),
        ({"fees_status": "UNAVAILABLE"}, "UNAVAILABLE"),
        ({}, "UNAVAILABLE"),
    ],
)
def test_cost_evidence_follows_the_rows_fees_status(
    cost: dict[str, Any], expected: str
) -> None:
    row = _merge(cost, "EVIDENCED", "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE")
    assert row["cost_evidence_status"] == expected


@pytest.mark.parametrize(
    ("assignment", "comparison", "expected"),
    [
        ("EVIDENCED", "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE", "EVIDENCED"),
        ("EVIDENCED", "UNAVAILABLE", "UNAVAILABLE_COMPARISON_NOT_ADMITTED"),
        ("UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT", "UNAVAILABLE", "UNAVAILABLE"),
        (
            "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT",
            "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
            "UNAVAILABLE",
        ),
    ],
)
def test_benchmark_evidence_needs_assignment_and_comparison(
    assignment: str, comparison: str, expected: str
) -> None:
    row = _merge({}, assignment, comparison)
    assert row["benchmark_evidence_status"] == expected


def test_the_version_catalog_placeholder_slots_no_longer_decide() -> None:
    """Kill test: a dead 'deferred' slot cannot make an evidenced row unavailable."""

    row = _merge(
        {"fees_status": "EVIDENCED_PRE_TAX", "annual_external_cost_usd": "1"},
        "EVIDENCED",
        "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
    )
    row_with_dead_slots = reporter._merge_attribution(  # noqa: SLF001
        [
            {
                "strategy_id": "tt-portfolio",
                "cost_evidence_status": "UNAVAILABLE",
                "benchmark_evidence_status": "UNAVAILABLE",
            }
        ],
        {
            "benchmark_assignments": [
                {"strategy_id": "tt-portfolio", "status": "EVIDENCED"}
            ],
            "benchmark_comparisons": [
                {
                    "strategy_id": "tt-portfolio",
                    "status": "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
                }
            ],
            "comparisons": [{"strategy_id": "tt-portfolio"}],
            "costs": [
                {
                    "strategy_id": "tt-portfolio",
                    "fees_status": "EVIDENCED_PRE_TAX",
                    "annual_external_cost_usd": "1",
                }
            ],
        },
    )[0]
    assert row == row_with_dead_slots
    assert validate_checks.is_available(row["cost_evidence_status"])
    assert validate_checks.is_available(row["benchmark_evidence_status"])


def test_the_validator_accepts_exactly_the_values_the_producer_emits_when_evidenced() -> (
    None
):
    emitted = {
        reporter.cost_evidence_status_for_fees("EVIDENCED_PRE_TAX"),
        reporter.cost_evidence_status_for_fees("DOCUMENTED_RATE_NOT_DOLLAR"),
        reporter.benchmark_evidence_status_for(
            "EVIDENCED", "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE"
        ),
    }
    assert all(validate_checks.is_available(value) for value in emitted)
    unavailable = {
        reporter.cost_evidence_status_for_fees("UPPER_BOUND_CONTRACT_CAP_PRE_TAX"),
        reporter.cost_evidence_status_for_fees("UNAVAILABLE"),
        reporter.benchmark_evidence_status_for("EVIDENCED", "UNAVAILABLE"),
        reporter.benchmark_evidence_status_for("UNAVAILABLE", None),
    }
    assert not any(validate_checks.is_available(value) for value in unavailable)


def test_only_a_version_compatible_comparison_evidences_a_benchmark() -> None:
    """Kill test: prefix matching admitted any DESCRIPTIVE*/EVIDENCED* string."""

    import portfolio_attribution

    assert reporter._ADMITTED_BENCHMARK_COMPARISONS <= frozenset(  # noqa: SLF001
        portfolio_attribution.AVAILABLE_STATUSES
    )
    for assignment, comparison in (
        ("EVIDENCED", "DESCRIPTIVE_NO_METRICS"),
        ("EVIDENCED", "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"),
        ("EVIDENCED_BUT_NOT", "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE"),
    ):
        assert reporter.benchmark_evidence_status_for(assignment, comparison) != (
            "EVIDENCED"
        )


def test_a_row_without_attribution_reads_unavailable_not_absent() -> None:
    """Gemini finding 1: absent keys silently bypassed the E5/E6 validator rows."""

    source = (ROOT / "tools" / "portfolio_report.py").read_text(encoding="utf-8")
    assert '"cost_evidence_status": EVIDENCE_STATUS_UNAVAILABLE' in source
    assert '"benchmark_evidence_status": EVIDENCE_STATUS_UNAVAILABLE' in source


def test_a_rejected_lineage_withdraws_the_evidence_statuses() -> None:
    import portfolio_control_panel as panel

    row = {
        "strategy_id": "tt-portfolio",
        "cost_evidence_status": "EVIDENCED",
        "benchmark_evidence_status": "EVIDENCED",
    }
    (out,) = panel._suppress_unmatched_strategy_performance([row])  # noqa: SLF001
    assert out["cost_evidence_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"
    assert out["benchmark_evidence_status"] == "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH"

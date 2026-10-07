"""The closed report-data schema accepts what the report builder emits for the
sections this branch changed.

The schema is not enforced on the real path (``portfolio_report`` never validates its
own output against it), so it can only stay the contract if a test holds the producer
to it.  Each case builds the section with the real producer function and validates it
against the shipped schema; a section the producer started to emit and the schema does
not admit fails here instead of at the first consumer that does validate.
"""

from __future__ import annotations

import json
from pathlib import Path
import runpy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import portfolio_report as reporter

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (REPOSITORY_ROOT / "schemas" / "portfolio" / "report-data.schema.json").read_text(
        encoding="utf-8"
    )
)
# Validate one top-level section at a time: the root keeps its closed property list,
# but a fragment need not carry the twenty-odd required sections.
SECTION_SCHEMA = {**SCHEMA, "required": []}
BOUNDS_FIXTURE = runpy.run_path(
    str(
        REPOSITORY_ROOT
        / "tests"
        / "portfolio_report"
        / "test_value_basis_bound_consumers.py"
    )
)


BASELINE_FIXTURE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_baseline" / "test_portfolio_baseline.py")
)


def _errors(section: str, value: Any) -> list[str]:
    validator = Draft202012Validator(SECTION_SCHEMA, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message[:120]}"
        for error in validator.iter_errors({section: value})
    ]


def test_value_basis_bounds_with_current_capital_entries_is_schema_valid() -> None:
    block = BOUNDS_FIXTURE["_disclosure"](BOUNDS_FIXTURE["_custody_report"]())
    assert block is not None
    sections = {row["section"] for row in block["bounded_sections"]}
    assert "current_capital" in sections

    assert _errors("value_basis_bounds", block) == []


def test_value_basis_bounds_schema_is_closed_over_the_producers_sections() -> None:
    block = BOUNDS_FIXTURE["_disclosure"](BOUNDS_FIXTURE["_custody_report"]())
    assert block is not None
    forged = json.loads(json.dumps(block))
    forged["bounded_sections"][0]["section"] = "not_a_report_section"

    assert _errors("value_basis_bounds", forged)
    unexpected = {**block, "note": "x"}
    assert _errors("value_basis_bounds", unexpected)


def _attribution_rows(kind: str) -> list[dict[str, str]]:
    return [{"strategy_id": f"strategy-{index}", "kind": kind} for index in range(7)]


def test_strategy_attribution_with_benchmark_comparisons_is_schema_valid() -> None:
    controls = {
        f"strategy-{index}": {"product_requirement": "REQUIRED"} for index in range(7)
    }
    catalog = {
        "benchmark_assignments": _attribution_rows("assignment"),
        "benchmark_comparisons": _attribution_rows("benchmark"),
        "comparisons": _attribution_rows("comparison"),
        "costs": _attribution_rows("cost"),
    }

    section = reporter._strategy_attribution_section(
        catalog, {"generation_id": "a" * 64}, controls
    )

    assert len(section["benchmark_comparisons"]) == 7
    assert _errors("strategy_attribution", section) == []
    # An older catalog without the list publishes an empty one, which also validates.
    del catalog["benchmark_comparisons"]
    older = reporter._strategy_attribution_section(
        catalog, {"generation_id": "a" * 64}, controls
    )
    assert older["benchmark_comparisons"] == []
    assert _errors("strategy_attribution", older) == []


def test_account_roles_the_report_labels_are_typed_in_the_schema() -> None:
    rows = reporter._label_account_roles(
        [
            {"account_id": "account-product"},
            {"account_id": "account-reserve"},
            {"account_id": "account-history"},
            {"account_id": None},
        ],
        product_account_ids={"account-product"},
        reserve_account_ids={"account-reserve"},
        excluded_account_ids=set(),
    )
    assert len({row["account_role"] for row in rows}) == 4
    assert _errors("fact_coverage_matrix", rows) == []
    assert _errors("fact_coverage_matrix", [{"account_role": 7}])

    leverage_role = Draft202012Validator(
        {
            "$defs": SCHEMA["$defs"],
            **SCHEMA["$defs"]["leverage_disclosure"]["properties"]["account_role"],
        }
    )
    for row in rows:
        assert list(leverage_role.iter_errors(row["account_role"])) == []
    assert list(leverage_role.iter_errors(7))


def _coverage_block() -> dict[str, Any]:
    """The coverage disclosure of a custody block the real baseline producer built:
    the sleeve-allocation admission ends the day before the cutoff."""

    case = BASELINE_FIXTURE["_cr2_custody_cash_case"]()
    BASELINE_FIXTURE["_admission_ends"](case, flows="2026-09-18", anchors="2026-09-16")
    custody = BASELINE_FIXTURE["baseline"]._build_current_capital(**case)
    block = reporter._capital_coverage_bounds(custody, "2026-09-17")
    assert block is not None
    return block


def test_capital_coverage_bounds_is_schema_valid() -> None:
    block = _coverage_block()

    assert [row["entity"] for row in block["bounded_sections"]] == [
        "alpha",
        "alpha:acct-a1",
        "systematic",
    ]
    assert _errors("capital_coverage_bounds", block) == []


def test_capital_coverage_bounds_schema_is_closed() -> None:
    block = _coverage_block()
    forged = json.loads(json.dumps(block))
    forged["bounded_sections"][0]["section"] = "account_summary"

    assert _errors("capital_coverage_bounds", forged)
    assert _errors("capital_coverage_bounds", {**block, "note": "x"})
    missing = json.loads(json.dumps(block))
    del missing["lines"][0]["coverage_end"]
    assert _errors("capital_coverage_bounds", missing)


def _versioned_errors(version: int, **sections: Any) -> list[str]:
    validator = Draft202012Validator(SECTION_SCHEMA, format_checker=FormatChecker())
    document = {"report_schema_version": version, **sections}
    return [error.message[:120] for error in validator.iter_errors(document)]


def test_capital_coverage_bounds_exists_only_from_report_schema_version_3() -> None:
    """N56: the block is a version-3 addition like the other N56 report fields; a
    version-1 or version-2 report that carries it is not a valid report."""

    block = _coverage_block()

    assert _versioned_errors(3, capital_coverage_bounds=block) == []
    assert _versioned_errors(4, capital_coverage_bounds=block) == []
    assert _versioned_errors(3) == []
    for version in (1, 2):
        assert _versioned_errors(version) == []
        assert _versioned_errors(version, capital_coverage_bounds=block), version


def test_report_schema_enumerates_version_four_and_no_other() -> None:
    """QC-1: version 4 is declared, version 5 is not, and the older versions stay."""

    versions = SCHEMA["properties"]["report_schema_version"]["enum"]
    assert versions == [1, 2, 3, 4]
    assert reporter.CUTOFF_BASIS_REPORT_SCHEMA_VERSION == 4
    assert reporter.CUSTODY_COVERAGE_REPORT_SCHEMA_VERSION == 3
    assert _versioned_errors(4) == []
    assert _versioned_errors(5)


def test_version_two_weight_label_rules_cover_version_four() -> None:
    """The Correlation-one stress label rule applies to every version from 2."""

    unlabeled = [{"metric": "Correlation-one stress", "value": "0.37"}]

    def label_errors(version: int) -> list[str]:
        return [
            message
            for message in _versioned_errors(version, portfolio_summary=unlabeled)
            if "weight_basis" in message
        ]

    for version in (2, 3, 4):
        assert label_errors(version), version
    assert label_errors(1) == []


def test_the_custody_cash_shape_of_version_three_is_required_at_version_four() -> None:
    """A version-4 report keeps the version-3 custody cash fields mandatory."""

    shape = SCHEMA["allOf"][-2]
    assert shape["if"]["properties"]["report_schema_version"]["enum"] == [3, 4]
    assert shape["then"]["properties"]["custody_current_capital"]["$ref"] == (
        "#/$defs/custody_handoff_current_capital_v3"
    )

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

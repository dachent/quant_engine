"""N56: a cash line dated by its coverage end is NOT_READY, never a blocker, when the
report discloses the coverage bound; and never a pass.

The Matrixport sleeve-allocation admission ends at the last subscription, so the real
robuxio-crypto cash line is honestly dated before the cutoff.  That is not an FX
value-basis end, so ``value_basis_bounds`` is absent and the validator could only call
it a blocker.  The report states the bound in ``capital_coverage_bounds``; the
validator downgrades a date that the block, the report's own cash line and the oracle
agree on.  Custody blocks here are built by the real baseline producer and the
disclosure by the real report producer; nothing in the chain is handwritten.
"""

from __future__ import annotations

import copy
from pathlib import Path
import runpy
from typing import Any

from test_n56_capital_labels import _custody, _member
from test_validate_alignment import (
    REPOSITORY_ROOT,
    _load_report_tool,
    _run,
    _strategy_cutoff,
)
from test_validate_generation import CUTOFF, Spec, build

from acceptance.validate import checks

CHECK = "capital.as_of_cutoff"
DAY = "2026-09-16"
BASELINE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_baseline" / "test_portfolio_baseline.py")
)


def _custody_block(anchors_end: str = DAY) -> dict[str, Any]:
    """The custody block the real baseline producer builds when the sleeve-allocation
    admission ends on ``anchors_end`` while the flows are admitted through the 18th."""

    case = BASELINE["_cr2_custody_cash_case"]()
    BASELINE["_admission_ends"](case, flows="2026-09-18", anchors=anchors_end)
    block: dict[str, Any] = BASELINE["baseline"]._build_current_capital(**case)
    return block


def _disclosure(custody: dict[str, Any]) -> dict[str, Any]:
    block = _load_report_tool()._capital_coverage_bounds(custody, CUTOFF)
    assert block is not None
    return block


def _expected(value: str = "109", as_of: str | None = CUTOFF) -> list[dict[str, Any]]:
    return [_strategy_cutoff("alpha", as_of=as_of, value=value)]


def _findings(
    tmp_path: Path,
    custody: dict[str, Any],
    *,
    block: dict[str, Any] | None,
    expected: list[dict[str, Any]] | None,
    check: str = CHECK,
) -> list[checks.Finding]:
    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = 3
        report["custody_current_capital"] = copy.deepcopy(custody)
        if block is not None:
            report["capital_coverage_bounds"] = copy.deepcopy(block)

    gen_dir = build(tmp_path, Spec(report=[mutate]))
    return [f for f in _run(gen_dir, expected) if f.check_id == check]


def _view(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return sorted((f.entity, f.klass) for f in findings)


LINE = "current_capital:alpha:cash:acct-a1"
ENTITIES = ("current_capital:alpha", LINE, "current_capital:systematic")


def _expect(klass: str) -> list[tuple[str, str]]:
    return [(entity, klass) for entity in ENTITIES]


def test_the_producers_own_early_world_is_honestly_dated() -> None:
    """Precondition: the line, its row and the total are the 16th, the member the 17th."""

    custody = _custody_block()
    line = custody["strategies"][0]["derived_cash"][0]
    assert (line["as_of_date"], line["coverage_end"]) == (DAY, DAY)
    assert custody["strategies"][0]["members"][0]["as_of_date"] == CUTOFF
    assert custody["strategies"][0]["as_of_date"] == DAY
    assert custody["systematic"]["as_of_date"] == DAY


def test_a_disclosed_coverage_bound_downgrades_the_line_row_and_total(
    tmp_path: Path,
) -> None:
    custody = _custody_block()

    findings = _findings(
        tmp_path, custody, block=_disclosure(custody), expected=_expected()
    )

    assert _view(findings) == _expect(checks.NOT_READY)
    assert all("coverage" in f.message for f in findings)


def test_an_undisclosed_coverage_date_is_still_a_blocker(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _custody_block(), block=None, expected=_expected())

    assert _view(findings) == _expect(checks.BLOCKER_AUTHORITY)


def test_disclosure_without_an_oracle_never_downgrades(tmp_path: Path) -> None:
    custody = _custody_block()

    findings = _findings(tmp_path, custody, block=_disclosure(custody), expected=None)

    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_an_oracle_that_dates_the_strategy_before_the_label_does_not_confirm(
    tmp_path: Path,
) -> None:
    """The label may err old, never new: an oracle date older than the label is not
    a confirmation of it."""

    custody = _custody_block()

    findings = _findings(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(as_of="2026-09-10"),
    )

    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_a_disclosure_that_disagrees_with_the_report_line_does_not_downgrade(
    tmp_path: Path,
) -> None:
    """The block says the coverage ended the 10th; the line it describes says the 16th."""

    custody = _custody_block()
    block = _disclosure(custody)
    block["lines"][0]["coverage_end"] = "2026-09-10"
    block["lines"][0]["as_of_date"] = "2026-09-10"
    for entry in block["bounded_sections"]:
        entry["date"] = "2026-09-10"

    findings = _findings(tmp_path, custody, block=block, expected=_expected())

    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}
    problems = _findings(
        tmp_path / "problems",
        custody,
        block=block,
        expected=_expected(),
        check="capital.coverage_bound",
    )
    assert problems
    assert {f.klass for f in problems} == {checks.BLOCKER_INVARIANT}


def test_a_block_that_states_a_cash_line_the_report_does_not_carry_downgrades_nothing(
    tmp_path: Path,
) -> None:
    """The entries are explained by the stated lines, and one stated line is invented:
    the block is not borne out by the report, so none of it is believed."""

    custody = _custody_block()
    block = _disclosure(custody)
    block["lines"].append({**block["lines"][0], "account_id": "invented-account"})

    findings = _findings(tmp_path, custody, block=block, expected=_expected())

    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}
    problems = _findings(
        tmp_path / "problems",
        custody,
        block=block,
        expected=_expected(),
        check="capital.coverage_bound",
    )
    assert [f.klass for f in problems] == [checks.BLOCKER_INVARIANT]
    assert "not a cash line of the report" in problems[0].observed


def test_a_section_the_lines_do_not_explain_is_not_downgraded(tmp_path: Path) -> None:
    """The block names the row at a date the strategy's oldest line does not have."""

    custody = _custody_block()
    block = _disclosure(custody)
    row = next(e for e in block["bounded_sections"] if e["entity"] == "alpha")
    row["date"] = "2026-09-12"

    findings = _findings(tmp_path, custody, block=block, expected=_expected())

    by_entity = dict(_view(findings))
    assert by_entity["current_capital:alpha"] == checks.BLOCKER_AUTHORITY
    assert by_entity[LINE] == checks.NOT_READY


def test_a_dishonest_label_is_not_downgraded_even_if_disclosed(
    tmp_path: Path,
) -> None:
    custody = _custody_block()
    block = _disclosure(custody)
    line = custody["strategies"][0]["derived_cash"][0]
    line["coverage_end"] = "2026-09-18"  # the label no longer equals min(cutoff, end)

    findings = _findings(tmp_path, custody, block=block, expected=_expected())

    assert checks.NOT_READY not in {f.klass for f in findings}


def test_a_full_coverage_line_needs_no_disclosure(tmp_path: Path) -> None:
    custody = _custody_block(anchors_end="2026-09-18")

    findings = _findings(tmp_path, custody, block=None, expected=_expected())

    assert findings == []


STRATEGY_CASH = "expected.capital.strategy_cash.cutoff"


def _cash_expectation(value: str = "4", as_of: str = CUTOFF) -> dict[str, Any]:
    return {
        "metric_id": "capital.strategy_cash.cutoff",
        "entity": "alpha",
        "expected": value,
        "as_of": as_of,
    }


def test_the_oracle_cash_row_dated_differently_follows_the_disclosure(
    tmp_path: Path,
) -> None:
    """Value matches the oracle; the date is the line's own coverage end.  One cause is
    one class: NOT_READY when disclosed, the regression blocker when it is not."""

    custody = _custody_block()
    expected = [*_expected(), _cash_expectation()]

    disclosed = _findings(
        tmp_path / "disclosed",
        custody,
        block=_disclosure(custody),
        expected=expected,
        check=STRATEGY_CASH,
    )
    undisclosed = _findings(
        tmp_path / "undisclosed",
        custody,
        block=None,
        expected=expected,
        check=STRATEGY_CASH,
    )

    assert [(f.entity, f.klass) for f in disclosed] == [
        ("alpha|2026-09-17", checks.NOT_READY)
    ]
    assert disclosed[0].observed["as_of"] == DAY
    assert [(f.entity, f.klass) for f in undisclosed] == [
        ("alpha|2026-09-17", checks.BLOCKER_REGRESSION)
    ]


def test_the_oracle_cash_row_still_blocks_when_the_value_differs(
    tmp_path: Path,
) -> None:
    custody = _custody_block()

    findings = _findings(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[*_expected(), _cash_expectation(value="5")],
        check=STRATEGY_CASH,
    )

    assert [f.klass for f in findings] == [checks.BLOCKER_REGRESSION]


def test_a_cash_label_newer_than_the_oracle_date_still_blocks(tmp_path: Path) -> None:
    """The downgrade is for a label that errs old; a newer one claims too much."""

    custody = _custody_block()

    findings = _findings(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[*_expected(), _cash_expectation(as_of="2026-09-10")],
        check=STRATEGY_CASH,
    )

    assert [f.klass for f in findings] == [checks.BLOCKER_REGRESSION]


ACCOUNT = "expected.capital.account.cutoff"


def _account_findings(tmp_path: Path, scope: str) -> list[checks.Finding]:
    world = _custody(member=_member("2025-11-24"), scope=scope, row="2025-11-24")
    gen_dir = build(tmp_path, Spec(report=[world]))
    expected = [
        {
            "metric_id": "capital.account.cutoff",
            "entity": "M1",
            "expected": "5500",
            "as_of": "2026-07-17",
        }
    ]
    return [f for f in _run(gen_dir, expected) if f.check_id == ACCOUNT]


def test_a_retained_excluded_member_dated_before_the_oracle_is_not_ready(
    tmp_path: Path,
) -> None:
    """N59: a retained-excluded account's NAV is dated by its last admitted observation
    (the oracle reads later, unmapped reference rows).  The value matches, so the date
    is a NOT_READY disclosure, not a regression of the product."""

    findings = _account_findings(tmp_path, "RETAINED_EXCLUDED")

    assert [(f.entity, f.klass) for f in findings] == [
        ("M1|2026-07-17", checks.NOT_READY)
    ]
    assert "RETAINED_EXCLUDED" in findings[0].message


def test_a_systematic_member_dated_before_the_oracle_still_blocks(
    tmp_path: Path,
) -> None:
    findings = _account_findings(tmp_path, "SYSTEMATIC")

    assert [f.klass for f in findings] == [checks.BLOCKER_REGRESSION]

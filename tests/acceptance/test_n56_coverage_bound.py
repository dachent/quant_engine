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
from acceptance.validate.generation import load_generation

CHECK = "capital.as_of_cutoff"
DAY = "2026-09-16"
BASELINE = runpy.run_path(
    str(REPOSITORY_ROOT / "tests" / "portfolio_baseline" / "test_portfolio_baseline.py")
)


def _custody_block(anchors_end: str = DAY, *, nav: bool = True) -> dict[str, Any]:
    """The custody block the real baseline producer builds when the sleeve-allocation
    admission ends on ``anchors_end`` while the flows are admitted through the 18th.

    ``nav=False`` removes the NAV records, which the real producer answers with a
    NOT_READY_AT_CUTOFF strategy row and Systematic total around an AVAILABLE cash line."""

    case = BASELINE["_cr2_custody_cash_case"]()
    BASELINE["_admission_ends"](case, flows="2026-09-18", anchors=anchors_end)
    if not nav:
        case["nav"] = []
    block: dict[str, Any] = BASELINE["baseline"]._build_current_capital(**case)
    return block


def _disclosure(custody: dict[str, Any]) -> dict[str, Any]:
    block = _load_report_tool()._capital_coverage_bounds(custody, CUTOFF)
    assert block is not None
    return block


COVERAGE_ROW = "alpha|synthetic|acct-a1"


def _coverage_row(
    end: str | None = DAY, entity: str = COVERAGE_ROW, as_of: str = CUTOFF
) -> dict[str, Any]:
    """The oracle's ``date.strategy_cash.coverage_end`` row (ruling C1)."""

    return {
        "metric_id": "date.strategy_cash.coverage_end",
        "entity": entity,
        "expected": end,
        "value": end,
        "as_of": as_of,
        "unit": "date",
    }


def _expected(
    value: str = "109",
    as_of: str | None = CUTOFF,
    coverage: str | None = DAY,
) -> list[dict[str, Any]]:
    """The oracle rows: the strategy cutoff row, and the coverage end of the cash line
    unless ``coverage`` is ``None`` (the oracle as published before the C1 row)."""

    rows = [_strategy_cutoff("alpha", as_of=as_of, value=value)]
    if coverage is not None:
        rows.append(_coverage_row(coverage))
    return rows


def _findings(
    tmp_path: Path,
    custody: dict[str, Any],
    *,
    block: dict[str, Any] | None,
    expected: list[dict[str, Any]] | None,
    check: str = CHECK,
    version: int = 3,
) -> list[checks.Finding]:
    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = version
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


def test_an_oracle_that_dates_the_coverage_before_the_label_does_not_confirm(
    tmp_path: Path,
) -> None:
    """The label may err old, never new: a coverage end the oracle states older than
    the label is not a confirmation of it."""

    custody = _custody_block()

    findings = _findings(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(coverage="2026-09-10"),
    )

    # Nothing is downgraded.  The line's own label (the 16th) also fails the recompute
    # against the oracle's coverage end (the 10th), an invariant on the line only.
    assert {f.klass for f in findings} == {
        checks.BLOCKER_AUTHORITY,
        checks.BLOCKER_INVARIANT,
    }
    assert [f.entity for f in findings if f.klass == checks.BLOCKER_INVARIANT] == [LINE]


def test_the_strategy_cutoff_date_is_not_the_anchor_of_the_coverage_date(
    tmp_path: Path,
) -> None:
    """The oracle's capital.strategy.cutoff date is the cutoff by construction and says
    nothing about how far a cash admission reaches: with the coverage row confirming the
    label, the strategy row's own date no longer decides."""

    custody = _custody_block()

    findings = _findings(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(as_of="2026-09-10"),
    )

    assert _view(findings) == _expect(checks.NOT_READY)


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

    # Round 7 (K1): this test used to assert ``by_entity[LINE] == NOT_READY``, which kept
    # the line's downgrade while the block carried an entry its own lines do not explain.
    # Ruling C3 reverses that: any invalid entry voids the WHOLE block, so no date is
    # downgraded anywhere in the report.
    assert dict(_view(findings)) == {
        entity: checks.BLOCKER_AUTHORITY for entity in ENTITIES
    }


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

    findings = _findings(
        tmp_path, custody, block=None, expected=_expected(coverage="2026-09-18")
    )

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


# ------------------------------------------------ round 6: the oracle anchors the date
#
# The coverage downgrade used to check the report against itself: the oracle's
# capital.strategy.cutoff is dated at the cutoff by construction, so it confirmed any
# earlier claimed coverage end.  The oracle now states the proven-through date of each
# custody cash line (date.strategy_cash.coverage_end), the validator compares the
# report's coverage end with it in both directions, and the downgrade needs it.

COVERAGE_END = "expected.capital.strategy_cash.coverage_end"
BOUND = "capital.coverage_bound"
MEMBER = "current_capital:alpha:member:acct-a1"


def _world(
    tmp_path: Path,
    custody: dict[str, Any],
    *,
    block: dict[str, Any] | None,
    expected: list[dict[str, Any]] | None,
    version: int = 3,
) -> dict[str, list[tuple[str, str]]]:
    """Every finding of the validator for this world, as (entity, class) by check id."""

    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = version
        report["custody_current_capital"] = copy.deepcopy(custody)
        if block is not None:
            report["capital_coverage_bounds"] = copy.deepcopy(block)

    gen_dir = build(tmp_path, Spec(report=[mutate]))
    out: dict[str, list[tuple[str, str]]] = {}
    for finding in _run(gen_dir, expected):
        out.setdefault(finding.check_id, []).append((finding.entity, finding.klass))
    return {key: sorted(value) for key, value in out.items()}


def test_the_honest_world_has_no_coverage_end_or_disclosure_finding(
    tmp_path: Path,
) -> None:
    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[*_expected(), _cash_expectation()],
    )

    assert sorted(seen[CHECK]) == sorted(_expect(checks.NOT_READY))
    assert COVERAGE_END not in seen
    assert BOUND not in seen
    assert seen[STRATEGY_CASH] == [("alpha|2026-09-17", checks.NOT_READY)]


def test_a_coverage_end_the_oracle_dates_earlier_is_not_believed(
    tmp_path: Path,
) -> None:
    """Astra's replay: the report claims coverage through the 16th and the producer's own
    block says so; the oracle proves it only through the 10th.  The old oracle clause
    (capital.strategy.cutoff dated on or after the label) confirmed the claim."""

    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[*_expected(coverage="2026-09-10"), _cash_expectation()],
    )

    # The label recompute reads the oracle's coverage end, so the line's own label (the
    # 16th) is also an invariant breach against the oracle's 10th.
    assert set(seen[CHECK]) == {
        *_expect(checks.BLOCKER_AUTHORITY),
        (LINE, checks.BLOCKER_INVARIANT),
    }
    assert seen[COVERAGE_END] == [
        ("alpha|synthetic|acct-a1", checks.BLOCKER_REGRESSION)
    ]
    assert seen[STRATEGY_CASH] == [("alpha|2026-09-17", checks.BLOCKER_REGRESSION)]
    assert {klass for _, klass in seen[BOUND]} == {checks.BLOCKER_INVARIANT}


def test_a_coverage_end_at_or_after_the_cutoff_is_checked_too(tmp_path: Path) -> None:
    """The seat's repro: coverage forged to the 18th with every label at the cutoff
    yielded no finding at all, because the label recompute read the coverage end from
    the report itself.  The oracle row says the 16th."""

    custody = _custody_block(anchors_end="2026-09-18")
    honest = _custody_block()

    for name, block in (("without", None), ("with", _disclosure(honest))):
        seen = _world(
            tmp_path / name,
            custody,
            block=block,
            expected=_expected(coverage="2026-09-16"),
        )

        assert seen[COVERAGE_END] == [
            ("alpha|synthetic|acct-a1", checks.BLOCKER_REGRESSION)
        ], name
        # The label recompute does not read the coverage end from the report: the
        # line is dated at the cutoff while the oracle proves it only to the 16th.
        assert (LINE, checks.BLOCKER_INVARIANT) in seen[CHECK], name


def test_a_cutoff_coverage_end_the_oracle_confirms_is_silent(tmp_path: Path) -> None:
    custody = _custody_block(anchors_end="2026-09-18")

    seen = _world(
        tmp_path,
        custody,
        block=None,
        expected=_expected(coverage="2026-09-18"),
    )

    assert COVERAGE_END not in seen
    assert CHECK not in seen


def test_an_oracle_with_no_coverage_end_row_confirms_nothing(tmp_path: Path) -> None:
    """The oracle as published before the row existed: the label claims a proof the
    independent instrument does not give."""

    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(coverage=None),
    )

    assert seen[COVERAGE_END] == [("alpha|synthetic|acct-a1", checks.BLOCKER_AUTHORITY)]
    assert sorted(seen[CHECK]) == sorted(_expect(checks.BLOCKER_AUTHORITY))


def test_a_not_derivable_coverage_end_confirms_nothing(tmp_path: Path) -> None:
    custody = _custody_block()

    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = 3
        report["custody_current_capital"] = copy.deepcopy(custody)
        report["capital_coverage_bounds"] = _disclosure(custody)

    gen_dir = build(tmp_path, Spec(report=[mutate]))
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=_expected(coverage=None),
        expected_not_derivable=[
            {
                "metric_id": "date.strategy_cash.coverage_end",
                "entity": COVERAGE_ROW,
                "reason": "the two derivations disagree",
            }
        ],
    )
    found = {
        (f.check_id, f.klass)
        for f in checks.check_capital_coverage_end(context)
        if f.check_id == COVERAGE_END
    }

    assert found == {(COVERAGE_END, checks.BLOCKER_AUTHORITY)}


def test_an_oracle_row_for_another_provider_is_a_regression(tmp_path: Path) -> None:
    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[
            *_expected(coverage=None),
            _coverage_row(entity="alpha|ibkr|acct-a1"),
        ],
    )

    assert seen[COVERAGE_END] == [
        ("alpha|synthetic|acct-a1", checks.BLOCKER_REGRESSION)
    ]
    assert sorted(seen[CHECK]) == sorted(_expect(checks.BLOCKER_AUTHORITY))


def test_two_oracle_rows_that_disagree_confirm_nothing(tmp_path: Path) -> None:
    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[*_expected(), _coverage_row("2026-09-10")],
    )

    assert seen[COVERAGE_END] == [("alpha|synthetic|acct-a1", checks.BLOCKER_AUTHORITY)]
    assert sorted(seen[CHECK]) == sorted(_expect(checks.BLOCKER_AUTHORITY))


def test_a_nav_member_is_never_dated_by_the_cash_coverage(tmp_path: Path) -> None:
    """C4: the member shares the cash line's account, and its NAV observation date at
    the cash coverage end is explained by no coverage admission.  The member, its row
    and the Systematic total stay blockers; the cash line itself is still NOT_READY."""

    custody = _custody_block()
    custody["strategies"][0]["members"][0]["as_of_date"] = DAY

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(),
    )

    assert dict(seen[CHECK]) == {
        MEMBER: checks.BLOCKER_AUTHORITY,
        LINE: checks.NOT_READY,
        "current_capital:alpha": checks.BLOCKER_AUTHORITY,
        "current_capital:systematic": checks.BLOCKER_AUTHORITY,
    }


def test_a_row_and_total_need_every_cash_line_at_that_date_verified(
    tmp_path: Path,
) -> None:
    """A second cash line at the same date that the oracle does not attest keeps the
    row and the Systematic total blockers, though the first line is NOT_READY."""

    custody = _custody_block()
    second = copy.deepcopy(custody["strategies"][0]["derived_cash"][0])
    second["account_id"] = "acct-a2"
    custody["strategies"][0]["derived_cash"].append(second)

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=_expected(),
    )

    # Round 8 (L1): this test used to assert LINE == NOT_READY beside the unproved acct-a2
    # line.  Ruling C3 voids the whole block when any stated line is not the oracle's, so
    # the honest-looking acct-a1 line is a blocker too and nothing is downgraded.
    assert dict(seen[CHECK]) == {
        LINE: checks.BLOCKER_AUTHORITY,
        "current_capital:alpha:cash:acct-a2": checks.BLOCKER_AUTHORITY,
        "current_capital:alpha": checks.BLOCKER_AUTHORITY,
        "current_capital:systematic": checks.BLOCKER_AUTHORITY,
    }
    # Round 7 (K1 minor): this assertion used to say BLOCKER_AUTHORITY.  The oracle names
    # acct-a1 for this strategy and provider and has no row for acct-a2, so the line's
    # account_id differs from the oracle entity, which ruling C2 classes
    # BLOCKER_REGRESSION (a differing provider_id already was).
    assert ("alpha|synthetic|acct-a2", checks.BLOCKER_REGRESSION) in seen[COVERAGE_END]


def _disclosure_variant(name: str) -> dict[str, Any]:
    custody = _custody_block()
    block = _disclosure(custody)
    line, entry = block["lines"][0], block["bounded_sections"][0]
    if name == "provider_id":
        line["provider_id"] = "ibkr"
    elif name == "last_event_date":
        line["last_event_date"] = "2020-01-01"
    elif name == "rule":
        block["rule"] = "N56-COVERAGE-X"
    elif name == "schema_version":
        block["schema_version"] = 2
    elif name == "schema_version_bool":
        block["schema_version"] = True
    elif name == "field":
        for item in block["bounded_sections"]:
            item["field"] = "current_value"
    elif name == "block_key":
        block["note"] = "x"
    elif name == "line_key":
        line["note"] = "x"
    elif name == "section_key":
        entry["note"] = "x"
    elif name == "duplicate_line":
        block["lines"].append(copy.deepcopy(line))
    elif name == "duplicate_section":
        block["bounded_sections"].append(copy.deepcopy(entry))
    else:
        raise AssertionError(name)
    return block


def test_a_false_disclosure_field_voids_every_downgrade(tmp_path: Path) -> None:
    """Astra's four attacks, and the closed-object and uniqueness clauses (C3): any
    field the block states that the report and the oracle do not bear out is a
    BLOCKER_INVARIANT, and no date is downgraded anywhere in the report."""

    custody = _custody_block()
    for name in (
        "provider_id",
        "last_event_date",
        "rule",
        "schema_version",
        "schema_version_bool",
        "field",
        "block_key",
        "line_key",
        "section_key",
        "duplicate_line",
        "duplicate_section",
    ):
        seen = _world(
            tmp_path / name,
            custody,
            block=_disclosure_variant(name),
            expected=[*_expected(), _cash_expectation()],
        )

        assert checks.BLOCKER_INVARIANT in {k for _, k in seen[BOUND]}, name
        assert checks.NOT_READY not in {k for _, k in seen[CHECK]}, name
        assert dict(seen[CHECK]) == {
            entity: checks.BLOCKER_AUTHORITY for entity in ENTITIES
        }, name
        assert seen[STRATEGY_CASH] == [
            ("alpha|2026-09-17", checks.BLOCKER_REGRESSION)
        ], name


def test_a_block_at_a_report_schema_other_than_3_downgrades_nothing(
    tmp_path: Path,
) -> None:
    """C4/G2: the block exists only at report schema version 3; the schema rejects it
    elsewhere and the validator does not believe it."""

    custody = _custody_block()

    for version in (1, 2):
        seen = _world(
            tmp_path / str(version),
            custody,
            block=_disclosure(custody),
            expected=_expected(),
            version=version,
        )

        assert checks.NOT_READY not in {k for _, k in seen.get(CHECK, [])}, version
        assert checks.BLOCKER_INVARIANT in {k for _, k in seen[BOUND]}, version


# ------------------------------------------- round 7: K1 (whole-block void), K2 (parents)


LINE_ENTITY = "alpha:acct-a1"
DAY_DATE = checks.date.fromisoformat(DAY)
BOUND_ENTITY = "capital_coverage_bounds"


def _section_variant(name: str) -> dict[str, Any]:
    custody = _custody_block()
    block = _disclosure(custody)
    row = next(e for e in block["bounded_sections"] if e["entity"] == "alpha")
    line_entry = next(
        e for e in block["bounded_sections"] if e["entity"] == LINE_ENTITY
    )
    if name == "false_date":
        row["date"] = "2026-09-12"
    elif name == "false_section":
        row["section"] = "strategy_summary"
    elif name == "false_bounded_by":
        row["bounded_by"] = ["beta"]
    elif name == "line_entry_false_date":
        line_entry["date"] = "2026-09-12"
    else:
        raise AssertionError(name)
    return block


def test_any_invalid_section_voids_every_downgrade_in_the_report(
    tmp_path: Path,
) -> None:
    """K1 / ruling C3: one invalid bounded_sections entry voids the whole block.  The
    row entry (or the line entry) is false while the OTHER entries are true; no date, and
    not the cash label the oracle compares either, may be downgraded."""

    custody = _custody_block()
    for name in (
        "false_date",
        "false_section",
        "false_bounded_by",
        "line_entry_false_date",
    ):
        seen = _world(
            tmp_path / name,
            custody,
            block=_section_variant(name),
            expected=[*_expected(), _cash_expectation()],
        )

        assert checks.BLOCKER_INVARIANT in {k for _, k in seen[BOUND]}, name
        assert checks.NOT_READY not in {k for _, k in seen[CHECK]}, name
        assert dict(seen[CHECK]) == {
            entity: checks.BLOCKER_AUTHORITY for entity in ENTITIES
        }, name
        assert seen[STRATEGY_CASH] == [
            ("alpha|2026-09-17", checks.BLOCKER_REGRESSION)
        ], name


def test_a_block_dated_at_another_cutoff_voids_every_downgrade(tmp_path: Path) -> None:
    custody = _custody_block()
    block = _disclosure(custody)
    block["calculation_cutoff_date"] = "2026-09-18"

    seen = _world(
        tmp_path,
        custody,
        block=block,
        expected=[*_expected(), _cash_expectation()],
    )

    assert (BOUND_ENTITY, checks.BLOCKER_AUTHORITY) in seen[BOUND]
    assert checks.NOT_READY not in {k for _, k in seen[CHECK]}
    assert seen[STRATEGY_CASH] == [("alpha|2026-09-17", checks.BLOCKER_REGRESSION)]


def _context(
    tmp_path: Path,
    custody: dict[str, Any],
    block: dict[str, Any] | None,
    expected: list[dict[str, Any]] | None = None,
    not_derivable: list[dict[str, Any]] | None = None,
) -> checks.Context:
    def mutate(report: dict[str, Any]) -> None:
        report["report_schema_version"] = 3
        report["custody_current_capital"] = copy.deepcopy(custody)
        if block is not None:
            report["capital_coverage_bounds"] = copy.deepcopy(block)

    gen_dir = build(tmp_path, Spec(report=[mutate]))
    return checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=_expected() if expected is None else expected,
        expected_not_derivable=[] if not_derivable is None else not_derivable,
    )


def test_the_verifier_is_closed_by_the_whole_block_gate(tmp_path: Path) -> None:
    """Class sweep: verified_capital_coverage_bound is the only path from the block to a
    downgrade (the label check and the cash-label expectation both call it), and a block
    with any finding of check_capital_coverage_bound verifies nothing for any entity."""

    custody = _custody_block()
    context = _context(tmp_path, custody, _section_variant("false_date"))
    assert list(checks.check_capital_coverage_bound(context))
    for entity in ("alpha", "systematic", LINE_ENTITY):
        assert (
            checks.verified_capital_coverage_bound(context, entity, DAY_DATE) is None
        ), entity

    honest = _context(tmp_path / "honest", custody, _disclosure(custody))
    assert not list(checks.check_capital_coverage_bound(honest))
    assert checks.verified_capital_coverage_bound(honest, LINE_ENTITY, DAY_DATE)


def test_an_oracle_row_for_another_account_is_a_regression(tmp_path: Path) -> None:
    """K1 minor / ruling C2: a report line whose account_id differs from the oracle's
    entity is BLOCKER_REGRESSION, as a differing provider_id is, not an absent row."""

    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=_disclosure(custody),
        expected=[
            *_expected(coverage=None),
            _coverage_row(entity="alpha|synthetic|acct-other"),
        ],
    )

    assert seen[COVERAGE_END] == [
        ("alpha|synthetic|acct-a1", checks.BLOCKER_REGRESSION)
    ]
    assert sorted(seen[CHECK]) == sorted(_expect(checks.BLOCKER_AUTHORITY))


def test_the_not_derivable_account_placeholder_stays_an_authority_finding(
    tmp_path: Path,
) -> None:
    """The ``strategy|provider|-`` placeholder names a not-derivable row, not another
    account: it must not be read as a different account (J1 interface)."""

    custody = _custody_block()

    seen = _world(
        tmp_path,
        custody,
        block=None,
        expected=[*_expected(coverage=None), _coverage_row(entity="alpha|synthetic|-")],
    )

    assert seen[COVERAGE_END] == [("alpha|synthetic|acct-a1", checks.BLOCKER_AUTHORITY)]


PARENT_FORGED = {"coverage_end": "2026-09-18", "as_of_date": CUTOFF}


def _forged_cash(custody: dict[str, Any], **fields: str) -> dict[str, Any]:
    forged = copy.deepcopy(custody)
    forged["strategies"][0]["derived_cash"][0].update(fields)
    return forged


def test_the_unavailable_parent_world_is_the_producers_own() -> None:
    """Precondition (Astra rg7-astra-parent-guard): no NAV gives a schema-valid
    NOT_READY_AT_CUTOFF strategy row and total around an AVAILABLE cash line."""

    custody = _custody_block(nav=False)
    row = custody["strategies"][0]
    assert row["status"] == "NOT_READY_AT_CUTOFF"
    assert row["current_value"]["status"] == "UNAVAILABLE"
    assert row["derived_cash"][0]["status"] == "AVAILABLE"
    assert custody["systematic"]["status"] == "NOT_READY_AT_CUTOFF"


def test_a_forged_cash_coverage_under_an_unavailable_parent_is_caught(
    tmp_path: Path,
) -> None:
    """K2: forging the AVAILABLE cash component's coverage_end 2026-09-16 -> 2026-09-18 and
    its label to the cutoff gave ZERO findings when the parent was unavailable, while the
    available-parent control raised BLOCKER_REGRESSION and BLOCKER_INVARIANT."""

    forged = _forged_cash(_custody_block(nav=False), **PARENT_FORGED)

    seen = _world(tmp_path, forged, block=None, expected=_expected())

    assert seen[COVERAGE_END] == [
        ("alpha|synthetic|acct-a1", checks.BLOCKER_REGRESSION)
    ]
    assert (LINE, checks.BLOCKER_INVARIANT) in seen[CHECK]


def test_the_unavailable_and_available_parent_controls_agree_on_the_cash_line(
    tmp_path: Path,
) -> None:
    cash_findings = []
    for name, custody in (
        ("unavailable", _forged_cash(_custody_block(nav=False), **PARENT_FORGED)),
        ("available", _forged_cash(_custody_block(), **PARENT_FORGED)),
    ):
        seen = _world(tmp_path / name, custody, block=None, expected=_expected())
        cash_findings.append(
            (
                seen[COVERAGE_END],
                sorted(item for item in seen[CHECK] if item[0] == LINE),
            )
        )

    assert cash_findings[0] == cash_findings[1]


def test_an_honest_unavailable_parent_world_is_judged_by_its_cash_line(
    tmp_path: Path,
) -> None:
    """The parent claims no label (value UNAVAILABLE), the AVAILABLE cash line does: it is
    dated before the cutoff by its coverage, NOT_READY when disclosed and the oracle
    attests it, a blocker otherwise.  No coverage-end finding when the oracle agrees."""

    custody = _custody_block(nav=False)

    disclosed = _world(
        tmp_path / "disclosed",
        custody,
        block=_disclosure(custody),
        expected=[*_expected(), _cash_expectation()],
    )
    undisclosed = _world(
        tmp_path / "undisclosed", custody, block=None, expected=_expected()
    )

    assert COVERAGE_END not in disclosed
    assert dict(disclosed[CHECK]) == {LINE: checks.NOT_READY}
    assert dict(undisclosed[CHECK]) == {LINE: checks.BLOCKER_AUTHORITY}


def test_a_stale_nav_member_under_an_unavailable_parent_is_still_judged(
    tmp_path: Path,
) -> None:
    """Class sweep of the parent-level skips: an AVAILABLE member under a strategy row
    that is itself unavailable is dated by its own NAV observation and checked like any."""

    custody = _custody_block(nav=False)
    member = custody["strategies"][0]["members"][0]
    member.update(
        status="AVAILABLE",
        as_of_date="2025-11-24",
        value={"currency": "USD", "status": "AVAILABLE", "value": "5500"},
    )

    seen = _world(tmp_path, custody, block=None, expected=_expected())

    assert (MEMBER, checks.BLOCKER_AUTHORITY) in seen[CHECK]


# ------------------------------------------- round 8: L1 every stated line needs the oracle


def _two_line_world(second_provider: str | None = None) -> dict[str, Any]:
    custody = _custody_block()
    second = copy.deepcopy(custody["strategies"][0]["derived_cash"][0])
    second["account_id"] = "acct-a2"
    if second_provider is not None:
        second["provider_id"] = second_provider
    custody["strategies"][0]["derived_cash"].append(second)
    return custody


SECOND = "alpha|synthetic|acct-a2"
# What the oracle says about the SECOND stated line, by the result kind the lookup gives.
# The first line (acct-a1) is always the oracle's own, honest and attested.
RESULT_KINDS = {
    # no row under this provider and no account of it: ``absent``
    "absent": ("other-provider-x", [], []),
    # a not-derivable row: ``unusable``
    "unusable": (
        None,
        [],
        [{"metric_id": checks.COVERAGE_END_METRIC, "entity": SECOND}],
    ),
    # two rows that disagree: ``unusable``
    "conflicting": (
        None,
        [_coverage_row(DAY, SECOND), _coverage_row("2026-09-10", SECOND)],
        [],
    ),
    # the oracle gives this strategy and provider only under acct-a1: ``other_account``
    "other_account": (None, [], []),
    # the oracle names this account under another provider: ``other_provider``
    "other_provider": ("ibkr", [_coverage_row(DAY, "alpha|synthetic|acct-a2")], []),
    # a row for the line that dates the coverage differently: ``row`` mismatch
    "row_mismatch": (None, [_coverage_row("2026-09-10", SECOND)], []),
}


def test_every_unproved_stated_line_voids_every_downgrade() -> None:
    """Astra rg8 two-strategy probe: alpha stays NOT_READY beside an unproved line.  Each
    result kind other than a verified match is a capital.coverage_bound blocker and no
    entity of the report is downgraded."""

    import tempfile

    for name, (provider, rows_extra, not_derivable) in RESULT_KINDS.items():
        custody = _two_line_world(provider)
        block = _disclosure(custody)
        expected = [*_expected(), *rows_extra]
        with tempfile.TemporaryDirectory() as raw:
            context = _context(Path(raw), custody, block, expected, not_derivable)
            findings = list(checks.check_capital_coverage_bound(context))
            assert findings, name
            assert {f.klass for f in findings} == {checks.BLOCKER_INVARIANT}, name
            for entity in ("alpha", "systematic", LINE_ENTITY):
                assert (
                    checks.verified_capital_coverage_bound(context, entity, DAY_DATE)
                    is None
                ), (name, entity)
            dated = [
                f
                for f in checks.check_current_capital_as_of(context)
                if f.check_id == CHECK
            ]
            assert checks.NOT_READY not in {f.klass for f in dated}, name


def test_a_second_line_the_oracle_proves_keeps_the_block_valid() -> None:
    """Good control: both stated lines have a matching oracle row, so no finding and the
    first line is downgraded as before."""

    import tempfile

    custody = _two_line_world()
    block = _disclosure(custody)
    expected = [*_expected(), _coverage_row(DAY, SECOND)]
    with tempfile.TemporaryDirectory() as raw:
        context = _context(Path(raw), custody, block, expected)
        assert not list(checks.check_capital_coverage_bound(context))
        assert checks.verified_capital_coverage_bound(context, LINE_ENTITY, DAY_DATE)

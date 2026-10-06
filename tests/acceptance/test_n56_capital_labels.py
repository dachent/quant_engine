"""N56: custody capital labels are recomputed, not trusted (decision-seat ruling).

``as_of_date`` is the last date through which a value is proven: a NAV member's observation
date, a derived cash line's ``min(cutoff, coverage_end)``, a strategy row's the oldest of its
members and cash lines, the Systematic total's the oldest of its rows.  Anything before the
cutoff is NOT_READY only when its labels recompute and a verified value-basis bound discloses
it; otherwise it is a blocker and never passes silently.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from test_validate_alignment import (
    BOUND_DAY,
    _bound_entry,
    _bound_strategy,
    _bounds_block,
    _run,
    _strategy_cutoff,
)
from test_validate_generation import CUTOFF, Spec, build

from acceptance.validate import checks

CHECK = "capital.as_of_cutoff"
EARLY = "2025-12-05"


def _member(as_of: str = CUTOFF) -> dict[str, Any]:
    return {
        "account_id": "M1",
        "as_of_date": as_of,
        "status": "AVAILABLE",
        "value": {"currency": "USD", "status": "AVAILABLE", "value": "5500"},
    }


def _cash(
    as_of: str = CUTOFF,
    coverage_end: str | None = "2026-09-18",
    last_event: str | None = EARLY,
    *,
    versioned: bool = True,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "account_id": "CASH1",
        "as_of_date": as_of,
        "status": "AVAILABLE",
        "current_value": {"currency": "USD", "status": "AVAILABLE", "value": "93.97"},
        "events": [
            {"event_date": "2025-12-01", "signed_usd": "10"},
            {"event_date": EARLY, "signed_usd": "83.97"},
        ],
    }
    if versioned:
        line["last_event_date"] = last_event
        line["coverage_end"] = coverage_end
    return line


def _custody(
    *,
    member: dict[str, Any] | None = None,
    cash: dict[str, Any] | None = None,
    row: str = CUTOFF,
    systematic: str = CUTOFF,
    scope: str = "SYSTEMATIC",
):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "systematic": {
                "as_of_date": systematic,
                "status": "AVAILABLE",
                "current_value": {"status": "AVAILABLE", "value": "5594"},
            },
            "strategies": [
                {
                    "strategy_id": "beta",
                    "scope": scope,
                    "status": "AVAILABLE",
                    "as_of_date": row,
                    "members": [copy.deepcopy(member or _member())],
                    "derived_cash": [copy.deepcopy(cash or _cash())],
                }
            ],
        }

    return mutate


def _findings(
    tmp_path: Path, *mutators: Any, expected: Any = None, block: Any = None
) -> list[checks.Finding]:
    def attach(report: dict[str, Any]) -> None:
        if block is not None:
            report["value_basis_bounds"] = copy.deepcopy(block)

    gen_dir = build(tmp_path, Spec(report=[*mutators, attach]))
    return [f for f in _run(gen_dir, expected) if f.check_id == CHECK]


def _view(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return sorted((f.entity, f.klass) for f in findings)


def test_a_fully_covered_cash_line_at_the_cutoff_passes(tmp_path: Path) -> None:
    assert _findings(tmp_path, _custody()) == []


def test_rule_2_an_early_ending_window_labelled_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    """coverage_end is 2025-12-05 but the line, row and total are labelled the cutoff."""

    findings = _findings(tmp_path, _custody(cash=_cash(coverage_end=EARLY)))
    assert _view(findings) == [
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_INVARIANT)
    ]
    assert "min(cutoff, coverage_end)" in findings[0].message


def test_rule_2_a_last_event_date_that_is_not_the_latest_event_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _custody(cash=_cash(last_event="2025-12-01")))
    assert _view(findings) == [
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_INVARIANT)
    ]
    assert "last_event_date" in findings[0].message


def test_rule_2_one_stated_field_without_the_other_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _custody(cash=_cash(coverage_end=None)))
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]


def test_rule_3_a_stale_member_behind_a_cutoff_labelled_strategy_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, _custody(member=_member("2026-01-15")))
    entities = dict(_view(findings))
    assert entities["current_capital:beta"] == checks.BLOCKER_INVARIANT
    assert "oldest of its members" in next(
        f.message for f in findings if f.entity == "current_capital:beta"
    )
    assert entities["current_capital:beta:member:M1"] == checks.BLOCKER_AUTHORITY


def test_rule_3_a_total_labelled_newer_than_its_oldest_row_blocks(
    tmp_path: Path,
) -> None:
    older = _custody(
        cash=_cash(as_of=EARLY, coverage_end=EARLY), row=EARLY, systematic=CUTOFF
    )
    findings = _findings(tmp_path, older)
    by = {f.entity: f for f in findings}
    assert by["current_capital:systematic"].klass == checks.BLOCKER_INVARIANT
    assert "oldest of its rows" in by["current_capital:systematic"].message


def _bound_world(day: str):
    cash = _cash(as_of=day, coverage_end=day)
    return _custody(cash=cash, row=day, systematic=day)


def _bounds() -> dict[str, Any]:
    entries = [
        _bound_entry("current_capital", "beta:CASH1", "as_of_date", ("beta",)),
        _bound_entry("current_capital", "beta", "as_of_date", ("beta",)),
        _bound_entry("current_capital", "systematic", "as_of_date", ("beta",)),
    ]
    return _bounds_block([_bound_strategy("beta")], entries)


def test_rule_4_a_disclosed_value_basis_bound_downgrades_to_not_ready(
    tmp_path: Path,
) -> None:
    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=_bounds(),
    )
    assert _view(findings) == [
        ("current_capital:beta", checks.NOT_READY),
        ("current_capital:beta:cash:CASH1", checks.NOT_READY),
        ("current_capital:systematic", checks.NOT_READY),
    ]
    assert all("value basis" in f.message for f in findings)


def test_rule_4_an_undisclosed_pre_cutoff_date_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _bound_world(BOUND_DAY))
    assert _view(findings) == [
        ("current_capital:beta", checks.BLOCKER_AUTHORITY),
        ("current_capital:beta:cash:CASH1", checks.BLOCKER_AUTHORITY),
        ("current_capital:systematic", checks.BLOCKER_AUTHORITY),
    ]


def test_rule_4_a_bound_the_oracle_does_not_confirm_still_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(
        tmp_path,
        _bound_world(BOUND_DAY),
        expected=[_strategy_cutoff("beta", as_of="2026-09-10")],
        block=_bounds(),
    )
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_rule_4_a_dishonest_label_is_not_downgraded_even_if_disclosed(
    tmp_path: Path,
) -> None:
    """Disclosure never excuses a label that does not recompute (rule 2 fails)."""

    cash = _cash(as_of=BOUND_DAY, coverage_end="2026-09-10")
    findings = _findings(
        tmp_path,
        _custody(cash=cash, row=BOUND_DAY, systematic=BOUND_DAY),
        expected=[_strategy_cutoff("beta")],
        block=_bounds(),
    )
    classes: dict[str, set[str]] = {}
    for finding in findings:
        classes.setdefault(finding.entity, set()).add(finding.klass)
    assert classes["current_capital:beta:cash:CASH1"] == {
        checks.BLOCKER_INVARIANT,
        checks.BLOCKER_AUTHORITY,
    }
    assert classes["current_capital:beta"] == {checks.BLOCKER_AUTHORITY}
    assert classes["current_capital:systematic"] == {checks.BLOCKER_AUTHORITY}
    assert checks.NOT_READY not in set().union(*classes.values())


def test_rule_5_a_date_after_the_cutoff_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, _custody(member=_member("2026-10-04")))
    assert checks.BLOCKER_AUTHORITY in {f.klass for f in findings}
    assert "current_capital:beta:member:M1" in {f.entity for f in findings}


def test_rule_1_a_cash_line_or_member_without_a_date_is_a_finding(
    tmp_path: Path,
) -> None:
    no_cash_date = _cash()
    del no_cash_date["as_of_date"]
    findings = _findings(tmp_path / "cash", _custody(cash=no_cash_date))
    assert ("current_capital:beta:cash:CASH1", checks.BLOCKER_AUTHORITY) in _view(
        findings
    )
    no_member_date = _member()
    no_member_date["as_of_date"] = None
    findings = _findings(tmp_path / "member", _custody(member=no_member_date))
    assert ("current_capital:beta:member:M1", checks.BLOCKER_AUTHORITY) in _view(
        findings
    )


def test_an_old_version_line_without_the_new_fields_keeps_the_previous_behaviour(
    tmp_path: Path,
) -> None:
    """No last_event_date / coverage_end: rule 2 does not apply, rule 4 does."""

    old = _cash(as_of=EARLY, versioned=False)
    findings = _findings(tmp_path, _custody(cash=old, row=EARLY, systematic=EARLY))
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}
    assert not [f for f in findings if f.klass == checks.BLOCKER_INVARIANT]


def test_retained_rows_and_unavailable_lines_stay_out(tmp_path: Path) -> None:
    stale = _custody(
        member=_member("2025-11-24"),
        cash=_cash(as_of="2025-11-24", coverage_end="2025-11-24"),
        row="2025-11-24",
        scope="RETAINED_EXCLUDED",
    )
    findings = _findings(tmp_path / "retained", stale)
    assert [f.entity for f in findings] == []
    unavailable = _cash(as_of=EARLY)
    unavailable["status"] = "UNAVAILABLE"
    findings = _findings(tmp_path / "unavailable", _custody(cash=unavailable))
    assert not [f for f in findings if ":cash:" in f.entity]

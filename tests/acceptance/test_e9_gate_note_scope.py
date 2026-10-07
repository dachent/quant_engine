"""E9: a gate note may state a provider's snapshot limitation, scoped; never the
portfolio-wide claim that account balances are unavailable while the panel shows them.

The positions gate note is the report's positions known gap.  The real baseline pools
every provider's declared gaps unscoped and the real report producer scopes a clause a
provider declared to that provider and its generation date; the validator accepts the
scoped clause and still blocks an unscoped one.  The note below is built by those two
producers, not written by hand.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import portfolio_baseline as baseline_tool
from test_validate_alignment import _load_report_tool, _run
from test_validate_generation import Spec, _set, build

from acceptance.validate import checks

CHECK = "status.gate_note_contradiction"
LEGACY_GAP = "current account balances remain unavailable"
OTHER_GAP = "Investor Checking is outside the accepted brokerage boundary"


def _positions_note(*, declared_by_provider: bool = True) -> str:
    """The positions note the real producers publish for these pooled gaps."""

    claims, _exceptions = baseline_tool._fact_coverage(
        [],
        [],
        [],
        [],
        [{"position_id": "p"}],
        [OTHER_GAP, LEGACY_GAP],
        0,
        date(2026, 9, 17),
    )
    providers: list[dict[str, Any]] = (
        [
            {
                "coverage": {
                    "known_gaps": [OTHER_GAP, LEGACY_GAP],
                    "out_of_boundary": [],
                    "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                },
                "generation_as_of_date": "2026-09-18",
                "provider_id": "schwab",
                "qualification": "STAGED_COMPLETE",
            }
        ]
        if declared_by_provider
        else []
    )
    coverage = _load_report_tool()._coverage(
        {
            "calendar_coverage": [],
            "fact_coverage": claims,
            "provider_coverage": providers,
            "reconciliation_exceptions": [],
        }
    )
    row = next(
        item
        for item in coverage
        if item["category"] == "FACT_COVERAGE" and item["item"] == "positions"
    )
    note: str = row["known_gap"]
    return note


def _findings(tmp_path: Path, note: str) -> list[checks.Finding]:
    gen_dir = build(tmp_path, Spec(panel=[_set(("gates", 0, "note"), note)]))
    return [f for f in _run(gen_dir, None) if f.check_id == CHECK]


def test_the_note_the_producers_scope_is_not_a_contradiction(tmp_path: Path) -> None:
    note = _positions_note()

    assert note.startswith("schwab (as of 2026-09-18): ")
    assert _findings(tmp_path, note) == []


def test_the_unscoped_note_the_producers_published_before_is_still_blocked(
    tmp_path: Path,
) -> None:
    """The same pooled gaps with no provider that declared them stay unscoped (the
    sealed generation case before the report scoped them): the contradiction stands."""

    note = _positions_note(declared_by_provider=False)

    assert LEGACY_GAP in note and "(as of" not in note
    findings = _findings(tmp_path, note)
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]


def test_one_unscoped_clause_among_scoped_ones_is_still_blocked(
    tmp_path: Path,
) -> None:
    note = f"{_positions_note()}; account balances are unavailable"

    assert [f.klass for f in _findings(tmp_path, note)] == [checks.BLOCKER_INVARIANT]


def test_a_clause_scoped_to_no_provider_or_date_is_not_exempt(tmp_path: Path) -> None:
    for note in (
        f"schwab: {LEGACY_GAP}",
        f"(as of 2026-09-18): {LEGACY_GAP}",
        f"schwab (as of yesterday): {LEGACY_GAP}",
    ):
        assert [f.klass for f in _findings(tmp_path / str(abs(hash(note))), note)] == [
            checks.BLOCKER_INVARIANT
        ], note

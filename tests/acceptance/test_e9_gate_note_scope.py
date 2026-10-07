"""E9: a gate note may state a provider's snapshot limitation, scoped; never the
portfolio-wide claim that account balances are unavailable while the panel shows them.

The positions gate note is the report's positions known gap.  The real baseline pools
every provider's declared gaps unscoped and the real report producer scopes a clause a
provider declared to that provider and its generation date.  The validator exempts a
clause only when it names exactly one provider and a valid date, that provider has
exactly one ``provider_coverage`` row with that generation date and that exact text among
its declared gaps, and the report's own positions coverage text carries the clause.  The
note below is built by those two producers, not written by hand.
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


def _provider(provider_id: str, day: str, *gaps: str) -> dict[str, Any]:
    return {
        "coverage": {
            "known_gaps": list(gaps),
            "out_of_boundary": [],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "generation_as_of_date": day,
        "provider_id": provider_id,
        "qualification": "STAGED_COMPLETE",
    }


SCHWAB = _provider("schwab", "2026-09-18", OTHER_GAP, LEGACY_GAP)


def _producers(
    providers: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """The positions coverage row the real producers publish for these pooled gaps."""

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
    return str(row["known_gap"]), row


def _positions_note(*, declared_by_provider: bool = True) -> str:
    return _producers([SCHWAB] if declared_by_provider else [])[0]


def _findings(
    tmp_path: Path,
    note: str,
    *,
    providers: list[dict[str, Any]] | None = None,
    text: str | None = None,
) -> list[checks.Finding]:
    """The report carries ``providers`` and a positions coverage row whose text is
    ``text`` (the producers' own row when ``None``); the panel gate shows ``note``."""

    declared = [SCHWAB] if providers is None else providers
    row = {
        **_producers([SCHWAB])[1],
        "known_gap": _producers(declared)[0] if text is None else text,
    }
    gen_dir = build(
        tmp_path,
        Spec(
            report=[
                _set(("provider_coverage",), declared),
                _set(("coverage_data_quality",), [row]),
            ],
            panel=[_set(("gates", 0, "note"), note)],
        ),
    )
    return [f for f in _run(gen_dir, None) if f.check_id == CHECK]


def test_the_note_the_producers_scope_is_not_a_contradiction(tmp_path: Path) -> None:
    note = _positions_note()

    assert note.startswith("schwab (as of 2026-09-18): ")
    assert _findings(tmp_path, note) == []


def test_two_providers_declaring_one_clause_are_each_scoped_at_their_own_date(
    tmp_path: Path,
) -> None:
    providers = [
        _provider("ibkr", "2026-09-10", LEGACY_GAP),
        _provider("schwab", "2026-09-18", LEGACY_GAP),
    ]
    note, _ = _producers(providers)

    assert note == (
        f"{OTHER_GAP}; ibkr (as of 2026-09-10): {LEGACY_GAP}; "
        f"schwab (as of 2026-09-18): {LEGACY_GAP}"
    )
    assert _findings(tmp_path, note, providers=providers) == []


def test_the_unscoped_note_the_producers_published_before_is_still_blocked(
    tmp_path: Path,
) -> None:
    """The same pooled gaps with no provider that declared them stay unscoped (the
    sealed generation case before the report scoped them): the contradiction stands."""

    note = _positions_note(declared_by_provider=False)

    assert LEGACY_GAP in note and "(as of" not in note
    findings = _findings(tmp_path, note, providers=[])
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
        assert [
            f.klass for f in _findings(tmp_path / str(abs(hash(note))), note, text=note)
        ] == [checks.BLOCKER_INVARIANT], note


def _blocked(tmp_path: Path, name: str, note: str, **kwargs: Any) -> None:
    findings = _findings(tmp_path / name, note, **kwargs)
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT], name


def test_a_clause_scoped_to_a_provider_that_never_declared_it_is_blocked(
    tmp_path: Path,
) -> None:
    """Astra E9 attack 1: the same-provider, same-date clause IBKR never declared, though
    the report's positions text carries it."""

    note = f"ibkr (as of 2026-09-17): {LEGACY_GAP}"

    _blocked(tmp_path, "undeclared", note, text=note)


def test_a_clause_with_an_impossible_calendar_date_is_blocked(tmp_path: Path) -> None:
    """Astra E9 attack 2: 2026-99-99, even when the provider row states the same date."""

    note = f"schwab (as of 2026-99-99): {LEGACY_GAP}"
    bad = _provider("schwab", "2026-99-99", OTHER_GAP, LEGACY_GAP)

    _blocked(tmp_path, "date", note, providers=[bad], text=note)


def test_a_clause_with_an_appended_portfolio_claim_is_blocked(tmp_path: Path) -> None:
    """Astra E9 attack 3: a prefixed clause that goes on to claim every balance is
    unavailable; equality with a declared gap, not containment, is required."""

    note = (
        "schwab (as of 2026-09-18): snapshot absent. "
        "All portfolio account balances are unavailable"
    )

    _blocked(tmp_path, "appended", note, text=note)


def test_a_clause_dated_differently_from_the_generation_is_blocked(
    tmp_path: Path,
) -> None:
    note = f"schwab (as of 2026-09-17): {LEGACY_GAP}"

    _blocked(tmp_path, "other-date", note, text=note)


def test_a_clause_naming_several_providers_is_blocked(tmp_path: Path) -> None:
    note = f"ibkr/schwab (as of 2026-09-18): {LEGACY_GAP}"
    providers = [SCHWAB, _provider("ibkr", "2026-09-18", LEGACY_GAP)]

    _blocked(tmp_path, "several", note, providers=providers, text=note)


def test_a_scoped_clause_the_positions_text_does_not_carry_is_blocked(
    tmp_path: Path,
) -> None:
    """The panel note is not the report's own positions known-gap text."""

    note = f"schwab (as of 2026-09-18): {LEGACY_GAP}"

    _blocked(tmp_path, "absent", note, text=OTHER_GAP)


def test_a_provider_declared_twice_exempts_nothing(tmp_path: Path) -> None:
    """A provider with two coverage rows has no single generation the clause belongs
    to; the report producer refuses it and the validator does not believe it."""

    note = _positions_note()
    twice = [SCHWAB, _provider("schwab", "2026-09-10", LEGACY_GAP)]

    _blocked(tmp_path, "twice", note, providers=twice, text=note)


def test_a_declared_clause_extended_by_a_sentence_is_blocked(tmp_path: Path) -> None:
    """The declared text is a prefix of the clause; equality with a declared gap, not
    containment of it, decides, so the appended portfolio claim is never exempt."""

    note = (
        f"schwab (as of 2026-09-18): {LEGACY_GAP}. "
        "All portfolio account balances are unavailable"
    )

    _blocked(tmp_path, "extended", note, text=note)

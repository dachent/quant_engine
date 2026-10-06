"""R2 and R3: the evaluation clock against the declared data cutoff (N54).

The sealed manifest has one date field and downstream code reads it as the data date, so
a manifest dated at the execution clock stays a BLOCKER (a real producer defect repaired
later); the panel's own data date is checked on its own. The history contract keeps two
honestly named clocks: ``as_of_date`` is the data date and must be the cutoff;
``evaluated_on`` is the execution clock, which may be later than the cutoff but never
earlier than the data date.  No self-declared label exempts anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import CUTOFF, Spec, _set, build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation


def _findings(
    tmp_path: Path, spec: Spec | None = None, manifest_as_of: str | None = None
) -> list[checks.Finding]:
    generation = load_generation(build(tmp_path, spec or Spec()))
    if manifest_as_of is not None:
        generation.manifest["as_of_date"] = manifest_as_of
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )
    return list(checks.check_evaluation_clock(context))


def _history(as_of: str | None, evaluated_on: str | None, **extra: str):
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_history_contract"] = {
            "as_of_date": as_of,
            "evaluated_on": evaluated_on,
            **extra,
        }

    return mutate


def _view(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return [(f.entity, f.klass) for f in findings]


def test_a_manifest_dated_at_the_execution_clock_is_still_a_blocker(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, manifest_as_of="2026-10-04")
    assert _view(findings) == [("generation.json.as_of_date", checks.BLOCKER_AUTHORITY)]


def test_a_manifest_dated_at_the_cutoff_is_silent(tmp_path: Path) -> None:
    assert _findings(tmp_path, manifest_as_of=CUTOFF) == []


def test_the_panel_data_date_is_checked_separately(tmp_path: Path) -> None:
    spec = Spec(panel=[_set(("meta", "portfolio_as_of"), "2026-09-16")])
    assert _view(_findings(tmp_path, spec)) == [
        ("meta.portfolio_as_of", checks.BLOCKER_AUTHORITY)
    ]
    assert _findings(tmp_path / "ok", Spec()) == []


def test_history_evaluated_after_the_cutoff_is_not_blocked(tmp_path: Path) -> None:
    spec = Spec(report=[_history(CUTOFF, "2026-10-04")])
    assert _findings(tmp_path, spec) == []


def test_history_dated_at_another_data_date_is_a_blocker(tmp_path: Path) -> None:
    spec = Spec(report=[_history("2026-09-16", "2026-10-04")])
    assert _view(_findings(tmp_path, spec)) == [
        ("strategy_history_contract.as_of_date", checks.BLOCKER_AUTHORITY)
    ]


def test_history_without_a_data_date_is_a_blocker(tmp_path: Path) -> None:
    spec = Spec(report=[_history(None, "2026-10-04")])
    assert _view(_findings(tmp_path, spec)) == [
        ("strategy_history_contract.as_of_date", checks.BLOCKER_AUTHORITY)
    ]


def test_history_evaluated_before_its_data_date_is_an_invariant_blocker(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_history(CUTOFF, "2026-09-16")])
    assert _view(_findings(tmp_path, spec)) == [
        ("strategy_history_contract.evaluated_on", checks.BLOCKER_INVARIANT)
    ]


def test_no_self_declared_label_exempts_a_history_clock(tmp_path: Path) -> None:
    label = {"evaluated_on_basis": "EXECUTION_CLOCK"}
    spec = Spec(report=[_history("2026-09-16", "2026-09-15", **label)])
    assert {f.klass for f in _findings(tmp_path, spec)} == {
        checks.BLOCKER_AUTHORITY,
        checks.BLOCKER_INVARIANT,
    }
    assert not hasattr(checks, "EXECUTION_CLOCK_LABEL")

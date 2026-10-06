"""R3 (2): a registered check name is what its findings carry.

``run_checks`` attributes a crashed check to its registered name (``validator.check_error``),
so a name that differs from the ids the check emits misattributes the failure.  Every check
runs alone over the paired bad fixtures (and the fixtures that trigger the V16 checks);
whatever it emits is its registered name or one of the sibling ids a composite check is
documented to emit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_validate_generation import (
    CASES,
    CUTOFF,
    Spec,
    _policy_fixture,
    _set,
    build,
)

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

# Composite checks that deliberately emit sibling ids beside their registered name.
COMPOSITE = {
    "dates.section_as_of_vs_cutoff": {"dates.source_clock"},
    "lint.returns": {
        "lint.extreme_cagr",
        "lint.extreme_live_return",
        "lint.impossible_annualized",
        "lint.short_window_annualization",
    },
}


def _live_end(report: dict[str, Any]) -> None:
    report["strategy_summary"][0]["account_live_observed_end"] = "2026-08-14"


def _capital_date(report: dict[str, Any]) -> None:
    report["custody_current_capital"] = {
        "systematic": {
            "as_of_date": "2025-12-05",
            "status": "AVAILABLE",
            "current_value": {"status": "AVAILABLE", "value": "6600"},
        },
        "strategies": [],
    }


EXTRA = {
    "live_window": Spec(report=[_live_end]),
    "capital_date": Spec(report=[_capital_date]),
    "history_clock": Spec(panel=[_set(("meta", "portfolio_as_of"), "2026-09-16")]),
}


def _emitted(tmp_path: Path) -> dict[str, set[str]]:
    emitted: dict[str, set[str]] = {}
    specs = [spec for _, _, spec in CASES.values()] + list(EXTRA.values())
    for number, spec in enumerate(specs):
        gen_dir = build(tmp_path / f"c{number}", spec)
        context = checks.Context(
            gen=load_generation(gen_dir),
            cutoff=checks.date(2026, 9, 17),
            cutoff_source="test",
            expected=[],
            repo_root=_policy_fixture(gen_dir.parent / "auth"),
        )
        for name, check in checks.CHECKS:
            emitted.setdefault(name, set()).update(f.check_id for f in check(context))
    return emitted


def test_every_registered_check_emits_only_its_registered_name(tmp_path: Path) -> None:
    emitted = _emitted(tmp_path)
    stray = {
        name: sorted(ids - {name} - COMPOSITE.get(name, set()))
        for name, ids in emitted.items()
        if ids - {name} - COMPOSITE.get(name, set())
    }
    assert stray == {}


def test_the_v16_checks_are_registered_under_the_ids_they_emit() -> None:
    registered = {fn.__name__: name for name, fn in checks.CHECKS}
    assert registered["check_account_live_window"] == "dates.section_as_of_vs_cutoff"
    assert registered["check_current_capital_as_of"] == "capital.as_of_cutoff"
    assert registered["check_derived_cash_evidence"] == "capital.derived_cash_evidence"
    assert registered["check_evaluation_clock"] == "dates.evaluation_clock"


@pytest.mark.parametrize("name", [name for name, _ in checks.CHECKS])
def test_a_crashing_check_is_attributed_to_its_registered_name(name: str) -> None:
    def boom(_: checks.Context) -> Any:
        raise RuntimeError("boom")

    context = checks.Context(
        gen=None,  # type: ignore[arg-type]
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )
    findings = checks.run_checks(context, [(name, boom)])
    assert [(f.check_id, f.entity) for f in findings] == [
        ("validator.check_error", name)
    ]
    assert CUTOFF

"""Batch V16: coverage gaps, new checks for dates the validator never compared.

C2   a labelled benchmark comparison whose ``observed_end`` is before the cutoff.
C4   ``implementation_*_contract.as_of_date`` before the cutoff.
N56  ``custody_current_capital`` as_of (the systematic total and the systematic strategies).
N54  the evaluation clock published as a data date: ``generation.json`` ``as_of_date`` and
     ``strategy_history_contract.evaluated_on`` against the declared cutoff, unless the
     record labels it ``evaluated_on_basis: EXECUTION_CLOCK``.
C5   the account live window (``account_live_observed_end``) restored after V2 dropped the
     identity-window yield: it must reach the cutoff unless the strategy is not active or
     the live status is not available.
Each pairs a bad fixture with the good one that must stay silent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import CUTOFF, Spec, _set, build, run

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

STALE = "2026-07-17"
SECTION = "dates.section_as_of_vs_cutoff"
CAPITAL_AS_OF = "capital.as_of_cutoff"


def _only(findings: list[checks.Finding], check_id: str) -> list[checks.Finding]:
    return [f for f in findings if f.check_id == check_id]


def _generation_findings(tmp_path: Path, spec: Spec, check_id: str):
    return _only(run(build(tmp_path, spec)), check_id)


# ------------------------------------------------------------------------- C2, C4


def test_c2_a_benchmark_window_that_ends_before_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    path = ("strategy_attribution", "benchmark_comparisons", 0, "observed_end")
    findings = _generation_findings(
        tmp_path, Spec(report=[_set(path, STALE)]), SECTION
    )
    assert [(f.entity, f.klass) for f in findings] == [
        ("benchmark:alpha", checks.BLOCKER_AUTHORITY)
    ]
    assert findings[0].defect_ids == ("C2",)


def test_c2_benchmark_windows_that_reach_the_cutoff_are_silent(tmp_path: Path) -> None:
    assert _generation_findings(tmp_path, Spec(), SECTION) == []


def test_c4_an_implementation_contract_dated_before_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    path = ("implementation_reconciliation_contract", "as_of_date")
    findings = _generation_findings(
        tmp_path, Spec(report=[_set(path, "2026-08-03")]), SECTION
    )
    assert [(f.entity, f.klass) for f in findings] == [
        ("implementation_reconciliation_contract", checks.BLOCKER_AUTHORITY)
    ]
    assert findings[0].defect_ids == ("C4",)


# ----------------------------------------------------------------------------- N56


def _custody(
    systematic_as_of: str, scope: str = "SYSTEMATIC", strategy_as_of: str = CUTOFF
):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "systematic": {
                "as_of_date": systematic_as_of,
                "status": "AVAILABLE",
                "current_value": {"status": "AVAILABLE", "value": "6600"},
            },
            "strategies": [
                {
                    "strategy_id": "beta",
                    "scope": scope,
                    "status": "AVAILABLE",
                    "as_of_date": strategy_as_of,
                }
            ],
        }

    return mutate


def test_n56_a_systematic_total_dated_before_its_valuation_blocks(
    tmp_path: Path,
) -> None:
    findings = _generation_findings(
        tmp_path, Spec(report=[_custody("2025-12-05")]), CAPITAL_AS_OF
    )
    assert [(f.entity, f.klass) for f in findings] == [
        ("current_capital:systematic", checks.BLOCKER_AUTHORITY)
    ]


def test_n56_a_systematic_strategy_dated_before_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_custody(CUTOFF, strategy_as_of="2026-01-15")])
    findings = _generation_findings(tmp_path, spec, CAPITAL_AS_OF)
    assert [(f.entity, f.klass) for f in findings] == [
        ("current_capital:beta", checks.BLOCKER_AUTHORITY)
    ]


def test_n56_capital_dated_at_the_cutoff_and_retained_rows_are_silent(
    tmp_path: Path,
) -> None:
    ok = Spec(report=[_custody(CUTOFF)])
    assert _generation_findings(tmp_path / "ok", ok, CAPITAL_AS_OF) == []
    retained = Spec(
        report=[
            _custody(CUTOFF, scope="RETAINED_EXCLUDED", strategy_as_of="2025-11-24")
        ]
    )
    assert _generation_findings(tmp_path / "retained", retained, CAPITAL_AS_OF) == []


# ----------------------------------------------------------------------------- N54


def _clock_findings(tmp_path: Path, spec: Spec, manifest_as_of: str | None = None):
    generation = load_generation(build(tmp_path, spec))
    if manifest_as_of is not None:
        generation.manifest["as_of_date"] = manifest_as_of
    context = checks.Context(
        gen=generation, cutoff=checks.date(2026, 9, 17), cutoff_source="test"
    )
    return list(checks.check_evaluation_clock(context))


def _history(evaluated_on: str, **label: str):
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_history_contract"] = {
            "as_of_date": CUTOFF,
            "evaluated_on": evaluated_on,
            **label,
        }

    return mutate


def test_n54_a_manifest_dated_at_the_execution_clock_blocks(tmp_path: Path) -> None:
    findings = _clock_findings(tmp_path, Spec(), manifest_as_of="2026-10-04")
    assert [(f.entity, f.klass) for f in findings] == [
        ("generation.json.as_of_date", checks.BLOCKER_AUTHORITY)
    ]
    assert findings[0].defect_ids == ("N54",)


def test_n54_a_manifest_dated_at_the_cutoff_is_silent(tmp_path: Path) -> None:
    assert _clock_findings(tmp_path, Spec(), manifest_as_of=CUTOFF) == []


def test_n54_history_evaluated_after_the_requested_date_blocks_unless_labelled(
    tmp_path: Path,
) -> None:
    bad = _clock_findings(tmp_path / "bad", Spec(report=[_history("2026-10-04")]))
    assert [(f.entity, f.klass) for f in bad] == [
        ("strategy_history_contract.evaluated_on", checks.BLOCKER_AUTHORITY)
    ]
    labelled = Spec(
        report=[_history("2026-10-04", evaluated_on_basis="EXECUTION_CLOCK")]
    )
    assert _clock_findings(tmp_path / "labelled", labelled) == []
    assert _clock_findings(tmp_path / "ok", Spec(report=[_history(CUTOFF)])) == []


# ------------------------------------------------------------------------------ C5


def _live(**changes: str):
    def mutate(report: dict[str, Any]) -> None:
        report["strategy_summary"][0].update(changes)

    return mutate


def test_c5_an_active_live_window_that_ends_before_the_cutoff_blocks(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_live(account_live_observed_end="2026-08-14")])
    findings = _generation_findings(tmp_path, spec, SECTION)
    assert [(f.entity, f.klass) for f in findings] == [
        ("account_live:alpha", checks.BLOCKER_AUTHORITY)
    ]


def test_c5_a_live_window_after_the_cutoff_blocks(tmp_path: Path) -> None:
    spec = Spec(report=[_live(account_live_observed_end="2026-10-04")])
    findings = _generation_findings(tmp_path, spec, SECTION)
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]


def test_c5_retained_or_unavailable_live_windows_are_exempt(tmp_path: Path) -> None:
    for name, changes in (
        ("retained", {"lifecycle_status": "RETAINED"}),
        ("excluded", {"lifecycle_status": "EXCLUDED"}),
        ("unavailable", {"account_live_status": "UNAVAILABLE_NO_NAV"}),
    ):
        spec = Spec(report=[_live(account_live_observed_end="2026-08-14", **changes)])
        assert _generation_findings(tmp_path / name, spec, SECTION) == [], name


def test_c5_a_live_window_at_the_cutoff_is_silent(tmp_path: Path) -> None:
    assert _generation_findings(tmp_path, Spec(), SECTION) == []


def test_c2_a_retained_reference_account_history_is_not_a_stale_window(
    tmp_path: Path,
) -> None:
    base = ("strategy_attribution", "benchmark_comparisons", 0)
    spec = Spec(
        report=[
            _set((*base, "observed_end"), "2025-11-24"),
            _set(
                (*base, "account_series_source"),
                "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY",
            ),
        ]
    )
    assert _generation_findings(tmp_path, spec, SECTION) == []

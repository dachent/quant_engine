"""Batch V16: coverage gaps, new checks for dates the validator never compared.

C2/C4 (labelled windows and contracts: see test_r2_dates.py.)
N56  ``custody_current_capital`` as_of (the systematic total and the systematic strategies).
N54  (the evaluation clock: see test_r2_clock.py.)
C5   the account live window (``account_live_observed_end``) restored after V2 dropped the
     identity-window yield: it must reach the cutoff unless the strategy is not active or
     the live status is not available.
Each pairs a bad fixture with the good one that must stay silent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_generation import CUTOFF, Spec, build, run

from acceptance.validate import checks

STALE = "2026-07-17"
SECTION = "dates.section_as_of_vs_cutoff"
CAPITAL_AS_OF = "capital.as_of_cutoff"


def _only(findings: list[checks.Finding], check_id: str) -> list[checks.Finding]:
    return [f for f in findings if f.check_id == check_id]


def _generation_findings(tmp_path: Path, spec: Spec, check_id: str):
    return _only(run(build(tmp_path, spec)), check_id)


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


def test_c5_producer_inactive_lifecycles_and_unavailable_windows_are_exempt(
    tmp_path: Path,
) -> None:
    """The producer's lifecycle values are ACTIVE, DEPRECATED_HISTORY_ONLY and
    IMMATERIAL_EXCLUDED (tools/portfolio_control_panel.py); exactly the last two exempt."""

    for name, changes in (
        ("deprecated", {"lifecycle_status": "DEPRECATED_HISTORY_ONLY"}),
        ("immaterial", {"lifecycle_status": "IMMATERIAL_EXCLUDED"}),
        ("unavailable", {"account_live_status": "UNAVAILABLE_NO_NAV"}),
    ):
        spec = Spec(report=[_live(account_live_observed_end="2026-08-14", **changes)])
        assert _generation_findings(tmp_path / name, spec, SECTION) == [], name


def test_c5_a_lifecycle_the_producer_does_not_define_exempts_nothing(
    tmp_path: Path,
) -> None:
    for name, status in (("a", "RETAINED"), ("b", "EXCLUDED"), ("c", "PAUSED")):
        spec = Spec(
            report=[
                _live(account_live_observed_end="2026-08-14", lifecycle_status=status)
            ]
        )
        findings = _generation_findings(tmp_path / name, spec, SECTION)
        assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY], status


def test_c5_a_live_window_at_the_cutoff_is_silent(tmp_path: Path) -> None:
    assert _generation_findings(tmp_path, Spec(), SECTION) == []

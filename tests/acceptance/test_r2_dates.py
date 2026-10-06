"""R4 and R5: labelled section dates and the account live window against their bounds.

R4  A labelled role that passes the role validation and ends before the cutoff keeps its
    true end date and is NOT_READY (the descriptive benchmark windows, the recipe-pinned
    historical contracts); an invalid role, a missing label or a date after the cutoff
    stays a BLOCKER.  A retained-history label exempts only a strategy the crosswalk
    shows as not active.
R5  The account live window of an active, available strategy must reach the cutoff;
    a verified value-basis bound (FXB-1, oracle-confirmed) makes an earlier end NOT_READY
    with the bound message, none or a mismatching bound keeps the BLOCKER, and a missing
    end is a BLOCKER.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_validate_alignment import (
    BLOCK,
    BOUND_DAY,
    _bound_entry,
    _bound_strategy,
    _bounds_block,
    _run,
    _strategy_cutoff,
)
from test_validate_generation import CUTOFF, Spec, _set, build, run

from acceptance.validate import checks

SECTION = "dates.section_as_of_vs_cutoff"
RETAINED = "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY"
COMPARISON = ("strategy_attribution", "benchmark_comparisons", 0)


def _section(findings: list[checks.Finding], prefix: str) -> list[checks.Finding]:
    return [
        f for f in findings if f.check_id == SECTION and f.entity.startswith(prefix)
    ]


def _view(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return [(f.entity, f.klass) for f in findings]


# ------------------------------------------------------------------------------ R4


def test_a_valid_labelled_benchmark_window_before_the_cutoff_is_not_ready(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_set((*COMPARISON, "observed_end"), "2026-07-17")])
    findings = _section(run(build(tmp_path, spec)), "benchmark:")
    assert _view(findings) == [("benchmark:alpha", checks.NOT_READY)]
    assert findings[0].observed["as_of"] == "2026-07-17"
    assert findings[0].defect_ids == ("C2",)


def test_an_invalid_role_before_the_cutoff_stays_a_blocker(tmp_path: Path) -> None:
    spec = Spec(
        report=[
            _set((*COMPARISON, "observed_end"), "2026-07-17"),
            _set((*COMPARISON, "version_compatibility_status"), "UNRESOLVED"),
        ]
    )
    assert _view(_section(run(build(tmp_path, spec)), "benchmark:")) == [
        ("benchmark:alpha", checks.BLOCKER_AUTHORITY)
    ]


def test_a_missing_label_before_the_cutoff_stays_a_blocker(tmp_path: Path) -> None:
    def drop(report: dict[str, Any]) -> None:
        row = report["strategy_attribution"]["benchmark_comparisons"][0]
        row["observed_end"] = "2026-07-17"
        del row["status"]

    findings = _section(run(build(tmp_path, Spec(report=[drop]))), "benchmark:")
    assert _view(findings) == [("benchmark:alpha", checks.BLOCKER_AUTHORITY)]


def test_a_labelled_date_after_the_cutoff_stays_a_blocker(tmp_path: Path) -> None:
    spec = Spec(report=[_set((*COMPARISON, "observed_end"), "2026-10-04")])
    assert _view(_section(run(build(tmp_path, spec)), "benchmark:")) == [
        ("benchmark:alpha", checks.BLOCKER_AUTHORITY)
    ]


def test_a_valid_historical_contract_before_the_cutoff_is_not_ready(
    tmp_path: Path,
) -> None:
    path = ("implementation_reconciliation_contract", "as_of_date")
    findings = _section(
        run(build(tmp_path, Spec(report=[_set(path, "2026-08-03")]))),
        "implementation_",
    )
    assert _view(findings) == [
        ("implementation_reconciliation_contract", checks.NOT_READY)
    ]
    assert findings[0].observed["as_of"] == "2026-08-03"


def test_an_invalid_historical_contract_stays_a_blocker(tmp_path: Path) -> None:
    def forge(report: dict[str, Any]) -> None:
        contract = report["implementation_reconciliation_contract"]
        contract["as_of_date"] = "2026-08-03"
        contract["sha256"] = "not-a-digest"

    findings = _section(run(build(tmp_path, Spec(report=[forge]))), "implementation_")
    assert _view(findings) == [
        ("implementation_reconciliation_contract", checks.BLOCKER_AUTHORITY)
    ]


def _retained_comparison(strategy: str):
    def mutate(report: dict[str, Any]) -> None:
        row = report["strategy_attribution"]["benchmark_comparisons"][0]
        row.update(
            {
                "strategy_id": strategy,
                "observed_end": "2025-11-24",
                "account_series_source": RETAINED,
            }
        )

    return mutate


def test_a_retained_label_is_confirmed_by_the_crosswalk_lifecycle(
    tmp_path: Path,
) -> None:
    """``gamma`` is an excluded strategy in the crosswalk fixture."""

    spec = Spec(report=[_retained_comparison("gamma")])
    assert _section(run(build(tmp_path, spec)), "benchmark:") == []


def test_an_active_strategy_cannot_label_itself_retained(tmp_path: Path) -> None:
    spec = Spec(report=[_retained_comparison("alpha")])
    findings = _section(run(build(tmp_path, spec)), "benchmark:")
    assert _view(findings) == [("benchmark:alpha", checks.NOT_READY)]


# ------------------------------------------------------------------------------ R5


def _live(**changes: Any):
    def mutate(report: dict[str, Any]) -> None:
        row = report["strategy_summary"][0]
        for key, value in changes.items():
            if value is _GONE:
                row.pop(key, None)
            else:
                row[key] = value

    return mutate


_GONE = object()


def _bounded(tmp_path: Path, block: Any, oracle: Any, end: str = BOUND_DAY):
    def attach(report: dict[str, Any]) -> None:
        if block is not None:
            report[BLOCK] = block

    spec = Spec(report=[_live(account_live_observed_end=end), attach])
    return _section(_run(build(tmp_path, spec), oracle), "account_live:")


BOUND = _bounds_block(
    [_bound_strategy("alpha")],
    [
        _bound_entry(
            "strategy_summary", "alpha", "account_live_observed_end", ("alpha",)
        )
    ],
)


def test_a_verified_bound_makes_the_short_live_window_not_ready(tmp_path: Path) -> None:
    findings = _bounded(tmp_path, BOUND, [_strategy_cutoff("alpha")])
    assert _view(findings) == [("account_live:alpha", checks.NOT_READY)]
    assert "value basis" in findings[0].message


def test_no_bound_keeps_the_blocker(tmp_path: Path) -> None:
    findings = _bounded(tmp_path, None, [_strategy_cutoff("alpha")])
    assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)]


def test_a_bound_the_oracle_does_not_confirm_keeps_the_blocker(tmp_path: Path) -> None:
    wrong_oracle = [_strategy_cutoff("alpha", as_of="2026-09-10")]
    findings = _bounded(tmp_path, BOUND, wrong_oracle)
    assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)]


def test_a_bound_for_another_date_keeps_the_blocker(tmp_path: Path) -> None:
    findings = _bounded(tmp_path, BOUND, [_strategy_cutoff("alpha")], end="2026-08-14")
    assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)]


def test_a_bound_naming_another_strategy_keeps_the_blocker(tmp_path: Path) -> None:
    other = _bounds_block(
        [_bound_strategy("beta")],
        [
            _bound_entry(
                "strategy_summary", "beta", "account_live_observed_end", ("beta",)
            )
        ],
    )
    oracle = [_strategy_cutoff("alpha"), _strategy_cutoff("beta")]
    findings = _bounded(tmp_path, other, oracle)
    assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)]


def test_an_active_available_strategy_without_an_end_is_a_blocker(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_live(account_live_observed_end=_GONE)])
    findings = _section(run(build(tmp_path, spec)), "account_live:")
    assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)]


def test_a_null_or_malformed_end_is_a_blocker(tmp_path: Path) -> None:
    for name, value in (("null", None), ("malformed", "2026-9-3")):
        spec = Spec(report=[_live(account_live_observed_end=value)])
        findings = _section(run(build(tmp_path / name, spec)), "account_live:")
        assert _view(findings) == [("account_live:alpha", checks.BLOCKER_AUTHORITY)], (
            name
        )


def test_a_missing_end_is_exempt_for_inactive_or_unavailable_strategies(
    tmp_path: Path,
) -> None:
    for name, changes in (
        ("retained", {"lifecycle_status": "RETAINED"}),
        ("unavailable", {"account_live_status": "UNAVAILABLE_NO_NAV"}),
    ):
        spec = Spec(report=[_live(account_live_observed_end=_GONE, **changes)])
        assert _section(run(build(tmp_path / name, spec)), "account_live:") == [], name


def test_a_live_window_at_the_cutoff_is_silent(tmp_path: Path) -> None:
    assert _section(run(build(tmp_path)), "account_live:") == []
    assert CUTOFF

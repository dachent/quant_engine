"""lint.short_window_annualization blocks unlabelled short-window figures (#594).

From report version 5 each annualised figure states its basis; a figure annualised
from under 365 days that does not is a blocker naming the exact row.  Earlier
versions carry no basis and keep the warning.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from acceptance.validate import checks


def _context(report: dict[str, Any]) -> Any:
    return SimpleNamespace(report=report, panel={}, _cache={})


def _mwr_row(**extra: Any) -> dict[str, Any]:
    return {
        "account_id": "A1",
        "strategy_id": "alpha",
        "effective_start": "2026-08-01",
        "effective_end": None,
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_xirr": "0.3176",
        "mwr_opening_date": "2026-08-01",
        "mwr_terminal_date": "2026-09-10",
        "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
        "twr": "0.0379",
        **extra,
    }


def _short(findings: list[Any]) -> list[Any]:
    return [f for f in findings if f.check_id == "lint.short_window_annualization"]


def test_version_four_keeps_the_warning() -> None:
    report = {"report_schema_version": 4, "twr_mwr": [_mwr_row()]}
    found = _short(list(checks.check_lint_returns(_context(report))))
    assert [f.klass for f in found] == [checks.WARNING]
    assert found[0].observed == {"twr_mwr.mwr_xirr:A1|alpha|2026-08-01": 40}


def test_version_five_blocks_an_unlabelled_short_window_mwr() -> None:
    report = {"report_schema_version": 5, "twr_mwr": [_mwr_row()]}
    found = _short(list(checks.check_lint_returns(_context(report))))
    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]
    assert found[0].observed == {"twr_mwr.mwr_xirr:A1|alpha|2026-08-01": 40}
    assert checks.BLOCKER_INVARIANT in checks.BLOCKERS


def test_version_five_accepts_a_short_window_figure_that_states_its_basis() -> None:
    row = _mwr_row(
        annualisation_basis={
            "mwr_xirr": {"reported_basis": "ANNUALISED_FROM_DAYS", "window_days": 40}
        }
    )
    report = {"report_schema_version": 5, "twr_mwr": [row]}
    found = _short(list(checks.check_lint_returns(_context(report))))
    assert [f.klass for f in found] == [checks.WARNING]


def test_version_five_blocks_an_annualised_twr_under_365_days() -> None:
    row = _mwr_row(
        window_days=40,
        twr_annualised="0.4",
        annualisation_basis={
            "mwr_xirr": {"reported_basis": "ANNUALISED_FROM_DAYS", "window_days": 40}
        },
    )
    report = {"report_schema_version": 5, "twr_mwr": [row]}
    found = _short(list(checks.check_lint_returns(_context(report))))
    blockers = [f for f in found if f.klass == checks.BLOCKER_INVARIANT]
    assert len(blockers) == 1
    assert blockers[0].observed == {"twr_mwr.twr_annualised:A1|alpha|2026-08-01": 40}


def test_period_recomputation_flags_a_wrong_or_early_annualised_value() -> None:
    good = _mwr_row(
        return_window_start="2026-08-01",
        return_close_date="2026-09-10",
        window_days=40,
        twr_annualised=None,
        annualisation_basis={"twr": {"reported_basis": "CUMULATIVE"}},
        calendar_year_twr={"2026": "0.0379"},
    )
    from decimal import Decimal, localcontext

    with localcontext() as context:
        context.prec = 34
        equivalent = Decimal("1.3176") ** (Decimal(40) / Decimal(365)) - 1
    good["mwr_window_equivalent"] = format(equivalent, "f")
    report = {"report_schema_version": 5, "twr_mwr": [good], "strategy_summary": []}
    assert list(checks.check_period_returns(_context(report))) == []
    bad = {**good, "twr_annualised": "0.4", "window_days": 41}
    report = {"report_schema_version": 5, "twr_mwr": [bad], "strategy_summary": []}
    found = list(checks.check_period_returns(_context(report)))
    assert len(found) == 1
    problems = found[0].observed["twr_mwr:A1|alpha|2026-08-01"]
    assert set(problems) == {"window_days", "twr_annualised"}

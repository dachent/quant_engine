"""The short-window annualisation lint covers account-manifest rows (J6)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from acceptance.validate import checks


def _ctx(rows: list[dict[str, Any]]) -> checks.Context:
    generation = SimpleNamespace(
        report={"report_schema_version": checks.PERIOD_RETURNS_REPORT_VERSION},
        panel={"account_manifest": {"rows": rows}},
    )
    return checks.Context(
        gen=generation,  # type: ignore[arg-type]
        cutoff=date(2026, 9, 17),
        cutoff_source="test",
    )


def _row(
    annual: str | None, basis: str | None, *, start: str = "2026-06-01"
) -> dict[str, Any]:
    return {
        "account_id": "account-aaaa",
        "strategy_id": "strategy",
        "effective_start": start,
        "effective_end": None,
        "opening_capital_date": start,
        "end": "2026-09-17",
        "annualized_return_on_net_capital": annual,
        "annualisation_basis": {
            "annualized_return_on_net_capital": {"reported_basis": basis}
        },
    }


def _findings(rows: list[dict[str, Any]]) -> list[checks.Finding]:
    return [
        item
        for item in checks.check_lint_returns(_ctx(rows))
        if item.check_id == "lint.short_window_annualization"
    ]


def test_a_short_window_annualised_ratio_without_a_basis_is_a_blocker() -> None:
    found = _findings([_row("0.5", None)])
    assert [item.klass for item in found] == [checks.BLOCKER_INVARIANT]
    assert any("annualized_return_on_net_capital" in key for key in found[0].observed)


def test_a_short_window_ratio_that_states_its_basis_is_a_warning() -> None:
    found = _findings([_row("0.5", "ANNUALISED_FROM_DAYS")])
    assert [item.klass for item in found] == [checks.WARNING]


def test_an_exited_epoch_with_no_ratio_and_a_year_long_window_are_clean() -> None:
    assert _findings([_row(None, None)]) == []
    assert _findings([_row("0.5", None, start="2025-01-01")]) == []


def _mwr_row(basis: str | None, *, start: str, end: str, state: str) -> dict[str, Any]:
    return {
        "account_id": "account-bbbb",
        "strategy_id": "retired",
        "effective_start": start,
        "effective_end": None if state == "ACTIVE" else end,
        "end": end,
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_xirr": "-0.8757",
        "mwr_opening_date": start,
        "mwr_terminal_date": end,
        "annualisation_basis": {"mwr_xirr": {"reported_basis": basis}},
    }


def test_a_short_window_annualised_mwr_on_a_retired_epoch_is_linted_by_name() -> None:
    """R-10: an XIRR annualised from 30 days is reported, retired or not."""

    retired = _mwr_row(
        "ANNUALISED_FROM_DAYS", start="2026-06-22", end="2026-07-22", state="RETIRED"
    )
    found = _findings([retired])
    assert [item.klass for item in found] == [checks.WARNING]
    assert any("account_manifest.mwr_xirr" in key for key in found[0].observed)
    # an annualised figure that does not say so is a blocker; a year-long window is clean
    unlabelled = _mwr_row(None, start="2026-06-22", end="2026-07-22", state="RETIRED")
    assert [item.klass for item in _findings([unlabelled])] == [
        checks.BLOCKER_INVARIANT
    ]
    year = _mwr_row(None, start="2025-06-22", end="2026-07-22", state="RETIRED")
    assert _findings([year]) == []

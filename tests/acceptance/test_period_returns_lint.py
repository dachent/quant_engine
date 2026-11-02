"""lint.short_window_annualization blocks unlabelled short-window figures (#594).

From report version 5 each annualised figure states its basis; a figure annualised
from under 365 days that does not is a blocker naming the exact row.  Earlier
versions carry no basis and keep the warning.
"""

from __future__ import annotations

from datetime import date, timedelta
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


def _oracle(opening: str, strategy: str = "alpha") -> Any:
    """A validator context whose own records (expected baseline) name the opening NAV date."""

    return [
        {
            "metric_id": "twr.strategy",
            "entity": strategy,
            "notes": f"account-x funded segment [2025-09-18, open] opening {opening} value 100",
        }
    ]


def _cagr_context(
    row: dict[str, Any],
    expected: Any = None,
    panel: dict[str, Any] | None = None,
    version: int = 5,
) -> Any:
    report = {"report_schema_version": version, "strategy_summary": [row]}
    context = SimpleNamespace(
        report=report, panel=panel or {}, expected=expected, _cache={}
    )
    # the governed source of the opening date is the provider ledger's anchor: tests that name
    # the opening through ``_oracle`` give the context a ledger anchor with the same date
    note = checks._OPENING_NOTE.search(str((expected or [{}])[0].get("notes", "")))
    context.provider_ledger = _ledger(note.group(1)) if note else None
    return context


def _cagr_row(basis: str, **extra: Any) -> dict[str, Any]:
    return {
        "strategy_id": "alpha",
        "account_live_cagr": "0.2",
        "account_live_observed_start": "2025-09-18",
        "account_live_observed_end": "2026-09-17",
        "account_live_return_authority": "CANONICAL_STRATEGY_RETURNS",
        "annualisation_basis": {
            "account_live_cagr": {"reported_basis": basis, "window_days": 365}
        },
        **extra,
    }


def _days_before_end(days: int) -> str:
    return (date(2026, 9, 17) - timedelta(days=days)).isoformat()


def test_validator_boundary_364_365_366_days_from_the_opening_nav() -> None:
    """ANNUALISED_FROM_DAYS under 365 days, ANNUALISED from 365, measured from the opening NAV."""

    for days, labelled_ok, plain_ok in (
        (364, True, False),
        (365, False, True),
        (366, False, True),
    ):
        opening = _days_before_end(days)
        for basis, accepted in (
            ("ANNUALISED_FROM_DAYS", labelled_ok),
            ("ANNUALISED", plain_ok),
        ):
            context = _cagr_context(_cagr_row(basis), _oracle(opening))
            found = _short(list(checks.check_lint_returns(context)))
            if days >= 365:
                assert found == [], (
                    days,
                    basis,
                )  # not a short window: nothing to label
            else:
                assert bool(found) is True  # short, recorded
                blocked = [f for f in found if f.klass == checks.BLOCKER_INVARIANT]
                assert (not blocked) is accepted, (days, basis)


def test_validator_opening_after_a_weekend_is_the_friday_not_the_day_before() -> None:
    # first return Monday 2025-09-22; the opening NAV is Friday 2025-09-19 (the previous close)
    monday = date(2025, 9, 22)
    assert monday.weekday() == 0
    row = _cagr_row(
        "ANNUALISED_FROM_DAYS", account_live_observed_start=monday.isoformat()
    )
    row["account_live_observed_end"] = "2026-09-18"
    # Friday to 2026-09-18 is 364 days (short); Sunday (first return minus one) would be 361
    context = _cagr_context(row, _oracle("2025-09-19"))
    assert checks._cagr_window_days(context, row) == 364
    assert checks._strategy_opening_date(context, "alpha", monday) == date(2025, 9, 19)
    # one day more of history and the Friday opening makes it exactly 365: not short
    row["account_live_observed_end"] = "2026-09-19"
    assert checks._cagr_window_days(context, row) == 365
    assert _short(list(checks.check_lint_returns(context))) == []


def test_validator_falls_back_to_the_manifest_link_ending_on_the_first_return() -> None:
    panel = {
        "account_manifest": {
            "rows": [
                {
                    "strategy_id": "alpha",
                    "observation_trace": {
                        "links": [
                            {"start_date": "2025-09-17", "end_date": "2025-09-18"},
                            {"start_date": "2025-09-18", "end_date": "2025-09-19"},
                        ]
                    },
                }
            ]
        }
    }
    row = _cagr_row("ANNUALISED")
    context = _cagr_context(row, expected=None, panel=panel)
    assert checks._cagr_window_days(context, row) == 365
    # no record at all: the first return date, as before
    bare = _cagr_context(row)
    assert checks._cagr_window_days(bare, row) == 364


def _tamper_row(**extra: Any) -> dict[str, Any]:
    row = _cagr_row("ANNUALISED", return_window_start="2025-09-17", window_days=365)
    row.update(extra)
    return row


def test_tampering_return_window_start_or_window_days_is_a_blocking_finding() -> None:
    expected = _oracle("2025-09-17")
    honest = _cagr_context(_tamper_row(), expected)
    assert (
        checks._opening_window_problems(honest, honest.report["strategy_summary"][0])
        == {}
    )

    moved = _cagr_context(_tamper_row(return_window_start="2025-09-18"), expected)
    found = list(checks.check_period_returns(moved))
    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]
    problems = found[0].observed["strategy_summary:alpha"]
    assert problems["return_window_start"] == {
        "published": "2025-09-18",
        "derived": "2025-09-17",
    }

    short = _cagr_context(_tamper_row(window_days=364), expected)
    found = list(checks.check_period_returns(short))
    assert [f.klass for f in found] == [checks.BLOCKER_INVARIANT]
    assert found[0].observed["strategy_summary:alpha"]["window_days"] == {
        "published": 364,
        "derived": 365,
    }
    # both edited together, still against the validator's own record
    both = _cagr_context(
        _tamper_row(return_window_start="2025-09-18", window_days=364), expected
    )
    problems = list(checks.check_period_returns(both))[0].observed[
        "strategy_summary:alpha"
    ]
    assert {"return_window_start", "window_days"} <= set(problems)


def test_a_non_canonical_row_or_one_without_the_field_is_not_compared() -> None:
    expected = _oracle("2025-09-17")
    row = _tamper_row(
        account_live_return_authority="OTHER", return_window_start="1999-01-01"
    )
    context = _cagr_context(row, expected)
    assert checks._opening_window_problems(context, row) == {}
    plain = _cagr_row("ANNUALISED")
    assert checks._opening_window_problems(_cagr_context(plain, expected), plain) == {}


def _ledger(opening: str, strategy: str = "alpha") -> dict[str, Any]:
    return {
        "capital_anchors": [
            {
                "strategy_id": strategy,
                "evidence_layer": "ACCOUNT_LIVE",
                "capital_type": "OPENING_CAPITAL_ANCHOR",
                "nav_recognition_date": opening,
            }
        ]
    }


def test_validator_reads_the_opening_anchor_from_the_ledger_not_the_note() -> None:
    row = _tamper_row()
    # the hand note says 2025-09-10 but the governed ledger anchor says 2025-09-17
    context = _cagr_context(row, _oracle("2025-09-10"))
    context.provider_ledger = _ledger("2025-09-17")
    assert checks._strategy_opening_date(context, "alpha", date(2025, 9, 18)) == date(
        2025, 9, 17
    )
    problems = checks._opening_window_problems(context, row)
    # the ledger-derived date matches the published row; the note disagrees and is reported
    assert set(problems) == {"opening_note_disagrees"}
    assert problems["opening_note_disagrees"] == {
        "derived": "2025-09-17",
        "expected_baseline_note": "2025-09-10",
    }


def test_the_note_alone_is_never_a_source_of_the_opening_date() -> None:
    row = _tamper_row()
    context = _cagr_context(row, _oracle("2025-09-17"))
    context.provider_ledger = None  # note only: no ledger, no manifest
    assert checks._strategy_opening_date(context, "alpha", date(2025, 9, 18)) is None
    assert checks._opening_window_problems(context, row) == {}


def test_ledger_anchor_beats_a_manifest_link_and_a_later_anchor_is_ignored() -> None:
    panel = {
        "account_manifest": {
            "rows": [
                {
                    "strategy_id": "alpha",
                    "observation_trace": {
                        "links": [
                            {"start_date": "2025-09-16", "end_date": "2025-09-18"}
                        ]
                    },
                }
            ]
        }
    }
    row = _tamper_row()
    context = _cagr_context(row, None, panel)
    context.provider_ledger = {
        "capital_anchors": [
            *_ledger("2025-09-17")["capital_anchors"],
            *_ledger("2025-09-19")[
                "capital_anchors"
            ],  # after the first return: not an opening
        ]
    }
    assert checks._strategy_opening_date(context, "alpha", date(2025, 9, 18)) == date(
        2025, 9, 17
    )
    context.provider_ledger = None
    assert checks._strategy_opening_date(context, "alpha", date(2025, 9, 18)) == date(
        2025, 9, 16
    )

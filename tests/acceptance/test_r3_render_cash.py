"""R3 (5): the weekly slice argument and the native-amount check of a cash event.

V13  a ``weeklyVisibleWeeks`` that keeps the window condition but slices by something other
     than the window variable (``slice(0)``) renders the whole grid and reports it.
     ACCEPTED RESIDUAL: the renderer check stays a structural heuristic (declaration,
     slicing function, slice argument, a call), not an execution of the renderer.
V5   a cash event that carries a native amount must reproduce its ``signed_usd`` from that
     amount and its event-time rate (or 1 for native USD); native USD 100 with signed_usd
     500 is not evidence.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from test_instrument_capital import _cash_line, _spec, _with_cash
from test_r2_measures import GOOD_APP_JS, WEEKLY, _rendered
from test_validate_generation import build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

# ----------------------------------------------------------------------------- V13


def test_a_slice_that_is_not_the_window_keeps_the_whole_grid(tmp_path: Path) -> None:
    for name, argument in (
        ("zero", "0"),
        ("fixed", "-52"),
        ("other", "-Number(someOtherWindow)"),
    ):
        js = WEEKLY.replace(
            "all.slice(-Number(selectedWeeklyWindow))", f"all.slice({argument})"
        )
        assert _rendered(tmp_path / name, GOOD_APP_JS + js) == [120_000], name


def test_a_slice_by_the_window_variable_is_bounded(tmp_path: Path) -> None:
    assert _rendered(tmp_path, GOOD_APP_JS + WEEKLY) == []
    parsed = WEEKLY.replace(
        "slice(-Number(selectedWeeklyWindow))",
        "slice(-parseInt(selectedWeeklyWindow, 10))",
    )
    assert _rendered(tmp_path / "parsed", GOOD_APP_JS + parsed) == []


# ------------------------------------------------------------------------------ V5

CASH_EVIDENCE = "capital.derived_cash_evidence"


def _event(signed: str, native: str, currency: str, rate: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "signed_usd": signed,
        "native_value": native,
        "native_currency": currency,
    }
    if rate is not None:
        event["event_time_fx"] = {
            "base_currency": "USD",
            "currency": currency,
            "rate": rate,
        }
    return event


def _line(*events: dict[str, Any]) -> dict[str, Any]:
    total = sum((Decimal(e["signed_usd"]) for e in events), Decimal(0))
    line = _cash_line(str(total))
    line["events"] = [
        {"event_date": "2025-01-01", "record_id": f"e{position}", **event}
        for position, event in enumerate(events)
    ]
    return line


def _issues(line: dict[str, Any]) -> list[str]:
    return checks.derived_cash_issues(line)[1]


def test_native_usd_100_with_signed_usd_500_is_not_accepted() -> None:
    issues = _issues(_line(_event("500", "100", "USD", None)))
    assert any("native" in problem for problem in issues), issues


def test_a_native_amount_that_reproduces_from_its_rate_is_accepted() -> None:
    native, rate = Decimal("906.03"), Decimal("1.0002319812774658")
    line = _line(
        _event("1000", "1000", "USD", None),
        _event(str(-(native * rate)), "906.03", "USDT", "1.0002319812774658"),
    )
    assert _issues(line) == []


def test_a_native_amount_with_the_wrong_rate_is_not_accepted() -> None:
    line = _line(
        _event("1000", "1000", "USD", None),
        _event("-1000", "906.03", "USDT", "1.0002319812774658"),
    )
    assert any("native" in problem for problem in _issues(line))


def test_a_foreign_native_amount_without_a_rate_is_not_accepted() -> None:
    issues = _issues(_line(_event("90", "100", "EUR", None)))
    assert any("rate" in problem for problem in issues), issues


def test_events_without_a_native_amount_are_judged_as_before() -> None:
    assert _issues(_line({"signed_usd": "1000"}, {"signed_usd": "-906.03"})) == []


def test_the_check_reports_the_native_mismatch(tmp_path: Path) -> None:
    line = _line(_event("500", "100", "USD", None))
    spec = _spec(_with_cash("5600", "6700", line))
    context = checks.Context(
        gen=load_generation(build(tmp_path, spec)),
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )
    findings = list(checks.check_derived_cash_evidence(context))
    assert [(f.check_id, f.klass) for f in findings] == [
        (CASH_EVIDENCE, checks.BLOCKER_INVARIANT)
    ]

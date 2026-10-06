"""R4 (2): custody cash evidence, the cases Astra reproduced.

* A native USD amount carries rate 1: ``native 100, signed_usd 500, rate 5`` is not evidence.
* The running dated balance never goes negative (the contract forbids negative cash): a
  withdrawal before the money arrived, replenished later, is a finding; a withdrawal listed
  before a same-day deposit that clears by day end is not (the real Robuxio shape).
* An event is counted once: a repeated ``record_id`` inside a line, the same record in two
  components, or two identical components is a finding and the repeat is not summed.
* A foreign rate must be the same for every event of one currency and date.
ACCEPTED RESIDUAL: the report carries no FX-rate table (only each event's own
``event_time_fx``), so the absolute rate is not independently recomputed; the oracle's
``capital.strategy_cash.cutoff`` row is the independent check of the amount.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_instrument_capital import _cash_line, _spec
from test_r3_render_cash import _event, _issues, _line
from test_validate_generation import build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

REAL = Path(
    "C:/Dev/q/out/stages/panel/generations/"
    "0da08a17fd1e8a342c777ba4ed91259b24684572c8804af3aa01adcb48be65a4/downloads/"
    "portfolio-report-data.json"
)


def _dated(day: str, signed: str, record: str | None = None) -> dict[str, Any]:
    event = _event(signed, str(abs(Decimal(signed))), "USD", None)
    event["event_date"] = day
    if record is not None:
        event["record_id"] = record
    return event


def _problems(line: dict[str, Any]) -> str:
    return " | ".join(_issues(line))


# --------------------------------------------------------------------- native USD


def test_a_native_usd_amount_cannot_carry_a_non_unit_rate() -> None:
    line = _line(_event("500", "100", "USD", "5"))
    assert "native USD" in _problems(line)


def test_a_native_usd_amount_with_rate_one_or_none_is_accepted() -> None:
    assert _issues(_line(_event("100", "100", "USD", "1"))) == []
    assert _issues(_line(_event("100", "100", "USD", None))) == []


# ------------------------------------------------------------------ running balance


def test_a_negative_dated_balance_followed_by_a_replenishment_is_a_finding() -> None:
    line = _line(_dated("2025-01-01", "-100", "r1"), _dated("2025-01-02", "100", "r2"))
    assert "negative" in _problems(line)


def test_a_same_day_withdrawal_listed_first_that_clears_by_day_end_is_accepted() -> (
    None
):
    line = _line(
        _dated("2025-12-05", "-49903.12", "r1"),
        _dated("2025-12-05", "49996.10", "r2"),
    )
    assert _issues(line) == []


def test_an_undated_event_cannot_be_ordered_and_is_a_finding() -> None:
    line = _line(_dated("2025-01-01", "100", "r1"))
    del line["events"][0]["event_date"]
    assert "event_date" in _problems(line)


# ----------------------------------------------------------------------- duplicates


def test_a_repeated_record_inside_a_line_is_a_finding() -> None:
    line = _line(_dated("2025-01-01", "100", "r1"), _dated("2025-01-02", "100", "r1"))
    assert "twice" in _problems(line)


def test_repeated_amounts_with_distinct_records_are_legitimate() -> None:
    line = _line(
        _dated("2025-01-01", "1000", "q:1"),
        _dated("2025-01-01", "1000", "q:2"),
        _dated("2025-01-02", "1000", "q:3"),
        _dated("2025-01-03", "-500", "q:4"),
    )
    assert _issues(line) == []


def _custody(*components: dict[str, Any]):
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "strategies": [
                {
                    "strategy_id": "beta",
                    "derived_cash": [copy.deepcopy(c) for c in components],
                }
            ]
        }

    return mutate


def _context(tmp_path: Path, *components: dict[str, Any]) -> checks.Context:
    spec = _spec(_custody(*components))
    return checks.Context(
        gen=load_generation(build(tmp_path, spec)),
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )


def _component(account: str, record: str, amount: str = "93.97") -> dict[str, Any]:
    line = _cash_line(amount)
    line["account_id"] = account
    line["events"] = [
        _dated("2025-01-01", "1000.00", f"{record}:a"),
        _dated("2025-01-02", str(Decimal(amount) - Decimal("1000.00")), f"{record}:b"),
    ]
    # the second event is a withdrawal: keep the balance non-negative on its day
    return line


def test_two_identical_components_are_a_finding_and_counted_once(
    tmp_path: Path,
) -> None:
    one = _component("CASH1", "c1")
    context = _context(tmp_path, one, one)
    findings = list(checks.check_derived_cash_evidence(context))
    assert [(f.check_id, f.klass) for f in findings] == [
        ("capital.derived_cash_evidence", checks.BLOCKER_INVARIANT)
    ]
    assert checks.derived_cash_by_strategy(context) == {"beta": Decimal("93.97")}


def test_the_same_record_in_two_differently_named_components_is_a_finding(
    tmp_path: Path,
) -> None:
    first = _component("CASH1", "c1")
    second = _component("CASH2", "c1")
    context = _context(tmp_path, first, second)
    findings = list(checks.check_derived_cash_evidence(context))
    assert [f.klass for f in findings] == [checks.BLOCKER_INVARIANT]
    assert checks.derived_cash_by_strategy(context) == {"beta": Decimal("93.97")}


def test_distinct_components_are_both_counted_and_clean(tmp_path: Path) -> None:
    context = _context(tmp_path, _component("CASH1", "c1"), _component("CASH2", "c2"))
    assert list(checks.check_derived_cash_evidence(context)) == []
    assert checks.derived_cash_by_strategy(context) == {"beta": Decimal("187.94")}


# ---------------------------------------------------------------------- FX rates


def _fx_line(account: str, record: str, rate: str) -> dict[str, Any]:
    native = Decimal("1000")
    deposit = _dated("2025-12-05", "1000", f"{record}:d")
    event = _event(str(-(native * Decimal(rate))), "1000", "USDT", rate)
    event.update({"event_date": "2025-12-05", "record_id": f"{record}:a"})
    event["event_time_fx"]["date"] = "2025-12-05"
    deposit_total = Decimal("1000") - native * Decimal(rate)
    line = _cash_line(
        str(deposit_total + native * Decimal(rate) - native * Decimal(rate))
    )
    line["account_id"] = account
    line["events"] = [_dated("2025-12-05", "2000", f"{record}:x"), event, deposit]
    total = sum((Decimal(e["signed_usd"]) for e in line["events"]), Decimal(0))
    line["current_value"]["value"] = str(total)
    return line


def test_one_currency_and_date_must_carry_one_rate(tmp_path: Path) -> None:
    context = _context(
        tmp_path, _fx_line("CASH1", "c1", "1.0002"), _fx_line("CASH2", "c2", "1.0009")
    )
    findings = list(checks.check_derived_cash_evidence(context))
    assert findings
    assert all("rate" in f.message for f in findings)


def test_the_same_rate_for_one_currency_and_date_is_clean(tmp_path: Path) -> None:
    context = _context(
        tmp_path, _fx_line("CASH1", "c1", "1.0002"), _fx_line("CASH2", "c2", "1.0002")
    )
    assert list(checks.check_derived_cash_evidence(context)) == []


# ------------------------------------------------------------------ the real report


@pytest.mark.skipif(
    not REAL.exists(), reason="the real generation is not on this machine"
)
def test_the_real_cash_lines_still_validate() -> None:
    report = json.loads(REAL.read_bytes())
    context = checks.Context(
        gen=None,  # type: ignore[arg-type]
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )
    context.gen = type("G", (), {"report": report})()  # type: ignore[assignment]
    assert list(checks.check_derived_cash_evidence(context)) == []
    cash = checks.derived_cash_by_strategy(context)
    assert cash == {
        "robuxio-crypto": Decimal("93.97474435567453281"),
        "volatis-strategy": Decimal("1879.943950216"),
    }

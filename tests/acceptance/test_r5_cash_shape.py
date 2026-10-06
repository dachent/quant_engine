"""R5: a cash event missing a required field is not verified cash.

``schemas/portfolio/report-data.schema.json`` requires every custody cash event to carry
``event_date``, ``record_id``, ``event_sha256``, ``native_currency``, ``native_value``,
``event_time_fx`` (an object or null) and ``signed_usd``.  An event whose native amount or
currency is missing, null, boolean, non-numeric or non-finite used to skip the native and
USD-rate verification and the line still counted as verified.  Each such event is now a
cash-evidence finding and the line is excluded from the verified total, so aggregates and
the strategy-cash metric cannot rest on it.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_instrument_capital import _cash_line, _spec
from test_validate_generation import build

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

REAL = Path(
    "C:/Dev/q/out/stages/panel/generations/"
    "0da08a17fd1e8a342c777ba4ed91259b24684572c8804af3aa01adcb48be65a4/downloads/"
    "portfolio-report-data.json"
)
SHA = "a" * 64
GONE = object()


def _event(
    day: str, signed: str, record: str, native: str | None = None, **changes: Any
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_date": day,
        "record_id": record,
        "event_sha256": SHA,
        "native_currency": "USD",
        "native_value": native if native is not None else str(abs(Decimal(signed))),
        "event_time_fx": None,
        "signed_usd": signed,
    }
    for key, value in changes.items():
        if value is GONE:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def _line(*events: dict[str, Any]) -> dict[str, Any]:
    total = sum((Decimal(e["signed_usd"]) for e in events), Decimal(0))
    line = _cash_line(str(total))
    line["events"] = list(events)
    return line


def _good() -> dict[str, Any]:
    return _line(
        _event("2025-01-01", "1000", "r:a"), _event("2025-01-02", "-906.03", "r:b")
    )


def _with_event(**changes: Any) -> dict[str, Any]:
    line = _good()
    line["events"][1] = _event("2025-01-02", "-906.03", "r:b", **changes)
    return line


def _problems(line: dict[str, Any]) -> str:
    return " | ".join(checks.derived_cash_issues(line)[1])


def test_the_complete_event_shape_is_accepted() -> None:
    assert checks.derived_cash_issues(_good())[1] == []


BAD = {
    "native_value": [
        ("missing", GONE),
        ("null", None),
        ("string", "abc"),
        ("empty", ""),
        ("bool", True),
        ("number", 906.03),
        ("nan", "NaN"),
        ("inf", "Infinity"),
        ("neg-inf", "-Infinity"),
    ],
    "native_currency": [
        ("missing", GONE),
        ("null", None),
        ("empty", ""),
        ("int", 1),
        ("bool", False),
    ],
    "signed_usd": [
        ("missing", GONE),
        ("null", None),
        ("bool", True),
        ("nan", "NaN"),
        ("inf", "Infinity"),
        ("word", "lots"),
    ],
    "record_id": [("missing", GONE), ("null", None), ("empty", ""), ("int", 7)],
    "event_sha256": [
        ("missing", GONE),
        ("null", None),
        ("short", "abc"),
        ("upper", "A" * 64),
        ("int", 1),
    ],
    "event_date": [
        ("missing", GONE),
        ("null", None),
        ("malformed", "2025-1-2"),
        ("number", 20250102),
    ],
    "event_time_fx": [("absent", GONE), ("string", "1.0"), ("list", [])],
}


@pytest.mark.parametrize(
    ("field", "label", "value"),
    [(field, label, value) for field, cases in BAD.items() for label, value in cases],
)
def test_an_event_missing_or_malforming_a_required_field_is_not_verified(
    field: str, label: str, value: Any
) -> None:
    line = _with_event(**{field: value})
    amount, problems = checks.derived_cash_issues(line)
    assert problems, (field, label)
    assert field in " ".join(problems), (field, label, problems)


def _context(tmp_path: Path, *components: dict[str, Any]) -> checks.Context:
    def mutate(report: dict[str, Any]) -> None:
        report["custody_current_capital"] = {
            "strategies": [
                {
                    "strategy_id": "beta",
                    "derived_cash": [copy.deepcopy(c) for c in components],
                }
            ]
        }

    return checks.Context(
        gen=load_generation(build(tmp_path, _spec(mutate))),
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"native_value": GONE},
        {"native_value": None},
        {"native_value": "abc"},
        {"native_value": True},
        {"native_value": "NaN"},
        {"native_currency": GONE},
        {"signed_usd": "Infinity"},
    ],
)
def test_the_line_is_excluded_from_the_verified_total_and_reported(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    context = _context(tmp_path, _with_event(**changes))
    findings = list(checks.check_derived_cash_evidence(context))
    assert [(f.check_id, f.klass) for f in findings] == [
        ("capital.derived_cash_evidence", checks.BLOCKER_INVARIANT)
    ]
    assert checks.derived_cash_by_strategy(context) == {}
    assert checks.resolve_metric(context, "capital.strategy_cash.cutoff", "beta") == []


def test_a_complete_line_is_counted_and_clean(tmp_path: Path) -> None:
    context = _context(tmp_path, _good())
    assert list(checks.check_derived_cash_evidence(context)) == []
    assert checks.derived_cash_by_strategy(context) == {"beta": Decimal("93.97")}


@pytest.mark.skipif(
    not REAL.exists(), reason="the real generation is not on this machine"
)
def test_the_real_cash_lines_still_validate_and_are_counted() -> None:
    report = json.loads(REAL.read_bytes())
    context = checks.Context(
        gen=None,  # type: ignore[arg-type]
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
    )
    context.gen = type("G", (), {"report": report})()  # type: ignore[assignment]
    assert list(checks.check_derived_cash_evidence(context)) == []
    assert checks.derived_cash_by_strategy(context) == {
        "robuxio-crypto": Decimal("93.97474435567453281"),
        "volatis-strategy": Decimal("1879.943950216"),
    }

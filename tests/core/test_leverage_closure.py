from __future__ import annotations

from decimal import Decimal
from typing import Any

import leverage_closure as lc

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def _amt(value: str, currency: str = "USD") -> dict[str, str]:
    return {"currency": currency, "status": "AVAILABLE", "value": value}


def _accrual(
    day: str, basis: str, value: str, mapping: str = "MAPPED"
) -> dict[str, Any]:
    return {
        "classification": "INTEREST_ACCRUAL_UNCLASSIFIED",
        "measurement_basis": basis,
        "period_end": day,
        "period_start": day,
        "source_amount": _amt(value),
        "mapping_status": mapping,
    }


def _series(balances: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous = Decimal(0)
    for day, text in zip(DAYS, balances, strict=True):
        balance = Decimal(text)
        rows.append(_accrual(day, "ACCRUAL_BALANCE", text))
        rows.append(_accrual(day, "PERIOD_ACCRUAL", str(balance - previous)))
        rows.append(_accrual(day, "ACCRUAL_REVERSAL", "0"))
        previous = balance
    return rows


def test_a_rolling_accrual_balance_closes() -> None:
    result = lc.financing_cost_closure(
        _series(["-1", "-2", "-3", "-4"]), DAYS, DAYS[-1], None
    )
    assert result.covered
    assert result.evidence["rollforward_steps_checked"] == 3


def test_a_balance_step_no_accrual_explains_is_not_covered() -> None:
    """Kill test (#894): the accrual moves with no accrued interest or reversal."""

    rows = _series(["-1", "-2", "-3", "-4"])
    for row in rows:
        if (
            row["period_end"] == DAYS[2]
            and row["measurement_basis"] == "ACCRUAL_BALANCE"
        ):
            row["source_amount"] = _amt("-50")
        if (
            row["period_end"] == DAYS[2]
            and row["measurement_basis"] == "PERIOD_ACCRUAL"
        ):
            row["source_amount"] = _amt("-1")
    result = lc.financing_cost_closure(rows, DAYS, DAYS[-1], None)
    assert not result.covered
    assert result.evidence["unexplained_step_count"] >= 1


def test_a_gap_day_may_carry_only_what_the_missing_days_could_accrue() -> None:
    rows = [r for r in _series(["-1", "-2", "-3", "-4"]) if r["period_end"] != DAYS[1]]
    # one missing day, largest observed daily accrual is 1: a step of 2 is allowed
    assert lc.financing_cost_closure(rows, DAYS, DAYS[-1], None).covered


def test_an_unmapped_posting_inside_the_epoch_is_not_covered_but_before_it_is() -> None:
    rows = _series(["-1", "-2", "-3", "-4"])
    rows.append(
        {
            "classification": "BROKER_DEBIT_INTEREST",
            "measurement_basis": "VALUE_DATE_INTEREST_DETAIL",
            "value_date": "2026-01-06",
            "accounting_date": "2026-01-07",
            "source_amount": _amt("-1"),
            "mapping_status": "UNMAPPED",
        }
    )
    assert not lc.financing_cost_closure(rows, DAYS, DAYS[-1], "2026-01-05").covered
    assert lc.financing_cost_closure(rows, DAYS, DAYS[-1], "2026-01-07").covered


def test_the_base_line_is_ignored_when_currency_lines_exist() -> None:
    rows = _series(["-1", "-2", "-3", "-4"])
    for row in _series(["-1", "-9", "-3", "-4"]):
        row["source_amount"] = _amt(row["source_amount"]["value"], lc.BASE_LINE)
        rows.append(row)
    assert lc.financing_cost_closure(rows, DAYS, DAYS[-1], None).covered


def _cash(day: str, value: str, level: str = "BASE_SUMMARY") -> dict[str, Any]:
    return {
        "aggregation_level": level,
        "measurement_basis": "TRADE_DATE_CASH_BALANCE",
        "as_of_date": day,
        "amount": _amt(value),
    }


def test_borrowed_principal_is_the_negative_net_cash_at_the_cutoff() -> None:
    rows = [_cash(d, "-10") for d in DAYS] + [_cash(DAYS[0], "999", "CURRENCY")]
    closure, principal = lc.borrowed_principal(rows, DAYS, DAYS[-1], "USD")
    assert closure.covered
    assert principal == Decimal(10)
    positive = [_cash(d, "5") for d in DAYS]
    assert lc.borrowed_principal(positive, DAYS, DAYS[-1], "USD")[1] == Decimal(0)


def test_a_missing_statement_day_in_the_cash_series_is_not_covered() -> None:
    """Kill test (#894): the loan series skips a statement day."""

    rows = [_cash(d, "-10") for d in DAYS if d != DAYS[1]]
    closure, _ = lc.borrowed_principal(rows, DAYS, DAYS[-1], "USD")
    assert not closure.covered
    assert closure.evidence["missing_days"] == 1
    assert lc.borrowed_principal([], DAYS, DAYS[-1], "USD")[1] is None


def test_gross_exposure_needs_one_mapped_value_and_one_nav() -> None:
    exposure = [
        {"as_of_date": DAYS[-1], "mapping_status": "MAPPED", "amount": _amt("120")}
    ]
    nav = [{"as_of_date": DAYS[-1], "mapping_status": "MAPPED", "nav": _amt("100")}]
    closure, gross, value = lc.gross_exposure(exposure, nav, DAYS[-1])
    assert closure.covered and gross == Decimal(120) and value == Decimal(100)
    assert not lc.gross_exposure([], nav, DAYS[-1])[0].covered

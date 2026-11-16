from __future__ import annotations

from datetime import date
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


def test_an_unmapped_posting_withholds_the_closure_and_is_explained() -> None:
    """Kill test (R6): a cost row the strategy mapping cannot place is a mapping gap.
    No ruling relaxes it, so the account is not covered; the reason stays in the evidence."""

    rows = _series(["-1", "-2", "-3", "-4"])
    assert lc.financing_cost_closure(rows, DAYS, DAYS[-1], "2026-01-05").covered
    rows.append(
        {
            "classification": "BROKER_DEBIT_INTEREST",
            "measurement_basis": "VALUE_DATE_INTEREST_DETAIL",
            "value_date": "2026-01-06",
            "accounting_date": "2026-01-07",
            "source_amount": _amt("-1"),
            "mapping_status": "UNMAPPED",
            "mapping_reason": "PERIOD_MAPPING_MISSING_VALIDATED_INTERVAL",
        }
    )
    result = lc.financing_cost_closure(rows, DAYS, DAYS[-1], "2026-01-05")
    assert not result.covered
    assert result.evidence["unmapped_rows_in_epoch"] == 1
    assert result.evidence["unmapped_reasons"] == {
        "PERIOD_MAPPING_MISSING_VALIDATED_INTERVAL": 1
    }
    # dated before the strategy epoch, the same row is outside the window that matters
    assert lc.financing_cost_closure(rows, DAYS, DAYS[-1], "2026-01-08").covered


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


def test_days_after_the_last_accrual_row_are_not_covered_while_the_balance_is_open() -> (
    None
):
    """Review of #894: a trailing gap needs a closed-out accrual."""

    rows = [r for r in _series(["-1", "-2", "-3", "-4"]) if r["period_end"] <= DAYS[1]]
    assert not lc.financing_cost_closure(rows, DAYS, DAYS[-1], None).covered
    closed = [r for r in _series(["-1", "0", "0", "0"]) if r["period_end"] <= DAYS[1]]
    assert lc.financing_cost_closure(closed, DAYS, DAYS[-1], None).covered


def test_an_unavailable_cash_day_counts_as_missing_not_as_absent_history() -> None:
    rows = [_cash(d, "-10") for d in DAYS]
    rows[0]["amount"] = {"currency": "USD", "status": "UNAVAILABLE", "value": None}
    closure, _ = lc.borrowed_principal(rows, DAYS, DAYS[-1], "USD")
    assert not closure.covered
    assert closure.evidence["missing_days"] == 1


def test_an_unmapped_cutoff_exposure_or_nav_does_not_close() -> None:
    """Kill test (R6): gross exposure and NAV at the cutoff must both be MAPPED."""

    exposure = [
        {
            "as_of_date": "2026-01-07",
            "mapping_status": "MAPPED",
            "amount": _amt("140"),
        }
    ]
    nav = [{"as_of_date": "2026-01-07", "mapping_status": "MAPPED", "nav": _amt("100")}]
    closure, gross, value = lc.gross_exposure(exposure, nav, "2026-01-07")
    assert closure.covered and (gross, value) == (140, 100)
    for rows in (exposure, nav):
        unmapped = [{**rows[0], "mapping_status": "UNMAPPED"}]
        args = (unmapped, nav) if rows is exposure else (exposure, unmapped)
        assert not lc.gross_exposure(*args, "2026-01-07")[0].covered


def test_a_provider_without_an_accrual_series_is_not_covered_by_its_postings() -> None:
    """Kill test (R6): postings alone are not an accrual roll-forward.  A provider that
    states cost only as posted cash (Schwab) stays uncovered until a governed ruling
    names a different closure."""

    posting = {
        "classification": "BROKER_DEBIT_INTEREST",
        "measurement_basis": "CASH_POSTING",
        "source_amount": _amt("-11.26"),
        "mapping_status": "MAPPED",
    }
    result = lc.financing_cost_closure([posting, posting], DAYS, DAYS[-1], None)
    assert not result.covered
    assert result.check_id == "FINANCING_COST_ACCRUAL_ROLLFORWARD"


# ---- RESERVE_WINDOW row class (control ruling 2026-11-13): attribution only through the
# declared window, never inferred.

WINDOW = [(date(2026, 1, 5), date(2026, 1, 8), "EXACT_IDENTITY_CLOSURE")]


def _reserve_series(reason: str | None = "PERIOD_MAPPING_MISSING_VALIDATED_INTERVAL"):
    rows = _series(["-1", "-2", "-3", "-4"])
    for row in rows:
        row["mapping_status"] = "UNMAPPED"
        row["mapping_reason"] = reason
    return rows


def test_a_declared_reserve_window_attributes_unmapped_cost_rows() -> None:
    rows = _reserve_series()
    assert not lc.financing_cost_closure(rows, DAYS, DAYS[-1], None).covered
    closed = lc.financing_cost_closure(rows, DAYS, DAYS[-1], None, WINDOW)
    assert closed.covered
    assert closed.evidence["reserve_window_rows"] == len(rows)
    assert closed.evidence["unmapped_rows_in_epoch"] == 0


def test_an_undeclared_or_too_short_reserve_window_stays_blocked() -> None:
    rows = _reserve_series()
    for windows in (None, [], [(date(2026, 1, 5), date(2026, 1, 7), "X")]):
        assert not lc.financing_cost_closure(
            rows, DAYS, DAYS[-1], None, windows
        ).covered
    gap = [
        (date(2026, 1, 5), date(2026, 1, 5), "X"),
        (date(2026, 1, 7), date(2026, 1, 8), "X"),
    ]
    period = {**rows[0], "period_start": "2026-01-05", "period_end": "2026-01-07"}
    assert not lc.in_reserve_window(period, gap)


def test_strategy_windows_are_untouched_by_the_reserve_rule() -> None:
    row = _reserve_series()[0]
    assert lc.in_reserve_window(row, WINDOW)
    for change in (
        {"mapping_reason": "PERIOD_MAPPING_AMBIGUOUS_INTERVAL"},
        {"mapping_reason": "PERIOD_CROSSES_STRATEGY_EPOCH"},
        {"mapping_reason": "PERIOD_MAPPING_BOUNDARY_MISSING"},
        {"strategy_id": "s"},
    ):
        assert not lc.in_reserve_window({**row, **change}, WINDOW)
    mapped = {**row, "mapping_status": "MAPPED", "strategy_id": "s"}
    assert not lc.in_reserve_window(mapped, WINDOW)
    assert lc.financing_cost_closure(
        _series(["-1", "-2", "-3", "-4"]), DAYS, DAYS[-1], None, []
    ).covered


def test_reserve_cutoff_exposure_and_nav_close_only_inside_the_window() -> None:
    exposure = [
        {
            "as_of_date": "2026-01-07",
            "mapping_status": "UNMAPPED",
            "mapping_reason": "PERIOD_MAPPING_MISSING_VALIDATED_INTERVAL",
            "amount": _amt("140"),
        }
    ]
    nav = [{**exposure[0], "nav": _amt("100")}]
    assert not lc.gross_exposure(exposure, nav, "2026-01-07")[0].covered
    assert lc.gross_exposure(exposure, nav, "2026-01-07", WINDOW)[0].covered
    outside = [(date(2026, 1, 8), date(2026, 1, 9), "X")]
    assert not lc.gross_exposure(exposure, nav, "2026-01-07", outside)[0].covered


def test_a_source_scope_ambiguity_is_never_a_reserve_row() -> None:
    """The IBKR exposure row of a reserve says SOURCE_ACCOUNT_SCOPE_UNMAPPED /
    AMBIGUOUS_REPORTED_ACCOUNT: the figure's account is unknown, not a missing strategy
    interval, so the declared window never attributes it."""

    row = {**_reserve_series()[0], "mapping_reason": "SOURCE_ACCOUNT_SCOPE_UNMAPPED"}
    assert not lc.in_reserve_window(row, WINDOW)


def test_a_row_with_no_stated_reason_or_an_ambiguous_point_is_never_a_reserve_row() -> (
    None
):
    """Kill test (round-2 R6 item 3): the ledger now states why a point row is unmapped;
    no reason, or two active strategies, never places the row in a reserve window."""

    row = _reserve_series()[0]
    assert lc.in_reserve_window(
        {**row, "mapping_reason": "MAPPING_NO_ACTIVE_INTERVAL"}, WINDOW
    )
    for reason in (None, "MAPPING_AMBIGUOUS_POINT", "MAPPING_ACCOUNT_OR_DAY_UNKNOWN"):
        assert not lc.in_reserve_window({**row, "mapping_reason": reason}, WINDOW)


def test_a_real_shaped_reserve_nav_row_is_placed_only_with_its_ledger_reason() -> None:
    """Kill test (round-3): the NAV row is shaped like a real ledger row (its own reason,
    never the exposure row's).  No reason fails closed; an ambiguous point never places."""

    exposure = [
        {
            "as_of_date": "2026-01-07",
            "mapping_status": "MAPPED",
            "amount": _amt("140"),
        }
    ]
    nav_row = {
        "as_of_date": "2026-01-07",
        "mapping_status": "UNMAPPED",
        "nav": _amt("100"),
    }

    def covered(**extra: str) -> bool:
        return lc.gross_exposure(
            exposure, [{**nav_row, **extra}], "2026-01-07", WINDOW
        )[0].covered

    assert covered(mapping_reason="MAPPING_NO_ACTIVE_INTERVAL")
    assert not covered()
    assert not covered(mapping_reason="MAPPING_AMBIGUOUS_POINT")


def test_a_posting_is_placed_only_if_every_date_it_carries_is_inside_the_window() -> (
    None
):
    """Kill test (round-4 Astra): a Schwab margin-interest posting is mapped by its
    effective date.  Event 01-14 inside the window but effective 01-15 outside it must
    stay unmapped and keep counting as a mapping gap; both inside places it."""

    window = [(date(2026, 1, 1), date(2026, 1, 14), "EXACT_IDENTITY_CLOSURE")]
    days = ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15"]
    posting = {
        "classification": "BROKER_DEBIT_INTEREST",
        "measurement_basis": "CASH_POSTING",
        "source_amount": _amt("-1"),
        "mapping_status": "UNMAPPED",
        "mapping_reason": "MAPPING_NO_ACTIVE_INTERVAL",
        "event_date": "2026-01-14",
        "effective_date": "2026-01-15",
    }
    assert not lc.in_reserve_window(posting, window)
    result = lc.financing_cost_closure([posting], days, "2026-01-16", None, window)
    assert result.evidence["reserve_window_rows"] == 0
    assert result.evidence["unmapped_rows_in_epoch"] == 1
    inside = {**posting, "effective_date": "2026-01-14"}
    assert lc.in_reserve_window(inside, window)
    assert (
        lc.financing_cost_closure([inside], days, "2026-01-16", None, window).evidence[
            "reserve_window_rows"
        ]
        == 1
    )


def _empty_day(day: str) -> dict[str, Any]:
    return {
        "classification": "NO_ACCRUALS_REPORTED",
        "measurement_basis": "ACCRUAL_SECTION_EMPTY",
        "as_of_date": day,
        "period_end": day,
        "amount": _amt("0"),
        "source_amount": _amt("0"),
        "source_account_scope": "STATEMENT_ACCOUNT",
        "source_section": "InterestAccruals",
        "mapping_status": "MAPPED",
    }


def test_an_explicit_empty_accrual_section_on_every_day_closes_the_cost() -> None:
    rows = [_empty_day(d) for d in DAYS]
    closed = lc.financing_cost_closure(rows, DAYS, DAYS[-1], None)
    assert closed.covered and closed.check_id == "FINANCING_COST_ACCRUAL_SECTION_EMPTY"
    # the unmapped section rows never count as mapping gaps
    assert closed.evidence["empty_section_days"] == len(DAYS)


def test_a_day_with_no_section_or_any_other_cost_row_is_not_covered() -> None:
    assert not lc.financing_cost_closure(
        [_empty_day(d) for d in DAYS[:-1]], DAYS, DAYS[-1], None
    ).covered
    posting = {
        "classification": "BROKER_DEBIT_INTEREST",
        "measurement_basis": "CASH_POSTING",
        "source_amount": _amt("-1"),
        "mapping_status": "MAPPED",
    }
    assert not lc.financing_cost_closure(
        [_empty_day(d) for d in DAYS] + [posting], DAYS, DAYS[-1], None
    ).covered
    assert not lc.financing_cost_closure([], DAYS, DAYS[-1], None).covered


def test_a_levered_account_needs_an_explicitly_stated_zero_accrual_per_day() -> None:
    """Kill test (round-5): for a borrowing account an empty accrual section closes a day
    only when the statement's NAV states the accrual as an explicit zero."""

    def day_row(day: str, stated: bool) -> dict[str, Any]:
        return {**_empty_day(day), "nav_interest_accruals_stated": stated}

    silent = [day_row(d, False) for d in DAYS]
    stated = [day_row(d, True) for d in DAYS]
    assert lc.financing_cost_closure(silent, DAYS, DAYS[-1], None).covered
    assert not lc.financing_cost_closure(
        silent, DAYS, DAYS[-1], None, require_explicit_zero=True
    ).covered
    assert lc.financing_cost_closure(
        stated, DAYS, DAYS[-1], None, require_explicit_zero=True
    ).covered


def test_a_malformed_or_unmapped_empty_section_row_never_closes_the_cost() -> None:
    """Kill tests (round-5 c): the empty-section rows are held to the roll-forward path's
    standard: AVAILABLE, exactly 0, statement account, the section, and placed."""

    def covered(rows: list[dict[str, Any]], windows: Any = None) -> bool:
        return lc.financing_cost_closure(rows, DAYS, DAYS[-1], None, windows).covered

    good = [_empty_day(d) for d in DAYS]
    assert covered(good)
    for change in (
        {"amount": {"currency": "USD", "status": "UNAVAILABLE", "value": None}},
        {"amount": _amt("0.01")},
        {"source_account_scope": "AMBIGUOUS_REPORTED_ACCOUNT"},
        {"source_section": "CashReport"},
    ):
        assert not covered([{**good[0], **change}, *good[1:]]), change
    unmapped = [{**r, "mapping_status": "UNMAPPED"} for r in good]
    assert not covered(unmapped)
    window = [(date(2026, 1, 5), date(2026, 1, 8), "EXACT_IDENTITY_CLOSURE")]
    reasoned = [{**r, "mapping_reason": "MAPPING_NO_ACTIVE_INTERVAL"} for r in unmapped]
    assert covered(reasoned, window)


def test_a_flawed_empty_row_in_a_mixed_history_never_closes_the_cost() -> None:
    """Kill test (final round P1-A): ordinary zero accruals on some days plus an empty row
    on another day: the empty row is validated on this path too (unmapped, unstated,
    malformed rows all refuse), exactly as when no ordinary accrual exists."""

    days = ["2026-01-05", "2026-01-06", "2026-01-07"]

    def zero_accrual(day: str) -> list[dict[str, Any]]:
        return [
            _accrual(day, "ACCRUAL_BALANCE", "0"),
            _accrual(day, "PERIOD_ACCRUAL", "0"),
            _accrual(day, "ACCRUAL_REVERSAL", "0"),
        ]

    def history(last: dict[str, Any]) -> list[dict[str, Any]]:
        return [*zero_accrual(days[0]), *zero_accrual(days[1]), last]

    good = {**_empty_day(days[2]), "nav_interest_accruals_stated": True}
    assert lc.financing_cost_closure(
        history(good), days, days[-1], None, require_explicit_zero=True
    ).covered
    for flaw in (
        {"mapping_status": "UNMAPPED"},
        {"nav_interest_accruals_stated": False},
        {"amount": _amt("0.5")},
        {"source_account_scope": "AMBIGUOUS_REPORTED_ACCOUNT"},
    ):
        result = lc.financing_cost_closure(
            history({**good, **flaw}), days, days[-1], None, require_explicit_zero=True
        )
        assert not result.covered, flaw


def test_a_contradictory_source_amount_makes_an_empty_row_malformed() -> None:
    """Kill test (final round): source_amount, when present, must be the AVAILABLE zero as
    well; a contradictory amount/source_amount pair never closes the cost."""

    good = [_empty_day(d) for d in DAYS]
    assert all("source_amount" in r for r in good)
    assert lc.financing_cost_closure(good, DAYS, DAYS[-1], None).covered
    for contradicted in (
        _amt("-3"),
        {"currency": "USD", "status": "UNAVAILABLE", "value": None},
    ):
        rows = [{**good[0], "source_amount": contradicted}, *good[1:]]
        assert not lc.financing_cost_closure(rows, DAYS, DAYS[-1], None).covered


def test_the_unstated_count_and_the_flawed_row_count_are_row_counts() -> None:
    """Kill test (final round F2/F3): the evidence names unstated rows, and the flawed
    count is distinct rows (a row both unmapped and unstated counts once)."""

    good = [{**_empty_day(d), "nav_interest_accruals_stated": True} for d in DAYS]
    both = {
        **good[0],
        "mapping_status": "UNMAPPED",
        "nav_interest_accruals_stated": False,
    }
    # the unstated duplicate on a day that also has a good row: covered=False, with a counter
    rows = [both, *good[1:], {**good[0]}]
    result = lc.financing_cost_closure(
        rows, DAYS, DAYS[-1], None, require_explicit_zero=True
    )
    assert not result.covered
    assert result.evidence["empty_rows_unstated"] == 1
    mixed = [_accrual(DAYS[0], "ACCRUAL_BALANCE", "0"), both]
    rolled = lc.financing_cost_closure(
        mixed, DAYS, DAYS[-1], None, require_explicit_zero=True
    )
    assert rolled.evidence["empty_section_rows_flawed"] == 1

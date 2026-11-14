from __future__ import annotations

import json
from typing import Any

import account_financing as af
import leverage_disclosure as ld

LEVERED = "account-" + "a" * 16
QUIET = "account-" + "b" * 16
DEBIT = "account-" + "c" * 16
DAYS = ["2026-01-05", "2026-01-06", "2026-01-07"]
CUTOFF = DAYS[-1]
SHA = "d" * 64


def _amt(value: str, currency: str = "USD") -> dict[str, str]:
    return {"currency": currency, "status": "AVAILABLE", "value": value}


def _declaration() -> dict[str, af.Declared]:
    rows = [
        (LEVERED, "MARGIN_USED"),
        (QUIET, "NO_MARGIN_FACILITY"),
        (DEBIT, "NO_MARGIN_FACILITY"),
    ]
    return af.load_declaration(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": "account-financing-declaration",
                "as_of_date": CUTOFF,
                "accounts": [
                    {
                        "account_id": a,
                        "scope": "ACTIVE",
                        "facility": f,
                        "verification": "VERIFIED",
                        "evidence_ref": "owner",
                    }
                    for a, f in rows
                ],
            }
        ).encode()
    )


def _catalog(*, base_cash: bool, break_accrual: bool = False) -> dict[str, Any]:
    nav = [
        {
            "account_id": a,
            "as_of_date": d,
            "provider_id": "ibkr",
            "strategy_id": "s",
            "mapping_status": "MAPPED",
            "nav": _amt("100"),
        }
        for a in (LEVERED, QUIET, DEBIT)
        for d in DAYS
    ]
    cost: list[dict[str, Any]] = []
    previous = 0
    for day, balance in zip(DAYS, (-1, -2, -3), strict=True):
        for basis, value in (
            (
                "ACCRUAL_BALANCE",
                balance if not (break_accrual and day == DAYS[2]) else -90,
            ),
            ("PERIOD_ACCRUAL", balance - previous),
            ("ACCRUAL_REVERSAL", 0),
        ):
            cost.append(
                {
                    "account_id": LEVERED,
                    "provider_id": "ibkr",
                    "classification": "INTEREST_ACCRUAL_UNCLASSIFIED",
                    "measurement_basis": basis,
                    "period_end": day,
                    "source_amount": _amt(str(value)),
                    "mapping_status": "MAPPED",
                }
            )
        previous = balance
    cost.append(
        {
            "account_id": DEBIT,
            "provider_id": "ibkr",
            "classification": "BROKER_DEBIT_INTEREST",
            "measurement_basis": "CASH_POSTING",
            "source_amount": _amt("-1"),
            "mapping_status": "MAPPED",
            "accounting_date": DAYS[1],
        }
    )
    liability = (
        [
            {
                "account_id": LEVERED,
                "provider_id": "ibkr",
                "aggregation_level": "BASE_SUMMARY",
                "measurement_basis": "TRADE_DATE_CASH_BALANCE",
                "classification": "NET_CASH_DEBIT_BALANCE",
                "as_of_date": d,
                "amount": _amt("-40"),
            }
            for d in DAYS
        ]
        if base_cash
        else []
    )
    if base_cash:
        # the quiet and incidental accounts have observed cash days with no debit
        liability += [
            {
                "account_id": a,
                "provider_id": "ibkr",
                "aggregation_level": "BASE_SUMMARY",
                "measurement_basis": "TRADE_DATE_CASH_BALANCE",
                "classification": "NET_CASH_CREDIT_BALANCE",
                "as_of_date": d,
                "amount": _amt("10"),
            }
            for a in (QUIET, DEBIT)
            for d in DAYS
        ]
    exposure = [
        {
            "account_id": LEVERED,
            "provider_id": "ibkr",
            "as_of_date": CUTOFF,
            "mapping_status": "MAPPED",
            "amount": _amt("140"),
        }
    ]
    return {
        "nav_observations": nav,
        "financing_cost_observations": cost,
        "financing_liability_observations": liability,
        "leverage_exposure_observations": exposure,
        "positions": [],
    }


def _rows(**kwargs: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = ld.account_financing_rows(
        _declaration(),
        _catalog(**kwargs),
        cutoff=CUTOFF,
        generation_id=SHA,
        catalog_sha256=SHA,
    )
    return {(r["account_id"], r["data_class"], r["measurement_basis"]): r for r in rows}


def test_a_levered_account_gets_covered_loan_cost_and_exposure_rows() -> None:
    rows = _rows(base_cash=True)
    loan = rows[(LEVERED, "FINANCING_LIABILITY", "BORROWED_PRINCIPAL")]
    assert loan["coverage_status"] == "COMPLETE_RECONCILED"
    assert loan["amount"]["value"] == "40"
    cost = rows[(LEVERED, "FINANCING_COST", "RECONCILED_FINANCING_COST")]
    assert cost["coverage_status"] == "COMPLETE_RECONCILED"
    gross = rows[(LEVERED, "LEVERAGE_EXPOSURE", "GROSS_EXPOSURE")]
    assert gross["amount"]["value"] == "140"
    ratio = rows[(LEVERED, "LEVERAGE_EXPOSURE", "GROSS_EXPOSURE_OVER_NAV")]
    assert ratio["amount"]["value"] == "1.4000"
    state = rows[(LEVERED, "ACCOUNT_FINANCING", "ACCOUNT_FINANCING_STATE")]
    assert state["classification"] == "LEVERED"


def test_a_dropped_loan_is_not_covered_and_has_no_amount() -> None:
    """Kill test (#894): the old ledger had no base-level loan row."""

    loan = _rows(base_cash=False)[
        (LEVERED, "FINANCING_LIABILITY", "BORROWED_PRINCIPAL")
    ]
    assert loan["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert loan["amount"]["value"] is None


def test_an_accrual_that_does_not_roll_forward_is_not_covered() -> None:
    cost = _rows(base_cash=True, break_accrual=True)[
        (LEVERED, "FINANCING_COST", "RECONCILED_FINANCING_COST")
    ]
    assert cost["coverage_status"] == "OBSERVED_UNRECONCILED"


def test_a_non_levered_account_is_an_explicit_row_and_a_denied_debit_is_a_conflict() -> (
    None
):
    rows = _rows(base_cash=True)
    quiet = rows[(QUIET, "ACCOUNT_FINANCING", "ACCOUNT_FINANCING_STATE")]
    assert quiet["classification"] == "NOT_LEVERED"
    assert quiet["coverage_status"] == "COMPLETE_DECLARED"
    assert not any(key[0] == QUIET and key[1] != "ACCOUNT_FINANCING" for key in rows)
    denied = rows[(DEBIT, "ACCOUNT_FINANCING", "ACCOUNT_FINANCING_STATE")]
    assert denied["classification"] == "INCIDENTAL_DEBIT"
    assert denied["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "DECLARED_NO_FACILITY_BUT_DEBIT_INTEREST" in denied["coverage_reason"]


def test_a_row_after_the_cutoff_does_not_move_the_state_and_unverified_is_visible() -> (
    None
):
    catalog = _catalog(base_cash=True)
    catalog["financing_cost_observations"].append(
        {
            "account_id": QUIET,
            "provider_id": "ibkr",
            "classification": "BROKER_DEBIT_INTEREST",
            "measurement_basis": "CASH_POSTING",
            "source_amount": _amt("-1"),
            "mapping_status": "MAPPED",
            "accounting_date": "2026-01-09",
        }
    )
    declaration = _declaration()
    declaration[QUIET] = af.Declared(
        QUIET, "ACTIVE", "NO_MARGIN_FACILITY", False, "owner"
    )
    rows = ld.account_financing_rows(
        declaration, catalog, cutoff=CUTOFF, generation_id=SHA, catalog_sha256=SHA
    )
    state = next(r for r in rows if r["account_id"] == QUIET)
    assert state["classification"] == "NOT_LEVERED"
    assert state["coverage_reason"].endswith("VERIFICATION=UNVERIFIED")


def _quiet_state(catalog: dict[str, Any], declaration=None) -> dict[str, Any]:
    rows = ld.account_financing_rows(
        declaration or _declaration(),
        catalog,
        cutoff=CUTOFF,
        generation_id=SHA,
        catalog_sha256=SHA,
    )
    return next(r for r in rows if r["account_id"] == QUIET)


def _quiet_debit(value: str, day_index: int = 0) -> dict[str, Any]:
    return {
        "account_id": QUIET,
        "provider_id": "ibkr",
        "aggregation_level": "BASE_SUMMARY",
        "measurement_basis": "TRADE_DATE_CASH_BALANCE",
        "classification": "NET_CASH_DEBIT_BALANCE",
        "as_of_date": DAYS[day_index],
        "amount": _amt(value),
    }


def test_a_dust_net_base_debit_is_still_borrowing() -> None:
    """Kill test (R6): no ruling makes a small net debit not-borrowing."""

    catalog = _catalog(base_cash=True)
    catalog["financing_liability_observations"].append(_quiet_debit("-0.0016"))
    state = _quiet_state(catalog)
    assert state["classification"] == "LEVERED"
    assert "DECLARED_NO_FACILITY_BUT_NET_BASE_DEBIT" in state["coverage_reason"]


def test_an_unreadable_debit_row_is_a_data_gap_not_a_zero() -> None:
    """Kill test (R6): a net-debit row with no amount cannot make the account NOT_LEVERED."""

    catalog = _catalog(base_cash=True)
    gap = _quiet_debit("-5")
    gap["amount"] = {"currency": "USD", "status": "UNAVAILABLE", "value": None}
    catalog["financing_liability_observations"].append(gap)
    state = _quiet_state(catalog)
    assert state["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "DEBIT_ROW_AMOUNT_UNAVAILABLE" in state["coverage_reason"]


def test_not_levered_needs_observed_cash_days_and_a_verified_declaration() -> None:
    """Kill test (R6): NOT_LEVERED is a finding.  Without observed base cash days (the
    producer simply emitted nothing) or with an unverified declaration it is a conflict,
    never COMPLETE_DECLARED."""

    good = _quiet_state(_catalog(base_cash=True))
    assert good["classification"] == "NOT_LEVERED"
    assert good["coverage_status"] == "COMPLETE_DECLARED"
    empty = _catalog(base_cash=True)
    empty["financing_liability_observations"] = [
        r for r in empty["financing_liability_observations"] if r["account_id"] != QUIET
    ]
    # a declaration alone (NO_MARGIN_FACILITY included) is never the evidence
    refused = _quiet_state(empty)
    assert refused["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert (
        "NOT_LEVERED_WITHOUT_OBSERVED_ZERO_FINANCING_DAYS" in refused["coverage_reason"]
    )
    # a pinned, provider-observed zero-financing row satisfies it (any provider)
    zero = {
        "account_id": QUIET,
        "provider_id": "ibkr",
        "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
        "classification": "ZERO_FINANCING_OBSERVED",
        "as_of_date": DAYS[0],
        "amount": _amt("0"),
        "source_artifact_id": "pins/statement-2026-01.txt",
        "source_field": "Balance Subject to Interest",
        "source_artifact_sha256": "a" * 64,
    }
    pinned = {**empty}
    pinned["financing_liability_observations"] = [
        *empty["financing_liability_observations"],
        *({**zero, "as_of_date": day} for day in DAYS),
    ]
    assert _quiet_state(pinned)["coverage_status"] == "COMPLETE_DECLARED"
    # coverage is required: one observed day of three is not enough (F2)
    partial = {**empty}
    partial["financing_liability_observations"] = [
        *empty["financing_liability_observations"],
        zero,
    ]
    refused = _quiet_state(partial)
    assert refused["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "NOT_LEVERED_WITH_UNOBSERVED_DAYS" in refused["coverage_reason"]
    # a zero row dated before the account's first NAV day is clipped, not a data gap (E5)
    early = {**empty}
    early["financing_liability_observations"] = [
        *pinned["financing_liability_observations"],
        {**zero, "as_of_date": "2025-12-01"},
    ]
    assert _quiet_state(early)["coverage_status"] == "COMPLETE_DECLARED"
    # without provenance, or with a non-zero amount, it is a gap, not evidence
    for broken in (
        {**zero, "source_artifact_id": None},
        {**zero, "source_field": None},
        {**zero, "amount": _amt("-5")},
        {**zero, "source_artifact_sha256": None},
        {**zero, "source_artifact_sha256": "xyz"},
        {**zero, "source_artifact_id": True, "source_field": 1},
        {**zero, "as_of_date": "0000"},
        {**zero, "as_of_date": "0"},
        {**zero, "as_of_date": "2025-13-99"},
        {**zero, "as_of_date": "2025-12-31"},  # before the account's window
    ):
        bad = {**empty}
        bad["financing_liability_observations"] = [
            *empty["financing_liability_observations"],
            broken,
        ]
        assert _quiet_state(bad)["coverage_status"] == "OBSERVED_UNRECONCILED"
    # a facility that exists needs observed cash days showing no debit
    available = _declaration()
    available[QUIET] = af.Declared(
        QUIET, "ACTIVE", "MARGIN_AVAILABLE_UNUSED", True, "x"
    )
    silent = _quiet_state(empty, available)
    assert silent["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert (
        "NOT_LEVERED_WITHOUT_OBSERVED_ZERO_FINANCING_DAYS" in silent["coverage_reason"]
    )
    # and the conflicted account still gets closure rows, so a route to closure exists
    rows = ld.account_financing_rows(
        available, empty, cutoff=CUTOFF, generation_id=SHA, catalog_sha256=SHA
    )
    assert any(
        r["account_id"] == QUIET and r["data_class"] == "FINANCING_COST" for r in rows
    )
    declaration = _declaration()
    declaration[QUIET] = af.Declared(QUIET, "ACTIVE", "NO_MARGIN_FACILITY", False, "x")
    unverified = _quiet_state(_catalog(base_cash=True), declaration)
    assert unverified["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "NOT_LEVERED_DECLARATION_UNVERIFIED" in unverified["coverage_reason"]


def test_a_conflicted_not_levered_account_reports_the_missing_field_not_no_row() -> (
    None
):
    """Kill test (round-3 F3): E8 gets an uncovered exposure row naming the missing
    observed_zero_financing_days, never NO_ROW."""

    empty = _catalog(base_cash=True)
    empty["financing_liability_observations"] = [
        r for r in empty["financing_liability_observations"] if r["account_id"] != QUIET
    ]
    rows = ld.account_financing_rows(
        _declaration(), empty, cutoff=CUTOFF, generation_id=SHA, catalog_sha256=SHA
    )
    exposure = next(
        r
        for r in rows
        if r["account_id"] == QUIET and r["data_class"] == "LEVERAGE_EXPOSURE"
    )
    assert exposure["coverage_status"] == "OBSERVED_UNRECONCILED"
    assert "observed_zero_financing_days" in exposure["coverage_reason"]


def test_a_positive_borrowed_balance_row_is_a_debit_day_not_an_unreadable_row() -> None:
    """A provider-observed positive borrowed balance is borrowing: LEVERED, and for a
    declared NO_MARGIN_FACILITY account a conflict."""

    catalog = _catalog(base_cash=True)
    catalog["financing_liability_observations"].append(
        {
            "account_id": QUIET,
            "provider_id": "ibkr",
            "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
            "classification": "ZERO_FINANCING_OBSERVED",
            "as_of_date": DAYS[1],
            "amount": _amt("12.5"),
            "source_artifact_id": "pins/statement-2026-01.txt",
            "source_field": "Margin Loan Balance",
            "source_artifact_sha256": "b" * 64,
        }
    )
    state = _quiet_state(catalog)
    assert state["classification"] == "LEVERED"
    assert "DECLARED_NO_FACILITY_BUT_NET_BASE_DEBIT" in state["coverage_reason"]


def test_an_incidental_debit_account_needs_a_stated_zero_not_a_silent_nav() -> None:
    """Kill test (round-6): every account that reaches the cost closure (incidental debit
    included) closes an empty accrual section only on a stated zero."""

    def catalog(stated: bool) -> dict[str, Any]:
        cat = _catalog(base_cash=True)
        cat["financing_liability_observations"] += [
            {
                "account_id": QUIET,
                "provider_id": "ibkr",
                "aggregation_level": "CURRENCY",
                "measurement_basis": "TRADE_DATE_CASH_BALANCE",
                "classification": "CASH_DEBIT_BALANCE",
                "as_of_date": d,
                "amount": _amt("-500", "EUR"),
            }
            for d in DAYS
        ]
        cat["financing_cost_observations"] += [
            {
                "account_id": QUIET,
                "provider_id": "ibkr",
                "classification": "NO_ACCRUALS_REPORTED",
                "measurement_basis": "ACCRUAL_SECTION_EMPTY",
                "as_of_date": d,
                "period_end": d,
                "amount": _amt("0"),
                "source_account_scope": "STATEMENT_ACCOUNT",
                "source_section": "InterestAccruals",
                "mapping_status": "MAPPED",
                "nav_interest_accruals_stated": stated,
            }
            for d in DAYS
        ]
        return cat

    def cost_status(stated: bool) -> str:
        rows = ld.account_financing_rows(
            _declaration(),
            catalog(stated),
            cutoff=CUTOFF,
            generation_id=SHA,
            catalog_sha256=SHA,
        )
        state = next(
            r
            for r in rows
            if r["account_id"] == QUIET and r["data_class"] == "ACCOUNT_FINANCING"
        )
        assert state["classification"] == "INCIDENTAL_DEBIT"
        return next(
            r["coverage_status"]
            for r in rows
            if r["account_id"] == QUIET and r["data_class"] == "FINANCING_COST"
        )

    assert cost_status(False) == "OBSERVED_UNRECONCILED"
    assert cost_status(True) == "COMPLETE_RECONCILED"

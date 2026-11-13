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


def test_a_dust_net_base_debit_is_not_borrowing() -> None:
    """A closed account keeps a fractional negative cash balance; that is not leverage."""

    catalog = _catalog(base_cash=False)
    catalog["financing_liability_observations"] = [
        {
            "account_id": QUIET,
            "provider_id": "ibkr",
            "aggregation_level": "BASE_SUMMARY",
            "measurement_basis": "TRADE_DATE_CASH_BALANCE",
            "classification": "NET_CASH_DEBIT_BALANCE",
            "as_of_date": day,
            "amount": _amt(value),
        }
        for day, value in zip(DAYS, ("-0.0016", "-0.0016", "-1.5"), strict=True)
    ]
    rows = ld.account_financing_rows(
        _declaration(), catalog, cutoff=CUTOFF, generation_id=SHA, catalog_sha256=SHA
    )
    quiet = next(r for r in rows if r["account_id"] == QUIET)
    # one day of real (above-dust) debit makes it levered, and a declared
    # NO_MARGIN_FACILITY account is then a conflict; dust days alone do not count.
    assert quiet["classification"] == "LEVERED"
    assert "DECLARED_NO_FACILITY_BUT_NET_BASE_DEBIT" in quiet["coverage_reason"]
    catalog["financing_liability_observations"][2]["amount"] = _amt("-0.4")
    rows = ld.account_financing_rows(
        _declaration(), catalog, cutoff=CUTOFF, generation_id=SHA, catalog_sha256=SHA
    )
    assert next(r for r in rows if r["account_id"] == QUIET)["classification"] == (
        "NOT_LEVERED"
    )

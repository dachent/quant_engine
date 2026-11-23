"""Settled debit of a retirement account (#894, owner ruling): its own state, not LEVERED.

A retirement account's settled debit bears no interest (the precedent statements print "No
margin interest was charged ... 0.00%"): it is INCIDENTAL_DEBIT / SETTLED_DEBIT_NO_INTEREST with
no NO_FACILITY conflict, and it needs no cost or exposure row.  A margin account's debit days
carry no ``interest_bearing: false`` statement, so they still make it LEVERED."""

from __future__ import annotations

import json
from typing import Any

import pytest

import account_financing as af
import leverage_disclosure as ld

RET = "account-" + "1" * 16
OTHER = "account-" + "2" * 16
CUTOFF = "2026-11-19"
REFS = ["precedent-statement-a", "precedent-statement-b"]


def _declaration(**extra: Any) -> bytes:
    row = {
        "account_id": RET,
        "scope": "ACTIVE",
        "facility": "NO_MARGIN_FACILITY",
        "verification": "VERIFIED",
        "evidence_ref": "owner",
        "settled_debit_interest": af.SETTLED_DEBIT_INTEREST_NONE,
        "settled_debit_interest_refs": REFS,
        **extra,
    }
    plain = {**row}
    plain.pop("settled_debit_interest", None)
    plain.pop("settled_debit_interest_refs", None)
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "account-financing-declaration",
            "as_of_date": CUTOFF,
            "accounts": [row, {**plain, "account_id": OTHER}],
        }
    ).encode()


def _classify(
    account: str = RET, declaration: bytes | None = None, **evidence: Any
) -> af.AccountState:
    declared = af.load_declaration(declaration or _declaration())
    base = {
        "net_base_debit_days": 3,
        "interest_free_debit_days": 3,
        "observed_base_days": 10,
        "unobserved_days": 0,
    }
    seen = af.DebitEvidence(**{**base, **evidence})
    return af.classify(declared, {account: seen}, [account])[account]


def test_an_interest_free_declared_settled_debit_is_incidental_without_conflict() -> (
    None
):
    state = _classify()
    assert (state.state, state.reason, state.conflicts) == (
        "INCIDENTAL_DEBIT",
        af.SETTLED_DEBIT_NO_INTEREST,
        (),
    )


def test_a_margin_accounts_debit_days_still_make_it_levered() -> None:
    # no producer statement of no interest (a margin account's rows never carry one)
    state = _classify(interest_free_debit_days=0)
    assert state.state == "LEVERED" and state.conflicts
    # and an account that declares margin use is levered whatever its days say
    declared = af.load_declaration(_declaration())
    used = {
        **declared,
        RET: af.Declared(
            RET,
            "ACTIVE",
            "MARGIN_USED",
            True,
            "owner",
            af.SETTLED_DEBIT_INTEREST_NONE,
            tuple(REFS),
        ),
    }
    seen = af.DebitEvidence(net_base_debit_days=3, interest_free_debit_days=3)
    assert af.classify(used, {RET: seen}, [RET])[RET].state == "LEVERED"


@pytest.mark.parametrize(
    "evidence",
    [
        {"interest_free_debit_days": 2},  # one debit day is not stated interest-free
        {"interest_free_debit_days": 0},
        {
            "debit_interest_postings": 1
        },  # a posted interest charge is evidence of interest
    ],
)
def test_a_partly_free_or_interest_posted_debit_is_not_exempt(
    evidence: dict[str, Any],
) -> None:
    state = _classify(**evidence)
    assert state.state == "LEVERED" and state.conflicts


def test_an_account_without_the_declaration_field_is_never_exempt() -> None:
    state = _classify(account=OTHER)
    assert state.state == "LEVERED" and state.conflicts


def test_an_unverified_declaration_is_never_exempt() -> None:
    raw = json.loads(_declaration())
    raw["accounts"][0]["verification"] = "UNVERIFIED"
    state = _classify(declaration=json.dumps(raw).encode())
    assert state.state == "LEVERED" and state.conflicts


def test_no_debit_day_is_not_the_exempt_state() -> None:
    declared = af.load_declaration(_declaration())
    seen = af.DebitEvidence(observed_base_days=5, unobserved_days=0)
    assert af.classify(declared, {RET: seen}, [RET])[RET].state == "NOT_LEVERED"


@pytest.mark.parametrize(
    "extra",
    [
        {"settled_debit_interest": "SOMETHING_ELSE"},
        {"settled_debit_interest_refs": []},
        {"settled_debit_interest_refs": [" "]},
        {"settled_debit_interest_refs": "precedent"},
        {"facility": "MARGIN_USED"},
        {"facility": "MARGIN_AVAILABLE_UNUSED"},
    ],
)
def test_the_declaration_field_is_strict(extra: dict[str, Any]) -> None:
    with pytest.raises(af.FinancingDeclarationError, match="settled_debit_interest"):
        af.load_declaration(_declaration(**extra))


def test_refs_without_the_statement_are_refused() -> None:
    raw = json.loads(_declaration())
    del raw["accounts"][0]["settled_debit_interest"]
    with pytest.raises(af.FinancingDeclarationError, match="settled_debit_interest"):
        af.load_declaration(json.dumps(raw).encode())


def test_the_two_declaration_keys_come_together() -> None:
    raw = json.loads(_declaration())
    del raw["accounts"][0]["settled_debit_interest_refs"]
    with pytest.raises(af.FinancingDeclarationError, match="settled_debit_interest"):
        af.load_declaration(json.dumps(raw).encode())
    declared = af.load_declaration(_declaration())
    assert declared[RET].settled_debit_interest == "NONE_BY_ACCOUNT_TYPE"
    assert declared[RET].settled_debit_interest_refs == tuple(REFS)
    assert declared[OTHER].settled_debit_interest is None


# --- the disclosure reads the producer's interest_bearing statement ---------------------------
def _amt(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _day(day: str, balance: str, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": RET,
        "provider_id": "schwab",
        "as_of_date": day,
        "amount": _amt(balance),
        "amount_measurement": "DAY_BALANCE_DERIVED",
        "classification": "OBSERVED_FINANCING_DAY",
        "measurement_basis": ld.ZERO_FINANCING_BASIS,
        "source_artifact_id": "capture",
        "source_artifact_sha256": "a" * 64,
        "source_field": "x",
        **extra,
    }


def _catalog(*, free: bool = True) -> dict[str, Any]:
    nav = [
        {
            "account_id": RET,
            "as_of_date": d,
            "provider_id": "schwab",
            "strategy_id": "s",
            "mapping_status": "MAPPED",
            "nav": _amt("100"),
        }
        for d in ("2026-11-17", "2026-11-18", "2026-11-19")
    ]
    extra = {"interest_bearing": False} if free else {}
    return {
        "nav_observations": nav,
        "financing_cost_observations": [],
        "financing_liability_observations": [
            _day("2026-11-17", "97.33", **extra),
            _day("2026-11-18", "97.33", **extra),
            _day("2026-11-19", "97.33", **extra),
        ],
        "leverage_exposure_observations": [],
        "positions": [],
    }


def _rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    return ld.account_financing_rows(
        af.load_declaration(_declaration()),
        catalog,
        cutoff=CUTOFF,
        generation_id="b" * 64,
        catalog_sha256="b" * 64,
        account_ids=[RET],
    )


def test_the_exempt_account_gets_a_state_row_and_no_cost_or_exposure_row() -> None:
    rows = _rows(_catalog())
    assert [(r["data_class"], r["measurement_basis"]) for r in rows] == [
        ("ACCOUNT_FINANCING", "ACCOUNT_FINANCING_STATE")
    ]
    state = rows[0]
    assert state["classification"] == "INCIDENTAL_DEBIT"
    assert state["coverage_status"] == "COMPLETE_DECLARED"
    assert state["coverage_reason"] == "SETTLED_DEBIT_NO_INTEREST"


def test_without_the_producer_statement_the_account_stays_levered_with_its_rows() -> (
    None
):
    rows = _rows(_catalog(free=False))
    state = next(r for r in rows if r["measurement_basis"] == "ACCOUNT_FINANCING_STATE")
    assert (
        state["classification"] == "LEVERED" and "CONFLICT" in state["coverage_reason"]
    )
    assert {r["data_class"] for r in rows} >= {"FINANCING_COST", "LEVERAGE_EXPOSURE"}


@pytest.mark.parametrize("value", ["false", True, 0, None])
def test_only_the_boolean_false_is_a_statement_of_no_interest(value: Any) -> None:
    catalog = _catalog()
    catalog["financing_liability_observations"][0]["interest_bearing"] = value
    state = next(
        r for r in _rows(catalog) if r["measurement_basis"] == "ACCOUNT_FINANCING_STATE"
    )
    assert state["classification"] == "LEVERED"


def test_the_debit_days_stay_in_the_ledger_rows_for_the_panel() -> None:
    catalog = _catalog()
    _rows(catalog)
    days = [
        r
        for r in catalog["financing_liability_observations"]
        if r["classification"] == "OBSERVED_FINANCING_DAY"
    ]
    assert [r["amount"]["value"] for r in days] == [
        "97.33"
    ] * 3  # the disclosure never drops them

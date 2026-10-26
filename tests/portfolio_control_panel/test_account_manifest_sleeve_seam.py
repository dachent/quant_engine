"""Derived nested-strategy sleeves flow through the account manifest (#567, stage 5).

Hash-bound sleeve-facts artifacts (``build_sleeve_account_facts`` for the IBKR
account, ``build_quantbase_fund_facts`` for the retired Quantbase funds) become
ordinary one-strategy accounts ``<parent>-<sleeve>`` in the manifest, with their
own epochs, NAV, INTERNAL_IN / INTERNAL_OUT flows and TWR/MWR.  Every scenario
runs through the generation builder AND the independent verifier, which must
agree.  All inputs are synthetic.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any

import pytest
from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import canonical_json_bytes

import build_sleeve_account_facts as facts_builder
from test_account_manifest_generation import (  # noqa: F401
    _generation_manifest_snapshot,
    _mixed_crosswalk_selected_inputs,
    _mixed_membership_census_snapshot,
    _mixed_verifier_inputs,
    _selected_input_kwargs,
    perimeter_module,
    verifier_module,
)

PARENT = "synthetic-account-01"
POOL = f"{PARENT}-unallocated"
S1, S2 = f"{PARENT}-s1", f"{PARENT}-s2"
DAYS = (
    "2026-01-27",
    "2026-01-28",
    "2026-01-29",
    "2026-01-30",
    "2026-01-31",
    "2026-02-01",
)
FIRST, LAST = DAYS[0], DAYS[-1]


# --------------------------------------------------------------------------
# the synthetic IBKR-style world


def _money(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _tag_map() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-02-01",
        "parents": [
            {
                "parent_account_id": PARENT,
                "provider_id": "synthetic",
                "tag_field": "orderReference",
                "tag_normalization": "NONE",
                "sleeves": [
                    {
                        "sleeve_id": name,
                        "strategy_id": f"strategy-{name}",
                        "label": name,
                        "effective_start": "2026-01-01",
                        "effective_end": None,
                        "tags": [{"match": "startswith", "pattern": pattern}],
                    }
                    for name, pattern in (("s1", "BookOne"), ("s2", "BookTwo"))
                ],
                "unmatched": {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s1",
                },
            }
        ],
    }


def _event(kind: str, ref: str, counterparty: str | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "evidence": "DERIVED",
        "evidence_ref": ref,
        "live": True,
        "note": None,
        "counterparty": counterparty,
    }


def _trade(day: str, quantity: str, price: str, tag: str, time: str) -> dict[str, Any]:
    return {
        "account_id": PARENT,
        "kind": "TRADE:ExchTrade:BUY"
        if Decimal(quantity) > 0
        else "TRADE:ExchTrade:SELL",
        "event_date": day,
        "security_id": "sec-a",
        "quantity": quantity,
        "execution_price": price,
        "close_price": price,
        "commission": _money("0"),
        "executed_time": time,
        "amount": _money("0"),
        "order_reference": tag,
    }


def _position(day: str, quantity: str, mark: str) -> dict[str, Any]:
    return {
        "account_id": PARENT,
        "as_of_date": day,
        "security_id": "sec-a",
        "quantity": quantity,
        "mark_price": mark,
        "market_value": _money("0"),
    }


def _change(day: str, start: str, end: str, interest: str = "0") -> dict[str, Any]:
    return {
        "account_id": PARENT,
        "accounting_date": day,
        "currency": "USD",
        "opening_nav": start,
        "ending_nav": end,
        "components": [
            {"source_component_key": "depositsWithdrawals", "value": "0"},
            {"source_component_key": "interest", "value": interest},
            {"source_component_key": "otherFees", "value": "0"},
            {"source_component_key": "changeInDividendAccruals", "value": "0"},
        ],
    }


# Parent account NAV by day: 10 shares are bought on day 0, marked up on day 1
# (with -0.5 of interest), sold on day 2 while 8 shares are bought with part of
# the proceeds, then marked on days 3..5.
PARENT_NAV = {
    DAYS[0]: "1000",
    DAYS[1]: "1009.5",
    DAYS[2]: "1019.5",
    DAYS[3]: "1027.5",
    DAYS[4]: "1027.5",
    DAYS[5]: "1023.5",
}


def _liquidate_and_reallocate_facts() -> dict[str, Any]:
    """s1 buys on day 0 and is liquidated on day 2 while s2 buys with the proceeds."""

    funding = {
        "s1": {
            DAYS[0]: _event("IN", "flex:fund-s1", "UNALLOCATED_ACCOUNT_CASH"),
            DAYS[2]: _event("EXIT", "flex:rebalance-2"),
        },
        "s2": {DAYS[2]: _event("IN", "flex:rebalance-2")},
    }
    rows = facts_builder.AccountRows(
        (
            _trade(DAYS[0], "10", "10", "BookOne-1", "09:30:00"),
            _trade(DAYS[2], "-10", "12", "BookOne-1", "09:31:00"),
            _trade(DAYS[2], "8", "12", "BookTwo-1", "09:32:00"),
        ),
        (
            _position(DAYS[0], "10", "10"),
            _position(DAYS[1], "10", "11"),
            _position(DAYS[2], "8", "12"),
            _position(DAYS[3], "8", "13"),
            _position(DAYS[4], "8", "13"),
            _position(DAYS[5], "8", "12.5"),
        ),
        (
            _change(DAYS[0], "1000", "1000"),
            _change(DAYS[1], "1000", "1009.5", interest="-0.5"),
            _change(DAYS[2], "1009.5", "1019.5"),
            _change(DAYS[3], "1019.5", "1027.5"),
            _change(DAYS[4], "1027.5", "1027.5"),
            _change(DAYS[5], "1027.5", "1023.5"),
        ),
    )
    return _facts(funding, rows)


def _open_and_close_facts() -> dict[str, Any]:
    """s2 opens on day 1 and closes on day 3 inside the account; s1 never exits."""

    funding = {
        "s1": {DAYS[0]: _event("IN", "flex:fund-s1", "UNALLOCATED_ACCOUNT_CASH")},
        "s2": {
            DAYS[1]: _event("IN", "flex:open-s2", "UNALLOCATED_ACCOUNT_CASH"),
            DAYS[3]: _event("EXIT", "flex:close-s2", "UNALLOCATED_ACCOUNT_CASH"),
        },
    }
    rows = facts_builder.AccountRows(
        (
            _trade(DAYS[0], "10", "10", "BookOne-1", "09:30:00"),
            _trade(DAYS[1], "5", "10", "BookTwo-1", "09:30:00"),
            _trade(DAYS[3], "-5", "12", "BookTwo-1", "09:30:00"),
        ),
        (
            _position(DAYS[0], "10", "10"),
            _position(DAYS[1], "15", "11"),
            _position(DAYS[2], "15", "12"),
            _position(DAYS[3], "10", "12"),
            _position(DAYS[4], "10", "11"),
            _position(DAYS[5], "10", "11"),
        ),
        (
            _change(DAYS[0], "1000", "1000"),
            _change(DAYS[1], "1000", "1015"),
            _change(DAYS[2], "1015", "1030"),
            _change(DAYS[3], "1030", "1040"),
            _change(DAYS[4], "1040", "1030"),
            _change(DAYS[5], "1030", "1030"),
        ),
    )
    return _facts(funding, rows)


def _facts(funding: dict[str, Any], rows: Any) -> dict[str, Any]:
    declared = {
        "schema_version": 1,
        "artifact_id": "sleeve-declared-inputs",
        "parent_account_id": PARENT,
        "base_currency": "USD",
        "residual_id": "other",
        "window": {"first_day": FIRST, "last_day": LAST},
        "closure_tolerance": {"per_day": "0.01", "window": "0.02"},
        "change_in_nav_components": {
            "external_flow": ["depositsWithdrawals"],
            "interest": ["interest"],
            "fees": ["otherFees"],
            "dividend_accrual": ["changeInDividendAccruals"],
        },
        "symbol_aliases": {"aliases": {}},
        "unheld_fallback": {},
        "funding_events": funding,
    }
    data = facts_builder.build_sleeve_account_facts(
        tag_map_bytes=json.dumps(_tag_map(), sort_keys=True).encode(),
        declared_inputs_bytes=json.dumps(declared, sort_keys=True).encode(),
        rows=rows,
        rows_sha256="0" * 64,
        parent_ledger_generation_id="synthetic-ibkr-generation",
    )
    result: dict[str, Any] = json.loads(data)
    return result


def _facts_bytes(artifact: dict[str, Any]) -> bytes:
    return (
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


# --------------------------------------------------------------------------
# selected inputs


def _census(extra: list[tuple[str, str, str | None]]) -> InputSnapshot:
    census = _mixed_membership_census_snapshot().json_object()
    for strategy, start, end in extra:
        census["current_census"]["account_sleeves"].append(
            {
                "provider_id": "SYNTHETIC",
                "assignments": [
                    {
                        "strategy_id": strategy,
                        "effective_start": start,
                        "effective_end": end,
                        "status": "FUNDED_MEMBERSHIP",
                    }
                ],
            }
        )
    return InputSnapshot.from_bytes("sleeve census", canonical_json_bytes(census))


def _selected(
    perimeter_module: Any,
    facts: tuple[InputSnapshot, ...],
    *,
    ledger_edit: Any = None,
    census: InputSnapshot | None = None,
) -> Any:
    """The mixed fixture with the parent's NAV by day and the facts selected."""

    selected = _mixed_crosswalk_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    ledger["nav_observations"] = [
        {
            "record_id": "mixed-nav-1" if day == FIRST else f"parent-nav-{day}",
            "account_id": PARENT,
            "as_of_date": day,
            "value_basis": "ACCOUNT_TOTAL",
            "nav": _money(value),
        }
        for day, value in PARENT_NAV.items()
    ]
    ledger["flows"] = []
    ledger["capital_anchors"] = []
    if ledger_edit is not None:
        ledger_edit(ledger)
    ledger_snapshot = InputSnapshot.from_bytes(
        "sleeve ledger", canonical_json_bytes(ledger)
    )
    policy = selected.flow_classification_policy.json_object()
    # the governed policy's prefix rule: an unpaired sleeve leg falls back to
    # EXTERNAL, a paired one is INTERNAL
    policy["event_dispositions"]["prefix"] = [
        {
            "flow_type_prefix": "TRANSFER:INTERNAL:",
            "disposition": "INTERNAL",
            "amount_sign": "NONZERO",
            "counterparty_rule": "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT",
            "unpaired_disposition": "EXTERNAL",
        }
    ]
    values = _selected_input_kwargs(selected)
    values.update(
        {
            "flow_classification_policy": InputSnapshot.from_bytes(
                "sleeve policy", canonical_json_bytes(policy)
            ),
            "provider_ledger": ledger_snapshot,
            "provider_ledger_manifest": _generation_manifest_snapshot(
                catalog=ledger_snapshot,
                catalog_artifact_id="normalized/provider-ledger.json",
                catalog_schema_name="provider-ledger-catalog",
                catalog_schema_version="1",
                as_of_date="2026-02-01",
            ),
            "receipt_authority": selected.receipt_authority,
            "admission_policy": selected.admission_policy,
            "manifest_schema": selected.manifest_schema,
            "membership_census": census
            or _census(
                [
                    ("strategy-s1", DAYS[0], DAYS[2]),
                    ("strategy-s2", DAYS[2], None),
                ]
            ),
            "sleeve_facts": facts,
        }
    )
    return perimeter_module.AccountManifestSelectedInputs(**values)


def _verifier_inputs(verifier_module: Any, selected: Any) -> Any:
    inputs = _mixed_verifier_inputs(verifier_module, selected)
    values = {
        name: getattr(inputs, name)
        for name in inputs.__dataclass_fields__
        if name != "sleeve_facts"
    }
    return verifier_module.IndependentVerifierInputs(
        **values, sleeve_facts=selected.sleeve_facts
    )


def _rows(payloads: Any) -> dict[str, dict[str, Any]]:
    output = json.loads(payloads["outputs/account-strategy-manifest.json"])
    return {row["account_id"]: row for row in output["rows"]}


def _build_and_verify(
    perimeter_module: Any, verifier_module: Any, selected: Any
) -> tuple[Any, dict[str, Any]]:
    payloads = perimeter_module.build_account_manifest_payloads(selected)
    report = json.loads(
        verifier_module.IndependentAccountManifestVerifier().verify(
            _verifier_inputs(verifier_module, selected), payloads
        )
    )
    return payloads, report


@pytest.fixture(scope="module")
def reallocate_facts() -> dict[str, Any]:
    return _liquidate_and_reallocate_facts()


# --------------------------------------------------------------------------
# tests


def test_liquidate_and_reallocate_builds_verifies_and_pairs(
    perimeter_module: Any, verifier_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))
    selected = _selected(perimeter_module, (snapshot,))
    payloads, report = _build_and_verify(perimeter_module, verifier_module, selected)

    rows = _rows(payloads)
    # the parent is a non-strategy residual: it has no epoch row any more
    assert PARENT not in rows
    assert rows[S1]["strategy_id"] == "strategy-s1"
    assert rows[S2]["strategy_id"] == "strategy-s2"
    assert rows[S1]["status"] == rows[S2]["status"] == "OK"
    assert rows[S1]["effective_end"] == DAYS[2]
    assert rows[S2]["effective_end"] is None
    # twelve other included epochs stay, two sleeves join, the parent leaves
    assert len(rows) == 13 - 1 + 2
    assert report["numeric_verification"]["discrepancy_count"] == 0
    assert report["numeric_verification"]["candidate_count"] == 2

    # opening is the first end-of-day NAV; every later flow is an internal leg
    assert rows[S1]["opening_basis"] == "FIRST_AVAILABLE_NAV"
    assert Decimal(rows[S1]["opening_capital"]) == Decimal("100")
    assert {leg["class"] for leg in rows[S1]["capital_flow_trace"]} <= {
        "INTERNAL_IN",
        "INTERNAL_OUT",
    }
    assert Decimal(rows[S1]["deposits"]) == 0
    assert Decimal(rows[S1]["external_withdrawals"]) == 0
    # the exit sweeps the sleeve out: its value falls to zero and the dollar
    # identity holds (gain = current - net capital)
    assert Decimal(rows[S1]["current_value"]) == 0
    assert Decimal(rows[S1]["gain"]) == Decimal(rows[S1]["current_value"]) - Decimal(
        rows[S1]["net_capital"]
    )
    # a sleeve's TWR is the chain-linked return of its own NAV path: day 1 of
    # s1 is 100 -> 109.5 after the interest it carries, exit-day own P&L +10
    # (value 119.5) is inside the exit step
    twr = Decimal(rows[S1]["observation_linked_return"])
    expected = (Decimal("109.5") / Decimal("100")) * (
        Decimal("119.5") / Decimal("109.5")
    ) - Decimal(1)
    assert twr == expected
    assert rows[S1]["mwr_status"] in {"AVAILABLE", "UNDEFINED", "NO_SOLUTION"} or (
        rows[S1]["mwr_status"].startswith("BLOCKED")
    )

    # classified flows: every sleeve leg is INTERNAL and reciprocal
    classified = json.loads(payloads["authority/classified-flows.json"])
    events = {
        event["source_record_id"]: event
        for event in classified["events"]
        if event["account_id"] in {S1, S2, POOL}
    }
    assert events
    assert {event["disposition"] for event in events.values()} == {"INTERNAL"}
    for event in events.values():
        partner = events[event["counterparty_source_record_id"]]
        assert Decimal(partner["amount"]["value"]) == -Decimal(event["amount"]["value"])
        assert partner["event_date"] == event["event_date"]
        assert partner["account_id"] != event["account_id"]

    # coverage: derived rows cite the parent's receipt and the facts' hash
    coverage = json.loads(payloads["authority/account-flow-coverage.json"])
    derived = [
        row
        for row in coverage["rows"]
        if row["authority_kind"] == "DERIVED_SLEEVE_COVERAGE_V1"
    ]
    assert {row["assignment"]["account_id"] for row in derived} == {S1, S2}
    for row in derived:
        assert row["derivation"] == {
            "parent_account_id": PARENT,
            "sleeve_facts_sha256": snapshot.sha256,
        }
        assert row["receipt_artifact_id"] == "receipt/mixed-account-01.json"
        assert row["status"] == "COMPLETE"


def test_parent_and_sleeves_reconcile_to_the_account(
    perimeter_module: Any, verifier_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))
    selected = _selected(perimeter_module, (snapshot,))
    payloads, _ = _build_and_verify(perimeter_module, verifier_module, selected)
    calculation = json.loads(payloads["authority/calculation-inputs.json"])
    by_account: dict[str, dict[str, Decimal]] = {}
    for nav in calculation["calculation_ledger"]["nav_observations"]:
        by_account.setdefault(nav["account_id"], {})[nav["as_of_date"]] = Decimal(
            nav["nav"]["value"]
        )
    pool_nav = {
        day["day"]: Decimal(day["nav_end"])
        for day in reallocate_facts["perimeter_account"]["days"]
    }
    for day in DAYS:
        total = (
            by_account.get(S1, {}).get(day, Decimal(0))
            + by_account.get(S2, {}).get(day, Decimal(0))
            + pool_nav[day]
        )
        assert total == Decimal(PARENT_NAV[day]), day
    # the sleeve NAV is the sleeve value after allocated cost, never the parent's
    assert by_account[S2][DAYS[3]] == Decimal("104") - Decimal("0.5")


def test_sleeve_opened_and_closed_inside_the_account(
    perimeter_module: Any, verifier_module: Any
) -> None:
    facts = _open_and_close_facts()
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(facts))
    census = _census(
        [("strategy-s1", DAYS[0], None), ("strategy-s2", DAYS[1], DAYS[3])]
    )
    selected = _selected(perimeter_module, (snapshot,), census=census)
    payloads, report = _build_and_verify(perimeter_module, verifier_module, selected)
    rows = _rows(payloads)
    assert rows[S2]["status"] == "OK"
    assert (rows[S2]["start"], rows[S2]["end"]) == (DAYS[1], DAYS[3])
    assert rows[S2]["effective_end"] == DAYS[3]
    assert Decimal(rows[S2]["current_value"]) == 0
    assert rows[S1]["effective_end"] is None
    assert rows[S1]["status"] == "OK"
    assert report["numeric_verification"]["discrepancy_count"] == 0


def test_census_lists_a_funded_membership_per_derived_epoch(
    perimeter_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))
    # the contract's census does not list the derived intervals yet
    selected = _selected(perimeter_module, (snapshot,), census=_census([]))
    with pytest.raises(Exception, match="FUNDED_MEMBERSHIP sleeve") as raised:
        perimeter_module.build_account_manifest_payloads(selected)
    message = str(raised.value)
    assert "SYNTHETIC/strategy-s1/2026-01-27/2026-01-29" in message
    assert "current_census.account_sleeves" in message
    # one entry short is still refused: s2 has no entry
    selected = _selected(
        perimeter_module,
        (snapshot,),
        census=_census([("strategy-s1", DAYS[0], DAYS[2])]),
    )
    with pytest.raises(Exception, match="strategy-s2/2026-01-29/None"):
        perimeter_module.build_account_manifest_payloads(selected)


def test_no_sleeve_facts_leave_the_generation_unchanged(
    perimeter_module: Any, verifier_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    plain = _selected(perimeter_module, ())
    with_facts = _selected(
        perimeter_module,
        (InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts)),),
    )
    plain_payloads, _ = _build_and_verify(perimeter_module, verifier_module, plain)
    assert "sleeve_facts" not in json.loads(
        plain_payloads["authority/selected-input-identities.json"]
    )
    facts_payloads = perimeter_module.build_account_manifest_payloads(with_facts)
    identities = json.loads(facts_payloads["authority/selected-input-identities.json"])
    assert [item["sha256"] for item in identities["sleeve_facts"]] == [
        with_facts.sleeve_facts[0].sha256
    ]
    # every other account keeps its row bytes
    plain_rows, facts_rows = _rows(plain_payloads), _rows(facts_payloads)
    for account, row in plain_rows.items():
        if account != PARENT:
            assert facts_rows[account] == row


def test_verifier_catches_tampered_derived_coverage(
    perimeter_module: Any, verifier_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))
    selected = _selected(perimeter_module, (snapshot,))
    payloads = dict(perimeter_module.build_account_manifest_payloads(selected))
    coverage = json.loads(payloads["authority/account-flow-coverage.json"])
    for row in coverage["rows"]:
        if row["authority_kind"] == "DERIVED_SLEEVE_COVERAGE_V1":
            row["derivation"]["sleeve_facts_sha256"] = "f" * 64
            break
    payloads["authority/account-flow-coverage.json"] = canonical_json_bytes(coverage)
    with pytest.raises(verifier_module.IndependentVerificationError):
        verifier_module.IndependentAccountManifestVerifier().verify(
            _verifier_inputs(verifier_module, selected), payloads
        )


def test_producer_refuses_a_foreign_fact_on_a_derived_account(
    perimeter_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))

    def add_fact(ledger: dict[str, Any]) -> None:
        ledger["nav_observations"].append(
            {
                "record_id": "foreign-nav",
                "account_id": S1,
                "as_of_date": DAYS[3],
                "nav": _money("5"),
            }
        )

    selected = _selected(perimeter_module, (snapshot,), ledger_edit=add_fact)
    with pytest.raises(Exception, match="already carries facts of a derived"):
        perimeter_module.build_account_manifest_payloads(selected)


def test_sleeves_that_do_not_reconcile_to_the_parent_are_refused(
    perimeter_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))

    def skew(ledger: dict[str, Any]) -> None:
        for nav in ledger["nav_observations"]:
            if nav["as_of_date"] == DAYS[3]:
                nav["nav"]["value"] = "1030"

    selected = _selected(perimeter_module, (snapshot,), ledger_edit=skew)
    with pytest.raises(Exception, match="do not reconcile to parent") as raised:
        perimeter_module.build_account_manifest_payloads(selected)
    assert DAYS[3] in str(raised.value)


def test_window_outside_the_parent_receipt_is_refused(
    perimeter_module: Any, reallocate_facts: dict[str, Any]
) -> None:
    moved = deepcopy(reallocate_facts)
    # push every date past the receipt window end by tampering the window only
    moved["window"]["last_day"] = "2026-02-02"
    for sleeve in moved["sleeves"]:
        if sleeve["last_day"] == LAST:
            sleeve["last_day"] = "2026-02-02"
            sleeve["days"][-1]["day"] = "2026-02-02"
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(moved))
    selected = _selected(perimeter_module, (snapshot,))
    with pytest.raises(Exception):
        perimeter_module.build_account_manifest_payloads(selected)


def test_ledger_hash_is_digest_of_the_snapshot(
    reallocate_facts: dict[str, Any],
) -> None:
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(reallocate_facts))
    assert snapshot.sha256 == hashlib.sha256(_facts_bytes(reallocate_facts)).hexdigest()

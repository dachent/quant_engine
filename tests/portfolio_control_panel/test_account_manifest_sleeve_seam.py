"""Derived nested-strategy sleeves flow through the account manifest (#567, stage 5).

Hash-bound sleeve-facts artifacts (``build_sleeve_account_facts`` for the IBKR
account, ``build_quantbase_fund_facts`` for the retired Quantbase funds) become
ordinary one-strategy accounts ``<parent>-<sleeve>`` in the manifest, with their
own epochs, NAV, INTERNAL_IN / INTERNAL_OUT flows and TWR/MWR.  Every scenario
runs through the generation builder AND the independent verifier, which must
agree.  All inputs are synthetic.
"""

# ruff: noqa: F811  (the shared fixtures are imported, then used as parameters)

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator
import pytest
from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import canonical_json_bytes

import build_sleeve_account_facts as facts_builder
from test_account_manifest_generation import (  # noqa: F401
    _generation_manifest_snapshot,
    _mixed_crosswalk_selected_inputs,
    _mixed_membership_census_snapshot,
    _mixed_verifier_inputs,
    ACCOUNT_STRATEGY_SCHEMA_PATH,
    VERIFIER_SCHEMA_PATH,
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


# Parent NAV of the open-and-close scenario.  Day 3 sells 5 shares at the
# unchanged mark of 12, so the account NAV does not move; day 4 marks 10 shares
# down by 1.  (The account NAV must equal the cash plus the marked positions, or
# the facts builder refuses the closure residual, which is what it is for.)
OPEN_CLOSE_PARENT_NAV = {
    DAYS[0]: "1000",
    DAYS[1]: "1015",
    DAYS[2]: "1030",
    DAYS[3]: "1030",
    DAYS[4]: "1020",
    DAYS[5]: "1020",
}


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
            _change(DAYS[3], "1030", "1030"),
            _change(DAYS[4], "1030", "1020"),
            _change(DAYS[5], "1020", "1020"),
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
    parent_nav: dict[str, str] | None = None,
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
        for day, value in (parent_nav or PARENT_NAV).items()
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
    # lifecycle (#592): an open derived epoch is ACTIVE, an ended one is retired
    assert rows[S2]["lifecycle_state"] == "ACTIVE"
    assert rows[S2]["retired_on"] is None
    assert rows[S1]["lifecycle_state"] in {"RETIRED", "CLOSED"}
    assert rows[S1]["retired_on"] == DAYS[2]
    # twelve other included epochs stay, two sleeves join, the parent leaves
    assert len(rows) == 13 - 1 + 2
    assert report["numeric_verification"]["discrepancy_count"] == 0
    assert report["numeric_verification"]["candidate_count"] == 2
    # the manifest output (schema 10, with the period-return fields) is valid
    Draft202012Validator(
        json.loads(ACCOUNT_STRATEGY_SCHEMA_PATH.read_text(encoding="utf-8"))
    ).validate(json.loads(payloads["outputs/account-strategy-manifest.json"]))
    # the facts are an identity of the verification report and its schema
    Draft202012Validator(
        json.loads(VERIFIER_SCHEMA_PATH.read_text(encoding="utf-8"))
    ).validate(report)
    assert [
        item["sha256"] for item in report["selected_source_identities"]["sleeve_facts"]
    ] == [snapshot.sha256]

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
    # the sleeve NAV is the sleeve value after allocated cost, never the parent's.
    # The only account cost (-0.5 interest on day 1) falls on s1, the only sleeve
    # holding anything that day (exposure share 1); s2 opens on day 2 and is
    # charged nothing (facts: s2 alloc_total 0, nav_end_after_alloc 104).
    assert by_account[S1][DAYS[1]] == Decimal("110") - Decimal("0.5")
    assert by_account[S2][DAYS[3]] == Decimal("104")


def test_sleeve_opened_and_closed_inside_the_account(
    perimeter_module: Any, verifier_module: Any
) -> None:
    facts = _open_and_close_facts()
    snapshot = InputSnapshot.from_bytes("facts", _facts_bytes(facts))
    census = _census(
        [("strategy-s1", DAYS[0], None), ("strategy-s2", DAYS[1], DAYS[3])]
    )
    selected = _selected(
        perimeter_module,
        (snapshot,),
        census=census,
        parent_nav=OPEN_CLOSE_PARENT_NAV,
    )
    payloads, report = _build_and_verify(perimeter_module, verifier_module, selected)
    rows = _rows(payloads)
    assert rows[S2]["status"] == "OK"
    assert (rows[S2]["start"], rows[S2]["end"]) == (DAYS[1], DAYS[3])
    assert rows[S2]["effective_end"] == DAYS[3]
    assert Decimal(rows[S2]["current_value"]) == 0
    assert rows[S1]["effective_end"] is None
    assert rows[S1]["status"] == "OK"
    assert report["numeric_verification"]["discrepancy_count"] == 0


# --------------------------------------------------------------------------
# a retired Quantbase fund beside the account's live fund


FUND = f"{PARENT}-crisis"
FUND_NAV = {DAYS[1]: "100", DAYS[2]: "115", DAYS[3]: "0"}


def _bindings(**extra: Any) -> dict[str, Any]:
    return {
        "tag_map_sha256": "0" * 64,
        "parent_ledger_generation_id": "synthetic-generation",
        "declared_inputs_sha256": "1" * 64,
        "builder_code": {
            "files": {"tools/synthetic_builder.py": "5" * 64},
            "sha256": "6" * 64,
        },
        **extra,
    }


def _quantbase_leg(
    pair: str,
    account: str,
    flow_class: str,
    day: str,
    amount: str,
    source: str,
) -> dict[str, Any]:
    return {
        "pair_id": pair,
        "account_id": account,
        "class": flow_class,
        "flow_type": "TRANSFER:INTERNAL:SLEEVE",
        "event_date": day,
        "nav_recognition_date": day,
        "amount": amount,
        "leg_source": source,
    }


def _quantbase_facts() -> dict[str, Any]:
    """Hand-written ``quantbase-fund-facts``: a fund funded on days 1 and 2 from
    unallocated cash and swept into the live fund on day 3.

    Only the day-3 counterpart (the live fund's INTERNAL_IN) is a leg of the
    parent ledger (``PARENT_LEDGER_REFERENCE``); every other leg is the artifact's.
    """

    legs = []
    for pair, day, amount, into_fund in (
        (f"{DAYS[1]}:0001", DAYS[1], "100", True),
        (f"{DAYS[2]}:0002", DAYS[2], "10", True),
        (f"{DAYS[3]}:0003", DAYS[3], "120", False),
    ):
        fund_class, other_class = (
            ("INTERNAL_IN", "INTERNAL_OUT")
            if into_fund
            else ("INTERNAL_OUT", "INTERNAL_IN")
        )
        legs.append(_quantbase_leg(pair, FUND, fund_class, day, amount, "ARTIFACT"))
        if into_fund:
            legs.append(
                _quantbase_leg(pair, POOL, other_class, day, amount, "ARTIFACT")
            )
        else:
            legs.append(
                _quantbase_leg(
                    pair, PARENT, other_class, day, amount, "PARENT_LEDGER_REFERENCE"
                )
            )
    return {
        "schema_version": 1,
        "artifact_id": "quantbase-fund-facts",
        "parent_account_id": PARENT,
        "provider_id": "synthetic",
        "base_currency": "USD",
        "data_cutoff": LAST,
        # the manifest seam reads only the bindings, sleeves, perimeter and flows;
        # the nested conventions / closure / open items are the builder's own
        # (covered by tests/core/test_build_quantbase_fund_facts.py)
        "bindings": _bindings(
            source_sha256={
                "account_activity": "2" * 64,
                "accounts": "3" * 64,
                "dashboards": {"crisis": "4" * 64},
            }
        ),
        "conventions": {},
        "closure": {},
        "open_items": {},
        "disclosures": {},
        "window": {"first_day": FIRST, "last_day": LAST},
        "perimeter_account": {
            "account_id": POOL,
            "days": [{"day": day, "nav_end": "0"} for day in DAYS],
        },
        "sleeves": [
            {
                "sleeve_id": "crisis",
                "account_id": FUND,
                "strategy_id": "strategy-crisis",
                "first_day": DAYS[1],
                "last_day": DAYS[3],
                "days": [{"day": day, "nav_end": nav} for day, nav in FUND_NAV.items()],
            }
        ],
        "flows": legs,
    }


def _live_fund_receives_the_sweep(ledger: dict[str, Any]) -> None:
    """R1's parent ledger already carries the live fund's INTERNAL_IN of the sweep."""

    ledger["flows"].append(
        {
            "record_id": "parent-ledger-sweep-in",
            "account_id": PARENT,
            "event_date": DAYS[3],
            "flow_type": "TRANSFER:INTERNAL:QUANTBASE",
            "amount": _money("120"),
        }
    )


def test_quantbase_parent_ledger_leg_is_pairing_evidence_not_a_second_flow(
    perimeter_module: Any, verifier_module: Any
) -> None:
    facts = _quantbase_facts()
    snapshot = InputSnapshot.from_bytes("qb facts", _facts_bytes(facts))
    census = _census([("strategy-crisis", DAYS[1], DAYS[3])])
    kwargs: dict[str, Any] = {
        "census": census,
        "ledger_edit": _live_fund_receives_the_sweep,
    }
    selected = _selected(perimeter_module, (snapshot,), **kwargs)
    payloads, report = _build_and_verify(perimeter_module, verifier_module, selected)
    assert report["numeric_verification"]["discrepancy_count"] == 0
    rows = _rows(payloads)

    # the live parent keeps its strategy row and its ledger flow is the only
    # sweep leg on it: the artifact adds nothing to the parent
    assert PARENT in rows
    assert rows[PARENT]["strategy_id"] != "strategy-crisis"
    sweep = [
        leg
        for leg in rows[PARENT]["capital_flow_trace"]
        if leg.get("event_date") == DAYS[3]
    ]
    assert [leg["class"] for leg in sweep] == ["INTERNAL_IN"]
    assert Decimal(sweep[0]["amount"]) == Decimal("120")

    # the fund is its own one-strategy account; its deposits are INTERNAL_IN
    fund = rows[FUND]
    assert fund["strategy_id"] == "strategy-crisis"
    assert (fund["start"], fund["end"]) == (DAYS[1], DAYS[3])
    assert Decimal(fund["deposits"]) == 0
    assert Decimal(fund["external_withdrawals"]) == 0
    # (the first day's 100 is the epoch opening, not a flow)
    assert Decimal(fund["opening_capital"]) == Decimal("100")
    assert [
        (leg["class"], leg["event_date"], Decimal(leg["amount"]))
        for leg in fund["capital_flow_trace"]
    ] == [
        ("INTERNAL_IN", DAYS[2], Decimal("10")),
        ("INTERNAL_OUT", DAYS[3], Decimal("120")),
    ]
    assert Decimal(fund["internal_in"]) == 10
    assert Decimal(fund["internal_out"]) == 120
    assert Decimal(fund["current_value"]) == 0

    # classified flows: the fund's exit pairs with the parent ledger's own
    # record, which stays the single flow of the live fund
    classified = json.loads(payloads["authority/classified-flows.json"])
    events = {event["source_record_id"]: event for event in classified["events"]}
    assert {
        event["disposition"]
        for event in events.values()
        if event["account_id"] in {FUND, POOL}
    } == {"INTERNAL"}
    parent_events = [e for e in classified["events"] if e["account_id"] == PARENT]
    assert [e["source_record_id"] for e in parent_events] == ["parent-ledger-sweep-in"]
    assert parent_events[0]["disposition"] == "INTERNAL"
    partner = events[parent_events[0]["counterparty_source_record_id"]]
    assert partner["account_id"] == FUND

    # The same ledger WITHOUT the fund facts: the parent's flow has no
    # counterpart and falls back to EXTERNAL (a deposit).  With the facts it is
    # INTERNAL_IN; invested capital, net capital, opening and value are unchanged.
    plain = _selected(perimeter_module, (), ledger_edit=_live_fund_receives_the_sweep)
    plain_row = _rows(perimeter_module.build_account_manifest_payloads(plain))[PARENT]
    live_row = rows[PARENT]
    assert (Decimal(plain_row["deposits"]), Decimal(plain_row["internal_in"])) == (
        120,
        0,
    )
    assert (Decimal(live_row["deposits"]), Decimal(live_row["internal_in"])) == (0, 120)
    for field in (
        "invested",
        "net_capital",
        "opening_capital",
        "current_value",
        "gain",
        "observation_linked_return",
        "flow_count",
    ):
        assert live_row[field] == plain_row[field], field


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda f: f.pop("bindings"), "lack governed field.*bindings"),
        (lambda f: f.pop("closure"), "lack governed field.*closure"),
        (
            lambda f: f["bindings"].__setitem__("tag_map_sha256", "xyz"),
            "tag_map_sha256 is not a sha256",
        ),
        (
            lambda f: f["bindings"].pop("source_sha256"),
            "bindings lack source_sha256",
        ),
        (
            lambda f: f["bindings"].__setitem__("builder_code", {"files": {}}),
            "builder_code is invalid",
        ),
        (
            lambda f: f.__setitem__("provider_id", "other-provider"),
            "provider_id differs from the parent account",
        ),
    ],
)
def test_sleeve_artifact_without_its_governed_bindings_is_refused(
    perimeter_module: Any, mutate: Any, message: str
) -> None:
    facts = _quantbase_facts()
    mutate(facts)
    snapshot = InputSnapshot.from_bytes("qb facts", _facts_bytes(facts))
    selected = _selected(
        perimeter_module,
        (snapshot,),
        census=_census([("strategy-crisis", DAYS[1], DAYS[3])]),
    )
    with pytest.raises(Exception, match=message):
        perimeter_module.build_account_manifest_payloads(selected)


def test_same_day_equal_amount_sleeve_legs_block_the_epoch_instead_of_guessing(
    perimeter_module: Any,
) -> None:
    facts = _quantbase_facts()
    # a second fund-account pair of the same amount on the same day: the
    # classifier can only match by date and amount, so it refuses to guess
    extra = []
    for leg in facts["flows"]:
        if leg["pair_id"] == f"{DAYS[1]}:0001":
            extra.append({**leg, "pair_id": f"{DAYS[1]}:0009"})
    facts["flows"].extend(extra)
    snapshot = InputSnapshot.from_bytes("qb facts", _facts_bytes(facts))
    selected = _selected(
        perimeter_module,
        (snapshot,),
        census=_census([("strategy-crisis", DAYS[1], DAYS[3])]),
        ledger_edit=_live_fund_receives_the_sweep,
    )
    with pytest.raises(Exception, match="blocked classified flow"):
        perimeter_module.build_account_manifest_payloads(selected)


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

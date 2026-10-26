"""The sleeve account facts builder derives, binds and fails closed (#567)."""

from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import build_sleeve_account_facts as builder
import nested_strategy_tag_map as tag_map_module
import sleeve_nav as sn

PARENT = "account-parent"
UNALLOCATED = f"{PARENT}-unallocated"
GENERATION = "ibkr-ledger-generation-001"


def _tag_map() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-01-10",
        "parents": [
            {
                "parent_account_id": PARENT,
                "provider_id": "provider-a",
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


def _event(
    kind: str,
    ref: str,
    *,
    evidence: str = "DERIVED",
    live: bool = True,
    counterparty: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "evidence": evidence,
        "evidence_ref": ref,
        "live": live,
        "note": None,
        "counterparty": counterparty,
    }


def _declared(
    funding: dict[str, Any], first: str, last: str, **extra: Any
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "artifact_id": "sleeve-declared-inputs",
        "parent_account_id": PARENT,
        "base_currency": "USD",
        "residual_id": "other",
        "window": {"first_day": first, "last_day": last},
        "closure_tolerance": {"per_day": "0.01", "window": "0.02"},
        "change_in_nav_components": {
            "external_flow": ["depositsWithdrawals"],
            "interest": ["interest"],
            "fees": ["otherFees"],
            "dividend_accrual": ["changeInDividendAccruals"],
        },
        "symbol_aliases": {"aliases": {}, "from_day": None},
        "unheld_fallback": {},
        "funding_events": funding,
    }
    document.update(extra)
    return document


def _bytes(document: object) -> bytes:
    return json.dumps(document, sort_keys=True).encode()


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


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
        "commission": _amount("0"),
        "executed_time": time,
        "amount": _amount("0"),
        "order_reference": tag,
    }


def _position(day: str, quantity: str, mark: str) -> dict[str, Any]:
    return {
        "account_id": PARENT,
        "as_of_date": day,
        "security_id": "sec-a",
        "quantity": quantity,
        "mark_price": mark,
        "market_value": _amount("0"),
    }


def _change(
    day: str, start: str, end: str, *, interest: str = "0", external: str = "0"
) -> dict[str, Any]:
    return {
        "account_id": PARENT,
        "accounting_date": day,
        "currency": "USD",
        "opening_nav": start,
        "ending_nav": end,
        "components": [
            {"source_component_key": "depositsWithdrawals", "value": external},
            {"source_component_key": "interest", "value": interest},
            {"source_component_key": "otherFees", "value": "0"},
            {"source_component_key": "changeInDividendAccruals", "value": "0"},
        ],
    }


def _rows(
    transactions: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> builder.AccountRows:
    return builder.AccountRows(tuple(transactions), tuple(positions), tuple(changes))


D1, D2, D3, D4 = "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"


def _reallocate() -> tuple[dict[str, Any], builder.AccountRows]:
    """s1 buys, then is liquidated on D2 while s2 buys with part of the proceeds."""

    funding = {
        "s1": {
            D1: _event("IN", "flex:funding-1", counterparty="UNALLOCATED_ACCOUNT_CASH"),
            D2: _event("EXIT", "flex:rebalance-2"),
        },
        "s2": {D2: _event("IN", "flex:rebalance-2")},
    }
    rows = _rows(
        [
            _trade(D1, "10", "10", "BookOne-1", "09:30:00"),
            _trade(D2, "-10", "12", "BookOne-1", "09:31:00"),
            _trade(D2, "8", "12", "BookTwo-1", "09:32:00"),
        ],
        [
            _position(D1, "10", "10"),
            _position(D2, "8", "12"),
            _position(D3, "8", "13"),
        ],
        [
            _change(D1, "1000", "1000"),
            _change(D2, "1000", "1020"),
            _change(D3, "1020", "1027.6", interest="-0.4"),
        ],
    )
    return _declared(funding, D1, D3), rows


def _build(
    declared: dict[str, Any],
    rows: builder.AccountRows,
    tag_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(tag_map or _tag_map()),
        declared_inputs_bytes=_bytes(declared),
        rows=rows,
        rows_sha256="0" * 64,
        parent_ledger_generation_id=GENERATION,
    )
    result: dict[str, Any] = json.loads(data)
    return result


def _legs(artifact: dict[str, Any], account: str) -> list[tuple[str, str, str]]:
    return [
        (leg["event_date"], leg["class"], leg["amount"])
        for leg in artifact["flows"]
        if leg["account_id"] == account
    ]


def test_liquidate_and_reallocate_pairs_legs_and_closes() -> None:
    declared, rows = _reallocate()
    artifact = _build(declared, rows)

    sleeves = {item["sleeve_id"]: item for item in artifact["sleeves"]}
    s1, s2 = sleeves["s1"], sleeves["s2"]
    assert [row["day"] for row in s1["days"]] == [D1, D2]
    assert s1["days"][1]["nav_end"] == "0" and s1["days"][1]["flow_out"] == "-120"
    assert s2["days"][0]["flow_in"] == "96"
    # the liquidated sleeve's proceeds pair with the reallocation into s2; what
    # s2 did not take stays in the account's unallocated cash, and is labelled
    assert _legs(artifact, f"{PARENT}-s1") == [
        (D1, "INTERNAL_IN", "100"),
        (D2, "INTERNAL_OUT", "96"),
        (D2, "INTERNAL_OUT", "24"),
    ]
    assert _legs(artifact, f"{PARENT}-s2") == [(D2, "INTERNAL_IN", "96")]
    assert _legs(artifact, UNALLOCATED) == [
        (D1, "INTERNAL_OUT", "100"),
        (D2, "INTERNAL_IN", "24"),
    ]
    pairs = [
        leg["trace"]["pairing"]
        for leg in artifact["flows"]
        if leg["account_id"] == f"{PARENT}-s2"
    ]
    assert pairs == ["SLEEVE_PAIR"]
    reallocation = next(
        leg for leg in artifact["flows"] if leg["trace"]["kind"] == "REALLOCATION"
    )
    assert reallocation["trace"]["from_sleeve"] == "s1"
    assert reallocation["trace"]["to_sleeve"] == "s2"
    assert reallocation["trace"]["evidence_refs"] == ["flex:rebalance-2"]
    assert {leg["flow_type"] for leg in artifact["flows"]} == {
        "TRANSFER:INTERNAL:SLEEVE"
    }
    assert {leg["class"] for leg in artifact["flows"]} <= {
        "INTERNAL_IN",
        "INTERNAL_OUT",
    }
    # every pair has an equal OUT and IN
    by_pair: dict[str, list[Decimal]] = {}
    for leg in artifact["flows"]:
        sign = 1 if leg["class"] == "INTERNAL_IN" else -1
        by_pair.setdefault(leg["pair_id"], []).append(sign * Decimal(leg["amount"]))
    assert all(sum(values) == 0 and len(values) == 2 for values in by_pair.values())

    # interest is allocated by exposure, disclosed apart and folded into NAV
    assert artifact["allocated_cost"]["account_costs"] == [
        {"day": D3, "interest": "-0.4", "fees": "0", "dividend_accrual": "0"}
    ]
    assert artifact["allocated_cost"]["sleeve_allocations"][0]["alloc_total"] == "-0.4"
    assert s2["days"][-1]["nav_end"] == "104"
    assert s2["days"][-1]["nav_end_after_alloc"] == "103.6"
    assert all(Decimal(item["unexplained"]) == 0 for item in artifact["closure"])
    assert artifact["closure_policy"]["max_abs_unexplained"] == "0"
    # the perimeter pseudo-account carries what no sleeve does
    perimeter = {day["day"]: day for day in artifact["perimeter_account"]["days"]}
    assert artifact["perimeter_account"]["opening_nav"] == "1000"
    assert perimeter[D3]["nav_end"] == "924"
    schema = json.loads(builder.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(artifact)


def test_a_sleeve_opened_and_closed_inside_the_account() -> None:
    funding = {
        "s1": {
            D1: _event("IN", "flex:open-s1", counterparty="UNALLOCATED_ACCOUNT_CASH")
        },
        "s2": {
            D2: _event("IN", "flex:open-s2", counterparty="UNALLOCATED_ACCOUNT_CASH"),
            D3: _event(
                "EXIT", "flex:close-s2", counterparty="UNALLOCATED_ACCOUNT_CASH"
            ),
        },
    }
    rows = _rows(
        [
            _trade(D1, "10", "10", "BookOne-1", "09:30:00"),
            _trade(D2, "5", "10", "BookTwo-1", "09:30:00"),
            _trade(D3, "-5", "11", "BookTwo-1", "09:30:00"),
        ],
        [
            _position(D1, "10", "10"),
            _position(D2, "15", "10"),
            _position(D3, "10", "11"),
            _position(D4, "10", "12"),
        ],
        [
            _change(D1, "1000", "1000"),
            _change(D2, "1000", "1000"),
            _change(D3, "1000", "1015"),
            _change(D4, "1015", "1025"),
        ],
    )
    artifact = _build(_declared(funding, D1, D4), rows)
    sleeves = {item["sleeve_id"]: item for item in artifact["sleeves"]}
    s2 = sleeves["s2"]
    assert (s2["first_day"], s2["last_day"]) == (D2, D3)
    assert [row["day"] for row in s2["days"]] == [D2, D3]
    assert s2["days"][-1]["nav_end"] == "0" and s2["days"][-1]["own_pnl"] == "5"
    assert [row["day"] for row in sleeves["s1"]["days"]] == [D1, D2, D3, D4]
    assert _legs(artifact, f"{PARENT}-s2") == [
        (D2, "INTERNAL_IN", "50"),
        (D3, "INTERNAL_OUT", "55"),
    ]
    assert _legs(artifact, UNALLOCATED) == [
        (D1, "INTERNAL_OUT", "100"),
        (D2, "INTERNAL_OUT", "50"),
        (D3, "INTERNAL_IN", "55"),
    ]
    assert all(Decimal(item["unexplained"]) == 0 for item in artifact["closure"])
    Draft202012Validator(json.loads(builder.SCHEMA_PATH.read_text("utf-8"))).validate(
        artifact
    )


def test_closure_residual_above_tolerance_refuses() -> None:
    declared, rows = _reallocate()
    changes = list(rows.change_in_nav)
    changes[2] = _change(D3, "1020", "1030", interest="-0.4")  # 2.4 unexplained
    broken = builder.AccountRows(rows.transactions, rows.positions, tuple(changes))
    with pytest.raises(builder.SleeveFactsError, match="per-day tolerance"):
        _build(declared, broken)
    # a residual within the declared tolerance is published and disclosed
    changes[2] = _change(D3, "1020", "1027.605", interest="-0.4")
    declared["closure_tolerance"] = {"per_day": "0.01", "window": "0.01"}
    near = builder.AccountRows(rows.transactions, rows.positions, tuple(changes))
    artifact = _build(declared, near)
    assert artifact["closure_policy"]["max_abs_unexplained"] == "0.005"
    changes[2] = _change(D3, "1020", "1027.608", interest="-0.4")
    declared["closure_tolerance"] = {"per_day": "0.01", "window": "0.005"}
    over = builder.AccountRows(rows.transactions, rows.positions, tuple(changes))
    with pytest.raises(builder.SleeveFactsError, match="window tolerance"):
        _build(declared, over)


def test_declared_unresolved_on_a_live_sleeve_names_the_ibkr_pull() -> None:
    declared, rows = _reallocate()
    declared["funding_events"]["s2"][D2] = _event(
        "IN", "flex:unresolved", evidence="DECLARED_UNRESOLVED", live=True
    )
    with pytest.raises(sn.SleeveNavError) as caught:
        _build(declared, rows)
    message = str(caught.value)
    assert "Flex cash transactions and trades" in message
    assert f"account {PARENT}" in message
    assert f"window {D1}..{D3}" in message
    assert "s2" in message and D2 in message

    # the same step on a sleeve that is not live stays visible, not refused
    declared["funding_events"]["s2"][D2] = _event(
        "IN", "flex:unresolved", evidence="DECLARED_UNRESOLVED", live=False
    )
    artifact = _build(declared, rows)
    s2 = next(item for item in artifact["sleeves"] if item["sleeve_id"] == "s2")
    assert s2["days"][0]["event_evidence"] == "DECLARED_UNRESOLVED"
    assert s2["days"][0]["live"] is False


def test_same_inputs_give_the_same_bytes_and_bind_every_input() -> None:
    declared, rows = _reallocate()
    first = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(declared),
        rows=rows,
        rows_sha256="1" * 64,
        parent_ledger_generation_id=GENERATION,
    )
    shuffled = builder.AccountRows(
        tuple(reversed(rows.transactions)),
        tuple(reversed(rows.positions)),
        tuple(reversed(rows.change_in_nav)),
    )
    second = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(declared),
        rows=shuffled,
        rows_sha256="1" * 64,
        parent_ledger_generation_id=GENERATION,
    )
    assert first == second
    assert b"\r" not in first and first.endswith(b"\n")
    bindings = json.loads(first)["bindings"]
    assert (
        bindings["tag_map_sha256"]
        == tag_map_module.validate_tag_map(_bytes(_tag_map())).sha256
    )
    assert (
        bindings["declared_inputs_sha256"]
        == hashlib.sha256(_bytes(declared)).hexdigest()
    )
    assert bindings["parent_ledger_generation_id"] == GENERATION
    assert bindings["account_rows_sha256"] == "1" * 64
    assert set(bindings["builder_code"]["files"]) == {
        f"tools/{name}.py" for name in builder._SOURCE_MODULES
    }
    changed = copy.deepcopy(declared)
    changed["funding_events"]["s2"][D2]["evidence_ref"] = "flex:other"
    third = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(changed),
        rows=rows,
        rows_sha256="1" * 64,
        parent_ledger_generation_id=GENERATION,
    )
    assert (
        json.loads(third)["bindings"]["declared_inputs_sha256"]
        != (bindings["declared_inputs_sha256"])
    )


def test_missing_inputs_refuse_instead_of_being_guessed() -> None:
    declared, rows = _reallocate()
    gap = builder.AccountRows(
        rows.transactions,
        rows.positions,
        (rows.change_in_nav[0], rows.change_in_nav[2]),
    )
    with pytest.raises(builder.SleeveFactsError, match="contiguous"):
        _build(declared, gap)
    short = builder.AccountRows(
        rows.transactions, rows.positions, rows.change_in_nav[:2]
    )
    with pytest.raises(builder.SleeveFactsError, match="declared window"):
        _build(declared, short)
    changes = [copy.deepcopy(dict(item)) for item in rows.change_in_nav]
    changes[1]["components"] = changes[1]["components"][:1]
    lacking = builder.AccountRows(rows.transactions, rows.positions, tuple(changes))
    with pytest.raises(builder.SleeveFactsError, match="does not state component"):
        _build(declared, lacking)
    no_ref = copy.deepcopy(declared)
    no_ref["funding_events"]["s1"][D1]["evidence_ref"] = " "
    with pytest.raises(builder.SleeveFactsError, match="evidence reference"):
        _build(no_ref, rows)
    missing_key = copy.deepcopy(declared)
    del missing_key["funding_events"]["s1"][D1]["live"]
    with pytest.raises(builder.SleeveFactsError, match="keys are invalid"):
        _build(missing_key, rows)
    foreign = builder.AccountRows(
        (*rows.transactions[:-1], {**rows.transactions[-1], "account_id": "other"}),
        rows.positions,
        rows.change_in_nav,
    )
    with pytest.raises(builder.SleeveFactsError, match="another account"):
        _build(declared, foreign)


def test_the_cli_writes_the_same_bytes(tmp_path: Path) -> None:
    declared, rows = _reallocate()
    (tmp_path / "tag.json").write_bytes(_bytes(_tag_map()))
    (tmp_path / "declared.json").write_bytes(_bytes(declared))
    rows_bytes = _bytes(
        {
            "transactions": list(rows.transactions),
            "positions": list(rows.positions),
            "change_in_nav_observations": list(rows.change_in_nav),
        }
    )
    (tmp_path / "rows.json").write_bytes(rows_bytes)
    args = [
        "--tag-map", str(tmp_path / "tag.json"),
        "--declared-inputs", str(tmp_path / "declared.json"),
        "--account-rows", str(tmp_path / "rows.json"),
        "--parent-ledger-generation-id", GENERATION,
        "--output", str(tmp_path / "out.json"),
    ]  # fmt: skip
    assert builder.main(args) == 0
    expected = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(declared),
        rows=rows,
        rows_sha256=hashlib.sha256(rows_bytes).hexdigest(),
        parent_ledger_generation_id=GENERATION,
    )
    assert (tmp_path / "out.json").read_bytes() == expected
    (tmp_path / "declared.json").write_bytes(b"{}")
    assert builder.main(args) == 1

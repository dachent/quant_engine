"""The Quantbase fund facts builder derives, pairs, binds and fails closed (#567).

Every fixture is synthetic: invented fund ids, titles, amounts and dates.  No
real account, fund or dollar figure of the owner appears here.
"""

from __future__ import annotations

import copy
import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
import pytest

import build_quantbase_fund_facts as builder
import build_sleeve_account_facts as ibkr_builder
import nested_strategy_tag_map as tag_map_module

PARENT = "account-parent"
UNALLOCATED = f"{PARENT}-unallocated"
GENERATION = "quantbase-generation-test-001"
D0 = date(2026, 1, 5)
ACTIVITY_COLUMNS = (
    "activity_type,amount,created_at,created_to_cover_fees,date,executed,filled,"
    "filled_at,fund,fund_title,id,is_rebalance,status,symbol,time,transaction,"
    "type,quantity"
).split(",")
SCHEMA = json.loads(builder.SCHEMA_PATH.read_text(encoding="utf-8"))


def _day(offset: int) -> date:
    return D0 + timedelta(days=offset)


def _at(offset: int, clock: str) -> str:
    return f"{_day(offset).isoformat()}T{clock}Z"


def _row(**fields: str) -> dict[str, str]:
    row = {column: "" for column in ACTIVITY_COLUMNS}
    row.update(fields)
    return row


def _txn(
    txn_id: str,
    fund: str,
    title: str,
    kind: str,
    amount: str,
    time: str,
    filled_at: str,
    symbol: str = "SYM",
    fee: bool = False,
) -> list[dict[str, str]]:
    return [
        _row(
            activity_type="filled_txn",
            id=txn_id,
            fund=fund,
            fund_title=title,
            type=kind,
            amount=amount,
            time=time,
            filled="Y",
            is_rebalance="False",
            created_to_cover_fees="True" if fee else "False",
        ),
        _row(
            activity_type="order",
            id=f"o{txn_id}",
            transaction=txn_id,
            fund=fund,
            symbol=symbol,
            executed="Y",
            filled_at=filled_at,
            created_to_cover_fees="True" if fee else "False",
            quantity="1",
        ),
    ]


def _activity_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    alpha, beta, gamma = ("11", "Alpha Fund"), ("22", "Beta Fund"), ("99", "Gamma Fund")
    rows += _txn(
        "a1", *alpha, "BUY", "100", _at(0, "13:00:00"), _at(0, "19:30:00"), "AAA"
    )
    # A later fee-paying order attaches to the first purchase; it must not
    # redate the purchase.
    rows.append(
        _row(
            activity_type="order",
            id="oa1fee",
            transaction="a1",
            fund="11",
            symbol="$USD",
            executed="Y",
            filled_at=_at(2, "11:00:00"),
            created_to_cover_fees="True",
        )
    )
    rows += _txn(
        "a2", *alpha, "SELL", "-1", _at(1, "11:00:00"), _at(1, "11:00:00"), "AAA", True
    )
    rows += _txn(
        "a3", *alpha, "BUY", "50", _at(2, "15:00:00"), _at(2, "20:00:00"), "AAA"
    )
    rows += _txn(
        "a4", *alpha, "MAX_S", "-156.5", _at(5, "04:00:00"), _at(5, "14:30:00"), "AAA"
    )
    rows.append(
        _row(
            activity_type="filled_txn",
            id="a5",
            fund="11",
            fund_title="Alpha Fund",
            type="RBLNC",
            amount="0.3",
            time=_at(2, "19:30:00"),
            filled="Y",
            is_rebalance="True",
        )
    )
    rows += _txn(
        "b1", *beta, "BUY", "200", _at(0, "13:00:01"), _at(0, "19:30:01"), "BBB"
    )
    rows += _txn(
        "b2", *beta, "MAX_S", "-204.5", _at(4, "10:00:00"), _at(4, "14:30:00"), "BBB"
    )
    rows += _txn(
        "g0", *gamma, "BUY", "50", _at(1, "13:00:00"), _at(1, "19:30:00"), "GGG"
    )
    rows += _txn(
        "g1", *gamma, "BUY", "204.49", _at(4, "10:00:30"), _at(4, "15:00:00"), "GGG"
    )
    rows.append(
        _row(
            activity_type="transfer",
            id="t1",
            type="DEPOSIT",
            amount="1000",
            created_at=_at(0, "09:00:00"),
            status="C",
        )
    )
    for index, (symbol, amount, offset) in enumerate(
        (("AAA", "2.5", 3), ("GGG", "1", 4), ("ZZZ", "0.4", 4))
    ):
        rows.append(
            _row(
                activity_type="dividend",
                id=f"d{index}",
                symbol=symbol,
                amount=amount,
                date=_at(offset, "04:00:00"),
            )
        )
    for journal_id, kind, amount, offset in (
        ("j1", "AUM_FEE", "0.5", 2),
        ("j2", "PARTNER_SUB_FEE", "0.25", 4),
    ):
        rows.append(
            _row(
                activity_type="journal",
                id=journal_id,
                type=kind,
                amount=amount,
                created_at=_at(offset, "11:00:00"),
                status="COMPLETE",
            )
        )
    return rows


def _csv(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _dashboard(
    values: list[str], costs: list[str] | None = None
) -> list[dict[str, str]]:
    costs = costs or ["0"] * len(values)
    return [
        {
            "time": _day(index).strftime("%m/%d/%Y"),
            "value": value,
            "cost_basis": cost,
        }
        for index, (value, cost) in enumerate(zip(values, costs, strict=True))
    ]


ALPHA = ["100", "101", "102", "153", "154", "156", "0", "0", "0"]
BETA = ["200", "201", "202", "203", "204", "0", "0", "0", "0"]
GAMMA = ["0", "0", "51", "52", "53", "258.5", "259", "260", "261"]


def _total(alpha: list[str], beta: list[str], gamma: list[str]) -> list[str]:
    return [
        str(sum(Decimal(v) for v in trio))
        for trio in zip(alpha, beta, gamma, strict=True)
    ]


def _inputs() -> dict[str, Any]:
    dash_cols = ["time", "value", "cost_basis"]
    gamma_rows = _dashboard(GAMMA, ["0"] * 5 + ["254.49"] * 4)
    # The live fund's file starts after the first day: absent means zero.
    gamma_rows = gamma_rows[1:]
    return {
        "activity": _activity_rows(),
        "accounts": [
            {"id": "bank", "type": "plaid_bank", "settled_balance": "5"},
            {"id": "broker", "type": "alpaca", "settled_balance": "760.00"},
        ],
        "dashboards": {
            "alpha": _dashboard(ALPHA, ["0"] * 8 + ["-7.5"]),
            "beta": _dashboard(BETA, ["0"] * 8 + ["-4.5"]),
            "gamma": gamma_rows,
            "total": _dashboard(_total(ALPHA, BETA, GAMMA)),
        },
        "columns": dash_cols,
        "declared": _declared(),
        "tag_map": _tag_map(),
    }


def _tag_map() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-01-13",
        "parents": [
            {
                "parent_account_id": PARENT,
                "provider_id": "provider-q",
                "tag_field": "fund_id",
                "tag_normalization": "NONE",
                "sleeves": [
                    {
                        "sleeve_id": name,
                        "strategy_id": f"strategy-{name}",
                        "label": name,
                        "effective_start": _day(0).isoformat(),
                        "effective_end": _day(end).isoformat(),
                        "tags": [{"match": "exact", "pattern": fund}],
                    }
                    for name, fund, end in (("alpha", "11", 5), ("beta", "22", 4))
                ],
                "unmatched": {"mode": "BLOCK"},
            }
        ],
    }


def _declared() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "quantbase-fund-declared-inputs",
        "parent_account_id": PARENT,
        "base_currency": "USD",
        "data_cutoff": _day(8).isoformat(),
        "closure_tolerance": {
            "cost_basis": "0.01",
            "nav_total": "0.02",
            "pairing_amount": "0.01",
            "exit_relative": "0.01",
        },
        "pairing": {
            "rule": "EXACT_AMOUNT_DAY_AND_TIME_ELSE_UNALLOCATED",
            "window_seconds": 120,
        },
        "total_dashboard": "total",
        "nav_total_exempt_days": {},
        "funds": {
            "alpha": {
                "fund_id": "11",
                "fund_title": "Alpha Fund",
                "dashboard": "alpha",
                "first_nonzero_day": _day(0).isoformat(),
                "last_nonzero_day": _day(5).isoformat(),
                "vendor_cost_basis": "-7.5",
                "opening_tolerance": "0.05",
            },
            "beta": {
                "fund_id": "22",
                "fund_title": "Beta Fund",
                "dashboard": "beta",
                "first_nonzero_day": _day(0).isoformat(),
                "last_nonzero_day": _day(4).isoformat(),
                "vendor_cost_basis": "-4.5",
                "opening_tolerance": "0.05",
            },
        },
        "out_of_scope_funds": {
            "99": {
                "fund_title": "Gamma Fund",
                "dashboard": "gamma",
                "counterparty_account_id": PARENT,
            }
        },
        "open_items": {
            "funding_attribution": {
                "evidence_class": "NOT_IN_SOURCE_BYTES",
                "owner_ruling_ref": None,
                "treatment": "UNALLOCATED_REMAINDER",
                "note": "no link from cash to purchase",
            },
            "cash_residual": {
                "evidence_class": "UNEXPLAINED_RESIDUAL",
                "owner_ruling_ref": None,
                "amount": "0.66",
                "tolerance": "0.01",
                "note": "cash is short",
            },
            "dividend_nav_treatment": {
                "evidence_class": "NOT_IN_SOURCE_BYTES",
                "owner_ruling_ref": None,
                "treatment": "NOT_FOLDED_INTO_NAV",
                "note": "not told by the bytes",
            },
        },
    }


def _bytes(document: object) -> bytes:
    return json.dumps(document, sort_keys=True).encode()


def _build(inputs: dict[str, Any] | None = None) -> bytes:
    data = inputs or _inputs()
    return builder.build_quantbase_fund_facts(
        tag_map_bytes=_bytes(data["tag_map"]),
        declared_inputs_bytes=_bytes(data["declared"]),
        account_activity=_csv(ACTIVITY_COLUMNS, data["activity"]),
        accounts=_csv(["id", "type", "settled_balance"], data["accounts"]),
        dashboards={
            key: _csv(data["columns"], rows) for key, rows in data["dashboards"].items()
        },
        parent_ledger_generation_id=GENERATION,
    )


def _facts(inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    return json.loads(_build(inputs))  # type: ignore[no-any-return]


def _refused(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data = _inputs()
    mutate(data)
    with pytest.raises(builder.QuantbaseFundFactsError, match=message):
        _build(data)


def _sleeve(facts: dict[str, Any], sleeve_id: str) -> dict[str, Any]:
    return next(item for item in facts["sleeves"] if item["sleeve_id"] == sleeve_id)


def _day_row(facts: dict[str, Any], sleeve_id: str, offset: int) -> dict[str, Any]:
    return next(
        item
        for item in _sleeve(facts, sleeve_id)["days"]
        if item["day"] == _day(offset).isoformat()
    )


def test_artifact_validates_against_its_schema() -> None:
    facts = _facts()
    Draft202012Validator.check_schema(SCHEMA)
    assert list(Draft202012Validator(SCHEMA).iter_errors(facts)) == []
    assert facts["artifact_id"] == "quantbase-fund-facts"
    assert [s["account_id"] for s in facts["sleeves"]] == [
        f"{PARENT}-alpha",
        f"{PARENT}-beta",
    ]


def test_flow_vocabulary_is_the_ibkr_sleeve_facts_vocabulary() -> None:
    assert builder.FLOW_TYPE == ibkr_builder.FLOW_TYPE
    assert builder.UNALLOCATED_SUFFIX == ibkr_builder.UNALLOCATED_SUFFIX
    ibkr_schema = json.loads(ibkr_builder.SCHEMA_PATH.read_text(encoding="utf-8"))
    theirs = ibkr_schema["properties"]["flows"]["items"]
    mine = SCHEMA["properties"]["flows"]["items"]
    assert set(theirs["required"]) <= set(mine["required"])
    for key in theirs["required"]:
        if key not in {"trace", "amount"}:
            assert mine["properties"][key] == theirs["properties"][key]
    assert set(mine["required"]) - set(theirs["required"]) == {"leg_source"}


def test_exit_day_return_includes_the_exit_flow() -> None:
    facts = _facts()
    beta_exit = _day_row(facts, "beta", 5)
    assert beta_exit["event"] == "EXIT"
    assert beta_exit["nav_prev"] == "204"
    assert beta_exit["flow_out"] == "-204.5"
    assert beta_exit["nav_end"] == "0"
    assert _close(beta_exit["interval_return"], Decimal("204.5") / Decimal("204") - 1)
    assert Decimal(beta_exit["interval_return"]) > 0
    alpha_exit = _day_row(facts, "alpha", 6)
    assert alpha_exit["event"] == "EXIT"
    assert _close(alpha_exit["interval_return"], Decimal("156.5") / Decimal("156") - 1)
    # The sleeve ends on its exit day: no row carries a zero NAV afterwards.
    assert _sleeve(facts, "beta")["last_day"] == _day(5).isoformat()
    assert _sleeve(facts, "beta")["last_nonzero_day"] == _day(4).isoformat()


def test_a_fill_is_recognized_one_day_later_and_the_opening_the_same_day() -> None:
    facts = _facts()
    opening = _day_row(facts, "alpha", 0)
    assert opening["event"] == "OPEN"
    assert opening["flow_in"] == "100"
    assert opening["interval_return"] is None
    # A fee-paying order attached to the first purchase does not redate it.
    assert _day_row(facts, "alpha", 3)["flow_in"] == "50"
    assert _day_row(facts, "alpha", 2)["flow_in"] == "0"
    # Own P&L is value less base less flows: a fund that ends at zero earned
    # exactly what its flows took out less what they put in.
    for sleeve_id in ("alpha", "beta"):
        days = _sleeve(facts, sleeve_id)["days"]
        pnl = sum(Decimal(r["own_pnl"]) for r in days)
        flows = sum(Decimal(r["flow_in"]) + Decimal(r["flow_out"]) for r in days)
        assert pnl == -flows
        assert (
            Decimal(facts["closure"]["fund_summaries"][sleeve_id]["own_pnl_total"])
            == pnl
        )
    assert facts["disclosures"]["fee_funding_sells"] == [
        {
            "sleeve_id": "alpha",
            "day": _day(1).isoformat(),
            "amount": "-1",
            "record_id": "quantbase:filled_txn:a2",
        }
    ]
    assert facts["closure"]["fund_summaries"]["alpha"]["rebalance_count"] == 1
    assert facts["closure"]["fund_summaries"]["alpha"]["rebalance_net_amount"] == "0.3"


def _close(text: str, expected: Decimal) -> bool:
    return abs(Decimal(text) - expected) < Decimal("1e-20")


def _legs(facts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pairs: dict[str, list[dict[str, Any]]] = {}
    for leg in facts["flows"]:
        pairs.setdefault(leg["pair_id"], []).append(leg)
    return pairs


def test_reallocation_pairs_an_exit_with_the_counterpart_purchase() -> None:
    facts = _facts()
    pairs = _legs(facts)
    for legs in pairs.values():
        assert sorted(leg["class"] for leg in legs) == ["INTERNAL_IN", "INTERNAL_OUT"]
        assert {leg["flow_type"] for leg in legs} == {"TRANSFER:INTERNAL:SLEEVE"}
        assert len({leg["amount"] for leg in legs}) == 1
        assert len({json.dumps(leg["trace"], sort_keys=True) for leg in legs}) == 1
    real = [
        legs
        for legs in pairs.values()
        if legs[0]["trace"]["pairing"] == "COUNTERPARTY_PAIR"
    ]
    assert len(real) == 1
    out = next(leg for leg in real[0] if leg["class"] == "INTERNAL_OUT")
    into = next(leg for leg in real[0] if leg["class"] == "INTERNAL_IN")
    assert out["account_id"] == f"{PARENT}-beta"
    assert into["account_id"] == PARENT
    assert into["leg_source"] == "PARENT_LEDGER_REFERENCE"
    assert out["leg_source"] == "ARTIFACT"
    assert out["amount"] == "204.49"
    assert out["event_date"] == _day(4).isoformat()
    assert out["nav_recognition_date"] == _day(5).isoformat()
    assert out["trace"]["kind"] == "REALLOCATION"
    assert out["trace"]["from_sleeve"] == "beta"
    assert out["trace"]["to_sleeve"] is None
    assert out["trace"]["pair_amount_difference"] == "0.01"
    assert out["trace"]["evidence_refs"] == [
        "quantbase:filled_txn:b2",
        "quantbase:filled_txn:g1",
    ]
    # The cent the pair does not cover is an unallocated remainder.
    remainders = [
        legs
        for legs in pairs.values()
        if legs[0]["trace"]["pairing"] == "UNALLOCATED_REMAINDER"
        and any(leg["account_id"] == f"{PARENT}-beta" for leg in legs)
        and legs[0]["amount"] == "0.01"
    ]
    assert len(remainders) == 1
    assert {leg["account_id"] for leg in remainders[0]} == {
        f"{PARENT}-beta",
        UNALLOCATED,
    }


def test_every_sleeve_flow_is_covered_by_legs_and_nothing_else_is_a_flow() -> None:
    facts = _facts()
    for sleeve_id, account in (
        ("alpha", f"{PARENT}-alpha"),
        ("beta", f"{PARENT}-beta"),
    ):
        net = Decimal(0)
        for leg in facts["flows"]:
            if leg["account_id"] == account:
                sign = 1 if leg["class"] == "INTERNAL_IN" else -1
                net += sign * Decimal(leg["amount"])
        days = _sleeve(facts, sleeve_id)["days"]
        flows = sum(Decimal(d["flow_in"]) + Decimal(d["flow_out"]) for d in days)
        assert net == flows
    # The exit with no counterpart is paired with the perimeter account whole.
    unpaired = [
        legs
        for legs in _legs(facts).values()
        if legs[0]["trace"]["from_sleeve"] == "alpha"
    ]
    assert len(unpaired) == 1
    assert unpaired[0][0]["trace"]["pairing"] == "UNALLOCATED_REMAINDER"
    assert {leg["amount"] for leg in unpaired[0]} == {"156.5"}
    # The opening purchase is paired with the perimeter and recognized at once.
    opening = [
        leg
        for leg in facts["flows"]
        if leg["account_id"] == f"{PARENT}-alpha"
        and leg["event_date"] == _day(0).isoformat()
    ]
    assert [leg["nav_recognition_date"] for leg in opening] == [_day(0).isoformat()]
    # Deposits, fees and the fee-funding sale are not flows.
    assert {leg["flow_type"] for leg in facts["flows"]} == {"TRANSFER:INTERNAL:SLEEVE"}
    assert {leg["class"] for leg in facts["flows"]} == {"INTERNAL_IN", "INTERNAL_OUT"}
    assert facts["conventions"]["deposits"] == "ACCOUNT_LEVEL_NOT_ATTRIBUTED_TO_A_FUND"
    assert facts["closure"]["account"]["cash"]["deposits"] == "1000"
    perimeter = {
        d["day"]: d["internal_flow_net"] for d in facts["perimeter_account"]["days"]
    }
    # Only the cent the reallocation does not cover reaches the perimeter.
    assert perimeter[_day(4).isoformat()] == "0.01"


def test_a_purchase_far_from_the_exit_is_not_a_reallocation() -> None:
    def later(data: dict[str, Any]) -> None:
        for row in data["activity"]:
            if row["id"] == "g1":
                row["time"] = _at(4, "10:30:00")

    data = _inputs()
    later(data)
    facts = _facts(data)
    assert {leg["trace"]["pairing"] for leg in facts["flows"]} == {
        "UNALLOCATED_REMAINDER"
    }
    assert (
        facts["open_items"]["funding_attribution"]["unallocated_remainder_pairs"] == 5
    )


def test_an_ambiguous_counterpart_is_refused_not_guessed() -> None:
    def twin(data: dict[str, Any]) -> None:
        data["activity"] += _txn(
            "g2",
            "99",
            "Gamma Fund",
            "BUY",
            "204.49",
            _at(4, "10:00:50"),
            _at(4, "15:01:00"),
        )
        data["accounts"][1]["settled_balance"] = "555.51"

    _refused(twin, "more than one counterpart")


def test_fund_closure_to_the_vendor_cost_basis_is_fail_closed() -> None:
    def cost(data: dict[str, Any]) -> None:
        data["dashboards"]["alpha"][-1]["cost_basis"] = "-7.4"

    _refused(cost, "do not close to the vendor cost basis")

    def declared(data: dict[str, Any]) -> None:
        data["declared"]["funds"]["beta"]["vendor_cost_basis"] = "-4.0"

    _refused(declared, "do not close to the vendor cost basis")

    def missing_fill(data: dict[str, Any]) -> None:
        data["activity"] = [r for r in data["activity"] if r["id"] != "a3"]

    _refused(missing_fill, "do not close to the vendor cost basis")


def test_account_closure_to_the_vendor_total_is_fail_closed() -> None:
    def total(data: dict[str, Any]) -> None:
        data["dashboards"]["total"][2]["value"] = "999"

    _refused(total, "differ from the vendor total")


def test_an_exempt_day_must_be_a_real_post_exit_disagreement() -> None:
    def stale(data: dict[str, Any]) -> None:
        data["declared"]["nav_total_exempt_days"] = {_day(8).isoformat(): "agrees"}

    _refused(stale, "not a post-exit disagreement")

    def during(data: dict[str, Any]) -> None:
        data["dashboards"]["total"][2]["value"] = "999"
        data["declared"]["nav_total_exempt_days"] = {_day(2).isoformat(): "x"}

    _refused(during, "not a post-exit disagreement")

    data = _inputs()
    data["dashboards"]["total"][8]["value"] = "1"
    data["declared"]["nav_total_exempt_days"] = {_day(8).isoformat(): "disagree"}
    facts = _facts(data)
    exempt = facts["closure"]["account"]["nav_total"]["exempt_days"]
    assert [item["day"] for item in exempt] == [_day(8).isoformat()]
    assert exempt[0]["difference"] == "260"


def test_the_cash_residual_must_be_the_declared_one() -> None:
    def amount(data: dict[str, Any]) -> None:
        data["declared"]["open_items"]["cash_residual"]["amount"] = "0.50"

    _refused(amount, "not the declared")

    def provider(data: dict[str, Any]) -> None:
        data["accounts"][1]["settled_balance"] = "700"

    _refused(provider, "not the declared")
    cash = _facts()["closure"]["account"]["cash"]
    assert cash["derived_cash"] == "760.66"
    assert cash["residual"] == "0.66"
    assert cash["net_fund_flows_by_fund"] == {
        "11": "-7.5",
        "22": "-4.5",
        "99": "254.49",
    }
    assert cash["net_fund_flows"] == "242.49"


def test_dividends_are_booked_once() -> None:
    dividends = _facts()["closure"]["account"]["dividends"]
    assert dividends["total"] == "3.9"
    assert dividends["attributed_to_sleeves"] == {"alpha": "2.5"}
    assert dividends["attributed_to_counterparty_funds"] == {"99": "1"}
    assert dividends["unattributed"] == "0.4"
    total = (
        Decimal(dividends["attributed_to_sleeves"]["alpha"])
        + Decimal(dividends["attributed_to_counterparty_funds"]["99"])
        + Decimal(dividends["unattributed"])
    )
    assert total == Decimal(dividends["total"])
    income = _facts()["disclosures"]["attributed_income"]
    assert [
        (item["sleeve_id"], item["amount"], item["after_exit"]) for item in income
    ] == [("alpha", "2.5", False)]


def test_epoch_dates_are_taken_from_the_bytes_not_from_a_stated_end() -> None:
    def stated(data: dict[str, Any]) -> None:
        data["declared"]["funds"]["beta"]["last_nonzero_day"] = _day(8).isoformat()
        data["tag_map"]["parents"][0]["sleeves"][1]["effective_end"] = _day(
            8
        ).isoformat()

    _refused(stated, "declared epoch")

    def revived(data: dict[str, Any]) -> None:
        data["dashboards"]["beta"][7]["value"] = "5"
        data["dashboards"]["total"][7]["value"] = "265"

    _refused(revived, "declared epoch")

    def interval(data: dict[str, Any]) -> None:
        data["tag_map"]["parents"][0]["sleeves"][0]["effective_end"] = _day(
            7
        ).isoformat()

    _refused(interval, "does not end on the declared last non-zero day")


def test_exit_proceeds_must_match_the_last_value() -> None:
    def proceeds(data: dict[str, Any]) -> None:
        for row in data["activity"]:
            if row["id"] == "b2":
                row["amount"] = "-250"
        data["declared"]["funds"]["beta"]["vendor_cost_basis"] = "-50"
        data["dashboards"]["beta"][-1]["cost_basis"] = "-50"

    _refused(proceeds, "differ from the last value")


def test_an_unknown_fund_or_activity_is_refused() -> None:
    def fund(data: dict[str, Any]) -> None:
        data["activity"] += _txn(
            "x1", "77", "Other", "BUY", "5", _at(1, "10:00:00"), _at(1, "11:00:00")
        )

    _refused(fund, "neither a sleeve nor a declared")

    def kind(data: dict[str, Any]) -> None:
        data["activity"].append(_row(activity_type="mystery", id="m"))

    _refused(kind, "unknown activity type")

    def journal(data: dict[str, Any]) -> None:
        data["activity"].append(
            _row(
                activity_type="journal",
                id="j9",
                type="NEW_FEE",
                amount="1",
                created_at=_at(1, "10:00:00"),
                status="COMPLETE",
            )
        )

    _refused(journal, "unknown journal type")

    def title(data: dict[str, Any]) -> None:
        for row in data["activity"]:
            if row["fund"] == "22":
                row["fund_title"] = "Renamed"

    _refused(title, "is titled")


def test_missing_or_extra_dashboards_are_refused() -> None:
    def missing(data: dict[str, Any]) -> None:
        del data["dashboards"]["gamma"]

    _refused(missing, "dashboards must be exactly")

    def extra(data: dict[str, Any]) -> None:
        data["dashboards"]["stray"] = data["dashboards"]["gamma"]

    _refused(extra, "dashboards must be exactly")

    def short(data: dict[str, Any]) -> None:
        data["dashboards"]["alpha"] = data["dashboards"]["alpha"][:7]

    _refused(short, "before the cutoff")

    def gap(data: dict[str, Any]) -> None:
        del data["dashboards"]["beta"][2]

    _refused(gap, "has no row for")


def test_open_items_must_carry_their_evidence_class() -> None:
    def klass(data: dict[str, Any]) -> None:
        data["declared"]["open_items"]["funding_attribution"]["evidence_class"] = (
            "GUESS"
        )

    _refused(klass, "evidence class is invalid")

    def ruled(data: dict[str, Any]) -> None:
        data["declared"]["open_items"]["cash_residual"]["evidence_class"] = (
            "OWNER_RULED"
        )

    _refused(ruled, "ruling reference")

    def treatment(data: dict[str, Any]) -> None:
        data["declared"]["open_items"]["dividend_nav_treatment"]["treatment"] = "ADD"

    _refused(treatment, "treatment is invalid")

    def absent(data: dict[str, Any]) -> None:
        del data["declared"]["open_items"]["cash_residual"]

    _refused(absent, "open_items keys are invalid")
    items = _facts()["open_items"]
    assert items["cash_residual"]["evidence_class"] == "UNEXPLAINED_RESIDUAL"
    assert items["funding_attribution"]["treatment"] == "UNALLOCATED_REMAINDER"


def test_the_tag_map_must_tag_by_fund_id_and_block_unmatched() -> None:
    def field(data: dict[str, Any]) -> None:
        data["tag_map"]["parents"][0]["tag_field"] = "title"

    _refused(field, "tag by fund_id")

    def fallback(data: dict[str, Any]) -> None:
        parent = data["tag_map"]["parents"][0]
        parent["unmatched"] = {
            "mode": "ASSIGN_BY_HOLDING_SYMBOL",
            "fallback_sleeve_id": "alpha",
        }

    _refused(fallback, "tag by fund_id")

    def window(data: dict[str, Any]) -> None:
        data["tag_map"]["parents"][0]["sleeves"][0]["effective_start"] = _day(
            1
        ).isoformat()

    _refused(window, "does not resolve")


def test_a_fill_after_the_cutoff_or_the_exit_is_refused() -> None:
    def late(data: dict[str, Any]) -> None:
        data["activity"] += _txn(
            "z1", "22", "Beta Fund", "BUY", "5", _at(9, "10:00:00"), _at(9, "11:00:00")
        )

    _refused(late, "after the cutoff")

    def after_exit(data: dict[str, Any]) -> None:
        data["activity"] += _txn(
            "z2", "22", "Beta Fund", "BUY", "5", _at(6, "10:00:00"), _at(6, "11:00:00")
        )

    _refused(after_exit, "outside the tag map's effective dates")


def test_the_build_is_deterministic_and_order_independent() -> None:
    first = _build()
    assert _build() == first
    shuffled = _inputs()
    shuffled["activity"].reverse()
    # Row order changes the source bytes, so only their hash may differ.
    reordered = json.loads(_build(shuffled))
    original = json.loads(first)
    assert (
        reordered["bindings"]["source_sha256"]["account_activity"]
        != original["bindings"]["source_sha256"]["account_activity"]
    )
    reordered["bindings"]["source_sha256"]["account_activity"] = "x"
    original["bindings"]["source_sha256"]["account_activity"] = "x"
    assert reordered == original
    assert first.endswith(b"\n") and first.isascii()
    assert json.loads(first) == json.loads(_build())


def test_every_input_is_hash_bound() -> None:
    data = _inputs()
    facts = _facts(data)
    bindings = facts["bindings"]
    assert (
        bindings["tag_map_sha256"]
        == tag_map_module.validate_tag_map(_bytes(data["tag_map"])).sha256
    )
    assert (
        bindings["declared_inputs_sha256"]
        == hashlib.sha256(_bytes(data["declared"])).hexdigest()
    )
    assert (
        bindings["source_sha256"]["account_activity"]
        == hashlib.sha256(_csv(ACTIVITY_COLUMNS, data["activity"])).hexdigest()
    )
    assert set(bindings["source_sha256"]["dashboards"]) == {
        "alpha",
        "beta",
        "gamma",
        "total",
    }
    assert bindings["parent_ledger_generation_id"] == GENERATION
    assert sorted(bindings["builder_code"]["files"]) == [
        "tools/build_quantbase_fund_facts.py",
        "tools/nested_strategy_tag_map.py",
    ]
    changed = copy.deepcopy(data)
    changed["declared"]["open_items"]["cash_residual"]["note"] = "other"
    assert (
        _facts(changed)["bindings"]["declared_inputs_sha256"]
        != bindings["declared_inputs_sha256"]
    )
    with pytest.raises(builder.QuantbaseFundFactsError, match="generation id"):
        builder.build_quantbase_fund_facts(
            tag_map_bytes=_bytes(data["tag_map"]),
            declared_inputs_bytes=_bytes(data["declared"]),
            account_activity=b"",
            accounts=b"",
            dashboards={},
            parent_ledger_generation_id="bad id",
        )


def test_the_cli_writes_the_artifact_or_refuses_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _inputs()
    paths = {
        "tag": tmp_path / "tag.json",
        "declared": tmp_path / "declared.json",
        "activity": tmp_path / "activity.csv",
        "accounts": tmp_path / "accounts.csv",
    }
    paths["tag"].write_bytes(_bytes(data["tag_map"]))
    paths["declared"].write_bytes(_bytes(data["declared"]))
    paths["activity"].write_bytes(_csv(ACTIVITY_COLUMNS, data["activity"]))
    paths["accounts"].write_bytes(
        _csv(["id", "type", "settled_balance"], data["accounts"])
    )
    args = [
        "--tag-map",
        str(paths["tag"]),
        "--declared-inputs",
        str(paths["declared"]),
        "--account-activity",
        str(paths["activity"]),
        "--accounts",
        str(paths["accounts"]),
        "--parent-ledger-generation-id",
        GENERATION,
    ]
    for key, rows in data["dashboards"].items():
        path = tmp_path / f"{key}.csv"
        path.write_bytes(_csv(data["columns"], rows))
        args += ["--dashboard", f"{key}={path}"]
    output = tmp_path / "facts.json"
    assert builder.main([*args, "--output", str(output)]) == 0
    assert output.read_bytes() == _build(data)
    refused = tmp_path / "refused.json"
    paths["accounts"].write_bytes(
        _csv(
            ["id", "type", "settled_balance"],
            [{"id": "b", "type": "bank", "settled_balance": "1"}],
        )
    )
    assert builder.main([*args, "--output", str(refused)]) == 1
    assert not refused.exists()
    assert "quantbase fund facts refused" in capsys.readouterr().err

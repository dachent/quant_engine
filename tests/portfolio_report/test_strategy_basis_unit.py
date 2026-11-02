"""Unit tests of the opt-in strategy-basis module on synthetic data (#624, option B)."""

from __future__ import annotations

from decimal import Decimal, localcontext
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import portfolio_baseline
import strategy_basis

SLEEVE_STRATEGY = "strat-sleeve"


def _links() -> list[dict[str, str]]:
    # opens 100 on 07-02; +2% to 07-03; a 100 transfer in at end of 07-06 then -1%
    return [
        {
            "start_date": "2026-07-02",
            "end_date": "2026-07-03",
            "factor": "1.02",
            "ending_nav": "102",
            "ending_flow": "0",
        },
        {
            "start_date": "2026-07-03",
            "end_date": "2026-07-06",
            "factor": "0.99",
            "ending_nav": "200.98",
            "ending_flow": "100",
        },
    ]


def _restatement() -> strategy_basis.SleeveRestatement:
    return strategy_basis.sleeve_restatement(
        strategy_id=SLEEVE_STRATEGY,
        account_id="acct-s2",
        parent_account_id="acct",
        provider_id="p",
        links=_links(),
    )


def _block() -> dict[str, Any]:
    def row(strategy_id: str, value: str, weight: str) -> dict[str, Any]:
        amount = {"currency": "USD", "status": "AVAILABLE", "value": value}
        return {
            "strategy_id": strategy_id,
            "scope": "SYSTEMATIC",
            "status": "AVAILABLE",
            "as_of_date": "2026-07-06",
            "current_value": amount,
            "current_weight": weight,
            "members": [
                {
                    "account_id": f"acct-{strategy_id}",
                    "as_of_date": "2026-07-06",
                    "assignment_ids": ["assignment-x"],
                    "effective_end": None,
                    "effective_start": "2026-01-22",
                    "nav_record_id": "nav-x",
                    "source_key": "provider-ledger",
                    "status": "AVAILABLE",
                    "value": amount,
                }
            ],
        }

    return {
        "strategies": [
            row("strat-a", "100", "0.1"),
            row("strat-b", "700", "0.7"),
            row(SLEEVE_STRATEGY, "200", "0.2"),
        ],
        "systematic": {
            "as_of_date": "2026-07-06",
            "status": "AVAILABLE",
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "1000",
            },
        },
        "total_liquid": {
            "as_of_date": "2026-07-06",
            "status": "AVAILABLE",
            "current_value": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": "1100",
            },
        },
    }


def test_the_series_is_one_row_per_link() -> None:
    restatement = _restatement()
    assert [(r["date"], r["return"]) for r in restatement.rows] == [
        ("2026-07-03", "0.02"),
        ("2026-07-06", "-0.01"),
    ]
    assert restatement.value == Decimal("200.98")
    assert restatement.as_of_date == "2026-07-06"
    assert restatement.opening_date == "2026-07-02"
    assert all(r["component_account_ids"] == ["acct-s2"] for r in restatement.rows)


def test_a_trace_whose_dates_do_not_advance_is_refused() -> None:
    links = _links()
    links[1]["end_date"] = "2026-07-03"
    with pytest.raises(strategy_basis.StrategyBasisError, match="do not advance"):
        strategy_basis.sleeve_restatement(
            strategy_id=SLEEVE_STRATEGY,
            account_id="a-s2",
            parent_account_id="a",
            provider_id="p",
            links=links,
        )


def test_restate_returns_swaps_only_the_declared_live_rows() -> None:
    restatement = _restatement()
    rows = [
        {"strategy_id": SLEEVE_STRATEGY, "evidence_layer": "ACCOUNT_LIVE", "date": "x"},
        {"strategy_id": SLEEVE_STRATEGY, "evidence_layer": "BACKTEST", "date": "y"},
        {"strategy_id": "strat-a", "evidence_layer": "ACCOUNT_LIVE", "date": "z"},
    ]
    result = strategy_basis.restate_returns(rows, {SLEEVE_STRATEGY: restatement})
    assert [r["date"] for r in result[:2]] == ["y", "z"]
    assert [r["date"] for r in result[2:]] == ["2026-07-03", "2026-07-06"]
    assert rows[0]["date"] == "x"


def test_restated_capital_recomputes_value_weights_and_total() -> None:
    block = _block()
    restated = strategy_basis.restate_current_capital(
        block, {SLEEVE_STRATEGY: _restatement()}
    )
    rows = {row["strategy_id"]: row for row in restated["strategies"]}
    assert rows[SLEEVE_STRATEGY]["current_value"]["value"] == "200.98"
    assert restated["systematic"]["current_value"]["value"] == "1000.98"
    # total liquid moves by the same amount so it keeps equalling its components
    assert restated["total_liquid"]["current_value"]["value"] == "1100.98"
    with localcontext() as context:
        context.prec = 34
        weights = {k: Decimal(v["current_weight"]) for k, v in rows.items()}
        assert sum(weights.values()) == 1
        for key, row in rows.items():
            share = Decimal(row["current_value"]["value"]) / Decimal("1000.98")
            assert abs(weights[key] - share) < Decimal("1e-30")
    # the member's identity is the sleeve epoch's, not the parent member's
    member = rows[SLEEVE_STRATEGY]["members"][0]
    assert member["assignment_ids"] == ["derived-sleeve:acct-s2:2026-07-02"]
    assert (member["effective_start"], member["effective_end"]) == ("2026-07-02", None)
    # a restated row keeps the closed custody schema's exact key sets
    assert set(rows[SLEEVE_STRATEGY]) == set(block["strategies"][2])
    assert set(rows[SLEEVE_STRATEGY]["members"][0]) == set(
        block["strategies"][2]["members"][0]
    )
    schema = json.loads(
        (
            Path(__file__).parents[2] / "schemas/portfolio/report-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.validate(
        rows[SLEEVE_STRATEGY]["members"][0],
        {"$ref": "#/$defs/custody_handoff_current_member", "$defs": schema["$defs"]},
    )
    # the input block is untouched
    assert block["strategies"][2]["current_value"]["value"] == "200"


def test_a_restated_strategy_absent_from_capital_is_refused() -> None:
    other = strategy_basis.SleeveRestatement(
        strategy_id="nope",
        account_id="a-s2",
        parent_account_id="a",
        provider_id="p",
        rows=(),
        value=Decimal(1),
        as_of_date="2026-09-17",
        opening_date="2026-01-30",
        assignment_id="derived-sleeve:a-s2:2026-01-30",
        effective_start="2026-01-30",
        effective_end=None,
    )
    with pytest.raises(
        strategy_basis.StrategyBasisError, match="absent from current capital"
    ):
        strategy_basis.restate_current_capital(_block(), {"nope": other})


def test_closed_weights_are_the_baseline_rule() -> None:
    values = {
        "a": Decimal("57200.875514128"),
        "b": Decimal("51259.8"),
        "c": Decimal("12345.678901234"),
    }
    assert strategy_basis.closed_weights(values) == portfolio_baseline._closed_weights(
        values
    )


def test_the_scope_names_the_retired_sleeves_and_what_is_not_restated() -> None:
    retired = ["acct-s1", "acct-s3"]
    scope = strategy_basis.return_scope(_restatement(), retired)
    assert scope["basis"] == "SLEEVE_EPOCH"
    assert scope["excludes_retired_sleeve_accounts"] == retired
    assert scope["not_restated"] == list(strategy_basis.NOT_RESTATED)
    assert "sleeve s2 of parent account acct, from 2026-07-02" in (
        strategy_basis.return_scope_label(_restatement())
    )


def _declaration(*entries: dict[str, Any]) -> bytes:
    return json.dumps(
        {"schema": strategy_basis.DECLARATION_SCHEMA, "strategies": list(entries)}
    ).encode()


def test_a_declaration_is_strict() -> None:
    entry = {"strategy_id": SLEEVE_STRATEGY, "basis": "SLEEVE_EPOCH"}
    assert strategy_basis.load_declaration(_declaration(entry)) == {
        SLEEVE_STRATEGY: "SLEEVE_EPOCH"
    }
    for bad in (
        b"{",
        b"[]",
        json.dumps({"schema": "x", "strategies": []}).encode(),
        _declaration(),
        _declaration({**entry, "basis": "PARENT_ACCOUNT_LIVE_SERIES"}),
        _declaration(entry, entry),
        _declaration({**entry, "x": 1}),
    ):
        with pytest.raises(strategy_basis.StrategyBasisError):
            strategy_basis.load_declaration(bad)


def test_provider_evidence_scopes_move_to_the_sleeve() -> None:
    evidence = [
        {
            "strategy_id": SLEEVE_STRATEGY,
            "evidence_layer": "ACCOUNT_LIVE",
            "account": {
                "observed_start": "2026-01-22",
                "account_scopes": [
                    {
                        "account_id": "acct",
                        "observed_start": "2026-01-22",
                        "observed_end": "2026-07-06",
                    }
                ],
            },
        },
        {
            "strategy_id": "strat-a",
            "evidence_layer": "ACCOUNT_LIVE",
            "account": {"account_scopes": [{"account_id": "acct"}]},
        },
    ]
    result = strategy_basis.restate_provider_evidence(
        evidence, {SLEEVE_STRATEGY: _restatement()}
    )
    assert result[0]["account"]["account_scopes"] == [
        {
            "account_id": "acct-s2",
            "observed_start": "2026-07-02",
            "observed_end": "2026-07-06",
        }
    ]
    assert result[1] == evidence[1]
    assert evidence[0]["account"]["account_scopes"][0]["account_id"] == "acct"


def test_recorded_allocations_are_labelled_not_rewritten() -> None:
    restatement = _restatement()
    rows = [
        {
            "strategy_id": SLEEVE_STRATEGY,
            "as_of_date": "2026-08-16",
            "current_value_usd": "57400.57",
            "systematic_weight": "0.1285",
        },
        {"strategy_id": "other", "as_of_date": "2026-08-16", "current_value_usd": "1"},
    ]
    labelled = strategy_basis.label_recorded_allocations(
        rows, {SLEEVE_STRATEGY: restatement}
    )
    first = labelled[0]
    assert first["current_value_usd"] == "57400.57"
    assert first["systematic_weight"] == "0.1285"
    assert first["value_basis"]["basis"] == strategy_basis.BASIS_RECORDED_SNAPSHOT
    assert (
        first["value_basis"]["recorded_in_account_id"] == restatement.parent_account_id
    )
    assert "2026-08-16" in first["value_basis"]["label"]
    assert restatement.account_id in first["value_basis"]["label"]
    assert "value_basis" not in labelled[1]
    # the input rows are not mutated
    assert "value_basis" not in rows[0]


# R2 regression reference values (ledger 058f471a): sleeve gross and whole-account value; both
# are unchanged on the R2.1 lineage (the IBKR NAV rows are equal), only the perimeter moves.
def _cost_row(**override: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "annual_external_cost_usd": "6000",
        "cost_rate_denominator_usd": "77258.13",
        "annual_external_cost_rate_pre_tax": "0.0776594",
    }
    row.update(override)
    return row


def test_the_cost_rate_note_names_both_denominators_for_one_numerator() -> None:
    note = strategy_basis.cost_rate_basis_note(_cost_row(), Decimal("57200.88"))
    assert note is not None
    assert "carried per strategy" in note
    assert "gross sleeve value 77,258.13" in note and "7.7659%" in note
    # the same amount over the account value, computed not typed
    assert "57,200.88 the same amount is 10.4893%" in note


def test_the_cost_rate_note_is_absent_without_a_published_rate() -> None:
    assert (
        strategy_basis.cost_rate_basis_note(
            _cost_row(annual_external_cost_usd=None), None
        )
        is None
    )
    assert (
        strategy_basis.cost_rate_basis_note(
            _cost_row(annual_external_cost_rate_pre_tax=None), None
        )
        is None
    )
    only_sleeve = strategy_basis.cost_rate_basis_note(_cost_row(), None)
    assert only_sleeve is not None and "whole-account value" not in only_sleeve

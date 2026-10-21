"""The adapter turns an account's ledger rows into sleeve inputs, or refuses (#567)."""

from __future__ import annotations

import ast
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

import nested_strategy_tag_map as tag_map_module
import sleeve_inputs as adapter
import sleeve_ledger as sl
import sleeve_nav as sn
import verify_sleeve_ledger as verifier


def _parent(unmatched: dict[str, Any] | None = None) -> Any:
    document = {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-01-04",
        "parents": [
            {
                "parent_account_id": "account-parent",
                "provider_id": "provider-a",
                "tag_field": "orderReference",
                "tag_normalization": "STRIP_LEADING_DIGITS_AND_DASH",
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
                "unmatched": unmatched
                or {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s1",
                },
            }
        ],
    }
    return tag_map_module.validate_tag_map(json.dumps(document).encode()).parent(
        "account-parent"
    )


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _trade(
    day: str, quantity: str, price: str, tag: str | None, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "TRADE:ExchTrade:BUY",
        "event_date": day,
        "security_id": "sec-a",
        "quantity": quantity,
        "execution_price": price,
        "commission": _amount("-1"),
        "executed_time": "09:30:00",
        "amount": _amount("0"),
    }
    if tag is not None:
        row["order_reference"] = tag
    row.update(extra)
    return row


def _rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transactions = [
        _trade("2026-01-01", "10", "10", "11-BookOne", close_price="10"),
        _trade("2026-01-01", "20", "10", "BookTwo", close_price="10"),
        _trade("2026-01-02", "-15", "11", None, close_price="11"),
        {
            "kind": "CASH_ACTIVITY:Dividends",
            "event_date": "2026-01-03",
            "nav_recognition_date": "2026-01-03",
            "security_id": "sec-a",
            "amount": _amount("3"),
        },
        {
            "kind": "CASH_ACTIVITY:Broker Interest Paid",
            "event_date": "2026-01-03",
            "amount": _amount("-1"),
        },
    ]
    positions = [
        {
            "as_of_date": day,
            "security_id": "sec-a",
            "quantity": quantity,
            "mark_price": mark,
            "market_value": _amount("0"),
        }
        for day, quantity, mark in (
            ("2026-01-01", "30", "10"),
            ("2026-01-02", "15", "11"),
            ("2026-01-03", "15", "11"),
        )
    ]
    return transactions, positions


def test_rows_become_inputs_the_ledger_and_the_verifier_agree_on() -> None:
    transactions, positions = _rows()
    inputs = adapter.sleeve_inputs(transactions, positions, _parent())

    assert [t.quantity for t in inputs.trades] == [
        Decimal(10),
        Decimal(20),
        Decimal(-15),
    ]
    assert inputs.trades[0].tag == "11-BookOne" and inputs.trades[2].tag is None
    assert inputs.marks["2026-01-02"] == {"sec-a": Decimal(11)}
    assert [(d.day, d.symbol, d.amount) for d in inputs.dividends] == [
        ("2026-01-03", "sec-a", Decimal(3))
    ]

    policy = sl.SleevePolicy(("s1", "s2"), "other")
    result = sl.derive_sleeve_ledger(
        inputs.trades,
        inputs.positions,
        inputs.marks,
        inputs.dividends,
        inputs.resolve,
        policy,
    )
    days = sorted(inputs.positions)
    zero = Decimal(0)
    nav = sn.derive_sleeve_nav(
        result,
        {"s1": {"2026-01-01": sn.FundingEvent("IN", "DERIVED")}},
        [sn.AccountDay(day, zero, zero, zero, zero, zero, zero) for day in days],
        days,
        ("s1", "s2"),
        "other",
    )

    def plain(row: Any) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(row).items()}

    published = {
        "ledger_rows": [plain(r) for r in result.rows],
        "nav_rows": [plain(r) for r in nav.rows],
        "closure": [plain(r) for r in nav.closure],
    }
    verifier_inputs = inputs.verifier_inputs(
        {"sleeve_ids": ["s1", "s2"], "residual_id": "other"},
        {"s1": {"2026-01-01": {"kind": "IN", "evidence": "DERIVED"}}},
        [
            {
                "day": day,
                "start_nav": "0",
                "end_nav": "0",
                "external_flow": "0",
                "interest": "0",
                "fees": "0",
                "dividend_accrual": "0",
            }
            for day in days
        ],
        days,
    )
    report = verifier.verify_sleeve_ledger(verifier_inputs, published)
    assert report.verified, report.mismatches[:3]


@pytest.mark.parametrize(
    "field, expected",
    [
        ("quantity", "quantity"),
        ("execution_price", "price"),
        ("commission", "commission"),
    ],
)
def test_a_trade_missing_a_figure_is_refused_with_its_count(
    field: str, expected: str
) -> None:
    transactions, positions = _rows()
    del transactions[0][field]
    with pytest.raises(adapter.SleeveInputError, match=expected):
        adapter.sleeve_inputs(transactions, positions, _parent())


def test_a_held_position_without_a_mark_price_is_refused() -> None:
    transactions, positions = _rows()
    del positions[1]["mark_price"]
    with pytest.raises(adapter.SleeveInputError, match="no mark price"):
        adapter.sleeve_inputs(transactions, positions, _parent())


def test_a_flat_position_needs_no_mark_and_a_duplicate_is_refused() -> None:
    transactions, positions = _rows()
    flat = {**positions[1], "quantity": "0"}
    del flat["mark_price"]
    ok = adapter.sleeve_inputs(transactions, [positions[0], flat], _parent())
    assert ok.positions["2026-01-02"] == {"sec-a": Decimal(0)}
    with pytest.raises(adapter.SleeveInputError, match="twice"):
        adapter.sleeve_inputs(transactions, [*positions, positions[0]], _parent())


def test_block_policy_refuses_an_unassigned_trade_and_assign_policy_does_not() -> None:
    transactions, positions = _rows()
    with pytest.raises(adapter.SleeveInputError, match="unassigned"):
        adapter.sleeve_inputs(transactions, positions, _parent({"mode": "BLOCK"}))
    assert adapter.sleeve_inputs(transactions, positions, _parent())


def test_the_adapter_does_not_import_the_verifier_or_the_nav_builder() -> None:
    tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
    roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not roots & {"verify_sleeve_ledger", "sleeve_nav"}

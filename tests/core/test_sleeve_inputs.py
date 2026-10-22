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


_DAYS = ["2026-01-01", "2026-01-02", "2026-01-03"]


def _build(
    transactions: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    parent: Any = None,
    **kwargs: Any,
) -> adapter.SleeveInputs:
    options: dict[str, Any] = {"base_currency": "USD", "days": _DAYS}
    options.update(kwargs)
    return adapter.sleeve_inputs(
        transactions, positions, parent or _parent(), **options
    )


def test_rows_become_inputs_the_ledger_and_the_verifier_agree_on() -> None:
    transactions, positions = _rows()
    inputs = _build(transactions, positions)

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

    policy = inputs.policy("other")
    assert policy.sleeve_ids == ("s1", "s2")
    assert dict(policy.unheld_fallback) == {"sec-a": "s1"}
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
        policy,
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
        _build(transactions, positions)


def test_a_held_position_without_a_mark_price_is_refused() -> None:
    transactions, positions = _rows()
    del positions[1]["mark_price"]
    with pytest.raises(adapter.SleeveInputError, match="no mark price"):
        _build(transactions, positions)


def test_a_flat_position_needs_no_mark_and_a_duplicate_is_refused() -> None:
    transactions, positions = _rows()
    flat = {**positions[1], "quantity": "0"}
    del flat["mark_price"]
    ok = _build(transactions, [positions[0], flat])
    assert ok.positions["2026-01-02"] == {"sec-a": Decimal(0)}
    with pytest.raises(adapter.SleeveInputError, match="twice"):
        _build(transactions, [*positions, positions[0]])


def test_block_policy_refuses_an_unassigned_trade_and_assign_policy_does_not() -> None:
    transactions, positions = _rows()
    with pytest.raises(adapter.SleeveInputError, match="unassigned"):
        _build(transactions, positions, _parent({"mode": "BLOCK"}))
    assert _build(transactions, positions)


def test_a_closing_trade_on_a_day_with_no_open_position_rows_is_an_empty_book() -> None:
    transactions, positions = _rows()
    sell = _trade("2026-01-03", "-15", "11", "BookOne", close_price="11")
    inputs = _build([*transactions, sell], positions[:2])
    assert inputs.positions["2026-01-03"] == {}
    assert inputs.marks["2026-01-03"] == {}
    assert inputs.trades[-1].day == "2026-01-03"


def test_rows_on_an_undeclared_day_are_refused() -> None:
    transactions, positions = _rows()
    with pytest.raises(adapter.SleeveInputError, match="not a declared"):
        _build(transactions, positions, days=_DAYS[:2])
    with pytest.raises(adapter.SleeveInputError, match="no observation days"):
        _build(transactions, positions, days=[])


@pytest.mark.parametrize("where", ["commission", "amount"])
def test_a_foreign_currency_figure_is_refused(where: str) -> None:
    transactions, positions = _rows()
    transactions[0][where] = {
        "currency": "EUR",
        "status": "AVAILABLE",
        "value": "-1",
    }
    with pytest.raises(adapter.SleeveInputError, match="EUR"):
        _build(transactions, positions)


def test_a_foreign_dividend_is_refused() -> None:
    transactions, positions = _rows()
    transactions[3]["amount"] = {"currency": "GBP", "status": "AVAILABLE", "value": "3"}
    with pytest.raises(adapter.SleeveInputError, match="GBP"):
        _build(transactions, positions)


def test_a_contract_multiplier_or_non_share_security_is_refused() -> None:
    transactions, positions = _rows()
    held = [{**positions[0], "multiplier": "100"}, *positions[1:]]
    with pytest.raises(adapter.SleeveInputError, match="cannot use"):
        _build(transactions, held)
    option = [{**p, "asset_category": "OPT"} for p in positions[:0]]
    flat_option = [{**positions[0], "asset_category": "OPT", "quantity": "0"}]
    with pytest.raises(adapter.SleeveInputError, match="cannot use"):
        _build(transactions, [*option, *flat_option, *positions[1:]])
    live = [{**positions[0], "asset_category": "OPT"}, *positions[1:]]
    with pytest.raises(adapter.SleeveInputError, match="plain share"):
        _build([], live)


def test_the_maps_fallback_sleeve_reaches_the_policy_for_unmatched_trades() -> None:
    transactions, positions = _rows()
    document = _parent({"mode": "ASSIGN_BY_HOLDING_SYMBOL", "fallback_sleeve_id": "s2"})
    inputs = _build(transactions, positions, document)
    assert dict(inputs.policy("other").unheld_fallback) == {"sec-a": "s2"}


def test_unassigned_trades_beside_an_untimed_execution_are_refused() -> None:
    transactions, positions = _rows()
    untimed = _trade("2026-01-02", "1", "11", "BookTwo", close_price="11")
    del untimed["executed_time"]
    # the untagged 01-02 sale is unassigned; an untimed execution shares its day
    with pytest.raises(adapter.SleeveInputError, match="no time of day"):
        _build([*transactions, untimed], positions)
    # an untimed execution alone, with every trade assigned, is order-free
    assigned = [
        t for t in transactions if t.get("order_reference") or "TRADE" not in t["kind"]
    ]
    assert _build([*assigned, untimed], positions)


def test_the_adapter_does_not_import_the_verifier_or_the_nav_builder() -> None:
    tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
    roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not roots & {"verify_sleeve_ledger", "sleeve_nav"}

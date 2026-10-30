from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from alpaca_capture_support import (
    ACCOUNT_ID,
    account_body,
    fill,
    nontrade,
    positions_body,
)
from quant_engine.alpaca.capture import NormalizationError
from quant_engine.alpaca.capture.normalize import (
    ACTIVITY_CATEGORIES,
    KNOWN_ACTIVITY_TYPES,
    account_binding_matches,
    normalize_account,
    normalize_activities,
    normalize_activity,
    normalize_positions,
    parse_json_bytes,
    summarize_activities,
)
from quant_engine.alpaca.capture.util import sha256_text

BINDING = sha256_text(ACCOUNT_ID)


def test_each_requested_category_normalizes() -> None:
    rows = [
        fill(1),
        fill(2, side="sell", type="partial_fill", qty="1", price="11"),
        nontrade(3, "DIV", "0.42", symbol="SYNA"),
        nontrade(4, "CSD", "-50.00"),
        nontrade(5, "CSR", "75.00"),
        nontrade(6, "JNLC", "5"),
        nontrade(7, "FEE", "-0.10"),
        nontrade(8, "INT", "0.03"),
    ]
    records = normalize_activities([rows])
    by_type = {r["activity_type"]: r for r in records}
    assert by_type["DIV"]["category"] == "dividend"
    assert by_type["CSD"]["category"] == "transfer"
    assert by_type["FEE"]["category"] == "fee"
    assert by_type["INT"]["category"] == "interest"
    buy = next(r for r in records if r["side"] == "buy")
    sell = next(r for r in records if r["side"] == "sell")
    assert (
        buy["cash_amount"] == "-21.00"
        and buy["cash_amount_basis"] == "derived_qty_x_price"
    )
    assert sell["cash_amount"] == "11" and sell["fill_kind"] == "partial_fill"
    assert buy["occurred_at"] == "2024-01-02T15:00:01.500000Z"
    summary = summarize_activities(records)
    assert summary["activity_count"] == 8
    assert summary["net_cash_amount"] == Decimal("-21.00") + 11 + Decimal(
        "0.42"
    ) - 50 + 75 + 5 - Decimal("0.10") + Decimal("0.03")
    assert summary["by_category"]["fill"]["count"] == 2
    assert (
        summary["first_date"] == "2024-01-02" and summary["last_date"] == "2024-02-01"
    )


def test_every_known_type_has_a_category_and_is_accepted() -> None:
    assert KNOWN_ACTIVITY_TYPES == set(ACTIVITY_CATEGORIES)
    for index, activity_type in enumerate(sorted(KNOWN_ACTIVITY_TYPES - {"FILL"})):
        record = normalize_activity(nontrade(index, activity_type, "1"))
        assert record["category"] == ACTIVITY_CATEGORIES[activity_type]


@pytest.mark.parametrize("activity_type", ["XYZ", "fill", "", "FILL ", "DIVIDEND"])
def test_unknown_activity_types_fail_closed(activity_type: str) -> None:
    with pytest.raises(NormalizationError, match="unknown activity type"):
        normalize_activity(nontrade(1, activity_type, "1"))


def test_missing_or_non_string_activity_type_fails_closed() -> None:
    with pytest.raises(NormalizationError, match="unknown activity type"):
        normalize_activity({"id": "x", "date": "2024-01-01", "net_amount": "1"})
    with pytest.raises(NormalizationError, match="unknown activity type"):
        normalize_activity({"activity_type": 7})


@pytest.mark.parametrize(
    "row",
    [
        {**fill(1), "surprise": "x"},
        {k: v for k, v in fill(1).items() if k != "price"},
        fill(1, price="0"),
        fill(1, qty="-1"),
        fill(1, qty="1e3"),
        fill(1, qty="NaN"),
        fill(1, qty=True),
        fill(1, side="sell_to_cover"),
        fill(1, type="cancelled"),
        fill(1, transaction_time="2024-01-02T15:00:00"),
        fill(1, transaction_time="not a time"),
        fill(1, symbol=""),
        fill(1, id=""),
        nontrade(1, "DIV", "abc"),
        nontrade(1, "DIV", "1", surprise="x"),
        {k: v for k, v in nontrade(1, "DIV", "1").items() if k != "net_amount"},
        nontrade(1, "DIV", "1", date="2024-13-45"),
        "not an object",
    ],
)
def test_malformed_activities_fail_closed(row: Any) -> None:
    with pytest.raises(NormalizationError):
        normalize_activity(row)


def test_security_only_types_may_omit_cash() -> None:
    row = {
        k: v
        for k, v in nontrade(1, "SC", "0", symbol="SYNA").items()
        if k != "net_amount"
    }
    record = normalize_activity(row)
    assert record["cash_amount"] is None
    assert summarize_activities([record])["activities_without_cash_amount"] == 1


def test_duplicate_ids_and_non_array_pages_fail_closed() -> None:
    with pytest.raises(NormalizationError, match="duplicate activity id"):
        normalize_activities([[fill(1)], [fill(1)]])
    with pytest.raises(NormalizationError, match="not an array"):
        normalize_activities([{"message": "oops"}])


def test_output_order_is_deterministic() -> None:
    forward = normalize_activities([[fill(1), fill(2), nontrade(3, "FEE", "-1")]])
    backward = normalize_activities([[nontrade(3, "FEE", "-1"), fill(2), fill(1)]])
    assert forward == backward


def test_json_parsing_rejects_duplicates_nan_and_bad_bytes() -> None:
    with pytest.raises(NormalizationError, match="duplicate key"):
        parse_json_bytes(b'{"a": 1, "a": 2}')
    with pytest.raises(NormalizationError, match="non-finite"):
        parse_json_bytes(b'{"a": NaN}')
    with pytest.raises(NormalizationError, match="not valid"):
        parse_json_bytes(b"\xff\xfe")
    assert parse_json_bytes(b'{"a": 1.50}') == {"a": Decimal("1.50")}


def test_account_snapshot_binds_and_drops_identifiers() -> None:
    raw = parse_json_bytes(account_body())
    snapshot = normalize_account(raw, BINDING)
    assert snapshot["account_binding_sha256"] == BINDING
    assert snapshot["cash"] == "100.25"
    assert ACCOUNT_ID not in str(snapshot) and "SYNTH-NUMBER" not in str(snapshot)
    by_number = parse_json_bytes(
        account_body(id="SYNTH-OTHER", account_number=ACCOUNT_ID)
    )
    assert account_binding_matches(by_number, BINDING)
    other = parse_json_bytes(account_body(id="SYNTH-OTHER", account_number="SYNTH-X"))
    with pytest.raises(NormalizationError, match="configured account"):
        normalize_account(other, BINDING)
    with pytest.raises(NormalizationError):
        normalize_account(parse_json_bytes(account_body(cash=None)), BINDING)


def test_positions_snapshot_sorted_and_strict() -> None:
    rows = normalize_positions(parse_json_bytes(positions_body()))
    assert [r["symbol"] for r in rows] == ["SYNA", "SYNB"]
    with pytest.raises(NormalizationError, match="not an array"):
        normalize_positions({"x": 1})
    with pytest.raises(NormalizationError, match="required"):
        normalize_positions([{"symbol": "SYNA"}])
    duplicate = parse_json_bytes(positions_body())
    duplicate.append(duplicate[0])
    with pytest.raises(NormalizationError, match="duplicate"):
        normalize_positions(duplicate)

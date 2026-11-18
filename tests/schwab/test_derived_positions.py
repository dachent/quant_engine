"""A later Schwab positions export carried back to the cutoff: only when nothing moved."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab.derived_positions import (
    DERIVED_BASIS,
    SchwabDerivedPositionError,
    derive_positions_at_cutoff,
)

ACCOUNT = "account-1825d6880d891d57"
HEADER = (
    '"Symbol","Description","Qty (Quantity)","Prev Close","Cost Basis","Price",'
    '"Price Chng $ (Price Change $)","Price Chng % (Price Change %)",'
    '"Mkt Val (Market Value)","Day Chng $ (Day Change $)","Day Chng % (Day Change %)",'
    '"Gain $ (Gain/Loss $)","Gain % (Gain/Loss %)","Ratings","Reinvest?",'
    '"Reinvest Capital Gains?","% of Acct (% of Account)","Cost/Share","Last Tr (ET)",'
    '"Exchange","Asset Type",'
)


def _export(
    *,
    qty: str = "1,000.00",
    price: str = "1.00",
    value: str = "$1,000.00",
    cash: str = "$5.00",
    total: str = "$1,005.00",
    asset: str = "Cash and Money Market",
) -> str:
    return "\n".join(
        [
            '"Positions for account Joint Tenant ...691 as of 08:20 PM ET, 2026/11/17"',
            "",
            HEADER,
            f'"SWVXX","SCHWAB PRIME ADVANTAGE MONEY INVESTOR","{qty}","1.00","$1,000.00","{price}","0","0%","{value}","$0.00","0%","$0.00","0%","-","Yes","Yes","99%","$1.00","6:45:00 PM","Mutual Fund","{asset}",',
            f'"Cash & Cash Investments","--","--","--","--","--","--","--","{cash}","$0.00","0%","--","--","--","--","--","1%","--","--","--","Cash and Money Market",',
            f'"Positions Total","","--","--","$1,000.00","--","--","--","{total}","$0.00","0%","$0.00","0%","--","--","--","--","--","--","--","--",',
            "",
        ]
    )


def _run(**over: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "account_id": ACCOUNT,
        "export_text": _export(),
        "cutoff": date(2026, 11, 6),
        "capture_date": date(2026, 11, 17),
        "history_transactions": [
            {"account_id": ACCOUNT, "event_date": "2026-10-30"},
            {"account_id": "account-other", "event_date": "2026-11-10"},
        ],
        "history_through": date(2026, 11, 14),
        "earlier_market_value": {"2026-11-14": Decimal("1000.00")},
        "balances_account_value": Decimal("1005.00"),
    }
    args.update(over)
    return derive_positions_at_cutoff(**args)


def test_a_closed_unmoved_money_market_snapshot_is_carried_to_the_cutoff() -> None:
    (row,) = _run()
    assert row["symbol"] == "SWVXX" and row["quantity"] == "1000.00"
    assert row["as_of_date"] == "2026-11-06" and row["capture_date"] == "2026-11-17"
    assert row["as_of_basis"] == row["evidence_class"] == DERIVED_BASIS
    assert row["market_value"]["value"] == "1000.00"
    assert row["closure"]["cash_usd"] == "5.00"
    assert row["closure"]["positions_total_usd"] == "1005.00"
    assert row["security_id"].startswith("security-schwab-")
    assert row["mapping_status"] == "UNMAPPED" and row["strategy_id"] is None


@pytest.mark.parametrize(
    "over,fragment",
    [
        ({"export_text": _export(total="$1,006.00")}, "does not close"),
        ({"balances_account_value": Decimal("1006.00")}, "balances account value"),
        (
            {
                "history_transactions": [
                    {"account_id": ACCOUNT, "event_date": "2026-11-09"}
                ]
            },
            "after the cutoff",
        ),
        ({"earlier_market_value": {"2026-11-14": Decimal("999.00")}}, "holdings moved"),
        ({"earlier_market_value": {}}, "independent observation"),
        (
            {
                "export_text": _export(
                    price="1.01", value="$1,010.00", total="$1,015.00"
                ),
                "balances_account_value": None,
                "earlier_market_value": {"2026-11-14": Decimal("1010.00")},
            },
            "moves",
        ),
        ({"export_text": _export(asset="Equity")}, "moves"),
        (
            {
                "export_text": _export(qty="999.00", value="$1,000.00"),
            },
            "quantity differs",
        ),
        ({"history_through": date(2026, 11, 5)}, "reach the cutoff"),
        ({"export_text": "no header here"}, "header is absent"),
    ],
)
def test_a_snapshot_that_is_not_the_cutoff_refuses(
    over: dict[str, Any], fragment: str
) -> None:
    """KILL: each way the later snapshot could differ from the cutoff refuses."""

    with pytest.raises(SchwabDerivedPositionError, match=fragment):
        _run(**over)


REAL = os.environ.get("QE_SCHWAB_691_EXPORT")


@pytest.mark.skipif(not REAL, reason="QE_SCHWAB_691_EXPORT not set (real bytes)")
def test_the_real_export_parses_and_closes() -> None:
    text = Path(str(REAL)).read_text(encoding="utf-8-sig")
    rows = derive_positions_at_cutoff(
        account_id=ACCOUNT,
        export_text=text,
        cutoff=date(2026, 11, 6),
        capture_date=date(2026, 11, 17),
        history_transactions=[],
        history_through=date(2026, 11, 17),
        earlier_market_value={},
    )
    assert [r["symbol"] for r in rows] == ["SWVXX"]
    assert rows[0]["market_value"]["value"] == rows[0]["quantity"]


def test_balances_page_values_are_parsed() -> None:
    from quant_engine.schwab.derived_positions import parse_balances_values

    page = (
        "Account Details\nAccount Value\n$1,005.00\n \nDay Change\n$0.00\n"
        "Cash & Cash Investments\n$5.00\nMarket Value\n$1,000.00\n"
    )
    assert parse_balances_values(page) == (Decimal("1005.00"), Decimal("1000.00"))
    with pytest.raises(SchwabDerivedPositionError):
        parse_balances_values("Account Value\n$1.00\n")


def test_balances_labels_are_read_by_line_layout_not_first_substring() -> None:
    """Pins the first-occurrence rule on the live page layout: a label line is a line that IS the
    label, its amount is the first '$' line within the next three lines; a label that appears earlier
    without such an amount (a table header) is passed over, one with an amount wins."""

    from quant_engine.schwab.derived_positions import (
        SchwabDerivedPositionError,
        parse_balances_values,
    )

    header_first = "\n".join(
        [
            "Positions table",
            "Market Value",  # a column header: no '$' amount within three lines
            "Symbol",
            "Qty",
            "Price",
            "Account Value",
            "$3.81",
            " ",
            "Day Change",
            "$0.00",
            "Cash & Cash Investments",
            "$3.81",
            "Market Value",
            "$0.00",
        ]
    )
    assert parse_balances_values(header_first) == (Decimal("3.81"), Decimal("0.00"))
    # the first label line that has an amount wins (the rule: first occurrence with an amount)
    twice = "Account Value\n$10.00\nMarket Value\n$1.00\nAccount Value\n$99.00\nMarket Value\n$98.00\n"
    assert parse_balances_values(twice) == (Decimal("10.00"), Decimal("1.00"))
    # an amount further than three lines away is not read
    far = "Account Value\nx\ny\nz\n$5.00\nMarket Value\n$1.00\n"
    with pytest.raises(SchwabDerivedPositionError):
        parse_balances_values(far)
    # a label embedded in a longer line is not a label line
    embedded = "Total Account Value History\n$5.00\nMarket Value\n$1.00\n"
    with pytest.raises(SchwabDerivedPositionError):
        parse_balances_values(embedded)

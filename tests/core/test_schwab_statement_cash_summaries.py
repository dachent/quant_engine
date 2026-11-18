"""statement_cash_summaries rows: printed columns, per-column ties, named gaps."""

from __future__ import annotations

from typing import Any

import pytest

import schwab_statement_cash_summaries as scs

HEAD = "\n".join(
    ["Statement Period", "January 1-31, 2026", "Account Number", "2525-5620"]
)
BODY = "\n".join(
    [
        "Transactions - Summary",
        "Beginning Cash* as of 01/01",
        "+ Deposits",
        "+ Expenses = Ending Cash* as of 01/31",
        "$125.62 $7,000.00 $0.00 ($103.73) $22,527.58 $27.97 $0.00 $29,577.44",
        "Transaction Details",
        "Date Category Action",
        "Interest($) Amount($)",
        "01/02 Deposit Journaled Funds IRA REGULAR CONTRIB 7,000.00",
        "01/05 Sale TIP ISHARES TIPS BOND ETF (19.0000) 110.0000 2,090.00 (5.73)",
        "Sale SWVXX SCHWAB VALUE ADVANTAGE (20,437.5800) 1.0000 20,437.58",
        "Purchase SCHW CHARLES SCHWAB CORP 1.0000 103.7300 (103.73)",
        "01/07 Dividend Cash Dividend EPOL ISHARES MSCI POLAND ETF 27.97",
        "Total Transactions $0.00",
    ]
)
RAW = HEAD + "\n" + BODY
MAPPING = {"620": "account-c63d"}


def _stmt(raw: str, name: str = "a.pdf") -> dict[str, Any]:
    return {"raw_text": raw, "source_artifact_id": name, "source_sha256": "0" * 64}


def test_a_tied_statement_has_every_printed_column_and_tie_status_tied() -> None:
    (row,) = scs.build_rows([_stmt(RAW)], MAPPING)
    assert row["account_id"] == "account-c63d"
    assert (row["period_start"], row["period_end"]) == ("2026-01-01", "2026-01-31")
    assert row["deposits"]["value"] == "7000.00"
    assert row["sales_redemptions"]["value"] == "22527.58"
    assert row["ending_cash"]["value"] == "29577.44"
    assert row["tie_status"] == "TIED"
    assert row["deposits_tied"] and row["withdrawals_tied"]


def test_a_failing_sales_column_leaves_the_flow_columns_tied_but_the_month_untied() -> (
    None
):
    # the Sales rows no longer add to the printed column; deposits and withdrawals still do
    raw = RAW.replace("20,437.5800) 1.0000 20,437.58", "20,436.5800) 1.0000 20,436.58")
    (row,) = scs.build_rows([_stmt(raw)], MAPPING)
    assert row["tie_status"] == "CASH_CATEGORY_TOTALS_DIFFER"
    assert row["deposits_tied"] and row["withdrawals_tied"]


def test_a_deposit_column_that_does_not_tie_is_flagged_and_the_month_is_untied() -> (
    None
):
    raw = RAW.replace("CONTRIB 7,000.00", "CONTRIB 6,999.00")
    (row,) = scs.build_rows([_stmt(raw)], MAPPING)
    assert not row["deposits_tied"] and row["withdrawals_tied"]
    assert row["tie_status"] == "CASH_CATEGORY_TOTALS_DIFFER"


def test_a_statement_with_no_cash_summary_is_emitted_with_its_reason() -> None:
    (row,) = scs.build_rows([_stmt(HEAD + "\nno summary page here")], MAPPING)
    assert row["tie_status"] == "CASH_SUMMARY_ABSENT"
    assert "deposits" not in row and not row["deposits_tied"]


def test_unmapped_account_duplicates_and_unreadable_headers_refuse() -> None:
    with pytest.raises(scs.StatementSummaryError, match="not mapped"):
        scs.build_rows([_stmt(RAW)], {"999": "x"})
    with pytest.raises(scs.StatementSummaryError, match="two statements"):
        scs.build_rows([_stmt(RAW, "a.pdf"), _stmt(RAW, "b.pdf")], MAPPING)
    with pytest.raises(scs.StatementSummaryError, match="period"):
        scs.build_rows([_stmt(BODY)], MAPPING)

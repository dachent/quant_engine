"""statement_cash_summaries rows: printed columns, per-column ties, named gaps."""

from __future__ import annotations

from typing import Any

import pytest

import schwab_statement_cash_summaries as scs

HEAD = "\n".join(
    ["Statement Period", "January 1-31, 2026", "Account Number", "0000-0620"]
)
BODY = "\n".join(
    [
        "Transactions - Summary",
        "Beginning Cash* as of 01/01",
        "+ Deposits",
        "+ Expenses = Ending Cash* as of 01/31",
        "$100.00 $5,000.00 $0.00 ($100.00) $22,000.00 $25.00 $0.00 $27,025.00",
        "Transaction Details",
        "Date Category Action",
        "Interest($) Amount($)",
        "01/02 Deposit Journaled Funds IRA REGULAR CONTRIB 5,000.00",
        "01/05 Sale TIPX SYNTHETIC BOND ETF (20.0000) 100.0000 2,000.00 (5.00)",
        "Sale SWVXX SCHWAB VALUE ADVANTAGE (20,000.0000) 1.0000 20,000.00",
        "Purchase SCHW CHARLES SCHWAB CORP 1.0000 100.0000 (100.00)",
        "01/07 Dividend Cash Dividend ZZZZ SYNTHETIC EXAMPLE ETF 25.00",
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
    assert row["deposits"]["value"] == "5000.00"
    assert row["sales_redemptions"]["value"] == "22000.00"
    assert row["ending_cash"]["value"] == "27025.00"
    assert row["tie_status"] == "TIED"
    assert row["deposits_tied"] and row["withdrawals_tied"]


def test_a_failing_sales_column_leaves_the_flow_columns_tied_but_the_month_untied() -> (
    None
):
    # the Sales rows no longer add to the printed column; deposits and withdrawals still do
    raw = RAW.replace("20,000.0000) 1.0000 20,000.00", "19,999.0000) 1.0000 19,999.00")
    (row,) = scs.build_rows([_stmt(raw)], MAPPING)
    assert row["tie_status"] == "CASH_CATEGORY_TOTALS_DIFFER"
    assert row["deposits_tied"] and row["withdrawals_tied"]


def test_a_deposit_column_that_does_not_tie_is_flagged_and_the_month_is_untied() -> (
    None
):
    raw = RAW.replace("CONTRIB 5,000.00", "CONTRIB 4,999.00")
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
        scs.build_rows([_stmt("Account Number\n0000-0620\n" + BODY)], MAPPING)


LEGACY = "\n".join(
    [
        "Account Number",
        "0000-0620",
        "Statement Period",
        "December 1-31, 2024",
        "Cash Transactions Summary This Period Year to Date",
        "Starting Cash* $ 0.00 $ 0.00",
        "Deposits and other Cash Credits 0.00 20,000.00",
        "Investments Sold 0.00 0.00",
        "Dividends and Interest 1,000.00 2,000.00",
        "Withdrawals and other Debits 0.00 0.00",
        "Investments Purchased (1,000.00) (22,000.00)",
        "Fees and Charges 0.00 0.00",
        "Total Cash Transaction Detail 0.00 0.00",
        "Ending Cash* $ 0.00 $ 0.00",
    ]
)


def test_the_old_pcra_layout_is_read_when_it_closes_and_named_when_it_does_not() -> (
    None
):
    (row,) = scs.build_rows([_stmt(LEGACY)], MAPPING)
    assert row["tie_status"] == "LEGACY_LAYOUT_CLOSES"
    assert row["dividends_interest"]["value"] == "1000.00"
    assert row["purchases"]["value"] == "-1000.00"
    assert row["deposits"]["value"] == "0.00" and not row["deposits_tied"]
    broken = LEGACY.replace("Ending Cash* $ 0.00", "Ending Cash* $ 5.00", 1)
    (gap,) = scs.build_rows([_stmt(broken)], MAPPING)
    assert gap["tie_status"] == "CASH_SUMMARY_ABSENT" and "deposits" not in gap

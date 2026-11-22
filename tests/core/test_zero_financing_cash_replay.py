"""Cash replay after the last statement: an account whose capture bracket cannot be proven by
silence (a buy in the window, or a borrowing signal on a retirement page) is observed from the
last statement's printed ending cash plus the captured Transaction History, closed to the live
balance to the cent.  A debit day is a financing day at its exact settled balance; anything
that does not close, or is not covered by the rule, stays unobserved."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import zero_financing_statements as zf

LAST3 = "123"
ACCOUNT = "account-" + "a" * 16
PROVIDER = "schwab"
LABEL = zf.statement_label(PROVIDER, LAST3)
CUTOFF = date(2026, 4, 6)
SUMMARY = "        $100.00   $0.00   $0.00   $0.00   $0.00   $0.00   $100.00"
RAW_MARCH = "\n".join(
    [
        "Transactions - Summary",
        "$100.00 $0.00 $0.00 $0.00 $0.00 $0.00 $0.00 $100.00",
    ]
)
DEBIT_PRECEDENT = (
    "   Settled Debit Balance Amount\n"
    "   $0.00 based on 0.00% margin interest rate charged on daily margin balances. "
    "No margin interest was charged. This equates to 0.00% annual interest rate.\n"
)


def _statement(
    period: str, *, margin: bool, extra: str = "", loan_closing: str = "0.00"
) -> str:
    lines = [
        ("Schwab One Account of" if margin else "Roth Contributory IRA of")
        + "   Account Number   Statement Period",
        f"   ****-*{LAST3}   {period}",
        "Transactions - Summary",
        "",
        "  words",
        SUMMARY,
    ]
    if margin:
        lines += [
            "   Margin Loan Information",
            "   Opening Margin      Closing Margin      Funds Available     Buying Power",
            "   Loan Balance        Loan Balance",
            "",
            f"   $0.00               ${loan_closing}               $10.00              $20.00",
        ]
    if extra:
        lines.append(extra)
    return "\f".join(["\n".join([*lines, "1 of 2"]), "p2\n2 of 2"])


def _us(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def trade(day: str, settle: str, amount: str, action: str = "Buy") -> dict[str, str]:
    return {
        "date": day,
        "settle": settle,
        "action": action,
        "description": "SPY",
        "amount": amount,
    }


def other(day: str, action: str, amount: str, description: str = "x") -> dict[str, str]:
    return {"date": day, "action": action, "description": description, "amount": amount}


def _capture(
    directory: Path,
    rows: list[dict[str, str]],
    *,
    live_cash: str,
    retirement_loan: float = 0.0,
    trade_date_balance: float = 0.0,
    bookmark: str | None = None,
) -> bytes:
    """A capture whose raw history carries settle dates; its claims equal its raw files."""

    history = {
        "brokerageTransactions": [
            {
                "transactionDate": _us(r["date"]),
                "action": r["action"],
                "description": r["description"],
                "symbol": "SPY",
                "amount": r["amount"],
                **(
                    {"transactionDetail": {"settleDate": _us(r["settle"])}}
                    if "settle" in r
                    else {}
                ),
            }
            for r in rows
        ]
        + [
            {
                "transactionDate": "02/20/2026",
                "action": "Buy",
                "description": "old",
                "amount": "-1.00",
            }
        ],
        "fromDate": "02/01/2026",
        "toDate": "04/08/2026",
        "bookmark": bookmark,
    }
    stamp = "Updated:\n3:14:06 AM Eastern Time, X\n03:14:06 AM ET, 04/08/2026\n"
    stamp += f"Account Selector: Joint Account ending in {' '.join(LAST3)} Selected\n"
    credit = max(float(live_cash), 0.0)
    balances = {
        "brokerageAccountList": [
            {
                "accountId": f"****{LAST3}",
                "cashInvestments": {"marginBalance": credit},
                "marginsInfo": {
                    "balanceSubjectInterest": retirement_loan,
                    "tradeDateBalance": trade_date_balance,
                },
            }
        ]
    }
    page = (
        stamp
        + "Margin Details\nBalance Subject to Interest   $"
        + f"{retirement_loan:.2f}\n"
        + f"Cash & Cash Investments Total\n\t\n${live_cash}\n"
    )
    files = {
        "history.json": (json.dumps(history).encode(), "history"),
        "balances.json": (json.dumps(balances).encode(), "balances"),
        "balances-page.txt": (page.encode(), "balances"),
        "history-page.txt": (stamp.encode(), "history"),
    }
    for name, (content, _kind) in files.items():
        (directory / name).write_bytes(content)
    claimed = [
        {k: v for k, v in r.items() if k != "settle"} | {"symbol": "SPY"} for r in rows
    ]
    body: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": PROVIDER,
        "account_last3": LAST3,
        "captured_at_utc": "2026-04-08T12:00:00Z",
        "capture_date": "2026-04-08",
        "window_start": "2026-03-20",
        "window_end": "2026-04-08",
        "balances": {
            "margin_loan_balance": f"{retirement_loan:.2f}",
            "balance_subject_to_interest": f"{retirement_loan:.2f}",
        },
        "transactions_reported_count": len(rows),
        "transactions": claimed,
        "transactions_complete": True,
        "raw_files": [
            {
                "name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "kind": kind,
            }
            for name, (content, kind) in files.items()
        ],
    }
    return json.dumps(body).encode()


def _build(
    tmp_path: Path,
    *,
    margin: bool,
    raw: bytes,
    statement_raw: str = RAW_MARCH,
    precedent_text: str | None = DEBIT_PRECEDENT,
    march_extra: str = "",
    loan_closing: str = "0.00",
) -> dict[str, Any]:
    texts = {
        f"schwab-stmt-{LABEL}-2026-03-31.pdf": _statement(
            "March 1-31, 2026",
            margin=margin,
            extra=march_extra,
            loan_closing=loan_closing,
        )
    }
    raws = {f"schwab-stmt-{LABEL}-2026-03-31.pdf": statement_raw}
    if precedent_text is not None and not margin:
        name = f"schwab-stmt-{LABEL}-2026-02-28.pdf"
        texts[name] = _statement(
            "February 1-28, 2026", margin=False, extra=precedent_text
        )
        raws[name] = "Transactions - Summary"
    sums = {}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256(name.encode()).hexdigest()
    return zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda path: texts[path.name],
        extract_raw=lambda path: raws[path.name],
        captures={ACCOUNT: raw},
        capture_dir=tmp_path,
        cutoff=CUTOFF,
    )


def _rows(artifact: dict[str, Any], first: str = "2026-03-01") -> dict[str, Any]:
    return {r["as_of_date"]: r for r in artifact["rows"] if r["as_of_date"] >= first}


def test_a_funded_buy_on_a_margin_account_is_zero_financing_by_replay(
    tmp_path: Path,
) -> None:
    """A buy funded by cash that settled before it: never a debit on either basis."""

    rows = [
        other("2026-04-01", "Sell", "100.00") | {"settle": "2026-04-02"},
        trade("2026-04-03", "2026-04-06", "-120.00"),
    ]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="80.00")
    )
    days = _rows(artifact)
    assert min(days) == "2026-03-01" and max(days) == "2026-04-06" and len(days) == 37
    assert {r["classification"] for r in days.values()} == {zf.CLASSIFICATION}
    assert {r["verdict_reason"] for r in days.values()} == {zf.REPLAY_VERDICT}
    assert artifact["unobserved_periods"] == []
    note = artifact["capture_decisions"][0]
    assert note["bracket"] is False
    assert note["reason"] == "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    assert note["cash_replay"]["closes"] is True
    assert (
        note["cash_replay"]["zero_days"],
        note["cash_replay"]["financing_days"],
    ) == (
        37,
        0,
    )
    zf.validate_rows(artifact, {ACCOUNT})


def test_a_margin_trade_date_shortfall_is_left_unobserved(tmp_path: Path) -> None:
    """Settled cash stays at or above zero but the trade-date cash is below zero from the
    buy to its settlement: a margin account's day is not published as zero."""

    rows = [
        trade("2026-04-02", "2026-04-07", "-150.00"),
        other("2026-04-07", "Sell", "100.00") | {"settle": "2026-04-08"},
    ]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="50.00")
    )
    days = _rows(artifact)
    assert max(days) == "2026-04-01"  # 04-02..04-06 carry no row
    assert artifact["unobserved_periods"] == [
        {
            "account_id": ACCOUNT,
            "period_start": "2026-04-02",
            "period_end": "2026-04-06",
            "state": "UNVERIFIED",
            "reason": "TRADE_DATE_CASH_BELOW_ZERO",
        }
    ]
    assert artifact["capture_decisions"][0]["cash_replay"]["unobserved_days"] == 5


def test_a_retirement_settled_debit_is_a_financing_day_at_its_exact_balance(
    tmp_path: Path,
) -> None:
    """The money-due case: the debit starts on the settle date, not the trade date, and its
    balance is the exact settled cash.  The days before it are zero days."""

    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    artifact = _build(
        tmp_path,
        margin=False,
        raw=_capture(tmp_path, rows, live_cash="-50.00", retirement_loan=50.0),
    )
    days = _rows(artifact, "2026-04-01")
    assert sorted(days) == [f"2026-04-0{d}" for d in range(1, 7)]
    assert [days[d]["classification"] for d in sorted(days)] == [
        zf.CLASSIFICATION,
        zf.CLASSIFICATION,
        *[zf.FINANCING_CLASSIFICATION] * 4,
    ]
    for day in ("2026-04-03", "2026-04-06"):
        assert days[day]["amount"]["value"] == "50.00"
        assert days[day]["amount_measurement"] == zf.DAY_BALANCE_DERIVED
        assert days[day]["verdict_reason"] == zf.REPLAY_VERDICT
    note = artifact["capture_decisions"][0]
    assert note["reason"] == "RETIREMENT_ACCOUNT_SHOWS_BORROWING"
    assert note["cash_replay"]["financing_days"] == 4
    assert note["cash_replay"]["retirement_debit_precedent_statements"] == 1
    zf.validate_rows(artifact, {ACCOUNT})


def test_a_retirement_debit_needs_the_zero_interest_statement_precedent(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    artifact = _build(
        tmp_path,
        margin=False,
        raw=_capture(tmp_path, rows, live_cash="-50.00", retirement_loan=50.0),
        precedent_text=None,
    )
    days = _rows(artifact, "2026-04-01")
    assert sorted(days) == ["2026-04-01", "2026-04-02"]
    assert artifact["unobserved_periods"][0]["reason"] == (
        "RETIREMENT_DEBIT_PRECEDENT_ABSENT"
    )
    assert artifact["unobserved_periods"][0]["period_start"] == "2026-04-03"


def test_the_precedent_counts_only_retirement_statements_with_both_phrases(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    raw = _capture(tmp_path, rows, live_cash="-50.00", retirement_loan=50.0)

    def counted(**kw: Any) -> int:
        artifact = _build(tmp_path, margin=False, raw=raw, **kw)
        return int(
            artifact["capture_decisions"][0]["cash_replay"][
                "retirement_debit_precedent_statements"
            ]
        )

    both = DEBIT_PRECEDENT
    assert counted(precedent_text=both) == 1
    # a different annual rate, or no debit statement, is not a precedent
    assert counted(precedent_text=both.replace("0.00% annual", "5.00% annual")) == 0
    assert counted(precedent_text=both.replace("Settled Debit Balance", "Cash")) == 0
    # a statement that also posted margin interest is not a precedent either
    posted = (
        both + "03/05 Expense     Margin Interest           INTEREST 02/04THRU 03/04\n"
    )
    assert counted(precedent_text=posted) == 0
    # the account's own March statement counts too when it carries both phrases
    assert counted(precedent_text=None, march_extra=both) == 1


def test_a_margin_statements_interest_text_is_not_a_retirement_precedent(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-03", "2026-04-06", "-40.00")]
    artifact = _build(
        tmp_path,
        margin=True,
        raw=_capture(tmp_path, rows, live_cash="60.00"),
        march_extra=DEBIT_PRECEDENT,
    )
    assert (
        artifact["capture_decisions"][0]["cash_replay"][
            "retirement_debit_precedent_statements"
        ]
        == 0
    )


def test_a_replay_that_does_not_close_leaves_the_days_unobserved(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-03", "2026-04-06", "-40.00")]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="60.01")
    )
    assert artifact["rows"] == []
    assert artifact["unobserved_periods"][0]["reason"] == "NEXT_STATEMENT_NOT_PINNED"
    assert artifact["capture_decisions"][0]["cash_replay"] == {
        "closes": False,
        "reason": "CAPTURE_CASH_REPLAY_DOES_NOT_CLOSE",
    }


def test_margin_interest_or_a_loan_row_after_the_statement_refuses_the_replay(
    tmp_path: Path,
) -> None:
    rows = [
        trade("2026-04-03", "2026-04-06", "-40.00"),
        other("2026-04-02", "Expense", "-0.50", "Margin Interest INTEREST"),
    ]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="59.50")
    )
    assert artifact["rows"] == []
    assert artifact["capture_decisions"][0]["cash_replay"] == {
        "closes": False,
        "reason": "CAPTURE_SHOWS_FINANCING_ROW",
    }


def test_a_statement_without_readable_ending_cash_stays_closed(tmp_path: Path) -> None:
    rows = [trade("2026-04-03", "2026-04-06", "-40.00")]
    artifact = _build(
        tmp_path,
        margin=True,
        raw=_capture(tmp_path, rows, live_cash="60.00"),
        statement_raw="no cash summary here",
    )
    assert artifact["rows"] == []
    assert artifact["capture_decisions"][0]["cash_replay"] == {
        "closes": False,
        "reason": "LAST_STATEMENT_CASH_UNAVAILABLE",
    }


def test_an_unsettled_trade_before_the_statement_end_is_part_of_the_replay(
    tmp_path: Path,
) -> None:
    """A buy traded on the last statement day settles after it: the replay must carry it,
    or the live balance would not close."""

    rows = [trade("2026-03-31", "2026-04-02", "-30.00")]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="70.00")
    )
    assert artifact["capture_decisions"][0]["cash_replay"]["closes"] is True
    assert len(_rows(artifact)) == 37


RAW_NEGATIVE_MARCH = "\n".join(
    [
        "Transactions - Summary",
        "Beginning Cash* as of 03/01",
        "+ Deposits",
        "+ Expenses = Ending Cash* as of 03/31",
        "$100.00 $0.00 ($300.00) $0.00 $250.00 $0.00 $0.00 $50.00",
        "Transaction Details",
        "Date Category Action",
        "Interest($) Amount($)",
        "03/03 Withdrawal Wire Sent X",
        "WIRED FUNDS",
        "(300.00)",
        "03/05 Sale ABC SOME DESCRIPTION",
        "(10.0000) 25.0000 250.00 (3.00),(ST)",
        "Industry Fee $0.01",
        "Total Transactions $0.00",
    ]
)


def test_a_margin_statement_whose_own_cash_went_below_zero_stays_closed(
    tmp_path: Path,
) -> None:
    """The statement's daily cash (here -200 on 03-03) is part of the final period: a
    margin account is re-admitted only when it never dipped below zero."""

    rows = [trade("2026-04-03", "2026-04-06", "-40.00")]
    artifact = _build(
        tmp_path,
        margin=True,
        raw=_capture(tmp_path, rows, live_cash="10.00"),
        statement_raw=RAW_NEGATIVE_MARCH,
    )
    assert artifact["rows"] == []
    assert artifact["capture_decisions"][0]["cash_replay"] == {
        "closes": False,
        "reason": "LAST_STATEMENT_CASH_NEGATIVE",
    }


def test_a_margin_statement_with_a_closing_loan_is_never_replayed(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-03", "2026-04-06", "-40.00")]
    artifact = _build(
        tmp_path,
        margin=True,
        raw=_capture(tmp_path, rows, live_cash="60.00"),
        loan_closing="200.00",
    )
    assert artifact["rows"] == []
    note = artifact["capture_decisions"][0]
    assert note["reason"] == "LAST_STATEMENT_NOT_ZERO"
    assert "cash_replay" not in note


def test_an_unsettled_pre_statement_buy_counts_on_the_trade_date_basis(
    tmp_path: Path,
) -> None:
    """A buy traded on the statement's last day and settling later lowers the trade-date
    cash from the next day: those days are unobserved on a margin account, and the
    settled debit that follows is a financing day at its exact balance."""

    rows = [
        trade("2026-03-31", "2026-04-03", "-150.00"),
        trade("2026-04-03", "2026-04-06", "100.00", action="Sell"),
    ]
    artifact = _build(
        tmp_path, margin=True, raw=_capture(tmp_path, rows, live_cash="50.00")
    )
    days = _rows(artifact, "2026-04-01")
    assert sorted(days) == ["2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06"]
    assert days["2026-04-03"]["classification"] == zf.FINANCING_CLASSIFICATION
    assert days["2026-04-03"]["amount"]["value"] == "50.00"
    assert days["2026-04-06"]["classification"] == zf.CLASSIFICATION
    assert artifact["unobserved_periods"] == [
        {
            "account_id": ACCOUNT,
            "period_start": "2026-04-01",
            "period_end": "2026-04-02",
            "state": "UNVERIFIED",
            "reason": "TRADE_DATE_CASH_BELOW_ZERO",
        }
    ]


def _retirement_through_cutoff(
    tmp_path: Path, rows: list[dict[str, str]], live_cash: str, **kw: Any
) -> dict[str, Any]:
    """The capture bracket holds for a retirement type (no borrowing on the raw loan fields,
    as on a page that shows money due but a zero balance subject to interest)."""

    return _build(
        tmp_path,
        margin=False,
        raw=_capture(
            tmp_path,
            rows,
            live_cash=live_cash,
            trade_date_balance=float(live_cash) if float(live_cash) < 0 else 0.0,
        ),
        **kw,
    )


def test_a_retirement_zero_day_through_the_cutoff_becomes_a_financing_day_on_a_debit(
    tmp_path: Path,
) -> None:
    """The money-due case on an account whose bracket holds: the zero days before the debit
    keep their rows, the settled-debit days are replaced by financing days at 0%."""

    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    artifact = _retirement_through_cutoff(tmp_path, rows, "-50.00")
    days = _rows(artifact, "2026-04-01")
    assert [days[d]["classification"] for d in sorted(days)] == [
        zf.CLASSIFICATION,
        zf.CLASSIFICATION,
        *[zf.FINANCING_CLASSIFICATION] * 4,
    ]
    assert days["2026-04-02"]["verdict_reason"] == "RETIREMENT_TYPE_THROUGH_CUTOFF"
    assert days["2026-04-03"]["verdict_reason"] == zf.REPLAY_VERDICT
    assert days["2026-04-05"]["amount"]["value"] == "50.00"
    note = artifact["capture_decisions"][0]
    assert (note["bracket"], note["reason"]) == (True, "RETIREMENT_TYPE_THROUGH_CUTOFF")
    assert note["cash_replay"]["financing_days"] == 4
    assert len(artifact["rows"]) == len({r["as_of_date"] for r in artifact["rows"]})
    zf.validate_rows(artifact, {ACCOUNT})


def test_a_retirement_account_without_a_debit_keeps_every_row_byte_identical(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-02", "2026-04-03", "-40.00")]
    with_replay = _retirement_through_cutoff(tmp_path, rows, "60.00")
    assert with_replay["capture_decisions"][0]["cash_replay"]["financing_days"] == 0
    # the same capture with the replay unavailable (no printed statement cash)
    without = _retirement_through_cutoff(
        tmp_path, rows, "60.00", statement_raw="no cash summary here"
    )
    assert without["capture_decisions"][0]["cash_replay"]["closes"] is False
    assert with_replay["rows"] == without["rows"]
    assert {
        r["verdict_reason"]
        for r in with_replay["rows"]
        if r["as_of_date"] > "2026-03-31"
    } == {"RETIREMENT_TYPE_THROUGH_CUTOFF"}


def test_a_retirement_debit_without_the_precedent_drops_its_zero_rows(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    artifact = _retirement_through_cutoff(tmp_path, rows, "-50.00", precedent_text=None)
    assert sorted(_rows(artifact, "2026-04-01")) == ["2026-04-01", "2026-04-02"]
    assert artifact["unobserved_periods"][0]["reason"] == (
        "RETIREMENT_DEBIT_PRECEDENT_ABSENT"
    )
    assert artifact["unobserved_periods"][0]["period_start"] == "2026-04-03"


def test_a_retirement_replay_that_does_not_close_leaves_the_published_zero_rows(
    tmp_path: Path,
) -> None:
    rows = [trade("2026-04-02", "2026-04-03", "-150.00")]
    artifact = _retirement_through_cutoff(tmp_path, rows, "-50.01")
    assert artifact["capture_decisions"][0]["cash_replay"] == {
        "closes": False,
        "reason": "CAPTURE_CASH_REPLAY_DOES_NOT_CLOSE",
    }
    assert {r["classification"] for r in artifact["rows"]} == {zf.CLASSIFICATION}

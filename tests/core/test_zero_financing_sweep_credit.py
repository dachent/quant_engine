"""A capture taken in a trading session on a retirement account: the live cash total is the
margin-balance field plus the bank-sweep credit it does not carry, and a positive trade-date
balance equal to the cash total is a credit.  Real borrowing signals still refuse, and the cash
replay closes to the live total on the settled basis or on the trade-date basis."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import zero_financing_statements as zf

LAST3 = "123"
CUTOFF = date(2026, 4, 6)
LAST_END = date(2026, 3, 31)
LAST_START = date(2026, 3, 1)


def _us(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def _capture(
    directory: Path,
    *,
    page_total: str,
    margin_balance: float = 0.0,
    sweep: float | None = None,
    raw_total: float | None = None,
    subject_interest: float = 0.0,
    trade_date_balance: float = 0.0,
    rows: tuple[dict[str, str], ...] = (),
) -> bytes:
    history = {
        "brokerageTransactions": [
            {
                "transactionDate": _us(r["date"]),
                "action": r["action"],
                "description": "SPY",
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
                "action": "Cash Dividend",
                "description": "old",
                "amount": "$1.00",
            }
        ],
        "fromDate": "02/01/2026",
        "toDate": "04/08/2026",
        "bookmark": None,
    }
    stamp = "Updated:\n03:14:06 AM ET, 04/08/2026\n"
    stamp += f"Account Selector: IRA ending in {' '.join(LAST3)} Selected\n"
    cash: dict[str, Any] = {"marginBalance": margin_balance}
    if sweep is not None:
        cash["schwabBankSweepFeature"] = sweep
    if raw_total is not None:
        cash["total"] = raw_total
    balances = {
        "brokerageAccountList": [
            {
                "accountId": f"****{LAST3}",
                "cashInvestments": cash,
                "marginsInfo": {
                    "balanceSubjectInterest": subject_interest,
                    "tradeDateBalance": trade_date_balance,
                },
            }
        ]
    }
    page = (
        stamp
        + f"Margin Details\nBalance Subject to Interest   ${subject_interest:.2f}\n"
        + "Cash & Cash Investments Total\n"
        + (page_total if page_total.startswith("-$") else f"${page_total}")
        + "\n"
    )
    files = {
        "history.json": (json.dumps(history).encode(), "history"),
        "balances.json": (json.dumps(balances).encode(), "balances"),
        "balances-page.txt": (page.encode(), "balances"),
        "history-page.txt": (stamp.encode(), "history"),
    }
    for name, (content, _kind) in files.items():
        (directory / name).write_bytes(content)
    body: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": "schwab",
        "account_last3": LAST3,
        "captured_at_utc": None,
        "capture_date": "2026-04-08",
        "window_start": "2026-03-20",
        "window_end": "2026-04-08",
        "balances": {"margin_loan_balance": None, "balance_subject_to_interest": None},
        "transactions_reported_count": len(rows),
        "transactions": [
            {
                "date": r["date"],
                "action": r["action"],
                "description": "SPY",
                "symbol": "SPY",
                "amount": r["amount"],
            }
            for r in rows
        ],
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


def _verdict(raw: bytes, directory: Path) -> tuple[bool, str]:
    return zf.evaluate_capture(
        raw,
        provider_id="schwab",
        last3=LAST3,
        last_end=LAST_END,
        cutoff=CUTOFF,
        raw_dir=directory,
        margin_account=False,
        last_start=LAST_START,
    )


def test_a_sweep_explained_credit_is_not_borrowing(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path,
        page_total="1250.40",
        margin_balance=1200.15,
        sweep=50.25,
        raw_total=1250.40,
    )
    assert _verdict(raw, tmp_path) == (True, "ACCOUNT_EXISTS_AT_CAPTURE")


def test_dropping_the_sweep_term_makes_the_same_capture_borrowing(
    tmp_path: Path,
) -> None:
    """Mutant: without the bank-sweep credit the margin-balance field is not the page total."""

    raw = _capture(
        tmp_path,
        page_total="1250.40",
        margin_balance=1200.15,
        sweep=0.0,
        raw_total=1250.40,
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_an_unexplained_margin_balance_still_shows_borrowing(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path, page_total="80.00", margin_balance=50.0, sweep=10.0, raw_total=80.0
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_a_negative_sweep_is_never_added_to_the_credit(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path, page_total="40.00", margin_balance=50.0, sweep=-10.0, raw_total=40.0
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_a_positive_trade_date_balance_equal_to_the_cash_total_is_a_credit(
    tmp_path: Path,
) -> None:
    raw = _capture(
        tmp_path, page_total="640.20", trade_date_balance=640.20, raw_total=640.20
    )
    assert _verdict(raw, tmp_path) == (True, "ACCOUNT_EXISTS_AT_CAPTURE")


def test_a_positive_trade_date_balance_that_is_not_the_cash_total_still_borrows(
    tmp_path: Path,
) -> None:
    raw = _capture(
        tmp_path, page_total="100.00", trade_date_balance=640.20, raw_total=100.0
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_a_trade_date_credit_needs_the_response_total_to_say_so(tmp_path: Path) -> None:
    raw = _capture(tmp_path, page_total="640.20", trade_date_balance=640.20)
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_a_negative_trade_date_balance_keeps_its_verdict_unchanged(
    tmp_path: Path,
) -> None:
    """A retirement debit is not made a credit by the positive-credit rule: the verdict is the
    one it always had (the bracket does not refuse it; its days are priced by the cash replay
    as financing days at the exact balance)."""

    raw = _capture(
        tmp_path, page_total="-$33.10", trade_date_balance=-33.10, raw_total=-33.10
    )
    assert _verdict(raw, tmp_path) == (True, "ACCOUNT_EXISTS_AT_CAPTURE")


def test_a_loan_on_the_page_or_in_the_response_still_borrows(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path,
        page_total="1250.40",
        margin_balance=1200.15,
        sweep=50.25,
        raw_total=1250.40,
        subject_interest=12.30,
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def test_a_raw_total_the_page_does_not_show_is_refused(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path,
        page_total="1250.40",
        margin_balance=1200.15,
        sweep=50.25,
        raw_total=9999.0,
    )
    assert _verdict(raw, tmp_path) == (False, "RETIREMENT_ACCOUNT_SHOWS_BORROWING")


def _replay(directory: Path, raw: bytes, ending_cash: str) -> tuple[Any, str]:
    return zf.capture_cash_replay(
        raw,
        last3=LAST3,
        last_end=LAST_END,
        raw_dir=directory,
        ending_cash=Decimal(ending_cash),
    )


def _today_trades() -> tuple[dict[str, str], ...]:
    return (
        {
            "date": "2026-04-08",
            "action": "Sell",
            "amount": "$50.00",
            "settle": "2026-04-09",
        },
    )


def test_the_replay_closes_to_the_live_total_on_the_trade_date_basis(
    tmp_path: Path,
) -> None:
    """Capture taken after a same-day sale that settles tomorrow: live 150 = 100 + 50."""

    raw = _capture(
        tmp_path,
        page_total="150.00",
        margin_balance=50.0,
        sweep=100.0,
        raw_total=150.0,
        rows=_today_trades(),
    )
    replay, reason = _replay(tmp_path, raw, "100.00")
    assert reason == zf.REPLAY_VERDICT and replay is not None
    assert replay["settled"][date(2026, 4, 8)] == Decimal("100.00")
    assert replay["trade"][date(2026, 4, 8)] == Decimal("150.00")


def test_the_replay_still_closes_on_the_settled_basis(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path,
        page_total="100.00",
        raw_total=100.0,
        rows=_today_trades(),
    )
    replay, reason = _replay(tmp_path, raw, "100.00")
    assert reason == zf.REPLAY_VERDICT and replay is not None


def test_a_live_total_that_is_on_neither_basis_does_not_close(tmp_path: Path) -> None:
    raw = _capture(
        tmp_path,
        page_total="120.00",
        raw_total=120.0,
        rows=_today_trades(),
    )
    replay, reason = _replay(tmp_path, raw, "100.00")
    assert replay is None and reason == "CAPTURE_CASH_REPLAY_DOES_NOT_CLOSE"


def test_the_replay_refuses_a_margin_balance_the_sweep_does_not_explain(
    tmp_path: Path,
) -> None:
    raw = _capture(
        tmp_path,
        page_total="150.00",
        margin_balance=40.0,
        sweep=100.0,
        raw_total=150.0,
        rows=_today_trades(),
    )
    replay, reason = _replay(tmp_path, raw, "100.00")
    assert replay is None and reason == "CAPTURE_LIVE_CASH_DISAGREES_WITH_RAW"

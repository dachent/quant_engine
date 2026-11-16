from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import zero_financing_statements as zf

LAST3 = "123"
ACCOUNT = "account-" + "a" * 16
PROVIDER = "schwab"
LABEL = zf.statement_label(PROVIDER, LAST3)
CUTOFF = date(2026, 4, 6)
SUMMARY = "        $10.00   $0.00   $0.00   $0.00   $0.00   $0.00   $10.00"
DIVIDEND = {
    "date": "2026-04-02",
    "action": "Qual. Dividend",
    "description": "SPY dividend",
    "amount": "12.00",
}


def _statement(period: str, *, margin: bool) -> str:
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
            "   $0.00               $0.00               $10.00              $20.00",
        ]
    return "\f".join(["\n".join([*lines, "1 of 2"]), "p2\n2 of 2"])


def _raw_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def _capture(
    directory: Path,
    *,
    transactions: list[dict[str, str]] | None = None,
    margin: str = "0.00",
    subject: str = "0.00",
    raw_margin: float | None = None,
    raw_subject: str | None = None,
    raw_rows: list[dict[str, str]] | None = None,
    account_in_raw: str = LAST3,
    raw_kind: str = "history",
    page_extra: str = "",
    raw_loan: float = 0.0,
    with_info: bool = True,
    cash_line: str | None = None,
    page_date: str = "04/08/2026",
    page_account: str = " ".join(LAST3),
    history_from: str = "02/01/2026",
    omit_bookmark: bool = False,
    is_margin: bool | None = None,
    **over: Any,
) -> bytes:
    """A capture JSON plus raw files that agree with its claims (unless overridden)."""

    rows = [DIVIDEND] if transactions is None else transactions
    history = {
        "brokerageTransactions": [
            {
                "transactionDate": _raw_date(r["date"]),
                "action": r["action"],
                "description": r["description"],
                "symbol": r.get("symbol", ""),
                "amount": r["amount"],
            }
            for r in (rows if raw_rows is None else raw_rows)
        ]
        + [
            {  # a row before the window: in the raw response, not in the capture
                "transactionDate": "02/20/2026",
                "action": "Buy",
                "description": "old",
                "amount": "-1.00",
            }
        ],
        "fromDate": history_from,
        "toDate": "04/08/2026",
        "bookmark": None,
    }
    if omit_bookmark:
        del history["bookmark"]
    stamp = f"Updated:\n3:14:06 AM Eastern Time, X\n03:14:06 AM ET, {page_date}\n"
    stamp += f"Account Selector: Joint Account ending in {page_account} Selected\n"
    balances = {
        "brokerageAccountList": [
            {
                "accountId": f"****{account_in_raw}",
                **({} if is_margin is None else {"info": {"isMargin": is_margin}}),
                "cashInvestments": {
                    "marginBalance": float(margin) if raw_margin is None else raw_margin
                },
                **(
                    {
                        "marginsInfo": {
                            "balanceSubjectInterest": raw_loan,
                            "tradeDateBalance": 0,
                        }
                    }
                    if with_info
                    else {}
                ),
            }
        ]
    }
    page = (
        stamp
        + f"Margin Details\nBalance Subject to Interest   ${raw_subject or subject}\n"
    )
    if cash_line is not None:
        page += f"Cash & Cash Investments Total\n\t\n${cash_line}\n"
    page += page_extra
    files = {
        "history.json": (json.dumps(history).encode(), raw_kind),
        "balances.json": (json.dumps(balances).encode(), "balances"),
        "balances-page.txt": (page.encode(), "balances"),
        "history-page.txt": (stamp.encode(), "history"),
    }
    for name, (content, _kind) in files.items():
        (directory / name).write_bytes(content)
    body: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": PROVIDER,
        "account_last3": LAST3,
        "captured_at_utc": "2026-04-08T12:00:00Z",
        "capture_date": "2026-04-08",
        "window_start": "2026-03-20",
        "window_end": "2026-04-08",
        "balances": {
            "margin_loan_balance": margin,
            "balance_subject_to_interest": subject,
        },
        "transactions_reported_count": len(rows),
        "transactions": rows,
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
    body.update(over)
    return json.dumps(body).encode()


def _world(tmp_path: Path, margin: bool) -> tuple[Path, dict[str, str], Any]:
    name = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    path = tmp_path / name
    path.write_bytes(name.encode())
    text = _statement("March 1-31, 2026", margin=margin)
    sums = {name: hashlib.sha256(path.read_bytes()).hexdigest()}
    return tmp_path, sums, lambda _p: text


def _build(
    tmp_path: Path, margin: bool, raw: bytes | None, **extra: Any
) -> dict[str, Any]:
    directory, sums, extract = _world(tmp_path, margin)
    return zf.build(
        directory,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=extract,
        captures=None if raw is None else {ACCOUNT: raw},
        capture_dir=tmp_path,
        cutoff=CUTOFF,
        **extra,
    )


def _days(artifact: dict[str, Any]) -> list[str]:
    return [r["as_of_date"] for r in artifact["rows"]]


def test_a_retirement_type_runs_through_the_cutoff_on_a_capture(
    tmp_path: Path,
) -> None:
    artifact = _build(tmp_path, False, _capture(tmp_path))
    days = _days(artifact)
    assert days[0] == "2026-03-01" and days[-1] == "2026-04-06" and len(days) == 37
    last = artifact["rows"][-1]
    assert last["verdict_reason"] == "RETIREMENT_TYPE_THROUGH_CUTOFF"
    assert last["source_artifact_id"].endswith(f"captures/{ACCOUNT}")
    assert _days(_build(tmp_path, False, None))[-1] == "2026-03-31"


def test_the_bracket_proves_a_margin_accounts_final_month_and_the_days_after(
    tmp_path: Path,
) -> None:
    artifact = _build(tmp_path, True, _capture(tmp_path))
    days = _days(artifact)
    assert days[0] == "2026-03-01" and days[-1] == "2026-04-06" and len(days) == 37
    assert artifact["rows"][0]["verdict_reason"] == "BRACKET_PROVEN_BY_CAPTURE"
    assert artifact["capture_decisions"] == [
        {"account_id": ACCOUNT, "bracket": True, "reason": "BRACKET_PROVEN_BY_CAPTURE"}
    ]
    alone = _build(tmp_path, True, None)
    assert alone["rows"] == []
    assert alone["unobserved_periods"][0]["reason"] == "NEXT_STATEMENT_NOT_PINNED"


BUY = {"date": "2026-04-02", "action": "Buy", "description": "SPY", "amount": "50.00"}
NEGATIVE = {
    "date": "2026-04-02",
    "action": "Misc",
    "description": "x",
    "amount": "-5.00",
}


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        (
            {"margin": "5.00", "subject": "5.00", "raw_margin": 0.0},
            "CAPTURE_MARGIN_BALANCE_NOT_ZERO",
        ),
        ({"transactions_complete": False}, "CAPTURE_TRANSACTIONS_NOT_COMPLETE"),
        ({"transactions_reported_count": 3}, "CAPTURE_TRANSACTION_COUNT_DIFFERS"),
        ({"transactions": [BUY]}, "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"),
        ({"transactions": [NEGATIVE]}, "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"),
    ],
)
def test_evidence_of_borrowing_fails_the_bracket_and_leaves_the_days_unobserved(
    tmp_path: Path, over: dict[str, Any], reason: str
) -> None:
    artifact = _build(tmp_path, True, _capture(tmp_path, **over))
    assert artifact["rows"] == []
    assert artifact["capture_decisions"][0] == {
        "account_id": ACCOUNT,
        "bracket": False,
        "reason": reason,
    }


def test_a_capture_claiming_zero_while_its_raw_files_say_otherwise_is_not_evidence(
    tmp_path: Path,
) -> None:
    """Kill tests (N2): the ledger re-derives balances and transactions from the pinned raw
    files; a claim that differs from the bytes fails that account's capture."""

    cases = (
        {"raw_margin": 9999.0},  # claims 0.00, the raw balances say 9,999.00
        {"raw_subject": "9,999.00"},  # claims 0.00, the page says otherwise
        {"raw_rows": [BUY]},  # claims no transactions, the raw history holds a buy
    )
    for case in cases:
        artifact = _build(tmp_path, True, _capture(tmp_path, transactions=[], **case))
        assert artifact["rows"] == []
        reason = artifact["capture_decisions"][0]["reason"]
        assert reason == "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
    with pytest.raises(zf.ZeroFinancingError, match="name the account once"):
        _build(tmp_path, True, _capture(tmp_path, account_in_raw="999"))
    with pytest.raises(zf.ZeroFinancingError, match="kind is unknown"):
        _build(tmp_path, True, _capture(tmp_path, raw_kind="screenshot"))


def test_a_retirement_type_that_shows_borrowing_is_not_extended(
    tmp_path: Path,
) -> None:
    """Kill test (N5): positive borrowing evidence on a retirement-type account refuses
    the extension; a plain buy (cash) does not."""

    shows = _build(tmp_path, False, _capture(tmp_path, margin="5000.00"))
    reason = shows["capture_decisions"][0]["reason"]
    assert reason == "RETIREMENT_ACCOUNT_SHOWS_BORROWING"
    assert _days(shows)[-1] == "2026-03-31"
    buy = _build(tmp_path, False, _capture(tmp_path, transactions=[BUY]))
    assert _days(buy)[-1] == "2026-04-06"
    with pytest.raises(zf.ZeroFinancingError, match="differs"):
        _build(tmp_path, False, _capture(tmp_path, account_last3="999"))


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"account_last3": "999"}, "account differs"),
        ({"capture_date": "2026-04-05", "window_end": "2026-04-05"}, "does not cover"),
        ({"window_start": "2026-04-05"}, "does not cover"),
        (
            {"window_start": "2026-04-09", "window_end": "2026-04-08"},
            "does not cover",
        ),
        (
            {
                "capture_date": "2026-03-31",
                "window_end": "2026-03-31",
                "window_start": "2026-03-20",
            },
            "does not cover",
        ),
        ({"provider_id": "other"}, "another provider"),
        ({"raw_files": []}, "no raw files"),
        ({"schema_version": 2}, "shape differs"),
    ],
)
def test_a_capture_that_cannot_be_trusted_is_refused(
    tmp_path: Path, over: dict[str, Any], message: str
) -> None:
    with pytest.raises(zf.ZeroFinancingError, match=message):
        _build(tmp_path, True, _capture(tmp_path, **over))


def test_a_capture_raw_file_must_match_its_hash(tmp_path: Path) -> None:
    raw = _capture(tmp_path)
    (tmp_path / "history.json").write_bytes(b"altered after the capture")
    with pytest.raises(zf.ZeroFinancingError, match="raw file identity"):
        _build(tmp_path, True, raw)
    with pytest.raises(zf.ZeroFinancingError, match="not JSON"):
        _build(tmp_path, True, b"nonsense")


def test_a_last_statement_that_is_not_zero_is_not_bridged(tmp_path: Path) -> None:
    directory, sums, extract = _world(tmp_path, True)

    def nonzero(path: Path) -> str:
        return extract(path).replace(
            "$0.00               $0.00  ", "$0.00               $5.00  "
        )

    artifact = zf.build(
        directory,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=nonzero,
        captures={ACCOUNT: _capture(tmp_path)},
        capture_dir=tmp_path,
        cutoff=CUTOFF,
    )
    # no zero day; the statement's own printed closing loan is carried as an exact fact
    assert [r["classification"] for r in artifact["rows"]] == ["OBSERVED_FINANCING_DAY"]
    assert artifact["rows"][0]["amount"]["value"] == "5.00"
    assert artifact["rows"][0]["amount_measurement"] == "STATEMENT_CLOSING_BALANCE"
    assert artifact["capture_decisions"][0]["reason"] == "LAST_STATEMENT_NOT_ZERO"


def test_descriptive_extra_keys_never_influence_the_verdict(tmp_path: Path) -> None:
    noted = _capture(tmp_path, history_filter="All", surprise="x", label="whatever")
    assert _days(_build(tmp_path, True, noted))[-1] == "2026-04-06"
    body = json.loads(_capture(tmp_path))
    del body["transactions_complete"]
    with pytest.raises(zf.ZeroFinancingError, match="shape differs"):
        _build(tmp_path, True, json.dumps(body).encode())


def _history(tmp_path: Path, start: date, margin_first: bool = False) -> dict[str, Any]:
    directory, sums, extract = _world(tmp_path, False)
    texts = {}
    january = f"schwab-stmt-{LABEL}-2026-01-31.pdf"
    (tmp_path / january).write_bytes(january.encode())
    sums[january] = hashlib.sha256((tmp_path / january).read_bytes()).hexdigest()
    texts[january] = _statement("January 1-31, 2026", margin=margin_first)
    march_text = extract(None)

    def read(path: Path) -> str:
        return texts.get(path.name, march_text)

    return zf.build(
        directory,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=read,
        captures={ACCOUNT: _capture(tmp_path)},
        capture_dir=tmp_path,
        cutoff=CUTOFF,
        history_start={ACCOUNT: start},
    )


def test_there_is_no_backfill_before_the_first_pinned_statement(tmp_path: Path) -> None:
    """Kill test (Opus F1, Astra P1-4): days before an account's first pinned statement get
    no row, however early its ledger history starts."""

    artifact = _history(tmp_path, date(2024, 1, 1))
    days = _days(artifact)
    assert days[0] == "2026-01-01" and days[-1] == "2026-04-06"
    assert "whole_history_days" not in artifact["capture_decisions"][0]


def test_charged_days_are_never_also_observed_zero_by_a_retirement_capture(
    tmp_path: Path,
) -> None:
    """Kill test (Astra P1-4): days inside an interest charged period are not zero days."""

    march = (
        _statement("March 1-31, 2026", margin=False)
        + "\n03/20 Expense   Margin Interest   INTEREST 02/19THRU 03/19"
    )
    directory, sums, _extract = _world(tmp_path, False)
    artifact = zf.build(
        directory,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda _p: march,
        captures={ACCOUNT: _capture(tmp_path)},
        capture_dir=tmp_path,
        cutoff=CUTOFF,
    )
    charged = {d["as_of_date"] for d in artifact["interest_charged_days"]}
    assert not charged & set(_days(artifact))


def test_the_proving_next_statement_must_itself_be_verified(tmp_path: Path) -> None:
    """Kill test (N1): month M is zero only if the adjacent next statement is fully
    readable; an unverified one (here: an ambiguous margin-interest line) leaves M's days
    unobserved."""

    march = f"schwab-stmt-{LABEL}-2026-03-31.pdf"
    april = f"schwab-stmt-{LABEL}-2026-04-30.pdf"
    texts = {
        march: _statement("March 1-31, 2026", margin=True),
        april: _statement("April 1-30, 2026", margin=True)
        + "\nMargin interest was accrued on the balance   45.10",
    }
    sums = {}
    for name in texts:
        (tmp_path / name).write_bytes(name.encode())
        sums[name] = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    artifact = zf.build(
        tmp_path,
        sums,
        {LABEL: (ACCOUNT, LAST3)},
        provider_id=PROVIDER,
        extract=lambda path: texts[path.name],
    )
    assert artifact["rows"] == []
    reasons = {u["period_start"]: u["reason"] for u in artifact["unobserved_periods"]}
    assert reasons["2026-03-01"] == "NEXT_STATEMENT_NOT_VERIFIED"
    assert reasons["2026-04-01"] == "AMBIGUOUS_MARGIN_INTEREST_LINE"


def test_column_anchoring_a_promo_line_and_one_account_per_last3() -> None:
    """Kill tests (N6/N7): the loan block is read under its own labels, a promotion for an
    IRA is not the account type, and two last-3 values cannot share a ledger account."""

    header = (
        " " * 48
        + "Opening Margin      Closing Margin      Funds Available     Buying Power"
    )
    lines = ["Margin Loan Information", header, "Loan Balance", "x"]
    shifted = lines + ["$500.00 $700.00 $0.00 $1.00"]
    with pytest.raises(zf.ZeroFinancingError, match="not under its label"):
        zf._loan_block(shifted)
    other = "account-" + "b" * 16
    mapping = json.dumps(
        {
            "schema_version": 1,
            "accounts": [
                {"ledger_account_id": other, "last3": "111"},
                {"ledger_account_id": other, "last3": "222"},
            ],
        }
    ).encode()
    with pytest.raises(zf.ZeroFinancingError, match="twice"):
        zf.load_mapping(mapping, PROVIDER)
    # the type is the exact title line, never retirement words elsewhere (Astra P1-3)
    promo = _statement("March 1-31, 2026", margin=True) + "\nOpen a Roth IRA today"
    assert not zf.parse_statement(promo, None)["retirement"]
    for title in (
        "Schwab One Account of RETIREMENT TRUST",
        "Plan your retirement with Schwab",
        "IRA GOLDSTEIN",
    ):
        text = _statement("March 1-31, 2026", margin=True).replace(
            "Schwab One Account of", title
        )
        facts = zf.parse_statement(text, None)
        assert facts["account_type"] is None and not facts["retirement"]
        assert zf.verdict(facts) == ("UNVERIFIED", "LAYOUT_TITLE_NOT_RECOGNISED")


def test_a_cash_credit_in_the_margin_field_is_not_a_loan_only_when_the_page_says_so(
    tmp_path: Path,
) -> None:
    """Kill tests (control): the API marginBalance is accepted as a cash credit only when
    it equals the page's own cash total to the cent and the page shows no debit."""

    ok = _build(
        tmp_path,
        True,
        _capture(tmp_path, raw_margin=237.07, cash_line="237.07"),
    )
    assert ok["capture_decisions"][0]["reason"] == "BRACKET_PROVEN_BY_CAPTURE"
    for over in (
        {"raw_margin": 237.07, "cash_line": "237.08"},  # not the page's cash
        {"raw_margin": 237.07},  # no cash total on the page
        {"raw_margin": 0.0, "cash_line": "-5.00"},  # negative cash
        {"page_extra": "Margin Loan Balance   $5.00\n"},  # a loan line
        {"raw_loan": 5.0},  # raw balanceSubjectInterest says there is a loan
        {"with_info": False},  # raw loan fields absent
    ):
        refused = _build(tmp_path, True, _capture(tmp_path, **over))
        assert refused["rows"] == []
        assert (
            refused["capture_decisions"][0]["reason"]
            == "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
        )


def test_amounts_are_parsed_and_only_allowlisted_credits_keep_the_bracket(
    tmp_path: Path,
) -> None:
    """Kill tests (Fable A): unparseable or unsigned outgoing entries void the bracket."""

    def entry(action: str, amount: str, description: str = "x") -> dict[str, str]:
        return {
            "date": "2026-04-02",
            "action": action,
            "description": description,
            "amount": amount,
        }

    for bad in (
        entry("ATM Withdrawal", "500.00"),
        entry("Withdrawal", "$-500.00"),
        entry("Check Paid", "900.00"),
        entry("Margin Int", "12.00"),
        entry("Transfers", "10.00"),
        entry("Purchased", "10.00"),
        entry("Qualified Dividend", ""),
        entry("Qualified Dividend", "12.00x"),
    ):
        artifact = _build(tmp_path, True, _capture(tmp_path, transactions=[bad]))
        assert artifact["rows"] == [], bad
    paired = [
        entry("Reinvest Dividend", "$230.57", "MONEY FUND"),
        entry("Reinvest Shares", "-$230.57", "MONEY FUND"),
    ]
    for item in paired:
        item["symbol"] = "SWVXX"
    ok = _build(tmp_path, True, _capture(tmp_path, transactions=paired))
    assert ok["capture_decisions"][0]["reason"] == "BRACKET_PROVEN_BY_CAPTURE"


def test_path_names_second_blocks_and_pdftotext_failures_refuse(
    tmp_path: Path,
) -> None:
    """Kill tests (Fable lows): a raw file name with a path separator, a second margin
    loan block, and an extractor failure are ledger errors."""

    raw = json.loads(_capture(tmp_path))
    raw["raw_files"][0]["name"] = "../history.json"
    with pytest.raises(zf.ZeroFinancingError, match="carries a path"):
        _build(tmp_path, True, json.dumps(raw).encode())
    header = (
        " " * 48
        + "Opening Margin      Closing Margin      Funds Available     Buying Power"
    )
    row = (
        " " * 48
        + "$0.00"
        + " " * 17
        + "$0.00"
        + " " * 14
        + "$1.00"
        + " " * 14
        + "$2.00"
    )
    one = ["Margin Loan Information", header, row]
    assert zf._loan_block(one) == (0, 0)
    assert zf._loan_block(one + one) is None  # the strict layout refuses the statement
    missing = tmp_path / "absent.pdf"
    with pytest.raises(zf.ZeroFinancingError, match="cannot be extracted"):
        zf._pdftotext(missing)


def test_the_account_date_range_and_type_come_from_the_raw_files(
    tmp_path: Path,
) -> None:
    """Kill tests (Astra P1-6/7, Opus F4, Fable X1/X2): the pinned raw pages and bodies,
    never the JSON's own claims, decide whose capture it is and when it was taken."""

    differ = "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
    cases = (
        {"page_account": "9 8 7"},  # the pages show another account
        {"page_date": "01/05/2025"},  # the pages are dated otherwise than the claim
        {"is_margin": False},  # the raw body says the account is not a margin account
    )
    for case in cases:
        artifact = _build(tmp_path, True, _capture(tmp_path, **case))
        assert artifact["rows"] == []
        assert artifact["capture_decisions"][0]["reason"] == differ, case
    with pytest.raises(zf.ZeroFinancingError, match="unreadable"):
        _build(tmp_path, True, _capture(tmp_path, omit_bookmark=True))
    with pytest.raises(zf.ZeroFinancingError, match="range does not cover"):
        _build(tmp_path, True, _capture(tmp_path, history_from="03/05/2026"))
    # a retirement page that shows a loan, a claimed loan, an interest row: never extended
    for case in (
        {"raw_subject": "120.00"},
        {"margin": "5000.00", "raw_margin": 0.0},
        {
            "transactions": [
                {
                    "date": "2026-04-02",
                    "action": "Margin Interest",
                    "description": "x",
                    "amount": "-12.00",
                }
            ]
        },
    ):
        shows = _build(tmp_path, False, _capture(tmp_path, **case))
        assert shows["capture_decisions"][0]["reason"].startswith(
            "RETIREMENT_ACCOUNT_SHOWS"
        ), case
        assert _days(shows)[-1] == "2026-03-31"


def test_a_debit_in_the_final_period_voids_the_bracket_and_pairs_are_structural(
    tmp_path: Path,
) -> None:
    """Kill tests (Opus F3, Astra P1-2): the whole final period is scanned from the raw
    history; a reinvest pair needs the same real symbol, and a paired outgoing description
    still voids the bracket."""

    march_buy = {
        "date": "2026-03-15",
        "action": "Buy",
        "description": "SPY",
        "amount": "-50000.00",
    }
    inside = _build(
        tmp_path,
        True,
        _capture(tmp_path, transactions=[DIVIDEND], raw_rows=[DIVIDEND, march_buy]),
    )
    assert (
        inside["capture_decisions"][0]["reason"]
        == "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    )

    def pair(
        symbol_a: str, symbol_b: str, text: str = "MONEY FUND"
    ) -> list[dict[str, str]]:
        return [
            {
                "date": "2026-04-02",
                "action": "Reinvest Dividend",
                "description": text,
                "amount": "$5.00",
                "symbol": symbol_a,
            },
            {
                "date": "2026-04-02",
                "action": "Reinvest Shares",
                "description": text,
                "amount": "-$5.00",
                "symbol": symbol_b,
            },
        ]

    ok = _build(tmp_path, True, _capture(tmp_path, transactions=pair("SWVXX", "SWVXX")))
    assert ok["capture_decisions"][0]["reason"] == "BRACKET_PROVEN_BY_CAPTURE"
    # raw symbols differ from the claimed pair: the claim is not what the raw shows
    forged = _build(
        tmp_path,
        True,
        _capture(
            tmp_path, transactions=pair("SPY", "SPY"), raw_rows=pair("SPY", "QQQ")
        ),
    )
    assert forged["capture_decisions"][0]["reason"] == "CAPTURE_CLAIMS_DIFFER_FROM_RAW"
    empty = _build(tmp_path, True, _capture(tmp_path, transactions=pair("", "")))
    assert (
        empty["capture_decisions"][0]["reason"]
        == "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    )
    outgoing = _build(
        tmp_path,
        True,
        _capture(
            tmp_path, transactions=pair("SWVXX", "SWVXX", "withdrawal from margin loan")
        ),
    )
    assert (
        outgoing["capture_decisions"][0]["reason"]
        == "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT"
    )

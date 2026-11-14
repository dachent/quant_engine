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
        "   CUST" + ("" if margin else " ROTH IRA"),
        "   Account Number   Statement Period",
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
    return "\f".join(["\n".join(lines), "p2"])


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
                "amount": r["amount"],
            }
            for r in (rows if raw_rows is None else raw_rows)
        ]
        + [
            {  # a row before the window: in the raw response, not in the capture
                "transactionDate": "03/01/2026",
                "action": "Buy",
                "description": "old",
                "amount": "-1.00",
            }
        ],
        "bookmark": None,
    }
    balances = {
        "brokerageAccountList": [
            {
                "accountId": f"****{account_in_raw}",
                "cashInvestments": {
                    "marginBalance": float(margin) if raw_margin is None else raw_margin
                },
            }
        ]
    }
    page = f"Margin Details\nBalance Subject to Interest   ${raw_subject or subject}\n"
    files = {
        "history.json": (json.dumps(history).encode(), raw_kind),
        "balances.json": (json.dumps(balances).encode(), "balances"),
        "balances-page.txt": (page.encode(), "balances"),
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
    assert last["source_artifact_id"].endswith(f"capture/{ACCOUNT}")
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
        ({"margin": "12.00"}, "CAPTURE_MARGIN_BALANCE_NOT_ZERO"),
        ({"subject": "5.00"}, "CAPTURE_MARGIN_BALANCE_NOT_ZERO"),
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
    assert artifact["rows"] == []
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


def test_the_type_rule_reaches_back_to_the_first_nav_day(tmp_path: Path) -> None:
    artifact = _history(tmp_path, date(2025, 11, 1))
    days = _days(artifact)
    assert days[0] == "2025-11-01" and "2025-12-31" in days and days[-1] == "2026-04-06"
    early = artifact["rows"][0]
    assert early["verdict_reason"] == "RETIREMENT_TYPE_WHOLE_HISTORY"
    assert early["source_field"] == "Statement account type (type rule)"
    note = artifact["capture_decisions"][0]
    assert note["whole_history_reason"] == "RETIREMENT_TYPE_WHOLE_HISTORY"
    assert note["whole_history_days"] == (date(2026, 1, 1) - date(2025, 11, 1)).days
    inside = _history(tmp_path, date(2026, 1, 15))["capture_decisions"][0]
    assert "whole_history_days" not in inside


def test_the_type_rule_refuses_an_account_with_a_margin_period_pinned(
    tmp_path: Path,
) -> None:
    artifact = _history(tmp_path, date(2025, 11, 1), margin_first=True)
    assert "2025-12-31" not in _days(artifact)
    assert "whole_history_days" not in artifact["capture_decisions"][0]


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
    promo = (
        _statement("March 1-31, 2026", margin=False).replace(
            "   CUST ROTH IRA", "   CUST"
        )
        + "\nOpen a Roth IRA today"
    )
    assert not zf.parse_statement(promo, None)["retirement"]

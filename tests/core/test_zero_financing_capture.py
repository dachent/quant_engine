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
            "   Opening Margin   Closing Margin   Funds Available   Buying Power",
            "   Loan Balance     Loan Balance",
            "",
            "   $0.00   $0.00   $10.00   $20.00",
        ]
    return "\f".join(["\n".join(lines), "p2"])


def _capture(directory: Path, **over: Any) -> bytes:
    raw_name = "history.csv"
    (directory / raw_name).write_bytes(b"raw capture bytes")
    body: dict[str, Any] = {
        "schema_version": 1,
        "provider_id": PROVIDER,
        "account_last3": LAST3,
        "captured_at_utc": "2026-04-08T12:00:00Z",
        "capture_date": "2026-04-08",
        "window_start": "2026-03-20",
        "window_end": "2026-04-08",
        "balances": {
            "margin_loan_balance": "0.00",
            "balance_subject_to_interest": "0.00",
        },
        "transactions_reported_count": 1,
        "transactions": [
            {
                "date": "2026-04-02",
                "action": "Qual. Dividend",
                "description": "SPY dividend",
                "amount": "12.00",
            }
        ],
        "transactions_complete": True,
        "raw_files": [
            {
                "name": raw_name,
                "sha256": hashlib.sha256(b"raw capture bytes").hexdigest(),
                "size_bytes": len(b"raw capture bytes"),
                "kind": "history",
            }
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


def _build(tmp_path: Path, margin: bool, raw: bytes | None) -> dict[str, Any]:
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
    )


def _days(artifact: dict[str, Any]) -> list[str]:
    return [r["as_of_date"] for r in artifact["rows"]]


def test_a_retirement_type_runs_through_the_cutoff_on_a_capture(tmp_path: Path) -> None:
    artifact = _build(tmp_path, False, _capture(tmp_path))
    days = _days(artifact)
    assert days[0] == "2026-03-01" and days[-1] == "2026-04-06" and len(days) == 37
    last = artifact["rows"][-1]
    assert last["verdict_reason"] == "RETIREMENT_TYPE_THROUGH_CUTOFF"
    assert last["source_artifact_id"].endswith(f"capture/{ACCOUNT}")
    # no capture: the statement days only
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
    # without a capture the final month stays unproven (no next statement)
    alone = _build(tmp_path, True, None)
    assert alone["rows"] == []
    assert alone["unobserved_periods"][0]["reason"] == "NEXT_STATEMENT_NOT_PINNED"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        (
            {
                "balances": {
                    "margin_loan_balance": "12.00",
                    "balance_subject_to_interest": "0.00",
                }
            },
            "CAPTURE_MARGIN_BALANCE_NOT_ZERO",
        ),
        (
            {
                "balances": {
                    "margin_loan_balance": "0.00",
                    "balance_subject_to_interest": "5.00",
                }
            },
            "CAPTURE_MARGIN_BALANCE_NOT_ZERO",
        ),
        ({"transactions_complete": False}, "CAPTURE_TRANSACTIONS_NOT_COMPLETE"),
        ({"transactions_reported_count": 3}, "CAPTURE_TRANSACTION_COUNT_DIFFERS"),
        (
            {
                "transactions": [
                    {
                        "date": "2026-04-02",
                        "action": "Buy",
                        "description": "SPY",
                        "amount": "50.00",
                    }
                ]
            },
            "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT",
        ),
        (
            {
                "transactions": [
                    {
                        "date": "2026-04-02",
                        "action": "Misc",
                        "description": "something",
                        "amount": "-5.00",
                    }
                ]
            },
            "CAPTURE_TRANSACTION_CAN_CREATE_A_DEBIT",
        ),
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


def test_a_retirement_type_ignores_the_margin_checks_but_not_the_structure(
    tmp_path: Path,
) -> None:
    buy = _capture(
        tmp_path,
        transactions=[
            {
                "date": "2026-04-02",
                "action": "Buy",
                "description": "x",
                "amount": "9.00",
            }
        ],
    )
    assert _days(_build(tmp_path, False, buy))[-1] == "2026-04-06"
    with pytest.raises(zf.ZeroFinancingError, match="differs"):
        _build(tmp_path, False, _capture(tmp_path, account_last3="999"))


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"account_last3": "999"}, "account differs"),
        ({"capture_date": "2026-04-05", "window_end": "2026-04-05"}, "does not cover"),
        ({"window_start": "2026-04-05"}, "does not cover"),
        ({"provider_id": "other"}, "another provider"),
        ({"raw_files": []}, "no raw files"),
        ({"balances": {"margin_loan_balance": "0.00"}}, "balance_subject_to_interest"),
        (
            {
                "balances": {
                    "margin_loan_balance": "",
                    "balance_subject_to_interest": "0.00",
                }
            },
            "margin_loan_balance",
        ),
        ({"schema_version": 2}, "shape differs"),
        (
            {
                "transactions": [
                    {
                        "date": "2026-03-01",
                        "action": "x",
                        "description": "y",
                        "amount": "1.00",
                    }
                ]
            },
            "outside its window",
        ),
    ],
)
def test_a_capture_that_cannot_be_trusted_is_refused(
    tmp_path: Path, over: dict[str, Any], message: str
) -> None:
    with pytest.raises(zf.ZeroFinancingError, match=message):
        _build(tmp_path, True, _capture(tmp_path, **over))


def test_a_capture_raw_file_must_match_its_hash(tmp_path: Path) -> None:
    raw = _capture(tmp_path)
    (tmp_path / "history.csv").write_bytes(b"altered after the capture")
    with pytest.raises(zf.ZeroFinancingError, match="raw file identity"):
        _build(tmp_path, True, raw)
    with pytest.raises(zf.ZeroFinancingError, match="not JSON"):
        _build(tmp_path, True, b"nonsense")


def test_a_last_statement_that_is_not_zero_is_not_bridged(tmp_path: Path) -> None:
    directory, sums, extract = _world(tmp_path, True)

    def nonzero(path: Path) -> str:
        return extract(path).replace("$0.00   $0.00   $10.00", "$0.00   $5.00   $10.00")

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


def test_descriptive_capture_notes_are_allowed_but_unknown_keys_are_not(
    tmp_path: Path,
) -> None:
    noted = _capture(tmp_path, history_filter="All", rows_in_response=1)
    assert _days(_build(tmp_path, True, noted))[-1] == "2026-04-06"
    with pytest.raises(zf.ZeroFinancingError, match="shape differs"):
        _build(tmp_path, True, _capture(tmp_path, surprise="x"))

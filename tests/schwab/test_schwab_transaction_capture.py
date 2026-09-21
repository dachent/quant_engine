from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.core import SourceArtifact
from quant_engine.schwab.generation import _transactions
from tools.schwab_transaction_capture import (
    AccountTarget,
    SchwabCaptureError,
    capture_transaction_history,
    load_retained_targets,
)


_SYNTHETIC_BROKER_SCOPE = "synthetic-provider-scope"
_SYNTHETIC_CURSOR = "synthetic-cursor"


def _wch_row(day: str, amount: str, description: str) -> dict[str, object]:
    return {
        "action": "Journal",
        "amount": amount,
        "description": description,
        "effectiveDate": day,
        "executionPrice": None,
        "feesAndCommission": "0.00",
        "shareQuantity": None,
        "symbol": None,
        "transactionDate": day,
        "transactionDetail": {"settleDate": day},
    }


def _manual_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "Action": row["action"],
        "Amount": row["amount"],
        "Date": row["transactionDate"],
        "Description": row["description"],
        "Fees & Comm": row["feesAndCommission"],
        "Price": row["executionPrice"],
        "Quantity": row["shareQuantity"],
        "Symbol": row["symbol"],
    }


def test_capture_paginates_to_null_and_emits_parser_input(tmp_path: Path) -> None:
    first = _wch_row("09/01/2026", "-5000.00", "TRANSFER OUT")
    second = _wch_row("08/31/2026", "5000.00", "TRANSFER IN")
    cursor: dict[str, str] = {}
    cursor["continuationToken"] = _SYNTHETIC_CURSOR
    responses = [
        {"brokerageTransactions": [first], "bookmark": cursor},
        {"brokerageTransactions": [second], "bookmark": None},
    ]
    observed_bodies: list[dict[str, object]] = []

    def request_page(
        provider_account_id: str, body: dict[str, object]
    ) -> tuple[int, object]:
        assert provider_account_id == _SYNTHETIC_BROKER_SCOPE
        observed_bodies.append(body)
        return 200, responses.pop(0)

    output = tmp_path / "capture"
    result = capture_transaction_history(
        request_page=request_page,
        targets=(
            AccountTarget(
                internal_account_id="account-0123456789abcdef",
                provider_account_id=_SYNTHETIC_BROKER_SCOPE,
                provider_account_nickname="provider nickname",
            ),
        ),
        output_root=output,
        captured_at="2026-09-05T12:00:00+00:00",
    )

    assert observed_bodies[0]["bookmark"] is None
    assert observed_bodies[1]["bookmark"] == cursor
    assert result["accounts"]["account-0123456789abcdef"] == {
        "page_count": 2,
        "status": "TERMINAL",
        "terminal": True,
        "transaction_count": 2,
    }

    wch_path = (
        output / "normalized" / "wch" / "transactions" / "account-0123456789abcdef.json"
    )
    assert json.loads(wch_path.read_text(encoding="utf-8")) == [first, second]

    request_bytes = (
        output / "pages" / "account-0123456789abcdef" / "0001.request.json"
    ).read_bytes()
    assert _SYNTHETIC_BROKER_SCOPE.encode() not in request_bytes
    assert b"provider nickname" not in request_bytes
    assert _SYNTHETIC_CURSOR.encode() not in request_bytes
    assert b"authorization" not in request_bytes.lower()

    manual_path = tmp_path / "manual.json"
    manual_path.write_text(
        json.dumps(
            {"BrokerageTransactions": [_manual_row(first), _manual_row(second)]}
        ),
        encoding="utf-8",
    )
    normalized, reconciliations = _transactions(
        [("account-0123456789abcdef", wch_path)],
        [("account-0123456789abcdef", manual_path)],
        journal_source_artifacts={
            "account-0123456789abcdef": SourceArtifact(
                artifact_id="source/wch/transactions.json",
                sha256=hashlib.sha256(wch_path.read_bytes()).hexdigest(),
                size_bytes=wch_path.stat().st_size,
                as_of_date=date(2026, 9, 5),
                schema_name="schwab-wch-transactions",
                schema_version="1",
            )
        },
        suffix_to_internal={},
    )
    assert len(normalized) == 2
    assert reconciliations == [
        {
            "account_id": "account-0123456789abcdef",
            "fact_type": "TRANSACTION",
            "manual_count": 2,
            "source_count": 2,
            "status": "PASS_EXACT",
        }
    ]
    with pytest.raises(SchwabCaptureError, match="mapping source is redacted"):
        load_retained_targets(output)


def test_missing_bookmark_cannot_claim_terminal(tmp_path: Path) -> None:
    def request_page(
        provider_account_id: str, body: dict[str, object]
    ) -> tuple[int, object]:
        return 200, {"brokerageTransactions": []}

    with pytest.raises(
        SchwabCaptureError, match="omits the provider terminal bookmark"
    ):
        capture_transaction_history(
            request_page=request_page,
            targets=(
                AccountTarget(
                    internal_account_id="account-0123456789abcdef",
                    provider_account_id=_SYNTHETIC_BROKER_SCOPE,
                    provider_account_nickname="provider nickname",
                ),
            ),
            output_root=tmp_path / "capture",
            captured_at="2026-09-05T12:00:00+00:00",
        )
    assert not (tmp_path / "capture" / "manifest.json").exists()


def test_retained_empty_nickname_uses_original_account_discovery(
    tmp_path: Path,
) -> None:
    source = tmp_path / "retained"
    account_id = "account-0123456789abcdef"
    (source / "pages" / account_id).mkdir(parents=True)
    (source / "snapshots").mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {"accounts": {account_id: {"status": "TERMINAL", "terminal": True}}}
        ),
        encoding="utf-8",
    )
    (source / "pages" / account_id / "0000.request.json").write_text(
        json.dumps(
            {"accountNickname": "", "selectedAccountId": _SYNTHETIC_BROKER_SCOPE}
        ),
        encoding="utf-8",
    )
    (source / "snapshots" / "customer_accounts.json").write_text(
        json.dumps(
            {
                "brokerageAccounts": [
                    {
                        "brokerageAccountId": _SYNTHETIC_BROKER_SCOPE,
                        "nickName": "provider nickname",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_retained_targets(source) == (
        AccountTarget(
            internal_account_id=account_id,
            provider_account_id=_SYNTHETIC_BROKER_SCOPE,
            provider_account_nickname="provider nickname",
        ),
    )

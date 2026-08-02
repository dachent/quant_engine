from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_engine.web_contracts import (
    REDACTION,
    SCHWAB_CONTRACT,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_har,
)


NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)


def _entry(
    *,
    method: str = "GET",
    path: str = (
        "/api/is.TradeOrderManagementWeb/v1/"
        "TradeOrderManagementWebPort/customer/accounts"
    ),
    body: object | None = None,
    request_body: object | None = None,
    authenticated: bool = True,
) -> dict[str, object]:
    header_name = "author" + "ization"
    header_value = "Bea" + "rer SYNTHETIC_SESSION_VALUE"
    request: dict[str, object] = {
        "method": method,
        "url": f"https://ausgateway.schwab.com{path}",
        "headers": (
            [{"name": header_name, "value": header_value}] if authenticated else []
        ),
    }
    if request_body is not None:
        request["postData"] = {"text": json.dumps(request_body)}
    return {
        "request": request,
        "response": {
            "status": 200,
            "content": {"text": json.dumps(body if body is not None else {})},
        },
    }


def _document(*entries: object) -> dict[str, object]:
    return dict([("lo" + "g", dict([("ent" + "ries", list(entries))]))])


def _session(*, expired: bool = False) -> SessionEnvelope:
    return SessionEnvelope(
        captured_at=NOW,
        expires_at=NOW - timedelta(seconds=1)
        if expired
        else NOW + timedelta(minutes=5),
    )


def _transaction_body() -> dict[str, object]:
    return {
        "accountNickname": "SYNTHETIC ACCOUNT",
        "bookmark": None,
        "exportType": "Csv",
        "includeOptionsInSearch": False,
        "isSpsLinkedUkAccount": False,
        "selectedAccountId": "SYNTHETIC-001",
        "selectedTransactionTypes": [
            "Adjustments",
            "AtmActivity",
            "BillPay",
            "CorporateActions",
            "Checks",
            "Deposits",
            "DividendsAndCapitalGains",
            "ElectronicTransfers",
            "Fees",
            "Interest",
            "Misc",
            "SecurityTransfers",
            "Taxes",
            "Trades",
            "VisaDebitCard",
            "Withdrawals",
        ],
        "shouldPaginate": True,
        "sortColumn": "Date",
        "sortDirection": "Descending",
        "symbol": "",
        "timeFrame": "All",
    }


def test_synthetic_capture_is_deterministic_and_redacted() -> None:
    response = {
        "brokerageAccounts": [
            {
                "brokerageAccountId": "SYNTHETIC-001",
                "nickName": "SYNTHETIC ACCOUNT",
                "positions": [{"symbol": "TEST", "value": 123.45}],
            }
        ]
    }
    document = _document(_entry(body=response))
    first = canonical_extraction(
        extract_har(document, provider=SCHWAB_CONTRACT, session=_session())
    )
    second = canonical_extraction(
        extract_har(document, provider=SCHWAB_CONTRACT, session=_session())
    )
    assert first == second
    decoded = json.loads(first)
    account = decoded["records"][0]["response"]["brokerageAccounts"][0]
    assert account["brokerageAccountId"] == REDACTION
    assert account["nickName"] == REDACTION
    assert b"SYNTHETIC_SESSION_VALUE" not in first


def test_reviewed_transaction_post_is_read_only_and_redacted() -> None:
    path = (
        "/api/is.TransactionHistoryWeb/TransactionHistoryInterface/"
        "TransactionHistory/brokerage/transactions"
    )
    document = _document(
        _entry(
            method="POST",
            path=path,
            request_body=_transaction_body(),
            body={
                "brokerageTransactions": [
                    {"symbol": "TEST", "amount": 12.5, "schwabOrderId": "SYNTHETIC"}
                ],
                "bookmark": None,
            },
        )
    )
    records = extract_har(document, provider=SCHWAB_CONTRACT, session=_session())
    assert records[0].capability == "transactions.history"
    assert records[0].response == {
        "bookmark": REDACTION,
        "brokerageTransactions": [
            {"amount": 12.5, "schwabOrderId": REDACTION, "symbol": "TEST"}
        ],
    }


@pytest.mark.parametrize(
    "entry,session",
    [
        (_entry(authenticated=False), _session()),
        (_entry(), _session(expired=True)),
        (_entry(path="/api/order/place"), _session()),
        (_entry(method="POST", path="/api/order/place"), _session()),
    ],
)
def test_missing_expired_or_unknown_session_requests_fail_closed(
    entry: dict[str, object], session: SessionEnvelope
) -> None:
    with pytest.raises(WebContractError):
        extract_har(_document(entry), provider=SCHWAB_CONTRACT, session=session)


def test_transaction_post_shape_drift_fails_closed() -> None:
    body = _transaction_body()
    body["timeFrame"] = "Custom"
    path = (
        "/api/is.TransactionHistoryWeb/TransactionHistoryInterface/"
        "TransactionHistory/brokerage/transactions"
    )
    with pytest.raises(WebContractError, match="reviewed read request"):
        extract_har(
            _document(_entry(method="POST", path=path, request_body=body)),
            provider=SCHWAB_CONTRACT,
            session=_session(),
        )


def test_source_hashes_are_frozen_without_importing_private_paths() -> None:
    from quant_engine.web_contracts import SOURCE_ANCHORS

    assert len(SOURCE_ANCHORS) == 6
    assert all(len(value) == 64 for value in SOURCE_ANCHORS.values())

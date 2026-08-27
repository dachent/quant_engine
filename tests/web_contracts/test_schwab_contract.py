from __future__ import annotations

import pytest

from quant_engine.web_contracts import SCHWAB_CONTRACT, WebContractError


_ACCOUNTS_URL = (
    "https://ausgateway.schwab.com/api/is.TradeOrderManagementWeb/v1/"
    "TradeOrderManagementWebPort/customer/accounts"
)
_TRANSACTIONS_URL = (
    "https://ausgateway.schwab.com/api/is.TransactionHistoryWeb/"
    "TransactionHistoryInterface/TransactionHistory/brokerage/transactions"
)


def _transaction_request() -> dict[str, object]:
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


def test_schwab_declarations_route_reviewed_read_only_endpoints() -> None:
    accounts = SCHWAB_CONTRACT.route("GET", _ACCOUNTS_URL)
    transactions = SCHWAB_CONTRACT.route("POST", _TRANSACTIONS_URL)

    assert accounts.capability == "accounts.discover"
    assert transactions.capability == "transactions.history"
    assert accounts.semantic_effect.value == "READ_ONLY"
    assert transactions.validate_request is not None
    transactions.validate_request(_transaction_request())


def test_schwab_transaction_declaration_rejects_shape_drift() -> None:
    endpoint = SCHWAB_CONTRACT.route("POST", _TRANSACTIONS_URL)
    request = _transaction_request()
    request["timeFrame"] = "Custom"

    assert endpoint.validate_request is not None
    with pytest.raises(WebContractError, match="reviewed read request"):
        endpoint.validate_request(request)


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", _ACCOUNTS_URL),
        ("GET", "https://ausgateway.schwab.com/api/order/place"),
    ],
)
def test_schwab_declarations_reject_unreviewed_routes(method: str, url: str) -> None:
    with pytest.raises(WebContractError):
        SCHWAB_CONTRACT.route(method, url)

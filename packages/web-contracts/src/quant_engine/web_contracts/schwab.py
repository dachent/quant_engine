"""Reviewed Schwab web contracts proven by the historical extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)


_ORIGIN = "https://ausgateway.schwab.com"
_TRANSACTION_TYPES = frozenset(
    {
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
    }
)
_TRANSACTION_KEYS = {
    "accountNickname",
    "bookmark",
    "exportType",
    "includeOptionsInSearch",
    "isSpsLinkedUkAccount",
    "selectedAccountId",
    "selectedTransactionTypes",
    "shouldPaginate",
    "sortColumn",
    "sortDirection",
    "symbol",
    "timeFrame",
}


def _transaction_query(value: Mapping[str, Any]) -> None:
    if set(value) != _TRANSACTION_KEYS:
        raise WebContractError("transaction query keys differ from the reviewed shape")
    selected = value.get("selectedTransactionTypes")
    if not isinstance(selected, list) or not all(
        isinstance(item, str) for item in selected
    ):
        raise WebContractError("transaction types must be text")
    if frozenset(selected) != _TRANSACTION_TYPES or len(selected) != len(
        _TRANSACTION_TYPES
    ):
        raise WebContractError(
            "transaction query must request the complete reviewed set"
        )
    required = {
        "exportType": "Csv",
        "includeOptionsInSearch": False,
        "isSpsLinkedUkAccount": False,
        "shouldPaginate": True,
        "sortColumn": "Date",
        "sortDirection": "Descending",
        "symbol": "",
        "timeFrame": "All",
    }
    if any(value.get(key) != expected for key, expected in required.items()):
        raise WebContractError(
            "transaction query differs from the reviewed read request"
        )
    for key in ("accountNickname", "selectedAccountId"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise WebContractError("transaction query account scope is missing")
    bookmark = value.get("bookmark")
    if bookmark is not None and not isinstance(bookmark, str):
        raise WebContractError("transaction bookmark must be text or null")


SCHWAB_CONTRACT = ProviderContract(
    provider="schwab",
    endpoints=(
        EndpointContract(
            capability="accounts.discover",
            method="GET",
            origin=_ORIGIN,
            path=(
                "/api/is.TradeOrderManagementWeb/v1/"
                "TradeOrderManagementWebPort/customer/accounts"
            ),
            semantic_effect=SemanticEffect.READ_ONLY,
        ),
        EndpointContract(
            capability="balances.historical",
            method="GET",
            origin=_ORIGIN,
            path="/api/is.Balances/V1/Balances/balances/historical",
            semantic_effect=SemanticEffect.READ_ONLY,
        ),
        EndpointContract(
            capability="transactions.history",
            method="POST",
            origin=_ORIGIN,
            path=(
                "/api/is.TransactionHistoryWeb/TransactionHistoryInterface/"
                "TransactionHistory/brokerage/transactions"
            ),
            semantic_effect=SemanticEffect.READ_ONLY,
            validate_request=_transaction_query,
        ),
    ),
    sensitive_fields=frozenset(
        {
            "accountid",
            "accountnickname",
            "accountnumber",
            "bookmark",
            "brokerageaccountid",
            "cusipid",
            "nickname",
            "selectedaccountid",
            "schwaborderid",
        }
    ),
)

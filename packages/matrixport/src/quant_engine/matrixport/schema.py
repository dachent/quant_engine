from __future__ import annotations


DATASET_DESCRIPTIONS: dict[str, str] = {
    "endpoint_catalog": "One row per documented endpoint and execution disposition.",
    "request_manifest": "One row per attempted API request/page/window with sanitized metadata.",
    "products": "One current product-detail record per discovered product.",
    "product_catalog": "One visible catalog product snapshot.",
    "holdings": "One current Strategy holding per product.",
    "portfolio_snapshots": "Current account-level Strategy holding summary.",
    "strategy_trades": "Subscription and redemption history records.",
    "subscription_details": "One confirmed subscription detail per order.",
    "redemption_details": "One settled or pending redemption detail per redemption.",
    "reservations": "Historical Strategy reservation records.",
    "nav_daily": "Normal product NAV observations.",
    "nav_realtime": "Real-time product NAV observations.",
    "btc_performance": "BTC performance and documented conversion-rate observations.",
    "product_statistics": "Product return, ranking, benchmark, and risk statistics.",
    "product_windows": "Current subscription and redemption window details.",
    "agreements": "Product and risk agreement content.",
    "wallet_balances": "Current account wallet balances.",
    "wallet_currencies": "Currency and network metadata.",
    "wallet_bills": "Wallet accounting ledger entries.",
    "wallet_deposits": "Deposit history across all enumerated currencies.",
    "wallet_withdrawals": "Withdrawal history across all enumerated currencies.",
    "wallet_currency_dispositions": "Terminal provider-structured parameter rejections by wallet currency and dataset.",
    "rfq_symbols": "RFQ symbol metadata.",
    "rfq_orders": "RFQ historical orders across all enumerated symbols.",
    "prime_accounts": "Prime Broker account enumeration.",
    "position_events": "Confirmed share additions and settled share reductions.",
    "daily_holdings": "Reconstructed per-product daily holdings and valuation.",
    "daily_portfolio": "Per-currency and USD portfolio daily totals with coverage.",
    "coverage": "Endpoint requested/observed history bounds and status.",
    "quality_checks": "Machine-readable extraction and reconciliation checks.",
    "schema_drift": "Unexpected or newly observed API fields.",
    "data_dictionary": "Dataset and column definitions.",
}


STRATEGY_WORKBOOK_DATASETS = (
    "daily_holdings",
    "daily_portfolio",
    "holdings",
    "portfolio_snapshots",
    "products",
    "product_catalog",
    "product_statistics",
    "nav_daily",
    "nav_realtime",
    "strategy_trades",
    "subscription_details",
    "redemption_details",
    "reservations",
    "product_windows",
    "agreements",
    "position_events",
)


ACCOUNT_WORKBOOK_DATASETS = (
    "wallet_balances",
    "wallet_currencies",
    "wallet_bills",
    "wallet_deposits",
    "wallet_withdrawals",
    "rfq_symbols",
    "rfq_orders",
    "prime_accounts",
)


AUDIT_WORKBOOK_DATASETS = (
    "endpoint_catalog",
    "request_manifest",
    "coverage",
    "quality_checks",
    "schema_drift",
    "data_dictionary",
    "wallet_currency_dispositions",
)

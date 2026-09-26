#!/usr/bin/env bash
# Rebuild the layered-history candidate against the corrected provider ledger,
# the union Portfolio123 generation, and the fixed level transform.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"

LEDGER="C:/Dev/scratch/quant-engine/pl12/provider-ledger"
LEDGER_GEN="59d04004d44a30ed912afc95350d22810137689ac2525dca85704b7297664a34"
MX="$Q/dataMatrixport"
MX_GEN="3badac0513e74e6b57a4f8cd1b4b30fe834ddffc1c372515af23a4e3225d9ccc"
MX_CSV="$MX/output/20260903T051029.331Z/csv"
QB="$Q/dataQuantbase"
QB_GEN="d3a8f2da5a02b84fcc016981c04e4b8fec501cb902c019cfb8f9dc80e6b2d1fe"
QB_CSV="$QB/source-runs/legacy-20260902-b1b5dcff331f/artifacts/csv"
P123="$Q/dataPortfolio123"
P123_GEN="80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8"
P123_TABLES="$P123/generations/$P123_GEN/tables"
# The accepted p123-eu-provider-live receipt is frozen on the 2026-08-14
# generation, so its three managed tables are read from there.
P123_HISTORY_GEN="10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1"
P123_HISTORY_TABLES="$P123/generations/$P123_HISTORY_GEN/tables"
OUT="C:/Dev/scratch/quant-engine/pl12/layered-history-r2"

cd "$REPO"
SHA="$(git rev-parse HEAD)"
mkdir -p "$OUT"

PYTHONIOENCODING=utf-8 "$PY" tools/portfolio_target_series.py \
  --layered-history-generation \
  --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json \
  --repository-sha "$SHA" \
  --as-of-date 2026-09-04 \
  --provider-ledger-current "$LEDGER/CURRENT.json" \
  --provider-ledger-manifest "$LEDGER/generations/$LEDGER_GEN/generation.json" \
  --provider-ledger-catalog "$LEDGER/generations/$LEDGER_GEN/provider-ledger.json" \
  --matrixport-current "$MX/CURRENT.json" \
  --manifest "$MX/generations/$MX_GEN/generation.json" \
  --nav "$MX_CSV/nav_daily.csv" \
  --holdings "$MX_CSV/holdings.csv" \
  --daily-holdings "$MX_CSV/daily_holdings.csv" \
  --subscriptions "$MX_CSV/subscription_details.csv" \
  --product-id 7361599360326447104 \
  --quantbase-current "$QB/generations/$QB_GEN/CURRENT.json" \
  --quantbase-manifest "$QB/generations/$QB_GEN/generation.json" \
  --quantbase-history "$QB_CSV/dashboard_Volatis_Strategy_pricehistory.csv" \
  --quantbase-activity "$QB_CSV/account_activity.csv" \
  --quantbase-fund-id 107 \
  --portfolio123-current "$P123/CURRENT.json" \
  --portfolio123-manifest "$P123/generations/$P123_GEN/generation.json" \
  --portfolio123-metadata "$P123_HISTORY_TABLES/har_managed_metadata.parquet" \
  --portfolio123-performance "$P123_HISTORY_TABLES/har_managed_performance.parquet" \
  --portfolio123-equity "$P123_HISTORY_TABLES/har_equity_curve.parquet" \
  --portfolio123-returns "$P123_TABLES/returns_daily.parquet" \
  --portfolio123-relationships "$P123_TABLES/strategy_relationships.parquet" \
  --portfolio-fx-rates-manifest "$Q/dataPortfolio/portfolio-fx-rates/generations/9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json" \
  --output-root "$OUT"

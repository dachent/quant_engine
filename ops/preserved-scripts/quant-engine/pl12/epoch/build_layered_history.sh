#!/usr/bin/env bash
# Rebuild the layered history with the retained strategy-history bundle bound.
#
# `--strategy-history-manifest` / `--strategy-history-returns` are what carry
# the backtest prehistory; without them the builder emits ACCOUNT_LIVE segments
# only, which is why every strategy read REQUIRED_NOT_READY under
# LIVE_PLUS_BACKTEST.
#
# Set PROBE_P123_VERSION=1 to print the live/simulation twin version the
# Portfolio123 route derives and exit before writing anything. That value is
# what the contract's p123-ai-factor strategy_version has to equal for its
# prehistory to be admitted.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
LIVE="$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
LH_ROOT="$LIVE/layered-history"
HISTORY_BUNDLE="${HISTORY_BUNDLE:-C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio/source-runs/strategy-history-mvp2-20260911/ff99e1b3095057fd2438f6b4085cdd803bd25c88d3f8955776581c7a8e9a551b}"

BACKTESTS_ROOT="$Q/dataStrategyBacktests"
BACKTESTS_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$BACKTESTS_ROOT/CURRENT.json")"
BACKTESTS_GEN="$BACKTESTS_ROOT/generations/$BACKTESTS_ID"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8
SHA="$(git rev-parse HEAD)"

PINNED_LEDGER_ROOT="$LIVE/provider-ledger"
PINNED_LEDGER_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$PINNED_LEDGER_ROOT/CURRENT.json")"
PINNED_LEDGER_GEN="$PINNED_LEDGER_ROOT/generations/$PINNED_LEDGER_ID"
MX="$Q/dataMatrixport"
MX_GEN="3badac0513e74e6b57a4f8cd1b4b30fe834ddffc1c372515af23a4e3225d9ccc"
MX_CSV="$MX/output/20260903T051029.331Z/csv"
QB="$Q/audit/coding/2026-09-06_codex_mvp2-execution/source-integration-r6/quantbase-private"
QB_GEN="d3a8f2da5a02b84fcc016981c04e4b8fec501cb902c019cfb8f9dc80e6b2d1fe"
QB_CSV="$QB/source-runs/legacy-20260902-b1b5dcff331f/artifacts/csv"
P123="$Q/dataPortfolio123"
P123_GEN="80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8"
P123_TABLES="$P123/generations/$P123_GEN/tables"
P123_HISTORY_TABLES="$P123/generations/10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1/tables"

args=(
  --layered-history-generation
  --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json
  --repository-sha "$SHA"
  --as-of-date 2026-09-04
  --provider-ledger-current "$PINNED_LEDGER_ROOT/CURRENT.json"
  --provider-ledger-manifest "$PINNED_LEDGER_GEN/generation.json"
  --provider-ledger-catalog "$PINNED_LEDGER_GEN/provider-ledger.json"
  --matrixport-current "$MX/CURRENT.json"
  --manifest "$MX/generations/$MX_GEN/generation.json"
  --nav "$MX_CSV/nav_daily.csv"
  --holdings "$MX_CSV/holdings.csv"
  --daily-holdings "$MX_CSV/daily_holdings.csv"
  --subscriptions "$MX_CSV/subscription_details.csv"
  --product-id 7361599360326447104
  --quantbase-current "$QB/CURRENT.json"
  --quantbase-manifest "$QB/generations/$QB_GEN/generation.json"
  --quantbase-history "$QB_CSV/dashboard_Volatis_Strategy_pricehistory.csv"
  --quantbase-activity "$QB_CSV/account_activity.csv"
  --quantbase-fund-id 107
  --portfolio123-current "$P123/CURRENT.json"
  --portfolio123-manifest "$P123/generations/$P123_GEN/generation.json"
  --portfolio123-metadata "$P123_HISTORY_TABLES/har_managed_metadata.parquet"
  --portfolio123-performance "$P123_HISTORY_TABLES/har_managed_performance.parquet"
  --portfolio123-equity "$P123_HISTORY_TABLES/har_equity_curve.parquet"
  --portfolio123-returns "$P123_TABLES/returns_daily.parquet"
  --portfolio123-relationships "$P123_TABLES/strategy_relationships.parquet"
  --portfolio-fx-rates-manifest "$Q/dataPortfolio/portfolio-fx-rates/generations/9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json"
  --strategy-history-manifest "$HISTORY_BUNDLE/manifest.json"
  --strategy-history-returns "$HISTORY_BUNDLE/returns.csv"
  --strategy-backtests-generation "$BACKTESTS_GEN/generation.json"
  --output-root "$LH_ROOT"
)

if [ "${PROBE_P123_VERSION:-0}" = "1" ]; then
  exec "$PY" "$W/epoch/probe_p123_version.py" "${args[@]}"
fi

if [ -n "${DUMP_SEGMENT_IDENTITIES:-}" ]; then
  exec "$PY" "$W/epoch/dump_segment_identities.py" "$DUMP_SEGMENT_IDENTITIES" "${args[@]}"
fi

"$PY" tools/portfolio_target_series.py "${args[@]}"

#!/usr/bin/env bash
# Rebuild the retained strategy-history bundle from the vendor artifacts.
#
# The bundle the 2026-08-16 release used (50761fad...) was lost with a deleted
# worktree; its rows survive only inside
# dataPortfolio/source-runs/refresh-20260814/layered-history-migrated/returns.csv.
# These are the same vendor sources, re-read, so the bundle is rebuilt rather
# than copied from a derived artifact.
#
# Source selection is by evidence, not filename:
#   * TT needs sheets `Index` and `raw_ttdata`; only "Strategies Data byAPI"
#     has both.
#   * Grizzly needs `Simulation`/`Summary`. SPXL is the current implementation,
#     SPX and SPY are the comparative research lineage, and
#     `VIX-TA-Macro Advanced.xlsx` is the original futures design.
#   * QSI's 2013-2024 weekly-return workbook exists only in the 2025-12-20
#     archive. The newer archives carry weeks whose price evidence ends
#     2025-12-19 in the retained price root, so they are not admitted here.
#
# AS_OF is the retained-source boundary. Each series is then truncated strictly
# before its OWN account-live inception, which is what the layered-history
# consumer requires; the inception dates are the contract census's
# `funded_membership.effective_start`, not literals chosen here. A single
# global as-of would discard weeks that are admissible for the later-starting
# strategies -- QSI alone lost four.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
S="C:/Users/BorisVaisman/V Capital/investing - Documents/Strategies"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
OUT="${HISTORY_OUT:-$Q/dataPortfolio/source-runs/strategy-history-mvp2-20260911}"
AS_OF="${HISTORY_AS_OF:-2026-09-04}"

GB="$S/GrizzlyBulls"
QSI="$S/Quanta72 Stratum Index"
TT="$S/TuringTrader"
VOLATIS="$Q/audit/coding/2026-09-06_codex_mvp2-execution/source-integration-r6/quantbase-private/source-runs/legacy-20260902-b1b5dcff331f/artifacts/csv/dashboard_Volatis_Strategy_pricehistory.csv"
P123_ROOT="$Q/dataPortfolio123/generations/80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

# Read each inception from the contract census so the boundary cannot drift
# from the product authority.
readarray -t INCEPTIONS < <("$PY" -c "
import json
contract = json.load(open('contracts/portfolio-financial-mvp-source-v1.json', encoding='utf-8'))
plan = json.load(open('docs/analytics/current-strategy-decomposition-plan.json', encoding='utf-8'))
series = {row['strategy_id']: row['series_id'] for row in plan['strategies']}
for row in contract['strategies']:
    start = (row.get('funded_membership') or {}).get('effective_start')
    name = series.get(row['strategy_id'])
    if start and name:
        print('%s=%s' % (name, start))
")

ROBUXIO_ROOT="$Q/dataProviderTrackRecord"
ROBUXIO_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$ROBUXIO_ROOT/CURRENT.json")"
ROBUXIO_GEN="$ROBUXIO_ROOT/generations/$ROBUXIO_ID"
ROBUXIO_VERSION="$("$PY" -c "
import json
contract = json.load(open('contracts/portfolio-financial-mvp-source-v1.json', encoding='utf-8'))
row = next(r for r in contract['strategies'] if r['strategy_id'] == 'robuxio-crypto')
print(row['strategy_version']['version_ref'])
")"

# The EU designer model publishes its own curve from 2008; the retained P123
# generation only holds the subscribed copy, whose curve starts when the copy
# was created. The designer capture is its own governed generation so the P123
# generation id is untouched.
EU_GEN="$ROBUXIO_ROOT/generations/cffc1db247ea0aa6e74a0637580bdbaa854a9466ba365b0ed9fe8f74afd81ede"
EU_VERSION="$("$PY" -c "
import json
contract = json.load(open('contracts/portfolio-financial-mvp-source-v1.json', encoding='utf-8'))
row = next(r for r in contract['strategies'] if r['strategy_id'] == 'p123-eu')
print(row['strategy_version']['version_ref'])
")"

args=()
for item in "${INCEPTIONS[@]}"; do
  args+=(--live-inception "$item")
done

"$PY" tools/portfolio_strategy_history.py \
  --grizzly-original-design-workbook "$GB/VIX-TA-Macro Advanced.xlsx" \
  --grizzly-spx-workbook "$GB/GB VIX-TA-Macro Advanced using SPX.xlsx" \
  --grizzly-spy-workbook "$GB/GB VIX-TA-Macro Advanced using SPY.xlsx" \
  --grizzly-workbook "$GB/GB VIX-TA-Macro Advanced using SPXL.xlsx" \
  --tt-workbook "$TT/TuringTrader Strategies Data byAPI.xlsx" \
  --qsi-subscription-archive "$QSI/Quanta 72 Paid Sub Files-20251220T024343Z-1-001.zip" \
  --qsi-appendix-workbook "$QSI/Analysis/Stratum_Master_Appendix_v3.xlsx" \
  --qsi-price-root "$QSI/stratum_yf_data" \
  --volatis-pricehistory-csv "$VOLATIS" \
  --portfolio123-generation-root "$P123_ROOT" \
  --robuxio-track-record-generation "$ROBUXIO_GEN" \
  --robuxio-strategy-version "$ROBUXIO_VERSION"   --p123-eu-designer-model-generation "$EU_GEN"   --p123-eu-strategy-version "$EU_VERSION" \
  --as-of-date "$AS_OF" \
  "${args[@]}" \
  --output-root "$OUT"

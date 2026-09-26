#!/usr/bin/env bash
# Rebuild the strategy-history bundle with the EU designer curve, off to the side.
#
# The vertical build still holds the repository at its release commit, so the
# builder is copied to scratch, patched there, and run against the real retained
# sources into a scratch output root. Nothing in the repository or in any
# published store is touched; only the bundle that comes out is inspected.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
S="C:/Users/BorisVaisman/V Capital/investing - Documents/Strategies"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
PEND="C:/Dev/scratch/quant-engine/pl12/epoch/pending"
OUT="C:/Dev/scratch/quant-engine/pl12/epoch/eu-history-dryrun"
AS_OF="2026-09-04"

GB="$S/GrizzlyBulls"
QSI="$S/Quanta72 Stratum Index"
TT="$S/TuringTrader"
VOLATIS="$Q/dataQuantbase/source-runs/legacy-20260902-b1b5dcff331f/artifacts/csv/dashboard_Volatis_Strategy_pricehistory.csv"
P123_ROOT="$Q/dataPortfolio123/generations/80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8"
EU_GEN="$Q/dataProviderTrackRecord/generations/cffc1db247ea0aa6e74a0637580bdbaa854a9466ba365b0ed9fe8f74afd81ede"

mkdir -p "$PEND/tools"
cp "$REPO/tools/portfolio_strategy_history.py" "$PEND/tools/portfolio_strategy_history.py"
PATCH_TARGET="$PEND/tools/portfolio_strategy_history.py" \
  "$PY" "C:/Dev/scratch/quant-engine/pl12/epoch/wire_eu_designer_curve.py"

export PYTHONPATH="$REPO/packages/core/src;$REPO"
export PYTHONIOENCODING=utf-8
cd "$REPO"

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
versions="$("$PY" -c "
import json
contract = json.load(open('contracts/portfolio-financial-mvp-source-v1.json', encoding='utf-8'))
by_id = {row['strategy_id']: row for row in contract['strategies']}
for key in ('robuxio-crypto', 'p123-eu'):
    print(by_id[key]['strategy_version']['version_ref'])
")"
ROBUXIO_VERSION="$(printf '%s\n' "$versions" | sed -n 1p)"
EU_VERSION="$(printf '%s\n' "$versions" | sed -n 2p)"
echo "robuxio version: $ROBUXIO_VERSION"
echo "eu version     : $EU_VERSION"

args=()
for item in "${INCEPTIONS[@]}"; do
  args+=(--live-inception "$item")
done
printf 'inception: %s\n' "${INCEPTIONS[@]}"

"$PY" "$PEND/tools/portfolio_strategy_history.py" \
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
  --robuxio-strategy-version "$ROBUXIO_VERSION" \
  --p123-eu-designer-model-generation "$EU_GEN" \
  --p123-eu-strategy-version "$EU_VERSION" \
  --as-of-date "$AS_OF" \
  "${args[@]}" \
  --output-root "$OUT"

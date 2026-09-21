#!/usr/bin/env bash
# Build the LIVE_PLUS_BACKTEST decomposition plan bundle from the same inputs
# the LIVE_ONLY bundle used. The only difference is --evidence-mode.
#
# A strategy whose prehistory is not version-matched is not a blocker here:
# `portfolio_current_decomposition_plan.py` carries an explicit
# `no_matched_prehistory` fallback that uses that strategy's live-only segments
# inside the hybrid mode, provided its LIVE_ONLY status is AVAILABLE. That is
# what the 2026-08-16 production release did for p123-eu and robuxio-crypto.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
E="$W/epoch"
LIVE="$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
PRIOR_BUNDLE="$Q/dataPortfolio/source-runs/anl-decomp-current-plan-mvp2-20260910/6914b0557a64c13b455aebd68721cdcd5a6eb25a7893d07c5e9c1d4592b86f0d"
PLAN_ROOT="$Q/dataPortfolio/source-runs/anl-decomp-hybrid-plan-mvp2-20260911"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

HISTORY_ID="$("$PY" -c "import json;print(json.load(open('contracts/portfolio-dependence-policy-v1.json',encoding='utf-8'))['history_authority']['generation_id'])")"
echo "layered history $HISTORY_ID"

"$PY" tools/portfolio_current_decomposition_plan.py \
  --plan docs/analytics/current-strategy-decomposition-plan.json \
  --catalog docs/analytics/decomposition-source-catalog.json \
  --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json \
  --layered-history-generation "$LIVE/layered-history/$HISTORY_ID" \
  --base-spec "$W/base-spec/base-decomposition-spec.json" \
  --base-source-root "$E/base-source" \
  --us-factors "$PRIOR_BUNDLE/inputs/market/us_factors.csv" \
  --europe-factors "$PRIOR_BUNDLE/inputs/market/europe_factors.csv" \
  --style-returns "$PRIOR_BUNDLE/inputs/market/style_returns.csv" \
  --sector-returns "$PRIOR_BUNDLE/inputs/market/sector_returns.csv" \
  --us-industries "$PRIOR_BUNDLE/inputs/market/us_industries.csv" \
  --public-factor-manifest "$PRIOR_BUNDLE/inputs/governance/public-factor-source-manifest.json" \
  --style-manifest "$PRIOR_BUNDLE/inputs/governance/style-source-manifest.json" \
  --sector-manifest "$PRIOR_BUNDLE/inputs/governance/sector-source-manifest.json" \
  --evidence-mode LIVE_PLUS_BACKTEST \
  --evaluation-date 2026-09-04 \
  --assurance-profile RELEASE_QC \
  --output-root "$PLAN_ROOT"

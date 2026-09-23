#!/usr/bin/env bash
# Re-stage the consuming roles with the governed PORT-HWM-4 and INVESTED_CAPITAL
# results bound. The ledger, baseline, attribution and analytics roles do not
# read them, so their staged generations are reused unchanged; report, workbook
# and panel-candidate do, and their identities move.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
OUT="$W/proof2"
CYCLE="$W/cycle2"
HWM="$W/hwm"
CAP="$W/capital"
# The admission receipt belongs to the ledger THIS cycle staged; a literal
# here silently binds a past one.
LEDGER_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$CYCLE/stage-ledger.json")"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

governed=(
  --hwm-governed-result "$HWM/as-held-composite-hwm.json"
  --hwm-governed-input-generation "$HWM/hwm-input-generation.json"
  --hwm-governed-calendar-authority "$HWM/calendar-authority.json"
  --hwm-governed-admission-receipt "$W/proof2/stages/ledger/generations/$LEDGER_ID/admit-or-declare-receipt.json"
  --invested-capital-result "$CAP/invested-capital.json"
  --invested-capital-input-generation "$CAP/capital-input-generation.json"
)

for role in report workbook panel-candidate; do
  echo "== stage $role =="
  "$PY" tools/portfolio_release_builder.py stage \
    --recipe "$CYCLE/vertical-recipe.json" --output-root "$OUT" --role "$role" \
    --qa-dir "$W/qa2" --portfolio-root "$Q/dataPortfolio" \
    "${governed[@]}" \
    > "$CYCLE/stage-$role.json"
  cat "$CYCLE/stage-$role.json"
done

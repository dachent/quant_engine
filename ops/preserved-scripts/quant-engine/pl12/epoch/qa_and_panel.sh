#!/usr/bin/env bash
# Browser and interaction QA against the emitted panel candidate, then the
# panel role. The QA output must live outside the candidate directory: the
# panel-candidate checkpoint records that directory's exact file inventory,
# so writing `qa/` inside it makes the panel stage reject its own candidate.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
OUT="$W/proof2"
CYCLE="$W/cycle2"
QA="$W/qa2"
export PLAYWRIGHT_MODULE="$Q/audit/coding/2026-09-03_kimi_w2-phasec/_qa-runtime/node_modules/playwright/index.mjs"
export PLAYWRIGHT_EXECUTABLE_PATH="C:/Users/BorisVaisman/AppData/Local/ms-playwright/chromium-1237/chrome-win64/chrome.exe"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

json_field() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))[sys.argv[2]])" "$1" "$2"; }

CANDIDATE="$(json_field "$CYCLE/stage-panel-candidate.json" candidate_dir)"
# The admission receipt belongs to the ledger THIS cycle staged. Naming a past
# cycle's generation here passes its own file check and then contradicts the
# recipe several steps later, so it is read from the stage result instead.
LEDGER_ID="$(json_field "$CYCLE/stage-ledger.json" generation_id)"
LEDGER_RECEIPT="$OUT/stages/ledger/generations/$LEDGER_ID/admit-or-declare-receipt.json"
echo "ledger    $LEDGER_ID"
CUTOFF="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['calculation_cutoff_date'])" "$W/epoch/current-epoch-crosswalk.json")"
echo "candidate $CANDIDATE"
echo "cutoff    $CUTOFF"

rm -rf "$QA"
mkdir -p "$QA"

echo "== browser QA =="
node apps/portfolio-control-panel/browser_qa.mjs \
  --html "$CANDIDATE/index.html" --output "$QA" \
  --expected-calculation-cutoff "$CUTOFF"

echo "== interaction QA =="
node apps/portfolio-control-panel/interaction_qa.mjs \
  --html "$CANDIDATE/index.html" \
  --output "$QA/interaction-results.json" \
  --expected-calculation-cutoff "$CUTOFF"

echo "== QA receipt =="
"$PY" tools/portfolio_control_panel.py qa \
  --candidate-dir "$CANDIDATE" --qa-dir "$QA" > "$CYCLE/qa-validation.json"
"$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print('all_passed', d['all_passed'], 'checks', len(d.get('checks',[])))
for c in d.get('checks',[]):
    if not c.get('passed', True):
        print('FAILED', c.get('check'))
" "$CYCLE/qa-validation.json"

echo "== stage panel =="
"$PY" tools/portfolio_release_builder.py stage \
  --recipe "$CYCLE/vertical-recipe.json" --output-root "$OUT" --role panel \
  --qa-dir "$QA" --portfolio-root "$Q/dataPortfolio" \
  --hwm-governed-result "$W/hwm/as-held-composite-hwm.json" \
  --hwm-governed-input-generation "$W/hwm/hwm-input-generation.json" \
  --hwm-governed-calendar-authority "$W/hwm/calendar-authority.json" \
  --hwm-governed-admission-receipt "$LEDGER_RECEIPT" \
  --invested-capital-result "$W/capital/invested-capital.json" \
  --invested-capital-input-generation "$W/capital/capital-input-generation.json" \
  > "$CYCLE/stage-panel.json"
"$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
for k in sorted(d):
    v=d[k]
    if not isinstance(v,(dict,list)):
        print(f'{k:44s} {str(v)[:90]}')
" "$CYCLE/stage-panel.json"

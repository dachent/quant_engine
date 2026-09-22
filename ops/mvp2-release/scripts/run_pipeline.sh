#!/usr/bin/env bash
# Materialize the pipeline from the staged vertical and run it to a prepared
# release. The roots are all under the scratch run root, so `--commit` here
# publishes a private proof and moves nothing in dataPortfolio.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
CA="$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only/correction-authority"
# The admission receipt belongs to the ledger THIS cycle staged; a literal
# here binds a past one, and after a restage that directory is simply gone.
LEDGER_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$W/cycle2/stage-ledger.json")"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

rm -rf "$W/run2" "$W/cycle2/pipeline-recipe.json" "$W/cycle2/pipeline-recipe-probe.json"
mkdir -p "$W/run2"

# One QA directory, shared with the proof stage, exactly as the runbook has
# it. `stage --role panel` writes semantic-validation.json into it, and the
# pipeline's own panel node writes that file again -- now byte-identical,
# because the report and panel identities no longer depend on the output
# root. So create-only passes and the recipe's pinned QA inventory still
# matches after the node runs. Stripping the file instead made the recipe pin
# an inventory the panel node then changed.
QA_PIPE="$W/qa2"

# The registries name the candidate generations the receipts bind, and those
# ids live in the pipeline recipe -- which will not be written without the
# registries. A probe recipe breaks the cycle: `expected_generations` is read
# from the proof root, not from the registries, so the probe and the real
# recipe agree on it by construction.
echo "== probe recipe for the candidate generation ids =="
"$PY" tools/portfolio_pipeline_materialize.py recipe   --vertical-recipe "$W/cycle2/vertical-recipe.json"   --proof-root "$W/proof2" --qa-dir "$QA_PIPE"   --pipeline-id mvp2-live-only-20260910-pipeline   --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json   --financial-mvp-source-matrix generated/financial-mvp/source-matrix.json   --output "$W/cycle2/pipeline-recipe-probe.json" | cut -c1-140

echo "== redraft the correction authority against the staged candidates =="
"$PY" ops/mvp2-release/scripts/draft_correction_registries.py \
  --run "$W/proof2" \
  --pipeline-recipe "$W/cycle2/pipeline-recipe-probe.json" \
  --history-store "$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only/layered-history" \
  --out "$CA" | tail -6

echo "== pipeline recipe =="
"$PY" tools/portfolio_pipeline_materialize.py recipe \
  --vertical-recipe "$W/cycle2/vertical-recipe.json" \
  --proof-root "$W/proof2" --qa-dir "$QA_PIPE" \
  --pipeline-id mvp2-live-only-20260910-pipeline \
  --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json \
  --financial-mvp-source-matrix generated/financial-mvp/source-matrix.json \
  --correction-registry "$CA/portfolio-correction-registry.json" \
  --rederive-receipt-registry "$CA/portfolio-rederive-receipt-registry.json" \
  --output "$W/cycle2/pipeline-recipe.json" | cut -c1-140

echo "== pipeline spec =="
"$PY" tools/portfolio_pipeline_materialize.py spec \
  --recipe "$W/cycle2/pipeline-recipe.json" --output-root "$W/run2" \
  --spec "$W/run2/pipeline-spec.json" \
  --portfolio-root "$Q/dataPortfolio" \
  --hwm-governed-result "$W/hwm/as-held-composite-hwm.json" \
  --hwm-governed-input-generation "$W/hwm/hwm-input-generation.json" \
  --hwm-governed-calendar-authority "$W/hwm/calendar-authority.json" \
  --hwm-governed-admission-receipt "$W/proof2/stages/ledger/generations/$LEDGER_ID/admit-or-declare-receipt.json" \
  --invested-capital-result "$W/capital/invested-capital.json" \
  --invested-capital-input-generation "$W/capital/capital-input-generation.json" | cut -c1-140

echo "== plan =="
"$PY" tools/data_pipeline.py plan --spec "$W/run2/pipeline-spec.json" > "$W/logs/plan.json"
"$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8')); f=d['financial_mvp']
print('nodes', d['node_count'], '| ready', f['ready_for_execution'], '| blocking', f['blocking_reasons'])
print('commit effects', len(d.get('commit_effects',[])))
for r in sorted(f.get('completion_blocking_reasons') or [])[:12]: print('   completion:', r)
" "$W/logs/plan.json"

echo "== execute =="
"$PY" tools/data_pipeline.py execute --spec "$W/run2/pipeline-spec.json" > "$W/logs/execute.json"
"$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print('release_state', d.get('release_state'), '| release_id', str(d.get('release_id'))[:16])
" "$W/logs/execute.json"

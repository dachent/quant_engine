#!/usr/bin/env bash
# Bind the verified private proof to the production root set, run release
# preflight QC, and (only when PUBLISH=1) commit the pointer move.
#
# Everything up to `execute --commit` is read-only with respect to
# dataPortfolio: `release-preflight` snapshots production before and after and
# fails if anything moved.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
P="$W/production"
# The production authority rebinds the provider generation to the copy the
# PIPELINE staged under run2, and compares the admission receipt by path as
# well as by digest -- so the receipt has to be that copy, not the proof
# stage it was built in. The bytes are identical either way.
# The admission receipt belongs to the ledger THIS cycle staged; a literal
# here binds a past one and the mismatch only surfaces at preflight.
LEDGER_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$W/cycle2/stage-ledger.json")"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8

# Every artifact below is a create-only output: the producer refuses to
# overwrite one whose bytes differ, which is what makes a re-run honest. A
# failed attempt therefore leaves files that block the next one, so this run's
# own outputs are cleared first. Nothing outside $P is touched, and
# dataPortfolio is never in scope here.
# Every artifact this script writes under $P is a create-only output: the
# producer refuses to overwrite one whose bytes differ, which is what makes a
# re-run honest. A failed attempt therefore leaves files that block the next
# one. Enumerating them by hand was wrong twice -- first
# financial-mvp-product-conformance.json, then production-post-build-semantic.json
# -- because the list has to be re-derived every time the producer grows an
# output. The whole directory is generated, so the whole directory is replaced;
# one generation is kept aside so a failed run is still inspectable.
rm -rf "$W/production-prev"
if [ -d "$P" ]; then
  mv "$P" "$W/production-prev"
fi
mkdir -p "$P"

echo "== financial artifact inventory =="
"$PY" ops/mvp2-release/scripts/build_financial_artifacts.py \
  --run "$W/proof2" \
  --cycle "$W/cycle2" \
  --out "$P/financial-artifacts.json"

# A generation an external agent rewrote after publication can never
# reproduce the inventory its release sealed, which blocks every successor
# at prepare. The pipeline admits exactly that under
# `predecessor-authority-repair`: it re-derives the drift itself and refuses
# a gate that does not match, or one whose new release reuses the drifted
# generation. Emitting it here keeps the evidence derived from disk rather
# than asserted.
echo "== predecessor authority repair gate =="
REPAIR_ARGS=()
if "$PY" "$W/epoch/emit_repair_gate.py"; then
  REPAIR_ARGS=(--predecessor-repair-gate "$P/predecessor-authority-repair.json")
else
  echo "no predecessor drift to admit"
fi

echo "== production specification =="
"$PY" tools/portfolio_pipeline_materialize.py production-spec \
  --recipe "$W/cycle2/pipeline-recipe.json" \
  --prepared-root "$W/run2" \
  --semantic-gate "$W/run2/evidence/post-build-semantic.json" \
  --portfolio-root "$Q/dataPortfolio" \
  --state-root "$P/state" \
  --spec "$P/production-spec.json" \
  --financial-artifacts "$P/financial-artifacts.json" \
  "${REPAIR_ARGS[@]}" \
  --hwm-governed-result "$W/hwm/as-held-composite-hwm.json" \
  --hwm-governed-input-generation "$W/hwm/hwm-input-generation.json" \
  --hwm-governed-calendar-authority "$W/hwm/calendar-authority.json" \
  --hwm-governed-admission-receipt "$W/run2/stages/ledger/generations/$LEDGER_ID/admit-or-declare-receipt.json"

echo "== release preflight QC =="
"$PY" tools/portfolio_qc.py --profile release-preflight \
  --spec "$P/production-spec.json" \
  --evidence-dir "$P/evidence" \
  --emit-commit-authorization "$P/commit-authorization.json"

echo "== plan =="
"$PY" tools/data_pipeline.py plan --spec "$P/production-spec.json" > "$P/plan.json"
"$PY" -c "
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8')); f=d.get('financial_mvp') or {}
print('nodes', d['node_count'], '| ready', f.get('ready_for_execution'), '| blocking', f.get('blocking_reasons'))
print('commit effects', len(d.get('commit_effects',[])))
" "$P/plan.json"

if [ "${PUBLISH:-0}" != "1" ]; then
  echo "== stopping before commit (set PUBLISH=1 to move the pointers) =="
  exit 0
fi

# #469/#326/#444, generalized: a detector already computes a signal that
# should stop publication (conformance blocking reasons, an applicable open
# correction, a stale retained pin) and nothing downstream refuses on it.
# `execute --commit` below reaches `commit_release`
# (packages/core/src/quant_engine/core/release.py), which now refuses -- for
# every caller, not only this script -- unless every target root in the
# release manifest has a clean validate_generation findings file for its own
# `proposed_generation_id`, expected at
# "<that root>/validation/<generation_id>/findings.json"
# (state/briefs/findings-contract.md; `evaluate_release_validation_findings`,
# packages/core/src/quant_engine/core/publication.py). Missing findings
# refuse too -- a validator that has not run yet is not evidence of
# readiness. There is deliberately no separate precondition step here: the
# generation identity that matters is the one in the release manifest being
# committed, not a spec-file hash invented by this script, so the check
# belongs at the sink that reads that manifest, not in a wrapper that has to
# guess it.

echo "== execute --commit =="
"$PY" tools/data_pipeline.py execute --spec "$P/production-spec.json" \
  --commit --commit-authorization "$P/commit-authorization.json" > "$P/execute.json"
cat "$P/execute.json"

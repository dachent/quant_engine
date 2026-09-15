#!/usr/bin/env bash
# Re-emit the schema-10 vertical recipe at the current HEAD without rebuilding
# any stage. `build_spec` requires the recipe's release_commit to equal HEAD,
# and two consecutive full rebuilds at different commits produced byte-identical
# generation ids, so the commit is recipe metadata and not stage input.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
CYCLE="$W/cycle2"
LIVE="$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
REPAIR="$Q/dataPortfolio/rollforward/2026-09-05-mvp2-execution/source-repair"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8
SHA="$(git rev-parse HEAD)"

field() { "$PY" -c "import json,sys;d=json.load(open(sys.argv[1],encoding='utf-8'));print(eval(sys.argv[2],{'d':d}))" "$1" "$2"; }

LEDGER_GEN="$(field "$CYCLE/vertical-recipe.json" "d['provider_ledger_generation']['path']")"
PLAN_BUNDLE="$(field "$CYCLE/vertical-recipe.json" "d['decomposition_plan_bundle']['path']")"
EPOCH_ROOT="$(field "$CYCLE/vertical-recipe.json" "d['epoch_manifest_generation']['runtime_root']")"
EPOCH_ID="$(field "$CYCLE/vertical-recipe.json" "d['epoch_manifest_generation']['generation_id']")"
DEP_SOURCE="$(field "$CYCLE/vertical-recipe.json" "d['correlation_source_root']['path']")"

echo "head        $SHA"
echo "ledger      $LEDGER_GEN"
echo "plan bundle $PLAN_BUNDLE"
echo "epoch       $EPOCH_ID"

rm -f "$CYCLE/vertical-recipe.json"
"$PY" tools/portfolio_release_builder.py recipe \
  --provider-ledger-generation "$LEDGER_GEN" \
  --decomposition-plan-bundle "$PLAN_BUNDLE" \
  --epoch-manifest-generation-root "$EPOCH_ROOT" \
  --epoch-manifest-generation-id "$EPOCH_ID" \
  --baseline-spec "$REPAIR/portfolio-baseline-spec-r6.json" \
  --base-factor-file "$LIVE/base-input/factors.csv" \
  --implementation-reconciliation "$Q/dataPortfolio/source-runs/port-underwrite-001-20260811/implementation-reconciliation.json" \
  --implementation-return-attribution "$Q/dataPortfolio/source-runs/port-underwrite-001-20260811/implementation-return-attribution.json" \
  --strategy-attribution-root "$Q/dataPortfolio/analytics/attribution" \
  --correlation-source-root "$DEP_SOURCE" \
  --legacy-panel "$Q/audit/quant/2026-08-10_strategy_allocation/model_harness_baseline/control-panel-qa/portfolio_control_panel" \
  --as-of-date 2026-09-04 \
  --pipeline-id mvp2-live-only-20260910 \
  --release-commit "$SHA" \
  --output "$CYCLE/vertical-recipe.json"
echo "recipe re-emitted at $SHA"

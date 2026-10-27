#!/usr/bin/env bash
# Rebuild the MVP-2 vertical end to end against whatever the code now
# materializes: contract pins -> dependence -> ledger + baseline -> current
# epoch manifest -> decomposition plan bundle -> schema-10 recipe -> report.
set -euo pipefail

REPO="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo"
Q="C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
PY="C:/Dev/scratch/quant-engine/.venv-mvp2/Scripts/python.exe"
W="C:/Dev/scratch/quant-engine/pl12"
E="$W/epoch"
LIVE="$Q/dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
REPAIR="$Q/dataPortfolio/rollforward/2026-09-05-mvp2-execution/source-repair"
R9="$REPAIR/epoch-inputs/five-role-r9"
RECEIPT_AUTHORITY="$E/receipts/authority/account-interval-retrieval-receipt-authority.json"
# The prior plan bundle supplies the market and manifest inputs unchanged; only
# its provider-ledger provenance has to move to the rebuilt ledger.
PRIOR_BUNDLE="$Q/dataPortfolio/source-runs/anl-decomp-current-plan-mvp2-20260910/6914b0557a64c13b455aebd68721cdcd5a6eb25a7893d07c5e9c1d4592b86f0d"
HISTORY="$LIVE/layered-history/df04a9d654f195592e4ba10124a582d35bb393b9bd5b12efa0e95bca26e213cc"
ALLOCATION="$Q/dataPortfolio/baseline/generations/20709bfd5f9a5846879854bf3166c11223a100cc62fcb13fe0b76040f881b542"

BACKTESTS_ROOT="$Q/dataStrategyBacktests"
BACKTESTS_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$BACKTESTS_ROOT/CURRENT.json")"
BACKTESTS_GEN="$BACKTESTS_ROOT/generations/$BACKTESTS_ID"
# The layered history builder refuses a strategy-history bundle the backtests
# generation was not staged from, and that generation names its own source:
# raw/strategy-history-manifest.json is a byte copy of that bundle's
# manifest.json, and carries its bundle_id. Deriving the path from it keeps the
# two in step; a literal here binds whichever bundle was current when it was
# written, and the mismatch only surfaces once the history actually rebuilds.
SH_ROOT="$Q/dataPortfolio/source-runs/strategy-history-mvp2-20260911"
HISTORY_BUNDLE="${HISTORY_BUNDLE:-$SH_ROOT/$("$PY" -c "
import hashlib, json, pathlib, sys
raw = pathlib.Path(sys.argv[1]) / 'raw' / 'strategy-history-manifest.json'
content = raw.read_bytes()
bundle_id = json.loads(content)['bundle_id']
manifest = pathlib.Path(sys.argv[2]) / bundle_id / 'manifest.json'
if hashlib.sha256(manifest.read_bytes()).digest() != hashlib.sha256(content).digest():
    raise SystemExit('strategy-history bundle manifest differs from the staged copy')
print(bundle_id)
" "$BACKTESTS_GEN" "$SH_ROOT")}"

cd "$REPO"
export PYTHONPATH=.
export PYTHONIOENCODING=utf-8
SHA="$(git rev-parse HEAD)"
OUT="$W/proof2"
CYCLE="$W/cycle2"
OUT_CAPITAL="$W/capital"
# The decomposition solver's output root is content-addressed, and every index
# hit is re-verified against the generation's own manifest before it is served
# -- a truncated or edited artifact re-solves. So it can be carried across a
# clean rebuild without weakening anything. Nothing else survives the wipe.
#
# This matters because the release commit reaches only the recipe files and the
# panel stage result; no generation id depends on it. Rebuilding at a new
# commit therefore re-derives byte-identical lens specs, and without the cache
# it would re-solve 24 lenses whose inputs did not move.
DECOMP_CACHE="$W/decomp-cache"
if [ -d "$OUT/stages/analytics/decomposition-lenses" ]; then
  rm -rf "$DECOMP_CACHE"
  mkdir -p "$DECOMP_CACHE"
  mv "$OUT/stages/analytics/decomposition" "$DECOMP_CACHE/decomposition"
  mv "$OUT/stages/analytics/decomposition-lenses" "$DECOMP_CACHE/decomposition-lenses"
fi
rm -rf "$CYCLE" "$OUT"
mkdir -p "$CYCLE"
if [ -d "$DECOMP_CACHE/decomposition-lenses" ]; then
  echo "== restoring the decomposition solver cache =="
  mkdir -p "$OUT/stages/analytics"
  cp -r "$DECOMP_CACHE/decomposition" "$OUT/stages/analytics/decomposition"
  cp -r "$DECOMP_CACHE/decomposition-lenses" "$OUT/stages/analytics/decomposition-lenses"
fi

json_field() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))[sys.argv[2]])" "$1" "$2"; }

echo "== resync the contract pins onto what the code now materializes =="
RELEASE_SPEC="$LIVE/config/provider-ledger-spec-release.json"
"$PY" "$E/sync_contract_pins.py" "$RELEASE_SPEC" "$RELEASE_SPEC"

# #444 (GOV-CONSUMER-PROPAGATION-001): a contract/schema bump with no matching
# refresh of the artifacts that pin it previously ran four stages before
# surfacing (commit 47120f1). The resync above is the fix; this proves it
# actually converged instead of trusting that it did. Exit 1 aborts the build
# under `set -euo pipefail` -- this is a hard precondition, not advisory.
echo "== verify no retained-authority pin under \$LIVE is stale after resync (#444) =="
"$PY" tools/check_retained_authority_pins.py "$LIVE" --max-depth 6

# The spec recipe validates a dependence source, and the ledger it stages is
# what the layered history is rebuilt from, so this first pass reuses the
# layered history the policy already pins. The final pass below rebuilds both.
echo "== rebuild the dependence source against the current contract =="
DEP_ROOT="$LIVE/dependence"
PINNED_HISTORY_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['history_authority']['generation_id'])" contracts/portfolio-dependence-policy-v1.json)"
HISTORY="$LIVE/layered-history/$PINNED_HISTORY_ID"
"$PY" tools/portfolio_current_dependence.py build \
  --contract contracts/portfolio-financial-mvp-source-v1.json \
  --policy contracts/portfolio-dependence-policy-v1.json \
  --history-generation "$HISTORY" \
  --allocation-generation "$ALLOCATION" \
  --evaluated-on 2026-09-04 \
  --output-root "$DEP_ROOT" > "$CYCLE/dependence.json"
DEP_ID="$(json_field "$CYCLE/dependence.json" bundle_id)"
echo "dependence $DEP_ID"

recipe_common=(
  --baseline-spec "$REPAIR/portfolio-baseline-spec-r6.json"
  --base-factor-file "$LIVE/base-input/factors.csv"
  --implementation-reconciliation "$Q/dataPortfolio/source-runs/port-underwrite-001-20260811/implementation-reconciliation.json"
  --implementation-return-attribution "$Q/dataPortfolio/source-runs/port-underwrite-001-20260811/implementation-return-attribution.json"
  --strategy-attribution-root "$Q/dataPortfolio/analytics/attribution"
  --correlation-source-root "$DEP_ROOT/$DEP_ID"
  --legacy-panel "$Q/audit/quant/2026-08-10_strategy_allocation/model_harness_baseline/control-panel-qa/portfolio_control_panel"
  --as-of-date 2026-09-04
  --pipeline-id mvp2-live-only-20260910
  --release-commit "$SHA"
)

echo "== spec recipe (schema 6) =="
"$PY" tools/portfolio_release_builder.py recipe \
  --provider-ledger-spec "$RELEASE_SPEC" \
  --decomposition-plan-bundle "$PRIOR_BUNDLE" \
  "${recipe_common[@]}" --output "$CYCLE/spec-recipe.json"

for role in ledger baseline; do
  echo "== stage $role =="
  "$PY" tools/portfolio_release_builder.py stage \
    --recipe "$CYCLE/spec-recipe.json" --output-root "$OUT" --role "$role" \
    > "$CYCLE/stage-$role.json"
  cat "$CYCLE/stage-$role.json"
done

LEDGER_ID="$(json_field "$CYCLE/stage-ledger.json" generation_id)"
BASELINE_ID="$(json_field "$CYCLE/stage-baseline.json" generation_id)"
LEDGER_GEN="$OUT/stages/ledger/generations/$LEDGER_ID"
BASELINE_GEN="$OUT/stages/baseline/generations/$BASELINE_ID"
echo "ledger   $LEDGER_ID"
echo "baseline $BASELINE_ID"

# The report checks that the strategy attribution generation was computed
# against the same baseline it reads, so the attribution store follows the
# baseline this cycle stages.
echo "== restage strategy attribution against the staged baseline =="
ATTR_ROOT="$Q/dataPortfolio/analytics/attribution"
ATTR_SPEC="$LIVE/config/strategy-attribution-spec.json"
"$PY" "$E/attribution_spec_for_baseline.py"   "$ATTR_ROOT/config/strategy-attribution-spec-v2.json" "$BASELINE_GEN" "$ATTR_SPEC"
"$PY" tools/portfolio_attribution.py stage   --baseline-root "$OUT/stages/baseline" --spec "$ATTR_SPEC"   --output-root "$ATTR_ROOT" > "$CYCLE/attribution-stage.json"
ATTR_ID="$(json_field "$CYCLE/attribution-stage.json" generation_id)"
"$PY" tools/portfolio_attribution.py publish   --baseline-root "$OUT/stages/baseline" --spec "$ATTR_SPEC"   --output-root "$ATTR_ROOT" --generation-id "$ATTR_ID" > "$CYCLE/attribution-publish.json"
echo "attribution $ATTR_ID"

# The layered history follows the contract's provider-ledger lineage pin, not
# the ledger this cycle stages: that pin is deliberately one cycle behind,
# because a ledger generation depends on the receipt that pins the contract,
# so no contract can name the ledger staged from itself.
echo "== rebuild the layered history against the contract-pinned ledger =="
LH_ROOT="$LIVE/layered-history"
# The builder requires --repository-sha to be the executing checkout, and that
# commit is an input to the generation id, so every commit moves the id and
# the dependence-policy repin that follows would never converge. Rebuild only
# when the history's dependency blobs actually changed; the plan bundle
# verifies them against the working tree and fails closed if they have.
LH_SHA="$SHA"
REBUILD_HISTORY="${REBUILD_HISTORY:-0}"
PINNED_LEDGER_ROOT="$LIVE/provider-ledger"
PINNED_LEDGER_ID="$(json_field "$PINNED_LEDGER_ROOT/CURRENT.json" generation_id)"
PINNED_LEDGER_GEN="$PINNED_LEDGER_ROOT/generations/$PINNED_LEDGER_ID"
echo "pinned ledger $PINNED_LEDGER_ID"
if [ "$REBUILD_HISTORY" = "1" ]; then
  MX="$Q/dataMatrixport"
  MX_GEN="3badac0513e74e6b57a4f8cd1b4b30fe834ddffc1c372515af23a4e3225d9ccc"
  MX_CSV="$MX/output/20260903T051029.331Z/csv"
  QB="$Q/dataQuantbase"
  QB_GEN="d3a8f2da5a02b84fcc016981c04e4b8fec501cb902c019cfb8f9dc80e6b2d1fe"
  QB_CSV="$QB/source-runs/legacy-20260902-b1b5dcff331f/artifacts/csv"
  P123="$Q/dataPortfolio123"
  P123_GEN="80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8"
  P123_TABLES="$P123/generations/$P123_GEN/tables"
  P123_HISTORY_TABLES="$P123/generations/10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1/tables"
  "$PY" tools/portfolio_target_series.py   --layered-history-generation   --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json   --repository-sha "$LH_SHA"   --as-of-date 2026-09-04   --provider-ledger-current "$PINNED_LEDGER_ROOT/CURRENT.json"   --provider-ledger-manifest "$PINNED_LEDGER_GEN/generation.json"   --provider-ledger-catalog "$PINNED_LEDGER_GEN/provider-ledger.json"   --matrixport-current "$MX/CURRENT.json"   --manifest "$MX/generations/$MX_GEN/generation.json"   --nav "$MX_CSV/nav_daily.csv"   --holdings "$MX_CSV/holdings.csv"   --daily-holdings "$MX_CSV/daily_holdings.csv"   --subscriptions "$MX_CSV/subscription_details.csv"   --product-id 7361599360326447104   --quantbase-current "$QB/generations/$QB_GEN/CURRENT.json"   --quantbase-manifest "$QB/generations/$QB_GEN/generation.json"   --quantbase-history "$QB_CSV/dashboard_Volatis_Strategy_pricehistory.csv"   --quantbase-activity "$QB_CSV/account_activity.csv"   --quantbase-fund-id 107   --portfolio123-current "$P123/CURRENT.json"   --portfolio123-manifest "$P123/generations/$P123_GEN/generation.json"   --portfolio123-metadata "$P123_HISTORY_TABLES/har_managed_metadata.parquet"   --portfolio123-performance "$P123_HISTORY_TABLES/har_managed_performance.parquet"   --portfolio123-equity "$P123_HISTORY_TABLES/har_equity_curve.parquet"   --portfolio123-returns "$P123_TABLES/returns_daily.parquet"   --portfolio123-relationships "$P123_TABLES/strategy_relationships.parquet"   --portfolio-fx-rates-manifest "$Q/dataPortfolio/portfolio-fx-rates/generations/9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json"   --strategy-history-manifest "$HISTORY_BUNDLE/manifest.json"   --strategy-history-returns "$HISTORY_BUNDLE/returns.csv"   --strategy-backtests-generation "$BACKTESTS_GEN/generation.json"   --output-root "$LH_ROOT" > "$CYCLE/layered-history.json"
  cat "$CYCLE/layered-history.json"
  HISTORY_ID="$(json_field "$CYCLE/layered-history.json" generation_id)"
else
  HISTORY_ID="$PINNED_HISTORY_ID"
  echo "layered history reused (dependency blobs unchanged)"
fi
echo "layered history $HISTORY_ID"

echo "== repin the dependence policy and rebuild the dependence source =="
"$PY" "$W/repin_dependence_policy.py" "$LH_ROOT/$HISTORY_ID" "$ALLOCATION"
"$PY" tools/portfolio_current_dependence.py build   --contract contracts/portfolio-financial-mvp-source-v1.json   --policy contracts/portfolio-dependence-policy-v1.json   --history-generation "$LH_ROOT/$HISTORY_ID"   --allocation-generation "$ALLOCATION"   --evaluated-on 2026-09-04   --output-root "$DEP_ROOT" > "$CYCLE/dependence-final.json"
DEP_ID="$(json_field "$CYCLE/dependence-final.json" bundle_id)"
echo "dependence $DEP_ID"
recipe_final=()
skip_next=0
for arg in "${recipe_common[@]}"; do
  if [ "$skip_next" = 1 ]; then skip_next=0; recipe_final+=("$DEP_ROOT/$DEP_ID"); continue; fi
  recipe_final+=("$arg")
  if [ "$arg" = "--correlation-source-root" ]; then skip_next=1; fi
done
recipe_common=("${recipe_final[@]}")

echo "== repin crosswalk onto the staged baseline =="
"$PY" "$E/repin_crosswalk.py" "$BASELINE_GEN" "$DEP_ROOT/$DEP_ID"

echo "== issue static-snapshot coverage receipts for the staged inputs =="
rm -rf "$E/receipts"
"$PY" tools/static_snapshot_coverage_receipts.py \
  --provider-ledger "$LEDGER_GEN/provider-ledger.json" \
  --provider-ledger-generation "$LEDGER_GEN/generation.json" \
  --baseline "$BASELINE_GEN/portfolio-baseline.json" \
  --crosswalk "$E/current-epoch-crosswalk.json" \
  --output-dir "$E/receipts"

# The nested strategies (#567): the facts are built from the governed policy under
# ops/mvp2-release/inputs/sleeves, the staged ledger generation (which names the
# IBKR generation its rows come from) and the admitted Quantbase source files.
echo "== build the sleeve facts the epoch manifest selects =="
: "${IBKR_GENERATION_ROOT:?the IBKR data root holding generations/<id> is required}"
: "${QUANTBASE_SOURCE_DIR:?the admitted Quantbase csv directory is required}"
SLEEVE_DIR="$E/sleeve-facts"
rm -rf "$SLEEVE_DIR"
"$PY" tools/build_sleeve_facts_release.py \
  --policy-manifest ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json \
  --provider-ledger-generation "$LEDGER_GEN" \
  --ibkr-generation-root "$IBKR_GENERATION_ROOT" \
  --quantbase-source-dir "$QUANTBASE_SOURCE_DIR" \
  --output-dir "$SLEEVE_DIR" > "$CYCLE/sleeve-facts.json"
sleeve_facts=()
while IFS= read -r f; do
  sleeve_facts+=(--sleeve-facts "$f")
done < <(find "$SLEEVE_DIR" -type f -name '*-facts.json' | sort)

echo "== stage current-epoch manifest =="
# The epoch-manifest generation is a retained artifact the recipe pins by
# absolute path, so it cannot live in scratch. It needs a shallow OneDrive
# root: under the rollforward root its deepest receipt path is 302 characters
# and this workstation has LongPathsEnabled=0.
EPOCH_ROOT="$Q/dataEpochManifest"
receipts=()
while IFS= read -r f; do
  receipts+=(--receipt "${f#$E/receipts/}=$f")
done < <(find "$E/receipts/receipt" -type f -name '*.json' | sort)
"$PY" tools/stage_current_epoch_manifest.py \
  --provider-ledger-generation-root "$OUT/stages/ledger" \
  --provider-ledger-generation-id "$LEDGER_ID" \
  --baseline-generation-root "$OUT/stages/baseline" \
  --baseline-generation-id "$BASELINE_ID" \
  --flow-policy contracts/account-strategy-flow-classification-policy-v1.json \
  --crosswalk "$E/current-epoch-crosswalk.json" \
  --manifest-schema schemas/portfolio/account-strategy-manifest.schema.json \
  --receipt-authority "$RECEIPT_AUTHORITY" \
  --admission-policy "$LIVE/epoch-inputs/mapped-epoch-admission.json" \
  "${receipts[@]}" \
  "${sleeve_facts[@]}" \
  --output-generation-root "$EPOCH_ROOT" > "$CYCLE/epoch-manifest.json"
EPOCH_ID="$(json_field "$CYCLE/epoch-manifest.json" generation_id)"
echo "epoch manifest $EPOCH_ID"

# The governed INVESTED_CAPITAL input is derived from THIS cycle's ledger and
# epoch manifest, and it records the absolute path of the provider-ledger.json
# it was bound to. That path has to exist when a stage verifies it, so the file
# is rebuilt against the root this vertical is staging into -- the run2 variant
# that run_production needs is built separately, after the pipeline stages it.
# Leaving this out let the report and workbook stages bind a capital file whose
# ledger root had been wiped, and only the panel stage noticed.
# Which root the capital input names is not free. Production checks that the
# provider-ledger path it records sits inside the generation the candidate
# SELECTED, which is the copy the pipeline staged under run2; the staged
# capital result also has to carry a digest that resolves to a real artifact
# node. Both only hold when every stage -- proof2 and run2 alike -- binds one
# capital file rooted at run2. That root does not exist on a cold start, so the
# first cycle builds against proof2 and the next closes the loop. The ledger
# generation id is identical in both roots, which is what makes this converge.
echo "== rebuild the governed capital input against the staged ledger =="
LEDGER_FOR_CAPITAL="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['generation_id'])" "$CYCLE/stage-ledger.json")"
if [ -f "$W/run2/stages/ledger/generations/$LEDGER_FOR_CAPITAL/provider-ledger.json" ]; then
  CAPITAL_ROOT="$W/run2"
else
  CAPITAL_ROOT="$OUT"
  echo "run2 ledger absent, binding capital to the proof root for this pass"
fi
echo "capital root $CAPITAL_ROOT"
CAPITAL_RUN_ROOT="$CAPITAL_ROOT" "$PY" "$E/build_capital_input.py" | tail -4

echo "== rebuild the base decomposition source root from the staged ledger =="
rm -rf "$E/base-source"
"$PY" "$W/build_base_source_root.py" "$LEDGER_GEN" "$E/base-source" | tail -12

echo "== repin the base decomposition spec onto that source root =="
"$PY" "$W/write_base_spec.py" "$E/base-source" "$W/base-spec/base-decomposition-spec.json" | tail -10

# The contract requires DECOMPOSITION in both evidence modes and the
# report resolves one lens layout per mode. The release builder refuses
# any primary mode other than LIVE_ONLY, so both bundles are built from
# one contract state; it verifies they carry the same contract bytes.
plan_bundle() {
  "$PY" tools/portfolio_current_decomposition_plan.py \
    --plan docs/analytics/current-strategy-decomposition-plan.json \
    --catalog docs/analytics/decomposition-source-catalog.json \
    --financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json \
    --layered-history-generation "$LH_ROOT/$HISTORY_ID" \
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
    --evidence-mode "$1" \
    --evaluation-date 2026-09-04 \
    --assurance-profile RELEASE_QC \
    --output-root "$PLAN_ROOT" > "$2"
}

echo "== rebuild the decomposition plan bundle =="
# Windows MAX_PATH: the bundle nests a 64-hex id plus inputs/governance/<long
# name>, so it needs the shorter source-runs root, not the rollforward one.
PLAN_ROOT="$Q/dataPortfolio/source-runs/anl-decomp-current-plan-mvp2-20260910"
plan_bundle LIVE_ONLY "$CYCLE/plan-bundle.json"
plan_bundle LIVE_PLUS_BACKTEST "$CYCLE/plan-bundle-hybrid.json"
PLAN_ID="$(json_field "$CYCLE/plan-bundle.json" bundle_id)"
HYBRID_PLAN_ID="$(json_field "$CYCLE/plan-bundle-hybrid.json" bundle_id)"
echo "plan bundle live-only $PLAN_ID"
echo "plan bundle hybrid    $HYBRID_PLAN_ID"


echo "== verified recipe (schema 10) =="
"$PY" tools/portfolio_release_builder.py recipe \
  --provider-ledger-generation "$LEDGER_GEN" \
  --decomposition-plan-bundle "$PLAN_ROOT/$PLAN_ID" \
  --hybrid-decomposition-plan-bundle "$PLAN_ROOT/$HYBRID_PLAN_ID" \
  --epoch-manifest-generation-root "$EPOCH_ROOT" \
  --epoch-manifest-generation-id "$EPOCH_ID" \
  "${recipe_common[@]}" --output "$CYCLE/vertical-recipe.json"

governed=(
  --hwm-governed-result "$W/hwm/as-held-composite-hwm.json"
  --hwm-governed-input-generation "$W/hwm/hwm-input-generation.json"
  --hwm-governed-calendar-authority "$W/hwm/calendar-authority.json"
  --hwm-governed-admission-receipt "$LEDGER_GEN/admit-or-declare-receipt.json"
  --invested-capital-result "$W/capital/invested-capital.json"
  --invested-capital-input-generation "$W/capital/capital-input-generation.json"
)

for role in attribution analytics report workbook panel-candidate; do
  echo "== stage $role =="
  extra=()
  case "$role" in report|workbook|panel-candidate) extra=("${governed[@]}");; esac
  "$PY" tools/portfolio_release_builder.py stage \
    --recipe "$CYCLE/vertical-recipe.json" --output-root "$OUT" --role "$role" \
    --qa-dir "$W/qa2" --portfolio-root "$Q/dataPortfolio" \
    "${extra[@]}" \
    > "$CYCLE/stage-$role.json"
  cat "$CYCLE/stage-$role.json"
done

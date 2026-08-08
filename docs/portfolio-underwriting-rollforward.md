# Portfolio underwriting roll-forward runbook

This is the Windows operator path for refreshing accepted portfolio facts,
recomputing the complete analysis stack, rebuilding the control panel, and
publishing one coordinated release. It codifies the recurring analysis; it does
not turn unavailable evidence into estimates or authorize trades.

The expanded strategy-underwriting output is produced by the control-panel
builder under `PORT-UNDERWRITE-1`. There is no manually maintained strategy
scorecard. Every cycle derives the nine rows again from the hash-bound report,
factor/style/sector/industry generations, dynamic replication, dependence,
weekly returns, and the underwriting policy.

## Windows authority and durable paths

Run the authoritative cycle in PowerShell from a clean checkout at the exact
release commit. WSL may be used for read-only inspection, but it is not the
publication runtime: the governed data, durable Python environment, browser
QA, and stable launcher use Windows paths and Windows filesystem semantics.

```powershell
$repoRoot = [IO.Path]::GetFullPath($PWD)
$portfolioRoot = [IO.Path]::GetFullPath('C:\Tools\quant-engine\data')
$enginePython = 'C:\Tools\quant-engine\.venv\Scripts\python.exe'
$cycleId = '<yyyy-mm-dd-port-underwrite-nnn>'
$cycleRoot = Join-Path $portfolioRoot (Join-Path 'rollforward' $cycleId)
$proofRoot = Join-Path $cycleRoot 'proof'
$qaRoot = Join-Path $proofRoot 'panel-qa'
$runRoot = Join-Path $cycleRoot 'run'
$legacyPanel = '<accepted-legacy-panel-root>'
$planBundle = '<exact-hash-bound-decomposition-plan-bundle>'
$providerLedgerSpec = '<exact-new-root-provider-ledger-spec.json>'
$baselineSpec = '<exact-new-root-baseline-spec.json>'
$baseFactorFile = '<exact-new-root-factor-file.csv>'
$correlationSourceRoot = '<exact-new-root-correlation-source-root>'
$asOfDate = '<yyyy-mm-dd>'
$releaseCommit = (git rev-parse HEAD).Trim()
$env:QE_PIPELINE_PYTHON = $enginePython

if ($portfolioRoot -like '*\OneDrive -*' -or $portfolioRoot -like '*\repo\*') {
  throw 'Portfolio authority must remain outside synced storage and Git.'
}
if ((git status --porcelain).Count -ne 0) {
  throw 'The release checkout must be clean.'
}
```

Do not substitute a copied historical recipe or an ambient `CURRENT.json` path.
The cycle must name exact source paths, generation IDs, hashes, repository
commit, runtime, and lock file.

## What is recomputed

The release graph recomputes these distinct evidence layers and never silently
concatenates them:

| Layer | Accepted authority | Recurring result |
|---|---|---|
| Design intent | effective-dated strategy-version registry and admitted design artifact | role and economic family; inferred archetypes remain explicitly non-authoritative |
| Version-matched backtest | exact strategy version and pre-live observations only | long-history factor, style, sector, industry, weekly-return, dependence, and regime evidence |
| Provider/model live | admitted provider/model return path | reference behavior kept separate from the brokerage account |
| Account live | accepted account return path | realized return, drawdown, factor/style/sector/industry, dynamic path, and live regime behavior |
| Implementation reconciliation | exact-common-date account and reference facts | total gap, beta, correlation, tracking error, diagnostic signature, causally proven components, and unresolved residual |
| Portfolio membership | current allocations and accepted dependence generation | correlation-one contribution, leave-one-out stress, and one budget per economic-driver cluster |
| Data scope | report fact matrix, observation windows, and attribution dispositions | all declared paths, including every `NO_ACCEPTED_FACT_PATH`, with no silent zero |

The panel exposes `LIVE_ONLY` and `LIVE_PLUS_BACKTEST` separately. A hybrid is a
true long-history result only when the strategy version is proven compatible
and the backtest ends before live inception. Otherwise the hybrid result remains
equal to live evidence and is labeled as unavailable for long-history
underwriting.

## 1. Admit or retain source evidence

Provider acquisition is a separate effect from recomputation. Reuse accepted
immutable source runs when their declared observation boundary has not changed.
Acquire new provider evidence only when the cycle explicitly authorizes it.
Portfolio123 remains HAR-first; an API call is allowed only for a declared HAR
gap with current credit evidence and explicit approval. Never read or persist a
credential value in a recipe, receipt, log, or panel artifact.

Before building, confirm that the source specifications name the intended
observation windows, effective-dated account assignments, strategy versions,
benchmark assignments, and provider dispositions. A later package date is not
evidence that a fact's observation window advanced.

## 2. Build freshness and the complete vertical

Create the same-day freshness artifact from the exact provider-ledger
generation. The evaluation date is the Windows local operational date.

```powershell
& $enginePython tools\portfolio_freshness.py `
  --provider-ledger-root (Join-Path $portfolioRoot 'provider-ledger') `
  --provider-ledger-generation-id '<exact-generation-id>' `
  --evaluation-date $asOfDate `
  --output (Join-Path $cycleRoot 'freshness-health.json')
```

Materialize a new vertical recipe and build every declared stage. The builder
executes the ledger, baseline, attribution, decomposition lenses, dynamic
replication, dependence, weekly returns, report, workbook, and panel from the
declared inputs; it does not copy a prior panel.

```powershell
& $enginePython tools\portfolio_release_builder.py recipe `
  --provider-ledger-spec $providerLedgerSpec `
  --baseline-spec $baselineSpec `
  --base-factor-file $baseFactorFile `
  --decomposition-plan-bundle $planBundle `
  --correlation-source-root $correlationSourceRoot `
  --legacy-panel $legacyPanel `
  --as-of-date $asOfDate `
  --pipeline-id $cycleId `
  --release-commit $releaseCommit `
  --output (Join-Path $cycleRoot 'vertical-recipe.json')

& $enginePython tools\portfolio_release_builder.py build `
  --recipe (Join-Path $cycleRoot 'vertical-recipe.json') `
  --output-root $proofRoot
```

## 3. Verify the expanded analysis before browser work

Run the panel verifier against the candidate emitted by the vertical build.
Acceptance requires:

- nine strategy-underwriting rows and exactly five separate evidence layers;
- `automatic_drop_authority`, `allocation_authority`,
  `readiness_gate_effect`, and `execution_authority` all equal to `NONE`;
- exact gap identity: total gap equals causally bound components plus unresolved
  residual, with reconciliation error no greater than `1e-12`;
- no fee, cash, fill, timing, slippage, or subscription-cost component unless an
  exact-date account-bound fact supports it;
- every factor/style/sector/industry lens retains beta/loadings, alpha where
  defined, sample length, collinearity/stability evidence, and sealed-holdout
  diagnostics;
- regime thresholds use benchmark weeks strictly before account-live inception;
- all fact-matrix, attribution-disposition, observation-window, and selection-
  boundary payloads are referenced by the renderer;
- all nine existing views remain present.

```powershell
& $enginePython tools\portfolio_control_panel.py verify `
  --report-root (Join-Path $portfolioRoot 'reporting') `
  --legacy-panel $legacyPanel `
  --candidate-dir '<candidate-directory>' `
  --decomposition-lenses-root (Join-Path $portfolioRoot 'analytics\decomposition-lenses') `
  --decomposition-plan-bundle $planBundle `
  --weekly-returns-root (Join-Path $portfolioRoot 'analytics\weekly-returns') `
  --replication-root (Join-Path $portfolioRoot 'analytics\replication') `
  --freshness-health (Join-Path $cycleRoot 'freshness-health.json')
```

Inspect `data\dashboard_data.json` as evidence, not as an editable input. The
Data-health fact-path totals are derived from the payload; the current 72 total
and 22 no-path rows are an acceptance anchor, not a value to hard-code into a
future cycle.

## 4. Visual QA, then Candidate interaction QA last

First run browser capture for all nine views at 1440x900, 1024x768, and 390x844.
Inspect every screenshot and require clean console, request, overflow, clipping,
and visual-lint evidence. Confirm that Data health shows the complete fact
matrix and that Factors shows the selection boundary and all nine underwriting
rows.

Only after the data build, semantic verification, automated tests, and visual
review are green, run `interaction_qa.mjs`. Candidate interaction QA is the last
candidate check. It must exercise navigation and all existing controls without
removing or bypassing the frozen Candidates appendix.

```powershell
$env:PLAYWRIGHT_MODULE = '<session-runtime>\node_modules\playwright\index.mjs'
$env:PLAYWRIGHT_EXECUTABLE_PATH = '<chromium-browser-executable>'

node apps\portfolio-control-panel\browser_qa.mjs `
  --html (Join-Path '<candidate-directory>' 'index.html') `
  --output $qaRoot

node apps\portfolio-control-panel\interaction_qa.mjs `
  --html (Join-Path '<candidate-directory>' 'index.html') `
  --output (Join-Path $qaRoot 'interaction-results.json')

& $enginePython tools\portfolio_control_panel.py qa `
  --candidate-dir '<candidate-directory>' `
  --qa-dir $qaRoot
```

## 5. Prepare, review, and commit one coordinated release

Stage the already verified panel and bind the complete release graph. Planning
and the first execution are non-authoritative: they prepare the release without
advancing pointers.

```powershell
& $enginePython tools\portfolio_release_builder.py stage `
  --recipe (Join-Path $cycleRoot 'vertical-recipe.json') `
  --output-root $proofRoot `
  --role panel `
  --qa-dir $qaRoot

& $enginePython tools\portfolio_pipeline_materialize.py recipe `
  --vertical-recipe (Join-Path $cycleRoot 'vertical-recipe.json') `
  --proof-root $proofRoot `
  --qa-dir $qaRoot `
  --pipeline-id $cycleId `
  --output (Join-Path $cycleRoot 'pipeline-recipe.json')

& $enginePython tools\portfolio_pipeline_materialize.py spec `
  --recipe (Join-Path $cycleRoot 'pipeline-recipe.json') `
  --output-root $runRoot `
  --spec (Join-Path $runRoot 'pipeline-spec.json')

& $enginePython tools\data_pipeline.py plan `
  --spec (Join-Path $runRoot 'pipeline-spec.json')
& $enginePython tools\data_pipeline.py execute `
  --spec (Join-Path $runRoot 'pipeline-spec.json')
& $enginePython tools\data_pipeline.py status `
  --spec (Join-Path $runRoot 'pipeline-spec.json')
```

After inspection, commit the same immutable specification. Do not rebuild
between preparation and commit.

```powershell
& $enginePython tools\data_pipeline.py execute `
  --spec (Join-Path $runRoot 'pipeline-spec.json') `
  --commit
```

Post-commit, validate `CURRENT_RELEASE.json`, all release targets, the production
stable launcher, and a fresh production-launcher browser capture. Retain the
recipe, specification, plan, receipts, QA, semantic gates, release manifest,
journal, and post-commit verification under `$cycleRoot`.

## Recovery and non-duplication rules

- Resume the same unchanged specification after a local failure; do not create
  a new recipe merely to rerun completed deterministic nodes.
- Do not repeat a provider request whose receipt is `UNKNOWN`; adjudicate it.
- Do not reacquire or rescan an unchanged immutable source simply because the
  panel code changed.
- Do not rebuild analysis roots when only a byte-identical panel rebind is
  required; prepare a coordinated rebind that preserves the other accepted
  generation identities.
- Do not edit a published generation, historical receipt, or prior recipe.
- If commit is interrupted, use the explicit recovery path instead of retrying
  the forward mutation:

```powershell
& $enginePython tools\data_pipeline.py recover `
  --spec (Join-Path $runRoot 'pipeline-spec.json') `
  --confirm-stale-lock
```

## Evidence still needed for deeper causal conclusions

The recurring calculations are codified, but several conclusions remain
unavailable until their source facts are admitted:

- authoritative design artifacts and exact strategy versions for strategies
  currently represented only by an inferred economic archetype;
- version-matched pre-live backtests for strategies whose hybrid path currently
  equals live-only evidence;
- complete daily account cash and fee paths, plus order/fill timestamps and
  slippage evidence, for causal model-to-account gap decomposition;
- accepted provider/model paths where no exact-common-date comparison exists;
- benchmark history or effective-dated benchmark assignment where the governed
  registry lacks one.

These are source-admission gaps, not missing formulas. The panel must continue
to display their unavailable status and unresolved residual rather than infer a
cause or manufacture a long-history result.

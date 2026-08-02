# Portfolio control panel

The portfolio control panel is a generated, local, read-only view of the
accepted portfolio report. It preserves the audited HTML/CSS visual language
without preserving stale calculations or hard-coded operator conclusions.

## Authority boundary

- Current accounts, allocations, strategies, positions, performance, costs,
  gaps, coverage, reconciliation, stress, and readiness come from the current
  accepted `dataPortfolio/reporting` generation. Material HWM coverage may also
  consume a hash-verified `ACCOUNT_LIVE` provider overlay under `PORT-HWM-2`. A
  position displays a strategy only when the baseline found one exact
  effective-dated account assignment.
- Factor composition, net/gross alpha status, holdout fit, coverage,
  contribution reconciliation, and any dated dynamic exposure path come only
  from the accepted `dataPortfolio/analytics/decomposition` generation. Static
  RBSA and clean-room dynamic FLS results are both labeled
  `MPI_INFORMED_NON_EQUIVALENT`; neither is proprietary MPI output nor a claim
  to undisclosed MPI production behavior.
- The material HWM perimeter is explicit: Composer remains visible in capital
  reporting but is excluded de minimis; Robuxio uses accepted Matrixport
  account NAV and Volatis uses accepted Quantbase account value reconciled to
  cash additions. Every other current systematic strategy remains included.
- The provider-custodied account paths supply sleeve TWR, cumulative return,
  and maximum drawdown. They do not manufacture MWR, unrelated account cash
  flows, or a full-portfolio composite.
- Portfolio123 provider-live evidence is displayed alongside, but never joined
  to, the IBKR account-live TWR. Provider history, current account snapshot,
  exact-date overlap, and implementation-gap dates remain separately labeled.
- Observed Pearson/Spearman cells, 5/21/22/63/126/130/252/260-observation
  matrices, true pair drawdown-regime matrices, conceptual priors, posterior/stress
  matrices, the decision-only no-credit view, and correlation-one loss come
  only from the accepted `dataPortfolio/analytics/correlation` generation and
  remain separately labeled.
- Dependence defaults to `LIVE_ONLY`. When the accepted correlation generation
  also contains `LIVE_PLUS_BACKTEST`, the operator may switch to an explicitly
  labeled hybrid view. It uses only version-matched backtest observations before
  live inception; live observations remain authoritative from inception onward,
  and the hybrid view cannot change readiness, allocation, or risk-control
  conclusions. A generation without accepted backtest composition disables the
  hybrid selector rather than inferring history.
- The Candidates view is the hash-locked 2026-07-17 audit appendix. Its 33 rows
  are not `Strategy` identities and cannot update current totals, rank a new
  candidate, authorize diligence, or allocate capital.
- The application uses embedded local data and relative assets. It contains no
  network, provider, broker, order, deployment, or live-refresh path.
- Missing or unmapped positions, FX, flows, NAV, mappings, costs, or performance remain
  `FAILED`, `PARTIAL`, `UNAVAILABLE`, or `UNMAPPED`.
- The accepted portfolio inputs resolve to governed private source runs. The
  original external tracker and allocation roots have completed verified
  cutover and are not runtime dependencies.

The source contract in
`contracts/portfolio-control-panel-source-v1.json` records the audited shell,
appendix hashes, as-of date, and prior 18/18 validation anchor. The copied CSS
must match that contract byte for byte. Repository HTML and JavaScript replace
the dated renderer with the accepted control-panel data contract in
`schemas/portfolio/control-panel-data.schema.json`. `analytics.css` extends the
locked legacy stylesheet without modifying it or adding a framework.

## Build and verify

From the repository root, set local paths without committing them:

```powershell
$engine = Split-Path -Parent $PWD
$report = Join-Path $engine "dataPortfolio\reporting"
$output = Join-Path $engine "dataPortfolio\control-panel"
$legacy = Join-Path $engine "_strategy_allocation_audit\model_harness_baseline\control-panel-qa\portfolio_control_panel"
$strategyEvidence = "<hash-bound current-strategy-provider-evidence.json>"

uv run --frozen --no-sync python tools/portfolio_control_panel.py build `
  --report-root $report --legacy-panel $legacy --output-root $output `
  --strategy-evidence $strategyEvidence
```

The command resolves the accepted analytics roots as siblings of `reporting`:
`dataPortfolio\analytics\decomposition` and
`dataPortfolio\analytics\correlation`. `--decomposition-root` and
`--correlation-root` may override those paths for isolated tests. The panel is
not built if either pointer, schema, nine-strategy boundary, 36-pair boundary,
factor contribution identity, or correlation-one reconciliation differs.

The command prints the content-addressed candidate directory. Rebuilds from
unchanged inputs produce the same panel ID. Verify that exact candidate before
browser work:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py verify `
  --report-root $report --legacy-panel $legacy `
  --candidate-dir <candidate-directory>
```

The verifier proves all eight views, local-only assets, lack of network or
trade code, accepted capital/risk replay, the exact nine-strategy boundary, 19
account identities, the available and unavailable factor counts bound to the
analytics generation, dynamic
estimator disclosures when present, measured/unavailable dependence counts
derived exactly from the accepted generation, live-only default authority,
optional version-matched backtest composition, explicit unavailable facts, and
frozen-appendix isolation.

## Browser and interaction QA

The two repository scripts use Playwright supplied by a session-local runtime.
`PLAYWRIGHT_MODULE` points to Playwright's `index.mjs`; an existing Chromium
browser may be supplied with `PLAYWRIGHT_EXECUTABLE_PATH`. Evidence is written
inside the candidate and never committed:

```powershell
$env:PLAYWRIGHT_MODULE = "<session-runtime>\node_modules\playwright\index.mjs"
$env:PLAYWRIGHT_EXECUTABLE_PATH = "<chromium-browser-executable>"
$qa = Join-Path <candidate-directory> "qa"

node apps/portfolio-control-panel/browser_qa.mjs `
  --html (Join-Path <candidate-directory> "index.html") --output $qa
node apps/portfolio-control-panel/interaction_qa.mjs `
  --html (Join-Path <candidate-directory> "index.html") `
  --output (Join-Path $qa "interaction-results.json")
uv run --frozen --no-sync python tools/portfolio_control_panel.py qa `
  --candidate-dir <candidate-directory> --qa-dir $qa
```

Acceptance requires all eight views at 1440×900, 1024×768, and 390×844 plus a
direct-file capture; no console or request failures; clean visual lint across
all 24 viewport/view combinations; PDF export; eight-view navigation; factor
selection; dynamic exposure-path and same-factor static-benchmark evidence
when a dynamic model is selected; full-overlap, requested-horizon, and
drawdown-regime matrix selection with cell drill-down; live-only versus
live-plus-backtest selection when accepted layered evidence exists; allocation filters;
current-position
attribution separated from historical General intervals; candidate
filter/search; a responsive provenance control; and
absence of execution controls. Screenshots must also be inspected visually
before publication.

## Publish and recover

After the repository candidate is merged and its exact-SHA and post-merge gates
pass, publish the already accepted local candidate:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py publish `
  --report-root $report --legacy-panel $legacy `
  --candidate-dir <candidate-directory> --qa-dir $qa --output-root $output `
  --strategy-evidence $strategyEvidence
```

Publication hash-binds the accepted portfolio, decomposition, and correlation
artifacts, stages an immutable generation, copies the panel and QA evidence,
and advances `dataPortfolio/control-panel/CURRENT.json` only after all checks
pass. A failed build or QA run remains a non-authoritative candidate; a failed
publication leaves the prior pointer unchanged. The strategy-allocation audit
source is never edited, moved, or deleted by this workflow.

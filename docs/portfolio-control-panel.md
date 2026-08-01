# Portfolio control panel

The portfolio control panel is a generated, local, read-only view of the
accepted portfolio report. It preserves the audited HTML/CSS visual language
without preserving stale calculations or hard-coded operator conclusions.

## Authority boundary

- Current accounts, allocations, strategies, performance, costs, gaps,
  coverage, reconciliation, HWM availability, stress, and readiness come only
  from the current accepted `dataPortfolio/reporting` generation.
- Factor composition, net/gross alpha status, holdout fit, coverage, and
  contribution reconciliation come only from the accepted
  `dataPortfolio/analytics/decomposition` generation. The static method is
  labeled `MPI_INFORMED_NON_EQUIVALENT`; it is not proprietary MPI output or
  Dynamic Style Analysis.
- Observed Pearson/Spearman cells, conceptual priors, posterior/stress
  matrices, the decision-only no-credit view, and correlation-one loss come
  only from the accepted `dataPortfolio/analytics/correlation` generation and
  remain separately labeled.
- The Candidates view is the hash-locked 2026-07-17 audit appendix. Its 33 rows
  are not `Strategy` identities and cannot update current totals, rank a new
  candidate, authorize diligence, or allocate capital.
- The application uses embedded local data and relative assets. It contains no
  network, provider, broker, order, deployment, or live-refresh path.
- Missing positions, FX, flows, NAV, mappings, costs, or performance remain
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

uv run --frozen --no-sync python tools/portfolio_control_panel.py build `
  --report-root $report --legacy-panel $legacy --output-root $output
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
trade code, accepted capital/risk replay, nine-strategy boundary, 19 account
identities, five validated/four unavailable factor results, 10 measured/26
unavailable dependence pairs, explicit unavailable facts, and frozen-appendix
isolation.

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

Acceptance requires desktop Command, Risk, Factor & alpha, Dependence,
Candidates, Data health, mobile Command, and direct-file captures; no console
or request failures; clean Command, Factor & alpha, Dependence, and mobile
visual lint; PDF export; eight-view navigation; factor selection; matrix
selection and cell drill-down; allocation filters; candidate filter/search; a
responsive provenance control; and absence of execution controls. Screenshots
must also be inspected visually before publication.

## Publish and recover

After the repository candidate is merged and its exact-SHA and post-merge gates
pass, publish the already accepted local candidate:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py publish `
  --report-root $report --legacy-panel $legacy `
  --candidate-dir <candidate-directory> --qa-dir $qa --output-root $output
```

Publication hash-binds the accepted portfolio, decomposition, and correlation
artifacts, stages an immutable generation, copies the panel and QA evidence,
and advances `dataPortfolio/control-panel/CURRENT.json` only after all checks
pass. A failed build or QA run remains a non-authoritative candidate; a failed
publication leaves the prior pointer unchanged. The strategy-allocation audit
source is never edited, moved, or deleted by this workflow.

# Portfolio control panel

The portfolio control panel is a generated, local, read-only view of the
accepted portfolio report. It preserves the audited HTML/CSS visual language
without preserving stale calculations or hard-coded operator conclusions.

## Authority boundary

- Current accounts, allocations, strategies, performance, costs, gaps,
  coverage, reconciliation, HWM availability, stress, and readiness come only
  from the current accepted `dataPortfolio/reporting` generation.
- The Candidates view is the hash-locked 2026-07-17 audit appendix. Its 33 rows
  are not `Strategy` identities and cannot update current totals, rank a new
  candidate, authorize diligence, or allocate capital.
- The application uses embedded local data and relative assets. It contains no
  network, provider, broker, order, deployment, or live-refresh path.
- Missing positions, FX, flows, NAV, mappings, costs, or performance remain
  `FAILED`, `PARTIAL`, `UNAVAILABLE`, or `UNMAPPED`.

The source contract in
`contracts/portfolio-control-panel-source-v1.json` records the audited shell,
appendix hashes, as-of date, and prior 18/18 validation anchor. The copied CSS
must match that contract byte for byte. Repository HTML and JavaScript replace
the dated renderer with the accepted control-panel data contract in
`schemas/portfolio/control-panel-data.schema.json`.

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

The command prints the content-addressed candidate directory. Rebuilds from
unchanged inputs produce the same panel ID. Verify that exact candidate before
browser work:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py verify `
  --report-root $report --legacy-panel $legacy `
  --candidate-dir <candidate-directory>
```

The verifier proves the six views, local-only assets, lack of network or trade
code, accepted capital/risk replay, nine-strategy boundary, 19 account
identities, explicit unavailable facts, and frozen-appendix isolation.

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

Acceptance requires desktop, mobile, Risk, Candidates, Data health, and direct
file captures; no console or request failures; clean desktop/mobile visual
lint; PDF export; navigation; allocation filters; candidate filter/search; a
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

Publication stages an immutable generation, copies the panel and QA evidence,
and advances `dataPortfolio/control-panel/CURRENT.json` only after all checks
pass. A failed build or QA run remains a non-authoritative candidate; a failed
publication leaves the prior pointer unchanged. The strategy-allocation audit
source is never edited, moved, or deleted by this workflow.

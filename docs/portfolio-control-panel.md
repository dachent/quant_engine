# Portfolio control panel

The portfolio control panel is a generated, local, read-only view of the
accepted portfolio report. It preserves the audited HTML/CSS visual language
without preserving stale calculations or hard-coded operator conclusions.

## Authority boundary

- Current accounts, allocations, strategies, positions, performance, costs,
  gaps, coverage, reconciliation, stress, and readiness come from the current
  accepted `<portfolio-root>/reporting` generation. Material HWM coverage may also
  consume a hash-verified account- or provider-live overlay under `PORT-HWM-3`.
  HWM freshness is evaluated on the lane admitted for that strategy. A
  position displays a strategy only when the baseline found one exact
  effective-dated account assignment.
- Current holdings are the accepted position rows that the report marks as the
  latest snapshot of their account and that are dated at the calculation
  cutoff. `counts.positions` counts only those rows. `position_snapshots`
  counts the rest by name (latest snapshot before the cutoff, historical
  snapshots, rows dated after the cutoff, unavailable rows) with the history
  date range, and every position row carries its `position_class`. The
  verifier recomputes the split from the report rows. Panel schema 24 (25 for a
  version-3 report) carries these fields; 21 to 23 remain readable.
- Version-matched long history for a strategy is the hybrid-gate predicate
  (accepted history for economic inference, live plus backtest eligible) and
  its own `history_observations`. A decomposition lens never sets membership:
  the lens statistics come from the strategy's AVAILABLE or DIAGNOSTIC row and
  name their source key (`long_history.lens_statistics_source`).
- An implementation contract row built from a different provider ledger than the governed
  one reaches the panel as the report's closed NOT_READY object (N66-R). The
  underwriting implementation layer then reports its comparison as not ready
  (`comparison_status`), whichever contract kind is stale, and a detail with no
  return reconciliation is not ready, never a legacy comparison. The detail holds
  each object under `stale_reconciliation` or `stale_return_attribution`, and
  `contracts` gives the contract dates, the stale and pre-policy flags and the
  provider return policy. The panel shows the governed account return with its basis and window; the contract's own
  superseded figure stays beside its basis label and is never read as a return. The
  verifier validates the closed object and accounts for the three strategies as
  causal, policy-unavailable or stale.
- The governed PORT-HWM-4 number appears as `advisory.hwm_candidate`, labelled
  as pending owner acceptance with the E2 reason. The numeric HWM status stays
  not accepted and `hwm_summary` is unchanged.
- The numerical HWM composite (the `hwm_inputs` envelope) is reconciled
  against the governed baseline generation, never against the report's
  carried copies: the panel requires the baseline root (`--baseline-root`),
  resolves the exact generation named by the report's baseline reference,
  hash-verifies its manifest and catalog, validates the catalog's envelope
  against the full contract (anchor constants and typed acceptance
  provenance, per-lane calendar authorities with artifact-role resolution,
  reconciliation block, disposition invariants, governed anchor-to-as-of
  keyspace over the systematic perimeter, and capital/component identity
  against the hash-bound rollforward rows), and recomputes the composite
  from the catalog. A missing root, an unresolvable generation, a copy that
  differs from the catalog, or any contract violation fails the build
  closed. The report's carried envelope remains display-only.
- Every strategy row also projects the effective-dated v2 strategy-version
  registry: overall disposition, design/configuration/backtest/live-boundary
  status, cost and benchmark evidence, source route, compatibility, and named
  gaps. QSI, GrizzlyBears, TuringTrader, and Volatis have admitted version-bound
  history. EU is a named exact-model HAR export defect; long EU curves from
  different books are retained but rejected as substitutes. EchoTrade is
  deprecated with retained event-only history rejected as non-daily account-live
  evidence, and Composer is immaterial with unconnected history. Return evidence
  does not manufacture a design or backtest version.
- Factor composition, net/gross alpha status, holdout fit, coverage,
  contribution reconciliation, and each dated dynamic exposure path come only
  from accepted analytics generations. The clean-room dynamic FLS lens remains
  under `<portfolio-root>/analytics/decomposition`; the current strategy views bind five
  independently published generations under
  `<portfolio-root>/analytics/decomposition-lenses` for U.S./European economic
  factors, U.S. equity ETF style, U.S. sector returns, and U.S. 49-industry
  returns. These lenses are selectable but never concatenated. Static
  RBSA and clean-room dynamic FLS results are both labeled
  `MPI_INFORMED_NON_EQUIVALENT`; neither is proprietary MPI output nor a claim
  to undisclosed MPI production behavior. Dynamic smoothness selection uses
  training evidence only, reports a sealed holdout, and displays the same-factor
  static benchmark beside the effective-dated exposure path.
- The current-product perimeter is explicit: EchoTrade is deprecated and
  history-only, so its historical $0.19 evidence is excluded from every current
  strategy, allocation, account-selector, count, manifest, consolidated-value,
  and HWM surface. Composer is separately excluded as immaterial; Robuxio uses
  accepted Matrixport account NAV and Volatis uses accepted Quantbase account
  value reconciled to cash additions. All seven required current systematic
  strategies remain included.
- The provider-custodied account paths supply sleeve TWR, cumulative return,
  and maximum drawdown. They do not manufacture MWR, unrelated account cash
  flows, or a full-portfolio composite.
- Portfolio123 provider-live evidence is displayed alongside, but never joined
  to, the IBKR account-live TWR. Provider history, current account snapshot,
  exact-date overlap, and implementation-gap dates remain separately labeled.
  The implementation-reconciliation artifact also separates provider/model
  instructions, connected live books, temporary P123 books, broker executions,
  and account returns for AI Factor, GrizzlyBears, and TuringTrader. It reports
  exact-common-date beta, correlation, tracking error, capture, relative
  drawdown, trade-event coverage, and confirmed root causes without treating
  unmatched events as automatically manual. The interpretation and corrective
  actions are documented in
  `docs/analytics/model-to-actual-rcca.md`; event coverage is one-to-one and
  remains explicitly quantity/notional-unverified where the model source does
  not provide account-sized orders. The separate causal return-attribution
  artifact is also a mandatory hash-bound vertical input. For AI Factor it
  corrects the primary-only benchmark to the inferred effective connected-model
  mix; for GrizzlyBears and TuringTrader it renders every supported ordered
  counterfactual category and any explicit residual. The report contract proves
  that each displayed waterfall closes to the exact model/account endpoint.
  Both artifacts bind their account return to the governed account-live series
  (`account_leg`, basis `BASELINE_ACCOUNT_LIVE`) and name the model basis of
  every comparison. The report never publishes an account return that is not
  that series, so one window carries one account return: a row built from
  another provider-ledger vintage is a NOT_READY object that shows the governed
  return and its reason, and carries no model return, gap or waterfall.
- Observed Pearson/Spearman cells, 5/21/22/63/126/130/252/260-observation
  matrices, true pair drawdown-regime matrices, conceptual priors, posterior/stress
  matrices, the decision-only no-credit view, and correlation-one loss come
  only from the accepted `<portfolio-root>/analytics/correlation` generation and
  remain separately labeled.
- Dependence defaults to `LIVE_ONLY`. When the accepted correlation generation
  also contains `LIVE_PLUS_BACKTEST`, the operator may switch to the explicitly
  labeled hybrid view when at least one strategy is eligible: the report marks
  its accepted exact-version history `ELIGIBLE_ACCEPTED_HISTORY` beside a
  `PROVEN_COMPATIBLE` registry row (the report binds an `UNRESOLVED` row to the
  history only when its version is empty or equal, or its `accepted-live-path:`
  ref names the evidenced account-live window that starts on the history's live
  start; otherwise the report refuses). The gate then reports
  `ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY` when some strategies are blocked.
  Each blocked strategy stays in the hybrid view on its live observations only
  (zero backtest observations) with its named blocker, and a blocked strategy
  that uses backtest observations refuses the build. Only when no strategy is
  eligible is the hybrid payload suppressed, with the selector visible but
  disabled and the named compatibility blockers shown. An admitted hybrid uses
  only version-matched backtest observations before live inception; live
  observations remain authoritative from inception onward, and the hybrid view
  cannot change readiness, allocation, or risk-control conclusions.
- Dependence separately displays a measured-path central case and the
  correlation-one conservative bound. The central case uses accepted-path
  maximum drawdowns, measured exact-date Pearson dependence, governed
  conceptual priors for unavailable pairs, and PSD projection. It is
  descriptive only; the correlation-one bound remains the explicit readiness
  governor.
- Weekly returns come only from the accepted
  `<portfolio-root>/analytics/weekly-returns` generation. Every row uses an ISO
  Monday-through-Sunday week and compounds accepted daily returns as
  `product(1 + r) - 1`. The current cutoff week is shown by default and, when
  partial, is marked with its source-derived data-through date; a missing week
  remains unavailable rather than becoming zero. Strategy rows default to
  account-live evidence and may optionally use the same version-matched
  pre-inception backtest boundary as Dependence. Factor rows remain labeled as
  excess or long-short returns; strategy, ETF style, sector, industry, and core
  benchmark rows retain their declared total-return or proxy basis.
- Layered strategy underwriting is recomputed under `PORT-UNDERWRITE-1` for
  all nine retained registry records: seven REQUIRED active current strategies,
  Echo as `DEPRECATED_HISTORY_ONLY`, and Composer as `IMMATERIAL_EXCLUDED`.
  Authoritative design, exact-version backtest,
  provider/model live, account live, and implementation reconciliation remain
  separate. The result combines long-history statistical membership,
  model-versus-account diagnostics, pre-live-defined regime checks,
  correlation-one leave-one-out stress, and economic-driver cluster budgets.
  It is advisory only: automatic drop, allocation, readiness, execution, and
  order authority are all `NONE`.
- Data health renders the complete `fact_coverage_matrix`,
  `observation_windows`, and `attribution_dispositions` payloads. A
  `NO_ACCEPTED_FACT_PATH` row means no admitted account/fact source exists; it
  never means zero balance or zero activity. The Factors view renders the
  governed `selection_boundary` beside the lens methodology.
- The Candidates view retains the hash-locked appendix and can consume a
  hash-bound `portfolio-driver-map/1` artifact. The map publishes the five
  current economic-driver clusters, tactical leave-one-out effects, and dated
  measured/unavailable states for all 33 candidates. The rows are not
  `Strategy` identities and cannot update current totals, readiness, or capital.
- The application uses embedded local data and relative assets. It contains no
  network, provider, broker, order, deployment, or live-refresh path.
- Missing or unmapped positions, FX, flows, NAV, mappings, costs, or performance
  remain `FAILED`, `PARTIAL`, `UNAVAILABLE`, or `UNMAPPED`. A provider package
  date or the first/last observed event never substitutes for a fact-specific
  completeness boundary; where that boundary is absent, Data health says so
  and retains the named authority and disposition.
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

## Lifecycle fields (#592)

A manifest, report or panel that declares `lifecycle_schema_version` 1 carries
`lifecycle_state` (`ACTIVE`, `INACTIVE`, `RETIRED`, `CLOSED`), `retired_on`,
`closed_on`, `parent_assignment_id` and `sleeve_label`; a payload without the
member is the previous schema and stays readable. They are derived, never
typed: `CLOSED` when the epoch's own return closed inside it
(`performance_closed_on`), `RETIRED` when its assignment ended
(`effective_end`), `INACTIVE` when it is open but the current-epoch crosswalk
keeps it outside the product, `ACTIVE` otherwise. A sleeve's parent is the
parent account's assignment that its window overlaps, and its label is the tag
map's. The report adds `retained_strategies[]` (history-only records the
crosswalk or the nested-strategy tag map names) and the panel appends them to
`strategies[]` after the current rows, marked `record_kind`
`RETAINED_HISTORY_ONLY`; `counts.strategies` and every current-product check
read the current rows only.

A sleeve row (a report `twr_mwr` row, a strategy row or a retained record whose
epoch is a derived sleeve) also carries `parent_strategy_id`, the strategy of
the parent's assignment, and `sleeve_id`, the tag map's id. The two fields are
present only on sleeve rows; the Performance tab copies them to the slice of
that strategy so a chart can nest the slice under its parent. The current
sleeve of a strategy names that strategy itself as its parent strategy.

## Derived sleeves in the perimeter (#567)

When the account manifest carries derived sleeves, the crosswalk still names the
baseline perimeter and the manifest differs from it by exactly one rule
(`tools/derived_epoch_perimeter.py`, shared by the report, the panel, the HWM
selection and the totals verification): a residual parent account's epoch is
replaced by the epochs of its sleeves, which the flow-coverage authority
(`DERIVED_SLEEVE_COVERAGE_V1`) names with their parent. The thirteen product
epochs stay thirteen: the sleeve of a required strategy stands for the parent's
epoch. A sleeve of a strategy outside the product is a retained history-only
epoch: it stays in the manifest and in the Performance tab slices, and it is in
no product aggregate (HWM, totals, strategy summary, `performance`). The
report requires the nested-strategy tag map whenever derived epochs exist, binds
each derived account to a sleeve of it, and requires the contract's history-only
controls to be exactly the tag map's sleeve strategies the product does not
require. The custody account of a strategy's capital stays the parent account.

## Build and verify

From the repository root, set local paths without committing them:

```powershell
# Canonical repo example; use the explicit configured root in an isolated checkout.
$portfolioRoot = [IO.Path]::GetFullPath((Join-Path $PWD '../dataPortfolio'))
$engine = Split-Path -Parent $PWD
$report = Join-Path $portfolioRoot 'reporting'
$baseline = Join-Path $portfolioRoot 'baseline'
$output = Join-Path $portfolioRoot 'control-panel'
$legacy = Join-Path $engine "_strategy_allocation_audit\model_harness_baseline\control-panel-qa\portfolio_control_panel"
$lensRoot = Join-Path $portfolioRoot 'analytics\decomposition-lenses'
$planBundle = "<hash-bound decomposition planning bundle>"
$weekly = Join-Path $portfolioRoot 'analytics\weekly-returns'
$replication = Join-Path $portfolioRoot 'analytics\replication'
$freshness = "<exact hash-bound release freshness-health.json>"

uv run --frozen --no-sync python tools/portfolio_control_panel.py build `
  --report-root $report --baseline-root $baseline --legacy-panel $legacy --output-root $output `
  --decomposition-lenses-root $lensRoot `
  --decomposition-plan-bundle $planBundle `
  --weekly-returns-root $weekly `
  --replication-root $replication `
  --freshness-health $freshness
```

The command resolves the accepted analytics roots as siblings of `reporting`:
`<portfolio-root>\analytics\decomposition` and
`<portfolio-root>\analytics\correlation`. `--decomposition-root` and
`--correlation-root` may override those paths for isolated tests. The panel is
not built if any lens pointer, plan hash, schema, nine-record retained-registry
boundary, 36-pair boundary, factor contribution identity, or correlation-one
reconciliation differs.

`--freshness-health` is mandatory. `--baseline-root` is likewise mandatory:
it must point at the governed baseline generation root (the panel resolves
the exact baseline generation named by the report's baseline reference and
hash-verifies its manifest and catalog before reconciling the numerical HWM
composite against it). Build the freshness file deterministically from the
exact
provider-ledger generation before panel construction:

```powershell
uv run --frozen --no-sync python tools/portfolio_freshness.py `
  --provider-ledger-root (Join-Path $portfolioRoot 'provider-ledger') `
  --provider-ledger-generation-id '<exact-generation-id>' `
  --evaluation-date (Get-Date -Format 'yyyy-MM-dd') `
  --output $freshness
```

Every material strategy must have a `nav` or `strategy-returns` assessment.
The evaluation date is the local operational publication date, recorded as the
artifact's `evaluated_on`; the artifact itself (`as_of_date`) and every assessment
are dated at the financial data cutoff (the provider-ledger catalog date), so a
strategy last accepted on the cutoff is current as of it however late the run is,
and a source that published past the cutoff is accepted through it. The panel
generation manifest is dated at that same cutoff, never the later run date, and
publication refuses a freshness artifact or any bound input dated after it.
Pipeline preparation and commit both recompute every assessment from the embedded
policy and reject an older or forged bundle. Portfolio123 is a finite source in
the provider ledger and uses the `finite-history` policy: `FINITE_COMPLETE` at its
own last accepted date, not a recurring cadence it never owed.
`STALE`, `UNAVAILABLE`, or an absent assessment revokes the HWM-available
claim and reduces freshness-adjusted NAV coverage. `LATE` remains visible and
escalated but does not yet block current consumers. The exact freshness file
is also an input artifact of the panel generation and a required
`DATA-PIPE-001` node and gate.

The command prints the content-addressed candidate directory. Rebuilds from
unchanged inputs produce the same panel ID. Verify that exact candidate before
browser work:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py verify `
  --report-root $report --baseline-root $baseline --legacy-panel $legacy `
  --candidate-dir <candidate-directory> `
  --decomposition-lenses-root $lensRoot `
  --decomposition-plan-bundle $planBundle `
  --weekly-returns-root $weekly `
  --replication-root $replication `
  --freshness-health $freshness
```

The verifier proves all nine views, local-only assets, lack of network or
trade code, accepted capital/risk replay, the strategy, allocation, and account
identity boundaries derived from the accepted report, five separate
decomposition lenses with nine explicit retained-strategy dispositions each
(seven current and two excluded), the available and unavailable factor counts
bound to the analytics generations, dynamic
estimator disclosures when present, measured/unavailable dependence counts
derived exactly from the accepted generation, live-only default authority,
optional version-matched backtest composition, explicit unavailable facts, and
frozen-appendix isolation. It also proves that all four previously passive
evidence sections are consumed by the renderer and that the nine-record
retained-strategy underwriting ledger for the accepted report reconciles
without fabricated causal components or any
automatic decision authority.

The Windows-first end-to-end operator procedure, including the full evidence
stack, non-duplication rules, visual QA, Candidate interaction QA last, and
coordinated publication, is in
[`portfolio-underwriting-rollforward.md`](portfolio-underwriting-rollforward.md).

The Weekly returns view exposes all accepted strategies, U.S. and European
economic factors, U.S. equity and multi-asset ETF styles, sectors, 49
industries, and nine core benchmarks. It provides 13-, 26-, and 52-week views
plus paged full history, group and text filters, live-only versus eligible
live-plus-backtest strategy evidence, an explicit partial-week control, and
cell-level source/basis/availability detail.

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
  --html (Join-Path <candidate-directory> "index.html") --output $qa `
  --expected-calculation-cutoff 2026-08-13
node apps/portfolio-control-panel/interaction_qa.mjs `
  --html (Join-Path <candidate-directory> "index.html") `
  --output (Join-Path $qa "interaction-results.json") `
  --expected-calculation-cutoff 2026-08-13
uv run --frozen --no-sync python tools/portfolio_control_panel.py qa `
  --candidate-dir <candidate-directory> --qa-dir $qa
```

Version-2 capture manifests (`qa_manifest_version: 2`, also stamped on each
`visual-lint.json`) are relative to `--html`: `target` and `final_url` are
`index.html#<view>` (or `generations/<id>/index.html#<view>` through the stable
launcher), the four `*_path` fields are bare file names inside the capture folder,
and `html_directory_name` names the directory that held `--html`. No sealed QA file
may carry a drive letter, `file:` URL or UNC path; `portfolio_control_panel.py qa`
refuses it, writes its receipt where `--receipt` says (outside `--qa-dir`), and
`qa-inventory --qa-dir` repeats the scan, including PDF streams, on a sealed tree.

Acceptance requires all nine views at 1440×900, 1024×768, and 390×844 plus a
direct-file capture; no console or request failures; clean visual lint across
all 27 viewport/view combinations; PDF export; nine-view navigation; factor
selection; dynamic exposure-path and same-factor static-benchmark evidence
when a dynamic model is selected; full-overlap, requested-horizon, and
drawdown-regime matrix selection with cell drill-down; live-only versus
live-plus-backtest selection when accepted layered evidence exists; allocation filters;
weekly group/window/search/partial-week controls and cell drill-down;
current-position
attribution separated from historical General intervals; candidate
filter/search; a responsive provenance control; and
absence of execution controls. Screenshots must also be inspected visually
before publication.

Pass the calculation cutoff explicitly from release acceptance. The scripts
read the artifact/provider capture date from the manifest's bound ledger
identity; it may be later than the calculation cutoff, but never expands it.

## Governed source-to-panel orchestration

The perpetual portfolio rollforward uses the existing
`quant_engine.core.pipeline` executor and release coordinator. A portfolio run
is identified by a normal pipeline specification containing a
`portfolio-orchestration-contract` gate with profile
`PORTFOLIO_SOURCE_TO_PANEL_V1`. The hash-bound contract names:

- every immutable source-acquisition command and its adapter commit;
- the command-produced AuthoritySnapshot artifact;
- command nodes for ledger, baseline, attribution, analytics, report,
  workbook, and panel, together with their release-generation outputs;
- the command that produces the `post-build-semantic` gate; and
- the absolute runtime executable and lock file plus both SHA-256 identities;
- the hash of the runtime's installed distribution inventory; and
- the repository root, exact release `HEAD`, clean-worktree requirement, and
  SHA-256 of the repository entrypoint executed by every command node.

This is an execution contract, not a wrapper around existing candidates. On a
new pipeline identity, every declared command output must be absent until its
receipt-producing command runs. Release generations must be under the declared
output of their assigned stage command, and every release target must be
assigned to exactly one stage. Source commands write beneath an explicit
immutable `runs/<run-id>` boundary. Provider reads and all non-workbook
commands use zero automatic retries. The local, idempotent workbook stage
permits one declared resume attempt so an accepted desktop-Excel result can be
validated after a sandbox-to-desktop handoff. A failed attempt remains
immutable evidence; no provider request is repeated. Completed receipts
provide resumability for an unchanged specification.

The graph is strictly source acquisitions → AuthoritySnapshot → ledger →
baseline → attribution → analytics → report → workbook → panel → post-build
semantic reconciliation. Command arguments, declared reads and writes, and
generation sources may not reference `CURRENT` or `CURRENT.json`; every input
is an explicit path or ID. The runtime, installed environment, lock, clean Git
`HEAD`, and every command entrypoint are checked before the first command and
again before release preparation or commit. An adapter commit must equal that
observed release commit; putting a commit-looking string in command arguments
does not establish code identity.

The post-build semantic file is machine-validated rather than accepted only by
hash. It must bind the pipeline as-of date, unchanged release commit, and exact
release-generation map; report `PASS` for ledger bridges, financial identities,
mappings, analytics/report/panel equality, freshness, and generation-wide
invariants; and report zero unwaived blocking exceptions. A failure prevents
release preparation, leaving publication authority unchanged.

For a financial release, the panel command also requires `--financial-mode`,
the staged schema-3 `--account-manifest`, and exact ledger, ledger-identity,
baseline-root, classified-flow, classification-policy, and admission-policy
authorities. The release builder supplies these from schema 8 and reuses the
same staged manifest for build, verify, and publish. Omitting them is permitted
only for an explicitly non-financial development view; it cannot publish the
financial product.

Use the same CLI for the portfolio profile:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py plan `
  --spec <explicit-portfolio-pipeline-spec.json>
uv run --frozen --no-sync python tools/data_pipeline.py execute `
  --spec <explicit-portfolio-pipeline-spec.json>
```

The second command builds and prepares but does not publish. `--commit` remains
the separately governed atomic multi-root publication effect; `recover` uses
the existing journaled reverse-order recovery path and does not depend on the
build runtime still being available.

## Publish and recover

After the repository candidate is merged and its exact-SHA and post-merge gates
pass, publish the already accepted local candidate:

```powershell
uv run --frozen --no-sync python tools/portfolio_control_panel.py publish `
  --report-root $report --baseline-root $baseline --legacy-panel $legacy `
  --candidate-dir <candidate-directory> --qa-dir $qa --output-root $output `
  --coordinated-release-receipt <release-builder-stage-results/panel-publication-authorization.json> `
  --decomposition-lenses-root $lensRoot `
  --decomposition-plan-bundle $planBundle `
  --weekly-returns-root $weekly `
  --replication-root $replication `
  --freshness-health $freshness
```

Direct publication requires the release builder's hash-bound coordinated-release
receipt for that exact output root, candidate, and frozen QA inventory. This is
an honest-operator guard against accidental out-of-band authority advancement;
it is not a security boundary against a local principal able to write the
receipt and output trees. Publication hash-binds the accepted portfolio, decomposition, correlation, and
weekly-return artifacts, stages an immutable generation, copies the panel and
both the browser and content-addressed semantic validation results, then creates
the relative stable launcher. Before coordinated release, run the same browser
suite against that staged launcher and validate that every capture begins at
the launcher and resolves to the expected immutable generation:

```powershell
node apps/portfolio-control-panel/browser_qa.mjs `
  --html (Join-Path <staged-panel-root> "index.html") `
  --output <stable-launcher-qa-directory> `
  --expected-calculation-cutoff 2026-08-13
uv run --frozen --no-sync python tools/portfolio_control_panel.py launcher-qa `
  --launcher (Join-Path <staged-panel-root> "index.html") `
  --qa-dir <stable-launcher-qa-directory> `
  --expected-generation-id <generation-id>
```

Hash the resulting `stable-launcher-validation.json` as a release gate. Repeat
the launcher browser capture against the production launcher after coordinated
commit so acceptance proves the actual released `file://` entry point as well
as the byte-identical staged companion.
QA evidence,
and advances `<portfolio-root>/control-panel/CURRENT.json` only after all checks
pass. A failed build or QA run remains a non-authoritative candidate; a failed
publication leaves the prior pointer unchanged. The strategy-allocation audit
source is never edited, moved, or deleted by this workflow.

## Payload size and load budget (#540)

Size, per block (CI-cheap, release-time on a real generation):

```powershell
uv run --frozen --no-sync python tools/panel_payload_size.py <generation-dir> --out size.json
```

It reports the physical size, the serialized bytes of each top-level block of
`data/dashboard_data.json`, and exits 1 when a figure is over
`apps/portfolio-control-panel/payload_budget.json` (R2 committed figures plus 3%).
A block over budget fails, a new block over 50 kB without an entry fails, and
download copies fail. Raise a budget only in the change that grows the block, and state
the reason under `justifications` in the budget file (a test requires one for every
block re-cut after R2); a block left unlisted must stay under 50 kB. CI guards block shape on the fixture
`performance_tab` (`tests/portfolio_control_panel/test_panel_payload_budget_540.py`).
Encode new daily grids compactly (nullable arrays keyed once by a shared date
axis, no per-observation repeats of ids or labels).

Load, unattended and off the critical path:

```powershell
node apps/portfolio-control-panel/measure_load.mjs --html <generation>\index.html `
  --control <committed-previous-generation>\index.html --runs 3 --memory --lens-and-download `
  --out load.json
```

Usable load is `page.goto(file://...#command, waitUntil: "commit")` until
`#app[data-app-state="ready"]` exists (no DOMContentLoaded, no paint; clicks are not
part of it). Timing runs never run a sampler, because a process-tree scan beside the
load produced R2's retracted 88-150 s figures. Peak RSS (`--memory`, Windows) is a
separate run. The machine state is recorded (`idle`), and the verdict is the
candidate/control ratio of the timed means (`--max-ratio`, default 1.25), so a busy
machine moves both. R1/R2 reference: about 2.2 s usable load, lens mode open 1.2 s,
JS heap about 305 MiB, peak RSS 0.85-0.96 GiB (idle-ish machine, R2 evidence).

The `--lens-and-download` step (#690) reaches the report link the way the panel
documents it: the link sits in the hidden `[data-view="data"]` section, so the
harness opens `[data-nav="attribution"]`, selects the lens mode, then opens
`[data-nav="data"]` and waits until that view is shown, then clicks
`a[data-download-filename="portfolio-report-data.json"]`. Every click is a DOM
`.click()` on an attached element (Playwright `page.click` times out on nav items
outside the viewport in a CDP-attached headless window, and a hidden element is never
waited on for visibility). Each step retries a bounded number of times
(`STEP_ATTEMPTS`) with explicit waits; on exhaustion the result file is still written
with `memory.failure = {step, message}` and the harness exits 4 (0 ok, 1 verdict over
ratio, 2 harness error, 3 watchdog). The timing definition is unchanged.

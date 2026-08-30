# quant_engine

> **Positioning:** `quant_engine` is **a governed investment-data and portfolio
> control plane that sits above mature trading infrastructure**.

`quant_engine` coordinates investment-data acquisition, canonical portfolio
accounting and authority, performance analysis, reporting, and tightly governed
interfaces to execution systems. It is not intended to reimplement standard
trading-engine, broker-execution, OMS, or generic data-platform capabilities
when mature infrastructure can satisfy the required semantics, controls, and
operability. The project should integrate or wrap such infrastructure where it
fits, while retaining investment-specific provenance, coverage, authority,
reconciliation, portfolio semantics, analytics, and governance in the control
plane. It is built as one governed system with provider and infrastructure
adapters, not as a collection of unrelated scripts.

> [!IMPORTANT]
> The Foundation is under construction. No live-trading authorization is
> implied by code, configuration, credentials, a passing test, or access to a
> broker. Initial broker work is limited to offline, dry-run, or explicitly
> approved PAPER environments. This positioning does not select or authorize any
> external trading, execution, portfolio, or data platform.

## Design goals

- Give every source the same provenance, coverage, verification, publication,
  and recovery envelope.
- Preserve provider evidence without forcing research data, portfolio facts,
  and execution events into one schema.
- Make allocation decisions append-only and performance reproducible from an
  accepted ledger generation.
- Reuse mature trading, execution, portfolio, and data infrastructure where it
  satisfies the required contracts; build custom infrastructure only where the
  investment-specific semantics or control boundary require it.
- Fail closed when authority, source coverage, dependencies, or an external
  effect is unclear.
- Manage all work and approvals through GitHub Issues.

## Target layout

The Git worktree is `repo`. Real provider data and source specifications remain
in governed folders outside the repository. Provider roots stay beside the
workspace; the coordinated portfolio authority uses the durable local tools
boundary so high-churn generations are not stored in OneDrive:

```text
_quant_engine/
  repo/
  xBin/
  dataPortfolio123/
  dataMatrixport/
  dataQuantbase/
  dataSchwab/
  dataIBKR/
  dataFidelity/
  dataAlpaca/

C:\Tools\quant-engine\
  data\
```

The target Python workspace contains shared domain packages, provider packages,
broker applications, portfolio reporting, and one repository-authoritative
Portfolio123 skill. See [system boundaries](docs/architecture/system-boundaries.md)
for dependency direction and interface ownership.

## How work is governed

GitHub Issues are the sole execution tracker. Every change must have one open
governing issue whose title begins with a stable key such as `[GOV-001]`. The
issue records scope, dependencies, acceptance evidence, allowed effects, stop
conditions, recovery, and the final handoff. Markdown files may document
architecture or policy but must never become a second backlog.

Work follows the project-local Phase 0–8c operating canon. Required `PROCEED`
approvals, scope changes, external effects, destructive actions, and any future
live-trading authority must be recorded in the governing issue before the
corresponding action.

Read these before contributing:

- [Operating model](docs/operating-model.md) — normative process and issue
  contract
- [System boundaries](docs/architecture/system-boundaries.md) — target
  architecture and public surfaces
- [Agent instructions](AGENTS.md) — repository-local execution rules

## Foundation status

The repository uses protected `main`: changes require a pull request and the
configured required checks must pass at the exact head SHA. CODEOWNERS and
local push controls remain defense-in-depth. Historical plan limitations and
their reapproval triggers remain recorded in the
[Free-plan governance decision](docs/decisions/0001-github-free-main-governance.md).

Interfaces described in the architecture document remain target contracts until
their governing issues provide accepted implementation evidence. Do not infer
that an adapter, extractor, publication path, or broker action exists merely
because it is documented.

The first accepted implementation slice is
[`quant-engine-core`](packages/core/README.md): a dependency-free,
content-addressed source/generation manifest and fail-closed publication
pointer. It does not implement provider acquisition, portfolio facts, or data
migration.

The minimal canonical fact model is
[`quant-engine-portfolio-ledger`](packages/portfolio-ledger/README.md). It
provides deterministic, synthetic-testable account, strategy, security,
assignment, position, transaction, cash-flow, NAV, and reconciliation records.
It deliberately does not implement performance policy, provider acquisition,
or execution.

The first-baseline interpretation rules are ratified in
[ADR 0002](docs/decisions/0002-portfolio-performance-policy.md). The decision
defines coverage-aware TWR/MWR, flow, FX, composite, HWM, stress, and advisory
readiness behavior without creating an automated risk or execution subsystem.

Private tracker and current-strategy provenance is built with the
[portfolio provenance runbook](docs/portfolio-provenance.md). Only the generic
validator, schemas, and synthetic tests live in Git; real specifications,
account aliases, strategy details, hashes, and catalogs remain under the
configured portfolio-data boundary.

The dated, read-only portfolio baseline is built with the
[portfolio baseline runbook](docs/portfolio-baseline.md). It normalizes the
accepted tracker, audit, and pinned provider slices into accounts, current
allocations, available NAV/flows/transactions/positions, strategy evidence,
explicit coverage, reconciliation, and frozen-control replay. Current
positions inherit a strategy only from one exact effective-dated account
assignment. Missing provider facts, FX, history, or mappings remain
`UNAVAILABLE`, `PARTIAL`, or `UNMAPPED`; the builder does not infer them.

Legacy Portfolio123 authenticated artifacts are handled only through the
[checksummed security-quarantine control](docs/p123-security-quarantine.md).
Quarantine is reversible and does not substitute for provider-side credential
rotation or authorize a Portfolio123 data/API call.

The repository-authoritative [Portfolio123 capability](docs/providers/portfolio123.md)
combines deterministic browser capture plans, a deny-by-default WCH route contract,
HAR-first transport decisions, metered API authorization evidence, and one
formula/ranking-system skill. Authentication or extraction failure never causes
automatic API fallback, and provider mutations require separate exact approval.

Quantbase authenticated captures and account evidence use the same move/hash
engine through a [narrow Quantbase profile](docs/quantbase-security-quarantine.md).
The normalized CSV baseline remains outside that security quarantine and is
not published until the separate integration issue passes.

The accepted [Quantbase provider contract and generation runbook](docs/providers/quantbase.md)
defines the HAR-only routes, the 55-file dated baseline, deterministic replay,
and the verified single-authority cutover into `dataQuantbase`.

Legacy Alpaca plaintext reports use the bounded, reversible
[Alpaca security quarantine](docs/alpaca-security-quarantine.md) before any
code or data integration.

The accepted replacement surface is the [read-only Alpaca integration](docs/alpaca-integration.md),
with hash-bound legacy-code disposition recorded in
[Alpaca prototype provenance](provenance/alpaca/legacy-prototypes.md).

The baseline's coverage-aware performance tables and independent unified
workbook are built with the [portfolio report runbook](docs/portfolio-report.md).
The workbook is a generated, read-only deliverable: calculations originate in
tested Python and accepted private data, then native Excel recalculates and a
machine-readable contract validates the saved copy before publication.

The accepted report feeds the existing local visual shell through the
[portfolio control-panel runbook](docs/portfolio-control-panel.md). Current
capital, strategy, performance, gap, and coverage views derive only from the
accepted report generation. Factor/alpha and dependence views separately bind
the accepted analytics generations, preserve unavailable states, and expose
their sample boundaries. The Candidates view is a separate, hash-identified
frozen audit appendix; it has no refresh, selection, allocation, provider,
broker, or execution authority.

Public, no-auth market and macro acquisition is routed through the
[Agent Market Data Terminal](docs/providers/market-data-terminal.md). Its
provider-native skill and helper scripts preserve the full `dachent/mdt`
history. The useful Yahoo workbook-lineage behavior and the flattened endpoint
gist are absorbed as provenance into that single capability rather than kept as
duplicate products. Generated market data remains outside Git and every Yahoo
artifact receives hash-bound request and coverage evidence.

The factor-composition and alpha-decomposition capability is a clean-room,
coverage-aware implementation with static RBSA and dynamic FLS estimators. It
uses exact-date alignment, training-only selection, sealed walk-forward
holdouts, immutable generations, and explicit gross/net cost gates; it does not
claim MPI/DSA equivalence. See the
[factor and alpha runbook](docs/analytics/factor-alpha-decomposition.md).

Coverage-aware portfolio dependence is built separately from factor
decomposition. Measured Pearson/Spearman cells, conceptual priors, posterior and
stress matrices, implementation coincidence, and correlation-one loss remain
distinct. See the
[correlation and dependence runbook](docs/analytics/correlation-dependence.md).

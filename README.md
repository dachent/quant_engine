# quant_engine

`quant_engine` is a governed monorepo for investment-data acquisition, canonical
portfolio accounting, performance analysis, and tightly controlled broker
execution. The project is being built as one operating system with provider
adapters, not as a collection of unrelated scripts.

> [!IMPORTANT]
> The Foundation is under construction. No live-trading authorization is
> implied by code, configuration, credentials, a passing test, or access to a
> broker. Initial broker work is limited to offline, dry-run, or explicitly
> approved PAPER environments.

## Design goals

- Give every source the same provenance, coverage, verification, publication,
  and recovery envelope.
- Preserve provider evidence without forcing research data, portfolio facts,
  and execution events into one schema.
- Make allocation decisions append-only and performance reproducible from an
  accepted ledger generation.
- Fail closed when authority, source coverage, dependencies, or an external
  effect is unclear.
- Manage all work and approvals through GitHub Issues.

## Target layout

The Git worktree is `repo`. Real provider data and source specifications live in
sibling folders outside the repository:

```text
_quant_engine/
  repo/
  xBin/
  data_Portfolio123/
  dataMatrixport/
  dataQuantbase/
  dataSchwab/
  dataIBKR/
  dataFidelity/
  dataAlpaca/
  dataPortfolio/
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

The repository uses a private personal GitHub Free account. Its pull-request,
exact-SHA CI, CODEOWNERS, and local push controls are procedural and
defense-in-depth; GitHub does not server-enforce protected `main` or required
checks for this private repository. The accepted residual risk and mandatory
reapproval triggers are recorded in the
[Free-plan governance decision](docs/decisions/0001-github-free-main-governance.md).

Interfaces described in the architecture document remain target contracts until
their governing issues provide accepted implementation evidence. Do not infer
that an adapter, extractor, publication path, or broker action exists merely
because it is documented.

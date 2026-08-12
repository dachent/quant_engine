# quant_engine agent contract

These instructions apply to the entire repository. A more deeply nested
`AGENTS.md` may narrow implementation details but may not weaken this contract,
the governing GitHub Issue, or `docs/operating-model.md`.

## Authority and source of truth

- Use exactly one open GitHub Issue as the governing work item. Its stable key,
  approved scope, dependencies, allowed writes/effects, acceptance evidence,
  stop conditions, and recovery procedure define the work boundary.
- GitHub Issues are the sole execution tracker. Do not create a Markdown
  backlog, task database, local state ledger, or other shadow tracker.
- Repository documents describe policy and architecture. They do not authorize
  execution and do not override a blocker or missing approval in the issue.
- Record material decisions, phase evidence, approvals, Dead Ends, blockers,
  and the final handoff in the governing issue. If GitHub is unavailable, stop
  or arrange a human-mediated update; do not substitute another tracker.
- Keep the approved current tranche to at most three `backlog:active` issues.
  Promotion requires an owner review recorded in the issue after scope,
  dependencies, effects, evidence, and recovery are decision-complete.

## Required preflight

Before mutating repository or external state:

1. Identify the governing issue by number and stable key.
2. Confirm its `Blocked by` references are resolved and the item is ready.
3. Identify the current Phase 0–8c state and verify that its issue-recorded
   authority is current. This may be an exact phase `PROCEED` or a current owner
   mandate that explicitly covers the bounded issue, candidate, writes,
   effects, and stop conditions. Do not request another approval for a routine
   step already inside that authority.
4. Bound the intended files, systems, external effects, checks, retries, time,
   cost, and integration owner.
5. Inspect the working tree and preserve unrelated user or agent changes.

If any of these cannot be established, perform only safe read-only discovery
and hand off the blocker.

## Execution rules

- Make the smallest coherent, preferably reversible change within the approved
  issue scope. Do not weaken acceptance criteria to obtain a pass.
- This repository is private and GitHub currently enforces protected `main`,
  strict required checks, administrator enforcement, conversation resolution,
  force-push denial, and branch-deletion denial. Required approving reviews,
  signed commits, and linear history are not enabled. The live protection is a
  server control; its historical billing-plan attribution is not independently
  established.
- Never update, force-update, delete, or repoint `main` directly. Work on a
  non-main branch and use a pull request. Before merge, record the exact PR head
  SHA and prove that Quality, Security, Data boundary, and Issue guard all
  completed successfully for that unchanged SHA. A template, green badge,
  local hook, or post-push run is not a substitute for this evidence.
- Stop before merge if any expected workflow is absent, pending, cancelled,
  skipped unexpectedly, or unsuccessful. Any detected direct or forced update
  to `main` freezes subsequent work until a governance incident reconciles the
  remote history. Reopen `GOV-002` before adding a writer or write-capable app,
  changing plan or visibility, or enabling live trading.
- Use one writer for a shared file, repository surface, external system, Office
  application, or browser session unless both the write and external-effect
  surfaces are demonstrably independent. Name an integration owner.
- Delegate only bounded work. Every task packet must state the issue, outcome,
  scope, references/Dead Ends, allowed writes, evidence, and limits. Every
  result must state status, artifact/diff, checks, uncertainty, blockers, and
  next action.
- Retry only after a new diagnosis or a relevant candidate, input, artifact,
  environment, or gate change, and remain within the approved attempt limits.
  Never loop until pass.
- Run each declared gate once per unchanged candidate. Rerun only after a
  relevant change. Treat a passing check as evidence, not infallible proof.
- A current bounded mandate authorizes the routine repository and GitHub steps
  it names, including edits, focused checks, commits, pushes, pull-request
  updates, and issue evidence. Do not interrupt execution with step-level
  approval prompts. Stop when the candidate, authority, scope, effects, or
  irreversible risk materially changes or is ambiguous.
- After each sprint, update the governing issue with outcome, evidence,
  decisions, Dead Ends, blockers, and dependencies, then follow the routing in
  the operating model.

## Hard approval boundaries

A general phase `PROCEED` does not authorize any of the following:

- writes to GitHub, provider accounts, cloud resources, broker systems, or
  other external destinations not already explicit in the issue;
- credential creation, rotation, revocation, or persistence;
- any operation on a credential-shaped file, even when believed stale or
  revoked;
- deletion of worktree or data-root content, overwriting authoritative data,
  archive/removal of a legacy source, or another action that is hard to recover;
- live trading, compensating orders, account-setting changes, or expansion from
  read-only/dry-run/PAPER behavior.

Each requires a separate, current, issue-recorded approval that names the exact
effect or target and its recovery or reconciliation procedure. Stop on an
unknown broker submission outcome and reconcile before any retry.

Until `SEC-001` is accepted, do not introduce credentials, captured sessions,
real account identifiers, or real provider/broker data into the repository or
CI. Synthetic fixtures require explicit identification.

## Domain boundaries

- Applications may compose domain packages and provider adapters; provider
  packages must not depend on one another.
- Research datasets, portfolio/accounting facts, and broker execution events
  retain separate contracts and provenance.
- External account numbers belong behind a restricted local crosswalk, never in
  source, tests, logs, issue text, or committed fixtures.
- All broker adapters default to non-live behavior. Code reachability is not
  trading authority.

## Completion

Before claiming completion, verify the integrated candidate against the issue's
success, failure, and out-of-scope criteria. Confirm the dependency graph is
still valid, list exact checks and results, document recovery state and known
gaps, and complete the issue's `Final evidence and handoff` section.
Never close with pending final evidence, and never move unfinished acceptance
scope to a successor key merely to close the governing issue. A successor is
valid only for a genuinely independent outcome; otherwise the original issue
stays open.

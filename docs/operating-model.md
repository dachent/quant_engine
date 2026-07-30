# Project operating model

This document is the normative, project-local operating canon for
`dachent/quant_engine`. It governs planning, execution, verification, approvals,
delegation, and handoffs. GitHub Issues hold execution state; this document does
not contain a backlog.

## Provenance and adaptation

| Field | Value |
|---|---|
| Upstream scaffold | `claude_code_deep_planning.md` in [dachent/cdc05151d047708c290bd4da0aaeed96](https://gist.github.com/dachent/cdc05151d047708c290bd4da0aaeed96) |
| Frozen gist revision | `ef2adb8dcb702eb39c0888cb3e455c7cc40c977d` |
| Upstream file SHA-256 | `B6113AAFEAD3856EFB1485039C4F801A8DFD30B41B5C0715C16B99C6B1921AC2` |
| Project acquisition date | 2026-07-27 |
| Project canon | This repository file |

The upstream file was validated as an 811-word, Phase 0–8c scaffold. This
adaptation preserves its phase order, approval cadence, Dead Ends, bounded
retry, one-writer/integration rules, sprint routing, evidence semantics, and
handoff requirements. It deliberately changes:

1. GitHub Issues are the only authoritative tracker; the upstream Jira or
   Markdown alternatives do not apply.
2. Each issue uses a stable key, explicit dependency block, scope/effect
   boundary, acceptance evidence, stop/reapproval triggers, recovery, and final
   handoff.
3. Quant-specific external, irreversible, data-authority, financial-calculation,
   and broker-execution risk requires explicit gates.
4. Named tools or skills are optional implementations of required outcomes. If
   unavailable, use native capabilities and record only material substitutions.
5. This repository copy is the sole canon for this project. Upstream changes
   have no effect until adopted through a governed issue with a new provenance
   and delta record.

## Non-negotiable operating rules

- Resume at the earliest phase whose output or approval lacks current,
  trustworthy evidence. For an audit, enter wherever needed to test the claim;
  prior work is evidence, not permission to skip review.
- Keep target, scope, criteria, architecture, authority, external effects, and
  irreversible risk fixed during an approved attempt. A material change returns
  to the relevant phase and invalidates downstream approval.
- Use one governing issue. If it is inaccessible, stop or use a human-mediated
  issue update; never create a shadow tracker.
- Use the issue's `Blocked by` relationships to determine readiness. Sub-issues
  represent decomposition, not execution order.
- Make declared checks and evidence proportional to impact. A check is evidence,
  not truth; a deterministic pass cannot expand authority.
- Preserve Dead Ends in the issue whenever a path is rejected. Rejected paths
  may be retried only when their recorded failure mechanism materially changes.
- Keep secrets, real account identifiers, captured sessions, and real financial
  data out of Git, issue text, and CI while the dedicated policy gate remains
  unresolved.

## Issue contract

Every issue title starts with a unique stable key, for example
`[GOV-001] Codify project operating model`. The issue body contains these nine
authoritative sections:

1. **Outcome** — observable end state.
2. **In scope** — approved work.
3. **Out of scope** — excluded work.
4. **Blocked by** — `None` or GitHub issue references with stable keys, such as
   `#3 (GOV-001)`.
5. **Acceptance evidence** — checks and artifacts required for closure.
6. **Allowed writes and external effects** — explicit mutation boundary; use
   `None` where appropriate.
7. **Stop and reapproval triggers** — conditions that halt autonomy.
8. **Recovery procedure** — how a failed or partial attempt is made safe.
9. **Final evidence and handoff** — completed before closure.

Dead Ends are required evidence whenever a path has been rejected. Record them
in an issue comment or an optional `## Dead Ends` section, including the failure
mechanism and the condition required to revisit the path. The optional heading
does not change the nine-section issue contract.

A work item is ready only when every hard blocker is closed, required approvals
are current, scope and evidence are decision-complete, and necessary systems
are safely reachable. Missing references, dependency cycles, or an open blocker
make it not ready. Closing a blocked issue is invalid and must be reversed.

### Dependency semantics

- The issue body's `Blocked by` list is the authoritative dependency
  declaration. GitHub native blocked-by relationships are a synchronized mirror,
  and reverse `Blocks`, Ready, Blocked, Project, and reporting views are derived.
- Readiness uses the referenced issue's live state. Markdown checkbox state is
  decorative and must be ignored; a stale unchecked box cannot keep a closed
  blocker open, and a checked box cannot conceal an open blocker.
- Every referenced issue must exist and its stable key must match. Missing,
  duplicate, self-referential, or cyclic dependencies fail closed.
- A body/native mirror mismatch is an automation or governance failure, not
  permission to choose the more convenient representation. Reconcile the mirror
  before execution.
- Sub-issues express decomposition only. They do not create execution order
  unless the body `Blocked by` list explicitly declares it.

After each sprint, update the current issue—not a local recap—with outcome,
exact evidence, decisions, Dead Ends, blockers, and changed dependencies.

## Private GitHub Free repository control boundary

The user selected a private personal GitHub Free repository in `GOV-002`.
GitHub therefore does not server-enforce protected `main`, required pull
requests, required status checks or reviews, CODEOWNERS approval, signed or
linear history, force-push denial, or branch-deletion denial. Project policy,
CI, templates, issue automation, repository settings, and local hooks must never
be described as equivalent enforcement.

While this boundary remains in effect:

1. Live collaborator, invitation, and deploy-key evidence shows `dachent` is
   the only verified direct Git writer, with no pending invitation or deploy
   key. The current interfaces cannot independently enumerate personal GitHub
   App installations; that is explicit residual risk, not proof of absence.
   Any known or observed repository-access app with contents/ref write authority
   requires reopening `GOV-002` before work continues.
2. Every change uses one non-main branch, one governing issue, and one pull
   request. Direct, forced, deletion, or repointing updates to `main` are
   prohibited, including through an API or administrative bypass.
3. Immediately before merge, the operator records the exact pull-request head
   SHA and verifies that Quality, Security, Data boundary, and Issue guard all
   completed successfully for that unchanged SHA. Missing, pending, cancelled,
   unexpectedly skipped, or failed evidence stops the merge.
4. GitHub Actions defaults remain read-only and unable to approve pull
   requests. Only the selected, full-SHA-pinned actions may run. Merge commit is
   the sole enabled GitHub UI merge method so provenance-preserving imports are
   not squashed.
5. The repository-owned pre-push hook is workstation defense-in-depth only. It
   is bypassable and does not protect another client, the GitHub UI, an API, or
   an administrator.
6. Any detected direct or forced `main` update freezes new work. Record a
   governance incident, compare remote history with the last accepted OID,
   recover through a governed corrective or revert pull request, and resume
   only after reconciliation is accepted. Never rewrite history as recovery.

Reopen `GOV-002` before adding a writer or write-capable app, when such an app
is discovered, changing the plan or repository visibility, enabling live
trading, or accepting a release that requires independently enforced controls.
The full decision and residual risk are recorded in
[ADR 0001](decisions/0001-github-free-main-governance.md).

## Quant-specific gates

These gates make the operating phases testable for an investment system. A
governing issue may strengthen them but may not silently omit or weaken them.

1. **Security and entitlement** — before restricted artifacts or provider
   access, prove data classification, credential handling, allowed egress,
   licensing/terms, and approved external effects. Secrets, sessions, account
   crosswalks, and real financial data never enter Git, issue text, or CI.
2. **Source provenance and coverage** — every input identifies its source,
   acquisition method, hash or immutable locator, adapter/schema version,
   declared provider/account/fact/time boundary, omissions, and a coverage
   result of `COMPLETE_WITHIN_DECLARED_BOUNDARY`, `PARTIAL`, or `FAILED`.
   Unknown coverage is not complete coverage.
3. **Authority, reconciliation, and publication** — prove exactly one selected
   authority for each provider, internal account, fact type, effective interval,
   and finalization state. Preserve overlaps and link reconciliations rather
   than overwrite evidence. Generations are create-only; partial or failed work
   cannot advance `CURRENT.json`; publication and cutover require hash/inventory
   parity plus a tested recovery state.
4. **Financial calculations** — version conventions for flows, fees, taxes,
   dividends, FX, timing, corrections, attribution, TWR, MWR, and composites.
   Preserve currency and decimal fidelity; verify referential integrity,
   deduplication, reconciliation equations where coverage permits, and
   independent hand-calculated micro-ledgers. Missing NAV, FX, flows, or signs
   makes an affected metric unavailable or explicitly qualified—never silently
   interpolated. Excel is controlled intake or a generated report, not the
   calculation authority.
5. **Broker execution** — initial authority is offline, dry-run, or separately
   approved PAPER only. Every submission requires an immutable order manifest,
   exact ledger/allocation references, environment and account allowlists,
   freshness and limit checks, approval, kill/reconciliation procedures, and an
   execution ledger. An unknown outcome halts retry. Live activation or a
   compensating order requires a separate, current governing issue and approval.

## Approval semantics

After Phases 0, 1, 2b, 3, 4, 5, 6, and 7, stop for explicit user approval. The
approval is a new comment on the governing issue in this form:

```text
PROCEED: Phase <completed> -> Phase <next>
Candidate/scope: <issue comment, commit, artifact, or bounded description>
```

Phase 8a has a separate `PROCEED: Phase 8a -> Phase 8b`; it authorizes execution
only inside the readiness record's writes, effects, limits, and candidate.
Approval is not transferable to another issue or candidate and expires when a
material target, scope, criterion, architecture, authority, effect, or
irreversible risk changes.

Phase approval never implicitly authorizes an external write, destructive
action, credential operation, source-data removal, live trade, compensating
order, or expansion beyond read-only/dry-run/PAPER behavior. The governing issue
must contain a separate approval comment that unambiguously names the exact
effect, target, and recovery or reconciliation procedure immediately before the
action. If that approval is absent or stale, stop.

## Phase 0 — Criteria and boundaries

Sharpen the draft goal with the user and relevant project evidence. Record
measurable success criteria, failure criteria, out-of-scope boundaries,
complexity, context needs, external/irreversible risks, applicable prior lessons,
and known Dead Ends. Do not invent product intent. Stop for `PROCEED`.

## Phase 1 — Catalog

Catalog relevant code, files, logs, inputs, outputs, systems, and dependencies as:

```text
[what | attempt | outcome: succeeded/failed/partial/unreached | known failure mechanism]
```

Include source identity and current authority where data or financial facts are
involved. Stop for `PROCEED`.

## Phase 2a — Evidence review

Classify catalog entries by relevance. Fully inspect high-relevance materials;
summarize lower-relevance items and record the rationale. Do not hydrate, open,
or expose restricted material unless the issue authorizes it and the applicable
policy gate is satisfied.

## Phase 2b — Failure autopsy

For existing or failed work, establish root causes, wrong decision points,
partial state, actual accomplishments, and evidence gaps. Create or update the
issue's Dead Ends with failure mechanisms and the condition required to revisit
each path. Stop for `PROCEED`.

## Phase 3 — Adversarial clarification

Stress-test the synthesis against project terminology and evidence. Expose
missing assumptions, boundary cases, unsafe authority expansion, data-coverage
claims, calculation ambiguity, and failure/recovery gaps. Add eliminated paths
to Dead Ends. Stop for `PROCEED`.

## Phase 4 — Targeted probes

Run only low-cost, high-impact probes within the issue boundary. Before each
probe, state the observation that would support or eliminate the path. Record
the actual result without retrofitting the expectation. Update Dead Ends and
stop for `PROCEED`.

## Phase 5 — Design

Develop viable approaches for the product/change, evidence package, workfolder,
and handoff. Compare tradeoffs and reject designs that conflict with Dead Ends
or the system boundaries. Record the selected design and why; do not yet turn it
into an execution checklist. Stop for `PROCEED`.

## Phase 6 — Architecture and sequence

Resolve the selected path's architecture, interfaces, dependency direction,
sequence, data authority, risks, recovery, compatibility, and remaining open
questions. A named planning tool may help but does not replace the outcome.
Stop for `PROCEED`.

## Phase 7 — Decision-complete issue plan

Write the execution plan into the governing issue and its governed sub-issues.
Every work item receives a stable key, outcome, material dependencies, approved
scope, acceptance evidence, allowed writes/effects, stop triggers, and recovery.
Add validation checkpoints and rollback/reconciliation triggers. Do not create a
parallel Markdown plan. Stop for `PROCEED`.

## Phase 8a — Readiness and authority preflight

Reconcile issue state, dependencies, directories, tools, permissions, declared
checks, and non-destructive reachability of required systems. Record material
pre-existing failures. Verify that worker context, writes, authority, and
external effects can be bounded. Set observable attempt, time, cost, context,
and fan-out limits. If readiness cannot be proven, mark the issue blocked and
stop. Otherwise record the readiness evidence and stop for the Phase 8a
`PROCEED`.

## Phase 8b — Bounded execution loop

Select one ready, authorized item and make the smallest coherent, preferably
reversible change. Run its declared checks and record exact evidence.

Retry only after a new diagnosis or a relevant candidate, artifact, input,
environment, permission, or gate change and only within approved limits. Never
loop until pass, weaken criteria, or turn an unknown broker outcome into a blind
retry. On a blocker, exhausted limit, unexpected external effect, or required
reapproval, preserve the safest recoverable state, hand off, and stop.

Delegate only when context, authority, writes, and effects are bounded. Send:

```text
[issue/item | outcome | scope | references and Dead Ends | allowed writes/effects | acceptance evidence | limits]
```

Require:

```text
[status | artifact/diff | checks/evidence | uncertainty | blockers/dependencies | next action]
```

Use one writer unless surfaces are demonstrably disjoint. Assign integration
ownership and verify the merged state. A worker's isolated pass is not proof of
integration.

At sprint close, update the issue and route exactly:

- Plan complete → Phase 8c.
- Ready, approved work remains within current limits → select the next item.
- No item is ready → mark blocked and stop.
- Limit, blocker, changed authority, or reapproval trigger → hand off and stop.

## Phase 8c — Integrated verification and handoff

Test the integrated result against Phase 0 criteria, failure criteria, and
out-of-scope boundaries. Run each declared gate once for an unchanged candidate;
rerun only after a relevant change. Add a fresh-context reviewer only when
impact, ambiguity, security, financial risk, or gameable checks justify it, and
give the reviewer authoritative criteria and integrated evidence rather than
maker rationale.

Complete the governing issue's final handoff with deliverables, exact checks,
remaining uncertainty, recovery state, final dependency status, Dead Ends, and
lessons delta. Append reusable lessons only to an already approved durable store;
otherwise retain them in the issue. Close the issue only when its acceptance
evidence is satisfied and no blocker remains.

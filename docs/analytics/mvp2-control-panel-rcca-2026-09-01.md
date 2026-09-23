# MVP-2 Control-Panel Root Cause and Corrective Action

**Date:** 2026-09-01

**Governing issue:** #374 (`MVP-AUDIT-001`)

**Repository baseline:** `main` merge `1bc1936254b494d78b5e85b979b085fb9ae88fd5`
from verified PR #383

**Disposition:** Product release incomplete; implementation merged, production
promotion not performed

## Executive finding

The operator was not looking at the MVP-2 candidate that had been developed and
tested. The stable launcher still selected an older schema-18 control-panel
generation. The epoch manifest and subsequent controls existed in a schema-21
candidate and, later, in source code, but were never proven at the actual
operator launcher.

The fundamental root cause was a fractured definition of completion. Code
readiness, evidence readiness, candidate generation, publication readiness,
and operator-visible presentation were allowed to pass as separate states.
The repository already had a release-builder authorization receipt, strict
publisher validation, atomic pointer/launcher publication, rollback, and
documented stable-launcher QA. The governing issue and product-level
completion claim did not require that existing path to be executed under
separate publication authority and followed by acceptance beginning at the
actual stable launcher.

This allowed agents to truthfully prove local properties while collectively
supporting the false product-level implication that “MVP-2 was delivered.”

## Impact

- The full epoch manifest was absent from the operator's active panel.
- Operator-critical implementation detail was reachable only indirectly.
- Source-reconciliation gaps were retained in data but not presented as a
  first-class actionable control.
- HWM coverage qualification and numerical HWM availability were visually
  conflated.
- Design, backtest, factor, and live-return evidence roles were collapsed into
  labels that did not explain why evidence was unavailable.
- Unresolved security-master identity appeared as missing tickers.
- Census, observation, calculation, evaluation, and publication dates were
  compressed into ambiguous “as of” language.

No evidence establishes that the user saw incorrect epoch numbers in the
stable launcher; the primary failure was that the reviewed epoch product was
not the product being displayed.

## Fact and inference boundary

| Statement | Classification | Evidence |
|---|---|---|
| The operator launcher selected schema-18 generation `41f41362...`. | Fact | Deployment-local `dataPortfolio/control-panel/CURRENT.json` and selected generation inventory. |
| Merged source contains the schema-21 epoch table and renderer. | Fact | `apps/portfolio-control-panel/index.html`; `apps/portfolio-control-panel/app.js`; PR #383 head `41e2838`. |
| Issue #374 excluded accepted-pointer movement and publication. | Fact | Governing issue #374 scope and allowed-effects sections. |
| The repository already implements authorization receipt validation, atomic publication, rollback, and launcher QA. | Fact | `tools/portfolio_release_builder.py`; `tools/portfolio_control_panel.py`; `docs/portfolio-control-panel.md`. |
| Candidate and synthetic-launcher tests do not prove the actual OneDrive launcher was promoted. | Fact | `tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py`; `tests/portfolio_control_panel/test_portfolio_control_panel.py`. |
| “Delivered” was used without terminal proof of separately authorized publication plus actual-root QA. | Inference supported by the preceding facts | Issue #374 remained open, the stable pointer remained schema 18, and no post-publication actual-root receipt was present. |
| Serial agents optimized bounded component gates without one owner accountable for terminal cutover evidence. | Process inference | Component evidence was completed and merged while the stable launcher remained unchanged; no issue record named a terminal cutover owner. |

## Recursive why-chain

### 1. Why was the epoch manifest missing?

The stable OneDrive launcher selected control-panel generation `41f41362...`,
whose schema-18 Risk view has no epoch-manifest table. The merged source now
contains the schema-21 epoch table in
`apps/portfolio-control-panel/index.html`, with rendering in
`apps/portfolio-control-panel/app.js`.

### 2. Why did the stable launcher still select the old generation?

The audit and implementation operated on create-only candidates. Issue #374
explicitly excluded accepted-pointer movement and publication. No later,
separately authorized release step promoted the verified candidate to the
stable launcher.

### 3. Why was the work nevertheless described as delivered?

Candidate tests proved that a supplied candidate could be built, verified, and
rendered. The candidate-report test intentionally accepts a hash-named report
candidate without a `CURRENT.json`. The stable-launcher test uses a synthetic
temporary publication root. Neither test proves the real OneDrive launcher was
advanced to the exact candidate.

### 4. Why did candidate success substitute for product success?

The process had multiple completion predicates:

- implementation and unit/integration tests;
- immutable evidence and independent numerical checks;
- a create-only panel candidate;
- GitHub pull-request readiness;
- publication authority and pointer movement;
- operator-visible browser acceptance.

The existing publisher could bind and publish them, but issue closure and the
product-level completion claim did not require separately authorized
publication followed by post-publication acceptance from the real launcher.
Synthetic publication-root testing was allowed to stand as the last automated
proof.

### 5. Why was the existing release path not enough?

The scaffold and serial-agent workflow organized work around component and
issue-local proofs. Ownership of the final operator outcome was implicit. Each
agent could close a bounded contract without owning the transition from
verified candidate to separately authorized publication and actual-root
acceptance. The high volume of intermediate control work increased confidence
in the candidate while obscuring that the product claim was not terminally
linked to execution of the controls already present.

### Fundamental root cause

Product-level completion semantics and issue lifecycle ownership did not
require one separately authorized cutover followed by actual-root operator
acceptance before “delivered” could be claimed. No named integration owner was
responsible for carrying the candidate through that terminal evidence. The
system controlled candidate construction more rigorously than it controlled
the claim that the candidate was the product in use.

## Contributing product-design causes

### Navigation reachability

The Signals/Implementation view exists, but the template deliberately omits a
`signals` sidebar entry. A command-page button is the only explicit route.
`tests/portfolio_control_panel/test_portfolio_control_panel.py` codifies this
omission as expected behavior. Therefore the hidden table is not a rendering
accident; it is a faulty operator requirement that passed its test.

### Gap presentation

The schema-21 JavaScript already provides an operator queue, remediation
routes, and Exception, State, Required evidence, and Destination columns. The
remaining deficiency is narrower: stable gap identifier, severity, affected
business outcome, and schema enforcement are incomplete. The schema-18 stable
product exposes less of this than the merged schema-21 source. Evidence can
therefore be technically present yet operationally buried or inconsistently
identified.

### HWM semantics

The product currently marks `AS_HELD_COMPOSITE_HWM` as
`REQUIRED_NOT_READY` in `generated/financial-mvp/current-product.md`. The panel
also reports governed perimeter/coverage states under the broad “Systematic
HWM” label. Perimeter qualification is not a numerical financial HWM. The
missing number is a legitimate evidence/admission gap; the overloaded label is
a product defect.

### Evidence-role semantics

Design configuration, exact-version backtest, provider-live comparison,
account-live return, and factor model are separate evidence roles. The current
contracts correctly refuse to substitute one for another, but the panel often
collapses several failure states into “Unavailable” or “Model unavailable.”
This hides the distinction between nonexistent evidence, captured but
unaccepted evidence, version mismatch, stale factors, and inapplicability.

### Holding identity

The required `portfolio-security-master` is still `pending-census` and
`REQUIRED_NOT_READY` in the financial-MVP source contract. Missing tickers are
therefore an upstream identity gap, not merely a table-rendering omission. The
panel should explicitly display “security master unresolved” instead of a
blank value that resembles a UI bug.

### Date semantics

`2026-08-16` is the accepted census/capture date for several provider
generations. It is not the common calculation-complete-through date; the owner
calculation cutoff is `2026-08-13`. The UI did not enforce distinct labels for
census date, provider observation end, calculation cutoff, freshness
evaluation, and panel publication.

## What was not the root cause

- The absence of a provider refresh after 2026-08-13 did not create the missing
  epoch panel. It limits evidence availability but cannot explain why a newer
  candidate was not selected by the launcher.
- Fail-closed design/backtest/factor/HWM/ticker states are not necessarily code
  defects and must not be repaired by relabeling or inference.
- Hashes and clean-room recomputation were valuable controls, but they proved
  candidate content—not stable-launcher adoption.
- Additional ACL, hostile-operator, encryption, or credential controls would
  not correct this failure in the stated isolated operating environment.

## Fundamental corrective action

Extend the existing release-builder authorization, panel publisher, rollback,
and launcher-QA receipt so the governing issue cannot reach a delivered or
complete state until post-publication acceptance starts from the actual stable
launcher. Do not create a parallel acceptance runtime. The terminal receipt
must bind:

1. source commit and reviewed pull-request head;
2. report/control-panel generation identifiers and exact byte identities;
3. the real stable `CURRENT.json` target;
4. the release authorization receipt;
5. browser QA beginning at the real launcher path;
6. the operator contract below.

The terminal release/issue gate fails if the stable pointer selects a different
generation, if publication is unauthorized, if independent verification is
not performed, or if any required operator surface is unreachable. `NOT_READY` and
`NOT_PERFORMED` may be valid product statuses, but may not satisfy a claim that
the affected outcome is complete.

### Operator contract

- Risk exposes the complete account × strategy epoch manifest.
- Every operator-critical view has a visible primary-navigation entry.
- Every retained source gap shows its identifier, severity, affected outcome,
  required evidence, and remediation route.
- HWM perimeter coverage and numerical financial HWM are separate controls.
- Evidence roles retain separate status and reason fields.
- Missing security-master identities are explicit, not blank.
- Census, observation, calculation, evaluation, and publication dates have
  separate labels.

## Preventive controls

- Name one release integration owner from candidate freeze through launcher
  verification.
- Prohibit “delivered,” “published,” or “MVP complete” claims from candidate or
  source-code tests alone.
- Make actual-root browser QA and real-pointer verification required release
  evidence, not synthetic fixture evidence.
- Test navigation reachability as an operator outcome; do not merely test that
  intentionally hidden sections exist in the DOM.
- Generate one typed availability projection for design, backtest,
  provider-live, account-live, factor, HWM, holdings identity, and gaps, and
  make the report and panel project it rather than reinterpret raw statuses.
- Retain unavailable states until their source contracts are admitted; never
  replace them with guesses to make the panel look complete.

## Closure evidence

This RCCA closes only when a separately authorized release proves all of the
following on one unchanged candidate:

- the real stable launcher resolves to the intended generation;
- actual-root browser QA reaches every critical view and the complete epoch
  table;
- the release receipt binds source, report, panel, pointer, and verification
  identities;
- typed gap, HWM, evidence-role, holding-identity, and date labels satisfy the
  operator contract;
- any remaining unavailable outcome is explicit and supported by its source
  contract;
- the governing issue records the exact candidate, commands, results, and
  recovery state.

PR #383 merges the implementation and control path. It does not, by itself,
satisfy this closure condition or publish a new stable panel.

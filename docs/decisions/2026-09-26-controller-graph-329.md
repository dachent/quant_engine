# Decision: #329 vs. the controller-graph gate

- **Status:** Accepted
- **Date:** 2026-09-26
- **Governing issues:** [#329](https://github.com/dachent/quant_engine/issues/329) (RCCA-006), [#444](https://github.com/dachent/quant_engine/issues/444) (GOV-CONSUMER-PROPAGATION-001)
- **Decision owner:** WS4 gates lane, quant_engine corrective program

## The contradiction

`#329` (RCCA-006), still open, states as its corrected RCCA answer:

> the repository has an executable downstream pipeline skeleton, but no single
> authoritative, executable **current-product composition and
> outcome-acceptance authority**

`docs/controller-graph.md` and `tools/portfolio_controller_graph.py` describe
and implement exactly the thing the first half of that sentence says does not
exist: a single, executable, CI-blocking projection of the product's
composition. Concretely:

- `docs/controller-graph.md:14-16` binds `STAGE_DEPENDENCIES`/`ATTENDED_STAGES`
  (`tools/portfolio_pipeline_materialize.py`) against
  `_PORTFOLIO_STAGE_DEPENDENCIES`/`_PORTFOLIO_STAGE_ORDER`
  (`packages/core/src/quant_engine/core/pipeline.py`) and requires the
  report→panel edge, "so the MVP path cannot be silently redirected through a
  workbook."
- `docs/controller-graph.md` (seam section) requires the core coordinator's
  `execute_pipeline` to call both `_validate_commit_authorization` and
  `commit_release` — "the coordinated publication sink protected by the
  existing owner-publication gate" — and requires `build_report` →
  `build_report_candidate` → `_tool("portfolio_report.py", ...)` and
  `build_panel_candidate` → `_tool("portfolio_control_panel.py", ...)`, with "a
  matching string literal or a call to `str` is not an edge."
- This is wired as a blocking CI step, not advisory:
  `.github/workflows/quality.yml:72` ("Validate source-bound controller
  graph"), no `continue-on-error`, run against the exact changed-range SHAs
  every pull request and push to `main`.
- The graph's own scope note (`docs/controller-graph.md`, "deliberately not an
  execution authority" section) is explicit that it proves static wiring, not
  runtime numerics or outcome acceptance: "AST cannot prove dynamic runtime
  ordering, source admission/finality, recovery/reuse behavior, or financial
  numerics."

So the "composition" half of #329's claim is false as currently worded: an
authoritative, executable, CI-enforced composition binding already exists and
has caught real drift (`docs/repo-map.md` §5 cites it as the mechanism behind
the #328/#329 family; the corrective-program repo map records it as blocking).
The "outcome-acceptance authority" half is a different, narrower, and still
real gap: whether a *specific generation's computed outputs* (HWM, invested
capital, decomposition, ...) are accepted, not whether the code that would
produce them is wired correctly. That gap is #468 (REL-READINESS-001) / #469
(REL-GATE-001), and this lane's `tools/check_release_validation_findings.py`
(T1.4, task 3) is the generalized precondition that closes it: release
publication now refuses unless a `validate_generation` findings file for the
exact generation being published shows zero `BLOCKER_*`.

## Resolution adopted

Keep what CI enforces unless it is wrong. `tools/portfolio_controller_graph.py`
is correct, tested, and already blocking; nothing here found a defect in it.
Treat the composition-authority half of #329 as **superseded by evidence
already in the repository**, not as open work, and treat the remaining
outcome-acceptance half as **owned by #468/#469 and now enforced by the
release-validation-findings precondition** (T1.4, task 3), not by a new or
parallel composition mechanism.

This matches #329's own review disposition (2026-09-17, already recorded on
the issue): *"Verify the delivered composition outcome against the real
source-bound vertical, then close with evidence. Do not build another
composition scaffold."* — i.e. the owner's own prior instruction already rules
out the alternative this decision rejects below.

## Rejected alternative

- **Build a new/parallel composition or outcome-acceptance scaffold** (a
  second graph, a shadow orchestrator, a new schema for "the" pipeline):
  rejected. It would duplicate `tools/portfolio_controller_graph.py`'s already
  enforced, tested static contract, and would not address the actual remaining
  gap (per-generation outcome acceptance), which is a runtime/data question
  the graph explicitly disclaims authority over and which #468/#469 and this
  lane's release-validation-findings gate already own.

## Follow-up

- `#329` should be closed (or re-scoped to name only the outcome-acceptance
  half) with a pointer to this record, `docs/controller-graph.md`, and
  `.github/workflows/quality.yml:72`, once the owner has reviewed this
  decision — closing issues is outside this lane's write scope.
- If `tools/portfolio_controller_graph.py` is ever found to have a real gap
  (an edge it should bind but does not), the correction belongs in that tool
  and its tests, not in a new mechanism.

## References

- `docs/controller-graph.md`
- `tools/portfolio_controller_graph.py`
- `.github/workflows/quality.yml:72`
- [#329](https://github.com/dachent/quant_engine/issues/329), [#468](https://github.com/dachent/quant_engine/issues/468), [#469](https://github.com/dachent/quant_engine/issues/469)
- `tools/check_release_validation_findings.py` (this lane, T1.4 task 3)

## Appendix: per-issue determination (T1.4 task 1)

Recorded in-repo per cross-family review (PR #481, fix round 1): rule,
enforcement point found, blocking vs. advisory, and the key file:line, for
each issue this lane's brief named.

| # | Rule | Enforcement found before this PR | Blocking? | Key file:line |
|---|---|---|---|---|
| **#444** GOV-CONSUMER-PROPAGATION-001 | AGENTS.md A1.3: a contract/schema change must enumerate consumers still reading the old meaning, in the same PR, with a regression test; widen to retained copies outside the repo. | `tools/check_retained_authority_pins.py` computed the right answer and had passing unit tests, but was invoked nowhere (no workflow, no `ops/` script). | Advisory / dead code (issue's own words: "no merge check, no test, nothing that fails when it is skipped"). | `tools/check_retained_authority_pins.py` (whole file, unused pre-PR); `tools/portfolio_controller_graph.py:108` |
| **#469** REL-GATE-001 | Release must refuse when computed conformance/readiness reports a required outcome or source not-ready. | `pipeline.py:3759-3763`: `ready_for_execution` derives from only 3 reasons; conformance/HWM reasons go to `completion_blocking_reasons` (`:3756`), written to the summary (`:3866`) and read by nothing. | Advisory (issue's own text: "gates nothing"). | `packages/core/src/quant_engine/core/pipeline.py:3736-3763, 3866` |
| **#326** RCCA-004 | Applicable unresolved corrections must block release; prove re-derivation. | `tools/portfolio_control_panel.py:479` reads a "#322/#326 producer envelope" at the panel consumer seam — a read, not a block. Disposition "NEXT" (not implemented). | Advisory / not implemented. | `tools/portfolio_control_panel.py:479` |
| **#328** RCCA-005 | Restore 4 decomposition lenses for the MVP; governed by #355/#409. | Data/analytics-completeness defect, not a gate/enforcement issue. Out of WS4 scope; no code change made for it here. | N/A (not a gate) | — |
| **#329** RCCA-006 | Claims no executable composition/outcome-acceptance authority exists. | Composition half is stale (see decision above); outcome-acceptance half is real, now closed by the release-validation-findings gate. | Blocking (composition half) — `.github/workflows/quality.yml:72`. | `docs/controller-graph.md`; `.github/workflows/quality.yml:72` |
| **#421** OPS-SCAFFOLD-001 | Umbrella: known dev/run failures prevented/detected by the engine's own execution boundaries (8 sub-items). | Mixed/partial, owned piecemeal by numbered children; not one mechanism. | N/A (umbrella) | — |

**#326 → finding class, pinned by the cross-lane contract**
(`state/briefs/findings-contract.md`, echoed in
`tools/check_release_validation_findings.py`'s module docstring): an
applicable, unresolved correction for an output in the generation is a
`BLOCKER_AUTHORITY` finding, `check_id` prefix `correction.`. This is the rule
the acc-validator lane's `tools/validate_generation.py` must implement for
#326 to actually close through the release-validation-findings gate; the gate
enforces "zero `BLOCKER_*`" generically, and this is the specific mapping that
makes #326 one of the things it enforces.

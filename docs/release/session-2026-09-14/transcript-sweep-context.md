# Reconciliation context (gathered by the orchestrator, verified from live state)

## Repository
- worktree: C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo
- branch: mvp2/293-xlsx-determinism
- 87 commits ahead of origin/main
- HEAD at time of gathering: 6e6bd86

## Open PRs (verified via ghx)
- #402  mvp2/293-xlsx-determinism  "Make the report generation identity independent of where it was built (#293)"
        NOTE: this is THIS branch. Title says #293 xlsx determinism, but the branch now carries 87 commits
        spanning the EU hybrid change, the FLS O(T^2) fix, workbook/sync work, formatter+mypy sweep,
        and the release-authority predecessor-repair change. Scope has drifted badly.
- #400  mvp2/374-capital-anchors   "Deliver the MVP-2 vertical: current-epoch manifest, verified epoch orchestration, green panel"

## Open issues: 60 total. Ones most likely to already cover swarm findings:
- #408 Decomposition refresh is O(T^2) (walk-forward) -- P4/P7 parallelisation items live here
- #407 Deferred: state-conditioned portfolio fragility
- #406 pre-pr QC cannot run: merge_readiness.py requires a gh login this workstation forbids
- #405 The contract requires a LIVE_PLUS_BACKTEST surface no cycle has ever produced
- #404 Cover the post-build semantic gate key set with a test
- #403 history_corrections cites closed issues and contradicts the admitted ledger
- #401 Resolved applicable corrections and empty registries clear the #326 gate
- #399/#398 CAPITAL_ANCHOR unmaterialised for ibkr and matrixport
- #396 Leverage facts have no materialization path
- #364 End-to-end coverage of the lens-layout producer/consumer seam
- #355 Deferred: robust collinearity, orthogonal attribution, sparse decomposition
- #332 Prove financial MVP in CI, workstation, and release QC
- #328 Restore current factor/style/sector/industry decomposition for the MVP
- #326 Block releases on applicable corrections and prove re-derivation
- #323 Use short disposable paths for workstation and Excel QC
- #321 Remove machine-absolute workbook runtime dependencies
- #304 Validate plausibility of facts consumed by the financial MVP
- #300 RCCA: flattened generation dirs + out-of-band worker publish to live roots
- #299 No sanctioned path to add roots to a committed release line
- #298 Retention classes for pipeline inputs + reference-check before deletion
- #297 Provenance build must recompute/verify strategy version_ref
- #296 Stage DAG runbook: dependencies, roots, identity bindings per stage
- #294 Bundle artifact-role manifest + cross-artifact semantic validation + diagnostics
- #293 Root-cause the stage/publish generation-identity mismatch
- #292 Deferred: projection digests only on measured seam evidence
- #409 robuxio provider track record (NOT_CAPTURED deferral)

## Already committed this session (do NOT re-file as new work)
- de8ab7e docs/release/RELEASE-HARDENING-BACKLOG.md  -- 12 sections, the orchestrator's own capture
- 6e6bd86 production spec can carry a predecessor-authority-repair gate
- c8c16f4 dependence policy repinned onto rebuilt layered history
- 57b3625 workbook parity via the panel's embedded payload
- 1894bb9 formatter + mypy sweep (ruff/mypy/format now green)

## Live blocker state (verified from bytes, not asserted)
- Production: 20/20 pointers UNCHANGED. Nothing has ever been published.
- portfolio-report generation a5cfa66d... was rewritten in place by the SharePoint sync client
  after publication. Sealed inventory 3b4a5607..., actual cf2f6c25....
- 970 distinct copies of that workbook hashed; none reproduce the sealed inventory. Bytes unrecoverable.
- Owner chose Option A. The mechanism already existed (predecessor-authority-repair gate,
  pipeline.py _verify_predecessor_repair_gate) and was merely unreachable from the production spec.

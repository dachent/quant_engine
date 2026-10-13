# Period refresh runbook: new data -> attended run -> release commit

The complete, evidence-backed procedure for refreshing the control panel with a
new period of provider data and committing the release. Every command below was
executed for the 2026-09-17-cutoff release (`ab216539b6f7dd1098b8c496ab86cf2eb
0bb19017038edb7fc9aa04b69f1077e`, committed 2026-10-13); receipts are named at
each step. Read `docs/attended-generation-runbook.md` first for the run-stage
details; this file covers the full cycle including the release tail.

## 0. Invariants that burned before (read first)

- **One frozen SHA per cycle.** The attended run, its resume, and the release
  commit must all execute from the same repo HEAD, clean tree. Any code fix
  mid-cycle (including validator fixes) forces the affected steps to re-run.
- **Generation ids bind `repository_sha`.** Two runs at different SHAs with
  cold caches produce different generation ids for identical content (history
  generation -> decomposition plan bundle -> lens generations -> panel
  candidate hash all move). Identical-candidate proof across roots requires the
  same SHA plus a warm cache. This is the allowed identity class, not drift;
  prove it with a leaf-level dashboard diff (values equal, only hex ids move).
- **`execute --commit` is not revertible by tooling.** Only run it after the
  dry-run inspection matched and the commit authorization was emitted.
- **Post-commit-only checks refuse before the commit by design:**
  `--profile post-release-verify` and `data_recovery_rehearsal.py` both
  require a COMMITTED journal. A pre-commit refusal there is sequencing, not
  failure.
- **Never hand-write or move a governed artifact** (findings, semantic gate
  payload, specs, release journal). Every step below has a producer command.

## 1. Capture the new period data

Per-provider, into the retained OneDrive source roots (see
`docs/data-policy.md`, `docs/data-authority.md`, and the provider docs under
`docs/providers/`). Requirements carried from the 09-17/09-18 cycle:

- Every capture needs a `generation.json` / finality evidence record
  (`run-observation.json` row: provider_data_date, finality basis, capture
  window for native-label providers).
- Native-label providers (Matrixport, Schwab, Quantbase) pin their own
  observed boundary; statement providers (IBKR flex) pin the label date.
- The newest `provider_ledger` generation must be re-admitted (or carried)
  against the current `financial-mvp-source` contract before anything else
  consumes it: `materialize` refuses on ledger/contract identity mismatch.

## 2. Rebuild the admission chain (ledger -> baseline -> history)

If any input moved, the dependent generations must be re-derived in order with
the retained-cache root warm so unchanged generations are reused (zero-refit):

1. Ledger: provider ledger generation + `admit-or-declare-receipt.json`.
2. Baseline: `baseline-spec.json` -> baseline generation (strategy NAV +
   derived custody cash per OWNER_318).
3. Layered history: binds `repository_sha` (see 0); rebuild at the cycle's
   frozen HEAD.
4. Dependence, attribution, calendar authority: re-derive against the new
   ledger/baseline identities (pin matrix; the runner refuses stale pins).

Pin every retained generation into the input spec (the `chain2/in/spec.json`
pattern: `provider_ledger_generation_root`, `expected_staged_baseline`,
`retained_verified_generations`, `plans`, `calendar_authority`).

## 3. Attended run

From the frozen run worktree (repo root = the run HEAD, `--python` = that
worktree's venv):

```
.venv\Scripts\python.exe tools/portfolio_pipeline_materialize.py attended ^
  --repo-root <run-worktree> --python <run-worktree>\.venv\Scripts\python.exe ^
  --workspace-root C:\Dev\worktrees\quant-engine-completion ^
  --output-root <cycle>\out --retained-cache-root <cycle>\cache ^
  --requested-as-of <cutoff> --evaluated-on <evaluation-date> ^
  --preparation-spec <input-spec.json> --qa-hold --execute
```

- Second run (different output root, `--retained-cache-base <first>\cache`):
  proves root-independence AND zero-refit (check
  `<out>\stage-results\analytics.json` `work_counters` all zero on the warm
  run). One cold + one warm run is the full proof set.
- The run stops at the panel QA hold. Run browser + interaction QA
  (`apps/portfolio-control-panel/browser_qa.mjs` and `interaction_qa.mjs`;
  both need `PLAYWRIGHT_MODULE` and optionally
  `PLAYWRIGHT_EXECUTABLE_PATH` env vars; `--expected-calculation-cutoff` is
  REQUIRED; the pinned Playwright package lives under
  `C:\Dev\scratch\quant_engine\_from-onedrive\.codex-bootstrap\playwright-panel`).
  A 30s `page.goto` timeout is a machine-load symptom, not a defect — check
  CPU for orphaned `pyenv exec python -` stdin scripts first.
- Native QA: `tools/portfolio_control_panel.py qa --candidate-dir <candidate>
  --qa-dir <qa-dir>` (11 checks; refuses on failure).
- Resume: same attended command with `--execute --resume --qa-dir <qa-dir>`.
- Validate the panel generation: `tools/validate_generation.py <gen-dir>
  --expected acceptance/expected-baseline.json --cutoff <cutoff> --crosswalk
  <epoch gen>\authority\account-assignment-crosswalk.json`. Zero BLOCKER_* is
  the gate.

## 4. Release tail (the ceremony)

Run root = the passed run's `out`. Move aside any stale tail outputs first
(`pipeline-spec.json`, `portfolio-orchestration-contract.json`,
`financial-mvp-*` contracts, `evidence/semantic-details.json`) — the tail
regenerates them.

1. **Vertical recipe** = the attended run's own
   `<out>\preparation\release-recipe.json` (NOT the CLI
   `portfolio_release_builder.py recipe`, which lacks the run's clock fields).
2. **Pipeline recipe:** `materialize recipe --vertical <release-recipe.json>
   --proof-root <out> --qa <qa-dir> --pipeline-id <id> ... --output
   <cycle>\pipeline-recipe.json`. If CORRECTION_AUTHORITY_REQUIRED: draft
   registries (`ops/mvp2-release/scripts/draft_correction_registries.py
   --run <out> ...`) and re-run.
3. **Pipeline spec:** `materialize spec --recipe <pipeline-recipe.json>
   --output-root <out> --spec <out>\pipeline-spec.json` plus the governed
   HWM/capital inputs from `<out>\preparation`, the ledger admission receipt,
   the financial input contract, `--portfolio-root` and
   `--retained-cache-root`. The spec must sit in the prepared root (the
   production spec reads the orchestration contract beside it).
4. **Semantic gate:** `materialize semantic --recipe <pipeline-recipe.json>
   --output <out>\evidence\post-build-semantic.json`. Expect all 8 checks
   PASS including `financial_identities` (NAV + derived custody cash).
5. **Production spec:** build per `materialize` (production-spec path;
   reads `<prepared>\evidence\post-build-semantic.json`). Do NOT pass
   `--financial-input-contract` here — the consumer's strict key set rejects
   the extra `input_contract` key (known producer/consumer seam; the flag is
   redundant because the input contract IS the product contract).
6. **Release-preflight:** `tools/portfolio_qc.py --profile release-preflight
   --spec <production-spec.json> --evidence-dir <production>\evidence
   --emit-commit-authorization <production>\commit-authorization.json`.
   Must PASS and emit `{"evidence_sha256": <spec sha>, "gate_id":
   "owner-publication-authorization"}`.
7. **Validation findings (the commit gate requires all 20 targets):**
   `tools/validate_generation.py <dataPortfolio>\control-panel\generations\<gid>
   --expected acceptance/expected-baseline.json --cutoff <cutoff>` (writes the
   panel findings), then `tools/validate_release_targets.py --spec
   <production-spec.json> --release-id <release-id>` (writes the other 19;
   refuses unless the panel findings are clean). Missing findings files are
   the PR481 commit-gate refusal.
8. **Plan + dry run:** `tools/data_pipeline.py plan --spec <spec>` (must show
   `ready_for_execution: true`, zero blocking reasons), then `execute
   --spec <spec>` WITHOUT `--commit`: prepares the release, moves no pointer.
   Snapshot `CURRENT_RELEASE.json` + root `CURRENT.json` files before/after.
9. **The real commit (owner-authorized only):** `tools/data_pipeline.py
   execute --spec <production-spec.json> --commit --commit-authorization
   <commit-authorization.json>`.
10. **Post-commit:** `--profile post-release-verify` (with `--spec`,
    `--preflight-receipt`, `--evidence-dir`), then
    `tools/data_recovery_rehearsal.py --spec <spec> --release-id <id>
    --output-root <cycle>\recovery-rehearsal --harness-code-commit <HEAD>`.
    Both PASS with a COMMITTED journal.

Cross-family code reviews (Kimi K3 + GLM via Fireworks) for any code change;
merges only through `prmerge` (verdict file convention: last line
`VERDICT: CLEAN (head <40-hex>)`, filename `pr<N>-<tag>.<family>.review.md`,
overruled rounds moved to `reviews/superseded/`).

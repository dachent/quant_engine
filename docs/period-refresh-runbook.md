# Period refresh runbook: new data -> attended run -> release commit

The complete procedure for refreshing the control panel with a new period of
provider data and committing the release. Every command below was executed for
the 2026-09-17-cutoff release (release id `ab216539b6f7dd1098b8c496ab86cf2eb
0bb19017038edb7fc9aa04b69f1077e`, recorded in the coordination root's
`CURRENT_RELEASE.json`, released 2026-10-13). Read
`docs/attended-generation-runbook.md` first for the run-stage details; this
file covers the full cycle including the release tail. Where a flag is not
shown, run the subcommand with `--help` and read the argparse source — flags
change between cycles and this document is not regenerated automatically.

## 0. Invariants that burned before (read first)

- **One frozen SHA per cycle.** The attended run, its resume, and the release
  commit all execute from the same repo HEAD, clean tree. Any code fix
  mid-cycle (including acceptance/validator fixes) forces the affected steps
  to re-run at the new SHA.
- **Generation ids bind `repository_sha`.** Two runs at different SHAs with
  cold caches produce different generation ids for identical content (layered
  history -> decomposition plan bundle -> lens generations -> panel candidate
  hash all move). Identical-candidate proof across roots requires the same SHA
  plus a warm cache. This is the allowed identity class, not drift; prove it
  with a leaf-level dashboard diff (values equal, only hex ids move).
- **`execute --commit` is not revertible by tooling.** Run it only after the
  dry-run inspection matched and the commit authorization was emitted.
- **Post-commit-only checks refuse before the commit by design:**
  `--profile post-release-verify` and `tools/data_recovery_rehearsal.py` both
  require a COMMITTED journal. A pre-commit refusal there is sequencing, not
  failure.
- **Never hand-write a governed artifact** (findings, semantic gate payload,
  specs, release journal). Every step below has a producer command; use it.
  (Moving aside regenerable tail outputs before a re-run — see the section-4
  preamble — is not an edit of a governed artifact: the tail regenerates
  them.)

## 1. Capture the new period data

Per-provider, into the retained OneDrive source roots. The provider docs
(`docs/providers/`), `docs/data-policy.md` and `docs/data-authority.md` govern
what a capture must carry; the operational vocabulary (finality basis, label
selection, capture windows) lives in the provider capture tooling and the
run-observation records it writes. Requirements carried from the 09-17/09-18
cycle:

- Every capture needs a generation record + finality evidence in the
  source-phase provenance (provider_data_date, finality basis, and a capture
  window for natively observed providers).
- The newest `provider ledger` generation must be (re-)admitted against the
  current `financial-mvp-source` contract before anything consumes it: the
  runner refuses on ledger/contract identity mismatch.

## 2. Rebuild the admission chain (ledger -> baseline -> history)

If any input moved, re-derive the dependent generations in order with the
retained-cache root warm so unchanged generations are reused:

1. Ledger: provider ledger generation + `admit-or-declare-receipt.json`.
2. Baseline: `baseline-spec.json` -> baseline generation (strategy NAV plus
   derived custody cash per OWNER_318).
3. Layered history: binds `repository_sha` (see 0) — rebuild at the cycle's
   frozen HEAD.
4. Dependence, attribution, calendar authority: re-derive against the new
   ledger/baseline identities (the runner refuses stale pins).

Pin every retained generation into the input spec (the `chain2/in/spec.json`
pattern: `provider_ledger_generation_root`, `expected_staged_baseline`,
`retained_verified_generations`, `plans`, `calendar_authority`).

## 3. Attended run

From the frozen run worktree (repo root = the run HEAD, `--python` = that
worktree's venv):

```
.venv\Scripts\python.exe tools/portfolio_pipeline_materialize.py attended ^
  --repo-root <run-worktree> --python <run-worktree>\.venv\Scripts\python.exe ^
  --workspace-root <workspace root> ^
  --output-root <cycle>\out --retained-cache-root <cycle>\cache ^
  --requested-as-of <cutoff> --evaluated-on <evaluation-date> ^
  --preparation-spec <input-spec.json> --qa-hold --execute
```

- Second run (different output root, `--retained-cache-base <first>\cache`
  on `--retained-cache-root <second>\cache`): proves root-independence AND
  zero-refit. Check `<out>\stage-results\analytics.json`
  `numerical_producer_evidence.work_counters`: all zero on the warm run.
  One cold + one warm run is the full proof set.
- The run stops at the panel QA hold. Run browser + interaction QA
  (`apps/portfolio-control-panel/browser_qa.mjs`, `interaction_qa.mjs`;
  both require env `PLAYWRIGHT_MODULE` (and optionally
  `PLAYWRIGHT_EXECUTABLE_PATH`) and the required arg
  `--expected-calculation-cutoff`). `interaction_qa.mjs` uses Playwright's
  default 30s `page.goto` timeout: a timeout there is a machine-load
  symptom more often than a defect — check for orphaned
  `pyenv exec python -` stdin scripts pegging CPU first.
- Native QA: `tools/portfolio_control_panel.py qa --candidate-dir <candidate>
  --qa-dir <qa-dir>` (11 checks; refuses on failure).
- Resume: the same attended command with `--execute --resume
  --qa-dir <qa-dir>`.
- Validate the panel generation: `tools/validate_generation.py <gen-dir>
  --expected acceptance/expected-baseline.json --cutoff <cutoff>
  --crosswalk <epoch-generation>\authority\account-assignment-crosswalk.json`.
  Zero `BLOCKER_*` is the gate.

## 4. Release tail (the ceremony)

Run root = the passed run's `out`. If a prior tail attempt left outputs in
the run root, move them aside first (`pipeline-spec.json`,
`portfolio-orchestration-contract.json`, the `financial-mvp-*` contracts,
`evidence/semantic-details.json`) — they are regenerable tail outputs and the
new tail would collide with them.

1. **Vertical recipe** = the attended run's own
   `<out>\preparation\release-recipe.json` (NOT the CLI
   `portfolio_release_builder.py recipe`, which lacks the run's clock fields).
2. **Pipeline recipe:**
   `materialize recipe --vertical-recipe <release-recipe.json>
   --proof-root <out> --qa-dir <qa-dir> --pipeline-id <id>
   --financial-mvp-contract <product contract json>
   --financial-mvp-source-matrix <source matrix json>
   [--correction-registry <r.json> --rederive-receipt-registry <r.json>]
   --output <cycle>\pipeline-recipe.json`.
   `CORRECTION_AUTHORITY_REQUIRED` is cleared by drafting the registries
   (see `ops/mvp2-release/scripts/draft_correction_registries.py`) and
   re-running with them.
3. **Pipeline spec:** `materialize spec --recipe <pipeline-recipe.json>
   --output-root <out> --spec <out>\pipeline-spec.json` plus the governed
   HWM/capital inputs from `<out>\preparation`, the ledger admission receipt,
   `--financial-input-contract`, `--portfolio-root`, and
   `--retained-cache-root`. The spec must sit in the prepared root (the
   production spec reads the orchestration contract beside it).
4. **Semantic gate:** `materialize semantic --recipe <pipeline-recipe.json>
   --output <out>\evidence\post-build-semantic.json`. Expect every check
   PASS, including `financial_identities` (NAV + derived custody cash).
5. **Production spec** (all seven required flags; there is no
   auto-discovery of the semantic gate — pass its output path):
   `materialize production-spec --recipe <pipeline-recipe.json>
   --prepared-root <out> --semantic-gate <out>\evidence\post-build-semantic.json
   --portfolio-root <live dataPortfolio root> --state-root <run retained cache
   root> --spec <production-spec.json> --financial-artifacts
   <cycle>\production\financial-artifacts.json`.
   `--financial-artifacts` comes from `ops` (see
   `build_financial_artifacts.py --run <out> --cycle <cycle> --out ...`).
   Do NOT pass `--financial-input-contract` here unless it truly differs
   from the product contract: the consumer's strict key set rejects the
   extra `input_contract` key (known producer/consumer seam).
6. **Release-preflight:** `tools/portfolio_qc.py --profile release-preflight
   --spec <production-spec.json> --evidence-dir <production>\evidence
   --emit-commit-authorization <production>\commit-authorization.json`.
   Must PASS and emit `{"evidence_sha256": <spec sha>, "gate_id":
   "owner-publication-authorization"}`.
7. **Validation findings (the commit gate requires findings for all 20
   release targets):** `tools/validate_generation.py
   <dataPortfolio>\control-panel\generations\<gid>
   --expected acceptance/expected-baseline.json --cutoff <cutoff>` (writes the
   panel findings), then `tools/validate_release_targets.py --spec
   <production-spec.json> --release-id <release-id>` (writes the other 19;
   refuses unless the panel findings are clean). Missing findings files are
   the PR481 commit-gate refusal.
8. **Plan + dry run:** `tools/data_pipeline.py plan --spec <spec>` (must
   show `financial_mvp.ready_for_execution: true`, zero blocking reasons),
   then `execute --spec <spec>` WITHOUT `--commit`: prepares the release,
   moves no pointer. Snapshot `CURRENT_RELEASE.json` and the root
   `CURRENT.json` files before/after to prove nothing moved.
9. **The real commit (owner-authorized only):** `tools/data_pipeline.py
   execute --spec <production-spec.json> --commit --commit-authorization
   <commit-authorization.json>`.
10. **Post-commit:** `tools/portfolio_qc.py --profile post-release-verify
    --spec <spec> --preflight-receipt <preflight receipt json>
    --evidence-dir <post-release evidence dir>`, then
    `tools/data_recovery_rehearsal.py --spec <spec> --release-id <id>
    --output-root <cycle>\recovery-rehearsal --harness-code-commit <HEAD>`.
    Both PASS against a COMMITTED journal.

Code changes go through cross-family review (two non-implementer model
families) and the merge choke point. The verdict-file and `reviews/superseded`
conventions are program-workspace conventions (state/tools/prmerge.ps1), not
part of this repo.

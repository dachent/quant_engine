# Attended generation runbook

This runbook is the current operational procedure for the supported attended
runner. The governing GitHub issue and its current owner-approved input/phase
receipt decide whether a run is authorized and which inputs to use. This file
does not authorize a run or replace those receipts. Older state-folder
runbooks are historical evidence; do not copy their paths, dates, model
instructions, or deletion/recovery commands into a current run.

## Before any run

1. Read the repository `AGENTS.md`, `docs/attended-run-storage.md`,
   `docs/controller-graph.md`, and the current issue-bound runbook and input
   receipt. Confirm the governing issue, active phase, blockers, selected
   candidate, owner cutoff, and exact intended effects.
2. Verify the checkout path, branch, clean status, and exact commit. Run the
   committed code that the resulting receipts will name. Resolve the configured
   interpreter from the repository environment; never fall back to a global
   `python` shim. Record `python --version` and the runner's resolved runtime.
3. Preflight every configured input and output path. Required output and cache
   roots must exist when the runner requires them; a cold output root must be
   absent, and a resume must use the exact same input identities and existing
   receipts. Use short, local `C:\Dev\scratch\` roots, outside synced storage.
   Do not delete or overwrite a failed run: preserve its state and start a new
   root when input identity changes.
4. Run the supported declaration-only attended plan with the selected config,
   frozen financial cutoff, and explicit evaluation date. Require `PLANNED`,
   source-phase `PASS`, the expected common EOD, and no pin/schema/runtime
   errors before `--execute`. Plan mode is not calculation or acceptance.
5. Check producer/consumer identities before execution. In particular, a
   retained strategy attribution must bind the explicitly pinned expected
   staged portfolio baseline by both generation ID and catalog SHA-256. The
   expected staged-baseline identity is distinct from the retained allocation
   generation used by dependence. Require both the early input check and the
   runner's post-stage comparison to pass; never infer one identity from the
   other or relax either check. Also validate any oracle/report/panel identity
   joins required by the issue's acceptance receipt.
   The preflight ladder refuses, in seconds and before any stage, a retained
   allocation, baseline, attribution or dependence selection that binds another
   provider ledger than the run (`check_retained_authority_pins.py --ledger-root L
   --retained ROLE=PATH` runs the same matrix by hand), and an output or cache root
   under which a generated path would reach 260 characters. For a run that declares
   acceptance (`--declares-acceptance`), pass the acceptance inputs MANIFEST file as
   `--oracle-manifest` (a receipt is accepted only if it embeds that manifest;
   `parent-execution-receipt.json` holds just `manifest_sha256` and is refused). The
   join identity is the ledger file's sha256: the run refuses when it differs from the
   ledger the run consumed, and also when the manifest's ledger folder is a 64-hex
   generation id that differs from the run's generation. A preserved copy under any
   other folder name is accepted on sha alone (`generation_id_checked=false`).
   Two manifest entries naming the same ledger path with different sha256 are refused
   as a conflicting duplicate. A plan-only run without the flag reports the join
   `NOT_CHECKED` on stderr and in the result, which is not a pass; `--execute`
   refuses `--declares-acceptance` unless `--oracle-manifest` is supplied.
6. Settle the QA hold before `--execute`. The `panel` stage needs browser QA
   evidence that only exists after `panel-candidate`. The plan prints a `qa` block
   with the stage and the required `--qa-dir`; `--execute` refuses unless the
   invocation gives `--qa-dir <directory>` or declares `--qa-hold` (build through
   `panel-candidate`, stop at `panel`, run browser QA, then `--execute --resume
   --qa-dir <directory>`).
7. Pre-run the exact required test selectors with `--collect-only`. For browser
   tests, verify the pinned Playwright module and browser executable first; for
   Windows temp files, verify `TEMP` and `TMP` point to existing short paths.
   Run producer, consumer, validator, CLI, and required browser suites. Record
   `PASS`, `FAIL`, and `SKIPPED` separately, with the concrete reason for every
   skip. A missing owner fixture is an open validation gap, not a pass.

## Execution and failure handling

- Execute only from the clean exact commit and only after the plan passes.
  Preserve the exact config, clocks, output/cache roots, and command in the
  issue evidence. Resume only when the runner's recorded input identity is
  unchanged; input, code, or evaluation-date changes require a new root.
- Follow stage receipts and state files, not process names or partial files.
  A failed or interrupted stage is not completion. Do not move a `CURRENT`
  pointer, publish, or label the candidate `READY` without the separate
  acceptance and owner authority required by the issue.
- On any failure, preserve the run root and classify it before retrying:
  code defect, stale/mismatched input, environment/setup, missing fixture, or
  acceptance gap. Trace the failing value to its selected source and owning
  producer/consumer seam. Add a failing regression before a code fix; never
  relax a correct financial, identity, or recovery guard to make the run green.
- Before retry, state the root cause, exact correction, preflight that should
  have caught it, and proof needed to show the class of failure is closed. Add
  a short durable runbook/RCCA note when the failure exposed a reusable
  procedure gap. Do not repeat unchanged failed attempts.

## Evidence to report

Report the exact commit, config digest, cutoff/evaluation clocks, plan result,
run state and completed stages, test counts and skips, output-root location,
and unresolved owner/fixture/acceptance gaps. Keep financial values and raw
source captures out of operational summaries. A completed generation is not
financial acceptance or publication authorization.

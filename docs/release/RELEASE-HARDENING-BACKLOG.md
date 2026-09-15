# Release hardening backlog

Captured 2026-09-14 from the MVP-2 publish attempt. Every item below is either a
defect found that day or a control that, had it existed, would have prevented
one. Nothing here is aspirational: each entry names the concrete failure that
motivates it.

Status legend: `OPEN` / `DECISION` (needs owner ruling) / `DONE`.

---

## 0. Blocker: corrupted predecessor generation — `DECISION`

`prepare_release` -> `read_current_release` -> `_validate_committed_state`
requires the predecessor graph to be byte-identical to what its committed
release sealed. Generation `a5cfa66d…` in the `portfolio-report` root was
mutated after publication by the SharePoint sync client, which rewrites `.xlsx`
document properties in place.

| value | digest |
|---|---|
| sealed inventory (release `3e78ab96…`) | `3b4a560700542d76…` |
| actual inventory on disk | `cf2f6c25d99f5616…` |
| live workbook bytes | `f74b720a41d38701…`, 520398 B |
| August staged copy | `2cbf1b25a4437a86…`, 520395 B |
| manifest-recorded (canonicalized) | `3e962c988592e683…`, 510780 B |

No pointer value satisfies both checks:

* pointer = `cf2f6c25…` -> `read_current` succeeds, authority guard fails.
* pointer = `3b4a5607…` -> `read_current` itself raises
  `current generation artifact inventory hash differs`.

970 distinct copies of `portfolio-report.xlsx` across `_quant_engine` and
`C:\Dev\scratch` were tested against the sealed inventory; none reproduce it.
25 further copies are unreadable (paths over `MAX_PATH`, `LongPathsEnabled=0`).
The sealed bytes are unrecoverable.

**Option A (recommended).** A first-class predecessor-repair receipt: a reviewed
record that a sealed generation was corrupted by an external agent, naming the
sealed and observed digests, which permits superseding it. Keeps the release
chain intact and auditable.

**Option B.** Owner-authorised authority reset so `prepare_release` starts from
current state. Faster, but discards the release-authority chain and loses the
record that this happened.

Not acceptable: editing the pointer or the authority record to paper over the
difference. That forges history.

Self-liquidating once past: the successor generation `c3889c02…` contains no
`.xlsx` at all, so no sealed reporting generation will hold a sync-rewritable
file again.

---

## 1. Release preflight does not check the commit precondition — `OPEN`

`_release_preflight` validates the candidate closure and takes a production
*snapshot*, but `_production_snapshot` only `_file_state`s each `CURRENT.json`
— it stats and hashes the pointer file. It never parses the pointer, resolves
the generation, or recomputes an inventory. It detects "production moved during
preflight", not "production is valid".

The real check already exists in the same module:
`tools/portfolio_qc.py:622 _read_committed_release` -> `read_current_release`.
Its only caller is the `post-release-verify` profile.

So the authority invariant is verified at prepare time (too late) and after the
release (too late), but not in the gate whose purpose is to predict whether the
commit will succeed. Preflight passed three times on 2026-09-14 while the commit
was guaranteed to fail.

**Fix.** Call `_read_committed_release` and `_verify_committed_manifest` inside
`_release_preflight`. Fails closed in seconds on the precondition that blocks the
commit, instead of 40 minutes later.

---

## 2. Orchestration is not under source control — `OPEN`

The whole chain is driven by unversioned bash in
`C:\Dev\scratch\quant-engine\pl12\epoch\`: `build_vertical.sh`,
`qa_and_panel.sh`, `run_pipeline.sh`, `run_production.sh`. No tests, no CI, no
review, no resume. Six of the seven gate failures on 2026-09-14 originated
there.

**Fix.** `tools/portfolio_release_run.py` in the repo, covered by the existing
`ruff` / `mypy` / `pytest` CI gates. Phases: `vertical -> qa -> pipeline ->
production -> publish`. Build order becomes a tested data structure.

Driver fixes made on 2026-09-14 that currently exist only in scratch and must
migrate with it:

* `HISTORY_BUNDLE` derived from the backtests generation's own
  `raw/strategy-history-manifest.json` (was a stale literal).
* Governed capital input rebuilt per cycle, with root selection following the
  ledger generation the release will select.
* `run_production.sh` clears its own create-only outputs before re-running.
* `qa_and_panel.sh` restored to the sequence (it stages the `panel` role).
* No `| tee` on driver invocations — it masked non-zero exit codes and reported
  a failed vertical as success.

---

## 3. No generated, committed control file — `OPEN`

`production-spec.json` (256 nodes) is build *output* in a scratch directory,
regenerated per run, never committed, never reviewed. A file the run generates
cannot control the run.

**Fix.** `contracts/portfolio-release-control-v1.json`, schema'd and
CI-validated:

1. `portfolio_release_run.py emit-control` derives it from repo state and the
   declared source authorities. Deterministic, read-only, seconds. Fails if
   anything it references is absent or digest-mismatched.
2. `git diff` shows every pin move as a reviewable line.
3. Commit. Moving a pin becomes a reviewed act.
4. `--check` validates the committed control file resolves.
5. The run's generated spec must match it; divergence fails closed.

The generation step *is* the validation step: a successful emit already proves
the long run's inputs are sound.

---

## 4. `--check`: fast gate before any long run — `OPEN`

Must cover, in seconds:

1. Repository clean; HEAD is the commit the recipe will record.
2. Trust anchors: `tool_sha256` of `portfolio_target_series.py` against the
   pinned history generation, and all five
   `LAYERED_HISTORY_REPOSITORY_DEPENDENCIES` against `git show <that
   generation's commit>`. Print `history: REUSE` or `history: REBUILD —
   requires policy repin and a second pass`.
3. Every referenced path exists and its digest matches the consumer's
   expectation.
4. No 64-hex literal anywhere in the orchestrator (lint, fails the build).
5. Capital: which root is selected, whether that ledger generation exists,
   whether the recorded provider-ledger path lies inside the generation
   production will select.
6. Create-only output collisions.
7. Which stage results exist vs which the next phase requires.
8. Free space, and dehydrated OneDrive placeholders under every read path.
9. The release-authority invariant (item 1).

---

## 5. Resume points — `OPEN`

Each phase writes a receipt binding input and output digests; a phase whose
receipt verifies is skipped, `--from <phase>` forces re-entry. A production-stage
failure currently costs a full vertical rebuild, which was the single largest
contributor to elapsed time on 2026-09-14.

---

## 6. Runbook generated from the orchestrator — `OPEN`

Emitted by the code, not maintained beside it, so it cannot drift from what
actually executes.

---

## 7. Diagnostics that name the failure — `OPEN`

`_artifact_for_digest` in `tools/portfolio_financial_outcomes.py` raises one
identical message for four call sites and names neither the digest nor the
caller. A ten-line wrapper printing the wanted digest, size, path filter and
near-misses by digest and by size turned an opaque `absent or ambiguous` into an
exact answer in one run. Make that permanent.

Same class: `generation_inventory_sha256` failures should name the file whose
digest moved, not just that the inventory differs.

---

## 8. Sealed directories must not contain sync-rewritable files — `OPEN`

`generation_inventory_sha256` hashes every regular file in a generation
directory. Any `.xlsx` inside one is rewritten in place by the sync client.

* Panel generation: fixed (`86eed6b`) — workbook carried as base64 payload in
  `index.html`, recovered by `embedded_download_bytes`.
* Reporting generation: the successor `c3889c02…` no longer contains it.
  Confirm the producer cannot reintroduce it; `portfolio_report.py:6401` still
  writes `portfolio-report.xlsx` into the report candidate, and
  `schemas/portfolio/report-delivery.schema.json:56` pins the filename as a
  schema `const`. Surfaces: that schema, `portfolio_release_builder.py:1141,
  1183`, `portfolio_qc.py:490,517,565`, `portfolio_pipeline_materialize.py:3537`
  — plus a schema version bump.
* Add a check: no sealed generation may contain a file with an Office
  extension.

---

## 9. Workstation environment — `OPEN`

* 5,175 of 9,775 repo files were dehydrated OneDrive placeholders; reads fail
  with `The cloud operation was not completed before the time-out period
  expired`. Took out a full test run and `pyarrow._parquet`. Mitigation applied:
  `attrib -U +P` plus a forced read pass (14m39s, 181 transient failures that
  cleared on retry). Needs to be a preflight step, not a manual remedy.
* `LongPathsEnabled=0`: 25 files unreadable even via the `\\?\` prefix.
* Scrub the 61.2 GB / 426,542-file `_quant_engine` tree (owner-requested,
  post-publish).

---

## 10. Carried over from earlier sessions — `OPEN`

* Producer-side order-independent sum, `tools/portfolio_control_panel.py:3807-3810`.
* Issue #408 P4 (parallelise the 24 lenses) and P7 (parallelise validator replay).

---

## 11. Working rule

**Before changing any pinned value, enumerate every reader of it.**

Broken twice on 2026-09-14 with the same consequence: the stale bundle id, and
the re-pin of `portfolio-report`'s inventory hash that satisfied preflight and
silently broke the release-authority invariant — the blocker in item 0.

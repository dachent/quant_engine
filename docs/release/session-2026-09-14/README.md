# Transcript sweep, 2026-09-14

Seven extractors swept the 21,105-line session transcript in disjoint segments, one segment was
re-run on a stronger model after the first pass returned narration as todos, and a reconciler
verified every surviving item against the repo and live GitHub state.

Kept because the reconciliation is the audit trail for issues #409-#418: it records what was
searched, what was found, what was judged already done, and what was deliberately dropped. It
lived only in a scratch directory until it was nearly deleted with it.

| file | what it is |
|---|---|
| `transcript-sweep-raw-items.md` | the raw union of all seven extractor passes, with the quality warnings that apply to each |
| `transcript-sweep-context.md` | repo, PR and issue state the reconciler worked against. **Contains three known errors** — it listed #409, #399 and #303 as open; #399 and #303 are closed and #409 did not exist. Kept uncorrected because the reconciler's conclusions were reached against it, and a later reader needs to see what it was actually given |
| `issue-405-correction.md` | the correction to issue #405, drafted and never posted, disproving both routes the issue states |

## Where the items went

| source | outcome |
|---|---|
| methodology choices that change published numbers | #409 |
| cross-root decomposition reuse | #410 |
| live-engine prerequisites | #411 |
| provider captures and the HAR/API disagreement | #412 |
| owner-publication-authorization | #413 |
| release orchestration, control file, `--check`, resume, runbook, diagnostics | #414 |
| preflight authority invariant and the corrupted-predecessor record | #415 |
| sync-rewritable files in sealed generations | #416 |
| current partial week | #417 |
| factor staleness and the refresh phase | #418 |
| already fixed in-session | verified DONE against the repo, not re-filed |

## Calibration note

The first pass on the densest segment was run on a small model and returned progress narration,
tool output and code snippets as todos. Re-running that segment on a stronger model returned
twelve verified items and an anti-list explaining what the first pass had wrongly flagged. For
extraction where precision matters as much as recall, the cheap model cost more than it saved.

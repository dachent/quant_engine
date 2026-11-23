# Release and refresh run times

How long a refresh or a release takes, stage by stage, measured on real runs, in plain language. Use it to plan a cycle and to see where the time goes. It holds no account numbers and no amounts.

Two real runs are measured. **A5** (cold: its own empty fit cache) was a full release at cutoff 2026-11-06. **D34** was the full release at cutoff 2026-11-19, the run that followed a provisional refresh at the same cutoff. Machine: 32 threads, other sessions running at the same time, so times are as-run, not best case. Seconds are the driver's own stage walls (`release-driver-timing.log`, written by the #882 counters) unless marked E (estimate).

## 1. The two tiers

| Tier | What it is | Stages it includes | Writes to real roots? | D34 wall (pure compute) |
|---|---|---|---|---|
| **Provisional decision refresh** | The same frozen code on newer data; produces a stamped, never-publishable root so the owner can look at current numbers. See `docs/decision-refresh-runbook.md`. | staging to ledger, prep chain, attended run, QA, resume, production-spec, then the **replica-only** ceremony | No | about 4.4 h (staging 0.3 h, attended + QA + resume 2.2 h, recipe to production-spec 0.4 h, replica copy plus the replica-only ceremony 1.5 h; the ceremony figure, 4,708 s, is from the provisional run, not D34) |
| **Full release** | Same as above, then the real lanes and the commit. | the above, plus attest, preflight, plan, dry-run, findings, release note, the replica lane, the real lane, **commit**, post-commit verify, evidence pack | Yes (generations, pointers, attestation) | about 7.0 h compute; D34 elapsed 10.7 h because decisions and one refusal were waited for |

A full release does not repeat the provisional ceremony: it replaces the replica-only ceremony stage by the replica lane plus the real lane. Plan on the full-release figure when a commit is the goal.

## 2. What each stage is for, and what it cost (D34)

| Stage (driver id) | What it is for | Seconds |
|---|---|---:|
| cycle-seeds, cycle-resolve, provenance-spec, provenance | pin the cycle's inputs and the code identity, resolve the provider generations | 12 |
| ledger-spec, ledger | build the provider ledger from the captured sources | 166 |
| implementation-reconciliation, implementation-return-attribution | reconcile the implementation sleeves against provider data | 70 |
| baseline-spec, baseline, attribution-spec | independent expected baseline and the attribution spec | 31 |
| selected-build, selected-inputs, pre-chain | choose and verify the exact input files | 141 |
| calendar-seed, calendar-authority | the calendar of observation days the run is allowed to use | 103 |
| prep-spec | the preparation spec the attended run consumes | 132 |
| **attended** | builds ledger, baseline, attribution, analytics (decomposition and lenses), the report and the panel candidate | 4,198 |
| g2 | quality findings on the candidate panel (payload size, validator findings) | 132 |
| qa-browser, qa-interaction, qa-native | browser and interaction checks of the candidate panel | 646 |
| **resume** | publishes the panel after QA | 2,743 |
| recipe-probe, registries, recipe, pipeline-spec, semantic, financial-artifacts, provider-generations | assemble the production recipe and spec inputs | 226 |
| **production-spec** | derives the production spec and replays the financial outcomes (the model fits happen here) | 903 |
| attest | records which merged code identity runs (create-only) | 1 |
| replica-copy, replica-inventory, replica-spec | a private copy of the workspace to rehearse the commit | 611 |
| preflight | the real quality gate; includes the financial-outcome validation | 500 |
| plan, dry-run | plan the commit and rehearse it on the real roots without moving a pointer | 1,202 |
| findings-panel, findings-targets, release-note | the findings the owner reads | 108 |
| **lane-replica** | the full ceremony on the replica | 5,381 |
| **lane-real** | the same checks and dry-run on the real roots | 1,764 |
| **commit** | installs the generations and moves the pointers | 1,743 |
| **post-commit-verify** | reads everything back from the real roots | 2,766 |
| pack-evidence | copy the run's evidence to a durable folder | 391 |

The sum of recorded stage time is 23,969 s (6.7 h). The driver also spends about 37 s between consecutive stages on its own rechecks (45 gaps, 28 min in D34).

## 3. The earlier cold run (A5) for comparison

| Part | Minutes | Note |
|---|---:|---|
| staging and prep chain | about 12 | no model fits |
| attended | 52.5 | of which the report and panel candidate re-verify about 21 min of identical bytes; the decomposition and lens builds about 13 min of model fits |
| QA | 6 | |
| resume | 20 | re-verification |
| production-spec chain | 13.4 | 5,404 model fits; a replica run of the same inputs took 0 fits (it reuses the receipts) |
| lane-real / lane-replica | 22 / 63 | run in parallel in A5, so the wall was the longer one |
| commit | 18.2 | |

D34's attended step is slower than A5's (70 min against 52.5) and its resume far slower (46 min against 20). The two runs were not instrumented the same way, so the difference is not separated; section 5 lists the known candidates (repeated verification passes, a shared machine). D34's release ceremony is also serial: the replica lane and the real lane ran one after the other, where A5 ran them in parallel.

## 4. What a cycle one trading day later reuses and what it rebuilds

Reused as is (verify the hash, never assume): the frozen code and its environment if no code changed, the fit cache and its base (content-addressed, so rows whose training window did not change hit), the input bundles that do not depend on the cutoff, the declarations and policies, the helper scripts.

Rebuilt every time: the ledger, baseline, attribution and selected inputs (they are bound to the cutoff), the calendar authority (a first attempt that stops at the calendar gap by design, then the authority stage), the attended output and its panel, the QA, the production spec, the replica workspace, both lanes, the commit, the verify and the pack. A rerecord (`release_driver rerecord`) re-binds only stages produced under the same configuration, so it does not apply to a new cutoff and does not apply after a code change either.

Realistic wall clock for one more day, once its inputs are captured and pinned: 8 to 9 hours, 10 to 11 if two or three decisions are waited for. The captures and the audit-input derivation are other sessions' time and are not in that figure.

## 5. Known slow spots

1. **The fit cache key is invalidated by any edit to the files it hashes.** As measured in D34 the key hashes the whole decomposition producer, the canonical-json helper, the methodology file and `uv.lock`, so a one-line change in any of them makes every cached fit miss and the next run refits all of them (5,404 fits in production-spec, plus the lens fits in the attended step). A new trading day only misses the windows whose training span includes it. PR 1065 narrows the fit-cache key to the fit code and the numeric stack; once it has merged this sentence is out of date for the fit cache, but the production-spec replay receipts still key the whole files.
2. **A silent preamble on resume (about 21 minutes in D34).** Before it writes its first file, an attended resume re-reads every bound artifact of every completed stage, and it did so three times in a row with nothing written in between; each pass re-reads and parses about 1.9 GB of analytics generations (measured at about 72 s per pass for the analytics stage alone) plus the report, panel and ledger checks. The same full re-verification also ran after every child, which grows with the number of finished stages. The driver additionally saw one silent interval for the whole child because the child's output was captured until it ended. PR 1068 proposes the change in `tools/portfolio_pipeline_materialize.py` (one verification shared by the gate, the resume and the execute start; a native read of only the stage a child just wrote; immutability checks on the earlier stages; one mandatory full pass at the end; streamed child output; phase timers written to the timing log). Until it has merged and been run once, the measured numbers above are the old behaviour, and the saving is not measured.
3. **Report and panel re-verification (about 21 min in A5).** The report and panel candidate re-verify bytes the previous stage already proved. Persisting verification receipts so they are checked once (the verify-once work, PR 1002) is the larger remaining saving, estimated at about 20 minutes on a weekly refresh.
4. **release-preflight spends about 8 minutes validating financial outcomes before it checks the provider generations.** A missing provider generation refuses only after those 500 seconds (this happened in D34). PR 1068 runs the provider-admission check first as well, and again after the candidate QA.
5. **The replica lane (about 90 minutes) and the post-commit verify (about 46 minutes)** are serial and mandatory in a full release.

## 6. Keep the timing evidence

The per-stage timing files and the per-child counters of a run live in the run's output tree and are the only record of where its minutes went. Keep them in the retained evidence when the run's scratch is cleaned up; a cleanup that removes them makes the next "where did the time go" question unanswerable.

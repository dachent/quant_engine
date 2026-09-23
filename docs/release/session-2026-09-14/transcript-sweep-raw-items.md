# Raw extracted items (7 haiku extractors, disjoint segments of a 21,105-line session transcript)

QUALITY WARNING, read before using:
- Per-segment STATUS is unreliable. An extractor sees only its own slice, so items fixed later
  in the session are often marked "appears-open". Segment 3 in particular marks test failures
  open that segment 4 shows resolved. Settle final state against the CURRENT repo and test
  suite, never against these labels.
- Segment 7 (the densest, most recent stretch) has poor precision: it tagged narration, code
  snippets and progress chatter as "user-request". Filter it hard.
- Deduplicate across segments; the same item recurs under different wording.

## Segment 1 (lines 1-3015)
- USER STANDING DIRECTIVE, never acted on: "owner-publication-authorization and signature is
  bullshit ceremony that needs to be removed. Pass QC and all green and create PR that passes
  CI is the real governing criteria."
- USER: deliver and publish MVP-2 autonomously
- USER: push the 4 post-merge commits; post #374 catch-up (merges #397/#399 + SHAs)
- USER: label #398/#396 per the issue contract (stable key + backlog state)
- USER: promote #293 to backlog:active
- USER: draft portfolio-correction-registry JSON, have Fable 5.1 disprove, then proceed
- USER: "To do final review I need a fully updated control panel with all pointers updated"
- #303: five of eleven contract history_corrections records cite issues closed three weeks ago
- #293: report generation id differed between release proof root and pipeline run root
- #326: a resolved applicable correction with no re-derivation receipt still executes
- env: bulk artifacts must not default into cloud-synced folders; nine sync roots hold ~300k files;
  cldflt ERESOURCE hold caused bugcheck 0x1CC on 2026-08-21

## Segment 2 (lines 3016-6030)
- OWNER DECISION NEEDED: EU launch boundary date. 2024-02-18 appears in zero local bytes and is
  a Sunday; the CSV has 02-16 and 02-19. Otherwise EU ships live-only inside hybrid mode.
- DEFECT: strategy_1832531_transactions.csv starts 2026-05-15 with US tickers and an SPY
  benchmark, which is not a EUR Europe microcap model. Capture mislabelled, or the connected
  book is not the 1999 simulation.
- Stage the strategy-history bundle as a governed generation (portfolio_fx_rates.stage_candidate
  pattern; reads retained bundle, no network, emits immutable candidate)
- Add history_corrections BACKTEST records for grizzly/qsi/tt citing that generation, plus
  version bindings and authority pin
- Layered history with --strategy-history-manifest/--returns -> real BACKTEST segments
- Vertical with hybrid plan bundle -> 14 lens roots -> panel with partial coverage
- Push mvp2/293-xlsx-determinism, open the PR via ghx, confirm CI
- Correct issue #405: its two stated routes are both wrong; not release-blocking
- p123-ai-factor stays UNRESOLVED: live/simulation twin version not accepted, no admitted prehistory
- p123-eu prehistory is a genuine capture gap
  (required_capture: DESIGNER_MODEL_HISTORY_AND_LAUNCH_SNAPSHOT_FOR_THE_SUBSCRIBED_MODEL)
- robuxio-crypto and volatis-strategy provider evidence stays comparison-only; capture done,
  version compatibility not proven

## Segment 3 (lines 6031-9045)
- CAPTURE PENDING: P123 API refresh for entity 1853549 (last API row 2026-06-26, 67 days stale
  vs capture 2026-09-02); needs USER_REQUESTED_API gate approval
- CAPTURE PENDING: p123-eu designer-model page portid 1764562, against the user account
- CAPTURE PENDING: volatis Quantbase fund-107 track record
- DATA TRUTH: HAR vs API disagree for sim entity 1853549 -- 1,424 of 1,598 overlapping daily
  ratios differ by >1e-6; different Cash Added events between sources
- #409 robuxio provider track record staged with NOT_CAPTURED deferral
- scratch-resident fix scripts: fix_false_design_case.py, fix_coherent_rebind.py,
  fix_robuxio_funding.py (C:\Dev\scratch\quant-engine\pl12\epoch\)
- p123-ai-factor MIXED backtest status across grizzlybears-vtma/qsi/robuxio-crypto/tt-portfolio
- numerous contract/test failures in this window -- CHECK CURRENT STATE, most were fixed later

## Segment 4 (lines 9046-12060) -- mostly resolved in-session, verify before filing
- PR #402 scope drift: open on this exact branch, titled for #293 xlsx determinism, but the
  branch now carries the whole EU hybrid change and much more
- issue-405-correction.md written to scratch, never landed durably
- Hybrid DEPENDENCE was live-only by construction (fixed): segments selected on
  mode_admission == INCLUDE_SELECTED_LIVE; BACKTEST segments carry
  INCLUDE_VERSION_COMPATIBLE_PREHISTORY and were never selected
- mode_dispositions was hardcoded literal text, not derived (fixed)
- prehistory_identities scope bug (NameError waiting to happen) (fixed)
- segments.csv written live-only while returns.csv carried prehistory (fixed)
- _age_evidence skipped prehistory identities (fixed)
- CurrentDependenceError named no differing fields; made to name them (fixed)
- report stage requires a LIVE_ONLY lens layout; build_vertical builds one mode per run (fixed)
- governed HWM and invested-capital bound to a stale 2026-09-11 contract (fixed)
- ASSISTANT SELF-CRITIQUE: "Every failure today was a precondition I could have read in seconds,
  sitting behind a 6-hour producer"

## Segment 5 (lines 12061-15075)
- METHODOLOGY, changes published numbers: lens smoothness grid (1e-8..1e-5) is two orders of
  magnitude short of the base grid (1e-8..1e-3); 144 of 204 lens models pin at the ceiling with
  sensitivity curves still climbing
- METHODOLOGY, changes published numbers: FLS models inherit the static estimator floor
  minimum_train_observations: 100, which defeats borrowing strength across adjacent periods
- METHODOLOGY: only split-sample holdout results are published; no total-period regression in
  any mode, and no gate reports its absence
- Decomposition index cache does not persist across runs: build_vertical.sh wipes proof2 which
  holds the index, so reuse never carries across runs until the index moves outside
- Assistant ran rather than read qa_and_panel.sh, portfolio_pipeline_materialize.py,
  run_production.sh
- schema-10 consumers: financial_epoch_manifest_inputs legitimately absent under schema 10;
  consumers never taught the newer profile the producer emits
- serially discovering one gate per run costs ~3h each time

## Segment 6 (lines 15076-18090)
- PERF DEFECT: hybrid-mode lens solve degrades superlinearly -- same lens 17s in LIVE_ONLY,
  still running after 10 minutes in hybrid; 35x+ for a sample only ~2x longer
- PERF: cross-root decomposition reuse (consult a verified generation in another root and
  re-verify) saves ~2h50m on every run, including after a code change
- PERF: parallelise the lens loop, 24 independent subprocesses -> ~2h50m to ~25-30 min
- PERF: parallelise the validation replay -> ~1h30m to ~20 min
- PERF: memoize validation by spec_sha256 within a run -> ~45m-1h
- PERF: stop rm -rf run2, keep state/ so COMPLETED receipts resume -> up to 3h03m on same-commit retry
- LIVE ENGINE, correctness: acting at time t on a smoothed beta_t|T is lookahead bias; the live
  analogue is the forward filter beta_t|t only
- LIVE ENGINE: append-only state; the whole-root-wipe pattern is incompatible with live
- LIVE ENGINE: degraded modes instead of hard stops; serve last-good exposures with a staleness flag
- LIVE ENGINE: bounded per-tick deadlines and backpressure (silence-based budget is wrong for live)
- LIVE ENGINE: dashboard_data.json is 62 MB; impossible per tick, needs incremental diffs over a stream
- Orchestration scripts in C:\Dev\scratch\...\epoch\ accumulated across sessions with baked-in ids
- Solver floor is O(T*k^3); the refresh is long because it runs 1,540 independent solves

## Segment 7 (lines 18091-21105) -- low precision, filter hard
- USER: "DROP THE FUCKING XSLX" -- reporting root still shipped portfolio-report.xlsx inside the
  sealed generation; successor c3889c02 no longer contains it, but the producer can still write it:
  portfolio_report.py:6401, schema const in schemas/portfolio/report-delivery.schema.json:56,
  portfolio_release_builder.py:1141/1183, portfolio_qc.py:490/517/565,
  portfolio_pipeline_materialize.py:3537 -- plus a schema version bump
- USER: why is skills/portfolio123/SKILL.md in OneDrive (answer: the whole worktree is)
- ENV: 5,175 of 9,775 repo files were dehydrated placeholders; reads fail with cloud timeout;
  mitigated by attrib -U +P plus a forced read pass (14m39s, 181 transient failures)
- ENV: LongPathsEnabled=0; 25 workbook copies unreadable even via the \\?\ prefix
- USER: control file must be in the controlled codebase AND generated before each run
- USER demanded: strict runbook, tight run script with QC and resume points, fast pre-run
  validation of paths/hashes/files
- Preflight does not check the release-authority invariant: _production_snapshot only _file_states
  each CURRENT.json; _read_committed_release exists at tools/portfolio_qc.py:622 but is only
  called by post-release-verify
- BLOCKER (owner chose Option A, now implemented): sealed generation a5cfa66d rewritten by the
  sync client; sealed inventory 3b4a5607 vs actual cf2f6c25; 970 distinct workbook copies hashed,
  none reproduce it; bytes unrecoverable
- Driver defects fixed in scratch only, must migrate into the repo: stale HISTORY_BUNDLE literal,
  missing per-cycle capital rebuild + root selection, missing panel role / skipped qa_and_panel.sh,
  create-only output cleanup, `| tee` masking exit codes

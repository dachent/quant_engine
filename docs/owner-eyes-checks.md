# Owner-eyes checks (#912)

Review Q1 of the decision-readiness review: every one of the owner's eight finds was
visible on the rendered panel with real data and in no diff, so these checks bound the
**output**, not the code.

| Control | Entry point | Needs |
|---|---|---|
| 50 plausibility rules on the real payload | `python tools/check_plausibility.py --panel-dir <generation dir> [--cutoff D] [--json out.json]`; `--list` prints the rule table | Python only |
| Same rules inside the validator (`plausibility.owner_eyes`) | every `tools/validate_generation.py` run, `--owner-eyes warn` by default | nothing extra |
| Contract consumer check (PL51, find 8) | `check_plausibility.py ... --repo-root <checkout>`; internal contracts are declared with a reason in `ops/contract-consumers.json` | the checkout |
| Numeric rendered-panel checks | `python tools/owner_eyes_render.py --panel-dir <generation dir> --out-dir <dir>` | Playwright Chromium (hosted CI) |
| Same, without a browser | `python tools/owner_eyes_render.py --snapshot <snapshot.json> --payload <dashboard_data.json> --out-dir <dir>` | node |

**Modes (non-blocking until calibrated).** `--owner-eyes warn` (the default) lists every hit as a
WARNING finding and cannot change the validator's exit status; a crash of the rule set is
itself a WARNING. `--owner-eyes block` makes a hit block (BLOCKER_REGRESSION, or NOT_READY for
register-type rules) and a crash fail closed; turn it on after calibration on a release
payload. `--owner-eyes off` skips them. `check_plausibility.py --mode warn|block` is the same
switch (default warn: hits printed, exit 0).

Exit codes in `--mode block`: `check_plausibility.py` 0 clean, 1 a BLOCKER rule hit, 3 only NOT_READY hits,
2 unreadable payload. `owner_eyes_render.py` 0 pass, 1 a check failed, 3 the browser runtime
is unavailable (NOT RUN, never a pass), 2 usage.

Rules that repeat one defect across many series print the first five and a count.
Staleness (PL15, PL16) stays NOT_READY until the builder emits a `staleness_register`
block; claims (PL28) until it emits `claim_checks`.

Local dry run on the A5 panel (330da1a0): 97 hits from 24 of 51 rules; owner finds 1 to 8
all caught. `OWNER_EYES_A5_PANEL_DIR=<generation dir>` enables the real-payload test.

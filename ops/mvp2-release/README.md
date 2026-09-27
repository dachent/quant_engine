# MVP-2 release drivers (preserved from scratch)

These are the scripts and governed inputs that actually produced the MVP-2 releases
`244da8fce80446f4` and `02278094179f1e36`. Until now they lived only in an unversioned scratch
directory on one workstation, which is the defect tracked by
[#414](https://github.com/dachent/quant_engine/issues/414). They are preserved here so the
scratch tree can be deleted without losing the ability to cut a release.

**This is a preservation commit, not a design.** The orchestration these scripts encode belongs
in `tools/portfolio_release_run.py`, driven by a committed control file, with a `--check` fast
gate and resume points. Read #414 before extending anything in here.

## Running order

```
scripts/build_vertical.sh     # contract pins -> ledger + baseline -> history -> epoch manifest
                              # -> capital -> base source -> plan bundles -> schema-10 recipe
                              # -> attribution, analytics, report, workbook, panel-candidate
scripts/qa_and_panel.sh       # browser QA, interaction QA, QA receipt, then the `panel` role
scripts/run_pipeline.sh       # probe recipe -> correction authority -> recipe -> spec -> execute
scripts/run_production.sh     # financial artifacts -> repair gate -> production spec
                              # -> release preflight -> plan -> (PUBLISH=1) execute --commit
```

`PUBLISH=1` is the only thing that moves a production pointer, and it must never be set without
explicit owner authorization.

## Things that cost hours to learn, encoded here

* **Nothing may be a literal.** `HISTORY_BUNDLE` is derived from the backtests generation's own
  `raw/strategy-history-manifest.json` and byte-checked against that bundle's `manifest.json`.
  Ledger and epoch ids come from the cycle's own stage results. Every stale literal in this tree
  cost a 40-minute round trip before it was removed.
* **The capital root must follow the ledger the release selects.** Production checks that the
  provider-ledger path the capital input records sits inside the *selected* generation, which is
  the copy the pipeline staged under `run2`. On a cold start that root does not exist, so the
  first cycle binds `proof2` and the next closes the loop. `build_vertical.sh` picks the root by
  testing for the run2 ledger rather than hardcoding either.
* **`qa_and_panel.sh` is not optional.** It stages the `panel` role; skipping it leaves
  `stage-results/panel.json` absent and the pipeline fails at the probe recipe.
* **Never pipe a driver through `tee`.** It masks the script's exit code behind `tee`'s, which
  once reported a failed vertical as success.
* **`run_production.sh` replaces its whole output directory.** Every artifact under it is
  create-only, so a failed attempt leaves files that block the next run. Enumerating them by
  hand was wrong twice; the directory is generated, so the directory is replaced.
* **A history rebuild does not converge with a policy repin.** `--repository-sha` is folded into
  the layered-history generation id, so each commit moves the id the commit was made to record.
  Rebuild only when the five files in `LAYERED_HISTORY_REPOSITORY_DEPENDENCIES` actually moved;
  otherwise reuse.

## Inputs

`inputs/` carries the small governed artifacts the drivers read and cannot re-derive from the
repo alone: the epoch crosswalk, the account-interval receipt authority, the HWM governed result
and calendar authority, the invested-capital result and its input generation, and the base
decomposition spec. Everything else the drivers touch is either in the repo or rebuilt per run.

## Release record closure (corrective program N1/T1.5)

No script in this repository assembles `evidence/releases/<date>/` (`inputs.zip` +
`inputs.manifest.json` + `run-records.zip` + the QC/plan/execute JSON files above) --
`run_production.sh` writes its own scratch `production/` directory (`$P` above), and the
`evidence/releases/<date>/` record itself is sealed by hand from that plus the working tree's
own input closure (see that record's own `README.md`, "This record was moved here from
`docs/code-review/...`, when the working trees it ran from were removed"). That manual sealing
step is exactly where defect N1 was introduced: `inputs.zip` was captured without three of its
own frozen recipe's pinned companions (the Alpaca and Fidelity provider catalogs, and the Schwab
native NAV-capture binding evidence), so the 2026-09-23 release could not be replayed from its
own record.

Until that sealing step has a driver of its own (`#414`), treat this as a required manual step
whenever `evidence/releases/<date>/` is sealed, mirroring how `build_vertical.sh` gates on
`tools/check_retained_authority_pins.py` before it will proceed (`#444`):

```
python tools/check_release_closure.py evidence/releases/<date>
```

Exit 1 lists every pin in the release's own `runner-input-config.json` -> `provider_ledger_spec`
-> `run_observation` chain whose sha256 does not resolve inside `inputs.zip` or
`inputs-supplement/` (see that tool's module docstring for exactly what it walks and why). A
release record is not complete until this exits 0. The 2026-09-23 record's own gap was closed by
promoting the 60 recovered files into `inputs-supplement/` (copies only; see that record's
`README.md` addendum and `inputs-supplement/MANIFEST.json`), not by re-sealing `inputs.zip`.

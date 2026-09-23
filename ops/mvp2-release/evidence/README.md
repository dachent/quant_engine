# Release evidence

Supporting evidence for the MVP-2 releases. The authoritative record is the release manifest and
journal under `dataPortfolio/releases-refresh-2026/releases/<release_id>/`, which is retained
and is what `read_current_release` validates against. These are the QC artifacts that sat beside
the run and would otherwise have been deleted with the scratch tree.

| release | code_commit | roots moved | note |
|---|---|---|---|
| `244da8fce80446f4` | `6e6bd863694d` | 20 of 20 | first publish; required the predecessor-authority-repair gate |
| `02278094179f1e36` | `79ba5f665d32` | 9 of 20 | mixed-basis alpha fix; all 7 hybrid lens roots plus panel and report |

`377266fc….json` is the release-preflight receipt for the second publish. Only the most recent
survives: `run_production.sh` replaces its whole output directory each run, because every file
under it is a create-only output and a failed attempt otherwise blocks the next one.

The panel QA evidence (browser and interaction screenshots, manifests, visual lint, the
interaction results) is not duplicated here. It is already sealed inside the published panel
generation under `qa/`, which is the copy that matters.

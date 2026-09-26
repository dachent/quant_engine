# Attended run storage, lifecycle and states

The supported attended runner (`tools/portfolio_pipeline_materialize.py attended`) writes
far more than 50 MB per run. These controls keep that bulk out of synced folders, bound
its growth, stop the retained cache from being copied per run, retire what the runner
created, and make a run's state observable without matching command lines. The helpers
live in `packages/core/src/quant_engine/core/run_storage.py`.

## Location gate

Before any probe or write, `attended` refuses an `--output-root`, `--workspace-root`,
`--retained-cache-root` or `--retained-cache-base` inside a OneDrive/SharePoint tree:

- the path, or its link-resolved form, is under `%OneDrive%`, `%OneDriveCommercial%` or
  `%OneDriveConsumer%`; or
- the path or any existing ancestor carries cloud-files state: the `PINNED`, `UNPINNED`,
  `RECALL_ON_OPEN`, `RECALL_ON_DATA_ACCESS` or `OFFLINE` attribute, or a cloud reparse
  tag. A SharePoint library synced outside every `%OneDrive*%` prefix is caught here.

The error names the refused root and `C:\Dev\scratch\` as the expected location. The
workspace is included, so a run reads a workspace staged into scratch rather than the
live synced `dataPortfolio`.

## Budget gate

| Flag | Default | Measured |
|---|---|---|
| `--max-run-growth` | `20GiB` | output root + retained-cache root, from the moment the runner created the run root |
| `--max-scratch` | `40GiB` | `--scratch-root` (default: the output root's parent) |
| `--scratch-root` | output root's parent | |

Sizes accept `B`, `KB`, `MB`, `GB`, `TB` and `KiB`, `MiB`, `GiB`, `TiB`. The scratch
budget is checked before anything is written and again at the end. Run growth is
checked at execution start, after every stage child and at the end; a resume cannot
reset it, because the baseline is recorded in the run-root marker at creation. Each
hard-linked file is counted once, and links into a shared cache base count as zero.

Bytes are recorded per stage in the running attempt's `storage` field of
`attended-execution-observations.json`: `cache` (overlay and base identity), `start`,
`stages` (`role`, `bytes_written`, `files`, `total_bytes`) and `end`.

## Retained cache: shared base and per-run overlay

The analytics producers add entries to the retained cache and never rewrite one that
exists, so the retained cache used to be copied into each run (`cp -r retained-cache`)
to keep the seed pristine. Instead:

- `--retained-cache-base <dir>` names a shared base. The runner seals its files
  read-only.
- `--retained-cache-root <dir>` then names this run's overlay, which must be absent or
  empty on a fresh run. On first execution the runner builds it as a tree of hard links
  to the base: no byte is copied, and a link failure (another volume, a filesystem
  without links) refuses the run rather than falling back to a copy.
- New entries land in the overlay only. A write through a link to a base file fails,
  because the file is read-only.
- The overlay marker (`retained-cache-overlay.json`) binds the base path, the base
  inventory digest and the run root. The inventory is re-verified at every start,
  resume and end, so a base changed under a run refuses that run.

Without `--retained-cache-base`, `--retained-cache-root` is used directly as before and
the request identity is unchanged; with it, the base path joins the identity.

A later run may name an earlier run's overlay as its base, to reuse what that run added.
`retire` keeps any root or overlay that a kept run uses as its base.

## Run roots and `retire`

A root the runner creates carries `attended-run-root.json`, written at creation, before
anything else lands in it. `retire` considers only such roots among the runs root's
children and grandchildren, and only when the marker names the directory it sits in.

```powershell
python tools/portfolio_pipeline_materialize.py retire --runs-root C:\Dev\scratch\quant_engine\runs
python tools/portfolio_pipeline_materialize.py retire --runs-root C:\Dev\scratch\quant_engine\runs --keep 2 --apply
```

The first form is a dry run and lists what it would remove, with bytes. It keeps the
current candidate (`--current`, by default the newest `PASSED` root), the `--keep` most
recent roots (default 2), every run whose runner process is still alive, and every root
or overlay a kept run uses as its retained-cache base. A removed root's overlay is
removed with it only when the overlay's marker names that root; the base is resealed
after its links are unlinked. A directory without the marker is never touched.

## Run states and `wait`

`attended-run-state.json` in the run root records the run's explicit state with the
runner's PID and process start token:

| State | Meaning |
|---|---|
| `QUEUED` | the runner created or claimed the root and has not started an attempt |
| `RUNNING` | a preparation or execution attempt is in progress |
| `CANCELLED` | the run was interrupted (Ctrl+C, or its process ended without an outcome) |
| `FAILED` | a stage child failed, validation refused the run, or a budget was breached |
| `PASSED` | every required stage completed and the end-of-run gates held |

Each attempt in `attended-execution-observations.json` (schema 3) carries the same state
vocabulary and its `pid`. Schema-1 and schema-2 receipts are read with `COMPLETED` as
`PASSED` and `INCOMPLETE` as `CANCELLED`. An interrupted attempt keeps no ending
instant; its successor records it `CANCELLED`. A passed preparation leaves the run
`RUNNING`, because execution follows in the same process.

```sh
python tools/portfolio_pipeline_materialize.py wait C:/Dev/scratch/quant_engine/runs/r1 --timeout 21600 --poll 300
```

`wait` is keyed by the recorded PID and start token, never by a command-line pattern,
and it refuses a state that names its own process. It returns only once that process
has exited, even when a terminal state was already recorded, so the repository freeze
that binds a run lasts until the run process is gone. Exit codes: `0` `PASSED`,
`1` `FAILED`, `2` `CANCELLED` (including a process that ended without recording an
outcome), `3` timed out while the process was still alive. A venv `python.exe` on
Windows is a launcher; the PID recorded is the interpreter it starts.

## A supported invocation

```powershell
python tools/portfolio_pipeline_materialize.py attended --repo-root C:\Dev\scratch\quant_engine\cp\main --python C:\Dev\scratch\quant_engine\cp\main\.venv\Scripts\python.exe --workspace-root C:\Dev\scratch\quant_engine\runs\r1\workspace --output-root C:\Dev\scratch\quant_engine\runs\r1\out --retained-cache-base C:\Dev\scratch\quant_engine\cache\retained --retained-cache-root C:\Dev\scratch\quant_engine\runs\r1\cache --requested-as-of 2026-09-17 --evaluated-on 2026-09-23 --preparation-spec C:\Dev\scratch\quant_engine\runs\r1\runner-input-config.json --execute
```

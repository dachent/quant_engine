# Decision refresh runbook: the first PROVISIONAL refresh (refs #418)

A refresh runs the **same frozen code** on newer data into a root that can never become a release. Nothing here commits, publishes or moves a pointer. Commands are as the tools exist at the time of writing; paths in angle brackets are for the operator. Times are CST.

## 0. What the first refresh can and cannot use (read first)
- Release B is frozen **without** PRs 994 (resolver and coverage guard), 996 (finality records), 997 and the `decision_refresh.py` PR (next release only). A step that runs *inside* the driver from the frozen tree (`cycle-resolve`) therefore cannot use `latest_at_or_before` until a release contains it.
- **Path A, the day after B commits (explicit ids):** build the new cycle manifest with explicit generation ids and manifest hashes (`release_world.py cycle-manifest`), written from the latest captures by hand-listing their ids. Tools from the newer branches (`release_world.py`, `decision_refresh.py stamp`) run from a *separate tools clone*; they only write plain data files. No guarantee against a cutoff beyond the data (that guard lives in the newer `release_cycle_manifest.py`): run its `resolve` from the tools clone first as a check (step 6).
- **Path B, first release that contains the new tools:** same steps, but step 5 uses `decision_refresh.py late-cm` and the driver resolves the generations itself.

## 1. Frozen code
```bash
pwsh C:/Dev/bin/New-CentralGitClone.ps1 -RepositoryUrl git@github.com:dachent/quant_engine.git -WorktreePath C:/Dev/scratch/qe-refresh-<sha8>
git -C C:/Dev/scratch/qe-refresh-<sha8> checkout --detach <B release sha>
cd C:/Dev/scratch/qe-refresh-<sha8> && C:/Tools/uv-0.11/uv.exe sync --frozen
git status --porcelain      # must print nothing: any diff makes this a release, not a refresh
```
Tools clone (Path A): a second central clone at the branch with the new tools, never used for the run.

## 2. Cutoff and freshness
Cutoff D = end of the last complete NYSE session when the refresh starts (`release_cycle_manifest.py` computes the check from `refresh_started_at_utc`). Then the table of every source and the logins needed:
```bash
python tools/capture_refresh.py --cutoff <D> --pins <pins.json> --evidence-root <dir>
# exit 3: owner logins needed (Schwab web, Portfolio123, Quantbase, GrizzlyBulls); exit 4: ready to capture
```

## 3. Captures
Owner signs in once (about 10 min); unattended pulls run meanwhile (IBKR Flex via the vault token, Matrixport API, TuringTrader, Yahoo raw Close+Dividends for base and style tickers). Each generation needs its finality evidence; until the capture stages write finality records (PR 996 follow-on), the evidence file and date are listed by hand in the manifest (Path A).
Per-cycle builders still run by hand: QSI refresh bundle (`portfolio_strategy_history.py --qsi-refresh-*` then backtests staging), weekly gap-fill spec root, base bundle (`base_bundle_builder.py`), style bundle (`portfolio_public_style_sources.py`), Quantbase generation, HWM calendar reissue. Rule: never build against pinned old inputs; check each against the dates in the S0 table.

## 4. World
```bash
python tools/release_world.py copy --from <last world> --to C:/Dev/scratch/quant_engine/refresh/<D>/world \
  --swap in/base-current-D=<new base bundle> --swap in/public-style-r3=<new style bundle> --swap in/sector-d=<new sector bundle> \
  --roots-file <D>/roots.json --data-root "<OneDrive _quant_engine>" --apply
```
World-local provider roots must keep the real leaf names (`dataSchwab`, `dataMatrixport`, ...): the leaf enters the ledger identity. The refresh root lives on the non-synced disk; only the context pack, memo and `PROVISIONAL.json` are copied to OneDrive afterwards.

## 5. Cycle manifest
```bash
# Path A (explicit ids)
python tools/release_world.py cycle-manifest --from <last cm> --out <D>/cm/cycle-manifest.refresh.json --cycle-id refresh-<D>-<sha8> \
  --predecessor <last release id> --generation <provider>=<generation id>:<manifest sha256> ... --root schwab=world:dataSchwab
# edit by hand: cutoff, evaluated_on, captured_at_utc, refresh_started_at_utc, finalized_through + finality_evidence per provider
# Path B (late binding)
python tools/decision_refresh.py late-cm --from <last cm> --out <D>/cm/cycle-manifest.refresh.json --cutoff <D> --evaluated-on <E> \
  --captured-at-utc <T> --refresh-started-at-utc <T> --cycle-id refresh-<D>-<sha8> --predecessor <last release id>
```

## 6. Validate and resolve (tools clone first as a coverage check, then the frozen tree)
```bash
H=$(sha256sum <cm> | cut -d' ' -f1)
python tools/release_cycle_manifest.py validate --manifest <cm> --roots-file roots.json --manifest-sha256 $H
python tools/release_cycle_manifest.py resolve  --manifest <cm> --roots-file roots.json --manifest-sha256 $H --out <world>/c/resolved-cycle.json
```
Refusals to expect and what they mean: `COVERAGE_BEFORE_CUTOFF` / `FINALITY_BEFORE_CUTOFF` (a source ends before D: capture it or stop; never shorten D), `NO_ELIGIBLE_GENERATION` (every verdict is printed), `AMBIGUOUS_GENERATION` (two captures share an as_of: remove the stray staged one).

## 7. Cache base (fits are reused, not recomputed)
```bash
python tools/release_cache_base.py seed --base <last sealed base> --target <cycle>/cachebase
# set run config retained_cache_base to <cycle>/cachebase (it must lie in the cycle)
```
Read the date of the last full fit from the base provenance (`created_at`); it is shown on every surface that reuses fits.

## 8. Ledger, prep and the run (replica-only, through attended)
The ledger chain and prep scripts are still the launcher scripts (`final-launch.sh` pattern, not repo tools yet; use a copy in your own directory). Then:
```bash
python tools/release_fresh_world.py config ...                 # run configuration from the previous pass, repointed
python tools/release_driver.py run --config <run.json> --through attended     # ceremony replica-only; never commit
```
Skipped on purpose: replica ceremony, QA, resume, production-spec, lanes, commit, model-family review. Kept: closure checks, per-source freshness, G0/G1 gates. Verify-once (`QE_RENDER_REUSE=provisional`) is enabled only through the driver's tier hook (P7 design, not merged): until then expect the full verification time.

## 9. Stamp and guard
```bash
python tools/decision_refresh.py stamp --resolved <world>/c/resolved-cycle.json --repo-root C:/Dev/scratch/qe-refresh-<sha8> \
  --out <cycle root> --last-full-fit <YYYY-MM-DD from the base provenance>
python tools/decision_refresh.py check-not-release --root <cycle root>     # exit 1: it is provisional
```
`stamp` needs a clean frozen repo and writes `PROVISIONAL.json` create-only: tier, cutoff, frozen commit, resolved-cycle sha, one staleness line per provider (data end, age at `evaluated_on`), last full fit. `stamp` requires late-bound providers (Path B); Path A records the same facts by hand in the memo.
**Open (R0, not in this PR):** the driver's `commit` and the pointer helpers do not yet call `check-not-release`; until they do, never point a release config at a refresh root.

## 10. After a PASSED run
```bash
python tools/release_cache_base.py promote ...      # seal the new overlay as the next base
```
Copy the context pack, memo and `PROVISIONAL.json` to OneDrive. Compare numbers with the last release (restatements before last week's cutoff must be explained). Delete nothing from the refresh root until the memo is accepted.

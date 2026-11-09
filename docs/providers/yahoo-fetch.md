# Yahoo fetch driver

`tools/yahoo_fetch_driver.py` is the governed, config-driven public Yahoo price and FX
fetch. It wraps the one maintained adapter,
`skills/agent-market-data-terminal/scripts/fetch_yfinance.py`; it does not define a
second tape format. Equity and ETF bars use the adapter's `fetch_history_frame`,
`transform_history_dataset`, `build_coverage`, `build_request_payload` and `request_id`,
so every `artifacts/yahoo-<group>-<dataset>.json` and its `.manifest.json` has the
schema the readers already require (`provider yahoo`, `transport yfinance`, `dataset`,
`source_run_id` = sha256 of the manifest request, coverage claim `PARTIAL`). Fiat FX
pairs are kept as the raw Yahoo chart response, the input `tools/portfolio_fx_rates.py`
fiat mode already reads.

## Config (data, not code)

`docs/providers/yahoo-fetch/r3-batch-a.config.json` is the R3 Batch A config. Keys:
`cutoff` (ISO date; `--cutoff` overrides), `groups[]` (`group_id`, `kind`
`yfinance|chart`, `symbols`, `interval`, `start`, `through` = `cutoff` or an inclusive
ISO date, `calendar` `weekday|every_day|nyse`, `min_rows`, `max_missing_weekdays`, optional
`closed_days[]` (nyse only), `datasets`, `artifact_profile` `tape|capture-v0`, `max_null_closes`,
`first_bar_tolerance_days`, `allowed_last_bar_lag_days`, `exceptions[]`), `limits`,
`symbol_resolution[]`.

**Calendars.** `weekday` expects every Monday to Friday, so a group whose window holds market
closures needed a guessed `max_missing_weekdays`. `nyse` expects exactly the NYSE sessions of
`quant_engine.core.market_calendar` (weekends and the regular full-day closures): a session
with no bar is missing, the closures are not, so the check is exact and
`max_missing_weekdays` stays only as an upper bound (set it to 0). An ad hoc closure the calendar
does not model (for example 2025-01-09) is declared per group in `closed_days` (distinct
weekdays inside the window, `nyse` only); an undeclared one refuses the group. The shipped
volatility groups use `nyse`. The request end is always `through + 1 day` (exclusive). Unknown
keys are refused. `capture-v0` emits exactly the ten-key capture the FX tool reads
(`provider, transport, dataset, tickers, columns, rows, start, end, interval, prepost`).

## Derived groups

A group with `symbols_from: "tt-source-manifest"` lists no symbols and no start. Both
are derived at run time from `--tt-source-manifest`, a TT source manifest JSON: the
symbols are the ones `quant_engine.turingtrader.source_manifest._target_symbols` would
return (held at live inception or in any snapshot up to the group's `through`), with
`BRK.B` mapped to `BRK-B`; the start is live inception minus
`start_offset_days_before_inception` (the tape reader needs every ticker to start before
inception). `symbols_must_include` is a guard: the run is refused if the manifest is
stale or incomplete. A hand list on such a group is refused. The derivation (manifest
sha256, end, inception) is written into the effective config, so the config hash binds
it, and `verify` re-parses the staged config as already resolved.

## Raw SPXL history (builder input)

The group `spxl-raw-history` fetches `SPXL` daily from its inception (`start` 2008-11-01,
first bar 2008-11-05) through the cutoff with `datasets: ["history"]` only, so the run
holds `artifacts/yahoo-spxl-raw-history-history.json` (`provider` yahoo, `transport`
yfinance, `dataset` history, raw `Close` beside `Adj Close`) and its sibling manifest, and
no `history_adjusted` file. That one file is the governed input of
`--spxl-market-data` (`portfolio_implementation_reconciliation.py`, which refuses any
dataset but `history`) and `--grizzly-market-data`
(`portfolio_implementation_return_attribution.py`); the Grizzly price extension of
`portfolio_strategy_history.py` reads the same shape. The guards are `min_rows` 4400 and
`max_missing_weekdays` 180 (the 2008-11-05..2026-09-17 span has 4,662 weekdays and 169 market
holidays). Do not convert a CSV by hand. The `qsi-daily` and `vtma-spxl-60m` groups also
carry `SPXL` for their own consumers and are unchanged.

Price column per reader: the reconciliation (`_market_prices`) and the Grizzly price extension of
`portfolio_strategy_history.py` take `Adj Close` and fall back to `Close` only when it is absent
(`row.get("Adj Close") or row.get("Close")`); the Grizzly return attribution values SPXL at the raw
`Close` (`_grizzly_state_simulation`, `_grizzly_actual_ledger_return`; its `Adj Close` fallback is the
TT path). `Adj Close` is a property of the vintage: a dividend paid after a fetch rescales every
earlier `Adj Close` of that fetch, so a later run reproduces `Close` (the retained 2026-09-17 CSV's
`close` of 280.760009765625 on that date) but not `adjclose` on dates before a newer dividend.

From the run to the release input (no hand step): the file is copied byte for byte into the
pinned release-inputs root as `ops/mvp2-release/inputs/implementation/spxl-market-data.json`
(the path `release-stages.json` reads for `--spxl-market-data` and `--grizzly-market-data`) by
the committed `release_inputs.py add`, which verifies the source root against its pin, copies it
to a new root outside the repository, adds the file, rebuilds the manifest and prints the new
pin (`release_inputs_manifest_sha256` of the release-driver config):

```
python -I tools/release_inputs.py add --root <ROOT> --manifest-sha256 <PIN> --cutoff <D> \
  --out <NEW_ROOT> --file ops/mvp2-release/inputs/implementation/spxl-market-data.json=<STAGE>/r3-yahoo/artifacts/yahoo-spxl-raw-history-history.json
```

A path the root already holds is refused unless `--replace` is passed. Adding is an entry edit, so
it belongs to steps 1-2 of the root assembly (R3-PLAN section 14.8) and `rebind-predecessor` stays the
last operation: it copies every manifest path of the root it is given, so the added file carries
through, and the pin handed to the release driver is the rebound root's, not this command's. Running
`add` again from the same root with the same bytes gives the same pin; a rebound root is never the
input of a later `add`.

## Dry run first

`dry-run --config CONFIG --cutoff D [--tt-source-manifest TT] [--schwab-positions POSITIONS]
[--allow-unresolved] [--stage-root SCRATCH] [--run-id DRY-...] [--max-requests N]` rehearses the
governed `fetch` of the same config and cutoff: the same groups, rules, staging and `verify`, with
the public reads, and none of the governance. It reads and consumes no evidence, stages in a scratch
directory (a fresh temporary one unless `--stage-root` is given; a given directory may hold only
`DRY-` entries, so a release root or a governed stage is refused), uses a run id that starts with
`DRY-` (a governed `make-evidence` or `fetch` refuses such an id, so a rehearsal can never be
substituted for a governed run), and verifies what it staged before it prints the outcome
(`status` `VERIFIED` or `REJECTED` with the failures, `evidence_consumed` false). It does not
replace the approval: spend single-use evidence only after a dry run of the same config and cutoff
verified. Every live driver follows this rule: rehearse first, with no evidence, in a scratch stage.
Every run manifest and every `official-closes-manifest.json` also carries `mode` (`dry-run` for a
`DRY-` id, `governed` otherwise); `verify` fails a run whose mode disagrees with its id, and
`schwab_capture_ingest` refuses an official-closes run or sidecar that is marked `dry-run`, has a
`DRY-` run id or sits in a `DRY-` directory, so a rehearsal cannot return a verified Yahoo binding.

## Run order and the clock (R3)

Commands, in order (`D` is the cutoff, the delivery date; every command that reads live data or
writes the config a fetch will use now refuses to run without `--cutoff`: the shipped config's own
`cutoff` (2026-09-17) is only a default, and `config-check` prints `cutoff_source` to say which one
is in effect):

1. `config-check --config CONFIG --cutoff D --tt-source-manifest TT --schwab-positions POSITIONS`
   prints `config_sha256` (the hash of the EFFECTIVE config: cutoff, derived symbols and starts and
   resolutions included, not a hash of the file), `cutoff`, `cutoff_source` (`cli`), the group ids
   and the resolution table. The probe evidence names this `config_sha256` and `D`.
2. The issue comment approving the probe run, then `make-evidence --config CONFIG --cutoff D
   --tt-source-manifest TT --schwab-positions POSITIONS --run-id R3-PROBE --review COMMENT_URL
   --valid-from-utc T --valid-until-utc T --max-requests N --out PROBE_EVIDENCE` (create-only; the
   window is at most 24 hours; it writes the effective config hash and `D` itself, so nothing is
   copied by hand), then `probe --config CONFIG --cutoff D --tt-source-manifest TT --schwab-positions POSITIONS
   --run-id R3-PROBE --stage-root STAGE --governance PROBE_EVIDENCE`, then `verify STAGE/R3-PROBE`.
3. `apply-admissions --config CONFIG --cutoff D --tt-source-manifest TT --schwab-positions POSITIONS
   --probe-run STAGE/R3-PROBE --out STAGE/r3-config.json` writes the resolved config (derived symbols
   and `derivations` included; the probe must have judged exactly this effective config).
4. `config-check --config STAGE/r3-config.json --cutoff D` prints the NEW `config_sha256`: the fetch
   evidence names that hash and `D`, not the hash of either file. The resolved config is read back as
   it is (its derived symbols are the ones in it; a `--cutoff` that is not the one it was derived for
   is refused).
5. The comment approving the fetch, `make-evidence --config STAGE/r3-config.json --cutoff D
   --run-id R3-YAHOO --review COMMENT_URL --valid-from-utc T --valid-until-utc T --max-requests N
   --out FETCH_EVIDENCE` (a window that contains the start instant, which is not before
   2026-11-07T00:00:00Z), then `fetch --config STAGE/r3-config.json --cutoff D --run-id R3-YAHOO --stage-root STAGE --governance
   FETCH_EVIDENCE [--allow-unresolved]`, then `verify STAGE/R3-YAHOO`, which must print `VERIFIED`.

The clock: the fetch refuses a group whose `through` day has not closed, and a day is closed only
once the UTC date of the fetch's start timestamp is later than it (`run_fetch`:
`group.through >= now.astimezone(utc).date()`; `require_closed_days` applies the same comparison
before any evidence is read). The live command freezes that timestamp once, before it reads the
evidence, so the fetch may start no earlier than `00:00:00` UTC on the day after `D`: for
`D` = Friday 2026-11-06 that is 2026-11-07T00:00:00Z = Friday 19:00 EST (US daylight time ended
2026-11-01, so Eastern is UTC-5). A start at 18:59 ET is refused. Governance evidence is also
checked against that timestamp (`valid_from_utc` <= start <= `valid_until_utc`, at most 24 hours
between them; `authorize_live`). The evidence does not mention `D` beyond its `cutoff` field and
is never compared with the closed-day time, so it may be issued at any time (Friday evening,
Saturday, even Sunday) as long as its window contains the instant the fetch starts: the only
deadline is the evidence's own expiry, not 19:00 ET. The evidence also names the effective config
hash and the run id, which only exist after the Schwab positions are captured, the probe has run
and `apply-admissions` has written the resolved config (each step changes the hash), so the probe
evidence can be issued after the positions and the first `config-check`, and the fetch evidence
only after `config-check` on `r3-config.json`; neither can be prepared on Friday for a config
resolved on Saturday. The evidence is single use and is consumed before the run starts, which is
why the closed-day check now runs first:
a start that is too early is refused with the evidence unspent. `config-check`, `probe` and
`apply-admissions` do not apply the rule (the code has no closed-day check in them), so they can
run before 19:00 ET; whether the probe's reference prices need the close of `D` is not established
here. `verify` applies the closed-day rule at its own clock as well.

## Official closes for the Schwab ingest

A daily `yfinance` group with `history` and `official_closes: {}` also emits
`official-closes/<SYMBOL>_yahoo_daily.csv`, the format `tools/schwab_generation.py`
(`_official_closes`) and the Schwab capture ingest read: header
`date,open,high,low,close,adjclose,volume,dividend`, one row per bar. The ingest values
a position as quantity x the `close` column of the cutoff date, so `close` is the raw
daily Close (`raw/<group>/history.csv` column `Close`, text copied unchanged; Yahoo's
`Close` is split-adjusted, not dividend-adjusted) and `adjclose` is `Adj Close`. The
`history_adjusted` artifact has `Close = Adj Close` and must never feed a valuation.
Files from several groups are merged into one directory; two groups disagreeing on a
bar refuse the run, and every name must have a bar on the cutoff date and on the previous
trading day (the newest day before the cutoff on which any symbol has a bar, so a market
holiday is not one; a held symbol that lacks it rejects the run and is named with the day). A
group with
`symbols_from: "schwab-positions"` takes its symbols from `--schwab-positions
<run>/normalized/positions.json` (`/` and `.` become `-` for Yahoo; the file name is the
Schwab symbol with `/` written as `.`). `verify` rebuilds each file from the raw CSV and
rejects any difference.

`MANIFEST.json` records `fetch_started_utc` (the clock the governance receipt used) and
`fetch_completed_utc`, both `Z` timestamps, and the cutoff; `verify` re-checks presence,
UTC form, `completed >= started` and that the fetch started after the cutoff day closed.
`official-closes/official-closes-manifest.json` sits beside the files so the ingest need
not open the run: per file the symbol, sha256, size, first and last bar, the cutoff bar
and its raw close, the previous trading day's bar and raw close (`previous_bar`,
`previous_close`), plus the run id, cutoff, config hash and the same two timestamps;
`verify` rebuilds it from the files and the header. The file name is the ingest's alias
key: the Schwab symbol with `/` written as `.` (`BRK/B` -> `BRK.B_yahoo_daily.csv`).

For R3 the `held-closes` group takes every symbol of the Schwab run's `positions.json` (the
derived valuation of day D needs the closes of D and of the previous trading day, which the
fetch now requires for every one of them) except those named in the group's `exclude_symbols`
(Schwab symbols; shipped: `SWVXX`). `positions.json` holds the equity and ETF rows only: the
Schwab driver writes the cash group's rows, the sweep money-market fund `SWVXX` among them (valued
by the derive step as cash at its stated NAV, as the R2.1 pull list did), to a separate sweeps
list, so `SWVXX` is not in the derived list anyway and the exclusion is a guard for the day it
appears as a position. An excluded symbol that is not held is ignored, the symbols actually left
out are recorded in the config's derivations, and the exclusion is part of the effective config
hash the evidence names. The derive step reads the per-symbol `<SYMBOL>_yahoo_daily.csv` (header
`date,open,high,low,close,adjclose,volume,dividend`; `date` and `close` are the columns it needs)
and the sidecar's per-file sha256; a held symbol without a close for D and for the day before is
its refusal, never a skipped symbol (`tools/schwab_nav_derive.py`, PR 830).

Before any evidence is minted (the Schwab capture has produced the held list): run
`held-check --positions RUN/normalized/positions.json --served-dir DIR [--served-dir DIR ...]`.
It is offline and lists the held symbols that no retained pull ever served (a
`<SYMBOL>_yahoo_daily.csv` in a given directory), and exits 1 if there is any, so a symbol Yahoo
does not serve is found before it can reject a run and spend the single-use evidence;
`--allow-unseen` reports without refusing. With `--ingest-config INGEST.json` (the Schwab ingest
config) it also names the held symbols that config aliases (`aliased_by_ingest`, symbol to the
file spelling the ingest looks for, which the driver does not write: it names the file by the held
symbol) or values at 1.00 as cash (`cash_equivalent_by_ingest`: a cash-equivalent that appears in
`positions.json` is fetched for nothing and belongs in `exclude_symbols`); both are reports, not
refusals, so they can be decided before Saturday. The retained R2.1 pulls
(`dataPortfolio/source-runs/r21-schwab-0917-closes-20261030` and `inputs/spy-spxl-yahoo`) served
all 15 non-cash symbols of the R2.1 09-17 capture (BIL, CL, DGP, ERX, IBIT, NEE, PEP, PG, SOXL,
SPXL, SPY, TQQQ, UDOW, UPRO, XLE; the "38 positions" of that pull list are rows across accounts,
16 symbols with `SWVXX`). A symbol that appears in the R3 capture and is not in those directories
is "never served" and needs a read-only Yahoo chart request by hand before the evidence is minted.

### Close vintage of a late fetch, and `rescale-check`

R3 uses the raw daily `Close` (split-adjusted by Yahoo, not dividend-adjusted) for every
official close; `Adj Close` is fetched beside it for history only and feeds no D close. A
dividend after D cannot move `Close`; a split after D rescales every earlier close, D included.
The request ends at D, so a pull after D cannot see a later split (the `split_event_count` in
the adjustment record covers days up to D only). The retained earlier pull is the witness:
`rescale-check --closes-dir NEW/official-closes --previous-dir DIR [--previous-dir DIR ...]
[--tolerance 1e-4]` compares each new `<SYMBOL>_yahoo_daily.csv` with the same symbol's retained
file on the days both hold, and exits 1 if any symbol's close differs by more than the tolerance
(`RESCALED`, with the worst day and the ratio) or shares no day with the retained pull
(`no_overlap`: the check proved nothing). A symbol the retained pulls never held is listed as
`not_in_previous`, warned about on stderr and not checked (`--require-previous` makes it exit 1
too, for a run where every symbol must have a retained pull).

Every retained pull is its own vintage and is compared with the new closes separately (nothing is
merged by argument order, so a later pull that already holds split-adjusted closes cannot hide a
split that the oldest pull still shows). The report lists `vintages_compared` and, per symbol, a
`vintages` entry for each retained file (and for the last day of each `--previous-adjusted-json`,
`basis` `adjusted_last_day`) with its own status, worst day and ratio; `rescaled_sources` names
the files that show the rescale, and the check exits 1 if any vintage does. Two retained
vintages that disagree with each other on a shared day beyond the tolerance are reported as
`RETAINED_VINTAGES_DISAGREE` (a split between the two pulls, or a bad pull) with the sources, day
and ratio, and also exit 1. A close that is `nan`, infinite, zero or negative in a new file, a
retained file or an adjusted json row is refused with a `DriverError` naming the file and date.
The `held-closes` window starts 60 days before the cutoff so
that it overlaps a retained pull that ended at the previous cutoff (R2.1 ended 2026-09-17); run
the check after the fetch and before the Schwab ingest. Run it on the Saturday pull too: it is a
no-op when nothing happened and the only gate if a symbol split between the previous pull and D.

## Symbol resolution and admission

An id in `symbol_resolution` is fetched only through `resolved_ticker`, which requires
`status CONFIRMED` and `evidence`. It is never passed to Yahoo as a plain ticker (`ASB`
is a different, real listing). `fetch` refuses an unresolved id unless
`--allow-unresolved`, which records it as `NOT_FETCHED_UNRESOLVED`.

Each id carries a `reference` built from retained bytes: the security's identity (P123
holdings row: name, currency, country) and independent prices (IBKR statement marks in
local currency on named dates, or, for an FX id, the IBKR FX observations), with the
source files and their sha256. `probe` fetches every candidate (live and governed, or
from `--recorded-dir`, a directory of recorded chart responses, which is offline and
needs no evidence) and judges it with `admission-rule-v1`: a candidate is ADMITTED only
if (1) the provider currency equals the reference currency (pence quotes are rescaled by
100 and read as GBP), (2) the provider exchange code is in `accepted_exchanges`, (3)
every `name_tokens` entry appears in the provider's long or short name, and (4) the
close on each reference date is within `tolerance`, with at least `min_points` dates
present and none outside tolerance. Anything else stays UNRESOLVED with every check
recorded in `probe-report.json`; two admitted candidates are AMBIGUOUS and admit
nothing. `verify` re-judges every candidate from its raw bytes.
`apply-admissions --config C --probe-run DIR --out NEW` (create-only) writes the config
with each admitted id CONFIRMED, its evidence naming the probe run and raw hash; the
probe must have judged exactly that config.

## Fail closed

A group is refused (a `<run-id>.REJECTED.json` report is written, no run directory) on:
empty or missing ticker, fewer than `min_rows`, a first bar later than
`start + first_bar_tolerance_days`, a last bar before the last expected session on or
before `through` (minus `allowed_last_bar_lag_days`), duplicate dates, more missing sessions than `max_missing_weekdays`, more null closes
than `max_null_closes`, or a `through` that is not a closed UTC day. Only an explicit
`exceptions[]` entry (`symbol`, `rule`, `label` matching `[A-Z][A-Z0-9_]{5,63}`) waives
`first_bar`, `last_bar`, `min_rows`, `missing_weekdays` or `null_closes`; the label is
written into the coverage report. Empty data and duplicates cannot be waived, and a bar
after `through` is refused (chart responses are trimmed into the derived file instead).

One live bar is ignored, and only that one: Yahoo appends a bar stamped with the current time
to an FX chart while the market is open, even for a window that ended in the past. A chart bar
that is both off local midnight AND dated after the cutoff is skipped (counted as
`live_bars_ignored`; the raw response is staged unmodified and the derived file drops it); an
off-midnight bar on or before the cutoff still refuses.

## Staging

Run layout under `<stage-root>/<run-id>/`: `raw/<group>/history.csv` (the validated raw
frame) or `raw/<group>/<TICKER>.chart.json` (the response bytes as received) with a
`derived/<group>/<TICKER>.chart.json` (the same response with every bar after `through`
removed; coverage is computed on it, the raw is kept, both are hash-listed, and `verify`
recomputes the derived file from the raw),
`artifacts/` (parsed artifact plus sibling manifest per dataset), `config.json` (the
effective config), `coverage.json`, and `MANIFEST.json` written last, binding every file
by sha256 and size, the config hash, the adapter file hash, the cutoff and the request
count. Every file is opened create-only. Stage outside Git and outside synced storage
(`C:\Dev\scratch\<project>\`).

`coverage.json` reports per symbol: rows, first and last bar, expected last session,
last-bar status, missing sessions (counted and listed up to 60), null closes and the
adjustment mode: split and dividend event counts, ratio of Adj Close to Close at the
first and last bar, and the dataset modes (`history`: Close is split-adjusted by Yahoo,
Adj Close is split+dividend adjusted; `history_adjusted`: OHLC scaled by AdjClose/Close,
Close = Adj Close).

## Commands

```
python tools/yahoo_fetch_driver.py config-check --config <config> [--cutoff D] [--tt-source-manifest M]
python tools/yahoo_fetch_driver.py probe  --config <config> --stage-root <root> --run-id <id> (--governance <evidence.json> | --recorded-dir <dir>) [--cutoff D] [--tt-source-manifest M]
python tools/yahoo_fetch_driver.py apply-admissions --config <config> --probe-run <root>/<id> --out <new-config> [--cutoff D] [--tt-source-manifest M]
python tools/yahoo_fetch_driver.py fetch  --config <config> --stage-root <root> --run-id <id> --governance <evidence.json> [--cutoff D] [--tt-source-manifest M] [--schwab-positions P] [--allow-unresolved]
python tools/yahoo_fetch_driver.py dry-run --config <config> --cutoff D [--tt-source-manifest M] [--schwab-positions P] [--allow-unresolved] [--stage-root <scratch>] [--run-id DRY-id] [--max-requests N]
python tools/yahoo_fetch_driver.py verify <root>/<id>
python tools/yahoo_fetch_driver.py verify-retained --artifact A [--manifest M] --cutoff D --min-rows N --max-missing-weekdays N [--calendar every_day] [--kind chart --symbol S --start D --max-null-closes N [--derive-out path]] [--report path]
```

`fetch` and `probe` touch the network only with governance evidence, and `dry-run` is the
evidence-free rehearsal of `fetch` (above): single-use JSON naming `run_id`, `config_sha256` (printed by
`config-check`), `cutoff`, a quant_engine issue-comment URL as `review`, a UTC window of
at most 24 h, and `max_requests` (1..5000, enforced on every transport call). `verify`
re-derives hashes, config binding, coverage, rows against the raw bytes, and every
fail-closed rule using only the standard library, and rejects unbound, missing, edited
or symlinked files. `verify-retained` applies the same checks to a tape already on disk
and never fetches.

## Limits (UNVERIFIED)

Never run live. The live transports (`LiveChart`, the yfinance module) are not exercised
by the repository tests. Chart `period1/period2` padding assumes London-stamped
`CCYUSD=X` pairs. Only the NYSE calendar is modelled (`calendar: nyse`); another exchange's
holiday or a cross-exchange cutoff needs a label or a higher `max_missing_weekdays` under
`weekday`. Hourly groups are checked by bar date only, not by bar time.

The yfinance cache (its cookie and timezone sqlite files) is ONE directory for the whole process
(`yf_cache_dir`, removed at exit), never one per group: yfinance binds its cache singleton to the
first location it is given, and a directory deleted after the first group made every later group
fail with `unable to open database file` (Windows). The adapter's cached results in it
(`history_*.csv`, `current_snapshot_*.json`, `ticker_search_*.json`) are deleted before each group,
so every group is a fresh read.

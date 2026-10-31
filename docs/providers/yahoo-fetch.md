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
ISO date, `calendar` `weekday|every_day`, `min_rows`, `max_missing_weekdays`, optional
`datasets`, `artifact_profile` `tape|capture-v0`, `max_null_closes`,
`first_bar_tolerance_days`, `allowed_last_bar_lag_days`, `exceptions[]`), `limits`,
`symbol_resolution[]`. The request end is always `through + 1 day` (exclusive). Unknown
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
python tools/yahoo_fetch_driver.py fetch  --config <config> --stage-root <root> --run-id <id> --governance <evidence.json> [--cutoff D] [--tt-source-manifest M] [--allow-unresolved]
python tools/yahoo_fetch_driver.py verify <root>/<id>
python tools/yahoo_fetch_driver.py verify-retained --artifact A [--manifest M] --cutoff D --min-rows N --max-missing-weekdays N [--calendar every_day] [--kind chart --symbol S --start D --max-null-closes N [--derive-out path]] [--report path]
```

`fetch` and `probe` are the only commands that touch the network, and only with
governance evidence: single-use JSON naming `run_id`, `config_sha256` (printed by
`config-check`), `cutoff`, a quant_engine issue-comment URL as `review`, a UTC window of
at most 24 h, and `max_requests` (1..5000, enforced on every transport call). `verify`
re-derives hashes, config binding, coverage, rows against the raw bytes, and every
fail-closed rule using only the standard library, and rejects unbound, missing, edited
or symlinked files. `verify-retained` applies the same checks to a tape already on disk
and never fetches.

## Limits (UNVERIFIED)

Never run live. The live transports (`LiveChart`, the yfinance module) are not exercised
by the repository tests. Chart `period1/period2` padding assumes London-stamped
`CCYUSD=X` pairs. Exchange holidays are not modelled: a holiday or a cross-exchange
cutoff needs a label or a higher `max_missing_weekdays`. Hourly groups are checked by
bar date only, not by bar time.

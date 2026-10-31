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

## Symbol resolution

An id in `symbol_resolution` is fetched only through `resolved_ticker`, which requires
`status CONFIRMED` and `evidence` (`probe_run_id`, `raw_sha256`, `confirmed_by`,
`note`). It is never passed to Yahoo as a plain ticker (`ASB` is a different, real
listing). `fetch` refuses an unresolved id unless `--allow-unresolved`, which records it
as `NOT_FETCHED_UNRESOLVED` in the manifest. `probe` fetches the listed candidates (and a
`search_query` if given) into its own run labelled `CANDIDATE_NOT_ADMITTED`; nothing is
promoted automatically. Promotion is an edit of the config by a human after reading the
probe report.

## Fail closed

A group is refused (a `<run-id>.REJECTED.json` report is written, no run directory) on:
empty or missing ticker, fewer than `min_rows`, a first bar later than
`start + first_bar_tolerance_days`, a last bar before the last expected session on or
before `through` (minus `allowed_last_bar_lag_days`), any bar after `through`,
duplicate dates, more missing sessions than `max_missing_weekdays`, more null closes
than `max_null_closes`, or a `through` that is not a closed UTC day. Only an explicit
`exceptions[]` entry (`symbol`, `rule`, `label` matching `[A-Z][A-Z0-9_]{5,63}`) waives
`first_bar`, `last_bar`, `min_rows`, `missing_weekdays` or `null_closes`; the label is
written into the coverage report. Empty data, post-cutoff bars and duplicates cannot be
waived.

## Staging

Run layout under `<stage-root>/<run-id>/`: `raw/<group>/history.csv` (the validated raw
frame) or `raw/<group>/<TICKER>.chart.json` (the response bytes as received),
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
python tools/yahoo_fetch_driver.py config-check --config <config>
python tools/yahoo_fetch_driver.py probe  --config <config> --stage-root <root> --run-id <id> --governance <evidence.json> [--cutoff D]
python tools/yahoo_fetch_driver.py fetch  --config <config> --stage-root <root> --run-id <id> --governance <evidence.json> [--cutoff D] [--allow-unresolved]
python tools/yahoo_fetch_driver.py verify <root>/<id>
python tools/yahoo_fetch_driver.py verify-retained --artifact A [--manifest M] --cutoff D --min-rows N --max-missing-weekdays N [--calendar every_day] [--kind chart --symbol S --start D --max-null-closes N] [--report path]
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

# French fetch driver

`tools/french_fetch_driver.py` is the governed, config-driven fetch of the Kenneth French
library daily feeds (public, read-only, no credentials). It follows the shape of
`tools/yahoo_fetch_driver.py` (`config-check`, `make-evidence`, `fetch`, `verify`) and wraps the one
maintained adapter, `skills/agent-market-data-terminal/scripts/fetch_famafrench.py` (v1.2.0); it
defines no second format. The adapter refuses an `--output` outside its current directory, so the
driver runs it once per feed with the staging `raw/` directory as its working directory.

## Config (data, not code)

`docs/providers/french-fetch/<cycle>.config.json`: `cutoff`, `evaluation_date`, `policy_id`
(`KENNETH_FRENCH_MONTHLY_DAILY_45D_V1`), `feeds[]` (`feed_key`, `feed_id`) and `limits.max_requests`.
The five `feed_key`s are exactly the keys `portfolio_public_decomposition_sources` requires:
`us_factors`, `us_momentum`, `europe_factors`, `europe_momentum`, `us_industries`. A `feed_id` is
a `_CSV` zip stem (no path, no TXT). Unknown keys are refused.

## Sequence

```
python tools/french_fetch_driver.py config-check --config docs/providers/french-fetch/r4-d.config.json
python tools/french_fetch_driver.py make-evidence --config <config> --run-id <ID> --review <issue-comment URL> \
    --valid-from-utc <T> --valid-until-utc <T+4h> --max-requests 5 --out <evidence.json>
python tools/french_fetch_driver.py fetch --config <config> --stage-root <dir outside Git/OneDrive> \
    --run-id <ID> --governance <evidence.json>
python tools/french_fetch_driver.py verify <stage-root>/<ID>
```

The evidence binds the config sha256, the run id, the cutoff, a validity window and a request cap
(at least one request per feed, at most the config cap); the review is a GitHub issue-comment URL.

## What `fetch` writes (create-only; a failure removes the partial run)

`raw/<feed>.json`, `.json.manifest.json`, `.json.source.zip` per feed; `config.json`,
`governance-evidence.json`; `publication-log.json`; `public-sources.factor-feeds.json` (the
`qe-release-public-sources/1` block `tools/release_public_bundles.py` consumes, paths relative to
`raw/`, with pins); `MANIFEST.json` (config, evidence and adapter hashes, run window, file
inventory); `SHA256SUMS.txt` last.

## Loud failure rules

Refused: evidence for another config, run, or cutoff, outside its window, or with a cap below the
feed count; an existing run folder; an adapter failure; a missing, extra or symlinked file in
staging; a parsed file, source zip, zip member or csv member that differs from its manifest; a
request or `final_url` that is not the configured feed on `mba.tuck.dartmouth.edu`; a response
status other than 200; a missing or unreadable `Last-Modified`; any section whose last row is after
the cutoff.

## Publication rule (45 days)

A month is required at the evaluation date when its month end plus 45 calendar days is on or before
that date (the same boundary as `portfolio_public_decomposition_sources`, pinned by a parity test over
two years). A feed whose last row is in an earlier month is `HARD_STALE`, otherwise `COMPLIANT`.
`publication-log.json` records, per feed, `Last-Modified`, the last row, the lag in days after that
month end, the required month and the state; `verify` re-derives all of it from the staged bytes.
The log is the evidence for the "typical publication lag" claim: keep each cycle's log.

Raw run folders are restricted evidence: keep them outside Git and OneDrive and reference them by
hash.

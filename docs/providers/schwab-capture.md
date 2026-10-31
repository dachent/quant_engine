# Schwab capture driver

`tools/schwab_capture_driver.py` captures the three Schwab families a release
cutoff needs under one governance gate: holdings at the cutoff close, historical
balances, and all-time transactions. Transaction paging, the redaction rule and
the WCH transaction shape are reused unchanged from
`tools/schwab_transaction_capture.py`; that file, `tools/schwab_generation.py`
and the `quant_engine.schwab` package are not modified (a test pins their bytes).
`tools/schwab_capture_ingest.py` is the adapter from a verified run to the
inputs `tools/schwab_generation.py` consumes (see "Ingest adapter" below).
`docs/schwab-generation.md` and `docs/schwab-matrixport-security-quarantine.md`
still govern what happens to the staged output.

Nothing in the repository exercises the live path. The tests use a recording
fake transport and invented values.

## What it refuses

- A live read with no evidence file, or with evidence that is expired, not yet
  current, already consumed, for another cutoff, for another catalog or terms
  hash, or whose scope does not name every catalog read exactly.
- Any request not named in the evidence scope, any request past the approved
  budget, and any retry (`retry_count` must be 0). Each request is recorded in
  a durable receipt before the transport is called.
- Holdings whose per-row as-of date (`price.priceDate`) differs from the declared
  cutoff close (16:00 America/New_York on the cutoff date). Only an explicit
  `exception_label` in the approved evidence admits an offset, and then every
  affected row carries the label and `as_of_offset_days`.
- A capture before the close, or more than 8 hours after it, without that same
  label. The R2 pattern (an end-of-day book captured at 23:59 New York, stored
  under the next UTC date) is accepted and recorded: rows keep the cutoff
  `as_of_date`, and the output records `capture_date_utc`,
  `capture_date_new_york`, `captured_at_utc`, `capture_lag_hours` and
  `as_of_basis = CAPTURED_AFTER_NEW_YORK_CLOSE_OF_AS_OF_DATE`.
- A held position (nonzero quantity), including a sweep row, whose market value
  is null, missing, or not a finite decimal, a row without a price or whose
  value is not quantity times its quoted price, and a cash summary row without a
  value. The older Holdings-V2 normalizer in `quant_engine.schwab` records such
  rows as unavailable; this driver stops and names only the reason and a count.
  A row with no price date is refused rather than dated from capture time.
- Unknown groups, duplicated identities, accounts outside the crosswalk, a
  crosswalk that is not one-to-one, and a balance series with no row at the
  cutoff (unless labelled). The balance response must be exactly
  `{"histories": {"M/D/YYYY": {"AccountValue": <number>}}}`; any other field,
  date spelling or value type is refused.
- Overwriting any staged file. Raw staging is create-only under a caller-supplied
  root; the manifest is written last and binds every file by SHA-256 and size.
  A run that stops early leaves no manifest and fails verification.

Raw responses can contain provider account ids and are restricted evidence
(`docs/restricted-evidence-output.md`): keep the staging root outside Git and
outside synced bulk storage, and summarise it only with
`tools/restricted_evidence_summary.py`. Normalized output and the manifest use
internal account ids only.

## Commands

```powershell
uv run --frozen --no-sync python tools/schwab_capture_driver.py catalog
uv run --frozen --no-sync python tools/schwab_capture_driver.py capture `
  --evidence <evidence.json> --receipt-root <receipts> --cdp-url <url> `
  --crosswalk <crosswalk.json> --selected-source-root <retained-source> `
  --staging-root <staging> --run-id <id> --cutoff <YYYY-MM-DD>
uv run --frozen --no-sync python tools/schwab_capture_driver.py normalize-holdings `
  <raw.json> <crosswalk.json> <out.json> --cutoff <date> --captured-at <utc> [--exception-label LABEL]
uv run --frozen --no-sync python tools/schwab_capture_driver.py verify <run-dir> [--crosswalk <crosswalk.json>]
uv run --frozen --no-sync python tools/schwab_capture_ingest.py ingest <run-dir> `
  --crosswalk <crosswalk.json> --cutoff <YYYY-MM-DD> `
  --official-closes-dir <dir of SYMBOL_yahoo_daily.csv> --output-root <new dir> `
  [--base-spec <generation spec> --spec-out <new file>]
```

`verify` re-hashes every artifact, rejects unbound files, re-checks the as-of,
market-value and terminal-history invariants from the bytes (so a forged but
re-hashed file still fails), and, given the crosswalk, rejects any provider id
in non-raw output and re-derives the positions and the balances from the raw
responses.

## Governance evidence

Operator-supplied, never copied into a run. Fields (exact set): `schema_version`
1, `operation` `capture`, `cutoff` {`as_of_date`, `close_time_zone`
`America/New_York`}, `exception_label` (null or `UPPER_SNAKE_CASE`), `scope`
{`account_count`, `requests`: one entry each for `balances.historical`,
`holdings.v2`, `transactions.history` with `method`, `path`, `query`, `body`},
`review` {`identity`, `reference` = a quant_engine issue-comment URL},
`authority` {`status` `CURRENT`, `issued_at`, `expires_at`}, `terms` {`version`,
`sha256`}, `catalog` {`sha256`} (printed by the `catalog` command), `metering`
{`max_requests`, `retry_count` 0}. The review reference is shape-checked only,
as in the Matrixport gate.

Request entries are fixed by the catalog: `holdings.v2` is a `POST` of
`/api/is.Holdings/V1/Holdings/HoldingV2` with an owner-supplied `body` (a
nonempty object using only the key names the Schwab SPA sends: `entitlement`,
`featureFlags`, `fetchSettings`, `groupByCallPut`, `groupBySecurityType`,
`groupByStrategy`, `groupByUnderlying`, `isExtendedPricing`, `isNWE`,
`selectedColumns`, `sortSettings`), sent with an empty `Schwab-Client-Ids` header
so one call returns every account; `balances.historical` is a `GET` with an empty
`query`, scoped per account by `Schwab-Client-Ids`; `transactions.history` is the
reviewed POST. `body` is `null` for the two requests the driver builds itself.

## Credentials

None. Capture attaches over CDP to the owner's already-authenticated browser and
replays the page's own request headers in memory; nothing is read from a
credential store, so no `[credentials.schwab]` plan is needed. A future
Developer API route would go through the core credential resolver.

## Shape review against preserved real evidence

Compared offline with `dataSchwab` (never live; no account number or balance is
reproduced here): the 2026-09-17 Holdings-V2 response and its capture receipt,
the 2026-07-31 HAR (which records the Schwab SPA bundle and the historical
balance requests), `schwab_playwright_capture.py` from that run, and the 16
retained historical-balance responses (2026-07-31 and 2026-09-03 captures).

| Item | Status | Evidence |
| --- | --- | --- |
| Holdings-V2 path `/api/is.Holdings/V1/Holdings/HoldingV2` | CONFIRMED | `capture-receipt.json` `positions.source_path`; SPA bundle `holdingsV2Uri` |
| Holdings-V2 method | CONFIRMED POST (driver had GET) | SPA `updateHoldingsData` calls `httpClient.post` |
| Holdings-V2 scoping | CONFIRMED all-accounts = empty `Schwab-Client-Ids` | SPA `"AllAccts"===group ? "" : account number` |
| Holdings-V2 request body | key names CONFIRMED, required subset UNVERIFIED | SPA `getRequestDataForPost`; the 09-17 body was not retained |
| Per-row price date | CONFIRMED `price.priceDate`, `MM/DD/YYYY` (driver read a top-level `priceDate` and would have refused every real row) | 39 of 39 priced rows |
| Row fields `qty.qty`, `marketValue.val`, `costBasis.cstBasis`, `dataSymbol`, `symbol.symbol`, `tradeTimeMs`, `closePrice.val` | CONFIRMED | all priced rows |
| Groups | CONFIRMED `Equity`/1, `ETF`/2, `Cash`/9 only | 17 groups |
| Cash group | CONFIRMED to hold a quantity-less `rowType` 3 summary row per account plus quantity rows (driver would have failed on the summary row) | 8 summary rows, 1 sweep row |
| `marketValue.val` | CONFIRMED provider-quoted `qty * price.price`, NOT the official close: `price.price` is the last trade (`lastTrade` after the close) and differed from the official 09-17 close for 30 of 34 symbols; `closePrice` equals the 09-16 close for 34 of 34 | 39 of 39 rows; Yahoo closes under `dataPortfolio` |
| `balances.historical` path | CONFIRMED | HAR request, capture script `HIST_BAL_URL`, web contract |
| `balances.historical` query | CONFIRMED none | HAR request query empty |
| `balances.historical` `Schwab-Client-Ids` on a GET | CONFIRMED | HAR request headers; capture script |
| `balances.historical` response | CONFIRMED `{"histories": {"M/D/YYYY": {"AccountValue": number}}}` (driver parsed `historicalBalances`/`date`/`accountValue`, which does not exist) | 16 of 16 files |
| Series end date | CONFIRMED one day before the capture date for the 2026-07-31 and 2026-09-03 captures; calendar days, 730 rows for a full-history account | 16 of 16 files |
| Series end date for a capture near midnight New York | UNVERIFIED | no retained capture; the 09-17 run captured no balances |
| `SchwabData_*.xlsx` (Strategies_dataSchwab-Trading) | Trader-API views (`Accounts`, `Portfolio`, `Order Status`); no Holdings-V2 or historical-balance shape to compare | 180 files |

Consequences. The driver now POSTs Holdings-V2, reads `price.price`/`priceDate`
from the nested object, treats the cash summary row as an account balance, parses
`histories`, and records `market_value_basis = PROVIDER_QUOTED_QTY_TIMES_PRICE_NOT_OFFICIAL_CLOSE`
with each row's `price_usd` and `quoted_at_utc`; it checks value = quantity times
price and re-derives balances from raw in `verify`. Because the series ends the
day before the capture date, a capture on the cutoff date has no balance row at
the cutoff and is refused unless labelled; the cutoff-day NAV then comes from
the official-close derivation as in R2.

Still UNVERIFIED (also written to every manifest as `unverified_shapes`): the
Holdings-V2 body subset the service requires, the empty `Schwab-Client-Ids`
scoping of the all-accounts POST, the balance series end date for a capture near
midnight New York, and groups other than Equity, ETF and Cash. Early-close days
(13:00) are not modelled.

## Ingest adapter

`tools/schwab_capture_ingest.py ingest` turns one run that passes `verify` (with
the crosswalk, so raw re-derivation runs) into what `tools/schwab_generation.py`
consumes, under a new create-only output root:

- `current/current-snapshot.json`: the schema-2 current snapshot, produced by the
  frozen `read_holdings_v2_current_snapshot` over the raw response. Positions are
  valued at quantity times the **official close** of the cutoff date from
  `<SYMBOL>_yahoo_daily.csv` files; the provider-quoted value is never used. The
  snapshot `observed_at` is the valuation instant (the cutoff close), whose UTC
  date is always the cutoff date, so the frozen consumer's date check holds.
- `nav/<account>.csv`: eight `WCH_NAV_CSV` files from the raw `histories`, columns
  `account_id,account_name,date,account_value`, rows up to the cutoff.
- `terminal/`: the terminal transaction package (pages, normalized rows, and the
  manifest re-emitted with `started_at` and `finished_at` expressed at the New
  York offset, the same instants, so the frozen consumer compares the New York
  date; the original and bound manifest hashes are recorded).
- `spec-fragment.json`: `as_of_date`, `current_snapshot`, `roots`, the sixteen
  WCH sources and `transaction_history_manifest`, each hash-bound. With
  `--base-spec` and `--spec-out` the fragment is merged into an existing spec
  (crosswalk, baseline and manual sources kept; crosswalk must equal the run's)
  and written to a separate restricted file because it names provider ids.
- `ingest-binding.json`, written last: `as_of_date` (the cutoff), the full
  capture block with `capture_date_utc`, `capture_date_new_york` and
  `capture_date_utc_offset_days`, the driver manifest and evidence hashes, the
  official-close file hashes, counts, NAV row counts, and `unverified_shapes`.

Before the binding is written the adapter re-checks over the written files the
date rules the frozen consumer applies (snapshot observation UTC date and every
position as-of equal the cutoff; the terminal manifest finishes no later than the
cutoff in New York time; every page of an account requests one interval whose end
equals that finish date). The tests run the frozen `_current_snapshot`,
`_terminal_transaction_history_coverage` and NAV reader over the same files, and
show that the driver's own UTC-stamped manifest is refused. With `--base-spec` the
composed spec is also offered to `build_generation` and the outcome
(`ACCEPTED` or `REFUSED` with the reason) is reported; a refusal there (for
example manual sources dated for another cutoff) does not undo the bound output.

It refuses: a run that does not verify; a labelled exception run; a cutoff that
differs from the run evidence; a capture whose New York date is not the cutoff
date; a held position with no official close (names the count and the symbols);
and any output that already exists. A refused run leaves no binding and needs a
new output root.

## Design notes

- Evidence is single use and a failed run is not resumed: a run stops before
  writing its manifest, fails verification, and a retry needs a fresh approval
  and a new run id. This mirrors the Matrixport gate; a resumable partial run
  would let one approval authorise an open-ended number of reads.
- The holdings request start time, not only the response time, must lie inside
  the allowed window after the close.

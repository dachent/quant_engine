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
- Any capture outside the one unlabelled mode (the first New York date after the
  cutoff and before 20:00; see "Capture modes"), and a cutoff
  that is not a trading day, without the approved label. Every output records
  `capture_mode`, `capture_date_utc`, `capture_date_new_york`, `captured_at_utc`,
  `capture_lag_hours`, `next_session_open_utc` and an `as_of_basis`.
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

## Capture modes

Coordinator ruling: the unlabelled window is the proven +1 pattern only (the R2.1
baseline already consumes Schwab generation as-of 2026-09-18 for cutoff
2026-09-17).

| Mode | Window (New York) | Acceptance |
| --- | --- | --- |
| `NEXT_SESSION_PRE_OPEN` | the first calendar date after the cutoff, at or after midnight and before 20:00, and before 09:30 when that date is a trading day | accepted unlabelled. Friday cutoff: Saturday only (so the capture UTC date equals the New York date and the as-of is exactly the cutoff plus one). A non-trading date after the cutoff (weekend or holiday) is accepted only when it is the first calendar date after the cutoff (Thursday before Good Friday: Good Friday; Christmas Eve: Christmas Day) |
| `SAME_DAY_POST_CLOSE` | cutoff date, at or after 16:00 | labelled only (fallback: the balance row at the cutoff minus one day and the official-close NAV derivation); bound by the ingest |
| `BEFORE_CLOSE`, `OUTSIDE_STANDARD_WINDOW` | Sunday, Monday pre-open, Saturday 20:00 or later, a trading day after 09:30, anything later | labelled only; the driver stages them and records the mode, the ingest refuses to bind them (they exceed the proven +1 pattern) |

In the accepted mode every priced row must carry `price.priceDate` equal to the
cutoff, and every account's `balances.historical` series must hold an OBSERVED
row at the cutoff (the series ends the day before the capture date, which is the
cutoff for a Saturday capture; a missing cutoff row is refused unless labelled).
Valuation is unchanged: quantity times the official close of the cutoff date,
never the provider value. The trading calendar is the regular NYSE full-day
closures (`nyse_holidays`: New Year, MLK, Presidents, Good Friday, Memorial,
Juneteenth, Independence, Labor, Thanksgiving, Christmas, with weekend
observance); ad hoc closures and 13:00 early closes are not modelled.

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
  --official-closes-dir <dir of SYMBOL_yahoo_daily.csv> --config <ingest-config.json> `
  --output-root <new dir> `
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

## Late pull: the capture is rolled back to the cutoff close

A capture is the account at capture time T. Any capture taken after the cutoff close D
(Saturday, Sunday, Monday, any later day, at any hour) is bound without an exception
label: `tools/schwab_rollback.py` takes the captured quantities and cash back to the close of
D by undoing the rows of the run's own transaction history that are posted after D, and the
closure checks decide whether the result is right. `derive` prices the rolled-back
holdings; `ingest` binds them (the snapshot carries the rolled-back quantities and cash) and
records `roll_back` (rows undone and rows after D that carry no quantity or amount, per
account) in the binding; `declare` pins the transaction files and the ledger replays the
same function (`replay.transactions`). A capture with no row after D is bound exactly as
before.

The rule was fitted to retained bytes, not assumed (the 2026-01-19, 04-12, 07-17 and 09-04
position exports against the 3,299 retained transaction rows, all eight accounts, tolerances
1e-6 shares and 0.005 cash; `tests/schwab/test_schwab_rollback_retained.py`, skipped when the
sources are not mounted). Five rolled-back pairs reproduce the earlier snapshot exactly
(quantities and cash, 876 to 2,080 rows each):

| Row type | Share effect forward | Cash effect forward |
|---|---|---|
| Buy, Reinvest Shares | `+abs(shareQuantity)` | `amount` (signed, fees netted) |
| Sell | `-abs(shareQuantity)` | `amount` |
| Journal, Journaled Shares, Security Transfer | `shareQuantity` as signed | `amount` when present |
| dividends of every kind, reinvest legs, interest, fees, MoneyLink, bank transfer, wire, funds received, cash in lieu | none | `amount` |

* **Which rows:** posted after D (first date of `transactionDate`) and effective after D
  (`effectiveDate`). The retained bytes cannot tell the two dates apart (none of the 68 rows
  whose posting and effective dates differ straddles a snapshot date), so a row posted after D
  with an effective date on or before D, or the reverse, is refused by name rather than
  guessed. A month-end D pulled late can hit this: Bank Interest rows are posted the next
  business day "as of" month end.
* **Trade date, not settle date:** the snapshot cash is trade-date cash (a Buy on 2026-04-10
  that settles 04-13 is already inside the 04-12 cash row; settle-date accounting fails).
* **Symbols** join with `.` and `/` removed (`BRK.B` in the snapshot, `BRKB` in the rows).

Refused, each naming account, action and date: Stock Split and Spin-off (a quantity added
after D, while Yahoo `Close` is split-adjusted), any action outside the table, a row whose
quantity or amount does not fit its action, a negative rolled-back quantity, a position held
at D that the capture never listed (sold in full after D), and a cash change when the account
has not exactly one cash summary row. The closure checks of `derive` stay: the rolled-back
value against the native balance of D and of D-1, tolerance 0.05; the waiver option is unchanged and is not needed for a
roll-back.

**Refusals removed, and why each protected nothing.** The driver's `assess_timing` and the
ingest no longer refuse a capture for its weekday, its hour (the 20:00 New York window), the
next session's open, the first-New-York-date rule, a missing label, or mode
(`OUTSIDE_STANDARD_WINDOW` is now `LATER_DAY_ROLLED_BACK`, bound as is). The holdings rows'
quoted price date may differ from D without a label (it is informational; the value comes
from the official closes). Each of these guarded the assumption that the capture equals the
account at D; the roll-back removes the assumption and the closures test the numbers. Kept:
a capture before the cutoff close (the closing data does not exist), a same-day capture
(the balance history ends the day before the capture, so the cutoff-day balance row does not
exist; a label still admits the D-1 row), a cutoff that is not a trading day, a missing D
balance row or official close, an account-set mismatch, any closure failure.

**Unverified.** Stock Split, Spin-off, Cash In Lieu, Journaled Shares and Security Transfer
signs sit before every retained snapshot, and Journal-with-symbol nets to zero in every
window; settle-date cash for trades that straddle a snapshot; which of posting and effective
date the broker's own accounting follows; an account whose sweep fund is reported as a
position at T but as cash at D (or the reverse); a Monday native D balance row (it is
checked against the rolled-back value, not assumed).

## Live capture tooling: evidence, crosswalk, attach check

### Generating and checking the evidence

`make-evidence` writes the evidence for you (create-only; the driver fills the
`catalog` and `terms` hashes and the three fixed request entries) and then runs
the same checks `capture` runs, deleting the file if any fails:

```powershell
python tools/schwab_capture_driver.py make-evidence --output <evidence.json> `
  --cutoff 2026-11-06 --account-count <N> --holdings-body <body.json> `
  --review-identity <name> --review-reference <issue-comment-url> `
  --issued-at <RFC3339> --expires-at <RFC3339> --max-requests <M>
```

`check-evidence <evidence.json> [--receipt-root <dir>]` re-runs every
authorization check against the real clock without writing a receipt, so it never
consumes the evidence; with `--receipt-root` it also reports `already_consumed`.
Run it just before `capture`. `issued_at` must be in the past and `expires_at` in
the future when `capture` runs. `capture` itself is unchanged and still the only
thing that consumes evidence.

### Crosswalk and the first-minute live check

`build-crosswalk --selected-source-root <dir> --out <crosswalk.json>` writes the
restricted `{"crosswalk": [...]}` file from the retained terminal targets
(create-only; prints only the account count and the file hash, never an id). It
is exactly what `capture` compares to the targets, so the two cannot disagree.
The approved evidence `--account-count` must equal that count (the retained
`refresh-20260902` root, manifest sha256 `1b57aa1d...ba858fd0`, yields 8).

`attach-check --cdp-url http://127.0.0.1:9223` is the live check to run before
the single-use capture: it attaches, requires exactly one `client.schwab.com`
page, reloads it and reads one of the SPA's own API requests (the same step
`capture` starts with), and prints `ok`, `governed_requests: 0` and the observed
header names (never values). It needs no evidence and consumes nothing.

#### The holdings body, read from the page itself

No retained capture holds a Holdings-V2 request body (the 2026-07-31 HARs contain
the SPA bundle, which has only key names). `attach-check --cdp-url <url>
--capture-body-out <body.json> [--wait-seconds 20]` records the body of the
page's OWN HoldingV2 POST while it reloads the page (the body is read from
`request.post_data`; no request of ours, no header, cookie or token is
stored or printed). The body is reduced to the keys the gate allows and written
create-only; the output lists the key names and the file hash. Feed the file to
`make-evidence --holdings-body`.

If no HoldingV2 request is seen within the wait window the command refuses and
writes nothing. The Schwab positions/holdings page is then opened in the same signed-in
window (navigate, click and scroll are allowed there for any non-trading read; the
one guard refuses trade, transfer, withdraw, deposit, order, buy, sell, submit and
confirm-payment controls and never types) and `attach-check` is run again, or STOPs
and uses the retained UI method below.

#### Account-set compare

With `--crosswalk <crosswalk.json>`, `attach-check` also reads the response of that
same HoldingV2 request (only the `accounts[].accountId` list is kept; no balance
or position leaves the function) and compares the set to the crosswalk. It
reports `accounts_on_page`, `accounts_in_crosswalk` and `account_set_matches`;
on a difference it refuses, naming crosswalk accounts missing on the page by
internal id and extra page accounts only by an 8-character hash, never a
provider id. Run it before issuing evidence; `--account-count` must equal the
matched count.

#### Fallback: the retained UI method (R2.1)

If the driver's first live use fails, the documented fallback is the method that
produced `source-runs/r21-schwab-0917-official`: the "All Brokerage Accounts"
page's own HoldingV2 call (HTTP 200) saved as `all-brokerage-positions.json`, with
`capture-receipt.json` (selected UI perimeter, source path, file hashes), the
selected-account transactions, `official-nav-derivation.json` and the
`finality/` and `normalized/` directories. Ledger finality for a fresh capture
then comes from the same chain as the driver's output: derive, ingest and
declare (above), with the declared evidence wired into the cycle manifest. The
fallback needs its own review and the owner's say-so; it is not interchangeable
with a driver run without that.

#### Re-issuing evidence in the capture window

Evidence is single use and a failed run is not resumed, so a second evidence is a
new file with a new review comment. The cycle is `make-evidence` (writes and
validates) then `check-evidence`, each well under a second; the time is the review
comment and the `--issued-at`/`--expires-at` values, not the tools. Use a fresh
`--output` path each time.

### Rehearsal without a governed request

`attach-check` is the only live step that sends no governed request (no evidence is consumed,
nothing is stored but header names and the body hash). A read of the Holdings-V2 response is
itself the first governed request, so it has no dry run: the governance reservation is made,
the page sends its own request, and the body is checked against the approved body before the
response is read. Everything after the bytes arrive is rehearsed offline instead:
`tests/schwab/test_schwab_retained_pages_parse.py` runs the driver's date handling over every
retained raw page of refresh-20260902 (including the `MM/DD/YYYY as of MM/DD/YYYY` shape) and,
with `QE_SCHWAB_STAGED_RUN=<staging run dir>`, over the normalized rows of a staged run.

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
the cutoff and is refused unless labelled; the preferred mode captures after the
cutoff day so the cutoff row is observed. The same-day fallback keeps the R2
official-close NAV derivation.

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
  snapshot `observed_at` is provenance and always the REAL capture instant; the
  as-of is its UTC date (cutoff + 1 in the first-next-date mode, which the frozen
  generation requires). The financial day of every position is the explicit
  `valuation_cutoff` the adapter writes into the snapshot document (an optional
  key the generation accepts; it may not be later than the observation's New York
  date). The generation carries it into the catalog's `position_snapshot_as_of`,
  the provider ledger into its position rows, and the baseline, the ledger
  selection and the pipeline materializer take the financial day from it, falling
  back to the New York date of `observed_at` when it is absent, so every legacy
  generation reads exactly as before. The binding records the cutoff, both
  capture dates, the offset and the mode. Readers are traced in the READY note.
  Zero-quantity rows are removed from the raw response before the frozen reader
  sees it (the driver omits them before reading their price fields). The emitted
  `sweep_vehicle_count` counts quantity rows only: the frozen reader also counts
  the quantity-less cash summary rows (R2's 9 was 8 summaries and 1 sweep row;
  the adapter emits 1) and the adapter requires the reader's raw count to
  reconcile with the verified run.
- `nav/<account>.csv`: eight `WCH_NAV_CSV` files from the raw `histories`, columns
  `account_id,account_name,date,account_value`, rows up to the cutoff (an observed
  row at the cutoff is required; the labelled same-day fallback may end the day
  before and the binding says `cutoff_row_present: false`).
- `terminal/`: the terminal transaction package (pages, normalized rows, and the
  manifest re-emitted with `started_at` and `finished_at` expressed at the New
  York offset, the same instants, so the frozen consumer compares the New York
  date; the original and bound manifest hashes are recorded).
- `spec-fragment.json`: `as_of_date`, `current_snapshot`, `roots`, the sixteen
  WCH sources and `transaction_history_manifest`, each hash-bound. With
  `--base-spec` and `--spec-out` the fragment is merged into an existing spec
  (crosswalk, baseline and manual sources kept; crosswalk must equal the run's)
  and written to a separate restricted file because it names provider ids. The
  base spec and the destination are checked before the create-only ingest root
  exists, so a corrected retry can reuse the same root.
- `ingest-binding.json`, written last: `as_of_date` (the cutoff), the full
  capture block with `capture_date_utc`, `capture_date_new_york` and
  `capture_date_utc_offset_days`, the driver manifest and evidence hashes, the
  official-close file hashes, counts, NAV row counts, and `unverified_shapes`.

Before the binding is written the adapter re-checks over the written files the
date rules the frozen consumer applies (snapshot observation UTC date and every
position as-of equal the cutoff; the terminal manifest finishes no later than the
as-of date in New York time; every page of an account requests one interval whose
end equals that finish date). The tests run the frozen `_current_snapshot`,
`_terminal_transaction_history_coverage` and NAV reader over the same files, and
show that for a Saturday 23:00 New York capture (Sunday in UTC) the driver's own
UTC-stamped manifest is refused while the New York rendering is accepted. With `--base-spec` the
composed spec is also offered to `build_generation` and the outcome
(`ACCEPTED` or `REFUSED` with the reason) is reported; a refusal there (for
example manual sources dated for another cutoff) does not undo the bound output.

### Guards declared as data (`--config`)

The config is required and strict (exact keys, no defaults in code); its hash is
recorded in the binding:

```json
{
  "schema_version": 1,
  "price_ratio_tolerance": "0.05",
  "symbol_aliases": {"BRK/B": "BRK.B", "BRK.B": "BRK.B", "BRK-B": "BRK.B"},
  "cash_equivalent_symbols": ["SWVXX"],
  "price_ratio_exceptions": {
    "XYZ": {"label": "SPLIT_2_FOR_1_LABELLED", "corporate_action": "2:1 split on the cutoff date"}
  }
}
```

- Split and mis-scaling guard: for every held symbol the provider's own
  `price.price` (the driver's `price_usd`) is compared with the official raw
  Close used for the value; a symbol whose relative difference exceeds the
  declared tolerance is refused (count and symbols named) unless
  `price_ratio_exceptions` names it with an `UPPER_SNAKE_CASE` label and the
  corporate action; applied exceptions are recorded in the binding. The binding
  also records the tolerance, the largest observed difference, the symbols with
  a dividend or split in the pulled window (CSV `dividend` column and the run's
  coverage entries) and the closes' fetch time. Closes must come from a verified
  Yahoo fetch run (PR 702, head b1b036fa): `--official-closes-dir` is
  `<run>/official-closes` and
  - the run's `MANIFEST.json` (`kind` `yahoo-fetch-run`, the cutoff, every run
    file bound by SHA-256 and size, no unbound file) must verify;
  - the sidecar `official-closes/official-closes-manifest.json` (`kind`
    `yahoo-official-closes-manifest`, `run_id`, `cutoff`, `fetch_started_utc`,
    `fetch_completed_utc` as `Z` timestamps, `config_effective_sha256`,
    `name_rule`, and per file `symbol`, `file`, `sha256`, `size_bytes`,
    `first_bar`, `last_bar`, `cutoff_bar`, `cutoff_close`) must list every
    `<SYMBOL>_yahoo_daily.csv` with matching bytes, no file may be missing, each
    `cutoff_close` must equal the file's own cutoff row, and its run id and
    completion time must agree with the run manifest;
  - the fetch must start no earlier than the cutoff close and complete after it
    starts. The Yahoo file for Berkshire is `BRK.B_yahoo_daily.csv` (Schwab
    `BRK/B`, Yahoo `BRK-B`; sidecar symbol `BRK.B`).
  An unlabelled (R3) run refuses closes that are not a verified Yahoo run, a
  missing sidecar, or an unrecorded fetch time; these are allowed only for a
  labelled historical replay and are then recorded as
  `UNVERIFIED_HISTORICAL_REPLAY_ONLY`, sidecar `ABSENT` and `UNRECORDED`. A
  sidecar that is present but wrong, or any fetch before the cutoff close, is
  refused in every mode. The Yahoo driver's own `verify` additionally rebuilds the
  files from the raw bars; run it as well.
- Symbol aliases map the provider's spelling to the spelling of the
  `<SYMBOL>_yahoo_daily.csv` files. The Yahoo driver (PR 702) writes `BRK.B` for
  `BRK-B`, so all three Berkshire spellings map to `BRK.B`. A symbol that is
  neither a declared cash equivalent nor present (directly or through an alias)
  in the closes fails closed.
- Cash equivalents: sweep money-market funds such as SWVXX need no close. R2's
  treatment is kept: Cash-group quantity rows are not positions (they are counted
  as sweep vehicles). Each must be a declared cash-equivalent symbol and be
  priced at exactly the stated 1.00 NAV, otherwise the run is refused; a declared
  symbol held as a position is valued at quantity times 1.00 with no close and
  no Yahoo file.

It refuses: a run that does not verify; an unlabelled run that is not the
first-next-date mode (as-of exactly the cutoff plus one); a labelled run in any
mode other than the first-next-date window or the same-day fallback; a cutoff
that differs from the run evidence; closes that are not a verified Yahoo run, or
have an unrecorded fetch time, in an unlabelled run; a held position with no
official close (names the count and the symbols); and any output that already
exists. A refused run leaves no binding and needs a new output root.

## Design notes

- Evidence is single use and a failed run is not resumed: a run stops before
  writing its manifest, fails verification, and a retry needs a fresh approval
  and a new run id. This mirrors the Matrixport gate; a resumable partial run
  would let one approval authorise an open-ended number of reads.
- The holdings request start time, not only the response time, must lie inside
  the allowed window after the close.

## The cutoff-day NAV: derive, ingest, declare

The native `balances.historical` row for the cutoff day D can be a stale carry of the day before (R2.1: every
non-flat account, captured after the New York close). A carry cannot be the cutoff day's NAV, so the order
of a capture whose series ends at D is: capture and verify the run, fetch the official closes of every held
symbol for D and the day before, then (all committed tools, Python 3.11 venv, no scratch script):

1. `python tools/schwab_nav_derive.py derive --run-dir <verified run> --crosswalk <crosswalk> --closes-dir <closes>
   --day D --prior-day <D-1> --out <derivation.json> [--report <report.json>]` writes the derivation record
   (per account `official_<MMDD>` for D and the day before, `nav_file_<MMDD>`, `cash`; the value is the sum of
   quantity x official close rounded to cents half-even plus the cash rows, exactly what the ledger replays) and
   refuses on a missing close, a prior-day closure beyond `--tolerance` (default 0.05; R2.1 saw 0.02), a flat
   (cash-only) account whose native NAV is not exactly its cash, or a non-flat account whose native D disagrees
   with the official value without being a carry of D-1.
2. `python tools/schwab_capture_ingest.py ingest ... --derivation-record <derivation.json>` writes the derived D
   row into the NAV CSVs of the stale-carry accounts and records it (`derived_cutoff_row`, native and derived
   value) in the binding. Without the record a carried cutoff row is refused.
3. After composing the spec, `python tools/schwab_nav_derive.py declare ... --derivation <derivation.json> --spec
   <composed spec> --out <finality evidence>` writes the source-branch finality evidence (the spec's roots and
   NAV sources plus the pinned `derived_patch`); it re-derives the record from the run and refuses a file that
   differs. The provider ledger replays the patch exactly (no tolerance) and refuses a selected cutoff row equal
   to the row before unless the pinned record states the same official value (a flat account).

The labelled same-day mode (series ending D-1) has no cutoff row and needs none of this; a same-day NAV for D
there is not produced by any tool.

### The exact chain for a capture whose series ends at D (R3: D = 2026-11-06, prior day 2026-11-05)

Python 3.11 venv for every command; `<run>` is the verified driver run, `<xw>` the crosswalk file, `<closes>` the
Yahoo run's `official-closes` directory (files `<SYMBOL>_yahoo_daily.csv`, `/` written as `.`), `<ev>` an evidence
directory outside the world.

1. `python tools/schwab_capture_driver.py verify ...` (existing) accepts the run.
2. The Yahoo run supplies a close for D and the prior trading day for every held non-cash symbol
   (`yahoo_fetch_driver.py held-check` lists symbols no retained pull served, before evidence is minted).
3. `python tools/schwab_nav_derive.py derive --run-dir <run> --crosswalk <xw> --closes-dir <closes> --day 2026-11-06
   --prior-day 2026-11-05 --out <ev>/schwab-nav-derivation.json --report <ev>/schwab-nav-report.json`
   (add `--waive-prior-closure ACCOUNT=REASON` only for an account the run shows transactions on D for; see below).
4. `python tools/schwab_capture_ingest.py ingest <run> --crosswalk <xw> --cutoff 2026-11-06 --official-closes-dir
   <closes> --config <ingest config> --output-root <ingest root> --base-spec <base spec> --spec-out
   <ev>/schwab-composed-spec.json --derivation-record <ev>/schwab-nav-derivation.json`.
5. `python tools/schwab_generation.py stage <ev>/schwab-composed-spec.json <dataSchwab root>` (then `publish` with the
   accepted generation id), giving the generation id and `generation.json` sha256.
6. `python tools/schwab_nav_derive.py declare --run-dir <run> --crosswalk <xw> --closes-dir <closes> --day 2026-11-06
   --prior-day 2026-11-05 --derivation <ev>/schwab-nav-derivation.json --spec <ev>/schwab-composed-spec.json
   --out <ev>/schwab-finality.json` (the same `--waive-prior-closure` arguments as step 3: it re-derives and refuses a
   derivation file that differs).
7. The cycle manifest's `providers.schwab` names that generation and evidence; `declare` does not edit it:
   `generation` `{"mode": "explicit", "generation_id": <id>}`, `manifest_sha256` = the `generation.json` sha256,
   `finality_source` `CYCLE_MANIFEST_NATIVE`, `finalized_through` `2026-11-06`, `finality_evidence`
   `{"path": {"root_key": ..., "relative": ...}, "sha256": <sha256 of schwab-finality.json>}` with the file under a
   declared root. A wrong pin, path or id refuses at `release_cycle_manifest.py` resolution (`HASH_MISMATCH`,
   `PATH_ESCAPES_ROOT`, `ID_MISMATCH`, `FINALITY_EVIDENCE_REQUIRED`) and `release_ledger_spec.py` copies the pins and
   `finalized_through`; the provider ledger is the first refusal only for the content (a stale carry, a patch that does not
   replay exactly), not for this wiring.

Refusals and the fallback for each (evidence of a capture is single-use, so a refusal is read, never retried blindly):

| Refusal | Cause | Fallback, within 30 minutes |
|---|---|---|
| `no official close file for X` / `X has no official close for D` | a held symbol the Yahoo run did not serve | none after the fact (the Yahoo evidence is single-use): `held-check` before the fetch; else the owner decides |
| `the native history lacks D-1 or D` | the capture did not end at D | not recoverable by a tool: the labelled same-day mode or a new capture, owner decision |
| `the official value ... of D-1 differs from the captured NAV ... by more than 0.05` | a wrong close/holding/crosswalk, or an account that traded on D | read the message: it names the D transaction rows if any; review them and waive that account with a reason (recorded as `prior_closure`, `waiver_reason`, row count); with no D rows it is an input error, do not waive |
| `a flat account's native NAV ... must both equal its cash` | a cash flow on D, or a wrong cash row | as above (waiver needs D transactions) |
| `the native NAV ... differs from the official value ... and is not a carry` | native D is a fresh value that disagrees with the closes | input error: check the closes first; no waiver |
| `an unlabelled cutoff NAV repeats its prior day` at ingest, or `repeats its prior day without a derived patch` at the ledger | the derivation was not passed to the ingest, or the evidence omits the account | rerun steps 4-6 with the record; never edit a NAV CSV |
| `schwab derived patch replay differs` at the ledger | the pinned holdings, closes or record do not reproduce the derived value exactly | rebuild the record with `derive`; the ledger has no tolerance |

A waiver does not change the derived value: it is the end-of-D holdings at the D closes (correct whatever traded on D); it
only skips the cross-check that D-1 held what D holds, which an account active on D cannot pass.

`derive` values a held symbol from `<SYMBOL>_yahoo_daily.csv` of the same spelling (`/` written as `.`). It does not apply the
ingest configuration's `symbol_aliases` or `cash_equivalent_symbols`: the ledger's exact replay has no such rule, so a record
that used them could not be replayed. A held symbol that needs either refuses with `no official close file for <symbol>`; run
`yahoo_fetch_driver.py held-check --ingest-config <ingest config>` right after the capture, which lists aliased and
cash-equivalent holdings, before the Yahoo evidence is minted.

The ledger accepts a selected cutoff row that repeats the row before only for an account the pinned holdings capture shows
flat (no position rows) whose cash equals that row; the derivation record is never the authority for that.

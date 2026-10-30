# Schwab capture driver

`tools/schwab_capture_driver.py` captures the three Schwab families a release
cutoff needs under one governance gate: holdings at the cutoff close (with
market value), historical balances, and all-time transactions. Transaction
paging, the redaction rule and the WCH transaction shape are reused unchanged
from `tools/schwab_transaction_capture.py`; that file, `tools/schwab_generation.py`
and the `quant_engine.schwab` package are not modified (a test pins their bytes).
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
- Holdings whose per-row as-of date differs from the declared cutoff close
  (16:00 America/New_York on the cutoff date). Only an explicit
  `exception_label` in the approved evidence admits an offset, and then every
  affected row carries the label and `as_of_offset_days`.
- A capture before the close, or more than 8 hours after it, without that same
  label. The R2 pattern (an end-of-day book captured at 23:59 New York, stored
  under the next UTC date) is accepted and recorded: rows keep the cutoff
  `as_of_date`, and the output records `capture_date_utc`,
  `capture_date_new_york`, `captured_at_utc`, `capture_lag_hours` and
  `as_of_basis = CAPTURED_AFTER_NEW_YORK_CLOSE_OF_AS_OF_DATE`.
- A held position (nonzero quantity), including a sweep row, whose market value
  is null, missing, or not a finite decimal. The older Holdings-V2 normalizer in
  `quant_engine.schwab` records such rows as unavailable; this driver stops and
  names only the reason and a count. A row with no price date is refused rather
  than dated from capture time.
- Unknown groups, duplicated identities, accounts outside the crosswalk, a
  crosswalk that is not one-to-one, and a balance series with no row at the
  cutoff (unless labelled).
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
```

`verify` re-hashes every artifact, rejects unbound files, re-checks the as-of,
market-value and terminal-history invariants from the bytes (so a forged but
re-hashed file still fails), and, given the crosswalk, rejects any provider id
in non-raw output and re-derives the positions from the raw response.

## Governance evidence

Operator-supplied, never copied into a run. Fields (exact set): `schema_version`
1, `operation` `capture`, `cutoff` {`as_of_date`, `close_time_zone`
`America/New_York`}, `exception_label` (null or `UPPER_SNAKE_CASE`), `scope`
{`account_count`, `requests`: one entry each for `balances.historical`,
`holdings.v2`, `transactions.history` with `method`, `path`, `query`},
`review` {`identity`, `reference` = a quant_engine issue-comment URL},
`authority` {`status` `CURRENT`, `issued_at`, `expires_at`}, `terms` {`version`,
`sha256`}, `catalog` {`sha256`} (printed by the `catalog` command), `metering`
{`max_requests`, `retry_count` 0}. The review reference is shape-checked only,
as in the Matrixport gate.

## Credentials

None. Capture attaches over CDP to the owner's already-authenticated browser and
replays the page's own request headers in memory; nothing is read from a
credential store, so no `[credentials.schwab]` plan is needed. A future
Developer API route would go through the core credential resolver.

## UNVERIFIED against a live response

- The Holdings-V2 route path (owner supplied in evidence; read-only path words
  are screened) and the per-row `priceDate` field and its date format.
- The `balances.historical` query parameters and the `historicalBalances` /
  `date` / `accountValue` response shape.
- That `Schwab-Client-Ids` scoping, verified for the transaction POST, also
  applies to the two GET reads.
- Early-close days (13:00) are not modelled; the close is 16:00 New York.

# Alpaca capture driver

`quant_engine.alpaca.capture` is a governed, strictly read-only capture driver
for the Alpaca Trading API. It exists to read the account activities report
(fills, dividends, transfers, fees, interest, journals) that
`docs/alpaca-integration.md` records as never exported, so the Quantbase 36.49
cash residual can be closed from provider evidence, and to take an account and
positions snapshot beside it. It extends the existing `quant-engine-alpaca`
package (`AlpacaReadClient` remains untouched) and follows the Matrixport
pattern: approved-endpoint catalog, live-read governance evidence, request
manifest, raw plus normalized staging, independent verifier.

This change performs no live read. No network call, download, or credential is
used by the code or tests; every test uses synthetic fakes.

## What it does

```powershell
python -m quant_engine.alpaca.capture plan   --config <config.toml>
python -m quant_engine.alpaca.capture run    --config <config.toml> `
  --governance-evidence <evidence.json> --receipt-root <dir> `
  --staging-root <dir> [--run-id <id>]
python -m quant_engine.alpaca.capture verify --run-dir <staged-run>
```

`plan` and `verify` never touch the network or a credential. `plan` prints the
exact requests, the SHA-256 of the configured account (the binding the evidence
must carry), and the catalog and terms digests the evidence must carry.

`run` validates the configuration and credential plan, consumes the governance
evidence, only then resolves the two credential fields, and performs:

1. `GET /v2/account`, checked against the configured account (the response `id`
   or `account_number` must hash to the binding; a mismatch stops the run before
   any further read);
2. `GET /v2/positions` (optional, on by default);
3. `GET /v2/account/activities`, ascending, `page_size` rows per page, following
   the cursor (the `id` of the last row of the previous page) until a page is
   short or empty.

Output is printed as aggregate counts and hashes only, per
`docs/restricted-evidence-output.md`: never an account identifier, balance, or
row.

## What it refuses

- Any method other than `GET`, in the governed fetcher and again in the HTTPS
  transport. The low-level transport has no method parameter.
- Any path outside the three-entry allow-list in
  `quant_engine/alpaca/capture/catalog.py`, and any path segment naming orders,
  transfers, journals, ACH relationships, wires, withdrawals, deposits,
  exercise, or close-all. Excluded trading, order, and transfer routes are
  listed in the catalog so its digest covers them.
- Query parameters outside the endpoint's declared set.
- A run without governance evidence, with expired, not-yet-current, replayed, or
  mismatched evidence, or past the evidence's request budget (reserved durably
  before each request).
- Redirects, hosts other than the two approved API hosts, responses over the
  byte limit, and any retry: a rate-limit (429) or error status stops the run.
  The raw response and a manifest saying why (`terminal.complete` false, with
  `retry_after_seconds` for 429) are still written; a later run needs fresh
  evidence.
- Overwriting staging: every file is created exclusively under a caller-supplied
  absolute staging root into a run directory that must not exist.
- Unknown shapes: an unknown `activity_type`, an unknown field on an activity, a
  missing required field, a malformed number or timestamp, a duplicate activity
  id, duplicate JSON keys, or a non-array page raises and leaves the raw bytes
  staged for a later parser fix.

## Configuration

The account identifier and the environment are configuration, not code. There is
no default environment: choosing `paper` (`paper-api.alpaca.markets`) or `live`
(`api.alpaca.markets`) is an explicit governed decision that the evidence
repeats.

```toml
[alpaca_capture]
account_id = "<owner-supplied account identifier>"
environment = "live"            # or "paper"; required, no default
after = "2024-07-20"            # optional RFC3339 timestamp or date
until = "2025-05-06"            # optional
activity_types = []             # optional filter, known types only
page_size = 100                 # 1..100
max_pages = 200
include_positions = true

[credentials.sources.azure-keyvault]
command = ["<owner-supplied read-only helper>"]

[credentials.alpaca]
key_id = [{ source = "azure-keyvault", name = "<owner-supplied name>" }]
key_secret = [{ source = "azure-keyvault", name = "<owner-supplied name>" }]
```

Credentials resolve through the core resolver (`CredentialResolver`,
`plan_from_config`, `sources_from_config`). The plan must declare exactly
`key_id` and `key_secret`. Secret names are owner-supplied and live only in
configuration; no value is accepted from arguments, environment, or files.

## Governance evidence

A single-use JSON file supplied outside the repository, with exactly these
fields: `schema_version` (1), `operation` (`"capture"`), `environment`,
`account_binding.sha256` (from `plan`), `scope.endpoints` (exactly the planned
`method`/`path` pairs), `review` (`identity` and a quant_engine issue-comment
URL `reference`), `authority` (`status` `"CURRENT"`, `issued_at`, `expires_at`),
`terms` (`version`, `sha256` from `plan`), `catalog.sha256` (from `plan`), and
`metering` (`max_requests`, `retry_count` exactly 0). A consumption receipt
named by the evidence hash is created exclusively under `--receipt-root` and
updated before every request.

Differences from Matrixport governance, stated plainly: the receipt root is a
caller argument rather than a fixed per-user directory, and on Windows the
receipt directory entry is made durable by file `fsync` and atomic `os.replace`
only. Evidence that fails after consumption (for example credential resolution)
stays consumed.

## Staged run layout

```
<staging-root>/<run-id>/
  raw/NNNN-<report>.bin        exact response bytes, one per request
  request_manifest.jsonl       one canonical record per request (no secrets)
  normalized/account.json      balances; only the account's SHA-256 is kept
  normalized/positions.json
  normalized/activities.jsonl  one row per activity, deterministic order
  normalized/summary.json      counts and cash totals by category
  manifest.json                hash-bound index and terminal state
  MANIFEST.sha256              digest of manifest.json (written last)
```

The run directory is restricted private evidence. It belongs in the source-run
staging area, not in the repository.

## Activity normalization

Fills carry a derived cash amount (`-qty*price` for buys, `+qty*price` for sells,
marked `derived_qty_x_price`); other activities carry the reported `net_amount`.
Categories: `fill`, `dividend`, `transfer`, `transfer_security`, `fee`,
`interest`, `corporate_action`, `option_event`, `other`. The type table is in
`normalize.py`; a type absent from it fails closed.

## Verifier

`verify` trusts nothing the runner said. It re-hashes every staged file, rejects
unlisted files, re-checks each recorded request against the catalog, replays the
pagination chain from the raw bytes, requires a complete terminal state, and
re-derives every normalized output from raw bytes, comparing byte for byte.
Findings are stable codes only.

## Fixture limits and UNVERIFIED items

All fixtures are synthetic and shaped from the Alpaca documentation. Nothing
below was observed against a real account or response:

- exact response field sets (activities, account, positions) and the activity
  type list; strict unknown-field rejection on activities may need loosening
  once a real response is seen;
- whether the cursor is `page_token` equal to the last row `id` for every
  activity type, and the behavior of `after`/`until` together with the cursor;
- the 429 response shape, any rate-limit headers, and the documented limit
  (assumed 200 requests/minute, not in the repo);
- whether the Quantbase-linked account is readable with an owner-held key, and
  whether it is served by `/v2/account/activities` (the Trading API for the
  keyed account) or only by the Broker API route
  `/v1/accounts/{account_id}/activities`, which is not in the catalog;
- whether the configured identifier is the account `id` or `account_number`
  (either binds);
- the side vocabulary for fills (`buy`, `sell`, `sell_short` accepted);
- journal (`JNL`) cash semantics: treated as cash movement when `net_amount` is
  present.

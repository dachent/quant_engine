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

## Ingest adapter and the native activities package

The driver stages activities but nothing read them. `quant_engine.alpaca.capture
ingest` is that consumer, and the Alpaca generation reads what it writes.

### Family decision

Native activities ride in the existing **Alpaca provider family**
(`quant-engine-alpaca`, `alpaca-read-generation`), as an optional
`native_activities` block of the generation plan. They do not get a new family,
and they do not modify the retained generation:

- the ledger vocabulary the cash closure needs already lives here
  (`Transaction`, `ObservedAmount`, USD, `UNMAPPED`), as do the create-only plan,
  stage, publish, verify, and single-authority cutover steps, the
  `dataAlpaca` pointer, and the reconciliation shape;
- a new family would need its own pointer, adapter identity, catalog schema, and
  acceptance baseline entry, none of which the closure needs;
- the legacy plan, and so the retained generation, is untouched: the block is
  added to a plan only when supplied, and a plan without it builds
  byte-for-byte as before (pinned by tests, and checked against the real
  retained generation 7942ee08 when the OneDrive tree is present).

A native plan is a **new** plan with its own plan id, run id, and generation id.
It rebuilds the legacy baseline from the same hash-bound source and adds the
native transactions beside the legacy order fills, with the native account named
in `accounts`. It supersedes the retained generation only if someone publishes
it; this change publishes nothing.

### What it does

```powershell
python -m quant_engine.alpaca.capture ingest --run-dir <verified-run> `
  --crosswalk <alpaca-crosswalk.json> --internal-account-id account-<16 hex> `
  --after 2024-07-20 --until 2025-05-06 --output-root <dir> [--package-id <id>]
python tools/alpaca_generation.py plan <legacy-root> <dataAlpaca-root> <tracker.csv> `
  --internal-account-id account-<16 hex> --as-of-date <date> `
  --native-activities <output-root>/<package-id>
```

The crosswalk file is `{"schema_version": 1, "provider": "ALPACA", "rows":
[{"internal_account_id": "account-...", "account_binding_sha256": "<hex>"}]}`,
one-to-one, holding only the SHA-256 the driver's `plan` prints, never the
provider identifier.

The package (`native-activities.jsonl`, `native-account.json`, optional
`native-positions.json`, `native-activities-spec.json`, then
`ingest-binding.json` last) holds every activity in the generation's native
row form: category, USD cash amount (reported `net_amount`, or the derived
`-qty*price` / `+qty*price` leg of a fill, labelled by basis), event date,
symbol, quantity, price, with provider activity and order ids kept only as
SHA-256 digests and free-text descriptions dropped. The spec carries exact
counts and cash totals by category and by activity type (with positive and
negative counts per type), so the Quantbase cash closure can be recomputed:
sum `cash_amount` by window, then compare to the account snapshot's `cash`.

The generation turns each row into a ledger transaction of kind
`ACTIVITY_<TYPE>` (amount `UNAVAILABLE` for security-only rows such as `JNLS`),
adds a `native_activities` catalog block (summary, account snapshot, positions,
source run digests, window) and hash-binds every package file as a source
artifact.

### What it refuses

Every check runs before the first byte is written; a refused run leaves no
package. The reader re-runs the same checks on every load.

- A run that does not pass `verify_run` (hash, allow-list, pagination replay,
  raw re-derivation), or whose terminal state is incomplete.
- A run window that is not bounded on both sides, or that differs from the
  window the caller declares (`--after`, `--until`).
- Any activity outside the window (dates inclusive; fills compared as instants),
  a repeated activity id (and a repeated digest), an unknown type, category,
  field, or number shape.
- A non-USD account snapshot or activity row.
- A pagination chain with a first-page cursor, a page after a short page, a
  missing or repeated cursor, or a full last page (replayed independently of the
  verifier from the request manifest).
- A run whose account binding differs from the crosswalk row for the internal
  account (checked against both the manifest and the account snapshot), a
  missing, repeated, or non-one-to-one crosswalk row.
- A deposit-type (`CSD`) row that removes cash or a withdrawal-type (`CSW`) row
  that adds it. Signs are otherwise recorded as observed and never reinterpreted.
- Overwriting an existing package directory; relative or missing output roots.
- In the generation: a package whose binding differs from the digest pinned in
  the plan, an altered or extra file, rows out of canonical order, a summary
  that differs from the rows, a record id collision, a package account that
  differs from the plan.

### Ingest limits and UNVERIFIED items

- Shapes are synthetic. The retained legacy export fetched zero activities
  (`alpaca_activities_20260717_145339/REPORT.md`: 0 fetched), so no real activity
  row exists locally; the only real-world evidence is the legacy exporter's list
  of requested types (`FILL`, `TRANS`, `DIV`, `INT`, `CIL`, `CS`, `CSD`, `FEE`),
  of which `TRANS`, `CIL`, and `CS` are not in the driver's table and would be
  refused by it.
- The sign convention of `net_amount` for transfers, journals, and fees is
  unverified; the closure must read the summary's per-type sign counts before
  trusting any total.
- A window bound with a time of day is compared as an instant for fills and as a
  calendar date for non-trade rows (the provider gives those a date only).
- Fills carry no fee fields in the driver's schema; regulatory and other fees are
  expected as separate `FEE` rows. Any fee embedded in a fill is not captured.
- The generation verifier (`verify_published`) rebuilds from the plan, so the
  package directory named in the plan must remain in place.

### Crosswalk authoring (generated, never hand-written)

```powershell
python -m quant_engine.alpaca.capture crosswalk --plan-digest <account_binding_sha256 from `plan`> `
  --internal-account-id account-<16 hex> --out <absolute path>
```

Writes the exact one-row, one-to-one crosswalk the ingest reads, create-only,
and prints its SHA-256. A digest that is not a lowercase SHA-256, an invalid
account id, a relative path, or an existing file is refused. Success test:
`test_generated_crosswalk_is_accepted_by_the_ingest` (the ingest accepts it) and
`test_a_wrong_plan_digest_is_refused_by_the_ingest`.

### Sign-convention check (run before any total is used)

```powershell
python -m quant_engine.alpaca.capture signcheck --package-dir <package> `
  --opening-cash <cash at window start> (--closing-cash <cash at window end> | --closing-from-snapshot) `
  [--tolerance 0] [--out <absolute report path>]
```

Exit 0 only when the totals are blessed. It compares the package against an
independent balance movement, `delta = closing - opening`, where opening cash
must come from a source other than the activities (a statement or the prior
capture) and `--closing-from-snapshot` uses the account snapshot `cash` and is
valid only if no cash movement occurred between the window end and the capture.
With `S` the package total and `s_T` a type's total:

- as reported, `R0 = delta - S` (zero when the reported signs hold);
- per type, `R1 = delta - (S - 2 s_T)`: FAIL when `|R1| < |R0|` (flipping that
  type fits the balance better), PASS otherwise, INCONCLUSIVE when `|s_T|` is
  within tolerance of zero, DERIVED for FILL (cash leg is qty times price with the
  side as sign, never flipped);
- a global inversion of every non-fill type is tested too.

The totals are blessed only if no type fails, no global inversion fits better,
and `|R0| <= tolerance`. Output is counts, sums, and residuals only (no ids), and
`--out` writes the same report create-only for the Quantbase 36.49 closure to
read. A residual that is large relative to a type's total can implicate that type
(for example a 36.49 residual against a small negative-sum type); read the
per-type residuals before concluding which it is. Tested with both conventions
(fee, journal, and global inversion reported with the opposite sign fail; the
documented convention passes): `tests/alpaca/test_capture_signcheck.py`.

# Quantbase HAR intake driver

`tools/capture_quantbase_har.py` is the governed, create-only intake for an
owner-supplied Quantbase browser HAR export. It contains no network, login,
credential, or subprocess code: the owner exports the HAR from an interactive
browser session, and the driver stages, hash-binds, parses, and verifies the
files offline. It extends the HAR-only contract in
[Quantbase HAR-only integration](quantbase.md) and the reviewed route table in
`quant_engine.web_contracts.quantbase`; it does not add a route.

Everything that used to be a constant is read from one intake manifest: the
expected capture file names, the per-capability response and record counts, the
date window, the cutoff, and the NAV publication grid.

## Intake manifest

```json
{
  "schema_version": 1,
  "provider_id": "quantbase",
  "provenance": {
    "kind": "OWNER_BROWSER_HAR_EXPORT",
    "approval_reference": "https://github.com/dachent/quant_engine/issues/<n>#issuecomment-<n>"
  },
  "capture_files": [{"name": "<export>.har", "sha256": "<optional pin>"}],
  "session": {"captured_at": "<ISO with offset>", "expires_at": "<ISO with offset>"},
  "window": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
  "window_fields": ["date", "time"],
  "cutoff": "YYYY-MM-DD",
  "nav_grid": {"anchor": "YYYY-MM-DD", "step_days": 5, "capabilities": ["funds.statistics"]},
  "capabilities": {
    "<reviewed capability>": {
      "expected_responses": 1,
      "expected_records": 0,
      "records_key": "<dotted path to the record array, or null>"
    }
  },
  "ignored_origins": []
}
```

The approval reference is the owner's recorded approval of the export; the
driver validates its shape and binds it by hash but cannot verify the comment.
The manifest is owner-authored, never invented by an agent.

## Commands

```powershell
uv run --frozen --no-sync python tools/capture_quantbase_har.py stage `
  --manifest <manifest.json> --capture-dir <owner-har-dir> --staging-root <private-root>
uv run --frozen --no-sync python tools/capture_quantbase_har.py parse --intake <intake-dir>
uv run --frozen --no-sync python tools/capture_quantbase_har.py verify --intake <intake-dir>
```

`stage` requires the capture directory to hold exactly the declared file names,
checks any pinned hashes, refuses a staging root inside the repository or the
capture directory, and copies the files byte for byte into
`<staging-root>/quantbase-har-intake/<intake-id>/raw`. The intake id is derived
from the manifest and file hashes. An existing intake is never overwritten.
`parse` writes `normalized/records.json` (the sanitized extraction),
`parse-report.json` (counts, hashes, dates, flags: no values), and
`native-artifacts.json`. `verify` re-derives every output from the raw bytes and
compares, rejects undeclared paths, and rejects any output that contains a
captured session header value.

## Refusals

The driver fails closed on a manifest key it does not know, a non-owner
provenance, an unreviewed capability, a HAR entry the reviewed web contract
does not allowlist (path, method, query shape, status, missing session header),
an origin outside the two Quantbase origins and `ignored_origins`, a capability
that is present but not in the manifest (or the reverse), a response or record
count that differs from the manifest, a `records_key` the response lacks, a
dated value outside the window, a NAV capability with no parseable date, and a
cutoff later than the newest captured NAV date.

## Flags (not failures)

- `NEWEST_NAV_OFF_GRID`: the newest NAV date does not land on the
  `anchor + k * step_days` grid. The report names the latest grid date at or
  before it.
- `CUTOFF_OFF_NAV_GRID`: the cutoff is not a grid date.
- `UNPARSED_DATE_VALUES`: a window field held a value that is not a date.
- `RAW_HAR_HOLDS_SESSION_MATERIAL`: the staged raw HAR carries session header
  values. Quarantine or invalidate them after use per
  [the quarantine runbook](../quantbase-security-quarantine.md).

## Generation hand-off

`native-artifacts.json` declares the retained HAR files and the intake manifest
together with their schema contract. Pass it to the existing generation tool:

```powershell
uv run --frozen --no-sync python tools/quantbase_generation.py plan <csv-root> <dataQuantbase-root> `
  --as-of-date <cutoff> --quarantine-plan-id <id> `
  --expected-file-count <n> --expected-record-count <n> `
  --native-intake <intake-dir>
```

Without `--native-intake` the generation tool behaves exactly as before, and the
legacy native contract (the 2026-09-02 file names) is unchanged. The CSV
conversion is a separate tool, see "HAR-to-CSV converter" below.

## Limits

Real response shapes are not observable from this repository. Record counting
and `records_key` are declared by the owner and verified only against synthetic
fixtures; they are UNVERIFIED against a real export until the first owner
capture is parsed.

`--native-intake` takes the intake directory, not a declaration file. The
generation tool re-hashes every raw file against the intake record, requires the
staged manifest and the parse report to agree with it, builds the schema contract
itself, and requires `--as-of-date` to equal the manifest cutoff. It does not
re-run `verify`; run `verify` first. `native-artifacts.json` is informational.
Interrupted `parse` runs resume over byte-identical partial output (the report is
written last).

## Declared native manifest and the NAV grid

`quantbase_generation.py plan --native-manifest <file>` (exclusive with
`--native-intake`) declares a native capture of any date or file name, with no
code edit: `{"schema_version": 1, "cutoff": "YYYY-MM-DD" (required), "files":
[{"role", "path" (absolute), "sha256", "bytes" (optional)}]}`. Roles are
`native-har`, `account-activity-response`, `account-activity-request-proof` and
`har-intake-manifest`; the published path and the schema contract are derived from the role (HAR files
keep their file name under `har/`; the other roles publish under their canonical
`capture/` names so flow coverage still resolves them). Every file is re-hashed; unknown keys or
roles, hash or size mismatch, unsafe or duplicate names, relative or linked paths
and a missing `native-har` fail closed. `--as-of-date` must equal the cutoff. The legacy 2026-09-02 pairs remain the default when no flag is given.

`capture_quantbase_har.last_publishable_grid_date(captured, cutoff, anchor=...,
step_days=5, label=None)` returns the newest captured on-grid date at or before
the cutoff plus the grid-predicted date. Off-grid captured rows (provisional
latest points) are never publishable. It refuses a cutoff before the first grid
date, interior grid dates missing from the capture, and an off-grid cutoff or a
cutoff later than the newest captured grid date unless a non-empty `label` says so.

## HAR-to-CSV converter

`tools/quantbase_har_to_csv.py` turns a verified intake into the exact source
root `quantbase_generation.py plan` takes, plus the `--native-manifest`
declaration, so the chain from owner HAR to plan needs no hand step. It opens no
network connection and reads no credential.

```powershell
uv run --frozen --no-sync python tools/quantbase_har_to_csv.py convert `
  --intake <intake-dir> --out-root <private-root> --volatis-pricehistory public
uv run --frozen --no-sync python tools/quantbase_har_to_csv.py verify `
  --intake <intake-dir> --out-root <private-root> --volatis-pricehistory public
uv run --frozen --no-sync python tools/quantbase_generation.py plan `
  <out>/quantbase-har-csv/<intake-id>/source <dataQuantbase-root> `
  --as-of-date <cutoff> --quarantine-plan-id <id> `
  --expected-file-count <csv_file_count> --expected-record-count <csv_record_count> `
  --native-manifest <out>/quantbase-har-csv/<intake-id>/native-manifest.json
```

`csv_file_count` and `csv_record_count` are in the command's JSON result and in
`conversion-report.json`. The output directory is create-only and holds
`source/csv/*.csv`, `source/RUNBOOK_QUANTBASE.md`, `conversion-report.json` (counts,
hashes and flags, no values) and, written last, `native-manifest.json`
(`native-har` per raw HAR and `har-intake-manifest`, absolute paths into the intake,
cutoff from the intake manifest). `--out-root` must be disjoint from the repository
and the intake.

Binding to the verified parse. `convert` first runs this driver's `verify`. It then
re-reads the staged raw HAR one entry at a time through the same reviewed web
contract and requires the sanitized form of what it read to equal
`normalized/records.json` byte for byte. Anything the contract disposes of
(preflight, analytics, static assets, ignored origins) is skipped; anything else
the contract refuses stops the conversion.

Why the CSV values are read from the raw bodies. The contract sanitizer redacts
every `id`, `user` and `*_id` field and every scalar that looks like a token. In the
retained 2026-09-02 capture that is every `fundPriceData` value (all 11,040 are
24 or more characters), every activity `id` (so the order-to-fill join key is
gone), the account fund value and the dashboard fund id. `records.json` therefore
cannot carry the series the CSVs exist for. The raw bodies stay inside the verified
intake; headers, cookies and the session JWT are never read into an output, and
`convert` and `verify` refuse any output that contains a captured session header
value (whole value or any `;`-separated piece). The raw HAR itself still holds the
session headers (the driver flags `RAW_HAR_HOLDS_SESSION_MATERIAL`), and the
`native-har` manifest entry binds it as received; redacting it is the owner decision
in the proposal above.

Role of each CSV (header order is the generation tool's, pinned by a test):

| Capability | CSV |
| --- | --- |
| `users.profile` | `user_profile.csv` (sorted key/value, `ob_status.*` flattened) |
| `accounts.linked` | `accounts.csv` |
| `accounts.activity` | `account_activity.csv` (full 43-column header; `activity_type` from the entry type) |
| `portfolio.dashboard` | `dashboard_<fund>_pricehistory.csv`, `dashboard_<fund>_holdings.csv` (only when non-empty), `dashboard_total_wins.csv` |
| `partners.status` | `partner_status.csv` (optional) |
| `funds.statistics` | `volatis_strategy_stats.csv` (only the Volatis fund) |
| `funds.rebalance-history` | `volatis_strategy_rebalance_history.csv`; any other title becomes `fund_rebalance_<title>.csv` |
| `funds.invest` | `ssg_<fund>_pricehistory.csv` per fund (the 5-day public series), `ssg_all_funds_stats.csv` and `projections_all_live_funds.csv` (one fund-metadata table under both names, as the retained converter wrote it) |

`--volatis-pricehistory` is required and has no default. `public` writes the
provider's public fund series for Volatis. `account-level` replaces only that file
with the dashboard's daily account series, which is what the retained 2026-09-02
files contain (the public series is then not written). The mode is the owner's
ruling on which series is authoritative; `verify` must be given the same value.

`dashboard_total_wins.csv` keeps the retained rule: the first fund in response order
with a non-null `wins.total` supplies the `total_*` rows (Volatis in the retained
capture, not the `Total` fund, whose values differ); the source fund is named in
`conversion-report.json`.

Refusals (fail closed): an intake that does not verify; records that differ from
`normalized/records.json`; a capability with no CSV role (`funds.catalog`,
`funds.index`, `funds.projections`) or a missing required one; a key the pinned
shape lacks or adds (names only, never values); repeated identical requests with
differing bodies; more than one distinct request for a singleton capability;
`funds.statistics` for any fund but Volatis; a fund title with no safe file name or
two titles with one file name; a fund with two ids or a repeated fund/time row;
missing required datasets; an existing output directory.

Limits. Key sets are pinned from the retained 2026-09-02 capture, so a provider
field addition stops the conversion until the shape is reviewed. The account
`funds` list was empty in that capture, so its entries are written as JSON without
shape checks. The 2026-09-02 reproduction (46 CSVs) is recorded in the READY note;
shapes after 2026-09-02 and a capture that includes `funds.projections` remain
UNVERIFIED. No flow-coverage artifact is produced: a native HAR declared under a new
name is not one of the files the ledger's account-activity check recognises.

## Passive CDP HAR recorder (`tools/quantbase_cdp_har_recorder.py`)

The owner rejected manual HAR export. This tool replaces it: the owner signs in to a
dedicated Edge or Chrome profile with a loopback debug port and browses by hand;
the tool only LISTENS to the browser's Network events and writes a HAR 1.2 file the
intake driver above accepts unchanged. It never navigates, clicks, types, submits or
injects script. Every message it can send is on a closed allow-list
(`Target.setDiscoverTargets`, `Target.attachToTarget`, `Network.enable`,
`Network.getResponseBody`); anything else is refused before it leaves the process,
`*ExtraInfo` events (cookies) are never opened, and the CDP URL must be loopback.

Why it reads headers, unlike the Portfolio123 recorder. The reviewed contract
requires the provider `jwt` request header on API evidence, and the driver consumes
a HAR. So for RECORDED requests only the tool copies the request headers (the
`jwt` header included) and the response headers into the HAR file. The owner ruled
that HAR session-header redaction does not matter and that the raw HAR may live in
the governed root. `Cookie`, `Set-Cookie`, `Authorization` and `Proxy-Authorization`
are dropped anyway. No header value and no body is ever printed; the `jwt` value
appears in exactly two files: the HAR and the per-run journal `journal.jsonl`
(below), both only inside the governed root. It never appears in `manifest.json`,
`discovery.json`, stdout or stderr. The HAR and the journal are restricted evidence.

What is recorded: same-origin GET responses on `https://app.getquantbase.com` and
`https://qbapi3.com` (an allow-list kept as data) that the reviewed contract
`QUANTBASE_CONTRACT` uniquely allow-lists, with a 2xx status, a JSON body, and (API
routes) a `jwt` header of at least 16 characters. Everything else is not recorded
and is listed by path template and counts only:

| List in `manifest.json` | Contents |
| --- | --- |
| `MUTATION_LIKE_NOT_RECORDED` | every non-GET (the `track-event` POST, `OPTIONS`) and any GET whose path contains `track-event`, `revert`, `delete`, `remove`, `save`, `submit`, `update`, `create`, `trade`, `order`, `subscri`, `cancel`, `logout`, `signout`, `sign-out`, `password`, `token`, `withdraw`, `transfer`, `deposit`, `link` (the body is never requested) |
| `UNREVIEWED_ROUTE_NOT_RECORDED` | a GET on an allowed origin that the contract does not allow-list (for example `/_next/data/{build_id}/funds/{id}.json` or a paged activity URL), with its origin and query KEY names. Recording one would make the driver's `parse` fail, so it is left out and named so the owner can ask for a contract review |
| `OTHER_NOT_RECORDED` | `NON_2XX`, `NOT_JSON`, `MISSING_JWT`, `UNREADABLE_BODY`, `OVERSIZE_BODY` per template and status |

Modes:

- `discover`: value-free metadata only (capability, path template, query key names,
  status, media type, body shape = field names and types, `jwt_header_present`
  boolean, body hash). No header and no body is kept. Its journal holds the same
  value-free digests, no headers.
- `capture`: `<output-root>/har/<name>.har` (default `quantbase-session.har`),
  `<output-root>/journal.jsonl` and `<output-root>/manifest.json`, written last, with the HAR hash, per-entry body and
  request hashes, per-capability response and distinct-request counts, and the
  lists above. `<output-root>/har` holds only the HAR, so it is exactly the driver's
  `--capture-dir`, and the file name goes into the intake manifest's `capture_files`.
- `recover`: assembles the HAR and manifest from `journal.jsonl` after a killed
  run (see below).
- `propose-contract`: a DRAFT contract extension from the unreviewed routes of
  finished runs (see below).
- `verify`: re-hashes the run, rejects strays, and re-checks the HAR (version 1.2,
  entry count equals the manifest, body hashes, request hashes and per-capability
  distinct-request counts recomputed from the HAR, the HAR equal to the journal,
  only allow-listed origins, only GET,
  every URL routes through the contract, no mutation-like path, 2xx, JSON body, `jwt`
  present on API entries, no cookie-class header). Exit 0, or `REFUSED <CODE>`, exit 2.

The output root must be absolute, new, outside the repository, outside any
`scratch`, `tmp` or `temp` folder and outside the system temp directory; the
governed OneDrive evidence root is the intended place. Files are create-only.

START THE RECORDER FIRST, then load or reload the pages: it only sees new traffic.
`--warn-after-seconds N` (default 30, 0 disables) prints `WARNING NO_ENTRIES_RECORDED`
once if nothing was recorded after N seconds; an empty run always records the
warning in the manifest.

FIRST OUTPUT LINE. `discover`, `capture` and `recover` print
`jwt_header_present: true` or `jwt_header_present: false` as their first line. If it
is `false` (an API response arrived without a `jwt` request header, or no API
response was recorded), the next line says STOP: do not continue, tell the
coordinator before loading more pages. A pre-sitting smoke on a public page can only
exercise the false path (no API traffic, so no `jwt`); the true path needs a
signed-in session.

```powershell
python tools/quantbase_cdp_har_recorder.py discover --cdp-url http://127.0.0.1:9333 --output-root "<governed root>\discover-1"
python tools/quantbase_cdp_har_recorder.py capture  --cdp-url http://127.0.0.1:9333 --output-root "<governed root>\capture-1"
python tools/quantbase_cdp_har_recorder.py verify   --run-root "<governed root>\capture-1"
python tools/capture_quantbase_har.py stage --manifest <intake-manifest.json> --capture-dir "<governed root>\capture-1\har" --staging-root <private-root>
```

The intake manifest's per-capability `expected_responses` must equal the recorder's
`capabilities.<name>.responses` (repeated loads of one page count again; load each
page once, or declare the repeats). Ctrl-C ends a run normally and assembles the
HAR.

Journal and recovery. Every recorder decision is appended to `journal.jsonl` in the
output root (flushed and synced per line) BEFORE it changes state; a recorded entry
line holds the full HAR entry, so it carries the `jwt` header exactly like the HAR.
Nothing is held only in memory. If the process is killed, run
`recover --run-root <output-root>`: it refuses a run that has a manifest, a run
whose HAR already exists (create-only) and a corrupt journal, drops a half-written
last line (warning `JOURNAL_LAST_LINE_TRUNCATED`), and writes the HAR and the manifest
with `recovered: true` and the warning `RECOVERED_FROM_JOURNAL`. `verify` then
checks the recovered run like any other. A finished run keeps its journal, hash-bound
in the manifest, and `verify` requires the HAR to equal it.

`propose-contract --run-root <run> [--run-root <run> ...] --out <new dir>` verifies the
runs, then lists the observed unreviewed same-origin GET route templates and query key
NAMES (never a value) and writes `contract-extension.draft.json`/`.md`: a proposed
`extension` document (`extra_query_keys` for reviewed routes that were seen with more
keys, and `routes` for new routes with a suggested capability name, path pattern and
`jwt`/none authentication) plus the `observed` table. It is a DRAFT; nothing applies
it.

Proof is offline: `tests/quantbase/test_cdp_har_recorder.py` replays synthetic CDP
events shaped like the reviewed routes through a fake transport, then runs the real
driver `stage`/`parse`/`verify` and the HAR-to-CSV converter `convert`/`verify` on
the recorder's HAR. UNVERIFIED without a live session: that Edge reports the
page-set `jwt` header in `Network.requestWillBeSent` (the tool warns
`API_RESPONSES_WITHOUT_JWT` and records nothing for such responses if it does not),
that response bodies stay readable after `loadingFinished` for large fund series,
and which extra routes the real pages call beyond the reviewed table.

## Declared contract extension (`build_quantbase_contract`)

`quant_engine.web_contracts.build_quantbase_contract(extension)` widens the reviewed
Quantbase contract with DECLARED data, so the routes and paging keys found at the
sitting (see `propose-contract` above) become a reviewed document, not a code edit.
With no extension it returns `QUANTBASE_CONTRACT` itself, and a golden table in
`tests/web_contracts/test_quantbase_extension.py` pins how every reviewed route and
near-miss URL routes. The reviewed routes keep exact query-key matching
(`EndpointContract.optional_query_keys` is empty for all of them).

```json
{
  "schema_version": 1,
  "routes": [
    {"capability": "funds.detail", "origin": "https://app.getquantbase.com",
     "path": "/_next/data/{build_id}/funds/{id}.json",
     "path_pattern": "/_next/data/[^/]+/funds/[0-9]+\.json"},
    {"capability": "funds.search", "origin": "https://qbapi3.com",
     "path": "/api/funds/search/", "query_keys": ["q"], "authentication_header": "jwt"}
  ],
  "extra_query_keys": {"accounts.activity": ["page"]}
}
```

`routes` add GET routes (required `query_keys` must all appear, `optional_query_keys` may); `extra_query_keys`
makes keys optional on one capability. It fails closed with `WebContractError`: unknown
keys at any level, a non-portable or repeated key, a capability or route identity that
repeats a reviewed one, an origin other than the two Quantbase origins, a path outside
`/api/` (API origin) or `/_next/data/` (page data), a templated path without a
`path_pattern`, a pattern that is not exactly the template's own (placeholders `{id}`, `{user_id}`
become `[0-9]+`, `{build_id}` becomes `[^/]+`, everything else is escaped literally) or an exact
path with one, any two endpoints that one request can match (sample-based check over every
endpoint and over each filled from another's literal segments), an API
route without `authentication_header: "jwt"` or a page-data route with one, any path,
capability or key containing a mutation word (`track-event`, `delete`, `withdraw`,
`transfer`, `link`, ...), and extra keys for an unknown capability, for one with a
reviewed query validator (`users.profile`, `funds.statistics`,
`funds.rebalance-history`), empty, or repeating a required key.
`extract_quantbase_har(document, session=..., contract=...)` takes the extended
contract; the default is unchanged. No real route is declared in this change, and the
intake driver does not load an extension yet: wiring `--contract-extension` into
`capture_quantbase_har.py` is the step after the sitting's draft is reviewed.

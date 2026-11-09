# Portfolio123 owner capture runbook

This runbook covers the Portfolio123 captures the reviewed web contract does not
cover today (AI Factor predictor, AI Factor model, screen, simulation) plus
account transactions, designer-model JSON, and billing evidence. It complements
`docs/providers/portfolio123.md` (routing and generation) and
`docs/p123-security-quarantine.md` (HAR handling). Nothing here logs in, reaches
the network, or calls the API. The owner exports; the code stages, normalizes, and
verifies offline.

## Status: two capabilities CONFIRMED from preserved evidence, the rest UNVERIFIED

The catalog was reviewed offline against preserved real bytes (2026-09 captures
under the private portfolio data root: 92 HARs, the raw JSON and HTML pages, the
CDP request records, the designer-model capture, the downloaded CSVs, and the
2026-10-16 design intake). Only route templates, methods, status codes, query keys,
and response field names and types were taken; no value or identifier was copied.

| Capability | Placeholder vs evidence | Catalog now | What the evidence showed |
|---|---|---|---|
| `designer-model.json` | DIFFERS | CONFIRMED | Real route is `GET /spr/strategy/model/{id}?dm=true` (not `/app/investment/model`); real response has 37 top-level fields, MM/DD/YYYY dates, and personal data (designer profile, comment threads) that is now redacted. Verified byte-for-byte against the preserved capture in HAR and file form. |
| `transactions.download` | DIFFERS | CONFIRMED | Real header is `Date,Symbol,Type,Shares,Price,Amount,TotFees,Rank,Notes`, identical in ten captures (route is the reviewed `strategy.transactions`). |
| `transactions.account` | DIFFERS | UNVERIFIED (route, columns, pager read from preserved bytes; last page pending) | No JSON account-transactions route in 92 HARs. The real list is `GET /transact_list.jsp?portid=&offset=`, server-rendered HTML (a pager row, a `rowTwo` header row, 50 data rows of 11 cells), strategy-scoped. Eight preserved pages of one strategy (offsets 1400 to 1750) parse and agree with their pager. Admitted by the fail-closed HTML parser (`capture_html`), see "HTML transactions list" below; the last page, an empty strategy, other strategies and other transaction types are NOT observed, so the row stays UNVERIFIED and a run is never `REVIEWED_SHAPES` until a capture shows a last page. |
| `aifactor.predictor` | UNVERIFIED | UNVERIFIED | No capture contains an AI Factor page or response. Only the menu link `/sv/opener/AIFACTOR/{cat}` and the user flags `aiFactorEnabled` / `hasAiFactor` were seen. |
| `aifactor.model` | UNVERIFIED | UNVERIFIED | Same; the design intake records the predictor definition as in no capture. |
| `screen.definition` | UNVERIFIED | UNVERIFIED | Only the list `/app/opener/SCR` and a `.../screen/summary/{n}/revert` copy-to-screen action link (a mutation: never capture or follow it). |
| `simulation.result` | UNVERIFIED | UNVERIFIED | Only `POST /port_simul6.jsp` (HTML) and the list `/app/opener/SIM`; no JSON simulation response. |
| `billing.credit-table` | UNVERIFIED | UNVERIFIED | Only the menu links `/sv/account-settings/billing` and `/sv/account-settings/dataminer-api`; `GET /spr/user` carries an integer `apiQuota` of unknown meaning. |
| `billing.invoice` | UNVERIFIED | UNVERIFIED | No preserved statement or invoice. |

The same table is pinned in `tests/portfolio123/test_capture_catalog.py`. A run is
marked `REVIEWED_SHAPES` in its manifest only when every capability in its scope is
CONFIRMED; otherwise it is `UNVERIFIED_SYNTHETIC_SHAPES`, and the provenance
admission below refuses it. Every UNVERIFIED row is still an invented placeholder
that matches no real traffic. The first real export of an UNVERIFIED capability
therefore does three jobs, in this order:

1. It is inventoried: route templates, methods, status codes, and counts, with no
   bodies and no identifiers. Unreviewed routes are listed, never parsed.
2. A reviewer compares the inventory with the catalog placeholders and corrects
   the catalog and the proposed contract entries in a reviewed change.
3. Only then does a re-run normalize bodies. Anything that is not exactly the
   reviewed shape refuses the whole run.

Expect the first stage of each HAR capture to refuse with `NO_REVIEWED_ROUTES` or
`SHAPE_REFUSED`. That is the designed outcome until step 2 is done, not a fault.
A refused run writes nothing, so run `inventory-har` first (below): it writes only
route templates and counts.

## Transport rule

HAR first. The p123api wrapper is used only for a declared HAR gap or an explicit
owner selection, with a credit budget (see the last section). A browser, login,
network, or schema failure never switches transport.

## Before any export

- Use a user-controlled browser profile that is already signed in. The agent never
  types a password. Sign in and complete any second step yourself.
- Choose a staging root outside every repository and outside cloud-synced folders,
  for example `C:\Dev\scratch\<project>\p123-staging`. The code refuses a root
  inside a repository.
- A HAR holds browser session material. Export it with sensitive data included
  (the extractor requires a session header on every reviewed request, then
  discards it), keep it only in the staging root, and quarantine or delete it
  after verification. Provider-side credential rotation (`P123-SEC-001`) remains
  an owner action.
- Capture one capability at a time, sequentially, and reload nothing during the
  export. Record the capture time and a session expiry time (for example capture
  time plus two hours); both are command arguments.

## What to export, per capture

HAR captures use `stage-har`; file captures use `stage-file`. The capability name
is the `--capability` value.

| Capability | Form | Owner action |
|---|---|---|
| `aifactor.predictor` | HAR | Open the predictor page, wait until predictions finish loading, export the HAR. |
| `aifactor.model` | HAR | Open the model page and its validation tab, export the HAR. |
| `screen.definition` | HAR | Open each screen and its rules tab, export the HAR. |
| `simulation.result` | HAR | Open the simulation results page, wait for the curve, export the HAR. |
| `transactions.account` | HAR | Open the transactions tab, page to the last page without skipping, export the HAR. |
| `transactions.download` | file (CSV) | Use the transactions download control and save the file unmodified. Do not open and re-save it. |
| `designer-model.json` | file (JSON) or HAR | Export the designer model JSON unmodified, or open the designer model summary page and export the HAR (the page issues `GET /spr/strategy/model/{id}?dm=true`; the sibling `.../perf` route is not catalogued and stays inventoried only). The designer profile and the comment threads are redacted in the normalized record; the restricted raw copy keeps them. |
| `billing.credit-table` | HAR | Open the signed-in billing credits page, export the HAR. |
| `billing.invoice` | file (CSV or PDF) | Download each statement or invoice from the billing page unmodified, one run per file. EU123 cost evidence goes here. |

Send back only the run summary (counts, hashes, flags) and any refusal code. Do
not paste export contents, identifiers, or file paths into chat or an issue.

## Commands

Run from the repository with the workspace environment. All commands print one
line of aggregate JSON or `REFUSED <CODE>` and exit 2 on refusal.

```powershell
uv run --frozen --no-sync python -m quant_engine.portfolio123.capture_cli catalog

uv run --frozen --no-sync python -m quant_engine.portfolio123.capture_cli inventory-har `
  --staging-root <staging-root> --run-id <run-id> --input <export.har>

uv run --frozen --no-sync python -m quant_engine.portfolio123.capture_cli stage-har `
  --staging-root <staging-root> --run-id <run-id> --input <export.har> `
  --captured-at <rfc3339> --session-expires-at <rfc3339> `
  --capability aifactor.predictor [--capability <another> ...] [--allow-partial]

uv run --frozen --no-sync python -m quant_engine.portfolio123.capture_cli stage-file `
  --staging-root <staging-root> --run-id <run-id> --input <export-file> `
  --captured-at <rfc3339> --capability billing.invoice

uv run --frozen --no-sync python -m quant_engine.portfolio123.capture_cli verify `
  --staging-root <staging-root> --run-id <run-id> [--expect-manifest-sha256 <hash>]
```

`--capability` for a HAR run lists the exact scope. A reviewed route outside the
scope, or a scope capability with no response, refuses the run (`SCOPE_MISMATCH`).

## What a run contains

`inventory-har` writes one create-only file, `<staging-root>/p123-capture-inventory/<run-id>.json`
(route templates, status codes, counts; no bodies, identifiers, or source copy).
Unknown path words are redacted to `{x}` except structural words already in the catalog;
add `--reveal-segments` only when you accept that slugs may appear, to see real route names.
`<staging-root>/p123-capture/<run-id>/` holds `raw/source.<ext>` (byte copy of the
export), `inventory.json` (route templates and dispositions), `normalized/records.json`
(canonical, shape-checked), and `manifest.json` (written last, hash-bound). Staging
is create-only: an existing run id, or any existing destination file, refuses and
nothing is overwritten. A failed run leaves no partial directory. `verify`
re-reads the bytes, checks every hash and the exact file set, re-validates every
record against the catalog, and re-derives the normalized output from the raw
export; it trusts nothing the stager reported. The manifest is marked restricted
and records `REVIEWED_SHAPES` or `UNVERIFIED_SYNTHETIC_SHAPES` (recomputed by `verify`).

## Refusal codes

| Code | Meaning |
|---|---|
| `NO_REVIEWED_ROUTES` | No request in the HAR matches a catalog route (inspect the inventory). |
| `SHAPE_REFUSED` | A reviewed route returned a body that is not exactly the catalog shape. |
| `HAR_ENTRY_REJECTED` | A reviewed route failed status, session, query, or JSON checks. |
| `SCOPE_MISMATCH` | Capabilities present differ from `--capability`. |
| `COVERAGE_PARTIAL` | Pagination incomplete; re-export all pages, or `--allow-partial` to keep it flagged. |
| `PAGINATION_INCONSISTENT` / `CONFLICTING_DUPLICATE` | The export contradicts itself; re-export. |
| `MEDIA_TYPE_REFUSED` / `MEDIA_SIGNATURE_MISMATCH` | File type is not reviewed for that capability. |
| `CSV_COLUMNS_UNREVIEWED` / `CSV_SHAPE_REFUSED` / `EXPORT_HAS_NO_ROWS` | Download differs from the reviewed columns or is empty. |
| `RUN_EXISTS` / `DESTINATION_EXISTS` | Create-only staging refused to overwrite. |
| `STAGING_ROOT_IN_REPOSITORY` | Staging root is inside a repository. |
| `STAGING_ROOT_NOT_PRIVATE` | POSIX staging root is accessible to group or others (files are written 0600, directories 0700). |
| `REDERIVATION_MISMATCH` / `FILE_HASH_MISMATCH` / `UNDECLARED_FILES` / `CATALOG_DRIFT` | `verify` found a changed, forged, extra, or stale artifact. |

## API exception (declared HAR gap or explicit owner selection only)

`quant_engine.portfolio123.capture_api` is a read-only stub that has never run
live. To use it later the owner supplies, outside the repository:

- Governance evidence (schema version 1, operation `api-read`): a review
  reference to a `dachent/quant_engine` issue comment, an authority window, the
  terms hash and allow-list hash printed by the module, the credit budget and
  call bound with `retry_count` 0, the signed-in credit-table capture time (take
  the table from the `billing.credit-table` capture; never hard-code prices), and
  per operation a basis of `UNSUPPORTED_BY_HAR` or `USER_SELECTED`. A capability
  that has a reviewed HAR route needs `USER_SELECTED`.
- Credentials through the core resolver only, in the owner's configuration file:

```toml
[credentials.sources.azure-keyvault]
command = ["<read-only helper command>"]

[credentials.p123]
key_id = [{ source = "azure-keyvault", name = "<owner-supplied name>" }]
key = [{ source = "azure-keyvault", name = "<owner-supplied name>" }]
```

Exactly the fields `key_id` and `key` are accepted, and they must name two
different secrets (a plan that points both at one secret is refused with
`CREDENTIAL_PLAN_SHARED_SECRET`). The Key Vault source returns one whole secret
and has no sub-field support, so the existing single secret `portfolio123-api`
(internal shape unknown, never read here) cannot serve both fields through the
resolver. The minimal mapping that works with the resolver as it is uses two
secrets, `portfolio123-api-key-id` and `portfolio123-api-key`:

```toml
[credentials.sources.azure-keyvault]
command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", "C:/Dev/bin/kvget.ps1"]

[credentials.p123]
key_id = [{ source = "azure-keyvault", name = "portfolio123-api-key-id" }]
key = [{ source = "azure-keyvault", name = "portfolio123-api-key" }]
```

Both names are valid Key Vault secret names for the resolver and are proven offline
with a fake runner in `tests/portfolio123/test_capture_credential_plan.py`. The
owner creates the two secrets (or splits `portfolio123-api` into them); no value is
read or written by the code. Secret names appear in this runbook and in tests only,
never in the package code.

The allow-list is the reviewed metered read operations only. Rebalance commit and
transaction import or delete are not representable. `ai_factor_predict` is a
candidate for a declared HAR gap and is deliberately absent from the allow-list
until the owner declares the gap and the routing registry is extended in a
reviewed change.

## AI Factor design admission (issue 628, SRC-AIFACTOR-DESIGN-001)

The `p123-ai-factor` design slot moves from `REFERENCE_ONLY` to `EVIDENCED` only
through a staged, reviewed capture. `tools/portfolio_provenance.py` accepts a design
reference of the form `p123-capture-design:<sha256>` and recomputes it from the
admitted bytes; a label or a model container cannot produce one.

What admission needs from the capture (all of it is produced by `stage-har`):

| Needed | Source in the run | Check |
|---|---|---|
| A staged HAR run | `manifest.json`: `kind`, `form` = `har`, `restricted` | exact manifest key set, canonical bytes |
| Reviewed shapes | `evidence_status` = `REVIEWED_SHAPES` | every capability in scope CONFIRMED in the catalog |
| Both AI Factor captures | `scope` holds `aifactor.predictor` and `aifactor.model` | each has at least one record, coverage `COMPLETE` |
| Complete, not partial | `complete` true, `allow_partial` false | |
| Capture before the as-of date | `captured_at` | timezone-aware, not after `as_of_date` |
| Bound records | `files` entry for `normalized/records.json` | hash and size equal the admitted bytes |
| Successful, non-empty responses | each AI Factor record: `status` 2xx, `response` a non-empty object | |
| Predictor identity | each record's `request_sha256` (hash of method, origin, id-bearing path, query) | part of the design reference |
| Shapes reviewed in this repository | each AI Factor response re-validated against the catalog row, which must be CONFIRMED now | a self-consistent forged run with invented content is refused |
| Owner attestation | `expected_hashes` in the source group pins both `manifest.json` and `normalized/records.json` | the run's own claims are not trusted; the owner confirms the hashes |

The design reference is `p123-capture-design:` plus the SHA-256 of the canonical
JSON of the AI Factor records only (capability, `request_sha256`, sanitized
response), sorted. It ignores the capture time, the run id, and every other
capability in the run, so the same design captured twice has one reference and a
changed design has another.

What R3 supplies as data in the private provenance specification:

1. A source group for the run: a private root plus the explicit relative paths of
   `<run>/manifest.json` and `<run>/normalized/records.json` (the raw HAR, which
   holds session material, is not admitted and may be quarantined first), with an
   `expected_hashes` entry for each of those two files, confirmed by the owner.
2. In the `p123-ai-factor` version row, the `design` slot: `status` `EVIDENCED`,
   `artifact_sha256` = SHA-256 of `normalized/records.json`, `source_ids` = that
   group, `version_ref` = the reference printed by:

```powershell
uv run --frozen --no-sync python tools/portfolio_provenance.py capture-design-ref `
  --run-dir <staging-root>\p123-capture\<run-id>
```

It refuses a run that is unreviewed, partial, out of scope, or inconsistent. Admission
repeats every check from the admitted bytes at `stage` and `publish`.

What the attended HAR must contain: the AI Factor predictor page and the AI Factor
model page, each loaded to completion in one signed-in session, exported with
sensitive data included, with the JSON responses that carry the predictor and model
definitions (type, target, horizon, features, universe) returned with status 2xx.

Limit, stated plainly: the AI Factor routes and shapes are not in any preserved
capture, so they cannot be reviewed offline. After the first attended export,
`inventory-har` lists the routes; a reviewed change adds the two real routes and
shapes to `capture_catalog` and marks them CONFIRMED; only then does `stage-har`
produce a `REVIEWED_SHAPES` run that admission accepts. That review is a small,
mechanical code change (two catalog rows and their tests), not data, and it must
land before the run that admission binds is staged.

## CDP recorder for the pre-R3 sitting (`tools/p123_cdp_recorder.py`)

One attended sitting (Block 0) can learn the real routes and response shapes of the
UNVERIFIED capabilities (`aifactor.predictor`, `aifactor.model`, `screen.definition`,
`simulation.result`, `transactions.account`) so that R3 stays data-only. The recorder
attaches to a dedicated Edge or Chrome profile with a loopback debug port that the
owner signed in to. It records the traffic, and with `--drive-plan <file>` it can also
drive the signed-in tab itself: navigate on `https://www.portfolio123.com`, click and
scroll. Nothing here logs in or types.

Safety rules, enforced in code and pinned by `tests/portfolio123/test_p123_cdp_recorder.py`
and `tests/core/test_capture_action_guard.py`:

- Allowed in the signed-in window for any non-trading read: navigate, click, scroll. The
  messages sent to the browser are `Target.setDiscoverTargets`, `Target.attachToTarget`,
  `Network.enable`, `Network.getResponseBody`, and the driving messages `Page.navigate`
  (one URL on the P123 origin), `Runtime.evaluate` (only the fixed element probe of
  `tools/capture_action_guard.py`) and `Input.dispatchMouseEvent` (move, wheel, left
  click). A guard refuses everything else (reload, any other evaluate, key or text input,
  fetch interception, cookies) before it leaves the process. It listens to four Network
  events only.
- The ONE guard (`tools/capture_action_guard.py`, shared by the live capture tools):
  never click or open a control whose accessible name, text, `aria-label`, `value`,
  `title` or `href` matches trade, transfer, withdraw, deposit, order, buy, sell, submit,
  confirm payment, or an account-changing control (delete, remove, cancel, unsubscribe, close account, save or apply changes, change or reset password, update profile, email or settings, link, unlink, connect or disconnect broker) (token match after splitting camelCase and punctuation); never touch
  a submit control (a typeless `<button>` inside a form counts), an image submit or a
  password control; never follow a link off the origin; no typing message exists, so
  credentials and 2FA are never entered. A drive plan is a JSON list of
  `{"navigate": url}`, `{"click": "<exact control name>"}` and `{"scroll": deltaY}`; a
  step the guard would refuse is rejected before connecting (`DRIVE_PLAN_REFUSED`), a
  control found refused on the page stops the plan (`DRIVE_ACTION_REFUSED`). The action
  log (stderr) is value-free: verb, role, first 8 hex characters of the label's SHA-256.
  Runs recorded by the earlier, listen-only recorder still verify.
- Which tab is driven is never decided by event order. `--target-id <CDP target id>` names
  the one tab; without it the plan runs only when exactly one P123 page is open, and with
  several the run is refused (`DRIVE_TARGET_AMBIGUOUS`) and the candidate target ids (opaque
  ids, no URLs or titles) are listed on stderr. A control is found by its exact name, the
  first match in document order; the guard checks that control and the element that would
  receive the click.
- Origin-limited to `https://www.portfolio123.com` (exact host list `ALLOWED_HOSTS`,
  no port, no userinfo). Only page targets already on that origin are attached.
  Other-origin responses are counted, never listed.
- Never reads request headers, cookies, or the Authorization value, and never opens
  the `*ExtraInfo` events; a test hands the listener header objects that fail the test
  if touched. Query values are not persisted; the only exceptions are the literals
  `true`/`false` and integer pagination positions (`offset`, `page`, `limit`, `start`,
  `size`, `count`).
- Records only same-origin GET responses. Any non-GET request and any GET whose
  decoded path or query contains `revert`, `delete`, `remove`, `save`, `submit`,
  `update`, `create`, `rebalanc`, `trade`, `order`, `subscri`, `cancel`, `logout`,
  `password`, or `token` is NOT recorded and is listed in the manifest as
  `MUTATION_LIKE_NOT_RECORDED` (method, path template, reason `NON_GET` or `PATTERN`,
  count; never the query). The match is a substring match and fails closed, so a few
  harmless pages (`/app/tradeActivity`) are listed there too.
- Billing and identity paths (`account`, `profile`, `billing`, `invoice`, `payment`,
  and the `/spr/user` segment `user`) are not recorded unless
  `--include-identity <word>` names them; they appear as
  `IDENTITY_BILLING_NOT_RECORDED` with the template only.
- Body shapes keep field names, so objects keyed by data collapse to `$keyed`: more
  than 64 keys, or at least half the keys numeric, dates, uppercase ticker-like, or
  containing spaces or other non-identifier characters. A map keyed by plain lowercase
  words cannot be told apart from field names, so `discover` output is still treated
  as restricted evidence and is kept only in the governed root.
- Path templates replace identifiers (digit runs, UUIDs, long hex or token-like
  segments) with `{id}` and drop path parameters such as `;jsessionid=`.
- The run directory (`--output-root`) must be absolute, new, outside any Git
  worktree, and not inside a `scratch`/`tmp`/`temp` folder of a cloud-synced root.
  Files are create-only; `manifest.json` is written last. Raw bodies are restricted
  evidence (`docs/restricted-evidence-output.md`): keep them only in the governed root
  the owner chooses. Stdout carries counts and route templates, never contents.

Modes:

| Mode | Writes | Notes |
|---|---|---|
| `discover` | `discovery.json`, `manifest.json` | Per response: method, path template, query key names, status, media type, body shape (field names and types; objects keyed by data collapse to `$keyed`; ISO and `MM/DD/YYYY` dates are typed `date`/`us_date`), HTML table header labels and data-row counts, CSV header labels. No body is kept. |
| `capture` | `raw/NNNNN.body`, `entries.json`, `manifest.json` | Raw bytes of recorded GET JSON, HTML, and CSV responses with one SHA-256 each (bodies over 32 MiB or unreadable are flagged, not recorded). |
| `verify` | nothing | Re-hashes every file, rejects strays, re-derives every shape and profile from the raw bytes, re-screens every recorded route (a recorded non-GET or mutation-like or identity route fails), and requires the `safety` block unchanged. Exit 0 or `REFUSED <CODE>` with exit 2. |
| `propose-catalog` | `catalog-diff.draft.{json,md}`, `test_capture_new_rows.py.draft`, `proposal-manifest.json` | Verifies the discover run (and an optional `--raw-run`), maps observed 2xx GET routes to the five capabilities plus a confirmation of `designer-model.json`, compares with the current `catalog_rows()`, and writes a DRAFT. It is never applied. |

`propose-catalog` maps routes by keyword (`CAPABILITY_HINTS`); several candidates mean
the owner picks one, and `--map CAPABILITY=TEMPLATE` pins a route explicitly. Each
candidate carries the route template, query keys, response media type, status
counts, top-level shape, and a generated `Obj(...)`/`Seq(...)` source text in which
anything the sample cannot decide (mixed types, data-keyed objects) is written as
`"<MIXED:...>"` or `"<KEYED:...>"`, which is not a valid shape, so a row cannot be
applied without a human finishing it. `transactions.account` is an HTML table: the
draft records its header labels, data rows per page, and `offset` pagination, and
compares them with the catalog row (which already admits that route through
`capture_html`; an HTML route with no catalog HTML row states `requires_code_change`).
Dates, optional fields, and nullability are
inferred from the observed sample only; the reviewer confirms against the raw bodies.

Commands (Edge debug port 9333, the dedicated profile the owner already signed in to):

```powershell
msedge.exe --remote-debugging-port=9333 --user-data-dir=<the dedicated profile>
python tools/p123_cdp_recorder.py discover --cdp-url http://127.0.0.1:9333 --output-root <governed root>\discover-1
python tools/p123_cdp_recorder.py capture  --cdp-url http://127.0.0.1:9333 --output-root <governed root>\capture-1
python tools/p123_cdp_recorder.py verify   --run-root <governed root>\capture-1
python tools/p123_cdp_recorder.py propose-catalog --discover <governed root>\discover-1 --raw-run <governed root>\capture-1 --out <governed root>\catalog-draft
```

**Start the recorder FIRST, then load or reload the pages.** It only sees NEW
traffic: a page that finished loading before the recorder attached produces nothing
(a smoke run attached after page load recorded 0 entries; opening a new tab or
reloading after it was running recorded them). If nothing is recorded after
`--warn-after-seconds` (default 30; 0 disables) the recorder prints
`WARNING NO_ENTRIES_RECORDED` and writes it to the manifest `warnings`; an empty run
always records that warning at the end.

`discover` and `capture` run until Ctrl-C (or `--max-seconds`) and then write the
manifest. Nothing in this section was run against the live site: the proof is a
fake CDP transport replaying synthetic events built from the known route templates.

## Unattended allowlisted fetch (`tools/p123_cdp_fetcher.py`)

For a capture with nobody at the keyboard, the fetcher attaches to the owner's
already-signed-in tab (loopback debug port only), navigates it to a FIXED list of
read-only export URLs, and keeps the downloaded bytes unmodified. It never types the
password; a signed-out session is a stop, not a login.

Routes (all reviewed read-only `GET`; the screen calls `PORTFOLIO123_CONTRACT.route`):

| Kind | Route | Delivered as | Saved as | Next step |
|---|---|---|---|---|
| `transactions_csv` | `/p123/DownloadPortTrans?portid=<id>` | download `P123_SimTran_*.csv` | `raw/strategy_<id>_transactions.csv` | `stage-file --capability transactions.download` |
| `perf_csv` | `/p123/DownloadPortPerf?portid=<id>` | download `P123_PortPerf_*.csv` | `raw/strategy_<id>_perf.csv` | read by the generation (PG6), no staging capability |
| `holdings_csv` | `/p123/DownloadPortHoldings?portid=<id>` | download `P123_PortHoldings_*.csv` | `raw/strategy_<id>_holdings.csv` | optional; makes identity check I5 decisive |
| page record | `/app/investment/resource?f=1&s=<13-digit ms stamp>` | JSON page, body read | `raw/p123_investment_resource.json` | read by the identity check (I7); new reviewed contract route `investment.resource-list` |
| designer model | `/spr/strategy/model/<id>?dm=true` | JSON page, body read | `raw/designer_model_<id>.json` | `stage-file --capability designer-model.json` |

The perf route was found offline, not guessed. The preserved 2026-09 HARs show 89
responses of `GET /p123/DownloadPortPerf?portid=` as `200 text/csv`, `attachment`, named
`P123_PortPerf_<strategy name>.csv` (one unauthenticated call returned `401 text/html`
and no file), and the 2026-09-12 capture script (`p123_har_capture.py`, preserved under
`ops/preserved-scripts`) saved the same route as `strategy_<id>_perf.csv`. No new
contract route was needed. Transactions downloads are named `P123_SimTran_*`. The
sibling `/spr/strategy/portmodel/{id}/perf` JSON route is not used.

The page record route was also found offline. The 2026-09-12 capture manifest lists
`raw/p123_investment_resource.json` (a JSON list, one record per book, with `assetUid`,
`assetName`, `assetType`, `inceptionDt`, `posCnt`, `hasTrans`, ...), and the preserved HARs
show 78 `GET /app/investment/resource` calls, all `200 application/json`, with exactly the
query keys `f` and `s`, `f` always `1` and `s` a 13-digit millisecond value that differs
per call (a cache-buster). The route was not in the reviewed contract, so this change adds
it as `investment.resource-list` (query keys `f`, `s`, positive integers). The run generates
`s` from the wall clock once, and that one exact URL is the only page-record URL the run may
navigate to or read. Live behaviour is UNVERIFIED (the fixture proves the guard, not the
site). Without the page record, check I7 warns for every book (`no page record`), which is
a REVIEW for every book and therefore a stop for the first book with trades; request it with
`--page-record` when making the allowlist.

Allowlist: data, not code. `make-allowlist` writes a canonical JSON (schema_version 1;
`books[{id, kinds}]`, `designer_models[]`, `drive_files[]`, `page_record` flag, origin) and
prints its sha256; every `run`/`drive` takes `--allowlist` and `--allowlist-sha256` and
refuses a file whose bytes differ (`ALLOWLIST_PIN_MISMATCH`). The real list (book ids, the
two designer models, a Drive file id) lives with the owner outside the repository, not in a
commit.

```powershell
python -m tools.p123_cdp_fetcher make-allowlist --out <allowlist.json> --book <id> [--book <id> ...] `
  [--designer-model <id> ...] [--drive-file-id <id>] [--page-record] [--kind perf_csv ...]
python -m tools.p123_cdp_fetcher run --cdp-url http://127.0.0.1:9333 --allowlist <allowlist.json> `
  --allowlist-sha256 <printed hash> --download-dir <new dir> --as-of <D> `
  --reference-index <legacy-20260731-...>\artifacts\source-index.json `
  [--prior-dir <older or HAR download set> ...] [--cross-capture-dir <other capture dir> ...] `
  [--skip-designer-models] [--no-reference]
python -m tools.p123_cdp_fetcher drive --cdp-url http://127.0.0.1:9333 --allowlist <allowlist.json> `
  --allowlist-sha256 <printed hash> --download-dir <new dir> --file-id <id>
```

`run` refuses (`REFERENCE_REQUIRED`) without `--reference-index` (the `source-index.json` of
the 2026-07-31 run) unless `--no-reference` is given; the choice and the index sha256 are
recorded in the manifest. `--as-of D` (required, `REFUSED AS_OF_REQUIRED` without it) is the
financial cutoff: the identity check rolls each book's holdings back to D (I5D) and
`book-identity-decisions.json` records it as `settings.as_of`; the generation plan refuses
decisions whose as-of is not its own `--as-of-date`, and a capture whose perf curve ends
before D fails I5D. The binding decisions file is the as-of one: the fetcher run with
`--as-of D` (D = the plan's as-of date) and `--no-reference`; a standalone
`tools/p123_book_identity_check.py --as-of D --reference-index ...` run (no
`--require-reference`) adds the reference tie, and a book absent from the reference pull is
REVIEW, accepted per book at plan time. `--prior-dir` (warn) and `--cross-capture-dir` (fail) feed the
identity check's permutation comparison: the same trade rows under another id.

`--download-dir` must not exist (absolute, outside Git, not in synced scratch). The
fetcher creates `capture/raw/` (the saved files and `capture/capture-manifest.json`, the
layout `portfolio123_generation.py plan --transaction-capture` reads), `incoming/` (the
browser's download target, empty between files), `p123-fetch-manifest.json` (value-free:
per file sha256, size, row count, stage capability, identity decision; never contents),
and `book-identity-decisions.json` (restricted: it carries the check's dates and amounts;
written even when the run STOPPED, from a final check run after the last manifest write so
it binds the final `capture-manifest.json` bytes).

Safety rules, pinned by `tests/portfolio123/test_p123_cdp_fetcher.py`:

- Transport allow-list: `Target.setDiscoverTargets`, `Target.attachToTarget`,
  `Page.navigate` to an exact allowlisted URL, and `Browser.setDownloadBehavior`
  (`allowAndName`, events on, the one download directory; `default` on exit). `Runtime.*`,
  `Fetch.*` and `Input.*` are refused before they leave the process.
- One narrow widening, only in a run that fetches the page record or designer models:
  `Network.enable` (no arguments) and `Network.getResponseBody`. A body read is accepted only
  for a request id that the session saw with method `GET` and exactly the allowlisted URL
  (the page-record URL of this run, or `/spr/strategy/model/<id>?dm=true` for an allowlisted
  id with query exactly `dm=true`); an id is dropped after one read. Every other `Network.*`
  method, any other id, any extra parameter, and any read in a run without such an entry is
  refused. The body is saved exactly as returned, create-only; the CDP body is text, so it
  is byte-identical to the response only when the server sends UTF-8 without a BOM
  (UNVERIFIED live). A redirect, a non-200, an HTML response, or a body that is not the
  expected JSON shape (a list for the page record, an object for a designer model) is a stop
  and nothing is saved.
- Refused before sending: not https, off-origin or userinfo, a mutation word (the
  recorder's `MUTATION_WORDS`), a URL that is not exactly an allowlist entry or not a
  reviewed contract route.
- Stops, exit 3, files already saved are kept and the manifest says `STOPPED`:
  `STOP_REAUTH` (the tab is on a login page or off `www.portfolio123.com`, a JSON page
  answered 401/403, was redirected or served HTML, or no download starts after navigation,
  which is how the preserved HARs show a signed-out session), `STOP_DOWNLOAD_NOT_EXACT`
  (URL, `P123_*` name prefix, `.csv`, a single completed download, a non-empty file whose
  size equals the bytes received, and nothing else in the directory), `STOP_IDENTITY_FAIL`
  (below), and `STOP_NO_SIGNED_IN_TAB` / `STOP_TARGET_AMBIGUOUS` (no or several candidate
  tabs after discovery, recorded in the manifest because the output directory already
  exists). Exit 2 is `REFUSED <CODE>` before anything was sent.
- Download ownership: the browser's download events and directory are shared by every tab,
  so a download counts only when `Browser.downloadWillBegin.frameId` equals the frame id
  returned by this run's `Page.navigate`. A download from another frame is ignored, and its
  file in the shared directory turns the exact-directory check into a stop, so it is never
  saved or manifested under an allowlisted id. Run with no other tab downloading anyway.
- Interpreter: the identity check is only guarded in its own `main`, so `run` refuses on any
  Python other than 3.11 before connecting (`INTERPRETER_REFUSED`, exit 2) and again before
  each call to the check.

Book identity check. After the last file of each book lands (the check needs both
transactions and perf, so it cannot run after the first file), the fetcher calls
`check_capture` of `tools/p123_book_identity_check.py` on `capture/` with the books landed
so far (so I8, no shared trade rows across books, is cumulative), the capture manifest (I1),
the reference index with `require_reference` (I9: a book absent from the reference pull
fails), and the `--prior-dir`/`--cross-capture-dir` sets. The page record is read first, so
I7 has its input. Result policy:

- `PASS`: continue.
- `FAIL`: stop at once (exit 3); no later book is fetched. The manifest and
  `book-identity-decisions.json` are still written.
- `REVIEW` (warnings only, for example a missing optional holdings export, no page record,
  or a shared simulation that is not on the account page): NEVER stops downloads. The book
  is recorded `REVIEW` with its warning checks (`identity_checks`, `review_checks`) and the
  run continues through every book. There is no special case for any book or book type.
  An unknown or missing decision is a `FAIL`.

Exit codes of `run`: `0` every book PASS (status `COMPLETE`); `2` refused before anything
was sent; `3` stopped (a FAIL, a login, an inexact download, ...); `4` no book FAILED but at
least one book is REVIEW (status `REVIEW_PENDING`). Exit 4 is NOT a failure of the capture:
every file was fetched and saved. It means the capture is not accepted yet. Nothing non-PASS
passes silently: the summary line carries `review_pending` (book id to check names), one
`REVIEW_PENDING book=<id> checks=<names>` line per book follows it, and the manifest lists
the same under `review_pending`. The operator must accept each REVIEW book at generation
time with `portfolio123_generation.py plan --accept-identity-review ID=REASON` (the reason
states what was seen); until then the generation refuses it. Re-pull or exclude a FAILed id
instead. For an unattended timeline, treat exit 4 as "capture finished, operator decision
pending", exit 0 as "finished and clean", and exit 3 as "stopped, partial directory".

**This needs PR 827:** the module is loaded by file path so this tool still imports and
passes CI without it, but `run` refuses with `IDENTITY_CHECK_UNAVAILABLE` (there is no skip
flag) until that module and its `quant_engine.portfolio123` dependency are on the branch.
`drive` does not use it.

Pairing with the staging CLI. Per saved transactions file or designer model, with the same
signed-in session's capture time:

```powershell
python -m quant_engine.portfolio123.capture_cli stage-file --staging-root <root> --run-id <run> `
  --input <download-dir>\capture\raw\strategy_<id>_transactions.csv --captured-at <rfc3339> `
  --capability transactions.download
python -m quant_engine.portfolio123.capture_cli verify --staging-root <root> --run-id <run>
```

Perf, holdings and the page record are not catalogued capabilities: they travel in the
`capture/` directory for the generation and the identity check, bound by
`capture-manifest.json`.

Google Drive mode. `drive` navigates the one tab to
`https://drive.google.com/uc?export=download&id=<file id>` for an id in the allowlist
`drive_files`, and accepts the download only when its reported start URL is https on
`drive.google.com`, `drive.usercontent.google.com`, or `doc-<label>-docs.googleusercontent.com`
(one anchored pattern). It needs a tab that is blank or already on those hosts; it will not
reuse the Portfolio123 tab. The bytes are saved as `capture/files/drive_<id><ext>`
(extension from the reported name, the name itself is not stored) with a sha256 manifest.

Stop rule for an unattended run: on any `STOP_*` the run ends, nothing is retried, no
second login or alternative transport is tried, and the owner decides. A partial directory
is evidence of what was saved, not a complete capture.

Designer models and what the model 1765750 page serves (offline, from retained evidence;
field names and categories only). `designer_models` entries are fetched with the narrow body
read above; `--skip-designer-models` skips them (listed under `designer_models_not_fetched`).

- The designer-model JSON shape is known for one model only (the 2026-09-12 capture of the
  first designer model). It has 37 top-level fields: `backtestDisabled`, `benchmark`,
  `benchmarkUid`, `comments` (list), `context`, `currency`, `desc`, `designerModel`
  (object), `editable`, `endDt`, `hasRebalTran`, `hedgeEnabled`, `holdingsAvg`,
  `inceptionDt`, `lastRebal`, `lastRebalanceToday`, `launchDt`, `liquidationValue`,
  `maxdd90`, `mkttypeUid`, `name`, `pctHedgeAvg`, `portUid`, `rebalFreq`, `refLiquidity`,
  `riskScore`, `stats3y`, `statsInception`, `statsLaunch` (objects), `topData` (list of 5),
  `tradesTtm`, `tradingAll`, `tradingCommitment`, `tradingLose`, `tradingWin` (objects),
  `trailingReturns` (list), `volatility90`. It carries trade COUNTS and summary statistics
  only (`tradesTtm`, `hasRebalTran`, `trading*`); no transaction list and no holdings list.
- Model 1765750 was never captured through `/spr/strategy/model/<id>?dm=true`: no preserved
  HAR or capture file contains that route for it, so its model JSON shape is unobserved and
  assumed (not shown) to match the above. What the evidence does show about 1765750: it is
  listed by `/app/investment/resource` (the list) with `assetType` `DESIGNER_MODEL`, `type`
  `Designer Model`, `currency` USD, `hasTrans` true, `posCnt` 0, `needsRebal` true, inception
  year 2026, live-account flag true; and `/app/investment/resource/<id>` returns a detail
  record with fields `designer`, `startingCapital`, `initializeAmount`, `cost`, `yield`,
  `lastRebalDt`, `nextRebalDt`, `rebalanceMethod`, `maxDrift`, `stats`, `tickers`,
  `hasTrans`, `posCnt` (no transaction rows).
- Its trade-history route is unknown. The only 1765750 capture of the classic pages (a
  2026-07-27 session) called `port_summary.jsp`, `port_wiz.jsp`, `holdings.jsp`,
  `performance.jsp`, `performance_stats.jsp`, `transact_list.jsp?portid=` (107 calls) and the
  five `DownloadPort*` routes, and every one of them was `401` or aborted because that
  session was signed out, so no trade history was observed. `hasTrans` true says the book
  has transactions; where they are served is not established. No other book's data is
  substituted: a designer model with no CSV is captured as the JSON page it serves and
  nothing else.

UNVERIFIED, never exercised live (the proof is a fake CDP transport):

- Whether a `Page.navigate` to a download route in the current Edge/Chrome emits
  `Browser.downloadWillBegin`/`downloadProgress` with `allowAndName` on the browser-level
  session exactly as scripted (the preserved 2026-09-12 run saved downloads by the same
  navigation but through the browser's default folder).
- Whether `Network.getResponseBody` returns the document body of a JSON page navigation
  (and byte-identical bytes), and whether the page-record `s` value may be freshly
  generated.
- Whether `downloadWillBegin.frameId` equals the `Page.navigate` frame id for a
  download-by-navigation (if it does not, every download stops with
  `STOP_DOWNLOAD_NOT_EXACT`, which fails closed).
- Whether any interstitial appears (a download prompt, a Drive virus-scan confirmation
  for large files, a consent banner). Any of them looks like "no download started" and
  stops.
- Session lifetime: how long the signed-in tab survives an unattended run; a signed-out
  session is a `STOP_REAUTH` mid-run with a partial directory.
- Transaction and perf row counts versus the page, which only the identity check
  (`I3`, `I4`) and the generation transaction gate judge.

### In-page routes (`routes` command)

Some reads only answer a `fetch()` issued FROM the signed-in page: the same URLs return
HTTP 400 when requested by direct navigation. `routes` navigates the tab to the host page
that normally issues each request, then runs one pinned `fetch` for the exact allowlisted
URL and saves the body unmodified (create-only) with a sha256 manifest entry. Allowlist
entries are `--route KIND:ID` (`make-allowlist`); an allowlist without `routes` keeps its
bytes and pin.

| Kind | Host page | In-page GET | Saved as |
|---|---|---|---|
| `designer_model` | `/app/r2g/summary?id=<id>` | `/spr/strategy/model/<id>?dm=true` | `raw/r2g_<id>_dm_true.json` |
| `designer_perf_full` | same | `/spr/strategy/model/<id>/perf?dm=true&init=true&years=25` (the FULL curve; its first date must equal the model's `inceptionDt`, else `STOP_SHAPE_UNEXPECTED` and nothing is saved; needs `designer_model` for the same id in the allowlist, read first) | `raw/r2g_<id>_perf-full.json` |
| `designer_holdings` | same | `/spr/strategy/model/<id>/holdings?dm=true` | `raw/r2g_<id>_holdings.json` |
| `investment_perfdata` | `/app/investment/details?id=<id>` | `/app/investment/perf-data/<id>?init=1` | `raw/strategy_<id>_app_investment_perfdata_init.json` |
| `investment_resource` | same | `/app/investment/resource/<id>` | `raw/strategy_<id>_app_investment_resource.json` |
| `investment_perfdata_blank` | same | `/app/investment/perf-data/<id>?init=` (catalog spelling; the operator-recorded request is `init=1`) | `raw/strategy_<id>_app_investment_perfdata_init_blank.json` |
| `investment_perfdata_true` | same | `/app/investment/perf-data/<id>?init=true` | `raw/strategy_<id>_app_investment_perfdata_init_true.json` |
| `investment_trans_resource` | same | `/app/investment/transResource/<id>` | `raw/strategy_<id>_app_investment_transresource.json` |
| `investment_holdings` | same | `/app/investment/holdings/<id>` | `raw/strategy_<id>_app_investment_holdings.json` |
| `investment_performance` | same | `/spr/strategy/portmodel/<id>/perf` | `raw/strategy_<id>_strategy_portmodel_perf.json` |
| `investment_performance_page` | same | `/app/investment/performance` (HTML fragment) | `raw/strategy_<id>_app_investment_performance.html` |
| `investment_holdings_csv` | same | `/app/investment/holdings.csv?id=<id>&v=basic` | `raw/strategy_<id>_app_investment_holdings_basic.csv` |
| `investment_trans_csv` | same | `/app/investment/trans.csv?id=<id>&type=&tickers=&fromDt=&toDt=` | `raw/strategy_<id>_app_investment_trans.csv` |
| `account_resource` | `/app/account/details?id=<id>` | `/app/account/resource/<id>` | `raw/account_<id>_app_account_resource.json` |
| `account_positions` | same | `/app/account/positions/<id>` | `raw/account_<id>_app_account_positions.json` |
| `account_pages` | the details page and each `&t=<tab>` | none (visit only) | nothing |

Rules, pinned by `tests/portfolio123/test_p123_cdp_routes.py`:

- The one widening of the transport: `Runtime.evaluate`, accepted only when the run supplied
  pinned expressions and the message is exactly `{expression, awaitPromise: true,
  returnByValue: true}` with an expression that is byte-for-byte the page probe or the
  `fetch` template for an allowlisted URL (`GET`, same origin, cookies, `redirect:'error'`).
  Every other run, method, expression and extra parameter is refused. No input events.
- Stops, value-free (the code and the check, never a value from a body): non-200
  (`STOP_DOWNLOAD_NOT_EXACT`), 401/403, a redirect, HTML where JSON or CSV is expected, or a
  login page (`STOP_REAUTH`), and an unexpected shape (`STOP_SHAPE_UNEXPECTED`: wrong
  container type, missing marker field, designer model id differs, wrong CSV header, empty
  body). Nothing is saved for a refused body; files already saved are kept and the rest are
  listed in `routes_not_fetched`.
- `page_gets` is accepted as the earlier name of the allowlist's `routes` list (and `--page-get` of `--route`). `run --dry-run` screens any allowlist and prints the plan with no browser and nothing written; `run` with a route-only allowlist reads the routes (no identity check, no `--as-of`), and refuses routes mixed with books.
- Page visits may navigate within the signed-in tab only to allowlisted pages; nothing is
  clicked, typed or submitted.
- **`--dry-run`**: the same code path and the same messages. It differs only in where it
  writes: the root must be under a `scratch` folder and not under `releases` or `evidence`,
  files land in `dry-run-capture/` (never `capture/`, the name staging reads), the manifest
  is `p123-dry-run-manifest.json` with `dry_run: true` and `evidence_eligible: false`. Run it
  before any evidence is spent; then run the same allowlist without the flag.

## Admitting recorder and fetcher bodies into the HAR root (`tools/p123_har_admit.py`)

The generation reads only the `csv/` and `downloads/` files of the plan's HAR root, by file name
(`strategy_<id>_app_investment_perfdata_<id>.csv` is managed performance, `..._resource_<id>.csv`
managed metadata, `..._holdings_<id>.csv` managed holdings, `strategy_<id>_perf.csv` the equity
curve). The 2026-09-02 HAR workspace holds the account's investment list but no per-investment
performance, resource or holdings, so a plan on it alone has no managed performance. The
recorder saves raw bodies under id-less path templates (`raw/NNNNN.body`); the fetcher's
`routes` command (PR 868; the fetcher on main saves book exports and `designer_model_<id>.json`, which this tool does not admit) names its bodies with the id. `p123_har_admit.py` is the one offline tool that
turns either into HAR CSVs. It never writes to its inputs.

```powershell
python tools/p123_har_admit.py admit --base <HAR root> --out <new governed dir> `
    --recorder <recorder run> [--recorder <another sitting>] [--fetcher <fetcher capture dir>] `
    --as-of 2026-11-06 --account-id <id> [--account-id <id>] [--dry-run]
python tools/p123_har_admit.py stage-designer --out <new dir> --model-id <id> `
    (--recorder <recorder run> | --fetcher <fetcher capture dir>) [--dry-run]
```

`admit` copies the base root into `<out>/har-root/`, adds the admitted CSVs, and writes
`<out>/p123-har-admission-manifest.json` beside it (outside the root, so the plan does not hash
it). The plan's `HAR_ROOT` is `<out>/har-root`. The manifest records the sha256 of the recorder
manifest and entries, of every source body and of every output CSV, the binding evidence per
item, the last date of every performance series, the base files an admitted file replaced, the
stale lists left in the root, and the final root layout (every file with its sha256 and whether
it is `base` or `admitted`).

Id binding (recorder bodies), each step checked, a body that cannot be bound is a refusal:

1. the account's investment list (`/app/investment/resource`) names every item by
   `portfolioUid`; across sittings its items must be identical (live columns may move, the
   list of the first `--recorder` is the one saved, the others are recorded);
2. an item's resource body is matched to exactly one list row on `name`, `inceptionDt` and
   `posCnt` (zero or several refuse; a `portfolioUid` in the body must agree);
3. a `perf-data` or `holdings` body binds to the one item, among those whose resource body is
   within 40 ordinals before it, that it corroborates (perf `date` equals `inceptionDt`, the
   position count equals `posCnt`); recorder ordinals follow response completion, so two items
   that both fit are an ambiguous interleaving and refuse;
4. a fetcher body carries the id in its name and must be byte-identical to the recorder body of
   the same item; two different bodies for one item refuse.

Also refused: a fetcher capture with no admissible body, an output root that overlaps the base
root, a performance series whose rows differ from the weekday calendar of
`date..dateLast`, one that ends before `--as-of`, an `--account-id` no run states, and an
admitted file that would replace a base data file (only the account's list csv is replaced).

`stage-designer` writes `capture-manifest.json` and `raw/r2g_<id>_dm_true.json`,
`r2g_<id>_perf-full.json` (and `_holdings.json` when present) for
`tools/portfolio_designer_model_curve.py`. A curve is admitted only when it starts on the
model's `inceptionDt`, ends on its `endDt` and has its launch line on `launchDt`: the chart's
default 15-year window starts years after inception and is refused (`years=25` is the full
history).

A run may end `CAPTURE_WITH_UNRECORDED_BODIES` (for example an unreadable `/app/state` poll) and
still be admitted, under one explicit rule: a 200 GET entry of a NEEDED route with no body file
is a refusal. Needed routes are the account list, item resource, item perf-data, item holdings
and account resource for `admit`, and the model, model perf and model holdings for
`stage-designer`; any other unrecorded body is recorded in the manifest and ignored.

`stage-designer` also meets several chart windows of one model (15 and 20 years beside the full
history). It stages the one body that starts on `inceptionDt`, ends on `endDt` and has its
launch line on `launchDt`, lists the windowed bodies (sha256) in the staged manifest and does not
stage them, and refuses two different full-history bodies.

Both commands build their output in an owned temp sibling directory (`.<name>.partial-<pid>`) and
rename it into place; a failure removes the sibling and leaves no output, so a retry is never
blocked, and a final path that exists (also one that appears while the output is built) is refused.

`--dry-run` is the scratch mode: the output must sit under a `scratch` folder, the manifest says
`evidence_eligible: false`, and every check and message is the same. A real run refuses a
scratch, tmp or temp root and any root inside a Git worktree.

## HTML transactions list (`transactions.account`)

`GET /transact_list.jsp?portid=<id>&offset=<n>` (the first page may omit `offset`) is
HTML, so it bypasses the JSON extractor (and the JSON `capture_contract`, which still
refuses this route) and goes through `capture_html.parse_transactions_page`, which uses
only the standard-library `html.parser` and refuses on anything unexpected.

Same gates as a JSON entry: HAR form only, status 2xx (any other status on a matched
route refuses the run), a session header of at least 16 characters (name and length
only; it is never stored outside the raw HAR), `text/html`, session bounds.

Accepted structure (all from preserved real pages): exactly one `TableData` table with
a `thead` (pager row, `rowTwo` header row) and a `tbody` of data rows; `table`, `thead`,
`tbody`, `tr`, `th`, `td` explicitly closed with no stray end tags inside; header cells
`(blank), Date, Type, Ticker, Shares, Price, Amount, Tot Fees, Rank, Note, TRADE`; pager
text `A - B of N`; page links, the download link, and the date links all bound to the
requested strategy; a page-size selection of 50 when one is selected; 11 cells per row;
date link text `MM/DD/YY` (or four-digit year) equal to the link's own date; type in
`BUY, SELL, DIV, SPLIT`; ticker box with a `:MARKET` suffix; comma-grouped numbers (a cell holds only the number, optionally inside one `span`; empty
allowed for shares, price, amount, fees, rank; a value a JSON float cannot hold exactly,
such as more than about 15 significant digits, refuses); a note of at most 200 characters; an empty
TRADE cell. Declared range must equal the request: `A = offset + 1`, `B = min(offset + 50,
N)`, with exactly `B - A + 1` rows (a short page that is not the last, rows beyond the
declared end, and an empty page all refuse); duplicate row sequence numbers refuse.

Refusal codes: `TABLE_NOT_FOUND`, `TABLE_AMBIGUOUS`, `TABLE_MALFORMED`, `TABLE_STRUCTURE`,
`HEADER_UNREVIEWED`, `PAGER_UNREVIEWED`, `PAGER_INCONSISTENT`, `ROWS_PER_PAGE_UNREVIEWED`,
`LINK_UNREVIEWED`, `LINK_STRATEGY_MISMATCH`, `DATE_UNREVIEWED`, `DATE_LINK_MISMATCH`,
`TYPE_UNREVIEWED`, `TICKER_UNREVIEWED`, `NUMBER_UNREVIEWED`, `CELL_STRUCTURE`,
`ROW_CELL_COUNT`, `ROW_SEQUENCE_UNREVIEWED`, `NOTE_TOO_LONG`, `TRADE_CELL_UNREVIEWED`,
`OFFSET_MISMATCH`, `SHORT_PAGE_NOT_LAST`, `ROWS_AFTER_DECLARED_END`, `EMPTY_PAGE`,
`DUPLICATE_ROWS`, `STRATEGIES_REQUIRED`, `STRATEGY_MISMATCH`, `STRATEGY_IDS_INVALID`,
`STRATEGY_IDS_UNEXPECTED`, `HTML_NOT_UTF8`, `HTML_TOO_LARGE`, `REQUEST_UNREVIEWED`.

Normalized record per page (no identifiers: the strategy id becomes a hash, the row
sequence number, the TRADE column, and every link are checked and dropped): `strategy_sha256`,
`offset`, `first`, `last`, `total`, and rows with `date` (ISO), `type`, `ticker`, `market`,
`shares`, `price`, `amount`, `tot_fees`, `rank` (nullable numbers), `note`.

Completeness per strategy: one `total` on every page, offsets only on 50-row steps up to
`N`, each offset once (identical duplicate pages dedupe, differing ones refuse), no repeated
row sequence number across pages, and every offset from 0 to the last present. Anything
less is `COVERAGE_PARTIAL` (refused unless `--allow-partial`, which `verify` still
refuses by default). The owner declares the strategies the run must cover with
`stage-har --strategy-id <id>` (repeat per strategy; only the hash is stored in the
manifest summary as `strategy_sha256s`): the strategies found must equal the declared set
(`STRATEGIES_REQUIRED`, `STRATEGY_MISMATCH`), and `verify` re-derives against the stored
hashes. A run is `REVIEWED_SHAPES` only when every capability in scope is CONFIRMED; this
row flips to CONFIRMED when a preserved capture shows a last page (a short page whose
range ends at the total), which is a reviewed catalog change, not a code change.

The HTML header differs from the CSV download header (`Ticker`, `Tot Fees`, `Note` versus
`Symbol`, `TotFees`, `Notes`), and the HTML date has a two-digit year; downstream joins
must map them explicitly. Conformance check against the preserved bytes (counts only): the
eight real pages stage with `allow_partial` as 8 records, 400 rows, 1 strategy, 56 pages
expected, PARTIAL, `UNVERIFIED_SYNTHETIC_SHAPES`, and strict `verify` refuses with `COVERAGE_PARTIAL`.

## Dry run before any governed run (rule for every live driver)

Before a governed live run spends single-use evidence, an allowlist pin or an owner session:

1. rehearse the identical command line and config against scratch output, with no governance
   evidence, consuming nothing, and never into a release root or the intake being built;
2. verify what the rehearsal staged with the same verifier the governed run uses;
3. run the governed command only after a rehearsal of the same config and cutoff verified.

A rehearsal's run ids start with `DRY-`, its outputs are never admitted, and a governed command
refuses such an id. The Yahoo fetch driver has this natively (`dry-run`,
`docs/providers/yahoo-fetch.md`, "Dry run first"). The recorder (`tools/p123_cdp_recorder.py`) has it natively: `python tools/p123_cdp_recorder.py dry-run --cdp-url <url> [--drive-plan <file>]` is the `capture` code path written to a scratch run root named `DRY-<utc>` (a new temporary directory, or `--output-root <dir>\DRY-<name>` in a directory that holds only `DRY-` entries), with no governance evidence, nothing consumed, `verify` run on the rehearsal before it prints (`dry_run: true`, the `DRY-` run id, `verified: true`). A governed `discover` or `capture` refuses a `DRY-` output root and `propose-catalog` refuses a `DRY-` run (`DRY_RUN_NOT_ADMITTED`). The fetcher's dry run is owned by its own work. Until it has one, the rehearsal is a ONE-book scratch run: `make-allowlist --book <one id> --kind perf_csv`, then `run --download-dir <new scratch dir> --no-reference --as-of D` against that allowlist (read-only reads of one book, a new directory outside synced storage), then the identity check on what it landed, and only then the governed allowlist; the recorder is rehearsed with `capture` and `verify --run-root` on a throwaway run root before the governed capture.

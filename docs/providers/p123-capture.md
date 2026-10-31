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
| `transactions.account` | DIFFERS | UNVERIFIED | No JSON account-transactions route in 92 HARs. The real list is `GET /transact_list.jsp?portid=&offset=`, an HTML table, 50 rows per page, `offset` (not `page`), strategy-scoped. The JSON-only extractor cannot admit it; use the reviewed CSV download. |
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

# Portfolio123 owner capture runbook

This runbook covers the Portfolio123 captures the reviewed web contract does not
cover today (AI Factor predictor, AI Factor model, screen, simulation) plus
account transactions, designer-model JSON, and billing evidence. It complements
`docs/providers/portfolio123.md` (routing and generation) and
`docs/p123-security-quarantine.md` (HAR handling). Nothing here logs in, reaches
the network, or calls the API. The owner exports; the code stages, normalizes, and
verifies offline.

## Status: every route and shape is UNVERIFIED

No real Portfolio123 response was observed when this code was written. The route
paths and response shapes in `quant_engine.portfolio123.capture_catalog` are
placeholders exercised only against invented fixtures. The first real export
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
| `designer-model.json` | file (JSON) or HAR | Export the designer model JSON unmodified, or open the model page and export the HAR. |
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
`<staging-root>/p123-capture/<run-id>/` holds `raw/source.<ext>` (byte copy of the
export), `inventory.json` (route templates and dispositions), `normalized/records.json`
(canonical, shape-checked), and `manifest.json` (written last, hash-bound). Staging
is create-only: an existing run id, or any existing destination file, refuses and
nothing is overwritten. A failed run leaves no partial directory. `verify`
re-reads the bytes, checks every hash and the exact file set, re-validates every
record against the catalog, and re-derives the normalized output from the raw
export; it trusts nothing the stager reported. The manifest is marked restricted
and `UNVERIFIED_SYNTHETIC_SHAPES`.

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

Exactly the fields `key_id` and `key` are accepted. Secret names are
owner-supplied and appear nowhere in the code.

The allow-list is the reviewed metered read operations only. Rebalance commit and
transaction import or delete are not representable. `ai_factor_predict` is a
candidate for a declared HAR gap and is deliberately absent from the allow-list
until the owner declares the gap and the routing registry is extended in a
reviewed change.

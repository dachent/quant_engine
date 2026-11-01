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

Cutoff. The manifest cutoff is the financial cutoff (repository policy: a later
capture timestamp does not change it; later source evidence is preserved and
post-cutoff financial observations are excluded). Time-series rows dated after the
cutoff are therefore left out of the CSVs while the raw HAR keeps them:
`ssg_*_pricehistory` and the dashboard price history by `time`, rebalance history by
date key, and account activity by the latest of a row's timestamps
(`time`, `date`, `completed_at`, `filled_at`, `created_at`, `submitted_at`,
`updated_at`, `canceled_at`; a row with none is kept). Counts per dataset are in
`conversion-report.json` (`derivation.excluded_after_cutoff`). `funds.statistics`
whose `end_day` is after the cutoff is refused, not filtered. Capture-time snapshots
(`user_profile`, `accounts`, `partner_status`, dashboard holdings and wins, the two
fund-metadata tables) have no observation date; they are what the provider showed
when the response was captured, not a cutoff-dated value. Each is labelled with
`captured_at` (UTC, the `startedDateTime` of the HAR entry that carried the
response, or the intake manifest's session capture time when the entry has none;
`source` says which) in `conversion-report.json` (`snapshots[]`) and, as
`snapshots[]` with `file`, `captured_at` and optional `gap_hours`, in
`native-manifest.json`. When a snapshot was captured after the cutoff day end
(cutoff day + 1, 00:00 UTC) the label also carries `gap_hours`, the positive gap in
hours; inside the cutoff day there is no gap key. `plan --native-manifest` carries
the labels into the plan (`snapshot_labels`) and into the catalog's dataset rows
(`captured_at`, `snapshot_kind: CAPTURE_TIME_SNAPSHOT`, and for a late snapshot
`gap_hours_after_cutoff_end` and `snapshot_exception`); dated series and legacy
plans carry none of it, so legacy plans, plan ids and catalogs are unchanged.

Downstream consumers of the snapshot files (traced by code) and the refusal. The
`accounts.csv` `settled_balance` of the `alpaca` row feeds a cutoff-bounded cash
closure: `tools/build_quantbase_fund_facts.py:699-707` (`_provider_cash`), used at
`:1394`, compared with activity-derived cash at `:1262-1270` (refusal when the
residual differs from the declaration) and published as `provider_settled_cash` at
`:1320` (shown in `tools/sleeve_disclosures.py:820`); the same closure is in
`acceptance/derive_a/sleeves.py:1010-1019` and `acceptance/derive_b/derive.py:6088-6096`.
The `accounts.csv` fund `value` becomes `QuantbaseSleeve.snapshot_value`
(`acceptance/derive_a/sources.py:562-577`) and is only a note in
`acceptance/derive_a/derive.py:1311-1321`. No consumer of `user_profile`,
`partner_status`, dashboard holdings or wins, `ssg_all_funds_stats` or
`projections_all_live_funds` exists in `tools`, `packages`, `acceptance`, `apps` or
`ops/mvp2-release`. NAV comes only from the dated series
(`dashboard_Volatis_Strategy_pricehistory.csv`) and activity. Because a balance
snapshot taken after the cutoff day end would be compared with cutoff-bounded cash,
`convert` and `verify` REFUSE any snapshot whose `captured_at` is after the cutoff
day end unless `--snapshot-exception LABEL` is passed (label: 1-120 characters,
`[A-Za-z0-9 ._:/#-]`, starting alphanumeric). The label is recorded in the report,
the native manifest, the plan and the catalog's late snapshot rows, and an
exception given when no snapshot is late is refused. R3 schedules the Quantbase
capture on the cutoff day after the close, so the gap is zero and no exception is
needed.

An output directory that is a link, or whose `quantbase-har-csv` ancestor is a
link, is refused.

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

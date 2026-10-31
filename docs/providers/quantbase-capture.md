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
conversion step is outside this driver.

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
code edit: `{"schema_version": 1, "cutoff": "YYYY-MM-DD" (optional), "files":
[{"role", "path" (absolute), "sha256", "bytes" (optional)}]}`. Roles are
`native-har`, `account-activity-response`, `account-activity-request-proof` and
`har-intake-manifest`; the published path (`har/` or `capture/`) and the schema
contract are derived from the role. Every file is re-hashed; unknown keys or
roles, hash or size mismatch, unsafe or duplicate names, relative or linked paths
and a missing `native-har` fail closed. `--as-of-date` must equal a declared
cutoff. The legacy 2026-09-02 pairs remain the default when no flag is given.

`capture_quantbase_har.last_publishable_grid_date(captured, cutoff, anchor=...,
step_days=5, label=None)` returns the newest captured on-grid date at or before
the cutoff plus the grid-predicted date. Off-grid captured rows (provisional
latest points) are never publishable. It refuses a cutoff before the first grid
date, interior grid dates missing from the capture, and an off-grid cutoff or a
cutoff later than the newest captured grid date unless a non-empty `label` says so.

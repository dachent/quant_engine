# TuringTrader evidence disposition

Issue #197 preserves the existing TuringTrader web-contract work as rejected
evidence. It does not admit a provider route, provider return series, strategy
version, authenticated profile lane, or replacement for account-live
authority.

## Decision

The audited candidate is `REJECTED_NO_ACCEPTED_PROVIDER_ROUTE`. Its original
branch remains unmerged and must not be cherry-picked. The private captures
remain outside Git and are described only by the allowlisted aggregate metadata
in `contracts/providers/turingtrader/source-rejection.v1.json`.

The rejection is fail-closed:

- the commit is not self-contained because its tracked initializer depends on
  unrelated untracked issue #196 work;
- it admits an authenticated private-profile route that #197 excludes;
- five capture artifacts are online-only placeholders whose hashes are
  unavailable without an unapproved hydration effect;
- the local runbook and summary conflict with the repository candidate's
  capture-count narrative;
- the provenance anchor does not bind the tracked provider module, runbook, or
  summary;
- no authorized terms evidence or response-schema validation exists;
- route-template hashing loses the public strategy slug identity;
- real plain-text endpoints are routed through JSON parsing; and
- there is no governed per-capability coverage, observed freshness, or mapping
  to `tt-portfolio`.

All observed provider routes and derived outputs are therefore unaccepted. In
particular, redaction does not turn private profile data into an authorized
input.
The rejection manifest is a sanitized derivative constrained by
`schemas/providers/turingtrader-source-rejection.schema.json`; unknown fields,
including any source locator or private profile field, fail the offline
governance test.

## Portfolio boundary

The TT Portfolio strategy remains governed by its accepted account-live
evidence. Provider evidence is `UNAVAILABLE`, its provider version reference
stays null, and the named disposition is
`UNRESOLVED_NO_AUTHORITATIVE_MAPPING`. No provider label, route template, or
unverified capture is allowed to supply a strategy version or overwrite the
account-live series.

## Reproduction and recovery

The manifest is validated entirely offline from committed metadata. It does
not hydrate a capture, commit a source filename or absolute path, reproduce a
profile value, or inspect the unrelated issue #196 files. The restricted-output
Dead End is tracked separately in v2 issue #214; future inspection must be
schema-first and aggregate-or-hash-only.

Provider extraction, terms review, or capture hydration requires a separately
approved provider issue. Recovery is non-destructive: leave the rejected
branch, capture placeholders, and unrelated untracked files unchanged and do
not advance any provider, strategy, portfolio, or publication pointer.

## Capture instant and the live holdings fallbacks

The portfolio-holdings and comp pages are live at capture, and TuringTrader publishes a trading
day's data the next morning (the `last-update.txt` stamp, for example 2026-11-06T06:46Z, covers data
through the previous day). A capture for a Friday cutoff D is therefore taken on the weekend, and
live holdings do not change until the next regular session opens. A capture records the cutoff
state if it was taken before `capture_bound_utc(D)`: the next weekday after D at 13:30Z (the
earliest US open). For a Friday D that allows Saturday, Sunday and Monday before 13:30Z; for a
Tuesday D, Wednesday before 13:30Z. Holidays are ignored, so a holiday next weekday makes the true
open later than the bound: the bound is conservative-early, never late, and can refuse a capture
that would have been valid.

The consumer (`with_truncated_row_fallbacks` in
`tools/portfolio_implementation_reconciliation.py`) refuses the two `LIVE_*` fallbacks for a
truncated holdings row unless the captures that supply them, recorded in the manifest provenance
(`capture_provenance.captures[]` by `role`), are before that instant: the `json` capture for
`LIVE_PORTFOLIO_HOLDINGS`, the `json` and `txt` captures for `LIVE_HOLDINGS_OVERRIDE` (the text
capture holds the rebalance marker). A later market-vane capture is ignored, a missing capture
instant for a needed role refuses the fallback, and each applied live fallback records the
`capture_bound_utc` that was applied. Complete rows and the dated fallbacks (`LAST_COMPLETE_ROW`,
`NEXT_COMPLETE_ROW`) are not affected.

The manifest builder takes an optional `--cutoff D`: it refuses a capture, the comp last-modified
stamp or the market-vane capture at or after the same bound, records `cutoff` and
`capture_bound_utc` in the provenance, and `verify` re-checks it. Without the flag the builder is
unchanged. The bound rule is written once in each of the two modules and a test keeps them equal.

## Public object capture tool

`tools/tt_public_capture.py` is the governed replacement for the three hand
scripts that captured the TuringTrader public objects (authority: #283 comment
5515808323; #699 comment 6536613285). It only issues HTTP GET requests for
Firebase Storage objects and stores the raw response bytes unchanged.

One run captures three groups into subdirectories of a new output directory:
`json/` (portfolio-comp plus per slug performance-chart, performance-table,
holdings-history, portfolio-holdings), `txt/` (per slug last-update and
last-rebalance) and `vane/` (tt-market-vane status). The output directory must
not exist; the tool is create-only. `capture-manifest.json` is written last and
holds per-group capture times, the cutoff, the freshness result, each object
with status, bytes, sha256, ETag and Last-Modified (URLs carry no token), and a
sorted sha256 list.

Each of `json/`, `txt/` and `vane/` also holds its own `capture-manifest.json` in
the shape `tools/tt_source_manifest_builder.py` reads (one `captured_at_utc`
string plus that group's object rows). Point the builder at those
subdirectories: `json/` as its capture directory, `txt/` as `--txt-capture-dir`
and `vane/` as `--vane-capture-dir`. File names and bytes are unchanged.

The output directory must also lie outside every git repository (checked before
anything is created), because it receives the real provider bodies.

    python tools/tt_public_capture.py capture --out DIR --cutoff YYYY-MM-DD --tokens-file FILE

Tokens (public, allochist, holdings; 36-character UUIDs) are read from a JSON
object in `--tokens-file`, which must lie outside every git repository, or from
the environment variables TT_PUBLIC_TOKEN, TT_ALLOCHIST_TOKEN and
TT_HOLDINGS_TOKEN. They are never written, printed or recorded, and errors are
redacted. Today the operator extracts them from the retained 2026-09-02 driver
(`audit/coding/2026-09-02_kimi_w2-execution/_scratch/tt_live_capture.py` under
the OneDrive quant engine root) into a temporary tokens file outside the
repository, and deletes that file after the run.

Freshness for cutoff D: TuringTrader publishes each trading day's data the next
morning (about 06:50Z). Every performance-chart must end on D and every
last-update stamp must be at or after D+1 06:00Z. Otherwise the tool exits 3,
names each object with the found and required value, and still keeps the
capture with manifest status STALE. Exit codes: 0 complete and fresh, 1 an
object failed, 2 refusal or usage error, 3 stale, 4 partial groups.

`--groups` may select a subset, but an omitted group is never captured or
freshness-checked, so the manifest then has status PARTIAL_GROUPS with
`omitted_groups` listed, never COMPLETE, and the exit code is 4 unless
`--allow-partial` is given (the status stays PARTIAL_GROUPS and the exit code is
0). A failed object (1) or stale freshness (3) takes precedence.

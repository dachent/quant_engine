# Fidelity governed export intake

Extends `docs/providers/fidelity-static-history.md`; it does not replace it.
No Fidelity API route is evidenced, so there is no live adapter, no network
code, no login code, and no credential use. The owner signs in and exports
the file; the repository only requests, stages, normalizes, and verifies it.

Code: `quant_engine.fidelity.intake` (catalog, governance, request manifest,
raw staging, normalizer), `quant_engine.fidelity.intake_verify` (independent
verifier), and `tools/capture_fidelity_intake.py` (CLI, aggregate JSON only).
The normalizer reuses the static-history row parser unchanged; the verifier
imports neither it nor the intake module.

## Flow (all paths under a caller-supplied staging root outside Git)

1. `request` - needs owner governance evidence (JSON outside the repo): schema
   `fidelity-export-intake-governance-v1`, the terms and catalog digests, a
   GitHub issue-comment review reference, `owner_export` of
   `{"performed_by": "owner", "automation_credentials": "NONE"}`, a scope of
   exactly one accepted report type and a window that covers the request, and
   a current approval interval. Only its digest is bound into
   `<request_id>/request-manifest.json`, which tells the owner what to export
   and for which window.
2. The owner exports and hands over the files unedited.
3. `stage-raw` - validates the shape, then copies the bytes as received to
   `raw/accounts-history-<sha16>.csv` (exclusive create) and writes a
   hash-bound `raw-manifest.json`. A second call, a duplicate file, or a failed
   file leaves no partial state.
4. `normalize --crosswalk <private.json>` - re-hashes every raw file, parses it,
   requires every row inside the window, and writes
   `normalized/fidelity-intake-catalog.json` plus a manifest bound to the raw
   manifest. Output excludes account numbers, names, descriptions, and raw
   action text. Nothing is published or admitted.
5. `verify` - re-derives row count, amount sum, date range, account count, hash
   bindings, cross-file row repeats, stray files, and private-value leakage from
   the raw bytes, reporting reason codes only.

## Catalog

Accepted: `accounts-history-csv` (the 18-column activity export). Catalogued but
refused as unobserved: `positions-csv`, `balances-csv`, `statement-pdf`. Adding
one needs a redacted owner sample and a reviewed parser.

## Fails closed on

Unknown, missing, or reordered columns; wrong blank-row or footer shape; non-UTF-8
or oversize files; an unclassified action; an unmapped account; non-USD rows;
activity outside the window; the same file or the same row in two files; any
existing target (every write is create-only).

## UNVERIFIED

- Fidelity menu names, per-file row or date limits, and whether the export
  shape is unchanged after 2024-10-03; the observed shape is the static history.
- Whether the export proves completeness: contiguity is checked, completeness
  against the provider is not.
- Whether any Fidelity account is in the accepted census (it has none today), so
  a new generation (never an edit) is an owner decision, and the static
  generation's hardcoded boundary text and 405-row baseline would need a
  separate reviewed change before any admission.

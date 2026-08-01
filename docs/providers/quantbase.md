# Quantbase HAR-only integration

Quantbase is a Web Contract Harvester provider with `api_fallback=false`. The
repository contract never logs in, replays a session, or calls the provider.
Future captures are accepted only as offline HAR evidence from the reviewed
read routes below.

## Reviewed route families

The plugin in `quant_engine.web_contracts.quantbase` accepts authenticated
`GET` requests to the following `qbapi3.com` capabilities:

- user profile;
- dashboard data;
- linked accounts and account activity;
- fund statistics and rebalance history; and
- partner status.

Numeric user identifiers are matched structurally and never embedded in the
contract. Query-key sets are exact. The provider's `jwt` header is required for
API evidence. Next.js `funds`, `index`, `invest`, and `projections` JSON are
accepted from build-ID-scoped `app.getquantbase.com/_next/data/` routes without
claiming that the build ID is stable.

Preflight `OPTIONS`, the observed analytics `track-event` POST, and static web
assets are explicitly excluded from data extraction. Any other in-scope method,
API path, query shape, missing authentication, non-success response, or expired
session fails closed. No failure triggers another transport.

## Dated legacy baseline

The accepted legacy snapshot is the exact 55 CSV files captured through
2026-07-29. Direct CSV parsing proves 14,901 records. The earlier planning total
of 14,981 is stale; it is superseded, not silently edited, by the hash-bound
file inventory. The runbook is also stale in its account-activity distribution:
the final CSV has 810 rows, including 590 `order` and 158 `filled_txn` rows.

The producing extractor revision is unavailable and is recorded as
`LEGACY_UNKNOWN`. The new plugin is the forward contract, not invented proof
that it produced the old files. Coverage is complete only inside the exact
dated file boundary. It makes no post-2026-07-29 freshness claim and preserves
the inherited wins limitation as out-of-boundary evidence.

## Governed generation and cutover

All commands operate on local paths; private values remain outside Git.

```powershell
uv run --frozen --no-sync python tools/quantbase_generation.py plan `
  <legacy-quantbase-root> <dataQuantbase-root> `
  --as-of-date 2026-07-29 `
  --quarantine-plan-id <accepted-QB-SEC-plan-id>

uv run --frozen --no-sync python tools/quantbase_generation.py build <plan.json>
uv run --frozen --no-sync python tools/quantbase_generation.py stage <plan.json>
uv run --frozen --no-sync python tools/quantbase_generation.py publish `
  <plan.json> <accepted-generation-id>
uv run --frozen --no-sync python tools/quantbase_generation.py verify `
  <plan.json> <accepted-generation-id>
uv run --frozen --no-sync python tools/quantbase_generation.py cutover `
  <plan.json> <accepted-generation-id>
```

`stage` copies and hash-verifies every input under a non-authoritative source-run
staging directory and creates an immutable generation without changing
`CURRENT.json`. `publish` promotes that exact source run, rebuilds it, and
atomically advances `CURRENT.json`. `cutover` first proves the published pointer
and destination parity, then removes only the explicitly planned files and
empty legacy directories. The accepted source-run copy is the recovery source.

The normalized generation catalog contains only dataset names, families,
counts, schemas, hashes, coverage, and lineage. Private account, transaction,
holding, profile, and financial rows remain in the local immutable source run.

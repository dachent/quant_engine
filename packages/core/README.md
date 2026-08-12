# quant-engine-core

This package is the minimal operational envelope used by the first portfolio
baseline. It is deliberately provider-neutral and dependency-free.

It records:

- source artifact identifiers, SHA-256 hashes, byte counts, as-of dates, and
  schema identities;
- adapter name/version and an explicit coverage claim;
- an immutable generation ID derived from canonical manifest content; and
- whether a generation is staged or is the generation named by `CURRENT.json`.

The on-disk contract is:

```text
<configured-root>/
  generations/<generation-id>/generation.json
  CURRENT.json
```

`stage_generation` is create-only and idempotent only for identical bytes.
`publish_generation` atomically replaces `CURRENT.json` only for a verified
generation whose coverage is `COMPLETE_WITHIN_DECLARED_BOUNDARY`. A partial,
failed, pending, tampered, or missing generation fails closed and cannot change
the prior pointer. `read_current` binds the pointer to the exact manifest hash.

The package does not choose provider authority, move legacy data, normalize
portfolio facts, or implement a provider registry. Those capabilities require
a concrete consumer and their own governing issues.

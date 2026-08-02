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

Schema-v1 generation manifests remain immutable. Authority, freshness, and
revision metadata is attached through a hash-bound `GenerationEvidence`
sidecar, so an accepted generation never needs to be rewritten merely to adopt
the newer operating contract.

`AuthorityCatalog` defines exactly one rule by consumer, provider, internal
subject, fact type, and effective interval. `decide_admission` fails closed on
manifest hash mismatch, ambiguous authority, stale current evidence,
unauthorized evidence layers, finalization mismatch, or unapproved partial
gaps. A verified partial generation can supply an explicitly pinned fact slice
only when the rule names the permitted gaps and allows non-current evidence.
That does not upgrade the provider generation or another fact slice.

The package does not move legacy data, normalize portfolio facts, or choose a
business-specific authority. Concrete consumers supply governed rules and
retain reconciliation evidence.

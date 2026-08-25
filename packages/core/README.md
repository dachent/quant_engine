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
the prior pointer. Pointer schema v2 binds both the exact manifest and the full
regular-file inventory (portable path, SHA-256, and byte count) of the accepted
generation. `read_current` revalidates both before a consumer can use it;
schema-v1 pointers remain readable as legacy manifest-only evidence.

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

`prepare_release` and `commit_release` provide the multi-root coordination
boundary documented in [coordinated release publication](../../docs/release-publication.md).
Preparation records an immutable release identity and predecessor graph without
changing authority. Commit advances every named pointer and companion under one
lock; a failure restores the recorded predecessor graph and leaves a durable
recovery journal. The final commit point is a single `CURRENT_RELEASE.json`
authority pointer. `read_current_release` validates that pointer, its manifest,
the committed journal, every generation inventory, and every companion before
returning a coherent graph; it fails closed while roots are mixed.

Pipeline command receipts retain credential-safe argv fingerprints, cwd,
return code, stdout/stderr hashes, interpreter/tool identity, and pre/post hashes
for every declared read and write. Completed commands are not silently reused
when a declared output has drifted.

On Windows, the coordinated publication entry points (`commit_release`,
`recover_release`, and `rollback_release`) fail closed with
`WINDOWS_NAMESPACE_UNTRUSTED` before reading or mutating their roots. This does
not affect `prepare_release`, which stages immutable evidence only. The generic
single-root `publish_generation` primitive is outside this coordinated
hostile-writer guarantee. A future trusted Windows implementation must retain
stable directory identities and handles through the whole transaction; pathname
revalidation is insufficient.

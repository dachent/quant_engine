# Coordinated release publication

`CORE-PUB-002` adds a local transaction boundary around multiple accepted data
roots and their companion entry points. A release binds the exact code commit,
verification-gate evidence, proposed generation manifests, predecessor
pointers, and companion-file hashes into one content-addressed manifest.

Preparation is non-mutating. It validates every proposed generation and its
complete regular-file inventory, captures
the predecessor graph and companion recovery bytes, then writes immutable
release evidence with a `PREPARED` journal. Commit obtains one coordination
lock, rejects changed inputs or pointers, advances each target, publishes its
companions, and verifies the resulting graph. Only then does it atomically
replace `CURRENT_RELEASE.json`, the sole reader-visible coordinated authority,
and write the terminal `COMMITTED` journal. Readers use `read_current_release`;
if root pointers are temporarily mixed, validation fails closed rather than
representing a mixed graph as accepted.

If any commit step fails, the journal records the applied steps and restores
those steps in reverse order. A successful recovery ends at `ROLLED_BACK`; a
recovery failure ends at `RECOVERY_FAILED` and requires operator intervention.
The last accepted generations remain immutable in either case.

## On-disk contract

```text
<coordination-root>/
  CURRENT_RELEASE.json                 # one validated coordinated authority
  release.lock                         # exists only during a commit
  releases/<release-id>/
    release.json                       # immutable prepared identity
    journal.json                       # atomic state and applied-step record
    recovery/<companion-id>.bin        # exact predecessor companion bytes
```

The release manifest is defined by
`schemas/core/release-manifest.schema.json`; coordinated authority is defined
by `schemas/core/release-authority.schema.json`; the mutable recovery state is
defined by `schemas/core/release-journal.schema.json`. `CURRENT.json` remains
the local authority pointer inside each data root and its schema-v2 inventory
hash detects added, removed, or modified generation files during ordinary
consumption. Schema-v1 releases and pointers remain readable as legacy evidence.

## Required proof

A production rollforward must retain:

- the exact repository commit;
- hashes of every required gate result;
- the prepared release ID and predecessor graph;
- a `COMMITTED` journal;
- a validated `CURRENT_RELEASE.json` commit point;
- post-commit verification of every pointer and companion; and
- a later recovery rehearsal proving restoration of a complete predecessor
  graph after an injected mid-commit failure.

Repeated commit calls revalidate the complete terminal graph before returning.
Root IDs may not alias the same resolved location, and a coordination root may
not alias a target root. Atomic replace operations flush their file content and
fsync the containing directory where the operating system permits.

The control-panel publisher also compensates its local pointer and stable
launcher if launcher publication fails. The complete source-to-panel workflow
uses the coordinated release boundary so report, analytics, weekly-return, and
panel authorities cannot silently diverge.

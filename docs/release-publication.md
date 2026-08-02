# Coordinated release publication

`CORE-PUB-002` adds a local transaction boundary around multiple accepted data
roots and their companion entry points. A release binds the exact code commit,
verification-gate evidence, proposed generation manifests, predecessor
pointers, and companion-file hashes into one content-addressed manifest.

Preparation is non-mutating. It validates every proposed generation, captures
the predecessor graph and companion recovery bytes, then writes immutable
release evidence with a `PREPARED` journal. Commit obtains one coordination
lock, rejects changed inputs or pointers, advances each target, publishes its
companions, and verifies the resulting graph.

If any commit step fails, the journal records the applied steps and restores
those steps in reverse order. A successful recovery ends at `ROLLED_BACK`; a
recovery failure ends at `RECOVERY_FAILED` and requires operator intervention.
The last accepted generations remain immutable in either case.

## On-disk contract

```text
<coordination-root>/
  release.lock                         # exists only during a commit
  releases/<release-id>/
    release.json                       # immutable prepared identity
    journal.json                       # atomic state and applied-step record
    recovery/<companion-id>.bin        # exact predecessor companion bytes
```

The release manifest is defined by
`schemas/core/release-manifest.schema.json`; the mutable recovery state is
defined by `schemas/core/release-journal.schema.json`. `CURRENT.json` remains
the authority pointer inside each data root.

## Required proof

A production rollforward must retain:

- the exact repository commit;
- hashes of every required gate result;
- the prepared release ID and predecessor graph;
- a `COMMITTED` journal;
- post-commit verification of every pointer and companion; and
- a later recovery rehearsal proving restoration of a complete predecessor
  graph after an injected mid-commit failure.

The control-panel publisher also compensates its local pointer and stable
launcher if launcher publication fails. The complete source-to-panel workflow
uses the coordinated release boundary so report, analytics, weekly-return, and
panel authorities cannot silently diverge.

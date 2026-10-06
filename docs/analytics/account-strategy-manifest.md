# Account-strategy manifest

The account-strategy manifest is a source-bound accounting candidate. It is not
an independently verified publication or trading authority.

## Flow-completeness authority

`--flow-coverage-json` is required. It selects one immutable
`account-flow-coverage` artifact and binds that artifact to the exact
provider-ledger generation/artifact and exact classified-flow artifact used by
the producer. The manifest records the coverage artifact's `SourceArtifact`
identity alongside those same two input identities.

Coverage is schema version 2. It has exactly one row for each producer-selected
mapped assignment key, retaining a nullable assignment end separately from the
finite requested/covered receipt interval. The coverage artifact declares the
exact selected receipt IDs, SHA-256 values, and byte sizes. `COMPLETE` requires
`ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2` or `STATIC_SNAPSHOT_COVERAGE_V1`, one declared receipt, and null
blockers; that receipt must bind the exact account, full assignment interval,
provider/source identity, issuer contract, evidence artifacts, categories, and
terminal pagination. `BLOCKED` requires `NONE`, no receipt, and one closed
blocker/detail. The production retained issuer contract set is empty in this
task, so real receipts cannot become complete here.

For a funded mapped epoch, the producer determines its final available NAV
date, then requires the exact `COMPLETE` row for that mapped assignment.
Coverage rows never combine or extend to a neighboring subinterval. A different
assignment key, a partial interval, another account, a `BLOCKED` row, or a gap
fails closed.

When completeness is not proven, the epoch output has status
`BLOCKED_UNPROVABLE_OPENING_FLOWS` and blocker
`FLOW_COMPLETENESS_NOT_PROVEN`. No numeric metric, calculation, or observation
trace is retained. The existing full-assignment
perimeter remains intact: non-strategy intervals are never calculated or used
to bridge continuity.

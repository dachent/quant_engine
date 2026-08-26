# Account-strategy manifest

The account-strategy manifest is a source-bound accounting candidate. It is not
an independently verified publication or trading authority.

## Flow-completeness authority

`--flow-coverage-json` is required. It selects one immutable
`account-flow-coverage` artifact and binds that artifact to the exact
provider-ledger generation/artifact and exact classified-flow artifact used by
the producer. The manifest records the coverage artifact's `SourceArtifact`
identity alongside those same two input identities.

Every coverage row is for one synthetic-safe `account_id` and one closed,
inclusive date interval. `COMPLETE` is valid only with non-empty exact source
artifact IDs, `reason: null`, and the closed machine-receipt kind
`ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V1`. `BLOCKED` has a non-empty reason and
never provides absent-flow-as-zero authority. Provider-wide prose, unknown
properties, unknown authority labels, identity mismatches, malformed dates,
and overlapping same-account rows are rejected.

For a funded mapped epoch, the producer determines its final available NAV
date, then requires a single same-account `COMPLETE` coverage row that contains
the entire interval from the epoch's effective start through that final NAV
date. Adjacent rows do not combine; a classified flow on one date says nothing
about a neighboring date. A coverage end before the final NAV date, a start
after the epoch start, another account, a `BLOCKED` row, or a gap fails closed.

When completeness is not proven, the epoch output has status
`BLOCKED_UNPROVABLE_OPENING_FLOWS` and blocker
`FLOW_COMPLETENESS_NOT_PROVEN`. No numeric metric, calculation, or observation
trace is retained. The existing full-assignment
perimeter remains intact: non-strategy intervals are never calculated or used
to bridge continuity.

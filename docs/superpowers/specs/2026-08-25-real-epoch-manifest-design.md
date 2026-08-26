# Real Epoch Manifest Production Design

## Status and purpose

This design repairs the production disconnect reproduced by MVP-AUDIT-001: the retained real baseline cannot enter the epoch-manifest producer, the required real authority artifacts do not exist, and provider flow inventories are not equivalent to proven interval completeness. The outcome is one reproducible path from retained provider evidence to the financial panel, with every displayed amount and ratio independently checkable.

## Considered approaches

### 1. Explicit perimeter and completeness authorities — selected

Partition baseline assignments by their governed disposition, retain an explicit non-strategy coverage ledger for historical/reserve facts, and calculate only `STRATEGY_MAPPED` epochs. Require a new hash-bound flow-coverage authority for every calculated account/interval, in addition to the existing one-to-one classified-flow artifact. Build and retain all candidate inputs and the output manifest in one content-addressed generation.

This is the smallest approach that both supports the real baseline and prevents an absent flow row from silently becoming zero.

### 2. Treat provider `COMPLETE_WITHIN_DECLARED_BOUNDARY` as zero-flow proof — rejected

This would populate more rows quickly, but current boundaries are provider-level prose with heterogeneous fact vocabularies. Schwab does not even declare cash-flow coverage, Matrixport does not claim provider-lifetime retention, and baseline flow coverage is explicitly `PARTIAL`. The approach would convert broad metadata into unreviewed financial assertions.

### 3. Keep every real row unavailable and preserve synthetic-only production tests — rejected

This is fail-closed but does not deliver a functional financial MVP. It also leaves the producer incompatible with its own real baseline and allows synthetic tests to stand in for acceptance.

## Assignment perimeter

The exact baseline assignment set remains the source perimeter. The producer validates every assignment and partitions it without dropping rows:

- `STRATEGY_MAPPED` requires a non-empty known `strategy_id` and enters the calculable epoch chain.
- `HISTORICAL_POOL_ONLY` requires no strategy and enters the explicit non-strategy fact-coverage chain.
- `RESERVE_NOT_APPLICABLE` requires no strategy and enters the explicit non-strategy fact-coverage chain.
- any unknown disposition, mapped row without a strategy, or non-strategy row with a strategy fails before calculation.

Every NAV and classified-flow fact must map to exactly one assignment interval. Facts on non-strategy intervals are counted and bound into the output's perimeter evidence but never contribute to strategy money or return fields. The panel shows active/current mapped strategy epochs by default; historical mapped epochs remain available through the manifest, while reserve and historical-pool coverage appears as a nonnumeric coverage summary rather than misleading epoch performance rows.

## Flow authority

The classified-flow artifact continues to provide one exact disposition for every raw provider-ledger flow. A separate closed-schema `account-flow-coverage` artifact authorizes whether that inventory is complete for a specific account and inclusive date interval.

Each coverage row contains:

- `account_id`, `effective_start`, and `effective_end`;
- `status`: `COMPLETE` or `BLOCKED`;
- `source_artifact_ids`: non-empty for `COMPLETE`;
- `authority_kind`: a closed vocabulary describing the verified provider retrieval boundary;
- `reason`: null for `COMPLETE`, non-empty for `BLOCKED`.

The top-level artifact binds the exact provider-ledger identity and carries its own source artifact identity. Complete rows must be non-overlapping, must not extend beyond admitted provider evidence, and must cover the full calculated epoch through its final NAV date. A listed flow on one date never proves adjacent dates. A provider-level status string alone cannot generate a complete row.

Initial real-data coverage is generated only where retained machine-readable source receipts prove the retrieval boundary. Unprovable providers/accounts receive explicit `BLOCKED` rows. The design does not manufacture attestations from human-readable coverage prose.

## Admission and continuity authority

The admission policy covers the mapped assignment identity, while the full assignment identity remains separately bound as the perimeter. A `FUNDED` admission must cite a closed authority kind and exact source record identities proving positive funded NAV within the interval. A `BLOCKED` admission carries a reason and cannot emit numeric metrics.

`continuity_to_successor` is allowed only when two mapped epochs on the same account are contiguous and the predecessor closing NAV is the successor opening boundary. It is false across non-strategy intervals, gaps, missing closing NAV, or blocked flow coverage.

## Calculation and display

Money identities retain the existing definitions. Observation-linked return aggregates every classified flow in `(prior_nav_date, current_nav_date]`; it does not require consecutive calendar days and does not pretend sparse observations are daily performance. Leading nonpositive NAV observations are excluded only through an explicit funded-start rule; a nonpositive return base after funding blocks the epoch.

An epoch emits numeric money, RONC, XIRR diagnostic, observation-linked return, HWM, and MaxDD only when assignment, admission, NAV, flow classification, flow coverage, and continuity preconditions all pass. Otherwise every numeric metric is null and the exact blocker is preserved. The UI label must continue to distinguish RONC, diagnostic XIRR, and observation-linked return; none may be relabeled MWR or daily TWR.

## Durable generation and release lineage

The build command snapshots each selected input once and writes a content-addressed generation containing:

- provider-ledger identity;
- full baseline/perimeter identity;
- classified-flow artifact and classification policy;
- account-flow-coverage artifact;
- mapped-epoch admission policy;
- calculation-code identity;
- generated account-strategy manifest;
- independent verification report with exact per-cell comparisons.

Financial release construction accepts only that generation and rechecks every identity before staging. The same manifest bytes flow through release materialization, workbook proof, dashboard generation, current pointer, and browser rendering. Development mode may omit the manifest only when explicitly non-financial.

## Error handling and stop rules

- Never infer a mapping, flow, zero, funding threshold, calendar, or continuity rule.
- Unknown or incomplete provider boundaries remain `BLOCKED`.
- No partial numeric row may escape a blocked epoch.
- No private full account number enters Git, logs, reports, browser fixtures, or GitHub; public surfaces use provider nickname and approved last-four only.
- No provider, trading, pointer, or publication effect occurs during audit generation.
- A real-data build that cannot cover the full current strategy perimeter is an honest partial-data result, not MVP-2 acceptance.

## Verification

Tests must first fail against the current producer using a fixture that mirrors the real mixed mapped/historical/reserve baseline. Additional tests cover malformed partition states, total fact coverage, interval-completeness gaps, neighboring-date nonproof, non-observation-date flows, sparse accepted calendars, leading zero NAV, blocked metric withholding, durable generation identity, release mutation rejection, and exact browser cell parity.

The final audit independently reconstructs each real row without importing producer calculation helpers. Every numeric cell must match exactly before formatting and after browser rendering. A hostile reviewer must review each bounded tranche, and exact-head Quality, Security, Data boundary, and Issue guard contexts must succeed before readiness.

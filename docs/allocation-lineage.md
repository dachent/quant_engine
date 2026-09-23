# Account, pool, strategy, and allocation lineage

The canonical bridge separates four identities that were previously conflated:

1. broker/provider accounts;
2. systematic and reserve capital pools;
3. current strategies; and
4. effective-dated allocation decisions.

Every current account maps completely to one or more capital pools with weights
summing to 100%. A systematic pool maps to strategies; the reserve pool maps to
no strategy. `reconcile_allocation_bridge` proves account total = pool total =
systematic strategy total + reserve for one effective date.

The source-effective date, valuation date, and publication date are distinct.
The current audit allocation values are dated July 25, 2026; a July 30 baseline
publication does not convert them into July 30 observations.

## General intervals

`General` is not promoted to a strategy. It is an accepted systematic-pool
classification with one of two explicit strategy dispositions:

- `CURRENT_POOL_ONLY` — currently inside the systematic pool but not attributed
  to a strategy; or
- `HISTORICAL_POOL_ONLY` — a past interval whose strategy remains unknown.

Mapped intervals use `STRATEGY_MAPPED`. This preserves all eleven legacy
intervals without guessing. Pool assignment completeness is reported
separately from current and historical strategy-attribution coverage. The old
`assignment-mapping-partial` label is retired because it incorrectly combined
three different states.

The generic reconciliation schema is
`schemas/portfolio/allocation-bridge.schema.json`. Real account identities and
allocation records remain outside Git.

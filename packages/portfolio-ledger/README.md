# quant-engine-portfolio-ledger

This package is the smallest canonical fact model needed by the dated first
portfolio baseline. It preserves stable internal identities and immutable
source-record facts without importing provider semantics or performance rules.

It supports:

- investment and reserve accounts, current strategies, and securities;
- effective-dated account-to-strategy assignments;
- dated positions, transactions, external/internal cash-flow observations, and
  account NAV observations;
- explicit `AVAILABLE`, `UNAVAILABLE`, `MAPPED`, `UNMAPPED`, and
  `NOT_APPLICABLE` states;
- idempotent duplicate ingestion and rejection of conflicting record IDs;
- deterministic canonical JSON and a content hash; and
- account/strategy position-value summaries with explicit reconciliation
  exceptions.

All monetary and quantity values are finite `Decimal` instances. Currency codes
are canonical three-letter uppercase identifiers. A float is always rejected.
Real account numbers and provider records remain outside Git.

This package does not calculate returns, classify investment results, infer
missing values, acquire provider data, model candidates, optimize allocations,
or submit broker orders.

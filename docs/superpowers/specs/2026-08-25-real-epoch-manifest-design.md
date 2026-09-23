# Real Epoch Manifest Production Design

## Status and purpose

This document is the owner-authority reset of the real account-strategy epoch
manifest contract. It supersedes contrary retained-run outcomes in earlier
revisions of this design. It authorizes no provider access, publication,
pointer change, release, external write, merge, or trading effect.

GitHub issue #374 is the sole execution tracker. This Markdown document is
design guidance: it neither records execution status nor substitutes for
issue-recorded authority, evidence, blockers, or handoff.

The product outcome is one independently verified manifest containing exactly
13 current-product epochs through the last complete data refresh. Every number
supported by the pinned governed source bytes is populated. A row is blocked
only when an exact missing or ambiguous fact is identified and linked to its
source evidence.

## Product perimeter and owner dispositions

The selected baseline retains its full governed perimeter:

- 19 hash-bound baseline accounts;
- 18 assignment-bearing accounts and 26 assignment rows;
- 15 assignments whose baseline disposition is `STRATEGY_MAPPED`;
- 7 `HISTORICAL_POOL_ONLY` assignments; and
- 4 `RESERVE_NOT_APPLICABLE` assignments.

Those baseline counts are not the product-manifest row count. The product
manifest contains exactly 13 included epochs:

- 11 open current-product epochs; and
- the two closed TT predecessor epochs required to carry exact account capital
  into the two successor QSI epochs.

Two owner dispositions explain the difference between 15 baseline mapped
assignments and 13 included product epochs:

1. EchoTrade is `DEPRECATED_HISTORY_ONLY` effective `2025-11-25`. Its retained
   assignment and strategy history end on `2025-11-24`. Later Alpaca
   observations are general/unmapped evidence and never extend the EchoTrade
   assignment. EchoTrade is excluded from all 13 product epochs and every
   current performance, return, risk, decomposition, dependence,
   funded-product, HWM, blocker, capital/value, and consolidated-value result.
   Its `$0.19` remains historical evidence only.
2. Composer/Holy Grail is owner-excluded as immaterial. It is not a product
   epoch and does not block any product calculation or release outcome.

EchoTrade may appear only in separate, clearly labeled historical context, never
current or consolidated capital/value; Composer may appear only in separate
owner-exclusion or historical context. Neither may appear among the 13 product
epoch rows, in the product calculation denominator, or as a product blocker.

## Calculation cutoff and retained lineage

The immutable calculation cutoff is `2026-08-13`, the last complete data
refresh. It applies to producer inputs, independent recomputation, every row
end, every calculation trace, every derived scalar, and all downstream
rendering of the product manifest.

Source observations after the cutoff remain retained in the governed outer
source/perimeter evidence when required for lineage. They cannot enter opening
capital, invested or withdrawn capital, current value, gain, returns, HWM,
drawdown, XIRR, trace links, row blockers, or the 13-row subject set. An open
assignment ends at the cutoff for this generation. A closed assignment remains
bounded by its exact `effective_end`.

The provider-ledger and baseline source identities are pinned by exact path,
SHA-256, size, schema, and declared generation. `CURRENT.json`, wall clock,
filesystem timestamp, locale, and timezone are not calculation authority.

## One explicit hash-bound current-product crosswalk

The 13 human-readable account-to-assignment joins are confirmed owner input.
They are supplied as one explicit hash-bound current-product crosswalk. Program
logic must not contain or infer those joins.

Each crosswalk row binds:

- the internal account ID and provider used by the pinned source bytes;
- the exact assignment key, strategy ID, effective start, and effective end;
- the owner-approved account label; and
- a required `last_four` value containing exactly four approved digits or
  explicit `null` when no approved suffix exists.

The current-product crosswalk is the join authority; no alternate variants
exist. It may carry approved display fields. Any
crosswalk-byte change changes the generation identity. The composite assignment
key is `(account_id, strategy_id, effective_start, effective_end)`. Repeated
account IDs are required and valid for the two distinct, non-overlapping
TT-to-QSI epoch chains. Every row for one repeated account must use the same
provider, approved label, and `last_four`. A duplicate composite assignment
key, overlapping intervals for one account, inconsistent repeated-account
display fields, invalid dates or invalid suffix values, missing or extra product
assignments, unknown keys, and noncanonical input fail closed.

There are no alternate crosswalk variants. The one explicit hash-bound input
may contain internal account IDs, exact assignment keys, approved labels, and
masked suffixes. Full account or routing numbers, credentials or secrets, and
raw restricted values are excluded from that input and from logs, exceptions,
generated release evidence, issue text, and pull-request text. An internal ID
is never substituted as a human label.

## Source facts, classification, and row admission

Outer source identity, baseline perimeter, account classification, and fact
disposition remain global evidence. Calculation payloads are narrower: they
contain only cutoff-bounded facts assigned to one of the 13 owner-confirmed
epochs plus the exact reciprocal support required for an included internal
transfer.

Every retained NAV and flow fact receives an explicit disposition. Duplicate
raw record IDs and ambiguous same-account/date NAVs are global failures. A fact
with no selected-baseline account or no matching assignment remains retained
under a typed non-calculation disposition; it is not silently discarded or
allowed to poison an unrelated epoch.

Flow classification consumes the pinned classification policy exactly:

- policy sign rules determine external contribution/withdrawal and internal
  in/out direction without guessing or sign flipping;
- unavailable amounts, unmatched flow types, sign mismatches, ambiguous
  assignment ownership, and missing or ambiguous required facts remain exact
  blockers for the affected epoch only;
- an included internal transfer must bind its exact same-date,
  different-account, same-currency, opposite-amount raw counterpart; and
- the counterpart may be retained as governed support but does not become a
  second product calculation fact merely because it proves reciprocity.

Unrelated facts on excluded, historical, reserve, unmapped, or blocked accounts
remain visible in global evidence. They cannot alter an included row's money,
return, risk, HWM, or blocker state. A selected fact that is missing,
ambiguous, unavailable, or policy-invalid blocks its own row with exact record
IDs and a typed reason.

## Epoch calculation semantics

Each included row is calculated independently under exact decimal arithmetic:

1. Select AVAILABLE NAV observations within the assignment and cutoff bounds.
   Trim only a leading nonpositive pre-funding prefix. Any nonpositive NAV
   after the first positive funded observation is a row-specific blocker.
2. For the two adjacent TT-to-QSI chains, use the accepted predecessor closing
   NAV and date as the successor opening-capital and observation-link anchor.
   Capital neither appears nor disappears at the assignment boundary.
3. For every other epoch, use the first admitted positive NAV as opening
   capital only after the opening-flow and interval-completeness evidence is
   resolved. A flow on or before that opening observation cannot be silently
   absorbed, omitted, or double counted.
4. Include every classified flow in the exact assignment interval through the
   closing NAV. A flow after the last covering NAV, an unresolved opening flow,
   or an unproved required counterpart blocks that row.
5. Calculate and retain exact source IDs and traces for:
   `opening_capital`, external deposits, internal-in, `invested`, external
   withdrawals, internal-out, `withdrawn`, `net_capital`, `current_value`,
   `gain`, simple return on net capital, annualized simple return on net
   capital, observation-linked return, observation-linked HWM index, maximum
   drawdown, and diagnostic XIRR.

The money identities are exact:

```text
invested    = opening_capital + external_deposits + internal_in
withdrawn   = external_withdrawals + internal_out
net_capital = invested - withdrawn
gain        = current_value - net_capital
RONC        = gain / net_capital, when net_capital > 0
```

Observation links apply the documented date-only convention and aggregate
every signed flow in `(prior_nav_date, current_nav_date]`. The linked index,
running HWM, and maximum drawdown are derived from that same trace. XIRR uses
investor signs and actual days/365 and remains explicitly diagnostic.

No synthetic online-retrieval artifact is a universal admission prerequisite.
Source-flow and opening completeness must instead be resolved from specific
pinned evidence. The absence of an invented artifact cannot turn every row
into a blocker, but neither may missing evidence be treated as zero or inferred
away.

## Independent verification

The verifier is a separate calculation authority. It reads the same pinned
source bytes, current-product crosswalk, policies, and finalized semantic
payloads, but
does not import producer calculation helpers or reuse producer-derived scalar
or trace values as expected results.

It independently derives and verifies:

- the exact 13-row subject set, including 11 open and two closed TT predecessor
  epochs and the exclusion of EchoTrade and Composer;
- the exact cutoff on every selected NAV, flow, output end, and trace;
- every included source record ID and required reciprocal support ID;
- every row-specific blocker and the evidence that justifies it;
- every populated money, return, HWM, drawdown, and XIRR scalar;
- every capital-flow and observation-link trace; and
- exact canonical payload paths, hashes, sizes, source identities, and semantic
  digest.

Verification tests are success-then-mutate and non-tautological: a valid
independently recomputed candidate passes, and one material mutation at a time
to a source fact, subject row, scalar, trace, blocker, cutoff, or lineage value
must fail the corresponding independent invariant. A producer-generated value
is never the verifier's oracle.

## Immutable generation and downstream byte lineage

All selected inputs are captured once as immutable bytes before parsing.
Strict JSON rejects duplicate keys, invalid numbers, malformed UTF-8, trailing
content, noncanonical generated JSON, and ambiguous paths. The generation is
create-only and content addressed. Its manifest, detached inventory, verification
report, and release controls bind exact path, SHA-256, size, and semantic
identity without circular self-inclusion.

Exact-byte identity applies to JSON-bearing seams: the generated manifest, the
independent verification subject/report, the financial release JSON, and the
panel/browser input JSON bind the same manifest path, SHA-256, size, generation,
and semantic identity. A one-byte manifest or JSON-lineage mutation fails
before publication or pointer movement.

Report and workbook artifacts and the rendered browser are not required to
physically embed the manifest JSON bytes. They must bind the exact verified
JSON input identity and prove row-by-row and cell-by-cell parity with its
values, statuses, blockers, labels, and lineage. Formatting may derive display
strings from the verified values, but no layer may recompute, substitute,
cache, omit, or relabel a different financial value.

EchoTrade and Composer context is rendered from separate historical evidence;
EchoTrade's `$0.19` may appear only as clearly labeled historical evidence,
never current or consolidated capital/value. Neither strategy may be injected
into the verified 13-row manifest, its numeric denominator, or its blocker
counts.

## Deployment model and proportional security

The operating context is an isolated corporate environment with one or two
trusted operators and static publication of results. Required controls are
therefore proportional and concrete:

- financial and data integrity;
- immutable source and exact-byte lineage;
- strict, fail-closed parsing and deterministic create-only generation;
- independent verification and exact-head release controls;
- no leakage of secrets, full account numbers, raw provider data, or other
  restricted financial values; and
- no unintended provider, publication, pointer, trading, or external-write
  effects.

The design does not invent multi-tenant, hostile-network-service, privilege-
escalation, denial-of-service, or high-availability requirements for a product
that has none of those deployment properties. Existing relevant integrity and
privacy regressions remain required; generic hardening work is not an
acceptance substitute.

## Acceptance contract

The real epoch-manifest outcome is accepted only when all of the following are
true for one unchanged exact candidate:

1. owner authority, the explicit current-product crosswalk, and all pinned
   source identities are exact and hash-bound;
2. the product manifest has exactly 13 rows: 11 open plus the two closed TT
   predecessors required for QSI continuity;
3. every supported number is populated and every absent number has one
   specific evidenced blocker rather than a universal proxy;
4. independent verification recomputes every populated scalar and trace and
   validates every blocker, source ID, support ID, subject row, and cutoff;
5. exact manifest JSON bytes remain identical at every JSON-bearing seam, while
   report, workbook, dashboard, and browser outputs bind that JSON identity and
   prove rendered-cell parity; and
6. bounded regressions, deployment-relevant hostile review, current required
   CI, and exact-head issue/pull-request handoff pass without merge.

Any earlier outcome that treats all 15 baseline mapped assignments as product
rows, requires every mapped row to be blocked, prohibits all numeric
population, uses a missing synthetic retrieval artifact as universal failure,
extends calculations beyond the cutoff, or requires two crosswalks is rejected
by this contract reset.

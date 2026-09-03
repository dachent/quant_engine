# GrizzlyBulls evidence disposition

Issue #195 preserves the existing GrizzlyBulls web-contract work as rejected
evidence. It does not admit a provider route, provider return series, strategy
version, or replacement for account-live authority.

## Decision

The audited candidate is `REJECTED_NO_ACCEPTED_PROVIDER_ROUTE`. Its original
branch remains unmerged. The private source remains outside Git and is bound by
the aggregate inventory and artifact hashes in
`contracts/providers/grizzlybulls/source-rejection.v1.json`.
The committed `inventory_sha256` is reproducible from each sanitized
`artifact_id`, byte count, and artifact SHA-256. The separate
`private_source_inventory_sha256` preserves the original locator-based
outside-Git evidence anchor without disclosing those private locators.

The rejection is fail-closed:

- one capture contains session-bearing requests and non-unique provider routes;
- the session-free capture contains one non-success response that the candidate
  silently skips while returning a passing result;
- mutation-like traffic, cookies, unexpected authorization material, missing
  bodies, schema drift, and duplicate identities are not all rejected by the
  candidate;
- the branch uses placeholder provenance anchors;
- no authorized terms evidence exists; and
- no authoritative mapping connects this provider evidence to
  `grizzlybears-vtma`.

All observed routes and derived outputs are therefore unaccepted. The existing
tests describe candidate behavior, but their passing result is not acceptance
evidence.

## Portfolio boundary

GrizzlyBears VTMA remains governed by its accepted account-live evidence. The
provider evidence state is `UNAVAILABLE`, its provider version reference stays
null, and the named disposition is
`UNRESOLVED_NO_AUTHORITATIVE_MAPPING`. No value is inferred from the provider
label, and no provider series is substituted for account NAV. This evidence
cannot make the HWM freshness gate pass.

## Reproduction and recovery

The manifest can be validated entirely offline from committed metadata. It
commits no source filename, absolute path, response body, session material,
account identifier, or financial value. Reproducing any provider extraction or
reviewing terms would require a separately approved provider issue.

Recovery is non-destructive: leave the rejected branch and private source
artifacts unchanged and do not advance any provider, strategy, portfolio, or
publication pointer.

## Successor work

Issue #240 (WCH-GB-002) is the open successor. It builds a refreshed,
fail-closed GrizzlyBulls HAR/web capture contract
(`packages/web-contracts/src/quant_engine/web_contracts/grizzlybulls.py`) that
fixes the blocking findings above: session-bearing and mutation-like routes are
rejected fail-closed at the contract level, non-success and empty responses
fail closed, every captured response is schema-validated, and the strategy
mapping to `grizzlybears-vtma` is an explicit authoritative table. The two
histories (all-history vs live-only) are separately identified series, and
replay re-derives identical output hashes from persisted raw bytes without
network. This document and `source-rejection.v1.json` remain the governing
record of the #195 rejection until the live capture passes under #240's
issue-recorded PROCEED and supersession goes through review.

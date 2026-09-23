# Security policy

`quant_engine` handles investment data and will eventually reach provider and
broker surfaces. Security is therefore a product boundary, not an optional
deployment concern. The repository is credential-free, contains no real
account data, and grants no trading authority.

The normative handling rules are in [the data policy](docs/data-policy.md).
The machine-enforced repository defaults are in
[the data-boundary policy](.policy/data-boundary.toml).

The sensitivity vocabulary is exactly `PUBLIC`, `INTERNAL`,
`RESTRICTED_FINANCIAL`, and `SECRET_AUTH`. `PUBLIC` and approved `INTERNAL`
content are the only classes eligible for Git; the latter two remain in
restricted local stores under their governing issues.

## Repository security boundary

Only `PUBLIC` and approved `INTERNAL` material may be committed. A fixture with
the `SYNTHETIC` provenance marker may be committed only when it is wholly
invented and its normalized path, SHA-256, byte count, purpose, and governing
issue exactly match the machine allowlist. `SYNTHETIC` is not a sensitivity
class. Redaction does not make production data synthetic.

Never put any of the following in source, history, pull requests, GitHub Issue
text, CI inputs or output, test snapshots, logs, screenshots, or generated
artifacts:

- passwords, API keys, tokens, private keys, authorization headers, cookies,
  browser storage, or authenticated HAR/session material;
- real external account identifiers or the local account crosswalk;
- real transactions, holdings, balances, NAV, allocations, statements,
  provider exports, broker reports, databases, or research datasets; or
- credential fallbacks, credential-bearing/raw discovered endpoint inventories,
  or real provider responses. Terms-approved, bounded, sanitized route
  contracts are permitted only under the endpoint policy.

Repository checks are defense in depth. A passing scanner does not reclassify
an artifact or authorize its use. Git LFS pointers cannot bypass inspection.
The 5,242,880-byte tracked-file ceiling is an absolute repository boundary for
this policy version.

## Credential and session rules

- Persist secrets only in an issue-approved operating-system credential store.
  Configuration contains a credential reference, never the value.
- Resolve a credential only at runtime for the approved provider, capability,
  and issue. Keep it in memory for the shortest practical period and never
  include it in an exception, trace, command line, child-process argument,
  cache, or diagnostic bundle.
- Literal defaults and fallback secrets are prohibited. Missing or inaccessible
  credentials fail closed.
- Cookies, authenticated HAR files, browser profiles, local storage, and Flex
  tokens are `SECRET_AUTH`. They remain outside Git and CI in a restricted
  local quarantine until an authorized provider issue disposes of them.
- Credential creation, rotation, revocation, session invalidation, or deletion
  is an external effect. It requires a current governing issue and an explicit
  approval naming the exact credential or session class and recovery plan.

## Provider and broker authority

HTTP method does not determine safety. A `GET` that creates a quote, order,
cancellation, transfer, reservation, download charge, or other provider-side
state is not read-only. An endpoint is denied unless a provider-specific record
has every required field, declares `READ_ONLY`, has current `APPROVED` terms,
uses no forbidden credential class, and is authorized by the governing issue.

Metered reads, provider mutations, destructive actions, PAPER orders, and live
orders require separate, current approvals. Live trading is outside the initial
program. An unknown broker submission outcome halts all retry until
reconciliation proves the broker state; a compensating order is a new governed
action, not rollback.

## Reporting a suspected exposure

1. Stop the operation. Do not paste, move, open, print, or re-encode the
   suspected value or artifact to demonstrate it.
2. Preserve only sanitized metadata needed to locate the evidence locally:
   component, path class, time, suspected sensitivity, and governing issue.
3. Create or update a sanitized GitHub Issue without secret or financial
   content. If even the metadata is sensitive, ask the repository owner through
   an approved out-of-band channel to make the issue entry.
4. Keep the affected material in a restricted location outside the repository.
   Do not delete evidence or rotate/revoke anything until the response issue
   authorizes that exact effect.
5. Treat the material and its descendants at the highest plausible sensitivity
   until credential rotation/session invalidation, lineage review, repository
   and artifact scans, provider reconciliation, and release evidence are
   complete.

Do not rewrite Git history or force-push as an improvised incident response.
Containment and any history remediation require an approved recovery plan.

## Security review expectations

Every security-sensitive change must prove the governing issue is ready, name
its data and external-effect boundaries, add stable negative tests, and retain
a recovery state. Reviews must reject broad fixture exemptions, method-only
allowlists, embedded credential fallbacks, real-data test cases, weakened
failure behavior, or code that makes live behavior reachable by default.

# TuringTrader evidence disposition

Issue #197 preserves the existing TuringTrader web-contract work as rejected
evidence. It does not admit a provider route, provider return series, strategy
version, authenticated profile lane, or replacement for account-live
authority.

## Decision

The audited candidate is `REJECTED_NO_ACCEPTED_PROVIDER_ROUTE`. Its original
branch remains unmerged and must not be cherry-picked. The private captures
remain outside Git and are described only by the allowlisted aggregate metadata
in `contracts/providers/turingtrader/source-rejection.v1.json`.

The rejection is fail-closed:

- the commit is not self-contained because its tracked initializer depends on
  unrelated untracked issue #196 work;
- it admits an authenticated private-profile route that #197 excludes;
- five capture artifacts are online-only placeholders whose hashes are
  unavailable without an unapproved hydration effect;
- the local runbook and summary conflict with the repository candidate's
  capture-count narrative;
- the provenance anchor does not bind the tracked provider module, runbook, or
  summary;
- no authorized terms evidence or response-schema validation exists;
- route-template hashing loses the public strategy slug identity;
- real plain-text endpoints are routed through JSON parsing; and
- there is no governed per-capability coverage, observed freshness, or mapping
  to `tt-portfolio`.

All observed provider routes and derived outputs are therefore unaccepted. In
particular, redaction does not turn private profile data into an authorized
input.

## Portfolio boundary

The TT Portfolio strategy remains governed by its accepted account-live
evidence. Provider evidence is `UNAVAILABLE`, its provider version reference
stays null, and the named disposition is
`UNRESOLVED_NO_AUTHORITATIVE_MAPPING`. No provider label, route template, or
unverified capture is allowed to supply a strategy version or overwrite the
account-live series.

## Reproduction and recovery

The manifest is validated entirely offline from committed metadata. It does
not hydrate a capture, commit a source filename or absolute path, reproduce a
profile value, or inspect the unrelated issue #196 files. The restricted-output
Dead End is tracked separately in v2 issue #214; future inspection must be
schema-first and aggregate-or-hash-only.

Provider extraction, terms review, or capture hydration requires a separately
approved provider issue. Recovery is non-destructive: leave the rejected
branch, capture placeholders, and unrelated untracked files unchanged and do
not advance any provider, strategy, portfolio, or publication pointer.

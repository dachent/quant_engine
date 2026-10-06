# Data authority and admission contract

This contract separates immutable provider evidence from a consumer's decision
to use one fact. It implements the shared authority boundary described in the
system architecture without rewriting schema-v1 generation manifests.

## Required identities

Every offered fact identifies:

- provider and stable internal subject;
- fact type and effective interval;
- evidence layer and finalization state;
- fact-level coverage and named gaps;
- exact generation and manifest SHA-256;
- acquisition time, provider revision, adapter Git commit, revision policy,
  publication disposition, and superseded generation where applicable.

Every consumer rule identifies one authority for the same key and interval,
the admitted evidence layers, finalization requirement, freshness policy, and
whether a pinned partial slice is allowed. Overlapping rules for the same
consumer and key are invalid.

## Evidence layers

`ACCOUNT_LIVE`, `PROVIDER_LIVE`, `OFFICIAL_VEHICLE`, `PREDECESSOR`,
`SHADOW_OOS`, `BACKTEST`, `STATIC_HISTORY`, and `CONCEPTUAL_STRESS` preserve
different economic meanings. Admission never converts one layer into another.
In particular, provider product NAV is not account NAV unless separately
reconciled holdings, flows, FX, and account identity produce an account-live
fact.

## Partial generations

A provider-level `PARTIAL` generation remains non-current. A consumer may use a
fact slice from it only when all of the following are true:

1. the generation and manifest hash are explicitly pinned;
2. the fact slice declares its own `PARTIAL` coverage and gap codes;
3. the authority rule enables both partial and non-current evidence;
4. every declared gap is in the rule's allowlist; and
5. the evidence layer, finalization, and freshness checks pass.

That decision is scoped to one consumer/fact/interval and never publishes the
whole generation or upgrades another fact.

## Freshness

Recurring facts define an expected interval, optional grace, and stale-after
boundary. The deterministic states are `FRESH`, `LATE`, `STALE`, and
`UNAVAILABLE`. `STALE` and `UNAVAILABLE` fail admission for current facts.

Finite sources use `FINITE_COMPLETE`; they do not become stale simply because
their historical interval is old. Fidelity's bounded static history is the
canonical example. A later source is a new generation or revision, not an
in-place edit.

`FreshnessPolicy.assess` records the last accepted date, expected next date,
evaluation date, acquisition timestamp, original/revised state, human-readable
reason, escalation flag, and whether current consumers must block. Local alert
hooks return deterministic warning/error records without sending an external
notification. The current freshness bundle is version 4, dated at the data
cutoff with the operational date carried as `evaluated_on`; it is governed by
the closed `schemas/core/freshness-assessment.schema.json`, which a test
validates the producer output against so the two cannot drift. It includes the
provider-ledger identity and is also structurally validated with closed keys,
types, dates, hashes, sizes, and the selected current provider-ledger catalog
and manifest before its rows appear in the Data view. Both the panel build and
coordinated publication recompute the assessment rather than trusting stored
state labels. `last_accepted_date` always names a day on or before the cutoff
on which an observation is proven: a declared capture interval that starts on or
before the cutoff and ends after it is accepted through the cutoff, and a source
that starts after the cutoff, or a strategy lane known only by its first and
last row dates and ending after the cutoff, has no accepted date and is
`UNAVAILABLE`.

## Failure and recovery

Authority catalogs and sidecars are deterministic, closed-schema artifacts.
Invalid or ambiguous rules fail before publication. Existing generations and
`CURRENT.json` pointers remain unchanged. Recovery restores the prior catalog
or code through a governed release; provider source bytes are never edited.

Schemas:

- `schemas/core/fact-authority-catalog.schema.json`
- `schemas/core/generation-evidence.schema.json`
- `schemas/core/freshness-assessment.schema.json`
- `schemas/portfolio/provider-ledger.schema.json`

The seven-provider portfolio admission and reconciliation implementation is
documented in `docs/provider-ledger.md`.

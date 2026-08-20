# Canonical provider ledger

`tools/provider_ledger.py` is the only provider-to-portfolio admission path. It
does not call providers, brokers, browsers, credential stores, or the network.
It reads the seven exact local generations and portfolio evidence named by a
private, hash-bound specification, then stages a deterministic generation
without advancing a current pointer.

## Authority boundary

Every admitted fact retains provider, generation, account or strategy scope,
effective interval, evidence layer, disposition, and upstream coverage. Only
`ACCOUNT_LIVE` facts with accepted mappings can be `AUTHORITATIVE` for account
capital or performance. Unmapped facts are retained as `REFERENCE_ONLY` quality
findings.

Catalog schema 2 adds an exhaustive admit-or-declare gate before the candidate
can be staged. The gate discovers every manifested source artifact, manifest
coverage fact type, provider-catalog collection, and #330 product data class.
Each discovered key has exactly one `ADMIT` or `EXCLUDE` disposition in
`admit-or-declare-receipt.json`; missing, duplicate, extra, or hash-mismatched
dispositions fail. The receipt binds the exact financial-MVP contract and #302
census digests, all seven provider generation/manifest/catalog identities,
source-segment identities, transform versions, materialized fact hashes and
intervals, units, currency, timezone, calendar, consumers, readiness impact,
and invalidated outputs. It contains no account identifiers, source roots, or
financial values.

The #330 source segment pins the complete upstream inventory, product data-class
set, and admitted binding set. An admitted binding includes its exact sorted
target facts, evidence layer, transform, and transform version; an artifact that
feeds several fact collections cannot be collapsed to a generic artifact fact.
Every exclusion is authorized by the #303 policy ID, owner, version, effective
interval, permitted reason, readiness effect, and invalidated-output rule.
Unknown or changed source items are not automatically excluded: they fail until
the authoritative source policy is deliberately updated and revalidated.

An exclusion is not a readiness waiver. A required data class without admitted
materialized facts remains `REQUIRED_NOT_READY` and names every invalidated
output. Provider-declared gaps are source evidence, not product authority.
Deprecated or no-current-member lanes can be explicitly excluded with no
current product consumer; their historical observations remain evidence and
cannot become current account authority.

Strategy curves retain their declared economic layer. In particular, the
Matrixport Robuxio product path is `PROVIDER_LIVE` and
`RECONCILIATION_ONLY`; it provides complete strategy NAV/HWM evidence but does
not become account-live performance. Portfolio123 provider returns are also
retained separately from IBKR account-live returns. The report and control
panel may display both, but neither may rewrite the other.

Each strategy evidence lane carries its own `provider_id`, `generation_id`,
and generation as-of date. A top-level account-live identity therefore cannot
relabel a Portfolio123 provider-live path as IBKR. Account-derived returns that
overlap inherited accepted history are compared by exact strategy/date. The
inherited path remains authority, while counts, tolerance, maximum absolute
difference, and any exception are retained in `reconciliation_checks`; an
overlapping broker path is never silently promoted or discarded without this
evidence.

Provider qualifications are explicit:

- `PUBLISHED_COMPLETE` for a published provider generation within its boundary;
- `ACCEPTED_PARTIAL` for a pinned, policy-accepted partial generation; and
- `FINITE_STATIC` for a complete bounded historical source.

## Build and verification

The private spec contains absolute local roots and hashes and therefore remains
outside Git. Specification schema 2 can pin the contract, schema, and registry
identities explicitly. A legacy schema-1 spec is upgraded in memory only by
hash-binding those three files from the executing repository; generated output
is always catalog schema 2 and adapter version 2. From the repository root:

```powershell
uv run --frozen --no-sync python tools/provider_ledger.py stage `
  --spec <private-provider-ledger-spec.json>

uv run --frozen --no-sync python tools/provider_ledger.py verify `
  --spec <private-provider-ledger-spec.json> `
  --generation-id <staged-generation-id>
```

`--output-root <candidate-root>` may be supplied to both commands for isolated,
create-only workstation evidence. It never changes the spec or advances a
pointer.

The output catalog conforms to
`schemas/portfolio/provider-ledger.schema.json`; the sibling receipt conforms
to `schemas/portfolio/provider-admission-receipt.schema.json`. Both artifacts
are hash-bound by the one existing provider-ledger `GenerationManifest`.
Receipt semantic validation also requires the independently loaded accepted
product/census identity, source segments, and seven trusted provider-generation
rows; receipt fields cannot authenticate themselves through coherent rehashing.
Rebuilding identical inputs must reproduce the same generation and bytes. The
tool has no publish command; `DATA-PIPE-001` owns coordinated release of
provider ledger, baseline, report, analytics, and panel pointers.

## Downstream behavior

Portfolio baseline specification version 3 pins the exact provider-ledger
generation, manifest hash, and catalog hash. It overlays only admitted mapped
facts, retains legacy observations for reconciliation, and records unmapped or
conflicting evidence instead of inferring it. The report carries admissions,
provider coverage, strategy evidence, and layered returns. The control panel
uses that report as its sole provider-evidence authority; an optional legacy
evidence file is validation-only and must be byte-equivalent.

On any hash, schema, mapping, overlap, layer, date, or replay failure, the new
candidate fails and existing authority pointers remain unchanged.

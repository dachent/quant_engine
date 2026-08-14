# hwm_inputs envelope contract

The `hwm_inputs` envelope is an OPTIONAL, closed, internally-versioned
section of the baseline catalog (catalog schema v1).  It encodes the
pre-computed HWM input period table — the single authority for capital,
returns, calendar, and flow/FX admission across every (strategy, date) in
the expected keyspace.  When absent the catalog omits the key entirely;
every existing consumer tolerates absence and any internal defect, and a
no-envelope spec produces a byte-identical catalog and manifest to the
pre-envelope builder (golden regression in
`tests/portfolio_baseline/test_portfolio_baseline.py`).

## Relationship to #277

The `hwm_inputs` envelope is the **input contract** consumed by the HWM
index engine (issue #277).  The builder populates and validates it; the
consumers read only from `catalog["hwm_inputs"]["periods"]`.

The #277 consumers are implemented (branch `feat/hwm-001-composite`):

- `tools/portfolio_report.py` (`_hwm_numerical_from_envelope`, called from
  `build_report_data`) computes the composite daily systematic return
  series from the envelope's AVAILABLE periods — per date, the
  beginning-capital-weighted member return with returns resolved through
  the hash-bound `strategy_returns` crosswalk — and hands it to the ledger
  kernel (`compute_full_systematic_hwm_index`).  The numerical block
  (`hwm_numerical_status`, `hwm_numerical_reason`, and when AVAILABLE
  `hwm_index` / `hwm_current_drawdown` / `hwm_maximum_drawdown`) publishes
  on the report's `hwm_summary` whether or not a freshness assessment is
  supplied.  An absent envelope yields `UNAVAILABLE`/`NO_HWM_INPUTS`; any
  internal defect, an all-UNAVAILABLE required day, a duplicate or
  mismatched return reference, or an unresolved reference yields
  `UNAVAILABLE` with a structured reason — never an error and never a
  fallback.  An in-scope perimeter with numerical status UNAVAILABLE gates
  the report's advisory readiness to `DO_NOT_EXPAND_SYSTEMATIC_RISK` (the
  gate only tightens the replayed control action).
- `tools/portfolio_control_panel.py` reconciles the report's claims against
  the governed baseline generation named by the report's baseline
  reference (manifest and catalog hash-verified through the core
  publication machinery), validates the catalog's envelope against the full
  contract above, recomputes the composite from the CATALOG, and requires
  the report's carried copies (`hwm_inputs`, `strategy_returns`, perimeter,
  as-of) to equal the catalog exactly.  The report's envelope remains a
  display view; authority for reconciliation is the catalog.  Any
  divergence, a missing baseline root, or an unresolvable generation fails
  the panel build closed.

## Envelope structure

### `contract_version`

Integer constant `1`.  Bumped only when the envelope shape makes a
breaking change.

### `anchor`

| Field | Meaning |
|---|---|
| `anchor_date` | Date ≤ catalog `as_of_date`; lower bound of the expected keyspace. |
| `index_base_value` | Exactly the string `"1.0"`. |
| `hwm_policy_id` | Exactly `"PORT-HWM-3"`. |
| `acceptance_provenance` | Must resolve to a typed, hash-bound catalog `source_artifact`. |

`acceptance_provenance` fields: `accepted_at` (a real ISO-8601 datetime
parsed with `datetime.fromisoformat` semantics, `Z` accepted; a timezone
offset is required and impossible values such as `2026-99-99T25:61:61Z` are
rejected), `accepted_by`,
`source_artifact_id`, `source_artifact_sha256`.  The referenced artifact
must BE the admitted `hwm-anchor-acceptance` governed spec source (the only
role the builder can open and hash-verify), must appear in the catalog's
FINAL `source_artifacts` set (core, governed HWM, and provider artifacts —
validation runs only after provider artifacts are admitted), and its hash
must match `source_artifact_sha256`.  An observation artifact (e.g.
`alpaca-events`) is never a valid acceptance authority.

**Anchor content cross-check:** the shared validator parses the admitted
acceptance record (when the admission builder supplies it via the
`acceptance_record` parameter) and requires exact equality of
`anchor_date`, `index_base_value`, and `hwm_policy_id` between the
envelope and the record.  Consumers that cannot access the governed source
files omit this parameter; the cross-check is admission-only.  The
expected keyspace is derived from the anchor only after this cross-check,
so a fabricated later anchor cannot truncate the keyspace.

### `calendar_authorities`

One row per **lane** (`ACCOUNT`, `PROVIDER`, `LEGACY`).  Each row declares
the ADMITTED expectation source — never derived from observations.  Every
lane used by any `periods` row must have exactly one admitted authority
(duplicate lanes are rejected; a period on a lane with no authority fails
closed).

| Field | Meaning |
|---|---|
| `lane` | One of `ACCOUNT`, `PROVIDER`, `LEGACY`. |
| `authority_id` | Unique identifier for this calendar authority. |
| `authority_version` | Version string for the authority. |
| `calendar_kind` | `EXCHANGE`, `ALWAYS_ON`, or `PROVIDER_PUBLICATION`. |
| `admitted_source_artifact_id` | Must resolve to a final-set artifact whose schema role is `hwm-calendar-authority-{lane}` (`account`/`provider`/`legacy` lowercased). |

### `periods`

One row per `(strategy_id, date)` in the expected keyspace — no more, no
fewer.  The expected keyspace is derived by the builder from the GOVERNED
PERIMETER — every strategy with a systematic allocation in the catalog,
classified included/excluded by the admitted PORT-HWM-3 policy — over every
calendar date from `anchor_date` through `as_of_date`.  Perimeter
membership never depends on account assignments, so an allocated but
unmapped (e.g. provider-only) sleeve cannot be silently omitted; provider
lane membership is derived from the policy/admissions path and such sleeves
appear as explicit rows.  Assignments are used only to validate
account-lane evidence.  The envelope's period keys must EQUAL the expected
set; omission cannot masquerade as exclusion.

| Field | Meaning |
|---|---|
| `strategy_id` | Must be a known strategy in the catalog. |
| `date` | ≤ catalog `as_of_date`. |
| `lane` | `ACCOUNT`, `PROVIDER`, or `LEGACY`; the lane must have an admitted calendar authority. |
| `disposition` | `AVAILABLE`, `UNAVAILABLE`, `NOT_ADMITTED`, `EXPECTED_NON_TRADING`, or `EXCLUDED_BY_POLICY`. |
| `beginning_capital_usd` | Decimal string iff `AVAILABLE`; `null` for EVERY other disposition (including `UNAVAILABLE`). |
| `return_source_record_id` | String iff `AVAILABLE`; `null` for every other disposition. |
| `component_account_ids` | Array of known account ids; non-empty and required for `ACCOUNT`-lane `AVAILABLE`. |
| `flow_status` | `OBSERVED`, `CONFIRMED_ZERO`, or `MISSING`. |
| `fx_status` | `OBSERVED`, `CONFIRMED_ONE`, `NOT_REQUIRED`, or `MISSING`. |
| `flow_source_record_ids` | Array; `CONFIRMED_ZERO` requires a non-empty resolved attestation (below). |
| `fx_source_record_ids` | Array; `CONFIRMED_ONE` requires a non-empty resolved attestation (below). |

**Disposition rules (fail-closed):**

- `AVAILABLE` requires non-null `beginning_capital_usd`, non-null
  `return_source_record_id`, `flow_status != MISSING`, and
  `fx_status != MISSING`.
- `UNAVAILABLE`, `NOT_ADMITTED`, `EXPECTED_NON_TRADING`, and
  `EXCLUDED_BY_POLICY` must ALL have `null` capital and `null` return
  reference.  The same conditional rule is encoded in the catalog JSON
  schema (`allOf` if/then on `disposition`).
- `CONFIRMED_ZERO` flow / `CONFIRMED_ONE` fx require every id in the
  corresponding `*_source_record_ids` array to resolve to an admitted,
  hash-verified artifact of the matching attestation role
  (`hwm-flow-attestation` / `hwm-fx-attestation`) in the final
  `source_artifacts` set.  Hash resolution proves admission, not content:
  the builder additionally parses the attestation record and requires its
  content to assert exactly the period under validation.  The attestation
  record contract is a closed JSON object
  `{"strategy_id", "date", "flow_status"}` (asserting `"CONFIRMED_ZERO"`)
  or `{"strategy_id", "date", "fx_status"}` (asserting `"CONFIRMED_ONE"`);
  the record's `strategy_id` and `date` must equal the period's, and the
  status field must equal the asserted value.  Non-empty-but-fabricated,
  wrong-role, or content-mismatched references all fail closed.  (One
  attestation artifact asserts one (strategy, date); multiple attestations
  require multiple admitted artifacts.)
- `EXCLUDED_BY_POLICY` is accepted only for strategies named in the
  admitted PORT-HWM-3 `material_perimeter.exclusions` whose
  `effective_date` is on or before the period date; anything else is
  rejected.  The builder loads the versioned contract
  `contracts/portfolio-hwm-policy-v3.json` directly.

**Membership:** every `component_account_ids` entry must be a known
account; for `ACCOUNT`-lane `AVAILABLE` rows each entry must additionally
be MAPPED-assigned to the row's strategy on the row's date.

**Lane enforcement:** every `AVAILABLE` row MUST use lane `ACCOUNT`.
`PROVIDER`- and `LEGACY`-lane `AVAILABLE` rows are rejected fail-closed at
both admission and consumption boundaries because no hash-bound provider or
legacy capital evidence is admitted (deferred admission requirements 4 and
5).  This check lives in the shared validator so every caller —
admission builder, report consumer, and panel consumer — enforces it
identically.

**Capital provenance (never a trusted label):**

- `ACCOUNT`-lane `AVAILABLE` capital is verified against the hash-bound
  NAV-rollforward rows in the catalog's `strategy_returns`
  (`evidence_layer == "ACCOUNT_LIVE"`): the row for `(strategy_id, date)`
  must exist, its `beginning_capital` must equal `beginning_capital_usd`
  exactly, its `component_account_ids` must match the period's, and
  `return_source_record_id` must equal its
  `account_reconciliation_sha256`.  Snapshot backfill is impossible: the
  allocation snapshot is never accepted as a capital source.
- `PROVIDER`-lane `AVAILABLE` is rejected outright (fail closed): the
  current catalog shapes carry no hash-bound provider capital evidence —
  provider-live return rows have no `beginning_capital`, no component
  account set, and no reconciliation hash — so provider-only periods must
  be `UNAVAILABLE` until such evidence is admitted (deferred admission
  requirement 5).
- `LEGACY`-lane `AVAILABLE` is rejected outright — no admitted
  effective-dated capital schedule exists for the lane.

### `reconciliation`

| Field | Meaning |
|---|---|
| `period_capital_method` | Always `"NAV_ROLLFORWARD"`. |
| `allocation_endpoint_check.matched` | Boolean; must be `true`. |
| `allocation_endpoint_check.delta_usd` | Decimal string; must equal the independently recomputed delta. |

The allocation snapshot is endpoint reconciliation only.  The shared
validator recomputes the check independently when the caller supplies the
`allocations` parameter: at the latest systematic allocation snapshot date,
each `ACCOUNT`-lane `AVAILABLE` period contributes its rollforward-verified
ending capital (`beginning_capital × (1 + return)` from the same
`strategy_returns` row); the sum is compared against the allocation snapshot
total over exactly those strategies.  Any non-zero recomputed delta aborts
the build (unreconciled capital is rejected); the declared `delta_usd` must
equal the recomputed delta (`"0"` in canonical decimal text) and `matched`
must be `true`.  Consumers that cannot access the allocation snapshot omit
this parameter and fall back to a simple check (`matched == true` and
`delta_usd == 0`); the admission builder always supplies allocations for
independent recomputation.

## Governed HWM source roles

The baseline spec admits optional hash-bound sources whose key IS the role
(the builder enforces `schema_name == source_key`; the spec schema lists
them as optional properties, never required).  The `hwm-inputs` envelope
source is itself role-bound the same way: its `schema_name` must equal
`hwm-inputs`.

| Source key (role) | Consumed by |
|---|---|
| `hwm-inputs` | The envelope itself (optional, role-bound). |
| `hwm-anchor-acceptance` | `anchor.acceptance_provenance` (and the anchor content cross-check). |
| `hwm-calendar-authority-account` / `-provider` / `-legacy` | `calendar_authorities` per lane. |
| `hwm-flow-attestation` | `flow_source_record_ids` under `CONFIRMED_ZERO`. |
| `hwm-fx-attestation` | `fx_source_record_ids` under `CONFIRMED_ONE`. |

## Deferred admission requirements

The following data cannot be derived from existing catalog inputs today;
the builder fails closed in their absence rather than fabricating them.
Each requires a separately admitted, hash-bound artifact before the
corresponding envelope content can validate:

1. **Calendar authority artifacts.**  No calendar-authority artifact type
   existed before this contract; the `hwm-calendar-authority-*` sources
   must be admitted as governed spec sources.  An envelope that declares a
   lane without the matching role artifact fails closed.
2. **Anchor acceptance artifact.**  The `hwm-anchor-acceptance` record is
   the governed acceptance evidence for the anchor; observation artifacts
   are rejected in this role.
3. **Exchange trading-day refinement.**  The expected keyspace is the full
   calendar-day interval; distinguishing trading from non-trading days for
   `EXCHANGE` calendars requires day-level calendar content in the
   authority artifacts and is deferred — non-trading days are represented
   explicitly as `EXPECTED_NON_TRADING` rows so the keyspace stays
   complete.
4. **LEGACY-lane capital schedule.**  No admitted effective-dated capital
   schedule exists for the LEGACY lane; LEGACY periods stay `UNAVAILABLE`
   until one is admitted.
5. **Provider capital evidence.**  PROVIDER-lane `AVAILABLE` requires
   hash-bound provider capital evidence (a provider rollforward or
   effective-dated capital schedule carrying beginning capital, component
   accounts, and a reconciliation/return identity).  No current catalog
   shape supplies it — provider-live return rows have no `beginning_capital`
   — so every PROVIDER `AVAILABLE` period is rejected until this evidence
   is admitted.

## Admission flow

1. The spec author creates an `hwm-inputs` source reference in the
   baseline spec (root, relative path, expected SHA-256, schema metadata),
   plus any governed HWM evidence sources the envelope references.
2. When the spec declares the source, the builder loads the JSON and
   validates every cross-reference, disposition rule, keyspace-completeness
   rule, capital-provenance rule, lane-enforcement rule, and the endpoint
   reconciliation against the COMPLETE catalog context (provider artifacts
   included) via the shared validator
   (`validate_hwm_inputs_envelope`), then populates
   `catalog["hwm_inputs"]`.  Admission-only checks (CONFIRMED_* attestation
   content verification, account-assignment mapping) run afterward and are
   clearly separated from the shared validation.
3. When the spec omits the source, the builder omits the key entirely.
4. The #277 HWM index engine reads only `catalog["hwm_inputs"]["periods"]`
   and filters to `disposition == "AVAILABLE"` rows.

## Consumer contract (#277 boundary)

The HWM index engine expects from `hwm_inputs.periods`:

- `strategy_id`, `date` — the period identity.
- `beginning_capital_usd` — the starting capital for NAV roll-forward.
- `return_source_record_id` — resolved against a return-series
  crosswalk to obtain the daily return for this period.

The engine does NOT consume `calendar_authorities`, `reconciliation`,
or non-`AVAILABLE` periods directly — those are governance interfaces
consumed by the admission pipeline.

Both #277 consumers AND the admission builder enforce the full contract
via the shared ledger-package validator
(`quant_engine.portfolio_ledger.validate_hwm_inputs_envelope`): a
hash-bound but semantically invalid generation publishes nothing.  The
builder calls the validator at admission with its full context
(allocations for endpoint recomputation, the parsed acceptance record for
anchor-content cross-check); consumers call it with only the catalog-bound
context.  Two consumption-side rules follow from that boundary: an
AVAILABLE row with a MISSING flow or fx status is invalid, and an
AVAILABLE row carrying a CONFIRMED_ZERO/CONFIRMED_ONE attestation is
rejected, because the attestation payload content is verifiable only at
admission (the governed source files live outside the generation stores
the consumers can reach).  **hwm-001 unification note:** the builder
previously admitted content-verified CONFIRMED_* statuses on AVAILABLE
rows.  As of the hwm-001 consolidation fix, the shared validator's
category rejection of CONFIRMED_* on AVAILABLE rows applies universally
at both admission and consumption.  The builder still verifies CONFIRMED_*
attestation content on non-AVAILABLE rows (admission-only).  CONFIRMED_*
assertions on non-AVAILABLE rows remain valid envelope content.

## Fail-closed invariants

- Decimal strings for all money and rate fields.
- Closed schemas (`additionalProperties: false`) throughout the envelope.
- Every artifact reference resolves against the catalog's FINAL
  `source_artifacts` set with the required schema role and a verified
  SHA-256.
- The expected (strategy, date) keyspace is derived from the governed
  perimeter (systematic allocations, classified by the admitted PORT-HWM-3
  policy) over the anchor-to-as-of window and must equal the period keys
  exactly.
- Any unknown key, invalid value, or rule violation raises
  `BaselineValidationError` and aborts catalog generation.
- Absence of the entire key or any internal defect is tolerated by
  existing consumers (the key is staged optional).

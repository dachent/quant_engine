# Real Epoch Manifest Production Design

## Status and purpose

This revision is the accepted correction to the failed Task 5 adversarial
review. It defines an additive, snapshot-first private generation bundle for
the retained real account-strategy authority. It does not authorize provider
access, publication, pointer changes, release consumption, or trading.

The retained real result is expected to remain honest and incomplete:

- 18 accounts and 26 assignment/perimeter rows;
- 15 `STRATEGY_MAPPED` rows: 12 current/open-ended and 3 closed;
- 7 `HISTORICAL_POOL_ONLY` rows;
- 4 `RESERVE_NOT_APPLICABLE` rows;
- 15 blocked mapped rows and zero numeric rows unless an admissible version-2
  account-interval retrieval receipt is found and independently verified.

An absent flow row is never zero. `VerificationStatus.VERIFIED` means that the
private bundle passed artifact-integrity and independent semantic comparison;
it does not mean complete financial coverage or publication readiness.

## Considered approaches

### 1. Additive snapshot-first bundle API -- selected

Keep the existing `GenerationManifest`, `stage_generation`, `read_generation`,
and legacy generation consumers unchanged. Add immutable byte snapshots, a
snapshot-native epoch producer entry point, version-2 closed retrieval
receipts, a 26-row perimeter artifact, an independent verifier, and new atomic
bundle functions: `stage_generation_bundle`, `verify_generation_bundle`, and
`read_generation_bundle`.

This is the smallest architecture that fixes one-time reads, receipt authority,
the generation-envelope circularity, full-perimeter evidence, and independent
verification without migrating existing producers.

### 2. Epoch-specific manifest and store -- rejected

A custom epoch manifest would duplicate content addressing, inventory,
collision, recovery, and later release lineage. Task 6 would then need an
adapter from the custom store into the existing core generation contract.

### 3. `GenerationManifest` version-2 migration -- rejected

Embedding new inventory fields in a new manifest version would require a
cross-repository producer and consumer migration. It would not eliminate the
need for excluded detached control files, and its regression surface is
disproportionate to Task 5.

## Boundaries and sequencing

Task 5 is implemented and reviewed sequentially:

1. **5A -- strict input authority:** strict JSON, immutable snapshots,
   snapshot-native producer API, and coverage/receipt version 2.
2. **5B -- atomic bundle envelope:** additive stage, verify, and read APIs with
   detached inventory and receipt.
3. **5C -- private authority generator:** perimeter, generator, independent
   verifier, private display adapter, schemas, contract, and synthetic tests.
4. **5D -- retained real build:** one local build from the pinned bytes,
   exhaustive mutation evidence, and private human reports.

Each tranche receives a fresh read-only adversarial review before the next
tranche begins. No Task 5 implementation begins until the 5A implementation
brief has been extracted from the approved plan.

Task 5 does not modify `publish_generation`, current-pointer logic,
authority/release/pipeline consumption, or financial panel materialization.
Task 6 owns verified-bundle consumption and publication integration.

## Immutable snapshot authority

`InputSnapshot` lives in the producer-independent core module
`quant_engine.core.snapshots`. It is a frozen value containing a label,
immutable bytes, SHA-256, byte size, and an optional path used only in
diagnostics. `capture(path, label)` performs one byte read.
`from_bytes(label, contents)` creates a snapshot for a generated in-memory
artifact. The type owns strict JSON parsing but has no JSON serialization
method; a snapshot is an in-process byte boundary, not a persisted authority.
Producer, generator, and verifier consume this exact core type or its immutable
bytes. Parsing, identity binding, artifact construction, calculation, and
independent verification never reopen the diagnostic path.

The generation command snapshots these selected inputs exactly once before it
parses or derives anything:

- the pinned provider-ledger catalog and its identity manifest;
- the pinned baseline catalog;
- the flow-classification policy;
- every candidate retrieval receipt;
- the accepted epoch producer source;
- the generation builder source;
- the independent verifier source.

The provider-ledger generation, hash, and size and the baseline generation,
hash, and size must match the pinned Task 5 values. `CURRENT.json` is never
resolved. A stale generation declaration, wrong bound source identity, linked
path, path escape, or byte mismatch fails before derivation.

The existing path-based `build_manifest_candidate(...)` remains a compatibility
wrapper. It captures each path once and delegates to
`build_manifest_candidate_from_snapshots(...)`. The Task 5 generator calls only
the snapshot-native entry point. A test mutates every selected path after
capture and proves that the generated bytes are unchanged and that no path is
reopened.

Import-boundary tests prove that the producer, generator, and verifier use the
exported core `InputSnapshot` rather than private lookalikes. They also prove
that importing the verifier does not import the producer or generator.

## One strict JSON boundary

One shared core loader is used by every Task 5 authority and generation
boundary, including `InputSnapshot.json_object`, `GenerationManifest`, retrieval
receipts, coverage, perimeter, independent reports, inventory, and bundle
receipts.

The loader:

- decodes strict UTF-8;
- rejects duplicate object keys at every nesting depth;
- rejects `NaN`, `Infinity`, and `-Infinity`;
- rejects malformed JSON and trailing content;
- optionally requires an object root;
- never silently normalizes source bytes.

Generated JSON must additionally equal the repository's canonical JSON bytes.
Selected source JSON may be noncanonical, but duplicate keys and invalid
numbers are always rejected.

## Deterministic generation date

The build derives exactly one `generation_as_of_date`. It strictly parses the
top-level canonical ISO `as_of_date` from the pinned provider-ledger snapshot
and the pinned baseline snapshot, then takes their maximum. Missing,
non-string, noncanonical, or malformed source dates fail before derivation.
Provider-ledger-before-baseline, baseline-before-provider-ledger, and equal-date
orders are all explicit positive cases.

That one derived date is used for `GenerationManifest.as_of_date` and every
generated artifact, independent report, inventory, or receipt field that has
as-of semantics. Source receipts retain their own snapshotted source dates;
they do not supply or override the generation date.

Task 5 code must not read wall-clock time, local date, timezone, locale, or
filesystem timestamps to produce bytes. Builds under different mocked dates
and timezones must produce identical payloads, reports, controls, generation
ID, and inventory digest.

## Assignment perimeter artifact

The selected baseline remains the full assignment source. The producer
validates every assignment and partitions without dropping rows:

- `STRATEGY_MAPPED` requires a known nonempty strategy and is eligible for
  coverage, admission, and calculation;
- `HISTORICAL_POOL_ONLY` requires no strategy and is retained as nonnumeric
  perimeter evidence;
- `RESERVE_NOT_APPLICABLE` requires no strategy and is retained as nonnumeric
  perimeter evidence.

The separate `account-assignment-perimeter` artifact contains:

- `schema_version`, `artifact_id`, and `as_of_date`;
- the exact selected baseline identity;
- `full_perimeter_identity` with canonical hash and count;
- `account_count`, `assignment_count`, and closed partition counts;
- `fact_bijection_status`, `unassigned_fact_count`, and `unassigned_facts`;
- one row per baseline assignment.

Each row contains the assignment identity, internal account ID, provider,
strategy disposition, nullable strategy ID, effective dates, calculation
eligibility, and counts for available NAV, unavailable NAV, raw flows, and
classified flows.

Every retained NAV and raw/classified-flow fact must map to exactly one full
assignment interval. A fact outside every interval or in overlapping intervals
fails verification. Non-strategy facts remain counted and hash-bound but never
enter numeric calculation.

For the pinned retained build, the verifier requires exactly 18 accounts,
26 rows, and the 15/7/4 partition. The schema remains general; these exact
counts are retained-run acceptance assertions rather than universal schema
constants.

## Account-interval flow-completeness authority version 2

Version 1 cannot authorize `COMPLETE` because its arbitrary source IDs do not
prove account, interval, category coverage, terminal pagination, or selected
source identity. Task 5 rejects version-1 `COMPLETE`. An explicit legacy
conversion may produce only `BLOCKED` with
`LEGACY_RECEIPT_UNVERIFIABLE`; it may never preserve `COMPLETE`.

The version-2 coverage artifact contains:

- `schema_version: 2`, `artifact_id`, and `as_of_date`;
- exact ledger and classified-flow identities;
- the exact mapped-assignment identity;
- the declared receipt artifacts;
- exactly one row for every mapped assignment.

Each row contains the closed assignment key, `COMPLETE` or `BLOCKED`,
`ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2` or `NONE`, a nullable receipt artifact
ID, and a closed blocker code/detail. Supported blocker codes are:

- `NO_ADMISSIBLE_V2_RECEIPT`;
- `ACCOUNT_BINDING_MISMATCH`;
- `INTERVAL_BOUNDARY_MISMATCH`;
- `CATEGORY_SET_INCOMPLETE`;
- `PAGINATION_NOT_TERMINAL`;
- `SOURCE_IDENTITY_MISMATCH`;
- `RECEIPT_OVERLAP`;
- `RECEIPT_GAP`;
- `LEGACY_RECEIPT_UNVERIFIABLE`.

A `COMPLETE` row is valid only when the referenced strict receipt bytes are
present and satisfy the closed receipt schema. The receipt binds:

- issuer adapter name/version and code SHA-256;
- provider, provider generation, generation-manifest artifact, and exact raw
  evidence artifacts;
- internal account ID, provider, and a source-account-identity SHA-256;
- requested and covered inclusive start/end dates;
- sorted unique required, completed, and unavailable flow categories;
- pagination mode, page count, request/response chain hashes, terminal-response
  hash, and `terminal: true`;
- `EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES` and `truncated: false`.

The validator requires exact account equality, full inclusive interval
coverage, identical required/completed category sets, no unavailable category,
terminal pagination, and exact equality to the selected immutable source
identity.

The reversible current ruling is an empty receipt-issuer allowlist unless an
exact retained adapter/code identity and category contract already exist.
Consequently the current real run remains 15 blocked mapped rows and zero
numeric rows. Future provider-specific work may add issuer/category contracts
without changing this generation envelope or the Task 4 calculations.

The retained allowlist is a closed supported-API boundary, not a caller
argument.
No production loader, candidate builder, assembler, or CLI entry point may
accept a caller-supplied issuer/category contract, validated-coverage token, or
synthetic authority registry. Tests may exercise strict receipt predicates and
arithmetic with test-local data. A pure predicate helper may return
non-authoritative validated `COMPLETE` facts, but it must not install synthetic
trust into the production producer, construct the opaque production coverage
object, or emit a numeric candidate.

This is an API and architecture boundary for cooperative code using the
supported surface, not isolation from arbitrary code already executing in the
same Python interpreter. Private names, closures, and slot-only objects are not
treated as security controls because Python reflection can inspect them. The
durable authority boundary is the exact persisted bundle plus a separately
invoked independent verifier in Tasks 5B/5C; that boundary still assumes the
checked-out code, interpreter, and operating system are trusted.

## Admission and calculation

The generator deterministically classifies each selected raw flow exactly once
under the snapshotted policy and emits a redacted calculation-input projection
that retains exact source record IDs and calculation facts without full account
numbers.

Mapped admission is built against exact positive `AVAILABLE` NAV record IDs and
the accepted Task 3 policy. Mapped assignments without qualifying in-interval
NAV remain blocked. Continuity remains allowed only across contiguous mapped
epochs with accepted predecessor closing NAV and complete flow coverage.

Only after coverage and admission pass may the accepted Task 4 producer emit a
numeric row. A blocked row has no numeric metric or calculation trace. Task 5
does not change the Task 4 money, RONC, XIRR diagnostic, observed-return, HWM,
or MaxDD definitions.

The accepted observation calculation aggregates every classified flow in
`(prior_nav_date, current_nav_date]`; it does not require consecutive calendar
days and does not relabel sparse observations as daily performance. Leading
nonpositive NAV observations are excluded only through the explicit funded-
start rule. A nonpositive return base after funding blocks the epoch. Human and
browser labels continue to distinguish RONC, diagnostic XIRR, and observation-
linked return; none is relabeled MWR or daily TWR.

## Independent verifier

`IndependentAccountManifestVerifier` is a named, separate implementation. Its
production module is forbidden from importing:

- `tools.account_strategy_manifest`;
- `tools.build_account_manifest_generation`;
- any producer calculation helper.

An AST/import test enforces that boundary. The verifier may use shared strict
JSON, canonical bytes, schema validation, and core immutable contract types.
It independently parses the selected snapshots and candidate payload byte map.

It verifies:

- exact canonical payload paths and bytes;
- every source identity, artifact SHA-256, and size;
- the 26-row perimeter and fact bijection;
- one coverage and admission disposition per mapped assignment;
- classified-flow identity, amount, sign, date, disposition, and reconciliation;
- funded NAV identity, account, date, status, and value;
- absence of every numeric field and trace on blocked rows;
- exact recomputation of every numeric field on a positive synthetic fixture;
- exact blocker propagation and zero-numeric proof on the retained run.

Its verification subject is the exact semantic payload path/hash/size set that
excludes `verification/independent-verification.json`. Its canonical report
contains verifier/code identity, selected source identities, the sorted subject
entries and their canonical semantic digest, exact counts, one blocker proof per
mapped assignment, numeric candidate/comparison/discrepancy counts, invariant
results, and status. The verifier recomputes the expected report from that
subject set and compares the finalized report bytes; it never hashes a set that
contains its own report.

The retained run is non-vacuous even with zero numeric rows: verification must
show 15 independent blocker chains, 15 blocked output rows, no forbidden numeric
field or trace, zero numeric candidates, zero numeric comparisons, and zero
discrepancies. A separate positive synthetic fixture ensures the independent
numeric implementation is exercised and catches value and trace mutation.

## Non-circular generation envelope

Construction order is fixed and non-circular:

1. derive and canonicalize the independent-verifier subject payloads;
2. independently verify their exact path/hash/size set and create the finalized
   report that binds only that subject set;
3. build `GenerationManifest` over the subject payloads plus the finalized
   report;
4. build and verify the detached inventory and receipt over the complete
   manifest-declared set.

`GenerationManifest` remains unchanged and continues to define the generation
ID from its canonical identity payload. Its artifacts are the exact subject
payload files plus `verification/independent-verification.json`. It does not
declare itself or either detached control file.

For a retained run without admissible receipts, the exact semantic payload set
is:

- `authority/selected-input-identities.json`;
- `authority/account-assignment-perimeter.json`;
- `authority/calculation-inputs.json`;
- `authority/flow-classification-policy.json`;
- `authority/classified-flows.json`;
- `authority/account-flow-coverage.json`;
- `authority/mapped-epoch-admission.json`;
- `authority/code-identities.json`;
- `outputs/account-strategy-manifest.json`;
- `verification/independent-verification.json`.

An accepted future receipt is stored under
`authority/retrieval-receipts/<receipt-id>.json` and declared as a normal
payload artifact.

The three control files are:

1. `generation.json`: the unchanged core `GenerationManifest`;
2. `generation-inventory.json`: canonical exact path/hash/size entries for
   `generation.json` plus every declared payload; it excludes itself and the
   receipt;
3. `generation-receipt.json`: binds the generation ID, manifest hash/size,
   inventory hash/size, independent-report artifact identity, exact path count,
   and envelope schema version.

This ordering is non-circular. The existing
`generation_inventory_sha256(root, generation_id)` continues to hash every
regular file in the final directory, including both detached control files.
That full-directory digest is reported externally and is never embedded in the
generation.

## Atomic create-only staging

`stage_generation_bundle(root, manifest, payloads)` validates the entire
in-memory bundle before writing. The manifest artifact path set must equal the
payload mapping exactly. Paths are portable POSIX-relative names with no drive,
backslash, empty component, `.`, `..`, control-file collision, or duplicate.

The function creates a sibling `.stage-*` directory, writes every file with
exclusive creation, flushes files, validates the on-disk temporary bundle, and
performs one directory rename to `generations/<generation_id>`.

Recovery and collision rules are exact:

- failure before rename leaves no final generation;
- a process death may leave only an ignored `.stage-*` orphan;
- orphans are never promoted, read as generations, or automatically deleted by
  Task 5;
- after rename the final directory is complete because nothing is appended;
- an existing final directory is idempotent only when the exact expected path
  set and every byte match;
- missing, extra, different, linked, or irregular content is a conflict and is
  never repaired in place;
- root, generations root, final directory, nested directories, and files must
  not be symlinks;
- a linked or irregular object fails closed;
- bounded transient Windows rename-denial retries reuse the existing delay
  policy; an exhausted retry preserves any prior final generation.

`verify_generation_bundle` repeats strict parsing, canonical-byte validation,
manifest identity, artifact hashes/sizes, inventory, receipt, path-set, and
link checks. `read_generation_bundle` succeeds only after that verification and
returns immutable bytes. Legacy `stage_generation` and `read_generation`
behavior remain unchanged.

Task 5 does not alter `publish_generation`. Task 6 must require a verified
bundle before using or publishing the epoch generation.

## Private human display identity

Machine artifacts use stable internal account IDs. Human audit tables use a
private redacted crosswalk outside the generation. Each row contains only:

- internal account ID;
- provider;
- approved nickname;
- exactly four decimal digits in `last_four`.

`PrivateDisplayIdentityAdapter` rejects unknown keys such as
`account_number`, routing or external identifiers, duplicate IDs, provider
mismatches, a last-four value not matching four digits, and any account-number-
like sequence of five or more digits after separator normalization. A sentinel
privacy test plants a forbidden full-number token and proves rejection and
absence from outputs, logs, exceptions, and generated artifacts.

Human tables lead with provider, approved nickname/last-four, strategy name,
effective dates, NAV observation range/count, coverage blocker, admission
blocker, and numeric-row disposition. The required 15-row mapped table and
26-row perimeter reconciliation are written outside
`generations/<generation_id>`. They may reference the generation ID but are not
manifest artifacts and cannot affect machine identity.

## Contract and release lineage

Task 5 is bundle-only. It does not modify the financial-MVP source contract,
its schema, its generated views, or their tooling/tests.
`docs/data-rollforward.md` documents the snapshot-only command, bundle layout,
`PARTIAL + VERIFIED` semantics, and recovery rules.

Task 6 adds a dedicated top-level `epoch_manifest_source` field to the
financial-MVP source contract. It must not add the epoch bundle to
`source_segments`: the accepted historical-source set and its equality
contract remain unchanged. The dedicated field binds the verified Task 5
bundle, exact `outputs/account-strategy-manifest.json` artifact identity, and
consumption rules. Task 6 bumps and validates the contract schema as needed,
regenerates the Markdown and source-matrix views, and proves their checked-in
bytes match `check_views`.

Task 6 accepts only `read_generation_bundle` output and passes the exact
account-manifest bytes through release materialization, workbook proof,
dashboard generation, pointer candidate, and browser rendering. Task 7 reviews
the exact unchanged bundle and manifest bytes used by Task 6.

## Verification and mutation requirements

The first Task 5 RED is a real-shape mixed-baseline integration test requiring
26 perimeter rows, the 15/7/4 partition, 15 mapped dispositions, and every
semantic payload hash-bound by `generation.json`.

Required mutation proofs include:

- post-snapshot path mutation and a path-read spy;
- use of the one exported core snapshot type and verifier import isolation;
- duplicate keys at top-level and nested JSON boundaries;
- invalid UTF-8 and nonfinite JSON numbers;
- missing/malformed/noncanonical source `as_of_date`, all source-date orderings,
  and identical bytes/identity under different mocked dates and timezones;
- version-1 complete coverage, missing receipt, wrong account, wrong interval,
  category gap, nonterminal pagination, source mismatch, receipt overlap/gap;
- absence from the supported public API and documented production entry points
  of caller-injectable receipt authority, coverage construction, or lower-level
  numeric assembly; no claim is made against arbitrary same-process reflection;
- perimeter row removal/disposition change and fact assignment drift;
- classified-event identity, amount, sign, date, and disposition mutation;
- funded-NAV identity, account, date, status, and value mutation;
- blocked-row blocker/status mutation and insertion of any numeric field/trace;
- positive synthetic numeric value and trace mutation;
- independent report discrepancy/count mutation;
- independent subject path/hash/size/digest mutation, report self-inclusion,
  report omission from `generation.json`, and finalized report byte mutation;
- payload path, canonical bytes, artifact hash/size, generation ID, detached
  inventory, receipt, and full-directory inventory mutation;
- identical restaging, differing collision, interrupted rename, stale orphan,
  extra/missing file, and file/directory symlink behavior;
- forbidden verifier imports;
- full-account sentinel privacy and proof that display edits do not alter the
  generation ID;
- a network/provider call trap proving zero calls.

Two builds from identical immutable snapshots must produce byte-identical
semantic authorities, independent report, `generation.json`, generation ID,
detached inventory, detached receipt, and full-directory inventory digest.

## Stop rules and scope exclusions

- Never infer a mapping, receipt, flow, zero, funding threshold, calendar, or
  continuity rule.
- Unknown or incomplete receipt authority remains `BLOCKED`.
- No partial numeric row may escape a blocked mapped assignment.
- No full account number enters Git, fixtures, logs, exceptions, reports, or
  bundle artifacts.
- The unresolved Alpaca later interval `2026-04-18..2026-07-17` remains
  unmapped; Task 5 does not infer a strategy.
- No provider, credential, browser, trading, pointer, publication, GitHub, or
  merge effect occurs during Task 5 generation.
- Task 5 does not modify Task 4 calculations, Task 6 consumers, or unrelated
  Schwab work.
- Task 5 does not modify the financial-MVP source contract, its schema,
  generated views, or their generator/check tests.
- A 15-blocked/zero-numeric retained build is an honest partial-data authority,
  not a functional MVP-2 acceptance.

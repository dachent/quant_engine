# Real Epoch Manifest Production Plan

## Authority and outcome

This plan implements the owner-authority reset in
`docs/superpowers/specs/2026-08-25-real-epoch-manifest-design.md`. Earlier
detailed Task 5A-7 checklists are superseded as planning guidance. GitHub issue
#374 is the sole execution tracker. This Markdown plan neither records execution
status nor substitutes for issue-recorded authority, evidence, blockers, or
handoff. Retain earlier accepted evidence when still relevant, but do not use a
stale detail to override one of the five outcome gates below.

The required result is one independently verified, cutoff-bounded product
manifest with exactly 13 account-strategy epochs. JSON-bearing seams preserve
its exact bytes; report, workbook, and browser surfaces bind its hash identity
and prove rendered-cell parity. The remaining critical path has five sequential
outcome gates. A gate closes only on its stated evidence; activity or test
volume is not an outcome.

This document authorizes no provider call, credential use, publication,
pointer movement, external write, trading action, GitHub write, merge, or
destructive recovery operation. Those effects remain governed by the active
issue and repository operating model.

## Fixed owner decisions

- Calculation cutoff: `2026-08-13`, the last complete data refresh.
- Baseline mapping census: 15 `STRATEGY_MAPPED` assignments.
- Product subject: exactly 13 epochs, comprising 11 open epochs and the two
  closed TT predecessors needed for QSI continuity.
- EchoTrade: `DEPRECATED_HISTORY_ONLY` effective `2025-11-25`; retained
  assignment/history ends `2025-11-24`. It is excluded from all 13 product
  epochs and every current performance, risk, HWM, blocker, capital/value, and
  consolidated-value result. Its `$0.19` remains historical evidence only.
- Composer/Holy Grail: owner-excluded as immaterial and not a product blocker.
- Account joins: 13 confirmed owner mappings supplied in one explicit
  hash-bound current-product crosswalk, never hardcoded.
- Deployment: isolated corporate environment, one or two trusted operators,
  static result publication.

## Execution rules shared by all gates

- Preserve the full global source/perimeter evidence while isolating the
  13-row calculation payload from unrelated facts.
- Capture and hash-bind selected source and crosswalk bytes once. Never resolve
  a moving pointer or use wall-clock, timezone, locale, or filesystem time as
  calculation authority.
- Use exact decimal arithmetic. Never convert a missing, unavailable,
  ambiguous, or policy-invalid fact into zero.
- Block only the row affected by one specific evidenced missing or ambiguous
  fact. Duplicate raw IDs and same-account/date NAV ambiguity remain global
  integrity failures.
- Preserve source facts after the cutoff for lineage only; exclude them from
  every epoch calculation, trace, end date, and downstream numeric value.
- Exclude full account or routing numbers, credentials or secrets, and raw
  restricted values from Git, logs, exceptions, GitHub, and static release
  evidence. One explicit hash-bound current-product crosswalk may contain internal
  account IDs, approved labels, and exact four-digit suffixes or explicit
  `null`; there are no alternate crosswalk variants.
- Use one writer for shared files. Preserve unrelated dirty-worktree changes.
  Run bounded checks once per changed candidate and reference unchanged
  evidence rather than repeating it.
- Security work is limited to integrity, immutability, strict parsing,
  restricted-data non-disclosure, no unintended external effects, and
  exact-head release controls. Do not add generic multi-tenant or hostile
  network-service hardening.

## Five-gate critical path

| Gate/outcome | Exact bounded proof command | Artifact/report path | Expected result | Independent reviewer | Recovery/stop action |
|---|---|---|---|---|---|
| 1 — close authority and crosswalk | `.venv\Scripts\python.exe -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py::test_runtime_crosswalk_allows_repeated_nonoverlapping_epoch_keys tests/portfolio_control_panel/test_account_manifest_generation.py::test_runtime_crosswalk_rejects_duplicate_composite_keys_and_overlaps --disable-socket -q` | `<generation-root>/generations/<generation-id>/authority/account-assignment-crosswalk.json` and `.codex-bootstrap/mvp-audit-001/gate-proofs/gate-1-authority-crosswalk.md` | Two semantic tests pass; exact 13 composite keys, two repeated-account TT-to-QSI chains, consistent provider/label/suffix, zero overlap | Authority/lineage reviewer independent of crosswalk implementer | Reject the input, retain the prior immutable evidence, keep #374 open at Gate 1 |
| 2 — build cutoff-bounded manifest | `.venv\Scripts\python.exe -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py::test_real_13_epoch_cutoff_manifest_populates_supported_numbers_and_exact_blockers tests/portfolio_control_panel/test_account_manifest_generation.py::test_mixed_open_receipt_cutoff_bounds_calculation_projection --disable-socket -q` | `<generation-root>/generations/<generation-id>/outputs/account-strategy-manifest.json` and `.codex-bootstrap/mvp-audit-001/gate-proofs/gate-2-manifest-build.md` | Two tests pass; exactly 13 rows, no calculation fact after `2026-08-13`, supported scalars populated, only evidenced row blockers | Financial-calculation reviewer independent of producer author | Discard the create-only candidate, preserve pinned inputs, return #374 to Gate 2 evidence resolution |
| 3 — independent recomputation | `.venv\Scripts\python.exe -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py::test_independent_verifier_recomputes_real_13_epoch_manifest tests/portfolio_control_panel/test_account_manifest_generation.py::test_independent_verifier_rejects_each_declared_numeric_and_trace_mutation --disable-socket -q` | `<generation-root>/generations/<generation-id>/verification/independent-verification.json` and `.codex-bootstrap/mvp-audit-001/gate-proofs/gate-3-independent-review.md` | Valid candidate passes; the planned exhaustive parameterized test rejects a mutation to every declared numeric scalar and every trace field; zero unexplained discrepancies | Hostile calculation reviewer who authored neither producer nor verifier | Reject candidate and report together; fix one finding per tranche; re-enter Gate 3 with new hashes |
| 4 — bind JSON and prove rendered parity | `.venv\Scripts\python.exe -m pytest tests/core/test_portfolio_pipeline_materialize.py::test_epoch_manifest_bundle_prewire_binds_once_and_stages_exact_bytes tests/portfolio_control_panel/test_portfolio_control_panel.py::test_verified_bundle_panel_manifest_reads_binds_and_preserves_literal_values tests/portfolio_control_panel/test_portfolio_control_panel.py::test_account_manifest_workbook_and_browser_bind_hash_and_match_rendered_cells --disable-socket -q` | `generated/financial-mvp/current-product.md`, `generated/financial-mvp/source-matrix.json`, and `.codex-bootstrap/mvp-audit-001/gate-proofs/gate-4-surface-parity.md` | Three tests pass; JSON seams preserve exact bytes; workbook/browser bind the manifest hash and match every rendered cell | Rendered-cell/browser reviewer independent of release integrator | Block publication/pointer movement, retain last verified generation, return #374 to the first divergent seam |
| 5 — exact-head acceptance/handoff | `.venv\Scripts\python.exe -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py tests/core/test_portfolio_pipeline_materialize.py tests/portfolio_control_panel/test_portfolio_control_panel.py -k "runtime_crosswalk or real_13_epoch or independent_verifier or epoch_manifest_bundle_prewire or verified_bundle_panel_manifest or account_manifest_renderer or workbook_and_browser" --disable-socket -q`<br>`.venv\Scripts\python.exe -m ruff check tools/account_strategy_manifest.py tools/build_account_manifest_generation.py tools/verify_account_manifest_generation.py tools/portfolio_control_panel.py`<br>`git diff --check`<br>`$head = git rev-parse HEAD; $pr = ConvertFrom-Json -InputObject (gh pr view --json number,headRefOid,headRefName,statusCheckRollup); if ($pr.headRefOid -ne $head) { throw "PR headRefOid does not equal local HEAD" }; ConvertTo-Json -InputObject $pr -Depth 10` | `.codex-bootstrap/mvp-audit-001/real-epoch-manifest-acceptance.md` plus sanitized evidence in GitHub issue #374 | Nonzero bounded selection including workbook/browser parity passes; Ruff and diff check pass; `git rev-parse HEAD` exactly equals the read-only `gh pr view` `headRefOid`; three hostile lanes accept those same head/artifact hashes; current required CI is green; no merge | Exact-head integration reviewer independent of all three hostile lanes | Do not mark #374 accepted, update PR, or merge; preserve evidence and route the exact failed gate in #374 |

### Gate 1: Close current-product authority and crosswalk

**Outcome**

One immutable authority envelope identifies the exact source bytes, cutoff,
13 product assignments, owner exclusions, and approved human labels used by
the generation.

**Required work**

1. Pin the exact provider-ledger, baseline, classification policy, assignment
   authority, and any other calculation-authority bytes by path, generation,
   SHA-256, size, and schema. Do not use `CURRENT.json`.
2. Reconcile the 15 baseline mapped assignments to the 13 product epochs:
   retain 11 open assignments and the two closed TT predecessor assignments;
   exclude EchoTrade and Composer under their exact owner dispositions.
3. Supply the 13 confirmed account-to-assignment joins as one explicit
   hash-bound current-product crosswalk. Each row carries internal
   account/provider, exact assignment key, strategy and bounds, approved
   account label, and required `last_four` of four digits or explicit `null`.
4. Hash-bind the complete crosswalk bytes into generation identity. Delete any
   design or interface distinction between alternate crosswalk variants. Allow
   a repeated account ID only for distinct, non-overlapping composite keys
   `(account_id, strategy_id, effective_start, effective_end)`. Require exact
   provider, approved-label, and `last_four` consistency across every row for
   that account. Reject duplicate composite keys and overlapping intervals. No
   join may be hardcoded or inferred.
5. Keep EchoTrade's retained `$0.19` visible only as clearly labeled historical
   evidence, excluded from current and consolidated capital/value, and prove
   that later general/unmapped Alpaca observations do not extend its history.
   Keep Composer in separate owner-exclusion context.

**Gate evidence**

- exact source and crosswalk identity table;
- explicit 15-to-13 reconciliation showing 11 open and two closed TT rows;
- strict crosswalk readback proving the two repeated-account TT-to-QSI chains,
  consistent repeated-account display fields, unique composite keys,
  non-overlapping intervals, and no missing or extra assignment;
- EchoTrade and Composer disposition readback; and
- restricted-data scan over the candidate evidence boundary.

**Stop conditions**

Stop this gate on a source-identity mismatch, an unconfirmed product join, a
crosswalk cardinality or composite-key mismatch, an account-interval overlap,
inconsistent provider/label/suffix for a repeated account, an invalid approved
label/suffix, an attempt to extend an excluded strategy, or leakage of an
excluded value. Do not begin numeric construction with an unresolved subject
set.

### Gate 2: Resolve source semantics and build the 13-row manifest

**Outcome**

One deterministic manifest contains exactly the cutoff-bounded 13 product
epochs. Every scalar supported by the pinned bytes is populated; each absent
scalar is explained by a specific row-level blocker with exact evidence.

**Required work**

1. Derive global source/perimeter dispositions before selecting calculation
   facts. Retain excluded, historical, reserve, unmapped, and unrelated blocked
   facts globally without allowing them to enter or poison an included row.
2. Enforce the cutoff in producer selection: open assignments end at
   `2026-08-13`; closed epochs retain exact `effective_end`; later observations
   remain lineage-only.
3. Resolve and document, against the pinned bytes, the exact semantics for:
   leading nonpositive NAV trim; nonpositive NAV after funding; first-opening
   flows; flows after the last covering NAV; interval completeness; policy
   signs; external versus internal flow classes; and exact reciprocal
   internal-transfer support.
4. Apply predecessor-closing continuity to the two adjacent TT-to-QSI chains.
   The predecessor closing NAV/date becomes successor opening capital and
   observation-link anchor.
5. Build the 13 rows with exact source NAV IDs, raw flow IDs, governed support
   IDs, blockers, capital-flow trace, observation-link trace, and every
   supported money/return/risk scalar required by the design.
6. Retain strict snapshot, JSON, canonical-byte, content-addressed, create-only,
   and collision controls already accepted for the generation envelope.

No synthetic online-retrieval artifact is a universal prerequisite. Missing
source-flow or opening evidence must be identified directly and block only the
affected row. The build may not suppress a supported number merely to preserve
an earlier all-blocked disposition.

**Gate evidence**

- exact 13-row structural readback;
- cutoff proof for every selected fact, output end, and trace;
- source-to-row and reciprocal-support lineage tables;
- exact money-identity checks and scalar/trace inventory;
- row-specific blocker table with no universal proxy blocker; and
- deterministic second build from the same immutable inputs.

**Stop conditions**

Stop on an extra or missing product row, later-than-cutoff calculation fact,
hardcoded join, inferred flow sign or amount, unrelated-fact contamination,
unexplained null, unsupported scalar, nonreciprocal internal support, or byte
nondeterminism.

### Gate 3: Independently recompute and adversarially challenge

**Outcome**

A separate verifier proves the exact 13-row subject, every populated scalar
and trace, every blocker, every source/support ID, and the cutoff without using
producer calculation helpers or producer output as its oracle.

**Required work**

1. Parse the same pinned source, policy, and crosswalk bytes independently.
2. Derive the 13 assignment keys and calculation windows independently,
   including EchoTrade/Composer exclusions and the two TT-to-QSI continuity
   chains.
3. Reclassify flows, reconstruct reciprocal support, select NAVs, apply the
   cutoff, and recompute opening/invested/withdrawn/net/current/gain, simple and
   annualized RONC, observation-linked return/HWM/drawdown, diagnostic XIRR,
   and both traces.
4. Verify exact source record IDs, support IDs, blocker evidence, canonical
   payload paths, hashes, sizes, and semantic digest.
5. Prove independence by import-boundary inspection and success-then-mutate
   checks. Start from a valid verified candidate. Use the planned exhaustive
   parameterized
   `test_independent_verifier_rejects_each_declared_numeric_and_trace_mutation`
   to mutate every declared numeric scalar and every trace field independently;
   each case must fail the corresponding verifier invariant. Separately mutate
   each material subject, blocker, cutoff, and lineage field.

Do not add tests that merely search implementation source text or restate the
plan. Evidence must exercise or semantically read back the actual contract.

**Gate evidence**

- verifier/code identity and forbidden-import audit;
- independent scalar and trace comparison table;
- exact row/blocker/source/support/cutoff reconciliation;
- mutation transcript with a positive control and one-fault negative controls;
  and
- zero unexplained discrepancies for the unchanged candidate.

**Stop conditions**

Stop on helper reuse, producer-derived expected values, subject-set mismatch,
unverified blocker, missing source/support ID, cutoff drift, a mutation that is
accepted, or any unexplained numeric or byte discrepancy.

### Gate 4: Bind verified JSON through every product surface

**Outcome**

Every JSON-bearing seam consumes the same independently verified manifest bytes.
Report, workbook, dashboard, and browser surfaces bind that JSON hash identity
and show the same 13 product rows, values, blockers, labels, and lineage.

**Required work**

1. Bind the exact verified manifest artifact path, SHA-256, size, schema,
   generation, inventory, verification report, and release-control identities
   into the financial release contract.
2. Preserve exact manifest bytes through each JSON-bearing materialization,
   financial-release, and panel-input seam. No layer may substitute a fixture,
   cached manifest, reconstructed value, or independently selected source.
3. Bind report, workbook, dashboard, and browser outputs to the exact manifest
   path/hash/size/generation and prove row/cell parity. These surfaces need not
   physically embed the JSON bytes. Formatting is allowed; financial
   recomputation is not.
4. Display approved account labels from the same hash-bound current-product
   crosswalk.
   Never substitute internal IDs or expose restricted values.
5. Render EchoTrade's `$0.19` only in separate, clearly labeled historical
   context, never current or consolidated capital/value; render Composer's
   exclusion only in separate owner-exclusion context. Neither becomes a
   product row, denominator component, or blocker.
6. Reject omission, one-byte mutation, lineage mismatch, stale generated view,
   row-set change, or rendered-cell divergence before publication or pointer
   movement.

**Gate evidence**

- JSON-seam path/hash/size/generation identity table;
- report/workbook/dashboard/browser input-hash and rendered-cell parity against
  the manifest;
- one-byte and omission negative controls at each JSON-bearing seam;
- separate-context proof for EchoTrade and Composer; and
- no publication, pointer, provider, credential, or trading call during the
  bounded build and verification.

**Stop conditions**

Stop on any JSON-seam byte-identity break, missing non-JSON input-hash binding,
rendered-cell divergence, value recomputation, stale or cached substitute,
product-row count change, excluded-strategy contamination, excluded-data
exposure, or unintended external effect.

### Gate 5: Bounded acceptance and exact-head handoff

**Outcome**

One unchanged exact head and exact verified bundle pass the necessary local
regressions, deployment-relevant hostile review, current required CI, and
governed issue/pull-request handoff. This plan stops before merge.

**Required work**

1. Run the bounded producer, verifier, schema, baseline, core-lineage,
   report/workbook, panel/browser, formatting, type, privacy, and static-
   integrity checks affected by the final candidate. Run no provider call and
   do not broaden the suite into irrelevant hardening.
2. Obtain fresh independent hostile reviews of financial calculation,
   authority/lineage/privacy, and rendered-cell/browser correctness for the
   exact candidate and artifact hashes. Resolve every validated in-scope
   finding and re-review the changed candidate.
3. Prove the reviewed Git head, source identities, current-product crosswalk
   hash, generation, verified manifest bytes, downstream consumed bytes,
   generated views, and retained evidence still agree. Capture
   `git rev-parse HEAD`, read
   `gh pr view --json number,headRefOid,headRefName,statusCheckRollup`, and fail
   unless the local HEAD string exactly equals `headRefOid`. This is read-only;
   do not merge.
4. Read or run the repository's current required Quality, Security,
   Data-boundary, and Issue-guard contexts for that unchanged exact head. Do
   not invent an additional generic security gate.
5. Update the governed issue and pull request only under their existing
   approval and sanitization rules. Record exact head/artifact identities,
   checks, review dispositions, recovery state, and known gaps. Do not merge.

**Gate evidence**

- bounded command/result inventory for the unchanged candidate;
- three deployment-relevant hostile-review dispositions;
- exact-head and cross-layer byte reconciliation;
- current required CI results for the same head; and
- sanitized issue/PR readback showing no merge and no restricted values.

**Stop conditions**

Stop on head drift, artifact drift, a validated unresolved finding, missing or
failed required context, stale generated output, unsanitized external evidence,
scope expansion, or any request to merge without separate current authority.

## Completion rule

The work is complete only when all five gates close in order for one unchanged
candidate. A blocked gate remains open with its exact evidence; work is not
relabelled as accepted and unfinished scope is not moved to a shadow tracker.

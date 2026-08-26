# Real Epoch Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and independently verify a durable real-data account-strategy epoch manifest that the financial release and browser panel consume byte-for-byte.

**Architecture:** Partition the full baseline assignment perimeter from the mapped calculation perimeter, bind a separate account/interval flow-completeness authority, and generate one content-addressed manifest generation. Release consumers accept only the verified generation, while blocked evidence yields all-null metrics with exact reasons.

**Tech Stack:** Python 3.12, `Decimal`, JSON Schema, pytest, Ruff, mypy, Node browser contract tests, existing core publication pipeline.

**Spec:** `docs/superpowers/specs/2026-08-25-real-epoch-manifest-design.md`

## Global Constraints

- No provider, credential, browser-login, trading, current-pointer, release-publication, or merge effect during implementation tests.
- Every production behavior change uses RED-GREEN TDD and a mutation check.
- Full account numbers and private values remain outside Git, logs, reports, fixtures, and GitHub.
- An incomplete real-data perimeter remains `BLOCKED`; no missing fact becomes zero.
- Shared files are edited serially; hostile reviews are read-only.

---

### Task 1: Real baseline assignment partition

**Files:**
- Modify: `tools/account_strategy_manifest.py`
- Modify: `tests/portfolio_control_panel/test_account_strategy_manifest.py`

**Interfaces:**
- Consumes: baseline `assignments` with `strategy_mapping_disposition`.
- Produces: `partition_assignments(assignments) -> AssignmentPerimeter` containing mapped chains, non-strategy chains, and one canonical full-perimeter identity.

- [ ] Add a real-shape fixture with one `STRATEGY_MAPPED`, one `HISTORICAL_POOL_ONLY`, and one `RESERVE_NOT_APPLICABLE` assignment; assert the producer no longer rejects null strategy ids and does not calculate non-strategy rows.
- [ ] Run the focused test and confirm the current `assignment.strategy_id must be a non-empty string` failure.
- [ ] Implement closed-disposition validation and partitioning; bind the full perimeter while passing only mapped assignments to admission/calculation topology.
- [ ] Add negative tests for mapped-without-strategy, nonstrategy-with-strategy, unknown disposition, overlap, and any NAV/flow fact outside exactly one full-perimeter interval.
- [ ] Run the account-manifest suite and mutate the partition filter to prove a real-shape test fails.
- [ ] Commit only the producer and its tests.

### Task 2: Account/interval flow-completeness authority

**Files:**
- Create: `schemas/portfolio/account-flow-coverage.schema.json`
- Modify: `tools/account_strategy_manifest.py`
- Modify: `tests/portfolio_control_panel/test_account_strategy_manifest.py`
- Modify: `docs/analytics/account-strategy-manifest.md`

**Interfaces:**
- Consumes: `account-flow-coverage` JSON plus exact ledger/source identities.
- Produces: `load_flow_coverage(...) -> (CoverageIndex, SourceArtifact)` and per-epoch `COMPLETE`/`BLOCKED` decisions.

- [ ] Add failing tests showing that a listed flow does not prove neighboring dates, broad provider coverage metadata is rejected, and a complete exact interval permits a no-row date to mean zero.
- [ ] Implement the closed schema, single-snapshot loader, identity binding, interval validation, and exact epoch coverage query.
- [ ] Gate `analyze_epoch` so any uncovered interval emits all-null metrics with `FLOW_COMPLETENESS_NOT_PROVEN`.
- [ ] Add tests for overlaps, gaps, source-less completeness, boundary overreach, account mismatch, and post-coverage NAV.
- [ ] Run focused and full manifest tests; mutate the coverage containment predicate and prove boundary tests fail.
- [ ] Commit schema, producer, tests, and contract documentation.

### Task 3: Verifiable mapped-epoch admission policy

**Files:**
- Modify: `tools/account_strategy_manifest.py`
- Modify: `tests/portfolio_control_panel/test_account_strategy_manifest.py`
- Modify: `schemas/portfolio/account-strategy-manifest.schema.json`

**Interfaces:**
- Consumes: mapped assignment identity and exact funded NAV record ids.
- Produces: funded/blocked admissions whose authority is machine-checkable against the ledger.

- [ ] Add failing tests proving an arbitrary non-empty authority label cannot fund an epoch and the policy covers mapped assignments exactly.
- [ ] Replace free-text authority acceptance with a closed authority kind plus exact source record ids; verify a positive NAV record falls inside the assignment.
- [ ] Enforce continuity only for contiguous mapped epochs with predecessor closing NAV and complete flow coverage.
- [ ] Add negative tests for zero NAV, wrong account/date, missing source record, non-strategy admission, gap, and blocked predecessor.
- [ ] Run the full producer/schema tests and mutate funded-source validation to prove the negative test fails.
- [ ] Commit the admission contract tranche.

### Task 4: Correct observation-interval calculations

**Files:**
- Modify: `tools/account_strategy_manifest.py`
- Modify: `tests/portfolio_control_panel/test_account_strategy_manifest.py`
- Modify: `.codex-bootstrap/mvp-audit-001/nav-hwm-cleanroom/nav_hwm_cleanroom.py`
- Modify: `.codex-bootstrap/mvp-audit-001/nav-hwm-cleanroom/*_test.py`

**Interfaces:**
- Consumes: funded NAV observations plus complete classified flows.
- Produces: exact epoch money identities, observation-linked return/HWM/MaxDD, and diagnostic XIRR.

- [ ] Add literal failing cases for a flow on a non-NAV date, a weekend observation gap, leading zero NAV before funded start, and a later nonpositive return base.
- [ ] Implement `(prior_nav_date, current_nav_date]` flow aggregation and explicit funded-start behavior without calendar-day adjacency assumptions.
- [ ] Extend the independent clean room without importing producer helpers and compare literal expected paths.
- [ ] Run both suites and mutate sign, interval inclusion, funded-start, HWM, and MaxDD branches one at a time to prove detection.
- [ ] Commit production code/tests; retain ignored clean-room evidence with hashes in the audit report.

### Task 5: Retained real authority generation

Task 5 is four sequential review tranches. Do not begin 5A implementation
until an implementation brief for 5A alone has been extracted from this plan.
After each tranche, run a fresh read-only adversarial review and resolve every
validated finding before extracting the next brief. Task 5 never calls
`publish_generation` and does not modify pointer, authority, release, pipeline,
or panel consumption.

#### Task 5A: Strict JSON, immutable snapshots, and receipt-bound coverage v2

**Files:**
- Create: `packages/core/src/quant_engine/core/strict_json.py`
- Create: `schemas/portfolio/account-interval-retrieval-receipt.schema.json`
- Modify: `packages/core/src/quant_engine/core/contracts.py`
- Modify: `packages/core/src/quant_engine/core/__init__.py`
- Modify: `tools/account_strategy_manifest.py`
- Modify: `schemas/portfolio/account-flow-coverage.schema.json`
- Modify: `docs/analytics/account-strategy-manifest.md`
- Modify: `tests/core/test_generation_envelope.py`
- Modify: `tests/portfolio_control_panel/test_account_strategy_manifest.py`

**Interfaces:**
- Consumes: raw bytes at Task 5 authority boundaries and existing path-based
  epoch-producer arguments.
- Produces:
  `strict_json_loads(value: bytes, *, label: str) -> object`,
  `strict_json_object(value: bytes, *, label: str) -> dict[str, object]`,
  frozen `InputSnapshot.capture(path: Path, label: str) -> InputSnapshot`,
  `InputSnapshot.from_bytes(label: str, contents: bytes) -> InputSnapshot`,
  frozen `ManifestCandidateSnapshots`, and
  `build_manifest_candidate_from_snapshots(inputs: ManifestCandidateSnapshots) -> dict[str, Any]`.
- Compatibility: the existing `build_manifest_candidate(...)` captures each
  path once and delegates. `GenerationManifest` identity and serialization do
  not change; `from_json_bytes` gains duplicate-key/nonfinite rejection.
- Coverage v2: `load_flow_coverage` receives a mapping of receipt artifact ID to
  receipt `InputSnapshot`. Version-1 `COMPLETE` is rejected. A legacy conversion
  may emit only `BLOCKED/LEGACY_RECEIPT_UNVERIFIABLE`.

- [ ] **Step 1: Write strict JSON RED tests.** Add literal bytes with duplicate
  top-level and nested keys, invalid UTF-8, `NaN`, `Infinity`, `-Infinity`,
  trailing JSON, and a non-object authority root. Assert the shared loader,
  `InputSnapshot.json_object`, and `GenerationManifest.from_json_bytes` reject
  them without echoing private bytes.

- [ ] **Step 2: Run the strict boundary REDs.** Run
  `python -m pytest tests/core/test_generation_envelope.py tests/portfolio_control_panel/test_account_strategy_manifest.py -k "duplicate or utf or nonfinite or snapshot" -q`.
  Expected: failures because the shared loader and snapshot-native API do not
  exist and duplicate keys are silently accepted.

- [ ] **Step 3: Implement the single strict loader and immutable snapshot
  value.** Use `object_pairs_hook` to reject a repeated member at every nesting
  depth and `parse_constant` to reject nonfinite values. Keep snapshot source
  paths diagnostic-only and make `contents`, SHA-256, and size immutable.

- [ ] **Step 4: Split the producer boundary.** Move the body of the current
  path-based producer behind `build_manifest_candidate_from_snapshots`. Add
  snapshots for ledger, ledger identity, baseline, classified flows,
  classification policy, admission policy, coverage, every retrieval receipt,
  and calculation code. The snapshot-native function must not call `Path.open`,
  `Path.read_bytes`, `resolve`, or any filesystem read.

- [ ] **Step 5: Write the post-snapshot mutation RED.** Capture all selected
  paths, replace their bytes, install a path-read trap, and call the
  snapshot-native producer. Assert exact equality to the pre-mutation candidate
  and zero reads after capture. Add a positive control showing that mutation
  before capture changes identity or fails a pinned hash.

- [ ] **Step 6: Define receipt v2 and coverage v2 REDs.** Use closed synthetic
  receipts binding issuer code, provider generation and raw artifacts, internal
  account/source-account hash, requested and covered inclusive intervals,
  required/completed/unavailable categories, request/response page chains,
  terminal page evidence, exact completeness, and `truncated: false`. Add REDs
  for version-1 complete, missing receipt bytes, wrong account, early/late
  boundary, category gap, nonterminal pagination, wrong source generation/hash,
  receipt overlap, and receipt gap. Add one fully valid synthetic receipt.

- [ ] **Step 7: Implement coverage v2 fail-closed validation.** Require exactly
  one coverage disposition per mapped assignment. A complete row must resolve
  to present strict receipt bytes and pass every account/interval/category/
  pagination/source check. Use the closed blocker codes in the design. Keep the
  issuer allowlist empty unless an exact retained issuer code and category
  contract are independently present.

- [ ] **Step 8: Update every in-repo epoch coverage fixture and caller.** The
  seam must have no producer consumer left on version 1. Synthetic complete
  fixtures carry valid receipt bytes; blocked fixtures carry null receipt IDs
  and precise closed blockers.

- [ ] **Step 9: Verify 5A.** Run
  `python -m pytest tests/core/test_generation_envelope.py tests/portfolio_control_panel/test_account_strategy_manifest.py -q`,
  `python -m ruff check packages/core/src/quant_engine/core/strict_json.py packages/core/src/quant_engine/core/contracts.py tools/account_strategy_manifest.py tests/core/test_generation_envelope.py tests/portfolio_control_panel/test_account_strategy_manifest.py`,
  and `git diff --check`. Mutate the receipt containment predicate and duplicate
  key hook one at a time and prove the targeted tests fail before restoring.

- [ ] **Step 10: Review and commit 5A only.** Record the exact tests and
  mutation failures, obtain the read-only review, and commit only the files in
  5A with subject `feat: bind epoch inputs and flow receipts`.

#### Task 5B: Atomic non-circular generation bundle

**Files:**
- Create: `schemas/core/generation-inventory.schema.json`
- Create: `schemas/core/generation-receipt.schema.json`
- Modify: `packages/core/src/quant_engine/core/publication.py`
- Modify: `packages/core/src/quant_engine/core/__init__.py`
- Modify: `tests/core/test_generation_envelope.py`

**Interfaces:**
- Consumes: an unchanged `GenerationManifest` and
  `Mapping[str, bytes]` semantic payloads whose path set exactly equals
  `manifest.artifacts[*].artifact_id`.
- Produces:
  `stage_generation_bundle(root: Path, manifest: GenerationManifest, payloads: Mapping[str, bytes]) -> Path`,
  `verify_generation_bundle(root: Path, generation_id: str) -> GenerationBundle`,
  and `read_generation_bundle(root: Path, generation_id: str) -> GenerationBundle`.
- `GenerationBundle` contains the verified manifest, immutable payload bytes,
  detached inventory object/bytes, detached receipt object/bytes, and the
  existing full-directory inventory SHA-256.
- Compatibility: do not change `stage_generation`, `read_generation`,
  `generation_inventory_sha256`, `publish_generation`, or current-pointer
  behavior.

- [ ] **Step 1: Write the non-circular envelope RED.** Build two canonical
  payloads plus an independent report, construct an unchanged
  `GenerationManifest`, and require `generation.json`,
  `generation-inventory.json`, and `generation-receipt.json`. Assert the
  inventory lists `generation.json` and declared payloads but excludes itself
  and the receipt; assert the receipt binds manifest, inventory, report, and
  exact path count.

- [ ] **Step 2: Run the bundle RED.** Run
  `python -m pytest tests/core/test_generation_envelope.py -k bundle -q`.
  Expected: failure because the three additive APIs and control schemas do not
  exist.

- [ ] **Step 3: Implement in-memory bundle validation.** Require canonical JSON
  for generated JSON files, exact artifact hash/size/path equality, portable
  POSIX-relative paths, no empty/`.`/`..` component, no drive/backslash, no
  duplicate, and no collision with the three control names.

- [ ] **Step 4: Implement detached inventory and receipt bytes.** Inventory
  entries are sorted exact path/SHA-256/size records for `generation.json` and
  every payload. The receipt binds generation ID, manifest SHA-256/size,
  inventory SHA-256/size, independent-report artifact identity, expected final
  path count, and envelope schema version. Neither control embeds the existing
  full-directory inventory digest.

- [ ] **Step 5: Implement atomic create-only staging.** Write all files with
  exclusive creation under a sibling `.stage-*` directory, flush them, verify
  the temporary bundle, and perform one directory rename. Append nothing after
  rename. Reuse the bounded transient Windows rename-denial delays.

- [ ] **Step 6: Implement verify/read.** Reject a linked root, generations root,
  generation directory, nested directory, or file; reject any irregular,
  missing, extra, noncanonical, hash/size-mismatched, inventory-mismatched, or
  receipt-mismatched object. Return immutable payload bytes only after full
  verification.

- [ ] **Step 7: Add exact recovery and collision tests.** Cover byte-identical
  idempotence; same ID with any different/missing/extra byte; exception before
  rename; exhausted rename retry; ignored stale `.stage-*` orphan; prior final
  preservation; file, nested-directory, generation-directory, and generations-
  root symlinks. Orphans are never promoted or automatically deleted.

- [ ] **Step 8: Prove legacy behavior is unchanged.** Run the existing
  `stage_generation`, `read_generation`, pointer, and full-directory inventory
  tests without modifying their expected bytes. Assert
  `generation_inventory_sha256` includes both detached controls for a bundle
  and remains external to the bundle bytes.

- [ ] **Step 9: Verify 5B.** Run
  `python -m pytest tests/core/test_generation_envelope.py -q`,
  `python -m ruff check packages/core/src/quant_engine/core/publication.py tests/core/test_generation_envelope.py`,
  and `git diff --check`. Mutate the exact path-set comparison, inventory
  exclusion, and collision comparison one at a time and prove their tests fail.

- [ ] **Step 10: Review and commit 5B only.** Record exact recovery/symlink/
  idempotence evidence, obtain the read-only review, and commit only 5B files
  with subject `feat: stage verified generation bundles`.

#### Task 5C: Perimeter, private generator, independent verifier, and display

**Files:**
- Create: `tools/build_account_manifest_generation.py`
- Create: `tools/verify_account_manifest_generation.py`
- Create: `tools/private_account_display.py`
- Create: `schemas/portfolio/account-assignment-perimeter.schema.json`
- Create: `schemas/portfolio/account-manifest-independent-verification.schema.json`
- Create: `tests/portfolio_control_panel/test_account_manifest_generation.py`
- Modify: `schemas/portfolio/account-strategy-manifest.schema.json`
- Modify: `contracts/portfolio-financial-mvp-source-v1.json`
- Modify: `schemas/portfolio/financial-mvp-source-contract.schema.json` only if
  the new epoch-generation source role cannot validate under the existing
  schema
- Modify: `docs/analytics/account-strategy-manifest.md`
- Modify: `docs/data-rollforward.md`

**Interfaces:**
- Consumes: pinned provider-ledger/baseline generation IDs, immutable snapshots,
  accepted classification policy, optional receipt snapshots, an output root,
  and a separate private redacted display-identity snapshot.
- Produces the exact verifier-subject payload set from the design, a finalized
  independent report that binds that subject set but excludes itself, an unchanged
  `GenerationManifest` with `CoverageClaim.PARTIAL` and
  `VerificationStatus.VERIFIED`, an atomically staged bundle, and private human
  reports outside the generation directory.
- `IndependentAccountManifestVerifier.verify(selected_snapshots, subject_payloads) -> bytes`
  returns canonical report bytes binding the exact sorted subject path/hash/size
  entries and semantic digest; `subject_payloads` must not contain the report.
  The verifier may not import the producer or generator.
- `PrivateDisplayIdentityAdapter.from_snapshot(snapshot)` accepts only internal
  account ID, provider, approved nickname, and exact four-digit last four.

- [ ] **Step 1: Write the real-shape first RED.** Create a synthetic fixture
  with 18 accounts and 26 assignment rows partitioned 15 mapped, 7 historical,
  and 4 reserve. Require 26 perimeter rows, one mapped coverage/admission/output
  disposition per mapped assignment, 15 blocked mapped rows, zero numeric rows,
  and every semantic payload declared by `generation.json`.

- [ ] **Step 2: Run the first RED.** Run
  `python -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py -k real_shape -q`.
  Expected: failure because the perimeter schema, generator, verifier, display
  adapter, and payload bundle do not exist.

- [ ] **Step 3: Implement the perimeter artifact.** Bind the selected baseline,
  full perimeter identity, account/assignment/partition counts, fact bijection,
  unassigned facts, and one row per assignment with internal account/provider,
  disposition, nullable strategy, dates, eligibility, and exact NAV/unavailable-
  NAV/raw-flow/classified-flow counts. Reject any fact outside exactly one full
  interval.

- [ ] **Step 4: Implement deterministic authority derivation.** From the
  captured ledger/policy/baseline bytes, generate selected-input identities,
  redacted calculation inputs, classified flows, coverage v2, mapped admission,
  code identities, and the snapshot-native epoch candidate. With the empty
  issuer allowlist, every real mapped coverage row is blocked and no numeric
  field or trace may appear.

- [ ] **Step 5: Implement the independent verifier and fixed construction
  order.** First derive and canonicalize every subject payload except
  `verification/independent-verification.json`. Independently reparse the
  selected snapshots and exact subject map, reconcile every perimeter and
  classified fact, validate admissions, and compare exact paths/canonical
  bytes. Finalize a report containing verifier code identity, sorted subject
  path/hash/size entries, their canonical semantic digest, exact counts, one
  blocker chain per mapped assignment, numeric candidate/comparison/discrepancy
  counts, and invariants. Then build `GenerationManifest` over the exact subject
  set plus the finalized report. Finally let the detached bundle verifier bind
  the complete manifest-declared set. No earlier stage includes the report in
  its own subject.

- [ ] **Step 6: Enforce verifier independence.** Parse the verifier module AST
  and fail if it imports `tools.account_strategy_manifest`,
  `tools.build_account_manifest_generation`, or a producer calculation helper.
  Add an import-spy runtime positive control.

- [ ] **Step 7: Make the zero-numeric proof non-vacuous.** For the 15-blocked
  fixture, mutate each coverage blocker, admission, output status, assignment
  key, independent comparison/count, and insert each numeric field/trace;
  require failure. Add a separate one-numeric-row synthetic fixture and mutate
  every money, return, HWM, MaxDD, XIRR diagnostic, observation link, and
  capital-flow trace value; require the independent calculation to disagree.

- [ ] **Step 7a: Prove report construction is non-circular.** Mutate a subject
  path, SHA-256, size, canonical semantic digest, finalized report byte, and the
  report artifact entry in `generation.json`; require failure. Attempt to place
  the report in its own subject map and require an explicit rejection. Remove
  the finalized report from `generation.json` and require the bundle path-set
  check to fail.

- [ ] **Step 8: Add classification and NAV mutation proofs.** Mutate classified
  event identity, amount, sign, date, disposition, and raw-record binding.
  Mutate funded NAV identity, account, date, availability status, and value.
  Each mutation must fail its exact independent invariant.

- [ ] **Step 9: Implement the private display adapter and sentinel tests.**
  Reject unknown account-number/routing/external-identifier keys, non-four-digit
  last four, duplicate IDs, provider mismatch, and any sequence that normalizes
  to five or more digits. Plant a unique forbidden full-number sentinel and
  prove absence from bundle bytes, human output, logs, exceptions, and test
  failure text. Prove nickname/last-four changes alter only human reports and
  not the generation ID.

- [ ] **Step 10: Render private human reports outside machine identity.** Write
  a 15-row mapped table and 26-row perimeter reconciliation outside
  `generations/<generation_id>`. Lead with provider, approved nickname/last-
  four, strategy name, effective dates, NAV range/count, coverage blocker,
  admission blocker, and numeric disposition. Never display internal IDs as the
  account label.

- [ ] **Step 11: Bind the financial source contract without private values.**
  Add the epoch-generation source and required artifact roles, leave its current
  generation unset, and state that `PARTIAL + VERIFIED` is not publication
  ready. Document the bundle layout, command boundary, and recovery behavior.

- [ ] **Step 12: Prove determinism and no external calls.** Build twice from the
  same snapshots and compare every subject payload, finalized independent report,
  generation ID, detached inventory, detached receipt, and full-directory
  inventory digest byte-for-byte. Trap provider/network/browser functions and
  assert zero calls.

- [ ] **Step 13: Verify 5C.** Run
  `python -m pytest tests/portfolio_control_panel/test_account_manifest_generation.py tests/portfolio_control_panel/test_account_strategy_manifest.py tests/core/test_generation_envelope.py tests/core/test_portfolio_financial_mvp_contract.py -q`,
  `python -m ruff check tools/build_account_manifest_generation.py tools/verify_account_manifest_generation.py tools/private_account_display.py tests/portfolio_control_panel/test_account_manifest_generation.py`,
  the repository schema checks for every new schema, and `git diff --check`.

- [ ] **Step 14: Review and commit 5C only.** Record every mutation proof,
  obtain the read-only review, and commit only 5C code/contracts/tests/docs with
  subject `feat: build independently verified epoch authority`.

#### Task 5D: Retained real build and exhaustive private evidence

**Files:**
- Create ignored: `.codex-bootstrap/mvp-audit-001/account-manifest-generation/generations/<generation-id>/**`
- Create ignored: `.codex-bootstrap/mvp-audit-001/account-manifest-audit/<generation-id>/mapped-assignments.md`
- Create ignored: `.codex-bootstrap/mvp-audit-001/account-manifest-audit/<generation-id>/perimeter-reconciliation.md`
- Create ignored: `.codex-bootstrap/mvp-audit-001/account-manifest-audit/<generation-id>/verification-summary.md`
- Create ignored: `.codex-bootstrap/mvp-audit-001/account-manifest-audit/<generation-id>/commands.txt`
- Modify tracked: none

**Interfaces:**
- Consumes the exact pinned provider-ledger generation
  `4b3de4f4656c236c3334eb76fb091b73c4a1a8afdbc9f3893485ed97403e0e36`
  and baseline generation
  `20709bfd5f9a5846879854bf3166c11223a100cc62fcb13fe0b76040f881b542`,
  accepted Task 5C code, and a private redacted display crosswalk.
- Produces one private create-only bundle and exhaustive private acceptance
  evidence. It does not create a tracked change or an empty commit.

- [ ] **Step 1: Recheck pinned bytes before capture.** Verify provider-ledger
  SHA-256 `315471b602bb4ce61cd854195162708ab188f6e08d6597deb9b9da11ca5bce44`
  and size `22484714`; verify baseline SHA-256
  `0a9b5b243dc16786e41099dde6d02cd0c03431534bc50376352837f36d4734f4`
  and size `8794134`. Resolve neither `CURRENT.json`.

- [ ] **Step 2: Validate the private display crosswalk.** Require all 18 machine
  accounts to resolve to provider, approved nickname, and last four. If the
  strict redacted crosswalk is absent or invalid, retain the machine bundle but
  report `BLOCKED_PRIVATE_DISPLAY_CROSSWALK_MISSING`; do not infer labels or
  claim Task 5 accepted.

- [ ] **Step 3: Run one local snapshot-only build.** Use the pinned generation
  IDs, accepted policy, existing local receipt candidates only, the private
  output root, and the private display crosswalk. Record the exact command and
  environment-safe relative paths. Do not call a provider, browser, credential,
  pointer, publication, GitHub, or trading function.

- [ ] **Step 4: Verify the retained disposition.** Require 18 accounts,
  26 perimeter rows, the 15/7/4 partition, 15 mapped dispositions, 15 blocked
  mapped rows, zero numeric rows, `CoverageClaim.PARTIAL`,
  `VerificationStatus.VERIFIED`, and zero independent discrepancies. If a v2
  complete receipt is unexpectedly discovered, stop acceptance and report its
  exact local path, account, interval, categories, pagination, issuer, and
  selected-source identity for review.

- [ ] **Step 5: Run the exhaustive mutation matrix against unchanged retained
  snapshots.** Execute every snapshot, receipt, perimeter, classification, NAV,
  blocker, positive-numeric, verifier, payload, inventory, receipt, collision,
  recovery, symlink, privacy, and no-network mutation test declared in the
  design. Record the exact command, test count, and result once per unchanged
  candidate.

- [ ] **Step 6: Rebuild identically.** Run the same build into a second private
  root and prove byte identity for all semantic artifacts, independent report,
  generation ID, detached controls, and full-directory inventory digest.

- [ ] **Step 7: Write the private acceptance evidence.** Include the 15-row
  mapped human table, 26-row perimeter table, source generations/hashes/sizes,
  exact blocker counts, explicit `zero numeric rows`, independent result,
  discrepancy count, generation ID, manifest hash, detached inventory/receipt
  hashes, full-directory inventory digest, commands, recovery state, and known
  gaps. Do not call the result a functional MVP-2.

- [ ] **Step 8: Review the exact retained bundle.** Obtain a fresh read-only
  adversarial review of the exact generation and report hashes. Re-run no gate
  unless the candidate or relevant evidence changes.

- [ ] **Step 9: Close the Task 5 boundary without a commit.** Confirm no private
  value is tracked, `git status --short` contains no Task 5D tracked change,
  and the reviewed Task 5C commit remains the implementation head. Record that
  commit and the private bundle identities as the Task 5 handoff to Task 6.

### Task 6: Financial release and browser byte lineage

**Files:**
- Modify: `packages/core/src/quant_engine/core/publication.py`
- Modify: `packages/core/src/quant_engine/core/pipeline.py`
- Modify: `packages/core/src/quant_engine/core/portfolio_pipeline.py`
- Modify: `tools/portfolio_control_panel.py`
- Modify: `tests/core/test_generation_envelope.py`
- Modify: `tests/core/test_portfolio_pipeline_materialize.py`
- Modify: `tests/portfolio_control_panel/test_portfolio_control_panel.py`
- Modify: `apps/portfolio-control-panel/app.js`

**Interfaces:**
- Consumes: the exact Task 5D bundle identity through
  `read_generation_bundle`, plus the exact
  `outputs/account-strategy-manifest.json` bytes from that verified bundle.
- Produces: release/workbook/dashboard/current-pointer candidates that all bind
  the same verified bundle identity and exact manifest bytes.

- [ ] Add a failing composed test requiring a verified Task 5 bundle in financial mode and exact account-manifest byte identity at each stage; a bare legacy `read_generation` result is insufficient for this source role.
- [ ] Integrate verified-bundle consumption and publication checks in Task 6, then thread the bundle identity and exact manifest bytes through preparation, materialization, workbook proof, dashboard build, and publication authorization.
- [ ] Add single-byte/source/value/provenance mutation rejection tests and non-financial omission positive control.
- [ ] Add browser assertions for human labels, exact null/blocker rendering, 13-column shape, and formatted parity with the manifest.
- [ ] Run core, panel, Node interaction, and schema tests; mutate each authority comparison to prove the composed test fails.
- [ ] Commit the release lineage tranche.

### Task 7: Acceptance and governed handoff

**Files:**
- Modify: `.codex-bootstrap/mvp-audit-001/epoch-manifest-root-cause-report.md`
- Create: `.codex-bootstrap/mvp-audit-001/real-epoch-manifest-acceptance.md`

**Interfaces:**
- Consumes: exact branch head, retained test outputs, hostile reviews, the exact reviewed Task 5 bundle identity, and the exact account-manifest bytes consumed by Task 6.
- Produces: an evidence-linked MVP-2 readiness disposition.

- [ ] Run bounded producer, baseline, core, panel, provider, workbook, browser, Ruff, mypy, schema, and security checks from a clean exact head.
- [ ] Dispatch independent hostile reviews of calculation, authority lineage, security, and rendered cells; resolve every validated finding through a new TDD tranche.
- [ ] Prove Git status, Task 5 bundle/manifest bytes, Task 6 consumed bytes, generated hashes, and reviewed commit head are identical; then run required remote Quality, Security, Data boundary, and Issue guard contexts.
- [ ] Report human-readable account/strategy rows, every numeric comparison, blocked providers/intervals, and the exact number of current fully populated rows.
- [ ] Create/update the governed GitHub issue and PR only within the existing user-approved repository workflow; do not merge without required approvals and exact-head green evidence.

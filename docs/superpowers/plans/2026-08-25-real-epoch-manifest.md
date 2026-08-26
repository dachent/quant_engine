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

**Files:**
- Create: `tools/build_account_manifest_generation.py`
- Create: `tests/portfolio_control_panel/test_account_manifest_generation.py`
- Modify: `contracts/portfolio-financial-mvp-source-v1.json`
- Modify: `docs/data-rollforward.md`

**Interfaces:**
- Consumes: pinned provider ledger/baseline, classified-flow policy, source receipts, and exact assignment perimeter.
- Produces: a content-addressed generation containing authorities, manifest, and verification report.

- [ ] Add a failing integration test that mirrors the real mixed baseline and requires every generated artifact hash to be bound by the generation manifest.
- [ ] Implement deterministic classified-flow, coverage, admission, manifest, and verification generation using immutable input snapshots.
- [ ] Add fail-closed tests for providers without machine-verifiable flow completeness; their mapped epochs remain blocked.
- [ ] Run the generator against the pinned local real data without provider calls; retain only governed non-private artifacts in approved roots.
- [ ] Independently compare every produced real numeric field and store exact discrepancies/blockers in the local audit report.
- [ ] Commit code/contracts/tests/docs, not private generated values.

### Task 6: Financial release and browser byte lineage

**Files:**
- Modify: `packages/core/src/quant_engine/core/pipeline.py`
- Modify: `packages/core/src/quant_engine/core/portfolio_pipeline.py`
- Modify: `tools/portfolio_control_panel.py`
- Modify: `tests/core/test_portfolio_pipeline_materialize.py`
- Modify: `tests/portfolio_control_panel/test_portfolio_control_panel.py`
- Modify: `apps/portfolio-control-panel/app.js`

**Interfaces:**
- Consumes: verified account-manifest generation identity.
- Produces: release/workbook/dashboard/current-pointer candidates that all bind the same manifest bytes.

- [ ] Add a failing composed test requiring the real-shape generated manifest in financial mode and exact byte identity at each stage.
- [ ] Thread the generation identity through preparation, materialization, workbook proof, dashboard build, and publication authorization.
- [ ] Add single-byte/source/value/provenance mutation rejection tests and non-financial omission positive control.
- [ ] Add browser assertions for human labels, exact null/blocker rendering, 13-column shape, and formatted parity with the manifest.
- [ ] Run core, panel, Node interaction, and schema tests; mutate each authority comparison to prove the composed test fails.
- [ ] Commit the release lineage tranche.

### Task 7: Acceptance and governed handoff

**Files:**
- Modify: `.codex-bootstrap/mvp-audit-001/epoch-manifest-root-cause-report.md`
- Create: `.codex-bootstrap/mvp-audit-001/real-epoch-manifest-acceptance.md`

**Interfaces:**
- Consumes: exact branch head, retained test outputs, hostile reviews, and real-data verification report.
- Produces: an evidence-linked MVP-2 readiness disposition.

- [ ] Run bounded producer, baseline, core, panel, provider, workbook, browser, Ruff, mypy, schema, and security checks from a clean exact head.
- [ ] Dispatch independent hostile reviews of calculation, authority lineage, security, and rendered cells; resolve every validated finding through a new TDD tranche.
- [ ] Prove Git status, generated hashes, and reviewed commit head are identical; then run required remote Quality, Security, Data boundary, and Issue guard contexts.
- [ ] Report human-readable account/strategy rows, every numeric comparison, blocked providers/intervals, and the exact number of current fully populated rows.
- [ ] Create/update the governed GitHub issue and PR only within the existing user-approved repository workflow; do not merge without required approvals and exact-head green evidence.

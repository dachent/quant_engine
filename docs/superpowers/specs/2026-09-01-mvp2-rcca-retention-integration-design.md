# MVP-2 RCCA, Retention, and Integration Design

**Date:** 2026-09-01

**Governing issue:** #374 (`MVP-AUDIT-001`)
**Approved outcome:** Preserve the useful work, eliminate workspace ambiguity,
merge validated code through its governing issues, and retain one durable RCCA
and one intelligible local evidence structure.

## Design boundary

This cleanup produces three outcomes only:

1. an evidence-linked RCCA for the operator-visible control-panel failure;
2. one canonical Git checkout plus deliberately separated local data and
   evidence;
3. reviewed GitHub integration of relevant code without mixing unrelated issue
   scopes.

It does not authorize provider access, data refreshes, broker effects, trading,
or silent publication/pointer movement. Missing financial evidence remains
unavailable rather than being inferred.

## Repository integration

`_quant_engine\repo` is the canonical checkout. Existing named worktrees are
recovery or integration inputs until their unique state is either merged or
preserved by a reachable remote ref.

Integration is sequential:

1. Treat PR #383 as completed evidence: exact head `41e2838` was merged to
   `main` as `1bc1936` after all required checks and both review threads passed.
2. Review the interrupted Schwab working tree and stash separately. Only code
   required by its governing issue and backed by behavioral tests is eligible
   to merge.
3. Integrate WCH-MIG-001 (#307) as its own pull request.
4. Rebase the four WCH-CLI-001 (#378) commits onto the accepted migration and
   integrate them as a second pull request.

The WCH migration and CLI may not be collapsed into one pull request because
the governing issue contracts explicitly separate extractor internals from the
CLI. The modify/delete conflict around `web_contracts/extract.py` is resolved
against the accepted post-#383 API, never by automatic preference.

## Local retention structure

The OneDrive root has four distinct responsibilities:

```text
_quant_engine/
  repo/                     canonical Git checkout
  dataPortfolio/            canonical portfolio data authority
  data*/                    provider-local governed inputs
  evidence/
    manifests/               hash-bound retention and validation records
    MVP-AUDIT-001/           retained audit and recovery records
      design-reviews/        historical design and hostile reviews
      legacy-session-notes/  historical diagnostics and CI evidence
      recovery/              non-mergeable interrupted work
  docs/
    historical/2026-08/     dated local decision and handoff documents
  .m2/ .q/ .t3/             root-level path-bound evidence
  quant_engine_mvp_audit_001_20260824/
    .q/                      selected path-bound panel candidate and receipt
```

The canonical retained schema-21 staged panel candidate is candidate
`7c516d40ee5b58a4c387208bf253575a05d12a2dbc8c8361c3d10ac118d30b92`
under audit bundle `4ab25c44a5dd`. The exact source-to-destination inventory
must name that candidate, its build/verification report, independent
recomputation, and every recovery artifact retained with it. No browser-QA
manifest located in the current evidence binds this candidate ID; its
candidate-specific browser-QA status is `NOT_FOUND_UNBOUND` and no QA claim may
be inferred from browser trees for other candidates.

Every retained item receives an inventory record containing source path,
destination path, SHA-256, size, role, authority status, sensitivity class,
retention reason, duplicate-of relationship, remote reachability, disposition,
and recovery action. Inventory paths are sanitized labels when a real path
would disclose private information.

Path-bound evidence is not moved. Existing authorization receipts bind exact
resolved output, candidate, and QA paths, so even a byte-perfect relocation
would invalidate semantic verification. Such bundles remain at their exact
legacy paths and are registered in the root evidence index. If a copy is made
under `evidence/`, it is labelled a historical snapshot and may not claim that
the original authorization receipt still validates. Governed provider data
also remains in place.

Evidence without path-bound authorization may move only after a pre-move
SHA-256 inventory and a byte-identical post-move inventory. Relative subtrees
move intact. No duplicate is removed until the exact inventory record proves
which retained copy replaces it.

The following are not moved until their active branch closes:

- the audit checkout's `.q` and `.superpowers` fixtures used by tests;
- its staged panel candidate;
- the interrupted Schwab source, tests, and stash.

Reproducible caches and duplicate synthetic test trees may be removed only
after the exact retained inventory and covering tests prove they are
unnecessary.
Linked Git worktrees are deregistered through Git only after cleanliness,
zero-stash, remote-reachability, and evidence-retention checks. A primary
checkout created with a centralized separate Git directory has no Git
`worktree remove` operation; after the same gates and exact approval, its
working directory may be removed while the centralized Git directory is
retained as recovery. Any inventory row classed as path-bound evidence blocks
either removal regardless of Git status until a replacement receipt or an
explicit historical-snapshot disposition removes that dependency.

Those proof gates do not authorize deletion. This cleanup's owner approval is
limited to the exact named root artifacts and superseded documents recorded on
issue #374, with the manifest or Git recovery procedure stated there. Before
each destructive batch, the issue record must name the exact targets, effect,
and recovery procedure. Any target outside that recorded set requires fresh
owner approval; data roots and path-bound evidence are not deletion targets.

## RCCA contract

The RCCA identifies:

- the operator-visible failure;
- direct and contributing causes;
- a recursive why-chain ending at a controllable system cause;
- legitimate evidence gaps that must not be mislabeled as defects;
- corrective and preventive controls;
- proof required to close the RCCA.

The fundamental cause must be expressed at the release boundary: candidate
implementation, evidence, publication, and operator presentation were allowed
to achieve separate local success states because issue closure and the product
claim did not require the existing release path plus post-publication
acceptance against the actual stable launcher.

## Verification

Completion requires all of the following:

- every retained source-code commit is reachable from a remote branch or
  merged `main`;
- every relevant pull request is green at its exact head, has no unresolved
  review conversation, and reports no unmet merge rule;
- the canonical checkout is clean and equals remote `main`;
- pre/post-move evidence inventories match byte-for-byte;
- no provider data, private account identifiers, private financial values, or
  identifying filenames enter Git, GitHub, documentation, logs, or fixtures;
- the control-panel RCCA is committed and linked from issue #374;
- the stable launcher state is reported honestly. Code integration alone must
  not be described as publication.

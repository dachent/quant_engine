# Coordinated release publication

`CORE-PUB-002` adds a local transaction boundary around multiple accepted data
roots and their companion entry points. A release binds the exact code commit,
verification-gate evidence, proposed generation manifests, predecessor
pointers, and companion-file hashes into one content-addressed manifest.

Preparation is non-mutating. It validates every proposed generation and its
complete regular-file inventory, captures
the predecessor graph and companion recovery bytes, then writes immutable
release evidence with a `PREPARED` journal. Commit obtains one coordination
lock, rejects changed inputs or pointers, advances each target, publishes its
companions, and verifies the resulting graph. Only then does it atomically
replace `CURRENT_RELEASE.json`, the sole reader-visible coordinated authority,
and write the terminal `COMMITTED` journal. Readers use `read_current_release`;
if root pointers are temporarily mixed, validation fails closed rather than
representing a mixed graph as accepted.

If any commit step fails, the journal records the applied steps and restores
those steps in reverse order. A successful recovery ends at `ROLLED_BACK`; a
recovery failure ends at `RECOVERY_FAILED` and requires operator intervention.
The last accepted generations remain immutable in either case.

## On-disk contract

```text
<coordination-root>/
  CURRENT_RELEASE.json                 # one validated coordinated authority
  release.lock                         # exists only during a commit
  releases/<release-id>/
    release.json                       # immutable prepared identity
    journal.json                       # atomic state and applied-step record
    recovery/<companion-id>.bin        # exact predecessor companion bytes
```

The release manifest is defined by
`schemas/core/release-manifest.schema.json`; coordinated authority is defined
by `schemas/core/release-authority.schema.json`; the mutable recovery state is
defined by `schemas/core/release-journal.schema.json`. `CURRENT.json` remains
the local authority pointer inside each data root and its schema-v2 inventory
hash detects added, removed, or modified generation files during ordinary
consumption. Schema-v1 releases and pointers remain readable as legacy evidence.

## Required proof

A production rollforward must retain:

- the exact repository commit;
- hashes of every required gate result;
- the prepared release ID and predecessor graph;
- a `COMMITTED` journal;
- a validated `CURRENT_RELEASE.json` commit point;
- post-commit verification of every pointer and companion; and
- a later recovery rehearsal proving restoration of a complete predecessor
  graph after an injected mid-commit failure.

Repeated commit calls revalidate the complete terminal graph before returning.
Root IDs may not alias the same resolved location, and a coordination root may
not alias a target root. Atomic replace operations flush their file content and
fsync the containing directory where the operating system permits.

The control-panel publisher also compensates its local pointer and stable
launcher if launcher publication fails. The complete source-to-panel workflow
uses the coordinated release boundary so report, analytics, weekly-return, and
panel authorities cannot silently diverge.

## Directory lease boundary

Pathname replacement can redirect a coordinated transaction after a directory
has been checked, and on Windows a junction makes that cheap. Revalidating
pathnames before individual operations is not enough.

Therefore `commit_release`, `recover_release`, and `rollback_release` acquire a
directory lease on every target root before they mutate state, and revalidate
every lease before each mutating step. A lease holds an open handle and records
the directory identity behind it: volume serial number and file index on
Windows, device and inode elsewhere. A root that is renamed or re-pointed
mid-transaction fails its lease check and raises `ReleaseConflictError` rather
than redirecting a write. This holds on every platform, so these entry points
are available on every platform.

`prepare_release` stages immutable evidence and does not authorize publication.
`publish_generation` remains a separate single-root primitive and does not
carry the coordinated hostile-writer guarantee.

## Provider roots and unusable predecessor pointers

`REL-PROVIDER-ROOTS-001`: a release is restorable only if the provider inputs it
depends on are in the graph it records. The provider roots (`dataAlpaca`,
`dataFidelity`, `dataIBKR`, `dataMatrixport`, `dataPortfolio123`, `dataQuantbase`,
`dataSchwab`) and `dataEpochManifest` sit beside `dataPortfolio`, and
`portfolio_pipeline_materialize.py production-spec --provider-generations` makes them
release targets under the root ids `provider-<name>` and `epoch-manifest`.

- Every provider generation must equal, by id, manifest hash and catalog hash, what
  the selected provider ledger recorded in `provider_generations`. The ledger's own
  qualification decides publication: `PUBLISHED_COMPLETE`, `STAGED_COMPLETE` and
  `FINITE_STATIC` generations become `CURRENT`; an `ACCEPTED_PARTIAL` generation is
  immutable evidence, is retained in its root through a non-target generation node,
  and is never published (`read_current` refuses a PARTIAL generation by design).
- The epoch-manifest generation must be the one the recipe binds.
- `tools/validate_release_targets.py` writes the findings every target needs:
  provider roots are judged against the same release's provider ledger, the epoch
  manifest against the account-manifest digests the panel pins
  (`release.provider_binding`).

A provider root whose own `CURRENT.json` dangles at a generation directory that no
longer exists, or is schema v1, cannot be recorded as a predecessor pointer graph. The
governed `predecessor-pointer-recovery` gate admits exactly those roots. The production
spec derives it from disk; the pipeline re-derives it before preparing the release and
refuses a gate that names a healthy root, omits an unusable one, or carries different
pointer bytes or refusal text. The prepared release records the pointer bytes it found
and a rollback restores them verbatim. A successor release may add roots: the
predecessor graph is judged over the roots it bound.

`tools/check_data_roots.py` is the read-only gate: every data root resolves through
`read_current`, except an evidence-only root (`dataIBKR` while its generation is
qualified PARTIAL), which must retain hash-verified generations and carries no pointer
a consumer may follow. Once a COMPLETE generation is published the pointer is followable
and the root is judged like any other.

`tools/verify_release_restore.py` proves a committed release is reproducible from the
durable stores: it copies only the bound generations (and any generation the spec
retains as non-target evidence) into an empty clean-room directory, re-derives every
pointer with `publish_generation`, and requires `read_current` to resolve each restored
root to exactly the generation, manifest hash and inventory hash the release bound;
`--expect-sums` additionally requires every row of a `SHA256SUMS` file (for example the
preserved bound-generation zips) to be found by hash among the restored files. A release
also binds each root's directory identity, so `read_current_release` cannot pass in a
different location by design: adopting restored roots needs a new release over them.

## Data-only gate profile (default off, owner approval required)

`DATA_ONLY_V1` lets a real-roots `data_pipeline.py execute --commit --gate-profile data-only`
present a *reuse receipt* instead of a fresh replica ceremony receipt. It replaces exactly one
check, `verify_replica_receipt`; validator findings, A/B derivers, telescoping checks, browser and
interaction QA, payload proofs, pointer snapshots and post-release verification are unchanged and
still run every release. Without the flag the commit path is byte-identical to before.

Enabling needs `--gate-profile-approval <file>`: an owner approval record written with
`tools/gate_profile.py seal-approval` only after the owner's direct ruling (`profile`,
`approved_by=owner`, `approved_on`, `statement`, `max_consecutive_reuse`). A missing or invalid
record is a hard refusal. Every other failed condition falls back, automatically, to the full
replica receipt check and writes `gate-profile-decision.json` beside the spec (`llm_review:
REQUIRED` on fallback, `NOT_REQUIRED` when the profile engaged).

The profile engages only when ALL hold: the tool identity (HEAD commit and tree, `uv.lock`, Python
runtime, clean worktree; `core.gate_profile.tool_identity`) is byte-identical to the one the reuse
receipt recorded; the previous release was committed at this code commit (so the first release
after a code merge always falls back); the receipt vouches for the release that is COMMITTED right
now; the root-id set, node count and target count are unchanged; the number of consecutive reuses
is below the owner's limit; and the leaf diff against the previous release
(`core.release_diff.explain_release`) has no unexplained change. A changed number is explained
only by a changed input generation in its target's `depends_on` ancestry; a derived target that
changed with no changed ancestor, an added or removed key or file, a type or boolean change, or
changed wording (text differing beyond digits) is unexplained.

Procedure per release: (1) full ceremony once, `tools/release_replica.py ceremony`; (2) after the
real commit and `post-release-verify`, `tools/gate_profile.py issue --ceremony-receipt <replica
receipt> --post-release-verify-receipt <receipt> --clean-room <dir> --out <reuse receipt>` (runs the
clean-room restore; refuses if it fails); (3) next data-only release commits with `--gate-profile
data-only --gate-profile-approval <file> --reuse-receipt <reuse receipt>`; (4) after it, `issue
--previous-reuse-receipt <that receipt>` extends the chain. `tools/gate_profile.py select` previews
the decision without committing. Run the four-family review when the decision record says
REQUIRED. Receipts and approvals are tamper-evident, not forgery-proof.

What it gives up, in plain terms: no rehearsal of this week's commit on a copy (a commit-path
refusal is found on the real commit and rolls back), no fresh injected-failure recovery rehearsal,
no fresh semantic review while every change is input-explained, and lineage is coarse (one changed
upstream input explains all downstream changes).

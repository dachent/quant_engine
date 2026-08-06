# Governed data rollforward

`DATA-PIPE-001` turns an accepted private source-to-panel dependency graph into
one explicit, resumable release. The pipeline specification is private because
it contains absolute data paths. It names every source artifact, immutable
generation, dependency, gate, release target, and companion entry point by
exact ID and SHA-256. It never discovers authority through an ambient
`CURRENT.json` pointer.

The repository contract is `schemas/core/data-pipeline.schema.json`; runtime
validation is implemented in `quant_engine.core.pipeline`. The execution state
directory uses the pipeline ID plus a shortened routing prefix to remain safe
under Windows path limits; every receipt and evidence payload retains and
validates the complete specification hash. Receipts contain
only identities, attempts, states, and output hashes—not provider payloads or
credentials.

For the portfolio source-to-panel profile, planning and execution additionally
fail closed unless the named repository has a clean worktree at the exact
release commit, the Python executable and `uv.lock` hashes match, the live
installed-distribution inventory matches, and each command executes a
hash-bound repository entrypoint declared as an input. These checks prevent a
declarative commit field from standing in for the code that actually ran.

## Operator commands

The canonical portfolio root on the governed Windows workstation is
`C:\Tools\quant-engine\data`. It must not resolve below OneDrive or the Git
worktree. Set it once and derive every current-cycle proof, QA, recipe, state,
coordination, generation, and release root from it:

```powershell
$portfolioRoot = [IO.Path]::GetFullPath('C:\Tools\quant-engine\data')
if ($portfolioRoot -like '*\OneDrive -*' -or $portfolioRoot -like '*\repo\*') {
  throw 'Portfolio authority must remain outside synced storage and Git.'
}
$cycleRoot = Join-Path $portfolioRoot 'rollforward\<yyyy-mm-dd-cycle-id>'
$proofRoot = Join-Path $cycleRoot 'proof'
$qaRoot = Join-Path $proofRoot 'panel-qa'
$runRoot = Join-Path $cycleRoot 'run'
```

Historical recipes, specifications, receipts, manifests, and generations stay
byte-identical after a storage move even when they contain the former absolute
path. They are evidence, not operational defaults. Never run a copied historical
specification to prove a cutover: generate a fresh pipeline identity and path-bound
recipe/specification below `$cycleRoot`.

Inventory each immutable generation before authoring the private specification:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py inventory `
  --generation-dir 'C:\absolute\data-root\generations\<generation-id>'
```

Render the exact no-write plan:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py plan `
  --spec 'C:\absolute\private\pipeline-spec.json'
```

Build or verify all nodes and prepare the coordinated release without moving
authority pointers:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py execute `
  --spec 'C:\absolute\private\pipeline-spec.json'
```

After inspecting the prepared release, the same governed command commits all
generation pointers and companion files through the `CORE-PUB-002` journal:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py execute `
  --spec 'C:\absolute\private\pipeline-spec.json' --commit
```

`status` reports node receipts and release state. Re-running the same exact
specification resumes completed local work after re-verifying artifacts,
generations, gates, and companions. A local command may retry at most twice,
but each `execute` invocation performs only one attempt; an explicit resume
consumes the next declared attempt after the failure has been inspected.
A read-only external command requires `--allow-external-read`, has zero retry,
and carries a stable request identity. A timeout or exception records
`UNKNOWN`; resume fails closed and will not send that request again.

If a process terminates after acquiring the lock, after a terminal journal
write, after a release journal reaches `COMMITTING`, or while it is
`ROLLING_BACK`, use the explicit recovery path. It never retries the forward
release:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py recover `
  --spec 'C:\absolute\private\pipeline-spec.json' `
  --confirm-stale-lock
```

`--confirm-stale-lock` is needed only when an exact release-owned lock survived
process termination. A missing, unreadable, linked, or different-release lock
fails closed. Recovery is idempotent when a pointer or companion was already
restored before a second interruption. For `PREPARED`, confirmation removes
the orphan lock and leaves the release prepared. For `COMMITTED` and
`ROLLED_BACK`, confirmation first validates the corresponding terminal graph,
then removes the orphan lock without changing the terminal state.

## Publication and recovery boundaries

- Candidate generations are copied create-only and verified byte-for-byte.
- All release targets must be `VERIFIED` and
  `COMPLETE_WITHIN_DECLARED_BOUNDARY`.
- Preparation records the complete predecessor pointer graph and companion
  recovery bytes without changing authority.
- Commit is coordinated under one lock. Journal intent is durable before every
  pointer or companion effect. Any caught post-mutation failure restores the
  intended pointer and companion steps in reverse order; process termination
  is handled by the explicit recovery command above.
- A current-portfolio release must include the exact freshness-health artifact
  as both a DAG node and a hash gate. A panel cannot be built without it.
  Preparation and commit independently require its evaluation date to equal
  the local operational date and recompute every state from the embedded
  policy, so a release cannot carry yesterday's still-fresh conclusion across
  a stale boundary.
- A committed resume reports `COMMITTED`; it does not mislabel an accepted
  release as merely prepared.
- Provider/network acquisition remains a separate, explicitly authorized
  concern. The baseline rollforward can consume already accepted immutable
  generations without any external call.

The private release evidence must include the pipeline specification, exact
plan, receipts, pipeline-evidence hash, release manifest/journal, gate files,
and post-commit pointer/companion verification. Recovery rehearsal is performed
under `DATA-RECOVERY-001` before this workflow is accepted for perpetual use.

## Private portfolio source-to-panel materialization

`tools/portfolio_release_builder.py` executes the real ledger, baseline,
attribution, analytics, report, native-workbook, and panel builders from one
hash-bound private vertical recipe. `tools/portfolio_pipeline_materialize.py`
then binds those commands, their expected deterministic generation inventories,
the browser-QA evidence, repository commit, locked Python runtime, and stable
panel launcher into the core pipeline specification used by
`tools/data_pipeline.py`.

No stage copies a prior release or substitutes a marker for computation. The
semantic command calculates six bridges from the newly built artifacts:
financial identities, current strategy mappings, ledger-to-baseline-to-report
lineage, analytics/report/panel equality (including HWM), freshness, and exact
generation/release evidence. Quantitative details are retained beside the
strict gate. Any mismatch fails before the release can reach `PREPARED`;
advancing production still requires the separate pipeline commit operation.

The retained 2026-08-03 p28 recipe is archival: its pipeline recipe names a
deleted p27 QA directory. Do not edit that evidence or manufacture a
hash-equivalent replacement. A runnable cycle must rebuild every stage, run
fresh browser and interaction QA, and generate both recipes and the pipeline
specification from the new root.

```powershell
$env:QE_PIPELINE_PYTHON = 'C:\Tools\quant-engine\.venv\Scripts\python.exe'
python tools/portfolio_release_builder.py recipe --provider-ledger-spec <new-root-provider-ledger-spec.json> --baseline-spec <new-root-baseline-spec.json> --base-factor-file <new-root-factor-file.csv> --decomposition-plan-bundle <new-root-plan-bundle> --correlation-source-root <new-root-correlation-source> --legacy-panel <accepted-legacy-panel> --as-of-date <yyyy-mm-dd> --pipeline-id <new-vertical-id> --release-commit <exact-commit> --output (Join-Path $cycleRoot 'vertical-recipe.json')
python tools/portfolio_release_builder.py build --recipe (Join-Path $cycleRoot 'vertical-recipe.json') --output-root $proofRoot
<run browser and interaction QA against the emitted panel candidate>
python tools/portfolio_release_builder.py stage --recipe (Join-Path $cycleRoot 'vertical-recipe.json') --output-root $proofRoot --role panel --qa-dir $qaRoot
python tools/portfolio_pipeline_materialize.py recipe --vertical-recipe (Join-Path $cycleRoot 'vertical-recipe.json') --proof-root $proofRoot --qa-dir $qaRoot --pipeline-id <new-pipeline-id> --output (Join-Path $cycleRoot 'pipeline-recipe.json')
python tools/portfolio_pipeline_materialize.py spec --recipe (Join-Path $cycleRoot 'pipeline-recipe.json') --output-root $runRoot --spec (Join-Path $runRoot 'pipeline-spec.json')
python tools/data_pipeline.py plan --spec (Join-Path $runRoot 'pipeline-spec.json')
python tools/data_pipeline.py execute --spec (Join-Path $runRoot 'pipeline-spec.json')
python tools/data_pipeline.py status --spec (Join-Path $runRoot 'pipeline-spec.json')
```

Before removing an old storage root, require exact source/destination inventories,
then validate every release target with the merged `read_current` runtime and the
complete graph with `read_current_release`. Comparing only `CURRENT.json` and
`generation.json` hashes is insufficient because it does not detect artifact-byte
drift within an otherwise unchanged immutable generation. A mismatch stops the
cutover; publish a new immutable generation and coordinated release instead of
editing history. The stable launcher must then resolve the new root and pass the
same direct-file and interaction QA. Recovery retains the old root until these
checks and the repository change are accepted.

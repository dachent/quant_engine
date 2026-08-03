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

## Operator commands

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
generations, gates, and companions. A local command may retry at most twice.
A read-only external command requires `--allow-external-read`, has zero retry,
and carries a stable request identity. A timeout or exception records
`UNKNOWN`; resume fails closed and will not send that request again.

If a process terminates after a release journal reaches `COMMITTING` or while
it is `ROLLING_BACK`, use the explicit recovery path. It never retries the
forward release:

```powershell
uv run --frozen --no-sync python tools/data_pipeline.py recover `
  --spec 'C:\absolute\private\pipeline-spec.json' `
  --confirm-stale-lock
```

`--confirm-stale-lock` is needed only when an exact release-owned lock survived
process termination. A missing, unreadable, linked, or different-release lock
fails closed. Recovery is idempotent when a pointer or companion was already
restored before a second interruption.

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
- A committed resume reports `COMMITTED`; it does not mislabel an accepted
  release as merely prepared.
- Provider/network acquisition remains a separate, explicitly authorized
  concern. The baseline rollforward can consume already accepted immutable
  generations without any external call.

The private release evidence must include the pipeline specification, exact
plan, receipts, pipeline-evidence hash, release manifest/journal, gate files,
and post-commit pointer/companion verification. Recovery rehearsal is performed
under `DATA-RECOVERY-001` before this workflow is accepted for perpetual use.

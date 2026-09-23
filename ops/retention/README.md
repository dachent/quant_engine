# Retention

Disposition is computed from the committed release, never recalled.

```
python ops/retention/retention.py keep-set --portfolio <dir> [--depth 2]
python ops/retention/retention.py sweep    --portfolio <dir> [--depth 2] [--apply]
python ops/retention/retention.py demo
```

`sweep` reports by default; `--apply` deletes. It refuses to run at all when the
release authority or journal is not `COMMITTED`, because a sweep during a
prepare→commit window can delete the predecessor the in-flight release needs.

## Why the keep-set comes from the release manifest

`core/release.py` re-hashes the **whole predecessor generation directory** of an
in-flight release — `_validate_prepared_state` → `_validate_previous_inventory`
(`:1402`), and again on the rollback path (`:1497`). `generation_inventory_sha256`
hashes every regular file under the directory, so the predecessor must exist and
be byte-identical to what `prepare_release` sealed.

The reason that check exists is sound: prepare→commit is two-phase with a gap,
and re-checking at commit time asks *is my undo still valid* before doing the
irreversible part. On the rollback path it is the postcondition itself — you
cannot assert "the graph is what it was" without hashing what the restored
pointers name.

But the coupling is **transient**. Once the journal reaches `COMMITTED`,
`_validate_committed_state` never reads `previous_pointer` again. And for the
*next* release, `previous_pointer` is the *current* generation, which is kept
regardless. So predecessor retention past a commit is a product decision about
revertability, not a protocol requirement. That decision is `--depth`.

## Depth

* `--depth 1` — current generation only.
* `--depth 2` — current plus the generation it superseded, so a published
  release can be reverted. **This is the intended steady state.**

Depth 2 was deliberately not used for the 2026-09-15 sweep. That release's
predecessor was the pre-alpha-fix generation, where 42 of 52 hybrid alphas were
withheld as `UNAVAILABLE_MODEL_RETURN_BASIS`, and every root also carried the
stale-factor defect in #418. Reverting to it would have republished a known
defect. **A predecessor is worth keeping only when it is a revert target you
would actually accept.**

## What the 2026-09-15 sweep found

The published product is **20 generations, 397 files, 0.50 GB**. Against it:

| | |
|---|---|
| dead generations in the stores | 3,122 — 8.71 GB |
| `.mvp2-t` build attempts | 391 directories — 14.91 GB |
| quarantine HAR | 223 files — 13.54 GB |
| scratch roots | 11.41 GB |
| superseded worktrees | 9.70 GB |

Total across the work folder and scratch was 68.37 GB, of which 41.28 GB (60.4%)
was a repeat of a same-name, same-size file held elsewhere. `provider-ledger.json`
alone existed in over 200 physical copies, because a generation inlines its
inputs by value rather than referencing them by digest.

See #421 for the RCCA and the run-workspace contract this tool belongs to.

## Known limit

`generation_inventory_sha256` seals the **directory**, not the manifest's
declared file set. Any file the filesystem adds — a sync conflict copy, a
`Zone.Identifier` stream, a stray log, a `cldflt` rehydration — invalidates a
generation nobody touched. Inside a SharePoint sync root that gives the
filesystem a vote on release validity, and it is what blocked the 2026-09-14
release. Narrowing the hash to the declared file set would keep every guarantee
above and remove that failure mode.

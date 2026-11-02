# Ledger spec and run observation builder (B1) and derived provider sources (B2)

RELEASE-DRIVER-001, hand-script replacement blocks B1 and B2 (governing issue 623). B1 replaces
`mk_ledger_spec.py` and `patch_ledger_spec.py`; B2 replaces the hand-typed `provider_sources` map
of the run configuration. Input contract: `docs/release-cycle-manifest.md` (the resolved cycle).

## B1: `tools/release_ledger_spec.py build`

```
release_ledger_spec.py build (--resolved-cycle FILE | --manifest M --root K=D ... [--roots-file F])
                             --predecessor-spec SPEC --out-root DIR
                             --provenance-generation-dir DIR [--repo-root DIR] [--plan]
```

For every provider of the ledger's provider catalog set (the tool asks
`provider_ledger.provider_catalog_names()`; a cycle with another provider set is refused) it reads the
resolved row and the generation manifest and builds ONE object per provider:

- the ledger reference: `root`, `generation_id`, `manifest_sha256`, `catalog_sha256`,
  `qualification`;
- the run-observation entry: that same reference plus `generation_as_of_date` (from the manifest),
  `finalized_through` (declared for evidence-file providers; the manifest's date for a finite
  provider) and `finality_evidence {path, sha256, size_bytes}`.

The reference is spread into the observation entry, so the two agree by construction.
Finality per provider: a finite provider's evidence is its generation manifest; the ibkr statement
must be a source artifact of the selected manifest (same sha256 and size); the others use the
pinned native file. `captured_at_utc` is the cycle manifest's value normalised to UTC with its microseconds kept (see "Capture timestamp" in `docs/release-cycle-manifest.md`; a value finer than a microsecond is refused, never truncated), `financial_cutoff` the cutoff,
`policy_contract_sha256` the hash of the contract file in the repository. `--repo-root` defaults to the checkout the tool itself runs from, and the spec's `financial_mvp.contract_path`, `registry_path` and `schema_path` name files under THAT checkout (their sha256 values are what bind); pass `--repo-root` explicitly when the run's frozen checkout is not the one executing the tool.

From the predecessor ledger spec only `strategy_inputs` (with `strategy_evidence_*` replaced by the
cycle's strategy evidence), `strategy_provider_map` and `portfolio_fx_rates` are carried. The
regenerated blocks are ignored; any other key is a refusal. The provenance block names the staged
provenance generation directory (its name is the id). Rebinding the layered `returns.csv` stays with
the existing rebind tool.

Predecessor spec precondition. "Carried as data" means byte for byte: `strategy_inputs.returns_path`
and `portfolio_fx_rates.root` are copied unchanged, and the ledger validator then requires them to
exist in the world the run uses. A predecessor spec written in another world (an earlier release's
evidence copy whose absolute paths name a checkout or data root that is absent now) must first be
rebound to the current world by the committed remap step; the builder neither remaps nor rewrites
paths, and passing the raw earlier spec fails the self-check. "The predecessor spec" is the ledger
spec of the release named by `predecessor.release_id` (for a rehearsal, that release's evidence
`ledger-spec.json` after the remap). Only `strategy_inputs`, `strategy_provider_map` and
`portfolio_fx_rates` of it are used; the cycle manifest's `predecessor.release_id` and
`release_manifest_sha256` are not read or checked by B1.

CLI inputs outside the cycle pins: `--predecessor-spec` and `--provenance-generation-dir` are not
hash-pinned by the cycle manifest. The provenance directory must be a staged `generations/<id>`
directory holding the provenance catalog; its name becomes the spec's provenance generation id and
the catalog file is hashed into the spec, so a different directory gives a different spec.

Outputs (create-only and resumable: each file is written through a temporary name and renamed into place;
a file already holding exactly the bytes the builder produces is kept and the self-check runs again, so a
killed run resumes; a file with other bytes is refused as `OUTPUT_EXISTS`; `--out-root` must be the cycle scratch root
or a directory beneath it, else `OUT_ROOT`: the scratch root is the manifest's `scratch` resolved under its root key, so a
rehearsal's output directory has to be placed inside it): `run-observation.json`
and `ledger-spec.json`, whose `run_observation` block points at the first. The new spec is then
checked by the committed ledger validator (`provider_ledger.build_catalog`, writes nothing). Any error
is a refusal and both files are renamed `*.rejected`. `--plan` builds and prints without writing or
checking.

Output conventions. Both files are written with sorted keys, `\n` line ends and a trailing newline, and
every path in them is absolute with forward slashes, including `run_observation.path` and each
provider `root` and finality-evidence path. `output_root` of the spec is `<out-root>/ledger`; the
builder names it and does not create it. A hand-written file with backslash paths and no trailing
newline has different bytes (and so a different run-observation sha256, which the spec embeds) for
identical content: compare after normalising separators and the trailing newline. A generation id
staged from the spec also depends on the receipt that embeds the run-observation path and bytes, so
it changes with `--out-root` or the checkout paths even when no ledger data does.

Refusal codes: `RESOLVED_VERSION`, `OUT_ROOT`, `PROVIDER_SET`, `PREDECESSOR_SPEC`, `MISSING`,
`HASH_MISMATCH`, `MANIFEST_INVALID`, `MANIFEST_NOT_VERIFIED`, `ID_MISMATCH`,
`CATALOG_NOT_IN_MANIFEST`, `COVERAGE_UNBOUNDED`, `QUALIFICATION_COVERAGE`,
`FINALITY_BEYOND_COVERAGE`, `FINALITY_EVIDENCE_MISSING`, `FINALITY_NOT_MANIFEST`,
`FINALITY_NOT_SELECTED_ARTIFACT`, `OUTPUT_EXISTS`, `SELF_CHECK`; cycle-manifest codes pass through.

Tests: `tests/portfolio_operations/test_release_ledger_spec_e2e.py` derives a cycle manifest from the
synthetic native-format generations that `tests/provider_ledger/test_admit_or_declare.py` builds, and the
builder's default self-check (the real validator, nothing patched) accepts all seven providers; a finality
claim the validator rejects leaves both files as `*.rejected`.

## B2: `provider_sources: "from_ledger"` in the release driver

See `docs/release-driver.md` (Run configuration). The `provider-generations` stage derives each
`provider-<id>` row from `{cycle}/ledger-spec.json` and compares it with the staged ledger catalog
(graph path `ledger_catalog`); a different generation id, a directory that is not that generation,
or an override for an unbound root is a refusal. The explicit map form still works.

The row for each provider is `<ledger-spec providers.<id>.root>/generations/<generation_id>`. When
the cycle staged a provider generation in one place for the ledger but the run must bind another
copy of the same generation (identical `generation.json` bytes), name that copy with
`real_root_overrides`; without it the ledger spec's root is bound. B2 covers ledger providers only:
other sources (for example the epoch manifest) stay outside it. `docs/release-driver.md` is the
authority for the stage; a probe that replicates B2's rule by logic is not a substitute for running
the `provider-generations` stage.

## Not a driver stage

The builder needs the cycle manifest, the predecessor spec and the staged provenance generation,
and the graph begins after staging (stage `selected-inputs`). It becomes a stage with the staging
group (block B8); until then it is run before the driver and its output is the `{cycle}` input.

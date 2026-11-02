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
pinned native file. `captured_at_utc` is the cycle manifest's value, `financial_cutoff` the cutoff,
`policy_contract_sha256` the hash of the contract file in the repository.

From the predecessor ledger spec only `strategy_inputs` (with `strategy_evidence_*` replaced by the
cycle's strategy evidence), `strategy_provider_map` and `portfolio_fx_rates` are carried. The
regenerated blocks are ignored; any other key is a refusal. The provenance block names the staged
provenance generation directory (its name is the id). Rebinding the layered `returns.csv` stays with
the existing rebind tool.

Outputs (create-only, `--out-root` must lie inside the cycle scratch root): `run-observation.json`
and `ledger-spec.json`, whose `run_observation` block points at the first. The new spec is then
checked by the committed ledger validator (`provider_ledger.build_catalog`, writes nothing). Any error
is a refusal and both files are renamed `*.rejected`. `--plan` builds and prints without writing or
checking.

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

## Not a driver stage

The builder needs the cycle manifest, the predecessor spec and the staged provenance generation,
and the graph begins after staging (stage `selected-inputs`). It becomes a stage with the staging
group (block B8); until then it is run before the driver and its output is the `{cycle}` input.

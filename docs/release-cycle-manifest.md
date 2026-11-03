# Release cycle manifest (B0 interface)

RELEASE-DRIVER-001, hand-script replacement block B0 (governing issue 623). This file is the
contract later blocks build on. Read the resolved-cycle file described here; do not import
`tools/release_cycle_manifest.py` internals.

Files:

- `schemas/portfolio/release-cycle-manifest.schema.json` - the per-cycle manifest (input).
- `schemas/portfolio/release-resolved-cycle.schema.json` - the resolved-cycle file (output).
- `tools/release_cycle_manifest.py` - `validate`, `resolve`, `print-ids`.
- `tests/portfolio_operations/test_release_cycle_manifest.py` - synthetic fixtures built in code.

## Inputs

| Input | Where it comes from |
|---|---|
| Cycle dates `cutoff`, `evaluated_on`, `captured_at_utc` | the cycle manifest (never `now()`) |
| Provider generations: root and which generation | the cycle manifest, per provider; `single` (the one generation under the root) or `explicit` (named id); the manifest sha256 is always pinned |
| Finality-evidence files | the cycle manifest, `providers.<id>.finality_evidence`, required for every provider whose `finality_source` is not `GENERATION_MANIFEST` (matrixport, quantbase, schwab: a native file; ibkr: the Flex statement, which B1 also requires to be a source artifact of the selected manifest) |
| Qualification and finality date | `providers.<id>.qualification` (required; the ledger validator is the authority on what is admissible) and `finalized_through` (required with a finality-evidence file, refused otherwise: a finite provider's date is derived from its manifest by B1) |
| Strategy evidence, cost inputs, epoch admission policy, public bundle roots | the cycle manifest, pinned by sha256 (bundle roots are directories, resolved but not hashed) |
| Predecessor release | `predecessor.release_id` (+ optional `release_manifest_sha256`) |
| Canonical scratch root | `scratch` is a `{root_key, relative}` pair; the directory behind the root key is a run argument |
| Root keys to directories | the run: `--root KEY=DIR` (repeatable) or `--roots-file file.json` (`{"key": "dir"}`). Roots are absolute existing directories. |

Every path is `{root_key, relative}`: `relative` uses `/`, has no `..`, no empty part, no drive
letter, no leading slash. No absolute string appears in a manifest.

## Commands

```
release_cycle_manifest.py validate  --manifest M [--manifest-sha256 H] [--root K=D ...] [--roots-file F]
release_cycle_manifest.py resolve   --manifest M [--manifest-sha256 H] --root K=D ... [--out FILE]
release_cycle_manifest.py print-ids --manifest M [--manifest-sha256 H] --root K=D ...
```

`validate` without roots runs schema and static rules only; with roots it verifies every file,
hash and generation. `resolve` writes the resolved-cycle JSON create-only to `--out` (an existing
file is a refusal) or prints it. `print-ids` prints one JSON line. Exit 0 ok, 1 refusal, 2 usage.

## Outputs (resolved-cycle file, `qe-release-resolved-cycle/1`)

Keys: `schema_version`, `cycle_id`, `cutoff`, `evaluated_on`, `captured_at_utc`,
`cycle_manifest_sha256`, `predecessor`, `roots` (key -> absolute posix path), `scratch` (absolute),
`providers`, `strategy_evidence`, optional `epoch_admission_policy`, `cost_inputs` (name -> file),
`public_bundles` (name -> directory). A "file" is `{path, sha256, size_bytes}`, all verified.

`providers.<id>` (the per-provider generation row; ids are alpaca, fidelity, ibkr, matrixport,
portfolio123, quantbase, schwab):

| Field | Meaning |
|---|---|
| `root` | the provider root, `<root>/generations/<generation_id>/` holds the generation |
| `generation_id`, `generation_dir` | the chosen generation (B2 provider_sources row = `generation_dir`) |
| `manifest_path`, `manifest_sha256`, `manifest_size_bytes` | `generation.json`, verified against the pin |
| `finality_source` | `GENERATION_MANIFEST` (alpaca, fidelity, portfolio123), `SELECTED_SOURCE_ARTIFACT` (ibkr), `CYCLE_MANIFEST_NATIVE` (matrixport, quantbase, schwab) |
| `finality_evidence` | a file object: the generation manifest itself, or the pinned evidence file (always present) |
| `qualification`, `finalized_through` | as declared; `finalized_through` is `null` for `GENERATION_MANIFEST` providers (B1 derives it) |

`finality_source` is declared per provider in the manifest and the schema pins the only allowed
value for each provider id (the tool holds no provider names); B1 re-checks it against the
provider ledger's own validator, which stays the authority. The set of providers is checked by the
schema (all seven required); B1 checks it equals the ledger's provider catalog set.

The catalog file, `catalog_sha256`, qualification and `generation_as_of_date` are NOT resolved
here: B1 reads them through the provider ledger's registry.

## Worked example (synthetic)

```json
{
  "schema_version": "qe-release-cycle-manifest/1",
  "cycle_id": "cycle-example",
  "cutoff": "2026-01-02",
  "evaluated_on": "2026-01-05",
  "captured_at_utc": "2026-01-04T10:00:00Z",
  "predecessor": {"release_id": "release-example-prev"},
  "scratch": {"root_key": "scratch", "relative": "cycle-example"},
  "providers": {
    "alpaca": {
      "root": {"root_key": "data", "relative": "dataAlpaca"},
      "generation": {"mode": "single"},
      "manifest_sha256": "<64 hex>",
      "finality_source": "GENERATION_MANIFEST",
      "qualification": "PUBLISHED_COMPLETE"
    },
    "schwab": {
      "root": {"root_key": "data", "relative": "dataSchwab"},
      "generation": {"mode": "explicit", "generation_id": "gen-schwab-example"},
      "manifest_sha256": "<64 hex>",
      "finality_source": "CYCLE_MANIFEST_NATIVE",
      "qualification": "STAGED_COMPLETE",
      "finalized_through": "2026-01-02",
      "finality_evidence": {
        "path": {"root_key": "data", "relative": "dataSchwab/native/evidence.json"},
        "sha256": "<64 hex>"
      }
    }
  },
  "strategy_evidence": {"path": {"root_key": "data", "relative": "evidence/strategy.json"}, "sha256": "<64 hex>"},
  "cost_inputs": {"economics": {"path": {"root_key": "data", "relative": "cost/cost.csv"}, "sha256": "<64 hex>"}},
  "public_bundles": {"factors": {"root_key": "data", "relative": "bundles/factors"}}
}
```

(The other five providers follow the same shape; all seven are required.) Run it with
`--root data=<absolute dir> --root scratch=<absolute dir>`.

## Error semantics

Every refusal is `release-cycle-manifest: FAIL: <CODE>: <detail>` on stderr, exit 1, no output file.

| Code | Raised when |
|---|---|
| `SCHEMA` | the manifest does not match its schema (unknown key, missing provider, bad date) |
| `DATE_ORDER` | `cutoff` is after `evaluated_on` |
| `PATH_ESCAPES_ROOT` | a `relative` is absolute, has `..`, backslash, drive letter, empty part, or resolves (symlink) outside its root |
| `UNKNOWN_ROOT` | a root key is not bound by the run |
| `ROOTS` / `ROOT_MISSING` | malformed `--root`, duplicate key with two directories, root not an existing absolute directory |
| `MISSING` | a pinned file, generation, generations directory or bundle directory does not exist |
| `HASH_MISMATCH` | file bytes (or the cycle manifest itself, via `--manifest-sha256`) differ from the pin |
| `SIZE_MISMATCH` | a `size_bytes` pin differs |
| `AMBIGUOUS_GENERATION` | `mode: single` and the root holds more than one generation |
| `ID_MISMATCH` | `generation.json` names a different generation than its directory |
| `FINALITY_EVIDENCE_REQUIRED` | a provider whose finality source is not its generation manifest, without a `finality_evidence` pin |
| `FINALITY_EVIDENCE_NOT_ALLOWED` | a `GENERATION_MANIFEST` provider with one |
| `FINALIZED_THROUGH_REQUIRED` / `FINALIZED_THROUGH_NOT_ALLOWED` | `finalized_through` missing with evidence, or present without it |
| `OUTPUT_EXISTS` | `resolve --out` target exists (outputs are create-only) |
| `RESOLVED_SHAPE` | internal: the produced file fails its own schema |
| `INPUT` | unreadable or non-JSON input |

## What each later block reads

All blocks take `--resolved-cycle <file>` (the output above) and refuse a file whose
`schema_version` is not `qe-release-resolved-cycle/1`.

| Block | Reads from the resolved cycle |
|---|---|
| B1 run observation + ledger spec | `providers.*` (generation_dir, manifest sha, `finality_evidence`, `finality_source`), `captured_at_utc`, `cutoff`, `strategy_evidence`, `scratch`, `predecessor` |
| B2 provider_sources from the ledger | `providers.*.generation_dir` / `generation_id` as the cross-check against the ledger catalog |
| B3 baseline spec | `cost_inputs`, `cutoff`, `evaluated_on`, `scratch`, `providers.*.generation_id` |
| B4 selected-inputs + prep spec | `evaluated_on`, `cutoff`, `public_bundles`, `epoch_admission_policy`, `scratch`, `predecessor`, `cycle_manifest_sha256` |
| B5 pre_chain | `scratch`, `cycle_manifest_sha256` (pin the cycle identity) |
| B6 calendar authority | `cutoff`, `evaluated_on`, `epoch_admission_policy`, `predecessor` |
| B7 public bundles + Quantbase sources | `public_bundles`, `cutoff`, `providers.quantbase` |
| B8 staging stages | `providers.*.root`, `scratch`, `roots` |
| B9 oracle manifests | `cutoff`, `roots`, `strategy_evidence`, `predecessor` |
| B10 post-commit | `predecessor`, `cycle_manifest_sha256`, `scratch` |

Extensions are additive: a block that needs a new manifest field adds it to the schema (optional)
and to the resolved-cycle schema in the same PR, and keeps `schema_version` unless a field changes
meaning.

## Not in B0 (later)

A content-hash resolver and spec-rewriting CLI on `tools/release_restore_world.py` (replacing
`resolve_pins.py`), and a driver config key naming the roots file, are deliberately left to the
block that first needs them; `--root` and `--roots-file` are the stable surface until then.

## B3: baseline spec builder

`tools/release_baseline_spec.py` builds the cycle's baseline spec (replaces `mk_baseline_spec.py` and
`patch_baseline_spec.py`). Schema of its governed input: `schemas/portfolio/baseline-declared-inputs.schema.json`.

```
release_baseline_spec.py build --resolved-cycle R --release-inputs-root D --release-inputs-manifest-sha256 H     --root ID=DIR ... --provenance-generation G --ledger-generation L [--cost-root-id ID] [--repo-root DIR] --out FILE
release_baseline_spec.py declare --previous-spec FILE --out FILE --roots-out FILE
```

- Reads from the resolved cycle: `cutoff` (the spec `as_of_date`), `cost_inputs.economics` (required) and
  `cost_inputs.evidence` (optional), `providers.*.generation_id` and `manifest_sha256`.
- The static sources and the replay expectation (cutoff dependent) are the declared-inputs file
  `ops/mvp2-release/inputs/baseline/baseline-declared-inputs.json`, read through the release-inputs loader
  (every entry verified, cutoff must equal the cycle cutoff). `declare` authors it from a previous baseline spec.
- `--root ID=DIR` must name exactly the root ids the declared sources use. Every static and cost file is hashed
  against its pin. The two cost files must share one directory (root id `--cost-root-id`, default `cost-inputs`).
- The cost-evidence `as_of_date` is the latest `feed_last_date` in the file, not typed.
- The ledger generation must have the cutoff as `as_of_date` and admit exactly the cycle's provider generations
  (id and manifest sha256). The financial-MVP triple binds the contract, registry and schema of `--repo-root`.
- Output is create-only, canonical bytes (sorted keys, compact, no trailing newline), checked against
  `baseline-spec.schema.json`. Refusals are `release-baseline-spec: FAIL: CODE: detail`, exit 1.

## B9: acceptance-inputs and sleeve-inputs builder

`tools/release_acceptance_inputs.py` writes the two files the independent derivation seats read
(`acceptance/derive_a`, `acceptance/derive_b`). It replaces `mk_oracle_r2.py` (the
`qe-cp-acceptance-inputs/3` manifest) and `mk_sleeve_inputs.py` (the `qe-cp-sleeve-inputs/1` file).
The derivations stay the only readers and the only interpreters; this tool writes pins.

```
release_acceptance_inputs.py acceptance-inputs --resolved-cycle R --qe-root D --repo-root D [--root K=D ...]
    --predecessor-manifest P [--predecessor-manifest-sha256 H] [--baseline-pointers FILE]
    --ledger-generation KEY:REL --file declared_cutoff_date=KEY:REL --file account_strategy_map_and_active_universe=KEY:REL
    [--file FAMILY=KEY:REL ...] [--artifact-dir PROVIDER=KEY:REL ...] [--stage-dir REL] --out FILE
release_acceptance_inputs.py sleeve-inputs     --resolved-cycle R --qe-root D --repo-root D [--root K=D ...]
    [--file ROLE=KEY:REL ...] [--artifact-dir PROVIDER=KEY:REL ...] [--stage-dir REL] --out FILE
```

- Reads from the resolved cycle: `cutoff` (the only cutoff; it is written into both files),
  `captured_at_utc`, `roots` and the generation row of each provider. The provider generation
  manifests are hashed again against the row's pin.
- `KEY:REL` is a root key and a plain relative path with `/` separators. Root keys are the resolved
  cycle's `roots`, `--root KEY=DIR`, and the two the derivations use, `qe` (`--qe-root`) and `repo`
  (`--repo-root`). A key bound to two directories is a refusal.
- Provider captures are not named by path. Each is the one file under `--artifact-dir` (default the
  provider root) whose name, size and sha256 equal the record in that provider's generation manifest;
  a generation's `normalized/*` outputs are looked up beside its manifest by content. Two matching
  files are a refusal even with equal bytes: narrow `--artifact-dir`. Symlinks and junctions are not
  followed and are never candidates.
- Where a file lies decides how it is pinned. Under the qe root: pinned in place. Under the repo root
  (sleeve inputs only): root `repo`. Under neither (a scratch ledger, a run output, the repository
  contract for the acceptance manifest): copied create-only to `<qe root>/<--stage-dir>/...` and pinned
  at the copy; a provider capture keeps its path below the provider root, so the path bindings the
  derivation checks (`generations/<id>/`, `output/<source run>/`) hold. An existing staged copy must be
  byte-identical. Without `--stage-dir` such a file is a refusal. Nothing is copied or written until
  every check has passed.

### acceptance-inputs

| Entry (family) | Source |
|---|---|
| `declared_cutoff_date`, `account_strategy_map_and_active_universe` | `--file` (the run's stage outputs); `calculation_cutoff_date` and `as_of_date` must equal the cycle cutoff; the crosswalk must carry a basename the derivation recognizes |
| provider ledger (`portfolio_and_strategy_capital_at_cutoff`) | `--ledger-generation`: `provider-ledger.json` there; `as_of_date` is the cutoff and `provider_generations` are exactly the cycle's (id and manifest sha256) |
| `financial_source_contract` | `--file` or `repo:contracts/portfolio-financial-mvp-source-v1.json` |
| Matrixport NAV, wallet files, native families, Quantbase accounts, activity and dashboard | the cycle's generations (artifact records); the native files must lie under `output/<source_run_id>/`, the run id read from the native catalog |
| `fx_*`, `provider_capital_policy`, owner receipts (kept families) | the predecessor manifest's entry for the family (exactly one), located at its qe-relative path, else (also when that path is gone) by sha256 through `--baseline-pointers` (the authority pointers of an expected-baseline file; two locations for one digest are not guessed). The file is hashed again and must equal the predecessor's pin (`CARRY_DRIFT`), unless `--file FAMILY=...` names its replacement. Other fields of the entry (`ticker`, `schema`, `fields`, `note`) are carried |

`generation_selection_evidence` is rebuilt from the cycle: `captured_at_utc`, `financial_cutoff`, and
per provider with a declared finality the generation id, qualification, `finalized_through`, the
finality evidence file's path and sha256 (read again; `..._verified_V` is written `true` only after
that read), plus the native catalog and manifest digests and the source run for the native provider.
`provenance` names the cycle, the resolved-cycle sha256 and the predecessor manifest sha256.

### sleeve-inputs

`ibkr_catalog` and the six Quantbase captures come from their generations like the capture families
above. Quantbase captures that the data root keeps only inside archives are not found under the provider
root: assemble them with `release_quantbase_sources.py` (B7, which checks them against the sleeve policy
manifest's pins) and pass that directory as `--artifact-dir quantbase=KEY:REL`; each file must still
equal the generation manifest's own record. `ibkr_declared_inputs`, `quantbase_declared_inputs`, `tag_map` and `crosswalk` default to the
governed files under `repo:ops/mvp2-release/inputs/` and can be replaced with `--file ROLE=KEY:REL`;
the declared inputs must end at the cycle cutoff (`window.last_day`, `data_cutoff`) and the crosswalk
must carry it.

### Refusals

`release-acceptance-inputs: FAIL: CODE: detail`, exit 1, nothing written; exit 2 is a usage error.

| Code | Raised when |
|---|---|
| `MISSING`, `HASH_MISMATCH`, `SIZE_MISMATCH` | a pinned or looked-up file is absent, or the bytes differ from the manifest record or pin (generation manifest, finality evidence, staged copy, `--resolved-cycle-sha256`, `--predecessor-manifest-sha256`) |
| `AMBIGUOUS_FILE` | two files match one artifact record |
| `PATH_ESCAPES_ROOT`, `UNKNOWN_ROOT`, `ROOTS`, `ROOT_MISSING` | a `--file`/`--stage-dir` path is absolute, has `..`, backslash or an empty part, or goes through a symlink or junction; a root key is unbound or bound twice |
| `NOT_UNDER_ROOT`, `STAGE_CONFLICT`, `STAGE_FAILED` | a file under neither root and no `--stage-dir`; two different files for one staged path; a copy that does not verify |
| `STALE_GENERATION`, `LEDGER_PROVIDERS`, `CUTOFF_MISMATCH` | a generation `as_of_date` or `finalized_through` before the cutoff; a ledger that is not at the cutoff or admitted another generation; a run output or governed input for another cutoff |
| `FINALITY_NOT_AT_CUTOFF` | the native provider's generation is finalized after the cutoff: `derive_a` accepts a later date, `derive_b` requires the cutoff, so a manifest both can read binds the cutoff exactly |
| `CARRY_DRIFT`, `CARRY_UNRESOLVED`, `PREDECESSOR` | a kept file changed; its location cannot be established; the predecessor is not a `qe-cp-acceptance-inputs/3` manifest with exactly one entry per kept family |
| `RUN_PATH`, `CATALOG`, `ARTIFACT_RECORD`, `ID_MISMATCH`, `PROVIDER_MISSING` | a native file outside its source run; no `source_run_id`; a missing or duplicate artifact record; a generation file naming another generation; a provider absent from the cycle |
| `OUTPUT_EXISTS`, `WRITE_FAILED` | `--out` exists (outputs are create-only); a write failed (the output and each staged copy are written to a temporary file beside the target and linked or renamed into place without replacing anything, so an interrupted run leaves no partial file and can be repeated) |
| `RESOLVED_CYCLE`, `INPUT`, `UNKNOWN_ROLE`, `BASENAME`, `USAGE` | the resolved cycle is not a `qe-release-resolved-cycle/1` file; unreadable or non-JSON input; a `--file` name that is not a role of the command; a crosswalk basename the derivation does not place; malformed arguments |

`acceptance/derive_a` no longer defaults `--cutoff`; the cycle's cutoff is always passed.

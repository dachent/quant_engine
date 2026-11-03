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

## B4: selected-inputs and attended preparation spec builders

`tools/release_prepare_inputs.py` builds the two per-cycle inputs the hand scripts `mk_selected.py`,
`mk_prep_spec.py` and `mk_prep_spec_p23.py` produced. The predecessor file (the previous release's, as
data) is carried only for the parts this cycle does not own; everything else is regenerated from the
cycle's outputs. No id, pass number, worktree path or provider name is typed; every pin is recomputed
from bytes. Tests: `tests/portfolio_operations/test_release_prepare_inputs.py` (synthetic fixtures
built in test code).

```
release_prepare_inputs.py selected  --resolved-cycle R --predecessor FILE --commit SHA --out FILE
    [--repo-root DIR] [--ledger-generation-dir DIR] [--resolved-cycle-sha256 H] [--rebind-cutoff]
    [--file NAME=ROOT_KEY:relative@sha256 ...] [--plan]
release_prepare_inputs.py prep-spec --resolved-cycle R --predecessor-spec FILE --out FILE
    [--repo-root DIR] [--ledger-generation-dir DIR] [--resolved-cycle-sha256 H] [--rebind-cutoff]
    [--selected-inputs FILE] [--calendar-authority FILE]
    [--baseline-id ID] [--history-id ID] [--attribution-id ID]
    [--factors-bundle NAME] [--sector-bundle NAME] [--style-bundle NAME] [--<kind>-bundle-id ID]
    [--pin KEY=ROOT_KEY:relative@sha256 ...] [--dir KEY=ROOT_KEY:relative ...] [--omit KEY ...]
    [--check attended-plan|none] [--plan]
```

Declared roots: every directory the resolved cycle's `roots` binds, its `scratch` and `--repo-root`.
A path anywhere in a carried part of a predecessor that is outside them (or has a `..` part, or a
symlink or junction on the way) is a refusal. A predecessor already remapped by
`tools/release_spec_remap.py` (or authored under the declared roots) is accepted as is.

### selected

Output: `{builder_kwargs, files, financial_data_cutoff}`, written create-only (default location
`<scratch>/in/selected-inputs.json`, read by `release_pre_chain.py` and the attended preparation).

- Rebound from the cycle: `contract_path` and `financial_input_contract_path` (the repository contract),
  `provider_admission_receipt_path`, `provider_ledger_catalog_path`, `provider_ledger_manifest_path` (the
  built ledger generation: `--ledger-generation-dir`, or the one generation under `<scratch>/ledger/generations`)
  and `repository_sha` (`--commit`, 40 hex characters, supplied by the driver and never read from git here).
- Every other `files` entry is carried: its file must exist under the roots, its bytes must equal the
  predecessor's pin, and its pin must name the path its builder argument names. Every entry of the output is
  then re-pinned from the bytes (`{path, sha256, size_bytes}`, `/` separators, equal to the builder argument).
- A native `<provider>_manifest_path` of a provider the cycle resolved must be that provider's manifest of this
  cycle (sha256 equal), else `STALE_PREDECESSOR_GENERATION`.
- The predecessor cutoff (`financial_data_cutoff`, `as_of_date`) must be the cycle's, else
  `STALE_PREDECESSOR`; `--rebind-cutoff` rebinds it and `--file NAME=ROOT_KEY:relative@sha256` names the new
  native file for a carried entry (the sha256 is required and checked against the bytes).

### prep-spec

Output: the `--preparation-spec` of `portfolio_pipeline_materialize.py attended`.

- Regenerated: `financial_data_cutoff`, `evaluation_date` (resolved `evaluated_on`), the contract and census pins,
  `provider_ledger_generation_root` and `provider_ledger_spec`, `baseline_spec_template`,
  `attribution_spec_template`, `retained_verified_generations` (baseline, history, attribution),
  `expected_staged_baseline` (generation id and the baseline catalog sha256), `source_selection`,
  `dependence_selection_template`, `epoch_admission_policy` (the resolved cycle's file), the crosswalk,
  `calendar_authority` (`--calendar-authority`, default `<scratch>/in/calendar-authority.json`: the seed for the
  first attended attempt, B6's output for the second), `plans.*` (plan, catalog and the public factor, sector and
  style files of the cycle's bundles) and `sleeve_facts` (the policy manifest, and each `<provider>_generation_root`
  key of the predecessor set to that provider's resolved root).
- Every generation id is the one generation under the cycle (`baseline/generations`, `pre/history`,
  `attribution/generations`, `ledger/generations`, and the bundle roots `public_bundles.factors|sector|style`, or
  the names given by `--<kind>-bundle`). Two candidates are `AMBIGUOUS_GENERATION`; `--*-id` only picks one.
- Carried from the predecessor and re-verified (existing, under the roots, bytes equal to its pin):
  `base_factor_file`, `capital_account_crosswalk`, `implementation_reconciliation`,
  `implementation_return_attribution`, `legacy_panel_root`, `calendar_derivation_inputs`, `purpose`,
  `authority_notes`, `plans.*.base_spec_template`, `base_source_template_root`, `evidence_mode` and
  `sleeve_facts.quantbase_source_dir` and `predecessor_data_root`. `--pin KEY=ROOT_KEY:relative@sha256` and
  `--dir KEY=ROOT_KEY:relative` replace a carried input (the sha256 is required; `KEY` may be
  `sleeve_facts.quantbase_source_dir`).
- Per-cycle inputs `weekly_gap_fill_spec`, `vendor_gap_fills`, `model_vs_actual_input` are present only when
  named by `--pin`. A predecessor that carries one the run did not name is `STALE_CARRIED_INPUT`; `--omit KEY`
  declares the cycle has none (absent = key omitted).
- The files the spec binds are checked against the cycle before it is written: the ledger spec (cutoff, every
  provider's generation id and manifest sha256, output root), the baseline spec cutoff and the selected inputs
  (cutoff and ledger generation).
- After the file is written (create-only), the committed attended preparation validates it through
  `plan_attended_inputs`, the planning path `portfolio_pipeline_materialize.py attended` takes without
  `--execute` (no writes). A refusal there renames the spec to `*.rejected` (`SELF_CHECK`); `--check none` skips
  it. The driver's `bound_ids` refusal then also fires on any spec that names another pass's generation.

Refusal codes (`release-prepare-inputs: FAIL: CODE: detail`, exit 1; 2 is a usage error):
`PATH_OUTSIDE_ROOTS`, `PATH_ESCAPES_ROOT`, `MISSING`, `HASH_MISMATCH`, `PIN_PATH_MISMATCH`,
`AMBIGUOUS_GENERATION`, `ID_MISMATCH`, `STALE_PREDECESSOR`, `STALE_PREDECESSOR_GENERATION`,
`STALE_CARRIED_INPUT`, `STALE_LEDGER_SPEC`, `STALE_BASELINE_SPEC`, `STALE_SELECTED_INPUTS`,
`PREDECESSOR_SHAPE`, `MISSING_KWARG`, `PROVIDER_UNKNOWN`, `OVERRIDE_NOT_ALLOWED`, `UNKNOWN_ROOT`, `USAGE`,
`COMMIT`, `OUTPUT_EXISTS`, `SELF_CHECK`, `RESOLVED_VERSION`, `RESOLVED_SHAPE`, `INPUT`.

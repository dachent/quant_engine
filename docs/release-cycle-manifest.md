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
| Cycle dates `cutoff`, `evaluated_on`, `captured_at_utc` | the cycle manifest (never `now()`); see "Capture timestamp" below for the accepted `captured_at_utc` forms |
| Provider generations: root and which generation | the cycle manifest, per provider; `single` (the one generation under the root) or `explicit` (named id); the manifest sha256 is always pinned |
| Finality-evidence files | the cycle manifest, `providers.<id>.finality_evidence`, required for every provider whose `finality_source` is not `GENERATION_MANIFEST` (matrixport, quantbase, schwab: a native file; ibkr: the Flex statement, which B1 also requires to be a source artifact of the selected manifest) |
| Qualification and finality date | `providers.<id>.qualification` (required; the ledger validator is the authority on what is admissible) and `finalized_through` (required with a finality-evidence file, refused otherwise: a finite provider's date is derived from its manifest by B1) |
| Strategy evidence, cost inputs, epoch admission policy, public bundle roots | the cycle manifest, pinned by sha256 (bundle roots are directories, resolved but not hashed) |
| Predecessor release | `predecessor.release_id` (a plain name token as the schema defines it, not required to be a 64-hex id) and optional `release_manifest_sha256`. Both are copied into the resolved file and NOT verified against anything; B1 reads neither (it takes the predecessor ledger spec as a separate file) |
| Canonical scratch root | `scratch` is a `{root_key, relative}` pair; the directory behind the root key is a run argument. `relative` must be a non-empty plain path: `.` is refused (`PATH_ESCAPES_ROOT`), so the scratch root is always a named directory beneath the root key, never the root itself |
| Root keys to directories | the run: `--root KEY=DIR` (repeatable) or `--roots-file file.json` (`{"key": "dir"}`). Roots are absolute existing directories. |

### Capture timestamp

`captured_at_utc` is ISO-8601 with seconds, optional fractional seconds and a mandatory offset:
`YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM)`. The resolver converts it to UTC and keeps every
microsecond the capture recorded; nothing is truncated or rounded.

| Input | Resolved `captured_at_utc` |
|---|---|
| `2026-09-19T09:19:17Z` | `2026-09-19T09:19:17Z` |
| `2026-09-19T09:19:17.369302Z` | `2026-09-19T09:19:17.369302Z` |
| `2026-09-19T09:19:17.369302+00:00` | `2026-09-19T09:19:17.369302+00:00` |
| `2026-09-19T04:19:17.369302-05:00` | `2026-09-19T09:19:17.369302+00:00` |
| `2026-09-19T09:19:17.5Z` | `2026-09-19T09:19:17.500000Z` |

Rules: a `Z` suffix stays `Z`; any numeric offset is converted to UTC and written `+00:00`; a
non-zero fraction is written with six digits; a zero fraction is dropped. The two UTC spellings are
different bytes, so to reproduce an existing run observation byte for byte, write the manifest value
in the spelling that run observation used (the retained hand-built files used `+00:00`).
A naive timestamp (no offset) is refused (`SCHEMA` from `validate`/`resolve`); an impossible date,
time or offset is `DATE_INVALID`.

Why microseconds are kept: the provider ledger's run-observation validator
(`provider_ledger._utc_timestamp`) accepts any timezone-aware UTC ISO-8601 text and stores it
verbatim, so microsecond precision survives into the run observation and its hash. A value finer
than a microsecond (more than six fractional digits with a non-zero tail) is refused with
`DATE_PRECISION_LOST` instead of being truncated: a downstream that cannot hold the precision must
never receive a shortened value. Trailing zeros beyond six digits are not precision and are accepted.

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
| `SCHEMA` | the manifest does not match its schema (unknown key, missing provider, malformed date, a timestamp without an offset) |
| `DATE_INVALID` | `cutoff`, `evaluated_on`, `finalized_through` or `captured_at_utc` is not a real date or timestamp (impossible calendar date, time or offset) |
| `DATE_PRECISION_LOST` | `captured_at_utc` carries a non-zero fraction finer than a microsecond, which the ledger cannot hold |
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

## Reading the resolved cycle

- `roots`, `scratch` and every provider path in the resolved file (`root`, `generation_dir`,
  `manifest_path`, `finality_evidence.path`) are absolute strings written with forward slashes
  (`Path.as_posix()`), whatever the host. A file written by a hand script on Windows used
  backslashes: compare paths after normalising separators, and expect different file bytes (hence
  different sha256) for the same content.
- The resolver verifies the `cost_inputs` and `epoch_admission_policy` hashes and resolves
  `public_bundles`, but B1 does not read them (B3, B4, B6 and B7 do). A successful B1 run says
  nothing about those pins; `validate` with roots does.
- The cycle manifest pins everything except two CLI inputs of B1: the predecessor ledger spec and the
  provenance generation directory (see `docs/release-ledger-spec.md`).

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

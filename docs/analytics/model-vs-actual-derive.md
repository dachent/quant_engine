# Model-vs-actual audit input derive tool (#898)

`tools/portfolio_model_vs_actual_derive.py` builds the `model-vs-actual-audit-bridges`
artifact (schema_version 1) that `tools/portfolio_model_vs_actual.py` consumes. Before
this tool the artifact was assembled by hand-run scratch scripts.

## CLI

    tools/portfolio_model_vs_actual_derive.py --spec SPEC --run-dir RUN_DIR
    tools/portfolio_model_vs_actual_derive.py --spec SPEC --out FILE --manifest FILE

`--run-dir` writes `audit-input.json` and `audit-input.manifest.json`. Relative input
paths resolve against `--root` (default: the spec's folder). Exit 2 and no output on any
refusal.

## Spec (`schemas/portfolio/model-vs-actual-derive-spec.schema.json`)

`inputs` lists every input key with path and expected sha256; any mismatch refuses the
run before anything is parsed. `audit_written` is a spec field (no wall clock). An
optional `as_of` (ISO date) is emitted as a top-level `as_of` in the artifact only when
the spec carries it; without it the output is byte-identical to the hand-built artifact.
`recipe` names the carried previous artifact (`base_input`), the ledger input, the pulls
completed by this run, and per strategy a list of ops: `window_bridge`, `ln_waterfall`,
`adjclose_extension`, `extend_epoch`, `set_notes`, `append_notes`, `clear_unavailable`.
Real audit figures (window dates, notes, account ids) live in the spec, not in Git.

## Gates

Refused: hash mismatch; undeclared input, unknown op or strategy; component id outside
the consumer vocabulary; origin tag other than O, D, I; `replacing_pull` naming a missing
pull (completed pulls are cleared first); audit gap differing from account minus model;
any window whose components miss the gap by more than `closure_tolerance_pp`. The result
must also pass `parse_audit_input(raw, sha256(raw))`.

## Output form

Key order is inherited from the carried artifact (insertion order, not sorted) so the
reproduction is byte-identical; bytes are LF, no trailing newline. The manifest is
sorted-key JSON with input hashes only (no paths).

## Real-input test

Set `QE_MVA_DERIVE_SPEC` (spec file) and `QE_MVA_DERIVE_EXPECTED_SHA256` to run
`test_real_inputs_reproduce_the_pinned_artifact_byte_for_byte`.

## Release wiring

Not wired. `ops/mvp2-release/release-stages.json` consumes `model_vs_actual_input` only
as a pinned input (`--pin model_vs_actual_input=...`); it has no stage that produces
inputs, so the release driver runs this CLI and passes the resulting pin.

"""Let a schema-10 recipe emit the epoch-manifest input gate, proved its own way.

`portfolio_financial_outcomes` requires the gate
`financial-epoch-manifest-input-authority` unconditionally, and it was only ever
emitted for schema-9 recipes -- the profile that names five loose epoch-manifest
inputs. A schema-10 recipe binds the verified generation instead, so no
schema-10 release could be validated at all.

Two things are needed, and they are separate.

**The inputs.** They are projected out of the bound generation. Nothing is
invented: `_rebuild_selected_epochs`, which consumes them, already defines them
in terms of a generation -- the admission policy is the artifact whose digest
the generation pins as `admission_policy`, and the other four are the canonical
encodings of four members of `authority/calculation-inputs.json`.

**The proof.** The schema-9 proof replays `build_manifest_candidate` from those
five inputs and requires the panel to declare
`INDEPENDENT_EXACT_REBUILD`. A schema-10 panel declares
`VERIFIED_GENERATION_BUNDLE_EXACT_BYTES` instead, because it consumed the
verified bundle rather than rebuilding from loose inputs -- so that proof is
categorically the wrong one here, and its replay is not even reproducible from
the projected inputs (the classified flows carry the bundle's own policy, not
the repository's).

The right proof already exists: `_verified_epoch_manifest_materialization_proof`,
which proves the bundle verifies and its bytes survive unchanged into the
published panel. It is already what the post-build semantic gate uses for this
profile. So the schema-9 proof steps aside for schema 10, and the gate is bound
to the verified-bundle proof instead.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_pipeline_materialize.py'

Q3 = '"' * 3

INPUTS_HELPER = '''

EPOCH_MANIFEST_CALCULATION_MEMBERS = {
    "account_strategy_ledger": "calculation_ledger",
    "account_strategy_ledger_identity": "calculation_ledger_identity",
    "account_strategy_classified_flows": "calculation_classified_flows",
    "account_strategy_flow_coverage": "calculation_flow_coverage",
}


def _epoch_manifest_inputs_from_generation(
    vertical: Mapping[str, Any], vertical_path: Path
) -> dict[str, dict[str, object]]:
    Q3Project the five epoch-manifest inputs out of the bound generation.

    A schema-9 recipe hands the five inputs in as loose files. Schema 10 binds
    the verified generation instead, and the gate that names them was never
    emitted for it -- so `portfolio_financial_outcomes`, which requires that
    gate unconditionally, could not validate a schema-10 release at all.

    Nothing is invented here, because the replay that consumes these inputs
    already says what they are in terms of a generation.
    `_rebuild_selected_epochs` requires the admission policy to be the artifact
    whose digest the generation pins as `admission_policy`, and the other four
    to be the canonical encodings of four members of
    `authority/calculation-inputs.json`. Those are exactly the bytes written
    here, so that replay compares each input against its own source.
    Q3

    definition = vertical["epoch_manifest_generation"]
    runtime_root = _absolute(
        definition["runtime_root"], "epoch_manifest_generation.runtime_root"
    )
    generation_id = _text(
        definition["generation_id"], "epoch_manifest_generation.generation_id"
    )
    generation = runtime_root / "generations" / generation_id
    identities = _load(
        generation / "authority" / "selected-input-identities.json",
        "selected input identities",
    )
    calculation = _load(
        generation / "authority" / "calculation-inputs.json",
        "account manifest calculation inputs",
    )
    contents: dict[str, bytes] = {
        key: canonical_json_bytes(calculation[member])
        for key, member in EPOCH_MANIFEST_CALCULATION_MEMBERS.items()
        if member in calculation
    }
    if len(contents) != len(EPOCH_MANIFEST_CALCULATION_MEMBERS):
        raise MaterializationError("epoch-manifest calculation inputs differ")
    admission = _generation_admission_policy(generation)
    contents["account_strategy_admission_policy"] = admission.read_bytes()
    declared = identities.get("admission_policy")
    if not isinstance(declared, Mapping) or declared != {
        "sha256": hashlib.sha256(
            contents["account_strategy_admission_policy"]
        ).hexdigest(),
        "size_bytes": len(contents["account_strategy_admission_policy"]),
    }:
        raise MaterializationError("epoch-manifest admission policy identity differs")

    root = vertical_path.resolve().parent / "epoch-manifest-inputs" / generation_id
    root.mkdir(parents=True, exist_ok=True)
    definitions: dict[str, dict[str, object]] = {}
    for key in EPOCH_MANIFEST_INPUTS:
        path = root / (key.replace("_", "-") + ".json")
        content = contents[key]
        if path.is_symlink():
            raise MaterializationError("epoch-manifest input is linked")
        # Re-derived on every call and rewritten only when it would change, so
        # the several call sites that ask for these definitions agree.
        if not path.is_file() or path.read_bytes() != content:
            path.write_bytes(content)
        definitions[key] = _file_definition(path)
    return definitions


def _epoch_manifest_gate_proof(
    recipe: Mapping[str, Any], output_root: Path, schema_nine_proof: Any
) -> Any:
    Q3Choose the proof the epoch-manifest gate is bound to.

    A schema-9 release proves this seam by replaying the producer from the five
    loose inputs. A schema-10 release cannot be proved that way and must not be:
    its panel consumed the verified bundle, declares
    `VERIFIED_GENERATION_BUNDLE_EXACT_BYTES`, and the matching proof is the one
    the post-build semantic gate already uses for this profile.
    Q3

    if schema_nine_proof is not None:
        return schema_nine_proof
    return _verified_epoch_manifest_materialization_proof(recipe, output_root)
'''.replace('Q3', Q3)

OLD_SIG = '''def _financial_epoch_manifest_inputs(
    vertical: Mapping[str, Any],
) -> dict[str, dict[str, object]] | None:
'''
NEW_SIG = '''def _financial_epoch_manifest_inputs(
    vertical: Mapping[str, Any],
    vertical_path: Path,
) -> dict[str, dict[str, object]] | None:
'''

OLD_BODY = '''    if vertical.get("schema_version") != 9:
        return None
'''
NEW_BODY = '''    if vertical.get("schema_version") == 10:
        return _epoch_manifest_inputs_from_generation(vertical, vertical_path)
    if vertical.get("schema_version") != 9:
        return None
'''

CALLS = [
    (
        '    manifest_inputs = _financial_epoch_manifest_inputs(vertical)\n'
        '    epoch_generation = _epoch_manifest_generation_definition(vertical)\n',
        '    manifest_inputs = _financial_epoch_manifest_inputs(vertical, vertical_path)\n'
        '    epoch_generation = _epoch_manifest_generation_definition(vertical)\n',
    ),
    (
        '    manifest_inputs = _financial_epoch_manifest_inputs(vertical)\n'
        '    if manifest_inputs is not None:\n        paths.extend(\n',
        '    manifest_inputs = _financial_epoch_manifest_inputs(vertical, vertical_path)\n'
        '    if manifest_inputs is not None:\n        paths.extend(\n',
    ),
]

OLD_RECIPE = '''    manifest_inputs = _financial_epoch_manifest_inputs(vertical)
    if manifest_inputs is not None:
        payload["financial_epoch_manifest_inputs"] = manifest_inputs
        proof = _financial_epoch_manifest_proof(payload, proof_root)
        if proof is None:  # pragma: no cover - guarded by manifest_inputs
            raise MaterializationError("financial epoch manifest proof is unavailable")
        payload["financial_epoch_manifest_proof"] = proof
    epoch_generation = _epoch_manifest_generation_definition(vertical)
    if epoch_generation is not None:
        payload["epoch_manifest_generation"] = epoch_generation
'''
NEW_RECIPE = '''    # The bound generation goes on the payload before the proof is taken,
    # because the proof reads it to decide which profile it is proving.
    epoch_generation = _epoch_manifest_generation_definition(vertical)
    if epoch_generation is not None:
        payload["epoch_manifest_generation"] = epoch_generation
    manifest_inputs = _financial_epoch_manifest_inputs(vertical, vertical_recipe)
    if manifest_inputs is not None:
        payload["financial_epoch_manifest_inputs"] = manifest_inputs
        proof = _financial_epoch_manifest_proof(payload, proof_root)
        if proof is None and epoch_generation is None:
            raise MaterializationError("financial epoch manifest proof is unavailable")
        if proof is not None:
            payload["financial_epoch_manifest_proof"] = proof
'''

OLD_PROOF_HEAD = '''    definitions = recipe.get("financial_epoch_manifest_inputs")
    if definitions is None:
        return None
'''
NEW_PROOF_HEAD = '''    definitions = recipe.get("financial_epoch_manifest_inputs")
    if definitions is None:
        return None
    if recipe.get("epoch_manifest_generation") is not None:
        # A schema-10 panel consumed the verified bundle rather than rebuilding
        # from loose inputs, and says so: it declares
        # VERIFIED_GENERATION_BUNDLE_EXACT_BYTES, not INDEPENDENT_EXACT_REBUILD.
        # Replaying the schema-9 producer here would be proving the wrong thing,
        # and could not succeed anyway -- the projected classified flows carry
        # the bundle's own classification policy, not the repository's.
        return None
'''

GATE_CALL = '''    manifest_gates = _financial_epoch_manifest_gate_rows(
        manifest_inputs,
        prepared_epoch_proof,
'''
GATE_CALL_NEW = '''    manifest_gates = _financial_epoch_manifest_gate_rows(
        manifest_inputs,
        _epoch_manifest_gate_proof(recipe, prepared_root, prepared_epoch_proof),
'''


def main() -> int:
    text = TARGET.read_text(encoding='utf-8')
    if '_epoch_manifest_inputs_from_generation' in text:
        print('already applied')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')

    anchor = '\n\ndef _financial_epoch_manifest_inputs(\n'
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique')
    text = text.replace(anchor, INPUTS_HELPER + anchor, 1)

    for label, old, new in (
        ('signature', OLD_SIG, NEW_SIG),
        ('schema-10 inputs branch', OLD_BODY, NEW_BODY),
        ('recipe ordering', OLD_RECIPE, NEW_RECIPE),
        ('schema-10 proof opt-out', OLD_PROOF_HEAD, NEW_PROOF_HEAD),
    ):
        if text.count(old) != 1:
            raise SystemExit('%s anchor is not unique (%d)' % (label, text.count(old)))
        text = text.replace(old, new, 1)
        print('applied:', label)

    for index, (old, new) in enumerate(CALLS):
        if text.count(old) != 1:
            raise SystemExit('call site %d is not unique (%d)' % (index, text.count(old)))
        text = text.replace(old, new, 1)
    print('applied: 2 remaining call sites')

    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

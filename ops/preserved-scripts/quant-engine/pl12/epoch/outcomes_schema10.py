"""Teach the financial-outcome validator the schema-10 epoch-manifest profile.

`portfolio_financial_outcomes` reads the gate
`financial-epoch-manifest-input-authority` unconditionally, in two places. That
gate describes the schema-9 profile, where a recipe names five loose
epoch-manifest inputs as files.

A schema-10 recipe binds the verified generation instead, and the materializer
says plainly that this profile has no such gate -- see the comment on
`_semantic_gate_payload`, which records that emitting the key anyway made the
semantic gate carry a key its reader rejects and failed every run's last node.
So the gate is genuinely absent by design, and requiring it here is what made a
schema-10 release impossible to validate.

The five inputs are still well defined for this profile: `_rebuild_selected_epochs`
already states what they are in terms of a generation -- the admission policy is
the artifact whose digest the generation pins as `admission_policy`, and the
other four are the canonical encodings of four members of
`authority/calculation-inputs.json`. They are projected from there when the gate
is absent, so every check downstream keeps comparing the same things.

The flow classification policy comes from the generation too. The repository
copy is a different document (1185 bytes) from the one the bundle's classified
flows were produced under (969 bytes, the policy digest they embed), and passing
the repository copy makes the replay refuse with "classified flows classification
policy differs from selected authority".
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_financial_outcomes.py'

Q3 = '"' * 3

HELPER = '''

EPOCH_MANIFEST_CALCULATION_MEMBERS = {
    "account_strategy_ledger": "calculation_ledger",
    "account_strategy_ledger_identity": "calculation_ledger_identity",
    "account_strategy_classified_flows": "calculation_classified_flows",
    "account_strategy_flow_coverage": "calculation_flow_coverage",
}


def _optional_gate(spec: Any, gate_id: str) -> tuple[Path, dict[str, Any]] | None:
    Q3The gate if the specification carries it, rather than insisting on it.Q3

    if not any(gate.gate_id == gate_id for gate in spec.gates):
        return None
    return _gate(spec, gate_id)


def _generation_artifact_path(
    generation_dir: Path, manifest: Any, sha256: str, size_bytes: int
) -> Path:
    Q3Find the artifact a generation pins by digest, without trusting a name.Q3

    matches = [
        artifact
        for artifact in manifest.artifacts
        if artifact.sha256 == sha256 and artifact.size_bytes == size_bytes
    ]
    if len(matches) != 1:
        raise FinancialOutcomeError("selected epoch generation artifact differs")
    parts = matches[0].artifact_id.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise FinancialOutcomeError("selected epoch generation artifact differs")
    path = generation_dir.joinpath(*parts)
    _verified_file(path, sha256, size_bytes)
    return path


def _epoch_manifest_profile(spec: Any) -> dict[str, Any]:
    Q3Resolve the five epoch-manifest inputs for whichever profile is in force.

    Schema 9 names them in the gate. Schema 10 has no gate -- by design -- and
    the same five are projected out of the bound generation, which is exactly
    how `_rebuild_selected_epochs` defines them.
    Q3

    gate = _optional_gate(spec, "financial-epoch-manifest-input-authority")
    if gate is not None:
        _path, payload = gate
        inputs = payload["inputs"]
        selected_bytes = {
            key: _verified_file(
                Path(definition["path"]),
                definition["sha256"],
                definition["size_bytes"],
            )
            for key, definition in inputs.items()
        }
        return {
            "selected_bytes": selected_bytes,
            "admission_policy_path": Path(
                inputs["account_strategy_admission_policy"]["path"]
            ),
            "classification_policy_path": ACCOUNT_STRATEGY_CLASSIFICATION_POLICY,
        }

    _keys, generation_dir, manifest = _selected_epoch_assignment_keys(spec)
    _identity_path, identity_bytes = _manifest_artifact(
        generation_dir, manifest, "authority/selected-input-identities.json"
    )
    identities = json.loads(identity_bytes)
    _calculation_path, calculation_bytes = _manifest_artifact(
        generation_dir, manifest, "authority/calculation-inputs.json"
    )
    calculation = json.loads(calculation_bytes)
    selected_bytes: dict[str, bytes] = {}
    for key, member in EPOCH_MANIFEST_CALCULATION_MEMBERS.items():
        if member not in calculation:
            raise FinancialOutcomeError("selected epoch calculation inputs differ")
        selected_bytes[key] = canonical_json_bytes(calculation[member])
    admission = identities.get("admission_policy")
    if not isinstance(admission, Mapping):
        raise FinancialOutcomeError("selected epoch admission policy identity differs")
    admission_path = _generation_artifact_path(
        generation_dir, manifest, admission["sha256"], admission["size_bytes"]
    )
    selected_bytes["account_strategy_admission_policy"] = admission_path.read_bytes()
    # The classified flows name the policy they were produced under, and it is
    # the bundle's own copy rather than the repository's. Passing the repository
    # copy makes the replay refuse.
    classified = json.loads(selected_bytes["account_strategy_classified_flows"])
    policy = classified.get("classification_policy")
    if not isinstance(policy, Mapping):
        raise FinancialOutcomeError("selected epoch classification policy differs")
    policy_matches = [
        artifact
        for artifact in manifest.artifacts
        if artifact.sha256 == policy.get("policy_sha256")
    ]
    if len(policy_matches) != 1:
        raise FinancialOutcomeError("selected epoch classification policy differs")
    classification_policy_path = _generation_artifact_path(
        generation_dir,
        manifest,
        policy_matches[0].sha256,
        policy_matches[0].size_bytes,
    )
    return {
        "selected_bytes": selected_bytes,
        "admission_policy_path": admission_path,
        "classification_policy_path": classification_policy_path,
    }
'''.replace('Q3', Q3)

OLD_HWM = '''    _, epoch_gate = _gate(spec, "financial-epoch-manifest-input-authority")
    policy = epoch_gate["inputs"]["account_strategy_admission_policy"]
    path = Path(policy["path"])
    _verified_file(path, policy["sha256"], policy["size_bytes"])
    result["account_manifest_admission_policy_path"] = path
    result["account_manifest_flow_classification_policy_path"] = (
        ACCOUNT_STRATEGY_CLASSIFICATION_POLICY
    )
    return result
'''
NEW_HWM = '''    profile = _epoch_manifest_profile(spec)
    result["account_manifest_admission_policy_path"] = profile[
        "admission_policy_path"
    ]
    result["account_manifest_flow_classification_policy_path"] = profile[
        "classification_policy_path"
    ]
    return result
'''

OLD_EPOCHS = '''    _path, gate = _gate(spec, "financial-epoch-manifest-input-authority")
    inputs = gate["inputs"]
    selected_bytes = {}
    for key, definition in inputs.items():
        path = Path(definition["path"])
        selected_bytes[key] = _verified_file(
            path, definition["sha256"], definition["size_bytes"]
        )
'''
NEW_EPOCHS = '''    selected_bytes = _epoch_manifest_profile(spec)["selected_bytes"]
'''


def main() -> int:
    text = TARGET.read_text(encoding='utf-8')
    if '_epoch_manifest_profile' in text:
        print('already applied')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')

    anchor = '\n\ndef _selected_epoch_assignment_keys(\n'
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique (%d)' % text.count(anchor))
    text = text.replace(anchor, HELPER + anchor, 1)

    for label, old, new in (
        ('hwm source paths', OLD_HWM, NEW_HWM),
        ('verify epochs', OLD_EPOCHS, NEW_EPOCHS),
    ):
        if text.count(old) != 1:
            raise SystemExit('%s anchor is not unique (%d)' % (label, text.count(old)))
        text = text.replace(old, new, 1)
        print('applied:', label)

    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

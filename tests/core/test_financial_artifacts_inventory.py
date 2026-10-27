"""Producer-side tests for the production artifact inventory.

The release consumers resolve the selected admission policy from this inventory, because they
cannot resolve it from the epoch-manifest generation: the generation stores a canonicalised
*projection* of the policy, and the captured policy is pretty-printed, which no generation
artifact may be. A consumer test that fabricates the row cannot tell whether the producer emits
it, so the producer is exercised here directly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from quant_engine.core.contracts import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "mvp2-release" / "scripts" / "build_financial_artifacts.py"

# A pretty-printed policy, exactly like the captured pin. Canonicalising it changes the bytes,
# which is the whole reason the input cannot travel inside the generation.
POLICY = b'{\n  "policy_id": "epoch-admission",\n  "admissions": []\n}\n'


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_financial_artifacts", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_root(tmp_path: Path, policy: bytes = POLICY) -> tuple[Path, Path, dict]:
    policy_path = tmp_path / "mapped-epoch-admission-current-v10-dated.json"
    policy_path.write_bytes(policy)
    pin = {
        "path": str(policy_path),
        "sha256": hashlib.sha256(policy).hexdigest(),
        "size_bytes": len(policy),
    }
    checkpoint = tmp_path / "run" / "preparation" / "checkpoints"
    checkpoint.mkdir(parents=True)
    (checkpoint / "epoch.json").write_bytes(
        json.dumps(
            {"input_identity": {"inputs": {"epoch_admission_policy": pin}}}
        ).encode()
    )
    return tmp_path / "run", policy_path, pin


def test_importing_the_inventory_producer_does_not_parse_argv():
    """It has to be importable, or the producer side of this seam cannot be tested at all.

    The roots used to be resolved at module scope, so importing the module parsed `sys.argv`
    and exited. That is exactly why the inventory had no producer test.
    """

    module = _load_module()
    assert hasattr(module, "admission_policy_row")
    assert hasattr(module, "main")


def test_admission_policy_row_names_the_pin_preparation_verified(tmp_path):
    """The emitted node carries the selected input's own bytes, non-canonical and all."""

    module = _load_module()
    run, policy_path, pin = _run_root(tmp_path)

    # The property that makes the generation unable to carry this input.
    assert canonical_json_bytes(json.loads(POLICY)) != POLICY

    row = module.admission_policy_row(run)
    assert row["node_id"] == "financial-account-manifest-admission-policy"
    assert row["kind"] == "ARTIFACT"
    assert Path(row["path"]) == policy_path
    assert row["sha256"] == pin["sha256"]
    assert row["size_bytes"] == pin["size_bytes"]
    assert row["depends_on"] == []


def test_admission_policy_row_refuses_a_file_that_drifted_from_the_pin(tmp_path):
    """A row that silently disagreed with the pin would bind the wrong policy."""

    module = _load_module()
    run, policy_path, _pin = _run_root(tmp_path)
    policy_path.write_bytes(POLICY + b"\n")

    with pytest.raises(SystemExit, match="differs from the pin"):
        module.admission_policy_row(run)


def test_the_emitted_row_is_accepted_by_both_release_consumers(tmp_path):
    """The row the producer emits is the row the consumers resolve. No fabrication either side.

    `AGENTS.md` A1.3: "A consumer suite that mocks the producer does not satisfy this rule --
    the seam needs at least one end-to-end regression from the producer's own output." The
    unit tests on each side of this seam pass with a hand-built row, which is why the seam
    could be broken while both sides were green.
    """

    # Imported, never re-executed from source. Other modules in this suite hold plain
    # `import portfolio_financial_outcomes` / `portfolio_pipeline_materialize` references
    # bound at their own import time, so exec'ing fresh copies over those `sys.modules`
    # entries leaves two identities of the same module alive in one session and breaks
    # tests that never touched this seam.
    from types import SimpleNamespace

    import portfolio_financial_outcomes as outcomes
    import portfolio_pipeline_materialize as materializer

    from quant_engine.core import pipeline as core_pipeline

    module = _load_module()
    run, policy_path, _pin = _run_root(tmp_path)
    row = module.admission_policy_row(run)

    assert materializer._admission_policy_artifact([row]) == policy_path

    node = core_pipeline.ArtifactNode(
        str(row["node_id"]),
        (),
        Path(str(row["path"])),
        str(row["sha256"]),
        int(row["size_bytes"]),
    )
    resolved = outcomes._artifact_node(
        SimpleNamespace(nodes=(node,)), "financial-account-manifest-admission-policy"
    )
    assert (
        outcomes._verified_file(resolved.path, resolved.sha256, resolved.size_bytes)
        == POLICY
    )


def test_main_appends_the_admission_policy_row_to_the_inventory():
    """Emitting the row is what the consumers depend on, not merely being able to build it.

    The other tests call `admission_policy_row` directly, which proves the function is correct
    and proves nothing about whether the inventory contains its output. `main()` reads a full
    run tree, so it cannot be called here; the call is asserted structurally instead, the same
    way `portfolio_controller_graph` asserts a named call exists.
    """

    import ast

    tree = ast.parse(MODULE_PATH.read_bytes().decode("utf-8"))
    main_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = {
        node.func.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "admission_policy_row" in called


def _authority_run(tmp_path):
    """A selected-input record and the provider-ledger generation it points at."""

    ledger = tmp_path / "source" / "generations" / "abc123"
    ledger.mkdir(parents=True)
    for name in (
        "admit-or-declare-receipt.json",
        "generation.json",
        "provider-ledger.json",
    ):
        (ledger / name).write_bytes(json.dumps({"name": name}).encode("utf-8"))
    evidence = tmp_path / "control" / "version-evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'{"portfolio123": "frozen"}')
    record = tmp_path / "dependence-selected-inputs.json"
    record.write_bytes(
        json.dumps(
            {
                "provider_ledger_generation_root": str(ledger),
                "provider_admission_receipt_path": str(
                    ledger / "admit-or-declare-receipt.json"
                ),
                "portfolio123_version_evidence_path": str(evidence),
            }
        ).encode("utf-8")
    )
    vertical = {
        "dependence_selected_inputs": {"path": str(record)},
        "provider_ledger_generation": {"path": str(ledger)},
    }
    return vertical, record, ledger, evidence


def test_authority_rows_name_the_five_files_the_replay_must_pass(tmp_path):
    """The production replay could not verify a RELEASE_QC layered history without these.

    `build_bundle` derives the exact key set a generation must carry from which authority
    arguments its caller supplies (`portfolio_target_series.py:6088-6119`), so the
    specification has to name the files or the replay cannot supply them.
    """

    module = _load_module()
    vertical, _record, ledger, evidence = _authority_run(tmp_path)

    rows = module.history_authority_rows(vertical)
    by_id = {row["node_id"]: row for row in rows}
    assert set(by_id) == {
        "financial-history-authority-admission-receipt",
        "financial-history-authority-ledger-manifest",
        "financial-history-authority-ledger-catalog",
        "financial-history-authority-p123-version",
    }
    assert Path(by_id["financial-history-authority-ledger-manifest"]["path"]) == (
        ledger / "generation.json"
    )
    assert Path(by_id["financial-history-authority-p123-version"]["path"]) == evidence
    for row in rows:
        assert row["kind"] == "ARTIFACT"
        assert row["depends_on"] == []
        content = Path(row["path"]).read_bytes()
        assert row["sha256"] == hashlib.sha256(content).hexdigest()
        assert row["size_bytes"] == len(content)


def test_authority_rows_refuse_a_ledger_generation_the_recipe_did_not_declare(tmp_path):
    """The record must not be able to nominate its own auditor."""

    module = _load_module()
    vertical, _record, _ledger, _evidence = _authority_run(tmp_path)
    other = tmp_path / "source" / "generations" / "def456"
    other.mkdir(parents=True)
    vertical["provider_ledger_generation"] = {"path": str(other)}

    with pytest.raises(SystemExit, match="differs from the recipe"):
        module.history_authority_rows(vertical)


def test_authority_rows_refuse_a_receipt_outside_its_ledger_generation(tmp_path):
    """The verifier requires the trio to be siblings; a row set that is not would fail late."""

    module = _load_module()
    vertical, record, ledger, evidence = _authority_run(tmp_path)
    stray = tmp_path / "admit-or-declare-receipt.json"
    stray.write_bytes(b"{}")
    selected = json.loads(record.read_bytes())
    selected["provider_admission_receipt_path"] = str(stray)
    record.write_bytes(json.dumps(selected).encode("utf-8"))

    with pytest.raises(SystemExit, match="outside its ledger generation"):
        module.history_authority_rows(vertical)


def test_authority_rows_omit_version_evidence_the_run_did_not_have(tmp_path):
    """The selected-input record declares version evidence optional, so no row is invented."""

    module = _load_module()
    vertical, record, _ledger, _evidence = _authority_run(tmp_path)
    selected = json.loads(record.read_bytes())
    del selected["portfolio123_version_evidence_path"]
    record.write_bytes(json.dumps(selected).encode("utf-8"))

    rows = module.history_authority_rows(vertical)
    assert {row["node_id"] for row in rows} == {
        "financial-history-authority-admission-receipt",
        "financial-history-authority-ledger-manifest",
        "financial-history-authority-ledger-catalog",
    }


def test_the_emitted_authority_rows_reach_the_replay(tmp_path):
    """The seam itself, from the producer's own output rather than fabricated nodes.

    AGENTS.md A1.3: a consumer suite that mocks the producer does not satisfy the rule. The
    consumer test builds its own nodes and the producer test stops at the rows, so a node ID that
    drifted on one side would leave both green while production replay received nothing.
    """

    import portfolio_financial_outcomes as outcomes
    from types import SimpleNamespace

    from quant_engine.core import pipeline

    module = _load_module()
    vertical, _record, _ledger, _evidence = _authority_run(tmp_path)
    rows = module.history_authority_rows(vertical)
    spec = SimpleNamespace(
        nodes=[
            pipeline.ArtifactNode(
                str(row["node_id"]),
                tuple(row["depends_on"]),
                Path(str(row["path"])),
                str(row["sha256"]),
                int(row["size_bytes"]),
            )
            for row in rows
        ]
    )

    resolved = outcomes._history_authorities(spec, tmp_path / "bundle")
    assert set(resolved) == {
        "provider_admission_receipt_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "portfolio123_version_evidence_path",
        "financial_input_contract_path",
    }


def test_main_appends_the_authority_rows_to_the_inventory():
    """Emitting the rows is what the replay depends on, not merely being able to build them.

    Same reasoning as the admission-policy row: `main()` reads a full run tree and cannot be
    called here, so the call is asserted structurally.
    """

    import ast

    tree = ast.parse(MODULE_PATH.read_bytes().decode("utf-8"))
    main_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = {
        node.func.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "history_authority_rows" in called


def _epoch_checkpoint_with_facts(tmp_path: Path, files: dict[str, bytes]) -> Path:
    checkpoint = tmp_path / "run" / "preparation" / "checkpoints"
    checkpoint.mkdir(parents=True)
    pins = []
    for name, payload in sorted(files.items()):
        path = tmp_path / "facts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(payload)
        pins.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    (checkpoint / "epoch.json").write_bytes(
        json.dumps({"result": {"sleeve_facts": pins}}).encode()
    )
    return tmp_path / "run"


def test_sleeve_facts_rows_name_every_file_preparation_consumed(tmp_path):
    from types import SimpleNamespace

    import portfolio_financial_outcomes as outcomes
    import portfolio_pipeline_materialize as materializer

    from quant_engine.core import pipeline as core_pipeline

    module = _load_module()
    files = {"a.json": b'{"a":1}\n', "b.json": b'{"b":2}\n'}
    run = _epoch_checkpoint_with_facts(tmp_path, files)
    rows = module.sleeve_facts_rows(run)

    assert len(rows) == 2
    assert {row["node_id"] for row in rows} == {
        outcomes.sleeve_facts_node_id(hashlib.sha256(payload).hexdigest())
        for payload in files.values()
    }
    assert all(row["kind"] == "ARTIFACT" and row["depends_on"] == [] for row in rows)
    # producer and both consumers agree, with nothing fabricated on either side
    assert {path.name for path in materializer._production_sleeve_facts_paths(rows)} == set(
        files
    )
    nodes = tuple(
        core_pipeline.ArtifactNode(
            str(row["node_id"]),
            (),
            Path(str(row["path"])),
            str(row["sha256"]),
            int(row["size_bytes"]),
        )
        for row in rows
    )
    assert {
        path.name for path in outcomes.selected_sleeve_facts_paths(SimpleNamespace(nodes=nodes))
    } == set(files)


def test_a_run_without_sleeve_facts_adds_no_row(tmp_path):
    module = _load_module()
    assert module.sleeve_facts_rows(_epoch_checkpoint_with_facts(tmp_path, {})) == []


def test_sleeve_facts_rows_refuse_a_file_that_drifted_from_the_record(tmp_path):
    module = _load_module()
    run = _epoch_checkpoint_with_facts(tmp_path, {"a.json": b'{"a":1}\n'})
    (tmp_path / "facts" / "a.json").write_bytes(b'{"a":2}\n')
    with pytest.raises(SystemExit, match="differ from the files preparation consumed"):
        module.sleeve_facts_rows(run)


def test_consumers_refuse_a_sleeve_facts_node_that_does_not_name_its_digest(tmp_path):
    from types import SimpleNamespace

    import portfolio_financial_outcomes as outcomes
    import portfolio_pipeline_materialize as materializer

    from quant_engine.core import pipeline as core_pipeline

    module = _load_module()
    run = _epoch_checkpoint_with_facts(tmp_path, {"a.json": b'{"a":1}\n'})
    (row,) = module.sleeve_facts_rows(run)
    row = {**row, "node_id": "financial-account-manifest-sleeve-facts-0123456789abcdef"}
    with pytest.raises(materializer.MaterializationError, match="binding differs"):
        materializer._production_sleeve_facts_paths([row])
    node = core_pipeline.ArtifactNode(
        str(row["node_id"]),
        (),
        Path(str(row["path"])),
        str(row["sha256"]),
        int(row["size_bytes"]),
    )
    with pytest.raises(outcomes.FinancialOutcomeError, match="does not name its digest"):
        outcomes.selected_sleeve_facts_paths(SimpleNamespace(nodes=(node,)))

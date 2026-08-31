"""Behavioral tests for the explicit current-epoch manifest staging CLI."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

import pytest
from quant_engine.core import InputSnapshot, read_generation_bundle
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
EXISTING_FIXTURES_PATH = (
    REPOSITORY_ROOT
    / "tests"
    / "portfolio_control_panel"
    / "test_account_manifest_generation.py"
)
BUILDER_PATH = TOOLS_ROOT / "build_account_manifest_generation.py"
RUNNER_PATH = TOOLS_ROOT / "stage_current_epoch_manifest.py"


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder_module() -> Any:
    return _load_module(BUILDER_PATH, "build_account_manifest_generation")


@pytest.fixture(scope="module")
def existing_fixtures() -> Any:
    return _load_module(EXISTING_FIXTURES_PATH, "stage_manifest_existing_fixtures")


@pytest.fixture(scope="module")
def runner_module() -> Any:
    sys.path.insert(0, str(TOOLS_ROOT))
    try:
        try:
            return importlib.import_module("stage_current_epoch_manifest")
        except ModuleNotFoundError:
            return None
    finally:
        sys.path.remove(str(TOOLS_ROOT))


def _valid_selected_inputs(existing_fixtures: Any, builder_module: Any) -> object:
    """Adapt the existing mixed fixture to the current 13-epoch contract."""

    selected = existing_fixtures._generation_composer_selected_inputs(builder_module)
    baseline = selected.baseline.json_object()
    providers = {row["account_id"]: row["provider"] for row in baseline["accounts"]}

    crosswalk = deepcopy(selected.current_epoch_crosswalk.json_object())
    crosswalk["selected_baseline"].update(
        {
            "generation_id": selected.baseline_generation_id,
            "sha256": selected.baseline.sha256,
            "size_bytes": selected.baseline.size_bytes,
            "as_of_date": baseline["as_of_date"],
            "schema_version": str(baseline["baseline_schema_version"]),
        }
    )
    for row in crosswalk["rows"]:
        row["provider"] = providers[row["account_id"]]

    admission = deepcopy(selected.admission_policy.json_object())
    second = admission["admissions"][1]
    second.clear()
    second.update(
        {
            "account_id": "synthetic-account-02",
            "strategy_id": "synthetic-strategy-02",
            "effective_start": "2025-01-01",
            "effective_end": None,
            "status": "BLOCKED",
            "continuity_to_successor": False,
            "reason": "SYNTHETIC_BLOCKED",
        }
    )

    values = existing_fixtures._selected_input_kwargs(selected)
    values.update(
        {
            "current_epoch_crosswalk": InputSnapshot.from_bytes(
                "current 13-epoch crosswalk", canonical_json_bytes(crosswalk)
            ),
            "receipt_authority": selected.receipt_authority,
            "admission_policy": InputSnapshot.from_bytes(
                "current admission policy", canonical_json_bytes(admission)
            ),
            "manifest_schema": selected.manifest_schema,
        }
    )
    return builder_module.AccountManifestSelectedInputs(**values)


def _write(path: Path, contents: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def _materialize_inputs(tmp_path: Path, selected: Any) -> dict[str, object]:
    provider_root = tmp_path / "provider-ledger"
    provider_generation = (
        provider_root / "generations" / selected.provider_ledger_generation_id
    )
    _write(
        provider_generation / "provider-ledger.json", selected.provider_ledger.contents
    )
    _write(
        provider_generation / "generation.json",
        selected.provider_ledger_manifest.contents,
    )

    baseline_root = tmp_path / "baseline"
    baseline_generation = (
        baseline_root / "generations" / selected.baseline_generation_id
    )
    _write(baseline_generation / "portfolio-baseline.json", selected.baseline.contents)
    _write(baseline_generation / "generation.json", selected.baseline_manifest.contents)

    authority_root = tmp_path / "authority"
    flow_policy = _write(
        authority_root / "flow-policy.json",
        selected.flow_classification_policy.contents,
    )
    crosswalk = _write(
        authority_root / "crosswalk.json", selected.current_epoch_crosswalk.contents
    )
    manifest_schema = _write(
        authority_root / "manifest-schema.json", selected.manifest_schema.contents
    )
    receipt_authority = _write(
        authority_root / "receipt-authority.json", selected.receipt_authority.contents
    )
    admission_policy = _write(
        authority_root / "admission-policy.json", selected.admission_policy.contents
    )
    receipts = {
        artifact_id: _write(
            authority_root / "receipts" / f"receipt-{index}.json", snapshot.contents
        )
        for index, (artifact_id, snapshot) in enumerate(
            sorted(selected.retrieval_receipts.items())
        )
    }
    return {
        "provider_ledger_generation_root": provider_root,
        "provider_ledger_generation_id": selected.provider_ledger_generation_id,
        "baseline_generation_root": baseline_root,
        "baseline_generation_id": selected.baseline_generation_id,
        "flow_policy_path": flow_policy,
        "crosswalk_path": crosswalk,
        "manifest_schema_path": manifest_schema,
        "receipt_authority_path": receipt_authority,
        "admission_policy_path": admission_policy,
        "receipt_paths": receipts,
    }


def test_runner_stages_exact_explicit_inputs_without_current_pointer(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
) -> None:
    """Only the named generation IDs and authority files feed the staged bundle."""

    assert runner_module is not None, (
        "the current-epoch manifest staging runner is missing"
    )
    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    _write(
        arguments["provider_ledger_generation_root"] / "CURRENT.json",
        b'{"generation_id":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}\n',
    )
    _write(
        arguments["baseline_generation_root"] / "CURRENT.json",
        b'{"generation_id":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}\n',
    )
    output_root = tmp_path / "output"

    result = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=output_root
    )

    assert {"generation_id", "artifacts"} <= set(result)
    assert len(result["generation_id"]) == 64
    generation = output_root / "generations" / result["generation_id"]
    assert generation.is_dir()
    assert len(result["artifacts"]) == 14
    for identity in result["artifacts"]:
        artifact = generation / identity["artifact_id"]
        contents = artifact.read_bytes()
        assert hashlib.sha256(contents).hexdigest() == identity["sha256"]
        assert len(contents) == identity["size_bytes"]

    captured = json.loads(
        (generation / "authority" / "selected-input-identities.json").read_bytes()
    )
    assert (
        captured["provider_ledger"]["artifact"]["sha256"]
        == hashlib.sha256(selected.provider_ledger.contents).hexdigest()
    )
    assert (
        captured["baseline"]["artifact"]["sha256"]
        == hashlib.sha256(selected.baseline.contents).hexdigest()
    )
    assert (
        captured["current_epoch_crosswalk"]["sha256"]
        == hashlib.sha256(selected.current_epoch_crosswalk.contents).hexdigest()
    )

    manifest = json.loads(
        (generation / "outputs" / "account-strategy-manifest.json").read_bytes()
    )
    assert len(manifest["rows"]) == 13
    assert [row["status"] for row in manifest["rows"]].count("OK") == 1
    assert [row["status"] for row in manifest["rows"]].count("BLOCKED_ADMISSION") == 12


def test_runner_writes_exact_report_source_receipt_outside_bundle(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
) -> None:
    """The report source sidecar binds both subjects without self-reference."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    output_root = tmp_path / "output"
    result = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=output_root
    )

    assert set(result) == {"generation_id", "artifacts", "source_receipt"}
    generation_id = result["generation_id"]
    generation = output_root / "generations" / generation_id
    source_path = (
        output_root / "report-inputs" / generation_id / "epoch-manifest-source.json"
    )
    source_bytes = source_path.read_bytes()
    generation_manifest_bytes = (generation / "generation.json").read_bytes()
    generation_receipt_bytes = (generation / "generation-receipt.json").read_bytes()
    generation_manifest = json.loads(generation_manifest_bytes)
    artifacts = {
        artifact["artifact_id"]: artifact
        for artifact in generation_manifest["artifacts"]
    }

    def descriptor(artifact_id: str) -> dict[str, object]:
        artifact = artifacts[artifact_id]
        payload = (generation / artifact_id).read_bytes()
        return {
            "path": artifact_id,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "schema": artifact["schema"],
        }

    bundle = read_generation_bundle(output_root, generation_id)
    expected = {
        "generation_id": generation_id,
        "generation_manifest_sha256": hashlib.sha256(
            generation_manifest_bytes
        ).hexdigest(),
        "generation_receipt_sha256": hashlib.sha256(
            generation_receipt_bytes
        ).hexdigest(),
        "full_directory_inventory_sha256": (bundle.full_directory_inventory_sha256),
        "artifact": descriptor("outputs/account-strategy-manifest.json"),
        "crosswalk": descriptor("authority/account-assignment-crosswalk.json"),
    }
    assert source_bytes == canonical_json_bytes(expected)
    assert result["source_receipt"] == {
        "path": str(source_path),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size_bytes": len(source_bytes),
    }
    assert source_path.is_relative_to(output_root / "report-inputs")
    assert not source_path.is_relative_to(generation)
    inventory = json.loads((generation / "generation-inventory.json").read_bytes())
    assert all(
        Path(item["relative_path"]).name != "epoch-manifest-source.json"
        for item in inventory["files"]
    )
    assert all(
        Path(artifact_id).name != "epoch-manifest-source.json"
        for artifact_id in artifacts
    )


def test_runner_is_byte_deterministic_across_two_fresh_roots(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
) -> None:
    """Output location does not participate in generation identity or bytes."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)

    first = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=tmp_path / "output-a"
    )
    second = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=tmp_path / "output-b"
    )

    assert first["generation_id"] == second["generation_id"]
    assert first["artifacts"] == second["artifacts"]
    assert first["source_receipt"]["sha256"] == second["source_receipt"]["sha256"]
    assert (
        first["source_receipt"]["size_bytes"] == second["source_receipt"]["size_bytes"]
    )
    assert len(first["generation_id"]) == 64
    assert (
        Path(first["source_receipt"]["path"]).read_bytes()
        == Path(second["source_receipt"]["path"]).read_bytes()
    )
    for relative_path in (
        "generation.json",
        "generation-inventory.json",
        "generation-receipt.json",
        "outputs/account-strategy-manifest.json",
        "verification/independent-verification.json",
    ):
        assert (
            tmp_path
            / "output-a"
            / "generations"
            / first["generation_id"]
            / relative_path
        ).read_bytes() == (
            tmp_path
            / "output-b"
            / "generations"
            / second["generation_id"]
            / relative_path
        ).read_bytes()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_catalog",
        "catalog_bytes",
        "missing_receipt",
        "receipt_bytes",
        "generation_id",
    ],
)
def test_runner_rejects_missing_or_mismatched_selected_bytes_before_output(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
    mutation: str,
) -> None:
    """A missing path or one changed source identity cannot produce a generation."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    provider_catalog = (
        arguments["provider_ledger_generation_root"]
        / "generations"
        / arguments["provider_ledger_generation_id"]
        / "provider-ledger.json"
    )
    if mutation == "missing_catalog":
        provider_catalog.unlink()
    elif mutation == "catalog_bytes":
        provider_catalog.write_bytes(provider_catalog.read_bytes() + b" ")
    elif mutation in {"missing_receipt", "receipt_bytes"}:
        receipt = next(iter(arguments["receipt_paths"].values()))
        if mutation == "missing_receipt":
            receipt.unlink()
        else:
            receipt.write_bytes(receipt.read_bytes() + b" ")
    else:
        arguments["provider_ledger_generation_id"] = "e" * 64

    output_root = tmp_path / "rejected-output"
    with pytest.raises(ValueError):
        runner_module.stage_current_epoch_manifest(
            **arguments, output_generation_root=output_root
        )
    assert not output_root.exists()


def test_runner_rejects_attested_source_mismatch_before_output(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selected producer byte change is rejected by the executing-code attestation."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    producer, builder, verifier = runner_module._code_snapshots()
    mismatched_producer = InputSnapshot.from_bytes(
        "changed producer", producer.contents + b"\n"
    )
    monkeypatch.setattr(
        runner_module,
        "_code_snapshots",
        lambda: (mismatched_producer, builder, verifier),
    )
    output_root = tmp_path / "source-rejected-output"

    with pytest.raises(ValueError, match="differs from executing bytes"):
        runner_module.stage_current_epoch_manifest(
            **arguments, output_generation_root=output_root
        )
    assert not output_root.exists()


def test_source_receipt_is_idempotent_but_rejects_changed_existing_bytes(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
) -> None:
    """An identical repeat is harmless while a pre-existing conflict stops reuse."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    output_root = tmp_path / "sidecar-output"
    first = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=output_root
    )
    second = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=output_root
    )
    assert first == second

    source_path = Path(first["source_receipt"]["path"])
    source_path.write_bytes(canonical_json_bytes({"changed": True}))
    with pytest.raises(
        runner_module.CurrentEpochManifestStageError,
        match="create-only epoch manifest source receipt differs",
    ):
        runner_module.stage_current_epoch_manifest(
            **arguments, output_generation_root=output_root
        )
    read_generation_bundle(output_root, first["generation_id"])


def test_runner_has_no_network_or_wall_clock_dependency(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Immutable input dates suffice while network and wall clocks are unavailable."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("external network or wall-clock dependency was used")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    result = runner_module.stage_current_epoch_manifest(
        **arguments, output_generation_root=tmp_path / "offline-output"
    )
    assert len(result["generation_id"]) == 64


def test_cli_emits_only_generation_and_exact_artifact_identities(
    tmp_path: Path,
    runner_module: Any,
    builder_module: Any,
    existing_fixtures: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command-line success surface excludes paths, accounts, and pointer state."""

    selected = _valid_selected_inputs(existing_fixtures, builder_module)
    arguments = _materialize_inputs(tmp_path, selected)
    argv = [
        "--provider-ledger-generation-root",
        str(arguments["provider_ledger_generation_root"]),
        "--provider-ledger-generation-id",
        arguments["provider_ledger_generation_id"],
        "--baseline-generation-root",
        str(arguments["baseline_generation_root"]),
        "--baseline-generation-id",
        arguments["baseline_generation_id"],
        "--flow-policy",
        str(arguments["flow_policy_path"]),
        "--crosswalk",
        str(arguments["crosswalk_path"]),
        "--manifest-schema",
        str(arguments["manifest_schema_path"]),
        "--receipt-authority",
        str(arguments["receipt_authority_path"]),
        "--admission-policy",
        str(arguments["admission_policy_path"]),
    ]
    for artifact_id, path in arguments["receipt_paths"].items():
        argv.extend(["--receipt", f"{artifact_id}={path}"])
    argv.extend(["--output-generation-root", str(tmp_path / "cli-output")])

    assert runner_module.main(argv) == 0
    streams = capsys.readouterr()
    assert streams.err == ""
    emitted = json.loads(streams.out)
    assert set(emitted) == {"generation_id", "artifacts", "source_receipt"}
    assert set(emitted["source_receipt"]) == {"path", "sha256", "size_bytes"}
    assert all(
        set(identity)
        == {
            "artifact_id",
            "as_of_date",
            "schema",
            "sha256",
            "size_bytes",
        }
        for identity in emitted["artifacts"]
    )


def test_cli_is_directly_executable_from_the_repository_root() -> None:
    """The documented script entrypoint resolves the existing tools package."""

    completed = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--help"],
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--provider-ledger-generation-root" in completed.stdout
    assert "--output-generation-root" in completed.stdout

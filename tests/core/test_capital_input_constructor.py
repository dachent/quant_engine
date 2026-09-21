"""Focused interface and import-boundary tests for the capital constructor."""

from __future__ import annotations

import builtins
import importlib.util
import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[2] / "ops/mvp2-release/scripts/build_capital_input.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("capital_input_constructor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_read_sources_or_resolve_machine_paths(monkeypatch):
    reads = []
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def record_bytes(self):
        reads.append(self)
        return original_read_bytes(self)

    def record_text(self, *args, **kwargs):
        reads.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", record_bytes)
    monkeypatch.setattr(Path, "read_text", record_text)
    module = _load_module()

    assert not reads
    assert list(inspect.signature(module.build_input_generation).parameters) == [
        "provider_ledger_generation_root",
        "epoch_manifest_generation_root",
        "provider_ledger_spec_path",
        "financial_contract_path",
        "strategy_registry_path",
        "account_crosswalk_path",
        "requested_as_of",
        "evaluated_on",
    ]


def test_invalid_evaluation_is_rejected_before_source_import_or_reads(monkeypatch):
    module = _load_module()
    source_root = Path("C:/definitely-missing-capital-constructor-fixture")
    imported = []
    original_import = builtins.__import__

    def record_import(name, *args, **kwargs):
        imported.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", record_import)
    with pytest.raises(ValueError, match="before requested_as_of"):
        module.build_input_generation(
            provider_ledger_generation_root=source_root / "ledger",
            epoch_manifest_generation_root=source_root / "epochs",
            provider_ledger_spec_path=source_root / "spec.json",
            financial_contract_path=source_root / "contract.json",
            strategy_registry_path=source_root / "registry.json",
            account_crosswalk_path=source_root / "crosswalk.json",
            requested_as_of="2026-09-17",
            evaluated_on="2026-09-16",
        )
    assert "portfolio_invested_capital" not in imported


def test_cli_resolves_repo_tools_before_reaching_date_validation(tmp_path):
    source_root = tmp_path / "missing-source-root"
    output_root = tmp_path / "output"
    args = [
        sys.executable,
        str(SCRIPT),
        "--provider-ledger-generation-root",
        str(source_root / "ledger"),
        "--epoch-manifest-generation-root",
        str(source_root / "epochs"),
        "--provider-ledger-spec",
        str(source_root / "spec.json"),
        "--financial-contract",
        str(source_root / "contract.json"),
        "--strategy-registry",
        str(source_root / "registry.json"),
        "--account-crosswalk",
        str(source_root / "crosswalk.json"),
        "--requested-as-of",
        "2026-09-17",
        "--evaluated-on",
        "2026-09-16",
        "--output-root",
        str(output_root),
    ]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        args,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    combined_output = completed.stdout + completed.stderr
    assert "evaluated_on cannot be before requested_as_of" in combined_output
    assert "ModuleNotFoundError" not in combined_output


def test_preparation_unpacks_exactly_what_the_builder_returns() -> None:
    """The attended caller and the builder must agree on the return shape.

    The builder returns `(generation, locators)` so the generation can stay
    portable while this machine's paths go to a sidecar. Changing that return
    from a dict to a tuple broke `portfolio_attended_preparation` with
    "dictionary update sequence element #0 has length 12", and no test caught
    it -- the signature check above pins parameter names, not the return -- so
    a full pipeline run was the first thing to notice.
    """

    import ast

    module = _load_module()
    returns = str(inspect.signature(module.build_input_generation).return_annotation)
    assert returns == "tuple[dict, dict[str, str]]", (
        "the builder's declared return changed; every caller must be updated "
        f"with it, not just this annotation: {returns!r}"
    )

    preparation = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "portfolio_attended_preparation.py"
    )
    tree = ast.parse(preparation.read_text(encoding="utf-8"))
    unpacks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Tuple)
        and len(node.targets[0].elts) == 2
        and any(
            isinstance(element, ast.Name) and element.id == "capital_input_value"
            for element in node.targets[0].elts
        )
    ]
    assert unpacks, (
        "portfolio_attended_preparation no longer unpacks the builder's two "
        "return values, so it will write a tuple where a generation belongs"
    )

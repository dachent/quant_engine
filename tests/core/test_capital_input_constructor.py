"""Focused interface and import-boundary tests for the capital constructor."""

from __future__ import annotations

import builtins
import importlib.util
import inspect
from pathlib import Path

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

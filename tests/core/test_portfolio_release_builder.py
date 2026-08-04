from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_release_builder.py"
SPEC = importlib.util.spec_from_file_location("portfolio_release_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _file_definition(path: Path) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _recipe(tmp_path: Path) -> Path:
    provider = tmp_path / "provider.json"
    baseline = tmp_path / "baseline.json"
    legacy = tmp_path / "legacy.json"
    provider.write_text("{}", encoding="utf-8")
    baseline.write_text("{}", encoding="utf-8")
    legacy.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    (plan / "inputs").mkdir(parents=True)
    correlation.mkdir()
    (plan / "inputs" / "one.txt").write_text("one", encoding="utf-8")
    (correlation / "returns.csv").write_text("date,value\n", encoding="utf-8")
    plan_inventory = builder._directory_inventory(plan)
    correlation_inventory = builder._directory_inventory(correlation)
    payload = {
        "as_of_date": "2026-08-03",
        "baseline_spec": _file_definition(baseline),
        "correlation_source_root": {"path": str(correlation), **correlation_inventory},
        "decomposition_plan_bundle": {"path": str(plan), **plan_inventory},
        "legacy_panel": _file_definition(legacy),
        "pipeline_id": "synthetic-source-to-panel",
        "provider_ledger_spec": _file_definition(provider),
        "release_commit": "a" * 40,
        "schema_version": 2,
    }
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps(payload), encoding="utf-8")
    return recipe


def test_recipe_binds_file_and_directory_inputs(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path)

    accepted = builder._recipe(recipe)

    assert accepted["pipeline_id"] == "synthetic-source-to-panel"


def test_recipe_rejects_directory_drift(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    plan = Path(payload["decomposition_plan_bundle"]["path"])
    (plan / "inputs" / "one.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(builder.VerticalBuildError, match="inventory differs"):
        builder._recipe(recipe)


def test_directory_inventory_is_path_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        (root / "b.txt").write_text("b", encoding="utf-8")
        (root / "a.txt").write_text("a", encoding="utf-8")

    assert builder._directory_inventory(first) == builder._directory_inventory(second)

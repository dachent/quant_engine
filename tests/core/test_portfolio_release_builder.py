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
    base_factor = tmp_path / "factors.csv"
    legacy = tmp_path / "legacy"
    for path in (provider, baseline):
        path.write_text("{}", encoding="utf-8")
    legacy.mkdir()
    (legacy / "app.js").write_text("// legacy shell", encoding="utf-8")
    base_factor.write_text("date,RF\n", encoding="utf-8")
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
        "base_factor_file": _file_definition(base_factor),
        "baseline_spec": _file_definition(baseline),
        "correlation_source_root": {"path": str(correlation), **correlation_inventory},
        "decomposition_plan_bundle": {"path": str(plan), **plan_inventory},
        "legacy_panel": {"path": str(legacy), **builder._directory_inventory(legacy)},
        "pipeline_id": "synthetic-source-to-panel",
        "provider_ledger_spec": _file_definition(provider),
        "release_commit": "a" * 40,
        "schema_version": 3,
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


def test_recipe_rejects_legacy_panel_shell_drift(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    legacy = Path(payload["legacy_panel"]["path"])
    (legacy / "app.js").write_text("// changed", encoding="utf-8")

    with pytest.raises(
        builder.VerticalBuildError, match="legacy_panel inventory differs"
    ):
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


def test_create_recipe_hash_binds_every_vertical_input(tmp_path: Path) -> None:
    provider = tmp_path / "provider.json"
    baseline = tmp_path / "baseline.json"
    base_factor = tmp_path / "factors.csv"
    legacy = tmp_path / "legacy"
    for path in (provider, baseline):
        path.write_text("{}", encoding="utf-8")
    legacy.mkdir()
    (legacy / "app.js").write_text("// legacy shell", encoding="utf-8")
    base_factor.write_text("date,RF\n", encoding="utf-8")
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    plan.mkdir()
    correlation.mkdir()
    (plan / "plan.json").write_text("{}", encoding="utf-8")
    (correlation / "returns.csv").write_text("date,value\n", encoding="utf-8")
    output = tmp_path / "generated-recipe.json"

    result = builder.create_recipe(
        provider_ledger_spec=provider,
        baseline_spec=baseline,
        base_factor_file=base_factor,
        decomposition_plan_bundle=plan,
        correlation_source_root=correlation,
        legacy_panel=legacy,
        as_of_date="2026-08-03",
        pipeline_id="synthetic-vertical",
        release_commit="a" * 40,
        output=output,
    )

    assert result["recipe"] == str(output)
    assert builder._recipe(output)["schema_version"] == 3


def test_xlsx_startup_deadline_is_the_only_extended_failure() -> None:
    startup = {
        "final_state": "TIMED_OUT",
        "steps": [
            {
                "error": {
                    "code": "PHASE_DEADLINE_EXCEEDED",
                    "details": {"phase": "STARTING_EXCEL"},
                }
            }
        ],
    }
    assert builder._is_xlsx_startup_deadline(startup) is True
    startup["steps"][0]["error"]["details"]["phase"] = "CALCULATING"
    assert builder._is_xlsx_startup_deadline(startup) is False
    startup["steps"][0]["error"]["details"]["phase"] = "STARTING_EXCEL"
    startup["steps"][0]["error"]["code"] = "WORKBOOK_OPEN_FAILED"
    assert builder._is_xlsx_startup_deadline(startup) is False


def test_xlsx_startup_extension_is_bounded_and_preserves_job() -> None:
    job = {
        "idempotency_key": "synthetic",
        "schema_version": "2.0",
        "steps": [{"type": "open", "workbook_path": "synthetic.xlsx"}],
    }
    extended = builder._xlsx_job_with_startup_extension(job)
    assert extended["idempotency_key"] == "synthetic"
    assert extended["steps"] == job["steps"]
    assert extended["timeouts"] == {
        "calculation_seconds": 180,
        "close_seconds": 300,
        "open_workbook_seconds": 60,
        "refresh_total_seconds": 300,
        "save_seconds": 60,
        "start_excel_seconds": 120,
    }


def test_xlsx_startup_extension_rejects_unvalidated_job_shape() -> None:
    with pytest.raises(builder.VerticalBuildError, match="keys differ"):
        builder._xlsx_job_with_startup_extension(
            {
                "idempotency_key": "synthetic",
                "schema_version": "2.0",
                "steps": [],
                "unknown": True,
            }
        )

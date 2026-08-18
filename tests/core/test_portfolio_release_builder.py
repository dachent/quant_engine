from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)


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


def _provider_ledger_generation(tmp_path: Path) -> Path:
    catalog = (
        json.dumps(
            {"as_of_date": "2026-08-03", "catalog_schema_version": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    manifest = GenerationManifest.build(
        as_of_date=builder.date.fromisoformat("2026-08-03"),
        adapter=AdapterIdentity("quant-engine-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("positions",),
            observed_start=builder.date.fromisoformat("2026-08-03"),
            observed_end=builder.date.fromisoformat("2026-08-03"),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=hashlib.sha256(catalog).hexdigest(),
                size_bytes=len(catalog),
                as_of_date=builder.date.fromisoformat("2026-08-03"),
                schema_name="provider-ledger-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = tmp_path / manifest.generation_id
    generation.mkdir()
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "provider-ledger.json").write_bytes(catalog)
    return generation


def _recipe(tmp_path: Path) -> Path:
    provider = tmp_path / "provider.json"
    baseline = tmp_path / "baseline.json"
    base_factor = tmp_path / "factors.csv"
    implementation = tmp_path / "implementation.json"
    implementation_return_attribution = (
        tmp_path / "implementation-return-attribution.json"
    )
    attribution = tmp_path / "strategy-attribution"
    legacy = tmp_path / "legacy"
    for path in (
        provider,
        baseline,
        implementation,
        implementation_return_attribution,
    ):
        path.write_text("{}", encoding="utf-8")
    legacy.mkdir()
    attribution.mkdir()
    (attribution / "CURRENT.json").write_text("{}", encoding="utf-8")
    (legacy / "app.js").write_text("// legacy shell", encoding="utf-8")
    base_factor.write_text("date,RF\n", encoding="utf-8")
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    (plan / "inputs").mkdir(parents=True)
    correlation.mkdir()
    (plan / "inputs" / "one.txt").write_text("one", encoding="utf-8")
    returns = correlation / "returns.csv"
    returns.write_text("date,value\n", encoding="utf-8")
    content = returns.read_bytes()
    (correlation / "evidence-proof.json").write_text(
        json.dumps(
            {
                "as_of_date": "2026-08-03",
                "builder": "portfolio-target-series/layered-correlation-1",
                "evidence_mode_policy": {
                    "default": "LIVE_ONLY",
                    "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
                    "post_inception_backtest_fill": False,
                },
                "series_mappings": [
                    {
                        "backtest_observations": 1,
                        "history_status": "ACCEPTED_EXACT_VERSION_HISTORY",
                        "series_id": f"Strategy {index}",
                        "strategy_version": f"synthetic:{index}",
                    }
                    for index in range(9)
                ],
                "target_sha256": hashlib.sha256(content).hexdigest(),
                "target_size_bytes": len(content),
            }
        ),
        encoding="utf-8",
    )
    plan_inventory = builder._directory_inventory(plan)
    correlation_inventory = builder._directory_inventory(correlation)
    payload = {
        "as_of_date": "2026-08-03",
        "base_factor_file": _file_definition(base_factor),
        "baseline_spec": _file_definition(baseline),
        "correlation_source_root": {"path": str(correlation), **correlation_inventory},
        "decomposition_plan_bundle": {"path": str(plan), **plan_inventory},
        "implementation_reconciliation": _file_definition(implementation),
        "implementation_return_attribution": _file_definition(
            implementation_return_attribution
        ),
        "legacy_panel": {"path": str(legacy), **builder._directory_inventory(legacy)},
        "pipeline_id": "synthetic-source-to-panel",
        "provider_ledger_spec": _file_definition(provider),
        "release_commit": "a" * 40,
        "schema_version": 6,
        "strategy_attribution_root": {
            "path": str(attribution),
            **builder._directory_inventory(attribution),
        },
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
    implementation = tmp_path / "implementation.json"
    implementation_return_attribution = (
        tmp_path / "implementation-return-attribution.json"
    )
    attribution = tmp_path / "strategy-attribution"
    legacy = tmp_path / "legacy"
    for path in (
        provider,
        baseline,
        implementation,
        implementation_return_attribution,
    ):
        path.write_text("{}", encoding="utf-8")
    legacy.mkdir()
    attribution.mkdir()
    (attribution / "CURRENT.json").write_text("{}", encoding="utf-8")
    (legacy / "app.js").write_text("// legacy shell", encoding="utf-8")
    base_factor.write_text("date,RF\n", encoding="utf-8")
    plan = tmp_path / "plan"
    correlation = tmp_path / "correlation"
    plan.mkdir()
    correlation.mkdir()
    (plan / "plan.json").write_text("{}", encoding="utf-8")
    returns = correlation / "returns.csv"
    returns.write_text("date,value\n", encoding="utf-8")
    content = returns.read_bytes()
    (correlation / "evidence-proof.json").write_text(
        json.dumps(
            {
                "as_of_date": "2026-08-03",
                "builder": "portfolio-target-series/layered-correlation-1",
                "evidence_mode_policy": {
                    "default": "LIVE_ONLY",
                    "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
                    "post_inception_backtest_fill": False,
                },
                "series_mappings": [
                    {
                        "backtest_observations": 1,
                        "history_status": "ACCEPTED_EXACT_VERSION_HISTORY",
                        "series_id": f"Strategy {index}",
                        "strategy_version": f"synthetic:{index}",
                    }
                    for index in range(9)
                ],
                "target_sha256": hashlib.sha256(content).hexdigest(),
                "target_size_bytes": len(content),
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "generated-recipe.json"

    result = builder.create_recipe(
        provider_ledger_spec=provider,
        provider_ledger_generation=None,
        baseline_spec=baseline,
        base_factor_file=base_factor,
        implementation_reconciliation=implementation,
        implementation_return_attribution=implementation_return_attribution,
        strategy_attribution_root=attribution,
        decomposition_plan_bundle=plan,
        correlation_source_root=correlation,
        legacy_panel=legacy,
        as_of_date="2026-08-03",
        pipeline_id="synthetic-vertical",
        release_commit="a" * 40,
        output=output,
    )

    assert result["recipe"] == str(output)
    assert builder._recipe(output)["schema_version"] == 6


def test_create_recipe_can_bind_verified_immutable_provider_ledger(
    tmp_path: Path,
) -> None:
    source_recipe = _recipe(tmp_path)
    payload = json.loads(source_recipe.read_text(encoding="utf-8"))
    generation = _provider_ledger_generation(tmp_path)
    output = tmp_path / "reused-ledger-recipe.json"

    result = builder.create_recipe(
        provider_ledger_spec=None,
        provider_ledger_generation=generation,
        baseline_spec=Path(payload["baseline_spec"]["path"]),
        base_factor_file=Path(payload["base_factor_file"]["path"]),
        implementation_reconciliation=Path(
            payload["implementation_reconciliation"]["path"]
        ),
        implementation_return_attribution=Path(
            payload["implementation_return_attribution"]["path"]
        ),
        strategy_attribution_root=Path(payload["strategy_attribution_root"]["path"]),
        decomposition_plan_bundle=Path(payload["decomposition_plan_bundle"]["path"]),
        correlation_source_root=Path(payload["correlation_source_root"]["path"]),
        legacy_panel=Path(payload["legacy_panel"]["path"]),
        as_of_date="2026-08-03",
        pipeline_id="synthetic-reused-ledger",
        release_commit="a" * 40,
        output=output,
    )

    assert result["recipe"] == str(output)
    accepted = builder._recipe(output)
    assert accepted["schema_version"] == 7
    assert accepted["provider_ledger_generation"]["path"] == str(generation)


def test_reusable_provider_ledger_fails_when_catalog_identity_differs(
    tmp_path: Path,
) -> None:
    generation = _provider_ledger_generation(tmp_path)
    (generation / "provider-ledger.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(builder.VerticalBuildError, match="identity differs"):
        builder._validated_reusable_provider_ledger(generation)


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


def test_successful_xlsx_evidence_is_resumable_and_identity_bound(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "excel").mkdir(parents=True)
    job = {
        "idempotency_key": "synthetic",
        "schema_version": "2.0",
        "steps": [{"type": "open"}, {"type": "save_as"}],
    }
    result = {
        "final_state": "SUCCEEDED",
        "idempotency_key": "synthetic",
        "ok": True,
        "schema_version": "2.0",
        "steps": [{"status": "succeeded"}, {"status": "succeeded"}],
    }
    (candidate / "xlsx-job.json").write_text(json.dumps(job), encoding="utf-8")
    (candidate / "xlsx-result.json").write_text(json.dumps(result), encoding="utf-8")
    (candidate / "excel" / "portfolio-report.xlsx").write_bytes(b"xlsx")

    assert builder._accepted_xlsx_evidence(candidate) is True

    result["idempotency_key"] = "different"
    (candidate / "xlsx-result.json").write_text(json.dumps(result), encoding="utf-8")
    assert builder._accepted_xlsx_evidence(candidate) is False


def test_workbook_validation_resume_requires_all_invariants(tmp_path: Path) -> None:
    path = tmp_path / "validation.json"
    path.write_text(
        json.dumps(
            {
                "all_passed": True,
                "invariants": [{"name": "sheet", "passed": True}],
            }
        ),
        encoding="utf-8",
    )

    assert builder._accepted_workbook_validation(path) is not None

    path.write_text(
        json.dumps(
            {
                "all_passed": True,
                "invariants": [{"name": "sheet", "passed": False}],
            }
        ),
        encoding="utf-8",
    )
    assert builder._accepted_workbook_validation(path) is None


def test_hybrid_decomposition_uses_shared_version_bound_history_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_spec = tmp_path / "base-spec.json"
    base_source = tmp_path / "base-source"
    correlation_source = tmp_path / "correlation-source"
    bundle_root = tmp_path / "bundles"
    output_root = tmp_path / "analytics"
    base_spec.write_text("{}", encoding="utf-8")
    base_source.mkdir()
    correlation_source.mkdir()
    (correlation_source / "returns.csv").write_text("returns", encoding="utf-8")
    (correlation_source / "spec.json").write_text("{}", encoding="utf-8")
    bundle = bundle_root / ("b" * 64)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_tool(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((args, kwargs))
        if args[0] == "portfolio_decomposition_evidence_modes.py":
            bundle.mkdir(parents=True)
            (bundle / "base-spec.json").write_text("{}", encoding="utf-8")
            return {"bundle_id": bundle.name, "destination": str(bundle)}
        if args[1] == "publish":
            return {"generation_id": "c" * 64}
        return {"candidate_dir": str(tmp_path / "candidate")}

    monkeypatch.setattr(builder, "_tool", fake_tool)

    result = builder._build_hybrid_decomposition(
        base_spec=base_spec,
        base_source=base_source,
        correlation_source=correlation_source,
        bundle_root=bundle_root,
        output_root=output_root,
    )

    assert result["bundle_id"] == "b" * 64
    assert result["generation_id"] == "c" * 64
    assert calls[0][0] == (
        "portfolio_decomposition_evidence_modes.py",
        "--base-spec",
        str(base_spec),
        "--base-source-root",
        str(base_source),
        "--layered-returns",
        str(correlation_source / "returns.csv"),
        "--correlation-spec",
        str(correlation_source / "spec.json"),
        "--output-root",
        str(bundle_root),
    )
    assert calls[1][0][0:2] == ("portfolio_decomposition.py", "stage")
    assert calls[2][0][0:2] == ("portfolio_decomposition.py", "publish")


def _legacy_plan_manifest(specs: list[dict[str, object]]) -> dict[str, object]:
    manifest: dict[str, object] = {
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": "0.16.0",
        "schema_version": 1,
        "specs": specs,
    }
    manifest["bundle_id"] = hashlib.sha256(
        builder.canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


def test_legacy_release_composer_rejects_current_surface_plan() -> None:
    with pytest.raises(
        builder.VerticalBuildError,
        match="requires #331 exact plan-bundle integration",
    ):
        builder._legacy_release_plan_specs(
            {
                "builder": "portfolio-current-decomposition-plan",
                "builder_version": "0.17.0",
                "schema_version": 2,
                "specs": [
                    {
                        "path": "specs/us-economic.json",
                        "source_key": "us-economic",
                        "spec_id": "us-economic:STATIC",
                        "surface": "STATIC",
                    },
                    {
                        "path": "specs/us-economic-dynamic-fls.json",
                        "source_key": "us-economic",
                        "spec_id": "us-economic:DYNAMIC_FLS",
                        "surface": "DYNAMIC_FLS",
                    },
                ],
            }
        )


def test_legacy_release_composer_rejects_duplicate_source_key() -> None:
    with pytest.raises(
        builder.VerticalBuildError,
        match="duplicates a source_key",
    ):
        builder._legacy_release_plan_specs(
            _legacy_plan_manifest(
                [
                    {"path": "specs/a.json", "source_key": "us-economic"},
                    {"path": "specs/b.json", "source_key": "us-economic"},
                ]
            )
        )


def test_legacy_release_composer_rejects_stripped_current_plan() -> None:
    with pytest.raises(
        builder.VerticalBuildError,
        match="requires #331 exact plan-bundle integration",
    ):
        builder._legacy_release_plan_specs(
            {
                "builder": "portfolio-current-decomposition-plan",
                "builder_version": "0.17.0",
                "bundle_id": "a" * 64,
                "schema_version": 2,
                "specs": [
                    {
                        "path": "specs/us-economic.json",
                        "source_key": "us-economic",
                    }
                ],
            }
        )


def test_legacy_release_composer_rejects_bundle_identity_rewrite() -> None:
    manifest = _legacy_plan_manifest(
        [{"path": "specs/us-economic.json", "source_key": "us-economic"}]
    )
    manifest["specs"] = [{"path": "specs/forged.json", "source_key": "us-economic"}]
    with pytest.raises(
        builder.VerticalBuildError,
        match="bundle identity differs",
    ):
        builder._legacy_release_plan_specs(manifest)


def test_panel_invocations_carry_baseline_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: every panel build/verify/publish invocation must carry
    --baseline-root (the panel's governed reconciliation authority)."""
    recipe = _recipe(tmp_path)
    calls: list[tuple[object, ...]] = []

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {"candidate_dir": str(tmp_path / "candidate")}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    analytics = {
        "decomposition_root": str(tmp_path / "decomposition"),
        "decomposition_lenses_root": str(tmp_path / "lenses"),
        "correlation_root": str(tmp_path / "correlation"),
        "weekly_root": str(tmp_path / "weekly"),
    }
    baseline_root = tmp_path / "stages" / "baseline"
    builder.build_panel_candidate(
        recipe,
        tmp_path / "stages" / "report",
        baseline_root,
        tmp_path / "bundle",
        analytics,
        tmp_path / "freshness.json",
        tmp_path / "stages" / "panel",
    )
    builder.publish_panel(
        recipe,
        tmp_path / "stages" / "report",
        baseline_root,
        tmp_path / "bundle",
        analytics,
        tmp_path / "freshness.json",
        tmp_path / "candidate",
        tmp_path / "qa",
        tmp_path / "stages" / "panel",
    )

    assert [call[1] for call in calls] == ["build", "verify", "publish"]
    for call in calls:
        assert call[0] == "portfolio_control_panel.py"
        assert "--baseline-root" in call
        assert call[call.index("--baseline-root") + 1] == str(baseline_root)

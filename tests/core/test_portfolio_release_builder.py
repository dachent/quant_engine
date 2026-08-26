from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
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
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_release_builder.py"
SPEC = importlib.util.spec_from_file_location("portfolio_release_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture(autouse=True)
def _stub_current_plan_upstream_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def synthetic_evidence(
        attribution_bundle: Path,
        _inputs: object,
        _plan: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        specification_path = next(
            path
            for path in sorted((attribution_bundle / "specs").glob("*.json"))
            if path.is_file()
        )
        first_spec = json.loads(specification_path.read_text(encoding="utf-8"))
        return {
            "layered_composition": {
                "Strategy 0": {"authority": "SYNTHETIC_LAYERED_HISTORY"}
            },
            "provenance_sources": first_spec["provenance_sources"],
        }

    monkeypatch.setattr(
        builder, "_verify_current_plan_upstream_evidence", synthetic_evidence
    )


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
            {"as_of_date": "2026-08-03", "catalog_schema_version": 2},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    receipt = canonical_json_bytes(
        {
            "admission_gate_status": "PASS",
            "product_readiness": "REQUIRED_NOT_READY",
            "receipt_type": "PROVIDER_ADMIT_OR_DECLARE_V1",
            "schema_version": 1,
        }
    )
    manifest = GenerationManifest.build(
        as_of_date=builder.date.fromisoformat("2026-08-03"),
        adapter=AdapterIdentity("quant-engine-provider-ledger", "2"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("positions",),
            observed_start=builder.date.fromisoformat("2026-08-03"),
            observed_end=builder.date.fromisoformat("2026-08-03"),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/admit-or-declare-receipt.json",
                sha256=hashlib.sha256(receipt).hexdigest(),
                size_bytes=len(receipt),
                as_of_date=builder.date.fromisoformat("2026-08-03"),
                schema_name="provider-admission-receipt",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=hashlib.sha256(catalog).hexdigest(),
                size_bytes=len(catalog),
                as_of_date=builder.date.fromisoformat("2026-08-03"),
                schema_name="provider-ledger-catalog",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = tmp_path / manifest.generation_id
    generation.mkdir()
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "admit-or-declare-receipt.json").write_bytes(receipt)
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


@pytest.mark.parametrize("kind", ("directory", "file"))
def test_directory_inventory_rejects_nested_links(tmp_path: Path, kind: str) -> None:
    """A candidate inventory may not hide content behind a nested link."""
    root = tmp_path / "candidate"
    nested = root / "nested"
    target = tmp_path / "target"
    root.mkdir()
    nested.mkdir()
    target.mkdir()
    (target / "evidence.txt").write_text("outside", encoding="utf-8")
    link = nested / "linked" if kind == "directory" else nested / "linked.txt"
    try:
        link.symlink_to(target if kind == "directory" else target / "evidence.txt", target_is_directory=kind == "directory")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(builder.VerticalBuildError, match="contains a link"):
        builder._directory_inventory(root)


def test_reparse_metadata_is_treated_as_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers Windows reparse-point detection where symlink privilege is absent."""
    reparse_point = 0x400
    monkeypatch.setattr(
        builder.stat, "FILE_ATTRIBUTE_REPARSE_POINT", reparse_point, raising=False
    )

    class ReparsePoint:
        def lstat(self) -> object:
            return type("Stat", (), {"st_file_attributes": reparse_point})()

        def is_symlink(self) -> bool:
            return False

    assert builder._is_link_or_reparse_point(ReparsePoint()) is True


def _panel_checkpoint_stage_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, list[Path]]:
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_bytes(b"{}\n")
    output_root = tmp_path / "output"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate_file = candidate / "panel.html"
    candidate_file.write_text("candidate-v1", encoding="utf-8")
    implementation = tmp_path / "implementation.json"
    return_attribution = tmp_path / "return-attribution.json"
    strategy_root = tmp_path / "strategy-root"
    implementation.write_text("{}", encoding="utf-8")
    return_attribution.write_text("{}", encoding="utf-8")
    strategy_root.mkdir()

    stages = output_root / "stage-results"
    stages.mkdir(parents=True)
    values = {
        "ledger": {"freshness_health": str(tmp_path / "freshness.json")},
        "baseline": {},
        "attribution": {"destination": str(tmp_path / "attribution")},
        "analytics": {"correlation_root": str(tmp_path / "correlation")},
        "report": {},
        "workbook": {},
    }
    for role, value in values.items():
        (stages / f"{role}.json").write_bytes(canonical_json_bytes(value))

    recipe = {
        "implementation_reconciliation": {"path": str(implementation)},
        "implementation_return_attribution": {"path": str(return_attribution)},
        "release_commit": "a" * 40,
        "strategy_attribution_root": {"path": str(strategy_root)},
    }
    monkeypatch.setattr(builder, "_recipe", lambda _path: recipe)
    monkeypatch.setattr(
        builder, "_history_contract_path", lambda _recipe: tmp_path / "history.json"
    )
    monkeypatch.setattr(
        builder, "_validate_stage_financial_arguments", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        builder,
        "build_panel_candidate",
        lambda *_args, **_kwargs: {"candidate_dir": str(candidate)},
    )
    published: list[Path] = []

    def fake_publish(*args: object, **_kwargs: object) -> dict[str, object]:
        published.append(Path(str(args[6])).resolve())
        return {"candidate_dir": str(args[6]), "published": True}

    monkeypatch.setattr(builder, "publish_panel", fake_publish)
    return recipe_path, output_root, candidate_file, published


def test_panel_candidate_checkpoint_rejects_foreign_or_stale_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse fails closed before panel publication for every bound identity."""
    recipe_path, output_root, candidate_file, published = _panel_checkpoint_stage_fixture(
        tmp_path, monkeypatch
    )
    candidate_result = builder.build_stage(recipe_path, output_root, "panel-candidate")
    checkpoint_path = output_root / "stage-results" / "panel-candidate.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    expected_keys = {
        "schema_version",
        "role",
        "recipe",
        "release_commit",
        "predecessors",
        "candidate",
        "result",
    }

    # This branch makes the initial RED case explicit: the historical raw
    # result shape allowed foreign checkpoints to reach publication.
    if set(checkpoint) != expected_keys:
        checkpoint_path.write_bytes(canonical_json_bytes(candidate_result))
        with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
            builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")
        assert published == []
        return

    recipe_bytes = recipe_path.read_bytes()
    predecessor_path = output_root / "stage-results" / "workbook.json"
    predecessor_bytes = predecessor_path.read_bytes()
    candidate_bytes = candidate_file.read_bytes()

    def reject(label: str, mutate: Callable[[dict[str, object]], None]) -> None:
        recipe_path.write_bytes(recipe_bytes)
        predecessor_path.write_bytes(predecessor_bytes)
        candidate_file.write_bytes(candidate_bytes)
        checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))
        mutated = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        mutate(mutated)
        checkpoint_path.write_bytes(canonical_json_bytes(mutated))
        with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
            builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")
        assert published == [], label

    reject(
        "release commit",
        lambda value: value.__setitem__("release_commit", "b" * 40),
    )
    reject(
        "recipe bytes",
        lambda _value: recipe_path.write_bytes(recipe_bytes + b"\n"),
    )
    reject(
        "predecessor bytes",
        lambda _value: predecessor_path.write_bytes(b"{ }\n"),
    )
    reject(
        "candidate content",
        lambda _value: candidate_file.write_text("candidate-v2", encoding="utf-8"),
    )
    reject(
        "candidate path",
        lambda value: value["candidate"].__setitem__("path", str(tmp_path / "other")),
    )
    reject(
        "candidate inventory",
        lambda value: value["candidate"].__setitem__("inventory_sha256", "0" * 64),
    )
    reject("missing key", lambda value: value.pop("recipe"))
    reject("extra key", lambda value: value.__setitem__("extra", True))
    reject("boolean schema", lambda value: value.__setitem__("schema_version", True))
    reject("float schema", lambda value: value.__setitem__("schema_version", 1.0))
    reject("wrong schema", lambda value: value.__setitem__("schema_version", 2))
    reject("wrong scalar role", lambda value: value.__setitem__("role", 1))
    reject("wrong role", lambda value: value.__setitem__("role", "panel"))
    reject("wrong type", lambda value: value.__setitem__("predecessors", []))
    reject(
        "boolean candidate file count",
        lambda value: value["candidate"].__setitem__("file_count", True),
    )
    reject(
        "float candidate file count",
        lambda value: value["candidate"].__setitem__("file_count", 1.0),
    )
    reject(
        "boolean recipe size",
        lambda value: value["recipe"].__setitem__("size_bytes", True),
    )
    reject(
        "float predecessor size",
        lambda value: value["predecessors"]["ledger"].__setitem__(
            "size_bytes", 1.0
        ),
    )
    reject(
        "wrong scalar candidate path",
        lambda value: value["candidate"].__setitem__("path", True),
    )
    reject(
        "wrong scalar release commit",
        lambda value: value.__setitem__("release_commit", 1),
    )
    reject("recipe wrong container", lambda value: value.__setitem__("recipe", []))
    reject(
        "recipe nested keys",
        lambda value: value["recipe"].__setitem__("extra", True),
    )
    reject(
        "predecessor nested keys",
        lambda value: value["predecessors"]["ledger"].__setitem__("extra", True),
    )
    reject("nested candidate", lambda value: value["result"].__setitem__("candidate_dir", str(tmp_path / "other")))

    checkpoint_path.write_bytes(canonical_json_bytes(candidate_result))
    with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
        builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")
    assert published == []


def test_panel_candidate_checkpoint_reuses_unchanged_candidate_for_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated checkpoint passes its nested result to publication unchanged."""
    recipe_path, output_root, candidate_file, published = _panel_checkpoint_stage_fixture(
        tmp_path, monkeypatch
    )
    candidate_result = builder.build_stage(recipe_path, output_root, "panel-candidate")
    checkpoint_path = output_root / "stage-results" / "panel-candidate.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    assert checkpoint["role"] == "panel-candidate"
    assert checkpoint["result"] == candidate_result
    assert checkpoint["candidate"]["path"] == str(candidate_file.parent.resolve())
    published_result = builder.build_stage(
        recipe_path, output_root, "panel", qa_dir=tmp_path / "qa"
    )

    assert published_result["published"] is True
    assert published == [candidate_file.parent.resolve()]


@pytest.mark.parametrize("payload", (b'{"schema_version":1,"schema_version":1}', b'{"candidate":{"path":"a","path":"b"}}'))
def test_panel_candidate_checkpoint_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    """Duplicate keys are rejected before a checkpoint can be interpreted."""
    recipe_path, output_root, _candidate_file, published = _panel_checkpoint_stage_fixture(
        tmp_path, monkeypatch
    )
    builder.build_stage(recipe_path, output_root, "panel-candidate")
    checkpoint_path = output_root / "stage-results" / "panel-candidate.json"
    checkpoint_path.write_bytes(payload)

    with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
        builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")

    assert published == []


def test_panel_candidate_checkpoint_rejects_noncanonical_json_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A semantically equal but noncanonical checkpoint is not reusable."""
    recipe_path, output_root, _candidate_file, published = _panel_checkpoint_stage_fixture(
        tmp_path, monkeypatch
    )
    builder.build_stage(recipe_path, output_root, "panel-candidate")
    checkpoint_path = output_root / "stage-results" / "panel-candidate.json"
    checkpoint_path.write_bytes(checkpoint_path.read_bytes() + b"\n")

    with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
        builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")

    assert published == []


@pytest.mark.parametrize("mutation", ("recipe", "candidate"))
def test_panel_revalidates_checkpoint_immediately_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Post-load recipe or candidate drift blocks publication at the last gate."""
    recipe_path, output_root, candidate_file, published = _panel_checkpoint_stage_fixture(
        tmp_path, monkeypatch
    )
    builder.build_stage(recipe_path, output_root, "panel-candidate")
    original_load = builder._load_panel_candidate_checkpoint
    calls = 0

    def mutate_after_initial_load(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        result = original_load(*args, **kwargs)
        if calls == 1:
            if mutation == "recipe":
                recipe_path.write_bytes(recipe_path.read_bytes() + b"\n")
            else:
                candidate_file.write_text("candidate-v2", encoding="utf-8")
        return result

    monkeypatch.setattr(
        builder, "_load_panel_candidate_checkpoint", mutate_after_initial_load
    )

    with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
        builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")

    assert calls == 2
    assert published == []


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_recipe = _recipe(tmp_path)
    payload = json.loads(source_recipe.read_text(encoding="utf-8"))
    generation = _provider_ledger_generation(tmp_path)
    output = tmp_path / "reused-ledger-recipe.json"
    monkeypatch.setattr(
        builder,
        "_history_contract_path",
        lambda recipe: (
            Path(recipe["correlation_source_root"]["path"]) / "dependence-source.json"
        ),
    )

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


def test_create_recipe_schema9_binds_exact_financial_epoch_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema 9 admits precisely the five selected financial inputs.

    The policy and template authorities are repository-owned implementation
    constants, so a recipe must not be able to substitute either one.
    """
    source_recipe = _recipe(tmp_path)
    payload = json.loads(source_recipe.read_text(encoding="utf-8"))
    generation = _provider_ledger_generation(tmp_path)
    financial_inputs = {
        "account_strategy_ledger": tmp_path / "account-ledger.json",
        "account_strategy_ledger_identity": tmp_path / "account-ledger-id.json",
        "account_strategy_classified_flows": tmp_path / "classified-flows.json",
        "account_strategy_flow_coverage": tmp_path / "flow-coverage.json",
        "account_strategy_admission_policy": tmp_path / "admission-policy.json",
    }
    for path in financial_inputs.values():
        path.write_text("{}", encoding="utf-8")
    output = tmp_path / "financial-recipe.json"
    monkeypatch.setattr(
        builder,
        "_history_contract_path",
        lambda recipe: (
            Path(recipe["correlation_source_root"]["path"]) / "dependence-source.json"
        ),
    )

    builder.create_recipe(
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
        pipeline_id="synthetic-financial-vertical",
        release_commit="a" * 40,
        output=output,
        **financial_inputs,
    )

    accepted = builder._recipe(output)

    assert accepted["schema_version"] == 9
    assert set(accepted) - set(payload) == {
        *financial_inputs,
        "provider_ledger_generation",
    }
    for key, path in financial_inputs.items():
        assert accepted[key] == _file_definition(path)


def test_current_schema_rejects_legacy_dependence_source(tmp_path: Path) -> None:
    recipe_path = _recipe(tmp_path)
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    generation = _provider_ledger_generation(tmp_path)
    recipe.pop("provider_ledger_spec")
    recipe["provider_ledger_generation"] = {
        "path": str(generation),
        **builder._directory_inventory(generation),
    }
    recipe["schema_version"] = 7
    recipe_path.write_bytes(canonical_json_bytes(recipe))

    with pytest.raises(
        builder.VerticalBuildError,
        match="current vertical build requires the verified dependence source",
    ):
        builder._recipe(recipe_path)


def test_reusable_provider_ledger_fails_when_catalog_identity_differs(
    tmp_path: Path,
) -> None:
    generation = _provider_ledger_generation(tmp_path)
    (generation / "provider-ledger.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(builder.VerticalBuildError, match="identity differs"):
        builder._validated_reusable_provider_ledger(generation)


def test_reusable_provider_ledger_requires_hash_bound_admission_receipt(
    tmp_path: Path,
) -> None:
    generation = _provider_ledger_generation(tmp_path)
    (generation / "admit-or-declare-receipt.json").write_text(
        '{"admission_gate_status":"PASS"}\n', encoding="utf-8"
    )

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
    candidate = tmp_path / "candidate"
    (candidate / "excel").mkdir(parents=True)
    path = candidate / "workbook-validation.json"
    path.write_text(
        json.dumps(
            {
                "all_passed": True,
                "invariants": [{"name": "sheet", "passed": True}],
            }
        ),
        encoding="utf-8",
    )
    evidence_paths = {
        "workbook": candidate / "excel" / "portfolio-report.xlsx",
        "xlsx_result": candidate / "xlsx-result.json",
        "validation_result": path,
        "workbook_contract": candidate / "workbook-contract.json",
        "xlsx_job": candidate / "xlsx-job.json",
        "workbook_canonicalization": candidate / "workbook-canonicalization.json",
        "xlsx_result_canonicalization": candidate / "xlsx-result-canonicalization.json",
    }
    for name, evidence_path in evidence_paths.items():
        if name != "validation_result":
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_bytes(f"synthetic {name}".encode())
    binding: dict[str, object] = {
        "binding_schema_version": 1,
        "validation_all_passed": True,
    }
    for name, evidence_path in evidence_paths.items():
        digest, size = builder._sha(evidence_path)
        binding[f"{name}_sha256"] = digest
        binding[f"{name}_size_bytes"] = size
    (candidate / "workbook-evidence-binding.json").write_text(
        json.dumps(binding), encoding="utf-8"
    )

    assert builder._accepted_workbook_validation(path, candidate) is not None

    evidence_paths["workbook"].write_bytes(b"drifted workbook")
    assert builder._accepted_workbook_validation(path, candidate) is None
    evidence_paths["workbook"].write_bytes(b"synthetic workbook")

    path.write_text(
        json.dumps(
            {
                "all_passed": True,
                "invariants": [{"name": "sheet", "passed": False}],
            }
        ),
        encoding="utf-8",
    )
    assert builder._accepted_workbook_validation(path, candidate) is None


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


def _current_plan_bundle(
    tmp_path: Path,
    *,
    source_keys: tuple[str, ...] = ("us-economic",),
    surfaces: tuple[str, ...] = ("STATIC", "DYNAMIC_FLS"),
    plan_mutator: Callable[[dict[str, object]], None] | None = None,
) -> Path:
    staging = tmp_path / "current-plan-staging"
    staging.mkdir()
    governed_plan = {
        "schema_version": 3,
        "split_policy": {
            "cv_folds": 3,
            "holdout_fraction": "0.20",
            "minimum_holdout_observations": 1,
            "minimum_training_observations_per_factor": 1,
            "parsimony_tolerance": "0.01",
            "ridge_candidates": ["0.0001"],
            "dynamic": {
                "constraint": "UNCONSTRAINED",
                "smoothness_candidates": ["0.00001"],
                "state_ridge": "0.0000001",
            },
        },
        "sources": {
            source_key: {
                "column_policy": "LOWER_CANDIDATE_ID",
                "freshness_policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1",
                "input_key": "us_factors",
                "lens": "ECONOMIC_FACTOR",
                "return_type": "EXCESS_RETURN",
                "risk_free_column": "rf",
                "universe_id": f"{source_key}-universe",
            }
            for source_key in source_keys
        },
        "strategies": [
            {
                "assignments": [
                    {
                        "constraint": "UNCONSTRAINED",
                        "factor_sets": [["MKT_RF"]],
                        "source_key": source_key,
                    }
                    for source_key in source_keys
                ],
                "series_id": "Strategy 0",
                "source_dispositions": {
                    source_key: {
                        "authority": "SYNTHETIC_GOVERNED_PLAN",
                        "readiness": "READY_TO_MODEL",
                        "reason": "APPLICABLE_SYNTHETIC_GOVERNED_ASSIGNMENT",
                        "requirement": "REQUIRED",
                    }
                    for source_key in source_keys
                },
                "strategy_id": "strategy-0",
            }
        ],
    }
    if plan_mutator is not None:
        plan_mutator(governed_plan)
    governed_plan_content = builder.canonical_json_bytes(governed_plan)
    governed_plan_relative = (
        "inputs/governance/current-strategy-decomposition-plan.json"
    )
    governed_plan_path = staging / governed_plan_relative
    governed_plan_path.parent.mkdir(parents=True)
    governed_plan_path.write_bytes(governed_plan_content)
    governed_plan_sha256 = hashlib.sha256(governed_plan_content).hexdigest()
    financial_contract = {
        "product": {
            "as_of_policy": {
                "execution_profiles": {
                    "DEVELOPMENT": "WARN_STALE_INPUTS",
                    "RELEASE_QC": "FAIL_STALE_REQUIRED_INPUTS",
                    "WORKSTATION_QC": "WARN_STALE_INPUTS",
                }
            }
        },
        "strategies": [
            {
                "history_series_id": "Strategy 0",
                "outcome_dispositions": {"DECOMPOSITION": "REQUIRED"},
                "product_requirement": "REQUIRED",
                "strategy_id": "strategy-0",
            }
        ],
        "source_segments": [
            {
                "admission": {
                    "facts": ["DECOMPOSITION_APPLICABILITY"],
                    "transform": "portfolio-current-product-strategy-census",
                    "version": "3",
                },
                "capture_status": "CAPTURED_GOVERNED",
                "consumers": ["DECOMPOSITION"],
                "current_generation": {
                    "generation_id": governed_plan_sha256,
                    "sha256": governed_plan_sha256,
                },
                "data_classes": ["DECOMPOSITION_APPLICABILITY"],
                "endpoint_or_artifact_selector": (
                    "docs/analytics/current-strategy-decomposition-plan.json"
                ),
                "existence_status": "EXISTS",
                "product_requirement": "REQUIRED",
                "schema": {
                    "name": "current-strategy-decomposition-plan",
                    "version": "3",
                },
                "source_id": "current-strategy-registry",
                "source_version": "decomposition-plan-v3",
            }
        ],
    }
    financial_contract_content = builder.canonical_json_bytes(financial_contract)
    financial_contract_relative = "inputs/history/financial-mvp-contract.json"
    financial_contract_path = staging / financial_contract_relative
    financial_contract_path.parent.mkdir(parents=True)
    financial_contract_path.write_bytes(financial_contract_content)
    public_factor_manifest = {
        "acquisition_run": {"evaluation_date": "2026-08-03"},
        "builder": "portfolio-public-decomposition-sources",
        "builder_version": "0.3.0",
        "schema_version": 2,
    }
    public_factor_manifest_content = builder.canonical_json_bytes(
        public_factor_manifest
    )
    public_factor_manifest_relative = (
        "inputs/governance/public-factor-source-manifest.json"
    )
    public_factor_manifest_path = staging / public_factor_manifest_relative
    public_factor_manifest_path.write_bytes(public_factor_manifest_content)
    style_manifest_relative = "inputs/governance/style-source-manifest.json"
    style_manifest_content = builder.canonical_json_bytes({"synthetic": "style"})
    (staging / style_manifest_relative).write_bytes(style_manifest_content)
    sector_manifest_relative = "inputs/governance/sector-source-manifest.json"
    sector_manifest_content = builder.canonical_json_bytes({"synthetic": "sector"})
    (staging / sector_manifest_relative).write_bytes(sector_manifest_content)
    plan_provenance = {
        "artifact_id": "plan/current-strategy-decomposition-plan.json",
        "expected_sha256": governed_plan_sha256,
        "expected_size_bytes": len(governed_plan_content),
        "relative_path": governed_plan_relative,
        "schema_name": "current-strategy-decomposition-plan",
        "schema_version": "3",
    }
    source_catalog = {
        "universes": [
            {
                "candidates": [{"candidate_id": "MKT_RF"}],
                "universe_id": f"{source_key}-universe",
            }
            for source_key in source_keys
        ]
    }
    source_catalog_content = builder.canonical_json_bytes(source_catalog)
    source_catalog_relative = "inputs/governance/decomposition-source-catalog.json"
    source_catalog_path = staging / source_catalog_relative
    source_catalog_path.write_bytes(source_catalog_content)
    provenance_content = builder.canonical_json_bytes({"source": "synthetic"})
    provenance_relative = "inputs/base/provenance/source.json"
    provenance_path = staging / provenance_relative
    provenance_path.parent.mkdir(parents=True)
    provenance_path.write_bytes(provenance_content)
    base_spec = {
        "provenance_sources": [
            {
                "expected_sha256": hashlib.sha256(provenance_content).hexdigest(),
                "expected_size_bytes": len(provenance_content),
                "relative_path": "provenance/source.json",
            }
        ]
    }
    base_spec_relative = "inputs/governance/base-decomposition-spec.json"
    base_spec_content = builder.canonical_json_bytes(base_spec)
    base_spec_path = staging / base_spec_relative
    base_spec_path.parent.mkdir(parents=True, exist_ok=True)
    base_spec_path.write_bytes(base_spec_content)

    dated_returns = (
        ("2026-07-25", "0.001", "0.010"),
        ("2026-07-26", "0.001", "0.011"),
        ("2026-07-27", "0.001", "0.012"),
        ("2026-07-28", "0.001", "0.013"),
        ("2026-07-29", "0.001", "0.014"),
        ("2026-07-30", "0.001", "0.015"),
        ("2026-07-31", "0.001", "0.016"),
        ("2026-08-01", "0.001", "0.017"),
        ("2026-08-02", "0.001", "0.018"),
        ("2026-08-03", "0.001", "0.019"),
    )
    model_sources: dict[str, dict[str, object]] = {}
    for source_key in source_keys:
        factor_relative = f"inputs/lenses/{source_key}-factors.csv"
        factor_content = (
            "date,rf,mkt_rf\n"
            + "".join(
                f"{observed},{risk_free},{factor_return}\n"
                for observed, risk_free, factor_return in dated_returns
            )
        ).encode("utf-8")
        factor_path = staging / factor_relative
        factor_path.parent.mkdir(parents=True, exist_ok=True)
        factor_path.write_bytes(factor_content)
        target_relative = f"inputs/lenses/{source_key}-targets.csv"
        target_content = (
            "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
            + "".join(
                f"Strategy 0,{observed},{factor_return},LIVE,NET,STRATEGY,INCOMPLETE\n"
                for observed, _risk_free, factor_return in dated_returns
            )
        ).encode("utf-8")
        target_path = staging / target_relative
        target_path.write_bytes(target_content)
        model_sources[source_key] = {
            "factor_content": factor_content,
            "factor_relative": factor_relative,
            "target_content": target_content,
            "target_relative": target_relative,
        }
    first_model_source = model_sources[source_keys[0]]
    raw_factor_relative = "inputs/market/us_factors.csv"
    raw_factor_content = first_model_source["factor_content"]
    assert isinstance(raw_factor_content, bytes)
    raw_factor_path = staging / raw_factor_relative
    raw_factor_path.parent.mkdir(parents=True, exist_ok=True)
    raw_factor_path.write_bytes(raw_factor_content)
    supplemental_market_sources: dict[str, bytes] = {}
    for input_key in (
        "europe_factors",
        "style_returns",
        "sector_returns",
        "us_industries",
    ):
        relative = f"inputs/market/{input_key}.csv"
        (staging / relative).write_bytes(raw_factor_content)
        supplemental_market_sources[relative] = raw_factor_content
    raw_target_relative = "inputs/history/selected-targets.csv"
    raw_target_content = first_model_source["target_content"]
    assert isinstance(raw_target_content, bytes)
    raw_target_path = staging / raw_target_relative
    raw_target_path.write_bytes(raw_target_content)

    specs: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []
    for source_key in source_keys:
        for surface in surfaces:
            estimator = "STATIC_RBSA" if surface == "STATIC" else "DYNAMIC_FLS"
            surface_slug = surface.lower().replace("_", "-")
            suffix = "" if surface == "STATIC" else f"-{surface_slug}"
            relative = f"specs/{source_key}{suffix}.json"
            base_model_id = f"strategy-0-{source_key}-01"
            model_id = f"{base_model_id}-{surface_slug}"
            model = {
                "as_of_date": "2026-08-03",
                "constraint": "UNCONSTRAINED",
                "cost_coverage": "INCOMPLETE",
                "cv_folds": 3,
                "estimator": estimator,
                "factor_ids": ["MKT_RF"],
                "minimum_test_observations": 1,
                "minimum_train_observations": 3,
                "model_id": model_id,
                "parsimony_tolerance": "0.01",
                "periods_per_year": 252,
                "ridge_candidates": (
                    ["0.0001"] if surface == "STATIC" else ["0.0000001"]
                ),
                "series_id": "Strategy 0",
                "test_start": "2026-08-02",
                "train_end": "2026-08-01",
            }
            if surface == "DYNAMIC_FLS":
                model["smoothness_candidates"] = ["0.00001"]
            age_boundary = {
                "age_days": 0,
                "complete_through": "2026-08-03",
                "evaluated_on": "2026-08-03",
                "limiting_inputs": [
                    "factor:us_factors:MKT_RF:rf",
                    "target:Strategy 0",
                ],
                "status": "CURRENT_WITHIN_AGE_THRESHOLD",
                "warning_after_days": 0,
            }
            spec = {
                "analysis_lens": "ECONOMIC_FACTOR",
                "as_of_date": "2026-08-03",
                "evidence_mode": "LIVE_ONLY",
                "factor_source": {
                    "artifact_id": f"market/{source_key}.csv",
                    "date_column": "date",
                    "expected_sha256": hashlib.sha256(
                        model_sources[source_key]["factor_content"]
                    ).hexdigest(),
                    "expected_size_bytes": len(
                        model_sources[source_key]["factor_content"]
                    ),
                    "factors": [
                        {
                            "column": "mkt_rf",
                            "economic_family": "UNCLASSIFIED",
                            "effective_end": "2026-08-03",
                            "effective_start": "2026-07-25",
                            "factor_id": "MKT_RF",
                            "raw_orthogonal": False,
                            "redundancy_policy": (
                                "TRAIN_ONLY_NEAR_DUPLICATE_VIF_CONDITION_NUMBER_AND_"
                                "PARSIMONY_GATES"
                            ),
                            "return_source_id": "UNAVAILABLE_NOT_DECLARED",
                            "return_type": "EXCESS_RETURN",
                            "selection_rationale": (
                                "MKT_RF represents the declared UNCLASSIFIED family "
                                "for the ECONOMIC_FACTOR candidate universe using "
                                "UNAVAILABLE_NOT_DECLARED; raw orthogonality is not "
                                "assumed, and final inclusion is determined only by "
                                "training-period validation, collinearity diagnostics, "
                                "and the declared parsimony rule."
                            ),
                            "source_id": "UNAVAILABLE_NOT_DECLARED",
                        }
                    ],
                    "relative_path": model_sources[source_key]["factor_relative"],
                    "risk_free_column": "rf",
                    "schema_name": f"portfolio-{source_key}-returns",
                    "schema_version": "1",
                },
                "maximum_factor_staleness_days": 0,
                "models": [model],
                "provenance_sources": [plan_provenance],
                "schema_version": 1,
                "series_composition": {
                    "Strategy 0": {"authority": "SYNTHETIC_LAYERED_HISTORY"}
                },
                "target_source": {
                    "artifact_id": f"target/{source_key}.csv",
                    "expected_sha256": hashlib.sha256(
                        model_sources[source_key]["target_content"]
                    ).hexdigest(),
                    "expected_size_bytes": len(
                        model_sources[source_key]["target_content"]
                    ),
                    "relative_path": model_sources[source_key]["target_relative"],
                    "schema_name": "portfolio-decomposition-target-series",
                    "schema_version": "1",
                },
            }
            content = builder.canonical_json_bytes(spec)
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            artifact = {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
            outputs.append(artifact)
            specs.append(
                {
                    "analysis_lens": "ECONOMIC_FACTOR",
                    "as_of_date": "2026-08-03",
                    "data_boundary": {
                        **age_boundary,
                        "limiting_inputs": [f"model:{base_model_id}"],
                    },
                    "model_calculation_boundaries": {model_id: age_boundary},
                    "model_count": 1,
                    **artifact,
                    "source_key": source_key,
                    "spec_id": f"{source_key}:{surface}",
                    "surface": surface,
                }
            )
    governed_strategy = governed_plan["strategies"][0]
    governed_assignments = {
        assignment["source_key"] for assignment in governed_strategy["assignments"]
    }
    disposition_content = builder.canonical_json_bytes(
        {
            "candidate_dispositions": [
                {
                    "authority": governed_strategy["source_dispositions"][source_key][
                        "authority"
                    ],
                    "candidate_id": "MKT_RF",
                    "disposition": (
                        "PLANNED_MODEL_CANDIDATE"
                        if source_key in governed_assignments
                        else governed_strategy["source_dispositions"][source_key][
                            "readiness"
                        ]
                    ),
                    "lens": "ECONOMIC_FACTOR",
                    "readiness": governed_strategy["source_dispositions"][source_key][
                        "readiness"
                    ],
                    "reason": governed_strategy["source_dispositions"][source_key][
                        "reason"
                    ],
                    "requirement": governed_strategy["source_dispositions"][source_key][
                        "requirement"
                    ],
                    "source_key": source_key,
                    "strategy_id": "strategy-0",
                    "surface": surface,
                }
                for source_key in source_keys
                for surface in ("STATIC", "DYNAMIC_FLS")
            ],
            "policy": "EVERY_CURRENT_STRATEGY_BY_EVERY_PLANNED_CANDIDATE_UNIVERSE",
            "schema_version": 2,
        }
    )
    disposition_relative = "candidate-dispositions.json"
    (staging / disposition_relative).write_bytes(disposition_content)
    outputs.append(
        {
            "path": disposition_relative,
            "sha256": hashlib.sha256(disposition_content).hexdigest(),
            "size_bytes": len(disposition_content),
        }
    )
    inputs = []
    for relative, content in (
        (base_spec_relative, base_spec_content),
        (provenance_relative, provenance_content),
        (governed_plan_relative, governed_plan_content),
        (source_catalog_relative, source_catalog_content),
        (financial_contract_relative, financial_contract_content),
        (public_factor_manifest_relative, public_factor_manifest_content),
        (style_manifest_relative, style_manifest_content),
        (sector_manifest_relative, sector_manifest_content),
    ):
        inputs.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    for source_key in source_keys:
        for relative_key, content_key in (
            ("factor_relative", "factor_content"),
            ("target_relative", "target_content"),
        ):
            relative = str(model_sources[source_key][relative_key])
            content = model_sources[source_key][content_key]
            assert isinstance(content, bytes)
            inputs.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            )
    for relative, content in (
        (raw_factor_relative, raw_factor_content),
        (raw_target_relative, raw_target_content),
        *supplemental_market_sources.items(),
    ):
        inputs.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    manifest: dict[str, object] = {
        "assurance_profile": "DEVELOPMENT",
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": "0.17.0",
        "data_age": {
            "market_sources": [],
            "public_factor_capture": {
                "age_days": 0,
                "complete_through": "2026-08-03",
                "evaluated_on": "2026-08-03",
                "limiting_inputs": ["public-factor-acquisition-run"],
                "status": "CURRENT_WITHIN_AGE_THRESHOLD",
                "warning_after_days": 0,
            },
            "spec_calculation_boundaries": {
                str(row["spec_id"]): row["data_boundary"] for row in specs
            },
        },
        "evaluation_date": "2026-08-03",
        "inputs": inputs,
        "layered_history": {
            "assurance_profile": "DEVELOPMENT",
            "assurance_profile_policy": "WARN_STALE_INPUTS",
            "mode": "LIVE_ONLY",
        },
        "outputs": outputs,
        "policies": {
            "calendar": "EXACT_DATE_COMPLETE_CANDIDATE_UNIVERSE_NO_FILL",
            "data_age": (
                "WARN_IN_DEVELOPMENT_AND_WORKSTATION; "
                "FAIL_REQUIRED_STALE_SOURCE_IN_RELEASE_QC"
            ),
            "holdout": {
                "fraction": "0.20",
                "minimum_observations": 1,
                "selection": "TRAILING_SEALED_HOLDOUT",
            },
            "model_dimension": {
                "absolute_minimum_training_observations": 3,
                "dynamic_constraint": "UNCONSTRAINED",
                "dynamic_smoothness_candidates": ["0.00001"],
                "dynamic_state_ridge": "0.0000001",
                "minimum_training_observations_per_factor": 1,
                "required_surfaces": ["STATIC", "DYNAMIC_FLS"],
            },
            "publication": "PLAN_ONLY_NO_ACCEPTED_POINTER_CHANGE",
            "strategy_evidence_mode": "LIVE_ONLY",
        },
        "release_qc_eligibility": "NOT_ELIGIBLE_NON_RELEASE_PROFILE",
        "schema_version": 2,
        "specs": specs,
    }
    manifest["bundle_id"] = hashlib.sha256(
        builder.canonical_json_bytes(manifest)
    ).hexdigest()
    (staging / "plan-manifest.json").write_bytes(builder.canonical_json_bytes(manifest))
    destination = tmp_path / str(manifest["bundle_id"])
    staging.rename(destination)
    return destination


def _bind_recipe_plan_bundle(recipe_path: Path, bundle: Path) -> None:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe["decomposition_plan_bundle"] = {
        "path": str(bundle),
        **builder._directory_inventory(bundle),
    }
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")


def _current_manifest(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "plan-manifest.json").read_text(encoding="utf-8"))


def _rebind_current_bundle(
    bundle: Path, manifest: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    manifest.pop("bundle_id", None)
    manifest["bundle_id"] = hashlib.sha256(
        builder.canonical_json_bytes(manifest)
    ).hexdigest()
    (bundle / "plan-manifest.json").write_bytes(builder.canonical_json_bytes(manifest))
    destination = bundle.parent / str(manifest["bundle_id"])
    bundle.rename(destination)
    return destination, manifest


def _rewrite_current_spec(
    bundle: Path, *, surface: str, spec_updates: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    manifest = _current_manifest(bundle)
    row = next(item for item in manifest["specs"] if item["surface"] == surface)
    relative = str(row["path"])
    specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
    specification.update(spec_updates)
    content = builder.canonical_json_bytes(specification)
    (bundle / relative).write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()
    row.update({"sha256": sha256, "size_bytes": len(content)})
    output = next(item for item in manifest["outputs"] if item["path"] == relative)
    output.update({"sha256": sha256, "size_bytes": len(content)})
    return _rebind_current_bundle(bundle, manifest)


def test_current_analytics_rejects_recipe_plan_as_of_drift(tmp_path: Path) -> None:
    recipe_root = tmp_path / "recipe-fixture"
    recipe_root.mkdir()
    recipe = _recipe(recipe_root)
    recipe_value = json.loads(recipe.read_text(encoding="utf-8"))
    recipe_value["as_of_date"] = "2026-08-02"
    recipe.write_bytes(builder.canonical_json_bytes(recipe_value))
    bundle = _current_plan_bundle(tmp_path)
    _bind_recipe_plan_bundle(recipe, bundle)

    with pytest.raises(builder.VerticalBuildError, match="as-of date differs"):
        builder.build_analytics(recipe, bundle, tmp_path / "analytics")


@pytest.mark.parametrize("value", (None, "not-a-generation", "g" * 64))
def test_generation_id_requires_canonical_hash(value: object) -> None:
    with pytest.raises(builder.VerticalBuildError, match="generation"):
        builder._generation_id(value, "synthetic generation_id")


def test_current_surface_plan_builds_distinct_generations_without_hybrid_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_root = tmp_path / "recipe-fixture"
    recipe_root.mkdir()
    recipe = _recipe(recipe_root)
    bundle = _current_plan_bundle(tmp_path)
    _bind_recipe_plan_bundle(recipe, bundle)
    calls: list[tuple[object, ...]] = []

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        if args[0:2] == ("portfolio_weekly_returns.py", "build"):
            return {"candidate_dir": str(tmp_path / "weekly-candidate")}
        if args[1] == "publish":
            output_root = str(args[args.index("--output-root") + 1])
            return {"generation_id": hashlib.sha256(output_root.encode()).hexdigest()}
        return {}

    def reject_hybrid(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("current surface plan must not call the hybrid adapter")

    monkeypatch.setattr(builder, "_tool", fake_tool)
    monkeypatch.setattr(builder, "_build_hybrid_decomposition", reject_hybrid)

    output_root = tmp_path / "analytics"
    result = builder.build_analytics(recipe, bundle, output_root)

    assert set(result["lens_generations"]) == {
        "us-economic:DYNAMIC_FLS",
        "us-economic:STATIC",
    }
    assert result["schema_version"] == 2
    assert result["plan_bundle_id"] == bundle.name
    bindings = {row["spec_id"]: row for row in result["lens_generation_bindings"]}
    assert Path(bindings["us-economic:STATIC"]["root"]) == (
        output_root / "decomposition-lenses" / "LIVE_ONLY" / "STATIC" / "us-economic"
    )
    assert Path(bindings["us-economic:DYNAMIC_FLS"]["root"]) == (
        output_root
        / "decomposition-lenses"
        / "LIVE_ONLY"
        / "DYNAMIC_FLS"
        / "us-economic"
    )
    assert (
        bindings["us-economic:STATIC"]["generation_id"]
        != bindings["us-economic:DYNAMIC_FLS"]["generation_id"]
    )
    assert not any(
        call[0] == "portfolio_decomposition_evidence_modes.py" for call in calls
    )


def test_current_surface_plan_rejects_missing_surface_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _current_plan_bundle(tmp_path, surfaces=("STATIC",))

    def unexpected_tool(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool execution preceded current-plan admission")

    monkeypatch.setattr(builder, "_tool", unexpected_tool)
    with pytest.raises(builder.VerticalBuildError, match="exact STATIC/DYNAMIC_FLS"):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_rejects_duplicate_surface_identity(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(
        tmp_path, surfaces=("STATIC", "STATIC", "DYNAMIC_FLS")
    )

    with pytest.raises(builder.VerticalBuildError, match="duplicated"):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_rejects_wrong_spec_id(tmp_path: Path) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["specs"][0]["spec_id"] = "us-economic:FORGED"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="spec_id differs"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    "unsafe_path",
    ("../escape.json", "C:/escape.json", "specs\\us-economic.json"),
)
def test_current_surface_plan_rejects_nonportable_spec_path(
    tmp_path: Path, unsafe_path: str
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["specs"][0]["path"] = unsafe_path
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="portable relative path"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_nonportable_manifest_input(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["inputs"][0]["path"] = "../escape.json"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="portable relative path"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    "required_path",
    (
        "inputs/governance/base-decomposition-spec.json",
        "inputs/governance/current-strategy-decomposition-plan.json",
        "candidate-dispositions.json",
    ),
)
def test_current_surface_plan_rejects_unlisted_required_artifact(
    tmp_path: Path, required_path: str
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    collection = "inputs" if required_path.startswith("inputs/") else "outputs"
    manifest[collection] = [
        row for row in manifest[collection] if row["path"] != required_path
    ]
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="artifact inventory differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_source_relabel_detached_from_governed_plan(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        row["source_key"] = "renamed"
        row["spec_id"] = f"renamed:{row['surface']}"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="governed plan"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize("substituted_source", ("FOO", "foo."))
def test_current_surface_plan_rejects_windows_source_root_collision(
    tmp_path: Path, substituted_source: str
) -> None:
    bundle = _current_plan_bundle(tmp_path, source_keys=("foo", "bar"))
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        if row["source_key"] == "bar":
            row["source_key"] = substituted_source
            row["spec_id"] = f"{substituted_source}:{row['surface']}"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="source_key"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_surface_model_population_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    dynamic = next(row for row in manifest["specs"] if row["surface"] == "DYNAMIC_FLS")
    relative = str(dynamic["path"])
    specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
    specification["models"][0]["series_id"] = "Strategy 1"
    content = builder.canonical_json_bytes(specification)
    (bundle / relative).write_bytes(content)
    dynamic.update(sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))
    output = next(row for row in manifest["outputs"] if row["path"] == relative)
    output.update(dynamic | {"path": relative})
    output.pop("analysis_lens", None)
    output.pop("as_of_date", None)
    output.pop("data_boundary", None)
    output.pop("model_calculation_boundaries", None)
    output.pop("model_count", None)
    output.pop("source_key", None)
    output.pop("spec_id", None)
    output.pop("surface", None)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="governed model identity"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_assignment_for_blocked_source(
    tmp_path: Path,
) -> None:
    def block_assigned_source(plan: dict[str, object]) -> None:
        strategies = plan["strategies"]
        assert isinstance(strategies, list)
        strategy = strategies[0]
        assert isinstance(strategy, dict)
        dispositions = strategy["source_dispositions"]
        assert isinstance(dispositions, dict)
        cell = dispositions["us-economic"]
        assert isinstance(cell, dict)
        cell.update(
            readiness="REQUIRED_NOT_READY",
            reason="REQUIRED_NOT_READY_SYNTHETIC_SOURCE",
        )

    bundle = _current_plan_bundle(
        tmp_path,
        source_keys=("us-economic", "us-style"),
        plan_mutator=block_assigned_source,
    )

    with pytest.raises(
        builder.VerticalBuildError,
        match="assignments differ from source disposition authority",
    ):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_rejects_missing_assignment_for_ready_source(
    tmp_path: Path,
) -> None:
    def omit_ready_source(plan: dict[str, object]) -> None:
        strategies = plan["strategies"]
        assert isinstance(strategies, list)
        strategy = strategies[0]
        assert isinstance(strategy, dict)
        assignments = strategy["assignments"]
        assert isinstance(assignments, list)
        strategy["assignments"] = [
            assignment
            for assignment in assignments
            if isinstance(assignment, dict)
            and assignment.get("source_key") != "us-economic"
        ]

    bundle = _current_plan_bundle(
        tmp_path,
        source_keys=("us-economic", "us-style"),
        plan_mutator=omit_ready_source,
    )

    with pytest.raises(
        builder.VerticalBuildError,
        match="assignments differ from source disposition authority",
    ):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_rejects_holdout_policy_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["policies"]["holdout"]["fraction"] = "0.10"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="execution policy differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_rehashed_holdout_split_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        relative = str(row["path"])
        specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
        specification["models"][0].update(
            train_end="2026-07-31",
            test_start="2026-08-01",
        )
        content = builder.canonical_json_bytes(specification)
        (bundle / relative).write_bytes(content)
        binding = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        row.update(binding)
        next(
            output for output in manifest["outputs"] if output["path"] == relative
        ).update(binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="holdout split differs"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    ("source_field", "relative"),
    (
        ("factor_source", "inputs/lenses/us-economic-factors.csv"),
        ("target_source", "inputs/lenses/us-economic-targets.csv"),
    ),
)
def test_current_surface_plan_rejects_rehashed_projected_value_substitution(
    tmp_path: Path,
    source_field: str,
    relative: str,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    path = bundle / relative
    content = path.read_bytes().replace(b"0.010", b"0.999", 1)
    path.write_bytes(content)
    binding = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    next(item for item in manifest["inputs"] if item["path"] == relative).update(
        binding
    )
    for row in manifest["specs"]:
        specification_path = bundle / str(row["path"])
        specification = json.loads(specification_path.read_text(encoding="utf-8"))
        specification[source_field].update(
            expected_sha256=binding["sha256"],
            expected_size_bytes=binding["size_bytes"],
        )
        specification_content = builder.canonical_json_bytes(specification)
        specification_path.write_bytes(specification_content)
        specification_binding = {
            "sha256": hashlib.sha256(specification_content).hexdigest(),
            "size_bytes": len(specification_content),
        }
        row.update(specification_binding)
        next(
            output for output in manifest["outputs"] if output["path"] == row["path"]
        ).update(specification_binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="projection differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_factor_staleness_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        relative = str(row["path"])
        specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
        specification["maximum_factor_staleness_days"] = 1
        content = builder.canonical_json_bytes(specification)
        (bundle / relative).write_bytes(content)
        binding = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        row.update(binding)
        next(
            output for output in manifest["outputs"] if output["path"] == relative
        ).update(binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="factor staleness differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_factor_return_type_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        relative = str(row["path"])
        specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
        specification["factor_source"]["factors"][0]["return_type"] = "TOTAL_RETURN"
        content = builder.canonical_json_bytes(specification)
        (bundle / relative).write_bytes(content)
        binding = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        row.update(binding)
        next(
            output for output in manifest["outputs"] if output["path"] == relative
        ).update(binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="factor definitions differ"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("ridge_candidates", ["NaN"]),
        ("dynamic.constraint", "BOGUS"),
        ("dynamic.state_ridge", "-1"),
        ("dynamic.smoothness_candidates", ["0"]),
    ),
)
def test_current_surface_plan_rejects_non_planner_numeric_policy(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    def substitute_policy(plan: dict[str, object]) -> None:
        split = plan["split_policy"]
        assert isinstance(split, dict)
        if field.startswith("dynamic."):
            dynamic = split["dynamic"]
            assert isinstance(dynamic, dict)
            dynamic[field.removeprefix("dynamic.")] = value
        else:
            split[field] = value

    bundle = _current_plan_bundle(tmp_path, plan_mutator=substitute_policy)

    with pytest.raises(
        builder.VerticalBuildError,
        match="governed current strategy model policy differs",
    ):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_admits_leading_incomplete_ordinary_factor_row(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    raw_relative = "inputs/market/us_factors.csv"
    raw_path = bundle / raw_relative
    raw_content = raw_path.read_bytes().replace(
        b"2026-07-25,0.001,0.010\n",
        b"2026-07-25,,0.010\n",
        1,
    )
    raw_path.write_bytes(raw_content)
    next(item for item in manifest["inputs"] if item["path"] == raw_relative).update(
        sha256=hashlib.sha256(raw_content).hexdigest(),
        size_bytes=len(raw_content),
    )

    projected_relative = "inputs/lenses/us-economic-factors.csv"
    projected_path = bundle / projected_relative
    projected_content = projected_path.read_bytes().replace(
        b"2026-07-25,0.001,0.010\n",
        b"",
        1,
    )
    projected_path.write_bytes(projected_content)
    projected_binding = {
        "sha256": hashlib.sha256(projected_content).hexdigest(),
        "size_bytes": len(projected_content),
    }
    next(
        item for item in manifest["inputs"] if item["path"] == projected_relative
    ).update(projected_binding)
    for row in manifest["specs"]:
        relative = str(row["path"])
        specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
        specification["factor_source"].update(
            expected_sha256=projected_binding["sha256"],
            expected_size_bytes=projected_binding["size_bytes"],
        )
        specification["factor_source"]["factors"][0]["effective_start"] = "2026-07-26"
        content = builder.canonical_json_bytes(specification)
        (bundle / relative).write_bytes(content)
        binding = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        row.update(binding)
        next(
            output for output in manifest["outputs"] if output["path"] == relative
        ).update(binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    assert len(builder._current_release_plan_specs(bundle, manifest)) == 2


def test_current_surface_plan_rejects_governed_model_identity_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        relative = str(row["path"])
        specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
        specification["models"][0]["series_id"] = "Forged Strategy"
        content = builder.canonical_json_bytes(specification)
        (bundle / relative).write_bytes(content)
        binding = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
        row.update(binding)
        next(
            output for output in manifest["outputs"] if output["path"] == relative
        ).update(binding)
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="governed model"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("constraint", "LONG_ONLY_SUM_TO_ONE"),
        ("ridge_candidates", ["9"]),
        ("smoothness_candidates", ["8"]),
    ),
)
def test_current_surface_plan_rejects_dynamic_policy_substitution(
    tmp_path: Path, field: str, value: object
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    row = next(item for item in manifest["specs"] if item["surface"] == "DYNAMIC_FLS")
    relative = str(row["path"])
    specification = json.loads((bundle / relative).read_text(encoding="utf-8"))
    specification["models"][0][field] = value
    content = builder.canonical_json_bytes(specification)
    (bundle / relative).write_bytes(content)
    binding = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    row.update(binding)
    next(output for output in manifest["outputs"] if output["path"] == relative).update(
        binding
    )
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="dynamic model policy"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_empty_age_boundaries(tmp_path: Path) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        row["data_boundary"] = {}
        row["model_calculation_boundaries"] = {
            model_id: {} for model_id in row["model_calculation_boundaries"]
        }
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="data age boundary"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_model_boundary_limiting_input_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        for boundary in row["model_calculation_boundaries"].values():
            boundary["limiting_inputs"] = ["target:Strategy 0"]
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(
        builder.VerticalBuildError,
        match="model data age boundary differs",
    ):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_manifest_data_age_boundary_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["data_age"]["spec_calculation_boundaries"] = {}
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="data-age manifest differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_public_capture_data_age_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["data_age"]["public_factor_capture"] = {}
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="data-age manifest differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_assurance_profile_relabel(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["assurance_profile"] = "RELEASE_QC"
    manifest["release_qc_eligibility"] = "REQUIRES_DOWNSTREAM_RELEASE_QC"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="assurance policy differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_age_warning_threshold_substitution(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    for row in manifest["specs"]:
        row["data_boundary"]["warning_after_days"] = 999
        for boundary in row["model_calculation_boundaries"].values():
            boundary["warning_after_days"] = 999
    manifest["data_age"]["spec_calculation_boundaries"] = {
        str(row["spec_id"]): row["data_boundary"] for row in manifest["specs"]
    }
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="data age boundary differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_release_eligibility_relabel(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["release_qc_eligibility"] = "REQUIRES_DOWNSTREAM_RELEASE_QC"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="release eligibility differs"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("lens", "BOGUS", "source lens differs"),
        ("input_key", "bogus", "input key differs"),
        ("freshness_policy_id", "BOGUS", "freshness policy differs"),
    ),
)
def test_current_surface_plan_rejects_non_planner_source_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    def substitute_source(plan: dict[str, object]) -> None:
        sources = plan["sources"]
        assert isinstance(sources, dict)
        source = sources["us-economic"]
        assert isinstance(source, dict)
        source[field] = value

    bundle = _current_plan_bundle(tmp_path, plan_mutator=substitute_source)

    with pytest.raises(builder.VerticalBuildError, match=message):
        builder._current_release_plan_specs(bundle, _current_manifest(bundle))


def test_current_surface_plan_rejects_candidate_disposition_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    relative = "candidate-dispositions.json"
    dispositions = json.loads((bundle / relative).read_text(encoding="utf-8"))
    dispositions["candidate_dispositions"][0]["disposition"] = "FORGED"
    content = builder.canonical_json_bytes(dispositions)
    (bundle / relative).write_bytes(content)
    next(row for row in manifest["outputs"] if row["path"] == relative).update(
        sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
    )
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="candidate dispositions"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize("drift", ("sha256", "size_bytes"))
def test_current_surface_plan_rejects_spec_artifact_drift(
    tmp_path: Path, drift: str
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    row = manifest["specs"][0]
    output = next(item for item in manifest["outputs"] if item["path"] == row["path"])
    value: object = "f" * 64 if drift == "sha256" else int(row[drift]) + 1
    row[drift] = value
    output[drift] = value
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="artifact differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_bundle_directory_drift(tmp_path: Path) -> None:
    bundle = _current_plan_bundle(tmp_path)
    drifted = tmp_path / ("f" * 64)
    bundle.rename(drifted)

    with pytest.raises(builder.VerticalBuildError, match="bundle directory differs"):
        builder._current_release_plan_specs(drifted, _current_manifest(drifted))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("analysis_lens", "RETURN_SECTOR"),
        ("as_of_date", "2026-08-02"),
        ("evidence_mode", "LIVE_PLUS_BACKTEST"),
        ("models", []),
        ("models", [{"estimator": "STATIC_RBSA"}]),
    ),
)
def test_current_surface_plan_rejects_spec_semantic_substitution(
    tmp_path: Path, field: str, value: object
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    bundle, manifest = _rewrite_current_spec(
        bundle,
        surface="DYNAMIC_FLS",
        spec_updates={field: value},
    )

    with pytest.raises(builder.VerticalBuildError, match="lens specification differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_model_count_substitution(tmp_path: Path) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["specs"][0]["model_count"] = 2
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="lens specification differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_evidence_mode_policy_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["policies"]["strategy_evidence_mode"] = "LIVE_PLUS_BACKTEST"
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(
        builder.VerticalBuildError, match="evidence-mode policy differs"
    ):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_required_surface_policy_drift(
    tmp_path: Path,
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest["policies"]["model_dimension"]["required_surfaces"] = [
        "STATIC",
        "STATIC",
    ]
    bundle, manifest = _rebind_current_bundle(bundle, manifest)

    with pytest.raises(builder.VerticalBuildError, match="required surfaces differ"):
        builder._current_release_plan_specs(bundle, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    (("builder_version", "0.18.0"), ("schema_version", 3)),
)
def test_current_surface_plan_rejects_unsupported_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    manifest[field] = value

    with pytest.raises(builder.VerticalBuildError, match="contract differs"):
        builder._current_release_plan_specs(bundle, manifest)


def test_current_surface_plan_rejects_recipe_bundle_substitution_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_root = tmp_path / "recipe-fixture"
    recipe_root.mkdir()
    recipe = _recipe(recipe_root)
    bundle = _current_plan_bundle(tmp_path)

    def unexpected_tool(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool execution preceded recipe-bundle admission")

    monkeypatch.setattr(builder, "_tool", unexpected_tool)
    with pytest.raises(builder.VerticalBuildError, match="bundle differs from recipe"):
        builder.build_analytics(recipe, bundle, tmp_path / "analytics")


def test_legacy_plan_rejects_recipe_bundle_substitution_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_root = tmp_path / "recipe-fixture"
    recipe_root.mkdir()
    recipe = _recipe(recipe_root)
    bundle = tmp_path / "legacy-substitute"
    bundle.mkdir()
    manifest = _legacy_plan_manifest(
        [{"path": "specs/us-economic.json", "source_key": "us-economic"}]
    )
    (bundle / "plan-manifest.json").write_bytes(builder.canonical_json_bytes(manifest))

    def unexpected_tool(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool execution preceded recipe-bundle admission")

    monkeypatch.setattr(builder, "_tool", unexpected_tool)
    with pytest.raises(builder.VerticalBuildError, match="bundle differs from recipe"):
        builder.build_analytics(recipe, bundle, tmp_path / "analytics")


def test_current_surface_plan_rejects_linked_spec(tmp_path: Path) -> None:
    bundle = _current_plan_bundle(tmp_path)
    manifest = _current_manifest(bundle)
    row = manifest["specs"][0]
    spec_path = bundle / str(row["path"])
    external = tmp_path / "external-spec.json"
    external.write_bytes(spec_path.read_bytes())
    spec_path.unlink()
    try:
        spec_path.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(builder.VerticalBuildError, match="resolves through a link"):
        builder._current_release_plan_specs(bundle, manifest)


def test_legacy_release_composer_rejects_current_surface_plan() -> None:
    with pytest.raises(
        builder.VerticalBuildError,
        match="cannot admit a surface-qualified plan",
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
        match="cannot admit a surface-qualified plan",
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
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "panel.html").write_text("candidate", encoding="utf-8")
    qa = tmp_path / "qa"
    qa.mkdir()
    (qa / "browser.json").write_text("{}", encoding="utf-8")
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
        candidate,
        qa,
        tmp_path / "stages" / "panel",
    )

    assert [call[1] for call in calls] == ["build", "verify", "verify", "qa", "publish"]
    for call in (call for call in calls if call[1] != "qa"):
        assert call[0] == "portfolio_control_panel.py"
        assert "--baseline-root" in call
        assert call[call.index("--baseline-root") + 1] == str(baseline_root)


def test_panel_publication_validates_browser_qa_before_authorizing_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder freezes semantic and browser QA before inventorying authority."""
    recipe = _recipe(tmp_path)
    candidate = tmp_path / "candidate"
    qa = tmp_path / "qa"
    candidate.mkdir()
    qa.mkdir()
    (candidate / "panel.html").write_text("candidate", encoding="utf-8")
    (qa / "browser.json").write_text("{}", encoding="utf-8")
    calls: list[tuple[object, ...]] = []

    receipt_bytes: bytes | None = None
    qa_bytes_at_receipt: dict[str, bytes] | None = None
    panel_result: dict[str, object] = {}

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal receipt_bytes, qa_bytes_at_receipt, panel_result
        calls.append(args)
        if args[1] == "qa":
            assert (qa / "semantic-validation.json").is_file()
        if args[1] == "publish":
            receipt_path = Path(str(args[args.index("--coordinated-release-receipt") + 1]))
            receipt_bytes = receipt_path.read_bytes()
            qa_bytes_at_receipt = {
                item.relative_to(qa).as_posix(): item.read_bytes()
                for item in qa.rglob("*")
                if item.is_file()
            }
            receipt = json.loads(receipt_bytes)
            panel_result = {
                "coordinated_release_receipt_sha256": hashlib.sha256(
                    receipt_bytes
                ).hexdigest(),
                "release_id": receipt["release_id"],
            }
            return panel_result
        return {"publication_state": "PUBLISHED"}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    analytics = {
        "decomposition_root": str(tmp_path / "decomposition"),
        "decomposition_lenses_root": str(tmp_path / "lenses"),
        "correlation_root": str(tmp_path / "correlation"),
        "weekly_root": str(tmp_path / "weekly"),
    }

    result = builder.publish_panel(
        recipe,
        tmp_path / "report",
        tmp_path / "baseline",
        tmp_path / "bundle",
        analytics,
        tmp_path / "freshness.json",
        candidate,
        qa,
        tmp_path / "output",
    )

    assert [call[1] for call in calls] == ["verify", "qa", "publish"]
    assert receipt_bytes is not None
    assert qa_bytes_at_receipt is not None
    assert qa_bytes_at_receipt == {
        item.relative_to(qa).as_posix(): item.read_bytes()
        for item in qa.rglob("*")
        if item.is_file()
    }
    assert receipt_bytes == canonical_json_bytes(json.loads(receipt_bytes))
    assert result["coordinated_release_receipt_sha256"] == panel_result[
        "coordinated_release_receipt_sha256"
    ]
    assert result["release_id"] == panel_result["release_id"]


def test_panel_publication_receipt_uses_exact_canonical_release_id_preimage(
    tmp_path: Path,
) -> None:
    recipe_path = _recipe(tmp_path)
    recipe = builder._recipe(recipe_path)
    candidate = tmp_path / "candidate"
    qa = tmp_path / "qa"
    candidate.mkdir()
    qa.mkdir()
    (candidate / "panel.html").write_text("candidate", encoding="utf-8")
    (qa / "browser.json").write_text("{}", encoding="utf-8")

    _path, receipt = builder._panel_publication_receipt(
        recipe_path, recipe, tmp_path / "output", candidate, qa
    )
    recipe_bytes = recipe_path.read_bytes()
    preimage = {
        "pipeline_id": recipe["pipeline_id"],
        "recipe": {
            "sha256": hashlib.sha256(recipe_bytes).hexdigest(),
            "size_bytes": len(recipe_bytes),
        },
        "release_commit": recipe["release_commit"],
    }
    assert receipt["release_id"] == hashlib.sha256(
        canonical_json_bytes(preimage)
    ).hexdigest()


def test_panel_publication_receipt_is_create_only_byte_idempotent(
    tmp_path: Path,
) -> None:
    recipe_path = _recipe(tmp_path)
    recipe = builder._recipe(recipe_path)
    output = tmp_path / "output"
    candidate = tmp_path / "candidate"
    qa = tmp_path / "qa"
    candidate.mkdir()
    qa.mkdir()
    candidate_file = candidate / "panel.html"
    candidate_file.write_text("candidate-v1", encoding="utf-8")
    (qa / "browser.json").write_text("{}", encoding="utf-8")

    receipt_path, first = builder._panel_publication_receipt(
        recipe_path, recipe, output, candidate, qa
    )
    original_bytes = receipt_path.read_bytes()
    second_path, second = builder._panel_publication_receipt(
        recipe_path, recipe, output, candidate, qa
    )
    assert second_path == receipt_path
    assert second == first
    assert receipt_path.read_bytes() == original_bytes

    candidate_file.write_text("candidate-v2", encoding="utf-8")
    with pytest.raises(builder.VerticalBuildError, match="create-only output differs"):
        builder._panel_publication_receipt(recipe_path, recipe, output, candidate, qa)


def test_schema9_stages_one_manifest_and_uses_it_for_every_panel_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Financial panel build/verify/publish share one exact staged manifest."""
    recipe = _recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    generation = _provider_ledger_generation(tmp_path)
    payload.pop("provider_ledger_spec")
    payload["provider_ledger_generation"] = {
        "path": str(generation),
        **builder._directory_inventory(generation),
    }
    financial_paths = {
        "account_strategy_ledger": tmp_path / "account-ledger.json",
        "account_strategy_ledger_identity": tmp_path / "account-ledger-id.json",
        "account_strategy_classified_flows": tmp_path / "classified-flows.json",
        "account_strategy_flow_coverage": tmp_path / "flow-coverage.json",
        "account_strategy_admission_policy": tmp_path / "admission-policy.json",
    }
    for key, path in financial_paths.items():
        path.write_text("{}", encoding="utf-8")
        payload[key] = _file_definition(path)
    payload["schema_version"] = 9
    recipe.write_bytes(canonical_json_bytes(payload))
    monkeypatch.setattr(
        builder,
        "_history_contract_path",
        lambda recipe: (
            Path(recipe["correlation_source_root"]["path"]) / "dependence-source.json"
        ),
    )
    calls: list[tuple[object, ...]] = []

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        if args[0] == "account_strategy_manifest.py":
            return {"schema_version": 6}
        return {"candidate_dir": str(tmp_path / "candidate")}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    monkeypatch.setattr(
        builder,
        "_report_baseline_generation_id",
        lambda _root: "b" * 64,
    )
    monkeypatch.setattr(
        builder,
        "_published_generation",
        lambda root: ("b" * 64, root / "generations" / ("b" * 64)),
    )
    analytics = {
        "decomposition_root": str(tmp_path / "decomposition"),
        "decomposition_lenses_root": str(tmp_path / "lenses"),
        "correlation_root": str(tmp_path / "correlation"),
        "weekly_root": str(tmp_path / "weekly"),
    }
    baseline_root = tmp_path / "stages" / "baseline"
    report_root = tmp_path / "stages" / "report"
    panel_root = tmp_path / "stages" / "panel"

    candidate = builder.build_panel_candidate(
        recipe,
        report_root,
        baseline_root,
        tmp_path / "bundle",
        analytics,
        tmp_path / "freshness.json",
        panel_root,
    )
    staged_manifest = Path(str(candidate["account_strategy_manifest"]))
    assert "account_strategy_manifest_sha256" in candidate
    assert "account_strategy_manifest_size_bytes" in candidate
    staged_bytes = staged_manifest.read_bytes()
    staged_manifest.write_bytes(staged_bytes + b" ")
    with pytest.raises(
        builder.VerticalBuildError,
        match="staged account-strategy manifest identity differs",
    ):
        builder.publish_panel(
            recipe,
            report_root,
            baseline_root,
            tmp_path / "bundle",
            analytics,
            tmp_path / "freshness.json",
            tmp_path / "candidate",
            tmp_path / "qa",
            panel_root,
            account_strategy_manifest=staged_manifest,
            expected_account_strategy_manifest_sha256=str(
                candidate["account_strategy_manifest_sha256"]
            ),
            expected_account_strategy_manifest_size_bytes=int(
                candidate["account_strategy_manifest_size_bytes"]
            ),
        )
    staged_manifest.write_bytes(staged_bytes)
    (tmp_path / "candidate").mkdir()
    (tmp_path / "candidate" / "panel.html").write_text(
        "candidate", encoding="utf-8"
    )
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "browser.json").write_text("{}", encoding="utf-8")
    builder.publish_panel(
        recipe,
        report_root,
        baseline_root,
        tmp_path / "bundle",
        analytics,
        tmp_path / "freshness.json",
        tmp_path / "candidate",
        tmp_path / "qa",
        panel_root,
        account_strategy_manifest=staged_manifest,
        expected_account_strategy_manifest_sha256=str(
            candidate["account_strategy_manifest_sha256"]
        ),
        expected_account_strategy_manifest_size_bytes=int(
            candidate["account_strategy_manifest_size_bytes"]
        ),
    )

    assert [call[0] for call in calls] == [
        "account_strategy_manifest.py",
        "portfolio_control_panel.py",
        "portfolio_control_panel.py",
        "portfolio_control_panel.py",
        "portfolio_control_panel.py",
        "portfolio_control_panel.py",
    ]
    assert [call[1] for call in calls[1:]] == [
        "build", "verify", "verify", "qa", "publish"
    ]
    for call in (call for call in calls[1:] if call[1] != "qa"):
        assert "--financial-mode" in call
        assert call[call.index("--account-manifest") + 1] == str(staged_manifest)
        assert call[call.index("--manifest-ledger") + 1] == str(
            financial_paths["account_strategy_ledger"]
        )
        assert call[call.index("--manifest-ledger-identity") + 1] == str(
            financial_paths["account_strategy_ledger_identity"]
        )
        assert call[call.index("--manifest-classified-flows") + 1] == str(
            financial_paths["account_strategy_classified_flows"]
        )
        assert call[call.index("--manifest-flow-coverage") + 1] == str(
            financial_paths["account_strategy_flow_coverage"]
        )
        assert call[call.index("--manifest-admission-policy") + 1] == str(
            financial_paths["account_strategy_admission_policy"]
        )
        assert call[call.index("--manifest-classification-policy") + 1] == str(
            builder.ACCOUNT_STRATEGY_CLASSIFICATION_POLICY
        )
        assert call[call.index("--template-root") + 1] == str(
            builder.ACCOUNT_STRATEGY_PANEL_TEMPLATE_ROOT
        )

    parsed = builder._recipe(recipe)
    builder._validate_stage_financial_arguments(
        parsed,
        manifest_ledger=financial_paths["account_strategy_ledger"],
        manifest_ledger_identity=financial_paths["account_strategy_ledger_identity"],
        manifest_classified_flows=financial_paths["account_strategy_classified_flows"],
        manifest_flow_coverage=financial_paths["account_strategy_flow_coverage"],
        manifest_admission_policy=financial_paths["account_strategy_admission_policy"],
    )
    with pytest.raises(builder.VerticalBuildError, match="requires all five"):
        builder._validate_stage_financial_arguments(
            parsed,
            manifest_ledger=financial_paths["account_strategy_ledger"],
            manifest_ledger_identity=None,
            manifest_classified_flows=financial_paths[
                "account_strategy_classified_flows"
            ],
            manifest_flow_coverage=financial_paths[
                "account_strategy_flow_coverage"
            ],
            manifest_admission_policy=financial_paths[
                "account_strategy_admission_policy"
            ],
        )

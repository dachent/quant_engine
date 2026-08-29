from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
from collections.abc import Callable
from pathlib import Path
import sys
import zipfile

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    PointerIntegrityError,
    SourceArtifact,
    VerificationStatus,
    inventory_generation,
    publish_generation,
    read_generation_bundle,
    stage_generation,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_release_builder.py"
SPEC = importlib.util.spec_from_file_location("portfolio_release_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

PREWIRE_MANIFEST_BYTES = b'{"fixture":"synthetic-task-6","version":8}\n'
PREWIRE_REPORT_BYTES = b'{"fixture":"synthetic-task-6-report"}\n'
PREWIRE_MANIFEST_SHA256 = (
    "810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834"
)
PREWIRE_REPORT_SHA256 = (
    "eee8288e0f9ac185d75cf3c7d36947cf1521bf0561e39d178649ba69b865829e"
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path contract")
def test_create_only_writer_supports_windows_extended_length_paths(
    tmp_path: Path,
) -> None:
    """A valid release root must remain writable beyond legacy MAX_PATH."""

    destination = tmp_path
    for index in range(5):
        destination /= f"bounded-release-component-{index:02d}-abcdefghijk"
    destination /= "artifact.json"
    assert len(str(destination.resolve())) > 260

    builder._write_exact(destination, b'{"authority":"bound"}\n')

    api_path = Path(f"\\\\?\\{destination.resolve()}")
    assert api_path.read_bytes() == b'{"authority":"bound"}\n'
    builder._write_exact(destination, b'{"authority":"bound"}\n')
    with pytest.raises(builder.VerticalBuildError, match="create-only output differs"):
        builder._write_exact(destination, b'{"authority":"forged"}\n')


def _stage_verified_epoch_generation(
    root: Path,
) -> tuple[GenerationManifest, object]:
    """Build the real detached-control envelope around hand-authored bytes."""

    as_of = builder.date.fromisoformat("2026-08-24")
    manifest_artifact = SourceArtifact(
        artifact_id="outputs/account-strategy-manifest.json",
        sha256=PREWIRE_MANIFEST_SHA256,
        size_bytes=43,
        as_of_date=as_of,
        schema_name="account-strategy-manifest",
        schema_version="8",
    )
    report_artifact = SourceArtifact(
        artifact_id="verification/independent-verification.json",
        sha256=PREWIRE_REPORT_SHA256,
        size_bytes=38,
        as_of_date=as_of,
        schema_name="independent-verification",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-task-6-prewire", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic Task-6 prewire fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(manifest_artifact, report_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        root,
        manifest,
        {
            "outputs/account-strategy-manifest.json": PREWIRE_MANIFEST_BYTES,
            "verification/independent-verification.json": PREWIRE_REPORT_BYTES,
        },
    )
    return manifest, read_generation_bundle(root, manifest.generation_id)


def _real_financial_contract_module() -> object:
    module_name = "_portfolio_financial_mvp_contract_for_release_builder_test"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "tools" / "portfolio_financial_mvp_contract.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


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
        adapter=AdapterIdentity("quant-engine-provider-ledger", "3"),
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


_LITERAL_EPOCH_MANIFEST_SOURCE = {
    "generation_id": "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8",
    "generation_manifest_sha256": (
        "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d"
    ),
    "generation_receipt_sha256": (
        "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f"
    ),
    "full_directory_inventory_sha256": (
        "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1"
    ),
    "artifact": {
        "path": "outputs/account-strategy-manifest.json",
        "sha256": PREWIRE_MANIFEST_SHA256,
        "size_bytes": 43,
        "schema": {"name": "account-strategy-manifest", "version": "8"},
    },
}


def _schema10_recipe(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a recipe whose financial authority is a real closed bundle."""

    recipe = _recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    provider_generation = _provider_ledger_generation(tmp_path)
    generation_root = tmp_path / "epoch-manifest-generations"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    payload.pop("provider_ledger_spec")
    payload.update(
        {
            "schema_version": 10,
            "provider_ledger_generation": {
                "path": str(provider_generation),
                **builder._directory_inventory(provider_generation),
            },
            "epoch_manifest_generation": {
                "runtime_root": str(generation_root),
                "generation_id": manifest.generation_id,
                "source": _LITERAL_EPOCH_MANIFEST_SOURCE,
            },
        }
    )
    recipe.write_bytes(canonical_json_bytes(payload))
    return recipe, generation_root, manifest.generation_id


@pytest.fixture
def _schema10_history_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep schema-10 tests focused on epoch authority, not legacy history migration."""

    monkeypatch.setattr(
        builder,
        "_history_contract_path",
        lambda _recipe: Path("synthetic-history-contract.json"),
    )


def test_schema10_recipe_binds_a_literal_verified_epoch_bundle(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """Replacing a closed generation source with raw-five data would admit drift."""

    recipe, generation_root, generation_id = _schema10_recipe(tmp_path)

    accepted = builder._recipe(recipe)

    assert accepted["schema_version"] == 10
    assert accepted["epoch_manifest_generation"] == {
        "runtime_root": str(generation_root.resolve()),
        "generation_id": generation_id,
        "source": _LITERAL_EPOCH_MANIFEST_SOURCE,
    }


def test_schema10_recipe_revalidation_is_deterministic(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """A second load must re-read the retained bundle rather than trust recipe bytes."""

    recipe, _generation_root, _generation_id = _schema10_recipe(tmp_path)

    assert builder._recipe(recipe) == builder._recipe(recipe)


def test_create_recipe_schema10_binds_the_operator_epoch_root_and_id(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """Omitting a validated bundle bind at creation would persist a bare operator claim."""

    seed = _recipe(tmp_path)
    source = json.loads(seed.read_text(encoding="utf-8"))
    generation_root = tmp_path / "operator-epoch-root"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    provider_generation = _provider_ledger_generation(tmp_path)
    output = tmp_path / "schema10-recipe.json"

    builder.create_recipe(
        provider_ledger_spec=None,
        provider_ledger_generation=provider_generation,
        baseline_spec=Path(source["baseline_spec"]["path"]),
        base_factor_file=Path(source["base_factor_file"]["path"]),
        implementation_reconciliation=Path(
            source["implementation_reconciliation"]["path"]
        ),
        implementation_return_attribution=Path(
            source["implementation_return_attribution"]["path"]
        ),
        strategy_attribution_root=Path(source["strategy_attribution_root"]["path"]),
        decomposition_plan_bundle=Path(source["decomposition_plan_bundle"]["path"]),
        correlation_source_root=Path(source["correlation_source_root"]["path"]),
        legacy_panel=Path(source["legacy_panel"]["path"]),
        as_of_date="2026-08-03",
        pipeline_id="synthetic-source-to-panel",
        release_commit="a" * 40,
        output=output,
        epoch_manifest_generation_root=generation_root,
        epoch_manifest_generation_id=manifest.generation_id,
    )

    created = builder._recipe(output)
    assert created["schema_version"] == 10
    assert created["epoch_manifest_generation"] == {
        "runtime_root": str(generation_root.resolve()),
        "generation_id": manifest.generation_id,
        "source": _LITERAL_EPOCH_MANIFEST_SOURCE,
    }


@pytest.mark.parametrize("mutation", ("payload", "receipt"))
def test_schema10_recipe_rejects_one_byte_epoch_bundle_mutation(
    tmp_path: Path, mutation: str, _schema10_history_contract: None
) -> None:
    """Trusting the stored source identity would overlook a mutated detached control."""

    recipe, generation_root, generation_id = _schema10_recipe(tmp_path)
    generation = generation_root / "generations" / generation_id
    target = (
        generation / "outputs" / "account-strategy-manifest.json"
        if mutation == "payload"
        else generation / "generation-receipt.json"
    )
    changed = bytearray(target.read_bytes())
    changed[-1] = ord(" ") if changed[-1] != ord(" ") else ord("\n")
    target.write_bytes(changed)

    with pytest.raises(builder.VerticalBuildError, match="epoch-manifest generation"):
        builder._recipe(recipe)


@pytest.mark.parametrize("mismatch", ("root", "generation_id"))
def test_schema10_recipe_rejects_epoch_bundle_root_or_id_mismatch(
    tmp_path: Path, mismatch: str, _schema10_history_contract: None
) -> None:
    """An operator must name the exact root-and-content-addressed generation pair."""

    recipe, generation_root, generation_id = _schema10_recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    epoch = payload["epoch_manifest_generation"]
    assert isinstance(epoch, dict)
    if mismatch == "root":
        epoch["runtime_root"] = str(generation_root / "wrong-root")
    else:
        epoch["generation_id"] = "f" * 64
    recipe.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(builder.VerticalBuildError, match="epoch-manifest generation"):
        builder._recipe(recipe)


def test_schema10_recipe_rejects_a_bare_generation_manifest(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """A recipe cannot substitute a bare generation manifest for detached controls."""

    recipe, generation_root, generation_id = _schema10_recipe(tmp_path)
    source_generation = generation_root / "generations" / generation_id
    bare_root = tmp_path / "bare-generation-root"
    bare_generation = bare_root / "generations" / generation_id
    bare_generation.mkdir(parents=True)
    (bare_generation / "generation.json").write_bytes(
        (source_generation / "generation.json").read_bytes()
    )
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    payload["epoch_manifest_generation"]["runtime_root"] = str(bare_root)
    recipe.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(builder.VerticalBuildError, match="bundle verification failed"):
        builder._recipe(recipe)


def test_schema10_recipe_rejects_an_unverified_epoch_bundle(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """A generation with otherwise valid bytes must still carry VERIFIED authority."""

    recipe, _generation_root, _generation_id = _schema10_recipe(tmp_path)
    unverified_root = tmp_path / "unverified-epoch-root"
    as_of = builder.date.fromisoformat("2026-08-24")
    manifest_bytes = PREWIRE_MANIFEST_BYTES
    report_bytes = PREWIRE_REPORT_BYTES
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-task-6-prewire", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic Task-6 prewire fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="outputs/account-strategy-manifest.json",
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size_bytes=len(manifest_bytes),
                as_of_date=as_of,
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=as_of,
                schema_name="independent-verification",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.PENDING,
    )
    stage_generation_bundle(
        unverified_root,
        manifest,
        {
            "outputs/account-strategy-manifest.json": manifest_bytes,
            "verification/independent-verification.json": report_bytes,
        },
    )
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    epoch = payload["epoch_manifest_generation"]
    epoch["runtime_root"] = str(unverified_root)
    epoch["generation_id"] = manifest.generation_id
    recipe.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(builder.VerticalBuildError, match="binding failed"):
        builder._recipe(recipe)


def test_schema10_recipe_rejects_raw_five_fields(
    tmp_path: Path, _schema10_history_contract: None
) -> None:
    """A schema-10 profile must not retain the raw-five financial ingestion route."""

    recipe, _generation_root, _generation_id = _schema10_recipe(tmp_path)
    payload = json.loads(recipe.read_text(encoding="utf-8"))
    raw_five = tmp_path / "account-strategy-ledger.json"
    raw_five.write_text("{}", encoding="utf-8")
    payload["account_strategy_ledger"] = _file_definition(raw_five)
    recipe.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(builder.VerticalBuildError, match="recipe keys differ"):
        builder._recipe(recipe)


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
        link.symlink_to(
            target if kind == "directory" else target / "evidence.txt",
            target_is_directory=kind == "directory",
        )
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
    recipe_path, output_root, candidate_file, published = (
        _panel_checkpoint_stage_fixture(tmp_path, monkeypatch)
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
        with pytest.raises(
            builder.VerticalBuildError, match="panel candidate checkpoint"
        ):
            builder.build_stage(
                recipe_path, output_root, "panel", qa_dir=tmp_path / "qa"
            )
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
        with pytest.raises(
            builder.VerticalBuildError, match="panel candidate checkpoint"
        ):
            builder.build_stage(
                recipe_path, output_root, "panel", qa_dir=tmp_path / "qa"
            )
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
        lambda value: value["predecessors"]["ledger"].__setitem__("size_bytes", 1.0),
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
    reject(
        "nested candidate",
        lambda value: value["result"].__setitem__(
            "candidate_dir", str(tmp_path / "other")
        ),
    )

    checkpoint_path.write_bytes(canonical_json_bytes(candidate_result))
    with pytest.raises(builder.VerticalBuildError, match="panel candidate checkpoint"):
        builder.build_stage(recipe_path, output_root, "panel", qa_dir=tmp_path / "qa")
    assert published == []


def test_panel_candidate_checkpoint_reuses_unchanged_candidate_for_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated checkpoint passes its nested result to publication unchanged."""
    recipe_path, output_root, candidate_file, published = (
        _panel_checkpoint_stage_fixture(tmp_path, monkeypatch)
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


@pytest.mark.parametrize(
    "payload",
    (
        b'{"schema_version":1,"schema_version":1}',
        b'{"candidate":{"path":"a","path":"b"}}',
    ),
)
def test_panel_candidate_checkpoint_rejects_duplicate_json_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    """Duplicate keys are rejected before a checkpoint can be interpreted."""
    recipe_path, output_root, _candidate_file, published = (
        _panel_checkpoint_stage_fixture(tmp_path, monkeypatch)
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
    recipe_path, output_root, _candidate_file, published = (
        _panel_checkpoint_stage_fixture(tmp_path, monkeypatch)
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
    recipe_path, output_root, candidate_file, published = (
        _panel_checkpoint_stage_fixture(tmp_path, monkeypatch)
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
            receipt_path = Path(
                str(args[args.index("--coordinated-release-receipt") + 1])
            )
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
    assert (
        result["coordinated_release_receipt_sha256"]
        == panel_result["coordinated_release_receipt_sha256"]
    )
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
    assert (
        receipt["release_id"]
        == hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()
    )


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


def test_verified_epoch_bundle_stage_reads_binds_once_and_writes_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-five rebuild, duplicate read, alias, or re-encoding breaks this seam."""

    generation_root = tmp_path / "accepted"
    manifest, expected_bundle = _stage_verified_epoch_generation(generation_root)
    real_reader = read_generation_bundle
    reader_calls: list[tuple[Path, str]] = []
    reader_results: list[object] = []

    def read_once(root: Path, generation_id: str) -> object:
        reader_calls.append((root, generation_id))
        result = real_reader(root, generation_id)
        reader_results.append(result)
        return result

    real_financial_module = _real_financial_contract_module()
    binder_calls: list[object] = []

    class ExactBinder:
        def epoch_manifest_source_from_bundle(self, bundle: object) -> object:
            binder_calls.append(bundle)
            return real_financial_module.epoch_manifest_source_from_bundle(bundle)

    producer_calls: list[tuple[object, ...]] = []

    def forbidden_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        producer_calls.append(args)
        raise AssertionError("raw-five epoch producer must be unreachable")

    def forbidden_raw_five(*_args: object, **_kwargs: object) -> object:
        producer_calls.append(("_stage_account_strategy_manifest",))
        raise AssertionError("raw-five epoch builder must be unreachable")

    monkeypatch.setattr(builder, "read_generation_bundle", read_once, raising=False)
    monkeypatch.setattr(builder, "_financial_mvp_module", ExactBinder, raising=False)
    monkeypatch.setattr(builder, "_tool", forbidden_tool)
    monkeypatch.setattr(
        builder,
        "_stage_account_strategy_manifest",
        forbidden_raw_five,
    )
    output_root = tmp_path / "release"

    staged_path, source, staged_identity = (
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            output_root,
        )
    )

    literal_source = {
        "generation_id": (
            "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8"
        ),
        "generation_manifest_sha256": (
            "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d"
        ),
        "generation_receipt_sha256": (
            "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f"
        ),
        "full_directory_inventory_sha256": (
            "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1"
        ),
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": PREWIRE_MANIFEST_SHA256,
            "size_bytes": 43,
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
    }
    expected_staged_path = output_root / "account-strategy-manifest.json"
    assert reader_calls == [(generation_root, manifest.generation_id)]
    assert len(binder_calls) == 1
    assert binder_calls[0] is reader_results[0]
    assert binder_calls[0] == expected_bundle
    assert type(binder_calls[0]) is type(expected_bundle)
    assert producer_calls == []
    assert staged_path == expected_staged_path
    assert staged_path.read_bytes() == PREWIRE_MANIFEST_BYTES
    assert source == literal_source
    assert staged_identity == {
        "path": str(expected_staged_path.resolve()),
        "sha256": PREWIRE_MANIFEST_SHA256,
        "size_bytes": 43,
    }


@pytest.mark.parametrize("include_raw_five", [False, True])
def test_verified_epoch_bundle_stage_rejects_missing_detached_controls(
    tmp_path: Path, include_raw_five: bool
) -> None:
    """A manifest, even beside historical raw-five inputs, is not a bundle."""

    manifest, _bundle = _stage_verified_epoch_generation(tmp_path / "valid")
    generation_root = tmp_path / "broken"
    generation = generation_root / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    if include_raw_five:
        for filename in (
            "account-strategy-ledger.json",
            "account-strategy-ledger-identity.json",
            "account-strategy-classified-flows.json",
            "account-strategy-flow-coverage.json",
            "account-strategy-admission-policy.json",
        ):
            (generation / filename).write_bytes(b"{}\n")
    output_root = tmp_path / "release"

    with pytest.raises(builder.VerticalBuildError, match="bundle"):
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            output_root,
        )

    assert not output_root.exists()


def test_verified_epoch_bundle_stage_rejects_bare_generation_manifest(
    tmp_path: Path,
) -> None:
    """Accepting a GenerationManifest where a generation ID belongs breaks authority."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    output_root = tmp_path / "release"

    with pytest.raises(builder.VerticalBuildError, match="bundle"):
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest,
            output_root,
        )

    assert not output_root.exists()


@pytest.mark.parametrize("mutation", ["payload", "receipt"])
def test_verified_epoch_bundle_stage_rejects_one_byte_mutation_before_output(
    tmp_path: Path, mutation: str
) -> None:
    """Trusting manifest metadata without immutable readback would stage drift."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    generation = generation_root / "generations" / manifest.generation_id
    if mutation == "payload":
        target = generation / "outputs" / "account-strategy-manifest.json"
        changed = bytearray(target.read_bytes())
        changed[-3] = ord("9")
    else:
        target = generation / "generation-receipt.json"
        changed = bytearray(target.read_bytes())
        changed[0] = ord("[")
    target.write_bytes(changed)
    output_root = tmp_path / "release"

    with pytest.raises(builder.VerticalBuildError, match="bundle"):
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            output_root,
        )

    assert not output_root.exists()


def test_verified_epoch_bundle_stage_wraps_binder_failure_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected bundle-to-contract binding must not emit panel bytes."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)

    class RejectingBinder:
        @staticmethod
        def epoch_manifest_source_from_bundle(_bundle: object) -> object:
            raise ValueError("synthetic binder rejection")

    monkeypatch.setattr(
        builder, "_financial_mvp_module", RejectingBinder, raising=False
    )
    output_root = tmp_path / "release"

    with pytest.raises(builder.VerticalBuildError, match="binding"):
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            output_root,
        )

    assert not output_root.exists()


def test_verified_epoch_bundle_stage_preserves_different_existing_output(
    tmp_path: Path,
) -> None:
    """Replacing create-only staging with overwrite would destroy prior evidence."""

    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    staged_path = tmp_path / "release" / "account-strategy-manifest.json"
    staged_path.parent.mkdir(parents=True)
    prior_bytes = b'{"different":true}\n'
    staged_path.write_bytes(prior_bytes)

    with pytest.raises(builder.VerticalBuildError, match="create-only"):
        builder._stage_verified_epoch_manifest_bundle(
            generation_root,
            manifest.generation_id,
            tmp_path / "release",
        )

    assert staged_path.read_bytes() == prior_bytes


def test_nonfinancial_manifest_stage_remains_a_noop(tmp_path: Path) -> None:
    """The verified-bundle seam must not alter the legacy nonfinancial route."""

    output_root = tmp_path / "release"

    assert (
        builder._stage_account_strategy_manifest(
            {"schema_version": 7},
            tmp_path / "report",
            tmp_path / "baseline",
            output_root,
        )
        is None
    )
    assert not output_root.exists()


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
            return {"schema_version": 7}
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
    (tmp_path / "candidate" / "panel.html").write_text("candidate", encoding="utf-8")
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
        "build",
        "verify",
        "verify",
        "qa",
        "publish",
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
            manifest_flow_coverage=financial_paths["account_strategy_flow_coverage"],
            manifest_admission_policy=financial_paths[
                "account_strategy_admission_policy"
            ],
        )


def test_schema10_panel_build_publish_routes_one_verified_epoch_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _schema10_history_contract: None,
) -> None:
    """Schema-10 panel gates must consume one staged, verified epoch bundle."""

    recipe, generation_root, generation_id = _schema10_recipe(tmp_path)
    panel_root = tmp_path / "stages" / "panel"
    candidate_dir = tmp_path / "candidate"
    qa_dir = tmp_path / "qa"
    calls: list[tuple[object, ...]] = []
    real_stage = builder._stage_verified_epoch_manifest_bundle
    stage_calls: list[tuple[object, ...]] = []

    def stage_once(*args: object) -> tuple[Path, dict[str, object], dict[str, object]]:
        stage_calls.append(args)
        return real_stage(*args)

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        command = str(args[1])
        if command == "build":
            candidate_dir.mkdir()
            (candidate_dir / "panel.html").write_text("candidate", encoding="utf-8")
            return {"candidate_dir": str(candidate_dir)}
        if command == "qa":
            qa_dir.mkdir(exist_ok=True)
            (qa_dir / "browser.json").write_text("{}", encoding="utf-8")
        return {"published": command == "publish"}

    monkeypatch.setattr(builder, "_stage_verified_epoch_manifest_bundle", stage_once)
    monkeypatch.setattr(builder, "_tool", fake_tool)
    analytics = {
        "decomposition_root": str(tmp_path / "decomposition"),
        "decomposition_lenses_root": str(tmp_path / "lenses"),
        "correlation_root": str(tmp_path / "correlation"),
        "weekly_root": str(tmp_path / "weekly"),
    }

    candidate = builder.build_panel_candidate(
        recipe,
        tmp_path / "report",
        tmp_path / "baseline",
        tmp_path / "attribution",
        analytics,
        tmp_path / "freshness.json",
        panel_root,
    )
    staged_manifest = Path(str(candidate["account_strategy_manifest"]))
    assert stage_calls == [(generation_root, generation_id, panel_root)]
    assert staged_manifest.read_bytes() == PREWIRE_MANIFEST_BYTES
    assert candidate["epoch_manifest_generation_root"] == str(generation_root.resolve())
    assert candidate["epoch_manifest_generation_id"] == generation_id
    assert candidate["epoch_manifest_source"] == _LITERAL_EPOCH_MANIFEST_SOURCE
    assert candidate["account_strategy_manifest_sha256"] == PREWIRE_MANIFEST_SHA256
    assert candidate["account_strategy_manifest_size_bytes"] == len(
        PREWIRE_MANIFEST_BYTES
    )

    published = builder.publish_panel(
        recipe,
        tmp_path / "report",
        tmp_path / "baseline",
        tmp_path / "attribution",
        analytics,
        tmp_path / "freshness.json",
        candidate_dir,
        qa_dir,
        panel_root,
        account_strategy_manifest=staged_manifest,
        expected_account_strategy_manifest_sha256=str(
            candidate["account_strategy_manifest_sha256"]
        ),
        expected_account_strategy_manifest_size_bytes=int(
            candidate["account_strategy_manifest_size_bytes"]
        ),
        expected_epoch_manifest_generation_root=str(
            candidate["epoch_manifest_generation_root"]
        ),
        expected_epoch_manifest_generation_id=str(
            candidate["epoch_manifest_generation_id"]
        ),
        expected_epoch_manifest_source=candidate["epoch_manifest_source"],
    )

    assert published["epoch_manifest_generation_root"] == str(generation_root.resolve())
    assert published["epoch_manifest_generation_id"] == generation_id
    assert published["epoch_manifest_source"] == _LITERAL_EPOCH_MANIFEST_SOURCE
    assert published["account_strategy_manifest_sha256"] == PREWIRE_MANIFEST_SHA256
    assert published["account_strategy_manifest_size_bytes"] == len(
        PREWIRE_MANIFEST_BYTES
    )
    assert stage_calls == [(generation_root, generation_id, panel_root)]
    panel_calls = [call for call in calls if call[0] == "portfolio_control_panel.py"]
    assert [call[1] for call in panel_calls] == [
        "build",
        "verify",
        "verify",
        "qa",
        "publish",
    ]
    for call in (call for call in panel_calls if call[1] != "qa"):
        assert call[call.index("--account-manifest-generation-root") + 1] == str(
            generation_root.resolve()
        )
        assert call[call.index("--account-manifest-generation-id") + 1] == generation_id
        assert "--financial-mode" not in call
        assert "--account-manifest" not in call
        assert not any(str(argument).startswith("--manifest-") for argument in call)


def test_schema10_workbook_candidate_binds_exact_epoch_bundle_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_root = tmp_path / "accepted"
    manifest, _bundle = _stage_verified_epoch_generation(generation_root)
    candidate = tmp_path / "report-candidate"
    candidate.mkdir()
    runtime = tmp_path / "xlsx-runtime.exe"
    cli = tmp_path / "xlsx-cli.py"
    runtime.write_bytes(b"runtime")
    cli.write_bytes(b"cli")
    monkeypatch.setattr(builder, "XLSX_RUNTIME", runtime)
    monkeypatch.setattr(builder, "XLSX_CLI", cli)
    monkeypatch.setattr(builder, "_accepted_xlsx_evidence", lambda _path: True)
    monkeypatch.setattr(
        builder,
        "_accepted_workbook_validation",
        lambda _path, _candidate: {"all_passed": True},
    )
    calls: list[tuple[object, ...]] = []

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        if args[1] == "publish":
            return {"generation_id": "f" * 64}
        return {"status": "PASS"}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    monkeypatch.setattr(
        builder,
        "_require_exact_report_delivery",
        lambda _root, generation_id, _delivery: {
            "delivery_classification": "NON_AUTHORITY_HUMAN_DELIVERY",
            "generation_id": generation_id,
            "workbook_status": "EXACT_NON_AUTHORITY",
        },
    )

    result = builder.build_workbook(
        tmp_path / "baseline",
        tmp_path / "ledger",
        tmp_path / "freshness.json",
        tmp_path / "history.json",
        tmp_path / "implementation.json",
        tmp_path / "return-attribution.json",
        tmp_path / "attribution",
        candidate,
        tmp_path / "report-output",
        tmp_path / "correlation",
        epoch_manifest_generation_root=generation_root,
        epoch_manifest_generation_id=manifest.generation_id,
    )

    manifest_path = candidate / "account-strategy-manifest.json"
    source_path = candidate / "epoch-manifest-source.json"
    assert manifest_path.read_bytes() == PREWIRE_MANIFEST_BYTES
    assert json.loads(source_path.read_text(encoding="utf-8")) == (
        _LITERAL_EPOCH_MANIFEST_SOURCE
    )
    for call in (
        call
        for call in calls
        if call[0] == "portfolio_report.py"
        and call[1] in {"bind-workbook-evidence", "verify", "publish"}
    ):
        assert call[call.index("--epoch-manifest") + 1] == str(manifest_path)
        assert call[call.index("--epoch-manifest-source") + 1] == str(source_path)
    assert result["epoch_manifest_generation_id"] == manifest.generation_id
    assert result["epoch_manifest_artifact"]["sha256"] == PREWIRE_MANIFEST_SHA256


def test_build_report_routes_schema10_epoch_binding_only_to_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper must not send workbook-only authority into candidate build."""

    candidate = tmp_path / "candidate"
    calls: dict[str, object] = {}

    def fake_candidate(*args: object, **kwargs: object) -> dict[str, object]:
        calls["candidate_args"] = args
        calls["candidate_kwargs"] = kwargs
        return {"candidate_dir": str(candidate)}

    def fake_workbook(*args: object, **kwargs: object) -> dict[str, object]:
        calls["workbook_args"] = args
        calls["workbook_kwargs"] = kwargs
        return {"generation_id": "a" * 64}

    monkeypatch.setattr(builder, "build_report_candidate", fake_candidate)
    monkeypatch.setattr(builder, "build_workbook", fake_workbook)
    generation_root = tmp_path / "epoch-generation"
    result = builder.build_report(
        tmp_path / "baseline",
        tmp_path / "ledger",
        tmp_path / "freshness.json",
        tmp_path / "history.json",
        tmp_path / "implementation.json",
        tmp_path / "return-attribution.json",
        tmp_path / "attribution",
        tmp_path / "report",
        tmp_path / "correlation",
        epoch_manifest_generation_root=generation_root,
        epoch_manifest_generation_id="b" * 64,
    )

    assert result == {"generation_id": "a" * 64}
    assert calls["candidate_kwargs"] == {}
    assert calls["workbook_kwargs"] == {
        "epoch_manifest_generation_root": generation_root,
        "epoch_manifest_generation_id": "b" * 64,
    }


def test_build_report_routes_real_cli_candidate_and_epoch_binding_to_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper consumes a real producer CLI candidate before workbook binding."""

    producer_spec = importlib.util.spec_from_file_location(
        "_report_build_cli_producer_for_release_builder",
        ROOT / "tools" / "portfolio_report.py",
    )
    assert producer_spec is not None and producer_spec.loader is not None
    producer = importlib.util.module_from_spec(producer_spec)
    sys.modules[producer_spec.name] = producer
    try:
        producer_spec.loader.exec_module(producer)
        fixture_source = (
            ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py"
        ).read_text(encoding="utf-8")
        start = fixture_source.index("def _amount(")
        end = fixture_source.index("\ndef _published_dependence(")
        namespace = {
            "AdapterIdentity": AdapterIdentity,
            "Any": object,
            "CoverageClaim": CoverageClaim,
            "CoverageStatus": CoverageStatus,
            "Decimal": __import__("decimal").Decimal,
            "GenerationManifest": GenerationManifest,
            "SourceArtifact": SourceArtifact,
            "VerificationStatus": VerificationStatus,
            "canonical_json_bytes": canonical_json_bytes,
            "date": __import__("datetime").date,
            "hashlib": hashlib,
            "publish_generation": __import__(
                "quant_engine.core", fromlist=["publish_generation"]
            ).publish_generation,
            "stage_generation": __import__(
                "quant_engine.core", fromlist=["stage_generation"]
            ).stage_generation,
        }
        exec(compile(fixture_source[start:end], "producer-fixture", "exec"), namespace)
        baseline_root = namespace["_published_baseline"](tmp_path / "baseline")
        candidate_output = io.StringIO()
        with contextlib.redirect_stdout(candidate_output):
            assert (
                producer.main(
                    [
                        "build",
                        "--baseline-root",
                        str(baseline_root),
                        "--output-root",
                        str(tmp_path / "candidate-output"),
                    ]
                )
                == 0
            )
        real_candidate = json.loads(candidate_output.getvalue())
    finally:
        sys.modules.pop(producer_spec.name, None)

    candidate_path = Path(real_candidate["candidate_dir"])
    calls: dict[str, object] = {}

    def real_candidate_boundary(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs == {}
        return real_candidate

    def workbook_boundary(*args: object, **kwargs: object) -> dict[str, object]:
        calls["candidate"] = args[7]
        calls["epoch"] = kwargs
        return {"generation_id": "c" * 64}

    monkeypatch.setattr(builder, "build_report_candidate", real_candidate_boundary)
    monkeypatch.setattr(builder, "build_workbook", workbook_boundary)
    epoch_root = tmp_path / "epoch-generation"
    result = builder.build_report(
        baseline_root,
        tmp_path / "ledger",
        tmp_path / "freshness.json",
        tmp_path / "history.json",
        tmp_path / "implementation.json",
        tmp_path / "return-attribution.json",
        tmp_path / "attribution",
        tmp_path / "report",
        epoch_manifest_generation_root=epoch_root,
        epoch_manifest_generation_id="d" * 64,
    )

    assert result == {"generation_id": "c" * 64}
    assert Path(str(calls["candidate"])).resolve() == candidate_path.resolve()
    assert (candidate_path / "portfolio-report-data.json").is_file()
    assert calls["epoch"] == {
        "epoch_manifest_generation_root": epoch_root,
        "epoch_manifest_generation_id": "d" * 64,
    }


def test_build_report_runs_real_candidate_workbook_and_delivery_with_epoch_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the external xlsx command is stubbed in the complete report wrapper seam."""

    fixture_source = (
        ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py"
    ).read_text(encoding="utf-8")
    start = fixture_source.index("def _amount(")
    end = fixture_source.index("\ndef _published_dependence(")
    namespace = {
        "AdapterIdentity": AdapterIdentity,
        "Any": object,
        "CoverageClaim": CoverageClaim,
        "CoverageStatus": CoverageStatus,
        "Decimal": __import__("decimal").Decimal,
        "GenerationManifest": GenerationManifest,
        "SourceArtifact": SourceArtifact,
        "VerificationStatus": VerificationStatus,
        "canonical_json_bytes": canonical_json_bytes,
        "date": __import__("datetime").date,
        "hashlib": hashlib,
        "publish_generation": publish_generation,
        "stage_generation": __import__(
            "quant_engine.core", fromlist=["stage_generation"]
        ).stage_generation,
    }
    exec(compile(fixture_source[start:end], "producer-fixture", "exec"), namespace)

    provider_root = tmp_path / "provider-ledger"
    provider_catalog = canonical_json_bytes({"reconciliation_checks": []})
    provider_artifact = SourceArtifact(
        artifact_id="normalized/provider-ledger.json",
        sha256=hashlib.sha256(provider_catalog).hexdigest(),
        size_bytes=len(provider_catalog),
        as_of_date=builder.date(2026, 1, 3),
        schema_name="provider-ledger-catalog",
        schema_version="1",
    )
    provider_manifest = GenerationManifest.build(
        as_of_date=builder.date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic-provider-ledger", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic report wrapper provider ledger",
            fact_types=("provider-ledger",),
            observed_start=builder.date(2026, 1, 3),
            observed_end=builder.date(2026, 1, 3),
        ),
        artifacts=(provider_artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    provider_generation_path = stage_generation(provider_root, provider_manifest)
    (provider_generation_path.parent / "provider-ledger.json").write_bytes(
        provider_catalog
    )
    publish_generation(provider_root, provider_manifest.generation_id)
    provider_generation = (
        provider_root / "generations" / provider_manifest.generation_id
    )
    provider_identity = {
        "as_of_date": "2026-01-03",
        "catalog_sha256": provider_artifact.sha256,
        "catalog_size_bytes": provider_artifact.size_bytes,
        "generation_id": provider_manifest.generation_id,
        "manifest_sha256": hashlib.sha256(
            (provider_generation / "generation.json").read_bytes()
        ).hexdigest(),
        "manifest_size_bytes": (provider_generation / "generation.json").stat().st_size,
    }
    catalog = namespace["_baseline_catalog"]()
    catalog["source_artifacts"].extend(
        [
            {
                "artifact_id": "providers/provider-ledger/generation.json",
                "as_of_date": "2026-01-03",
                "schema_name": "generation-manifest",
                "schema_version": "1",
                "sha256": provider_identity["manifest_sha256"],
                "source_key": "provider-ledger-generation",
            },
            {
                "artifact_id": "providers/provider-ledger/provider-ledger.json",
                "as_of_date": "2026-01-03",
                "schema_name": "provider-ledger-catalog",
                "schema_version": "1",
                "sha256": provider_artifact.sha256,
                "source_key": "provider-ledger-generation",
            },
        ]
    )
    baseline_root = namespace["_published_baseline"](tmp_path / "baseline", catalog)
    freshness = tmp_path / "freshness.json"
    freshness.write_bytes(
        canonical_json_bytes(
            {
                "assessment_schema_version": 3,
                "as_of_date": "2026-01-03",
                "assessments": [{"evaluated_on": "2026-01-03"}],
                "policies": [],
                "provider_ledger": provider_identity,
            }
        )
    )
    strategy_names = [row["name"] for row in catalog["strategies"]]
    history_root = tmp_path / "history-tree"
    history_root.mkdir()
    history = history_root / "history.json"
    history.write_bytes(
        canonical_json_bytes(
            {
                "as_of_date": "2026-01-03",
                "builder": "portfolio-target-series/layered-correlation-1",
                "evidence_mode_policy": {
                    "default": "LIVE_ONLY",
                    "hybrid": "BACKTEST_STRICTLY_BEFORE_FIRST_LIVE_THEN_LIVE",
                    "post_inception_backtest_fill": False,
                },
                "series_mappings": [
                    {
                        "backtest_observations": 0,
                        "history_status": "UNAVAILABLE_SYNTHETIC",
                        "implementation_reconciliation_status": "NOT_APPLICABLE",
                        "series_id": name,
                        "strategy_version": "UNRESOLVED",
                    }
                    for name in strategy_names
                ],
                "target_sha256": "a" * 64,
                "target_size_bytes": 1,
            }
        )
    )
    implementation_ids = ("grizzlybears-vtma", "p123-ai-factor", "tt-portfolio")
    implementation = tmp_path / "implementation.json"
    implementation.write_bytes(
        canonical_json_bytes(
            {
                "as_of_date": "2026-01-03",
                "builder": "portfolio-implementation-reconciliation",
                "reconciliation_id": "synthetic-implementation",
                "schema_version": 1,
                "strategies": [
                    {
                        "return_reconciliation": {
                            "observations": 2,
                            "observed_end": "2026-01-03",
                            "observed_start": "2026-01-01",
                        },
                        "root_causes": [{"status": "SYNTHETIC"}],
                        "strategy_id": strategy_id,
                        "verification": "VERIFIED",
                    }
                    for strategy_id in implementation_ids
                ],
                "verification": "VERIFIED",
            }
        )
    )
    return_attribution = tmp_path / "return-attribution.json"
    return_attribution.write_bytes(
        canonical_json_bytes(
            {
                "as_of_date": "2026-01-03",
                "attribution_id": "synthetic-return-attribution",
                "builder": "portfolio-implementation-return-attribution",
                "schema_version": 1,
                "strategies": [
                    {
                        "bridge": {
                            "account_return": 0.0,
                            "categories": [{"effect_pp": 0.0}],
                            "gap_pp": 0.0,
                            "model_return": 0.0,
                        },
                        "observed_end": "2026-01-03",
                        "observed_start": "2026-01-01",
                        "strategy_id": strategy_id,
                        "verification": "VERIFIED_WITH_DECLARED_PROXY",
                    }
                    for strategy_id in implementation_ids
                ],
                "verification": "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
            }
        )
    )
    attribution_root = tmp_path / "attribution"
    baseline_pointer = builder.read_current(baseline_root)
    attribution = canonical_json_bytes(
        {
            "baseline": {
                "catalog_sha256": hashlib.sha256(
                    (
                        baseline_root
                        / "generations"
                        / baseline_pointer.generation_id
                        / "portfolio-baseline.json"
                    ).read_bytes()
                ).hexdigest(),
                "generation_id": baseline_pointer.generation_id,
            },
            "benchmark_assignments": [
                {"strategy_id": row["strategy_id"]} for row in catalog["strategies"]
            ],
            "catalog_schema_version": 1,
            "comparisons": [
                {"strategy_id": row["strategy_id"]} for row in catalog["strategies"]
            ],
            "costs": [
                {"strategy_id": row["strategy_id"]} for row in catalog["strategies"]
            ],
        }
    )
    attribution_manifest = GenerationManifest.build(
        as_of_date=builder.date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic-attribution", "1"),
        coverage=provider_manifest.coverage,
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/strategy-attribution.json",
                sha256=hashlib.sha256(attribution).hexdigest(),
                size_bytes=len(attribution),
                as_of_date=builder.date(2026, 1, 3),
                schema_name="strategy-attribution-catalog",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    attribution_generation_path = stage_generation(
        attribution_root, attribution_manifest
    )
    (attribution_generation_path.parent / "strategy-attribution.json").write_bytes(
        attribution
    )
    publish_generation(attribution_root, attribution_manifest.generation_id)
    epoch_root = history_root / "epoch"
    epoch_payload = canonical_json_bytes({"schema_version": 8})
    epoch_verification = canonical_json_bytes({"status": "VERIFIED"})
    epoch_manifest = GenerationManifest.build(
        as_of_date=builder.date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic-epoch-manifest", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic report wrapper epoch manifest",
            fact_types=("account-strategy-manifest",),
            observed_start=builder.date(2026, 1, 3),
            observed_end=builder.date(2026, 1, 3),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="outputs/account-strategy-manifest.json",
                sha256=hashlib.sha256(epoch_payload).hexdigest(),
                size_bytes=len(epoch_payload),
                as_of_date=builder.date(2026, 1, 3),
                schema_name="account-strategy-manifest",
                schema_version="8",
            ),
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(epoch_verification).hexdigest(),
                size_bytes=len(epoch_verification),
                as_of_date=builder.date(2026, 1, 3),
                schema_name="independent-verification",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        epoch_root,
        epoch_manifest,
        {
            "outputs/account-strategy-manifest.json": epoch_payload,
            "verification/independent-verification.json": epoch_verification,
        },
    )
    runtime = tmp_path / "xlsx-runtime.exe"
    cli = tmp_path / "xlsx-cli.py"
    runtime.write_bytes(b"synthetic-runtime")
    cli.write_bytes(b"synthetic-cli")
    monkeypatch.setattr(builder, "XLSX_RUNTIME", runtime)
    monkeypatch.setattr(builder, "XLSX_CLI", cli)
    real_run = builder._run

    def xlsx_command_boundary(command: object, **kwargs: object) -> dict[str, object]:
        assert isinstance(command, tuple)
        if command[:3] == (str(runtime), str(cli), "run"):
            job_path = Path(command[3])
            result_path = Path(command[command.index("--result") + 1])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            candidate = job_path.parent
            workbook = candidate / "excel" / "portfolio-report.xlsx"
            workbook.parent.mkdir(exist_ok=True)
            shutil.copyfile(candidate / "portfolio-report.xlsx", workbook)
            result_path.write_bytes(
                canonical_json_bytes(
                    {
                        "final_state": "SUCCEEDED",
                        "idempotency_key": job["idempotency_key"],
                        "invariants": [],
                        "ok": True,
                        "run_id": "b" * 32,
                        "schema_version": "2.0",
                        "steps": [
                            {"status": "succeeded", "step_index": 0, "type": "open"},
                            {"status": "succeeded", "step_index": 1, "type": "recalc"},
                            {"status": "succeeded", "step_index": 2, "type": "save_as"},
                        ],
                    }
                )
            )
            return {}
        if command[:3] == (str(runtime), str(cli), "validate-contract"):
            return {"all_passed": True, "invariants": [{"passed": True}]}
        return real_run(command, **kwargs)

    monkeypatch.setattr(builder, "_run", xlsx_command_boundary)
    output_root = tmp_path / "nested-output-root" / "report"
    result = builder.build_report(
        baseline_root,
        provider_root,
        freshness,
        history,
        implementation,
        return_attribution,
        attribution_root,
        output_root,
        epoch_manifest_generation_root=epoch_root,
        epoch_manifest_generation_id=epoch_manifest.generation_id,
    )

    generation_id = str(result["generation_id"])
    authority = output_root / "generations" / generation_id
    delivery = output_root / "deliveries" / generation_id
    assert result["epoch_manifest_generation_id"] == epoch_manifest.generation_id
    assert (
        result["epoch_manifest_artifact"]["sha256"]
        == hashlib.sha256(epoch_payload).hexdigest()
    )
    assert (authority / "portfolio-report-data.json").read_bytes() == (
        Path(str(result["candidate_dir"])) / "portfolio-report-data.json"
    ).read_bytes()
    assert not any(path.suffix.lower() == ".xlsx" for path in authority.rglob("*"))
    assert (delivery / "portfolio-report.xlsx").is_file()
    assert result["report_delivery"]["workbook_status"] == "EXACT_NON_AUTHORITY"


@pytest.mark.parametrize(
    "smuggled_path",
    (
        "deliveries/smuggled.json",
        "nested/deliveries/smuggled.json",
        "Deliveries/smuggled.json",
        "nested/portfolio-report.XLSX",
    ),
)
def test_report_delivery_drift_is_non_authority_and_exact_acceptance_refuses_it(
    tmp_path: Path, smuggled_path: str
) -> None:
    """The consumer reads real producer output without inventing workbook authority."""

    producer_spec = importlib.util.spec_from_file_location(
        "_report_delivery_producer_for_release_builder",
        ROOT / "tools" / "portfolio_report.py",
    )
    assert producer_spec is not None and producer_spec.loader is not None
    producer = importlib.util.module_from_spec(producer_spec)
    sys.modules[producer_spec.name] = producer
    try:
        producer_spec.loader.exec_module(producer)
        fixture_source = (
            ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py"
        ).read_text(encoding="utf-8")
        start = fixture_source.index("def _amount(")
        end = fixture_source.index("\ndef _published_dependence(")
        namespace = {
            "AdapterIdentity": AdapterIdentity,
            "Any": object,
            "CoverageClaim": CoverageClaim,
            "CoverageStatus": CoverageStatus,
            "Decimal": __import__("decimal").Decimal,
            "GenerationManifest": GenerationManifest,
            "SourceArtifact": SourceArtifact,
            "VerificationStatus": VerificationStatus,
            "canonical_json_bytes": canonical_json_bytes,
            "date": __import__("datetime").date,
            "hashlib": hashlib,
            "publish_generation": __import__(
                "quant_engine.core", fromlist=["publish_generation"]
            ).publish_generation,
            "stage_generation": __import__(
                "quant_engine.core", fromlist=["stage_generation"]
            ).stage_generation,
        }
        exec(compile(fixture_source[start:end], "producer-fixture", "exec"), namespace)
        baseline_root = namespace["_published_baseline"](tmp_path / "baseline")
        candidate = Path(
            producer.build_candidate(baseline_root, tmp_path / "candidates")[
                "candidate_dir"
            ]
        )
        initial_workbook = candidate / producer.WORKBOOK_NAME
        workbook = candidate / "excel" / producer.WORKBOOK_NAME
        workbook.parent.mkdir(parents=True)
        shutil.copyfile(initial_workbook, workbook)
        job = json.loads((candidate / "xlsx-job.json").read_text(encoding="utf-8"))
        xlsx_result = candidate / "xlsx-result.json"
        xlsx_result.write_text(
            json.dumps(
                {
                    "final_state": "SUCCEEDED",
                    "idempotency_key": job["idempotency_key"],
                    "invariants": [],
                    "ok": True,
                    "run_id": "a" * 32,
                    "schema_version": "2.0",
                    "steps": [
                        {
                            "message": "Opened canonical staged workbook.",
                            "status": "succeeded",
                            "step_index": 0,
                            "type": "open",
                        },
                        {
                            "message": "Calculation reached xlDone.",
                            "status": "succeeded",
                            "step_index": 1,
                            "type": "recalc",
                        },
                        {
                            "message": "Saved canonical staged workbook.",
                            "status": "succeeded",
                            "step_index": 2,
                            "type": "save_as",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        validation = candidate / "workbook-validation.json"
        validation.write_text(
            json.dumps({"all_passed": True, "invariants": [{"passed": True}]}),
            encoding="utf-8",
        )
        producer.canonicalize_workbook(
            workbook, candidate / "workbook-canonicalization.json"
        )
        producer.canonicalize_xlsx_result(
            xlsx_result, candidate / "xlsx-result-canonicalization.json"
        )
        binding = candidate / "workbook-evidence-binding.json"
        producer.bind_workbook_evidence(
            candidate, workbook, xlsx_result, validation, binding
        )
        authority_root = tmp_path / "published"
        published = producer.publish_report(
            baseline_root,
            candidate,
            workbook,
            xlsx_result,
            validation,
            authority_root,
            workbook_evidence_binding=binding,
        )
        generation_id = str(published["generation_id"])
        delivery_dir = authority_root / "deliveries" / generation_id
    finally:
        sys.modules.pop(producer_spec.name, None)

    authority_generation = authority_root / "generations" / generation_id
    authority_report = (
        authority_generation / "portfolio-report-data.json"
    ).read_bytes()
    authority_manifest = (authority_generation / "generation.json").read_bytes()
    expected_report_sha256 = hashlib.sha256(authority_report).hexdigest()
    expected_manifest_sha256 = hashlib.sha256(authority_manifest).hexdigest()

    materializer_spec = importlib.util.spec_from_file_location(
        "_report_delivery_materializer_for_release_builder",
        ROOT / "tools" / "portfolio_pipeline_materialize.py",
    )
    assert materializer_spec is not None and materializer_spec.loader is not None
    materializer = importlib.util.module_from_spec(materializer_spec)
    sys.modules[materializer_spec.name] = materializer
    try:
        materializer_spec.loader.exec_module(materializer)
        producer_inventory = inventory_generation(authority_generation)
        assert materializer._generation_expectation(
            {
                "files": producer_inventory["files"],
                "generation_id": producer_inventory["generation_id"],
                "manifest_sha256": producer_inventory["manifest_sha256"],
            },
            "report",
        ) == {
            "files": producer_inventory["files"],
            "generation_id": producer_inventory["generation_id"],
            "manifest_sha256": producer_inventory["manifest_sha256"],
        }
    finally:
        sys.modules.pop(materializer_spec.name, None)

    inspection = builder._inspect_report_delivery(
        authority_root, generation_id, delivery_dir
    )
    assert inspection["delivery_classification"] == "NON_AUTHORITY_HUMAN_DELIVERY"
    assert inspection["workbook_status"] == "EXACT_NON_AUTHORITY"
    assert inspection["report_data_sha256"] == expected_report_sha256
    assert inspection["generation_manifest_sha256"] == expected_manifest_sha256
    assert not list(authority_generation.rglob("*.xlsx"))

    receipt_path = delivery_dir / "report-delivery.json"
    receipt_bytes = receipt_path.read_bytes()
    trusted_authority = json.loads(receipt_bytes)["authority"]
    trusted_manifest_sha256 = trusted_authority["generation_manifest_sha256"]
    trusted_inventory_sha256 = trusted_authority["generation_inventory_sha256"]
    with pytest.raises(
        builder.VerticalBuildError,
        match="explicit report authority identity is incomplete",
    ):
        builder._inspect_report_delivery(
            authority_root,
            generation_id,
            delivery_dir,
            expected_manifest_sha256=trusted_manifest_sha256,
        )
    explicit = builder._require_exact_report_delivery(
        authority_root,
        generation_id,
        delivery_dir,
        expected_manifest_sha256=trusted_manifest_sha256,
        expected_inventory_sha256=trusted_inventory_sha256,
    )
    assert explicit["workbook_status"] == "EXACT_NON_AUTHORITY"
    receipt_path.unlink()
    with pytest.raises(builder.VerticalBuildError, match="delivery is unavailable"):
        builder._require_exact_report_delivery(
            authority_root, generation_id, delivery_dir
        )
    receipt_path.write_bytes(receipt_bytes)
    receipt_path.write_bytes(b"{")
    with pytest.raises(builder.VerticalBuildError, match="receipt is unreadable"):
        builder._require_exact_report_delivery(
            authority_root, generation_id, delivery_dir
        )
    receipt_path.write_bytes(receipt_bytes)
    rebound = json.loads(receipt_bytes)
    rebound["authority"]["generation_id"] = "0" * 64
    receipt_path.write_bytes(canonical_json_bytes(rebound))
    with pytest.raises(builder.VerticalBuildError, match="authority binding differs"):
        builder._require_exact_report_delivery(
            authority_root, generation_id, delivery_dir
        )
    receipt_path.write_bytes(receipt_bytes)
    assert (
        authority_generation / "portfolio-report-data.json"
    ).read_bytes() == authority_report
    assert builder.read_current(authority_root).generation_id == generation_id

    with zipfile.ZipFile(delivery_dir / "portfolio-report.xlsx", "a") as archive:
        archive.writestr("customXml/item1.xml", b"<sharepoint-rewrite/>")

    drifted = builder._inspect_report_delivery(
        authority_root, generation_id, delivery_dir
    )
    assert drifted["workbook_status"] == "DRIFTED_NON_AUTHORITY"
    assert (
        authority_generation / "portfolio-report-data.json"
    ).read_bytes() == authority_report
    assert (authority_generation / "generation.json").read_bytes() == authority_manifest
    with pytest.raises(builder.VerticalBuildError, match="exact non-authority"):
        builder._require_exact_report_delivery(
            authority_root, generation_id, delivery_dir
        )

    smuggled = authority_generation / smuggled_path
    smuggled.parent.mkdir(parents=True)
    smuggled.write_bytes(canonical_json_bytes({"smuggled": True}))
    rebound = json.loads(receipt_bytes)
    rebound["authority"]["generation_inventory_sha256"] = (
        builder.generation_inventory_sha256(authority_root, generation_id)
    )
    receipt_path.write_bytes(canonical_json_bytes(rebound))
    with pytest.raises(
        builder.VerticalBuildError,
        match="inventories workbook delivery",
    ):
        builder._inspect_report_delivery(
            authority_root,
            generation_id,
            delivery_dir,
            expected_manifest_sha256=trusted_manifest_sha256,
            expected_inventory_sha256=trusted_inventory_sha256,
        )
    with pytest.raises(
        builder.VerticalBuildError, match="current report generation differs"
    ):
        builder._inspect_report_delivery(authority_root, generation_id, delivery_dir)
    with pytest.raises(PointerIntegrityError, match="artifact inventory"):
        builder.read_current(authority_root)


@pytest.mark.parametrize("mutation", ("root", "generation_id", "source", "bytes"))
def test_schema10_publish_rejects_mutated_epoch_candidate_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _schema10_history_contract: None,
    mutation: str,
) -> None:
    """A publish must reject a substituted bundle identity or staged byte stream."""

    recipe, _generation_root, _generation_id = _schema10_recipe(tmp_path)
    panel_root = tmp_path / "stages" / "panel"
    candidate_dir = tmp_path / "candidate"
    qa_dir = tmp_path / "qa"

    def fake_tool(*args: object, **_kwargs: object) -> dict[str, object]:
        command = str(args[1])
        if command == "build":
            candidate_dir.mkdir()
            (candidate_dir / "panel.html").write_text("candidate", encoding="utf-8")
            return {"candidate_dir": str(candidate_dir)}
        if command == "qa":
            qa_dir.mkdir(exist_ok=True)
            (qa_dir / "browser.json").write_text("{}", encoding="utf-8")
        return {}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    analytics = {
        "decomposition_root": str(tmp_path / "decomposition"),
        "decomposition_lenses_root": str(tmp_path / "lenses"),
        "correlation_root": str(tmp_path / "correlation"),
        "weekly_root": str(tmp_path / "weekly"),
    }
    candidate = builder.build_panel_candidate(
        recipe,
        tmp_path / "report",
        tmp_path / "baseline",
        tmp_path / "attribution",
        analytics,
        tmp_path / "freshness.json",
        panel_root,
    )
    staged_manifest = Path(str(candidate["account_strategy_manifest"]))
    expected_root = str(candidate["epoch_manifest_generation_root"])
    expected_generation_id = str(candidate["epoch_manifest_generation_id"])
    expected_source = candidate["epoch_manifest_source"]
    if mutation == "root":
        expected_root = str(tmp_path / "alternate-generation-root")
    elif mutation == "generation_id":
        expected_generation_id = "f" * 64
    elif mutation == "source":
        expected_source = {**expected_source, "generation_id": "f" * 64}
    else:
        staged_manifest.write_bytes(staged_manifest.read_bytes() + b" ")

    with pytest.raises(
        builder.VerticalBuildError, match="epoch-manifest|staged account"
    ):
        builder.publish_panel(
            recipe,
            tmp_path / "report",
            tmp_path / "baseline",
            tmp_path / "attribution",
            analytics,
            tmp_path / "freshness.json",
            candidate_dir,
            qa_dir,
            panel_root,
            account_strategy_manifest=staged_manifest,
            expected_account_strategy_manifest_sha256=str(
                candidate["account_strategy_manifest_sha256"]
            ),
            expected_account_strategy_manifest_size_bytes=int(
                candidate["account_strategy_manifest_size_bytes"]
            ),
            expected_epoch_manifest_generation_root=expected_root,
            expected_epoch_manifest_generation_id=expected_generation_id,
            expected_epoch_manifest_source=expected_source,
        )

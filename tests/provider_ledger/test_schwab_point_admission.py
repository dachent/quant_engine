"""Synthetic selected-source point positions across the real admission seam."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest

from quant_engine.core import GenerationManifest
from quant_engine.core.contracts import canonical_json_bytes
import portfolio_pipeline_materialize as materializer


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "schwab_point_source_fixture", Path(__file__).with_name("test_admit_or_declare.py")
)
assert SPEC is not None and SPEC.loader is not None
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
ledger = fixtures.ledger
CLOCK = "2026-09-18T03:59:40.004417+00:00"
CUTOFF = date(2026, 9, 17)
REQUESTED = date(2026, 9, 18)
PointStage = tuple[Path, dict[str, Any], Path]


@pytest.fixture(scope="module")
def point_stage(tmp_path_factory: pytest.TempPathFactory) -> PointStage:
    return cast(
        PointStage,
        fixtures._source_bound_stage(
            tmp_path_factory.mktemp("point-source"),
            source_cutoff=CUTOFF,
            schwab_point_capture=CLOCK,
            requested_as_of=REQUESTED,
        ),
    )


def _verify(generation: Path, authority: dict[str, Any]) -> dict[str, Any]:
    return materializer._provider_admission_authority(
        generation,
        authority["contract"],
        **{
            key: authority[key]
            for key in (
                "contract_raw_sha256",
                "contract_semantic_sha256",
                "census_sha256",
            )
        },
    )


def _selected(
    authority: dict[str, Any],
) -> tuple[Path, GenerationManifest, dict[str, Any]]:
    selection = authority["run_observation"]["providers"]["schwab"]
    root = Path(selection["root"])
    generation = root / "generations" / selection["generation_id"]
    return (
        root,
        GenerationManifest.from_json_bytes(
            (generation / "generation.json").read_bytes()
        ),
        json.loads((generation / "schwab-generation.json").read_bytes()),
    )


def test_build_catalog_retains_selected_point_without_extending_daily_finality(
    point_stage: PointStage,
) -> None:
    generation, authority, spec_path = point_stage
    source_root, _, native = _selected(authority)
    originals = {
        path: path.read_bytes() for path in source_root.rglob("*") if path.is_file()
    }
    policy_path = ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
    policy_before = policy_path.read_bytes()
    catalog, _, _, receipt_bytes = ledger.build_catalog(spec_path)
    positions = [row for row in catalog["positions"] if row["provider_id"] == "schwab"]
    assert len(positions) == 1
    position = positions[0]
    assert position["source_record_id"] == native["positions"][0]["record_id"]
    assert position["record_id"] == "schwab:" + native["positions"][0]["record_id"]
    assert (position["as_of_date"], position["observed_at"], position["quantity"]) == (
        "2026-09-18",
        CLOCK,
        "3",
    )
    assert position["source_artifact_id"] == "web/api-current-snapshot.json"
    assert position["average_price"] == native["positions"][0]["average_price"]
    assert position["market_value"] == {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "value": None,
    }
    assert native["current_position_account_count"] == 1
    assert len(native["current_accounts"]) == 2
    for fact in ("nav_observations", "flows", "transactions"):
        assert all(
            row.get("as_of_date", row.get("event_date", "")) <= "2026-09-17"
            for row in catalog[fact]
        )
    assert all(row["date"] <= "2026-09-17" for row in catalog["strategy_returns"])
    receipt = json.loads(receipt_bytes)
    assert all(
        row["finalized_through"] == "2026-09-17"
        for row in receipt["run_observation"]["providers"]
    )
    assert _verify(generation, authority)["provider_count"] == 7
    assert policy_path.read_bytes() == policy_before
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not (spec_path.parent / "staged/CURRENT.json").exists()


def _forged_candidate(
    tmp_path: Path, generation: Path, authority: dict[str, Any], catalog: dict[str, Any]
) -> Path:
    """Rebind all candidate receipts/digests; only selected sources stay fixed."""
    identities, manifests, catalogs = [], {}, {}
    observation = authority["run_observation"]
    for provider_id, selection in observation["providers"].items():
        reference = {
            key: selection[key]
            for key in (
                "root",
                "generation_id",
                "manifest_sha256",
                "catalog_sha256",
                "qualification",
            )
        }
        identity, manifest, source, _, _ = ledger._provider(
            provider_id, reference, REQUESTED, run_observation=observation
        )
        identities.append(identity)
        manifests[provider_id], catalogs[provider_id] = manifest, source
    facts = {
        provider_id: {
            fact_name: [
                row
                for row in catalog.get(fact_name, [])
                if row["provider_id"] == provider_id
            ]
            for fact_name in ledger._TARGET_ADMISSION_FACT
            if fact_name != "provider_strategy_returns"
        }
        for provider_id in manifests
    }
    for fact_name in (
        "financing_cost_observations",
        "financing_liability_observations",
        "leverage_exposure_observations",
    ):
        if not facts["ibkr"].get(fact_name):
            facts["ibkr"].pop(fact_name, None)
    _, receipt_bytes = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        facts,
        catalog["provider_strategy_returns"],
    )
    contents = {
        "normalized/admit-or-declare-receipt.json": receipt_bytes,
        "normalized/provider-ledger.json": canonical_json_bytes(catalog),
    }
    original = GenerationManifest.from_json_bytes(
        (generation / "generation.json").read_bytes()
    )
    manifest = GenerationManifest.build(
        as_of_date=original.as_of_date,
        adapter=original.adapter,
        coverage=original.coverage,
        verification=original.verification,
        artifacts=tuple(
            replace(
                artifact,
                sha256=hashlib.sha256(contents[artifact.artifact_id]).hexdigest(),
                size_bytes=len(contents[artifact.artifact_id]),
            )
            if artifact.artifact_id in contents
            else artifact
            for artifact in original.artifacts
        ),
    )
    forged = tmp_path / manifest.generation_id
    forged.mkdir()
    (forged / "generation.json").write_bytes(manifest.to_json_bytes())
    for artifact_id, content in contents.items():
        (forged / Path(artifact_id).name).write_bytes(content)
    return forged


@pytest.mark.parametrize(
    "attack",
    [
        "quantity",
        "market_value",
        "average_price",
        "account_id",
        "source_record_id",
        "source_artifact_id",
        "observed_at",
        "as_of_date",
        "omit",
        "duplicate",
        "daily_nav",
    ],
)
def test_materializer_reconstructs_point_from_selected_rows_despite_coherent_receipt(
    point_stage: PointStage, tmp_path: Path, attack: str
) -> None:
    generation, authority, _ = point_stage
    catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    assert len(catalog["positions"]) == 1
    position = catalog["positions"][0]
    if attack == "omit":
        catalog["positions"] = []
    elif attack == "duplicate":
        catalog["positions"].append(copy.deepcopy(position))
    elif attack == "daily_nav":
        next(
            row for row in catalog["nav_observations"] if row["provider_id"] == "schwab"
        )["as_of_date"] = "2026-09-18"
    else:
        position[attack] = {
            "quantity": "4",
            "market_value": {"currency": "USD", "status": "AVAILABLE", "value": "99"},
            "average_price": "999",
            "account_id": "synthetic-schwab-empty",
            "source_record_id": "synthetic-unselected-position",
            "source_artifact_id": "native/nav.csv",
            "observed_at": "2026-09-19T03:59:40+00:00",
            "as_of_date": "2026-09-19",
        }[attack]
    forged = _forged_candidate(tmp_path, generation, authority, catalog)
    reason = (
        "materialized provider fact cutoff or generation differs"
        if attack == "daily_nav"
        else "materialized Schwab point differs from selected native positions"
    )
    with pytest.raises(materializer.MaterializationError, match=reason):
        _verify(forged, authority)


def test_point_source_catalog_mutation_rejected_by_build_and_materializer(
    tmp_path: Path,
) -> None:
    generation, authority, spec = fixtures._source_bound_stage(
        tmp_path,
        source_cutoff=CUTOFF,
        schwab_point_capture=CLOCK,
        requested_as_of=REQUESTED,
    )
    root, manifest, _ = _selected(authority)
    path = root / "generations" / manifest.generation_id / "schwab-generation.json"
    content = path.read_bytes()
    assert b'"quantity":"3"' in content
    path.write_bytes(content.replace(b'"quantity":"3"', b'"quantity":"4"'))
    with pytest.raises(ledger.ProviderLedgerError):
        ledger.build_catalog(spec)
    with pytest.raises(materializer.MaterializationError):
        _verify(generation, authority)


@pytest.mark.parametrize("requested", [None, date(2026, 9, 16), CUTOFF])
def test_point_projection_preserves_legacy_and_requested_date_bounds(
    point_stage: PointStage,
    requested: date | None,
) -> None:
    _, authority, _ = point_stage
    root, manifest, catalog = _selected(authority)
    kwargs = {} if requested is None else {"requested_as_of": requested}
    facts, findings = ledger._project_account_facts(
        "schwab",
        manifest.generation_id,
        catalog,
        {"synthetic-schwab"},
        set(),
        {},
        manifest=manifest,
        root=root,
        financial_cutoff=CUTOFF,
        **kwargs,
    )
    outside_positions = [
        row
        for row in findings
        if row.get("fact_type") == "positions"
        and row["status"] == "OUTSIDE_FINANCIAL_CUTOFF"
    ]
    if requested == CUTOFF:
        # The native UTC timestamp falls on September 17 in New York. Keep
        # its native bytes while selecting it by the provider's financial day.
        assert len(facts["positions"]) == 1
        position = facts["positions"][0]
        assert position["as_of_date"] == "2026-09-18"
        assert position["observed_at"] == CLOCK
        assert not outside_positions
    else:
        assert not facts["positions"]
        assert outside_positions


@pytest.mark.parametrize(
    "attack",
    [
        "missing_clock",
        "naive_clock",
        "mismatched_clock",
        "missing_artifact",
        "artifact_hash",
        "artifact_size",
        "artifact_date",
        "artifact_schema",
        "transport",
        "row_date",
        "row_clock",
        "row_account",
        "row_record",
        "duplicate_row",
        "account_clock",
        "account_scope",
    ],
)
def test_selected_point_proof_must_be_coherent(
    point_stage: PointStage,
    tmp_path: Path,
    attack: str,
) -> None:
    _, authority, _ = point_stage
    _, original_manifest, catalog = _selected(authority)
    if attack == "missing_clock":
        catalog.pop("position_snapshot_as_of")
    elif attack in {"naive_clock", "mismatched_clock"}:
        catalog["position_snapshot_as_of"]["observed_at"] = (
            "2026-09-18T03:59:40"
            if attack == "naive_clock"
            else "2026-09-18T04:59:40+00:00"
        )
    elif attack == "missing_artifact":
        catalog["source_artifacts"] = []
    elif attack.startswith("artifact_"):
        field, value = {
            "artifact_hash": ("sha256", "b" * 64),
            "artifact_size": ("size_bytes", 1),
            "artifact_date": ("as_of_date", "2026-09-17"),
            "artifact_schema": ("schema", {"name": "unknown", "version": "2"}),
        }[attack]
        catalog["source_artifacts"][0][field] = value
    elif attack == "transport":
        catalog["current_transport"] = "UNSUPPORTED"
    elif attack.startswith("row_"):
        field, value = {
            "row_date": ("as_of_date", "2026-09-19"),
            "row_clock": ("observed_at", "2026-09-18T04:00:00+00:00"),
            "row_account": ("account_id", "synthetic-unselected"),
            "row_record": ("record_id", "synthetic-unselected"),
        }[attack]
        catalog["positions"][0][field] = value
    elif attack == "duplicate_row":
        catalog["positions"].append(copy.deepcopy(catalog["positions"][0]))
    elif attack == "account_clock":
        catalog["current_accounts"][0]["observed_at"] = "2026-09-18T04:00:00+00:00"
    else:
        catalog["current_accounts"].pop()
    content = canonical_json_bytes(catalog)
    manifest = GenerationManifest.build(
        as_of_date=original_manifest.as_of_date,
        adapter=original_manifest.adapter,
        coverage=original_manifest.coverage,
        verification=original_manifest.verification,
        artifacts=tuple(
            replace(
                a, sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
            )
            if a.artifact_id == "schwab-generation.json"
            else a
            for a in original_manifest.artifacts
        ),
    )
    generation = tmp_path / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "schwab-generation.json").write_bytes(content)
    try:
        facts, _ = ledger._project_account_facts(
            "schwab",
            manifest.generation_id,
            catalog,
            {"synthetic-schwab"},
            set(),
            {},
            manifest=manifest,
            root=tmp_path,
            financial_cutoff=CUTOFF,
            requested_as_of=REQUESTED,
        )
    except ledger.ProviderLedgerError:
        pass
    else:
        assert not facts["positions"]
    try:
        expected = materializer._schwab_selected_point_positions(
            manifest, catalog, REQUESTED
        )
    except materializer.MaterializationError:
        pass
    else:
        assert not expected

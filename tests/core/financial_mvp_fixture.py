from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
DAY = date(2026, 8, 14)


@dataclass(frozen=True)
class FinancialMvpFixture:
    contract: dict[str, Any]
    contract_path: Path
    financial_module: object
    generation: Path
    matrix_bytes: bytes
    matrix_path: Path
    receipt: dict[str, Any]


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _provider_test_module() -> object:
    path = ROOT / "tests" / "provider_ledger" / "test_admit_or_declare.py"
    spec = importlib.util.spec_from_file_location(
        "financial_mvp_provider_fixture", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy_rebuild(
    provider_module: object,
    sources: dict[str, dict[str, Any]],
    identities: list[object],
    manifests: dict[str, GenerationManifest],
    catalogs: dict[str, dict[str, Any]],
    provider_facts: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    ready: bool,
) -> None:
    ledger = provider_module.ledger
    identity_by_provider = {identity.provider_id: identity for identity in identities}
    for provider_id, source_id in ledger._PROVIDER_SOURCE_SEGMENTS.items():
        source = sources[source_id]
        source.update(
            {
                "absence_behavior": "FAIL_CLOSED_AND_NAME_MISSING_DATA",
                "acquisition": {
                    "adapter": f"synthetic-{provider_id}",
                    "mode": "LOCAL_PRIVATE_READ_ONLY",
                },
                "authority": {"class": "PORTABLE_SYNTHETIC"},
                "blockers": [] if ready else [303],
                "endpoint_or_artifact_selector": "normalized/nav.json",
                "existence_status": "CAPTURED",
                "freshness": {
                    "max_age_days": 3,
                    "publication_lag_days": 0,
                    "rule": "WARN_COMPLETE_DATA_AGE",
                },
                "interval": {
                    "observed_end": DAY.isoformat(),
                    "observed_start": DAY.isoformat(),
                },
                "owner_policy_decision": (
                    "SYNTHETIC_READY" if ready else "SYNTHETIC_REQUIRED_NOT_READY"
                ),
                "readiness": "READY" if ready else "REQUIRED_NOT_READY",
                "schema": {"name": "synthetic-nav", "version": "1"},
                "source_version": "1",
                "strategy_version": {"status": "UNRESOLVED", "version_refs": {}},
                "subjects": {
                    "account_scope": "PORTABLE_SYNTHETIC",
                    "strategy_ids": ["strategy-1"],
                },
            }
        )
        identity = identity_by_provider[provider_id]
        source["current_generation"] = {
            "as_of": DAY.isoformat(),
            "generation_id": identity.generation_id,
            "sha256": identity.manifest_sha256,
        }
        inventory = ledger._provider_inventory(
            provider_id, manifests[provider_id], catalogs[provider_id], source
        )
        policy = source["admission"]["dataset_disposition_policy"]
        policy["upstream_inventory_sha256"] = ledger._inventory_digest(inventory)
        product_count, product_digest = ledger._product_data_class_set_digest(inventory)
        policy["product_data_class_count"] = product_count
        policy["product_data_class_set_sha256"] = product_digest
        dispositions = [
            ledger._dataset_disposition(
                item,
                source,
                policy,
                DAY,
                DAY,
                provider_facts[provider_id],
                (),
                "c" * 64,
            )
            for item in inventory
        ]
        admitted_count, admitted_digest = ledger._admitted_item_set_digest(dispositions)
        policy["admitted_item_count"] = admitted_count
        policy["admitted_item_set_sha256"] = admitted_digest


def build_financial_mvp_fixture(
    tmp_path: Path, *, ready: bool = False
) -> FinancialMvpFixture:
    tmp_path.mkdir(parents=True, exist_ok=True)
    provider_module = _provider_test_module()
    authority, identities, manifests, catalogs, provider_facts = (
        provider_module._fixture()
    )
    sources = authority["sources"]
    _policy_rebuild(
        provider_module,
        sources,
        identities,
        manifests,
        catalogs,
        provider_facts,
        ready=ready,
    )
    contract: dict[str, Any] = {
        "contract_schema_version": 6,
        "current_census": {
            "account_sleeves": [],
            "census_id": "SYNTHETIC_CENSUS",
        },
        "financial_outcomes": [
            {
                "blockers": [] if ready else [324],
                "calculation_contract": {"policy_id": "SYNTHETIC_INVESTED_CAPITAL"},
                "consumers": ["REPORT"],
                "outcome_id": "INVESTED_CAPITAL",
                "product_requirement": "REQUIRED",
                "readiness": "READY" if ready else "REQUIRED_NOT_READY",
            }
        ],
        "history_corrections": {"corrections": []},
        "product": {
            "product_id": "FINANCIAL_MVP",
            "product_version": "test-v1",
        },
        "source_segments": sorted(
            (deepcopy(row) for row in sources.values()),
            key=lambda row: row["source_id"],
        ),
        "strategies": [
            {
                "blockers": [] if ready else [302],
                "product_requirement": "REQUIRED",
                "readiness": "READY" if ready else "REQUIRED_NOT_READY",
                "strategy_id": "strategy-1",
            }
        ],
    }
    contract_bytes = canonical_json_bytes(contract)
    contract_semantic_sha256 = _sha(contract_bytes)
    census_sha256 = _sha(canonical_json_bytes(contract["current_census"]))
    authority.update(
        {
            "census_id": contract["current_census"]["census_id"],
            "census_sha256": census_sha256,
            "contract": contract,
            "contract_raw_sha256": contract_semantic_sha256,
            "contract_semantic_sha256": contract_semantic_sha256,
            "product_id": contract["product"]["product_id"],
            "product_version": contract["product"]["product_version"],
            "sources": sources,
        }
    )
    receipt, receipt_bytes = provider_module.ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        (),
    )
    catalog_bytes = canonical_json_bytes(
        {"as_of_date": DAY.isoformat(), "catalog_schema_version": 2}
    )
    provider_artifacts = []
    for row in receipt["provider_generations"]:
        provider_id = row["provider_id"]
        provider_artifacts.extend(
            (
                SourceArtifact(
                    artifact_id=f"providers/{provider_id}/generation.json",
                    sha256=row["manifest_sha256"],
                    size_bytes=1,
                    as_of_date=DAY,
                    schema_name="generation-manifest",
                    schema_version="1",
                ),
                SourceArtifact(
                    artifact_id=f"providers/{provider_id}/catalog.json",
                    sha256=row["catalog_sha256"],
                    size_bytes=1,
                    as_of_date=DAY,
                    schema_name="synthetic-provider-catalog",
                    schema_version="1",
                ),
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=DAY,
        adapter=AdapterIdentity("quant-engine-provider-ledger", "5"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="portable seven-provider composition authority",
            fact_types=("provider-admission-receipt",),
            observed_start=DAY,
            observed_end=DAY,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/admit-or-declare-receipt.json",
                sha256=_sha(receipt_bytes),
                size_bytes=len(receipt_bytes),
                as_of_date=DAY,
                schema_name="provider-admission-receipt",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="normalized/provider-ledger.json",
                sha256=_sha(catalog_bytes),
                size_bytes=len(catalog_bytes),
                as_of_date=DAY,
                schema_name="provider-ledger-catalog",
                schema_version="2",
            ),
            *provider_artifacts,
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = tmp_path / manifest.generation_id
    generation.mkdir()
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "admit-or-declare-receipt.json").write_bytes(receipt_bytes)
    (generation / "provider-ledger.json").write_bytes(catalog_bytes)

    contract_path = tmp_path / "financial-mvp-contract.json"
    contract_path.write_bytes(contract_bytes)
    matrix = {
        "census_sha256": census_sha256,
        "contract_sha256": contract_semantic_sha256,
        "product_version": contract["product"]["product_version"],
    }
    matrix_bytes = canonical_json_bytes(matrix)
    matrix_path = tmp_path / "financial-mvp-source-matrix.json"
    matrix_path.write_bytes(matrix_bytes)

    financial_module = SimpleNamespace(
        census_digest=lambda value: _sha(canonical_json_bytes(value)),
        contract_digest=lambda value: _sha(canonical_json_bytes(value)),
        invested_capital_policy_digest=lambda _value: _sha(
            canonical_json_bytes({"policy_id": "SYNTHETIC_INVESTED_CAPITAL"})
        ),
        load_contract=lambda _path, _registry, _schema: deepcopy(contract),
        render_views=lambda _value: SimpleNamespace(source_matrix_json=matrix_bytes),
    )
    return FinancialMvpFixture(
        contract=contract,
        contract_path=contract_path,
        financial_module=financial_module,
        generation=generation,
        matrix_bytes=matrix_bytes,
        matrix_path=matrix_path,
        receipt=receipt,
    )

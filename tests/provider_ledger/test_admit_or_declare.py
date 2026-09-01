from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

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
from quant_engine.portfolio_ledger import (
    GenerationQualification,
    ProviderAdmissionError,
    ProviderGenerationIdentity,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "provider_ledger_admit_or_declare_test", ROOT / "tools" / "provider_ledger.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)


DAY = date(2026, 8, 14)
P123_GENERATION_ID = "10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1"
P123_MANIFEST_SHA256 = (
    "e8870a4ff2a375d2c514975ac933c858ab7afe580eb9736fad04c1a2cf6f0907"
)
P123_CATALOG_SHA256 = "19000bdbdef4e07b01e9927a7e9b2683589352ebe55ca61247f1526a8d426aed"
P123_MANAGED_PERFORMANCE_SHA256 = (
    "6fe41e9c558d37df4e69c273ff67781284af03d1daedd0877799976ad86e29b8"
)
P123_EQUITY_CURVE_SHA256 = (
    "b87f760a1cd369bfd40ebc5e8db6b44e3626f9b4c53950e3d1e8c7ea0abf2b4e"
)
P123_RETURN_START = date(2025, 11, 13)
P123_RETURN_END = date(2026, 8, 13)
P123_GENERATION_ROOT = (
    ROOT.parent / "dataPortfolio123" / "generations" / P123_GENERATION_ID
)
P123_RETAINED_INPUTS = (
    P123_GENERATION_ROOT / "generation.json",
    P123_GENERATION_ROOT / "portfolio123-generation.json",
    P123_GENERATION_ROOT / "tables" / "har_managed_performance.parquet",
    P123_GENERATION_ROOT / "tables" / "har_equity_curve.parquet",
)


def _retained_p123_admit_or_declare_fixture() -> tuple[
    dict[str, Any],
    list[ProviderGenerationIdentity],
    dict[str, GenerationManifest],
    dict[str, dict[str, Any]],
    dict[str, dict[str, list[dict[str, Any]]]],
    list[dict[str, Any]],
]:
    """Bind the real receipt path to the retained P123 EU return evidence."""

    authority, identities, manifests, catalogs, provider_facts = _fixture()
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    source = next(
        item
        for item in contract["source_segments"]
        if item["source_id"] == "portfolio123-model-generation"
    )
    authority["sources"]["portfolio123-model-generation"] = source
    authority["contract_semantic_sha256"] = hashlib.sha256(
        canonical_json_bytes(contract)
    ).hexdigest()

    p123_root = ROOT.parent / "dataPortfolio123"
    generation_root = p123_root / "generations" / P123_GENERATION_ID
    manifest = GenerationManifest.from_json_bytes(
        (generation_root / "generation.json").read_bytes()
    )
    catalog = json.loads(
        (generation_root / "portfolio123-generation.json").read_text(encoding="utf-8")
    )
    assert manifest.generation_id == P123_GENERATION_ID
    assert hashlib.sha256(
        (generation_root / "generation.json").read_bytes()
    ).hexdigest() == (P123_MANIFEST_SHA256)
    assert (
        hashlib.sha256(
            (generation_root / "portfolio123-generation.json").read_bytes()
        ).hexdigest()
        == P123_CATALOG_SHA256
    )
    artifacts = {item.artifact_id: item for item in manifest.artifacts}
    assert artifacts["tables/har_managed_performance.parquet"].sha256 == (
        P123_MANAGED_PERFORMANCE_SHA256
    )
    assert artifacts["tables/har_equity_curve.parquet"].sha256 == (
        P123_EQUITY_CURVE_SHA256
    )

    identities = [
        (
            ProviderGenerationIdentity(
                provider_id="portfolio123",
                generation_id=P123_GENERATION_ID,
                manifest_sha256=P123_MANIFEST_SHA256,
                catalog_sha256=P123_CATALOG_SHA256,
                qualification=GenerationQualification.PUBLISHED_COMPLETE,
            )
            if item.provider_id == "portfolio123"
            else item
        )
        for item in identities
    ]
    manifests["portfolio123"] = manifest
    catalogs["portfolio123"] = catalog
    provider_facts["portfolio123"] = {}

    import pyarrow.parquet as pq

    entity_ids = sorted(
        {
            str(item["_entity_id"])
            for item in pq.read_table(
                generation_root / "tables" / "har_managed_performance.parquet",
                columns=["_entity_id"],
            ).to_pylist()
            if item.get("_entity_id") not in {None, ""}
        }
    )
    candidates = []
    for entity_id in entity_ids:
        rows = ledger._portfolio123_provider_returns(
            {"root": str(p123_root)},
            manifest,
            [
                {
                    "generation_id": P123_GENERATION_ID,
                    "strategy_id": "p123-eu",
                    "strategy_name": "EU [P123]",
                    "provider": {
                        "provider_id": "portfolio123",
                        "entity_id": entity_id,
                        "cash_added_semantics": (
                            "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA"
                        ),
                    },
                }
            ],
        )
        if (
            len(rows) == 196
            and rows[0]["date"] == P123_RETURN_START.isoformat()
            and rows[-1]["date"] == P123_RETURN_END.isoformat()
        ):
            candidates.append(rows)
    assert len(candidates) == 1
    return authority, identities, manifests, catalogs, provider_facts, candidates[0]


def _fixture(
    *,
    quantbase_layer: str = "ACCOUNT_LIVE",
    extra_dataset: bool = False,
    missing_product_class: bool = False,
    policy_effective_start: date = DAY,
    policy_effective_end: date | None = None,
) -> tuple[
    dict[str, Any],
    list[ProviderGenerationIdentity],
    dict[str, GenerationManifest],
    dict[str, dict[str, Any]],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    identities = []
    manifests = {}
    catalogs = {}
    provider_facts = {}
    sources = {}
    for index, provider_id in enumerate(sorted(ledger._PROVIDER_CATALOGS), start=1):
        artifact_bytes = f"synthetic-{provider_id}".encode()
        artifact = SourceArtifact(
            artifact_id="normalized/nav.json",
            sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            size_bytes=len(artifact_bytes),
            as_of_date=DAY,
            schema_name="synthetic-nav",
            schema_version="1",
        )
        manifest = GenerationManifest.build(
            as_of_date=DAY,
            adapter=AdapterIdentity(f"synthetic-{provider_id}", "1"),
            coverage=CoverageClaim(
                status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
                boundary="portable synthetic fixture",
                fact_types=("nav_observations",),
                observed_start=DAY,
                observed_end=DAY,
            ),
            artifacts=(artifact,),
            verification=VerificationStatus.VERIFIED,
        )
        catalog: dict[str, Any] = {
            "catalog_schema_version": 1,
            "nav_observations": [{"synthetic": True}],
        }
        catalog_sha = hashlib.sha256(canonical_json_bytes(catalog)).hexdigest()
        manifest_sha = f"{index:x}" * 64
        identity = ProviderGenerationIdentity(
            provider_id=provider_id,
            generation_id=manifest.generation_id,
            manifest_sha256=manifest_sha,
            catalog_sha256=catalog_sha,
            qualification=GenerationQualification.PUBLISHED_COMPLETE,
        )
        source_id = ledger._PROVIDER_SOURCE_SEGMENTS[provider_id]
        evidence_layer = (
            quantbase_layer if provider_id == "quantbase" else "ACCOUNT_LIVE"
        )
        sources[source_id] = {
            "admission": {
                "facts": ["NAV_OBSERVATION"],
                "transform": (
                    "provider-ledger-portfolio123-evidence"
                    if provider_id == "portfolio123"
                    else "provider-ledger"
                ),
                "version": "1",
                "dataset_disposition_policy": {
                    "admitted_item_count": 0,
                    "admitted_item_set_sha256": "0" * 64,
                    "allowed_exclusion_reasons": [
                        "REQUIRED_DATA_CLASS_NOT_MATERIALIZED",
                        "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER",
                    ],
                    "authority_issue": 303,
                    "effective_end": (
                        None
                        if policy_effective_end is None
                        else policy_effective_end.isoformat()
                    ),
                    "effective_start": policy_effective_start.isoformat(),
                    "exclusion_invalidated_outputs_rule": (
                        "SOURCE_SEGMENT_INVALIDATED_OUTPUTS_OR_NO_CURRENT_PRODUCT_OUTPUT"
                    ),
                    "exclusion_readiness_impact": "REQUIRED_NOT_READY",
                    "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
                    "policy_owner": "PRODUCT_OWNER_GOVERNING_ISSUE_303",
                    "policy_version": "1",
                    "product_data_class_count": 0,
                    "product_data_class_set_sha256": "0" * 64,
                    "upstream_inventory_sha256": "0" * 64,
                },
            },
            "capture_status": "CAPTURED_GOVERNED",
            "consumers": ["REPORT"],
            "current_generation": {
                "as_of": DAY.isoformat(),
                "generation_id": manifest.generation_id,
                "sha256": manifest_sha,
            },
            "data_classes": ["ACCOUNT_NAV"],
            "evidence_layer": evidence_layer,
            "invalidated_outputs": ["REPORT", "RELEASE_RECEIPT"],
            "product_requirement": "REQUIRED",
            "source_id": source_id,
            "temporal": {
                "calendar": "SYNTHETIC_DAILY",
                "cadence": "DAILY",
                "timezone": "UTC",
            },
            "units": {"currency": "USD", "measurement": "NAV"},
        }
        if missing_product_class and provider_id == "schwab":
            sources[source_id]["data_classes"].append("FINANCING_LIABILITY")
            sources[source_id]["admission"]["facts"].append("FINANCING_LIABILITY")
        row = {
            "account_id": f"private-account-{index}",
            "as_of_date": DAY.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "mapping_status": "MAPPED",
            "nav": str(100 + index),
            "provider_id": provider_id,
            "record_id": f"private-record-{index}",
            "source_artifact_id": "normalized/nav.json",
            "strategy_id": f"strategy-{index}",
        }
        identities.append(identity)
        manifests[provider_id] = manifest
        catalogs[provider_id] = catalog
        provider_facts[provider_id] = {"nav_observations": [row]}
        inventory = ledger._provider_inventory(
            provider_id, manifest, catalog, sources[source_id]
        )
        policy = sources[source_id]["admission"]["dataset_disposition_policy"]
        policy["upstream_inventory_sha256"] = ledger._inventory_digest(inventory)
        product_count, product_digest = ledger._product_data_class_set_digest(inventory)
        policy["product_data_class_count"] = product_count
        policy["product_data_class_set_sha256"] = product_digest
        dispositions = [
            ledger._dataset_disposition(
                item,
                sources[source_id],
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
    if extra_dataset:
        catalogs["schwab"]["new_provider_collection"] = [{"synthetic": True}]
    authority = {
        "census_id": "DATA-CENSUS-001",
        "census_sha256": "a" * 64,
        "contract": {"strategies": []},
        "contract_raw_sha256": "b" * 64,
        "contract_semantic_sha256": "c" * 64,
        "product_id": "FINANCIAL_MVP",
        "product_version": "1.0.0",
        "sources": sources,
    }
    return authority, identities, manifests, catalogs, provider_facts


def _receipt(
    **fixture_options: object,
) -> tuple[dict[str, Any], bytes, dict[str, Any], dict[str, dict[str, Any]]]:
    authority, identities, manifests, catalogs, provider_facts = _fixture(
        **fixture_options
    )
    receipt, receipt_bytes = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        (),
    )
    provider_generations = {
        str(row["provider_id"]): copy.deepcopy(row)
        for row in receipt["provider_generations"]
    }
    return receipt, receipt_bytes, authority, provider_generations


@pytest.mark.skipif(
    not all(path.is_file() for path in P123_RETAINED_INPUTS),
    reason="retained P123 EU provider evidence is not mounted",
)
def test_retained_p123_receipt_admits_managed_returns_only() -> None:
    (
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        provider_returns,
    ) = _retained_p123_admit_or_declare_fixture()

    receipt, _ = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        provider_returns,
    )

    p123 = [
        row for row in receipt["dispositions"] if row["provider_id"] == "portfolio123"
    ]
    managed = next(
        row
        for row in p123
        if row["item_id"] == "tables/har_managed_performance.parquet"
    )
    equity = next(
        row for row in p123 if row["item_id"] == "tables/har_equity_curve.parquet"
    )
    policy = authority["sources"]["portfolio123-model-generation"]["admission"][
        "dataset_disposition_policy"
    ]

    assert receipt["admission_gate_status"] == "PASS"
    assert authority["sources"]["portfolio123-model-generation"]["readiness"] == (
        "REQUIRED_NOT_READY"
    )
    assert managed["action"] == "ADMIT"
    assert managed["item_kind"] == "ARTIFACT"
    assert managed["item_sha256"] == P123_MANAGED_PERFORMANCE_SHA256
    assert managed["source_segment_id"] == "portfolio123-model-generation"
    assert managed["evidence_layer"] == "PROVIDER_LIVE"
    assert managed["transform"] == "provider-ledger-portfolio123-evidence"
    assert managed["materialized_record_count"] == 196
    assert managed["observed_start"] == "2025-11-13"
    assert managed["observed_end"] == "2026-08-13"
    assert managed["target_facts"] == ["provider_strategy_returns"]
    assert equity["action"] == "EXCLUDE"
    assert equity["item_kind"] == "ARTIFACT"
    assert equity["item_sha256"] == P123_EQUITY_CURVE_SHA256
    assert equity["source_segment_id"] == "portfolio123-model-generation"
    assert equity["materialized_record_count"] == 0
    assert policy["admitted_item_count"] == 2
    assert sum(row["action"] == "ADMIT" for row in p123) == 2
    assert policy["admitted_item_set_sha256"] == (
        "b1fa77780c774e1d3ede18bf9698e00137447527c787aa30fdd62b5b8ad67643"
    )
    assert (
        policy["upstream_inventory_sha256"]
        == "3f0d3b5bd6239b6743f739226d97cde9979e6295291cdcbb694210d710b31e5f"
    )
    assert policy["product_data_class_count"] == 1
    assert policy["product_data_class_set_sha256"] == (
        "211c807f708e2fea19065c194963775428475a7616d4b96074e21f69c0c95fcf"
    )
    assert authority["sources"]["portfolio123-model-generation"]["evidence_layer"] == (
        "PROVIDER_LIVE"
    )
    assert authority["sources"]["portfolio123-model-generation"]["readiness"] == (
        "REQUIRED_NOT_READY"
    )
    identity = next(item for item in identities if item.provider_id == "portfolio123")
    refreshed = ledger._refresh_strategy_evidence(
        provider_returns,
        [
            {
                "strategy_id": "p123-eu",
                "strategy_name": "P123 EU",
                "provider": {
                    "provider_id": "portfolio123",
                    "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
                },
            }
        ],
        {"p123-eu": "P123 EU"},
        {"portfolio123": identity},
        {"portfolio123": DAY},
    )
    assert ledger._admissions({}, refreshed)[0].disposition.value == (
        "RECONCILIATION_ONLY"
    )


def _rehash_receipt(receipt: dict[str, Any]) -> None:
    receipt["inventory_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipt["inventory"])
    ).hexdigest()
    receipt["disposition_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipt["dispositions"])
    ).hexdigest()


def test_admit_or_declare_receipt_is_exhaustive_hash_bound_and_value_free() -> None:
    receipt, receipt_bytes, authority, provider_generations = _receipt()

    assert receipt["admission_gate_status"] == "PASS"
    assert len(receipt["inventory"]) == len(receipt["dispositions"])
    inventory_keys = {
        (row["provider_id"], row["item_kind"], row["item_id"])
        for row in receipt["inventory"]
    }
    disposition_keys = {
        (row["provider_id"], row["item_kind"], row["item_id"])
        for row in receipt["dispositions"]
    }
    assert inventory_keys == disposition_keys
    assert (
        receipt["inventory_sha256"]
        == hashlib.sha256(canonical_json_bytes(receipt["inventory"])).hexdigest()
    )
    assert (
        receipt["disposition_sha256"]
        == hashlib.sha256(canonical_json_bytes(receipt["dispositions"])).hexdigest()
    )
    assert b"private-account" not in receipt_bytes
    assert b"private-record" not in receipt_bytes
    assert b'"nav":"' not in receipt_bytes
    _, dispositions = ledger.validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=authority["sources"],
        provider_generations=provider_generations,
    )
    admitted = [item for item in dispositions if item.action.value == "ADMIT"]
    assert admitted
    assert all(item.target_facts for item in admitted)
    assert all("source_artifact" not in item.target_facts for item in admitted)

    original_count, original_digest = ledger._admitted_item_set_digest(admitted)
    rewritten = [*admitted]
    rewritten[0] = replace(rewritten[0], target_facts=("transactions",))
    changed_count, changed_digest = ledger._admitted_item_set_digest(rewritten)
    assert changed_count == original_count
    assert changed_digest != original_digest

    schema = json.loads(
        (
            ROOT / "schemas" / "portfolio" / "provider-admission-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert ledger._financial_mvp_module().schema_errors(schema, receipt, schema) == []


def test_receipt_consumer_rejects_coherent_provider_authority_rewrite() -> None:
    receipt, _, authority, provider_generations = _receipt()
    rewritten = json.loads(json.dumps(receipt))
    rewritten["dispositions"][0]["source_observed_start"] = "2026-08-13"
    rewritten["disposition_sha256"] = hashlib.sha256(
        canonical_json_bytes(rewritten["dispositions"])
    ).hexdigest()

    with pytest.raises(
        ProviderAdmissionError,
        match="authority differs from provider generation",
    ):
        ledger.validate_admit_or_declare_receipt(
            rewritten,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )


@pytest.mark.parametrize(
    "attack",
    (
        "SOURCE_SWAP",
        "EVIDENCE_LAYER",
        "POLICY_OWNER",
        "POLICY_INTERVAL",
        "UNKNOWN_PROVIDER",
        "CATALOG_SHA",
        "ADAPTER_NAME",
        "QUALIFICATION",
        "CONTRACT_SEMANTIC",
        "CONTRACT_RAW",
        "CENSUS_SHA",
        "PRODUCT_VERSION",
    ),
)
def test_receipt_consumer_rejects_coherent_authority_attacks(attack: str) -> None:
    receipt, _, authority, provider_generations = _receipt()
    rewritten = copy.deepcopy(receipt)
    contract_digest = rewritten["contract_semantic_sha256"]
    if attack == "SOURCE_SWAP":
        provider_rows = {
            row["provider_id"]: row for row in rewritten["provider_generations"]
        }
        left = provider_rows["ibkr"]
        right = provider_rows["schwab"]
        for field in ("source_segment_id", "source_segment_sha256"):
            left[field], right[field] = right[field], left[field]
        for row in rewritten["dispositions"]:
            if row["provider_id"] in {"ibkr", "schwab"}:
                provider_row = provider_rows[row["provider_id"]]
                row["source_segment_id"] = provider_row["source_segment_id"]
                row["source_segment_sha256"] = provider_row["source_segment_sha256"]
                row["authority_ref"] = (
                    f"contract:{contract_digest}#source_segments/"
                    f"{provider_row['source_segment_id']};issue:303"
                )
    elif attack == "EVIDENCE_LAYER":
        for row in rewritten["dispositions"]:
            if row["provider_id"] == "ibkr":
                row["evidence_layer"] = "PROVIDER_LIVE"
    elif attack == "POLICY_OWNER":
        rewritten["dispositions"][0]["policy_owner"] = "UNAUTHORIZED_OWNER"
    elif attack == "POLICY_INTERVAL":
        rewritten["dispositions"][0]["policy_effective_start"] = "2026-08-01"
    elif attack == "UNKNOWN_PROVIDER":
        for row in rewritten["provider_generations"]:
            if row["provider_id"] == "alpaca":
                row["provider_id"] = "unknown-provider"
        for key in ("inventory", "dispositions"):
            for row in rewritten[key]:
                if row["provider_id"] == "alpaca":
                    row["provider_id"] = "unknown-provider"
    elif attack in {"CATALOG_SHA", "ADAPTER_NAME", "QUALIFICATION"}:
        provider_row = next(
            row
            for row in rewritten["provider_generations"]
            if row["provider_id"] == "ibkr"
        )
        if attack == "CATALOG_SHA":
            provider_row["catalog_sha256"] = "d" * 64
        elif attack == "ADAPTER_NAME":
            provider_row["adapter_name"] = "unauthorized-adapter"
        else:
            provider_row["qualification"] = "ACCEPTED_PARTIAL"
    elif attack == "CONTRACT_SEMANTIC":
        rewritten["contract_semantic_sha256"] = "d" * 64
        for row in rewritten["dispositions"]:
            row["authority_ref"] = row["authority_ref"].replace(
                contract_digest, "d" * 64
            )
    elif attack == "CONTRACT_RAW":
        rewritten["contract_raw_sha256"] = "d" * 64
    elif attack == "CENSUS_SHA":
        rewritten["census_sha256"] = "d" * 64
    else:
        rewritten["product_version"] = "999.0.0"
    _rehash_receipt(rewritten)

    with pytest.raises(ProviderAdmissionError):
        ledger.validate_admit_or_declare_receipt(
            rewritten,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("readiness_impact", "REQUIRED_NOT_READY"),
        ("data_class", "UNAUTHORIZED_DATA_CLASS"),
        ("item_sha256", "d" * 64),
        ("materialized_sha256", "d" * 64),
        ("materialized_record_count", 999),
        ("observed_start", "2026-08-13"),
        ("observed_end", "2026-08-15"),
    ),
)
def test_receipt_rejects_coherent_admitted_identity_rewrite(
    field: str, value: object
) -> None:
    receipt, _, authority, provider_generations = _receipt()
    rewritten = copy.deepcopy(receipt)
    admitted = next(
        row for row in rewritten["dispositions"] if row["action"] == "ADMIT"
    )
    admitted[field] = value
    _rehash_receipt(rewritten)

    with pytest.raises(ProviderAdmissionError):
        ledger.validate_admit_or_declare_receipt(
            rewritten,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )


def test_receipt_rejects_coherent_excluded_product_class_sha_rewrite() -> None:
    receipt, _, authority, provider_generations = _receipt(missing_product_class=True)
    rewritten = copy.deepcopy(receipt)
    excluded = next(
        row
        for row in rewritten["dispositions"]
        if row["action"] == "EXCLUDE" and row["item_kind"] == "PRODUCT_DATA_CLASS"
    )
    inventory = next(
        row
        for row in rewritten["inventory"]
        if row["provider_id"] == excluded["provider_id"]
        and row["item_kind"] == excluded["item_kind"]
        and row["item_id"] == excluded["item_id"]
    )
    inventory["sha256"] = "d" * 64
    excluded["item_sha256"] = "d" * 64
    _rehash_receipt(rewritten)

    with pytest.raises(ProviderAdmissionError):
        ledger.validate_admit_or_declare_receipt(
            rewritten,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )


def test_receipt_rejects_coherent_excluded_product_class_field_rewrite() -> None:
    receipt, _, authority, provider_generations = _receipt(missing_product_class=True)
    rewritten = copy.deepcopy(receipt)
    excluded = next(
        row
        for row in rewritten["dispositions"]
        if row["action"] == "EXCLUDE" and row["item_kind"] == "PRODUCT_DATA_CLASS"
    )
    excluded["data_class"] = "UNAUTHORIZED_DATA_CLASS"
    _rehash_receipt(rewritten)

    with pytest.raises(ProviderAdmissionError):
        ledger.validate_admit_or_declare_receipt(
            rewritten,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations=provider_generations,
        )


def test_new_provider_dataset_fails_before_receipt() -> None:
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="schwab upstream dataset inventory differs from authority",
    ):
        _receipt(extra_dataset=True)


def test_bounded_policy_interval_covering_generation_is_accepted() -> None:
    receipt, _, authority, provider_generations = _receipt(
        policy_effective_start=date(2026, 8, 1),
        policy_effective_end=date(2026, 8, 31),
    )
    ledger.validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=authority["sources"],
        provider_generations=provider_generations,
    )


@pytest.mark.parametrize(
    "start,end",
    (
        (date(2026, 8, 15), None),
        (date(2026, 8, 1), date(2026, 8, 13)),
    ),
)
def test_policy_interval_not_covering_generation_fails_before_receipt(
    start: date, end: date | None
) -> None:
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="dataset disposition policy does not cover generation",
    ):
        _receipt(policy_effective_start=start, policy_effective_end=end)


def test_evidence_layer_conflict_blocks_the_product_data_class() -> None:
    with pytest.raises(
        ledger.ProviderLedgerError,
        match="quantbase dataset exclusion is not authorized",
    ):
        _receipt(quantbase_layer="PROVIDER_LIVE")


def test_unknown_product_data_class_fails_before_receipt() -> None:
    authority, identities, manifests, catalogs, provider_facts = _fixture()
    source = authority["sources"]["schwab-account-generation"]
    source["data_classes"].append("UNAUTHORIZED_NEW_DATA_CLASS")

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="schwab product data-class set differs from authority",
    ):
        ledger._admit_or_declare_receipt(
            authority,
            identities,
            manifests,
            catalogs,
            provider_facts,
            (),
        )


def test_product_data_class_requires_matching_admission_fact() -> None:
    authority, identities, manifests, catalogs, provider_facts = _fixture()
    source = authority["sources"]["schwab-account-generation"]
    source["admission"]["facts"] = ["UNRELATED_FACT"]

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="schwab product data classes differ from admission facts",
    ):
        ledger._admit_or_declare_receipt(
            authority,
            identities,
            manifests,
            catalogs,
            provider_facts,
            (),
        )

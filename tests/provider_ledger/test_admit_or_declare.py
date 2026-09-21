from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
import hashlib
import importlib.util
import json
import os
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
    readiness_by_reason: dict[str, str] | None = None,
    unreferenced_dataset: bool = False,
    native_source_inventory: bool = False,
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
                    **(
                        {}
                        if readiness_by_reason is None
                        else {
                            "exclusion_readiness_impact_by_reason": dict(
                                readiness_by_reason
                            )
                        }
                    ),
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
        if native_source_inventory:
            # The native-format fixture also enumerates empty account fact
            # families. Declare those families in this synthetic authority;
            # its only required product class remains ACCOUNT_NAV.
            sources[source_id]["admission"]["facts"] = sorted(
                {
                    "NAV_OBSERVATION",
                    *(
                        ledger._TARGET_ADMISSION_FACT[
                            "flows" if fact == "cash_flows" else fact
                        ]
                        for fact in ledger._ACCOUNT_FACTS[provider_id]
                    ),
                }
            )
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
        if unreferenced_dataset and provider_id == "schwab":
            catalog["unreferenced_collection"] = [{"synthetic": True}]
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


def _staged_observation(
    tmp_path: Path,
    identities: list[ProviderGenerationIdentity],
    manifests: dict[str, GenerationManifest],
    catalogs: dict[str, dict[str, Any]],
    *,
    cutoff: date = DAY,
    schwab_point_capture: str | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Small synthetic sources using native formats, no asserted finality flag."""
    from datetime import datetime, timedelta, timezone
    from quant_engine.core.publication import CurrentPointer

    refs, selections = {}, {}
    for index, identity in enumerate(list(identities)):
        provider_id = identity.provider_id
        generation_day = (
            datetime.fromisoformat(schwab_point_capture).date()
            if provider_id == "schwab" and schwab_point_capture is not None
            else max(DAY, cutoff)
        )
        root = tmp_path / provider_id
        root.mkdir(parents=True)
        catalog_name, versions = ledger._PROVIDER_CATALOGS[provider_id]
        catalog = {**catalogs[provider_id], "catalog_schema_version": min(versions)}
        catalog.pop("nav_observations", None)
        artifacts = list(manifests[provider_id].artifacts)
        for artifact in artifacts:
            source_path = root / artifact.artifact_id
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(f"synthetic-{provider_id}".encode())
        for fact_name in ledger._ACCOUNT_FACTS[provider_id]:
            catalog.setdefault(fact_name, [])
        evidence_path = root / "finality.json"
        account = "synthetic-" + provider_id
        nav_artifact = "native/nav.csv"

        def bind(
            artifact_id: str,
            path: Path,
            content: bytes,
            schema: str = "synthetic-source",
            version: str = "1",
        ) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            artifacts.append(
                SourceArtifact(
                    artifact_id=artifact_id,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    as_of_date=generation_day,
                    schema_name=schema,
                    schema_version=version,
                )
            )

        def nav(artifact_id: str) -> dict[str, Any]:
            return {
                "account_id": account,
                "as_of_date": cutoff.isoformat(),
                "record_id": "synthetic-nav",
                "source_artifact_id": artifact_id,
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
                "value_basis": "ACCOUNT_VALUE"
                if provider_id == "schwab"
                else "ACCOUNT_TOTAL",
            }

        if provider_id == "ibkr":
            native_day = cutoff.strftime("%Y%m%d")
            xml = (
                '<FlexQueryResponse type="AF" queryName="SYNTHETIC"><FlexStatements>'
                f'<FlexStatement accountId="SYNTHETIC" fromDate="{native_day}" toDate="{native_day}" whenGenerated="{generation_day:%Y%m%d};120000">'
                '<AccountInformation accountId="SYNTHETIC" currency="USD"/>'
                f'<EquitySummaryByReportDateInBase reportDate="{native_day}" total="100"/>'
                '<ChangeInNAV endingValue="100"/><CashTransactions/><ConversionRates/>'
                "<CorporateActions/><OpenPositions/><Trades/><Transfers/>"
                "</FlexStatement></FlexStatements></FlexQueryResponse>"
            ).encode()
            evidence_path = root / "statement.xml"
            bind("native/current.xml", evidence_path, xml, "activity-flex")
            catalog.update(
                accounts=[{"account_id": account}],
                nav_observations=[nav("native/current.xml")],
                fact_coverage=[
                    {
                        "fact_type": "FLEX_FINALIZED_HISTORY",
                        "calendar": "WEEKDAY_SOURCE_CALENDAR",
                        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                        "known_gaps": [],
                        "accounts": [
                            {
                                "account_id": account,
                                "statement_count": 1,
                                "intervals": [
                                    {
                                        "effective_start": cutoff.isoformat(),
                                        "effective_end": cutoff.isoformat(),
                                        "source_artifact_ids": ["native/current.xml"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            )
        elif provider_id == "matrixport":
            run_id = "synthetic-current"
            capture = datetime.combine(
                cutoff + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
            )
            summary = {
                "run_id": run_id,
                "status": "COMPLETED",
                "run_end_ms": int(capture.timestamp() * 1000),
            }
            evidence_path = root / "output" / run_id / "reports/extraction_summary.json"
            bind(
                "reports/extraction_summary.json",
                evidence_path,
                canonical_json_bytes(summary),
            )
            nav_time = capture - timedelta(days=1)
            bind(
                "csv/nav_daily.csv",
                root / "output" / run_id / "csv/nav_daily.csv",
                (
                    "day_timestamp_ms,day_timestamp_utc,product_id\n"
                    f"{int(nav_time.timestamp() * 1000)},{nav_time.isoformat()},SYNTHETIC\n"
                ).encode(),
            )
            prior = (cutoff - timedelta(days=1)).isoformat()
            last = cutoff.isoformat()
            contents = {
                "csv/daily_portfolio_totals.csv": "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,strict_complete\n"
                + f"{prior},100,100,true\n{last},100,100,true\n",
                "csv/daily_holdings.csv": "utc_date,product_id,share,currency,native_value,valuation_source,value_usd_api_rate,value_usd_api_reported,value_usd_strict_component\n"
                + f"{prior},P1,1,USD,100,api_reported_usd_value,1,100,100\n{last},P1,1,USD,100,api_reported_usd_value,1,100,100\n",
                "csv/valuation_quality_issues.csv": "source_index,source_table,reason\n",
                "csv/subscription_details.csv": "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,subscription_amount,subscription_currency,subscription_time_utc\n"
                + f"100,{prior}T00:00:00Z,{ledger._MATRIXPORT_ROBUXIO_ORDER_ID},{ledger._MATRIXPORT_ROBUXIO_PRODUCT_ID},100,USD,{prior}T00:00:00Z\n",
            }
            for artifact_id, text in contents.items():
                bind(artifact_id, root / "output" / run_id / artifact_id, text.encode())
            catalog.update(
                source_run_id=run_id, observed_boundary_date=cutoff.isoformat()
            )
        elif provider_id == "quantbase":
            run_id = "synthetic-current"
            relative = "csv/dashboard_Volatis_Strategy_pricehistory.csv"
            content = (
                "time,value,cost_basis\n"
                + cutoff.strftime("%m/%d/%Y")
                + ",100,100\n"
                + (cutoff + timedelta(days=1)).strftime("%m/%d/%Y")
                + ",999,100\n"
            ).encode()
            bind(
                "source/" + relative,
                root / "source-runs" / run_id / "artifacts" / relative,
                content,
            )
            original = root / "conversion/artifacts" / relative
            original.parent.mkdir(parents=True)
            original.write_bytes(content)
            evidence_path = root / "conversion/conversion-receipt.json"
            evidence_path.write_bytes(
                canonical_json_bytes(
                    {
                        "refreshed_datasets": [Path(relative).name],
                        "preserved_companions": [],
                        "latest_complete_daily_cutoff": cutoff.isoformat(),
                        "intraday_unfinalized_dates": [
                            (cutoff + timedelta(days=1)).isoformat()
                        ],
                    }
                )
            )
            activity = (
                "activity_type,amount,fund,is_rebalance,time,type,amount_cents,created_at,destination_account_id,id,origin_account_id,status\n"
                + f"filled_txn,100,107,False,{cutoff.isoformat()}T00:00:00Z,BUY,10000,,,synthetic-buy,,C\n"
            )
            activity_relative = "csv/account_activity.csv"
            bind(
                "source/" + activity_relative,
                root / "source-runs" / run_id / "artifacts" / activity_relative,
                activity.encode(),
            )
            (root / "conversion/artifacts" / activity_relative).write_bytes(
                activity.encode()
            )
            conversion = json.loads(evidence_path.read_bytes())
            conversion["refreshed_datasets"].append("account_activity.csv")
            evidence_path.write_bytes(canonical_json_bytes(conversion))
            catalog.update(
                legacy_lineage={"run_id": run_id},
                datasets=[
                    {
                        "relative_path": rel,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                    }
                    for rel, raw in (
                        (relative, content),
                        (activity_relative, activity.encode()),
                    )
                ],
            )
        elif provider_id == "schwab":
            content = f"date,account_value\n{cutoff.isoformat()},100\n".encode()
            bind(
                nav_artifact,
                root / nav_artifact,
                content,
                "schwab-wch-historical-balance",
            )
            catalog.update(
                accounts=[{"account_id": account}], nav_observations=[nav(nav_artifact)]
            )
            if schwab_point_capture is not None:
                from quant_engine.schwab import (
                    read_holdings_v2_current_snapshot,
                    write_current_snapshot,
                )

                snapshot = read_holdings_v2_current_snapshot(
                    {
                        "accounts": [
                            {
                                "accountId": "SYNTHETIC-001",
                                "groupedPositions": [
                                    {
                                        "groupName": "Equity",
                                        "securityType": 1,
                                        "holdingsRows": [
                                            {
                                                "dataSymbol": "SYNTH",
                                                "qty": {"qty": "3"},
                                                "marketValue": {"val": "99"},
                                                "costBasis": {"cstBasis": "27"},
                                            }
                                        ],
                                    }
                                ],
                            },
                            {"accountId": "SYNTHETIC-002", "groupedPositions": []},
                        ],
                        "accountTotals": [],
                    },
                    crosswalk={
                        "SYNTHETIC-001": account,
                        "SYNTHETIC-002": "synthetic-schwab-empty",
                    },
                    captured_at=schwab_point_capture,
                )
                snapshot_path = root / "snapshot.json"
                digest, size = write_current_snapshot(snapshot, snapshot_path)
                document = json.loads(snapshot_path.read_bytes())
                point_artifact = SourceArtifact(
                    artifact_id="web/api-current-snapshot.json",
                    sha256=digest,
                    size_bytes=size,
                    as_of_date=generation_day,
                    schema_name="schwab-api-current-snapshot",
                    schema_version="2",
                )
                artifacts.append(point_artifact)
                catalog.update(
                    catalog_schema_version=2,
                    as_of_date=generation_day.isoformat(),
                    accounts=[
                        {"account_id": row["account_id"], "base_currency": "USD"}
                        for row in document["accounts"]
                    ],
                    current_accounts=document["accounts"],
                    positions=document["positions"],
                    current_transport="WEB_CONTRACT",
                    current_position_account_count=1,
                    position_snapshot_as_of={"observed_at": schwab_point_capture},
                    source_artifacts=[point_artifact.to_dict()],
                )
                bind(
                    "native/empty-nav.csv",
                    root / "native/empty-nav.csv",
                    content,
                    "schwab-wch-historical-balance",
                )
                catalog["nav_observations"].append(
                    {
                        **nav("native/empty-nav.csv"),
                        "account_id": "synthetic-schwab-empty",
                        "record_id": "synthetic-empty-nav",
                    }
                )
            evidence_path.write_bytes(
                canonical_json_bytes(
                    {
                        "as_of_date": generation_day.isoformat(),
                        "roots": {"native": str(root)},
                        "sources": [
                            {
                                "role": "WCH_NAV_CSV",
                                "artifact_id": nav_artifact,
                                "account_id": account,
                                "root_id": "native",
                                "relative_path": nav_artifact,
                                "expected_sha256": hashlib.sha256(content).hexdigest(),
                                "expected_size_bytes": len(content),
                            }
                        ]
                        + (
                            [
                                {
                                    "role": "WCH_NAV_CSV",
                                    "artifact_id": "native/empty-nav.csv",
                                    "account_id": "synthetic-schwab-empty",
                                    "root_id": "native",
                                    "relative_path": "native/empty-nav.csv",
                                    "expected_sha256": hashlib.sha256(
                                        content
                                    ).hexdigest(),
                                    "expected_size_bytes": len(content),
                                }
                            ]
                            if schwab_point_capture is not None
                            else []
                        ),
                    }
                )
            )
        catalog_bytes = canonical_json_bytes(catalog)
        bind(
            catalog_name,
            root / catalog_name,
            catalog_bytes,
            provider_id + "-generation-catalog",
            str(catalog["catalog_schema_version"])
            if provider_id == "schwab" and schwab_point_capture is not None
            else "1",
        )
        qualification = {
            "alpaca": "PUBLISHED_COMPLETE",
            "fidelity": "FINITE_STATIC",
            "ibkr": "ACCEPTED_PARTIAL",
            "matrixport": "ACCEPTED_PARTIAL",
            "portfolio123": "STAGED_COMPLETE",
            "quantbase": "STAGED_COMPLETE",
            "schwab": "STAGED_COMPLETE",
        }[provider_id]
        coverage = replace(
            manifests[provider_id].coverage,
            observed_end=generation_day,
            fact_types=ledger._ACCOUNT_FACTS[provider_id] or ("model_evidence",),
            status=CoverageStatus.PARTIAL
            if qualification == "ACCEPTED_PARTIAL"
            else CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            known_gaps=("synthetic historical gap",)
            if qualification == "ACCEPTED_PARTIAL"
            else (),
        )
        manifest = GenerationManifest.build(
            as_of_date=generation_day,
            adapter=(
                AdapterIdentity("quant-engine-schwab", "1")
                if provider_id == "schwab" and schwab_point_capture is not None
                else manifests[provider_id].adapter
            ),
            coverage=coverage,
            artifacts=artifacts,
            verification=VerificationStatus.VERIFIED,
        )
        generation = root / "generations" / manifest.generation_id
        generation.mkdir(parents=True)
        manifest_bytes = manifest.to_json_bytes()
        (generation / "generation.json").write_bytes(manifest_bytes)
        (generation / catalog_name).write_bytes(catalog_bytes)
        if provider_id in {"alpaca", "fidelity", "portfolio123"}:
            evidence_path = generation / "generation.json"
        if qualification == "PUBLISHED_COMPLETE":
            # Use the existing pointer constructor, same as provider publication.
            (root / "CURRENT.json").write_bytes(
                CurrentPointer(
                    manifest.generation_id, hashlib.sha256(manifest_bytes).hexdigest()
                ).to_json_bytes()
            )
        ref = {
            "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            "generation_id": manifest.generation_id,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "qualification": qualification,
            "root": str(root),
        }
        refs[provider_id] = ref
        evidence_bytes = evidence_path.read_bytes()
        selections[provider_id] = {
            **ref,
            "finality_evidence": {
                "path": str(evidence_path),
                "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "size_bytes": len(evidence_bytes),
            },
            "finalized_through": cutoff.isoformat(),
            "generation_as_of_date": generation_day.isoformat(),
        }
        manifests[provider_id] = manifest
        catalogs[provider_id] = catalog
        identities[index] = ProviderGenerationIdentity(
            provider_id=provider_id,
            generation_id=manifest.generation_id,
            manifest_sha256=ref["manifest_sha256"],
            catalog_sha256=ref["catalog_sha256"],
            qualification=GenerationQualification(qualification),
        )
    return refs, selections


def _source_bound_stage(
    tmp_path: Path,
    *,
    source_cutoff: date = DAY,
    schwab_point_capture: str | None = None,
    requested_as_of: date = date(2026, 9, 17),
) -> tuple[Path, dict[str, Any], Path]:
    """Real stage, real financial contract/schema/registry, tiny synthetic inputs."""
    _, identities, manifests, catalogs, _ = _fixture(unreferenced_dataset=True)
    refs, selections = _staged_observation(
        tmp_path / "sources",
        identities,
        manifests,
        catalogs,
        cutoff=source_cutoff,
        schwab_point_capture=schwab_point_capture,
    )
    contract_path = ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
    schema_path = ROOT / "schemas/portfolio/financial-mvp-source-contract.schema.json"
    registry_path = ROOT / "docs/analytics/current-strategy-decomposition-plan.json"
    financial = {
        key + "_path": str(path)
        for key, path in (
            ("contract", contract_path),
            ("schema", schema_path),
            ("registry", registry_path),
        )
    }
    financial.update(
        {
            key + "_sha256": hashlib.sha256(
                Path(financial[key + "_path"]).read_bytes()
            ).hexdigest()
            for key in ("contract", "schema", "registry")
        }
    )
    observation = {
        "schema_version": 1,
        "captured_at_utc": "2026-09-18T12:00:00Z",
        "financial_cutoff": requested_as_of.isoformat(),
        "policy_contract_sha256": financial["contract_sha256"],
        "providers": selections,
    }

    def write(name: str, value: object) -> tuple[Path, str]:
        path = tmp_path / name
        content = canonical_json_bytes(value)
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    observation_path, observation_sha = write("observation.json", observation)
    contract = json.loads(contract_path.read_bytes())
    strategies = [
        {"strategy_id": row["strategy_id"], "name": row["strategy_id"]}
        for row in contract["strategies"]
    ]
    provider_map = {
        "p123-ai-factor": "schwab",
        "p123-eu": "ibkr",
        "qsi": "schwab",
        "tt-portfolio": "schwab",
        "volatis-strategy": "quantbase",
        "grizzlybears-vtma": "schwab",
        "robuxio-crypto": "matrixport",
    }
    provenance_path, provenance_sha = write(
        "provenance.json",
        {
            "as_of_date": DAY.isoformat(),
            "accounts": [
                {"account_id": account}
                for account in (
                    ledger._CSV_FACT_ACCOUNT["matrixport"],
                    ledger._CSV_FACT_ACCOUNT["quantbase"],
                    "synthetic-ibkr",
                    "synthetic-schwab",
                )
            ],
            "strategies": strategies,
            "assignments": [
                {
                    "account_id": ledger._CSV_FACT_ACCOUNT[provider_id],
                    "strategy_id": strategy_id,
                    "mapping_status": "MAPPED",
                    "effective_start": "2025-12-06",
                    "source_classification": "financial_mvp_funded_membership_reconciled",
                }
                for provider_id, strategy_id in (
                    ("matrixport", "robuxio-crypto"),
                    ("quantbase", "volatis-strategy"),
                )
            ],
        },
    )
    evidence = []
    for strategy_id in ledger._provider_native_account_return_strategy_ids(contract):
        provider_id = provider_map[strategy_id]
        artifact = next(
            a
            for a in manifests[provider_id].artifacts
            if a.artifact_id == ledger._PROVIDER_CATALOGS[provider_id][0]
        )
        evidence.append(
            {
                "strategy_id": strategy_id,
                "provider_id": provider_id,
                "generation_id": manifests[provider_id].generation_id,
                "sources": {
                    "native": {
                        "artifact_id": artifact.artifact_id,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                },
            }
        )
    evidence_path, evidence_sha = write(
        "strategy-evidence.json",
        {"as_of_date": DAY.isoformat(), "strategies": evidence},
    )
    returns_path = tmp_path / "returns.csv"
    returns_path.write_text(
        "series_id,date,return,evidence_layer\nrobuxio-crypto,2026-08-14,0,BACKTEST\n",
        encoding="utf-8",
    )
    spec_path, _ = write(
        "spec.json",
        {
            "schema_version": 2,
            "as_of_date": requested_as_of.isoformat(),
            "financial_mvp": financial,
            "output_root": str(tmp_path / "staged"),
            "providers": refs,
            "provenance": {
                "catalog_path": str(provenance_path),
                "catalog_sha256": provenance_sha,
                "generation_id": "1" * 64,
            },
            "strategy_inputs": {
                "returns_path": str(returns_path),
                "returns_sha256": hashlib.sha256(returns_path.read_bytes()).hexdigest(),
                "strategy_evidence_path": str(evidence_path),
                "strategy_evidence_sha256": evidence_sha,
            },
            "strategy_provider_map": provider_map,
            "run_observation": {
                "path": str(observation_path),
                "sha256": observation_sha,
                "size_bytes": observation_path.stat().st_size,
            },
        },
    )
    authority = ledger._financial_mvp_authority(ledger._load_spec(spec_path))
    staged = ledger.stage_candidate(spec_path)
    return (
        tmp_path / "staged/generations" / str(staged["generation_id"]),
        authority,
        spec_path,
    )


def test_source_bound_stage_runs_without_replacing_validators(tmp_path: Path) -> None:
    policy_path = ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
    policy_before = policy_path.read_bytes()
    generation, authority, spec_path = _source_bound_stage(tmp_path)
    catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    receipt = json.loads((generation / "admit-or-declare-receipt.json").read_bytes())
    assert len(receipt["provider_generations"]) == 7
    assert len(receipt["inventory"]) == len(receipt["dispositions"])
    assert {
        row["nav"]["value"]
        for row in catalog["nav_observations"]
        if row["provider_id"] == "matrixport"
    } == {"100"}
    assert {
        (row["strategy_id"], row["return"])
        for row in catalog["strategy_returns"]
        if row["evidence_layer"] == "ACCOUNT_LIVE"
    } == {("robuxio-crypto", "0")}
    # Quantbase has one finalized NAV: its opening anchor is valid capital,
    # but only a later finalized NAV can establish a return interval.
    anchor = next(
        row for row in catalog["capital_anchors"] if row["provider_id"] == "quantbase"
    )
    assert (anchor["strategy_id"], anchor["amount"]["value"], anchor["event_date"]) == (
        "volatis-strategy",
        "100",
        DAY.isoformat(),
    )
    assert any(
        row["status"] == "OUTSIDE_FINANCIAL_CUTOFF"
        and row["provider_id"] == "quantbase"
        for row in catalog["quality_findings"]
    )
    assert authority["contract_raw_sha256"] == hashlib.sha256(policy_before).hexdigest()
    assert policy_path.read_bytes() == policy_before
    assert not (tmp_path / "staged/CURRENT.json").exists()
    ledger.verify_candidate(spec_path, generation.name)


@pytest.mark.parametrize(
    ("source_cutoff", "object_mapping", "expected_dates", "excluded_nav_date"),
    [
        (date(2026, 9, 17), False, ["2026-09-16", "2026-09-17"], "2026-09-18"),
        (date(2026, 9, 16), True, ["2026-09-16"], "2026-09-17"),
    ],
)
def test_current_build_keeps_source_bounded_returns_past_frozen_snapshot(
    tmp_path: Path,
    source_cutoff: date,
    object_mapping: bool,
    expected_dates: list[str],
    excluded_nav_date: str,
) -> None:
    """The real builder must forward current bounds to retained and derived rows."""
    import portfolio_pipeline_materialize as materializer

    policy_path = ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
    policy_before = policy_path.read_bytes()
    generation, authority, spec_path = _source_bound_stage(
        tmp_path, source_cutoff=source_cutoff
    )
    spec = json.loads(spec_path.read_bytes())
    provenance_path = Path(spec["provenance"]["catalog_path"])
    provenance = json.loads(provenance_path.read_bytes())
    provenance["assignments"].append(
        {
            "account_id": "synthetic-schwab",
            "strategy_id": "qsi",
            "mapping_status": "MAPPED",
            "effective_start": "2026-01-05",
            "effective_end": None,
        }
    )
    provenance_path.write_bytes(canonical_json_bytes(provenance))
    spec["provenance"]["catalog_sha256"] = hashlib.sha256(
        provenance_path.read_bytes()
    ).hexdigest()
    returns_path = Path(spec["strategy_inputs"]["returns_path"])
    returns_path.write_text(
        "series_id,date,return,evidence_layer\n"
        "qsi,2026-09-16,0.02,ACCOUNT_LIVE\n"
        "qsi,2026-09-17,0.03,ACCOUNT_LIVE\n",
        encoding="utf-8",
    )
    spec["strategy_inputs"]["returns_sha256"] = hashlib.sha256(
        returns_path.read_bytes()
    ).hexdigest()
    if object_mapping:
        spec["strategy_provider_map"] = {
            strategy_id: {"provider_id": provider_id, "evidence_layer": "ACCOUNT_LIVE"}
            for strategy_id, provider_id in spec["strategy_provider_map"].items()
        }
    spec_path.write_bytes(canonical_json_bytes(spec))
    sources_before = {
        path: path.read_bytes()
        for path in (tmp_path / "sources").rglob("*")
        if path.is_file()
    }

    catalog, _, _, _ = ledger.build_catalog(spec_path)

    retained = [
        row for row in catalog["strategy_returns"] if row["strategy_id"] == "qsi"
    ]
    assert [(row["date"], row["return"]) for row in retained] == [
        (day, {"2026-09-16": "0.02", "2026-09-17": "0.03"}[day])
        for day in expected_dates
    ]
    derived = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] == "robuxio-crypto"
    ]
    assert [(row["date"], row["return"]) for row in derived] == [
        (expected_dates[-1], "0")
    ]
    assert all(row["evidence_layer"] == "ACCOUNT_LIVE" for row in retained + derived)
    assert any(
        row["status"] == "OUTSIDE_FINANCIAL_CUTOFF"
        and row["provider_id"] == "quantbase"
        and row["original_source_date"] == excluded_nav_date
        for row in catalog["quality_findings"]
    )
    accepted = materializer._provider_admission_authority(
        generation,
        authority["contract"],
        contract_raw_sha256=authority["contract_raw_sha256"],
        contract_semantic_sha256=authority["contract_semantic_sha256"],
        census_sha256=authority["census_sha256"],
    )
    assert (
        accepted["catalog_sha256"]
        == hashlib.sha256(
            (generation / "provider-ledger.json").read_bytes()
        ).hexdigest()
    )
    assert authority["contract_raw_sha256"] == hashlib.sha256(policy_before).hexdigest()
    assert policy_path.read_bytes() == policy_before
    assert all(path.read_bytes() == content for path, content in sources_before.items())
    assert not (tmp_path / "staged/CURRENT.json").exists()


@pytest.mark.parametrize(
    "mapping",
    [None, "unknown", {"provider_id": "unknown", "evidence_layer": "ACCOUNT_LIVE"}],
)
def test_current_build_rejects_missing_or_unknown_required_provider_mapping(
    tmp_path: Path, mapping: object
) -> None:
    _, _, spec_path = _source_bound_stage(tmp_path)
    spec = json.loads(spec_path.read_bytes())
    if mapping is None:
        del spec["strategy_provider_map"]["qsi"]
    else:
        spec["strategy_provider_map"]["qsi"] = mapping
    spec_path.write_bytes(canonical_json_bytes(spec))
    with pytest.raises(ledger.ProviderLedgerError, match="strategy_provider_map"):
        ledger.build_catalog(spec_path)


def test_finality_requires_selected_native_sources_and_account_scope(
    tmp_path: Path,
) -> None:
    _, identities, manifests, catalogs, _ = _fixture()
    _, selections = _staged_observation(
        tmp_path / "sources", identities, manifests, catalogs
    )
    asserted = tmp_path / "unrelated-finality.json"
    asserted.write_bytes(
        canonical_json_bytes(
            {
                "status": "COMPLETED",
                "as_of_date": DAY.isoformat(),
                "finalized_through": DAY.isoformat(),
                "native_timezone": "UTC",
                "native_calendar": "SYNTHETIC_DAILY",
                "finality_basis": "asserted",
            }
        )
    )
    for provider_id, selection in selections.items():
        interpreted = ledger._validate_observed_finality_evidence(
            provider_id, selection
        )
        assert interpreted["provider_finality_guarantee"] is False
        if provider_id != "matrixport":
            assert interpreted["native_timezone"] is None
        substituted = {
            **selection,
            "finality_evidence": {
                "path": str(asserted),
                "sha256": hashlib.sha256(asserted.read_bytes()).hexdigest(),
                "size_bytes": asserted.stat().st_size,
            },
        }
        with pytest.raises(ledger.ProviderLedgerError):
            ledger._validate_observed_finality_evidence(provider_id, substituted)
    selected = selections["schwab"]
    source_spec = json.loads(Path(selected["finality_evidence"]["path"]).read_bytes())
    source_spec["sources"][0]["account_id"] = "synthetic-unselected-account"
    asserted.write_bytes(canonical_json_bytes(source_spec))
    substituted = {
        **selected,
        "finality_evidence": {
            "path": str(asserted),
            "sha256": hashlib.sha256(asserted.read_bytes()).hexdigest(),
            "size_bytes": asserted.stat().st_size,
        },
    }
    with pytest.raises(ledger.ProviderLedgerError, match="account scope differs"):
        ledger._validate_observed_finality_evidence("schwab", substituted)


def test_staged_current_observation_rebuilds_receipt_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real stage/verify producer seam with existing synthetic inputs.

    The fixture supplies only source-bound provider facts; the test keeps the
    owner-policy source objects byte-identical while the producer independently
    derives the observed inventory and disposition digests.
    """

    authority, identities, manifests, catalogs, provider_facts = _fixture(
        unreferenced_dataset=True, native_source_inventory=True
    )
    authority["contract"]["strategies"] = [
        {
            "product_requirement": "REQUIRED",
            "strategy_id": "synthetic-required",
        }
    ]
    refs, selections = _staged_observation(
        tmp_path / "sources", identities, manifests, catalogs
    )
    observation = {
        "captured_at_utc": "2026-08-14T12:00:00Z",
        "financial_cutoff": DAY.isoformat(),
        "policy_contract_sha256": authority["contract_raw_sha256"],
        "providers": selections,
        "schema_version": 1,
    }
    observation_bytes = canonical_json_bytes(observation)
    observation_path = tmp_path / "run-observation.json"
    observation_path.write_bytes(observation_bytes)
    authority["run_observation"] = observation
    authority["run_observation_path"] = str(tmp_path / "run-observation.json")
    authority["run_observation_size_bytes"] = len(observation_bytes)
    (tmp_path / "run-observation.json").write_bytes(observation_bytes)
    authority["run_observation_sha256"] = hashlib.sha256(observation_bytes).hexdigest()
    policy_before = canonical_json_bytes(authority["sources"])
    spec = {
        "as_of_date": DAY.isoformat(),
        "financial_mvp": {},
        "output_root": str(tmp_path / "staged"),
        "provenance": {},
        "providers": refs,
        "run_observation": {
            "path": str(observation_path),
            "sha256": authority["run_observation_sha256"],
            "size_bytes": len(observation_bytes),
        },
        "schema_version": 2,
        "strategy_inputs": {},
        "strategy_provider_map": {"synthetic-required": "ibkr"},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(canonical_json_bytes(spec))
    provenance_artifact = SourceArtifact(
        artifact_id="synthetic/provenance.json",
        sha256="a" * 64,
        size_bytes=1,
        as_of_date=DAY,
        schema_name="synthetic-provenance",
        schema_version="1",
    )
    strategy_artifact = SourceArtifact(
        artifact_id="synthetic/strategy.csv",
        sha256="b" * 64,
        size_bytes=1,
        as_of_date=DAY,
        schema_name="synthetic-strategy",
        schema_version="1",
    )
    evidence_artifact = SourceArtifact(
        artifact_id="synthetic/strategy-evidence.json",
        sha256="c" * 64,
        size_bytes=1,
        as_of_date=DAY,
        schema_name="synthetic-strategy-evidence",
        schema_version="1",
    )
    monkeypatch.setattr(ledger, "_financial_mvp_authority", lambda _spec: authority)
    monkeypatch.setattr(
        ledger,
        "_provenance",
        lambda _spec: ({"assignments": [], "strategies": []}, provenance_artifact),
    )
    monkeypatch.setattr(
        ledger,
        "_assignment_index",
        lambda *_args: (
            {"synthetic-ibkr"},
            {"synthetic-required"},
            {
                "synthetic-ibkr": [
                    {
                        "mapping_status": "MAPPED",
                        "strategy_id": "synthetic-required",
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr(
        ledger,
        "_project_account_facts",
        lambda provider_id, *_args, **_kwargs: (provider_facts[provider_id], []),
    )
    monkeypatch.setattr(
        ledger,
        "_complete_schwab_security_transfer_values",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ledger,
        "_validate_current_census_provider_generations",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        ledger,
        "_strategy_inputs",
        lambda *_args, **_kwargs: ([], [], (strategy_artifact, evidence_artifact)),
    )
    monkeypatch.setattr(
        ledger, "_declared_nav_gap_dates_by_provider", lambda *_args: {}
    )
    monkeypatch.setattr(
        ledger, "_provider_native_account_return_strategy_ids", lambda *_args: set()
    )
    monkeypatch.setattr(
        ledger, "_validate_provider_native_strategy_evidence", lambda *_args: None
    )
    monkeypatch.setattr(
        ledger, "_validate_nav_observation_value_bases", lambda *_args: None
    )
    monkeypatch.setattr(ledger, "_validate_nav_observation_moves", lambda *_args: None)
    monkeypatch.setattr(
        ledger, "_broker_strategy_returns", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(ledger, "_merge_account_returns", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        ledger, "_funded_account_live_boundaries", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        ledger,
        "_account_live_returns_within_funded_boundaries",
        lambda rows, *_args: rows,
    )
    monkeypatch.setattr(
        ledger, "_strategy_return_overlap_checks", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(ledger, "_portfolio123_provider_returns", lambda *_args: [])
    monkeypatch.setattr(ledger, "_refresh_strategy_evidence", lambda *_args: [])
    monkeypatch.setattr(ledger, "_admissions", lambda *_args, **_kwargs: [])
    coverage_call: dict[str, object] = {}

    def capture_ibkr_coverage(*_args: object, **kwargs: object) -> dict[str, object]:
        coverage_call.update(kwargs)
        return {}

    monkeypatch.setattr(ledger, "_ibkr_flow_coverage", capture_ibkr_coverage)
    monkeypatch.setattr(ledger, "_matrixport_flow_coverage", lambda *_args: {})
    monkeypatch.setattr(ledger, "_quantbase_flow_coverage", lambda *_args: {})

    staged = ledger.stage_candidate(spec_path)
    assert staged["state"] == "STAGED"
    assert coverage_call["required_accounts"] == {"synthetic-ibkr"}
    assert not (tmp_path / "staged" / "CURRENT.json").exists()
    assert canonical_json_bytes(authority["sources"]) == policy_before

    generation = tmp_path / "staged" / "generations" / str(staged["generation_id"])
    receipt = json.loads((generation / "admit-or-declare-receipt.json").read_bytes())
    assert receipt["run_observation"]["financial_cutoff"] == DAY.isoformat()
    assert {
        row["provider_id"] for row in receipt["run_observation"]["providers"]
    } == set(ledger._PROVIDER_CATALOGS)
    assert any(
        row["item_id"] == "unreferenced_collection" and row["action"] == "EXCLUDE"
        for row in receipt["dispositions"]
    )
    ledger.verify_candidate(spec_path, str(staged["generation_id"]))

    tampered = copy.deepcopy(receipt)
    tampered["run_observation"]["providers"][0]["observed_inventory_count"] += 1
    with pytest.raises(ProviderAdmissionError, match="counts or digests"):
        ledger.validate_admit_or_declare_receipt(
            tampered,
            product_authority=authority,
            source_segments=authority["sources"],
            provider_generations={
                str(row["provider_id"]): row for row in receipt["provider_generations"]
            },
        )


def test_observed_receipt_keeps_each_provider_original_qualification(
    tmp_path: Path,
) -> None:
    """An observation binds all selected providers; it does not restage them.

    The production current selection intentionally combines published, finite,
    partial, and staged generations.  Receipt verification must bind each
    observed identity and finality evidence without rewriting that qualification
    into ``STAGED_COMPLETE``.
    """

    authority, identities, manifests, catalogs, provider_facts = _fixture(
        native_source_inventory=True
    )
    qualifications = {
        "alpaca": GenerationQualification.PUBLISHED_COMPLETE,
        "fidelity": GenerationQualification.FINITE_STATIC,
        "ibkr": GenerationQualification.ACCEPTED_PARTIAL,
        "matrixport": GenerationQualification.ACCEPTED_PARTIAL,
        "portfolio123": GenerationQualification.STAGED_COMPLETE,
        "quantbase": GenerationQualification.STAGED_COMPLETE,
        "schwab": GenerationQualification.STAGED_COMPLETE,
    }
    requested_identities = [
        replace(identity, qualification=qualifications[identity.provider_id])
        for identity in identities
    ]
    refs, selections = _staged_observation(
        tmp_path / "sources", requested_identities, manifests, catalogs
    )
    for provider_id, qualification in qualifications.items():
        selections[provider_id]["qualification"] = qualification.value
    identities = [
        ProviderGenerationIdentity(
            provider_id=provider_id,
            generation_id=refs[provider_id]["generation_id"],
            manifest_sha256=refs[provider_id]["manifest_sha256"],
            catalog_sha256=refs[provider_id]["catalog_sha256"],
            qualification=qualification,
        )
        for provider_id, qualification in qualifications.items()
    ]
    observation = {
        "captured_at_utc": "2026-08-14T12:00:00Z",
        "financial_cutoff": DAY.isoformat(),
        "policy_contract_sha256": authority["contract_raw_sha256"],
        "providers": selections,
        "schema_version": 1,
    }
    observation_bytes = canonical_json_bytes(observation)
    authority["run_observation"] = observation
    authority["run_observation_path"] = str(tmp_path / "run-observation.json")
    authority["run_observation_size_bytes"] = len(observation_bytes)
    (tmp_path / "run-observation.json").write_bytes(observation_bytes)
    authority["run_observation_sha256"] = hashlib.sha256(observation_bytes).hexdigest()

    receipt, _ = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        (),
    )
    assert {
        row["provider_id"]: row["qualification"]
        for row in receipt["run_observation"]["providers"]
    } == {
        provider_id: qualification.value
        for provider_id, qualification in qualifications.items()
    }


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


def _repin_dataset_pins(
    authority: dict[str, Any],
    provider_id: str,
    manifests: dict[str, GenerationManifest],
    catalogs: dict[str, dict[str, Any]],
    provider_facts: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    admitted_too: bool,
) -> None:
    """Recompute one lane's #303 pins after a fixture mutation (digests only)."""

    source = authority["sources"][ledger._PROVIDER_SOURCE_SEGMENTS[provider_id]]
    policy = source["admission"]["dataset_disposition_policy"]
    inventory = ledger._provider_inventory(
        provider_id, manifests[provider_id], catalogs[provider_id], source
    )
    policy["upstream_inventory_sha256"] = ledger._inventory_digest(inventory)
    product_count, product_digest = ledger._product_data_class_set_digest(inventory)
    policy["product_data_class_count"] = product_count
    policy["product_data_class_set_sha256"] = product_digest
    if not admitted_too:
        return
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


def _schwab_retained_journal_lane(
    *, admit_external_flow: bool
) -> tuple[
    dict[str, Any],
    list[ProviderGenerationIdentity],
    dict[str, GenerationManifest],
    dict[str, dict[str, Any]],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    """One Schwab lane whose retained Journal wch capture materializes a flow."""

    authority, identities, manifests, catalogs, provider_facts = _fixture()
    if admit_external_flow:
        source = authority["sources"]["schwab-account-generation"]
        source["admission"]["facts"].append("EXTERNAL_FLOW")
    journal_id = "wch/transactions/account-38b653b91ff43a10.json"
    journal_bytes = b"synthetic-schwab-retained-journal"
    manifest = manifests["schwab"]
    manifests["schwab"] = GenerationManifest.build(
        as_of_date=manifest.as_of_date,
        adapter=manifest.adapter,
        coverage=manifest.coverage,
        artifacts=(
            *manifest.artifacts,
            SourceArtifact(
                artifact_id=journal_id,
                sha256=hashlib.sha256(journal_bytes).hexdigest(),
                size_bytes=len(journal_bytes),
                as_of_date=DAY,
                schema_name="schwab-wch-transactions",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    provider_facts["schwab"]["flows"] = [
        {
            "account_id": "private-schwab-journal",
            "as_of_date": DAY.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "mapping_basis": "PINNED_RETAINED_JOURNAL",
            "mapping_status": "MAPPED",
            "provider_id": "schwab",
            "record_id": "schwab-flow:private-journal-record",
            "source_artifact_id": journal_id,
            "strategy_id": "strategy-schwab",
        }
    ]
    _repin_dataset_pins(
        authority,
        "schwab",
        manifests,
        catalogs,
        provider_facts,
        admitted_too=admit_external_flow,
    )
    return authority, identities, manifests, catalogs, provider_facts


def test_schwab_pinned_journal_artifact_admits_external_flow() -> None:
    """The retained Schwab Journal wch capture is an admitted capital fact."""

    authority, identities, manifests, catalogs, provider_facts = (
        _schwab_retained_journal_lane(admit_external_flow=True)
    )

    receipt, _ = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        (),
    )
    journal = next(
        row
        for row in receipt["dispositions"]
        if row["provider_id"] == "schwab"
        and row["item_id"] == "wch/transactions/account-38b653b91ff43a10.json"
    )
    assert journal["item_kind"] == "ARTIFACT"
    assert journal["action"] == "ADMIT"
    assert journal["target_facts"] == ["flows"]
    assert journal["materialized_record_count"] == 1


def test_schwab_retained_journal_flow_requires_the_admitted_fact() -> None:
    """Without EXTERNAL_FLOW authority the retained Journal artifact fails."""

    authority, identities, manifests, catalogs, provider_facts = (
        _schwab_retained_journal_lane(admit_external_flow=False)
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="schwab materialized target facts differ from authority",
    ):
        ledger._admit_or_declare_receipt(
            authority,
            identities,
            manifests,
            catalogs,
            provider_facts,
            (),
        )


def _portfolio123_retained_transactions_lane(
    *, admit_transaction: bool
) -> tuple[
    dict[str, Any],
    list[ProviderGenerationIdentity],
    dict[str, GenerationManifest],
    dict[str, dict[str, Any]],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    """One P123 lane whose refreshed generation declares a transactions dataset."""

    authority, identities, manifests, catalogs, provider_facts = _fixture()
    if admit_transaction:
        source = authority["sources"]["portfolio123-model-generation"]
        source["admission"]["facts"].append("TRANSACTION")
    manifest = manifests["portfolio123"]
    manifests["portfolio123"] = GenerationManifest.build(
        as_of_date=manifest.as_of_date,
        adapter=manifest.adapter,
        coverage=replace(
            manifest.coverage,
            fact_types=(*manifest.coverage.fact_types, "transactions"),
        ),
        artifacts=manifest.artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    _repin_dataset_pins(
        authority,
        "portfolio123",
        manifests,
        catalogs,
        provider_facts,
        admitted_too=admit_transaction,
    )
    return authority, identities, manifests, catalogs, provider_facts


def test_portfolio123_transactions_fact_excludes_retained_model_evidence() -> None:
    """A retained P123 model transactions dataset declares, never admits."""

    authority, identities, manifests, catalogs, provider_facts = (
        _portfolio123_retained_transactions_lane(admit_transaction=True)
    )

    receipt, _ = ledger._admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        (),
    )
    transactions = next(
        row
        for row in receipt["dispositions"]
        if row["provider_id"] == "portfolio123" and row["item_id"] == "transactions"
    )
    assert transactions["item_kind"] == "COVERAGE_FACT_TYPE"
    assert transactions["action"] == "EXCLUDE"
    assert transactions["reason"] == "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER"
    assert transactions["readiness_impact"] == "REQUIRED_NOT_READY"
    assert transactions["materialized_record_count"] == 0
    assert transactions["target_facts"] == []


def test_portfolio123_retained_transactions_require_the_admitted_fact() -> None:
    """Without TRANSACTION authority the retained dataset fails the gate."""

    authority, identities, manifests, catalogs, provider_facts = (
        _portfolio123_retained_transactions_lane(admit_transaction=False)
    )

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="portfolio123 materialized target facts differ from authority",
    ):
        ledger._admit_or_declare_receipt(
            authority,
            identities,
            manifests,
            catalogs,
            provider_facts,
            (),
        )


QE_LEDGER_SPEC_PATH = os.environ.get("QE_LEDGER_SPEC_PATH", "")


@pytest.mark.skipif(
    not QE_LEDGER_SPEC_PATH or not Path(QE_LEDGER_SPEC_PATH).is_file(),
    reason="QE_LEDGER_SPEC_PATH does not name a mounted provider-ledger spec",
)
def test_receipt_reproduces_every_contract_admitted_set() -> None:
    """Producer-output regression (PR #387 review): run the governed
    admit-or-declare derivation against the mounted estate and require the
    receipt's per-provider admitted sets to reproduce the contract's
    dataset_disposition_policy pins. build_catalog itself enforces the
    upstream-inventory, data-class-set, and admitted-set digest gates against
    these pins; this test re-runs that producer on live generations so the
    pinned literals cannot drift silently alongside the contract."""

    import collections

    _catalog, _manifest, _catalog_bytes, receipt_bytes = ledger.build_catalog(
        Path(QE_LEDGER_SPEC_PATH)
    )
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    admitted_counts: dict[str, int] = collections.defaultdict(int)
    for row in receipt["dispositions"]:
        action = str(row["action"]).rsplit(".", maxsplit=1)[-1]
        if action == "ADMIT":
            admitted_counts[str(row["source_segment_id"])] += 1
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    checked = 0
    for segment in contract["source_segments"]:
        policy = (segment.get("admission") or {}).get("dataset_disposition_policy")
        if not policy:
            continue
        segment_id = str(segment["source_id"])
        assert admitted_counts[segment_id] == policy["admitted_item_count"], segment_id
        checked += 1
    assert checked >= 7


def test_per_reason_readiness_map_is_honored_by_builder_and_verifier() -> None:
    """The verifier must resolve an exclusion exactly as the builder did.

    A per-reason map that the builder applies but the verifier does not would
    make every correctly built receipt fail its own validation, so this walks
    the full builder -> verifier round trip rather than the resolver alone.
    """

    receipt, _receipt_bytes, authority, provider_generations = _receipt(
        unreferenced_dataset=True,
        readiness_by_reason={
            "REQUIRED_DATA_CLASS_NOT_MATERIALIZED": "REQUIRED_NOT_READY",
            "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER": "NO_CURRENT_PRODUCT_EFFECT",
        },
    )
    ledger.validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=authority["sources"],
        provider_generations=provider_generations,
    )
    impacts = {
        (row["reason"], row["readiness_impact"])
        for row in receipt["dispositions"]
        if row["action"] == "EXCLUDE"
    }
    assert (
        "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER",
        "NO_CURRENT_PRODUCT_EFFECT",
    ) in impacts
    assert not any(
        reason == "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER"
        and impact == "REQUIRED_NOT_READY"
        for reason, impact in impacts
    )


def test_absent_readiness_map_keeps_the_scalar_impact() -> None:
    """An unamended contract must behave exactly as it did before the map."""

    receipt, _receipt_bytes, authority, provider_generations = _receipt(
        unreferenced_dataset=True
    )
    ledger.validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=authority["sources"],
        provider_generations=provider_generations,
    )
    assert {
        row["readiness_impact"]
        for row in receipt["dispositions"]
        if row["action"] == "EXCLUDE"
    } == {"REQUIRED_NOT_READY"}

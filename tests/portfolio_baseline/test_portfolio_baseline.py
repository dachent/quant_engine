from __future__ import annotations

import copy
import csv
from datetime import date, timedelta
from decimal import Decimal, localcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_baseline.py"
SPEC = importlib.util.spec_from_file_location("portfolio_baseline", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio baseline builder")
baseline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = baseline
SPEC.loader.exec_module(baseline)


def _csv(path: Path, source_key: str, rows: list[dict[str, str]]) -> None:
    fields = baseline._CSV_FIELDS[source_key]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _row(source_key: str, **values: str) -> dict[str, str]:
    return {field: values.get(field, "") for field in baseline._CSV_FIELDS[source_key]}


# The retired history-only records the provenance registry carries beyond the nine
# tracker strategies: the governed HWM policy excludes each of them.
RETIRED_RECORD_IDS = (
    "p123-conn-1911998-andreas-sc-ai-mq-live",
    "p123-conn-1951348-higher-beta-earnings",
    "p123-conn-1956961-ai-largecap-ls",
    "p123-conn-1958970-high-octane",
    "quantbase-crisis-factor-investing",
    "quantbase-single-index-leveraged-in-out",
)
RETIRED_EXCLUSIONS = dict.fromkeys(RETIRED_RECORD_IDS, date(2026, 1, 1))


def _published_provenance(
    tmp_path: Path, *, reserve_accounts: tuple[str, ...] = ()
) -> tuple[Path, str, str]:
    root = tmp_path / "private-provenance"
    strategies = []
    for index in range(1, 16):
        strategy_id = f"strategy-{index}" if index <= 9 else RETIRED_RECORD_IDS[index - 10]
        strategies.append(
            {
                "aliases": [f"Synthetic Strategy {index}"],
                "cost_status": "KNOWN" if index == 1 else "UNAVAILABLE",
                "coverage_status": "PARTIAL",
                "economic_family": f"SYNTHETIC FAMILY {index}",
                "evidence_routes": [
                    {
                        "evidence_layer": "ACCOUNT_LIVE",
                        "purpose": "SYNTHETIC live evidence",
                        "source_id": "synthetic-source",
                    },
                    {
                        "evidence_layer": "BACKTEST",
                        "purpose": "SYNTHETIC comparison only",
                        "source_id": "synthetic-source",
                    },
                ],
                "gaps": ["SYNTHETIC incomplete history"],
                "name": f"Synthetic Strategy {index}",
                "strategy_id": strategy_id,
                "version_ref": None,
            }
        )
    accounts = [
        {
            "account_id": "account-schwab-a",
            "aliases": ["Schwab | SYNTH-A"],
            "name": "SYNTHETIC",
            "role": "INVESTMENT",
            "source_routes": ["synthetic-source"],
        },
        {
            "account_id": "account-schwab-b",
            "aliases": ["Schwab | SYNTH-B"],
            "name": "SYNTHETIC",
            "role": "INVESTMENT",
            "source_routes": ["synthetic-source"],
        },
        {
            "account_id": "account-ibkr",
            "aliases": ["IBRK | SYNTH-I"],
            "name": "SYNTHETIC",
            "role": "INVESTMENT",
            "source_routes": ["synthetic-source"],
        },
        {
            "account_id": "account-alpaca",
            "aliases": ["Alpaca"],
            "name": "SYNTHETIC",
            "role": "INVESTMENT",
            "source_routes": ["synthetic-source"],
        },
        {
            "account_id": "account-composer",
            "aliases": ["Composer"],
            "name": "SYNTHETIC",
            "role": "INVESTMENT",
            "source_routes": ["synthetic-source"],
        },
        {
            "account_id": "reserve-classification",
            "aliases": ["SYNTHETIC reserve"],
            "name": "SYNTHETIC",
            "role": "RESERVE",
            "source_routes": ["synthetic-source"],
        },
    ]
    assignments = []
    for index, account_id in enumerate(
        ("account-schwab-a", "account-ibkr", "account-alpaca"), start=1
    ):
        assignments.append(
            {
                "account_id": account_id,
                "assignment_id": f"assignment-{index}",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "MAPPED",
                "observed_label": f"Synthetic Strategy {index}",
                "source_classification": "SYNTHETIC",
                "source_id": "synthetic-source",
                "strategy_id": f"strategy-{index}",
            }
        )
    for index, reserve_account_id in enumerate(reserve_accounts, start=1):
        assignments.append(
            {
                "account_id": reserve_account_id,
                "assignment_id": f"assignment-reserve-{index}",
                "effective_end": None,
                "effective_start": "2025-01-01",
                "mapping_status": "UNMAPPED",
                "observed_label": "General",
                "pool_id": "reserve-capital",
                "pool_mapping_status": "MAPPED",
                "source_classification": "SYNTHETIC",
                "source_id": "synthetic-source",
                "strategy_id": None,
                "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            }
        )
    assignments.append(
        {
            "account_id": "account-composer",
            "assignment_id": "assignment-unmapped",
            "effective_end": None,
            "effective_start": None,
            "mapping_status": "UNMAPPED",
            "observed_label": "General",
            "source_classification": "SYNTHETIC",
            "source_id": "synthetic-source",
            "strategy_id": None,
        }
    )
    catalog: dict[str, Any] = {
        "accounts": accounts,
        "acquisition_date": "2026-07-30",
        "as_of_date": "2026-07-30",
        "assignments": assignments,
        "assumptions": [],
        "catalog_schema_version": 1,
        "corrections": [],
        "open_gaps": [],
        "policy_version": "PORT-PERF-1",
        "source_artifacts": [],
        "strategies": strategies,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_id="normalized/portfolio-provenance.json",
        sha256=catalog_hash,
        size_bytes=len(catalog_bytes),
        as_of_date=date(2026, 7, 30),
        schema_name="portfolio-provenance-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-provenance", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="SYNTHETIC provenance boundary",
            fact_types=("accounts", "strategies"),
            observed_start=date(2026, 7, 30),
            observed_end=date(2026, 7, 30),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-provenance.json").write_bytes(catalog_bytes)
    publish_generation(root, manifest.generation_id)
    return root, manifest.generation_id, catalog_hash


def _spec(
    tmp_path: Path, *, reserve_accounts: tuple[str, ...] = ()
) -> tuple[Path, dict[str, Any]]:
    source_root = tmp_path / "private-source"
    source_root.mkdir(parents=True)
    values = ["20", "15", "10", "10", "5", "10", "10", "10", "10"]
    weights = [str(int(value) / 100) for value in values]

    _csv(
        source_root / "component.csv",
        "component-decision-table",
        [
            _row(
                "component-decision-table",
                component=f"Synthetic Strategy {index}",
                current_value=value,
                systematic_weight=weights[index - 1],
                conceptual_family="SYNTHETIC",
                preliminary_verdict="HOLD",
                required_gate="SYNTHETIC",
            )
            for index, value in enumerate(values, start=1)
        ],
    )
    _csv(
        source_root / "coverage.csv",
        "data-coverage-matrix",
        [
            _row(
                "data-coverage-matrix",
                component=f"Synthetic Strategy {index}",
                current_value=value,
                systematic_weight=weights[index - 1],
                account_specific_daily_series="yes" if index <= 5 else "no",
                daily_start="2026-07-01" if index <= 5 else "",
                daily_end="2026-07-06" if index <= 5 else "",
                return_observations="2" if index <= 5 else "",
                current_evidence_tier="SYNTHETIC",
                phase2_limitation="" if index <= 5 else "SYNTHETIC missing NAV",
            )
            for index, value in enumerate(values, start=1)
        ],
    )
    _csv(
        source_root / "costs.csv",
        "cost-economics",
        [
            _row(
                "cost-economics",
                component=f"Synthetic Strategy {index}",
                current_value=value,
                systematic_weight=weights[index - 1],
                annual_external_cost_dollars="1" if index == 1 else "",
                cost_status="known_fixed" if index == 1 else "unknown",
            )
            for index, value in enumerate(values, start=1)
        ],
    )
    _csv(
        source_root / "layers.csv",
        "evidence-layer-metrics",
        [
            _row(
                "evidence-layer-metrics",
                component=f"Synthetic Strategy {index}",
                layer="account_live",
                provenance="SYNTHETIC",
                start_date="2026-07-01",
                end_date="2026-07-06",
                observations="2",
                status="available" if index > 1 else "",
            )
            for index in range(1, 10)
        ],
    )
    _csv(
        source_root / "live.csv",
        "strategy-live-metrics",
        [
            _row(
                "strategy-live-metrics",
                first_date="2026-07-01",
                last_date="2026-07-06",
                return_observations="2",
                strategy=f"Synthetic Strategy {index}",
            )
            for index in range(1, 10)
        ]
        + [
            _row(
                "strategy-live-metrics",
                first_date="2026-07-01",
                last_date="2026-07-06",
                return_observations="2",
                strategy="SYNTHETIC AGGREGATE",
            )
        ],
    )
    _csv(
        source_root / "gaps.csv",
        "implementation-gap",
        [
            _row(
                "implementation-gap",
                component=f"Synthetic Strategy {index}",
                provider_layer="SYNTHETIC provider",
                account_layer="SYNTHETIC account",
                status="partial",
                note="SYNTHETIC gap",
            )
            for index in range(1, 10)
        ],
    )
    _csv(
        source_root / "schwab_nav.csv",
        "schwab-nav",
        [
            _row("schwab-nav", account="SYNTH-A", date="2026-07-01", nav="10"),
            _row("schwab-nav", account="SYNTH-A", date="2026-07-02", nav="10.5"),
            _row("schwab-nav", account="SYNTH-A", date="2026-07-06", nav="11"),
            _row("schwab-nav", account="SYNTH-B", date="2026-07-01", nav="40"),
            _row("schwab-nav", account="SYNTH-B", date="2026-07-06", nav="41"),
        ],
    )
    _csv(
        source_root / "ibkr.csv",
        "ibkr-ledger",
        [
            _row(
                "ibkr-ledger",
                date="2026-07-01",
                account="SYNTH-I",
                starting_value="19",
                ending_value="20",
                twr="0.01",
                external_flow="1",
                transfer_flow="0",
                source_file="SYNTHETIC.xml",
            )
        ],
    )
    _csv(
        source_root / "alpaca_nav.csv",
        "alpaca-nav",
        [_row("alpaca-nav", date="2026-07-01", nav="30")],
    )
    _csv(
        source_root / "diagnostic.csv",
        "attribution-diagnostic",
        [
            _row(
                "attribution-diagnostic",
                institution="Schwab",
                account="SYNTH-A",
                date="2026-07-01",
                nav="10",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="Schwab",
                account="SYNTH-A",
                date="2026-07-02",
                nav="10.5",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="Schwab",
                account="SYNTH-A",
                date="2026-07-06",
                nav="11",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="Schwab",
                account="SYNTH-B",
                date="2026-07-01",
                nav="40",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="Schwab",
                account="SYNTH-B",
                date="2026-07-06",
                nav="41",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="IBRK",
                account="SYNTH-I",
                date="2026-07-01",
                nav="20",
                note="SYNTHETIC",
            ),
            _row(
                "attribution-diagnostic",
                institution="Alpaca",
                account="Alpaca",
                date="2026-07-01",
                nav="30",
                note="SYNTHETIC",
            ),
        ],
    )
    _csv(
        source_root / "schwab_flows.csv",
        "schwab-flows",
        [
            _row(
                "schwab-flows",
                account="SYNTH-A",
                date="2026-07-01",
                bucket="external_flow",
                amount="5",
                action="SYNTHETIC",
                description="SYNTHETIC",
            ),
            _row(
                "schwab-flows",
                account="SYNTH-A",
                date="2026-07-01",
                bucket="external_flow",
                amount="5",
                action="SYNTHETIC DUPLICATE VALUE",
                description="SYNTHETIC DISTINCT SOURCE ROW",
            ),
        ],
    )
    _csv(
        source_root / "alpaca_flows.csv",
        "alpaca-flows",
        [
            _row(
                "alpaca-flows",
                account="Alpaca",
                date="2026-07-01",
                bucket="external_flow",
                amount="30",
                note="SYNTHETIC observed",
            )
        ],
    )
    _csv(
        source_root / "alpaca_events.csv",
        "alpaca-events",
        [
            _row(
                "alpaca-events",
                date="2026-07-01",
                type="deposit",
                cash_after="30",
                nav_after="30",
            ),
            _row(
                "alpaca-events",
                date="2026-07-02",
                type="buy_fill",
                symbol="SYNTH",
                qty="2",
                price="5",
                cash_after="20",
                nav_after="30",
            ),
        ],
    )
    json_sources = {
        "summary.json": {"systematic_total": 100, "reserve_total": 25},
        "evidence.json": {"current_correlation_one_underwriting_loss": -0.3725},
        "control.json": {"portfolio_action": "DO_NOT_EXPAND_SYSTEMATIC_RISK"},
        "hwm.json": {
            "full_systematic_hwm_status": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV"
        },
    }
    for filename, value in json_sources.items():
        (source_root / filename).write_text(json.dumps(value), encoding="utf-8")

    source_files = {
        "alpaca-events": "alpaca_events.csv",
        "alpaca-flows": "alpaca_flows.csv",
        "alpaca-nav": "alpaca_nav.csv",
        "attribution-diagnostic": "diagnostic.csv",
        "combined-control-status": "control.json",
        "component-decision-table": "component.csv",
        "cost-economics": "costs.csv",
        "current-portfolio-summary": "summary.json",
        "data-coverage-matrix": "coverage.csv",
        "evidence-layer-metrics": "layers.csv",
        "hwm-status": "hwm.json",
        "ibkr-ledger": "ibkr.csv",
        "implementation-gap": "gaps.csv",
        "phase2-evidence-summary": "evidence.json",
        "schwab-flows": "schwab_flows.csv",
        "schwab-nav": "schwab_nav.csv",
        "strategy-live-metrics": "live.csv",
    }
    sources = {}
    for source_key, filename in source_files.items():
        content = (source_root / filename).read_bytes()
        sources[source_key] = {
            "as_of_date": "2026-07-30",
            "expected_sha256": hashlib.sha256(content).hexdigest(),
            "relative_path": filename,
            "root_id": "synthetic",
            "schema_name": f"synthetic-{source_key}",
            "schema_version": "1",
        }
    sources["component-decision-table"]["as_of_date"] = "2026-07-25"
    sources["current-portfolio-summary"]["as_of_date"] = "2026-07-25"

    provenance_root, generation_id, catalog_hash = _published_provenance(
        tmp_path, reserve_accounts=reserve_accounts
    )
    spec: dict[str, Any] = {
        "as_of_date": "2026-07-30",
        "policy_version": "PORT-PERF-1",
        "provenance": {
            "catalog_sha256": catalog_hash,
            "generation_id": generation_id,
            "root": str(provenance_root),
        },
        "replay": {
            "correlation_one_stress": {"tolerance": "0.0001", "value": "0.3725"},
            "full_systematic_hwm_status": "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
            "nav_coverage": {"tolerance": "0.0001", "value": "0.6"},
            "portfolio_action": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            "reserve_total_usd": {"tolerance": "0.01", "value": "25"},
            "systematic_total_usd": {"tolerance": "0.01", "value": "100"},
            "total_liquid_usd": {"tolerance": "0.01", "value": "125"},
        },
        "roots": {"synthetic": str(source_root)},
        "schema_version": 1,
        "sources": sources,
    }
    spec_path = tmp_path / "private-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, spec


def _spec_with_schwab_provider(
    tmp_path: Path,
    *,
    catalog_schema_version: int = 1,
    catalog_artifact_as_of_date: date = date(2026, 7, 30),
    manifest_as_of_date: date = date(2026, 7, 30),
    v2_nav_provenance: str = "VALID",
) -> Path:
    spec_path, spec = _spec(tmp_path)
    provider_root = tmp_path / "dataSchwab"
    valid_nav_artifact_ids = ["wch/nav-0.csv", "wch/nav-1.csv"]
    provider_catalog = {
        "accounts": [
            {"account_id": "account-schwab-a"},
            {"account_id": "account-schwab-b"},
        ],
        "as_of_date": "2026-07-30",
        "catalog_schema_version": catalog_schema_version,
        "coverage": {
            "known_gaps": [
                "one accepted account has no reported non-cash position row"
            ],
            "status": "PARTIAL",
        },
        "nav_observations": [
            {
                "account_id": "account-schwab-a",
                "as_of_date": "2026-07-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            },
            {
                "account_id": "account-schwab-b",
                "as_of_date": "2026-07-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "40"},
            },
        ],
        "positions": [
            {
                "account_id": "account-schwab-a",
                "as_of_date": "2026-07-30",
                "average_price": "9",
                "evidence_class": "WCH_CURRENT",
                "mapping_status": "UNMAPPED",
                "market_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "30",
                },
                "observed_at": "2026-07-30T20:00:00+00:00",
                "quantity": "3",
                "record_id": "position-synthetic-schwab",
                "security_id": "security-schwab-synthetic",
                "strategy_id": None,
                "symbol": "SYNTH",
            }
        ],
        "transactions": [
            {
                "account_id": "account-schwab-a",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
                "event_date": "2026-07-02",
                "evidence_class": "WCH_HISTORY",
                "kind": "TRADE",
                "mapping_status": "UNMAPPED",
                "record_id": "transaction-synthetic-schwab",
                "security_id": "security-schwab-synthetic",
                "strategy_id": None,
                "symbol": "SYNTH",
            }
        ],
    }
    if catalog_schema_version == 2:
        for index, row in enumerate(provider_catalog["nav_observations"]):
            if v2_nav_provenance != "MISSING":
                row["source_artifact_id"] = (
                    f"wch/forged-{index}.csv"
                    if v2_nav_provenance == "FORGED_SOURCE"
                    else valid_nav_artifact_ids[index]
                )
                row["value_basis"] = (
                    "ACCOUNT_TOTAL"
                    if v2_nav_provenance == "WRONG_BASIS"
                    else "ACCOUNT_VALUE"
                )
    catalog_bytes = canonical_json_bytes(provider_catalog)
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_id="normalized/schwab-generation.json",
        sha256=catalog_hash,
        size_bytes=len(catalog_bytes),
        as_of_date=catalog_artifact_as_of_date,
        schema_name="schwab-generation-catalog",
        schema_version=str(catalog_schema_version),
    )
    artifacts = [artifact]
    if catalog_schema_version == 2:
        artifacts.extend(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(artifact_id.encode()).hexdigest(),
                size_bytes=1,
                as_of_date=date(2026, 7, 30),
                schema_name=(
                    "schwab-account-transaction"
                    if v2_nav_provenance == "WRONG_ROLE"
                    else "schwab-wch-historical-balance"
                ),
                schema_version="provider-export",
            )
            for artifact_id in valid_nav_artifact_ids
        )
    manifest = GenerationManifest.build(
        as_of_date=manifest_as_of_date,
        adapter=AdapterIdentity("synthetic-schwab", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic accepted Schwab slice",
            fact_types=("nav", "positions", "transactions"),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(provider_root, manifest)
    (manifest_path.parent / "schwab-generation.json").write_bytes(catalog_bytes)
    pointer = publish_generation(provider_root, manifest.generation_id)
    spec["roots"]["schwab-provider"] = str(provider_root)
    spec["schema_version"] = 2
    spec["provider_generations"] = {
        "schwab": {
            "catalog_sha256": catalog_hash,
            "generation_id": manifest.generation_id,
            "manifest_sha256": pointer.manifest_sha256,
            "root_id": "schwab-provider",
        }
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


def _provider_ledger_generation(
    tmp_path: Path,
    *,
    catalog_schema_version: int,
    catalog_as_of_date: date = date(2026, 7, 30),
    catalog_artifact_as_of_date: date = date(2026, 7, 30),
    manifest_catalog_schema_version: int | None = None,
    manifest_as_of_date: date = date(2026, 7, 30),
    retained_source_as_of_date: date | None = None,
    nav_observations: list[dict[str, Any]] | None = None,
    flows: list[dict[str, Any]] | None = None,
    admissions: list[dict[str, Any]] | None = None,
    extra_catalog: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, str]]:
    provider_ids = (
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    )
    zero_fact_types = (
        "corporate_actions",
        "flows",
        "nav_observations",
        "positions",
        "transactions",
    )
    catalog = {
        "admissions": admissions or [],
        "as_of_date": catalog_as_of_date.isoformat(),
        "catalog_schema_version": catalog_schema_version,
        "corporate_actions": [],
        "fact_counts": [
            {
                "fact_type": fact_type,
                "mapped_count": 0,
                "record_count": 0,
                "unmapped_count": 0,
            }
            for fact_type in zero_fact_types
        ],
        "flows": flows or [],
        "nav_observations": nav_observations or [],
        "positions": [],
        "provider_coverage": [
            {
                "coverage": {
                    "boundary": "synthetic empty provider boundary",
                    "known_gaps": [],
                    "observed_end": "2026-07-30",
                    "observed_start": "2026-07-30",
                    "status": "PARTIAL",
                },
                "provider_id": provider_id,
            }
            for provider_id in provider_ids
        ],
        "provider_generations": [
            {
                "catalog_sha256": hashlib.sha256(
                    f"{provider_id}-catalog".encode()
                ).hexdigest(),
                "generation_as_of_date": "2026-07-30",
                "generation_id": hashlib.sha256(
                    f"{provider_id}-generation".encode()
                ).hexdigest(),
                "manifest_sha256": hashlib.sha256(
                    f"{provider_id}-manifest".encode()
                ).hexdigest(),
                "provider_id": provider_id,
                "qualification": "ACCEPTED_PARTIAL",
            }
            for provider_id in provider_ids
        ],
        "provider_strategy_returns": [],
        "quality_findings": [],
        "reconciliation_checks": [
            {"check_id": f"synthetic-check-{index}", "status": "PASS"}
            for index in range(3)
        ],
        "securities": [],
        "strategy_evidence": [
            {"evidence_id": f"synthetic-evidence-{index}"} for index in range(9)
        ],
        "strategy_returns": [
            {
                "date": "2026-07-30",
                "return": "0",
                "strategy_id": "strategy-1",
            }
        ],
        "transactions": [],
    }
    if catalog_schema_version == 2:
        catalog["capital_anchors"] = []
        schema = json.loads(
            (
                REPOSITORY_ROOT / "schemas/portfolio/provider-ledger.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(catalog)
    # Keys the schema does not list (portfolio_fx_generation, which the producer
    # emits) are added after validation.
    catalog.update(extra_catalog or {})
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_id="normalized/provider-ledger.json",
        sha256=catalog_hash,
        size_bytes=len(catalog_bytes),
        as_of_date=catalog_artifact_as_of_date,
        schema_name="provider-ledger-catalog",
        schema_version=str(manifest_catalog_schema_version or catalog_schema_version),
    )
    retained_artifacts = ()
    if retained_source_as_of_date is not None:
        retained_artifacts = (
            SourceArtifact(
                artifact_id="source/retained-provider-observation.json",
                sha256=hashlib.sha256(b"retained-provider-observation").hexdigest(),
                size_bytes=len(b"retained-provider-observation"),
                as_of_date=retained_source_as_of_date,
                schema_name="retained-provider-observation",
                schema_version="1",
            ),
        )
    manifest = GenerationManifest.build(
        as_of_date=manifest_as_of_date,
        adapter=AdapterIdentity("synthetic-provider-ledger", "2.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic accepted provider-ledger slice",
            fact_types=("nav",),
        ),
        artifacts=(artifact, *retained_artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    root = tmp_path / "provider-ledger"
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "provider-ledger.json").write_bytes(catalog_bytes)
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return root, {
        "catalog_sha256": catalog_hash,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest_hash,
        "root_id": "provider-ledger",
    }


def test_stage_is_deterministic_reconciled_and_coverage_aware(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path)
    output_root = tmp_path / "private-output"

    first = baseline.stage_baseline(spec_path, output_root)
    second = baseline.stage_baseline(spec_path, output_root)

    assert first == second
    assert first["strategy_count"] == 9
    assert first["allocation_count"] == 10
    assert first["nav_count"] == 7
    assert first["flow_count"] == 4
    assert first["transaction_count"] == 1
    assert first["unmapped_count"] == 1
    assert first["replay_status"] == "PASS"
    generation_dir = output_root / "generations" / first["generation_id"]
    catalog_bytes = (generation_dir / "portfolio-baseline.json").read_bytes()
    catalog = json.loads(catalog_bytes)
    assert str(spec["roots"]["synthetic"]) not in catalog_bytes.decode()
    assert str(spec["provenance"]["root"]) not in catalog_bytes.decode()
    assert catalog["positions"] == []
    assert catalog["evidence_layer_metrics"][0]["status"] == "UNAVAILABLE_UNLABELED"
    assert catalog["replay"]["total_liquid_usd"] == "125"
    assert catalog["replay"]["nav_coverage"] == "0.6"
    assert {item["role"] for item in catalog["capital_pools"]} == {
        "SYSTEMATIC",
        "RESERVE",
    }
    assert {item["as_of_date"] for item in catalog["allocations"]} == {"2026-07-25"}
    assert {item["publication_date"] for item in catalog["allocations"]} == {
        "2026-07-30"
    }
    assignment_coverage = next(
        item for item in catalog["fact_coverage"] if item["fact_type"] == "assignments"
    )
    assert assignment_coverage == {
        "fact_type": "assignments",
        "known_gaps": [],
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }
    assert {item["exception_id"] for item in catalog["reconciliation_exceptions"]} >= {
        "current-strategy-attribution-partial",
        "historical-strategy-attribution-unresolved",
    }
    assert {item["status"] for item in catalog["calendar_coverage"]} >= {
        "NON_BUSINESS_WEEKEND",
        "MISSING_ON_PROVIDER_OBSERVED_DATE",
        "UNRESOLVED_NO_CALENDAR_EVIDENCE",
        "NO_ACCOUNT_NAV_OBSERVATIONS",
    }
    assert (
        next(
            item
            for item in catalog["fact_coverage"]
            if item["fact_type"] == "positions"
        )["status"]
        == "FAILED"
    )
    assert first["catalog_sha256"] == hashlib.sha256(catalog_bytes).hexdigest()
    assert not (output_root / "CURRENT.json").exists()


def test_generation_identity_uses_logical_not_physical_root_bindings(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    first_catalog, first_manifest, first_bytes = baseline.build_catalog(spec_path)

    relocated_source = tmp_path / "relocated-source"
    relocated_provenance = tmp_path / "relocated-provenance"
    shutil.copytree(Path(spec["roots"]["synthetic"]), relocated_source)
    shutil.copytree(Path(spec["provenance"]["root"]), relocated_provenance)
    relocated = json.loads(json.dumps(spec))
    relocated["roots"]["synthetic"] = str(relocated_source)
    relocated["provenance"]["root"] = str(relocated_provenance)
    relocated_path = tmp_path / "relocated-spec.json"
    relocated_path.write_text(json.dumps(relocated), encoding="utf-8")

    second_catalog, second_manifest, second_bytes = baseline.build_catalog(
        relocated_path
    )
    assert second_catalog == first_catalog
    assert second_bytes == first_bytes
    assert second_manifest.generation_id == first_manifest.generation_id
    assert second_manifest.to_json_bytes() == first_manifest.to_json_bytes()


def test_reserve_pool_intervals_are_not_strategy_attribution_gaps() -> None:
    claims, exceptions = baseline._fact_coverage(
        accounts=[{"account_id": "reserve-account", "role": "INVESTMENT"}],
        assignments=[
            {
                "account_id": "reserve-account",
                "effective_end": None,
                "effective_start": "2026-01-01",
                "mapping_status": "UNMAPPED",
                "pool_id": "reserve-capital",
                "strategy_id": None,
                "strategy_mapping_disposition": "RESERVE_NOT_APPLICABLE",
            }
        ],
        transactions=[],
        flows=[],
        positions=[],
        schwab_known_gaps=[],
        missing_strategy_nav_count=0,
        as_of_date=date(2026, 8, 3),
    )

    current_claim = next(
        row for row in claims if row["fact_type"] == "current_strategy_attribution"
    )
    current_exception = next(
        row
        for row in exceptions
        if row["exception_id"] == "current-strategy-attribution-partial"
    )
    assert current_claim["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert current_claim["known_gaps"] == []
    assert current_exception["record_count"] == 0
    assert current_exception["status"] == "PASS"


def test_coverage_evidence_never_invents_fact_missing_intervals() -> None:
    matrix, windows, attribution = baseline._coverage_evidence(
        accounts=[
            {
                "account_id": "account-a",
                "base_currency": "USD",
                "provider": "provider-a",
                "role": "INVESTMENT",
            }
        ],
        assignments=[
            {
                "account_id": "account-a",
                "effective_end": "2026-07-31",
                "effective_start": "2026-01-01",
                "mapping_status": "UNMAPPED",
                "pool_id": "systematic-capital",
                "source_id": "assignment-authority",
                "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            }
        ],
        provider_admissions=[
            {
                "account_id": "account-a",
                "disposition": "AUTHORITATIVE",
                "effective_end": "2026-08-03",
                "effective_start": "2026-07-31",
                "fact_type": "transactions",
            }
        ],
        provider_coverage=[
            {
                "coverage": {
                    "boundary": "provider package only",
                    "observed_end": "2026-08-03",
                    "observed_start": "2026-07-31",
                    "status": "PARTIAL",
                },
                "provider_id": "provider-a",
            }
        ],
        nav=[],
        positions=[],
        transactions=[],
        flows=[],
        as_of_date=date(2026, 8, 3),
    )

    transaction = next(row for row in matrix if row["fact_type"] == "transactions")
    assert transaction["boundary_basis"] == (
        "EFFECTIVE_DATED_ADMISSION_NOT_COMPLETENESS"
    )
    assert transaction["status"] == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"
    assert transaction["admission_effective_start"] == "2026-07-31"
    assert transaction["admission_effective_end"] == "2026-08-03"
    assert transaction["declared_start"] is None
    assert transaction["declared_end"] is None
    assert transaction["missing_interval_start"] is None
    assert transaction["missing_interval_end"] is None
    assert transaction["missing_intervals"] == []
    assert (
        next(row for row in windows if row["window_type"] == "PROVIDER_SOURCE")[
            "observed_start"
        ]
        == "2026-07-31"
    )
    assert attribution == [
        {
            "account_id": "account-a",
            "authority": "assignment-authority",
            "disposition": "POOL_ONLY_UNALLOCATED",
            "effective_end": "2026-07-31",
            "effective_start": "2026-01-01",
            "period": "HISTORICAL",
            "provider_id": "provider-a",
        }
    ]


def test_publish_requires_exact_current_candidate(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)
    output_root = tmp_path / "private-output"
    staged = baseline.stage_baseline(spec_path, output_root)

    with pytest.raises(baseline.BaselineValidationError, match="differs"):
        baseline.publish_baseline(spec_path, output_root, "0" * 64)
    assert not (output_root / "CURRENT.json").exists()

    published = baseline.publish_baseline(
        spec_path, output_root, staged["generation_id"]
    )
    assert published["publication_state"] == "PUBLISHED"
    assert read_current(output_root).generation_id == staged["generation_id"]


def test_accepted_schwab_generation_adds_positions_and_transactions(
    tmp_path: Path,
) -> None:
    spec_path = _spec_with_schwab_provider(tmp_path)
    catalog, manifest, _ = baseline.build_catalog(spec_path)
    assert manifest.coverage.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    assert len(catalog["positions"]) == 1
    assert catalog["positions"][0]["mapping_status"] == "MAPPED"
    assert catalog["positions"][0]["strategy_id"] == "strategy-1"
    assert catalog["positions"][0]["market_value"]["value"] == "30"
    assert len(catalog["transactions"]) == 2
    assert len(catalog["source_artifacts"]) == 19
    position_coverage = next(
        item for item in catalog["fact_coverage"] if item["fact_type"] == "positions"
    )
    assert position_coverage["status"] == "PARTIAL"
    assert (
        next(
            item
            for item in catalog["reconciliation_checks"]
            if item["check_id"] == "schwab-provider-nav-to-portfolio-nav"
        )["status"]
        == "PASS_WITH_DECLARED_VARIANCE"
    )
    assert next(
        item
        for item in catalog["reconciliation_checks"]
        if item["check_id"] == "current-position-strategy-attribution"
    ) == {
        "check_id": "current-position-strategy-attribution",
        "mapped_count": 1,
        "market_value_usd": "30",
        "position_count": 1,
        "status": "PASS",
        "unmapped_count": 0,
    }


def test_accepted_schwab_v2_generation_preserves_catalog_schema_version(
    tmp_path: Path,
) -> None:
    spec_path = _spec_with_schwab_provider(tmp_path, catalog_schema_version=2)

    catalog, _, _ = baseline.build_catalog(spec_path)

    artifact = next(
        item
        for item in catalog["source_artifacts"]
        if item["artifact_id"] == "providers/schwab/schwab-generation.json"
    )
    assert artifact["schema_version"] == "2"


@pytest.mark.parametrize(
    ("v2_nav_provenance", "message"),
    (
        ("MISSING", "Schwab provider v2 NAV source artifact is invalid"),
        ("FORGED_SOURCE", "Schwab provider v2 NAV source artifact is unauthorized"),
        ("WRONG_ROLE", "Schwab provider v2 NAV source artifact is unauthorized"),
        ("WRONG_BASIS", "Schwab provider v2 NAV value basis differs"),
    ),
)
def test_schwab_v2_generation_rejects_invalid_nav_provenance(
    tmp_path: Path,
    v2_nav_provenance: str,
    message: str,
) -> None:
    spec_path = _spec_with_schwab_provider(
        tmp_path,
        catalog_schema_version=2,
        v2_nav_provenance=v2_nav_provenance,
    )

    with pytest.raises(baseline.BaselineValidationError, match=message):
        baseline.build_catalog(spec_path)


@pytest.mark.parametrize(
    ("date_field", "drifted_date"),
    (
        ("catalog_artifact_as_of_date", date(2026, 7, 29)),
        ("manifest_as_of_date", date(2026, 7, 31)),
    ),
)
def test_schwab_generation_rejects_catalog_as_of_binding_drift(
    tmp_path: Path,
    date_field: str,
    drifted_date: date,
) -> None:
    spec_path = _spec_with_schwab_provider(
        tmp_path,
        catalog_schema_version=2,
        **{date_field: drifted_date},
    )

    with pytest.raises(
        baseline.BaselineValidationError,
        match="Schwab provider catalog as-of binding differs",
    ):
        baseline.build_catalog(spec_path)


@pytest.mark.parametrize("catalog_schema_version", (1, 2))
def test_provider_ledger_consumer_preserves_supported_catalog_schema_version(
    tmp_path: Path,
    catalog_schema_version: int,
) -> None:
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=catalog_schema_version,
    )

    _, artifacts, links = baseline._provider_ledger(
        ref,
        {"provider-ledger": root},
        date(2026, 7, 30),
    )

    catalog_artifact = next(
        item
        for item in artifacts
        if item.artifact_id == "providers/provider-ledger/provider-ledger.json"
    )
    catalog_link = next(
        item
        for item in links
        if item["artifact_id"] == "providers/provider-ledger/provider-ledger.json"
    )
    assert catalog_artifact.schema_version == str(catalog_schema_version)
    assert catalog_link["schema_version"] == str(catalog_schema_version)


def test_provider_ledger_consumer_preserves_financial_cutoff_with_later_provenance(
    tmp_path: Path,
) -> None:
    """The catalog stays financial-dated while the retained source stays provenance-dated."""

    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        manifest_as_of_date=date(2026, 7, 31),
        retained_source_as_of_date=date(2026, 7, 31),
    )

    catalog, artifacts, links = baseline._provider_ledger(
        ref,
        {"provider-ledger": root},
        date(2026, 7, 30),
    )

    assert catalog["as_of_date"] == "2026-07-30"
    assert {item.artifact_id: item.as_of_date.isoformat() for item in artifacts} == {
        "providers/provider-ledger/generation.json": "2026-07-31",
        "providers/provider-ledger/provider-ledger.json": "2026-07-30",
    }
    assert {item["artifact_id"]: item["as_of_date"] for item in links} == {
        "providers/provider-ledger/generation.json": "2026-07-31",
        "providers/provider-ledger/provider-ledger.json": "2026-07-30",
    }


def test_provider_ledger_consumer_rejects_coherent_financial_cutoff_tampering(
    tmp_path: Path,
) -> None:
    """A rehashed later catalog cannot evade the baseline's selected cutoff."""

    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        catalog_as_of_date=date(2026, 7, 31),
        catalog_artifact_as_of_date=date(2026, 7, 31),
        manifest_as_of_date=date(2026, 7, 31),
    )

    with pytest.raises(
        baseline.BaselineValidationError, match="provider-ledger is newer than baseline"
    ):
        baseline._provider_ledger(
            ref,
            {"provider-ledger": root},
            date(2026, 7, 30),
        )


def test_portfolio_baseline_spec_v3_consumes_schema_valid_provider_ledger_v2(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
    )
    spec["roots"]["provider-ledger"] = str(root)
    spec["schema_version"] = 3
    spec["provider_ledger"] = ref
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    catalog, _, _ = baseline.build_catalog(spec_path)

    artifact = next(
        item
        for item in catalog["source_artifacts"]
        if item["artifact_id"] == "providers/provider-ledger/provider-ledger.json"
    )
    assert artifact["schema_version"] == "2"


@pytest.mark.parametrize(
    ("fact_type", "date_field", "result_index"),
    (
        ("nav_observations", "as_of_date", 0),
        ("flows", "event_date", 1),
        ("transactions", "event_date", 2),
        ("positions", "as_of_date", 3),
    ),
)
def test_provider_ledger_v2_consumes_only_effective_authoritative_account_facts(
    fact_type: str,
    date_field: str,
    result_index: int,
) -> None:
    accounts = [
        {"account_id": f"account-{provider_id}"}
        for provider_id in ("schwab", "alpaca", "ibkr")
    ]
    admissions = [
        {
            "account_id": f"account-{provider_id}",
            "account_reconciliation_sha256": hashlib.sha256(
                provider_id.encode()
            ).hexdigest(),
            "disposition": disposition,
            "effective_end": effective_end,
            "effective_start": effective_start,
            "evidence_layer": "ACCOUNT_LIVE",
            "fact_type": fact_type,
            "pool_id": None,
            "provider_id": provider_id,
            "strategy_id": None,
        }
        for provider_id, disposition, effective_start, effective_end in (
            ("schwab", "AUTHORITATIVE", "2026-07-01", "2026-07-30"),
            ("alpaca", "REFERENCE_ONLY", "2026-07-01", "2026-07-30"),
            ("ibkr", "AUTHORITATIVE", "2026-07-02", "2026-07-30"),
        )
    ]
    rows = []
    for provider_id in ("schwab", "alpaca", "ibkr"):
        row = {
            "account_id": f"account-{provider_id}",
            date_field: "2026-07-01",
            "mapping_status": "MAPPED",
            "provider_id": provider_id,
            "record_id": f"{fact_type}-{provider_id}",
        }
        if fact_type == "positions":
            row["security_id"] = f"security-{provider_id}"
        rows.append(row)
    catalog = {
        "admissions": admissions,
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }
    catalog[fact_type] = rows

    result = baseline._provider_ledger_facts(
        catalog,
        accounts,
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    assert [row["record_id"] for row in result[result_index]] == [f"{fact_type}-schwab"]


def _capital_anchor(
    *,
    account_id: str,
    provider_id: str,
    strategy_id: str | None,
    capital_type: str,
    mapping_status: str,
    event_date: str,
    amount: str,
    record_id: str,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
        "capital_type": capital_type,
        "event_date": event_date,
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "generation-provider-ledger",
        "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
        "mapping_status": mapping_status,
        "nav_recognition_date": event_date,
        "nav_source_artifact_id": "source/csv/dashboard.csv",
        "provider_id": provider_id,
        "record_id": record_id,
        "source_artifact_id": "source/csv/account_activity.csv",
        "source_record_id": record_id,
        "strategy_id": strategy_id,
    }


def _capital_anchor_admission(*, account_id: str, provider_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_reconciliation_sha256": hashlib.sha256(
            f"{provider_id}-capital-anchors".encode()
        ).hexdigest(),
        "disposition": "AUTHORITATIVE",
        "effective_end": "2026-07-30",
        "effective_start": "2026-01-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "capital_anchors",
        "pool_id": None,
        "provider_id": provider_id,
        "strategy_id": None,
    }


def _capital_anchor_catalog(anchors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "admissions": [
            _capital_anchor_admission(
                account_id="account-quantbase", provider_id="quantbase"
            )
        ],
        "capital_anchors": anchors,
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }


def test_provider_ledger_v2_capital_anchor_strategy_allocation_becomes_a_flow() -> None:
    """D2: the sleeve-funding capital anchor is the only admitted flow evidence.

    Bad (pre-fix): capital_anchors is never read by the baseline, so the two
    STRATEGY_CAPITAL_ALLOCATION legs contribute 0 admitted flow records.
    Good (this test, against the fix): they become 2 MAPPED, strategy-tagged
    flow facts, and the opening anchor -- the performance opening basis, not
    a funding event -- is never among them.
    """

    opening = _capital_anchor(
        account_id="account-quantbase",
        provider_id="quantbase",
        strategy_id="strategy-volatis",
        capital_type="OPENING_CAPITAL_ANCHOR",
        mapping_status="MAPPED",
        event_date="2026-01-04",
        amount="5000.00",
        record_id="quantbase-capital-anchor:opening",
    )
    allocations = [
        _capital_anchor(
            account_id="account-quantbase",
            provider_id="quantbase",
            strategy_id="strategy-volatis",
            capital_type="STRATEGY_CAPITAL_ALLOCATION",
            mapping_status="MAPPED",
            event_date=event_date,
            amount=amount,
            record_id=f"quantbase-capital-anchor:{index}",
        )
        for index, (event_date, amount) in enumerate(
            (("2026-02-01", "1000.00"), ("2026-03-01", "1500.00")), start=1
        )
    ]
    catalog = _capital_anchor_catalog([opening, *allocations])

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": "account-quantbase"}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    flows = result[1]
    reconciliation = result[6]
    assert {row["record_id"] for row in flows} == {
        "capital-allocation-flow:quantbase-capital-anchor:1",
        "capital-allocation-flow:quantbase-capital-anchor:2",
    }
    assert all(row["flow_type"] == "EXTERNAL_CONTRIBUTION" for row in flows)
    assert all(row["capital_disposition"] == "EXTERNAL_CAPITAL" for row in flows)
    assert all(row["strategy_id"] == "strategy-volatis" for row in flows)
    assert all(row["mapping_status"] == "MAPPED" for row in flows)
    assert {row["amount"]["value"] for row in flows} == {"1000.00", "1500.00"}
    assert reconciliation["admitted_flow_count"] == 2


def test_provider_ledger_v2_capital_anchor_requires_mapped_strategy() -> None:
    """An unmapped allocation stays non-performance: it must not leak into flows."""

    unmapped = _capital_anchor(
        account_id="account-quantbase",
        provider_id="quantbase",
        strategy_id=None,
        capital_type="STRATEGY_CAPITAL_ALLOCATION",
        mapping_status="UNMAPPED",
        event_date="2026-02-01",
        amount="1000.00",
        record_id="quantbase-capital-anchor:1",
    )
    catalog = _capital_anchor_catalog([unmapped])

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": "account-quantbase"}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    assert result[1] == []
    assert result[6]["admitted_flow_count"] == 0


def test_provider_ledger_v2_without_capital_anchors_key_does_not_raise() -> None:
    """A catalog that predates capital_anchors has no anchors, not a bad shape."""

    catalog = {
        "admissions": [],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": "account-a"}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    assert result[1] == []


def test_coverage_evidence_flows_status_flips_once_capital_anchor_flows_are_admitted() -> (
    None
):
    """D2: gates[flows] moves off NO_REPORTED_EVENTS once the anchor-derived
    flow is admitted -- the exact fact-matrix note in the flip list."""

    account = {
        "account_id": "account-quantbase",
        "base_currency": "USD",
        "provider": "quantbase",
        "role": "INVESTMENT",
    }
    admission = {
        "account_id": "account-quantbase",
        "disposition": "AUTHORITATIVE",
        "effective_end": "2026-07-30",
        "effective_start": "2026-01-01",
        "fact_type": "flows",
    }
    admitted_flow = {
        "account_id": "account-quantbase",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "1000.00"},
        "event_date": "2026-02-01",
        "record_id": "capital-allocation-flow:quantbase-capital-anchor:1",
    }

    def flows_status(flows: list[dict[str, Any]]) -> str:
        matrix, _, _ = baseline._coverage_evidence(
            accounts=[account],
            assignments=[],
            provider_admissions=[admission],
            provider_coverage=[],
            nav=[],
            positions=[],
            transactions=[],
            flows=flows,
            as_of_date=date(2026, 7, 30),
        )
        row = next(
            item
            for item in matrix
            if item["fact_type"] == "flows"
            and item["account_id"] == "account-quantbase"
        )
        return str(row["status"])

    assert flows_status([]) == "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"
    assert flows_status([admitted_flow]) == "OBSERVED_NOT_COMPLETENESS_PROOF"


def test_provider_ledger_v2_capital_anchor_native_currency_is_not_ready() -> None:
    """B7/M9 fail-closed rule: a native-currency allocation (Matrixport's
    subscription currency, e.g. USDT) must never be treated as USD.  It is
    excluded from flows and recorded as an explicit NOT_READY finding, not
    silently admitted at par and not silently dropped."""

    account_id = "account-matrixport"
    native = _capital_anchor(
        account_id=account_id,
        provider_id="matrixport",
        strategy_id="strategy-robuxio",
        capital_type="STRATEGY_CAPITAL_ALLOCATION",
        mapping_status="MAPPED",
        event_date="2026-02-01",
        amount="500.00",
        record_id="matrixport-capital-anchor:1",
    )
    native["amount"]["currency"] = "USDT"
    catalog = {
        "admissions": [
            _capital_anchor_admission(account_id=account_id, provider_id="matrixport")
        ],
        "capital_anchors": [native],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": account_id}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    flows, quality_findings = result[1], result[5]
    assert flows == []
    finding = next(
        item
        for item in quality_findings
        if item["record_id"] == "matrixport-capital-anchor:1"
    )
    assert finding["status"] == "NOT_READY_AWAITING_CONTRACT_FX"
    assert finding["currency"] == "USDT"
    assert finding["provider_id"] == "matrixport"
    assert finding["fact_type"] == "capital_anchors"


def test_provider_ledger_v2_capital_anchor_flow_requires_usd_currency() -> None:
    """Defense in depth: the projection helper itself refuses a non-USD
    anchor, so no other caller can silently mint a mislabeled USD flow."""

    anchor = _capital_anchor(
        account_id="account-matrixport",
        provider_id="matrixport",
        strategy_id="strategy-robuxio",
        capital_type="STRATEGY_CAPITAL_ALLOCATION",
        mapping_status="MAPPED",
        event_date="2026-02-01",
        amount="500.00",
        record_id="matrixport-capital-anchor:1",
    )
    anchor["amount"]["currency"] = "USDT"

    with pytest.raises(baseline.BaselineValidationError, match="requires a USD amount"):
        baseline._capital_anchor_flow(anchor)


def test_provider_ledger_v2_matrixport_capital_anchor_becomes_a_flow_once_usd_admitted() -> (
    None
):
    """The Matrixport half of D2 is blocked on T4 (lane cp/c1-t4-ledger):
    ``_matrixport_capital_anchors`` computes a contract-admitted USD
    ``opening_nav`` only for the OPENING anchor; a later
    ``STRATEGY_CAPITAL_ALLOCATION`` leg carries only its native subscription
    currency today, so the pinned catalog (run baseline-950733c, ledger
    3b1445ee) has zero Matrixport STRATEGY_CAPITAL_ALLOCATION anchors and
    this code path is currently exercised by Quantbase alone.

    This test simulates T4's landed state -- a USD-denominated allocation
    anchor -- to prove T5's projection is already correct and provider-
    agnostic for it.  It will flip from exercising this synthetic anchor to
    exercising T4's real output the day T4 merges a USD conversion, with no
    further change to portfolio_baseline.py required.
    """

    account_id = "account-matrixport"
    anchor = _capital_anchor(
        account_id=account_id,
        provider_id="matrixport",
        strategy_id="strategy-robuxio",
        capital_type="STRATEGY_CAPITAL_ALLOCATION",
        mapping_status="MAPPED",
        event_date="2026-02-01",
        amount="500.00",
        record_id="matrixport-capital-anchor:1",
    )
    catalog = {
        "admissions": [
            _capital_anchor_admission(account_id=account_id, provider_id="matrixport")
        ],
        "capital_anchors": [anchor],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": account_id}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    flows = result[1]
    assert [row["record_id"] for row in flows] == [
        "capital-allocation-flow:matrixport-capital-anchor:1"
    ]
    assert flows[0]["flow_type"] == "EXTERNAL_CONTRIBUTION"
    assert flows[0]["provider"] == "MATRIXPORT"


@pytest.mark.parametrize(
    ("observed_at", "expects_position"),
    (
        ("2026-09-18T03:59:40.004417+00:00", True),
        ("2026-09-18T04:00:00+00:00", False),
    ),
)
def test_provider_ledger_schwab_current_position_uses_new_york_financial_day(
    observed_at: str, expects_position: bool
) -> None:
    """A native UTC position date remains authoritative, but EOD selection is New York."""

    catalog = {
        "admissions": [
            {
                "account_id": "account-schwab",
                "account_reconciliation_sha256": "a" * 64,
                "disposition": "AUTHORITATIVE",
                "effective_end": "2026-09-18",
                "effective_start": "2026-09-18",
                "evidence_layer": "ACCOUNT_LIVE",
                "fact_type": "positions",
                "pool_id": None,
                "provider_id": "schwab",
                "strategy_id": None,
            }
        ],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [
            {
                "account_id": "account-schwab",
                "as_of_date": "2026-09-18",
                "mapping_status": "MAPPED",
                "observed_at": observed_at,
                "provider_id": "schwab",
                "record_id": "schwab:position-native-utc-18",
                "security_id": "security-schwab-current",
                "source_artifact_id": "source/schwab-current.json",
                "source_record_id": "position-native-utc-18",
            }
        ],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    if expects_position:
        result = baseline._provider_ledger_facts(
            catalog,
            [{"account_id": "account-schwab"}],
            [],
            [],
            [],
            [],
            date(2026, 9, 17),
        )
        assert [row["record_id"] for row in result[3]] == [
            "schwab:position-native-utc-18"
        ]
    else:
        with pytest.raises(
            baseline.BaselineValidationError,
            match="provider-ledger fact is newer than baseline",
        ):
            baseline._provider_ledger_facts(
                catalog,
                [{"account_id": "account-schwab"}],
                [],
                [],
                [],
                [],
                date(2026, 9, 17),
            )


@pytest.mark.parametrize(
    ("admission_updates", "fact_updates"),
    (
        ({"provider_id": "alpaca"}, {}),
        ({"fact_type": "flows"}, {}),
        ({"account_id": "account-other"}, {}),
        ({"strategy_id": "strategy-1"}, {}),
        ({}, {"provider_id": "alpaca"}),
        ({}, {"account_id": "account-other"}),
    ),
)
def test_provider_ledger_v2_fact_authority_matches_the_exact_account_scope(
    admission_updates: dict[str, object],
    fact_updates: dict[str, object],
) -> None:
    admission = {
        "account_id": "account-schwab",
        "account_reconciliation_sha256": "a" * 64,
        "disposition": "AUTHORITATIVE",
        "effective_end": "2026-07-30",
        "effective_start": "2026-07-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "nav_observations",
        "pool_id": None,
        "provider_id": "schwab",
        "strategy_id": None,
    }
    admission.update(admission_updates)
    fact = {
        "account_id": "account-schwab",
        "as_of_date": "2026-07-01",
        "mapping_status": "MAPPED",
        "provider_id": "schwab",
        "record_id": "candidate-nav",
    }
    fact.update(fact_updates)
    catalog = {
        "admissions": [admission],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [fact],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    result = baseline._provider_ledger_facts(
        catalog,
        [
            {"account_id": "account-schwab"},
            {"account_id": "account-other"},
        ],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    assert result[0] == []


@pytest.mark.parametrize("malformation", ("MISSING_NULLABLE_KEY", "UNKNOWN_KEY"))
def test_provider_ledger_v2_rejects_malformed_admission_shape(
    malformation: str,
) -> None:
    admission = {
        "account_id": "account-schwab",
        "account_reconciliation_sha256": "a" * 64,
        "disposition": "AUTHORITATIVE",
        "effective_end": "2026-07-30",
        "effective_start": "2026-07-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "nav_observations",
        "pool_id": None,
        "provider_id": "schwab",
        "strategy_id": None,
    }
    if malformation == "MISSING_NULLABLE_KEY":
        admission.pop("pool_id")
    else:
        admission["unexpected"] = None
    catalog = {
        "admissions": [admission],
        "catalog_schema_version": 2,
        "flows": [],
        "nav_observations": [],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    with pytest.raises(
        baseline.BaselineValidationError,
        match="provider-ledger admission keys differ",
    ):
        baseline._provider_ledger_facts(
            catalog,
            [{"account_id": "account-schwab"}],
            [],
            [],
            [],
            [],
            date(2026, 7, 30),
        )


def _strategy_return_admission(
    *,
    account_id: str,
    disposition: str,
    provider_id: str,
    strategy_id: str,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "account_reconciliation_sha256": "a" * 64,
        "disposition": disposition,
        "effective_end": "2026-07-30",
        "effective_start": "2026-07-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "strategy_returns",
        "pool_id": None,
        "provider_id": provider_id,
        "strategy_id": strategy_id,
    }


def test_provider_ledger_v2_strategy_returns_exclude_reference_only_strategies() -> (
    None
):
    catalog = {
        "admissions": [
            _strategy_return_admission(
                account_id="account-required",
                disposition="AUTHORITATIVE",
                provider_id="schwab",
                strategy_id="strategy-required",
            ),
            _strategy_return_admission(
                account_id="account-reference",
                disposition="REFERENCE_ONLY",
                provider_id="alpaca",
                strategy_id="strategy-reference",
            ),
        ],
        "catalog_schema_version": 2,
        "strategy_returns": [
            {
                "component_account_ids": ["account-required"],
                "date": "2026-07-15",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "schwab",
                "strategy_id": "strategy-required",
            },
            {
                "component_account_ids": ["account-reference"],
                "date": "2026-07-15",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "alpaca",
                "strategy_id": "strategy-reference",
            },
        ],
    }

    rows = baseline._admitted_strategy_returns(catalog)

    assert [row["strategy_id"] for row in rows] == ["strategy-required"]


@pytest.mark.parametrize(
    ("admission_updates", "return_updates"),
    (
        ({"provider_id": "alpaca"}, {}),
        ({"strategy_id": "strategy-other"}, {}),
        ({"account_id": "account-other"}, {}),
        ({}, {"date": "2026-07-31"}),
        ({}, {"evidence_layer": "PROVIDER_LIVE"}),
    ),
)
def test_provider_ledger_v2_strategy_return_authority_matches_exact_scope(
    admission_updates: dict[str, object],
    return_updates: dict[str, object],
) -> None:
    admission = _strategy_return_admission(
        account_id="account-required",
        disposition="AUTHORITATIVE",
        provider_id="schwab",
        strategy_id="strategy-required",
    )
    admission.update(admission_updates)
    strategy_return = {
        "component_account_ids": ["account-required"],
        "date": "2026-07-15",
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": "schwab",
        "strategy_id": "strategy-required",
    }
    strategy_return.update(return_updates)

    rows = baseline._admitted_strategy_returns(
        {
            "admissions": [admission],
            "catalog_schema_version": 2,
            "strategy_returns": [strategy_return],
        }
    )

    assert rows == []


def test_provider_ledger_v2_strategy_return_rejects_unbound_multi_provider_scope() -> (
    None
):
    strategy_return = {
        "component_account_ids": ["account-ibkr", "account-schwab"],
        "date": "2026-07-15",
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": "multi-provider",
        "strategy_id": "strategy-required",
    }
    rows = baseline._admitted_strategy_returns(
        {
            "admissions": [
                _strategy_return_admission(
                    account_id=f"account-{provider_id}",
                    disposition="AUTHORITATIVE",
                    provider_id=provider_id,
                    strategy_id="strategy-required",
                )
                for provider_id in ("ibkr", "schwab")
            ],
            "catalog_schema_version": 2,
            "strategy_returns": [strategy_return],
        }
    )

    assert rows == []


def test_provider_ledger_v2_strategy_return_requires_every_component_account() -> None:
    strategy_return = {
        "component_account_ids": ["account-admitted", "account-unbound"],
        "date": "2026-07-15",
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": "schwab",
        "strategy_id": "strategy-required",
    }

    rows = baseline._admitted_strategy_returns(
        {
            "admissions": [
                _strategy_return_admission(
                    account_id="account-admitted",
                    disposition="AUTHORITATIVE",
                    provider_id="schwab",
                    strategy_id="strategy-required",
                )
            ],
            "catalog_schema_version": 2,
            "strategy_returns": [strategy_return],
        }
    )

    assert rows == []


def test_provider_ledger_v1_preserves_legacy_strategy_returns() -> None:
    legacy_row = {
        "date": "2026-07-15",
        "return": "0.01",
        "strategy_id": "strategy-legacy",
    }

    rows = baseline._admitted_strategy_returns(
        {"catalog_schema_version": 1, "strategy_returns": [legacy_row]}
    )

    assert rows == [legacy_row]


def test_provider_ledger_v1_preserves_legacy_fact_consumption() -> None:
    catalog = {
        "catalog_schema_version": 1,
        "flows": [],
        "nav_observations": [
            {
                "account_id": "account-schwab",
                "as_of_date": "2026-07-01",
                "mapping_status": "MAPPED",
                "provider_id": "schwab",
                "record_id": "legacy-nav",
            }
        ],
        "positions": [],
        "provider_coverage": [],
        "quality_findings": [],
        "securities": [],
        "transactions": [],
    }

    result = baseline._provider_ledger_facts(
        catalog,
        [{"account_id": "account-schwab"}],
        [],
        [],
        [],
        [],
        date(2026, 7, 30),
    )

    assert [row["record_id"] for row in result[0]] == ["legacy-nav"]


def test_provider_ledger_consumer_rejects_manifest_schema_version_drift(
    tmp_path: Path,
) -> None:
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        manifest_catalog_schema_version=1,
    )

    with pytest.raises(
        baseline.BaselineValidationError,
        match="provider-ledger catalog manifest binding differs",
    ):
        baseline._provider_ledger(
            ref,
            {"provider-ledger": root},
            date(2026, 7, 30),
        )


@pytest.mark.parametrize(
    ("date_field", "drifted_date", "message"),
    (
        (
            "catalog_artifact_as_of_date",
            date(2026, 7, 29),
            "provider-ledger catalog as-of binding differs",
        ),
        (
            "manifest_as_of_date",
            date(2026, 7, 31),
            "provider-ledger generation date is not artifact-derived",
        ),
    ),
)
def test_provider_ledger_consumer_rejects_catalog_as_of_binding_drift(
    tmp_path: Path,
    date_field: str,
    drifted_date: date,
    message: str,
) -> None:
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        **{date_field: drifted_date},
    )

    with pytest.raises(baseline.BaselineValidationError, match=message):
        baseline._provider_ledger(
            ref,
            {"provider-ledger": root},
            date(2026, 7, 30),
        )


def test_strategy_for_date_is_effective_dated_and_fails_on_overlap() -> None:
    assignments = [
        {
            "account_id": "account-a",
            "effective_end": "2026-01-31",
            "effective_start": "2026-01-01",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-old",
        },
        {
            "account_id": "account-a",
            "effective_end": None,
            "effective_start": "2026-02-01",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-current",
        },
    ]

    assert baseline._strategy_for_date(assignments, "account-a", date(2026, 1, 31)) == (
        baseline.MappingStatus.MAPPED,
        "strategy-old",
    )
    assert baseline._strategy_for_date(assignments, "account-a", date(2026, 2, 1)) == (
        baseline.MappingStatus.MAPPED,
        "strategy-current",
    )
    assert baseline._strategy_for_date(
        assignments, "account-missing", date(2026, 2, 1)
    ) == (baseline.MappingStatus.UNMAPPED, None)

    overlapping = [
        *assignments,
        {
            "account_id": "account-a",
            "effective_end": None,
            "effective_start": "2026-02-01",
            "mapping_status": "MAPPED",
            "strategy_id": "strategy-overlap",
        },
    ]
    with pytest.raises(
        baseline.BaselineValidationError,
        match="multiple mapped strategies overlap",
    ):
        baseline._strategy_for_date(overlapping, "account-a", date(2026, 2, 1))

    with pytest.raises(
        baseline.BaselineValidationError,
        match="explicit effective_start",
    ):
        baseline._strategy_for_date(
            [
                {
                    "account_id": "account-a",
                    "effective_end": None,
                    "effective_start": None,
                    "mapping_status": "MAPPED",
                    "strategy_id": "strategy-unknown-start",
                }
            ],
            "account-a",
            date(2026, 2, 1),
        )


def test_source_drift_nav_divergence_and_replay_difference_fail_closed(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path / "drift")
    source_root = Path(spec["roots"]["synthetic"])
    (source_root / "schwab_nav.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(baseline.BaselineValidationError, match="hash differs"):
        baseline.build_catalog(spec_path)

    spec_path, spec = _spec(tmp_path / "nav-mismatch")
    source_root = Path(spec["roots"]["synthetic"])
    nav_path = source_root / "schwab_nav.csv"
    rows = list(csv.DictReader(nav_path.read_text(encoding="utf-8").splitlines()))
    rows[0]["nav"] = "999"
    _csv(nav_path, "schwab-nav", rows)
    spec["sources"]["schwab-nav"]["expected_sha256"] = hashlib.sha256(
        nav_path.read_bytes()
    ).hexdigest()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(baseline.BaselineValidationError, match="do not reconcile"):
        baseline.build_catalog(spec_path)

    spec_path, spec = _spec(tmp_path / "replay")
    spec["replay"]["total_liquid_usd"]["value"] = "999"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(baseline.BaselineValidationError, match="frozen replay differs"):
        baseline.build_catalog(spec_path)


def _artifacts_from_sources(
    sources: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Compute catalog source-artifact identities from spec source refs."""
    result: dict[str, dict[str, Any]] = {}
    for source_key, ref in sources.items():
        artifact_id = (
            f"sources/{source_key}/"
            f"{hashlib.sha256(ref['relative_path'].encode()).hexdigest()[:16]}"
        )
        result[source_key] = {
            "artifact_id": artifact_id,
            "sha256": ref["expected_sha256"],
        }
    return result


def _add_governed_source(
    spec: dict[str, Any], source_root: Path, source_key: str, payload: dict[str, Any]
) -> None:
    """Write a governed HWM evidence file and bind it into the spec sources.

    The governed roles require ``schema_name == source_key`` (the builder
    enforces the binding); the artifact hash is the admitted evidence hash.
    """
    content = canonical_json_bytes(payload)
    relative_path = f"{source_key}.json"
    (source_root / relative_path).write_bytes(content)
    spec["sources"][source_key] = {
        "as_of_date": "2026-07-30",
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "relative_path": relative_path,
        "root_id": "synthetic",
        "schema_name": source_key,
        "schema_version": "1",
    }


def _governed_hwm_sources(spec: dict[str, Any], source_root: Path) -> None:
    """Admit the anchor-acceptance and per-lane calendar authority artifacts."""
    _add_governed_source(
        spec,
        source_root,
        "hwm-anchor-acceptance",
        {
            "anchor_date": "2026-07-01",
            "hwm_policy_id": "PORT-HWM-3",
            "index_base_value": "1.0",
        },
    )
    for lane in ("account", "provider", "legacy"):
        _add_governed_source(
            spec,
            source_root,
            f"hwm-calendar-authority-{lane}",
            {
                "authority_id": f"synthetic-{lane}-calendar",
                "authority_version": "1",
                "days": "SYNTHETIC admitted calendar expectation",
            },
        )


def _hwm_inputs_valid_envelope(
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a synthetic, fully valid hwm_inputs envelope for testing.

    The governed perimeter is the nine allocated systematic strategies, so
    the expected keyspace is strategy-1..strategy-9 over
    2026-07-01..2026-07-30 (270 rows).  strategy-1 rides the ACCOUNT lane
    (mapped account-schwab-a), strategy-2 the PROVIDER lane, and the
    remaining strategies the LEGACY lane; every row is UNAVAILABLE because
    no hash-bound NAV-rollforward evidence or admitted provider/legacy
    capital evidence exists in the fixture.
    """
    anchor_artifact = artifacts["hwm-anchor-acceptance"]
    periods: list[dict[str, Any]] = []
    lanes = {
        **{"strategy-1": "ACCOUNT", "strategy-2": "PROVIDER"},
        **{f"strategy-{index}": "LEGACY" for index in range(3, 10)},
    }
    day = date(2026, 7, 1)
    while day <= date(2026, 7, 30):
        for strategy_id, lane in sorted(lanes.items()):
            periods.append(
                {
                    "strategy_id": strategy_id,
                    "date": day.isoformat(),
                    "lane": lane,
                    "disposition": "UNAVAILABLE",
                    "beginning_capital_usd": None,
                    "return_source_record_id": None,
                    "flow_status": "MISSING",
                    "fx_status": "MISSING",
                }
            )
        day += timedelta(days=1)
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-07-01",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-07-30T00:00:00Z",
                "accepted_by": "synthetic-builder",
                "source_artifact_id": anchor_artifact["artifact_id"],
                "source_artifact_sha256": anchor_artifact["sha256"],
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synthetic-account-calendar",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": artifacts[
                    "hwm-calendar-authority-account"
                ]["artifact_id"],
            },
            {
                "lane": "PROVIDER",
                "authority_id": "synthetic-provider-calendar",
                "authority_version": "1",
                "calendar_kind": "PROVIDER_PUBLICATION",
                "admitted_source_artifact_id": artifacts[
                    "hwm-calendar-authority-provider"
                ]["artifact_id"],
            },
            {
                "lane": "LEGACY",
                "authority_id": "synthetic-legacy-calendar",
                "authority_version": "1",
                "calendar_kind": "ALWAYS_ON",
                "admitted_source_artifact_id": artifacts[
                    "hwm-calendar-authority-legacy"
                ]["artifact_id"],
            },
        ],
        "periods": periods,
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {
                "matched": True,
                "delta_usd": "0",
            },
        },
    }


def _write_envelope(
    spec_path: Path, spec: dict[str, Any], envelope: dict[str, Any]
) -> None:
    """Persist an envelope mutation and re-bind its expected hash."""
    source_root = Path(spec["roots"]["synthetic"])
    envelope_bytes = canonical_json_bytes(envelope)
    (source_root / "hwm_inputs.json").write_bytes(envelope_bytes)
    spec["sources"]["hwm-inputs"]["expected_sha256"] = hashlib.sha256(
        envelope_bytes
    ).hexdigest()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


def _spec_with_hwm_inputs(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Build a spec with the optional hwm-inputs source wired in."""
    spec_path, spec = _spec(tmp_path)
    source_root = Path(spec["roots"]["synthetic"])
    _governed_hwm_sources(spec, source_root)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope_bytes = canonical_json_bytes(envelope)
    (source_root / "hwm_inputs.json").write_bytes(envelope_bytes)
    spec["sources"]["hwm-inputs"] = {
        "as_of_date": "2026-07-30",
        "expected_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
        "relative_path": "hwm_inputs.json",
        "root_id": "synthetic",
        "schema_name": "hwm-inputs",
        "schema_version": "1",
    }
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, spec


def test_spec_and_catalog_schemas_are_closed_and_preserve_yagni_boundary() -> None:
    spec_schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "baseline-spec.schema.json"
        ).read_text(encoding="utf-8")
    )
    catalog_schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "baseline-catalog.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert spec_schema["additionalProperties"] is False
    assert (
        set(spec_schema["properties"]["sources"]["required"]) == baseline._SOURCE_KEYS
    )
    # hwm-inputs and the governed HWM evidence roles must be optional.
    source_properties = spec_schema["properties"]["sources"]["properties"]
    for optional_key in sorted(baseline._HWM_GOVERNED_SOURCE_KEYS | {"hwm-inputs"}):
        assert optional_key in source_properties
        assert optional_key not in spec_schema["properties"]["sources"]["required"]
    assert catalog_schema["additionalProperties"] is False
    assert catalog_schema["properties"]["positions"]["items"] == {"type": "object"}
    assert spec_schema["properties"]["schema_version"] == {"enum": [1, 2, 3]}
    assert catalog_schema["properties"]["strategies"]["minItems"] == 9
    assert catalog_schema["properties"]["strategies"]["maxItems"] == 9
    # The conditional disposition invariants must be encoded in the schema.
    period_rules = catalog_schema["properties"]["hwm_inputs"]["properties"]["periods"][
        "items"
    ]["allOf"]
    assert any(
        rule.get("then", {}).get("properties", {}).get("beginning_capital_usd")
        == {"type": "null"}
        for rule in period_rules
    )
    # The embedded acceptance record must be declared in the closed catalog
    # schema and coupled to the envelope in both directions (PR #284 review).
    acceptance = catalog_schema["properties"]["hwm_acceptance_record"]
    assert acceptance["additionalProperties"] is False
    assert set(acceptance["required"]) == {
        "anchor_date",
        "hwm_policy_id",
        "index_base_value",
    }
    coupling = catalog_schema["allOf"]
    assert any(
        rule.get("if", {}).get("required") == ["hwm_inputs"]
        and rule.get("then", {}).get("required") == ["hwm_acceptance_record"]
        for rule in coupling
    )
    assert any(
        rule.get("if", {}).get("required") == ["hwm_acceptance_record"]
        and rule.get("then", {}).get("required") == ["hwm_inputs"]
        for rule in coupling
    )


# ── hwm_inputs envelope tests ─────────────────────────────────────────────


def test_hwm_inputs_present_populates_catalog_key(tmp_path: Path) -> None:
    spec_path, _spec_data = _spec_with_hwm_inputs(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    assert "hwm_inputs" in catalog
    hwm = catalog["hwm_inputs"]
    assert hwm["contract_version"] == 1
    assert hwm["anchor"]["anchor_date"] == "2026-07-01"
    assert hwm["anchor"]["hwm_policy_id"] == "PORT-HWM-3"
    # 9 perimeter strategies x 30 days (2026-07-01..2026-07-30).
    assert len(hwm["periods"]) == 270
    assert {row["disposition"] for row in hwm["periods"]} == {"UNAVAILABLE"}
    assert hwm["reconciliation"]["period_capital_method"] == "NAV_ROLLFORWARD"
    # The hwm-inputs source artifact must appear in source_artifacts.
    artifact_ids = {item["artifact_id"] for item in catalog["source_artifacts"]}
    hwm_artifacts = [
        item
        for item in catalog["source_artifacts"]
        if item["source_key"] == "hwm-inputs"
    ]
    assert len(hwm_artifacts) == 1
    assert hwm_artifacts[0]["artifact_id"] in artifact_ids
    # The embedded acceptance record must be present and equal the governed
    # hwm-anchor-acceptance source content on every governed field.
    assert catalog["hwm_acceptance_record"] == {
        "anchor_date": "2026-07-01",
        "hwm_policy_id": "PORT-HWM-3",
        "index_base_value": "1.0",
    }


def test_hwm_inputs_absent_omits_catalog_key(tmp_path: Path) -> None:
    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    assert "hwm_inputs" not in catalog
    assert "hwm_acceptance_record" not in catalog


def test_hwm_inputs_absent_output_is_byte_identical_to_pre_envelope_baseline(
    tmp_path: Path,
) -> None:
    """Golden byte-identity regression: a spec without the hwm-inputs source
    must produce a catalog and generation manifest with exactly these hashes.

    The pins moved once, with cluster 2 task T1 (the catalog gained the
    ``current_capital`` and ``audit_snapshot`` blocks and the two audit checks
    left ``reconciliation_checks``; the builder version went 1.2.0 -> 1.3.0).
    ``test_allocations_stay_byte_identical_to_the_dated_audit_snapshot`` proves
    that nothing else moved: undoing exactly those changes reproduces the
    previous catalog hash ``fde46f73...``."""
    spec_path, _spec_data = _spec(tmp_path)
    output_root = tmp_path / "private-output"
    evidence = baseline.stage_baseline(spec_path, output_root)
    generation_dir = output_root / "generations" / evidence["generation_id"]
    catalog_bytes = (generation_dir / "portfolio-baseline.json").read_bytes()
    manifest_bytes = (generation_dir / "generation.json").read_bytes()
    assert b'"hwm_inputs"' not in catalog_bytes
    assert (
        hashlib.sha256(catalog_bytes).hexdigest()
        == "cd5751c7dd6d70ccd38fdfe4d5c8610d00d094dd7aabac4eea7b7574896f6636"
    )
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == "2940ca6db27414bb8f0c7fbe9b67d237510009db4b5baa271ac7a245f33534d8"
    )


def test_hwm_inputs_anchor_date_after_as_of_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["anchor"]["anchor_date"] = "2026-08-01"  # after as_of 2026-07-30
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="anchor_date must not be after catalog as_of_date",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_provenance_unresolved_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["anchor"]["acceptance_provenance"]["source_artifact_id"] = (
        "nonexistent-artifact"
    )
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="does not resolve to an admitted source artifact",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_provenance_sha256_mismatch_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    # Keep a valid artifact_id but change sha256.
    envelope["anchor"]["acceptance_provenance"]["source_artifact_sha256"] = "a" * 64
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="sha256 does not match catalog source_artifact",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_provenance_observation_artifact_role_fails(
    tmp_path: Path,
) -> None:
    """Acceptance provenance must resolve to the anchor-acceptance role, not
    an arbitrary observation artifact."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope = _hwm_inputs_valid_envelope(artifacts)
    observation = artifacts["alpaca-events"]
    envelope["anchor"]["acceptance_provenance"]["source_artifact_id"] = observation[
        "artifact_id"
    ]
    envelope["anchor"]["acceptance_provenance"]["source_artifact_sha256"] = observation[
        "sha256"
    ]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="has schema role 'synthetic-alpaca-events'",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_accepted_at_not_iso_datetime_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    # Impossible calendar values must be rejected by real datetime parsing,
    # not by digit-placement patterns.
    envelope["anchor"]["acceptance_provenance"]["accepted_at"] = "2026-99-99T25:61:61Z"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="must be an ISO datetime",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_accepted_at_without_timezone_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["anchor"]["acceptance_provenance"]["accepted_at"] = (
        "2026-07-30T00:00:00"  # naive: no offset
    )
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="must be an ISO datetime with a timezone offset",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_excluded_by_policy_requires_policy_exclusion(
    tmp_path: Path,
) -> None:
    """EXCLUDED_BY_POLICY is not a bare enum label: strategy-1 is included in
    the admitted PORT-HWM-3 perimeter, so labeling it excluded must fail."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "EXCLUDED_BY_POLICY"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="is not named in the admitted PORT-HWM-3 exclusions",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_duplicate_period_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"].append(envelope["periods"][0].copy())
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="duplicate period",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_available_period_null_capital_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "AVAILABLE"
    envelope["periods"][0]["beginning_capital_usd"] = None
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE period must have non-null beginning_capital_usd",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_available_period_null_return_reference_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "AVAILABLE"
    envelope["periods"][0]["beginning_capital_usd"] = "10"
    envelope["periods"][0]["return_source_record_id"] = None
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE period must have non-null return_source_record_id",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_not_admitted_period_has_null_capital_and_return(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "NOT_ADMITTED"
    _write_envelope(spec_path, spec, envelope)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    row = catalog["hwm_inputs"]["periods"][0]
    assert row["disposition"] == "NOT_ADMITTED"


def test_hwm_inputs_not_admitted_with_non_null_capital_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "NOT_ADMITTED"
    envelope["periods"][0]["beginning_capital_usd"] = "10"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="NOT_ADMITTED period must have null beginning_capital_usd",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unavailable_with_non_null_capital_fails(tmp_path: Path) -> None:
    """An UNAVAILABLE row must never carry capital (disposition invariant)."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["beginning_capital_usd"] = "10"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="UNAVAILABLE period must have null beginning_capital_usd",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unavailable_with_non_null_return_reference_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["return_source_record_id"] = "return-ghost"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="UNAVAILABLE period must have null return_source_record_id",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_confirmed_zero_flow_without_source_records_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["flow_status"] = "CONFIRMED_ZERO"
    envelope["periods"][0]["flow_source_record_ids"] = []
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="CONFIRMED_ZERO flow_status requires non-empty",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_confirmed_one_fx_without_source_records_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["fx_status"] = "CONFIRMED_ONE"
    envelope["periods"][0]["fx_source_record_ids"] = []
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="CONFIRMED_ONE fx_status requires non-empty fx_source_record_ids",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_confirmed_zero_fabricated_attestation_fails(
    tmp_path: Path,
) -> None:
    """A non-empty but unresolvable attestation reference must fail closed."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["flow_status"] = "CONFIRMED_ZERO"
    envelope["periods"][0]["flow_source_record_ids"] = [
        "sources/hwm-flow-attestation/0000000000000000"
    ]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="does not resolve to an admitted",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_confirmed_zero_wrong_role_attestation_fails(
    tmp_path: Path,
) -> None:
    """An attestation must resolve to the flow-attestation role, not to an
    arbitrary admitted observation artifact."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope = _hwm_inputs_valid_envelope(artifacts)
    envelope["periods"][0]["flow_status"] = "CONFIRMED_ZERO"
    envelope["periods"][0]["flow_source_record_ids"] = [
        artifacts["schwab-flows"]["artifact_id"]
    ]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="does not resolve to an admitted hwm-flow-attestation",
    ):
        baseline.build_catalog(spec_path)


def _envelope_with_flow_attestation(
    spec_path: Path,
    spec: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    """Admit an hwm-flow-attestation record and reference it from the first
    period (strategy-1, 2026-07-01) with CONFIRMED_ZERO flow status."""
    source_root = Path(spec["roots"]["synthetic"])
    _add_governed_source(spec, source_root, "hwm-flow-attestation", attestation)
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope = _hwm_inputs_valid_envelope(artifacts)
    envelope["periods"][0]["flow_status"] = "CONFIRMED_ZERO"
    envelope["periods"][0]["flow_source_record_ids"] = [
        artifacts["hwm-flow-attestation"]["artifact_id"]
    ]
    _write_envelope(spec_path, spec, envelope)


def test_hwm_inputs_confirmed_zero_resolved_attestation_passes(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    _envelope_with_flow_attestation(
        spec_path,
        spec,
        {
            "strategy_id": "strategy-1",
            "date": "2026-07-01",
            "flow_status": "CONFIRMED_ZERO",
        },
    )
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    assert catalog["hwm_inputs"]["periods"][0]["flow_status"] == "CONFIRMED_ZERO"


def test_hwm_inputs_attestation_wrong_date_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    _envelope_with_flow_attestation(
        spec_path,
        spec,
        {
            "strategy_id": "strategy-1",
            "date": "2026-07-02",  # period is 2026-07-01
            "flow_status": "CONFIRMED_ZERO",
        },
    )
    with pytest.raises(
        baseline.BaselineValidationError,
        match="flow attestation date does not match the period",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_attestation_wrong_strategy_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    _envelope_with_flow_attestation(
        spec_path,
        spec,
        {
            "strategy_id": "strategy-2",  # period is strategy-1
            "date": "2026-07-01",
            "flow_status": "CONFIRMED_ZERO",
        },
    )
    with pytest.raises(
        baseline.BaselineValidationError,
        match="flow attestation strategy_id does not match the period",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_attestation_wrong_asserted_value_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    _envelope_with_flow_attestation(
        spec_path,
        spec,
        {
            "strategy_id": "strategy-1",
            "date": "2026-07-01",
            "flow_status": "OBSERVED",  # must assert CONFIRMED_ZERO
        },
    )
    with pytest.raises(
        baseline.BaselineValidationError,
        match="flow attestation must assert flow_status CONFIRMED_ZERO",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_available_missing_flow_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "AVAILABLE"
    envelope["periods"][0]["beginning_capital_usd"] = "10"
    envelope["periods"][0]["return_source_record_id"] = "return-1"
    envelope["periods"][0]["flow_status"] = "MISSING"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE period must not have MISSING flow_status",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_available_missing_fx_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["disposition"] = "AVAILABLE"
    envelope["periods"][0]["beginning_capital_usd"] = "10"
    envelope["periods"][0]["return_source_record_id"] = "return-1"
    envelope["periods"][0]["flow_status"] = "OBSERVED"
    envelope["periods"][0]["fx_status"] = "MISSING"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE period must not have MISSING fx_status",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_hwm_policy_id_wrong_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["anchor"]["hwm_policy_id"] = "WRONG-POLICY"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="hwm_policy_id must be PORT-HWM-3",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_contract_version_not_one_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["contract_version"] = 2
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="contract_version must be 1",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_duplicate_calendar_authority_lane_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["calendar_authorities"].append(envelope["calendar_authorities"][0].copy())
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="duplicate calendar_authorities lane",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_calendar_authority_observation_artifact_fails(
    tmp_path: Path,
) -> None:
    """A calendar authority must resolve to the per-lane calendar-authority
    role, never to an observation artifact such as alpaca-events."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope = _hwm_inputs_valid_envelope(artifacts)
    envelope["calendar_authorities"][0]["admitted_source_artifact_id"] = artifacts[
        "alpaca-events"
    ]["artifact_id"]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="has schema role 'synthetic-alpaca-events'",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_period_lane_without_authority_fails(tmp_path: Path) -> None:
    """Every lane used by a period must have an admitted calendar authority."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["calendar_authorities"] = [
        item for item in envelope["calendar_authorities"] if item["lane"] != "PROVIDER"
    ]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="period lane has no admitted calendar authority",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_incomplete_keyspace_fails(tmp_path: Path) -> None:
    """Dropping an expected (strategy, date) row must fail closed — omission
    cannot masquerade as exclusion."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"] = envelope["periods"][:-1]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="period keyspace differs from the expected perimeter keyspace",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unmapped_allocated_strategy_rows_required(
    tmp_path: Path,
) -> None:
    """Survivor-leak regression: strategy-4 has a systematic allocation but
    no mapped account assignment.  The governed perimeter still expects its
    rows; omitting them must fail closed."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"] = [
        row for row in envelope["periods"] if row["strategy_id"] != "strategy-4"
    ]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="period keyspace differs from the expected perimeter keyspace",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unexpected_keyspace_row_fails(tmp_path: Path) -> None:
    """A row outside the expected keyspace (a pre-anchor date) fails."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"].append(
        {
            "strategy_id": "strategy-1",
            "date": "2026-06-30",  # before the 2026-07-01 anchor
            "lane": "ACCOUNT",
            "disposition": "UNAVAILABLE",
            "beginning_capital_usd": None,
            "return_source_record_id": None,
            "flow_status": "MISSING",
            "fx_status": "MISSING",
        }
    )
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="period keyspace differs from the expected perimeter keyspace",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unknown_component_account_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["component_account_ids"] = ["account-ghost"]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="component account 'account-ghost' is not a known account",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_component_account_not_mapped_to_strategy_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    row = envelope["periods"][0]  # strategy-1, ACCOUNT lane
    row["disposition"] = "AVAILABLE"
    row["beginning_capital_usd"] = "10"
    row["return_source_record_id"] = "return-1"
    row["flow_status"] = "OBSERVED"
    row["fx_status"] = "NOT_REQUIRED"
    row["component_account_ids"] = ["account-schwab-b"]  # known, but unassigned
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="does not bind to exactly one account-live rollforward row",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_account_available_without_rollforward_evidence_fails(
    tmp_path: Path,
) -> None:
    """Snapshot backfill is rejected: an ACCOUNT-lane AVAILABLE period needs
    hash-bound NAV-rollforward evidence; the allocation snapshot is never a
    capital source."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    row = envelope["periods"][0]  # strategy-1, ACCOUNT lane
    row["disposition"] = "AVAILABLE"
    row["beginning_capital_usd"] = "10"
    row["return_source_record_id"] = "return-1"
    row["flow_status"] = "OBSERVED"
    row["fx_status"] = "NOT_REQUIRED"
    row["component_account_ids"] = ["account-schwab-a"]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="does not bind to exactly one account-live rollforward row",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_provider_available_without_capital_schedule_fails(
    tmp_path: Path,
) -> None:
    """PROVIDER-lane AVAILABLE periods are rejected fail-closed: the current
    catalog admits no hash-bound provider capital evidence (provider-live
    rows carry no beginning_capital, component accounts, or reconciliation
    hash), so arbitrary capital/return IDs can never validate."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    row = envelope["periods"][1]  # strategy-2, PROVIDER lane
    assert row["lane"] == "PROVIDER"
    row["disposition"] = "AVAILABLE"
    row["beginning_capital_usd"] = "10"
    row["return_source_record_id"] = "return-2"
    row["flow_status"] = "OBSERVED"
    row["fx_status"] = "NOT_REQUIRED"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE row must use ACCOUNT lane",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_anchor_disagrees_with_acceptance_record_fails(
    tmp_path: Path,
) -> None:
    """A fabricated later anchor truncates the derived keyspace; the anchor
    must equal the admitted hwm-anchor-acceptance record on every governed
    field."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["anchor"]["anchor_date"] = "2026-07-02"  # acceptance says 2026-07-01
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="anchor anchor_date disagrees with the admitted "
        "hwm-anchor-acceptance record",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_anchor_index_base_value_disagrees_with_record_fails(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    source_root = Path(spec["roots"]["synthetic"])
    # The envelope policy id is pinned by the builder's own constant check,
    # so exercise the cross-check through a drifted acceptance RECORD that
    # still passes the pin but no longer matches the envelope.
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    _add_governed_source(
        spec,
        source_root,
        "hwm-anchor-acceptance",
        {
            "anchor_date": "2026-07-01",
            "hwm_policy_id": "PORT-HWM-3",
            "index_base_value": "2.0",
        },
    )
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope["anchor"]["acceptance_provenance"]["source_artifact_sha256"] = artifacts[
        "hwm-anchor-acceptance"
    ]["sha256"]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="anchor index_base_value disagrees with the admitted "
        "hwm-anchor-acceptance record",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_anchor_policy_id_disagrees_with_acceptance_record_fails(
    tmp_path: Path,
) -> None:
    """The acceptance record's hwm_policy_id drift is caught by the cross-check."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    source_root = Path(spec["roots"]["synthetic"])
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    _add_governed_source(
        spec,
        source_root,
        "hwm-anchor-acceptance",
        {
            "anchor_date": "2026-07-01",
            "hwm_policy_id": "PORT-HWM-DRIFTED",
            "index_base_value": "1.0",
        },
    )
    artifacts = _artifacts_from_sources(spec["sources"])
    envelope["anchor"]["acceptance_provenance"]["source_artifact_sha256"] = artifacts[
        "hwm-anchor-acceptance"
    ]["sha256"]
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="anchor hwm_policy_id disagrees with the admitted "
        "hwm-anchor-acceptance record",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_envelope_source_role_binding_fails(tmp_path: Path) -> None:
    """The hwm-inputs source itself is role-bound: schema_name must equal the
    canonical governed role."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    spec["sources"]["hwm-inputs"]["schema_name"] = "WRONG-HWM-ROLE"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(
        baseline.BaselineValidationError,
        match="sources.hwm-inputs schema_name must equal the governed role",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_legacy_available_fails_closed(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    row = envelope["periods"][2]  # strategy-3, LEGACY lane
    assert row["lane"] == "LEGACY"
    row["disposition"] = "AVAILABLE"
    row["beginning_capital_usd"] = "10"
    row["return_source_record_id"] = "return-3"
    row["flow_status"] = "OBSERVED"
    row["fx_status"] = "NOT_REQUIRED"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="AVAILABLE row must use ACCOUNT lane",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_unreconciled_endpoint_delta_fails(tmp_path: Path) -> None:
    """The declared delta is recomputed independently; a fabricated delta is
    rejected."""
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["reconciliation"]["allocation_endpoint_check"]["delta_usd"] = "1"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="delta_usd differs from the recomputed delta",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_endpoint_matched_false_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["reconciliation"]["allocation_endpoint_check"]["matched"] = False
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="matched must be true for a reconciled endpoint",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_source_hash_differs_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    spec["sources"]["hwm-inputs"]["expected_sha256"] = "a" * 64
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(
        baseline.BaselineValidationError,
        match="hwm-inputs source hash differs",
    ):
        baseline.build_catalog(spec_path)


def test_hwm_inputs_stage_deterministic_with_envelope(tmp_path: Path) -> None:
    spec_path, _spec_data = _spec_with_hwm_inputs(tmp_path)
    output_root = tmp_path / "private-output"
    first = baseline.stage_baseline(spec_path, output_root)
    second = baseline.stage_baseline(spec_path, output_root)
    assert first == second
    generation_dir = output_root / "generations" / first["generation_id"]
    catalog_bytes = (generation_dir / "portfolio-baseline.json").read_bytes()
    catalog = json.loads(catalog_bytes)
    assert "hwm_inputs" in catalog
    assert catalog["hwm_inputs"]["contract_version"] == 1
    # Existing stats must still hold.
    assert first["strategy_count"] == 9


def test_hwm_inputs_unknown_strategy_id_fails(tmp_path: Path) -> None:
    spec_path, spec = _spec_with_hwm_inputs(tmp_path)
    envelope = _hwm_inputs_valid_envelope(_artifacts_from_sources(spec["sources"]))
    envelope["periods"][0]["strategy_id"] = "unknown-strategy"
    _write_envelope(spec_path, spec, envelope)
    with pytest.raises(
        baseline.BaselineValidationError,
        match="period keyspace differs from the expected perimeter keyspace",
    ):
        baseline.build_catalog(spec_path)


def test_cli_output_is_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path, spec = _spec(tmp_path)
    output_root = tmp_path / "private-output"

    assert (
        baseline.main(
            ["stage", "--spec", str(spec_path), "--output-root", str(output_root)]
        )
        == 0
    )
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["replay_status"] == "PASS"
    assert str(spec["roots"]["synthetic"]) not in output
    assert "SYNTH-A" not in output
    assert "Synthetic Strategy" not in output


def test_load_json_preserves_sub_cent_precision(tmp_path: Path) -> None:
    """Pass-3 defect: governed money parsed via float cannot survive a
    Decimal-exact reconciliation when the true sum carries sub-cent digits.
    The Decimal parse is scoped to `_load_json_decimal` (monetary
    reconciliation inputs); the default loader must NOT Decimal-ize, so
    verbatim-copied sections stay JSON-serializable."""
    import decimal

    path = tmp_path / "summary.json"
    exact = "446546.0893751776827506151026"
    path.write_text(
        '{"systematic_total": ' + exact + ', "note": 0.1}', encoding="utf-8"
    )

    parsed = baseline._load_json_decimal(path, "summary")
    assert isinstance(parsed["systematic_total"], decimal.Decimal)
    assert str(parsed["systematic_total"]) == exact

    default = baseline._load_json(path, "summary")
    assert isinstance(default["systematic_total"], float)


# ── C2-T1: current_capital, audit_snapshot and the current reconciliations ──
#
# Cluster 2 design (state/c2-design.md v3, T1, M1/M2): every current capital
# figure is the sum of its members' latest admitted USD NAV on or before the
# cutoff; the dated 2026-08-16 audit table stays byte-identical in
# ``allocations`` and its replay moves under the labeled ``audit_snapshot``.

CUTOFF = date(2026, 9, 17)
# sha256 of the catalog / allocations bytes the merge base builds for the
# synthetic ``_spec`` fixture (captured before the C2-T1 change).
LEGACY_CATALOG_SHA256 = (
    "289a3e3d173f44252cd804b941d756fc9aa93b09cc6320562a239c327250e706"
)
LEGACY_ALLOCATIONS_SHA256 = (
    "22dab8643f6f8f1ccf75b05e6e6759476f31a9dec204865da3eda61df2d7e044"
)


def _cc_nav(
    account_id: str,
    day: str,
    value: str | None,
    *,
    status: str = "AVAILABLE",
    currency: str = "USD",
    source_key: str = "provider-ledger",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "as_of_date": day,
        "nav": {"currency": currency, "status": status, "value": value},
        "provider": "SYNTHETIC",
        "record_id": f"nav-{account_id}-{day}",
        "source_key": source_key,
    }


def _cc_assignment(
    account_id: str,
    strategy_id: str | None,
    start: str | None,
    end: str | None = None,
    *,
    pool: str = "systematic-capital",
    mapped: bool = True,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "assignment_id": f"assignment-{account_id}-{strategy_id or pool}-{start}",
        "effective_end": end,
        "effective_start": start,
        "mapping_status": "MAPPED" if mapped else "UNMAPPED",
        "pool_id": pool,
        "strategy_id": strategy_id,
        "strategy_mapping_disposition": (
            "STRATEGY_MAPPED" if mapped else "RESERVE_NOT_APPLICABLE"
        ),
    }


def _cc_admission(
    account_id: str,
    disposition: str,
    start: str = "2025-11-03",
    end: str = "2026-09-17",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_reconciliation_sha256": None,
        "disposition": disposition,
        "effective_end": end,
        "effective_start": start,
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "nav_observations",
        "pool_id": None,
        "provider_id": "synthetic",
        "strategy_id": None,
    }


def _cc_case() -> dict[str, Any]:
    """Three required strategies, two retained-excluded, a two-account reserve.

    alpha   = a1 100 + a2 50 (both @ cutoff)           -> 150 @ 2026-09-17
    bravo   = b1 200 @ 2026-09-03 (provider boundary)  -> 200 @ 2026-09-03
    charlie = c1 150 @ cutoff                          -> 150 @ 2026-09-17
    echo-x  = e1 5 @ 2025-11-24 (ended membership, retained)
    zulu-x  = z1, an account that no source has ever valued
    reserve = r1 (legacy NAV, admitted REFERENCE_ONLY) + r2 (no source)
    """

    providers = {
        "acct-a1": "SCHWAB",
        "acct-a2": "SCHWAB",
        "acct-b1": "MATRIXPORT",
        "acct-c1": "IBKR",
        "acct-e1": "ALPACA",
        "acct-z1": "COMPOSER",
        "acct-r1": "IBKR",
        "acct-r2": "JOHN HANCOCK",
    }
    return {
        "valuation_date": CUTOFF,
        "strategies": [
            {"strategy_id": strategy_id}
            for strategy_id in ("alpha", "bravo", "charlie", "echo-x", "zulu-x")
        ],
        "accounts": [
            {
                "account_id": account_id,
                "base_currency": "USD",
                "provider": provider,
                "role": "INVESTMENT",
            }
            for account_id, provider in providers.items()
        ],
        "assignments": [
            _cc_assignment("acct-a1", "alpha", "2026-01-01"),
            _cc_assignment("acct-a2", "alpha", "2026-01-01"),
            _cc_assignment("acct-b1", "bravo", "2026-01-01"),
            _cc_assignment("acct-c1", "charlie", "2026-01-01"),
            # An ended membership never counts toward the current set.
            _cc_assignment("acct-a2", "charlie", "2025-06-01", "2025-12-31"),
            _cc_assignment("acct-e1", "echo-x", "2025-09-27", "2025-11-24"),
            _cc_assignment("acct-z1", "zulu-x", "2025-09-22"),
            _cc_assignment(
                "acct-r1", None, "2025-09-15", pool="reserve-capital", mapped=False
            ),
            _cc_assignment("acct-r2", None, None, pool="reserve-capital", mapped=False),
        ],
        "nav": [
            _cc_nav("acct-a1", "2026-09-16", "99"),
            _cc_nav("acct-a1", "2026-09-17", "100"),
            _cc_nav("acct-a2", "2026-09-17", "50"),
            # A later observation is not on or before the cutoff.
            _cc_nav("acct-a2", "2026-09-18", "777"),
            _cc_nav("acct-b1", "2026-09-02", "190"),
            _cc_nav("acct-b1", "2026-09-03", "200"),
            _cc_nav("acct-c1", "2026-09-17", "150"),
            _cc_nav("acct-e1", "2025-11-24", "5", source_key="alpaca-nav"),
            _cc_nav("acct-r1", "2026-07-14", "1000", source_key="ibkr-ledger"),
        ],
        "provider_admissions": [
            _cc_admission("acct-a1", "AUTHORITATIVE"),
            _cc_admission("acct-a2", "AUTHORITATIVE"),
            _cc_admission("acct-b1", "AUTHORITATIVE", end="2026-09-03"),
            _cc_admission("acct-c1", "AUTHORITATIVE"),
            _cc_admission("acct-r1", "REFERENCE_ONLY"),
        ],
        "policy_exclusions": {
            "echo-x": date(2025, 11, 25),
            "zulu-x": date(2026, 8, 1),
        },
    }


def _cc_build(case: dict[str, Any]) -> dict[str, Any]:
    return baseline._build_current_capital(**case)


def _cc_rows(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["strategy_id"]: row for row in block["strategies"]}


def _cc_checks(
    block: dict[str, Any], case: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    rows = baseline._current_capital_checks(
        block,
        assignments=case["assignments"],
        nav=case["nav"],
        provider_admissions=case["provider_admissions"],
    )
    return {row["check_id"]: row for row in rows}


def _cc_value(row: dict[str, Any]) -> str | None:
    value: str | None = row["current_value"]["value"]
    return value


def test_current_capital_is_the_sum_of_member_navs_not_the_audit_table(
    tmp_path: Path,
) -> None:
    """(a) The audit table says 20/15/10; the member NAVs say 11/20/30."""
    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)

    table = {
        row["strategy_id"]: row["current_value"]["value"]
        for row in catalog["allocations"]
        if row["scope"] == "SYSTEMATIC"
    }
    assert [table[f"strategy-{index}"] for index in (1, 2, 3)] == ["20", "15", "10"]

    block = catalog["current_capital"]
    assert block["valuation_date"] == "2026-07-30"
    rows = _cc_rows(block)
    assert {
        strategy_id: (_cc_value(rows[strategy_id]), rows[strategy_id]["as_of_date"])
        for strategy_id in ("strategy-1", "strategy-2", "strategy-3")
    } == {
        "strategy-1": ("11", "2026-07-06"),
        "strategy-2": ("20", "2026-07-01"),
        "strategy-3": ("30", "2026-07-01"),
    }
    assert [member["account_id"] for member in rows["strategy-1"]["members"]] == [
        "account-schwab-a"
    ]
    # Registry strategies with no MAPPED member publish nothing, never the table.
    assert rows["strategy-4"]["status"] == "NOT_READY_AT_CUTOFF"
    assert _cc_value(rows["strategy-4"]) is None
    assert block["systematic"]["status"] == "NOT_READY_AT_CUTOFF"
    assert {row["current_weight"] for row in block["strategies"]} == {None}


def test_current_capital_sums_members_with_own_dates_and_exact_weights() -> None:
    case = _cc_case()
    block = _cc_build(case)

    assert block["valuation_date"] == "2026-09-17"
    assert block["currency"] == "USD"
    assert block["weight_basis"] == "CURRENT_CUTOFF"
    rows = _cc_rows(block)
    assert [(row["strategy_id"], row["scope"]) for row in block["strategies"]] == [
        ("alpha", "SYSTEMATIC"),
        ("bravo", "SYSTEMATIC"),
        ("charlie", "SYSTEMATIC"),
        ("echo-x", "RETAINED_EXCLUDED"),
        ("zulu-x", "RETAINED_EXCLUDED"),
    ]
    assert _cc_value(rows["alpha"]) == "150"
    assert rows["alpha"]["as_of_date"] == "2026-09-17"
    assert [
        (member["account_id"], member["as_of_date"], member["value"]["value"])
        for member in rows["alpha"]["members"]
    ] == [("acct-a1", "2026-09-17", "100"), ("acct-a2", "2026-09-17", "50")]
    # The 2026-09-18 observation and the ended charlie/a2 membership are out.
    assert _cc_value(rows["charlie"]) == "150"
    assert [member["account_id"] for member in rows["charlie"]["members"]] == [
        "acct-c1"
    ]
    assert [rows[name]["current_weight"] for name in ("alpha", "bravo", "charlie")] == [
        "0.3",
        "0.4",
        "0.3",
    ]
    systematic = block["systematic"]
    assert systematic["status"] == "AVAILABLE"
    assert systematic["current_value"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "500",
    }
    assert systematic["components"] == ["alpha", "bravo", "charlie"]
    # Retained-excluded rows carry no systematic weight.
    assert rows["echo-x"]["current_weight"] is None
    assert rows["zulu-x"]["current_weight"] is None


def test_member_as_of_is_its_own_provider_boundary_not_the_valuation_date() -> None:
    """(c) An FX-boundary member keeps its own 2026-09-03 observation date."""
    block = _cc_build(_cc_case())
    rows = _cc_rows(block)

    assert _cc_value(rows["bravo"]) == "200"
    assert rows["bravo"]["as_of_date"] == "2026-09-03"
    assert rows["bravo"]["members"][0]["as_of_date"] == "2026-09-03"
    assert rows["alpha"]["as_of_date"] == "2026-09-17"
    # The systematic total is dated by its oldest member.
    assert block["systematic"]["as_of_date"] == "2026-09-03"


def test_strategy_as_of_is_the_oldest_member_date() -> None:
    case = _cc_case()
    case["nav"] = [
        row for row in case["nav"] if not (row["account_id"] == "acct-a2")
    ] + [_cc_nav("acct-a2", "2026-09-10", "50")]

    rows = _cc_rows(_cc_build(case))

    assert _cc_value(rows["alpha"]) == "150"
    assert rows["alpha"]["as_of_date"] == "2026-09-10"


def test_only_available_usd_navs_on_or_before_the_valuation_date_count() -> None:
    case = _cc_case()
    case["nav"] = [
        _cc_nav("acct-a1", "2026-09-15", "90"),
        # The newest on-or-before row is unavailable: fall back to the last good one.
        _cc_nav("acct-a1", "2026-09-17", None, status="UNAVAILABLE"),
        # A non-USD amount is never summed as USD.
        _cc_nav("acct-a2", "2026-09-16", "40"),
        _cc_nav("acct-a2", "2026-09-17", "9999", currency="EUR"),
        # A row after the cutoff is ignored.
        _cc_nav("acct-b1", "2026-09-03", "200"),
        _cc_nav("acct-b1", "2026-09-18", "5000"),
        _cc_nav("acct-c1", "2026-09-17", "150"),
    ]

    rows = _cc_rows(_cc_build(case))

    assert _cc_value(rows["alpha"]) == "130"
    assert rows["alpha"]["as_of_date"] == "2026-09-15"
    assert _cc_value(rows["bravo"]) == "200"


def test_required_member_without_a_nav_makes_the_strategy_and_weights_not_ready() -> (
    None
):
    case = _cc_case()
    case["nav"] = [row for row in case["nav"] if row["account_id"] != "acct-a2"]

    block = _cc_build(case)
    rows = _cc_rows(block)

    assert rows["alpha"]["status"] == "NOT_READY_AT_CUTOFF"
    assert rows["alpha"]["current_value"]["status"] == "UNAVAILABLE"
    assert _cc_value(rows["alpha"]) is None
    assert rows["alpha"]["as_of_date"] is None
    assert "acct-a2" in rows["alpha"]["reason"]
    # A partial sum is never published, and the shares become undefined.
    assert block["systematic"]["status"] == "NOT_READY_AT_CUTOFF"
    assert "alpha" in block["systematic"]["reason"]
    assert [rows[name]["current_weight"] for name in ("alpha", "bravo", "charlie")] == [
        None,
        None,
        None,
    ]
    # Strategies that are ready keep their own value.
    assert _cc_value(rows["bravo"]) == "200"


def test_weights_close_to_exactly_one_with_the_residual_on_the_last_row() -> None:
    strategies = ("s1", "s2", "s3")
    case = {
        "valuation_date": CUTOFF,
        "strategies": [{"strategy_id": strategy_id} for strategy_id in strategies],
        "accounts": [],
        "assignments": [
            _cc_assignment(f"acct-{strategy_id}", strategy_id, "2026-01-01")
            for strategy_id in strategies
        ],
        "nav": [
            _cc_nav(f"acct-{strategy_id}", "2026-09-17", "100")
            for strategy_id in strategies
        ],
        "provider_admissions": [],
        "policy_exclusions": {},
    }

    block = _cc_build(case)
    weights = [row["current_weight"] for row in block["strategies"]]

    assert weights == [
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333334",
    ]
    with localcontext() as context:
        context.prec = 80
        assert sum((Decimal(weight) for weight in weights), Decimal(0)) == 1


def test_current_capital_makes_no_strategy_count_assumption() -> None:
    """D9: the registry count is a registry fact; current capital is not 9 or 7."""
    assert baseline.RETAINED_REGISTRY_STRATEGY_COUNT == 15
    assert baseline.ALLOCATION_SNAPSHOT_STRATEGY_COUNT == 9
    assert not hasattr(baseline, "CURRENT_STRATEGY_COUNT")
    for count in (1, 2, 8):
        strategies = [f"s{index}" for index in range(1, count + 1)]
        case = {
            "valuation_date": CUTOFF,
            "strategies": [{"strategy_id": strategy_id} for strategy_id in strategies],
            "accounts": [],
            "assignments": [
                _cc_assignment(f"acct-{strategy_id}", strategy_id, "2026-01-01")
                for strategy_id in strategies
            ],
            "nav": [
                _cc_nav(f"acct-{strategy_id}", "2026-09-17", "100")
                for strategy_id in strategies
            ],
            "provider_admissions": [],
            "policy_exclusions": {},
        }

        block = _cc_build(case)

        assert len(block["strategies"]) == count
        assert block["systematic"]["current_value"]["value"] == str(100 * count)
        with localcontext() as context:
            context.prec = 80
            assert (
                sum(
                    (Decimal(row["current_weight"]) for row in block["strategies"]),
                    Decimal(0),
                )
                == 1
            )


def test_retained_excluded_without_any_source_is_null_not_ready_never_the_audit_value() -> (
    None
):
    """(e) zulu-x's account has no NAV: null + NOT_READY, whatever the table says."""
    case = _cc_case()
    block = _cc_build(case)
    rows = _cc_rows(block)

    assert rows["zulu-x"]["scope"] == "RETAINED_EXCLUDED"
    assert rows["zulu-x"]["status"] == "NOT_READY_AT_CUTOFF"
    assert rows["zulu-x"]["current_value"] == {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "value": None,
    }
    assert rows["zulu-x"]["as_of_date"] is None
    assert "acct-z1" in rows["zulu-x"]["reason"]
    # A dated audit value for that account (the 2026-08-16 table said 1068.45)
    # must not leak into the current block under any key.
    assert "1068.45" not in json.dumps(block)
    # echo-x keeps the NAV that exists, with the date it was observed.
    assert rows["echo-x"]["status"] == "AVAILABLE"
    assert _cc_value(rows["echo-x"]) == "5"
    assert rows["echo-x"]["as_of_date"] == "2025-11-24"
    # One missing component makes the aggregate not ready.
    retained = block["retained_excluded"]
    assert retained["status"] == "NOT_READY_AT_CUTOFF"
    assert retained["current_value"]["value"] is None
    assert "zulu-x" in retained["reason"]
    assert retained["components"] == ["echo-x", "zulu-x"]


def test_retained_excluded_is_available_when_every_component_has_a_nav() -> None:
    case = _cc_case()
    case["nav"].append(_cc_nav("acct-z1", "2026-09-01", "2.5", source_key="manual"))

    block = _cc_build(case)

    assert block["retained_excluded"]["status"] == "AVAILABLE"
    assert block["retained_excluded"]["current_value"]["value"] == "7.5"
    assert block["retained_excluded"]["as_of_date"] == "2025-11-24"


def test_excluded_strategy_never_claims_an_account_a_current_strategy_owns() -> None:
    case = _cc_case()
    # echo-x also held a1 in the past; a1 is alpha's current member.
    case["assignments"].append(
        _cc_assignment("acct-a1", "echo-x", "2025-01-01", "2025-09-26")
    )

    block = _cc_build(case)
    rows = _cc_rows(block)

    assert [member["account_id"] for member in rows["echo-x"]["members"]] == ["acct-e1"]
    assert _cc_value(rows["echo-x"]) == "5"
    assert _cc_value(rows["alpha"]) == "150"


def test_reserve_without_an_authoritative_admitted_nav_is_not_ready_never_the_audit_reserve() -> (
    None
):
    """(d) r1 is admitted REFERENCE_ONLY, r2 has no source: no reserve is published."""
    case = _cc_case()
    block = _cc_build(case)
    reserve = block["reserve"]

    assert reserve["status"] == "NOT_READY_AT_CUTOFF"
    assert reserve["current_value"] == {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "value": None,
    }
    assert reserve["as_of_date"] is None
    assert reserve["components"] == ["acct-r1", "acct-r2"]
    assert [
        (
            member["account_id"],
            member["provider"],
            member["status"],
            member["authority"],
        )
        for member in reserve["members"]
    ] == [
        ("acct-r1", "IBKR", "NOT_READY_AT_CUTOFF", "NAV_NOT_AUTHORITATIVE"),
        ("acct-r2", "JOHN HANCOCK", "NOT_READY_AT_CUTOFF", "NO_NAV"),
    ]
    assert "JOHN HANCOCK" in reserve["reason"]
    assert "REFERENCE_ONLY" in reserve["reason"]
    # The legacy 1000 @ 2026-07-14 observation is not capital authority.
    assert "1000" not in json.dumps(reserve["current_value"])
    assert block["total_liquid"]["status"] == "NOT_READY_AT_CUTOFF"
    assert block["total_liquid"]["current_value"]["value"] is None
    assert "reserve" in block["total_liquid"]["reason"]


def test_reserve_and_total_liquid_are_available_once_every_reserve_account_is_admitted() -> (
    None
):
    case = _cc_case()
    case["provider_admissions"] = [
        row for row in case["provider_admissions"] if row["account_id"] != "acct-r1"
    ] + [
        _cc_admission("acct-r1", "AUTHORITATIVE"),
        _cc_admission("acct-r2", "AUTHORITATIVE"),
    ]
    case["nav"].append(_cc_nav("acct-r2", "2026-09-17", "10"))
    case["nav"].append(_cc_nav("acct-z1", "2026-09-01", "2.5", source_key="manual"))

    block = _cc_build(case)

    assert block["reserve"]["status"] == "AVAILABLE"
    assert block["reserve"]["current_value"]["value"] == "1010"
    assert block["reserve"]["as_of_date"] == "2026-07-14"
    assert block["retained_excluded"]["status"] == "AVAILABLE"
    assert block["total_liquid"]["status"] == "AVAILABLE"
    # systematic 500 + retained 7.5 + reserve 1010
    assert block["total_liquid"]["current_value"]["value"] == "1517.5"
    # The total is dated by its oldest component: echo-x's 2025-11-24 observation.
    assert block["retained_excluded"]["as_of_date"] == "2025-11-24"
    assert block["total_liquid"]["as_of_date"] == "2025-11-24"
    checks = _cc_checks(block, case)
    assert checks["capital-identity-at-cutoff"]["status"] == "PASS"
    assert checks["capital-identity-at-cutoff"]["total_liquid_usd"] == "1517.5"


def test_reserve_is_not_ready_when_no_account_is_assigned_to_the_reserve_pool() -> None:
    case = _cc_case()
    case["assignments"] = [
        row for row in case["assignments"] if row["pool_id"] != "reserve-capital"
    ]

    reserve = _cc_build(case)["reserve"]

    assert reserve["status"] == "NOT_READY_AT_CUTOFF"
    assert reserve["components"] == []
    assert "no account" in reserve["reason"]


def test_membership_honours_assignment_windows_inclusively_and_fails_closed() -> None:
    base = _cc_case()
    base["assignments"] = [
        _cc_assignment("acct-a1", "alpha", "2026-09-17"),  # starts on the cutoff
        _cc_assignment("acct-a2", "alpha", "2026-01-01", "2026-09-17"),  # ends on it
        _cc_assignment("acct-b1", "bravo", "2026-09-18"),  # starts after it
        _cc_assignment("acct-c1", "charlie", "2026-01-01", "2026-09-16"),  # ended
    ]
    rows = _cc_rows(_cc_build(base))
    assert [member["account_id"] for member in rows["alpha"]["members"]] == [
        "acct-a1",
        "acct-a2",
    ]
    assert rows["bravo"]["members"] == []
    assert rows["charlie"]["members"] == []
    assert rows["bravo"]["status"] == "NOT_READY_AT_CUTOFF"

    missing_start = _cc_case()
    missing_start["assignments"].append(_cc_assignment("acct-c1", "charlie", None))
    with pytest.raises(
        baseline.BaselineValidationError, match="explicit effective_start"
    ):
        _cc_build(missing_start)

    unregistered = _cc_case()
    unregistered["assignments"].append(_cc_assignment("acct-c1", "delta", "2026-01-01"))
    with pytest.raises(baseline.BaselineValidationError, match="unregistered strategy"):
        _cc_build(unregistered)

    double_owner = _cc_case()
    double_owner["assignments"].append(
        _cc_assignment("acct-a1", "charlie", "2026-02-01")
    )
    with pytest.raises(
        baseline.BaselineValidationError, match="more than one strategy"
    ):
        _cc_build(double_owner)


def test_independent_recompute_agrees_with_the_published_block() -> None:
    case = _cc_case()
    block = _cc_build(case)
    scopes = {row["strategy_id"]: row["scope"] for row in block["strategies"]}

    recomputed = baseline._recompute_strategy_capital(
        scopes=scopes,
        assignments=case["assignments"],
        nav=case["nav"],
        valuation_date=CUTOFF,
    )

    assert recomputed == {
        "alpha": (Decimal("150"), date(2026, 9, 17), ("acct-a1", "acct-a2")),
        "bravo": (Decimal("200"), date(2026, 9, 3), ("acct-b1",)),
        "charlie": (Decimal("150"), date(2026, 9, 17), ("acct-c1",)),
        "echo-x": (Decimal("5"), date(2025, 11, 24), ("acct-e1",)),
        "zulu-x": (None, None, ("acct-z1",)),
    }


def test_current_reconciliations_pass_not_ready_and_fail_from_an_independent_recompute() -> (
    None
):
    """(b) A corrupted current value must FAIL; a literal PASS cannot survive."""
    case = _cc_case()
    block = _cc_build(case)
    checks = _cc_checks(block, case)

    assert set(checks) == {
        "strategy-capital-to-cutoff-account-values",
        "capital-identity-at-cutoff",
    }
    capital = checks["strategy-capital-to-cutoff-account-values"]
    assert capital["status"] == "PASS"
    assert capital["systematic_total_usd"] == "500"
    assert capital["mismatches"] == []
    # Reserve, retained-excluded and total liquid are not current: NOT_READY, never PASS.
    identity = checks["capital-identity-at-cutoff"]
    assert identity["status"] == "NOT_READY"
    assert identity["not_ready_components"] == [
        "reserve",
        "retained_excluded",
        "total_liquid",
    ]
    assert "total_liquid_usd" not in identity

    corrupted = copy.deepcopy(block)
    _cc_rows(corrupted)["alpha"]["current_value"]["value"] = "151"
    failed = _cc_checks(corrupted, case)["strategy-capital-to-cutoff-account-values"]
    assert failed["status"] == "FAIL"
    assert [row["strategy_id"] for row in failed["mismatches"]] == ["alpha"]
    assert failed["mismatches"][0]["recomputed_usd"] == "150"
    assert failed["mismatches"][0]["published_usd"] == "151"

    stale_date = copy.deepcopy(block)
    _cc_rows(stale_date)["bravo"]["as_of_date"] = "2026-09-17"
    assert (
        _cc_checks(stale_date, case)["strategy-capital-to-cutoff-account-values"][
            "status"
        ]
        == "FAIL"
    )

    drifted_members = copy.deepcopy(block)
    _cc_rows(drifted_members)["alpha"]["members"].pop()
    assert (
        _cc_checks(drifted_members, case)["strategy-capital-to-cutoff-account-values"][
            "status"
        ]
        == "FAIL"
    )

    wrong_total = copy.deepcopy(block)
    wrong_total["systematic"]["current_value"]["value"] = "501"
    assert (
        _cc_checks(wrong_total, case)["capital-identity-at-cutoff"]["status"] == "FAIL"
    )

    wrong_weight = copy.deepcopy(block)
    _cc_rows(wrong_weight)["alpha"]["current_weight"] = "0.31"
    assert (
        _cc_checks(wrong_weight, case)["capital-identity-at-cutoff"]["status"] == "FAIL"
    )


def test_current_reconciliation_rejects_false_availability_of_a_not_ready_component() -> (
    None
):
    case = _cc_case()
    block = _cc_build(case)

    audit_reserve = copy.deepcopy(block)
    audit_reserve["reserve"]["status"] = "AVAILABLE"
    audit_reserve["reserve"]["current_value"] = {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "100043.62",
    }
    assert (
        _cc_checks(audit_reserve, case)["capital-identity-at-cutoff"]["status"]
        == "FAIL"
    )

    painted_total = copy.deepcopy(block)
    painted_total["total_liquid"]["status"] = "AVAILABLE"
    painted_total["total_liquid"]["current_value"] = {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "505",
    }
    assert (
        _cc_checks(painted_total, case)["capital-identity-at-cutoff"]["status"]
        == "FAIL"
    )

    audit_excluded = copy.deepcopy(block)
    zulu = _cc_rows(audit_excluded)["zulu-x"]
    zulu["status"] = "AVAILABLE"
    zulu["current_value"] = {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "1068.45",
    }
    zulu["as_of_date"] = "2026-08-16"
    assert (
        _cc_checks(audit_excluded, case)["strategy-capital-to-cutoff-account-values"][
            "status"
        ]
        == "FAIL"
    )


def test_a_required_strategy_that_is_not_ready_keeps_the_capital_check_not_ready() -> (
    None
):
    case = _cc_case()
    case["nav"] = [row for row in case["nav"] if row["account_id"] != "acct-a2"]
    block = _cc_build(case)

    check = _cc_checks(block, case)["strategy-capital-to-cutoff-account-values"]

    assert check["status"] == "NOT_READY"
    assert check["not_ready_strategy_ids"] == ["alpha"]
    assert check["mismatches"] == []
    assert "systematic_total_usd" not in check


def test_builder_refuses_to_publish_a_failed_current_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, _spec_data = _spec(tmp_path)
    original = baseline._build_current_capital

    def corrupted(**kwargs: Any) -> dict[str, Any]:
        block: dict[str, Any] = original(**kwargs)
        row = next(
            item for item in block["strategies"] if item["status"] == "AVAILABLE"
        )
        row["current_value"]["value"] = "123456"
        return block

    monkeypatch.setattr(baseline, "_build_current_capital", corrupted)

    with pytest.raises(baseline.BaselineValidationError, match="does not reconcile"):
        baseline.build_catalog(spec_path)


def test_baseline_publishes_current_checks_and_moves_the_audit_checks(
    tmp_path: Path,
) -> None:
    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)

    check_ids = [row["check_id"] for row in catalog["reconciliation_checks"]]
    assert check_ids == [
        "raw-nav-to-attribution-diagnostic",
        "strategy-capital-to-cutoff-account-values",
        "capital-identity-at-cutoff",
    ]
    statuses = {
        row["check_id"]: row["status"] for row in catalog["reconciliation_checks"]
    }
    # Six synthetic strategies have no member, so nothing here may claim PASS.
    assert statuses["strategy-capital-to-cutoff-account-values"] == "NOT_READY"
    assert statuses["capital-identity-at-cutoff"] == "NOT_READY"

    snapshot = catalog["audit_snapshot"]
    assert snapshot["status"] == "HISTORICAL_REFERENCE"
    assert snapshot["as_of_date"] == "2026-07-25"
    assert snapshot["replay"] == catalog["replay"]
    assert [row["check_id"] for row in snapshot["checks"]] == [
        "allocation-to-audit-systematic-total",
        "allocation-plus-reserve-to-liquid-total",
    ]
    assert {row["status"] for row in snapshot["checks"]} == {"PASS"}
    assert snapshot["reserve"]["value_usd"] == "25"
    assert snapshot["reserve"]["source_key"] == "current-portfolio-summary"
    assert snapshot["reserve"]["source_effective_date"] == "2026-07-25"
    assert snapshot["systematic_total_usd"] == "100"
    assert snapshot["total_liquid_usd"] == "125"
    # The dated reserve is never promoted into the current block.
    reserve = catalog["current_capital"]["reserve"]
    assert reserve["status"] == "NOT_READY_AT_CUTOFF"
    assert reserve["current_value"]["value"] is None


def test_allocations_stay_byte_identical_to_the_dated_audit_snapshot(
    tmp_path: Path,
) -> None:
    """(f) Dependence and attribution still consume the 2026-08-16 snapshot."""
    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)

    assert "current_capital" in catalog
    assert (
        hashlib.sha256(canonical_json_bytes(catalog["allocations"])).hexdigest()
        == LEGACY_ALLOCATIONS_SHA256
    )
    # Nothing but the new blocks and the two moved checks differs from the
    # merge-base catalog: restoring them reproduces its exact bytes.
    legacy = {
        key: value
        for key, value in catalog.items()
        if key not in {"current_capital", "audit_snapshot"}
    }
    legacy["reconciliation_checks"] = [
        catalog["reconciliation_checks"][0],
        *catalog["audit_snapshot"]["checks"],
    ]
    assert (
        hashlib.sha256(canonical_json_bytes(legacy)).hexdigest()
        == LEGACY_CATALOG_SHA256
    )


def test_audit_snapshot_records_reserve_observation_dates_and_later_flows() -> None:
    case = _cc_case()
    replay = {
        "reserve_total_usd": "100043.62",
        "status": "PASS",
        "systematic_total_usd": "446546.09",
        "total_liquid_usd": "546589.71",
    }
    flows = [
        {  # after r1's 2026-07-14 observation, inside the snapshot window
            "account_id": "acct-r1",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-6901"},
            "event_date": "2026-08-03",
            "flow_type": "TRANSFER:INTERNAL:OUT",
            "record_id": "ibkr:flow-1",
        },
        {
            "account_id": "acct-r1",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-5309.52"},
            "event_date": "2026-08-03",
            "flow_type": "TRANSFER:INTERNAL:OUT",
            "record_id": "ibkr:flow-2",
        },
        {  # on the observation date: already inside the observed balance
            "account_id": "acct-r1",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-5000"},
            "event_date": "2026-07-14",
            "flow_type": "TRANSFER:INTERNAL:OUT",
            "record_id": "ibkr:flow-3",
        },
        {  # after the snapshot date
            "account_id": "acct-r1",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "1"},
            "event_date": "2026-08-17",
            "flow_type": "DEPOSIT_OR_WITHDRAWAL",
            "record_id": "ibkr:flow-4",
        },
        {  # not a reserve account
            "account_id": "acct-a1",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "9"},
            "event_date": "2026-08-03",
            "flow_type": "TRANSFER:INTERNAL:IN",
            "record_id": "schwab:flow-5",
        },
    ]

    snapshot = baseline._build_audit_snapshot(
        as_of_date=date(2026, 8, 16),
        replay=replay,
        systematic_total=Decimal("446546.09"),
        reserve_value=Decimal("100043.62"),
        reserve_source_date=date(2026, 8, 16),
        assignments=case["assignments"],
        nav=case["nav"],
        reference_flows=flows,
    )

    assert snapshot["status"] == "HISTORICAL_REFERENCE"
    assert snapshot["as_of_date"] == "2026-08-16"
    assert snapshot["replay"] == replay
    assert snapshot["systematic_total_usd"] == "446546.09"
    assert snapshot["total_liquid_usd"] == "546589.71"
    assert snapshot["checks"] == [
        {
            "check_id": "allocation-to-audit-systematic-total",
            "status": "PASS",
            "value_usd": "446546.09",
        },
        {
            "check_id": "allocation-plus-reserve-to-liquid-total",
            "status": "PASS",
            "value_usd": "546589.71",
        },
    ]
    reserve = snapshot["reserve"]
    assert reserve["value_usd"] == "100043.62"
    assert reserve["source_effective_date"] == "2026-08-16"
    assert reserve["account_observations"] == [
        {"account_id": "acct-r1", "as_of_date": "2026-07-14", "value_usd": "1000"}
    ]
    assert reserve["accounts_without_observation"] == ["acct-r2"]
    assert reserve["account_observations_total_usd"] == "1000"
    assert reserve["unexplained_difference_usd"] == "99043.62"
    assert [
        (row["record_id"], row["event_date"], row["amount"]["value"])
        for row in reserve["post_observation_flows"]
    ] == [
        ("ibkr:flow-1", "2026-08-03", "-6901"),
        ("ibkr:flow-2", "2026-08-03", "-5309.52"),
    ]


def _cc_schema_validator(definition: str) -> Draft202012Validator:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "baseline-catalog.schema.json"
        ).read_text(encoding="utf-8")
    )
    return Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
    )


def test_catalog_schema_requires_and_closes_the_current_capital_blocks(
    tmp_path: Path,
) -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "baseline-catalog.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert {"current_capital", "audit_snapshot"} <= set(schema["required"])
    assert schema["properties"]["current_capital"] == {
        "$ref": "#/$defs/current_capital"
    }
    assert schema["properties"]["audit_snapshot"] == {"$ref": "#/$defs/audit_snapshot"}

    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    validator = _cc_schema_validator("current_capital")
    validator.validate(catalog["current_capital"])
    validator.validate(_cc_build(_cc_case()))
    _cc_schema_validator("audit_snapshot").validate(catalog["audit_snapshot"])

    # Paired bad cases: false availability and unknown keys are structurally refused.
    painted = copy.deepcopy(catalog["current_capital"])
    painted["strategies"][0]["current_value"] = {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "value": "20",
    }
    with pytest.raises(ValidationError):
        validator.validate(painted)

    mismatched = copy.deepcopy(catalog["current_capital"])
    mismatched["strategies"][0]["status"] = "NOT_READY_AT_CUTOFF"
    with pytest.raises(ValidationError):
        validator.validate(mismatched)

    unknown = copy.deepcopy(catalog["current_capital"])
    unknown["audit_reserve_usd"] = "100043.62"
    with pytest.raises(ValidationError, match="Additional properties"):
        validator.validate(unknown)

    snapshot = copy.deepcopy(catalog["audit_snapshot"])
    snapshot["status"] = "CURRENT"
    with pytest.raises(ValidationError, match="HISTORICAL_REFERENCE"):
        _cc_schema_validator("audit_snapshot").validate(snapshot)


def test_excluded_registry_strategy_is_a_retained_row_in_a_built_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, _spec_data = _spec(tmp_path)
    monkeypatch.setattr(
        baseline,
        "_hwm_policy_exclusions",
        lambda: RETIRED_EXCLUSIONS | {"strategy-9": date(2026, 7, 1)},
    )

    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    rows = _cc_rows(catalog["current_capital"])

    assert rows["strategy-9"]["scope"] == "RETAINED_EXCLUDED"
    assert rows["strategy-9"]["status"] == "NOT_READY_AT_CUTOFF"
    assert _cc_value(rows["strategy-9"]) is None
    # The audit table still says 10 for it, only in the dated allocations.
    audit = {
        row["strategy_id"]: row["current_value"]["value"]
        for row in catalog["allocations"]
        if row["scope"] == "SYSTEMATIC"
    }
    assert audit["strategy-9"] == "10"
    assert "strategy-9" not in catalog["current_capital"]["systematic"]["components"]
    assert catalog["current_capital"]["retained_excluded"]["components"] == [
        "strategy-9"
    ]


def _load_materializer() -> Any:
    materializer_spec = importlib.util.spec_from_file_location(
        "portfolio_pipeline_materialize_seam",
        REPOSITORY_ROOT / "tools" / "portfolio_pipeline_materialize.py",
    )
    assert materializer_spec is not None and materializer_spec.loader is not None
    module = importlib.util.module_from_spec(materializer_spec)
    materializer_spec.loader.exec_module(module)
    return module


def test_materializer_verifies_the_catalog_the_real_builder_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1.3 seam regression: producer output -> consumer, no mocked producer.

    ``build_catalog`` runs for real over the synthetic sources; the materializer's
    ``financial_identities`` verification then recomputes the block it published
    from the catalog's own NAV facts and assignments, and rejects it once the
    published bytes are corrupted.
    """

    materializer = _load_materializer()
    spec_path, _spec_data = _spec(tmp_path)
    # Strategies 4-9 are retained-excluded, so strategies 1-3 are the whole required
    # set and the producer publishes real current values and weights.
    monkeypatch.setattr(
        baseline,
        "_hwm_policy_exclusions",
        lambda: RETIRED_EXCLUSIONS
        | {f"strategy-{index}": date(2026, 1, 1) for index in range(4, 10)},
    )
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)

    block = catalog["current_capital"]
    assert block["systematic"]["status"] == "AVAILABLE"
    assert block["systematic"]["current_value"]["value"] == "61"  # 11 + 20 + 30
    weights = {
        row["strategy_id"]: Decimal(row["current_weight"])
        for row in block["strategies"]
        if row["scope"] == "SYSTEMATIC"
    }
    with localcontext() as context:
        context.prec = 60
        assert sum(weights.values(), Decimal(0)) == 1
        for strategy_id, value in (
            ("strategy-1", 11),
            ("strategy-2", 20),
            ("strategy-3", 30),
        ):
            assert abs(weights[strategy_id] - Decimal(value) / Decimal(61)) < Decimal(
                "1e-32"
            )

    accepted, evidence = materializer._current_capital_identity(catalog)

    assert accepted is True
    assert evidence["strategy_mismatches"] == []
    assert evidence["component_mismatches"] == []
    assert evidence["problems"] == []
    assert evidence["systematic_total_usd"] == "61"
    # The excluded strategies have no account, and there is no reserve account:
    # reported as not current, never stamped PASS.
    assert evidence["identity_status"] == "NOT_READY"
    assert evidence["not_ready_components"] == [
        "reserve",
        "retained_excluded",
        "total_liquid",
    ]

    corrupted = copy.deepcopy(catalog)
    _cc_rows(corrupted["current_capital"])["strategy-1"]["current_value"]["value"] = (
        "12"
    )
    rejected, evidence = materializer._current_capital_identity(corrupted)

    assert rejected is False
    assert evidence["identity_status"] == "FAIL"
    assert evidence["strategy_mismatches"] == ["strategy-1"]


def _ledger_admission(
    account_id: str, provider_id: str, disposition: str
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_reconciliation_sha256": hashlib.sha256(
            account_id.encode()
        ).hexdigest(),
        "disposition": disposition,
        "effective_end": "2026-07-30",
        "effective_start": "2026-07-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "nav_observations",
        "pool_id": None,
        "provider_id": provider_id,
        "strategy_id": None,
    }


def _ledger_nav(
    account_id: str, provider_id: str, day: str, value: str
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "as_of_date": day,
        "mapping_status": "MAPPED",
        "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
        "provider_id": provider_id,
        "record_id": f"{provider_id}:nav-{account_id}-{day}",
    }


def _provider_ledger_catalog_for_current_capital(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reserve_disposition: str,
) -> dict[str, Any]:
    """Build a baseline through the production path (spec v3 + provider ledger v2).

    Strategies 1-3 are valued by AUTHORITATIVE provider NAVs dated 2026-07-29 (newer
    than their legacy CSV rows); account-schwab-b is a reserve-pool account whose
    2026-07-29 provider NAV carries ``reserve_disposition``; strategies 4-9 are
    retained-excluded by policy and have no account.
    """

    spec_path, spec = _spec(tmp_path, reserve_accounts=("account-schwab-b",))
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        nav_observations=[
            _ledger_nav("account-schwab-a", "schwab", "2026-07-29", "12"),
            _ledger_nav("account-ibkr", "ibkr", "2026-07-29", "22"),
            _ledger_nav("account-alpaca", "alpaca", "2026-07-29", "33"),
            _ledger_nav("account-schwab-b", "schwab", "2026-07-29", "45"),
        ],
        flows=[
            {
                "account_id": "account-schwab-b",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-3"},
                "event_date": "2026-07-10",
                "flow_type": "TRANSFER:INTERNAL:OUT",
                "mapping_status": "UNMAPPED",
                "provider_id": "schwab",
                "record_id": "schwab:flow-1",
            }
        ],
        admissions=[
            _ledger_admission("account-schwab-a", "schwab", "AUTHORITATIVE"),
            _ledger_admission("account-ibkr", "ibkr", "AUTHORITATIVE"),
            _ledger_admission("account-alpaca", "alpaca", "AUTHORITATIVE"),
            _ledger_admission("account-schwab-b", "schwab", reserve_disposition),
        ],
    )
    spec["roots"]["provider-ledger"] = str(root)
    spec["schema_version"] = 3
    spec["provider_ledger"] = ref
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(
        baseline,
        "_hwm_policy_exclusions",
        lambda: RETIRED_EXCLUSIONS
        | {f"strategy-{index}": date(2026, 1, 1) for index in range(4, 10)},
    )
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    assert isinstance(catalog, dict)
    return catalog


def test_provider_ledger_path_values_current_capital_and_gates_the_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production (spec v3) path: newer admitted NAVs win; REFERENCE_ONLY is not authority."""

    materializer = _load_materializer()
    catalog = _provider_ledger_catalog_for_current_capital(
        tmp_path, monkeypatch, reserve_disposition="REFERENCE_ONLY"
    )

    block = catalog["current_capital"]
    rows = _cc_rows(block)
    assert {
        strategy_id: (_cc_value(rows[strategy_id]), rows[strategy_id]["as_of_date"])
        for strategy_id in ("strategy-1", "strategy-2", "strategy-3")
    } == {
        "strategy-1": ("12", "2026-07-29"),
        "strategy-2": ("22", "2026-07-29"),
        "strategy-3": ("33", "2026-07-29"),
    }
    assert [member["source_key"] for member in rows["strategy-1"]["members"]] == [
        "provider-ledger"
    ]
    assert block["systematic"]["current_value"]["value"] == "67"
    assert block["systematic"]["as_of_date"] == "2026-07-29"
    assert block["retained_excluded"]["status"] == "NOT_READY_AT_CUTOFF"
    # The reserve account's provider NAV is admitted REFERENCE_ONLY: not capital authority.
    reserve = block["reserve"]
    assert reserve["status"] == "NOT_READY_AT_CUTOFF"
    assert reserve["current_value"]["value"] is None
    assert [
        (member["account_id"], member["authority"]) for member in reserve["members"]
    ] == [("account-schwab-b", "NAV_NOT_AUTHORITATIVE")]
    assert "SCHWAB" in reserve["reason"] and "REFERENCE_ONLY" in reserve["reason"]
    checks = {row["check_id"]: row for row in catalog["reconciliation_checks"]}
    assert checks["strategy-capital-to-cutoff-account-values"]["status"] == "PASS"
    assert (
        checks["strategy-capital-to-cutoff-account-values"]["systematic_total_usd"]
        == "67"
    )
    assert checks["capital-identity-at-cutoff"]["status"] == "NOT_READY"
    # The dated audit snapshot still carries the audit reserve, with the observation
    # dates and the reserve-account flow booked after them (from the ledger's flows).
    snapshot = catalog["audit_snapshot"]
    assert snapshot["reserve"]["value_usd"] == "25"
    assert snapshot["reserve"]["account_observations"] == [
        {
            "account_id": "account-schwab-b",
            "as_of_date": "2026-07-06",
            "value_usd": "41",
        }
    ]
    assert snapshot["reserve"]["unexplained_difference_usd"] == "-16"
    assert [
        row["record_id"] for row in snapshot["reserve"]["post_observation_flows"]
    ] == ["schwab:flow-1"]
    # The independent consumer agrees with what the producer published.
    accepted, evidence = materializer._current_capital_identity(catalog)
    assert accepted is True
    assert evidence["strategy_mismatches"] == [] == evidence["component_mismatches"]


def test_provider_ledger_path_publishes_an_authoritatively_admitted_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materializer = _load_materializer()
    catalog = _provider_ledger_catalog_for_current_capital(
        tmp_path, monkeypatch, reserve_disposition="AUTHORITATIVE"
    )

    reserve = catalog["current_capital"]["reserve"]
    assert reserve["status"] == "AVAILABLE"
    assert reserve["current_value"]["value"] == "45"
    assert reserve["as_of_date"] == "2026-07-29"
    assert [
        (member["account_id"], member["authority"], member["as_of_date"])
        for member in reserve["members"]
    ] == [("account-schwab-b", "AUTHORITATIVE", "2026-07-29")]
    # Retained-excluded is still not current, so the identity cannot pass yet.
    identity = {row["check_id"]: row for row in catalog["reconciliation_checks"]}[
        "capital-identity-at-cutoff"
    ]
    assert identity["status"] == "NOT_READY"
    assert identity["not_ready_components"] == ["retained_excluded", "total_liquid"]
    accepted, evidence = materializer._current_capital_identity(catalog)
    assert accepted is True
    assert evidence["component_mismatches"] == []
    assert evidence["not_ready_components"] == ["retained_excluded", "total_liquid"]


def _cr2_custody_financial_ref():
    paths = {
        "contract": REPOSITORY_ROOT
        / "contracts/portfolio-financial-mvp-source-v1.json",
        "registry": REPOSITORY_ROOT
        / "docs/analytics/current-strategy-decomposition-plan.json",
        "schema": REPOSITORY_ROOT
        / "schemas/portfolio/financial-mvp-source-contract.schema.json",
    }
    return {
        f"{name}_{kind}": (
            str(path)
            if kind == "path"
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for name, path in paths.items()
        for kind in ("path", "sha256")
    }


def test_cr2_custody_held_ledger_cannot_adopt_unadmitted_new_contract(tmp_path):
    root, ref = _provider_ledger_generation(tmp_path, catalog_schema_version=2)
    ref["financial_mvp"] = _cr2_custody_financial_ref()
    with pytest.raises(
        baseline.BaselineValidationError, match="admission|file boundary"
    ):
        baseline._provider_ledger(ref, {"provider-ledger": root}, date(2026, 9, 17))


def test_cr2_custody_declared_contract_tamper_fails_without_legacy_fallback(tmp_path):
    root, ref = _provider_ledger_generation(tmp_path, catalog_schema_version=2)
    financial = _cr2_custody_financial_ref()
    changed = tmp_path / "contract.json"
    changed.write_bytes(Path(financial["contract_path"]).read_bytes() + b" ")
    financial["contract_path"] = str(changed)
    ref["financial_mvp"] = financial
    with pytest.raises(baseline.BaselineValidationError, match="hash"):
        baseline._provider_ledger(ref, {"provider-ledger": root}, date(2026, 9, 17))


def _cr2_custody_cash_case():
    case = _cc_case()
    case["strategies"] = [{"strategy_id": "alpha"}]
    case["assignments"] = [_cc_assignment("acct-a1", "alpha", "2026-01-01")]
    case["accounts"] = [
        row for row in case["accounts"] if row["account_id"] == "acct-a1"
    ]
    case["nav"] = [_cc_nav("acct-a1", "2026-09-17", "105")]
    case["policy_exclusions"] = {}

    def row(record_id, value, **kw):
        return {
            "account_id": "acct-a1",
            "provider_id": "synthetic",
            "record_id": record_id,
            "source_record_id": record_id,
            "source_artifact_id": "native/capital.csv",
            "generation_id": "a" * 64,
            "event_date": "2026-09-16",
            "evidence_layer": "ACCOUNT_LIVE",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": value},
            **kw,
        }

    case["provider_catalog"] = {
        "catalog_schema_version": 2,
        "flows": [
            row(
                "deposit",
                "100",
                provider_account_scope="ACCOUNT_ONLY",
                capital_disposition="EXTERNAL_CAPITAL",
                flow_type="DEPOSIT_OR_WITHDRAWAL",
                mapping_status="UNMAPPED",
                strategy_id=None,
            )
        ],
        "capital_anchors": [
            row(
                "allocation",
                "96",
                mapping_status="MAPPED",
                strategy_id="alpha",
                capital_type="STRATEGY_CAPITAL_ALLOCATION",
            )
        ],
        "admissions": [_cc_admission("acct-a1", "AUTHORITATIVE")],
    }
    for fact_type in ("flows", "capital_anchors"):
        admission = _cc_admission("acct-a1", "AUTHORITATIVE")
        admission["fact_type"] = fact_type
        case["provider_catalog"]["admissions"].append(admission)
    for admission in case["provider_catalog"]["admissions"]:
        admission["account_reconciliation_sha256"] = hashlib.sha256(
            b"SYNTHETIC_RECONCILIATION"
        ).hexdigest()
    case["custody_authority"] = {
        **{
            key: "a" * 64
            for key in (
                "ledger_generation_id",
                "ledger_catalog_sha256",
                "ledger_manifest_sha256",
                "admission_receipt_sha256",
                "contract_raw_sha256",
                "contract_semantic_sha256",
                "census_sha256",
                "invested_capital_policy_sha256",
            )
        },
        "census_id": "SYNTHETIC_CENSUS",
    }
    return case


def test_cr2_custody_current_capital_adds_residual_cash_without_changing_members():
    case = _cr2_custody_cash_case()
    block = baseline._build_current_capital(**case)
    row = block["strategies"][0]
    assert row["current_value"]["value"] == "109"
    assert row["members"][0]["value"]["value"] == "105"
    assert row["derived_cash"][0]["current_value"]["value"] == "4"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda raw: raw["flows"].append(copy.deepcopy(raw["flows"][0])), "duplicate"),
        (
            lambda raw: raw["capital_anchors"][0]["amount"].update(value="101"),
            "negative",
        ),
        (
            lambda raw: raw["capital_anchors"].append(
                {
                    **raw["capital_anchors"][0],
                    "record_id": "other",
                    "strategy_id": "bravo",
                }
            ),
            "strategy",
        ),
    ],
)
def test_cr2_custody_cash_rejects_invalid_raw_facts(mutate, match):
    case = _cr2_custody_cash_case()
    mutate(case["provider_catalog"])
    with pytest.raises(baseline.BaselineValidationError, match=match):
        baseline._build_current_capital(**case)


def test_cr2_custody_baseline_check_recomputes_raw_cash_and_rejects_emitted_tamper():
    case = _cr2_custody_cash_case()
    block = baseline._build_current_capital(**case)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }
    assert all(
        row["status"] != "FAIL"
        for row in baseline._current_capital_checks(block, **arguments)
    )
    block["strategies"][0]["derived_cash"][0]["current_value"]["value"] = "5"
    assert any(
        row["status"] == "FAIL"
        for row in baseline._current_capital_checks(block, **arguments)
    )


def test_cr2_custody_catalog_schema_accepts_strict_separate_cash():
    block = baseline._build_current_capital(**_cr2_custody_cash_case())
    _cc_schema_validator("current_capital").validate(block)


def test_cr2_custody_same_day_is_order_invariant_and_date_negative_fails():
    case = _cr2_custody_cash_case()
    raw = case["provider_catalog"]
    raw["capital_anchors"].append(
        {
            **raw["capital_anchors"][0],
            "record_id": "allocation-2",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "2"},
        }
    )
    original = baseline._build_current_capital(**case)
    raw["capital_anchors"].reverse()
    assert baseline._build_current_capital(**case) == original
    raw["flows"][0]["event_date"] = "2026-09-17"
    with pytest.raises(baseline.BaselineValidationError, match="negative dated"):
        baseline._build_current_capital(**case)


@pytest.mark.parametrize("rate,expected", [("0.99", "108"), ("1.01", "110")])
def test_cr2_custody_native_event_fx_converts_once(rate, expected):
    case = _cr2_custody_cash_case()
    event = case["provider_catalog"]["flows"][0]
    event["amount"]["currency"] = "USDC"
    event["event_time_fx"] = {
        "base_currency": "USD",
        "currency": "USDC",
        "date": event["event_date"],
        "field": "Close",
        "generation_id": "f" * 64,
        "provider": "synthetic",
        "rate": rate,
        "source_artifact_id": "rates.csv",
        "timing": "UTC_DAILY_CLOSE",
    }
    block = baseline._build_current_capital(**case)
    assert block["strategies"][0]["current_value"]["value"] == expected
    event["event_time_fx"]["date"] = "2026-09-15"
    with pytest.raises(baseline.BaselineValidationError, match="FX"):
        baseline._build_current_capital(**case)


def test_cr2_custody_withdrawal_decreases_cash_and_q_a_no_preserves_members():
    case = _cr2_custody_cash_case()
    legacy = baseline._build_current_capital(
        **{
            key: value
            for key, value in case.items()
            if key not in {"custody_authority", "provider_catalog"}
        }
    )
    raw = case["provider_catalog"]
    raw["flows"].append(
        {
            **raw["flows"][0],
            "record_id": "withdrawal",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-3"},
        }
    )
    block = baseline._build_current_capital(**case)
    assert block["strategies"][0]["current_value"]["value"] == "106"
    assert canonical_json_bytes(
        block["strategies"][0]["members"]
    ) == canonical_json_bytes(legacy["strategies"][0]["members"])


def test_cr2_custody_internal_movements_fail_without_sleeve_semantics():
    case = _cr2_custody_cash_case()
    raw = case["provider_catalog"]
    raw["flows"].append(
        {
            **raw["flows"][0],
            "record_id": "internal",
            "flow_type": "TRANSFER:INTERNAL:CASH",
            "mapping_status": "MAPPED",
            "strategy_id": "alpha",
        }
    )
    with pytest.raises(
        baseline.BaselineValidationError, match="unsupported|ambiguous internal"
    ):
        baseline._build_current_capital(**case)


def _cash_line(block: dict[str, Any]) -> dict[str, Any]:
    line: dict[str, Any] = block["strategies"][0]["derived_cash"][0]
    return line


def _admission_ends(case: dict[str, Any], *, flows: str, anchors: str) -> None:
    """Set the end of the AUTHORITATIVE flows and capital-anchor admission windows."""

    for admission in case["provider_catalog"]["admissions"]:
        if admission["fact_type"] == "flows":
            admission["effective_end"] = flows
        elif admission["fact_type"] == "capital_anchors":
            admission["effective_end"] = anchors


def test_n56_cash_line_covered_by_both_admissions_is_dated_the_cutoff() -> None:
    """N56: a cash line is proven true through min(cutoff, every admission window end).

    The deposit and the sleeve allocation are both on 2026-09-16 and both captures are
    admitted through 2026-09-18, so the balance is proven through the 2026-09-17 cutoff;
    the date of the last event is kept beside it, not used as the label.
    """

    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-18")

    block = baseline._build_current_capital(**case)

    line = _cash_line(block)
    assert line["as_of_date"] == "2026-09-17"
    assert line["last_event_date"] == "2026-09-16"
    assert line["coverage_end"] == "2026-09-18"
    # The member is dated 2026-09-17, so the row and Systematic are too.
    assert block["strategies"][0]["as_of_date"] == "2026-09-17"
    assert block["systematic"]["as_of_date"] == "2026-09-17"


def test_n56_cash_line_dated_the_cutoff_when_coverage_ends_exactly_there() -> None:
    case = _cr2_custody_cash_case()

    line = _cash_line(baseline._build_current_capital(**case))

    assert line["as_of_date"] == "2026-09-17"
    assert line["coverage_end"] == "2026-09-17"
    assert line["last_event_date"] == "2026-09-16"


def test_n56_an_early_ending_anchor_window_keeps_the_line_the_row_and_systematic() -> (
    None
):
    """Cash subtracts sleeve allocations, so an unadmitted later allocation would
    overstate it: the anchor window end, not the flows' later end, bounds the proof."""

    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-16")

    block = baseline._build_current_capital(**case)

    line = _cash_line(block)
    assert line["as_of_date"] == "2026-09-16"
    assert line["coverage_end"] == "2026-09-16"
    assert line["last_event_date"] == "2026-09-16"
    assert block["strategies"][0]["as_of_date"] == "2026-09-16"
    assert block["systematic"]["as_of_date"] == "2026-09-16"
    # Values are unchanged: only the label moves.
    assert block["strategies"][0]["current_value"]["value"] == "109"


def test_n56_an_early_ending_flows_window_bounds_the_line_too() -> None:
    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-16", anchors="2026-09-18")

    assert _cash_line(baseline._build_current_capital(**case))["as_of_date"] == (
        "2026-09-16"
    )


def _flow_window(case: dict[str, Any], start: str, end: str) -> None:
    """Append one more AUTHORITATIVE flows admission window to the catalog."""

    admissions = case["provider_catalog"]["admissions"]
    first = next(row for row in admissions if row["fact_type"] == "flows")
    admissions.append({**first, "effective_start": start, "effective_end": end})


def _later_deposit(case: dict[str, Any], day: str = "2026-09-17") -> None:
    raw = case["provider_catalog"]
    raw["flows"].append(
        {
            **raw["flows"][0],
            "record_id": "later-deposit",
            "event_date": day,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "20"},
        }
    )


def test_n56_a_hole_between_flow_windows_is_not_bridged() -> None:
    """Astra rg-9fb7125 item 1: flows are admitted to 09-16 and again from 09-18, so
    09-17 is attested by nothing; the line is proven through 09-16, not the cutoff."""

    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-16", anchors="2026-09-19")
    _flow_window(case, "2026-09-18", "2026-09-19")

    block = baseline._build_current_capital(**case)

    line = _cash_line(block)
    assert line["coverage_end"] == "2026-09-16"
    assert line["as_of_date"] == "2026-09-16"
    assert line["current_value"]["value"] == "4"
    assert block["strategies"][0]["as_of_date"] == "2026-09-16"
    assert block["systematic"]["as_of_date"] == "2026-09-16"


def test_n56_adjacent_flow_windows_merge_into_one_segment() -> None:
    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-16", anchors="2026-09-19")
    _flow_window(case, "2026-09-17", "2026-09-19")

    line = _cash_line(baseline._build_current_capital(**case))

    assert line["coverage_end"] == "2026-09-19"
    assert line["as_of_date"] == "2026-09-17"


def test_n56_a_hole_in_the_anchor_windows_bounds_the_line_too() -> None:
    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-19", anchors="2026-09-16")
    admissions = case["provider_catalog"]["admissions"]
    first = next(row for row in admissions if row["fact_type"] == "capital_anchors")
    admissions.append(
        {**first, "effective_start": "2026-09-18", "effective_end": "2026-09-19"}
    )

    line = _cash_line(baseline._build_current_capital(**case))

    assert line["coverage_end"] == "2026-09-16"
    assert line["as_of_date"] == "2026-09-16"


def test_n56_the_value_is_the_balance_on_the_label_not_one_with_later_events() -> None:
    """Astra item 2: a 09-17 deposit outruns anchor coverage ending 09-16.  The line
    keeps the balance that was proven on 09-16 (4), not 24 labelled 09-16."""

    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-16")
    _later_deposit(case)

    block = baseline._build_current_capital(**case)

    line = _cash_line(block)
    assert line["as_of_date"] == "2026-09-16"
    assert line["current_value"]["value"] == "4"
    assert line["last_event_date"] == "2026-09-16"
    assert [event["record_id"] for event in line["events"]] == ["allocation", "deposit"]
    assert all(event["event_date"] <= line["as_of_date"] for event in line["events"])
    assert block["strategies"][0]["current_value"]["value"] == "109"
    assert block["strategies"][0]["as_of_date"] == "2026-09-16"


def test_n56_a_later_event_inside_full_coverage_is_counted_at_the_cutoff() -> None:
    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-17")
    _later_deposit(case)

    block = baseline._build_current_capital(**case)

    line = _cash_line(block)
    assert line["as_of_date"] == "2026-09-17"
    assert line["current_value"]["value"] == "24"
    assert line["last_event_date"] == "2026-09-17"
    assert len(line["events"]) == 3
    assert block["strategies"][0]["current_value"]["value"] == "129"


def test_n56_a_cash_dated_before_the_cutoff_keeps_the_native_check_not_ready() -> None:
    """The native reconciliation recomputes with the producer's own logic, so it must
    not turn a row dated before the cutoff into a PASS or a cutoff-labelled total."""

    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-16")
    _later_deposit(case)
    block = baseline._build_current_capital(**case)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }

    check = next(
        row
        for row in baseline._current_capital_checks(block, **arguments)
        if row["check_id"] == "strategy-capital-to-cutoff-account-values"
    )

    assert check["status"] == "NOT_READY"
    assert check["mismatches"] == []
    assert check["not_ready_strategy_ids"] == ["alpha"]
    assert "systematic_total_usd" not in check


def test_n56_a_cash_dated_at_the_cutoff_keeps_the_native_check_passing() -> None:
    case = _cr2_custody_cash_case()
    block = baseline._build_current_capital(**case)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }

    check = next(
        row
        for row in baseline._current_capital_checks(block, **arguments)
        if row["check_id"] == "strategy-capital-to-cutoff-account-values"
    )

    assert check["status"] == "PASS"
    assert check["systematic_total_usd"] == "109"


def test_n56_baseline_check_recomputes_the_cash_label_and_rejects_a_forged_one() -> (
    None
):
    case = _cr2_custody_cash_case()
    _admission_ends(case, flows="2026-09-18", anchors="2026-09-16")
    block = baseline._build_current_capital(**case)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }
    assert all(
        row["status"] != "FAIL"
        for row in baseline._current_capital_checks(block, **arguments)
    )

    block["strategies"][0]["derived_cash"][0]["as_of_date"] = "2026-09-17"

    assert any(
        row["status"] == "FAIL"
        for row in baseline._current_capital_checks(block, **arguments)
    )


def test_n56_custody_catalog_schema_requires_the_new_cash_fields_from_version_two() -> (
    None
):
    case = _cr2_custody_cash_case()
    block = baseline._build_current_capital(**case)

    _cc_schema_validator("current_capital").validate(block)
    _cc_schema_validator("current_capital_v2").validate(block)
    for field in ("last_event_date", "coverage_end"):
        stripped = copy.deepcopy(block)
        del stripped["strategies"][0]["derived_cash"][0][field]
        _cc_schema_validator("current_capital").validate(stripped)
        with pytest.raises(ValidationError):
            _cc_schema_validator("current_capital_v2").validate(stripped)


def test_n56_catalog_version_two_only_when_custody_cash_is_present() -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "baseline-catalog.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["baseline_schema_version"] == {"enum": [1, 2]}
    assert baseline.CUSTODY_CASH_CATALOG_SCHEMA_VERSION == 2
    assert baseline.CATALOG_SCHEMA_VERSION == 1


# ---------------------------------------------------------------- E3: fact_status.fx
#
# The FX fact was the literal FAILED while the financial contract's portfolio-fx-rates
# segment is READY with an admitted generation.  Every fixture here is built through
# the production path (spec v3 + a schema-2 provider ledger + build_catalog) over the
# repository's real contract bytes; only the custody admission gate, which needs a
# published admission receipt, is replaced by a gate that returns the real contract.

_FX_GENERATION = "fd6096d8faafd6271ca210e992ca22ab89356674f513af3a642f4cdf71c4d0aa"


def _e3_real_contract() -> dict[str, Any]:
    return json.loads(
        (
            REPOSITORY_ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )


def _e3_install_gate(monkeypatch: pytest.MonkeyPatch, contract: dict[str, Any]) -> None:
    policy = copy.deepcopy(
        next(
            row["calculation_contract"]
            for row in contract["financial_outcomes"]
            if row["outcome_id"] == "INVESTED_CAPITAL"
        )
    )
    policy.pop("provider_custody_perimeter", None)
    authority = {"contract": contract}
    admission = {"receipt_sha256": "0" * 64}
    monkeypatch.setattr(
        baseline,
        "_selected_custody_authority",
        lambda ref, generation_root: (authority, admission, policy),
    )


def _e3_flow(currency: str, index: int) -> dict[str, Any]:
    return {
        "account_id": "account-ibkr",
        "amount": {"currency": currency, "status": "AVAILABLE", "value": "5"},
        "event_date": "2026-07-10",
        "flow_type": "DEPOSIT",
        "mapping_status": "MAPPED",
        "provider_id": "ibkr",
        "record_id": f"ibkr:flow-{currency}-{index}",
    }


def _e3_fx_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    contract_bound: bool = True,
    ledger_fx_generation: str | None = _FX_GENERATION,
    non_usd_flows: int = 0,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec_path, spec = _spec(tmp_path)
    flows_admission = {
        **_ledger_admission("account-ibkr", "ibkr", "AUTHORITATIVE"),
        "fact_type": "flows",
    }
    extra: dict[str, Any] = {}
    if ledger_fx_generation is not None:
        extra["portfolio_fx_generation"] = {
            "generation_id": ledger_fx_generation,
            "selection": "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT",
        }
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        nav_observations=[
            _ledger_nav("account-ibkr", "ibkr", "2026-07-29", "22"),
        ],
        flows=[_e3_flow("EUR", index) for index in range(non_usd_flows)],
        admissions=[
            _ledger_admission("account-ibkr", "ibkr", "AUTHORITATIVE"),
            flows_admission,
        ],
        extra_catalog=extra,
    )
    if contract_bound:
        ref["financial_mvp"] = _cr2_custody_financial_ref()
        (
            root
            / "generations"
            / ref["generation_id"]
            / "admit-or-declare-receipt.json"
        ).write_bytes(b"{}")
        _e3_install_gate(monkeypatch, contract or _e3_real_contract())
    spec["roots"]["provider-ledger"] = str(root)
    spec["schema_version"] = 3
    spec["provider_ledger"] = ref
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    return next(row for row in catalog["fact_coverage"] if row["fact_type"] == "fx")


def test_e3_an_admitted_fx_generation_with_unconverted_rows_is_partial_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _e3_fx_row(tmp_path, monkeypatch, non_usd_flows=3)

    assert row["status"] == "PARTIAL"
    assert len(row["known_gaps"]) == 1
    assert "3 non-USD" in row["known_gaps"][0]
    assert "EUR" in row["known_gaps"][0]
    disposition = row["disposition"]
    assert disposition["fx_affected_record_count"] == 3
    assert disposition["generation_id"] == _FX_GENERATION
    assert disposition["admitted_start"] == "2025-01-01"
    assert disposition["admitted_end"] == "2026-09-17"
    assert disposition["currencies"] == ["EUR"]


def test_e3_an_admitted_fx_generation_with_no_unconverted_rows_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _e3_fx_row(tmp_path, monkeypatch, non_usd_flows=0)

    assert row["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert row["known_gaps"] == []


def test_e3_no_contract_bound_generation_stays_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _e3_fx_row(tmp_path, monkeypatch, contract_bound=False, non_usd_flows=3)

    assert row["status"] == "FAILED"
    assert row["disposition"]["code"] == "RETAINED_NO_ACCEPTED_FX_SOURCE"
    assert "3 non-USD" in row["known_gaps"][0]


def test_e3_a_ledger_that_did_not_use_the_admitted_generation_stays_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for other in (None, "b" * 64):
        row = _e3_fx_row(
            tmp_path / f"case-{other is None}",
            monkeypatch,
            ledger_fx_generation=other,
            non_usd_flows=1,
        )
        assert row["status"] == "FAILED"


def test_e3_a_segment_that_is_not_ready_is_not_an_admitted_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _e3_real_contract()
    segment = next(
        item
        for item in contract["source_segments"]
        if item["source_id"] == "portfolio-fx-rates"
    )
    segment["readiness"] = "REQUIRED_NOT_READY"

    row = _e3_fx_row(tmp_path, monkeypatch, non_usd_flows=1, contract=contract)

    assert row["status"] == "FAILED"


def test_e3_a_generation_that_ends_before_the_cutoff_is_partial_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _e3_real_contract()
    segment = next(
        item
        for item in contract["source_segments"]
        if item["source_id"] == "portfolio-fx-rates"
    )
    segment["interval"]["observed_end"] = "2026-07-20"
    segment["current_generation"]["as_of"] = "2026-07-20"

    row = _e3_fx_row(tmp_path, monkeypatch, non_usd_flows=0, contract=contract)

    assert row["status"] == "PARTIAL"
    assert any("2026-07-20" in gap for gap in row["known_gaps"])


def test_e3_a_generation_that_ends_before_the_window_starts_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _e3_real_contract()
    segment = next(
        item
        for item in contract["source_segments"]
        if item["source_id"] == "portfolio-fx-rates"
    )
    segment["interval"]["observed_end"] = "2026-07-05"
    segment["current_generation"]["as_of"] = "2026-07-05"

    row = _e3_fx_row(tmp_path, monkeypatch, non_usd_flows=1, contract=contract)

    assert row["status"] == "FAILED"


# ------------------------------------------- N59: retained-excluded member dating
#
# A retained-excluded account's current NAV came only from the legacy CSV (dated
# 2025-11-24 in the real shape) although the frozen ledger carries the same value to a
# later date as unmapped, account-level rows under an AUTHORITATIVE account-live NAV
# admission.  The member is dated by the ledger's last admitted observation of that
# value; the value does not change.  Built through the production path (spec v3 +
# schema-2 ledger + build_catalog).


def _n59_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ledger_rows: list[tuple[str, str]],
    disposition: str = "AUTHORITATIVE",
    excluded: bool = True,
) -> dict[str, Any]:
    spec_path, spec = _spec(tmp_path)
    rows = []
    for day, value in ledger_rows:
        row = _ledger_nav("account-alpaca", "alpaca", day, value)
        row["mapping_status"] = "UNMAPPED"
        rows.append(row)
    root, ref = _provider_ledger_generation(
        tmp_path,
        catalog_schema_version=2,
        nav_observations=rows,
        admissions=[_ledger_admission("account-alpaca", "alpaca", disposition)],
    )
    spec["roots"]["provider-ledger"] = str(root)
    spec["schema_version"] = 3
    spec["provider_ledger"] = ref
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    exclusions = RETIRED_EXCLUSIONS | (
        {"strategy-3": date(2026, 1, 1)} if excluded else {}
    )
    monkeypatch.setattr(baseline, "_hwm_policy_exclusions", lambda: exclusions)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    return catalog


def _n59_member(catalog: dict[str, Any]) -> dict[str, Any]:
    row = _cc_rows(catalog["current_capital"])["strategy-3"]
    (member,) = row["members"]
    assert member["account_id"] == "account-alpaca"
    return member


def test_n59_a_retained_excluded_member_is_dated_by_the_ledgers_last_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(
        tmp_path,
        monkeypatch,
        ledger_rows=[("2026-07-15", "30"), ("2026-07-29", "30")],
    )

    member = _n59_member(catalog)
    assert member["as_of_date"] == "2026-07-29"
    assert member["value"] == {"currency": "USD", "status": "AVAILABLE", "value": "30"}
    assert member["source_key"] == "provider-ledger"
    assert member["nav_record_id"] == "alpaca:nav-account-alpaca-2026-07-29"
    retained = catalog["current_capital"]["retained_excluded"]
    assert retained["status"] == "AVAILABLE"
    assert retained["as_of_date"] == "2026-07-29"
    accepted, evidence = _load_materializer()._current_capital_identity(catalog)
    assert accepted is True
    assert evidence["strategy_mismatches"] == []


def test_n59_a_member_with_no_ledger_observation_keeps_the_csv_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(tmp_path, monkeypatch, ledger_rows=[])

    member = _n59_member(catalog)
    assert member["as_of_date"] == "2026-07-01"
    assert member["source_key"] == "alpaca-nav"
    assert member["value"]["value"] == "30"


def test_n59_a_later_different_ledger_value_ends_the_carry_at_the_last_equal_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(
        tmp_path,
        monkeypatch,
        ledger_rows=[("2026-07-15", "30"), ("2026-07-29", "31")],
    )

    member = _n59_member(catalog)
    assert (member["as_of_date"], member["value"]["value"]) == ("2026-07-15", "30")
    assert member["source_key"] == "provider-ledger"


def test_n59_a_value_that_changes_and_returns_is_not_carried_across(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(
        tmp_path,
        monkeypatch,
        ledger_rows=[("2026-07-10", "31"), ("2026-07-29", "30")],
    )

    member = _n59_member(catalog)
    assert (member["as_of_date"], member["source_key"]) == (
        "2026-07-01",
        "alpaca-nav",
    )


def test_n59_a_reference_only_ledger_row_is_not_an_admitted_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(
        tmp_path,
        monkeypatch,
        ledger_rows=[("2026-07-29", "30")],
        disposition="REFERENCE_ONLY",
    )

    member = _n59_member(catalog)
    assert (member["as_of_date"], member["source_key"]) == (
        "2026-07-01",
        "alpaca-nav",
    )


def test_n59_a_systematic_member_is_not_redated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _n59_catalog(
        tmp_path,
        monkeypatch,
        ledger_rows=[("2026-07-29", "30")],
        excluded=False,
    )

    member = _n59_member(catalog)
    assert (member["as_of_date"], member["source_key"]) == (
        "2026-07-01",
        "alpaca-nav",
    )


def test_fund_scoped_provider_account_builds_no_derived_cash_line():
    """#536: one account funds several funds; the strategy's value is its fund NAV."""

    case = _cr2_custody_cash_case()
    for fact in ("flows", "capital_anchors"):
        for row in case["provider_catalog"][fact]:
            row["provider_id"] = "quantbase"
    case["custody_authority"]["fund_scoped_providers"] = ["QUANTBASE"]

    block = baseline._build_current_capital(**case)

    row = block["strategies"][0]
    assert "derived_cash" not in row
    assert row["current_value"]["value"] == "105"
    assert row["members"][0]["value"]["value"] == "105"
    _cc_schema_validator("current_capital").validate(block)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }
    assert all(
        check["status"] != "FAIL"
        for check in baseline._current_capital_checks(block, **arguments)
    )
    # A derived cash line for the fund-scoped account is a tamper, not a value.
    block["strategies"][0]["derived_cash"] = [{"current_value": {"value": "4"}}]
    assert any(
        check["status"] == "FAIL"
        for check in baseline._current_capital_checks(block, **arguments)
    )


def test_fund_scoped_strategy_shows_dividends_and_fees_as_labelled_lines():
    """#536: NAV stays the value; dividends and cash-paid fees sit beside it."""

    case = _cr2_custody_cash_case()
    for fact in ("flows", "capital_anchors"):
        for row in case["provider_catalog"][fact]:
            row["provider_id"] = "quantbase"
    case["custody_authority"]["fund_scoped_providers"] = ["QUANTBASE"]

    def activity(record_id, kind, value, **extra):
        return {
            "account_id": "acct-a1",
            "provider_id": "quantbase",
            "record_id": record_id,
            "event_date": "2026-09-10",
            "kind": kind,
            "mapping_status": "MAPPED",
            "strategy_id": "alpha",
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": value},
            **extra,
        }

    case["provider_catalog"]["transactions"] = [
        activity("div-1", "DIVIDEND", "10"),
        activity("div-2", "DIVIDEND", "2.5"),
        activity(
            "fee-cash", "FEE_JOURNAL:PARTNER_SUB_FEE", "-4", funding="ACCOUNT_CASH"
        ),
        activity(
            "fee-fund", "FEE_JOURNAL:PARTNER_SUB_FEE", "-3", funding="IN_FUND_SELL"
        ),
        activity("fee-sell", "FEE_FUNDING_SELL", "-3"),
        # Another provider's, an unmapped fund's and a later row are not this line.
        {**activity("div-other", "DIVIDEND", "99"), "provider_id": "matrixport"},
        {
            **activity("div-unmapped", "DIVIDEND", "98"),
            "mapping_status": "UNMAPPED",
            "strategy_id": None,
        },
        {**activity("div-late", "DIVIDEND", "97"), "event_date": "2026-09-18"},
    ]

    block = baseline._build_current_capital(**case)

    row = block["strategies"][0]
    assert row["current_value"]["value"] == "105"
    lines = {line["label"]: line for line in row["account_cash_lines"]}
    assert (
        lines["DIVIDENDS_PAID_TO_ACCOUNT_CASH_NOT_IN_FUND_NAV"]["current_value"][
            "value"
        ]
        == "12.5"
    )
    assert lines["DIVIDENDS_PAID_TO_ACCOUNT_CASH_NOT_IN_FUND_NAV"]["event_count"] == 2
    assert lines["FEES_PAID_FROM_ACCOUNT_CASH"]["current_value"]["value"] == "-4"
    assert lines["IN_FUND_FEE_FUNDED_SELLS_COST"]["current_value"]["value"] == "-3"
    _cc_schema_validator("current_capital").validate(block)
    arguments = {
        key: case[key]
        for key in ("assignments", "nav", "provider_admissions", "provider_catalog")
    }
    assert all(
        check["status"] != "FAIL"
        for check in baseline._current_capital_checks(block, **arguments)
    )
    block["strategies"][0]["account_cash_lines"][0]["current_value"]["value"] = "13"
    failed = [
        check
        for check in baseline._current_capital_checks(block, **arguments)
        if check["status"] == "FAIL"
    ]
    assert failed and "account cash lines differ" in failed[0]["error"]


def test_matrixport_wallet_sleeve_equals_the_product_nav():
    """#568: a wallet that funds products builds no derived cash; sleeve equals NAV.

    The stablecoin conversion between a wallet deposit and the product
    subscription is a cost outside capital, never a cash line in the sleeve.
    """

    case = _cr2_custody_cash_case()
    for fact in ("flows", "capital_anchors"):
        for row in case["provider_catalog"][fact]:
            row["provider_id"] = "matrixport"
    case["custody_authority"]["fund_scoped_providers"] = ["MATRIXPORT"]

    block = baseline._build_current_capital(**case)

    row = block["strategies"][0]
    nav = row["members"][0]["value"]["value"]
    assert "derived_cash" not in row
    assert row["current_value"]["value"] == nav
    _cc_schema_validator("current_capital").validate(block)

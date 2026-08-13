from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
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


def _published_provenance(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "private-provenance"
    strategies = []
    for index in range(1, 10):
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
                "strategy_id": f"strategy-{index}",
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


def _spec(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
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

    provenance_root, generation_id, catalog_hash = _published_provenance(tmp_path)
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


def _spec_with_schwab_provider(tmp_path: Path) -> Path:
    spec_path, spec = _spec(tmp_path)
    provider_root = tmp_path / "dataSchwab"
    provider_catalog = {
        "accounts": [
            {"account_id": "account-schwab-a"},
            {"account_id": "account-schwab-b"},
        ],
        "as_of_date": "2026-07-30",
        "catalog_schema_version": 1,
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
    catalog_bytes = canonical_json_bytes(provider_catalog)
    catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
    artifact = SourceArtifact(
        artifact_id="normalized/schwab-generation.json",
        sha256=catalog_hash,
        size_bytes=len(catalog_bytes),
        as_of_date=date(2026, 7, 30),
        schema_name="schwab-generation-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 30),
        adapter=AdapterIdentity("synthetic-schwab", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic accepted Schwab slice",
            fact_types=("nav", "positions", "transactions"),
        ),
        artifacts=(artifact,),
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


def test_hwm_inputs_absent_omits_catalog_key(tmp_path: Path) -> None:
    spec_path, _spec_data = _spec(tmp_path)
    catalog, _manifest, _bytes = baseline.build_catalog(spec_path)
    assert "hwm_inputs" not in catalog


def test_hwm_inputs_absent_output_is_byte_identical_to_pre_envelope_baseline(
    tmp_path: Path,
) -> None:
    """Golden byte-identity regression: a spec without the hwm-inputs source
    must produce a catalog and generation manifest byte-identical to the
    pre-envelope builder (hashes captured from the pre-envelope code path)."""
    spec_path, _spec_data = _spec(tmp_path)
    output_root = tmp_path / "private-output"
    evidence = baseline.stage_baseline(spec_path, output_root)
    generation_dir = output_root / "generations" / evidence["generation_id"]
    catalog_bytes = (generation_dir / "portfolio-baseline.json").read_bytes()
    manifest_bytes = (generation_dir / "generation.json").read_bytes()
    assert b'"hwm_inputs"' not in catalog_bytes
    assert (
        hashlib.sha256(catalog_bytes).hexdigest()
        == "fde46f735538e0cce5246e32e9ce15e9e071ed0d0d992507c954969f8f8e61ce"
    )
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == "58a0fcf30bc5b21a09444b1fbdd19cc5d9c7f10a4f42d382472cbd8cfc8f8b39"
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
        match="does not resolve to an admitted source artifact",
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
        match="has schema role 'synthetic-schwab-flows'",
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
        match="periods lane 'PROVIDER' has no admitted calendar authority",
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
        match="'account-schwab-b' is not mapped to strategy 'strategy-1'",
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
        match="lacks hash-bound NAV rollforward evidence",
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
        match="requires hash-bound provider capital evidence",
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
        match="LEGACY lane has no admitted effective-dated capital schedule",
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
        match="not a known strategy",
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

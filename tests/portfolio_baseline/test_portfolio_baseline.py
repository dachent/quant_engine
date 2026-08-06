from __future__ import annotations

import csv
from datetime import date
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
    assert catalog_schema["additionalProperties"] is False
    assert catalog_schema["properties"]["positions"]["items"] == {"type": "object"}
    assert spec_schema["properties"]["schema_version"] == {"enum": [1, 2, 3]}
    assert catalog_schema["properties"]["strategies"]["minItems"] == 9
    assert catalog_schema["properties"]["strategies"]["maxItems"] == 9


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

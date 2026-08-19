from __future__ import annotations

import copy
import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_target_series_layered_history",
    ROOT / "tools" / "portfolio_target_series.py",
)
assert SPEC is not None and SPEC.loader is not None
target_series = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = target_series
SPEC.loader.exec_module(target_series)

TargetSeriesError = target_series.TargetSeriesError
build_layered_history_generation = target_series.build_layered_history_generation
validate_layered_history_generation = target_series.validate_layered_history_generation
write_layered_history_generation = target_series.write_layered_history_generation
verify_layered_history_generation = target_series.verify_layered_history_generation


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def _artifact(path: Path, artifact_id: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _manifest(
    path: Path,
    generation_id: str,
    as_of: str,
    artifacts: list[dict[str, object]],
) -> None:
    _write_json(
        path,
        {
            "artifacts": artifacts,
            "as_of_date": as_of,
            "generation_id": generation_id,
            "verification": "VERIFIED",
        },
    )


def _pointer(path: Path, generation_id: str, manifest_path: Path) -> None:
    source_name = {
        "ledger": "provider-ledger",
        "matrixport": "matrixport",
        "quantbase": "quantbase",
        "p123": "portfolio123",
    }[path.parent.name]
    manifest_content = manifest_path.read_bytes()
    manifest = json.loads(manifest_content)
    _write_json(
        path,
        {
            "artifact_inventory_sha256": (
                target_series._source_generation_inventory_sha256(
                    source_name, manifest_content, manifest
                )
            ),
            "generation_id": generation_id,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "pointer_schema_version": 2,
            "state": "PUBLISHED",
        },
    )


def _update_source_generation(
    contract: dict[str, Any], source_id: str, manifest: Path, as_of: str
) -> None:
    source = next(
        item for item in contract["source_segments"] if item["source_id"] == source_id
    )
    generation = json.loads(manifest.read_text(encoding="utf-8"))
    pointer_path = manifest.parent / "CURRENT.json"
    if pointer_path.exists():
        _pointer(pointer_path, generation["generation_id"], manifest)
    source["current_generation"] = {
        "as_of": as_of,
        "generation_id": generation["generation_id"],
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }


def _update_correction_generations(
    contract: dict[str, Any],
    source_id: str,
    manifest: Path,
    as_of: str,
    authority_root: str,
) -> None:
    generation = json.loads(manifest.read_text(encoding="utf-8"))
    generation_id = generation["generation_id"]
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    for correction in contract["history_corrections"]["corrections"]:
        if correction["source_id"] != source_id:
            continue
        correction["capture"] = {
            "as_of": as_of,
            "capture_defect": None,
            "generation_id": generation_id,
            "generation_sha256": manifest_sha256,
            "status": "CAPTURED_GOVERNED",
        }
        correction["source_receipts"] = [
            {
                "artifact_role": f"{source_id}-generation-manifest",
                "artifact_selector": f"generations/{generation_id}/generation.json",
                "authority_root": authority_root,
                "generation_id": generation_id,
                "locator_kind": "GENERATION_MANIFEST",
                "manifest_artifact_id": None,
                "sha256": manifest_sha256,
            }
        ]


def _append_p123_performance_receipt(
    contract: dict[str, Any], manifest: Path, performance_path: Path
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    correction["source_receipts"].append(
        {
            "artifact_role": "portfolio123-managed-performance",
            "artifact_selector": (
                f"generations/{generation_id}/tables/har_managed_performance.parquet"
            ),
            "authority_root": "PORTFOLIO123",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "tables/har_managed_performance.parquet",
            "sha256": hashlib.sha256(performance_path.read_bytes()).hexdigest(),
        }
    )


def _append_p123_ai_receipts(
    contract: dict[str, Any],
    manifest: Path,
    returns_path: Path,
    relationships_path: Path,
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-ai-backtest-candidate"
    )
    for artifact_role, artifact_id, path in (
        (
            "portfolio123-simulation-returns",
            "tables/returns_daily.parquet",
            returns_path,
        ),
        (
            "portfolio123-strategy-relationships",
            "tables/strategy_relationships.parquet",
            relationships_path,
        ),
    ):
        correction["source_receipts"].append(
            {
                "artifact_role": artifact_role,
                "artifact_selector": f"generations/{generation_id}/{artifact_id}",
                "authority_root": "PORTFOLIO123",
                "generation_id": generation_id,
                "locator_kind": "GENERATION_ARTIFACT",
                "manifest_artifact_id": artifact_id,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    _rows, identity = target_series._portfolio123_simulation_returns(
        returns_path,
        relationships_path,
        "live-ai",
        "sim-ai",
        "AI Factor [P123]",
    )
    correction["derived_backtest_identity"] = identity
    correction["interval"]["observed_start"] = identity["observed_start"]
    correction["interval"]["observed_end"] = identity["observed_end"]


def _update_echo_rejected_identity(
    contract: dict[str, Any],
    manifest: Path,
    catalog: Path,
    series_id: str,
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    target_content, _evidence, _proof, _manifest = (
        target_series.build_provider_ledger_targets(manifest, catalog)
    )
    rows = [
        row
        for row in target_series._target_rows(target_content)
        if row["series_id"] == series_id
    ]
    identity = target_series._target_row_identity(rows)
    catalog_sha256 = hashlib.sha256(catalog.read_bytes()).hexdigest()
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "echotrade-event-only-account-live-rejected"
    )
    correction["source_receipts"] = [
        receipt
        for receipt in correction["source_receipts"]
        if receipt["artifact_role"] != "provider-ledger-catalog"
    ]
    correction["source_receipts"].append(
        {
            "artifact_role": "provider-ledger-catalog",
            "artifact_selector": f"generations/{generation_id}/provider-ledger.json",
            "authority_root": "PROVIDER_LEDGER",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "normalized/provider-ledger.json",
            "sha256": catalog_sha256,
        }
    )
    correction["rejected_evidence_identity"] = {
        "canonicalization": "TARGET_FIELDS_CANONICAL_JSON_V1",
        "return_observations": identity["return_observations"],
        "sha256": identity["sha256"],
        "source_receipt_sha256": catalog_sha256,
    }


def _update_correction_interval(
    contract: dict[str, Any], correction_id: str, **values: str | None
) -> None:
    correction = next(
        item
        for item in contract["history_corrections"]["corrections"]
        if item["correction_id"] == correction_id
    )
    correction["interval"].update(values)


def _history_fixture(tmp_path: Path) -> dict[str, Any]:
    as_of = "2026-01-10"
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    ids = {
        "ledger": "1" * 64,
        "matrixport": "2" * 64,
        "quantbase": "3" * 64,
        "p123": "4" * 64,
    }

    ledger_catalog = tmp_path / "ledger" / "provider-ledger.json"
    series = {
        "echotrade-growth-pivots": "Growth at Market Pivot Points [EchoTrade]",
        "grizzlybears-vtma": "GrizzlyBears VTMA",
        "p123-ai-factor": "AI Factor [P123]",
        "p123-eu": "EU [P123]",
        "qsi": "QSI",
        "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
        "tt-portfolio": "TT Portfolio",
        "volatis-strategy": "Volatis Strategy",
    }
    ledger_rows: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for strategy_id, series_id in series.items():
        dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        if strategy_id == "p123-eu":
            dates = [
                "2025-11-21",
                "2025-11-24",
                "2025-11-25",
                "2025-11-26",
                "2025-11-27",
            ]
        if strategy_id == "robuxio-crypto":
            dates = [
                "2025-12-07",
                "2025-12-08",
                "2025-12-09",
                "2025-12-10",
                "2025-12-11",
            ]
        if strategy_id == "volatis-strategy":
            dates = [
                "2024-10-16",
                "2024-10-17",
                "2024-10-18",
                "2024-10-19",
                "2024-10-20",
            ]
        for index, observed in enumerate(dates, start=1):
            ledger_rows.append(
                {
                    "cost": None,
                    "date": observed,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": ids["ledger"],
                    "provider_id": "synthetic",
                    "return": str(index / 1000),
                    "return_basis": "NET",
                    "return_type": "TOTAL",
                    "strategy_id": strategy_id,
                    "strategy_name": series_id,
                }
            )
        account = {
            "observed_end": dates[-1],
            "observed_start": dates[0],
            "return_observations": len(dates),
        }
        row: dict[str, object] = {
            "account": account,
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_as_of_date": as_of,
            "generation_id": ids["ledger"],
            "provider_id": "synthetic",
            "sources": {"account_returns": {"sha256": "a" * 64}},
            "strategy_id": strategy_id,
            "strategy_name": series_id,
        }
        if strategy_id == "p123-ai-factor":
            row.update(
                {
                    "provider": {
                        "entity_id": "live-ai",
                        "provider_id": "portfolio123",
                    },
                    "overlap": {"observations": 2, "status": "TESTED_EXACT_DATE_PATHS"},
                }
            )
        evidence.append(row)
    _write_json(
        ledger_catalog,
        {
            "as_of_date": as_of,
            "catalog_schema_version": 1,
            "strategy_evidence": evidence,
            "strategy_returns": ledger_rows,
        },
    )
    ledger_manifest = tmp_path / "ledger" / "generation.json"
    _manifest(
        ledger_manifest,
        ids["ledger"],
        as_of,
        [_artifact(ledger_catalog, "normalized/provider-ledger.json")],
    )
    ledger_pointer = tmp_path / "ledger" / "CURRENT.json"
    _pointer(ledger_pointer, ids["ledger"], ledger_manifest)

    matrix_root = tmp_path / "matrixport"
    matrix_nav = matrix_root / "nav_daily.csv"
    _write_csv(
        matrix_nav,
        ["nav", "product_id", "timestamp_utc"],
        [
            {
                "nav": str(100 + index),
                "product_id": "private-product",
                "timestamp_utc": f"2025-12-{day:02d}T00:00:00Z",
            }
            for index, day in enumerate(range(1, 12))
            if day != 9
        ],
    )
    matrix_holdings = matrix_root / "holdings.csv"
    _write_csv(
        matrix_holdings,
        ["product_id", "shares"],
        [{"product_id": "private-product", "shares": "10"}],
    )
    matrix_subscriptions = matrix_root / "subscription_details.csv"
    _write_csv(
        matrix_subscriptions,
        [
            "confirm_nav_time_utc",
            "product_id",
            "status",
            "subscription_share",
            "subscription_time_utc",
        ],
        [
            {
                "confirm_nav_time_utc": "2025-12-06T00:00:00Z",
                "product_id": "private-product",
                "status": "300",
                "subscription_share": "10",
                "subscription_time_utc": "2025-12-05T20:00:00Z",
            }
        ],
    )
    matrix_manifest = matrix_root / "generation.json"
    _manifest(
        matrix_manifest,
        ids["matrixport"],
        as_of,
        [
            _artifact(matrix_nav, "csv/nav_daily.csv"),
            _artifact(matrix_holdings, "csv/holdings.csv"),
            _artifact(matrix_subscriptions, "csv/subscription_details.csv"),
        ],
    )
    matrix_manifest_value = json.loads(matrix_manifest.read_text(encoding="utf-8"))
    matrix_manifest_value["coverage"] = {
        "declared_exclusions": _matrixport_exclusions("2025-12-09"),
        "known_gaps": [],
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }
    _write_json(matrix_manifest, matrix_manifest_value)
    matrix_pointer = matrix_root / "CURRENT.json"
    _pointer(matrix_pointer, ids["matrixport"], matrix_manifest)

    quantbase_root = tmp_path / "quantbase"
    quantbase_history = quantbase_root / "history.csv"
    _write_csv(
        quantbase_history,
        ["time", "value", "cost_basis", "profit_loss"],
        [
            {"time": "10/14/2024", "value": "0", "cost_basis": "0", "profit_loss": "0"},
            {
                "time": "10/15/2024",
                "value": "100",
                "cost_basis": "100",
                "profit_loss": "0",
            },
            {
                "time": "10/16/2024",
                "value": "101",
                "cost_basis": "100",
                "profit_loss": "1",
            },
            {
                "time": "10/17/2024",
                "value": "102",
                "cost_basis": "100",
                "profit_loss": "2",
            },
            {
                "time": "10/18/2024",
                "value": "103",
                "cost_basis": "100",
                "profit_loss": "3",
            },
            {
                "time": "10/19/2024",
                "value": "104",
                "cost_basis": "100",
                "profit_loss": "4",
            },
            {
                "time": "10/20/2024",
                "value": "105",
                "cost_basis": "100",
                "profit_loss": "5",
            },
            {
                "time": "10/21/2024",
                "value": "106",
                "cost_basis": "100",
                "profit_loss": "6",
            },
            {
                "time": "10/22/2024",
                "value": "107",
                "cost_basis": "100",
                "profit_loss": "7",
            },
            {
                "time": "10/23/2024",
                "value": "108",
                "cost_basis": "100",
                "profit_loss": "8",
            },
        ],
    )
    quantbase_activity = quantbase_root / "activity.csv"
    _write_csv(
        quantbase_activity,
        ["activity_type", "amount", "fund", "is_rebalance", "time", "type"],
        [
            {
                "activity_type": "filled_txn",
                "amount": "100",
                "fund": "private-fund",
                "is_rebalance": "false",
                "time": "2024-10-15T12:00:00Z",
                "type": "BUY",
            }
        ],
    )
    quantbase_manifest = quantbase_root / "generation.json"
    _manifest(
        quantbase_manifest,
        ids["quantbase"],
        as_of,
        [
            _artifact(
                quantbase_history,
                "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            ),
            _artifact(quantbase_activity, "source/csv/account_activity.csv"),
        ],
    )
    quantbase_pointer = quantbase_root / "CURRENT.json"
    _pointer(quantbase_pointer, ids["quantbase"], quantbase_manifest)

    p123_root = tmp_path / "p123"
    p123_root.mkdir()
    metadata_path = p123_root / "metadata.parquet"
    metadata_row = {
        "assetName": "EU Multifactor Microcap",
        "assetUid": "exact-asset",
        "currency": "USD",
        "inceptionDt": "2025-11-12",
        "isAccountLive": "true",
        "name": "EU Multifactor Microcap",
        "portfolioUid": "exact-portfolio",
        "posCnt": "13",
        "strategyTotalHoldCount": "13",
    }
    pq.write_table(
        pa.Table.from_pylist([metadata_row, metadata_row, metadata_row]), metadata_path
    )
    performance_path = p123_root / "performance.parquet"
    panels = [
        {"graphs": [{"idx": 0, "s": "Strategy"}]},
        {"graphs": [{"idx": 1, "s": "Total Value"}]},
    ]
    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [2.0, 102.0, 99.0],
        [3.0, 103.0, 100.0],
        [4.0, 104.0, 101.0],
        [5.0, 105.0, 102.0],
        [6.0, 106.0, 103.0],
    ]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "exact-asset",
                    "field": "data",
                    "value": json.dumps(data),
                },
                {"_entity_id": "exact-asset", "field": "date", "value": "2025-11-13"},
                {
                    "_entity_id": "exact-asset",
                    "field": "dateLast",
                    "value": "2025-11-21",
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "panels",
                    "value": json.dumps(panels),
                },
            ]
        ),
        performance_path,
    )
    equity_path = p123_root / "equity.parquet"
    weekdays = [
        "2025-11-13",
        "2025-11-14",
        "2025-11-17",
        "2025-11-18",
        "2025-11-19",
        "2025-11-20",
        "2025-11-21",
    ]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "Cash Added": row[2],
                    "Date": observed,
                    "Total Equity": row[1],
                    "_entity_id": "exact-asset",
                }
                for observed, row in zip(weekdays, data, strict=True)
            ]
        ),
        equity_path,
    )
    returns_path = p123_root / "returns.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_id": "sim-ai",
                    "model_return": float(100 + index),
                    "period_date": observed,
                    "source": "synthetic",
                }
                for index, observed in enumerate(
                    ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]
                )
            ]
        ),
        returns_path,
    )
    relationships_path = p123_root / "relationships.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_ref_a": "live-ai",
                    "entity_ref_b": "sim-ai",
                    "relationship_type": "live_sim_twin",
                    "notes": "synthetic",
                }
            ]
        ),
        relationships_path,
    )
    p123_manifest = p123_root / "generation.json"
    _manifest(
        p123_manifest,
        ids["p123"],
        as_of,
        [
            _artifact(metadata_path, "tables/har_managed_metadata.parquet"),
            _artifact(performance_path, "tables/har_managed_performance.parquet"),
            _artifact(equity_path, "tables/har_equity_curve.parquet"),
            _artifact(returns_path, "tables/returns_daily.parquet"),
            _artifact(relationships_path, "tables/strategy_relationships.parquet"),
        ],
    )
    p123_pointer = p123_root / "CURRENT.json"
    _pointer(p123_pointer, ids["p123"], p123_manifest)

    p123_version = target_series._strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": ids["p123"],
            "provider_entity_ids": ["exact-asset", "exact-portfolio"],
            "strategy_name": "EU Multifactor Microcap",
        },
    )
    p123_strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "p123-eu"
    )
    p123_strategy["strategy_version"] = {
        "status": "EVIDENCED",
        "version_ref": p123_version,
    }
    for source_id in ("portfolio123-model-generation", "owner-account-live-history"):
        source = next(
            row for row in contract["source_segments"] if row["source_id"] == source_id
        )
        source["strategy_version"]["version_refs"]["p123-eu"] = p123_version
    for correction_id in (
        "p123-eu-backtest-not-captured",
        "p123-eu-provider-live",
        "p123-eu-account-live",
    ):
        correction = next(
            row
            for row in contract["history_corrections"]["corrections"]
            if row["correction_id"] == correction_id
        )
        correction["strategy_version"]["version_ref"] = p123_version

    _update_correction_interval(
        contract,
        "robuxio-provider-live-before-funding",
        observed_start="2025-12-01",
        observed_end="2025-12-05",
        effective_start="2025-12-01",
        effective_end="2025-12-05",
    )
    _update_correction_interval(
        contract,
        "robuxio-account-live-after-funding",
        observed_start="2025-12-07",
        observed_end="2025-12-11",
        effective_start="2025-12-06",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "p123-eu-provider-live",
        observed_start="2025-11-14",
        observed_end="2025-11-21",
        effective_start="2025-11-14",
        effective_end="2025-11-21",
    )
    p123_provider_correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    p123_provider_correction["derived_evidence_identity"] = {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 7,
        "observed_end": "2025-11-21",
        "observed_start": "2025-11-14",
        "raw_level_observations": 7,
        "return_observations": 6,
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    _update_correction_interval(
        contract,
        "p123-eu-account-live",
        observed_start="2025-11-21",
        observed_end="2025-11-27",
        effective_start="2025-11-20",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "volatis-provider-live",
        observed_start="2024-10-15",
        observed_end="2024-10-23",
        effective_start="2024-10-15",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "volatis-account-live",
        observed_start="2024-10-16",
        observed_end="2024-10-23",
        effective_start="2024-10-15",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "echotrade-event-only-account-live-rejected",
        observed_start="2026-01-05",
        observed_end="2026-01-09",
        effective_start=None,
        effective_end=None,
    )
    for source_id, manifest, authority_root in (
        ("owner-account-live-history", ledger_manifest, "PROVIDER_LEDGER"),
        ("matrixport-account-generation", matrix_manifest, "MATRIXPORT"),
        ("quantbase-account-generation", quantbase_manifest, "QUANTBASE"),
        ("portfolio123-model-generation", p123_manifest, "PORTFOLIO123"),
    ):
        _update_source_generation(contract, source_id, manifest, as_of)
        _update_correction_generations(
            contract, source_id, manifest, as_of, authority_root
        )
    _update_echo_rejected_identity(
        contract,
        ledger_manifest,
        ledger_catalog,
        series["echotrade-growth-pivots"],
    )
    matrixport_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    matrixport_source["declared_gap_policy"] = _matrixport_gap_policy("2025-12-09")
    _append_p123_performance_receipt(contract, p123_manifest, performance_path)
    _append_p123_ai_receipts(contract, p123_manifest, returns_path, relationships_path)
    contract_path = tmp_path / "contract.json"
    sources_by_id = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        generation = sources_by_id[identity["source_id"]]["current_generation"]
        identity["source_generation_id"] = generation["generation_id"]
        identity["source_generation_sha256"] = generation["sha256"]
        version_refs = sources_by_id[identity["source_id"]]["strategy_version"][
            "version_refs"
        ]
        identity["strategy_version"] = (
            version_refs.get(identity["strategy_id"]) or "UNRESOLVED"
        )
    _write_json(contract_path, contract)

    fixture = {
        "as_of_date": as_of,
        "contract_path": contract_path,
        "provider_ledger_pointer_path": ledger_pointer,
        "provider_ledger_manifest_path": ledger_manifest,
        "provider_ledger_catalog_path": ledger_catalog,
        "matrixport_pointer_path": matrix_pointer,
        "matrixport_manifest_path": matrix_manifest,
        "matrixport_nav_path": matrix_nav,
        "matrixport_holdings_path": matrix_holdings,
        "matrixport_subscription_path": matrix_subscriptions,
        "matrixport_product_id": "private-product",
        "quantbase_pointer_path": quantbase_pointer,
        "quantbase_manifest_path": quantbase_manifest,
        "quantbase_history_path": quantbase_history,
        "quantbase_activity_path": quantbase_activity,
        "quantbase_fund_id": "private-fund",
        "portfolio123_pointer_path": p123_pointer,
        "portfolio123_manifest_path": p123_manifest,
        "portfolio123_metadata_path": metadata_path,
        "portfolio123_performance_path": performance_path,
        "portfolio123_equity_path": equity_path,
        "portfolio123_returns_path": returns_path,
        "portfolio123_relationships_path": relationships_path,
    }
    _refresh_materialized_segment_identities(fixture)
    return fixture


def _build(fixture: dict[str, Any]) -> tuple[dict[str, bytes], bytes]:
    return build_layered_history_generation(
        repository_sha=target_series._trusted_layered_history_code_identity()[0],
        **fixture,
    )


def _refresh_materialized_segment_identities(fixture: dict[str, Any]) -> None:
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        generation = sources[identity["source_id"]]["current_generation"]
        identity["source_generation_id"] = generation["generation_id"]
        identity["source_generation_sha256"] = generation["sha256"]
        version_refs = sources[identity["source_id"]]["strategy_version"][
            "version_refs"
        ]
        identity["strategy_version"] = (
            version_refs.get(identity["strategy_id"]) or "UNRESOLVED"
        )
    _write_json(contract_path, contract)
    artifacts, _manifest_content = _build(fixture)
    rows = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8"))))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    contract["history_corrections"]["materialized_segment_identities"] = (
        target_series._materialized_segment_identities(rows, sources)
    )
    _write_json(contract_path, contract)


def _matrixport_exclusions(*dates: str) -> list[dict[str, str]]:
    return [
        {
            "item": f"{kind}:{observed}",
            "provenance": "owner-accepted 2026-08-14, issue #287",
            "reason": "provider NAV endpoint returned no data for this date",
        }
        for kind in (
            "fx_missing_date",
            "nav_missing_date",
            "reconciliation_incomplete_date",
        )
        for observed in dates
    ]


def _matrixport_gap_policy(*dates: str) -> dict[str, object]:
    missing = frozenset(target_series.date.fromisoformat(value) for value in dates)
    omitted = target_series._matrixport_expected_gap_endpoints(missing)
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    policy = dict(source["declared_gap_policy"])
    derived = target_series._matrixport_source_gap_policy(missing, omitted)
    derived.pop("authority_bound")
    policy.update(derived)
    return policy


def _inject_matrixport_gap(fixture: dict[str, Any], missing_date: str) -> None:
    nav_path = fixture["matrixport_nav_path"]
    nav_rows = list(csv.DictReader(io.StringIO(nav_path.read_text(encoding="utf-8"))))
    retained = [
        row
        for row in nav_rows
        if not row["timestamp_utc"].startswith(f"{missing_date}T")
    ]
    _write_csv(nav_path, list(nav_rows[0]), retained)

    manifest_path = fixture["matrixport_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "csv/nav_daily.csv"
    )
    artifact.update(_artifact(nav_path, "csv/nav_daily.csv"))
    manifest["coverage"]["declared_exclusions"] = _matrixport_exclusions(missing_date)
    _write_json(manifest_path, manifest)
    _pointer(
        fixture["matrixport_pointer_path"], manifest["generation_id"], manifest_path
    )

    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    source["declared_gap_policy"] = _matrixport_gap_policy(missing_date)
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)


def _reidentify_manifest(manifest: dict[str, Any]) -> bytes:
    without_id = dict(manifest)
    without_id.pop("generation_id", None)
    manifest["generation_id"] = hashlib.sha256(
        canonical_json_bytes(without_id)
    ).hexdigest()
    return canonical_json_bytes(manifest)


def _csv_bytes(fields: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _replace_output(
    artifacts: dict[str, bytes], manifest: dict[str, Any], name: str, content: bytes
) -> None:
    artifacts[name] = content
    manifest["outputs"][name]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["outputs"][name]["size_bytes"] = len(content)
    if name.endswith(".csv"):
        manifest["outputs"][name]["row_count"] = max(
            content.decode("utf-8").count("\n") - 1, 0
        )


def test_layered_history_code_identity_ignores_inherited_git_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_identity = target_series._trusted_layered_history_code_identity()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-worktree"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "foreign-objects"))
    target_series._trusted_layered_history_code_identity.cache_clear()
    assert target_series._trusted_layered_history_code_identity() == trusted_identity


def test_layered_history_rejects_repository_dependency_outside_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _history_fixture(tmp_path)
    committed = dict(target_series._committed_layered_history_dependency_sha256s())
    committed["tools/portfolio_financial_mvp_contract.py"] = "0" * 64
    monkeypatch.setattr(
        target_series,
        "_committed_layered_history_dependency_sha256s",
        lambda: committed,
    )
    with pytest.raises(TargetSeriesError, match="repository dependency differs"):
        _build(fixture)


def test_layered_history_rejects_dynamic_validator_with_different_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _history_fixture(tmp_path)
    fake_core_path = tmp_path / "foreign_core" / "contracts.py"
    fake_core_path.parent.mkdir()
    fake_core_path.write_text(
        "import json\n"
        "def canonical_json_bytes(value):\n"
        "    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\\n'\n",
        encoding="utf-8",
    )
    fake_spec = importlib.util.spec_from_file_location(
        "quant_engine.core.contracts", fake_core_path
    )
    assert fake_spec is not None and fake_spec.loader is not None
    fake_core = importlib.util.module_from_spec(fake_spec)
    fake_spec.loader.exec_module(fake_core)
    monkeypatch.setitem(sys.modules, "quant_engine.core.contracts", fake_core)
    with pytest.raises(TargetSeriesError, match="financial MVP contract is invalid"):
        _build(fixture)


def test_layered_history_generation_is_deterministic_and_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    repeated_artifacts, repeated_manifest = _build(fixture)
    assert artifacts == repeated_artifacts
    assert manifest_content == repeated_manifest
    assert json.loads(manifest_content)["builder_version"] == "1.12.0"
    generation_schema = json.loads(
        (
            ROOT / "schemas" / "portfolio" / "layered-history-generation.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert generation_schema["properties"]["builder_version"] == {"const": "1.12.0"}
    assert (
        "strategy-history"
        in generation_schema["properties"]["source_manifests"]["properties"]
    )

    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    assert result["strategy_count"] == 9
    coverage = json.loads(artifacts["coverage.json"])
    coverage_by_strategy = {row["strategy_id"]: row for row in coverage["strategies"]}
    assert coverage_by_strategy["echotrade-growth-pivots"]["segments"] == []
    assert coverage_by_strategy["echotrade-growth-pivots"]["modes"]["LIVE_ONLY"] == {
        "reason": "CAPTURED_EVENT_ONLY_RECONSTRUCTION_NOT_ADMISSIBLE_DAILY_NAV",
        "status": "REQUIRED_NOT_READY",
    }
    assert coverage_by_strategy["composer-holy-grail"]["segments"] == []
    assert coverage_by_strategy["composer-holy-grail"]["modes"]["LIVE_ONLY"] == {
        "reason": "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT",
        "status": "REQUIRED_NOT_READY",
    }
    assert coverage["correction_resolutions"]["p123-eu-provider-live"] == {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 7,
        "observed_end": "2025-11-21",
        "observed_start": "2025-11-14",
        "raw_level_observations": 7,
        "return_observations": 6,
        "status": "DERIVED_NOT_ADMITTED_PENDING_303",
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    assert coverage["correction_resolutions"]["p123-ai-backtest"]["status"] == (
        "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    )
    echo_resolution = coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    assert {
        key: value for key, value in echo_resolution.items() if key != "sha256"
    } == {
        "observed_end": "2026-01-09",
        "observed_start": "2026-01-05",
        "return_observations": 5,
        "source_generation_id": "1" * 64,
        "status": "REJECTED_EVENT_ONLY_RECONSTRUCTION_NOT_DAILY_NAV",
    }
    assert len(echo_resolution["sha256"]) == 64
    excluded = list(
        csv.DictReader(io.StringIO(artifacts["excluded.csv"].decode("utf-8")))
    )
    assert len(excluded) == echo_resolution["return_observations"]
    assert {row["series_id"] for row in excluded} == {
        coverage_by_strategy["echotrade-growth-pivots"]["series_id"]
    }
    assert {row["evidence_layer"] for row in excluded} == {"ACCOUNT_LIVE"}
    assert not any(
        row["strategy_id"] == "p123-ai-factor" and row["evidence_layer"] == "BACKTEST"
        for row in csv.DictReader(
            io.StringIO(artifacts["segments.csv"].decode("utf-8"))
        )
    )
    live_only = artifacts["live-only.csv"].decode("utf-8")
    assert "PROVIDER_LIVE" not in live_only
    assert "BACKTEST" not in live_only
    assert "p123-eu:provider-live" not in artifacts["segments.csv"].decode("utf-8")
    assert "private-product" not in artifacts["coverage.json"].decode("utf-8")
    assert "private-fund" not in artifacts["coverage.json"].decode("utf-8")

    destination = write_layered_history_generation(
        tmp_path / "candidate", artifacts, manifest_content
    )
    assert (
        write_layered_history_generation(
            tmp_path / "candidate", artifacts, manifest_content
        )
        == destination
    )
    verified = verify_layered_history_generation(destination, fixture["contract_path"])
    assert verified["verification"] == "VALID"
    assert "assurance" not in verified

    extra_file_destination = write_layered_history_generation(
        tmp_path / "candidate-extra-file", artifacts, manifest_content
    )
    (extra_file_destination / "untracked.txt").write_text(
        "not receipt-bound", encoding="utf-8"
    )
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            extra_file_destination, fixture["contract_path"]
        )

    extra_directory_destination = write_layered_history_generation(
        tmp_path / "candidate-extra-directory", artifacts, manifest_content
    )
    (extra_directory_destination / "untracked").mkdir()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            extra_directory_destination, fixture["contract_path"]
        )

    missing_file_destination = write_layered_history_generation(
        tmp_path / "candidate-missing-file", artifacts, manifest_content
    )
    (missing_file_destination / "coverage.json").unlink()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            missing_file_destination, fixture["contract_path"]
        )

    replaced_file_destination = write_layered_history_generation(
        tmp_path / "candidate-replaced-file", artifacts, manifest_content
    )
    (replaced_file_destination / "coverage.json").unlink()
    (replaced_file_destination / "coverage.json").mkdir()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            replaced_file_destination, fixture["contract_path"]
        )

    symlink_destination = write_layered_history_generation(
        tmp_path / "candidate-symlink", artifacts, manifest_content
    )
    symlink_path = symlink_destination / "coverage.json"
    symlink_target = tmp_path / "external-coverage.json"
    symlink_target.write_bytes(artifacts["coverage.json"])
    symlink_path.unlink()
    try:
        symlink_path.symlink_to(symlink_target)
    except OSError:
        pass
    else:
        with pytest.raises(TargetSeriesError, match="generation inventory differs"):
            verify_layered_history_generation(
                symlink_destination, fixture["contract_path"]
            )

    rewritten_repository = json.loads(manifest_content)
    rewritten_repository["repository_sha"] = "0" * 40
    with pytest.raises(TargetSeriesError, match="code identity is not trusted"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(rewritten_repository),
            contract_path=fixture["contract_path"],
        )

    rewritten_tool = json.loads(manifest_content)
    rewritten_tool["tool_sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="code identity is not trusted"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(rewritten_tool),
            contract_path=fixture["contract_path"],
        )

    crlf_artifacts = dict(artifacts)
    crlf_manifest = json.loads(manifest_content)
    crlf_segments = artifacts["segments.csv"].replace(b"\n", b"\r\n")
    _replace_output(crlf_artifacts, crlf_manifest, "segments.csv", crlf_segments)
    with pytest.raises(TargetSeriesError, match="segments serialization differs"):
        validate_layered_history_generation(
            artifacts=crlf_artifacts,
            manifest_content=_reidentify_manifest(crlf_manifest),
            contract_path=fixture["contract_path"],
        )

    quoted_artifacts = dict(artifacts)
    quoted_manifest = json.loads(manifest_content)
    quoted_rows = list(
        csv.DictReader(io.StringIO(artifacts["live-only.csv"].decode("utf-8")))
    )
    quoted_buffer = io.StringIO(newline="")
    quoted_writer = csv.DictWriter(
        quoted_buffer,
        fieldnames=target_series.CORRELATION_FIELDS_V2,
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    quoted_writer.writeheader()
    quoted_writer.writerows(quoted_rows)
    _replace_output(
        quoted_artifacts,
        quoted_manifest,
        "live-only.csv",
        quoted_buffer.getvalue().encode("utf-8"),
    )
    with pytest.raises(TargetSeriesError, match="history serialization differs"):
        validate_layered_history_generation(
            artifacts=quoted_artifacts,
            manifest_content=_reidentify_manifest(quoted_manifest),
            contract_path=fixture["contract_path"],
        )

    with pytest.raises(
        TargetSeriesError, match="repository SHA differs from the executing checkout"
    ):
        build_layered_history_generation(repository_sha="0" * 40, **fixture)

    with pytest.raises(TargetSeriesError, match="required history mode is not ready"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=manifest_content,
            contract_path=fixture["contract_path"],
            require_mode="LIVE_PLUS_BACKTEST",
        )

    stale = json.loads(manifest_content)
    manifest_input = next(
        row for row in stale["inputs"] if row["input_id"] == "provider-ledger-manifest"
    )
    manifest_input["sha256"] = "f" * 64
    with pytest.raises(TargetSeriesError, match="current input differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(stale),
            contract_path=fixture["contract_path"],
        )

    rewritten_artifact = json.loads(manifest_content)
    performance_input = next(
        row
        for row in rewritten_artifact["inputs"]
        if row["input_id"] == "portfolio123-performance"
    )
    performance_input["sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="source artifact differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(rewritten_artifact),
            contract_path=fixture["contract_path"],
        )

    extra_field = json.loads(manifest_content)
    extra_field["uncontrolled_policy"] = "ACCEPT"
    with pytest.raises(TargetSeriesError, match="generation keys differ"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(extra_field),
            contract_path=fixture["contract_path"],
        )

    rewritten_echo = dict(artifacts)
    rewritten_echo_manifest = json.loads(manifest_content)
    rewritten_echo_coverage = json.loads(artifacts["coverage.json"])
    rewritten_echo_resolution = rewritten_echo_coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    rewritten_echo_resolution["sha256"] = "0" * 64
    rewritten_echo_resolution["return_observations"] += 1
    _replace_output(
        rewritten_echo,
        rewritten_echo_manifest,
        "coverage.json",
        canonical_json_bytes(rewritten_echo_coverage),
    )
    with pytest.raises(TargetSeriesError, match="event-only resolution differs"):
        validate_layered_history_generation(
            artifacts=rewritten_echo,
            manifest_content=_reidentify_manifest(rewritten_echo_manifest),
            contract_path=fixture["contract_path"],
        )

    fabricated_echo = dict(artifacts)
    fabricated_echo_manifest = json.loads(manifest_content)
    fabricated_echo_rows = list(
        csv.DictReader(io.StringIO(artifacts["excluded.csv"].decode("utf-8")))
    )
    fabricated_echo_rows[0]["return"] = "0.777"
    fabricated_echo_content = _csv_bytes(
        target_series.TARGET_FIELDS, fabricated_echo_rows
    )
    _replace_output(
        fabricated_echo,
        fabricated_echo_manifest,
        "excluded.csv",
        fabricated_echo_content,
    )
    fabricated_identity = target_series._target_row_identity(fabricated_echo_rows)
    fabricated_coverage = json.loads(artifacts["coverage.json"])
    fabricated_resolution = fabricated_coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    fabricated_resolution.update(fabricated_identity)
    _replace_output(
        fabricated_echo,
        fabricated_echo_manifest,
        "coverage.json",
        canonical_json_bytes(fabricated_coverage),
    )
    with pytest.raises(TargetSeriesError, match="product authority"):
        validate_layered_history_generation(
            artifacts=fabricated_echo,
            manifest_content=_reidentify_manifest(fabricated_echo_manifest),
            contract_path=fixture["contract_path"],
        )

    baseline_coverage = json.loads(artifacts["coverage.json"])
    ledger_catalog_path = fixture["provider_ledger_catalog_path"]
    ledger_catalog = json.loads(ledger_catalog_path.read_text(encoding="utf-8"))
    qsi_row = next(
        row for row in ledger_catalog["strategy_returns"] if row["strategy_id"] == "qsi"
    )
    qsi_row["return"] = "0.123456"
    _write_json(ledger_catalog_path, ledger_catalog)
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    ledger_manifest = json.loads(ledger_manifest_path.read_text(encoding="utf-8"))
    ledger_manifest["artifacts"] = [
        _artifact(ledger_catalog_path, "normalized/provider-ledger.json")
    ]
    _write_json(ledger_manifest_path, ledger_manifest)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _update_echo_rejected_identity(
        contract,
        ledger_manifest_path,
        ledger_catalog_path,
        "Growth at Market Pivot Points [EchoTrade]",
    )
    _write_json(fixture["contract_path"], contract)
    _refresh_materialized_segment_identities(fixture)

    changed_artifacts, changed_manifest = _build(fixture)
    changed_coverage = json.loads(changed_artifacts["coverage.json"])
    baseline_hashes = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in baseline_coverage["strategies"]
    }
    changed_hashes = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in changed_coverage["strategies"]
    }
    assert changed_hashes["qsi"] != baseline_hashes["qsi"]
    assert {
        strategy_id: segment_hashes
        for strategy_id, segment_hashes in changed_hashes.items()
        if strategy_id != "qsi"
    } == {
        strategy_id: segment_hashes
        for strategy_id, segment_hashes in baseline_hashes.items()
        if strategy_id != "qsi"
    }
    assert changed_artifacts["returns.csv"] != artifacts["returns.csv"]
    assert changed_manifest != manifest_content


def test_layered_history_replays_robuxio_funding_correction_with_bounded_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest_content = _build(fixture)
    baseline_manifest = json.loads(baseline_manifest_content)
    baseline_coverage = json.loads(baseline_artifacts["coverage.json"])
    baseline_result = validate_layered_history_generation(
        artifacts=baseline_artifacts,
        manifest_content=baseline_manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert baseline_result["verification"] == "VALID"

    subscription_path = fixture["matrixport_subscription_path"]
    _write_csv(
        subscription_path,
        [
            "confirm_nav_time_utc",
            "product_id",
            "status",
            "subscription_share",
            "subscription_time_utc",
        ],
        [
            {
                "confirm_nav_time_utc": "2025-12-07T00:00:00Z",
                "product_id": "private-product",
                "status": "300",
                "subscription_share": "10",
                "subscription_time_utc": "2025-12-06T20:00:00Z",
            }
        ],
    )
    matrixport_manifest_path = fixture["matrixport_manifest_path"]
    matrixport_manifest = json.loads(
        matrixport_manifest_path.read_text(encoding="utf-8")
    )
    matrixport_manifest["generation_id"] = "9" * 64
    subscription_artifact = next(
        row
        for row in matrixport_manifest["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    subscription_artifact.update(
        _artifact(subscription_path, "csv/subscription_details.csv")
    )
    _write_json(matrixport_manifest_path, matrixport_manifest)
    _pointer(
        fixture["matrixport_pointer_path"],
        matrixport_manifest["generation_id"],
        matrixport_manifest_path,
    )

    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _update_correction_interval(
        contract,
        "robuxio-provider-live-before-funding",
        observed_start="2025-12-01",
        observed_end="2025-12-06",
        effective_start="2025-12-01",
        effective_end="2025-12-06",
    )
    _update_correction_interval(
        contract,
        "robuxio-account-live-after-funding",
        observed_start="2025-12-08",
        observed_end="2025-12-11",
        effective_start="2025-12-07",
        effective_end=None,
    )
    _update_source_generation(
        contract,
        "matrixport-account-generation",
        matrixport_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        matrixport_manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    _write_json(contract_path, contract)
    with pytest.raises(
        TargetSeriesError, match="financial MVP contract is invalid"
    ) as exc:
        _build(fixture)
    assert exc.value.__cause__ is not None
    assert (
        "materialized history segment source receipt differs: "
        "robuxio-crypto:account-live"
    ) in str(exc.value.__cause__)
    _refresh_materialized_segment_identities(fixture)

    corrected_artifacts, corrected_manifest_content = _build(fixture)
    corrected_manifest = json.loads(corrected_manifest_content)
    corrected_coverage = json.loads(corrected_artifacts["coverage.json"])
    result = validate_layered_history_generation(
        artifacts=corrected_artifacts,
        manifest_content=corrected_manifest_content,
        contract_path=contract_path,
    )
    assert result["verification"] == "VALID"
    assert corrected_manifest["generation_id"] != baseline_manifest["generation_id"]

    baseline_robuxio = next(
        row
        for row in baseline_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    corrected_robuxio = next(
        row
        for row in corrected_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    baseline_robuxio_segments = {
        row["segment_id"]: row for row in baseline_robuxio["segments"]
    }
    corrected_robuxio_segments = {
        row["segment_id"]: row for row in corrected_robuxio["segments"]
    }
    provider_segment_id = "robuxio-crypto:provider-live-before-funding"
    account_segment_id = "robuxio-crypto:account-live"
    assert (
        corrected_robuxio_segments[provider_segment_id]["sha256"]
        != baseline_robuxio_segments[provider_segment_id]["sha256"]
    )
    assert (
        corrected_robuxio_segments[account_segment_id]["sha256"]
        != baseline_robuxio_segments[account_segment_id]["sha256"]
    )
    assert (
        baseline_robuxio_segments[provider_segment_id]["observed_start"],
        baseline_robuxio_segments[provider_segment_id]["observed_end"],
    ) == ("2025-12-02", "2025-12-05")
    assert (
        corrected_robuxio_segments[provider_segment_id]["observed_start"],
        corrected_robuxio_segments[provider_segment_id]["observed_end"],
    ) == ("2025-12-02", "2025-12-06")
    assert (
        baseline_robuxio_segments[account_segment_id]["observed_start"],
        corrected_robuxio_segments[account_segment_id]["observed_start"],
    ) == ("2025-12-07", "2025-12-08")

    original_provider_returns = target_series._matrixport_provider_returns

    def stale_provider_returns(
        nav_path: Path,
        product_id: str,
        series_id: str,
        _account_live_start: object,
    ) -> list[dict[str, str]]:
        return original_provider_returns(
            nav_path,
            product_id,
            series_id,
            target_series.date(2025, 12, 6),
        )

    monkeypatch.setattr(
        target_series, "_matrixport_provider_returns", stale_provider_returns
    )
    with pytest.raises(
        TargetSeriesError,
        match="Robuxio provider-live interval differs from contract",
    ):
        _build(fixture)
    monkeypatch.setattr(
        target_series, "_matrixport_provider_returns", original_provider_returns
    )

    baseline_segments = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in baseline_coverage["strategies"]
    }
    corrected_segments = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in corrected_coverage["strategies"]
    }
    assert baseline_segments.keys() == corrected_segments.keys()
    assert corrected_segments["robuxio-crypto"] != baseline_segments["robuxio-crypto"]
    assert {
        strategy_id: segments
        for strategy_id, segments in corrected_segments.items()
        if strategy_id != "robuxio-crypto"
    } == {
        strategy_id: segments
        for strategy_id, segments in baseline_segments.items()
        if strategy_id != "robuxio-crypto"
    }

    baseline_resolutions = baseline_coverage["correction_resolutions"]
    corrected_resolutions = corrected_coverage["correction_resolutions"]
    assert baseline_resolutions.keys() == corrected_resolutions.keys()
    assert (
        corrected_resolutions["robuxio-account-live-after-funding"]
        != baseline_resolutions["robuxio-account-live-after-funding"]
    )
    assert {
        correction_id: resolution
        for correction_id, resolution in corrected_resolutions.items()
        if correction_id != "robuxio-account-live-after-funding"
    } == {
        correction_id: resolution
        for correction_id, resolution in baseline_resolutions.items()
        if correction_id != "robuxio-account-live-after-funding"
    }

    baseline_inputs = {row["input_id"]: row for row in baseline_manifest["inputs"]}
    corrected_inputs = {row["input_id"]: row for row in corrected_manifest["inputs"]}
    assert baseline_inputs.keys() == corrected_inputs.keys()
    assert {
        input_id
        for input_id in baseline_inputs
        if baseline_inputs[input_id] != corrected_inputs[input_id]
    } == {
        "financial-mvp-contract",
        "matrixport-current",
        "matrixport-holdings",
        "matrixport-manifest",
        "matrixport-nav",
        "matrixport-subscriptions",
    }
    for input_id in ("matrixport-holdings", "matrixport-nav"):
        assert (
            corrected_inputs[input_id]["sha256"] == baseline_inputs[input_id]["sha256"]
        )
        assert (
            corrected_inputs[input_id]["size_bytes"]
            == baseline_inputs[input_id]["size_bytes"]
        )
    assert corrected_artifacts["excluded.csv"] == baseline_artifacts["excluded.csv"]
    assert baseline_artifacts.keys() == corrected_artifacts.keys()
    assert (
        baseline_manifest["source_manifests"].keys()
        == corrected_manifest["source_manifests"].keys()
    )
    assert {
        name
        for name in baseline_artifacts
        if baseline_artifacts[name] != corrected_artifacts[name]
    } == {
        "coverage.json",
        "live-only.csv",
        "live-plus-backtest.csv",
        "returns.csv",
        "segments.csv",
    }
    assert (
        baseline_manifest["source_pointers"].keys()
        == corrected_manifest["source_pointers"].keys()
    )
    assert {
        name
        for name in baseline_manifest["source_manifests"]
        if baseline_manifest["source_manifests"][name]
        != corrected_manifest["source_manifests"][name]
    } == {"matrixport"}
    assert {
        name
        for name in baseline_manifest["source_pointers"]
        if baseline_manifest["source_pointers"][name]
        != corrected_manifest["source_pointers"][name]
    } == {"matrixport"}


def test_layered_history_provider_interval_matches_current_product_authority() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    provider_correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "robuxio-provider-live-before-funding"
    )
    provider_segment = next(
        row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "robuxio-crypto:provider-live-before-funding"
    )
    target_series._validate_robuxio_provider_interval(
        [
            {"date": provider_segment["observed_start"]},
            {"date": provider_segment["observed_end"]},
        ],
        provider_correction["interval"],
    )
    assert (
        provider_segment["observed_start"]
        > provider_correction["interval"]["observed_start"]
    )
    assert (
        provider_segment["observed_end"]
        == provider_correction["interval"]["observed_end"]
    )


def test_complete_account_history_age_is_warning_not_unavailability(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    owner_history = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "owner-account-live-history"
    )
    owner_history["freshness"]["max_age_days"] = 0
    _write_json(fixture["contract_path"], contract)

    artifacts, manifest_content = _build(fixture)
    coverage = json.loads(artifacts["coverage.json"])
    qsi = next(row for row in coverage["strategies"] if row["strategy_id"] == "qsi")
    assert qsi["data_age"] == {
        "age_days": 1,
        "complete_through": "2026-01-09",
        "evaluated_on": "2026-01-10",
        "status": "WARN_COMPLETE_DATA_AGE",
        "warning_after_days": 0,
    }
    assert qsi["modes"]["LIVE_ONLY"] == {
        "reason": "WARN_ACCOUNT_LIVE_COMPLETE_DATA_AGE",
        "status": "AVAILABLE",
    }
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"


def test_layered_history_verification_applies_assurance_profile_after_validation(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    destination = write_layered_history_generation(
        tmp_path / "assurance-candidate", artifacts, manifest_content
    )
    receipt_as_of = date.fromisoformat(fixture["as_of_date"])

    for profile in ("DEVELOPMENT", "WORKSTATION_QC"):
        current = verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile=profile,
            evaluated_on=receipt_as_of,
        )
        assert current["assurance"] == {
            "evaluated_on": receipt_as_of.isoformat(),
            "profile": profile,
            "profile_policy": (
                "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
            ),
            "receipt_age_days": 0,
            "receipt_as_of": receipt_as_of.isoformat(),
            "status": "CURRENT_IMMUTABLE_RECEIPT",
        }

        reused_on = receipt_as_of + timedelta(days=4)
        reused = verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile=profile,
            evaluated_on=reused_on,
        )
        assert reused["assurance"] == {
            "evaluated_on": reused_on.isoformat(),
            "profile": profile,
            "profile_policy": (
                "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
            ),
            "receipt_age_days": 4,
            "receipt_as_of": receipt_as_of.isoformat(),
            "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
        }

    release = verify_layered_history_generation(
        destination,
        fixture["contract_path"],
        assurance_profile="RELEASE_QC",
        evaluated_on=receipt_as_of,
    )
    assert release["assurance"] == {
        "evaluated_on": receipt_as_of.isoformat(),
        "profile": "RELEASE_QC",
        "profile_policy": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
        "receipt_age_days": 0,
        "receipt_as_of": receipt_as_of.isoformat(),
        "status": "CURRENT_RELEASE_RECEIPT",
    }

    with pytest.raises(
        TargetSeriesError,
        match="release QC requires a layered history receipt for the evaluation date",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="RELEASE_QC",
            evaluated_on=receipt_as_of + timedelta(days=1),
        )
    with pytest.raises(
        TargetSeriesError,
        match="receipt exceeds the assurance evaluation date",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of - timedelta(days=1),
        )
    with pytest.raises(
        TargetSeriesError,
        match="assurance profile and evaluation date must be supplied together",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
        )
    with pytest.raises(
        TargetSeriesError,
        match="assurance profile and evaluation date must be supplied together",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            evaluated_on=receipt_as_of,
        )
    with pytest.raises(
        TargetSeriesError,
        match="required history mode is not ready: LIVE_PLUS_BACKTEST",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            require_mode="LIVE_PLUS_BACKTEST",
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of + timedelta(days=1),
        )

    for rewritten_as_of in ("20260110", "2026-W02-6"):
        noncanonical_manifest = json.loads(manifest_content)
        noncanonical_manifest["as_of_date"] = rewritten_as_of
        noncanonical_destination = write_layered_history_generation(
            tmp_path / f"noncanonical-{rewritten_as_of}",
            artifacts,
            _reidentify_manifest(noncanonical_manifest),
        )
        with pytest.raises(
            TargetSeriesError,
            match="layered history generation as-of is invalid",
        ):
            verify_layered_history_generation(
                noncanonical_destination,
                fixture["contract_path"],
                assurance_profile="RELEASE_QC",
                evaluated_on=receipt_as_of,
            )

    rewritten_contract = json.loads(
        fixture["contract_path"].read_text(encoding="utf-8")
    )
    rewritten_contract["product"]["as_of_policy"]["execution_profiles"][
        "DEVELOPMENT"
    ] = "REUSE_WITHOUT_WARNING"
    _write_json(fixture["contract_path"], rewritten_contract)
    with pytest.raises(TargetSeriesError, match="financial MVP contract is invalid"):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of,
        )


def test_layered_history_accepts_only_manifest_declared_matrixport_gap(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)

    artifacts, manifest_content = _build(fixture)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    live_dates = [
        row["date"]
        for row in segments
        if row["strategy_id"] == "robuxio-crypto"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    assert live_dates == ["2025-12-07", "2025-12-08", "2025-12-11"]
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]
    fixture_contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    fixture_source = next(
        row
        for row in fixture_contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    assert resolution["source_gap_policy"] == fixture_source["declared_gap_policy"]
    assert resolution["source_gap_policy"]["declared_missing_source_dates"] == [
        "2025-12-09"
    ]
    assert resolution["source_gap_policy"]["omitted_return_dates"] == ["2025-12-10"]
    robuxio_summary = next(
        summary
        for row in coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
        for summary in row["segments"]
        if summary["segment_id"] == "robuxio-crypto:account-live"
    )
    assert robuxio_summary["gap_disposition"] == ("COMPLETE_WITHIN_DECLARED_BOUNDARY")

    tampered_artifacts = dict(artifacts)
    tampered_manifest = json.loads(manifest_content)
    resolution["source_gap_policy"]["cumulative_return_across_declared_gaps"] = (
        "MATERIALIZED"
    )
    _replace_output(
        tampered_artifacts,
        tampered_manifest,
        "coverage.json",
        canonical_json_bytes(coverage),
    )
    with pytest.raises(TargetSeriesError, match="source gap resolution differs"):
        validate_layered_history_generation(
            artifacts=tampered_artifacts,
            manifest_content=_reidentify_manifest(tampered_manifest),
            contract_path=fixture["contract_path"],
        )


def test_layered_history_propagates_one_authoritative_gap_policy_edit(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest_content = _build(fixture)
    baseline_manifest = json.loads(baseline_manifest_content)
    baseline_coverage = json.loads(baseline_artifacts["coverage.json"])
    contract_path = fixture["contract_path"]
    manifest_path = fixture["matrixport_manifest_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    policy = source["declared_gap_policy"]
    policy.update(
        {
            "authority_issue": 999,
            "policy_id": "SYNTHETIC_OWNER_DECLARED_GAP_POLICY_V2",
            "provenance": "synthetic owner evidence for propagation",
            "reason": "synthetic provider omission",
            "return_rule": "SYNTHETIC_ADJACENT_DAY_RESET_RULE",
        }
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for exclusion in manifest["coverage"]["declared_exclusions"]:
        exclusion["provenance"] = policy["provenance"]
        exclusion["reason"] = policy["reason"]
    _write_json(manifest_path, manifest)
    _pointer(
        fixture["matrixport_pointer_path"], manifest["generation_id"], manifest_path
    )
    _update_source_generation(
        contract, "matrixport-account-generation", manifest_path, fixture["as_of_date"]
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)

    artifacts, manifest_content = _build(fixture)
    changed_manifest = json.loads(manifest_content)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=contract_path,
    )
    assert result["verification"] == "VALID"
    coverage = json.loads(artifacts["coverage.json"])
    propagated = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"]
    assert propagated == policy
    assert changed_manifest["generation_id"] != baseline_manifest["generation_id"]
    assert (
        changed_manifest["product"]["contract_sha256"]
        != (baseline_manifest["product"]["contract_sha256"])
    )

    baseline_inputs = {row["input_id"]: row for row in baseline_manifest["inputs"]}
    changed_inputs = {row["input_id"]: row for row in changed_manifest["inputs"]}
    assert {
        input_id
        for input_id in baseline_inputs
        if baseline_inputs[input_id] != changed_inputs[input_id]
    } == {
        "financial-mvp-contract",
        "matrixport-current",
        "matrixport-manifest",
    }
    assert {
        name
        for name in baseline_artifacts
        if baseline_artifacts[name] != artifacts[name]
    } == {"coverage.json"}
    assert {
        name
        for name in baseline_manifest["source_manifests"]
        if baseline_manifest["source_manifests"][name]
        != changed_manifest["source_manifests"][name]
    } == {"matrixport"}
    assert {
        name
        for name in baseline_manifest["source_pointers"]
        if baseline_manifest["source_pointers"][name]
        != changed_manifest["source_pointers"][name]
    } == {"matrixport"}

    baseline_policy = baseline_coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"]
    assert {
        field
        for field in baseline_policy
        if baseline_policy[field] != propagated[field]
    } == {
        "authority_issue",
        "policy_id",
        "provenance",
        "reason",
        "return_rule",
    }
    coverage_with_baseline_policy = copy.deepcopy(coverage)
    coverage_with_baseline_policy["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"] = baseline_policy
    assert coverage_with_baseline_policy == baseline_coverage


def test_layered_history_rejects_unproduced_matrixport_transform_fact(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    source["declared_gap_policy"]["return_transform"]["facts"].append(
        "FINANCING_LIABILITY"
    )
    _write_json(fixture["contract_path"], contract)

    with pytest.raises(TargetSeriesError, match="return transform capability differs"):
        _build(fixture)


def test_layered_history_does_not_admit_pre_live_p123_ai_without_product_version(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    returns_path = fixture["portfolio123_returns_path"]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_id": "sim-ai",
                    "model_return": float(100 + index),
                    "period_date": observed,
                    "source": "synthetic",
                }
                for index, observed in enumerate(
                    ["2025-12-29", "2025-12-30", "2025-12-31", "2026-01-02"]
                )
            ]
        ),
        returns_path,
    )
    manifest_path = fixture["portfolio123_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "tables/returns_daily.parquet"
    )
    artifact.update(_artifact(returns_path, artifact["artifact_id"]))
    _write_json(manifest_path, manifest)
    _pointer(
        fixture["portfolio123_pointer_path"], manifest["generation_id"], manifest_path
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
        "PORTFOLIO123",
    )
    _append_p123_performance_receipt(
        contract, manifest_path, fixture["portfolio123_performance_path"]
    )
    _append_p123_ai_receipts(
        contract,
        manifest_path,
        returns_path,
        fixture["portfolio123_relationships_path"],
    )
    _write_json(fixture["contract_path"], contract)

    artifacts, _manifest_content = _build(fixture)
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    assert not any(
        row["strategy_id"] == "p123-ai-factor" and row["evidence_layer"] == "BACKTEST"
        for row in segments
    )
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"]["p123-ai-backtest"]
    assert resolution["observed_end"] == "2026-01-02"
    assert resolution["status"] == (
        "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    )
    p123 = next(
        row for row in coverage["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    assert p123["modes"]["LIVE_PLUS_BACKTEST"] == {
        "reason": "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED",
        "status": "REQUIRED_NOT_READY",
    }


def test_layered_history_registers_optional_strategy_history_manifest(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    returns_path = tmp_path / "strategy-history-returns.csv"
    returns_content = _csv_bytes(
        target_series.CORRELATION_FIELDS_V2,
        [
            {
                "date": "2024-01-01",
                "evidence_layer": "BACKTEST",
                "return": "0.01",
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            },
            {
                "date": "2024-01-02",
                "evidence_layer": "BACKTEST",
                "return": "-0.005",
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            },
        ],
    )
    returns_path.write_bytes(returns_content)
    strategy_manifest = {
        "as_of_date": fixture["as_of_date"],
        "builder": "portfolio-strategy-history",
        "builder_version": "1.4.0",
        "history_dispositions": [
            {
                "history_status": (
                    "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_DERIVED_NOT_ADMITTED"
                ),
                "implementation_reconciliation_status": (
                    "BLOCKED_ADMIT_OR_DECLARE; SEPARATE_FROM_ACCOUNT_LIVE"
                ),
                "reason": "Synthetic v1.4 provider-live transform remains unadmitted.",
                "required_for_analysis": True,
                "series_id": "EU [P123]",
                "status": (
                    "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
                ),
                "strategy_version": "synthetic-p123-eu:v1",
            }
        ],
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(returns_content).hexdigest(),
            "size_bytes": len(returns_content),
        },
        "schema_version": 1,
        "series": [
            {
                "compatibility_basis": "SYNTHETIC_CURRENT_SPECIFICATION",
                "frequency": "DAILY_TRADING",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "implementation_reconciliation_status": "UNRESOLVED",
                "observed_end": "2024-01-02",
                "observed_start": "2024-01-01",
                "required_for_analysis": True,
                "return_observations": 2,
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            }
        ],
        "verification": "VERIFIED",
    }
    strategy_manifest["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(strategy_manifest)
    ).hexdigest()
    manifest_path = tmp_path / "strategy-history-manifest.json"
    _write_json(manifest_path, strategy_manifest)
    fixture["strategy_history_manifest_path"] = manifest_path
    fixture["strategy_history_returns_path"] = returns_path

    artifacts, manifest_content = _build(fixture)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    generation = json.loads(manifest_content)
    assert generation["source_manifests"]["strategy-history"] == (
        manifest_path.read_text(encoding="utf-8")
    )
    inputs = {row["input_id"]: row for row in generation["inputs"]}
    assert (
        inputs["strategy-history-manifest"]["generation_id"]
        == (strategy_manifest["bundle_id"])
    )
    assert inputs["strategy-history-returns"]["artifact_id"] == (
        "strategy-history/returns.csv"
    )

    optional_as_of_rewrite = copy.deepcopy(generation)
    optional_returns_input = next(
        row
        for row in optional_as_of_rewrite["inputs"]
        if row["input_id"] == "strategy-history-returns"
    )
    optional_returns_input["as_of_date"] = "1999-12-31"
    with pytest.raises(
        TargetSeriesError,
        match="input source authority differs: strategy-history-returns",
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(optional_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    missing_artifact_id = copy.deepcopy(strategy_manifest)
    missing_artifact_id["returns_artifact"].pop("artifact_id")
    missing_artifact_id.pop("bundle_id")
    missing_artifact_id["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(missing_artifact_id)
    ).hexdigest()
    _write_json(manifest_path, missing_artifact_id)
    with pytest.raises(TargetSeriesError, match="returns artifact keys differ"):
        _build(fixture)

    wrong_artifact_id = copy.deepcopy(strategy_manifest)
    wrong_artifact_id["returns_artifact"]["artifact_id"] = "other/returns.csv"
    wrong_artifact_id.pop("bundle_id")
    wrong_artifact_id["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(wrong_artifact_id)
    ).hexdigest()
    _write_json(manifest_path, wrong_artifact_id)
    with pytest.raises(TargetSeriesError, match="returns artifact ID differs"):
        _build(fixture)

    invalid_bundle = dict(strategy_manifest)
    invalid_bundle["bundle_id"] = "0" * 64
    _write_json(manifest_path, invalid_bundle)
    invalid_artifacts, invalid_manifest = _build(fixture)
    with pytest.raises(TargetSeriesError, match="bundle identity differs"):
        validate_layered_history_generation(
            artifacts=invalid_artifacts,
            manifest_content=invalid_manifest,
            contract_path=fixture["contract_path"],
        )


def test_layered_history_rejects_coherent_receipt_and_semantic_rewrites(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)

    noncanonical_manifest = json.dumps(json.loads(manifest_content), indent=2).encode(
        "utf-8"
    )
    with pytest.raises(TargetSeriesError, match="generation manifest is not canonical"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=noncanonical_manifest,
            contract_path=fixture["contract_path"],
        )

    pointer_rewrite = json.loads(manifest_content)
    current_input = next(
        row
        for row in pointer_rewrite["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    current_input["sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="current pointer differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(pointer_rewrite),
            contract_path=fixture["contract_path"],
        )

    inventory_rewrite = json.loads(manifest_content)
    rewritten_pointer = json.loads(
        inventory_rewrite["source_pointers"]["provider-ledger"]
    )
    rewritten_pointer["artifact_inventory_sha256"] = "0" * 64
    rewritten_pointer_content = canonical_json_bytes(rewritten_pointer)
    inventory_rewrite["source_pointers"]["provider-ledger"] = (
        rewritten_pointer_content.decode("utf-8")
    )
    inventory_input = next(
        row
        for row in inventory_rewrite["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    inventory_input["sha256"] = hashlib.sha256(rewritten_pointer_content).hexdigest()
    inventory_input["size_bytes"] = len(rewritten_pointer_content)
    with pytest.raises(TargetSeriesError, match="current pointer inventory differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(inventory_rewrite),
            contract_path=fixture["contract_path"],
        )

    pointer_downgrade = json.loads(manifest_content)
    downgraded_pointer = json.loads(
        pointer_downgrade["source_pointers"]["provider-ledger"]
    )
    downgraded_pointer["pointer_schema_version"] = 1
    downgraded_pointer.pop("artifact_inventory_sha256")
    downgraded_pointer_content = canonical_json_bytes(downgraded_pointer)
    pointer_downgrade["source_pointers"]["provider-ledger"] = (
        downgraded_pointer_content.decode("utf-8")
    )
    downgraded_input = next(
        row
        for row in pointer_downgrade["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    downgraded_input["sha256"] = hashlib.sha256(downgraded_pointer_content).hexdigest()
    downgraded_input["size_bytes"] = len(downgraded_pointer_content)
    with pytest.raises(TargetSeriesError, match="source pointer contract differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(pointer_downgrade),
            contract_path=fixture["contract_path"],
        )

    input_order_rewrite = json.loads(manifest_content)
    input_order_rewrite["inputs"][0], input_order_rewrite["inputs"][1] = (
        input_order_rewrite["inputs"][1],
        input_order_rewrite["inputs"][0],
    )
    with pytest.raises(TargetSeriesError, match="input ordering differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_order_rewrite),
            contract_path=fixture["contract_path"],
        )

    input_contract_rewrite = json.loads(manifest_content)
    matrixport_nav_input = next(
        row
        for row in input_contract_rewrite["inputs"]
        if row["input_id"] == "matrixport-nav"
    )
    matrixport_nav_input.update(
        {
            "artifact_id": "fabricated/nav.csv",
            "artifact_role": "UNCONTROLLED_ARTIFACT",
            "sha256": "0" * 64,
            "size_bytes": 1,
        }
    )
    with pytest.raises(
        TargetSeriesError, match="input contract differs: matrixport-nav"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_contract_rewrite),
            contract_path=fixture["contract_path"],
        )

    input_as_of_rewrite = json.loads(manifest_content)
    matrixport_nav_input = next(
        row
        for row in input_as_of_rewrite["inputs"]
        if row["input_id"] == "matrixport-nav"
    )
    matrixport_nav_input["as_of_date"] = "1999-12-31"
    with pytest.raises(
        TargetSeriesError, match="input source authority differs: matrixport-nav"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    product_as_of_rewrite = json.loads(manifest_content)
    product_input = next(
        row
        for row in product_as_of_rewrite["inputs"]
        if row["input_id"] == "financial-mvp-contract"
    )
    product_input["as_of_date"] = "1999-12-31"
    with pytest.raises(TargetSeriesError, match="product input provenance differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(product_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    reordered_artifacts = dict(artifacts)
    reordered_manifest = json.loads(manifest_content)
    reordered_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    reordered_segments[0], reordered_segments[1] = (
        reordered_segments[1],
        reordered_segments[0],
    )
    _replace_output(
        reordered_artifacts,
        reordered_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, reordered_segments),
    )
    with pytest.raises(TargetSeriesError, match="segment ordering differs"):
        validate_layered_history_generation(
            artifacts=reordered_artifacts,
            manifest_content=_reidentify_manifest(reordered_manifest),
            contract_path=fixture["contract_path"],
        )

    reordered_coverage_artifacts = dict(artifacts)
    reordered_coverage_manifest = json.loads(manifest_content)
    reordered_coverage = json.loads(artifacts["coverage.json"])
    reordered_coverage["strategies"][0], reordered_coverage["strategies"][1] = (
        reordered_coverage["strategies"][1],
        reordered_coverage["strategies"][0],
    )
    _replace_output(
        reordered_coverage_artifacts,
        reordered_coverage_manifest,
        "coverage.json",
        canonical_json_bytes(reordered_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=reordered_coverage_artifacts,
            manifest_content=_reidentify_manifest(reordered_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    nested_order_artifacts = dict(artifacts)
    nested_order_manifest = json.loads(manifest_content)
    nested_order_coverage = json.loads(artifacts["coverage.json"])
    robuxio_coverage = next(
        row
        for row in nested_order_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    robuxio_coverage["segments"][0], robuxio_coverage["segments"][1] = (
        robuxio_coverage["segments"][1],
        robuxio_coverage["segments"][0],
    )
    _replace_output(
        nested_order_artifacts,
        nested_order_manifest,
        "coverage.json",
        canonical_json_bytes(nested_order_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=nested_order_artifacts,
            manifest_content=_reidentify_manifest(nested_order_manifest),
            contract_path=fixture["contract_path"],
        )

    duplicate_summary_artifacts = dict(artifacts)
    duplicate_summary_manifest = json.loads(manifest_content)
    duplicate_summary_coverage = json.loads(artifacts["coverage.json"])
    robuxio_coverage = next(
        row
        for row in duplicate_summary_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    robuxio_coverage["segments"].insert(0, dict(robuxio_coverage["segments"][0]))
    _replace_output(
        duplicate_summary_artifacts,
        duplicate_summary_manifest,
        "coverage.json",
        canonical_json_bytes(duplicate_summary_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=duplicate_summary_artifacts,
            manifest_content=_reidentify_manifest(duplicate_summary_manifest),
            contract_path=fixture["contract_path"],
        )

    noncanonical_coverage_artifacts = dict(artifacts)
    noncanonical_coverage_manifest = json.loads(manifest_content)
    noncanonical_coverage = json.dumps(
        json.loads(artifacts["coverage.json"]), indent=2
    ).encode("utf-8")
    _replace_output(
        noncanonical_coverage_artifacts,
        noncanonical_coverage_manifest,
        "coverage.json",
        noncanonical_coverage,
    )
    with pytest.raises(TargetSeriesError, match="coverage is not canonical"):
        validate_layered_history_generation(
            artifacts=noncanonical_coverage_artifacts,
            manifest_content=_reidentify_manifest(noncanonical_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    extra_coverage_artifacts = dict(artifacts)
    extra_coverage_manifest = json.loads(manifest_content)
    extra_coverage = json.loads(artifacts["coverage.json"])
    extra_coverage["uncontrolled_policy"] = "ACCEPT"
    _replace_output(
        extra_coverage_artifacts,
        extra_coverage_manifest,
        "coverage.json",
        canonical_json_bytes(extra_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage keys differ"):
        validate_layered_history_generation(
            artifacts=extra_coverage_artifacts,
            manifest_content=_reidentify_manifest(extra_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    metadata_artifacts = dict(artifacts)
    metadata_manifest = json.loads(manifest_content)
    metadata_coverage = json.loads(artifacts["coverage.json"])
    metadata_coverage["builder_version"] = "9.9.9"
    _replace_output(
        metadata_artifacts,
        metadata_manifest,
        "coverage.json",
        canonical_json_bytes(metadata_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage metadata differs"):
        validate_layered_history_generation(
            artifacts=metadata_artifacts,
            manifest_content=_reidentify_manifest(metadata_manifest),
            contract_path=fixture["contract_path"],
        )

    nested_extra_artifacts = dict(artifacts)
    nested_extra_manifest = json.loads(manifest_content)
    nested_extra_coverage = json.loads(artifacts["coverage.json"])
    nested_extra_coverage["strategies"][0]["uncontrolled_policy"] = "ACCEPT"
    _replace_output(
        nested_extra_artifacts,
        nested_extra_manifest,
        "coverage.json",
        canonical_json_bytes(nested_extra_coverage),
    )
    with pytest.raises(TargetSeriesError, match="strategy coverage keys differ"):
        validate_layered_history_generation(
            artifacts=nested_extra_artifacts,
            manifest_content=_reidentify_manifest(nested_extra_manifest),
            contract_path=fixture["contract_path"],
        )

    mode_policy_artifacts = dict(artifacts)
    mode_policy_manifest = json.loads(manifest_content)
    mode_policy_coverage = json.loads(artifacts["coverage.json"])
    mode_policy_coverage["mode_policy"]["comparison_layers_in_modes"] = True
    _replace_output(
        mode_policy_artifacts,
        mode_policy_manifest,
        "coverage.json",
        canonical_json_bytes(mode_policy_coverage),
    )
    with pytest.raises(TargetSeriesError, match="mode policy differs"):
        validate_layered_history_generation(
            artifacts=mode_policy_artifacts,
            manifest_content=_reidentify_manifest(mode_policy_manifest),
            contract_path=fixture["contract_path"],
        )

    correction_set_artifacts = dict(artifacts)
    correction_set_manifest = json.loads(manifest_content)
    correction_set_coverage = json.loads(artifacts["coverage.json"])
    correction_set_coverage["correction_resolutions"]["uncontrolled"] = {}
    _replace_output(
        correction_set_artifacts,
        correction_set_manifest,
        "coverage.json",
        canonical_json_bytes(correction_set_coverage),
    )
    with pytest.raises(TargetSeriesError, match="correction resolutions are absent"):
        validate_layered_history_generation(
            artifacts=correction_set_artifacts,
            manifest_content=_reidentify_manifest(correction_set_manifest),
            contract_path=fixture["contract_path"],
        )

    p123_proof_artifacts = dict(artifacts)
    p123_proof_manifest = json.loads(manifest_content)
    p123_proof_coverage = json.loads(artifacts["coverage.json"])
    p123_proof = p123_proof_coverage["correction_resolutions"]["p123-eu-provider-live"]
    p123_proof["raw_level_observations"] = 8
    p123_proof["return_observations"] = 7
    p123_proof["equity_overlap_observations"] = 8
    _replace_output(
        p123_proof_artifacts,
        p123_proof_manifest,
        "coverage.json",
        canonical_json_bytes(p123_proof_coverage),
    )
    with pytest.raises(TargetSeriesError, match="return transform disposition differs"):
        validate_layered_history_generation(
            artifacts=p123_proof_artifacts,
            manifest_content=_reidentify_manifest(p123_proof_manifest),
            contract_path=fixture["contract_path"],
        )

    p123_ai_artifacts = dict(artifacts)
    p123_ai_manifest = json.loads(manifest_content)
    p123_ai_coverage = json.loads(artifacts["coverage.json"])
    p123_ai_proof = p123_ai_coverage["correction_resolutions"]["p123-ai-backtest"]
    p123_ai_proof["observed_start"] = "2025-12-31"
    p123_ai_proof["index_observations"] = 3
    p123_ai_proof["return_observations"] = 2
    _replace_output(
        p123_ai_artifacts,
        p123_ai_manifest,
        "coverage.json",
        canonical_json_bytes(p123_ai_coverage),
    )
    with pytest.raises(TargetSeriesError, match="AI backtest disposition differs"):
        validate_layered_history_generation(
            artifacts=p123_ai_artifacts,
            manifest_content=_reidentify_manifest(p123_ai_manifest),
            contract_path=fixture["contract_path"],
        )

    volatis_artifacts = dict(artifacts)
    volatis_manifest = json.loads(manifest_content)
    volatis_coverage = json.loads(artifacts["coverage.json"])
    volatis_proof = volatis_coverage["correction_resolutions"]["volatis-account-live"]
    volatis_proof.update(
        {
            "observed_end": "2024-10-23",
            "observed_start": "2024-10-17",
            "source_first_positive_value_date": "2024-10-16",
            "status": "OWNER_APPROVED_NOT_APPLICABLE",
        }
    )
    _replace_output(
        volatis_artifacts,
        volatis_manifest,
        "coverage.json",
        canonical_json_bytes(volatis_coverage),
    )
    with pytest.raises(
        TargetSeriesError, match="Volatis account-live resolution differs"
    ):
        validate_layered_history_generation(
            artifacts=volatis_artifacts,
            manifest_content=_reidentify_manifest(volatis_manifest),
            contract_path=fixture["contract_path"],
        )

    return_rewrite_artifacts = dict(artifacts)
    return_rewrite_manifest = json.loads(manifest_content)
    return_rewrite_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    rewritten_row = next(
        row
        for row in return_rewrite_segments
        if row["segment_id"] == "qsi:account-live"
    )
    rewritten_row["return"] = "0.123456789"
    _replace_output(
        return_rewrite_artifacts,
        return_rewrite_manifest,
        "segments.csv",
        _csv_bytes(
            target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
            return_rewrite_segments,
        ),
    )
    for name in ("returns.csv", "live-only.csv", "live-plus-backtest.csv"):
        projection = list(csv.DictReader(io.StringIO(artifacts[name].decode("utf-8"))))
        matching = next(
            row
            for row in projection
            if (
                row["series_id"],
                row["date"],
                row["evidence_layer"],
                row["strategy_version"],
            )
            == (
                rewritten_row["series_id"],
                rewritten_row["date"],
                rewritten_row["evidence_layer"],
                rewritten_row["strategy_version"],
            )
        )
        matching["return"] = rewritten_row["return"]
        _replace_output(
            return_rewrite_artifacts,
            return_rewrite_manifest,
            name,
            _csv_bytes(target_series.CORRELATION_FIELDS_V2, projection),
        )
    return_rewrite_coverage = json.loads(artifacts["coverage.json"])
    rewritten_summary = next(
        summary
        for strategy in return_rewrite_coverage["strategies"]
        if strategy["strategy_id"] == "qsi"
        for summary in strategy["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    rewritten_summary.update(
        target_series._row_identity(
            [
                row
                for row in return_rewrite_segments
                if row["segment_id"] == "qsi:account-live"
            ]
        )
    )
    _replace_output(
        return_rewrite_artifacts,
        return_rewrite_manifest,
        "coverage.json",
        canonical_json_bytes(return_rewrite_coverage),
    )
    with pytest.raises(TargetSeriesError, match="values differ from product authority"):
        validate_layered_history_generation(
            artifacts=return_rewrite_artifacts,
            manifest_content=_reidentify_manifest(return_rewrite_manifest),
            contract_path=fixture["contract_path"],
        )

    invalid_contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    invalid_contract["uncontrolled_policy"] = "ACCEPT"
    _write_json(fixture["contract_path"], invalid_contract)
    with pytest.raises(TargetSeriesError, match="contract is invalid"):
        _build(fixture)
    del invalid_contract["uncontrolled_policy"]
    _write_json(fixture["contract_path"], invalid_contract)

    semantic_bypass = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    p123_provider = next(
        row
        for row in semantic_bypass["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    p123_provider["readiness"] = "READY"
    p123_provider["blockers"] = []
    _write_json(fixture["contract_path"], semantic_bypass)
    with pytest.raises(TargetSeriesError, match="contract is invalid"):
        _build(fixture)
    _write_json(fixture["contract_path"], invalid_contract)

    reason_artifacts = dict(artifacts)
    reason_manifest = json.loads(manifest_content)
    reason_coverage = json.loads(reason_artifacts["coverage.json"])
    qsi_coverage = next(
        row for row in reason_coverage["strategies"] if row["strategy_id"] == "qsi"
    )
    qsi_coverage["modes"]["LIVE_PLUS_BACKTEST"]["reason"] = (
        "OWNER_APPROVED_NOT_APPLICABLE"
    )
    _replace_output(
        reason_artifacts,
        reason_manifest,
        "coverage.json",
        canonical_json_bytes(reason_coverage),
    )
    with pytest.raises(TargetSeriesError, match="mode reason differs"):
        validate_layered_history_generation(
            artifacts=reason_artifacts,
            manifest_content=_reidentify_manifest(reason_manifest),
            contract_path=fixture["contract_path"],
        )

    series_artifacts = dict(artifacts)
    series_manifest = json.loads(manifest_content)
    series_coverage = json.loads(series_artifacts["coverage.json"])
    composer_coverage = next(
        row
        for row in series_coverage["strategies"]
        if row["strategy_id"] == "composer-holy-grail"
    )
    composer_coverage["series_id"] = "SYNTHETIC_UNCONTROLLED_SERIES"
    _replace_output(
        series_artifacts,
        series_manifest,
        "coverage.json",
        canonical_json_bytes(series_coverage),
    )
    with pytest.raises(TargetSeriesError, match="series identity differs"):
        validate_layered_history_generation(
            artifacts=series_artifacts,
            manifest_content=_reidentify_manifest(series_manifest),
            contract_path=fixture["contract_path"],
        )

    relabelled_artifacts = dict(artifacts)
    relabelled_manifest = json.loads(manifest_content)
    relabelled_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    for row in relabelled_segments:
        if (
            row["strategy_id"] == "qsi"
            and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
        ):
            row.update(
                {
                    "calendar": "UNAPPROVED_CALENDAR",
                    "frequency": "UNAPPROVED_FREQUENCY",
                    "return_basis": "UNAPPROVED_BASIS",
                    "return_type": "UNAPPROVED_RETURN_TYPE",
                    "source_generation_id": "0" * 64,
                    "source_id": "unapproved-source",
                }
            )
    _replace_output(
        relabelled_artifacts,
        relabelled_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, relabelled_segments),
    )
    relabelled_coverage = json.loads(artifacts["coverage.json"])
    qsi_summary = next(
        summary
        for row in relabelled_coverage["strategies"]
        if row["strategy_id"] == "qsi"
        for summary in row["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    qsi_summary.update(
        {
            "calendar": "UNAPPROVED_CALENDAR",
            "frequency": "UNAPPROVED_FREQUENCY",
            "source_generation_id": "0" * 64,
            "source_id": "unapproved-source",
        }
    )
    _replace_output(
        relabelled_artifacts,
        relabelled_manifest,
        "coverage.json",
        canonical_json_bytes(relabelled_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment source receipt differs"):
        validate_layered_history_generation(
            artifacts=relabelled_artifacts,
            manifest_content=_reidentify_manifest(relabelled_manifest),
            contract_path=fixture["contract_path"],
        )

    gap_artifacts = dict(artifacts)
    gap_manifest = json.loads(manifest_content)
    gap_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    qsi_dates = [
        row["date"]
        for row in gap_segments
        if row["strategy_id"] == "qsi"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    removed_date = qsi_dates[len(qsi_dates) // 2]
    gap_segments = [
        row
        for row in gap_segments
        if not (row["strategy_id"] == "qsi" and row["date"] == removed_date)
    ]
    _replace_output(
        gap_artifacts,
        gap_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, gap_segments),
    )
    for name in ("returns.csv", "live-only.csv", "live-plus-backtest.csv"):
        rows = list(csv.DictReader(io.StringIO(artifacts[name].decode("utf-8"))))
        rows = [
            row
            for row in rows
            if not (row["series_id"] == "QSI" and row["date"] == removed_date)
        ]
        _replace_output(
            gap_artifacts,
            gap_manifest,
            name,
            _csv_bytes(target_series.CORRELATION_FIELDS_V2, rows),
        )
    gap_coverage = json.loads(artifacts["coverage.json"])
    qsi_summary = next(
        summary
        for row in gap_coverage["strategies"]
        if row["strategy_id"] == "qsi"
        for summary in row["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    selected_qsi = [
        row for row in gap_segments if row["segment_id"] == "qsi:account-live"
    ]
    qsi_summary.update(target_series._row_identity(selected_qsi))
    qsi_summary["gap_disposition"] = target_series._gap_disposition(selected_qsi)
    qsi_strategy = next(
        row for row in gap_coverage["strategies"] if row["strategy_id"] == "qsi"
    )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        qsi_strategy["modes"][mode] = {
            "reason": "REQUIRED_ACCOUNT_LIVE_INTERIOR_GAP",
            "status": "REQUIRED_NOT_READY",
        }
    live_not_ready = set(
        gap_manifest["mode_status"]["LIVE_ONLY"]["required_not_ready_strategy_ids"]
    )
    live_not_ready.add("qsi")
    gap_manifest["mode_status"]["LIVE_ONLY"] = {
        "required_not_ready_strategy_ids": sorted(live_not_ready),
        "status": "REQUIRED_NOT_READY",
    }
    _replace_output(
        gap_artifacts,
        gap_manifest,
        "coverage.json",
        canonical_json_bytes(gap_coverage),
    )
    with pytest.raises(TargetSeriesError, match="product authority"):
        validate_layered_history_generation(
            artifacts=gap_artifacts,
            manifest_content=_reidentify_manifest(gap_manifest),
            contract_path=fixture["contract_path"],
        )


def test_layered_history_rejects_conflicting_p123_performance_capture(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    performance_path = fixture["portfolio123_performance_path"]
    rows = pq.read_table(performance_path).to_pylist()
    conflicting = dict(next(row for row in rows if row["field"] == "data"))
    conflicting["value"] = "[]"
    pq.write_table(pa.Table.from_pylist([*rows, conflicting]), performance_path)

    manifest_path = fixture["portfolio123_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "tables/har_managed_performance.parquet"
    )
    artifact.update(_artifact(performance_path, artifact["artifact_id"]))
    _write_json(manifest_path, manifest)
    _pointer(
        fixture["portfolio123_pointer_path"],
        manifest["generation_id"],
        manifest_path,
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
        "PORTFOLIO123",
    )
    _append_p123_performance_receipt(contract, manifest_path, performance_path)
    _append_p123_ai_receipts(
        contract,
        manifest_path,
        fixture["portfolio123_returns_path"],
        fixture["portfolio123_relationships_path"],
    )
    _write_json(fixture["contract_path"], contract)

    with pytest.raises(TargetSeriesError, match="conflicting captures"):
        _build(fixture)


def test_layered_history_rejects_relabel_and_admitted_overlap(tmp_path: Path) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    provider = next(
        row
        for row in segments
        if row["strategy_id"] == "robuxio-crypto"
        and row["evidence_layer"] == "PROVIDER_LIVE"
    )
    provider["evidence_layer"] = "ACCOUNT_LIVE"
    provider["mode_admission"] = "INCLUDE_SELECTED_LIVE"
    relabelled = dict(artifacts)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(segments)
    relabelled["segments.csv"] = buffer.getvalue().encode("utf-8")
    manifest = json.loads(manifest_content)
    manifest["outputs"]["segments.csv"]["sha256"] = hashlib.sha256(
        relabelled["segments.csv"]
    ).hexdigest()
    manifest["outputs"]["segments.csv"]["size_bytes"] = len(relabelled["segments.csv"])
    with pytest.raises(TargetSeriesError):
        validate_layered_history_generation(
            artifacts=relabelled,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )

    overlapping = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    p123_live = next(
        row
        for row in overlapping
        if row["strategy_id"] == "p123-ai-factor"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    )
    admitted_overlap = dict(p123_live)
    admitted_overlap.update(
        {
            "evidence_layer": "BACKTEST",
            "mode_admission": "INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
            "return_basis": "MODEL",
            "segment_id": "p123-ai-factor:backtest",
            "source_generation_id": "4" * 64,
            "source_id": "strategy-backtests",
        }
    )
    overlapping.append(admitted_overlap)
    overlapping.sort(
        key=lambda row: (
            row["strategy_id"],
            row["date"],
            row["evidence_layer"],
            row["segment_id"],
        )
    )
    overlap_artifacts = dict(artifacts)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(overlapping)
    overlap_content = buffer.getvalue().encode("utf-8")
    # Even a coherent local rehash cannot make overlapping backtest/live rows admissible.
    manifest = json.loads(manifest_content)
    _replace_output(overlap_artifacts, manifest, "segments.csv", overlap_content)
    with pytest.raises(TargetSeriesError):
        validate_layered_history_generation(
            artifacts=overlap_artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_layered_history_cli_builds_verifies_and_requires_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _history_fixture(tmp_path)
    output_root = tmp_path / "cli-candidate"
    path_flags = {
        "--financial-mvp-contract": "contract_path",
        "--provider-ledger-current": "provider_ledger_pointer_path",
        "--provider-ledger-manifest": "provider_ledger_manifest_path",
        "--provider-ledger-catalog": "provider_ledger_catalog_path",
        "--matrixport-current": "matrixport_pointer_path",
        "--manifest": "matrixport_manifest_path",
        "--nav": "matrixport_nav_path",
        "--holdings": "matrixport_holdings_path",
        "--subscriptions": "matrixport_subscription_path",
        "--quantbase-current": "quantbase_pointer_path",
        "--quantbase-manifest": "quantbase_manifest_path",
        "--quantbase-history": "quantbase_history_path",
        "--quantbase-activity": "quantbase_activity_path",
        "--portfolio123-current": "portfolio123_pointer_path",
        "--portfolio123-manifest": "portfolio123_manifest_path",
        "--portfolio123-metadata": "portfolio123_metadata_path",
        "--portfolio123-performance": "portfolio123_performance_path",
        "--portfolio123-equity": "portfolio123_equity_path",
        "--portfolio123-returns": "portfolio123_returns_path",
        "--portfolio123-relationships": "portfolio123_relationships_path",
    }
    argv = [
        "portfolio_target_series.py",
        "--layered-history-generation",
        "--repository-sha",
        target_series._trusted_layered_history_code_identity()[0],
        "--as-of-date",
        fixture["as_of_date"],
        "--product-id",
        fixture["matrixport_product_id"],
        "--quantbase-fund-id",
        fixture["quantbase_fund_id"],
        "--output-root",
        str(output_root),
    ]
    for flag, key in path_flags.items():
        argv.extend((flag, str(fixture[key])))
    monkeypatch.setattr(sys, "argv", argv)
    assert target_series.main() == 0
    build_result = json.loads(capsys.readouterr().out)
    generation_root = output_root / build_result["generation_id"]
    assert generation_root.is_dir()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
        ],
    )
    assert target_series.main() == 0
    verify_result = json.loads(capsys.readouterr().out)
    assert verify_result["verification"] == "VALID"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--assurance-profile",
            "DEVELOPMENT",
            "--evaluated-on",
            "2026-01-14",
        ],
    )
    assert target_series.main() == 0
    assurance_result = json.loads(capsys.readouterr().out)
    assert assurance_result["assurance"] == {
        "evaluated_on": "2026-01-14",
        "profile": "DEVELOPMENT",
        "profile_policy": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "receipt_age_days": 4,
        "receipt_as_of": "2026-01-10",
        "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
    }

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--assurance-profile",
            "RELEASE_QC",
            "--evaluated-on",
            "2026-01-11",
        ],
    )
    assert target_series.main() == 2
    assert (
        "release QC requires a layered history receipt for the evaluation date"
        in capsys.readouterr().err
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--require-mode",
            "LIVE_PLUS_BACKTEST",
        ],
    )
    assert target_series.main() == 2
    assert "required history mode is not ready" in capsys.readouterr().err

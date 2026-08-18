from __future__ import annotations

import csv
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
    _write_json(
        path,
        {
            "artifact_inventory_sha256": "f" * 64,
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
    matrixport_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    matrixport_source["declared_gap_policy"] = _matrixport_gap_policy("2025-12-09")
    _append_p123_performance_receipt(contract, p123_manifest, performance_path)
    contract_path = tmp_path / "contract.json"
    _write_json(contract_path, contract)

    return {
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


def _build(fixture: dict[str, Any]) -> tuple[dict[str, bytes], bytes]:
    return build_layered_history_generation(
        repository_sha="a" * 40,
        **fixture,
    )


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


def test_layered_history_generation_is_deterministic_and_fail_closed(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    repeated_artifacts, repeated_manifest = _build(fixture)
    assert artifacts == repeated_artifacts
    assert manifest_content == repeated_manifest
    assert json.loads(manifest_content)["builder_version"] == "1.3.0"
    generation_schema = json.loads(
        (
            ROOT / "schemas" / "portfolio" / "layered-history-generation.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert generation_schema["properties"]["builder_version"] == {"const": "1.3.0"}

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
    _write_json(fixture["contract_path"], contract)

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

    artifacts, manifest_content = _build(fixture)
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


def test_layered_history_rejects_coherent_receipt_and_semantic_rewrites(
    tmp_path: Path,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)

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
    with pytest.raises(TargetSeriesError, match="segment authority differs"):
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
    with pytest.raises(TargetSeriesError, match="unexplained live gap"):
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
        "a" * 40,
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
            "--require-mode",
            "LIVE_PLUS_BACKTEST",
        ],
    )
    assert target_series.main() == 2
    assert "required history mode is not ready" in capsys.readouterr().err

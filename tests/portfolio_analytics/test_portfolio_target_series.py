from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_target_series.py"
SPEC = importlib.util.spec_from_file_location("portfolio_target_series", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio target-series builder")
target_series = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = target_series
SPEC.loader.exec_module(target_series)
TargetSeriesError = target_series.TargetSeriesError
build_matrixport_targets = target_series.build_matrixport_targets
build_decomposition_inputs = target_series.build_decomposition_inputs
build_quantbase_targets = target_series.build_quantbase_targets
build_portfolio123_evidence = target_series.build_portfolio123_evidence
build_strategy_evidence = target_series.build_strategy_evidence


def _write(path: Path, content: str) -> tuple[str, int]:
    data = content.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(data)


def _sources(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    nav = tmp_path / "nav.csv"
    nav_hash, nav_size = _write(
        nav,
        "product_id,timestamp_utc,nav\n"
        "P1,2026-01-01T00:00:00+00:00,1\n"
        "P1,2026-01-02T00:00:00+00:00,1.1\n"
        "P1,2026-01-03T00:00:00+00:00,0.99\n"
        "OTHER,2026-01-03T00:00:00+00:00,8\n",
    )
    holdings = tmp_path / "holdings.csv"
    holding_hash, holding_size = _write(holdings, "product_id,shares\nP1,100\n")
    manifest = tmp_path / "generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "a" * 64,
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "csv/nav_daily.csv",
                        "sha256": nav_hash,
                        "size_bytes": nav_size,
                    },
                    {
                        "artifact_id": "csv/holdings.csv",
                        "sha256": holding_hash,
                        "size_bytes": holding_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return base, manifest, nav, holdings


def test_matrixport_target_build_is_deterministic_and_account_live(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path)
    first, proof_bytes = build_matrixport_targets(*sources, "P1", "Robuxio")
    second, second_proof = build_matrixport_targets(*sources, "P1", "Robuxio")
    assert first == second
    assert proof_bytes == second_proof
    rows = list(csv.DictReader(io.StringIO(first.decode("utf-8"))))
    robuxio = [row for row in rows if row["series_id"] == "Robuxio"]
    assert [row["return"] for row in robuxio] == ["0.1", "-0.1"]
    assert {row["evidence_layer"] for row in robuxio} == {"ACCOUNT_LIVE"}
    proof = json.loads(proof_bytes)
    assert proof["coverage"]["nav_observations"] == 3
    assert proof["coverage"]["return_observations"] == 2


def test_matrixport_target_rejects_manifest_drift(tmp_path: Path) -> None:
    base, manifest, nav, holdings = _sources(tmp_path)
    nav.write_text(nav.read_text(encoding="utf-8") + "P1,2026-01-04T00:00:00Z,1\n")
    with pytest.raises(TargetSeriesError, match="differs from the manifest"):
        build_matrixport_targets(base, manifest, nav, holdings, "P1", "Robuxio")


def test_matrixport_target_requires_matching_live_holding(tmp_path: Path) -> None:
    base, manifest, nav, holdings = _sources(tmp_path)
    with pytest.raises(TargetSeriesError, match="exactly one live holding"):
        build_matrixport_targets(base, manifest, nav, holdings, "UNKNOWN", "Robuxio")


def test_decomposition_inputs_rebind_exact_sources(tmp_path: Path) -> None:
    base, manifest, nav, holdings = _sources(tmp_path)
    targets, proof = build_matrixport_targets(
        base, manifest, nav, holdings, "P1", "Robuxio"
    )
    factors = tmp_path / "factors.csv"
    factor_hash, factor_size = _write(factors, "date,RF,F1\n2026-01-01,0,0\n")
    base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-01",
                "factor_source": {
                    "expected_sha256": factor_hash,
                    "expected_size_bytes": factor_size,
                },
                "target_source": {
                    "expected_sha256": base_hash,
                    "expected_size_bytes": base.stat().st_size,
                },
            }
        ),
        encoding="utf-8",
    )
    rebound, copied_factors, copied_manifest = build_decomposition_inputs(
        spec, factors, base, manifest, targets, proof
    )
    value = json.loads(rebound)
    assert value["as_of_date"] == "2026-01-03"
    assert (
        value["target_source"]["expected_sha256"] == hashlib.sha256(targets).hexdigest()
    )
    assert value["target_source"]["schema_version"] == "2"
    assert len(value["provenance_sources"]) == 2
    assert copied_factors == factors.read_bytes()
    assert copied_manifest == manifest.read_bytes()


def test_quantbase_account_path_is_flow_adjusted_and_deterministic(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    history = tmp_path / "history.csv"
    history_hash, history_size = _write(
        history,
        "time,value,cost_basis,profit_loss\n"
        "01/01/2026,0,0,0\n"
        "01/02/2026,100,100,0\n"
        "01/03/2026,110,100,10\n"
        "01/04/2026,220,200,20\n",
    )
    activity = tmp_path / "activity.csv"
    activity_hash, activity_size = _write(
        activity,
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,100,107,false,2026-01-01T12:00:00Z,BUY\n"
        "filled_txn,100,107,false,2026-01-03T12:00:00Z,BUY\n",
    )
    manifest = tmp_path / "quantbase-generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "b" * 64,
                "as_of_date": "2026-01-04",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
                        "sha256": history_hash,
                        "size_bytes": history_size,
                    },
                    {
                        "artifact_id": "source/csv/account_activity.csv",
                        "sha256": activity_hash,
                        "size_bytes": activity_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    targets, proof_bytes = build_quantbase_targets(
        base, manifest, history, activity, "107", "Volatis Strategy"
    )
    again, again_proof = build_quantbase_targets(
        base, manifest, history, activity, "107", "Volatis Strategy"
    )

    assert targets == again
    assert proof_bytes == again_proof
    rows = [
        row
        for row in csv.DictReader(io.StringIO(targets.decode("utf-8")))
        if row["series_id"] == "Volatis Strategy"
    ]
    assert [row["date"] for row in rows] == ["2026-01-03", "2026-01-04"]
    assert [row["return"] for row in rows] == ["0.1", "0.090909090909090909090909091"]
    proof = json.loads(proof_bytes)
    assert proof["coverage"]["flow_events"] == 2
    assert proof["coverage"]["return_observations"] == 2
    assert proof["evidence_layer"] == "ACCOUNT_LIVE"


def test_quantbase_account_path_rejects_manifest_drift(tmp_path: Path) -> None:
    base = tmp_path / "base.csv"
    _write(
        base,
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "Existing,2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n",
    )
    history = tmp_path / "history.csv"
    history_hash, history_size = _write(
        history,
        "time,value,cost_basis,profit_loss\n"
        "01/01/2026,100,100,0\n"
        "01/02/2026,101,100,1\n",
    )
    activity = tmp_path / "activity.csv"
    activity_hash, activity_size = _write(
        activity,
        "activity_type,amount,fund,is_rebalance,time,type\n"
        "filled_txn,100,107,false,2026-01-01T12:00:00Z,BUY\n",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "b" * 64,
                "as_of_date": "2026-01-02",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
                        "sha256": history_hash,
                        "size_bytes": history_size,
                    },
                    {
                        "artifact_id": "source/csv/account_activity.csv",
                        "sha256": activity_hash,
                        "size_bytes": activity_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    history.write_text(history.read_text(encoding="utf-8") + "01/03/2026,102,100,2\n")
    with pytest.raises(TargetSeriesError, match="differs from the manifest"):
        build_quantbase_targets(
            base, manifest, history, activity, "107", "Volatis Strategy"
        )


def test_portfolio123_provider_and_account_paths_remain_separate(
    tmp_path: Path,
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    targets = (
        "series_id,date,return,evidence_layer,return_basis,return_type,cost\n"
        "AI Factor [P123],2026-01-02,0.01,ACCOUNT_LIVE,NET,TOTAL,\n"
        "AI Factor [P123],2026-01-03,-0.02,ACCOUNT_LIVE,NET,TOTAL,\n"
    ).encode("utf-8")
    equity = tmp_path / "equity.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-01",
                    "Total Equity": 100.0,
                    "Cash Added": 0.0,
                },
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-02",
                    "Total Equity": 102.0,
                    "Cash Added": 0.0,
                },
                {
                    "_entity_id": "provider-synthetic",
                    "Date": "2026-01-03",
                    "Total Equity": 101.0,
                    "Cash Added": 0.0,
                },
            ]
        ),
        equity,
    )
    metadata = tmp_path / "metadata.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "id": "account-synthetic",
                    "lastFix": 1767484800000,
                    "netWorth": 48562.85,
                    "netWorthPrevWithCashAdded": 48562.85,
                    "isLive": True,
                }
            ]
        ),
        metadata,
    )
    equity_hash = hashlib.sha256(equity.read_bytes()).hexdigest()
    metadata_hash = hashlib.sha256(metadata.read_bytes()).hexdigest()
    manifest = tmp_path / "portfolio123-generation.json"
    manifest.write_text(
        json.dumps(
            {
                "generation_id": "c" * 64,
                "as_of_date": "2026-01-05",
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "tables/har_equity_curve.parquet",
                        "sha256": equity_hash,
                        "size_bytes": equity.stat().st_size,
                    },
                    {
                        "artifact_id": "tables/har_account_metadata.parquet",
                        "sha256": metadata_hash,
                        "size_bytes": metadata.stat().st_size,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    evidence_bytes = build_portfolio123_evidence(
        targets,
        manifest,
        equity,
        metadata,
        "provider-synthetic",
        "account-synthetic",
        "p123-ai-factor",
        "AI Factor [P123]",
    )
    evidence = json.loads(evidence_bytes)

    assert evidence["account"]["observed_end"] == "2026-01-03"
    assert evidence["provider"]["observed_end"] == "2026-01-03"
    assert evidence["provider"]["return_observations"] == 2
    assert evidence["overlap"]["observations"] == 2
    assert evidence["evidence_layer"] == ("PROVIDER_LIVE_RECONCILED_TO_ACCOUNT_LIVE")
    combined = json.loads(
        build_strategy_evidence(
            json.dumps(
                {
                    "coverage": {
                        "observed_end": "2026-01-04",
                        "observed_start": "2026-01-03",
                        "return_observations": 1,
                    },
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": "d" * 64,
                    "generation_as_of_date": "2026-01-04",
                    "provider_id": "quantbase",
                    "series_id": "Volatis Strategy",
                    "sources": {},
                    "strategy_id": "volatis-strategy",
                }
            ).encode("utf-8"),
            evidence_bytes,
        )
    )
    assert combined["as_of_date"] == "2026-01-05"
    assert [row["strategy_id"] for row in combined["strategies"]] == [
        "volatis-strategy",
        "p123-ai-factor",
    ]

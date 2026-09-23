from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.turingtrader import source_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reconciliation = _load(
    "tt_manifest_reconciliation",
    TOOLS_ROOT / "portfolio_implementation_reconciliation.py",
)
attribution = _load(
    "tt_manifest_attribution",
    TOOLS_ROOT / "portfolio_implementation_return_attribution.py",
)


COMPONENTS = (
    "tt-hokey-pokey",
    "tt-mach-3-v3",
    "tt-mean-kitty-v2",
    "tt-quick-change-v2",
    "tt-vix-spritz-aggro-v3",
    "tt-hot-sauce",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def _capture_fixture(root: Path) -> None:
    portfolio_comp = {
        "slug": list(COMPONENTS),
        "portfolios": [f"Synthetic {slug}" for slug in COMPONENTS],
        "nav-end": [1100.0 + index for index in range(len(COMPONENTS))],
        "date-end": ["09/01/2026"] * len(COMPONENTS),
        "updated": ["2026-09-02T01:00Z"] * len(COMPONENTS),
    }
    _write_json(root / "portfolio-comp.json", portfolio_comp)
    objects: list[dict[str, object]] = [
        {
            "object": "portfolio-comp.json",
            "bytes": (root / "portfolio-comp.json").stat().st_size,
            "mode": "global",
        }
    ]
    for index, slug in enumerate(COMPONENTS):
        files = {
            "performance-chart.json": {
                "labels": ["Date", f"Synthetic {slug}", "Benchmark"],
                "data": [
                    ["08/31/2026", 1000.0, 1000.0],
                    ["09/01/2026", 1100.0 + index, 1001.0],
                ],
            },
            "performance-table.json": [
                ["Metric", "", f"Synthetic {slug}", "Benchmark"],
                ["Simulation End", "09/01/2026", f"${1100 + index:,.2f}", "$1,001.00"],
            ],
            "holdings-history.json": [
                ["Date", "Allocation"],
                ["8/31/2026 4:00:00 PM", "SPY=60.00%, BIL=40.00%"],
                ["9/1/2026 4:00:00 PM", "SPY=50.00%, BIL=50.00%"],
            ],
            "portfolio-holdings.json": [
                ["Symbol", "Name", "Allocation", "Price"],
                ["SPY", "Synthetic SPY", "50.00%", "$100.00"],
                ["BIL", "Synthetic BIL", "50.00%", "$91.00"],
            ],
        }
        for object_name, value in files.items():
            path = root / f"{slug}__{object_name}"
            _write_json(path, value)
            objects.append(
                {"slug": slug, "object": object_name, "bytes": path.stat().st_size}
            )
        (root / f"{slug}__last-update.txt").write_text(
            "2026-09-03T02:57Z", encoding="utf-8"
        )
        (root / f"{slug}__last-rebalance.txt").write_text(
            "September 1, 2026", encoding="utf-8"
        )
    _write_json(
        root / "tt-market-vane__status.json",
        [
            ["Key", "Value"],
            ["MarketVane", "1"],
            ["StockMom", "1.5"],
            ["EcoMom", "2.5"],
            ["StockPref", "3.5"],
            ["VixMargin", "4.5"],
            ["DdMargin", "5.5"],
        ],
    )
    _write_json(
        root / "capture-manifest.json",
        {"captured_at": "2026-09-03T02:39:48Z", "objects": objects},
    )


def _build_manifest(capture_root: Path, output_root: Path) -> bytes:
    price_tape = output_root / "tt-adjusted.json"
    _write_json(
        price_tape,
        {
            "provider": "yahoo",
            "dataset": "history_adjusted",
            "rows": [
                {
                    "Ticker": ticker,
                    "Date": observed,
                    "Close": 100.0,
                    "Adj Close": 100.0,
                }
                for ticker in ("BIL", "SPY")
                for observed in ("2026-08-28T00:00:00", "2026-09-01T00:00:00")
            ],
        },
    )
    tape_bytes = price_tape.read_bytes()
    price_manifest = output_root / "tt-adjusted.json.manifest.json"
    _write_json(
        price_manifest,
        {
            "artifact": {
                "name": price_tape.name,
                "sha256": hashlib.sha256(tape_bytes).hexdigest(),
                "size_bytes": len(tape_bytes),
            },
            "coverage": {
                "claim": "PARTIAL",
                "missing_tickers": [],
                "observed_tickers": ["BIL", "SPY"],
            },
            "request": {
                "dataset": "history_adjusted",
                "end": "2026-09-02",
                "provider": "yahoo",
                "start": "2026-08-28",
                "tickers": ["BIL", "SPY"],
                "transport": "yfinance",
            },
            "schema_version": 1,
            "source_run_id": "synthetic-source-run",
        },
    )
    return source_manifest.build_source_manifest(
        capture_root,
        portfolio_id="tt-portfolio",
        portfolio_version="six-component-v1",
        live_inception="2026-08-31",
        selected_components=COMPONENTS,
        selected_component_weights={slug: 1.0 / 6.0 for slug in COMPONENTS},
        price_tape=price_tape,
        price_tape_manifest=price_manifest,
        target_price_symbols=("BIL", "SPY"),
        target_start="2026-08-31",
        target_end="2026-09-01",
    )


def test_actual_capture_producer_builds_and_replays_shared_manifest(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest_path = tmp_path / "tt-source-manifest.json"

    content = _build_manifest(capture_root, tmp_path)
    source_manifest.write_create_only(manifest_path, content)
    manifest = source_manifest.verify_source_manifest(manifest_path, capture_root)

    assert manifest["portfolio"] == {
        "live_inception": "2026-08-31",
        "portfolio_id": "tt-portfolio",
        "portfolio_version": "six-component-v1",
    }
    assert [row["slug"] for row in manifest["selected_components"]] == list(COMPONENTS)
    first = manifest["selected_components"][0]
    assert first["nav"] == {"as_of_date": "2026-09-01", "value": 1100.0}
    assert {
        key: first["dated_holdings"][key]
        for key in ("observed_end", "observed_start", "rows")
    } == {
        "observed_end": "2026-09-01",
        "observed_start": "2026-08-31",
        "rows": 2,
    }
    assert first["current_holdings"] == [
        {"symbol": "BIL", "weight": 0.5},
        {"symbol": "SPY", "weight": 0.5},
    ]
    assert first["portfolio_weight"] == pytest.approx(1.0 / 6.0)
    assert first["last_update"] == "2026-09-03T02:57:00Z"
    assert first["last_rebalance"] == "2026-09-01"
    assert manifest["market_vane"]["class_ids"] == list(range(8))
    assert manifest["market_vane"]["current_class_id"] == 1
    assert manifest["price_tape"]["dataset"] == "history_adjusted"
    assert manifest["price_tape"]["target"] == {
        "observed_end": "2026-09-01",
        "observed_start": "2026-08-31",
        "symbols": ["BIL", "SPY"],
    }
    assert manifest["versioned_history"] == {
        "component_nav_earliest_observed": "2026-08-31",
        "live_target_weights_complete_from": "2026-08-31",
        "live_target_weights_uncovered_interval": None,
        "live_plus_backtest": {
            "compatibility_basis": (
                "CURRENT_SIX_COMPONENT_SLUGS_AND_PUBLISHED_EQUAL_WEIGHTS"
            ),
            "interpretation": (
                "CURRENT_SPECIFICATION_SIMULATION_NOT_HISTORICAL_COMPOSITION"
            ),
            "method": "DAILY_FIXED_WEIGHT_COMPONENT_RETURN_SUM",
            "observed_end": "2026-09-01",
            "observed_start": "2026-09-01",
            "status": "AVAILABLE_CURRENT_SPECIFICATION_BACKTEST",
            "strategy_version": (
                "0b53f1f09a348b7c678a57f888074ef32056d91f1300f38b4f81686b5d188210"
            ),
        },
    }
    assert set(first["endpoint_hashes"]) == {
        "holdings-history.json",
        "last-rebalance.txt",
        "last-update.txt",
        "performance-chart.json",
        "performance-table.json",
        "portfolio-holdings.json",
    }


def test_replay_rejects_changed_endpoint_bytes(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest_path = tmp_path / "tt-source-manifest.json"
    source_manifest.write_create_only(
        manifest_path,
        _build_manifest(capture_root, tmp_path),
    )
    changed = capture_root / "tt-hokey-pokey__holdings-history.json"
    changed.write_bytes(changed.read_bytes() + b"\n")

    with pytest.raises(
        source_manifest.SourceManifestError,
        match="retained capture size differs",
    ):
        source_manifest.verify_source_manifest(manifest_path, capture_root)


@pytest.mark.parametrize("consumer", ["reconciliation", "attribution"])
def test_both_tt_consumers_reject_a_manifest_for_different_components(
    tmp_path: Path,
    consumer: str,
) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest_path = tmp_path / "tt-source-manifest.json"
    source_manifest.write_create_only(
        manifest_path,
        _build_manifest(capture_root, tmp_path),
    )
    selected = list(COMPONENTS)
    selected[-1] = "tt-different-component"

    error = (
        reconciliation.ImplementationReconciliationError
        if consumer == "reconciliation"
        else attribution.ReturnAttributionError
    )
    loader = (
        reconciliation._read_tt_source_manifest
        if consumer == "reconciliation"
        else attribution._read_tt_source_manifest
    )
    with pytest.raises(error, match="selected components differ"):
        loader(manifest_path, selected)


@pytest.mark.parametrize("consumer", ["reconciliation", "attribution"])
def test_both_tt_consumers_accept_the_same_governed_manifest(
    tmp_path: Path,
    consumer: str,
) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest_path = tmp_path / "tt-source-manifest.json"
    source_manifest.write_create_only(
        manifest_path,
        _build_manifest(capture_root, tmp_path),
    )
    loader = (
        reconciliation._read_tt_source_manifest
        if consumer == "reconciliation"
        else attribution._read_tt_source_manifest
    )

    loaded = loader(manifest_path, COMPONENTS)

    assert loaded["source_manifest_id"]
    assert loaded["price_tape"]["dataset"] == "history_adjusted"


def test_manifest_identity_rejects_tampering(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest_path = tmp_path / "tt-source-manifest.json"
    content = _build_manifest(capture_root, tmp_path)
    source_manifest.write_create_only(manifest_path, content)
    manifest = json.loads(content)
    manifest["selected_components"][0]["nav"]["value"] = 999.0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    expected = hashlib.sha256(
        source_manifest.canonical_json_bytes(
            {
                key: value
                for key, value in manifest.items()
                if key != "source_manifest_id"
            }
        )
    ).hexdigest()
    assert expected != manifest["source_manifest_id"]
    with pytest.raises(
        reconciliation.ImplementationReconciliationError,
        match="identity differs",
    ):
        reconciliation._read_tt_source_manifest(manifest_path, COMPONENTS)


def test_attribution_reads_the_governed_adjusted_price_shape(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    _build_manifest(capture_root, tmp_path)

    rows = attribution._market_rows(
        tmp_path / "tt-adjusted.json",
        accepted_as_of=attribution.date(2026, 9, 1),
    )

    assert set(rows) == {"BIL", "SPY"}
    assert rows["SPY"][attribution.date(2026, 9, 1)]["Close"] == 100.0


def test_both_consumers_use_normalized_direct_source_weights(tmp_path: Path) -> None:
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    _capture_fixture(capture_root)
    manifest = json.loads(_build_manifest(capture_root, tmp_path))

    snapshots = attribution._tt_target_snapshots(
        manifest,
        COMPONENTS,
        accepted_as_of=attribution.date(2026, 9, 1),
    )
    events, latest = reconciliation._tt_allocation_events(
        manifest,
        COMPONENTS,
        accepted_as_of=reconciliation.date(2026, 9, 1),
    )

    assert snapshots[attribution.date(2026, 8, 31)] == {
        "BIL": pytest.approx(0.4),
        "SPY": pytest.approx(0.6),
    }
    assert snapshots[attribution.date(2026, 9, 1)] == {
        "BIL": pytest.approx(0.5),
        "SPY": pytest.approx(0.5),
    }
    assert events
    assert min(event.observed for event in events) == reconciliation.date(2026, 9, 1)
    assert set(latest) == set(COMPONENTS)

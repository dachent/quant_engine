"""Offline tests for the single canonical Yahoo market-data adapter."""

from __future__ import annotations

from argparse import Namespace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / "skills"
    / "agent-market-data-terminal"
    / "scripts"
    / "fetch_yfinance.py"
)
SPEC = importlib.util.spec_from_file_location("quant_engine_yahoo_adapter", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load canonical Yahoo adapter")
yahoo = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = yahoo
SPEC.loader.exec_module(yahoo)


def _args(dataset: str = "history_adjusted") -> Namespace:
    return Namespace(
        dataset=dataset,
        tickers=["AAPL", "MSFT", "AAPL"],
        start="2020-01-01",
        end="2020-01-03",
        interval="1d",
        prepost=False,
        batch_size=2,
        threads=1,
        timeout_seconds=30,
        retry_attempts=1,
        retry_backoff_seconds=0.0,
        resolve_names=False,
        search_count=5,
        cache_dir=None,
        format="parsed",
        output="outputs/example.json",
    )


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Ticker": "AAPL",
                "Date": pd.Timestamp("2020-01-02"),
                "Open": 100.0,
                "High": 110.0,
                "Low": 95.0,
                "Close": 105.0,
                "Adj Close": 84.0,
                "Volume": 1000,
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            },
            {
                "Ticker": "MSFT",
                "Date": pd.Timestamp("2020-01-02"),
                "Open": 50.0,
                "High": 55.0,
                "Low": 49.0,
                "Close": 50.0,
                "Adj Close": 50.0,
                "Volume": 500,
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            },
        ]
    )


def test_dataset_contract_and_argument_validation() -> None:
    assert {
        "history",
        "history_yf",
        "history_raw",
        "history_adjusted",
        "history_summary",
        "ticker_search",
        "current_snapshot",
    } == set(yahoo.DATASET_CHOICES)
    assert yahoo.validate_args(_args()) == ["AAPL", "MSFT"]

    invalid = _args()
    invalid.start = None
    with pytest.raises(ValueError, match="start"):
        yahoo.validate_args(invalid)


def test_workbook_lineage_adjustment_and_summary() -> None:
    history_yf = yahoo.build_history_yf_frame(
        _history(), {"AAPL": "Apple Inc.", "MSFT": "Microsoft"}
    )
    raw = yahoo.build_history_raw_frame(history_yf)
    adjusted = yahoo.build_history_adjusted_frame(raw)
    summary = yahoo.build_history_summary_frame(raw)

    aapl = adjusted.loc[adjusted["Ticker"] == "AAPL"].iloc[0]
    assert aapl["Ticker.Name"] == "Apple Inc."
    assert aapl["Open"] == pytest.approx(80.0)
    assert aapl["High"] == pytest.approx(88.0)
    assert aapl["Low"] == pytest.approx(76.0)
    assert aapl["Close"] == pytest.approx(84.0)
    assert set(summary["Observations"]) == {1}
    assert list(summary["Ticker"]) == ["AAPL", "MSFT"]


def test_search_rows_are_deterministic_and_nested_values_are_serialized() -> None:
    frame = yahoo.build_ticker_search_frame(
        {
            "MSFT": [{"symbol": "MSFT", "nested": {"rank": 2}}],
            "AAPL": [{"symbol": "AAPL", "nested": {"rank": 1}}],
        }
    )
    assert list(frame["RequestedTicker"]) == ["AAPL", "MSFT"]
    assert frame.loc[0, "nested"] == '{"rank": 1}'


def test_manifest_is_hash_bound_and_deterministic(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text('{"synthetic":true}\n', encoding="utf-8")
    request = yahoo.build_request_payload(_args(), ["AAPL", "MSFT"])
    coverage = yahoo.build_coverage(_history(), ["AAPL", "MSFT", "MISSING"])

    first_path = yahoo.write_run_manifest(artifact_path, request, coverage)
    first = first_path.read_bytes()
    second_path = yahoo.write_run_manifest(artifact_path, request, coverage)

    assert first == second_path.read_bytes()
    manifest = json.loads(first)
    assert (
        manifest["artifact"]["sha256"] == sha256(artifact_path.read_bytes()).hexdigest()
    )
    assert manifest["coverage"]["claim"] == "PARTIAL"
    assert manifest["coverage"]["missing_tickers"] == ["MISSING"]
    assert manifest["source_run_id"] == yahoo.request_id(request)


def test_output_and_cache_paths_must_stay_in_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert yahoo.resolve_output_path("outputs/result.json", workspace).is_relative_to(
        workspace
    )
    assert yahoo.resolve_cache_dir("cache", workspace).is_relative_to(workspace)
    with pytest.raises(ValueError, match="inside"):
        yahoo.resolve_output_path(str(tmp_path / "outside.json"), workspace)
    with pytest.raises(ValueError, match="inside"):
        yahoo.resolve_cache_dir(str(tmp_path / "outside-cache"), workspace)


def test_all_absorbed_source_histories_are_ancestors_and_manifested() -> None:
    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "docs"
            / "providers"
            / "market-data-terminal-source-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["governing_issue"] == 107
    for source in manifest["sources"]:
        commit = source["commit"]
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        assert (REPOSITORY_ROOT / source["retained_path"]).exists()


def test_no_duplicate_yahoo_runtime_survives() -> None:
    implementations = list(REPOSITORY_ROOT.glob("**/fetch_yfinance.py"))
    assert implementations == [SCRIPT_PATH]
    legacy_import = REPOSITORY_ROOT / "imports" / "yf-marketdata"
    assert not any(legacy_import.rglob("*.py"))

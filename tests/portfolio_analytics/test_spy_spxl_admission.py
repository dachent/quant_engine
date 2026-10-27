"""SPY and SPXL are governed catalog candidates and core weekly benchmarks."""

import importlib.util
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _catalog() -> dict:  # type: ignore[type-arg]
    return json.loads(
        (
            REPOSITORY_ROOT / "docs/analytics/decomposition-source-catalog.json"
        ).read_text(encoding="utf-8")
    )


def test_spy_and_spxl_are_yahoo_adjusted_candidates_of_the_multi_asset_universe() -> (
    None
):
    universe = next(
        u
        for u in _catalog()["universes"]
        if u["universe_id"] == "multi-asset-etf-style"
    )
    by_id = {c["candidate_id"]: c for c in universe["candidates"]}
    for ticker in ("SPY", "SPXL"):
        assert by_id[ticker]["source_id"] == "yahoo-adjusted-etf-daily"
        assert by_id[ticker]["raw_orthogonal"] is False
    assert by_id["SPXL"]["economic_family"] != by_id["SPY"]["economic_family"]


def test_core_benchmarks_include_spy_and_spxl_and_the_panel_count_agrees() -> None:
    weekly = _load(
        "portfolio_weekly_returns_for_admission", "tools/portfolio_weekly_returns.py"
    )
    assert {"SPY", "SPXL"} <= set(weekly.CORE_BENCHMARKS)
    assert len(weekly.CORE_BENCHMARKS) == 11
    source = (REPOSITORY_ROOT / "tools/portfolio_control_panel.py").read_text(
        encoding="utf-8"
    )
    assert "CORE_BENCHMARK_COUNT: Final = 11" in source
    assert "!= 9" not in source.split("def _govern_weekly_hybrid_modes")[0][-6000:]

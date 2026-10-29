"""#540 payload budget: the checker, the budget file, and a CI-sized shape guard."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


size = _load("panel_payload_size", REPOSITORY_ROOT / "tools" / "panel_payload_size.py")
budget = json.loads(size.BUDGET_PATH.read_bytes())
fixture_tab = _load(
    "test_performance_tab_fixture",
    REPOSITORY_ROOT / "tests" / "portfolio_control_panel" / "test_performance_tab.py",
)


def test_every_budget_covers_a_committed_figure_within_three_percent_plus_rounding() -> (
    None
):
    committed = budget["committed_bytes"]["blocks"]
    assert set(committed) == set(budget["blocks"])
    for name, limit in budget["blocks"].items():
        assert committed[name] <= limit <= committed[name] * 1.03 + 1000


def test_physical_budgets_cover_the_committed_generation() -> None:
    committed = budget["committed_bytes"]
    pairs = {
        "total_bytes": "total_physical",
        "index_html_bytes": "index_html",
        "dashboard_data_json_bytes": "dashboard_data_json",
        "dashboard_data_js_bytes": "dashboard_data_js",
    }
    for key, committed_key in pairs.items():
        assert committed[committed_key] <= budget["physical"][key]


def _fits(blocks: dict[str, int], physical: dict[str, Any] | None = None) -> list[str]:
    return size.check(blocks, physical or {}, budget)


def test_committed_figures_pass_and_growth_fails() -> None:
    committed = dict(budget["committed_bytes"]["blocks"])
    assert _fits(committed) == []
    grown = {**committed, "performance_tab": budget["blocks"]["performance_tab"] + 1}
    assert [f.split(":")[0] for f in _fits(grown)] == ["block performance_tab"]


def test_a_new_large_block_needs_a_budget_and_small_ones_share_one() -> None:
    committed = dict(budget["committed_bytes"]["blocks"])
    assert len(_fits({**committed, "daily_grid": 60_000})) == 1
    assert _fits({**committed, "meta": 1_000}) == []
    crowded = {f"small_{i}": 40_000 for i in range(11)}
    assert any("unlisted small blocks" in f for f in _fits(crowded))


def test_download_copies_and_physical_growth_fail() -> None:
    findings = _fits(
        {},
        {
            "total_bytes": budget["physical"]["total_bytes"] + 1,
            "download_copies": ["downloads/portfolio-report-data.json"],
        },
    )
    assert len(findings) == 2


def test_block_sizes_measures_canonical_value_bytes() -> None:
    assert size.block_sizes({"a": {"b": [1, 2]}, "c": "x"}) == {"a": 11, "c": 3}


def test_fixture_performance_tab_stays_compact() -> None:
    # A block that starts carrying per-observation duplicates or uncompacted grids grows first.
    panel = fixture_tab._panel()
    block = fixture_tab.tab.build_performance_tab(panel)
    measured = size.block_sizes({"performance_tab": block})["performance_tab"]
    assert measured <= budget["fixture"]["performance_tab_max_bytes"], measured


def test_cli_reports_blocks_and_fails_on_an_over_budget_generation(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "index.html").write_bytes(b"x" * 10)
    payload = {"performance_tab": "y" * (budget["blocks"]["performance_tab"] + 1)}
    (tmp_path / "data" / "dashboard_data.json").write_bytes(
        json.dumps(payload).encode()
    )
    out = tmp_path / "report.json"
    assert size.main([str(tmp_path), "--out", str(out)]) == 1
    report = json.loads(out.read_bytes())
    assert report["physical"]["index_html_bytes"] == 10
    assert len(report["findings"]) == 1
    assert size.main([str(tmp_path), "--out", str(out), "--no-check"]) == 0

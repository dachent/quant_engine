"""#540 payload budget: the validator blocks a payload over the committed budget."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from acceptance.validate import checks

ROOT = Path(__file__).resolve().parents[2]
BUDGET_FILE = ROOT / "apps" / "portfolio-control-panel" / "payload_budget.json"
sys.path.insert(0, str(ROOT / "tools"))

import panel_payload_size as script  # noqa: E402

BUDGET: dict[str, Any] = {
    "blocks": {"a": 1000, "b": 5000},
    "unlisted_block_max_bytes": 100,
    "other_blocks_max_bytes": 150,
    "physical": {"total_bytes": 10000, "index_html_bytes": 500},
}


def over(blocks: dict[str, int], physical: dict[str, int] | None = None) -> list[str]:
    return [
        f[0] for f in checks.payload_budget_findings(blocks, physical or {}, BUDGET)
    ]


def test_under_budget_passes() -> None:
    assert over({"a": 1000, "b": 10, "c": 100}, {"total_bytes": 10000}) == []


def test_a_listed_block_one_byte_over_fails() -> None:
    assert over({"a": 1001, "b": 10}) == ["a"]


def test_an_unlisted_block_over_the_maximum_fails() -> None:
    assert over({"c": 101}) == ["c"]


def test_unlisted_small_blocks_together_over_their_budget_fail() -> None:
    assert over({"c": 80, "d": 80}) == ["unlisted small blocks"]


def test_physical_over_budget_fails() -> None:
    assert over({}, {"index_html_bytes": 501}) == ["index_html_bytes"]


def test_the_block_measurement_is_the_scripts_measurement() -> None:
    panel = {"a": {"x": [1, "é"]}, "b": "y"}
    assert checks.payload_block_bytes(panel) == script.block_sizes(panel)


def test_committed_budget_is_measured_plus_one_percent_rounded_up() -> None:
    budget = json.loads(BUDGET_FILE.read_bytes())
    measured = budget["committed_bytes"]["blocks"]
    assert measured
    for name, size in measured.items():
        assert budget["blocks"][name] == math.ceil(size * 1.01 / 1000) * 1000, name
    assert {"model_vs_actual", "performance", "strategies"} <= set(budget["blocks"])


def test_the_scripts_rule_gives_140000_for_138128() -> None:
    budget = json.loads(BUDGET_FILE.read_bytes())
    raised = script.raise_budgets(
        budget,
        ["model_vs_actual"],
        {"model_vs_actual": 1},
        {"model_vs_actual": 138128},
        margin=0.01,
        generation="g",
    )
    assert raised["blocks"]["model_vs_actual"] == 140000


def test_the_scripts_check_and_the_validators_agree_on_the_committed_budget() -> None:
    budget = json.loads(BUDGET_FILE.read_bytes())
    blocks = {n: budget["blocks"][n] + 1 for n in ("strategies", "performance")}
    findings = checks.payload_budget_findings(blocks, {}, budget)
    assert sorted(f[0] for f in findings) == ["performance", "strategies"]
    assert script.check(blocks, {}, budget)


def test_a_missing_budget_file_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "PAYLOAD_BUDGET_RELATIVE", Path("nowhere.json"))
    ctx: Any = type("C", (), {"repo_root": tmp_path})()
    budget, why = checks._payload_budget(ctx)
    assert budget is None and "not found" in why


@pytest.mark.skipif(
    not os.environ.get("QE_PAYLOAD_PANEL"),
    reason="QE_PAYLOAD_PANEL (a real generation directory) is not set",
)
def test_a_real_generation_fits_the_committed_budget() -> None:
    generation = Path(os.environ["QE_PAYLOAD_PANEL"])
    data = json.loads((generation / "data" / "dashboard_data.json").read_bytes())
    budget = json.loads(BUDGET_FILE.read_bytes())
    sizes = script.physical(generation)
    assert (
        checks.payload_budget_findings(checks.payload_block_bytes(data), sizes, budget)
        == []
    )


def _ctx(files: dict[str, tuple[int, str]], embedded_only: bool) -> Any:
    from types import SimpleNamespace

    scan = SimpleNamespace(files=files, report_embedded_only=embedded_only)
    return SimpleNamespace(gen=SimpleNamespace(scan=scan))


def test_total_bytes_counts_files_on_disk_not_the_embedded_virtual_report() -> None:
    files = {
        "index.html": (9000, "a"),
        "data/dashboard_data.json": (500, "b"),
        checks.REPORT: (900_000, "c"),
    }
    sizes = checks._physical_sizes(_ctx(files, embedded_only=True))
    assert sizes["total_bytes"] == 9500 and sizes["index_html_bytes"] == 9000
    # a report that is a real file is a file on disk, and counts
    assert checks._physical_sizes(_ctx(files, embedded_only=False))["total_bytes"] == (
        909_500
    )
    # over-budget still blocks: the same physical figure against a smaller budget
    assert over({"a": 1}, {"total_bytes": sizes["total_bytes"]}) == []
    assert over({"a": 1}, {"total_bytes": 10_001}) == ["total_bytes"]


def test_the_validator_and_the_script_measure_the_same_physical_total(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "index.html").write_bytes(b"x" * 300)
    (tmp_path / "data" / "dashboard_data.json").write_bytes(b"{}" * 50)
    files = {
        rel: (path.stat().st_size, "d")
        for path in tmp_path.rglob("*")
        if path.is_file()
        for rel in [path.relative_to(tmp_path).as_posix()]
    }
    # the embedded virtual report is in the scan, not on disk
    files[checks.REPORT] = (777_777, "e")
    sizes = checks._physical_sizes(_ctx(files, embedded_only=True))
    measured = script.physical(tmp_path)
    for key in ("total_bytes", "index_html_bytes", "dashboard_data_json_bytes"):
        assert sizes[key] == measured[key], key


def test_every_block_in_the_committed_budget_is_checked_one_byte_over() -> None:
    budget = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    for name in ("performance_tab", "orthogonal_risk_attribution", "limitations"):
        assert name in budget["blocks"], name
    for name, limit in budget["blocks"].items():
        found = checks.payload_budget_findings({name: limit + 1}, {}, budget)
        assert [f[0] for f in found] == [name], name
        assert checks.payload_budget_findings({name: limit}, {}, budget) == [], name

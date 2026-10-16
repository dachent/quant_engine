"""Every architecture ban fires, and the live tree matches its allowlist."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import check_architecture as arch  # noqa: E402

CORE = "packages/core/src/quant_engine/core/mod.py"
ALPACA = "packages/alpaca/src/quant_engine/alpaca/mod.py"
LEDGER = "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/returns.py"
TOOL = "tools/some_tool.py"
OTHER_TOOL = "tools/other_tool.py"


def _rules(files: dict[str, str], tmp_path: Path) -> set[str]:
    return {rule for rule, *_ in arch.census(files, root=tmp_path)}


@pytest.mark.parametrize(
    ("rule", "files"),
    [
        ("LAYER", {CORE: "from quant_engine.portfolio_ledger import x"}),
        ("LAYER", {ALPACA: "import quant_engine.schwab"}),
        ("LAYER", {CORE: "import other_tool", OTHER_TOOL: ""}),
        ("LAYER", {TOOL: "import acceptance"}),
        ("PRIVATE", {TOOL: "from other_tool import _hidden", OTHER_TOOL: ""}),
        ("PRIVATE", {TOOL: "import other_tool\nother_tool._hidden()", OTHER_TOOL: ""}),
        ("SUBMODULE", {TOOL: "from quant_engine.core.mod import x", CORE: ""}),
        ("SUBMODULE", {TOOL: "from quant_engine.core import mod", CORE: ""}),
        (
            "DYNAMIC",
            {
                TOOL: "import importlib.util\nimportlib.util.spec_from_file_location('a','b')"
            },
        ),
        ("DYNAMIC", {TOOL: "spec.loader.exec_module(m)"}),
        ("DYNAMIC", {TOOL: "import sys\nsys.path.insert(0, 'x')"}),
        ("DYNAMIC", {TOOL: "import sys\nsys.path = []"}),
        ("DYNAMIC", {TOOL: "def f():\n    _tool('x.py')"}),
        (
            "DYNAMIC",
            {TOOL: "import subprocess\nsubprocess.run(['python', 'tools/x.py'])"},
        ),
        ("PROVIDER_NAME", {TOOL: "PROVIDER = 'schwab'"}),
    ],
)
def test_each_ban_fires(rule: str, files: dict[str, str], tmp_path: Path) -> None:
    assert rule in _rules(files, tmp_path)


def test_undeclared_dependency_fires_only_when_not_declared(tmp_path: Path) -> None:
    project = tmp_path / "packages" / "alpaca"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["httpx>=1"]\n', encoding="utf-8"
    )
    assert "UNDECLARED" in _rules({ALPACA: "import flask"}, tmp_path)
    assert "UNDECLARED" not in _rules({ALPACA: "import httpx\nimport json"}, tmp_path)


def test_allowed_patterns_do_not_fire(tmp_path: Path) -> None:
    files = {
        ALPACA: "from quant_engine.core import x\nfrom quant_engine.portfolio_ledger import y",
        CORE: "from . import sibling",
        TOOL: "from quant_engine.core import x\nimport sys\nsys.path.append  # not a call",
        "tools/schwab_generation.py": "NAME = 'schwab'",
    }
    assert arch.census(files, root=tmp_path) == []


def test_new_violation_fails_and_removed_one_is_stale() -> None:
    allowed = {"DYNAMIC": {"tools/a.py": {"sys.path": 2}}}
    assert arch.compare({"DYNAMIC": {"tools/a.py": {"sys.path": 3}}}, allowed) == (
        ["DYNAMIC tools/a.py [sys.path]: 3 > allowed 2"],
        [],
    )
    assert arch.compare({"DYNAMIC": {"tools/b.py": {"sys.path": 1}}}, allowed)[0] == [
        "DYNAMIC tools/b.py [sys.path]: 1 > allowed 0"
    ]
    assert arch.compare({"DYNAMIC": {}}, allowed) == (
        [],
        ["DYNAMIC tools/a.py [sys.path]: 0 < allowed 2"],
    )


def test_swapping_one_violation_for_another_in_an_allowlisted_file_fails() -> None:
    allowed = {"PRIVATE": {"tools/a.py": {"tools:x._old": 1}}}
    swapped = {"PRIVATE": {"tools/a.py": {"tools:y._new": 1}}}
    new, stale = arch.compare(swapped, allowed)
    assert new == ["PRIVATE tools/a.py [tools:y._new]: 1 > allowed 0"]
    assert stale == ["PRIVATE tools/a.py [tools:x._old]: 0 < allowed 1"]


def test_relative_cross_package_import_is_a_layer_violation(tmp_path: Path) -> None:
    deep = "packages/alpaca/src/quant_engine/alpaca/sub/deep.py"
    files = {
        ALPACA: "from ..schwab import api",
        deep: "from ...schwab import api; from ...core import x",
    }
    found = arch.census(files, root=tmp_path)
    assert ("LAYER", ALPACA, 1, "alpaca->schwab") in found
    assert ("LAYER", deep, 1, "alpaca->schwab") in found
    assert not [f for f in found if f[1] == deep and f[3] == "alpaca->core"]


def test_stale_import_graph_package_edges_fail_the_default_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "import-graph.json"
    stale.write_text(json.dumps({"package_edges": [["x", "y", 1]]}), encoding="utf-8")
    monkeypatch.setattr(arch, "GRAPH", stale)
    assert arch.main([]) == 1
    assert "import-graph.json is stale" in capsys.readouterr().err


def test_live_tree_matches_allowlist_and_contract_files() -> None:
    actual = arch.counts(arch.census(arch.tracked_python(REPOSITORY_ROOT)))
    allowed = json.loads(arch.ALLOWLIST.read_text(encoding="utf-8"))
    assert arch.compare(actual, allowed) == ([], [])
    edges = json.loads(arch.EDGES.read_text(encoding="utf-8"))
    assert edges == arch.edges_contract()


def test_module_less_relative_import_resolves_the_imported_package(
    tmp_path: Path,
) -> None:
    files = {
        ALPACA: "from .. import schwab",
        "packages/alpaca/src/quant_engine/alpaca/other.py": "from quant_engine import schwab",
        "packages/alpaca/src/quant_engine/alpaca/ok.py": "from .. import core",
    }
    found = arch.census(files, root=tmp_path)
    assert ("LAYER", ALPACA, 1, "alpaca->schwab") in found
    assert [f[3] for f in found if f[1].endswith("other.py") and f[0] == "LAYER"] == [
        "alpaca->schwab"
    ]
    assert not [f for f in found if f[1].endswith("ok.py") and f[0] == "LAYER"]


def test_stale_tools_edges_fail_the_default_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = arch.import_graph(arch.tracked_python(REPOSITORY_ROOT))
    graph["tools_edges"] = [*graph["tools_edges"], ["a_tool", "b_tool", 1]]
    stale = tmp_path / "import-graph.json"
    stale.write_text(json.dumps(graph), encoding="utf-8")
    monkeypatch.setattr(arch, "GRAPH", stale)
    assert arch.main([]) == 1
    assert "import-graph.json is stale" in capsys.readouterr().err

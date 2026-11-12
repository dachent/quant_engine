"""RUN-TIME #887 and #892: stages of one ``parallel_group`` run at the same time.

The replica ceremony runs beside the real lane (the commit stays gated on both), and the two
browser QA stages read the same candidate side by side. The kill test is a rendezvous: each member
of the group waits for a file only its sibling writes, so one-after-the-other execution deadlocks
(and the member times out) where parallel execution passes.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_driver as driver  # noqa: E402
from test_release_driver import (  # noqa: E402,F401
    _drive,
    _graph,
    _write_stage,
    world,
)

SHIPPED = REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json"

RENDEZVOUS = (
    "import sys,time,pathlib;"
    "mine,theirs=map(pathlib.Path,sys.argv[1:3]);"
    "mine.write_text('here');"
    "end=time.time()+20;"
    "ok=False\n"
    "while time.time()<end and not ok:\n"
    "    ok=theirs.exists();time.sleep(0.05)\n"
    "raise SystemExit(0 if ok else 7)"
)


def _member(name: str, other: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": name,
        "requires": ["first"],
        "parallel_group": "g",
        "outputs": [f"{{cycle}}/{name}.out"],
        "argv": [
            "{python}",
            "-c",
            RENDEZVOUS,
            f"{{cycle}}/{name}.out",
            f"{{cycle}}/{other}.out",
        ],
        **extra,
    }


FIRST = {
    "id": "first",
    "outputs": ["{cycle}/first.txt"],
    "argv": _write_stage("{cycle}/first.txt"),
}


def _state(world: dict[str, Any]) -> dict[str, Any]:
    return json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))


def test_the_members_of_a_group_run_at_the_same_time_and_both_are_recorded(
    world: dict[str, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    graph = _graph(world, [FIRST, _member("a", "b"), _member("b", "a")])
    assert _drive(world, graph) == 0
    stages = _state(world)["stages"]
    assert [stages[i]["status"] for i in ("first", "a", "b")] == ["ok"] * 3
    printed = capsys.readouterr().out
    assert "parallel group g: a, b" in printed
    # a later run keeps both, verified from their recorded bytes
    assert _drive(world, graph) == 0


def test_a_group_member_that_fails_fails_the_run_and_the_other_is_still_recorded(
    world: dict[str, Any],  # noqa: F811
) -> None:
    bad = _member("b", "a")
    bad["argv"] = ["{python}", "-c", "raise SystemExit(3)"]
    bad["outputs"] = []
    ok_alone = _member("a", "b")
    ok_alone["argv"] = _write_stage("{cycle}/a.out")
    graph = _graph(world, [FIRST, ok_alone, bad])
    assert _drive(world, graph) == 1
    stages = _state(world)["stages"]
    assert stages["a"]["status"] == "ok" and stages["b"]["status"] == "failed"


def test_a_refusal_of_one_member_starts_no_member(
    world: dict[str, Any],  # noqa: F811
) -> None:
    # b's output is left over from an earlier pass: b refuses, and a must not have run either
    (world["cycle"]).mkdir(parents=True, exist_ok=True)
    (world["cycle"] / "b.out").write_text("old", encoding="utf-8")
    graph = _graph(world, [FIRST, _member("a", "b"), _member("b", "a")])
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "a.out").exists()
    assert "a" not in _state(world)["stages"] or _state(world)["stages"]["a"].get(
        "status"
    ) != "ok"


def test_a_stage_alone_in_its_group_runs_like_any_stage(
    world: dict[str, Any],  # noqa: F811
) -> None:
    solo = {**_member("a", "b"), "argv": _write_stage("{cycle}/a.out")}
    graph = _graph(world, [FIRST, solo])
    assert _drive(world, graph) == 0


def test_the_shipped_graph_runs_the_replica_ceremony_beside_the_real_lane() -> None:
    stages = {s["id"]: s for s in json.loads(SHIPPED.read_text(encoding="utf-8"))["stages"]}
    real, replica = stages["lane-real"], stages["lane-replica"]
    assert real["parallel_group"] == replica["parallel_group"] == "lanes"
    # neither waits for the other; both wait for the replica spec
    assert "lane-replica" not in real["requires"] and "lane-real" not in replica["requires"]
    # the commit needs the ceremony receipt AND the real lane, exactly as it did with one stage
    assert set(stages["commit"]["requires"]) == {"lane-real", "lane-replica"}
    assert "replica_receipt" in stages["commit"]["commit_gate"]
    assert "replica_verify" in stages["commit"]["commit_gate"]
    assert "lanes" not in stages
    # the real lane announces the release id while the ceremony runs
    assert real.get("announce_release") is True
    assert "real-lane" in real["argv"] and "ceremony" in replica["argv"]


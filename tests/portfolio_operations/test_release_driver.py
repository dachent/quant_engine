"""RELEASE-DRIVER-001: the driver refuses stale, leftover, wrong-generation and moved-repository states."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_driver as driver  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "frozen")
    cycle = tmp_path / "cycle"
    run = tmp_path / "run"
    (run / "stages" / "ledger" / "generations" / "L1").mkdir(parents=True)
    (run / "stages" / "panel").mkdir(parents=True)
    (run / "stages" / "panel" / "CURRENT.json").write_text(
        '{"generation_id": "P1"}', encoding="utf-8"
    )
    config = {
        "repo_root": str(repo),
        "python": sys.executable,
        "cycle": str(cycle),
        "run": str(run),
        "code_commit": _git(repo, "rev-parse", "HEAD"),
        "mode": "release",
    }
    config_path = tmp_path / "run.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return {
        "repo": repo,
        "cycle": cycle,
        "run": run,
        "config": config_path,
        "tmp": tmp_path,
    }


def _write_stage(out: str, text: str = "x") -> list[str]:
    return ["{python}", "-c", f"import sys;open(sys.argv[1],'w').write({text!r})", out]


def _graph(world: dict[str, Any], stages: list[dict[str, Any]], **extra: Any) -> Path:
    graph = {"schema_version": 1, "paths": {}, "derive": {}, "stages": stages, **extra}
    path = world["tmp"] / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def _drive(world: dict[str, Any], graph: Path, *args: str) -> int:
    return driver.main(
        ["run", "--config", str(world["config"]), "--graph", str(graph), *args]
    )


CHAIN = [
    {
        "id": "one",
        "outputs": ["{cycle}/one.txt"],
        "argv": _write_stage("{cycle}/one.txt", "1"),
    },
    {
        "id": "two",
        "requires": ["one"],
        "outputs": ["{cycle}/two.txt"],
        "argv": _write_stage("{cycle}/two.txt", "2"),
    },
]


def test_runs_in_order_records_state_and_keeps_completed_stages(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph) == 0
    state = json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert [state["stages"][name]["status"] for name in ("one", "two")] == ["ok", "ok"]
    assert (
        state["stages"]["one"]["code_commit"]
        == json.loads(world["config"].read_text())["code_commit"]
    )
    # a second invocation neither reruns nor refuses completed stages
    assert _drive(world, graph) == 0


def test_exit_code_decides_not_log_text(world: dict[str, Any]) -> None:
    noisy_but_ok = [
        "{python}",
        "-c",
        "print('CHAIN_B_FAIL tail2'); open(__import__('sys').argv[1],'w').write('x')",
        "{cycle}/a.txt",
    ]
    failing = ["{python}", "-c", "raise SystemExit(3)"]
    graph = _graph(
        world,
        [
            {"id": "noisy", "outputs": ["{cycle}/a.txt"], "argv": noisy_but_ok},
            {"id": "bad", "requires": ["noisy"], "argv": failing},
            {
                "id": "never",
                "requires": ["bad"],
                "outputs": ["{cycle}/never.txt"],
                "argv": _write_stage("{cycle}/never.txt"),
            },
        ],
    )
    assert _drive(world, graph) == 1
    state = json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert state["stages"]["noisy"]["status"] == "ok"
    assert (
        state["stages"]["bad"]["status"] == "failed"
        and state["stages"]["bad"]["rc"] == 3
    )
    assert (
        "never" not in state["stages"] and not (world["cycle"] / "never.txt").exists()
    )


def test_refuses_a_dirty_or_moved_repository(world: dict[str, Any]) -> None:
    graph = _graph(world, CHAIN)
    (world["repo"] / "tracked.txt").write_text("edited\n", encoding="utf-8")
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "one.txt").exists()
    _git(world["repo"], "checkout", "-q", "--", "tracked.txt")
    (world["repo"] / "tracked.txt").write_text("b\n", encoding="utf-8")
    _git(world["repo"], "commit", "-q", "-am", "moved")
    assert _drive(world, graph) == 2


def test_a_stage_that_dirties_the_repository_fails(world: dict[str, Any]) -> None:
    dirty = [
        "{python}",
        "-c",
        "import sys;open(sys.argv[1],'w').write('z')",
        "{repo_root}/tracked.txt",
    ]
    graph = _graph(world, [{"id": "dirty", "argv": dirty}])
    assert _drive(world, graph) == 1


def test_refuses_leftover_outputs_from_an_earlier_pass_and_replace_moves_them_aside(
    world: dict[str, Any],
) -> None:
    world["cycle"].mkdir()
    leftover = world["cycle"] / "one.txt"
    leftover.write_text("pass-20", encoding="utf-8")
    graph = _graph(world, CHAIN)
    assert _drive(world, graph) == 2
    assert leftover.read_text(encoding="utf-8") == "pass-20"
    assert _drive(world, graph, "--replace") == 0
    assert leftover.read_text(encoding="utf-8") == "1"
    assert [
        p.read_text(encoding="utf-8") for p in world["cycle"].glob("one.txt.prev-*")
    ] == ["pass-20"]


def test_refuses_a_stage_whose_required_output_changed_after_it_was_produced(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph, "--through", "one") == 0
    (world["cycle"] / "one.txt").write_text("edited by another pass", encoding="utf-8")
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "two.txt").exists()


def test_refuses_a_required_stage_that_ran_at_another_code_identity(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph, "--through", "one") == 0
    state_path = world["cycle"] / driver.STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["stages"]["one"]["code_commit"] = "0" * 40
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _drive(world, graph) == 2


def test_refuses_a_generation_that_is_not_the_one_the_run_produced(
    world: dict[str, Any],
) -> None:
    spec = world["tmp"] / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "kind": "GENERATION",
                        "root_id": "panel",
                        "generation_id": "OLD85d0e609",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stages = [
        {
            "id": "findings",
            "assert_equal": [["{panel_gid}", "{spec_panel_gid}"]],
            "outputs": ["{cycle}/f.txt"],
            "argv": _write_stage("{cycle}/f.txt"),
        }
    ]
    derive = {
        "panel_gid": {
            "json_field": ["{run}/stages/panel/CURRENT.json", "generation_id"]
        },
        "spec_panel_gid": {"spec_node_field": [str(spec), "panel", "generation_id"]},
    }
    assert _drive(world, _graph(world, stages, derive=derive)) == 2
    assert not (world["cycle"] / "f.txt").exists()
    spec.write_text(
        json.dumps(
            {
                "nodes": [
                    {"kind": "GENERATION", "root_id": "panel", "generation_id": "P1"}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert _drive(world, _graph(world, stages, derive=derive)) == 0


def test_a_derived_id_needs_exactly_one_entry(world: dict[str, Any]) -> None:
    (world["run"] / "stages" / "ledger" / "generations" / "L2").mkdir()
    stages = [{"id": "s", "argv": ["{python}", "-c", "pass", "{ledger_id}"]}]
    derive = {"ledger_id": {"single_dir": "{run}/stages/ledger/generations"}}
    assert _drive(world, _graph(world, stages, derive=derive)) == 2


def test_json_check_failure_fails_the_stage(world: dict[str, Any]) -> None:
    write = [
        "{python}",
        "-c",
        "import sys;open(sys.argv[1],'w').write('{\"ready\": false}')",
        "{cycle}/plan.json",
    ]
    stage = {
        "id": "plan",
        "outputs": ["{cycle}/plan.json"],
        "argv": write,
        "checks": [{"json": "{cycle}/plan.json", "path": "ready", "equals": True}],
    }
    assert _drive(world, _graph(world, [stage])) == 1


def _pointer_world(world: dict[str, Any]) -> Path:
    root = world["tmp"] / "roots" / "r"
    root.mkdir(parents=True)
    (root / "CURRENT.json").write_text('{"generation_id": "G0"}', encoding="utf-8")
    coordination = world["tmp"] / "coord"
    (coordination / "releases").mkdir(parents=True)
    spec = world["tmp"] / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "coordination_root": str(coordination),
                "roots": {"r": str(root)},
                "companions": [],
            }
        ),
        encoding="utf-8",
    )
    return spec


def _dry_run_stage(code: str, spec: Path) -> dict[str, Any]:
    return {
        "id": "dry",
        "pointers_unchanged": str(spec),
        "snapshot_to": "{cycle}/after.json",
        "outputs": ["{cycle}/execute.json", "{cycle}/after.json"],
        "stdout_to": "{cycle}/execute.json",
        "argv": ["{python}", "-c", code, str(spec)],
    }


def test_dry_run_may_prepare_its_own_release_but_not_move_a_pointer(
    world: dict[str, Any],
) -> None:
    spec = _pointer_world(world)
    prepare = (
        "import json,sys,pathlib;"
        "s=json.load(open(sys.argv[1]));"
        "pathlib.Path(s['coordination_root'],'releases','R1').mkdir();"
        "print(json.dumps({'release_id':'R1'}))"
    )
    assert _drive(world, _graph(world, [_dry_run_stage(prepare, spec)])) == 0
    assert (world["cycle"] / "after.json").is_file()


def test_dry_run_that_moves_a_pointer_fails(world: dict[str, Any]) -> None:
    spec = _pointer_world(world)
    move = (
        "import json,sys,pathlib;"
        "s=json.load(open(sys.argv[1]));"
        "pathlib.Path(s['roots']['r'],'CURRENT.json').write_text('{\"generation_id\": \"G1\"}');"
        "print(json.dumps({'release_id':'R1'}))"
    )
    assert _drive(world, _graph(world, [_dry_run_stage(move, spec)])) == 1
    assert not (world["cycle"] / "after.json").exists()


def test_dry_run_that_prepares_another_release_fails(world: dict[str, Any]) -> None:
    spec = _pointer_world(world)
    stranger = (
        "import json,sys,pathlib;"
        "s=json.load(open(sys.argv[1]));"
        "pathlib.Path(s['coordination_root'],'releases','OTHER').mkdir();"
        "print(json.dumps({'release_id':'R1'}))"
    )
    assert _drive(world, _graph(world, [_dry_run_stage(stranger, spec)])) == 1


def _provider_graph(world: dict[str, Any], roots: list[str]) -> Path:
    stage = {
        "id": "provider-generations",
        "builtin": "provider_generations",
        "outputs": ["{cycle}/pg.json"],
    }
    return _graph(
        world,
        [stage],
        provider_roots=roots,
        provider_run_sources={"epoch-manifest": "{run}/ep"},
    )


def _config(world: dict[str, Any], **extra: Any) -> None:
    config = json.loads(world["config"].read_text(encoding="utf-8"))
    config.update(extra)
    world["config"].write_text(json.dumps(config), encoding="utf-8")


def test_provider_generations_is_generated_complete_and_refuses_gaps(
    world: dict[str, Any],
) -> None:
    alpaca = world["tmp"] / "alpaca"
    alpaca.mkdir()
    (world["run"] / "ep").mkdir()
    graph = _provider_graph(world, ["provider-alpaca", "epoch-manifest"])
    _config(world, provider_sources={"provider-alpaca": str(alpaca)})
    assert _drive(world, graph) == 0
    rows = json.loads((world["cycle"] / "pg.json").read_text(encoding="utf-8"))
    assert [row["root_id"] for row in rows] == ["provider-alpaca", "epoch-manifest"]
    # a bound root with no source, a missing directory and an unbound extra all refuse
    _config(world, provider_sources={})
    assert _drive(world, graph, "--only", "provider-generations", "--replace") == 2
    _config(world, provider_sources={"provider-alpaca": str(world["tmp"] / "absent")})
    assert _drive(world, graph, "--only", "provider-generations", "--replace") == 2
    _config(
        world,
        provider_sources={
            "provider-alpaca": str(alpaca),
            "provider-extra": str(alpaca),
        },
    )
    assert _drive(world, graph, "--only", "provider-generations", "--replace") == 2


def test_the_shipped_graph_is_self_consistent() -> None:
    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_text(
            encoding="utf-8"
        )
    )
    ids = [stage["id"] for stage in graph["stages"]]
    assert len(ids) == len(set(ids))
    config_keys = set()
    known = set(graph["paths"]) | set(graph["derive"])

    def placeholders(value: object) -> set[str]:
        return set(re.findall(r"\{([A-Za-z_]\w*)\}", json.dumps(value)))

    for name in list(graph["paths"].values()) + list(graph["derive"].values()):
        config_keys |= placeholders(name) - known
    for position, stage in enumerate(graph["stages"]):
        for required in stage.get("requires", []):
            assert required in ids[:position], (
                f"{stage['id']} requires {required} out of order"
            )
        config_keys |= placeholders(stage) - known
        if "builtin" not in stage:
            assert stage["argv"][0] == "{python}"
    config_keys |= placeholders(graph["provider_run_sources"]) - known
    assert config_keys == {
        "python",
        "repo_root",
        "cycle",
        "run",
        "qa",
        "pipeline_id",
        "history_store",
        "expect_superseded",
        "build_portfolio_root",
        "portfolio_root",
        "retained_cache_root",
        "requested_as_of",
    }
    for tool in re.findall(r"\{(?:tools|mvp2)\}/([\w.]+)", json.dumps(graph)):
        base = (
            "tools"
            if (REPO_ROOT / "tools" / tool).exists()
            else "ops/mvp2-release/scripts"
        )
        assert (REPO_ROOT / base / tool).is_file(), tool


def test_a_changed_run_configuration_invalidates_completed_stages(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph) == 0
    _config(world, requested_as_of="2026-09-18")
    assert _drive(world, graph) == 2
    assert _drive(world, graph, "--only", "one", "--replace") == 0


def test_kept_stages_are_revalidated_before_success_is_reported(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph) == 0
    (world["cycle"] / "two.txt").write_text("edited", encoding="utf-8")
    assert _drive(world, graph) == 2
    (world["cycle"] / "two.txt").write_text("2", encoding="utf-8")
    assert _drive(world, graph) == 0
    (world["cycle"] / "one.txt").unlink()
    assert _drive(world, graph) == 2
    (world["cycle"] / "one.txt").write_text("1", encoding="utf-8")
    (world["repo"] / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    assert _drive(world, graph) == 2


def test_directory_digest_sees_same_length_edits(tmp_path: Path) -> None:
    (tmp_path / "f.json").write_text("aaaa", encoding="utf-8")
    before = driver.path_digest(tmp_path)
    (tmp_path / "f.json").write_text("bbbb", encoding="utf-8")
    assert driver.path_digest(tmp_path) != before


def test_a_stage_that_omits_a_declared_output_fails(world: dict[str, Any]) -> None:
    stage = {
        "id": "partial",
        "outputs": ["{cycle}/a.txt", "{cycle}/b.txt"],
        "argv": _write_stage("{cycle}/a.txt"),
    }
    assert _drive(world, _graph(world, [stage])) == 1
    state = json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert "b.txt" in state["stages"]["partial"]["failures"][0]


def _guard_world(world: dict[str, Any], *, spec_under_live: bool) -> Path:
    live = world["tmp"] / "live"
    scratch = world["tmp"] / "scratch"
    base = live if spec_under_live else scratch
    (base / "dataPortfolio").mkdir(parents=True)
    spec = world["tmp"] / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "coordination_root": str(base / "dataPortfolio" / "releases"),
                "roots": {"r": str(base / "dataPortfolio" / "r")},
                "companions": [],
                "state_root": str(scratch),
            }
        ),
        encoding="utf-8",
    )
    _config(world, mode="probe", live_workspace=str(live))
    return spec


GUARDED = {
    "id": "guarded",
    "guard_spec": "{spec_path}",
    "outputs": ["{cycle}/g.txt"],
    "argv": _write_stage("{cycle}/g.txt"),
}


def test_probe_refuses_a_spec_bound_to_the_live_workspace(
    world: dict[str, Any],
) -> None:
    spec = _guard_world(world, spec_under_live=True)
    _config(world, spec_path=str(spec))
    assert _drive(world, _graph(world, [GUARDED])) == 2
    assert not (world["cycle"] / "g.txt").exists()


def test_probe_accepts_a_scratch_spec_and_release_mode_does_not_guard(
    world: dict[str, Any],
) -> None:
    spec = _guard_world(world, spec_under_live=False)
    _config(world, spec_path=str(spec))
    assert _drive(world, _graph(world, [GUARDED])) == 0
    live_spec = _guard_world(world, spec_under_live=True)
    _config(world, spec_path=str(live_spec), mode="release")
    assert (
        _drive(world, _graph(world, [GUARDED]), "--only", "guarded", "--replace") == 0
    )


def test_probe_refuses_a_live_portfolio_root_argument(world: dict[str, Any]) -> None:
    _guard_world(world, spec_under_live=False)
    _config(world, portfolio_root=str(world["tmp"] / "live" / "dataPortfolio"))
    stage = {
        "id": "g",
        "live_guard_keys": ["portfolio_root"],
        "outputs": ["{cycle}/g.txt"],
        "argv": _write_stage("{cycle}/g.txt"),
    }
    assert _drive(world, _graph(world, [stage])) == 2


def test_mode_must_be_declared(world: dict[str, Any]) -> None:
    config = json.loads(world["config"].read_text(encoding="utf-8"))
    del config["mode"]
    world["config"].write_text(json.dumps(config), encoding="utf-8")
    assert _drive(world, _graph(world, CHAIN)) == 2


def test_probe_refuses_a_live_retained_cache_even_when_the_portfolio_root_is_scratch(
    world: dict[str, Any],
) -> None:
    _guard_world(world, spec_under_live=False)
    _config(
        world,
        portfolio_root=str(world["tmp"] / "scratch" / "dataPortfolio"),
        retained_cache_root=str(world["tmp"] / "live" / "cache"),
    )
    stage = {
        "id": "g",
        "live_guard_keys": ["portfolio_root", "retained_cache_root"],
        "outputs": ["{cycle}/g.txt"],
        "argv": _write_stage("{cycle}/g.txt"),
    }
    assert _drive(world, _graph(world, [stage])) == 2


def test_stage_records_from_release_mode_are_not_reused_by_a_probe(
    world: dict[str, Any],
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph) == 0
    _config(world, mode="probe", live_workspace=str(world["tmp"] / "live"))
    assert _drive(world, graph) == 2


def test_the_shipped_graph_guards_every_stage_that_binds_a_root_or_cache() -> None:
    graph = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json").read_text(
            encoding="utf-8"
        )
    )
    by_id = {stage["id"]: stage for stage in graph["stages"]}
    assert set(by_id["production-spec"]["live_guard_keys"]) == {
        "portfolio_root",
        "retained_cache_root",
    }
    assert set(by_id["pipeline-spec"]["live_guard_keys"]) == {
        "build_portfolio_root",
        "retained_cache_root",
    }


def _node(node_id: str, generation: str) -> dict[str, Any]:
    return {
        "kind": "GENERATION",
        "node_id": node_id,
        "depends_on": [],
        "root_id": "r",
        "generation_id": generation,
        "manifest_sha256": "m" * 64,
        "release_target": True,
    }


def _spec(path: Path, base: Path, commit: str, generation: str = "G1") -> None:
    (base / "coord" / "releases").mkdir(parents=True, exist_ok=True)
    (base / "root").mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "coordination_root": str(base / "coord"),
                "roots": {"r": str(base / "root")},
                "companions": [],
                "code_commit": commit,
                "as_of_date": "2026-09-17",
                "pipeline_id": "p",
                "nodes": [_node("n1", generation)],
            }
        ),
        encoding="utf-8",
    )


GATE_STAGE = {
    "id": "commit",
    "commit_gate": {
        "spec": "{cycle}/spec.json",
        "replica_spec": "{cycle}/replica-spec.json",
        "replica_stages": ["replica-commit", "replica-restore"],
        "inputs_same": [["{cycle}/pg.json", "{cycle}/replica-pg.json"]],
        "pointer_snapshot": "{cycle}/after.json",
    },
    "outputs": ["{cycle}/committed.txt"],
    "argv": _write_stage("{cycle}/committed.txt"),
}
GATE_DERIVE = {
    "release_id": {"json_field": ["{cycle}/execute-stage.json", "release_id"]}
}


def _gate_world(world: dict[str, Any]) -> dict[str, Any]:
    cycle, tmp = world["cycle"], world["tmp"]
    cycle.mkdir(parents=True, exist_ok=True)
    commit = json.loads(world["config"].read_text(encoding="utf-8"))["code_commit"]
    _spec(cycle / "spec.json", tmp / "real", commit)
    _spec(cycle / "replica-spec.json", tmp / "replica", commit)
    (tmp / "real" / "coord" / "releases" / "R1").mkdir()
    (cycle / "execute-stage.json").write_text('{"release_id": "R1"}', encoding="utf-8")
    for name in ("pg.json", "replica-pg.json"):
        (cycle / name).write_text("[]", encoding="utf-8")
    (cycle / "after.json").write_text(
        json.dumps(driver.pointer_snapshot(cycle / "spec.json")), encoding="utf-8"
    )
    record = {"status": "ok", "code_commit": commit, "outputs": {}}
    (cycle / driver.STATE_NAME).write_text(
        json.dumps({"stages": {"replica-commit": record, "replica-restore": record}}),
        encoding="utf-8",
    )
    return {"commit": commit, "graph": _graph(world, [GATE_STAGE], derive=GATE_DERIVE)}


def _commit(
    world: dict[str, Any], gate: dict[str, Any], confirm: str | None = "R1"
) -> int:
    args = ["--confirm-release-id", confirm] if confirm else []
    return _drive(world, gate["graph"], *args)


def test_commit_runs_for_the_confirmed_release_after_a_matching_replica(
    world: dict[str, Any],
) -> None:
    gate = _gate_world(world)
    assert _commit(world, gate) == 0
    assert (world["cycle"] / "committed.txt").is_file()


@pytest.mark.parametrize("confirm", [None, "R2"])
def test_commit_refuses_without_the_owner_confirmed_exact_release(
    world: dict[str, Any], confirm: str | None
) -> None:
    gate = _gate_world(world)
    assert _commit(world, gate, confirm) == 2
    assert not (world["cycle"] / "committed.txt").exists()


def test_commit_refuses_probe_mode(world: dict[str, Any]) -> None:
    gate = _gate_world(world)
    _config(world, mode="probe")
    assert _commit(world, gate) == 2


def test_commit_refuses_a_missing_or_foreign_replica_receipt(
    world: dict[str, Any],
) -> None:
    gate = _gate_world(world)
    state_path = world["cycle"] / driver.STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["stages"]["replica-restore"]["status"] = "failed"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _commit(world, gate) == 2
    state["stages"]["replica-restore"].update(status="ok", code_commit="0" * 40)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _commit(world, gate) == 2
    del state["stages"]["replica-restore"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _commit(world, gate) == 2


def test_commit_refuses_a_replica_with_a_different_topology_or_inputs(
    world: dict[str, Any],
) -> None:
    gate = _gate_world(world)
    _spec(
        world["cycle"] / "replica-spec.json",
        world["tmp"] / "replica",
        gate["commit"],
        generation="G2",
    )
    assert _commit(world, gate) == 2
    _spec(
        world["cycle"] / "replica-spec.json", world["tmp"] / "replica", gate["commit"]
    )
    (world["cycle"] / "replica-pg.json").write_text(
        '[{"root_id": "x"}]', encoding="utf-8"
    )
    assert _commit(world, gate) == 2


def test_commit_refuses_an_unprepared_release_or_moved_pointers(
    world: dict[str, Any],
) -> None:
    gate = _gate_world(world)
    (world["tmp"] / "real" / "coord" / "releases" / "R1").rmdir()
    assert _commit(world, gate) == 2
    (world["tmp"] / "real" / "coord" / "releases" / "R1").mkdir()
    (world["tmp"] / "real" / "root" / "CURRENT.json").write_text(
        '{"generation_id": "G9"}', encoding="utf-8"
    )
    assert _commit(world, gate) == 2


def test_commit_refuses_a_spec_bound_to_another_code_commit(
    world: dict[str, Any],
) -> None:
    gate = _gate_world(world)
    for name, base in (("spec.json", "real"), ("replica-spec.json", "replica")):
        _spec(world["cycle"] / name, world["tmp"] / base, "f" * 40)
    assert _commit(world, gate) == 2

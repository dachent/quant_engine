"""RELEASE-DRIVER-001: schema, stale-input refusals seeded from R2's incidents, resume, PIDs, offline plan."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_driver as driver  # noqa: E402
from quant_engine.core import process_alive, process_started  # noqa: E402
from test_release_driver import (  # noqa: E402
    CHAIN,
    _config,
    _drive,
    _gate_world,
    _git,
    _graph,
    _write_stage,
    world,  # noqa: F401  (pytest fixture)
)

SHIPPED = REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json"

import pass26_commands  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _pass26_files(tmp_path_factory: pytest.TempPathFactory) -> None:
    global FIXTURES
    FIXTURES = tmp_path_factory.mktemp("pass26")
    pass26_commands.write(FIXTURES)


FIXTURES = Path()


def _shipped() -> dict[str, Any]:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


# -- schema -----------------------------------------------------------------------------


def test_the_shipped_graph_matches_its_schema() -> None:
    driver.validate_graph(_shipped())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g["stages"][0].update(surprise=1),
        lambda g: g["stages"][1].pop("argv"),
        lambda g: g["stages"][1].update(argv=["{python}"]),
        lambda g: g["stages"][1].update(builtin="inputs_only"),
        lambda g: g["stages"][1].update(requires=["no-such-stage"]),
        lambda g: g["stages"].append(copy.deepcopy(g["stages"][0])),
        lambda g: g["stages"][0].update(id="Bad Id"),
        lambda g: g["stages"][1].update(expect_rc="1"),
        lambda g: g.update(schema_version=2),
        lambda g: g["stages"][1].update(checks=[{"json": "a", "path": "b"}]),
    ],
)
def test_a_graph_that_does_not_match_the_schema_is_refused(mutate: Any) -> None:
    graph = _shipped()
    mutate(graph)
    with pytest.raises(driver.Refusal):
        driver.validate_graph(graph)


def test_run_refuses_an_invalid_graph_before_any_stage(world: dict[str, Any]) -> None:  # noqa: F811
    graph = _graph(world, [{**CHAIN[0], "surprise": True}])
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "one.txt").exists()


# -- R2 incident 1: a stale script path pointing at the probe repository -------------------


def test_incident_script_path_pointing_at_another_repository_is_refused(
    world: dict[str, Any],  # noqa: F811
) -> None:
    probe = world["tmp"] / "probe-repo" / "tools"
    probe.mkdir(parents=True)
    (probe / "tool.py").write_text(
        "open(__import__('sys').argv[1], 'w')\n", encoding="utf-8"
    )
    (world["repo"] / "tools").mkdir()
    graph = _graph(
        world,
        [
            {
                "id": "one",
                "outputs": ["{cycle}/one.txt"],
                "argv": ["{python}", "{tools}/tool.py", "{cycle}/one.txt"],
            }
        ],
        paths={"tools": "{repo_root}/tools"},
    )
    # the run configuration (or an old script) names the probe repo's tools directory
    _config(world, tools=str(probe))
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "one.txt").exists()
    # the graph's own default resolves inside the run's repository, where the script is missing: also refused
    _config(world, tools=str(world["repo"] / "tools"))
    assert _drive(world, graph) == 2


# -- R2 incident 2: a stale cache base path rewritten by a root rewrite --------------------


def _cache_graph(world: dict[str, Any]) -> Path:  # noqa: F811
    stage = {
        "id": "one",
        "outputs": ["{cycle}/one.txt"],
        "argv": [
            "{python}",
            "-c",
            "import sys;open(sys.argv[1],'w').write('x')",
            "{cycle}/one.txt",
            {"if": "retained_cache_base", "args": ["{retained_cache_base}"]},
        ],
    }
    return _graph(
        world,
        [stage],
        config_paths={
            "retained_cache_base": {"exists": True, "under": "{cycle}"},
            "retained_cache_root": {"under": "{cycle}"},
        },
    )


def test_incident_stale_cache_base_after_a_root_rewrite_is_refused(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _cache_graph(world)
    (world["cycle"]).mkdir(parents=True, exist_ok=True)
    older = world["tmp"] / "qe-p26" / "c" / "cachebase"  # the earlier pass's cache base
    older.mkdir(parents=True)
    _config(world, retained_cache_base=str(older))
    assert _drive(world, graph) == 2  # exists, but outside this cycle
    rewritten = (
        world["cycle"] / "cachebase"
    )  # the rewritten path names a directory that is not there
    _config(world, retained_cache_base=str(rewritten))
    assert _drive(world, graph) == 2
    rewritten.mkdir()
    assert _drive(world, graph) == 0
    _config(world, retained_cache_root=str(older / "cache"))
    assert _drive(world, graph, "--only", "one", "--replace") == 2


# -- R2 incident 3: a HEAD assertion that no longer holds ---------------------------------


def test_incident_head_assertion_refuses_before_and_after_the_repository_moves(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _graph(world, CHAIN)
    assert _drive(world, graph, "--through", "one") == 0
    (world["repo"] / "tracked.txt").write_text("b\n", encoding="utf-8")
    _git(world["repo"], "commit", "-q", "-am", "moved")
    assert _drive(world, graph) == 2
    assert not (world["cycle"] / "two.txt").exists()
    # `check` reports it without running anything, and a run frozen at the new head is a different run
    assert (
        driver.main(["check", "--config", str(world["config"]), "--graph", str(graph)])
        == 2
    )
    _config(world, code_commit=_git(world["repo"], "rev-parse", "HEAD"))
    assert (
        _drive(world, graph) == 2
    )  # `one` ran at the old identity: refused as a kept stage


# -- earlier R2 incidents ----------------------------------------------------------------


def test_incident_a_hand_built_input_that_names_another_pass_is_refused(
    world: dict[str, Any],  # noqa: F811
) -> None:
    (world["run"] / "stages" / "ledger" / "generations" / "L1").exists()
    spec = world["tmp"] / "prep-spec.json"
    spec.write_text('{"history": "H-pass18"}', encoding="utf-8")
    stage = {
        "id": "attended",
        "inputs": [str(spec)],
        "bound_ids": [[str(spec), "{history_id}"]],
        "outputs": ["{cycle}/a.txt"],
        "argv": _write_stage("{cycle}/a.txt"),
    }
    graph = _graph(
        world, [stage], derive={"history_id": {"single_dir": "{cycle}/hist"}}
    )
    (world["cycle"] / "hist" / "H-pass26").mkdir(parents=True)
    assert _drive(world, graph) == 2
    spec.write_text('{"history": "H-pass26"}', encoding="utf-8")
    assert _drive(world, graph) == 0


def test_the_driver_never_reads_a_log_to_decide_a_stage(world: dict[str, Any]) -> None:  # noqa: F811
    stage = {
        "id": "one",
        "outputs": ["{cycle}/one.txt"],
        "argv": [
            "{python}",
            "-c",
            "import sys;open(sys.argv[1],'w');print('FAIL step rc=1');sys.exit(0)",
            "{cycle}/one.txt",
        ],
    }
    assert _drive(world, _graph(world, [stage])) == 0


# -- resume from a stage's own boundary ---------------------------------------------------

FLAKY = (
    "import sys;p=sys.argv[1];t=open(p).read() if __import__('os').path.exists(p) else '';"
    "open(p,'w').write(t+'x');sys.exit(0 if len(t)>=1 else 1)"
)


ONCE = (
    "import os,sys;open(sys.argv[1],'a').write('partial');"
    "m=sys.argv[2];first=not os.path.exists(m);open(m,'w');"
    "sys.exit(1) if first else open(sys.argv[1],'w').write('done')"
)


def _flaky_graph(world: dict[str, Any]) -> Path:  # noqa: F811
    stages = [
        dict(CHAIN[0]),
        {
            "id": "long",
            "requires": ["one"],
            "outputs": ["{cycle}/long.txt"],
            "resume": {"same_argv": True},
            "argv": ["{python}", "-c", FLAKY, "{cycle}/long.txt"],
        },
        {
            "id": "after",
            "requires": ["long"],
            "outputs": ["{cycle}/after.txt"],
            "argv": _write_stage("{cycle}/after.txt"),
        },
    ]
    return _graph(world, stages)


def test_a_failed_resumable_stage_restarts_from_its_own_boundary(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _flaky_graph(world)
    assert _drive(world, graph) == 1
    state = json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert state["stages"]["one"]["status"] == "ok"
    assert state["stages"]["long"]["status"] == "failed"
    one_started = state["stages"]["one"]["started_at"]
    assert _drive(world, graph) == 0
    state = json.loads((world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert (
        state["stages"]["one"]["started_at"] == one_started
    )  # never restarted from zero
    long = state["stages"]["long"]
    assert long["resumed"] is True and long["attempt"] == 2
    assert long["seconds_total"] >= long["seconds"]
    assert (world["cycle"] / "long.txt").read_text(
        encoding="utf-8"
    ) == "xx"  # continued, not moved aside
    assert not list(world["cycle"].glob("long.txt.prev-*"))
    assert [p.name for p in (world["cycle"] / "logs").glob("long.*.log")] == [
        "long.1.log",
        "long.2.log",
    ]


def test_a_killed_stage_is_marked_interrupted_and_resumed_not_restarted(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _flaky_graph(world)
    assert _drive(world, graph, "--through", "one") == 0
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state_path = world["cycle"] / driver.STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["stages"]["long"] = {
        "status": "running",
        "pid": dead.pid,
        "process_started": None,
        "code_commit": state["stages"]["one"]["code_commit"],
        "attempt": 1,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (world["cycle"] / "logs" / "long.1.log").write_text("killed", encoding="utf-8")
    (world["cycle"] / "long.txt").write_text(
        "x", encoding="utf-8"
    )  # the killed attempt's partial output
    assert _drive(world, graph) == 0
    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert after["stages"]["long"]["resumed"] is True
    assert (world["cycle"] / "long.txt").read_text(encoding="utf-8") == "xx"
    assert after["stages"]["one"]["started_at"] == state["stages"]["one"]["started_at"]


def test_a_non_resumable_stage_that_failed_is_rerun_alone_with_outputs_moved_aside(
    world: dict[str, Any],  # noqa: F811
) -> None:
    stages = [
        dict(CHAIN[0]),
        {
            "id": "plain",
            "requires": ["one"],
            "outputs": ["{cycle}/plain.txt"],
            "argv": ["{python}", "-c", ONCE, "{cycle}/plain.txt", "{cycle}/marker"],
        },
    ]
    graph = _graph(world, stages)
    assert _drive(world, graph) == 1
    assert _drive(world, graph) == 0
    assert (world["cycle"] / "plain.txt").read_text(encoding="utf-8") == "done"
    assert len(list(world["cycle"].glob("plain.txt.prev-*"))) == 1


# -- PID discipline ------------------------------------------------------------------------


def test_the_driver_records_the_pid_it_launched_and_that_pid_is_the_stages_process(
    world: dict[str, Any],  # noqa: F811
) -> None:
    stage = {
        "id": "pid",
        "outputs": ["{cycle}/pid.txt"],
        "argv": [
            "{python}",
            "-c",
            "import os,sys;open(sys.argv[1],'w').write(f'{os.getpid()} {os.getppid()}')",
            "{cycle}/pid.txt",
        ],
    }
    assert _drive(world, _graph(world, [stage])) == 0
    record = json.loads(
        (world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8")
    )["stages"]["pid"]
    # the venv launcher on Windows is the recorded process and the interpreter its child: the
    # recorded pid is the stage's own pid or its parent, and kill-tree takes both
    own, parent = (world["cycle"] / "pid.txt").read_text(encoding="utf-8").split()
    assert str(record["pid"]) in (own, parent)
    assert record["process_started"] or os.name != "nt"


def test_stop_kills_only_the_recorded_process_and_a_live_stage_blocks_a_second_run(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _graph(world, CHAIN)
    mine = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    try:
        world["cycle"].mkdir(parents=True, exist_ok=True)
        (world["cycle"] / driver.STATE_NAME).write_text(
            json.dumps(
                {
                    "stages": {
                        "one": {
                            "status": "running",
                            "pid": mine.pid,
                            "process_started": process_started(mine.pid),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert _drive(world, graph) == 2  # refuses while the recorded stage is alive
        assert (
            driver.main(
                ["stop", "--config", str(world["config"]), "--graph", str(graph)]
            )
            == 0
        )
        deadline = time.monotonic() + 15
        while mine.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert mine.poll() is not None
        assert other.poll() is None and process_alive(
            other.pid, None
        )  # not ours: untouched
        state = json.loads(
            (world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8")
        )
        assert state["stages"]["one"]["status"] == "interrupted"
    finally:
        for child in (mine, other):
            if child.poll() is None:
                child.kill()
            child.wait()


def test_stop_does_not_kill_a_reused_pid_whose_start_token_differs(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _graph(world, CHAIN)
    bystander = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"])
    try:
        world["cycle"].mkdir(parents=True, exist_ok=True)
        (world["cycle"] / driver.STATE_NAME).write_text(
            json.dumps(
                {
                    "stages": {
                        "one": {
                            "status": "running",
                            "pid": bystander.pid,
                            "process_started": "0",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert (
            driver.main(
                ["stop", "--config", str(world["config"]), "--graph", str(graph)]
            )
            == 0
        )
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


# -- wall time and work counters ----------------------------------------------------------


def test_stage_wall_time_and_work_counters_are_recorded(world: dict[str, Any]) -> None:  # noqa: F811
    stage = {
        "id": "fits",
        "outputs": ["{cycle}/fits.txt"],
        "counter_keys": ["model_fit_calls", "receipt_reuse_count"],
        "argv": [
            "{python}",
            "-c",
            "import sys;open(sys.argv[1],'w');print('{\"model_fit_calls\": 5324, \"receipt_reuse_count\": 0}')",
            "{cycle}/fits.txt",
        ],
    }
    assert _drive(world, _graph(world, [stage])) == 0
    record = json.loads(
        (world["cycle"] / driver.STATE_NAME).read_text(encoding="utf-8")
    )["stages"]["fits"]
    assert record["work_counters"] == {
        "model_fit_calls": 5324,
        "receipt_reuse_count": 0,
    }
    assert record["seconds"] >= 0 and record["seconds_total"] >= record["seconds"]
    lines = (
        (world["cycle"] / driver.TIMING_NAME).read_text(encoding="utf-8").splitlines()
    )
    assert [line.split()[1:3] for line in lines] == [["fits", "start"], ["fits", "ok"]]
    assert "model_fit_calls" in lines[-1]
    assert (
        driver.main(
            [
                "status",
                "--config",
                str(world["config"]),
                "--graph",
                str(world["tmp"] / "graph.json"),
            ]
        )
        == 0
    )


# -- check and gate-commit ----------------------------------------------------------------


def test_check_reports_leftovers_and_stale_outputs_without_changing_anything(
    world: dict[str, Any],  # noqa: F811
) -> None:
    graph = _graph(world, CHAIN)
    cycle = world["cycle"]
    cycle.mkdir(parents=True, exist_ok=True)
    args = ["check", "--config", str(world["config"]), "--graph", str(graph)]
    assert driver.main(args) == 0
    (cycle / "two.txt").write_text("from an earlier pass", encoding="utf-8")
    before = sorted(p.name for p in cycle.iterdir())
    assert driver.main(args) == 2  # `two` would start on a leftover output
    assert sorted(p.name for p in cycle.iterdir()) == before
    (cycle / "two.txt").unlink()
    assert _drive(world, graph) == 0
    assert driver.main(args) == 0
    (cycle / "one.txt").write_text("edited", encoding="utf-8")
    assert driver.main(args) == 2


def test_gate_commit_opens_only_for_the_confirmed_release_and_never_runs_the_commit(
    world: dict[str, Any],  # noqa: F811
) -> None:
    gate = _gate_world(world)
    base = [
        "gate-commit",
        "--config",
        str(world["config"]),
        "--graph",
        str(gate["graph"]),
    ]
    assert driver.main(base) == 2
    assert driver.main([*base, "--confirm-release-id", "R2"]) == 2
    assert driver.main([*base, "--confirm-release-id", "R1"]) == 0
    assert not (world["cycle"] / "committed.txt").exists()


def test_gate_commit_defers_the_replica_receipt_to_the_replica_verifier(
    world: dict[str, Any],  # noqa: F811
) -> None:
    tools = world["repo"] / "tools"
    tools.mkdir()
    (tools / "release_replica.py").write_text(
        "import sys\n"
        "r = sys.argv[sys.argv.index('--receipt') + 1]\n"
        "ok = 'PASSED' in open(r).read()\n"
        "print('' if ok else 'replica receipt: commit step missing', file=sys.stderr)\n"
        "sys.exit(0 if ok else 1)\n",
        encoding="utf-8",
    )
    _git(world["repo"], "add", "tools")
    _git(world["repo"], "commit", "-q", "-m", "verifier")
    _config(world, code_commit=_git(world["repo"], "rev-parse", "HEAD"))
    _gate_world(world)
    stage = {
        "id": "commit",
        "commit_gate": {
            "spec": "{cycle}/spec.json",
            "replica_receipt": "{cycle}/replica-receipt.json",
            "replica_verify": [
                "{python}",
                "{repo_root}/tools/release_replica.py",
                "verify",
                "--receipt",
                "{cycle}/replica-receipt.json",
            ],
            "pointer_snapshot": "{cycle}/after.json",
        },
        "outputs": ["{cycle}/committed.txt"],
        "argv": _write_stage("{cycle}/committed.txt"),
    }
    graph = _graph(
        world,
        [stage],
        derive={
            "release_id": {"json_field": ["{cycle}/execute-stage.json", "release_id"]}
        },
    )
    base = [
        "gate-commit",
        "--config",
        str(world["config"]),
        "--graph",
        str(graph),
        "--confirm-release-id",
        "R1",
    ]
    receipt = world["cycle"] / "replica-receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    assert (
        driver.main(base) == 2
    )  # the verifier rejects it, whatever the driver's own checks say
    receipt.write_text("PASSED", encoding="utf-8")
    assert driver.main(base) == 0


# -- offline plan against the recorded pass-26 command lines ----------------------------------


def _offline(*extra: str, recorded: Path | None = None) -> int:
    return driver.main(
        [
            "plan",
            "--offline",
            "--config",
            str(FIXTURES / "pass26-run.json"),
            "--ids",
            str(FIXTURES / "pass26-ids.json"),
            "--recorded",
            str(recorded or FIXTURES / "pass26-commands.json"),
            "--through",
            "lanes",
            *extra,
        ]
    )


def test_the_offline_plan_reproduces_every_recorded_pass_26_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorded = json.loads(
        (FIXTURES / "pass26-commands.json").read_text(encoding="utf-8")
    )
    assert _offline() == 0
    printed = capsys.readouterr().out
    for stage_id in recorded["stages"]:
        assert f"\n{stage_id}: " in "\n" + printed, stage_id
    assert len(recorded["stages"]) == 16


def test_the_offline_plan_reads_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    opened: list[str] = []
    real_open = Path.read_bytes

    def spy(self: Path) -> bytes:
        opened.append(self.name)
        return real_open(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    assert _offline() == 0
    assert sorted(set(opened)) == [
        "pass26-commands.json",
        "pass26-ids.json",
        "pass26-run.json",
        "release-stages.json",
        "release-stages.schema.json",
    ]
    assert list(tmp_path.iterdir()) == []


def _mutated(tmp_path: Path, stage: str, mutate: Any) -> Path:
    recorded = json.loads(
        (FIXTURES / "pass26-commands.json").read_text(encoding="utf-8")
    )
    mutate(recorded["stages"][stage])
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps(recorded), encoding="utf-8")
    return path


def test_the_offline_plan_names_a_flag_that_differs_from_the_recorded_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def drop(row: dict[str, Any]) -> None:
        i = row["argv"].index("--retained-cache-base")
        del row["argv"][i : i + 2]

    assert _offline(recorded=_mutated(tmp_path, "attended", drop)) == 2
    assert (
        "attended: --retained-cache-base: only in the plan" in capsys.readouterr().err
    )

    def stale(row: dict[str, Any]) -> None:
        row["argv"][row["argv"].index("--cutoff") + 1] = "2026-09-10"

    assert _offline(recorded=_mutated(tmp_path, "lanes", stale)) == 2

    def explained(row: dict[str, Any]) -> None:
        drop(row)
        row["known"] = [
            {"flag": "--retained-cache-base", "reason": "documented difference"}
        ]

    assert _offline(recorded=_mutated(tmp_path, "attended", explained)) == 0


def test_the_offline_plan_omits_the_cache_base_when_the_run_has_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = json.loads((FIXTURES / "pass26-run.json").read_text(encoding="utf-8"))
    del config["retained_cache_base"]
    path = tmp_path / "run.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    code = driver.main(
        [
            "plan",
            "--offline",
            "--config",
            str(path),
            "--ids",
            str(FIXTURES / "pass26-ids.json"),
            "--only",
            "attended",
        ]
    )
    assert code == 0 and "--retained-cache-base" not in capsys.readouterr().out


def test_every_stage_of_the_shipped_graph_is_planned_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = driver.main(
        [
            "plan",
            "--offline",
            "--config",
            str(FIXTURES / "pass26-run.json"),
            "--ids",
            str(FIXTURES / "pass26-ids.json"),
            "--through",
            "pack-evidence",
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    # a derived id the fixture does not give would print as <name>
    assert set(re.findall(r"<(\w+)>", printed)) <= {"release_id", "serial_release_id"}


def test_the_stage_order_is_the_order_pass_26_ran_them() -> None:
    # chain_p26.sh: resume, tail1 (2a probe recipe, 2b registries, 2c recipe, 3 spec, 4 semantic,
    # 5a financial artifacts), provider generations, production spec, replica copy, replica spec,
    # lanes; before it run_attended.sh, qa_only.sh and the pre-chain
    pass26 = [
        "pre-chain",
        "attended",
        "qa-browser",
        "qa-interaction",
        "qa-native",
        "resume",
        "recipe-probe",
        "registries",
        "recipe",
        "pipeline-spec",
        "semantic",
        "financial-artifacts",
        "provider-generations",
        "production-spec",
        "replica-copy",
        "replica-provider-generations",
        "replica-spec",
        "lanes",
    ]
    ids = [stage["id"] for stage in _shipped()["stages"]]
    assert [i for i in ids if i in pass26] == pass26
    # a default run covers exactly that path, then stops before the explicit commit
    assert [i for i in ids if not _stage(i).get("explicit")] == [
        "selected-inputs",
        *pass26,
    ]
    assert ids[-2:] == ["commit", "pack-evidence"]


def _stage(stage_id: str) -> dict[str, Any]:
    return next(s for s in _shipped()["stages"] if s["id"] == stage_id)


def test_an_operator_script_outside_the_repo_runs_only_at_its_pinned_sha256(
    world: dict[str, Any],  # noqa: F811
) -> None:
    import hashlib

    script = world["tmp"] / "ops" / "pre.py"
    script.parent.mkdir()
    script.write_text("open(__import__('sys').argv[1], 'w')\n", encoding="utf-8")
    stage = {
        "id": "one",
        "outputs": ["{cycle}/one.txt"],
        "pinned_script": ["{pre_script}", "{pre_sha}"],
        "argv": ["{python}", "{pre_script}", "{cycle}/one.txt"],
    }
    graph = _graph(world, [stage])
    _config(world, pre_script=str(script), pre_sha="0" * 64)
    assert _drive(world, graph) == 2
    _config(world, pre_sha=hashlib.sha256(script.read_bytes()).hexdigest())
    assert _drive(world, graph) == 0
    script.write_text("# edited after pinning\n", encoding="utf-8")
    assert _drive(world, graph, "--only", "one", "--replace") == 2

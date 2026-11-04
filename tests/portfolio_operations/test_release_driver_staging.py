"""RELEASE-DRIVER-001 block B8: the staging group of the shipped stage graph.

The graph starts at ``cycle-resolve`` and stages provenance, the ledger and the baseline from specs the
committed builders write, before ``selected-inputs`` and the pre-chain. These tests read the shipped graph
and the committed tools' own ``--help`` (no private import): every flag a stage passes is one its tool
declares, every flag the tool requires is passed, the stages are ordered and closed under ``requires``,
the offline plan prints them, and the real ``cycle-resolve``, ``ledger-spec`` and ``ledger`` stages run
over the synthetic seven-provider world the ledger-spec end-to-end test builds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_driver as driver  # noqa: E402
import pass26_commands  # noqa: E402
from test_release_driver import _git, world  # noqa: E402,F401  (pytest fixture)
from test_release_ledger_spec_e2e import real_cycle  # noqa: E402

SHIPPED = REPO_ROOT / "ops" / "mvp2-release" / "release-stages.json"
STAGING = (
    "cycle-resolve",
    "provenance",
    "ledger-spec",
    "ledger",
    "baseline-spec",
    "baseline",
)
# stages whose tool and flags are checked against the tool's own --help
TOOL_STAGES = (
    *STAGING,
    "selected-build",
    "pre-chain",
    "calendar-authority",
    "prep-spec",
    "post-commit-verify",
)
NEW_CONFIG_KEYS = (
    "predecessor_selected_inputs",
    "predecessor_selected_inputs_sha256",
    "predecessor_prep_spec",
    "predecessor_prep_spec_sha256",
    "rebind_cutoff",
    "selected_files",
    "factors_bundle",
    "sector_bundle",
    "style_bundle",
    "factors_bundle_id",
    "sector_bundle_id",
    "style_bundle_id",
    "weekly_gap_fill_spec",
    "vendor_gap_fills",
    "model_vs_actual_input",
    "prep_pins",
    "prep_dirs",
    "prep_omit",
    "cycle_manifest",
    "cycle_manifest_sha256",
    "cycle_roots_file",
    "provenance_spec",
    "provenance_spec_sha256",
    "predecessor_ledger_spec",
    "predecessor_ledger_spec_sha256",
    "release_inputs_root",
    "release_inputs_manifest_sha256",
    "baseline_roots",
    "baseline_declared_inputs_path",
    "baseline_cost_root_id",
    "calendar_attempt_run",
    "calendar_attempt_spec",
    "calendar_seed_authority",
    "calendar_seed_authority_sha256",
)


def _shipped() -> dict[str, Any]:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _stage(stage_id: str) -> dict[str, Any]:
    return next(s for s in _shipped()["stages"] if s["id"] == stage_id)


@pytest.fixture
def pass26(tmp_path: Path) -> Path:
    pass26_commands.write(tmp_path)
    return tmp_path


# -- the flags of every stage tool are the tool's own ---------------------------------------


def _tokens(stage: dict[str, Any], *, optional: bool) -> list[str]:
    out: list[str] = []
    for item in stage["argv"]:
        if isinstance(item, str):
            out.append(item)
        elif optional:
            out.extend(item["args"])
    return out


def _help(script: str, subcommand: list[str]) -> str:
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / script), *subcommand, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (script, done.stderr[-300:])
    return done.stdout


def _declared_flags(text: str) -> set[str]:
    """Flags the tool lists in its options section (not the ones its prose mentions)."""

    return set(re.findall(r"^ {2}(?:-\w, )?(--[\w-]+)", text, flags=re.MULTILINE))


def _required_flags(text: str) -> set[str]:
    """Flags that sit outside every bracket in the usage line: the tool refuses without them."""

    usage = text.split("\n\n", 1)[0]
    depth, plain = 0, []
    for char in usage:
        depth += char == "["
        plain.append(char if depth == 0 else " ")
        depth -= char == "]"
    return set(re.findall(r"--[\w-]+", "".join(plain)))


@pytest.mark.parametrize("stage_id", TOOL_STAGES)
def test_every_flag_a_stage_passes_is_one_its_tool_declares_and_every_required_one_is_passed(
    stage_id: str,
) -> None:
    stage = _stage(stage_id)
    tokens = _tokens(stage, optional=True)
    assert tokens[0] == "{python}"
    match = re.fullmatch(r"\{tools\}/([\w.]+\.py)", tokens[1])
    assert match is not None, tokens[1]
    script = match.group(1)
    assert (REPO_ROOT / "tools" / script).is_file()
    subcommand = [tokens[2]] if not tokens[2].startswith(("--", "{")) else []
    text = _help(script, subcommand)
    used = {token for token in tokens if token.startswith("--")}
    assert used <= _declared_flags(text), sorted(used - _declared_flags(text))
    always = {
        token for token in _tokens(stage, optional=False) if token.startswith("--")
    }
    assert _required_flags(text) <= always, sorted(_required_flags(text) - always)


def test_the_flag_reader_tells_required_from_optional() -> None:
    text = "usage: t.py [-h] --need A [--maybe B] [--group [--inner X]] --also C\n\noptions:\n  -h, --help  x\n  --need A\n"
    assert _required_flags(text) == {"--need", "--also"}
    assert _declared_flags(text) == {"--help", "--need"}


# -- order and closure ---------------------------------------------------------------------


def _closure(stage_id: str) -> set[str]:
    stages = {s["id"]: s for s in _shipped()["stages"]}
    seen: set[str] = set()
    todo = [stage_id]
    while todo:
        for required in stages[todo.pop()].get("requires", []):
            if required not in seen:
                seen.add(required)
                todo.append(required)
    return seen


def test_the_staging_group_comes_first_in_the_order_stage_p23_ran_it() -> None:
    ids = [s["id"] for s in _shipped()["stages"]]
    # stage_p23.sh: provenance, ledger, baseline, then selected inputs and the pre-chain; each spec
    # builder runs just before the stage tool that reads its output
    assert ids[:9] == [*STAGING, "selected-build", "selected-inputs", "pre-chain"]
    # the calendar authority comes before the preparation spec that pins it, the spec before the run
    assert ids[ids.index("pre-chain") + 1 : ids.index("attended") + 1] == [
        "calendar-authority",
        "prep-spec",
        "attended",
    ]
    assert "prep-spec" in _closure("attended") and "pre-chain" in _closure("prep-spec")
    assert "selected-build" in _closure("pre-chain")
    for earlier, later in zip(STAGING, STAGING[1:]):
        assert earlier in _closure(later), (earlier, later)
    assert set(STAGING) <= _closure("selected-inputs")
    assert set(STAGING) <= _closure("pre-chain")


def test_a_stage_requires_every_stage_whose_output_it_reads() -> None:
    graph = _shipped()
    names = {**graph["paths"]}

    def reaches(template: str) -> set[str]:
        out: set[str] = set()
        todo = re.findall(r"\{(\w+)\}", template)
        while todo:
            name = todo.pop()
            if name in out:
                continue
            out.add(name)
            todo += re.findall(r"\{(\w+)\}", names.get(name, ""))
        return out

    produced: dict[str, str] = {}
    for stage in graph["stages"]:
        for output in stage.get("outputs", []):
            for name in re.findall(r"\{(\w+)\}", output):
                if name in names or name == "resolved_cycle":
                    produced.setdefault(name, stage["id"])
    assert {
        "resolved_cycle",
        "prov",
        "ledger_spec",
        "ledger_out",
        "baseline_spec",
    } <= set(produced)
    for stage_id in (
        *STAGING,
        "selected-build",
        "prep-spec",
        "attended",
        "calendar-authority",
    ):
        stage = _stage(stage_id)
        used: set[str] = set()
        for template in [
            *stage.get("inputs", []),
            *[t for t in _tokens(stage, optional=True)],
        ]:
            used |= reaches(template)
        for name, producer in produced.items():
            # the calendar authority is an explicit stage's output the run pins by sha256 (the seed of a
            # first attempt is operator-supplied), so no stage requires it
            if name == "calendar_authority":
                continue
            if name in used and producer != stage_id:
                assert producer in _closure(stage_id), (stage_id, name, producer)


def test_the_calendar_authority_is_an_explicit_stage_nothing_requires() -> None:
    stage = _stage("calendar-authority")
    assert stage["explicit"] is True
    assert not [
        s["id"]
        for s in _shipped()["stages"]
        if "calendar-authority" in s.get("requires", [])
    ]
    # a default run never authors it (it needs a first attempt's staged run): see the offline plan test
    assert "{calendar_authority}" in stage["outputs"]
    assert "{calendar_authority}.receipt.json" in stage["outputs"]


def test_staging_stages_write_only_under_the_cycle_and_in_every_ceremony(
    pass26: Path,
) -> None:
    config = json.loads((pass26 / "pass26-run.json").read_text(encoding="utf-8"))
    run = driver.Run(config, _shipped(), offline=True)
    cycle = Path(config["cycle"])
    for stage_id in (*STAGING, "selected-build", "prep-spec", "calendar-authority"):
        stage = _stage(stage_id)
        assert "only_ceremony" not in stage and "only_mode" not in stage
        written = driver._claimed_outputs(run, stage)
        assert written
        for path in written:
            assert driver._inside(path, cycle), (stage_id, path)


def test_the_new_config_keys_are_documented() -> None:
    text = (REPO_ROOT / "docs" / "release-driver.md").read_text(encoding="utf-8")
    for key in NEW_CONFIG_KEYS:
        assert f"`{key}`" in text, key


def test_the_pre_chain_runs_the_committed_tool_not_a_pinned_script() -> None:
    stage = _stage("pre-chain")
    assert "pinned_script" not in stage
    assert stage["argv"][1] == "{tools}/release_pre_chain.py"
    assert not any("pinned_script" in s for s in _shipped()["stages"])


# -- offline plan --------------------------------------------------------------------------


def _plan(directory: Path, *extra: str) -> tuple[int, str]:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = driver.main(
            [
                "plan",
                "--offline",
                "--config",
                str(directory / "pass26-run.json"),
                "--ids",
                str(directory / "pass26-ids.json"),
                *extra,
            ]
        )
    return code, out.getvalue()


def test_the_offline_plan_prints_every_staging_stage_without_reading_the_run(
    pass26: Path,
) -> None:
    code, printed = _plan(pass26, "--through", "pre-chain")
    assert code == 0
    lines = {line.split(":", 1)[0]: line for line in printed.splitlines()}
    for stage_id in (*STAGING, "selected-inputs", "pre-chain"):
        assert stage_id in lines, stage_id
    assert "<" not in printed
    # the builders get ids and paths from the configuration and the cycle, never from a typed id
    assert "--out-root" in lines["ledger-spec"]
    assert "--root baseline-sources=" in lines["baseline-spec"]
    assert "--provenance-generation" in lines["baseline-spec"]
    code, printed = _plan(pass26, "--only", "calendar-authority")
    assert code == 0 and "release_hwm_calendar.py author" in printed
    ids = json.loads((pass26 / "pass26-ids.json").read_text(encoding="utf-8"))
    assert f"--resolved-cycle-sha256 {ids['resolved_cycle_sha256']}" in printed


def test_a_default_offline_plan_leaves_the_explicit_calendar_stage_out(
    pass26: Path,
) -> None:
    code, printed = _plan(pass26, "--through", "lanes")
    assert code == 0 and not any(
        line.startswith("calendar-authority:") for line in printed.splitlines()
    )


def test_optional_groups_are_omitted_when_the_run_does_not_configure_them(
    pass26: Path,
) -> None:
    config = json.loads((pass26 / "pass26-run.json").read_text(encoding="utf-8"))
    del config["baseline_roots"]
    (pass26 / "pass26-run.json").write_text(json.dumps(config), encoding="utf-8")
    code, printed = _plan(pass26, "--only", "baseline-spec")
    assert (
        code == 0
        and "--root" not in printed
        and "--declared-inputs-path" not in printed
    )
    config["baseline_declared_inputs_path"] = "x/declared.json"
    config["baseline_cost_root_id"] = "cost-x"
    config["baseline_roots"] = {"b": "{cycle}/in/b", "a": "/tmp/a"}
    (pass26 / "pass26-run.json").write_text(json.dumps(config), encoding="utf-8")
    code, printed = _plan(pass26, "--only", "baseline-spec")
    assert "--declared-inputs-path x/declared.json" in printed
    assert "--cost-root-id cost-x" in printed
    # one group per entry, in key order, templates expanded
    assert printed.index("--root a=/tmp/a") < printed.index("--root b=C:/Dev/scratch")


def test_the_offline_plan_binds_no_release_inputs_and_shows_where_they_are(
    pass26: Path,
) -> None:
    code, printed = _plan(pass26, "--only", "lanes")
    assert code == 0
    assert "/in/release-inputs/acceptance/expected-baseline.json" in printed.replace(
        "\\", "/"
    )


# -- driver mechanics the stages use ------------------------------------------------------


def test_for_each_refuses_a_map_that_is_not_an_object(world: dict[str, Any]) -> None:  # noqa: F811
    config = json.loads(world["config"].read_text(encoding="utf-8"))
    config["baseline_roots"] = "a"
    run = driver.Run(config, {"paths": {}, "derive": {}, "stages": []})
    stage = {
        "argv": [
            "{python}",
            "x.py",
            {
                "for_each": "baseline_roots",
                "args": ["--root", "{each_key}={each_value}"],
            },
        ]
    }
    with pytest.raises(driver.Refusal):
        driver.stage_argv(run, stage)


def test_file_sha256_derives_the_digest_of_a_file_an_earlier_stage_wrote(
    world: dict[str, Any],  # noqa: F811
) -> None:
    from test_release_driver import _drive, _graph, _write_stage

    target = world["tmp"] / "out.txt"
    graph = _graph(
        world,
        [
            {
                "id": "one",
                "outputs": [str(target)],
                "argv": _write_stage(str(target), "abc"),
            },
            {
                "id": "two",
                "requires": ["one"],
                "outputs": [str(world["tmp"] / "two.txt")],
                "argv": [
                    "{python}",
                    "-c",
                    "import sys;open(sys.argv[1],'w').write(sys.argv[2])",
                    str(world["tmp"] / "two.txt"),
                    "{out_sha}",
                ],
            },
        ],
        derive={"out_sha": {"file_sha256": str(target)}},
    )
    assert _drive(world, graph) == 0
    assert (world["tmp"] / "two.txt").read_text(encoding="utf-8") == hashlib.sha256(
        b"abc"
    ).hexdigest()


# -- the real stages over the synthetic seven-provider world ------------------------------


def _copy_repo(destination: Path) -> None:
    """A committed copy of the files the staging tools read from their checkout."""

    def ignore(_: str, names: list[str]) -> list[str]:
        return [n for n in names if n in ("__pycache__", ".pytest_cache")]

    for relative in (
        "tools",
        "schemas",
        "contracts",
        "docs/analytics",
        "docs/decisions",
        "acceptance",
    ):
        source = REPO_ROOT / relative
        if source.is_dir():
            shutil.copytree(source, destination / relative, ignore=ignore)
    shutil.copy2(REPO_ROOT / ".gitignore", destination / ".gitignore")
    graph = destination / "ops" / "mvp2-release"
    graph.mkdir(parents=True)
    _git(destination, "init", "-q")
    _git(destination, "add", "-A")
    _git(destination, "commit", "-q", "-m", "frozen")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    base = tmp_path_factory.mktemp("staging")
    # the contract validator reads the accepted history commit with `git show`: the copy borrows the
    # objects of this checkout instead of cloning it
    objects = _git(
        REPO_ROOT, "rev-parse", "--path-format=absolute", "--git-path", "objects"
    )
    patch = pytest.MonkeyPatch()
    patch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", objects)
    # the stage tools run in the copy: they must leave its tree clean (no bytecode)
    patch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    cycle_world = real_cycle(base / "world")
    repo = base / "repo"
    repo.mkdir()
    _copy_repo(repo)
    cycle = cycle_world["out"]
    roots_file = base / "roots.json"
    roots_file.write_text(
        json.dumps({key: str(path) for key, path in cycle_world["roots"].items()}),
        encoding="utf-8",
    )
    prov_spec = base / "provenance-spec.json"
    prov_spec.write_text("{}", encoding="utf-8")
    graph = _shipped()
    # the provenance stage reads a hand-made spec (no committed builder): stand in for the staging
    # tool by placing the generation the ledger-spec fixture staged, where the stage's output lies
    for stage in graph["stages"]:
        if stage["id"] == "provenance":
            stage["argv"] = [
                "{python}",
                "-c",
                "import shutil,sys,pathlib;s=pathlib.Path(sys.argv[1]);"
                "shutil.copytree(s,pathlib.Path(sys.argv[2])/'generations'/s.name)",
                str(cycle_world["provenance"]),
                "{prov}",
            ]
    graph_path = base / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    config = {
        "repo_root": str(repo),
        "python": sys.executable,
        "cycle": str(cycle),
        "run": str(cycle / "out"),
        "code_commit": _git(repo, "rev-parse", "HEAD"),
        "mode": "release",
        "ceremony": "full",
        "requested_as_of": cycle_world["manifest"]["cutoff"],
        "evaluated_on": cycle_world["manifest"]["evaluated_on"],
        "cycle_manifest": str(cycle_world["manifest_path"]),
        "cycle_manifest_sha256": _sha(cycle_world["manifest_path"]),
        "cycle_roots_file": str(roots_file),
        "resolved_cycle": str(cycle / "in" / "resolved-cycle.json"),
        "provenance_spec": str(prov_spec),
        "provenance_spec_sha256": _sha(prov_spec),
        "predecessor_ledger_spec": str(cycle_world["predecessor_path"]),
        "predecessor_ledger_spec_sha256": _sha(cycle_world["predecessor_path"]),
    }
    config_path = base / "run.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    try:
        yield {
            "base": base,
            "cycle": cycle,
            "graph": graph_path,
            "config": config_path,
            "values": config,
            "world": cycle_world,
        }
    finally:
        patch.undo()


def _run(staged: dict[str, Any], *args: str) -> int:
    return driver.main(
        [
            args[0],
            "--config",
            str(staged["config"]),
            "--graph",
            str(staged["graph"]),
            *args[1:],
        ]
    )


def test_a_wrong_cycle_manifest_pin_is_refused_before_the_resolver_runs(
    staged: dict[str, Any],
) -> None:
    config = dict(staged["values"], cycle_manifest_sha256="0" * 64)
    path = staged["base"] / "run-bad-pin.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    code = driver.main(
        [
            "run",
            "--config",
            str(path),
            "--graph",
            str(staged["graph"]),
            "--only",
            "cycle-resolve",
        ]
    )
    assert code == 2
    assert not (staged["cycle"] / "in" / "resolved-cycle.json").exists()


def test_the_real_cycle_resolve_ledger_spec_and_ledger_stages_run_in_order(
    staged: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(staged, "check", "--through", "ledger") == 0
    assert _run(staged, "run", "--through", "ledger") == 0
    out = capsys.readouterr().out
    for stage_id in ("cycle-resolve", "provenance", "ledger-spec", "ledger"):
        assert f"{stage_id}: ok" in out, out
    cycle = staged["cycle"]
    resolved = json.loads(
        (cycle / "in" / "resolved-cycle.json").read_text(encoding="utf-8")
    )
    assert (
        resolved["cycle_manifest_sha256"] == staged["values"]["cycle_manifest_sha256"]
    )
    spec = json.loads((cycle / "ledger-spec.json").read_text(encoding="utf-8"))
    provenance = next((cycle / "prov" / "generations").iterdir())
    assert spec["provenance"]["generation_id"] == provenance.name
    (generation,) = list((cycle / "ledger" / "generations").iterdir())
    assert (generation / "provider-ledger.json").is_file()
    # the next stage derives its ids from what these wrote and refuses a spec that names another
    state = json.loads((cycle / driver.STATE_NAME).read_text(encoding="utf-8"))
    assert [
        state["stages"][i]["status"]
        for i in ("cycle-resolve", "provenance", "ledger-spec", "ledger")
    ] == ["ok"] * 4
    # a second run keeps them; a changed input of a finished stage is refused
    assert _run(staged, "run", "--through", "ledger") == 0
    roots = Path(staged["values"]["cycle_roots_file"])
    original = roots.read_bytes()
    roots.write_bytes(original + b"\n")
    try:
        assert _run(staged, "run", "--through", "ledger") == 2
    finally:
        roots.write_bytes(original)


def test_a_ledger_spec_that_names_another_provenance_generation_is_refused(
    staged: dict[str, Any],
) -> None:
    assert _run(staged, "run", "--through", "ledger") == 0
    run = driver.load_run(staged["config"], staged["graph"])
    stage = run.stage("ledger")
    driver.check_bound_ids(
        run, stage
    )  # the spec this run built names this run's provenance
    spec_path = staged["cycle"] / "ledger-spec.json"
    original = spec_path.read_bytes()
    provenance = run.derive("provenance_id")
    spec_path.write_bytes(original.replace(provenance.encode(), b"f" * len(provenance)))
    try:
        with pytest.raises(driver.Refusal, match="does not bind"):
            driver.check_bound_ids(run, stage)
        assert _run(staged, "run", "--only", "ledger", "--replace") == 2
    finally:
        spec_path.write_bytes(original)


# -- B4 stages: selected-inputs and the attended preparation spec -----------------------------


def test_the_offline_plan_prints_the_selected_and_prep_spec_stages(
    pass26: Path,
) -> None:
    code, printed = _plan(pass26, "--through", "attended")
    assert code == 0
    lines = {line.split(":", 1)[0]: line for line in printed.splitlines()}
    selected, prep = lines["selected-build"], lines["prep-spec"]
    ids = json.loads((pass26 / "pass26-ids.json").read_text(encoding="utf-8"))
    config = json.loads((pass26 / "pass26-run.json").read_text(encoding="utf-8"))
    # the frozen code identity is passed in, never read from git by the tool
    assert f"--commit {config['code_commit']}" in selected
    for flag, value in (
        ("--baseline-id", ids["baseline_id"]),
        ("--history-id", ids["history_id"]),
        ("--attribution-id", ids["attribution_id"]),
    ):
        assert f"{flag} {value}" in prep
    assert "--calendar-authority" in prep and "--selected-inputs" in prep
    # optional inputs are absent unless the run names them
    for absent in ("--pin", "--dir", "--omit", "--factors-bundle", "--rebind-cutoff"):
        assert absent not in prep and absent not in selected


def test_optional_prep_spec_inputs_become_arguments_when_configured(
    pass26: Path,
) -> None:
    config = json.loads((pass26 / "pass26-run.json").read_text(encoding="utf-8"))
    config.update(
        {
            "weekly_gap_fill_spec": "data:gap/weekly.json@" + "a" * 64,
            "vendor_gap_fills": "data:gap/vendor.json@" + "b" * 64,
            "factors_bundle": "factors-x",
            "style_bundle_id": "c" * 64,
            "prep_pins": {"base_factor_file": "data:bf.json@" + "d" * 64},
            "prep_dirs": {"sleeve_facts.quantbase_source_dir": "data:qb"},
            "prep_omit": ["model_vs_actual_input", "authority_notes"],
            "rebind_cutoff": "true",
        }
    )
    (pass26 / "pass26-run.json").write_text(json.dumps(config), encoding="utf-8")
    code, printed = _plan(pass26, "--only", "prep-spec")
    assert code == 0
    assert "--pin weekly_gap_fill_spec=data:gap/weekly.json@" + "a" * 64 in printed
    assert "--pin vendor_gap_fills=data:gap/vendor.json@" + "b" * 64 in printed
    assert "--pin model_vs_actual_input" not in printed
    assert "--factors-bundle factors-x" in printed
    assert "--style-bundle-id " + "c" * 64 in printed
    assert "--pin base_factor_file=data:bf.json@" + "d" * 64 in printed
    # a directory field is replaced with --dir, never --pin (a pin turns the path into a file object)
    assert "--dir sleeve_facts.quantbase_source_dir=data:qb" in printed
    assert (
        "--omit authority_notes" in printed
        and "--omit model_vs_actual_input" in printed
    )
    assert "--rebind-cutoff" in printed


def test_selected_inputs_must_name_this_runs_ledger_generation_and_follow_its_builder() -> (
    None
):
    stage = _stage("selected-inputs")
    assert stage["requires"] == ["selected-build"]
    assert ["{selected_inputs}", "{cycle_ledger_id}"] in stage["bound_ids"]
    assert _stage("selected-build")["outputs"] == ["{selected_inputs}"]
    assert _stage("prep-spec")["outputs"] == ["{preparation_spec}"]
    # the prep spec is pinned to the calendar authority the attended run pins (same key, same pin)
    pins = [tuple(item) for item in _stage("prep-spec")["pinned_inputs"]]
    assert ("{calendar_authority}", "{calendar_authority_sha256}") in pins
    assert ("{calendar_authority}", "{calendar_authority_sha256}") in [
        tuple(item) for item in _stage("attended")["pinned_inputs"]
    ]


def test_a_stage_refuses_a_release_inputs_set_lacking_a_file_it_reads(
    world: dict[str, Any],  # noqa: F811
    tmp_path: Path,
) -> None:
    from quant_engine.core.release_inputs import build_manifest

    root = tmp_path / "ri"
    baseline = root / driver.BASELINE_INPUT
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}\n", encoding="utf-8")
    config = json.loads(world["config"].read_text(encoding="utf-8"))
    _, pin = build_manifest(root, [driver.BASELINE_INPUT], cutoff="2026-09-17")
    config.update(
        release_inputs_root=str(root),
        release_inputs_manifest_sha256=pin,
        requested_as_of="2026-09-17",
    )
    run = driver.Run(config, {"paths": {}, "derive": {}, "stages": []})
    stage = {"id": "prep-spec", "release_inputs": ["ops/mvp2-release/inputs/x.json"]}
    with pytest.raises(driver.Refusal, match="prep-spec: .*lacks .*inputs/x.json"):
        driver.check_release_inputs(run, stage)
    driver.check_release_inputs(
        run, {"id": "ok", "release_inputs": [driver.BASELINE_INPUT]}
    )


def test_the_shipped_staging_stages_name_the_release_inputs_they_read() -> None:
    graph = json.loads(driver.GRAPH_DEFAULT.read_bytes())
    named = {s["id"]: s.get("release_inputs", []) for s in graph["stages"]}
    assert (
        "ops/mvp2-release/inputs/baseline/baseline-declared-inputs.json"
        in named["baseline-spec"]
    )
    assert "ops/mvp2-release/inputs/current-epoch-crosswalk.json" in named["prep-spec"]


def test_the_validating_stages_pass_the_basis_inputs_the_attended_run_used() -> None:
    # the findings the ceremony and the lanes gate on must be the attended run's: without the
    # declaration, facts, ledger, ibkr root and tag map a sleeve row has nothing to be checked against.
    # One optional group, present only when the run names a release-inputs root, so a run without
    # one keeps the repository-default command lines byte for byte.
    graph = json.loads(SHIPPED.read_text(encoding="utf-8"))
    paths, derive = graph["paths"], graph["derive"]
    assert paths["release_inputs_strategy_basis"].endswith(
        "/sleeves/strategy-basis-declaration.json"
    )
    assert paths["release_inputs_tag_map"].endswith(
        "/sleeves/nested-strategy-tag-map.json"
    )
    assert paths["sleeve_facts"].endswith("/input/sleeve-account-facts.json")
    assert derive["ibkr_root"]["json_field"][1] == "sleeve_facts.ibkr_generation_root"
    expected = {
        "--strategy-basis": "{release_inputs_strategy_basis}",
        "--sleeve-facts": "{sleeve_facts}",
        "--provider-ledger": "{ledger_catalog}",
        "--ibkr-root": "{ibkr_root}",
        "--tag-map": "{release_inputs_tag_map}",
    }
    stages = {s["id"]: s for s in graph["stages"]}
    for stage_id in ("findings-panel", "ceremony", "lanes"):
        groups = [
            item
            for item in stages[stage_id]["argv"]
            if isinstance(item, dict) and item.get("if") == "release_inputs_root"
        ]
        assert len(groups) == 1, stage_id
        args = groups[0]["args"]
        for flag, value in expected.items():
            assert args[args.index(flag) + 1] == value, (stage_id, flag)
        assert "release_inputs" not in stages[stage_id]


def test_the_basis_group_is_absent_without_a_release_inputs_root() -> None:
    graph = json.loads(SHIPPED.read_text(encoding="utf-8"))
    stage = next(s for s in graph["stages"] if s["id"] == "findings-panel")
    config = {
        "repo_root": "R",
        "python": "py",
        "cycle": "C",
        "run": "C/run",
        "code_commit": "c" * 40,
        "mode": "release",
        "requested_as_of": "2026-09-17",
        "ceremony": "full",
    }
    run = driver.Run(config, graph, offline=True)
    argv = driver.stage_argv(run, stage)
    assert "--strategy-basis" not in argv and "--tag-map" not in argv
    assert "--crosswalk" in argv

"""Release inputs outside Git: loader, fail-closed refusals, producer/consumer seam, default path.

Every fixture is synthetic: invented cutoff, invented bytes, no real data or ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from quant_engine.core import gate_profile as gp
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.release_inputs import (
    CODE_BOUND,
    MANIFEST_NAME,
    POLICY,
    RELEASE_INPUT,
    ReleaseInputsError,
    build_manifest,
    classify,
    load_release_inputs,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_driver as driver  # noqa: E402
import release_inputs as cli  # noqa: E402

CUTOFF = "2000-01-07"
BASELINE = "acceptance/expected-baseline.json"
POLICY_FILE = "ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json"
FILES = {
    BASELINE: json.dumps({"cutoff": CUTOFF, "synthetic": True}).encode(),
    POLICY_FILE: b'{"synthetic": "policy"}\n',
    "ops/mvp2-release/inputs/hwm/calendar-authority.json": b'{"synthetic": "calendar"}\n',
}


def _write(root: Path, files: dict[str, bytes] = FILES) -> tuple[Path, str]:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest, pin = build_manifest(root, list(files), cutoff=CUTOFF)
    assert manifest.name == MANIFEST_NAME
    return manifest, pin


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "inputs"
    root.mkdir()
    return root, _write(root)[1]


def _reseal(root: Path, mutate: Any) -> str:
    """Rewrite the manifest with a valid seal; return its new pin (a tamper by a careful writer)."""

    path = root / MANIFEST_NAME
    body = json.loads(path.read_bytes())
    body.pop("self_sha256")
    mutate(body)
    body["self_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    data = (json.dumps(body, indent=1, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


# -- loader ---------------------------------------------------------------------------
def test_load_verifies_every_entry(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    loaded = load_release_inputs(root, pin)
    assert loaded.cutoff == CUTOFF
    assert loaded.paths == tuple(sorted(FILES))
    assert loaded.path(BASELINE).read_bytes() == FILES[BASELINE]
    assert loaded.sha256(BASELINE) == hashlib.sha256(FILES[BASELINE]).hexdigest()


def test_changed_file_refused(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    (root / POLICY_FILE).write_bytes(b'{"synthetic": "other"}\n')
    with pytest.raises(ReleaseInputsError, match="differs from the manifest"):
        load_release_inputs(root, pin)


def test_same_size_change_refused(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    data = bytearray(FILES[POLICY_FILE])
    data[5] ^= 1
    (root / POLICY_FILE).write_bytes(bytes(data))
    with pytest.raises(ReleaseInputsError, match="differs from the manifest"):
        load_release_inputs(root, pin)


def test_missing_file_refused(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    (root / BASELINE).unlink()
    with pytest.raises(ReleaseInputsError, match="is missing"):
        load_release_inputs(root, pin)


def test_missing_manifest_and_root_refused(
    inputs: tuple[Path, str], tmp_path: Path
) -> None:
    root, pin = inputs
    (root / MANIFEST_NAME).unlink()
    with pytest.raises(ReleaseInputsError, match="is missing"):
        load_release_inputs(root, pin)
    with pytest.raises(ReleaseInputsError, match="not a directory"):
        load_release_inputs(tmp_path / "nowhere", pin)


def test_wrong_or_malformed_pin_refused(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    with pytest.raises(ReleaseInputsError, match="differs from its pin"):
        load_release_inputs(root, "0" * 64)
    with pytest.raises(ReleaseInputsError, match="not a sha256"):
        load_release_inputs(root, "not-a-hash")


def test_manifest_edit_changes_the_pin(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    new_pin = _reseal(root, lambda body: body.update(cutoff="2000-01-14"))
    assert new_pin != pin
    with pytest.raises(ReleaseInputsError, match="differs from its pin"):
        load_release_inputs(root, pin)
    assert load_release_inputs(root, new_pin).cutoff == "2000-01-14"


def test_hand_edited_manifest_body_refused(inputs: tuple[Path, str]) -> None:
    root, _ = inputs
    path = root / MANIFEST_NAME
    body = json.loads(path.read_bytes())
    body["cutoff"] = "2000-01-14"
    data = (json.dumps(body, indent=1, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    with pytest.raises(ReleaseInputsError, match="does not match its body"):
        load_release_inputs(root, hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize(
    "bad",
    ["../escape.json", "/abs.json", "a//b.json", "a\\b.json", "C:/x.json", "./a.json"],
)
def test_unsafe_manifest_path_refused(inputs: tuple[Path, str], bad: str) -> None:
    root, _ = inputs

    def mutate(body: dict[str, Any]) -> None:
        body["entries"][0]["path"] = bad

    with pytest.raises(ReleaseInputsError, match="safe relative path"):
        load_release_inputs(root, _reseal(root, mutate))


def test_entry_outside_registry_or_policy_refused(inputs: tuple[Path, str]) -> None:
    root, _ = inputs
    (root / "contracts").mkdir()
    (root / "contracts" / "x.json").write_bytes(b"{}")

    def mutate(body: dict[str, Any]) -> None:
        body["entries"].append(
            {
                "path": "contracts/x.json",
                "sha256": hashlib.sha256(b"{}").hexdigest(),
                "size_bytes": 2,
            }
        )

    with pytest.raises(ReleaseInputsError, match="not a release input"):
        load_release_inputs(root, _reseal(root, mutate))


def test_duplicate_and_malformed_entries_refused(inputs: tuple[Path, str]) -> None:
    root, _ = inputs
    with pytest.raises(ReleaseInputsError, match="repeats"):
        load_release_inputs(
            root, _reseal(root, lambda b: b["entries"].append(dict(b["entries"][0])))
        )
    with pytest.raises(ReleaseInputsError, match="malformed"):
        load_release_inputs(
            root, _reseal(root, lambda b: b["entries"][0].pop("size_bytes"))
        )


def test_symlink_refused(inputs: tuple[Path, str], tmp_path: Path) -> None:
    root, pin = inputs
    outside = tmp_path / "outside.json"
    outside.write_bytes(FILES[POLICY_FILE])
    (root / POLICY_FILE).unlink()
    try:
        os.symlink(outside, root / POLICY_FILE)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ReleaseInputsError, match="symlink"):
        load_release_inputs(root, pin)


def test_file_swapped_after_load_refused(inputs: tuple[Path, str]) -> None:
    root, pin = inputs
    loaded = load_release_inputs(root, pin)
    (root / BASELINE).write_bytes(b'{"cutoff": "2000-01-14"}')
    with pytest.raises(ReleaseInputsError, match="changed after it was verified"):
        loaded.path(BASELINE)
    with pytest.raises(ReleaseInputsError, match="do not include"):
        loaded.path("acceptance/other.json")


def test_build_refuses_unregistered_and_empty(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "x.json").write_bytes(b"{}")
    with pytest.raises(ReleaseInputsError, match="not a release input"):
        build_manifest(tmp_path, ["contracts/x.json"], cutoff=CUTOFF)
    with pytest.raises(ReleaseInputsError, match="at least one"):
        build_manifest(tmp_path, [], cutoff=CUTOFF)


# -- registry ---------------------------------------------------------------------------
def test_registry_classifies_every_gate_profile_data_pattern() -> None:
    """Whatever the gate profile calls a data input must have a disposition here."""

    samples = [p.replace("*", "-x") for p in gp.DATA_INPUT_PATTERNS]
    assert all(classify(sample) is not None for sample in samples)
    assert classify(BASELINE) == RELEASE_INPUT
    assert classify("contracts/portfolio-financial-mvp-source-v1.json") == CODE_BOUND
    assert classify("contracts/portfolio-dependence-policy-v1.json") == POLICY
    assert classify("tools/release_driver.py") is None


def test_every_tracked_data_input_is_classified() -> None:
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
        .stdout.decode()
        .split("\0")
    )
    import fnmatch

    missing = [
        name
        for name in tracked
        if name
        and any(fnmatch.fnmatchcase(name, p) for p in gp.DATA_INPUT_PATTERNS)
        and classify(name) is None
    ]
    assert missing == []


# -- producer to consumer seam ---------------------------------------------------------
def _tracked_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src-repo"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.test", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    for relative, data in {**FILES, "tools/code.py": b"x = 1\n"}.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_bytes(data)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "synthetic"], cwd=repo, check=True)
    return repo


def _run_config(tmp_path: Path, repo: Path, **extra: str) -> dict[str, Any]:
    return {
        "repo_root": str(repo),
        "python": sys.executable,
        "cycle": str(tmp_path / "cycle"),
        "run": str(tmp_path / "run"),
        "code_commit": "c" * 40,
        "mode": "release",
        "ceremony": "full",
        "requested_as_of": CUTOFF,
        **extra,
    }


def test_seam_extract_then_driver_consumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _tracked_repo(tmp_path)
    out = tmp_path / "release-inputs"
    assert (
        cli.main(
            [
                "extract",
                "--repo-root",
                str(repo),
                "--out-root",
                str(out),
                "--cutoff",
                CUTOFF,
            ]
        )
        == 0
    )
    produced = json.loads(capsys.readouterr().out)
    assert produced["entries"] == len(FILES)  # code.py is not a release input
    assert not (out / "tools").exists()
    pin = produced["manifest_sha256"]

    assert cli.main(["verify", "--root", str(out), "--manifest-sha256", pin]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "path",
                "--root",
                str(out),
                "--manifest-sha256",
                pin,
                "--logical",
                BASELINE,
            ]
        )
        == 0
    )
    assert Path(capsys.readouterr().out.strip()) == out / BASELINE

    graph = json.loads(driver.GRAPH_DEFAULT.read_bytes())
    run = driver.Run(
        _run_config(
            tmp_path,
            repo,
            release_inputs_root=str(out),
            release_inputs_manifest_sha256=pin,
        ),
        graph,
        offline=True,
    )
    baseline = out / BASELINE
    assert run.expand("{expected_baseline}") == str(baseline)
    assert run.expand("{expected_baseline_arg}") == str(baseline)
    assert (
        run.values["expected_baseline_sha256"]
        == hashlib.sha256(FILES[BASELINE]).hexdigest()
    )
    stage = next(s for s in graph["stages"] if "expected_baseline" in s)
    driver.check_expected_baseline(
        run, stage
    )  # the producer's cutoff is the run's cutoff
    assert [run.expand(item) for pair in stage["pinned_inputs"] for item in pair] == [
        str(baseline),
        run.values["expected_baseline_sha256"],
    ]


def test_seam_extract_refuses_inside_repo_and_nonempty(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path)
    with pytest.raises(ReleaseInputsError, match="outside the repository"):
        cli._extract(
            cli.argparse.Namespace(
                repo_root=repo, commit="HEAD", out_root=repo / "inner", cutoff=CUTOFF
            )
        )
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_bytes(b"1")
    assert (
        cli.main(
            [
                "extract",
                "--repo-root",
                str(repo),
                "--out-root",
                str(busy),
                "--cutoff",
                CUTOFF,
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "damage", ["tamper", "missing", "wrong-pin", "no-pin", "cutoff", "sha-pin"]
)
def test_driver_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    repo = _tracked_repo(tmp_path)
    out = tmp_path / "release-inputs"
    cli.main(
        [
            "extract",
            "--repo-root",
            str(repo),
            "--out-root",
            str(out),
            "--cutoff",
            CUTOFF,
        ]
    )
    pin = json.loads(capsys.readouterr().out)["manifest_sha256"]
    extra = {"release_inputs_root": str(out), "release_inputs_manifest_sha256": pin}
    if damage == "tamper":
        (out / BASELINE).write_bytes(b'{"cutoff": "2000-01-07", "synthetic": false}')
    elif damage == "missing":
        (out / BASELINE).unlink()
    elif damage == "wrong-pin":
        extra["release_inputs_manifest_sha256"] = "1" * 64
    elif damage == "no-pin":
        del extra["release_inputs_manifest_sha256"]
    elif damage == "sha-pin":
        extra["expected_baseline_sha256"] = "2" * 64
    config = _run_config(tmp_path, repo, **extra)
    if damage == "cutoff":
        config["requested_as_of"] = "2000-01-14"
    graph = json.loads(driver.GRAPH_DEFAULT.read_bytes())
    with pytest.raises(driver.Refusal):
        driver.Run(config, graph, offline=True)


# -- the default path is unchanged --------------------------------------------------------
_OLD = {
    '"{expected_baseline}"': '"{repo_root}/acceptance/expected-baseline.json"',
    '"{expected_baseline_arg}"': '"acceptance/expected-baseline.json"',
}


def _legacy_graph_text(text: str) -> str:
    """The graph as it was before the loader: literals in the stages, no new ``paths``."""

    text = re.sub(r'    "expected_baseline(_arg)?": "[^"]*",\n', "", text)
    for placeholder, literal in _OLD.items():
        text = text.replace(placeholder, literal)
    return text


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def test_default_expansion_is_byte_identical_to_the_legacy_graph(
    tmp_path: Path,
) -> None:
    text = driver.GRAPH_DEFAULT.read_bytes().decode("utf-8")
    new_graph, old_graph = json.loads(text), json.loads(_legacy_graph_text(text))
    assert "expected_baseline" not in old_graph["paths"]
    assert "acceptance/expected-baseline.json" in json.dumps(old_graph)
    names = (
        {
            name
            for s in _strings(new_graph["stages"])
            for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", s)
        }
        - set(new_graph["paths"])
        - set(new_graph["derive"])
        - {"release_inputs_root", "release_inputs_manifest_sha256"}
    )
    config = {name: f"<{name}>" for name in names}
    config.update(_run_config(tmp_path, tmp_path / "repo"))
    new_run = driver.Run(config, new_graph, offline=True)
    old_run = driver.Run(config, old_graph, offline=True)

    # the baseline spec stage names the release-inputs root itself; configuring one is what switches the
    # run to it, so those strings are outside this comparison
    def comparable(graph: Any) -> list[str]:
        return [s for s in _strings(graph["stages"]) if "release_inputs" not in s]

    new_out = [new_run.expand(s) for s in comparable(new_graph)]
    old_out = [old_run.expand(s) for s in comparable(old_graph)]
    assert new_out == old_out
    assert any(s.endswith("acceptance/expected-baseline.json") for s in new_out)
    assert (
        new_run.expand("{expected_baseline_arg}") == "acceptance/expected-baseline.json"
    )
    assert "release_inputs_root" not in new_run.values


# -- review findings -----------------------------------------------------------------------
def _bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, str, Path]:
    repo = _tracked_repo(tmp_path)
    out = tmp_path / "release-inputs"
    cli.main(
        [
            "extract",
            "--repo-root",
            str(repo),
            "--out-root",
            str(out),
            "--cutoff",
            CUTOFF,
        ]
    )
    return out, json.loads(capsys.readouterr().out)["manifest_sha256"], repo


def test_baseline_rechecked_after_binding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, pin, repo = _bound(tmp_path, capsys)
    graph = json.loads(driver.GRAPH_DEFAULT.read_bytes())
    config = _run_config(
        tmp_path, repo, release_inputs_root=str(out), release_inputs_manifest_sha256=pin
    )
    run = driver.Run(config, graph, offline=True)
    run.recheck_release_inputs()
    (out / BASELINE).write_bytes(b'{"cutoff": "2000-01-07", "synthetic": "moved"}')
    with pytest.raises(driver.Refusal, match="changed after it was verified"):
        run.recheck_release_inputs()


def test_stop_and_status_do_not_load_the_inputs_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, pin, repo = _bound(tmp_path, capsys)
    config = _run_config(
        tmp_path, repo, release_inputs_root=str(out), release_inputs_manifest_sha256=pin
    )
    (out / BASELINE).unlink()  # the root is damaged or unmounted mid-run
    graph = json.loads(driver.GRAPH_DEFAULT.read_bytes())
    with pytest.raises(driver.Refusal):
        driver.Run(config, graph, offline=True)
    driver.Run(
        config, graph, offline=True, bind_inputs=False
    )  # the kill path still loads


def test_cli_refuses_another_cutoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, pin, _ = _bound(tmp_path, capsys)
    args = ["--root", str(out), "--manifest-sha256", pin, "--logical", POLICY_FILE]
    assert cli.main(["path", *args, "--cutoff", CUTOFF]) == 0
    assert cli.main(["path", *args, "--cutoff", "2000-01-14"]) == 1
    assert (
        cli.main(
            [
                "verify",
                "--root",
                str(out),
                "--manifest-sha256",
                pin,
                "--cutoff",
                "2000-01-14",
            ]
        )
        == 1
    )

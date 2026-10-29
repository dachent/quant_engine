"""RELEASE-DRIVER-001: the replay world is restored from archives by path, verified by hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any
import zipfile

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_restore_world as tool  # noqa: E402

OLD_W = "C:\\Dev\\worktrees\\quant-engine-completion\\"
OLD_C2 = "C:\\Dev\\q\\chain2\\"
WORLD = {
    "roots": [
        {
            "old": OLD_W,
            "key": "wt",
            "archive": "evidence/staging.zip",
            "archive_prefix": "worktrees/",
            "only_prefixes": [".pt/"],
            "workspace_fallback": True,
        },
        {
            "old": OLD_C2,
            "key": "chain2",
            "archive": "evidence/inputs.zip",
            "archive_prefix": "",
        },
    ]
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    (workspace / "dataPortfolio").mkdir()
    (workspace / "dataPortfolio" / "live.json").write_bytes(b"live")
    with zipfile.ZipFile(workspace / "evidence" / "staging.zip", "w") as z:
        z.writestr("worktrees/.pt/source/a.json", b"alpha")
        z.writestr("worktrees/.pt/dir/one.txt", b"1")
        z.writestr("worktrees/.pt/dir/two.txt", b"2")
    with zipfile.ZipFile(workspace / "evidence" / "inputs.zip", "w") as z:
        z.writestr("in/spec.json", b"chain")
    world_file = tmp_path / "world.json"
    world_file.write_text(json.dumps(WORLD), encoding="utf-8")
    return {
        "tmp": tmp_path,
        "workspace": workspace,
        "world": world_file,
        "dest": tmp_path / "dest",
    }


def _spec(world: dict[str, Any], *entries: Any) -> Path:
    path = world["tmp"] / "spec.json"
    path.write_text(json.dumps({"inputs": list(entries)}), encoding="utf-8")
    return path


def _run(world: dict[str, Any], spec: Path, *extra: str) -> int:
    return tool.main(
        [
            "--spec", str(spec),
            "--world", str(world["world"]),
            "--workspace", str(world["workspace"]),
            "--dest", str(world["dest"]),
            "--report", str(world["tmp"] / "report.json"),
            *extra,
        ]
    )  # fmt: skip


def _report(world: dict[str, Any]) -> dict[str, Any]:
    return json.loads((world["tmp"] / "report.json").read_text(encoding="utf-8"))


def test_restores_files_directories_and_uses_the_workspace_in_place(
    world: dict[str, Any],
) -> None:
    spec = _spec(
        world,
        {"path": OLD_W + ".pt\\source\\a.json", "sha256": _sha(b"alpha")},
        OLD_W + ".pt\\dir",
        OLD_W + "dataPortfolio\\live.json",
        {"path": OLD_C2 + "in\\spec.json", "sha256": _sha(b"chain")},
    )
    assert _run(world, spec) == 0
    dest = world["dest"]
    assert (dest / "wt" / ".pt" / "source" / "a.json").read_bytes() == b"alpha"
    assert (dest / "wt" / ".pt" / "dir" / "two.txt").read_bytes() == b"2"
    assert (dest / "chain2" / "in" / "spec.json").read_bytes() == b"chain"
    assert not (dest / "wt" / "dataPortfolio").exists()
    assert _report(world)["counts"] == {"RESTORED": 3, "WORKSPACE": 1}


def test_a_pinned_file_that_hashes_differently_fails(world: dict[str, Any]) -> None:
    spec = _spec(
        world, {"path": OLD_W + ".pt\\source\\a.json", "sha256": _sha(b"other")}
    )
    assert _run(world, spec) == 1
    assert _report(world)["counts"] == {"MISMATCH": 1}


def test_missing_is_an_error_unless_the_cycle_regenerates_it(
    world: dict[str, Any],
) -> None:
    spec = _spec(world, OLD_W + ".pt\\source\\gone.json", OLD_C2 + "cycle\\made.json")
    assert _run(world, spec) == 1
    assert _report(world)["unexplained_missing"] == [
        OLD_C2 + "cycle\\made.json",
        OLD_W + ".pt\\source\\gone.json",
    ]
    assert (
        _run(
            world,
            spec,
            "--allow-missing",
            OLD_C2 + "cycle",
            OLD_W + ".pt\\source\\gone",
        )
        == 0
    )


def test_paths_outside_every_old_root_are_ignored_and_the_archive_prefix_is_enforced(
    world: dict[str, Any],
) -> None:
    spec = _spec(world, "C:\\elsewhere\\x.json", OLD_W + "tools\\y.py")
    assert _run(world, spec, "--allow-missing", OLD_W) == 0
    assert [row["path"] for row in _report(world)["rows"]] == [OLD_W + "tools\\y.py"]


def test_remap_path_follows_the_world_roots(world: dict[str, Any]) -> None:
    dest = world["dest"]
    assert tool.remap_path(OLD_C2 + "in\\spec.json", WORLD, dest) == str(
        dest / "chain2" / "in" / "spec.json"
    )
    assert tool.remap_path("C:\\elsewhere\\x", WORLD, dest) == "C:\\elsewhere\\x"


def test_an_absent_archive_is_a_named_missing_not_a_crash(
    world: dict[str, Any],
) -> None:
    (world["workspace"] / "evidence" / "inputs.zip").unlink()
    assert _run(world, _spec(world, OLD_C2 + "in\\spec.json")) == 1
    assert "absent" in _report(world)["rows"][0]["detail"]


def test_the_shipped_world_file_names_real_roots() -> None:
    shipped = json.loads(
        (REPO_ROOT / "ops" / "mvp2-release" / "replay-world.json").read_text(
            encoding="utf-8"
        )
    )
    assert {root["key"] for root in shipped["roots"]} == {"wt", "chain2"}
    assert all(root["old"].endswith("\\") for root in shipped["roots"])

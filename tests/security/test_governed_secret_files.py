"""Governed-secret files of the synthetic-fixture generator never reach Git (issue 696).

``salt.txt`` inverts the synthetic id/amount mapping and ``spec.json`` lists the real
values it rewrites; both live in the data root only.  Two independent guards must
reject them: ``.gitignore`` and the repository-boundary policy.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_repository_boundary.py"
SPEC = importlib.util.spec_from_file_location("check_repository_boundary", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
boundary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = boundary
SPEC.loader.exec_module(boundary)

SECRET_PATHS = (
    "salt.txt",
    "spec.json",
    "tests/acceptance/rows/salt.txt",
    "tests/acceptance/rows/spec.json",
    "fixture-salt.txt",
    "fixture-spec.json",
)


def _dummy_worktree(root: Path) -> None:
    for relative in (*SECRET_PATHS, "tests/acceptance/rows/ordinary.json"):
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dummy-not-a-real-secret\n")


def test_the_boundary_policy_rejects_governed_secret_files(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _dummy_worktree(worktree)
    policy = boundary.load_policy(REPOSITORY_ROOT / ".policy" / "data-boundary.toml")
    findings = boundary.scan_tracked_entries(
        worktree,
        policy,
        [boundary.TrackedEntry("100644", path) for path in SECRET_PATHS],
    )
    flagged = {f.path for f in findings if f.code == "PATH_FORBIDDEN_BASENAME"}
    assert flagged == set(SECRET_PATHS)
    control = boundary.scan_tracked_entries(
        worktree,
        policy,
        [boundary.TrackedEntry("100644", "tests/acceptance/rows/ordinary.json")],
    )
    assert not [f for f in control if f.code == "PATH_FORBIDDEN_BASENAME"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
def test_gitignore_rejects_governed_secret_files(tmp_path: Path) -> None:
    _dummy_worktree(tmp_path / "worktree")
    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "check-ignore",
            "--no-index",
            "-v",
            "--",
            *SECRET_PATHS,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert len(ignored.stdout.strip().splitlines()) == len(SECRET_PATHS)
    ordinary = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "check-ignore",
            "--no-index",
            "-q",
            "--",
            "tests/acceptance/rows/ordinary.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ordinary.returncode == 1

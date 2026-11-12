"""A force push leaves github.event.before pointing at a commit the checkout lacks (#910).

The first run of the new branch-push trigger failed on exactly that. For a push the range falls
back to the whole tree; a pull request still refuses a base it cannot find.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _range_step() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/quality.yml").read_text("utf-8")
    )
    step = next(
        s
        for s in workflow["jobs"]["quality"]["steps"]
        if s.get("id") == "controller_graph_range"
    )
    return str(step["run"])


def _run(tmp_path: Path, event: str, base: str) -> tuple[int, str]:
    argv = shlex.split(_range_step(), posix=True)
    assert argv[:2] == ["python", "-c"]
    out = tmp_path / "out.txt"
    out.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "EVENT_NAME": event,
        "PULL_REQUEST_BASE": base,
        "PUSH_BASE": base,
        "GITHUB_OUTPUT": str(out),
    }
    done = subprocess.run(
        [sys.executable, "-c", argv[2]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    return done.returncode, out.read_text(encoding="utf-8").strip()


def test_push_with_an_unknown_before_commit_uses_the_whole_tree(tmp_path: Path) -> None:
    assert _run(tmp_path, "push", "5" * 40) == (0, "base=EMPTY_TREE")


def test_pull_request_with_an_unknown_base_still_refuses(tmp_path: Path) -> None:
    code, _ = _run(tmp_path, "pull_request", "5" * 40)
    assert code != 0

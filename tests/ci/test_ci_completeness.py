"""Completeness guards: a new test directory or package must be registered.

The hosted Quality job lists its pytest directories, mypy targets and package
roots by hand. These tests fail when something is added to the tree without
being added to those lists, and prove `tools/ci.py` reproduces every required
job from the workflow files instead of a second copy of them.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import ci  # noqa: E402

QUALITY = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
    encoding="utf-8"
)
PYPROJECT = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8"))


def _quality_command(prefix: str) -> str:
    commands = [s.command for s in ci.plan("quality") if s.command.startswith(prefix)]
    assert len(commands) == 1, prefix
    return commands[0]


def test_every_test_directory_is_run_by_quality() -> None:
    pytest_line = _quality_command(
        "uv run --frozen --no-sync pytest -q --disable-socket --durations"
    )
    listed = set(re.findall(r"\btests/([A-Za-z0-9_]+)", pytest_line))
    on_disk = {
        path.name
        for path in (REPOSITORY_ROOT / "tests").iterdir()
        if path.is_dir()
        and any(
            found
            for pattern in ("test_*.py", "*_test.py")
            for found in path.rglob(pattern)
        )
    }
    assert sorted(on_disk - listed) == [], "add the directory to the pytest step"
    assert sorted(listed - on_disk) == [], "pytest step names a missing directory"


def test_every_workspace_member_is_registered_and_checked() -> None:
    mypy_line = _quality_command("uv run --frozen --no-sync mypy")
    mypy_path = set(PYPROJECT["tool"]["mypy"]["mypy_path"])
    members = set(PYPROJECT["tool"]["uv"]["workspace"]["members"])
    on_disk = {
        f"{root}/{path.name}"
        for root in ("packages", "apps")
        for path in (REPOSITORY_ROOT / root).iterdir()
        if (path / "pyproject.toml").is_file()
    }
    assert on_disk <= members, "member missing from [tool.uv.workspace].members"
    for member in sorted(members):
        assert (REPOSITORY_ROOT / member / "pyproject.toml").is_file(), member
        if member.startswith("packages/"):
            assert f"{member}/src" in mypy_line, f"{member} not type-checked in Quality"
            assert f"{member}/src" in mypy_path, f"{member} missing from mypy_path"
        else:
            # Apps are not importable packages; Quality runs their own test step.
            assert f"{member}/tests" in QUALITY, f"{member} tests not run in Quality"


def test_ci_entrypoint_reproduces_every_required_job() -> None:
    for job in ci.JOBS:
        assert ci.plan(job), job
    quality = [s.command for s in ci.plan("quality")]
    # Same commands the hosted job runs, with expressions resolved.
    assert any(c.startswith("uv run --frozen --no-sync ruff check") for c in quality)
    assert any("tools/portfolio_controller_graph.py" in c for c in quality)
    assert all(
        "${{" not in c for job in ci.JOBS for c in (s.command for s in ci.plan(job))
    )
    assert "name: ${{ matrix.os }} / Python ${{ matrix.python-version }}" in QUALITY

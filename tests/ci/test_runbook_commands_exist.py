"""The commands a runbook tells the operator to run name subcommands their tool defines (F2 item 24).

The decision-refresh runbook cited ``release_cache_base.py seed``, a subcommand that does not exist; the operator
found out mid-cycle.  Every ``python tools/<tool>.py <subcommand>`` in a bash block of the runbook must answer
``--help`` with exit 0 (an unknown subcommand is argparse exit 2).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = (ROOT / "docs" / "decision-refresh-runbook.md",)
COMMAND = re.compile(r"python (tools/[a-z0-9_]+\.py) ([a-z][a-z0-9-]*)\b")


def _commands() -> list[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for runbook in RUNBOOKS:
        for tool, sub in COMMAND.findall(runbook.read_text(encoding="utf-8")):
            found.add((runbook.name, tool, sub))
    return sorted(found)


def test_the_runbook_cites_commands_at_all() -> None:
    assert len(_commands()) >= 10


@pytest.mark.parametrize(("runbook", "tool", "sub"), _commands())
def test_a_runbook_command_names_a_subcommand_its_tool_defines(
    runbook: str, tool: str, sub: str
) -> None:
    assert (ROOT / tool).is_file(), f"{runbook} cites a missing tool {tool}"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / tool), sub, "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"{runbook}: `{tool} {sub}` -> {result.stderr[-300:]}"
    )

"""#907: the uv the workflows install must satisfy the repository's uv pin."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

from packaging.specifiers import SpecifierSet
from packaging.version import Version

_ROOT = Path(__file__).resolve().parents[2]


def test_every_workflow_uv_version_satisfies_the_required_version() -> None:
    pin = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "uv"
    ]["required-version"]
    allowed = SpecifierSet(pin)
    found = 0
    for workflow in sorted((_ROOT / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for match in re.finditer(
            r"setup-uv[^\n]*\n(?:[^\n]*\n){0,4}?\s+version:\s*\"([^\"]+)\"", text
        ):
            found += 1
            assert Version(match.group(1)) in allowed, (workflow.name, match.group(1))
    assert found, "no workflow pins uv"


def test_the_uv_pin_is_documented_for_local_runs() -> None:
    agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "required-version" in agents and "uv" in agents

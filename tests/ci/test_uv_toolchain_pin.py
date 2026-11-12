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
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "setup-uv" not in line or "uses:" not in line:
                continue
            # the step's own `with:` block: lines indented deeper than the `uses:` key
            indent = len(line) - len(line.lstrip())
            step = []
            for later in lines[index + 1 :]:
                if later.strip() and len(later) - len(later.lstrip()) < indent:
                    break
                if later.strip().startswith("- "):
                    break
                step.append(later)
            versions = [
                match.group(1)
                for text in step
                if (match := re.match(r"\s*version:\s*[\"']?([0-9][^\"'\s#]*)", text))
            ]
            assert versions, f"{workflow.name} installs uv without a pinned version"
            for version in versions:
                found += 1
                assert Version(version) in allowed, (workflow.name, version)
    assert found, "no workflow pins uv"


def test_the_uv_pin_is_documented_for_local_runs() -> None:
    agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "required-version" in agents and "uv" in agents

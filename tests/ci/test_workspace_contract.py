from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import sys
import tomllib

import quant_engine.ci_controls as ci_controls


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_supported_python_and_installed_ci_controls() -> None:
    assert sys.version_info[:2] in {(3, 11), (3, 12), (3, 13)}
    assert ci_controls.__version__ == "0.1.0"
    distribution = metadata.distribution("quant-engine-ci-controls")
    assert distribution.metadata["Requires-Python"].replace(" ", "") == ">=3.11,<3.14"


def test_virtual_root_has_only_the_ci_controls_workspace_member() -> None:
    root = _toml(REPOSITORY_ROOT / "pyproject.toml")
    tool = root["tool"]
    assert isinstance(tool, dict)
    uv = tool["uv"]
    assert isinstance(uv, dict)
    assert uv["package"] is False
    assert uv["workspace"] == {"members": ["packages/ci-controls"]}
    assert root["project"]["requires-python"] == ">=3.11,<3.14"  # type: ignore[index]


def test_ci_controls_package_is_private_namespaced_and_non_product() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "ci-controls"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == ["PyYAML>=6.0.3,<7"]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert set(project["scripts"]) == {"quant-engine-workflow-policy"}  # type: ignore[arg-type]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.ci_controls"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (member_root / "src" / "quant_engine" / "ci_controls" / "py.typed").is_file()


def test_ci_lock_contains_only_full_action_shas_and_checksummed_tools() -> None:
    ci_lock = json.loads(
        (REPOSITORY_ROOT / ".github" / "ci-lock.json").read_text(encoding="utf-8")
    )
    assert ci_lock["schema_version"] == 1
    assert set(ci_lock["actions"]) == {
        "actions/checkout",
        "actions/setup-python",
        "astral-sh/setup-uv",
    }
    for record in ci_lock["actions"].values():
        assert len(record["sha"]) == 40
        assert record["sha"] == record["sha"].lower()
        int(record["sha"], 16)
    gitleaks = ci_lock["tools"]["gitleaks"]
    assert len(gitleaks["sha256"]) == 64
    int(gitleaks["sha256"], 16)

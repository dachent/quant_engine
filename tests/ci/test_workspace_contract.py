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


def test_virtual_root_has_only_accepted_workspace_members() -> None:
    root = _toml(REPOSITORY_ROOT / "pyproject.toml")
    tool = root["tool"]
    assert isinstance(tool, dict)
    uv = tool["uv"]
    assert isinstance(uv, dict)
    assert uv["package"] is False
    assert uv["workspace"] == {
        "members": [
            "packages/ci-controls",
            "packages/core",
            "packages/fidelity",
            "packages/ibkr",
            "packages/matrixport",
            "packages/portfolio-ledger",
            "packages/schwab",
            "packages/web-contracts",
        ]
    }
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


def test_core_package_is_private_namespaced_and_dependency_free() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "core"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == []
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.core"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (member_root / "src" / "quant_engine" / "core" / "py.typed").is_file()


def test_portfolio_ledger_package_is_private_and_depends_only_on_core() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "portfolio-ledger"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == ["quant-engine-core"]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.portfolio_ledger"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (
        member_root / "src" / "quant_engine" / "portfolio_ledger" / "py.typed"
    ).is_file()


def test_ibkr_package_is_private_and_read_side_only() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "ibkr"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == [
        "quant-engine-core",
        "quant-engine-portfolio-ledger",
    ]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.ibkr"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (member_root / "src" / "quant_engine" / "ibkr" / "py.typed").is_file()
    public_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (member_root / "src" / "quant_engine" / "ibkr").glob("*.py")
    ).lower()
    for forbidden in ("submit_order", "place_order", "cancel_order"):
        assert forbidden not in public_source


def test_fidelity_package_is_private_static_and_read_side_only() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "fidelity"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == [
        "quant-engine-core",
        "quant-engine-portfolio-ledger",
    ]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.fidelity"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (member_root / "src" / "quant_engine" / "fidelity" / "py.typed").is_file()
    public_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (member_root / "src" / "quant_engine" / "fidelity").glob("*.py")
    ).lower()
    for forbidden in ("submit_order", "place_order", "cancel_order", "httpx"):
        assert forbidden not in public_source
    quality = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    assert "packages/fidelity/src" in quality
    assert "tests/fidelity" in quality


def test_matrixport_package_is_private_and_semantically_read_only() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "matrixport"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert "quant-engine-core" in project["dependencies"]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.matrixport"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (member_root / "src" / "quant_engine" / "matrixport" / "py.typed").is_file()
    quality = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    assert "packages/matrixport/src" in quality
    assert "tests/matrixport" in quality
    assert "tools/matrixport_generation.py" in quality


def test_web_contract_package_is_private_and_read_side_only() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "web-contracts"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == ["quant-engine-core"]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.web_contracts"
    )
    assert not (member_root / "src" / "quant_engine" / "__init__.py").exists()
    assert (
        member_root / "src" / "quant_engine" / "web_contracts" / "py.typed"
    ).is_file()

    quality = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    assert "packages/web-contracts/src" in quality
    assert "tests/web_contracts" in quality


def test_schwab_package_is_private_and_read_side_only() -> None:
    member_root = REPOSITORY_ROOT / "packages" / "schwab"
    member = _toml(member_root / "pyproject.toml")
    project = member["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["dependencies"] == [
        "quant-engine-core",
        "quant-engine-portfolio-ledger",
    ]
    assert project["classifiers"] == ["Private :: Do Not Upload"]
    assert member["tool"]["uv"]["build-backend"]["module-name"] == (  # type: ignore[index]
        "quant_engine.schwab"
    )
    public_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (member_root / "src" / "quant_engine" / "schwab").glob("*.py")
    ).lower()
    for forbidden in ("submit_order", "place_order", "cancel_order", "raw_request"):
        assert forbidden not in public_source
    quality = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    assert "packages/schwab/src" in quality
    assert "tests/schwab" in quality


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

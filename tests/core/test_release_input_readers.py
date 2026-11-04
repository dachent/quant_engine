"""Every reader of a release input outside Git goes through the release-inputs loader.

The cutoff-dependent files left Git (``core.release_inputs``). A tool that still builds the
repo-relative path of one and opens it finds nothing in a clean checkout, or worse, a stale copy.
The static scan below fails when any tool, derivation or release script names a release-input path
without routing it through the loader; the other tests exercise the resolver. Synthetic fixtures only.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from quant_engine.core import ReleaseInputsError, resolve_release_input
from quant_engine.core.release_inputs import (
    ENV_PIN,
    ENV_ROOT,
    RELEASE_INPUT,
    build_manifest,
    classify,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_acceptance_inputs as acceptance_inputs  # noqa: E402

CUTOFF = "2000-01-07"
CROSSWALK = "ops/mvp2-release/inputs/current-epoch-crosswalk.json"

# the path prefixes of the removed files (the registry's release-input patterns)
_PREFIXES = (
    "ops/mvp2-release/inputs",
    "acceptance/expected-baseline",
    "acceptance/baselines",
)
# code scanned for readers; preserved scripts and tests are history and fixtures
_SCAN = (
    ("tools", "*.py"),
    ("acceptance", "*.py"),
    ("ops/mvp2-release/scripts", "*.py"),
    ("ops/mvp2-release/scripts", "*.sh"),
    ("packages/*/src", "*.py"),
)
# a module that names the paths is fine when it routes them through the loader
_LOADER_MARKERS = (
    "resolve_release_input",
    "load_release_inputs",
    "release_input_root",
    "RELEASE_INPUTS_ROOT",
)
# the paths are data here, not reads: the registry, the gate profile's tracked-data patterns and the
# census/extract CLI that define or move the release inputs
_DEFINES = {
    "packages/core/src/quant_engine/core/release_inputs.py",
    "packages/core/src/quant_engine/core/gate_profile.py",
    "tools/release_inputs.py",
}


def _named_paths(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".sh":
        return [
            line.strip()
            for line in text.splitlines()
            if any(prefix in line for prefix in _PREFIXES)
        ]
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(_PREFIXES):
                found.append(node.value)
    return found


def _scanned() -> list[Path]:
    files: list[Path] = []
    for base, pattern in _SCAN:
        for directory in sorted(ROOT.glob(base)):
            files.extend(sorted(directory.rglob(pattern)))
    return files


def test_no_tool_names_a_removed_input_path_without_the_loader() -> None:
    offenders = []
    for path in _scanned():
        relative = path.relative_to(ROOT).as_posix()
        if relative in _DEFINES:
            continue
        names = _named_paths(path)
        if not names:
            continue
        if not any(
            marker in path.read_text(encoding="utf-8") for marker in _LOADER_MARKERS
        ):
            offenders.append(f"{relative}: {names[0][:90]}")
    assert not offenders, (
        "these read a cutoff-dependent input by repo path; route them through "
        "quant_engine.core.resolve_release_input / load_release_inputs:\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_the_known_loader_routed_readers() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _scanned() if _named_paths(p)}
    assert {
        "tools/release_acceptance_inputs.py",
        "tools/release_baseline_spec.py",
        "tools/release_driver.py",
        "tools/release_prepare_inputs.py",
        "tools/release_quantbase_sources.py",
    } <= names


def test_no_removed_input_is_tracked() -> None:
    tracked = [
        path.relative_to(ROOT).as_posix()
        for base in ("acceptance", "ops/mvp2-release/inputs")
        for path in (ROOT / base).rglob("*")
        if path.is_file()
    ]
    assert [t for t in tracked if classify(t) == RELEASE_INPUT] == []


@pytest.fixture
def root(tmp_path: Path) -> tuple[Path, str]:
    target = tmp_path / "inputs" / CROSSWALK
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"calculation_cutoff_date": CUTOFF}), encoding="utf-8")
    _, pin = build_manifest(tmp_path / "inputs", [CROSSWALK], cutoff=CUTOFF)
    return tmp_path / "inputs", pin


def test_resolver_reads_the_pinned_root_never_the_checkout(
    root: tuple[Path, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / CROSSWALK).parent.mkdir(parents=True)
    (repo / CROSSWALK).write_text("stale checkout copy", encoding="utf-8")
    path = resolve_release_input(
        repo, CROSSWALK, root=root[0], manifest_sha256=root[1], cutoff=CUTOFF
    )
    assert path == root[0] / CROSSWALK
    assert (
        resolve_release_input(
            repo, CROSSWALK, environ={ENV_ROOT: str(root[0]), ENV_PIN: root[1]}
        )
        == root[0] / CROSSWALK
    )
    with pytest.raises(ReleaseInputsError, match="this run is for"):
        resolve_release_input(
            repo, CROSSWALK, root=root[0], manifest_sha256=root[1], cutoff="2000-02-04"
        )
    with pytest.raises(ReleaseInputsError, match="both the release-inputs root"):
        resolve_release_input(repo, CROSSWALK, root=root[0])


def test_resolver_without_a_root_needs_the_file_in_the_checkout(tmp_path: Path) -> None:
    with pytest.raises(ReleaseInputsError, match="live outside Git"):
        resolve_release_input(tmp_path, CROSSWALK, environ={})
    (tmp_path / CROSSWALK).parent.mkdir(parents=True)
    (tmp_path / CROSSWALK).write_text("{}", encoding="utf-8")
    assert (
        resolve_release_input(tmp_path, CROSSWALK, environ={}) == tmp_path / CROSSWALK
    )


def test_resolver_refuses_a_path_that_is_not_a_release_input(tmp_path: Path) -> None:
    with pytest.raises(ReleaseInputsError, match="not a release input"):
        resolve_release_input(
            tmp_path, "contracts/portfolio-financial-mvp-source-v1.json"
        )
    with pytest.raises(ReleaseInputsError, match="safe relative path"):
        resolve_release_input(tmp_path, "../acceptance/expected-baseline.json")


def test_a_tampered_root_file_refuses_through_the_resolver(
    root: tuple[Path, str], tmp_path: Path
) -> None:
    (root[0] / CROSSWALK).write_text("changed", encoding="utf-8")
    with pytest.raises(ReleaseInputsError, match="differs from the manifest"):
        resolve_release_input(
            tmp_path, CROSSWALK, root=root[0], manifest_sha256=root[1]
        )


def test_acceptance_sleeve_defaults_resolve_from_the_root(
    root: tuple[Path, str], tmp_path: Path
) -> None:
    path = acceptance_inputs._release_input(
        tmp_path, CROSSWALK, (root[0], root[1]), CUTOFF, "crosswalk"
    )
    assert path == root[0] / CROSSWALK
    with pytest.raises(acceptance_inputs.AcceptanceInputsError, match="RELEASE_INPUTS"):
        acceptance_inputs._release_input(tmp_path, CROSSWALK, None, CUTOFF, "crosswalk")

"""Three modules hash a directory's identity. They must agree byte for byte.

`portfolio_release_builder._directory_inventory` writes the panel-candidate
receipt's `inventory_sha256`; `portfolio_pipeline_materialize._directory_identity`
validates it on resume; `portfolio_control_panel._authorization_directory_identity`
computes the same thing for authorization roots.

The materialiser's copy omitted the trailing newline the other two append before
hashing. The rows agreed byte for byte and only the framing differed, so every
panel-candidate receipt was unverifiable and the attended gate could never pass
on a real run. Nothing caught it, because each module was self-consistent and no
test compared them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> Any:
    module_name = f"_identity_agreement_{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "tools" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory with nested paths, an empty file, and non-ASCII bytes."""

    root = tmp_path_factory.mktemp("identity")
    (root / "a.txt").write_bytes(b"alpha")
    (root / "empty.txt").write_bytes(b"")
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "b.json").write_bytes(b'{"k":"v"}')
    (nested / "unicode.txt").write_bytes("café — naïve".encode("utf-8"))
    return root


def test_directory_identity_agrees_across_producers(sample: Path) -> None:
    """The writer, the validator and the panel reader must produce one value."""

    materializer = _load("portfolio_pipeline_materialize")
    release = _load("portfolio_release_builder")
    panel = _load("portfolio_control_panel")

    validator = materializer._directory_identity(sample)  # noqa: SLF001
    writer = release._directory_inventory(sample)  # noqa: SLF001
    reader = panel._authorization_directory_identity(  # noqa: SLF001
        sample, "identity agreement"
    )

    assert validator["file_count"] == writer["file_count"] == 4
    assert validator["inventory_sha256"] == writer["inventory_sha256"], (
        "the attended validator and the panel-candidate writer disagree, so the "
        "receipt the writer emits can never be verified"
    )
    assert reader["inventory_sha256"] == writer["inventory_sha256"], (
        "the panel authorization reader disagrees with the writer"
    )


def test_identity_changes_when_any_byte_changes(sample: Path, tmp_path: Path) -> None:
    """Agreement must not come from all three being trivially constant."""

    materializer = _load("portfolio_pipeline_materialize")
    release = _load("portfolio_release_builder")

    before = materializer._directory_identity(sample)  # noqa: SLF001
    mutated = tmp_path / "mutated"
    mutated.mkdir()
    for item in sorted(sample.rglob("*")):
        if item.is_file():
            target = mutated / item.relative_to(sample)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    (mutated / "a.txt").write_bytes(b"alphb")

    after = materializer._directory_identity(mutated)  # noqa: SLF001
    assert after["inventory_sha256"] != before["inventory_sha256"]
    assert release._directory_inventory(mutated) == after  # noqa: SLF001

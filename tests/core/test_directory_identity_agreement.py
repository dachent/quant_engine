"""Four modules hash a directory's identity. They must agree byte for byte.

`portfolio_release_builder._directory_inventory` writes the panel-candidate
receipt's `inventory_sha256`; `portfolio_pipeline_materialize._directory_identity`
validates it on resume; `portfolio_attended_preparation._directory_definition`
writes the `history_generation` definition that the same validator reads; and
`portfolio_control_panel._authorization_directory_identity` computes it for
authorization roots.

Two of the four omitted the trailing newline the others append before hashing.
The rows agreed byte for byte and only the framing differed, so a receipt one
module wrote could never be verified by another, and both the panel-candidate
and attended-request gates failed on real runs for that reason alone.

Nothing caught it because each module was self-consistent -- it produced and
validated with its own copy -- and no test ever compared two of them. A fifth
copy would be just as invisible, which is why this compares every one rather
than a fixed pair.
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
    """Every implementation of this identity must produce one value.

    Four modules compute it independently. Each was self-consistent -- it
    produced and validated with its own copy -- so nothing caught that two of
    them framed the canonical rows differently until a real run failed.
    """

    materializer = _load("portfolio_pipeline_materialize")
    release = _load("portfolio_release_builder")
    panel = _load("portfolio_control_panel")
    preparation = _load("portfolio_attended_preparation")

    computed = {
        "materialize._directory_identity": materializer._directory_identity(  # noqa: SLF001
            sample
        ),
        "release._directory_inventory": release._directory_inventory(sample),  # noqa: SLF001
        "panel._authorization_directory_identity": (
            panel._authorization_directory_identity(sample, "identity agreement")  # noqa: SLF001
        ),
        "preparation._directory_definition": preparation._directory_definition(  # noqa: SLF001
            sample
        ),
    }

    digests = {name: value["inventory_sha256"] for name, value in computed.items()}
    assert len(set(digests.values())) == 1, (
        "directory-identity implementations disagree, so an artifact written by "
        f"one cannot be verified by another: {digests}"
    )
    assert {value["file_count"] for value in computed.values()} == {4}


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


# Digest of the sample above computed by the four pre-consolidation encoders at
# main b4d8de34 (release_builder._directory_inventory); consolidating the framing
# into core.directory_identity must not move a single byte.
GOLDEN_SAMPLE_SHA256 = (
    "c910b15d67b551c8c42cc4d38756b26e86f6c1fd56f1da0b67536c694425e5b9"
)


def test_every_producer_still_matches_the_pre_consolidation_golden_digest(
    sample: Path,
) -> None:
    from quant_engine.core import inventory_identity

    for name, compute in {
        "materialize": lambda: _load(
            "portfolio_pipeline_materialize"
        )._directory_identity(  # noqa: SLF001
            sample
        ),
        "release": lambda: _load("portfolio_release_builder")._directory_inventory(  # noqa: SLF001
            sample
        ),
        "panel": lambda: _load(
            "portfolio_control_panel"
        )._authorization_directory_identity(sample, "golden"),  # noqa: SLF001
        "preparation": lambda: _load(
            "portfolio_attended_preparation"
        )._directory_definition(sample),  # noqa: SLF001
    }.items():
        assert compute()["inventory_sha256"] == GOLDEN_SAMPLE_SHA256, name
    rows = [
        {"path": "a.txt", "sha256": "0" * 64, "size_bytes": 1},
    ]
    assert inventory_identity(rows)["file_count"] == 1
    assert inventory_identity([]) == inventory_identity([])
    assert inventory_identity(rows) != inventory_identity([])


def test_the_two_stage_maps_are_equal() -> None:
    from quant_engine.core import pipeline

    materializer = _load("portfolio_pipeline_materialize")
    assert tuple(materializer.STAGES) == tuple(pipeline._PORTFOLIO_STAGE_ORDER)  # noqa: SLF001
    assert dict(materializer.STAGE_DEPENDENCIES) == dict(
        pipeline._PORTFOLIO_STAGE_DEPENDENCIES  # noqa: SLF001
    )

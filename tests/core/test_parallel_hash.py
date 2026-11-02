from __future__ import annotations

import hashlib
import shutil
import types
from pathlib import Path

import pytest

from quant_engine.core import parallel_hash, pipeline


def _tree(root: Path, count: int = 40) -> list[Path]:
    paths = []
    for index in range(count):
        path = root / f"d{index % 5}" / f"f{index}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([index % 251]) * (index * 997 + 3))
        paths.append(path)
    return paths


def test_hash_files_matches_serial_hashing_in_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _tree(tmp_path)
    expected = [
        (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
        for path in paths
    ]
    for workers in ("1", "4", "16"):
        monkeypatch.setenv(parallel_hash.WORKERS_ENV, workers)
        assert parallel_hash.hash_files(paths) == expected
        assert parallel_hash.hash_files(paths[:3]) == expected[:3]


def test_hash_files_raises_the_first_failure_in_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(parallel_hash.WORKERS_ENV, "4")
    paths = _tree(tmp_path)
    paths[5] = tmp_path / "missing-5"
    paths[20] = tmp_path / "missing-20"
    with pytest.raises(FileNotFoundError, match="missing-5"):
        parallel_hash.hash_files(paths)


@pytest.mark.parametrize("raw", ["0", "17", "x", ""])
def test_worker_count_is_bounded_and_validated(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(parallel_hash.WORKERS_ENV, raw)
    with pytest.raises(ValueError, match=parallel_hash.WORKERS_ENV):
        parallel_hash.hash_worker_count()


def test_inventories_are_identical_for_serial_and_parallel_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path / "g")
    results = []
    for workers in ("1", "8"):
        monkeypatch.setenv(parallel_hash.WORKERS_ENV, workers)
        results.append(
            (
                pipeline._inventory(tmp_path / "g"),
                pipeline._path_evidence(tmp_path / "g"),
            )
        )
    assert results[0] == results[1]
    assert len(results[0][0]) == 40


class _StubManifest:
    generation_id = "g1"
    verification = pipeline.VerificationStatus.VERIFIED
    coverage = types.SimpleNamespace(
        status=pipeline.CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
    )

    @classmethod
    def from_json_bytes(cls, _content: bytes) -> type[_StubManifest]:
        return cls


def _generation_node(
    tmp_path: Path, source_dir: Path | None
) -> tuple[types.SimpleNamespace, pipeline.GenerationNode]:
    spec = types.SimpleNamespace(roots={"r": tmp_path / "root"})
    holder = source_dir or tmp_path / "root" / "generations" / "g1"
    holder.mkdir(parents=True)
    (holder / "generation.json").write_bytes(b"{}")
    (holder / "data.bin").write_bytes(b"x" * 100)
    expected = pipeline._inventory(holder)
    manifest_sha256 = hashlib.sha256(b"{}").hexdigest()
    node = pipeline.GenerationNode(
        node_id="n",
        depends_on=(),
        root_id="r",
        generation_id="g1",
        manifest_sha256=manifest_sha256,
        files=expected,
        source_dir=source_dir,
        release_target=True,
    )
    return spec, node


@pytest.mark.parametrize(
    ("layout", "expected_inventories"),
    [("in-place", 1), ("present-destination", 2), ("fresh-copy", 3)],
)
def test_verify_generation_hashes_each_tree_only_when_it_could_have_changed(
    layout: str,
    expected_inventories: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "GenerationManifest", _StubManifest)
    source = None if layout == "in-place" else tmp_path / "source"
    spec, node = _generation_node(tmp_path, source)
    if layout == "present-destination":
        shutil.copytree(source, tmp_path / "root" / "generations" / "g1")
    calls: list[Path] = []
    real = pipeline._inventory
    monkeypatch.setattr(
        pipeline, "_inventory", lambda path: (calls.append(path), real(path))[1]
    )
    assert pipeline._verify_generation(spec, node, materialize=True) == (
        node.manifest_sha256
    )
    assert len(calls) == expected_inventories
    destination = tmp_path / "root" / "generations" / "g1"
    assert real(destination) == node.files


def test_verify_generation_still_refuses_a_differing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "GenerationManifest", _StubManifest)
    source = tmp_path / "source"
    spec, node = _generation_node(tmp_path, source)
    destination = tmp_path / "root" / "generations" / "g1"
    shutil.copytree(source, destination)
    (destination / "data.bin").write_bytes(b"y" * 100)
    with pytest.raises(pipeline.PipelineExecutionError, match="destination"):
        pipeline._verify_generation(spec, node, materialize=True)

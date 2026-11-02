from __future__ import annotations

import hashlib
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
    with pytest.raises(parallel_hash.IrregularFileError, match="missing-5"):
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


def test_a_member_swapped_for_a_link_after_the_walk_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    victim = _tree(tmp_path / "g")[3]
    victim.unlink()
    try:
        victim.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv(parallel_hash.WORKERS_ENV, "4")
    with pytest.raises(parallel_hash.IrregularFileError):
        parallel_hash.hash_files([victim] * 10)

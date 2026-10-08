"""FINAL-PLAN FP:176 and FP:179 regression tests for correlation reuse.

``build_correlation`` is wrapped, never replaced, so every count is real work.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable

import pytest

import test_portfolio_correlation as base

correlation = base.correlation


class Work:
    builds = 0


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> Work:
    counted = Work()
    real_build = correlation.build_correlation

    def build(*args: Any, **kwargs: Any) -> Any:
        counted.builds += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(correlation, "build_correlation", build)
    return counted


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _staged(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    source_root = tmp_path / "source"
    source, days = base._write_returns(source_root)
    spec = base._spec(source_root, source, days)
    retained = tmp_path / "retained"
    staged = correlation.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    return source_root, spec, retained, staged


def _spoil_changed_source(source_root: Path, spec: Path, staged: dict) -> None:
    returns = source_root / "returns.csv"
    returns.write_bytes(returns.read_bytes() + b"\n")


def _spoil_deleted_source(source_root: Path, spec: Path, staged: dict) -> None:
    (source_root / "returns.csv").unlink()


def _spoil_extra_file(source_root: Path, spec: Path, staged: dict) -> None:
    (Path(str(staged["candidate"])) / "unexpected.txt").write_text("x", "utf-8")


def _spoil_wrong_generation(source_root: Path, spec: Path, staged: dict) -> None:
    retained = Path(str(staged["candidate"])).parents[1]
    other = json.loads(spec.read_text("utf-8"))
    other["stress_uplift"] = "0.16"
    other_spec = spec.parent / "spec-b.json"
    other_spec.write_text(json.dumps(other, sort_keys=True), "utf-8")
    staged_b = correlation.stage_candidate(
        other_spec, source_root, spec.parent / "b-output", retained_cache_root=retained
    )
    key = correlation._input_key(spec, source_root)
    b_bytes = (Path(str(staged_b["candidate"])) / "correlation.json").read_bytes()
    (retained / ".correlation-index" / f"{key}.json").write_bytes(
        correlation.canonical_json_bytes(
            {
                "generation_id": staged_b["generation_id"],
                "key": key,
                "result_sha256": hashlib.sha256(b_bytes).hexdigest(),
                "result_size_bytes": len(b_bytes),
            }
        )
    )


@pytest.mark.parametrize(
    ("spoil", "error"),
    [
        (_spoil_changed_source, (correlation.CorrelationError,)),
        (_spoil_deleted_source, (correlation.CorrelationError, OSError)),
        (_spoil_extra_file, (correlation.CorrelationError,)),
        (_spoil_wrong_generation, (correlation.CorrelationError,)),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_rejected_reuse_builds_nothing_and_preserves_index_and_generation(
    tmp_path: Path,
    work: Work,
    spoil: Callable[[Path, Path, dict], None],
    error: tuple[type[BaseException], ...],
) -> None:
    source_root, spec, retained, staged = _staged(tmp_path)
    spoil(source_root, spec, staged)
    work.builds = 0
    before = _tree(retained)
    output = tmp_path / "rejected-output"

    with pytest.raises(error):
        correlation.stage_candidate(
            spec, source_root, output, retained_cache_root=retained
        )
    with pytest.raises(error):
        correlation.publish(spec, source_root, output, retained_cache_root=retained)

    assert work.builds == 0
    assert _tree(retained) == before
    assert not output.exists()


@pytest.mark.parametrize("corrupted", ["correlation.json", "generation.json"])
def test_corrupt_retained_result_is_rejected_before_any_build(
    tmp_path: Path, work: Work, corrupted: str
) -> None:
    source_root, spec, retained, staged = _staged(tmp_path)
    (Path(str(staged["candidate"])) / "correlation.json").write_bytes(b"{}\n")
    work.builds = 0
    before = _tree(retained)

    with pytest.raises(correlation.CorrelationError):
        correlation.stage_candidate(
            spec, source_root, tmp_path / "out", retained_cache_root=retained
        )
    with pytest.raises(correlation.CorrelationError, match="exact build"):
        correlation.publish(
            spec, source_root, tmp_path / "out", retained_cache_root=retained
        )

    assert work.builds == 0
    assert _tree(retained) == before


def test_same_bytes_other_source_root_and_clock_reuse_with_zero_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Work
) -> None:
    first_root = tmp_path / "root-one" / "deeper" / "source"
    second_root = tmp_path / "other-root" / "source"
    source, days = base._write_returns(first_root)
    spec = base._spec(first_root, source, days)
    retained = tmp_path / "retained"
    first = correlation.stage_candidate(
        spec, first_root, tmp_path / "out-one", retained_cache_root=retained
    )
    assert work.builds == 1
    first_key = correlation._input_key(spec, first_root)
    index_before = _tree(retained / ".correlation-index")

    shutil.copytree(first_root, second_root)
    later = time.time() + 5 * 365 * 86400
    for path in second_root.rglob("*"):
        os.utime(path, (later, later))
    monkeypatch.setattr(time, "time", lambda: later)
    monkeypatch.setattr(time, "time_ns", lambda: int(later * 1e9))
    second_spec = second_root / "spec.json"
    assert second_spec.read_bytes() == spec.read_bytes()
    assert second_spec.stat().st_mtime != spec.stat().st_mtime
    assert correlation._input_key(second_spec, second_root) == first_key

    work.builds = 0
    second = correlation.stage_candidate(
        second_spec, second_root, tmp_path / "out-two", retained_cache_root=retained
    )
    published = correlation.publish(
        second_spec, second_root, tmp_path / "out-two", retained_cache_root=retained
    )

    assert second["reused"] is True and published["reused"] is True
    assert work.builds == 0
    assert second["generation_id"] == first["generation_id"]
    assert published["generation_id"] == first["generation_id"]
    assert _tree(retained / ".correlation-index") == index_before
    for name in ("correlation.json", "generation.json"):
        assert (Path(str(second["candidate"])) / name).read_bytes() == (
            Path(str(first["candidate"])) / name
        ).read_bytes()

"""Rung (b): the deepest generated path stays under the Windows limit."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    REFUSAL_SECONDS,
)

pytestmark = pytest.mark.real_attended_path_budget

# ---------------------------------------------------------------- (b) path budget


def _root_of_length(prefix: str, length: int) -> Path:
    """A Windows-shaped root string of exactly ``length`` characters."""
    pad = length - len(prefix)
    assert pad > 0
    return Path(prefix + "x" * pad)


def test_path_budget_refuses_the_recorded_261_character_case():
    _, tail = materializer.longest_generated_tail()
    root = _root_of_length("C:\\Dev\\scratch\\", 261 - len(tail) - 1)
    started = time.monotonic()
    with pytest.raises(materializer.MaterializationError, match=r"261 characters"):
        materializer._attended_path_budget(root, Path("C:\\Dev\\scratch\\c"))
    assert time.monotonic() - started < REFUSAL_SECONDS


def test_path_budget_boundary_259_passes_260_refuses():
    _, tail = materializer.longest_generated_tail()
    cache = Path("C:\\Dev\\scratch\\c")
    materializer._attended_path_budget(
        _root_of_length("C:\\Dev\\scratch\\", 259 - len(tail) - 1), cache
    )
    with pytest.raises(materializer.MaterializationError, match=r"260 characters"):
        materializer._attended_path_budget(
            _root_of_length("C:\\Dev\\scratch\\", 260 - len(tail) - 1), cache
        )


def test_path_budget_refuses_a_long_cache_root_too():
    with pytest.raises(materializer.MaterializationError, match=r"retained cache"):
        materializer._attended_path_budget(
            Path("C:\\Dev\\scratch\\o"), _root_of_length("C:\\Dev\\scratch\\", 240)
        )


def test_path_budget_admits_the_supported_short_roots():
    materializer._attended_path_budget(
        Path("C:\\Dev\\scratch\\quant_engine\\runs\\r1\\out"),
        Path("C:\\Dev\\scratch\\quant_engine\\runs\\r1\\cache"),
    )


def _preflight_args(base: Path, repo: Path):
    (base / "workspace" / "dataPortfolio").mkdir(parents=True)
    (base / "cache").mkdir()
    return materializer._parser().parse_args(
        [
            "attended",
            "--repo-root", str(repo),
            "--python", sys.executable,
            "--workspace-root", str(base / "workspace"),
            "--output-root", str(base / "out"),
            "--retained-cache-root", str(base / "cache"),
            "--requested-as-of", "2026-09-17",
            "--source-run", str(base / "source-run.json"),
        ]
    )  # fmt: skip


def test_basic_preflight_refuses_a_deep_root_and_admits_a_short_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        materializer, "_runtime_identity", lambda python, repo: {"repo": str(repo)}
    )
    repo = materializer.REPOSITORY_ROOT
    deep = tmp_path / ("d" * 60)
    started = time.monotonic()
    with pytest.raises(materializer.MaterializationError, match="Windows limit"):
        materializer._attended_basic_preflight(_preflight_args(deep, repo))
    assert time.monotonic() - started < REFUSAL_SECONDS
    scratch = Path(r"C:\Dev\scratch\quant_engine")
    if not scratch.is_dir():
        pytest.skip("no short scratch root on this machine")
    short = Path(tempfile.mkdtemp(prefix="pb", dir=scratch))
    try:
        basic = materializer._attended_basic_preflight(_preflight_args(short, repo))
        assert basic["output_root"] == (short / "out").resolve()
    finally:
        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.parametrize(
    "output_root",
    [r"C:\Dev\q\out", r"C:\Dev\q\mvp-final-20261004-r2\out"],
)
def test_real_supported_roots_pass_the_budget(output_root):
    materializer._attended_path_budget(Path(output_root), Path(r"C:\Dev\q\cache"))


# ------------------------------------- what the production chain writes in the cache


def _relative_tail(path: Path, root: Path) -> str:
    return "\\".join(path.relative_to(root).parts)


def test_cache_tails_are_the_paths_the_production_chain_really_writes(tmp_path):
    """Both new tails come from the writers' own path functions, not from a copy."""

    import portfolio_financial_outcomes as outcomes
    from quant_engine.core import pipeline as core_pipeline

    root = tmp_path.resolve()
    tails = dict(materializer._cache_tails())

    replay = outcomes._replay_receipt_path(SimpleNamespace(state_root=root), "0" * 64)
    assert replay is not None
    assert tails["financial replay receipt"] == _relative_tail(replay, root)

    node_id = max(
        (f"publish-{root_id}-dynamic-fls" for root_id in materializer.PRODUCTION_ROOTS),
        key=len,
    )
    run_state = SimpleNamespace(
        state_root=root,
        pipeline_id="p" * materializer.PRODUCTION_PIPELINE_ID_BUDGET,
        spec_sha256="0" * 64,
    )
    receipt = core_pipeline._receipt_path(run_state, node_id)
    assert tails["production run receipt"] == _relative_tail(receipt, root)


def test_cache_budget_boundary_is_set_by_the_production_run_receipt_tail():
    stage, tail = max(materializer._cache_tails(), key=lambda item: len(item[1]))
    assert stage == "production run receipt"
    output = Path("C:\\Dev\\scratch\\o")
    materializer._attended_path_budget(
        output, _root_of_length("C:\\Dev\\scratch\\", 259 - len(tail) - 1)
    )
    with pytest.raises(
        materializer.MaterializationError,
        match=r"retained cache root .* 260 characters \(production run receipt",
    ):
        materializer._attended_path_budget(
            output, _root_of_length("C:\\Dev\\scratch\\", 260 - len(tail) - 1)
        )


def test_cache_budget_refuses_a_root_that_only_fit_before_the_production_tails():
    """A cache root that held every earlier cache layout can still overflow in production."""

    earlier = max(
        len(tail)
        for stage, tail in materializer._cache_tails()
        if stage not in {"financial replay receipt", "production run receipt"}
    )
    cache = _root_of_length("C:\\Dev\\scratch\\", 259 - earlier - 1)
    # It fit under the previous budget: its longest earlier tail ends at 259.
    assert len(str(cache)) + 1 + earlier == 259
    with pytest.raises(
        materializer.MaterializationError,
        match=r"retained cache root .*\(production run receipt",
    ):
        materializer._attended_path_budget(Path("C:\\Dev\\scratch\\o"), cache)

"""The attended path-budget patch in tests/core/conftest.py is scoped by a marker."""

from __future__ import annotations

from pathlib import Path

import pytest

import portfolio_pipeline_materialize as materializer


def _too_deep_root() -> Path:
    _, tail = materializer.longest_generated_tail()
    return Path("C:\\Dev\\scratch\\" + "x" * (270 - len(tail)))


def test_unmarked_test_sees_the_patched_budget() -> None:
    # Would refuse (the generated path exceeds 260 characters) were the gate real.
    assert (
        materializer._attended_path_budget(_too_deep_root(), Path("C:\\Dev\\c")) is None
    )


@pytest.mark.real_attended_path_budget
def test_marked_test_sees_the_real_gate() -> None:
    with pytest.raises(materializer.MaterializationError, match="Windows limit"):
        materializer._attended_path_budget(_too_deep_root(), Path("C:\\Dev\\c"))


def test_a_name_containing_preflight_ladder_no_longer_opts_out_of_the_patch() -> None:
    # The old scope was a nodeid substring; only the marker opts in now.
    assert (
        materializer._attended_path_budget(_too_deep_root(), Path("C:\\Dev\\c")) is None
    )

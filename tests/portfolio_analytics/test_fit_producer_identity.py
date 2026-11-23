"""The retained fit cache is keyed on the fit code and numeric stack, not the whole file.

An edit elsewhere in ``portfolio_decomposition.py`` (a loader, a guard, a report
field) or in ``uv.lock`` must keep the retained fits valid; an edit to the fit
code, or a change of numeric library, must not.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import sys
from typing import Any

import pytest

import test_fp_decomposition_reuse as reuse
import test_portfolio_decomposition as base

decomposition = base.decomposition
work = reuse.work  # fixture re-export


def _source() -> str:
    return Path(decomposition.__file__).read_text(encoding="utf-8")


def _edit(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, old
    return source.replace(old, new)


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s + "\n# a trailing comment\n",
        lambda s: _edit(
            s,
            "def _check_risk_free(rows: Mapping[date, FactorRow]) -> None:\n",
            "def _check_risk_free(rows: Mapping[date, FactorRow]) -> None:\n"
            '    """Reworded docstring."""\n',
        ),
        lambda s: _edit(s, "RISK_FREE_DAILY_MAX = 0.005", "RISK_FREE_DAILY_MAX = 0.01"),
        lambda s: _edit(
            s, "    last_day = max(rows) if rows else None\n", "    last_day = None\n"
        ),
        lambda s: _edit(
            s,
            '    return {"status": "FACTORS_FRESH", "series_id": model.series_id}',
            '    return {"status": "FACTORS_FRESH", "series_id": model.series_id, "x": 1}',
        ),
    ],
    ids=["comment", "docstring", "loader-constant", "loader-body", "report-field"],
)
def test_an_edit_outside_the_fit_code_keeps_the_identity(edit: Any) -> None:
    source = _source()
    assert decomposition._fit_source_identity(
        edit(source)
    ) == decomposition._fit_source_identity(source)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "(ridge + neighbor_penalty) * identity",
            "(ridge * neighbor_penalty) * identity",
        ),
        ("if abs(denominator) <= 1e-12:", "if abs(denominator) <= 1e-11:"),
        (
            "solution[-1] = transformed_right[-1]",
            "solution[-1] = transformed_right[-1] * 1.0",
        ),
        (
            "def _predict(fit: Fit, matrix: FloatArray)",
            "def _predict(fit: Fit, matrix: FloatArray, x=0)",
        ),
    ],
    ids=["fit-line", "fit-threshold", "helper-body", "unreached-predict"],
)
def test_an_edit_to_the_fit_code_changes_the_identity(old: str, new: str) -> None:
    source = _source()
    changed = source.replace(old, new, 1)
    assert changed != source
    if old.startswith("def _predict"):
        # _predict is read back after a hit, never part of what is cached
        assert decomposition._fit_source_identity(
            changed
        ) == decomposition._fit_source_identity(source)
    else:
        assert decomposition._fit_source_identity(
            changed
        ) != decomposition._fit_source_identity(source)


def test_an_edit_to_a_constant_or_class_the_fit_uses_changes_the_identity() -> None:
    source = _source()
    changed = _edit(
        source,
        "FloatArray = npt.NDArray[np.float64]",
        "FloatArray = npt.NDArray[np.float32]",
    )
    assert decomposition._fit_source_identity(
        changed
    ) != decomposition._fit_source_identity(source)


@pytest.mark.parametrize("package", ["numpy", "scipy", "scikit-learn"])
def test_a_numeric_library_version_change_changes_the_identity(
    monkeypatch: pytest.MonkeyPatch, package: str
) -> None:
    before = decomposition._fit_producer_identity()
    real = metadata.version

    def version(name: str) -> str:
        return "99.0" if name == package else real(name)

    monkeypatch.setattr(metadata, "version", version)
    assert decomposition._fit_producer_identity() != before


def test_a_python_minor_version_change_changes_the_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = decomposition._fit_producer_identity()
    monkeypatch.setattr(sys, "version_info", (3, 99, 0, "final", 0))
    assert decomposition._fit_producer_identity() != before


def test_the_lock_is_not_part_of_the_fit_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = decomposition._fit_producer_identity()
    reuse._alter_lock(tmp_path / "lock", monkeypatch)
    assert decomposition._fit_producer_identity() == before


def test_an_unrelated_producer_edit_hits_the_fits_and_matches_a_cold_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Any
) -> None:
    """Whole-file bytes change -> a new generation, but every fit is a cache hit."""

    source_root = tmp_path / "source"
    target_path, days = reuse._write_dynamic(source_root)
    spec = base._spec(
        source_root, target_path, days, models=[base._dynamic_model(days)]
    )
    retained = tmp_path / "retained"
    first = decomposition.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    assert work.dynamic_fits > 0
    key = decomposition._input_key(spec, source_root)

    reuse._alter_producer(tmp_path / "altered", monkeypatch)  # appends a comment
    assert decomposition._input_key(spec, source_root) != key
    work.reset()
    warm = decomposition.stage_candidate(
        spec, source_root, tmp_path / "warm", retained_cache_root=retained
    )
    assert warm["reused"] is False
    assert work.builds == 1
    assert work.dynamic_fits == 0 and work.static_fits == 0

    work.reset()
    cold = decomposition.stage_candidate(
        spec,
        source_root,
        tmp_path / "cold",
        retained_cache_root=tmp_path / "empty",
    )
    assert work.dynamic_fits > 0
    assert warm["generation_id"] == cold["generation_id"]
    warm_bytes = (Path(warm["candidate"]) / "decomposition.json").read_bytes()
    cold_bytes = (Path(cold["candidate"]) / "decomposition.json").read_bytes()
    assert warm_bytes == cold_bytes
    assert first["generation_id"] == cold["generation_id"]


def test_a_numeric_stack_change_refits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Any
) -> None:
    source_root = tmp_path / "source"
    target_path, days = reuse._write_dynamic(source_root)
    spec = base._spec(
        source_root, target_path, days, models=[base._dynamic_model(days)]
    )
    retained = tmp_path / "retained"
    decomposition.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    # the result-level key is the whole-file one: move it so only the fit level decides
    reuse._alter_producer(tmp_path / "altered", monkeypatch)
    work.reset()
    real = decomposition._numeric_stack_identity
    monkeypatch.setattr(
        decomposition, "_numeric_stack_identity", lambda: {**real(), "numpy": "99.0"}
    )
    decomposition.stage_candidate(
        spec, source_root, tmp_path / "second", retained_cache_root=retained
    )
    assert work.dynamic_fits > 0


# ---------------------------------------------------------------------------
# A retained fit is never served to a call that asks for other parameters
# ---------------------------------------------------------------------------


def _first_run_then_new_result_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Any
) -> tuple[Any, Path, Path, Path]:
    source_root = tmp_path / "source"
    target_path, days = reuse._write_dynamic(source_root)
    spec = base._spec(
        source_root, target_path, days, models=[base._dynamic_model(days)]
    )
    retained = tmp_path / "retained"
    decomposition.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    assert work.dynamic_fits > 0 and work.static_fits > 0
    # move the whole-file result key so only the fit level decides what is reused
    reuse._alter_producer(tmp_path / "altered", monkeypatch)
    work.reset()
    return spec, source_root, retained, tmp_path / "second"


def _wrap(monkeypatch: pytest.MonkeyPatch, name: str, **change: Any) -> None:
    """Mimic an edit of the call in ``_dynamic_model_result`` (not in the key)."""

    real = getattr(decomposition, name)

    def changed(*args: Any, **kwargs: Any) -> Any:
        for key, edit in change.items():
            if key in kwargs:
                kwargs[key] = edit(kwargs[key])
        return real(*args, **kwargs)

    monkeypatch.setattr(decomposition, name, changed)


@pytest.mark.parametrize(
    ("function", "change", "expect"),
    [
        ("_dynamic_loo_score", {"ridge": lambda value: value * 1000.0}, "dynamic"),
        ("_dynamic_loo_score", {"smoothness": lambda value: value * 2.0}, "dynamic"),
        ("_benchmark_fit", {"constraint": lambda value: "LONG_ONLY"}, "static"),
        ("_benchmark_fit", {"ridge": lambda value: value * 2.0}, "static"),
    ],
    ids=[
        "M1-dynamic-ridge",
        "M4-smoothness",
        "M2-static-constraint",
        "M3-static-ridge",
    ],
)
def test_a_changed_fit_argument_is_a_miss_not_a_stale_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work: Any,
    function: str,
    change: dict[str, Any],
    expect: str,
) -> None:
    spec, source_root, retained, out = _first_run_then_new_result_key(
        tmp_path, monkeypatch, work
    )
    _wrap(monkeypatch, function, **change)
    decomposition.stage_candidate(spec, source_root, out, retained_cache_root=retained)
    assert (work.dynamic_fits if expect == "dynamic" else work.static_fits) > 0


def test_unchanged_arguments_still_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Any
) -> None:
    spec, source_root, retained, out = _first_run_then_new_result_key(
        tmp_path, monkeypatch, work
    )
    decomposition.stage_candidate(spec, source_root, out, retained_cache_root=retained)
    assert work.dynamic_fits == 0 and work.static_fits == 0


def test_a_fit_unresolvable_name_is_an_error() -> None:
    source = _source()
    changed = _edit(
        source,
        "        rank = int(np.linalg.matrix_rank(design))\n",
        "        rank = int(np.linalg.matrix_rank(design)) + some_undefined_name\n",
    )
    with pytest.raises(decomposition.DecompositionError, match="cannot resolve"):
        decomposition._fit_source_identity(changed)


def test_the_walk_follows_unpacked_conditional_augmented_and_async_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = (
        "import math\n"
        "if True:\n"
        "    A, B = {a}, 2\n"
        "C = 1\n"
        "C += {c}\n"
        "try:\n"
        "    D = {d}\n"
        "except ImportError:\n"
        "    D = 0\n"
        "async def helper():\n"
        "    return {h}\n"
        "def f(x):\n"
        "    return math.floor(x) + A + B + C + D\n"
        "def g():\n"
        "    return helper\n"
    )
    monkeypatch.setattr(decomposition, "_FIT_CACHE_ROOTS", ("f", "g"))
    base_values = {"a": 1, "c": 1, "d": 1, "h": 1}
    reference = decomposition._fit_source_identity(template.format(**base_values))
    for name in base_values:
        changed = {**base_values, name: 9}
        assert (
            decomposition._fit_source_identity(template.format(**changed)) != reference
        ), name

"""FINAL-PLAN FP:176-179 regression tests for decomposition reuse.

Each test counts the work the producer really does (``build_decomposition``,
``_dynamic_fit`` and ``_fit`` are wrapped, never replaced) and compares every
reused or recomputed generation byte for byte with a cold derivation made in an
empty output root with no retained cache.
"""

from __future__ import annotations

import csv
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable

import pytest

import test_portfolio_decomposition as base

decomposition = base.decomposition


class Work:
    """Counts of the numerical work done through the real producer."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.builds = 0
        self.dynamic_fits = 0
        self.static_fits = 0


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> Work:
    counted = Work()
    real_build = decomposition.build_decomposition
    real_dynamic = decomposition._dynamic_fit
    real_static = decomposition._fit

    def build(*args: Any, **kwargs: Any) -> Any:
        counted.builds += 1
        return real_build(*args, **kwargs)

    def dynamic_fit(*args: Any, **kwargs: Any) -> Any:
        counted.dynamic_fits += 1
        return real_dynamic(*args, **kwargs)

    def static_fit(*args: Any, **kwargs: Any) -> Any:
        counted.static_fits += 1
        return real_static(*args, **kwargs)

    monkeypatch.setattr(decomposition, "build_decomposition", build)
    monkeypatch.setattr(decomposition, "_dynamic_fit", dynamic_fit)
    monkeypatch.setattr(decomposition, "_fit", static_fit)
    return counted


def _write_dynamic(
    root: Path, *, count: int = 150, phase: float = 0.0
) -> tuple[Path, list[date]]:
    """Dynamic-fit sources whose first rows never depend on ``count``.

    Appending rows therefore leaves every earlier byte unchanged, which is what an
    evaluation-only extension of a real history does.
    """

    root.mkdir(parents=True)
    days = base._days(count)
    target_path = root / "targets.csv"
    factor_path = root / "factors.csv"
    with (
        target_path.open("w", encoding="utf-8", newline="") as target_handle,
        factor_path.open("w", encoding="utf-8", newline="") as factor_handle,
    ):
        target_writer = csv.writer(target_handle)
        factor_writer = csv.writer(factor_handle)
        target_writer.writerow(decomposition.TARGET_HEADERS)
        factor_writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.012 * math.sin(index / 3.7 + phase) + 0.004 * math.cos(index / 13.0)
            f2 = 0.010 * math.cos(index / 5.1 + phase) - 0.003 * math.sin(index / 17.0)
            beta_one = 0.15 + 0.70 * index / 149
            beta_two = -0.35 + 0.30 * math.sin(index / 28.0)
            risk_free = 0.00005
            excess = 0.00015 + beta_one * f1 + beta_two * f2
            target_writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + risk_free, ".17g"),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "",
                ]
            )
            factor_writer.writerow(
                [
                    day.isoformat(),
                    format(risk_free, ".17g"),
                    format(f1, ".17g"),
                    format(f2, ".17g"),
                ]
            )
    return target_path, days


def _shift_boundary(model: dict[str, object], days: list[date], end: int) -> dict:
    moved = dict(model)
    moved["train_end"] = days[end].isoformat()
    moved["test_start"] = days[end + 1].isoformat()
    return moved


def _tree(root: Path) -> dict[str, str]:
    """Relative path to sha256 for every file below ``root``."""

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _entry(retained: Path, key: str, generation_id: str) -> dict:
    """The index entry and generation directory a stage left for ``key``."""

    index = retained / ".decomposition-index" / f"{key}.json"
    return {
        "index": index.read_bytes(),
        "generation": _tree(retained / "candidates" / generation_id),
    }


def _result(staged: dict[str, object]) -> dict[str, Any]:
    return json.loads(
        (Path(str(staged["candidate"])) / "decomposition.json").read_bytes()
    )


def _selected(staged: dict[str, object]) -> dict[str, Any]:
    return next(
        row for row in _result(staged)["model_results"] if row["selected_for_series"]
    )


def _same_bytes(left: dict[str, object], right: dict[str, object]) -> None:
    for name in ("decomposition.json", "generation.json"):
        assert (Path(str(left["candidate"])) / name).read_bytes() == (
            Path(str(right["candidate"])) / name
        ).read_bytes()
    assert left["generation_id"] == right["generation_id"]


# ---------------------------------------------------------------------------
# M1  FP:177
# ---------------------------------------------------------------------------


def test_changed_source_recomputes_only_its_own_generation_and_matches_cold(
    tmp_path: Path, work: Work
) -> None:
    """One changed training row recomputes its own node, never an unrelated one."""

    retained = tmp_path / "retained"
    a_root, b_root = tmp_path / "source-a", tmp_path / "source-b"
    a_target, days = _write_dynamic(a_root, phase=0.0)
    b_target, _ = _write_dynamic(b_root, phase=0.7)
    model = base._dynamic_model(days)
    candidates = len(model["smoothness_candidates"])  # type: ignore[arg-type]
    a_spec0 = base._spec(a_root, a_target, days, models=[model])
    b_spec = base._spec(b_root, b_target, days, models=[model])

    a0 = decomposition.stage_candidate(
        a_spec0, a_root, tmp_path / "a0", retained_cache_root=retained
    )
    b0 = decomposition.stage_candidate(
        b_spec, b_root, tmp_path / "b0", retained_cache_root=retained
    )
    assert a0["reused"] is False and b0["reused"] is False
    assert work.builds == 2 and work.dynamic_fits == 2 * candidates
    a_key0 = decomposition._input_key(a_spec0, a_root)
    b_key = decomposition._input_key(b_spec, b_root)
    a0_entry = _entry(retained, a_key0, str(a0["generation_id"]))
    b0_entry = _entry(retained, b_key, str(b0["generation_id"]))
    a_spec0_bytes = a_spec0.read_bytes()

    # One TRAINING row (index 10, well before train_end) of source A only.
    rows = list(csv.reader(a_target.open(newline="", encoding="utf-8")))
    rows[11][2] = format(float(rows[11][2]) + 0.000001, ".17g")
    with a_target.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    a_spec1 = base._spec(a_root, a_target, days, models=[model])
    assert a_spec1.read_bytes() != a_spec0_bytes
    assert decomposition._input_key(a_spec1, a_root) != a_key0

    work.reset()
    a1 = decomposition.stage_candidate(
        a_spec1, a_root, tmp_path / "a1", retained_cache_root=retained
    )
    b1 = decomposition.stage_candidate(
        b_spec, b_root, tmp_path / "b1", retained_cache_root=retained
    )

    assert a1["reused"] is False
    assert b1["reused"] is True
    assert b1["generation_id"] == b0["generation_id"]
    assert a1["generation_id"] != a0["generation_id"]
    assert work.builds == 1
    assert work.dynamic_fits == candidates
    assert _selected(a1)["training_identity"] != _selected(a0)["training_identity"]

    work.reset()
    cold = decomposition.stage_candidate(a_spec1, a_root, tmp_path / "cold")
    assert work.builds == 1 and work.dynamic_fits == candidates
    _same_bytes(a1, cold)

    # The superseded generation and the untouched neighbour stay byte-identical.
    assert _entry(retained, a_key0, str(a0["generation_id"])) == a0_entry
    assert _entry(retained, b_key, str(b0["generation_id"])) == b0_entry


# ---------------------------------------------------------------------------
# M2a + M2b  FP:178
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "extra_rows", "moved_end", "training_reused"),
    [
        ("evaluation_rows_appended", 8, None, True),
        ("training_boundary_moved", 0, 109, False),
        ("rows_appended_and_boundary_moved", 8, 109, False),
    ],
)
def test_evaluation_extension_reuses_training_only_if_boundary_unchanged(
    tmp_path: Path,
    work: Work,
    case: str,
    extra_rows: int,
    moved_end: int | None,
    training_reused: bool,
) -> None:
    """Training is reused across an appended extension and refit when it moves."""

    retained = tmp_path / "retained"
    first_root = tmp_path / "first"
    first_target, days = _write_dynamic(first_root)
    model = base._dynamic_model(days)
    candidates = len(model["smoothness_candidates"])  # type: ignore[arg-type]
    first_spec = base._spec(first_root, first_target, days, models=[model])
    first = decomposition.stage_candidate(
        first_spec, first_root, tmp_path / "out-first", retained_cache_root=retained
    )
    assert work.dynamic_fits == candidates

    second_root = tmp_path / "second"
    second_target, longer = _write_dynamic(second_root, count=150 + extra_rows)
    assert longer[:150] == days
    second_model = (
        model if moved_end is None else _shift_boundary(model, longer, moved_end)
    )
    second_spec = base._spec(second_root, second_target, longer, models=[second_model])
    assert second_spec.read_bytes() != first_spec.read_bytes()

    work.reset()
    second = decomposition.stage_candidate(
        second_spec, second_root, tmp_path / "out-second", retained_cache_root=retained
    )
    assert second["reused"] is False
    assert second["generation_id"] != first["generation_id"]
    assert work.builds == 1
    assert work.dynamic_fits == (0 if training_reused else candidates)
    before, after = _selected(first), _selected(second)
    assert (before["training_identity"] == after["training_identity"]) is (
        training_reused
    )
    assert before["evaluation_identity"] != after["evaluation_identity"]

    work.reset()
    cold = decomposition.stage_candidate(second_spec, second_root, tmp_path / "cold")
    assert work.builds == 1 and work.dynamic_fits == candidates
    _same_bytes(second, cold)


# ---------------------------------------------------------------------------
# M2c  FP:178, static models (ruling: a refit is accepted and asserted)
# ---------------------------------------------------------------------------


def test_static_evaluation_extension_refits_identical_training_and_matches_cold(
    tmp_path: Path, work: Work
) -> None:
    """Static estimators refit on an extension, but training is untouched."""

    retained = tmp_path / "retained"
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_target, days = base._write_sources(first_root, count=140)
    model = base._model(days)
    first_spec = base._spec(first_root, first_target, days, models=[model])
    first = decomposition.stage_candidate(
        first_spec, first_root, tmp_path / "out-first", retained_cache_root=retained
    )
    initial_fits = work.static_fits
    assert initial_fits > 0 and work.dynamic_fits == 0

    second_target, longer = base._write_sources(second_root, count=152)
    assert longer[:140] == days
    second_spec = base._spec(second_root, second_target, longer, models=[model])
    work.reset()
    second = decomposition.stage_candidate(
        second_spec, second_root, tmp_path / "out-second", retained_cache_root=retained
    )

    # A refit happened, and it is exactly the work the code does for the
    # eligible candidates: (cv folds + one candidate fit) each, plus the final fit.
    assert second["reused"] is False
    eligible = [
        item
        for item in _selected(second)["ridge_sensitivity"]
        if item["eligibility"] == "ELIGIBLE" and item["cv_predicted_r_squared"]
    ]
    assert work.static_fits > 0
    assert work.static_fits == len(eligible) * (model["cv_folds"] + 1) + 1
    assert work.static_fits == initial_fits
    assert work.dynamic_fits == 0

    before, after = _selected(first), _selected(second)
    assert after["training_identity"] == before["training_identity"]
    assert after["selected_ridge"] == before["selected_ridge"]
    # The training coefficients (not the holdout-dependent contribution fields).
    assert [
        (item["factor_id"], item["beta"], item["exposure"], item["standard_error"])
        for item in after["exposures"]
    ] == [
        (item["factor_id"], item["beta"], item["exposure"], item["standard_error"])
        for item in before["exposures"]
    ]
    assert after["alpha"] == before["alpha"]
    assert after["evaluation_identity"] != before["evaluation_identity"]

    work.reset()
    cold = decomposition.stage_candidate(second_spec, second_root, tmp_path / "cold")
    assert work.builds == 1 and work.static_fits == initial_fits
    _same_bytes(second, cold)


# ---------------------------------------------------------------------------
# M3a  FP:179, rejection performs no build and preserves index and generation
# ---------------------------------------------------------------------------


def _staged_static(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    source_root = tmp_path / "source"
    target_path, days = base._write_sources(source_root, complete_costs=True)
    spec = base._spec(
        source_root,
        target_path,
        days,
        models=[base._model(days, cost_coverage="COMPLETE")],
    )
    retained = tmp_path / "retained"
    staged = decomposition.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    return source_root, spec, retained, staged


def _spoil_changed_source(source_root: Path, spec: Path, staged: dict) -> None:
    target = source_root / "targets.csv"
    target.write_bytes(target.read_bytes() + b"\n")


def _spoil_deleted_source(source_root: Path, spec: Path, staged: dict) -> None:
    (source_root / "factors.csv").unlink()


def _spoil_extra_file(source_root: Path, spec: Path, staged: dict) -> None:
    (Path(str(staged["candidate"])) / "unexpected.txt").write_text("x", "utf-8")


def _spoil_wrong_generation(source_root: Path, spec: Path, staged: dict) -> None:
    retained = Path(str(staged["candidate"])).parents[1]
    other = json.loads(spec.read_text("utf-8"))
    other["analysis_lens"] = "ETF_STYLE"
    other_spec = spec.parent / "spec-b.json"
    other_spec.write_text(
        json.dumps(other, sort_keys=True, separators=(",", ":")) + "\n", "utf-8"
    )
    staged_b = decomposition.stage_candidate(
        other_spec, source_root, spec.parent / "b-output", retained_cache_root=retained
    )
    key = decomposition._input_key(spec, source_root)
    b_bytes = (Path(str(staged_b["candidate"])) / "decomposition.json").read_bytes()
    (retained / ".decomposition-index" / f"{key}.json").write_bytes(
        decomposition.canonical_json_bytes(
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
        (_spoil_changed_source, (decomposition.DecompositionError,)),
        (_spoil_deleted_source, (decomposition.DecompositionError, OSError)),
        (_spoil_extra_file, (decomposition.DecompositionError,)),
        (_spoil_wrong_generation, (decomposition.DecompositionError,)),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_rejected_reuse_builds_nothing_and_preserves_index_and_generation(
    tmp_path: Path,
    work: Work,
    spoil: Callable[[Path, Path, dict], None],
    error: tuple[type[BaseException], ...],
) -> None:
    source_root, spec, retained, staged = _staged_static(tmp_path)
    spoil(source_root, spec, staged)
    work.reset()
    before = _tree(retained)
    output = tmp_path / "rejected-output"

    with pytest.raises(error):
        decomposition.stage_candidate(
            spec, source_root, output, retained_cache_root=retained
        )
    with pytest.raises(error):
        decomposition.publish(spec, source_root, output, retained_cache_root=retained)

    assert (work.builds, work.dynamic_fits, work.static_fits) == (0, 0, 0)
    assert _tree(retained) == before
    assert not output.exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING FP:179: a corrupt retained decomposition.json is not rejected "
        "before fitting; _indexed_generation returns None on the hash mismatch and "
        "stage_candidate re-solves (tools/portfolio_decomposition.py:4487-4495 then "
        ":4541-4546) before _verify_candidate rejects it"
    ),
)
def test_corrupt_retained_result_is_rejected_before_any_fitting(
    tmp_path: Path, work: Work
) -> None:
    source_root, spec, retained, staged = _staged_static(tmp_path)
    (Path(str(staged["candidate"])) / "decomposition.json").write_bytes(b"{}\n")
    work.reset()
    before = _tree(retained)

    with pytest.raises(decomposition.DecompositionError):
        decomposition.stage_candidate(
            spec, source_root, tmp_path / "out", retained_cache_root=retained
        )

    assert (work.builds, work.dynamic_fits, work.static_fits) == (0, 0, 0)
    assert _tree(retained) == before


# ---------------------------------------------------------------------------
# M3b  FP:179, the key binds methodology and helper bytes
# ---------------------------------------------------------------------------


def _alter_methodology(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    value = json.loads(decomposition.METHODOLOGY_PATH.read_bytes())
    value["test_consumed_methodology_revision"] = "changed"
    tmp.mkdir()
    changed = tmp / "methodology.json"
    changed.write_bytes(decomposition.canonical_json_bytes(value))
    monkeypatch.setattr(decomposition, "METHODOLOGY_PATH", changed)
    return "methodology"


def _alter_helper(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    # A helper whose defining file is not the shipped one: its bytes differ.
    original = decomposition.canonical_json_bytes

    def replacement(value: object) -> bytes:
        return original(value)

    monkeypatch.setattr(decomposition, "canonical_json_bytes", replacement)
    return "canonical_json"


def _alter_producer(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    copied = tmp / "tools" / "portfolio_decomposition.py"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(Path(decomposition.__file__).read_bytes() + b"\n# changed\n")
    shutil.copyfile(
        Path(decomposition.__file__).resolve().parents[1] / "uv.lock", tmp / "uv.lock"
    )
    monkeypatch.setattr(decomposition, "__file__", str(copied))
    return "producer"


def _alter_lock(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    producer = tmp / "tools" / "portfolio_decomposition.py"
    producer.parent.mkdir(parents=True)
    producer.write_bytes(Path(decomposition.__file__).read_bytes())
    (tmp / "uv.lock").write_bytes(b"# a different dependency lock\n")
    monkeypatch.setattr(decomposition, "__file__", str(producer))
    return "dependency_lock"


@pytest.mark.parametrize(
    "alter",
    [_alter_methodology, _alter_helper, _alter_producer, _alter_lock],
    ids=lambda value: value.__name__,
)
def test_decomposition_input_key_binds_consumed_methodology_and_helper_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alter: Callable[..., str]
) -> None:
    source_root = tmp_path / "source"
    target_path, days = base._write_sources(source_root)
    spec = base._spec(source_root, target_path, days)
    key = decomposition._input_key(spec, source_root)
    identity = decomposition._consumed_producer_identity()
    assert decomposition._input_key(spec, source_root) == key
    assert identity == {
        "canonical_json": hashlib.sha256(
            Path(decomposition.canonical_json_bytes.__code__.co_filename).read_bytes()
        ).hexdigest(),
        "dependency_lock": hashlib.sha256(
            (Path(decomposition.__file__).resolve().parents[1] / "uv.lock").read_bytes()
        ).hexdigest(),
        "methodology": hashlib.sha256(
            decomposition.METHODOLOGY_PATH.read_bytes()
        ).hexdigest(),
        "producer": hashlib.sha256(
            Path(decomposition.__file__).read_bytes()
        ).hexdigest(),
    }

    component = alter(tmp_path / "altered", monkeypatch)
    changed = decomposition._consumed_producer_identity()

    assert decomposition._input_key(spec, source_root) != key
    assert {name for name in identity if identity[name] != changed[name]} == {component}


def test_methodology_change_is_not_served_from_the_retained_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Work
) -> None:
    """The retained generation survives a methodology change but is not reused."""

    source_root = tmp_path / "source"
    target_path, days = base._write_sources(source_root, complete_costs=True)
    spec = base._spec(
        source_root,
        target_path,
        days,
        models=[base._model(days, cost_coverage="COMPLETE")],
    )
    retained = tmp_path / "retained"
    first = decomposition.stage_candidate(
        spec, source_root, tmp_path / "first", retained_cache_root=retained
    )
    key = decomposition._input_key(spec, source_root)
    original = _entry(retained, key, str(first["generation_id"]))
    _alter_methodology(tmp_path / "altered", monkeypatch)
    work.reset()

    second = decomposition.stage_candidate(
        spec, source_root, tmp_path / "second", retained_cache_root=retained
    )

    assert second["reused"] is False
    assert work.builds == 1
    assert second["generation_id"] != first["generation_id"]
    assert _entry(retained, key, str(first["generation_id"])) == original


# ---------------------------------------------------------------------------
# M5  FP:176, same bytes at another source root and clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estimator", ["static", "dynamic"])
def test_same_bytes_other_source_root_and_clock_reuse_with_zero_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: Work, estimator: str
) -> None:
    first_root = tmp_path / "root-one" / "deeper" / "source"
    second_root = tmp_path / "other-root" / "source"
    if estimator == "static":
        target, days = base._write_sources(first_root, complete_costs=True)
        models = [base._model(days, cost_coverage="COMPLETE")]
    else:
        target, days = _write_dynamic(first_root)
        models = [base._dynamic_model(days)]
    spec = base._spec(first_root, target, days, models=models)
    retained = tmp_path / "retained"

    first = decomposition.stage_candidate(
        spec, first_root, tmp_path / "out-one", retained_cache_root=retained
    )
    assert work.builds == 1
    first_key = decomposition._input_key(spec, first_root)
    index_before = _tree(retained / ".decomposition-index")

    shutil.copytree(first_root, second_root)
    later = time.time() + 5 * 365 * 86400
    for path in second_root.rglob("*"):
        os.utime(path, (later, later))
    monkeypatch.setattr(time, "time", lambda: later)
    monkeypatch.setattr(time, "time_ns", lambda: int(later * 1e9))
    second_spec = second_root / "spec.json"
    assert second_spec.read_bytes() == spec.read_bytes()
    assert second_root.resolve() != first_root.resolve()
    assert second_spec.stat().st_mtime != spec.stat().st_mtime
    assert decomposition._input_key(second_spec, second_root) == first_key

    work.reset()
    second = decomposition.stage_candidate(
        second_spec, second_root, tmp_path / "out-two", retained_cache_root=retained
    )
    published = decomposition.publish(
        second_spec, second_root, tmp_path / "out-two", retained_cache_root=retained
    )

    assert second["reused"] is True and published["reused"] is True
    assert (work.builds, work.dynamic_fits, work.static_fits) == (0, 0, 0)
    assert second["generation_id"] == first["generation_id"]
    assert published["generation_id"] == first["generation_id"]
    assert _tree(retained / ".decomposition-index") == index_before
    _same_bytes(second, first)

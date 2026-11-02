"""The account-lane lens control run of a restated release (#624).

A release that declares a strategy basis runs the same decomposition plan a second time on
the parent-account series (the control) and derives the lens-moves table from the two runs'
published generations.  The table is data, so a retained one would be stale: a missing
control refuses, an edited table refuses, and a release without a declaration is untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any

import pytest

import portfolio_release_builder as builder
from test_portfolio_release_builder import (
    _bind_recipe_plan_bundle,
    _current_plan_bundle,
    _recipe,
)

SERIES = "Strategy 0"


@pytest.fixture
def short_root() -> Any:
    """A short scratch root: lens generation paths are long (a 64-hex id below them)."""

    root = Path(tempfile.mkdtemp(prefix="lc"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


MODEL = "m-strict"


def _model_results(status: str, *, lane: bool) -> list[dict[str, Any]]:
    coverage: dict[str, Any] = {
        "aligned_observations": 120,
        "train_observations": 40 if status.startswith("UNAVAILABLE") else 90,
        "test_observations": 30,
        "target_observed_start": "2026-02-02",
        "target_observed_end": "2026-09-17",
        "target_cumulative_return": "0.1543",
        "target_current_drawdown": "0.0092",
        "target_max_drawdown": "0.0674",
    }
    if lane:
        coverage["account_basis"] = {
            "label": "whole account acct incl. the unallocated margin bucket",
            "window": {"start": "2026-01-22", "end": "2026-09-17"},
            "observations": 171,
        }
    return [
        {
            "model_id": MODEL,
            "series_id": SERIES,
            "status": status,
            "coverage": coverage,
        }
    ]


def _spec(*, lane: bool, surface: str) -> dict[str, Any]:
    document: dict[str, Any] = {
        "models": [
            {
                "model_id": MODEL,
                "series_id": SERIES,
                "factor_ids": ["MKT_RF"],
                "minimum_train_observations": 45,
                "minimum_test_observations": 20,
                "cv_folds": 3,
                "estimator": surface,
            }
        ]
    }
    if lane:
        document["series_composition"] = {SERIES: {"account_basis": {"rows": []}}}
    return document


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: bool,
    control: bool,
) -> dict[str, Any]:
    recipe_root = tmp_path / "recipe-fixture"
    recipe_root.mkdir()
    recipe = _recipe(recipe_root)
    recipe_value = json.loads(recipe.read_text(encoding="utf-8"))
    correlation_spec = (
        Path(recipe_value["correlation_source_root"]["path"]) / "spec.json"
    )
    correlation_spec.write_text("{}", encoding="utf-8")
    recipe_value["correlation_source_root"].update(
        builder._directory_inventory(correlation_spec.parent)
    )
    recipe.write_bytes(builder.canonical_json_bytes(recipe_value))
    bundle = _current_plan_bundle(tmp_path)
    control_bundle = tmp_path / "control-plan"
    shutil.copytree(bundle, control_bundle)
    rows = [
        {
            "evidence_mode": "LIVE_ONLY",
            "path": f"inputs/lenses/us-economic-{surface.lower()}.json",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "source_key": "us-economic",
            "spec_id": f"us-economic:{surface}",
            "surface": surface,
        }
        for surface in ("STATIC", "DYNAMIC_FLS")
    ]
    # the lens spec files the table reads: the sleeve lane declares its parent-account lane
    for row in rows:
        for root, lane in ((bundle, True), (control_bundle, False)):
            path = root / str(row["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_spec(lane=lane, surface=str(row["surface"]))),
                encoding="utf-8",
            )
    _bind_recipe_plan_bundle(recipe, bundle)
    # the spec files above are test inputs: the plan's own output verification is exercised
    # by the real builder tests, so the specs are read back as they were listed
    monkeypatch.setattr(
        builder,
        "_current_release_plan_specs",
        lambda _bundle, _plan, **_kwargs: [dict(row) for row in rows],
    )
    recipe_value = json.loads(recipe.read_bytes())
    if declared:
        declaration = tmp_path / "strategy-basis-declaration.json"
        declaration.write_text(
            json.dumps(
                {
                    "schema": "strategy-basis-declaration/1",
                    "strategies": [
                        {"strategy_id": "strategy-0", "basis": "SLEEVE_EPOCH"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        recipe_value["strategy_basis"] = builder._file_definition(declaration)
    if control:
        recipe_value["decomposition_control_plan_bundle"] = {
            "path": str(control_bundle),
            **builder._directory_inventory(control_bundle),
        }
    recipe.write_bytes(builder.canonical_json_bytes(recipe_value))
    calls: list[tuple[str, ...]] = []
    lock = threading.Lock()

    def fake_tool(*args: str, **_kwargs: object) -> dict[str, object]:
        with lock:
            calls.append(tuple(args))
        if args[0] == "portfolio_decomposition.py" and args[1] in {"stage", "publish"}:
            output_root = Path(args[args.index("--output-root") + 1])
            generation_id = hashlib.sha256(str(output_root).encode()).hexdigest()
            if (
                args[1] == "publish"
                and "decomposition-lenses" in str(output_root) + str(output_root.parent)
                or "lens-control" in str(output_root)
            ):
                lane_run = "lens-control" not in str(output_root)
                status = (
                    "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV"
                    if lane_run
                    else "AVAILABLE"
                )
                generation = output_root / "generations" / generation_id
                generation.mkdir(parents=True, exist_ok=True)
                (generation / "decomposition.json").write_text(
                    json.dumps(
                        {"model_results": _model_results(status, lane=lane_run)}
                    ),
                    encoding="utf-8",
                )
            return {"generation_id": generation_id}
        if args[0] == "portfolio_correlation.py" and args[1] in {"stage", "publish"}:
            output_root = str(args[args.index("--output-root") + 1])
            return {"generation_id": hashlib.sha256(output_root.encode()).hexdigest()}
        if args[0:2] == ("portfolio_weekly_returns.py", "build"):
            return {"candidate_dir": str(tmp_path / "weekly-candidate")}
        if args[1] == "publish":
            output_root = str(args[args.index("--output-root") + 1])
            return {"generation_id": hashlib.sha256(output_root.encode()).hexdigest()}
        return {}

    monkeypatch.setattr(builder, "_tool", fake_tool)
    monkeypatch.setattr(
        builder,
        "_build_hybrid_decomposition",
        lambda **_k: (_ for _ in ()).throw(AssertionError("no hybrid adapter")),
    )
    return {"recipe": recipe, "bundle": bundle, "calls": calls}


def _run(world: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    return builder.build_analytics(
        world["recipe"],
        world["bundle"],
        tmp_path / "analytics",
        retained_cache_root=tmp_path / "retained",
    )


def test_a_declared_release_regenerates_the_table_from_its_control_run(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=True, control=True)
    result = _run(world, short_root)
    lens_control = result["lens_control"]
    table_path = (
        short_root / "analytics" / "decomposition-lenses" / builder.LENS_MOVES_NAME
    )
    content = table_path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == lens_control["table_sha256"]
    table = json.loads(content)
    assert table["schema"] == "strategy-basis-lens-moves/1"
    assert table["series_id"] == SERIES
    # the sleeve lane lost the model that the control (account) lane keeps
    assert table["summary"]["changed_models"] == 2
    assert {row["move"] for row in table["rows"]} == {
        "AVAILABLE -> UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV"
    }
    assert table["findings"] == []
    # the control ran the control plan's two specs beside the sleeve lane's two
    control_calls = [
        call
        for call in world["calls"]
        if call[0] == "portfolio_decomposition.py"
        and "lens-control" in call[call.index("--output-root") + 1]
    ]
    assert len(control_calls) == 4
    assert len(lens_control["control_runs"]) == 2
    timing = json.loads(
        (short_root / "analytics" / "lens-control-timing.json").read_text("utf-8")
    )
    assert set(timing) == {
        "control_lane_builds",
        "sleeve_lane_builds",
        "wall",
        "workers",
    }
    # the verifier recomputes the same bytes from the recorded roots
    sleeve_entries = [
        {**binding, "spec_path": str(world["bundle"] / binding["path"])}
        for binding in result["lens_generation_bindings"]
    ]
    builder.verify_lens_moves(table_path.parent, lens_control, sleeve_entries)


def test_a_declaration_without_a_control_run_is_refused(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=True, control=False)
    with pytest.raises(builder.VerticalBuildError, match="lens control run"):
        _run(world, short_root)


def test_control_plan_bundles_without_a_declaration_are_refused(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=False, control=True)
    with pytest.raises(builder.VerticalBuildError, match="need a strategy basis"):
        _run(world, short_root)


def test_an_edited_table_row_is_refused_by_the_verifier(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=True, control=True)
    result = _run(world, short_root)
    lenses_root = short_root / "analytics" / "decomposition-lenses"
    table_path = lenses_root / builder.LENS_MOVES_NAME
    table = json.loads(table_path.read_bytes())
    table["rows"][0]["move"] = "AVAILABLE -> AVAILABLE"
    table_path.write_bytes(
        (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    sleeve_entries = [
        {**binding, "spec_path": str(world["bundle"] / binding["path"])}
        for binding in result["lens_generation_bindings"]
    ]
    with pytest.raises(
        builder.VerticalBuildError, match="differs from its control run"
    ):
        builder.verify_lens_moves(lenses_root, result["lens_control"], sleeve_entries)
    table_path.unlink()
    with pytest.raises(builder.VerticalBuildError, match="is missing"):
        builder.verify_lens_moves(lenses_root, result["lens_control"], sleeve_entries)


def test_without_a_declaration_there_is_no_control_run_and_no_table(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=False, control=False)
    result = _run(world, short_root)
    assert "lens_control" not in result
    assert not (
        short_root / "analytics" / "decomposition-lenses" / builder.LENS_MOVES_NAME
    ).exists()
    assert not (short_root / "analytics" / "lens-control").exists()
    assert not any("lens-control" in str(call) for call in world["calls"])


def test_the_recorded_lens_operations_follow_the_plan_order_not_completion_order(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (short_root / "a").mkdir()
    (short_root / "b").mkdir()
    sequential = _world(short_root / "a", monkeypatch, declared=True, control=True)
    monkeypatch.setenv(builder.LENS_WORKERS_ENV, "1")
    first = _run(sequential, short_root / "a")
    parallel = _world(short_root / "b", monkeypatch, declared=True, control=True)
    monkeypatch.setenv(builder.LENS_WORKERS_ENV, "4")
    second = _run(parallel, short_root / "b")
    # the numerical producer record (what the stage result keeps) is the same either way
    assert first["numerical_producer_evidence"]["operations"] == second[
        "numerical_producer_evidence"
    ]["operations"] or [
        op["operation"] for op in first["numerical_producer_evidence"]["operations"]
    ] == [op["operation"] for op in second["numerical_producer_evidence"]["operations"]]
    assert [b["spec_id"] for b in first["lens_generation_bindings"]] == [
        b["spec_id"] for b in second["lens_generation_bindings"]
    ]


def test_the_stage_check_stops_a_declared_release_without_a_valid_control(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(short_root, monkeypatch, declared=True, control=True)
    result = _run(world, short_root)
    recipe = builder._recipe(world["recipe"])
    builder.check_lens_control_stage(recipe, result)
    # a stage result without its control
    stripped = {key: value for key, value in result.items() if key != "lens_control"}
    with pytest.raises(builder.VerticalBuildError, match="needs the lens control"):
        builder.check_lens_control_stage(recipe, stripped)
    # an edited table
    table_path = (
        short_root / "analytics" / "decomposition-lenses" / builder.LENS_MOVES_NAME
    )
    table = json.loads(table_path.read_bytes())
    table["rows"].pop()
    table_path.write_bytes(
        (json.dumps(table, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    with pytest.raises(
        builder.VerticalBuildError, match="differs from its control run"
    ):
        builder.check_lens_control_stage(recipe, result)
    # a control with no declaration
    plain = {key: value for key, value in recipe.items() if key != "strategy_basis"}
    with pytest.raises(builder.VerticalBuildError, match="needs a declaration"):
        builder.check_lens_control_stage(plain, result)

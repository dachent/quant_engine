from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_designer_model_curve.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_designer_model_curve", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio designer-model-curve stager")
designer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = designer
SPEC.loader.exec_module(designer)
DesignerModelCurveError = designer.DesignerModelCurveError

AS_OF = date(2026, 9, 12)
MODEL_RELATIVE = "raw/designer-model.json"
PERFORMANCE_RELATIVE = "raw/designer-performance.json"
INCEPTION = date(2024, 1, 1)
LAUNCH = date(2024, 1, 4)


def _rows(count: int = 8, step: str = "0.01") -> list[list[object]]:
    """A chart path: [day step, model level, benchmark level], padded at the end."""

    level = Decimal(100)
    rows: list[list[object]] = []
    for index in range(count):
        if index:
            level *= Decimal(1) + Decimal(step)
        rows.append([0 if index == 0 else 1, float(level), float(level)])
    rows.append([1, None, None])
    return rows


def _performance(rows: list[list[object]] | None = None) -> dict[str, object]:
    return {
        "perfData": {
            "date": INCEPTION.isoformat(),
            "dateLast": "2026-12-31",
            "data": _rows() if rows is None else rows,
            "plotLines": [{"dt": LAUNCH.isoformat(), "color": "green", "width": 2}],
        }
    }


def _end_of(performance: dict[str, object]) -> date:
    block = performance["perfData"]
    observed = date.fromisoformat(str(block["date"]))
    last = observed
    for index, row in enumerate(block["data"]):
        if index:
            observed = observed + timedelta(days=int(row[0]))
        if row[1] is not None:
            last = observed
    return last


def _model(performance: dict[str, object], **overrides: object) -> dict[str, object]:
    end = _end_of(performance)
    payload: dict[str, object] = {
        "portUid": 1764562,
        "name": "EU multifactor microcap model",
        "currency": "EUR",
        "benchmark": "Europe Small",
        "benchmarkUid": 112910,
        "inceptionDt": INCEPTION.strftime("%m/%d/%Y"),
        "launchDt": LAUNCH.strftime("%m/%d/%Y"),
        "endDt": end.strftime("%m/%d/%Y"),
        "designerModel": {"revisions": [{"revision_uid": 360, "update_dt": "2025-09-06"}]},
    }
    payload.update(overrides)
    return payload


def _capture(
    root: Path,
    performance: dict[str, object] | None = None,
    model: dict[str, object] | None = None,
) -> Path:
    """A retained capture: fetched bytes plus a manifest that digests them."""

    capture = root / "capture"
    (capture / "raw").mkdir(parents=True)
    performance = _performance() if performance is None else performance
    model = _model(performance) if model is None else model
    artifacts = []
    for relative, payload in (
        (MODEL_RELATIVE, model),
        (PERFORMANCE_RELATIVE, performance),
    ):
        content = json.dumps(payload, sort_keys=True).encode("utf-8")
        (capture / relative).write_bytes(content)
        artifacts.append(
            {
                "relative_path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "observed_length": len(content),
            }
        )
    (capture / "capture-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}, sort_keys=True), encoding="utf-8"
    )
    return capture


def _stage(capture: Path, output_root: Path) -> dict[str, object]:
    return designer.stage_candidate(
        capture, output_root, MODEL_RELATIVE, PERFORMANCE_RELATIVE, AS_OF
    )


def test_stages_a_verified_candidate_without_advancing_current(tmp_path: Path) -> None:
    result = _stage(_capture(tmp_path), tmp_path / "store")

    assert result["state"] == "STAGED_CANDIDATE_PENDING_OWNER_SELECTION"
    assert result["published"] is False
    assert result["verified_capture_artifacts"] == 2
    assert not (tmp_path / "store/CURRENT.json").exists()
    generation = tmp_path / "store/generations" / str(result["generation_id"])
    assert (generation / "returns.csv").is_file()
    assert (generation / "raw/designer-model.json").is_file()
    assert (generation / "raw/designer-performance.json").is_file()


def test_decodes_the_chart_calendar_from_accumulated_day_steps(tmp_path: Path) -> None:
    # A weekend gap is a step of 3, so the calendar is not index arithmetic.
    rows = _rows(count=4)
    rows[2][0] = 3
    performance = _performance(rows)
    capture = _capture(tmp_path, performance, _model(performance))

    result = _stage(capture, tmp_path / "store")
    generation = tmp_path / "store/generations" / str(result["generation_id"])
    dates = [
        line.split(",")[1]
        for line in (generation / "returns.csv").read_text(encoding="utf-8").splitlines()[1:]
    ]

    assert dates == ["2024-01-02", "2024-01-05", "2024-01-06"]


def test_splits_the_layer_at_the_providers_own_launch(tmp_path: Path) -> None:
    result = _stage(_capture(tmp_path), tmp_path / "store")
    series = result["series"]

    assert series["provider_launch"] == LAUNCH.isoformat()
    assert series["prelaunch_return_observations"] == 2
    assert series["model_live_return_observations"] == 5
    assert (
        series["prelaunch_return_observations"]
        + series["model_live_return_observations"]
        == series["return_observations"]
    )


def test_carries_the_currency_benchmark_and_revision_dates(tmp_path: Path) -> None:
    result = _stage(_capture(tmp_path), tmp_path / "store")
    series = result["series"]

    assert series["currency"] == "EUR"
    assert series["benchmark"] == "Europe Small"
    assert series["provider_revision_dates"] == ["2025-09-06"]


def test_trailing_unvalued_rows_are_calendar_padding_not_observations(
    tmp_path: Path,
) -> None:
    padded = _rows(count=5)
    padded.extend([[1, None, None], [1, None, None]])
    performance = _performance(padded)
    capture = _capture(tmp_path, performance, _model(performance))

    result = _stage(capture, tmp_path / "store")

    assert result["series"]["observed_end"] == "2024-01-05"
    assert result["series"]["level_observations"] == 5


def test_refuses_a_curve_whose_end_contradicts_the_stated_end_date(
    tmp_path: Path,
) -> None:
    performance = _performance()
    model = _model(performance, endDt="12/31/2026")
    capture = _capture(tmp_path, performance, model)

    with pytest.raises(DesignerModelCurveError, match="stated endDt"):
        _stage(capture, tmp_path / "store")


def test_refuses_a_launch_line_that_contradicts_the_stated_launch(
    tmp_path: Path,
) -> None:
    performance = _performance()
    model = _model(performance, launchDt="06/01/2024")
    capture = _capture(tmp_path, performance, model)

    with pytest.raises(DesignerModelCurveError, match="launchDt"):
        _stage(capture, tmp_path / "store")


def test_refuses_a_start_that_contradicts_the_stated_inception(tmp_path: Path) -> None:
    performance = _performance()
    model = _model(performance, inceptionDt="01/01/2008")
    capture = _capture(tmp_path, performance, model)

    with pytest.raises(DesignerModelCurveError, match="inceptionDt"):
        _stage(capture, tmp_path / "store")


def test_refuses_a_capture_artifact_that_differs_from_its_digest(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path)
    (capture / PERFORMANCE_RELATIVE).write_text("{}", encoding="utf-8")

    with pytest.raises(DesignerModelCurveError, match="differs from its manifest"):
        _stage(capture, tmp_path / "store")


def test_restaging_the_same_capture_is_byte_identical(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    first = _stage(capture, tmp_path / "store")
    second = _stage(capture, tmp_path / "store")

    assert first == second


def test_refuses_a_level_path_that_reaches_zero(tmp_path: Path) -> None:
    rows = _rows(count=4)
    rows[2][1] = 0.0
    performance = _performance(rows)
    capture = _capture(tmp_path, performance, _model(performance))

    with pytest.raises(DesignerModelCurveError, match="positive wealth index"):
        _stage(capture, tmp_path / "store")

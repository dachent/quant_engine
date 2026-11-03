"""The 5-day Quantbase NAV publication grid: last publishable date at a cutoff."""

from __future__ import annotations

import csv
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "capture_quantbase_har_grid_test",
    REPOSITORY_ROOT / "tools" / "capture_quantbase_har.py",
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load Quantbase HAR driver")
driver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = driver
SPEC.loader.exec_module(driver)

ANCHOR = date(2023, 10, 18)
ERROR = driver.QuantbaseHarIntakeError
# On-disk 09-02 refresh public Volatis fund NAV series, proven offline when present.
ON_DISK = Path(
    r"C:\Users\BorisVaisman\V Capital\investing - Documents\_quant_engine"
    r"\dataQuantbase\source-runs\refresh-20260902\csv_full"
    r"\diag_public_fund_pricehistory_STALE_PROVIDER_BUILD.csv"
)


def _grid(first: date, count: int, step: int = 5) -> list[date]:
    return [first + timedelta(days=step * index) for index in range(count)]


def _resolve(captured: list[date], cutoff: date, **kwargs: object) -> dict[str, object]:
    return driver.last_publishable_grid_date(captured, cutoff, anchor=ANCHOR, **kwargs)


def test_cutoff_on_a_captured_grid_date_is_publishable_as_is() -> None:
    captured = _grid(date(2026, 7, 29), 6)  # through 2026-08-23
    result = _resolve(captured, date(2026, 8, 23))
    assert result["publishable"] == "2026-08-23"
    assert result["flags"] == []


def test_cutoff_between_grid_dates_requires_a_label() -> None:
    captured = _grid(date(2026, 7, 29), 6)
    with pytest.raises(ERROR, match="CUTOFF_OFF_NAV_GRID"):
        _resolve(captured, date(2026, 8, 20))
    result = _resolve(
        captured, date(2026, 8, 20), label="owner ruling: off-grid cutoff"
    )
    assert result["publishable"] == "2026-08-18"
    assert result["flags"] == ["CUTOFF_OFF_NAV_GRID"]


def test_cutoff_after_the_capture_returns_prediction_only_when_labelled() -> None:
    captured = _grid(date(2026, 7, 29), 6)  # newest 2026-08-23
    with pytest.raises(ERROR, match="AFTER_NEWEST"):
        _resolve(captured, date(2026, 10, 30))
    result = _resolve(captured, date(2026, 10, 30), label="capture predates cutoff")
    assert result["publishable"] == "2026-08-23"
    assert result["grid_predicted"] == "2026-10-27"
    assert "CUTOFF_AFTER_NEWEST_CAPTURED_GRID_DATE" in str(result["flags"])


def test_cutoff_before_the_first_grid_date_is_always_refused() -> None:
    captured = _grid(date(2026, 7, 29), 3)
    with pytest.raises(ERROR, match="before the first"):
        _resolve(captured, date(2026, 7, 28), label="x")


def test_off_grid_provisional_rows_are_never_publishable() -> None:
    # the 2026-07-29 capture ended on an off-grid 2026-07-18 provisional row
    captured = [*_grid(date(2026, 6, 24), 5), date(2026, 7, 18)]
    result = _resolve(captured, date(2026, 7, 18), label="off-grid cutoff")
    assert result["publishable"] == "2026-07-14"
    assert result["off_grid_captured_dates"] == ["2026-07-18"]


def test_differently_dated_capture_with_another_anchor_phase() -> None:
    anchor = date(2027, 1, 3)
    result = driver.last_publishable_grid_date(
        _grid(anchor, 20),
        date(2027, 4, 2),
        anchor=anchor,
        step_days=5,
        label="off-grid",
    )
    assert result["publishable"] == "2027-03-29"
    assert result["grid_predicted"] == "2027-03-29"


def test_interior_gaps_empty_capture_and_bad_step_fail_closed() -> None:
    captured = _grid(date(2026, 7, 29), 6)
    del captured[2]
    with pytest.raises(ERROR, match="interior"):
        _resolve(captured, date(2026, 8, 23))
    assert _resolve(captured, date(2026, 8, 23), allow_interior_gaps=True)[
        "missing_interior_grid_dates"
    ] == ["2026-08-08"]
    with pytest.raises(ERROR, match="no date"):
        _resolve([date(2026, 7, 18)], date(2026, 7, 18))
    with pytest.raises(ERROR, match="step"):
        _resolve(captured, date(2026, 8, 23), step_days=0)
    with pytest.raises(ERROR, match="label"):
        _resolve(captured, date(2026, 8, 23), label=" ")


@pytest.mark.skipif(not ON_DISK.is_file(), reason="owner OneDrive capture not present")
def test_on_disk_09_02_public_nav_is_a_pure_5_day_grid_ending_08_23() -> None:
    with ON_DISK.open(newline="", encoding="utf-8") as handle:
        days = [date.fromisoformat(row["time"][:10]) for row in csv.DictReader(handle)]
    assert len(days) == 209
    result = _resolve(days, date(2026, 8, 23))
    assert result["publishable"] == "2026-08-23"
    assert result["missing_interior_grid_dates"] == []
    # the capture predates 2026-10-30; the grid predicts 2026-10-27 but it is unevidenced
    late = _resolve(days, date(2026, 10, 30), label="capture predates cutoff")
    assert (late["publishable"], late["grid_predicted"]) == ("2026-08-23", "2026-10-27")

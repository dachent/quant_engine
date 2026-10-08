"""A4 (QC1-R): an annualized comparison figure must match the series' own frequency.

The ledger row read "annualization on sparse or short windows".  The bytes showed
that density is the wrong question: the qsi weekly provider comparison (27
observations over 182 days at 52 periods a year) is a correct short-window
annualization.  What was wrong, in the original Echotrade -298% symptom, was a
declared frequency the dates do not carry: weekly-sparse common dates scaled as if
they were daily.  The kernel therefore refuses a comparison whose observations fall
below 0.8 x periods_per_year x span_years, and publishes it UNAVAILABLE with the
reason ANNUALIZATION_FREQUENCY_INCONSISTENT instead of a number.  A short window
that is frequency-consistent is not suppressed here; it is labelled by the report.

Both fixtures run through the real producer, ``build_catalog``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]


def _load_fixture_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "attribution_frequency_guard_fixture",
        ROOT / "tests" / "portfolio_attribution" / "test_portfolio_attribution.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fixture = _load_fixture_module()
attribution = fixture.attribution


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _account_returns(first: date, last: date) -> dict[date, str]:
    return {
        day: str(Decimal("0.001") * ((index % 5) - 2))
        for index, day in enumerate(_days(first, last))
    }


def _weekly_reference(start: date, weeks: int) -> dict[str, str]:
    return {
        (start + timedelta(days=7 * index)).isoformat(): str(
            Decimal("0.002") * ((index % 4) - 1)
        )
        for index in range(weeks)
    }


def _world(
    tmp_path: Path,
    *,
    periods_per_year: int,
    first_reference: date,
    weeks: int,
) -> dict[str, Any]:
    """Strategy-1's provider comparison against a weekly reference series."""

    last = first_reference + timedelta(days=7 * (weeks - 1))
    baseline = fixture._baseline(
        tmp_path,
        account_live_returns=_account_returns(date(2025, 12, 29), last),
    )
    spec, returns = fixture._inputs(tmp_path)
    # One earlier reference date, so the first period opens before the window.
    reference = {
        (first_reference - timedelta(days=7)).isoformat(): "0.001",
        **_weekly_reference(first_reference, weeks),
    }
    fixture._with_weekly_reference(
        spec,
        returns,
        reference,
        periods_per_year=periods_per_year,
        observed_start=first_reference.isoformat(),
        observed_end=last.isoformat(),
    )
    catalog, _raw, _artifacts = attribution.build_catalog(baseline, spec)
    assert isinstance(catalog, dict)
    return catalog


def test_a_daily_declared_series_with_weekly_sparse_dates_is_unavailable(
    tmp_path: Path,
) -> None:
    """BAD (the Echotrade shape): weekly dates declared at 252 periods a year."""

    catalog = _world(
        tmp_path, periods_per_year=252, first_reference=date(2026, 1, 9), weeks=27
    )
    row = catalog["comparisons"][0]

    assert row["status"] == "UNAVAILABLE"
    assert row["not_ready_reason"] == "ANNUALIZATION_FREQUENCY_INCONSISTENT"
    assert row["metrics"] is None
    # No annualized figure of any kind survives the refusal.
    assert "active_return_annualized" not in json.dumps(row)
    # The refused window is disclosed, not silently dropped.
    detail = row["annualization_refusal"]
    assert detail["periods_per_year"] == 252
    assert detail["observations"] == 27
    assert detail["observed_start"] == "2026-01-09"
    assert detail["observed_end"] == "2026-07-10"
    assert detail["minimum_observations"] > 27
    # An UNAVAILABLE row names no series and no window, as every other one does.
    for key in (
        "account_series_id",
        "reference_series_id",
        "observed_start",
        "observed_end",
    ):
        assert row[key] is None


def test_the_refusal_is_scoped_to_the_comparison_that_is_inconsistent(
    tmp_path: Path,
) -> None:
    """The strategy's other (daily) comparison, in the same catalog, is untouched."""

    catalog = _world(
        tmp_path, periods_per_year=252, first_reference=date(2026, 1, 9), weeks=27
    )

    benchmark = catalog["benchmark_comparisons"][0]
    assert benchmark["status"] == "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE"
    assert benchmark["metrics"]["observations"] == 2


def test_the_qsi_weekly_shape_is_a_correct_short_window_annualization(
    tmp_path: Path,
) -> None:
    """GOOD: 52 periods a year, 27 weekly observations over 182 days, unchanged."""

    catalog = _world(
        tmp_path, periods_per_year=52, first_reference=date(2026, 1, 9), weeks=27
    )
    row = catalog["comparisons"][0]
    metrics = row["metrics"]

    assert row["status"] == "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"
    assert "not_ready_reason" not in row
    assert metrics["observations"] == 27
    assert metrics["observed_start"] == "2026-01-09"
    assert metrics["observed_end"] == "2026-07-10"
    assert metrics["active_return_annualized"] is not None
    # Byte for byte: this digest was taken from the producer before the guard existed.
    digest = hashlib.sha256(canonical_json_bytes(row)).hexdigest()
    assert digest == _QSI_SHAPE_ROW_SHA256


# Recorded on e6392f3, where no frequency guard exists.
_QSI_SHAPE_ROW_SHA256 = (
    "854ffe7b25a9fcb3d485ac096d77731f790f7d3652882ea71f2d2bca567f6b9c"
)


@pytest.mark.parametrize(
    ("periods_per_year", "weeks", "refused"),
    [
        # 12 weekly periods over 77 days need 0.8 * 52 * 77 / 365 = 8.8
        (52, 12, False),
        # monthly declared on weekly dates: 12 periods over 182 days -> need 4.8
        (12, 27, False),
        # weekly dates declared daily: refused at any length
        (252, 12, True),
        (365, 27, True),
    ],
)
def test_the_threshold_follows_the_declared_frequency_not_the_window_length(
    tmp_path: Path, periods_per_year: int, weeks: int, refused: bool
) -> None:
    catalog = _world(
        tmp_path,
        periods_per_year=periods_per_year,
        first_reference=date(2026, 1, 9),
        weeks=weeks,
    )
    row = catalog["comparisons"][0]

    assert (row["status"] == "UNAVAILABLE") is refused
    assert (row["metrics"] is None) is refused

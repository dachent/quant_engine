"""A post-cutoff Schwab capture keeps its real observed_at and an explicit valuation cutoff.

The synthetic stages go through the real ledger selection (``build_catalog``) and the
real materializer authority check, as the R2.1 point-admission tests do.
"""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
from typing import Any

import pytest

import portfolio_pipeline_materialize as materializer


SPEC = importlib.util.spec_from_file_location(
    "schwab_valuation_fixture", Path(__file__).with_name("test_admit_or_declare.py")
)
assert SPEC is not None and SPEC.loader is not None
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
ledger = fixtures.ledger

# Friday 2026-09-18 is the valuation cutoff; the book is captured on Saturday morning
# New York time, so observed_at is the real Saturday instant (the NY date is the 19th).
SATURDAY_CAPTURE = "2026-09-19T12:00:00+00:00"
FRIDAY = date(2026, 9, 18)


def _stage(
    tmp_path: Path, valuation_cutoff: str | None
) -> tuple[Path, dict[str, Any], Path]:
    return fixtures._source_bound_stage(
        tmp_path,
        source_cutoff=FRIDAY,
        schwab_point_capture=SATURDAY_CAPTURE,
        requested_as_of=FRIDAY,
        schwab_valuation_cutoff=valuation_cutoff,
    )


def _schwab_positions(spec_path: Path) -> list[dict[str, Any]]:
    catalog, _, _, _ = ledger.build_catalog(spec_path)
    return [row for row in catalog["positions"] if row["provider_id"] == "schwab"]


def test_real_saturday_observation_with_a_friday_cutoff_is_selected(
    tmp_path: Path,
) -> None:
    generation, authority, spec_path = _stage(tmp_path, "2026-09-18")
    positions = _schwab_positions(spec_path)
    assert len(positions) == 1
    position = positions[0]
    # provenance is the real instant and the native UTC date; the financial day is
    # the explicit cutoff, carried on the row for the baseline
    assert position["observed_at"] == SATURDAY_CAPTURE
    assert position["as_of_date"] == "2026-09-19"
    assert position["valuation_cutoff"] == "2026-09-18"
    # the materializer's independent reconstruction of the point agrees row for row
    verified = materializer._provider_admission_authority(
        generation,
        authority["contract"],
        **{
            key: authority[key]
            for key in (
                "contract_raw_sha256",
                "contract_semantic_sha256",
                "census_sha256",
            )
        },
    )
    assert verified["provider_count"] == 7


def test_without_the_cutoff_the_legacy_new_york_day_excludes_the_position(
    tmp_path: Path,
) -> None:
    """Legacy behaviour: the New York day of observed_at (Saturday) is newer than
    the Friday request, so the point is not selected."""

    _, _, spec_path = _stage(tmp_path, None)
    assert _schwab_positions(spec_path) == []


def test_a_cutoff_later_than_the_observed_new_york_day_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="valuation cutoff is later than"):
        _, _, spec_path = _stage(tmp_path, "2026-09-20")
        ledger.build_catalog(spec_path)


def test_a_cutoff_on_the_observed_new_york_day_is_the_legacy_day(
    tmp_path: Path,
) -> None:
    """valuation_cutoff equal to the New York date changes nothing but the key."""

    first = tmp_path / "with"
    second = tmp_path / "without"
    first.mkdir()
    second.mkdir()
    # requested one day later so the legacy (Saturday) day is selected either way
    with_key = fixtures._source_bound_stage(
        first,
        source_cutoff=date(2026, 9, 19),
        schwab_point_capture=SATURDAY_CAPTURE,
        requested_as_of=date(2026, 9, 19),
        schwab_valuation_cutoff="2026-09-19",
    )
    without = fixtures._source_bound_stage(
        second,
        source_cutoff=date(2026, 9, 19),
        schwab_point_capture=SATURDAY_CAPTURE,
        requested_as_of=date(2026, 9, 19),
    )
    keyed = _schwab_positions(with_key[2])
    legacy = _schwab_positions(without[2])
    assert len(keyed) == len(legacy) == 1
    for key in ("observed_at", "as_of_date", "quantity", "source_record_id"):
        assert keyed[0][key] == legacy[0][key]
    assert keyed[0]["valuation_cutoff"] == "2026-09-19"
    assert "valuation_cutoff" not in legacy[0]

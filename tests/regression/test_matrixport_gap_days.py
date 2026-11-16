"""#899: a missing NAV day is admitted only with a bound; one that could hide a peak is not."""

from __future__ import annotations

from typing import Any

from quant_engine.portfolio_ledger.hwm_gap_bounds import assess_missing_point_bounds


def _point(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "date": "2026-01-27",
        "origin": "DERIVED_BRIDGE_WITH_CLOSURE",
        "nav": "0.85",
        "range_low": "0.8498",
        "range_high": "0.8502",
        "prior_high": "0.9356",
        "max_observed_closure_error": "0.0366",
        "tolerance_relative": "0.005",
    }
    return {**base, **over}


def test_matrixport_gap_days_are_bounded_or_unavailable() -> None:
    span = [{"missing_source_date": "2026-01-27"}]
    assert assess_missing_point_bounds(span, [_point()])["status"] == "BOUNDED"
    hiding = _point(range_high="0.9100", nav="0.9090", range_low="0.9080")
    assert assess_missing_point_bounds(span, [hiding])["status"] == "UNBOUNDED"
    assert assess_missing_point_bounds(span, [])["status"] == "UNBOUNDED"

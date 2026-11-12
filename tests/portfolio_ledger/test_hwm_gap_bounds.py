"""#899: a missing NAV point is admitted only when its bound stays below the prior high."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from quant_engine.portfolio_ledger.hwm_gap_bounds import assess_missing_point_bounds


def _span(day: str) -> dict[str, Any]:
    return {"missing_source_date": day}


def _point(day: str = "2026-01-27", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "date": day,
        "origin": "DERIVED_BRIDGE_WITH_CLOSURE",
        "nav": "0.85",
        "range_low": "0.8498",
        "range_high": "0.8502",
        "prior_high": "0.9356",
        "max_observed_closure_error": "0.0366",
        "tolerance_relative": "0.005",
    }
    return {**base, **over}


def test_a_bounded_point_below_the_prior_high_is_bounded() -> None:
    result = assess_missing_point_bounds([_span("2026-01-27")], [_point()])
    assert result["status"] == "BOUNDED"
    (verdict,) = result["points"]
    assert verdict["cannot_hide_peak"] is True
    assert Decimal(verdict["margin_relative"]) > 0


def test_an_alternate_source_point_is_admitted() -> None:
    point = _point(origin="ADMITTED_ALTERNATE_SOURCE")
    assert (
        assess_missing_point_bounds([_span("2026-01-27")], [point])["status"]
        == "BOUNDED"
    )


def test_a_gap_that_could_hide_a_peak_stays_unbounded() -> None:
    """KILL: margin below the closure error (point 3% under the high, 3.66% error)."""

    point = _point(range_high="0.9100", nav="0.9090", range_low="0.9080")
    result = assess_missing_point_bounds([_span("2026-01-27")], [point])
    assert result["status"] == "UNBOUNDED"
    assert result["points"][0]["reason"] == "BOUND_REACHES_PRIOR_HIGH"


def test_a_point_already_above_the_prior_high_stays_unbounded() -> None:
    point = _point(nav="0.95", range_low="0.95", range_high="0.95")
    assert (
        assess_missing_point_bounds([_span("2026-01-27")], [point])["status"]
        == "UNBOUNDED"
    )


def test_a_stated_cannot_create_new_high_flag_is_not_trusted() -> None:
    point = _point(
        range_high="0.9100",
        nav="0.9090",
        range_low="0.9080",
        cannot_create_new_high=True,
    )
    assert (
        assess_missing_point_bounds([_span("2026-01-27")], [point])["status"]
        == "UNBOUNDED"
    )


@pytest.mark.parametrize(
    "origin", ["LINEAR_INTERPOLATION", "PROVIDER_RETURN_BRIDGE", None]
)
def test_an_origin_that_is_not_admitted_is_unbounded(origin: str | None) -> None:
    result = assess_missing_point_bounds([_span("2026-01-27")], [_point(origin=origin)])
    assert result["status"] == "UNBOUNDED"
    assert result["points"][0]["reason"] == "ORIGIN_NOT_ADMITTED"


@pytest.mark.parametrize(
    "over",
    [
        {"nav": "x"},
        {"range_low": "0.86"},
        {"prior_high": "0"},
        {"max_observed_closure_error": "-0.1"},
        {"nav": None},
    ],
)
def test_malformed_fields_are_unbounded(over: dict[str, Any]) -> None:
    result = assess_missing_point_bounds([_span("2026-01-27")], [_point(**over)])
    assert result["status"] == "UNBOUNDED"
    assert result["points"][0]["reason"] == "FIELD_INVALID"


def test_every_span_needs_its_own_point() -> None:
    spans = [_span("2026-01-27"), _span("2026-07-03")]
    result = assess_missing_point_bounds(spans, [_point("2026-01-27")])
    assert result["status"] == "UNBOUNDED"
    assert [p["reason"] for p in result["points"]] == [None, "NO_POINT"]


def test_a_point_for_no_span_and_duplicates_are_unbounded() -> None:
    assert (
        assess_missing_point_bounds(
            [_span("2026-01-27")], [_point("2026-01-27"), _point("2026-02-02")]
        )["status"]
        == "UNBOUNDED"
    )
    assert (
        assess_missing_point_bounds(
            [_span("2026-01-27")], [_point("2026-01-27"), _point("2026-01-27")]
        )["status"]
        == "UNBOUNDED"
    )


def test_no_spans_is_not_a_bounded_gap_set() -> None:
    assert assess_missing_point_bounds([], [])["status"] == "UNBOUNDED"
    assert assess_missing_point_bounds([_span("2026-01-27")], None)["status"] == (
        "UNBOUNDED"
    )


@pytest.mark.parametrize("bad", ["bad", "-0.1", "NaN"])
def test_a_present_but_malformed_tolerance_fails_closed(bad: str) -> None:
    point = _point(origin="ADMITTED_ALTERNATE_SOURCE", tolerance_relative=bad)
    result = assess_missing_point_bounds([_span("2026-01-27")], [point])
    assert result["status"] == "UNBOUNDED"
    assert result["points"][0]["reason"] == "FIELD_INVALID"


def test_ambient_decimal_precision_cannot_make_an_unsafe_point_bounded() -> None:
    from decimal import localcontext

    point = _point(
        nav="1.00000000000000000000000000001",
        range_low="1.00000000000000000000000000001",
        range_high="1.00000000000000000000000000001",
        prior_high="1.000000000000000000000000000001",
        max_observed_closure_error="0",
        tolerance_relative="0",
    )
    outcomes = set()
    for prec in (10, 28, 60):
        with localcontext() as ctx:
            ctx.prec = prec
            outcomes.add(
                assess_missing_point_bounds([_span("2026-01-27")], [point])["status"]
            )
    assert outcomes == {"UNBOUNDED"}

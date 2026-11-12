"""Can a missing daily NAV point hide a new high-water mark? (#899)

A vendor NAV series can omit a day.  The governed returns path keeps that day as a
two-day holding-period span and never invents a daily point, so a path-dependent
high-water mark cannot see what the span hides.  The span hides nothing when the
missing day's value is *bounded* and the whole bound sits below the prior high:

* the point comes from an admitted origin (an independent alternate source, or a
  bridge derived from neighbouring days with a measured closure error);
* its stated range contains its value, and its uncertainty is the larger of the
  range and the measured error, applied multiplicatively;
* the upper end of that uncertainty is strictly below the prior high.

Anything else (no point, an origin that is not admitted, a malformed field, or a
bound that reaches the prior high) leaves the missing day able to hide a peak, and
the numeric HWM stays UNAVAILABLE.  The function is pure and derives every verdict
from the point's own fields; it never trusts a stated ``cannot_create_new_high``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import Any, Final, Mapping, Sequence

from .models import decimal_text

#: Origins a missing point may carry and still count as bounded.
ADMITTED_POINT_ORIGINS: Final = frozenset(
    {"ADMITTED_ALTERNATE_SOURCE", "DERIVED_BRIDGE_WITH_CLOSURE"}
)

_REQUIRED_FIELDS: Final = (
    "date",
    "origin",
    "nav",
    "range_low",
    "range_high",
    "prior_high",
    "max_observed_closure_error",
)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _assess_point(point: Mapping[str, Any]) -> dict[str, Any]:
    day = str(point.get("date"))
    verdict: dict[str, Any] = {"date": day, "origin": point.get("origin")}
    if point.get("origin") not in ADMITTED_POINT_ORIGINS:
        return {**verdict, "cannot_hide_peak": False, "reason": "ORIGIN_NOT_ADMITTED"}
    values = {name: _decimal(point.get(name)) for name in _REQUIRED_FIELDS[2:]}
    if any(value is None for value in values.values()):
        return {**verdict, "cannot_hide_peak": False, "reason": "FIELD_INVALID"}
    nav = values["nav"]
    low = values["range_low"]
    high = values["range_high"]
    prior_high = values["prior_high"]
    closure = values["max_observed_closure_error"]
    assert nav is not None and low is not None and high is not None
    assert prior_high is not None and closure is not None
    if not (0 < low <= nav <= high) or prior_high <= 0 or closure < 0:
        return {**verdict, "cannot_hide_peak": False, "reason": "FIELD_INVALID"}
    tolerance: Decimal | None = None
    if point.get("tolerance_relative") is not None:
        tolerance = _decimal(point.get("tolerance_relative"))
        if tolerance is None or tolerance < 0:
            return {**verdict, "cannot_hide_peak": False, "reason": "FIELD_INVALID"}
    # An alternate source is exact to its stated tolerance; a derived bridge is only
    # as good as its worst measured closure.  Take the larger so neither understates.
    bound = max(closure, tolerance) if tolerance is not None else closure
    # The safety comparison is exact (rationals), so ambient Decimal precision can
    # never turn an unsafe point into a bounded one.
    exact_upper = Fraction(high) * (1 + Fraction(bound))
    exact_prior = Fraction(prior_high)
    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        upper = Decimal(exact_upper.numerator) / Decimal(exact_upper.denominator)
        margin = (
            Decimal((exact_prior - exact_upper).numerator)
            / Decimal((exact_prior - exact_upper).denominator)
            / prior_high
        )
        verdict.update(
            {
                "bound_relative": format(bound, "f"),
                "upper_bound_nav": format(upper, "f"),
                "prior_high": format(prior_high, "f"),
                "margin_relative": format(margin, "f"),
            }
        )
    if exact_upper >= exact_prior:
        return {
            **verdict,
            "cannot_hide_peak": False,
            "reason": "BOUND_REACHES_PRIOR_HIGH",
        }
    return {**verdict, "cannot_hide_peak": True, "reason": None}


def assess_missing_point_bounds(
    span_receipts: Sequence[Mapping[str, Any]],
    points: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Return ``{"status": "BOUNDED"|"UNBOUNDED", "points": [...]}``.

    ``span_receipts`` are the holding-period spans (each names its
    ``missing_source_date``); ``points`` the bounded values for those days.  BOUNDED
    only when there is at least one span and every span has exactly one point that
    cannot hide a peak.  A point for a day no span names is itself unbounded evidence
    (unreconciled), and so is a day named twice.
    """

    declared = [str(span.get("missing_source_date")) for span in span_receipts]
    supplied = list(points or ())
    by_date: dict[str, list[Mapping[str, Any]]] = {}
    for point in supplied:
        by_date.setdefault(str(point.get("date")), []).append(point)
    verdicts: list[dict[str, Any]] = []
    for day in sorted(set(declared)):
        candidates = by_date.get(day, [])
        if len(candidates) != 1:
            verdicts.append(
                {
                    "date": day,
                    "origin": None,
                    "cannot_hide_peak": False,
                    "reason": "NO_POINT" if not candidates else "POINT_DUPLICATED",
                }
            )
        else:
            verdicts.append(_assess_point(candidates[0]))
    for day in sorted(set(by_date) - set(declared)):
        verdicts.append(
            {
                "date": day,
                "origin": None,
                "cannot_hide_peak": False,
                "reason": "POINT_WITHOUT_SPAN",
            }
        )
    if len(declared) != len(set(declared)):
        verdicts.append(
            {
                "date": None,
                "origin": None,
                "cannot_hide_peak": False,
                "reason": "SPAN_DUPLICATED",
            }
        )
    bounded = bool(declared) and all(item["cannot_hide_peak"] for item in verdicts)
    return {"status": "BOUNDED" if bounded else "UNBOUNDED", "points": verdicts}


GAP_BOUNDS_SCHEMA: Final = "portfolio-hwm-gap-bounds/1"
GOVERNED_BASIS: Final = "PORT-HWM-4_GOVERNED_WITH_BOUNDED_MISSING_POINTS"


def assess_gap_bounds_document(
    spans_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]],
    document: Mapping[str, Any],
    input_sha256: str,
) -> dict[str, Any]:
    """Assess each strategy's spans against the pinned bounds document."""

    if document.get("schema") != GAP_BOUNDS_SCHEMA:
        raise ValueError("HWM gap bounds schema is unsupported")
    points = document.get("points")
    if not isinstance(points, list) or not all(isinstance(p, Mapping) for p in points):
        raise ValueError("HWM gap bounds points differ")
    results: dict[str, Any] = {}
    bounded = True
    for strategy_id, spans in sorted(spans_by_strategy.items()):
        if not spans:
            continue
        outcome = assess_missing_point_bounds(
            spans, [p for p in points if p.get("strategy_id") == strategy_id]
        )
        bounded = bounded and outcome["status"] == "BOUNDED"
        results[strategy_id] = outcome["points"]
    return {
        "status": "BOUNDED" if bounded and results else "UNBOUNDED",
        "input_sha256": input_sha256,
        "strategies": results,
    }


def proof_covers_spans(
    proof: Mapping[str, Any],
    spans_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]],
) -> bool:
    """The verified proof's path observes both endpoints of every span.

    The bound says the missing day sits below the prior high; it speaks for this
    proof only when the proof's own path runs through the span's endpoints.
    """

    path = proof.get("index_path")
    if not isinstance(path, Sequence) or isinstance(path, (str, bytes)) or not path:
        return False
    observed = {str(row.get("as_of")) for row in path if isinstance(row, Mapping)}
    return all(
        str(span.get("span_start_date")) in observed
        and str(span.get("span_end_date")) in observed
        for spans in spans_by_strategy.values()
        for span in spans
    )


def numerical_from_governed_proof(
    proof: Mapping[str, Any], assessment: Mapping[str, Any]
) -> dict[str, Any]:
    """The numeric HWM of a bounded-gap perimeter, read from the governed path.

    The governed index is base 100 with signed non-positive drawdowns; the summary
    carries the legacy base-1 index and positive drawdown fractions.
    """

    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        return {
            "hwm_index": decimal_text(Decimal(str(proof["current_index"])) / 100),
            "hwm_current_drawdown": decimal_text(
                -Decimal(str(proof["current_drawdown"]))
            ),
            "hwm_maximum_drawdown": decimal_text(
                -Decimal(str(proof["maximum_drawdown"]))
            ),
            "hwm_numerical_status": "AVAILABLE",
            "hwm_numerical_basis": GOVERNED_BASIS,
            "hwm_gap_bound": {
                "input_sha256": assessment["input_sha256"],
                "governed_receipt_sha256": proof["receipt_sha256"],
                "strategies": assessment["strategies"],
            },
        }

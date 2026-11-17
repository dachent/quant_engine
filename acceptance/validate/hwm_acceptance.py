"""E2 (#895, #899): is an unavailable legacy hwm_summary covered by an owner-accepted governed proof?

Release B's baseline carries no ``hwm_inputs`` envelope, so ``hwm_summary`` reads
UNAVAILABLE / NO_HWM_INPUTS (the PORT-HWM-3 path) beside the governed PORT-HWM-4 index.  The
validator expectation E2 ("hwm_summary is not AVAILABLE, so NOT_READY") clears only when ALL of
these hold, each recomputed here from the bytes and never read from a builder's stated flag:

* the governed proof is AVAILABLE and states ``financial_acceptance == OWNER_ACCEPTED``;
* the owner's record (``contracts/portfolio-hwm-owner-acceptance-v1.json``) names the same
  policy, carries the decision, and states exactly this proof: the same ``complete_through``,
  the same index-point count, the same index to four decimals, and the same missing daily
  points, none of which is a point of the proof's own path;
* the panel's missing daily points are exactly the recorded ones;
* every recorded missing point has exactly one bound in ``contracts/portfolio-hwm-gap-bounds-v1.json``
  from an admitted origin whose upper end (range high x (1 + max(closure error, tolerance)),
  exact rationals) is strictly below its prior high, so no missing day can hide a new peak.

The receipt sha in the record is informational (it carries the build day) and is not compared.
Pure functions; the caller reads the files.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

ACCEPTANCE_SCHEMA = "portfolio-hwm-owner-acceptance/1"
OWNER_ACCEPTED = "OWNER_ACCEPTED"
ADMITTED_ORIGINS = frozenset(
    {"ADMITTED_ALTERNATE_SOURCE", "DERIVED_BRIDGE_WITH_CLOSURE"}
)


def _dec(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _point_key(point: Mapping[str, Any]) -> tuple[str, str]:
    return (str(point.get("strategy_id")), str(point.get("date")))


def _bound_is_safe(point: Mapping[str, Any]) -> bool:
    if point.get("origin") not in ADMITTED_ORIGINS:
        return False
    names = (
        "nav",
        "range_low",
        "range_high",
        "prior_high",
        "max_observed_closure_error",
    )
    values = {n: _dec(point.get(n)) for n in names}
    if any(v is None for v in values.values()):
        return False
    nav, low, high, prior, closure = (values[n] for n in names)
    assert nav is not None and low is not None and high is not None
    assert prior is not None and closure is not None
    if not (0 < low <= nav <= high) or prior <= 0 or closure < 0:
        return False
    bound = closure
    if point.get("tolerance_relative") is not None:
        tolerance = _dec(point.get("tolerance_relative"))
        if tolerance is None or tolerance < 0:
            return False
        bound = max(closure, tolerance)
    return Fraction(high) * (1 + Fraction(bound)) < Fraction(prior)


def verify_acceptance(
    proof: Mapping[str, Any] | None,
    panel_missing: Sequence[Mapping[str, Any]] | None,
    record: Mapping[str, Any] | None,
    bounds: Mapping[str, Any] | None,
) -> list[str]:
    """Reasons the acceptance does NOT cover this proof; empty means E2 may clear."""

    if not isinstance(proof, Mapping) or proof.get("status") != "AVAILABLE":
        return ["the governed proof is absent or not AVAILABLE"]
    if proof.get("financial_acceptance") != OWNER_ACCEPTED:
        return [
            f"the proof's financial acceptance is {proof.get('financial_acceptance')!r}"
        ]
    if not isinstance(record, Mapping):
        return ["the owner acceptance record is absent or unreadable"]
    reasons: list[str] = []
    if record.get("schema") != ACCEPTANCE_SCHEMA:
        reasons.append("the record has another schema")
    if record.get("decision") != OWNER_ACCEPTED:
        reasons.append("the record does not carry the decision")
    if record.get("policy_id") != proof.get("policy_id"):
        reasons.append("the record names another policy")
    accepted = record.get("accepted")
    if not isinstance(accepted, Mapping):
        return reasons + ["the record has no accepted block"]
    path = proof.get("index_path")
    path_dates = (
        {str(r.get("as_of")) for r in path if isinstance(r, Mapping)}
        if isinstance(path, list)
        else set()
    )
    if accepted.get("complete_through") != proof.get("complete_through"):
        reasons.append("complete_through differs from the record")
    if accepted.get("index_points") != (len(path) if isinstance(path, list) else None):
        reasons.append("the index point count differs from the record")
    index = _dec(proof.get("current_index"))
    if index is None or format(index.quantize(Decimal("0.0001")), "f") != str(
        accepted.get("current_index_4dp")
    ):
        reasons.append("the index to 4 decimals differs from the record")
    recorded = accepted.get("missing_daily_points")
    if not isinstance(recorded, list) or not recorded:
        return reasons + ["the record states no missing daily points"]
    recorded_keys = {_point_key(p) for p in recorded if isinstance(p, Mapping)}
    if len(recorded_keys) != len(recorded):
        reasons.append("the recorded missing points repeat or are malformed")
    if any(date in path_dates for _, date in recorded_keys):
        reasons.append("a recorded missing day is a point of the proof's own path")
    shown = {_point_key(p) for p in (panel_missing or ()) if isinstance(p, Mapping)}
    if shown != recorded_keys:
        reasons.append("the panel's missing daily points differ from the record")
    points = bounds.get("points") if isinstance(bounds, Mapping) else None
    if not isinstance(points, list):
        return reasons + ["the gap bounds document is absent or unreadable"]
    for key in sorted(recorded_keys):
        matches = [p for p in points if isinstance(p, Mapping) and _point_key(p) == key]
        if len(matches) != 1 or not _bound_is_safe(matches[0]):
            reasons.append(f"the gap bound for {key[0]} {key[1]} is absent or unsafe")
    return reasons

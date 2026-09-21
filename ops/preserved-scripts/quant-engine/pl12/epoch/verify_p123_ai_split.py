"""Teach the layered-history verifier the twin's admitted/overlap split.

The verifier required the resolution to be the derived identity plus a status,
which was exactly right while admission was all-or-nothing. The twin is now
split at the strategy's own account-live inception, so the resolution also
carries the boundary and the count on each side of it.

Those three fields are checked rather than restated: the counts must equal the
admitted and overlapping segment rows the verifier already has, and the
boundary must actually separate them -- every admitted row strictly before it,
every retained overlap row on or after it. A split that does not hold that
shape is not a split.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_target_series.py'
raw = path.read_bytes()

old_keys = b'''            {
                "index_observations",
                "observed_end",
                "observed_start",
                "relationship_type",
                "return_observations",
                "source_labels",
                "status",
            }
        ),
        "Portfolio123 AI backtest disposition",
    )'''
new_keys = b'''            {
                "admitted_prehistory_observations",
                "index_observations",
                "live_inception",
                "observed_end",
                "observed_start",
                "overlap_observations_not_admitted",
                "relationship_type",
                "return_observations",
                "source_labels",
                "status",
            }
        ),
        "Portfolio123 AI backtest disposition",
    )'''
assert raw.count(old_keys) == 1, raw.count(old_keys)
raw = raw.replace(old_keys, new_keys)

old_status = b'''    elif any(
        row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        for row in p123_ai_rows
    ):
        expected_p123_ai_status = "ADMITTED_EXACT_VERSION_PREHISTORY"
    elif any(
        row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP" for row in p123_ai_rows
    ):
        expected_p123_ai_status = "DERIVED_NOT_ADMITTED_OVERLAP"'''
new_status = b'''    elif any(
        row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        for row in p123_ai_rows
    ):
        expected_p123_ai_status = (
            "ADMITTED_EXACT_VERSION_PREHISTORY_STRICTLY_BEFORE_LIVE_INCEPTION"
            if any(
                row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP"
                for row in p123_ai_rows
            )
            else "ADMITTED_EXACT_VERSION_PREHISTORY"
        )
    elif any(
        row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP" for row in p123_ai_rows
    ):
        expected_p123_ai_status = "DERIVED_NOT_ADMITTED_OVERLAP"'''
assert raw.count(old_status) == 1, raw.count(old_status)
raw = raw.replace(old_status, new_status)

old_compare = b'''    if not isinstance(p123_ai_identity, dict) or p123_ai_resolution != {
        **p123_ai_identity,
        "status": expected_p123_ai_status,
    }:
        raise TargetSeriesError("Portfolio123 AI backtest disposition differs")'''
new_compare = b'''    admitted_rows = [
        row
        for row in p123_ai_rows
        if row["mode_admission"] == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
    ]
    overlap_rows = [
        row
        for row in p123_ai_rows
        if row["mode_admission"] == "REQUIRED_NOT_READY_OVERLAP"
    ]
    live_inception = str(p123_ai_resolution.get("live_inception") or "")
    if not isinstance(p123_ai_identity, dict) or p123_ai_resolution != {
        **p123_ai_identity,
        "admitted_prehistory_observations": len(admitted_rows),
        "live_inception": live_inception,
        "overlap_observations_not_admitted": len(overlap_rows),
        "status": expected_p123_ai_status,
    }:
        raise TargetSeriesError("Portfolio123 AI backtest disposition differs")
    # The boundary has to separate the two sides, or the counts describe nothing.
    if admitted_rows or overlap_rows:
        if (
            not live_inception
            or any(row["date"] >= live_inception for row in admitted_rows)
            or any(row["date"] < live_inception for row in overlap_rows)
        ):
            raise TargetSeriesError(
                "Portfolio123 AI backtest split does not hold its live inception"
            )'''
assert raw.count(old_compare) == 1, raw.count(old_compare)
raw = raw.replace(old_compare, new_compare)
path.write_bytes(raw)
print('ok')

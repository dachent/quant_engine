"""E2: an unavailable hwm_summary clears only behind an owner-accepted, matching governed proof.

Kill cases: accepted + valid clears; no acceptance, a missing or mismatching record, a missing
bound or an unsafe bound each leave E2 firing.  Synthetic numbers only.
"""

from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import checks
from acceptance.validate.generation import load_generation
from acceptance.validate.hwm_acceptance import verify_acceptance

from test_validate_generation import CUTOFF, _policy_fixture, build

PATH = [
    {"as_of": "2026-01-23", "index": "100"},
    {"as_of": "2026-01-26", "index": "101"},
    {"as_of": "2026-01-28", "index": "100.00004"},
]
MISSING = [{"date": "2026-01-27", "strategy_id": "alpha"}]


def proof(**over: Any) -> dict[str, Any]:
    return {
        "status": "AVAILABLE",
        "financial_acceptance": "OWNER_ACCEPTED",
        "policy_id": "PORT-HWM-4",
        "complete_through": "2026-01-28",
        "current_index": "100.00004",
        "index_path": copy.deepcopy(PATH),
        **over,
    }


def record(**over: Any) -> dict[str, Any]:
    return {
        "schema": "portfolio-hwm-owner-acceptance/1",
        "decision": "OWNER_ACCEPTED",
        "policy_id": "PORT-HWM-4",
        "accepted": {
            "complete_through": "2026-01-28",
            "index_points": 3,
            "current_index_4dp": "100.0000",
            "missing_daily_points": copy.deepcopy(MISSING),
        },
        **over,
    }


def bounds(**point_over: Any) -> dict[str, Any]:
    point = {
        "date": "2026-01-27",
        "strategy_id": "alpha",
        "origin": "ADMITTED_ALTERNATE_SOURCE",
        "nav": "0.85",
        "range_low": "0.85",
        "range_high": "0.85",
        "prior_high": "0.90",
        "max_observed_closure_error": "0.036",
        "tolerance_relative": "0.005",
        **point_over,
    }
    return {"points": [point]}


def reasons(**over: Any) -> list[str]:
    args = {
        "proof": proof(),
        "panel_missing": copy.deepcopy(MISSING),
        "record": record(),
        "bounds": bounds(),
        **over,
    }
    return verify_acceptance(
        args["proof"], args["panel_missing"], args["record"], args["bounds"]
    )


def test_accepted_and_valid_clears():
    assert reasons() == []


@pytest.mark.parametrize(
    "over",
    [
        {"proof": None},
        {"proof": proof(status="UNAVAILABLE")},
        {"proof": proof(financial_acceptance="PENDING_OWNER_ACCEPTANCE")},
        {"record": None},
        {"record": record(schema="x/1")},
        {"record": record(decision="PENDING")},
        {"record": record(policy_id="PORT-HWM-3")},
        {"bounds": None},
        {"panel_missing": []},
        {"panel_missing": [{"date": "2026-07-03", "strategy_id": "alpha"}]},
        {"proof": proof(complete_through="2026-01-29")},
        {"proof": proof(current_index="100.0100")},
    ],
    ids=lambda o: next(iter(o)),
)
def test_each_missing_or_mismatching_input_leaves_e2_firing(over):
    assert reasons(**over)


def test_a_changed_point_count_leaves_e2_firing():
    changed = proof(index_path=PATH + [{"as_of": "2026-01-29", "index": "100"}])
    assert reasons(proof=changed)


def test_a_recorded_gap_the_path_actually_has_is_not_what_was_accepted():
    day = {
        "date": "2026-01-26",
        "strategy_id": "alpha",
    }  # a point of the proof's own path
    rec = record()
    rec["accepted"]["missing_daily_points"] = [day]
    # everything else is consistent (panel and bound name the same day): only the path rule fires
    found = reasons(record=rec, panel_missing=[day], bounds=bounds(date="2026-01-26"))
    assert found == ["a recorded missing day is a point of the proof's own path"]


def test_the_record_must_state_missing_points():
    rec = record()
    rec["accepted"]["missing_daily_points"] = []
    assert reasons(record=rec, panel_missing=[])


@pytest.mark.parametrize(
    "point",
    [
        {"origin": "SELF_DECLARED"},  # origin not admitted
        {"range_high": "0.89"},  # 0.89 x 1.036 reaches 0.922 > prior high 0.90
        {
            "max_observed_closure_error": "0.20"
        },  # closure wide enough to reach the prior high
        {"prior_high": "0.85"},  # bound reaches the prior high
        {"nav": "0.80"},  # nav below the stated range
        {"tolerance_relative": "-1"},
        {"range_high": "not-a-number"},
    ],
    ids=lambda p: next(iter(p)),
)
def test_an_unsafe_or_malformed_gap_bound_leaves_e2_firing(point):
    assert reasons(bounds=bounds(**point))


def test_the_safe_boundary_is_strict():
    # upper = range_high x (1 + max(closure, tolerance)) = 0.85 x 1.036 = 0.8806 exactly
    safe = bounds(range_high="0.85", nav="0.85", prior_high="0.88061")
    edge = bounds(range_high="0.85", nav="0.85", prior_high="0.8806")
    assert reasons(bounds=safe) == []
    assert reasons(bounds=edge)  # reaching the prior high is unsafe


def test_a_duplicated_or_absent_bound_leaves_e2_firing():
    one = bounds()["points"][0]
    assert reasons(bounds={"points": [one, copy.deepcopy(one)]})
    assert reasons(bounds={"points": []})


# ---------------------------------------------------------------- the check itself


def _context(tmp_path: Path, *, accepted: bool, rec: Any = None, bnd: Any = None):
    gen_dir = build(tmp_path / "g")
    root = _policy_fixture(tmp_path / "authority")
    contracts = root / "contracts"
    contracts.mkdir(exist_ok=True)
    if rec is not False:
        (contracts / "portfolio-hwm-owner-acceptance-v1.json").write_text(
            json.dumps(rec if rec is not None else record())
        )
    (contracts / "portfolio-hwm-gap-bounds-v1.json").write_text(
        json.dumps(bnd if bnd is not None else bounds())
    )
    ctx = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or date.today(),
        cutoff_source="test",
        repo_root=root,
    )
    ctx.report["hwm_summary"] = {
        "hwm_numerical_status": "UNAVAILABLE",
        "hwm_numerical_reason": "NO_HWM_INPUTS",
    }
    ctx.panel["hwm_governed_proof"] = proof(
        financial_acceptance="OWNER_ACCEPTED"
        if accepted
        else "PENDING_OWNER_ACCEPTANCE"
    )
    ctx.panel["advisory"] = {
        "hwm_candidate": {"kpi": {"missing_daily_points": copy.deepcopy(MISSING)}}
    }
    return ctx


def _e2(ctx) -> list[checks.Finding]:
    return [f for f in checks.check_completeness(ctx) if "E2" in f.defect_ids]


def test_the_check_clears_e2_for_an_accepted_matching_proof(tmp_path):
    assert _e2(_context(tmp_path, accepted=True)) == []


def test_the_check_still_fires_without_acceptance(tmp_path):
    found = _e2(_context(tmp_path, accepted=False))
    assert len(found) == 1 and found[0].klass == checks.NOT_READY


def test_the_check_fires_when_the_record_is_absent_or_mismatched(tmp_path):
    assert len(_e2(_context(tmp_path / "a", accepted=True, rec=False))) == 1
    rec = record()
    rec["accepted"]["complete_through"] = "2026-01-21"
    found = _e2(_context(tmp_path / "b", accepted=True, rec=rec))
    assert len(found) == 1
    assert found[0].observed["owner_acceptance_not_covering"]


def test_the_check_fires_for_an_unsafe_bound(tmp_path):
    found = _e2(_context(tmp_path, accepted=True, bnd=bounds(prior_high="0.85")))
    assert len(found) == 1

"""Accepted-multi-day-interval support: derivation, validation, builder admission.

Owner ruling #324 comment 5383730751: 'Fri-close -> Mon-open is one accepted
interval.' Sources whose next observed-day return row embeds the full gap
(flow-adjusted) declare that gap as an accepted multi-day interval; the
authority binds the gap to that return row by hash. Distinct from carry
closures (exact capital reconciliation, nothing accrued inside).
"""

import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.portfolio_ledger.hwm_calendar_authority import (
    derive_governed_calendar_authority,
)
from quant_engine.portfolio_ledger.hwm_governed import (
    validate_governed_hwm_calendar_authority,
)


def _policy_bytes(tmp_path):
    """Real policy template with only the calendar seam flipped to READY."""
    policy = json.loads(
        Path("contracts/portfolio-hwm-policy-v4.json").read_text(encoding="utf-8")
    )
    policy["calendar_policy"]["release_authority_status"] = "READY"
    policy["calendar_policy"]["accepted_governed_calendar_policy_ids"] = [
        "TEST-MIXED-1"
    ]
    raw = json.dumps(policy, indent=2, sort_keys=True).encode()
    (tmp_path / "policy.json").write_bytes(raw)
    return raw


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _base_entry(sid="s-a", dates=("2026-01-02", "2026-01-05"), capital=None):
    d1, d2 = dates
    c = capital or {"d1": "100", "d2": "101"}
    return {
        "strategy_id": sid,
        "calendar_kind": "DECLARED_SESSION",
        "observed_dates": [d1, d2],
        "closing_capital_usd_by_date": {d1: c["d1"], d2: c["d2"]},
    }


def test_accepted_interval_gap_is_derived_and_binds_next_day_return():
    """Gap with embedded economics derives as an accepted interval, not closure."""
    entry = _base_entry(
        capital={"d1": "100", "d2": "103"},
    )
    entry["accepted_intervals"] = [
        {
            "prior_observation_date": "2026-01-02",
            "next_observation_date": "2026-01-05",
            "binding_return_date": "2026-01-05",
            "binding_return_value": "0.03",
        }
    ]
    candidate = derive_governed_calendar_authority(
        strategy_calendars=[entry],
        calendar_policy_id="TEST-MIXED-1",
        owner_decision_evidence="owner-comment-url",
        hwm_policy_sha256=_sha("policy"),
    )
    intervals = candidate["accepted_source_intervals"]
    assert len(intervals) == 1
    row = intervals[0]
    assert row["prior_observation_date"] == "2026-01-02"
    assert row["next_observation_date"] == "2026-01-05"
    assert row["treatment"] == "ACCEPTED_MULTI_DAY_RETURN_BINDING"
    assert row["interval_evidence_sha256"] == _sha(
        "\n".join(["s-a", row["calendar_id"], "2026-01-02", "2026-01-05", "0.03"])
    )


def test_unreconciled_gap_without_accepted_interval_still_fails_closed():
    """An unreconciled gap not declared as accepted interval keeps failing."""
    with pytest.raises(Exception, match="does not reconcile"):
        derive_governed_calendar_authority(
            strategy_calendars=[_base_entry(capital={"d1": "100", "d2": "103"})],
            calendar_policy_id="TEST-MIXED-1",
            owner_decision_evidence="owner-comment-url",
            hwm_policy_sha256=_sha("policy"),
        )


def test_validator_accepts_mixed_closures_and_accepted_intervals(tmp_path):
    """Round-trip: carry closures + accepted intervals pass a READY policy seam."""
    raw = _policy_bytes(tmp_path)
    policy_sha = hashlib.sha256(raw).hexdigest()
    carry = _base_entry(
        "s-carry", ("2026-01-02", "2026-01-05"), {"d1": "50", "d2": "50"}
    )
    mixed = _base_entry(
        "s-mixed", ("2026-01-02", "2026-01-05"), {"d1": "100", "d2": "103"}
    )
    mixed["accepted_intervals"] = [
        {
            "prior_observation_date": "2026-01-02",
            "next_observation_date": "2026-01-05",
            "binding_return_date": "2026-01-05",
            "binding_return_value": "0.03",
        }
    ]
    candidate = derive_governed_calendar_authority(
        strategy_calendars=[carry, mixed],
        calendar_policy_id="TEST-MIXED-1",
        owner_decision_evidence="owner-comment-url",
        hwm_policy_sha256=policy_sha,
    )
    assert candidate["closed_source_dates"], "carry closures expected"
    assert candidate["accepted_source_intervals"], "accepted intervals expected"
    validate_governed_hwm_calendar_authority(
        candidate,
        hwm_policy_bytes=_policy_bytes(tmp_path),
        accepted_authority_sha256=candidate["authority_evidence_sha256"],
    )


def test_validator_rejects_gap_with_no_disposition(tmp_path):
    """A gap covered by neither closure nor accepted interval fails closed."""
    entry = _base_entry("s-bad", ("2026-01-02", "2026-01-09"), {"d1": "10", "d2": "12"})
    entry["accepted_intervals"] = []  # explicitly none
    with pytest.raises(Exception):
        derive_governed_calendar_authority(
            strategy_calendars=[entry],
            calendar_policy_id="TEST-MIXED-1",
            owner_decision_evidence="owner-comment-url",
            hwm_policy_sha256=_sha("policy"),
        )


def test_builder_admits_declared_multi_day_interval_row():
    """Builder accepts a multi-day interval only when the authority declares it."""
    from quant_engine.portfolio_ledger.hwm_input_builder import (
        build_governed_hwm_inputs,
    )

    members = [
        {
            "strategy_id": "s-mixed",
            "effective_start": "2026-01-01",
            "evidence_ref": "packet",
        }
    ]
    capital = [
        {"strategy_id": "s-mixed", "date": "2026-01-02", "closing_capital_usd": "100"},
        {"strategy_id": "s-mixed", "date": "2026-01-06", "closing_capital_usd": "104"},
    ]
    rets = [
        {
            "strategy_id": "s-mixed",
            "interval_start": "2026-01-02",
            "interval_end": "2026-01-05",
            "return_basis": "ACCOUNT_LIVE_NET",
            "net_return": "0.03",
            "source_artifact_sha256": _sha("ret-row"),
        }
    ]
    gen = build_governed_hwm_inputs(
        members=members,
        capital_observations=capital,
        return_observations=rets,
        calendar_authority_sha256=_sha("cal"),
        hwm_policy_sha256=_sha("policy"),
        accepted_intervals={
            "s-mixed": [
                {
                    "prior_observation_date": "2026-01-02",
                    "next_observation_date": "2026-01-05",
                    "binding_return_date": "2026-01-05",
                }
            ],
        },
    )
    periods = [p for p in gen["component_periods"] if p["strategy_id"] == "s-mixed"]
    assert periods[0]["interval_end"] == "2026-01-05"


def test_builder_rejects_undeclared_multi_day_interval_row():
    """Multi-day rows outside the declared set still fail closed."""
    from quant_engine.portfolio_ledger.hwm_input_builder import (
        build_governed_hwm_inputs,
    )

    members = [
        {
            "strategy_id": "s-x",
            "effective_start": "2026-01-01",
            "evidence_ref": "packet",
        }
    ]
    capital = [
        {"strategy_id": "s-x", "date": "2026-01-02", "closing_capital_usd": "100"}
    ]
    rets = [
        {
            "strategy_id": "s-x",
            "interval_start": "2026-01-02",
            "interval_end": "2026-01-06",
            "return_basis": "ACCOUNT_LIVE_NET",
            "net_return": "0.04",
            "source_artifact_sha256": _sha("ret-row"),
        }
    ]
    with pytest.raises(Exception):
        build_governed_hwm_inputs(
            members=members,
            capital_observations=capital,
            return_observations=rets,
            calendar_authority_sha256=_sha("cal"),
            hwm_policy_sha256=_sha("policy"),
        )

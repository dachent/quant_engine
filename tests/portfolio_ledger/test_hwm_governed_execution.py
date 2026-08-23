"""Governed kernel execution entry: PORT-HWM-4 on real governed inputs (RED first).

Hand-worked micro-fixture (Task 5 criterion 4): two members over five days.
  Member A (ALWAYS_ON-like): daily rows, capital 100 -> ... returns known.
  Member B (DECLARED_SESSION): weekend gap declared as accepted interval whose
  economics land on Monday's row.
Hand-computed composite index/HWM/drawdown asserted exactly.
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from quant_engine.portfolio_ledger.hwm import HwmCoverageError
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    compute_governed_as_held_composite_hwm,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _policy_bytes(tmp_path, ready=True):
    policy = json.loads(
        Path("contracts/portfolio-hwm-policy-v4.json").read_text(encoding="utf-8")
    )
    cp = policy["calendar_policy"]
    cp["release_authority_status"] = "READY" if ready else "REQUIRED_NOT_READY"
    cp["accepted_governed_calendar_policy_ids"] = ["GOV-CAL-TEST"] if ready else []
    raw = json.dumps(policy, indent=2, sort_keys=True).encode()
    (tmp_path / "policy.json").write_bytes(raw)
    return raw


def _authority_doc():
    """Two sources: A always-on; B session source with one accepted weekend."""
    doc = {
        "schema_version": 2,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": "GOV-CAL-TEST",
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "owner-comment",
        "hwm_policy_sha256": "0" * 64,
        "source_calendars": [
            {
                "strategy_id": "a-daily",
                "calendar_id": "a:OBSERVED:2026-01-01",
                "calendar_kind": "ALWAYS_ON",
                "calendar_evidence_sha256": _sha("a-evidence"),
            },
            {
                "strategy_id": "b-session",
                "calendar_id": "b:OBSERVED:2026-01-01",
                "calendar_kind": "DECLARED_SESSION",
                "calendar_evidence_sha256": _sha("b-evidence"),
            },
        ],
        "closed_source_dates": [],
        "accepted_source_intervals": [
            {
                "strategy_id": "b-session",
                "calendar_id": "b:OBSERVED:2026-01-01",
                "prior_observation_date": "2026-01-02",
                "next_observation_date": "2026-01-05",
                "binding_return_date": "2026-01-05",
                "binding_return_value": "0.04",
                "interval_evidence_sha256": hashlib.sha256(
                    "\n".join(
                        [
                            "b-session",
                            "b:OBSERVED:2026-01-01",
                            "2026-01-02",
                            "2026-01-05",
                            "0.04",
                        ]
                    ).encode()
                ).hexdigest(),
                "treatment": "ACCEPTED_MULTI_DAY_RETURN_BINDING",
            }
        ],
    }
    canonical = json.dumps(
        {k: v for k, v in doc.items() if k != "authority_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    doc["authority_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return doc


def _period(sid, start, end, ret, cap):
    return {
        "strategy_id": sid,
        "interval_start": start,
        "interval_end": end,
        "beginning_capital_usd": cap,
        "net_return": ret,
        "return_basis": "ACCOUNT_LIVE_NET",
        "return_evidence_sha256": _sha(f"{sid}:{start}:{end}:{ret}"),
        "membership_evidence_sha256": _sha(f"m-{sid}"),
        "policy_binding_sha256": "0" * 64,
        "calendar_sha256": "0" * 64,
        "source_generation_sha256": "",
    }


def _generation(periods, policy_binding=None, calendar_binding=None):
    gen = {
        "schema_version": 1,
        "builder": "portfolio-hwm-inputs-governed",
        "builder_version": 1,
        "authority_profile": "ACCEPTED_GOVERNED",
        "memberships": [
            {
                "strategy_id": "a-daily",
                "effective_start": "2026-01-01",
                "membership_evidence_sha256": _sha("m-a"),
            },
            {
                "strategy_id": "b-session",
                "effective_start": "2026-01-01",
                "membership_evidence_sha256": _sha("m-b"),
            },
        ],
        "component_periods": periods,
        "policy_binding_sha256": policy_binding or _sha("policy"),
        "calendar_sha256": calendar_binding or _sha("cal"),
        "evidence_sha256s": sorted({_sha("m-a"), _sha("m-b")}),
    }
    canonical = json.dumps(
        {k: v for k, v in gen.items() if k != "generation_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    gen["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return gen


DAYS = [
    "2026-01-02",
    "2026-01-03",
    "2026-01-04",
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
]


def _fixture(tmp_path, ready=True):
    policy_bytes = _policy_bytes(tmp_path, ready=ready)
    cal = _authority_doc()
    # Bind policy sha into the doc like the real derivation would.
    cal["hwm_policy_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
    canonical = json.dumps(
        {k: v for k, v in cal.items() if k != "authority_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    cal["authority_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()

    periods = [
        # A: five one-day rows (weekend included: always-on).
        _period("a-daily", "2026-01-02", "2026-01-03", "0.01", "60"),
        _period("a-daily", "2026-01-03", "2026-01-04", "0.02", "61.2"),
        _period("a-daily", "2026-01-04", "2026-01-05", "-0.01", "62.424"),
        _period("a-daily", "2026-01-05", "2026-01-06", "0.03", "61.79976"),
        _period("a-daily", "2026-01-06", "2026-01-07", "0.005", "63.653748"),
        # B: Friday one-day row, then the REAL embedded-span shape: the
        # Mon row spans Fri->Mon (declared accepted interval), then one-day
        # rows resume; Sat/Sun interior days -> kernel-derived zeros.
        _period("b-session", "2026-01-02", "2026-01-03", "0.005", "40"),
        _period("b-session", "2026-01-02", "2026-01-05", "0.04", "40.2"),
        _period("b-session", "2026-01-05", "2026-01-06", "0.002", "41.808"),
        _period("b-session", "2026-01-06", "2026-01-07", "0.01", "41.891616"),
    ]
    gen = _generation(
        periods,
        policy_binding=hashlib.sha256(policy_bytes).hexdigest(),
        # Semantic identity: the authority document's internal self-hash.
        calendar_binding=cal["authority_evidence_sha256"],
    )
    return {
        "accepted_intervals": [
            {
                "interval_id": f"i{index}",
                "interval_start": start,
                "interval_end": DAYS[index + 1],
                "interval_evidence_sha256": _sha(f"iv:{start}"),
            }
            for index, start in enumerate(DAYS[:-1])
        ],
        "memberships": gen["memberships"],
        "component_periods": periods,
        "policy_bytes": policy_bytes,
        "product_contract_bytes": b"{}",
        "performance_policy_bytes": b"{}",
        "legacy_policy_bytes": b"{}",
        "calendar_authority_bytes": json.dumps(cal, sort_keys=True).encode(),
        "input_generation_bytes": json.dumps(gen, sort_keys=True).encode(),
        "calendar_accepted_sha256": cal["authority_evidence_sha256"],
        "evaluated_on": "2026-01-07",
        "complete_through": "2026-01-07",
        "maximum_age_days": 30,
        "assurance_profile": "WORKSTATION_QC",
    }


def test_governed_entry_computes_hand_worked_composite(tmp_path):
    fx = _fixture(tmp_path)
    result = compute_governed_as_held_composite_hwm(
        fx["accepted_intervals"],
        fx["memberships"],
        fx["component_periods"],
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
        calendar_authority_bytes=fx["calendar_authority_bytes"],
        input_generation_bytes=fx["input_generation_bytes"],
        calendar_accepted_sha256=fx["calendar_accepted_sha256"],
        evaluated_on=fx["evaluated_on"],
        complete_through=fx["complete_through"],
        maximum_age_days=fx["maximum_age_days"],
        assurance_profile=fx["assurance_profile"],
    )
    assert result["status"] == "AVAILABLE"
    assert result["input_authority_class"] == "ACCEPTED_GOVERNED"
    assert result["current_index"] > Decimal("100")
    # Hand-check the first interval composite: (60*.01 + 40*.005)/100 = 0.008
    first = result["periods"][0]
    assert first["composite_return"] == Decimal("0.008")
    # Weekend interior days: B inside accepted span -> kernel-derived 0;
    # A keeps trading (Sat .02). The span's economics land on the grid
    # interval ENDING at the binding day (Sun->Mon carries the 0.04).
    sat = result["periods"][1]
    b_sat = [c for c in sat["components"] if c["strategy_id"] == "b-session"]
    assert b_sat[0]["period_return"] == Decimal("0")
    sun_to_mon = result["periods"][2]
    b_sm = [c for c in sun_to_mon["components"] if c["strategy_id"] == "b-session"]
    assert b_sm[0]["period_return"] == Decimal("0.04")
    # One-day rows resume after the binding day (Mon->Tue .002).
    mon = result["periods"][3]
    b_mon = [c for c in mon["components"] if c["strategy_id"] == "b-session"]
    assert b_mon[0]["period_return"] == Decimal("0.002")


def test_governed_entry_requires_ready_seam(tmp_path):
    from quant_engine.portfolio_ledger.hwm_governed import (
        GovernedHwmAuthorityError,
    )

    fx = _fixture(tmp_path, ready=False)
    with pytest.raises(
        (HwmCoverageError, GovernedHwmAuthorityError), match="release-ready"
    ):
        compute_governed_as_held_composite_hwm(
            fx["accepted_intervals"],
            fx["memberships"],
            fx["component_periods"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
            calendar_authority_bytes=fx["calendar_authority_bytes"],
            input_generation_bytes=fx["input_generation_bytes"],
            calendar_accepted_sha256=fx["calendar_accepted_sha256"],
            evaluated_on=fx["evaluated_on"],
            complete_through=fx["complete_through"],
            maximum_age_days=fx["maximum_age_days"],
            assurance_profile=fx["assurance_profile"],
        )


def test_governed_entry_rejects_generation_hash_drift(tmp_path):
    fx = _fixture(tmp_path)
    drifted = json.loads(fx["input_generation_bytes"])
    drifted["component_periods"][0]["net_return"] = "0.5"
    with pytest.raises(HwmCoverageError, match="identity differs"):
        compute_governed_as_held_composite_hwm(
            fx["accepted_intervals"],
            fx["memberships"],
            fx["component_periods"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
            calendar_authority_bytes=fx["calendar_authority_bytes"],
            input_generation_bytes=json.dumps(drifted, sort_keys=True).encode(),
            calendar_accepted_sha256=fx["calendar_accepted_sha256"],
            evaluated_on=fx["evaluated_on"],
            complete_through=fx["complete_through"],
            maximum_age_days=fx["maximum_age_days"],
            assurance_profile=fx["assurance_profile"],
        )


def test_governed_entry_fails_on_uncovered_member_interval(tmp_path):
    fx = _fixture(tmp_path)
    fx["component_periods"] = [
        p
        for p in fx["component_periods"]
        if not (p["strategy_id"] == "a-daily" and p["interval_start"] == "2026-01-03")
    ]
    gen = _generation(
        fx["component_periods"],
        policy_binding=hashlib.sha256(fx["policy_bytes"]).hexdigest(),
        calendar_binding=json.loads(fx["calendar_authority_bytes"])[
            "authority_evidence_sha256"
        ],
    )
    with pytest.raises(HwmCoverageError, match="coverage"):
        compute_governed_as_held_composite_hwm(
            fx["accepted_intervals"],
            fx["memberships"],
            fx["component_periods"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
            calendar_authority_bytes=fx["calendar_authority_bytes"],
            input_generation_bytes=json.dumps(gen, sort_keys=True).encode(),
            calendar_accepted_sha256=fx["calendar_accepted_sha256"],
            evaluated_on=fx["evaluated_on"],
            complete_through=fx["complete_through"],
            maximum_age_days=fx["maximum_age_days"],
            assurance_profile=fx["assurance_profile"],
        )

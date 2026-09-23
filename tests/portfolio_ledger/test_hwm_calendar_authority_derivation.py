"""Derivation of a governed mixed-calendar authority candidate from evidence.

The derivation consumes observed per-source interval sequences and produces a
candidate that passes the exact identity semantics enforced by
``validate_governed_hwm_calendar_authority``.  It never infers holidays,
never zero-fills, and fails closed when an always-on source misses an
observation or a declared-session gap fails the capital carry reconciliation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quant_engine.portfolio_ledger.hwm_governed import (
    CLOSED_SOURCE_REASON,
    CLOSED_SOURCE_TREATMENT,
    GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS,
    GOVERNED_HWM_CALENDAR_SCHEMA_VERSION,
    GOVERNED_HWM_PORTFOLIO_CALENDAR,
    GovernedHwmAuthorityError,
)
from quant_engine.portfolio_ledger.hwm_calendar_authority import (
    derive_governed_calendar_authority,
)


def _canon(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


POLICY_SHA256 = "a" * 64


def _always_on(strategy_id: str, start_day: int, end_day: int) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "calendar_kind": "ALWAYS_ON",
        "observed_dates": [
            f"2026-01-{day:02d}" for day in range(start_day, end_day + 1)
        ],
        "closing_capital_usd_by_date": {},
    }


def _declared_session(
    strategy_id: str,
    observed_dates: list[str],
    capital_by_date: dict[str, str],
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "calendar_kind": "DECLARED_SESSION",
        "observed_dates": observed_dates,
        "closing_capital_usd_by_date": capital_by_date,
    }


def test_always_on_contiguous_history_derives_clean_authority() -> None:
    candidate = derive_governed_calendar_authority(
        strategy_calendars=[_always_on("robuxio-crypto", 1, 5)],
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )

    assert candidate["schema_version"] == GOVERNED_HWM_CALENDAR_SCHEMA_VERSION
    assert candidate["authority_class"] == GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS
    assert candidate["portfolio_calendar"] == GOVERNED_HWM_PORTFOLIO_CALENDAR
    assert candidate["calendar_policy_id"] == "GOV-CAL-001"
    assert candidate["hwm_policy_sha256"] == POLICY_SHA256
    assert candidate["source_calendars"][0]["calendar_kind"] == "ALWAYS_ON"
    assert candidate["closed_source_dates"] == []


def test_declared_gap_emits_full_closure_rows() -> None:
    candidate = derive_governed_calendar_authority(
        strategy_calendars=[
            _declared_session(
                "p123-eu",
                ["2026-01-02", "2026-01-05"],
                {"2026-01-02": "1000", "2026-01-05": "1000"},
            )
        ],
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )

    closures = candidate["closed_source_dates"]
    assert [row["date"] for row in closures] == ["2026-01-03", "2026-01-04"]
    for row in closures:
        assert row["closure_reason"] == CLOSED_SOURCE_REASON
        assert row["treatment"] == CLOSED_SOURCE_TREATMENT
        assert row["prior_observation_date"] == "2026-01-02"
        assert row["next_observation_date"] == "2026-01-05"
        assert row["prior_ending_capital_usd"] == "1000"
        assert row["next_beginning_capital_usd"] == "1000"


def test_root_hash_matches_external_acceptance_identity() -> None:
    candidate = derive_governed_calendar_authority(
        strategy_calendars=[
            _always_on("robuxio-crypto", 1, 3),
            _declared_session(
                "p123-eu",
                ["2026-01-02", "2026-01-06"],
                {"2026-01-02": "500", "2026-01-06": "500"},
            ),
        ],
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )

    unhashed = dict(candidate)
    declared = unhashed.pop("authority_evidence_sha256")
    assert hashlib.sha256(_canon(unhashed)).hexdigest() == declared


def test_distinct_declared_session_histories_bind_distinct_identities() -> None:
    kwargs = dict(
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )
    short = derive_governed_calendar_authority(
        strategy_calendars=[
            _declared_session("p123-eu", ["2026-01-01", "2026-01-02"], {})
        ],
        **kwargs,
    )
    long = derive_governed_calendar_authority(
        strategy_calendars=[
            _declared_session(
                "p123-eu",
                ["2026-01-01", "2026-01-02", "2026-01-03"],
                {},
            )
        ],
        **kwargs,
    )
    assert (
        short["source_calendars"][0]["calendar_evidence_sha256"]
        != long["source_calendars"][0]["calendar_evidence_sha256"]
    )
    assert short["authority_evidence_sha256"] != long["authority_evidence_sha256"]


def test_gap_with_drifting_capital_fails_closed() -> None:
    with pytest.raises(GovernedHwmAuthorityError, match="reconcile"):
        derive_governed_calendar_authority(
            strategy_calendars=[
                _declared_session(
                    "p123-eu",
                    ["2026-01-02", "2026-01-05"],
                    {"2026-01-02": "1000", "2026-01-05": "1010"},
                )
            ],
            calendar_policy_id="GOV-CAL-001",
            owner_decision_evidence="owner acceptance pending",
            hwm_policy_sha256=POLICY_SHA256,
        )


def test_always_on_with_missing_day_fails_closed() -> None:
    with pytest.raises(GovernedHwmAuthorityError, match="ALWAYS_ON"):
        derive_governed_calendar_authority(
            strategy_calendars=[_always_on_with_hole()],
            calendar_policy_id="GOV-CAL-001",
            owner_decision_evidence="owner acceptance pending",
            hwm_policy_sha256=POLICY_SHA256,
        )


def _always_on_with_hole() -> dict[str, object]:
    return {
        "strategy_id": "robuxio-crypto",
        "calendar_kind": "ALWAYS_ON",
        "observed_dates": ["2026-01-01", "2026-01-03"],
        "closing_capital_usd_by_date": {},
    }


def test_unknown_kind_and_short_histories_rejected() -> None:
    with pytest.raises(GovernedHwmAuthorityError):
        derive_governed_calendar_authority(
            strategy_calendars=[
                {
                    "strategy_id": "x",
                    "calendar_kind": "WEEKDAYS_ONLY",
                    "observed_dates": ["2026-01-01"],
                    "closing_capital_usd_by_date": {},
                }
            ],
            calendar_policy_id="GOV-CAL-001",
            owner_decision_evidence="e",
            hwm_policy_sha256=POLICY_SHA256,
        )
    with pytest.raises(GovernedHwmAuthorityError):
        derive_governed_calendar_authority(
            strategy_calendars=[],
            calendar_policy_id="GOV-CAL-001",
            owner_decision_evidence="e",
            hwm_policy_sha256=POLICY_SHA256,
        )


def _ready_policy_bytes(policy_sha256: str) -> tuple[bytes, str]:
    """A synthetic PORT-HWM-4-valid policy whose calendar seam is READY."""

    from quant_engine.portfolio_ledger.hwm import validate_as_held_hwm_policy

    repo_policy_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "portfolio-hwm-policy-v4.json"
    )
    policy = json.loads(repo_policy_path.read_text(encoding="utf-8"))
    calendar = policy["calendar_policy"]
    calendar["release_authority_status"] = "READY"
    calendar["accepted_governed_calendar_policy_ids"] = ["GOV-CAL-001"]
    validate_as_held_hwm_policy(policy)
    payload = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def test_derived_candidate_passes_governed_validator_round_trip() -> None:
    from quant_engine.portfolio_ledger.hwm_governed import (
        validate_governed_hwm_calendar_authority,
    )

    candidate = derive_governed_calendar_authority(
        strategy_calendars=[
            _always_on("robuxio-crypto", 1, 3),
            _declared_session(
                "p123-eu",
                ["2026-01-02", "2026-01-06"],
                {"2026-01-02": "500", "2026-01-06": "500"},
            ),
        ],
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )
    ready_bytes, ready_sha = _ready_policy_bytes(POLICY_SHA256)

    rebound = dict(candidate)
    rebound["hwm_policy_sha256"] = ready_sha
    unhashed = dict(rebound)
    unhashed.pop("authority_evidence_sha256")
    rebound["authority_evidence_sha256"] = hashlib.sha256(_canon(unhashed)).hexdigest()

    validate_governed_hwm_calendar_authority(
        rebound,
        hwm_policy_bytes=ready_bytes,
        accepted_authority_sha256=rebound["authority_evidence_sha256"],
    )


def test_derived_candidate_fails_closed_while_policy_not_ready() -> None:
    from quant_engine.portfolio_ledger.hwm_governed import (
        validate_governed_hwm_calendar_authority,
    )

    candidate = derive_governed_calendar_authority(
        strategy_calendars=[_always_on("robuxio-crypto", 1, 3)],
        calendar_policy_id="GOV-CAL-001",
        owner_decision_evidence="owner acceptance pending",
        hwm_policy_sha256=POLICY_SHA256,
    )
    repo_policy_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "portfolio-hwm-policy-v4.json"
    )
    # The production seam is now READY per the owner ruling (#324 comment
    # 5383730751), so this test derives a self-contained REQUIRED_NOT_READY
    # policy from the committed bytes to exercise the fail-closed path.
    policy = json.loads(repo_policy_path.read_text(encoding="utf-8"))
    policy["calendar_policy"]["release_authority_status"] = "REQUIRED_NOT_READY"
    policy["calendar_policy"]["accepted_governed_calendar_policy_ids"] = []
    real_bytes = json.dumps(policy, indent=2, sort_keys=True).encode()
    real_sha = hashlib.sha256(real_bytes).hexdigest()
    bound = dict(candidate)
    bound["hwm_policy_sha256"] = real_sha
    unhashed = dict(bound)
    unhashed.pop("authority_evidence_sha256")
    bound["authority_evidence_sha256"] = hashlib.sha256(_canon(unhashed)).hexdigest()

    with pytest.raises(GovernedHwmAuthorityError, match="release-ready"):
        validate_governed_hwm_calendar_authority(
            bound,
            hwm_policy_bytes=real_bytes,
            accepted_authority_sha256=bound["authority_evidence_sha256"],
        )

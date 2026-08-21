from __future__ import annotations

import hashlib
import json

import pytest

from quant_engine.portfolio_ledger import (
    CLOSED_SOURCE_TREATMENT,
    GovernedHwmAuthorityError,
    validate_governed_hwm_calendar_authority,
)


POLICY_ID = "HWM_ALWAYS_ON_DECLARED_SOURCE_CLOSURES_V1"
SHA_A = "a" * 64
SHA_C = "c" * 64


def _hash(value: dict[str, object]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _authority(*, hwm_policy_sha256: str) -> dict[str, object]:
    closure = {
        "strategy_id": "p123-ai-factor",
        "date": "2026-08-08",
        "calendar_id": "P123_CONNECTED_ACCOUNT_SESSION_V1",
        "closure_reason": "DECLARED_SOURCE_CLOSED_SESSION",
        "prior_observation_date": "2026-08-07",
        "next_observation_date": "2026-08-09",
        "prior_ending_capital_usd": "57140.197298561",
        "next_beginning_capital_usd": "57140.197298561",
        "closure_evidence_sha256": SHA_C,
        "treatment": CLOSED_SOURCE_TREATMENT,
    }
    authority: dict[str, object] = {
        "schema_version": 2,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": POLICY_ID,
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "accepted-owner-decision",
        "hwm_policy_sha256": hwm_policy_sha256,
        "source_calendars": [
            {
                "strategy_id": "p123-ai-factor",
                "calendar_id": "P123_CONNECTED_ACCOUNT_SESSION_V1",
                "calendar_kind": "DECLARED_SESSION",
                "calendar_evidence_sha256": SHA_A,
            }
        ],
        "closed_source_dates": [closure],
    }
    authority["authority_evidence_sha256"] = _hash(authority)
    return authority


def test_partial_caller_policy_cannot_claim_ready() -> None:
    partial_policy = {
        "policy_id": "PORT-HWM-4",
        "calendar_policy": {
            "authority": "HASH_BOUND_ACCEPTED_INTERVAL_SEQUENCE",
            "interval_rule": "EXACT_CONTIGUOUS_ONE_DAY_ACCEPTED_INTERVALS",
            "missing_interval": "FAIL_NO_ZERO_FILL_OR_HOLIDAY_INFERENCE",
            "release_authority_status": "READY",
            "accepted_governed_calendar_policy_ids": [POLICY_ID],
            "portable_synthetic_policy_id": "SYNTHETIC_DAILY_COMMON_CALENDAR",
        },
    }
    partial_policy_bytes = _json_bytes(partial_policy)
    authority = _authority(
        hwm_policy_sha256=hashlib.sha256(partial_policy_bytes).hexdigest()
    )
    accepted_hash = authority["authority_evidence_sha256"]
    assert isinstance(accepted_hash, str)

    with pytest.raises(GovernedHwmAuthorityError, match="policy"):
        validate_governed_hwm_calendar_authority(
            authority,
            hwm_policy_bytes=partial_policy_bytes,
            accepted_authority_sha256=accepted_hash,
        )

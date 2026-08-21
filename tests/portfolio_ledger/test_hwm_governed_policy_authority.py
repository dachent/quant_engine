from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio_ledger import (
    CLOSED_SOURCE_TREATMENT,
    GovernedHwmAuthorityError,
    validate_governed_hwm_calendar_authority,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_POLICY = json.loads(
    (_REPO_ROOT / "contracts" / "portfolio-hwm-policy-v4.json").read_text(
        encoding="utf-8"
    )
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


def _policy_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _ready_policy() -> dict[str, Any]:
    policy = deepcopy(_BASE_POLICY)
    calendar = policy["calendar_policy"]
    assert isinstance(calendar, dict)
    calendar["release_authority_status"] = "READY"
    calendar["accepted_governed_calendar_policy_ids"] = [POLICY_ID]
    return policy


def _authority() -> dict[str, object]:
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
        "schema_version": 1,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": POLICY_ID,
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "accepted-owner-decision",
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
    authority = _authority()
    accepted_hash = authority["authority_evidence_sha256"]
    assert isinstance(accepted_hash, str)
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

    with pytest.raises(GovernedHwmAuthorityError, match="policy"):
        validate_governed_hwm_calendar_authority(
            authority,
            hwm_policy=partial_policy,
            accepted_authority_sha256=accepted_hash,
        )


def test_complete_policy_cannot_replace_accepted_policy_bytes() -> None:
    authority = _authority()
    accepted_authority_hash = authority["authority_evidence_sha256"]
    assert isinstance(accepted_authority_hash, str)

    accepted_policy = _ready_policy()
    accepted_policy_bytes = _policy_bytes(accepted_policy)
    accepted_policy_hash = hashlib.sha256(accepted_policy_bytes).hexdigest()

    forged_policy = deepcopy(accepted_policy)
    receipts = forged_policy["owner_decision_receipts"]
    assert isinstance(receipts, dict)
    receipts["FX_BASE_CURRENCY"] = "f" * 64
    forged_policy_bytes = _policy_bytes(forged_policy)

    with pytest.raises(GovernedHwmAuthorityError, match="externally accepted policy"):
        validate_governed_hwm_calendar_authority(
            authority,
            hwm_policy=forged_policy,
            hwm_policy_bytes=forged_policy_bytes,
            accepted_hwm_policy_sha256=accepted_policy_hash,
            accepted_authority_sha256=accepted_authority_hash,
        )

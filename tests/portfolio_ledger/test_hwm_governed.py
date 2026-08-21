from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_engine.portfolio_ledger import (
    CLOSED_SOURCE_TREATMENT,
    GovernedHwmAuthorityError,
    governed_closed_source_return,
    governed_hwm_calendar_authority_bytes,
    validate_governed_hwm_calendar_authority,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_POLICY = json.loads(
    (_REPO_ROOT / "contracts" / "portfolio-hwm-policy-v4.json").read_text(
        encoding="utf-8"
    )
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
POLICY_ID = "HWM_ALWAYS_ON_DECLARED_SOURCE_CLOSURES_V1"


def _hash(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _policy(*, ready: bool = True, accepted: bool = True) -> dict[str, Any]:
    policy = deepcopy(_BASE_POLICY)
    calendar = policy["calendar_policy"]
    assert isinstance(calendar, dict)
    calendar["release_authority_status"] = "READY" if ready else "REQUIRED_NOT_READY"
    calendar["accepted_governed_calendar_policy_ids"] = (
        [POLICY_ID] if ready and accepted else []
    )
    return policy


def _closure(day: str) -> dict[str, Any]:
    return {
        "strategy_id": "p123-ai-factor",
        "date": day,
        "calendar_id": "P123_CONNECTED_ACCOUNT_SESSION_V1",
        "closure_reason": "DECLARED_SOURCE_CLOSED_SESSION",
        "prior_observation_date": "2026-08-07",
        "next_observation_date": "2026-08-10",
        "prior_ending_capital_usd": "57140.197298561",
        "next_beginning_capital_usd": "57140.197298561",
        "closure_evidence_sha256": SHA_C,
        "treatment": CLOSED_SOURCE_TREATMENT,
    }


def _authority() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": POLICY_ID,
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "issue:283#5306020941+issue:320#5328916249",
        "source_calendars": [
            {
                "strategy_id": "p123-ai-factor",
                "calendar_id": "P123_CONNECTED_ACCOUNT_SESSION_V1",
                "calendar_kind": "DECLARED_SESSION",
                "calendar_evidence_sha256": SHA_A,
            },
            {
                "strategy_id": "robuxio-crypto",
                "calendar_id": "CRYPTO_ALWAYS_ON_V1",
                "calendar_kind": "ALWAYS_ON",
                "calendar_evidence_sha256": SHA_B,
            },
        ],
        "closed_source_dates": [_closure("2026-08-08"), _closure("2026-08-09")],
    }
    value["authority_evidence_sha256"] = _hash(value)
    return value


def _rehash(authority: dict[str, Any]) -> None:
    payload = dict(authority)
    payload.pop("authority_evidence_sha256")
    authority["authority_evidence_sha256"] = _hash(payload)


def _accepted_hash(authority: dict[str, Any]) -> str:
    value = authority["authority_evidence_sha256"]
    assert isinstance(value, str)
    return value


def _validate(
    authority: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    accepted_hash: str | None = None,
) -> None:
    validate_governed_hwm_calendar_authority(
        authority,
        hwm_policy=_policy() if policy is None else policy,
        accepted_authority_sha256=(
            _accepted_hash(authority) if accepted_hash is None else accepted_hash
        ),
    )


def _closed_return(authority: dict[str, Any], day: str) -> Decimal | None:
    return governed_closed_source_return(
        authority,
        hwm_policy=_policy(),
        accepted_authority_sha256=_accepted_hash(authority),
        strategy_id="p123-ai-factor",
        as_of=day,
    )


def test_closed_date_zero_requires_policy_acceptance() -> None:
    authority = _authority()
    _validate(authority)

    assert _closed_return(authority, "2026-08-08") == Decimal("0")
    assert _closed_return(authority, "2026-08-07") is None

    crypto = governed_closed_source_return(
        authority,
        hwm_policy=_policy(),
        accepted_authority_sha256=_accepted_hash(authority),
        strategy_id="robuxio-crypto",
        as_of="2026-08-08",
    )
    assert crypto is None

    result = governed_hwm_calendar_authority_bytes(
        authority,
        hwm_policy=_policy(),
        accepted_authority_sha256=_accepted_hash(authority),
    )
    expected = json.dumps(
        authority,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert result == expected


def test_not_ready_policy_cannot_promote() -> None:
    authority = _authority()
    with pytest.raises(GovernedHwmAuthorityError, match="not release-ready"):
        _validate(authority, policy=_policy(ready=False))


def test_self_rehash_cannot_promote_policy_id() -> None:
    authority = _authority()
    authority["calendar_policy_id"] = "ATTACKER_SELECTED_POLICY"
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="not accepted"):
        _validate(authority)


def test_self_rehash_cannot_replace_accepted_authority() -> None:
    authority = _authority()
    accepted_hash = _accepted_hash(authority)
    for closure in authority["closed_source_dates"]:
        closure["closure_evidence_sha256"] = SHA_D
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="not externally accepted"):
        _validate(authority, accepted_hash=accepted_hash)


def test_closure_requires_capital_continuity() -> None:
    authority = _authority()
    authority["closed_source_dates"][0]["next_beginning_capital_usd"] = (
        "57140.197298562"
    )
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="does not reconcile"):
        _validate(authority)


def test_closure_gap_cannot_change_capital() -> None:
    authority = _authority()
    second = authority["closed_source_dates"][1]
    second["prior_ending_capital_usd"] = "999"
    second["next_beginning_capital_usd"] = "999"
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="capital/evidence varies"):
        _validate(authority)


def test_closure_brackets_must_be_observed() -> None:
    authority = _authority()
    first, second = authority["closed_source_dates"]
    first["next_observation_date"] = "2026-08-09"
    second["prior_observation_date"] = "2026-08-08"
    second["prior_ending_capital_usd"] = "999"
    second["next_beginning_capital_usd"] = "999"
    second["closure_evidence_sha256"] = SHA_D
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="bracket date is not observed"):
        _validate(authority)


def test_closure_gap_cannot_omit_date() -> None:
    authority = _authority()
    authority["closed_source_dates"] = [authority["closed_source_dates"][0]]
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="omits a date"):
        _validate(authority)


def test_missing_data_cannot_be_relabelled_closed() -> None:
    authority = _authority()
    authority["closed_source_dates"][0]["closure_reason"] = "MISSING_DATA_IS_CLOSED"
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="reason differs"):
        _validate(authority)


def test_always_on_source_cannot_close() -> None:
    authority = _authority()
    closure = deepcopy(authority["closed_source_dates"][0])
    closure["strategy_id"] = "robuxio-crypto"
    closure["calendar_id"] = "CRYPTO_ALWAYS_ON_V1"
    authority["closed_source_dates"] = [closure]
    _rehash(authority)

    with pytest.raises(GovernedHwmAuthorityError, match="ALWAYS_ON source"):
        _validate(authority)


def test_policy_cannot_weaken_missing_interval_semantics() -> None:
    policy = _policy()
    policy["calendar_policy"]["missing_interval"] = "ZERO_FILL_MISSING_INTERVALS"

    with pytest.raises(GovernedHwmAuthorityError, match="policy"):
        _validate(_authority(), policy=policy)


def test_tampered_authority_hash_rejects() -> None:
    authority = _authority()
    accepted_hash = _accepted_hash(authority)
    authority["authority_evidence_sha256"] = "f" * 64

    with pytest.raises(GovernedHwmAuthorityError, match="identity differs"):
        _validate(authority, accepted_hash=accepted_hash)

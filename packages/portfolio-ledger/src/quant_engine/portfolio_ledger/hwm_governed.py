"""Governed mixed-calendar authority for the as-held HWM.

An accepted closed source session is different from a missing observation.
This module verifies that distinction without inferring holidays or weekends.
A candidate payload cannot accept itself: callers must supply the exact
authority SHA-256 already accepted by an external governed receipt.

The complete PORT-HWM-4 policy is validated inside this calendar-specific seam
before any release-readiness field is honored.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from quant_engine.portfolio_ledger.hwm import (
    HwmCoverageError,
    validate_as_held_hwm_policy,
)


GOVERNED_HWM_CALENDAR_SCHEMA_VERSION: Final = 2
GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS: Final = "ACCEPTED_GOVERNED"
GOVERNED_HWM_PORTFOLIO_CALENDAR: Final = "ALWAYS_ON"
CLOSED_SOURCE_REASON: Final = "DECLARED_SOURCE_CLOSED_SESSION"
CLOSED_SOURCE_TREATMENT: Final = "CARRY_CAPITAL_NO_SOURCE_OBSERVATION"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

Row = Mapping[str, Any]
ClosureKey = tuple[str, date]
GapKey = tuple[str, date, date]


class GovernedHwmAuthorityError(ValueError):
    """A governed HWM calendar authority is inconsistent or forged."""


def _canonical_json_bytes(value: Row) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Row) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernedHwmAuthorityError(f"{field} must be non-empty text")
    return value.strip()


def _sha256(value: object, field: str) -> str:
    accepted = _text(value, field)
    if _SHA256_RE.fullmatch(accepted) is None:
        raise GovernedHwmAuthorityError(f"{field} must be lowercase SHA-256")
    return accepted


def _date(value: object, field: str) -> date:
    accepted = _text(value, field)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        message = f"{field} must be a canonical date"
        raise GovernedHwmAuthorityError(message) from exc
    if parsed.isoformat() != accepted:
        raise GovernedHwmAuthorityError(f"{field} must be a canonical date")
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise GovernedHwmAuthorityError(f"{field} must be decimal text")
    try:
        accepted = Decimal(value)
    except InvalidOperation as exc:
        raise GovernedHwmAuthorityError(f"{field} must be decimal text") from exc
    if not accepted.is_finite():
        raise GovernedHwmAuthorityError(f"{field} must be finite")
    return accepted


def _mapping(value: object, field: str) -> Row:
    if not isinstance(value, Mapping):
        raise GovernedHwmAuthorityError(f"{field} must be an object")
    return value


def _objects(value: object, field: str) -> list[Row]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GovernedHwmAuthorityError(f"{field} must be an array")
    return [_mapping(item, field) for item in value]


def _policy_calendar(hwm_policy_bytes: bytes, expected_sha256: str) -> Row:
    if not isinstance(hwm_policy_bytes, bytes) or not hwm_policy_bytes:
        raise GovernedHwmAuthorityError("HWM policy must be non-empty bytes")
    if hashlib.sha256(hwm_policy_bytes).hexdigest() != expected_sha256:
        raise GovernedHwmAuthorityError("HWM policy identity differs")
    try:
        parsed = json.loads(hwm_policy_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernedHwmAuthorityError("HWM policy bytes must contain JSON") from exc
    hwm_policy = _mapping(parsed, "HWM policy bytes")
    try:
        validate_as_held_hwm_policy(hwm_policy)
    except HwmCoverageError as exc:
        raise GovernedHwmAuthorityError(
            "HWM policy is not a complete PORT-HWM-4 policy"
        ) from exc

    calendar = _mapping(hwm_policy.get("calendar_policy"), "policy.calendar_policy")
    required = {
        "authority",
        "interval_rule",
        "missing_interval",
        "release_authority_status",
        "accepted_governed_calendar_policy_ids",
        "portable_synthetic_policy_id",
    }
    if set(calendar) != required:
        raise GovernedHwmAuthorityError("PORT-HWM-4 calendar policy shape differs")

    semantics = (
        calendar["authority"],
        calendar["interval_rule"],
        calendar["missing_interval"],
    )
    expected = (
        "HASH_BOUND_ACCEPTED_INTERVAL_SEQUENCE",
        "EXACT_CONTIGUOUS_ONE_DAY_ACCEPTED_INTERVALS",
        "FAIL_NO_ZERO_FILL_OR_HOLIDAY_INFERENCE",
    )
    if semantics != expected:
        raise GovernedHwmAuthorityError("PORT-HWM-4 calendar semantics differ")
    return calendar


def _source_calendars(authority: Row) -> dict[str, Row]:
    rows = _objects(authority.get("source_calendars"), "source_calendars")
    by_strategy: dict[str, Row] = {}
    for row in rows:
        required = {
            "strategy_id",
            "calendar_id",
            "calendar_kind",
            "calendar_evidence_sha256",
        }
        if set(row) != required:
            raise GovernedHwmAuthorityError("source calendar shape differs")

        strategy_id = _text(row.get("strategy_id"), "source calendar strategy_id")
        if strategy_id in by_strategy:
            raise GovernedHwmAuthorityError("source calendar strategy is duplicated")
        _text(row.get("calendar_id"), "source calendar calendar_id")
        if row.get("calendar_kind") not in {"ALWAYS_ON", "DECLARED_SESSION"}:
            raise GovernedHwmAuthorityError("source calendar kind differs")
        _sha256(row.get("calendar_evidence_sha256"), "source calendar evidence")
        by_strategy[strategy_id] = row
    return by_strategy


def _expected_gap_days(prior: date, following: date) -> list[date]:
    result: list[date] = []
    cursor = prior + timedelta(days=1)
    while cursor < following:
        result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _closure_index(authority: Row) -> dict[ClosureKey, Row]:
    sources = _source_calendars(authority)
    rows = _objects(authority.get("closed_source_dates"), "closed_source_dates")
    closures: dict[ClosureKey, Row] = {}
    gap_days: dict[GapKey, list[date]] = {}
    gap_signatures: dict[GapKey, tuple[Decimal, str]] = {}

    for row in rows:
        required = {
            "strategy_id",
            "date",
            "calendar_id",
            "closure_reason",
            "prior_observation_date",
            "next_observation_date",
            "prior_ending_capital_usd",
            "next_beginning_capital_usd",
            "closure_evidence_sha256",
            "treatment",
        }
        if set(row) != required:
            raise GovernedHwmAuthorityError("closed source date shape differs")

        strategy_id = _text(row.get("strategy_id"), "closure strategy_id")
        source = sources.get(strategy_id)
        if source is None:
            raise GovernedHwmAuthorityError("closure strategy lacks source calendar")
        if source["calendar_kind"] == "ALWAYS_ON":
            raise GovernedHwmAuthorityError("ALWAYS_ON source cannot be closed")
        if row.get("calendar_id") != source["calendar_id"]:
            raise GovernedHwmAuthorityError("closure calendar identity differs")
        if row.get("closure_reason") != CLOSED_SOURCE_REASON:
            raise GovernedHwmAuthorityError("closed source reason differs")
        if row.get("treatment") != CLOSED_SOURCE_TREATMENT:
            raise GovernedHwmAuthorityError("closed source treatment differs")

        day = _date(row.get("date"), "closure date")
        prior = _date(row.get("prior_observation_date"), "prior observation date")
        following = _date(row.get("next_observation_date"), "next observation date")
        if not prior < day < following:
            raise GovernedHwmAuthorityError("closure lacks observed date brackets")

        prior_capital = _decimal(row.get("prior_ending_capital_usd"), "prior capital")
        next_capital = _decimal(row.get("next_beginning_capital_usd"), "next capital")
        if prior_capital <= 0 or next_capital <= 0:
            raise GovernedHwmAuthorityError("closure capital must be positive")
        if prior_capital != next_capital:
            raise GovernedHwmAuthorityError("closure capital does not reconcile")
        evidence = _sha256(row.get("closure_evidence_sha256"), "closure evidence")

        key = (strategy_id, day)
        if key in closures:
            raise GovernedHwmAuthorityError("closed source date is duplicated")
        closures[key] = row

        gap_key = (strategy_id, prior, following)
        gap_days.setdefault(gap_key, []).append(day)
        signature = (prior_capital, evidence)
        accepted_signature = gap_signatures.setdefault(gap_key, signature)
        if signature != accepted_signature:
            raise GovernedHwmAuthorityError("closure gap capital/evidence varies")

    for (strategy_id, _day), row in closures.items():
        prior = _date(row.get("prior_observation_date"), "prior observation date")
        following = _date(row.get("next_observation_date"), "next observation date")
        if (strategy_id, prior) in closures or (strategy_id, following) in closures:
            raise GovernedHwmAuthorityError("closure bracket date is not observed")

    for gap_key, declared_days in gap_days.items():
        strategy_id, prior, following = gap_key
        if sorted(declared_days) != _expected_gap_days(prior, following):
            message = f"closure gap omits a date for {strategy_id}"
            raise GovernedHwmAuthorityError(message)
    return closures


def validate_governed_hwm_calendar_authority(
    authority: Row,
    *,
    hwm_policy_bytes: bytes,
    accepted_authority_sha256: str,
) -> None:
    """Validate one externally accepted mixed-calendar authority."""

    required = {
        "schema_version",
        "authority_class",
        "calendar_policy_id",
        "portfolio_calendar",
        "owner_decision_evidence",
        "hwm_policy_sha256",
        "source_calendars",
        "closed_source_dates",
        "authority_evidence_sha256",
    }
    if set(authority) != required:
        raise GovernedHwmAuthorityError("calendar authority shape differs")
    if authority.get("schema_version") != GOVERNED_HWM_CALENDAR_SCHEMA_VERSION:
        raise GovernedHwmAuthorityError("calendar authority schema version differs")
    if authority.get("authority_class") != GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS:
        raise GovernedHwmAuthorityError("calendar authority class differs")
    if authority.get("portfolio_calendar") != GOVERNED_HWM_PORTFOLIO_CALENDAR:
        raise GovernedHwmAuthorityError("portfolio HWM calendar must be ALWAYS_ON")

    expected_policy_sha256 = _sha256(
        authority.get("hwm_policy_sha256"), "HWM policy hash"
    )
    declared_hash = _sha256(
        authority.get("authority_evidence_sha256"), "authority hash"
    )
    unhashed = dict(authority)
    unhashed.pop("authority_evidence_sha256")
    if declared_hash != _canonical_sha256(unhashed):
        raise GovernedHwmAuthorityError("calendar authority identity differs")

    accepted_hash = _sha256(accepted_authority_sha256, "accepted_authority_sha256")
    if declared_hash != accepted_hash:
        raise GovernedHwmAuthorityError("calendar authority is not externally accepted")

    policy_calendar = _policy_calendar(hwm_policy_bytes, expected_policy_sha256)
    if policy_calendar["release_authority_status"] != "READY":
        raise GovernedHwmAuthorityError("HWM calendar policy is not release-ready")
    policy_id = _text(authority.get("calendar_policy_id"), "calendar_policy_id")
    accepted_ids = policy_calendar["accepted_governed_calendar_policy_ids"]
    if not isinstance(accepted_ids, list) or policy_id not in accepted_ids:
        raise GovernedHwmAuthorityError("calendar policy id is not accepted")

    _text(authority.get("owner_decision_evidence"), "owner decision evidence")
    _closure_index(authority)


def governed_closed_source_return(
    authority: Row,
    *,
    hwm_policy_bytes: bytes,
    accepted_authority_sha256: str,
    strategy_id: str,
    as_of: str,
) -> Decimal | None:
    """Return zero only for an externally accepted declared closure."""

    validate_governed_hwm_calendar_authority(
        authority,
        hwm_policy_bytes=hwm_policy_bytes,
        accepted_authority_sha256=accepted_authority_sha256,
    )
    key = (_text(strategy_id, "strategy_id"), _date(as_of, "as_of"))
    if key not in _closure_index(authority):
        return None
    return Decimal("0")


def governed_hwm_calendar_authority_bytes(
    authority: Row,
    *,
    hwm_policy_bytes: bytes,
    accepted_authority_sha256: str,
) -> bytes:
    """Return canonical bytes only after external acceptance verification."""

    validate_governed_hwm_calendar_authority(
        authority,
        hwm_policy_bytes=hwm_policy_bytes,
        accepted_authority_sha256=accepted_authority_sha256,
    )
    return _canonical_json_bytes(authority)

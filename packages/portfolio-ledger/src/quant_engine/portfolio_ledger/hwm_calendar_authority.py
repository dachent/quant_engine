"""Derive a governed mixed-calendar authority candidate from observed evidence.

The derivation consumes per-source observed interval sequences plus closing
capital observations and emits a candidate whose canonical identity matches
the external-acceptance semantics enforced by
``validate_governed_hwm_calendar_authority``.

Rules mirrored from the accepted validator semantics:

- ALWAYS_ON sources must observe every calendar day across their history;
  a single missing day fails closed (no zero fill, no holiday inference).
- DECLARED_SESSION sources may skip days, but every skipped day inside an
  observation bracket becomes an explicit closure row carrying the exact
  capital carry reconciliation (prior ending == next beginning, strictly
  positive, Decimal text).
- The emitted ``authority_evidence_sha256`` is the canonical SHA-256 of the
  document without that field, so an externally accepted hash binds the
  complete derived content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from quant_engine.portfolio_ledger.hwm_governed import (
    CLOSED_SOURCE_REASON,
    CLOSED_SOURCE_TREATMENT,
    GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS,
    GOVERNED_HWM_CALENDAR_SCHEMA_VERSION,
    GOVERNED_HWM_PORTFOLIO_CALENDAR,
    GovernedHwmAuthorityError,
)

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

Row = dict[str, Any]


def _canon(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernedHwmAuthorityError(f"derivation {field} must be non-empty text")
    return value.strip()


def _policy_sha(value: object, field: str) -> str:
    accepted = _text(value, field)
    if _SHA256_RE.fullmatch(accepted) is None:
        raise GovernedHwmAuthorityError(f"derivation {field} must be lowercase SHA-256")
    return accepted


def _date(value: object, field: str) -> date:
    accepted = _text(value, field)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise GovernedHwmAuthorityError(
            f"derivation {field} must be a canonical date"
        ) from exc
    if parsed.isoformat() != accepted:
        raise GovernedHwmAuthorityError(f"derivation {field} must be a canonical date")
    return parsed


def _decimal_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise GovernedHwmAuthorityError(f"derivation {field} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise GovernedHwmAuthorityError(
            f"derivation {field} must be decimal text"
        ) from exc
    if not parsed.is_finite():
        raise GovernedHwmAuthorityError(f"derivation {field} must be finite")
    return value.strip()


def derive_governed_calendar_authority(
    *,
    strategy_calendars: Sequence[Mapping[str, Any]],
    calendar_policy_id: str,
    owner_decision_evidence: str,
    hwm_policy_sha256: str,
) -> Row:
    """Derive one governed mixed-calendar authority candidate.

    ``strategy_calendars`` rows carry observed evidence:

    - ``strategy_id``: required perimeter member id.
    - ``calendar_kind``: ``ALWAYS_ON`` or ``DECLARED_SESSION``.
    - ``observed_dates``: canonical ISO dates with an observed source session.
    - ``closing_capital_usd_by_date``: closing-capital observations keyed by
      canonical date (required on both bracket ends of any declared gap).
    """

    policy_id = _text(calendar_policy_id, "calendar_policy_id")
    decision = _text(owner_decision_evidence, "owner_decision_evidence")
    policy_sha = _policy_sha(hwm_policy_sha256, "hwm_policy_sha256")
    if not strategy_calendars:
        raise GovernedHwmAuthorityError("derivation requires source calendars")

    source_calendars: list[Row] = []
    closed_source_dates: list[Row] = []
    seen_strategies: set[str] = set()

    for entry in strategy_calendars:
        strategy_id = _text(entry.get("strategy_id"), "strategy_id")
        if strategy_id in seen_strategies:
            raise GovernedHwmAuthorityError("derivation strategy is duplicated")
        seen_strategies.add(strategy_id)
        kind = entry.get("calendar_kind")
        if kind not in {"ALWAYS_ON", "DECLARED_SESSION"}:
            raise GovernedHwmAuthorityError("derivation calendar kind differs")
        raw_dates = entry.get("observed_dates")
        if not isinstance(raw_dates, Sequence) or isinstance(raw_dates, (str, bytes)):
            raise GovernedHwmAuthorityError(
                "derivation observed_dates must be an array"
            )
        observed = [_date(item, "observed_dates") for item in raw_dates]
        if len(observed) < 2:
            raise GovernedHwmAuthorityError(
                "derivation requires at least two observations per source"
            )
        if sorted(observed) != observed:
            raise GovernedHwmAuthorityError("derivation observed_dates are unordered")
        capital_raw = entry.get("closing_capital_usd_by_date")
        if not isinstance(capital_raw, Mapping):
            raise GovernedHwmAuthorityError("derivation capital map differs")
        capital_by_day = {
            _date(key, "closing capital key"): _decimal_text(
                value, "closing_capital_usd_by_date"
            )
            for key, value in capital_raw.items()
        }
        calendar_id = f"{strategy_id}:OBSERVED:{observed[0].isoformat()}"

        if kind == "ALWAYS_ON":
            cursor = observed[0]
            while cursor < observed[-1]:
                if cursor not in set(observed):
                    raise GovernedHwmAuthorityError(
                        f"ALWAYS_ON source {strategy_id} misses an observation"
                    )
                cursor += timedelta(days=1)
            evidence = hashlib.sha256(
                "\n".join(day.isoformat() for day in observed).encode("utf-8")
            ).hexdigest()
        else:
            evidence_parts: list[str] = []
            for prior, following in zip(observed, observed[1:]):
                gap_days = max((following - prior).days - 1, 0)
                if gap_days == 0:
                    continue
                prior_key = prior.isoformat()
                next_key = following.isoformat()
                if prior not in capital_by_day or following not in capital_by_day:
                    raise GovernedHwmAuthorityError(
                        f"declared gap lacks bracket capital for {strategy_id}"
                    )
                prior_capital = Decimal(capital_by_day[prior])
                next_capital = Decimal(capital_by_day[following])
                if prior_capital <= 0 or next_capital <= 0:
                    raise GovernedHwmAuthorityError(
                        f"closure capital must be positive for {strategy_id}"
                    )
                if prior_capital != next_capital:
                    raise GovernedHwmAuthorityError(
                        f"closure capital does not reconcile for {strategy_id}"
                    )
                carry = capital_by_day[prior]
                cursor = prior + timedelta(days=1)
                while cursor < following:
                    closed_source_dates.append(
                        {
                            "calendar_id": calendar_id,
                            "closure_evidence_sha256": hashlib.sha256(
                                "\n".join(
                                    [
                                        strategy_id,
                                        calendar_id,
                                        prior_key,
                                        next_key,
                                        carry,
                                    ]
                                ).encode("utf-8")
                            ).hexdigest(),
                            "closure_reason": CLOSED_SOURCE_REASON,
                            "date": cursor.isoformat(),
                            "next_beginning_capital_usd": carry,
                            "next_observation_date": next_key,
                            "prior_ending_capital_usd": carry,
                            "prior_observation_date": prior_key,
                            "strategy_id": strategy_id,
                            "treatment": CLOSED_SOURCE_TREATMENT,
                        }
                    )
                    cursor += timedelta(days=1)
                evidence_parts.append(f"{prior_key}..{next_key}:{carry}")
            observed_binding = "|".join(day.isoformat() for day in observed)
            gap_binding = ";".join(evidence_parts)
            evidence = hashlib.sha256(
                "\n".join([observed_binding, f"GAPS:{gap_binding}"]).encode("utf-8")
            ).hexdigest()

        source_calendars.append(
            {
                "calendar_evidence_sha256": evidence,
                "calendar_id": calendar_id,
                "calendar_kind": kind,
                "strategy_id": strategy_id,
            }
        )

    closed_source_dates.sort(key=lambda row: (row["strategy_id"], row["date"]))
    candidate: Row = {
        "schema_version": GOVERNED_HWM_CALENDAR_SCHEMA_VERSION,
        "authority_class": GOVERNED_HWM_CALENDAR_AUTHORITY_CLASS,
        "calendar_policy_id": policy_id,
        "portfolio_calendar": GOVERNED_HWM_PORTFOLIO_CALENDAR,
        "owner_decision_evidence": decision,
        "hwm_policy_sha256": policy_sha,
        "source_calendars": source_calendars,
        "closed_source_dates": closed_source_dates,
    }
    candidate["authority_evidence_sha256"] = hashlib.sha256(
        _canon(candidate)
    ).hexdigest()
    return candidate

"""Build governed HWM input generations from observed evidence.

The builder consumes only derived or independently observed facts:

- members: owner-validated funded-membership evidence (decision packet);
- capital_observations: account NAV closing-capital observations;
- return_observations: accepted account-live NET returns per interval.

It emits component periods with beginning-capital weights taken strictly
between observed brackets, binds every row to the governing policy and
calendar identities, and fails closed on terminal-only backcast, returns
without an opening anchor, non-member evidence, and duplicate membership.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

Row = dict[str, Any]


class HwmInputBuilderError(ValueError):
    """Governed HWM input derivation lacks required observed evidence."""


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
        raise HwmInputBuilderError(f"builder {field} must be non-empty text")
    return value.strip()


def _sha_field(value: object, field: str) -> str:
    accepted = _text(value, field)
    if _SHA256_RE.fullmatch(accepted) is None:
        raise HwmInputBuilderError(f"builder {field} must be lowercase SHA-256")
    return accepted


def _date(value: object, field: str) -> date:
    accepted = _text(value, field)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise HwmInputBuilderError(f"builder {field} must be a canonical date") from exc
    if parsed.isoformat() != accepted:
        raise HwmInputBuilderError(f"builder {field} must be a canonical date")
    return parsed


def _decimal_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise HwmInputBuilderError(f"builder {field} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HwmInputBuilderError(f"builder {field} must be decimal text") from exc
    if not parsed.is_finite():
        raise HwmInputBuilderError(f"builder {field} must be finite")
    return value.strip()


def _declared_intervals(
    accepted_intervals: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, set[tuple[str, str, str]]]:
    """Shape-check declared accepted intervals into a per-strategy lookup."""
    if not accepted_intervals:
        return {}
    declared: dict[str, set[tuple[str, str, str]]] = {}
    for strategy_id, entries in accepted_intervals.items():
        sid = _text(strategy_id, "accepted interval strategy_id")
        keys: set[tuple[str, str, str]] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise HwmInputBuilderError("accepted interval entry shape differs")
            prior = _date(entry.get("prior_observation_date"), "interval prior date")
            following = _date(entry.get("next_observation_date"), "interval next date")
            if prior >= following:
                raise HwmInputBuilderError("interval brackets are unordered")
            binding = _date(entry.get("binding_return_date"), "binding return date")
            if binding != following:
                raise HwmInputBuilderError(
                    "accepted interval must bind the next observed return row"
                )
            keys.add((sid, prior.isoformat(), following.isoformat()))
        declared[sid] = keys
    return declared


BUILDER_ID: Final = "portfolio-hwm-inputs-governed"


def build_governed_hwm_inputs(
    *,
    members: Sequence[Mapping[str, Any]],
    capital_observations: Sequence[Mapping[str, Any]],
    return_observations: Sequence[Mapping[str, Any]],
    calendar_authority_sha256: str,
    hwm_policy_sha256: str,
    accepted_intervals: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> Row:
    """Derive one governed input generation from observed evidence.

    ``accepted_intervals`` maps strategy_id to its declared multi-day accepted
    intervals (from the externally accepted calendar authority); multi-day
    return rows outside the declared set fail closed.
    """

    calendar_sha = _sha_field(calendar_authority_sha256, "calendar_authority_sha256")
    policy_sha = _sha_field(hwm_policy_sha256, "hwm_policy_sha256")
    declared_intervals = _declared_intervals(accepted_intervals)

    member_rows: dict[str, Row] = {}
    for entry in members:
        strategy_id = _text(entry.get("strategy_id"), "member strategy_id")
        if strategy_id in member_rows:
            raise HwmInputBuilderError(
                f"builder membership is duplicated for {strategy_id}"
            )
        effective_start = _date(entry.get("effective_start"), "effective_start")
        evidence_ref = _text(entry.get("evidence_ref"), "evidence_ref")
        member_rows[strategy_id] = {
            "strategy_id": strategy_id,
            "effective_start": effective_start.isoformat(),
            "membership_evidence_sha256": hashlib.sha256(
                "\n".join(
                    [strategy_id, effective_start.isoformat(), evidence_ref]
                ).encode()
            ).hexdigest(),
        }
    if not member_rows:
        raise HwmInputBuilderError("builder requires at least one member")

    capital_by_strategy: dict[str, list[tuple[date, str]]] = {}
    for obs in capital_observations:
        strategy_id = _text(obs.get("strategy_id"), "capital strategy_id")
        if strategy_id not in member_rows:
            raise HwmInputBuilderError(
                f"builder capital observation outside perimeter: {strategy_id}"
            )
        day = _date(obs.get("date"), "capital date")
        capital = _decimal_text(obs.get("closing_capital_usd"), "closing_capital_usd")
        if Decimal(capital) <= 0:
            raise HwmInputBuilderError(
                f"builder capital must be positive for {strategy_id}"
            )
        rows = capital_by_strategy.setdefault(strategy_id, [])
        rows.append((day, capital))
    for rows in capital_by_strategy.values():
        rows.sort(key=lambda item: item[0])

    required_basis = "ACCOUNT_LIVE_NET"
    for obs in return_observations:
        strategy_id = _text(obs.get("strategy_id"), "return strategy_id")
        if strategy_id not in member_rows:
            raise HwmInputBuilderError(
                f"builder return observation outside perimeter: {strategy_id}"
            )
        interval_start = _date(obs.get("interval_start"), "interval_start")
        interval_end = _date(obs.get("interval_end"), "interval_end")
        if (interval_end - interval_start).days != 1:
            key = (
                strategy_id,
                interval_start.isoformat(),
                interval_end.isoformat(),
            )
            if key not in declared_intervals.get(strategy_id, set()):
                raise HwmInputBuilderError(
                    f"builder requires exact one-day accepted intervals for {strategy_id}"
                )
        basis = obs.get("return_basis")
        if basis != required_basis:
            raise HwmInputBuilderError(
                f"builder requires {required_basis} returns, got {basis!r} "
                f"for {strategy_id}"
            )

    generation: Row = {
        "schema_version": 1,
        "builder": BUILDER_ID,
        "builder_version": 1,
        "authority_profile": "ACCEPTED_GOVERNED",
        "memberships": [],
        "component_periods": [],
        "policy_binding_sha256": policy_sha,
        "calendar_sha256": calendar_sha,
    }

    for strategy_id in sorted(member_rows):
        member = member_rows[strategy_id]
        effective_start = _date(member["effective_start"], "effective_start")
        anchors = capital_by_strategy.get(strategy_id, [])
        if not anchors:
            raise HwmInputBuilderError(
                f"builder requires bracket capital anchors for {strategy_id}"
            )
        if anchors[0][0] < effective_start:
            raise HwmInputBuilderError(
                f"builder capital predates funded membership for {strategy_id}"
            )

        anchor_index = 0
        for obs in return_observations:
            if _text(obs.get("strategy_id"), "return strategy_id") != strategy_id:
                continue
            interval_start = _date(obs.get("interval_start"), "interval_start")
            interval_end = _date(obs.get("interval_end"), "interval_end")
            if interval_end <= interval_start:
                raise HwmInputBuilderError(
                    f"builder return interval is empty for {strategy_id}"
                )
            if (interval_end - interval_start).days != 1:
                key = (
                    strategy_id,
                    interval_start.isoformat(),
                    interval_end.isoformat(),
                )
                if key not in declared_intervals.get(strategy_id, set()):
                    raise HwmInputBuilderError(
                        f"builder requires exact one-day accepted intervals for "
                        f"{strategy_id}; multi-day row "
                        f"{interval_start.isoformat()}..{interval_end.isoformat()} "
                        "is not a declared accepted interval"
                    )
            while (
                anchor_index + 1 < len(anchors)
                and anchors[anchor_index + 1][0] <= interval_start
            ):
                anchor_index += 1
            if anchors[anchor_index][0] > interval_start:
                raise HwmInputBuilderError(
                    f"builder return lacks an opening bracket anchor for "
                    f"{strategy_id} before {interval_start.isoformat()} "
                    "(terminal-only backcast is rejected)"
                )
            if anchor_index + 1 >= len(anchors):
                raise HwmInputBuilderError(
                    f"builder requires an independently observed closing "
                    f"bracket anchor after {interval_end.isoformat()} for "
                    f"{strategy_id}"
                )
            net_return = _decimal_text(obs.get("net_return"), "net_return")
            basis = _text(obs.get("return_basis"), "return_basis")
            source_sha = _sha_field(
                obs.get("source_artifact_sha256"), "return source artifact"
            )
            generation["component_periods"].append(
                {
                    "strategy_id": strategy_id,
                    "interval_start": interval_start.isoformat(),
                    "interval_end": interval_end.isoformat(),
                    "beginning_capital_usd": anchors[anchor_index][1],
                    "net_return": net_return,
                    "return_basis": basis,
                    "return_evidence_sha256": source_sha,
                    "membership_evidence_sha256": member["membership_evidence_sha256"],
                    "source_generation_sha256": "",
                    "calendar_sha256": calendar_sha,
                    "policy_binding_sha256": policy_sha,
                }
            )
        generation["memberships"].append(member)

    evidence_pool: set[str] = set()
    for member in generation["memberships"]:
        evidence_pool.add(str(member["membership_evidence_sha256"]))
    for row in generation["component_periods"]:
        evidence_pool.update(
            {
                str(row["return_evidence_sha256"]),
                str(row["membership_evidence_sha256"]),
            }
        )
    generation["evidence_sha256s"] = sorted(evidence_pool)

    generation["generation_evidence_sha256"] = hashlib.sha256(
        _canon(generation)
    ).hexdigest()
    return generation

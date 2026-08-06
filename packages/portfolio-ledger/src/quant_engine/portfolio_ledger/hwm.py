"""Governed high-water-mark coverage shared by reports and applications."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping, Sequence


HWM_STATUS_CODE_VERSION: Final = 2
HWM_STATUS_SEMANTICS: Final = "EXPLICIT_GOVERNED_HWM_PERIMETER"


class HwmCoverageError(ValueError):
    """The governed HWM perimeter or its evidence is invalid."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HwmCoverageError(f"{field_name} must be non-empty text")
    return value.strip()


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HwmCoverageError(f"{field_name} must be decimal") from exc
    if not accepted.is_finite():
        raise HwmCoverageError(f"{field_name} must be finite")
    return accepted


def governed_hwm_coverage(
    strategies: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    freshness_health: Sequence[Mapping[str, Any]] | None = None,
    *,
    require_freshness: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Qualify the material strategy perimeter for an automated systematic HWM."""

    policy_id = _text(policy.get("policy_id", "PORT-HWM-2"), "policy.policy_id")
    excluded = set(policy.get("excluded_strategy_ids", []))
    accepted_evidence = set(policy.get("accepted_daily_nav_evidence", []))
    accepted_provider_live = set(policy.get("accepted_provider_live_strategy_ids", []))
    if not accepted_evidence:
        raise HwmCoverageError("accepted daily NAV evidence is empty")

    accepted: list[dict[str, Any]] = []
    covered = Decimal(0)
    governed_perimeter = Decimal(0)
    excluded_value = Decimal(0)
    excluded_names: list[str] = []
    excluded_details: list[dict[str, Any]] = []
    uncovered: list[str] = []
    uncovered_value = Decimal(0)
    uncovered_details: list[dict[str, Any]] = []
    exclusion_by_strategy: dict[str, Mapping[str, Any]] = {}
    material_perimeter = policy.get("material_perimeter")
    if isinstance(material_perimeter, Mapping):
        exclusions = material_perimeter.get("exclusions", [])
        if isinstance(exclusions, Sequence) and not isinstance(exclusions, str | bytes):
            for item in exclusions:
                if isinstance(item, Mapping) and item.get("strategy_id") in excluded:
                    exclusion_by_strategy[str(item["strategy_id"])] = item
    freshness_by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for freshness in freshness_health or []:
        if (
            freshness.get("fact_type") in {"nav", "strategy-returns"}
            and freshness.get("subject_type", "STRATEGY") == "STRATEGY"
        ):
            freshness_by_strategy[str(freshness.get("subject_id"))].append(freshness)

    seen: set[str] = set()
    for original in strategies:
        row = dict(original)
        strategy_id = _text(row.get("strategy_id"), "strategy.strategy_id")
        strategy_name = _text(row.get("strategy_name"), "strategy.strategy_name")
        if strategy_id in seen:
            raise HwmCoverageError("strategy identity is duplicated")
        seen.add(strategy_id)
        value = _decimal(row.get("current_value_usd"), "strategy.current_value")
        if value < 0:
            raise HwmCoverageError("strategy current value cannot be negative")
        if strategy_id in excluded:
            exclusion = exclusion_by_strategy.get(strategy_id, {})
            classification = str(exclusion.get("classification", "EXCLUDED_DE_MINIMIS"))
            effective_date = exclusion.get("effective_date")
            reason = str(
                exclusion.get(
                    "reason", "Policy exclusion retained without a supplied reason."
                )
            )
            row.update(
                {
                    "hwm_scope": "EXCLUDED_DE_MINIMIS",
                    "hwm_source": policy_id,
                    "hwm_exclusion_classification": classification,
                    "hwm_exclusion_effective_date": effective_date,
                    "hwm_exclusion_reason": reason,
                    "nav_status": "EXCLUDED_DE_MINIMIS",
                }
            )
            excluded_value += value
            excluded_names.append(strategy_name)
            excluded_details.append(
                {
                    "classification": classification,
                    "current_value_usd": format(value, "f"),
                    "effective_date": effective_date,
                    "reason": reason,
                    "strategy_id": strategy_id,
                    "strategy_name": strategy_name,
                }
            )
            accepted.append(row)
            continue

        governed_perimeter += value
        row["hwm_scope"] = "INCLUDED"
        row["hwm_source"] = "PORTFOLIO_REPORT"
        evidence_layer = row.get("nav_evidence_layer")
        evidence_blocked = evidence_layer not in accepted_evidence or (
            evidence_layer == "PROVIDER_LIVE"
            and strategy_id not in accepted_provider_live
        )
        row["hwm_evidence_status"] = (
            "ACCEPTED" if not evidence_blocked else "UNACCEPTED"
        )
        eligible_lanes = (
            {"PROVIDER"}
            if evidence_layer == "PROVIDER_LIVE"
            else {"ACCOUNT", "LEGACY"}
            if evidence_layer == "ACCOUNT_LIVE"
            else {"ACCOUNT", "LEGACY", "PROVIDER"}
        )
        strategy_freshness = [
            item
            for item in freshness_by_strategy.get(strategy_id, [])
            if str(item.get("lane", "LEGACY")) in eligible_lanes
        ]
        state_rank = {
            "UNAVAILABLE": 5,
            "STALE": 4,
            "LATE": 3,
            "FRESH": 2,
            "FINITE_COMPLETE": 1,
        }
        selected_freshness = max(
            strategy_freshness,
            key=lambda item: (
                state_rank.get(str(item.get("state") or item.get("status")), 6),
                str(item.get("lane", "LEGACY")),
            ),
            default=None,
        )
        freshness_blocked = any(
            item.get("blocks_current_consumers") is True
            or str(item.get("state") or item.get("status")) in {"STALE", "UNAVAILABLE"}
            for item in strategy_freshness
        )
        freshness_missing = require_freshness and not strategy_freshness
        freshness_state = (
            str(selected_freshness.get("state") or selected_freshness.get("status"))
            if selected_freshness is not None
            else None
        )
        evidence_observed_end = row.get("nav_observed_end")
        freshness_observed_end = (
            selected_freshness.get("last_accepted_date")
            if selected_freshness is not None
            else None
        )
        freshness_boundary_mismatch = (
            evidence_observed_end is not None
            and freshness_observed_end is not None
            and str(evidence_observed_end) != str(freshness_observed_end)
        )
        freshness_blocked = freshness_blocked or freshness_boundary_mismatch
        row["hwm_freshness_status"] = (
            "UNAVAILABLE"
            if freshness_missing
            else "BLOCKED"
            if freshness_blocked
            else "ACCEPTED"
        )
        row["hwm_freshness_state"] = freshness_state
        row["hwm_freshness_observed_end"] = freshness_observed_end
        row["hwm_freshness_boundary_matches_evidence"] = (
            None
            if evidence_observed_end is None or freshness_observed_end is None
            else not freshness_boundary_mismatch
        )
        if (
            row.get("nav_status") == "AVAILABLE"
            and not evidence_blocked
            and not freshness_blocked
            and not freshness_missing
        ):
            covered += value
        else:
            uncovered.append(strategy_name)
            uncovered_value += value
            if row.get("nav_status") != "AVAILABLE":
                qualification_gap = "MISSING_DAILY_NAV"
            elif evidence_blocked:
                qualification_gap = "UNACCEPTED_EVIDENCE"
            elif freshness_missing:
                qualification_gap = "FRESHNESS_UNAVAILABLE"
            elif freshness_boundary_mismatch:
                qualification_gap = "FRESHNESS_EVIDENCE_BOUNDARY_MISMATCH"
            elif freshness_blocked:
                qualification_gap = f"FRESHNESS_{freshness_state or 'BLOCKED'}"
            else:
                qualification_gap = "UNQUALIFIED"
            uncovered_details.append(
                {
                    "current_value_usd": format(value, "f"),
                    "disposition": "RETAINED_UNCOVERED_PENDING_FRESH_DAILY_NAV",
                    "freshness_lane": (
                        selected_freshness.get("lane")
                        if selected_freshness is not None
                        else None
                    ),
                    "freshness_reason": (
                        selected_freshness.get("reason")
                        if selected_freshness is not None
                        else "No accepted freshness assessment exists for the admitted evidence lane."
                    ),
                    "freshness_state": freshness_state,
                    "hwm_evidence_observed_end": evidence_observed_end,
                    "last_accepted_date": (
                        selected_freshness.get("last_accepted_date")
                        if selected_freshness is not None
                        else None
                    ),
                    "next_expected_date": (
                        selected_freshness.get("next_expected_date")
                        if selected_freshness is not None
                        else None
                    ),
                    "policy_id": policy_id,
                    "qualification_gap": qualification_gap,
                    "strategy_id": strategy_id,
                    "strategy_name": strategy_name,
                    "systematic_weight": row.get("systematic_weight"),
                }
            )
        accepted.append(row)

    unknown_exclusions = excluded - seen
    if unknown_exclusions:
        raise HwmCoverageError("HWM policy excludes an unknown strategy")
    if governed_perimeter <= 0:
        raise HwmCoverageError("governed HWM perimeter is empty")
    ratio = covered / governed_perimeter
    if covered + uncovered_value != governed_perimeter:
        raise HwmCoverageError("governed HWM coverage arithmetic differs")
    status = (
        "AVAILABLE_GOVERNED_HWM_PERIMETER"
        if not uncovered
        else "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV"
    )
    return accepted, {
        "covered_value_usd": format(covered, "f"),
        "excluded_value_usd": format(excluded_value, "f"),
        "excluded_strategies": sorted(excluded_names),
        "excluded_details": sorted(
            excluded_details, key=lambda item: str(item["strategy_name"])
        ),
        "governed_perimeter_value_usd": format(governed_perimeter, "f"),
        "nav_coverage": format(ratio, "f"),
        "policy_id": policy_id,
        "status": status,
        "status_code_version": HWM_STATUS_CODE_VERSION,
        "status_semantics": HWM_STATUS_SEMANTICS,
        "uncovered_details": sorted(
            uncovered_details, key=lambda item: str(item["strategy_name"])
        ),
        "uncovered_strategies": sorted(uncovered),
        "uncovered_value_usd": format(uncovered_value, "f"),
    }

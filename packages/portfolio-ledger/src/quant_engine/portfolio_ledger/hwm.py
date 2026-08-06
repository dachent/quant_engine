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
    uncovered: list[str] = []
    uncovered_details: list[dict[str, Any]] = []
    freshness_by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for freshness in freshness_health or []:
        if (
            freshness.get("fact_type") in {"nav", "strategy-returns"}
            and freshness.get("subject_type", "STRATEGY") == "STRATEGY"
            and freshness.get("lane", "LEGACY") in {"ACCOUNT", "LEGACY"}
            and freshness.get("required_for_current", True) is True
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
            row.update(
                {
                    "hwm_scope": "EXCLUDED_DE_MINIMIS",
                    "hwm_source": "PORT-HWM-2",
                    "nav_status": "EXCLUDED_DE_MINIMIS",
                }
            )
            excluded_value += value
            excluded_names.append(strategy_name)
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
        strategy_freshness = freshness_by_strategy.get(strategy_id, [])
        freshness_blocked = any(
            item.get("blocks_current_consumers") is True for item in strategy_freshness
        )
        freshness_missing = require_freshness and not strategy_freshness
        freshness_state = next(
            (
                str(item.get("state") or item.get("status"))
                for item in strategy_freshness
                if isinstance(item.get("state") or item.get("status"), str)
                and (item.get("state") or item.get("status"))
            ),
            None,
        )
        row["hwm_freshness_status"] = (
            "UNAVAILABLE"
            if freshness_missing
            else "BLOCKED"
            if freshness_blocked
            else "ACCEPTED"
        )
        row["hwm_freshness_state"] = freshness_state
        if (
            row.get("nav_status") == "AVAILABLE"
            and not evidence_blocked
            and not freshness_blocked
            and not freshness_missing
        ):
            covered += value
        else:
            uncovered.append(strategy_name)
            if row.get("nav_status") != "AVAILABLE":
                qualification_gap = "MISSING_DAILY_NAV"
            elif evidence_blocked:
                qualification_gap = "UNACCEPTED_EVIDENCE"
            elif freshness_missing:
                qualification_gap = "FRESHNESS_UNAVAILABLE"
            elif freshness_blocked:
                qualification_gap = f"FRESHNESS_{freshness_state or 'BLOCKED'}"
            else:
                qualification_gap = "UNQUALIFIED"
            uncovered_details.append(
                {
                    "current_value_usd": format(value, "f"),
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
    status = (
        "AVAILABLE_GOVERNED_HWM_PERIMETER"
        if not uncovered
        else "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV"
    )
    return accepted, {
        "covered_value_usd": format(covered, "f"),
        "excluded_value_usd": format(excluded_value, "f"),
        "excluded_strategies": sorted(excluded_names),
        "governed_perimeter_value_usd": format(governed_perimeter, "f"),
        "nav_coverage": format(ratio, "f"),
        "status": status,
        "status_code_version": HWM_STATUS_CODE_VERSION,
        "status_semantics": HWM_STATUS_SEMANTICS,
        "uncovered_details": sorted(
            uncovered_details, key=lambda item: str(item["strategy_name"])
        ),
        "uncovered_strategies": sorted(uncovered),
    }

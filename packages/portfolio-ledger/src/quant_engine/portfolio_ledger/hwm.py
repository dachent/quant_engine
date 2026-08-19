"""Governed high-water-mark coverage shared by reports and applications."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import re
from typing import Any, Final, Mapping, Sequence


HWM_STATUS_CODE_VERSION: Final = 2
HWM_STATUS_SEMANTICS: Final = "EXPLICIT_GOVERNED_HWM_PERIMETER"
AS_HELD_HWM_POLICY_ID: Final = "PORT-HWM-4"
AS_HELD_HWM_RETURN_BASIS: Final = "LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_PORTABLE_INPUT_AUTHORITY: Final = "PORTABLE_SYNTHETIC"
_PORTABLE_PROOF_STATUS: Final = "PORTABLE_SYNTHETIC_PROOF"
_FLOW_EVENT_TREATMENTS: Final = {
    "OWNER_CONTRIBUTION": "EXTERNAL_FLOW",
    "OWNER_WITHDRAWAL": "EXTERNAL_FLOW",
    "IN_KIND_FUNDING": "EXTERNAL_FLOW",
    "OPENING_BASIS": "EXTERNAL_FLOW",
    "OWNER_BOUNDARY_PREDECESSOR_TRANSITION": "EXTERNAL_FLOW",
    "INTERNAL_TRANSFER": "INTERNAL_TRANSFER",
    "PREDECESSOR_TRANSITION": "INTERNAL_TRANSFER",
    "MARGIN_BORROW": "MARGIN_PRINCIPAL",
    "MARGIN_REPAYMENT": "MARGIN_PRINCIPAL",
    "FINANCING_COST": "FINANCING_COST",
    "DIVIDEND": "INVESTMENT_RESULT",
    "INTEREST": "INVESTMENT_RESULT",
    "REINVESTMENT": "INVESTMENT_RESULT",
    "FEE": "INVESTMENT_RESULT",
    "TAX": "INVESTMENT_RESULT",
    "EXPENSE": "INVESTMENT_RESULT",
    "REBATE": "INVESTMENT_RESULT",
}


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


def _canonical_date(value: object, field_name: str) -> date:
    text = _text(value, field_name)
    try:
        accepted = date.fromisoformat(text)
    except ValueError as exc:
        raise HwmCoverageError(f"{field_name} must be a canonical ISO date") from exc
    if text != accepted.isoformat():
        raise HwmCoverageError(f"{field_name} must be a canonical ISO date")
    return accepted


def _canonical_utc_timestamp(value: object, field_name: str) -> datetime:
    text = _text(value, field_name)
    if _UTC_TIMESTAMP_RE.fullmatch(text) is None:
        raise HwmCoverageError(f"{field_name} must be a canonical UTC timestamp")
    try:
        accepted = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise HwmCoverageError(
            f"{field_name} must be a canonical UTC timestamp"
        ) from exc
    if accepted.tzinfo != timezone.utc:
        raise HwmCoverageError(f"{field_name} must be a canonical UTC timestamp")
    canonical = accepted.strftime("%Y-%m-%dT%H:%M:%S")
    if accepted.microsecond:
        canonical += f".{accepted.microsecond:06d}".rstrip("0")
    canonical += "Z"
    if text != canonical:
        raise HwmCoverageError(f"{field_name} must be a canonical UTC timestamp")
    return accepted


def _sha256(value: object, field_name: str) -> str:
    accepted = _text(value, field_name)
    if _SHA256_RE.fullmatch(accepted) is None:
        raise HwmCoverageError(f"{field_name} must be a lowercase SHA-256")
    return accepted


def _strict_keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise HwmCoverageError(
            f"{field_name} keys differ; missing={missing}, extra={extra}"
        )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(value, newline=False)).hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any], *, newline: bool = True) -> bytes:
    suffix = "\n" if newline else ""
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + suffix
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HwmCoverageError(f"{field_name} must be an object")
    return value


def validate_as_held_hwm_policy(policy: Mapping[str, Any]) -> None:
    """Validate the executable semantic invariants of ``PORT-HWM-4``."""

    _strict_keys(
        policy,
        {
            "schema_version",
            "policy_id",
            "product_authority",
            "performance_authority",
            "owner_decision_receipts",
            "material_perimeter_authority",
            "calculation",
            "as_of_policy",
            "membership_policy",
            "capital_policy",
            "calendar_policy",
            "input_authority_policy",
            "absence_behavior",
        },
        "policy",
    )
    if policy["schema_version"] != 5 or policy["policy_id"] != AS_HELD_HWM_POLICY_ID:
        raise HwmCoverageError("as-held HWM policy identity is invalid")

    product = _mapping(policy["product_authority"], "policy.product_authority")
    _strict_keys(
        product,
        {"path", "raw_sha256", "semantic_digest"},
        "policy.product_authority",
    )
    if product["path"] != "contracts/portfolio-financial-mvp-source-v1.json":
        raise HwmCoverageError("HWM product authority path is invalid")
    _sha256(product["raw_sha256"], "policy.product_authority.raw_sha256")
    _sha256(product["semantic_digest"], "policy.product_authority.semantic_digest")

    performance = _mapping(
        policy["performance_authority"], "policy.performance_authority"
    )
    _strict_keys(
        performance,
        {"path", "sha256", "policy_version"},
        "policy.performance_authority",
    )
    if (
        performance["path"] != "contracts/portfolio-performance-policy-v1.json"
        or performance["policy_version"] != "PORT-PERF-1"
    ):
        raise HwmCoverageError("HWM performance authority is invalid")
    _sha256(performance["sha256"], "policy.performance_authority.sha256")

    decisions = _mapping(
        policy["owner_decision_receipts"], "policy.owner_decision_receipts"
    )
    decision_ids = {
        "CORRECTION_REVERSAL",
        "FX_BASE_CURRENCY",
        "MARGIN_AND_LEVERAGE",
        "NEGATIVE_NET_CONTRIBUTIONS",
        "PREDECESSOR_TRANSITION",
    }
    _strict_keys(decisions, decision_ids, "policy.owner_decision_receipts")
    for decision_id in decision_ids:
        _sha256(
            decisions[decision_id],
            f"policy.owner_decision_receipts.{decision_id}",
        )

    perimeter = _mapping(
        policy["material_perimeter_authority"],
        "policy.material_perimeter_authority",
    )
    _strict_keys(
        perimeter,
        {"source", "legacy_policy_path", "legacy_policy_sha256"},
        "policy.material_perimeter_authority",
    )
    if (
        perimeter["source"]
        != ("PRODUCT_AUTHORITY_HWM_REQUIREMENT_AND_ACCEPTED_HWM_EXCLUSIONS")
        or perimeter["legacy_policy_path"] != "contracts/portfolio-hwm-policy-v3.json"
    ):
        raise HwmCoverageError("HWM material perimeter authority is invalid")
    _sha256(
        perimeter["legacy_policy_sha256"],
        "policy.material_perimeter_authority.legacy_policy_sha256",
    )

    exact_objects = {
        "calculation": {
            "method": "GEOMETRIC_BEGINNING_CAPITAL_WEIGHTED_AS_HELD",
            "index_base": "100",
            "return_basis": AS_HELD_HWM_RETURN_BASIS,
            "weight_basis": "BEGINNING_NET_NAV",
            "hwm_rule": "RUNNING_MAXIMUM_OF_COMPOSITE_INDEX",
            "drawdown_sign": "SIGNED_NON_POSITIVE",
            "governor_loss_sign": "POSITIVE_MAGNITUDE",
            "numeric_type": "DECIMAL_CONTEXT_INDEPENDENT_HALF_EVEN",
        },
        "membership_policy": {
            "basis": "EFFECTIVE_FUNDED_MEMBERSHIP",
            "entry_exit": "ACCEPTED_INTERVAL_BOUNDARIES_ONLY",
            "missing_member": "FAIL_NO_SURVIVOR_RENORMALIZATION",
        },
        "capital_policy": {
            "first_period": "INDEPENDENT_OBSERVED_BEGINNING_NET_NAV",
            "later_periods": (
                "INDEPENDENT_OBSERVED_OR_EXACT_PRIOR_ENDING_NET_NAV_ROLLFORWARD"
            ),
            "terminal_anchor": "NO_BACKCAST",
        },
    }
    for field_name, expected in exact_objects.items():
        if _mapping(policy[field_name], f"policy.{field_name}") != expected:
            raise HwmCoverageError(f"HWM {field_name} differs")

    calendar_policy = _mapping(policy["calendar_policy"], "policy.calendar_policy")
    _strict_keys(
        calendar_policy,
        {
            "authority",
            "interval_rule",
            "missing_interval",
            "release_authority_status",
            "accepted_governed_calendar_policy_ids",
            "portable_synthetic_policy_id",
        },
        "policy.calendar_policy",
    )
    if {
        key: calendar_policy[key]
        for key in {"authority", "interval_rule", "missing_interval"}
    } != {
        "authority": "HASH_BOUND_ACCEPTED_INTERVAL_SEQUENCE",
        "interval_rule": "EXACT_CONTIGUOUS_ONE_DAY_ACCEPTED_INTERVALS",
        "missing_interval": "FAIL_NO_ZERO_FILL_OR_HOLIDAY_INFERENCE",
    }:
        raise HwmCoverageError("HWM calendar policy differs")
    release_calendar_status = calendar_policy["release_authority_status"]
    accepted_calendar_ids = calendar_policy["accepted_governed_calendar_policy_ids"]
    if release_calendar_status not in {"REQUIRED_NOT_READY", "READY"}:
        raise HwmCoverageError("HWM release calendar authority status differs")
    if (
        not isinstance(accepted_calendar_ids, list)
        or len(accepted_calendar_ids) != len(set(accepted_calendar_ids))
        or any(
            not isinstance(value, str) or not value for value in accepted_calendar_ids
        )
        or (release_calendar_status == "REQUIRED_NOT_READY" and accepted_calendar_ids)
        or (release_calendar_status == "READY" and not accepted_calendar_ids)
        or calendar_policy["portable_synthetic_policy_id"]
        != "SYNTHETIC_DAILY_COMMON_CALENDAR"
    ):
        raise HwmCoverageError("HWM accepted calendar authorities differ")

    input_authority = _mapping(
        policy["input_authority_policy"], "policy.input_authority_policy"
    )
    if input_authority != {
        "portable_synthetic": "DEVELOPMENT_PROOF_ONLY_NOT_FINANCIAL_ACCEPTANCE",
        "available_status": "REQUIRES_EXISTING_PIPELINE_GOVERNED_RECEIPT",
        "workstation_and_release": "REJECT_PORTABLE_SYNTHETIC_INPUTS",
    }:
        raise HwmCoverageError("HWM input authority policy differs")

    as_of = _mapping(policy["as_of_policy"], "policy.as_of_policy")
    _strict_keys(
        as_of,
        {
            "boundary",
            "development",
            "workstation_qc",
            "release_qc",
            "required_evidence",
        },
        "policy.as_of_policy",
    )
    if {
        "boundary": as_of["boundary"],
        "development": as_of["development"],
        "workstation_qc": as_of["workstation_qc"],
        "release_qc": as_of["release_qc"],
    } != {
        "boundary": "LATEST_COMMON_COMPLETE_ACCEPTED_BOUNDARY",
        "development": "WARN_AND_CALCULATE",
        "workstation_qc": "WARN_AND_CALCULATE",
        "release_qc": "WARN_AND_CALCULATE",
    }:
        raise HwmCoverageError("HWM as-of policy differs")
    evidence = as_of["required_evidence"]
    if not isinstance(evidence, Sequence) or isinstance(evidence, str | bytes):
        raise HwmCoverageError("HWM as-of required evidence must be an array")
    if len(evidence) != 4 or set(evidence) != {
        "EVALUATED_ON",
        "COMPLETE_THROUGH",
        "CALCULATION_AGE_DAYS",
        "LIMITING_INPUT_IDENTITIES",
    }:
        raise HwmCoverageError("HWM as-of required evidence differs")
    if policy["absence_behavior"] != (
        "REQUIRED_INFRASTRUCTURE_OR_EVIDENCE_ABSENCE_FAILS_NUMERICAL_ACCEPTANCE"
    ):
        raise HwmCoverageError("HWM absence behavior differs")


def _json_mapping_from_bytes(value: bytes, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, bytes) or not value:
        raise HwmCoverageError(f"{field_name} must be non-empty bytes")
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HwmCoverageError(f"{field_name} must contain JSON") from exc
    return _mapping(parsed, field_name)


def _validate_policy_authority_bytes(
    policy: Mapping[str, Any],
    *,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
) -> None:
    product = _mapping(policy["product_authority"], "policy.product_authority")
    if hashlib.sha256(product_contract_bytes).hexdigest() != product["raw_sha256"]:
        raise HwmCoverageError("HWM product authority bytes differ")
    product_contract = _json_mapping_from_bytes(
        product_contract_bytes, "product_contract_bytes"
    )
    if (
        hashlib.sha256(_canonical_json_bytes(product_contract)).hexdigest()
        != product["semantic_digest"]
    ):
        raise HwmCoverageError("HWM product semantic authority differs")
    outcomes = product_contract.get("financial_outcomes")
    if not isinstance(outcomes, list):
        raise HwmCoverageError("HWM product financial outcomes differ")
    invested = [row for row in outcomes if row.get("outcome_id") == "INVESTED_CAPITAL"]
    if len(invested) != 1:
        raise HwmCoverageError("HWM invested-capital authority differs")
    calculation = _mapping(
        _mapping(invested[0], "invested outcome").get("calculation_contract"),
        "invested calculation contract",
    )
    receipts = _mapping(
        calculation.get("owner_decision_receipts"), "owner decision receipts"
    )
    expected_receipts = _mapping(
        policy["owner_decision_receipts"], "policy.owner_decision_receipts"
    )
    if set(receipts) != set(expected_receipts):
        raise HwmCoverageError("HWM owner decision receipt set differs")
    for decision_id, expected_sha256 in expected_receipts.items():
        receipt = _mapping(receipts[decision_id], f"owner receipt {decision_id}")
        if receipt.get("payload_sha256") != expected_sha256:
            raise HwmCoverageError(f"HWM owner decision receipt differs: {decision_id}")

    performance = _mapping(
        policy["performance_authority"], "policy.performance_authority"
    )
    if hashlib.sha256(performance_policy_bytes).hexdigest() != performance["sha256"]:
        raise HwmCoverageError("HWM performance authority bytes differ")
    performance_policy = _json_mapping_from_bytes(
        performance_policy_bytes, "performance_policy_bytes"
    )
    if performance_policy.get("policy_version") != "PORT-PERF-1":
        raise HwmCoverageError("HWM performance policy content differs")

    perimeter = _mapping(
        policy["material_perimeter_authority"],
        "policy.material_perimeter_authority",
    )
    if (
        hashlib.sha256(legacy_policy_bytes).hexdigest()
        != perimeter["legacy_policy_sha256"]
    ):
        raise HwmCoverageError("HWM legacy perimeter authority bytes differ")
    legacy_policy = _json_mapping_from_bytes(legacy_policy_bytes, "legacy_policy_bytes")
    if legacy_policy.get("policy_id") != "PORT-HWM-3":
        raise HwmCoverageError("HWM legacy perimeter policy content differs")


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
            else "BOUNDARY_MISMATCH"
            if freshness_boundary_mismatch
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


def compute_full_systematic_hwm_index(
    daily_returns: Sequence[Decimal],
    *,
    full_systematic_coverage: bool,
) -> dict[str, Any]:
    """Compute the full-systematic high-water-mark index and drawdown.

    The full-systematic HWM index is a geometrically linked TWR index that
    starts at ``1.0`` on the first covered day.  For each daily return *r_t*:

        index_t = index_{t-1} * (1 + r_t)
        HWM_t = max(index_0 … index_t)
        drawdown_t = 1 - index_t / HWM_t

    Returns a ``dict`` with the index series, running HWM, current drawdown,
    maximum drawdown (exact ``Decimal``), and a ``status`` field.  When
    ``full_systematic_coverage`` is ``False`` or ``daily_returns`` is empty,
    ``status`` is ``"UNAVAILABLE"`` and no numeric fields are produced.

    Composite construction rule for downstream wiring
    -------------------------------------------------
    Period returns are beginning-capital weighted across all included eligible
    members and then geometrically linked.  A missing member, daily return,
    or weight makes the period ``UNAVAILABLE`` — the composite is never
    recalculated over survivors.
    """

    if not full_systematic_coverage:
        return {"status": "UNAVAILABLE"}

    if not daily_returns:
        return {"status": "UNAVAILABLE"}

    with localcontext() as ctx:
        ctx.prec = 34
        # Pin the rounding mode too: a caller's thread-local rounding mode
        # otherwise leaks through and changes reported drawdowns.
        ctx.rounding = ROUND_HALF_EVEN
        index = Decimal("1.0")
        hwm = index
        max_drawdown = Decimal("0")
        index_series: list[Decimal] = [index]

        for r in daily_returns:
            index = index * (Decimal("1") + r)
            index_series.append(index)
            if index > hwm:
                hwm = index
            drawdown = Decimal("1") - index / hwm
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        current_drawdown = Decimal("1") - index_series[-1] / hwm

        return {
            "status": "AVAILABLE",
            "index_series": index_series,
            "hwm": hwm,
            "current_drawdown": current_drawdown,
            "maximum_drawdown": max_drawdown,
        }


def compute_as_held_composite_hwm(
    accepted_intervals: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    component_periods: Sequence[Mapping[str, Any]],
    *,
    policy_bytes: bytes,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
    calendar_bytes: bytes,
    input_generation_bytes: bytes,
    evaluated_on: str,
    complete_through: str,
    maximum_age_days: int,
    assurance_profile: str,
) -> dict[str, Any]:
    """Calculate PORT-HWM-4 under one explicit decimal arithmetic context."""

    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        return _compute_as_held_composite_hwm(
            accepted_intervals,
            memberships,
            component_periods,
            policy_bytes=policy_bytes,
            product_contract_bytes=product_contract_bytes,
            performance_policy_bytes=performance_policy_bytes,
            legacy_policy_bytes=legacy_policy_bytes,
            calendar_bytes=calendar_bytes,
            input_generation_bytes=input_generation_bytes,
            evaluated_on=evaluated_on,
            complete_through=complete_through,
            maximum_age_days=maximum_age_days,
            assurance_profile=assurance_profile,
        )


def _compute_as_held_composite_hwm(
    accepted_intervals: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    component_periods: Sequence[Mapping[str, Any]],
    *,
    policy_bytes: bytes,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
    calendar_bytes: bytes,
    input_generation_bytes: bytes,
    evaluated_on: str,
    complete_through: str,
    maximum_age_days: int,
    assurance_profile: str,
) -> dict[str, Any]:
    """Calculate the governed as-held composite HWM under ``PORT-HWM-4``.

    The caller supplies the accepted common interval sequence.  This kernel
    therefore neither invents calendar observations nor fills missing member
    periods.  Every membership effective in an interval must have exactly one
    period record, and no record may exist outside effective membership.

    Component returns are independently evidenced, flow-neutral net-NAV
    returns after financing cost.  Beginning net NAV supplies each interval's
    as-held weights.  The output uses a base-100 index and signed, non-positive
    drawdowns; ``governor_drawdown_loss`` exposes the corresponding positive
    magnitude used by the 15 percent governor.
    """

    if not isinstance(maximum_age_days, int) or isinstance(maximum_age_days, bool):
        raise HwmCoverageError("maximum_age_days must be an integer")
    if maximum_age_days < 0:
        raise HwmCoverageError("maximum_age_days cannot be negative")
    if assurance_profile not in {"DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC"}:
        raise HwmCoverageError("assurance_profile is invalid")

    policy = _json_mapping_from_bytes(policy_bytes, "policy_bytes")
    validate_as_held_hwm_policy(policy)
    _validate_policy_authority_bytes(
        policy,
        product_contract_bytes=product_contract_bytes,
        performance_policy_bytes=performance_policy_bytes,
        legacy_policy_bytes=legacy_policy_bytes,
    )
    accepted_policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    policy_semantic_digest = _canonical_sha256(policy)
    accepted_calendar_sha256 = hashlib.sha256(calendar_bytes).hexdigest()
    accepted_generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    evaluation_date = _canonical_date(evaluated_on, "evaluated_on")
    complete_date = _canonical_date(complete_through, "complete_through")
    if complete_date > evaluation_date:
        raise HwmCoverageError("complete_through cannot be after evaluated_on")
    age_days = (evaluation_date - complete_date).days

    interval_keys = {
        "interval_id",
        "interval_start",
        "interval_end",
        "interval_evidence_sha256",
    }
    normalized_intervals: list[dict[str, str]] = []
    interval_by_id: dict[str, dict[str, Any]] = {}
    previous_end: date | None = None
    for index, original in enumerate(accepted_intervals):
        if not isinstance(original, Mapping):
            raise HwmCoverageError("accepted interval must be an object")
        _strict_keys(original, interval_keys, f"accepted_intervals[{index}]")
        interval_id = _text(original["interval_id"], "interval.interval_id")
        if interval_id in interval_by_id:
            raise HwmCoverageError("accepted interval identity is duplicated")
        interval_start = _canonical_date(
            original["interval_start"], "interval.interval_start"
        )
        interval_end = _canonical_date(
            original["interval_end"], "interval.interval_end"
        )
        if (interval_end - interval_start).days != 1:
            raise HwmCoverageError("accepted HWM interval must be exactly one day")
        if previous_end is not None and interval_start != previous_end:
            raise HwmCoverageError("accepted intervals are not contiguous")
        evidence_sha256 = _sha256(
            original["interval_evidence_sha256"],
            "interval.interval_evidence_sha256",
        )
        normalized: dict[str, Any] = {
            "interval_id": interval_id,
            "interval_start": interval_start.isoformat(),
            "interval_end": interval_end.isoformat(),
            "interval_evidence_sha256": evidence_sha256,
        }
        normalized_intervals.append(normalized)
        interval_by_id[interval_id] = {
            **normalized,
            "start_date": interval_start,
            "end_date": interval_end,
        }
        previous_end = interval_end
    if not normalized_intervals:
        raise HwmCoverageError("accepted intervals are empty")
    if normalized_intervals[-1]["interval_end"] != complete_date.isoformat():
        raise HwmCoverageError(
            "complete_through differs from the accepted interval boundary"
        )

    calendar = _json_mapping_from_bytes(calendar_bytes, "calendar_bytes")
    _strict_keys(
        calendar,
        {
            "schema_version",
            "calendar_policy_id",
            "authority_status",
            "accepted_intervals",
            "authority_evidence_sha256",
        },
        "calendar authority",
    )
    if calendar["schema_version"] != 2:
        raise HwmCoverageError("calendar authority schema version differs")
    calendar_policy_id = _text(
        calendar["calendar_policy_id"], "calendar.calendar_policy_id"
    )
    calendar_authority_status = _text(
        calendar["authority_status"], "calendar.authority_status"
    )
    policy_calendar = _mapping(policy["calendar_policy"], "policy.calendar_policy")
    if (
        calendar_authority_status != _PORTABLE_INPUT_AUTHORITY
        or calendar_policy_id != policy_calendar["portable_synthetic_policy_id"]
    ):
        raise HwmCoverageError(
            "governed calendar authority integration is required for financial acceptance"
        )
    if assurance_profile != "DEVELOPMENT":
        raise HwmCoverageError(
            "portable synthetic HWM inputs are development proof only"
        )
    calendar_intervals = calendar["accepted_intervals"]
    if not isinstance(calendar_intervals, list) or calendar_intervals != [
        {
            "interval_id": item["interval_id"],
            "interval_start": item["interval_start"],
            "interval_end": item["interval_end"],
        }
        for item in normalized_intervals
    ]:
        raise HwmCoverageError("accepted intervals differ from calendar authority")
    if any(
        item["interval_evidence_sha256"] != accepted_calendar_sha256
        for item in normalized_intervals
    ):
        raise HwmCoverageError("accepted interval calendar identity differs")
    calendar_evidence_sha256 = _sha256(
        calendar["authority_evidence_sha256"],
        "calendar.authority_evidence_sha256",
    )
    expected_calendar_evidence_sha256 = _canonical_sha256(
        {
            key: value
            for key, value in calendar.items()
            if key != "authority_evidence_sha256"
        }
    )
    if calendar_evidence_sha256 != expected_calendar_evidence_sha256:
        raise HwmCoverageError("calendar authority evidence identity differs")

    generation = _json_mapping_from_bytes(
        input_generation_bytes, "input_generation_bytes"
    )
    _strict_keys(
        generation,
        {
            "schema_version",
            "builder",
            "builder_version",
            "authority_profile",
            "authority_bindings",
            "upstream_receipt_sha256",
            "maximum_age_days",
            "evidence_sha256s",
            "flow_events",
        },
        "HWM input generation",
    )
    if (
        generation["schema_version"] != 2
        or generation["builder"] != "portfolio-hwm-inputs"
        or generation["builder_version"] != "0.2.0"
        or generation["authority_profile"] != _PORTABLE_INPUT_AUTHORITY
        or generation["upstream_receipt_sha256"] is not None
    ):
        raise HwmCoverageError("HWM input generation identity differs")
    if generation["maximum_age_days"] != maximum_age_days:
        raise HwmCoverageError("HWM input generation freshness threshold differs")
    authority_bindings = _mapping(
        generation["authority_bindings"], "HWM input authority bindings"
    )
    _strict_keys(
        authority_bindings,
        {
            "policy_sha256",
            "policy_semantic_digest",
            "product_contract_sha256",
            "performance_policy_sha256",
            "legacy_policy_sha256",
            "calendar_sha256",
        },
        "HWM input authority bindings",
    )
    expected_authority_bindings = {
        "policy_sha256": accepted_policy_sha256,
        "policy_semantic_digest": policy_semantic_digest,
        "product_contract_sha256": hashlib.sha256(product_contract_bytes).hexdigest(),
        "performance_policy_sha256": hashlib.sha256(
            performance_policy_bytes
        ).hexdigest(),
        "legacy_policy_sha256": hashlib.sha256(legacy_policy_bytes).hexdigest(),
        "calendar_sha256": accepted_calendar_sha256,
    }
    if authority_bindings != expected_authority_bindings:
        raise HwmCoverageError("HWM input authority bindings differ")
    evidence_values = generation["evidence_sha256s"]
    if (
        not isinstance(evidence_values, list)
        or not evidence_values
        or len(evidence_values) != len(set(evidence_values))
    ):
        raise HwmCoverageError("HWM input evidence registry differs")
    evidence_registry = {
        _sha256(value, "HWM input evidence sha256") for value in evidence_values
    }
    used_evidence_sha256s: set[str] = set()
    if calendar_evidence_sha256 not in evidence_registry:
        raise HwmCoverageError("calendar authority evidence is not admitted")
    used_evidence_sha256s.add(calendar_evidence_sha256)

    flow_event_values = generation["flow_events"]
    if not isinstance(flow_event_values, list):
        raise HwmCoverageError("HWM input flow events must be an array")
    flow_event_by_id: dict[str, dict[str, Any]] = {}
    source_correction_target_ids: set[str] = set()
    normalized_flow_events: list[dict[str, Any]] = []
    event_keys = {
        "event_id",
        "strategy_id",
        "interval_id",
        "subperiod_id",
        "event_type",
        "capital_treatment",
        "effective_at_utc",
        "effective_timezone",
        "timing_basis",
        "original_amount",
        "original_currency",
        "fx_rate_to_usd",
        "fx_authority_id",
        "fx_generation_sha256",
        "usd_amount",
        "correction_kind",
        "related_event_id",
        "transfer_group_id",
        "counterparty_strategy_id",
        "classification_authority_sha256",
        "event_evidence_sha256",
    }
    for event_index, event_value in enumerate(flow_event_values):
        event = _mapping(event_value, f"HWM input flow_events[{event_index}]")
        _strict_keys(event, event_keys, f"HWM input flow_events[{event_index}]")
        event_id = _text(event["event_id"], "flow_event.event_id")
        if event_id in flow_event_by_id:
            raise HwmCoverageError("HWM input flow event identity is duplicated")
        event_type = _text(event["event_type"], "flow_event.event_type")
        expected_treatment = _FLOW_EVENT_TREATMENTS.get(event_type)
        if (
            expected_treatment is None
            or event["capital_treatment"] != expected_treatment
        ):
            raise HwmCoverageError("HWM input flow event classification differs")
        strategy_id = _text(event["strategy_id"], "flow_event.strategy_id")
        interval_id = _text(event["interval_id"], "flow_event.interval_id")
        subperiod_id = _text(event["subperiod_id"], "flow_event.subperiod_id")
        transfer_group_id = (
            None
            if event["transfer_group_id"] is None
            else _text(event["transfer_group_id"], "flow_event.transfer_group_id")
        )
        counterparty_strategy_id = (
            None
            if event["counterparty_strategy_id"] is None
            else _text(
                event["counterparty_strategy_id"],
                "flow_event.counterparty_strategy_id",
            )
        )
        if expected_treatment == "INTERNAL_TRANSFER":
            if (
                transfer_group_id is None
                or counterparty_strategy_id is None
                or counterparty_strategy_id == strategy_id
            ):
                raise HwmCoverageError("HWM internal transfer pairing differs")
        elif transfer_group_id is not None or counterparty_strategy_id is not None:
            raise HwmCoverageError("non-transfer HWM event has transfer pairing")
        effective_at = _canonical_utc_timestamp(
            event["effective_at_utc"], "flow_event.effective_at_utc"
        )
        timing_basis = _text(event["timing_basis"], "flow_event.timing_basis")
        if timing_basis not in {"EXACT_TIMESTAMP", "DATE_ONLY_END_OF_DAY"}:
            raise HwmCoverageError("HWM input flow event timing basis differs")
        original_amount = _decimal(
            event["original_amount"], "flow_event.original_amount"
        )
        original_currency = _text(
            event["original_currency"], "flow_event.original_currency"
        )
        if re.fullmatch(r"[A-Z]{3}", original_currency) is None:
            raise HwmCoverageError("HWM input flow event currency differs")
        fx_rate = _decimal(event["fx_rate_to_usd"], "flow_event.fx_rate_to_usd")
        usd_amount = _decimal(event["usd_amount"], "flow_event.usd_amount")
        if fx_rate <= 0 or Fraction(original_amount) * Fraction(fx_rate) != Fraction(
            usd_amount
        ):
            raise HwmCoverageError("HWM input flow event FX conversion differs")
        fx_authority_id = _text(event["fx_authority_id"], "flow_event.fx_authority_id")
        fx_generation_sha256 = _sha256(
            event["fx_generation_sha256"], "flow_event.fx_generation_sha256"
        )
        if original_currency == "USD":
            if fx_rate != 1 or fx_authority_id != "IDENTITY_USD":
                raise HwmCoverageError("HWM input USD identity FX differs")
        elif fx_authority_id == "IDENTITY_USD":
            raise HwmCoverageError("HWM input non-USD FX authority differs")
        classification_authority_sha256 = _sha256(
            event["classification_authority_sha256"],
            "flow_event.classification_authority_sha256",
        )
        if (
            classification_authority_sha256
            != expected_authority_bindings["product_contract_sha256"]
        ):
            raise HwmCoverageError("HWM input flow classification authority differs")
        correction_kind = _text(event["correction_kind"], "flow_event.correction_kind")
        if correction_kind not in {
            "ORIGINAL",
            "SOURCE_CORRECTION",
            "ECONOMIC_REVERSAL",
        }:
            raise HwmCoverageError("HWM input flow correction kind differs")
        related_event_id = event["related_event_id"]
        if correction_kind == "ORIGINAL":
            if related_event_id is not None:
                raise HwmCoverageError("HWM original flow event has correction lineage")
        elif not isinstance(related_event_id, str) or not related_event_id:
            raise HwmCoverageError("HWM corrected flow event lacks lineage")
        normalized_without_evidence = {
            "event_id": event_id,
            "strategy_id": strategy_id,
            "interval_id": interval_id,
            "subperiod_id": subperiod_id,
            "event_type": event_type,
            "capital_treatment": expected_treatment,
            "effective_at_utc": _text(
                event["effective_at_utc"], "flow_event.effective_at_utc"
            ),
            "effective_timezone": _text(
                event["effective_timezone"], "flow_event.effective_timezone"
            ),
            "timing_basis": timing_basis,
            "original_amount": _decimal_text(original_amount),
            "original_currency": original_currency,
            "fx_rate_to_usd": _decimal_text(fx_rate),
            "fx_authority_id": fx_authority_id,
            "fx_generation_sha256": fx_generation_sha256,
            "usd_amount": _decimal_text(usd_amount),
            "correction_kind": correction_kind,
            "related_event_id": related_event_id,
            "transfer_group_id": transfer_group_id,
            "counterparty_strategy_id": counterparty_strategy_id,
            "classification_authority_sha256": classification_authority_sha256,
        }
        evidence_sha256 = _sha256(
            event["event_evidence_sha256"], "flow_event.event_evidence_sha256"
        )
        if evidence_sha256 != _canonical_sha256(normalized_without_evidence):
            raise HwmCoverageError("HWM input flow event evidence identity differs")
        if (
            evidence_sha256 not in evidence_registry
            or fx_generation_sha256 not in evidence_registry
        ):
            raise HwmCoverageError("HWM input flow event evidence is not admitted")
        normalized = {
            **normalized_without_evidence,
            "event_evidence_sha256": evidence_sha256,
            "effective_at": effective_at,
            "usd_amount_decimal": usd_amount,
        }
        flow_event_by_id[event_id] = normalized
        normalized_flow_events.append(normalized)
        used_evidence_sha256s.update({evidence_sha256, fx_generation_sha256})

    for event in normalized_flow_events:
        correction_kind = event["correction_kind"]
        if correction_kind in {"ORIGINAL", "SOURCE_CORRECTION"}:
            if (
                event["event_type"]
                in {
                    "OWNER_CONTRIBUTION",
                    "IN_KIND_FUNDING",
                    "OPENING_BASIS",
                    "OWNER_BOUNDARY_PREDECESSOR_TRANSITION",
                    "MARGIN_BORROW",
                    "FINANCING_COST",
                }
                and event["usd_amount_decimal"] <= 0
            ):
                raise HwmCoverageError("HWM input flow event sign differs")
            if (
                event["event_type"] in {"OWNER_WITHDRAWAL", "MARGIN_REPAYMENT"}
                and event["usd_amount_decimal"] >= 0
            ):
                raise HwmCoverageError("HWM input flow event sign differs")
        if correction_kind == "ORIGINAL":
            continue
        related = flow_event_by_id.get(event["related_event_id"])
        if related is None or related["correction_kind"] != "ORIGINAL":
            raise HwmCoverageError("HWM corrected flow event lineage differs")
        if (
            event["strategy_id"],
            event["event_type"],
            event["capital_treatment"],
        ) != (
            related["strategy_id"],
            related["event_type"],
            related["capital_treatment"],
        ):
            raise HwmCoverageError("HWM corrected flow event classification differs")
        if correction_kind == "SOURCE_CORRECTION":
            if (
                event["effective_at"] != related["effective_at"]
                or event["interval_id"] != related["interval_id"]
                or event["subperiod_id"] != related["subperiod_id"]
                or related["event_id"] in source_correction_target_ids
            ):
                raise HwmCoverageError("HWM source correction effective time differs")
            source_correction_target_ids.add(related["event_id"])
        elif (
            event["effective_at"] <= related["effective_at"]
            or event["usd_amount_decimal"] != -related["usd_amount_decimal"]
        ):
            raise HwmCoverageError("HWM economic reversal differs")

    effective_flow_events_by_subperiod: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for event in normalized_flow_events:
        if event["event_id"] in source_correction_target_ids:
            continue
        effective_flow_events_by_subperiod[
            (event["strategy_id"], event["interval_id"], event["subperiod_id"])
        ].append(event)
    for events in effective_flow_events_by_subperiod.values():
        events.sort(key=lambda item: (item["effective_at"], item["event_id"]))

    membership_keys = {
        "strategy_id",
        "effective_start",
        "effective_end",
        "membership_evidence_sha256",
    }
    normalized_memberships: list[dict[str, str | None]] = []
    membership_by_strategy: dict[str, dict[str, Any]] = {}
    interval_starts = {item["start_date"] for item in interval_by_id.values()}
    interval_boundaries = interval_starts | {
        item["end_date"] for item in interval_by_id.values()
    }
    first_interval_start = next(iter(interval_by_id.values()))["start_date"]
    final_interval_end = list(interval_by_id.values())[-1]["end_date"]
    for index, original in enumerate(memberships):
        if not isinstance(original, Mapping):
            raise HwmCoverageError("membership must be an object")
        _strict_keys(original, membership_keys, f"memberships[{index}]")
        strategy_id = _text(original["strategy_id"], "membership.strategy_id")
        if strategy_id in membership_by_strategy:
            raise HwmCoverageError("strategy membership is duplicated")
        effective_start = _canonical_date(
            original["effective_start"], "membership.effective_start"
        )
        raw_end = original["effective_end"]
        effective_end = (
            None
            if raw_end is None
            else _canonical_date(raw_end, "membership.effective_end")
        )
        if effective_end is not None and effective_end <= effective_start:
            raise HwmCoverageError("membership interval must have positive duration")
        if (
            first_interval_start < effective_start < final_interval_end
            and effective_start not in interval_starts
        ):
            raise HwmCoverageError(
                "membership entry is not aligned to an accepted interval"
            )
        if (
            effective_end is not None
            and first_interval_start < effective_end < final_interval_end
            and effective_end not in interval_boundaries
        ):
            raise HwmCoverageError(
                "membership exit is not aligned to an accepted interval"
            )
        evidence_sha256 = _sha256(
            original["membership_evidence_sha256"],
            "membership.membership_evidence_sha256",
        )
        expected_membership_evidence_sha256 = _canonical_sha256(
            {
                "schema_version": 1,
                "evidence_type": "FUNDED_MEMBERSHIP",
                "strategy_id": strategy_id,
                "effective_start": effective_start.isoformat(),
                "effective_end": (
                    effective_end.isoformat() if effective_end is not None else None
                ),
            }
        )
        if evidence_sha256 != expected_membership_evidence_sha256:
            raise HwmCoverageError("membership evidence identity differs")
        if evidence_sha256 not in evidence_registry:
            raise HwmCoverageError("membership evidence is not admitted")
        used_evidence_sha256s.add(evidence_sha256)
        membership_normalized: dict[str, str | None] = {
            "strategy_id": strategy_id,
            "effective_start": effective_start.isoformat(),
            "effective_end": (
                effective_end.isoformat() if effective_end is not None else None
            ),
            "membership_evidence_sha256": evidence_sha256,
        }
        normalized_memberships.append(membership_normalized)
        membership_by_strategy[strategy_id] = {
            **membership_normalized,
            "start_date": effective_start,
            "end_date": effective_end,
        }
    if not normalized_memberships:
        raise HwmCoverageError("memberships are empty")

    def active_members(interval: Mapping[str, Any]) -> set[str]:
        start = interval["start_date"]
        return {
            strategy_id
            for strategy_id, membership in membership_by_strategy.items()
            if membership["start_date"] <= start
            and (membership["end_date"] is None or start < membership["end_date"])
        }

    active_members_by_interval = {
        interval_id: active_members(interval)
        for interval_id, interval in interval_by_id.items()
    }
    inactive_members = set(membership_by_strategy) - set().union(
        *active_members_by_interval.values()
    )
    if inactive_members:
        raise HwmCoverageError("membership has no effective accepted interval")
    if any(not members for members in active_members_by_interval.values()):
        raise HwmCoverageError("accepted interval has no effective members")

    interval_order = {
        item["interval_id"]: index for index, item in enumerate(normalized_intervals)
    }
    effective_flow_events = [
        event
        for event in normalized_flow_events
        if event["event_id"] not in source_correction_target_ids
    ]
    opening_event_ids: set[str] = set()
    opening_basis_strategies: set[str] = set()
    transfer_events_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in effective_flow_events:
        interval = interval_by_id.get(event["interval_id"])
        membership = membership_by_strategy.get(event["strategy_id"])
        if interval is None or membership is None:
            raise HwmCoverageError("HWM flow event authority reference differs")
        if event["event_type"] == "OPENING_BASIS" and event["correction_kind"] in {
            "ORIGINAL",
            "SOURCE_CORRECTION",
        }:
            opening_boundary = datetime(
                interval["start_date"].year,
                interval["start_date"].month,
                interval["start_date"].day,
                tzinfo=timezone.utc,
            )
            if (
                event["strategy_id"] in opening_basis_strategies
                or membership["start_date"] != interval["start_date"]
                or event["effective_at"] != opening_boundary
                or event["timing_basis"] != "EXACT_TIMESTAMP"
            ):
                raise HwmCoverageError(
                    "opening basis differs from funded membership start"
                )
            opening_basis_strategies.add(event["strategy_id"])
            opening_event_ids.add(event["event_id"])
        if event["capital_treatment"] == "INTERNAL_TRANSFER":
            if event["usd_amount_decimal"] == 0:
                raise HwmCoverageError("HWM internal transfer amount differs")
            transfer_events_by_group[event["transfer_group_id"]].append(event)

    for transfer_group_id, events in transfer_events_by_group.items():
        if len(events) != 2:
            raise HwmCoverageError("HWM internal transfer pairing differs")
        first, second = events
        if (
            first["counterparty_strategy_id"] != second["strategy_id"]
            or second["counterparty_strategy_id"] != first["strategy_id"]
            or first["usd_amount_decimal"] + second["usd_amount_decimal"] != 0
        ):
            raise HwmCoverageError("HWM internal transfer pairing differs")
        if first["interval_id"] == second["interval_id"]:
            if first["effective_at"] != second["effective_at"]:
                raise HwmCoverageError("HWM internal transfer timing differs")
            continue
        negative = first if first["usd_amount_decimal"] < 0 else second
        positive = second if negative is first else first
        negative_interval = interval_by_id[negative["interval_id"]]
        positive_interval = interval_by_id[positive["interval_id"]]
        boundary = datetime(
            negative_interval["end_date"].year,
            negative_interval["end_date"].month,
            negative_interval["end_date"].day,
            tzinfo=timezone.utc,
        )
        if (
            {negative["event_type"], positive["event_type"]}
            != {"PREDECESSOR_TRANSITION"}
            or interval_order[positive["interval_id"]]
            != interval_order[negative["interval_id"]] + 1
            or negative_interval["end_date"] != positive_interval["start_date"]
            or membership_by_strategy[negative["strategy_id"]]["end_date"]
            != negative_interval["end_date"]
            or membership_by_strategy[positive["strategy_id"]]["start_date"]
            != positive_interval["start_date"]
            or negative["effective_at"] != boundary
            or positive["effective_at"] != boundary
            or positive["timing_basis"] != "EXACT_TIMESTAMP"
        ):
            raise HwmCoverageError(
                f"predecessor transfer group {transfer_group_id} boundary differs"
            )
        opening_event_ids.add(positive["event_id"])

    component_keys = {
        "strategy_id",
        "interval_id",
        "interval_start",
        "interval_end",
        "beginning_capital_usd",
        "beginning_gross_assets_usd",
        "beginning_financing_liability_usd",
        "ending_gross_assets_usd",
        "ending_financing_liability_usd",
        "margin_principal_flow_usd",
        "financing_cost_usd",
        "gross_asset_return",
        "ending_nav_usd",
        "external_flow_usd",
        "internal_transfer_usd",
        "flow_adjusted_ending_nav_usd",
        "period_return",
        "flow_timing",
        "subperiods",
        "capital_basis",
        "capital_evidence_sha256",
        "flow_reconciliation_sha256",
        "return_evidence_sha256",
        "leverage_evidence_sha256",
        "source_generation_sha256",
        "policy_sha256",
        "calendar_sha256",
        "return_basis",
    }
    normalized_components: list[dict[str, Any]] = []
    component_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    components_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    used_effective_flow_event_ids: set[str] = set()
    for index, original in enumerate(component_periods):
        if not isinstance(original, Mapping):
            raise HwmCoverageError("component period must be an object")
        _strict_keys(original, component_keys, f"component_periods[{index}]")
        strategy_id = _text(original["strategy_id"], "component.strategy_id")
        interval_id = _text(original["interval_id"], "component.interval_id")
        interval = interval_by_id.get(interval_id)
        if interval is None:
            raise HwmCoverageError("component references an unknown interval")
        if strategy_id not in membership_by_strategy:
            raise HwmCoverageError("component references an unknown membership")
        key = (interval_id, strategy_id)
        if key in component_by_key:
            raise HwmCoverageError("component period identity is duplicated")
        interval_start = _canonical_date(
            original["interval_start"], "component.interval_start"
        )
        interval_end = _canonical_date(
            original["interval_end"], "component.interval_end"
        )
        if (
            interval_start != interval["start_date"]
            or interval_end != interval["end_date"]
        ):
            raise HwmCoverageError("component interval differs from authority")
        if strategy_id not in active_members_by_interval[interval_id]:
            raise HwmCoverageError("component exists outside effective membership")
        beginning_capital = _decimal(
            original["beginning_capital_usd"],
            "component.beginning_capital_usd",
        )
        beginning_gross_assets = _decimal(
            original["beginning_gross_assets_usd"],
            "component.beginning_gross_assets_usd",
        )
        beginning_financing_liability = _decimal(
            original["beginning_financing_liability_usd"],
            "component.beginning_financing_liability_usd",
        )
        gross_assets = _decimal(
            original["ending_gross_assets_usd"],
            "component.ending_gross_assets_usd",
        )
        financing_liability = _decimal(
            original["ending_financing_liability_usd"],
            "component.ending_financing_liability_usd",
        )
        margin_principal_flow = _decimal(
            original["margin_principal_flow_usd"],
            "component.margin_principal_flow_usd",
        )
        financing_cost = _decimal(
            original["financing_cost_usd"], "component.financing_cost_usd"
        )
        gross_asset_return = _decimal(
            original["gross_asset_return"], "component.gross_asset_return"
        )
        ending_nav = _decimal(original["ending_nav_usd"], "component.ending_nav_usd")
        external_flow = _decimal(
            original["external_flow_usd"], "component.external_flow_usd"
        )
        internal_transfer = _decimal(
            original["internal_transfer_usd"],
            "component.internal_transfer_usd",
        )
        flow_adjusted_ending_nav = _decimal(
            original["flow_adjusted_ending_nav_usd"],
            "component.flow_adjusted_ending_nav_usd",
        )
        period_return = _decimal(original["period_return"], "component.period_return")
        if beginning_capital <= 0:
            raise HwmCoverageError("component beginning capital must be positive")
        if (
            beginning_gross_assets < 0
            or beginning_financing_liability < 0
            or gross_assets < 0
            or financing_liability < 0
            or financing_cost < 0
            or gross_asset_return <= Decimal("-1")
        ):
            raise HwmCoverageError("component leverage facts cannot be negative")
        if beginning_gross_assets - beginning_financing_liability != beginning_capital:
            raise HwmCoverageError(
                "component beginning net NAV differs from leverage facts"
            )
        if beginning_financing_liability + margin_principal_flow != financing_liability:
            raise HwmCoverageError("component margin principal reconciliation differs")
        membership_ends_at_interval = (
            membership_by_strategy[strategy_id]["end_date"] == interval_end
        )
        if membership_ends_at_interval and ending_nav != 0:
            raise HwmCoverageError("membership exit retains residual net NAV")
        if (
            flow_adjusted_ending_nav <= 0
            or ending_nav < 0
            or (ending_nav == 0 and not membership_ends_at_interval)
        ):
            raise HwmCoverageError("component ending net NAV is invalid")
        if gross_assets - financing_liability != ending_nav:
            raise HwmCoverageError(
                "component net NAV differs from gross assets less financing liability"
            )
        if period_return <= Decimal("-1"):
            raise HwmCoverageError("component return must be greater than negative one")
        flow_timing = _text(original["flow_timing"], "component.flow_timing")
        subperiod_values = original["subperiods"]
        if not isinstance(subperiod_values, Sequence) or isinstance(
            subperiod_values, str | bytes
        ):
            raise HwmCoverageError("component subperiods must be an array")
        subperiod_keys = {
            "subperiod_id",
            "subperiod_start_utc",
            "subperiod_end_utc",
            "beginning_nav_usd",
            "ending_nav_before_flow_usd",
            "beginning_gross_assets_usd",
            "beginning_financing_liability_usd",
            "ending_gross_assets_before_flow_usd",
            "ending_financing_liability_before_flow_usd",
            "margin_principal_flow_after_subperiod_usd",
            "financing_cost_usd",
            "external_flow_after_subperiod_usd",
            "internal_transfer_after_subperiod_usd",
            "flow_event_evidence_sha256",
        }
        normalized_subperiods: list[dict[str, str]] = []
        linked_return = Fraction(1, 1)
        linked_gross_return = Fraction(1, 1)
        summed_external_flow = Decimal("0")
        summed_internal_transfer = Decimal("0")
        summed_margin_principal_flow = Decimal("0")
        summed_financing_cost = Decimal("0")
        previous_post_flow_nav: Decimal | None = None
        previous_post_flow_gross_assets: Decimal | None = None
        previous_post_flow_financing_liability: Decimal | None = None
        previous_subperiod_end: datetime | None = None
        subperiod_ids: set[str] = set()
        for subperiod_index, subperiod_value in enumerate(subperiod_values):
            if not isinstance(subperiod_value, Mapping):
                raise HwmCoverageError("component subperiod must be an object")
            _strict_keys(
                subperiod_value,
                subperiod_keys,
                f"component.subperiods[{subperiod_index}]",
            )
            subperiod_id = _text(
                subperiod_value["subperiod_id"], "subperiod.subperiod_id"
            )
            if subperiod_id in subperiod_ids:
                raise HwmCoverageError("component subperiod identity is duplicated")
            subperiod_ids.add(subperiod_id)
            subperiod_start = _canonical_utc_timestamp(
                subperiod_value["subperiod_start_utc"],
                "subperiod.subperiod_start_utc",
            )
            subperiod_end = _canonical_utc_timestamp(
                subperiod_value["subperiod_end_utc"],
                "subperiod.subperiod_end_utc",
            )
            expected_interval_start = datetime(
                interval_start.year,
                interval_start.month,
                interval_start.day,
                tzinfo=timezone.utc,
            )
            expected_interval_end = datetime(
                interval_end.year,
                interval_end.month,
                interval_end.day,
                tzinfo=timezone.utc,
            )
            if (
                subperiod_end <= subperiod_start
                or (subperiod_index == 0 and subperiod_start != expected_interval_start)
                or (
                    previous_subperiod_end is not None
                    and subperiod_start != previous_subperiod_end
                )
            ):
                raise HwmCoverageError("component subperiod time boundaries differ")
            subperiod_beginning = _decimal(
                subperiod_value["beginning_nav_usd"],
                "subperiod.beginning_nav_usd",
            )
            subperiod_ending = _decimal(
                subperiod_value["ending_nav_before_flow_usd"],
                "subperiod.ending_nav_before_flow_usd",
            )
            subperiod_beginning_gross_assets = _decimal(
                subperiod_value["beginning_gross_assets_usd"],
                "subperiod.beginning_gross_assets_usd",
            )
            subperiod_beginning_financing_liability = _decimal(
                subperiod_value["beginning_financing_liability_usd"],
                "subperiod.beginning_financing_liability_usd",
            )
            subperiod_ending_gross_assets = _decimal(
                subperiod_value["ending_gross_assets_before_flow_usd"],
                "subperiod.ending_gross_assets_before_flow_usd",
            )
            subperiod_ending_financing_liability = _decimal(
                subperiod_value["ending_financing_liability_before_flow_usd"],
                "subperiod.ending_financing_liability_before_flow_usd",
            )
            subperiod_margin_principal_flow = _decimal(
                subperiod_value["margin_principal_flow_after_subperiod_usd"],
                "subperiod.margin_principal_flow_after_subperiod_usd",
            )
            subperiod_financing_cost = _decimal(
                subperiod_value["financing_cost_usd"],
                "subperiod.financing_cost_usd",
            )
            subperiod_external_flow = _decimal(
                subperiod_value["external_flow_after_subperiod_usd"],
                "subperiod.external_flow_after_subperiod_usd",
            )
            subperiod_internal_transfer = _decimal(
                subperiod_value["internal_transfer_after_subperiod_usd"],
                "subperiod.internal_transfer_after_subperiod_usd",
            )
            effective_events = effective_flow_events_by_subperiod.get(
                (strategy_id, interval_id, subperiod_id), []
            )
            if any(
                event["effective_at"]
                != (
                    subperiod_start
                    if event["event_id"] in opening_event_ids
                    else subperiod_end
                )
                for event in effective_events
            ):
                raise HwmCoverageError("HWM flow event effective time differs")
            derived_external_flow = sum(
                (
                    event["usd_amount_decimal"]
                    for event in effective_events
                    if event["capital_treatment"] == "EXTERNAL_FLOW"
                    and event["event_id"] not in opening_event_ids
                ),
                Decimal("0"),
            )
            derived_internal_transfer = sum(
                (
                    event["usd_amount_decimal"]
                    for event in effective_events
                    if event["capital_treatment"] == "INTERNAL_TRANSFER"
                    and event["event_id"] not in opening_event_ids
                ),
                Decimal("0"),
            )
            derived_margin_principal = sum(
                (
                    event["usd_amount_decimal"]
                    for event in effective_events
                    if event["capital_treatment"] == "MARGIN_PRINCIPAL"
                ),
                Decimal("0"),
            )
            derived_financing_cost = sum(
                (
                    event["usd_amount_decimal"]
                    for event in effective_events
                    if event["capital_treatment"] == "FINANCING_COST"
                ),
                Decimal("0"),
            )
            if (
                derived_external_flow != subperiod_external_flow
                or derived_internal_transfer != subperiod_internal_transfer
                or derived_margin_principal != subperiod_margin_principal_flow
                or derived_financing_cost != subperiod_financing_cost
            ):
                raise HwmCoverageError(
                    "component flow facts differ from admitted events"
                )
            used_effective_flow_event_ids.update(
                event["event_id"] for event in effective_events
            )
            if (
                subperiod_beginning <= 0
                or subperiod_ending <= 0
                or subperiod_beginning_gross_assets <= 0
                or subperiod_ending_gross_assets < 0
                or subperiod_beginning_financing_liability < 0
                or subperiod_ending_financing_liability < 0
                or subperiod_financing_cost < 0
            ):
                raise HwmCoverageError("component subperiod NAV must be positive")
            if (
                subperiod_beginning_gross_assets
                - subperiod_beginning_financing_liability
                != subperiod_beginning
                or subperiod_ending_gross_assets - subperiod_ending_financing_liability
                != subperiod_ending
            ):
                raise HwmCoverageError("component subperiod leverage facts differ")
            if (
                subperiod_ending_financing_liability
                != subperiod_beginning_financing_liability
            ):
                raise HwmCoverageError(
                    "component subperiod liability changes without principal flow"
                )
            if subperiod_index == 0 and subperiod_beginning != beginning_capital:
                raise HwmCoverageError("first subperiod differs from beginning capital")
            if subperiod_index == 0 and (
                subperiod_beginning_gross_assets != beginning_gross_assets
                or subperiod_beginning_financing_liability
                != beginning_financing_liability
            ):
                raise HwmCoverageError("first subperiod leverage facts differ")
            if (
                previous_post_flow_nav is not None
                and subperiod_beginning != previous_post_flow_nav
            ):
                raise HwmCoverageError("component subperiod flow linkage differs")
            if previous_post_flow_gross_assets is not None and (
                subperiod_beginning_gross_assets != previous_post_flow_gross_assets
                or subperiod_beginning_financing_liability
                != previous_post_flow_financing_liability
            ):
                raise HwmCoverageError("component subperiod leverage linkage differs")
            normalized_subperiod_without_evidence = {
                "subperiod_id": subperiod_id,
                "subperiod_start_utc": _text(
                    subperiod_value["subperiod_start_utc"],
                    "subperiod.subperiod_start_utc",
                ),
                "subperiod_end_utc": _text(
                    subperiod_value["subperiod_end_utc"],
                    "subperiod.subperiod_end_utc",
                ),
                "beginning_nav_usd": _decimal_text(subperiod_beginning),
                "ending_nav_before_flow_usd": _decimal_text(subperiod_ending),
                "beginning_gross_assets_usd": _decimal_text(
                    subperiod_beginning_gross_assets
                ),
                "beginning_financing_liability_usd": _decimal_text(
                    subperiod_beginning_financing_liability
                ),
                "ending_gross_assets_before_flow_usd": _decimal_text(
                    subperiod_ending_gross_assets
                ),
                "ending_financing_liability_before_flow_usd": _decimal_text(
                    subperiod_ending_financing_liability
                ),
                "margin_principal_flow_after_subperiod_usd": _decimal_text(
                    subperiod_margin_principal_flow
                ),
                "financing_cost_usd": _decimal_text(subperiod_financing_cost),
                "external_flow_after_subperiod_usd": _decimal_text(
                    subperiod_external_flow
                ),
                "internal_transfer_after_subperiod_usd": _decimal_text(
                    subperiod_internal_transfer
                ),
            }
            flow_evidence_sha256 = _sha256(
                subperiod_value["flow_event_evidence_sha256"],
                "subperiod.flow_event_evidence_sha256",
            )
            expected_flow_evidence_sha256 = _canonical_sha256(
                {
                    "schema_version": 2,
                    "evidence_type": "FLOW_EVENT",
                    "strategy_id": strategy_id,
                    "interval_id": interval_id,
                    "subperiod": {
                        **normalized_subperiod_without_evidence,
                        "flow_event_ids": [
                            event["event_id"] for event in effective_events
                        ],
                        "flow_event_sha256s": [
                            event["event_evidence_sha256"] for event in effective_events
                        ],
                    },
                }
            )
            if flow_evidence_sha256 != expected_flow_evidence_sha256:
                raise HwmCoverageError("subperiod flow evidence identity differs")
            if flow_evidence_sha256 not in evidence_registry:
                raise HwmCoverageError("subperiod flow evidence is not admitted")
            used_evidence_sha256s.add(flow_evidence_sha256)
            linked_return *= Fraction(subperiod_ending) / Fraction(subperiod_beginning)
            linked_gross_return *= Fraction(
                subperiod_ending_gross_assets + subperiod_financing_cost
            ) / Fraction(subperiod_beginning_gross_assets)
            summed_external_flow += subperiod_external_flow
            summed_internal_transfer += subperiod_internal_transfer
            summed_margin_principal_flow += subperiod_margin_principal_flow
            summed_financing_cost += subperiod_financing_cost
            previous_post_flow_nav = (
                subperiod_ending + subperiod_external_flow + subperiod_internal_transfer
            )
            previous_post_flow_gross_assets = (
                subperiod_ending_gross_assets
                + subperiod_external_flow
                + subperiod_internal_transfer
                + subperiod_margin_principal_flow
            )
            previous_post_flow_financing_liability = (
                subperiod_ending_financing_liability + subperiod_margin_principal_flow
            )
            previous_subperiod_end = subperiod_end
            normalized_subperiods.append(
                {
                    **normalized_subperiod_without_evidence,
                    "flow_event_evidence_sha256": flow_evidence_sha256,
                }
            )
        if not normalized_subperiods:
            raise HwmCoverageError("component subperiods are empty")
        expected_interval_end = datetime(
            interval_end.year,
            interval_end.month,
            interval_end.day,
            tzinfo=timezone.utc,
        )
        if previous_subperiod_end != expected_interval_end:
            raise HwmCoverageError("component subperiod time boundaries differ")
        if previous_post_flow_nav != ending_nav:
            raise HwmCoverageError("component ending NAV differs from subperiods")
        if (
            previous_post_flow_gross_assets != gross_assets
            or previous_post_flow_financing_liability != financing_liability
        ):
            raise HwmCoverageError(
                "component ending leverage facts differ from subperiods"
            )
        if ending_nav == 0:
            final_subperiod_id = normalized_subperiods[-1]["subperiod_id"]
            terminal_events = effective_flow_events_by_subperiod.get(
                (strategy_id, interval_id, final_subperiod_id), []
            )
            if (
                gross_assets != 0
                or financing_liability != 0
                or not any(
                    event["event_type"]
                    in {
                        "OWNER_WITHDRAWAL",
                        "INTERNAL_TRANSFER",
                        "PREDECESSOR_TRANSITION",
                    }
                    and event["usd_amount_decimal"] < 0
                    for event in terminal_events
                )
            ):
                raise HwmCoverageError(
                    "zero ending NAV lacks an admitted funded-exit event"
                )
        if summed_external_flow != external_flow:
            raise HwmCoverageError("component external flow differs from subperiods")
        if summed_internal_transfer != internal_transfer:
            raise HwmCoverageError(
                "component internal transfer differs from subperiods"
            )
        if summed_margin_principal_flow != margin_principal_flow:
            raise HwmCoverageError("component margin principal differs from subperiods")
        if summed_financing_cost != financing_cost:
            raise HwmCoverageError("component financing cost differs from subperiods")
        if flow_timing == "NO_EXTERNAL_OR_INTERNAL_FLOW":
            if external_flow != 0 or internal_transfer != 0:
                raise HwmCoverageError("no-flow component contains a flow")
        elif flow_timing == "DATE_ONLY_END_OF_DAY":
            if len(normalized_subperiods) != 1 or (
                external_flow == 0 and internal_transfer == 0
            ):
                raise HwmCoverageError("date-only flow timing differs")
            if any(
                event["timing_basis"] != "DATE_ONLY_END_OF_DAY"
                for event in effective_flow_events_by_subperiod.get(
                    (
                        strategy_id,
                        interval_id,
                        normalized_subperiods[0]["subperiod_id"],
                    ),
                    [],
                )
                if event["capital_treatment"] in {"EXTERNAL_FLOW", "INTERNAL_TRANSFER"}
            ):
                raise HwmCoverageError("date-only flow event timing differs")
        elif flow_timing == "EXACT_TIMESTAMP_SEGMENTED":
            if len(normalized_subperiods) < 2 or all(
                _decimal(item["external_flow_after_subperiod_usd"], "flow") == 0
                and _decimal(item["internal_transfer_after_subperiod_usd"], "transfer")
                == 0
                for item in normalized_subperiods[:-1]
            ):
                raise HwmCoverageError("exact-timestamp flow timing differs")
            if any(
                event["timing_basis"] != "EXACT_TIMESTAMP"
                for item in normalized_subperiods[:-1]
                for event in effective_flow_events_by_subperiod.get(
                    (strategy_id, interval_id, item["subperiod_id"]), []
                )
                if event["capital_treatment"] in {"EXTERNAL_FLOW", "INTERNAL_TRANSFER"}
            ):
                raise HwmCoverageError("exact-timestamp flow event timing differs")
        else:
            raise HwmCoverageError("component flow timing is invalid")
        derived_return_fraction = linked_return - Fraction(1, 1)
        derived_gross_return_fraction = linked_gross_return - Fraction(1, 1)
        with localcontext() as ctx:
            ctx.prec = 34
            ctx.rounding = ROUND_HALF_EVEN
            derived_period_return = Decimal(
                derived_return_fraction.numerator
            ) / Decimal(derived_return_fraction.denominator)
            derived_gross_asset_return = Decimal(
                derived_gross_return_fraction.numerator
            ) / Decimal(derived_gross_return_fraction.denominator)
        if Fraction(flow_adjusted_ending_nav) != (
            Fraction(beginning_capital) * linked_return
        ):
            raise HwmCoverageError(
                "component flow-adjusted ending NAV differs from subperiod TWR"
            )
        if derived_period_return != period_return:
            raise HwmCoverageError(
                "component return differs from flow-adjusted net NAV"
            )
        if derived_gross_asset_return != gross_asset_return:
            raise HwmCoverageError(
                "component gross asset return differs from leverage facts"
            )
        capital_basis = _text(original["capital_basis"], "component.capital_basis")
        if capital_basis not in {
            "OBSERVED_INDEPENDENT",
            "ROLLFORWARD_FROM_PRIOR_INTERVAL",
        }:
            raise HwmCoverageError("component capital basis is invalid")
        return_basis = _text(original["return_basis"], "component.return_basis")
        if return_basis != AS_HELD_HWM_RETURN_BASIS:
            raise HwmCoverageError("component return basis is invalid")
        if (
            _sha256(original["policy_sha256"], "component.policy_sha256")
            != accepted_policy_sha256
        ):
            raise HwmCoverageError("component policy identity differs")
        if (
            _sha256(original["calendar_sha256"], "component.calendar_sha256")
            != accepted_calendar_sha256
        ):
            raise HwmCoverageError("component calendar identity differs")
        hashes = {
            field: _sha256(original[field], f"component.{field}")
            for field in {
                "capital_evidence_sha256",
                "flow_reconciliation_sha256",
                "return_evidence_sha256",
                "leverage_evidence_sha256",
                "source_generation_sha256",
            }
        }
        expected_component_evidence = {
            "capital_evidence_sha256": _canonical_sha256(
                {
                    "schema_version": 1,
                    "evidence_type": "BEGINNING_CAPITAL",
                    "strategy_id": strategy_id,
                    "interval_id": interval_id,
                    "beginning_capital_usd": _decimal_text(beginning_capital),
                    "capital_basis": capital_basis,
                }
            ),
            "flow_reconciliation_sha256": _canonical_sha256(
                {
                    "schema_version": 1,
                    "evidence_type": "FLOW_RECONCILIATION",
                    "strategy_id": strategy_id,
                    "interval_id": interval_id,
                    "flow_timing": flow_timing,
                    "external_flow_usd": _decimal_text(external_flow),
                    "internal_transfer_usd": _decimal_text(internal_transfer),
                    "margin_principal_flow_usd": _decimal_text(margin_principal_flow),
                    "subperiod_flow_evidence_sha256s": [
                        item["flow_event_evidence_sha256"]
                        for item in normalized_subperiods
                    ],
                }
            ),
            "return_evidence_sha256": _canonical_sha256(
                {
                    "schema_version": 1,
                    "evidence_type": "LEVERED_NET_RETURN",
                    "strategy_id": strategy_id,
                    "interval_id": interval_id,
                    "beginning_capital_usd": _decimal_text(beginning_capital),
                    "ending_nav_usd": _decimal_text(ending_nav),
                    "flow_adjusted_ending_nav_usd": _decimal_text(
                        flow_adjusted_ending_nav
                    ),
                    "period_return": _decimal_text(period_return),
                    "subperiods": normalized_subperiods,
                }
            ),
            "leverage_evidence_sha256": _canonical_sha256(
                {
                    "schema_version": 1,
                    "evidence_type": "LEVERAGE_RECONCILIATION",
                    "strategy_id": strategy_id,
                    "interval_id": interval_id,
                    "beginning_gross_assets_usd": _decimal_text(beginning_gross_assets),
                    "beginning_financing_liability_usd": _decimal_text(
                        beginning_financing_liability
                    ),
                    "ending_gross_assets_usd": _decimal_text(gross_assets),
                    "ending_financing_liability_usd": _decimal_text(
                        financing_liability
                    ),
                    "margin_principal_flow_usd": _decimal_text(margin_principal_flow),
                    "financing_cost_usd": _decimal_text(financing_cost),
                    "gross_asset_return": _decimal_text(gross_asset_return),
                    "subperiods": normalized_subperiods,
                }
            ),
        }
        for field_name, expected_sha256 in expected_component_evidence.items():
            if hashes[field_name] != expected_sha256:
                raise HwmCoverageError(f"component {field_name} identity differs")
        if hashes["source_generation_sha256"] != accepted_generation_sha256:
            raise HwmCoverageError("component source generation identity differs")
        if any(
            hashes[field] not in evidence_registry
            for field in {
                "capital_evidence_sha256",
                "flow_reconciliation_sha256",
                "return_evidence_sha256",
                "leverage_evidence_sha256",
            }
        ):
            raise HwmCoverageError("component evidence is not admitted")
        used_evidence_sha256s.update(expected_component_evidence.values())
        component_normalized: dict[str, Any] = {
            "strategy_id": strategy_id,
            "interval_id": interval_id,
            "interval_start": interval_start.isoformat(),
            "interval_end": interval_end.isoformat(),
            "beginning_capital_usd": _decimal_text(beginning_capital),
            "beginning_gross_assets_usd": _decimal_text(beginning_gross_assets),
            "beginning_financing_liability_usd": _decimal_text(
                beginning_financing_liability
            ),
            "ending_gross_assets_usd": _decimal_text(gross_assets),
            "ending_financing_liability_usd": _decimal_text(financing_liability),
            "margin_principal_flow_usd": _decimal_text(margin_principal_flow),
            "financing_cost_usd": _decimal_text(financing_cost),
            "gross_asset_return": _decimal_text(gross_asset_return),
            "ending_nav_usd": _decimal_text(ending_nav),
            "external_flow_usd": _decimal_text(external_flow),
            "internal_transfer_usd": _decimal_text(internal_transfer),
            "flow_adjusted_ending_nav_usd": _decimal_text(flow_adjusted_ending_nav),
            "period_return": _decimal_text(period_return),
            "flow_timing": flow_timing,
            "subperiods": normalized_subperiods,
            "capital_basis": capital_basis,
            **hashes,
            "policy_sha256": accepted_policy_sha256,
            "calendar_sha256": accepted_calendar_sha256,
            "return_basis": return_basis,
        }
        enriched = {
            **component_normalized,
            "beginning_capital": beginning_capital,
            "ending_nav": ending_nav,
            "period_return_decimal": period_return,
        }
        normalized_components.append(component_normalized)
        component_by_key[key] = enriched
        components_by_strategy[strategy_id].append(enriched)

    expected_keys = {
        (interval_id, strategy_id)
        for interval_id, members in active_members_by_interval.items()
        for strategy_id in members
    }
    actual_keys = set(component_by_key)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise HwmCoverageError(
            f"component membership coverage differs; missing={missing}, extra={extra}"
        )
    expected_effective_flow_event_ids = {
        event["event_id"]
        for event in normalized_flow_events
        if event["event_id"] not in source_correction_target_ids
    }
    if used_effective_flow_event_ids != expected_effective_flow_event_ids:
        raise HwmCoverageError("HWM input flow event coverage differs")
    if evidence_registry != used_evidence_sha256s:
        raise HwmCoverageError("HWM input evidence registry coverage differs")

    for strategy_id, components in components_by_strategy.items():
        ordered = sorted(
            components, key=lambda item: interval_order[item["interval_id"]]
        )
        if ordered[0]["capital_basis"] != "OBSERVED_INDEPENDENT":
            raise HwmCoverageError(
                "membership begins without an independent capital anchor"
            )
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current["beginning_capital"] != previous["ending_nav"]:
                raise HwmCoverageError(
                    "beginning capital differs from prior ending net NAV"
                )

    normalized_memberships.sort(key=lambda item: str(item["strategy_id"]))
    normalized_components.sort(
        key=lambda item: (
            interval_order[item["interval_id"]],
            item["strategy_id"],
        )
    )
    normalized_input = {
        "policy_id": AS_HELD_HWM_POLICY_ID,
        "policy_sha256": accepted_policy_sha256,
        "policy_semantic_digest": policy_semantic_digest,
        "calendar_sha256": accepted_calendar_sha256,
        "input_generation_sha256": accepted_generation_sha256,
        "input_authority_class": _PORTABLE_INPUT_AUTHORITY,
        "calendar_authority_status": calendar_authority_status,
        "return_basis": AS_HELD_HWM_RETURN_BASIS,
        "evaluated_on": evaluation_date.isoformat(),
        "complete_through": complete_date.isoformat(),
        "maximum_age_days": maximum_age_days,
        "assurance_profile": assurance_profile,
        "accepted_intervals": normalized_intervals,
        "memberships": normalized_memberships,
        "component_periods": normalized_components,
    }
    input_sha256 = _canonical_sha256(normalized_input)

    with localcontext() as ctx:
        ctx.prec = 34
        ctx.rounding = ROUND_HALF_EVEN
        index_value = Decimal("100")
        running_hwm = index_value
        maximum_drawdown = Decimal("0")
        index_path: list[dict[str, Any]] = [
            {
                "as_of": normalized_intervals[0]["interval_start"],
                "index": index_value,
                "hwm": running_hwm,
                "drawdown": Decimal("0"),
            }
        ]
        calculated_periods: list[dict[str, Any]] = []
        for interval in normalized_intervals:
            interval_id = interval["interval_id"]
            selected_members = sorted(active_members_by_interval[interval_id])
            components = [
                component_by_key[(interval_id, member)] for member in selected_members
            ]
            total_capital = sum(
                (item["beginning_capital"] for item in components), Decimal("0")
            )
            if total_capital <= 0:
                raise HwmCoverageError("interval beginning capital must be positive")
            composite_return = (
                sum(
                    (
                        item["beginning_capital"] * item["period_return_decimal"]
                        for item in components
                    ),
                    Decimal("0"),
                )
                / total_capital
            )
            component_results = [
                {
                    "strategy_id": item["strategy_id"],
                    "beginning_capital_usd": item["beginning_capital"],
                    "weight": item["beginning_capital"] / total_capital,
                    "period_return": item["period_return_decimal"],
                }
                for item in components
            ]
            calculated_periods.append(
                {
                    "interval_id": interval_id,
                    "interval_start": interval["interval_start"],
                    "interval_end": interval["interval_end"],
                    "total_beginning_capital_usd": total_capital,
                    "composite_return": composite_return,
                    "components": component_results,
                }
            )
            index_value *= Decimal("1") + composite_return
            if index_value > running_hwm:
                running_hwm = index_value
            drawdown = index_value / running_hwm - Decimal("1")
            if drawdown < maximum_drawdown:
                maximum_drawdown = drawdown
            index_path.append(
                {
                    "as_of": interval["interval_end"],
                    "index": index_value,
                    "hwm": running_hwm,
                    "drawdown": drawdown,
                }
            )
        current_drawdown = index_value / running_hwm - Decimal("1")

    warning = age_days > maximum_age_days
    final_interval_id = normalized_intervals[-1]["interval_id"]
    complete_through_limiting_inputs = [
        {
            "strategy_id": item["strategy_id"],
            "interval_id": item["interval_id"],
            "source_generation_sha256": item["source_generation_sha256"],
            "capital_evidence_sha256": item["capital_evidence_sha256"],
            "flow_reconciliation_sha256": item["flow_reconciliation_sha256"],
            "return_evidence_sha256": item["return_evidence_sha256"],
            "leverage_evidence_sha256": item["leverage_evidence_sha256"],
        }
        for item in normalized_components
        if item["interval_id"] == final_interval_id
    ]
    result: dict[str, Any] = {
        "schema_version": 2,
        "status": _PORTABLE_PROOF_STATUS,
        "policy_id": AS_HELD_HWM_POLICY_ID,
        "policy_sha256": accepted_policy_sha256,
        "policy_semantic_digest": policy_semantic_digest,
        "calendar_sha256": accepted_calendar_sha256,
        "input_generation_sha256": accepted_generation_sha256,
        "input_authority_class": _PORTABLE_INPUT_AUTHORITY,
        "calendar_authority_status": calendar_authority_status,
        "input_sha256": input_sha256,
        "return_basis": AS_HELD_HWM_RETURN_BASIS,
        "assurance_profile": assurance_profile,
        "evaluated_on": evaluation_date.isoformat(),
        "complete_through": complete_date.isoformat(),
        "calculation_age_days": age_days,
        "maximum_age_days": maximum_age_days,
        "age_status": "COMPLETE_DATA_AGE_WARNING" if warning else "CURRENT",
        "warnings": ["HWM_COMPLETE_DATA_AGE"] if warning else [],
        "complete_through_limiting_inputs": complete_through_limiting_inputs,
        "index_base": Decimal("100"),
        "current_index": index_value,
        "current_hwm": running_hwm,
        "current_drawdown": current_drawdown,
        "maximum_drawdown": maximum_drawdown,
        "governor_drawdown_loss": -maximum_drawdown,
        "index_path": index_path,
        "periods": calculated_periods,
        "input_evidence": {
            "accepted_intervals": normalized_intervals,
            "memberships": normalized_memberships,
            "component_periods": normalized_components,
        },
    }

    result["receipt_sha256"] = _canonical_sha256(_json_safe(result))
    return result


def as_held_hwm_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return and verify the canonical JSON-safe PORT-HWM-4 receipt."""

    expected_keys = {
        "schema_version",
        "status",
        "policy_id",
        "policy_sha256",
        "policy_semantic_digest",
        "calendar_sha256",
        "input_generation_sha256",
        "input_authority_class",
        "calendar_authority_status",
        "input_sha256",
        "return_basis",
        "assurance_profile",
        "evaluated_on",
        "complete_through",
        "calculation_age_days",
        "maximum_age_days",
        "age_status",
        "warnings",
        "complete_through_limiting_inputs",
        "index_base",
        "current_index",
        "current_hwm",
        "current_drawdown",
        "maximum_drawdown",
        "governor_drawdown_loss",
        "index_path",
        "periods",
        "input_evidence",
        "receipt_sha256",
    }
    _strict_keys(result, expected_keys, "as-held HWM result")
    receipt = _json_safe(result)
    assert isinstance(receipt, dict)
    accepted_sha256 = _sha256(receipt["receipt_sha256"], "as-held HWM receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if _canonical_sha256(unsigned) != accepted_sha256:
        raise HwmCoverageError("as-held HWM receipt identity differs")
    _validate_as_held_hwm_receipt_semantics(receipt)
    return receipt


def _validate_as_held_hwm_receipt_semantics(receipt: Mapping[str, Any]) -> None:
    """Reject internally contradictory receipts even after a coherent rehash."""

    if (
        receipt["schema_version"] != 2
        or receipt["status"] != _PORTABLE_PROOF_STATUS
        or receipt["policy_id"] != AS_HELD_HWM_POLICY_ID
        or receipt["return_basis"] != AS_HELD_HWM_RETURN_BASIS
    ):
        raise HwmCoverageError("as-held HWM receipt identity is invalid")
    for field_name in {
        "policy_sha256",
        "policy_semantic_digest",
        "calendar_sha256",
        "input_generation_sha256",
        "input_sha256",
    }:
        _sha256(receipt[field_name], f"receipt.{field_name}")
    assurance_profile = _text(receipt["assurance_profile"], "receipt.assurance_profile")
    if assurance_profile != "DEVELOPMENT":
        raise HwmCoverageError("as-held HWM receipt assurance profile differs")
    if (
        receipt["input_authority_class"] != _PORTABLE_INPUT_AUTHORITY
        or receipt["calendar_authority_status"] != _PORTABLE_INPUT_AUTHORITY
    ):
        raise HwmCoverageError("as-held HWM receipt authority class differs")
    evaluated_on = _canonical_date(receipt["evaluated_on"], "receipt.evaluated_on")
    complete_through = _canonical_date(
        receipt["complete_through"], "receipt.complete_through"
    )
    if complete_through > evaluated_on:
        raise HwmCoverageError("as-held HWM receipt boundary differs")
    calculation_age_days = receipt["calculation_age_days"]
    maximum_age_days = receipt["maximum_age_days"]
    if (
        not isinstance(calculation_age_days, int)
        or isinstance(calculation_age_days, bool)
        or calculation_age_days < 0
        or not isinstance(maximum_age_days, int)
        or isinstance(maximum_age_days, bool)
        or maximum_age_days < 0
        or calculation_age_days != (evaluated_on - complete_through).days
    ):
        raise HwmCoverageError("as-held HWM receipt calculation age differs")
    warning = calculation_age_days > maximum_age_days
    if receipt["age_status"] != (
        "COMPLETE_DATA_AGE_WARNING" if warning else "CURRENT"
    ) or receipt["warnings"] != (["HWM_COMPLETE_DATA_AGE"] if warning else []):
        raise HwmCoverageError("as-held HWM receipt age disposition differs")

    def receipt_decimal(value: object, field_name: str) -> Decimal:
        accepted = _decimal(value, field_name)
        if not isinstance(value, str) or value != _decimal_text(accepted):
            raise HwmCoverageError(f"{field_name} must be a canonical decimal")
        return accepted

    index_base = receipt_decimal(receipt["index_base"], "receipt.index_base")
    if index_base != Decimal("100"):
        raise HwmCoverageError("as-held HWM receipt index base differs")
    path_values = receipt["index_path"]
    period_values = receipt["periods"]
    if (
        not isinstance(path_values, Sequence)
        or isinstance(path_values, str | bytes)
        or len(path_values) < 2
        or not isinstance(period_values, Sequence)
        or isinstance(period_values, str | bytes)
        or len(period_values) != len(path_values) - 1
    ):
        raise HwmCoverageError("as-held HWM receipt path length differs")

    normalized_path: list[dict[str, Any]] = []
    running_hwm = Decimal("0")
    for index, value in enumerate(path_values):
        point = _mapping(value, f"receipt.index_path[{index}]")
        _strict_keys(
            point,
            {"as_of", "index", "hwm", "drawdown"},
            f"receipt.index_path[{index}]",
        )
        as_of = _canonical_date(point["as_of"], f"receipt.index_path[{index}].as_of")
        index_value = receipt_decimal(
            point["index"], f"receipt.index_path[{index}].index"
        )
        hwm_value = receipt_decimal(point["hwm"], f"receipt.index_path[{index}].hwm")
        drawdown = receipt_decimal(
            point["drawdown"], f"receipt.index_path[{index}].drawdown"
        )
        if index_value <= 0:
            raise HwmCoverageError("as-held HWM receipt index must be positive")
        expected_hwm = index_value if index == 0 else max(running_hwm, index_value)
        with localcontext() as ctx:
            ctx.prec = 34
            ctx.rounding = ROUND_HALF_EVEN
            expected_drawdown = index_value / hwm_value - 1
        if hwm_value != expected_hwm or drawdown != expected_drawdown:
            raise HwmCoverageError("as-held HWM receipt path semantics differ")
        if drawdown > 0:
            raise HwmCoverageError("as-held HWM receipt drawdown sign differs")
        running_hwm = hwm_value
        normalized_path.append(
            {
                "as_of": as_of,
                "index": index_value,
                "hwm": hwm_value,
                "drawdown": drawdown,
            }
        )
    if (
        normalized_path[0]["index"] != Decimal("100")
        or normalized_path[0]["hwm"] != Decimal("100")
        or normalized_path[0]["drawdown"] != 0
        or normalized_path[-1]["as_of"] != complete_through
    ):
        raise HwmCoverageError("as-held HWM receipt path boundary differs")

    for index, value in enumerate(period_values):
        period = _mapping(value, f"receipt.periods[{index}]")
        _strict_keys(
            period,
            {
                "interval_id",
                "interval_start",
                "interval_end",
                "total_beginning_capital_usd",
                "composite_return",
                "components",
            },
            f"receipt.periods[{index}]",
        )
        _text(period["interval_id"], f"receipt.periods[{index}].interval_id")
        start = _canonical_date(
            period["interval_start"], f"receipt.periods[{index}].interval_start"
        )
        end = _canonical_date(
            period["interval_end"], f"receipt.periods[{index}].interval_end"
        )
        if (
            start != normalized_path[index]["as_of"]
            or end != normalized_path[index + 1]["as_of"]
        ):
            raise HwmCoverageError("as-held HWM receipt period boundary differs")
        total_capital = receipt_decimal(
            period["total_beginning_capital_usd"],
            f"receipt.periods[{index}].total_beginning_capital_usd",
        )
        composite_return = receipt_decimal(
            period["composite_return"],
            f"receipt.periods[{index}].composite_return",
        )
        components = period["components"]
        if (
            total_capital <= 0
            or composite_return <= -1
            or not isinstance(components, Sequence)
            or isinstance(components, str | bytes)
            or not components
        ):
            raise HwmCoverageError("as-held HWM receipt period semantics differ")
        component_capital = Decimal("0")
        weighted_return = Decimal("0")
        strategy_ids: set[str] = set()
        for component_index, component_value in enumerate(components):
            component = _mapping(
                component_value,
                f"receipt.periods[{index}].components[{component_index}]",
            )
            _strict_keys(
                component,
                {"strategy_id", "beginning_capital_usd", "weight", "period_return"},
                f"receipt.periods[{index}].components[{component_index}]",
            )
            strategy_id = _text(component["strategy_id"], "receipt component strategy")
            if strategy_id in strategy_ids:
                raise HwmCoverageError("as-held HWM receipt component is duplicated")
            strategy_ids.add(strategy_id)
            capital = receipt_decimal(
                component["beginning_capital_usd"], "receipt component capital"
            )
            weight = receipt_decimal(component["weight"], "receipt component weight")
            accepted_return = receipt_decimal(
                component["period_return"], "receipt component return"
            )
            if capital <= 0 or accepted_return <= -1:
                raise HwmCoverageError("as-held HWM receipt component semantics differ")
            component_capital += capital
            weighted_return += capital * accepted_return
            with localcontext() as ctx:
                ctx.prec = 34
                ctx.rounding = ROUND_HALF_EVEN
                expected_weight = capital / total_capital
            if weight != expected_weight:
                raise HwmCoverageError("as-held HWM receipt component weight differs")
        with localcontext() as ctx:
            ctx.prec = 34
            ctx.rounding = ROUND_HALF_EVEN
            expected_composite_return = weighted_return / total_capital
            expected_next_index = normalized_path[index]["index"] * (
                1 + composite_return
            )
        if (
            component_capital != total_capital
            or expected_composite_return != composite_return
            or expected_next_index != normalized_path[index + 1]["index"]
        ):
            raise HwmCoverageError("as-held HWM receipt period calculation differs")

    current_index = receipt_decimal(receipt["current_index"], "receipt.current_index")
    current_hwm = receipt_decimal(receipt["current_hwm"], "receipt.current_hwm")
    current_drawdown = receipt_decimal(
        receipt["current_drawdown"], "receipt.current_drawdown"
    )
    maximum_drawdown = receipt_decimal(
        receipt["maximum_drawdown"], "receipt.maximum_drawdown"
    )
    governor_loss = receipt_decimal(
        receipt["governor_drawdown_loss"], "receipt.governor_drawdown_loss"
    )
    if (
        current_index != normalized_path[-1]["index"]
        or current_hwm != normalized_path[-1]["hwm"]
        or current_drawdown != normalized_path[-1]["drawdown"]
        or maximum_drawdown != min(point["drawdown"] for point in normalized_path)
        or governor_loss != -maximum_drawdown
        or maximum_drawdown > 0
    ):
        raise HwmCoverageError("as-held HWM receipt summary differs")

    evidence = _mapping(receipt["input_evidence"], "receipt.input_evidence")
    _strict_keys(
        evidence,
        {"accepted_intervals", "memberships", "component_periods"},
        "receipt.input_evidence",
    )
    normalized_input = {
        "policy_id": receipt["policy_id"],
        "policy_sha256": receipt["policy_sha256"],
        "policy_semantic_digest": receipt["policy_semantic_digest"],
        "calendar_sha256": receipt["calendar_sha256"],
        "input_generation_sha256": receipt["input_generation_sha256"],
        "input_authority_class": receipt["input_authority_class"],
        "calendar_authority_status": receipt["calendar_authority_status"],
        "return_basis": receipt["return_basis"],
        "evaluated_on": receipt["evaluated_on"],
        "complete_through": receipt["complete_through"],
        "maximum_age_days": maximum_age_days,
        "assurance_profile": assurance_profile,
        **evidence,
    }
    if _canonical_sha256(normalized_input) != receipt["input_sha256"]:
        raise HwmCoverageError("as-held HWM receipt input identity differs")

    limiting_values = receipt["complete_through_limiting_inputs"]
    components = evidence["component_periods"]
    if (
        not isinstance(limiting_values, list)
        or not limiting_values
        or not isinstance(components, list)
    ):
        raise HwmCoverageError("as-held HWM receipt limiting inputs differ")
    expected_limiting = [
        {
            "strategy_id": component["strategy_id"],
            "interval_id": component["interval_id"],
            "source_generation_sha256": component["source_generation_sha256"],
            "capital_evidence_sha256": component["capital_evidence_sha256"],
            "flow_reconciliation_sha256": component["flow_reconciliation_sha256"],
            "return_evidence_sha256": component["return_evidence_sha256"],
            "leverage_evidence_sha256": component["leverage_evidence_sha256"],
        }
        for component_value in components
        for component in [_mapping(component_value, "receipt input component")]
        if component["interval_end"] == receipt["complete_through"]
    ]
    if limiting_values != expected_limiting:
        raise HwmCoverageError("as-held HWM receipt limiting inputs differ")
    for index, value in enumerate(limiting_values):
        limiting = _mapping(value, f"receipt limiting input {index}")
        _strict_keys(
            limiting,
            {
                "strategy_id",
                "interval_id",
                "source_generation_sha256",
                "capital_evidence_sha256",
                "flow_reconciliation_sha256",
                "return_evidence_sha256",
                "leverage_evidence_sha256",
            },
            f"receipt limiting input {index}",
        )
        _text(limiting["strategy_id"], "receipt limiting strategy")
        _text(limiting["interval_id"], "receipt limiting interval")
        for field_name in set(limiting) - {"strategy_id", "interval_id"}:
            _sha256(limiting[field_name], f"receipt limiting input {field_name}")


def as_held_hwm_receipt_bytes(result: Mapping[str, Any]) -> bytes:
    """Serialize a verified PORT-HWM-4 receipt to deterministic JSON bytes."""

    receipt = as_held_hwm_receipt(result)
    return (
        json.dumps(
            receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def verify_as_held_hwm_receipt(
    receipt: Mapping[str, Any],
    *,
    policy_bytes: bytes,
    product_contract_bytes: bytes,
    performance_policy_bytes: bytes,
    legacy_policy_bytes: bytes,
    calendar_bytes: bytes,
    input_generation_bytes: bytes,
) -> None:
    """Re-derive a PORT-HWM-4 receipt from its hash-bound input evidence."""

    accepted = as_held_hwm_receipt(receipt)
    evidence = _mapping(accepted["input_evidence"], "receipt.input_evidence")
    _strict_keys(
        evidence,
        {"accepted_intervals", "memberships", "component_periods"},
        "receipt.input_evidence",
    )
    intervals = evidence["accepted_intervals"]
    memberships = evidence["memberships"]
    components = evidence["component_periods"]
    if (
        not isinstance(intervals, Sequence)
        or isinstance(intervals, str | bytes)
        or not isinstance(memberships, Sequence)
        or isinstance(memberships, str | bytes)
        or not isinstance(components, Sequence)
        or isinstance(components, str | bytes)
    ):
        raise HwmCoverageError("as-held HWM receipt input evidence differs")
    maximum_age_days = accepted["maximum_age_days"]
    if not isinstance(maximum_age_days, int) or isinstance(maximum_age_days, bool):
        raise HwmCoverageError("as-held HWM receipt maximum age differs")
    recalculated = compute_as_held_composite_hwm(
        intervals,
        memberships,
        components,
        policy_bytes=policy_bytes,
        product_contract_bytes=product_contract_bytes,
        performance_policy_bytes=performance_policy_bytes,
        legacy_policy_bytes=legacy_policy_bytes,
        calendar_bytes=calendar_bytes,
        input_generation_bytes=input_generation_bytes,
        evaluated_on=_text(accepted["evaluated_on"], "receipt.evaluated_on"),
        complete_through=_text(
            accepted["complete_through"], "receipt.complete_through"
        ),
        maximum_age_days=maximum_age_days,
        assurance_profile=_text(
            accepted["assurance_profile"], "receipt.assurance_profile"
        ),
    )
    if as_held_hwm_receipt(recalculated) != accepted:
        raise HwmCoverageError("as-held HWM receipt re-derivation differs")

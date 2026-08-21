from __future__ import annotations

from copy import deepcopy
from datetime import date as date_type
import hashlib
import json
from pathlib import Path
from collections.abc import Callable

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from decimal import Decimal

from quant_engine.portfolio_ledger import (
    AS_HELD_HWM_DETAILED_COMPONENT_MODE,
    AS_HELD_HWM_DIRECT_NET_COMPONENT_MODE,
    HwmCoverageError,
    as_held_hwm_receipt,
    as_held_hwm_receipt_bytes,
    compute_as_held_composite_hwm,
    compute_full_systematic_hwm_index,
    governed_hwm_coverage,
    validate_as_held_hwm_policy,
    validate_as_held_hwm_receipt_generation_binding,
    verify_as_held_hwm_receipt,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_as_held_hwm_component_modes_are_public_wire_constants() -> None:
    assert AS_HELD_HWM_DETAILED_COMPONENT_MODE == "DETAILED_RECONCILED_LEVERAGE"
    assert (
        AS_HELD_HWM_DIRECT_NET_COMPONENT_MODE == "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN"
    )


_PORT_HWM4_POLICY_PATH = _REPO_ROOT / "contracts" / "portfolio-hwm-policy-v4.json"
_PORT_HWM4_POLICY_BYTES = _PORT_HWM4_POLICY_PATH.read_bytes()
_PORT_HWM4_POLICY = json.loads(_PORT_HWM4_POLICY_BYTES)
_PORT_HWM4_POLICY_SHA256 = hashlib.sha256(_PORT_HWM4_POLICY_BYTES).hexdigest()
_PRODUCT_CONTRACT_BYTES = (
    _REPO_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
).read_bytes()
_PERFORMANCE_POLICY_BYTES = (
    _REPO_ROOT / "contracts" / "portfolio-performance-policy-v1.json"
).read_bytes()
_LEGACY_HWM_POLICY_BYTES = (
    _REPO_ROOT / "contracts" / "portfolio-hwm-policy-v3.json"
).read_bytes()


def _policy() -> dict[str, object]:
    return {
        "policy_id": "PORT-HWM-3",
        "accepted_daily_nav_evidence": ["ACCOUNT_LIVE", "PROVIDER_LIVE"],
        "accepted_provider_live_strategy_ids": ["strategy-provider"],
        "excluded_strategy_ids": ["strategy-tiny"],
        "material_perimeter": {
            "default": "INCLUDED",
            "exclusions": [
                {
                    "classification": "EXCLUDED_DE_MINIMIS",
                    "effective_date": "2026-08-01",
                    "reason": "Accepted de-minimis test exclusion.",
                    "strategy_id": "strategy-tiny",
                }
            ],
        },
    }


def _strategies() -> list[dict[str, object]]:
    return [
        {
            "current_value_usd": "90",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_status": "AVAILABLE",
            "strategy_id": "strategy-live",
            "strategy_name": "Live",
            "systematic_weight": "0.9",
        },
        {
            "current_value_usd": "10",
            "nav_evidence_layer": None,
            "nav_status": "UNAVAILABLE",
            "strategy_id": "strategy-tiny",
            "strategy_name": "Tiny",
            "systematic_weight": "0.1",
        },
    ]


def test_governed_hwm_excludes_de_minimis_strategy_and_requires_freshness() -> None:
    strategies, summary = governed_hwm_coverage(
        _strategies(),
        _policy(),
        [
            {
                "blocks_current_consumers": False,
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "required_for_current": True,
                "state": "FRESH",
                "subject_id": "strategy-live",
                "subject_type": "STRATEGY",
            }
        ],
        require_freshness=True,
    )

    assert summary == {
        "covered_value_usd": "90",
        "excluded_details": [
            {
                "classification": "EXCLUDED_DE_MINIMIS",
                "current_value_usd": "10",
                "effective_date": "2026-08-01",
                "reason": "Accepted de-minimis test exclusion.",
                "strategy_id": "strategy-tiny",
                "strategy_name": "Tiny",
            }
        ],
        "excluded_value_usd": "10",
        "excluded_strategies": ["Tiny"],
        "governed_perimeter_value_usd": "90",
        "nav_coverage": "1",
        "policy_id": "PORT-HWM-3",
        "status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
        "status_code_version": 2,
        "status_semantics": "EXPLICIT_GOVERNED_HWM_PERIMETER",
        "uncovered_details": [],
        "uncovered_strategies": [],
        "uncovered_value_usd": "0",
    }
    assert strategies[0]["hwm_freshness_status"] == "ACCEPTED"
    assert strategies[1]["hwm_scope"] == "EXCLUDED_DE_MINIMIS"
    assert strategies[1]["hwm_exclusion_reason"] == (
        "Accepted de-minimis test exclusion."
    )


def test_governed_hwm_fails_closed_when_required_freshness_is_missing() -> None:
    _strategies_result, summary = governed_hwm_coverage(
        _strategies(), _policy(), [], require_freshness=True
    )

    assert summary["status"] == (
        "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV"
    )
    assert summary["uncovered_details"][0]["qualification_gap"] == (
        "FRESHNESS_UNAVAILABLE"
    )
    assert summary["uncovered_value_usd"] == "90"
    assert (
        summary["covered_value_usd"] == "0"
        and summary["governed_perimeter_value_usd"] == "90"
    )


def test_provider_live_uses_provider_lane_and_stale_blocks() -> None:
    strategies = [
        {
            "current_value_usd": "25.19",
            "nav_evidence_layer": "PROVIDER_LIVE",
            "nav_status": "AVAILABLE",
            "strategy_id": "strategy-provider",
            "strategy_name": "Provider",
            "systematic_weight": "1",
        }
    ]
    policy = _policy()
    policy["excluded_strategy_ids"] = []
    policy["material_perimeter"] = {"default": "INCLUDED", "exclusions": []}
    _rows, summary = governed_hwm_coverage(
        strategies,
        policy,
        [
            {
                "blocks_current_consumers": False,
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "required_for_current": True,
                "state": "FRESH",
                "subject_id": "strategy-provider",
                "subject_type": "STRATEGY",
            },
            {
                "blocks_current_consumers": False,
                "fact_type": "strategy-returns",
                "lane": "PROVIDER",
                "last_accepted_date": "2026-07-30",
                "reason": "provider lane is stale",
                "required_for_current": False,
                "state": "STALE",
                "subject_id": "strategy-provider",
                "subject_type": "STRATEGY",
            },
        ],
        require_freshness=True,
    )

    assert summary["covered_value_usd"] == "0"
    assert summary["uncovered_value_usd"] == "25.19"
    assert summary["uncovered_details"][0]["freshness_lane"] == "PROVIDER"
    assert summary["uncovered_details"][0]["freshness_state"] == "STALE"


def test_freshness_cannot_advance_beyond_the_accepted_hwm_series() -> None:
    strategies = [
        {
            "current_value_usd": "50.17",
            "nav_evidence_layer": "ACCOUNT_LIVE",
            "nav_observed_end": "2026-07-17",
            "nav_status": "AVAILABLE",
            "strategy_id": "strategy-live",
            "strategy_name": "Live",
            "systematic_weight": "1",
        }
    ]
    policy = _policy()
    policy["excluded_strategy_ids"] = []
    policy["material_perimeter"] = {"default": "INCLUDED", "exclusions": []}
    rows, summary = governed_hwm_coverage(
        strategies,
        policy,
        [
            {
                "blocks_current_consumers": False,
                "fact_type": "strategy-returns",
                "lane": "ACCOUNT",
                "last_accepted_date": "2026-07-30",
                "reason": "newer raw evidence exists",
                "required_for_current": True,
                "state": "FRESH",
                "subject_id": "strategy-live",
                "subject_type": "STRATEGY",
            }
        ],
        require_freshness=True,
    )

    assert summary["covered_value_usd"] == "0"
    assert summary["uncovered_value_usd"] == "50.17"
    assert summary["uncovered_details"][0]["qualification_gap"] == (
        "FRESHNESS_EVIDENCE_BOUNDARY_MISMATCH"
    )
    assert summary["uncovered_details"][0]["hwm_evidence_observed_end"] == (
        "2026-07-17"
    )
    assert rows[0]["hwm_freshness_status"] == "BOUNDARY_MISMATCH"
    assert rows[0]["hwm_freshness_state"] == "FRESH"


def test_governed_hwm_rejects_unknown_policy_exclusion() -> None:
    policy = _policy()
    policy["excluded_strategy_ids"] = ["unknown"]

    with pytest.raises(HwmCoverageError, match="unknown strategy"):
        governed_hwm_coverage(_strategies(), policy)


# ---------------------------------------------------------------------------
# compute_full_systematic_hwm_index
# ---------------------------------------------------------------------------


def test_complete_hwm_index_reproduces_conformance_vector() -> None:
    """Exact reproduction of the normative complete_hwm_index case.

    Index path: 1.0 -> 1.10 -> 1.045 -> 0.9405
    HWM = 1.10, drawdown = 1 - 0.9405 / 1.10 = 0.145
    """
    daily_returns = [Decimal("0.10"), Decimal("-0.05"), Decimal("-0.10")]
    result = compute_full_systematic_hwm_index(
        daily_returns, full_systematic_coverage=True
    )

    assert result["status"] == "AVAILABLE"
    assert result["index_series"] == [
        Decimal("1.0"),
        Decimal("1.10"),
        Decimal("1.045"),
        Decimal("0.9405"),
    ]
    assert result["hwm"] == Decimal("1.10")
    assert result["current_drawdown"] == Decimal("0.145")
    assert result["maximum_drawdown"] == Decimal("0.145")


def test_missing_full_hwm_fails_closed() -> None:
    """Incomplete coverage yields UNAVAILABLE with no numeric output."""
    result = compute_full_systematic_hwm_index(
        [Decimal("0.10")], full_systematic_coverage=False
    )

    assert result == {"status": "UNAVAILABLE"}


def test_empty_series_is_unavailable() -> None:
    """An empty daily-returns sequence yields UNAVAILABLE."""
    result = compute_full_systematic_hwm_index([], full_systematic_coverage=True)

    assert result == {"status": "UNAVAILABLE"}


def test_single_day_edge() -> None:
    """A single daily return produces a valid two-point index series."""
    result = compute_full_systematic_hwm_index(
        [Decimal("0.02")], full_systematic_coverage=True
    )

    assert result["status"] == "AVAILABLE"
    assert result["index_series"] == [Decimal("1.0"), Decimal("1.02")]
    assert result["hwm"] == Decimal("1.02")
    assert result["current_drawdown"] == Decimal("0")
    assert result["maximum_drawdown"] == Decimal("0")


def test_all_gains_drawdown_zero() -> None:
    """When the index never retreats, drawdown is exactly 0."""
    daily_returns = [Decimal("0.03"), Decimal("0.015")]
    result = compute_full_systematic_hwm_index(
        daily_returns, full_systematic_coverage=True
    )

    assert result["status"] == "AVAILABLE"
    assert result["hwm"] == result["index_series"][-1]
    assert result["current_drawdown"] == Decimal("0")
    assert result["maximum_drawdown"] == Decimal("0")


def test_hwm_uses_decimal_exclusively() -> None:
    """Every numeric field in the result is Decimal — no float anywhere."""
    daily_returns = [
        Decimal("0.01"),
        Decimal("-0.02"),
        Decimal("0.005"),
    ]
    result = compute_full_systematic_hwm_index(
        daily_returns, full_systematic_coverage=True
    )

    assert isinstance(result["hwm"], Decimal)
    assert isinstance(result["current_drawdown"], Decimal)
    assert isinstance(result["maximum_drawdown"], Decimal)
    assert all(isinstance(v, Decimal) for v in result["index_series"])


def test_hwm_index_is_context_independent() -> None:
    """Identical inputs produce bit-exact outputs regardless of the
    caller's decimal context precision or rounding settings."""
    from decimal import getcontext, ROUND_DOWN, ROUND_UP

    daily_returns = [Decimal("0.10"), Decimal("-0.05"), Decimal("-0.10")]

    # Run once with default context → capture reference.
    ref = compute_full_systematic_hwm_index(
        daily_returns, full_systematic_coverage=True
    )

    # Change global context and run again — must still match exactly.
    saved_prec = getcontext().prec
    saved_rounding = getcontext().rounding
    try:
        getcontext().prec = 8
        getcontext().rounding = ROUND_DOWN
        polluted = compute_full_systematic_hwm_index(
            daily_returns, full_systematic_coverage=True
        )
    finally:
        getcontext().prec = saved_prec
        getcontext().rounding = saved_rounding

    assert ref == polluted
    assert polluted["maximum_drawdown"] == Decimal("0.145")

    # Non-terminating divisions diverge under caller rounding modes unless the
    # function pins its own (review probe: forty Decimal("0.01") returns).
    probe_returns = [Decimal("0.01")] * 40
    ref_probe = compute_full_systematic_hwm_index(
        probe_returns, full_systematic_coverage=True
    )
    try:
        getcontext().prec = 28
        getcontext().rounding = ROUND_UP
        up_probe = compute_full_systematic_hwm_index(
            probe_returns, full_systematic_coverage=True
        )
        getcontext().rounding = ROUND_DOWN
        down_probe = compute_full_systematic_hwm_index(
            probe_returns, full_systematic_coverage=True
        )
    finally:
        getcontext().prec = saved_prec
        getcontext().rounding = saved_rounding

    assert ref_probe == up_probe == down_probe

    # Rounding-up context must also match.
    try:
        getcontext().prec = 5
        getcontext().rounding = ROUND_UP
        up_result = compute_full_systematic_hwm_index(
            daily_returns, full_systematic_coverage=True
        )
    finally:
        getcontext().prec = saved_prec
        getcontext().rounding = saved_rounding

    assert ref == up_result


# ---------------------------------------------------------------------------
# compute_as_held_composite_hwm / PORT-HWM-4
# ---------------------------------------------------------------------------


def _fixture_decimal_text(value: object) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _fixture_sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _refresh_receipt_self_hashes(receipt: dict[str, object]) -> None:
    evidence = receipt["input_evidence"]
    assert isinstance(evidence, dict)
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
        "maximum_age_days": receipt["maximum_age_days"],
        "assurance_profile": receipt["assurance_profile"],
        **evidence,
    }
    receipt["input_sha256"] = _fixture_sha256(normalized_input)
    receipt["receipt_sha256"] = _fixture_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def _bind_receipt_to_generation(
    receipt: dict[str, object], generation: dict[str, object]
) -> bytes:
    generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    generation_sha256 = hashlib.sha256(generation_bytes).hexdigest()
    receipt["input_generation_sha256"] = generation_sha256
    evidence = receipt["input_evidence"]
    assert isinstance(evidence, dict)
    components = evidence["component_periods"]
    assert isinstance(components, list)
    for component in components:
        assert isinstance(component, dict)
        identity_field = (
            "source_generation_sha256"
            if receipt["schema_version"] == 2
            else "portable_input_generation_sha256"
        )
        component[identity_field] = generation_sha256
    limiting_inputs = receipt["complete_through_limiting_inputs"]
    assert isinstance(limiting_inputs, list)
    for limiting_input in limiting_inputs:
        assert isinstance(limiting_input, dict)
        identity_field = (
            "source_generation_sha256"
            if receipt["schema_version"] == 2
            else "portable_input_generation_sha256"
        )
        limiting_input[identity_field] = generation_sha256
    _refresh_receipt_self_hashes(receipt)
    return generation_bytes


def _generation_binding_kwargs(fixture: dict[str, object]) -> dict[str, bytes]:
    return {
        "product_contract_bytes": fixture["product_contract_bytes"],  # type: ignore[dict-item]
        "performance_policy_bytes": fixture["performance_policy_bytes"],  # type: ignore[dict-item]
        "legacy_policy_bytes": fixture["legacy_policy_bytes"],  # type: ignore[dict-item]
        "calendar_bytes": fixture["calendar_bytes"],  # type: ignore[dict-item]
    }


def _refresh_membership_evidence(row: dict[str, object]) -> None:
    row["membership_evidence_sha256"] = _fixture_sha256(
        {
            "schema_version": 1,
            "evidence_type": "FUNDED_MEMBERSHIP",
            "strategy_id": str(row["strategy_id"]),
            "effective_start": str(row["effective_start"]),
            "effective_end": row["effective_end"],
        }
    )


_IDENTITY_USD_SHA256 = hashlib.sha256(b"IDENTITY_USD").hexdigest()


def _refresh_component_evidence(
    row: dict[str, object],
    flow_events_by_subperiod: dict[tuple[str, str, str], list[dict[str, object]]]
    | None = None,
) -> None:
    normalized_subperiods: list[dict[str, object]] = []
    for subperiod in row["subperiods"]:  # type: ignore[union-attr]
        normalized_without_evidence = {
            "subperiod_id": str(subperiod["subperiod_id"]),
            "subperiod_start_utc": str(subperiod["subperiod_start_utc"]),
            "subperiod_end_utc": str(subperiod["subperiod_end_utc"]),
            "beginning_nav_usd": _fixture_decimal_text(subperiod["beginning_nav_usd"]),
            "ending_nav_before_flow_usd": _fixture_decimal_text(
                subperiod["ending_nav_before_flow_usd"]
            ),
            "beginning_gross_assets_usd": _fixture_decimal_text(
                subperiod["beginning_gross_assets_usd"]
            ),
            "beginning_financing_liability_usd": _fixture_decimal_text(
                subperiod["beginning_financing_liability_usd"]
            ),
            "ending_gross_assets_before_flow_usd": _fixture_decimal_text(
                subperiod["ending_gross_assets_before_flow_usd"]
            ),
            "ending_financing_liability_before_flow_usd": _fixture_decimal_text(
                subperiod["ending_financing_liability_before_flow_usd"]
            ),
            "margin_principal_flow_after_subperiod_usd": _fixture_decimal_text(
                subperiod["margin_principal_flow_after_subperiod_usd"]
            ),
            "financing_cost_usd": _fixture_decimal_text(
                subperiod["financing_cost_usd"]
            ),
            "external_flow_after_subperiod_usd": _fixture_decimal_text(
                subperiod["external_flow_after_subperiod_usd"]
            ),
            "internal_transfer_after_subperiod_usd": _fixture_decimal_text(
                subperiod["internal_transfer_after_subperiod_usd"]
            ),
        }
        if flow_events_by_subperiod is not None:
            events = flow_events_by_subperiod.get(
                (
                    str(row["strategy_id"]),
                    str(row["interval_id"]),
                    str(subperiod["subperiod_id"]),
                ),
                [],
            )
        else:
            events = []
        flow_sha256 = _fixture_sha256(
            {
                "schema_version": 2,
                "evidence_type": "FLOW_EVENT",
                "strategy_id": str(row["strategy_id"]),
                "interval_id": str(row["interval_id"]),
                "subperiod": {
                    **normalized_without_evidence,
                    "flow_event_ids": [str(event["event_id"]) for event in events],
                    "flow_event_sha256s": [
                        str(event["event_evidence_sha256"]) for event in events
                    ],
                },
            }
        )
        subperiod["flow_event_evidence_sha256"] = flow_sha256
        normalized_subperiods.append(
            {**normalized_without_evidence, "flow_event_evidence_sha256": flow_sha256}
        )

    common = {
        "schema_version": 1,
        "strategy_id": str(row["strategy_id"]),
        "interval_id": str(row["interval_id"]),
    }
    row["capital_evidence_sha256"] = _fixture_sha256(
        {
            **common,
            "evidence_type": "BEGINNING_CAPITAL",
            "beginning_capital_usd": _fixture_decimal_text(
                row["beginning_capital_usd"]
            ),
            "capital_basis": str(row["capital_basis"]),
        }
    )
    row["flow_reconciliation_sha256"] = _fixture_sha256(
        {
            **common,
            "evidence_type": "FLOW_RECONCILIATION",
            "flow_timing": str(row["flow_timing"]),
            "external_flow_usd": _fixture_decimal_text(row["external_flow_usd"]),
            "internal_transfer_usd": _fixture_decimal_text(
                row["internal_transfer_usd"]
            ),
            "margin_principal_flow_usd": _fixture_decimal_text(
                row["margin_principal_flow_usd"]
            ),
            "subperiod_flow_evidence_sha256s": [
                item["flow_event_evidence_sha256"] for item in normalized_subperiods
            ],
        }
    )
    row["return_evidence_sha256"] = _fixture_sha256(
        {
            **common,
            "evidence_type": "LEVERED_NET_RETURN",
            "beginning_capital_usd": _fixture_decimal_text(
                row["beginning_capital_usd"]
            ),
            "ending_nav_usd": _fixture_decimal_text(row["ending_nav_usd"]),
            "flow_adjusted_ending_nav_usd": _fixture_decimal_text(
                row["flow_adjusted_ending_nav_usd"]
            ),
            "period_return": _fixture_decimal_text(row["period_return"]),
            "subperiods": normalized_subperiods,
        }
    )
    row["leverage_evidence_sha256"] = _fixture_sha256(
        {
            **common,
            "evidence_type": "LEVERAGE_RECONCILIATION",
            "beginning_gross_assets_usd": _fixture_decimal_text(
                row["beginning_gross_assets_usd"]
            ),
            "beginning_financing_liability_usd": _fixture_decimal_text(
                row["beginning_financing_liability_usd"]
            ),
            "ending_gross_assets_usd": _fixture_decimal_text(
                row["ending_gross_assets_usd"]
            ),
            "ending_financing_liability_usd": _fixture_decimal_text(
                row["ending_financing_liability_usd"]
            ),
            "margin_principal_flow_usd": _fixture_decimal_text(
                row["margin_principal_flow_usd"]
            ),
            "financing_cost_usd": _fixture_decimal_text(row["financing_cost_usd"]),
            "gross_asset_return": _fixture_decimal_text(row["gross_asset_return"]),
            "subperiods": normalized_subperiods,
        }
    )


def _as_held_component(
    strategy_id: str,
    interval_id: str,
    interval_start: str,
    interval_end: str,
    beginning_capital: str,
    period_return: str,
    *,
    financing_liability: str = "0",
    beginning_financing_liability: str | None = None,
    margin_principal_flow: str = "0",
    financing_cost: str = "0",
    external_flow: str = "0",
    internal_transfer: str = "0",
    capital_basis: str = "OBSERVED_INDEPENDENT",
) -> dict[str, object]:
    beginning = Decimal(beginning_capital)
    accepted_return = Decimal(period_return)
    flow = Decimal(external_flow)
    transfer = Decimal(internal_transfer)
    adjusted_ending_nav = beginning * (Decimal("1") + accepted_return)
    ending_nav = adjusted_ending_nav + flow + transfer
    liability = Decimal(financing_liability)
    beginning_liability = Decimal(
        financing_liability
        if beginning_financing_liability is None
        else beginning_financing_liability
    )
    principal_flow = Decimal(margin_principal_flow)
    gross_assets = ending_nav + liability
    ending_gross_before_flow = gross_assets - flow - transfer - principal_flow
    gross_asset_return = (ending_gross_before_flow + Decimal(financing_cost)) / (
        beginning + beginning_liability
    ) - Decimal("1")
    hash_character = str((sum(ord(character) for character in strategy_id) % 6) + 1)
    row: dict[str, object] = {
        "strategy_id": strategy_id,
        "interval_id": interval_id,
        "interval_start": interval_start,
        "interval_end": interval_end,
        "beginning_capital_usd": format(beginning, "f"),
        "beginning_gross_assets_usd": format(beginning + beginning_liability, "f"),
        "beginning_financing_liability_usd": format(beginning_liability, "f"),
        "ending_gross_assets_usd": format(gross_assets, "f"),
        "ending_financing_liability_usd": format(liability, "f"),
        "margin_principal_flow_usd": format(principal_flow, "f"),
        "financing_cost_usd": financing_cost,
        "gross_asset_return": format(gross_asset_return, "f"),
        "ending_nav_usd": format(ending_nav, "f"),
        "external_flow_usd": format(flow, "f"),
        "internal_transfer_usd": format(transfer, "f"),
        "flow_adjusted_ending_nav_usd": format(adjusted_ending_nav, "f"),
        "period_return": format(accepted_return, "f"),
        "flow_timing": (
            "NO_EXTERNAL_OR_INTERNAL_FLOW"
            if flow == 0 and transfer == 0
            else "DATE_ONLY_END_OF_DAY"
        ),
        "subperiods": [
            {
                "subperiod_id": f"{interval_id}-close",
                "subperiod_start_utc": f"{interval_start}T00:00:00Z",
                "subperiod_end_utc": f"{interval_end}T00:00:00Z",
                "beginning_nav_usd": format(beginning, "f"),
                "ending_nav_before_flow_usd": format(adjusted_ending_nav, "f"),
                "beginning_gross_assets_usd": format(
                    beginning + beginning_liability, "f"
                ),
                "beginning_financing_liability_usd": format(beginning_liability, "f"),
                "ending_gross_assets_before_flow_usd": format(
                    ending_gross_before_flow, "f"
                ),
                "ending_financing_liability_before_flow_usd": format(
                    beginning_liability, "f"
                ),
                "margin_principal_flow_after_subperiod_usd": format(
                    principal_flow, "f"
                ),
                "financing_cost_usd": financing_cost,
                "external_flow_after_subperiod_usd": format(flow, "f"),
                "internal_transfer_after_subperiod_usd": format(transfer, "f"),
                "flow_event_evidence_sha256": "7" * 64,
            }
        ],
        "capital_basis": capital_basis,
        "capital_evidence_sha256": hash_character * 64,
        "flow_reconciliation_sha256": "7" * 64,
        "return_evidence_sha256": "8" * 64,
        "leverage_evidence_sha256": "6" * 64,
        "source_generation_sha256": "0" * 64,
        "policy_sha256": _PORT_HWM4_POLICY_SHA256,
        "calendar_sha256": "0" * 64,
        "return_basis": "LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL",
    }
    _refresh_component_evidence(row)
    return row


def _fixture_flow_events(
    periods: list[dict[str, object]],
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    product_sha256 = hashlib.sha256(_PRODUCT_CONTRACT_BYTES).hexdigest()
    for period in periods:
        for subperiod in period["subperiods"]:  # type: ignore[union-attr]
            definitions = [
                (
                    "external_flow_after_subperiod_usd",
                    "OWNER_CONTRIBUTION"
                    if Decimal(str(subperiod["external_flow_after_subperiod_usd"])) > 0
                    else "OWNER_WITHDRAWAL",
                    "EXTERNAL_FLOW",
                ),
                (
                    "internal_transfer_after_subperiod_usd",
                    "INTERNAL_TRANSFER",
                    "INTERNAL_TRANSFER",
                ),
                (
                    "margin_principal_flow_after_subperiod_usd",
                    "MARGIN_BORROW"
                    if Decimal(
                        str(subperiod["margin_principal_flow_after_subperiod_usd"])
                    )
                    > 0
                    else "MARGIN_REPAYMENT",
                    "MARGIN_PRINCIPAL",
                ),
                ("financing_cost_usd", "FINANCING_COST", "FINANCING_COST"),
            ]
            for field_name, event_type, treatment in definitions:
                amount = Decimal(str(subperiod[field_name]))
                if amount == 0:
                    continue
                event_without_evidence: dict[str, object] = {
                    "event_id": (
                        f"{period['strategy_id']}:{period['interval_id']}:"
                        f"{subperiod['subperiod_id']}:{event_type}"
                    ),
                    "strategy_id": str(period["strategy_id"]),
                    "interval_id": str(period["interval_id"]),
                    "subperiod_id": str(subperiod["subperiod_id"]),
                    "event_type": event_type,
                    "capital_treatment": treatment,
                    "effective_at_utc": str(subperiod["subperiod_end_utc"]),
                    "effective_timezone": "UTC",
                    "timing_basis": (
                        "EXACT_TIMESTAMP"
                        if period["flow_timing"] == "EXACT_TIMESTAMP_SEGMENTED"
                        else "DATE_ONLY_END_OF_DAY"
                    ),
                    "original_amount": _fixture_decimal_text(amount),
                    "original_currency": "USD",
                    "fx_rate_to_usd": "1",
                    "fx_authority_id": "IDENTITY_USD",
                    "fx_generation_sha256": _IDENTITY_USD_SHA256,
                    "usd_amount": _fixture_decimal_text(amount),
                    "correction_kind": "ORIGINAL",
                    "related_event_id": None,
                    "transfer_group_id": None,
                    "counterparty_strategy_id": None,
                    "classification_authority_sha256": product_sha256,
                }
                events.append(
                    {
                        **event_without_evidence,
                        "event_evidence_sha256": _fixture_sha256(
                            event_without_evidence
                        ),
                    }
                )
    transfer_events_by_interval: dict[str, list[dict[str, object]]] = {}
    for event in events:
        if event["capital_treatment"] == "INTERNAL_TRANSFER":
            transfer_events_by_interval.setdefault(
                str(event["interval_id"]), []
            ).append(event)
    for interval_id, transfer_events in transfer_events_by_interval.items():
        negatives = sorted(
            (
                event
                for event in transfer_events
                if Decimal(str(event["usd_amount"])) < 0
            ),
            key=lambda event: str(event["event_id"]),
        )
        positives = sorted(
            (
                event
                for event in transfer_events
                if Decimal(str(event["usd_amount"])) > 0
            ),
            key=lambda event: str(event["event_id"]),
        )
        for pair_index, negative in enumerate(negatives):
            positive = next(
                (
                    candidate
                    for candidate in positives
                    if Decimal(str(candidate["usd_amount"]))
                    == -Decimal(str(negative["usd_amount"]))
                    and candidate["transfer_group_id"] is None
                ),
                None,
            )
            if positive is None:
                continue
            transfer_group_id = f"fixture-transfer:{interval_id}:{pair_index}"
            negative["transfer_group_id"] = transfer_group_id
            negative["counterparty_strategy_id"] = positive["strategy_id"]
            positive["transfer_group_id"] = transfer_group_id
            positive["counterparty_strategy_id"] = negative["strategy_id"]
    for event in events:
        unsigned_event = {
            key: value for key, value in event.items() if key != "event_evidence_sha256"
        }
        event["event_evidence_sha256"] = _fixture_sha256(unsigned_event)
    return sorted(events, key=lambda event: str(event["event_id"]))


def _as_held_hwm_fixture() -> dict[str, object]:
    interval_definitions = [
        {
            "interval_id": "d1",
            "interval_start": "2026-08-10",
            "interval_end": "2026-08-11",
        },
        {
            "interval_id": "d2",
            "interval_start": "2026-08-11",
            "interval_end": "2026-08-12",
        },
        {
            "interval_id": "d3",
            "interval_start": "2026-08-12",
            "interval_end": "2026-08-13",
        },
    ]
    calendar_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "calendar_policy_id": "SYNTHETIC_DAILY_COMMON_CALENDAR",
                "accepted_intervals": interval_definitions,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    calendar_sha256 = hashlib.sha256(calendar_bytes).hexdigest()
    intervals = [
        {**row, "interval_evidence_sha256": calendar_sha256}
        for row in interval_definitions
    ]
    memberships = [
        {
            "strategy_id": "strategy-a",
            "effective_start": "2026-08-10",
            "effective_end": None,
            "membership_evidence_sha256": "1" * 64,
        },
        {
            "strategy_id": "strategy-b",
            "effective_start": "2026-08-10",
            "effective_end": None,
            "membership_evidence_sha256": "2" * 64,
        },
    ]
    periods = [
        _as_held_component(
            "strategy-a", "d1", "2026-08-10", "2026-08-11", "600", "0.10"
        ),
        _as_held_component(
            "strategy-b", "d1", "2026-08-10", "2026-08-11", "400", "-0.05"
        ),
        _as_held_component(
            "strategy-a", "d2", "2026-08-11", "2026-08-12", "660", "-0.10"
        ),
        _as_held_component(
            "strategy-b", "d2", "2026-08-11", "2026-08-12", "380", "-0.10"
        ),
        _as_held_component("strategy-a", "d3", "2026-08-12", "2026-08-13", "594", "0"),
        _as_held_component("strategy-b", "d3", "2026-08-12", "2026-08-13", "342", "0"),
    ]
    evidence_sha256s = sorted(
        {
            *(str(row["membership_evidence_sha256"]) for row in memberships),
            *(
                str(row[field])
                for row in periods
                for field in (
                    "capital_evidence_sha256",
                    "flow_reconciliation_sha256",
                    "return_evidence_sha256",
                    "leverage_evidence_sha256",
                )
            ),
            *(
                str(subperiod["flow_event_evidence_sha256"])
                for row in periods
                for subperiod in row["subperiods"]  # type: ignore[union-attr]
            ),
        }
    )
    input_generation_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "builder": "portfolio-hwm-inputs",
                "evidence_sha256s": evidence_sha256s,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    input_generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    for row in periods:
        row["source_generation_sha256"] = input_generation_sha256
        row["calendar_sha256"] = calendar_sha256
    fixture: dict[str, object] = {
        "accepted_intervals": intervals,
        "memberships": memberships,
        "component_periods": periods,
        "policy_bytes": _PORT_HWM4_POLICY_BYTES,
        "product_contract_bytes": _PRODUCT_CONTRACT_BYTES,
        "performance_policy_bytes": _PERFORMANCE_POLICY_BYTES,
        "legacy_policy_bytes": _LEGACY_HWM_POLICY_BYTES,
        "calendar_bytes": calendar_bytes,
        "input_generation_bytes": input_generation_bytes,
        "evaluated_on": "2026-08-17",
        "complete_through": "2026-08-13",
        "maximum_age_days": 3,
        "assurance_profile": "DEVELOPMENT",
    }
    _refresh_fixture_authorities(fixture)
    return fixture


def _refresh_fixture_authorities(fixture: dict[str, object]) -> None:
    """Rebuild synthetic authority bytes after an intentional fixture edit."""

    intervals: list[dict[str, object]] = fixture["accepted_intervals"]  # type: ignore[assignment]
    memberships: list[dict[str, object]] = fixture["memberships"]  # type: ignore[assignment]
    periods: list[dict[str, object]] = fixture["component_periods"]  # type: ignore[assignment]
    for row in memberships:
        _refresh_membership_evidence(row)
    interval_definitions = [
        {
            "interval_id": str(row["interval_id"]),
            "interval_start": str(row["interval_start"]),
            "interval_end": str(row["interval_end"]),
        }
        for row in intervals
    ]
    calendar_without_evidence: dict[str, object] = {
        "schema_version": 2,
        "calendar_policy_id": fixture.get(
            "_calendar_policy_id_override", "SYNTHETIC_DAILY_COMMON_CALENDAR"
        ),
        "authority_status": fixture.get(
            "_calendar_authority_status_override", "PORTABLE_SYNTHETIC"
        ),
        "accepted_intervals": interval_definitions,
    }
    calendar = {
        **calendar_without_evidence,
        "authority_evidence_sha256": _fixture_sha256(calendar_without_evidence),
    }
    calendar_bytes = (
        json.dumps(
            calendar,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    calendar_sha256 = hashlib.sha256(calendar_bytes).hexdigest()
    for row in intervals:
        row["interval_evidence_sha256"] = calendar_sha256

    override_events = fixture.get("_flow_events_override")
    flow_events = (
        deepcopy(override_events)
        if isinstance(override_events, list)
        else _fixture_flow_events(periods)
    )
    for event in flow_events:
        unsigned_event = {
            key: value for key, value in event.items() if key != "event_evidence_sha256"
        }
        event["event_evidence_sha256"] = _fixture_sha256(unsigned_event)
    flow_events_by_subperiod: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    corrected_event_ids = {
        str(event["related_event_id"])
        for event in flow_events
        if event.get("correction_kind") == "SOURCE_CORRECTION"
    }
    for event in flow_events:
        if str(event["event_id"]) in corrected_event_ids:
            continue
        flow_events_by_subperiod.setdefault(
            (
                str(event["strategy_id"]),
                str(event["interval_id"]),
                str(event["subperiod_id"]),
            ),
            [],
        ).append(event)
    for row in periods:
        _refresh_component_evidence(row, flow_events_by_subperiod)

    evidence_sha256s = sorted(
        {
            str(calendar["authority_evidence_sha256"]),
            *(str(row["membership_evidence_sha256"]) for row in memberships),
            *(
                str(row[field])
                for row in periods
                for field in (
                    "capital_evidence_sha256",
                    "flow_reconciliation_sha256",
                    "return_evidence_sha256",
                    "leverage_evidence_sha256",
                )
            ),
            *(
                str(subperiod["flow_event_evidence_sha256"])
                for row in periods
                for subperiod in row["subperiods"]
            ),
            *(str(event["event_evidence_sha256"]) for event in flow_events),
            *(str(event["fx_generation_sha256"]) for event in flow_events),
        }
    )
    authority_bindings = {
        "policy_sha256": hashlib.sha256(fixture["policy_bytes"]).hexdigest(),  # type: ignore[arg-type]
        "policy_semantic_digest": hashlib.sha256(
            json.dumps(
                json.loads(fixture["policy_bytes"]),  # type: ignore[arg-type]
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "product_contract_sha256": hashlib.sha256(
            fixture["product_contract_bytes"]  # type: ignore[arg-type]
        ).hexdigest(),
        "performance_policy_sha256": hashlib.sha256(
            fixture["performance_policy_bytes"]  # type: ignore[arg-type]
        ).hexdigest(),
        "legacy_policy_sha256": hashlib.sha256(
            fixture["legacy_policy_bytes"]  # type: ignore[arg-type]
        ).hexdigest(),
        "calendar_sha256": calendar_sha256,
    }
    input_generation_bytes = (
        json.dumps(
            {
                "schema_version": 2,
                "builder": "portfolio-hwm-inputs",
                "builder_version": "0.2.0",
                "authority_profile": "PORTABLE_SYNTHETIC",
                "authority_bindings": authority_bindings,
                "upstream_receipt_sha256": None,
                "maximum_age_days": int(fixture["maximum_age_days"]),
                "evidence_sha256s": evidence_sha256s,
                "flow_events": flow_events,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    for row in periods:
        row["calendar_sha256"] = calendar_sha256
        row["source_generation_sha256"] = generation_sha256
    fixture["calendar_bytes"] = calendar_bytes
    fixture["input_generation_bytes"] = input_generation_bytes


def _compute_as_held(fixture: dict[str, object]) -> dict[str, object]:
    return compute_as_held_composite_hwm(
        fixture["accepted_intervals"],  # type: ignore[arg-type]
        fixture["memberships"],  # type: ignore[arg-type]
        fixture["component_periods"],  # type: ignore[arg-type]
        policy_bytes=fixture["policy_bytes"],  # type: ignore[arg-type]
        product_contract_bytes=fixture["product_contract_bytes"],  # type: ignore[arg-type]
        performance_policy_bytes=fixture["performance_policy_bytes"],  # type: ignore[arg-type]
        legacy_policy_bytes=fixture["legacy_policy_bytes"],  # type: ignore[arg-type]
        calendar_bytes=fixture["calendar_bytes"],  # type: ignore[arg-type]
        input_generation_bytes=fixture["input_generation_bytes"],  # type: ignore[arg-type]
        evaluated_on=str(fixture["evaluated_on"]),
        complete_through=str(fixture["complete_through"]),
        maximum_age_days=int(fixture["maximum_age_days"]),
        assurance_profile=str(fixture["assurance_profile"]),
    )


def _direct_return_evidence_payload(row: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "component_financial_mode": row["component_financial_mode"],
        "strategy_id": str(row["strategy_id"]),
        "interval_id": str(row["interval_id"]),
        "evidence_type": "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN",
        "return_basis": row["return_basis"],
        "portable_return_basis_declaration_sha256": row[
            "portable_return_basis_declaration_sha256"
        ],
        "beginning_capital_usd": _fixture_decimal_text(row["beginning_capital_usd"]),
        "ending_nav_usd": _fixture_decimal_text(row["ending_nav_usd"]),
        "flow_adjusted_ending_nav_usd": _fixture_decimal_text(
            row["flow_adjusted_ending_nav_usd"]
        ),
        "period_return": _fixture_decimal_text(row["period_return"]),
        "subperiods": row["subperiods"],
    }


def _as_held_direct_net_return_fixture() -> dict[str, object]:
    """Build a portable proof without inventing leverage-detail facts."""

    fixture = _as_held_hwm_fixture()
    periods: list[dict[str, object]] = fixture["component_periods"]  # type: ignore[assignment]
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    authority_bindings = generation["authority_bindings"]
    portable_return_basis_declaration_sha256 = _fixture_sha256(
        {
            "schema_version": 1,
            "evidence_type": "PORTABLE_RETURN_BASIS_DECLARATION",
            "authority_profile": "PORTABLE_SYNTHETIC",
            "component_financial_mode": "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN",
            "return_basis": "LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL",
            **authority_bindings,
        }
    )
    flow_events = generation["flow_events"]
    flow_events_by_subperiod: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for event in flow_events:
        assert event["capital_treatment"] not in {
            "MARGIN_PRINCIPAL",
            "FINANCING_COST",
        }
        flow_events_by_subperiod.setdefault(
            (
                str(event["strategy_id"]),
                str(event["interval_id"]),
                str(event["subperiod_id"]),
            ),
            [],
        ).append(event)

    for row in periods:
        row["component_financial_mode"] = "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN"
        row["portable_return_basis_declaration_sha256"] = (
            portable_return_basis_declaration_sha256
        )
        for field_name in (
            "beginning_gross_assets_usd",
            "beginning_financing_liability_usd",
            "ending_gross_assets_usd",
            "ending_financing_liability_usd",
            "margin_principal_flow_usd",
            "financing_cost_usd",
            "gross_asset_return",
            "leverage_evidence_sha256",
        ):
            del row[field_name]

        normalized_subperiods: list[dict[str, object]] = []
        for subperiod in row["subperiods"]:  # type: ignore[union-attr]
            for field_name in (
                "beginning_gross_assets_usd",
                "beginning_financing_liability_usd",
                "ending_gross_assets_before_flow_usd",
                "ending_financing_liability_before_flow_usd",
                "margin_principal_flow_after_subperiod_usd",
                "financing_cost_usd",
            ):
                del subperiod[field_name]
            normalized_without_evidence = {
                "subperiod_id": str(subperiod["subperiod_id"]),
                "subperiod_start_utc": str(subperiod["subperiod_start_utc"]),
                "subperiod_end_utc": str(subperiod["subperiod_end_utc"]),
                "beginning_nav_usd": _fixture_decimal_text(
                    subperiod["beginning_nav_usd"]
                ),
                "ending_nav_before_flow_usd": _fixture_decimal_text(
                    subperiod["ending_nav_before_flow_usd"]
                ),
                "external_flow_after_subperiod_usd": _fixture_decimal_text(
                    subperiod["external_flow_after_subperiod_usd"]
                ),
                "internal_transfer_after_subperiod_usd": _fixture_decimal_text(
                    subperiod["internal_transfer_after_subperiod_usd"]
                ),
            }
            events = flow_events_by_subperiod.get(
                (
                    str(row["strategy_id"]),
                    str(row["interval_id"]),
                    str(subperiod["subperiod_id"]),
                ),
                [],
            )
            flow_evidence_sha256 = _fixture_sha256(
                {
                    "schema_version": 3,
                    "evidence_type": "FLOW_EVENT",
                    "component_financial_mode": row["component_financial_mode"],
                    "strategy_id": str(row["strategy_id"]),
                    "interval_id": str(row["interval_id"]),
                    "subperiod": {
                        **normalized_without_evidence,
                        "flow_event_ids": [str(event["event_id"]) for event in events],
                        "flow_event_sha256s": [
                            str(event["event_evidence_sha256"]) for event in events
                        ],
                    },
                }
            )
            subperiod["flow_event_evidence_sha256"] = flow_evidence_sha256
            normalized_subperiods.append(
                {
                    **normalized_without_evidence,
                    "flow_event_evidence_sha256": flow_evidence_sha256,
                }
            )
        row["subperiods"] = normalized_subperiods

        common = {
            "schema_version": 2,
            "component_financial_mode": row["component_financial_mode"],
            "strategy_id": str(row["strategy_id"]),
            "interval_id": str(row["interval_id"]),
        }
        row["capital_evidence_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "BEGINNING_CAPITAL",
                "beginning_capital_usd": _fixture_decimal_text(
                    row["beginning_capital_usd"]
                ),
                "capital_basis": str(row["capital_basis"]),
            }
        )
        row["flow_reconciliation_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "FLOW_RECONCILIATION",
                "flow_timing": str(row["flow_timing"]),
                "external_flow_usd": _fixture_decimal_text(row["external_flow_usd"]),
                "internal_transfer_usd": _fixture_decimal_text(
                    row["internal_transfer_usd"]
                ),
                "subperiod_flow_evidence_sha256s": [
                    item["flow_event_evidence_sha256"] for item in normalized_subperiods
                ],
            }
        )
        row["return_evidence_sha256"] = _fixture_sha256(
            _direct_return_evidence_payload(row)
        )

    generation["schema_version"] = 3
    generation["builder_version"] = "0.3.0"
    generation["component_financial_modes"] = ["DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN"]
    calendar = json.loads(fixture["calendar_bytes"])  # type: ignore[arg-type]
    generation["evidence_sha256s"] = sorted(
        {
            str(calendar["authority_evidence_sha256"]),
            *(
                str(row["membership_evidence_sha256"])
                for row in fixture["memberships"]  # type: ignore[union-attr]
            ),
            *(
                str(row[field_name])
                for row in periods
                for field_name in (
                    "capital_evidence_sha256",
                    "flow_reconciliation_sha256",
                    "return_evidence_sha256",
                    "portable_return_basis_declaration_sha256",
                )
            ),
            *(
                str(subperiod["flow_event_evidence_sha256"])
                for row in periods
                for subperiod in row["subperiods"]  # type: ignore[union-attr]
            ),
            *(str(event["event_evidence_sha256"]) for event in flow_events),
            *(str(event["fx_generation_sha256"]) for event in flow_events),
        }
    )
    input_generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    input_generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    for row in periods:
        del row["source_generation_sha256"]
        row["portable_input_generation_sha256"] = input_generation_sha256
    fixture["input_generation_bytes"] = input_generation_bytes
    return fixture


def _refresh_v3_generation(
    fixture: dict[str, object], component_financial_modes: list[str]
) -> None:
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["schema_version"] = 3
    generation["builder_version"] = "0.3.0"
    generation["component_financial_modes"] = component_financial_modes
    periods: list[dict[str, object]] = fixture["component_periods"]  # type: ignore[assignment]
    calendar = json.loads(fixture["calendar_bytes"])  # type: ignore[arg-type]
    flow_events = generation["flow_events"]
    generation["evidence_sha256s"] = sorted(
        {
            str(calendar["authority_evidence_sha256"]),
            *(
                str(row["membership_evidence_sha256"])
                for row in fixture["memberships"]  # type: ignore[union-attr]
            ),
            *(
                str(row[field_name])
                for row in periods
                for field_name in (
                    "capital_evidence_sha256",
                    "flow_reconciliation_sha256",
                    "return_evidence_sha256",
                    (
                        "leverage_evidence_sha256"
                        if row["component_financial_mode"]
                        == "DETAILED_RECONCILED_LEVERAGE"
                        else "portable_return_basis_declaration_sha256"
                    ),
                )
            ),
            *(
                str(subperiod["flow_event_evidence_sha256"])
                for row in periods
                for subperiod in row["subperiods"]  # type: ignore[union-attr]
            ),
            *(str(event["event_evidence_sha256"]) for event in flow_events),
            *(str(event["fx_generation_sha256"]) for event in flow_events),
        }
    )
    input_generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    portable_input_generation_sha256 = hashlib.sha256(
        input_generation_bytes
    ).hexdigest()
    for row in periods:
        row.pop("source_generation_sha256", None)
        row["portable_input_generation_sha256"] = portable_input_generation_sha256
    fixture["input_generation_bytes"] = input_generation_bytes


def _as_held_detailed_v3_fixture() -> dict[str, object]:
    """Upgrade the original detailed proof to explicit v3 evidence shapes."""

    fixture = _as_held_hwm_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    assert generation["flow_events"] == []
    normalized_result = _compute_as_held(fixture)
    periods = deepcopy(normalized_result["input_evidence"]["component_periods"])
    fixture["component_periods"] = periods
    for row in periods:
        row["component_financial_mode"] = "DETAILED_RECONCILED_LEVERAGE"
        for subperiod in row["subperiods"]:
            subperiod_without_evidence = {
                key: value
                for key, value in subperiod.items()
                if key != "flow_event_evidence_sha256"
            }
            subperiod["flow_event_evidence_sha256"] = _fixture_sha256(
                {
                    "schema_version": 3,
                    "evidence_type": "FLOW_EVENT",
                    "component_financial_mode": row["component_financial_mode"],
                    "strategy_id": row["strategy_id"],
                    "interval_id": row["interval_id"],
                    "subperiod": {
                        **subperiod_without_evidence,
                        "flow_event_ids": [],
                        "flow_event_sha256s": [],
                    },
                }
            )
        common = {
            "schema_version": 2,
            "component_financial_mode": row["component_financial_mode"],
            "strategy_id": row["strategy_id"],
            "interval_id": row["interval_id"],
        }
        row["capital_evidence_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "BEGINNING_CAPITAL",
                "beginning_capital_usd": row["beginning_capital_usd"],
                "capital_basis": row["capital_basis"],
            }
        )
        row["flow_reconciliation_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "FLOW_RECONCILIATION",
                "flow_timing": row["flow_timing"],
                "external_flow_usd": row["external_flow_usd"],
                "internal_transfer_usd": row["internal_transfer_usd"],
                "subperiod_flow_evidence_sha256s": [
                    subperiod["flow_event_evidence_sha256"]
                    for subperiod in row["subperiods"]
                ],
                "margin_principal_flow_usd": row["margin_principal_flow_usd"],
            }
        )
        row["return_evidence_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "LEVERED_NET_RETURN",
                "beginning_capital_usd": row["beginning_capital_usd"],
                "ending_nav_usd": row["ending_nav_usd"],
                "flow_adjusted_ending_nav_usd": row["flow_adjusted_ending_nav_usd"],
                "period_return": row["period_return"],
                "subperiods": row["subperiods"],
            }
        )
        row["leverage_evidence_sha256"] = _fixture_sha256(
            {
                **common,
                "evidence_type": "LEVERAGE_RECONCILIATION",
                "beginning_gross_assets_usd": row["beginning_gross_assets_usd"],
                "beginning_financing_liability_usd": row[
                    "beginning_financing_liability_usd"
                ],
                "ending_gross_assets_usd": row["ending_gross_assets_usd"],
                "ending_financing_liability_usd": row["ending_financing_liability_usd"],
                "margin_principal_flow_usd": row["margin_principal_flow_usd"],
                "financing_cost_usd": row["financing_cost_usd"],
                "gross_asset_return": row["gross_asset_return"],
                "subperiods": row["subperiods"],
            }
        )
    _refresh_v3_generation(fixture, ["DETAILED_RECONCILED_LEVERAGE"])
    return fixture


def _as_held_mixed_v3_fixture() -> dict[str, object]:
    direct_fixture = _as_held_direct_net_return_fixture()
    detailed_fixture = _as_held_detailed_v3_fixture()
    detailed_by_key = {
        (row["interval_id"], row["strategy_id"]): row
        for row in detailed_fixture["component_periods"]  # type: ignore[union-attr]
    }
    periods = [
        (
            deepcopy(detailed_by_key[(row["interval_id"], row["strategy_id"])])
            if row["strategy_id"] == "strategy-a"
            else row
        )
        for row in direct_fixture["component_periods"]  # type: ignore[union-attr]
    ]
    direct_fixture["component_periods"] = periods
    _refresh_v3_generation(
        direct_fixture,
        [
            "DETAILED_RECONCILED_LEVERAGE",
            "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN",
        ],
    )
    return direct_fixture


def _single_strategy_flow_fixture(
    *,
    external_flow: str = "0",
    margin_principal_flow: str = "0",
    financing_liability: str = "0",
    financing_cost: str = "0",
) -> dict[str, object]:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0",
            external_flow=external_flow,
            margin_principal_flow=margin_principal_flow,
            financing_liability=financing_liability,
            beginning_financing_liability="0",
            financing_cost=financing_cost,
        )
    ]
    fixture["complete_through"] = "2026-08-11"
    fixture["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)
    return fixture


def test_as_held_hwm_policy_is_valid_and_binds_current_authorities() -> None:
    validate_as_held_hwm_policy(_PORT_HWM4_POLICY)

    schema = json.loads(
        (_REPO_ROOT / "schemas" / "portfolio" / "hwm-policy.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(schema["required"]) == set(_PORT_HWM4_POLICY)
    assert schema["properties"]["schema_version"]["const"] == 5
    assert schema["properties"]["policy_id"]["const"] == "PORT-HWM-4"
    receipt_schema = json.loads(
        (
            _REPO_ROOT / "schemas" / "portfolio" / "hwm-calculation-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt_schema["properties"]["assurance_profile"] == {"const": "DEVELOPMENT"}

    authorities = {
        "product_authority": ("path", "raw_sha256"),
        "performance_authority": ("path", "sha256"),
        "material_perimeter_authority": (
            "legacy_policy_path",
            "legacy_policy_sha256",
        ),
    }
    for authority_name, (path_field, hash_field) in authorities.items():
        authority = _PORT_HWM4_POLICY[authority_name]
        assert (
            hashlib.sha256(
                (_REPO_ROOT / authority[path_field]).read_bytes()
            ).hexdigest()
            == authority[hash_field]
        )


def test_as_held_hwm_policy_semantic_mutation_fails_closed() -> None:
    policy = deepcopy(_PORT_HWM4_POLICY)
    policy["calculation"]["return_basis"] = "UNLEVERED_GROSS_ASSETS"

    with pytest.raises(HwmCoverageError, match="calculation differs"):
        validate_as_held_hwm_policy(policy)


def test_as_held_hwm_reproduces_authoritative_base_100_vector() -> None:
    result = _compute_as_held(_as_held_hwm_fixture())

    assert result["status"] == "PORTABLE_SYNTHETIC_PROOF"
    assert [row["index"] for row in result["index_path"]] == [
        Decimal("100"),
        Decimal("104"),
        Decimal("93.6"),
        Decimal("93.6"),
    ]
    assert result["current_hwm"] == Decimal("104")
    assert result["current_drawdown"] == Decimal("-0.1")
    assert result["maximum_drawdown"] == Decimal("-0.1")
    assert result["governor_drawdown_loss"] == Decimal("0.1")
    assert result["calculation_age_days"] == 4
    assert result["age_status"] == "COMPLETE_DATA_AGE_WARNING"
    assert result["warnings"] == ["HWM_COMPLETE_DATA_AGE"]
    assert len(result["receipt_sha256"]) == 64


def test_as_held_hwm_accepts_direct_net_return_without_leverage_facts() -> None:
    fixture = _as_held_direct_net_return_fixture()

    result = _compute_as_held(fixture)

    assert result["status"] == "PORTABLE_SYNTHETIC_PROOF"
    assert result["schema_version"] == 3
    assert result["current_index"] == Decimal("93.6")
    assert {
        component["component_financial_mode"]
        for period in result["periods"]  # type: ignore[union-attr]
        for component in period["components"]
    } == {"DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN"}
    receipt = as_held_hwm_receipt(result)
    verify_as_held_hwm_receipt(
        receipt,
        policy_bytes=fixture["policy_bytes"],  # type: ignore[arg-type]
        product_contract_bytes=fixture["product_contract_bytes"],  # type: ignore[arg-type]
        performance_policy_bytes=fixture["performance_policy_bytes"],  # type: ignore[arg-type]
        legacy_policy_bytes=fixture["legacy_policy_bytes"],  # type: ignore[arg-type]
        calendar_bytes=fixture["calendar_bytes"],  # type: ignore[arg-type]
        input_generation_bytes=fixture["input_generation_bytes"],  # type: ignore[arg-type]
    )
    receipt_schema = json.loads(
        (
            _REPO_ROOT
            / "schemas"
            / "portfolio"
            / "hwm-calculation-receipt-v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    generation_schema = json.loads(
        (
            _REPO_ROOT / "schemas" / "portfolio" / "hwm-input-generation-v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    assert receipt_schema["properties"]["schema_version"] == {"const": 3}
    assert set(receipt_schema["required"]) == set(receipt)
    assert generation_schema["properties"]["schema_version"] == {"const": 3}
    assert set(generation_schema["required"]) == set(generation)
    assert all(
        component["portable_input_generation_sha256"]
        == receipt["input_generation_sha256"]
        for component in receipt["input_evidence"]["component_periods"]
    )


@pytest.mark.parametrize(
    ("fixture_builder", "expected_modes"),
    [
        (
            _as_held_detailed_v3_fixture,
            {"DETAILED_RECONCILED_LEVERAGE"},
        ),
        (
            _as_held_mixed_v3_fixture,
            {
                "DETAILED_RECONCILED_LEVERAGE",
                "DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN",
            },
        ),
    ],
)
def test_as_held_hwm_v3_detailed_and_mixed_modes_round_trip(
    fixture_builder: Callable[[], dict[str, object]],
    expected_modes: set[str],
) -> None:
    fixture = fixture_builder()
    result = _compute_as_held(fixture)
    receipt = as_held_hwm_receipt(result)

    assert result["schema_version"] == 3
    assert {
        component["component_financial_mode"]
        for period in receipt["periods"]
        for component in period["components"]
    } == expected_modes
    verify_as_held_hwm_receipt(
        receipt,
        policy_bytes=fixture["policy_bytes"],
        product_contract_bytes=fixture["product_contract_bytes"],
        performance_policy_bytes=fixture["performance_policy_bytes"],
        legacy_policy_bytes=fixture["legacy_policy_bytes"],
        calendar_bytes=fixture["calendar_bytes"],
        input_generation_bytes=fixture["input_generation_bytes"],
    )


def test_as_held_hwm_v3_json_schemas_validate_every_mode_shape() -> None:
    receipt_schema = json.loads(
        (
            _REPO_ROOT
            / "schemas"
            / "portfolio"
            / "hwm-calculation-receipt-v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    generation_schema = json.loads(
        (
            _REPO_ROOT / "schemas" / "portfolio" / "hwm-input-generation-v3.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(receipt_schema)
    Draft202012Validator.check_schema(generation_schema)
    receipt_validator = Draft202012Validator(receipt_schema)
    generation_validator = Draft202012Validator(generation_schema)

    accepted_receipts: dict[str, dict[str, object]] = {}
    for name, fixture_builder in {
        "detailed": _as_held_detailed_v3_fixture,
        "direct": _as_held_direct_net_return_fixture,
        "mixed": _as_held_mixed_v3_fixture,
    }.items():
        fixture = fixture_builder()
        generation_validator.validate(
            json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
        )
        receipt = as_held_hwm_receipt(_compute_as_held(fixture))
        receipt_validator.validate(receipt)
        accepted_receipts[name] = receipt

    direct_with_leverage = deepcopy(accepted_receipts["direct"])
    direct_with_leverage["input_evidence"]["component_periods"][0][  # type: ignore[index]
        "financing_cost_usd"
    ] = "0"
    with pytest.raises(ValidationError):
        receipt_validator.validate(direct_with_leverage)

    detailed_with_portable_declaration = deepcopy(accepted_receipts["detailed"])
    detailed_with_portable_declaration["input_evidence"]["component_periods"][0][  # type: ignore[index]
        "portable_return_basis_declaration_sha256"
    ] = "a" * 64
    with pytest.raises(ValidationError):
        receipt_validator.validate(detailed_with_portable_declaration)

    reversed_mixed_generation = json.loads(
        _as_held_mixed_v3_fixture()["input_generation_bytes"]  # type: ignore[arg-type]
    )
    reversed_mixed_generation["component_financial_modes"].reverse()
    with pytest.raises(ValidationError):
        generation_validator.validate(reversed_mixed_generation)


def test_as_held_hwm_direct_mode_rejects_fabricated_zero_leverage() -> None:
    fixture = _as_held_direct_net_return_fixture()
    fixture["component_periods"][0]["financing_cost_usd"] = "0"  # type: ignore[index]

    with pytest.raises(HwmCoverageError, match=r"component_periods\[0\] keys differ"):
        _compute_as_held(fixture)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "beginning_gross_assets_usd",
        "beginning_financing_liability_usd",
        "ending_gross_assets_before_flow_usd",
        "ending_financing_liability_before_flow_usd",
        "margin_principal_flow_after_subperiod_usd",
        "financing_cost_usd",
    ],
)
def test_as_held_hwm_direct_mode_rejects_forbidden_subperiod_fields(
    forbidden_field: str,
) -> None:
    fixture = _as_held_direct_net_return_fixture()
    subperiod = fixture["component_periods"][0]["subperiods"][0]  # type: ignore[index]
    subperiod[forbidden_field] = "0"

    with pytest.raises(
        HwmCoverageError, match=r"component\.subperiods\[0\] keys differ"
    ):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_rejects_offsetting_margin_events() -> None:
    fixture = _as_held_direct_net_return_fixture()
    periods: list[dict[str, object]] = fixture["component_periods"]  # type: ignore[assignment]
    component = periods[0]
    subperiod = component["subperiods"][0]  # type: ignore[index]
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    product_sha256 = hashlib.sha256(_PRODUCT_CONTRACT_BYTES).hexdigest()

    margin_events = []
    for event_type, amount in (("MARGIN_BORROW", "10"), ("MARGIN_REPAYMENT", "-10")):
        event_without_evidence = {
            "event_id": f"{component['strategy_id']}:{component['interval_id']}:{event_type}",
            "strategy_id": component["strategy_id"],
            "interval_id": component["interval_id"],
            "subperiod_id": subperiod["subperiod_id"],
            "event_type": event_type,
            "capital_treatment": "MARGIN_PRINCIPAL",
            "effective_at_utc": subperiod["subperiod_end_utc"],
            "effective_timezone": "UTC",
            "timing_basis": "DATE_ONLY_END_OF_DAY",
            "original_amount": amount,
            "original_currency": "USD",
            "fx_rate_to_usd": "1",
            "fx_authority_id": "IDENTITY_USD",
            "fx_generation_sha256": _IDENTITY_USD_SHA256,
            "usd_amount": amount,
            "correction_kind": "ORIGINAL",
            "related_event_id": None,
            "transfer_group_id": None,
            "counterparty_strategy_id": None,
            "classification_authority_sha256": product_sha256,
        }
        margin_events.append(
            {
                **event_without_evidence,
                "event_evidence_sha256": _fixture_sha256(event_without_evidence),
            }
        )
    generation["flow_events"].extend(margin_events)

    subperiod_without_evidence = {
        key: value
        for key, value in subperiod.items()
        if key != "flow_event_evidence_sha256"
    }
    subperiod["flow_event_evidence_sha256"] = _fixture_sha256(
        {
            "schema_version": 3,
            "evidence_type": "FLOW_EVENT",
            "component_financial_mode": component["component_financial_mode"],
            "strategy_id": component["strategy_id"],
            "interval_id": component["interval_id"],
            "subperiod": {
                **subperiod_without_evidence,
                "flow_event_ids": [event["event_id"] for event in margin_events],
                "flow_event_sha256s": [
                    event["event_evidence_sha256"] for event in margin_events
                ],
            },
        }
    )
    component_common = {
        "schema_version": 2,
        "component_financial_mode": component["component_financial_mode"],
        "strategy_id": component["strategy_id"],
        "interval_id": component["interval_id"],
    }
    component["flow_reconciliation_sha256"] = _fixture_sha256(
        {
            **component_common,
            "evidence_type": "FLOW_RECONCILIATION",
            "flow_timing": component["flow_timing"],
            "external_flow_usd": component["external_flow_usd"],
            "internal_transfer_usd": component["internal_transfer_usd"],
            "subperiod_flow_evidence_sha256s": [
                subperiod["flow_event_evidence_sha256"]
            ],
        }
    )
    component["return_evidence_sha256"] = _fixture_sha256(
        _direct_return_evidence_payload(component)
    )
    fixture["input_generation_bytes"] = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _refresh_v3_generation(fixture, ["DIRECT_NET_TOTAL_FLOW_NEUTRAL_RETURN"])

    with pytest.raises(HwmCoverageError, match="component flow facts differ"):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_cannot_downgrade_to_schema_two() -> None:
    fixture = _as_held_direct_net_return_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["schema_version"] = 2
    generation["builder_version"] = "0.2.0"
    del generation["component_financial_modes"]
    input_generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    fixture["input_generation_bytes"] = input_generation_bytes
    for component in fixture["component_periods"]:  # type: ignore[union-attr]
        del component["portable_input_generation_sha256"]
        component["source_generation_sha256"] = generation_sha256

    with pytest.raises(HwmCoverageError, match=r"component_periods\[0\] keys differ"):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_must_be_declared_by_generation() -> None:
    fixture = _as_held_direct_net_return_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["component_financial_modes"] = ["DETAILED_RECONCILED_LEVERAGE"]
    input_generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    generation_sha256 = hashlib.sha256(input_generation_bytes).hexdigest()
    fixture["input_generation_bytes"] = input_generation_bytes
    for component in fixture["component_periods"]:  # type: ignore[union-attr]
        component["portable_input_generation_sha256"] = generation_sha256

    with pytest.raises(HwmCoverageError, match="financial mode is not declared"):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_rejects_non_string_mode_declaration() -> None:
    fixture = _as_held_direct_net_return_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["component_financial_modes"] = [{}]
    fixture["input_generation_bytes"] = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    with pytest.raises(HwmCoverageError, match="component financial modes differ"):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_rejects_governed_self_relabel() -> None:
    fixture = _as_held_direct_net_return_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["authority_profile"] = "ACCEPTED_GOVERNED"
    generation["upstream_receipt_sha256"] = "a" * 64
    fixture["input_generation_bytes"] = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    with pytest.raises(HwmCoverageError, match="input generation identity differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_direct_mode_rejects_coherent_declaration_rewrite() -> None:
    fixture = _as_held_direct_net_return_fixture()
    periods: list[dict[str, object]] = fixture["component_periods"]  # type: ignore[assignment]
    component = periods[0]
    old_return_sha256 = str(component["return_evidence_sha256"])
    fabricated_declaration_sha256 = "a" * 64
    component["portable_return_basis_declaration_sha256"] = (
        fabricated_declaration_sha256
    )
    component["return_evidence_sha256"] = _fixture_sha256(
        _direct_return_evidence_payload(component)
    )

    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    evidence_sha256s = set(generation["evidence_sha256s"])
    evidence_sha256s.remove(old_return_sha256)
    evidence_sha256s.update(
        {
            fabricated_declaration_sha256,
            component["return_evidence_sha256"],
        }
    )
    generation["evidence_sha256s"] = sorted(evidence_sha256s)
    input_generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    fixture["input_generation_bytes"] = input_generation_bytes
    portable_input_generation_sha256 = hashlib.sha256(
        input_generation_bytes
    ).hexdigest()
    for row in periods:
        row["portable_input_generation_sha256"] = portable_input_generation_sha256

    with pytest.raises(
        HwmCoverageError,
        match="portable_return_basis_declaration_sha256 identity differs",
    ):
        _compute_as_held(fixture)


@pytest.mark.parametrize("assurance_profile", ["WORKSTATION_QC", "RELEASE_QC"])
def test_as_held_hwm_direct_mode_stays_development_only(
    assurance_profile: str,
) -> None:
    fixture = _as_held_direct_net_return_fixture()
    fixture["assurance_profile"] = assurance_profile

    with pytest.raises(HwmCoverageError, match="development proof only"):
        _compute_as_held(fixture)


@pytest.mark.parametrize(
    ("field_name", "field_value", "message"),
    [
        ("status", "AVAILABLE", "receipt identity is invalid"),
        ("assurance_profile", "RELEASE_QC", "receipt assurance profile differs"),
    ],
)
def test_as_held_hwm_v3_receipt_rejects_coherent_release_relabel(
    field_name: str,
    field_value: str,
    message: str,
) -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    receipt[field_name] = field_value
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = _fixture_sha256(unsigned)

    with pytest.raises(HwmCoverageError, match=message):
        verify_as_held_hwm_receipt(
            receipt,
            policy_bytes=fixture["policy_bytes"],  # type: ignore[arg-type]
            product_contract_bytes=fixture["product_contract_bytes"],  # type: ignore[arg-type]
            performance_policy_bytes=fixture["performance_policy_bytes"],  # type: ignore[arg-type]
            legacy_policy_bytes=fixture["legacy_policy_bytes"],  # type: ignore[arg-type]
            calendar_bytes=fixture["calendar_bytes"],  # type: ignore[arg-type]
            input_generation_bytes=fixture["input_generation_bytes"],  # type: ignore[arg-type]
        )


def test_as_held_hwm_v3_receipt_rejects_coherent_component_mode_relabel() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    receipt["periods"][0]["components"][0]["component_financial_mode"] = (  # type: ignore[index]
        "DETAILED_RECONCILED_LEVERAGE"
    )
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = _fixture_sha256(unsigned)

    with pytest.raises(
        HwmCoverageError,
        match="component financial mode differs from input evidence",
    ):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_v3_receipt_rejects_coherent_component_return_rewrite() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    first_components = receipt["periods"][0]["components"]  # type: ignore[index]
    first_components[0]["period_return"] = "0.2"
    first_components[1]["period_return"] = "-0.2"
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = _fixture_sha256(unsigned)

    with pytest.raises(
        HwmCoverageError,
        match="calculated component differs from input evidence",
    ):
        as_held_hwm_receipt(receipt)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "beginning_gross_assets_usd",
        "beginning_financing_liability_usd",
        "ending_gross_assets_usd",
        "ending_financing_liability_usd",
        "margin_principal_flow_usd",
        "financing_cost_usd",
        "gross_asset_return",
        "leverage_evidence_sha256",
    ],
)
def test_as_held_hwm_v3_direct_receipt_rejects_forbidden_component_fields(
    forbidden_field: str,
) -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    component = receipt["input_evidence"]["component_periods"][0]  # type: ignore[index]
    component[forbidden_field] = (
        "a" * 64 if forbidden_field == "leverage_evidence_sha256" else "0"
    )
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="receipt input component keys differ"):
        as_held_hwm_receipt(receipt)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "beginning_gross_assets_usd",
        "beginning_financing_liability_usd",
        "ending_gross_assets_before_flow_usd",
        "ending_financing_liability_before_flow_usd",
        "margin_principal_flow_after_subperiod_usd",
        "financing_cost_usd",
    ],
)
def test_as_held_hwm_v3_direct_receipt_rejects_forbidden_subperiod_fields(
    forbidden_field: str,
) -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    subperiod = receipt["input_evidence"]["component_periods"][0]["subperiods"][0]  # type: ignore[index]
    subperiod[forbidden_field] = "0"
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="receipt input subperiod keys differ"):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_v3_receipt_rejects_coherent_subperiod_value_rewrite() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    component = receipt["input_evidence"]["component_periods"][0]  # type: ignore[index]
    subperiod = component["subperiods"][0]
    subperiod["ending_nav_before_flow_usd"] = "999"
    component["return_evidence_sha256"] = _fixture_sha256(
        _direct_return_evidence_payload(component)
    )
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="component ending NAV differs"):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_v3_receipt_rejects_subperiod_evidence_hash_rewrite() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    subperiod = receipt["input_evidence"]["component_periods"][0]["subperiods"][0]  # type: ignore[index]
    subperiod["flow_event_evidence_sha256"] = "a" * 64
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(
        HwmCoverageError,
        match="component flow_reconciliation_sha256 identity differs",
    ):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_v3_detailed_receipt_rejects_subperiod_leverage_rewrite() -> None:
    receipt = as_held_hwm_receipt(_compute_as_held(_as_held_detailed_v3_fixture()))
    subperiod = receipt["input_evidence"]["component_periods"][0]["subperiods"][0]  # type: ignore[index]
    subperiod["ending_gross_assets_before_flow_usd"] = "999"
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="subperiod leverage semantics differ"):
        as_held_hwm_receipt(receipt)


def test_generation_binding_rejects_coherent_unadmitted_subperiod_evidence() -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    component = receipt["input_evidence"]["component_periods"][0]  # type: ignore[index]
    subperiod = component["subperiods"][0]
    subperiod["flow_event_evidence_sha256"] = "a" * 64
    evidence_common = {
        "schema_version": 2,
        "component_financial_mode": component["component_financial_mode"],
        "strategy_id": component["strategy_id"],
        "interval_id": component["interval_id"],
    }
    component["flow_reconciliation_sha256"] = _fixture_sha256(
        {
            **evidence_common,
            "evidence_type": "FLOW_RECONCILIATION",
            "flow_timing": component["flow_timing"],
            "external_flow_usd": component["external_flow_usd"],
            "internal_transfer_usd": component["internal_transfer_usd"],
            "subperiod_flow_evidence_sha256s": [
                item["flow_event_evidence_sha256"] for item in component["subperiods"]
            ],
        }
    )
    component["return_evidence_sha256"] = _fixture_sha256(
        _direct_return_evidence_payload(component)
    )
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="flow event coverage differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            fixture["input_generation_bytes"],  # type: ignore[arg-type]
            **_generation_binding_kwargs(fixture),
        )


def test_generation_binding_rejects_declared_component_mode_mismatch() -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["component_financial_modes"] = ["DETAILED_RECONCILED_LEVERAGE"]
    generation_bytes = _bind_receipt_to_generation(receipt, generation)

    with pytest.raises(HwmCoverageError, match="component financial modes differ"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )


def test_generation_binding_rejects_membership_payload_rewrite() -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    membership = receipt["input_evidence"]["memberships"][0]  # type: ignore[index]
    membership["effective_start"] = "2023-12-31"
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="membership evidence identity differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            fixture["input_generation_bytes"],  # type: ignore[arg-type]
            **_generation_binding_kwargs(fixture),
        )


def test_generation_binding_rejects_authority_header_mismatch() -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["authority_bindings"]["calendar_sha256"] = "b" * 64
    generation_bytes = _bind_receipt_to_generation(receipt, generation)

    with pytest.raises(HwmCoverageError, match="generation authority differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )


def test_generation_binding_rejects_incomplete_evidence_registry() -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    calendar = json.loads(fixture["calendar_bytes"])  # type: ignore[arg-type]
    generation["evidence_sha256s"].remove(calendar["authority_evidence_sha256"])
    generation_bytes = _bind_receipt_to_generation(receipt, generation)

    with pytest.raises(HwmCoverageError, match="generation evidence differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )


def test_generation_binding_rejects_unused_flow_event() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="10")
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    extra_event = deepcopy(generation["flow_events"][0])
    extra_event["event_id"] = f"{extra_event['event_id']}:unused"
    extra_event["event_evidence_sha256"] = _fixture_sha256(
        {
            key: value
            for key, value in extra_event.items()
            if key != "event_evidence_sha256"
        }
    )
    generation["flow_events"].append(extra_event)
    generation["evidence_sha256s"] = sorted(
        {*generation["evidence_sha256s"], extra_event["event_evidence_sha256"]}
    )
    generation_bytes = _bind_receipt_to_generation(receipt, generation)

    with pytest.raises(HwmCoverageError, match="flow event coverage differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )


@pytest.mark.parametrize(
    "authority_field",
    [
        "product_contract_sha256",
        "performance_policy_sha256",
        "legacy_policy_sha256",
    ],
)
def test_generation_binding_rejects_untrusted_authority_hash(
    authority_field: str,
) -> None:
    fixture = _as_held_direct_net_return_fixture()
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["authority_bindings"][authority_field] = "b" * 64
    generation_bytes = _bind_receipt_to_generation(receipt, generation)

    with pytest.raises(HwmCoverageError, match="generation authority differs"):
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )


def test_as_held_hwm_receipt_rejects_missing_independent_capital_anchor() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    component = receipt["input_evidence"]["component_periods"][0]  # type: ignore[index]
    component["capital_basis"] = "ROLLFORWARD_FROM_PRIOR_INTERVAL"
    component["capital_evidence_sha256"] = _fixture_sha256(
        {
            "schema_version": 2,
            "component_financial_mode": component["component_financial_mode"],
            "strategy_id": component["strategy_id"],
            "interval_id": component["interval_id"],
            "evidence_type": "BEGINNING_CAPITAL",
            "beginning_capital_usd": component["beginning_capital_usd"],
            "capital_basis": component["capital_basis"],
        }
    )
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="independent capital anchor"):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_receipt_rejects_membership_exit_with_residual_nav() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    membership = receipt["input_evidence"]["memberships"][0]  # type: ignore[index]
    membership["effective_end"] = "2026-08-11"
    _refresh_membership_evidence(membership)
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="membership exit retains residual NAV"):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_receipt_rejects_zero_nav_for_ongoing_membership() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="-100")
    membership = fixture["memberships"][0]  # type: ignore[index]
    membership["effective_end"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)
    receipt = as_held_hwm_receipt(_compute_as_held(fixture))
    receipt_membership = receipt["input_evidence"]["memberships"][0]  # type: ignore[index]
    receipt_membership["effective_end"] = None
    _refresh_membership_evidence(receipt_membership)
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(
        HwmCoverageError, match="ongoing membership has zero ending NAV"
    ):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_receipt_rejects_unhashable_schema_version() -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    receipt["schema_version"] = []
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match="receipt identity is invalid"):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_v3_numeric_schema_version_round_trips_as_integer() -> None:
    fixture = _as_held_direct_net_return_fixture()
    generation = json.loads(fixture["input_generation_bytes"])  # type: ignore[arg-type]
    generation["schema_version"] = 3.0
    generation_bytes = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    generation_sha256 = hashlib.sha256(generation_bytes).hexdigest()
    periods = fixture["component_periods"]
    assert isinstance(periods, list)
    for period in periods:
        assert isinstance(period, dict)
        period["portable_input_generation_sha256"] = generation_sha256
    fixture["input_generation_bytes"] = generation_bytes

    receipt = as_held_hwm_receipt(_compute_as_held(fixture))

    assert receipt["schema_version"] == 3
    assert isinstance(receipt["schema_version"], int)
    assert (
        validate_as_held_hwm_receipt_generation_binding(
            receipt,
            generation_bytes,
            **_generation_binding_kwargs(fixture),
        )
        == receipt
    )
    verify_as_held_hwm_receipt(
        receipt,
        policy_bytes=fixture["policy_bytes"],  # type: ignore[arg-type]
        product_contract_bytes=fixture["product_contract_bytes"],  # type: ignore[arg-type]
        performance_policy_bytes=fixture["performance_policy_bytes"],  # type: ignore[arg-type]
        legacy_policy_bytes=fixture["legacy_policy_bytes"],  # type: ignore[arg-type]
        calendar_bytes=fixture["calendar_bytes"],  # type: ignore[arg-type]
        input_generation_bytes=generation_bytes,
    )


@pytest.mark.parametrize(
    ("collection_name", "expected_error"),
    [
        ("accepted_intervals", "receipt input interval keys differ"),
        ("memberships", "receipt input membership keys differ"),
    ],
)
def test_as_held_hwm_v3_receipt_rejects_forbidden_evidence_row_fields(
    collection_name: str,
    expected_error: str,
) -> None:
    receipt = as_held_hwm_receipt(
        _compute_as_held(_as_held_direct_net_return_fixture())
    )
    evidence = receipt["input_evidence"]
    assert isinstance(evidence, dict)
    rows = evidence[collection_name]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    row["unexpected"] = "forbidden"
    _refresh_receipt_self_hashes(receipt)

    with pytest.raises(HwmCoverageError, match=expected_error):
        as_held_hwm_receipt(receipt)


def test_as_held_hwm_receipt_generation_binding_rejects_schema_downgrade() -> None:
    v2_receipt = as_held_hwm_receipt(_compute_as_held(_as_held_hwm_fixture()))
    v3_fixture = _as_held_direct_net_return_fixture()

    with pytest.raises(HwmCoverageError, match="generation identity differs"):
        validate_as_held_hwm_receipt_generation_binding(
            v2_receipt,
            v3_fixture["input_generation_bytes"],
            **_generation_binding_kwargs(v3_fixture),
        )


def test_as_held_hwm_receipt_is_deterministic_and_tamper_evident() -> None:
    fixture = _as_held_hwm_fixture()
    result = _compute_as_held(fixture)
    receipt = as_held_hwm_receipt(result)
    receipt_bytes = as_held_hwm_receipt_bytes(result)

    assert json.loads(receipt_bytes) == receipt
    assert receipt_bytes == as_held_hwm_receipt_bytes(result)
    schema = json.loads(
        (
            _REPO_ROOT / "schemas" / "portfolio" / "hwm-calculation-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(schema["required"]) == set(receipt)
    assert set(schema["$defs"]["input_component"]["required"]) == set(
        receipt["input_evidence"]["component_periods"][0]
    )
    assert set(schema["$defs"]["input_subperiod"]["required"]) == set(
        receipt["input_evidence"]["component_periods"][0]["subperiods"][0]
    )
    generation = json.loads(fixture["input_generation_bytes"])
    generation_schema = json.loads(
        (
            _REPO_ROOT / "schemas" / "portfolio" / "hwm-input-generation.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(generation_schema["required"]) == set(generation)
    assert generation_schema["properties"]["authority_profile"]["const"] == (
        "PORTABLE_SYNTHETIC"
    )
    flow_fixture = _single_strategy_flow_fixture(external_flow="10")
    flow_generation = json.loads(flow_fixture["input_generation_bytes"])
    assert set(generation_schema["$defs"]["flow_event"]["required"]) == set(
        flow_generation["flow_events"][0]
    )
    assert generation_schema["$defs"]["flow_event"]["allOf"] == [
        {
            "if": {
                "properties": {"capital_treatment": {"const": "INTERNAL_TRANSFER"}},
                "required": ["capital_treatment"],
            },
            "then": {
                "properties": {
                    "transfer_group_id": {"type": "string", "minLength": 1},
                    "counterparty_strategy_id": {
                        "type": "string",
                        "minLength": 1,
                    },
                }
            },
            "else": {
                "properties": {
                    "transfer_group_id": {"type": "null"},
                    "counterparty_strategy_id": {"type": "null"},
                }
            },
        }
    ]
    assert receipt["current_index"] == "93.6"
    assert receipt["current_hwm"] == "104"
    assert receipt["maximum_drawdown"] == "-0.1"
    assert [
        row["strategy_id"] for row in receipt["complete_through_limiting_inputs"]
    ] == ["strategy-a", "strategy-b"]
    verification_args = {
        "policy_bytes": _PORT_HWM4_POLICY_BYTES,
        "product_contract_bytes": _PRODUCT_CONTRACT_BYTES,
        "performance_policy_bytes": _PERFORMANCE_POLICY_BYTES,
        "legacy_policy_bytes": _LEGACY_HWM_POLICY_BYTES,
        "calendar_bytes": fixture["calendar_bytes"],
        "input_generation_bytes": fixture["input_generation_bytes"],
    }
    verify_as_held_hwm_receipt(receipt, **verification_args)  # type: ignore[arg-type]

    tampered = deepcopy(result)
    tampered["current_index"] = Decimal("104")
    with pytest.raises(HwmCoverageError, match="receipt identity differs"):
        as_held_hwm_receipt(tampered)

    coherently_tampered = deepcopy(receipt)
    coherently_tampered["current_index"] = "104"
    unsigned = {
        key: value
        for key, value in coherently_tampered.items()
        if key != "receipt_sha256"
    }
    coherently_tampered["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(HwmCoverageError, match="summary differs"):
        as_held_hwm_receipt(coherently_tampered)
    with pytest.raises(HwmCoverageError, match="summary differs"):
        verify_as_held_hwm_receipt(
            coherently_tampered,
            **verification_args,  # type: ignore[arg-type]
        )


def test_as_held_hwm_development_proof_warns_on_complete_data_age() -> None:
    fixture = _as_held_hwm_fixture()
    result = _compute_as_held(fixture)
    assert result["status"] == "PORTABLE_SYNTHETIC_PROOF"
    assert result["warnings"] == ["HWM_COMPLETE_DATA_AGE"]


@pytest.mark.parametrize("profile", ["WORKSTATION_QC", "RELEASE_QC"])
def test_as_held_hwm_portable_inputs_cannot_claim_assurance(profile: str) -> None:
    fixture = _as_held_hwm_fixture()
    fixture["assurance_profile"] = profile
    with pytest.raises(HwmCoverageError, match="development proof only"):
        _compute_as_held(fixture)


def test_as_held_hwm_coherent_calendar_rewrite_cannot_gain_authority() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["_calendar_policy_id_override"] = "FORGED_UNAPPROVED_CALENDAR"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="calendar authority integration"):
        _compute_as_held(fixture)


def test_as_held_hwm_portable_receipt_cannot_be_relabelled_available() -> None:
    receipt = as_held_hwm_receipt(_compute_as_held(_as_held_hwm_fixture()))
    forged = deepcopy(receipt)
    forged["status"] = "AVAILABLE"
    unsigned = {key: value for key, value in forged.items() if key != "receipt_sha256"}
    forged["receipt_sha256"] = _fixture_sha256(unsigned)

    with pytest.raises(HwmCoverageError, match="receipt identity is invalid"):
        as_held_hwm_receipt(forged)


def test_as_held_hwm_freshness_threshold_is_bound_to_input_receipt() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["maximum_age_days"] = 4

    with pytest.raises(HwmCoverageError, match="freshness threshold differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_wrong_non_usd_fx_conversion() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="120")
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    events[0].update(
        original_amount="100",
        original_currency="EUR",
        fx_rate_to_usd="1.2",
        fx_authority_id="SYNTHETIC_EURUSD_EVENT_TIME",
        usd_amount="100",
    )
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="FX conversion differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_fx_validation_uses_fixed_decimal_context() -> None:
    from decimal import ROUND_DOWN, getcontext, localcontext

    fixture = _single_strategy_flow_fixture()
    product_sha256 = hashlib.sha256(fixture["product_contract_bytes"]).hexdigest()
    event = {
        "event_id": "strategy-a:d1:d1-close:DIVIDEND",
        "strategy_id": "strategy-a",
        "interval_id": "d1",
        "subperiod_id": "d1-close",
        "event_type": "DIVIDEND",
        "capital_treatment": "INVESTMENT_RESULT",
        "effective_at_utc": "2026-08-11T00:00:00Z",
        "effective_timezone": "UTC",
        "timing_basis": "DATE_ONLY_END_OF_DAY",
        "original_amount": "1234567890123456789012345678901234567891",
        "original_currency": "EUR",
        "fx_rate_to_usd": "0.0000000000000000000000000000000000000001",
        "fx_authority_id": "SYNTHETIC_EURUSD_EVENT_TIME",
        "fx_generation_sha256": "e" * 64,
        "usd_amount": "0.1234567890123456789012345678901234567891",
        "correction_kind": "ORIGINAL",
        "related_event_id": None,
        "transfer_group_id": None,
        "counterparty_strategy_id": None,
        "classification_authority_sha256": product_sha256,
        "event_evidence_sha256": "0" * 64,
    }
    fixture["_flow_events_override"] = [event]
    _refresh_fixture_authorities(fixture)

    precise = _as_held_hwm_fixture()
    precise["accepted_intervals"] = precise["accepted_intervals"][:1]
    precise["component_periods"] = []
    with localcontext() as context:
        context.prec = 34
        precise["component_periods"] = [
            _as_held_component(
                "strategy-a",
                "d1",
                "2026-08-10",
                "2026-08-11",
                "1.0000000000000000000000000001",
                "0",
            ),
            _as_held_component(
                "strategy-b",
                "d1",
                "2026-08-10",
                "2026-08-11",
                "1.0000000000000000000000000002",
                "0",
            ),
        ]
    precise["complete_through"] = "2026-08-11"
    precise["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(precise)
    precise_result = _compute_as_held(precise)

    saved_prec = getcontext().prec
    saved_rounding = getcontext().rounding
    try:
        getcontext().prec = 5
        getcontext().rounding = ROUND_DOWN
        result = _compute_as_held(fixture)
        precise_receipt = as_held_hwm_receipt(precise_result)
        as_held_hwm_receipt_bytes(precise_result)
        verify_as_held_hwm_receipt(
            precise_receipt,
            policy_bytes=precise["policy_bytes"],
            product_contract_bytes=precise["product_contract_bytes"],
            performance_policy_bytes=precise["performance_policy_bytes"],
            legacy_policy_bytes=precise["legacy_policy_bytes"],
            calendar_bytes=precise["calendar_bytes"],
            input_generation_bytes=precise["input_generation_bytes"],
        )
    finally:
        getcontext().prec = saved_prec
        getcontext().rounding = saved_rounding

    assert result["current_index"] == Decimal("100")
    assert precise_receipt["current_index"] == "100"


def test_as_held_hwm_rejects_opening_basis_after_membership_start() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:2]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        fixture["component_periods"][0],
        _as_held_component(
            "strategy-a",
            "d2",
            "2026-08-11",
            "2026-08-12",
            "660",
            "0",
            external_flow="10",
            capital_basis="ROLLFORWARD_FROM_PRIOR_INTERVAL",
        ),
    ]
    fixture["complete_through"] = "2026-08-12"
    fixture["evaluated_on"] = "2026-08-12"
    _refresh_fixture_authorities(fixture)
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    events[0].update(
        event_type="OPENING_BASIS",
        effective_at_utc="2026-08-11T00:00:00Z",
        timing_basis="EXACT_TIMESTAMP",
    )
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="funded membership start"):
        _compute_as_held(fixture)


def test_as_held_hwm_binds_opening_basis_once_and_preserves_later_reversal() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:2]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        _as_held_component("strategy-a", "d1", "2026-08-10", "2026-08-11", "100", "0"),
        _as_held_component(
            "strategy-a",
            "d2",
            "2026-08-11",
            "2026-08-12",
            "100",
            "0",
            external_flow="-10",
            capital_basis="ROLLFORWARD_FROM_PRIOR_INTERVAL",
        ),
    ]
    fixture["complete_through"] = "2026-08-12"
    fixture["evaluated_on"] = "2026-08-12"
    _refresh_fixture_authorities(fixture)
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    reversal = events[0]
    original = deepcopy(reversal)
    original.update(
        event_id="strategy-a:d1:d1-close:OPENING_BASIS",
        interval_id="d1",
        subperiod_id="d1-close",
        event_type="OPENING_BASIS",
        effective_at_utc="2026-08-10T00:00:00Z",
        timing_basis="EXACT_TIMESTAMP",
        original_amount="10",
        usd_amount="10",
        correction_kind="ORIGINAL",
        related_event_id=None,
    )
    reversal.update(
        event_type="OPENING_BASIS",
        correction_kind="ECONOMIC_REVERSAL",
        related_event_id=original["event_id"],
    )
    fixture["_flow_events_override"] = [original, reversal]
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("100")
    assert result["periods"][1]["components"][0]["period_return"] == Decimal("0")

    duplicate = deepcopy(fixture)
    duplicate_events = deepcopy(duplicate["_flow_events_override"])
    duplicate_opening = deepcopy(original)
    duplicate_opening["event_id"] = f"{original['event_id']}:duplicate"
    duplicate_events.append(duplicate_opening)
    duplicate["_flow_events_override"] = duplicate_events
    _refresh_fixture_authorities(duplicate)
    with pytest.raises(HwmCoverageError, match="funded membership start"):
        _compute_as_held(duplicate)


def test_as_held_hwm_rejects_dividend_misclassified_as_external_flow() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="10")
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    events[0].update(event_type="DIVIDEND", capital_treatment="INVESTMENT_RESULT")
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    with pytest.raises(
        HwmCoverageError, match="flow facts differ from admitted events"
    ):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_margin_principal_as_owner_flow() -> None:
    fixture = _single_strategy_flow_fixture(
        margin_principal_flow="50", financing_liability="50"
    )
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    events[0]["capital_treatment"] = "EXTERNAL_FLOW"
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="classification differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_survivor_recalculation() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["component_periods"] = [
        row
        for row in fixture["component_periods"]
        if not (row["strategy_id"] == "strategy-b" and row["interval_id"] == "d2")
    ]

    with pytest.raises(HwmCoverageError, match="membership coverage differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_applies_effective_entry_and_exit_boundaries() -> None:
    fixture = _as_held_hwm_fixture()
    memberships = fixture["memberships"]
    memberships[1]["effective_start"] = "2026-08-11"
    memberships[1]["effective_end"] = "2026-08-12"
    fixture["component_periods"] = [
        row
        for row in fixture["component_periods"]
        if row["strategy_id"] != "strategy-b" or row["interval_id"] == "d2"
    ]
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-b",
            "d2",
            "2026-08-11",
            "2026-08-12",
            "380",
            "-0.10",
            external_flow="-342",
        )
        if row["strategy_id"] == "strategy-b"
        else row
        for row in fixture["component_periods"]
    ]
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert [row["index"] for row in result["index_path"]] == [
        Decimal("100"),
        Decimal("110"),
        Decimal("99"),
        Decimal("99"),
    ]
    assert [
        [component["strategy_id"] for component in period["components"]]
        for period in result["periods"]
    ] == [["strategy-a"], ["strategy-a", "strategy-b"], ["strategy-a"]]


def test_as_held_hwm_rejects_membership_exit_with_residual_nav() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["memberships"][1]["effective_start"] = "2026-08-11"
    fixture["memberships"][1]["effective_end"] = "2026-08-12"
    fixture["component_periods"] = [
        row
        for row in fixture["component_periods"]
        if row["strategy_id"] != "strategy-b" or row["interval_id"] == "d2"
    ]
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="residual net NAV"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_zero_nav_for_continuing_membership() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="-100")

    with pytest.raises(HwmCoverageError, match="ending net NAV is invalid"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_component_outside_membership() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["memberships"][1]["effective_start"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="outside effective membership"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_terminal_anchor_backcast() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["component_periods"][0]["capital_basis"] = "ROLLFORWARD_FROM_PRIOR_INTERVAL"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="independent capital anchor"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_unreconciled_rollforward() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["component_periods"][2]["capital_basis"] = "ROLLFORWARD_FROM_PRIOR_INTERVAL"
    fixture["component_periods"][2]["beginning_capital_usd"] = "659"
    fixture["component_periods"][2]["beginning_gross_assets_usd"] = "659"
    fixture["component_periods"][2]["ending_gross_assets_usd"] = "593.1"
    fixture["component_periods"][2]["ending_nav_usd"] = "593.1"
    fixture["component_periods"][2]["flow_adjusted_ending_nav_usd"] = "593.1"
    fixture["component_periods"][2]["subperiods"][0]["beginning_nav_usd"] = "659"
    fixture["component_periods"][2]["subperiods"][0]["ending_nav_before_flow_usd"] = (
        "593.1"
    )
    fixture["component_periods"][2]["subperiods"][0]["beginning_gross_assets_usd"] = (
        "659"
    )
    fixture["component_periods"][2]["subperiods"][0][
        "ending_gross_assets_before_flow_usd"
    ] = "593.1"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(
        HwmCoverageError, match="beginning capital differs from prior ending net NAV"
    ):
        _compute_as_held(fixture)


def test_as_held_hwm_internal_transfer_does_not_change_composite() -> None:
    baseline = _compute_as_held(_as_held_hwm_fixture())
    fixture = _as_held_hwm_fixture()
    fixture["component_periods"][0:4] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "600",
            "0.10",
            internal_transfer="-100",
        ),
        _as_held_component(
            "strategy-b",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "400",
            "-0.05",
            internal_transfer="100",
        ),
        _as_held_component(
            "strategy-a", "d2", "2026-08-11", "2026-08-12", "560", "-0.10"
        ),
        _as_held_component(
            "strategy-b", "d2", "2026-08-11", "2026-08-12", "480", "-0.10"
        ),
    ]
    fixture["component_periods"][4:6] = [
        _as_held_component("strategy-a", "d3", "2026-08-12", "2026-08-13", "504", "0"),
        _as_held_component("strategy-b", "d3", "2026-08-12", "2026-08-13", "432", "0"),
    ]
    _refresh_fixture_authorities(fixture)

    transferred = _compute_as_held(fixture)

    assert [row["index"] for row in transferred["index_path"]] == [
        row["index"] for row in baseline["index_path"]
    ]
    assert transferred["periods"][1]["total_beginning_capital_usd"] == Decimal("1040")

    mixed_pair = deepcopy(fixture)
    mixed_events = json.loads(mixed_pair["input_generation_bytes"])["flow_events"]
    mixed_events[0]["event_type"] = "PREDECESSOR_TRANSITION"
    mixed_pair["_flow_events_override"] = mixed_events
    _refresh_fixture_authorities(mixed_pair)
    with pytest.raises(HwmCoverageError, match="internal transfer pairing differs"):
        _compute_as_held(mixed_pair)


def test_as_held_hwm_rejects_unpaired_internal_transfer() -> None:
    fixture = _as_held_hwm_fixture()
    row = fixture["component_periods"][0]
    row["internal_transfer_usd"] = "1"
    row["ending_gross_assets_usd"] = "661"
    row["ending_nav_usd"] = "661"
    row["flow_timing"] = "DATE_ONLY_END_OF_DAY"
    row["subperiods"][0]["internal_transfer_after_subperiod_usd"] = "1"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="internal transfer pairing differs"):
        _compute_as_held(fixture)


def test_as_held_hwm_pairs_predecessor_transfer_across_membership_boundary() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:2]
    fixture["memberships"] = [
        {
            "strategy_id": "predecessor",
            "effective_start": "2026-08-10",
            "effective_end": "2026-08-11",
            "membership_evidence_sha256": "1" * 64,
        },
        {
            "strategy_id": "successor",
            "effective_start": "2026-08-11",
            "effective_end": None,
            "membership_evidence_sha256": "2" * 64,
        },
    ]
    fixture["component_periods"] = [
        _as_held_component(
            "predecessor",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0",
            internal_transfer="-100",
        ),
        _as_held_component("successor", "d2", "2026-08-11", "2026-08-12", "100", "0"),
    ]
    fixture["complete_through"] = "2026-08-12"
    fixture["evaluated_on"] = "2026-08-12"
    _refresh_fixture_authorities(fixture)
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    predecessor = events[0]
    predecessor.update(
        event_type="PREDECESSOR_TRANSITION",
        transfer_group_id="predecessor-to-successor",
        counterparty_strategy_id="successor",
    )
    successor = deepcopy(predecessor)
    successor.update(
        event_id="successor:d2:d2-close:PREDECESSOR_TRANSITION",
        strategy_id="successor",
        interval_id="d2",
        subperiod_id="d2-close",
        effective_at_utc="2026-08-11T00:00:00Z",
        timing_basis="EXACT_TIMESTAMP",
        original_amount="100",
        usd_amount="100",
        counterparty_strategy_id="predecessor",
    )
    fixture["_flow_events_override"] = [predecessor, successor]
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("100")
    assert [
        [component["strategy_id"] for component in period["components"]]
        for period in result["periods"]
    ] == [["predecessor"], ["successor"]]

    invalid = deepcopy(fixture)
    invalid_events = deepcopy(invalid["_flow_events_override"])
    invalid_events[1]["counterparty_strategy_id"] = "not-the-predecessor"
    invalid["_flow_events_override"] = invalid_events
    _refresh_fixture_authorities(invalid)
    with pytest.raises(HwmCoverageError, match="internal transfer pairing differs"):
        _compute_as_held(invalid)

    wrong_boundary = deepcopy(fixture)
    wrong_boundary_events = deepcopy(wrong_boundary["_flow_events_override"])
    wrong_boundary_events[1]["effective_at_utc"] = "2026-08-12T00:00:00Z"
    wrong_boundary["_flow_events_override"] = wrong_boundary_events
    _refresh_fixture_authorities(wrong_boundary)
    with pytest.raises(HwmCoverageError, match="boundary differs"):
        _compute_as_held(wrong_boundary)


def test_as_held_hwm_rejects_calendar_interval_gap() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"][2]["interval_start"] = "2026-08-13"
    fixture["accepted_intervals"][2]["interval_end"] = "2026-08-14"

    with pytest.raises(HwmCoverageError, match="not contiguous"):
        _compute_as_held(fixture)


def test_as_held_hwm_uses_net_nav_for_leveraged_total_return() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0.14",
            financing_liability="50",
            financing_cost="1",
        )
    ]
    fixture["complete_through"] = "2026-08-11"
    fixture["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("114")
    assert result["periods"][0]["components"][0]["period_return"] == Decimal("0.14")
    assert fixture["component_periods"][0]["ending_gross_assets_usd"] == "164.00"
    assert fixture["component_periods"][0]["ending_nav_usd"] == "114.00"
    assert fixture["component_periods"][0]["gross_asset_return"] == "0.10"
    assert fixture["component_periods"][0]["financing_cost_usd"] == "1"

    forged = deepcopy(fixture)
    forged_period = forged["component_periods"][0]
    forged_period["period_return"] = "0.64"
    forged_period["flow_adjusted_ending_nav_usd"] = "164"
    with pytest.raises(HwmCoverageError, match="subperiod TWR"):
        _compute_as_held(forged)

    missing_leverage = deepcopy(fixture)
    missing_leverage["component_periods"][0].pop("leverage_evidence_sha256")
    with pytest.raises(HwmCoverageError, match=r"component_periods\[0\] keys differ"):
        _compute_as_held(missing_leverage)

    altered_financing = deepcopy(fixture)
    altered_financing["component_periods"][0]["financing_cost_usd"] = "0"
    altered_financing["component_periods"][0]["subperiods"][0]["financing_cost_usd"] = (
        "0"
    )
    _refresh_fixture_authorities(altered_financing)
    with pytest.raises(HwmCoverageError, match="gross asset return differs"):
        _compute_as_held(altered_financing)


def test_as_held_hwm_margin_principal_changes_leverage_not_return() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0",
            financing_liability="50",
            beginning_financing_liability="0",
            margin_principal_flow="50",
        )
    ]
    fixture["complete_through"] = "2026-08-11"
    fixture["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("100")
    component = fixture["component_periods"][0]
    assert component["ending_gross_assets_usd"] == "150"
    assert component["ending_financing_liability_usd"] == "50"
    assert component["ending_nav_usd"] == "100"
    assert component["external_flow_usd"] == "0"


def test_as_held_hwm_rejects_self_asserted_evidence_rewrite() -> None:
    fixture = _as_held_hwm_fixture()
    row = fixture["component_periods"][0]
    old_evidence = row["capital_evidence_sha256"]
    row["capital_evidence_sha256"] = "a" * 64
    generation = json.loads(fixture["input_generation_bytes"])
    generation["evidence_sha256s"] = sorted(
        "a" * 64 if value == old_evidence else value
        for value in generation["evidence_sha256s"]
    )
    fixture["input_generation_bytes"] = (
        json.dumps(
            generation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    rewritten_generation_sha256 = hashlib.sha256(
        fixture["input_generation_bytes"]
    ).hexdigest()
    for component in fixture["component_periods"]:
        component["source_generation_sha256"] = rewritten_generation_sha256

    with pytest.raises(
        HwmCoverageError, match="capital_evidence_sha256 identity differs"
    ):
        _compute_as_held(fixture)


def test_as_held_hwm_exact_timestamp_flow_uses_segmented_twr() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    row = _as_held_component(
        "strategy-a",
        "d1",
        "2026-08-10",
        "2026-08-11",
        "100",
        "0.10",
        external_flow="100",
    )
    row.update(
        ending_gross_assets_usd="220",
        ending_nav_usd="220",
        flow_adjusted_ending_nav_usd="110",
        flow_timing="EXACT_TIMESTAMP_SEGMENTED",
        subperiods=[
            {
                "subperiod_id": "d1-before-flow",
                "subperiod_start_utc": "2026-08-10T00:00:00Z",
                "subperiod_end_utc": "2026-08-10T12:00:00Z",
                "beginning_nav_usd": "100",
                "ending_nav_before_flow_usd": "100",
                "beginning_gross_assets_usd": "100",
                "beginning_financing_liability_usd": "0",
                "ending_gross_assets_before_flow_usd": "100",
                "ending_financing_liability_before_flow_usd": "0",
                "margin_principal_flow_after_subperiod_usd": "0",
                "financing_cost_usd": "0",
                "external_flow_after_subperiod_usd": "100",
                "internal_transfer_after_subperiod_usd": "0",
                "flow_event_evidence_sha256": "7" * 64,
            },
            {
                "subperiod_id": "d1-after-flow",
                "subperiod_start_utc": "2026-08-10T12:00:00Z",
                "subperiod_end_utc": "2026-08-11T00:00:00Z",
                "beginning_nav_usd": "200",
                "ending_nav_before_flow_usd": "220",
                "beginning_gross_assets_usd": "200",
                "beginning_financing_liability_usd": "0",
                "ending_gross_assets_before_flow_usd": "220",
                "ending_financing_liability_before_flow_usd": "0",
                "margin_principal_flow_after_subperiod_usd": "0",
                "financing_cost_usd": "0",
                "external_flow_after_subperiod_usd": "0",
                "internal_transfer_after_subperiod_usd": "0",
                "flow_event_evidence_sha256": "7" * 64,
            },
        ],
    )
    fixture["component_periods"] = [row]
    fixture["complete_through"] = "2026-08-11"
    fixture["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("110")
    assert result["periods"][0]["composite_return"] == Decimal("0.10")

    reordered = deepcopy(fixture)
    events = json.loads(reordered["input_generation_bytes"])["flow_events"]
    events[0]["effective_at_utc"] = "2026-08-10T13:00:00Z"
    reordered["_flow_events_override"] = events
    _refresh_fixture_authorities(reordered)
    with pytest.raises(HwmCoverageError, match="effective time differs"):
        _compute_as_held(reordered)


def test_as_held_hwm_rejects_duplicate_flow_event_identity() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="10")
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    events.append(deepcopy(events[0]))
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="identity is duplicated"):
        _compute_as_held(fixture)


def test_as_held_hwm_binds_economic_reversal_lineage_and_amount() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:2]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0",
            external_flow="10",
        ),
        _as_held_component(
            "strategy-a",
            "d2",
            "2026-08-11",
            "2026-08-12",
            "110",
            "0",
            external_flow="-10",
            capital_basis="ROLLFORWARD_FROM_PRIOR_INTERVAL",
        ),
    ]
    fixture["complete_through"] = "2026-08-12"
    fixture["evaluated_on"] = "2026-08-12"
    _refresh_fixture_authorities(fixture)
    events = json.loads(fixture["input_generation_bytes"])["flow_events"]
    original = next(event for event in events if event["interval_id"] == "d1")
    reversal = next(event for event in events if event["interval_id"] == "d2")
    reversal.update(
        event_type="OWNER_CONTRIBUTION",
        capital_treatment="EXTERNAL_FLOW",
        correction_kind="ECONOMIC_REVERSAL",
        related_event_id=original["event_id"],
    )
    fixture["_flow_events_override"] = events
    _refresh_fixture_authorities(fixture)

    assert _compute_as_held(fixture)["current_index"] == Decimal("100")

    invalid = deepcopy(fixture)
    invalid_events = deepcopy(events)
    reversal = next(
        event
        for event in invalid_events
        if event["correction_kind"] == "ECONOMIC_REVERSAL"
    )
    reversal.update(original_amount="-9", usd_amount="-9")
    invalid["_flow_events_override"] = invalid_events
    _refresh_fixture_authorities(invalid)
    with pytest.raises(HwmCoverageError, match="economic reversal differs"):
        _compute_as_held(invalid)


def test_as_held_hwm_source_correction_replaces_original_once() -> None:
    fixture = _single_strategy_flow_fixture(external_flow="20")
    correction = json.loads(fixture["input_generation_bytes"])["flow_events"][0]
    original = deepcopy(correction)
    original.update(
        event_id=f"{correction['event_id']}:original",
        original_amount="10",
        usd_amount="10",
        correction_kind="ORIGINAL",
        related_event_id=None,
    )
    correction.update(
        correction_kind="SOURCE_CORRECTION",
        related_event_id=original["event_id"],
    )
    fixture["_flow_events_override"] = [original, correction]
    _refresh_fixture_authorities(fixture)

    assert _compute_as_held(fixture)["current_index"] == Decimal("100")

    duplicate = deepcopy(fixture)
    duplicate_events = deepcopy(fixture["_flow_events_override"])
    second_correction = deepcopy(correction)
    second_correction["event_id"] = f"{correction['event_id']}:duplicate"
    duplicate_events.append(second_correction)
    duplicate["_flow_events_override"] = duplicate_events
    _refresh_fixture_authorities(duplicate)
    with pytest.raises(
        HwmCoverageError, match="source correction effective time differs"
    ):
        _compute_as_held(duplicate)


def test_as_held_hwm_coherent_synthetic_return_rewrite_stays_proof_only() -> None:
    fixture = _single_strategy_flow_fixture()
    fixture["component_periods"] = [
        _as_held_component(
            "strategy-a",
            "d1",
            "2026-08-10",
            "2026-08-11",
            "100",
            "0.25",
        )
    ]
    _refresh_fixture_authorities(fixture)

    result = _compute_as_held(fixture)

    assert result["current_index"] == Decimal("125")
    assert result["status"] == "PORTABLE_SYNTHETIC_PROOF"
    assert result["input_authority_class"] == "PORTABLE_SYNTHETIC"


def test_as_held_hwm_rejects_multi_day_aggregate_interval() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = fixture["component_periods"][:1]
    fixture["accepted_intervals"][0]["interval_end"] = "2026-08-13"
    fixture["component_periods"][0]["interval_end"] = "2026-08-13"
    fixture["complete_through"] = "2026-08-13"
    _refresh_fixture_authorities(fixture)

    with pytest.raises(HwmCoverageError, match="must be exactly one day"):
        _compute_as_held(fixture)


def test_as_held_hwm_rejects_corrupted_authority_bytes() -> None:
    fixture = _as_held_hwm_fixture()
    fixture["product_contract_bytes"] = fixture["product_contract_bytes"] + b" "

    with pytest.raises(HwmCoverageError, match="product authority bytes differ"):
        _compute_as_held(fixture)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update(policy_sha256="0" * 64), "policy identity differs"),
        (lambda row: row.update(calendar_sha256="0" * 64), "calendar identity differs"),
        (lambda row: row.update(period_return="-1"), "greater than negative one"),
        (lambda row: row.update(beginning_capital_usd="0"), "must be positive"),
        (
            lambda row: row.update(
                return_basis="UNLEVERED_GROSS_ASSETS_BEFORE_FINANCING_COST"
            ),
            "return basis is invalid",
        ),
    ],
)
def test_as_held_hwm_rejects_authority_and_financial_semantic_drift(
    mutation: object, message: str
) -> None:
    fixture = _as_held_hwm_fixture()
    mutation(fixture["component_periods"][0])

    with pytest.raises(HwmCoverageError, match=message):
        _compute_as_held(fixture)


def test_as_held_hwm_is_decimal_context_independent() -> None:
    from decimal import ROUND_DOWN, getcontext

    fixture = _as_held_hwm_fixture()
    fixture["accepted_intervals"] = fixture["accepted_intervals"][:1]
    fixture["memberships"] = fixture["memberships"][:1]
    fixture["component_periods"] = [
        {
            **fixture["component_periods"][0],
            "beginning_capital_usd": "3",
            "beginning_gross_assets_usd": "3",
            "ending_gross_assets_usd": "4",
            "ending_financing_liability_usd": "0",
            "ending_nav_usd": "4",
            "external_flow_usd": "0",
            "flow_adjusted_ending_nav_usd": "4",
            "period_return": "0.3333333333333333333333333333333333",
            "gross_asset_return": "0.3333333333333333333333333333333333",
            "subperiods": [
                {
                    **fixture["component_periods"][0]["subperiods"][0],
                    "beginning_nav_usd": "3",
                    "ending_nav_before_flow_usd": "4",
                    "beginning_gross_assets_usd": "3",
                    "ending_gross_assets_before_flow_usd": "4",
                }
            ],
        }
    ]
    fixture["complete_through"] = "2026-08-11"
    fixture["evaluated_on"] = "2026-08-11"
    _refresh_fixture_authorities(fixture)
    reference = _compute_as_held(fixture)
    saved_precision = getcontext().prec
    saved_rounding = getcontext().rounding
    try:
        getcontext().prec = 7
        getcontext().rounding = ROUND_DOWN
        polluted = _compute_as_held(fixture)
    finally:
        getcontext().prec = saved_precision
        getcontext().rounding = saved_rounding

    assert polluted == reference


# ── hwm_inputs consumer-contract conformance ─────────────────────────────


def _hwm_envelope_fixture() -> dict[str, object]:
    """A minimal governed envelope: two AVAILABLE ACCOUNT-lane periods for
    one strategy across two consecutive days, plus one UNAVAILABLE row."""
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-07-01",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-07-01T00:00:00Z",
                "accepted_by": "synthetic",
                "source_artifact_id": "artifact-1",
                "source_artifact_sha256": "a" * 64,
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synth",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": "artifact-1",
            }
        ],
        "periods": [
            {
                "strategy_id": "strategy-live",
                "date": "2026-07-01",
                "lane": "ACCOUNT",
                "disposition": "AVAILABLE",
                "beginning_capital_usd": "100",
                "return_source_record_id": "return-live-2026-07-01",
                "component_account_ids": ["account-a"],
                "flow_status": "OBSERVED",
                "fx_status": "NOT_REQUIRED",
                "flow_source_record_ids": ["flow-1"],
                "fx_source_record_ids": [],
            },
            {
                "strategy_id": "strategy-live",
                "date": "2026-07-02",
                "lane": "ACCOUNT",
                "disposition": "AVAILABLE",
                "beginning_capital_usd": "110",
                "return_source_record_id": "return-live-2026-07-02",
                "component_account_ids": ["account-a"],
                "flow_status": "OBSERVED",
                "fx_status": "NOT_REQUIRED",
                "flow_source_record_ids": ["flow-2"],
                "fx_source_record_ids": [],
            },
            {
                "strategy_id": "strategy-tiny",
                "date": "2026-07-01",
                "lane": "ACCOUNT",
                "disposition": "EXCLUDED_BY_POLICY",
                "beginning_capital_usd": None,
                "return_source_record_id": None,
                "flow_status": "MISSING",
                "fx_status": "MISSING",
            },
        ],
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
        },
    }


# Return-series crosswalk the consumer resolves return references against
# (catalog strategy_returns identity -> Decimal return).
_RETURN_CROSSWALK = {
    "return-live-2026-07-01": Decimal("0.1"),
    "return-live-2026-07-02": Decimal("0.2"),
}


def _available_period_rows(
    envelope: dict[str, object],
) -> list[tuple[str, date_type, Decimal, str]]:
    """The #277 consumer boundary: read only AVAILABLE period rows as
    (strategy_id, date, beginning_capital, return_reference)."""
    rows: list[tuple[str, date_type, Decimal, str]] = []
    for row in envelope["periods"]:  # type: ignore[index]
        if row["disposition"] != "AVAILABLE":
            continue
        rows.append(
            (
                str(row["strategy_id"]),
                date_type.fromisoformat(str(row["date"])),
                Decimal(str(row["beginning_capital_usd"])),
                str(row["return_source_record_id"]),
            )
        )
    return rows


def test_hwm_inputs_periods_drive_the_real_hwm_kernel() -> None:
    """Exercise the real consumer: extract AVAILABLE periods, resolve daily
    returns through the crosswalk, build the beginning-capital-weighted
    composite, and run compute_full_systematic_hwm_index end to end."""
    rows = _available_period_rows(_hwm_envelope_fixture())
    assert rows == [
        (
            "strategy-live",
            date_type(2026, 7, 1),
            Decimal("100"),
            "return-live-2026-07-01",
        ),
        (
            "strategy-live",
            date_type(2026, 7, 2),
            Decimal("110"),
            "return-live-2026-07-02",
        ),
    ]

    # Composite construction rule: beginning-capital weighted per period,
    # then geometrically linked by the kernel.
    by_date: dict[date_type, list[tuple[Decimal, Decimal]]] = {}
    for _sid, day, capital, return_ref in rows:
        by_date.setdefault(day, []).append((capital, _RETURN_CROSSWALK[return_ref]))
    composite_returns = [
        sum(capital * value for capital, value in items)
        / sum(capital for capital, _ in items)
        for _day, items in sorted(by_date.items())
    ]
    assert composite_returns == [Decimal("0.1"), Decimal("0.2")]

    result = compute_full_systematic_hwm_index(
        composite_returns, full_systematic_coverage=True
    )

    assert result["status"] == "AVAILABLE"
    assert result["index_series"] == [
        Decimal("1.0"),
        Decimal("1.10"),
        Decimal("1.320"),
    ]
    assert result["hwm"] == Decimal("1.320")
    assert result["current_drawdown"] == Decimal("0")
    assert result["maximum_drawdown"] == Decimal("0")


def test_hwm_inputs_incomplete_coverage_closes_the_kernel() -> None:
    """A missing member or return makes the period UNAVAILABLE — the kernel
    is never recalculated over survivors."""
    rows = _available_period_rows(_hwm_envelope_fixture())
    # strategy-tiny is EXCLUDED_BY_POLICY and contributes no AVAILABLE row;
    # with full coverage unavailable the engine refuses the composite.
    result = compute_full_systematic_hwm_index(
        [Decimal("0.1")], full_systematic_coverage=False
    )
    assert result == {"status": "UNAVAILABLE"}
    assert len(rows) == 2

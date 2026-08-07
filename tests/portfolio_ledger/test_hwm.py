from __future__ import annotations

import pytest

from quant_engine.portfolio_ledger import HwmCoverageError, governed_hwm_coverage


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

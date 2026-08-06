from __future__ import annotations

import pytest

from quant_engine.portfolio_ledger import HwmCoverageError, governed_hwm_coverage


def _policy() -> dict[str, object]:
    return {
        "accepted_daily_nav_evidence": ["ACCOUNT_LIVE", "PROVIDER_LIVE"],
        "accepted_provider_live_strategy_ids": ["strategy-provider"],
        "excluded_strategy_ids": ["strategy-tiny"],
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
        "excluded_value_usd": "10",
        "excluded_strategies": ["Tiny"],
        "governed_perimeter_value_usd": "90",
        "nav_coverage": "1",
        "status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
        "status_code_version": 2,
        "status_semantics": "EXPLICIT_GOVERNED_HWM_PERIMETER",
        "uncovered_details": [],
        "uncovered_strategies": [],
    }
    assert strategies[0]["hwm_freshness_status"] == "ACCEPTED"
    assert strategies[1]["hwm_scope"] == "EXCLUDED_DE_MINIMIS"


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


def test_governed_hwm_rejects_unknown_policy_exclusion() -> None:
    policy = _policy()
    policy["excluded_strategy_ids"] = ["unknown"]

    with pytest.raises(HwmCoverageError, match="unknown strategy"):
        governed_hwm_coverage(_strategies(), policy)

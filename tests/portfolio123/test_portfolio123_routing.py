from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_engine.portfolio123 import (
    ApiAuthorization,
    Portfolio123RoutingError,
    RouteReason,
    Transport,
    record_api_result,
    route_capability,
)


NOW = datetime(2026, 8, 1, 18, tzinfo=timezone.utc)


def _authorization(operation: str, *, budget: int = 3) -> ApiAuthorization:
    return ApiAuthorization(
        operation=operation,
        estimated_credits=1,
        authorized_credit_budget=budget,
        approval_reference="#18-SYNTHETIC-API-AUTHORIZATION",
        credit_table_captured_at=NOW,
    )


def test_har_is_always_first_when_the_capability_is_supported() -> None:
    decision = route_capability("strategy.transactions")
    assert decision.transport is Transport.HAR
    assert decision.reason is RouteReason.HAR_FIRST
    assert decision.api_operation is None


def test_user_can_explicitly_select_a_budgeted_api_read() -> None:
    decision = route_capability(
        "strategy.transactions",
        user_requested_api=True,
        api_authorization=_authorization("strategy_transactions"),
    )
    assert decision.transport is Transport.API
    assert decision.reason is RouteReason.USER_REQUESTED_API
    assert decision.api_operation == "strategy_transactions"


def test_declared_har_gap_can_use_api_but_only_with_budget_evidence() -> None:
    with pytest.raises(Portfolio123RoutingError, match="authorization"):
        route_capability("strategy.rebalance-recommendations")
    decision = route_capability(
        "strategy.rebalance-recommendations",
        api_authorization=_authorization("strategy_rebalance"),
    )
    assert decision.reason is RouteReason.UNSUPPORTED_BY_HAR


def test_provider_mutation_needs_separate_exact_approval() -> None:
    authorization = _authorization("strategy_rebalance_commit")
    with pytest.raises(Portfolio123RoutingError, match="mutation"):
        route_capability("strategy.rebalance-commit", api_authorization=authorization)
    decision = route_capability(
        "strategy.rebalance-commit",
        api_authorization=authorization,
        provider_mutation_approval="#SYNTHETIC-MUTATION-APPROVAL",
    )
    assert decision.transport is Transport.API


def test_network_or_schema_failure_has_no_automatic_fallback_surface() -> None:
    decision = route_capability("strategy.holdings")
    assert decision.transport is Transport.HAR
    with pytest.raises(TypeError):
        route_capability(  # type: ignore[call-arg]
            "strategy.holdings", har_failure="timeout"
        )


def test_authorization_must_match_operation_and_budget() -> None:
    with pytest.raises(Portfolio123RoutingError, match="does not match"):
        route_capability(
            "screen.run", api_authorization=_authorization("screen_backtest")
        )
    with pytest.raises(Portfolio123RoutingError, match="exceeds"):
        ApiAuthorization(
            operation="screen_run",
            estimated_credits=4,
            authorized_credit_budget=3,
            approval_reference="#SYNTHETIC",
            credit_table_captured_at=NOW,
        ).validate()


def test_actual_response_cost_is_retained_and_budget_enforced() -> None:
    decision = route_capability(
        "screen.run", api_authorization=_authorization("screen_run", budget=2)
    )
    evidence = record_api_result(decision, actual_credits=2, quota_remaining=98)
    assert evidence.actual_credits == 2
    assert evidence.quota_remaining == 98
    with pytest.raises(Portfolio123RoutingError, match="exceeded"):
        record_api_result(decision, actual_credits=3, quota_remaining=97)


def test_unknown_capability_fails_closed() -> None:
    with pytest.raises(Portfolio123RoutingError, match="not reviewed"):
        route_capability("strategy.magic")

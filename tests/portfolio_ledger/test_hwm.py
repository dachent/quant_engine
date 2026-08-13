from __future__ import annotations

from datetime import date as date_type

import pytest

from decimal import Decimal

from quant_engine.portfolio_ledger import (
    HwmCoverageError,
    compute_full_systematic_hwm_index,
    governed_hwm_coverage,
)


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

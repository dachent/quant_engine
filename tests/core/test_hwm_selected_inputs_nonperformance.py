"""The HWM selected-inputs gate excludes non-performance manifest rows.

Under the landed R3/I1 semantics the account-strategy manifest legitimately
carries rows with no performance data: BLOCKED_ADMISSION rows from the
census/admission gate and OK rows whose performance_status is UNAVAILABLE
(zero-capital rule -- identity kept, numerics null).  HWM is a high-water-mark
computation over performance data, so the gate selects only rows with
status OK and performance_status AVAILABLE, discloses the exclusion count,
and still fails closed when a selected strategy has no performance rows.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

import pytest

import portfolio_hwm_selected_inputs as selected_hwm
from portfolio_hwm_selected_inputs import SelectedHwmInputError


def _documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Thirteen selected product epochs over two strategies, all performing."""
    crosswalk_rows: list[dict[str, Any]] = []
    calculation_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    for index in range(13):
        strategy_id = "alpha" if index < 2 else "beta"
        epoch = {
            "account_id": f"synthetic-account-{index:02d}",
            "strategy_id": strategy_id,
            "effective_start": "2026-01-05",
            "effective_end": None,
        }
        crosswalk_rows.append(
            {
                **epoch,
                "provider": "synthetic",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        )
        calculation_rows.append({"epoch": dict(epoch), "provider": "synthetic"})
        output_rows.append(
            {
                **epoch,
                "provider": "synthetic",
                "status": "OK",
                "performance_status": "AVAILABLE",
                "performance_reason": None,
                "observation_trace": {"links": []},
            }
        )
    return (
        {"rows": calculation_rows},
        {"rows": output_rows},
        {"rows": crosswalk_rows},
    )


def test_blocked_admission_row_is_excluded_with_disclosure() -> None:
    calculation, output, crosswalk = _documents()
    blocked = output["rows"][0]
    blocked["status"] = "BLOCKED_ADMISSION"
    blocked["blocking_reason"] = "EPOCH_NOT_EXPLICITLY_FUNDED"
    del blocked["performance_status"], blocked["performance_reason"]
    del blocked["observation_trace"]

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 12
    assert all(
        row["status"] == "OK" and row["performance_status"] == "AVAILABLE"
        for row in selected.values()
    )
    assert len(excluded) == 1
    disclosure = excluded[0]
    assert disclosure["account_id"] == "synthetic-account-00"
    assert disclosure["strategy_id"] == "alpha"
    assert disclosure["status"] == "BLOCKED_ADMISSION"
    assert disclosure["performance_status"] is None
    assert disclosure["reason"] == "EPOCH_NOT_EXPLICITLY_FUNDED"


def test_unavailable_performance_row_is_excluded_with_disclosure() -> None:
    calculation, output, crosswalk = _documents()
    unavailable = output["rows"][0]
    unavailable["performance_status"] = "UNAVAILABLE"
    unavailable["performance_reason"] = "ZERO_CAPITAL_RETURN_UNDEFINED"

    _, _, selected, excluded = selected_hwm._source_rows(calculation, output, crosswalk)

    assert len(selected) == 12
    assert len(excluded) == 1
    assert excluded[0]["status"] == "OK"
    assert excluded[0]["performance_status"] == "UNAVAILABLE"
    assert excluded[0]["reason"] == "ZERO_CAPITAL_RETURN_UNDEFINED"


def test_strategy_with_zero_performance_available_rows_still_raises() -> None:
    calculation, output, crosswalk = _documents()
    blocked, unavailable = output["rows"][:2]
    blocked["status"] = "BLOCKED_ADMISSION"
    blocked["blocking_reason"] = "EPOCH_NOT_EXPLICITLY_FUNDED"
    del blocked["performance_status"], blocked["performance_reason"]
    del blocked["observation_trace"]
    unavailable["performance_status"] = "UNAVAILABLE"
    unavailable["performance_reason"] = "ZERO_CAPITAL_RETURN_UNDEFINED"

    with pytest.raises(SelectedHwmInputError, match="no performance-available.*alpha"):
        selected_hwm._source_rows(calculation, output, crosswalk)


def _one_strategy_mixed_maps() -> tuple[
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
]:
    """One strategy whose second account epoch is excluded as non-performance."""
    strategy_id = "synthetic-strategy"
    days = ["2026-07-13", "2026-07-14", "2026-07-15"]
    performance_key = ("account-performing", strategy_id, days[0], None)
    blocked_key = ("account-blocked", strategy_id, days[0], None)

    def nav(account_id: str, observed_on: str, value: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "as_of_date": observed_on,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
        }

    def link(start: str, end: str, base: str, ending: str) -> dict[str, str]:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            factor = Decimal(ending) / Decimal(base)
        return {
            "start_date": start,
            "end_date": end,
            "base_nav": base,
            "beginning_flow": "0",
            "ending_nav": ending,
            "ending_flow": "0",
            "net_flow": "0",
            "factor": str(factor),
        }

    crosswalk: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        key: {"provider": "synthetic", "assignment_id": f"assignment-{key[0]}"}
        for key in (performance_key, blocked_key)
    }
    calculation: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        performance_key: {
            "nav_observations": [
                nav(performance_key[0], days[0], "100"),
                nav(performance_key[0], days[1], "105"),
                nav(performance_key[0], days[2], "110"),
            ],
            "capital_anchors": [],
            "classified_flows": [],
        },
        blocked_key: {
            "nav_observations": [
                nav(blocked_key[0], days[0], "50"),
                nav(blocked_key[0], days[1], "55"),
                nav(blocked_key[0], days[2], "60"),
            ],
            "capital_anchors": [],
            "classified_flows": [],
        },
    }
    output: dict[selected_hwm.EpochKey, dict[str, Any]] = {
        performance_key: {
            "observation_trace": {
                "links": [
                    link(days[0], days[1], "100", "105"),
                    link(days[1], days[2], "105", "110"),
                ]
            }
        }
    }
    return crosswalk, calculation, output


def test_builder_arithmetic_only_sees_performance_rows() -> None:
    crosswalk, calculation, output = _one_strategy_mixed_maps()

    memberships, capital, returns = selected_hwm._derive_builder_rows(
        crosswalk,
        calculation,
        output,
        account_generation_id="account-generation",
        source_generation_id="provider-generation",
        source_generation_sha256="1" * 64,
        artifact_sha256s={"authority/calculation-inputs.json": "2" * 64},
    )

    assert [row["strategy_id"] for row in memberships] == ["synthetic-strategy"]
    # Only the performing account's NAVs contribute; the excluded account's
    # 50/55/60 series never enters the aggregated capital or returns.
    assert capital == [
        {"strategy_id": "synthetic-strategy", "date": day, "closing_capital_usd": value}
        for day, value in zip(
            ["2026-07-13", "2026-07-14", "2026-07-15"], ["100", "105", "110"]
        )
    ]
    assert len(returns) == 2
    assert all(row["strategy_id"] == "synthetic-strategy" for row in returns)

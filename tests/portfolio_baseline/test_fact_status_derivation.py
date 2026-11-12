"""#905 / #895 M4: fact statuses are decided from evidence, not asserted.

* FX (E3): coverage is decided per record against the admitted generation;
  PARTIAL only for records it does not convert; closure against the provider's
  USD amount where one exists.
* flows / transactions / positions: COMPLETE only when the account fact matrix
  declares a completeness boundary for every investment account.
* costs: COMPLETE only when every sleeve cost is a known dollar amount.

Each test fails on the pre-fix builder, which hard-coded these statuses.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from tools import portfolio_baseline as baseline

GEN = "a" * 64
ADMISSION = {"generation_id": GEN, "start": date(2026, 1, 1), "end": date(2026, 11, 6)}
AS_OF = date(2026, 11, 6)


def _record(
    index: int,
    *,
    currency: str = "EUR",
    value: str = "100",
    day: str = "2026-07-10",
    evidence: dict[str, Any] | None | str = "default",
    provider_usd: dict[str, Any] | None = None,
    status: str = "AVAILABLE",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account_id": "account-a",
        "amount": {"currency": currency, "status": status, "value": value},
        "event_date": day,
        "provider": "matrixport",
        "record_id": f"r{index}",
    }
    if evidence == "default":
        evidence = {
            "currency": currency,
            "date": day,
            "generation_id": GEN,
            "rate": "1.10",
        }
    if evidence is not None:
        row["event_time_fx"] = evidence
    if provider_usd is not None:
        row["provider_usd_amount"] = provider_usd
    return row


def _fx(flows: list[dict[str, Any]], admission: Any = ADMISSION) -> dict[str, Any]:
    claims, _ = baseline._fact_coverage(
        [], [], [], flows, [], [], 0, AS_OF, fx_admission=admission
    )
    return next(item for item in claims if item["fact_type"] == "fx")


def test_every_non_usd_record_converted_by_the_generation_is_complete() -> None:
    row = _fx([_record(1), _record(2, day="2026-08-01")])
    assert row["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert row["known_gaps"] == []
    assert row["disposition"]["uncovered_record_count"] == 0


def test_only_the_uncovered_records_make_it_partial() -> None:
    covered = _record(1)
    uncovered = _record(2, evidence=None)
    row = _fx([covered, uncovered])
    assert row["status"] == "PARTIAL"
    (gap,) = row["known_gaps"]
    assert "1 of 2 are not converted" in gap
    assert "NO_FX_EVIDENCE=1" in gap
    assert row["disposition"]["uncovered_record_count"] == 1


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": "UNAVAILABLE"}, "AMOUNT_UNAVAILABLE"),
        ({"evidence": None}, "NO_FX_EVIDENCE"),
        (
            {
                "evidence": {
                    "currency": "EUR",
                    "date": "2026-07-10",
                    "generation_id": "b" * 64,
                    "rate": "1.1",
                }
            },
            "OTHER_GENERATION",
        ),
        (
            {
                "evidence": {
                    "currency": "GBP",
                    "date": "2026-07-10",
                    "generation_id": GEN,
                    "rate": "1.1",
                }
            },
            "CURRENCY_DIFFERS",
        ),
        (
            {
                "evidence": {
                    "currency": "EUR",
                    "date": "2026-07-09",
                    "generation_id": GEN,
                    "rate": "1.1",
                }
            },
            "FX_EVIDENCE_INVALID",
        ),
        (
            {
                "evidence": {
                    "currency": "EUR",
                    "date": "2026-07-10",
                    "generation_id": GEN,
                    "rate": "0",
                }
            },
            "FX_EVIDENCE_INVALID",
        ),
        (
            {
                "day": "2025-12-31",
                "evidence": {
                    "currency": "EUR",
                    "date": "2025-12-31",
                    "generation_id": GEN,
                    "rate": "1.1",
                },
            },
            "OUTSIDE_GENERATION_INTERVAL",
        ),
    ],
)
def test_each_reason_a_record_is_not_converted(
    kwargs: dict[str, Any], reason: str
) -> None:
    assert (
        baseline._fx_record_uncovered_reason(_record(1, **kwargs), ADMISSION) == reason
    )


def test_closure_against_the_provider_usd_amount() -> None:
    # 100 EUR at 1.10 = 110.00 USD
    closing = _record(1, provider_usd={"status": "AVAILABLE", "value": "110.20"})
    assert baseline._fx_record_uncovered_reason(closing, ADMISSION) is None
    off = _record(2, provider_usd={"status": "AVAILABLE", "value": "130"})
    assert baseline._fx_record_uncovered_reason(off, ADMISSION) == "CLOSURE_FAILED"
    assert _fx([closing, off])["status"] == "PARTIAL"


def test_no_admitted_generation_is_still_failed() -> None:
    assert _fx([_record(1)], admission=None)["status"] == "FAILED"


# ----------------------------------------------------- flows/transactions/positions


def _row(fact: str, account: str, **kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "account_id": account,
        "declared_end": None,
        "declared_start": None,
        "fact_type": fact,
        "missing_intervals": [],
        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
    }
    return {**base, **kwargs}


def _claims(matrix: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
    claims, _ = baseline._fact_coverage(
        [{"account_id": "account-a", "role": "INVESTMENT"}],
        [],
        kwargs.pop("transactions", [{"provider": "p", "record_id": "t"}]),
        kwargs.pop("flows", [{"provider": "p", "record_id": "f"}]),
        kwargs.pop("positions", [{"position_id": "p"}]),
        kwargs.pop("schwab_known_gaps", []),
        0,
        AS_OF,
        costs=kwargs.pop("costs", []),
        coverage_matrix=matrix,
    )
    return {item["fact_type"]: item for item in claims}


def test_observed_records_are_not_completeness() -> None:
    matrix = [_row(f, "account-a") for f in ("flows", "transactions", "positions")]
    claims = _claims(matrix)
    for fact in ("flows", "transactions", "positions"):
        assert claims[fact]["status"] == "PARTIAL"


def test_no_matrix_is_no_completeness() -> None:
    claims = _claims(None)
    assert claims["flows"]["status"] == "PARTIAL"
    assert claims["positions"]["status"] == "PARTIAL"


def test_a_declared_boundary_for_every_account_makes_the_fact_complete() -> None:
    declared = {"declared_start": "2025-11-03", "declared_end": "2026-11-06"}
    matrix = [
        _row("flows", "account-a", status="DECLARED_COMPLETE", **declared),
        _row("transactions", "account-a", status="DECLARED_COMPLETE", **declared),
        _row("positions", "account-a", status="DECLARED_COMPLETE", **declared),
    ]
    claims = _claims(matrix)
    assert claims["flows"]["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert claims["flows"]["known_gaps"] == []
    assert claims["transactions"]["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert claims["positions"]["status"] == "COMPLETE_WITHIN_DECLARED_BOUNDARY"


def test_one_undeclared_account_keeps_the_fact_partial() -> None:
    declared = {"declared_start": "2025-11-03", "declared_end": "2026-11-06"}
    matrix = [
        _row("flows", "account-a", status="DECLARED_COMPLETE", **declared),
        _row("flows", "account-b"),
    ]
    assert _claims(matrix)["flows"]["status"] == "PARTIAL"


def test_a_missing_interval_keeps_a_declared_fact_partial() -> None:
    matrix = [
        _row(
            "flows",
            "account-a",
            status="DECLARED_COMPLETE",
            declared_start="2025-11-03",
            declared_end="2026-11-06",
            missing_intervals=[{"start": "2026-02-01", "end": "2026-02-03"}],
        )
    ]
    assert _claims(matrix)["flows"]["status"] == "PARTIAL"


def test_positions_with_provider_gaps_stay_partial_even_when_declared() -> None:
    declared = {"declared_start": "2025-11-03", "declared_end": "2026-11-06"}
    matrix = [_row("positions", "account-a", status="DECLARED_COMPLETE", **declared)]
    claims = _claims(matrix, schwab_known_gaps=["a provider gap"])
    assert claims["positions"]["status"] == "PARTIAL"
    assert claims["positions"]["known_gaps"] == ["a provider gap"]


def test_no_positions_is_failed() -> None:
    assert _claims([], positions=[])["positions"]["status"] == "FAILED"


def test_costs_complete_only_when_every_cost_is_a_known_amount() -> None:
    known = {"cost_status": "known_fixed", "annual_external_cost_usd": "10"}
    unknown = {"cost_status": "unknown", "annual_external_cost_usd": None}
    rate = {"cost_status": "documented_rate_percent", "annual_external_cost_usd": None}
    assert _claims([], costs=[known])["costs"]["status"] == (
        "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    )
    assert _claims([], costs=[known, unknown])["costs"]["status"] == "PARTIAL"
    assert _claims([], costs=[known, rate])["costs"]["status"] == "PARTIAL"
    assert _claims([], costs=[])["costs"]["status"] == "PARTIAL"

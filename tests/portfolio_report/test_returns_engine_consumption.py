"""Paired tests for the report's consumption of the returns engine (C1-T3).

Every test here is a paired fail-before/good-bad gate for one mechanism of the
corrective program: the P1 report TWR loop is retired (TWR, flow counts and
observation facts come from the manifest epoch payload), per-account return
paths are the account's OWN path rather than a strategy-level substitution,
epoch HPR gap spans are spliced in the one return engine, and a single-account
strategy's manifest epoch TWR must equal its own compounded return path.
"""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import decimal_text


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_report.py"
SPEC = importlib.util.spec_from_file_location("portfolio_report", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio report builder")
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _owned_assignment(account_id: str, strategy_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "assignment_id": f"assignment-{account_id}",
        "effective_end": None,
        "effective_start": "2026-01-01",
        "mapping_status": "MAPPED",
        "pool_id": "systematic-capital",
        "pool_mapping_status": "MAPPED",
        "strategy_id": strategy_id,
        "strategy_mapping_disposition": "STRATEGY_MAPPED",
    }


def _owned_epoch_details(
    twr: str | None,
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    return {
        ("account-owned", "strategy-owned", "2026-01-01", None): {
            "account_label": "Owned account",
            "last_four": "4321",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "performance_payload": {
                "flow_count": 0,
                "flow_timing": None,
                "nav_observations": 2,
                "observed_end": "2026-01-05",
                "observed_start": "2026-01-02",
                "twr": twr,
                "twr_status": (
                    "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
                    if twr is not None
                    else "UNAVAILABLE"
                ),
            },
        }
    }


def _owned_path_catalog() -> dict[str, Any]:
    return {
        "assignments": [_owned_assignment("account-owned", "strategy-owned")],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [{"account_id": "account-owned"}],
                    "observed_start": "2026-01-02",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-owned",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": ["account-owned"],
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0",
                "strategy_id": "strategy-owned",
            },
            {
                "component_account_ids": ["account-owned"],
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.1",
                "strategy_id": "strategy-owned",
            },
        ],
    }


def test_performance_takes_twr_from_the_manifest_epoch_payload() -> None:
    """Paired (fail-before): the P1 TWR loop is retired.

    The old loop re-derived TWR from raw NAV/flow records; it now comes from
    the manifest epoch payload, and raw NAV/calendar records are never read.
    """

    epoch_details = {
        ("account-owned", "strategy-owned", "2026-01-01", None): {
            "account_label": "Owned account",
            "last_four": "4321",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "performance_payload": {
                "mwr_cashflows": [],
                "mwr_coverage_status": "COMPLETE",
                "mwr_day_count": "ACTUAL_365",
                "mwr_label": "ACCOUNT_EPOCH_MWR",
                "mwr_opening_date": "2026-01-01",
                "mwr_opening_value": "100",
                "mwr_reason": None,
                "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
                "mwr_status": "AVAILABLE",
                "mwr_terminal_date": "2026-01-10",
                "mwr_terminal_value": "125",
                "mwr_unit": "ANNUALIZED_RETURN",
                "mwr_valuation_timing": "NAV_RECOGNITION_DATE_OR_EXACT_FILL_DATE",
                "mwr_value_basis": "ACCOUNT_TOTAL",
                "mwr_xirr": "0.9",
                "flow_count": 2,
                "flow_timing": "DATE_ONLY_END_OF_DAY_INFERRED_TIMING",
                "nav_observations": 9,
                "observed_end": "2026-01-10",
                "observed_start": "2026-01-02",
                "twr": "0.25",
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
        }
    }
    catalog = {
        "assignments": [_owned_assignment("account-owned", "strategy-owned")],
        "calendar_coverage": [
            {
                "account_id": "account-owned",
                "date": "TRAP-MUST-NOT-BE-READ",
                "status": "MISSING_ON_PROVIDER_OBSERVED_DATE",
            }
        ],
        "flows": [],
        "nav_observations": [
            {
                "account_id": "account-owned",
                "as_of_date": "TRAP-MUST-NOT-BE-READ",
                "nav": {"status": "AVAILABLE", "value": "100"},
            }
        ],
        "strategy_returns": [],
    }

    rows = reporter._account_strategy_performance(
        catalog,
        None,
        set(epoch_details),
        epoch_details,
    )

    row = rows[0]
    assert row["twr"] == "0.25"
    assert row["twr_status"] == "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
    assert row["flow_count"] == 2
    assert row["flow_timing"] == "DATE_ONLY_END_OF_DAY_INFERRED_TIMING"
    assert row["nav_observations"] == 9
    assert row["observed_start"] == "2026-01-02"
    assert row["observed_end"] == "2026-01-10"
    assert row["mwr_status"] == "AVAILABLE"
    assert row["mwr_xirr"] == "0.9"


def test_single_account_epoch_twr_gate_accepts_the_agreeing_own_path() -> None:
    epoch_details = _owned_epoch_details("0.1")

    rows = reporter._account_strategy_performance(
        _owned_path_catalog(),
        None,
        set(epoch_details),
        epoch_details,
    )

    row = rows[0]
    assert row["twr"] == "0.1"
    assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
    assert row["return_path_cumulative_return"] == "0.1"
    assert row["return_path_account_binding"] == "EVIDENCE_ACCOUNT_SCOPE"
    assert row["return_path_observations"] == 2


def test_single_account_epoch_twr_gate_rejects_a_disagreeing_own_path() -> None:
    epoch_details = _owned_epoch_details("0.100000001")

    with pytest.raises(
        reporter.ReportValidationError,
        match="single-account epoch TWR differs from the account return path",
    ):
        reporter._account_strategy_performance(
            _owned_path_catalog(),
            None,
            set(epoch_details),
            epoch_details,
        )


def test_multi_account_members_carry_no_strategy_level_return_path() -> None:
    """Paired (fail-before): the strategy-level substitution is removed.

    A member account of a multi-account strategy keeps no return path when no
    row is the account's own; the strategy composite is never substituted.
    """

    catalog = {
        "assignments": [
            _owned_assignment("account-a", "strategy-shared"),
            _owned_assignment("account-b", "strategy-shared"),
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [
                        {"account_id": "account-a"},
                        {"account_id": "account-b"},
                    ],
                    "observed_start": "2026-01-02",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-shared",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": ["account-a", "account-b"],
                "date": "2026-01-02",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.1",
                "strategy_id": "strategy-shared",
            },
            {
                "component_account_ids": ["account-a", "account-b"],
                "date": "2026-01-05",
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": "0.2",
                "strategy_id": "strategy-shared",
            },
        ],
    }

    rows = reporter._account_strategy_performance(catalog)

    assert len(rows) == 2
    assert all("return_path_status" not in row for row in rows)
    assert all(row["twr"] is None for row in rows)
    assert all(row["twr_status"] == "UNAVAILABLE" for row in rows)


def test_epoch_hpr_spans_are_spliced_through_the_return_engine() -> None:
    """Paired (fail-before): spans are spliced in one place (splice_spans)."""

    from quant_engine.portfolio_ledger.returns import (
        ReturnSpan as EngineReturnSpan,
        splice_spans,
    )

    daily = [
        {"date": "2026-01-02", "return": "0.01"},
        {"date": "2026-01-05", "return": "0.02"},
    ]
    with localcontext() as context:
        context.prec = 28
        engine_path = splice_spans(
            [
                (date.fromisoformat(item["date"]), Decimal(item["return"]))
                for item in daily
            ],
            [EngineReturnSpan(date(2026, 1, 2), date(2026, 1, 4), Decimal("0.1"))],
        )
    receipt = {
        "admission_cutoff": "2026-01-05",
        "daily_cumulative_return": decimal_text(engine_path.daily_cumulative_return),
        "daily_return_observations": 2,
        "daily_return_series_schema": "ordered-date-return/1",
        "daily_return_series_sha256": hashlib.sha256(
            canonical_json_bytes(daily)
        ).hexdigest(),
        "epoch_cumulative_return": decimal_text(engine_path.cumulative_return),
        "epoch_holding_period_link_observations": 3,
        "observed_end": "2026-01-05",
        "observed_start": "2026-01-02",
        "source_gap_policy_sha256": "0" * 64,
        "span_return_receipts": [
            {
                "end_endpoint_sha256": "1" * 64,
                "missing_source_date": "2026-01-03",
                "return": "0.1",
                "span_days": 2,
                "span_end_date": "2026-01-04",
                "span_start_date": "2026-01-02",
                "start_endpoint_sha256": "2" * 64,
                "subscription_events_in_span": 0,
            }
        ],
    }
    catalog = {
        "assignments": [_owned_assignment("account-owned", "strategy-owned")],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": receipt,
                "source_generation_id": "3" * 64,
                "source_id": "matrixport-account-generation",
                "strategy_id": "strategy-owned",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": ["account-owned"],
                "date": item["date"],
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "matrixport",
                "return": item["return"],
                "strategy_id": "strategy-owned",
            }
            for item in daily
        ],
    }

    strategy_level = reporter._account_live_return_series(catalog)
    own = reporter._account_live_return_series(catalog, account_owned=True)

    expected = decimal_text(engine_path.cumulative_return)
    assert strategy_level["strategy-owned"]["cumulative_return"] == expected
    assert (
        strategy_level["strategy-owned"]["epoch_holding_period_link_observations"] == 3
    )
    assert own[("account-owned", "strategy-owned")]["cumulative_return"] == expected
    assert own[("account-owned", "strategy-owned")]["return_observations"] == 2


def test_epoch_hpr_span_that_does_not_replace_a_gap_fails_closed() -> None:
    catalog = {
        "assignments": [_owned_assignment("account-owned", "strategy-owned")],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_returns": [
            {
                "component_account_ids": ["account-owned"],
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "matrixport",
                "return": value,
                "strategy_id": "strategy-owned",
            }
            for day, value in (
                ("2026-01-02", "0.01"),
                ("2026-01-04", "0.02"),
                ("2026-01-05", "0.03"),
            )
        ],
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": {
                    "admission_cutoff": "2026-01-05",
                    "daily_cumulative_return": "0.061106",
                    "daily_return_observations": 3,
                    "daily_return_series_schema": "ordered-date-return/1",
                    "daily_return_series_sha256": hashlib.sha256(
                        canonical_json_bytes(
                            [
                                {"date": "2026-01-02", "return": "0.01"},
                                {"date": "2026-01-04", "return": "0.02"},
                                {"date": "2026-01-05", "return": "0.03"},
                            ]
                        )
                    ).hexdigest(),
                    "epoch_cumulative_return": "0.1672166",
                    "epoch_holding_period_link_observations": 4,
                    "observed_end": "2026-01-05",
                    "observed_start": "2026-01-02",
                    "source_gap_policy_sha256": "0" * 64,
                    "span_return_receipts": [
                        {
                            "end_endpoint_sha256": "1" * 64,
                            "missing_source_date": "2026-01-03",
                            "return": "0.1",
                            "span_days": 2,
                            "span_end_date": "2026-01-04",
                            "span_start_date": "2026-01-02",
                            "start_endpoint_sha256": "2" * 64,
                            "subscription_events_in_span": 0,
                        }
                    ],
                },
                "source_generation_id": "3" * 64,
                "source_id": "matrixport-account-generation",
                "strategy_id": "strategy-owned",
            }
        ],
    }

    with pytest.raises(reporter.ReportValidationError, match="return path is invalid"):
        reporter._account_live_return_series(catalog, account_owned=True)


def test_multi_account_strategy_path_matches_the_engine_composite() -> None:
    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
        splice_spans,
    )

    epoch_a = link_epoch(
        {date(2026, 1, 2): Decimal("100"), date(2026, 1, 5): Decimal("110")},
        [],
        OpeningPoint(date(2026, 1, 2), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"),
    )
    epoch_b = link_epoch(
        {date(2026, 1, 2): Decimal("200"), date(2026, 1, 5): Decimal("204")},
        [],
        OpeningPoint(date(2026, 1, 2), Decimal("200"), "LAST_OBSERVATION_BEFORE_START"),
    )
    composited = composite(
        (*member_returns("account-a", epoch_a), *member_returns("account-b", epoch_b))
    )
    rows = [
        {
            "component_account_ids": ["account-a", "account-b"],
            "date": item.end_date.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "synthetic",
            "return": decimal_text(item.period_return),
            "strategy_id": "strategy-shared",
        }
        for item in composited
    ]
    catalog = {
        "assignments": [
            _owned_assignment("account-a", "strategy-shared"),
            _owned_assignment("account-b", "strategy-shared"),
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_returns": rows,
    }

    canonical = reporter._account_live_return_series(catalog)

    with localcontext() as context:
        context.prec = 34
        engine_compound = splice_spans(
            [(date.fromisoformat(row["date"]), Decimal(row["return"])) for row in rows]
        )
    assert canonical["strategy-shared"]["cumulative_return"] == decimal_text(
        engine_compound.cumulative_return
    )
    assert canonical["strategy-shared"]["return_observations"] == len(rows)

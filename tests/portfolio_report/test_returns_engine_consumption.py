"""Paired tests for the report's consumption of the returns engine (C1-T3).

Every test here is a paired fail-before/good-bad gate for one mechanism of the
corrective program: the P1 report TWR loop is retired (TWR, flow counts and
observation facts come from the manifest epoch payload), per-account return
paths are the account's OWN path -- compounded from the per-account
observation-trace links the manifest producer persists, never a
strategy-level composite substitution, and an ungoverned multi-account series
publishes no member path at all -- and epoch HPR gap spans are spliced in the
one return engine.  A governed member's epoch TWR must equal its own
compounded trace path within 1e-12 (single- and multi-account alike), and a
multi-account strategy's member paths must reconcile to the canonical
strategy composite within 1e-12: both gates fail closed, with no band.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace
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


def _shared_member_catalog() -> dict[str, Any]:
    """Every ACCOUNT_LIVE row is composed by both member accounts."""

    return {
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
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": value,
                "strategy_id": "strategy-shared",
            }
            for day, value in (("2026-01-02", "0.1"), ("2026-01-05", "0.2"))
        ],
    }


def _shared_member_epoch_details(
    twr_by_account: dict[str, str | None],
    links_by_account: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    """Manifest epoch details for the members of the shared strategy.

    ``links_by_account`` attaches each member's own observation-trace links in
    the manifest schema-9 shape; the report compounds them into the member's
    own return path.
    """

    return {
        (account_id, "strategy-shared", "2026-01-01", None): {
            "account_label": "Shared member",
            "last_four": "4321",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "own_path_links": (
                links_by_account[account_id] if links_by_account is not None else None
            ),
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
        for account_id, twr in twr_by_account.items()
    }


def _manifest_trace_links(epoch: Any) -> list[dict[str, Any]]:
    """One member epoch's observation trace in the manifest schema-9 shape."""

    return [
        {
            "start_date": link.start_date.isoformat(),
            "end_date": link.end_date.isoformat(),
            "base_nav": decimal_text(link.beginning_value),
            "beginning_flow": decimal_text(link.beginning_flow),
            "ending_nav": decimal_text(link.ending_value),
            "ending_flow": decimal_text(link.ending_flow),
            "net_flow": decimal_text(link.beginning_flow + link.ending_flow),
            "factor": decimal_text(link.factor),
            "cumulative_index": decimal_text(link.cumulative_index * Decimal(100)),
            "running_hwm_index": decimal_text(link.running_hwm_index * Decimal(100)),
            "drawdown": decimal_text(link.drawdown),
        }
        for link in epoch.links
    ]


def _equal_composite_member_links() -> dict[str, list[dict[str, Any]]]:
    """Both members' own epochs compound the shared rows (10% then 20%).

    Each member links 100 -> 110 -> 132, so each member's own path equals the
    compounded shared series (1.1 * 1.2 - 1 = 0.32): the equal-overlap case
    where the members' own paths and the composite coincide.
    """

    from quant_engine.portfolio_ledger.returns import OpeningPoint, link_epoch

    links: dict[str, list[dict[str, Any]]] = {}
    for account_id in ("account-a", "account-b"):
        epoch = link_epoch(
            {
                date(2026, 1, 1): Decimal("100"),
                date(2026, 1, 2): Decimal("110"),
                date(2026, 1, 5): Decimal("132"),
            },
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )
        links[account_id] = _manifest_trace_links(epoch)
    return links


def _shared_strategy_catalog(composited: tuple[Any, ...]) -> dict[str, Any]:
    """A shared two-member catalog from the engine's composite rows.

    Each ACCOUNT_LIVE row carries its sub-period's composing member ids and
    the engine composite return, so the strategy-level series is exactly the
    compounded composite the producer emits.
    """

    return {
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
                "component_account_ids": list(item.member_ids),
                "date": item.end_date.isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": decimal_text(item.period_return),
                "strategy_id": "strategy-shared",
            }
            for item in composited
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


def test_multi_account_divergent_member_twrs_fail_closed_at_the_seam() -> None:
    """Paired (fail-before): divergent member TWRs must never pass the seam.

    W1: the ungated seam accepted both consistent member payloads and
    deliberately divergent member TWRs 9 and 8.  Each member's own trace path
    compounds the shared rows to 0.32; a member epoch TWR that disagrees with
    the member's OWN compounded path beyond 1e-12 is a divergent member TWR,
    and the seam refuses it instead of silently publishing.
    """

    epoch_details = _shared_member_epoch_details(
        {"account-a": "9", "account-b": "8"},
        _equal_composite_member_links(),
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member epoch TWR differs from the member return path",
    ):
        reporter._account_strategy_performance(
            _shared_member_catalog(),
            None,
            set(epoch_details),
            epoch_details,
        )


def test_multi_account_member_twrs_consistent_with_the_composite_pass() -> None:
    """Paired (fail-before): member TWRs agreeing with their own paths pass.

    Both members link the full shared series, so each member's own trace path
    is the compounded series (1.1 * 1.2 - 1 = 0.32) and each member epoch TWR
    agrees with it within 1e-12; both members publish their own paths and the
    member links reconcile to the strategy composite.
    """

    from quant_engine.portfolio_ledger.returns import splice_spans

    with localcontext() as context:
        context.prec = 34
        composite_path = splice_spans(
            [(date(2026, 1, 2), Decimal("0.1")), (date(2026, 1, 5), Decimal("0.2"))]
        )
    composite_twr = decimal_text(composite_path.cumulative_return)

    epoch_details = _shared_member_epoch_details(
        {"account-a": composite_twr, "account-b": composite_twr},
        _equal_composite_member_links(),
    )

    rows = reporter._account_strategy_performance(
        _shared_member_catalog(),
        None,
        set(epoch_details),
        epoch_details,
    )

    rendered = {row["account_id"]: row for row in rows}
    assert len(rendered) == 2
    for account_id in ("account-a", "account-b"):
        assert rendered[account_id]["twr"] == composite_twr
        assert (
            rendered[account_id]["return_path_status"]
            == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        )
        assert rendered[account_id]["return_path_cumulative_return"] == composite_twr
        assert rendered[account_id]["return_path_observations"] == 2


def test_multi_account_member_twr_deviating_from_the_own_path_fails_closed() -> None:
    """Paired (fail-before): a member TWR deviating from its own path fails.

    The removed composite tolerance band accepted a member TWR of 0.326
    against the compounded shared rows 0.32 (a 0.6pp deviation inside the
    5pp band).  Member TWRs are now gated against each member's OWN compounded
    trace path with exact 1e-12 semantics: any deviation beyond 1e-12 fails
    closed, with no band.
    """

    epoch_details = _shared_member_epoch_details(
        {"account-a": "0.326", "account-b": "0.314"},
        _equal_composite_member_links(),
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member epoch TWR differs from the member return path",
    ):
        reporter._account_strategy_performance(
            _shared_member_catalog(),
            None,
            set(epoch_details),
            epoch_details,
        )


def test_ungoverned_multi_account_member_paths_publish_no_member_path() -> None:
    """Paired (fail-before): ungoverned multi-account members publish nothing.

    Without manifest epoch details there is no verified per-member series, so
    a multi-account series publishes NO member own path at all -- a fail-closed
    absence rather than a strategy-level composite substitution or a partial
    sole-row subset.  The strategy-level path still publishes the shared
    series through the strategy evidence surface.
    """

    rows = reporter._account_strategy_performance(_shared_member_catalog())

    assert len(rows) == 2
    assert all(row["twr"] is None for row in rows)
    assert all(row["twr_status"] == "UNAVAILABLE" for row in rows)
    for row in rows:
        for field in (
            "return_path_status",
            "return_path_cumulative_return",
            "return_path_observations",
            "return_path_observed_start",
            "return_path_observed_end",
            "return_path_series_sha256",
            "return_path_account_binding",
            "return_path_authority",
        ):
            assert field not in row
    canonical = reporter._account_live_return_series(_shared_member_catalog())
    assert canonical["strategy-shared"]["return_observations"] == 2
    assert canonical["strategy-shared"]["cumulative_return"] == "0.32"


def test_epoch_hpr_spans_are_spliced_through_the_return_engine() -> None:
    """Paired (fail-before): spans are spliced in one place (splice_spans).

    The epoch HPR receipt binds the strategy-level series, and its gap spans
    are spliced there through the one return engine.  A sole-component member
    publishes that same strategy-level series as its own path -- the member
    IS the series -- so the member's published path carries the spliced span.
    """

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
    published = reporter._account_strategy_performance(catalog)
    member_row = next(
        row for row in published if row["assignment_id"] == "assignment-account-owned"
    )

    expected = decimal_text(engine_path.cumulative_return)
    assert strategy_level["strategy-owned"]["cumulative_return"] == expected
    assert (
        strategy_level["strategy-owned"]["epoch_holding_period_link_observations"] == 3
    )
    assert member_row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
    assert member_row["return_path_cumulative_return"] == expected
    assert member_row["return_path_observations"] == 2


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

    with pytest.raises(
        reporter.ReportValidationError, match="epoch HPR span does not replace a gap"
    ):
        reporter._account_live_return_series(catalog)


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


def test_volatis_like_capital_anchors_are_not_booked_as_returns() -> None:
    """Paired (fail-before): strategy capital anchors never become returns.

    The P1 loop re-derived the epoch TWR from raw NAV and flow records, so the
    eleven STRATEGY_CAPITAL_ALLOCATION sleeve anchors -- evidence P1 never read
    as allocations -- were divided into a flat 100 -> 100 NAV pair and rendered
    ``(100 + 11 * 536) / 100 - 1 = 58.96``, a ~5,896% epoch.  The manifest
    payload now carries the engine TWR and the raw records are never read.
    """

    from quant_engine.portfolio_ledger.returns import splice_spans

    daily = [
        {"date": "2026-01-02", "return": "0.01"},
        {"date": "2026-01-05", "return": "0.02"},
    ]
    with localcontext() as context:
        context.prec = 34
        engine = splice_spans(
            [
                (date.fromisoformat(item["date"]), Decimal(item["return"]))
                for item in daily
            ]
        )
    engine_twr = decimal_text(engine.cumulative_return)
    epoch_details = {
        ("account-owned", "strategy-volatis", "2026-01-01", None): {
            "account_label": "Volatis account",
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
                "twr": engine_twr,
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
        }
    }
    catalog = {
        "assignments": [_owned_assignment("account-owned", "strategy-volatis")],
        "calendar_coverage": [],
        "flows": [
            {
                "account_id": "account-owned",
                "amount": {"status": "AVAILABLE", "value": "-536"},
                "capital_type": "STRATEGY_CAPITAL_ALLOCATION",
                "event_date": "2026-01-05",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "strategy_id": "strategy-volatis",
            }
            for _anchor in range(11)
        ],
        "nav_observations": [
            {
                "account_id": "account-owned",
                "as_of_date": "2026-01-02",
                "nav": {"status": "AVAILABLE", "value": "100"},
            },
            {
                "account_id": "account-owned",
                "as_of_date": "2026-01-05",
                "nav": {"status": "AVAILABLE", "value": "100"},
            },
        ],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [{"account_id": "account-owned"}],
                    "observed_start": "2026-01-02",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-volatis",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": ["account-owned"],
                "date": item["date"],
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": item["return"],
                "strategy_id": "strategy-volatis",
            }
            for item in daily
        ],
    }

    rows = reporter._account_strategy_performance(
        catalog,
        None,
        set(epoch_details),
        epoch_details,
    )

    row = rows[0]
    assert row["twr"] == engine_twr
    # The ~58.9 (5,896%) P1 garbage must not survive in any rendered field.
    assert Decimal(row["twr"]) < Decimal("1")
    assert row["twr_status"] == "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
    assert row["flow_count"] == 0
    assert row["nav_observations"] == 2


def test_multi_account_member_paths_are_the_members_own_returns() -> None:
    """Paired (fail-before): each member's path is its OWN trace path.

    A member's published return path is compounded from the member's own
    observation-trace links the manifest producer persists per account, never
    the strategy-level row distribution.  Unequal overlapping members -- a
    links solo +1% then shared +4%; b links shared +2% then solo +2% -- publish
    their own 0.0504 and 0.0404, not the weighted composite rows.  The removed
    fan-out substituted the shared composite row for every member, so both
    members published the same composite path.  Each member's published window
    opens at its first trace link's start date and closes at its last link's
    end date.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
        splice_spans,
    )

    epoch_a = link_epoch(
        {
            date(2026, 1, 1): Decimal("100"),
            date(2026, 1, 2): Decimal("101"),
            date(2026, 1, 3): Decimal("105.04"),
        },
        [],
        OpeningPoint(date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"),
    )
    epoch_b = link_epoch(
        {
            date(2026, 1, 2): Decimal("200"),
            date(2026, 1, 3): Decimal("204"),
            date(2026, 1, 4): Decimal("208.08"),
        },
        [],
        OpeningPoint(date(2026, 1, 2), Decimal("200"), "LAST_OBSERVATION_BEFORE_START"),
    )
    composited = composite(
        (*member_returns("account-a", epoch_a), *member_returns("account-b", epoch_b))
    )
    catalog = _shared_strategy_catalog(composited)
    epoch_details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(epoch_a.twr),
            "account-b": decimal_text(epoch_b.twr),
        },
        {
            "account-a": _manifest_trace_links(epoch_a),
            "account-b": _manifest_trace_links(epoch_b),
        },
    )

    rows = reporter._account_strategy_performance(
        catalog, None, set(epoch_details), epoch_details
    )
    rendered = {row["account_id"]: row for row in rows}

    for account_id, epoch in (("account-a", epoch_a), ("account-b", epoch_b)):
        own_series = [
            {
                "date": link.end_date.isoformat(),
                "return": decimal_text(link.period_return),
            }
            for link in epoch.links
        ]
        with localcontext() as context:
            context.prec = 34
            own_compound = splice_spans(
                [
                    (date.fromisoformat(item["date"]), Decimal(item["return"]))
                    for item in own_series
                ]
            )
        row = rendered[account_id]
        # The window bounds come from the trace links the fixture supplied to
        # the builder (an independent source), never from the published series:
        # the path window opens at the FIRST link's start date and closes at
        # the LAST link's end date.  The guard keeps the start assertion
        # discriminating -- a first link whose start equals its end date could
        # not tell the window start from the first link's end date.
        member_key = (account_id, "strategy-shared", "2026-01-01", None)
        trace_links = epoch_details[member_key]["own_path_links"]
        assert trace_links[0]["start_date"] != trace_links[0]["end_date"]
        # No link is silently dropped between the member's verified trace and
        # the published path: observation count, compounded value, endpoints
        # and series hash bind exactly the member's own linked series.
        assert row["twr"] == decimal_text(epoch.twr)
        assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        assert row["return_path_observations"] == len(own_series)
        assert row["return_path_observed_start"] == trace_links[0]["start_date"]
        assert row["return_path_observed_end"] == trace_links[-1]["end_date"]
        assert row["return_path_cumulative_return"] == decimal_text(
            own_compound.cumulative_return
        )
        assert (
            row["return_path_series_sha256"]
            == hashlib.sha256(canonical_json_bytes(own_series)).hexdigest()
        )
    assert (
        rendered["account-a"]["return_path_cumulative_return"]
        != rendered["account-b"]["return_path_cumulative_return"]
    )
    # Neither member's own path is the strategy composite path.
    canonical = reporter._account_live_return_series(catalog)
    with localcontext() as context:
        context.prec = 34
        strategy_compound = splice_spans(
            [(item.end_date, item.period_return) for item in composited]
        )
    assert canonical["strategy-shared"]["cumulative_return"] == decimal_text(
        strategy_compound.cumulative_return
    )
    for account_id in ("account-a", "account-b"):
        assert (
            rendered[account_id]["return_path_cumulative_return"]
            != canonical["strategy-shared"]["cumulative_return"]
        )


def test_multi_account_corrupted_member_twr_fails_closed() -> None:
    """Paired (fail-before): corrupted member TWR payloads fail closed.

    True member epochs link +4% and +2% (the strategy row is their weighted
    3% composite), but the manifest payloads claim +7% and +5%.  The removed
    five-percentage-point band accepted both corrupted payloads against the
    substituted composite path; the member gate now compares each member TWR
    with its OWN compounded trace path and fails closed.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    true_epochs = {"account-a": member_epoch("104"), "account-b": member_epoch("102")}
    composited = composite(
        (
            *member_returns("account-a", true_epochs["account-a"]),
            *member_returns("account-b", true_epochs["account-b"]),
        )
    )
    catalog = _shared_strategy_catalog(composited)
    epoch_details = _shared_member_epoch_details(
        {"account-a": "0.07", "account-b": "0.05"},
        {
            "account-a": _manifest_trace_links(true_epochs["account-a"]),
            "account-b": _manifest_trace_links(true_epochs["account-b"]),
        },
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member epoch TWR differs from the member return path",
    ):
        reporter._account_strategy_performance(
            catalog, None, set(epoch_details), epoch_details
        )


def test_multi_account_member_paths_must_reconcile_to_the_strategy_composite() -> None:
    """Paired (fail-before): self-consistent inflation fails closed.

    The manifest trace links AND the member TWRs both claim +7% and +5%, so
    the member gate agrees -- each TWR equals its own compounded trace path
    -- but the claimed members imply a 6% weighted composite while the
    canonical strategy path still publishes the true 3% row.  Only the
    composite reconciliation gate catches this: no proximity band can
    express it, and the removed band accepted it.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    true_composited = composite(
        (
            *member_returns("account-a", member_epoch("104")),
            *member_returns("account-b", member_epoch("102")),
        )
    )
    catalog = _shared_strategy_catalog(true_composited)
    inflated = {"account-a": member_epoch("107"), "account-b": member_epoch("105")}
    epoch_details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(inflated["account-a"].twr),
            "account-b": decimal_text(inflated["account-b"].twr),
        },
        {
            "account-a": _manifest_trace_links(inflated["account-a"]),
            "account-b": _manifest_trace_links(inflated["account-b"]),
        },
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member own paths do not reconcile "
        "to the strategy composite",
    ):
        reporter._account_strategy_performance(
            catalog, None, set(epoch_details), epoch_details
        )


def test_multi_account_valid_divergent_members_reconcile_and_publish() -> None:
    """Paired (fail-before): valid divergent members reconcile and publish.

    Equal-capital members earning +20% and -10% imply exactly the 5% weighted
    composite the strategy row publishes.  The removed band REJECTED these
    valid members (each deviates 15pp from the substituted composite path);
    the member gate and the composite reconciliation gate both accept them,
    and each member publishes its own divergent path.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    epochs = {"account-a": member_epoch("120"), "account-b": member_epoch("90")}
    composited = composite(
        (
            *member_returns("account-a", epochs["account-a"]),
            *member_returns("account-b", epochs["account-b"]),
        )
    )
    catalog = _shared_strategy_catalog(composited)
    epoch_details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(epochs["account-a"].twr),
            "account-b": decimal_text(epochs["account-b"].twr),
        },
        {
            "account-a": _manifest_trace_links(epochs["account-a"]),
            "account-b": _manifest_trace_links(epochs["account-b"]),
        },
    )

    rows = reporter._account_strategy_performance(
        catalog, None, set(epoch_details), epoch_details
    )
    rendered = {row["account_id"]: row for row in rows}

    for account_id, epoch in epochs.items():
        row = rendered[account_id]
        assert row["twr"] == decimal_text(epoch.twr)
        assert row["twr_status"] == "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
        assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        assert row["return_path_observations"] == len(epoch.links)
        assert row["return_path_cumulative_return"] == decimal_text(epoch.twr)
    assert (
        rendered["account-a"]["return_path_cumulative_return"]
        != rendered["account-b"]["return_path_cumulative_return"]
    )
    canonical = reporter._account_live_return_series(catalog)
    assert canonical["strategy-shared"]["cumulative_return"] == decimal_text(
        composited[0].period_return
    )
    for account_id in ("account-a", "account-b"):
        assert (
            rendered[account_id]["return_path_cumulative_return"]
            != canonical["strategy-shared"]["cumulative_return"]
        )


def test_multi_account_member_paths_stay_absent_with_epoch_hpr_spans() -> None:
    """Paired (fail-before): a strategy receipt's spans never reach members.

    The epoch HPR receipt binds the strategy-level series and its gap spans
    are spliced there through the one return engine.  Ungoverned multi-account
    members have no verified per-member series, so NO member path is published
    at all: the receipt's spans can never reach a member path, and neither a
    substituted composite path nor a partial sole-row subset appears.
    """

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
        "assignments": [
            _owned_assignment("account-a", "strategy-shared"),
            _owned_assignment("account-b", "strategy-shared"),
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "epoch_hpr_receipts": [
            {
                "evidence_layer": "ACCOUNT_LIVE",
                "receipt": receipt,
                "source_generation_id": "3" * 64,
                "source_id": "matrixport-account-generation",
                "strategy_id": "strategy-shared",
            }
        ],
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
                "date": item["date"],
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "matrixport",
                "return": item["return"],
                "strategy_id": "strategy-shared",
            }
            for item in daily
        ],
    }

    canonical = reporter._account_live_return_series(catalog)
    published = reporter._account_strategy_performance(catalog)

    assert canonical["strategy-shared"]["cumulative_return"] == decimal_text(
        engine_path.cumulative_return
    )
    assert (
        canonical["strategy-shared"]["cumulative_return_label"]
        == "EPOCH_CUMULATIVE_TWR_WITH_HASH_BOUND_HPR_GAPS"
    )
    assert len(published) == 2
    for row in published:
        assert row["twr"] is None
        assert row["twr_status"] == "UNAVAILABLE"
        for field in (
            "return_path_status",
            "return_path_cumulative_return",
            "return_path_observations",
            "return_path_observed_start",
            "return_path_observed_end",
            "return_path_series_sha256",
            "return_path_account_binding",
            "return_path_authority",
        ):
            assert field not in row


def _full_report_fixture() -> Any:
    """The full report test module, for its synthetic baseline publisher."""

    spec = importlib.util.spec_from_file_location(
        "portfolio_report_full_fixture",
        REPOSITORY_ROOT / "tests" / "portfolio_report" / "test_portfolio_report.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the full report fixture")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_builder_threads_member_trace_links_into_the_seam_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired (fail-before): the real report path reaches both member gates.

    The builder's current-epoch reconstruction dropped ``own_path_links``
    from the verified manifest details, so the public ``build_report_data``
    path reached neither gate: the corrupted 7%/5% member payloads published
    and every multi-account member row carried a null own path.  The
    reconstruction now threads the member traces through, so the real path
    fails closed on the corrupted payload instead of publishing it.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    true_epochs = {"account-a": member_epoch("104"), "account-b": member_epoch("102")}
    composited = composite(
        (
            *member_returns("account-a", true_epochs["account-a"]),
            *member_returns("account-b", true_epochs["account-b"]),
        )
    )
    catalog = _shared_strategy_catalog(composited)
    catalog["strategy_returns"][0]["return"] = "0.03"
    for evidence in catalog["strategy_provider_evidence"]:
        evidence["account"]["provider_id"] = "synthetic"
        for scope in evidence["account"]["account_scopes"]:
            scope.update(observed_start="2026-01-01", observed_end="2026-01-02")
    catalog = json.loads(json.dumps(catalog).replace("strategy-shared", "strategy-1"))
    full = _full_report_fixture()
    baseline_root = full._published_baseline(
        tmp_path, {**full._baseline_catalog(), **catalog}
    )
    epoch_details = _shared_member_epoch_details(
        {"account-a": "0.07", "account-b": "0.05"},
        {
            "account-a": _manifest_trace_links(true_epochs["account-a"]),
            "account-b": _manifest_trace_links(true_epochs["account-b"]),
        },
    )
    epoch_details = {
        (key[0], "strategy-1", key[2], key[3]): detail
        for key, detail in epoch_details.items()
    }
    crosswalk = {
        "calculation_cutoff_date": "2026-01-03",
        "rows": [
            {
                "account_id": key[0],
                "strategy_id": key[1],
                "effective_start": key[2],
                "effective_end": key[3],
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
            for key in epoch_details
        ],
    }
    monkeypatch.setattr(
        full.reporter,
        "_validate_epoch_manifest_input",
        lambda *_args, **_kwargs: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            epoch_details,
        ),
    )

    with pytest.raises(
        full.reporter.ReportValidationError,
        match="multi-account member epoch TWR differs from the member return path",
    ):
        full.reporter.build_report_data(
            baseline_root,
            epoch_manifest_path=tmp_path / "synthetic-manifest.json",
            epoch_manifest_source_path=tmp_path / "synthetic-source.json",
            epoch_crosswalk_path=tmp_path / "synthetic-crosswalk.json",
            epoch_manifest_generation_root=REPOSITORY_ROOT,
        )


def test_blocked_member_never_disables_the_composite_reconciliation_gate() -> None:
    """Paired (fail-before): a blocked member must never turn the gates off.

    Two funded members carry self-consistent inflated traces and TWRs (7% and
    5% against the strategy's true 3% composite), and a third member epoch of
    the same strategy is BLOCKED without a trace -- a routine governed
    admission outcome, not corrupt data.  The fail-open early return let the
    blocked member skip the composite reconciliation for the funded members
    and publish the inflated paths; the blocked member is now excluded from
    the reconcile input set, and the funded members' self-consistent
    inflation still fails closed against the canonical strategy composite.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    true_composited = composite(
        (
            *member_returns("account-a", member_epoch("104")),
            *member_returns("account-b", member_epoch("102")),
        )
    )
    catalog = _shared_strategy_catalog(true_composited)
    catalog["assignments"].append(_owned_assignment("account-c", "strategy-shared"))
    inflated = {"account-a": member_epoch("107"), "account-b": member_epoch("105")}
    epoch_details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(inflated["account-a"].twr),
            "account-b": decimal_text(inflated["account-b"].twr),
        },
        {
            "account-a": _manifest_trace_links(inflated["account-a"]),
            "account-b": _manifest_trace_links(inflated["account-b"]),
        },
    )
    epoch_details[("account-c", "strategy-shared", "2026-01-01", None)] = {
        "account_label": "Blocked member",
        "last_four": "4321",
        "epoch_admission_status": "BLOCKED",
        "epoch_output_status": "BLOCKED_ADMISSION",
        "epoch_blocking_reason": "SYNTHETIC_BLOCKED_COVERAGE",
        "own_path_links": [],
        "performance_payload": None,
    }

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member own paths do not reconcile "
        "to the strategy composite",
    ):
        reporter._account_strategy_performance(
            catalog, None, set(epoch_details), epoch_details
        )


def test_blocked_member_is_excluded_from_the_reconcile_set_and_disclosed() -> None:
    """Paired: a blocked member neither raises nor silently disappears.

    Two funded members carry their true traces (4% and 2%, reconciling to the
    strategy's composite) and a third member epoch of the same strategy is
    BLOCKED without a trace -- the governed outcome of incomplete coverage /
    an owner-approved unavailable observation.  The publication succeeds: the
    funded members publish their own paths, the composite reconciliation runs
    over the admitted set, and the blocked member's row discloses its
    exclusion with the BLOCKED_ADMISSION statuses and its typed reason.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    epochs = {"account-a": member_epoch("104"), "account-b": member_epoch("102")}
    composited = composite(
        (
            *member_returns("account-a", epochs["account-a"]),
            *member_returns("account-b", epochs["account-b"]),
        )
    )
    catalog = _shared_strategy_catalog(composited)
    catalog["assignments"].append(_owned_assignment("account-c", "strategy-shared"))
    epoch_details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(epochs["account-a"].twr),
            "account-b": decimal_text(epochs["account-b"].twr),
        },
        {
            "account-a": _manifest_trace_links(epochs["account-a"]),
            "account-b": _manifest_trace_links(epochs["account-b"]),
        },
    )
    epoch_details[("account-c", "strategy-shared", "2026-01-01", None)] = {
        "account_label": "Blocked member",
        "last_four": "4321",
        "epoch_admission_status": "BLOCKED",
        "epoch_output_status": "BLOCKED_ADMISSION",
        "epoch_blocking_reason": "SYNTHETIC_BLOCKED_COVERAGE",
        "own_path_links": [],
        "performance_payload": None,
    }

    rows = reporter._account_strategy_performance(
        catalog, None, set(epoch_details), epoch_details
    )
    rendered = {row["account_id"]: row for row in rows}

    for account_id, epoch in epochs.items():
        row = rendered[account_id]
        assert row["twr"] == decimal_text(epoch.twr)
        assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        assert row["return_path_cumulative_return"] == decimal_text(epoch.twr)
    blocked = rendered["account-c"]
    assert blocked["epoch_admission_status"] == "BLOCKED"
    assert blocked["epoch_blocking_reason"] == "SYNTHETIC_BLOCKED_COVERAGE"
    assert blocked["twr"] is None
    assert blocked["twr_status"] == "BLOCKED_ADMISSION"
    assert blocked["return_path_status"] == "BLOCKED_ADMISSION"
    assert blocked["return_path_cumulative_return"] is None


def test_funded_member_without_a_trace_fails_closed_with_a_blocked_sibling() -> None:
    """Paired: the funded-member trace gate stays fail-closed.

    One funded member carries its true trace, the second funded member's
    trace is missing, and a third member epoch is BLOCKED.  Excluding the
    blocked member from the reconcile set never excuses an admitted member:
    the funded member without a verified trace fails closed.
    """

    from quant_engine.portfolio_ledger.returns import (
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    def member_epoch(ending: str) -> Any:
        return link_epoch(
            {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal(ending)},
            [],
            OpeningPoint(
                date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"
            ),
        )

    epochs = {"account-a": member_epoch("104"), "account-b": member_epoch("102")}
    composited = composite(
        (
            *member_returns("account-a", epochs["account-a"]),
            *member_returns("account-b", epochs["account-b"]),
        )
    )
    catalog = _shared_strategy_catalog(composited)
    catalog["assignments"].append(_owned_assignment("account-c", "strategy-shared"))
    epoch_details = _shared_member_epoch_details(
        {"account-a": decimal_text(epochs["account-a"].twr)},
        {"account-a": _manifest_trace_links(epochs["account-a"])},
    )
    # The blocked entry precedes the traceless funded member, so the r3 code
    # raises on the blocked member before ever reaching account-b.
    epoch_details[("account-c", "strategy-shared", "2026-01-01", None)] = {
        "account_label": "Blocked member",
        "last_four": "4321",
        "epoch_admission_status": "BLOCKED",
        "epoch_output_status": "BLOCKED_ADMISSION",
        "epoch_blocking_reason": "SYNTHETIC_BLOCKED_COVERAGE",
        "own_path_links": [],
        "performance_payload": None,
    }
    epoch_details.update(
        _shared_member_epoch_details(
            {"account-b": decimal_text(epochs["account-b"].twr)},
        )
    )
    assert (
        epoch_details[("account-b", "strategy-shared", "2026-01-01", None)][
            "own_path_links"
        ]
        is None
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match=r"multi-account member own trace path is missing "
        r"for strategy-shared \(account-b\)",
    ):
        reporter._account_strategy_performance(
            catalog, None, set(epoch_details), epoch_details
        )


def test_single_account_trace_never_replaces_the_canonical_path_gate() -> None:
    """Paired (fail-before): the trace ADDS the gate; it never replaces it.

    A sole member's own trace and manifest TWR both claim 20% while the
    canonical account-live series still compounds 10%: the member IS the
    strategy series, so the single-account canonical comparison must hold
    alongside the trace gate.  The round-2 wiring selected the trace instead
    of the canonical path and published the 20%.
    """

    from quant_engine.portfolio_ledger.returns import OpeningPoint, link_epoch

    epoch = link_epoch(
        {date(2026, 1, 1): Decimal("100"), date(2026, 1, 2): Decimal("120")},
        [],
        OpeningPoint(date(2026, 1, 1), Decimal("100"), "LAST_OBSERVATION_BEFORE_START"),
    )
    epoch_details = _owned_epoch_details("0.2")
    epoch_details[("account-owned", "strategy-owned", "2026-01-01", None)][
        "own_path_links"
    ] = _manifest_trace_links(epoch)

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


def _publish_single_link_sole_account_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Publish one sole-account epoch through the public report builder.

    The member's whole observation trace is ONE link, July 1 -> July 30, +4%
    (Astra's window probe).  The report is built by the real
    ``build_report_data`` over a synthetic published baseline; only the
    verified epoch manifest input is replaced by these synthetic epoch details.
    Returns the published ``twr_mwr`` rows, the trace links the fixture
    supplied (the independent source of the expected window) and the panel's
    account-manifest rows the fixture derives from the same epoch.
    """

    from quant_engine.portfolio_ledger.returns import (
        OPENING_FIRST_POSITIVE_ON_OR_AFTER_START,
        OpeningPoint,
        link_epoch,
    )

    account_id, strategy_id, effective_start = "account-a", "strategy-1", "2026-07-01"
    epoch = link_epoch(
        {date(2026, 7, 1): Decimal("100"), date(2026, 7, 30): Decimal("104")},
        [],
        OpeningPoint(
            date(2026, 7, 1), Decimal("100"), OPENING_FIRST_POSITIVE_ON_OR_AFTER_START
        ),
    )
    trace_links = _manifest_trace_links(epoch)
    twr = decimal_text(epoch.twr)
    assert twr == "0.04"
    catalog = {
        "assignments": [
            {
                **_owned_assignment(account_id, strategy_id),
                "effective_start": effective_start,
            }
        ],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [
                        {
                            "account_id": account_id,
                            "observed_end": "2026-07-30",
                            "observed_start": "2026-07-01",
                        }
                    ],
                    "observed_start": "2026-07-01",
                    "provider_id": "synthetic",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": strategy_id,
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": [account_id],
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": value,
                "strategy_id": strategy_id,
            }
            for day, value in (("2026-07-01", "0"), ("2026-07-30", twr))
        ],
    }
    epoch_details = {
        (account_id, strategy_id, effective_start, None): {
            "account_label": "Sole member",
            "last_four": "4321",
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "own_path_links": trace_links,
            "performance_payload": {
                "flow_count": 0,
                "flow_timing": None,
                "nav_observations": 2,
                "observed_end": "2026-07-30",
                "observed_start": "2026-07-01",
                "twr": twr,
                "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            },
        }
    }
    crosswalk = {
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": account_id,
                "strategy_id": strategy_id,
                "effective_start": effective_start,
                "effective_end": None,
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        ],
    }
    full = _full_report_fixture()
    baseline_root = full._published_baseline(
        tmp_path, {**full._baseline_catalog(), **catalog}
    )
    monkeypatch.setattr(
        full.reporter,
        "_validate_epoch_manifest_input",
        lambda *_args, **_kwargs: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            epoch_details,
        ),
    )

    report, _content, _baseline_path = full.reporter.build_report_data(
        baseline_root,
        epoch_manifest_path=tmp_path / "synthetic-manifest.json",
        epoch_manifest_source_path=tmp_path / "synthetic-source.json",
        epoch_crosswalk_path=tmp_path / "synthetic-crosswalk.json",
        epoch_manifest_generation_root=REPOSITORY_ROOT,
    )

    manifest_rows = [
        {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "effective_start": effective_start,
            "start": trace_links[0]["start_date"],
            "end": trace_links[-1]["end_date"],
            "observation_linked_return": twr,
        }
    ]
    return report["twr_mwr"], trace_links, manifest_rows


def _twr_view_findings(
    rows: list[dict[str, Any]], manifest_rows: list[dict[str, Any]]
) -> list[Any]:
    """Run the acceptance oracle's TWR cross-view check over published output.

    The acceptance package is consumed as-is and never edited.
    ``check_twr_views`` reads the report's ``twr_mwr`` rows and the panel's
    account manifest; the panel ``performance`` section carries the published
    rows as the control panel projects them.
    """

    from acceptance.validate import checks

    generation = SimpleNamespace(
        report={"twr_mwr": rows},
        panel={
            "performance": copy.deepcopy(rows),
            "account_manifest": {"rows": manifest_rows},
        },
    )
    context = checks.Context(generation, date(2026, 9, 17), "synthetic-window-probe")
    return list(checks.check_twr_views(context))


def test_sole_account_single_link_trace_publishes_the_full_epoch_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired (fail-before): the path window opens at the first link's START.

    A sole member whose whole observation trace is one link, July 1 -> July 30
    (+4%), publishes the window July 1 -> July 30 -- the epoch's own window.
    The removed code published the first link's END date as the window start,
    collapsing the single-link epoch to the point July 30 -> July 30.  The
    expected bounds come from the trace links the fixture supplied, never from
    the published series; every other path field stays byte-identical (the
    series hash still binds the link END dates).
    """

    rows, trace_links, _manifest_rows = _publish_single_link_sole_account_report(
        tmp_path, monkeypatch
    )

    assert len(trace_links) == 1
    # Guard: a single link whose start equals its end could not tell the
    # window start from the first link's end date.
    assert trace_links[0]["start_date"] != trace_links[0]["end_date"]
    (row,) = rows
    assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
    assert row["return_path_observed_start"] == trace_links[0]["start_date"]
    assert row["return_path_observed_end"] == trace_links[-1]["end_date"]
    assert (
        row["return_path_observed_start"],
        row["return_path_observed_end"],
    ) == ("2026-07-01", "2026-07-30")
    assert (
        row["return_path_observed_start"],
        row["return_path_observed_end"],
    ) == (row["observed_start"], row["observed_end"])
    assert row["return_path_observations"] == 1
    assert row["return_path_cumulative_return"] == row["twr"] == "0.04"
    assert (
        row["return_path_series_sha256"]
        == hashlib.sha256(
            canonical_json_bytes([{"date": "2026-07-30", "return": "0.04"}])
        ).hexdigest()
    )


@pytest.mark.parametrize("corrupted", ["path_return", "epoch_return"])
def test_window_probe_fires_check_twr_views_on_a_corrupted_single_link_return(
    corrupted: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paired (fail-before): Astra's window probe now fires the oracle check.

    The published July 1 -> July 30 single-link 4% epoch agrees across every
    TWR view.  Corrupting one return to 17% must yield a BLOCKER_INVARIANT
    finding from ``check_twr_views``: either the path return against the
    unchanged epoch TWR and manifest linked return, or the epoch TWR mirrored
    into the manifest and the panel performance section against the unchanged
    path.  The removed window (July 30 -> July 30) was dropped by the oracle's
    ``windows_equivalent`` filter, so the path was never compared and both
    corruptions produced zero findings.
    """

    from acceptance.validate import checks

    rows, _trace_links, manifest_rows = _publish_single_link_sole_account_report(
        tmp_path, monkeypatch
    )
    assert _twr_view_findings(rows, manifest_rows) == []

    bad_rows = copy.deepcopy(rows)
    bad_manifest_rows = copy.deepcopy(manifest_rows)
    if corrupted == "path_return":
        bad_rows[0]["return_path_cumulative_return"] = "0.17"
    else:
        bad_rows[0]["twr"] = "0.17"
        bad_manifest_rows[0]["observation_linked_return"] = "0.17"

    findings = _twr_view_findings(bad_rows, bad_manifest_rows)

    assert findings
    assert {finding.check_id for finding in findings} == {"returns.twr_views_agree"}
    assert {finding.klass for finding in findings} == {checks.BLOCKER_INVARIANT}
    path_view = findings[0].observed["report.twr_mwr[0].return_path_cumulative_return"]
    assert path_view["window"] == [date(2026, 7, 1), date(2026, 7, 30)]


# --- R3 zero-capital member: reconcile by value (gates D and D2) -----------------
#
# A funded member whose own return is undefined across a zero-capital run (R3:
# UNAVAILABLE + ZERO_CAPITAL_RETURN_UNDEFINED) has no observation trace.  Its
# NAV and flows are admitted and hash bound in the epoch calculation inputs, and
# the strategy composite is the SUMMED-VALUE composite -- per boundary
# sum(V_end - F_eod) / sum(V_begin + F_bod) - 1 -- which the ledger and the HWM
# already use.  The beginning-capital weighted mean of member returns cannot
# take the income the member earns on a day it starts at zero capital.

R3_DAYS = (
    "2026-01-01",
    "2026-01-02",
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
)
R3_KEY = ("account-r", "strategy-shared", "2026-01-01", None)
_PRIVATE_DETAIL_FIELDS = (
    "own_path_links",
    "by_value_source",
    "performance_status",
    "performance_reason",
    "performance_opening_date",
)
_OWN_TRACE_MISSING = r"own trace path is missing for strategy-shared \(account-r\)"
_DO_NOT_RECONCILE = (
    "multi-account member own paths do not reconcile to the strategy composite"
)


def _r3_observations(
    days: tuple[str, ...], values: tuple[str, ...]
) -> dict[date, Decimal]:
    return {date.fromisoformat(d): Decimal(v) for d, v in zip(days, values)}


def _r3_leg(amount: str, recognized: str) -> Any:
    from quant_engine.portfolio_ledger.returns import FlowLeg

    value = Decimal(amount)
    return FlowLeg(
        amount=value,
        flow_class="EXTERNAL_IN" if value > 0 else "EXTERNAL_OUT",
        event_date=date.fromisoformat(recognized),
        nav_recognition_date=date.fromisoformat(recognized),
        timing="DATE_ONLY",
    )


def _usd(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _zero_run_source(
    account_id: str, days: tuple[str, ...], values: tuple[str, ...]
) -> dict[str, Any]:
    """The hash-bound calculation-input facts of the zero-run member.

    NAV rows and classified flows in the shape the epoch calculation inputs
    carry them (typed USD amounts, the flow's recognition date).  The 40 is
    withdrawn in full at the end of the second day.
    """

    return {
        "nav_observations": [
            {
                "account_id": account_id,
                "as_of_date": day,
                "nav": _usd(value),
                "record_id": f"{account_id}:nav:{day}",
                "value_basis": "ACCOUNT_VALUE",
            }
            for day, value in zip(days, values)
        ],
        "classified_flows": [
            {
                "account_id": account_id,
                "amount": _usd("-40"),
                "blocker_code": None,
                "classification_status": "CLASSIFIED",
                "disposition": "EXTERNAL",
                "event_date": days[1],
                "event_id": f"classified-flow:{account_id}:withdrawal",
                "flow_type": "WITHDRAWAL",
                "nav_recognition_date": days[1],
                "source_record_id": f"{account_id}:withdrawal",
            }
        ],
        "capital_anchors": [],
    }


def _r3_member_detail(
    source: dict[str, Any] | None, opening_day: str, **changes: Any
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "account_label": "Zero-run member",
        "last_four": "4321",
        "epoch_admission_status": "FUNDED",
        "epoch_output_status": "OK",
        "epoch_blocking_reason": None,
        "own_path_links": [],
        "performance_status": "UNAVAILABLE",
        "performance_reason": "ZERO_CAPITAL_RETURN_UNDEFINED",
        "performance_opening_date": opening_day,
        "by_value_source": source,
        "performance_payload": {
            "flow_count": 1,
            "flow_timing": None,
            "nav_observations": 6,
            "observed_end": "2026-01-08",
            "observed_start": opening_day,
            "twr": None,
            "twr_status": "UNAVAILABLE",
        },
    }
    detail.update(changes)
    return detail


def _three_member_catalog(rows: list[dict[str, str]]) -> dict[str, Any]:
    accounts = ("account-a", "account-b", "account-r")
    return {
        "assignments": [_owned_assignment(a, "strategy-shared") for a in accounts],
        "calendar_coverage": [],
        "flows": [],
        "nav_observations": [],
        "strategy_provider_evidence": [
            {
                "account": {
                    "account_scopes": [{"account_id": a} for a in accounts],
                    "observed_start": "2026-01-02",
                },
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": "a" * 64,
                "strategy_id": "strategy-shared",
            }
        ],
        "strategy_returns": [
            {
                "component_account_ids": list(accounts),
                "date": row["date"],
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": row["return"],
                "strategy_id": "strategy-shared",
            }
            for row in rows
        ],
    }


def _r3_world(
    *,
    canonical: str = "summed",
    canonical_income: str = "3.81",
    member_income: str = "3.81",
    twr_shift: str | None = None,
    r3_changes: dict[str, Any] | None = None,
    compact: bool = False,
) -> tuple[
    dict[str, Any],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[str, Any],
]:
    """Two traced members plus the zero-run member, and the strategy path.

    The traced members' numbers come from the return engine.  The canonical
    strategy rows are the link of the SUMMED member values and flows (the
    ledger construction), or -- ``canonical="weighted"`` -- the beginning-capital
    weighted mean with the zero-capital sub-period skipped.  ``member_income``
    differs from ``canonical_income`` only to tamper the member's own facts.
    ``compact`` keeps three dates (the synthetic baseline's window).
    """

    from quant_engine.portfolio_ledger.returns import (
        MemberReturn,
        OpeningPoint,
        composite,
        link_epoch,
        member_returns,
    )

    if compact:
        days = R3_DAYS[:2] + ("2026-01-03",)
        a_values, b_values = ("100", "101", "102.01"), ("200", "204", "232.08")
        zero_run = ("40", "0")
        deposit_day = days[2]
    else:
        days = R3_DAYS
        a_values = ("100", "101", "102.01", "103.0301", "104.060401", "105.10100501")
        b_values = ("200", "204", "208.08", "232", "236.64", "241.3728")
        zero_run = ("40", "0", "0", "0")
        deposit_day = days[3]
    canonical_r = _r3_observations(
        days, zero_run + (canonical_income,) * (len(days) - len(zero_run))
    )
    member_values = zero_run + (member_income,) * (len(days) - len(zero_run))
    first = date.fromisoformat(days[0])
    obs_a = _r3_observations(days, a_values)
    obs_b = _r3_observations(days, b_values)
    deposit = _r3_leg("20", deposit_day)
    withdrawal = _r3_leg("-40", days[1])
    prior = "LAST_OBSERVATION_BEFORE_START"
    epoch_a = link_epoch(obs_a, [], OpeningPoint(first, obs_a[first], prior))
    epoch_b = link_epoch(obs_b, [deposit], OpeningPoint(first, obs_b[first], prior))
    assert epoch_a.status == epoch_b.status == "AVAILABLE"
    if canonical == "summed":
        summed = {d: obs_a[d] + obs_b[d] + canonical_r[d] for d in obs_a}
        reference = link_epoch(
            summed,
            [deposit, withdrawal],
            OpeningPoint(
                first, summed[first], "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
            ),
        )
        assert reference.status == "AVAILABLE"
        rows = [
            {
                "date": link.end_date.isoformat(),
                "return": decimal_text(link.period_return),
            }
            for link in reference.links
        ]
    else:
        # The zero-run member enters only where it holds capital: its first
        # sub-period (40 -> 0 after the withdrawal, flow-neutral, return 0) and,
        # in the long world, the flat day after the income day.
        zero_member = [
            MemberReturn(
                "account-r",
                date.fromisoformat(days[1]),
                Decimal("40"),
                Decimal("0"),
                first,
            )
        ]
        if not compact:
            zero_member.append(
                MemberReturn(
                    "account-r",
                    date.fromisoformat(days[5]),
                    Decimal(canonical_income),
                    Decimal("0"),
                    date.fromisoformat(days[4]),
                )
            )
        weighted = composite(
            (
                *member_returns("account-a", epoch_a),
                *member_returns("account-b", epoch_b),
                *zero_member,
            )
        )
        rows = [
            {
                "date": item.end_date.isoformat(),
                "return": decimal_text(item.period_return),
            }
            for item in weighted
        ]
    shift = Decimal(twr_shift) if twr_shift else Decimal(0)
    details = _shared_member_epoch_details(
        {
            "account-a": decimal_text(epoch_a.twr + shift),
            "account-b": decimal_text(epoch_b.twr),
        },
        {
            "account-a": _manifest_trace_links(epoch_a),
            "account-b": _manifest_trace_links(epoch_b),
        },
    )
    details[R3_KEY] = _r3_member_detail(
        _zero_run_source("account-r", days, member_values),
        days[0],
        **(r3_changes or {}),
    )
    return _three_member_catalog(rows), details, {"a": epoch_a, "b": epoch_b}


def _performance(
    catalog: dict[str, Any],
    details: dict[tuple[str, str, str, str | None], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows = reporter._account_strategy_performance(catalog, None, set(details), details)
    return {row["account_id"]: row for row in rows}


def test_r3_zero_capital_member_reconciles_by_value_and_publishes_no_own_path() -> None:
    """D-1 (fail-before): the zero-run member enters the composite by value.

    Two traced members and a funded R3 member (40, withdrawn, flat at zero for
    three days, then 3.81 of income with no flow; empty trace, UNAVAILABLE,
    ZERO_CAPITAL_RETURN_UNDEFINED).  The canonical strategy path is the
    summed-value path.  The merge base raised ``own trace path is missing``;
    the strategy now publishes, the traced members keep their own paths, and
    the R3 row publishes no TWR and no return path (the oracle's "not
    derivable").
    """

    catalog, details, epochs = _r3_world()

    rows = _performance(catalog, details)

    for account_id, epoch in (("account-a", epochs["a"]), ("account-b", epochs["b"])):
        assert rows[account_id]["twr"] == decimal_text(epoch.twr)
        assert (
            rows[account_id]["return_path_status"]
            == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        )
        # The path compounds the link factors at 34 digits; the epoch TWR was
        # chained at 28, so equality is numerical, far inside the 1e-12 gate.
        assert abs(
            Decimal(rows[account_id]["return_path_cumulative_return"]) - epoch.twr
        ) < Decimal("1e-20")
        assert rows[account_id]["return_path_observations"] == len(epoch.links)
    zero_run = rows["account-r"]
    assert zero_run["twr"] is None
    assert zero_run["twr_status"] == "UNAVAILABLE"
    assert zero_run["epoch_admission_status"] == "FUNDED"
    for field in (
        "return_path_status",
        "return_path_cumulative_return",
        "return_path_observations",
        "return_path_observed_start",
        "return_path_observed_end",
        "return_path_series_sha256",
        "return_path_authority",
        "return_path_evidence_layer",
    ):
        assert zero_run[field] is None


def test_r3_reconcile_rejects_the_weighted_path_that_drops_the_zero_capital_income() -> (
    None
):
    """D-2: the canonical path must be the summed-value path, not the weighted skip.

    Same world, but the canonical strategy rows are the beginning-capital
    weighted mean with the zero-capital sub-period skipped: the 3.81 of income
    earned on zero capital is missing from it.  The summed-value reconcile
    refuses it (the single date the weighted construction cannot see).
    """

    catalog, details, _epochs = _r3_world(canonical="weighted")

    with pytest.raises(
        reporter.ReportValidationError,
        match=f"{_DO_NOT_RECONCILE} for strategy-shared",
    ):
        _performance(catalog, details)


def test_r3_world_still_gates_a_traced_member_epoch_twr() -> None:
    """D-3: the member gate is unchanged: a traced TWR off by 1e-9 fails closed."""

    catalog, details, _epochs = _r3_world(twr_shift="0.000000001")

    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member epoch TWR differs from the member return path",
    ):
        _performance(catalog, details)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        pytest.param(
            {"performance_reason": "FLOW_AFTER_CLOSING_OBSERVATION"},
            _OWN_TRACE_MISSING,
            id="traceless_funded_member_with_another_unavailable_reason",
        ),
        pytest.param(
            {"performance_status": "AVAILABLE", "performance_reason": None},
            _OWN_TRACE_MISSING,
            id="available_member_with_an_empty_trace",
        ),
        pytest.param(
            {"performance_status": None, "performance_reason": None},
            _OWN_TRACE_MISSING,
            id="performance_status_absent_with_an_empty_trace",
        ),
        pytest.param(
            {"performance_reason": None},
            _OWN_TRACE_MISSING,
            id="unavailable_without_any_reason",
        ),
        pytest.param(
            {"by_value_source": None},
            r"by-value source is missing for strategy-shared \(account-r\)",
            id="r3_member_without_its_calculation_inputs",
        ),
    ),
)
def test_r3_allow_list_keeps_every_other_untraced_member_failing_closed(
    changes: dict[str, Any], message: str
) -> None:
    """D-4: only UNAVAILABLE + ZERO_CAPITAL_RETURN_UNDEFINED enters by value."""

    catalog, details, _epochs = _r3_world(r3_changes=changes)

    with pytest.raises(reporter.ReportValidationError, match=message):
        _performance(catalog, details)


def test_r3_allow_list_refuses_a_traced_member_whose_performance_is_unavailable() -> (
    None
):
    """D-4 (fail-before): an UNAVAILABLE non-R3 member never enters as traced.

    The member carries a verified-looking trace AND reports its performance
    UNAVAILABLE for another reason.  The merge base treated any member with
    links as traced; the allow-list now refuses it by name.
    """

    catalog, details, epochs = _r3_world()
    details[R3_KEY].update(
        performance_reason="NONPOSITIVE_LINK_FACTOR",
        own_path_links=_manifest_trace_links(epochs["a"]),
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match=r"performance is unavailable outside the R3 allow-list "
        r"for strategy-shared \(account-r\): NONPOSITIVE_LINK_FACTOR",
    ):
        _performance(catalog, details)


def test_r3_income_tampered_by_a_small_amount_fails_the_1e_12_gate() -> None:
    """D-5: 3.81 -> 3.8101 in the member's own facts misses the path by ~3e-7.

    The gap is between 1e-12 and 1e-4 by construction: a band as wide as 1e-4
    would accept it, the 1e-12 gate does not.  The canonical path is unchanged.
    """

    catalog, details, _epochs = _r3_world(member_income="3.8101")

    with pytest.raises(
        reporter.ReportValidationError,
        match=f"{_DO_NOT_RECONCILE} for strategy-shared",
    ):
        _performance(catalog, details)
    # The same world with the untampered facts publishes: only the tamper fails.
    clean_catalog, clean_details, _ = _r3_world()
    assert _performance(clean_catalog, clean_details)["account-r"]["twr"] is None


def _mutated_source(mutation: str) -> dict[str, Any]:
    _catalog, details, _epochs = _r3_world()
    source = copy.deepcopy(details[R3_KEY]["by_value_source"])
    navs = source["nav_observations"]
    flows = source["classified_flows"]
    if mutation == "nav_not_usd":
        navs[2]["nav"]["currency"] = "EUR"
    elif mutation == "nav_not_available":
        navs[2]["nav"]["status"] = "UNAVAILABLE"
    elif mutation == "nav_other_account":
        navs[2]["account_id"] = "account-a"
    elif mutation == "nav_conflict":
        navs.append({**navs[2], "nav": _usd("7")})
    elif mutation == "flow_unclassified":
        flows[0]["classification_status"] = "BLOCKED"
    elif mutation == "flow_not_usd":
        flows[0]["amount"]["currency"] = "EUR"
    elif mutation == "flow_other_account":
        flows[0]["account_id"] = "account-a"
    elif mutation == "flow_after_last_nav":
        flows.append(
            {**flows[0], "event_id": "late", "nav_recognition_date": "2026-01-09"}
        )
    elif mutation == "flow_recognition_before_event":
        flows[0]["event_date"] = "2026-01-05"
    elif mutation == "anchor_unsupported":
        source["capital_anchors"].append(
            {
                "account_id": "account-r",
                "amount": _usd("10"),
                "capital_type": "SOMETHING_ELSE",
                "event_date": "2026-01-06",
                "nav_recognition_date": "2026-01-06",
            }
        )
    return source


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("nav_not_usd", "NAV observation lacks source-native USD"),
        ("nav_not_available", "NAV observation lacks source-native USD"),
        ("nav_other_account", "NAV observation account differs from the epoch"),
        ("nav_conflict", "NAV observations conflict on one date"),
        ("flow_unclassified", "classified flow is not CLASSIFIED"),
        ("flow_not_usd", "classified flow lacks source-native USD"),
        ("flow_other_account", "classified flow account differs from the epoch"),
        ("flow_after_last_nav", "flow is recognized after the last observation"),
        ("flow_recognition_before_event", "recognition precedes its economic event"),
        ("anchor_unsupported", "capital anchor type is unsupported"),
    ),
)
def test_r3_by_value_source_fails_closed_on_anything_the_hwm_inputs_refuse(
    mutation: str, message: str
) -> None:
    """The by-value facts follow HWM T0's rules; anything else raises.

    USD AVAILABLE NAVs, CLASSIFIED flows (signed at the NAV recognition date),
    STRATEGY_CAPITAL_ALLOCATION anchors as beginning-of-period legs and
    OPENING_CAPITAL_ANCHOR ignored; a flow after the last observation, a
    foreign account or a conflicting NAV fail closed.
    """

    catalog, details, _epochs = _r3_world()
    details[R3_KEY]["by_value_source"] = _mutated_source(mutation)

    with pytest.raises(
        reporter.ReportValidationError,
        match=rf"by-value source is invalid for strategy-shared \(account-r\): .*{message}",
    ):
        _performance(catalog, details)


def test_r3_by_value_opening_must_be_an_observation_of_the_member() -> None:
    catalog, details, _epochs = _r3_world(
        r3_changes={"performance_opening_date": "2025-12-31"}
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match=r"by-value source is invalid for strategy-shared \(account-r\): "
        r".*not an observation",
    ):
        _performance(catalog, details)


def test_r3_member_without_a_valued_sub_period_fails_closed() -> None:
    """An R3 member with nothing to value never silently drops out of the gate.

    The engine only reports ZERO_CAPITAL_RETURN_UNDEFINED for a sub-period that
    starts at zero capital, so an R3 member whose facts hold no NAV after its
    opening is inconsistent.  In a two-member strategy, accepting it with no
    periods would leave the traced member alone, the reconcile would be skipped
    (fewer than two members) and the canonical path never compared.
    """

    catalog, details, _epochs = _r3_world()
    del details[("account-b", "strategy-shared", "2026-01-01", None)]
    catalog["assignments"] = [
        item for item in catalog["assignments"] if item["account_id"] != "account-b"
    ]
    for evidence in catalog["strategy_provider_evidence"]:
        evidence["account"]["account_scopes"] = [
            scope
            for scope in evidence["account"]["account_scopes"]
            if scope["account_id"] != "account-b"
        ]
    source = details[R3_KEY]["by_value_source"]
    source["nav_observations"] = source["nav_observations"][:1]
    source["classified_flows"] = []

    with pytest.raises(
        reporter.ReportValidationError,
        match=r"by-value source is invalid for strategy-shared \(account-r\): "
        r".*no valued sub-period after the opening",
    ):
        _performance(catalog, details)


def test_r3_by_value_reads_opening_anchors_as_nothing_and_allocations_as_capital() -> (
    None
):
    """OPENING_CAPITAL_ANCHOR is ignored; a capital allocation joins the capital.

    An opening anchor leaves the composite untouched.  A STRATEGY_CAPITAL_ALLOCATION
    anchor is an exact-timestamp leg: it enlarges the member's beginning capital
    of the sub-period it is recognized in, so the summed path moves and no longer
    equals the canonical path -- proving the anchor is read, as capital.
    """

    catalog, details, _epochs = _r3_world()
    opening_anchor = {
        "account_id": "account-r",
        "amount": {"currency": "EUR", "status": "AVAILABLE", "value": "999"},
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "event_date": "2026-01-01",
        "nav_recognition_date": "2026-01-01",
    }
    details[R3_KEY]["by_value_source"]["capital_anchors"].append(opening_anchor)
    assert _performance(catalog, details)["account-r"]["twr"] is None

    allocation = {
        "account_id": "account-r",
        "amount": _usd("10"),
        "capital_type": "STRATEGY_CAPITAL_ALLOCATION",
        "event_date": "2026-01-06",
        "nav_recognition_date": "2026-01-06",
    }
    details[R3_KEY]["by_value_source"]["capital_anchors"].append(allocation)
    with pytest.raises(reporter.ReportValidationError, match=_DO_NOT_RECONCILE):
        _performance(catalog, details)


def test_r3_private_inputs_never_reach_a_published_row() -> None:
    """D-8: the trace, the by-value source and the R3 labels stay private."""

    catalog, details, _epochs = _r3_world()

    rows = _performance(catalog, details)

    assert set(rows) == {"account-a", "account-b", "account-r"}
    for row in rows.values():
        assert not set(row) & set(_PRIVATE_DETAIL_FIELDS)
    # The rows are what the fixture's public payloads say, no more.
    assert rows["account-r"]["flow_count"] == 1
    assert rows["account-r"]["nav_observations"] == 6


def test_public_builder_threads_the_r3_inputs_into_the_composite_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seam (fail-before): the real builder carries the R3 inputs to the gate.

    ``build_report_data`` rebuilds the current-epoch details from the verified
    epoch authority.  Dropping the private R3 inputs there would leave the
    zero-run member traceless (``own trace path is missing``) and the tamper
    below unseen; with them threaded the real path reaches the summed-value
    reconcile and fails closed on the tampered income, exactly as the unit
    path does.  A merge-base builder raises ``own trace path is missing``.
    """

    catalog, details, _epochs = _r3_world(compact=True)
    source = details[R3_KEY]["by_value_source"]
    # Tamper the member's own income (3.81 -> 3.8101) in its hash-bound facts.
    for nav in source["nav_observations"]:
        if nav["nav"]["value"] == "3.81":
            nav["nav"] = _usd("3.8101")
    for evidence in catalog["strategy_provider_evidence"]:
        evidence["account"]["provider_id"] = "synthetic"
        for scope in evidence["account"]["account_scopes"]:
            scope.update(observed_start="2026-01-01", observed_end="2026-01-03")
    catalog = json.loads(json.dumps(catalog).replace("strategy-shared", "strategy-1"))
    details = {
        (key[0], "strategy-1", key[2], key[3]): detail
        for key, detail in details.items()
    }
    crosswalk = {
        "calculation_cutoff_date": "2026-01-03",
        "rows": [
            {
                "account_id": key[0],
                "strategy_id": key[1],
                "effective_start": key[2],
                "effective_end": key[3],
                "account_label": "SYNTHETIC member",
                "last_four": "4321",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
            for key in details
        ],
    }
    full = _full_report_fixture()
    baseline = full._baseline_catalog()
    baseline["accounts"] = [
        *baseline["accounts"],
        {
            "account_id": "account-r",
            "base_currency": "USD",
            "provider": "SYNTHETIC",
            "role": "INVESTMENT",
        },
    ]
    baseline_root = full._published_baseline(tmp_path, {**baseline, **catalog})
    monkeypatch.setattr(
        full.reporter,
        "_validate_epoch_manifest_input",
        lambda *_args, **_kwargs: (
            {},
            {"generation_id": "a" * 64},
            b"{}",
            b"{}",
            json.dumps(crosswalk).encode("utf-8"),
            details,
        ),
    )

    with pytest.raises(
        full.reporter.ReportValidationError,
        match=_DO_NOT_RECONCILE,
    ):
        full.reporter.build_report_data(
            baseline_root,
            epoch_manifest_path=tmp_path / "synthetic-manifest.json",
            epoch_manifest_source_path=tmp_path / "synthetic-source.json",
            epoch_crosswalk_path=tmp_path / "synthetic-crosswalk.json",
            epoch_manifest_generation_root=REPOSITORY_ROOT,
        )

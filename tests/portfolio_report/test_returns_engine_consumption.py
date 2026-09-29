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

import hashlib
import importlib.util
from datetime import date
from decimal import Decimal, localcontext
import json
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
    members published the same composite path.
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
        # No link is silently dropped between the member's verified trace and
        # the published path: observation count, compounded value, endpoints
        # and series hash bind exactly the member's own linked series.
        assert row["twr"] == decimal_text(epoch.twr)
        assert row["return_path_status"] == "AVAILABLE_ACCOUNT_LIVE_RETURN_PATH"
        assert row["return_path_observations"] == len(own_series)
        assert row["return_path_observed_start"] == own_series[0]["date"]
        assert row["return_path_observed_end"] == own_series[-1]["date"]
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
    the same strategy is BLOCKED without a trace.  The fail-open early return
    let the blocked member skip the composite reconciliation for the funded
    members and publish the inflated paths; the missing member trace now
    fails closed instead of silently disabling the gates.
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
        match="multi-account member own trace path is missing",
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

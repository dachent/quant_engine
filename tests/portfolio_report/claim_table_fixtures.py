"""Synthetic payloads for the claim table tests.

Everything here is invented: two strategies, a handful of epochs, round
numbers.  It carries only the sections the claim-table predicates read.  The
payload is built to satisfy every predicate, so a row that fails on it has been
broken on purpose by its ``break_fixture``.
"""

from __future__ import annotations

from typing import Any

from claim_table import Payload

CUTOFF = "2026-09-17"


def _line(series_id: str, kind: str, origin: Any, first: int = 0) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "kind": kind,
        "first_index": first,
        "origin": origin,
    }


def _epoch(
    strategy: str, end: str, lifecycle: str, current: str, gain: str, net: str
) -> dict[str, Any]:
    return {
        "account_id": f"acct-{strategy}",
        "strategy_id": strategy,
        "status": "OK",
        "end": end,
        "lifecycle_state": lifecycle,
        "current_value": current,
        "gain": gain,
        "net_capital": net,
    }


def _perf(strategy: str, days: int, annualised: str | None) -> dict[str, Any]:
    return {
        "strategy_id": strategy,
        "window_days": days,
        "twr_annualised": annualised,
        "twr_status": "AVAILABLE",
        "mwr_status": "AVAILABLE",
        "mwr_coverage_status": "COMPLETE",
    }


def base_payload() -> Payload:
    live = [[0, 9, "LIVE"]]
    panel: dict[str, Any] = {
        "performance_tab": {
            "modes": {
                "LIVE_ONLY": {
                    "dates": ["2026-09-07", "2026-09-14"],
                    "lines": [
                        _line("strategy:A", "STRATEGY", [[2, 9, "LIVE"]], 2),
                        _line("strategy:B", "STRATEGY", [[4, 9, "LIVE"]], 4),
                    ],
                },
                "LIVE_PLUS_BACKTEST": {
                    "dates": ["2026-09-07", "2026-09-14"],
                    "lines": [
                        _line("strategy:A", "STRATEGY", live),
                        _line("strategy:B", "STRATEGY", live),
                        _line("portfolio:equal_weight", "PORTFOLIO", live),
                        _line("market:SPY", "BENCHMARK", None),
                        _line("market:SPXL", "BENCHMARK", None),
                    ],
                },
            }
        },
        "account_manifest": {
            "calculation_cutoff_date": CUTOFF,
            "rows": [
                _epoch("A", CUTOFF, "ACTIVE", "100.00", "10.00", "90.00"),
                _epoch("B", CUTOFF, "ACTIVE", "50.00", "5.00", "45.00"),
                _epoch("A", "2026-01-04", "RETIRED", "0", "-5.00", "5.00"),
            ],
        },
        "capital_reconciliation": {"open_epoch_count": 2},
        "performance": [
            _perf("A", 100, None),
            _perf("B", 100, None),
            _perf("A", 400, "0.10"),
        ],
        "counts": {"strategies": 2, "twr_available": 3, "twr_rows": 3, "positions": 2},
        "strategies": [
            {"strategy_id": "A", "implementation_gap_status": "tested"},
            {"strategy_id": "B", "implementation_gap_status": "not_testable"},
        ],
        "model_vs_actual": {
            "strategies": [
                {
                    "strategy_id": "A",
                    "primary_window_id": "W",
                    "windows": [{"id": "W"}],
                },
                {"strategy_id": "B", "primary_window_id": None, "windows": []},
            ]
        },
        "coverage": [
            {
                "item": "positions",
                "status": "PARTIAL",
                "disposition": {
                    "effect": "Accepted current and historical snapshots are reported."
                },
            },
            {"item": "nav", "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY"},
            {"item": "flows", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
        ],
        "position_snapshots": {"historical_rows": 3},
        "positions": [
            {"position_class": "CURRENT_AT_CUTOFF", "strategy_id": "A"},
            {"position_class": "CURRENT_AT_CUTOFF", "strategy_id": "B"},
            {"position_class": "HISTORICAL", "strategy_id": "A"},
        ],
        "leverage_disclosures": [
            {
                "coverage_status": "OBSERVED_ONLY",
                "amount": {"status": "OBSERVED", "value": "1"},
            },
            {
                "coverage_status": "UNAVAILABLE",
                "amount": {"status": "UNAVAILABLE", "value": None},
            },
        ],
        "portfolio_twr_cumulative": {"status": "AVAILABLE"},
        "portfolio_mwr_annualised": {"status": "UNAVAILABLE"},
        "advisory": {"hwm_candidate": {"kpi": {"value": "100"}}},
        "fact_status": {"positions": "PARTIAL", "fx": "PARTIAL", "nav": "COMPLETE"},
        "fact_coverage_matrix": [
            {"account_id": "a1", "fact_type": "nav"},
            {"account_id": "a1", "fact_type": "flows"},
            {"account_id": "a2", "fact_type": "nav"},
            {"account_id": "a2", "fact_type": "flows"},
        ],
        "orthogonal_risk_attribution": {
            "modes": {
                "LIVE_ONLY": {
                    "aggregates": [
                        {
                            "source_key": "k",
                            "weighting": [
                                {"start": "2026-01-01", "end": "2026-01-10", "n": 10}
                            ],
                            "components": {
                                "ids": ["x", "y"],
                                "share_of_total": ["0.5", "0.3"],
                            },
                            "closure": {
                                "residual_share": "0.15",
                                "cross_term_share": "0.05",
                            },
                        }
                    ],
                    "cells": [],
                }
            }
        },
        "dependence_modes": {
            "LIVE_ONLY": {
                "correlation_one": {
                    "components": [
                        {"series_id": "A", "allocation_weight": "0.6"},
                        {"series_id": "B", "allocation_weight": "0.4"},
                    ]
                },
                "economic_driver_cluster_budget": {
                    "clusters": [
                        {
                            "cluster_id": "C1",
                            "members": ["A", "B"],
                            "allocation_weight": "1.0",
                        }
                    ]
                },
            }
        },
        "vendor_gap_fills": {
            "fills": [
                {"date": "2026-01-27", "origin_label": "ADMITTED_ALTERNATE_SOURCE"}
            ]
        },
        "strategy_underwriting": {
            "rows": [
                {
                    "strategy_id": "A",
                    "advisory_disposition": {
                        "allocation_effect": "NONE",
                        "execution_effect": "NONE",
                    },
                }
            ]
        },
        "decomposition_lenses": {"lenses": {"x": {"coefficients": {"estimate": 1}}}},
        "hwm_summary": {
            "nav_coverage": "1",
            "uncovered_strategies": [],
            "excluded_strategies": [],
            "uncovered_value_usd": "0",
        },
    }
    report: dict[str, Any] = {
        "coverage_data_quality": [
            {
                "item": "positions",
                "disposition": {
                    "effect": "Accepted current and historical snapshots are reported."
                },
            },
            {
                "item": "flows",
                "disposition": {
                    "effect": "MWR is shown only where admitted flows are complete for that epoch."
                },
            },
        ],
        "positions": [
            {"snapshot_status": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT", "strategy_id": "A"},
            {"snapshot_status": "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT", "strategy_id": "B"},
            {
                "snapshot_status": "HISTORICAL_ACCEPTED_ACCOUNT_SNAPSHOT",
                "strategy_id": "A",
            },
        ],
    }
    return Payload(panel=panel, report=report)

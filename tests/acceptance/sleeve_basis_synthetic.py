"""SYNTHETIC inputs for the sleeve-basis tests (#624): invented accounts, values, flows.

Nothing here is real portfolio data.  The numbers are chosen so each figure can be
computed by hand with exact fractions in the tests:

* sleeve ``acct-parent-sa`` (strategy ``synthetic-alpha``) opens on 2026-02-02 with a
  transfer of 1000, receives 500 on 2026-02-04 and closes the window at NAV 1575 after
  allocated costs;
* the parent account's unallocated bucket carries the margin liability (-1400), so the
  live sleeve plus the bucket equals the account equity, 175.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from typing import Any

DECLARED = "synthetic-alpha"
OTHER_STRATEGIES = {"synthetic-beta": Decimal(3000), "synthetic-gamma": Decimal(5000)}
PARENT = "acct-parent"
SLEEVE = f"{PARENT}-sa"
BUCKET = f"{PARENT}-unallocated"
FIRST, LAST = "2026-02-02", "2026-02-06"
DAYS = ("2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06")
PARENT_EQUITY = Decimal(175)

DECLARATION = {
    "schema": "strategy-basis-declaration/1",
    "strategies": [{"strategy_id": DECLARED, "basis": "SLEEVE_EPOCH"}],
}


def declaration_bytes(strategy_id: str = DECLARED) -> bytes:
    document = copy.deepcopy(DECLARATION)
    document["strategies"][0]["strategy_id"] = strategy_id
    return json.dumps(document).encode()


def _rows(entries: list[tuple[str, int, int, int, int]]) -> list[dict[str, str]]:
    """NAV rows from ``(day, own_pnl, flow_in, flow_out, alloc)`` that close exactly."""

    rows: list[dict[str, str]] = []
    previous = 0
    cumulative = 0
    for day, pnl, flow_in, flow_out, alloc in entries:
        nav_end = previous + pnl + flow_in - flow_out
        cumulative += alloc
        rows.append(
            {
                "day": day,
                "alloc_total": str(alloc),
                "nav_prev": str(previous),
                "own_pnl": str(pnl),
                "flow_in": str(flow_in),
                "flow_out": str(flow_out),
                "nav_end": str(nav_end),
                "nav_end_after_alloc": str(nav_end + cumulative),
            }
        )
        previous = nav_end
    return rows


def _leg(
    pair: str, day: str, account: str, other: str, kind: str, amount: int
) -> dict[str, Any]:
    return {
        "account_id": account,
        "amount": str(amount),
        "class": kind,
        "counterparty_account_id": other,
        "event_date": day,
        "flow_type": "TRANSFER:INTERNAL:SLEEVE",
        "nav_recognition_date": day,
        "pair_id": pair,
    }


def facts() -> dict[str, Any]:
    """Sleeve facts: one live sleeve of the declared strategy and one retired sleeve."""

    live = _rows(
        [
            ("2026-02-02", -10, 1000, 0, 2),
            ("2026-02-03", 99, 0, 0, 0),
            ("2026-02-04", 0, 500, 0, 0),
            ("2026-02-05", 159, 0, 0, 0),
            ("2026-02-06", -175, 0, 0, 0),
        ]
    )
    bucket_level = 100
    bucket_days = []
    for day in DAYS:
        internal = {"2026-02-02": -1000, "2026-02-04": -500}.get(day, 0)
        bucket_level += internal
        bucket_days.append(
            {
                "day": day,
                "external_flow": "0",
                "internal_flow_net": str(internal),
                "nav_end": str(bucket_level),
                "residual_pnl": "0",
            }
        )
    return {
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": PARENT,
        "window": {"first_day": FIRST, "last_day": LAST},
        "sleeves": [
            {
                "account_id": f"{PARENT}-sx",
                "strategy_id": "synthetic-retired",
                "first_day": FIRST,
                "last_day": "2026-02-03",
                "days": [
                    {"day": FIRST, "nav_end_after_alloc": "0"},
                    {"day": "2026-02-03", "nav_end_after_alloc": "0"},
                ],
            },
            {
                "account_id": SLEEVE,
                "strategy_id": DECLARED,
                "first_day": FIRST,
                "last_day": LAST,
                "days": live,
            },
        ],
        "flows": [
            _leg("p1", "2026-02-02", BUCKET, SLEEVE, "INTERNAL_OUT", 1000),
            _leg("p1", "2026-02-02", SLEEVE, BUCKET, "INTERNAL_IN", 1000),
            _leg("p2", "2026-02-04", BUCKET, SLEEVE, "INTERNAL_OUT", 500),
            _leg("p2", "2026-02-04", SLEEVE, BUCKET, "INTERNAL_IN", 500),
        ],
        "perimeter_account": {
            "account_id": BUCKET,
            "opening_nav": "100",
            "days": bucket_days,
        },
    }


# the figures the facts above must produce, as exact integers over integers
NAV = {"d2": (1091, 992), "d3": (1091, 1091), "d4": (1750, 1591), "d5": (1575, 1750)}
FINAL_VALUE = Decimal(1575)
TWR_NUM, TWR_DEN = 1091 * 1575, 992 * 1591  # 1 + twr
MAX_DRAWDOWN_NUM, MAX_DRAWDOWN_DEN = -1, 10  # the last day's -10%
WINDOW_DAYS = 4


def governed_events() -> list[dict[str, Any]]:
    """Governed external flows: two contributions and one withdrawal (a portfolio MWR)."""

    return [
        {
            "classification": "OPENING_CONTRIBUTED_BASIS",
            "date": "2025-01-01",
            "invested_capital_effect_usd": "8000",
        },
        {
            "classification": "EXTERNAL_CONTRIBUTION",
            "date": "2025-07-01",
            "invested_capital_effect_usd": "1000",
        },
        {
            "classification": "EXTERNAL_WITHDRAWAL",
            "date": "2025-10-01",
            "invested_capital_effect_usd": "-200",
        },
        {
            "classification": "INTERNAL_TRANSFER",
            "date": "2025-11-01",
            "invested_capital_effect_usd": "0",
        },
    ]


# ------------------------------------------------------------------ expected baseline


def _row(
    metric: str, entity: str, value: str, unit: str, window: str = ""
) -> dict[str, Any]:
    return {
        "metric_id": metric,
        "entity": entity,
        "window": window,
        "expected": value,
        "unit": unit,
        "tolerance": "0.01" if unit == "USD" else "0.00000001",
        "as_of": "2026-02-06",
    }


def baseline() -> dict[str, Any]:
    """An expected baseline with the declared strategy on its parent-account basis."""

    capital = {
        DECLARED: Decimal("175.00"),
        "synthetic-beta": Decimal("3000.00"),
        "synthetic-gamma": Decimal("5000.00"),
    }
    total = sum(capital.values(), Decimal(0))
    quantum = Decimal("1e-12")
    metrics = [
        _row("twr.strategy", DECLARED, "0.050000000000", "ratio", "ITD"),
        _row("twr.strategy", "synthetic-beta", "0.100000000000", "ratio", "ITD"),
        _row("twr.strategy", "synthetic-gamma", "0.200000000000", "ratio", "ITD"),
        _row("mwr.strategy", DECLARED, "0.300000000000", "ratio", "ITD"),
        _row("mwr.strategy", "synthetic-beta", "0.110000000000", "ratio", "ITD"),
        _row("mwr.strategy", "synthetic-gamma", "0.210000000000", "ratio", "ITD"),
        _row("capital.portfolio.systematic.cutoff", "portfolio", f"{total}", "USD"),
        _row(
            "universe.derived_epoch_ids",
            "portfolio",
            [f"{SLEEVE}|{DECLARED}|{FIRST}|open"],  # type: ignore[arg-type]
            "list",
        ),
        _row("twr.account", SLEEVE, "0.088737000000", "ratio", "ITD"),
        _row("capital.epoch.closing", SLEEVE, "1575.00", "USD"),
        _row("capital.account.cutoff", PARENT, "175.00", "USD"),
    ]
    for strategy, value in sorted(capital.items()):
        metrics.append(_row("capital.strategy.cutoff", strategy, f"{value}", "USD"))
        metrics.append(
            _row(
                "weight.strategy.cutoff",
                strategy,
                format((value / total).quantize(quantum), "f"),
                "ratio",
            )
        )
    return {
        "schema": "qe-cp-expected-baseline/1",
        "cutoff": LAST,
        "metrics": metrics,
        "not_derivable": [
            {"metric_id": "capital.portfolio.liquid.cutoff", "entity": "portfolio"}
        ],
    }


# ---------------------------------------------------------------------------- panel


def panel_before() -> dict[str, Any]:
    """A panel whose declared strategy row is on the parent-account basis."""

    figures: dict[str, Any] = {
        "account_live_cumulative_return": "0.05",
        "ytd_twr": "0.05",
        "calendar_year_twr": {"2026": "0.05"},
        "account_live_cagr": "0.9",
        "account_live_max_drawdown": "-0.2",
        "hwm_max_drawdown": "-0.2",
        "hwm_current_drawdown": "-0.05",
        "current_value_usd": "175",
        "cost_rate_denominator_usd": "175",
        "systematic_weight": "0.0214",
        "account_live_return_series_sha256": "ab" * 32,
        "account_live_observed_start": "2026-01-20",
        "account_live_observed_end": LAST,
        "account_live_return_observations": 12,
    }
    return {
        "_note": "SYNTHETIC: invented panel for the #624 non-diminish test",
        "strategies": [
            {
                "strategy_id": DECLARED,
                **figures,
                "provider_evidence": {
                    "account": {
                        "cumulative_return": "0.05",
                        "current_drawdown": "-0.05",
                        "max_drawdown": "-0.2",
                        "maximum_drawdown": "-0.2",
                        "return_series_sha256": "ab" * 32,
                        "observed_start": "2026-01-20",
                        "observed_end": LAST,
                        "return_observations": 12,
                    }
                },
                "return_scope": {
                    "basis": "PARENT_ACCOUNT_LIVE_SERIES",
                    "parent_account_ids": [PARENT],
                },
            },
            {"strategy_id": "synthetic-beta", "current_value_usd": "3000"},
        ],
        "account_summary": [
            {"account_id": PARENT, "latest_value_usd": "175", "as_of_date": LAST}
        ],
        "capital": {"systematic": "8175"},
    }


def panel_restated(*, home: bool = True, label: bool = True) -> dict[str, Any]:
    """The same panel after B: the row on the sleeve, the account figures at their home."""

    before = panel_before()
    row = before["strategies"][0]
    account_basis = {
        "basis": "WHOLE_ACCOUNT_INCL_UNALLOCATED_BUCKET" if label else "",
        "cumulative_return": row["account_live_cumulative_return"],
        "annualised": row["account_live_cagr"],
        "max_drawdown": row["account_live_max_drawdown"],
        "current_drawdown": row["hwm_current_drawdown"],
        "value_usd": row["current_value_usd"],
        "weight": row["systematic_weight"],
        "series_sha256": row["account_live_return_series_sha256"],
        "window_start": row["account_live_observed_start"],
        "window_end": row["account_live_observed_end"],
        "return_observations": row["account_live_return_observations"],
        "year_to_date": row["ytd_twr"],
        "calendar_year": row["calendar_year_twr"]["2026"],
    }
    sleeve = {
        "account_live_cumulative_return": "0.088",
        "ytd_twr": "0.088",
        "calendar_year_twr": {"2026": "0.088"},
        "account_live_cagr": "1.2",
        "account_live_max_drawdown": "-0.1",
        "hwm_max_drawdown": "-0.1",
        "hwm_current_drawdown": "-0.1",
        "current_value_usd": "1575",
        "cost_rate_denominator_usd": "1575",
        "systematic_weight": "0.1645",
        "account_live_return_series_sha256": "cd" * 32,
        "account_live_observed_start": FIRST,
        "account_live_return_observations": 5,
    }
    row.update(sleeve)
    row["provider_evidence"] = {
        "account": {
            "cumulative_return": "0.088",
            "current_drawdown": "-0.1",
            "max_drawdown": "-0.1",
            "maximum_drawdown": "-0.1",
            "return_series_sha256": "cd" * 32,
            "observed_start": FIRST,
            "observed_end": LAST,
            "return_observations": 5,
        }
    }
    row["return_scope"] = {
        "basis": "SLEEVE_EPOCH",
        "parent_account_ids": [PARENT],
        **(
            {"account_basis_ref": f"account_summary[{PARENT}].account_basis"}
            if home
            else {}
        ),
    }
    if home:
        before["account_summary"][0]["account_basis"] = account_basis
    return before

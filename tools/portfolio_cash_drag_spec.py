"""Write each strategy's cash-drag status into an attribution spec template.

``cash_drag_status`` was authored text ("no complete daily cash path").  Here it is the
result of :func:`portfolio_cash_drag.derive_cash_drag` on hashed daily balances:

* a strategy source lists one or more account files (``date,nav,cash`` CSV, or the IBKR
  EquitySummary history with ``reportDate``/``total``/``cash``); a strategy's path is the
  per-day sum over its accounts and exists only on days every account has;
* a source with ``unavailable`` names why there is no path (the reason is published);
* the benchmark is the row's own reference series, the cash yield the BIL return series,
  both read from the pinned return and style CSVs; the window is the account series'
  own dates.

Status is EVIDENCED only when the derivation succeeds; the reason carries the
cumulative figure and the observation count.  Nothing is filled or carried.
"""

from __future__ import annotations

import copy
import csv
from decimal import Decimal
from pathlib import Path
from typing import Any

from portfolio_cash_drag import STATUS_EVIDENCED, derive_cash_drag

ROW_LISTS = ("comparisons", "benchmark_comparisons")


def read_balances(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(nav, cash) by ISO date from a ``date,nav,cash`` CSV or an IBKR history CSV."""

    nav: dict[str, str] = {}
    cash: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if "reportDate" in row:
                raw = row["reportDate"]
                day = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                nav[day], cash[day] = row["total"], row["cash"]
            else:
                nav[row["date"]], cash[row["date"]] = row["nav"], row["cash"]
    return nav, cash


def combine(paths: list[Path]) -> tuple[dict[str, str], dict[str, str]]:
    """Per-day sum over accounts, on the days every account has a balance."""

    parts = [read_balances(path) for path in paths]
    days = set.intersection(*(set(n) for n, _ in parts))
    nav = {d: str(sum(Decimal(n[d]) for n, _ in parts)) for d in days}
    cash = {d: str(sum(Decimal(c[d]) for _, c in parts)) for d in days}
    return nav, cash


def series(rows: list[dict[str, str]], series_id: str) -> dict[str, str]:
    return {r["date"]: r["return"] for r in rows if r["series_id"] == series_id}


def derive_strategy(
    strategy_id: str,
    source: dict[str, Any],
    returns_rows: list[dict[str, str]],
    reference_series_id: str,
    cash_returns: dict[str, str],
) -> dict[str, Any]:
    if "unavailable" in source:
        return {"status": "UNAVAILABLE", "reason": str(source["unavailable"])}
    nav, cash = combine([Path(p) for p in source["paths"]])
    account = series(returns_rows, f"account:{strategy_id}")
    reference = series(returns_rows, reference_series_id)
    if not account or not reference:
        return {
            "status": "UNAVAILABLE",
            "reason": "no account or benchmark return series",
        }
    days = sorted(d for d in account if d in reference)
    if len(days) < 2:
        return {"status": "UNAVAILABLE", "reason": "fewer than two shared return dates"}
    out = derive_cash_drag(
        start=days[0],
        end=days[-1],
        nav=nav,
        cash=cash,
        benchmark_returns={d: reference[d] for d in days},
        cash_returns=cash_returns,
    )
    if out["status"] != STATUS_EVIDENCED:
        return {"status": "UNAVAILABLE", "reason": out["reason"]}
    return {
        "status": STATUS_EVIDENCED,
        "reason": (
            f"cash drag {out['cash_drag']} cumulative, {out['reason']}; benchmark "
            f"{reference_series_id}, cash yield BIL, positive cash only"
        ),
    }


def apply(
    template: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    returns_rows: list[dict[str, str]],
    cash_returns: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    spec = copy.deepcopy(template)
    results: dict[str, dict[str, Any]] = {}
    references = {
        row["strategy_id"]: row["reference_series_id"]
        for row in spec["benchmark_comparisons"]
        if row.get("reference_series_id")
    }
    for name in ROW_LISTS:
        for row in spec[name]:
            strategy = row["strategy_id"]
            source = sources.get(strategy)
            if source is None or strategy not in references:
                continue
            if strategy not in results:
                results[strategy] = derive_strategy(
                    strategy, source, returns_rows, references[strategy], cash_returns
                )
            row["cash_drag_status"] = results[strategy]["status"]
            row["cash_drag_reason"] = results[strategy]["reason"]
    return spec, results

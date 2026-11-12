"""Write each strategy's cash-drag status into an attribution spec template.

``cash_drag_status`` was authored text ("no complete daily cash path").  Here it is the
result of :func:`portfolio_cash_drag.derive_cash_drag` on hashed daily balances:

* a strategy source lists its accounts; each names a ``date,nav,cash`` CSV (or the IBKR
  EquitySummary history with ``reportDate``/``total``/``cash``), the date it was funded
  from (the capture may not start later), and the statement it closes to;
* a source with ``unavailable`` names why there is no path (the reason is published);
* the benchmark is the row's own reference series, the cash yield the BIL return series,
  both read from the pinned return and style CSVs; the window is the account return
  series' own dates, and every reference day inside it must have an account return.

Cash is each account's own positive balance (a negative balance is borrowing and is never
netted against another account's cash).  Status is EVIDENCED only when the derivation
succeeds and every statement figure closes; every row of the template is set (a row with
no source or no reference is UNAVAILABLE, never left as authored).  Nothing is filled or
carried; every malformed input refuses.
"""

from __future__ import annotations

import copy
import csv
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant_engine.core.market_calendar import is_nyse_trading_day

from portfolio_cash_drag import STATUS_EVIDENCED, STATUS_UNAVAILABLE, derive_cash_drag

ROW_LISTS = ("comparisons", "benchmark_comparisons")


def _decimal(text: str, what: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"{what} is not a number: {text!r}") from error
    if not value.is_finite():
        raise ValueError(f"{what} is not finite: {text!r}")
    return value


def _iso(text: str) -> str:
    return date.fromisoformat(text).isoformat()


def read_balances(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(nav, cash) by ISO date from a ``date,nav,cash`` CSV or an IBKR history CSV."""

    nav: dict[str, str] = {}
    cash: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if "reportDate" in row:
                raw = row["reportDate"]
                day = _iso(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
                value_nav, value_cash = row["total"], row["cash"]
            else:
                day = _iso(row["date"])
                value_nav, value_cash = row["nav"], row["cash"]
            if day in nav:
                raise ValueError(f"{path.name}: repeated balance date {day}")
            _decimal(value_nav, f"{path.name} nav {day}")
            _decimal(value_cash, f"{path.name} cash {day}")
            nav[day], cash[day] = value_nav, value_cash
    if not nav:
        raise ValueError(f"{path.name}: no balance rows")
    return nav, cash


def combine(
    accounts: list[tuple[Path, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Per-day strategy NAV and positive cash, summed over its accounts.

    Each account contributes ``max(cash, 0)`` (borrowing is never netted against other
    cash).  An account contributes nothing before its declared funding date; its capture
    may not start after that date, and after it the account must have a balance on every
    day or the day is dropped (the derivation then refuses on the missing observation).
    """

    parts = []
    for path, funded_from in accounts:
        nav, cash = read_balances(path)
        funded = _iso(funded_from)
        if min(nav) > funded:
            raise ValueError(
                f"{path.name}: capture starts {min(nav)}, after the declared "
                f"funding date {funded_from}"
            )
        parts.append(
            (
                {d: v for d, v in nav.items() if d >= funded},
                {d: v for d, v in cash.items() if d >= funded},
            )
        )
    firsts = [_iso(f) for _, f in accounts]
    lasts = [max(n) for n, _ in parts]
    days = {d for n, _ in parts for d in n}
    nav_out: dict[str, str] = {}
    cash_out: dict[str, str] = {}
    for day in sorted(d for d in days if d <= min(lasts)):
        if any(first <= day and day not in n for (n, _), first in zip(parts, firsts)):
            continue
        nav_out[day] = str(sum(Decimal(n[day]) for n, _ in parts if day in n))
        cash_out[day] = str(
            sum(max(Decimal(c[day]), Decimal(0)) for n, c in parts if day in n)
        )
    return nav_out, cash_out


def statement_figures(
    accounts: list[dict[str, Any]],
) -> dict[str, str]:
    """Strategy-level statement cash (positive per account) by date.

    Schwab accounts close to the month-end ``stmt_cash`` of their statement CSV, compared
    with the account's own daily cash to the cent; an IBKR account's daily history is the
    broker statement itself and is admitted only when its extraction recorded no
    NAV-identity mismatch.  A date counts only when every account that exists then has a
    figure for it.
    """

    per_account: list[dict[str, Decimal]] = []
    for account in accounts:
        _, cash = read_balances(Path(account["path"]))
        cash = {d: v for d, v in cash.items() if d >= _iso(account["funded_from"])}
        figures: dict[str, Decimal] = {}
        if "statement_csv" in account:
            with Path(account["statement_csv"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                for row in csv.DictReader(handle):
                    day = _iso(row["statement_date"])
                    if day < account.get("statement_from", "0000-01-01"):
                        continue  # before the declared strategy windows
                    if not row["stmt_cash"].strip():
                        continue  # a statement with no cash line is not a figure
                    figure = _decimal(row["stmt_cash"], f"statement cash {day}")
                    if day not in cash:
                        raise ValueError(f"statement {day} has no daily cash balance")
                    if abs(Decimal(cash[day]) - figure) > Decimal("0.005"):
                        raise ValueError(
                            f"daily cash does not close to the statement on {day}"
                        )
                    if day in figures:
                        raise ValueError(f"repeated statement date {day}")
                    figures[day] = figure
        else:
            summary = json.loads(
                Path(account["broker_series_summary"]).read_text(encoding="utf-8")
            )
            entry = _find_account(summary, str(account["summary_key"]))
            if entry.get("total_vs_changeinnav_mismatch_days") != []:
                raise ValueError("broker history has NAV-identity mismatch days")
            figures = {day: Decimal(value) for day, value in cash.items()}
        per_account.append(figures)
    firsts = [_iso(a["funded_from"]) for a in accounts]
    out: dict[str, str] = {}
    for day in sorted({d for f in per_account for d in f}):
        existing = [
            f for f, first in zip(per_account, firsts, strict=True) if first <= day
        ]
        if all(day in f for f in existing):
            out[day] = str(sum(max(f[day], Decimal(0)) for f in existing))
    return out


def _find_account(summary: Any, key: str) -> dict[str, Any]:
    if isinstance(summary, dict):
        entry = summary.get(key)
        if isinstance(entry, dict):
            return dict(entry)
        for value in summary.values():
            found = _find_account(value, key)
            if found:
                return found
    return {}


def _trading_days_between(d0: str, d1: str) -> bool:
    day = date.fromisoformat(d0) + timedelta(days=1)
    end = date.fromisoformat(d1)
    while day < end:
        if is_nyse_trading_day(day):
            return True
        day += timedelta(days=1)
    return False


def bil_returns(style_json: Path) -> dict[str, str]:
    """BIL simple close-to-close returns by ISO date from the pinned Yahoo capture."""

    rows = json.loads(style_json.read_text(encoding="utf-8"), parse_float=Decimal)[
        "rows"
    ]
    closes: dict[str, Decimal] = {}
    for r in rows:
        if r["Ticker"] != "BIL" or r.get("Close") is None:
            continue
        day = _iso(r["Date"][:10])
        close = Decimal(str(r["Close"]))
        if day in closes:
            raise ValueError(f"BIL: repeated close date {day}")
        if close <= 0:
            raise ValueError(f"BIL: non-positive close on {day}")
        closes[day] = close
    ordered = sorted(closes.items())
    out: dict[str, str] = {}
    for (d0, c0), (d1, c1) in zip(ordered, ordered[1:], strict=False):
        # a return across a missing close is not a one-day return: leave the day out,
        # and the derivation refuses if it needs it
        if _trading_days_between(d0, d1):
            continue
        out[d1] = str(c1 / c0 - 1)
    return out


def series(rows: list[dict[str, str]], series_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in rows:
        if r["series_id"] != series_id:
            continue
        if r["date"] in out:
            raise ValueError(f"{series_id}: repeated return date {r['date']}")
        _decimal(r["return"], f"{series_id} return {r['date']}")
        out[r["date"]] = r["return"]
    return out


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": STATUS_UNAVAILABLE, "reason": reason}


def derive_strategy(
    strategy_id: str,
    source: dict[str, Any],
    returns_rows: list[dict[str, str]],
    reference_series_id: str,
    cash_returns: dict[str, str],
) -> dict[str, Any]:
    if "unavailable" in source:
        return _unavailable(str(source["unavailable"]))
    try:
        account = series(returns_rows, f"account:{strategy_id}")
        reference = series(returns_rows, reference_series_id)
    except ValueError as error:
        return _unavailable(f"return series refused: {error}")
    if not account or not reference:
        return _unavailable("no account or benchmark return series")
    low, high = min(account), max(account)
    days = sorted(d for d in reference if low <= d <= high)
    if len(days) < 2:
        return _unavailable("fewer than two benchmark days in the account window")
    absent = [d for d in days if d not in account]
    if absent:
        return _unavailable(
            f"account return series lacks {len(absent)} benchmark day(s), "
            f"first {absent[0]}"
        )
    # NYSE closed days (weekends, holidays) have no BIL close: the cash instrument did
    # not move, its return is exactly zero; an open day without a close stays missing.
    cash_returns = {
        **{
            d: "0"
            for d in days
            if d not in cash_returns and not is_nyse_trading_day(date.fromisoformat(d))
        },
        **cash_returns,
    }
    earlier = [d for d in reference if d < days[0]]
    opening = max(earlier) if earlier else None
    try:
        accounts = source["accounts"]
        nav, cash = combine(
            [(Path(a["path"]), str(a["funded_from"])) for a in accounts]
        )
        statements = statement_figures(accounts)
    except (ValueError, KeyError, OSError) as error:
        return _unavailable(f"cash path input refused: {error}")
    out = derive_cash_drag(
        start=days[0],
        end=days[-1],
        nav=nav,
        cash=cash,
        benchmark_returns={d: reference[d] for d in days},
        cash_returns=cash_returns,
        statement_cash=statements,
        opening_day=opening,
    )
    if out["status"] != STATUS_EVIDENCED:
        return _unavailable(out["reason"])
    return {
        "status": STATUS_EVIDENCED,
        "reason": (
            f"cash drag {out['cash_drag']} cumulative, {out['reason']}; "
            f"reference {reference_series_id}, cash yield BIL, positive cash per account only"
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
            reference = row.get("reference_series_id") or references.get(strategy)
            source = sources.get(strategy)
            if source is None:
                result = _unavailable("no cash-drag source declared for this strategy")
            elif reference is None:
                result = _unavailable("no benchmark reference series for this row")
            else:
                key = f"{strategy}|{reference}"
                if key not in results:
                    results[key] = derive_strategy(
                        strategy, source, returns_rows, reference, cash_returns
                    )
                result = results[key]
            row["cash_drag_status"] = result["status"]
            row["cash_drag_reason"] = result["reason"]
    return spec, results


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--returns", type=Path, required=True)
    ap.add_argument("--style-json", type=Path, required=True)
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    a = ap.parse_args(argv)
    with a.returns.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    spec, results = apply(
        json.loads(a.template.read_text(encoding="utf-8")),
        json.loads(a.sources.read_text(encoding="utf-8")),
        rows,
        bil_returns(a.style_json),
    )
    a.out.write_bytes((json.dumps(spec, indent=2) + "\n").encode())
    a.report.write_bytes(
        (json.dumps(results, indent=2, sort_keys=True) + "\n").encode()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

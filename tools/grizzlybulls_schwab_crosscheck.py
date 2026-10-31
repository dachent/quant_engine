"""Compare GrizzlyBulls signals with Schwab fills. Signals are not fills.

Offline and read-only: it reads a captured trade-history CSV and the
``schwab-generation.json`` catalog of one retained Schwab generation. A vendor
signal is an instruction at a vendor price; a Schwab fill is what the account
actually did, later and at its own price. The report measures that gap (lag in
calendar days, unmatched events, daily long/flat disagreement) and never gates a
release or substitutes one series for the other.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys

from quant_engine.web_contracts import SignalSeries, parse_trade_history_csv


class CrosscheckError(ValueError):
    """The Schwab catalog or the signal series cannot be compared."""


@dataclass(frozen=True)
class Fill:
    day: date
    side: str
    quantity: Decimal


def load_fills(
    catalog: object, account_id: str, symbol: str
) -> tuple[list[Fill], Decimal]:
    """Return buy/sell fills and the current position quantity for one symbol."""

    if not isinstance(catalog, dict):
        raise CrosscheckError("catalog must be an object")
    fills: list[Fill] = []
    for row in catalog.get("transactions", []):
        if (
            row.get("account_id") == account_id
            and row.get("symbol") == symbol
            and row.get("kind") in {"Buy", "Sell"}
        ):
            quantity = row.get("quantity")
            if quantity is None:
                raise CrosscheckError("Schwab fill has no quantity")
            fills.append(
                Fill(
                    date.fromisoformat(row["event_date"]),
                    row["kind"],
                    Decimal(quantity),
                )
            )
    held = [
        Decimal(p["quantity"])
        for p in catalog.get("positions", [])
        if p.get("account_id") == account_id and p.get("symbol") == symbol
    ]
    if len(held) > 1:
        raise CrosscheckError("more than one current position for the symbol")
    if not fills:
        raise CrosscheckError("no Schwab fills for the account and symbol")
    return sorted(fills, key=lambda f: f.day), (held[0] if held else Decimal(0))


def _signal_long_on(series: SignalSeries, days: Sequence[date]) -> dict[date, bool]:
    """Vendor state at the end of each day (the last signal on or before it)."""

    stamps = [e.moment.date() for e in series.events]
    result: dict[date, bool] = {}
    for day in days:
        index = bisect_right(stamps, day) - 1
        if index >= 0:
            result[day] = series.events[index].long
    return result


def crosscheck(
    series: SignalSeries,
    fills: Sequence[Fill],
    final_quantity: Decimal,
    *,
    max_lag_days: int = 7,
) -> dict[str, object]:
    signals = [e for e in series.events if e.action != "Hypothetical Sell"]
    matches: list[dict[str, object]] = []
    unmatched_fills: list[str] = []
    for fill in fills:
        want_long = fill.side == "Buy"
        prior = [
            e for e in signals if e.long == want_long and e.moment.date() <= fill.day
        ]
        if not prior or (fill.day - prior[-1].moment.date()).days > max_lag_days:
            unmatched_fills.append(fill.day.isoformat())
            continue
        matches.append(
            {
                "fill": fill.day.isoformat(),
                "side": fill.side,
                "signal": prior[-1].moment.isoformat(),
                "lag_days": (fill.day - prior[-1].moment.date()).days,
            }
        )
    first, last = fills[0].day, max(fills[-1].day, series.last.date())
    window = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    held = final_quantity - sum(
        (f.quantity if f.side == "Buy" else -f.quantity for f in fills), Decimal(0)
    )
    signal_state = _signal_long_on(series, window)
    disagreement: list[str] = []
    cursor, quantity = 0, held
    for day in window:
        while cursor < len(fills) and fills[cursor].day <= day:
            fill = fills[cursor]
            quantity += fill.quantity if fill.side == "Buy" else -fill.quantity
            cursor += 1
        if (
            day in signal_state
            and (quantity > 0) != signal_state[day]
            and day.weekday() < 5
        ):
            disagreement.append(day.isoformat())
    matched_signals = {m["signal"] for m in matches}
    unfilled = [
        e.moment.isoformat()
        for e in signals
        if first <= e.moment.date() <= last
        and e.moment.isoformat() not in matched_signals
    ]
    lags = sorted(int(str(m["lag_days"])) for m in matches)
    return {
        "disclaimer": "signals are not fills; informational, never a gate",
        "fill_count": len(fills),
        "matched_fill_count": len(matches),
        "unmatched_fills": unmatched_fills,
        "signals_in_window_without_fill": unfilled,
        "lag_days": {"min": lags[0], "max": lags[-1], "median": lags[len(lags) // 2]}
        if lags
        else None,
        "weekday_state_disagreement_days": len(disagreement),
        "weekday_state_disagreement_sample": disagreement[:10],
        "position_quantity_before_first_fill": str(held),
        "window": [first.isoformat(), last.isoformat()],
        "matches": matches,
    }


def compare_series(first: SignalSeries, second: SignalSeries) -> dict[str, object]:
    """Match two vendor signal series on (timestamp, action); report the gap."""

    other = {(e.moment, e.action): e for e in second.events}
    mine = {(e.moment, e.action) for e in first.events}
    matched = [e for e in first.events if (e.moment, e.action) in other]
    diffs = [
        abs(e.price - other[(e.moment, e.action)].price)  # type: ignore[operator]
        for e in matched
        if e.price is not None and other[(e.moment, e.action)].price is not None
    ]
    extra = sorted(e.moment for e in second.events if (e.moment, e.action) not in mine)
    return {
        "first_rows": len(first.events),
        "second_rows": len(second.events),
        "matched_rows": len(matched),
        "match_rate_of_first": len(matched) / len(first.events),
        "max_price_diff": str(max(diffs)) if diffs else None,
        "only_in_first": len(first.events) - len(matched),
        "only_in_second": len(extra),
        "second_extra_range": [extra[0].isoformat(), extra[-1].isoformat()]
        if extra
        else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-csv", required=True, type=Path)
    parser.add_argument("--schwab-catalog", required=True, type=Path)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--symbol", default="SPXL")
    parser.add_argument("--max-lag-days", type=int, default=7)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    series = parse_trade_history_csv(args.signals_csv.read_bytes())
    catalog = json.loads(args.schwab_catalog.read_text(encoding="utf-8"))
    fills, final_quantity = load_fills(catalog, args.account_id, args.symbol)
    report = crosscheck(series, fills, final_quantity, max_lag_days=args.max_lag_days)
    print(json.dumps(report, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

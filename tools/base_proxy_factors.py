"""Base proxy factor file from a raw Yahoo capture: total returns, spliced risk-free.

Factor-model audit D2/D3.  The earlier construction took the price return of the
cash ETF BIL as the risk-free rate and the price return of SPY/IWM/HYG minus it as
the proxy factors.  A price return drops on every distribution (BIL about -0.3%
a month, HYG about -0.4%) and is quantised by the one-cent price tick (1.094e-4),
so the risk-free column was negative on some days and the proxies omitted dividends.

Here every return is a total return from the raw capture, ``(close + dividend) /
previous close - 1`` on the ex-date; the risk-free rate is the dated Ken French RF
through its last date and the BIL total return after it, with an overlap check on
the last trading days both cover.  A capture whose latest monthly distribution is
not recorded yet is refused: its last ex-date would show as a loss.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any, Final, Mapping, Sequence

PROXIES: Final = {"SPY_XS": "SPY", "IWM_XS": "IWM", "HYG_XS": "HYG"}
CASH: Final = "BIL"
MONTHLY_PAYERS: Final = ("BIL", "HYG")
OVERLAP_DAYS: Final = 60
OVERLAP_TOLERANCE: Final = Decimal("0.00003")
RISK_FREE_DAILY_MIN: Final = Decimal("-0.0005")
RISK_FREE_DAILY_MAX: Final = Decimal("0.005")
# monthly payers go ex in the first days of the month
MONTHLY_EX_DATE_LAST_DAY: Final = 5


class BaseProxyError(ValueError):
    """The capture cannot produce a trustworthy proxy factor file."""


def _day(text: str) -> date:
    return date.fromisoformat(str(text)[:10])


def _series(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[date, tuple[Decimal, Decimal]]]:
    series: dict[str, dict[date, tuple[Decimal, Decimal]]] = {}
    for row in rows:
        ticker, raw_day, close = row.get("Ticker"), row.get("Date"), row.get("Close")
        if not ticker or not raw_day or close in (None, ""):
            continue
        dividend = row.get("Dividends")
        series.setdefault(str(ticker), {})[_day(str(raw_day))] = (
            Decimal(str(close)),
            Decimal(str(dividend)) if dividend not in (None, "") else Decimal(0),
        )
    return series


def total_returns(
    prices: Mapping[date, tuple[Decimal, Decimal]],
) -> dict[date, Decimal]:
    """``(close + dividend) / previous close - 1`` on each observed date."""

    days = sorted(prices)
    out: dict[date, Decimal] = {}
    for previous, current in zip(days, days[1:]):
        before = prices[previous][0]
        if before == 0:
            raise BaseProxyError(f"zero close on {previous.isoformat()}")
        close, dividend = prices[current]
        out[current] = (close + dividend) / before - Decimal(1)
    return out


def _check_dividend_record(
    ticker: str, prices: Mapping[date, tuple[Decimal, Decimal]]
) -> None:
    """A monthly payer's latest ex-date must be recorded once the capture is past it.

    Monthly ETFs go ex in the first days of the month: a capture dated after the
    5th must show a distribution in its own month, an earlier one in the month before.
    """

    last_price = max(prices)
    month_start = last_price.replace(day=1)
    needed_from = (
        month_start
        if last_price.day > MONTHLY_EX_DATE_LAST_DAY
        else (month_start - timedelta(days=1)).replace(day=1)
    )
    paid = [day for day, (_close, dividend) in prices.items() if dividend > 0]
    if not paid or max(paid) < needed_from:
        raise BaseProxyError(
            f"{ticker}: no distribution recorded since {needed_from.isoformat()} "
            f"(price data to {last_price.isoformat()}, last distribution "
            f"{max(paid).isoformat() if paid else 'none'}): the provider has not "
            "posted the latest monthly distribution, so its ex-date would show as "
            "a loss; capture again once it is recorded"
        )


def build_factors(
    raw_rows: Sequence[Mapping[str, Any]],
    french_rf: Mapping[date, Decimal],
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Rows ``date, RF, SPY_XS, IWM_XS, HYG_XS`` and a provenance receipt."""

    series = _series(raw_rows)
    missing = sorted({CASH, *PROXIES.values()} - set(series))
    if missing:
        raise BaseProxyError(f"capture lacks tickers: {missing}")
    for ticker in MONTHLY_PAYERS:
        _check_dividend_record(ticker, series[ticker])
    with localcontext() as context:
        context.prec = 60
        returns = {ticker: total_returns(series[ticker]) for ticker in series}
        cash = returns[CASH]
        calendar = sorted(cash)
        french_last = max(french_rf) if french_rf else None
        both = [
            day
            for day in calendar
            if day in french_rf and day in cash and french_last is not None
        ][-OVERLAP_DAYS:]
        if len(both) < OVERLAP_DAYS:
            raise BaseProxyError(
                f"risk-free overlap has {len(both)} days, needs {OVERLAP_DAYS}"
            )
        mean_gap = sum((cash[day] - french_rf[day] for day in both), Decimal(0)) / len(
            both
        )
        if abs(mean_gap) > OVERLAP_TOLERANCE:
            raise BaseProxyError(
                f"BIL total return differs from French RF by a mean {mean_gap} a day "
                f"over the last {OVERLAP_DAYS} shared days"
            )
        rows: list[dict[str, str]] = []
        for day in calendar:
            if any(day not in returns[ticker] for ticker in PROXIES.values()):
                continue
            rf = french_rf[day] if day in french_rf else cash[day]
            if not RISK_FREE_DAILY_MIN <= rf <= RISK_FREE_DAILY_MAX:
                raise BaseProxyError(
                    f"risk-free {rf} on {day.isoformat()} is outside "
                    f"[{RISK_FREE_DAILY_MIN}, {RISK_FREE_DAILY_MAX}]"
                )
            row = {"date": day.isoformat(), "RF": format(rf, "f")}
            for column, ticker in PROXIES.items():
                row[column] = format(returns[ticker][day] - rf, "f")
            rows.append(row)
    receipt: dict[str, object] = {
        "return_basis": "TOTAL_RETURN_CLOSE_PLUS_DIVIDEND_OVER_PREVIOUS_CLOSE",
        "risk_free_source": "FRENCH_RF_THROUGH_LAST_DATE_THEN_BIL_TOTAL_RETURN",
        "risk_free_splice_after": None
        if french_last is None
        else french_last.isoformat(),
        "overlap_days": len(both),
        "overlap_first": both[0].isoformat(),
        "overlap_last": both[-1].isoformat(),
        "overlap_mean_gap": format(mean_gap, "f"),
        "row_count": len(rows),
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
    }
    return rows, receipt


def french_rf_from_text(text: str) -> dict[date, Decimal]:
    """Daily RF (percent in the file, decimal here) from a Ken French 5-factor CSV body."""

    rates: dict[date, Decimal] = {}
    column: int | None = None
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if parts and parts[0] == "" and "RF" in parts:
            column = parts.index("RF")
            continue
        if column is not None and len(parts) > column and len(parts[0]) == 8:
            if not parts[0].isdigit():
                continue
            day = date(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:]))
            rates[day] = Decimal(parts[column]) / Decimal(100)
    if not rates:
        raise BaseProxyError("no RF column found in the French file")
    return rates


def main(argv: Sequence[str] | None = None) -> int:
    """Write ``factors.csv`` (LF bytes) and a provenance receipt from pinned inputs."""

    import argparse
    import hashlib
    import json
    from pathlib import Path
    import zipfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True, help="raw Yahoo capture")
    parser.add_argument(
        "--french-zip",
        type=Path,
        required=True,
        help="French 5-factor daily source zip",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.capture.read_bytes())
    rows = payload["rows"] if isinstance(payload, dict) else payload
    with zipfile.ZipFile(args.french_zip) as archive:
        text = archive.read(archive.namelist()[0]).decode("latin-1")
    built, receipt = build_factors(rows, french_rf_from_text(text))
    columns = ("date", "RF", *PROXIES)
    body = "".join(",".join(row[column] for column in columns) + "\n" for row in built)
    output = (",".join(columns) + "\n" + body).encode("utf-8")
    args.out.write_bytes(output)
    receipt.update(
        capture_sha256=hashlib.sha256(args.capture.read_bytes()).hexdigest(),
        french_source_sha256=hashlib.sha256(args.french_zip.read_bytes()).hexdigest(),
        factors_sha256=hashlib.sha256(output).hexdigest(),
    )
    args.receipt.write_bytes(
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

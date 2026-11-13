"""Record daily closes from the Yahoo chart API twice and note non-trading weekdays.

Records only. Whether Yahoo closes are admitted as governed data needs an owner
ruling; this tool does not decide it. The network fetch and the sleep are
injectable so the normal suite never touches the network.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any
import urllib.request

URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={p1}&period2={p2}&interval=1d&events=div%7Csplit"
    "&includeAdjustedClose=true"
)
PAUSE_SECONDS = 3.0
SYMBOL = re.compile(r"^[A-Z0-9.^=-]{1,15}$")


class CaptureError(ValueError):
    """The pull is unusable or the run directory is not fresh."""


Fetch = Callable[[str], bytes]


def http_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: bytes = resp.read()
    return data


def _epoch(day: dt.date) -> int:
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    return int(midnight.timestamp())


def pull(
    symbols: list[str],
    start: str,
    end: str,
    fetch: Fetch,
    raw_out: dict[str, bytes] | None = None,
) -> list[tuple[str, str, str]]:
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if d1 < d0:
        raise CaptureError("end precedes start")
    p1 = _epoch(d0 - dt.timedelta(days=7))
    p2 = _epoch(d1 + dt.timedelta(days=3))
    rows: list[tuple[str, str, str]] = []
    for symbol in symbols:
        try:
            raw = fetch(URL.format(symbol=symbol, p1=p1, p2=p2))
            if raw_out is not None:
                raw_out[symbol] = raw
            res = json.loads(raw)["chart"]["result"][0]
            stamps = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CaptureError(f"no usable chart for {symbol}") from exc
        if len(stamps) != len(closes):
            raise CaptureError(f"{symbol} timestamps and closes differ in length")
        got = 0
        seen: set[str] = set()
        for ts, close in zip(stamps, closes):
            day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
            if start <= day <= end and close is not None:
                if not isinstance(close, (int, float)) or not math.isfinite(close):
                    raise CaptureError(f"{symbol} has a non-finite close on {day}")
                if day in seen:
                    raise CaptureError(f"{symbol} has two bars on {day}")
                seen.add(day)
                rows.append((symbol, day, f"{close:.4f}"))
                got += 1
        if not got:
            raise CaptureError(f"{symbol} has no bars in {start}..{end}")
    return sorted(rows)


def non_trading_weekdays(
    rows: list[tuple[str, str, str]], start: str, end: str
) -> list[str]:
    have = {day for _, day, _ in rows}
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    return [
        d.isoformat() for d in days if d.weekday() < 5 and d.isoformat() not in have
    ]


def symbol_gaps(
    rows: list[tuple[str, str, str]], symbols: list[str]
) -> list[tuple[str, str]]:
    """Weekdays on which some symbol has a bar and another does not."""

    by_day: dict[str, set[str]] = {}
    for s, d, _ in rows:
        by_day.setdefault(d, set()).add(s)
    return sorted(
        (s, d) for d, have in by_day.items() for s in symbols if s not in have
    )


def _csv(rows: list[tuple[str, str, str]]) -> bytes:
    body = "".join(f"{s},{d},{c}\n" for s, d, c in rows)
    return ("symbol,date,close\n" + body).encode("utf-8")


def capture(
    symbols: list[str],
    start: str,
    end: str,
    out_dir: Path,
    fetch: Fetch = http_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Write both pulls and the weekday list; return True when the pulls are equal."""

    if out_dir.exists():
        raise CaptureError("out-dir already exists")
    if not symbols or len(set(symbols)) != len(symbols):
        raise CaptureError("symbols must be a non-empty list without duplicates")
    if not all(SYMBOL.match(s) for s in symbols):
        raise CaptureError("symbol has characters outside A-Z 0-9 . ^ = -")
    raw_a: dict[str, bytes] = {}
    raw_b: dict[str, bytes] = {}
    a = pull(symbols, start, end, fetch, raw_a)
    sleep(PAUSE_SECONDS)
    b = pull(symbols, start, end, fetch, raw_b)
    out_dir.mkdir(parents=True)
    (out_dir / "prices-a.csv").write_bytes(_csv(a))
    (out_dir / "prices-b.csv").write_bytes(_csv(b))
    text = "".join(d + "\n" for d in non_trading_weekdays(a, start, end))
    (out_dir / "non_trading_weekdays.txt").write_bytes(text.encode("utf-8"))
    gaps = symbol_gaps(a, symbols)
    gap_text = "".join(f"{s},{d}\n" for s, d in gaps)
    (out_dir / "symbol_gaps.csv").write_bytes(("symbol,date\n" + gap_text).encode())
    sums = []
    for tag, raws in (("a", raw_a), ("b", raw_b)):
        for sym in sorted(raws):
            name = f"raw-{tag}-{sym}.json"
            (out_dir / name).write_bytes(raws[sym])
            sums.append(f"{hashlib.sha256(raws[sym]).hexdigest()}  {name}\n")
    (out_dir / "SHA256SUMS.txt").write_bytes("".join(sums).encode())
    return a == b and not gaps


def main(argv: list[str] | None = None, **kw: Any) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        same = capture(
            args.symbols.split(","), args.start, args.end, args.out_dir, **kw
        )
    except (CaptureError, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if not same:
        print("flagged: the two pulls differ", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

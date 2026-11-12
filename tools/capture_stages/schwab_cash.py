"""Schwab daily brokerage-cash stage (#913 source ``schwab_daily_cash``).

Cash is not a published series: anchor a live Balances-page cash value and walk it backwards through the signed
transaction amounts. Trades (Buy/Sell) move cash on their SETTLEMENT date, everything else (transfers, journals,
income, fees) on its event date: month-end statements agree with exactly that basis (verified on seven accounts, every
month end of one year, to the cent). Pure functions over plain rows; no I/O, no values logged.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
import re
from typing import Any

TRADE_KINDS = ("buy", "sell")


def walk_cash(
    nav_dates: Iterable[str],
    transactions: Sequence[Mapping[str, Any]],
    *,
    anchor_date: str,
    anchor_cash: float,
) -> dict[str, float]:
    """Cash at the end of EVERY calendar day from the first NAV day to the anchor, walked back from ``anchor_cash``.

    ``transactions``: dicts with ``kind``, ``event_date``, ``settle_date`` (required for Buy/Sell) and a signed
    ``amount`` (None, e.g. an in-kind journal, moves no cash and is skipped). Refused (``ValueError``, stable code):
    a flow after the anchor (``FLOW_AFTER_ANCHOR``: it cannot be undone), an anchor before the last NAV day
    (``ANCHOR_BEFORE_NAV``: days would be dropped silently) and a trade with no settlement date
    (``MISSING_SETTLE_DATE``). Calendar days are returned so a weekend month-end statement still has a row.
    """
    anchor = date.fromisoformat(anchor_date)
    days = sorted(date.fromisoformat(d) for d in nav_dates)
    if not days:
        return {}
    if anchor < days[-1]:
        raise ValueError("ANCHOR_BEFORE_NAV")
    flows: dict[date, float] = defaultdict(float)
    for t in transactions:
        if t.get("amount") is None:
            continue
        if str(t["kind"]).casefold() in TRADE_KINDS:
            if not t.get("settle_date"):
                raise ValueError("MISSING_SETTLE_DATE")
            when = t["settle_date"]
        else:
            when = t["event_date"]
        d = date.fromisoformat(when)
        if d > anchor:
            raise ValueError("FLOW_AFTER_ANCHOR")
        flows[d] += float(t["amount"])
    out: dict[str, float] = {}
    cash, d = float(anchor_cash), anchor
    while d >= days[0]:
        out[d.isoformat()] = round(cash, 2)
        cash -= flows.get(
            d, 0.0
        )  # cash at the end of d-1 = cash at the end of d minus the flows on d
        d -= timedelta(days=1)
    return dict(sorted(out.items()))


_AMT = r"(\(?\$?-?[\d,]+\.\d\d\)?)"
_TOTAL = re.compile(r"Total Cash and Cash Investments\s+" + _AMT + r"\s+" + _AMT)
_BOX = re.compile(r"Cash and Cash Investments\s+(-?[\d,]+\.\d\d)")
_DEBIT = re.compile(r"Settled Debit Balance\s+\(([\d,]+\.\d\d)\)")
_SWEEP = re.compile(
    r"Bank Sweep\s+CHARLES SCHWAB\s+\$?([\d,]+\.\d\d)\s+\$?([\d,]+\.\d\d)"
)


def _num(s: str) -> float:
    neg = s.startswith("(") and s.endswith(")")
    v = float(s.strip("()").replace("$", "").replace(",", ""))
    return -v if neg else v


def parse_statement_cash(text: str) -> float | None:
    """Ending settled cash from a statement (pdftotext -layout text), or None. Order, validated against twelve month ends
    of seven accounts (every strategy-window month end equals the walk to the cent):

    1. ``Settled Debit Balance (x)`` -> ``-x`` (an overdrawn account);
    2. the detail line ``Total Cash and Cash Investments <begin> <end>`` -> ``end``, unless it is zero while a bank-sweep
       row exists (then the sweep's ending balance is used);
    3. the ``Bank Sweep ... <begin> <end>`` row -> ``end``;
    4. last resort: the asset-allocation line ``Cash and Cash Investments <x> <pct>%`` -> ``x``. From mid-2026 that box also
       counts T-bill ETFs, so this value is only ever compared, never trusted: the statement-cash check against the walk
       fails loudly when it is wrong.

    Several accounts' totals in one text are ambiguous and return None (refused, not guessed).
    """
    if m := _DEBIT.search(text):
        return -_num(m.group(1))
    totals = _TOTAL.findall(text)
    if len(totals) > 1:
        return None
    sweep = _SWEEP.search(text)
    if totals and not (_num(totals[0][0]) == 0 and sweep):
        return _num(totals[0][1])
    if sweep:
        return _num(sweep.group(2))
    if m := _BOX.search(text):
        return _num(m.group(1))
    return None

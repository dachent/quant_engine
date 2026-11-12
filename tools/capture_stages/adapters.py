"""Per-source verify adapters (#913 S3): turn a capture directory into the closure-check inputs the declared checks need.

Each adapter returns ``(as_of, checks)`` where ``checks`` maps a declared check name to a zero-argument callable that reads the
CAPTURED FILES (not caller-supplied numbers) and returns a ``CheckResult``. ``as_of`` is derived from the data. Parameters a
source declares in the contract (``check_params``) are passed through. Parsers raise ``ValueError``; ``verify`` records that as
``CHECK_ERROR`` for the one check, so a broken file fails loudly without masking the others.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any

try:
    import capture_pipeline as cp
    import schwab_cash as sc
    import statement_browser as sb
except ImportError:  # pragma: no cover - imported as tools.capture_stages.adapters
    from tools import capture_pipeline as cp
    from tools.capture_stages import schwab_cash as sc
    from tools.capture_stages import statement_browser as sb

Checks = dict[str, Callable[[], "cp.CheckResult"]]


def _quarter_ends(first: date, last: date) -> list[str]:
    out, y, m = [], first.year, ((first.month - 1) // 3 + 1) * 3
    while True:
        d = (
            (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1))
            if m < 12
            else date(y, 12, 31)
        )
        if d > last:
            return out
        out.append(d.isoformat())
        y, m = (y + 1, 3) if m == 12 else (y, m + 3)


def _month_ends(first: date, last: date) -> list[str]:
    out, y, m = [], first.year, first.month
    while True:
        d = date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)
        if d > last:
            return out
        out.append(d.isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def john_hancock(capture_dir: Path, params: Mapping[str, Any]) -> tuple[str, Checks]:
    """Quarterly statements: identity per quarter (chain per ``check_params``, default off because of published boundary
    restatements, which are reported by ``jh_boundary_gaps``) and every quarter end between the first and last statement
    present. ``as_of`` is the last statement's end."""
    quarters = [
        sb.parse_jh_statement(p.read_text(encoding="utf-8", errors="replace"))
        for p in sorted(Path(capture_dir).glob("stmt-*.txt"))
    ]
    if not quarters:
        raise ValueError("NO_STATEMENTS")
    ends = sorted(q["end"] for q in quarters)
    first, last = (
        date.fromisoformat(min(q["start"] for q in quarters)),
        date.fromisoformat(ends[-1]),
    )
    chain = bool(params.get("nav_identity", {}).get("chain", False))
    return ends[-1], {
        "nav_identity": lambda: cp.check_nav_identity(
            sb.jh_identity_rows(quarters), chain=chain
        ),
        "expected_dates": lambda: cp.check_expected_dates(
            ends, _quarter_ends(first, last)
        ),
    }


def composer(capture_dir: Path, params: Mapping[str, Any]) -> tuple[str, Checks]:
    """Monthly statements: holdings (incl. cash) sum to the total market value in every statement (per-line rounding allowance
    from ``check_params``) and every month end between the first and last statement is present."""
    tol = params.get("sum_equals_total", {}).get("tol_per_part", 0)
    rows: list[tuple[str, dict[str, Any]]] = []
    for p in sorted(Path(capture_dir).glob("stmt-????-??.txt")):
        month = re.fullmatch(r"stmt-(\d{4})-(\d{2})\.txt", p.name)
        assert month is not None
        y, m = int(month.group(1)), int(month.group(2))
        end = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()
        rows.append(
            (
                end,
                sb.parse_composer_statement(
                    p.read_text(encoding="utf-8", errors="replace")
                ),
            )
        )
    if not rows:
        raise ValueError("NO_STATEMENTS")

    def sums() -> cp.CheckResult:
        bad = [
            r
            for _e, r in rows
            if not cp.check_sum_equals_total(
                r["holdings_values"], r["total_market_value"], tol_per_part=tol
            ).ok
        ]
        return cp.CheckResult(
            "sum_equals_total", not bad, "OK" if not bad else "TOTAL_MISMATCH", len(bad)
        )

    ends = [e for e, _r in rows]
    first = date.fromisoformat(ends[0]).replace(day=1)
    return ends[-1], {
        "sum_equals_total": sums,
        "expected_dates": lambda: cp.check_expected_dates(
            ends, _month_ends(first, date.fromisoformat(ends[-1]))
        ),
    }


_ROW = re.compile(r"(\d\d)/(\d\d)/(\d{4})\s")


def schwab_history_rows(text: str, *, since: str) -> list[dict[str, Any]]:
    """Rows of a live Transaction History page text dated on or after ``since``: ``date``, ``type``, signed ``amount`` (the page's
    last dollar column). Dated blocks only; a block with no amount has ``amount`` None."""
    rows = []
    for block in re.split(r"\n(?=\d\d/\d\d/\d{4}\s)", text):
        m = _ROW.match(block)
        if not m:
            continue
        day = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        if day < since:
            continue
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        amts = re.findall(r"-?\$[\d,]+\.\d\d", block.split("\nTotal")[0])
        rows.append(
            {
                "date": day,
                "type": lines[1] if len(lines) > 1 else "",
                "amount": float(amts[-1].replace("$", "").replace(",", ""))
                if amts
                else None,
            }
        )
    return rows


def schwab_cash(
    capture_dir: Path,
    *,
    suffix: str,
    nav_by_date: Mapping[str, float],
    ledger_transactions: Sequence[Mapping[str, Any]],
    anchor_date: str,
    anchor_cash: float,
    ledger_end: str,
    statement_cash_by_date: Mapping[str, float],
    allowed_negative: float = 0.0,
) -> tuple[str, Checks]:
    """Settle-basis cash walk for one account from the ledger's transactions plus the captured post-ledger history rows (trade
    date only; they sit after the ledger end and before the anchor), checked against its bounds and against month-end statement
    cash parsed from the capture. ``as_of`` is the anchor date."""
    page = Path(capture_dir) / f"history-{suffix}.txt"
    post = [
        {
            "kind": r["type"] if r["type"] in ("Buy", "Sell") else "Other",
            "event_date": r["date"],
            "settle_date": r["date"],
            "amount": r["amount"],
        }
        for r in schwab_history_rows(
            page.read_text(encoding="utf-8", errors="replace"), since=ledger_end
        )
        if r["date"] > ledger_end
    ]
    walk = sc.walk_cash(
        list(nav_by_date),
        [*ledger_transactions, *post],
        anchor_date=anchor_date,
        anchor_cash=anchor_cash,
    )
    return anchor_date, {
        "cash_walk": lambda: cp.check_cash_walk(
            [
                {"cash": walk[d], "nav": nav}
                for d, nav in nav_by_date.items()
                if d in walk
            ],
            allowed_negative=allowed_negative,
        ),
        "statement_cash": lambda: cp.check_statement_cash(
            walk, dict(statement_cash_by_date)
        ),
    }


def ibkr_flex(
    series: Mapping[str, Mapping[str, Any]],
    *,
    start: str,
    end: str,
    non_trading: Iterable[str] = (),
) -> tuple[str, Checks]:
    """From ``ibkr_equity_history.extract`` output for one account: every expected weekday present, the equity-summary total equal
    to the ChangeInNAV ending value on every day that has one (restatement conflicts count as failures), and the bridge check
    only where a gap exists (none declared here: a hole is reported by coverage and needs a receipt capture)."""
    try:
        from ibkr_equity_history import nav_vs_change_in_nav  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        from tools.capture_stages.ibkr_equity_history import nav_vs_change_in_nav

    days = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in series]

    def totals() -> cp.CheckResult:
        r = nav_vs_change_in_nav(dict(series))
        conflicts = sum(1 for v in series.values() if v.get("conflict"))
        bad = len(r["mismatch"]) + conflicts
        return cp.CheckResult(
            "sum_equals_total", bad == 0, "OK" if bad == 0 else "TOTAL_MISMATCH", bad
        )

    return end, {
        "coverage": lambda: cp.check_coverage(
            days, start, end, non_trading=non_trading
        ),
        "sum_equals_total": totals,
        "bridge_receipt": lambda: cp.not_applicable("bridge_receipt"),
    }

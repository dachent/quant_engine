"""Producer for the control-panel Performance tab (``performance_tab`` block).

One additive, optional top-level panel block with its own ``schema_version``.  It
carries the data the Performance tab draws, in compact array form so the payload
grows by well under 0.2 MB:

``modes``
    Per evidence mode (``LIVE_ONLY`` and ``LIVE_PLUS_BACKTEST``) a weekly grid over
    the common window and the lines drawn on it: one total-return index per
    strategy (1.0 immediately before the first common week), the equal-weight
    portfolio, and per point origin runs (``LIVE`` / ``BACKTEST`` / ``MIXED``)
    derived from the admitted mode coverage.  The window start is stated with the
    series that sets it.

``benchmarks``
    SPY and SPXL are not admitted yet.  They are explicit ``UNAVAILABLE`` entries
    naming the required pull; nothing is fabricated and no other ETF is
    substituted.

``slices``
    Dollar value of each strategy slice per calendar day, whole cents, summed from
    the admitted epoch observation traces (``ending_nav``), reserve excluded, with
    the carry-forward rule and the residual against the systematic capital.

``readout``
    The governed HWM index is the headline.  The summed-epoch figure and the
    difference come from ``portfolio_twr_cumulative`` when that block (issue 593)
    is present and are null with a named reason otherwise.

``verify_performance_tab`` re-derives every figure with separate code (``Fraction``
arithmetic, per-date lookups) and raises on any difference.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Final, Mapping, Sequence

PERFORMANCE_TAB_SCHEMA_VERSION: Final = 2
SCOPE: Final = "SYSTEMATIC_EX_RESERVE"
MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
TRI_DECIMALS: Final = 8

_PRECISION: Final = 50
_WHOLE: Final = Decimal(1)
_TRI_QUANTUM: Final = Decimal(1).scaleb(-TRI_DECIMALS)

EW_SERIES_ID: Final = "portfolio:equal_weight"
EW_DEFINITION: Final = (
    "Equal-weight portfolio of every strategy line in this mode: each week's "
    "return is the arithmetic mean of the strategy weekly total returns "
    "(1/N each, rebalanced to equal weights at the start of every week), compounded "
    "from 1.0 immediately before the first common week.  Daily returns are not in "
    "the panel, so the rebalance is weekly.  A week missing for any strategy makes "
    "the mode UNAVAILABLE; it is never filled with zero.  In LIVE_PLUS_BACKTEST a "
    "strategy with no admitted backtest history contributes its live weeks and the "
    "line is labelled by origin (MIXED where components differ)."
)
STRATEGY_DEFINITION: Final = (
    "Weekly total return of the strategy compounded from 1.0 immediately before "
    "the first common week; the value at index i is the index at the end of week "
    "dates[i]."
)
SLICE_CARRY_FORWARD: Final = (
    "Each epoch contributes its observation-trace ending_nav on every calendar day "
    "from its first link start (its base_nav) to its last link end, carrying the "
    "prior ending NAV through any day without a link.  The opening-day base_nav is "
    "not counted when another epoch of the same account ends on that day (the "
    "predecessor's closing NAV already carries it).  A closed epoch contributes 0 "
    "after its last link end_date; cash left in the account after liquidation is not "
    "in any slice and shows in the residual."
)
SLICE_FLOW_DEFINITION: Final = (
    "net_flow of the epoch's observation links recognised on the date, in cents. "
    "Includes transfers between strategies of one account: the trace does not "
    "separate external from internal flows."
)

REQUIRED_PULL: Final[dict[str, Any]] = {
    "source_id": "yahoo-adjusted-etf-daily",
    "series": (
        "daily adjusted close with dividends and distributions reinvested, or "
        "daily price plus the distribution history"
    ),
    "existing_files": (
        "price-only daily CSVs exist on disk (SPY to 2026-01-22, SPXL to 2025-12-19): "
        "not admitted, no adjusted close or distributions, and they stop before the "
        "cutoff, so they are not a total-return series and are not used"
    ),
    "to_date": "2026-09-17",
    "minimum_window": ["2024-10-01", "2026-09-17"],
    "admission": "hash-bound input plus a place in CORE_BENCHMARKS and the plan manifest",
    "approval": "the owner's direct approval of the pull",
}
_BENCHMARKS: Final = (
    ("market:SPY", "SPY", "2007-01-03", "SPY"),
    ("market:SPXL", "SPXL", "2008-11-05", "SPXL (its own total return, not 3x SPY)"),
)


BENCHMARK_DEFINITION: Final = (
    "Weekly total return of the benchmark ETF from the governed weekly returns "
    "(adjusted-close total return from the admitted yahoo-adjusted-etf-daily source), "
    "compounded from 1.0 immediately before the first common week.  It has no "
    "live or backtest origin."
)


def _benchmark_row(
    weekly: Mapping[str, Any], series_id: str
) -> Mapping[str, Any] | None:
    for row in _sequence(weekly.get("series")):
        if isinstance(row, Mapping) and row.get("series_id") == series_id:
            return row
    return None


class PerformanceTabError(ValueError):
    """The performance tab cannot be built or does not verify."""


# ---------------------------------------------------------------- helpers


def _day(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PerformanceTabError(f"{label} is not an ISO date") from exc


def _dec(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise PerformanceTabError(f"{label} is not a decimal") from exc
    if not number.is_finite():
        raise PerformanceTabError(f"{label} is not finite")
    return number


def _cents(value: Decimal) -> int:
    return int((value * 100).quantize(_WHOLE))


def _text(value: Decimal) -> str:
    return format(value, "f")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "UNAVAILABLE", "reason": reason}


def _runs(flags: Sequence[str]) -> list[list[Any]]:
    runs: list[list[Any]] = []
    for index, flag in enumerate(flags):
        if runs and runs[-1][2] == flag:
            runs[-1][1] = index
        else:
            runs.append([index, index, flag])
    return runs


def _combined_flag(flags: Sequence[str]) -> str:
    unique = set(flags)
    return next(iter(unique)) if len(unique) == 1 else "MIXED"


# ---------------------------------------------------------------- weekly lines


def _strategy_rows(weekly: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = [
        row
        for row in _sequence(weekly.get("series"))
        if isinstance(row, Mapping)
        and row.get("primary_group") == "STRATEGY"
        and row.get("return_basis") == "STRATEGY_TOTAL_RETURN"
    ]
    return sorted(rows, key=lambda row: str(row.get("series_id")))


def _live_start(row: Mapping[str, Any]) -> date | None:
    """First day of the first live observation, from the live-only coverage."""

    coverage = _mapping(_mapping(row.get("mode_coverage")).get("LIVE_ONLY"))
    first_week = coverage.get("first_week")
    meta = _mapping(_mapping(row.get("observation_meta_by_mode")).get("LIVE_ONLY"))
    entry = meta.get(first_week) if isinstance(first_week, str) else None
    if isinstance(entry, list) and len(entry) > 2:
        return _day(entry[1], "live observation start")
    return None


def _mode_inputs(
    row: Mapping[str, Any], mode: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], str] | None:
    """Weekly values, per-week meta and the source label used for this mode."""

    by_mode = _mapping(row.get("values_by_mode"))
    meta_by_mode = _mapping(row.get("observation_meta_by_mode"))
    values = _mapping(by_mode.get(mode))
    if values:
        return values, _mapping(meta_by_mode.get(mode)), mode
    if mode == "LIVE_PLUS_BACKTEST":
        live = _mapping(by_mode.get("LIVE_ONLY"))
        if live:
            return live, _mapping(meta_by_mode.get("LIVE_ONLY")), "LIVE_ONLY_FALLBACK"
    return None


def _week_origin(meta_entry: Any, live_start: date | None, source: str) -> str:
    if source in ("LIVE_ONLY", "LIVE_ONLY_FALLBACK"):
        return "LIVE"
    if live_start is None:
        raise PerformanceTabError("live start of a hybrid series is not derivable")
    if not isinstance(meta_entry, list) or len(meta_entry) < 3:
        raise PerformanceTabError("observation meta is missing for an origin week")
    start = _day(meta_entry[1], "observation start")
    end = _day(meta_entry[2], "observation end")
    if end < live_start:
        return "BACKTEST"
    if start >= live_start:
        return "LIVE"
    return "MIXED"


def _mondays(first: date, last: date) -> list[date]:
    out: list[date] = []
    current = first
    while current <= last:
        out.append(current)
        current += timedelta(days=7)
    return out


def _build_mode(weekly: Mapping[str, Any], mode: str) -> dict[str, Any]:
    rows = _strategy_rows(weekly)
    if not rows:
        return _unavailable("NO_STRATEGY_WEEKLY_SERIES")
    per: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str]] = []
    for row in rows:
        picked = _mode_inputs(row, mode)
        if picked is None:
            return _unavailable(f"NO_WEEKLY_VALUES:{row.get('series_id')}")
        per.append((row, *picked))
    firsts = {str(row["series_id"]): min(values) for row, values, _m, _s in per}
    first_setter = max(sorted(firsts), key=lambda key: firsts[key])
    first_common = _day(firsts[first_setter], "first common observation")
    last = max(_day(max(values), "last week") for _r, values, _m, _s in per)
    # The common window starts where every series is observed in every week through
    # the last one: an absent week is unavailable, never zero, so a gap before it
    # ends a series' usable run.
    scan = _mondays(first_common, last)
    run_start: dict[str, date] = {}
    gaps: list[dict[str, Any]] = []
    for row, values, _m, _s in per:
        series_id = str(row["series_id"])
        missing = [week for week in scan if week.isoformat() not in values]
        if missing and missing[-1] == scan[-1]:
            return _unavailable(f"LAST_WEEK_MISSING:{series_id}")
        run_start[series_id] = missing[-1] + timedelta(days=7) if missing else scan[0]
        if missing:
            gaps.append(
                {
                    "series_id": series_id,
                    "missing_weeks": len(missing),
                    "first_missing_week": missing[0].isoformat(),
                    "last_missing_week": missing[-1].isoformat(),
                }
            )
    common_start = max(run_start.values())
    before_start = (common_start - timedelta(days=7)).isoformat()
    setter = next(
        str(row["series_id"])
        for row, values, _m, _s in sorted(
            per, key=lambda item: str(item[0]["series_id"])
        )
        if run_start[str(row["series_id"])] == common_start
        and before_start not in values
    )
    weeks = _mondays(common_start, last)
    week_keys = [week.isoformat() for week in weeks]
    matrix: list[list[Decimal]] = []
    flags: list[list[str]] = []
    with localcontext() as context:
        context.prec = _PRECISION
        for row, values, meta, source in per:
            live_start = _live_start(row)
            returns: list[Decimal] = []
            origin: list[str] = []
            for key in week_keys:
                if key not in values:
                    return _unavailable(f"MISSING_WEEK:{row.get('series_id')}:{key}")
                returns.append(_dec(values[key], f"{row.get('series_id')} {key}"))
                origin.append(_week_origin(meta.get(key), live_start, source))
            matrix.append(returns)
            flags.append(origin)
        ends: list[str] = []
        for index, key in enumerate(week_keys):
            ends.append(
                max(
                    _day(_sequence(meta.get(key))[2], "week end").isoformat()
                    for _r, _v, meta, _s in per
                    if len(_sequence(meta.get(key))) > 2
                )
                if any(len(_sequence(meta.get(key))) > 2 for _r, _v, meta, _s in per)
                else (weeks[index] + timedelta(days=6)).isoformat()
            )
        lines: list[dict[str, Any]] = []
        for (row, _values, _meta, source), returns, origin in zip(per, matrix, flags):
            lines.append(
                {
                    "series_id": row["series_id"],
                    "display_name": row.get("display_name"),
                    "kind": "STRATEGY",
                    "basis": "STRATEGY_TOTAL_RETURN",
                    "definition": STRATEGY_DEFINITION,
                    "source_mode": source,
                    "tri": _index_path(returns),
                    "origin": _runs(origin),
                }
            )
        count = len(per)
        ew_returns = [
            sum((column[i] for column in matrix), Decimal(0)) / count
            for i in range(len(week_keys))
        ]
        lines.append(
            {
                "series_id": EW_SERIES_ID,
                "display_name": "Equal-weight portfolio",
                "kind": "PORTFOLIO",
                "basis": "EQUAL_WEIGHT_WEEKLY_REBALANCED_TOTAL_RETURN",
                "definition": EW_DEFINITION,
                "source_mode": mode,
                "tri": _index_path(ew_returns),
                "origin": _runs(
                    [
                        _combined_flag([column[i] for column in flags])
                        for i in range(len(week_keys))
                    ]
                ),
            }
        )
    benchmark_gaps: list[dict[str, str]] = []
    for series_id, ticker, _since, _note in _BENCHMARKS:
        bench_source = _benchmark_row(weekly, series_id)
        if bench_source is None:
            continue
        values = _mapping(bench_source.get("values"))
        absent = next((key for key in week_keys if key not in values), None)
        if absent is not None:
            benchmark_gaps.append(
                {"series_id": series_id, "reason": f"MISSING_WEEK:{series_id}:{absent}"}
            )
            continue
        with localcontext() as context:
            context.prec = _PRECISION
            bench_returns = [
                _dec(values[key], f"{series_id} {key}") for key in week_keys
            ]
            lines.append(
                {
                    "series_id": series_id,
                    "display_name": bench_source.get("display_name"),
                    "kind": "BENCHMARK",
                    "basis": bench_source.get("return_basis"),
                    "definition": BENCHMARK_DEFINITION,
                    "source_mode": "WEEKLY_RETURNS_SINGLE_MODE",
                    "tri": _index_path(bench_returns),
                }
            )
    current = _mapping(weekly.get("current_week"))
    return {
        "status": "AVAILABLE",
        "grid": "ISO_WEEK_START_MONDAY",
        "base_point": "START_OF_FIRST_WEEK",
        "common_start_date": common_start.isoformat(),
        "common_start_set_by": setter,
        "first_observation_common_start": first_common.isoformat(),
        "first_observation_set_by": first_setter,
        "gaps_before_common_start": gaps,
        "dates": week_keys,
        "week_end_dates": ends,
        "last_week_partial": current.get("week_start") == week_keys[-1]
        and bool(current.get("is_partial")),
        "stale_before_current_week": bool(current.get("week_start"))
        and week_keys[-1] < str(current.get("week_start")),
        "data_through_date": ends[-1],
        "benchmarks_unavailable": benchmark_gaps,
        "lines": lines,
    }


def _index_path(returns: Sequence[Decimal]) -> list[float]:
    level = Decimal(1)
    out: list[float] = []
    for value in returns:
        level = level * (1 + value)
        out.append(float(level.quantize(_TRI_QUANTUM)))
    return out


# ---------------------------------------------------------------- slices


def _epoch_id(row: Mapping[str, Any]) -> str:
    return (
        f"{row.get('account_id')}|{row.get('strategy_id')}|{row.get('effective_start')}"
    )


def _build_slices(
    manifest: Mapping[str, Any] | None, capital: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        return _unavailable("ACCOUNT_MANIFEST_ABSENT")
    cutoff = _day(manifest.get("calculation_cutoff_date"), "manifest cutoff")
    epochs: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in _sequence(manifest.get("rows")):
        if not isinstance(row, Mapping):
            continue
        links = _sequence(_mapping(row.get("observation_trace")).get("links"))
        if row.get("status") != "OK" or not links:
            excluded.append(
                {"strategy_id": row.get("strategy_id"), "status": row.get("status")}
            )
            continue
        nav: dict[date, Decimal] = {}
        flow: dict[date, Decimal] = {}
        first = _day(links[0]["start_date"], "first link start")
        with localcontext() as context:
            context.prec = _PRECISION
            opening = _dec(links[0]["base_nav"], "opening base_nav")
            for link in links:
                end = _day(link["end_date"], "link end")
                nav[end] = _dec(link["ending_nav"], "ending_nav")
                flow[end] = flow.get(end, Decimal(0)) + _dec(
                    link["net_flow"], "net_flow"
                )
        epochs.append(
            {
                "id": _epoch_id(row),
                "account": row.get("account_id"),
                "strategy": str(row.get("strategy_id")),
                "first": first,
                "last": max(nav),
                "opening": opening,
                "nav": nav,
                "flow": flow,
            }
        )
    if not epochs:
        return _unavailable("NO_ADMITTED_EPOCH_OBSERVATION_TRACE")
    start = min(epoch["first"] for epoch in epochs)
    days = [start + timedelta(days=i) for i in range((cutoff - start).days + 1)]
    for epoch in epochs:
        epoch["skip_opening"] = any(
            other is not epoch
            and other["account"] == epoch["account"]
            and other["last"] == epoch["first"]
            for other in epochs
        )
    strategies = sorted({epoch["strategy"] for epoch in epochs})
    nav_rows: dict[str, list[int]] = {s: [0] * len(days) for s in strategies}
    flow_rows: dict[str, list[int]] = {s: [0] * len(days) for s in strategies}
    with localcontext() as context:
        context.prec = _PRECISION
        nav_exact: dict[str, list[Decimal]] = {
            s: [Decimal(0)] * len(days) for s in strategies
        }
        flow_exact: dict[str, list[Decimal]] = {
            s: [Decimal(0)] * len(days) for s in strategies
        }
        for epoch in epochs:
            carried: Decimal | None = None
            for index, day in enumerate(days):
                if day < epoch["first"] or day > epoch["last"]:
                    continue
                if day in epoch["nav"]:
                    carried = epoch["nav"][day]
                elif day == epoch["first"] and not epoch["skip_opening"]:
                    carried = epoch["opening"]
                elif day == epoch["first"]:
                    carried = None
                if carried is not None:
                    nav_exact[epoch["strategy"]][index] += carried
                if day in epoch["flow"]:
                    flow_exact[epoch["strategy"]][index] += epoch["flow"][day]
        for strategy in strategies:
            nav_rows[strategy] = [_cents(v) for v in nav_exact[strategy]]
            flow_rows[strategy] = [_cents(v) for v in flow_exact[strategy]]
    rows = []
    for strategy in strategies:
        first_day = next(
            (days[i].isoformat() for i, v in enumerate(nav_rows[strategy]) if v), None
        )
        rows.append(
            {
                "slice_id": strategy,
                "strategy_id": strategy,
                "parent_strategy_id": None,
                "sleeve_id": None,
                "first_nav_date": first_day,
                "epoch_ids": sorted(
                    e["id"] for e in epochs if e["strategy"] == strategy
                ),
                "nav_cents": nav_rows[strategy],
                "net_flow_cents": flow_rows[strategy],
            }
        )
    totals = [sum(nav_rows[s][i] for s in strategies) for i in range(len(days))]
    reference = _systematic_cents(capital)
    return {
        "status": "AVAILABLE",
        "scope": SCOPE,
        "unit": "USD_CENTS",
        "grid": "CALENDAR_DAY",
        "dates": [day.isoformat() for day in days],
        "carry_forward": SLICE_CARRY_FORWARD,
        "net_flow_definition": SLICE_FLOW_DEFINITION,
        "reserve": "EXCLUDED",
        "rows": rows,
        "excluded_epochs": excluded,
        "reconciliation": {
            "total_cents": totals,
            "reference_date": cutoff.isoformat(),
            "systematic_capital_cents": reference,
            "residual_cents": None if reference is None else reference - totals[-1],
            "residual_label": (
                "SYSTEMATIC_CAPITAL_MINUS_SLICE_TOTAL_AT_CUTOFF "
                "(cash left in a liquidated epoch; named by capital_reconciliation "
                "when that block is present)"
            ),
            "reason": None if reference is not None else "CAPITAL_SYSTEMATIC_ABSENT",
        },
    }


def _systematic_cents(capital: Mapping[str, Any]) -> int | None:
    value = capital.get("systematic")
    if value is None:
        return None
    with localcontext() as context:
        context.prec = _PRECISION
        return _cents(_dec(value, "capital.systematic"))


# ---------------------------------------------------------------- readout


def _build_readout(panel: Mapping[str, Any]) -> dict[str, Any]:
    proof = _mapping(panel.get("hwm_governed_proof"))
    path = _sequence(proof.get("index_path"))
    if proof.get("status") != "AVAILABLE" or len(path) < 2:
        headline: dict[str, Any] = _unavailable("NO_GOVERNED_HWM_INDEX_PATH")
    else:
        with localcontext() as context:
            context.prec = _PRECISION
            base = _dec(path[0]["index"], "index base")
            last = _dec(path[-1]["index"], "index last")
            headline = {
                "status": "AVAILABLE",
                "series": "hwm_governed_proof.index_path",
                "return_basis": proof.get("return_basis"),
                "financial_acceptance": proof.get("financial_acceptance"),
                "window_start": path[0]["as_of"],
                "window_end": path[-1]["as_of"],
                "base_index": _text(base),
                "current_index": _text(last),
                "cumulative_return": _text(last / base - 1),
            }
    totals = _mapping(panel.get("portfolio_twr_cumulative"))
    if not totals:
        summed: Any = None
        difference: Any = None
        reason: str | None = "PORTFOLIO_TWR_CUMULATIVE_ABSENT"
    else:
        block = _mapping(totals.get("summed_epoch_twr"))
        summed = dict(block) if block else None
        difference = totals.get("headline_minus_summed_epoch_twr")
        reason = None
        if summed is None or summed.get("status") != "AVAILABLE":
            reason = str(
                (summed or {}).get("reason")
                or totals.get("reason")
                or "SUMMED_EPOCH_TWR_UNAVAILABLE"
            )
            difference = None
    return {
        "headline": "HWM_GOVERNED_INDEX",
        "hwm_governed_index": headline,
        "summed_epoch_twr": summed,
        "headline_minus_summed_epoch_twr": difference,
        "summed_epoch_unavailable_reason": reason,
    }


# ---------------------------------------------------------------- builder


def _benchmark_entry(
    weekly: Mapping[str, Any], series_id: str, ticker: str, since: str, note: str
) -> dict[str, Any]:
    if _benchmark_row(weekly, series_id) is not None:
        return {
            "series_id": series_id,
            "ticker": ticker,
            "status": "AVAILABLE",
            "reason": None,
            "substitute": None,
            "source": "weekly_returns.series",
            "required_pull": None,
        }
    return {
        "series_id": series_id,
        "ticker": ticker,
        "status": "UNAVAILABLE",
        "reason": "NOT_ADMITTED_NO_GOVERNED_PULL",
        "substitute": None,
        "source": None,
        "required_pull": {**REQUIRED_PULL, "from_date": since, "note": note},
    }


def build_performance_tab(panel: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ``performance_tab`` block from the assembled panel mapping."""

    weekly = _mapping(panel.get("weekly_returns"))
    modes: dict[str, Any] = {}
    for mode in MODES:
        if not weekly.get("series"):
            modes[mode] = _unavailable("WEEKLY_RETURNS_ABSENT")
            continue
        try:
            modes[mode] = _build_mode(weekly, mode)
        except PerformanceTabError as exc:
            modes[mode] = _unavailable(str(exc))
    manifest = panel.get("account_manifest")
    try:
        slices = _build_slices(
            manifest if isinstance(manifest, Mapping) else None,
            _mapping(panel.get("capital")),
        )
    except PerformanceTabError as exc:
        slices = _unavailable(str(exc))
    return {
        "schema_version": PERFORMANCE_TAB_SCHEMA_VERSION,
        "as_of": weekly.get("as_of_date"),
        "scope": SCOPE,
        "modes": modes,
        "benchmarks": [
            _benchmark_entry(weekly, series_id, ticker, since, note)
            for series_id, ticker, since, note in _BENCHMARKS
        ],
        "slices": slices,
        "readout": _build_readout(panel),
    }


# ---------------------------------------------------------------- verifier
#
# Separate code from the builder above: exact ``Fraction`` arithmetic, a pointwise
# origin rule and per-date lookups instead of the builder's forward fill.


def _fail(message: str) -> PerformanceTabError:
    return PerformanceTabError(f"performance_tab: {message}")


def _expand(runs: Sequence[Any], length: int) -> list[str]:
    out: list[str | None] = [None] * length
    for run in runs:
        start, end, flag = run
        if not (0 <= start <= end < length):
            raise _fail("origin run outside the grid")
        for index in range(start, end + 1):
            if out[index] is not None:
                raise _fail("origin runs overlap")
            out[index] = flag
    if any(item is None for item in out):
        raise _fail("origin runs leave a point without an origin")
    return [str(item) for item in out]


def _origin_of(entry: Any, live_start: date | None, source: str) -> str:
    """Pointwise origin rule, written separately from the builder's."""

    if source != "LIVE_PLUS_BACKTEST":
        return "LIVE"
    if live_start is None or not isinstance(entry, list):
        raise _fail("hybrid origin inputs are missing")
    boundary = live_start.isoformat()
    if str(entry[2]) < boundary:
        return "BACKTEST"
    return "LIVE" if str(entry[1]) >= boundary else "MIXED"


def _verify_mode(
    block: Mapping[str, Any], weekly: Mapping[str, Any], mode: str
) -> None:
    rows = _strategy_rows(weekly)
    if block.get("status") == "UNAVAILABLE":
        if not block.get("reason"):
            raise _fail(f"{mode} unavailable without a reason")
        if rows:
            probe = None
            try:
                probe = _build_mode(weekly, mode)
            except PerformanceTabError as exc:
                probe = _unavailable(str(exc))
            if probe.get("status") != "UNAVAILABLE":
                raise _fail(f"{mode} is unavailable but the weekly series admit it")
        return
    dates = list(block["dates"])
    length = len(dates)
    value_sets: dict[str, set[str]] = {}
    for row in rows:
        picked = _mode_inputs(row, mode)
        if picked is None:
            raise _fail(f"{mode} is available but {row.get('series_id')} has no values")
        value_sets[str(row["series_id"])] = set(picked[0])
    last_week = max(max(keys) for keys in value_sets.values())
    earliest = max(min(keys) for keys in value_sets.values())
    if block["first_observation_common_start"] != earliest:
        raise _fail(f"{mode} first_observation_common_start differs")
    if min(value_sets[str(block["first_observation_set_by"])]) != earliest:
        raise _fail(f"{mode} first_observation_set_by differs")
    latest = ""
    candidate = date.fromisoformat(earliest)
    while candidate.isoformat() <= last_week:
        tail = _mondays(candidate, date.fromisoformat(last_week))
        if all(day.isoformat() in keys for keys in value_sets.values() for day in tail):
            latest = candidate.isoformat()
            break
        candidate += timedelta(days=7)
    if not latest:
        raise _fail(f"{mode} is available but no common window exists")
    if block["common_start_date"] != latest or dates[0] != latest:
        raise _fail(f"{mode} common_start_date differs from the common window")
    setter_keys = value_sets[str(block["common_start_set_by"])]
    before = (date.fromisoformat(latest) - timedelta(days=7)).isoformat()
    if before in setter_keys:
        raise _fail(f"{mode} common_start_set_by does not set the common start")
    expected_gaps = []
    for series_id in sorted(value_sets):
        absent = [
            day.isoformat()
            for day in _mondays(
                date.fromisoformat(earliest), date.fromisoformat(last_week)
            )
            if day.isoformat() not in value_sets[series_id]
        ]
        if absent:
            expected_gaps.append((series_id, len(absent), absent[0], absent[-1]))
    published_gaps = [
        (
            g["series_id"],
            g["missing_weeks"],
            g["first_missing_week"],
            g["last_missing_week"],
        )
        for g in block["gaps_before_common_start"]
    ]
    if sorted(published_gaps) != expected_gaps:
        raise _fail(f"{mode} gaps_before_common_start differ")
    for index, key in enumerate(dates):
        expected = date.fromisoformat(latest) + timedelta(days=7 * index)
        if key != expected.isoformat():
            raise _fail(f"{mode} dates are not consecutive Mondays")
    observed_ends = []
    for key in dates:
        candidates = []
        for row in rows:
            entry = (_mode_inputs(row, mode) or ({}, {}, ""))[1].get(key)
            if isinstance(entry, list) and len(entry) > 2:
                candidates.append(str(entry[2]))
        observed_ends.append(
            max(candidates)
            if candidates
            else (date.fromisoformat(key) + timedelta(days=6)).isoformat()
        )
    if list(block["week_end_dates"]) != observed_ends:
        raise _fail(f"{mode} week_end_dates differ from the observation meta")
    if block["data_through_date"] != observed_ends[-1]:
        raise _fail(f"{mode} data_through_date is not the final observation")
    current_week = _mapping(weekly.get("current_week"))
    week_start = current_week.get("week_start")
    if block["last_week_partial"] != (
        week_start == dates[-1] and bool(current_week.get("is_partial"))
    ):
        raise _fail(f"{mode} last_week_partial differs")
    if block["stale_before_current_week"] != (
        bool(week_start) and dates[-1] < str(week_start)
    ):
        raise _fail(f"{mode} stale_before_current_week differs")
    if len(block["week_end_dates"]) != length:
        raise _fail(f"{mode} week_end_dates length differs")
    by_id = {str(line["series_id"]): line for line in block["lines"]}
    if len(by_id) != len(block["lines"]) or EW_SERIES_ID not in by_id:
        raise _fail(f"{mode} lines are duplicated or lack the equal-weight line")
    benchmark_lines = {k: v for k, v in by_id.items() if v["kind"] == "BENCHMARK"}
    if len(by_id) - len(benchmark_lines) != len(rows) + 1:
        raise _fail(f"{mode} line count differs from the strategy count")
    for bench_id in ("market:SPY", "market:SPXL"):
        bench_row = next(
            (
                r
                for r in _sequence(weekly.get("series"))
                if isinstance(r, Mapping) and r.get("series_id") == bench_id
            ),
            None,
        )
        gap = [g for g in block["benchmarks_unavailable"] if g["series_id"] == bench_id]
        if bench_row is None:
            if bench_id in benchmark_lines or gap:
                raise _fail(f"{mode} {bench_id} is published without a source row")
            continue
        bench_values = _mapping(bench_row.get("values"))
        if any(key not in bench_values for key in dates):
            if bench_id in benchmark_lines or not gap:
                raise _fail(f"{mode} {bench_id} gap is not named")
            continue
        if gap or bench_id not in benchmark_lines:
            raise _fail(f"{mode} {bench_id} line is absent")
        bench_line = benchmark_lines[bench_id]
        if "origin" in bench_line or len(bench_line["tri"]) != length:
            raise _fail(f"{mode} {bench_id} shape differs")
        level = Fraction(1)
        for index, key in enumerate(dates):
            level *= 1 + Fraction(str(bench_values[key]))
            if abs(Fraction(bench_line["tri"][index]) - level) > Fraction(6, 10**9):
                raise _fail(f"{mode} {bench_id} index differs at {key}")
    derivable = {
        bench_id
        for bench_id in ("market:SPY", "market:SPXL")
        if any(
            isinstance(r, Mapping)
            and r.get("series_id") == bench_id
            and all(key in _mapping(r.get("values")) for key in dates)
            for r in _sequence(weekly.get("series"))
        )
    }
    if set(benchmark_lines) != derivable:
        raise _fail(f"{mode} benchmark lines differ from the derivable pair")
    component_origin: list[list[str]] = []
    component_returns: list[list[Fraction]] = []
    for row in rows:
        values, meta, source = _mode_inputs(row, mode) or ({}, {}, "")
        line = by_id[str(row["series_id"])]
        if line["source_mode"] != source or line["kind"] != "STRATEGY":
            raise _fail(f"{mode} {row['series_id']} source or kind differs")
        live_start = _live_start(row)
        returns = [Fraction(str(values[key])) for key in dates]
        level = Fraction(1)
        for index, step in enumerate(returns):
            level *= 1 + step
            if abs(Fraction(line["tri"][index]) - level) > Fraction(6, 10**9):
                raise _fail(
                    f"{mode} {row['series_id']} index differs at {dates[index]}"
                )
        if len(line["tri"]) != length:
            raise _fail(f"{mode} {row['series_id']} index length differs")
        flags = [_origin_of(meta.get(key), live_start, source) for key in dates]
        if _expand(line["origin"], length) != flags:
            raise _fail(f"{mode} {row['series_id']} origin differs")
        component_origin.append(flags)
        component_returns.append(returns)
    ew = by_id[EW_SERIES_ID]
    level = Fraction(1)
    for index in range(length):
        level *= 1 + sum((col[index] for col in component_returns), Fraction(0)) / len(
            component_returns
        )
        if abs(Fraction(ew["tri"][index]) - level) > Fraction(6, 10**9):
            raise _fail(f"{mode} equal-weight index differs at {dates[index]}")
    expected_ew = [
        col[0] if len({c[i] for c in component_origin}) == 1 else "MIXED"
        for i, col in enumerate(zip(*component_origin))
    ]
    if _expand(ew["origin"], length) != expected_ew:
        raise _fail(f"{mode} equal-weight origin differs")
    if ew["definition"] != EW_DEFINITION or ew["kind"] != "PORTFOLIO":
        raise _fail(f"{mode} equal-weight definition differs")


def _verify_slices(block: Mapping[str, Any], panel: Mapping[str, Any]) -> None:
    manifest = panel.get("account_manifest")
    if block.get("status") == "UNAVAILABLE":
        if not block.get("reason"):
            raise _fail("slices unavailable without a reason")
        if isinstance(manifest, Mapping) and any(
            _sequence(_mapping(r.get("observation_trace")).get("links"))
            for r in _sequence(manifest.get("rows"))
            if isinstance(r, Mapping) and r.get("status") == "OK"
        ):
            raise _fail("slices are unavailable but the manifest has traces")
        return
    if not isinstance(manifest, Mapping):
        raise _fail("slices are available without an account manifest")
    if block["scope"] != SCOPE or block["unit"] != "USD_CENTS":
        raise _fail("slice scope or unit differs")
    days = [date.fromisoformat(item) for item in block["dates"]]
    if any(b - a != timedelta(days=1) for a, b in zip(days, days[1:])):
        raise _fail("slice dates are not consecutive calendar days")
    if days[-1] != date.fromisoformat(str(manifest["calculation_cutoff_date"])):
        raise _fail("slice grid does not end at the manifest cutoff")
    traces: list[
        tuple[
            str, str, date, date, list[tuple[date, date, Fraction, Fraction, Fraction]]
        ]
    ] = []
    for row in _sequence(manifest.get("rows")):
        if not isinstance(row, Mapping) or row.get("status") != "OK":
            continue
        links = _sequence(_mapping(row.get("observation_trace")).get("links"))
        if not links:
            continue
        parsed = sorted(
            (
                date.fromisoformat(link["start_date"]),
                date.fromisoformat(link["end_date"]),
                Fraction(str(link["base_nav"])),
                Fraction(str(link["ending_nav"])),
                Fraction(str(link["net_flow"])),
            )
            for link in links
        )
        traces.append(
            (
                str(row["account_id"]),
                str(row["strategy_id"]),
                parsed[0][0],
                parsed[-1][1],
                parsed,
            )
        )
    if days[0] != min(t[2] for t in traces):
        raise _fail("slice grid does not start at the earliest epoch")
    expected_rows = sorted({t[1] for t in traces})
    if [r["strategy_id"] for r in block["rows"]] != expected_rows:
        raise _fail("slice rows differ from the admitted strategies")

    def cents(value: Fraction) -> int:
        # Round half to even on exact cents, as the builder's Decimal quantize does.
        scaled = value * 100
        floor = scaled.numerator // scaled.denominator
        rest = scaled - floor
        if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and floor % 2 == 1):
            floor += 1
        return floor

    def epoch_nav(trace: Any, day: date) -> Fraction:
        account, _strategy, first, last, parsed = trace
        if day < first or day > last:
            return Fraction(0)
        ends = [link[1] for link in parsed]
        position = bisect_right(ends, day) - 1
        if position >= 0:
            return Fraction(parsed[position][3])
        handoff = any(
            other is not trace and other[0] == account and other[3] == first
            for other in traces
        )
        return Fraction(0) if handoff else parsed[0][2]

    totals = [0] * len(days)
    for row in block["rows"]:
        strategy = row["strategy_id"]
        mine = [t for t in traces if t[1] == strategy]
        if row["epoch_ids"] != sorted(
            f"{t[0]}|{t[1]}|{_effective_start(manifest, t)}" for t in mine
        ):
            raise _fail(f"{strategy} epoch_ids differ")
        if len(row["nav_cents"]) != len(days) or len(row["net_flow_cents"]) != len(
            days
        ):
            raise _fail(f"{strategy} slice length differs")
        for index, day in enumerate(days):
            if row["nav_cents"][index] != cents(
                sum((epoch_nav(t, day) for t in mine), Fraction(0))
            ):
                raise _fail(f"{strategy} NAV differs on {day.isoformat()}")
            flow = sum(
                (link[4] for t in mine for link in t[4] if link[1] == day), Fraction(0)
            )
            if row["net_flow_cents"][index] != cents(flow):
                raise _fail(f"{strategy} flow differs on {day.isoformat()}")
            totals[index] += row["nav_cents"][index]
        if row["parent_strategy_id"] is not None or row["sleeve_id"] is not None:
            raise _fail(
                f"{strategy} carries a sleeve identity before sleeve facts exist"
            )
    reconciliation = block["reconciliation"]
    if reconciliation["total_cents"] != totals:
        raise _fail("slice total differs from the sum of the rows")
    capital = _mapping(panel.get("capital")).get("systematic")
    if capital is None:
        if reconciliation["systematic_capital_cents"] is not None:
            raise _fail("systematic capital is published without a source")
    else:
        reference = cents(Fraction(str(capital)))
        if reconciliation["systematic_capital_cents"] != reference:
            raise _fail("systematic capital differs")
        if reconciliation["residual_cents"] != reference - totals[-1]:
            raise _fail("residual differs from capital minus the slice total")


def _effective_start(manifest: Mapping[str, Any], trace: Any) -> str:
    for row in _sequence(manifest.get("rows")):
        if (
            isinstance(row, Mapping)
            and row.get("account_id") == trace[0]
            and row.get("strategy_id") == trace[1]
            and row.get("status") == "OK"
            and date.fromisoformat(_first_link_start(row)) == trace[2]
        ):
            return str(row.get("effective_start"))
    raise _fail("epoch of a slice trace is not in the manifest")


def _first_link_start(row: Mapping[str, Any]) -> str:
    links = _sequence(_mapping(row.get("observation_trace")).get("links"))
    return str(links[0]["start_date"])


def _verify_readout(block: Mapping[str, Any], panel: Mapping[str, Any]) -> None:
    if block.get("headline") != "HWM_GOVERNED_INDEX":
        raise _fail("headline is not the governed HWM index")
    proof = _mapping(panel.get("hwm_governed_proof"))
    path = _sequence(proof.get("index_path"))
    index = _mapping(block.get("hwm_governed_index"))
    if proof.get("status") == "AVAILABLE" and len(path) >= 2:
        base = Fraction(str(path[0]["index"]))
        last = Fraction(str(path[-1]["index"]))
        if index.get("status") != "AVAILABLE":
            raise _fail("headline is unavailable but the governed path exists")
        published = Fraction(str(index["cumulative_return"]))
        if abs(published - (last / base - 1)) > Fraction(1, 10**20):
            raise _fail("headline cumulative return differs")
        if (
            index["window_start"] != path[0]["as_of"]
            or index["window_end"] != path[-1]["as_of"]
        ):
            raise _fail("headline window differs")
    elif index.get("status") != "UNAVAILABLE":
        raise _fail("headline is available without a governed path")
    totals = _mapping(panel.get("portfolio_twr_cumulative"))
    if not totals:
        if (
            block["summed_epoch_twr"] is not None
            or block["headline_minus_summed_epoch_twr"] is not None
            or block["summed_epoch_unavailable_reason"]
            != "PORTFOLIO_TWR_CUMULATIVE_ABSENT"
        ):
            raise _fail("summed-epoch figure is published without its source")
    else:
        source = totals.get("summed_epoch_twr") or None
        if block["summed_epoch_twr"] != source:
            raise _fail("summed-epoch figure differs from portfolio_twr_cumulative")
        if source is None or source.get("status") != "AVAILABLE":
            expected_reason = str(
                (source or {}).get("reason")
                or totals.get("reason")
                or "SUMMED_EPOCH_TWR_UNAVAILABLE"
            )
            expected_difference = None
        else:
            expected_reason = None
            expected_difference = totals.get("headline_minus_summed_epoch_twr")
        if block["summed_epoch_unavailable_reason"] != expected_reason:
            raise _fail("summed-epoch reason differs from portfolio_twr_cumulative")
        if block["headline_minus_summed_epoch_twr"] != expected_difference:
            raise _fail("headline difference differs from portfolio_twr_cumulative")


def verify_performance_tab(block: Mapping[str, Any], panel: Mapping[str, Any]) -> None:
    """Re-derive the block from the panel inputs; raise on any difference."""

    if (
        block.get("schema_version") != PERFORMANCE_TAB_SCHEMA_VERSION
        or block.get("scope") != "SYSTEMATIC_EX_RESERVE"
    ):
        raise _fail("schema_version or scope differs")
    weekly = _mapping(panel.get("weekly_returns"))
    if block.get("as_of") != weekly.get("as_of_date"):
        raise _fail("as_of differs from the weekly returns")
    modes = _mapping(block.get("modes"))
    if sorted(modes) != ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]:
        raise _fail("modes differ")
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        _verify_mode(_mapping(modes[mode]), weekly, mode)
    benchmarks = _sequence(block.get("benchmarks"))
    expected_ids = ["market:SPY", "market:SPXL"]
    if [b.get("series_id") for b in benchmarks] != expected_ids:
        raise _fail("benchmarks differ from SPY and SPXL")
    for benchmark in benchmarks:
        present = any(
            isinstance(row, Mapping) and row.get("series_id") == benchmark["series_id"]
            for row in _sequence(weekly.get("series"))
        )
        if benchmark.get("substitute") is not None:
            raise _fail("a benchmark is substituted")
        if present:
            if benchmark.get("status") != "AVAILABLE" or benchmark.get("required_pull"):
                raise _fail("an admitted benchmark must be AVAILABLE with no pull")
            continue
        pull = _mapping(benchmark.get("required_pull"))
        if (
            benchmark.get("status") != "UNAVAILABLE"
            or pull.get("source_id") != "yahoo-adjusted-etf-daily"
            or pull.get("to_date") != "2026-09-17"
        ):
            raise _fail("benchmark must be an explicit UNAVAILABLE entry with its pull")
    _verify_slices(_mapping(block.get("slices")), panel)
    _verify_readout(_mapping(block.get("readout")), panel)

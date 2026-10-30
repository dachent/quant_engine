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
from typing import Any, Final, Mapping, Sequence, cast

PERFORMANCE_TAB_SCHEMA_VERSION: Final = 3
SCOPE: Final = "SYSTEMATIC_EX_RESERVE"
MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
TRI_DECIMALS: Final = 8

_PRECISION: Final = 50
_WHOLE: Final = Decimal(1)
_TRI_QUANTUM: Final = Decimal(1).scaleb(-TRI_DECIMALS)
_TRAILING_QUANTUM: Final = Decimal(1).scaleb(-12)

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


def _retired_component_cents(
    reconciliation: Mapping[str, Any], retired: Sequence[Mapping[str, Any]]
) -> int:
    """The retired-epochs component, from the report's unrounded outside gain.

    The report publishes ``epoch_set.gain_of_outside_report_epochs_usd`` exactly; the
    component is that value rounded once, not the sum of per-line rounded cents
    (which can differ by a cent).  The report always carries it, so its absence
    stops the build.
    """

    outside = _mapping(reconciliation.get("epoch_set")).get(
        "gain_of_outside_report_epochs_usd"
    )
    if outside is None:
        raise PerformanceTabError(
            "capital_reconciliation.epoch_set.gain_of_outside_report_epochs_usd "
            f"is absent for {len(retired)} retired epochs"
        )
    with localcontext() as context:
        context.prec = _PRECISION
        return -_cents(_dec(outside, "gain of outside report epochs"))


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
    declared = _flag_origin(meta_entry)
    if declared is not None:
        return declared
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


ORIGIN_DERIVED_FLAG: Final = "ORIGIN_DERIVED"
ORIGIN_PROVIDER_MODEL_LIVE_FLAG: Final = "ORIGIN_PROVIDER_MODEL_LIVE"
EW_AVAILABLE_SERIES_ID: Final = "portfolio:equal_weight_available"
EW_AVAILABLE_DEFINITION: Final = (
    "Equal-weight portfolio of the strategy lines that have a value in each week "
    "(the member count of every week is published): each week's return is the "
    "arithmetic mean of the available strategy weekly total returns, compounded "
    "from 1.0 immediately before the first week with any strategy.  It is a "
    "secondary line; the headline equal-weight line starts only where every "
    "strategy has data."
)


def _flag_origin(meta_entry: Any) -> str | None:
    """The origin a weekly observation's own flag list declares, when it does."""

    flags = (
        meta_entry[4]
        if isinstance(meta_entry, list)
        and len(meta_entry) > 4
        and isinstance(meta_entry[4], list)
        else []
    )
    if ORIGIN_DERIVED_FLAG in flags:
        return "DERIVED"
    if ORIGIN_PROVIDER_MODEL_LIVE_FLAG in flags:
        return "PROVIDER_MODEL_LIVE"
    return None


def _segments(present: Sequence[bool]) -> list[list[int]]:
    out: list[list[int]] = []
    for index, flag in enumerate(present):
        if not flag:
            continue
        if out and out[-1][1] == index - 1:
            out[-1][1] = index
        else:
            out.append([index, index])
    return out


def _segmented_path(
    returns: Sequence[Decimal | None],
) -> tuple[list[float | None], list[list[int]]]:
    """Index path, null outside the segments, 1.0 before each segment's first week."""

    segments = _segments([item is not None for item in returns])
    out: list[float | None] = [None] * len(returns)
    for start, end in segments:
        level = Decimal(1)
        for index in range(start, end + 1):
            level = level * (1 + cast(Decimal, returns[index]))
            out[index] = float(level.quantize(_TRI_QUANTUM))
    return out, segments


def _origin_runs(flags: Sequence[str | None]) -> list[list[Any]]:
    """Origin runs over the non-null indices only; a null index breaks a run."""

    runs: list[list[Any]] = []
    for index, flag in enumerate(flags):
        if flag is None:
            continue
        if runs and runs[-1][2] == flag and runs[-1][1] == index - 1:
            runs[-1][1] = index
        else:
            runs.append([index, index, flag])
    return runs


def _line_fields(
    returns: Sequence[Decimal | None], week_keys: Sequence[str]
) -> dict[str, Any]:
    path, segments = _segmented_path(returns)
    first = segments[0][0]
    return {
        "first_week": week_keys[first],
        "first_index": first,
        "last_index": segments[-1][1],
        "segments": segments,
        "tri": path,
    }


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
    per.sort(key=lambda item: str(item[0]["series_id"]))
    firsts = {str(row["series_id"]): min(values) for row, values, _m, _s in per}
    last = max(_day(max(values), "last week") for _r, values, _m, _s in per)
    for row, values, _m, _s in per:
        if last.isoformat() not in values:
            return _unavailable(f"LAST_WEEK_MISSING:{row['series_id']}")
    benchmark_rows: list[tuple[str, Mapping[str, Any]]] = []
    for series_id, _ticker, _since, _note in _BENCHMARKS:
        bench = _benchmark_row(weekly, series_id)
        if bench is not None and _mapping(bench.get("values")):
            benchmark_rows.append((series_id, bench))
    union_candidates = {
        **firsts,
        **{
            f"{series_id}": min(_mapping(bench["values"]))
            for series_id, bench in benchmark_rows
        },
    }
    union_start_key = min(union_candidates.values())
    union_setter = min(k for k, v in union_candidates.items() if v == union_start_key)
    weeks = _mondays(_day(union_start_key, "union start"), last)
    week_keys = [week.isoformat() for week in weeks]

    strategy_returns: list[list[Decimal | None]] = []
    strategy_flags: list[list[str | None]] = []
    gaps: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    with localcontext() as context:
        context.prec = _PRECISION
        for row, values, meta, source in per:
            live_start = _live_start(row)
            returns: list[Decimal | None] = []
            flags: list[str | None] = []
            for key in week_keys:
                if key in values:
                    returns.append(_dec(values[key], f"{row.get('series_id')} {key}"))
                    flags.append(_week_origin(meta.get(key), live_start, source))
                else:
                    returns.append(None)
                    flags.append(None)
            first_i = next(i for i, v in enumerate(returns) if v is not None)
            missing = [
                week_keys[i]
                for i in range(first_i, len(week_keys))
                if returns[i] is None
            ]
            if missing:
                gaps.append(
                    {
                        "series_id": str(row["series_id"]),
                        "missing_weeks": len(missing),
                        "first_missing_week": missing[0],
                        "last_missing_week": missing[-1],
                    }
                )
            strategy_returns.append(returns)
            strategy_flags.append(flags)
            lines.append(
                {
                    "series_id": row["series_id"],
                    "display_name": row.get("display_name"),
                    "kind": "STRATEGY",
                    "basis": "STRATEGY_TOTAL_RETURN",
                    "definition": STRATEGY_DEFINITION,
                    "source_mode": source,
                    **_line_fields(returns, week_keys),
                    "origin": _origin_runs(flags),
                }
            )
        # The group anchor is the first week from which every strategy line is
        # present without a break through the last week.
        run_starts = [
            _segments([v is not None for v in r])[-1][0] for r in strategy_returns
        ]
        anchor_index = max(run_starts)
        anchor_setter = next(
            str(per[i][0]["series_id"])
            for i in range(len(per))
            if run_starts[i] == anchor_index
        )
        first_common_key = max(firsts.values())
        first_setter = max(sorted(firsts), key=lambda key: firsts[key])
        count = len(per)
        ew_returns: list[Decimal | None] = [None] * len(week_keys)
        ew_flags: list[str | None] = [None] * len(week_keys)
        for i in range(anchor_index, len(week_keys)):
            ew_returns[i] = (
                sum((cast(Decimal, r[i]) for r in strategy_returns), Decimal(0)) / count
            )
            ew_flags[i] = _combined_flag([cast(str, f[i]) for f in strategy_flags])
        lines.append(
            {
                "series_id": EW_SERIES_ID,
                "display_name": "Equal-weight portfolio",
                "kind": "PORTFOLIO",
                "basis": "EQUAL_WEIGHT_WEEKLY_REBALANCED_TOTAL_RETURN",
                "definition": EW_DEFINITION,
                "source_mode": mode,
                **_line_fields(ew_returns, week_keys),
                "member_count": count,
                "member_series_ids": [str(row["series_id"]) for row, *_ in per],
                "origin": _origin_runs(ew_flags),
            }
        )
        avail_returns: list[Decimal | None] = [None] * len(week_keys)
        avail_flags: list[str | None] = [None] * len(week_keys)
        member_counts: list[int | None] = [None] * len(week_keys)
        for i in range(len(week_keys)):
            present = [r[i] for r in strategy_returns if r[i] is not None]
            if present:
                avail_returns[i] = sum(
                    (cast(Decimal, v) for v in present), Decimal(0)
                ) / len(present)
                avail_flags[i] = _combined_flag(
                    [cast(str, f[i]) for f in strategy_flags if f[i] is not None]
                )
                member_counts[i] = len(present)
        lines.append(
            {
                "series_id": EW_AVAILABLE_SERIES_ID,
                "display_name": "Equal-weight, strategies available that week",
                "kind": "PORTFOLIO",
                "basis": "EQUAL_WEIGHT_AVAILABLE_STRATEGIES_WEEKLY_REBALANCED_TOTAL_RETURN",
                "definition": EW_AVAILABLE_DEFINITION,
                "source_mode": mode,
                **_line_fields(avail_returns, week_keys),
                "member_count": None,
                "member_counts": member_counts,
                "origin": _origin_runs(avail_flags),
            }
        )
        benchmark_gaps: list[dict[str, str]] = []
        for series_id, bench_source in benchmark_rows:
            values = _mapping(bench_source.get("values"))
            if not any(key in values for key in week_keys):
                benchmark_gaps.append(
                    {"series_id": series_id, "reason": f"NO_WEEK_IN_GRID:{series_id}"}
                )
                continue
            bench_returns = [
                _dec(values[key], f"{series_id} {key}") if key in values else None
                for key in week_keys
            ]
            lines.append(
                {
                    "series_id": series_id,
                    "display_name": bench_source.get("display_name"),
                    "kind": "BENCHMARK",
                    "basis": bench_source.get("return_basis"),
                    "definition": BENCHMARK_DEFINITION,
                    "source_mode": "WEEKLY_RETURNS_SINGLE_MODE",
                    **_line_fields(bench_returns, week_keys),
                }
            )
    ends: list[str] = []
    for index, key in enumerate(week_keys):
        candidates = [
            _day(_sequence(meta.get(key))[2], "week end").isoformat()
            for _r, _v, meta, _s in per
            if len(_sequence(meta.get(key))) > 2
        ]
        ends.append(
            max(candidates)
            if candidates
            else (weeks[index] + timedelta(days=6)).isoformat()
        )
    current = _mapping(weekly.get("current_week"))
    return {
        "status": "AVAILABLE",
        "grid": "ISO_WEEK_START_MONDAY",
        "base_point": "START_OF_EACH_LINE_FIRST_WEEK",
        "union_start_date": union_start_key,
        "union_start_set_by": union_setter,
        "group_anchor_date": week_keys[anchor_index],
        "group_anchor_index": anchor_index,
        "group_anchor_set_by": anchor_setter,
        "common_start_date": week_keys[anchor_index],
        "common_start_set_by": anchor_setter,
        "first_observation_common_start": first_common_key,
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


def _sleeve_links(panel: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Strategy id -> (parent strategy id, sleeve id) for every sleeve strategy.

    The panel's lifecycle rows (current and retained records alike) carry the
    link the report proved from the nested-strategy tag map and the parent's
    assignment (#567); a row without a sleeve carries neither field.
    """

    links: dict[str, tuple[str, str]] = {}
    for row in _sequence(panel.get("strategies")):
        if not isinstance(row, Mapping) or row.get("sleeve_id") is None:
            continue
        strategy = str(row.get("strategy_id"))
        parent, sleeve = row.get("parent_strategy_id"), row.get("sleeve_id")
        if not isinstance(parent, str) or not isinstance(sleeve, str):
            raise PerformanceTabError("a sleeve strategy lacks its parent or id")
        if strategy in links:
            raise PerformanceTabError("a sleeve strategy is listed twice")
        links[strategy] = (parent, sleeve)
    return links


def _build_slices(
    manifest: Mapping[str, Any] | None,
    capital: Mapping[str, Any],
    sleeve_links: Mapping[str, tuple[str, str]],
    remainder: Mapping[str, Any] | None = None,
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
                "first_end": _day(links[0]["end_date"], "first link end"),
                "legs": _sequence(row.get("performance_flow_legs")),
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
    flows = _build_flows(epochs, days, nav_rows)
    rows = []
    for strategy in strategies:
        first_day = next(
            (days[i].isoformat() for i, v in enumerate(nav_rows[strategy]) if v), None
        )
        rows.append(
            {
                "slice_id": strategy,
                "strategy_id": strategy,
                "parent_strategy_id": (
                    sleeve_links[strategy][0] if strategy in sleeve_links else None
                ),
                "sleeve_id": (
                    sleeve_links[strategy][1] if strategy in sleeve_links else None
                ),
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
        "flows": flows,
        "rows": rows,
        "excluded_epochs": excluded,
        "reconciliation": {
            "total_cents": totals,
            "reference_date": cutoff.isoformat(),
            "systematic_capital_cents": reference,
            "residual_cents": None if reference is None else reference - totals[-1],
            "residual_label": _residual_label(
                bool(sleeve_links),
                None if reference is None else reference - totals[-1],
                remainder,
            ),
            "reason": None if reference is not None else "CAPITAL_SYSTEMATIC_ABSENT",
        },
    }


SLICE_PERIMETER_FLOW_DEFINITION: Final = (
    "F(d) = the money that crossed the boundary of the plotted slices on day d: "
    "the observation links' net_flow recognised on d, plus the opening NAV of an "
    "epoch (on its first day, or on its first link end when it inherits a "
    "predecessor of the same account), minus the closing NAV of an epoch on the "
    "day after its last link end.  Transfers between two plotted epochs appear "
    "with both signs and cancel in the sum; a transfer from or to a source outside "
    "the plotted slices (a reserve or unplotted account, even when the ledger "
    "labels it internal) is a contribution.  F reconciles to the slice total by "
    "total(d) - total(d-1) = F(d) + P&L(d)."
)
SLICE_RESIDUAL_TREATMENT: Final = (
    "The residual against systematic capital is not a flow: it is the parent "
    "account's value outside the plotted epochs (unallocated cash, margin debit, "
    "other sleeves) and is excluded from the matching.  Benchmark dollars are "
    "compared with the slice total; the difference to systematic capital is "
    "published separately."
)


def _build_flows(
    epochs: Sequence[Mapping[str, Any]],
    days: Sequence[date],
    nav_rows: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """The perimeter flow series F(d) the benchmark dollars are matched to."""

    index = {day: i for i, day in enumerate(days)}
    size = len(days)
    zero = Decimal(0)
    link_flow = [zero] * size
    opening = [zero] * size
    closing = [zero] * size
    internal_in = internal_out = zero
    with localcontext() as context:
        context.prec = _PRECISION
        for epoch in epochs:
            for day, value in epoch["flow"].items():
                if day in index:
                    link_flow[index[day]] += value
            when = epoch["first_end"] if epoch["skip_opening"] else epoch["first"]
            if when in index:
                opening[index[when]] += epoch["opening"]
            after = epoch["last"] + timedelta(days=1)
            if after in index and epoch["last"] in epoch["nav"]:
                closing[index[after]] += epoch["nav"][epoch["last"]]
            for leg in epoch["legs"]:
                if not isinstance(leg, Mapping):
                    continue
                amount = _dec(leg.get("amount"), "flow leg amount")
                if leg.get("class") == "INTERNAL_IN":
                    internal_in += amount
                elif leg.get("class") == "INTERNAL_OUT":
                    internal_out += amount
        net = [link_flow[i] + opening[i] - closing[i] for i in range(size)]
        net_cents = [_cents(v) for v in net]
        total_cents = sum(nav_rows[s][-1] for s in nav_rows)
        contributed = sum(net_cents)
        net_internal = internal_in - internal_out
    return {
        "unit": "USD_CENTS",
        "definition": SLICE_PERIMETER_FLOW_DEFINITION,
        "net_flow_cents": net_cents,
        "opening_cents": [_cents(v) for v in opening],
        "closing_cents": [_cents(v) for v in closing],
        "link_net_flow_cents": [_cents(v) for v in link_flow],
        "total_contributed_cents": contributed,
        "implied_pnl_cents": total_cents - contributed,
        "internal_cancellation": {
            "status": (
                "CANCELS_EXACTLY"
                if net_internal == 0
                else "NET_INTERNAL_FLOW_CROSSES_PERIMETER"
            ),
            "internal_in_cents": _cents(internal_in),
            "internal_out_cents": _cents(internal_out),
            "net_cents": _cents(net_internal),
            "treatment": "FLOWS_CROSSING_THE_SLICE_PERIMETER_ARE_CONTRIBUTIONS",
        },
        "residual_treatment": SLICE_RESIDUAL_TREATMENT,
    }


def _residual_label(
    sleeves: bool, residual_cents: int | None, remainder: Mapping[str, Any] | None
) -> str:
    """The residual's name; with sleeves and a memo remainder, both amounts are quoted."""

    base = "SYSTEMATIC_CAPITAL_MINUS_SLICE_TOTAL_AT_CUTOFF"
    if (
        sleeves
        and residual_cents is not None
        and isinstance(remainder, Mapping)
        and remainder.get("amount_usd") is not None
    ):
        with localcontext() as context:
            context.prec = _PRECISION
            memo = _dec(remainder["amount_usd"], "perimeter remainder").quantize(
                Decimal("0.01")
            )
            residual = Decimal(residual_cents) / 100
            rest = residual - memo
        return (
            f"{base}: residual {residual:,.2f} = "
            f"capital_reconciliation.memo.perimeter_remainder {memo:,.2f} "
            "(the residual parent account's value outside the product sleeve: "
            f"unallocated cash, margin debit and any other sleeve's remainder) {rest:+,.2f} "
            "cash left in liquidated epochs"
        )
    return (
        f"{base} (cash left in a liquidated epoch"
        + (
            " and in the unallocated cash of a residual parent account"
            if sleeves
            else ""
        )
        + "; named in capital_reconciliation.memo.perimeter_remainder "
        "(component RESIDUAL_PARENT_REMAINDER_NOT_IN_PRODUCT_EPOCHS) "
        "when that block carries it)"
    )


def _systematic_cents(capital: Mapping[str, Any]) -> int | None:
    value = capital.get("systematic")
    if value is None:
        return None
    with localcontext() as context:
        context.prec = _PRECISION
        return _cents(_dec(value, "capital.systematic"))


# ---------------------------------------------------------------- benchmark dollars

FLOW_MATCHED_DEFINITION: Final = (
    "B(t) = sum over flow days d <= t of F(d) * TRI(t) / TRI(d): every dollar the "
    "slices received on day d is invested in the benchmark at TRI(d) and held.  "
    "F is slices.flows.net_flow_cents; the daily path is B(t) = B(t-1) * "
    "TRI(t)/TRI(t-1) + F(t).  TRI of a day without a benchmark observation carries "
    "the prior value."
)
TRAILING_WINDOWS: Final = (
    ("1w", 7),
    ("1m", 30),
    ("3m", 91),
    ("6m", 183),
    ("YTD", None),
    ("1y", 365),
    ("SINCE_START", "start"),
)


def _weekly_step_levels(
    bench_row: Mapping[str, Any],
) -> list[tuple[date, Decimal]] | str:
    """(observation end date, level) per week from the weekly returns, or a reason."""

    values = _mapping(bench_row.get("values"))
    meta = _mapping(bench_row.get("observation_meta"))
    weeks = sorted(values)
    if not weeks:
        return "NO_WEEKLY_VALUES"
    level = Decimal(1)
    out: list[tuple[date, Decimal]] = []
    previous: date | None = None
    for key in weeks:
        monday = _day(key, "benchmark week")
        if previous is not None and monday != previous + timedelta(days=7):
            return f"MISSING_WEEK_AFTER:{previous.isoformat()}"
        previous = monday
        level = level * (1 + _dec(values[key], f"benchmark {key}"))
        entry = meta.get(key)
        end = (
            _day(entry[2], "benchmark week end")
            if isinstance(entry, list) and len(entry) > 2
            else monday + timedelta(days=6)
        )
        out.append((end, level))
    return out


def _daily_levels(returns: Mapping[str, Any]) -> list[tuple[date, Decimal]]:
    level = Decimal(1)
    out: list[tuple[date, Decimal]] = []
    for key in sorted(returns):
        level = level * (1 + _dec(returns[key], f"daily benchmark {key}"))
        out.append((_day(key, "daily benchmark date"), level))
    return out


def _carry(
    levels: Sequence[tuple[date, Decimal]], days: Sequence[date]
) -> list[Decimal | None]:
    """The level on each calendar day: the latest observation on or before it."""

    out: list[Decimal | None] = []
    position = -1
    for day in days:
        while position + 1 < len(levels) and levels[position + 1][0] <= day:
            position += 1
        out.append(levels[position][1] if position >= 0 else None)
    return out


def _window_return(
    tri: Sequence[Decimal | None],
    days: Sequence[date],
    start: date,
) -> Decimal | None:
    end_level = tri[-1]
    if end_level is None or start < days[0]:
        return None
    position = (start - days[0]).days
    begin = tri[position] if 0 <= position < len(tri) else None
    return None if begin is None or begin <= 0 else end_level / begin - 1


def _trailing(
    tri: Sequence[Decimal | None], days: Sequence[date], since_start: date
) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    end = days[-1]
    for label, spec in TRAILING_WINDOWS:
        if spec == "start":
            start = since_start
        elif spec is None:
            start = date(end.year - 1, 12, 31)
        else:
            start = end - timedelta(days=int(spec))
        value = _window_return(tri, days, start)
        out[label] = None if value is None else _text(value.quantize(_TRAILING_QUANTUM))
    return out


def _bench_unavailable(series_id: str, ticker: str, reason: str) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "ticker": ticker,
        "status": "UNAVAILABLE",
        "reason": reason,
        "method": "FLOW_MATCHED",
        "method_definition": FLOW_MATCHED_DEFINITION,
        "tri_basis": None,
        "flow_timing": None,
        "tri": None,
        "nav_cents": None,
        "end_value_cents": None,
        "portfolio_minus_benchmark_cents": None,
        "systematic_capital_minus_benchmark_cents": None,
        "trailing_since_start_date": None,
        "trailing": None,
    }


def _daily_usable(
    daily: Mapping[str, Any] | None, weekly_last_end: date | None
) -> tuple[Mapping[str, Any] | None, str | None]:
    if not isinstance(daily, Mapping) or not _mapping(daily.get("returns")):
        return None, "DAILY_SERIES_NOT_SUPPLIED_TO_THE_PERFORMANCE_TAB_BUILDER"
    dates = sorted(_mapping(daily["returns"]))
    if weekly_last_end is not None and _day(dates[-1], "daily last") < weekly_last_end:
        return None, "DAILY_SERIES_ENDS_BEFORE_THE_LAST_WEEKLY_OBSERVATION"
    return daily, None


def _build_slice_benchmarks(
    weekly: Mapping[str, Any],
    slices: Mapping[str, Any],
    since_start: date,
    daily_benchmarks: Mapping[str, Any] | None,
    allow_weekly_fallback: bool = False,
) -> list[dict[str, Any]]:
    days = [_day(item, "slice date") for item in slices["dates"]]
    flows = [int(v) for v in slices["flows"]["net_flow_cents"]]
    totals = list(slices["reconciliation"]["total_cents"])
    capital = slices["reconciliation"]["systematic_capital_cents"]
    out: list[dict[str, Any]] = []
    for series_id, ticker, _since, _note in _BENCHMARKS:
        bench_row = _benchmark_row(weekly, series_id)
        if bench_row is None:
            out.append(_bench_unavailable(series_id, ticker, "NOT_ADMITTED"))
            continue
        weekly_levels = _weekly_step_levels(bench_row)
        if isinstance(weekly_levels, str):
            out.append(_bench_unavailable(series_id, ticker, weekly_levels))
            continue
        daily, why = _daily_usable(
            _mapping(daily_benchmarks).get(series_id) if daily_benchmarks else None,
            weekly_levels[-1][0],
        )
        if daily is not None:
            levels = _daily_levels(_mapping(daily["returns"]))
            basis, timing = "DAILY_ADJUSTED_CLOSE", "FLOW_DATE_CLOSE"
        elif allow_weekly_fallback:
            levels = weekly_levels
            basis, timing = "WEEKLY_TOTAL_RETURN_INDEX", "FLOW_WEEK_START"
        else:
            out.append(
                _bench_unavailable(series_id, ticker, f"DAILY_SERIES_REQUIRED:{why}")
            )
            continue
        with localcontext() as context:
            context.prec = _PRECISION
            tri = _carry(levels, days)
            first_flow = next((i for i, v in enumerate(flows) if v), None)
            if first_flow is None:
                out.append(_bench_unavailable(series_id, ticker, "NO_FLOW_IN_WINDOW"))
                continue
            if any(tri[i] is None for i in range(first_flow, len(days))):
                out.append(
                    _bench_unavailable(
                        series_id, ticker, "BENCHMARK_STARTS_AFTER_THE_FIRST_FLOW"
                    )
                )
                continue
            nav: list[Decimal] = [Decimal(0)] * len(days)
            running = Decimal(0)
            for i in range(first_flow, len(days)):
                previous = tri[i - 1] if i > first_flow else None
                if previous is not None:
                    running = running * cast(Decimal, tri[i]) / previous
                running += Decimal(flows[i])
                nav[i] = running
            nav_cents = [int(v.quantize(_WHOLE)) for v in nav]
            trailing = _trailing(tri, days, since_start)
            out.append(
                {
                    "series_id": series_id,
                    "ticker": ticker,
                    "status": "AVAILABLE",
                    "reason": None,
                    "method": "FLOW_MATCHED",
                    "method_definition": FLOW_MATCHED_DEFINITION,
                    "tri_basis": basis,
                    "flow_timing": timing,
                    "tri": [
                        None if v is None else float(v.quantize(_TRI_QUANTUM))
                        for v in tri
                    ],
                    "nav_cents": nav_cents,
                    "end_value_cents": nav_cents[-1],
                    "portfolio_minus_benchmark_cents": totals[-1] - nav_cents[-1],
                    "systematic_capital_minus_benchmark_cents": (
                        None if capital is None else capital - nav_cents[-1]
                    ),
                    "trailing_since_start_date": since_start.isoformat(),
                    "trailing": trailing,
                }
            )
    return out


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
    weekly: Mapping[str, Any],
    series_id: str,
    ticker: str,
    since: str,
    note: str,
    daily: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = _benchmark_row(weekly, series_id)
    if row is not None:
        weeks = sorted(_mapping(row.get("values")))
        weekly_last = None
        levels = _weekly_step_levels(row)
        if not isinstance(levels, str):
            weekly_last = levels[-1][0]
        usable, why = _daily_usable(daily, weekly_last)
        returns = _mapping(usable.get("returns")) if usable else {}
        return {
            "series_id": series_id,
            "ticker": ticker,
            "status": "AVAILABLE",
            "reason": None,
            "substitute": None,
            "source": "weekly_returns.series",
            "required_pull": None,
            "first_week": weeks[0] if weeks else None,
            "last_week": weeks[-1] if weeks else None,
            "daily": {
                "status": "AVAILABLE" if usable else "UNAVAILABLE",
                "source_id": str(usable.get("source_id")) if usable else None,
                "first_date": min(returns) if returns else None,
                "last_date": max(returns) if returns else None,
                "reason": why,
            },
        }
    return {
        "series_id": series_id,
        "ticker": ticker,
        "status": "UNAVAILABLE",
        "reason": "NOT_ADMITTED_NO_GOVERNED_PULL",
        "substitute": None,
        "source": None,
        "required_pull": {**REQUIRED_PULL, "from_date": since, "note": note},
        "first_week": None,
        "last_week": None,
        "daily": {
            "status": "UNAVAILABLE",
            "source_id": None,
            "first_date": None,
            "last_date": None,
            "reason": "NOT_ADMITTED_NO_GOVERNED_PULL",
        },
    }


def _return_scope_labels(panel: Mapping[str, Any]) -> dict[str, str]:
    """Series id -> the return-scope label the panel's strategy row states, when it does."""

    return {
        f"strategy:{row.get('strategy_name')}": str(row["return_scope_label"])
        for row in _sequence(panel.get("strategies"))
        if isinstance(row, Mapping) and isinstance(row.get("return_scope_label"), str)
    }


def _qualified_label(label: str, line: Mapping[str, Any], dates: object) -> str:
    """A return-scope label, qualified where the line's own weeks are not live.

    The label states the parent account's live series; a hybrid line's weeks before live
    capital are backtest (or a mix at the boundary), which the line's origin runs say.
    """

    grid = _sequence(dates)
    parts = [
        (
            f"{grid[start]} ({flag})"
            if start == end
            else f"{grid[start]} to {grid[end]} ({flag})"
        )
        for start, end, flag in _sequence(line.get("origin"))
        if flag != "LIVE" and 0 <= start <= end < len(grid)
    ]
    if not parts:
        return label
    return (
        f"{label} In this mode the weeks {', '.join(parts)} are not that live series."
    )


def _daily_from_weekly(weekly: Mapping[str, Any]) -> dict[str, Any] | None:
    """``weekly_returns.daily_benchmark_returns`` as series id -> source and returns.

    The weekly-returns generation carries parallel ``dates`` and ``returns`` arrays
    per series; an unavailable or malformed block is not used.
    """

    block = _mapping(weekly.get("daily_benchmark_returns"))
    if block.get("status") != "AVAILABLE":
        return None
    out: dict[str, Any] = {}
    for item in _sequence(block.get("series")):
        if not isinstance(item, Mapping):
            continue
        dates, returns = _sequence(item.get("dates")), _sequence(item.get("returns"))
        if not dates or len(dates) != len(returns):
            continue
        out[str(item.get("series_id"))] = {
            "source_id": block.get("source_id"),
            "returns": dict(zip((str(d) for d in dates), returns)),
        }
    return out or None


def build_performance_tab(
    panel: Mapping[str, Any],
    daily_benchmarks: Mapping[str, Any] | None = None,
    allow_weekly_fallback: bool = False,
) -> dict[str, Any]:
    """Build the ``performance_tab`` block from the assembled panel mapping.

    ``daily_benchmarks`` optionally maps a benchmark series id to
    ``{"source_id": ..., "returns": {ISO date: daily total return}}`` from the
    admitted daily adjusted-close series, or is read from
    ``weekly_returns.daily_benchmark_returns`` (parallel dates/returns arrays per
    series) when the weekly generation carries it.  Without either the flow-matched dollars are UNAVAILABLE with the reason
    (``DAILY_SERIES_REQUIRED``): the release path never ships the weekly index.
    ``allow_weekly_fallback`` is for fixtures and development only; it labels the
    weekly basis.
    """

    weekly = _mapping(panel.get("weekly_returns"))
    if daily_benchmarks is None:
        daily_benchmarks = _daily_from_weekly(weekly)
    modes: dict[str, Any] = {}
    for mode in MODES:
        if not weekly.get("series"):
            modes[mode] = _unavailable("WEEKLY_RETURNS_ABSENT")
            continue
        try:
            modes[mode] = _build_mode(weekly, mode)
        except PerformanceTabError as exc:
            modes[mode] = _unavailable(str(exc))
    scopes = _return_scope_labels(panel)
    for block in modes.values():
        for line in _sequence(block.get("lines")) if isinstance(block, dict) else []:
            if isinstance(line, dict) and line.get("series_id") in scopes:
                line["return_scope_label"] = _qualified_label(
                    scopes[str(line["series_id"])], line, block.get("dates")
                )
    readout = _build_readout(panel)
    manifest = panel.get("account_manifest")
    try:
        slices = _build_slices(
            manifest if isinstance(manifest, Mapping) else None,
            _mapping(panel.get("capital")),
            _sleeve_links(panel),
            _mapping(
                _mapping(_mapping(panel.get("capital_reconciliation")).get("memo")).get(
                    "perimeter_remainder"
                )
            )
            or None,
        )
        if slices.get("status") == "AVAILABLE":
            slices["flows"]["report_crosscheck"] = _report_crosscheck(
                slices["flows"],
                _mapping(panel.get("capital_reconciliation")),
                _mapping(manifest),
                slices["reconciliation"]["total_cents"][-1],
                _report_epoch_keys(_mapping(panel.get("invested_capital"))),
            )
            slices["benchmarks"] = _build_slice_benchmarks(
                weekly,
                slices,
                _since_start(readout, slices),
                daily_benchmarks,
                allow_weekly_fallback,
            )
    except PerformanceTabError as exc:
        slices = _unavailable(str(exc))
    return {
        "schema_version": PERFORMANCE_TAB_SCHEMA_VERSION,
        "as_of": weekly.get("as_of_date"),
        "scope": SCOPE,
        "modes": modes,
        "benchmarks": [
            _benchmark_entry(
                weekly,
                series_id,
                ticker,
                since,
                note,
                _mapping(daily_benchmarks).get(series_id) if daily_benchmarks else None,
            )
            for series_id, ticker, since, note in _BENCHMARKS
        ],
        "slices": slices,
        "readout": readout,
    }


CROSSCHECK_NOTES: Final = {
    "CLOSED_EPOCH_VALUE_AT_CUTOFF": (
        "The report's current value of the epochs open at the cutoff carries the "
        "value of a closed epoch (post-liquidation) that no plotted slice holds."
    ),
    "OPENING_BASIS_TRACE_VS_LEDGER": (
        "Each epoch's P&L from the NAV trace (final value - opening NAV - flows) "
        "against the ledger gain on the manifest row: epochs that open from the last "
        "observation before the start (or from a first positive observation) carry a "
        "NAV-trace opening that differs from the governed ledger opening capital. "
        "Lines name every epoch."
    ),
    "RETIRED_EPOCHS_OUTSIDE_REPORT_EPOCH_SET": (
        "The report's totals use only the governed epochs (invested_capital."
        "required_epochs); retired epochs that the slices still plot (closed sleeves "
        "of a parent account, retired provider funds) carry gains that are not in the "
        "report's gain of all epochs.  The amount is the report's unrounded gain of the "
        "outside epochs rounded once; lines name each such epoch with its ledger gain, "
        "each rounded to the cent independently."
    ),
    "REPORT_GAIN_NOT_ON_PLOTTED_EPOCH_ROWS": (
        "DISCLOSED DIFFERENCE, not explained by the manifest rows: the report's "
        "gain of all epochs minus the sum of the manifest rows' gains.  The report "
        "totals use its own epoch set (see report_epoch_count against "
        "manifest_row_count), which includes the four closed sleeve rows and chained "
        "hand-offs differently; the components of that set are not in the payload."
    ),
    "ROUNDING": "Whole-cent rounding of per-epoch and per-day figures.",
    "UNRECONCILED_REMAINDER": (
        "DISCLOSED DIFFERENCE: more than whole-cent rounding remains after the named "
        "components."
    ),
}


def _epoch_trace_pnl(row: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    links = _sequence(_mapping(row.get("observation_trace")).get("links"))
    if row.get("status") != "OK" or not links:
        return None
    flow = sum((_dec(x["net_flow"], "net_flow") for x in links), Decimal(0))
    pnl = (
        _dec(links[-1]["ending_nav"], "ending_nav")
        - _dec(links[0]["base_nav"], "base_nav")
        - flow
    )
    return pnl, _dec(row.get("gain") or 0, "row gain")


def _report_epoch_keys(
    invested_capital: Mapping[str, Any],
) -> set[tuple[str, str, str]] | None:
    """(account, strategy, effective start) of the epochs the report's totals use."""

    required = invested_capital.get("required_epochs")
    if not isinstance(required, list) or not required:
        return None
    return {
        (
            str(item.get("account_id")),
            str(item.get("strategy_id")),
            str(item.get("effective_start")),
        )
        for item in required
        if isinstance(item, Mapping)
    }


def _report_crosscheck(
    flows: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    total_end_cents: int,
    report_keys: set[tuple[str, str, str]] | None = None,
) -> dict[str, Any] | None:
    """The NAV-trace contributions against the report's ledger-based net capital.

    ``contributed_minus_report_cents`` is split into named components that sum to it
    exactly; the part the payload cannot attribute is labelled as such.
    """

    if reconciliation.get("status") != "RECONCILED":
        return None
    with localcontext() as context:
        context.prec = _PRECISION
        net = _cents(
            _dec(reconciliation["derived_total_net_capital_usd"], "net capital")
        )
        gain = _cents(_dec(reconciliation["gain_of_all_epochs_usd"], "gain"))
        report_current = _cents(
            _dec(reconciliation["current_of_open_epochs_usd"], "current of open")
        )
        lines: list[dict[str, Any]] = []
        retired: list[dict[str, Any]] = []
        opening = row_gain_total = 0
        for row in _sequence(manifest.get("rows")):
            pair = _epoch_trace_pnl(row) if isinstance(row, Mapping) else None
            if pair is None:
                continue
            trace_c, gain_c = _cents(pair[0]), _cents(pair[1])
            row_gain_total += gain_c
            if (
                report_keys is not None
                and (
                    str(row.get("account_id")),
                    str(row.get("strategy_id")),
                    str(row.get("effective_start")),
                )
                not in report_keys
            ):
                retired.append(
                    {
                        "account_id": row.get("account_id"),
                        "strategy_id": row.get("strategy_id"),
                        "effective_start": row.get("effective_start"),
                        "effective_end": row.get("effective_end"),
                        "row_gain_cents": gain_c,
                    }
                )
            opening += trace_c - gain_c
            lines.append(
                {
                    "account_id": row.get("account_id"),
                    "strategy_id": row.get("strategy_id"),
                    "effective_start": row.get("effective_start"),
                    "trace_pnl_cents": trace_c,
                    "row_gain_cents": gain_c,
                    "difference_cents": trace_c - gain_c,
                }
            )
    contributed = int(flows["total_contributed_cents"])
    gap = contributed - net
    closed = total_end_cents - report_current
    epoch_set = (
        _retired_component_cents(reconciliation, retired)
        if report_keys is not None
        else gain - row_gain_total
    )
    parts = [
        ("CLOSED_EPOCH_VALUE_AT_CUTOFF", closed, [], True),
        ("OPENING_BASIS_TRACE_VS_LEDGER", -opening, lines, True),
        (
            ("RETIRED_EPOCHS_OUTSIDE_REPORT_EPOCH_SET", epoch_set, retired, True)
            if report_keys is not None
            else ("REPORT_GAIN_NOT_ON_PLOTTED_EPOCH_ROWS", epoch_set, [], False)
        ),
    ]
    rounding = gap - sum(item[1] for item in parts)
    rounded = abs(rounding) <= len(lines)
    parts.append(
        ("ROUNDING" if rounded else "UNRECONCILED_REMAINDER", rounding, [], rounded)
    )
    return {
        "source": "capital_reconciliation",
        "report_net_capital_cents": net,
        "trace_contributed_cents": contributed,
        "contributed_minus_report_cents": gap,
        "report_gain_cents": gain,
        "trace_implied_pnl_cents": flows["implied_pnl_cents"],
        "pnl_minus_report_gain_cents": flows["implied_pnl_cents"] - gain,
        "report_epoch_count": reconciliation.get("epoch_count"),
        "manifest_row_count": len(lines),
        "retired_epoch_count": len(retired),
        "components": [
            {
                "component_id": name,
                "amount_cents": amount,
                "explained": explained,
                "note": CROSSCHECK_NOTES[name],
                "lines": detail,
            }
            for name, amount, detail, explained in parts
        ],
    }


def _since_start(readout: Mapping[str, Any], slices: Mapping[str, Any]) -> date:
    """The start the trailing table's Since-start column uses: the governed index start."""

    start = _mapping(readout.get("hwm_governed_index")).get("window_start")
    return _day(start if start is not None else slices["dates"][0], "since start")


# ---------------------------------------------------------------- verifier
#
# Separate code from the builder above: exact ``Fraction`` arithmetic, a pointwise
# origin rule and per-date lookups instead of the builder's forward fill.


def _fail(message: str) -> PerformanceTabError:
    return PerformanceTabError(f"performance_tab: {message}")


def _origin_of(entry: Any, live_start: date | None, source: str) -> str:
    """Pointwise origin rule, written separately from the builder's."""

    if source != "LIVE_PLUS_BACKTEST":
        return "LIVE"
    if isinstance(entry, list) and len(entry) > 4 and isinstance(entry[4], list):
        if "ORIGIN_DERIVED" in entry[4]:
            return "DERIVED"
        if "ORIGIN_PROVIDER_MODEL_LIVE" in entry[4]:
            return "PROVIDER_MODEL_LIVE"
    if live_start is None or not isinstance(entry, list):
        raise _fail("hybrid origin inputs are missing")
    boundary = live_start.isoformat()
    if str(entry[2]) < boundary:
        return "BACKTEST"
    return "LIVE" if str(entry[1]) >= boundary else "MIXED"


def _expect_path(
    returns: Sequence[Fraction | None],
) -> tuple[list[Fraction | None], list[tuple[int, int]]]:
    """Levels with the base 1 before each run of consecutive weeks, from scratch."""

    path: list[Fraction | None] = [None] * len(returns)
    runs: list[tuple[int, int]] = []
    level = Fraction(1)
    start = None
    for index, step in enumerate(returns):
        if step is None:
            if start is not None:
                runs.append((start, index - 1))
            start = None
            continue
        if start is None:
            start = index
            level = Fraction(1)
        level *= 1 + step
        path[index] = level
    if start is not None:
        runs.append((start, len(returns) - 1))
    return path, runs


def _check_line(
    line: Mapping[str, Any],
    returns: Sequence[Fraction | None],
    dates: Sequence[str],
    label: str,
) -> None:
    path, runs = _expect_path(returns)
    if len(line["tri"]) != len(dates):
        raise _fail(f"{label} index length differs")
    for index, expected in enumerate(path):
        got = line["tri"][index]
        if (got is None) != (expected is None):
            raise _fail(f"{label} null mask differs at {dates[index]}")
        if expected is not None and abs(Fraction(got) - expected) > Fraction(6, 10**9):
            raise _fail(f"{label} index differs at {dates[index]}")
    if [tuple(item) for item in line["segments"]] != runs:
        raise _fail(f"{label} segments differ")
    if (
        line["first_index"] != runs[0][0]
        or line["last_index"] != runs[-1][1]
        or line["first_week"] != dates[runs[0][0]]
    ):
        raise _fail(f"{label} first or last week differs")


def _check_origin(
    line: Mapping[str, Any], flags: Sequence[str | None], label: str
) -> None:
    length = len(flags)
    expanded: list[str | None] = [None] * length
    for run in line["origin"]:
        start, end, flag = run
        if not (0 <= start <= end < length):
            raise _fail("origin run outside the grid")
        for index in range(start, end + 1):
            if expanded[index] is not None:
                raise _fail("origin runs overlap")
            expanded[index] = flag
    if expanded != list(flags):
        raise _fail(f"{label} origin differs")


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
    inputs: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], str]] = {}
    for row in rows:
        picked = _mode_inputs(row, mode)
        if picked is None:
            raise _fail(f"{mode} is available but {row.get('series_id')} has no values")
        inputs[str(row["series_id"])] = picked
    last_week = max(max(item[0]) for item in inputs.values())
    if any(last_week not in item[0] for item in inputs.values()):
        raise _fail(f"{mode} is available but a strategy lacks the last week")
    bench_values: dict[str, Mapping[str, Any]] = {}
    for bench_id in ("market:SPY", "market:SPXL"):
        bench_row = _benchmark_row(weekly, bench_id)
        if bench_row is not None and _mapping(bench_row.get("values")):
            bench_values[bench_id] = _mapping(bench_row["values"])
    starts = {sid: min(item[0]) for sid, item in inputs.items()}
    union_firsts = {**starts, **{sid: min(vals) for sid, vals in bench_values.items()}}
    union_start = min(union_firsts.values())
    if block["union_start_date"] != union_start or block["union_start_set_by"] != min(
        k for k, v in union_firsts.items() if v == union_start
    ):
        raise _fail(f"{mode} union start differs")
    dates = list(block["dates"])
    expected_dates = [
        day.isoformat()
        for day in _mondays(
            date.fromisoformat(union_start), date.fromisoformat(last_week)
        )
    ]
    if dates != expected_dates:
        raise _fail(f"{mode} dates are not the consecutive Mondays of the union")
    length = len(dates)
    strategy_returns: dict[str, list[Fraction | None]] = {}
    strategy_flags: dict[str, list[str | None]] = {}
    by_id = {str(line["series_id"]): line for line in block["lines"]}
    if len(by_id) != len(block["lines"]):
        raise _fail(f"{mode} lines are duplicated")
    expected_gaps = []
    for row in rows:
        series_id = str(row["series_id"])
        values, meta, source = inputs[series_id]
        line = by_id.get(series_id)
        if line is None or line["source_mode"] != source or line["kind"] != "STRATEGY":
            raise _fail(f"{mode} {series_id} source or kind differs")
        live_start = _live_start(row)
        returns = [
            Fraction(str(values[key])) if key in values else None for key in dates
        ]
        flags = [
            _origin_of(meta.get(key), live_start, source) if key in values else None
            for key in dates
        ]
        _check_line(line, returns, dates, f"{mode} {series_id}")
        _check_origin(line, flags, f"{mode} {series_id}")
        strategy_returns[series_id] = returns
        strategy_flags[series_id] = flags
        first = next(i for i, v in enumerate(returns) if v is not None)
        absent = [dates[i] for i in range(first, length) if returns[i] is None]
        if absent:
            expected_gaps.append((series_id, len(absent), absent[0], absent[-1]))
    published_gaps = sorted(
        (
            g["series_id"],
            g["missing_weeks"],
            g["first_missing_week"],
            g["last_missing_week"],
        )
        for g in block["gaps_before_common_start"]
    )
    if published_gaps != sorted(expected_gaps):
        raise _fail(f"{mode} gaps_before_common_start differ")
    # The anchor: the earliest week from which no strategy is ever absent again.
    anchor = 0
    for series_id, returns in strategy_returns.items():
        back = length - 1
        while back >= 0 and returns[back] is not None:
            back -= 1
        anchor = max(anchor, back + 1)
    setters = [
        sid
        for sid, returns in strategy_returns.items()
        if all(r is not None for r in returns[anchor:])
        and (anchor == 0 or returns[anchor - 1] is None)
    ]
    first_common = max(starts.values())
    if (
        block["group_anchor_index"] != anchor
        or block["group_anchor_date"] != dates[anchor]
        or block["common_start_date"] != dates[anchor]
        or block["group_anchor_set_by"] != min(setters)
        or block["common_start_set_by"] != min(setters)
        or block["first_observation_common_start"] != first_common
        or block["first_observation_set_by"]
        != max(sorted(starts), key=lambda key: starts[key])
    ):
        raise _fail(f"{mode} group anchor differs")
    ids = list(strategy_returns)
    count = len(ids)
    if count == 0:
        raise _fail(f"{mode} has no strategy")
    ew = by_id.get(EW_SERIES_ID)
    if (
        ew is None
        or ew["definition"] != EW_DEFINITION
        or ew["kind"] != "PORTFOLIO"
        or ew["member_count"] != count
        or sorted(ew["member_series_ids"]) != sorted(ids)
    ):
        raise _fail(f"{mode} equal-weight line differs")
    ew_returns: list[Fraction | None] = [
        sum((cast(Fraction, strategy_returns[sid][i]) for sid in ids), Fraction(0))
        / count
        if i >= anchor
        else None
        for i in range(length)
    ]
    _check_line(ew, ew_returns, dates, f"{mode} equal-weight")
    ew_flags: list[str | None] = []
    for i in range(length):
        if i < anchor:
            ew_flags.append(None)
            continue
        members = {strategy_flags[sid][i] for sid in ids}
        ew_flags.append(next(iter(members)) if len(members) == 1 else "MIXED")
    _check_origin(ew, ew_flags, f"{mode} equal-weight")
    avail = by_id.get(EW_AVAILABLE_SERIES_ID)
    if avail is None or avail["definition"] != EW_AVAILABLE_DEFINITION:
        raise _fail(f"{mode} available-strategies equal-weight line differs")
    avail_returns: list[Fraction | None] = []
    avail_flags: list[str | None] = []
    counts: list[int | None] = []
    for i in range(length):
        present = [sid for sid in ids if strategy_returns[sid][i] is not None]
        if not present:
            avail_returns.append(None)
            avail_flags.append(None)
            counts.append(None)
            continue
        avail_returns.append(
            sum(
                (cast(Fraction, strategy_returns[sid][i]) for sid in present),
                Fraction(0),
            )
            / len(present)
        )
        members = {strategy_flags[sid][i] for sid in present}
        avail_flags.append(next(iter(members)) if len(members) == 1 else "MIXED")
        counts.append(len(present))
    _check_line(avail, avail_returns, dates, f"{mode} available equal-weight")
    _check_origin(avail, avail_flags, f"{mode} available equal-weight")
    if avail["member_counts"] != counts or avail["member_count"] is not None:
        raise _fail(f"{mode} member counts differ")
    expected_gap_ids = []
    published_ids = {sid for sid, line in by_id.items() if line["kind"] == "BENCHMARK"}
    for bench_id, values in bench_values.items():
        returns = [
            Fraction(str(values[key])) if key in values else None for key in dates
        ]
        if all(r is None for r in returns):
            expected_gap_ids.append(bench_id)
            if bench_id in published_ids:
                raise _fail(
                    f"{mode} {bench_id} is published without a week in the grid"
                )
            continue
        line = by_id.get(bench_id)
        if line is None or "origin" in line:
            raise _fail(f"{mode} {bench_id} line is absent or has an origin")
        _check_line(line, returns, dates, f"{mode} {bench_id}")
    if published_ids != {b for b in bench_values if b not in expected_gap_ids}:
        raise _fail(f"{mode} benchmark lines differ from the derivable pair")
    if sorted(g["series_id"] for g in block["benchmarks_unavailable"]) != sorted(
        expected_gap_ids
    ):
        raise _fail(f"{mode} benchmark gaps are not named")
    if len(by_id) != len(ids) + 2 + len(published_ids):
        raise _fail(f"{mode} line count differs")
    observed_ends = []
    for key in dates:
        candidates = []
        for series_id in ids:
            entry = inputs[series_id][1].get(key)
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
    if block["base_point"] != "START_OF_EACH_LINE_FIRST_WEEK":
        raise _fail(f"{mode} base_point differs")


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

    expected_links = _expected_sleeve_links(panel)
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
        expected_link = expected_links.get(strategy)
        if (row["parent_strategy_id"], row["sleeve_id"]) != (
            (None, None) if expected_link is None else expected_link
        ):
            raise _fail(f"{strategy} sleeve identity differs from the panel's")
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
    _verify_flows(block["flows"], manifest, traces, days, totals, cents)
    cross = block["flows"]["report_crosscheck"]
    recon = _mapping(panel.get("capital_reconciliation"))
    if recon.get("status") != "RECONCILED":
        if cross is not None:
            raise _fail("flows cross-check is published without a reconciled report")
    else:
        net = cents(Fraction(str(recon["derived_total_net_capital_usd"])))
        gain = cents(Fraction(str(recon["gain_of_all_epochs_usd"])))
        current_open = cents(Fraction(str(recon["current_of_open_epochs_usd"])))
        mine = block["flows"]["total_contributed_cents"]
        opening_total = 0
        row_gains = 0
        expected_lines = []
        for trace in traces:
            account, strategy, first, _last, parsed = trace
            row = next(
                r
                for r in _sequence(manifest.get("rows"))
                if isinstance(r, Mapping)
                and r.get("status") == "OK"
                and r.get("account_id") == account
                and r.get("strategy_id") == strategy
                and _first_link_start(r) == first.isoformat()
            )
            trace_pnl = (
                parsed[-1][3]
                - parsed[0][2]
                - sum((link[4] for link in parsed), Fraction(0))
            )
            trace_c = cents(trace_pnl)
            gain_c = cents(Fraction(str(row.get("gain") or 0)))
            row_gains += gain_c
            opening_total += trace_c - gain_c
            expected_lines.append(
                {
                    "account_id": row.get("account_id"),
                    "strategy_id": row.get("strategy_id"),
                    "effective_start": row.get("effective_start"),
                    "trace_pnl_cents": trace_c,
                    "row_gain_cents": gain_c,
                    "difference_cents": trace_c - gain_c,
                }
            )
        report_keys = _report_epoch_keys(_mapping(panel.get("invested_capital")))
        expected_retired: list[dict[str, Any]] = []
        if report_keys is not None:
            for trace in traces:
                account, strategy, first, _last, _parsed = trace
                row = next(
                    r
                    for r in _sequence(manifest.get("rows"))
                    if isinstance(r, Mapping)
                    and r.get("status") == "OK"
                    and r.get("account_id") == account
                    and r.get("strategy_id") == strategy
                    and _first_link_start(r) == first.isoformat()
                )
                if (
                    account,
                    strategy,
                    str(row.get("effective_start")),
                ) not in report_keys:
                    expected_retired.append(
                        {
                            "account_id": row.get("account_id"),
                            "strategy_id": row.get("strategy_id"),
                            "effective_start": row.get("effective_start"),
                            "effective_end": row.get("effective_end"),
                            "row_gain_cents": cents(
                                Fraction(str(row.get("gain") or 0))
                            ),
                        }
                    )
        gap = mine - net
        amounts = [
            totals[-1] - current_open,
            -opening_total,
            (
                _retired_component_cents(recon, expected_retired)
                if report_keys is not None
                else gain - row_gains
            ),
        ]
        amounts.append(gap - sum(amounts))
        rounded = abs(amounts[3]) <= len(traces)
        components = (cross or {}).get("components") or []
        if (
            cross is None
            or [c["component_id"] for c in components]
            != [
                "CLOSED_EPOCH_VALUE_AT_CUTOFF",
                "OPENING_BASIS_TRACE_VS_LEDGER",
                (
                    "RETIRED_EPOCHS_OUTSIDE_REPORT_EPOCH_SET"
                    if report_keys is not None
                    else "REPORT_GAIN_NOT_ON_PLOTTED_EPOCH_ROWS"
                ),
                "ROUNDING" if rounded else "UNRECONCILED_REMAINDER",
            ]
            or [c["amount_cents"] for c in components] != amounts
            or sorted(
                components[1]["lines"],
                key=lambda x: (x["account_id"], x["strategy_id"], x["effective_start"]),
            )
            != sorted(
                expected_lines,
                key=lambda x: (x["account_id"], x["strategy_id"], x["effective_start"]),
            )
            or [c["explained"] for c in components]
            != [True, True, report_keys is not None, rounded]
            or sorted(components[2]["lines"], key=lambda x: str(x))
            != sorted(expected_retired, key=lambda x: str(x))
            or any(c["note"] != CROSSCHECK_NOTES[c["component_id"]] for c in components)
            or {k: v for k, v in cross.items() if k != "components"}
            != {
                "source": "capital_reconciliation",
                "report_net_capital_cents": net,
                "trace_contributed_cents": mine,
                "contributed_minus_report_cents": gap,
                "report_gain_cents": gain,
                "trace_implied_pnl_cents": block["flows"]["implied_pnl_cents"],
                "pnl_minus_report_gain_cents": block["flows"]["implied_pnl_cents"]
                - gain,
                "report_epoch_count": recon.get("epoch_count"),
                "manifest_row_count": len(traces),
                "retired_epoch_count": len(expected_retired),
            }
        ):
            raise _fail("flows cross-check differs")


def _verify_flows(
    flows: Mapping[str, Any],
    manifest: Mapping[str, Any],
    traces: Sequence[Any],
    days: Sequence[date],
    totals: Sequence[int],
    cents: Any,
) -> None:
    """Re-derive F(d) from the manifest's links and legs, written separately."""

    position = {day: i for i, day in enumerate(days)}
    size = len(days)
    legs_of: dict[tuple[str, str, date], Sequence[Any]] = {}
    for row in _sequence(manifest.get("rows")):
        if not isinstance(row, Mapping) or row.get("status") != "OK":
            continue
        if not _sequence(_mapping(row.get("observation_trace")).get("links")):
            continue
        legs_of[
            (
                str(row["account_id"]),
                str(row["strategy_id"]),
                date.fromisoformat(_first_link_start(row)),
            )
        ] = _sequence(row.get("performance_flow_legs"))
    link_flow = [Fraction(0)] * size
    opening = [Fraction(0)] * size
    closing = [Fraction(0)] * size
    internal_in = internal_out = Fraction(0)
    for trace in traces:
        account, strategy, first, last, parsed = trace
        handoff = any(
            other is not trace and other[0] == account and other[3] == first
            for other in traces
        )
        for link in parsed:
            if link[1] in position:
                link_flow[position[link[1]]] += link[4]
        opened = parsed[0][1] if handoff else first
        if opened in position:
            opening[position[opened]] += parsed[0][2]
        if last + timedelta(days=1) in position:
            closing[position[last + timedelta(days=1)]] += parsed[-1][3]
        for leg in legs_of.get((account, strategy, first), []):
            kind = _mapping(leg).get("class")
            amount = Fraction(str(_mapping(leg).get("amount")))
            if kind == "INTERNAL_IN":
                internal_in += amount
            elif kind == "INTERNAL_OUT":
                internal_out += amount
    expected = {
        "link_net_flow_cents": [cents(v) for v in link_flow],
        "opening_cents": [cents(v) for v in opening],
        "closing_cents": [cents(v) for v in closing],
        "net_flow_cents": [
            cents(link_flow[i] + opening[i] - closing[i]) for i in range(size)
        ],
    }
    for key, values in expected.items():
        if list(flows[key]) != values:
            raise _fail(f"flows {key} differ")
    contributed = sum(expected["net_flow_cents"])
    if (
        flows["total_contributed_cents"] != contributed
        or flows["implied_pnl_cents"] != totals[-1] - contributed
        or flows["definition"] != SLICE_PERIMETER_FLOW_DEFINITION
        or flows["residual_treatment"] != SLICE_RESIDUAL_TREATMENT
        or flows["unit"] != "USD_CENTS"
    ):
        raise _fail("flows totals or definitions differ")
    cancel = flows["internal_cancellation"]
    net_internal = internal_in - internal_out
    if (
        cancel["internal_in_cents"] != cents(internal_in)
        or cancel["internal_out_cents"] != cents(internal_out)
        or cancel["net_cents"] != cents(net_internal)
        or cancel["status"]
        != (
            "CANCELS_EXACTLY"
            if net_internal == 0
            else "NET_INTERNAL_FLOW_CROSSES_PERIMETER"
        )
    ):
        raise _fail("flows internal cancellation differs")


def _verify_slice_benchmarks(
    block: Mapping[str, Any],
    weekly: Mapping[str, Any],
    readout: Mapping[str, Any],
    daily_benchmarks: Mapping[str, Any] | None,
    allow_weekly_fallback: bool = False,
) -> None:
    """Direct-sum re-derivation of the flow-matched dollars, one benchmark at a time."""

    published = _sequence(block.get("benchmarks"))
    if [b.get("series_id") for b in published] != [b[0] for b in _BENCHMARKS]:
        raise _fail("slice benchmarks differ from SPY and SPXL")
    days = [date.fromisoformat(item) for item in block["dates"]]
    flows = list(block["flows"]["net_flow_cents"])
    totals = block["reconciliation"]["total_cents"]
    capital = block["reconciliation"]["systematic_capital_cents"]
    start_text = _mapping(readout.get("hwm_governed_index")).get("window_start")
    since = date.fromisoformat(
        str(start_text if start_text is not None else block["dates"][0])
    )
    for entry, (series_id, ticker, _since, _note) in zip(published, _BENCHMARKS):
        bench_row = _benchmark_row(weekly, series_id)
        if entry["ticker"] != ticker or entry["method"] != "FLOW_MATCHED":
            raise _fail(f"{series_id} slice benchmark identity differs")
        if bench_row is None:
            if entry["status"] != "UNAVAILABLE" or not entry["reason"]:
                raise _fail(f"{series_id} slice benchmark must be unavailable")
            continue
        weekly_levels = _weekly_step_levels(bench_row)
        if isinstance(weekly_levels, str):
            if entry["status"] != "UNAVAILABLE" or entry["reason"] != weekly_levels:
                raise _fail(f"{series_id} slice benchmark reason differs")
            continue
        daily, _why = _daily_usable(
            _mapping(daily_benchmarks).get(series_id) if daily_benchmarks else None,
            weekly_levels[-1][0],
        )
        if daily is not None:
            levels = _daily_levels(_mapping(daily["returns"]))
            basis, timing = "DAILY_ADJUSTED_CLOSE", "FLOW_DATE_CLOSE"
        elif allow_weekly_fallback:
            levels, basis, timing = (
                weekly_levels,
                "WEEKLY_TOTAL_RETURN_INDEX",
                "FLOW_WEEK_START",
            )
        else:
            if (
                entry["status"] != "UNAVAILABLE"
                or entry["reason"] != f"DAILY_SERIES_REQUIRED:{_why}"
            ):
                raise _fail(f"{series_id} slice benchmark needs the daily series")
            continue
        with localcontext() as context:
            context.prec = _PRECISION + 10
            level_on: list[Decimal | None] = []
            for day in days:
                known = [lvl for end, lvl in levels if end <= day]
                level_on.append(known[-1] if known else None)
            flow_days = [i for i, v in enumerate(flows) if v]
            if not flow_days:
                if entry["status"] != "UNAVAILABLE":
                    raise _fail(f"{series_id} slice benchmark must be unavailable")
                continue
            if any(level_on[i] is None for i in range(flow_days[0], len(days))):
                if entry["status"] != "UNAVAILABLE":
                    raise _fail(f"{series_id} slice benchmark must be unavailable")
                continue
            if entry["status"] != "AVAILABLE":
                raise _fail(f"{series_id} slice benchmark is unavailable but derivable")
            if entry["tri_basis"] != basis or entry["flow_timing"] != timing:
                raise _fail(f"{series_id} slice benchmark basis differs")
            for index, level in enumerate(level_on):
                got = entry["tri"][index]
                if (got is None) != (level is None):
                    raise _fail(f"{series_id} slice benchmark tri mask differs")
                if level is not None and abs(Decimal(str(got)) - level) > Decimal(
                    "0.000000006"
                ):
                    raise _fail(f"{series_id} slice benchmark tri differs")
            for t in range(len(days)):
                value = sum(
                    (
                        Decimal(flows[d])
                        * cast(Decimal, level_on[t])
                        / cast(Decimal, level_on[d])
                        for d in flow_days
                        if d <= t
                    ),
                    Decimal(0),
                )
                if abs(Decimal(entry["nav_cents"][t]) - value) > Decimal("1.01"):
                    raise _fail(f"{series_id} flow-matched value differs on {days[t]}")
            if (
                entry["end_value_cents"] != entry["nav_cents"][-1]
                or entry["portfolio_minus_benchmark_cents"]
                != totals[-1] - entry["nav_cents"][-1]
                or entry["systematic_capital_minus_benchmark_cents"]
                != (None if capital is None else capital - entry["nav_cents"][-1])
                or entry["trailing_since_start_date"] != since.isoformat()
            ):
                raise _fail(f"{series_id} slice benchmark totals differ")
            end = days[-1]
            for label, spec in TRAILING_WINDOWS:
                if spec == "start":
                    begin = since
                elif spec is None:
                    begin = date(end.year - 1, 12, 31)
                else:
                    begin = end - timedelta(days=int(spec))
                expected_value: Decimal | None = None
                if begin >= days[0]:
                    base = level_on[(begin - days[0]).days]
                    if base is not None and base > 0:
                        expected_value = cast(Decimal, level_on[-1]) / base - 1
                got_text = entry["trailing"][label]
                if (got_text is None) != (expected_value is None) or (
                    expected_value is not None
                    and abs(Decimal(got_text) - expected_value) > Decimal("1e-11")
                ):
                    raise _fail(f"{series_id} trailing {label} differs")


def _expected_sleeve_links(panel: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """The sleeve identity of each strategy, read again from the panel's rows."""

    expected: dict[str, tuple[str, str]] = {}
    for raw in _sequence(panel.get("strategies")):
        if not isinstance(raw, Mapping):
            continue
        sleeve, parent = raw.get("sleeve_id"), raw.get("parent_strategy_id")
        if sleeve is None and parent is None:
            continue
        if not (isinstance(sleeve, str) and isinstance(parent, str)):
            raise _fail("a sleeve strategy lacks its parent or id")
        if str(raw.get("strategy_id")) in expected:
            raise _fail("a sleeve strategy is listed twice")
        expected[str(raw.get("strategy_id"))] = (parent, sleeve)
    return expected


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


def verify_performance_tab(
    block: Mapping[str, Any],
    panel: Mapping[str, Any],
    daily_benchmarks: Mapping[str, Any] | None = None,
    allow_weekly_fallback: bool = False,
) -> None:
    """Re-derive the block from the panel inputs; raise on any difference."""

    if (
        block.get("schema_version") != PERFORMANCE_TAB_SCHEMA_VERSION
        or block.get("scope") != "SYSTEMATIC_EX_RESERVE"
    ):
        raise _fail("schema_version or scope differs")
    weekly = _mapping(panel.get("weekly_returns"))
    if daily_benchmarks is None:
        daily_benchmarks = _daily_from_weekly(weekly)
    if block.get("as_of") != weekly.get("as_of_date"):
        raise _fail("as_of differs from the weekly returns")
    modes = _mapping(block.get("modes"))
    if sorted(modes) != ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"]:
        raise _fail("modes differ")
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        _verify_mode(_mapping(modes[mode]), weekly, mode)
    scopes = _return_scope_labels(panel)
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        for line in _sequence(_mapping(modes[mode]).get("lines")):
            expected = scopes.get(str(line.get("series_id")))
            if expected is not None:
                expected = _qualified_label(
                    expected, line, _mapping(modes[mode]).get("dates")
                )
            if isinstance(line, Mapping) and line.get("return_scope_label") != expected:
                raise _fail(
                    f"{mode} {line.get('series_id')} return scope label differs"
                )
    benchmarks = _sequence(block.get("benchmarks"))
    expected_ids = ["market:SPY", "market:SPXL"]
    if [b.get("series_id") for b in benchmarks] != expected_ids:
        raise _fail("benchmarks differ from SPY and SPXL")
    for benchmark in benchmarks:
        row = _benchmark_row(weekly, str(benchmark["series_id"]))
        if benchmark.get("substitute") is not None:
            raise _fail("a benchmark is substituted")
        daily_input = (
            _mapping(daily_benchmarks).get(str(benchmark["series_id"]))
            if daily_benchmarks
            else None
        )
        if row is not None:
            if benchmark.get("status") != "AVAILABLE" or benchmark.get("required_pull"):
                raise _fail("an admitted benchmark must be AVAILABLE with no pull")
            weeks = sorted(_mapping(row.get("values")))
            levels = _weekly_step_levels(row)
            usable, why = _daily_usable(
                daily_input, None if isinstance(levels, str) else levels[-1][0]
            )
            returns = sorted(_mapping(usable.get("returns"))) if usable else []
            daily_expected = {
                "status": "AVAILABLE" if usable else "UNAVAILABLE",
                "source_id": str(usable.get("source_id")) if usable else None,
                "first_date": returns[0] if returns else None,
                "last_date": returns[-1] if returns else None,
                "reason": why,
            }
            if (
                benchmark.get("first_week") != (weeks[0] if weeks else None)
                or benchmark.get("last_week") != (weeks[-1] if weeks else None)
                or benchmark.get("daily") != daily_expected
            ):
                raise _fail("benchmark first/last week or daily status differs")
            continue
        pull = _mapping(benchmark.get("required_pull"))
        if (
            benchmark.get("status") != "UNAVAILABLE"
            or pull.get("source_id") != "yahoo-adjusted-etf-daily"
            or pull.get("to_date") != "2026-09-17"
            or benchmark.get("first_week") is not None
            or _mapping(benchmark.get("daily")).get("status") != "UNAVAILABLE"
        ):
            raise _fail("benchmark must be an explicit UNAVAILABLE entry with its pull")
    _verify_slices(_mapping(block.get("slices")), panel)
    slices_block = _mapping(block.get("slices"))
    if slices_block.get("status") == "AVAILABLE":
        _verify_slice_benchmarks(
            slices_block,
            weekly,
            _mapping(block.get("readout")),
            daily_benchmarks,
            allow_weekly_fallback,
        )
    _verify_readout(_mapping(block.get("readout")), panel)

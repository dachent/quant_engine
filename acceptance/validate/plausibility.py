"""Owner-eyes plausibility rules on the REAL rendered panel payload (#912, Q1 of the
decision-readiness review).

Every one of the owner's eight finds was visible in the payload with real data and in no
diff: a stale hand-audit column, an underwriting score of 5 or 6 out of 7, a "conservative"
loss fraction below the drawdown already observed, betas of 6 and negative risk-free rates,
a week called PARTIAL on its last trading day, a Health tab that cannot turn green.  The
validator re-derived each from the same bytes, so a faithful reproduction of a bad input
passed.  These rules bound the OUTPUT instead: ranges, calendar truth, equalities between
surfaces, dates against the cutoff, and claims that must be able to fail.

Pure functions over the decoded ``dashboard_data.json``; nothing here reads a file.  A rule
yields ``Hit`` rows; a rule that yields nothing passes.  ``RULES`` is the full table (id,
the owner finds it catches, what it asserts).  The command line is tools/check_plausibility.py;
the validator calls ``evaluate`` through the ``plausibility.owner_eyes`` check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import math
import re
from typing import Any

# --- the owner's eight finds (review Q1) ---------------------------------------------------
F1_STALE_HAND_AUDIT = 1
F2_UNDERWRITING_GAP = 2
F3_CONSERVATIVE_CLAIM = 3
F4_FACTOR_TAB = 4
F5_CHARTS = 5
F6_STATUS_TOKEN = 6
F7_HEALTH_UNREACHABLE = 7
F8_UNCONSUMED_CONTRACT = 8

# --- bounds (named so a change is a reviewed edit, not a drift) ----------------------------
ALLOWED_AGE_DAYS = (
    45  # default allowed age of a dated non-code input against the cutoff
)
POLICY_ALLOWED_AGE_DAYS = 400  # owner-approved policy constants and contracts
FACTOR_STALE_DAYS = 10  # fit window / factor file may end this far before the cutoff
BETA_BOUND = 3.0
ALPHA_BOUND = 0.5
RISK_FREE_DAILY_MAX = 0.002  # 0.2 %/day = 50 %/yr: far above any real rate
PERCENT_CHIP_MAX = 100.0  # 10 000 %
TRACKING_ERROR_MAX = 2.0
GROSS_TO_EQUITY_MAX = 10.0
WEIGHT_SUM_TOLERANCE = 0.01
ANNUALISATION_REL_TOLERANCE = 1e-6
AXIS_RATIO_MAX = 1000.0  # max/min of a total-return index on one axis
FUTURE_DATE_TOLERANCE_DAYS = 14

# NYSE sessions that fall on a Friday (the last trading day of that week is the Thursday).
# Calendar truth for the week-status rule; extend when the horizon passes.
NYSE_FRIDAY_HOLIDAYS = frozenset(
    date.fromisoformat(d)
    for d in (
        "2026-04-03",
        "2026-07-03",
        "2026-12-25",
        "2027-01-01",
        "2027-03-26",
        "2027-06-18",
        "2027-12-24",
    )
)

_POLICY_ARTIFACT = re.compile(r"(policy|contract|cluster|crosswalk)", re.I)
_QUALITATIVE = re.compile(r"\b(conservative|complete|current)\b", re.I)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISSUE_REF = re.compile(r"(#\s?\d{2,5}|\bissue\s+\d{2,5}\b)", re.I)


@dataclass(frozen=True)
class Hit:
    rule: str
    finds: tuple[int, ...]
    path: str
    observed: Any
    expected: str
    message: str

    def as_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "finds": list(self.finds),
            "path": self.path,
            "observed": self.observed,
            "expected": self.expected,
            "message": self.message,
        }


@dataclass(frozen=True)
class Rule:
    rule_id: str
    finds: tuple[int, ...]
    klass: str  # "BLOCKER" | "NOT_READY"
    text: str
    run: Callable[[Panel], Iterator[Hit]]


class Panel:
    """The decoded payload plus lazily built views (one walk answers every key lookup)."""

    def __init__(self, data: Mapping[str, Any], cutoff: date | None = None) -> None:
        self.data = data
        meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
        self.cutoff = cutoff or to_date(
            (meta or {}).get("portfolio_as_of") or data.get("as_of_date")
        )
        self._index: dict[str, list[tuple[str, Any]]] | None = None

    def keyed(self, *names: str) -> Iterator[tuple[str, str, Any]]:
        """Every (key, path, value) for the given key names, anywhere in the payload."""

        if self._index is None:
            self._index = {}
            _walk(self.data, "$", self._index)
        for name in names:
            for path, value in self._index.get(name, ()):
                yield name, path, value


_INTERESTING = (
    "beta",
    "risk_free",
    "r_squared",
    "predicted_r_squared",
    "adjusted_r_squared",
    "net_annualized",
    "drawdown",
    "current_drawdown",
    "maximum_drawdown",
    "max_drawdown",
    "account_live_max_drawdown",
    "hwm_current_drawdown",
    "hwm_max_drawdown",
    "target_max_drawdown",
    "target_current_drawdown",
    "correlation",
    "decision_correlation",
    "observed_correlation",
    "stress_pre_psd",
    "tracking_error_annualized",
    "annualized_tracking_error",
    "gross_to_equity_ratio",
    "cumulative_return",
    "target_cumulative_return",
    "account_cumulative_return",
    "ytd_twr",
    "account_live_cagr",
    "conservative_loss_fraction",
    "adverse_loss_fraction",
    "alignment_ratio",
    "factor_staleness_days",
)


def _walk(node: Any, path: str, out: dict[str, list[tuple[str, Any]]]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in _INTERESTING and not isinstance(value, (Mapping, list)):
                out.setdefault(key, []).append((f"{path}.{key}", value))
            if isinstance(value, (Mapping, list)):
                _walk(value, f"{path}.{key}", out)
    elif isinstance(node, list):
        if node and not isinstance(node[0], (Mapping, list)):
            return  # a numeric or text series: not walked element by element
        for i, value in enumerate(node):
            if isinstance(value, (Mapping, list)):
                _walk(value, f"{path}[{i}]", out)


def num(value: Any) -> float | None:
    """A finite float from a number or a numeric string; None for anything else."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _DATE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _get(node: Any, *keys: Any) -> Any:
    for key in keys:
        if isinstance(node, Mapping):
            node = node.get(key)
        elif (
            isinstance(node, list)
            and isinstance(key, int)
            and -len(node) <= key < len(node)
        ):
            node = node[key]
        else:
            return None
    return node


def _rows(node: Any) -> list[Mapping[str, Any]]:
    return [r for r in node if isinstance(r, Mapping)] if isinstance(node, list) else []


MODES = ("OFF", "WARN", "BLOCK")

RULES: list[Rule] = []


def rule(
    rule_id: str, finds: tuple[int, ...], text: str, klass: str = "BLOCKER"
) -> Callable[[Callable[[Panel], Iterator[Hit]]], Callable[[Panel], Iterator[Hit]]]:
    def register(
        fn: Callable[[Panel], Iterator[Hit]],
    ) -> Callable[[Panel], Iterator[Hit]]:
        RULES.append(Rule(rule_id, finds, klass, text, fn))
        return fn

    return register


def _hit(rule_id: str, path: str, observed: Any, expected: str, message: str) -> Hit:
    finds = next(r.finds for r in RULES if r.rule_id == rule_id)
    return Hit(rule_id, finds, path, observed, expected, message)


def _lens_series(panel: Panel) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every factor-series object (top-level and per lens/mode/surface), with its path."""

    for i, s in enumerate(_rows(_get(panel.data, "factor_analysis", "series"))):
        yield f"$.factor_analysis.series[{i}]", s
    modes = _get(panel.data, "decomposition_lenses", "modes")
    for mode, lenses in modes.items() if isinstance(modes, Mapping) else ():
        for li, lens in enumerate(_rows(lenses)):
            base = f"$.decomposition_lenses.modes.{mode}[{li}]"
            for i, s in enumerate(_rows(lens.get("series"))):
                yield f"{base}.series[{i}]", s
            surfaces = lens.get("surfaces")
            for name, surface in (
                surfaces.items() if isinstance(surfaces, Mapping) else ()
            ):
                for i, s in enumerate(_rows(_get(surface, "series"))):
                    yield f"{base}.surfaces.{name}.series[{i}]", s


# ============================================================================================
# Range rules (find 4 and the general chip/axis class)
# ============================================================================================


@rule(
    "PL01",
    (F4_FACTOR_TAB,),
    "absolute beta at most 3 unless the series is labelled levered",
)
def pl01(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        levered = any(
            "LEVER" in str(s.get(k, "")).upper()
            for k in ("return_basis", "basis_label", "label")
        )
        for i, e in enumerate(_rows(s.get("exposures"))):
            b = num(e.get("beta"))
            if b is not None and abs(b) > BETA_BOUND and not levered:
                yield _hit(
                    "PL01",
                    f"{path}.exposures[{i}].beta",
                    b,
                    f"|beta| <= {BETA_BOUND:g}",
                    "an exposure beyond the bound is shown without a levered-series label",
                )


@rule(
    "PL02", (F4_FACTOR_TAB,), "risk-free daily rate is non-negative and at most 0.2 %"
)
def pl02(panel: Panel) -> Iterator[Hit]:
    seen = 0
    for _, path, value in panel.keyed("risk_free"):
        v = num(value)
        if v is not None and not (0.0 <= v <= RISK_FREE_DAILY_MAX):
            seen += 1
            if seen <= 20:
                yield _hit(
                    "PL02",
                    path,
                    v,
                    f"0 <= risk_free <= {RISK_FREE_DAILY_MAX}",
                    "a negative or implausible risk-free rate feeds the excess returns",
                )


@rule(
    "PL03",
    (F4_FACTOR_TAB,),
    "no negative R2 is published (train, holdout, adjusted, predicted)",
)
def pl03(panel: Panel) -> Iterator[Hit]:
    for name, path, value in panel.keyed(
        "r_squared", "predicted_r_squared", "adjusted_r_squared"
    ):
        v = num(value)
        if v is not None and v < 0:
            yield _hit(
                "PL03",
                path,
                v,
                "R2 >= 0 or the fit is not published",
                f"{name} below zero: the fit does worse than its own mean and was published",
            )


@rule(
    "PL04",
    (F4_FACTOR_TAB,),
    "absolute annual alpha at most 50 % unless labelled not-a-headline",
)
def pl04(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        a = num(_get(s, "alpha", "net_annualized"))
        if a is not None and abs(a) > ALPHA_BOUND:
            status = str(_get(s, "alpha_context", "headline_status") or "")
            if not status.startswith("NOT_A_HEADLINE"):
                yield _hit(
                    "PL04",
                    f"{path}.alpha.net_annualized",
                    a,
                    f"|alpha| <= {ALPHA_BOUND:g} or headline_status NOT_A_HEADLINE*",
                    "an extreme alpha is a headline",
                )


@rule(
    "PL05",
    (F4_FACTOR_TAB, F1_STALE_HAND_AUDIT),
    "fit window and factor file end within 10 days of the cutoff or are labelled stale",
)
def pl05(panel: Panel) -> Iterator[Hit]:
    def contexts() -> Iterator[tuple[str, Mapping[str, Any]]]:
        for path, s in _lens_series(panel):
            yield f"{path}.alpha_context", _get(s, "alpha_context") or {}
        for i, row in enumerate(
            _rows(_get(panel.data, "strategy_underwriting", "rows"))
        ):
            lh = row.get("long_history")
            for key in ("hybrid_net_alpha_context", "live_net_alpha_context"):
                ctx = _get(lh, key)
                if isinstance(ctx, Mapping):
                    yield f"$.strategy_underwriting.rows[{i}].long_history.{key}", ctx

    for path, ctx in contexts():
        stale = num(ctx.get("factor_staleness_days"))
        end = to_date(ctx.get("window_end"))
        lag = None
        if panel.cutoff and end:
            lag = (panel.cutoff - end).days
        worst = max([x for x in (stale, lag) if x is not None], default=None)
        label = f"{ctx.get('basis_status', '')} {ctx.get('basis_label', '')}".upper()
        if worst is not None and worst > FACTOR_STALE_DAYS and "STALE" not in label:
            yield _hit(
                "PL05",
                path,
                {"factor_staleness_days": stale, "window_end_lag_days": lag},
                f"<= {FACTOR_STALE_DAYS} days or labelled stale",
                "a lens fitted on old factors is shown as current",
            )


@rule(
    "PL06",
    (F4_FACTOR_TAB, F5_CHARTS),
    "no annualized headline from a window under 365 days",
)
def pl06(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        ctx = _get(s, "alpha_context") or {}
        days = num(ctx.get("window_days"))
        status = str(ctx.get("headline_status") or "")
        if (
            days is not None
            and days < 365
            and num(_get(s, "alpha", "net_annualized")) is not None
            and not status.startswith("NOT_A_HEADLINE")
        ):
            yield _hit(
                "PL06",
                f"{path}.alpha_context",
                days,
                ">= 365 days",
                "an annualized alpha headline rests on a short window",
            )
    for i, row in enumerate(_rows(panel.data.get("strategies"))):
        status = str(row.get("account_live_cagr_status") or "")
        if row.get("account_live_cagr") is not None and "UNDER_365" in status:
            yield _hit(
                "PL06",
                f"$.strategies[{i}].account_live_cagr",
                row.get("account_live_cagr"),
                "suppressed under 365 days",
                "a CAGR is shown beside a status that says the window is under a year",
            )


@rule("PL07", (F5_CHARTS,), "no return or percent chip reaches 10 000 %")
def pl07(panel: Panel) -> Iterator[Hit]:
    for name, path, value in panel.keyed(
        "cumulative_return",
        "target_cumulative_return",
        "account_cumulative_return",
        "ytd_twr",
        "net_annualized",
    ):
        v = num(value)
        if v is not None and abs(v) >= PERCENT_CHIP_MAX:
            yield _hit(
                "PL07",
                path,
                v,
                f"|value| < {PERCENT_CHIP_MAX:g}",
                f"{name} would print as {v * 100:,.0f} %",
            )


@rule(
    "PL08",
    (F4_FACTOR_TAB,),
    "drawdowns lie in [-1, 1] and a current drawdown never exceeds the maximum",
)
def pl08(panel: Panel) -> Iterator[Hit]:
    for name, path, value in panel.keyed(
        "drawdown",
        "current_drawdown",
        "maximum_drawdown",
        "max_drawdown",
        "account_live_max_drawdown",
        "hwm_current_drawdown",
        "hwm_max_drawdown",
        "target_max_drawdown",
        "target_current_drawdown",
    ):
        v = num(value)
        if v is not None and abs(v) > 1:
            yield _hit("PL08", path, v, "|drawdown| <= 1", f"{name} outside 0-100 %")
    hp = _get(panel.data, "hwm_governed_proof") or {}
    cur, mx = num(hp.get("current_drawdown")), num(hp.get("maximum_drawdown"))
    if cur is not None and mx is not None and abs(cur) > abs(mx) + 1e-9:
        yield _hit(
            "PL08",
            "$.hwm_governed_proof",
            {"current": cur, "maximum": mx},
            "|current| <= |maximum|",
            "the current drawdown is deeper than the maximum",
        )
    raw_dd = [num(p.get("drawdown")) for p in _rows(hp.get("index_path"))]
    path_dd = [x for x in raw_dd if x is not None]
    if path_dd and mx is not None and abs(min(path_dd) - mx) > 1e-9:
        yield _hit(
            "PL08",
            "$.hwm_governed_proof.index_path",
            {"min_in_path": min(path_dd), "stated": mx},
            "equal",
            "the stated maximum drawdown is not the worst point of its own path",
        )


@rule(
    "PL09",
    (F4_FACTOR_TAB,),
    "correlations lie in [-1, 1]; stress correlations in [0, 1]",
)
def pl09(panel: Panel) -> Iterator[Hit]:
    for name, path, value in panel.keyed(
        "correlation", "decision_correlation", "observed_correlation"
    ):
        v = num(value)
        if v is not None and abs(v) > 1 + 1e-9:
            yield _hit("PL09", path, v, "|r| <= 1", f"{name} outside [-1, 1]")
    for _, path, value in panel.keyed("stress_pre_psd"):
        v = num(value)
        if v is not None and not 0 <= v <= 1:
            yield _hit(
                "PL09", path, v, "0 <= stress <= 1", "stress correlation outside [0, 1]"
            )


@rule(
    "PL10",
    (F4_FACTOR_TAB,),
    "tracking error, alignment ratio and gross-to-equity are inside plausible ranges",
)
def pl10(panel: Panel) -> Iterator[Hit]:
    for name, path, value in panel.keyed(
        "tracking_error_annualized", "annualized_tracking_error"
    ):
        v = num(value)
        if v is not None and not 0 <= v <= TRACKING_ERROR_MAX:
            yield _hit(
                "PL10",
                path,
                v,
                f"0 <= TE <= {TRACKING_ERROR_MAX:g}",
                "tracking error out of range",
            )
    for _, path, value in panel.keyed("alignment_ratio"):
        v = num(value)
        if v is not None and not 0 <= v <= 1:
            yield _hit(
                "PL10", path, v, "0 <= ratio <= 1", "alignment ratio out of range"
            )
    for _, path, value in panel.keyed("gross_to_equity_ratio"):
        v = num(value)
        if v is not None and not 0 < v <= GROSS_TO_EQUITY_MAX:
            yield _hit(
                "PL10",
                path,
                v,
                f"0 < gross/equity <= {GROSS_TO_EQUITY_MAX:g}",
                "leverage ratio out of range",
            )


@rule("PL11", (F4_FACTOR_TAB,), "no NaN or infinite number anywhere in the payload")
def pl11(panel: Panel) -> Iterator[Hit]:
    stack: list[tuple[str, Any]] = [("$", panel.data)]
    count = 0
    while stack:
        path, node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend((f"{path}.{k}", v) for k, v in node.items())
        elif isinstance(node, list):
            if node and not isinstance(node[0], (Mapping, list)):
                bad = [
                    i
                    for i, x in enumerate(node)
                    if isinstance(x, float) and not math.isfinite(x)
                ]
                if bad:
                    count += 1
                    if count <= 20:
                        yield _hit(
                            "PL11",
                            f"{path}[{bad[0]}]",
                            "non-finite",
                            "finite",
                            "NaN or infinity in a series",
                        )
            else:
                stack.extend((f"{path}[{i}]", v) for i, v in enumerate(node))
        elif isinstance(node, float) and not math.isfinite(node):
            count += 1
            if count <= 20:
                yield _hit("PL11", path, "non-finite", "finite", "NaN or infinity")


# ============================================================================================
# Weights and sums
# ============================================================================================


def _sum_hits(rule_id: str, path: str, values: list[float], what: str) -> Iterator[Hit]:
    if values and abs(sum(values) - 1) > WEIGHT_SUM_TOLERANCE:
        yield _hit(
            rule_id, path, round(sum(values), 6), "sum = 1", f"{what} do not sum to 1"
        )


@rule(
    "PL12",
    (F4_FACTOR_TAB, F1_STALE_HAND_AUDIT),
    "weights lie in [0, 1] and each weight set sums to 1",
)
def pl12(panel: Panel) -> Iterator[Hit]:
    alloc = _rows(_get(panel.data, "allocations"))
    weights = [
        w for w in (num(a.get("systematic_weight")) for a in alloc) if w is not None
    ]
    for i, a in enumerate(alloc):
        w = num(a.get("systematic_weight"))
        if w is not None and not 0 <= w <= 1:
            yield _hit(
                "PL12",
                f"$.allocations[{i}].systematic_weight",
                w,
                "0 <= w <= 1",
                "weight outside [0, 1]",
            )
    yield from _sum_hits(
        "PL12", "$.allocations[*].systematic_weight", weights, "allocation weights"
    )
    dep = panel.data.get("dependence_analysis")
    for block in ("central_case", "correlation_one"):
        comps = _rows(_get(dep, block, "components"))
        yield from _sum_hits(
            "PL12",
            f"$.dependence_analysis.{block}.components[*].allocation_weight",
            [
                w
                for w in (num(c.get("allocation_weight")) for c in comps)
                if w is not None
            ],
            f"{block} allocation weights",
        )
    cur = [
        w
        for w in (
            num(s.get("current_weight"))
            for s in _rows(_get(panel.data, "custody_current_capital", "strategies"))
        )
        if w is not None
    ]
    for w in cur:
        if not 0 <= w <= 1:
            yield _hit(
                "PL12",
                "$.custody_current_capital.strategies[*].current_weight",
                w,
                "0 <= w <= 1",
                "current weight outside [0, 1]",
            )


# ============================================================================================
# Calendar truth and as-of dates (finds 1, 6)
# ============================================================================================


def _last_trading_day(week_start: date) -> date:
    friday = week_start + timedelta(days=4)
    return friday - timedelta(days=1) if friday in NYSE_FRIDAY_HOLIDAYS else friday


@rule(
    "PL13",
    (F6_STATUS_TOKEN,),
    "a week whose data reach its last trading day is never PARTIAL",
)
def pl13(panel: Panel) -> Iterator[Hit]:
    cw = _get(panel.data, "weekly_returns", "current_week") or {}
    through, start = to_date(cw.get("data_through_date")), to_date(cw.get("week_start"))
    if (
        through
        and start
        and cw.get("is_partial") is True
        and through >= _last_trading_day(start)
    ):
        yield _hit(
            "PL13",
            "$.weekly_returns.current_week.is_partial",
            {
                "data_through": str(through),
                "last_trading_day": str(_last_trading_day(start)),
                "is_partial": True,
            },
            "is_partial false",
            "PARTIAL on a complete trading week",
        )
    for mode, block in (_get(panel.data, "performance_tab", "modes") or {}).items():
        t = to_date(block.get("data_through_date"))
        ends = [to_date(x) for x in block.get("week_end_dates") or []][-1:]
        if t and block.get("last_week_partial") is True and ends and ends[0]:
            ws = ends[0] - timedelta(days=ends[0].weekday())
            if t >= _last_trading_day(ws):
                yield _hit(
                    "PL13",
                    f"$.performance_tab.modes.{mode}.last_week_partial",
                    {
                        "data_through": str(t),
                        "last_trading_day": str(_last_trading_day(ws)),
                    },
                    "false",
                    "chart labels the last week PARTIAL on a complete trading week",
                )


@rule(
    "PL14",
    (F6_STATUS_TOKEN,),
    "the current-week window contains the data date and starts on a Monday",
)
def pl14(panel: Panel) -> Iterator[Hit]:
    cw = _get(panel.data, "weekly_returns", "current_week") or {}
    start, end, through = (
        to_date(cw.get("week_start")),
        to_date(cw.get("week_end")),
        to_date(cw.get("data_through_date")),
    )
    if start and start.weekday() != 0:
        yield _hit(
            "PL14",
            "$.weekly_returns.current_week.week_start",
            str(start),
            "a Monday",
            "week does not start on Monday",
        )
    if start and end and through and not start <= through <= end:
        yield _hit(
            "PL14",
            "$.weekly_returns.current_week",
            {"start": str(start), "end": str(end), "through": str(through)},
            "start <= through <= end",
            "data date outside its own week",
        )


def _age_limit(artifact: str) -> int:
    return (
        POLICY_ALLOWED_AGE_DAYS
        if _POLICY_ARTIFACT.search(artifact)
        else ALLOWED_AGE_DAYS
    )


def _provenance_rows(panel: Panel) -> list[tuple[int, Mapping[str, Any]]]:
    return [(i, r) for i, r in enumerate(_rows(panel.data.get("provenance")))]


@rule(
    "PL15",
    (F1_STALE_HAND_AUDIT,),
    "no dated input is older than its allowed age against the cutoff behind a current panel",
    klass="NOT_READY",
)
def pl15(panel: Panel) -> Iterator[Hit]:
    if not panel.cutoff:
        return
    register = panel.data.get("staleness_register")
    listed = (
        {str(r.get("artifact")) for r in _rows(register)}
        if isinstance(register, list)
        else set()
    )
    for i, row in _provenance_rows(panel):
        when = to_date(row.get("as_of_date"))
        name = str(row.get("artifact"))
        if when is None:
            continue
        age = (panel.cutoff - when).days
        if age > _age_limit(name) and name not in listed:
            yield _hit(
                "PL15",
                f"$.provenance[{i}]",
                {
                    "artifact": name,
                    "as_of_date": str(when),
                    "age_days": age,
                    "schema": row.get("schema"),
                },
                f"<= {_age_limit(name)} days or listed in the staleness register",
                "a stale input is not on the Health tab and nothing prevents a green cell behind it",
            )


@rule(
    "PL16",
    (F1_STALE_HAND_AUDIT,),
    "every non-code input carries a date",
    klass="NOT_READY",
)
def pl16(panel: Panel) -> Iterator[Hit]:
    register = panel.data.get("staleness_register")
    listed = (
        {str(r.get("artifact")) for r in _rows(register)}
        if isinstance(register, list)
        else set()
    )
    for i, row in _provenance_rows(panel):
        if (
            to_date(row.get("as_of_date")) is None
            and not row.get("generation_id")
            and str(row.get("artifact")) not in listed
        ):
            yield _hit(
                "PL16",
                f"$.provenance[{i}]",
                {"artifact": row.get("artifact"), "schema": row.get("schema")},
                "an as_of_date or a generation id",
                "an input with no date can never be judged stale",
            )


@rule(
    "PL17",
    (F1_STALE_HAND_AUDIT,),
    "no input is dated after the cutoff by more than 14 days",
)
def pl17(panel: Panel) -> Iterator[Hit]:
    if not panel.cutoff:
        return
    for i, row in _provenance_rows(panel):
        when = to_date(row.get("as_of_date"))
        if when and (when - panel.cutoff).days > FUTURE_DATE_TOLERANCE_DAYS:
            yield _hit(
                "PL17",
                f"$.provenance[{i}]",
                str(when),
                f"<= cutoff + {FUTURE_DATE_TOLERANCE_DAYS} days",
                "an input dated in the future of the cutoff",
            )


@rule(
    "PL18",
    (F1_STALE_HAND_AUDIT,),
    "an allocation snapshot older than the allowed age is not shown while current weights exist",
)
def pl18(panel: Panel) -> Iterator[Hit]:
    if not panel.cutoff or not _rows(
        _get(panel.data, "custody_current_capital", "strategies")
    ):
        return
    for i, a in enumerate(_rows(panel.data.get("allocations"))):
        m = re.search(
            r"SNAPSHOT_(\d{4})_(\d{2})_(\d{2})", str(a.get("weight_basis") or "")
        )
        if m:
            when = date(int(m[1]), int(m[2]), int(m[3]))
            if (panel.cutoff - when).days > ALLOWED_AGE_DAYS:
                yield _hit(
                    "PL18",
                    f"$.allocations[{i}].weight_basis",
                    {"snapshot": str(when), "age_days": (panel.cutoff - when).days},
                    f"<= {ALLOWED_AGE_DAYS} days",
                    "a historical weight snapshot is the allocation shown while a current-cutoff weight exists",
                )


@rule(
    "PL19",
    (F1_STALE_HAND_AUDIT,),
    "no model-vs-account gap rests on a window that ends before the account window",
)
def pl19(panel: Panel) -> Iterator[Hit]:
    for i, row in enumerate(_rows(_get(panel.data, "strategy_underwriting", "rows"))):
        gap = row.get("model_account_gap")
        end = to_date(_get(gap, "observed_end"))
        live = to_date(_get(row, "evidence_layers", "account_live", "observed_end"))
        if (
            end
            and live
            and (live - end).days > FACTOR_STALE_DAYS
            and num(_get(gap, "total_gap")) is not None
        ):
            yield _hit(
                "PL19",
                f"$.strategy_underwriting.rows[{i}].model_account_gap",
                {"gap_ends": str(end), "account_live_ends": str(live)},
                f"gap window within {FACTOR_STALE_DAYS} days of the account window",
                "a numeric gap computed on a stale window",
            )


@rule(
    "PL20",
    (F1_STALE_HAND_AUDIT, F7_HEALTH_UNREACHABLE),
    "dated source clocks are within the allowed age of the cutoff unless the source is a finite closed boundary",
)
def pl20(panel: Panel) -> Iterator[Hit]:
    if not panel.cutoff:
        return
    for i, row in enumerate(
        _rows(_get(panel.data, "meta", "source_clock", "source_data_as_of"))
    ):
        when = to_date(row.get("provider_data_date"))
        finite = str(_get(row, "finality", "basis") or "").startswith("FINITE")
        if when and not finite and (panel.cutoff - when).days > ALLOWED_AGE_DAYS:
            yield _hit(
                "PL20",
                f"$.meta.source_clock.source_data_as_of[{i}]",
                {"source": row.get("source_id"), "date": str(when)},
                f"<= {ALLOWED_AGE_DAYS} days",
                "a live source clock is older than its allowed age",
            )


# ============================================================================================
# Status tokens and the Health tab (finds 2, 6, 7)
# ============================================================================================

_COMPLETE = re.compile(
    r"^(COMPLETE|FINITE_COMPLETE|DECLARED_COMPLETE|RECONCILED|CLOSES)"
)


@rule(
    "PL21",
    (F7_HEALTH_UNREACHABLE,),
    "a coverage matrix can contain a complete status: green must be reachable",
)
def pl21(panel: Panel) -> Iterator[Hit]:
    matrix = _rows(panel.data.get("fact_coverage_matrix"))
    if matrix and not any(_COMPLETE.match(str(r.get("status") or "")) for r in matrix):
        tokens = sorted({str(r.get("status")) for r in matrix})
        yield _hit(
            "PL21",
            "$.fact_coverage_matrix[*].status",
            {"rows": len(matrix), "tokens": tokens},
            "at least one row can read complete",
            "no row of the coverage matrix is complete: every dependent status is permanently yellow",
        )


@rule(
    "PL22",
    (F7_HEALTH_UNREACHABLE,),
    "a status that stays PARTIAL must have an achievable complete state in its own matrix rows",
)
def pl22(panel: Panel) -> Iterator[Hit]:
    status = panel.data.get("fact_status")
    matrix = _rows(panel.data.get("fact_coverage_matrix"))
    if not isinstance(status, Mapping) or not matrix:
        return
    for fact, state in status.items():
        if str(state) == "PARTIAL":
            rows = [
                r
                for r in matrix
                if str(r.get("fact_type"))
                in {fact, fact.rstrip("s"), fact[:-1] if fact.endswith("s") else fact}
            ]
            if rows and not any(
                _COMPLETE.match(str(r.get("status") or "")) for r in rows
            ):
                yield _hit(
                    "PL22",
                    f"$.fact_status.{fact}",
                    {"matrix_tokens": sorted({str(r.get("status")) for r in rows})},
                    "a complete token exists for this fact",
                    "PARTIAL can never clear: its rows use only never-complete tokens",
                )
    partial = sorted(k for k, v in status.items() if str(v) == "PARTIAL")
    if len(status) and len(partial) * 2 >= len(status):
        yield _hit(
            "PL22",
            "$.fact_status",
            {"partial": partial, "of": len(status)},
            "fewer than half non-complete",
            "Health cannot be mostly yellow when green is defined as reconciles",
        )


_NEVER_COMPLETE = re.compile(r"NOT_COMPLETENESS_PROOF|NO_ACCEPTED_FACT_PATH")


@rule(
    "PL23",
    (F7_HEALTH_UNREACHABLE, F2_UNDERWRITING_GAP),
    "every non-complete coverage cell names its open issue",
    klass="NOT_READY",
)
def pl23(panel: Panel) -> Iterator[Hit]:
    unexplained = []
    for i, r in enumerate(_rows(panel.data.get("coverage"))):
        st = str(r.get("status") or "")
        if (
            st
            and not _COMPLETE.match(st)
            and st
            not in {"FRESH", "RETAINED_WITH_EXPLICIT_DISPOSITION", "OBSERVED_COUNT"}
        ):
            text = " ".join(
                str(r.get(k) or "")
                for k in ("known_gap", "note", "disposition", "issue")
            )
            if not _ISSUE_REF.search(text):
                unexplained.append(i)
    if unexplained:
        yield _hit(
            "PL23",
            "$.coverage",
            {"unexplained_cells": len(unexplained), "first": unexplained[:5]},
            "0 non-green cells without an issue reference",
            "non-green cells with no named issue or pull that closes them",
        )


@rule(
    "PL24",
    (F2_UNDERWRITING_GAP,),
    "an underwriting evidence layer that is unavailable names its cause and the pull that closes it",
    klass="NOT_READY",
)
def pl24(panel: Panel) -> Iterator[Hit]:
    uw = _get(panel.data, "strategy_underwriting") or {}
    counts = uw.get("counts") or {}
    total = num(counts.get("strategies"))
    short = {
        k: v
        for k, v in counts.items()
        if k != "strategies" and total and (n := num(v)) is not None and n < total
    }
    if not short:
        return
    missing = []
    for i, row in enumerate(_rows(uw.get("rows"))):
        for layer, body in (row.get("evidence_layers") or {}).items():
            if isinstance(body, Mapping) and str(body.get("status")) in {
                "UNAVAILABLE",
                "NOT_EVIDENCED",
            }:
                text = " ".join(
                    str(body.get(k) or "")
                    for k in (
                        "reason",
                        "unavailable_reason",
                        "issue",
                        "next_pull",
                        "known_gap",
                    )
                )
                if not _ISSUE_REF.search(text):
                    missing.append(f"{row.get('strategy_id')}.{layer}")
    yield _hit(
        "PL24",
        "$.strategy_underwriting.counts",
        {
            "counts_below_strategies": short,
            "strategies": total,
            "unavailable_without_named_pull": missing,
        },
        "every shortfall names an open issue and its closing pull",
        "underwriting shows 5 or 6 of 7 on a metric with no issue behind the gap",
    )


@rule(
    "PL25",
    (F2_UNDERWRITING_GAP, F6_STATUS_TOKEN),
    "counts agree with the rows they count",
)
def pl25(panel: Panel) -> Iterator[Hit]:
    uw = _get(panel.data, "strategy_underwriting") or {}
    rows = _rows(uw.get("rows"))
    counts = uw.get("counts") or {}
    if (
        rows
        and num(counts.get("strategies")) is not None
        and int(counts["strategies"]) != len(rows)
    ):
        yield _hit(
            "PL25",
            "$.strategy_underwriting.counts.strategies",
            {"stated": counts["strategies"], "rows": len(rows)},
            "equal",
            "underwriting strategy count differs from its rows",
        )
    avail = sum(
        1
        for r in rows
        if str(_get(r, "design_fit", "status")).startswith("AUTHORITATIVE")
    )
    if (
        rows
        and num(counts.get("authoritative_design")) is not None
        and int(counts["authoritative_design"]) != avail
    ):
        yield _hit(
            "PL25",
            "$.strategy_underwriting.counts.authoritative_design",
            {"stated": counts["authoritative_design"], "rows": avail},
            "equal",
            "authoritative-design count differs from its rows",
        )
    c = panel.data.get("counts") or {}
    if num(c.get("twr_rows")) is not None and int(c["twr_rows"]) != len(
        _rows(panel.data.get("performance"))
    ):
        yield _hit(
            "PL25",
            "$.counts.twr_rows",
            {
                "stated": c["twr_rows"],
                "rows": len(_rows(panel.data.get("performance"))),
            },
            "equal",
            "performance row count differs from counts.twr_rows",
        )


# ============================================================================================
# Claims that must be able to fail (find 3)
# ============================================================================================


@rule(
    "PL26",
    (F3_CONSERVATIVE_CLAIM,),
    "a conservative loss fraction is at least the loss already observed at the cutoff, per series and mode",
)
def pl26(panel: Panel) -> Iterator[Hit]:
    for block_name, dep in [
        ("dependence_analysis", panel.data.get("dependence_analysis"))
    ] + [
        (f"dependence_modes.{m}", v)
        for m, v in (_get(panel.data, "dependence_modes") or {}).items()
    ]:
        conservative = {
            c.get("series_id"): num(c.get("conservative_loss_fraction"))
            for c in _rows(_get(dep, "correlation_one", "components"))
        }
        for i, c in enumerate(_rows(_get(dep, "central_case", "components"))):
            sid, observed = c.get("series_id"), num(c.get("adverse_loss_fraction"))
            bound = conservative.get(sid)
            if bound is not None and observed is not None and bound < observed:
                yield _hit(
                    "PL26",
                    f"$.{block_name}.correlation_one.components[{sid}].conservative_loss_fraction",
                    {"conservative": bound, "observed_adverse": observed},
                    "conservative >= observed",
                    "the conservative fraction is below the drawdown already observed",
                )


@rule(
    "PL27",
    (F3_CONSERVATIVE_CLAIM,),
    "a conservative loss fraction is at least the series' own observed maximum drawdown",
)
def pl27(panel: Panel) -> Iterator[Hit]:
    worst: dict[str, float] = {}
    for _, s in _lens_series(panel):
        sid = s.get("series_id") or s.get("label")
        dd = num(_get(s, "coverage", "target_max_drawdown"))
        if sid and dd is not None:
            worst[str(sid)] = max(worst.get(str(sid), 0.0), abs(dd))
    for i, c in enumerate(
        _rows(_get(panel.data, "dependence_analysis", "correlation_one", "components"))
    ):
        bound, sid = num(c.get("conservative_loss_fraction")), str(c.get("series_id"))
        if bound is not None and sid in worst and bound < worst[sid]:
            yield _hit(
                "PL27",
                f"$.dependence_analysis.correlation_one.components[{i}]",
                {
                    "series": sid,
                    "conservative": bound,
                    "observed_max_drawdown": worst[sid],
                },
                "conservative >= observed max drawdown",
                "a conservative bound below the series' own worst drawdown",
            )


@rule(
    "PL28",
    (F3_CONSERVATIVE_CLAIM,),
    "a qualitative word in prose carries a recompute that can fail",
    klass="NOT_READY",
)
def pl28(panel: Panel) -> Iterator[Hit]:
    claim_table = panel.data.get("claim_checks")
    checked = (
        {str(c.get("claim")) for c in _rows(claim_table)}
        if isinstance(claim_table, list)
        else set()
    )
    found: dict[str, str] = {}
    stack: list[tuple[str, Any]] = [
        ("$.advisory", panel.data.get("advisory")),
        ("$.dependence_analysis", panel.data.get("dependence_analysis")),
    ]
    while stack:
        path, node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(
                (f"{path}.{k}", v) for k, v in node.items() if k != "components"
            )
        elif isinstance(node, list):
            stack.extend(
                (f"{path}[{i}]", v)
                for i, v in enumerate(node[:200])
                if isinstance(v, (Mapping, list, str))
            )
        elif isinstance(node, str) and " " in node and len(node) > 30:
            m = re.search(r"\bconservative\b", node, re.I)
            if m and path not in found:
                found[path] = node[:100]
    for path, text in found.items():
        if path not in checked and not any(path.endswith(c) for c in checked):
            yield _hit(
                "PL28",
                path,
                text,
                "an entry in claim_checks that recomputes the word at the cutoff",
                "an adjective with no test",
            )


# ============================================================================================
# Equalities between surfaces (find 5/6 and the general class)
# ============================================================================================


@rule("PL29", (F5_CHARTS, F6_STATUS_TOKEN), "every surface reports the same cutoff")
def pl29(panel: Panel) -> Iterator[Hit]:
    if not panel.cutoff:
        yield _hit(
            "PL29",
            "$.meta.portfolio_as_of",
            None,
            "a cutoff date",
            "the payload names no cutoff",
        )
        return
    values = {
        "meta.portfolio_as_of": _get(panel.data, "meta", "portfolio_as_of"),
        "meta.source_clock.selected_financial_cutoff": _get(
            panel.data, "meta", "source_clock", "selected_financial_cutoff"
        ),
        "factor_analysis.as_of_date": _get(panel.data, "factor_analysis", "as_of_date"),
        "weekly_returns.as_of_date": _get(panel.data, "weekly_returns", "as_of_date"),
        "strategy_underwriting.as_of_date": _get(
            panel.data, "strategy_underwriting", "as_of_date"
        ),
        "weekly_returns.current_week.data_through_date": _get(
            panel.data, "weekly_returns", "current_week", "data_through_date"
        ),
        "performance_tab.as_of": _get(panel.data, "performance_tab", "as_of"),
        "hwm_candidate.complete_through": _get(
            panel.data, "advisory", "hwm_candidate", "complete_through"
        ),
    }
    for name, value in values.items():
        d = to_date(value)
        if d is not None and d != panel.cutoff:
            yield _hit(
                "PL29",
                f"$.{name}",
                str(d),
                str(panel.cutoff),
                "a surface disagrees with the cutoff",
            )


@rule(
    "PL30", (F5_CHARTS,), "the chart legend end date equals the trailing table end date"
)
def pl30(panel: Panel) -> Iterator[Hit]:
    pt = _get(panel.data, "performance_tab", "modes") or {}
    wk = _get(panel.data, "weekly_returns", "weeks")
    last_table = to_date(wk[-1]) if isinstance(wk, list) and wk else None
    for mode, block in pt.items():
        dates, ends = block.get("dates"), block.get("week_end_dates")
        through = to_date(block.get("data_through_date"))
        if isinstance(ends, list) and ends and through and to_date(ends[-1]) != through:
            yield _hit(
                "PL30",
                f"$.performance_tab.modes.{mode}.week_end_dates",
                {"last_week_end": ends[-1], "data_through": str(through)},
                "equal",
                "the legend's last date differs from the data date",
            )
        if (
            isinstance(dates, list)
            and dates
            and last_table
            and to_date(dates[-1]) is not None
        ):
            if (to_date(dates[-1]) or last_table) + timedelta(days=6) < last_table:
                yield _hit(
                    "PL30",
                    f"$.performance_tab.modes.{mode}.dates",
                    {"chart_last_week": dates[-1], "table_last_week": str(last_table)},
                    "same week",
                    "the chart stops before the trailing table",
                )


@rule(
    "PL31",
    (F5_CHARTS,),
    "chart axes: dates unique and ascending, lines non-negative with a bounded range ratio",
)
def pl31(panel: Panel) -> Iterator[Hit]:
    for mode, block in (_get(panel.data, "performance_tab", "modes") or {}).items():
        dates = [d for d in (block.get("dates") or []) if isinstance(d, str)]
        if len(dates) != len(set(dates)):
            yield _hit(
                "PL31",
                f"$.performance_tab.modes.{mode}.dates",
                len(dates) - len(set(dates)),
                "0 repeated labels",
                "repeated axis labels",
            )
        if dates != sorted(dates):
            yield _hit(
                "PL31",
                f"$.performance_tab.modes.{mode}.dates",
                "unsorted",
                "ascending",
                "axis dates out of order",
            )
        for i, line in enumerate(_rows(block.get("lines"))):
            values = [
                v for v in (num(x) for x in line.get("tri") or []) if v is not None
            ]
            if not values:
                continue
            lo, hi = min(values), max(values)
            if lo <= 0:
                yield _hit(
                    "PL31",
                    f"$.performance_tab.modes.{mode}.lines[{i}].tri",
                    lo,
                    "> 0",
                    "a total-return index at or below zero cannot be drawn",
                )
            elif hi / lo > AXIS_RATIO_MAX:
                yield _hit(
                    "PL31",
                    f"$.performance_tab.modes.{mode}.lines[{i}].tri",
                    hi / lo,
                    f"<= {AXIS_RATIO_MAX:g}",
                    "axis range ratio makes the chart unreadable",
                )


@rule("PL32", (F5_CHARTS,), "line and display names are unique within a chart")
def pl32(panel: Panel) -> Iterator[Hit]:
    for mode, block in (_get(panel.data, "performance_tab", "modes") or {}).items():
        names = [str(ln.get("display_name")) for ln in _rows(block.get("lines"))]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            yield _hit(
                "PL32",
                f"$.performance_tab.modes.{mode}.lines",
                dup,
                "unique display names",
                "two lines share a label",
            )
        ids = [str(ln.get("series_id")) for ln in _rows(block.get("lines"))]
        if len(ids) != len(set(ids)):
            yield _hit(
                "PL32",
                f"$.performance_tab.modes.{mode}.lines",
                "duplicate series_id",
                "unique",
                "two lines share an id",
            )
    names = [
        str(s.get("series_id"))
        for s in _rows(_get(panel.data, "factor_analysis", "series"))
    ]
    if len(names) != len(set(names)):
        yield _hit(
            "PL32",
            "$.factor_analysis.series",
            "duplicate series_id",
            "unique",
            "two factor series share an id",
        )


@rule(
    "PL33",
    (F6_STATUS_TOKEN,),
    "the default view includes the benchmarks the legend names",
)
def pl33(panel: Panel) -> Iterator[Hit]:
    for mode, block in (_get(panel.data, "performance_tab", "modes") or {}).items():
        kinds = {ln.get("kind") for ln in _rows(block.get("lines"))}
        if "BENCHMARK" not in kinds and not block.get("benchmarks_unavailable"):
            yield _hit(
                "PL33",
                f"$.performance_tab.modes.{mode}.lines",
                sorted(map(str, kinds)),
                "a BENCHMARK line or a stated unavailable list",
                "benchmarks silently missing from the chart",
            )
        for line in _rows(block.get("lines")):
            if line.get("kind") == "BENCHMARK" and (
                line.get("hidden") or line.get("default_visible") is False
            ):
                yield _hit(
                    "PL33",
                    f"$.performance_tab.modes.{mode}.lines[{line.get('series_id')}]",
                    "hidden by default",
                    "visible by default",
                    "a benchmark is hidden in the default view",
                )


@rule(
    "PL34",
    (F4_FACTOR_TAB,),
    "annual alpha equals the periodic intercept times periods per year",
)
def pl34(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        alpha, ctx = s.get("alpha") or {}, s.get("alpha_context") or {}
        per, ann, ppy = (
            num(alpha.get("periodic_intercept")),
            num(alpha.get("net_annualized")),
            num(ctx.get("periods_per_year")),
        )
        if (
            per is not None
            and ann is not None
            and ppy
            and "TIMES_PERIODS" in str(ctx.get("annualisation"))
        ):
            want = per * ppy
            if abs(ann - want) > max(abs(want) * ANNUALISATION_REL_TOLERANCE, 1e-9):
                yield _hit(
                    "PL34",
                    f"{path}.alpha.net_annualized",
                    {"stated": ann, "recomputed": want},
                    "equal",
                    "annualisation not consistent with its own intercept",
                )


@rule(
    "PL35",
    (F3_CONSERVATIVE_CLAIM,),
    "loss contribution equals weight times loss fraction; the headline stress equals the governed bound",
)
def pl35(panel: Panel) -> Iterator[Hit]:
    comps = _rows(
        _get(panel.data, "dependence_analysis", "correlation_one", "components")
    )
    for i, c in enumerate(comps):
        w, f, loss = (
            num(c.get("allocation_weight")),
            num(c.get("conservative_loss_fraction")),
            num(c.get("loss_contribution")),
        )
        if (
            w is not None
            and f is not None
            and loss is not None
            and abs(w * f - loss) > 1e-9
        ):
            yield _hit(
                "PL35",
                f"$.dependence_analysis.correlation_one.components[{i}]",
                {"weight*fraction": w * f, "stated": loss},
                "equal",
                "loss contribution is not weight x fraction",
            )
    adv, dep = (
        num(_get(panel.data, "advisory", "correlation_one_stress")),
        num(
            _get(
                panel.data,
                "dependence_analysis",
                "economic_driver_cluster_budget",
                "correlation_one_loss",
            )
        ),
    )
    if adv is not None and dep is not None and abs(adv - dep) > 1e-9:
        yield _hit(
            "PL35",
            "$.advisory.correlation_one_stress",
            {"advisory": adv, "dependence": dep},
            "equal",
            "the advisory stress differs from the dependence block",
        )
    central_adv = num(_get(panel.data, "advisory", "central_case_loss"))
    parts = [
        num(c.get("loss_scale_contribution"))
        for c in _rows(
            _get(panel.data, "dependence_analysis", "central_case", "components")
        )
    ]
    if central_adv is not None and parts and None not in parts:
        total = sum(parts)  # type: ignore[arg-type]
        # the central case is the square-root-of-quadratic-form combination of its components:
        # it is never above their plain sum (equal only at correlation 1), so only a value
        # ABOVE the sum is wrong; a value below it is the diversification the form measures
        if total > 0 and central_adv > total * (1 + 1e-9):
            yield _hit(
                "PL35",
                "$.advisory.central_case_loss",
                {"advisory": central_adv, "component_sum": total},
                "central case <= sum of its components",
                "central-case loss exceeds the sum of its components",
            )


@rule(
    "PL36",
    (F4_FACTOR_TAB,),
    "an HWM label and its numbers agree: window end equals complete-through",
)
def pl36(panel: Panel) -> Iterator[Hit]:
    h = _get(panel.data, "advisory", "hwm_candidate") or {}
    we, ct = to_date(_get(h, "kpi", "window_end")), to_date(h.get("complete_through"))
    if we and ct and we != ct:
        yield _hit(
            "PL36",
            "$.advisory.hwm_candidate.kpi.window_end",
            {"window_end": str(we), "complete_through": str(ct)},
            "equal",
            "HWM tile window differs from the evidence end",
        )
    cd, kd = num(h.get("current_drawdown")), num(_get(h, "kpi", "drawdown"))
    if cd is not None and kd is not None and abs(round(cd, 4) - kd) > 1e-4:
        yield _hit(
            "PL36",
            "$.advisory.hwm_candidate.kpi.drawdown",
            {"tile": kd, "value": cd},
            "rounds equal",
            "the tile is not the stated drawdown",
        )


@rule(
    "PL37",
    (F7_HEALTH_UNREACHABLE,),
    "gates that read complete do not contradict a PARTIAL fact of the same name",
)
def pl37(panel: Panel) -> Iterator[Hit]:
    status = panel.data.get("fact_status")
    if not isinstance(status, Mapping):
        return
    for g in _rows(panel.data.get("gates")):
        item = str(g.get("item") or "")
        if item in status and str(g.get("status")) != str(status[item]):
            yield _hit(
                "PL37",
                f"$.gates[{item}]",
                {"gate": g.get("status"), "fact_status": status[item]},
                "equal",
                "two surfaces give different status for one fact",
            )


@rule(
    "PL38",
    (F5_CHARTS,),
    "decomposition lenses declare a bounded evidence window: observations do not exceed the window days",
)
def pl38(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        ctx = s.get("alpha_context") or {}
        n, days = num(ctx.get("n_obs")), num(ctx.get("window_days"))
        if n is not None and days is not None and n > days + 1:
            yield _hit(
                "PL38",
                f"{path}.alpha_context",
                {"n_obs": n, "window_days": days},
                "n_obs <= window_days",
                "more observations than days in the window",
            )


@rule(
    "PL39",
    (F4_FACTOR_TAB,),
    "train and holdout observations are positive and the split margins are non-negative",
)
def pl39(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        sm = _get(s, "coverage", "split_margin") or {}
        for key in ("test_margin", "train_margin"):
            v = num(sm.get(key))
            if v is not None and v < 0:
                yield _hit(
                    "PL39",
                    f"{path}.coverage.split_margin.{key}",
                    v,
                    ">= 0",
                    "a fit below the stated minimum sample was published",
                )


@rule(
    "PL40",
    (F4_FACTOR_TAB, F5_CHARTS),
    "no dynamic exposure path starts before its estimation window or has a date gap larger than 7 days",
)
def pl40(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        ep = _get(s, "diagnostics", "dynamic", "exposure_path")
        raw_days = [to_date(p.get("date")) for p in _rows(ep)]
        days = [d for d in raw_days if d]
        for a, b in zip(days, days[1:]):
            if (b - a).days > 7 or b <= a:
                yield _hit(
                    "PL40",
                    f"{path}.diagnostics.dynamic.exposure_path",
                    {"from": str(a), "to": str(b)},
                    "ascending, gaps <= 7 days",
                    "a chart series jumps or repeats",
                )
                break


@rule(
    "PL41",
    (F6_STATUS_TOKEN,),
    "weekly series show a value or a stated absence, never an unlabelled gap",
)
def pl41(panel: Panel) -> Iterator[Hit]:
    wr = panel.data.get("weekly_returns") or {}
    for i, s in enumerate(_rows(wr.get("series"))):
        if not s.get("absent_value_status"):
            yield _hit(
                "PL41",
                f"$.weekly_returns.series[{i}]",
                s.get("series_id"),
                "an absent_value_status",
                "a series has no declared meaning for a missing week",
            )
            break


@rule("PL42", (F5_CHARTS,), "weekly weeks are unique and ascending")
def pl42(panel: Panel) -> Iterator[Hit]:
    weeks = _get(panel.data, "weekly_returns", "weeks")
    if isinstance(weeks, list):
        text = [w for w in weeks if isinstance(w, str)]
        if len(text) != len(set(text)):
            yield _hit(
                "PL42",
                "$.weekly_returns.weeks",
                len(text) - len(set(text)),
                "no repeats",
                "repeated week labels",
            )
        if text != sorted(text):
            yield _hit(
                "PL42",
                "$.weekly_returns.weeks",
                "unsorted",
                "ascending",
                "week labels out of order",
            )


@rule(
    "PL43",
    (F2_UNDERWRITING_GAP,),
    "every strategy with a numeric gap has comparable evidence; every comparable strategy has a numeric gap",
)
def pl43(panel: Panel) -> Iterator[Hit]:
    for i, row in enumerate(_rows(_get(panel.data, "strategy_underwriting", "rows"))):
        gap = row.get("model_account_gap") or {}
        has_number = num(gap.get("total_gap")) is not None
        live = _get(row, "evidence_layers", "account_live", "status")
        if (
            str(live) == "AVAILABLE"
            and not has_number
            and str(gap.get("status")) == "UNAVAILABLE"
        ):
            yield _hit(
                "PL43",
                f"$.strategy_underwriting.rows[{i}].model_account_gap",
                {"strategy": row.get("strategy_id"), "gap_status": gap.get("status")},
                "a numeric gap or a named cause",
                "account evidence exists but the gap column is empty",
            )


@rule(
    "PL44",
    (F7_HEALTH_UNREACHABLE,),
    "capital reconciliation closes within its stated tolerance",
)
def pl44(panel: Panel) -> Iterator[Hit]:
    # difference_usd is the EXPLAINED difference (components that net to it); closure is the
    # residual and the status, never the difference itself
    cr = panel.data.get("capital_reconciliation") or {}
    tol = max(num(cr.get("tolerance")) or 0.01, 0.01)
    for key in ("residual_usd", "unexplained_usd"):
        v = num(cr.get(key))
        if v is not None and abs(v) > tol:
            yield _hit(
                "PL44",
                f"$.capital_reconciliation.{key}",
                v,
                f"|x| <= {tol}",
                "an unexplained residual: the account does not close",
            )
    status = cr.get("status")
    if isinstance(status, str) and not status.startswith("RECONCILED"):
        yield _hit(
            "PL44",
            "$.capital_reconciliation.status",
            status,
            "RECONCILED",
            "the reconciliation does not report closed",
        )


@rule(
    "PL45",
    (F7_HEALTH_UNREACHABLE, F2_UNDERWRITING_GAP),
    "the panel publishes a readiness verdict and no status token disagrees with it",
)
def pl45(panel: Panel) -> Iterator[Hit]:
    ready = _get(panel.data, "meta", "publication_readiness") or _get(
        panel.data, "meta", "account_manifest_verification", "publication_readiness"
    )
    if ready is None:
        yield _hit(
            "PL45",
            "$.meta.publication_readiness",
            None,
            "a readiness verdict",
            "the payload does not say whether it is publishable",
        )


@rule(
    "PL46",
    (F8_UNCONSUMED_CONTRACT,),
    "every source family in the panel's provenance resolves to a named consumer block",
    klass="NOT_READY",
)
def pl46(panel: Panel) -> Iterator[Hit]:
    blocks = set(panel.data)
    for i, row in _provenance_rows(panel):
        schema = str(row.get("schema") or "")
        if schema.startswith("strategy-concept") and not any(
            "concept" in b for b in blocks
        ):
            yield _hit(
                "PL46",
                f"$.provenance[{i}]",
                schema,
                "a payload block that renders this contract",
                "a contract is bound but no block carries it",
            )


@rule(
    "PL47",
    (F4_FACTOR_TAB,),
    "a fit published as available has at least the minimum train and test observations",
)
def pl47(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        sm = _get(s, "coverage", "split_margin") or {}
        tr, te = num(sm.get("train_observations")), num(sm.get("test_observations"))
        mtr, mte = (
            num(sm.get("minimum_train_observations")),
            num(sm.get("minimum_test_observations")),
        )
        if None not in (tr, mtr) and tr < mtr:  # type: ignore[operator]
            yield _hit(
                "PL47",
                f"{path}.coverage.split_margin.train_observations",
                tr,
                f">= {mtr}",
                "fewer train observations than the minimum",
            )
        if None not in (te, mte) and te < mte:  # type: ignore[operator]
            yield _hit(
                "PL47",
                f"{path}.coverage.split_margin.test_observations",
                te,
                f">= {mte}",
                "fewer test observations than the minimum",
            )


@rule(
    "PL48",
    (F4_FACTOR_TAB,),
    "factor source lag against the cutoff is within the allowed gap",
)
def pl48(panel: Panel) -> Iterator[Hit]:
    for path, s in _lens_series(panel):
        cov = s.get("coverage") or {}
        lag = num(cov.get("source_lag_calendar_days"))
        if lag is not None and lag > FACTOR_STALE_DAYS:
            yield _hit(
                "PL48",
                f"{path}.coverage.source_lag_calendar_days",
                lag,
                f"<= {FACTOR_STALE_DAYS}",
                "factor data lag the target by more than the allowed days",
            )
        end = to_date(cov.get("factor_observed_end"))
        if panel.cutoff and end and (panel.cutoff - end).days > FACTOR_STALE_DAYS:
            yield _hit(
                "PL48",
                f"{path}.coverage.factor_observed_end",
                str(end),
                f"within {FACTOR_STALE_DAYS} days of the cutoff",
                "factor series end long before the cutoff",
            )


@rule(
    "PL49",
    (F6_STATUS_TOKEN, F2_UNDERWRITING_GAP),
    "every status token appears in the declared vocabulary of its field",
)
def pl49(panel: Panel) -> Iterator[Hit]:
    status = panel.data.get("fact_status")
    allowed = {
        "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        "PARTIAL",
        "UNAVAILABLE",
        "NOT_APPLICABLE",
        "RECONCILED",
    }
    if isinstance(status, Mapping):
        for k, v in status.items():
            if str(v) not in allowed:
                yield _hit(
                    "PL49",
                    f"$.fact_status.{k}",
                    v,
                    ", ".join(sorted(allowed)),
                    "a status token outside the declared set",
                )


@rule("PL50", (F5_CHARTS,), "a table and its chart end on the same week")
def pl50(panel: Panel) -> Iterator[Hit]:
    wk = _get(panel.data, "weekly_returns", "weeks")
    through = to_date(_get(panel.data, "weekly_returns", "current_week", "week_start"))
    if isinstance(wk, list) and wk and through:
        last = to_date(wk[-1])
        if last and abs((last - through).days) > 7:
            yield _hit(
                "PL50",
                "$.weekly_returns.weeks",
                {"last_week": str(last), "current_week": str(through)},
                "within one week",
                "the weekly table ends away from the current week",
            )


def evaluate(
    data: Mapping[str, Any], cutoff: date | None = None, max_per_rule: int = 5
) -> list[Hit]:
    """Run every rule; a rule that raises is a hit itself (a rule that cannot run cannot pass).

    A rule that repeats one defect on many series reports its first ``max_per_rule`` hits and
    one summary row with the count, so the finding list stays readable and the count is kept."""

    panel = Panel(data, cutoff)
    hits: list[Hit] = []
    for r in RULES:
        try:
            found = list(r.run(panel))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            found = [
                Hit(
                    r.rule_id,
                    r.finds,
                    "(rule)",
                    type(exc).__name__,
                    "the rule runs",
                    f"rule crashed: {exc}",
                )
            ]
        hits.extend(found[:max_per_rule])
        if len(found) > max_per_rule:
            hits.append(
                Hit(
                    r.rule_id,
                    r.finds,
                    "(more)",
                    len(found) - max_per_rule,
                    "no further occurrences",
                    f"{len(found) - max_per_rule} further occurrence(s) of {r.rule_id} not listed",
                )
            )
    return hits


def rule_table() -> list[dict[str, Any]]:
    return [
        {"rule": r.rule_id, "finds": list(r.finds), "class": r.klass, "asserts": r.text}
        for r in RULES
    ]


__all__ = ["Hit", "Panel", "RULES", "evaluate", "rule_table"]


# ============================================================================================
# Consumer check (find 8): a contract that no output carries (repository-level)
# ============================================================================================

CONSUMER_RULE = "PL51"
CONSUMER_TEXT = (
    "every contract file reaches the panel payload by one of its ids, or is declared internal "
    "with a reason in ops/contract-consumers.json"
)


def contract_ids(document: Mapping[str, Any]) -> list[str]:
    """The identifying strings of a contract: its top-level ``*_id`` values and its ``schema``."""

    ids = [
        value
        for key, value in document.items()
        if isinstance(value, str)
        and (key.endswith("_id") or key == "schema")
        and len(value) >= 6
    ]
    return sorted(set(ids))


def contract_consumer_hits(
    contracts: Mapping[str, Mapping[str, Any]],
    payload_text: str,
    internal: Mapping[str, str],
) -> list[Hit]:
    """One hit per contract file with no id in the payload and no declared-internal entry.

    ``contracts`` maps a repository path to the decoded file; ``payload_text`` is the raw
    ``dashboard_data.json``; ``internal`` maps a path to the reason it is not rendered."""

    hits = []
    for path, document in sorted(contracts.items()):
        ids = contract_ids(document)
        carried = any(i in payload_text for i in ids) or Path(path).stem in payload_text
        reason = str(internal.get(path) or "").strip()
        if not carried and not reason:
            hits.append(
                Hit(
                    CONSUMER_RULE,
                    (F8_UNCONSUMED_CONTRACT,),
                    path,
                    {"ids": ids},
                    CONSUMER_TEXT,
                    "a contract exists but no panel block carries it and nothing says it is internal",
                )
            )
    return hits

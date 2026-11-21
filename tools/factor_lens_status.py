"""Factor lens Stage 1: per-panel staleness states, labels and run-failing assertions.

Pure, stdlib-only library that wraps the numbers the decomposition already publishes.
It computes no regression. It states, for every panel, the staleness state with its exit,
the last observation date and age, and it fails the run when a panel rule is broken
(missing last observation, lens identity, window shift, splice, significance marks on
live-window increments).  Emits ``factor-lens-status.json``.

Semantics follow ``FACTOR-LENS-DECISION-v6`` sections 7 to 9 and the round-5 gating list.
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

SCHEMA = "qe-factor-lens-status/1"

FRENCH_ALLOWANCE_DAYS = 45
ETF_FRESH_DAYS = 7
ETF_STALE_DAYS = 14
P123_FRESH_DAYS = 45
P123_STALE_DAYS = 90
# Round-5 R5-4: live bootstrap intervals average 0.41 wide; 0.1 is too small to call a difference real.
NOISE_ADJ_R2 = 0.2
# Round-5 gating: no lens explains the strategy (every adjusted R-squared at or below 0.02).
NOT_APPLICABLE_ADJ_R2 = 0.02
DISAGREEMENT_MIN_ABS_T = 2.0

FRENCH_FACTORS: dict[str, tuple[str, ...]] = {
    "US": ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"),
    "Europe": ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "WML"),
}

NOISE_RULE_TEXT = (
    "Differences in adjusted R-squared below about 0.2 are noise at live-window length; "
    "otherwise print the bootstrap interval."
)
BRIDGE_LABELS = {
    ("Europe", "market_only"): (
        "market only, materially weaker than French for Europe, never spliced to French levels"
    ),
    ("Europe", "enlarged"): (
        "enlarged ETFs, materially weaker than French for Europe, never spliced to French levels"
    ),
    ("US", "market_only"): "ETF bridge, never spliced to French levels",
    ("US", "enlarged"): "ETF bridge, never spliced to French levels",
}
DISAGREEMENT_LABEL = "informational, not validated"

_SIGNIFICANCE_KEYS = frozenset(
    {
        "p",
        "p_value",
        "p_classical",
        "p_nw",
        "p_wild",
        "stars",
        "star",
        "significant",
        "significance",
        "mark",
    }
)


class StatusError(ValueError):
    """One or more panel rules failed; ``failures`` lists every one."""

    def __init__(self, failures: Sequence[str] | str) -> None:
        self.failures = [failures] if isinstance(failures, str) else list(failures)
        super().__init__("; ".join(self.failures))


# --- dates ---------------------------------------------------------------------------


def _parse_date(value: object, what: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise StatusError(f"{what} is required as an ISO date, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise StatusError(f"{what} is not a valid date: {value!r}") from exc


def _as_date(value: date | str, what: str) -> date:
    return value if isinstance(value, date) else _parse_date(value, what)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _prev_month_end(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def _next_month_end(d: date) -> date:
    first_next = d.replace(day=1) + timedelta(days=32)
    return _month_end(first_next.year, first_next.month)


def required_french_month(evaluation_date: date) -> date:
    """Latest month end whose month end plus 45 days is on or before the evaluation date."""
    candidate = _month_end(evaluation_date.year, evaluation_date.month)
    while candidate + timedelta(days=FRENCH_ALLOWANCE_DAYS) > evaluation_date:
        candidate = _prev_month_end(candidate)
    return candidate


def _age(last: date, evaluation_date: date, what: str) -> int:
    age = (evaluation_date - last).days
    if age < 0:
        raise StatusError(
            f"{what} {last} is after the evaluation date {evaluation_date}"
        )
    return age


# --- panels --------------------------------------------------------------------------


def _panel(
    *,
    panel_id: str,
    kind: str,
    state: str,
    last: date,
    age: int,
    exit_: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "panel_id": panel_id,
        "kind": kind,
        "state": state,
        "last_observation": last.isoformat(),
        "age_days": age,
        "exit": exit_,
        "contributes_to_headline": state != "HARD_STALE",
        **extra,
    }


def french_panel(
    *,
    panel_id: str,
    region: str,
    last_observation: date | str,
    evaluation_date: date,
    factors: Sequence[str],
) -> dict[str, Any]:
    if region not in FRENCH_FACTORS:
        raise StatusError(f"{panel_id}: unknown French region {region!r}")
    last = _as_date(last_observation, f"{panel_id} last_observation")
    age = _age(last, evaluation_date, f"{panel_id} last_observation")
    if list(factors) != list(FRENCH_FACTORS[region]):
        raise StatusError(
            f"{panel_id}: lens identity: factors {list(factors)} != {list(FRENCH_FACTORS[region])}"
        )
    required = required_french_month(evaluation_date)
    next_required = _next_month_end(required)
    compliant = (last.year, last.month) >= (required.year, required.month)
    return _panel(
        panel_id=panel_id,
        kind="french",
        state="COMPLIANT" if compliant else "HARD_STALE",
        last=last,
        age=age,
        exit_=(
            f"holds until {(next_required + timedelta(days=FRENCH_ALLOWANCE_DAYS)).isoformat()}, "
            f"then needs {next_required:%Y-%m} data"
            if compliant
            else f"exit when {required:%Y-%m} data arrive"
        ),
        region=region,
        factors=list(factors),
        required_month_end=required.isoformat(),
        next_required_month_end=next_required.isoformat(),
        next_due_date=(
            next_required + timedelta(days=FRENCH_ALLOWANCE_DAYS)
        ).isoformat(),
    )


def _age_band_state(age: int, fresh: int, stale: int) -> str:
    return "FRESH" if age <= fresh else "STALE" if age <= stale else "HARD_STALE"


def etf_bridge_panel(
    *,
    panel_id: str,
    region: str,
    last_observation: date | str,
    evaluation_date: date,
    bridge_ids: Sequence[str],
    configured_ids: Sequence[str],
) -> dict[str, Any]:
    last = _as_date(last_observation, f"{panel_id} last_observation")
    age = _age(last, evaluation_date, f"{panel_id} last_observation")
    if list(bridge_ids) != list(configured_ids):
        raise StatusError(
            f"{panel_id}: lens identity: bridge ids {list(bridge_ids)} != configured {list(configured_ids)}"
        )
    state = _age_band_state(age, ETF_FRESH_DAYS, ETF_STALE_DAYS)
    return _panel(
        panel_id=panel_id,
        kind="etf_bridge",
        state=state,
        last=last,
        age=age,
        exit_="a new ETF return within 7 days of the report date",
        region=region,
        bridge_ids=list(bridge_ids),
    )


def p123_panel(
    *,
    panel_id: str,
    last_observation: date | str,
    evaluation_date: date,
    definition_hashes_present: bool,
) -> dict[str, Any]:
    """Stage 2 panel state; ``last_observation`` is the capture date."""
    last = _as_date(last_observation, f"{panel_id} last_observation")
    age = _age(last, evaluation_date, f"{panel_id} last_observation")
    return _panel(
        panel_id=panel_id,
        kind="labelled_panel",
        state=_age_band_state(age, P123_FRESH_DAYS, P123_STALE_DAYS),
        last=last,
        age=age,
        exit_="a new capture",
        labels=[] if definition_hashes_present else ["UNHASHED"],
    )


def validate_panels(panels: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every panel record must carry a valid last observation and age."""
    failures: list[str] = []
    for panel in panels:
        pid = panel.get("panel_id", "<unnamed>")
        last = panel.get("last_observation")
        age = panel.get("age_days")
        if not isinstance(last, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last):
            failures.append(f"{pid}: last_observation missing or invalid")
        if not isinstance(age, int) or isinstance(age, bool) or age < 0:
            failures.append(f"{pid}: age_days missing or invalid")
        if not panel.get("state"):
            failures.append(f"{pid}: state missing")
    return failures


# --- windows -------------------------------------------------------------------------


def check_weekly_windows(
    windows: Sequence[Mapping[str, Any]], trading_days: Sequence[date]
) -> list[str]:
    """Each window equals the trading days of its ISO Monday-to-Friday week."""
    tdays = set(trading_days)
    failures: list[str] = []
    for window in windows:
        try:
            start = _parse_date(window.get("week_start"), "week_start")
            days = [_parse_date(d, "window day") for d in window.get("days", [])]
        except StatusError as exc:
            failures.append(str(exc))
            continue
        if start.weekday() != 0:
            failures.append(f"window {start}: week_start is not a Monday")
            continue
        expected = [
            start + timedelta(days=i)
            for i in range(5)
            if start + timedelta(days=i) in tdays
        ]
        if days != expected:
            failures.append(
                f"window {start}: days {[d.isoformat() for d in days]} != trading days "
                f"{[d.isoformat() for d in expected]}"
            )
    return failures


# --- strategy applicability, noise, significance -------------------------------------


def attribution_status(adj_r2_by_lens: Mapping[str, float]) -> dict[str, Any]:
    if not adj_r2_by_lens:
        raise StatusError(
            "attribution_status needs at least one lens adjusted R-squared"
        )
    if all(v <= NOT_APPLICABLE_ADJ_R2 for v in adj_r2_by_lens.values()):
        return {
            "state": "NOT_APPLICABLE",
            "reason": (
                f"adjusted R-squared at or below {NOT_APPLICABLE_ADJ_R2} under every lens; "
                "no factor attribution is shown"
            ),
        }
    return {"state": "APPLICABLE", "reason": None}


def _interval_ok(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
        and value[0] <= value[1]
    )


def validate_comparisons(comparisons: Sequence[Mapping[str, Any]]) -> list[str]:
    """Live-window comparisons carry the noise rule or an interval and no significance marks."""
    failures: list[str] = []
    for cmp in comparisons:
        if cmp.get("window") != "live":
            continue
        name = cmp.get("strategy", "<unnamed>")
        threshold = cmp.get("noise_threshold")
        has_threshold = (
            isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
        ) and threshold >= NOISE_ADJ_R2
        if not has_threshold and not _interval_ok(cmp.get("interval")):
            failures.append(
                f"{name}: live comparison needs a noise threshold of at least {NOISE_ADJ_R2} "
                "or a valid [low, high] interval"
            )
        marks = sorted(k for k in cmp if k in _SIGNIFICANCE_KEYS)
        if marks:
            failures.append(
                f"{name}: significance marks {marks} are not allowed on live-window increments"
            )
        for value in cmp.values():
            if isinstance(value, str) and re.fullmatch(r"\*+", value.strip()):
                failures.append(f"{name}: significance star on a live-window increment")
    return failures


# --- disagreement rule ---------------------------------------------------------------


def disagreement_state(
    *,
    window: str,
    french_loading: float | None,
    french_t: float | None,
    p123_loading: float | None,
    p123_t: float | None,
) -> dict[str, str]:
    """French vs P123 loading disagreement: informational, never a plain "no" when untestable."""
    inputs = (french_loading, french_t, p123_loading, p123_t)
    if window == "live" or any(v is None for v in inputs):
        state = "NOT_TESTABLE"
    else:
        assert french_loading is not None and french_t is not None
        assert p123_loading is not None and p123_t is not None
        opposite = french_loading * p123_loading < 0
        both_t = min(abs(french_t), abs(p123_t)) >= DISAGREEMENT_MIN_ABS_T
        state = "FIRES" if opposite and both_t else "NO_DISAGREEMENT"
    return {"state": state, "label": DISAGREEMENT_LABEL}


# --- bridge labels -------------------------------------------------------------------


def bridge_record(
    *, region: str, enlarged_configured: bool, spliced: bool = False
) -> dict[str, Any]:
    if spliced:
        raise StatusError(f"{region} bridge: splice onto French levels is not allowed")
    coverage = "enlarged" if enlarged_configured else "market_only"
    label = BRIDGE_LABELS.get((region, coverage))
    if label is None:
        raise StatusError(f"unknown bridge region {region!r}")
    return {
        "region": region,
        "coverage": coverage,
        "never_spliced": True,
        "label": label,
    }


# --- document ------------------------------------------------------------------------


def build_status(
    *,
    evaluation_date: date,
    panels: Sequence[Mapping[str, Any]],
    strategies: Sequence[Mapping[str, Any]] = (),
    comparisons: Sequence[Mapping[str, Any]] = (),
    bridges: Sequence[Mapping[str, Any]] = (),
    disagreements: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    failures: list[str] = []
    if not panels:
        failures.append("at least one panel is required")
    failures += validate_panels(panels)
    failures += validate_comparisons(comparisons)
    for bridge in bridges:
        if bridge.get("never_spliced") is not True:
            failures.append(
                f"{bridge.get('region', '<unnamed>')} bridge: never_spliced must be true"
            )
    strategy_rows: list[dict[str, Any]] = []
    for strategy in strategies:
        try:
            attribution = attribution_status(strategy.get("adj_r2", {}))
        except StatusError as exc:
            failures.append(f"{strategy.get('strategy', '<unnamed>')}: {exc}")
            continue
        strategy_rows.append({**strategy, "attribution": attribution})
    if failures:
        raise StatusError(failures)
    return {
        "schema": SCHEMA,
        "evaluation_date": evaluation_date.isoformat(),
        "noise_rule": {"adj_r2_threshold": NOISE_ADJ_R2, "text": NOISE_RULE_TEXT},
        "panels": [dict(p) for p in panels],
        "strategies": strategy_rows,
        "comparisons": [dict(c) for c in comparisons],
        "bridges": [dict(b) for b in bridges],
        "disagreements": [dict(d) for d in disagreements],
    }


def render_status(doc: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON with LF line endings (bytes, so no platform newline rewrite)."""
    return (
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def write_status(doc: Mapping[str, Any], path: Path) -> None:
    with path.open("xb") as fh:  # create-only
        fh.write(render_status(doc))


# --- CLI -----------------------------------------------------------------------------


def _build_panel(spec: Mapping[str, Any], evaluation_date: date) -> dict[str, Any]:
    kind = spec.get("kind")
    common = {
        "panel_id": spec.get("panel_id", "<unnamed>"),
        "evaluation_date": evaluation_date,
    }
    if kind == "french":
        return french_panel(
            region=spec.get("region", ""),
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            factors=spec.get("factors", []),
            **common,
        )
    if kind == "etf_bridge":
        return etf_bridge_panel(
            region=spec.get("region", ""),
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            bridge_ids=spec.get("bridge_ids", []),
            configured_ids=spec.get("configured_ids", []),
            **common,
        )
    if kind == "labelled_panel":
        return p123_panel(
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            definition_hashes_present=bool(spec.get("definition_hashes_present")),
            **common,
        )
    raise StatusError(f"{common['panel_id']}: unknown panel kind {kind!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit factor-lens-status.json from a panel spec."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.input.read_bytes())
        evaluation_date = _parse_date(spec.get("evaluation_date"), "evaluation_date")
        failures: list[str] = []
        panels: list[dict[str, Any]] = []
        for item in spec.get("panels", []):
            try:
                panels.append(_build_panel(item, evaluation_date))
            except StatusError as exc:
                failures += exc.failures
        if failures:
            raise StatusError(failures)
        doc = build_status(
            evaluation_date=evaluation_date,
            panels=panels,
            strategies=spec.get("strategies", []),
            comparisons=spec.get("comparisons", []),
            bridges=spec.get("bridges", []),
            disagreements=spec.get("disagreements", []),
        )
        write_status(doc, args.output)
    except StatusError as exc:
        for failure in exc.failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Factor lens Stage 1: per-panel staleness states, labels and run-failing assertions.

Pure, stdlib-only library that wraps the numbers the decomposition already publishes.
It computes no regression. It states, for every panel, the staleness state with its exit,
the last observation date and age, and it fails the run when a panel rule is broken
(missing last observation, forged panel record, lens identity, window shift, splice,
significance marks on live-window increments).  Emits ``factor-lens-status.json``.

Semantics follow ``FACTOR-LENS-DECISION-v6`` sections 7 to 9 and the round-5 gating list.
Stage 2 (the labelled P123 panel) is out of scope: a panel of that kind raises
``NotImplementedError`` so it cannot pass unchecked.
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, timedelta
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

SCHEMA = "qe-factor-lens-status/1"

FRENCH_ALLOWANCE_DAYS = 45
# Owner ruling: single source for the French headline age ceiling, in days from the MONTH END of
# the last observation's month to the evaluation date. Independent of
# acceptance.validate.plausibility.FACTOR_STALE_DAYS (10), a different check that stays untouched.
FRENCH_HEADLINE_LAG_DAYS = 75
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

# Only these windows are not live. Anything else, including a missing window, is live.
_NON_LIVE_WINDOWS = frozenset({"full", "backtest"})
_SIGNIFICANCE_KEY = re.compile(
    r"^p(_|$)|star|signif|^t(_|$)|wild_bootstrap|bootstrap|pvalue|t_stat", re.IGNORECASE
)
_LOADING_KEY = re.compile(r"loading|beta|coef", re.IGNORECASE)


class StatusError(ValueError):
    """One or more panel rules failed; ``failures`` lists every one."""

    def __init__(self, failures: Sequence[str] | str) -> None:
        self.failures = [failures] if isinstance(failures, str) else list(failures)
        super().__init__("; ".join(self.failures))


# --- helpers -------------------------------------------------------------------------


def is_live_window(window: object) -> bool:
    """Any window not explicitly full or backtest (trimmed, case-insensitive) is live."""
    return not (isinstance(window, str) and window.strip().lower() in _NON_LIVE_WINDOWS)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


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


def _month_end_of(d: date) -> date:
    return _month_end(d.year, d.month)


def _prev_month_end(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def _next_month_end(d: date) -> date:
    first_next = d.replace(day=1) + timedelta(days=32)
    return _month_end(first_next.year, first_next.month)


def required_french_month(evaluation_date: date) -> date:
    """Latest month end whose month end plus 45 days is on or before the evaluation date."""
    candidate = _month_end_of(evaluation_date)
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
    """French panel; age is counted from the month end of the last observation's month."""
    if region not in FRENCH_FACTORS:
        raise StatusError(f"{panel_id}: unknown French region {region!r}")
    last = _as_date(last_observation, f"{panel_id} last_observation")
    _age(last, evaluation_date, f"{panel_id} last_observation")  # rejects a future date
    if list(factors) != list(FRENCH_FACTORS[region]):
        raise StatusError(
            f"{panel_id}: lens identity: factors {list(factors)} != {list(FRENCH_FACTORS[region])}"
        )
    last_month_end = _month_end_of(last)
    age = max((evaluation_date - last_month_end).days, 0)
    required = required_french_month(evaluation_date)
    compliant = last_month_end >= required and age <= FRENCH_HEADLINE_LAG_DAYS
    # The next month whose data are not yet in hand, and the date it becomes due.
    next_needed = _next_month_end(max(required, last_month_end))
    due = next_needed + timedelta(days=FRENCH_ALLOWANCE_DAYS)
    return _panel(
        panel_id=panel_id,
        kind="french",
        state="COMPLIANT" if compliant else "HARD_STALE",
        last=last,
        age=age,
        exit_=(
            f"stale from {due.isoformat()} unless {next_needed:%Y-%m} data arrive"
            if compliant
            else f"exit when {required:%Y-%m} data arrive"
        ),
        region=region,
        factors=list(factors),
        required_month_end=required.isoformat(),
        headline_lag_days=FRENCH_HEADLINE_LAG_DAYS,
        next_required_month_end=next_needed.isoformat(),
        next_due_date=due.isoformat(),
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
    return _panel(
        panel_id=panel_id,
        kind="etf_bridge",
        state=_age_band_state(age, ETF_FRESH_DAYS, ETF_STALE_DAYS),
        last=last,
        age=age,
        exit_="a new ETF return within 7 days of the report date",
        region=region,
        bridge_ids=list(bridge_ids),
        configured_ids=list(configured_ids),
    )


def p123_panel(
    *,
    panel_id: str,
    last_observation: date | str,
    evaluation_date: date,
    definition_hashes_present: object,
) -> dict[str, Any]:
    """Stage 2 panel state; ``last_observation`` is the capture date.

    Only a real JSON ``true`` clears the UNHASHED label.
    """
    last = _as_date(last_observation, f"{panel_id} last_observation")
    age = _age(last, evaluation_date, f"{panel_id} last_observation")
    return _panel(
        panel_id=panel_id,
        kind="labelled_panel",
        state=_age_band_state(age, P123_FRESH_DAYS, P123_STALE_DAYS),
        last=last,
        age=age,
        exit_="a new capture",
        labels=[] if definition_hashes_present is True else ["UNHASHED"],
    )


def check_labelled_panel_rows(panel: Mapping[str, Any]) -> None:
    """Placeholder: row-identity checks for the labelled panel belong to Stage 2."""
    raise NotImplementedError(
        f"Stage 2: labelled panel {panel.get('panel_id', '<unnamed>')!r} row-identity checks "
        "are not implemented; the labelled panel cannot ship before them"
    )


def _derive_panel(spec: Mapping[str, Any], evaluation_date: date) -> dict[str, Any]:
    kind = spec.get("kind")
    pid = spec.get("panel_id", "<unnamed>")
    if kind == "french":
        return french_panel(
            panel_id=pid,
            region=spec.get("region", ""),
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            evaluation_date=evaluation_date,
            factors=spec.get("factors", []),
        )
    if kind == "etf_bridge":
        return etf_bridge_panel(
            panel_id=pid,
            region=spec.get("region", ""),
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            evaluation_date=evaluation_date,
            bridge_ids=spec.get("bridge_ids", []),
            configured_ids=spec.get("configured_ids", []),
        )
    if kind == "labelled_panel":
        derived = p123_panel(
            panel_id=pid,
            last_observation=spec.get("last_observation"),  # type: ignore[arg-type]
            evaluation_date=evaluation_date,
            definition_hashes_present=spec.get("definition_hashes_present"),
        )
        check_labelled_panel_rows(derived)
        return derived
    raise StatusError(f"{pid}: unknown panel kind {kind!r}")


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


def verify_panels(
    panels: Sequence[Mapping[str, Any]], evaluation_date: date
) -> list[str]:
    """Do not trust a panel record: re-derive it and reject any declared field that differs."""
    failures = validate_panels(panels)
    for panel in panels:
        pid = panel.get("panel_id", "<unnamed>")
        try:
            derived = _derive_panel(panel, evaluation_date)
        except StatusError as exc:
            failures += exc.failures
            continue
        failures += [
            f"{pid}: declared {key}={panel.get(key)!r} but derived {value!r}"
            for key, value in derived.items()
            if panel.get(key) != value
        ]
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
    if not isinstance(adj_r2_by_lens, Mapping) or not adj_r2_by_lens:
        raise StatusError(
            "attribution_status needs at least one lens adjusted R-squared"
        )
    bad = sorted(str(k) for k, v in adj_r2_by_lens.items() if not _is_number(v))
    if bad:
        raise StatusError(
            f"adjusted R-squared must be a finite number for lens(es) {bad}"
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
        and all(_is_number(x) for x in value)
        and value[0] < value[1]  # a zero-width interval says nothing
    )


def _significance_findings(value: object, path: str = "") -> list[str]:
    """Key-pattern and string-content scan, recursive through nested dicts and lists."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if _SIGNIFICANCE_KEY.search(str(key)):
                found.append(where)
            found += _significance_findings(item, where)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += _significance_findings(item, f"{path}[{index}]")
    elif isinstance(value, str) and "*" in value:
        found.append(path or "<value>")
    return found


def validate_comparisons(comparisons: Sequence[Mapping[str, Any]]) -> list[str]:
    """Live-window comparisons carry the noise rule or an interval and no significance marks."""
    failures: list[str] = []
    for cmp in comparisons:
        if not is_live_window(cmp.get("window")):
            continue
        name = cmp.get("strategy", "<unnamed>")
        threshold = cmp.get("noise_threshold")
        has_threshold = (
            isinstance(threshold, (int, float))
            and _is_number(threshold)
            and threshold >= NOISE_ADJ_R2
        )
        if not has_threshold and not _interval_ok(cmp.get("interval")):
            failures.append(
                f"{name}: live comparison needs a noise threshold of at least {NOISE_ADJ_R2} "
                "or a valid [low, high] interval with low < high"
            )
        marks = _significance_findings(cmp)
        if marks:
            failures.append(
                f"{name}: significance marks at {marks} are not allowed on live-window increments"
            )
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
    """French vs P123 loading disagreement: informational.

    Testable only outside live windows and with both loadings finite and both |t| >= 2;
    anything else is NOT_TESTABLE, never a plain "no".
    """
    values = (french_loading, french_t, p123_loading, p123_t)
    testable = (
        not is_live_window(window)
        and all(_is_number(v) for v in values)
        and min(abs(french_t), abs(p123_t)) >= DISAGREEMENT_MIN_ABS_T  # type: ignore[arg-type]
    )
    if not testable:
        state = "NOT_TESTABLE"
    else:
        assert french_loading is not None and p123_loading is not None
        state = "FIRES" if french_loading * p123_loading < 0 else "NO_DISAGREEMENT"
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


def validate_bridges(bridges: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every bridge is checked against the canonical record; a spliced or mislabelled one fails."""
    failures: list[str] = []
    for bridge in bridges:
        region, coverage = bridge.get("region"), bridge.get("coverage")
        name = f"{region} bridge"
        if bridge.get("never_spliced") is not True:
            failures.append(f"{name}: never_spliced must be literal true")
        expected = BRIDGE_LABELS.get((region, coverage))  # type: ignore[arg-type]
        if expected is None:
            failures.append(f"{name}: unknown region/coverage {region!r}/{coverage!r}")
        elif bridge.get("label") != expected:
            failures.append(
                f"{name}: label {bridge.get('label')!r} != required {expected!r}"
            )
    return failures


# --- document ------------------------------------------------------------------------


def build_status(
    *,
    evaluation_date: date,
    panels: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]] | None = None,
    trading_days: Sequence[date] | None = None,
    strategies: Sequence[Mapping[str, Any]] = (),
    comparisons: Sequence[Mapping[str, Any]] = (),
    bridges: Sequence[Mapping[str, Any]] = (),
    disagreements: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    failures: list[str] = []
    if not panels:
        failures.append("at least one panel is required")
    failures += verify_panels(panels, evaluation_date)
    if not windows or not trading_days:
        failures.append(
            "weekly windows and trading_days are required and must not be empty"
        )
    else:
        failures += check_weekly_windows(windows, trading_days)
    failures += validate_comparisons(comparisons)
    failures += validate_bridges(bridges)
    strategy_rows: list[dict[str, Any]] = []
    for strategy in strategies:
        try:
            attribution = attribution_status(strategy.get("adj_r2", {}))
        except StatusError as exc:
            failures.append(f"{strategy.get('strategy', '<unnamed>')}: {exc}")
            continue
        row = {**strategy, "attribution": attribution}
        if attribution["state"] == "NOT_APPLICABLE":
            row = {k: v for k, v in row.items() if not _LOADING_KEY.search(str(k))}
        strategy_rows.append(row)
    if failures:
        raise StatusError(failures)
    return {
        "schema": SCHEMA,
        "evaluation_date": evaluation_date.isoformat(),
        "noise_rule": {"adj_r2_threshold": NOISE_ADJ_R2, "text": NOISE_RULE_TEXT},
        "panels": [dict(p) for p in panels],
        "windows": [dict(w) for w in windows or ()],
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
                panels.append(_derive_panel(item, evaluation_date))
            except StatusError as exc:
                failures += exc.failures
        if failures:
            raise StatusError(failures)
        trading_days = [
            _parse_date(d, "trading day") for d in spec.get("trading_days", [])
        ]
        doc = build_status(
            evaluation_date=evaluation_date,
            panels=panels,
            windows=spec.get("windows"),
            trading_days=trading_days,
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
    except NotImplementedError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

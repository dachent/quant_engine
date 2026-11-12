"""Claim table: machine checks of the static prose claims against the payload.

A control-panel or report sentence that says "every", "all", "no", "only", a
count, a basis (parent / sleeve / account) or an epoch / week / evidence-mode
fact is a claim about the payload.  Each claim gets one ROWS entry here: the
sentence is located in the shipped renderer or builder by a regular expression
(``needles``), and ``predicate`` evaluates it over the loaded panel
(``dashboard_data.json``) and report (``portfolio-report-data.json``) payloads.

Rules the table enforces together with ``claim_table_runner.py``:

* a row whose sentence can no longer be found is STALE (the wording changed and
  the assertion must be re-stated with it);
* a static sentence with a claim pattern and no row and no allowlist entry is
  UNCOVERED (see ``claim_table_scan.py``);
* every predicate can pass and fail: ``break_fixture`` turns the synthetic
  passing payload of ``claim_table_fixtures.py`` into a failing one.

The module holds no real data.  Predicates are pure functions of the payload.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Any

Json = Any


class Verdict(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


Result = tuple[Verdict, str]


@dataclass
class Payload:
    """The two loaded payloads: panel ``dashboard_data`` and report data."""

    panel: dict[str, Json]
    report: dict[str, Json]
    # Optional generation-bundle evidence (``claim_table_runner.load_bundle``); None = not given.
    bundle: dict[str, Json] | None = None


@dataclass(frozen=True)
class Row:
    id: str
    klass: str  # universal | count | basis | epoch | coverage | status
    view: str
    file: str  # repository-relative file that holds the sentence
    needles: tuple[str, ...]  # regexes (re.S) locating the sentence in ``file``
    claim: str  # the claim, in words
    predicate: Callable[[Payload], Result]
    break_fixture: Callable[[Payload], None] | None
    rendered_view: str = ""
    # Applied to the synthetic passing payload before the predicate is exercised,
    # for rows whose passing state needs data the base payload does not carry.
    pass_fixture: Callable[[Payload], None] | None = None
    # Not assertable from the payload: the predicate returns SKIP with the reason.
    unverifiable: bool = False


def ok(evidence: str) -> Result:
    return Verdict.PASS, evidence


def bad(evidence: str) -> Result:
    return Verdict.FAIL, evidence


def skip(evidence: str) -> Result:
    return Verdict.SKIP, evidence


def get(obj: Json, *path: str | int, default: Json = None) -> Json:
    """Nested lookup; a missing key or wrong type yields ``default``."""

    for key in path:
        if isinstance(obj, Mapping) and isinstance(key, str) and key in obj:
            obj = obj[key]
        elif (
            isinstance(obj, Sequence)
            and not isinstance(obj, str)
            and isinstance(key, int)
            and -len(obj) <= key < len(obj)
        ):
            obj = obj[key]
        else:
            return default
    return obj


def rows_of(value: Json) -> list[dict[str, Json]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def dec(value: Json) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def names(items: Iterable[str], limit: int = 6) -> str:
    listed = sorted(items)
    more = f" (+{len(listed) - limit} more)" if len(listed) > limit else ""
    return ", ".join(listed[:limit]) + more


# --- shared payload views ---------------------------------------------------


def _mode_lines(payload: Payload, mode: str) -> list[dict[str, Json]]:
    return rows_of(get(payload.panel, "performance_tab", "modes", mode, "lines"))


def _segments(line: Mapping[str, Json]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    origin = line.get("origin")
    if isinstance(origin, list):
        for seg in origin:
            if isinstance(seg, list) and len(seg) == 3:
                found.append((int(seg[0]), int(seg[1]), str(seg[2])))
    return found


def _cutoff(payload: Payload) -> str | None:
    value = get(payload.panel, "account_manifest", "calculation_cutoff_date")
    return value if isinstance(value, str) else None


def _manifest_rows(payload: Payload) -> list[dict[str, Json]]:
    return rows_of(get(payload.panel, "account_manifest", "rows"))


# --- known-false sentences and universal / epoch / basis claims -------------


def p_evidence_separate(payload: Payload) -> Result:
    """Backtest history is joined to a live line only in LIVE_PLUS_BACKTEST, and labelled by origin."""

    leaked = [
        str(line.get("series_id"))
        for line in _mode_lines(payload, "LIVE_ONLY")
        if any(label == "BACKTEST" for _s, _e, label in _segments(line))
    ]
    if leaked:
        return bad(
            f"{len(leaked)} LIVE_ONLY line(s) carry backtest segments: {names(leaked)}"
        )
    lines = _mode_lines(payload, "LIVE_PLUS_BACKTEST")
    if not lines:
        return skip("no LIVE_PLUS_BACKTEST lines in the performance tab")
    unlabelled = [
        str(line.get("series_id"))
        for line in lines
        if line.get("kind") == "STRATEGY" and not _segments(line)
    ]
    if unlabelled:
        return bad(
            f"{len(unlabelled)} LIVE_PLUS_BACKTEST strategy line(s) carry no origin "
            f"labels: {names(unlabelled)}"
        )
    return ok(
        "no LIVE_ONLY line has backtest segments; every LIVE_PLUS_BACKTEST "
        f"strategy line ({len(lines)} lines) is labelled by origin"
    )


def b_evidence_separate(payload: Payload) -> None:
    line = _mode_lines(payload, "LIVE_ONLY")[0]
    line["origin"] = [[0, 2, "BACKTEST"], [3, 9, "ACCOUNT_LIVE"]]


def p_closed_epochs_current(payload: Payload) -> Result:
    cutoff = _cutoff(payload)
    rows = _manifest_rows(payload)
    if cutoff is None or not rows:
        return skip("no account manifest rows or cutoff")
    counted = [r for r in rows if r.get("status") == "OK" and r.get("end") == cutoff]
    handed = []
    for row in counted:
        value = dec(row.get("current_value"))
        if row.get("continuity_to_successor") and value not in (None, 0):
            handed.append(f"{row.get('strategy_id')} current {value:.2f}")
    if handed:
        return bad(
            f"{len(handed)} epoch(s) with a successor still carry a current value: "
            f"{names(handed)}"
        )
    states = sorted({str(r.get("lifecycle_state")) for r in counted})
    return ok(
        f"no epoch with a successor carries a current value; {len(counted)} epochs "
        f"end at the cutoff (states: {', '.join(states)}, printed from the rows)"
    )


def b_closed_epochs_current(payload: Payload) -> None:
    rows = _manifest_rows(payload)
    rows[0]["continuity_to_successor"] = {"successor": "next"}
    rows[0]["current_value"] = "3.81"


def f_inception_week_mixed(payload: Payload) -> None:
    for line in _mode_lines(payload, "LIVE_PLUS_BACKTEST"):
        if line.get("series_id") == "strategy:A":
            line["origin"] = [[0, 1, "BACKTEST"], [2, 2, "MIXED"], [3, 9, "ACCOUNT_LIVE"]]


def p_inception_week_mixed(payload: Payload) -> Result:
    checked = 0
    misplaced: list[str] = []
    for line in _mode_lines(payload, "LIVE_PLUS_BACKTEST"):
        if line.get("kind") != "STRATEGY":
            continue  # the sentence is about strategy rows
        segs = _segments(line)
        labels = [label for _s, _e, label in segs]
        if "MIXED" not in labels:
            continue
        checked += 1
        first = labels.index("MIXED")
        if "BACKTEST" not in labels[:first] or "LIVE" not in labels[first + 1 :]:
            misplaced.append(f"{line.get('series_id')}={'/'.join(labels)}")
    if not checked:
        return skip("no LIVE_PLUS_BACKTEST line has a MIXED week")
    if misplaced:
        return bad(
            f"{len(misplaced)} line(s) have a MIXED week with no backtest before or "
            f"live after it: {names(misplaced)}"
        )
    return ok(
        f"every MIXED week ({checked} lines) sits between backtest and live weeks"
    )


def b_inception_week_mixed(payload: Payload) -> None:
    for line in _mode_lines(payload, "LIVE_PLUS_BACKTEST"):
        if line.get("kind") == "STRATEGY":
            line["origin"] = [[0, 3, "MIXED"], [4, 9, "ACCOUNT_LIVE"]]
            break


_ONLY_CURRENT = re.compile(r"\bonly\b[^.;]*\bcurrent\b", re.I)


def p_positions_only_current(payload: Payload) -> Result:
    effects: list[tuple[str, str]] = []
    for source, rows in (
        ("panel", rows_of(payload.panel.get("coverage"))),
        ("report", rows_of(payload.report.get("coverage_data_quality"))),
    ):
        for row in rows:
            if row.get("item") == "positions":
                effect = get(row, "disposition", "effect")
                if isinstance(effect, str):
                    effects.append((source, effect))
    if not effects:
        return skip("no positions coverage disposition effect")
    historical = get(payload.panel, "position_snapshots", "historical_rows")
    if not isinstance(historical, int):
        historical = sum(
            1
            for r in rows_of(payload.report.get("positions"))
            if str(r.get("snapshot_status")).startswith("HISTORICAL")
        )
    wrong = [src for src, text in effects if _ONLY_CURRENT.search(text)]
    if wrong and historical > 0:
        return bad(
            f"positions effect says only current positions are reported "
            f"({'/'.join(sorted(set(wrong)))}) but {historical} historical "
            f"snapshot rows are reported"
        )
    return ok(f"effect is consistent with {historical} historical snapshot rows")


def b_positions_only_current(payload: Payload) -> None:
    for row in rows_of(payload.panel.get("coverage")):
        if row.get("item") == "positions":
            row["disposition"] = {"effect": "Only accepted current rows are reported."}
    payload.panel["position_snapshots"] = {"historical_rows": 5}


def p_gap_tested_count(payload: Payload) -> Result:
    strategies = rows_of(payload.panel.get("strategies"))
    scoped = [s for s in strategies if s.get("implementation_gap_status") is not None]
    if not scoped:
        return skip("no strategy carries implementation_gap_status")
    # The tile counts strategies whose implementation_gap_status is "tested" (provider versus
    # account evidence, a different test from Model vs actual's aligned window) and names the
    # others by status, so tested plus the named others equal the denominator.
    tested = sum(1 for s in scoped if s.get("implementation_gap_status") == "tested")
    others: dict[str, int] = {}
    for s in scoped:
        status = s.get("implementation_gap_status")
        if status != "tested":
            others[str(status)] = others.get(str(status), 0) + 1
    denominator = get(payload.panel, "counts", "strategies")
    if isinstance(denominator, int) and tested + sum(others.values()) != denominator:
        return bad(
            f"tested {tested} plus not tested {sum(others.values())} is not the "
            f"{denominator} strategies the tile divides by"
        )
    listed = ", ".join(f"{k} {v}" for k, v in sorted(others.items())) or "none"
    return ok(f"tested {tested} of {denominator}; not tested: {listed}")


def b_gap_tested_count(payload: Payload) -> None:
    payload.panel.setdefault("counts", {})["strategies"] = 99


def p_epoch_counts(payload: Payload) -> Result:
    performance = rows_of(payload.panel.get("performance"))
    manifest = _manifest_rows(payload)
    if not performance or not manifest:
        return skip("no performance rows or manifest rows")
    manifest_ids = {r.get("account_id") for r in manifest}
    missing = [
        str(r.get("account_id"))
        for r in performance
        if r.get("account_id") is not None and r.get("account_id") not in manifest_ids
    ]
    if missing:
        return bad(
            f"{len(missing)} performance epoch(s) are not in the manifest: {names(missing)}"
        )
    return ok(
        f"all {len(performance)} performance epochs are among the manifest's {len(manifest)} epochs"
    )


def b_epoch_counts(payload: Payload) -> None:
    rows = rows_of(payload.panel.get("performance"))
    rows[0]["account_id"] = "not-in-the-manifest"
    payload.panel["performance"] = rows


# --- leverage ---------------------------------------------------------------


def p_leverage_rows_status(payload: Payload) -> Result:
    rows = rows_of(payload.panel.get("leverage_disclosures"))
    if not rows:
        return skip("no leverage rows")
    missing = [r for r in rows if not r.get("coverage_status")]
    if missing:
        return bad(
            f"{len(missing)} of {len(rows)} leverage rows have no coverage status"
        )
    return ok(f"all {len(rows)} leverage rows carry a coverage status")


def b_leverage_rows_status(payload: Payload) -> None:
    rows_of(payload.panel.get("leverage_disclosures"))[0]["coverage_status"] = None


def p_leverage_tiles(payload: Payload) -> Result:
    rows = rows_of(payload.panel.get("leverage_disclosures"))
    if not rows:
        return skip("no leverage rows")
    observed = [r for r in rows if r.get("coverage_status") == "OBSERVED_ONLY"]
    derived = [r for r in rows if r.get("coverage_status") == "DERIVED"]
    # reconciled / declared rows (#894) passed their closure check: they are covered
    covered = [r for r in rows if str(r.get("coverage_status")).startswith("COMPLETE")]
    unavailable = [
        r
        for r in rows
        if r.get("coverage_status") not in {"OBSERVED_ONLY", "DERIVED"}
        and not str(r.get("coverage_status")).startswith("COMPLETE")
    ]
    statuses = {str(r.get("coverage_status")) for r in rows}
    stray = statuses - {
        "OBSERVED_ONLY",
        "UNAVAILABLE",
        "DERIVED",
        "COMPLETE_RECONCILED",
        "COMPLETE_DECLARED",
        "OBSERVED_UNRECONCILED",
    }
    with_amount = sum(1 for r in observed if get(r, "amount", "value") is not None)
    no_amount = len(rows) - sum(
        1 for r in rows if get(r, "amount", "value") is not None
    )
    if stray:
        return bad(
            "coverage statuses other than OBSERVED_ONLY/DERIVED/UNAVAILABLE exist: "
            f"{names(stray)}; the tiles ({len(observed)} observed, {len(covered)} covered, "
            f"{len(derived)} derived, {len(unavailable)} unavailable) do not name them"
        )
    return ok(
        f"{len(observed)} observed ({with_amount} with an amount) + {len(covered)} "
        f"covered + {len(derived)} derived + {len(unavailable)} unavailable = "
        f"{len(rows)}; {no_amount} rows "
        "have no amount"
    )


def b_leverage_tiles(payload: Payload) -> None:
    rows_of(payload.panel.get("leverage_disclosures"))[0]["coverage_status"] = "PARTIAL"


def p_portfolio_return_sentence(payload: Payload) -> Result:
    twr = get(payload.panel, "portfolio_twr_cumulative", "status")
    mwr = get(payload.panel, "portfolio_mwr_annualised", "status")
    if twr is None or mwr is None:
        return skip("portfolio TWR/MWR sections absent")
    if twr == "AVAILABLE" and not get(
        payload.panel, "advisory", "hwm_candidate", "kpi", "value"
    ):
        return bad(
            "portfolio TWR is AVAILABLE but no governed index candidate is published"
        )
    return ok(
        f"portfolio TWR {twr}, MWR {mwr}; the index candidate backs the TWR sentence"
    )


def b_portfolio_return_sentence(payload: Payload) -> None:
    payload.panel["advisory"]["hwm_candidate"]["kpi"]["value"] = None


# --- coverage / facts / counts ---------------------------------------------

_ATTENTION = {"UNAVAILABLE", "NO_ACCEPTED_FACT_PATH", "PARTIAL", "LATE", "STALE"}
_OTHER = re.compile(
    r"^(COMPLETE|FINITE_COMPLETE|FRESH|OBSERVED|RETAINED|NO_REPORTED|PASS)"
)


def p_coverage_other_rows(payload: Payload) -> Result:
    rows = rows_of(payload.panel.get("coverage"))
    if not rows:
        return skip("no coverage rows")
    stray = [
        r
        for r in rows
        if str(r.get("status")) not in _ATTENTION
        and not r.get("escalation_required")
        and not r.get("blocks_current_consumers")
        and not _OTHER.match(str(r.get("status")))
    ]
    if stray:
        return bad(
            f"{len(stray)} rows are neither needs-attention nor complete, fresh, "
            f"observed-only, retained or no-record: {names({str(r.get('status')) for r in stray})}"
        )
    attention = sum(1 for r in rows if str(r.get("status")) in _ATTENTION)
    return ok(
        f"{attention} need attention, {len(rows) - attention} fall in the named classes"
    )


def b_coverage_other_rows(payload: Payload) -> None:
    rows_of(payload.panel.get("coverage"))[0]["status"] = "WEIRD_STATE"


_FX_COMPLETE = re.compile(r"^COMPLETE")


def p_positions_fx_not_complete(payload: Payload) -> Result:
    facts = payload.panel.get("fact_status")
    if not isinstance(facts, dict):
        return skip("no fact_status")
    done = [k for k in ("positions", "fx") if _FX_COMPLETE.match(str(facts.get(k)))]
    if done:
        return bad(f"the sentence says not complete but {names(done)} is COMPLETE")
    return ok(f"positions={facts.get('positions')}, fx={facts.get('fx')}")


def b_positions_fx_not_complete(payload: Payload) -> None:
    payload.panel["fact_status"]["fx"] = "COMPLETE_WITHIN_DECLARED_BOUNDARY"


def p_fact_matrix_grid(payload: Payload) -> Result:
    matrix = rows_of(payload.panel.get("fact_coverage_matrix"))
    if not matrix:
        return skip("no fact coverage matrix")
    keys = [(r.get("account_id"), r.get("fact_type")) for r in matrix]
    accounts = {k[0] for k in keys}
    facts = {k[1] for k in keys}
    if len(set(keys)) != len(keys):
        return bad(f"{len(keys) - len(set(keys))} duplicate account-by-fact rows")
    if len(accounts) * len(facts) != len(keys):
        return bad(
            f"{len(keys)} rows but {len(accounts)} accounts x {len(facts)} facts = "
            f"{len(accounts) * len(facts)}: the matrix is not a full grid"
        )
    return ok(
        f"{len(accounts)} accounts x {len(facts)} facts = {len(keys)} rows, no duplicates"
    )


def b_fact_matrix_grid(payload: Payload) -> None:
    payload.panel["fact_coverage_matrix"].pop()


_OLD_MWR = (
    "MWR remains unavailable outside accounts and intervals with complete admitted"
)


def p_mwr_effect(payload: Payload) -> Result:
    performance = rows_of(payload.panel.get("performance"))
    if not performance:
        return skip("no performance rows")
    unflowed = [
        r
        for r in performance
        if r.get("mwr_status") == "AVAILABLE"
        and r.get("mwr_coverage_status") != "COMPLETE"
    ]
    old = [
        (src, r.get("item"))
        for src, rows in (
            ("panel", rows_of(payload.panel.get("coverage"))),
            ("report", rows_of(payload.report.get("coverage_data_quality"))),
        )
        for r in rows
        if _OLD_MWR in str(get(r, "disposition", "effect") or "")
    ]
    if unflowed:
        return bad(f"{len(unflowed)} epochs show MWR without COMPLETE flow coverage")
    if old:
        return bad(
            f"{len(old)} coverage row(s) still carry the unscoped MWR sentence: "
            f"{names(f'{s}:{i}' for s, i in old)}"
        )
    return ok(
        f"all {len(performance)} MWR rows have COMPLETE coverage; scoped sentence only"
    )


def b_mwr_effect(payload: Payload) -> None:
    rows_of(payload.panel.get("performance"))[0]["mwr_coverage_status"] = "PARTIAL"


def p_money_identity(payload: Payload) -> Result:
    """Gain = Current - Net capital on the unrounded values; the cent-rounded cells differ by at most $0.01."""

    rows = [r for r in _manifest_rows(payload) if r.get("status") == "OK"]
    if not rows:
        return skip("no OK manifest rows")
    off = []
    shown_off = []
    checked = 0
    for row in rows:
        gain, cur, net = (
            dec(row.get("gain")),
            dec(row.get("current_value")),
            dec(row.get("net_capital")),
        )
        if None in (gain, cur, net):
            continue
        assert gain is not None and cur is not None and net is not None
        checked += 1
        if abs(gain - (cur - net)) > Decimal("1e-9"):
            off.append(str(row.get("strategy_id")))
        cent = Decimal("0.01")
        shown = abs(gain.quantize(cent) - (cur.quantize(cent) - net.quantize(cent)))
        if shown > cent:
            shown_off.append(str(row.get("strategy_id")))
    if off:
        return bad(
            f"Gain != Current - Net capital on the unrounded values of {len(off)} rows: {names(off)}"
        )
    if shown_off:
        return bad(
            f"displayed cent cells differ by more than $0.01 on {len(shown_off)} rows: {names(shown_off)}"
        )
    return ok(
        f"Gain = Current - Net capital on the unrounded values of {checked} rows (tolerance 1e-9); "
        "cent-rounded cells differ by at most $0.01"
    )


def b_money_identity(payload: Payload) -> None:
    _manifest_rows(payload)[0]["gain"] = "999.00"


def p_annualised_threshold(payload: Payload) -> Result:
    rows = rows_of(payload.panel.get("performance"))
    if not rows:
        return skip("no performance rows")
    wrong = []
    for row in rows:
        days = row.get("window_days")
        shown = row.get("twr_annualised") is not None
        if isinstance(days, int) and (days < 365) == shown:
            wrong.append(f"{row.get('strategy_id')}({days}d)")
    if wrong:
        return bad(
            f"annualised TWR shown below 365 days or hidden at 365+: {names(wrong)}"
        )
    return ok(
        f"annualised TWR shown only for windows of 365 days or more ({len(rows)} rows)"
    )


def b_annualised_threshold(payload: Payload) -> None:
    rows_of(payload.panel.get("performance"))[0]["twr_annualised"] = "0.1"


def p_twr_counts(payload: Payload) -> Result:
    counts = payload.panel.get("counts")
    performance = rows_of(payload.panel.get("performance"))
    if not isinstance(counts, dict) or not performance:
        return skip("no counts or performance rows")
    available = sum(
        1 for r in performance if str(r.get("twr_status", "")).startswith("AVAILABLE")
    )
    if counts.get("twr_available") != available or counts.get("twr_rows") != len(
        performance
    ):
        return bad(
            f"counts say {counts.get('twr_available')} of {counts.get('twr_rows')} but "
            f"performance has {available} of {len(performance)}"
        )
    return ok(f"{available} of {len(performance)} segments have TWR")


def b_twr_counts(payload: Payload) -> None:
    payload.panel["counts"]["twr_available"] = 99


def p_current_positions_mapped(payload: Payload) -> Result:
    current = [
        r
        for r in rows_of(payload.report.get("positions"))
        if r.get("snapshot_status") == "LATEST_ACCEPTED_ACCOUNT_SNAPSHOT"
    ]
    if not current:
        return skip("no current positions")
    unmapped = [r for r in current if not r.get("strategy_id")]
    if unmapped:
        return bad(
            f"{len(unmapped)} of {len(current)} current positions have no strategy"
        )
    return ok(f"all {len(current)} current positions carry a strategy")


def b_current_positions_mapped(payload: Payload) -> None:
    rows_of(payload.report.get("positions"))[0]["strategy_id"] = None


def p_positions_total(payload: Payload) -> Result:
    counts = payload.panel.get("counts")
    positions = rows_of(payload.panel.get("positions"))
    if not isinstance(counts, dict) or not positions:
        return skip("no counts or positions")
    current = sum(
        1 for r in positions if r.get("position_class") == "CURRENT_AT_CUTOFF"
    )
    if counts.get("positions") != current:
        return bad(
            f"counts.positions={counts.get('positions')} but {current} current rows"
        )
    return ok(f"{current} current positions equal counts.positions")


def b_positions_total(payload: Payload) -> None:
    payload.panel["counts"]["positions"] = 1


def p_underwriting_none(payload: Payload) -> Result:
    rows = rows_of(get(payload.panel, "strategy_underwriting", "rows"))
    if not rows:
        return skip("no underwriting rows")
    off = [
        str(r.get("strategy_id") or i)
        for i, r in enumerate(rows)
        if get(r, "advisory_disposition", "execution_effect") != "NONE"
        or get(r, "advisory_disposition", "allocation_effect") != "NONE"
    ]
    if off:
        return bad(
            f"{len(off)} underwriting rows declare an allocation/execution effect"
        )
    return ok(f"allocation and execution effect NONE on all {len(rows)} rows")


def b_underwriting_none(payload: Payload) -> None:
    rows_of(get(payload.panel, "strategy_underwriting", "rows"))[0][
        "advisory_disposition"
    ]["execution_effect"] = "TRADE"


def p_hwm_perimeter(payload: Payload) -> Result:
    hwm = payload.panel.get("hwm_summary")
    if not isinstance(hwm, dict):
        return skip("no hwm_summary")
    gaps = list(hwm.get("uncovered_strategies") or []) + list(
        hwm.get("excluded_strategies") or []
    )
    if gaps or dec(hwm.get("uncovered_value_usd")) not in (None, Decimal(0)):
        return bad(
            f"HWM perimeter has uncovered/excluded strategies: {names(map(str, gaps))}"
        )
    return ok("no uncovered or excluded HWM-perimeter strategy")


def b_hwm_perimeter(payload: Payload) -> None:
    payload.panel["hwm_summary"]["uncovered_strategies"] = ["x"]


def p_open_epoch_count(payload: Payload) -> Result:
    cutoff = _cutoff(payload)
    rows = _manifest_rows(payload)
    recon = get(payload.panel, "capital_reconciliation", "open_epoch_count")
    if cutoff is None or not rows or recon is None:
        return skip("no manifest or capital reconciliation")
    counted = [r for r in rows if r.get("status") == "OK" and r.get("end") == cutoff]
    if len(counted) != recon:
        return bad(
            f"{len(counted)} manifest rows end at the cutoff; reconciliation says {recon}"
        )
    return ok(
        f"{len(counted)} epochs have a window ending at the cutoff, as reconciled"
    )


def b_open_epoch_count(payload: Payload) -> None:
    payload.panel["capital_reconciliation"]["open_epoch_count"] = 99


def p_weighting_days(payload: Payload) -> Result:
    found = 0
    off: list[str] = []
    modes = get(payload.panel, "orthogonal_risk_attribution", "modes")
    for mode, block in (modes or {}).items():
        for agg in rows_of(get(block, "aggregates")):
            for item in rows_of(agg.get("weighting")):
                try:
                    span = (
                        date.fromisoformat(item["end"])
                        - date.fromisoformat(item["start"])
                    ).days + 1
                except (KeyError, ValueError):
                    continue
                found += 1
                n = item.get("n")
                if not isinstance(n, int) or not 0 < n <= span:
                    off.append(
                        f"{mode}:{agg.get('source_key')} n={n} vs a {span}-day span"
                    )
    if not found:
        return skip("no weighting spans")
    if off:
        return bad(
            f"{len(off)} of {found} weighting spans print a date count outside "
            f"their span: {names(off, 2)}"
        )
    return ok(f"all {found} weighting date counts fit inside their spans")


def b_weighting_days(payload: Payload) -> None:
    modes = payload.panel["orthogonal_risk_attribution"]["modes"]
    first = next(iter(modes.values()))["aggregates"][0]["weighting"][0]
    first["n"] = 100000


def _share_entries(payload: Payload) -> Iterable[tuple[str, dict[str, Json]]]:
    modes = get(payload.panel, "orthogonal_risk_attribution", "modes")
    for mode, block in (modes or {}).items():
        for kind in ("aggregates", "cells"):
            for entry in rows_of(get(block, kind)):
                if isinstance(entry.get("components"), dict) and isinstance(
                    entry.get("closure"), dict
                ):
                    yield f"{mode}.{kind}", entry


def p_share_strip_sum(payload: Payload) -> Result:
    total = 0
    off = 0
    example = ""
    for where, entry in _share_entries(payload):
        shares = entry["components"].get("share_of_total")
        if not isinstance(shares, list):
            continue
        parts = [dec(x) for x in shares]
        parts += [
            dec(entry["closure"].get("residual_share")),
            dec(entry["closure"].get("cross_term_share")),
        ]
        usable = [p for p in parts if p is not None]
        if not usable:
            continue
        total += 1
        if abs(sum(usable) - Decimal(1)) > Decimal("1e-9"):
            off += 1
            example = example or f"{where}: sums to {sum(usable)}"
    if not total:
        return skip("no variance-share strips")
    if off:
        return bad(
            f"{off} of {total} strips whose unrounded shares do not sum to one "
            f"(e.g. {example})"
        )
    return ok(f"unrounded shares sum to one in all {total} strips")


def b_share_strip_sum(payload: Payload) -> None:
    _, entry = next(iter(_share_entries(payload)))
    entry["closure"]["residual_share"] = "0.5"


def p_cluster_weights(payload: Payload) -> Result:
    found = 0
    off: list[str] = []
    for mode, block in (get(payload.panel, "dependence_modes") or {}).items():
        comps = {
            str(c.get("series_id")): dec(c.get("allocation_weight"))
            for c in rows_of(get(block, "correlation_one", "components"))
        }
        for cluster in rows_of(
            get(block, "economic_driver_cluster_budget", "clusters")
        ):
            weight = dec(cluster.get("allocation_weight"))
            members = [comps.get(str(m)) for m in cluster.get("members") or []]
            if weight is None or any(m is None for m in members):
                continue
            found += 1
            if abs(
                weight - sum((m for m in members if m is not None), Decimal(0))
            ) > Decimal("1e-9"):
                off.append(f"{mode}:{cluster.get('cluster_id')}")
    if not found:
        return skip("no cluster weights")
    if off:
        return bad(
            f"{len(off)} cluster weights differ from the correlation components: {names(off)}"
        )
    return ok(
        f"all {found} cluster weights equal the sum of their correlation components"
    )


def b_cluster_weights(payload: Payload) -> None:
    block = next(iter(payload.panel["dependence_modes"].values()))
    block["economic_driver_cluster_budget"]["clusters"][0]["allocation_weight"] = "0.9"


def p_labelled_joins(payload: Payload) -> Result:
    live_only_backtest = [
        str(line.get("series_id"))
        for line in _mode_lines(payload, "LIVE_ONLY")
        if any(label == "BACKTEST" for _s, _e, label in _segments(line))
    ]
    unlabelled = [
        str(line.get("series_id"))
        for line in _mode_lines(payload, "LIVE_PLUS_BACKTEST")
        if not _segments(line) and line.get("kind") in ("STRATEGY", "PORTFOLIO")
    ]
    if live_only_backtest:
        return bad(
            f"LIVE_ONLY lines carry backtest origin: {names(live_only_backtest)}"
        )
    if unlabelled:
        return bad(
            f"LIVE_PLUS_BACKTEST lines without origin labels: {names(unlabelled)}"
        )
    return ok(
        "backtest appears only in LIVE_PLUS_BACKTEST and every joined line is labelled"
    )


def b_labelled_joins(payload: Payload) -> None:
    _mode_lines(payload, "LIVE_PLUS_BACKTEST")[0]["origin"] = None


def p_gap_fills_labelled(payload: Payload) -> Result:
    fills = rows_of(get(payload.panel, "vendor_gap_fills", "fills"))
    if not fills:
        return skip("no filled NAV days")
    unlabelled = [str(f.get("date")) for f in fills if not f.get("origin_label")]
    if unlabelled:
        return bad(
            f"{len(unlabelled)} filled days carry no origin label: {names(unlabelled)}"
        )
    return ok(f"all {len(fills)} filled NAV days are labelled with their origin")


def b_gap_fills_labelled(payload: Payload) -> None:
    rows_of(get(payload.panel, "vendor_gap_fills", "fills"))[0]["origin_label"] = None


def p_aligned_windows(payload: Payload) -> Result:
    strategies = rows_of(get(payload.panel, "model_vs_actual", "strategies"))
    if not strategies:
        return skip("no model-vs-actual strategies")
    off = [
        str(s.get("strategy_id"))
        for s in strategies
        if bool(s.get("primary_window_id")) != bool(s.get("windows"))
    ]
    if off:
        return bad(f"primary window and windows disagree for {names(off)}")
    aligned = sum(1 for s in strategies if s.get("windows"))
    return ok(f"{aligned} of {len(strategies)} strategies have an aligned window")


def b_aligned_windows(payload: Payload) -> None:
    rows_of(get(payload.panel, "model_vs_actual", "strategies"))[0]["windows"] = []


def p_chart_lines(payload: Payload) -> Result:
    lines = _mode_lines(payload, "LIVE_PLUS_BACKTEST")
    if not lines:
        return skip("no performance lines")
    ids = {str(line.get("series_id")) for line in lines}
    strategies = {i for i in ids if i.startswith("strategy:")}
    portfolios = {i for i in ids if i.startswith("portfolio:")}
    expected = {i for i in ids if i.startswith("market:")}
    if len(rows_of(payload.panel.get("strategies"))) and len(strategies) != get(
        payload.panel, "counts", "strategies", default=len(strategies)
    ):
        return bad(f"{len(strategies)} strategy lines but counts.strategies differs")
    if {"market:SPY", "market:SPXL"} - expected:
        return bad("SPY and SPXL are not both charted")
    return ok(
        f"{len(strategies)} strategy, {len(portfolios)} portfolio, {len(expected)} benchmark lines"
    )


def b_chart_lines(payload: Payload) -> None:
    lines = _mode_lines(payload, "LIVE_PLUS_BACKTEST")
    lines[:] = [ln for ln in lines if ln.get("series_id") != "market:SPXL"]
    payload.panel["performance_tab"]["modes"]["LIVE_PLUS_BACKTEST"]["lines"] = lines


def p_hwm_nav_coverage(payload: Payload) -> Result:
    coverage = dec(get(payload.panel, "hwm_summary", "nav_coverage"))
    if coverage is None:
        return skip("no hwm_summary.nav_coverage")
    if coverage != 1:
        return bad(f"HWM NAV coverage is {coverage}, not complete")
    return ok("HWM NAV coverage is 1 (complete)")


def b_hwm_nav_coverage(payload: Payload) -> None:
    payload.panel["hwm_summary"]["nav_coverage"] = "0.5"


def p_equal_weight_start(payload: Payload) -> Result:
    lines = _mode_lines(payload, "LIVE_PLUS_BACKTEST")
    strategies = [ln for ln in lines if ln.get("kind") == "STRATEGY"]
    head = next(
        (ln for ln in lines if ln.get("series_id") == "portfolio:equal_weight"), None
    )
    if not strategies or head is None:
        return skip("no strategy lines or equal-weight line")
    starts = [ln.get("first_index") for ln in strategies]
    if not all(isinstance(s, int) for s in starts) or not isinstance(
        head.get("first_index"), int
    ):
        return skip("first_index absent")
    latest = max(s for s in starts if isinstance(s, int))
    if head["first_index"] != latest:
        return bad(
            f"equal-weight line starts at index {head['first_index']} but the last "
            f"strategy line starts at {latest}"
        )
    return ok(
        f"equal-weight line starts at index {latest}, where every strategy has data"
    )


def b_equal_weight_start(payload: Payload) -> None:
    for line in _mode_lines(payload, "LIVE_PLUS_BACKTEST"):
        if line.get("series_id") == "strategy:A":
            line["first_index"] = 5


def p_weeks_are_mondays(payload: Payload) -> Result:
    found = 0
    off: list[str] = []
    for mode, block in (get(payload.panel, "performance_tab", "modes") or {}).items():
        for text in get(block, "dates", default=[]) or []:
            try:
                day = date.fromisoformat(str(text))
            except ValueError:
                off.append(f"{mode}:{text}")
                continue
            found += 1
            if day.weekday() != 0:
                off.append(f"{mode}:{text}")
    if not found:
        return skip("no weekly dates")
    if off:
        return bad(f"{len(off)} weekly dates are not Mondays: {names(off, 3)}")
    return ok(f"all {found} weekly dates are Mondays")


def b_weeks_are_mondays(payload: Payload) -> None:
    payload.panel["performance_tab"]["modes"]["LIVE_ONLY"]["dates"][0] = "2026-09-09"


_INFERENCE_KEY = re.compile(
    r"^(p_values?|pvalues?|confidence_intervals?|ci_(low|high|lower|upper)\w*)$"
)


def p_no_inference_keys(payload: Payload) -> Result:
    root = payload.panel.get("decomposition_lenses")
    if root is None:
        return skip("no decomposition_lenses")
    stack: list[Json] = [root]
    seen = 0
    bad_paths: list[str] = []
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                seen += 1
                if _INFERENCE_KEY.match(str(key)):
                    bad_paths.append(str(key))
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    if bad_paths:
        return bad(
            f"{len(bad_paths)} p-value / confidence-interval keys: {names(set(bad_paths))}"
        )
    return ok(f"no p-value or confidence-interval key among {seen} keys")


def b_no_inference_keys(payload: Payload) -> None:
    payload.panel["decomposition_lenses"]["lenses"]["x"]["p_value"] = 0.04


# --- sentences the payload cannot decide -----------------------------------


def _bundle_evidence(payload: Payload) -> Result:
    """U02: the generation manifest is VERIFIED and its panel artifacts match the bundle bytes.

    Needs the bundle input; without it the row stays an UNVERIFIABLE skip.
    """

    bundle = payload.bundle
    if bundle is None:
        return skip(
            "UNVERIFIABLE from the payload: needs the bundle bytes, not a payload field "
            "(pass --bundle <generation dir> to run it)"
        )
    status = bundle.get("verification")
    if status != "VERIFIED":
        return bad(f"generation manifest verification is {status!r}, not 'VERIFIED'")
    artifacts = bundle.get("artifacts")
    on_disk = bundle.get("on_disk")
    if not isinstance(artifacts, list) or not isinstance(on_disk, dict):
        return bad("bundle evidence is malformed")
    panel = [a for a in artifacts if str(a.get("artifact_id", "")).startswith("panel/")]
    if not panel:
        return bad("the manifest lists no panel/ artifact")
    problems: list[str] = []
    for artifact in panel:
        name = artifact["artifact_id"]
        found = on_disk.get(name)
        if not isinstance(found, dict):
            problems.append(f"{name}: not stored in the bundle")
        elif (found.get("sha256"), found.get("size_bytes")) != (
            artifact.get("sha256"),
            artifact.get("size_bytes"),
        ):
            problems.append(f"{name}: bytes differ from the manifest")
    if problems:
        return bad("; ".join(problems[:5]))
    return ok(
        f"manifest VERIFIED; {len(panel)} panel/ artifacts byte-identical "
        f"(sha256 + size) to the bundle files; {len(artifacts) - len(panel)} other "
        "manifest artifacts not checked"
    )


def _b_bundle_evidence(payload: Payload) -> None:
    bundle = payload.bundle
    assert bundle is not None
    bundle["on_disk"][bundle["artifacts"][0]["artifact_id"]]["sha256"] = "0" * 64


def _p_bundle_evidence(payload: Payload) -> None:
    sha = "a" * 64
    payload.bundle = {
        "verification": "VERIFIED",
        "artifacts": [
            {"artifact_id": "panel/index.html", "sha256": sha, "size_bytes": 5},
            {"artifact_id": "evidence/x.json", "sha256": "b" * 64, "size_bytes": 9},
        ],
        "on_disk": {"panel/index.html": {"sha256": sha, "size_bytes": 5}},
    }


def _unverifiable(reason: str) -> Callable[[Payload], Result]:
    def predicate(_payload: Payload) -> Result:
        return skip(f"UNVERIFIABLE from the payload: {reason}")

    return predicate


PANEL_JS = "apps/portfolio-control-panel/app.js"
PANEL_HTML = "apps/portfolio-control-panel/index.html"
PANEL_PY = "tools/portfolio_control_panel.py"
REPORT_PY = "tools/portfolio_report.py"
BASELINE_PY = "tools/portfolio_baseline.py"


ROWS: tuple[Row, ...] = (
    Row(
        "B1",
        "universal",
        "Command",
        PANEL_JS,
        (
            r"remain separate, except in the LIVE_PLUS_BACKTEST view, which joins version-matched backtest history",
        ),
        "backtest history is joined to a live line only in LIVE_PLUS_BACKTEST, and that line is labelled by origin",
        p_evidence_separate,
        b_evidence_separate,
        rendered_view="Command: construction points",
    ),
    Row(
        "B2",
        "epoch",
        "Risk",
        PANEL_JS,
        (
            r"A retired epoch with a successor hands its closing value to the successor",
            r"the totals count Current for the \$\{totals\.open\} epochs whose window ends at the cutoff",
        ),
        "an epoch that has a successor holds no current value at the cutoff (its value was handed on)",
        p_closed_epochs_current,
        b_closed_epochs_current,
        rendered_view="Risk: epoch manifest footnote",
    ),
    Row(
        "S15",
        "epoch",
        "Weekly returns",
        PANEL_HTML,
        (
            r"a week containing both pre-live history and live observations is labelled MIXED",
        ),
        "a MIXED week sits between backtest and live weeks of its line (it is at the boundary, never at an end)",
        p_inception_week_mixed,
        b_inception_week_mixed,
        pass_fixture=f_inception_week_mixed,
        rendered_view="Weekly returns: return-basis boundary",
    ),
    Row(
        "AB2",
        "coverage",
        "Data health / report coverage",
        REPORT_PY,
        (r"Accepted position rows, current and historical snapshots, are reported",),
        "the positions effect does not say only current rows are reported while historical snapshot rows are counted",
        p_positions_only_current,
        b_positions_only_current,
        rendered_view="Data health: coverage item positions",
    ),
    Row(
        "VC1",
        "count",
        "Signals",
        PANEL_JS,
        (r"Implementation gap tested",),
        "the implementation-gap count matches the strategies Model vs actual compares",
        p_gap_tested_count,
        b_gap_tested_count,
        rendered_view="Signals: KPI row",
    ),
    Row(
        "VC2",
        "count",
        "Risk",
        PANEL_JS,
        (
            r"All \$\{mappedPerformance\.length\} epochs in this table",
            r"Total · \$\{totals\.count\} of \$\{totals\.of\} epochs",
        ),
        "every epoch in the performance table is an epoch of the manifest table",
        p_epoch_counts,
        b_epoch_counts,
        rendered_view="Risk: ALL n EPOCHS vs n OF n EPOCHS",
    ),
    Row(
        "S01",
        "universal",
        "Capital",
        PANEL_HTML,
        (r"Every leverage observation, with its coverage status",),
        "every leverage observation carries a coverage status",
        p_leverage_rows_status,
        b_leverage_rows_status,
    ),
    Row(
        "S02",
        "coverage",
        "Data health",
        PANEL_PY,
        (r"Positions and FX records are not complete",),
        "positions and FX are not complete",
        p_positions_fx_not_complete,
        b_positions_fx_not_complete,
    ),
    Row(
        "S03",
        "count",
        "Capital",
        PANEL_JS,
        (r"Observed-only coverage:", r"By coverage status \("),
        "observed and unavailable tiles partition the leverage rows by coverage status",
        p_leverage_tiles,
        b_leverage_tiles,
    ),
    Row(
        "S05",
        "status",
        "Risk",
        PANEL_JS,
        (
            r"The portfolio TWR is the governed index candidate",
            r"Portfolio-level MWR is not published in this snapshot",
        ),
        "the portfolio TWR sentence is backed by a governed index candidate",
        p_portfolio_return_sentence,
        b_portfolio_return_sentence,
    ),
    Row(
        "S08",
        "coverage",
        "Data health",
        PANEL_JS,
        (
            r"other rows \(complete, fresh, observed-only, retained or with no reported record\)",
        ),
        "every row that does not need attention is complete, fresh, observed-only, "
        "retained or has no reported record",
        p_coverage_other_rows,
        b_coverage_other_rows,
    ),
    Row(
        "S09",
        "count",
        "Attribution",
        PANEL_JS,
        (r"\(\$\{integer\.format\(item\.n\)\} weighted dates\)",),
        "N weighted dates is a number of dates inside the weighting span",
        p_weighting_days,
        b_weighting_days,
    ),
    Row(
        "S10",
        "count",
        "Attribution",
        PANEL_JS,
        (r"Share of total variance:", r"unrounded they sum to 100%"),
        "the unrounded variance shares in a strip sum to one",
        p_share_strip_sum,
        b_share_strip_sum,
    ),
    Row(
        "S11",
        "epoch",
        "Risk",
        PANEL_JS,
        (r"Current: \$\{totals\.open\} with a window ending at the cutoff",),
        "the epochs counted in Current are those with a window ending at the cutoff",
        p_open_epoch_count,
        b_open_epoch_count,
    ),
    Row(
        "S12",
        "basis",
        "Dependence",
        PANEL_JS,
        (r"recomputed from the accepted components at the correlation specification",),
        "cluster weights are the correlation specification's component weights",
        p_cluster_weights,
        b_cluster_weights,
    ),
    Row(
        "S14",
        "universal",
        "Data health / Performance",
        PANEL_PY,
        (r"are not concatenated, except in the LIVE_PLUS_BACKTEST mode",),
        "backtest and live are joined only in LIVE_PLUS_BACKTEST and the joined line is labelled",
        p_labelled_joins,
        b_labelled_joins,
    ),
    Row(
        "S16",
        "universal",
        "Risk / Performance",
        REPORT_PY,
        (r"a day the HWM candidate fills is labelled with its origin",),
        "every filled NAV day is labelled with its origin",
        p_gap_fills_labelled,
        b_gap_fills_labelled,
    ),
    Row(
        "S17",
        "universal",
        "Model vs actual",
        PANEL_HTML,
        (r"Each strategy with an aligned window",),
        "a strategy has a model comparison exactly when it has an aligned window",
        p_aligned_windows,
        b_aligned_windows,
    ),
    Row(
        "S18",
        "count",
        "Performance charts",
        PANEL_HTML,
        (r"Each strategy, the equal-weight portfolio lines, SPY and SPXL",),
        "the chart carries every strategy, the portfolio lines, SPY and SPXL",
        p_chart_lines,
        b_chart_lines,
    ),
    Row(
        "S19",
        "count",
        "Data health",
        PANEL_HTML,
        (r"Every account-by-fact row in this matrix",),
        "the matrix is a complete accounts x facts grid with no duplicates",
        p_fact_matrix_grid,
        b_fact_matrix_grid,
    ),
    Row(
        "S20",
        "coverage",
        "Data health",
        REPORT_PY,
        (r"MWR is shown only where admitted flows are complete for that epoch",),
        "MWR is available only with complete flow coverage; the unscoped sentence is gone",
        p_mwr_effect,
        b_mwr_effect,
    ),
    Row(
        "T03",
        "count",
        "Capital",
        PANEL_JS,
        (r'position\$\{accepted\.length === 1 \? "" : "s"\}, all pages',),
        "the all-pages position total is the current-position count",
        p_positions_total,
        b_positions_total,
    ),
    Row(
        "T08",
        "universal",
        "Risk",
        PANEL_HTML,
        (r"holds on the unrounded values of every available row",),
        "Gain = Current - Net capital on the unrounded values of every available row; displayed cells can differ by $0.01",
        p_money_identity,
        b_money_identity,
    ),
    Row(
        "T08b",
        "universal",
        "Risk",
        PANEL_JS,
        (r"Gain = Current . Net capital on the unrounded values",),
        "Gain = Current - Net capital on the unrounded values; displayed cells can differ by $0.01",
        p_money_identity,
        b_money_identity,
    ),
    Row(
        "T09",
        "universal",
        "Risk",
        PANEL_HTML,
        (r"only shown as a result for windows of 365 days or more",),
        "annualised TWR appears only for windows of 365 days or more",
        p_annualised_threshold,
        b_annualised_threshold,
    ),
    Row(
        "T13",
        "universal",
        "Risk",
        PANEL_JS,
        (
            r"Every included HWM-perimeter sleeve has a qualified account-live or provider-live return path",
        ),
        "no HWM-perimeter strategy is uncovered or excluded",
        p_hwm_perimeter,
        b_hwm_perimeter,
    ),
    Row(
        "T14",
        "count",
        "Command / Risk",
        PANEL_JS,
        (r"account-strategy segments have coverage-aware TWR",),
        "the TWR segment counts equal the performance rows",
        p_twr_counts,
        b_twr_counts,
    ),
    Row(
        "T18",
        "universal",
        "Data health",
        BASELINE_PY,
        (r"Every current systematic interval has an accepted strategy assignment",),
        "every current position carries a strategy assignment",
        p_current_positions_mapped,
        b_current_positions_mapped,
    ),
    Row(
        "T20",
        "universal",
        "Signals",
        PANEL_JS,
        (r"Read-only advisory surface; declared by every underwriting row",),
        "allocation and execution effect are NONE on every underwriting row",
        p_underwriting_none,
        b_underwriting_none,
    ),
    Row(
        "T13b",
        "universal",
        "Command",
        PANEL_PY,
        (r"Every included HWM-perimeter strategy is qualified",),
        "no HWM-perimeter strategy is uncovered or excluded",
        p_hwm_perimeter,
        b_hwm_perimeter,
    ),
    Row(
        "T13c",
        "universal",
        "Report methodology",
        REPORT_PY,
        (r"Every included HWM-perimeter strategy is qualified",),
        "no HWM-perimeter strategy is uncovered or excluded",
        p_hwm_perimeter,
        b_hwm_perimeter,
    ),
    Row(
        "T13d",
        "coverage",
        "Command",
        PANEL_PY,
        (r"Governed systematic HWM has complete qualified NAV coverage",),
        "HWM NAV coverage is complete",
        p_hwm_nav_coverage,
        b_hwm_nav_coverage,
    ),
    Row(
        "T21",
        "universal",
        "Attribution",
        PANEL_HTML,
        (r"never post-selection p-values or confidence intervals",),
        "no decomposition lens carries a p-value or confidence-interval field",
        p_no_inference_keys,
        b_no_inference_keys,
    ),
    Row(
        "T24",
        "epoch",
        "Weekly returns",
        PANEL_HTML,
        (r"compounded Monday through Sunday",),
        "every weekly date is a Monday",
        p_weeks_are_mondays,
        b_weeks_are_mondays,
    ),
    Row(
        "T25",
        "epoch",
        "Performance charts",
        "tools/portfolio_performance_tab.py",
        (
            r"the headline equal-weight line starts only where every \"\s*\"strategy has data",
        ),
        "the headline equal-weight line starts where the last strategy line starts",
        p_equal_weight_start,
        b_equal_weight_start,
    ),
    Row(
        "U02",
        "status",
        "Risk",
        PANEL_JS,
        (r"verified generation bundle exact bytes",),
        "report-attested static publication bytes are verified",
        _bundle_evidence,
        _b_bundle_evidence,
        pass_fixture=_p_bundle_evidence,
    ),
)

# The rows of the sleeve restatement (#624); the module imports this one for Row, Payload and
# the verdict helpers, so it is read after they are defined.
from claim_table_sleeve import SLEEVE_ROWS  # noqa: E402

ROWS = ROWS + SLEEVE_ROWS

ROW_IDS = tuple(row.id for row in ROWS)
assert len(set(ROW_IDS)) == len(ROW_IDS), "duplicate claim-table row ids"


def evaluate(row: Row, payload: Payload) -> Result:
    """Run one predicate; an exception is a FAIL, never a silent skip."""

    try:
        return row.predicate(payload)
    except Exception as exc:  # noqa: BLE001 - a predicate that cannot run is a finding
        return bad(f"predicate raised {type(exc).__name__}: {exc}")

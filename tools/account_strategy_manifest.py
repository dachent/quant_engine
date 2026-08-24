"""Account × strategy epoch manifest — governed accounting engine.

Owner-mandated epoch continuity rule (HWM-003, #324):
    When strategy A ends and strategy B begins on the same account, the
    closing NAV of A becomes the opening invested capital of B. Capital
    is continuous across epochs; nothing appears or disappears.

Columns (owner-approved clean investor view):
    Account | Strategy | Started | Ended | Invested | Withdrawn |
    Net capital | Current | Gain | MWR% | Ann% | TWR% | HWM | MaxDD%

Definitions (owner-approved legend):
    Invested   = opening capital + deposits + internal-in after start
                 (flows ON the first funded day ARE the opening; excluded)
    Withdrawn  = external withdrawals + internal-out after start
    Net capital= Invested - Withdrawn (still at work)
    Current    = last funded NAV in the epoch window
    Gain       = Current - Net capital (money identity, exact to the cent)
    MWR        = Gain / Net capital (raw when < 1yr; annualized when >= 1yr)
    Ann%       = (1+MWR)^(365/days) - 1 when days >= 365 else MWR*365/days
    TWR        = flow-adjusted linked daily index (Modified-Dietz segments)
    HWM/MaxDD  = running max / worst drawdown OF THE DAILY flow-adjusted
                 index (never the sparse flow-boundary index)

Flow classification (fail-closed, ledger-evidence-derived):
    TRANSFER:INTERNAL:*            -> INTERNAL (broker's own label)
    ACCOUNT_TRANSFER               -> INTERNAL iff a same-date opposite-sign
                                      counterparty row exists on another
                                      account; otherwise UNRESOLVED (row is
                                      flagged, never silently assumed)
    *_CONTRIBUTION / *_WITHDRAWAL /
    EXTERNAL_PROVIDER_TRANSFER /
    DEPOSIT_OR_WITHDRAWAL          -> EXTERNAL

Usage:
    python tools/account_strategy_manifest.py \
        --ledger-json <provider-ledger.json> \
        [--baseline-root <dataPortfolio>] [--format table|json]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 40

# Real broker account numbers are DEPLOYMENT-LOCAL data (repository data-
# boundary policy): IBKR numbers are supplied via --ibkr-numbers-json and
# were proven 2026-08-23 by exact daily-equity-series equality against Flex
# AllAccounts XML. Schwab numbers resolve dynamically from playwright_csv
# balance files. No real number may appear in this repository.

MIN_FUNDED_NAV = Decimal("100")
MIN_SCHWAB_MATCH_SCORE = 50
INTERNAL_TYPES = frozenset({"TRANSFER:INTERNAL:IN", "TRANSFer:INTERNAL:OUT"})
EXTERNAL_PREFIXES = (
    "EXTERNAL_CONTRIBUTION",
    "EXTERNAL_WITHDRAWAL",
    "EXTERNAL_PROVIDER_TRANSFER",
    "DEPOSIT_OR_WITHDRAWAL",
)


class ManifestError(RuntimeError):
    """Raised when inputs violate the manifest contract (fail-closed)."""


def _dec(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - boundary conversion
        raise ManifestError(f"{field} is not numeric: {value!r}") from exc


def _amount_value(row: dict[str, Any], field: str) -> Decimal | None:
    amount = row.get("amount")
    if not isinstance(amount, dict):
        return None
    raw = amount.get("value")
    if raw is None:
        return None
    return _dec(raw, field)


def load_ledger(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for section in ("nav_observations", "flows"):
        if not isinstance(payload.get(section), list):
            raise ManifestError(f"ledger missing list section '{section}'")
    return payload


def load_baseline(baseline_root: Path) -> dict[str, Any]:
    pointer_path = baseline_root / "CURRENT.json"
    pointer: dict[str, Any] = json.loads(pointer_path.read_text(encoding="utf-8"))
    generation_id = pointer.get("generation_id")
    if not generation_id:
        raise ManifestError("baseline CURRENT.json lacks generation_id")
    baseline_path = (
        baseline_root / "generations" / generation_id / "portfolio-baseline.json"
    )
    payload: dict[str, Any] = json.loads(baseline_path.read_text(encoding="utf-8"))
    for section in ("accounts", "assignments"):
        if not isinstance(payload.get(section), list):
            raise ManifestError(f"baseline missing list section '{section}'")
    return payload


def load_ibkr_numbers(path: Path | None) -> dict[str, str]:
    """Deployment-local governed-account-id -> real IBKR number mapping."""
    if path is None:
        return {}
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in payload.items()}


def load_flow_classes(path: Path) -> list[dict[str, Any]]:
    """Validated classified-flow artifact (2026-08-23 session): deduped,
    transaction-reconciled, class in {EXTERNAL, INTERNAL}. This artifact is
    the authority for flow buckets; raw-ledger classification is run only
    as a reconciliation disclosure."""
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("events"), list):
        raise ManifestError("flow classes missing 'events' list")
    return [dict(item) for item in payload["events"]]


def flow_buckets_from_classes(
    events: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Decimal]]], int]:
    """Buckets mirror the validated session engine exactly: EXTERNAL is
    day-netted; INTERNAL keeps GROSS in/out per event sign (a same-day
    in-and-out pair presents both legs, matching owner-accepted output)."""
    external: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    internal_in: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    internal_out: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    skipped = 0
    for ev in events:
        account = ev.get("account")
        day = ev.get("date")
        cls = ev.get("class")
        try:
            value = Decimal(str(ev.get("amount")))
        except Exception:
            skipped += 1
            continue
        if not account or not day or cls not in {"EXTERNAL", "INTERNAL"}:
            skipped += 1
            continue
        if cls == "EXTERNAL":
            external[account][day] += value
        elif value > 0:
            internal_in[account][day] += value
        else:
            internal_out[account][day] += -value
    return (
        {
            "external": external,
            "internal_in": internal_in,
            "internal_out": internal_out,
        },
        skipped,
    )


def reconcile_against_raw_ledger(
    buckets: dict[str, dict[str, dict[str, Decimal]]],
    ledger: dict[str, Any],
) -> list[str]:
    """Non-blocking disclosure: classify raw ledger flows independently
    (broker labels + counterparty pairing) and report aggregate divergence
    versus the validated artifact. Never silently substitutes numbers."""
    notes: list[str] = []
    raw, unresolved = classify_flows(ledger)
    for label in ("external", "internal_in", "internal_out"):
        art_total = sum(
            (sum(days.values(), Decimal(0)) for days in buckets[label].values()),
            Decimal(0),
        )
        if label == "external":
            raw_total = sum(
                (sum(days.values(), Decimal(0)) for days in raw["external"].values()),
                Decimal(0),
            )
        else:
            sign = 1 if label == "internal_in" else -1
            raw_total = sum(
                (
                    sum(days.values(), Decimal(0)) * sign
                    for days in raw["internal"].values()
                ),
                Decimal(0),
            )
        if art_total != raw_total:
            notes.append(
                f"{label}: validated artifact total {art_total} vs "
                f"independent raw-ledger classification {raw_total} "
                "(artifact governs; divergence disclosed)"
            )
    notes.extend(f"unresolved: {u}" for u in unresolved)
    return notes


def nav_series(ledger: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    series: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for obs in ledger["nav_observations"]:
        nav = obs.get("nav")
        if not isinstance(nav, dict) or nav.get("status") != "AVAILABLE":
            continue
        value = nav.get("value")
        account_id = obs.get("account_id")
        as_of = obs.get("as_of_date")
        if value is None or not account_id or not as_of:
            continue
        v = _dec(value, f"nav[{account_id}/{as_of}]")
        prior = series[account_id].get(as_of)
        series[account_id][as_of] = v if prior is None else max(prior, v)
    return {a: dict(sorted(d.items())) for a, d in series.items()}


def classify_flows(
    ledger: dict[str, Any],
) -> tuple[dict[str, dict[str, dict[str, Decimal]]], list[str]]:
    """Return per-account {date: signed_total} for EXTERNAL and INTERNAL
    flows plus a list of unresolved record ids (fail-closed disclosure)."""
    flows = []
    for row in ledger["flows"]:
        value = _amount_value(row, "flows.amount")
        if value is None or value == 0:
            continue
        record: dict[str, Any] = {
            "record_id": str(row.get("record_id", "")),
            "account_id": str(row.get("account_id", "")),
            "event_date": str(row.get("event_date", "")),
            "flow_type": str(row.get("flow_type", "")),
            "value": value,
        }
        flows.append(record)

    # Counterparty pairing for ACCOUNT_TRANSFER: same date, opposite sign,
    # different account, same magnitude.
    transfer_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in flows:
        if f["flow_type"] == "ACCOUNT_TRANSFER":
            day_key = str(f["event_date"])
            f["event_date"] = day_key
            transfer_index[day_key].append(f)

    def _has_counterparty(f: dict[str, Any]) -> bool:
        for other in transfer_index[f["event_date"]]:
            if other["record_id"] == f["record_id"]:
                continue
            if other["account_id"] == f["account_id"]:
                continue
            if other["value"] == -f["value"]:
                return True
        return False

    external: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    internal: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unresolved: list[str] = []
    for f in flows:
        ft = f["flow_type"]
        if ft.startswith("TRANSFER:INTERNAL:"):
            bucket = internal
        elif ft == "ACCOUNT_TRANSFER":
            if _has_counterparty(f):
                bucket = internal
            else:
                unresolved.append(
                    f"{f['record_id']} ACCOUNT_TRANSFER without counterparty "
                    f"({f['account_id']} {f['event_date']} {f['value']})"
                )
                continue
        elif ft.startswith(EXTERNAL_PREFIXES):
            bucket = external
        else:
            unresolved.append(
                f"{f['record_id']} unknown flow_type '{ft}' "
                f"({f['account_id']} {f['event_date']} {f['value']})"
            )
            continue
        bucket[f["account_id"]][f["event_date"]] += f["value"]
    return {"external": external, "internal": internal}, unresolved


def schwab_account_numbers(
    series: dict[str, dict[str, Decimal]],
    accounts: dict[str, dict[str, Any]],
    source_dir: Path,
) -> dict[str, str]:
    """Resolve real Schwab account numbers by exact daily-value equality."""
    resolved: dict[str, str] = {}
    if not source_dir.exists():
        return resolved
    governed_schwab = {
        aid: s
        for aid, s in series.items()
        if accounts.get(aid, {}).get("provider") == "SCHWAB"
    }
    pattern = re.compile(r"(\d{8})_")
    for path in sorted(source_dir.glob("historical_balances_*.csv")):
        match = pattern.search(path.name)
        if match is None:
            continue
        values: dict[str, Decimal] = {}
        with path.open(encoding="utf-8", errors="ignore", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_date = (row.get("date") or "").strip()
                raw_value = (row.get("account_value") or "").replace(",", "")
                if not raw_date or not raw_value:
                    continue
                try:
                    iso = datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
                    values[iso] = Decimal(raw_value)
                except ValueError:
                    continue
        best_id: str | None = None
        best_score = 0
        for aid, governed in governed_schwab.items():
            score = sum(
                1
                for d in list(values)[:400]
                if d in governed and governed[d] == values[d]
            )
            if score > best_score:
                best_score, best_id = score, aid
        if best_id is not None and best_score >= MIN_SCHWAB_MATCH_SCORE:
            resolved.setdefault(best_id, match.group(1))
    return resolved


def xirr(cashflows: list[tuple[date, Decimal]]) -> Decimal | None:
    """Bisection XIRR; None when sign conditions or convergence fail."""
    if len(cashflows) < 2 or len({a > 0 for _, a in cashflows}) < 2:
        return None
    t0 = cashflows[0][0]

    def npv(rate: Decimal) -> Decimal:
        total = Decimal(0)
        for dt, a in cashflows:
            total += a / (
                (Decimal(1) + rate) ** (Decimal((dt - t0).days) / Decimal(365))
            )
        return total

    lo, hi = Decimal("-0.95"), Decimal("50")
    flo = npv(lo)
    if flo * npv(hi) > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        fmid = npv(mid)
        if (fmid < 0) == (flo < 0):
            lo, flo = mid, fmid
        else:
            hi = mid
    rate = (lo + hi) / 2
    return rate if abs(npv(rate)) < Decimal("25") else None


def _in_window(day: str, w0: str, w1: str | None) -> bool:
    return day >= w0 and (w1 is None or day <= w1)


def analyze_epoch(
    window_navs: dict[str, Decimal],
    ext_by_day: dict[str, Decimal],
    int_in_by_day: dict[str, Decimal],
    int_out_by_day: dict[str, Decimal],
    prev_closing: Decimal | None,
    w0: str,
    w1: str | None,
) -> dict[str, Any] | None:
    if len(window_navs) < 3:
        return None
    days = sorted(window_navs)
    first_funded = days[0]
    # Owner mandate: flows outside the assignment window never touch the
    # strategy (pre-window cash is prior-epoch business).
    ext_by_day = {d: v for d, v in ext_by_day.items() if _in_window(d, w0, w1)}
    int_in_by_day = {d: v for d, v in int_in_by_day.items() if _in_window(d, w0, w1)}
    int_out_by_day = {d: v for d, v in int_out_by_day.items() if _in_window(d, w0, w1)}
    closing_nav_date = days[-1]
    late_sources = [
        (label, d, v)
        for label, source_days in (
            ("external", ext_by_day),
            ("internal_in", int_in_by_day),
            ("internal_out", int_out_by_day),
        )
        for d, v in source_days.items()
        if d > closing_nav_date
    ]
    if late_sources:
        # Fail-closed (reviewer P1): a flow after the last funded NAV would
        # corrupt the money identity while being absent from Current. The
        # epoch stays unavailable until a covering NAV observation exists.
        return {
            "unavailable_reason": "FLOWS_AFTER_CLOSING_NAV",
            "late_flows": [
                {"class": label, "date": day, "value": str(value)}
                for label, day, value in late_sources
            ],
        }

    opening = prev_closing if prev_closing is not None else window_navs[first_funded]
    # Flows ON the first funded day ARE the funding event reflected in the
    # opening NAV; including them double-counts (Grizzly $100K defect).
    dep = sum(
        (v for d, v in ext_by_day.items() if v > 0 and d != first_funded), Decimal(0)
    )
    wd = sum(
        (-v for d, v in ext_by_day.items() if v < 0 and d != first_funded),
        Decimal(0),
    )
    int_in = sum((v for d, v in int_in_by_day.items() if d != first_funded), Decimal(0))
    int_out = sum(
        (v for d, v in int_out_by_day.items() if d != first_funded), Decimal(0)
    )

    invested = opening + dep + int_in
    withdrawn = wd + int_out
    net_capital = invested - withdrawn
    current = window_navs[days[-1]]
    gain = current - net_capital

    net_flow_by_day: dict[str, Decimal] = {}
    for source, sign in ((ext_by_day, 1), (int_in_by_day, 1), (int_out_by_day, -1)):
        for d, v in source.items():
            net_flow_by_day[d] = net_flow_by_day.get(d, Decimal(0)) + sign * v

    def _segment_flow(start_exclusive: str, end_inclusive: str) -> Decimal:
        # Every intervening flow date counts, including dates that carry no
        # NAV observation of their own (reviewer P1).
        return sum(
            (
                value
                for day, value in net_flow_by_day.items()
                if start_exclusive < day <= end_inclusive
            ),
            Decimal(0),
        )

    # TWR: Modified-Dietz linked between consecutive observations.
    index = Decimal(100)
    for prev_d, cur_d in zip(days, days[1:]):
        v0, v1 = window_navs[prev_d], window_navs[cur_d]
        flow = _segment_flow(prev_d, cur_d)
        if v0 > 0:
            index *= Decimal(1) + (v1 - v0 - flow) / v0
    twr = index / Decimal(100) - Decimal(1)

    # HWM/MaxDD: DAILY flow-adjusted index. Computing these on the sparse
    # flow-boundary index was the MaxDD-everywhere-wrong defect (intra-window
    # drawdowns invisible).
    daily_index = Decimal(100)
    hwm = Decimal(100)
    max_dd = Decimal(0)
    prev_value: Decimal | None = None
    for d in days:
        value = window_navs[d]
        if prev_value is not None and prev_value > 0:
            segment_start = days[days.index(d) - 1]
            r = (value - prev_value - _segment_flow(segment_start, d)) / prev_value
            daily_index *= Decimal(1) + r
            hwm = max(hwm, daily_index)
            max_dd = min(max_dd, daily_index / hwm - Decimal(1))
        prev_value = value

    span_days = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days
    years = Decimal(span_days) / Decimal(365)
    # Contract semantics: MWR is ALWAYS Gain / Net capital; annualization
    # lives only in annualized_mwr. Overwriting MWR with its annualized
    # form corrupted every epoch >= 1 year (reviewer P1).
    mwr = gain / net_capital if net_capital != 0 else None
    if mwr is None:
        annualized = None
    elif years >= Decimal(1):
        annualized = (Decimal(1) + mwr) ** (Decimal(1) / years) - Decimal(1)
    else:
        annualized = mwr * Decimal(365) / Decimal(max(span_days, 1))

    closing_cashflows: list[tuple[date, Decimal]] = [
        (date.fromisoformat(days[0]), -opening)
    ]
    for d, v in ext_by_day.items():
        if first_funded < d <= days[-1]:
            closing_cashflows.append((date.fromisoformat(d), -v))
    closing_cashflows.append((date.fromisoformat(days[-1]), current))
    xirr_rate = xirr(closing_cashflows)

    return {
        "start": days[0],
        "end": days[-1],
        "opening_capital": opening,
        "deposits": dep,
        "internal_in": int_in,
        "invested": invested,
        "external_withdrawals": wd,
        "internal_out": int_out,
        "withdrawn": withdrawn,
        "net_capital": net_capital,
        "current_value": current,
        "gain": gain,
        "mwr": mwr,
        "annualized_mwr": annualized,
        "xirr_crosscheck": xirr_rate,
        "twr": twr,
        "hwm_index": hwm,
        "max_drawdown": max_dd,
        "nav_observations": len(days),
    }


def assemble_manifest(
    ledger: dict[str, Any],
    baseline: dict[str, Any],
    series: dict[str, dict[str, Decimal]],
    accounts_index: dict[str, dict[str, Any]],
    buckets: dict[str, dict[str, dict[str, Decimal]]],
    schwab_numbers: dict[str, str],
    ibkr_numbers: dict[str, str],
    ledger_packet_id: str,
) -> dict[str, Any]:
    chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in baseline["assignments"]:
        chains[assignment["account_id"]].append(assignment)
    for aid in chains:
        chains[aid].sort(key=lambda a: a.get("effective_start") or "")

    rows: list[dict[str, Any]] = []
    for aid, chain in sorted(chains.items()):
        provider = accounts_index.get(aid, {}).get("provider", "")
        display_num = schwab_numbers.get(aid) or ibkr_numbers.get(aid) or ""
        prev_closing: Decimal | None = None
        for assignment in chain:
            strategy_id = assignment.get("strategy_id")
            if not strategy_id:
                continue
            w0 = assignment.get("effective_start") or ""
            w1 = assignment.get("effective_end")
            window = {
                d: v
                for d, v in series.get(aid, {}).items()
                if d >= w0 and (w1 is None or d <= w1) and v > MIN_FUNDED_NAV
            }
            stats = analyze_epoch(
                window,
                buckets["external"].get(aid, {}),
                buckets["internal_in"].get(aid, {}),
                buckets["internal_out"].get(aid, {}),
                prev_closing,
                w0,
                w1,
            )
            if stats is not None and stats.get("unavailable_reason"):
                blocked: dict[str, Any] = {
                    "provider": provider,
                    "strategy_id": strategy_id,
                    "effective_start": w0,
                    "effective_end": w1,
                    "status": stats["unavailable_reason"],
                    "late_flows": stats.get("late_flows", []),
                }
                blocked["account_id"] = aid
                blocked["account_number"] = display_num
                rows.append(blocked)
                prev_closing = None
                continue
            if stats is None:
                stub: dict[str, Any] = {
                    "provider": provider,
                    "strategy_id": strategy_id,
                    "effective_start": w0,
                    "effective_end": w1,
                    "status": "NO_FUNDED_NAV",
                }
                stub["account_id"] = aid
                stub["account_number"] = display_num
                rows.append(stub)
                prev_closing = None
                continue
            stats.update(
                {
                    "provider": provider,
                    "strategy_id": strategy_id,
                    "effective_start": w0,
                    "effective_end": w1,
                    "status": "OK",
                }
            )
            stats["account_id"] = aid
            stats["account_number"] = display_num
            rows.append(stats)
            prev_closing = None if stats["withdrawn"] > 0 else stats["current_value"]

    return {
        "schema_version": 1,
        "artifact": "account-strategy-manifest",
        "ledger_packet": ledger_packet_id,
        "epoch_continuity_rule": (
            "closing NAV of an ended assignment opens the successor assignment "
            "on the same account; flows on the first funded day belong to the "
            "opening, never double-counted"
        ),
        "reconciliation_source": "raw-ledger classifier (see reconciliation_disclosure)",
        "rows": rows,
    }


MONEY_FIELDS = (
    "invested",
    "withdrawn",
    "net_capital",
    "current_value",
    "gain",
)
PCT_FIELDS = ("mwr", "annualized_mwr", "twr", "max_drawdown")


def format_table(manifest: dict[str, Any]) -> str:
    header = (
        "| Account | Strategy | Started | Ended | Invested | Withdrawn "
        "| Net capital | Current | Gain | MWR% | Ann% | TWR% | HWM | MaxDD% |"
    )
    divider = "|---|" * 14
    lines = [header, divider]
    for row in manifest["rows"]:
        if row["status"] != "OK":
            lines.append(
                f"| {row['account_number']} | {row['strategy_id']} "
                f"| {row['effective_start']} | {row['effective_end'] or 'open'} "
                f"| — | — | — | — | — | — | — | — | — | — | no NAV |"
            )
            continue
        money = " | ".join(f"{row[f]:,.2f}" for f in MONEY_FIELDS)
        pcts = " | ".join(
            "—" if row[f] is None else f"{(row[f] * 100):.2f}" for f in PCT_FIELDS[:3]
        )
        hwm = f"{row['hwm_index']:.2f}"
        maxdd = f"{(row['max_drawdown'] * 100):.2f}"
        lines.append(
            f"| {row['account_number']} | {row['strategy_id']} "
            f"| {row['start']} | {row['end']} | {money} "
            f"| {pcts} | {hwm} | {maxdd} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-json", type=Path, required=True)
    parser.add_argument(
        "--baseline-root",
        type=Path,
        required=True,
        help="dataPortfolio store root containing CURRENT.json (deployment-local)",
    )
    parser.add_argument(
        "--schwab-csv-root",
        type=Path,
        default=None,
        help="playwright_csv dir with historical_balances_<number>_*.csv files",
    )
    parser.add_argument(
        "--ibkr-numbers-json",
        type=Path,
        default=None,
        help="deployment-local mapping of governed account ids to IBKR numbers",
    )
    parser.add_argument(
        "--flow-classes-json",
        type=Path,
        required=True,
        help="validated classified-flows artifact (deduped, reconciled)",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)

    ledger = load_ledger(args.ledger_json)
    baseline = load_baseline(args.baseline_root)
    events = load_flow_classes(args.flow_classes_json)
    buckets, skipped = flow_buckets_from_classes(events)
    series = nav_series(ledger)
    accounts_index = {a["account_id"]: a for a in baseline["accounts"]}
    reconciliation_notes = reconcile_against_raw_ledger(buckets, ledger)
    if skipped:
        reconciliation_notes.append(f"skipped malformed classified events: {skipped}")
    schwab_numbers = (
        schwab_account_numbers(series, accounts_index, args.schwab_csv_root)
        if args.schwab_csv_root is not None
        else {}
    )
    manifest = assemble_manifest(
        ledger,
        baseline,
        series,
        accounts_index,
        buckets,
        schwab_numbers,
        load_ibkr_numbers(args.ibkr_numbers_json),
        ledger_packet_id=args.ledger_json.parent.name,
    )
    manifest["reconciliation_disclosure"] = reconciliation_notes
    if args.format == "json":
        print(json.dumps(manifest, indent=2, default=str))
    else:
        print(format_table(manifest))
        if manifest.get("reconciliation_disclosure"):
            print(
                "\nRECONCILIATION DISCLOSURE (artifact governs):",
                file=sys.stderr,
            )
            for item in manifest["reconciliation_disclosure"]:
                print(f"  - {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

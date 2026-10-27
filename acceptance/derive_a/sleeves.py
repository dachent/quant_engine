"""Pinned sleeve facts and epoch metrics; provider arithmetic migrated from prev/.

Provider facts retain Decimal(28); account returns use the existing engine.
No provider file is opened until every manifest pin has been validated.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, DecimalException, localcontext
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from .sources import AccountFlows, Flow, InputError

if TYPE_CHECKING:
    from .derive import Output
    from .numerics import Member

Z = D(0)
EPS = D("0.000000001")
TOL = D("0.000001")


def encode(x: Any) -> Any:
    if isinstance(x, D):
        return format(x, "f")
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, int):
        return str(x)
    if isinstance(x, dict):
        return {k: encode(v) for k, v in x.items()}
    if isinstance(x, list):
        return [encode(v) for v in x]
    return x


def resolve(t: dict[str, Any], sleeves: list[dict[str, Any]]) -> list[str]:
    day: Any
    tag: Any
    tag = re.sub("^[0-9]+-?", "", t.get("order_reference") or "", count=1)
    day = t["event_date"]
    return [
        s["sleeve_id"]
        for s in sleeves
        if tag
        and s["effective_start"] <= day
        and (s["effective_end"] is None or day <= s["effective_end"])
        and any(
            (
                tag.startswith(r["pattern"])
                if r["match"] == "startswith"
                else r["pattern"] in tag
                if r["match"] == "contains"
                else tag == r["pattern"]
                for r in s["tags"]
            )
        )
    ]


def ibkr_facts(
    src: dict[str, Any], dec: dict[str, Any], tag: dict[str, Any]
) -> dict[str, Any]:
    active: Any
    actual: Any
    aliases: Any
    all_s: Any
    alt: Any
    amount: Any
    ap: Any
    assumptions: Any
    base: Any
    bases: Any
    c: Any
    closure: Any
    comm: Any
    components: Any
    cumul: Any
    cumul_alt: Any
    cumulative: Any
    day: Any
    days: Any
    denom: Any
    disc: Any
    dividend: Any
    divs: Any
    ends: Any
    event: Any
    events: Any
    exp: Any
    factor: Any
    fi: Any
    fo: Any
    holders: Any
    holdings: Any
    hs: Any
    ins: Any
    invalid: Any
    lastmarks: Any
    marks: Any
    matches: Any
    mx: Any
    n: Any
    nav: Any
    net: Any
    np: Any
    nt: Any
    other: Any
    output: Any
    outs: Any
    own: Any
    pairs: Any
    part: Any
    pnl: Any
    pos: Any
    pre: Any
    prev: Any
    previous_marks: Any
    price: Any
    prior_nav: Any
    pvals: Any
    q: Any
    real: Any
    refusals: Any
    residual: Any
    roles: Any
    sap: Any
    sec: Any
    securities: Any
    share: Any
    shares: Any
    starts: Any
    total: Any
    trades: Any
    ts: Any
    uc: Any
    unallocated: Any
    vals: Any
    real = sorted((s["sleeve_id"] for s in tag["sleeves"]))
    other = dec["residual_id"]
    all_s = real + [other]
    refusals = []
    assumptions = [
        "Ledger starts empty; the first day previous value is zero as specified. Initial book differences enter other through reconciliation.",
        "No EXIT for s2 means its NAV rows continue through the last NAV day.",
        "All numeric fields, including counts and pair sequences, are emitted as decimal strings; flags remain JSON booleans.",
        "Manual assignment sleeves is a mapping to allocation fractions; exact sort-key ties need no extra tie breaker because their execution effects are identical.",
        "Reconciliation scaling and vanishing are applied literally without synthetic trades or cash; any resulting P&L remains in the stated formula.",
    ]
    nav = {r["accounting_date"]: r for r in src["change_in_nav_observations"]}
    days = sorted(nav)
    pos = defaultdict(dict)
    invalid = set()
    for p in src["positions"]:
        day = p["as_of_date"]
        sec = p["security_id"]
        if day not in nav:
            refusals.append(f"Position on non-NAV day {day} {sec}")
            continue
        if sec in pos[day]:
            refusals.append(f"Duplicate position {day} {sec}")
        if p.get("asset_category") != "STK" or D(p.get("multiplier", "1")) != 1:
            invalid.add(sec)
        if D(p["quantity"]) != 0 and p.get("mark_price") is None:
            refusals.append(f"Nonzero position lacks mark {day} {sec}")
        pos[day][sec] = p
    trades = defaultdict(list)
    divs = defaultdict(list)
    for t in src["transactions"]:
        if t["kind"].startswith("TRADE:"):
            day = t["event_date"]
            sec = t["security_id"]
            if day not in nav:
                refusals.append(f"Trade on non-NAV day {day} {sec}")
                continue
            if (
                any(
                    (
                        t.get(k) is None
                        for k in ["quantity", "execution_price", "commission"]
                    )
                )
                or t.get("commission", {}).get("value") is None
                or sec in invalid
                or (t.get("amount", {}).get("currency") != "USD")
            ):
                refusals.append(f"Invalid trade {day} {sec}")
                continue
            matches = resolve(t, tag["sleeves"])
            if len(matches) > 1:
                refusals.append(f"Multiple tag matches {day} {sec}")
                continue
            trades[day].append((t, matches[0] if matches else None))
        elif t["kind"] in [
            "CASH_ACTIVITY:Dividends",
            "CASH_ACTIVITY:Payment In Lieu Of Dividends",
            "CASH_ACTIVITY:Withholding Tax",
        ] and t.get("security_id"):
            day = t.get("nav_recognition_date") or t["event_date"]
            if day not in nav:
                refusals.append(f"Dividend on non-NAV day {day}")
                continue
            divs[day].append(t)
    disc = {
        k: []
        for k in [
            "manual_assignments",
            "reconciliation_gaps",
            "reconciliation_scalings",
            "offsetting_residues",
            "unallocated_dividends",
        ]
    }
    holdings = {s: defaultdict(lambda: Z) for s in all_s}
    events = dec["funding_events"]
    funded = [s for s in real if events.get(s)]
    unfunded = [s for s in real if not events.get(s)]
    if unfunded:
        assumptions.append(
            "Sleeves with no declared funding events ("
            + ", ".join(unfunded)
            + ") have no rows, cumulative figures, flows, epochs or metrics and "
            "do not affect exposure shares; their ledger P&L remains in closure "
            "residuals. The residual-parent rule still uses the full tag map."
        )
    output = {s: [] for s in funded}
    aliases = dec["symbol_aliases"]["aliases"]
    starts = {s: min(events[s]) for s in funded}
    ends = {
        s: next((d for d, e in events[s].items() if e["kind"] == "EXIT"), days[-1])
        for s in funded
    }
    cumul = {s: Z for s in funded}
    cumul_alt = cumul.copy()
    closure = []
    pairs = []
    lastmarks = {}
    prior_nav = None

    def pair(day: str, amount: D, fr: str, to: str, kind: str, how: str) -> None:
        pairs.append(
            dict(
                day=day,
                sequence=len(pairs) + 1,
                amount=amount,
                out_account=tag["parent_account_id"] + "-" + fr,
                in_account=tag["parent_account_id"] + "-" + to,
                kind=kind,
                pairing=how,
                from_sleeve=fr,
                to_sleeve=to,
            )
        )

    for day in days:
        for old, a in aliases.items():
            if a["from_day"] == day:
                for s in all_s:
                    holdings[s][a["to"]] += holdings[s].pop(old, Z)
        for s in all_s:
            holdings[s] = defaultdict(
                lambda: Z, {k: q for k, q in holdings[s].items() if abs(q) > EPS}
            )
        prev = {s: dict(holdings[s]) for s in all_s}
        nt = {s: Z for s in all_s}
        comm = nt.copy()
        dividend = nt.copy()
        ts = trades[day]
        if any((not t.get("executed_time") for t, s in ts)) and any(
            (s is None for t, s in ts)
        ):
            refusals.append(
                f"Ambiguous missing-time and unassigned execution day {day}; used specified order"
            )
        ts.sort(
            key=lambda x: (
                x[0].get("executed_time") or "",
                x[1] is None,
                x[0]["security_id"],
                D(x[0]["quantity"]),
                D(x[0]["execution_price"]),
                D(x[0]["commission"]["value"]),
            )
        )
        for t, s in ts:
            sec = t["security_id"]
            q = D(t["quantity"])
            price = D(t["execution_price"])
            c = D(t["commission"]["value"])
            if s:
                shares = {s: D(1)}
            else:
                holders = [s for s in real if holdings[s][sec] * q < 0]
                denom = sum((abs(holdings[s][sec]) for s in holders), Z)
                shares = (
                    {s: abs(holdings[s][sec]) / denom for s in holders}
                    if holders
                    else {
                        dec["unheld_fallback"].get(
                            sec, tag["unmatched"]["fallback_sleeve_id"]
                        )
                        or other: D(1)
                    }
                )
                disc["manual_assignments"].append(
                    dict(
                        day=day,
                        security_id=sec,
                        quantity=q,
                        how="split"
                        if len(holders) > 1
                        else "assigned"
                        if holders
                        else "unheld",
                        sleeves=shares,
                    )
                )
            for s, share in shares.items():
                part = q * share
                holdings[s][sec] += part
                nt[s] += part * price
                comm[s] += c * share
        securities = set(pos[day]) | {k for s in all_s for k in holdings[s]}
        for sec in sorted(securities):
            total = sum((holdings[s][sec] for s in all_s), Z)
            actual = D(pos[day].get(sec, {}).get("quantity", "0"))
            if abs(total - actual) <= TOL:
                if actual == 0 and any((abs(holdings[s][sec]) > EPS for s in all_s)):
                    disc["offsetting_residues"].append(
                        dict(
                            day=day,
                            security_id=sec,
                            quantities={
                                s: holdings[s][sec] for s in all_s if holdings[s][sec]
                            },
                        )
                    )
                    for s in all_s:
                        holdings[s].pop(sec, None)
            else:
                disc["reconciliation_gaps"].append(
                    dict(day=day, security_id=sec, derived=total, account=actual)
                )
                if abs(total) > EPS and abs(actual) > EPS and (total * actual > 0):
                    factor = actual / total
                    for s in all_s:
                        holdings[s][sec] *= factor
                    disc["reconciliation_scalings"].append(
                        dict(day=day, security_id=sec, factor=factor)
                    )
                elif abs(total) > EPS and actual == 0:
                    for s in all_s:
                        holdings[s].pop(sec, None)
                else:
                    holdings[other][sec] += actual - total
        unallocated = Z
        for t in divs[day]:
            sec = t["security_id"]
            amount = D(t["amount"]["value"])
            hs = {s: prev[s].get(sec, Z) for s in all_s if prev[s].get(sec, Z) > 0}
            if not hs:
                hs = {s: holdings[s][sec] for s in all_s if holdings[s][sec] > 0}
            denom = sum(hs.values(), Z)
            if denom:
                for s, q in hs.items():
                    dividend[s] += amount * q / denom
            else:
                unallocated += amount
        if unallocated:
            disc["unallocated_dividends"].append(dict(day=day, amount=unallocated))
        marks = {
            sec: D(p["mark_price"])
            for sec, p in pos[day].items()
            if p.get("mark_price") is not None
        }
        previous_marks = lastmarks.copy()
        for old, a in aliases.items():
            if (
                a["from_day"] == day
                and a["to"] not in previous_marks
                and (old in lastmarks)
            ):
                previous_marks[a["to"]] = lastmarks[old]

        def value(book: Any, prices: Any, label: Any) -> Any:
            v: Any
            v = Z
            for sec, q in sorted(book.items()):
                if abs(q) <= EPS:
                    continue
                if sec in prices:
                    v += q * prices[sec]
                elif abs(q) > TOL:
                    refusals.append(
                        f"Missing {label} mark {day} {sec}; value incomplete"
                    )
            return v

        vals = {s: value(holdings[s], marks, "current") for s in all_s}
        pvals = {
            s: value(prev[s], previous_marks, "previous") if prior_nav else Z
            for s in all_s
        }
        pnl = {s: vals[s] - pvals[s] - nt[s] for s in all_s}
        own = {s: pnl[s] + comm[s] + dividend[s] for s in all_s}
        n = nav[day]
        if prior_nav is not None and D(n["opening_nav"]) != prior_nav:
            refusals.append(f"NAV continuity break {day}")
        components = {r["source_component_key"]: D(r["value"]) for r in n["components"]}
        roles = {}
        for role, keys in dec["change_in_nav_components"].items():
            for k in keys:
                if k not in components:
                    refusals.append(f"Missing component {day} {k}")
            roles[role] = sum((components.get(k, Z) for k in keys), Z)
        active = {}
        for s in funded:
            if not starts[s] <= day <= ends[s]:
                continue
            event = events[s].get(day, {}).get("kind", "")
            np = output[s][-1]["nav_end"] if output[s] else Z
            fi = nt[s] if event == "IN" else Z
            base = np + fi
            pre = base + own[s]
            fo = (
                nt[s]
                if event == "OUT"
                else -pre
                if event == "EXIT"
                else vals[s] - pre
                if event == "SWEEP"
                else Z
            )
            active[s] = dict(
                day=day,
                event=event,
                nav_prev=np,
                flow_in=fi,
                flow_out=fo,
                nav_end=Z if event == "EXIT" else pre + fo,
                value=Z if event == "EXIT" else vals[s],
                exposure=abs(pvals[s]) + max(nt[s], Z),
                price_pnl=pnl[s],
                commission=comm[s],
                dividend=dividend[s],
                own_pnl=own[s],
                base=base,
                r_own=own[s] / base if base > 1 else Z,
            )
        exp = sum((r["exposure"] for r in active.values()), Z)
        bases = sum((max(r["base"], Z) for r in active.values()), Z)
        for s, r in active.items():
            share = r["exposure"] / exp if exp else Z
            alt = max(r["base"], Z) / bases if bases else Z
            r["exposure_share"] = share
            for key in ["interest", "fees", "dividend_accrual"]:
                r["alloc_" + key] = roles[key] * share
            r["alloc_total"] = sum(
                (r["alloc_" + k] for k in ["interest", "fees", "dividend_accrual"]), Z
            )
            r["alloc_total_alt"] = sum(
                (roles[k] * alt for k in ["interest", "fees", "dividend_accrual"]), Z
            )
            cumul[s] += r["alloc_total"]
            cumul_alt[s] += r["alloc_total_alt"]
            for suffix, cu in [("", cumul[s]), ("_alt", cumul_alt[s])]:
                r["r_alloc" + suffix] = (
                    (own[s] + r["alloc_total" + suffix]) / r["base"]
                    if r["base"] > 1
                    else Z
                )
                r["nav_end_after_alloc" + suffix] = (
                    Z if r["event"] == "EXIT" else r["nav_end"] + cu
                )
            output[s].append(r)
        sap = sum((r["own_pnl"] + r["alloc_total"] for r in active.values()), Z)
        residual = sum((own[s] for s in all_s if s not in active), Z) + unallocated
        ap = D(n["ending_nav"]) - D(n["opening_nav"]) - roles["external_flow"]
        uc = Z
        ins = []
        outs = []
        for s, r in active.items():
            net = r["flow_in"] + r["flow_out"]
            if not net:
                continue
            if events[s].get(day, {}).get("counterparty") == "UNALLOCATED_ACCOUNT_CASH":
                uc -= net
                pair(
                    day,
                    abs(net),
                    "unallocated" if net > 0 else s,
                    s if net > 0 else "unallocated",
                    "UNALLOCATED_CASH",
                    "DECLARED_UNALLOCATED",
                )
            else:
                (ins if net > 0 else outs).append([s, abs(net)])
        i = j = 0
        while i < len(ins) and j < len(outs):
            amount = min(ins[i][1], outs[j][1])
            pair(day, amount, outs[j][0], ins[i][0], "REALLOCATION", "SLEEVE_PAIR")
            ins[i][1] -= amount
            outs[j][1] -= amount
            if not ins[i][1]:
                i += 1
            if not outs[j][1]:
                j += 1
        for s, amount in ins:
            if amount:
                pair(
                    day,
                    amount,
                    "unallocated",
                    s,
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                )
        for s, amount in outs:
            if amount:
                pair(
                    day,
                    amount,
                    s,
                    "unallocated",
                    "UNALLOCATED_CASH",
                    "UNALLOCATED_REMAINDER",
                )
        closure.append(
            dict(
                day=day,
                account_pnl=ap,
                sleeves_own_plus_alloc=sap,
                residual_and_post_exit=residual,
                unexplained=ap - sap - residual,
                unallocated_cash_flow=uc,
            )
        )
        lastmarks = marks
        prior_nav = D(n["ending_nav"])
    cumulative = {}
    for s, rows in output.items():
        c = dict(
            first_day=rows[0]["day"],
            last_day=rows[-1]["day"],
            rows=len(rows),
            final_nav_end=rows[-1]["nav_end"],
            final_nav_end_after_alloc=rows[-1]["nav_end_after_alloc"],
        )
        for key in ["flow_in", "flow_out", "own_pnl", "alloc_total", "alloc_total_alt"]:
            c["sum_" + key] = sum((r[key] for r in rows), Z)
        for key, out in [
            ("r_own", "twr_own"),
            ("r_alloc", "twr_after_alloc"),
            ("r_alloc_alt", "twr_after_alloc_alt"),
        ]:
            factor = D(1)
            for r in rows:
                factor *= 1 + r[key]
            c[out] = factor - 1
        cumulative[s] = c
    mx = max((abs(r["unexplained"]) for r in closure))
    total = sum((r["unexplained"] for r in closure), Z)
    return dict(
        spec="SPEC-IBKR",
        window=dict(first_day=days[0], last_day=days[-1]),
        sleeves=output,
        cumulative=cumulative,
        closure=closure,
        closure_summary=dict(
            max_abs_unexplained=mx,
            window_unexplained=total,
            per_day_tolerance_met=mx <= D(dec["closure_tolerance"]["per_day"]),
            window_tolerance_met=abs(total) <= D(dec["closure_tolerance"]["window"]),
        ),
        pairs=pairs,
        disclosures=disc,
        refusals=refusals,
        assumptions=assumptions,
    )


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def nextday(day: str) -> str:
    return (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()


def elapsed(a: datetime, b: datetime) -> D:
    delta: Any
    delta = abs(a - b)
    return D(delta.days * 86400 + delta.seconds) + D(delta.microseconds) / D(1000000)


def recognition(fill: str, kind: str, first: str) -> str:
    return fill if kind == "BUY" and fill == first else nextday(fill)


def quantbase_facts(
    dec: dict[str, Any], tags: dict[str, Any], csv_data: dict[str, list[dict[str, str]]]
) -> dict[str, Any]:
    all_at: Any
    amount: Any
    assumptions: Any
    at: Any
    attributed: Any
    aum: Any
    buy: Any
    candidates: Any
    cash: Any
    cb: Any
    checks: Any
    count: Any
    counter: Any
    cs: Any
    cutoff: Any
    daily: Any
    dash: Any
    dashboards: Any
    data: Any
    day: Any
    deposits: Any
    derived: Any
    diff: Any
    divout: Any
    divtotal: Any
    ed: Any
    eligible: Any
    end: Any
    event: Any
    excluded: Any
    exemptions: Any
    exitday: Any
    expected: Any
    factor: Any
    fee: Any
    ff: Any
    fid: Any
    files: Any
    fills: Any
    first: Any
    flows: Any
    fund: Any
    funds: Any
    i: Any
    ignored: Any
    ins: Any
    keep: Any
    key: Any
    kind: Any
    last: Any
    names: Any
    net: Any
    net_by: Any
    np: Any
    nz: Any
    od: Any
    oo: Any
    opening: Any
    orders: Any
    out: Any
    outs: Any
    owners: Any
    pairs: Any
    parent: Any
    partner: Any
    pnl: Any
    proceeds: Any
    provider: Any
    pseudo: Any
    rec: Any
    refusals: Any
    remaining: Any
    residual: Any
    ret: Any
    rows: Any
    sleeve: Any
    stamp: Any
    tolerance: Any
    traded: Any
    typ: Any
    unattributed: Any
    unique: Any
    val: Any
    volatis: Any

    def csvread(name: str) -> list[dict[str, str]]:
        return csv_data[name]

    cutoff = dec["data_cutoff"]
    parent = dec["parent_account_id"]
    pseudo = parent + "-unallocated"
    names = {r["tags"][0]["pattern"]: r["sleeve_id"] for r in tags["sleeves"]}
    names["107"] = "volatis"
    refusals = []
    assumptions = [
        "Numeric counts and amounts are decimal strings; boolean flags and null returns retain their JSON types.",
        "Exact duplicates are collapsed for every activity type by (activity_type,id); conflicting duplicates are refused and the first row retained.",
        "Cutoff filtering uses filled_txn fill day, executed order filled_at, transfer/journal created_at and dividend date; RBLNC uses transaction time. Excluded-row count covers unique activity rows, not dashboard observations.",
        "Fund tag-map end dates identify retirement; the specified exit observation on the following day is included.",
        "Null opening interval returns are omitted from the TWR product, equivalent to a factor of one.",
        "Pairs with two retired sides use the outflow recognition day; with one retired side use that side, except an out-of-scope outflow uses fill day plus one as expressly specified.",
        "Unpaired non-fee fund 107 flows also produce perimeter remainder pairs, since all three funds participate in section 4.",
        "NAV-total maximum and days_checked exclude exempt days; exempt differences are reported separately. Missing early dashboard observations are zero only before the first available row.",
        "A nonzero declared cash residual is disclosed as unexplained even when it matches the declared amount within tolerance.",
    ]
    dashboards = {}
    files = {
        "crisis": "Crisis_Factor_Investing",
        "singleindex": "Single_Index_Leveraged_In_and_Out",
        "volatis": "Volatis_Strategy",
        "total": "Total",
    }
    for key, stem in files.items():
        data = {}
        for r in csvread("dashboard_" + stem + "_pricehistory.csv"):
            day = datetime.strptime(r["time"], "%m/%d/%Y").date().isoformat()
            if day > cutoff:
                continue
            if day in data:
                refusals.append(f"Duplicate dashboard date {key} {day}")
            data[day] = {k: D(r[k]) for k in ["value", "cost_basis", "profit_loss"]}
        dashboards[key] = data
    unique = {}
    for r in csvread("account_activity.csv"):
        key = (r["activity_type"], r["id"])
        if key in unique:
            if unique[key] != r:
                refusals.append(f"Conflicting duplicate {key}")
        else:
            unique[key] = r
    rows = list(unique.values())
    orders = defaultdict(list)
    for r in rows:
        if r["activity_type"] == "order" and r["executed"] == "Y" and r["filled_at"]:
            orders[r["transaction"]].append(r)
    fills = []
    volatis = []
    excluded = 0
    keep = []
    for r in rows:
        typ = r["activity_type"]
        if (
            typ == "filled_txn"
            and r["type"] in ["BUY", "SELL", "MAX_S"]
            and (r["filled"] == "Y")
        ):
            fee = r["created_to_cover_fees"] == "True" and r["type"] in [
                "SELL",
                "MAX_S",
            ]
            oo = orders[r["id"]]
            eligible = [o for o in oo if fee or o["created_to_cover_fees"] != "True"]
            if not eligible:
                refusals.append(f"No eligible executed order for transaction {r['id']}")
                continue
            at = max((utc(o["filled_at"]) for o in eligible))
            all_at = max((utc(o["filled_at"]) for o in oo))
            day = at.date().isoformat()
            if day > cutoff:
                excluded += 1
                continue
            amount = D(r["amount"])
            fund = r["fund"]
            kind = r["type"]
            if kind == "BUY" and amount <= 0 or (kind != "BUY" and amount >= 0):
                refusals.append(f"Wrong fill amount sign transaction {r['id']}")
            if fund not in names:
                refusals.append(f"Unknown fund {fund}")
                continue
            first = dec["funds"].get(names[fund], {}).get("first_nonzero_day", "")
            fills.append(
                dict(
                    fund=fund,
                    txn_id=r["id"],
                    kind=kind,
                    amount=amount,
                    fee_sell=fee,
                    fill_day=day,
                    recognition_day=recognition(day, kind, first),
                    fill_at_utc=at.isoformat(),
                    time_utc=utc(r["time"]).isoformat(),
                )
            )
            if fund == "107":
                volatis.append(
                    dict(
                        txn_id=r["id"],
                        kind=kind,
                        amount=amount,
                        fill_day_excluding_fee_orders=day,
                        fill_day_including_all_orders=all_at.date().isoformat(),
                        fee_sell=fee,
                    )
                )
            continue
        stamp = (
            r["filled_at"]
            if typ == "order" and r["filled_at"]
            else r["date"]
            if typ == "dividend"
            else r["created_at"]
            if typ in ["transfer", "journal"]
            else r["time"]
        )
        if stamp and utc(stamp).date().isoformat() > cutoff:
            excluded += 1
            continue
        keep.append(r)
    fills.sort(key=lambda f: (f["fill_at_utc"], f["txn_id"]))
    funds = {}
    for sleeve, decl in dec["funds"].items():
        fid = decl["fund_id"]
        dash = dashboards[sleeve]
        nz = sorted((d for d, r in dash.items() if r["value"] != 0))
        first = decl["first_nonzero_day"]
        last = decl["last_nonzero_day"]
        exitday = nextday(last)
        if (nz[0], nz[-1]) != (first, last):
            refusals.append(
                f"Epoch mismatch {sleeve}: observed {nz[0]}..{nz[-1]} declared {first}..{last}"
            )
        ff = [f for f in fills if f["fund"] == fid]
        flows = [f for f in ff if not f["fee_sell"]]
        fee = [f for f in ff if f["fee_sell"]]
        daily = defaultdict(lambda: [Z, Z])
        for f in flows:
            daily[f["recognition_day"]][0 if f["kind"] == "BUY" else 1] += f["amount"]
            if not first <= f["recognition_day"] <= exitday:
                refusals.append(f"Flow outside epoch {sleeve} {f['txn_id']}")
        out = []
        np = Z
        factor = D(1)
        day = first
        while day <= exitday:
            if day not in dash:
                refusals.append(f"Dashboard gap {sleeve} {day}")
                day = nextday(day)
                continue
            end = dash[day]["value"]
            fi, fo = daily[day]
            pnl = end - np - fi - fo
            ret = pnl / np if np > 0 else None
            event = (
                "OPEN"
                if day == first
                else "EXIT"
                if day == exitday
                else "BOTH"
                if fi and fo
                else "IN"
                if fi
                else "OUT"
                if fo
                else ""
            )
            out.append(
                dict(
                    day=day,
                    event=event,
                    nav_prev=np,
                    flow_in=fi,
                    flow_out=fo,
                    nav_end=end,
                    own_pnl=pnl,
                    interval_return=ret,
                )
            )
            if ret is not None:
                factor *= 1 + ret
            np = end
            day = nextday(day)
        net = sum((f["amount"] for f in ff), Z)
        cb = dash[cutoff]["cost_basis"]
        diff = net - cb
        opening = sum(
            (
                f["amount"]
                for f in flows
                if f["kind"] == "BUY" and f["fill_day"] == first
            ),
            Z,
        )
        proceeds = -sum((f["amount"] for f in flows if f["kind"] == "MAX_S"), Z)
        od = dash[first]["value"] - opening
        ed = proceeds - dash[last]["value"]
        if abs(diff) > D(dec["closure_tolerance"]["cost_basis"]):
            refusals.append(f"Cost basis mismatch {sleeve}: {diff}")
        if abs(od) > D(decl["opening_tolerance"]):
            refusals.append(f"Opening mismatch {sleeve}: {od}")
        if abs(ed) > abs(dash[last]["value"]) * D(
            dec["closure_tolerance"]["exit_relative"]
        ):
            refusals.append(f"Exit mismatch {sleeve}: {ed}")
        if dash[exitday]["value"] != 0:
            refusals.append(f"Nonzero exit NAV {sleeve}")
        funds[sleeve] = dict(
            fund_id=fid,
            first_day=first,
            last_nonzero_day=last,
            exit_recognition_day=exitday,
            rows=out,
            net_external_flow=sum((f["amount"] for f in flows), Z),
            fee_sells_net=sum((f["amount"] for f in fee), Z),
            own_pnl_total=sum((r["own_pnl"] for r in out), Z),
            twr=factor - 1,
            flows=[
                {k: v for k, v in f.items() if k not in ["fund", "fee_sell"]}
                for f in flows
            ],
            fee_sells=[
                {k: f[k] for k in ["txn_id", "fill_day", "amount"]} for f in fee
            ],
            net_fills=net,
            dashboard_cost_basis=cb,
            cost_basis_difference=diff,
            opening_value=dash[first]["value"],
            opening_purchase=opening,
            opening_difference=od,
            exit_proceeds=proceeds,
            last_nonzero_value=dash[last]["value"],
            exit_difference=ed,
        )
    checks = []
    exemptions = []
    for day, row in sorted(dashboards["total"].items()):
        val = Z
        for key in ["crisis", "singleindex", "volatis"]:
            if day in dashboards[key]:
                val += dashboards[key][day]["value"]
            elif day >= min(dashboards[key]):
                refusals.append(f"NAV-total missing dashboard {key} {day}")
        diff = val - row["value"]
        if day in dec["nav_total_exempt_days"]:
            exemptions.append(dict(day=day, difference=diff))
        else:
            checks.append(abs(diff))
            if abs(diff) > D(dec["closure_tolerance"]["nav_total"]):
                refusals.append(f"NAV-total mismatch {day}: {diff}")
    deposits = Z
    count = 0
    ignored = 0
    aum = Z
    partner = Z
    divtotal = Z
    traded = defaultdict(set)
    for r in keep:
        if (
            r["activity_type"] == "order"
            and r["executed"] == "Y"
            and r["symbol"]
            and (r["symbol"] != "$USD")
        ):
            traded[r["symbol"]].add(r["fund"])
    attributed = {s: Z for s in dec["funds"]}
    counter = {"107": Z}
    unattributed = Z
    divout = []
    for r in keep:
        typ = r["activity_type"]
        if typ == "transfer":
            if r["status"] == "C" and r["type"] in ["DEPOSIT", "WITHDRAWAL"]:
                deposits += abs(D(r["amount"])) * (1 if r["type"] == "DEPOSIT" else -1)
                count += 1
            else:
                ignored += 1
        elif typ == "journal" and r["status"] == "COMPLETE":
            if r["type"] == "AUM_FEE":
                aum += D(r["amount"])
            elif r["type"] == "PARTNER_SUB_FEE":
                partner += D(r["amount"])
        elif typ == "dividend":
            amount = D(r["amount"])
            divtotal += amount
            owners = traded[r["symbol"]]
            day = utc(r["date"]).date().isoformat()
            fid = next(iter(owners)) if len(owners) == 1 else None
            if fid in names and names[fid] in funds:
                sleeve = names[fid]
                attributed[sleeve] += amount
                divout.append(
                    dict(
                        sleeve=sleeve,
                        day=day,
                        symbol=r["symbol"],
                        amount=amount,
                        after_exit=day > funds[sleeve]["last_nonzero_day"],
                    )
                )
            elif fid == "107":
                counter[fid] += amount
            else:
                unattributed += amount
    net_by = {
        fid: sum((f["amount"] for f in fills if f["fund"] == fid), Z)
        for fid in ["14", "56", "107"]
    }
    net = sum(net_by.values(), Z)
    derived = deposits - net + divtotal - aum - partner
    provider = D(
        next(
            (
                r["settled_balance"]
                for r in csvread("accounts.csv")
                if r["type"] == "alpaca"
            )
        )
    )
    residual = derived - provider
    expected = D(dec["open_items"]["cash_residual"]["amount"])
    tolerance = D(dec["open_items"]["cash_residual"]["tolerance"])
    if abs(residual - expected) > tolerance:
        refusals.append(
            f"Cash residual differs from declaration: actual {residual}, declared {expected}"
        )
    if abs(residual - expected) > tolerance and abs(residual) > tolerance:
        refusals.append(
            f"Unexplained account cash residual {residual} (declared {expected})"
        )
    flows = [f for f in fills if not f["fee_sell"]]
    ins = [f for f in flows if f["kind"] == "BUY"]
    outs = [f for f in flows if f["kind"] != "BUY"]
    candidates = defaultdict(list)
    for o in outs:
        for i in ins:
            if o["fund"] == i["fund"] or (o["fund"] == "107" and i["fund"] == "107"):
                continue
            if (
                o["fill_day"] == i["fill_day"]
                and elapsed(utc(o["time_utc"]), utc(i["time_utc"]))
                <= D(dec["pairing"]["window_seconds"])
                and (
                    abs(abs(o["amount"]) - i["amount"])
                    <= D(dec["closure_tolerance"]["pairing_amount"])
                )
            ):
                candidates[o["txn_id"]].append(i)
                candidates[i["txn_id"]].append(o)
    for tid, cs in candidates.items():
        if len(cs) > 1:
            refusals.append(
                f"Ambiguous reallocation candidates transaction {tid}: "
                + ",".join((c["txn_id"] for c in cs))
            )
    remaining = {f["txn_id"]: abs(f["amount"]) for f in flows}
    pairs = []

    def account(f: Any) -> Any:
        return (
            dec["out_of_scope_funds"][f["fund"]]["counterparty_account_id"]
            if f["fund"] == "107"
            else parent + "-" + names[f["fund"]]
        )

    for o in outs:
        cs = candidates[o["txn_id"]]
        if len(cs) != 1 or len(candidates[cs[0]["txn_id"]]) != 1:
            continue
        i = cs[0]
        amount = min(abs(o["amount"]), i["amount"])
        remaining[o["txn_id"]] -= amount
        remaining[i["txn_id"]] -= amount
        rec = nextday(o["fill_day"]) if o["fund"] == "107" else o["recognition_day"]
        pairs.append(
            dict(
                event_day=o["fill_day"],
                recognition_day=rec,
                amount=amount,
                out_account=account(o),
                in_account=account(i),
                kind="REALLOCATION",
                pairing="COUNTERPARTY_PAIR"
                if "107" in [o["fund"], i["fund"]]
                else "SLEEVE_PAIR",
                **{"from": names[o["fund"]], "to": names[i["fund"]]},
                _sort=(o["fill_day"], 0, o["fill_at_utc"], o["txn_id"]),
            )
        )
    for f in flows:
        amount = remaining[f["txn_id"]]
        if not amount:
            continue
        buy = f["kind"] == "BUY"
        pairs.append(
            dict(
                event_day=f["fill_day"],
                recognition_day=f["recognition_day"],
                amount=amount,
                out_account=pseudo if buy else account(f),
                in_account=account(f) if buy else pseudo,
                kind="UNALLOCATED_CASH",
                pairing="UNALLOCATED_REMAINDER",
                **{
                    "from": "unallocated" if buy else names[f["fund"]],
                    "to": names[f["fund"]] if buy else "unallocated",
                },
                _sort=(f["fill_day"], 1, f["fill_at_utc"], f["txn_id"]),
            )
        )
    pairs.sort(key=lambda p: p["_sort"])
    for n, p in enumerate(pairs, 1):
        p.pop("_sort")
        p["sequence"] = n
    cash = dict(
        deposits=deposits,
        deposit_count=count,
        ignored_transfers=ignored,
        net_fund_flows_by_fund=net_by,
        net_fund_flows=net,
        dividends=divtotal,
        aum_fees=aum,
        partner_subscription_fees=partner,
        derived_cash=derived,
        provider_settled_cash=provider,
        residual=residual,
        declared_residual=expected,
        declared_residual_difference=residual - expected,
        declared_residual_tolerance_met=abs(residual - expected) <= tolerance,
    )
    return dict(
        spec="SPEC-QB",
        funds=funds,
        pairs=pairs,
        closure=dict(
            nav_total=dict(
                days_checked=len(checks),
                max_abs_difference=max(checks),
                exempt_days=exemptions,
                tolerance_met=max(checks) <= D(dec["closure_tolerance"]["nav_total"]),
            ),
            cash=cash,
            dividends=dict(
                total=divtotal,
                attributed_to_sleeves=attributed,
                attributed_to_counterparty_funds=counter,
                unattributed=unattributed,
            ),
            rows_after_cutoff_excluded=excluded,
        ),
        dividends=sorted(divout, key=lambda x: (x["day"], x["sleeve"], x["symbol"])),
        volatis_fills=sorted(
            volatis, key=lambda x: (x["fill_day_excluding_fee_orders"], x["txn_id"])
        ),
        refusals=refusals,
        assumptions=assumptions,
        disclosures=(
            {
                "cash_residual": {
                    "derived_cash": derived,
                    "provider_settled_cash": provider,
                    "residual": residual,
                    "declared_amount": expected,
                    "tolerance": tolerance,
                    "status": "DECLARED_WITHIN_TOLERANCE",
                }
            }
            if abs(residual - expected) <= tolerance
            else {}
        ),
    )


ROLES = frozenset(
    {
        "crosswalk",
        "ibkr_catalog",
        "tag_map",
        "ibkr_declared_inputs",
        "quantbase_declared_inputs",
        "quantbase_accounts",
        "quantbase_account_activity",
        "quantbase_dashboard_crisis",
        "quantbase_dashboard_singleindex",
        "quantbase_dashboard_volatis",
        "quantbase_dashboard_total",
    }
)
ACCOUNT_METRICS = frozenset(
    {
        "twr.account",
        "mwr.account",
        "flows.account.net",
        "flows.account.count",
    }
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key/role {key}")
        result[key] = value
    return result


def _json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8-sig"), parse_float=D, object_pairs_hook=_unique_object
        )
    except (ValueError, UnicodeError) as exc:
        raise InputError(f"invalid JSON in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label}: expected an object")
    return value


def load_sleeve_inputs(
    path: Path,
    qe_root: Path,
    repo_root: Path,
    cutoff: dt.date,
) -> tuple[dict[str, dict[str, str]], dict[str, bytes]]:
    """Validate the complete role set and every pin before interpreting any facts."""
    try:
        manifest = _json(path.read_bytes(), str(path))
    except OSError as exc:
        raise InputError(f"cannot read sleeve inputs {path}") from exc
    if manifest.get("schema") != "qe-cp-sleeve-inputs/1":
        raise InputError("unsupported sleeve inputs schema")
    if manifest.get("cutoff") != cutoff.isoformat():
        raise InputError("sleeve inputs cutoff differs from --cutoff")
    entries = manifest.get("inputs")
    if not isinstance(entries, dict) or set(entries) != ROLES:
        actual = set(entries) if isinstance(entries, dict) else set()
        raise InputError(
            f"sleeve roles missing {sorted(ROLES - actual)}, "
            f"unknown {sorted(actual - ROLES)}"
        )
    records: dict[str, dict[str, str]] = {}
    data: dict[str, bytes] = {}
    for role in sorted(ROLES):
        entry = entries[role]
        if not isinstance(entry, dict):
            raise InputError(f"{role}: expected an input object")
        pin, relative, root_name = (entry.get(k) for k in ("sha256", "path", "root"))
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", pin):
            raise InputError(f"{role}: bad sha256 form")
        if root_name not in ("qe", "repo") or not isinstance(relative, str):
            raise InputError(f"{role}: invalid root/path")
        if not relative or Path(relative).anchor or PureWindowsPath(relative).anchor:
            raise InputError(f"{role}: path must stay relative to its root")
        try:
            base = (qe_root if root_name == "qe" else repo_root).resolve()
            target = (base / relative).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise InputError(f"{role}: cannot resolve path within its root") from exc
        if not target.is_relative_to(base):
            raise InputError(f"{role}: path must stay relative to its root")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise InputError(f"{role}: cannot read {relative}") from exc
        if hashlib.sha256(raw).hexdigest() != pin.lower():
            raise InputError(f"{role}: sha256 mismatch for {relative}")
        records[role] = {"path": relative, "sha256": pin}
        data[role] = raw
    return records, data


def slice_ibkr(
    catalog: dict[str, Any],
    parent: str,
    first: str,
    last: str,
) -> dict[str, Any]:
    """One linear pass per source family; cash uses its recognition date."""
    version = catalog.get("catalog_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 7:
        raise InputError("IBKR catalog schema must be at least 7")
    result: dict[str, Any] = {}
    for family, date_key in (
        ("transactions", "event_date"),
        ("positions", "as_of_date"),
        ("change_in_nav_observations", "accounting_date"),
        ("corporate_actions", ""),
        ("accounts", ""),
    ):
        selected: list[dict[str, Any]] = []
        for row in catalog[family]:
            if row.get("account_id") != parent:
                continue
            day = row.get(date_key)
            if family == "transactions" and not row["kind"].startswith("TRADE:"):
                day = row.get("nav_recognition_date") or day
            if not date_key or (isinstance(day, str) and first <= day <= last):
                selected.append(row)
        result[family] = selected
    return result


def _leg(day: str, event: str, kind: str, amount: D, label: str = "") -> dict[str, Any]:
    return {
        "recognition_day": day,
        "event_day": event,
        "class": kind,
        "amount": amount,
        "kind": label,
    }


def epoch(
    account: str,
    strategy: str,
    nav: dict[str, D],
    end: str | None,
    legs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not nav:
        raise InputError(f"{account}: no epoch NAV observations")
    days = sorted(nav)
    legs = sorted(
        legs,
        key=lambda leg: (
            leg["recognition_day"],
            leg["event_day"],
            leg["class"],
            leg.get("kind", ""),
            leg["amount"],
        ),
    )
    capital = [leg for leg in legs if days[0] < leg["recognition_day"] <= days[-1]]
    net = nav[days[0]] + sum(
        (
            leg["amount"] * (1 if leg["class"] == "INTERNAL_IN" else -1)
            for leg in capital
        ),
        Z,
    )
    return {
        "account_id": account,
        "strategy_id": strategy,
        "effective_start": days[0],
        "effective_end": end,
        "opening": nav[days[0]],
        "closing": nav[days[-1]],
        "net_capital": net,
        "legs": legs,
        "nav": {day: nav[day] for day in days},
    }


def ibkr_epochs(facts: dict[str, Any], tag: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sleeve in tag["sleeves"]:
        sid = sleeve["sleeve_id"]
        if sid == "other":
            continue
        account = tag["parent_account_id"] + "-" + sid
        rows = facts["sleeves"].get(sid, [])
        if not rows:
            continue
        nav = {row["day"]: D(row["nav_end_after_alloc"]) for row in rows}
        exits = [row["day"] for row in rows if row["event"] == "EXIT"]
        end = exits[-1] if exits else None
        legs: list[dict[str, Any]] = []
        for pair in facts["pairs"]:
            for side, kind in (
                ("in_account", "INTERNAL_IN"),
                ("out_account", "INTERNAL_OUT"),
            ):
                if pair[side] == account:
                    legs.append(
                        _leg(
                            pair["day"],
                            pair["day"],
                            kind,
                            D(pair["amount"]),
                            pair["kind"],
                        )
                    )
        cost = sum((D(row["alloc_total"]) for row in rows), Z)
        if end is not None and cost:
            legs.append(
                _leg(
                    end,
                    end,
                    "INTERNAL_OUT" if cost > 0 else "INTERNAL_IN",
                    abs(cost),
                    "SETTLEMENT",
                )
            )
        result.append(epoch(account, sleeve["strategy_id"], nav, end, legs))
    return result


def quantbase_epochs(
    facts: dict[str, Any],
    tag: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sleeve in tag["sleeves"]:
        sid = sleeve["sleeve_id"]
        if sid not in ("crisis", "singleindex"):
            continue
        fund = facts["funds"][sid]
        account = tag["parent_account_id"] + "-" + sid
        legs: list[dict[str, Any]] = []
        # Recognition belongs to each fund's own fill, including opening exceptions;
        # a pair's single reporting date cannot override the other side's date.
        recognition_days = {
            (f["fill_day"], "INTERNAL_IN" if f["kind"] == "BUY" else "INTERNAL_OUT"): f[
                "recognition_day"
            ]
            for f in fund["flows"]
        }
        for pair in facts["pairs"]:
            for side, kind in (
                ("in_account", "INTERNAL_IN"),
                ("out_account", "INTERNAL_OUT"),
            ):
                if pair[side] == account:
                    event = pair["event_day"]
                    legs.append(
                        _leg(
                            recognition_days[(event, kind)],
                            event,
                            kind,
                            D(pair["amount"]),
                            pair["kind"],
                        )
                    )
        nav = {row["day"]: D(row["nav_end"]) for row in fund["rows"]}
        result.append(epoch(account, sleeve["strategy_id"], nav, max(nav), legs))
    return result


def universe(
    crosswalk: dict[str, Any],
    parents: list[dict[str, Any]],
    epochs: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = {row["strategy_id"] for row in crosswalk["rows"]}
    strategies = {item["strategy_id"] for item in epochs}
    residual = sorted(
        {
            parent["parent_account_id"]
            for parent in parents
            if any(
                row["account_id"] == parent["parent_account_id"]
                and row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
                and row["strategy_id"] in {s["strategy_id"] for s in parent["sleeves"]}
                for row in crosswalk["rows"]
            )
        }
    )
    return {
        "universe.derived_epoch_ids": sorted(
            f"{e['account_id']}|{e['strategy_id']}|{e['effective_start']}|"
            f"{e['effective_end'] or 'open'}"
            for e in epochs
        ),
        "universe.derived_epoch_count": str(len(epochs)),
        "universe.retained_strategy_ids": sorted(strategies - baseline),
        "universe.retained_strategy_count": str(len(strategies - baseline)),
        "universe.registry_strategy_count": str(len(strategies | baseline)),
        "universe.residual_parent_ids": residual,
    }


def emit_epoch(
    item: dict[str, Any],
    output: Output,
    cutoff: dt.date,
    inputs: tuple[str, ...],
    members: dict[str, Member] | None = None,
) -> None:
    from .derive import (
        AccountSource,
        NotDerivableError,
        Segment,
        WINDOW,
        _emit_returns,
        _money,
        _ratio,
        build_member,
    )
    from .numerics import (
        CalendarMisalignmentError,
        Member,
        UndefinedReturnError,
        composite_subperiods,
        time_weighted_return,
    )

    account = item["account_id"]
    nav = {dt.date.fromisoformat(day): D(value) for day, value in item["nav"].items()}
    start = dt.date.fromisoformat(item["effective_start"])
    end = (
        dt.date.fromisoformat(item["effective_end"]) if item["effective_end"] else None
    )
    for name in ("start", "end"):
        output.metric(
            f"date.epoch.{name}",
            account,
            "",
            cutoff,
            item[f"effective_{name}"] or "open",
            "date",
            inputs,
        )
    for name in ("opening", "net_capital", "closing"):
        output.metric(
            f"capital.epoch.{name}",
            account,
            "",
            cutoff,
            _money(D(item[name])),
            "USD",
            inputs,
        )
        output.metrics[-1]["tolerance"] = "0.01"
    flows = AccountFlows(
        flows=[
            Flow(
                dt.date.fromisoformat(leg["recognition_day"]),
                D(leg["amount"]) * (1 if leg["class"] == "INTERNAL_IN" else -1),
                dt.date.fromisoformat(leg["event_day"]),
                leg["class"],
                f"sleeve:{account}:{index}",
                dated_by="nav_recognition_date",
                disposition=leg["class"],
            )
            for index, leg in enumerate(item["legs"])
        ]
    )
    source = AccountSource(account, "DERIVED_SLEEVE", nav, flows, inputs, inputs)
    try:
        # The harness explicitly binds the first row as the complete opening.
        # Do not let the engine silently advance past a nonpositive first row.
        if nav[start] <= 0:
            member = Member(
                key=account,
                values=nav,
                open_date=start,
                close_date=max(nav),
                flows=tuple(
                    (flow.effective_date, flow.amount, flow.timing)
                    for flow in flows.flows
                    if start < flow.effective_date <= max(nav)
                ),
            )
            notes = [
                f"{account} first-row opening {start} value {_money(nav[start])}; "
                "nonpositive opening retained for the return engine"
            ]
        else:
            member, notes = build_member(source, Segment(account, start, end), cutoff)
    except NotDerivableError as exc:
        for metric in sorted(ACCOUNT_METRICS):
            output.missing(metric, account, WINDOW, str(exc))
        return
    if members is not None:
        members[account] = member
    output.metric(
        "flows.account.net",
        account,
        WINDOW,
        member.close_date,
        _money(sum((amount for _, amount, _ in member.flows), Z)),
        "USD",
        inputs,
        notes,
    )
    output.metric(
        "flows.account.count",
        account,
        WINDOW,
        member.close_date,
        str(len(member.flows)),
        "count",
        inputs,
        notes,
    )
    try:
        _emit_returns(output, "account", account, [member], inputs, notes)
    except DecimalException as exc:
        # An extremely negative short-window XIRR can hit the engine's Decimal
        # boundary near -1. Preserve that exception and independently retain
        # the TWR engine's result/refusal; do not change its solver or precision.
        output.missing("mwr.account", account, WINDOW, f"{type(exc).__name__}: {exc}")
        try:
            periods = composite_subperiods([member])
            twr = time_weighted_return(periods)
        except (
            UndefinedReturnError,
            CalendarMisalignmentError,
            DecimalException,
        ) as twr_exc:
            output.missing(
                "twr.account",
                account,
                WINDOW,
                f"{type(twr_exc).__name__}: {twr_exc}",
            )
        else:
            output.metric(
                "twr.account",
                account,
                WINDOW,
                member.close_date,
                _ratio(twr),
                "ratio",
                inputs,
                notes + [f"{len(periods)} sub-periods"],
            )


def append_sleeves(
    document: dict[str, Any],
    path: Path,
    qe_root: Path,
    repo_root: Path,
    cutoff: dt.date,
    *,
    parents_out: dict[str, str] | None = None,
    members_out: dict[str, Member] | None = None,
) -> dict[str, Any]:
    from .derive import Output
    from .numerics import PRECISION

    records, raw = load_sleeve_inputs(path, qe_root, repo_root, cutoff)
    tag_map = _json(raw["tag_map"], "tag_map")
    parents = tag_map.get("parents", [])
    selected: dict[str, dict[str, Any]] = {}
    for provider in ("ibkr", "quantbase"):
        found = [p for p in parents if p.get("provider_id") == provider]
        if len(found) != 1:
            raise InputError(f"tag map needs exactly one {provider} parent")
        selected[provider] = found[0]
    ib_dec = _json(raw["ibkr_declared_inputs"], "ibkr_declared_inputs")
    qb_dec = _json(raw["quantbase_declared_inputs"], "quantbase_declared_inputs")
    if ib_dec["window"]["last_day"] != cutoff.isoformat():
        raise InputError("IBKR declared window cutoff mismatch")
    if qb_dec["data_cutoff"] != cutoff.isoformat():
        raise InputError("Quantbase declared cutoff mismatch")
    for provider, dec in (("ibkr", ib_dec), ("quantbase", qb_dec)):
        if dec["parent_account_id"] != selected[provider]["parent_account_id"]:
            raise InputError(f"{provider}: declared parent differs from tag map")
    catalog = _json(raw["ibkr_catalog"], "ibkr_catalog")
    sliced = slice_ibkr(
        catalog,
        selected["ibkr"]["parent_account_id"],
        ib_dec["window"]["first_day"],
        ib_dec["window"]["last_day"],
    )
    csv_names = {
        "account_activity.csv": "quantbase_account_activity",
        "accounts.csv": "quantbase_accounts",
        "dashboard_Crisis_Factor_Investing_pricehistory.csv": "quantbase_dashboard_crisis",
        "dashboard_Single_Index_Leveraged_In_and_Out_pricehistory.csv": "quantbase_dashboard_singleindex",
        "dashboard_Volatis_Strategy_pricehistory.csv": "quantbase_dashboard_volatis",
        "dashboard_Total_pricehistory.csv": "quantbase_dashboard_total",
    }
    csv_data = {
        name: list(csv.DictReader(io.StringIO(raw[role].decode("utf-8-sig"))))
        for name, role in csv_names.items()
    }
    with localcontext() as context:
        context.prec = 28
        ib = ibkr_facts(sliced, ib_dec, selected["ibkr"])
        qb = quantbase_facts(qb_dec, selected["quantbase"], csv_data)
        ib_epochs = ibkr_epochs(ib, selected["ibkr"])
        qb_epochs = quantbase_epochs(qb, selected["quantbase"])
        epochs = sorted(ib_epochs + qb_epochs, key=lambda e: e["account_id"])
    if parents_out is not None:
        for provider, derived in (("ibkr", ib_epochs), ("quantbase", qb_epochs)):
            for item in derived:
                parents_out[item["account_id"]] = selected[provider][
                    "parent_account_id"
                ]
    census = universe(_json(raw["crosswalk"], "crosswalk"), parents, epochs)
    inputs = tuple(record["path"] for record in records.values())
    output = Output()
    with localcontext() as context:
        context.prec = PRECISION
        for item in epochs:
            emit_epoch(item, output, cutoff, inputs, members_out)
    for metric, value in census.items():
        output.metric(
            metric,
            "portfolio",
            "",
            cutoff,
            value,
            "list" if isinstance(value, list) else "count",
            inputs,
        )
    residual = set(census["universe.residual_parent_ids"])
    for key, rows in (
        ("metrics", output.metrics),
        ("not_derivable", output.not_derivable),
    ):
        document[key] = [
            r
            for r in document.get(key, [])
            if not (r["entity"] in residual and r["metric_id"] in ACCOUNT_METRICS)
        ] + rows
    assumptions = (
        ib["assumptions"]
        + qb["assumptions"]
        + [
            "Harness section 3 explicitly mirrors existing return/flow conventions: ITD window and last NAV day as_of; section 4 cutoff/empty window applies to new epoch capital/date and universe metrics.",
            "Provider facts and epoch capital arithmetic use Decimal precision 28; return-engine arithmetic uses its existing precision 60.",
            "Epoch legs include opening-embedded legs for audit; only recognition days strictly after the first NAV day through the last NAV day enter return-engine flows.",
            "Each Quantbase pair side uses its own fund recognition date; the pair-level reporting date does not override an opening purchase exception.",
            "Metric inputs_used follows existing path-list style; sha256 pins are recorded in sleeve_derivation.inputs and document inputs.",
            "No Git inspection for sleeve runs: absent explicit --code-commit, the header uses UNKNOWN.",
        ]
    )
    document["sleeve_derivation"] = encode(
        {
            "schema": "qe-cp-sleeve-derivation/1",
            "inputs": records,
            "epochs": epochs,
            "closure": {
                "ibkr": ib["closure_summary"],
                "ibkr_daily": ib["closure"],
                "quantbase": qb["closure"],
            },
            "disclosures": {
                **qb.get("disclosures", {}),
                "ibkr": ib["disclosures"],
                "quantbase_dividends": qb["dividends"],
                "quantbase_volatis_fills": qb["volatis_fills"],
                "quantbase_fund_checks": {
                    sid: {k: v for k, v in fund.items() if k not in ("rows", "flows")}
                    for sid, fund in qb["funds"].items()
                },
            },
            "assumptions": assumptions,
            "refusals": ["IBKR: " + r for r in ib["refusals"]]
            + ["Quantbase: " + r for r in qb["refusals"]],
        }
    )
    existing = {record["path"] for record in document.get("inputs", [])}
    document.setdefault("inputs", []).extend(
        record for record in records.values() if record["path"] not in existing
    )
    return document

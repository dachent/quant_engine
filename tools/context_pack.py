"""The analysis context pack: what an LLM analyst reads instead of the 279 MB panel payload.

``build_pack`` is a pure function of the finished panel payload (with its ``decision`` block),
the owner's decision policy, the strategy concept contract and the recommendation log.  The
pack is self-describing: every number is a fact record with an as-of date and an evidence
grade, every status token has a plain-words glossary entry, and what the analyst must not
rely on is listed.  ``validate_pack`` fails a pack that has an undefined token, a banned
measure outside the exclusion list, a citation to a missing fact, or too many tokens.

    python tools/context_pack.py --payload dashboard_data.json --out DIR

The command is read-only on the payload and writes ``context-pack.md`` and
``context-pack.json`` into DIR.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
CORE_TOKEN_TARGET = 25_000
HARD_TOKEN_CAP = 40_000
CHARS_PER_UNIT = 4  # an estimate: ceil(utf-8 bytes / 4)

GRADES = {
    "A": "Reconciled to statements with closure.",
    "B": "Derived from governed data by a stated method.",
    "C": "Estimated or audit-derived; a human-checked analysis, not a reconciled figure.",
    "D": "Model, vendor or backtest figure; context only, never a basis for sizing.",
    "P": "Policy constant or rule output: the owner's rule applied to the data, not a measurement.",
}

CAVEATS = {
    "HWM_FILL": "The drawdown index has 753 daily points through the cutoff; two missing daily points are filled by span returns. The owner accepted the index as the official measure on 2026-11-17.",
    "UNDER_ONE_YEAR": "Live history is under one year for six of seven strategies: no ranking of strategies and no annualised figure is supported.",
    "WEEKLY_BASIS": "Computed on weekly account returns because the payload carries no daily live path for this strategy; drawdowns are understated against daily.",
    "AUDIT_DERIVED": "From the pinned model-versus-account audit, not from a reconciled ledger term.",
    "BACKTEST_PRIOR": "Prior worst drawdown is a backtest or vendor figure (possibly across a calendar gap); counted as loss evidence by owner-adopted rule.",
    "PLATFORM_ONLY": "Live history exists only on the provider platform, not in an owner-held account; the stress rule adds 5 points.",
    "POLICY_PLACEHOLDER": "The policy number behind this figure is a placeholder not yet set by the owner.",
    "NO_MODEL_SERIES": "The payload carries no model return series for this window, so no gap is computed.",
    "STRESS_SCENARIO": "Every strategy at its stressed loss on the same day: a scenario, not a forecast and not an expected loss.",
    "COST_RATE": "Pre-tax external cost rate on the strategy's account value; partial coverage of cost facts.",
}

GLOSSARY: dict[str, str] = {
    "NORMAL": "Drawdown under 10%: no restriction.",
    "NO_NEW_RISK": "Drawdown 10% to the limit: no new risk, no leverage increase, no new strategy; rebalance moves wait, reductions to cash are allowed.",
    "LIMIT_BREACH": "Drawdown over the limit: the policy's pre-set reduction applies (see the policy section; it may be unset).",
    "HOLD": "Strategy status: live drawdown below the prior worst; no action.",
    "WATCH": "Strategy status: live drawdown at or above the prior worst; no additions.",
    "REDUCE": "Strategy status: ratio over the reduce threshold; reduce by the policy fraction.",
    "EXIT": "Strategy status: ratio over the exit threshold; exit and review.",
    "NO_LIVE_DRAWDOWN": "No live drawdown path in the payload; the strategy is not rated.",
    "IN_BAND": "Weight within the rebalance band around its target.",
    "ABOVE_BAND": "Weight above target plus the band; a sell is implied if the zone allows.",
    "BELOW_BAND": "Weight below target minus the band; a buy is implied if the zone and status allow.",
    "CURRENT": "Source data date is within its allowed age.",
    "STALE": "Source data date is older than its allowed age: treat dependent numbers as out of date.",
    "INACTIVE": "Closed or retired source; no current data is expected.",
    "NO_ALLOWED_AGE": "No allowed age is set for this source in the policy.",
    "ABOVE_CAP": "Cluster weight above the policy cap.",
    "WITHIN_CAP": "Cluster weight within the policy cap.",
    "ABOVE_LIMIT": "Cluster stress loss above the owner's 15% limit on its own.",
    "WITHIN_LIMIT": "Cluster stress loss within the 15% limit.",
    "SELL": "Rebalance sale toward target.",
    "BUY": "Rebalance purchase toward target.",
    "SELL_TO_CASH": "Reduction under the status rule or the breach action; proceeds stay in cash.",
    "PARTIAL": "The accepted records for this kind of fact are incomplete: what is shown is correct as far as it goes and is never zero-filled. It cannot turn complete by itself; the missing records or a ruling would have to arrive. It does not make the shown numbers wrong.",
    "COMPLETE_WITHIN_DECLARED_BOUNDARY": "Complete inside a stated boundary; says nothing outside it.",
    "CLOSED_BY_CAPTURE_B_STATE_EVIDENCED_BY_ABSENCE_OF_TRADES": "Pull status: the pull was closed by the release B capture; the vendor's live-signal file was not received, and the state is evidenced by the absence of trades, not by that file.",
    "NO_ACCEPTED_FACT_PATH": "No admitted source path exists for this fact on this account. Structural: more data pulls of the same kind will not change it; an admitted source or a ruling is needed.",
    "OBSERVED_NOT_COMPLETENESS_PROOF": "Records were observed; no provider export can prove it listed everything. Structural: stays this status whatever is pulled.",
    "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF": "The provider reported no events; that is not proof that none occurred.",
    "OBSERVED_WITH_UNAVAILABLE_AMOUNTS_NOT_COMPLETENESS_PROOF": "Records exist but some amounts are unavailable.",
    "NO_REPORTED_FACT_NOT_COMPLETENESS_PROOF": "No fact of this type was reported; not proof of absence.",
    "NOT_TESTABLE": "The question cannot be tested with the evidence held; it is neither passed nor failed.",
    "NOT_READY_AT_CUTOFF": "The figure could not be produced at the cutoff; it is absent, not zero.",
    "DO_NOT_EXPAND_SYSTEMATIC_RISK": "The engine's own advisory output: its stress figure is above the owner's limit, so it advises no expansion. A rule output, not a forecast.",
    "BREACHES_SYSTEMATIC_LOSS_LIMIT": "The engine's cluster budget status: the policy-constant stress figure is above the 15% limit.",
    "NO_PRIOR_PACK": "There is no earlier pack to compare with.",
    "NO_PRIOR_DECISION": "There is no earlier decision block to compare with.",
    "EVIDENCED": "Evidence tag: backed by admitted records.",
    "ESTIMATED": "Evidence tag: an estimate.",
    "AUDIT_DERIVED": "Evidence tag: from the pinned audit analysis.",
    "UNSET": "A policy value the owner has not set.",
    "BLOCKING_FOR_FULL_ATTRIBUTION": "Disclosed gap that stops a full attribution of history to strategies; the shown attribution covers accepted rows only.",
    "DISCLOSED_GAP": "A known gap the engine states openly; nothing is inferred to fill it.",
    "DISCLOSED_HISTORICAL_GAP": "A known gap in past history, stated openly; nothing is inferred to fill it.",
    "NOT_A_DOWNLOAD_OWNER_RULING": "The gap closes by an owner ruling, not by downloading data.",
    "REQUESTED_NOT_DOWNLOADED": "The data has been requested and is not yet downloaded.",
    "REJECTED_NO_ACCEPTED_PROVIDER_ROUTE": "No accepted provider route exists for this pull; it needs a ruling or a new route.",
    "CURRENT_DRAWDOWN": "Policy setting: the status ratio uses the current live drawdown.",
    "WORST_LIVE_DRAWDOWN": "Policy setting: the status ratio uses the worst live drawdown.",
    "TACTICAL_VOLATILITY": "Economic family label of a strategy: tactical volatility exposure.",
    "VERIFIED": "Recommendation log: hash chain verified.",
    "EMPTY": "Recommendation log: no rows yet.",
    "LOG_BROKEN": "Recommendation log: the chain or a row failed verification; do not rely on it.",
    "OPEN": "Recommendation status: not closed.",
    "CLOSED": "Recommendation status: closed.",
    "SUPERSEDED": "Recommendation status: replaced by a later one.",
    "WITHDRAWN": "Recommendation status: withdrawn.",
}

_STATUS_WORD = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SINGLE = re.compile(
    r"\b(NORMAL|HOLD|WATCH|REDUCE|EXIT|PARTIAL|CURRENT|STALE|INACTIVE|UNSET|VERIFIED|EMPTY|OPEN|CLOSED)\b"
)
_BANNED = re.compile(
    r"\b(alphas?|betas?|r-squared|t-stat(?:istic)?s?|conservative)\b", re.IGNORECASE
)
_EXEMPT_FROM_BANNED = {"do_not_rely", "glossary", "standing_rules", "strategy_cards"}
_MOJIBAKE = re.compile("[âÃ]")


class PackError(ValueError):
    """The pack cannot be built or failed validation."""


KNOWN_ISSUES_PATH = ROOT / "contracts" / "portfolio-known-issues-v1.json"
_ISSUE_ID = re.compile(r"KI-\d{2,}")


STRESS_BOUND_FALLBACK = "The stress loss is a bound under the stated rule, not a forecast and not a statistical confidence bound."


def stress_bound_line(data: Mapping[str, Any]) -> str:
    """The stress-bound rule text, read from the payload's adverse_loss_basis.rule_statement when it carries one."""

    stack: list[tuple[Any, int]] = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, Mapping):
            basis = node.get("adverse_loss_basis")
            if isinstance(basis, Mapping) and isinstance(
                basis.get("rule_statement"), str
            ):
                return f"{STRESS_BOUND_FALLBACK} Rule as published: {_clean(basis['rule_statement'])}."
            if depth < 6:
                stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list) and depth < 6:
            stack.extend((v, depth + 1) for v in node[:50])
    return STRESS_BOUND_FALLBACK


def known_issue_lines(contract: Mapping[str, Any]) -> list[str]:
    """One DO NOT RELY line per OPEN known issue of the governed list; a malformed list refuses the pack."""

    issues = contract.get("issues")
    if contract.get("schema_version") != 1 or not isinstance(issues, list):
        raise PackError(
            "known-issues contract: schema_version 1 and an issues list are required"
        )
    seen: set[str] = set()
    lines: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise PackError("known-issues contract: every issue must be an object")
        issue_id = str(issue.get("id", ""))
        if not _ISSUE_ID.fullmatch(issue_id) or issue_id in seen:
            raise PackError(f"known-issues contract: bad or repeated id {issue_id!r}")
        seen.add(issue_id)
        status = issue.get("status")
        if status not in ("OPEN", "FIXED"):
            raise PackError(
                f"known-issues contract: {issue_id} status must be OPEN or FIXED"
            )
        text = issue.get("do_not_rely")
        if not isinstance(text, str) or not text.strip():
            raise PackError(
                f"known-issues contract: {issue_id} has no do_not_rely text"
            )
        if status == "OPEN":
            fix = issue.get("fix") if isinstance(issue.get("fix"), Mapping) else {}
            ticket = f", fix {fix['ticket']}" if fix.get("ticket") else ""
            lines.append(
                f"{issue_id} ({issue.get('verification', 'CONTROL')}{ticket}): {_clean(text)}"
            )
    return lines


def _clean(text: str) -> str:
    """Undo a double-encoded dash or quote (a UTF-8 file read through cp1252)."""

    if _MOJIBAKE.search(text):
        try:
            return text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    return text


def _pct(value: object, digits: int = 1) -> str:
    return f"{float(str(value)) * 100:.{digits}f}"


def _fact(
    facts: list[dict[str, Any]],
    fact_id: str,
    label: str,
    value: str | None,
    unit: str,
    as_of: str,
    grade: str,
    window: str | None = None,
    caveats: Sequence[str] = (),
) -> None:
    if value is None:
        return
    facts.append(
        {
            "id": fact_id,
            "label": label,
            "value": value,
            "unit": unit,
            "as_of": as_of,
            "window": window,
            "grade": grade,
            "caveats": list(caveats),
        }
    )


def build_facts(
    data: Mapping[str, Any], decision: Mapping[str, Any]
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    as_of = decision["as_of"]
    p = decision["portfolio"]
    _fact(
        facts,
        "portfolio.capital",
        "Systematic capital",
        decision["capital_usd"],
        "USD",
        as_of,
        "A",
    )
    _fact(
        facts,
        "portfolio.drawdown",
        "Drawdown from the high-water mark",
        _pct(p["drawdown"], 2),
        "%",
        as_of,
        "B",
        "since inception of the index",
        ["HWM_FILL"],
    )
    _fact(
        facts,
        "portfolio.worst_drawdown",
        "Worst drawdown on record",
        _pct(p["worst_drawdown"], 2),
        "%",
        as_of,
        "B",
        "whole index",
        ["HWM_FILL"],
    )
    _fact(
        facts,
        "portfolio.limit",
        "Owner drawdown limit",
        _pct(p["limit"], 1),
        "%",
        as_of,
        "P",
    )
    _fact(
        facts,
        "portfolio.headroom",
        "Headroom to the limit",
        p["headroom_points"],
        "points",
        as_of,
        "B",
        None,
        ["HWM_FILL"],
    )
    _fact(
        facts,
        "portfolio.stress_loss",
        "Stress loss, policy rule re-applied at the cutoff",
        _pct(p["stress_loss"], 2),
        "% of capital",
        as_of,
        "P",
        None,
        ["STRESS_SCENARIO", "BACKTEST_PRIOR"],
    )
    _fact(
        facts,
        "portfolio.stress_loss_usd",
        "Stress loss in dollars",
        p["stress_loss_usd"],
        "USD",
        as_of,
        "P",
        None,
        ["STRESS_SCENARIO"],
    )
    if p.get("published_policy_constant_stress") is not None:
        _fact(
            facts,
            "portfolio.stress_published",
            "Stress loss from the August policy constants (published figure)",
            _pct(p["published_policy_constant_stress"], 2),
            "% of capital",
            as_of,
            "P",
            None,
            ["STRESS_SCENARIO"],
        )
    for c in decision["clusters"]:
        cid = c["cluster_id"].lower()
        _fact(
            facts,
            f"cluster.{cid}.weight",
            f"{c['label']}: weight",
            _pct(c["weight"], 1),
            "% of capital",
            as_of,
            "A",
        )
        _fact(
            facts,
            f"cluster.{cid}.stress_loss",
            f"{c['label']}: stress loss",
            _pct(c["stress_loss"], 2),
            "% of capital",
            as_of,
            "P",
            None,
            ["STRESS_SCENARIO"],
        )
    for s in decision["sources"]:
        _fact(
            facts,
            f"source.{s['source']}.data_date",
            f"{s['source']}: data date",
            s["data_date"],
            "date",
            s["data_date"],
            "A",
            None,
            [],
        )
    for r in decision["strategies"]:
        key = r["strategy_id"]
        weekly = r["drawdown_basis"].startswith("weekly")
        live_cav = ["UNDER_ONE_YEAR"] + (["WEEKLY_BASIS"] if weekly else [])
        live_grade = "C" if weekly else "B"
        _fact(
            facts,
            f"strategy.{key}.value",
            f"{r['name']}: account value",
            r["value_usd"],
            "USD",
            as_of,
            "A",
        )
        _fact(
            facts,
            f"strategy.{key}.weight",
            f"{r['name']}: weight in the systematic perimeter",
            _pct(r["weight"], 2),
            "%",
            as_of,
            "A",
        )
        _fact(
            facts,
            f"strategy.{key}.target",
            f"{r['name']}: policy target weight",
            _pct(r["target"], 2),
            "%",
            as_of,
            "P",
            None,
            ["POLICY_PLACEHOLDER"],
        )
        _fact(
            facts,
            f"strategy.{key}.stress_fraction",
            f"{r['name']}: stressed loss fraction",
            _pct(r["stress_loss_fraction"], 2),
            "% of its value",
            as_of,
            "P",
            None,
            ["STRESS_SCENARIO", "BACKTEST_PRIOR"]
            + (
                ["PLATFORM_ONLY"]
                if key in ("volatis-strategy", "robuxio-crypto")
                else []
            ),
        )
        _fact(
            facts,
            f"strategy.{key}.stress_share",
            f"{r['name']}: share of total stress loss",
            _pct(r["stress_share"], 1),
            "%",
            as_of,
            "P",
            None,
            ["STRESS_SCENARIO"],
        )
        _fact(
            facts,
            f"strategy.{key}.live_drawdown_now",
            f"{r['name']}: current live drawdown",
            None if r["live_drawdown_now"] is None else _pct(r["live_drawdown_now"], 2),
            "%",
            as_of,
            live_grade,
            "live history",
            live_cav,
        )
        _fact(
            facts,
            f"strategy.{key}.live_drawdown_worst",
            f"{r['name']}: worst live drawdown",
            None
            if r["live_drawdown_worst"] is None
            else _pct(r["live_drawdown_worst"], 2),
            "%",
            as_of,
            live_grade,
            "live history",
            live_cav,
        )
        _fact(
            facts,
            f"strategy.{key}.prior_worst",
            f"{r['name']}: prior worst drawdown",
            _pct(r["prior_worst_drawdown"], 2),
            "%",
            as_of,
            "D",
            "backtest or vendor history",
            ["BACKTEST_PRIOR"],
        )
        _fact(
            facts,
            f"strategy.{key}.drawdown_ratio",
            f"{r['name']}: live drawdown over prior worst",
            r["drawdown_ratio"],
            "ratio",
            as_of,
            live_grade,
            None,
            live_cav + ["BACKTEST_PRIOR"],
        )
        cost = r["financing"]["annual_cost_rate"]
        _fact(
            facts,
            f"strategy.{key}.cost_rate",
            f"{r['name']}: annual external cost rate",
            None if cost is None else _pct(cost, 2),
            "% a year",
            as_of,
            "C",
            None,
            ["COST_RATE"],
        )
        tr = r["tracking"]
        for label, window in (("last_week", "last week"), ("four_weeks", "4 weeks")):
            item = tr[label]
            if item is not None:
                _fact(
                    facts,
                    f"strategy.{key}.account_return_{label}",
                    f"{r['name']}: account return, {window}",
                    _pct(item["account_return"], 2),
                    "%",
                    as_of,
                    "B",
                    f"from week {item['from_week']}",
                    ["UNDER_ONE_YEAR"],
                )
                if item["gap_points"] is not None:
                    _fact(
                        facts,
                        f"strategy.{key}.gap_{label}",
                        f"{r['name']}: account minus model, {window}",
                        item["gap_points"],
                        "points",
                        as_of,
                        "D",
                        f"from week {item['from_week']}",
                        ["UNDER_ONE_YEAR"],
                    )
        since = tr["since_start"]
        if since is not None:
            window = f"{since['start']} to {since['end']}"
            cav = ["AUDIT_DERIVED"] + (
                [] if since["ends_at_cutoff"] else ["UNDER_ONE_YEAR"]
            )
            _fact(
                facts,
                f"strategy.{key}.gap_since_start",
                f"{r['name']}: account minus model since start",
                str(since["gap_points"]),
                "points",
                since["end"],
                "C",
                window,
                cav,
            )
            if since["gap_usd"] is not None:
                _fact(
                    facts,
                    f"strategy.{key}.gap_since_start_usd",
                    f"{r['name']}: account minus model since start, in dollars",
                    str(since["gap_usd"]),
                    "USD",
                    since["end"],
                    "C",
                    window,
                    cav,
                )
    return facts


def _holes(data: Mapping[str, Any]) -> list[dict[str, str]]:
    holes: list[dict[str, str]] = []
    blocks = {
        "costs": "cost rates and the financing line (grade C figures)",
        "flows": "owner-flow completeness behind account-return figures",
        "fx": "currency translation for the European sleeve",
        "historical_strategy_attribution": "attribution of pre-assignment history",
        "positions": "holdings against signals; position coverage",
        "transactions": "execution and implementation-gap detail",
    }
    for kind, status in sorted((data.get("fact_status") or {}).items()):
        if status != "COMPLETE_WITHIN_DECLARED_BOUNDARY":
            holes.append(
                {
                    "what": f"Fact kind '{kind}' is {status}",
                    "blocks": blocks.get(kind, "figures that depend on it"),
                    "closes_by": "the missing records, or an owner ruling on the boundary",
                }
            )
    matrix = data.get("fact_coverage_matrix") or []
    nopath = sorted(
        {
            f"{row.get('fact_type')}"
            for row in matrix
            if row.get("status") == "NO_ACCEPTED_FACT_PATH"
        }
    )
    count = sum(1 for row in matrix if row.get("status") == "NO_ACCEPTED_FACT_PATH")
    if count:
        holes.append(
            {
                "what": f"{count} account-fact rows have NO_ACCEPTED_FACT_PATH ({', '.join(nopath)})",
                "blocks": "completeness of those facts for those accounts",
                "closes_by": "an admitted source path or an owner ruling per account",
            }
        )
    for item in data.get("blocking_items") or []:
        holes.append(
            {
                "what": f"{item.get('item')}: {item.get('value')} ({item.get('known_gap')})",
                "blocks": "figures listed under that item",
                "closes_by": str(
                    item.get("resolution_authority", "see the panel Data health tab")
                ),
            }
        )
    for pull in (data.get("model_vs_actual") or {}).get("pulls") or []:
        holes.append(
            {
                "what": f"Pull {pull.get('pull_id')}: {_clean(str(pull.get('title')))[:170]}",
                "blocks": f"account-versus-model terms ({', '.join(pull.get('replaces') or [])})",
                "closes_by": f"{pull.get('access')}; status {pull.get('status')}",
            }
        )
    cs = data.get("capital_status") or {}
    for key, status in sorted(cs.items()):
        if status != "AVAILABLE":
            holes.append(
                {
                    "what": f"Capital line '{key}' is {status}",
                    "blocks": "total liquid and reserve figures",
                    "closes_by": "reserve and liquid-capital carriers",
                }
            )
    for number, hole in enumerate(holes, start=1):
        hole["id"] = f"H-{number:02d}"
    return holes


def _card(source: Mapping[str, Any]) -> str:
    falsification = "; ".join(_clean(str(x)) for x in source.get("falsification") or [])
    primary = (source.get("sources") or {}).get("primary")
    return "\n".join(
        [
            f"Mechanism: {_clean(str(source.get('mechanism')))}",
            f"Persistence: {_clean(str(source.get('persistence')))}",
            f"Alternative explanation: {_clean(str(source.get('alternative_explanation')))}",
            f"What would prove it wrong: {falsification}",
            f"Confidence label: {source.get('confidence')}. Identified source: {'none' if source.get('no_identified_source') else primary}",
        ]
    )


DEFINITIONS = [
    "Systematic weight: a strategy's account value divided by the sum of the account values of the seven strategies at the cutoff (current-cutoff basis). The engine also holds August 2026 policy allocation weights; this pack never uses them.",
    "Account return: time-weighted return of the owner's account, net of the account's own costs; model return is the provider's published or derived model series and is gross of account costs.",
    "Drawdown: fall from the previous peak of the compounded path, as a positive percentage. Portfolio drawdown uses the high-water-mark index of the systematic perimeter; strategy drawdowns use that strategy's account series.",
    "Prior worst drawdown: the deepest fall in backtest or vendor history before the live record; context for the status rule, grade D.",
    "Stress loss: each strategy loses its stressed fraction on the same day; the portfolio figure is the weighted sum. The fraction is max(1.25 x prior worst, 1.5 x worst live) plus 5 points where live history is platform-only, capped at 100%.",
    "Status ratio: current live drawdown divided by prior worst drawdown; thresholds are in the policy section.",
    "Cutoff: the last day every source covers; evaluated-on is the date the ages are measured to.",
]

STANDING_RULES_FALLBACK: list[dict[str, str]] = []

DO_NOT_RELY = [
    "Any alpha, beta, R-squared or t-statistic: none is in this pack; rejected fits, no standard errors, under a year of live data.",
    "Cumulative or annualised returns that join backtest history to live history, and chart index levels.",
    "Annualised figures on windows under 365 days.",
    "The engine's 'central case' loss: it is not in the core pack; if you meet it elsewhere it needs its method and its sample length.",
    "Any word that asserts quality (for example 'conservative' or 'complete') that is not backed by a recompute at the cutoff in this pack.",
    "Absence of a fact as zero: an absent figure is absent.",
    "Ranking strategies by return: every strategy has under one year of live history except one.",
    "Grade D figures for sizing.",
]

QUESTIONS = [
    "1. Limits: where is the portfolio against its drawdown limit and each cluster against its stress limit?",
    "2. Tracking: how far is each account behind or ahead of its model, last week, 4 weeks and since start?",
    "3. Holdings against signals: is any strategy outside its band, and does the zone allow acting?",
    "4. Financing: any financing or cost issue?",
    "5. Data currency: is any source stale?",
    "6. Strategy status: does any strategy trigger the status rule?",
    "7. What changed since the last pack?",
    "8. What can you not answer, naming the data hole id?",
]


def _table(facts: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "id | value | unit | as of | grade | window | caveats",
        "--- | --- | --- | --- | --- | --- | ---",
    ]
    for f in facts:
        lines.append(
            " | ".join(
                [
                    f["id"],
                    f["value"],
                    f["unit"],
                    f["as_of"],
                    f["grade"],
                    f["window"] or "-",
                    ",".join(f["caveats"]) or "-",
                ]
            )
        )
    return "\n".join(lines)


def build_pack(
    data: Mapping[str, Any],
    decision: Mapping[str, Any],
    policy: Mapping[str, Any],
    policy_sha256: str,
    strategy_sources: Sequence[Mapping[str, Any]],
    recommendations: Mapping[str, Any] | None = None,
    prior_pack: Mapping[str, Any] | None = None,
    known_issues: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    facts = build_facts(data, decision)
    p = decision["portfolio"]
    sections: list[dict[str, str]] = []

    def add(section_id: str, title: str, body: str) -> None:
        sections.append({"id": section_id, "title": title, "text": body})

    add(
        "header",
        "Header",
        "\n".join(
            [
                f"Cutoff: {decision['as_of']}. Evaluated on: {decision['evaluated_on']}. Pack schema {SCHEMA_VERSION}. Policy {policy['policy_id']} version {policy['policy_version']} (file digest {policy_sha256[:12]}).",
                "Tier: built from the release panel payload of one accepted generation; not a provisional refresh.",
                "Your role: analyst, not decision-maker. Answer the fixed questions below, cite fact ids for every number, end with a section 'Cannot answer' naming hole ids. The owner decides and executes. Agents never place trades.",
                "Fixed questions:\n" + "\n".join(QUESTIONS),
            ]
        ),
    )
    held = decision["actions"]["held_back"]
    execute = decision["actions"]["execute"]
    snap = [
        f"{decision['actions']['headline']}",
        f"Zone: {p['zone']} ({p['zone_rule']})",
        f"Drawdown {_pct(p['drawdown'], 2)}% against the {_pct(p['limit'], 0)}% limit; {p['headroom_points']} points of headroom [portfolio.drawdown, portfolio.headroom].",
        f"Stress loss {_pct(p['stress_loss'], 1)}% of capital, about ${int(p['stress_loss_usd']):,} [portfolio.stress_loss, portfolio.stress_loss_usd]. Rule: {p['stress_rule']}",
        "Actions the rules imply this week: "
        + (
            "; ".join(f"{a['text']} ({a['reason']})" for a in execute)
            if execute
            else "none."
        ),
    ]
    if held:
        snap.append(
            "Held back by the rules: "
            + "; ".join(f"{a['text']} ({a['held_because']})" for a in held)
        )
    add(
        "decision_snapshot",
        "Decision snapshot (the human Decision tab shows this)",
        "\n".join(snap),
    )
    add("definitions", "Definitions", "\n".join(f"- {d}" for d in DEFINITIONS))
    add(
        "grades",
        "Evidence grades and caveat ids",
        "\n".join(f"- {g}: {t}" for g, t in GRADES.items())
        + "\n"
        + "\n".join(f"- {c}: {t}" for c, t in CAVEATS.items()),
    )
    # the glossary is added last so it covers every token that appears anywhere
    state_lines = [
        "Engine advisory readiness: "
        + str((data.get("advisory") or {}).get("readiness"))
        + " (an engine rule output, see glossary)."
    ]
    for c in decision["clusters"]:
        cid = c["cluster_id"].lower()
        cap = (
            ""
            if c["weight_cap"] is None
            else " against a cap of " + _pct(c["weight_cap"], 0) + "%"
        )
        state_lines.append(
            f"- {c['label']} ({', '.join(c['members'])}): weight {_pct(c['weight'], 1)}%{cap} {c['weight_state']}; "
            f"stress loss {_pct(c['stress_loss'], 2)}% of capital, {_pct(c['stress_share'], 0)}% of the total, "
            f"{c['stress_vs_limit']} [cluster.{cid}.weight, cluster.{cid}.stress_loss]."
        )
    add("portfolio_state", "Portfolio state", "\n".join(state_lines))
    cards = []
    by_id = {s.get("strategy_id"): s for s in strategy_sources}
    for r in decision["strategies"]:
        key = r["strategy_id"]
        row = [
            f"## {r['name']} ({key})",
            f"Weight {_pct(r['weight'], 1)}% against a target of {_pct(r['target'], 1)}% (band +/-{r['band_points']} points, {r['band_state']}); stress share {_pct(r['stress_share'], 0)}%; status {r['status']} at ratio {r['drawdown_ratio']} [strategy.{key}.weight, strategy.{key}.target, strategy.{key}.drawdown_ratio].",
            f"Live drawdown now {r['live_drawdown_now']} and worst {r['live_drawdown_worst']} (fractions, {r['drawdown_basis']}); prior worst {r['prior_worst_drawdown']} from {r['prior_worst_source']}.",
        ]
        since = r["tracking"]["since_start"]
        if since:
            row.append(
                f"Account against model since {since['start']}: gap {since['gap_points']} points ({since['evidence']}) [strategy.{key}.gap_since_start]."
            )
        if key in by_id:
            row.append(_card(by_id[key]))
        else:
            row.append("No concept card in the contract for this strategy.")
        cards.append("\n".join(row))
    add("strategy_cards", "Strategies", "\n\n".join(cards))
    add("facts", "Facts", _table(facts))
    if prior_pack is not None:
        changes = diff_packs(prior_pack, {"facts": facts})
        body = (
            "\n".join(f"- {c}" for c in changes)
            if changes
            else "No fact changed since the prior pack."
        )
    else:
        body = "NO_PRIOR_PACK: no earlier pack to compare with."
    add(
        "changes",
        "What changed since the last pack",
        body
        + "\nDecision-level changes: "
        + (
            ", ".join(decision["changes"]["items"])
            if decision["changes"]["items"]
            else decision["changes"]["status"] + "."
        ),
    )
    pol = [
        f"Policy {policy['policy_id']} version {policy['policy_version']}; {policy['status'].replace('_', ' ').lower()}.",
        f"Limit {policy['limit']['drawdown_limit']} (normal below {policy['limit']['normal_below']}). Breach action: {policy['limit']['breach_action']['kind']}.",
        f"Rebalance band: larger of {policy['rebalance_band']['absolute_points']} (weight points) or {policy['rebalance_band']['relative_to_target']} of target.",
        "Status bands: "
        + ", ".join(
            f"{b['status']} from {b['from']}" for b in policy["status_rule"]["bands"]
        )
        + f" (ratio basis {policy['status_rule']['ratio_basis']}).",
        f"Targets: {policy['targets']['method']} Strategy cap {policy['targets']['max_strategy_weight']}, cluster cap {policy['targets']['max_cluster_weight']}.",
        "Placeholders not yet set by the owner: "
        + (", ".join(decision["policy"]["placeholders"]) or "none")
        + ".",
        "You may recommend: limit actions, rebalances inside the policy, status changes under the status rule, execution fixes, data pulls, requests for a policy ruling, or no action. You may not recommend sizing from grade D numbers, dropping a strategy to close a data gap, or any trade the policy zone forbids.",
    ]
    add("policy", "Policy", "\n".join(pol))
    rules = policy.get("standing_rules") or []
    add(
        "standing_rules",
        "Owner standing rules",
        "\n".join(f"- {r['id']}: {r['text']} (source: {r['source']})" for r in rules)
        or "none recorded",
    )
    holes = _holes(data)
    dates = "; ".join(
        f"{s['source']} {s['data_date']} ({s['age_days']} days old, {s['state']})"
        for s in decision["sources"]
    )
    add(
        "data_holes",
        "Data dates and known data holes",
        f"Data dates: {dates}.\n"
        + "\n".join(
            f"- {h['id']}: {h['what']}. Blocks: {h['blocks']}. Closes by: {h['closes_by']}."
            for h in holes
        ),
    )
    recs = (recommendations or {}).get("records") or []
    open_recs = [r for r in recs if r["status"] == "OPEN"]
    add(
        "open_recommendations",
        "Open recommendations from earlier runs",
        (
            f"Log {(recommendations or {}).get('status', 'EMPTY')}. "
            + (
                "; ".join(
                    f"{r['rec_id']} {r['type']}: {r['recommendation']} (owner: {(r['owner_response'] or {}).get('response', 'not answered')})"
                    for r in open_recs
                )
                if open_recs
                else "No open recommendations."
            )
        ),
    )
    issue_lines = known_issue_lines(known_issues) if known_issues is not None else []
    add(
        "do_not_rely",
        "DO NOT RELY ON",
        "\n".join(f"- {d}" for d in [*DO_NOT_RELY, stress_bound_line(data)])
        + (
            "\nKnown issues of this release (governed list, contracts/portfolio-known-issues-v1.json):\n"
            + "\n".join(f"- {line}" for line in issue_lines)
            if issue_lines
            else ""
        ),
    )
    all_text = "\n".join(s["text"] for s in sections)
    used = sorted(set(_STATUS_WORD.findall(all_text)) | set(_SINGLE.findall(all_text)))
    missing = [t for t in used if t not in GLOSSARY and t not in CAVEATS]
    glossary_lines = [f"- {t}: {GLOSSARY[t]}" for t in used if t in GLOSSARY]
    glossary_lines += [f"- {t}: (no glossary entry)" for t in missing]
    sections.insert(
        4,
        {
            "id": "glossary",
            "title": "Status glossary",
            "text": "\n".join(glossary_lines),
        },
    )
    body = "\n\n".join(
        f"# {n}. {s['title']}\n{s['text']}" for n, s in enumerate(sections, start=1)
    )
    pack_id = "PACK-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    text = body + f"\n\nPack id: {pack_id}\n"
    tokens = math.ceil(len(text.encode("utf-8")) / CHARS_PER_UNIT)
    pack = {
        "schema_version": SCHEMA_VERSION,
        "pack_id": pack_id,
        "as_of": decision["as_of"],
        "evaluated_on": decision["evaluated_on"],
        "policy_version": policy["policy_version"],
        "policy_sha256": policy_sha256,
        "tokens_estimate": tokens,
        "token_cap": HARD_TOKEN_CAP,
        "core_target": CORE_TOKEN_TARGET,
        "section_tokens": {
            s["id"]: math.ceil(len(s["text"].encode("utf-8")) / CHARS_PER_UNIT)
            for s in sections
        },
        "grade_counts": {g: sum(1 for f in facts if f["grade"] == g) for g in GRADES},
        "holes": holes,
        "facts": facts,
        "sections": [{"id": s["id"], "title": s["title"]} for s in sections],
        "text": text,
    }
    failures = validate_pack(pack, sections)
    if failures:
        raise PackError("pack validation: " + "; ".join(failures))
    return pack


def validate_pack(
    pack: Mapping[str, Any], sections: Sequence[Mapping[str, str]] | None = None
) -> list[str]:
    failures: list[str] = []
    text = pack["text"]
    if pack["tokens_estimate"] > HARD_TOKEN_CAP:
        failures.append(
            f"{pack['tokens_estimate']} tokens exceeds the hard cap {HARD_TOKEN_CAP}"
        )
    for token in sorted(set(_STATUS_WORD.findall(text)) | set(_SINGLE.findall(text))):
        if token not in GLOSSARY and token not in CAVEATS:
            failures.append(f"token {token} has no glossary entry")
    ids = {f["id"] for f in pack["facts"]}
    if len(ids) != len(pack["facts"]):
        failures.append("duplicate fact ids")
    for fact in pack["facts"]:
        if fact["grade"] not in GRADES:
            failures.append(f"{fact['id']}: unknown grade")
        for caveat in fact["caveats"]:
            if caveat not in CAVEATS:
                failures.append(f"{fact['id']}: unknown caveat {caveat}")
        if not fact["as_of"]:
            failures.append(f"{fact['id']}: no as-of date")
    for cited in re.findall(r"\[((?:[a-z_]+\.)[^\]\s]+(?:, [^\]\s]+)*)\]", text):
        for fact_id in cited.split(", "):
            if fact_id not in ids:
                failures.append(f"cited fact {fact_id} does not exist")
    if sections is not None:
        for section in sections:
            found = _BANNED.search(section["text"])
            if section["id"] not in _EXEMPT_FROM_BANNED and found:
                failures.append(
                    f"banned measure or word in section {section['id']}: {found.group(0)}"
                )
    return failures


def diff_packs(prior: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    before = {f["id"]: f for f in prior["facts"]}
    after = {f["id"]: f for f in current["facts"]}
    out: list[str] = []
    for fact_id, fact in after.items():
        old = before.get(fact_id)
        if old is None:
            out.append(f"new fact {fact_id} = {fact['value']} {fact['unit']}")
        elif old["value"] != fact["value"]:
            out.append(
                f"{fact_id}: {old['value']} to {fact['value']} {fact['unit']} (as of {old['as_of']} to {fact['as_of']})"
            )
        elif old["grade"] != fact["grade"]:
            out.append(f"{fact_id}: grade {old['grade']} to {fact['grade']}")
    out += [
        f"fact {fact_id} no longer present"
        for fact_id in before
        if fact_id not in after
    ]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prior-pack", type=Path)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import portfolio_decision

    data = json.loads(args.payload.read_bytes())
    policy_bytes = (
        ROOT / "contracts" / "portfolio-decision-policy-v1.json"
    ).read_bytes()
    policy = json.loads(policy_bytes)
    sha = hashlib.sha256(policy_bytes).hexdigest()
    decision = data.get("decision") or portfolio_decision.build_decision(
        data, policy, sha
    )
    sources = json.loads(
        (ROOT / "contracts" / "strategy-return-sources-v1.json").read_bytes()
    )["strategies"]
    prior = json.loads(args.prior_pack.read_bytes()) if args.prior_pack else None
    pack = build_pack(
        data,
        decision,
        policy,
        sha,
        sources,
        data.get("recommendations"),
        prior,
        json.loads(KNOWN_ISSUES_PATH.read_bytes()),
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "context-pack.md").write_bytes(pack["text"].encode("utf-8"))
    (args.out / "context-pack.json").write_bytes(
        (json.dumps(pack, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )
    print(
        f"context_pack: {pack['pack_id']} {pack['tokens_estimate']} tokens (cap {HARD_TOKEN_CAP}) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

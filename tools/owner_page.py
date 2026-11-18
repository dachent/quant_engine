"""One-page owner view generated from the panel payload (#908): no hand-written number.

    python tools/owner_page.py --payload dashboard_data.json --known-issues FILE.md --out DIR

Every figure on the page is read from the payload and recorded with its payload path in
``owner-page.json``; ``validate_page`` fails the page if a digit string in the generated text
is not one of those figures (dates aside).  The caveat block is the known-issues file, verbatim
and hashed.  Draft: it reads the payload read-only and writes two small files.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class OwnerPageError(ValueError):
    """The page cannot be built or failed validation."""


class _Page:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.figures: dict[str, dict[str, str]] = {}
        self.labels: list[str] = []

    def label(self, text: str) -> str:
        self.labels.append(text)
        return text

    def fig(self, key: str, text: str, path: str) -> str:
        self.figures[key] = {"text": text, "path": path}
        return text

    def add(self, line: str = "") -> None:
        self.lines.append(line)


def _get(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


def _pct(value: object, digits: int = 1) -> str:
    return f"{float(str(value)) * 100:.{digits}f}%"


def _usd(value: object) -> str:
    return "$" + format(Decimal(str(value)), ",.0f")


def _age(cutoff: str, other: str | None) -> str:
    if other is None:
        return "no date"
    return f"{(date.fromisoformat(cutoff) - date.fromisoformat(other)).days} days before the cutoff"


def build_page(
    data: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    known_issues: str,
    known_issues_sha256: str,
    prior_figures: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    page = _Page()
    meta = data.get("meta") or {}
    cutoff = str(
        meta.get("portfolio_as_of") or _get(data, "capital_as_of.systematic") or ""
    )
    if not cutoff:
        raise OwnerPageError("the payload has no cutoff date")
    generation = str(meta.get("report_generation_id") or "unknown")[:8]
    page.add("# Owner page")
    page.add(
        f"Cutoff {cutoff}. Generated from the panel payload (report generation {generation}). Every number below is read from that payload; the path of each is in owner-page.json."
    )

    # 1. total liquid and the dates of its parts
    page.add("")
    page.add("## Total liquid")
    capital = data.get("custody_current_capital") or {}
    total = capital.get("total_liquid") or {}
    if (
        total.get("status") == "AVAILABLE"
        and (total.get("current_value") or {}).get("value") is not None
    ):
        value = page.fig(
            "total_liquid",
            _usd(total["current_value"]["value"]),
            "custody_current_capital.total_liquid.current_value.value",
        )
        stated = total.get("as_of_date")
        page.add(
            f"Total liquid {value}, labelled as of {stated} (the oldest part's date, not the cutoff)."
        )
    else:
        reason = total.get("reason") or "no reason given"
        page.add(
            f"Total liquid is not produced at the cutoff ({total.get('status', 'status missing')}): {reason}"
        )
    parts = total.get("part_dates")
    page.add(
        "Part dates (a total is a sum of parts with these dates):"
        if total.get("status") == "AVAILABLE"
        else "The parts and their dates:"
    )
    if parts:
        for part in parts:
            age = page.fig(
                f"part_age.{part['part']}",
                _age(cutoff, part["as_of_date"]),
                f"custody_current_capital.total_liquid.part_dates[{part['part']}]",
            )
            page.add(f"- {page.label(part['part'])}: {part['as_of_date']} ({age})")
    else:
        for name in ("systematic", "retained_excluded", "reserve"):
            node = capital.get(name) or {}
            amount = (node.get("current_value") or {}).get("value")
            shown = (
                page.fig(
                    f"capital.{name}",
                    _usd(amount),
                    f"custody_current_capital.{name}.current_value.value",
                )
                if amount is not None
                else "no value"
            )
            page.add(
                f"- {name}: {shown}, as of {node.get('as_of_date') or 'no date'}, status {node.get('status')}"
            )

    # 2. weights
    page.add("")
    page.add("## Strategy weights at the cutoff")
    rows = [
        s
        for s in data.get("strategies") or []
        if s.get("systematic_weight") is not None
        and s.get("weight_basis") == "CURRENT_CUTOFF"
    ]
    if not rows:
        page.add("No strategy carries a current-cutoff weight in this payload.")
    for row in sorted(rows, key=lambda s: -float(s["systematic_weight"])):
        key = row["strategy_id"]
        weight = page.fig(
            f"weight.{key}",
            _pct(row["systematic_weight"]),
            f"strategies[{key}].systematic_weight",
        )
        value = page.fig(
            f"value.{key}",
            _usd(row["current_value_usd"]),
            f"strategies[{key}].current_value_usd",
        )
        page.add(f"- {page.label(row['strategy_name'])}: {weight} ({value})")

    # 3. stress bound and the rule
    page.add("")
    page.add("## Stress bound")
    bound = _get(data, "advisory.correlation_one_stress")
    limit = _get(data, "advisory.hwm_limit")
    if bound is None:
        page.add("The payload carries no stress bound.")
    else:
        shown = page.fig(
            "stress_bound", _pct(bound, 2), "advisory.correlation_one_stress"
        )
        lim = (
            page.fig("limit", _pct(limit, 0), "advisory.hwm_limit")
            if limit is not None
            else "no limit"
        )
        page.add(
            f"Stress bound {shown} of capital against the owner's {lim} limit. It is a scenario in which every strategy loses its stressed fraction on the same day: not a forecast, not an expected loss."
        )
        if decision is not None:
            rule = page.fig(
                "stress_rule",
                decision["portfolio"]["stress_rule"],
                "contracts/portfolio-decision-policy-v1.json stress_rule.label",
            )
            page.add(f"Rule: {rule}")
            mine = Decimal(decision["portfolio"]["stress_loss"])
            theirs = Decimal(str(bound))
            check = page.fig(
                "stress_recompute", _pct(mine, 2), "decision.portfolio.stress_loss"
            )
            if abs(mine - theirs) > Decimal("0.0005"):
                page.add(
                    f"Cross-check: recomputing from the policy file and the payload weights gives {check}, which differs from the payload figure. The payload figure is the figure of record; reconcile the difference before relying on either."
                )
            else:
                page.add(
                    f"Cross-check: recomputing from the policy file gives {check}, the same within rounding."
                )
        else:
            page.add(
                "Rule line and cross-check unavailable: the decision inputs are missing from this payload."
            )

    # 4. drawdown against the limit
    page.add("")
    page.add("## Drawdown against the limit (high-water-mark index)")
    if decision is not None:
        p = decision["portfolio"]
        dd = page.fig(
            "drawdown",
            _pct(p["drawdown"], 2),
            "advisory.hwm_candidate.current_drawdown",
        )
        lim2 = page.fig("limit_dd", _pct(p["limit"], 0), "advisory.hwm_limit")
        head = page.fig(
            "headroom",
            f"{p['headroom_points']} points",
            "advisory.hwm_limit minus advisory.hwm_candidate.current_drawdown",
        )
        worst = page.fig(
            "worst_drawdown",
            _pct(p["worst_drawdown"], 2),
            "advisory.hwm_candidate.maximum_drawdown",
        )
        page.add(
            f"Drawdown from the high-water mark {dd} against the {lim2} limit: {head} of headroom. Worst on record {worst}. Zone: {p['zone_label']}. {p['zone_rule']}"
        )
        label = _get(data, "advisory.hwm_candidate.label")
        page.add(
            f"Index label in the payload: {label}. Owner accepted the index on {p['measure_acceptance']['accepted_on']} (ruling)."
        )
    else:
        page.add(
            "Not available: the payload lacks the high-water-mark or cluster blocks the decision reads."
        )

    # 5. data health
    page.add("")
    page.add("## Data health")
    status = data.get("fact_status") or {}
    complete = sorted(
        k for k, v in status.items() if v == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    )
    other = sorted(k for k in status if k not in complete)
    head_line = page.fig(
        "facts_complete", f"{len(complete)} of {len(status)}", "fact_status"
    )
    page.add(
        f"{head_line} fact kinds are complete within their declared boundary; the rest are incomplete."
    )
    matrix = data.get("fact_coverage_matrix") or []
    for kind in other:
        rows_k = [r for r in matrix if r.get("fact_type") == kind]
        nopath = sum(1 for r in rows_k if r.get("status") == "NO_ACCEPTED_FACT_PATH")
        named = f"{status[kind]}"
        if nopath:
            named += f"; {page.fig(f'nopath.{kind}', str(nopath), f'fact_coverage_matrix[{kind}] NO_ACCEPTED_FACT_PATH')} account rows have no admitted source path"
        page.add(f"- {page.label(kind)}: {named}")
    for item in data.get("blocking_items") or []:
        value = page.fig(
            f"blocking.{item.get('item')}",
            "no count" if item.get("value") is None else str(item.get("value")),
            f"blocking_items[{item.get('item')}].value",
        )
        page.add(
            f"- gap named in the payload: {item.get('item')} ({value}); {item.get('known_gap')}; closes by: {item.get('resolution_authority')}"
        )

    # 6. changes
    page.add("")
    page.add("## What changed since last week")
    if prior_figures is None:
        page.add("Placeholder: no previous page to compare with.")
    else:
        for key, fig in sorted(page.figures.items()):
            old = prior_figures.get(key)
            if old is not None and old["text"] != fig["text"]:
                page.add(f"- {key}: {page.label(old['text'])} to {fig['text']}")
        if page.lines[-1] == "## What changed since last week":
            page.add("No figure changed.")
    body = "\n".join(page.lines)
    problems = validate_page(body, page.figures, page.labels)
    if problems:
        raise OwnerPageError("; ".join(problems))

    # 7. caveats (verbatim; not part of the generated-number check)
    text = (
        body
        + "\n\n## Known issues of this release (verbatim from the disclosure file)\n"
        + known_issues.rstrip()
        + "\n"
    )
    return {
        "text": text,
        "figures": page.figures,
        "known_issues_sha256": known_issues_sha256,
        "cutoff": cutoff,
        "generation": generation,
    }


def validate_page(
    body: str, figures: Mapping[str, Mapping[str, str]], labels: Sequence[str] = ()
) -> list[str]:
    """Every digit string in the generated text must be, or sit inside, a recorded figure
    or a payload name (strategy, part, fact kind); dates and quoted payload sentences aside."""

    stripped = _DATE.sub("", body)
    for text in sorted(
        [f["text"] for f in figures.values()] + list(labels), key=len, reverse=True
    ):
        stripped = stripped.replace(text, " ")
    allowed = (
        "Total liquid is not produced",
        "Index label in the payload",
        "Cutoff ",
        "- gap named in the payload",
    )
    out: list[str] = []
    for line in stripped.splitlines():
        if line.startswith(allowed):
            continue
        out += [
            f"untraced number {t!r} in: {line.strip()[:80]}"
            for t in _NUMBER.findall(line)
        ]
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--known-issues", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prior-figures", type=Path)
    args = parser.parse_args(argv)
    import portfolio_decision

    data = json.loads(args.payload.read_bytes())
    policy_bytes = (
        ROOT / "contracts" / "portfolio-decision-policy-v1.json"
    ).read_bytes()
    decision = data.get("decision")
    if decision is None:
        try:
            decision = portfolio_decision.build_decision(
                data, json.loads(policy_bytes), hashlib.sha256(policy_bytes).hexdigest()
            )
        except portfolio_decision.DecisionError as exc:
            print(f"owner_page: decision block unavailable: {exc}", file=sys.stderr)
    issues = args.known_issues.read_bytes()
    prior = (
        json.loads(args.prior_figures.read_bytes())["figures"]
        if args.prior_figures
        else None
    )
    page = build_page(
        data,
        decision,
        issues.decode("utf-8"),
        hashlib.sha256(issues).hexdigest(),
        prior,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "owner-page.md").write_bytes(page["text"].encode("utf-8"))
    (args.out / "owner-page.json").write_bytes(
        (
            json.dumps(
                {k: v for k, v in page.items() if k != "text"}, indent=1, sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
    )
    print(
        f"owner_page: cutoff {page['cutoff']} generation {page['generation']} {len(page['figures'])} figures -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

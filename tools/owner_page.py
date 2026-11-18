"""One-page owner view generated from the panel payload (#908): no hand-written number.

    python tools/owner_page.py --payload dashboard_data.json --known-issues FILE.md --out DIR

Every figure on the page is read from the payload and recorded with its payload path in
``owner-page.json``; ``validate_page`` fails the page if a digit string in the generated text
is not one of those figures, a payload name or a date.  The stress rule line is the payload's own
(``adverse_loss_basis.rule_statement``); the page refuses to build without it.  The caveat block is
the known-issues file, verbatim and hashed.  Read-only on the payload; it imports no other tool.
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
from typing import Any

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PRIOR = re.compile(r"([\d.]+)\s*x\s*(?:its\s*)?prior", re.IGNORECASE)
_LIVE = re.compile(r"([\d.]+)\s*x\s*(?:its\s*)?live", re.IGNORECASE)
_UPLIFT = re.compile(r"\+\s*([\d.]+)\s*pp", re.IGNORECASE)
_TOLERANCE = Decimal("0.000001")


class OwnerPageError(ValueError):
    """The page cannot be built or failed validation."""


class _Page:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.figures: dict[str, dict[str, str]] = {}
        self.labels: list[str] = []

    def fig(self, key: str, text: str, path: str) -> str:
        self.figures[key] = {"text": text, "path": path}
        return text

    def label(self, text: str) -> str:
        self.labels.append(text)
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


def _rule_loss(basis: Mapping[str, Any], rule: str) -> Decimal | None:
    """The stored rule applied to a component's recorded drawdown inputs, with the
    multipliers read from the payload's own rule statement; None if they cannot be read."""

    prior_m, live_m, uplift = (
        _PRIOR.search(rule),
        _LIVE.search(rule),
        _UPLIFT.search(rule),
    )
    if not (prior_m and live_m and uplift):
        return None
    prior_inputs = [
        Decimal(str(basis[k]))
        for k in ("prior_max_drawdown", "backtest_max_drawdown")
        if basis.get(k) is not None
    ]
    terms = []
    if prior_inputs:
        terms.append(Decimal(prior_m.group(1)) * max(prior_inputs))
    if basis.get("live_max_drawdown") is not None:
        terms.append(
            Decimal(live_m.group(1)) * Decimal(str(basis["live_max_drawdown"]))
        )
    if not terms:
        return None
    loss = max(terms)
    if basis.get("platform_only_uplift_applied"):
        loss += Decimal(uplift.group(1)) / 100
    return min(loss, Decimal(1))


def build_page(
    data: Mapping[str, Any],
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
    available = (
        total.get("status") == "AVAILABLE"
        and (total.get("current_value") or {}).get("value") is not None
    )
    if available:
        value = page.fig(
            "total_liquid",
            _usd(total["current_value"]["value"]),
            "custody_current_capital.total_liquid.current_value.value",
        )
        page.add(
            f"Total liquid {value}, labelled as of {total.get('as_of_date')} (the oldest part's date, not the cutoff)."
        )
    else:
        page.add(
            f"Total liquid is not produced at the cutoff ({total.get('status', 'status missing')}): {total.get('reason') or 'no reason given'}"
        )
    parts = total.get("part_dates")
    page.add(
        "Part dates (a total is a sum of parts with these dates):"
        if available
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

    # 3. stress bound, its rule (from the payload) and checks against the payload's own components
    page.add("")
    page.add("## Stress bound")
    bound = _get(data, "advisory.correlation_one_stress")
    limit = _get(data, "advisory.hwm_limit")
    components = _get(data, "dependence_analysis.correlation_one.components") or []
    if bound is None:
        page.add("The payload carries no stress bound.")
    else:
        statements = {
            str(c["adverse_loss_basis"]["rule_statement"])
            for c in components
            if isinstance(c.get("adverse_loss_basis"), Mapping)
            and c["adverse_loss_basis"].get("rule_statement")
        }
        if not statements:
            raise OwnerPageError(
                "the payload lacks adverse_loss_basis.rule_statement: the stress rule line cannot be stated"
            )
        if len(statements) > 1:
            raise OwnerPageError(
                "the payload carries different rule statements for different strategies"
            )
        rule = page.fig(
            "stress_rule",
            statements.pop(),
            "dependence_analysis.correlation_one.components[].adverse_loss_basis.rule_statement",
        )
        shown = page.fig(
            "stress_bound", _pct(bound, 2), "advisory.correlation_one_stress"
        )
        lim = (
            page.fig("limit", _pct(limit, 0), "advisory.hwm_limit")
            if limit is not None
            else "no limit"
        )
        page.add(f"Stress bound {shown} of capital against the owner's {lim} limit.")
        page.add(f"Rule: {rule}")
        page.add(
            "Strategy, weight used in the bound, loss under the rule, contribution; the recorded inputs follow:"
        )
        recompute_sum = Decimal(0)
        mismatches: list[str] = []
        unreadable = False
        for item in sorted(components, key=lambda c: -float(c["loss_contribution"])):
            sid = str(item["series_id"])
            w = page.fig(
                f"bound_weight.{sid}",
                _pct(item["allocation_weight"], 2),
                f"dependence_analysis.correlation_one.components[{sid}].allocation_weight",
            )
            f = page.fig(
                f"bound_loss.{sid}",
                _pct(item["conservative_loss_fraction"], 2),
                f"dependence_analysis.correlation_one.components[{sid}].conservative_loss_fraction",
            )
            c = page.fig(
                f"bound_contribution.{sid}",
                _pct(item["loss_contribution"], 2),
                f"dependence_analysis.correlation_one.components[{sid}].loss_contribution",
            )
            basis = item.get("adverse_loss_basis") or {}
            inputs = []
            for key, name in (
                ("prior_max_drawdown", "prior worst"),
                ("backtest_max_drawdown", "backtest worst"),
                ("live_max_drawdown", "live worst"),
            ):
                if basis.get(key) is not None:
                    inputs.append(
                        f"{name} {page.fig(f'basis.{sid}.{key}', _pct(basis[key], 1), f'dependence_analysis.correlation_one.components[{sid}].adverse_loss_basis.{key}')}"
                    )
            if basis.get("platform_only_uplift_applied"):
                inputs.append("platform-only uplift applied")
            page.add(
                f"- {page.label(sid)}: weight {w}, loss {f}, contribution {c}"
                + (f" ({'; '.join(inputs)})" if inputs else "")
            )
            recompute_sum += Decimal(str(item["allocation_weight"])) * Decimal(
                str(item["conservative_loss_fraction"])
            )
            by_rule = _rule_loss(basis, rule) if basis else None
            if by_rule is None:
                unreadable = True
            elif (
                abs(by_rule - Decimal(str(item["conservative_loss_fraction"])))
                > _TOLERANCE
            ):
                mismatches.append(sid)
        if components:
            check = page.fig(
                "bound_from_components",
                _pct(recompute_sum, 2),
                "sum of allocation_weight x conservative_loss_fraction",
            )
            same = abs(recompute_sum - Decimal(str(bound))) <= _TOLERANCE
            page.add(
                f"Rows check: the strategy rows above add up to {check}, "
                + (
                    "the same as the bound."
                    if same
                    else "which DIFFERS from the bound: do not rely on the bound until reconciled."
                )
            )
            if unreadable:
                page.add(
                    "Rule check: the rule could not be re-applied to every strategy from the recorded inputs and the rule statement."
                )
            elif mismatches:
                page.add(
                    "Rule check: re-applying the rule to the recorded inputs DIFFERS from the stored loss for "
                    + ", ".join(page.label(m) for m in mismatches)
                    + "."
                )
            else:
                page.add(
                    "Rule check: re-applying the rule to the recorded inputs reproduces every stored loss."
                )
        else:
            page.add("No strategy rows in the payload to check the bound against.")
        other = _get(data, "dependence_analysis.correlation_one.loss")
        if (
            other is not None
            and abs(Decimal(str(other)) - Decimal(str(bound))) > _TOLERANCE
        ):
            page.add(
                "Bound check: the advisory bound and the dependence block's own bound DIFFER."
            )

    # 4. drawdown against the limit (HWM index)
    page.add("")
    page.add("## Drawdown against the limit (high-water-mark index)")
    cand = _get(data, "advisory.hwm_candidate") or {}
    if cand.get("current_drawdown") is not None and limit is not None:
        dd_value = abs(Decimal(str(cand["current_drawdown"])))
        dd = page.fig(
            "drawdown", _pct(dd_value, 2), "advisory.hwm_candidate.current_drawdown"
        )
        lim2 = page.fig("limit_dd", _pct(limit, 0), "advisory.hwm_limit")
        head = page.fig(
            "headroom",
            f"{(Decimal(str(limit)) - dd_value) * 100:.2f} points",
            "advisory.hwm_limit minus advisory.hwm_candidate.current_drawdown",
        )
        line = f"Drawdown from the high-water mark {dd} against the {lim2} limit: {head} of headroom."
        if cand.get("maximum_drawdown") is not None:
            line += f" Worst on record {page.fig('worst_drawdown', _pct(abs(Decimal(str(cand['maximum_drawdown']))), 2), 'advisory.hwm_candidate.maximum_drawdown')}."
        page.add(line)
        page.add(
            f"Index label in the payload: {cand.get('label')}; financial acceptance in the payload: {cand.get('financial_acceptance')}."
        )
    else:
        page.add(
            "Not available: the payload lacks the high-water-mark drawdown or the limit."
        )

    # 5. data health
    page.add("")
    page.add("## Data health")
    status = data.get("fact_status") or {}
    complete = sorted(
        k for k, v in status.items() if v == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    )
    other_kinds = sorted(k for k in status if k not in complete)
    head_line = page.fig(
        "facts_complete", f"{len(complete)} of {len(status)}", "fact_status"
    )
    page.add(
        f"{head_line} fact kinds are complete within their declared boundary; the rest are incomplete."
    )
    matrix = data.get("fact_coverage_matrix") or []
    for kind in other_kinds:
        nopath = sum(
            1
            for r in matrix
            if r.get("fact_type") == kind and r.get("status") == "NO_ACCEPTED_FACT_PATH"
        )
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
        changed = 0
        for key, fig in sorted(page.figures.items()):
            old = prior_figures.get(key)
            if old is not None and old["text"] != fig["text"]:
                page.add(f"- {key}: {page.label(old['text'])} to {fig['text']}")
                changed += 1
        if not changed:
            page.add("No figure changed.")
    body = "\n".join(page.lines)
    problems = validate_page(body, page.figures, page.labels)
    if problems:
        raise OwnerPageError("; ".join(problems))
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
    data = json.loads(args.payload.read_bytes())
    issues = args.known_issues.read_bytes()
    prior = (
        json.loads(args.prior_figures.read_bytes())["figures"]
        if args.prior_figures
        else None
    )
    page = build_page(
        data, issues.decode("utf-8"), hashlib.sha256(issues).hexdigest(), prior
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "owner-page.md").write_bytes(page["text"].encode("utf-8"))
    summary = {k: v for k, v in page.items() if k != "text"}
    (args.out / "owner-page.json").write_bytes(
        (json.dumps(summary, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )
    print(
        f"owner_page: cutoff {page['cutoff']} generation {page['generation']} {len(page['figures'])} figures -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

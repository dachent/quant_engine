"""Owner view (#908): one generated page per release from the validator findings.

Stdlib only (the acceptance layer imports no product code).  Every finding becomes one or
more *items*; a finding no register entry classifies becomes an UNCLASSIFIED item, which
counts as a real problem and is listed first.  Nothing is dropped: ``reconcile`` fails when
any source finding is not referenced by an item.  Design: records/sprint/OWNER-VIEW-DESIGN.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import json
import re
from typing import Any

SCHEMA = "qe-owner-view/1"

BLOCKER = "BLOCKER"
UNCLASSIFIED = "UNCLASSIFIED"
REAL_DATA_GAP = "REAL_DATA_GAP"
CHECKER_BUG = "CHECKER_BUG"
BUILDER_NOT_WIRED = "BUILDER_NOT_WIRED"
NOT_APPLICABLE = "NOT_APPLICABLE"
ACCEPTED_BY_DESIGN = "ACCEPTED_BY_DESIGN"
NEEDS_OWNER_DECISION = "NEEDS_OWNER_DECISION"

OWNER_CLASSES = (
    NEEDS_OWNER_DECISION,
    REAL_DATA_GAP,
    BUILDER_NOT_WIRED,
    ACCEPTED_BY_DESIGN,
    CHECKER_BUG,
    NOT_APPLICABLE,
)
# display order: stop first, then anything unclassified, then the six
ORDER = (BLOCKER, UNCLASSIFIED, *OWNER_CLASSES)
WORDS = {
    BLOCKER: "Wrong number: release stops",
    UNCLASSIFIED: "Unclassified: treat as a real problem",
    NEEDS_OWNER_DECISION: "Needs your decision",
    REAL_DATA_GAP: "Real data gap",
    BUILDER_NOT_WIRED: "Not built or not wired",
    ACCEPTED_BY_DESIGN: "Accepted by design (owner record named)",
    CHECKER_BUG: "Checker bug (the rule is wrong)",
    NOT_APPLICABLE: "Does not apply",
}
REAL = frozenset(
    {BLOCKER, UNCLASSIFIED, NEEDS_OWNER_DECISION, REAL_DATA_GAP, BUILDER_NOT_WIRED}
)
_REQUIRED = ("what", "fix", "who", "how_long")
_TEMPLATE = re.compile(r"\{([A-Za-z0-9_.]+)\}")


def real_or_checker(klass: str) -> str:
    if klass in REAL:
        return "real"
    return "accepted" if klass == ACCEPTED_BY_DESIGN else "checker"


# ------------------------------------------------------------------------ registry


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


_MISSING = object()


def _iso(value: Any) -> bool:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return isinstance(value, str)


def _entry_errors(position: int, entry: Any) -> list[str]:
    if not isinstance(entry, Mapping):
        return [f"entry {position}: not an object"]
    block = entry.get("owner_view")
    if block is None:
        return []
    label = f"entry {position} ({entry.get('check_id')}/{entry.get('defect')})"
    if not isinstance(block, Mapping):
        return [f"{label}: owner_view is not an object"]
    klass = block.get("owner_class")
    if klass not in OWNER_CLASSES:
        return [f"{label}: owner_class {klass!r} is not one of the six"]
    errors: list[str] = []
    if not isinstance(entry.get("defect"), str) or not entry["defect"].strip():
        errors.append(
            f"{label}: defect is required (a defect id, or NONE for a finding with none)"
        )
    if "close_by" in entry and not _iso(entry["close_by"]):
        errors.append(f"{label}: close_by is not an ISO date")
    for key in _REQUIRED:
        if not _text(block.get(key)):
            errors.append(f"{label}: owner_view.{key} is required text")
    for key in ("where", "number_affected", "proof_check"):
        if key in block and not _text(block[key]):
            errors.append(f"{label}: owner_view.{key} must be non-blank text")
    for key in ("entity_match", "finding_entity_match", "finding_check_id"):
        if key in block and not isinstance(block[key], str):
            errors.append(f"{label}: {key} must be a string")
    when = block.get("when", {})
    if not isinstance(when, Mapping) or not all(isinstance(k, str) for k in when):
        errors.append(f"{label}: when must be an object with string keys")
    if "decision" in block and klass != NEEDS_OWNER_DECISION:
        errors.append(f"{label}: only a NEEDS_OWNER_DECISION entry carries a decision")
    if "owner_record" in block and not _text(block["owner_record"]):
        errors.append(f"{label}: owner_record must be non-blank text")
    if klass == NEEDS_OWNER_DECISION:
        decision = block.get("decision")
        decision = decision if isinstance(decision, Mapping) else {}
        options = decision.get("options")
        if (
            not isinstance(options, list)
            or not _text(decision.get("question"))
            or not 2 <= len(options) <= 3
            or not all(_text(o) for o in options)
        ):
            errors.append(f"{label}: a decision needs a question and 2-3 options")
    if klass == ACCEPTED_BY_DESIGN and not (
        _text(block.get("owner_record")) and _iso(block.get("reviewed_on"))
    ):
        errors.append(
            f"{label}: ACCEPTED_BY_DESIGN needs owner_record and an ISO reviewed_on"
        )
    if block.get("scope", "entity") not in ("entity", "row"):
        errors.append(f"{label}: scope must be 'entity' or 'row'")
    if block.get("scope") != "row" and "entity_match" not in block:
        errors.append(
            f"{label}: an entry must say which entities it covers (entity_match) "
            "or declare scope 'row'"
        )
    if (
        block.get("scope") == "row"
        and not str(block.get("row_scope_reason") or "").strip()
    ):
        errors.append(
            f"{label}: scope 'row' needs row_scope_reason (why every entity of the "
            "row is in the same state)"
        )
    for key in ("entity_match", "finding_entity_match"):
        try:
            re.compile(block[key] if isinstance(block.get(key), str) else ".*")
        except re.error as exc:
            errors.append(f"{label}: {key} is not a regex: {exc}")
    return errors


def registry_errors(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Problems that make an ``owner_view`` block unusable (it is then ignored)."""

    return [e for n, entry in enumerate(entries) for e in _entry_errors(n, entry)]


def _usable(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        entry
        for n, entry in enumerate(entries)
        if isinstance(entry, Mapping)
        and entry.get("owner_view")
        and not _entry_errors(n, entry)
    ]


# -------------------------------------------------------------------------- expand


def _flat(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            _flat(f"{prefix}.{key}", inner, out)
    else:
        out[prefix] = value


def expand(finding: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(entity, facts) pairs.  A per-entity map in ``observed`` is split: the states of the
    entities differ, so one verdict for the whole row would hide the minority."""

    observed = finding.get("observed")
    pairs: list[tuple[str, dict[str, Any]]] = []
    if isinstance(observed, Mapping):
        uncovered = observed.get("uncovered_accounts")
        unavailable = observed.get("unavailable")
        if isinstance(uncovered, Mapping) and uncovered:
            pairs = [(str(k), {"status": v}) for k, v in sorted(uncovered.items())]
        elif isinstance(unavailable, list) and unavailable:
            pairs = [(str(k), {}) for k in unavailable]
    if not pairs:
        pairs = [(str(finding.get("entity", "")), {})]
    result = []
    for entity, extra in pairs:
        facts: dict[str, Any] = {"entity": entity}
        _flat("observed", observed, facts)
        facts.update({f"entity_{k}": v for k, v in extra.items()})
        facts["check_id"] = finding.get("check_id")
        result.append((entity, facts))
    return result


def _fill(text: str, facts: Mapping[str, Any]) -> str:
    return _TEMPLATE.sub(lambda m: str(facts.get(m.group(1), "?")), text)


def _matches(
    entry: Mapping[str, Any],
    finding: Mapping[str, Any],
    entity: str,
    facts: Mapping[str, Any],
    row: bool = False,
) -> bool:
    block = entry["owner_view"]
    # the register is keyed by the validator's check id; a check that emits rows under
    # derived ids (expected.not_derivable) names them in finding_check_id
    if block.get("finding_check_id", entry.get("check_id")) != finding.get("check_id"):
        return False
    if not re.fullmatch(
        block.get("finding_entity_match", ".*"), str(finding.get("entity"))
    ):
        return False
    wanted = str(entry.get("defect"))
    have = sorted(set(finding.get("defect_ids") or []))
    # the entry names the finding's whole defect set ("B4,B6"), or NONE for no defects:
    # an entry for one defect never classifies a finding that also carries another
    if (wanted == "NONE") != (not have) or (
        have and sorted(p.strip() for p in wanted.split(",")) != have
    ):
        return False
    if (block.get("scope") == "row") != row:
        return False
    if not re.fullmatch(block.get("entity_match", ".*"), entity):
        return False
    return all(
        facts.get(k, _MISSING) == v for k, v in (block.get("when") or {}).items()
    )


def _item(
    klass: str,
    finding: Mapping[str, Any],
    index: int,
    entity: str,
    message: str,
    entry: Mapping[str, Any] | None,
    facts: Mapping[str, Any],
    today: date,
    since: str | None,
) -> dict[str, Any]:
    ref = {
        "check_id": finding.get("check_id"),
        "defect_ids": list(finding.get("defect_ids") or []),
        "issue": (entry or {}).get("issue"),
    }
    common = {
        "class": klass,
        "word": WORDS[klass],
        "since": since,
        "ref": ref,
        "finding_refs": [index],
        "entity": entity,
    }
    if entry is None:
        return {
            **common,
            "real_or_checker": "real",
            "what": message
            or f"{finding.get('check_id')} on {entity}: not classified yet.",
            "what_all": message or f"{finding.get('check_id')}: not classified yet.",
            "where": entity,
            "number_affected": "unknown",
            "decision": None,
            "fix": "classify this finding in the register, or fix the cause",
            "who": "validator owner",
            "how_long": "unknown",
            "overdue": False,
        }
    block = entry["owner_view"]
    number = _fill(str(block.get("number_affected") or "unknown"), facts)
    if number.strip().lower().startswith(("none", "0", "zero", "nil")) and not _text(
        block.get("proof_check")
    ):
        number = "unknown"  # "none" must cite the check that proves it
    close_by = entry.get("close_by")
    return {
        **common,
        "real_or_checker": real_or_checker(klass),
        "what": _fill(block["what"], facts),
        # the same sentence for every entity of a group: the entities go under Where
        "what_all": _fill(
            block["what"], {**facts, "entity": "each entity under Where"}
        ),
        "where": _fill(str(block.get("where") or entity), facts),
        "number_affected": number,
        "decision": block.get("decision"),
        "fix": _fill(block["fix"], facts),
        "who": block["who"],
        "how_long": block["how_long"],
        "overdue": bool(close_by and date.fromisoformat(close_by) < today),
        "owner_record": block.get("owner_record"),
    }


# ---------------------------------------------------------------------------- build


class ReconciliationError(Exception):
    pass


def reconcile(findings: Sequence[Any], items: Sequence[Mapping[str, Any]]) -> None:
    referenced = {r for item in items for r in item["finding_refs"]}
    expected = set(range(len(findings)))
    if expected - referenced:
        raise ReconciliationError(
            f"findings without an owner-view item: {sorted(expected - referenced)}"
        )
    if referenced - expected:
        raise ReconciliationError(
            f"items reference unknown findings: {sorted(referenced - expected)}"
        )


def build_owner_view(
    findings_doc: Mapping[str, Any],
    *,
    messages: Sequence[str] = (),
    register: Sequence[Mapping[str, Any]] = (),
    today: date | None = None,
    history: Sequence[tuple[str, Mapping[str, Any]]] = (),
    compared_with: str | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    findings = list(findings_doc.get("findings") or [])
    entries = _usable(register)
    first_seen: dict[tuple[Any, Any], str] = {}
    for label, doc in history:
        if not isinstance(doc, Mapping) or not isinstance(doc.get("findings"), list):
            continue  # a malformed retained document never blocks the current run
        for old in doc["findings"]:
            if not isinstance(old, Mapping):
                continue
            first_seen.setdefault((old.get("check_id"), old.get("entity")), label)
            for sub, _ in expand(old):
                first_seen.setdefault((old.get("check_id"), sub), label)
    items: list[dict[str, Any]] = []
    overlaps: list[str] = []
    for index, finding in enumerate(findings):
        message = messages[index] if index < len(messages) else ""
        since = first_seen.get((finding.get("check_id"), finding.get("entity")))
        if str(finding.get("class", "")).startswith("BLOCKER"):
            entity = str(finding.get("entity"))
            items.append(
                _item(BLOCKER, finding, index, entity, message, None, {}, today, since)
            )
            continue
        whole = str(finding.get("entity", ""))
        row_facts = expand({**finding, "observed": None})[0][1]
        _flat("observed", finding.get("observed"), row_facts)
        states = {
            tuple(sorted((k, str(v)) for k, v in f.items() if k.startswith("entity_")))
            for _, f in expand(finding)
        }
        row_matched = (
            [e for e in entries if _matches(e, finding, whole, row_facts, True)][:1]
            if len(states) == 1  # differing entity states are never classified as one
            else []
        )
        if row_matched:  # the entry asserts one state for every entity of the row
            for entry in row_matched:
                klass = entry["owner_view"]["owner_class"]
                items.append(
                    _item(
                        klass,
                        finding,
                        index,
                        whole,
                        message,
                        entry,
                        row_facts,
                        today,
                        since,
                    )
                )
        for entity, facts in expand(finding):
            entity_since = (
                since
                if entity == finding.get("entity")
                else first_seen.get((finding.get("check_id"), entity))
            )
            hits = [e for e in entries if _matches(e, finding, entity, facts)]
            if len({e["owner_view"]["owner_class"] for e in hits}) > 1:
                # overlapping entries disagree: the most real class wins, never the
                # harmless one, and the conflict is reported on the page
                overlaps.append(
                    f"{finding.get('check_id')} {entity}: entries disagree on the class"
                )
                hits.sort(key=lambda e: ORDER.index(e["owner_view"]["owner_class"]))
            matched = hits[:1]
            for entry in matched:
                klass = entry["owner_view"]["owner_class"]
                items.append(
                    _item(
                        klass,
                        finding,
                        index,
                        entity,
                        message,
                        entry,
                        facts,
                        today,
                        entity_since,
                    )
                )
            if not matched and not row_matched:  # a row verdict never hides an entity
                items.append(
                    _item(
                        UNCLASSIFIED,
                        finding,
                        index,
                        entity,
                        message,
                        None,
                        facts,
                        today,
                        entity_since,
                    )
                )
    reconcile(findings, items)
    items.sort(
        key=lambda i: (
            ORDER.index(i["class"]),
            str(i["ref"]["check_id"]),
            i["entity"],
            ",".join(i["ref"]["defect_ids"]),
            i["what"],
        )
    )
    counts = {k: 0 for k in ORDER}
    for item in items:
        counts[item["class"]] += 1
    return {
        "schema": SCHEMA,
        "generation_id": findings_doc.get("generation_id"),
        "evaluated_on": today.isoformat(),
        "compared_with": compared_with,
        "source_findings": len(findings),
        "source_summary": dict(findings_doc.get("summary") or {}),
        "items": items,
        "counts": counts,
        "real_items": sum(1 for i in items if i["real_or_checker"] == "real"),
        "overdue_items": sum(1 for i in items if i["overdue"]),
        "registry_errors": registry_errors(register) + sorted(set(overlaps)),
        "findings": [
            {
                "index": n,
                "check_id": f.get("check_id"),
                "class": f.get("class"),
                "entity": f.get("entity"),
                "message": messages[n] if n < len(messages) else "",
            }
            for n, f in enumerate(findings)
        ],
    }


# --------------------------------------------------------------------------- render


def verdict(view: Mapping[str, Any]) -> str:
    c = view["counts"]
    if c[BLOCKER]:
        return (
            f"STOP: {c[BLOCKER]} published figure(s) contradict the independent "
            "recalculation."
        )
    head = (
        f"{view['source_findings']} validator rows became {len(view['items'])} items: "
        f"{c[UNCLASSIFIED]} unclassified (counted as real), "
        f"{c[NEEDS_OWNER_DECISION]} need your decision, {c[REAL_DATA_GAP]} real data "
        f"gaps, {c[BUILDER_NOT_WIRED]} not built or wired, {c[ACCEPTED_BY_DESIGN]} "
        f"accepted by design, {c[CHECKER_BUG]} checker bugs, {c[NOT_APPLICABLE]} do not "
        "apply."
    )
    if view["overdue_items"]:
        head += f" {view['overdue_items']} item(s) are past their fix date."
    return head


def _group(items: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for item in items:
        key = (
            item["class"],
            item["what_all"],
            item["fix"],
            item["number_affected"],
            item["who"],
            item["how_long"],
            item["overdue"],
            item.get("owner_record"),
            item["since"],
            json.dumps(item.get("decision"), sort_keys=True),
        )
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def render_markdown(view: Mapping[str, Any]) -> str:
    lines = [
        f"# Release owner view (generation {str(view.get('generation_id'))[:8]})",
        "",
        f"**{verdict(view)}**",
        "",
    ]
    lines += [f"Evaluated on {view['evaluated_on']}.", ""]
    if view.get("compared_with"):
        lines += [f"Compared with {view['compared_with']}.", ""]
    for klass in ORDER:
        group = [i for i in view["items"] if i["class"] == klass]
        if not group:
            continue
        lines += [f"## {WORDS[klass]} ({len(group)})", ""]
        for rows in _group(group):
            first = rows[0]
            where = ", ".join(sorted({r["where"] for r in rows}))
            lines.append(f"- {first['what'] if len(rows) == 1 else first['what_all']}")
            lines.append(f"  - Where: {where}")
            lines.append(f"  - Number affected: {first['number_affected']}")
            lines.append(
                f"  - {first['real_or_checker'].capitalize()}. Fix: {first['fix']} "
                f"({first['who']}; {first['how_long']})"
            )
            decision = first.get("decision")
            if decision:
                options = " / ".join(
                    f"({chr(97 + n)}) {o}" for n, o in enumerate(decision["options"])
                )
                lines.append(f"  - Decision: {decision['question']} {options}")
            if first.get("owner_record"):
                lines.append(f"  - Owner record: {first['owner_record']}")
            ids = first["ref"]
            tail = [f"since {first['since']}" if first["since"] else "since: not known"]
            if first["overdue"]:
                tail.append("PAST ITS FIX DATE")
            tail.append(f"ref {ids['check_id']} {','.join(ids['defect_ids'])}".strip())
            lines.append("  - " + "; ".join(tail))
        lines.append("")
    if view["registry_errors"]:
        lines += ["## Register errors (entries ignored)", ""]
        lines += [f"- {e}" for e in view["registry_errors"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


def render_js(view: Mapping[str, Any]) -> str:
    payload = dict(view)
    payload["verdict"] = verdict(view)
    text = json.dumps(payload, sort_keys=True).replace("<", "\\u003c")
    return "window.PORTFOLIO_OWNER_VIEW = " + text + ";\n"

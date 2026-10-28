"""Disclosures of a release with derived sleeve epochs, derived from governed bytes (R2).

Two steps, so a report is a pure function of its pinned inputs:

* ``build_sources`` runs once, in the release preparation.  It reads the selected sleeve
  facts, the governed disclosure inputs (a JSON file the sleeve policy manifest pins, plus
  the evidence file it names) and, when reachable, the retained predecessor generation,
  and writes one ``sleeve-disclosure-sources/1`` document.  Every value in it is read from
  those bytes; nothing is typed.  A figure that cannot be resolved is recorded as omitted
  with the reason.
* ``compose`` runs in the report build.  It adds what the report itself carries (epoch
  rows, governed capital, the tag map's sleeves) and returns the methodology items.  A
  measured bound is shown only when the committed return it was measured against equals
  the report row's return; otherwise it is omitted with that reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final

SOURCES_SCHEMA: Final = "sleeve-disclosure-sources/1"
INPUTS_ARTIFACT_ID: Final = "sleeve-disclosure-inputs"
# the two facts artifacts are told apart by what they are (account facts of a parent account,
# facts of a fund family), never by a provider name
ACCOUNT_FACTS: Final = "sleeve-account-facts"
FUND_FACTS: Final = "quantbase-fund-facts"
_CENT: Final = Decimal("0.01")


# the methodology items this module writes; the panel shows them among its limitations
ITEM_NAMES: Final = (
    "Flow timing bounds",
    "Quantbase account cash",
    "Sleeve closure",
    "Sleeve capital basis",
    "Product perimeter",
    "HWM member series",
    "Disclosures not shown",
)


class DisclosureError(ValueError):
    """A disclosure input is malformed or does not match its pinned bytes."""


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise DisclosureError(f"not a decimal: {value!r}") from exc


def _usd(value: Decimal) -> str:
    return format(value.quantize(_CENT), ",f")


def _sha(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisclosureError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise DisclosureError(f"{label} is not an object")
    return value


# ------------------------------------------------------------------ evidence parsing


_BLOCK = re.compile(r"^=== (?P<strategy>\S+) .*?(?P<account>account-[0-9a-f]+)\b")
_COMMITTED = re.compile(r"^committed TWR\s+(?P<value>-?[0-9.]+) %")
_ALL_BOD = re.compile(r"^all legs BOD\s+[-0-9.]+ %\s+delta (?P<value>[-+]?[0-9.]+) pt")
_EXT_BOD = re.compile(
    r"^external legs BOD only\s+[-0-9.]+ %\s+delta (?P<value>[-+]?[0-9.]+) pt"
)
_BOUND = re.compile(
    r"^bounds (?P<name>b_all|b_ext)\s+lower (?P<lo>[-+]?[0-9.]+) pt\s+upper (?P<hi>[-+]?[0-9.]+) pt"
)


def parse_timestamp_evidence(text: str) -> dict[tuple[str, str], dict[str, str]]:
    """Per (strategy, account): the committed TWR and the BOD/EOD bounds, as written."""

    blocks: dict[tuple[str, str], dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        header = _BLOCK.match(line)
        if header is not None:
            current = {}
            blocks[(header["strategy"], header["account"])] = current
            continue
        if current is None:
            continue
        committed = _COMMITTED.match(line)
        if committed is not None:
            current["committed_twr_pct"] = committed["value"]
            continue
        all_bod = _ALL_BOD.match(line)
        if all_bod is not None:
            current["all_bod_delta_pt"] = all_bod["value"]
            continue
        ext_bod = _EXT_BOD.match(line)
        if ext_bod is not None:
            current["ext_bod_delta_pt"] = ext_bod["value"]
            continue
        bound = _BOUND.match(line)
        if bound is not None:
            current[f"{bound['name']}_lower_pt"] = bound["lo"]
            current[f"{bound['name']}_upper_pt"] = bound["hi"]
    return blocks


# ------------------------------------------------------------------ preparation step


def _facts_summary(facts: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(facts.get("provider_id"))
    summary: dict[str, Any] = {
        "artifact_id": facts.get("artifact_id"),
        "provider_id": provider,
        "parent_account_id": facts.get("parent_account_id"),
    }
    closure = facts.get("closure")
    if isinstance(closure, Mapping):
        account = closure.get("account")
        if isinstance(account, Mapping):
            cash = account.get("cash")
            dividends = account.get("dividends")
            summary["account_cash"] = dict(cash) if isinstance(cash, Mapping) else None
            summary["dividends"] = (
                {
                    key: dividends.get(key)
                    for key in ("total", "attributed_to_sleeves", "unattributed")
                }
                if isinstance(dividends, Mapping)
                else None
            )
    if isinstance(facts.get("open_items"), Mapping):
        summary["open_items"] = dict(facts["open_items"])
    policy = facts.get("closure_policy")
    if isinstance(policy, Mapping):
        summary["closure_policy"] = dict(policy)
    disclosures = facts.get("disclosures")
    if isinstance(disclosures, Mapping) and facts.get("artifact_id") == ACCOUNT_FACTS:
        summary["disclosure_counts"] = {
            key: len(value)
            for key, value in sorted(disclosures.items())
            if isinstance(value, list)
        }
        dividends = disclosures.get("unallocated_dividends")
        if isinstance(dividends, list):
            summary["unallocated_dividends"] = {
                "total": str(
                    sum((_dec(item["amount"]) for item in dividends), Decimal(0))
                ),
                "days": len(dividends),
            }
    flows = facts.get("flows")
    inflow: dict[str, dict[str, Decimal]] = {}
    for flow in flows if isinstance(flows, list) else []:
        if isinstance(flow, Mapping) and flow.get("class") == "INTERNAL_IN":
            per_day = inflow.setdefault(str(flow.get("account_id")), {})
            day = str(flow.get("event_date"))
            per_day[day] = per_day.get(day, Decimal(0)) + _dec(flow.get("amount"))
    summary["internal_in_by_day"] = {
        account: {day: str(total) for day, total in sorted(days.items())}
        for account, days in sorted(inflow.items())
    }
    by_source: dict[str, dict[str, dict[str, Decimal]]] = {}
    for flow in flows if isinstance(flows, list) else []:
        if isinstance(flow, Mapping) and flow.get("class") == "INTERNAL_IN":
            source = by_source.setdefault(str(flow.get("account_id")), {}).setdefault(
                str(flow.get("event_date")), {}
            )
            counterparty = str(flow.get("counterparty_account_id"))
            source[counterparty] = source.get(counterparty, Decimal(0)) + _dec(
                flow.get("amount")
            )
    summary["internal_in_by_source"] = {
        account: {
            day: {cp: str(total) for cp, total in sorted(parts.items())}
            for day, parts in sorted(days.items())
        }
        for account, days in sorted(by_source.items())
    }
    if facts.get("artifact_id") == ACCOUNT_FACTS:
        summary.update(_sleeve_perimeter(facts))
    return summary


def _sleeve_perimeter(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Per sleeve the gain from its first-day end-of-day NAV and its entry-day difference.

    A sleeve's first day carries the difference between its end-of-day NAV and the amount
    allocated to it; the gain is every later day.  The facts' daily closure ties both to
    the account: the closure's sleeve column must equal the sum of the two, or the facts
    are inconsistent and the preparation is refused.
    """

    sleeves: list[dict[str, str]] = []
    own_total = Decimal(0)
    for sleeve in facts.get("sleeves", []):
        days = sleeve.get("days") or []
        if not days:
            continue
        daily = [_dec(day["own_pnl"]) + _dec(day["alloc_total"]) for day in days]
        own_total += sum(daily, Decimal(0))
        sleeves.append(
            {
                "sleeve_id": str(sleeve["sleeve_id"]),
                "account_id": str(sleeve["account_id"]),
                "strategy_id": str(sleeve["strategy_id"]),
                "first_day": str(days[0]["day"]),
                "last_day": str(days[-1]["day"]),
                "entry_day_difference": str(daily[0]),
                "gain_from_first_day_nav": str(sum(daily[1:], Decimal(0))),
            }
        )
    closure = facts.get("closure") or []
    totals = {
        key: sum((_dec(day[key]) for day in closure), Decimal(0))
        for key in (
            "account_pnl",
            "sleeves_own_plus_alloc",
            "residual_and_post_exit",
            "unexplained",
        )
    }
    if closure and abs(totals["sleeves_own_plus_alloc"] - own_total) > Decimal("0.005"):
        raise DisclosureError(
            "the sleeve facts' daily closure does not equal the sleeves' own P&L"
        )
    window = facts.get("window") or {}
    bindings = facts.get("bindings") or {}
    return {
        "perimeter_sleeves": sleeves,
        "closure_totals": {key: str(value) for key, value in totals.items()},
        "closure_window": {
            "first_day": window.get("first_day"),
            "last_day": window.get("last_day"),
        },
        "parent_ledger_generation_id": bindings.get("parent_ledger_generation_id"),
    }


def _account_reconciliation(
    summary: Mapping[str, Any], ledger: Mapping[str, Any]
) -> dict[str, Any]:
    """The parent account's gain against the sleeves', every figure from the ledger.

    Capital paid in is the opening capital anchor's NAV plus every flow recognised after
    the anchor's day up to the closure window's last day.  The account's gain is its NAV on
    that day less that capital.  It must equal the sleeves' gains and entry-day differences
    plus an unattributed remainder that is measured a second way (the days before the first
    sleeve day, then the facts' own residual columns); the build fails if the two differ.
    """

    parent = str(summary["parent_account_id"])
    window = summary["closure_window"]
    last_day = str(window["last_day"])
    first_day = str(window["first_day"])
    anchors = [
        item
        for item in ledger.get("capital_anchors", [])
        if item.get("account_id") == parent
        and item.get("capital_type") == "OPENING_CAPITAL_ANCHOR"
    ]
    if len(anchors) != 1:
        raise DisclosureError("the parent account has no unique opening capital anchor")
    anchor = anchors[0]
    if anchor.get("generation_id") != summary.get("parent_ledger_generation_id"):
        raise DisclosureError("the sleeve facts bind a different ledger generation")
    opening = _dec(anchor["opening_nav"]["value"])
    anchor_day = str(anchor["opening_nav_accounting_date"])
    nav = {
        str(item["as_of_date"]): _dec(item["nav"]["value"])
        for item in ledger.get("nav_observations", [])
        if item.get("account_id") == parent
    }
    flows = [
        (str(item["nav_recognition_date"]), _dec(item["amount"]["value"]))
        for item in ledger.get("flows", [])
        if item.get("account_id") == parent
        and anchor_day < str(item["nav_recognition_date"]) <= last_day
    ]
    if last_day not in nav:
        raise DisclosureError("the parent account has no NAV on the window's last day")
    # capital paid in is the inflows; withdrawals are kept apart, so a comparison with the
    # capital an account was given is never lowered by money that later left it
    paid_in = opening + sum((a for _day, a in flows if a > 0), Decimal(0))
    withdrawn = -sum((a for _day, a in flows if a < 0), Decimal(0))
    account_gain = nav[last_day] - (paid_in - withdrawn)
    sleeves = summary["perimeter_sleeves"]
    gains = sum((_dec(s["gain_from_first_day_nav"]) for s in sleeves), Decimal(0))
    entries = sum((_dec(s["entry_day_difference"]) for s in sleeves), Decimal(0))
    unattributed = account_gain - gains - entries
    totals = summary["closure_totals"]
    # the second measurement: the days before the first sleeve day, from the ledger's own
    # NAV, plus the facts' residual columns
    before = [day for day in nav if day < first_day]
    if not before:
        raise DisclosureError(
            "the parent account has no NAV before the first sleeve day"
        )
    previous = max(before)
    pre_sleeve = (
        nav[previous]
        - opening
        - sum((a for d, a in flows if d <= previous), Decimal(0))
    )
    independent = (
        pre_sleeve
        + _dec(totals["residual_and_post_exit"])
        + _dec(totals["unexplained"])
    )
    if abs(unattributed - independent) > Decimal("0.005"):
        raise DisclosureError(
            "the sleeves, the entry-day differences and the unattributed remainder "
            "do not sum to the parent account's gain"
        )
    return {
        "parent_account_id": parent,
        "ledger_generation_id": anchor["generation_id"],
        "anchor_day": anchor_day,
        "last_day": last_day,
        "opening_nav": str(opening),
        "paid_in_after_anchor": str(paid_in - opening),
        "withdrawn_after_anchor": str(withdrawn),
        "paid_in": str(paid_in),
        "terminal_nav": str(nav[last_day]),
        "account_gain": str(account_gain),
        "sleeve_gain_total": str(gains),
        "entry_day_total": str(entries),
        "unattributed": str(unattributed),
        "unattributed_pre_sleeve_days": str(pre_sleeve),
        "sleeves": sleeves,
    }


def _margin_funding_events(declared: Mapping[str, Any]) -> list[dict[str, str]]:
    """The declared funding events whose note or evidence names account margin."""

    events: list[dict[str, str]] = []
    funding = declared.get("funding_events")
    for sleeve, days in sorted(funding.items() if isinstance(funding, Mapping) else []):
        for day, event in sorted(days.items() if isinstance(days, Mapping) else []):
            if not isinstance(event, Mapping):
                continue
            text = " ".join(
                str(event.get(key) or "") for key in ("note", "evidence_ref")
            )
            if "margin" in text.lower():
                events.append(
                    {
                        "sleeve_id": str(sleeve),
                        "day": str(day),
                        "kind": str(event.get("kind")),
                        "note": str(event.get("note") or ""),
                    }
                )
    return events


def _exit_events(declared: Mapping[str, Any]) -> list[dict[str, str]]:
    """The declared funding events that close a sleeve (its proceeds go elsewhere)."""

    funding = declared.get("funding_events")
    return [
        {"sleeve_id": str(sleeve), "day": str(day)}
        for sleeve, days in sorted(
            funding.items() if isinstance(funding, Mapping) else []
        )
        for day, event in sorted(days.items() if isinstance(days, Mapping) else [])
        if isinstance(event, Mapping) and event.get("kind") == "EXIT"
    ]


def _pointer(document: Any, pointer: str) -> Any:
    node = document
    for part in pointer.lstrip("/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _resolve_predecessor(
    entry: Mapping[str, Any], data_root: Path | None
) -> dict[str, Any]:
    release = {
        "release_id": entry.get("release_id"),
        "panel_generation_id": entry.get("panel_generation_id"),
    }
    data_file = entry.get("data_file")
    if not isinstance(data_file, Mapping):
        return {"status": "OMITTED", "reason": "NO_DATA_FILE_NAMED", **release}
    if data_root is None:
        return {
            "status": "OMITTED",
            "reason": "PREDECESSOR_DATA_ROOT_NOT_SUPPLIED",
            **release,
        }
    path = data_root / str(data_file.get("relative_path"))
    if not path.is_file():
        return {
            "status": "OMITTED",
            "reason": "PREDECESSOR_GENERATION_NOT_REACHABLE",
            **release,
        }
    sha256, _size = _sha(path)
    if sha256 != data_file.get("sha256"):
        return {
            "status": "OMITTED",
            "reason": "PREDECESSOR_GENERATION_HASH_DIFFERS",
            **release,
        }
    document = json.loads(path.read_bytes())
    pointers = entry.get("pointers")
    if not isinstance(pointers, Mapping):
        return {"status": "OMITTED", "reason": "NO_POINTERS_NAMED", **release}
    values: dict[str, str] = {}
    try:
        by_strategy = _pointer(document, str(pointers["strategy_capital_list"]))
        wanted = str(pointers["strategy_id"])
        values["strategy_capital_usd"] = str(
            next(
                item[str(pointers["strategy_capital_field"])]
                for item in by_strategy
                if item.get("strategy_id") == wanted
            )
        )
        values["strategy_id"] = wanted
        values["aggregate_capital_usd"] = str(
            _pointer(document, str(pointers["aggregate"]))
        )
        if "hwm_current_index" in pointers:
            values["hwm_current_index"] = str(
                _pointer(document, str(pointers["hwm_current_index"]))
            )
    except (KeyError, IndexError, ValueError, StopIteration, TypeError):
        return {"status": "OMITTED", "reason": "POINTER_DOES_NOT_RESOLVE", **release}
    return {
        "status": "RESOLVED",
        **release,
        "data_file_sha256": sha256,
        "pointers": dict(pointers),
        "values": values,
    }


def build_sources(
    *,
    facts_paths: Sequence[Path],
    inputs_path: Path | None,
    predecessor_data_root: Path | None,
    declared_inputs_path: Path | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """The preparation-time document: every value read from pinned bytes."""

    facts = [
        _facts_summary(_object(path.read_bytes(), f"facts {path.name}"))
        for path in facts_paths
    ]
    sources: dict[str, Any] = {
        "schema_version": 1,
        "artifact_id": "sleeve-disclosure-sources",
        "schema": SOURCES_SCHEMA,
        "facts": sorted(facts, key=lambda item: str(item["artifact_id"])),
        "margin_funding_events": (
            _margin_funding_events(
                _object(declared_inputs_path.read_bytes(), "declared sleeve inputs")
            )
            if declared_inputs_path is not None
            else []
        ),
        "sleeve_exit_events": (
            _exit_events(
                _object(declared_inputs_path.read_bytes(), "declared sleeve inputs")
            )
            if declared_inputs_path is not None
            else []
        ),
        "account_reconciliation": [],
        "timestamp_bounds": None,
        "predecessor": None,
    }
    if ledger_path is not None:
        ledger = _object(ledger_path.read_bytes(), "provider ledger")
        sources["account_reconciliation"] = [
            _account_reconciliation(item, ledger)
            for item in sources["facts"]
            if item.get("perimeter_sleeves")
        ]
    if inputs_path is None:
        return sources
    inputs = _object(inputs_path.read_bytes(), "disclosure inputs")
    if (
        inputs.get("artifact_id") != INPUTS_ARTIFACT_ID
        or inputs.get("schema_version") != 1
    ):
        raise DisclosureError("disclosure inputs identity differs")
    stamp = inputs.get("timestamp_bounds")
    if isinstance(stamp, Mapping):
        evidence = stamp.get("evidence")
        if not isinstance(evidence, Mapping):
            raise DisclosureError("timestamp bounds name no evidence file")
        evidence_path = inputs_path.parent / str(evidence.get("path"))
        sha256, size = _sha(evidence_path)
        if sha256 != evidence.get("sha256") or size != evidence.get("size_bytes"):
            raise DisclosureError("timestamp evidence file differs from its pin")
        parsed = parse_timestamp_evidence(evidence_path.read_text(encoding="utf-8"))
        entries: list[dict[str, Any]] = []
        for item in stamp.get("entries", []):
            key = (str(item["strategy_id"]), str(item["account_id"]))
            block = parsed.get(key)
            entries.append(
                {"strategy_id": key[0], "account_id": key[1], **(block or {})}
                if block is not None and "committed_twr_pct" in block
                else {
                    "strategy_id": key[0],
                    "account_id": key[1],
                    "omitted": "NOT_IN_EVIDENCE_FILE",
                }
            )
        sources["timestamp_bounds"] = {
            "evidence": {
                "path": str(evidence.get("path")),
                "sha256": sha256,
                "size_bytes": size,
            },
            "entries": entries,
        }
    predecessor = inputs.get("predecessor_comparison")
    if isinstance(predecessor, Mapping):
        sources["predecessor"] = _resolve_predecessor(
            predecessor, predecessor_data_root
        )
    return sources


# ------------------------------------------------------------------ report step


def _pct(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _signed_pt(text: str) -> str:
    value = _dec(text)
    return f"{value:+.2f}"


def _sleeve_label(account: str) -> str:
    return (
        "no sleeve" if account.endswith("-unallocated") else account.rsplit("-", 1)[-1]
    )


def _margin_allocation(
    event: Mapping[str, str], by_source: Mapping[str, Mapping[str, str]]
) -> str:
    """One declared margin allocation, split by where its amount came from."""

    parts = by_source.get(event["day"], {})
    total = sum((_dec(value) for value in parts.values()), Decimal(0))
    split = [f"{_usd(_dec(v))} from {_sleeve_label(cp)}" for cp, v in parts.items()]
    head = f"{event['day']} {_usd(total)}" if parts else event["day"]
    if len(parts) == 1:
        (only,) = parts
        where = f" from {_sleeve_label(only)}"
    else:
        where = f": {' and '.join(split)}" if split else ""
    return f"{head}{where} (declared: {event['note']})"


def _exit_proceeds(
    sources: Mapping[str, Any],
    *,
    product_sleeves: Mapping[str, str],
    by_source: Mapping[str, Mapping[str, Mapping[str, str]]],
    margin_days: set[tuple[str, str]],
) -> str:
    """Allocations that are the proceeds of a closing sleeve whose entries were margin-funded."""

    exits = {(e["sleeve_id"], e["day"]) for e in sources.get("sleeve_exit_events", [])}
    margin_entries: dict[str, list[tuple[str, str]]] = {}
    inflow_by_account = {
        account: {
            day: sum((_dec(v) for v in parts.values()), Decimal(0))
            for day, parts in days.items()
        }
        for account, days in by_source.items()
    }
    sleeve_account = {a.rsplit("-", 1)[-1]: a for a in by_source}
    for event in sources.get("margin_funding_events", []):
        if event["kind"] == "IN":
            amount = inflow_by_account.get(
                sleeve_account.get(event["sleeve_id"], ""), {}
            ).get(event["day"])
            if amount is not None:
                margin_entries.setdefault(event["sleeve_id"], []).append(
                    (event["day"], _usd(amount))
                )
    sentences: list[str] = []
    for sleeve_id, account in sorted(product_sleeves.items()):
        for day, parts in sorted(by_source.get(account, {}).items()):
            if (sleeve_id, day) in margin_days:
                continue
            closing = {
                cp: _dec(v)
                for cp, v in parts.items()
                if (_sleeve_label(cp), day) in exits
                and _sleeve_label(cp) in margin_entries
            }
            if not closing:
                continue
            total = sum((_dec(v) for v in parts.values()), Decimal(0))
            proceeds = " and ".join(
                f"{_sleeve_label(cp)} ({_usd(v)})" for cp, v in sorted(closing.items())
            )
            entries = "; ".join(
                f"{_sleeve_label(cp)} {amount} on {entry_day}"
                for cp in sorted(closing)
                for entry_day, amount in margin_entries[_sleeve_label(cp)]
            )
            sentences.append(
                f" The {day} allocation into {sleeve_id} ({_usd(total)}) is the proceeds of "
                f"{proceeds}, whose entries ({entries}) are declared account-margin-funded."
            )
    return "".join(sentences)


def _perimeter_reconciliation(
    reconciliation: Mapping[str, Any], excluded: Sequence[Mapping[str, Any]]
) -> str:
    """What sits between the parent account's gain and the sleeves' gains."""

    by_account = {s["account_id"]: s for s in reconciliation["sleeves"]}
    for row in excluded:
        known = by_account.get(str(row["account_id"]))
        if known is None or abs(
            _dec(known["gain_from_first_day_nav"]) - _dec(row["gain"])
        ) > Decimal("0.01"):
            raise DisclosureError(
                f"the report row gain of {row['account_id']} differs from the sleeve facts"
            )
    entries = ", ".join(
        f"{s['sleeve_id']} {_dec(s['entry_day_difference']):+,.2f}"
        for s in sorted(reconciliation["sleeves"], key=lambda s: s["sleeve_id"])
    )
    entry_total = _dec(reconciliation["entry_day_total"]).quantize(_CENT)
    gain = _dec(reconciliation["account_gain"]).quantize(_CENT)
    sleeves = _dec(reconciliation["sleeve_gain_total"]).quantize(_CENT)
    # shown figures close: the remainder is the shown difference less the shown entry-day
    # differences (the unrounded remainder is within one cent of it)
    difference = gain - sleeves
    remainder = difference - entry_total
    if abs(remainder - _dec(reconciliation["unattributed"])) >= _CENT:
        raise DisclosureError("the shown perimeter figures do not close")
    later = _dec(reconciliation["paid_in_after_anchor"])
    withdrawn = _dec(reconciliation.get("withdrawn_after_anchor", "0"))
    left = f", {_usd(withdrawn)} withdrawn" if withdrawn else ""
    return (
        " Sleeve gains are measured from each sleeve's first-day end-of-day NAV, not from "
        f"the amount allocated to it; the entry-day differences ({entries}; total "
        f"{entry_total:+,.2f}) belong to no sleeve. Parent account gain over "
        f"{reconciliation['anchor_day']}..{reconciliation['last_day']} is {gain:+,.2f} on "
        f"{_usd(_dec(reconciliation['paid_in']))} of external capital (opening NAV "
        f"{_usd(_dec(reconciliation['opening_nav']))} plus {_usd(later)} paid in later{left}); "
        f"the sleeves sum to {sleeves:+,.2f}; the {difference:+,.2f} difference is the "
        f"entry-day differences ({entry_total:+,.2f}) and {remainder:+,.2f} not attributed "
        "(days before the first sleeve day and the parent remainder)."
    )


def _index_move(predecessor: object, current: str | None) -> str:
    if (
        current is not None
        and isinstance(predecessor, Mapping)
        and predecessor.get("status") == "RESOLVED"
        and "hwm_current_index" in predecessor["values"]
    ):
        before = _dec(predecessor["values"]["hwm_current_index"])
        now = _dec(current)
        return (
            f"The index is {now:.4f} against {before:.4f} in the previous release "
            f"({now - before:+.2f}, {((now - before) / before * 100):+.2f}%); the move is "
            "not attributed beyond this timing."
        )
    return "Its movement against the previous release is not attributed beyond this timing."


def compose(
    sources: Mapping[str, Any],
    *,
    rows: Sequence[Mapping[str, Any]],
    invested_capital: Mapping[str, Any] | None,
    sleeve_accounts: Mapping[str, str],
    product_strategies: Sequence[str],
    strategy_returns: Mapping[str, Decimal] | None = None,
    hwm_current_index: str | None = None,
) -> dict[str, Any]:
    """Methodology items from the sources and the report's own rows.

    ``sleeve_accounts`` maps each derived sleeve account to its parent account;
    ``product_strategies`` are the strategies whose product epoch is a derived sleeve.
    """

    items: list[dict[str, str]] = []
    omitted: list[dict[str, str]] = []
    facts = {str(item.get("artifact_id")): item for item in sources.get("facts", [])}

    stamp = sources.get("timestamp_bounds")
    if isinstance(stamp, Mapping):
        evidence = stamp["evidence"]
        parts: list[str] = []
        for entry in stamp.get("entries", []):
            label = f"{entry['strategy_id']} on {entry['account_id']}"
            if "omitted" in entry:
                omitted.append(
                    {
                        "entry": f"timestamp bound {label}",
                        "reason": str(entry["omitted"]),
                    }
                )
                continue
            row = next(
                (
                    r
                    for r in rows
                    if r.get("account_id") == entry["account_id"]
                    and r.get("strategy_id") == entry["strategy_id"]
                    and r.get("end") is not None
                    and r.get("observation_linked_return") is not None
                ),
                None,
            )
            twr = None if row is None else _dec(row["observation_linked_return"]) * 100
            basis_note = ""
            if row is None and entry["account_id"] in set(sleeve_accounts.values()):
                # the epoch of a residual parent is replaced by its sleeves: the
                # strategy's own account-live return is that parent account's series
                own = (strategy_returns or {}).get(entry["strategy_id"])
                twr = None if own is None else own * 100
                basis_note = ", the strategy's account-live series"
            if row is None and twr is None:
                omitted.append(
                    {
                        "entry": f"timestamp bound {label}",
                        "reason": "NO_REPORT_ROW_FOR_THE_MEASURED_EPOCH",
                    }
                )
                continue
            if twr is None or abs(twr - _dec(entry["committed_twr_pct"])) > Decimal(
                "0.0001"
            ):
                omitted.append(
                    {
                        "entry": f"timestamp bound {label}",
                        "reason": "COMMITTED_RETURN_DIFFERS_FROM_REPORT_ROW",
                    }
                )
                continue
            every_bod = (
                f"; placing every flow at beginning of day moves it by "
                f"{_signed_pt(entry['all_bod_delta_pt'])} points"
                if "all_bod_delta_pt" in entry
                else ""
            )
            parts.append(
                f"{label} (cumulative TWR {_pct(twr)}%{basis_note}): placing each flow "
                "at the beginning or the end of its day, independently, moves it by "
                f"between {_signed_pt(entry['b_all_lower_pt'])} and "
                f"{_signed_pt(entry['b_all_upper_pt'])} points (external flows only: "
                f"{_signed_pt(entry['b_ext_lower_pt'])} / "
                f"{_signed_pt(entry['b_ext_upper_pt'])}){every_bod}"
            )
        if parts:
            items.append(
                {
                    "item": "Flow timing bounds",
                    "value": (
                        "Date-only flows are valued at end of day and carry "
                        "DATE_ONLY_END_OF_DAY_INFERRED_TIMING. Measured on the epochs named, "
                        "the return depends on flow timing as follows: "
                        + "; ".join(parts)
                        + f". Source: {evidence['path']} (sha256 {evidence['sha256'][:12]}). "
                        "Epochs derived from sleeves are not covered by these bounds; "
                        "exact-timestamp TWR is acceptance item #535."
                    ),
                }
            )

    quantbase = facts.get(FUND_FACTS)
    if quantbase is not None and isinstance(quantbase.get("account_cash"), Mapping):
        cash = quantbase["account_cash"]
        open_items = quantbase.get("open_items") or {}
        funding = open_items.get("funding_attribution") or {}
        dividend = open_items.get("dividend_nav_treatment") or {}
        sleeves = (quantbase.get("dividends") or {}).get("attributed_to_sleeves") or {}
        income = sum((_dec(value) for value in sleeves.values()), Decimal(0))
        items.append(
            {
                "item": "Quantbase account cash",
                "value": (
                    f"Derived account cash {_usd(_dec(cash['derived_cash']))} against provider "
                    f"settled cash {_usd(_dec(cash['provider_settled_cash']))}: residual "
                    f"{_usd(_dec(cash['residual']))} (declared {_usd(_dec(cash['declared_residual']))}, "
                    f"tolerance {_dec(cash['residual_tolerance'])}); the activity does not explain it. "
                    f"Funding attribution of fund purchases is {funding.get('evidence_class')} "
                    f"(treatment {funding.get('treatment')}, "
                    f"{funding.get('unallocated_remainder_pairs')} pairs). Dividends attributed "
                    f"to the retired funds total {_usd(income)}; their NAV treatment is "
                    f"{dividend.get('evidence_class')} ({dividend.get('treatment')})."
                ),
            }
        )

    ibkr = facts.get(ACCOUNT_FACTS)
    if ibkr is not None and isinstance(ibkr.get("closure_policy"), Mapping):
        policy = ibkr["closure_policy"]
        counts = ibkr.get("disclosure_counts") or {}
        unallocated = ibkr.get("unallocated_dividends") or {}
        items.append(
            {
                "item": "Sleeve closure",
                "value": (
                    f"Maximum daily unexplained {_dec(policy['max_abs_unexplained']):.4f} and "
                    f"window unexplained {_dec(policy['window_unexplained']):.4f} against a "
                    f"tolerance of {_dec(policy['tolerance_per_day'])} per day and "
                    f"{_dec(policy['tolerance_window'])} over the window; "
                    f"{counts.get('manual_assignments', 0)} manual assignments, "
                    f"{counts.get('reconciliation_gaps', 0)} reconciliation gaps, "
                    f"{counts.get('reconciliation_scalings', 0)} scalings, "
                    f"{counts.get('offsetting_residues', 0)} offsetting residue, and "
                    f"{_usd(_dec(unallocated.get('total', '0')))} of unallocated dividends over "
                    f"{unallocated.get('days', 0)} days."
                ),
            }
        )

    if invested_capital is not None and product_strategies:
        capital = {
            str(item.get("strategy_id")): _dec(item["invested_capital_usd"])
            for item in invested_capital.get("current_by_strategy", [])
            if isinstance(item, Mapping)
        }
        total = _dec(invested_capital["current_aggregate_usd"])
        for strategy in product_strategies:
            if strategy not in capital:
                continue
            product_accounts = {
                str(r.get("account_id"))
                for r in rows
                if r.get("strategy_id") == strategy
                and str(r.get("account_id")) in sleeve_accounts
            }
            product_sleeves = {
                account.rsplit("-", 1)[-1]: account for account in product_accounts
            }
            by_source = {
                account: days
                for item in facts.values()
                for account, days in (item.get("internal_in_by_source") or {}).items()
            }
            margin = [
                event
                for event in sources.get("margin_funding_events", [])
                if event["sleeve_id"] in product_sleeves and event["kind"] == "IN"
            ]
            text = (
                f"Governed capital is measured gross: {strategy} is {_usd(capital[strategy])} "
                f"of {_usd(total)} ({(capital[strategy] / total * 100).quantize(Decimal('0.1'))}%), "
                "the sum of the allocations into its sleeve, margin-funded allocations included "
                "(not netted)."
            )
            margin_days: set[tuple[str, str]] = set()
            if margin:
                listed = "; ".join(
                    _margin_allocation(
                        event,
                        by_source.get(product_sleeves[event["sleeve_id"]], {}),
                    )
                    for event in margin
                )
                margin_days = {(e["sleeve_id"], e["day"]) for e in margin}
                text += (
                    " Allocations whose declared funding events name account margin "
                    f"({len(margin)}): {listed}."
                )
            text += _exit_proceeds(
                sources,
                product_sleeves=product_sleeves,
                by_source=by_source,
                margin_days=margin_days,
            )
            parents = sorted({sleeve_accounts[a] for a in product_accounts})
            known = {
                item["parent_account_id"]: item
                for item in sources.get("account_reconciliation", [])
            }
            for parent in parents:
                if parent not in known:
                    omitted.append(
                        {
                            "entry": f"capital paid into {parent}",
                            "reason": "ACCOUNT_RECONCILIATION_NOT_IN_SOURCES",
                        }
                    )
            if parents and all(parent in known for parent in parents):
                paid_in = sum(
                    (_dec(known[parent]["paid_in"]) for parent in parents), Decimal(0)
                )
                if capital[strategy] > paid_in:
                    text += (
                        f" Gross capital {_usd(capital[strategy])} exceeds all external "
                        f"capital paid into the account ({_usd(paid_in)}) by "
                        f"{_usd(capital[strategy] - paid_in)}."
                    )
            predecessor = sources.get("predecessor")
            if isinstance(predecessor, Mapping):
                if predecessor.get("status") == "RESOLVED":
                    values = predecessor["values"]
                    if values.get("strategy_id") == strategy:
                        before = _dec(values["strategy_capital_usd"])
                        before_total = _dec(values["aggregate_capital_usd"])
                        delta = capital[strategy] - before
                        text += (
                            f" Previous release {str(predecessor['release_id'])[:8]} "
                            f"(panel generation {str(predecessor['panel_generation_id'])[:8]}): "
                            f"{_usd(before)} for {strategy} ({delta:+,.2f}, "
                            f"{(delta / before * 100).quantize(Decimal('0.1')):+}%) "
                            f"and {_usd(before_total)} in total ({total - before_total:+,.2f})."
                        )
                else:
                    omitted.append(
                        {
                            "entry": "predecessor capital comparison",
                            "reason": str(predecessor.get("reason")),
                        }
                    )
            items.append({"item": "Sleeve capital basis", "value": text})

    if product_strategies:
        product_parents = {
            sleeve_accounts[str(r.get("account_id"))]
            for r in rows
            if r.get("strategy_id") in product_strategies
            and str(r.get("account_id")) in sleeve_accounts
        }
        retained = [
            r
            for r in rows
            if str(r.get("account_id")) in sleeve_accounts
            and sleeve_accounts[str(r["account_id"])] in product_parents
            and r.get("strategy_id") not in product_strategies
            and r.get("status") == "OK"
        ]
        by_parent: dict[str, list[Mapping[str, Any]]] = {}
        for r in retained:
            by_parent.setdefault(sleeve_accounts[str(r["account_id"])], []).append(r)
        for parent in sorted(by_parent):
            group = by_parent[parent]
            total_gain = sum((_dec(r["gain"]) for r in group), Decimal(0))
            lines = ", ".join(
                f"{str(r['account_id']).rsplit('-', 1)[-1]} {_dec(r['gain']):+,.2f}"
                for r in sorted(group, key=lambda r: str(r["account_id"]))
            )
            text = (
                f"The product epoch of {', '.join(product_strategies)} is its derived "
                f"sleeve only. Sleeves of the same parent account kept as history, "
                f"outside the product: gain {total_gain:+,.2f} USD in total ({lines})."
            )
            reconciliation = next(
                (
                    item
                    for item in sources.get("account_reconciliation", [])
                    if item["parent_account_id"] == parent
                ),
                None,
            )
            if reconciliation is not None:
                text += _perimeter_reconciliation(reconciliation, group)
            else:
                omitted.append(
                    {
                        "entry": f"perimeter reconciliation of {parent}",
                        "reason": "ACCOUNT_RECONCILIATION_NOT_IN_SOURCES",
                    }
                )
            items.append({"item": "Product perimeter", "value": text})
    for strategy in product_strategies:
        epochs = sorted(
            (str(r.get("effective_start")), str(r.get("account_id")))
            for r in rows
            if r.get("strategy_id") == strategy
            and r.get("effective_end") is None
            and str(r.get("account_id")) in sleeve_accounts
        )
        if epochs:
            items.append(
                {
                    "item": "HWM member series",
                    "value": (
                        f"The governed HWM member for {strategy} is the sleeve series "
                        f"{epochs[0][1]} from {epochs[0][0]}; the index carries this member from "
                        "that date. "
                        + _index_move(sources.get("predecessor"), hwm_current_index)
                    ),
                }
            )
    return {"items": items, "omitted": omitted}

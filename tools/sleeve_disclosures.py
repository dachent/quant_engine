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
    if isinstance(disclosures, Mapping) and provider == "ibkr":
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
    margin: list[dict[str, str]] = []
    for flow in flows if isinstance(flows, list) else []:
        trace = flow.get("trace") if isinstance(flow, Mapping) else None
        refs = trace.get("evidence_refs") if isinstance(trace, Mapping) else None
        if isinstance(refs, list) and any(
            isinstance(ref, str) and "margin" in ref.lower() for ref in refs
        ):
            margin.append(
                {
                    "account_id": str(flow.get("account_id")),
                    "class": str(flow.get("class")),
                    "event_date": str(flow.get("event_date")),
                    "amount": str(flow.get("amount")),
                    "pair_id": str(flow.get("pair_id")),
                }
            )
    summary["margin_funded_flows"] = sorted(
        margin, key=lambda item: (item["event_date"], item["pair_id"], item["class"])
    )
    return summary


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
        "facts": sorted(facts, key=lambda item: str(item["provider_id"])),
        "timestamp_bounds": None,
        "predecessor": None,
    }
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


def compose(
    sources: Mapping[str, Any],
    *,
    rows: Sequence[Mapping[str, Any]],
    invested_capital: Mapping[str, Any] | None,
    sleeve_accounts: Mapping[str, str],
    product_strategies: Sequence[str],
) -> dict[str, Any]:
    """Methodology items from the sources and the report's own rows.

    ``sleeve_accounts`` maps each derived sleeve account to its parent account;
    ``product_strategies`` are the strategies whose product epoch is a derived sleeve.
    """

    items: list[dict[str, str]] = []
    omitted: list[dict[str, str]] = []
    facts = {str(item.get("provider_id")): item for item in sources.get("facts", [])}

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
            parts.append(
                f"{label} (cumulative TWR {_pct(twr)}%): beginning-of-day placement of every "
                f"flow moves it by between {_signed_pt(entry['b_all_lower_pt'])} and "
                f"{_signed_pt(entry['b_all_upper_pt'])} points (external flows only: "
                f"{_signed_pt(entry['b_ext_lower_pt'])} / {_signed_pt(entry['b_ext_upper_pt'])})"
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

    quantbase = facts.get("quantbase")
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

    ibkr = facts.get("ibkr")
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
            margin = [
                flow
                for item in facts.values()
                for flow in item.get("margin_funded_flows", [])
                if flow["account_id"] in product_accounts
                and flow["class"] == "INTERNAL_IN"
            ]
            text = (
                f"Governed capital is measured gross: {strategy} is {_usd(capital[strategy])} "
                f"of {_usd(total)} ({(capital[strategy] / total * 100).quantize(Decimal('0.1'))}%), "
                "the sum of the allocations into its sleeve, margin-funded allocations included "
                "(not netted)."
            )
            if margin:
                listed = ", ".join(
                    f"{flow['event_date']} {_usd(_dec(flow['amount']))}"
                    for flow in margin
                )
                text += (
                    f" Allocations whose declared evidence names account margin as funding "
                    f"({len(margin)}): {listed}; the margin-funded part of each is stated "
                    "only in the evidence note."
                )
            predecessor = sources.get("predecessor")
            if isinstance(predecessor, Mapping):
                if predecessor.get("status") == "RESOLVED":
                    values = predecessor["values"]
                    if values.get("strategy_id") == strategy:
                        before = _dec(values["strategy_capital_usd"])
                        before_total = _dec(values["aggregate_capital_usd"])
                        text += (
                            f" Previous release ({str(predecessor['panel_generation_id'])[:8]}): "
                            f"{_usd(before)} for {strategy} ({_usd(capital[strategy] - before)}, "
                            f"{((capital[strategy] - before) / before * 100).quantize(Decimal('0.1'))}%) "
                            f"and {_usd(before_total)} in total ({_usd(total - before_total)})."
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
            items.append(
                {
                    "item": "Product perimeter",
                    "value": (
                        f"The product epoch of {', '.join(product_strategies)} is its derived "
                        f"sleeve only. Sleeves of the same parent account kept as history, "
                        f"outside the product: gain {total_gain:+,.2f} USD in total ({lines})."
                    ),
                }
            )
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
                        "that date. Its movement against the previous release is not attributed "
                        "beyond this timing."
                    ),
                }
            )
    return {"items": items, "omitted": omitted}

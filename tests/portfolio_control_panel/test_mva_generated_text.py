"""model_vs_actual statements that count things are generated from the block's own rows.

Synthetic audit input only (``test_model_vs_actual._audit``): no real figure appears here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from test_model_vs_actual import _audit, _build, _errors, _panel

import portfolio_model_vs_actual as mva

AUDIT_DERIVED = "AUDIT_DERIVED"


def _bridge_rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for strategy in block["strategies"]
        for window in [*strategy["windows"], *strategy["epochs"]]
        for row in window["bridge"]
    ]


def _counts(block: dict[str, Any]) -> tuple[int, int]:
    rows = [r for r in _bridge_rows(block) if r["evidence_tag"] == AUDIT_DERIVED]
    named = sum(1 for r in rows if r["replacing_pull"] is not None)
    return named, len(rows) - named


def _definition(block: dict[str, Any]) -> str:
    return next(
        t["definition"] for t in block["evidence_tags"] if t["tag"] == AUDIT_DERIVED
    )


def test_the_definition_states_the_pull_counts_of_the_rows_it_describes() -> None:
    block, _raw, _sha = _build()
    named, unnamed = _counts(block)
    assert unnamed > 0 and named > 0, "fixture must contain both kinds of row"
    text = _definition(block)
    assert (
        f"{named} of {named + unnamed} AUDIT_DERIVED bridge rows name a replacing pull"
        in text
    )
    assert f"{unnamed} name none" in text
    assert "Each row names the pull" not in text
    note = block["notes"][0]
    assert f"The {named} bridge rows that name a pull" in note
    assert f"the {unnamed} that name none" in note
    assert "replaced when the named pull is admitted" not in note


def test_no_statement_about_pulls_contradicts_the_row_counts() -> None:
    """Every "N ... name(s) a pull / none" number in the block's text equals the payload count."""

    block, _raw, _sha = _build()
    named, unnamed = _counts(block)
    texts = [_definition(block), block["notes"][0]]
    claimed_named = {
        int(n)
        for t in texts
        for n in re.findall(
            r"(\d+) (?:of \d+ AUDIT_DERIVED )?bridge rows (?:name a|that name a)", t
        )
    }
    claimed_none = {
        int(n)
        for t in texts
        for n in re.findall(r"(\d+) (?:name none|that name none)", t)
    }
    assert claimed_named == {named}
    assert claimed_none == {unnamed}
    # a universal claim is only allowed when it is true
    universal = any("Each row names the pull" in t for t in texts)
    assert not universal or unnamed == 0


def test_the_counts_follow_the_data_not_the_wording() -> None:
    audit = _audit()
    for strategy in audit["strategies"]:
        for window in strategy["windows"]:
            for component in window["components"]:
                if component["component_id"] != "leverage":
                    component["replacing_pull"] = "EU-P1"
    block, _raw, _sha = _build(audit)
    named, unnamed = _counts(block)
    assert unnamed == 0
    assert (
        f"{named} of {named} AUDIT_DERIVED bridge rows name a replacing pull"
        in _definition(block)
    )
    assert "0 name none" in _definition(block)


def test_a_day_count_in_an_audit_label_is_restated_from_the_window_dates() -> None:
    audit = _audit()
    window = audit["strategies"][0]["windows"][0]
    window["label"] = "Aligned window, 211 days"
    window["days"] = 211
    block, _raw, _sha = _build(audit)
    built = block["strategies"][0]["windows"][0]
    assert built["days"] == 60  # 2026-01-02 .. 2026-03-02 inclusive
    assert (
        built["label"]
        == "Aligned window, 60 calendar days inclusive (211 observations)"
    )
    # the audit text itself stays verbatim in the input
    assert window["label"] == "Aligned window, 211 days"


def test_a_label_without_a_trailing_day_count_is_left_verbatim() -> None:
    audit = _audit()
    audit["strategies"][0]["windows"][0]["label"] = "Primary window"
    block, _raw, _sha = _build(audit)
    assert block["strategies"][0]["windows"][0]["label"] == "Primary window"


def test_no_generated_sentence_repeats_a_word() -> None:
    block, _raw, _sha = _build()
    texts = [
        block["portfolio_bridge"]["dollar_basis"],
        _definition(block),
        *block["notes"],
    ]
    for text in texts:
        assert re.search(r"\b(\w+) \1\b", text, re.IGNORECASE) is None, text
    assert "times the window capital base" in block["portfolio_bridge"]["dollar_basis"]
    json.dumps(copy.deepcopy(block))  # still serialisable


def test_a_withheld_window_is_named_from_its_label_and_dates_not_its_id() -> None:
    """The raw ``window:E1`` id is not shown as a name: the label is generated from the window."""

    from test_model_vs_actual import _errors

    audit = _audit()
    audit["strategies"][0]["windows"][1]["end"] = (
        "2026-04-01"  # after the 2026-03-09 cutoff
    )
    audit["strategies"][0]["windows"][1]["label"] = "Epoch one"
    block, _raw, _sha = _build(audit)
    withheld = [
        u for u in block["strategies"][0]["unavailable"] if u["term"] == "window:E1"
    ]
    assert len(withheld) == 1
    item = withheld[0]
    assert item["label"] == "Epoch one, ends 2026-04-01 after the 2026-03-09 cutoff"
    assert item["window_end"] == "2026-04-01"
    assert "window:" not in item["label"]
    # the other unavailable terms are untouched (no label is invented for them)
    assert all(
        "label" not in u for u in block["strategies"][0]["unavailable"] if u is not item
    )
    assert _errors(block) == []


def _unavailable_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    items = [u for s in block["strategies"] for u in s["unavailable"]]
    for strategy in block["strategies"]:
        for window in [*strategy["windows"], *strategy["epochs"]]:
            items += [
                t
                for t in window["governed_terms"]
                if t["evidence_tag"] == "UNAVAILABLE"
            ]
    return items


def _unavailable_definition(block: dict[str, Any]) -> str:
    return next(
        t["definition"] for t in block["evidence_tags"] if t["tag"] == "UNAVAILABLE"
    )


def test_the_unavailable_definition_counts_the_items_that_name_a_pull() -> None:
    audit = _audit()
    audit["strategies"][0]["unavailable"].append(
        {
            "term": "gap_two",
            "reason": "NO_MODEL_LEG",
            "window_end": None,
            "replacing_pull": None,
        }
    )
    panel = _panel()
    panel[
        "leverage_disclosures"
    ] = []  # no governed interest rows: governed terms are UNAVAILABLE
    raw = json.dumps(audit).encode("utf-8")
    block = mva.build_model_vs_actual(panel, raw, hashlib.sha256(raw).hexdigest())
    items = _unavailable_items(block)
    named = sum(1 for u in items if u.get("replacing_pull") is not None)
    empty = sum(
        1 for u in items if "replacing_pull" in u and u["replacing_pull"] is None
    )
    absent = sum(1 for u in items if "replacing_pull" not in u)
    assert named >= 1 and empty >= 1 and absent >= 1, (
        "fixture must hold all three kinds"
    )
    text = _unavailable_definition(block)
    assert f"{named} of {len(items)} UNAVAILABLE items name a replacing pull" in text
    assert f"{empty} have an empty pull field" in text
    assert f"{absent} carry no pull field" in text
    assert "the pull is named" not in text


def test_the_unavailable_note_counts_only_the_listed_unavailable_entries() -> None:
    audit = _audit()
    audit["strategies"][0]["unavailable"].append(
        {
            "term": "gap_two",
            "reason": "NO_MODEL_LEG",
            "window_end": None,
            "replacing_pull": None,
        }
    )
    block, _raw, _sha = _build(audit)
    entries = [u for s in block["strategies"] for u in s["unavailable"]]
    named = sum(1 for u in entries if u["replacing_pull"] is not None)
    note = block["notes"][1]
    assert f"{named} of the {len(entries)} unavailable entries name" in note
    assert "with the pull that closes it" not in note
    assert _errors(block) == []


def test_the_unavailable_note_lists_the_reasons_of_the_entries_it_counts() -> None:
    audit = _audit()
    audit["strategies"][0]["unavailable"].append(
        {
            "term": "t2",
            "reason": "OTHER_REASON",
            "window_end": None,
            "replacing_pull": None,
        }
    )
    audit["strategies"][0]["windows"][1]["end"] = (
        "2026-04-01"  # after the 2026-03-09 cutoff
    )
    block, _raw, _sha = _build(audit)
    entries = [u for s in block["strategies"] for u in s["unavailable"]]
    reasons: dict[str, int] = {}
    for entry in entries:
        key = entry["reason"].split(":", 1)[0]
        reasons[key] = reasons.get(key, 0) + 1
    assert len(reasons) >= 3, "fixture must hold several reasons"
    note = block["notes"][1]
    for reason, count in reasons.items():
        assert f"{reason} ({count})" in note
    assert "has no admitted model leg" not in note


def test_the_static_intro_does_not_promise_a_pull_for_every_unavailable_term() -> None:
    """T2: the intro states only what the payload guarantees; counts live in the block."""

    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "portfolio-control-panel"
        / "index.html"
    ).read_text(encoding="utf-8")
    intro = re.search(r'id="modelvsactual-heading".*?</p>', html, re.S)
    assert intro is not None
    text = re.sub(r"<[^>]+>", " ", intro.group(0))
    assert "and the pull that replaces them" not in text
    assert "only where the audit input names one" in text
    assert not re.search(r"\d", text.split("Why did")[-1].split("?")[-1])

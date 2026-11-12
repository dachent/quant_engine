"""Owner view (#908): the page reconciles to the validator findings; nothing is suppressed."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

import pytest

from acceptance.owner_view import build as ov
import test_validate_generation as tvg


def finding(
    check: str,
    klass: str = "NOT_READY",
    entity: str = "e",
    observed: Any = None,
    defects: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "check_id": check,
        "class": klass,
        "entity": entity,
        "observed": observed,
        "expected": "x",
        "pointer": "f#$",
        "defect_ids": list(defects),
    }


def doc(*findings: dict[str, Any]) -> dict[str, Any]:
    return {"generation_id": "g" * 64, "summary": {}, "findings": list(findings)}


def entry(klass: str, **kw: Any) -> dict[str, Any]:
    block = {
        "owner_class": klass,
        "entity_match": ".*",
        "what": "Thing is wrong for {entity}.",
        "fix": "do it",
        "who": "loader",
        "how_long": "1 day",
    }
    block.update(kw.pop("block", {}))
    return {
        "check_id": "c.one",
        "defect": kw.pop("defect", "E1"),
        "issue": 1,
        "close_by": "2026-12-15",
        "owner_view": block,
        **kw,
    }


TODAY = date(2026, 11, 11)


def test_unregistered_finding_is_real_and_listed_first() -> None:
    d = doc(finding("c.one", defects=("E1",)), finding("c.two"))
    view = ov.build_owner_view(d, register=[entry(ov.NOT_APPLICABLE)], today=TODAY)
    assert [i["class"] for i in view["items"]] == [ov.UNCLASSIFIED, ov.NOT_APPLICABLE]
    assert view["items"][0]["real_or_checker"] == "real"
    assert view["real_items"] == 1
    assert ov.render_markdown(view).index("Unclassified") < ov.render_markdown(
        view
    ).index("Does not apply")


def test_no_register_means_everything_is_unclassified_and_real() -> None:
    view = ov.build_owner_view(doc(finding("a"), finding("b")), today=TODAY)
    assert view["counts"][ov.UNCLASSIFIED] == 2 and view["real_items"] == 2


def test_blocker_is_listed_before_unclassified() -> None:
    view = ov.build_owner_view(
        doc(finding("a"), finding("b", "BLOCKER_INVARIANT")), today=TODAY
    )
    assert view["items"][0]["class"] == ov.BLOCKER
    assert ov.verdict(view).startswith("STOP")


def test_reconciliation_every_finding_has_an_item_kill() -> None:
    d = doc(finding("a"), finding("b"), finding("c"))
    view = ov.build_owner_view(d, today=TODAY)
    assert {r for i in view["items"] for r in i["finding_refs"]} == {0, 1, 2}
    with pytest.raises(ov.ReconciliationError):
        ov.reconcile(d["findings"], view["items"][:-1])  # a dropped row is refused


def test_multi_entity_finding_is_split_per_entity() -> None:
    f = finding(
        "c.one",
        observed={
            "uncovered_accounts": {"a1": "NO_ROW", "a2": "OBSERVED_ONLY|UNAVAILABLE"}
        },
        defects=("E1",),
    )
    reg = [entry(ov.NOT_APPLICABLE, block={"when": {"entity_status": "NO_ROW"}})]
    view = ov.build_owner_view(doc(f), register=reg, today=TODAY)
    by = {i["entity"]: i["class"] for i in view["items"]}
    assert by == {
        "a1": ov.NOT_APPLICABLE,
        "a2": ov.UNCLASSIFIED,
    }  # the minority is not hidden


def test_entry_voids_itself_when_facts_change() -> None:
    reg = [entry(ov.NOT_APPLICABLE, block={"when": {"observed.status": "NONE"}})]
    ok = ov.build_owner_view(
        doc(finding("c.one", observed={"status": "NONE"}, defects=("E1",))),
        register=reg,
        today=TODAY,
    )
    changed = ov.build_owner_view(
        doc(finding("c.one", observed={"status": "DEBIT"}, defects=("E1",))),
        register=reg,
        today=TODAY,
    )
    assert ok["items"][0]["class"] == ov.NOT_APPLICABLE
    assert changed["items"][0]["class"] == ov.UNCLASSIFIED


def test_invalid_entries_are_ignored_not_trusted() -> None:
    bad = [
        entry(ov.ACCEPTED_BY_DESIGN),  # no owner_record / reviewed_on
        entry(ov.NEEDS_OWNER_DECISION),  # no decision
        entry(ov.REAL_DATA_GAP, block={"how_long": ""}),
        entry("MADE_UP"),
    ]
    assert len(ov.registry_errors(bad)) >= 4
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=bad, today=TODAY
    )
    assert view["items"][0]["class"] == ov.UNCLASSIFIED
    assert view["registry_errors"]


def test_accepted_by_design_with_record_and_decision_ok() -> None:
    reg = [
        entry(
            ov.ACCEPTED_BY_DESIGN,
            block={"owner_record": "ruling X", "reviewed_on": "2026-11-01"},
        ),
        entry(
            ov.NEEDS_OWNER_DECISION,
            defect="E2",
            block={"decision": {"question": "q?", "options": ["a", "b"]}},
        ),
    ]
    assert ov.registry_errors(reg) == []
    md = ov.render_markdown(
        ov.build_owner_view(
            doc(finding("c.one", defects=("E2",))), register=reg, today=TODAY
        )
    )
    assert "Decision: q? (a) a / (b) b" in md


def test_none_number_needs_a_proof_check() -> None:
    reg = [entry(ov.CHECKER_BUG, block={"number_affected": "none"})]
    item = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
    )["items"][0]
    assert item["number_affected"] == "unknown"
    reg = [
        entry(
            ov.CHECKER_BUG, block={"number_affected": "none", "proof_check": "baseline"}
        )
    ]
    item = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
    )["items"][0]
    assert item["number_affected"] == "none"


def test_overdue_entry_is_marked() -> None:
    reg = [entry(ov.REAL_DATA_GAP, close_by="2026-11-01")]
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
    )
    assert view["overdue_items"] == 1 and "PAST ITS FIX DATE" in ov.render_markdown(
        view
    )


def test_since_comes_from_retained_history() -> None:
    old = doc(finding("c.one", entity="e"))
    view = ov.build_owner_view(
        doc(finding("c.one"), finding("c.new")), history=[("r22", old)], today=TODAY
    )
    assert {i["ref"]["check_id"]: i["since"] for i in view["items"]} == {
        "c.one": "r22",
        "c.new": None,
    }


def test_message_is_persisted_per_finding() -> None:
    view = ov.build_owner_view(
        doc(finding("a")), messages=["plain sentence"], today=TODAY
    )
    assert view["findings"][0]["message"] == "plain sentence"
    assert view["items"][0]["what"] == "plain sentence"


def test_js_is_assignment_of_json() -> None:
    view = ov.build_owner_view(doc(finding("a")), today=TODAY)
    text = ov.render_js(view)
    assert (
        text.startswith("window.PORTFOLIO_OWNER_VIEW = ")
        and json.loads(text.split(" = ", 1)[1].rstrip(";\n"))["verdict"]
    )


def test_cli_writes_sidecars_that_reconcile_to_findings(tmp_path: Path) -> None:
    gen_dir = tvg.build(tmp_path, tvg.CASES["liquid_identity"][2])
    tvg._cli(str(gen_dir))
    out = gen_dir.parent.parent / "validation" / tvg.GEN
    findings = json.loads((out / "findings.json").read_bytes())["findings"]
    view = json.loads((out / "owner-view.json").read_bytes())
    assert {r for i in view["items"] for r in i["finding_refs"]} == set(
        range(len(findings))
    )
    assert len(view["findings"]) == len(findings)
    assert all(f["message"] for f in view["findings"])  # the sentence is persisted
    assert b"\r\n" not in (out / "owner-view.md").read_bytes()
    assert (
        (out / "owner-view.js").read_bytes().startswith(b"window.PORTFOLIO_OWNER_VIEW")
    )


def test_entry_must_state_its_entity_scope() -> None:
    e = entry(ov.NOT_APPLICABLE)
    del e["owner_view"]["entity_match"]
    assert any("entity_match" in m for m in ov.registry_errors([e]))
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=[e], today=TODAY
    )
    assert (
        view["items"][0]["class"] == ov.UNCLASSIFIED
    )  # a catch-all by omission is refused


def test_row_scope_classifies_the_whole_row_once() -> None:
    f = finding(
        "c.one",
        observed={"uncovered_accounts": {"a1": "X", "a2": "Y"}},
        defects=("E1",),
    )
    e = entry(ov.CHECKER_BUG, block={"scope": "row"})
    del e["owner_view"]["entity_match"]
    view = ov.build_owner_view(doc(f), register=[e], today=TODAY)
    assert [i["class"] for i in view["items"]] == [ov.CHECKER_BUG]

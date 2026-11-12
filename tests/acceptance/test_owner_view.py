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


def _row_entry(klass: str) -> dict[str, Any]:
    e = entry(
        klass, block={"scope": "row", "row_scope_reason": "all entities identical"}
    )
    del e["owner_view"]["entity_match"]
    return e


def test_row_scope_classifies_a_uniform_row_once() -> None:
    f = finding(
        "c.one",
        observed={"uncovered_accounts": {"a1": "X", "a2": "X"}},
        defects=("E1",),
    )
    view = ov.build_owner_view(
        doc(f), register=[_row_entry(ov.CHECKER_BUG)], today=TODAY
    )
    assert [i["class"] for i in view["items"]] == [ov.CHECKER_BUG]


def test_row_scope_never_hides_a_minority_entity_kill() -> None:
    f = finding(
        "c.one",
        observed={"uncovered_accounts": {"a1": "NO_ROW", "a2": "DEBIT"}},
        defects=("E1",),
    )
    view = ov.build_owner_view(
        doc(f), register=[_row_entry(ov.NOT_APPLICABLE)], today=TODAY
    )
    assert {i["entity"]: i["class"] for i in view["items"]} == {
        "a1": ov.UNCLASSIFIED,
        "a2": ov.UNCLASSIFIED,
    }
    assert view["real_items"] == 2


def test_row_scope_needs_a_reason() -> None:
    e = _row_entry(ov.CHECKER_BUG)
    del e["owner_view"]["row_scope_reason"]
    assert any("row_scope_reason" in m for m in ov.registry_errors([e]))


@pytest.mark.parametrize("defect", ["", None, "  "])
def test_defect_is_required(defect: Any) -> None:
    e = entry(ov.NOT_APPLICABLE)
    e["defect"] = defect
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E9",))), register=[e], today=TODAY
    )
    assert view["items"][0]["class"] == ov.UNCLASSIFIED and view["registry_errors"]


def test_defect_none_matches_only_findings_without_defects() -> None:
    e = entry(ov.NOT_APPLICABLE, defect="NONE")
    view = ov.build_owner_view(
        doc(finding("c.one"), finding("c.one", entity="z", defects=("E1",))),
        register=[e],
        today=TODAY,
    )
    assert sorted(i["class"] for i in view["items"]) == [
        ov.NOT_APPLICABLE,
        ov.UNCLASSIFIED,
    ]


def test_entity_match_is_anchored() -> None:
    e = entry(ov.NOT_APPLICABLE, block={"entity_match": "ibkr"})
    view = ov.build_owner_view(
        doc(finding("c.one", entity="not-ibkr-reserve", defects=("E1",))),
        register=[e],
        today=TODAY,
    )
    assert view["items"][0]["class"] == ov.UNCLASSIFIED


def test_first_match_wins_one_item_per_entity() -> None:
    reg = [entry(ov.REAL_DATA_GAP), entry(ov.NOT_APPLICABLE)]
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
    )
    assert [i["class"] for i in view["items"]] == [ov.REAL_DATA_GAP]


def test_malformed_register_never_crashes_the_page() -> None:
    reg = [
        entry(ov.NEEDS_OWNER_DECISION, block={"decision": "pick one"}),
        entry(ov.REAL_DATA_GAP, close_by="soon"),
        entry(
            ov.ACCEPTED_BY_DESIGN, block={"owner_record": "r", "reviewed_on": "soon"}
        ),
        "not an object",
    ]
    assert len(ov.registry_errors(reg)) == 4
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
    )
    assert view["items"][0]["class"] == ov.UNCLASSIFIED


def test_since_is_per_entity_inside_a_split_finding() -> None:
    old = doc(finding("c.one", observed={"uncovered_accounts": {"a1": "X"}}))
    new = finding("c.one", observed={"uncovered_accounts": {"a1": "X", "a2": "X"}})
    view = ov.build_owner_view(doc(new), history=[("r22", old)], today=TODAY)
    assert {i["entity"]: i["since"] for i in view["items"]} == {"a1": "r22", "a2": None}


def test_group_does_not_merge_different_overdue_or_records() -> None:
    reg = [
        entry(
            ov.ACCEPTED_BY_DESIGN,
            block={
                "entity_match": "^a$",
                "owner_record": "ruling-A",
                "reviewed_on": "2026-11-01",
            },
            close_by="2026-11-01",
        ),
        entry(
            ov.ACCEPTED_BY_DESIGN,
            block={
                "entity_match": "^b$",
                "owner_record": "ruling-B",
                "reviewed_on": "2026-11-01",
            },
            close_by="2099-01-01",
        ),
    ]
    f = finding("c.one", observed={"unavailable": ["a", "b"]}, defects=("E1",))
    md = ov.render_markdown(ov.build_owner_view(doc(f), register=reg, today=TODAY))
    assert "ruling-A" in md and "ruling-B" in md and md.count("PAST ITS FIX DATE") == 1


def test_js_escapes_script_close() -> None:
    reg = [entry(ov.CHECKER_BUG, block={"what": "</script><script>alert(1)"})]
    text = ov.render_js(
        ov.build_owner_view(
            doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
        )
    )
    assert "</script>" not in text and "<" not in text


def test_reconciliation_failure_writes_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen_dir = tvg.build(tmp_path, tvg.CASES["liquid_identity"][2])
    monkeypatch.setattr(
        ov,
        "reconcile",
        lambda *a, **k: (_ for _ in ()).throw(ov.ReconciliationError("x")),
    )
    assert tvg._cli(str(gen_dir)) == 2
    out = gen_dir.parent.parent / "validation" / tvg.GEN
    assert not (out / "findings.json").exists()


def test_malformed_field_types_never_crash_or_classify() -> None:
    bad = [
        entry(ov.REAL_DATA_GAP, block={"entity_match": ["^e$"]}),
        entry(ov.REAL_DATA_GAP, block={"when": ["status=NONE"]}),
        entry(
            ov.NEEDS_OWNER_DECISION,
            block={"decision": {"question": "q", "options": ["", None]}},
        ),
        entry(ov.REAL_DATA_GAP, block={"who": 1}),
        entry(
            ov.ACCEPTED_BY_DESIGN,
            block={"owner_record": "   ", "reviewed_on": "2026-11-01"},
        ),
        entry(ov.CHECKER_BUG, block={"number_affected": "none", "proof_check": "   "}),
    ]
    assert len(ov.registry_errors(bad)) >= 6
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=bad, today=TODAY
    )
    assert [i["class"] for i in view["items"]] == [ov.UNCLASSIFIED]


def test_zero_and_nil_also_need_a_proof_check() -> None:
    for word in ("0", "zero", "nil"):
        reg = [entry(ov.CHECKER_BUG, block={"number_affected": word})]
        item = ov.build_owner_view(
            doc(finding("c.one", defects=("E1",))), register=reg, today=TODAY
        )["items"][0]
        assert item["number_affected"] == "unknown"


def test_entry_for_one_defect_never_classifies_a_finding_with_another_kill() -> None:
    f = finding("c.one", defects=("B4", "B6"))
    one = entry(ov.NOT_APPLICABLE, defect="B6")
    both = entry(ov.NOT_APPLICABLE, defect="B4,B6")
    assert (
        ov.build_owner_view(doc(f), register=[one], today=TODAY)["items"][0]["class"]
        == ov.UNCLASSIFIED
    )
    assert (
        ov.build_owner_view(doc(f), register=[both], today=TODAY)["items"][0]["class"]
        == ov.NOT_APPLICABLE
    )


def test_when_none_does_not_match_a_missing_key() -> None:
    e = entry(ov.NOT_APPLICABLE, block={"when": {"observed.k": None}})
    gone = ov.build_owner_view(
        doc(finding("c.one", observed={"x": 1}, defects=("E1",))),
        register=[e],
        today=TODAY,
    )
    there = ov.build_owner_view(
        doc(finding("c.one", observed={"k": None}, defects=("E1",))),
        register=[e],
        today=TODAY,
    )
    assert gone["items"][0]["class"] == ov.UNCLASSIFIED
    assert there["items"][0]["class"] == ov.NOT_APPLICABLE


def test_row_entry_and_entity_entry_both_show_the_real_minority() -> None:
    f = finding("c.one", observed={"unavailable": ["p123", "crypto"]}, defects=("E1",))
    reg = [
        entry(ov.REAL_DATA_GAP, block={"entity_match": "^crypto$"}),
        _row_entry(ov.CHECKER_BUG),
    ]
    view = ov.build_owner_view(doc(f), register=reg, today=TODAY)
    assert sorted(i["class"] for i in view["items"]) == [
        ov.CHECKER_BUG,
        ov.REAL_DATA_GAP,
    ]
    assert view["real_items"] == 1  # the crypto gap is not shadowed by the row verdict


def test_blocker_cannot_be_reclassified_by_the_register() -> None:
    reg = [entry(ov.NOT_APPLICABLE, block={"entity_match": ".*"})]
    view = ov.build_owner_view(
        doc(finding("c.one", "BLOCKER_INVARIANT", defects=("E1",))),
        register=reg,
        today=TODAY,
    )
    assert [i["class"] for i in view["items"]] == [ov.BLOCKER]


def test_output_is_deterministic_for_a_given_day() -> None:
    d = doc(finding("a"), finding("b", defects=("E2",)), finding("a", entity="z"))
    first = ov.render_js(ov.build_owner_view(d, today=TODAY))
    second = ov.render_js(ov.build_owner_view(d, today=TODAY))
    assert first == second and "2026-11-11" in first


def test_overlapping_entries_never_let_the_harmless_one_win_kill() -> None:
    f = finding("c.one", entity="ibkr-a", defects=("E1",))
    reg = [
        entry(ov.NOT_APPLICABLE, block={"entity_match": ".*"}),
        entry(ov.REAL_DATA_GAP, block={"entity_match": "ibkr-a"}),
    ]
    view = ov.build_owner_view(doc(f), register=reg, today=TODAY)
    assert [i["class"] for i in view["items"]] == [ov.REAL_DATA_GAP]
    assert any("disagree" in e for e in view["registry_errors"])


def test_decision_only_on_decision_entries_and_owner_record_is_text() -> None:
    bad = [
        entry(ov.REAL_DATA_GAP, block={"decision": "malformed"}),
        entry(ov.REAL_DATA_GAP, block={"owner_record": []}),
    ]
    assert len(ov.registry_errors(bad)) == 2
    view = ov.build_owner_view(
        doc(finding("c.one", defects=("E1",))), register=bad, today=TODAY
    )
    assert view["items"][0]["class"] == ov.UNCLASSIFIED


def test_markdown_keeps_distinct_decisions_apart() -> None:
    f = finding("c.one", observed={"unavailable": ["a", "b"]}, defects=("E1",))
    reg = [
        entry(
            ov.NEEDS_OWNER_DECISION,
            block={
                "entity_match": "^a$",
                "decision": {"question": "A?", "options": ["a1", "a2"]},
            },
        ),
        entry(
            ov.NEEDS_OWNER_DECISION,
            block={
                "entity_match": "^b$",
                "decision": {"question": "B?", "options": ["b1", "b2"]},
            },
        ),
    ]
    md = ov.render_markdown(ov.build_owner_view(doc(f), register=reg, today=TODAY))
    assert "A?" in md and "B?" in md


def test_malformed_history_never_blocks_the_run() -> None:
    view = ov.build_owner_view(
        doc(finding("a")),
        history=[("x", []), ("y", {"findings": [1, "s"]})],
        today=TODAY,
    )  # type: ignore[list-item]
    assert view["items"][0]["class"] == ov.UNCLASSIFIED


def test_cli_also_findings_join_the_owner_view_not_findings_json(
    tmp_path: Path,
) -> None:
    gen_dir = tvg.build(tmp_path, tvg.CASES["liquid_identity"][2])
    extra = tmp_path / "targetgen" / "findings.json"
    extra.parent.mkdir()
    extra.write_bytes(
        json.dumps(
            {"findings": [finding("t.block", "BLOCKER_AUTHORITY", entity="x")]}
        ).encode()
    )
    tvg._cli(str(gen_dir), "--also-findings", str(extra))
    out = gen_dir.parent.parent / "validation" / tvg.GEN
    own = json.loads((out / "findings.json").read_bytes())["findings"]
    view = json.loads((out / "owner-view.json").read_bytes())
    assert all(f["check_id"] != "t.block" for f in own)
    assert any(
        i["entity"] == "targetgen:x" and i["class"] == ov.BLOCKER for i in view["items"]
    )
    assert view["source_findings"] == len(own) + 1
    assert ov.verdict(view).startswith("STOP")


def test_derived_check_id_and_finding_entity_scope() -> None:
    e = entry(
        ov.NOT_APPLICABLE,
        block={"finding_check_id": "c.derived", "finding_entity_match": r"^x\..*"},
    )
    e["check_id"] = "c.base"
    hit = finding("c.derived", entity="x.one", defects=("E1",))
    miss_entity = finding("c.derived", entity="y.one", defects=("E1",))
    miss_check = finding("c.base", entity="x.one", defects=("E1",))
    view = ov.build_owner_view(
        doc(hit, miss_entity, miss_check), register=[e], today=TODAY
    )
    assert sorted(i["class"] for i in view["items"]) == [
        ov.NOT_APPLICABLE,
        ov.UNCLASSIFIED,
        ov.UNCLASSIFIED,
    ]

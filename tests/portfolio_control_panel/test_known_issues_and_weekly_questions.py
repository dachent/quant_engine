"""The governed known-issues list feeds DO NOT RELY ON, and the pack alone answers the weekly questions.

The acceptance check is a deterministic rubric over the pack text (no model call): each of the five
weekly questions must be answerable from the pack with fact ids.  `tools/context_pack.py` prints the
pack for a real analyst run; this test is the floor that run starts from.
"""

from __future__ import annotations

import copy
import functools
import os
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import context_pack as cp  # noqa: E402
import portfolio_decision as pd  # noqa: E402

POLICY_PATH = ROOT / "contracts" / "portfolio-decision-policy-v1.json"
SOURCES_PATH = ROOT / "contracts" / "strategy-return-sources-v1.json"
ISSUES_PATH = ROOT / "contracts" / "portfolio-known-issues-v1.json"
# QE_PACK_PAYLOAD points the rubric at a real panel payload (a candidate's dashboard_data.json);
# without it the committed 330da1a0 fixture is used.
FIXTURE = Path(
    os.environ.get("QE_PACK_PAYLOAD")
    or Path(__file__).with_name("decision_fixture_330da1a0.json")
)


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(FIXTURE.read_bytes())


def _issues() -> dict:
    return json.loads(ISSUES_PATH.read_bytes())


def _pack(issues=None, prior=None) -> dict:
    data = _data()
    policy_bytes = POLICY_PATH.read_bytes()
    policy = json.loads(policy_bytes)
    sha = hashlib.sha256(policy_bytes).hexdigest()
    decision = data.get("decision") or pd.build_decision(data, policy, sha)
    sources = json.loads(SOURCES_PATH.read_bytes())["strategies"]
    return cp.build_pack(data, decision, policy, sha, sources, None, prior, issues)


def _section(pack: dict, section_id: str) -> str:
    text = pack["text"]
    titles = {s["id"]: s["title"] for s in pack["sections"]}
    order = [s["id"] for s in pack["sections"]]
    start = text.index(f"# {order.index(section_id) + 1}. {titles[section_id]}")
    nxt = order.index(section_id) + 1
    end = (
        text.index(f"# {nxt + 1}. {titles[order[nxt]]}")
        if nxt < len(order)
        else len(text)
    )
    return text[start:end]


# ---- the governed list -------------------------------------------------------------------


def test_the_list_has_unique_ids_and_the_resolved_schwab_054_item_is_not_in_it() -> (
    None
):
    issues = _issues()["issues"]
    ids = [i["id"] for i in issues]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"KI-\d{2,}", i) for i in ids)
    blob = json.dumps(issues)
    # B-KNOWN-ISSUES item 7 is resolved (no gap, 31/31 days observed): it must not be disclosed.
    assert "cash ladder" not in blob and "Jan-2026" not in blob
    for issue in issues:
        assert issue["status"] in ("OPEN", "FIXED")
        assert issue["do_not_rely"].strip() and issue["effect"].strip()
        assert set(issue["fix"]) == {"ticket", "release", "note"}


def test_every_open_issue_is_in_do_not_rely_and_a_fixed_one_is_not() -> None:
    issues = _issues()
    pack = _pack(issues)
    section = _section(pack, "do_not_rely")
    for issue in issues["issues"]:
        assert (issue["id"] in section) == (issue["status"] == "OPEN")
    fixed = copy.deepcopy(issues)
    fixed["issues"][0]["status"] = "FIXED"
    after = _section(_pack(fixed), "do_not_rely")
    assert issues["issues"][0]["id"] not in after
    assert issues["issues"][1]["id"] in after


def test_the_general_list_stays_without_a_known_issues_file() -> None:
    section = _section(_pack(None), "do_not_rely")
    assert "Grade D figures for sizing" in section
    assert "KI-" not in section


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(schema_version=2),
        lambda c: c.update(issues="x"),
        lambda c: c["issues"][1].update(id=c["issues"][0]["id"]),
        lambda c: c["issues"][0].update(id="bad"),
        lambda c: c["issues"][0].update(status="MAYBE"),
        lambda c: c["issues"][0].update(do_not_rely=" "),
    ],
)
def test_a_malformed_list_refuses_the_pack(mutate) -> None:
    broken = copy.deepcopy(_issues())
    mutate(broken)
    with pytest.raises(cp.PackError):
        _pack(broken)


def test_the_pack_with_the_list_stays_inside_the_token_cap() -> None:
    pack = _pack(_issues())
    assert pack["tokens_estimate"] <= cp.HARD_TOKEN_CAP


# ---- the five weekly questions, answered from the pack text alone --------------------------

ZONES = ("NORMAL", "NO_NEW_RISK", "LIMIT_BREACH")
STATUSES = ("HOLD", "WATCH", "REDUCE", "EXIT")


def _fact_ids(pack: dict) -> set[str]:
    return {f["id"] for f in pack["facts"]}


def _cited(text: str) -> set[str]:
    return {
        part.strip()
        for group in re.findall(r"\[([a-z0-9_.,\- ]+)\]", text)
        for part in group.split(",")
    }


def test_q1_limit_status_is_stated_with_the_zone_and_cited_facts() -> None:
    pack = _pack(_issues())
    snapshot = _section(pack, "decision_snapshot")
    assert re.search(r"Drawdown [\d.]+% against the \d+% limit", snapshot)
    assert any(f"Zone: {z}" in snapshot for z in ZONES)
    cited = _cited(snapshot)
    assert {"portfolio.drawdown", "portfolio.headroom"} <= cited <= _fact_ids(pack)


def test_q2_rebalance_actions_and_the_moves_held_back_are_stated() -> None:
    snapshot = _section(_pack(_issues()), "decision_snapshot")
    assert "Actions the rules imply this week:" in snapshot
    assert (
        "Held back by the rules:" in snapshot
        or "Actions the rules imply this week: none" in snapshot
    )


def test_q3_every_strategy_has_a_status_and_its_cited_facts() -> None:
    pack = _pack(_issues())
    cards = _section(pack, "strategy_cards")
    # The current strategies are the ones the decision block weights, not every contract entry.
    current = {
        m.group(1)
        for f in pack["facts"]
        if (m := re.fullmatch(r"strategy\.([a-z0-9-]+)\.weight", f["id"]))
    }
    assert current
    for sid in current:
        assert f"({sid})" in cards, sid
    statuses = re.findall(r"status (HOLD|WATCH|REDUCE|EXIT) at ratio", cards)
    assert len(statuses) == len(current)
    assert _cited(cards) <= _fact_ids(pack)


def test_q4_what_changed_is_present_and_names_the_missing_prior() -> None:
    first = _section(_pack(_issues()), "changes")
    assert "NO_PRIOR_PACK" in first
    prior = _pack(_issues())
    again = _section(_pack(_issues(), prior=prior), "changes")
    assert "NO_PRIOR_PACK" not in again


def test_q5_what_not_to_trust_names_the_known_issues_and_the_data_holes() -> None:
    pack = _pack(_issues())
    not_trust = _section(pack, "do_not_rely")
    holes = _section(pack, "data_holes")
    assert all(
        i["id"] in not_trust for i in _issues()["issues"] if i["status"] == "OPEN"
    )
    assert all(h["id"] in holes for h in pack["holes"])


def test_the_stress_bound_is_not_a_forecast_and_uses_the_published_rule_when_present() -> (
    None
):
    plain = _section(_pack(_issues()), "do_not_rely")
    assert "bound under the stated rule, not a forecast" in plain
    if os.environ.get("QE_PACK_PAYLOAD"):
        # a real payload carries its own published rule statement; the injected one below is for the fixture
        assert "Rule as published:" in plain
        return
    data = dict(_data())
    data["correlation"] = {
        "rows": [{"adverse_loss_basis": {"rule_statement": "1.25x prior rule text"}}]
    }
    policy_bytes = POLICY_PATH.read_bytes()
    sha = hashlib.sha256(policy_bytes).hexdigest()
    decision = pd.build_decision(data, json.loads(policy_bytes), sha)
    sources = json.loads(SOURCES_PATH.read_bytes())["strategies"]
    pack = cp.build_pack(
        data, decision, json.loads(policy_bytes), sha, sources, None, None, _issues()
    )
    assert "Rule as published: 1.25x prior rule text" in _section(pack, "do_not_rely")


def test_a_closed_pull_is_not_a_data_hole_and_an_open_one_is() -> None:
    data = dict(_data())
    mva = dict(data.get("model_vs_actual") or {})
    mva["pulls"] = [
        {
            "pull_id": "X-OPEN",
            "title": "open pull",
            "replaces": ["timing"],
            "access": "public",
            "status": "REQUESTED_NOT_DOWNLOADED",
        },
        {
            "pull_id": "X-CLOSED",
            "title": "closed pull",
            "replaces": ["timing"],
            "access": "public",
            "status": "CLOSED_BY_CAPTURE_X",
        },
    ]
    data["model_vs_actual"] = mva
    whats = " | ".join(h["what"] for h in cp._holes(data))
    assert "X-OPEN" in whats and "X-CLOSED" not in whats


def test_an_owner_ruling_pull_stays_a_hole_and_says_it_is_not_a_download() -> None:
    data = dict(_data())
    data["model_vs_actual"] = {
        "pulls": [
            {
                "pull_id": "X-RULING",
                "title": "owner ruling pull",
                "replaces": ["fx"],
                "access": "no download",
                "status": "NOT_A_DOWNLOAD_OWNER_RULING",
            }
        ]
    }
    holes = [h for h in cp._holes(data) if "X-RULING" in h["what"]]
    assert len(holes) == 1
    assert holes[0]["closes_by"].startswith("owner ruling: not a download")
    assert "to download" not in holes[0]["closes_by"]

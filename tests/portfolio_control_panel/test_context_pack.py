"""The analysis context pack: self-describing, graded, within its token cap, and refused when not."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import context_pack as cp  # noqa: E402
import portfolio_decision as pd  # noqa: E402
import recommendation_log as rl  # noqa: E402

POLICY_PATH = ROOT / "contracts" / "portfolio-decision-policy-v1.json"
SOURCES_PATH = ROOT / "contracts" / "strategy-return-sources-v1.json"
FIXTURE = Path(__file__).with_name("decision_fixture_330da1a0.json")
HARNESS = Path(__file__).with_name("shell_harness.mjs")
APP = ROOT / "apps" / "portfolio-control-panel" / "app.js"
REAL_DEFAULT = Path(r"C:\Dev\scratch\sprint-decision\payload-330da1a0.json")


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_bytes())


def _sha() -> str:
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def _payload() -> dict:
    return json.loads(FIXTURE.read_bytes())


def _sources() -> list:
    return json.loads(SOURCES_PATH.read_bytes())["strategies"]


def _pack(data: dict | None = None, prior=None, recs=None) -> dict:
    data = data if data is not None else _payload()
    decision = pd.build_decision(data, _policy(), _sha())
    return cp.build_pack(data, decision, _policy(), _sha(), _sources(), recs, prior)


def _fact(pack: dict, fact_id: str) -> dict:
    return next(f for f in pack["facts"] if f["id"] == fact_id)


def test_pack_fits_its_budget_and_is_self_describing() -> None:
    pack = _pack()
    assert pack["schema_version"] == 1 and pack["pack_id"].startswith("PACK-")
    assert 3_000 < pack["tokens_estimate"] < cp.CORE_TOKEN_TARGET <= cp.HARD_TOKEN_CAP
    titles = [s["title"] for s in pack["sections"]]
    assert titles[0] == "Header" and titles[-1] == "DO NOT RELY ON"
    for needed in (
        "Decision snapshot (the human Decision tab shows this)",
        "Definitions",
        "Evidence grades and caveat ids",
        "Status glossary",
        "Facts",
        "Policy",
        "Owner standing rules",
        "Data dates and known data holes",
    ):
        assert needed in titles
    text = pack["text"]
    assert "Policy PORT-DECISION-1 version 1" in text
    assert "Agents never place trades." in text
    assert text.rstrip().endswith(pack["pack_id"])
    assert (
        pack["pack_id"]
        == "PACK-"
        + hashlib.sha256(text.rsplit("\n\nPack id:", 1)[0].encode()).hexdigest()[:12]
    )


def test_pack_is_deterministic_and_changes_when_a_fact_changes() -> None:
    assert _pack()["pack_id"] == _pack()["pack_id"]
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.1300"
    assert _pack(data)["pack_id"] != _pack()["pack_id"]


def test_every_fact_has_an_as_of_a_known_grade_and_known_caveats() -> None:
    pack = _pack()
    assert len(pack["facts"]) > 100
    for fact in pack["facts"]:
        assert fact["as_of"] and fact["grade"] in cp.GRADES
        assert all(c in cp.CAVEATS for c in fact["caveats"])
    assert sum(pack["grade_counts"].values()) == len(pack["facts"])


def test_grades_follow_the_evidence() -> None:
    pack = _pack()
    assert _fact(pack, "portfolio.capital")["grade"] == "A"
    assert (
        _fact(pack, "portfolio.drawdown")["grade"] == "B"
        and "HWM_FILL" in _fact(pack, "portfolio.drawdown")["caveats"]
    )
    assert _fact(pack, "portfolio.stress_loss")["grade"] == "P"
    assert _fact(pack, "strategy.volatis-strategy.prior_worst")["grade"] == "D"
    assert (
        "PLATFORM_ONLY"
        in _fact(pack, "strategy.volatis-strategy.stress_fraction")["caveats"]
    )
    robuxio = _fact(pack, "strategy.robuxio-crypto.live_drawdown_now")
    assert robuxio["grade"] == "C" and "WEEKLY_BASIS" in robuxio["caveats"]
    assert _fact(pack, "strategy.tt-portfolio.gap_since_start")["grade"] == "C"
    assert not any(
        f["id"].endswith(".gap_last_week") for f in pack["facts"]
    )  # no model series: no gap invented


def test_no_alpha_beta_or_quality_word_outside_the_exclusion_list() -> None:
    pack = _pack()
    text = pack["text"]
    body, _, rest = text.partition("# 14. DO NOT RELY ON")
    assert "alpha" in rest.lower()  # named only in the exclusion list
    cards_start = body.index("# 7. Strategies")
    cards_end = body.index("# 8. Facts")
    outside = body[:cards_start] + body[cards_end:]
    for word in ("alpha", "beta", "r-squared", "conservative"):
        assert word not in outside.lower(), word


def test_strategy_cards_carry_the_concept_text_cleaned() -> None:
    text = _pack()["text"]
    assert "Mechanism:" in text and "What would prove it wrong:" in text
    assert "\u00e2\u20ac" not in text  # no double-encoded dashes


def test_every_status_token_is_in_the_glossary() -> None:
    pack = _pack()
    glossary = pack["text"].split("# 5. Status glossary")[1].split("# 6.")[0]
    for token in (
        "NO_NEW_RISK",
        "ABOVE_BAND",
        "HOLD",
        "INACTIVE",
        "ABOVE_CAP",
        "ABOVE_LIMIT",
    ):
        assert f"- {token}:" in glossary
    assert "(no glossary entry)" not in pack["text"]


# --- the validator refuses ----------------------------------------------------------------


def test_unknown_status_token_is_refused() -> None:
    pack = _pack()
    pack["text"] += "\nA new status SOMETHING_NEW appears here.\n"
    assert any("SOMETHING_NEW" in f for f in cp.validate_pack(pack))


def test_a_pack_over_the_token_cap_is_refused() -> None:
    pack = _pack()
    pack["tokens_estimate"] = cp.HARD_TOKEN_CAP + 1
    assert any("hard cap" in f for f in cp.validate_pack(pack))


def test_a_citation_to_a_missing_fact_is_refused() -> None:
    pack = _pack()
    pack["text"] += "\nSee [portfolio.nothing_here].\n"
    assert any("portfolio.nothing_here" in f for f in cp.validate_pack(pack))


def test_a_banned_measure_in_a_body_section_is_refused() -> None:
    pack = _pack()
    sections = [{"id": "portfolio_state", "text": "The strategy alpha is 4%."}]
    assert any("banned" in f for f in cp.validate_pack(pack, sections))
    sections = [{"id": "portfolio_state", "text": "A conservative bound."}]
    assert any("banned" in f for f in cp.validate_pack(pack, sections))
    assert (
        cp.validate_pack(pack, [{"id": "do_not_rely", "text": "no alpha here"}]) == []
    )


def test_a_fact_with_an_unknown_grade_or_caveat_or_no_date_is_refused() -> None:
    pack = _pack()
    pack["facts"][0]["grade"] = "Z"
    pack["facts"][1]["caveats"] = ["MADE_UP"]
    pack["facts"][2]["as_of"] = ""
    failures = cp.validate_pack(pack)
    assert any("unknown grade" in f for f in failures)
    assert any("unknown caveat" in f for f in failures)
    assert any("no as-of date" in f for f in failures)


def test_duplicate_fact_ids_are_refused() -> None:
    pack = _pack()
    pack["facts"].append(copy.deepcopy(pack["facts"][0]))
    assert any("duplicate" in f for f in cp.validate_pack(pack))


def test_a_policy_text_that_adds_a_banned_word_stops_the_build() -> None:
    policy = _policy()
    policy["targets"]["method"] += " A conservative caps rule."
    data = _payload()
    decision = pd.build_decision(data, policy, _sha())
    with pytest.raises(cp.PackError, match="banned"):
        cp.build_pack(data, decision, policy, _sha(), _sources())


# --- changes, holes, recommendations ---------------------------------------------------------


def test_diff_between_packs_names_changed_new_and_removed_facts() -> None:
    before = _pack()
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.1300"
    after = _pack(data, prior=before)
    assert "portfolio.drawdown: 12.48 to 13.00" in after["text"]
    changes = cp.diff_packs(before, after)
    assert any(c.startswith("portfolio.drawdown") for c in changes)
    trimmed = copy.deepcopy(before)
    trimmed["facts"] = trimmed["facts"][1:]
    assert any("new fact" in c for c in cp.diff_packs(trimmed, before))
    assert any("no longer present" in c for c in cp.diff_packs(before, trimmed))
    assert cp.diff_packs(before, before) == []
    assert (
        "NO_PRIOR_PACK" in before["text"]
        and "NO_PRIOR_PACK" not in after["text"].split("# 9.")[1].split("# 10.")[0]
    )


def test_data_holes_come_from_the_payload_and_are_numbered() -> None:
    data = _payload()
    data["fact_status"] = {
        "nav": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        "costs": "PARTIAL",
        "fx": "PARTIAL",
    }
    data["fact_coverage_matrix"] = [
        {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
        {"fact_type": "nav", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
    ]
    data["capital_status"] = {
        "systematic": "AVAILABLE",
        "reserve": "NOT_READY_AT_CUTOFF",
    }
    pack = _pack(data)
    ids = [h["id"] for h in pack["holes"]]
    assert ids == [f"H-{n:02d}" for n in range(1, len(ids) + 1)] and len(ids) == 4
    text = pack["text"]
    assert (
        "Fact kind 'costs' is PARTIAL" in text
        and "1 account-fact rows have NO_ACCEPTED_FACT_PATH (flows)" in text
    )
    assert "Capital line 'reserve' is NOT_READY_AT_CUTOFF" in text


def test_open_recommendations_are_listed_with_the_owner_response(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log.jsonl"
    rl.append_row(
        {
            "kind": "RECOMMENDATION",
            "recorded_on": "2026-11-17",
            "run_id": "r1",
            "pack_id": "PACK-0123456789ab",
            "policy_version": "1",
            "date": "2026-11-17",
            "type": "REBALANCE",
            "subject": "s",
            "recommendation": "Trim TT.",
            "rationale": "r",
            "evidence": [{"fact_id": "strategy.tt-portfolio.weight", "value": "23.80"}],
            "confidence": "LOW",
            "rule_ref": "bands",
            "would_change_if": "x",
            "review_on": "2026-11-24",
            "model": "m",
        },
        path,
    )
    rl.append_row(
        {
            "kind": "OWNER_RESPONSE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-18",
            "response": "REJECT",
            "date": "2026-11-18",
            "reason": "wait",
        },
        path,
    )
    data = _payload()
    block = rl.panel_block(path, today="2026-11-19")
    pack = _pack(data, recs=block)
    assert "R-0001 REBALANCE: Trim TT. (owner: REJECT)" in pack["text"]
    assert "Log VERIFIED." in pack["text"]


def test_standing_rules_come_from_the_policy_file() -> None:
    rules = _policy()["standing_rules"]
    text = _pack()["text"]
    assert len(rules) >= 7
    for rule in rules:
        assert f"{rule['id']}: {rule['text']}" in text


# --- real payload and producer ------------------------------------------------------------------


def test_real_payload_pack_fits_and_validates() -> None:
    path = Path(os.environ.get("QE_DECISION_REAL_PAYLOAD", str(REAL_DEFAULT)))
    if not path.is_file():
        pytest.skip("the 330da1a0 payload is not on this machine")
    data = json.loads(path.read_bytes())
    pack = _pack(data)
    assert 5_000 < pack["tokens_estimate"] < cp.CORE_TOKEN_TARGET
    assert (
        len(pack["text"]) < 0.001 * path.stat().st_size
    )  # about one part in ten thousand of the payload
    assert any("Fact kind" in h["what"] for h in pack["holes"])
    assert cp.validate_pack(pack) == []


def test_producer_helpers_build_the_blocks_from_a_payload() -> None:
    import portfolio_control_panel as panel

    data = _payload()
    decision = panel._decision_block(data)
    assert decision is not None and decision["policy"]["policy_version"] == "1"
    data["decision"] = decision
    data["recommendations"] = rl.panel_block(today=decision["evaluated_on"])
    block = panel._context_pack_block(data, decision)
    assert block["pack_id"] == _pack()["pack_id"] and block["policy_version"] == "1"
    assert data["recommendations"]["status"] == "EMPTY"


def test_the_pack_block_matches_the_payload_schema() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json").read_bytes()
    )
    pack = _pack()
    assert not list(
        Draft202012Validator(schema["properties"]["context_pack"]).iter_errors(pack)
    )
    assert not list(
        Draft202012Validator(schema["properties"]["recommendations"]).iter_errors(
            rl.panel_block()
        )
    )
    pack["tokens_estimate"] = 50_000
    assert list(
        Draft202012Validator(schema["properties"]["context_pack"]).iter_errors(pack)
    )


def test_cli_writes_the_pack_files(tmp_path: Path) -> None:
    payload = _payload()
    payload["decision"] = pd.build_decision(payload, _policy(), _sha())
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "out"
    assert cp.main(["--payload", str(path), "--out", str(out)]) == 0
    text = (out / "context-pack.md").read_bytes()
    assert b"\r" not in text and text.decode().rstrip().endswith(
        json.loads((out / "context-pack.json").read_bytes())["pack_id"]
    )


# --- panel tabs ----------------------------------------------------------------------------------

node = shutil.which("node")


def _render(function: str, selector: str, payload: dict, tmp_path: Path) -> str:
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    done = subprocess.run(
        [str(node), str(HARNESS), str(APP), str(path), function],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == [], result["errors"]
    return result["dom"][selector]


@pytest.mark.skipif(node is None, reason="node required")
def test_context_pack_tab_shows_size_holes_and_the_exact_text(tmp_path: Path) -> None:
    pack = _pack()
    dom = _render(
        "renderContextPack", "#contextpack-root", {"context_pack": pack}, tmp_path
    )
    html = dom["innerHTML"]
    assert (
        pack["pack_id"] in html
        and f"{pack['tokens_estimate']} tokens of 40000 allowed" in html
    )
    assert (
        "Download" in html
        and "Known data holes" in html
        and "The text the analyst receives" in html
    )
    text_dom = _render(
        "renderContextPack", "#contextpack-text", {"context_pack": pack}, tmp_path
    )
    assert text_dom["textContent"] == pack["text"]


@pytest.mark.skipif(node is None, reason="node required")
def test_pack_and_recommendation_tabs_say_so_when_the_block_is_missing(
    tmp_path: Path,
) -> None:
    assert (
        "not in this payload"
        in _render(
            "renderContextPack", "#contextpack-root", {"context_pack": None}, tmp_path
        )["innerHTML"]
    )
    assert (
        "not in this payload"
        in _render(
            "renderRecommendations",
            "#recommendations-root",
            {"recommendations": None},
            tmp_path,
        )["innerHTML"]
    )


@pytest.mark.skipif(node is None, reason="node required")
def test_recommendations_tab_renders_records_and_a_broken_log(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    rl.append_row(
        {
            "kind": "RECOMMENDATION",
            "recorded_on": "2026-11-17",
            "run_id": "run-x",
            "pack_id": "PACK-0123456789ab",
            "policy_version": "1",
            "date": "2026-11-17",
            "type": "REBALANCE",
            "subject": "TT <b>above</b> band",
            "recommendation": "Trim.",
            "rationale": "Above band.",
            "evidence": [{"fact_id": "strategy.tt-portfolio.weight", "value": "23.80"}],
            "confidence": "LOW",
            "rule_ref": "bands",
            "would_change_if": "zone normal",
            "review_on": "2026-11-24",
            "model": "m",
        },
        path,
    )
    block = rl.panel_block(path, today="2026-12-20")
    html = _render(
        "renderRecommendations",
        "#recommendations-root",
        {"recommendations": block},
        tmp_path,
    )["innerHTML"]
    assert (
        "R-0001" in html
        and "Not answered yet" in html
        and "4 weeks: due Dec 15, 2026 (overdue)" in html
    )
    assert (
        "PACK-0123456789ab" in html and "strategy.tt-portfolio.weight = 23.80" in html
    )
    assert "<b>" not in html  # escaped
    empty = {
        "status": "EMPTY",
        "reason": None,
        "rows": 0,
        "records": [],
        "head_sha256": rl.GENESIS,
    }
    assert (
        "No recommendations logged yet."
        in _render(
            "renderRecommendations",
            "#recommendations-root",
            {"recommendations": empty},
            tmp_path,
        )["innerHTML"]
    )
    broken = {
        "status": "LOG_BROKEN",
        "reason": "line 2: the chain is broken",
        "rows": 0,
        "records": [],
        "head_sha256": None,
    }
    assert (
        "Do not rely on it"
        in _render(
            "renderRecommendations",
            "#recommendations-root",
            {"recommendations": broken},
            tmp_path,
        )["innerHTML"]
    )


def test_decisions_area_has_three_linked_tabs_in_order() -> None:
    html = (
        (ROOT / "apps" / "portfolio-control-panel" / "index.html").read_bytes().decode()
    )
    order = [
        html.index(f'data-nav="{name}"')
        for name in ("decision", "contextpack", "recommendations", "command")
    ]
    assert order == sorted(order)
    for view in ("contextpack", "recommendations"):
        assert f'data-view="{view}"' in html
    app = APP.read_bytes().decode()
    assert (
        "contextpack: renderContextPack" in app
        and "recommendations: renderRecommendations" in app
    )

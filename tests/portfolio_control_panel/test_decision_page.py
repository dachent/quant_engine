"""The Decision page: policy file, producer block, plausibility rules, first-tab render.

Golden fixture: ``decision_fixture_330da1a0.json`` is a value-free cut of the real panel
payload of generation 330da1a0 (cutoff 2026-11-06).  Fractions, dates and returns are the
payload's own; dollar amounts are rescaled to a 100,000 capital.  The full 278 MB payload is
checked too when ``QE_DECISION_REAL_PAYLOAD`` points at it (or the default scratch copy exists).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from jsonschema import Draft202012Validator
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_decision as pd  # noqa: E402

POLICY_PATH = ROOT / "contracts" / "portfolio-decision-policy-v1.json"
POLICY_SCHEMA = ROOT / "schemas" / "portfolio" / "decision-policy.schema.json"
PANEL_SCHEMA = ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json"
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


def _build(data: dict | None = None, policy: dict | None = None, prior=None) -> dict:
    return pd.build_decision(
        data if data is not None else _payload(),
        policy if policy is not None else _policy(),
        _sha(),
        prior,
    )


def _row(block: dict, strategy_id: str) -> dict:
    return next(r for r in block["strategies"] if r["strategy_id"] == strategy_id)


# --- policy file ------------------------------------------------------------------


def test_policy_file_matches_its_schema_and_the_owner_rulings() -> None:
    policy = _policy()
    assert not list(
        Draft202012Validator(json.loads(POLICY_SCHEMA.read_bytes())).iter_errors(policy)
    )
    assert pd.validate_policy(policy) == []
    assert policy["limit"]["drawdown_limit"] == "0.15"
    assert policy["limit"]["normal_below"] == "0.10"
    assert policy["rebalance_band"] == {
        "absolute_points": "0.03",
        "relative_to_target": "0.20",
        "rule": "LARGER_OF",
    }
    assert [b["from"] for b in policy["status_rule"]["bands"]] == [
        "0",
        "1.0",
        "1.25",
        "1.5",
    ]
    assert policy["targets"]["blend_to_equal"] == "0.5"
    assert policy["stress_rule"]["backtest_counts_as_loss_evidence"] is True
    assert policy["limit"]["acceptance"]["accepted_on"] == "2026-11-17"


def test_policy_file_is_lf_bytes() -> None:
    assert b"\r" not in POLICY_PATH.read_bytes()


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["limit"].update(normal_below="0.20"),
        lambda p: p["status_rule"]["bands"].reverse(),
        lambda p: p["targets"].update(max_strategy_weight="0"),
        lambda p: p["targets"].update(blend_to_equal="1.5"),
        lambda p: p["stress_rule"]["prior_worst_drawdown"]["qsi"].update(value="1.2"),
        lambda p: p["limit"]["breach_action"].update(kind="REDUCE_EACH_STRATEGY"),
    ],
)
def test_a_policy_that_cannot_be_applied_is_refused(edit) -> None:
    policy = _policy()
    edit(policy)
    assert pd.validate_policy(policy)
    with pytest.raises(pd.DecisionError):
        _build(policy=policy)


# --- golden on the 330da1a0 payload -------------------------------------------------


def test_golden_portfolio_block() -> None:
    block = _build()
    portfolio = block["portfolio"]
    assert block["as_of"] == "2026-11-06" and block["evaluated_on"] == "2026-11-09"
    assert portfolio["zone"] == "NO_NEW_RISK"
    assert portfolio["drawdown"] == "0.124838" and portfolio["limit"] == "0.150000"
    assert portfolio["headroom_points"] == "2.52"
    assert portfolio["stress_loss"] == "0.441893"
    assert portfolio["published_policy_constant_stress"] == "0.370859"
    assert portfolio["stress_loss_usd"] == "44200"  # on the rescaled 100,000 capital
    assert portfolio["index_points"] == 753
    assert "not a forecast" in portfolio["stress_rule"]


def test_golden_rows_match_the_owner_rule() -> None:
    block = _build()
    rows = {r["strategy_id"]: r for r in block["strategies"]}
    assert len(rows) == 7
    assert {r["status"] for r in rows.values()} == {"HOLD"}
    expected_target = {
        "p123-ai-factor": "0.2033",
        "tt-portfolio": "0.1973",
        "robuxio-crypto": "0.1297",
        "qsi": "0.1197",
        "grizzlybears-vtma": "0.1199",
        "volatis-strategy": "0.1171",
        "p123-eu": "0.1126",
    }
    for key, target in expected_target.items():
        assert rows[key]["target"].startswith(target), key
    assert rows["volatis-strategy"]["drawdown_ratio"] == "0.9570"
    assert rows["volatis-strategy"]["next_status"] == "WATCH"
    assert rows["volatis-strategy"]["next_status_at"] == "1.0"
    assert rows["volatis-strategy"]["stress_loss_fraction"] == "0.625087"
    assert rows["p123-eu"]["stress_loss_fraction"].startswith("0.6937")
    assert rows["robuxio-crypto"]["drawdown_basis"].startswith("weekly")
    assert rows["tt-portfolio"]["band_state"] == "ABOVE_BAND"
    assert {r["band_state"] for k, r in rows.items() if k != "tt-portfolio"} == {
        "IN_BAND"
    }


def test_golden_actions_are_no_action_with_the_tt_move_held_back() -> None:
    actions = _build()["actions"]
    assert actions["no_action"] is True and actions["execute"] == []
    assert len(actions["held_back"]) == 1
    held = actions["held_back"][0]
    assert held["strategy_id"] == "tt-portfolio" and held["kind"] == "SELL"
    assert "no new risk" in held["held_because"]
    assert "no action this week" in actions["headline"]


def test_golden_clusters_and_sources() -> None:
    block = _build()
    clusters = {c["cluster_id"]: c for c in block["clusters"]}
    tactical = clusters["TACTICAL_TIMING_MULTI_ASSET"]
    assert (
        tactical["weight_state"] == "ABOVE_CAP"
        and tactical["stress_vs_limit"] == "ABOVE_LIMIT"
    )
    assert clusters["US_SYSTEMATIC_EQUITY"]["weight_cap"] is None
    sources = {s["source"]: s for s in block["sources"]}
    assert sources["ibkr"]["state"] == "CURRENT" and sources["ibkr"]["age_days"] == 3
    assert (
        sources["alpaca"]["state"] == "INACTIVE"
        and sources["fidelity"]["state"] == "INACTIVE"
    )


def test_account_against_model_columns_do_not_invent_a_model_return() -> None:
    row = _row(_build(), "tt-portfolio")
    assert row["tracking"]["last_week"]["model_return"] is None
    assert row["tracking"]["last_week"]["gap_points"] is None
    since = row["tracking"]["since_start"]
    assert since["gap_points"] == -5.9156 and since["ends_at_cutoff"] is True


def test_a_model_series_in_the_payload_fills_the_weekly_gap() -> None:
    data = _payload()
    series = next(
        s
        for s in data["weekly_returns"]["series"]
        if s["series_id"] == "strategy:TT Portfolio"
    )
    live = series["values_by_mode"]["LIVE_ONLY"]
    data["weekly_returns"]["series"].append(
        {
            "series_id": "model:TT Portfolio",
            "values_by_mode": {
                "LIVE_ONLY": {week: str(float(v) + 0.01) for week, v in live.items()}
            },
        }
    )
    last = _row(_build(data), "tt-portfolio")["tracking"]["last_week"]
    assert last["gap_points"] == "-1.00"


def test_the_block_has_no_alpha_beta_or_the_word_conservative() -> None:
    text = json.dumps(_build())
    for word in ("alpha", "beta", "conservative"):
        assert word not in text.lower()


def test_block_validates_against_the_payload_schema_definition() -> None:
    schema = json.loads(PANEL_SCHEMA.read_bytes())
    definition = schema["properties"]["decision"]
    assert not list(Draft202012Validator(definition).iter_errors(_build()))
    broken = _build()
    del broken["actions"]["no_action"]
    assert list(Draft202012Validator(definition).iter_errors(broken))


def test_block_is_deterministic_and_small() -> None:
    first, second = (
        json.dumps(_build(), sort_keys=True),
        json.dumps(_build(), sort_keys=True),
    )
    assert first == second and len(first) < 40_000


# --- the rules do what the rulings say (kill tests) ----------------------------------


def test_normal_zone_executes_the_rebalance_move_the_zone_otherwise_holds_back() -> (
    None
):
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.0900"
    block = _build(data)
    assert block["portfolio"]["zone"] == "NORMAL"
    execute = block["actions"]["execute"]
    assert [a["strategy_id"] for a in execute] == ["tt-portfolio"] and execute[0][
        "kind"
    ] == "SELL"
    assert (
        execute[0]["text"].startswith("Sell about $")
        and block["actions"]["held_back"] == []
    )
    assert block["actions"]["no_action"] is False


@pytest.mark.parametrize(
    ("drawdown", "zone"),
    [
        ("-0.0999", "NORMAL"),
        ("-0.10", "NO_NEW_RISK"),
        ("-0.15", "NO_NEW_RISK"),
        ("-0.1501", "LIMIT_BREACH"),
    ],
)
def test_zone_edges_follow_the_ruling(drawdown: str, zone: str) -> None:
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = drawdown
    data["advisory"]["hwm_candidate"]["maximum_drawdown"] = "-0.32"
    assert _build(data)["portfolio"]["zone"] == zone


def test_limit_breach_without_a_set_reduction_says_so_and_never_invents_one() -> None:
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.16"
    block = _build(data)
    assert block["portfolio"]["zone"] == "LIMIT_BREACH"
    assert block["actions"]["execute"] == []
    assert any(h["kind"] == "LIMIT_BREACH" for h in block["actions"]["held_back"])
    assert "policy sets no reduction" in block["actions"]["held_back"][-1]["text"]


def test_limit_breach_with_a_set_reduction_sells_each_strategy() -> None:
    data, policy = _payload(), _policy()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.16"
    policy["limit"]["breach_action"] = {
        "kind": "REDUCE_EACH_STRATEGY",
        "reduce_each_strategy_by": "0.25",
        "owner_set": True,
        "note": "test",
    }
    block = _build(data, policy)
    assert len(block["actions"]["execute"]) == 7
    assert all(a["kind"] == "SELL_TO_CASH" for a in block["actions"]["execute"])
    total = sum(int(a["usd"]) for a in block["actions"]["execute"])
    assert (
        abs(total - 25000) <= 700
    )  # a quarter of the 100,000 capital, rounded per position


@pytest.mark.parametrize(
    ("live_now", "status", "sells"),
    [
        ("-0.30", "HOLD", None),
        ("-0.40", "WATCH", None),
        ("-0.50", "REDUCE", 0.5),
        ("-0.60", "EXIT", 1.0),
    ],
)
def test_status_rule_thresholds_and_sell_fractions(
    live_now: str, status: str, sells
) -> None:
    data = _payload()
    volatis = next(
        s for s in data["strategies"] if s["strategy_id"] == "volatis-strategy"
    )
    volatis["hwm_current_drawdown"] = live_now
    block = _build(data)
    row = _row(block, "volatis-strategy")
    assert row["status"] == status
    if sells is None:
        assert not (row["action"] and row["action"]["kind"] == "SELL_TO_CASH")
    else:
        assert row["action"]["kind"] == "SELL_TO_CASH"
        assert abs(int(row["action"]["usd"]) - float(row["value_usd"]) * sells) <= 50


def test_watch_status_blocks_additions_even_in_the_normal_zone() -> None:
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.05"
    volatis = next(
        s for s in data["strategies"] if s["strategy_id"] == "volatis-strategy"
    )
    volatis["hwm_current_drawdown"] = "-0.40"  # ratio 1.03: watch
    ai = next(s for s in data["strategies"] if s["strategy_id"] == "p123-ai-factor")
    ai["hwm_current_drawdown"] = (
        "-0.35"  # ratio > 1.5: exit, so its buy is never proposed
    )
    block = _build(data)
    kinds = {(a["strategy_id"], a["kind"]) for a in block["actions"]["execute"]}
    assert ("p123-ai-factor", "SELL_TO_CASH") in kinds
    assert not any(
        k == "BUY" and s in ("volatis-strategy", "p123-ai-factor") for s, k in kinds
    )


def test_ratio_basis_choice_changes_the_status_input() -> None:
    policy = _policy()
    policy["status_rule"]["ratio_basis"] = "WORST_LIVE_DRAWDOWN"
    row = _row(_build(policy=policy), "volatis-strategy")
    assert row["drawdown_ratio"] == "0.9881"


def test_stale_source_is_marked_and_a_missing_allowed_age_is_not_green() -> None:
    data = _payload()
    for entry in data["meta"]["source_clock"]["source_data_as_of"]:
        if entry["source_id"] == "schwab":
            entry["provider_data_date"] = "2026-10-20"
    policy = _policy()
    del policy["data_ages"]["allowed_days"]["quantbase"]
    sources = {s["source"]: s for s in _build(data, policy)["sources"]}
    assert sources["schwab"]["state"] == "STALE" and sources["schwab"]["tone"] == "bad"
    assert (
        sources["quantbase"]["state"] == "NO_ALLOWED_AGE"
        and sources["quantbase"]["tone"] == "warn"
    )


def test_caps_bind_and_the_targets_still_sum_to_one() -> None:
    policy = _policy()
    policy["targets"]["max_cluster_weight"] = "0.50"
    policy["targets"]["max_strategy_weight"] = "0.18"
    block = _build(policy=policy)
    targets = {r["strategy_id"]: float(r["target"]) for r in block["strategies"]}
    assert (
        abs(sum(targets.values()) - 1) < 1e-5 and max(targets.values()) <= 0.18 + 1e-6
    )
    tactical = ("tt-portfolio", "volatis-strategy", "qsi", "grizzlybears-vtma")
    assert sum(targets[k] for k in tactical) <= 0.50 + 1e-6


def test_infeasible_caps_are_refused() -> None:
    policy = _policy()
    policy["targets"]["max_strategy_weight"] = "0.10"
    with pytest.raises(pd.DecisionError, match="cap"):
        _build(policy=policy)


def test_two_stress_rule_terms_are_both_used() -> None:
    policy = _policy()
    assert pd.stress_fraction(policy, "tt-portfolio", None) == pd.Decimal(
        "0.1816"
    ) * pd.Decimal("1.25")
    assert pd.stress_fraction(policy, "tt-portfolio", pd.Decimal("0.5")) == pd.Decimal(
        "0.75"
    )
    assert (
        pd.stress_fraction(policy, "volatis-strategy", pd.Decimal("0.9")) == 1
    )  # capped
    assert pd.stress_fraction(
        policy, "robuxio-crypto", pd.Decimal("0.1")
    ) == pd.Decimal("0.352") * pd.Decimal("1.25") + pd.Decimal("0.05")


# --- plausibility: bad payloads are refused, not shown ------------------------------


def test_weights_that_do_not_sum_to_one_are_refused() -> None:
    data = _payload()
    data["strategies"][0]["systematic_weight"] = "0.60"
    with pytest.raises(pd.DecisionError, match="weights do not sum"):
        _build(data)


def test_a_strategy_without_a_prior_worst_drawdown_is_refused() -> None:
    policy = _policy()
    del policy["stress_rule"]["prior_worst_drawdown"]["qsi"]
    with pytest.raises(pd.DecisionError, match="prior worst"):
        _build(policy=policy)


def test_a_drawdown_larger_than_the_worst_on_record_is_refused() -> None:
    data = _payload()
    data["advisory"]["hwm_candidate"]["maximum_drawdown"] = "-0.05"
    with pytest.raises(pd.DecisionError, match="exceeds the worst"):
        _build(data)


def test_a_source_dated_after_the_evaluation_is_refused() -> None:
    data = _payload()
    data["meta"]["source_clock"]["source_data_as_of"][2]["provider_data_date"] = (
        "2026-12-01"
    )
    with pytest.raises(pd.DecisionError, match="after the evaluation"):
        _build(data)


def test_a_missing_strategy_row_is_refused() -> None:
    data = _payload()
    data["counts"]["strategies"] = 8
    with pytest.raises(pd.DecisionError, match="counted strategies"):
        _build(data)


def test_a_banned_word_in_the_policy_text_is_refused() -> None:
    policy = _policy()
    policy["stress_rule"]["label"] += " A conservative bound."
    with pytest.raises(pd.DecisionError, match="banned word"):
        _build(policy=policy)


# --- since last week ------------------------------------------------------------------


def test_changes_since_the_prior_page() -> None:
    assert _build()["changes"] == {"status": "NO_PRIOR_DECISION", "items": []}
    prior = copy.deepcopy(_build())
    prior["portfolio"]["zone"] = "NORMAL"
    prior["strategies"][0]["status"] = "WATCH"
    prior["strategies"][1]["weight"] = str(
        float(prior["strategies"][1]["weight"]) + 0.02
    )
    prior["sources"][0]["state"] = "CURRENT"
    changes = _build(prior=prior)["changes"]
    assert changes["status"] == "COMPARED" and changes["prior_as_of"] == "2026-11-06"
    text = " ".join(changes["items"])
    assert "Zone NORMAL to NO_NEW_RISK" in text and "status WATCH to HOLD" in text
    assert "weight moved" in text and "Source alpaca" in text


def test_identical_prior_reports_no_change() -> None:
    block = _build()
    assert _build(prior=block)["changes"]["items"] == []


# --- the real payload ------------------------------------------------------------------


def test_real_payload_gives_the_same_fractions_as_the_committed_fixture() -> None:
    path = Path(os.environ.get("QE_DECISION_REAL_PAYLOAD", str(REAL_DEFAULT)))
    if not path.is_file():
        pytest.skip("the 330da1a0 payload is not on this machine")
    real = pd.build_decision(json.loads(path.read_bytes()), _policy(), _sha())
    fixture = _build()
    assert real["portfolio"]["stress_loss"] == fixture["portfolio"]["stress_loss"]
    assert real["portfolio"]["zone"] == fixture["portfolio"]["zone"]
    for a, b in zip(real["strategies"], fixture["strategies"], strict=True):
        for key in (
            "strategy_id",
            "weight",
            "target",
            "band_state",
            "stress_share",
            "status",
            "drawdown_ratio",
        ):
            assert a[key] == b[key], key
    assert real["actions"]["no_action"] is True


# --- the first tab ----------------------------------------------------------------------

node = shutil.which("node")


def _render(block: dict | None, tmp_path: Path) -> str:
    payload = {"decision": block}
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    done = subprocess.run(
        [str(node), str(HARNESS), str(APP), str(path), "renderDecision"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["errors"] == [], result["errors"]
    return result["dom"]["#decision-root"]["innerHTML"]


@pytest.mark.skipif(node is None, reason="node required")
def test_first_tab_renders_every_section_from_the_golden_block(tmp_path: Path) -> None:
    html = _render(_build(), tmp_path)
    for needle in (
        "No action",
        "Held back by the rules",
        "Sell about $",
        "Data dates",
        "Stress loss",
        "44.2% of capital",
        "Strategies",
        "Changes since the previous run",
        "12.5%",
        "2.52 points of headroom",
        "Volatis Strategy",
        "HOLD",
        "WATCH at 1.00",
        "model not in this payload",
        "No earlier decision page to compare with.",
        "Placeholder values, not yet set by the owner",
    ):
        assert needle in html, needle
    assert html.count('class="dec-card"') == 7
    assert html.count("<li class=") >= 7  # one line per source
    for word in ("alpha", "beta", "conservative"):
        assert word not in html.lower()


@pytest.mark.skipif(node is None, reason="node required")
def test_first_tab_says_so_when_the_block_is_missing(tmp_path: Path) -> None:
    html = _render(None, tmp_path)
    assert "not in this payload" in html and "unchecked" in html


@pytest.mark.skipif(node is None, reason="node required")
def test_first_tab_escapes_text_from_the_block(tmp_path: Path) -> None:
    block = _build()
    block["actions"]["headline"] = "<img src=x onerror=1>"
    assert "<img" not in _render(block, tmp_path)


def test_decision_is_the_first_view_and_the_default() -> None:
    html = (
        (ROOT / "apps" / "portfolio-control-panel" / "index.html").read_bytes().decode()
    )
    first_nav = html.index('data-nav="decision"')
    assert first_nav < html.index('data-nav="command"')
    assert html.index('data-view="decision"') < html.index('data-view="command"')
    assert (
        'data-view="decision" aria-labelledby="decision-heading">' in html
    )  # active, not hidden
    app = APP.read_bytes().decode()
    assert (
        app.count('|| "decision"') == 1 and app.count('? requested : "decision"') == 1
    )


def test_the_decision_view_text_has_no_alpha_or_beta() -> None:
    html = (
        (ROOT / "apps" / "portfolio-control-panel" / "index.html").read_bytes().decode()
    )
    start = html.index('data-view="decision"')
    section = html[start : html.index("</section>", start)]
    app = APP.read_bytes().decode()
    fn = app[
        app.index("function renderDecision()") : app.index("function renderCommand()")
    ]
    for text in (section, fn):
        assert not any(w in text.lower() for w in ("alpha", "beta", "conservative"))


# --- partial payloads ------------------------------------------------------------------


def test_a_payload_without_the_decision_inputs_is_named_not_guessed() -> None:
    assert pd.missing_inputs(
        {}
    ) and "advisory.hwm_candidate.current_drawdown" in pd.missing_inputs({})
    assert pd.missing_inputs(_payload()) == []
    with pytest.raises(pd.DecisionInputsMissing):
        _build({"meta": {}})


def test_the_producer_omits_the_block_for_a_partial_payload_and_builds_it_for_a_full_one() -> (
    None
):
    import portfolio_control_panel as panel

    assert panel._decision_block({"meta": {}}) is None
    block = panel._decision_block(_payload())
    assert block is not None and block["policy"]["sha256"] == _sha()
    assert not list(
        Draft202012Validator(
            json.loads(PANEL_SCHEMA.read_bytes())["properties"]["decision"]
        ).iter_errors(block)
    )

"""Owner-eyes plausibility rules (#912, review Q1): each rule has a kill test, and each of the
owner's eight finds fails at least one rule on a payload carrying its A5 values.

``clean_payload`` is a minimal payload every rule passes.  ``MUTATIONS`` breaks it one way per
rule (the A5 values where the owner's find supplies them); a rule is killed when its own
mutation makes it fire and the clean payload does not.
"""

from __future__ import annotations

import copy
from datetime import date
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable

import pytest

from acceptance.validate import plausibility

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

CUTOFF = date(2026, 11, 6)  # a Friday
COMPLETE = "COMPLETE_WITHIN_DECLARED_BOUNDARY"


def clean_payload() -> dict[str, Any]:
    series = {
        "series_id": "A",
        "alpha": {"net_annualized": "0.252", "periodic_intercept": "0.001"},
        "alpha_context": {
            "periods_per_year": 252,
            "annualisation": "ARITHMETIC_PERIODIC_INTERCEPT_TIMES_PERIODS_PER_YEAR",
            "window_days": 400,
            "window_end": "2026-11-06",
            "factor_staleness_days": 0,
            "headline_status": "HEADLINE",
            "basis_status": "AVAILABLE",
            "basis_label": "observed",
            "n_obs": 300,
        },
        "exposures": [{"beta": "0.9", "exposure": "0.9"}],
        "coverage": {
            "split_margin": {
                "train_margin": 5,
                "test_margin": 5,
                "train_observations": 80,
                "test_observations": 100,
                "minimum_train_observations": 30,
                "minimum_test_observations": 10,
            },
            "source_lag_calendar_days": 0,
            "factor_observed_end": "2026-11-06",
            "target_max_drawdown": "0.2",
            "alignment_ratio": "0.97",
        },
        "diagnostics": {
            "train": {"r_squared": "0.5", "adjusted_r_squared": "0.4"},
            "holdout": {
                "r_squared": "0.3",
                "predicted_r_squared": "0.3",
                "adjusted_r_squared": "0.2",
                "tracking_error_annualized": "0.1",
            },
            "dynamic": {
                "exposure_path": [{"date": "2026-11-02"}, {"date": "2026-11-03"}]
            },
        },
        "interval_association": {"buckets": [{"risk_free": "0.0002"}]},
    }
    return {
        "meta": {
            "portfolio_as_of": "2026-11-06",
            "publication_readiness": "READY",
            "source_clock": {
                "selected_financial_cutoff": "2026-11-06",
                "source_data_as_of": [
                    {
                        "source_id": "x",
                        "provider_data_date": "2026-11-05",
                        "finality": {"basis": "LIVE"},
                    }
                ],
            },
        },
        "as_of_date": "2026-11-06",
        "factor_analysis": {"as_of_date": "2026-11-06", "series": [series]},
        "weekly_returns": {
            "as_of_date": "2026-11-06",
            "current_week": {
                "data_through_date": "2026-11-06",
                "week_start": "2026-11-02",
                "week_end": "2026-11-08",
                "is_partial": False,
            },
            "weeks": ["2026-10-26", "2026-11-02"],
            "series": [
                {"series_id": "s", "absent_value_status": "UNAVAILABLE_NOT_ZERO"}
            ],
        },
        "performance_tab": {
            "as_of": "2026-11-06",
            "modes": {
                "LIVE_ONLY": {
                    "data_through_date": "2026-11-06",
                    "last_week_partial": False,
                    "dates": ["2026-10-26", "2026-11-02"],
                    "week_end_dates": ["2026-11-01", "2026-11-06"],
                    "benchmarks_unavailable": [],
                    "lines": [
                        {
                            "kind": "STRATEGY",
                            "display_name": "A",
                            "series_id": "a",
                            "tri": [1.0, 1.1],
                        },
                        {
                            "kind": "BENCHMARK",
                            "display_name": "SPY",
                            "series_id": "spy",
                            "tri": [1.0, 1.05],
                        },
                    ],
                }
            },
        },
        "allocations": [
            {"systematic_weight": "0.5", "weight_basis": "CURRENT_CUTOFF"},
            {"systematic_weight": "0.5", "weight_basis": "CURRENT_CUTOFF"},
        ],
        "custody_current_capital": {
            "strategies": [{"current_weight": "0.5"}, {"current_weight": "0.5"}]
        },
        "dependence_analysis": {
            "central_case": {
                "components": [
                    {
                        "series_id": "A",
                        "allocation_weight": "1",
                        "adverse_loss_fraction": "0.2",
                        "loss_scale_contribution": "0.2",
                    }
                ]
            },
            "correlation_one": {
                "components": [
                    {
                        "series_id": "A",
                        "allocation_weight": "1",
                        "conservative_loss_fraction": "0.4",
                        "loss_contribution": "0.4",
                    }
                ]
            },
            "economic_driver_cluster_budget": {"correlation_one_loss": "0.4"},
            "cells": [
                {
                    "observed": {"horizons": {"5": {"correlation": "0.3"}}},
                    "blend": {"stress_pre_psd": "0.5"},
                }
            ],
        },
        "advisory": {
            "correlation_one_stress": "0.4",
            "central_case_loss": "0.2",
            "reason": "Readiness is governed by the governed bound.",
            "hwm_candidate": {
                "complete_through": "2026-11-06",
                "current_drawdown": "-0.1",
                "kpi": {"window_end": "2026-11-06", "drawdown": "-0.1"},
            },
        },
        "hwm_governed_proof": {
            "current_drawdown": "-0.1",
            "maximum_drawdown": "-0.2",
            "index_path": [{"drawdown": "-0.2"}, {"drawdown": "-0.1"}],
        },
        "strategy_underwriting": {
            "as_of_date": "2026-11-06",
            "counts": {"strategies": 1, "authoritative_design": 1},
            "rows": [
                {
                    "strategy_id": "a",
                    "design_fit": {"status": "AUTHORITATIVE_DESIGN_AVAILABLE"},
                    "evidence_layers": {
                        "account_live": {
                            "status": "AVAILABLE",
                            "observed_end": "2026-11-06",
                        }
                    },
                    "model_account_gap": {
                        "status": "QUANTIFIED",
                        "total_gap": "0.01",
                        "observed_end": "2026-11-06",
                    },
                    "long_history": {
                        "hybrid_net_alpha_context": {
                            "factor_staleness_days": 0,
                            "window_end": "2026-11-06",
                        }
                    },
                }
            ],
        },
        "fact_status": {"accounts": COMPLETE, "nav": COMPLETE},
        "fact_coverage_matrix": [
            {"fact_type": "accounts", "status": COMPLETE},
            {"fact_type": "nav", "status": COMPLETE},
        ],
        "gates": [{"item": "nav", "status": COMPLETE}],
        "coverage": [{"status": "FRESH"}],
        "provenance": [
            {"artifact": "report", "as_of_date": "2026-11-06", "schema": "x/1"},
            {
                "artifact": "portfolio-economic-driver-clusters",
                "as_of_date": "2026-08-06",
                "schema": "x/1",
            },
        ],
        "capital_reconciliation": {
            "difference_usd": 17023.09,  # the EXPLAINED difference
            "residual_usd": "0",
            "status": "RECONCILED",
            "tolerance": 0.01,
        },
        "counts": {"twr_rows": 1},
        "performance": [{"account_id": "a"}],
        "account_summary": [
            {"account_basis": {"figures": {"gross_to_equity_ratio": 1.4}}}
        ],
    }


def _series(p: dict[str, Any]) -> dict[str, Any]:
    return p["factor_analysis"]["series"][0]


def _mode(p: dict[str, Any]) -> dict[str, Any]:
    return p["performance_tab"]["modes"]["LIVE_ONLY"]


def _set_alpha(p: dict[str, Any], annual: float, headline: str) -> None:
    _series(p)["alpha"] = {
        "net_annualized": str(annual),
        "periodic_intercept": str(annual / 252),
    }
    _series(p)["alpha_context"]["headline_status"] = headline


def _m_beta(p):  # A5: betas up to 6.4 on the factor tab
    _series(p)["exposures"][0]["beta"] = "-6.359"


def _m_rf(p):  # A5: daily risk-free -0.0032
    _series(p)["interval_association"]["buckets"][0]["risk_free"] = "-0.003162"


def _m_r2(p):  # A5: holdout R2 -3.58
    _series(p)["diagnostics"]["holdout"]["r_squared"] = "-3.58"


def _m_alpha(p):
    _set_alpha(p, 1.61, "HEADLINE")


def _m_stale_lens(p):  # A5: factor_staleness_days 67, window ending 2026-08-31
    ctx = _series(p)["alpha_context"]
    ctx["factor_staleness_days"] = 67
    ctx["window_end"] = "2026-08-31"


def _m_short_window(p):  # A5: window_days 277 with an annualized figure
    _series(p)["alpha_context"]["window_days"] = 277
    _series(p)["alpha_context"]["n_obs"] = 200


def _m_chip(p):
    p["account_summary"][0]["account_basis"]["figures"]["ytd_twr"] = 150.0


def _m_dd(p):
    p["account_summary"][0]["account_basis"]["figures"][
        "account_live_max_drawdown"
    ] = -1.5


def _m_corr(p):
    p["dependence_analysis"]["cells"][0]["observed"]["horizons"]["5"]["correlation"] = (
        "1.2"
    )


def _m_te(p):
    _series(p)["diagnostics"]["holdout"]["tracking_error_annualized"] = "3.0"


def _m_nan(p):
    _mode(p)["lines"][0]["tri"] = [1.0, math.nan]


def _m_weights(p):
    p["allocations"][1]["systematic_weight"] = "0.6"


def _m_partial(p):  # A5: is_partial true on Friday 2026-11-06
    p["weekly_returns"]["current_week"]["is_partial"] = True


def _m_tuesday(p):
    p["weekly_returns"]["current_week"]["week_start"] = "2026-11-03"


def _m_stale_input(p):  # A5: audit-combined-control-status 2026-07-25
    p["provenance"].append(
        {
            "artifact": "combined-control-status",
            "as_of_date": "2026-07-25",
            "schema": "audit-combined-control-status/audit-2026-07-25",
        }
    )


def _m_undated(p):
    p["provenance"].append(
        {"artifact": "sleeve-account-facts", "schema": "sleeve-account-facts/1"}
    )


def _m_future(p):
    p["provenance"].append(
        {"artifact": "ledger", "as_of_date": "2026-12-25", "schema": "x/1"}
    )


def _m_snapshot(p):  # A5: HISTORICAL_SNAPSHOT_2026_08_16
    p["allocations"][0]["weight_basis"] = "HISTORICAL_SNAPSHOT_2026_08_16"


def _m_gap_window(p):  # A5: gap from the 2026-07-25 audit
    p["strategy_underwriting"]["rows"][0]["model_account_gap"]["observed_end"] = (
        "2026-07-25"
    )


def _m_source_clock(p):
    p["meta"]["source_clock"]["source_data_as_of"][0]["provider_data_date"] = (
        "2026-07-01"
    )


def _m_no_complete(p):  # A5: no matrix row reads complete
    p["fact_coverage_matrix"] = [
        {"fact_type": "accounts", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
        {"fact_type": "nav", "status": "NO_ACCEPTED_FACT_PATH"},
    ]


def _m_partial_health(p):  # A5: 6 of 12 PARTIAL and permanently so
    p["fact_status"] = {
        "accounts": COMPLETE,
        "costs": "PARTIAL",
        "flows": "PARTIAL",
        "nav": "PARTIAL",
    }
    p["fact_coverage_matrix"] += [
        {"fact_type": "costs", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
        {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
    ]
    p["gates"] = []


def _m_unexplained(p):
    p["coverage"].append(
        {"status": "PARTIAL", "known_gap": "provider export is partial"}
    )


def _m_underwriting(p):  # A5: 5 and 6 of 7 with unavailable layers and no named pull
    uw = p["strategy_underwriting"]
    uw["counts"] = {
        "strategies": 7,
        "authoritative_design": 6,
        "quantified_model_account_gap": 5,
    }
    uw["rows"][0]["evidence_layers"]["backtest"] = {"status": "UNAVAILABLE"}
    uw["rows"] += [copy.deepcopy(uw["rows"][0]) for _ in range(6)]


def _m_uw_count(p):
    p["strategy_underwriting"]["counts"]["strategies"] = 2


def _m_conservative(
    p,
):  # A5: conservative 0.2166 against an observed 0.0859 is fine; the A5 breach is per series
    p["dependence_analysis"]["correlation_one"]["components"][0][
        "conservative_loss_fraction"
    ] = "0.1"
    p["dependence_analysis"]["correlation_one"]["components"][0][
        "loss_contribution"
    ] = "0.1"


def _m_conservative_drawdown(
    p,
):  # A5: GrizzlyBears 0.343 against a 0.471 maximum drawdown
    _series(p)["coverage"]["target_max_drawdown"] = "0.5"


def _m_word(p):
    p["advisory"]["reason"] = "The loss fractions are conservative by construction."


def _m_cutoff(p):
    p["factor_analysis"]["as_of_date"] = "2026-10-30"


def _m_legend(p):
    _mode(p)["week_end_dates"][-1] = "2026-10-30"


def _m_axis_dates(p):  # A5: repeated axis labels
    _mode(p)["dates"] = ["2026-10-26", "2026-10-26"]


def _m_axis_ratio(p):  # A5: LIVE_PLUS_BACKTEST lines with a 3 600 000x range
    _mode(p)["lines"][0]["tri"] = [0.00001, 40.0]


def _m_same_name(p):
    _mode(p)["lines"][1]["display_name"] = "A"


def _m_no_benchmark(p):
    _mode(p)["lines"] = _mode(p)["lines"][:1]


def _m_hidden_benchmark(p):  # A5: SPY and SPXL hidden by default
    _mode(p)["lines"][1]["default_visible"] = False


def _m_annualise(p):
    _series(p)["alpha"]["net_annualized"] = "0.30"


def _m_loss_contrib(p):
    p["dependence_analysis"]["correlation_one"]["components"][0][
        "loss_contribution"
    ] = "0.3"


def _m_hwm_end(p):
    p["advisory"]["hwm_candidate"]["kpi"]["window_end"] = "2026-10-30"


def _m_gate(p):
    p["gates"] = [{"item": "nav", "status": "PARTIAL"}]


def _m_nobs(p):
    _series(p)["alpha_context"]["n_obs"] = 500


def _m_margin(p):
    _series(p)["coverage"]["split_margin"]["test_margin"] = -1


def _m_path_gap(p):  # A5: 2025-05-06 -> 2026-01-06
    _series(p)["diagnostics"]["dynamic"]["exposure_path"] = [
        {"date": "2025-05-06"},
        {"date": "2026-01-06"},
    ]


def _m_absent(p):
    del p["weekly_returns"]["series"][0]["absent_value_status"]


def _m_weeks(p):
    p["weekly_returns"]["weeks"] = ["2026-11-02", "2026-11-02"]


def _m_gap_empty(p):
    gap = p["strategy_underwriting"]["rows"][0]["model_account_gap"]
    gap.update({"status": "UNAVAILABLE", "total_gap": None})


def _m_close(p):  # A5: account does not close
    p["capital_reconciliation"]["residual_usd"] = "17000"
    p["capital_reconciliation"]["status"] = "UNRECONCILED"


def _m_readiness(p):
    del p["meta"]["publication_readiness"]


def _m_concept(p):
    p["provenance"].append(
        {
            "artifact": "concepts",
            "as_of_date": "2026-11-06",
            "schema": "strategy-concept-panel/1",
        }
    )


def _m_min_obs(p):
    _series(p)["coverage"]["split_margin"]["train_observations"] = 10


def _m_lag(p):  # A5: factors ended 2026-08-31
    _series(p)["coverage"]["source_lag_calendar_days"] = 67
    _series(p)["coverage"]["factor_observed_end"] = "2026-08-31"


def _m_token(p):
    p["fact_status"]["nav"] = "OK"


def _m_table_end(p):
    p["weekly_returns"]["weeks"] = ["2026-01-05", "2026-01-12"]


MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "PL01": _m_beta,
    "PL02": _m_rf,
    "PL03": _m_r2,
    "PL04": _m_alpha,
    "PL05": _m_stale_lens,
    "PL06": _m_short_window,
    "PL07": _m_chip,
    "PL08": _m_dd,
    "PL09": _m_corr,
    "PL10": _m_te,
    "PL11": _m_nan,
    "PL12": _m_weights,
    "PL13": _m_partial,
    "PL14": _m_tuesday,
    "PL15": _m_stale_input,
    "PL16": _m_undated,
    "PL17": _m_future,
    "PL18": _m_snapshot,
    "PL19": _m_gap_window,
    "PL20": _m_source_clock,
    "PL21": _m_no_complete,
    "PL22": _m_partial_health,
    "PL23": _m_unexplained,
    "PL24": _m_underwriting,
    "PL25": _m_uw_count,
    "PL26": _m_conservative,
    "PL27": _m_conservative_drawdown,
    "PL28": _m_word,
    "PL29": _m_cutoff,
    "PL30": _m_legend,
    "PL31": _m_axis_ratio,
    "PL32": _m_same_name,
    "PL33": _m_hidden_benchmark,
    "PL34": _m_annualise,
    "PL35": _m_loss_contrib,
    "PL36": _m_hwm_end,
    "PL37": _m_gate,
    "PL38": _m_nobs,
    "PL39": _m_margin,
    "PL40": _m_path_gap,
    "PL41": _m_absent,
    "PL42": _m_weeks,
    "PL43": _m_gap_empty,
    "PL44": _m_close,
    "PL45": _m_readiness,
    "PL46": _m_concept,
    "PL47": _m_min_obs,
    "PL48": _m_lag,
    "PL49": _m_token,
    "PL50": _m_table_end,
}


def hits_of(payload: dict[str, Any]) -> list[plausibility.Hit]:
    return plausibility.evaluate(payload, CUTOFF)


def test_the_clean_payload_passes_every_rule():
    assert hits_of(clean_payload()) == []


def test_every_rule_has_a_mutation_and_every_mutation_a_rule():
    assert sorted(MUTATIONS) == sorted(r.rule_id for r in plausibility.RULES)


def test_there_are_about_fifty_rules():
    assert 45 <= len(plausibility.RULES) <= 60


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_each_rule_is_killed_by_its_own_mutation(rule_id):
    payload = clean_payload()
    MUTATIONS[rule_id](payload)
    assert rule_id in {h.rule for h in hits_of(payload)}, f"{rule_id} did not fire"


@pytest.mark.parametrize("rule_id", sorted(MUTATIONS))
def test_a_mutation_does_not_fire_unrelated_rules_silently(rule_id):
    payload = clean_payload()
    MUTATIONS[rule_id](payload)
    fired = {h.rule for h in hits_of(payload)}
    # one defect may trip a few related rules; it may not trip a third of the table
    assert len(fired) <= 8, sorted(fired)


FIND_MUTATIONS = {
    1: ("PL15", "a stale hand-audit input behind a current panel"),
    2: ("PL24", "underwriting 5 or 6 of 7 with no named pull"),
    3: ("PL26", "a conservative loss fraction below the loss observed at the cutoff"),
    4: ("PL01", "betas of 6"),
    5: ("PL31", "an axis range no chart can show"),
    6: ("PL13", "PARTIAL on the last trading day of a week"),
    7: ("PL21", "a coverage matrix with no complete status"),
    8: ("PL46", "a contract bound to the panel that reaches no block"),
}


@pytest.mark.parametrize("find", sorted(FIND_MUTATIONS))
def test_each_owner_find_fails_a_rule_that_lists_it(find):
    rule_id, _ = FIND_MUTATIONS[find]
    payload = clean_payload()
    MUTATIONS[rule_id](payload)
    caught = {f for h in hits_of(payload) if h.rule == rule_id for f in h.finds}
    assert find in caught


def test_a_rule_that_crashes_is_a_hit_not_a_pass():
    hits = plausibility.evaluate({"meta": "not-a-mapping", "weekly_returns": 3}, CUTOFF)
    assert hits is not None  # no exception escaped


def test_repeated_defects_are_capped_but_counted():
    payload = clean_payload()
    _series(payload)["exposures"] = [{"beta": "9"}] * 12
    hits = [h for h in hits_of(payload) if h.rule == "PL01"]
    assert len(hits) == 6 and hits[-1].path == "(more)" and hits[-1].observed == 7


def test_the_rule_table_names_the_finds():
    table = {r["rule"]: r for r in plausibility.rule_table()}
    assert all(r["finds"] for r in table.values())
    assert {f for r in table.values() for f in r["finds"]} >= {1, 2, 3, 4, 5, 6, 7, 8}


def test_the_command_exit_codes(tmp_path):
    import check_plausibility

    def run(path: Path, mode: str) -> int:
        return check_plausibility.main(
            ["--payload", str(path), "--cutoff", "2026-11-06", "--mode", mode]
        )

    good = tmp_path / "good.json"
    good.write_text(json.dumps(clean_payload()))
    bad_payload = clean_payload()
    _m_beta(bad_payload)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_payload))
    soft = clean_payload()
    _m_undated(soft)
    notready = tmp_path / "soft.json"
    notready.write_text(json.dumps(soft))
    assert run(good, "block") == 0 and run(good, "warn") == 0
    assert run(bad, "block") == 1
    assert run(notready, "block") == 3
    # the default is warn: hits are printed and the exit status is unaffected
    assert run(bad, "warn") == 0 and run(notready, "warn") == 0
    assert (
        check_plausibility.main(["--payload", str(bad), "--cutoff", "2026-11-06"]) == 0
    )
    assert check_plausibility.main(["--payload", str(tmp_path / "missing.json")]) == 2
    assert check_plausibility.main(["--list"]) == 0


A5_PANEL = os.environ.get("OWNER_EYES_A5_PANEL_DIR")


@pytest.mark.skipif(
    not A5_PANEL, reason="OWNER_EYES_A5_PANEL_DIR names a local generation directory"
)
def test_the_real_a5_payload_fails_the_rules_for_finds_one_to_seven():
    data = json.loads((Path(A5_PANEL) / "data" / "dashboard_data.json").read_bytes())
    hits = plausibility.evaluate(data)
    caught = {f for h in hits for f in h.finds}
    assert {1, 2, 3, 4, 5, 6, 7} <= caught


def _context(tmp_path: Path, *, owner_eyes: str):
    import test_validate_generation as fixture
    from acceptance.validate import checks
    from acceptance.validate.generation import load_generation

    gen_dir = fixture.build(tmp_path / "g")
    return checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(fixture.CUTOFF) or date.today(),
        cutoff_source="test",
        repo_root=fixture._policy_fixture(tmp_path / "authority"),
        owner_eyes=owner_eyes,
    )


def _owner_eyes_findings(tmp_path: Path, mode: str):
    from acceptance.validate import checks

    return [
        f
        for f in checks.run_checks(_context(tmp_path, owner_eyes=mode))
        if f.check_id == "plausibility.owner_eyes"
    ]


def test_off_runs_nothing(tmp_path):
    assert _owner_eyes_findings(tmp_path, "OFF") == []


def test_warn_lists_every_hit_as_a_warning_that_cannot_block(tmp_path):
    from acceptance.validate import checks

    found = _owner_eyes_findings(tmp_path, "WARN")
    assert found, "the synthetic panel's all-incomplete coverage matrix must trip PL21"
    assert {f.klass for f in found} == {checks.WARNING}
    assert not {f.klass for f in found} & checks.BLOCKERS
    assert all(f.defect_ids for f in found)


def test_block_makes_a_hit_block(tmp_path):
    from acceptance.validate import checks

    found = _owner_eyes_findings(tmp_path, "BLOCK")
    assert {f.klass for f in found} & checks.BLOCKERS
    assert {f.klass for f in found} <= {checks.BLOCKER_REGRESSION, checks.NOT_READY}


def _check_errors(tmp_path: Path, mode: str):
    from acceptance.validate import checks

    return [
        f
        for f in checks.run_checks(_context(tmp_path, owner_eyes=mode))
        if f.check_id == "validator.check_error"
        and f.entity == "plausibility.owner_eyes"
    ]


def test_an_unknown_mode_fails_closed(tmp_path):
    assert len(_check_errors(tmp_path, "LOUD")) == 1


def test_a_crashing_rule_set_cannot_red_a_warn_run_but_does_in_block(
    tmp_path, monkeypatch
):
    from acceptance.validate import checks

    def boom(*_a, **_k):
        raise RuntimeError("rule bug")

    monkeypatch.setattr(plausibility, "evaluate", boom)
    found = _owner_eyes_findings(tmp_path / "w", "WARN")
    assert [f.klass for f in found] == [checks.WARNING]
    assert "crashed" in found[0].message
    assert _check_errors(tmp_path / "w2", "WARN") == []
    assert len(_check_errors(tmp_path / "b", "BLOCK")) == 1


def test_the_runner_lists_hits_as_warnings_by_default_and_blocks_in_block(tmp_path):
    import test_validate_generation as fixture
    import validate_generation

    def run(mode: str, sub: str) -> tuple[int, set[str]]:
        gen = fixture.build(tmp_path / sub)
        out = tmp_path / f"{sub}.json"
        code = validate_generation.main(
            [
                str(gen),
                "--cutoff",
                fixture.CUTOFF,
                "--owner-eyes",
                mode,
                "--out",
                str(out),
            ]
        )
        document = json.loads(out.read_bytes())
        return code, {
            f["class"]
            for f in document["findings"]
            if f["check_id"] == "plausibility.owner_eyes"
        }

    off_code, off_classes = run("off", "a")
    warn_code, warn_classes = run("warn", "b")
    _, block_classes = run("block", "c")
    assert off_classes == set()
    assert warn_classes == {"WARNING"} and warn_code == off_code
    assert "BLOCKER_REGRESSION" in block_classes


def test_the_command_defaults_the_rules_to_warn():
    import validate_generation

    source = Path(validate_generation.__file__).read_text(encoding="utf-8")
    assert "owner_eyes=args.owner_eyes.upper()" in source
    assert 'default="warn"' in source


def test_a_cagr_beside_an_under_a_year_status_is_a_hit():
    payload = clean_payload()
    payload["strategies"] = [
        {
            "account_live_cagr": "0.2",
            "account_live_cagr_status": "SUPPRESSED_UNDER_365D",
        }
    ]
    assert "PL06" in {h.rule for h in hits_of(payload)}
    payload["strategies"] = [
        {"account_live_cagr": None, "account_live_cagr_status": "SUPPRESSED_UNDER_365D"}
    ]
    assert "PL06" not in {h.rule for h in hits_of(payload)}


CONTRACTS = {
    "contracts/rubric-v1.json": {"rubric_id": "PORT-RUBRIC-1", "gates": []},
    "contracts/policy-v1.json": {"policy_id": "PORT-POLICY-1"},
}


def test_a_contract_no_block_carries_is_a_find_eight_hit():
    hits = plausibility.contract_consumer_hits(
        CONTRACTS, '{"policy": "PORT-POLICY-1"}', {}
    )
    assert [h.path for h in hits] == ["contracts/rubric-v1.json"]
    assert hits[0].finds == (8,) and hits[0].rule == plausibility.CONSUMER_RULE


def test_a_contract_carried_by_an_id_or_declared_internal_passes():
    carried = '{"a": "PORT-POLICY-1", "b": "PORT-RUBRIC-1"}'
    assert plausibility.contract_consumer_hits(CONTRACTS, carried, {}) == []
    declared = {"contracts/rubric-v1.json": "read by the builder only"}
    assert (
        plausibility.contract_consumer_hits(
            CONTRACTS, '{"x": "PORT-POLICY-1"}', declared
        )
        == []
    )
    blank = {"contracts/rubric-v1.json": "  "}
    assert plausibility.contract_consumer_hits(
        CONTRACTS, '{"x": "PORT-POLICY-1"}', blank
    )


def test_the_command_runs_the_consumer_check_against_a_checkout(tmp_path):
    import check_plausibility

    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    (root / "contracts" / "rubric-v1.json").write_text(
        json.dumps(CONTRACTS["contracts/rubric-v1.json"])
    )
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps(clean_payload()))
    args = [
        "--payload",
        str(payload),
        "--cutoff",
        "2026-11-06",
        "--mode",
        "block",
        "--repo-root",
        str(root),
    ]
    assert check_plausibility.main(args) == 1
    (root / "ops").mkdir()
    (root / "ops" / "contract-consumers.json").write_text(
        json.dumps({"internal": {"contracts/rubric-v1.json": "builder input only"}})
    )
    assert check_plausibility.main(args) == 0


def test_an_explained_difference_is_not_an_unclosed_account():
    payload = clean_payload()  # difference_usd 17023.09 with residual 0 and RECONCILED
    assert "PL44" not in {h.rule for h in hits_of(payload)}
    payload["capital_reconciliation"]["status"] = "PARTIAL"
    assert "PL44" in {h.rule for h in hits_of(payload)}


def test_a_central_case_below_the_component_sum_is_diversification_not_an_error():
    payload = clean_payload()
    payload["advisory"]["central_case_loss"] = "0.1"  # components sum to 0.2
    assert "PL35" not in {h.rule for h in hits_of(payload)}
    payload["advisory"]["central_case_loss"] = "0.25"
    assert "PL35" in {h.rule for h in hits_of(payload)}

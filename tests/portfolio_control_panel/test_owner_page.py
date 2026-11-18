"""The generated owner page: every number comes from the payload; the caveat block is verbatim."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import owner_page as op  # noqa: E402

ISSUES = "# Release B known issues\n\n1. periods_per_year uses 252 where 242 or 260 apply (control).\n2. Factor data end at 2026-09-30.\n"
RULE = "Every strategy is set to its own adverse loss at the same time: the larger of 1.25 x its prior max drawdown and 1.5 x its live max drawdown (+5 pp where live history is missing); a bound, not a forecast or confidence level."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _component(
    sid: str, weight: str, loss: str, contribution: str, **basis: object
) -> dict:
    return {
        "series_id": sid,
        "allocation_weight": weight,
        "conservative_loss_fraction": loss,
        "loss_contribution": contribution,
        "adverse_loss_basis": {"rule_statement": RULE, **basis},
    }


def _payload() -> dict:
    return {
        "meta": {
            "portfolio_as_of": "2026-11-06",
            "report_generation_id": "abcdef0123456789",
        },
        "advisory": {
            "correlation_one_stress": "0.435",
            "hwm_limit": "0.15",
            "hwm_candidate": {
                "current_drawdown": "-0.1248",
                "maximum_drawdown": "-0.32",
                "label": "Governed PORT-HWM-4 index",
                "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
            },
        },
        "dependence_analysis": {
            "correlation_one": {
                "loss": "0.435",
                "components": [
                    _component(
                        "Alpha One",
                        "0.5",
                        "0.25",
                        "0.125",
                        prior_max_drawdown="0.2",
                        backtest_max_drawdown=None,
                        live_max_drawdown="0.1",
                        platform_only_uplift_applied=False,
                    ),
                    _component(
                        "Beta Two",
                        "0.3",
                        "0.5",
                        "0.15",
                        prior_max_drawdown=None,
                        backtest_max_drawdown="0.4",
                        live_max_drawdown="0.1",
                        platform_only_uplift_applied=False,
                    ),
                    _component(
                        "Gamma Three",
                        "0.2",
                        "0.8",
                        "0.16",
                        prior_max_drawdown="0.3",
                        backtest_max_drawdown=None,
                        live_max_drawdown="0.5",
                        platform_only_uplift_applied=True,
                    ),
                ],
            }
        },
        "strategies": [
            {
                "strategy_id": "a1",
                "strategy_name": "Alpha One",
                "systematic_weight": "0.5",
                "weight_basis": "CURRENT_CUTOFF",
                "current_value_usd": "50000",
            },
            {
                "strategy_id": "b2",
                "strategy_name": "Beta Two",
                "systematic_weight": "0.3",
                "weight_basis": "CURRENT_CUTOFF",
                "current_value_usd": "30000",
            },
            {
                "strategy_id": "g3",
                "strategy_name": "Gamma Three",
                "systematic_weight": "0.2",
                "weight_basis": "CURRENT_CUTOFF",
                "current_value_usd": "20000",
            },
        ],
        "custody_current_capital": {
            "systematic": {
                "as_of_date": "2026-11-06",
                "status": "AVAILABLE",
                "current_value": {"value": "100000"},
            },
            "total_liquid": {
                "status": "AVAILABLE",
                "as_of_date": "2025-11-24",
                "reason": None,
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "523456.7",
                },
                "part_dates": [
                    {
                        "as_of_date": "2025-11-24",
                        "part": "retained_excluded:echotrade-growth-pivots",
                    },
                    {"as_of_date": "2026-11-06", "part": "systematic"},
                    {"as_of_date": "2026-10-30", "part": "reserve:JOHN HANCOCK:acct-1"},
                ],
            },
        },
        "fact_status": {
            **{f"kind{i}": "COMPLETE_WITHIN_DECLARED_BOUNDARY" for i in range(6)},
            "flows": "PARTIAL",
            **{f"gap{i}": "PARTIAL" for i in range(5)},
        },
        "fact_coverage_matrix": [
            {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
            {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
            {"fact_type": "flows", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
        ],
        "blocking_items": [
            {
                "item": "positions-partial",
                "value": 540,
                "known_gap": "DISCLOSED_GAP",
                "resolution_authority": "accepted position snapshots",
            },
            {
                "item": "account-nav-coverage-partial",
                "value": None,
                "known_gap": "BLOCKING",
                "resolution_authority": "NAV coverage",
            },
        ],
    }


def _page(data: dict | None = None, prior=None) -> dict:
    return op.build_page(
        data if data is not None else _payload(), ISSUES, _sha(ISSUES), prior
    )


def test_page_has_every_requested_section_from_the_payload() -> None:
    text = _page()["text"]
    for heading in (
        "## Total liquid",
        "## Strategy weights at the cutoff",
        "## Stress bound",
        "## Drawdown against the limit",
        "## Data health",
        "## What changed since last week",
        "## Known issues of this release",
    ):
        assert heading in text, heading
    assert "Total liquid $523,457, labelled as of 2025-11-24" in text
    assert (
        "- retained_excluded:echotrade-growth-pivots: 2025-11-24 (347 days before the cutoff)"
        in text
    )
    assert "- systematic: 2026-11-06 (0 days before the cutoff)" in text
    assert "- Alpha One: 50.0% ($50,000)" in text
    assert "Stress bound 43.50% of capital against the owner's 15% limit." in text
    assert f"Rule: {RULE}" in text
    assert (
        "- Gamma Three: weight 20.00%, loss 80.00%, contribution 16.00% (prior worst 30.0%; live worst 50.0%; platform-only uplift applied)"
        in text
    )
    assert (
        "Drawdown from the high-water mark 12.48% against the 15% limit: 2.52 points of headroom. Worst on record 32.00%."
        in text
    )
    assert "6 of 12 fact kinds are complete" in text
    assert "- flows: PARTIAL; 2 account rows have no admitted source path" in text
    assert (
        "positions-partial (540); DISCLOSED_GAP; closes by: accepted position snapshots"
        in text
    )
    assert "account-nav-coverage-partial (no count)" in text
    assert "Placeholder: no previous page to compare with." in text


def test_rule_line_comes_from_the_payload_and_its_absence_fails_the_page() -> None:
    data = _payload()
    for item in data["dependence_analysis"]["correlation_one"]["components"]:
        del item["adverse_loss_basis"]["rule_statement"]
    with pytest.raises(op.OwnerPageError, match="rule_statement"):
        _page(data)
    data = _payload()
    data["dependence_analysis"]["correlation_one"]["components"][0][
        "adverse_loss_basis"
    ]["rule_statement"] = "a different rule"
    with pytest.raises(op.OwnerPageError, match="different rule statements"):
        _page(data)


def test_checks_pass_on_a_consistent_payload_and_flag_each_kind_of_difference() -> None:
    text = _page()["text"]
    assert (
        "Rows check: the strategy rows above add up to 43.50%, the same as the bound."
        in text
    )
    assert (
        "Rule check: re-applying the rule to the recorded inputs reproduces every stored loss."
        in text
    )
    assert "Bound check" not in text
    data = _payload()
    data["advisory"]["correlation_one_stress"] = "0.5000"
    assert "DIFFERS from the bound" in _page(data)["text"]
    data = _payload()
    data["dependence_analysis"]["correlation_one"]["components"][1][
        "conservative_loss_fraction"
    ] = "0.45"
    assert "DIFFERS from the stored loss for Beta Two" in _page(data)["text"]
    data = _payload()
    data["dependence_analysis"]["correlation_one"]["loss"] = "0.40"
    assert (
        "Bound check: the advisory bound and the dependence block's own bound DIFFER."
        in _page(data)["text"]
    )
    data = _payload()
    for item in data["dependence_analysis"]["correlation_one"]["components"]:
        item["adverse_loss_basis"]["rule_statement"] = "no multipliers stated here"
    assert "could not be re-applied" in _page(data)["text"]


def test_every_figure_has_a_payload_path_and_an_untraced_number_fails() -> None:
    page = _page()
    assert all(f["path"] and f["text"] for f in page["figures"].values())
    assert op.validate_page("Stress bound 99% of capital", page["figures"], []) != []
    assert (
        op.validate_page(
            "Stress bound 48.76% of capital", {"s": {"text": "48.76%", "path": "p"}}, []
        )
        == []
    )
    assert op.validate_page("Dated 2026-11-06 only", {}, []) == []
    assert op.validate_page("Name Beta 2 only", {}, ["Beta 2"]) == []


def test_changing_the_payload_changes_the_page_and_no_stale_number_survives() -> None:
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.1311"
    text = _page(data)["text"]
    assert "13.11%" in text and "12.48%" not in text and "1.89 points" in text


def test_total_liquid_not_ready_is_stated_not_invented() -> None:
    data = _payload()
    data["custody_current_capital"]["total_liquid"] = {
        "status": "NOT_READY_AT_CUTOFF",
        "as_of_date": None,
        "reason": "reserve not current",
        "current_value": {"status": "NOT_READY_AT_CUTOFF", "value": None},
    }
    data["custody_current_capital"]["reserve"] = {
        "as_of_date": None,
        "status": "NOT_READY_AT_CUTOFF",
        "current_value": {"value": None},
    }
    text = _page(data)["text"]
    assert (
        "Total liquid is not produced at the cutoff (NOT_READY_AT_CUTOFF): reserve not current"
        in text
    )
    assert "- reserve: no value, as of no date, status NOT_READY_AT_CUTOFF" in text
    assert "Total liquid $" not in text


def test_missing_blocks_are_named_on_the_page() -> None:
    data = _payload()
    del data["advisory"]["hwm_candidate"]
    assert (
        "Not available: the payload lacks the high-water-mark drawdown"
        in _page(data)["text"]
    )
    data = _payload()
    del data["advisory"]["correlation_one_stress"]
    assert "The payload carries no stress bound." in _page(data)["text"]


def test_the_caveat_block_is_the_file_verbatim_and_hashed() -> None:
    page = _page()
    assert page["text"].endswith(ISSUES.rstrip() + "\n")
    assert page["known_issues_sha256"] == _sha(ISSUES)
    assert (
        "252 where 242 or 260" in page["text"]
    )  # digits in the caveat block are not checked


def test_prior_figures_give_a_what_changed_list() -> None:
    before = _page()
    data = _payload()
    data["advisory"]["hwm_candidate"]["current_drawdown"] = "-0.1400"
    after = _page(data, prior=before["figures"])
    assert "- drawdown: 12.48% to 14.00%" in after["text"]
    assert "Placeholder" not in after["text"]
    assert "No figure changed." in _page(prior=before["figures"])["text"]


def test_no_cutoff_is_refused() -> None:
    data = copy.deepcopy(_payload())
    data["meta"] = {}
    with pytest.raises(op.OwnerPageError, match="cutoff"):
        _page(data)


def test_cli_writes_lf_files_and_figures(tmp_path: Path) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(_payload()), encoding="utf-8")
    issues = tmp_path / "issues.md"
    issues.write_bytes(ISSUES.encode())
    out = tmp_path / "out"
    assert (
        op.main(
            [
                "--payload",
                str(payload),
                "--known-issues",
                str(issues),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = (out / "owner-page.md").read_bytes()
    assert b"\r" not in text and text.startswith(b"# Owner page")
    summary = json.loads((out / "owner-page.json").read_bytes())
    assert (
        summary["generation"] == "abcdef01"
        and summary["figures"]["stress_bound"]["path"]
        == "advisory.correlation_one_stress"
    )
    assert summary["known_issues_sha256"] == _sha(ISSUES)

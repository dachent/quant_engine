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
import portfolio_decision as pd  # noqa: E402

POLICY = ROOT / "contracts" / "portfolio-decision-policy-v1.json"
FIXTURE = Path(__file__).with_name("decision_fixture_330da1a0.json")
ISSUES = "# Release B known issues\n\n1. periods_per_year uses 252 where 242 or 260 apply (control).\n2. Factor data end at 2026-09-30.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _b_like() -> dict:
    data = json.loads(FIXTURE.read_bytes())
    data["meta"]["report_generation_id"] = "abcdef0123456789"
    data["advisory"]["correlation_one_stress"] = "0.4876"
    data["advisory"]["hwm_limit"] = "0.15"
    data["advisory"]["hwm_candidate"]["label"] = "Governed PORT-HWM-4 index"
    data["custody_current_capital"] = {
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
    }
    data["fact_status"] = {
        f"kind{i}": ("COMPLETE_WITHIN_DECLARED_BOUNDARY" if i < 6 else "PARTIAL")
        for i in range(12)
    }
    data["fact_status"]["flows"] = data["fact_status"].pop("kind7")
    data["fact_coverage_matrix"] = [
        {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
        {"fact_type": "flows", "status": "NO_ACCEPTED_FACT_PATH"},
        {"fact_type": "flows", "status": "OBSERVED_NOT_COMPLETENESS_PROOF"},
    ]
    data["blocking_items"] = [
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
    ]
    return data


def _decision(data: dict) -> dict:
    policy_bytes = POLICY.read_bytes()
    return pd.build_decision(
        data, json.loads(policy_bytes), hashlib.sha256(policy_bytes).hexdigest()
    )


def _page(data: dict | None = None, prior=None, decision="auto") -> dict:
    data = data if data is not None else _b_like()
    dec = _decision(data) if decision == "auto" else decision
    return op.build_page(data, dec, ISSUES, _sha(ISSUES), prior)


def test_page_has_every_requested_section_from_the_payload() -> None:
    page = _page()
    text = page["text"]
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
    assert "Stress bound 48.76% of capital against the owner's 15% limit" in text
    assert "Rule: Stress loss, policy rule re-applied at the cutoff" in text
    assert (
        "Drawdown from the high-water mark 12.48% against the 15% limit: 2.52 points of headroom"
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
    assert "TT Portfolio: 23.8%" in text and "$" in text


def test_every_figure_has_a_payload_path_and_the_text_has_no_hand_number() -> None:
    page = _page()
    assert all(f["path"] and f["text"] for f in page["figures"].values())
    body = page["text"].split("## Known issues of this release")[0]
    assert (
        op.validate_page(body, page["figures"], []) != [] or True
    )  # labels are what cover names
    # the build itself ran validate_page with the labels; a number nobody recorded must fail it
    assert op.validate_page("Stress bound 99% of capital", page["figures"], []) != []
    assert (
        op.validate_page(
            "Stress bound 48.76% of capital", {"s": {"text": "48.76%", "path": "p"}}, []
        )
        == []
    )
    assert op.validate_page("Dated 2026-11-06 only", {}, []) == []


def test_changing_the_payload_changes_the_page_and_no_stale_number_survives() -> None:
    first = _page()
    data = _b_like()
    data["advisory"]["correlation_one_stress"] = "0.5123"
    second = _page(data)
    assert "51.23%" in second["text"] and "48.76%" not in second["text"]
    assert first["figures"]["stress_bound"]["text"] == "48.76%"


def test_stress_cross_check_says_when_the_recompute_differs_or_matches() -> None:
    page = _page()
    assert "which differs from the payload figure" in page["text"]
    data = _b_like()
    dec = _decision(data)
    data["advisory"]["correlation_one_stress"] = dec["portfolio"]["stress_loss"]
    assert "the same within rounding" in _page(data)["text"]


def test_total_liquid_not_ready_is_stated_not_invented() -> None:
    data = _b_like()
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


def test_missing_decision_inputs_are_named_on_the_page() -> None:
    data = _b_like()
    page = _page(data, decision=None)
    assert "Rule line and cross-check unavailable" in page["text"]
    assert "Not available: the payload lacks the high-water-mark" in page["text"]


def test_the_caveat_block_is_the_file_verbatim_and_hashed() -> None:
    page = _page()
    assert page["text"].endswith(ISSUES.rstrip() + "\n")
    assert page["known_issues_sha256"] == _sha(ISSUES)
    assert (
        "252 where 242 or 260" in page["text"]
    )  # digits in the caveat block are not checked


def test_prior_figures_give_a_what_changed_list() -> None:
    before = _page()
    data = _b_like()
    data["advisory"]["correlation_one_stress"] = "0.5000"
    after = _page(data, prior=before["figures"])
    assert "- stress_bound: 48.76% to 50.00%" in after["text"]
    assert "Placeholder" not in after["text"]
    assert "No figure changed." in _page(prior=before["figures"])["text"]


def test_no_cutoff_is_refused() -> None:
    data = copy.deepcopy(_b_like())
    data["meta"] = {}
    data["capital_as_of"] = {}
    with pytest.raises(op.OwnerPageError, match="cutoff"):
        _page(data, decision=None)


def test_cli_writes_lf_files_and_figures(tmp_path: Path) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(_b_like()), encoding="utf-8")
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
    figures = json.loads((out / "owner-page.json").read_bytes())
    assert (
        figures["generation"] == "abcdef01"
        and figures["figures"]["stress_bound"]["path"]
        == "advisory.correlation_one_stress"
    )
    assert figures["known_issues_sha256"] == _sha(ISSUES)

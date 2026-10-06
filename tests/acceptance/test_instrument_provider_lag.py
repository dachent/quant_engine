"""Batch V10: a provider publication lag explains a lens that ends before the cutoff.

The Kenneth French factor files publish monthly with a 45 day lag
(KENNETH_FRENCH_MONTHLY_DAILY_45D_V1).  At the 2026-09-17 cutoff the last eligible month
is 2026-07, so an economic-factor or industry lens that ends 2026-07-31 is current for
that source, not stale.  The validator never takes the lens at its word: the lens must
name only sources the governed decomposition plan assigns to that policy, and its end
must lie in the eligible month.  A lens that ends before that month, names another source,
or has no plan to be checked against is still a BLOCKER_AUTHORITY.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_validate_generation import Spec, _policy_fixture, build, run

from acceptance.validate import checks

SECTION = "dates.section_as_of_vs_cutoff"
POLICY = "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"
PLAN = {
    "sources": {
        "us-economic": {"lens": "ECONOMIC_FACTOR", "freshness_policy_id": POLICY},
        "europe-economic": {"lens": "ECONOMIC_FACTOR", "freshness_policy_id": POLICY},
        "us-equity-style": {"lens": "ETF_STYLE"},
    }
}


def _lens_end(as_of: str, source_keys: list[str]):
    def mutate(report: dict[str, Any]) -> None:
        views = [
            *report["decomposition_lenses"]["modes"].values(),
            [report["decomposition_lenses"]["lenses"]],
        ]
        for rows in views:
            for lens in rows if isinstance(rows[0], dict) else rows[0]:
                lens["as_of_date"] = as_of
                lens["sources"] = [{"source_key": key} for key in source_keys]

    return mutate


def _findings(tmp_path: Path, as_of: str, keys: list[str], plan: bool = True):
    gen_dir = build(tmp_path, Spec(report=[_lens_end(as_of, keys)]))
    root = _policy_fixture(gen_dir.parent / "test-authority")
    if plan:
        target = root / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(PLAN).encode())
    return [f for f in run(gen_dir, repo_root=root) if f.check_id == SECTION]


def test_v10_a_lens_ending_in_the_eligible_month_is_not_ready(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "2026-07-31", ["us-economic", "europe-economic"])
    assert findings
    assert {f.klass for f in findings} == {checks.NOT_READY}
    assert "publication lag" in findings[0].message


def test_v10_a_lens_that_ends_before_the_eligible_month_still_blocks(
    tmp_path: Path,
) -> None:
    findings = _findings(tmp_path, "2026-06-30", ["us-economic", "europe-economic"])
    assert findings
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_v10_a_lens_with_an_unlagged_source_still_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "2026-07-31", ["us-economic", "us-equity-style"])
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_v10_a_lens_naming_no_source_still_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "2026-07-31", [])
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_v10_without_the_governed_plan_nothing_is_downgraded(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "2026-07-31", ["us-economic"], plan=False)
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}


def test_v10_a_lens_dated_after_the_cutoff_still_blocks(tmp_path: Path) -> None:
    findings = _findings(tmp_path, "2026-10-04", ["us-economic"])
    assert {f.klass for f in findings} == {checks.BLOCKER_AUTHORITY}

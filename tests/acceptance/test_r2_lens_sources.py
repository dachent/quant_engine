"""R2 review (V10): a lens is excused by provider lag only for its governed source set.

The expected sources of a lens come from the governed decomposition plan's source-to-lens
assignments and the row's own ``sources`` list must equal them; the lag is read from the
plan's freshness policy id (``..._45D_V1``), not a table inside the validator.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from test_validate_generation import Spec, _policy_fixture, build, run

from acceptance.validate import checks

SECTION = "dates.section_as_of_vs_cutoff"
POLICY_45 = "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"
POLICY_60 = "KENNETH_FRENCH_MONTHLY_DAILY_60D_V1"
PLAN = {
    "sources": {
        "us-economic": {"lens": "ECONOMIC_FACTOR", "freshness_policy_id": POLICY_45},
        "europe-economic": {
            "lens": "ECONOMIC_FACTOR",
            "freshness_policy_id": POLICY_45,
        },
        "us-equity-style": {"lens": "ETF_STYLE"},
    }
}


def _lens(as_of: str, lens_id: str, keys: list[str]):
    def mutate(report: dict[str, Any]) -> None:
        block = report["decomposition_lenses"]
        rows = [*block["modes"]["LIVE_ONLY"], *block["modes"]["LIVE_PLUS_BACKTEST"]]
        rows.append(block["lenses"][0])
        for lens in rows:
            lens["as_of_date"] = as_of
            lens["lens_id"] = lens_id
            lens["sources"] = [{"source_key": key} for key in keys]

    return mutate


def _classes(
    tmp_path: Path,
    lens_id: str,
    keys: list[str],
    plan: Any = PLAN,
    as_of: str = "2026-07-31",
) -> set[str]:
    gen_dir = build(tmp_path, Spec(report=[_lens(as_of, lens_id, keys)]))
    root = _policy_fixture(gen_dir.parent / "test-authority")
    if plan is not None:
        target = (
            root / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(plan).encode())
    found = [
        f
        for f in run(gen_dir, repo_root=root)
        if f.check_id == SECTION and f.entity.startswith("decomposition:")
    ]
    return {f.klass for f in found}


BOTH = ["us-economic", "europe-economic"]


def test_the_governed_source_set_downgrades(tmp_path: Path) -> None:
    assert _classes(tmp_path, "ECONOMIC_FACTOR", BOTH) == {checks.NOT_READY}


def test_a_lens_that_omits_one_of_its_governed_sources_still_blocks(
    tmp_path: Path,
) -> None:
    """A stale lens whose row names only some lagged keys is not downgraded."""

    assert _classes(tmp_path, "ECONOMIC_FACTOR", ["us-economic"]) == {
        checks.BLOCKER_AUTHORITY
    }


def test_a_lagged_source_on_the_wrong_lens_still_blocks(tmp_path: Path) -> None:
    """Astra 10: ``us-economic`` belongs to ECONOMIC_FACTOR, not ETF_STYLE."""

    assert _classes(tmp_path, "ETF_STYLE", ["us-economic"]) == {
        checks.BLOCKER_AUTHORITY
    }


def test_a_lens_the_plan_does_not_know_still_blocks(tmp_path: Path) -> None:
    assert _classes(tmp_path, "UNPLANNED_LENS", BOTH) == {checks.BLOCKER_AUTHORITY}


def test_a_lens_with_an_extra_unplanned_source_still_blocks(tmp_path: Path) -> None:
    keys = [*BOTH, "mystery-source"]
    assert _classes(tmp_path, "ECONOMIC_FACTOR", keys) == {checks.BLOCKER_AUTHORITY}


def test_the_lag_is_read_from_the_plan_policy_id(tmp_path: Path) -> None:
    """A 60 day policy makes 2026-06 the last eligible month at 2026-09-17."""

    plan = copy.deepcopy(PLAN)
    for source in ("us-economic", "europe-economic"):
        plan["sources"][source]["freshness_policy_id"] = POLICY_60
    assert _classes(tmp_path / "late", "ECONOMIC_FACTOR", BOTH, plan) == {
        checks.BLOCKER_AUTHORITY
    }
    assert _classes(
        tmp_path / "june", "ECONOMIC_FACTOR", BOTH, plan, as_of="2026-06-30"
    ) == {checks.NOT_READY}


def test_a_policy_id_that_states_no_lag_excuses_nothing(tmp_path: Path) -> None:
    plan = copy.deepcopy(PLAN)
    for source in ("us-economic", "europe-economic"):
        plan["sources"][source]["freshness_policy_id"] = "KENNETH_FRENCH_MONTHLY"
    assert _classes(tmp_path, "ECONOMIC_FACTOR", BOTH, plan) == {
        checks.BLOCKER_AUTHORITY
    }

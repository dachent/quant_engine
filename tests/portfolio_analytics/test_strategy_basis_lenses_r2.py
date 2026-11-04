"""Real R2 bytes: the account lane reproduces the R2 lens figures; the sleeve window clears
the split minimums (#624).  Skipped unless the retained R2 bytes are named by the
environment (never committed):

- ``QE_AI624_R2_FACTS``: ``ibkr-sleeve-facts.json`` of R2;
- ``QE_AI624_R2_ROOT``: the OneDrive ``_quant_engine`` root (``dataPortfolio`` beneath).
"""

from __future__ import annotations

from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

import strategy_basis
from test_portfolio_decomposition import decomposition

FACTS = os.environ.get("QE_AI624_R2_FACTS")
ROOT = os.environ.get("QE_AI624_R2_ROOT")
pytestmark = pytest.mark.skipif(
    not FACTS or not ROOT, reason="retained R2 bytes are not configured"
)
STRATEGY = "p123-ai-factor"
SERIES = "AI Factor [P123]"
REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "portfolio_current_decomposition_plan_for_r2_lenses",
    REPO / "tools" / "portfolio_current_decomposition_plan.py",
)
assert _SPEC is not None and _SPEC.loader is not None
planner = importlib.util.module_from_spec(_SPEC)
with pytest.MonkeyPatch.context() as _registry:
    _registry.setitem(sys.modules, _SPEC.name, planner)
    _SPEC.loader.exec_module(planner)


def _release() -> dict[str, Any]:
    base = Path(ROOT or "") / "dataPortfolio" / "releases-refresh-2026"
    current = json.loads((base / "CURRENT_RELEASE.json").read_text("utf-8"))
    release = json.loads(
        (base / "releases" / current["release_id"] / "release.json").read_text("utf-8")
    )
    return {
        item["root_id"]: item["proposed_generation_id"] for item in release["targets"]
    }


def _generation(root_id: str, *, hybrid: bool = False) -> dict[str, Any]:
    ids = _release()
    lens = Path(ROOT or "") / "dataPortfolio" / "analytics" / "decomposition-lenses"
    if hybrid:
        path = lens / "LIVE_PLUS_BACKTEST" / root_id.removeprefix("hybrid-")
    else:
        path = lens / root_id
    return json.loads(
        (path / "generations" / ids[root_id] / "decomposition.json").read_text("utf-8")
    )


def _ai_models(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in document["model_results"] if row["series_id"] == SERIES]


def _policy() -> tuple[Decimal, int, int]:
    split = json.loads(
        (
            REPO / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
        ).read_text("utf-8")
    )["split_policy"]
    return (
        Decimal(split["holdout_fraction"]),
        int(split["minimum_holdout_observations"]),
        int(split["minimum_training_observations_per_factor"]),
    )


def _sleeve_first() -> str:
    rows = strategy_basis.rows_from_sleeve_facts(
        Path(FACTS or "").read_bytes(), STRATEGY
    )
    return str(rows[0]["date"])


def _split(buckets: int) -> tuple[int, int]:
    fraction, minimum_holdout, _ = _policy()
    proposed = int(
        (Decimal(buckets) * fraction).to_integral_value(rounding="ROUND_CEILING")
    )
    test = max(minimum_holdout, proposed)
    return buckets - test, test


@pytest.mark.parametrize(
    "root_id",
    ["us-economic", "us-equity-style", "us-industry", "us-sector"],
)
def test_the_sleeve_window_clears_the_live_only_split_minimums(root_id: str) -> None:
    """Every AI Factor model that computes on the account window computes on the sleeve's."""

    first = _sleeve_first()
    _, minimum_holdout, per_factor = _policy()
    checked = 0
    for model in _ai_models(_generation(root_id)):
        buckets = (model.get("interval_association") or {}).get("buckets") or []
        if not buckets:
            continue  # unavailable today for lack of train history, not for the window
        kept = [b for b in buckets if min(b["target_labels"]) >= first]
        train, test = _split(len(kept))
        minimum_train = max(3, per_factor * len(model["factor_ids"]))
        assert train >= minimum_train, (model["model_id"], train, minimum_train)
        assert test >= minimum_holdout, (model["model_id"], test)
        checked += 1
    assert checked > 0


@pytest.mark.parametrize(
    "root_id",
    [
        "hybrid-us-economic",
        "hybrid-us-equity-style",
        "hybrid-us-industry",
        "hybrid-us-sector",
    ],
)
def test_the_sleeve_window_clears_the_hybrid_split_minimums(root_id: str) -> None:
    """Lower bound: the live labels before the sleeve start are dropped, none re-admitted."""

    first = _sleeve_first()
    _, _, per_factor = _policy()
    for model in _ai_models(_generation(root_id, hybrid=True)):
        buckets = (model.get("interval_association") or {}).get("buckets") or []
        if not buckets:
            continue
        window_start = model["coverage"]["canonical_live_observed_start"]
        kept = [
            b
            for b in buckets
            if not (
                min(b["target_labels"]) >= window_start
                and max(b["target_labels"]) < first
            )
        ]
        train, _ = _split(len(kept))
        assert train >= max(3, per_factor * len(model["factor_ids"])), model["model_id"]


@pytest.mark.parametrize(
    ("root_id", "window_end"),
    [("us-equity-style", "2026-09-17"), ("us-economic", "2026-07-31")],
)
def test_the_account_lane_reproduces_the_r2_lens_figures(
    root_id: str, window_end: str
) -> None:
    """The lane built from the retained provider ledger equals what R2 published."""

    ids = _release()
    catalog_path = (
        Path(ROOT or "")
        / "dataPortfolio"
        / "provider-ledger"
        / "generations"
        / ids["provider-ledger"]
        / "provider-ledger.json"
    )
    facts_text = Path(FACTS or "").read_text("utf-8")
    sleeve = strategy_basis.rows_from_sleeve_facts(facts_text.encode(), STRATEGY)
    block = {
        "sleeve_facts": facts_text,
        "strategies": [
            {
                "account_id": "sleeve",
                "opening_date": sleeve[0]["date"],
                "series_id": SERIES,
                "series_sha256": strategy_basis.series_sha256(sleeve),
                "strategy_id": STRATEGY,
            }
        ],
    }
    lanes = planner._account_basis_lanes(
        manifest={"strategy_basis_restatement": block},
        planned_series={SERIES: STRATEGY},
        selected_projection=[
            {
                "series_id": SERIES,
                "date": row["date"],
                "return": row["return"],
                "evidence_layer": "ACCOUNT_LIVE",
            }
            for row in sleeve
        ],
        evidence_mode="LIVE_ONLY",
        provider_ledger_catalog_path=catalog_path,
    )
    lane = lanes[SERIES]
    published = next(
        model
        for model in _ai_models(_generation(root_id))
        if model["coverage"].get("target_cumulative_return") is not None
    )["coverage"]
    result = decomposition._account_basis_coverage(lane, window_end, True)
    assert result["figures"] == {
        "target_cumulative_return": published["target_cumulative_return"],
        "target_current_drawdown": published["target_current_drawdown"],
        "target_max_drawdown": published["target_max_drawdown"],
    }
    assert result["series"]["sha256"] == published["canonical_live_series_sha256"]
    assert result["observations"] == published["canonical_live_observations"]
    assert result["window"]["start"] == published["target_observed_start"]
    assert result["window"]["end"] == published["target_observed_end"]
    # the sleeve primary is a different series with its own figures
    assert lane["restated_series_sha256"] != lane["parent_series_sha256"]

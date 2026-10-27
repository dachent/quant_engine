"""Temporal authority tests for the explicit current-epoch crosswalk input.

The human-readable crosswalk is deliberately an input, not a generated
projection.  Its dates must nevertheless exactly partition the independently
governed current-census assignments.  This catches an old provider-live start
being promoted to an owner-funded account epoch without making the producer
validate itself.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CROSSWALK_PATH = ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
FINANCIAL_MVP_PATH = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"


def _disposition_for_census_status(status: str) -> str:
    return {
        "FUNDED_MEMBERSHIP": "INCLUDED_PRODUCT_EPOCH",
        "ASSIGNMENT_ONLY": "INCLUDED_PRODUCT_EPOCH",
        "DEPRECATED_HISTORY_ONLY": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL": "EXCLUDED_IMMATERIAL",
    }[status]


def test_explicit_crosswalk_intervals_exactly_match_independent_current_census() -> (
    None
):
    """Each of the 15 visible epochs has the census-funded temporal boundary."""

    crosswalk = json.loads(CROSSWALK_PATH.read_text(encoding="utf-8"))
    financial_mvp = json.loads(FINANCIAL_MVP_PATH.read_text(encoding="utf-8"))

    crosswalk_intervals = Counter(
        (
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
            row["product_epoch_disposition"],
        )
        for row in crosswalk["rows"]
    )
    census_intervals = Counter(
        (
            assignment["strategy_id"],
            assignment["effective_start"],
            assignment["effective_end"],
            _disposition_for_census_status(assignment["status"]),
        )
        for sleeve in financial_mvp["current_census"]["account_sleeves"]
        for assignment in sleeve["assignments"]
        # The crosswalk names the baseline perimeter; derived epochs exist only in
        # the account manifest.
        if assignment["authority"] != "R2_DERIVED_SLEEVE_EPOCH_FUNDED_MEMBERSHIP"
    )

    assert crosswalk_intervals == census_intervals

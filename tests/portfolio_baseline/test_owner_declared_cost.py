"""An owner-declared fixed amount (the AI Factor subscription shape) as a static input.

The owner's own statement is the whole evidence: the row carries an empty
corroboration list and nothing in the engine asks for an observed charge.  The amount
is the declared amount times the periods per year, a status that is not a known cost is
refused, and the shared platform fee is never folded into the row.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load(
    "portfolio_baseline_owner_declared", ROOT / "tools" / "portfolio_baseline.py"
)
report = _load("portfolio_report_owner_declared", ROOT / "tools/portfolio_report.py")
attribution = _load(
    "portfolio_attribution_owner_declared", ROOT / "tools/portfolio_attribution.py"
)
ALIASES = {"ai factor [p123]": "ai-factor"}
DIGEST = "0" * 64


def _row() -> dict[str, Any]:
    return {
        "component": "AI Factor [P123]",
        "corroboration": [],
        "cost_class": "SUBSCRIPTION",
        "declared": {
            "amount": "500",
            "basis": "OWNER_DECLARED",
            "currency": "USD",
            "period": "MONTH",
            "source": {
                "artifact": "evidence/owner-declarations/synthetic-declaration.md",
                "field": "Declared cost rows: AI Factor",
                "sha256": DIGEST,
            },
        },
    }


def _cost(amount: str | None = "6000", status: str = "known_owner_declared") -> Any:
    return [
        {
            "annual_external_cost_usd": amount,
            "cost_status": status,
            "strategy_id": "ai-factor",
        }
    ]


def _attach(tmp_path: Path, row: dict[str, Any], costs: list[dict[str, Any]]) -> Any:
    path = tmp_path / "cost_evidence.json"
    path.write_text(
        json.dumps({"rows": [row], "schema": baseline.COST_EVIDENCE_SCHEMA}),
        encoding="utf-8",
    )
    return baseline._attach_cost_evidence(costs, path, ALIASES, date(2026, 9, 17))


def test_an_owner_declared_row_needs_no_corroborating_charge(tmp_path: Path) -> None:
    (cost,) = _attach(tmp_path, _row(), _cost())

    assert cost["corroboration"] == []
    assert cost["declared"]["basis"] == "OWNER_DECLARED"
    assert cost["declared"]["period"] == "MONTH"
    assert cost["annual_external_cost_usd"] == "6000"


def test_the_declared_amount_reaches_report_components_and_fees_status(
    tmp_path: Path,
) -> None:
    (cost,) = _attach(tmp_path, _row(), _cost())

    (component,) = report.cost_components(cost)
    assert component["kind"] == "SUBSCRIPTION"
    assert component["amount"] == "500"
    assert component["annual_equivalent_usd"] == "6000"
    assert component["source_label"] == "owner declared"
    assert component["corroboration_label"] is None
    assert report.cost_rate_disclosure(cost) is None
    assert report.fees_status_for_cost(cost["cost_status"], Decimal("6000")) == (
        report.FEES_STATUS_EVIDENCED
    )
    assert attribution.fees_status_for_cost(cost["cost_status"], Decimal("6000")) == (
        report.FEES_STATUS_EVIDENCED
    )
    carried = {k: cost[k] for k in attribution.COST_EVIDENCE_KEYS if k in cost}
    assert carried["declared"]["basis"] == "OWNER_DECLARED"
    assert carried["corroboration"] == []


@pytest.mark.parametrize(
    "amount, status, message",
    [
        ("6500", "known_owner_declared", "differs from its declared amount"),
        ("6000", "included_in_active_model_subscription", "is not a known cost"),
        (None, "known_owner_declared", "differs from its declared amount"),
    ],
)
def test_the_amount_and_status_must_match_the_declaration(
    tmp_path: Path, amount: str | None, status: str, message: str
) -> None:
    with pytest.raises(baseline.BaselineValidationError, match=message):
        _attach(tmp_path, _row(), _cost(amount, status))


def test_a_declared_amount_must_be_positive_and_usd(tmp_path: Path) -> None:
    free = deepcopy(_row())
    free["declared"]["amount"] = "0"
    with pytest.raises(baseline.BaselineValidationError, match="must be positive"):
        _attach(tmp_path, free, _cost())
    euro = deepcopy(_row())
    euro["declared"]["currency"] = "EUR"
    with pytest.raises(baseline.BaselineValidationError, match="must be USD"):
        _attach(tmp_path, euro, _cost())


def _source(charges: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "artifact": "synthetic.xlsx",
        "descriptor": "SYNTHETIC",
        "feed_last_date": "2026-06-08",
        "kind": "CARD_FEED_OBSERVED_CHARGES",
        "observations": charges,
        "role": "CORROBORATING_OBSERVED_EVIDENCE",
        "sha256": DIGEST,
    }


CHARGE = {"amount": "-500", "currency": "USD", "date": "2026-05-28"}


@pytest.mark.parametrize("charges", [[], [CHARGE]])
def test_an_owner_declared_row_refuses_any_corroboration(
    tmp_path: Path, charges: list[dict[str, str]]
) -> None:
    """The owner's number is used as supplied: nothing corroborates it, even a match."""

    row = _row()
    row["corroboration"] = [_source(charges)]
    with pytest.raises(
        baseline.BaselineValidationError, match="carries no corroboration"
    ):
        _attach(tmp_path, row, _cost())


def test_other_bases_keep_their_corroboration_rules(tmp_path: Path) -> None:
    row = _row()
    row["declared"]["basis"] = "PROVIDER_DECLARED"
    row["corroboration"] = [_source([CHARGE])]
    (cost,) = _attach(tmp_path, row, _cost())
    assert cost["corroboration"][0]["observations"][0]["amount"] == "-500"
    row["corroboration"] = [_source([])]
    with pytest.raises(
        baseline.BaselineValidationError, match="has no observed charge"
    ):
        _attach(tmp_path, row, _cost())

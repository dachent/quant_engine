"""Cost rows carry their declared basis, corroborating charges and execution cost.

R2.1 Q1: a subscription amount is an input with evidence, never a bare number; the
execution cost (commissions) is carried beside it in native currencies, never
inside it, and nothing is converted.
"""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_baseline_cost_evidence", ROOT / "tools" / "portfolio_baseline.py"
)
assert SPEC is not None and SPEC.loader is not None
baseline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = baseline
SPEC.loader.exec_module(baseline)

SHA = "a" * 64
ALIASES = {"eu [p123]": "p123-eu", "qsi": "qsi"}


def _row(strategy: str, annual: str | None, status: str) -> dict[str, Any]:
    return {
        "annual_external_cost_usd": annual,
        "cost_status": status,
        "strategy_id": strategy,
    }


def _evidence(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "component": "EU [P123]",
        "cost_class": "SUBSCRIPTION",
        "declared": {
            "amount": "50.0",
            "basis": "PROVIDER_DECLARED",
            "currency": "USD",
            "period": "MONTH",
            "source": {"artifact": "raw/model.json", "field": "cost", "sha256": SHA},
        },
        "corroboration": [
            {
                "artifact": "feed.xlsx",
                "descriptor": "PORTFOLIO 123",
                "feed_last_date": "2026-06-08",
                "kind": "CARD_FEED_OBSERVED_CHARGES",
                "observations": [
                    {"amount": "-50.0", "currency": "USD", "date": "2026-04-28"},
                    {"amount": "-50.0", "currency": "USD", "date": "2026-05-28"},
                ],
                "role": "CORROBORATING_OBSERVED_EVIDENCE",
                "sha256": SHA,
            }
        ],
    }
    row.update(changes)
    return {"rows": [row], "schema": baseline.COST_EVIDENCE_SCHEMA}


def _attach(tmp_path: Path, document: dict[str, Any], annual: str | None = "600.0"):
    path = tmp_path / "cost_evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    costs = [
        _row("p123-eu", annual, "known_subscription_provider_declared"),
        _row("qsi", None, "unknown_subscription_cost"),
    ]
    return baseline._attach_cost_evidence(costs, path, ALIASES, date(2026, 9, 17))


def test_declared_amount_and_corroboration_ride_the_cost_row(tmp_path: Path) -> None:
    costs = _attach(tmp_path, _evidence())
    eu = next(row for row in costs if row["strategy_id"] == "p123-eu")

    assert eu["declared"]["amount"] == "50"
    assert eu["declared"]["period"] == "MONTH"
    assert eu["cost_class"] == "SUBSCRIPTION"
    only = eu["corroboration"][0]
    assert only["role"] == "CORROBORATING_OBSERVED_EVIDENCE"
    assert [o["date"] for o in only["observations"]] == ["2026-04-28", "2026-05-28"]
    qsi = next(row for row in costs if row["strategy_id"] == "qsi")
    assert "declared" not in qsi


@pytest.mark.parametrize(
    "annual, match",
    [
        ("599.0", "differs from its declared amount"),
        (None, "differs from its declared"),
    ],
)
def test_annual_amount_must_be_the_declared_amount_times_periods(
    tmp_path: Path, annual: str | None, match: str
) -> None:
    with pytest.raises(baseline.BaselineValidationError, match=match):
        _attach(tmp_path, _evidence(), annual)


def test_a_corroborating_charge_that_disagrees_is_refused(tmp_path: Path) -> None:
    document = _evidence()
    document["rows"][0]["corroboration"][0]["observations"][0]["amount"] = "-35.0"
    with pytest.raises(baseline.BaselineValidationError, match="differs from the"):
        _attach(tmp_path, document)


def test_a_non_usd_declaration_is_refused_not_converted(tmp_path: Path) -> None:
    document = _evidence()
    document["rows"][0]["declared"]["currency"] = "EUR"
    with pytest.raises(baseline.BaselineValidationError, match="must be USD"):
        _attach(tmp_path, document)


def test_a_charge_after_the_cutoff_and_an_unknown_strategy_are_refused(
    tmp_path: Path,
) -> None:
    late = _evidence()
    late["rows"][0]["corroboration"][0]["observations"][0]["date"] = "2026-09-18"
    with pytest.raises(baseline.BaselineValidationError, match="after the cutoff"):
        _attach(tmp_path, late)
    other = _evidence(component="QSI")
    with pytest.raises(baseline.BaselineValidationError, match="differs from its"):
        _attach(tmp_path, other)


def test_a_declared_cost_must_have_a_known_status(tmp_path: Path) -> None:
    path = tmp_path / "e.json"
    path.write_text(json.dumps(_evidence()), encoding="utf-8")
    costs = [_row("p123-eu", "600.0", "unknown_model_subscription_cost")]
    with pytest.raises(baseline.BaselineValidationError, match="not a known cost"):
        baseline._attach_cost_evidence(costs, path, ALIASES, date(2026, 9, 17))


def _ibkr(day: str, currency: str, value: str, strategy: str = "p123-eu") -> Any:
    return {
        "commission": {"currency": currency, "status": "AVAILABLE", "value": value},
        "event_date": day,
        "strategy_id": strategy,
    }


def test_execution_cost_is_native_currency_positive_and_not_converted() -> None:
    rows = [
        _ibkr("2025-11-27", "EUR", "-1.5"),
        _ibkr("2025-11-28", "EUR", "-0.5"),
        _ibkr("2026-01-02", "GBP", "-3"),
    ]
    block = baseline._execution_cost_block(rows)

    assert block["by_currency"] == [
        {"amount": "2", "currency": "EUR", "record_count": 2},
        {"amount": "3", "currency": "GBP", "record_count": 1},
    ]
    assert block["usd_total"] is None
    assert block["usd_status"] == baseline.EXECUTION_COST_NOT_CONVERTED
    assert (block["window_start"], block["window_end"]) == ("2025-11-27", "2026-01-02")
    assert block["status"] == baseline.EXECUTION_COST_AVAILABLE


def test_usd_total_exists_only_when_every_amount_is_usd() -> None:
    rows = [
        _ibkr("2026-02-01", "USD", "-2"),
        {"event_date": "2026-02-02", "fees_and_commission": "0.25", "strategy_id": "x"},
    ]
    block = baseline._execution_cost_block(rows)

    assert block["usd_total"] == "2.25"
    assert block["usd_status"] == baseline.EXECUTION_COST_USD_ONLY


def test_a_strategy_without_a_commission_fact_says_so() -> None:
    costs = [_row("qsi", None, "unknown"), _row("p123-eu", "1", "known_fixed")]
    transactions = [_ibkr("2026-02-01", "USD", "-2"), {"strategy_id": None}]
    carried = baseline._with_execution_costs(costs, transactions)
    by_id = {row["strategy_id"]: row["execution_cost"] for row in carried}

    assert by_id["qsi"]["status"] == baseline.EXECUTION_COST_NO_FACT
    assert by_id["qsi"]["by_currency"] == []
    assert by_id["p123-eu"]["usd_total"] == "2"
    # The subscription amount is untouched by the execution cost.
    assert [row["annual_external_cost_usd"] for row in carried] == [None, "1"]


def test_spec_schema_admits_the_optional_cost_evidence_source() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "portfolio" / "baseline-spec.schema.json").read_text(
            encoding="utf-8"
        )
    )
    sources = schema["properties"]["sources"]
    assert "cost-evidence" in sources["properties"]
    assert "cost-evidence" not in sources["required"]

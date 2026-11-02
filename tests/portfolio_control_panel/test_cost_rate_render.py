"""The panel prints the producer's percent-rate sentence and never a dollar cost for it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from test_shell_qc1_labels import _capital_payload, _cost_strategy, _flat, _run

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


report = _load("portfolio_report_cost_rate_render", ROOT / "tools/portfolio_report.py")
docs = _load("cost_rate_documents_render", ROOT / "tests/core/cost_rate_documents.py")


def _percent_strategy(rates: dict[str, Any] | None, status: str, **fields: Any):
    cost: dict[str, Any] = {"cost_status": status, "annual_external_cost_usd": None}
    if rates is not None:
        cost["fee_rates"] = rates
    row = _cost_strategy(
        annual_external_cost_usd=None,
        annual_external_cost_rate_pre_tax=None,
        cost_status=status,
        cost_class="PERCENT_RATE" if rates is not None else None,
        cost_rate_status="UNAVAILABLE_NO_COST_AMOUNT",
        cost_rate_reason="the cost is a documented percent rate; no dollar amount is published",
        fees_status=report.fees_status_for_cost(status, None),
        cost_rate_disclosure=report.cost_rate_disclosure(cost),
        **fields,
    )
    return row


def _render(tmp_path: Path, row: dict[str, Any]) -> str:
    return _flat(
        _run(tmp_path, _capital_payload(row), "renderCapital"), "#strategy-table"
    )


def test_volatis_qualified_row_prints_the_generated_sentence(tmp_path: Path) -> None:
    row = _percent_strategy(
        docs.volatis_rates("QUALIFIED_0_1M"),
        "documented_rate_percent",
        strategy_name="Volatis Strategy",
    )
    table = _render(tmp_path, row)

    assert "Documented percent rate · no dollar amount (percent-based)" in table
    assert "management fee 0%; performance fee 20%" in table
    assert "Fees Documented percent rate (no dollar cost)" in table
    assert "No dollar cost (percent-based)" in table
    assert "per year" not in table and "annually" not in table
    assert "Upper bound" not in table and "Evidenced" not in table


def test_volatis_non_qualified_and_unsettled_rows_render(tmp_path: Path) -> None:
    flat = _render(
        tmp_path,
        _percent_strategy(
            docs.volatis_rates("NON_QUALIFIED_FLAT"), "documented_rate_percent"
        ),
    )
    assert "management fee 3.5%" in flat
    assert "performance fee not stated in the documentation" in flat

    unsettled = _render(
        tmp_path,
        _percent_strategy(None, "documented_rate_tier_unsettled"),
    )
    assert "Percent-based; fee tier not settled" in unsettled
    assert "the fee tier is not settled, so no rate is claimed" in unsettled
    assert "20%" not in unsettled and "3.5%" not in unsettled
    assert "management fee" not in unsettled and "performance fee" not in unsettled
    assert "Fees Unavailable" in unsettled


def test_robuxio_row_prints_redemption_lock_and_placement(tmp_path: Path) -> None:
    table = _render(
        tmp_path,
        _percent_strategy(docs.robuxio_rates(), "documented_rate_percent"),
    )

    assert "performance fee 30% (high-water mark: over the high-water mark)" in table
    assert "fee placement: inside the vehicle NAV" in table
    assert "redemption fee 0.5% (placement: billed outside the NAV)" in table
    assert "lock 30 days" in table


def test_a_percent_row_is_not_counted_as_a_known_cost(tmp_path: Path) -> None:
    payload = _capital_payload(
        _percent_strategy(docs.robuxio_rates(), "documented_rate_percent")
    )
    dom = _run(tmp_path, payload, "renderSignals")

    assert "0/" in _flat(dom, "#signal-kpis")

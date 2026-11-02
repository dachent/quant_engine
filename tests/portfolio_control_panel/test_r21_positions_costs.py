"""R2.1 renderer: null market values say so, the as-of basis is shown, cost components are printed as published."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_shell_f3_e1_render import _symbol_row
from test_shell_qc1_labels import (
    _capital_payload,
    _cost_strategy,
    _flat,
    _positions_payload,
    _run,
)


def _position_cells(dom: dict[str, Any]) -> list[str]:
    rows = re.findall(
        r"<tr data-account-id=.*?</tr>", dom["#position-table"]["innerHTML"]
    )
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip() for row in rows]


def _row(**fields: Any) -> dict[str, Any]:
    row = _symbol_row("AVAILABLE", None, "SYNA", "a1")
    row["market_value_usd"] = None
    row.update(fields)
    return row


def test_a_null_market_value_says_no_value_was_published(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_row()]
    cells = _position_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "No market value published for this row" in cells[0]


def test_a_published_reason_is_printed_in_words(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_row(market_value_reason="SNAPSHOT_CARRIES_NO_VALUE")]
    cells = _position_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "Snapshot carries no value" in cells[0]
    assert "No market value published" not in cells[0]


def test_the_as_of_basis_and_capture_date_sit_beside_the_date(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [
        _row(
            as_of_basis="CAPTURED_AFTER_CLOSE_OF_AS_OF_DATE",
            capture_date="2026-09-18",
        )
    ]
    cells = _position_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "Captured after close of as of date" in cells[0]
    assert "captured Sep 18, 2026" in cells[0]


def test_a_priced_row_has_no_missing_value_note(tmp_path: Path) -> None:
    payload = _positions_payload()
    payload["positions"] = [_row(market_value_usd="1250.50")]
    cells = _position_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert "No market value published" not in cells[0]
    assert "$1,251" in cells[0]


def test_cost_components_are_printed_beside_the_status_and_annual_amount(
    tmp_path: Path,
) -> None:
    row = _cost_strategy(
        strategy_id="syn",
        strategy_name="Synthetic Strategy",
        annual_external_cost_usd="600",
        cost_status="provider_declared",
        cost_components=[
            {
                "kind": "SUBSCRIPTION",
                "amount": "50.00",
                "currency": "USD",
                "period": "MONTH",
                "annual_equivalent_usd": "600.00",
                "status": "provider_declared",
                "source_label": "provider-declared price",
                "corroboration_label": "synthetic card feed charges",
                "window_start": None,
                "window_end": None,
            },
            {
                "kind": "EXECUTION",
                "amount": "12.30",
                "currency": "EUR",
                "period": "PERIOD_TO_DATE",
                "annual_equivalent_usd": None,
                "status": "observed",
                "source_label": "broker commissions",
                "corroboration_label": None,
                "window_start": "2026-01-05",
                "window_end": "2026-08-28",
            },
        ],
    )
    table = _flat(
        _run(tmp_path, _capital_payload(row), "renderCapital"), "#strategy-table"
    )

    assert "Subscription 50.00 USD per month · $600.00 a year" in table
    assert "provider-declared price; synthetic card feed charges" in table
    assert "Execution 12.30 EUR per period to date Jan 5, 2026 to Aug 28, 2026" in table
    assert "broker commissions" in table
    assert "a year" in table and table.count("a year") == 1


def test_rows_without_components_print_only_the_status_and_amount(
    tmp_path: Path,
) -> None:
    row = _cost_strategy(
        annual_external_cost_usd="2822.458", cost_status="known_gold_api"
    )
    table = _flat(
        _run(tmp_path, _capital_payload(row), "renderCapital"), "#strategy-table"
    )

    assert "Known (gold API) · $2,822.46 per year" in table
    assert "Subscription" not in table and "Execution" not in table


def test_a_derived_position_says_which_day_it_is_for_and_when_it_was_observed(
    tmp_path: Path,
) -> None:
    """The derived label is generated from the row's own dates and never says a close was captured."""

    payload = _positions_payload()
    basis = {
        "basis": "DERIVED_FROM_OFFICIAL_CLOSES",
        "derivation_record_sha256": "d" * 64,
        "finality_evidence_sha256": "e" * 64,
        "official_closes_manifest_sha256": "c" * 64,
        "provider_finality_guarantee": False,
    }
    payload["positions"] = [
        _row(
            as_of_date="2026-03-04",
            as_of_basis="DERIVED_FROM_OFFICIAL_CLOSES",
            capture_date="2026-03-05",
            value_basis=basis,
        ),
        _row(
            security_id="same-day",
            as_of_date="2026-03-04",
            as_of_basis="DERIVED_FROM_OFFICIAL_CLOSES",
            value_basis=basis,
        ),
    ]
    dom = _run(tmp_path, payload, "renderPositionTable")
    joined = " ".join(_position_cells(dom))

    assert (
        "Derived from official closes as of Mar 4, 2026 (observed Mar 5, 2026)"
        in joined
    )
    assert joined.count("Derived from official closes as of Mar 4, 2026") == 2
    assert joined.count("(observed") == 1  # the same-day row has no observed day to add
    assert "captured" not in joined
    assert "official closes manifest cccccccc" in dom["#position-table"]["innerHTML"]

"""Schema accept/reject coverage for each governed endpoint, traps included."""

from __future__ import annotations

import json

import pytest

from quant_engine.turingtrader.endpoints import get_endpoint
from quant_engine.turingtrader.errors import (
    NormalizationError,
    SchemaValidationError,
)
from quant_engine.turingtrader.normalize import normalize
from quant_engine.turingtrader.schemas import validate

from synthetic import (
    holdings_history_payload,
    performance_chart_payload,
    performance_table_payload,
    portfolio_comp_payload,
    portfolio_holdings_payload,
)


def _accepts(object_name: str, payload: object) -> None:
    validate(get_endpoint(object_name), payload)


def _rejects(object_name: str, payload: object) -> None:
    with pytest.raises(SchemaValidationError):
        validate(get_endpoint(object_name), payload)


def test_valid_payloads_validate_for_all_endpoints() -> None:
    _accepts("portfolio-comp.json", portfolio_comp_payload())
    _accepts("performance-chart.json", performance_chart_payload())
    _accepts("performance-table.json", performance_table_payload())
    _accepts("holdings-history.json", holdings_history_payload())
    _accepts("portfolio-holdings.json", portfolio_holdings_payload())


def test_portfolio_comp_rejects_cagr_percentiles_as_real_arrays() -> None:
    payload = portfolio_comp_payload()
    entry = payload["strategies"][0]  # type: ignore[index]
    entry["cagr-5th"] = [0.02, 0.03]  # type: ignore[index]
    _rejects("portfolio-comp.json", payload)


def test_portfolio_comp_accepts_stringified_arrays_and_normalizes() -> None:
    payload = portfolio_comp_payload()
    _accepts("portfolio-comp.json", payload)
    rows = normalize("portfolio-comp.json", payload)
    assert rows[0]["cagr-5th"] == [0.02, 0.03]
    assert rows[0]["cagr-95th"] == [0.21, 0.24]


def test_portfolio_comp_stringified_non_array_passes_schema_fails_normalize() -> None:
    payload = portfolio_comp_payload()
    entry = payload["strategies"][0]  # type: ignore[index]
    entry["cagr-5th"] = "0.05"  # type: ignore[index]
    _accepts("portfolio-comp.json", payload)
    with pytest.raises(NormalizationError):
        normalize("portfolio-comp.json", payload)


def test_portfolio_comp_stringified_array_of_strings_fails_normalize() -> None:
    payload = portfolio_comp_payload()
    entry = payload["strategies"][0]  # type: ignore[index]
    entry["cagr-95th"] = json.dumps(["high"])  # type: ignore[index]
    _accepts("portfolio-comp.json", payload)
    with pytest.raises(NormalizationError):
        normalize("portfolio-comp.json", payload)


def test_portfolio_comp_rejects_extra_fields() -> None:
    payload = portfolio_comp_payload()
    entry = payload["strategies"][0]  # type: ignore[index]
    entry["omega"] = 1.7  # type: ignore[index]
    _rejects("portfolio-comp.json", payload)


def test_performance_chart_rejects_bad_date_and_extra_keys() -> None:
    payload = performance_chart_payload()
    payload["series"][0]["date"] = "2026/08/30"  # type: ignore[index]
    _rejects("performance-chart.json", payload)
    payload = performance_chart_payload()
    payload["benchmark"] = []  # type: ignore[index]
    _rejects("performance-chart.json", payload)


def test_performance_table_requires_all_metrics() -> None:
    payload = performance_table_payload()
    del payload["ulcer"]
    _rejects("performance-table.json", payload)
    payload = performance_table_payload()
    payload["sharpe"] = "1.05"
    _rejects("performance-table.json", payload)


def test_holdings_history_rejects_non_2tuple_rows() -> None:
    payload = holdings_history_payload()
    payload["rows"] = [["2026-08-01T00:00:00", "SYNTH=100.00%", "extra"]]
    _rejects("holdings-history.json", payload)
    payload = holdings_history_payload()
    payload["rows"] = [["2026-08-01T00:00:00"]]
    _rejects("holdings-history.json", payload)


def test_holdings_history_rejects_bad_datetime_text() -> None:
    payload = holdings_history_payload()
    payload["rows"] = [["2026-08-01", "SYNTH=100.00%"]]
    _rejects("holdings-history.json", payload)


def test_holdings_history_rejects_bad_signal_string() -> None:
    payload = holdings_history_payload()
    payload["rows"] = [["2026-08-01T00:00:00", "SYNTH 55.00%"]]
    _rejects("holdings-history.json", payload)


def test_holdings_history_normalization_catches_impossible_calendar_date() -> None:
    payload = holdings_history_payload()
    payload["rows"] = [["2026-13-40T00:00:00", "SYNTH=100.00%"]]
    _accepts("holdings-history.json", payload)
    with pytest.raises(NormalizationError):
        normalize("holdings-history.json", payload)


def test_holdings_history_normalizes_dates_and_signals() -> None:
    rows = normalize("holdings-history.json", holdings_history_payload())
    assert rows == [
        {"date": "2026-08-01", "signal": "SYNTH=55.00%, SYNTH2=45.00%"},
        {"date": "2026-09-01", "signal": "SYNTH=100.00%"},
    ]


def test_portfolio_holdings_blank_and_dash_sentinels_normalize_to_null() -> None:
    payload = portfolio_holdings_payload()
    _accepts("portfolio-holdings.json", payload)
    rows = normalize("portfolio-holdings.json", payload)
    assert rows[1] == {"symbol": "SYNTH2", "allocation": None, "price": None}


def test_portfolio_holdings_rejects_other_string_sentinels() -> None:
    payload = portfolio_holdings_payload()
    payload["holdings"][0]["allocation"] = "n/a"  # type: ignore[index]
    _rejects("portfolio-holdings.json", payload)

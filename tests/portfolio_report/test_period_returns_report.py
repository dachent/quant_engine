"""Report version 5: period returns with an explicit annualisation basis (#594).

Synthetic rows through the report's own helpers and its JSON schema.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (REPOSITORY_ROOT / "schemas" / "portfolio" / "report-data.schema.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(scope="module")
def reporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "portfolio_report_period_returns",
        REPOSITORY_ROOT / "tools" / "portfolio_report.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _daily(start: date, days: int, *, weekends: bool = False) -> list[dict[str, str]]:
    rows = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        if not weekends and current.weekday() >= 5:
            continue
        rows.append(
            {
                "date": current.isoformat(),
                "return": "0.001" if offset % 2 == 0 else "-0.0005",
            }
        )
    return rows


def _cumulative(rows: list[dict[str, str]]) -> str:
    wealth = Decimal(1)
    for item in rows:
        wealth *= 1 + Decimal(item["return"])
    return format(wealth - 1, "f")


def test_calendar_year_pieces_multiply_back_to_the_path_and_state_coverage(
    reporter: Any,
) -> None:
    rows = _daily(date(2025, 11, 3), 120)
    pieces = reporter._daily_period_pieces(rows, [])
    assert set(pieces["calendar_year_twr"]) == {"2025", "2026"}
    product = Decimal(1)
    for value in pieces["calendar_year_twr"].values():
        product *= 1 + Decimal(value)
    assert abs(product - 1 - Decimal(_cumulative(rows))) < Decimal("1e-25")
    assert pieces["calendar_year_windows"]["2025"]["covers_period"] is False
    assert pieces["calendar_year_windows"]["2026"]["covers_period"] is True
    assert pieces["periods_per_year"] == 252
    assert pieces["periods_per_year_basis"] == "DAILY_TRADING"
    calendar = reporter._daily_period_pieces(
        _daily(date(2026, 1, 1), 60, weekends=True), []
    )
    assert calendar["periods_per_year"] == 365
    assert calendar["periods_per_year_basis"] == "DAILY_CALENDAR"


def test_a_gap_span_belongs_to_its_start_year_and_keeps_the_product_exact(
    reporter: Any,
) -> None:
    rows = [
        {"date": "2025-12-30", "return": "0.01"},
        {"date": "2025-12-31", "return": "0.02"},
        {"date": "2026-01-12", "return": "0.03"},
    ]
    span = reporter.ReturnSpan(date(2025, 12, 31), date(2026, 1, 11), Decimal("0.05"))
    pieces = reporter._daily_period_pieces(rows, [span])
    assert Decimal(pieces["calendar_year_twr"]["2025"]) == (
        Decimal("1.01") * Decimal("1.02") * Decimal("1.05") - 1
    )
    total = reporter.splice_spans(
        [(date.fromisoformat(r["date"]), Decimal(r["return"])) for r in rows], [span]
    ).cumulative_return
    product = Decimal(1)
    for value in pieces["calendar_year_twr"].values():
        product *= 1 + Decimal(value)
    assert abs(product - 1 - total) < Decimal("1e-25")


def _summary_row(start: str, end: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    cumulative = _cumulative(rows)
    return {
        "strategy_id": "alpha",
        "account_live_observed_start": start,
        "account_live_observed_end": end,
        "account_live_cumulative_return": cumulative,
        "account_live_cagr": "0.5",
        "mwr_xirr": "0.2",
        "mwr_opening_date": start,
        "mwr_terminal_date": end,
    }


def test_strategy_annualised_twr_exists_only_from_365_days(reporter: Any) -> None:
    rows = _daily(date(2026, 7, 1), 70)
    short = _summary_row(rows[0]["date"], rows[-1]["date"], rows)
    fields = reporter._strategy_period_fields(
        short, reporter._daily_period_pieces(rows, [])
    )
    assert fields["twr_annualised"] is None
    assert fields["window_days"] < 365
    basis = fields["annualisation_basis"]
    assert basis["twr"]["annualised_status"] == "SUPPRESSED_UNDER_365D"
    assert basis["account_live_cagr"]["reported_basis"] == "ANNUALISED_FROM_DAYS"
    assert basis["mwr_xirr"]["reported_basis"] == "ANNUALISED_FROM_DAYS"
    assert fields["mwr_window_equivalent"] is not None
    # Accepted values are never rewritten by the additive helper.
    assert set(fields).isdisjoint(short.keys() - {"account_live_cagr"})

    long_rows = _daily(date(2025, 1, 1), 500)
    long = _summary_row(long_rows[0]["date"], long_rows[-1]["date"], long_rows)
    fields = reporter._strategy_period_fields(
        long, reporter._daily_period_pieces(long_rows, [])
    )
    assert fields["twr_annualised"] == reporter._cagr(
        long["account_live_cumulative_return"],
        long["account_live_observed_start"],
        long["account_live_observed_end"],
    )
    assert fields["annualisation_basis"]["mwr_xirr"]["reported_basis"] == "ANNUALISED"
    assert (
        fields["ytd_twr"]
        == fields["calendar_year_twr"][long["account_live_observed_end"][:4]]
    )


def test_strategy_without_a_canonical_series_states_it(reporter: Any) -> None:
    rows = _daily(date(2026, 7, 1), 70)
    row = _summary_row(rows[0]["date"], rows[-1]["date"], rows)
    fields = reporter._strategy_period_fields(row, None)
    assert fields["calendar_year_twr"] == {}
    assert fields["ytd_twr"] is None
    assert fields["periods_per_year"] is None
    assert fields["periods_per_year_basis"] == "UNAVAILABLE_NO_CANONICAL_DAILY_SERIES"


def test_comparison_window_is_read_from_the_annualisation_label(
    reporter: Any,
) -> None:
    label = reporter._annualization_label(
        basis="PERIODS_PER_YEAR_SCALED",
        start="2026-02-01",
        end="2026-07-10",
        observations=100,
        periods_per_year=252,
    )
    assert reporter._label_window(label) == {
        "start": "2026-02-01",
        "end": "2026-07-10",
        "days": 159,
        "observations": 100,
    }
    assert reporter._label_window(None) is None


def test_epoch_payload_copies_only_the_manifest_period_fields(reporter: Any) -> None:
    row = {
        "twr": "0.1",
        "window_days": 40,
        "twr_annualised": None,
        "annualisation_basis": {"twr": {"reported_basis": "CUMULATIVE"}},
    }
    assert reporter._epoch_period_payload(row) == {
        "window_days": 40,
        "twr_annualised": None,
        "annualisation_basis": {"twr": {"reported_basis": "CUMULATIVE"}},
    }


def _performance_validator() -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": "#/$defs/performance", "$defs": SCHEMA["$defs"]},
        format_checker=FormatChecker(),
    )


def test_schema_accepts_period_fields_on_a_performance_row() -> None:
    row = {
        "account_id": "A1",
        "account_label": None,
        "assignment_id": "x",
        "effective_end": None,
        "effective_start": "2026-08-01",
        "flow_count": 0,
        "flow_timing": None,
        "last_four": None,
        "mwr_status": "AVAILABLE",
        "mwr_xirr": "0.3",
        "nav_observations": 5,
        "observed_end": "2026-09-10",
        "observed_start": "2026-08-01",
        "pool_id": None,
        "pool_mapping_status": None,
        "strategy_id": "alpha",
        "strategy_mapping_disposition": None,
        "twr": "0.03",
        "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
        "return_window_start": "2026-08-01",
        "return_close_date": "2026-09-10",
        "window_days": 40,
        "twr_annualised": None,
        "ytd_twr": "0.03",
        "ytd_window": {
            "start": "2026-08-01",
            "end": "2026-09-10",
            "days": 40,
            "covers_period": False,
        },
        "calendar_year_twr": {"2026": "0.03"},
        "calendar_year_windows": {
            "2026": {
                "start": "2026-08-01",
                "end": "2026-09-10",
                "days": 40,
                "covers_period": False,
            }
        },
        "periods_per_year": 252,
        "periods_per_year_basis": "DAILY_TRADING",
        "mwr_window_equivalent": "0.03",
        "annualisation_basis": {
            "twr": {
                "reported_basis": "CUMULATIVE",
                "window_days": 40,
                "annualised_field": "twr_annualised",
                "annualised_status": "SUPPRESSED_UNDER_365D",
                "annualised_method": None,
            },
            "mwr_xirr": {
                "reported_basis": "ANNUALISED_FROM_DAYS",
                "window_days": 40,
                "method": "XIRR_ACT_365",
            },
        },
    }
    validator = _performance_validator()
    assert list(validator.iter_errors(row)) == []
    bad = {**row, "annualisation_basis": {"mwr_xirr": {"reported_basis": "SOMETIMES"}}}
    assert list(validator.iter_errors(bad))
    unknown = {**row, "annualised_basis": "x"}
    assert list(validator.iter_errors(unknown))


def test_versions_before_five_forbid_the_period_fields() -> None:
    gates = [
        entry
        for entry in SCHEMA["allOf"]
        if entry.get("if", {}).get("properties", {}).get("report_schema_version")
        == {"enum": [1, 2, 3, 4]}
    ]
    assert len(gates) == 1
    assert SCHEMA["properties"]["report_schema_version"]["enum"] == [1, 2, 3, 4, 5]

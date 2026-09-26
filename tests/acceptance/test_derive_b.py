"""Hand-computed behavioral tests for the independent derivation."""

from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2] / "tools" / "acceptance" / "derive_b" / "derive.py"
)


def load_derivation_module():
    spec = importlib.util.spec_from_file_location("derive_b", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_twr_removes_end_of_day_external_flow_before_return():
    """Removing the flow adjustment would report 15%, rather than the hand-calculated 5%."""
    derivation = load_derivation_module()

    result = derivation.time_weighted_return(
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("115")),
        ],
        {date(2026, 1, 2): Decimal("10")},
    )

    assert result == Decimal("0.05")


def test_twr_compounds_each_subperiod():
    """Replacing compounded subperiod returns with an arithmetic sum would fail this case."""
    derivation = load_derivation_module()

    result = derivation.time_weighted_return(
        [
            (date(2026, 1, 1), Decimal("100")),
            (date(2026, 1, 2), Decimal("110")),
            (date(2026, 1, 3), Decimal("171")),
        ],
        {date(2026, 1, 3): Decimal("50")},
    )

    assert result == Decimal("0.21")


def test_xirr_solves_actual_365_one_year_gain():
    """A wrong cash-flow sign or day-count convention would not yield the literal 10% result."""
    derivation = load_derivation_module()

    result = derivation.xirr(
        [
            (date(2025, 1, 1), Decimal("-100")),
            (date(2026, 1, 1), Decimal("110")),
        ]
    )

    assert result is not None
    assert abs(result - Decimal("0.10")) < Decimal("0.00000000000000000001")


def test_xirr_rejects_non_unique_sign_pattern():
    """Accepting a multiple-sign-change series would claim an unproved unique money-weighted return."""
    derivation = load_derivation_module()

    result = derivation.xirr(
        [
            (date(2025, 1, 1), Decimal("-100")),
            (date(2026, 1, 1), Decimal("230")),
            (date(2027, 1, 1), Decimal("-132")),
        ]
    )

    assert result is None


def test_aggregate_capital_keeps_excluded_value_out_of_systematic_total():
    """Including retained/excluded capital in systematic capital would change the hand-calculated totals."""
    derivation = load_derivation_module()

    result = derivation.aggregate_capital(
        {
            "account-a": Decimal("100.00"),
            "account-b": Decimal("250.00"),
            "account-excluded": Decimal("25.00"),
        },
        {"account-a": "strategy-one", "account-b": "strategy-two"},
        {"account-excluded"},
        Decimal("40.00"),
    )

    assert result == {
        "strategies": {
            "strategy-one": Decimal("100.00"),
            "strategy-two": Decimal("250.00"),
        },
        "retained_excluded": Decimal("25.00"),
        "systematic": Decimal("350.00"),
        "liquid": Decimal("390.00"),
    }

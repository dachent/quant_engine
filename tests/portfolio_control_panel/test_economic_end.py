"""The economic end of a retired epoch (J10) and the lifecycle rule both programs derive."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import build_account_manifest_generation as builder
import verify_account_manifest_generation as verifier


def _leg(cls: str, event: str, recognition: str) -> dict[str, str]:
    return {
        "class": cls,
        "event_date": event,
        "nav_recognition_date": recognition,
        "amount": "1",
    }


CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "exit the day before the NAV day",
        {
            "status": "OK",
            "effective_end": "2024-12-13",
            "current_value": "0",
            "capital_flow_trace": [
                _leg("INTERNAL_IN", "2024-07-24", "2024-07-25"),
                _leg("INTERNAL_OUT", "2024-12-12", "2024-12-13"),
            ],
        },
    ),
    (
        "exit on the end day",
        {
            "status": "OK",
            "effective_end": "2026-06-23",
            "current_value": "0",
            "capital_flow_trace": [_leg("INTERNAL_OUT", "2026-06-23", "2026-06-23")],
        },
    ),
    (
        "the latest outgoing leg decides",
        {
            "status": "OK",
            "effective_end": "2026-07-27",
            "current_value": "0",
            "capital_flow_trace": [
                _leg("INTERNAL_OUT", "2026-07-20", "2026-07-20"),
                _leg("EXTERNAL_OUT", "2026-07-26", "2026-07-27"),
            ],
        },
    ),
    (
        "an epoch that still holds value has none",
        {
            "status": "OK",
            "effective_end": "2026-06-23",
            "current_value": "5",
            "capital_flow_trace": [_leg("INTERNAL_OUT", "2026-06-22", "2026-06-23")],
        },
    ),
    (
        "an open epoch has none",
        {
            "status": "OK",
            "effective_end": None,
            "current_value": "0",
            "capital_flow_trace": [_leg("INTERNAL_OUT", "2026-06-22", "2026-06-23")],
        },
    ),
    (
        "a closed epoch whose assignment is still open ends on its close date",
        {
            "status": "OK",
            "effective_end": None,
            "performance_closed_on": "2026-06-23",
            "current_value": "0",
            "capital_flow_trace": [_leg("INTERNAL_OUT", "2026-06-22", "2026-06-23")],
        },
    ),
    (
        "no outgoing leg",
        {
            "status": "OK",
            "effective_end": "2026-06-23",
            "current_value": "0",
            "capital_flow_trace": [_leg("INTERNAL_IN", "2026-06-22", "2026-06-23")],
        },
    ),
]


@pytest.mark.parametrize(("name", "row"), CASES, ids=[name for name, _ in CASES])
def test_builder_and_independent_verifier_derive_the_same_economic_end(
    name: str, row: dict[str, Any]
) -> None:
    end = row["effective_end"]
    produced = builder._epoch_lifecycle_fields(row, "INCLUDED_PRODUCT_EPOCH")
    derived = verifier._independent_epoch_lifecycle(
        end,
        row.get("performance_closed_on"),
        "INCLUDED_PRODUCT_EPOCH",
        {
            "current_value": Decimal(row["current_value"]),
            "capital_flow_trace": row["capital_flow_trace"],
        },
    )
    assert produced == derived, name


def test_the_recognition_day_basis_is_named_for_a_next_day_exit() -> None:
    fields = builder._epoch_lifecycle_fields(CASES[0][1], "INCLUDED_PRODUCT_EPOCH")
    assert fields["retired_on"] == "2024-12-13"
    assert fields["economic_end_date"] == "2024-12-12"
    assert fields["end_recognition_basis"] == "NAV_RECOGNITION_DAY_AFTER_EXIT_FILL"
    same_day = builder._epoch_lifecycle_fields(CASES[1][1], "INCLUDED_PRODUCT_EPOCH")
    assert same_day["end_recognition_basis"] == "EXIT_FILL_DAY_IS_END_DAY"
    held = builder._epoch_lifecycle_fields(CASES[3][1], "INCLUDED_PRODUCT_EPOCH")
    assert held["economic_end_date"] is None and held["end_recognition_basis"] is None


def test_a_closed_epoch_is_dated_by_its_own_close_not_by_the_open_assignment() -> None:
    fields = builder._epoch_lifecycle_fields(CASES[5][1], "INCLUDED_PRODUCT_EPOCH")
    assert fields["lifecycle_state"] == "CLOSED"
    assert fields["economic_end_date"] == "2026-06-22"
    assert fields["end_recognition_basis"] == "NAV_RECOGNITION_DAY_AFTER_EXIT_FILL"

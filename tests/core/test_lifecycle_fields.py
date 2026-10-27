"""Lifecycle state of epochs and strategies derives only from governed facts (#592)."""

from __future__ import annotations

import pytest

from lifecycle_fields import (
    LIFECYCLE_ROW_FIELDS,
    LifecycleError,
    epoch_lifecycle,
    parent_assignment_id,
    strategy_lifecycle,
)


def test_open_included_epoch_is_active() -> None:
    fields = epoch_lifecycle(
        effective_end=None,
        closed_on=None,
        product_disposition="INCLUDED_PRODUCT_EPOCH",
    )
    assert tuple(fields) == LIFECYCLE_ROW_FIELDS
    assert fields == {
        "lifecycle_state": "ACTIVE",
        "retired_on": None,
        "closed_on": None,
        "parent_assignment_id": None,
        "sleeve_label": None,
    }


def test_epoch_without_a_crosswalk_disposition_is_never_inactive() -> None:
    fields = epoch_lifecycle(
        effective_end=None, closed_on=None, product_disposition=None
    )
    assert fields["lifecycle_state"] == "ACTIVE"


def test_open_epoch_outside_the_product_is_inactive() -> None:
    fields = epoch_lifecycle(
        effective_end=None,
        closed_on=None,
        product_disposition="EXCLUDED_IMMATERIAL",
    )
    assert fields["lifecycle_state"] == "INACTIVE"
    assert fields["retired_on"] is None


def test_ended_epoch_is_retired_on_its_effective_end() -> None:
    fields = epoch_lifecycle(
        effective_end="2025-11-24",
        closed_on=None,
        product_disposition="EXCLUDED_DEPRECATED_HISTORY_ONLY",
    )
    assert fields["lifecycle_state"] == "RETIRED"
    assert fields["retired_on"] == "2025-11-24"
    assert fields["closed_on"] is None


def test_epoch_closed_inside_its_window_is_closed() -> None:
    fields = epoch_lifecycle(
        effective_end="2026-06-21",
        closed_on="2026-06-18",
        product_disposition="INCLUDED_PRODUCT_EPOCH",
    )
    assert fields["lifecycle_state"] == "CLOSED"
    assert fields["retired_on"] == "2026-06-21"
    assert fields["closed_on"] == "2026-06-18"


@pytest.mark.parametrize("bad", ["2026-6-1", "20260601", 20260601, "2026-02-30"])
def test_malformed_dates_stop_the_derivation(bad: object) -> None:
    with pytest.raises(LifecycleError):
        epoch_lifecycle(effective_end=bad, closed_on=None, product_disposition=None)


def test_required_current_strategy_is_active_whatever_its_epochs() -> None:
    fields = strategy_lifecycle(
        current_required=True,
        effective_ends=["2026-01-04", None],
        closed_ons=[None, None],
    )
    assert fields["lifecycle_state"] == "ACTIVE"
    assert fields["retired_on"] is None


def test_strategy_with_an_open_epoch_outside_the_product_is_inactive() -> None:
    fields = strategy_lifecycle(
        current_required=False, effective_ends=[None], closed_ons=[None]
    )
    assert fields["lifecycle_state"] == "INACTIVE"


def test_strategy_whose_epochs_all_ended_is_retired_on_the_last_end() -> None:
    fields = strategy_lifecycle(
        current_required=False,
        effective_ends=["2024-12-12", "2025-03-01"],
        closed_ons=[None, None],
    )
    assert fields["lifecycle_state"] == "RETIRED"
    assert fields["retired_on"] == "2025-03-01"
    assert fields["closed_on"] is None


def test_strategy_whose_epochs_all_closed_is_closed() -> None:
    fields = strategy_lifecycle(
        current_required=False,
        effective_ends=["2026-07-22"],
        closed_ons=["2026-07-20"],
    )
    assert fields["lifecycle_state"] == "CLOSED"
    assert fields["closed_on"] == "2026-07-20"
    assert fields["retired_on"] == "2026-07-22"


def test_strategy_without_any_epoch_is_inactive() -> None:
    fields = strategy_lifecycle(
        current_required=False, effective_ends=[], closed_ons=[]
    )
    assert fields["lifecycle_state"] == "INACTIVE"


def _assignment(
    assignment_id: str, account_id: str, start: str | None, end: str | None
) -> dict[str, object]:
    return {
        "assignment_id": assignment_id,
        "account_id": account_id,
        "effective_start": start,
        "effective_end": end,
    }


def test_parent_is_the_earliest_overlapping_assignment_of_the_parent_account() -> None:
    assignments = [
        _assignment("a-late", "parent", "2026-01-22", None),
        _assignment("a-early", "parent", "2025-01-01", "2026-01-21"),
        _assignment("a-other", "other", "2025-01-01", None),
    ]
    assert parent_assignment_id(assignments, "parent", "2025-06-01", "2025-07-01") == (
        "a-early"
    )
    assert parent_assignment_id(assignments, "parent", "2025-06-01", None) == "a-early"
    assert parent_assignment_id(assignments, "parent", "2026-02-01", None) == "a-late"


def test_sleeve_window_no_parent_assignment_overlaps_has_no_parent() -> None:
    assignments = [_assignment("a1", "parent", "2025-01-01", "2025-02-01")]
    with pytest.raises(LifecycleError, match="overlaps"):
        parent_assignment_id(assignments, "parent", "2025-03-01", None)
    with pytest.raises(LifecycleError, match="overlaps"):
        parent_assignment_id(assignments, "missing", "2025-01-15", None)

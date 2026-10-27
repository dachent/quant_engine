"""Lifecycle state of epochs and strategies (#592, PANEL-VIS-001).

The panel shows inactive and retired strategies as a muted group and nests
sleeves under their parent.  Both need a state below strategy level.  Every
value here is derived from a governed fact that already exists: the assignment's
``effective_end``, the epoch's ``performance_closed_on``, the current-epoch
crosswalk's product disposition, and the nested-strategy tag map.  Nothing is
typed by hand and no return, capital or window value is read or changed.

The derivation is deliberately small and total:

* ``CLOSED``   the epoch's capital was closed inside the epoch
  (``performance_closed_on`` is set);
* ``RETIRED``  the assignment ended (``effective_end`` is set) without a close;
* ``INACTIVE`` the assignment is open but outside the current product (the
  crosswalk excludes it, or the strategy is a retained history-only record);
* ``ACTIVE``   everything else.

A payload carrying these fields declares ``lifecycle_schema_version`` 1.  A
payload without that member is the previous schema and stays readable.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Final, Mapping, Sequence

LIFECYCLE_SCHEMA_VERSION: Final = 1
LIFECYCLE_STATES: Final = ("ACTIVE", "INACTIVE", "RETIRED", "CLOSED")
LIFECYCLE_ROW_FIELDS: Final = (
    "lifecycle_state",
    "retired_on",
    "closed_on",
    "parent_assignment_id",
    "sleeve_label",
)
INCLUDED_PRODUCT_EPOCH: Final = "INCLUDED_PRODUCT_EPOCH"
RETAINED_HISTORY_ONLY: Final = "RETAINED_HISTORY_ONLY"
_ISO_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LifecycleError(ValueError):
    """A lifecycle fact is malformed or a link cannot be proven."""


def _optional_date(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise LifecycleError(f"{name} must be an ISO date or null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise LifecycleError(f"{name} must be an ISO date or null") from exc
    return value


def epoch_lifecycle(
    *,
    effective_end: object,
    closed_on: object,
    product_disposition: object,
) -> dict[str, Any]:
    """Lifecycle fields of one account-strategy epoch.

    ``product_disposition`` is the crosswalk's ``product_epoch_disposition``;
    ``None`` means no crosswalk names the epoch, which never marks it inactive.
    """

    end = _optional_date(effective_end, "effective_end")
    closed = _optional_date(closed_on, "closed_on")
    if closed is not None:
        state = "CLOSED"
    elif end is not None:
        state = "RETIRED"
    elif product_disposition is not None and (
        product_disposition != INCLUDED_PRODUCT_EPOCH
    ):
        state = "INACTIVE"
    else:
        state = "ACTIVE"
    return {
        "lifecycle_state": state,
        "retired_on": end if state in {"RETIRED", "CLOSED"} else None,
        "closed_on": closed if state == "CLOSED" else None,
        "parent_assignment_id": None,
        "sleeve_label": None,
    }


def strategy_lifecycle(
    *,
    current_required: bool,
    effective_ends: Sequence[object],
    closed_ons: Sequence[object],
) -> dict[str, Any]:
    """Lifecycle fields of one strategy from its epochs' governed ends.

    A REQUIRED current strategy is ``ACTIVE``.  Any other strategy is
    ``INACTIVE`` while one of its epochs is open, ``CLOSED`` when every epoch
    ended with a close, and ``RETIRED`` when every epoch ended.
    """

    ends = [_optional_date(item, "effective_end") for item in effective_ends]
    closes = [_optional_date(item, "closed_on") for item in closed_ons]
    if current_required:
        state = "ACTIVE"
    elif not ends or any(item is None for item in ends):
        state = "INACTIVE"
    elif closes and len(closes) == len(ends) and all(closes):
        state = "CLOSED"
    else:
        state = "RETIRED"
    retired = max((item for item in ends if item is not None), default=None)
    closed = max((item for item in closes if item is not None), default=None)
    return {
        "lifecycle_state": state,
        "retired_on": retired if state in {"RETIRED", "CLOSED"} else None,
        "closed_on": closed if state == "CLOSED" else None,
        "parent_assignment_id": None,
        "sleeve_label": None,
    }


def parent_assignment_id(
    assignments: Sequence[Mapping[str, Any]],
    parent_account_id: str,
    start: str,
    end: str | None,
) -> str:
    """The parent account's assignment that a sleeve window runs inside.

    The earliest-starting assignment of the parent account that overlaps
    ``[start, end]`` is the parent.  A sleeve window that no assignment of the
    parent overlaps has no provable parent and stops the run.
    """

    window_start = _optional_date(start, "sleeve effective_start")
    window_end = _optional_date(end, "sleeve effective_end")
    if window_start is None:
        raise LifecycleError("sleeve effective_start is required")
    candidates: list[tuple[str, str]] = []
    for assignment in assignments:
        if assignment.get("account_id") != parent_account_id:
            continue
        assignment_id = assignment.get("assignment_id")
        if not isinstance(assignment_id, str) or not assignment_id:
            raise LifecycleError("parent assignment_id is invalid")
        first = _optional_date(assignment.get("effective_start"), "parent start")
        last = _optional_date(assignment.get("effective_end"), "parent end")
        starts_before_window_end = (
            window_end is None or first is None or first <= window_end
        )
        ends_after_window_start = last is None or last >= window_start
        if starts_before_window_end and ends_after_window_start:
            candidates.append((first or "", assignment_id))
    if not candidates:
        raise LifecycleError(
            f"no assignment of {parent_account_id} overlaps the sleeve window"
        )
    return min(candidates)[1]

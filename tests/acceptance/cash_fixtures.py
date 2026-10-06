"""Shared fixture helper: a custody cash event carries every field the schema requires."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any


def complete_event(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` with the schema's required fields filled in where the test left them out.

    Fields the test states are kept as they are (so a test can break one on purpose after
    this); the defaults describe a native USD flow.
    """

    completed = dict(event)
    signed = completed.get("signed_usd")
    completed.setdefault("event_date", "2025-01-01")
    completed.setdefault(
        "record_id",
        "fixture:"
        + hashlib.sha256(repr(sorted(event.items())).encode()).hexdigest()[:12],
    )
    completed.setdefault(
        "event_sha256", hashlib.sha256(str(completed["record_id"]).encode()).hexdigest()
    )
    completed.setdefault("native_currency", "USD")
    if "native_value" not in completed and isinstance(signed, str):
        completed["native_value"] = str(abs(Decimal(signed)))
    completed.setdefault("event_time_fx", None)
    return completed

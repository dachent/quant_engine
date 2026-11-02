"""Capture configuration (``[alpaca_capture]``) and credential plan (``[credentials.alpaca]``).

The account identifier, the paper-or-live environment, and the activity window
are configuration, never code.  The environment has no default: choosing live or
paper is an explicit governed decision that the governance evidence must repeat.
Credentials are two resolver fields, ``key_id`` and ``key_secret``; a candidate
holds a source and a name, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any

from quant_engine.alpaca.capture.catalog import ENVIRONMENTS
from quant_engine.alpaca.capture.errors import CaptureRefused
from quant_engine.alpaca.capture.normalize import KNOWN_ACTIVITY_TYPES
from quant_engine.core import CredentialPlan, plan_from_config

PROVIDER = "alpaca"
CREDENTIAL_FIELDS = frozenset({"key_id", "key_secret"})
_SECTION = "alpaca_capture"
_KEYS = frozenset(
    {
        "account_id",
        "environment",
        "after",
        "until",
        "activity_types",
        "page_size",
        "max_pages",
        "include_positions",
    }
)
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_MAX_PAGES_LIMIT = 1000


@dataclass(frozen=True)
class CaptureConfig:
    account_id: str
    environment: str
    after: str | None
    until: str | None
    activity_types: tuple[str, ...]
    page_size: int
    max_pages: int
    include_positions: bool

    @property
    def base_url(self) -> str:
        return ENVIRONMENTS[self.environment]

    def planned_endpoints(self) -> tuple[str, ...]:
        """The exact ``METHOD path`` set this configuration will request."""

        planned = ["GET /v2/account"]
        if self.include_positions:
            planned.append("GET /v2/positions")
        planned.append("GET /v2/account/activities")
        return tuple(sorted(planned))


def _timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CaptureRefused(f"{field} must be an RFC3339 timestamp or a date")
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError(field)
    except ValueError:
        raise CaptureRefused(
            f"{field} must be an RFC3339 timestamp or a date"
        ) from None
    return value


def capture_config_from_mapping(config: Mapping[str, Any]) -> CaptureConfig:
    table = config.get(_SECTION)
    if not isinstance(table, Mapping):
        raise CaptureRefused(f"configuration section [{_SECTION}] is required")
    if not set(table) <= _KEYS:
        raise CaptureRefused(f"[{_SECTION}] carries an unknown key")
    account_id = table.get("account_id")
    if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
        raise CaptureRefused("account_id is required and must be a plain identifier")
    environment = table.get("environment")
    if environment not in ENVIRONMENTS:
        raise CaptureRefused("environment must be explicitly 'paper' or 'live'")
    types_raw = table.get("activity_types", [])
    if not isinstance(types_raw, list) or not all(
        isinstance(item, str) for item in types_raw
    ):
        raise CaptureRefused("activity_types must be a list of strings")
    if not set(types_raw) <= KNOWN_ACTIVITY_TYPES or len(set(types_raw)) != len(
        types_raw
    ):
        raise CaptureRefused("activity_types names an unknown or repeated type")
    page_size = table.get("page_size", 100)
    max_pages = table.get("max_pages", 200)
    for name, value, high in (
        ("page_size", page_size, 100),
        ("max_pages", max_pages, _MAX_PAGES_LIMIT),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= high
        ):
            raise CaptureRefused(f"{name} must be an integer from 1 to {high}")
    include_positions = table.get("include_positions", True)
    if not isinstance(include_positions, bool):
        raise CaptureRefused("include_positions must be a boolean")
    return CaptureConfig(
        account_id=account_id,
        environment=str(environment),
        after=_timestamp(table.get("after"), "after"),
        until=_timestamp(table.get("until"), "until"),
        activity_types=tuple(sorted(types_raw)),
        page_size=int(page_size),
        max_pages=int(max_pages),
        include_positions=include_positions,
    )


def credential_plan(config: Mapping[str, Any]) -> CredentialPlan:
    """The Alpaca credential plan: exactly ``key_id`` and ``key_secret``."""

    plan = plan_from_config(config, PROVIDER)
    if set(plan.fields) != CREDENTIAL_FIELDS:
        raise CaptureRefused(
            "[credentials.alpaca] must declare exactly key_id and key_secret"
        )
    return plan

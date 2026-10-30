"""Request plan: the exact, hash-bound set of Flex requests a capture may make."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Any

from ._util import canonical_json_bytes, parse_iso_date, require_keys, sha256_hex
from .catalog import WINDOW_SINGLE_DAY, get_query

PLAN_SCHEMA_VERSION = 1
MAX_REQUESTS = 12
_LABEL = re.compile(r"^[0-9a-f]{16}$")


class PlanError(ValueError):
    """A request plan is malformed, outside the catalog, or not final yet."""


@dataclass(frozen=True)
class CaptureRequest:
    query: str
    window_from: date
    window_to: date


@dataclass(frozen=True)
class RequestPlan:
    pull_date: date
    requests: tuple[CaptureRequest, ...]
    expected_account_labels: tuple[str, ...] = ()
    known_non_trading_days: tuple[date, ...] = ()

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "pull_date": self.pull_date.isoformat(),
            "requests": [
                {
                    "query": item.query,
                    "window_from": item.window_from.isoformat(),
                    "window_to": item.window_to.isoformat(),
                }
                for item in self.requests
            ],
            "expected_account_labels": list(self.expected_account_labels),
            "known_non_trading_days": [
                item.isoformat() for item in self.known_non_trading_days
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_document())

    @property
    def sha256(self) -> str:
        return sha256_hex(self.canonical_bytes())


def _validate(plan: RequestPlan) -> None:
    if not 1 <= len(plan.requests) <= MAX_REQUESTS:
        raise PlanError("a plan holds between one and twelve requests")
    seen: set[tuple[str, date, date]] = set()
    for item in plan.requests:
        try:
            query = get_query(item.query)
        except KeyError:
            raise PlanError("a request names a query outside the catalog") from None
        if item.window_from > item.window_to:
            raise PlanError("a request window is reversed")
        span = (item.window_to - item.window_from).days + 1
        if span > query.max_window_days:
            raise PlanError("a request window exceeds the catalog maximum")
        if query.window_kind == WINDOW_SINGLE_DAY and span != 1:
            raise PlanError("this query accepts exactly one day")
        if item.window_to >= plan.pull_date:
            # Flex finalizes a day only after it closes; never ask for a day that
            # is not at least one calendar day behind the pull.
            raise PlanError("a request window must end before the pull date")
        key = (item.query, item.window_from, item.window_to)
        if key in seen:
            raise PlanError("a request is duplicated")
        seen.add(key)
    if len(set(plan.expected_account_labels)) != len(plan.expected_account_labels):
        raise PlanError("expected account labels are duplicated")
    if any(_LABEL.fullmatch(label) is None for label in plan.expected_account_labels):
        raise PlanError("expected account labels must be masked 16-hex labels")
    if len(set(plan.known_non_trading_days)) != len(plan.known_non_trading_days):
        raise PlanError("known non-trading days are duplicated")


def plan_from_document(document: Mapping[str, Any]) -> RequestPlan:
    try:
        root = require_keys(
            dict(document),
            "plan",
            {
                "schema_version",
                "pull_date",
                "requests",
                "expected_account_labels",
                "known_non_trading_days",
            },
        )
        if root["schema_version"] != PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported plan schema")
        raw_requests = root["requests"]
        raw_labels = root["expected_account_labels"]
        raw_days = root["known_non_trading_days"]
        if (
            not isinstance(raw_requests, list)
            or not isinstance(raw_labels, list)
            or not isinstance(raw_days, list)
        ):
            raise ValueError("plan lists are malformed")
        requests = []
        for entry in raw_requests:
            row = require_keys(entry, "request", {"query", "window_from", "window_to"})
            if not isinstance(row["query"], str):
                raise ValueError("request query must be a string")
            requests.append(
                CaptureRequest(
                    row["query"],
                    parse_iso_date(row["window_from"], "window_from"),
                    parse_iso_date(row["window_to"], "window_to"),
                )
            )
        if not all(isinstance(label, str) for label in raw_labels):
            raise ValueError("expected account labels must be strings")
        plan = RequestPlan(
            pull_date=parse_iso_date(root["pull_date"], "pull_date"),
            requests=tuple(requests),
            expected_account_labels=tuple(raw_labels),
            known_non_trading_days=tuple(
                parse_iso_date(item, "known_non_trading_days") for item in raw_days
            ),
        )
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    _validate(plan)
    return plan


def load_plan(data: bytes) -> RequestPlan:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PlanError("the plan must be UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise PlanError("the plan must be an object")
    return plan_from_document(document)

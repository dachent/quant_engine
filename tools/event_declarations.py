"""Pinned per-account event-interval declarations for the baseline fact matrix.

The baseline never invents completeness: an account's flows/transactions row is
DECLARED_COMPLETE only when a hash-pinned declaration says so, names the method that
verified it, and is consistent with the accepted records (it covers the observed range,
reaches the cutoff, no unavailable amount).  An account with no event source is declared
STRUCTURAL_NO_EVENT_SOURCE with its reason: labelled and neutral, never complete.
A declaration that contradicts the records is refused, never ignored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final

COMPLETE: Final = "DECLARED_COMPLETE"
STRUCTURAL: Final = "STRUCTURAL_NO_EVENT_SOURCE"
METHODS: Final = frozenset(
    {
        "NAV_ROLLFORWARD_CLOSURE",
        "PROVIDER_ACTIVITY_PINS",
        "PROVIDER_STATEMENT_PINS",
    }
)
FACTS: Final = frozenset({"flows", "transactions"})
_PIN_KEYS: Final = {"path", "sha256", "size_bytes"}
_SHA: Final = re.compile(r"^[0-9a-f]{64}$")


class EventDeclarationError(ValueError):
    """A declaration is not pinned, well formed, or consistent with the records."""


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise EventDeclarationError(f"{label} is not a date") from exc


def load(pin: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the pinned declaration file; one row per (account_id, fact_type)."""

    if set(pin) != _PIN_KEYS:
        raise EventDeclarationError("event declarations pin has wrong keys")
    try:
        raw = Path(str(pin["path"])).read_bytes()
    except OSError as exc:
        raise EventDeclarationError("event declarations file is unreadable") from exc
    if (
        hashlib.sha256(raw).hexdigest() != pin["sha256"]
        or len(raw) != pin["size_bytes"]
    ):
        raise EventDeclarationError("event declarations differ from their pin")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise EventDeclarationError("event declarations are not JSON") from exc
    if (
        not isinstance(body, dict)
        or body.get("schema_version") != 1
        or not isinstance(body.get("declarations"), list)
    ):
        raise EventDeclarationError("event declarations shape differs")
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in body["declarations"]:
        if not isinstance(item, dict):
            raise EventDeclarationError("a declaration must be an object")
        key = (str(item.get("account_id")), str(item.get("fact_type")))
        if not key[0] or key[1] not in FACTS:
            raise EventDeclarationError("a declaration names no account or fact")
        if key in rows:
            raise EventDeclarationError("a declaration is duplicated")
        status = item.get("status")
        if status == COMPLETE:
            if item.get("method") not in METHODS:
                raise EventDeclarationError("a declaration method is unknown")
            start = _day(item.get("declared_start"), "declared_start")
            end = _day(item.get("declared_end"), "declared_end")
            if end < start:
                raise EventDeclarationError("a declared interval is reversed")
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise EventDeclarationError("a declaration carries no evidence")
            for ref in evidence:
                if (
                    not isinstance(ref, dict)
                    or not str(ref.get("artifact_id", "")).strip()
                    or not _SHA.fullmatch(str(ref.get("sha256", "")))
                    or not isinstance(ref.get("size_bytes"), int)
                ):
                    raise EventDeclarationError("a declaration evidence pin is invalid")
        elif status == STRUCTURAL:
            if not str(item.get("reason", "")).strip():
                raise EventDeclarationError("a structural declaration needs a reason")
        else:
            raise EventDeclarationError("a declaration status is unknown")
        rows[key] = item
    return rows


def apply(
    matrix: Sequence[Mapping[str, Any]],
    declarations: Mapping[tuple[str, str], Mapping[str, Any]],
    as_of_date: date,
) -> list[dict[str, Any]]:
    """The matrix with each declared row set; every declaration must meet a row."""

    by_key = {(str(row["account_id"]), str(row["fact_type"])): row for row in matrix}
    missing = sorted(set(declarations) - set(by_key))
    if missing:
        raise EventDeclarationError(
            f"declaration names an account or fact outside the matrix: {missing[0]}"
        )
    out: list[dict[str, Any]] = []
    for row in matrix:
        merged = dict(row)
        decl = declarations.get((str(row["account_id"]), str(row["fact_type"])))
        if decl is not None:
            merged.update(_declared_row(row, decl, as_of_date))
        out.append(merged)
    return out


def _declared_row(
    row: Mapping[str, Any], decl: Mapping[str, Any], as_of_date: date
) -> dict[str, Any]:
    where = f"{row['fact_type']}:{row['account_id']}"
    if decl["status"] == STRUCTURAL:
        if row.get("record_count"):
            raise EventDeclarationError(
                f"structural declaration contradicts accepted records: {where}"
            )
        return {
            "authority": "PINNED_EVENT_DECLARATION",
            "boundary_basis": STRUCTURAL,
            "disposition": STRUCTURAL,
            "missing_interval_reason": str(decl["reason"]).strip(),
            "status": STRUCTURAL,
        }
    start = _day(decl["declared_start"], "declared_start")
    end = _day(decl["declared_end"], "declared_end")
    if end < as_of_date:
        raise EventDeclarationError(
            f"declared interval stops before the cutoff: {where}"
        )
    if row.get("unavailable_amount_count"):
        raise EventDeclarationError(
            f"declared complete with unavailable amounts: {where}"
        )
    observed_start, observed_end = row.get("observed_start"), row.get("observed_end")
    if observed_start is not None and not (
        start <= _day(observed_start, "observed_start")
        and _day(observed_end, "observed_end") <= end
    ):
        raise EventDeclarationError(
            f"declared interval does not cover the accepted records: {where}"
        )
    return {
        "authority": "PINNED_EVENT_DECLARATION",
        "boundary_basis": str(decl["method"]),
        "declared_end": end.isoformat(),
        "declared_start": start.isoformat(),
        "disposition": COMPLETE,
        "evidence": [dict(ref) for ref in decl["evidence"]],
        "missing_interval_reason": f"none: declared complete by {decl['method']}",
        "missing_intervals": [],
        "status": COMPLETE,
    }

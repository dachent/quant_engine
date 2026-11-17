from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import event_declarations as ed

CUTOFF = date(2026, 11, 6)
EVIDENCE = [{"artifact_id": "ibkr/flex.xml", "sha256": "a" * 64, "size_bytes": 10}]


def _row(account: str = "a1", fact: str = "flows", **extra: Any) -> dict[str, Any]:
    return {
        "account_id": account,
        "fact_type": fact,
        "record_count": 2,
        "observed_start": "2026-01-10",
        "observed_end": "2026-08-03",
        "unavailable_amount_count": 0,
        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
        "boundary_basis": "OBSERVED_RECORDS_ONLY",
        "declared_start": None,
        "declared_end": None,
        "disposition": "RETAINED_ACCEPTED_REPORTED_FACTS_ONLY",
        "missing_interval_reason": "not declared",
        "missing_intervals": [],
        **extra,
    }


def _decl(**extra: Any) -> dict[str, Any]:
    return {
        "account_id": "a1",
        "fact_type": "flows",
        "status": "DECLARED_COMPLETE",
        "method": "NAV_ROLLFORWARD_CLOSURE",
        "declared_start": "2026-01-01",
        "declared_end": "2026-11-06",
        "evidence": EVIDENCE,
        **extra,
    }


def _pin(tmp_path: Path, declarations: list[dict[str, Any]]) -> dict[str, Any]:
    raw = json.dumps({"schema_version": 1, "declarations": declarations}).encode()
    path = tmp_path / "decl.json"
    path.write_bytes(raw)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def test_a_consistent_declaration_makes_the_row_declared_complete(
    tmp_path: Path,
) -> None:
    decls = ed.load(_pin(tmp_path, [_decl()]))
    out = ed.apply([_row(), _row("a2")], decls, CUTOFF)
    assert out[0]["status"] == "DECLARED_COMPLETE"
    assert (out[0]["declared_start"], out[0]["declared_end"]) == (
        "2026-01-01",
        "2026-11-06",
    )
    assert out[0]["boundary_basis"] == "NAV_ROLLFORWARD_CLOSURE"
    assert out[0]["evidence"] == EVIDENCE
    assert out[1]["status"] == "OBSERVED_NOT_COMPLETENESS_PROOF"  # undeclared stays so


@pytest.mark.parametrize(
    "bad",
    [
        {"declared_end": "2026-11-05"},  # stops before the cutoff
        {"declared_start": "2026-02-01"},  # does not cover the first observed record
        {"declared_end": "2026-11-06", "declared_start": "2026-12-01"},  # reversed
    ],
)
def test_a_declaration_the_records_contradict_is_refused(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    with pytest.raises(ed.EventDeclarationError):
        ed.apply([_row()], ed.load(_pin(tmp_path, [_decl(**bad)])), CUTOFF)


def test_unavailable_amounts_never_become_complete(tmp_path: Path) -> None:
    decls = ed.load(_pin(tmp_path, [_decl()]))
    with pytest.raises(ed.EventDeclarationError, match="unavailable amounts"):
        ed.apply([_row(unavailable_amount_count=1)], decls, CUTOFF)


def test_structural_rows_are_labelled_never_complete_and_never_contradict_records(
    tmp_path: Path,
) -> None:
    decl = {
        "account_id": "a1",
        "fact_type": "flows",
        "status": "STRUCTURAL_NO_EVENT_SOURCE",
        "reason": "provider published no account activities",
    }
    decls = ed.load(_pin(tmp_path, [decl]))
    out = ed.apply(
        [_row(record_count=0, observed_start=None, observed_end=None)], decls, CUTOFF
    )
    assert out[0]["status"] == "STRUCTURAL_NO_EVENT_SOURCE"
    assert (
        out[0]["missing_interval_reason"] == "provider published no account activities"
    )
    with pytest.raises(ed.EventDeclarationError, match="contradicts"):
        ed.apply([_row()], decls, CUTOFF)


def test_pin_and_shape_are_enforced(tmp_path: Path) -> None:
    pin = _pin(tmp_path, [_decl()])
    with pytest.raises(ed.EventDeclarationError, match="differ from their pin"):
        ed.load({**pin, "sha256": "0" * 64})
    for bad in (
        _decl(method="WISHFUL"),
        _decl(evidence=[]),
        _decl(fact_type="nav"),
        _decl(status="MAYBE"),
    ):
        with pytest.raises(ed.EventDeclarationError):
            ed.load(_pin(tmp_path, [bad]))
    with pytest.raises(ed.EventDeclarationError, match="duplicated"):
        ed.load(_pin(tmp_path, [_decl(), _decl()]))
    with pytest.raises(ed.EventDeclarationError, match="outside the matrix"):
        ed.apply([_row("other")], ed.load(_pin(tmp_path, [_decl()])), CUTOFF)

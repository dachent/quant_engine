"""Mutation tests: a fact dropped between catalog and ledger fails the gate."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_fact_admission as gate  # noqa: E402
import provider_ledger as ledger  # noqa: E402

ACCOUNT = "account-ibkr-1"
ASSIGNMENTS = {
    ACCOUNT: [{"effective_start": "2026-01-01", "strategy_id": "p123-eu"}],
}


def _catalog() -> dict[str, list[dict[str, object]]]:
    catalog: dict[str, list[dict[str, object]]] = {
        fact: []
        for fact in ledger._ACCOUNT_FACTS["ibkr"]  # noqa: SLF001
    }
    catalog["transactions"] = [
        {
            "account_id": ACCOUNT,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
            "commission": {"currency": "USD", "status": "AVAILABLE", "value": "-1.05"},
            "event_date": "2026-01-05",
            "executed_time": "09:30:15",
            "kind": "TRADE:ExchTrade:BUY",
            "record_id": "ibkr-trade-1",
            "timing_granularity": "DATETIME",
        },
        {
            "account_id": ACCOUNT,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-5"},
            "event_date": "2026-01-05",
            "kind": "TRADE:ExchTrade:BUY",
            "record_id": "ibkr-trade-2",
            "timing_granularity": "DATE_ONLY",
        },
    ]
    return catalog


def _project(catalog: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    projected, _ = ledger._project_account_facts(  # noqa: SLF001
        "ibkr", "generation-ibkr", catalog, {ACCOUNT}, {"p123-eu"}, ASSIGNMENTS
    )
    return projected["transactions"]


def test_real_projection_passes_the_gate() -> None:
    catalog = _catalog()
    assert gate.dropped_facts(catalog["transactions"], _project(catalog)) == []


@pytest.mark.parametrize("field", ["commission", "executed_time", "timing_granularity"])
def test_a_dropped_field_fails_the_gate(field: str) -> None:
    catalog = _catalog()
    rows = copy.deepcopy(_project(catalog))
    for row in rows:
        row.pop(field, None)
    findings = gate.dropped_facts(catalog["transactions"], rows)
    assert f"ibkr-trade-1: {field} dropped" in findings


def test_a_changed_value_and_a_missing_row_fail_the_gate() -> None:
    catalog = _catalog()
    rows = copy.deepcopy(_project(catalog))
    rows[0]["commission"] = {"currency": "USD", "status": "AVAILABLE", "value": "0"}
    assert "ibkr-trade-1: commission changed" in gate.dropped_facts(
        catalog["transactions"], rows
    )
    assert "ibkr-trade-1: ledger row missing" in "; ".join(
        gate.dropped_facts(catalog["transactions"], [])
    )


def test_a_regressed_projection_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the regression itself: the projection stops copying commission."""

    catalog = _catalog()
    original = ledger._project_account_facts  # noqa: SLF001

    def regressed(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        projected, findings = original(*args, **kwargs)  # type: ignore[arg-type]
        for row in projected["transactions"]:
            row.pop("commission", None)
        return projected, findings

    monkeypatch.setattr(ledger, "_project_account_facts", regressed)
    assert gate.dropped_facts(catalog["transactions"], _project(catalog)) != []


def test_gate_fields_equal_the_projection_tuple() -> None:
    """The gate cannot drift from what provider_ledger actually copies."""

    tree = ast.parse((ROOT / "tools" / "provider_ledger.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Tuple)
            and {"commission", "timing_granularity"}
            <= {
                e.value
                for e in node.iter.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
        ):
            fields = tuple(
                e.value for e in node.iter.elts if isinstance(e, ast.Constant)
            )
            assert fields == gate.ADMITTED_FIELDS
            return
    raise AssertionError("projection field tuple not found in provider_ledger.py")


def test_cli_exit_codes(tmp_path: Path) -> None:
    catalog = _catalog()["transactions"]
    good = _project(_catalog())
    bad = copy.deepcopy(good)
    for row in bad:
        row.pop("commission", None)
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    args = ["--catalog", str(tmp_path / "catalog.json"), "--ledger"]
    assert gate.main([*args, str(tmp_path / "good.json")]) == 0
    assert gate.main([*args, str(tmp_path / "bad.json")]) == 1

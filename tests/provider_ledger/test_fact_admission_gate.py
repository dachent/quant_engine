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


def _workspace(tmp_path: Path, *, drop: str | None = None, late: bool = False) -> Path:
    catalog = _catalog()
    if late:
        catalog["transactions"].append(
            {
                "account_id": ACCOUNT,
                "commission": {"currency": "USD", "status": "AVAILABLE", "value": "-2"},
                "event_date": "2026-12-31",
                "kind": "TRADE:ExchTrade:BUY",
                "record_id": "ibkr-trade-late",
            }
        )
    projected = copy.deepcopy(_project(_catalog()))
    for row in projected:
        row["provider_id"] = "ibkr"
        if drop:
            row.pop(drop, None)
    root = tmp_path / "dataIBKR" / "generations" / "gen-1"
    root.mkdir(parents=True)
    (root / "ibkr-catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    ledger_file = tmp_path / "provider-ledger.json"
    ledger_file.write_text(
        json.dumps(
            {
                "as_of_date": "2026-09-17",
                "provider_generations": [
                    {"provider_id": "ibkr", "generation_id": "gen-1"},
                    {"provider_id": "schwab", "generation_id": "gen-2"},
                ],
                "quality_findings": [],
                "transactions": projected,
            }
        ),
        encoding="utf-8",
    )
    return ledger_file


def test_workspace_mode_passes_excludes_cutoff_rows_and_reports_skips(
    tmp_path: Path,
) -> None:
    ledger_file = _workspace(tmp_path, late=True)
    outcome = gate.check_ledger_generation(ledger_file, tmp_path)
    assert outcome["status"] == "PASS", outcome
    assert "1 excluded" in outcome["providers"]["ibkr"]
    # a provider whose catalog is not on disk is skipped, never passed
    assert outcome["providers"]["schwab"] == "SKIPPED_CATALOG_NOT_ON_DISK"
    assert gate.main(["--workspace", str(tmp_path), "--ledger", str(ledger_file)]) == 0


def test_workspace_mode_fails_on_a_dropped_fact(tmp_path: Path) -> None:
    ledger_file = _workspace(tmp_path, drop="commission")
    outcome = gate.check_ledger_generation(ledger_file, tmp_path)
    assert outcome["status"] == "FAIL"
    assert "ibkr: ibkr-trade-1: commission dropped" in outcome["findings"]
    assert gate.main(["--workspace", str(tmp_path), "--ledger", str(ledger_file)]) == 1


def test_workspace_with_no_checkable_provider_is_skipped_not_passed(
    tmp_path: Path,
) -> None:
    ledger_file = tmp_path / "provider-ledger.json"
    ledger_file.write_text(
        json.dumps({"as_of_date": "2026-09-17", "provider_generations": []}),
        encoding="utf-8",
    )
    assert gate.check_ledger_generation(ledger_file, tmp_path)["status"] == "SKIPPED"


def _preflight_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "portfolio_qc_fact_admission", ROOT / "tools" / "portfolio_qc.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _preflight_spec(tmp_path: Path, ledger_file: Path | None):
    from types import SimpleNamespace

    qc = _preflight_module()
    gates = ()
    if ledger_file is not None:
        gates = (SimpleNamespace(gate_id=qc.FACT_ADMISSION_GATE_ID, path=ledger_file),)
    coordination = tmp_path / "dataPortfolio" / "releases"
    coordination.mkdir(parents=True, exist_ok=True)
    return qc, SimpleNamespace(coordination_root=coordination, gates=gates)


def _only_ibkr(ledger_file: Path) -> None:
    value = json.loads(ledger_file.read_text(encoding="utf-8"))
    value["provider_generations"] = [
        row for row in value["provider_generations"] if row["provider_id"] == "ibkr"
    ]
    ledger_file.write_text(json.dumps(value), encoding="utf-8")


def test_release_preflight_runs_the_gate_on_the_candidate_ledger(
    tmp_path: Path,
) -> None:
    ledger_file = _workspace(tmp_path)
    _only_ibkr(ledger_file)
    qc, spec = _preflight_spec(tmp_path, ledger_file)
    assert qc._fact_admission(spec) == {  # noqa: SLF001
        "providers": {"ibkr": "PASS"},
        "status": "PASS",
    }


def test_release_preflight_fails_when_the_candidate_ledger_dropped_a_fact(
    tmp_path: Path,
) -> None:
    ledger_file = _workspace(tmp_path, drop="commission")
    _only_ibkr(ledger_file)
    qc, spec = _preflight_spec(tmp_path, ledger_file)
    with pytest.raises(qc.QualityControlFailure, match="commission dropped"):
        qc._fact_admission(spec)  # noqa: SLF001


def test_release_preflight_fails_when_a_bound_provider_was_not_checked(
    tmp_path: Path,
) -> None:
    # ibkr is checked and passes, but the bound schwab catalog is not on disk.
    ledger_file = _workspace(tmp_path)
    qc, spec = _preflight_spec(tmp_path, ledger_file)
    with pytest.raises(qc.QualityControlFailure, match="schwab"):
        qc._fact_admission(spec)  # noqa: SLF001


def test_release_preflight_fails_when_nothing_is_checkable(tmp_path: Path) -> None:
    ledger_file = tmp_path / "provider-ledger.json"
    ledger_file.write_text(
        json.dumps({"as_of_date": "2026-09-17", "provider_generations": []}),
        encoding="utf-8",
    )
    qc, spec = _preflight_spec(tmp_path, ledger_file)
    with pytest.raises(qc.QualityControlFailure):
        qc._fact_admission(spec)  # noqa: SLF001


def test_release_preflight_without_the_gate_is_not_applicable(tmp_path: Path) -> None:
    qc, spec = _preflight_spec(tmp_path, None)
    assert qc._fact_admission(spec) == {"status": "NOT_APPLICABLE"}  # noqa: SLF001

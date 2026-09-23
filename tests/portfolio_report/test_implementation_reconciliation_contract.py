from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_report.py"
SPEC = importlib.util.spec_from_file_location("portfolio_report_impl_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio report builder")
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _contract(path: Path) -> None:
    strategies = []
    for strategy_id in (
        "grizzlybears-vtma",
        "p123-ai-factor",
        "tt-portfolio",
    ):
        strategies.append(
            {
                "return_reconciliation": {
                    "observations": 2,
                    "observed_end": "2026-08-02",
                    "observed_start": "2026-08-01",
                },
                "root_causes": [
                    {"cause": "SYNTHETIC", "evidence": "test", "status": "CONFIRMED"}
                ],
                "strategy_id": strategy_id,
                "verification": "VERIFIED",
            }
        )
    path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-08-03",
                "builder": "portfolio-implementation-reconciliation",
                "reconciliation_id": "a" * 64,
                "schema_version": 1,
                "strategies": strategies,
                "verification": "VERIFIED",
            }
        ),
        encoding="utf-8",
    )


def _payload(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError("synthetic contract must be an object")
    return loaded


def _strategy(payload: dict[str, object], strategy_id: str) -> dict[str, object]:
    strategies = payload["strategies"]
    if not isinstance(strategies, list):
        raise AssertionError("synthetic strategies must be a list")
    return next(
        row
        for row in strategies
        if isinstance(row, dict) and row.get("strategy_id") == strategy_id
    )


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_implementation_reconciliation_contract_accepts_exact_three_strategy_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)

    rows, identity = reporter._implementation_reconciliation_contract(
        path, portfolio_as_of="2026-08-03"
    )

    assert set(rows) == {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "tt-portfolio",
    }
    assert identity["strategy_count"] == 3
    assert identity["reconciliation_id"] == "a" * 64


def test_implementation_reconciliation_contract_rejects_future_artifact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)

    with pytest.raises(reporter.ReportValidationError, match="future-dated"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-02"
        )


def test_implementation_reconciliation_contract_rejects_future_observed_end(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)
    payload = _payload(path)
    metrics = _strategy(payload, "p123-ai-factor")["return_reconciliation"]
    assert isinstance(metrics, dict)
    metrics["observed_end"] = "2026-08-04"
    _write_payload(path, payload)

    with pytest.raises(reporter.ReportValidationError, match="future-dated"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_implementation_reconciliation_contract_rejects_future_observed_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)
    payload = _payload(path)
    metrics = _strategy(payload, "p123-ai-factor")["return_reconciliation"]
    assert isinstance(metrics, dict)
    metrics["observed_start"] = "2026-08-04"
    metrics["observed_end"] = "2026-08-04"
    _write_payload(path, payload)

    with pytest.raises(reporter.ReportValidationError, match="future-dated"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_implementation_reconciliation_contract_rejects_nested_future_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)
    payload = _payload(path)
    strategy = _strategy(payload, "grizzlybears-vtma")
    strategy["holding_state_reconciliation"] = {
        "observed_end": "2026-08-04",
        "observed_start": "2026-08-01",
    }
    _write_payload(path, payload)

    with pytest.raises(reporter.ReportValidationError, match="future-dated"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_implementation_reconciliation_contract_rejects_reversed_nested_interval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)
    payload = _payload(path)
    strategy = _strategy(payload, "tt-portfolio")
    strategy["return_window_decomposition"] = {
        "from_complete_allocation_history": {
            "observed_end": "2026-08-01",
            "observed_start": "2026-08-02",
        }
    }
    _write_payload(path, payload)

    with pytest.raises(reporter.ReportValidationError, match="reversed"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_implementation_reconciliation_contract_uses_stricter_reconciliation_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implementation.json"
    _contract(path)
    payload = _payload(path)
    payload["as_of_date"] = "2026-08-02"
    metrics = _strategy(payload, "p123-ai-factor")["return_reconciliation"]
    assert isinstance(metrics, dict)
    metrics["observed_end"] = "2026-08-03"
    _write_payload(path, payload)

    with pytest.raises(reporter.ReportValidationError, match="future-dated"):
        reporter._implementation_reconciliation_contract(
            path, portfolio_as_of="2026-08-03"
        )

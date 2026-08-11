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

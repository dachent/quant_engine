from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_report.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report_return_attribution_test", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio report builder")
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _contract(path: Path) -> None:
    strategies = []
    verifications = {
        "grizzlybears-vtma": "VERIFIED_WITH_DECLARED_PROXY",
        "p123-ai-factor": "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
        "tt-portfolio": "VERIFIED_WITH_DECLARED_PUBLIC_PRICE_PROXY",
    }
    for strategy_id, verification in verifications.items():
        strategies.append(
            {
                "bridge": {
                    "account_return": 0.08,
                    "categories": [
                        {
                            "category": "DELAYED_TRADES",
                            "effect_pp": -2.0,
                            "status": "EXACT_DATES_PROXY",
                        }
                    ],
                    "gap_pp": -2.0,
                    "model_return": 0.10,
                    "status": "ORDERED_COUNTERFACTUAL_WITH_EXPLICIT_RESIDUAL",
                },
                "observed_end": "2026-08-02",
                "observed_start": "2026-08-01",
                "strategy_id": strategy_id,
                "verification": verification,
            }
        )
    path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-08-09",
                "attribution_id": "b" * 64,
                "builder": "portfolio-implementation-return-attribution",
                "schema_version": 1,
                "strategies": strategies,
                "verification": "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
            }
        ),
        encoding="utf-8",
    )


def test_return_attribution_contract_accepts_closed_three_strategy_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)

    rows, identity = reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-08-03"
    )

    assert set(rows) == {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "tt-portfolio",
    }
    assert identity["strategy_count"] == 3
    assert identity["attribution_id"] == "b" * 64


def test_return_attribution_contract_rejects_open_bridge(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["strategies"][0]["bridge"]["categories"][0]["effect_pp"] = -1.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="does not reconcile"):
        reporter._implementation_return_attribution_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_return_attribution_contract_rejects_future_evidence(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["strategies"][0]["observed_end"] = "2026-08-04"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="boundary differs"):
        reporter._implementation_return_attribution_contract(
            path, portfolio_as_of="2026-08-03"
        )


def test_return_attribution_contract_rejects_non_numeric_effect(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["strategies"][0]["bridge"]["categories"][0]["effect_pp"] = "-2.0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="finite number"):
        reporter._implementation_return_attribution_contract(
            path, portfolio_as_of="2026-08-03"
        )

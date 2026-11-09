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


_UNAVAILABLE = "UNAVAILABLE_UNDATED_CURRENT_SNAPSHOT"


def _unavailable_ai_bridge(
    *, account_return: float = 0.05, model_return: float = 0.07
) -> dict[str, object]:
    """The producer's own unavailable state: three model-dependent effects are null."""

    unexplained = -0.5
    cash = -0.25
    gap_pp = (account_return - model_return) * 100.0
    assert abs(gap_pp - (unexplained + cash)) > 1e-6  # the nulls carry the remainder
    return {
        "account_return": account_return,
        "categories": [
            {
                "category": "PRIMARY_ONLY_BENCHMARK_TO_EFFECTIVE_ACCOUNT_MODEL_MIX",
                "effect_pp": None,
                "status": _UNAVAILABLE,
            },
            {
                "category": "PRIMARY_AI_SLEEVE_IMPLEMENTATION",
                "effect_pp": None,
                "status": _UNAVAILABLE,
            },
            {
                "category": "OTHER_CONNECTED_SLEEVES_IMPLEMENTATION",
                "effect_pp": None,
                "status": _UNAVAILABLE,
            },
            {
                "category": "UNEXPLAINED_ACTUAL_ACTIVITY",
                "effect_pp": unexplained,
                "status": "EXACT_RETURN_LINK_NO_ADMITTED_MODEL_EVENT_CANDIDATE",
            },
            {
                "category": "CASH_FINANCING_FEES_AND_POSITION_RECONCILIATION",
                "effect_pp": cash,
                "status": "EXACT_RETURN_LINK_NON_SECURITY_AND_OPENING_POSITION_EFFECT",
            },
        ],
        "effective_account_model_mix_gap_pp": None,
        "effective_account_model_mix_return": None,
        "gap_pp": gap_pp,
        "model_return": model_return,
        "status": _UNAVAILABLE,
    }


def _write_with_unavailable_ai(path: Path) -> dict[str, object]:
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    ai = next(
        row for row in payload["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    ai["bridge"] = _unavailable_ai_bridge()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ai


def _build(path: Path) -> None:
    reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-08-03"
    )


def test_unavailable_bridge_with_null_model_dependent_effects_is_accepted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _write_with_unavailable_ai(path)

    rows, _identity = reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-08-03"
    )

    bridge = rows["p123-ai-factor"]["bridge"]
    assert bridge["status"] == _UNAVAILABLE
    assert [
        item["effect_pp"]
        for item in bridge["categories"]
        if item["status"] == _UNAVAILABLE
    ] == [None, None, None]


def test_unavailable_bridge_still_refuses_a_gap_that_is_not_account_minus_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    ai = _write_with_unavailable_ai(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(
        r for r in payload["strategies"] if r["strategy_id"] == ai["strategy_id"]
    )
    row["bridge"]["gap_pp"] = -1.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="does not reconcile"):
        _build(path)


@pytest.mark.parametrize(
    "category",
    (
        "UNEXPLAINED_ACTUAL_ACTIVITY",
        "CASH_FINANCING_FEES_AND_POSITION_RECONCILIATION",
    ),
)
def test_unavailable_bridge_refuses_a_null_on_an_exact_category(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write_with_unavailable_ai(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(r for r in payload["strategies"] if r["strategy_id"] == "p123-ai-factor")
    next(c for c in row["bridge"]["categories"] if c["category"] == category)[
        "effect_pp"
    ] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="finite number"):
        _build(path)


@pytest.mark.parametrize(
    "category",
    (
        "PRIMARY_ONLY_BENCHMARK_TO_EFFECTIVE_ACCOUNT_MODEL_MIX",
        "PRIMARY_AI_SLEEVE_IMPLEMENTATION",
        "OTHER_CONNECTED_SLEEVES_IMPLEMENTATION",
    ),
)
def test_unavailable_bridge_refuses_a_value_on_an_unavailable_effect(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write_with_unavailable_ai(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(r for r in payload["strategies"] if r["strategy_id"] == "p123-ai-factor")
    next(c for c in row["bridge"]["categories"] if c["category"] == category)[
        "effect_pp"
    ] = 0.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="carries a value"):
        _build(path)


def test_unavailable_bridge_refuses_a_missing_model_dependent_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _write_with_unavailable_ai(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(r for r in payload["strategies"] if r["strategy_id"] == "p123-ai-factor")
    row["bridge"]["categories"] = [
        c
        for c in row["bridge"]["categories"]
        if c["category"] != "PRIMARY_AI_SLEEVE_IMPLEMENTATION"
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="lacks the"):
        _build(path)


def test_an_available_bridge_still_requires_every_effect_finite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["strategies"][0]["bridge"]["categories"][0]["effect_pp"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="finite number"):
        _build(path)

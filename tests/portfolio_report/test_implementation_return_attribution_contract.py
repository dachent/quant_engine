from __future__ import annotations

import importlib.util
from collections.abc import Callable
import json
from pathlib import Path
import sys
from typing import Any

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
_BENCHMARK = "PRIMARY_ONLY_BENCHMARK_TO_EFFECTIVE_ACCOUNT_MODEL_MIX"
_PRIMARY = "PRIMARY_AI_SLEEVE_IMPLEMENTATION"
_OTHER = "OTHER_CONNECTED_SLEEVES_IMPLEMENTATION"
_UNEXPLAINED = "UNEXPLAINED_ACTUAL_ACTIVITY"
_CASH = "CASH_FINANCING_FEES_AND_POSITION_RECONCILIATION"


def _unavailable_ai_row(row: dict[str, Any]) -> dict[str, Any]:
    """The producer's unavailable shape for the AI path, with synthetic numbers.

    Benchmark and other-connected effects are explicit nulls, the primary effect is finite,
    the two exact links equal the trade-provenance contributions that make them up, and
    the contributions sum to the account return.
    """

    account_return, model_return = 0.05, 0.07
    row["bridge"] = {
        "account_basis": "BASELINE_ACCOUNT_LIVE",
        "account_return": account_return,
        "categories": [
            {"category": _BENCHMARK, "effect_pp": None, "status": _UNAVAILABLE},
            {
                "category": _PRIMARY,
                "effect_pp": 2.0,
                "status": "PRIMARY_ONLY_CANDIDATE_ACTUAL_PNL_MINUS_INFERRED_PRIMARY_TARGET_PNL",
            },
            {"category": _OTHER, "effect_pp": None, "status": _UNAVAILABLE},
            {
                "category": _UNEXPLAINED,
                "effect_pp": -1.0,
                "status": "EXACT_RETURN_LINK_NO_ADMITTED_MODEL_EVENT_CANDIDATE",
            },
            {
                "category": _CASH,
                "effect_pp": 0.0,
                "status": "EXACT_RETURN_LINK_NON_SECURITY_AND_OPENING_POSITION_EFFECT",
            },
        ],
        "effective_account_model_mix_gap_pp": None,
        "effective_account_model_mix_return": None,
        "gap_pp": (account_return - model_return) * 100.0,
        "model_basis": "EXACT_PROVIDER_LIVE_RETURNS",
        "model_return": model_return,
        "status": _UNAVAILABLE,
    }
    row["effective_account_model_mix"] = {
        "model_mix_return": None,
        "status": _UNAVAILABLE,
        "unavailable_roles": ["TEMP_LONG_SHORT"],
    }
    row["actual_return_contribution_by_trade_provenance"] = {
        "contributions_pp": {
            "AMBIGUOUS_PRIMARY_AND_TEMPORARY": 1.5,
            "CASH_FINANCING_FEES_TAXES_AND_UNCLASSIFIED": 0.25,
            "NON_AI_TEMPORARY_ONLY": 0.5,
            "OPENING_OR_POSITION_RECONCILIATION": -0.25,
            "PRIMARY_ONLY": 4.0,
            "UNEXPLAINED_ACTUAL_ACTIVITY": -1.0,
        },
        "daily_observations": 10,
        "status": "EXACT_DAILY_RETURN_LINK_CANDIDATE_LOT_PROVENANCE_NOT_BROKER_TAGGED",
    }
    return row


def _write(path: Path, *mutations: Callable[[dict[str, Any]], object]) -> None:
    """Write the three-strategy contract with an unavailable AI row, then mutate it."""

    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    ai = next(r for r in payload["strategies"] if r["strategy_id"] == "p123-ai-factor")
    _unavailable_ai_row(ai)
    for mutation in mutations:
        mutation(ai)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _category(row: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in row["bridge"]["categories"] if c["category"] == name)


def _build(path: Path) -> None:
    reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-08-03"
    )


def test_the_producers_unavailable_bridge_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path)

    rows, _identity = reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-08-03"
    )

    bridge = rows["p123-ai-factor"]["bridge"]
    assert bridge["status"] == _UNAVAILABLE
    effects = {c["category"]: c["effect_pp"] for c in bridge["categories"]}
    assert effects[_BENCHMARK] is None and effects[_OTHER] is None
    assert effects[_PRIMARY] == 2.0


def test_the_primary_effect_may_itself_be_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _write(
        path,
        lambda row: _category(row, _PRIMARY).update(
            {"effect_pp": None, "status": _UNAVAILABLE}
        ),
    )

    _build(path)


def test_an_unavailable_bridge_still_refuses_a_gap_that_is_not_account_minus_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: row["bridge"].update({"gap_pp": -1.0}))

    with pytest.raises(reporter.ReportValidationError, match="does not reconcile"):
        _build(path)


@pytest.mark.parametrize("category", (_UNEXPLAINED, _CASH))
def test_an_unavailable_bridge_refuses_a_null_on_an_exact_link(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: _category(row, category).update({"effect_pp": None}))

    with pytest.raises(reporter.ReportValidationError, match="finite number"):
        _build(path)


@pytest.mark.parametrize("category", (_BENCHMARK, _OTHER))
def test_an_unavailable_bridge_refuses_a_value_on_an_unavailable_effect(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: _category(row, category).update({"effect_pp": 0.0}))

    with pytest.raises(reporter.ReportValidationError, match="carries a value"):
        _build(path)


@pytest.mark.parametrize("category", (_BENCHMARK, _OTHER, _PRIMARY))
def test_an_unavailable_bridge_refuses_an_omitted_effect_key(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: _category(row, category).pop("effect_pp"))

    with pytest.raises(reporter.ReportValidationError, match="omits an effect"):
        _build(path)


def test_an_unavailable_bridge_refuses_a_missing_category(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"

    def drop(row: dict[str, Any]) -> None:
        row["bridge"]["categories"] = [
            c for c in row["bridge"]["categories"] if c["category"] != _PRIMARY
        ]

    _write(path, drop)

    with pytest.raises(reporter.ReportValidationError, match="categories differ"):
        _build(path)


def test_the_unavailable_waiver_is_only_for_the_ai_path(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"
    _contract(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    tt = next(r for r in payload["strategies"] if r["strategy_id"] == "tt-portfolio")
    _unavailable_ai_row(tt)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(reporter.ReportValidationError, match="not declared"):
        _build(path)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda row: row["effective_account_model_mix"].update(
            {
                "status": "EFFECTIVE_MODEL_MIX_INFERRED_FROM_UNIQUE_BOUNDED_TRADE_QUANTITIES"
            }
        ),
        lambda row: row["effective_account_model_mix"].update(
            {"model_mix_return": 0.01}
        ),
        lambda row: row["bridge"].update({"effective_account_model_mix_return": 0.01}),
        lambda row: row["bridge"].update({"effective_account_model_mix_gap_pp": 0.01}),
        lambda row: row.pop("effective_account_model_mix"),
        lambda row: row["bridge"].update({"residual_pp": 0.5}),
    ),
    ids=(
        "forged-mix-status",
        "mix-return-present",
        "bridge-mix-present",
        "bridge-mix-gap-present",
        "no-mix",
        "added-residual-field",
    ),
)
def test_a_forged_unavailable_status_cannot_skip_closure_alone(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], object]
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, mutation)

    with pytest.raises(reporter.ReportValidationError):
        _build(path)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda row: _category(row, _UNEXPLAINED).update({"effect_pp": -0.9}),
        lambda row: _category(row, _CASH).update({"effect_pp": 0.1}),
        lambda row: row["actual_return_contribution_by_trade_provenance"][
            "contributions_pp"
        ].update({"PRIMARY_ONLY": 4.5}),
    ),
    ids=("unexplained-differs", "cash-differs", "contributions-do-not-sum"),
)
def test_the_exact_links_must_match_the_trade_provenance_contributions(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], object]
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, mutation)

    with pytest.raises(reporter.ReportValidationError, match="exact return links"):
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


def test_an_unavailable_bridge_refuses_an_unknown_field_on_a_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: _category(row, _UNEXPLAINED).update({"plug_pp": 0.1}))

    with pytest.raises(reporter.ReportValidationError, match="unknown category field"):
        _build(path)


def test_an_unavailable_bridge_refuses_an_added_category(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"

    def add(row: dict[str, Any]) -> None:
        row["bridge"]["categories"].append(
            {"category": "RESIDUAL", "effect_pp": 0.5, "status": "ANYTHING"}
        )

    _write(path, add)

    with pytest.raises(reporter.ReportValidationError, match="categories differ"):
        _build(path)


def test_an_unavailable_bridge_refuses_a_duplicated_category(tmp_path: Path) -> None:
    path = tmp_path / "return-attribution.json"

    def duplicate(row: dict[str, Any]) -> None:
        row["bridge"]["categories"].append(dict(_category(row, _UNEXPLAINED)))

    _write(path, duplicate)

    with pytest.raises(reporter.ReportValidationError, match="categories differ"):
        _build(path)


@pytest.mark.parametrize("category", (_UNEXPLAINED, _CASH))
def test_an_unavailable_bridge_refuses_a_deleted_exact_category(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "return-attribution.json"

    def delete(row: dict[str, Any]) -> None:
        row["bridge"]["categories"] = [
            c for c in row["bridge"]["categories"] if c["category"] != category
        ]

    _write(path, delete)

    with pytest.raises(reporter.ReportValidationError, match="categories differ"):
        _build(path)


@pytest.mark.parametrize(
    "category, status",
    (
        (_BENCHMARK, "CORRECTS_MULTI_MODEL_SHARED_ACCOUNT_BENCHMARK"),
        (_PRIMARY, _UNAVAILABLE),
        (_UNEXPLAINED, _UNAVAILABLE),
        (_CASH, "SOMETHING_ELSE"),
    ),
)
def test_an_unavailable_bridge_refuses_a_category_status_that_contradicts_its_effect(
    tmp_path: Path, category: str, status: str
) -> None:
    path = tmp_path / "return-attribution.json"
    _write(path, lambda row: _category(row, category).update({"status": status}))

    with pytest.raises(reporter.ReportValidationError, match="status differs"):
        _build(path)


def test_the_unavailable_bridge_is_a_partial_status_never_the_verified_closure() -> (
    None
):
    attribution = {"bridge": {"status": _UNAVAILABLE}}
    verified = {"bridge": {"status": "ORDERED_COUNTERFACTUAL_WITH_EXPLICIT_RESIDUAL"}}

    assert (
        reporter._implementation_status("HISTORY", {"x": 1}, attribution)
        == "PARTIAL_CAUSAL_ATTRIBUTION_MODEL_MIX_UNAVAILABLE"
    )
    assert (
        reporter._implementation_status("HISTORY", {"x": 1}, verified)
        == "VERIFIED_CAUSAL_RETURN_ATTRIBUTION"
    )

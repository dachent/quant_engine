"""The orthogonal risk-share block: closure, refusal, aggregate and the real schema."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

import portfolio_orthogonal_risk as risk

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPOSITORY_ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json"

START = date(2026, 2, 2)
DAYS = 8
MODE_A = [
    "0.001",
    "-0.002",
    "0.003",
    "0.000",
    "-0.001",
    "0.002",
    "0.004",
    "-0.003",
]
MODE_B = [
    "0.002",
    "0.001",
    "-0.001",
    "0.003",
    "0.000",
    "-0.002",
    "0.001",
    "0.002",
]
NOISE = [
    "0.0005",
    "-0.0004",
    "0.0002",
    "0.0001",
    "-0.0003",
    "0.0006",
    "-0.0002",
    "0.0003",
]
INTERCEPT = [
    "0.0001",
    "0.0002",
    "-0.0001",
    "0.0000",
    "0.0003",
    "-0.0002",
    "0.0001",
    "0.0000",
]


def _day(index: int) -> str:
    return (START + timedelta(days=index)).isoformat()


def _cov(first: list[Fraction], second: list[Fraction]) -> Fraction:
    mean_first = sum(first) / len(first)
    mean_second = sum(second) / len(second)
    return sum((a - mean_first) * (b - mean_second) for a, b in zip(first, second)) / (
        len(first) - 1
    )


def _text(value: Fraction, digits: int = 15) -> str:
    with localcontext() as context:
        context.prec = digits
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _root(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 30
        return str((Decimal(value.numerator) / Decimal(value.denominator)).sqrt())


def _series_row(
    series_id: str,
    *,
    source_key: str = "src",
    dynamic: bool = False,
    response_extra: list[str] | None = None,
    scale: int = 1,
    offset: int = 0,
) -> dict[str, Any]:
    """A lens series row whose published figures are computed here, independently."""

    mode_a = [Fraction(v) * scale for v in MODE_A][offset:]
    mode_b = [Fraction(v) for v in MODE_B][offset:]
    intercept = [Fraction(v) for v in INTERCEPT][offset:]
    components = {"symmetric_mode:AAA": mode_a, "symmetric_mode:BBB": mode_b}
    if dynamic:
        components["orthogonal_intercept"] = intercept
    count = DAYS - offset
    fitted = [sum(c[i] for c in components.values()) for i in range(count)]
    extra = [Fraction(v) for v in (response_extra or ["0"] * DAYS)][offset:]
    response = [f + Fraction(n) + e for f, n, e in zip(fitted, NOISE[offset:], extra)]
    residual = [y - f for y, f in zip(response, fitted)]
    mean_y = sum(response) / count
    mean_e = sum(residual) / count
    var_y_pop = sum((y - mean_y) ** 2 for y in response) / count
    var_e_pop = sum((e - mean_e) ** 2 for e in residual) / count
    euler = {key: _cov(series, fitted) for key, series in components.items()}
    per_date = []
    for i in range(count):
        contributions = {
            key: _text(series[i])
            for key, series in components.items()
            if key != "orthogonal_intercept"
        }
        per_date.append(
            {
                "date": _day(offset + i),
                "fitted_excess_return": _text(fitted[i]),
                "mode_contributions": contributions,
                "partition": "TRAIN",
            }
        )
    return {
        "series_id": series_id,
        "source_key": source_key,
        "source_label": "Source",
        "surface": "DYNAMIC_FLS" if dynamic else "STATIC",
        "status": "AVAILABLE",
        "model_id": f"model-{series_id}",
        "evidence_layer": "ACCOUNT_LIVE",
        "cost_coverage": "INCOMPLETE",
        "alpha": {
            "net_annualized": "0.10",
            "annualized_standard_error": "0.05",
            "confidence_status": "AVAILABLE_HAC_DESCRIPTIVE",
        },
        "coverage": {"test_observations": 2},
        "model_quality": {"decision_status": "PM_VALID"},
        "series_composition": {
            "status": "AVAILABLE_LIVE_ONLY",
            "calculation_observations_used": 10,
            "backtest_observations_used": 0,
            "live_observations": 10,
        },
        "interval_association": {
            "policy_id": "POLICY",
            "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
            "periods_per_year": 252,
            "buckets": [
                {
                    "partition": "TRAIN",
                    "factor_anchor": _day(offset + i),
                    "anchor_adjusted_target_return": _text(response[i], 30),
                }
                for i in range(count)
            ],
        },
        "diagnostics": {
            "train": {
                "observation_count": count,
                "r_squared": _text(1 - var_e_pop / var_y_pop),
                "adjusted_r_squared": "0.5",
            },
            "holdout": {"predicted_r_squared": "0.1", "observation_count": 2},
            "residuals": {
                "train": {"standard_deviation": _root(var_e_pop)},
            },
        },
        "orthogonal_attribution": {
            "status": "AVAILABLE",
            "method_id": "METHOD",
            "per_date": per_date,
            "train_fitted_risk_budget": {
                "euler_allocations": {k: _text(v) for k, v in euler.items()},
                "fitted_variance": _text(_cov(fitted, fitted)),
                "cross_mode_covariance_present": dynamic,
                "intercept_time_varying": dynamic,
            },
        },
    }


def _unavailable_row(series_id: str, source_key: str = "src") -> dict[str, Any]:
    return {
        "series_id": series_id,
        "source_key": source_key,
        "source_label": "Source",
        "surface": "STATIC",
        "status": "UNAVAILABLE",
        "unavailable_reasons": ["NOT_APPLICABLE_METHOD_SCOPE"],
    }


def _panel(rows: list[dict[str, Any]], capital_from: int = 0) -> dict[str, Any]:
    names = {row["series_id"] for row in rows}
    strategies = [
        {"strategy_id": f"id-{name}", "strategy_name": name} for name in sorted(names)
    ]
    capital = []
    for index, name in enumerate(sorted(names)):
        for i in range(capital_from, DAYS + 2):
            capital.append(
                {
                    "date": _day(i - 1),
                    "strategy_id": f"id-{name}",
                    "invested_capital_usd": str(1000 * (index + 1)),
                }
            )
    surfaces: dict[str, Any] = {}
    for row in rows:
        surfaces.setdefault(row["surface"], {"series": []})["series"].append(row)
    lens = {
        "lens_id": "ECONOMIC_FACTOR",
        "lens_label": "Economic factors",
        "as_of_date": "2026-03-01",
        "return_basis": "EXCESS",
        "surfaces": surfaces,
    }
    return {
        "strategies": strategies,
        "invested_capital": {"daily_by_strategy": capital},
        "decomposition_lenses": {
            "modes": {"LIVE_ONLY": [lens], "LIVE_PLUS_BACKTEST": []}
        },
    }


def _errors(block: dict[str, Any]) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    wrapper = {"$ref": "#/$defs/orthogonal_risk_attribution", "$defs": schema["$defs"]}
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message[:120]}"
        for e in Draft202012Validator(
            wrapper, format_checker=FormatChecker()
        ).iter_errors(block)
    ]


def _cell(
    block: dict[str, Any], series_id: str, surface: str = "STATIC"
) -> dict[str, Any]:
    for cell in block["modes"]["LIVE_ONLY"]["cells"]:
        if cell["series_id"] == series_id and cell["surface"] == surface:
            return cell
    raise AssertionError(series_id)


def test_shares_close_to_one_and_match_an_independent_euler() -> None:
    panel = _panel([_series_row("A"), _series_row("B", scale=3)])
    block = risk.build_orthogonal_risk_attribution(panel)
    assert _errors(block) == []
    for name in ("A", "B"):
        cell = _cell(block, name)
        assert cell["status"] == "AVAILABLE"
        shares = sum(Fraction(v) for v in cell["components"]["share_of_total"])
        closing = (
            shares
            + Fraction(cell["closure"]["residual_share"])
            + Fraction(cell["closure"]["cross_term_share"])
        )
        assert abs(closing - 1) < Fraction(1, 10**11)
        assert abs(Fraction(cell["closure"]["explained_share"]) - shares) < Fraction(
            1, 10**11
        )
    row = panel["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]["surfaces"]["STATIC"][
        "series"
    ][0]
    published = row["orthogonal_attribution"]["train_fitted_risk_budget"]
    fitted = Fraction(published["fitted_variance"])
    cell = _cell(block, "A")
    by_id = dict(
        zip(cell["components"]["ids"], cell["components"]["share_of_explained"])
    )
    for key, value in published["euler_allocations"].items():
        expected = Fraction(value) / fitted
        assert abs(Fraction(by_id[key]) - expected) < Fraction(1, 10**11)
    assert cell["fit"]["alpha"]["t"] == 2.0
    assert cell["window"]["n"] == DAYS
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_a_cross_term_between_fitted_and_residual_is_published_not_folded() -> None:
    extra = [
        "0.0002",
        "-0.0001",
        "0.0003",
        "0.0000",
        "0.0001",
        "-0.0002",
        "0.0004",
        "-0.0003",
    ]
    panel = _panel([_series_row("A", response_extra=extra), _series_row("B")])
    block = risk.build_orthogonal_risk_attribution(panel)
    cell = _cell(block, "A")
    assert Fraction(cell["closure"]["cross_term_share"]) != 0
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_a_dynamic_row_carries_the_intercept_as_its_own_component() -> None:
    panel = _panel([_series_row("A", dynamic=True), _series_row("B", dynamic=True)])
    block = risk.build_orthogonal_risk_attribution(panel)
    cell = _cell(block, "A", "DYNAMIC_FLS")
    assert "orthogonal_intercept" in cell["components"]["ids"]
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_a_published_euler_that_disagrees_is_refused_not_repaired() -> None:
    panel = _panel([_series_row("A"), _series_row("B")])
    row = panel["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]["surfaces"]["STATIC"][
        "series"
    ][0]
    row["orthogonal_attribution"]["train_fitted_risk_budget"]["euler_allocations"][
        "symmetric_mode:AAA"
    ] = "0.5"
    block = risk.build_orthogonal_risk_attribution(panel)
    cell = _cell(block, "A")
    assert cell["status"] == "UNAVAILABLE"
    assert cell["reason"] == "PUBLISHED_EULER_DIFFERS_FROM_DATED_CONTRIBUTIONS"
    assert _errors(block) == []


def test_a_row_that_does_not_apply_is_unavailable_with_its_reason() -> None:
    panel = _panel([_series_row("A"), _series_row("B"), _unavailable_row("C")])
    block = risk.build_orthogonal_risk_attribution(panel)
    cell = _cell(block, "C")
    assert (cell["status"], cell["reason"]) == (
        "UNAVAILABLE",
        "NOT_APPLICABLE_METHOD_SCOPE",
    )
    aggregate = block["modes"]["LIVE_ONLY"]["aggregates"][0]
    assert {"series_id": "C", "reason": "NOT_APPLICABLE_METHOD_SCOPE"} in aggregate[
        "excluded_series"
    ]
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_the_aggregate_is_capital_weighted_where_capital_exists_and_equal_weight_before() -> (
    None
):
    panel = _panel([_series_row("A"), _series_row("B", scale=3)], capital_from=4)
    block = risk.build_orthogonal_risk_attribution(panel)
    aggregate = block["modes"]["LIVE_ONLY"]["aggregates"][0]
    assert aggregate["status"] == "AVAILABLE"
    bases = [segment["basis"] for segment in aggregate["weighting"]]
    assert bases == ["EQUAL_WEIGHT_DIAGNOSTIC", "CAPITAL_WEIGHTED"]
    assert aggregate["weighting"][0]["start"] == _day(0)
    assert (
        sum(segment["n"] for segment in aggregate["weighting"])
        == aggregate["window"]["n"]
    )
    assert aggregate["unweighted_ranges"] == []
    assert Fraction(aggregate["capital_coverage"]) == 1
    closing = (
        sum(Fraction(v) for v in aggregate["components"]["share_of_total"])
        + Fraction(aggregate["closure"]["residual_share"])
        + Fraction(aggregate["closure"]["cross_term_share"])
    )
    assert abs(closing - 1) < Fraction(1, 10**11)
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_without_any_capital_the_aggregate_is_equal_weight_with_no_coverage_figure() -> (
    None
):
    late = _panel([_series_row("A"), _series_row("B")], capital_from=DAYS + 1)
    block = risk.build_orthogonal_risk_attribution(late)
    aggregate = block["modes"]["LIVE_ONLY"]["aggregates"][0]
    assert [s["basis"] for s in aggregate["weighting"]] == ["EQUAL_WEIGHT_DIAGNOSTIC"]
    assert aggregate["capital_coverage"] is None
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, late)


def test_one_covered_strategy_and_one_funded_strategy_stay_unavailable_or_named() -> (
    None
):
    one = risk.build_orthogonal_risk_attribution(_panel([_series_row("A")]))
    assert one["modes"]["LIVE_ONLY"]["aggregates"][0]["reason"] == (
        "FEWER_THAN_TWO_COVERED_STRATEGIES"
    )
    panel = _panel([_series_row("A"), _series_row("B")])
    funded = panel["invested_capital"]["daily_by_strategy"]
    for row in funded:
        if row["strategy_id"] == "id-B":
            row["invested_capital_usd"] = "0"
    block = risk.build_orthogonal_risk_attribution(panel)
    aggregate = block["modes"]["LIVE_ONLY"]["aggregates"][0]
    assert aggregate["status"] == "UNAVAILABLE"
    assert aggregate["unweighted_ranges"][0]["reason"].startswith(
        "FEWER_THAN_TWO_COVERED_STRATEGIES_HELD_INVESTED_CAPITAL"
    )
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)


def test_a_payload_without_lens_rows_is_an_explicit_unavailable_block() -> None:
    block = risk.build_orthogonal_risk_attribution({})
    assert block["status"] == "UNAVAILABLE"
    assert block["reason"] == "NO_ATTRIBUTABLE_ROW_IN_ANY_MODE"
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, {})


def test_the_verifier_rejects_a_tampered_share_a_dropped_cell_and_a_wrong_version() -> (
    None
):
    panel = _panel([_series_row("A"), _series_row("B")])
    good = risk.build_orthogonal_risk_attribution(panel)
    tampered = deepcopy(good)
    _cell(tampered, "A")["components"]["share_of_total"][0] = 0.5
    with pytest.raises(risk.OrthogonalRiskError):
        risk.verify_orthogonal_risk_attribution(tampered, panel)
    dropped = deepcopy(good)
    dropped["modes"]["LIVE_ONLY"]["cells"].pop()
    with pytest.raises(risk.OrthogonalRiskError):
        risk.verify_orthogonal_risk_attribution(dropped, panel)
    versioned = deepcopy(good)
    versioned["schema_version"] = 2
    with pytest.raises(risk.OrthogonalRiskError):
        risk.verify_orthogonal_risk_attribution(versioned, panel)


def test_the_schema_rejects_an_unknown_field_in_a_cell() -> None:
    panel = _panel([_series_row("A"), _series_row("B")])
    block = risk.build_orthogonal_risk_attribution(panel)
    _cell(block, "A")["surprise"] = 1
    assert _errors(block)


def test_a_date_counts_when_two_strategies_are_train_on_it() -> None:
    panel = _panel([_series_row("A"), _series_row("B", offset=3)])
    block = risk.build_orthogonal_risk_attribution(panel)
    aggregate = block["modes"]["LIVE_ONLY"]["aggregates"][0]
    assert aggregate["status"] == "AVAILABLE"
    assert aggregate["window"]["start"] == _day(3)
    assert aggregate["window"]["n"] == DAYS - 3
    assert _errors(block) == []
    risk.verify_orthogonal_risk_attribution(block, panel)

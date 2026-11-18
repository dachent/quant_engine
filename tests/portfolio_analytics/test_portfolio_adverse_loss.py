"""The correlation-one adverse loss is computed by its rule from the governed series, not held as a constant."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import portfolio_adverse_loss as rule

POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "portfolio-dependence-policy-v1.json"
)
POLICY = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
# audit-time live max drawdowns (underwriting_posterior_and_stress.csv, live_max_drawdown column)
AUDIT_LIVE_MDD = {
    "p123-ai-factor": "0.10428436475988678",
    "p123-eu": "0.15327238939432286",
    "qsi": "0.15304234722224175",
    "tt-portfolio": "0.14313447868665696",
    "volatis-strategy": "0.18280509683738055",
    "grizzlybears-vtma": "0.22886022536149908",
    "robuxio-crypto": None,
}


def _drawdown_rows(
    series_id: str, layer: str, drawdown: str, day: str
) -> list[dict[str, str]]:
    """One return whose max drawdown is exactly ``drawdown``."""
    return [
        {
            "series_id": series_id,
            "date": day,
            "return": str(-Decimal(drawdown)),
            "evidence_layer": layer,
        }
    ]


def _live_rows(series_id: str, drawdown: str, count: int = 30) -> list[dict[str, str]]:
    """``count`` live observations (the rule's minimum is 30), the last of which draws down."""
    flat = [
        {
            "series_id": series_id,
            "date": f"2026-01-{day:02d}",
            "return": "0",
            "evidence_layer": "ACCOUNT_LIVE",
        }
        for day in range(1, count)
    ]
    return flat + _drawdown_rows(series_id, "ACCOUNT_LIVE", drawdown, "2026-02-02")


def _rows(
    policy: dict[str, Any],
    live: dict[str, str | None],
    backtest: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for series in policy["series"]:
        sid, strategy = series["series_id"], series["strategy_id"]
        if live.get(strategy) is not None:
            rows += _live_rows(sid, str(live[strategy]))
        # EU and VTMA require their backtest layer; a small one leaves the audit loss unchanged
        bt = (backtest or {}).get(
            strategy, "0.01" if series["backtest_layer_required"] else None
        )
        if bt is not None:
            rows += _drawdown_rows(sid, "BACKTEST", bt, "2020-01-02")
        if not any(r["series_id"] == sid for r in rows):
            rows += _drawdown_rows(sid, "ACCOUNT_LIVE", "0.01", "2026-02-02")
    return rows


def _loss(restated: dict[str, Any], strategy: str) -> Decimal:
    return Decimal(
        next(r for r in restated["series"] if r["strategy_id"] == strategy)[
            "adverse_loss_fraction"
        ]
    )


def test_the_rule_reproduces_the_august_constants_on_the_august_inputs() -> None:
    restated = rule.restate_policy(POLICY, _rows(POLICY, AUDIT_LIVE_MDD))
    for series in POLICY["series"]:
        computed = _loss(restated, series["strategy_id"])
        assert computed == pytest.approx(
            Decimal(series["audit_adverse_loss_fraction"]), abs=Decimal("1e-9")
        ), series["strategy_id"]


def test_a_drawdown_that_grew_since_the_audit_moves_the_loss_to_the_rule_output() -> (
    None
):
    live = {**AUDIT_LIVE_MDD, "volatis-strategy": "0.3834"}
    restated = rule.restate_policy(POLICY, _rows(POLICY, live))
    # 1.5 x 0.3834 + 5pp, above the 0.535 audit constant
    assert _loss(restated, "volatis-strategy") == Decimal("0.6251")
    assert _loss(restated, "volatis-strategy") > Decimal(
        next(s for s in POLICY["series"] if s["strategy_id"] == "volatis-strategy")[
            "audit_adverse_loss_fraction"
        ]
    )


def test_the_governed_backtest_drawdown_is_prior_evidence() -> None:
    restated = rule.restate_policy(
        POLICY,
        _rows(
            POLICY, AUDIT_LIVE_MDD, {"p123-eu": "0.555", "grizzlybears-vtma": "0.4713"}
        ),
    )
    assert _loss(restated, "p123-eu") == Decimal("0.69375")
    assert _loss(restated, "grizzlybears-vtma") == Decimal("0.589125")
    # an unrelated strategy is untouched
    assert _loss(restated, "tt-portfolio") == Decimal("0.214701718030")


def test_a_backtest_below_the_pinned_prior_does_not_lower_the_loss() -> None:
    restated = rule.restate_policy(
        POLICY, _rows(POLICY, AUDIT_LIVE_MDD, {"qsi": "0.10"})
    )
    assert _loss(restated, "qsi") == Decimal("0.59125")


def test_the_loss_is_capped_and_records_its_basis() -> None:
    live = {**AUDIT_LIVE_MDD, "volatis-strategy": "0.9"}
    restated = rule.restate_policy(POLICY, _rows(POLICY, live))
    assert _loss(restated, "volatis-strategy") == Decimal(1)
    basis = next(r for r in restated["series"] if r["strategy_id"] == "tt-portfolio")[
        "adverse_loss_basis"
    ]
    assert basis["audit_adverse_loss_fraction"] == "0.21470171803"
    assert basis["live_max_drawdown"] == "0.143134478687"
    assert basis["rule_statement"] == rule.STATEMENT


def test_a_stored_constant_below_the_rule_output_refuses() -> None:
    rows = _rows(POLICY, {**AUDIT_LIVE_MDD, "volatis-strategy": "0.3834"})
    stale = json.loads(json.dumps(POLICY))
    for series in stale["series"]:
        series["adverse_loss_fraction"] = series["audit_adverse_loss_fraction"]
    with pytest.raises(rule.AdverseLossError, match="Volatis Strategy.*below the rule"):
        rule.refuse_constants_below_rule(stale, rows)
    # at or above the rule output is accepted
    for series in stale["series"]:
        series["adverse_loss_fraction"] = "1"
    rule.refuse_constants_below_rule(stale, rows)


def test_a_policy_without_the_rule_refuses() -> None:
    bare = {key: value for key, value in POLICY.items() if key != "adverse_loss_rule"}
    with pytest.raises(rule.AdverseLossError, match="lacks the adverse loss rule"):
        rule.restate_policy(bare, _rows(POLICY, AUDIT_LIVE_MDD))


def test_a_series_with_no_history_refuses() -> None:
    rows = [r for r in _rows(POLICY, AUDIT_LIVE_MDD) if r["series_id"] != "QSI"]
    with pytest.raises(rule.AdverseLossError, match="no governed history for QSI"):
        rule.restate_policy(POLICY, rows)


def test_the_max_drawdown_is_the_largest_peak_to_trough_loss() -> None:
    returns = [Decimal(x) for x in ("0.10", "-0.20", "0.05", "-0.10")]
    # path 1.1, 0.88, 0.924, 0.8316; trough 0.8316 against the 1.1 peak
    assert rule.max_drawdown(returns) == pytest.approx(
        Decimal("0.244"), abs=Decimal("1e-12")
    )
    assert rule.max_drawdown([]) == 0


def test_a_series_that_requires_its_backtest_layer_refuses_without_it() -> None:
    # EU would otherwise fall back to 1.5 x its live drawdown, the audit constant, with no error
    rows = [
        r
        for r in _rows(POLICY, AUDIT_LIVE_MDD)
        if not (r["series_id"] == "EU [P123]" and r["evidence_layer"] == "BACKTEST")
    ]
    with pytest.raises(
        rule.AdverseLossError, match="EU .P123. requires its governed backtest"
    ):
        rule.restate_policy(POLICY, rows)


def test_the_policy_schema_pins_the_rule_parameters() -> None:
    schema = json.loads(
        (
            POLICY_PATH.parents[1] / "schemas/portfolio/dependence-policy.schema.json"
        ).read_text(encoding="utf-8")
    )
    props = schema["$defs"]["adverse_loss_rule"]["properties"]
    assert [
        props[k]["const"]
        for k in (
            "prior_multiplier",
            "live_multiplier",
            "platform_only_uplift",
            "cap",
            "minimum_live_observations",
        )
    ] == ["1.25", "1.5", "0.05", "1", "30"]


def test_an_accepted_span_compounds_into_the_live_drawdown() -> None:
    rows = _rows(POLICY, AUDIT_LIVE_MDD)
    spans = {
        "QSI": [
            {"start_date": "2026-03-01", "end_date": "2026-03-05", "return": "-0.5"}
        ]
    }
    without = rule.restate_policy(POLICY, rows)
    with_span = rule.restate_policy(POLICY, rows, spans)
    # 1.5 x the 50% span drawdown, well above the 1.25 x 0.473 prior term
    assert _loss(without, "qsi") == Decimal("0.59125")
    assert _loss(with_span, "qsi") > Decimal("0.75")
    assert _loss(with_span, "tt-portfolio") == _loss(without, "tt-portfolio")


def test_the_uplift_applies_below_the_live_observation_minimum() -> None:
    robuxio = next(s for s in POLICY["series"] if s["strategy_id"] == "robuxio-crypto")
    assert robuxio["platform_only_history"] is False
    rows = [
        r
        for r in _rows(POLICY, AUDIT_LIVE_MDD)
        if r["series_id"] != robuxio["series_id"]
    ]
    sid = robuxio["series_id"]
    # below the minimum the live history is missing: 1.25 x 0.352 + 5pp
    thin = rule.restate_policy(POLICY, rows + _live_rows(sid, "0.1103", count=29))
    assert _loss(thin, "robuxio-crypto") == Decimal("0.49")
    basis = next(r for r in thin["series"] if r["strategy_id"] == "robuxio-crypto")
    assert basis["adverse_loss_basis"]["platform_only_uplift_applied"] is True
    assert basis["adverse_loss_basis"]["live_observations"] == 29
    # at the minimum it exists: the uplift goes, 1.25 x 0.352
    full = rule.restate_policy(POLICY, rows + _live_rows(sid, "0.1103", count=30))
    assert _loss(full, "robuxio-crypto") == Decimal("0.44")
    # Volatis stays platform-only whatever its observation count
    assert _loss(full, "volatis-strategy") == Decimal("0.535")


def test_overlapping_model_and_account_live_rows_are_one_live_path() -> None:
    """int14: AI Factor / EU carry MODEL_LIVE and ACCOUNT_LIVE rows on the same dates.

    The published live drawdown compounded both (0.160951, 0.255236) instead of the
    one governed live path (0.085878, 0.153272).
    """

    series = "p123-ai-factor"
    rows = []
    for index in range(40):
        day = f"2026-01-{index + 1:02d}" if index < 31 else f"2026-02-{index - 30:02d}"
        loss = "-0.01" if index == 20 else "0"
        rows.append(
            {
                "series_id": series,
                "date": day,
                "return": loss,
                "evidence_layer": "MODEL_LIVE",
            }
        )
        if index >= 10:  # the account starts later and overlaps the model record
            rows.append(
                {
                    "series_id": series,
                    "date": day,
                    "return": loss,
                    "evidence_layer": "ACCOUNT_LIVE",
                }
            )
    _backtest, live = rule._series_returns(rows, series)
    assert len(live) == 40  # one return per date, not 70
    # one -1% day, not two: the drawdown is 1%, not 1.99%
    assert rule.max_drawdown(live) == Decimal("0.01")
    # where only the model has a row it is used; on shared dates the account row wins
    rows[-1] = {**rows[-1], "return": "-0.05"}
    _backtest, live = rule._series_returns(rows, series)
    assert rule.max_drawdown(live) > Decimal("0.05")


def _gap_rows(account_days: list[int]) -> list[dict[str, str]]:
    rows = []
    for day in range(1, 11):
        rows.append(
            {
                "series_id": "p123-ai-factor",
                "date": f"2026-01-{day:02d}",
                "return": "0.001",
                "evidence_layer": "MODEL_LIVE",
            }
        )
        if day in account_days:
            rows.append(
                {
                    "series_id": "p123-ai-factor",
                    "date": f"2026-01-{day:02d}",
                    "return": "0.002",
                    "evidence_layer": "ACCOUNT_LIVE",
                }
            )
    return rows


def test_model_live_fills_only_the_dates_before_the_accounts_first_live_date() -> None:
    _backtest, live = rule._series_returns(
        _gap_rows([5, 6, 7, 8, 9, 10]), "p123-ai-factor"
    )
    assert len(live) == 10  # four model dates, then six account dates


def test_an_account_gap_after_funding_is_refused_not_filled_with_the_model_record() -> (
    None
):
    """Review follow-on: day 8 has no account return; the model row must not stand in."""

    with pytest.raises(rule.AdverseLossError, match="account gap is not filled"):
        rule._series_returns(_gap_rows([5, 6, 7, 9, 10]), "p123-ai-factor")

"""R2.1 renderer: retained rows, HWM candidate KPI, alpha evidence, cost label, symbol and category."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from test_shell_f3_e1_render import _symbol_cells, _symbol_row
from test_shell_qc1_labels import (
    _capital_payload,
    _cost_strategy,
    _flat,
    _governed_proof,
    _hwm_candidate,
    _lens_payload,
    _positions_payload,
    _risk_payload,
    _run,
)


def _retained(**fields: Any) -> dict[str, Any]:
    row = _cost_strategy(
        strategy_id="composer-holy-grail",
        strategy_name="[Composer] - The Holy Grail",
        record_kind="RETAINED_HISTORY_ONLY",
        cost_status=None,
        annual_external_cost_usd=None,
    )
    row.update(fields)
    return row


def test_retained_records_stay_out_of_the_strategy_register(tmp_path: Path) -> None:
    payload = _capital_payload(_cost_strategy(), _retained())
    table = _flat(_run(tmp_path, payload, "renderCapital"), "#strategy-table")

    assert "GrizzlyBears VTMA" in table
    assert "Holy Grail" not in table


def test_retained_records_stay_out_of_the_implementation_table(tmp_path: Path) -> None:
    payload = _capital_payload(_cost_strategy(), _retained())
    dom = _run(tmp_path, payload, "renderSignals")
    rows = re.findall(r"<tr>.*?</tr>", dom["#implementation-table"]["innerHTML"])

    assert len([row for row in rows if "GrizzlyBears VTMA" in row]) == 1
    assert "Holy Grail" not in dom["#implementation-table"]["innerHTML"]


def test_cost_label_is_the_status_and_the_amount_over_its_period(
    tmp_path: Path,
) -> None:
    known = _cost_strategy(
        annual_external_cost_usd="3590", cost_status="known_gold_api"
    )
    unknown = _cost_strategy(
        strategy_id="eu",
        strategy_name="EU [P123]",
        annual_external_cost_usd=None,
        cost_status="unknown_model_subscription_cost",
    )
    table = _flat(
        _run(tmp_path, _capital_payload(known, unknown), "renderCapital"),
        "#strategy-table",
    )

    assert "Known (gold API) · $3,590.00 per year" in table
    assert "Unknown model subscription cost · annual amount not published" in table


def test_risk_kpi_shows_the_candidate_pending_acceptance_with_the_reason(
    tmp_path: Path,
) -> None:
    advisory = _risk_payload()["advisory"]
    candidate = _hwm_candidate()
    candidate["kpi"] = {
        "value": "143.7000",
        "hwm": "150.0000",
        "drawdown": "-0.0500",
        "label": candidate.get("label"),
        "window_start": "2024-10-15",
        "window_end": "2026-09-17",
        "index_points": 703,
        "gated_status": {
            "policy_id": "PORT-HWM-3",
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
        },
        "missing_daily_points": [{"strategy": "Robuxio", "date": "2026-01-27"}],
    }
    advisory["hwm_candidate"] = candidate
    dom = _run(
        tmp_path,
        _risk_payload(advisory=advisory, hwm_governed_proof=_governed_proof()),
        "renderRisk",
    )
    kpis = _flat(dom, "#risk-kpis")

    assert "Systematic HWM 143.7000" in kpis
    assert "pending owner acceptance" in kpis
    assert "HWM 150.0000" in kpis and "drawdown -5.00%" in kpis
    assert "703 daily points" in kpis
    assert "Missing daily points: Robuxio Jan 27, 2026" in kpis
    assert "Systematic HWM Unavailable" not in kpis


def test_risk_kpi_without_the_kpi_block_reads_the_candidate_fields(
    tmp_path: Path,
) -> None:
    advisory = _risk_payload()["advisory"]
    advisory["hwm_candidate"] = _hwm_candidate()
    dom = _run(
        tmp_path,
        _risk_payload(advisory=advisory, hwm_governed_proof=_governed_proof()),
        "renderRisk",
    )

    kpis = _flat(dom, "#risk-kpis")
    assert "Systematic HWM " in kpis and "pending owner acceptance" in kpis


def _alpha_series(context: dict[str, Any] | None, net_status: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "alpha": {
            "net_annualized": "0.362",
            "net_status": net_status,
            "gross_annualized": None,
            "gross_status": "UNAVAILABLE_MODEL_RETURN_BASIS",
            "annualized_standard_error": None,
            "periodic_intercept": "0.0014",
            "confidence_status": "UNAVAILABLE",
        },
        "coverage": {
            "aligned_observations": 1657,
            "observed_start": "2020-05-11",
            "observed_end": "2026-07-31",
        },
        "contribution_summary": {
            "factor_means": {"MKT_RF": "0.001"},
            "alpha_mean": "0.0001",
            "fitted_mean": "0.001",
            "residual_mean": "0",
            "realized_mean": "0.001",
            "observations": 10,
            "maximum_reconciliation_error": "0",
        },
        "diagnostics": {
            "holdout": {"predicted_r_squared": "0.1", "observation_count": 10},
            "inference": {},
            "residuals": {"train": {}},
            "training_design": {},
        },
        "estimator": "STATIC_RBSA",
        "evidence_layer": "LIVE",
        "exposures": [{"factor_id": "MKT_RF", "beta": "1.0"}],
        "model_id": "m",
        "series_id": "EU [P123]",
        "source_key": "europe-economic",
        "status": "AVAILABLE",
    }
    if context is not None:
        row["alpha_context"] = context
    return row


def _alpha_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = _lens_payload()
    lens = payload["decomposition_lenses"]["modes"]["LIVE_ONLY"][0]
    lens["series"] = [row]
    lens["surfaces"]["STATIC"]["series"] = [row]
    return payload


def _context(**fields: Any) -> dict[str, Any]:
    context = {
        "status": "AVAILABLE",
        "unavailable_reason": None,
        "window_start": "2020-05-11",
        "window_end": "2026-07-31",
        "window_days": 2273,
        "n_obs": 1657,
        "se": None,
        "t_stat": None,
        "t_status": "UNAVAILABLE_NO_STANDARD_ERROR",
        "backtest_share": "0.897",
        "basis_status": "DIAGNOSTIC_ONLY_MIXED_MODEL_BASIS",
        "basis_label": "Diagnostic: mixed model basis, 90% backtest, cost coverage incomplete",
        "periods_per_year": 252,
        "annualisation": "ARITHMETIC_PERIODIC_INTERCEPT_TIMES_PERIODS_PER_YEAR",
        "annualisation_basis": "ANNUALISED",
        "factor_as_of_date": "2026-07-31",
        "factor_staleness_days": 48,
    }
    context.update(fields)
    return context


def test_alpha_tile_prints_window_n_share_basis_and_the_mixed_badge(
    tmp_path: Path,
) -> None:
    row = _alpha_series(_context(), "DIAGNOSTIC_ONLY_MIXED_MODEL_BASIS")
    dom = _run(tmp_path, _alpha_payload(row), "renderFactorDetail")
    tile = _flat(dom, "#factor-detail")

    assert "observed basis" not in tile
    assert "Diagnostic only mixed model basis" in tile  # the mixed-basis badge
    assert "Diagnostic: mixed model basis, 90% backtest" in tile
    assert "2,273 days" in tile and "n 1,657" in tile and "90% backtest" in tile
    assert "no t-statistic: unavailable no standard error" in tile
    assert "Factors as of Jul 31, 2026 (48 days before the cutoff)" in tile
    assert "periods per year" in dom["#factor-detail"]["innerHTML"]


def test_alpha_tile_with_a_standard_error_prints_the_t_statistic(
    tmp_path: Path,
) -> None:
    row = _alpha_series(
        _context(
            se="0.09",
            t_stat="8.99",
            t_status="AVAILABLE_HAC_DESCRIPTIVE_POST_SELECTION",
        ),
        "AVAILABLE_OBSERVED_BASIS",
    )
    tile = _flat(
        _run(tmp_path, _alpha_payload(row), "renderFactorDetail"), "#factor-detail"
    )

    assert "t 8.99 (annualised SE 9.0%)" in tile
    assert "alpha-basis-badge" not in tile


def test_alpha_tile_without_a_context_says_what_is_not_published(
    tmp_path: Path,
) -> None:
    row = _alpha_series(None, "AVAILABLE_OBSERVED_BASIS")
    tile = _flat(
        _run(tmp_path, _alpha_payload(row), "renderFactorDetail"), "#factor-detail"
    )

    assert "observed basis" not in tile.replace("Available: observed basis", "")
    assert "are not published for this alpha" in tile


def test_position_cell_shows_symbol_and_asset_category(tmp_path: Path) -> None:
    payload = _positions_payload()
    row = _symbol_row("AVAILABLE", None, "SPXL", "a1")
    row["asset_category"] = "STK"
    unavailable = _symbol_row("UNAVAILABLE_NO_SYMBOL", "no listing", None, "a2")
    payload["positions"] = [row, unavailable]
    cells = _symbol_cells(_run(tmp_path, payload, "renderPositionTable"))

    assert 'class="cell-primary"' in cells[0] and "SPXL" in cells[0]
    assert '<span class="cell-secondary">Stk</span>' in cells[0]
    assert "Symbol unavailable no symbol" in cells[1]


def test_risk_kpi_shows_the_producer_built_vendor_gap_fill_beside_its_receipt(
    tmp_path: Path,
) -> None:
    """The producer's block (built by the tool, not hand-written) reaches the rendered KPI text."""

    import hashlib
    import json
    import sys
    from datetime import date
    from decimal import Decimal

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import portfolio_vendor_nav_gap_fill as fills

    policy = {
        "origin_labels": {
            "ALTERNATE_SOURCE": "ADMITTED_ALTERNATE_SOURCE",
            "PROVIDER_RETURN_BRIDGE": "ESTIMATED_VENDOR_GAP",
            "LINEAR_INTERPOLATION": "ESTIMATED_VENDOR_GAP",
        },
        "days": [{"date": "2026-01-27", "rule": "LINEAR_INTERPOLATION"}],
        "provider_return_lag_days": 0,
    }
    record = fills.build_fills(
        policy,
        {date(2026, 1, 26): Decimal("0.9"), date(2026, 1, 28): Decimal("0.88")},
        None,
        None,
    )
    record["subject"] = {"strategy_id": "robuxio-crypto"}
    content = json.dumps(record).encode("utf-8")
    point = {
        "strategy_id": "robuxio-crypto",
        "date": "2026-01-27",
        "span_start_date": "2026-01-26",
        "span_end_date": "2026-01-28",
        "span_return": "-0.0217",
    }
    kpi_data = {
        "advisory": {"hwm_candidate": {"kpi": {"missing_daily_points": [point]}}}
    }
    block = fills.build_vendor_gap_fills(
        kpi_data, content, hashlib.sha256(content).hexdigest()
    )

    advisory = _risk_payload()["advisory"]
    candidate = _hwm_candidate()
    candidate["kpi"] = {
        "value": "143.7000",
        "hwm": "150.0000",
        "drawdown": "-0.0500",
        "label": candidate.get("label"),
        "window_start": "2024-10-15",
        "window_end": "2026-09-17",
        "index_points": 703,
        "gated_status": {
            "policy_id": "PORT-HWM-3",
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_numerical_reason": "MISSING_DAILY_POINT_CAN_HIDE_PEAK",
        },
        "missing_daily_points": [point],
    }
    advisory["hwm_candidate"] = candidate
    dom = _run(
        tmp_path,
        _risk_payload(
            advisory=advisory,
            hwm_governed_proof=_governed_proof(),
            vendor_gap_fills=block,
        ),
        "renderRisk",
    )
    kpis = _flat(dom, "#risk-kpis")

    assert "robuxio-crypto Jan 27, 2026" in kpis
    assert "estimated vendor gap" in kpis
    assert "NAV 0.8900" in kpis
    assert "closure residual 0.000000000000" in kpis


# ---- P24: nothing hardcoded in the HWM acceptance wording; scope label and enum labels ----


def test_hwm_acceptance_wording_is_the_payloads_own_state_not_a_literal(
    tmp_path: Path,
) -> None:
    advisory = _risk_payload()["advisory"]
    candidate = _hwm_candidate()
    candidate["financial_acceptance"] = "HELD_FOR_SIGN_OFF"
    candidate["status"] = "CANDIDATE_HELD_FOR_SIGN_OFF"
    advisory["hwm_candidate"] = candidate
    proof = _governed_proof()
    proof["financial_acceptance"] = "HELD_FOR_SIGN_OFF"
    dom = _run(
        tmp_path,
        _risk_payload(advisory=advisory, hwm_governed_proof=proof),
        "renderRisk",
    )
    kpis = _flat(dom, "#risk-kpis")
    card = _flat(dom, "#hwm-status")

    assert "candidate shown, held for sign off" in kpis
    assert "GOVERNED REAL DATA \u2014 HELD FOR SIGN OFF" in card
    assert "pending owner acceptance" not in (kpis + card).lower()


def test_strategy_rows_carry_the_producers_return_scope_label(tmp_path: Path) -> None:
    scoped = _cost_strategy(
        strategy_name="S",
        return_scope_label="Synthetic scope: parent account X; MWR and capital: sleeve Y.",
    )
    bare = _cost_strategy(strategy_id="other", strategy_name="Other")
    capital = _flat(
        _run(tmp_path, _capital_payload(scoped, bare), "renderCapital"),
        "#strategy-table",
    )
    payload = _risk_payload(
        performance=[
            {
                "account_id": "a",
                "mwr_status": "AVAILABLE",
                "strategy_name": "S",
                "twr": "0.1",
                "twr_status": "AVAILABLE",
            }
        ]
    )
    payload["strategies"] = [scoped, bare]
    performance = _flat(_run(tmp_path, payload, "renderRisk"), "#performance-table")

    assert capital.count("Synthetic scope: parent account X") == 1
    implementation = _flat(
        _run(tmp_path, _capital_payload(scoped, bare), "renderSignals"),
        "#implementation-table",
    )
    assert implementation.count("Synthetic scope: parent account X") == 1
    assert performance.count("Synthetic scope: parent account X") == 1

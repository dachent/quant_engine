from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_diligence_rubric.py"
SPEC = importlib.util.spec_from_file_location("portfolio_diligence_rubric", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio diligence rubric")
rubric = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rubric
SPEC.loader.exec_module(rubric)

SERIES = (
    "TT Portfolio",
    "Volatis Strategy",
    "QSI",
    "AI Factor [P123]",
    "GrizzlyBears VTMA",
    "Robuxio Crypto (High Sharpe High Vol)",
    "EU [P123]",
)
CLUSTERS = (
    "TACTICAL_TIMING_MULTI_ASSET",
    "US_SYSTEMATIC_EQUITY",
    "EUROPE_SYSTEMATIC_EQUITY",
    "CRYPTO_DIRECTIONAL",
)


def _dependence() -> dict[str, Any]:
    return {
        "analytics_schema_version": 2,
        "as_of_date": "2026-08-12",
        "policy": {
            "minimum_observations": 20,
            "minimum_downside_observations": 5,
        },
        "dependence": {
            "series_ids": list(SERIES),
            "correlation_one": {
                "loss": "0.105",
                "components": [
                    {
                        "series_id": series_id,
                        "allocation_weight": "0.1428571428571429",
                        "conservative_loss_fraction": "0.15",
                        "loss_contribution": "0.015",
                    }
                    for series_id in SERIES
                ],
            },
            "central_case": {
                "loss": format(math.sqrt(7 * 0.01**2), ".12g"),
                "components": [
                    {
                        "series_id": series_id,
                        "allocation_weight": "0.1",
                        "adverse_loss_fraction": "0.1",
                        "loss_scale_contribution": "0.01",
                    }
                    for series_id in SERIES
                ],
                "dependence_matrix": {
                    "values": [
                        ["1" if left == right else "0" for right in range(7)]
                        for left in range(7)
                    ]
                },
            },
        },
    }


def _candidate() -> dict[str, Any]:
    digest = "a" * 64
    return {
        "schema_version": 1,
        "candidate_id": "candidate-a",
        "candidate_name": "Candidate A",
        "as_of_date": "2026-08-12",
        "record_lineage": {
            "ruleset_version": "candidate-a/v1",
            "effective_date": "2025-01-01",
            "immutable_version_sha256": digest,
            "signal_archive_sha256": "b" * 64,
            "version_matches_return_evidence": True,
        },
        "subscriber_realism": {
            "return_series_sha256": "c" * 64,
            "return_observations": 30,
            "observed_start": "2025-01-01",
            "observed_end": "2026-08-12",
            "evidence_layer": "ACCOUNT_LIVE",
            "net_of_all_strategy_costs": True,
            "accepted_evidence_layers_for_verdict": ["ACCOUNT_LIVE"],
            "minimum_overlap_days_for_verdict": 365,
            "evidence_threshold_authority": "candidate-underwriting-record/v1",
        },
        "tail_boundary": {
            "position_risk_cube_sha256": "d" * 64,
            "payoff_reproduction_sha256": "e" * 64,
            "worst_loss_fraction": "0.1",
            "stated_loss_limit_fraction": "0.2",
        },
        "scenario": {
            "mode": "REPLACE",
            "funding_source": "REPLACEMENT_PROCEEDS",
            "replacement_series_id": SERIES[0],
            "systematic_weight": "0.05",
            "conservative_loss_fraction": "0.1",
            "central_loss_fraction": "0.08",
            "economic_driver_cluster_id": "TACTICAL_TIMING_MULTI_ASSET",
            "economic_driver_cluster_authority": "economic-driver-map/v1",
            "projection_eigenvalue_floor": "0.001",
            "projection_authority": "accepted-dependence-spec-sha256",
        },
        "net_economics": {
            "sleeve_notional": "10000",
            "subscription_annual": "100",
            "expense_ratio": "0.01",
            "spread_fraction": "0.001",
            "commissions_annual": "20",
            "tax_cost_annual": "30",
            "tax_character": "MIXED",
        },
        "operational_fit": {
            "supported_broker": True,
            "order_protocol_evidenced": True,
            "reconciliation_evidenced": True,
            "kill_switch_evidenced": True,
        },
        "independence": {
            "economic_shock_map": "pays in sustained defensive rotation",
            "maximum_downside_correlation_for_pass": "0.3",
            "minimum_worst_decile_return_for_pass": "0",
            "decision_threshold_authority": "candidate-underwriting-record/v1",
        },
        "capacity_liquidity": {
            "modeled_position": "10000",
            "maximum_position": "50000",
            "daily_dollar_volume": "1000000",
            "open_interest": "10000",
            "bid_ask_spread": "0.001",
            "market_impact": "0.002",
            "liquidation_days": "1",
            "maximum_liquidation_days": "3",
        },
        "correlations": [
            {
                "series_id": series_id,
                "status": "AVAILABLE",
                "pearson": "0.1",
                "downside_correlation": "0.1",
                "worst_decile_average_return": "0.01",
                "overlap_observations": 30,
                "downside_observations": 10,
                "overlap_calendar_days": 400,
                "evidence_layer": "ACCOUNT_LIVE",
            }
            for series_id in SERIES
        ],
        "cluster_correlations": [
            {
                "cluster_id": cluster_id,
                "status": "AVAILABLE",
                "pearson": "0.1",
                "downside_correlation": "0.1",
                "worst_decile_average_return": "0.01",
                "overlap_observations": 30,
                "downside_observations": 10,
                "overlap_calendar_days": 400,
                "evidence_layer": "ACCOUNT_LIVE",
            }
            for cluster_id in CLUSTERS
        ],
        "replacement_comparison": {
            "candidate_observed_start": "2025-01-01",
            "candidate_observed_end": "2026-08-12",
            "candidate_evidence_layer": "ACCOUNT_LIVE",
            "incumbent_observed_start": "2025-01-01",
            "incumbent_observed_end": "2026-08-12",
            "incumbent_evidence_layer": "ACCOUNT_LIVE",
        },
    }


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _write(tmp_path / "dependence.json", _dependence()),
        _write(tmp_path / "candidate.json", _candidate()),
    )


def _gate(payload: dict[str, Any], gate_id: str) -> dict[str, Any]:
    return next(row for row in payload["gates"] if row["gate_id"] == gate_id)


def test_build_reproduces_baseline_and_executes_all_eight_gates(
    tmp_path: Path,
) -> None:
    dependence, candidate = _inputs(tmp_path)
    output = tmp_path / "rubric.json"

    payload = rubric.build(dependence, candidate, output)

    assert payload["gate_counts"] == {"PASS": 8, "FAIL": 0, "UNAVAILABLE": 0}
    assert [row["gate_id"] for row in payload["gates"]] == list(rubric.EXPECTED_GATES)
    assert payload["scenario"]["baseline"] == {
        "correlation_one_loss": "0.105",
        "central_case_loss": format(math.sqrt(7 * 0.01**2), ".12g"),
    }
    assert payload["scenario"]["result"]["correlation_one_loss"] == "0.10475"
    assert payload["scenario"]["result"]["central_case_status"] == "AVAILABLE"
    assert payload["replacement_comparison"] == {"status": "PASS", "reasons": []}
    assert len(payload["leave_one_out"]) == 7
    assert payload["leave_one_out"][0]["correlation_one_loss_without"] == "0.09"
    assert float(
        payload["leave_one_out"][0]["central_case_loss_without"]
    ) == pytest.approx(math.sqrt(6 * 0.01**2))
    assert payload["authority_boundary"] == {
        "allocation_effect": "NONE",
        "execution_effect": "NONE",
        "readiness_effect": "NONE",
        "automatic_add_drop_resize_authority": "NONE",
    }
    assert output.read_bytes().endswith(b"\n")


def test_identical_inputs_rebuild_identical_bytes_and_verify(tmp_path: Path) -> None:
    dependence, candidate = _inputs(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    rubric.build(dependence, candidate, first)
    rubric.build(dependence, candidate, second)

    assert first.read_bytes() == second.read_bytes()
    verification = rubric.verify(dependence, candidate, first)
    assert verification["ok"] is True
    assert verification["gate_counts"] == {"PASS": 8, "FAIL": 0, "UNAVAILABLE": 0}


def test_missing_or_contradicted_evidence_fails_closed(tmp_path: Path) -> None:
    dependence = _write(tmp_path / "dependence.json", _dependence())
    candidate_value = _candidate()
    del candidate_value["subscriber_realism"]["return_series_sha256"]
    candidate_value["tail_boundary"]["worst_loss_fraction"] = "0.3"
    candidate_value["operational_fit"]["kill_switch_evidenced"] = False
    candidate_value["subscriber_realism"]["evidence_layer"] = "BACKTEST"
    candidate_value["correlations"][1]["overlap_observations"] = 10
    candidate = _write(tmp_path / "candidate.json", candidate_value)

    payload = rubric.build(dependence, candidate, tmp_path / "rubric.json")

    assert _gate(payload, "subscriber_realism")["status"] == "UNAVAILABLE"
    assert _gate(payload, "tail_boundary")["status"] == "FAIL"
    assert _gate(payload, "operational_fit")["status"] == "FAIL"
    assert _gate(payload, "independence")["status"] == "UNAVAILABLE"


def test_missing_scenario_fact_emits_unavailable_without_losing_baseline(
    tmp_path: Path,
) -> None:
    dependence = _write(tmp_path / "dependence.json", _dependence())
    candidate_value = _candidate()
    del candidate_value["scenario"]["central_loss_fraction"]
    candidate = _write(tmp_path / "candidate.json", candidate_value)

    payload = rubric.build(dependence, candidate, tmp_path / "rubric.json")

    assert payload["scenario"]["status"] == "UNAVAILABLE"
    assert payload["scenario"]["baseline"]["correlation_one_loss"] == "0.105"
    assert _gate(payload, "portfolio_hwm")["status"] == "UNAVAILABLE"


def test_missing_candidate_pair_makes_central_hwm_unavailable(tmp_path: Path) -> None:
    dependence = _write(tmp_path / "dependence.json", _dependence())
    candidate_value = _candidate()
    candidate_value["correlations"] = [
        row for row in candidate_value["correlations"] if row["series_id"] != SERIES[1]
    ]
    candidate = _write(tmp_path / "candidate.json", candidate_value)

    payload = rubric.build(dependence, candidate, tmp_path / "rubric.json")

    assert payload["scenario"]["result"]["central_case_status"] == "UNAVAILABLE"
    assert _gate(payload, "portfolio_hwm")["status"] == "UNAVAILABLE"


def test_replacement_comparison_requires_identical_window_and_layer(
    tmp_path: Path,
) -> None:
    dependence = _write(tmp_path / "dependence.json", _dependence())
    candidate_value = _candidate()
    candidate_value["replacement_comparison"]["incumbent_observed_end"] = "2026-08-11"
    candidate = _write(tmp_path / "candidate.json", candidate_value)

    payload = rubric.build(dependence, candidate, tmp_path / "rubric.json")

    assert payload["replacement_comparison"] == {
        "status": "FAIL",
        "reasons": ["END_DATE_MISMATCH"],
    }


def test_resize_and_new_cluster_modes_are_deterministic(tmp_path: Path) -> None:
    dependence = _write(tmp_path / "dependence.json", _dependence())
    resize_value = _candidate()
    resize_value["scenario"]["mode"] = "RESIZE"
    resize = _write(tmp_path / "resize.json", resize_value)

    resized = rubric.build(dependence, resize, tmp_path / "resize-output.json")

    assert resized["scenario"]["result"]["correlation_one_loss"] == "0.10475"
    assert resized["replacement_comparison"] == {
        "status": "NOT_APPLICABLE",
        "reasons": [],
    }

    add_value = _candidate()
    add_value["scenario"]["mode"] = "ADD"
    add_value["scenario"]["funding_source"] = "RESERVE_TRANSFER"
    add_value["scenario"]["replacement_series_id"] = None
    add_value["scenario"]["economic_driver_cluster_id"] = "NEW_DEFENSIVE"
    added = rubric.build(
        dependence,
        _write(tmp_path / "add.json", add_value),
        tmp_path / "add-output.json",
    )

    assert added["scenario"]["result"]["economic_driver_cluster_status"] == (
        "CANDIDATE_DECLARED_NEW_CLUSTER"
    )
    assert added["scenario"]["result"]["cluster_loss"] == "0.005"
    assert [
        (row["cluster_id"], row["scenario_loss"])
        for row in added["scenario"]["result"]["cluster_budget_effects"]
    ] == [
        ("CRYPTO_DIRECTIONAL", "0.01425"),
        ("EUROPE_SYSTEMATIC_EQUITY", "0.01425"),
        ("NEW_DEFENSIVE", "0.005"),
        ("TACTICAL_TIMING_MULTI_ASSET", "0.057"),
        ("US_SYSTEMATIC_EQUITY", "0.01425"),
    ]


def test_verify_rejects_tampered_artifact(tmp_path: Path) -> None:
    dependence, candidate = _inputs(tmp_path)
    output = tmp_path / "rubric.json"
    rubric.build(dependence, candidate, output)
    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["gate_counts"]["PASS"] = 7
    _write(output, tampered)

    with pytest.raises(rubric.DiligenceError, match="differs"):
        rubric.verify(dependence, candidate, output)


def test_current_portfolio_boundary_is_exactly_seven_series(tmp_path: Path) -> None:
    dependence_value = _dependence()
    dependence_value["dependence"]["series_ids"].pop()
    dependence = _write(tmp_path / "dependence.json", dependence_value)
    candidate = _write(tmp_path / "candidate.json", _candidate())

    with pytest.raises(rubric.DiligenceError, match="series count differs"):
        rubric.build(dependence, candidate, tmp_path / "rubric.json")


def test_output_schema_preserves_fixed_authority_and_cardinality() -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "portfolio"
            / "diligence-rubric-output.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["properties"]["gates"]["minItems"] == 8
    assert schema["properties"]["gates"]["maxItems"] == 8
    assert schema["properties"]["leave_one_out"]["minItems"] == 7
    assert schema["properties"]["leave_one_out"]["maxItems"] == 7
    boundary = schema["properties"]["authority_boundary"]["properties"]
    assert {field: value["const"] for field, value in boundary.items()} == {
        "allocation_effect": "NONE",
        "execution_effect": "NONE",
        "readiness_effect": "NONE",
        "automatic_add_drop_resize_authority": "NONE",
    }

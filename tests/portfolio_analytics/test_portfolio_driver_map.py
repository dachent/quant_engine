from __future__ import annotations

import csv
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_driver_map.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("portfolio_driver_map", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio driver map")
driver_map = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = driver_map
SPEC.loader.exec_module(driver_map)

SERIES = (
    "TT Portfolio",
    "Volatis Strategy",
    "QSI",
    "AI Factor [P123]",
    "GrizzlyBears VTMA",
    "Robuxio Crypto (High Sharpe High Vol)",
    "EU [P123]",
)


def _dependence(series: tuple[str, ...] = SERIES) -> dict[str, Any]:
    series_count = len(series)
    return {
        "analytics_schema_version": 2,
        "dependence": {
            "series_ids": list(series),
            "blend_cells": [
                {
                    "left": series[left],
                    "right": series[right],
                    "observed_correlation": "0.2",
                }
                for left in range(series_count)
                for right in range(left + 1, series_count)
            ],
            "correlation_one": {
                "loss": format(series_count * 0.015, ".12g"),
                "components": [
                    {
                        "series_id": series_id,
                        "allocation_weight": format(1 / series_count, ".12g"),
                        "conservative_loss_fraction": "0.15",
                        "loss_contribution": "0.015",
                    }
                    for series_id in series
                ],
            },
            "central_case": {
                "loss": format(math.sqrt(series_count * 0.01**2), ".12g"),
                "components": [
                    {
                        "series_id": series_id,
                        "loss_scale_contribution": "0.01",
                    }
                    for series_id in series
                ],
                "dependence_matrix": {
                    "values": [
                        ["1" if left == right else "0" for right in range(series_count)]
                        for left in range(series_count)
                    ]
                },
            },
        },
    }


def _providers() -> list[dict[str, str]]:
    return [
        {
            "candidate": f"Candidate {index:02d}",
            "ticker": f"C{index:02d}" if index < 21 else "",
            "category": "Test category",
            "tier": f"Tier {(index % 4) + 1}",
            "portfolio_role": "Research only",
            "expense_ratio": "0.01" if index != 32 else "",
            "annual_fixed_cost": "0",
        }
        for index in range(33)
    ]


def _candidate_dependence() -> list[dict[str, str]]:
    return [
        {
            "candidate": ticker,
            "current_component": member,
            "start_date": "2026-01-01",
            "end_date": "2026-07-17",
            "observations": "120",
            "pearson_correlation": str(0.1 + index / 100),
            "downside_correlation_on_current_loss_days": str(0.2 + index / 100),
            "candidate_avg_return_on_worst_10pct_current_days": "0.01",
        }
        for index, ticker in enumerate(("C00", "C01"))
        for member in ("TT Portfolio", "QSI", "GrizzlyBears VTMA")
    ]


def _metrics() -> list[dict[str, str]]:
    return [
        {
            "ticker": f"C{index:02d}",
            "window": "full_available_live",
            "start_date": "2020-01-01",
            "end_date": "2026-07-24",
            "observations": "1000",
            "cagr": "0.1",
            "max_drawdown": "-0.2",
            "ulcer_index": "0.1",
            "upi_vs_3m_treasury": "0.5",
            "source": "retained public live vehicle",
        }
        for index in range(21)
    ]


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    return (
        _write_json(tmp_path / "dependence.json", _dependence()),
        _write_csv(tmp_path / "providers.csv", _providers()),
        _write_csv(tmp_path / "candidate-dependence.csv", _candidate_dependence()),
        _write_csv(tmp_path / "metrics.csv", _metrics()),
    )


def test_build_maps_33_candidates_and_four_tactical_leave_one_out_rows(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    payload = driver_map.build(*inputs, tmp_path / "driver-map.json")

    assert payload["coverage"] == {
        "candidate_count": 33,
        "measured_candidate_count": 2,
        "unavailable_candidate_count": 31,
        "retained_dependence_row_count": 6,
        "cluster_dependence_row_count": 6,
        "out_of_scope_dependence_row_count": 0,
        "dependence_scope_status": (
            "CLUSTER_SCORING_EXCLUDES_NON_CLUSTER_CURRENT_COMPONENTS"
        ),
        "refresh_status": "UNAVAILABLE_RAW_RETURN_INPUTS_NOT_RETAINED",
    }
    assert len(payload["driver_clusters"]) == 4
    assert len(payload["tactical_leave_one_out"]) == 4
    assert payload["authority_boundary"] == {
        "allocation_effect": "NONE",
        "execution_effect": "NONE",
        "readiness_effect": "NONE",
        "automatic_add_drop_authority": "NONE",
    }


def test_cluster_budget_uses_exact_source_contract_hash() -> None:
    contract = json.loads(driver_map.CLUSTER_CONTRACT.read_text(encoding="utf-8"))
    exact_source_hash = "b" * 64

    accepted, _ = driver_map._cluster_map(_dependence(), contract, exact_source_hash)

    assert (
        accepted["economic_driver_cluster_budget"]["contract_sha256"]
        == exact_source_hash
    )


def test_current_cluster_contract_budgets_only_product_required_series() -> None:
    financial_contract = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )
    active_series = tuple(
        row["history_series_id"]
        for row in financial_contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    )
    cluster_contract = json.loads(
        driver_map.CLUSTER_CONTRACT.read_text(encoding="utf-8")
    )

    accepted, clusters = driver_map._cluster_map(
        _dependence(active_series), cluster_contract, "c" * 64
    )

    budget = accepted["economic_driver_cluster_budget"]
    assert len(active_series) == 7
    assert {member for cluster in clusters for member in cluster["members"]} == set(
        active_series
    )
    assert [row["cluster_id"] for row in budget["clusters"]] == [
        "TACTICAL_TIMING_MULTI_ASSET",
        "US_SYSTEMATIC_EQUITY",
        "EUROPE_SYSTEMATIC_EQUITY",
        "CRYPTO_DIRECTIONAL",
    ]


def test_partial_measurement_is_ranked_but_never_promoted_to_verdict(
    tmp_path: Path,
) -> None:
    payload = driver_map.build(*_inputs(tmp_path), tmp_path / "driver-map.json")
    first = payload["candidates"][0]
    tactical = next(
        row
        for row in first["cluster_scores"]
        if row["cluster_id"] == driver_map.TACTICAL_CLUSTER
    )

    assert tactical["status"] == "PARTIAL_DESCRIPTIVE_FROZEN"
    assert tactical["missing_members"] == ["Volatis Strategy"]
    assert tactical["descriptive_rank"] == 1


def test_unmeasured_candidate_and_missing_cost_remain_explicit(tmp_path: Path) -> None:
    payload = driver_map.build(*_inputs(tmp_path), tmp_path / "driver-map.json")
    candidate = payload["candidates"][-1]

    assert candidate["live_metrics"]["status"] == "UNAVAILABLE"
    assert candidate["cost"]["status"] == "PARTIAL_COMPONENTS_NO_MODELED_NOTIONAL"
    assert all(
        row["status"] == "UNAVAILABLE_NO_ACCEPTED_MEASUREMENTS"
        for row in candidate["cluster_scores"]
    )


def test_identical_inputs_rebuild_identical_bytes_and_verify(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    driver_map.build(*inputs, first)
    driver_map.build(*inputs, second)

    assert first.read_bytes() == second.read_bytes()
    sentinel = tmp_path / ".first.json.verify"
    sentinel.write_bytes(b"owned-by-another-process")
    assert driver_map.verify(*inputs, first)["ok"] is True
    assert sentinel.read_bytes() == b"owned-by-another-process"


def test_provider_ticker_is_normalized_before_evidence_joins(tmp_path: Path) -> None:
    inputs = list(_inputs(tmp_path))
    providers = _providers()
    providers[0]["ticker"] = "  C00  "
    inputs[1] = _write_csv(tmp_path / "providers-with-whitespace.csv", providers)

    payload = driver_map.build(*inputs, tmp_path / "driver-map.json")

    candidate = payload["candidates"][0]
    assert candidate["ticker"] == "C00"
    assert candidate["live_metrics"]["status"].startswith("AVAILABLE")
    assert any(
        score["status"] != "UNAVAILABLE_NO_ACCEPTED_MEASUREMENTS"
        for score in candidate["cluster_scores"]
    )


def test_exact_candidate_count_is_enforced(tmp_path: Path) -> None:
    inputs = list(_inputs(tmp_path))
    inputs[1] = _write_csv(tmp_path / "short-providers.csv", _providers()[:-1])

    with pytest.raises(driver_map.DriverMapError, match="exactly 33"):
        driver_map.build(*inputs, tmp_path / "driver-map.json")


def test_duplicate_dependence_pair_is_rejected(tmp_path: Path) -> None:
    inputs = list(_inputs(tmp_path))
    rows = _candidate_dependence()
    inputs[2] = _write_csv(tmp_path / "duplicate-dependence.csv", [*rows, rows[0]])

    with pytest.raises(driver_map.DriverMapError, match="pair is duplicated"):
        driver_map.build(*inputs, tmp_path / "driver-map.json")


def test_out_of_universe_metric_is_rejected(tmp_path: Path) -> None:
    inputs = list(_inputs(tmp_path))
    rows = _metrics()
    inputs[3] = _write_csv(
        tmp_path / "outside-metrics.csv",
        [*rows, {**rows[0], "ticker": "OUTSIDE"}],
    )

    with pytest.raises(driver_map.DriverMapError, match="outside provider universe"):
        driver_map.build(*inputs, tmp_path / "driver-map.json")


def test_non_cluster_dependence_rows_are_counted_but_not_scored(
    tmp_path: Path,
) -> None:
    inputs = list(_inputs(tmp_path))
    rows = _candidate_dependence()
    inputs[2] = _write_csv(
        tmp_path / "broad-dependence.csv",
        [*rows, {**rows[0], "current_component": "Broader account aggregate"}],
    )

    payload = driver_map.build(*inputs, tmp_path / "driver-map.json")

    assert payload["coverage"]["retained_dependence_row_count"] == 7
    assert payload["coverage"]["cluster_dependence_row_count"] == 6
    assert payload["coverage"]["out_of_scope_dependence_row_count"] == 1

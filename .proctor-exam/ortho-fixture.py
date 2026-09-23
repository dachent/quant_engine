"""Frozen synthetic constructors from a579a35 decomposition tests; parent owned.
No provider data. The verifier injects the candidate decomposition module.
"""
from __future__ import annotations
import csv
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

def _days(count: int, start: date = date(2025, 1, 2)) -> list[date]:
    accepted: list[date] = []
    current = start
    while len(accepted) < count:
        if current.weekday() < 5:
            accepted.append(current)
        current += timedelta(days=1)
    return accepted


def _hash(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _write_sources(
    root: Path,
    *,
    count: int = 140,
    formula: str = "unconstrained",
    identical_factors: bool = False,
    target_only_dates: tuple[date, ...] = (),
    mixed_layer: bool = False,
    complete_costs: bool = False,
) -> tuple[Path, list[date]]:
    root.mkdir(parents=True)
    days = _days(count)
    target_path = root / "targets.csv"
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(decomposition.TARGET_HEADERS)
        for index, day in enumerate([*days, *target_only_dates]):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = f1 if identical_factors else 0.006 * math.cos(index / 7.0)
            if formula == "simplex":
                excess = 0.0002 + 0.7 * f1 + 0.3 * f2
            elif formula == "single":
                excess = 0.0001 + 0.8 * f1
            else:
                excess = 0.0001 + 0.6 * f1 - 0.2 * f2
            layer = "BACKTEST" if mixed_layer and index == count - 1 else "ACCOUNT_LIVE"
            writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + 0.00005, ".17g"),
                    layer,
                    "NET",
                    "TOTAL",
                    "0.00001" if complete_costs else "",
                ]
            )
    factor_path = root / "factors.csv"
    with factor_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = f1 if identical_factors else 0.006 * math.cos(index / 7.0)
            writer.writerow(
                [day.isoformat(), "0.00005", format(f1, ".17g"), format(f2, ".17g")]
            )
    return target_path, days


def _model(
    days: list[date],
    *,
    model_id: str = "model-two-factor",
    factors: list[str] | None = None,
    constraint: str = "UNCONSTRAINED",
    cost_coverage: str = "INCOMPLETE",
    minimum_train: int = 40,
    minimum_test: int = 20,
) -> dict[str, object]:
    return {
        "constraint": constraint,
        "cost_coverage": cost_coverage,
        "cv_folds": 3,
        "factor_ids": factors or ["factor-one", "factor-two"],
        "minimum_test_observations": minimum_test,
        "minimum_train_observations": minimum_train,
        "model_id": model_id,
        "parsimony_tolerance": "0.000001",
        "periods_per_year": 252,
        "ridge_candidates": ["0", "0.000001", "0.0001"],
        "series_id": "synthetic-strategy",
        "test_start": days[90].isoformat(),
        "train_end": days[89].isoformat(),
    }


def _dynamic_model(days: list[date]) -> dict[str, object]:
    model = _model(
        days,
        model_id="dynamic-fls",
        minimum_train=60,
        minimum_test=20,
    )
    model.update(
        {
            "estimator": "DYNAMIC_FLS",
            "ridge_candidates": ["0.0000000001"],
            "smoothness_candidates": [
                "0.00000001",
                "0.0000001",
                "0.000001",
                "0.00001",
                "0.0001",
                "0.001",
            ],
            "test_start": days[105].isoformat(),
            "train_end": days[104].isoformat(),
        }
    )
    return model


def _write_dynamic_sources(
    root: Path,
) -> tuple[Path, list[date], list[tuple[float, float]]]:
    root.mkdir(parents=True)
    days = _days(150)
    truth: list[tuple[float, float]] = []
    target_path = root / "targets.csv"
    factor_path = root / "factors.csv"
    with (
        target_path.open("w", encoding="utf-8", newline="") as target_handle,
        factor_path.open("w", encoding="utf-8", newline="") as factor_handle,
    ):
        target_writer = csv.writer(target_handle)
        factor_writer = csv.writer(factor_handle)
        target_writer.writerow(decomposition.TARGET_HEADERS)
        factor_writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.012 * math.sin(index / 3.7) + 0.004 * math.cos(index / 13.0)
            f2 = 0.010 * math.cos(index / 5.1) - 0.003 * math.sin(index / 17.0)
            beta_one = 0.15 + 0.70 * index / (len(days) - 1)
            beta_two = -0.35 + 0.30 * math.sin(index / 28.0)
            alpha = 0.00015
            risk_free = 0.00005
            excess = alpha + beta_one * f1 + beta_two * f2
            truth.append((beta_one, beta_two))
            target_writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + risk_free, ".17g"),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "",
                ]
            )
            factor_writer.writerow(
                [
                    day.isoformat(),
                    format(risk_free, ".17g"),
                    format(f1, ".17g"),
                    format(f2, ".17g"),
                ]
            )
    return target_path, days, truth


def _spec(
    root: Path,
    target_path: Path,
    days: list[date],
    *,
    models: list[dict[str, object]] | None = None,
    maximum_staleness_days: int = 7,
    analysis_lens: str | None = None,
) -> Path:
    factor_path = root / "factors.csv"
    target_hash, target_size = _hash(target_path)
    factor_hash, factor_size = _hash(factor_path)
    value = {
        "as_of_date": max(days).isoformat(),
        "factor_source": {
            "artifact_id": "frozen/factors.csv",
            "date_column": "date",
            "expected_sha256": factor_hash,
            "expected_size_bytes": factor_size,
            "factors": [
                {
                    "column": "F1",
                    "factor_id": "factor-one",
                    "return_type": "EXCESS",
                },
                {
                    "column": "F2",
                    "factor_id": "factor-two",
                    "return_type": "EXCESS",
                },
            ],
            "relative_path": "factors.csv",
            "risk_free_column": "RF",
            "schema_name": "synthetic-factor-returns",
            "schema_version": "1",
        },
        "maximum_factor_staleness_days": maximum_staleness_days,
        "models": models or [_model(days)],
        "schema_version": 1,
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "expected_sha256": target_hash,
            "expected_size_bytes": target_size,
            "relative_path": "targets.csv",
            "schema_name": "synthetic-target-returns",
            "schema_version": "1",
        },
    }
    if analysis_lens is not None:
        value["analysis_lens"] = analysis_lens
    spec_path = root / "spec.json"
    spec_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return spec_path



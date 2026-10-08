"""Verifier work counters and the per-call verification ledger (Z3a, M7).

The producers are the real ones; nothing about the code under test is patched.
The helper builders below are the same synthetic source builders the producer test
modules use, copied here because test modules are not importable from each other.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core import pipeline
from quant_engine.core.run_storage import create_cache_overlay

import portfolio_correlation as correlation
import portfolio_decomposition as decomposition
import portfolio_financial_outcomes as outcomes

LEDGER = "financial-verification-ledger"
RECEIPTS = "financial-replay-receipts"
LEDGER_NAME = re.compile(
    r"^\d{8}T\d{12}-\d+-[A-Za-z0-9._-]+-[0-9a-f]{16}(-\d+)?\.json$"
)
ZERO = {
    "model_fit_calls": 0,
    "grid_search_calls": 0,
    "correlation_build_calls": 0,
    "full_rederivation_count": 0,
}


def _hash(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _dec_days(count: int, start: date = date(2025, 1, 2)) -> list[date]:
    accepted: list[date] = []
    current = start
    while len(accepted) < count:
        if current.weekday() < 5:
            accepted.append(current)
        current += timedelta(days=1)
    return accepted


def _dec_model(
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


def _dec_dynamic_model(days: list[date]) -> dict[str, object]:
    model = _dec_model(
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


def _write_dec_sources(
    root: Path,
) -> tuple[Path, list[date], list[tuple[float, float]]]:
    root.mkdir(parents=True)
    days = _dec_days(150)
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


def _dec_spec(
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
            "interval_policy": {
                "available_through_anchor": max(days).isoformat(),
                "currency": "USD",
                "finality": "COMPLETE_SELECTED_SNAPSHOT",
                "native_calendar": "SOURCE_LABELLED_DAILY",
                "native_label_convention": "PUBLISHED_DAILY_LABEL",
                "return_basis": "NET",
                "source_vintage": "SELECTED_SNAPSHOT",
            },
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
        "models": models or [_dec_model(days)],
        "schema_version": 1,
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "expected_sha256": target_hash,
            "expected_size_bytes": target_size,
            "relative_path": "targets.csv",
            "schema_name": "synthetic-target-returns",
            "schema_version": "1",
            "interval_policy": {
                "currency": "USD",
                "evaluation_information_set": "RETROSPECTIVE_SELECTED_SNAPSHOT",
                "finality": "FINALIZED_THROUGH_DECLARED_LABEL",
                "finalized_through": max(days).isoformat(),
                "gap_disposition": "REJECT_BUCKET",
                "native_calendar": "SESSION_LABELLED",
                "native_label_convention": "SYNTHETIC_EXCHANGE_SESSION_DATE",
                "return_basis": "NET",
                "session_schedule_labels": {
                    "synthetic-strategy": [
                        item.isoformat() for item in sorted(set(days))
                    ]
                },
                "source_vintage": "SELECTED_SNAPSHOT",
            },
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


def _cor_days(count: int) -> list[date]:
    accepted: list[date] = []
    current = date(2025, 1, 2)
    while len(accepted) < count:
        if current.weekday() < 5:
            accepted.append(current)
        current += timedelta(days=1)
    return accepted


def _write_cor_returns(
    root: Path, *, mixed_layer: bool = False
) -> tuple[Path, list[date]]:
    root.mkdir(parents=True, exist_ok=True)
    days = _cor_days(120)
    path = root / "returns.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(correlation.RETURN_HEADERS)
        for index, day in enumerate(days):
            base = 0.008 * math.sin(index / 5.0) + 0.003 * math.cos(index / 13.0)
            values = {
                "base": base,
                "identical": base,
                "inverse": -base,
                "independent": 0.007 * math.sin(index * 1.731 + 0.4),
                "constant": 0.001,
                "gapped": base + 0.000001 * math.sin(index),
                "provider": base,
            }
            for series_id, value in values.items():
                if series_id == "gapped" and index % 2:
                    continue
                layer = "PROVIDER_LIVE" if series_id == "provider" else "ACCOUNT_LIVE"
                if mixed_layer and series_id == "base" and index == 119:
                    layer = "BACKTEST"
                writer.writerow(
                    [series_id, day.isoformat(), format(value, ".17g"), layer]
                )
            if index < 10:
                writer.writerow(
                    ["short", day.isoformat(), format(base, ".17g"), "ACCOUNT_LIVE"]
                )
    return path, days


def _cor_spec(root: Path, source: Path, days: list[date]) -> Path:
    source_hash, source_size = _hash(source)
    priors = [
        {
            "evidence_note": "SYNTHETIC conceptual prior",
            "left": min(left, right),
            "right": max(left, right),
            "value": "0.2",
        }
        for index, left in enumerate(SERIES)
        for right in SERIES[index + 1 :]
    ]
    components = [
        {
            "adverse_loss_fraction": "0.3",
            "allocation_weight": format(1.0 / len(SERIES), ".17g"),
            "series_id": series_id,
        }
        for series_id in SERIES
    ]
    value = {
        "as_of_date": days[-1].isoformat(),
        "conceptual_priors": priors,
        "correlation_one": {
            "components": components,
            "expected_loss": "0.3",
            "tolerance": "0.000000001",
        },
        "eigenvalue_floor": "0.00000001",
        "full_observed_weight_observations": 500,
        "implementation_mappings": [
            {
                "instruments": ["SYNTH-A", "SYNTH-B"],
                "providers": ["SYNTH-PROVIDER"],
                "series_id": "base",
                "venues": ["SYNTH-VENUE"],
            },
            {
                "instruments": ["SYNTH-A"],
                "providers": ["SYNTH-PROVIDER"],
                "series_id": "identical",
                "venues": ["SYNTH-VENUE"],
            },
        ],
        "maximum_observed_weight": "0.4",
        "minimum_downside_observations": 10,
        "minimum_observations": 30,
        "minimum_rolling_windows": 1,
        "return_source": {
            "artifact_id": "synthetic/returns.csv",
            "expected_sha256": source_hash,
            "expected_size_bytes": source_size,
            "relative_path": "returns.csv",
            "schema_name": "synthetic-daily-returns",
            "schema_version": "1",
        },
        "rolling_window": 40,
        "schema_version": 1,
        "series_ids": list(SERIES),
        "stress_cap": "0.95",
        "stress_uplift": "0.15",
        "tail_quantile": "0.1",
    }
    path = root / "spec.json"
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return path


SERIES = (
    "base",
    "identical",
    "inverse",
    "independent",
    "constant",
    "gapped",
    "short",
    "provider",
    "missing",
)


def _file_node(name: str, path: Path) -> pipeline.ArtifactNode:
    return pipeline.ArtifactNode(name, (), path, *_hash(path))


def _generation_nodes(
    staged: dict[str, object], result_name: str, extra: list[tuple[str, Path]]
) -> tuple[Any, ...]:
    candidate = Path(str(staged["candidate"]))
    files = tuple(
        pipeline.FileExpectation(path.name, *_hash(path))
        for path in sorted(candidate.iterdir())
        if path.is_file()
    )
    generation = pipeline.GenerationNode(
        "generation",
        (),
        "analytics",
        str(staged["generation_id"]),
        _hash(candidate / "generation.json")[0],
        files,
        candidate,
        False,
    )
    artifact = pipeline.ArtifactNode(
        "selected-result",
        (generation.node_id,),
        candidate / result_name,
        *_hash(candidate / result_name),
    )
    return (generation, artifact, *(_file_node(n, p) for n, p in extra))


def _decomposition_case(tmp_path: Path) -> tuple[tuple[Any, ...], Path, str]:
    source_root = tmp_path / "dec-source"
    target_path, days, _truth = _write_dec_sources(source_root)
    spec_path = _dec_spec(
        source_root,
        target_path,
        days,
        models=[_dec_model(days), _dec_dynamic_model(days)],
    )
    staged = decomposition.stage_candidate(spec_path, source_root, tmp_path / "dec-out")
    nodes = _generation_nodes(
        staged,
        "decomposition.json",
        [
            ("spec", spec_path),
            ("target", target_path),
            ("factor", source_root / "factors.csv"),
        ],
    )
    return nodes, source_root, "DECOMPOSITION"


def _dependence_case(tmp_path: Path) -> tuple[tuple[Any, ...], Path, str]:
    source_root = tmp_path / "cor-source"
    returns_path, days = _write_cor_returns(source_root)
    spec_path = _cor_spec(source_root, returns_path, days)
    staged = correlation.stage_candidate(spec_path, source_root, tmp_path / "cor-out")
    nodes = _generation_nodes(
        staged,
        "correlation.json",
        [("spec", spec_path), ("returns", returns_path)],
    )
    return nodes, source_root, "DEPENDENCE"


def _verify(
    case: tuple[tuple[Any, ...], Path, str], state_root: Path, caller: str
) -> dict[str, int]:
    nodes, source_root, kind = case
    invocation = SimpleNamespace(
        nodes=nodes, roots={"analytics": nodes[0].source_dir}, state_root=state_root
    )
    with outcomes._verification_ledger(invocation, caller) as (counters, calls):
        outcomes._replay_model(
            invocation,
            nodes[1],
            kind,
            source_root=source_root,
            work_counters=counters,
            calls=calls,
        )
    return counters


def _entries(state_root: Path) -> list[dict[str, Any]]:
    directory = state_root / LEDGER
    names = sorted(path.name for path in directory.glob("*.json"))
    assert all(LEDGER_NAME.fullmatch(name) for name in names), names
    return [json.loads((directory / name).read_bytes()) for name in names]


def _receipt_names(state_root: Path) -> list[str]:
    return sorted(path.name for path in (state_root / RECEIPTS).glob("*.json"))


@pytest.mark.parametrize("builder", [_decomposition_case, _dependence_case])
def test_cold_then_warm_then_other_store_counters_and_ledger(
    tmp_path: Path, builder: Any
) -> None:
    """M7: cold rederives; the same store and an overlay of it reuse; an empty one rederives."""

    case = builder(tmp_path)
    kind = case[2]
    s1 = tmp_path / "state-1"

    cold = _verify(case, s1, "test.cold")
    assert cold["full_rederivation_count"] == 1
    assert cold["receipt_reuse_count"] == 0
    first = _entries(s1)
    assert len(first) == 1
    entry = first[0]
    assert entry["caller"] == "test.cold"
    assert entry["schema_version"] == 1
    assert entry["pid"] > 0
    assert entry["work_counters"] == cold
    (call,) = entry["calls"]
    assert call["outcome"] == "REDERIVED"
    assert call["kind"] == kind
    assert call["node_ids"] == ["selected-result"]
    assert re.fullmatch(r"[0-9a-f]{64}", call["calculation_identity"])
    split = entry["model_fit_calls_by_estimator"]
    assert split["static"] + split["dynamic"] == cold["model_fit_calls"]
    assert call["model_fit_calls_static"] == split["static"]
    assert call["model_fit_calls_dynamic"] == split["dynamic"]
    assert call["grid_search_calls"] == cold["grid_search_calls"]
    assert call["correlation_build_calls"] == cold["correlation_build_calls"]
    if kind == "DECOMPOSITION":
        assert split["static"] > 0
        assert split["dynamic"] > 0
        assert cold["grid_search_calls"] == 2
        assert cold["correlation_build_calls"] == 0
    else:
        assert cold["correlation_build_calls"] == 1
        assert cold["model_fit_calls"] == 0
    receipts = _receipt_names(s1)
    assert receipts == [f"{call['calculation_identity']}.json"]

    warm = _verify(case, s1, "test.warm")
    assert {key: warm[key] for key in ZERO} == ZERO
    assert warm["receipt_reuse_count"] == 1
    assert _receipt_names(s1) == receipts
    second = _entries(s1)[-1]
    assert second["caller"] == "test.warm"
    assert [row["outcome"] for row in second["calls"]] == ["RECEIPT_REUSED"]
    assert second["calls"][0]["calculation_identity"] == call["calculation_identity"]
    assert second["model_fit_calls_by_estimator"] == {"static": 0, "dynamic": 0}

    s2 = tmp_path / "state-2"
    create_cache_overlay(s1, s2, run_root=tmp_path / "run-2")
    other = _verify(case, s2, "test.other-root")
    assert {key: other[key] for key in ZERO} == ZERO
    assert other["receipt_reuse_count"] == 1
    assert _receipt_names(s2) == receipts

    s3 = tmp_path / "state-3"
    empty = _verify(case, s3, "test.empty-store")
    assert empty == cold
    assert [row["outcome"] for row in _entries(s3)[0]["calls"]] == ["REDERIVED"]


def test_ledger_is_written_when_the_verification_fails(tmp_path: Path) -> None:
    spec = SimpleNamespace(state_root=tmp_path / "state", nodes=())
    with pytest.raises(RuntimeError, match="boom"):
        with outcomes._verification_ledger(spec, "test.failing") as (counters, _calls):
            counters["full_rederivation_count"] += 1
            raise RuntimeError("boom")
    (entry,) = _entries(tmp_path / "state")
    assert entry["caller"] == "test.failing"
    assert entry["work_counters"]["full_rederivation_count"] == 1
    assert entry["calls"] == []


@pytest.mark.parametrize("caller", ["", "a/b", "a b", "x" * 65, "..\\evil"])
def test_ledger_rejects_a_caller_that_is_not_a_file_name_label(
    tmp_path: Path, caller: str
) -> None:
    spec = SimpleNamespace(state_root=tmp_path / "state", nodes=())
    with pytest.raises(outcomes.FinancialOutcomeError, match="caller label"):
        with outcomes._verification_ledger(spec, caller):
            raise AssertionError("body must not run")
    assert not (tmp_path / "state").exists()


def test_two_calls_in_one_microsecond_keep_separate_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = outcomes.datetime(2026, 10, 7, 12, 0, 0, 123456, tzinfo=outcomes.UTC)

    class Clock(outcomes.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return frozen

    monkeypatch.setattr(outcomes, "datetime", Clock)
    spec = SimpleNamespace(state_root=tmp_path / "state", nodes=())
    for _ in range(2):
        with outcomes._verification_ledger(spec, "test.same-instant"):
            pass
    assert len(_entries(tmp_path / "state")) == 2


def test_semantic_identity_still_ignores_counters() -> None:
    proof = {"status": "PASS", "work_counters": {"model_fit_calls": 9}}
    other = {"status": "PASS", "work_counters": {"model_fit_calls": 0}}
    assert outcomes.financial_outcomes_semantic_identity(
        proof
    ) == outcomes.financial_outcomes_semantic_identity(other)

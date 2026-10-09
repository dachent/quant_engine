"""Z3b: the attended numerical producers run under a counting wrapper.

The producers only emit a `reused` label. These tests run the real producers
through the real wrapper and the real release-builder launcher, so a count that
reads zero can only mean no numerical work ran.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
WRAPPER = TOOLS / "portfolio_counted_producer.py"
ZERO = {
    "build_calls": 0,
    "correlation_build_calls": 0,
    "grid_search_calls": 0,
    "model_fit_calls_dynamic": 0,
    "model_fit_calls_static": 0,
}


def _load(name: str, path: Path) -> ModuleType:
    """Load a module under a private name without replacing a registry entry."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load(
    "portfolio_release_builder_under_z3b", TOOLS / "portfolio_release_builder.py"
)


def _wrapper() -> ModuleType:
    return _load("portfolio_counted_producer_under_test", WRAPPER)


def _days(count: int) -> list[date]:
    accepted: list[date] = []
    current = date(2025, 1, 2)
    while len(accepted) < count:
        if current.weekday() < 5:
            accepted.append(current)
        current += timedelta(days=1)
    return accepted


def _hash(path: Path) -> tuple[str, int]:
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def _decomposition_inputs(root: Path) -> tuple[Path, Path]:
    """One static ridge model over a synthetic target and two factors."""

    root.mkdir(parents=True)
    days = _days(140)
    with (root / "targets.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "series_id",
                "date",
                "return",
                "evidence_layer",
                "return_basis",
                "return_type",
                "cost",
            ]
        )
        for index, day in enumerate(days):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = 0.006 * math.cos(index / 7.0)
            excess = 0.0001 + 0.6 * f1 - 0.2 * f2
            writer.writerow(
                [
                    "synthetic-strategy",
                    day.isoformat(),
                    format(excess + 0.00005, ".17g"),
                    "ACCOUNT_LIVE",
                    "NET",
                    "TOTAL",
                    "0.00001",
                ]
            )
    with (root / "factors.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "RF", "F1", "F2"])
        for index, day in enumerate(days):
            f1 = 0.008 * math.sin(index / 5.0) + 0.002 * math.cos(index / 11.0)
            f2 = 0.006 * math.cos(index / 7.0)
            writer.writerow(
                [day.isoformat(), "0.00005", format(f1, ".17g"), format(f2, ".17g")]
            )
    target_hash, target_size = _hash(root / "targets.csv")
    factor_hash, factor_size = _hash(root / "factors.csv")
    spec = {
        "as_of_date": max(days).isoformat(),
        "factor_source": {
            "artifact_id": "frozen/factors.csv",
            "date_column": "date",
            "expected_sha256": factor_hash,
            "expected_size_bytes": factor_size,
            "factors": [
                {"column": "F1", "factor_id": "factor-one", "return_type": "EXCESS"},
                {"column": "F2", "factor_id": "factor-two", "return_type": "EXCESS"},
            ],
            "interval_policy": {
                "available_through_anchor": max(days).isoformat(),
                "currency": "USD",
                "finality": "COMPLETE_SELECTED_SNAPSHOT",
                "native_calendar": "SOURCE_LABELLED_DAILY",
                "native_label_convention": "PUBLISHED_DAILY_LABEL",
                "return_basis": "NET",
                "source_vintage": "SELECTED_SNAPSHOT",
            },
            "relative_path": "factors.csv",
            "risk_free_column": "RF",
            "schema_name": "synthetic-factor-returns",
            "schema_version": "1",
        },
        "maximum_factor_staleness_days": 7,
        "models": [
            {
                "constraint": "UNCONSTRAINED",
                "cost_coverage": "COMPLETE",
                "cv_folds": 3,
                "factor_ids": ["factor-one", "factor-two"],
                "minimum_test_observations": 20,
                "minimum_train_observations": 40,
                "model_id": "model-two-factor",
                "parsimony_tolerance": "0.000001",
                "periods_per_year": 252,
                "ridge_candidates": ["0", "0.000001", "0.0001"],
                "series_id": "synthetic-strategy",
                "test_start": days[90].isoformat(),
                "train_end": days[89].isoformat(),
            }
        ],
        "schema_version": 1,
        "target_source": {
            "artifact_id": "frozen/targets.csv",
            "expected_sha256": target_hash,
            "expected_size_bytes": target_size,
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
                    "synthetic-strategy": [item.isoformat() for item in days]
                },
                "source_vintage": "SELECTED_SNAPSHOT",
            },
            "relative_path": "targets.csv",
            "schema_name": "synthetic-target-returns",
            "schema_version": "1",
        },
    }
    _write_json(root / "spec.json", spec)
    return root / "spec.json", root


_SERIES = (
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


def _correlation_inputs(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    days = _days(120)
    with (root / "returns.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["series_id", "date", "return", "evidence_layer"])
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
                writer.writerow(
                    [series_id, day.isoformat(), format(value, ".17g"), layer]
                )
            if index < 10:
                writer.writerow(
                    ["short", day.isoformat(), format(base, ".17g"), "ACCOUNT_LIVE"]
                )
    source_hash, source_size = _hash(root / "returns.csv")
    spec = {
        "as_of_date": days[-1].isoformat(),
        "conceptual_priors": [
            {
                "evidence_note": "SYNTHETIC conceptual prior",
                "left": min(left, right),
                "right": max(left, right),
                "value": "0.2",
            }
            for index, left in enumerate(_SERIES)
            for right in _SERIES[index + 1 :]
        ],
        "correlation_one": {
            "components": [
                {
                    "adverse_loss_fraction": "0.3",
                    "allocation_weight": format(1.0 / len(_SERIES), ".17g"),
                    "series_id": series_id,
                }
                for series_id in _SERIES
            ],
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
        "series_ids": list(_SERIES),
        "stress_cap": "0.95",
        "stress_uplift": "0.15",
        "tail_quantile": "0.1",
    }
    _write_json(root / "spec.json", spec)
    return root / "spec.json", root


def _args(
    operation: str, spec: Path, source: Path, output: Path, cache: Path
) -> tuple[str, ...]:
    return (
        operation,
        "--spec",
        str(spec),
        "--source-root",
        str(source),
        "--output-root",
        str(output),
        "--retained-cache-root",
        str(cache),
    )


def _counters(result: object) -> dict[str, int]:
    counters = getattr(result, "work_counters", None)
    assert isinstance(counters, dict)
    return dict(counters)


def test_decomposition_cold_stage_counts_fits_and_warm_restage_counts_zero(
    tmp_path: Path,
) -> None:
    """GOOD: the cold stage fits; the same inputs from a second output root do not."""

    spec, source = _decomposition_inputs(tmp_path / "source")
    cache = tmp_path / "cache"

    cold = builder._tool(
        "portfolio_decomposition.py",
        *_args("stage", spec, source, tmp_path / "out-1", cache),
    )
    warm = builder._tool(
        "portfolio_decomposition.py",
        *_args("stage", spec, source, tmp_path / "out-2", cache),
    )

    assert cold["reused"] is False
    cold_counts = _counters(cold)
    assert cold_counts["build_calls"] == 1
    assert cold_counts["grid_search_calls"] == 1
    assert cold_counts["model_fit_calls_static"] > 0
    assert cold_counts["model_fit_calls_dynamic"] == 0
    assert cold_counts["correlation_build_calls"] == 0
    assert warm["reused"] is True
    assert _counters(warm) == ZERO
    assert warm["generation_id"] == cold["generation_id"]


def test_correlation_cold_stage_counts_one_build_and_warm_restage_counts_zero(
    tmp_path: Path,
) -> None:
    spec, source = _correlation_inputs(tmp_path / "source")
    cache = tmp_path / "cache"

    cold = builder._tool(
        "portfolio_correlation.py",
        *_args("stage", spec, source, tmp_path / "out-1", cache),
    )
    warm = builder._tool(
        "portfolio_correlation.py",
        *_args("stage", spec, source, tmp_path / "out-2", cache),
    )

    assert _counters(cold) == {**ZERO, "build_calls": 1, "correlation_build_calls": 1}
    assert warm["reused"] is True
    assert _counters(warm) == ZERO


def test_counters_catch_a_rebuild_whose_bytes_match(tmp_path: Path) -> None:
    """BAD control: the index entry is gone, the generation is kept.

    The rebuild reproduces the same generation id and bytes, so no byte count can
    tell it from a reuse; the counters must.
    """

    spec, source = _decomposition_inputs(tmp_path / "source")
    cache = tmp_path / "cache"
    cold = builder._tool(
        "portfolio_decomposition.py",
        *_args("stage", spec, source, tmp_path / "out-1", cache),
    )
    candidate = Path(str(cold["candidate"]))
    before = (candidate / "decomposition.json").read_bytes()
    shutil.rmtree(cache / ".decomposition-index")

    rebuilt = builder._tool(
        "portfolio_decomposition.py",
        *_args("stage", spec, source, tmp_path / "out-2", cache),
    )

    assert rebuilt["generation_id"] == cold["generation_id"]
    assert (candidate / "decomposition.json").read_bytes() == before
    assert rebuilt["reused"] is False
    rebuilt_counts = _counters(rebuilt)
    assert rebuilt_counts["build_calls"] == 1
    assert rebuilt_counts["model_fit_calls_static"] > 0
    assert builder._numerical_producer_evidence_total([rebuilt_counts]) != ZERO


@pytest.mark.parametrize(
    ("producer", "inputs"),
    [
        ("portfolio_decomposition.py", _decomposition_inputs),
        ("portfolio_correlation.py", _correlation_inputs),
    ],
)
def test_wrapper_forwards_producer_stdout_byte_for_byte(
    tmp_path: Path, producer: str, inputs: Any
) -> None:
    spec, source = inputs(tmp_path / "source")
    cache = tmp_path / "cache"
    output = tmp_path / "out"
    arguments = _args("stage", spec, source, output, cache)

    bare = subprocess.run(
        [sys.executable, str(TOOLS / producer), *arguments],
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    shutil.rmtree(cache)
    shutil.rmtree(output, ignore_errors=True)
    counted = subprocess.run(
        [sys.executable, str(WRAPPER), producer, *arguments],
        capture_output=True,
        check=True,
        cwd=ROOT,
        env={
            **os.environ,
            "QE_NUMERICAL_WORK_COUNTERS": str(tmp_path / "counters.json"),
        },
    )

    assert counted.stdout == bare.stdout
    assert counted.stderr == bare.stderr


def test_wrapper_counts_a_known_number_of_calls(tmp_path: Path) -> None:
    """GOOD: three static fits, two dynamic fits and one four-model build."""

    wrapper = _wrapper()
    stub = ModuleType("portfolio_decomposition")
    stub._fit = lambda *_a, **_k: "static"  # type: ignore[attr-defined]
    stub._dynamic_fit = lambda *_a, **_k: "dynamic"  # type: ignore[attr-defined]
    stub._load_spec = lambda _path: SimpleNamespace(models=(1, 2, 3, 4))  # type: ignore[attr-defined]

    def build(_spec: Path, *_a: object, **_k: object) -> str:
        for _ in range(3):
            stub._fit()  # type: ignore[attr-defined]
        for _ in range(2):
            stub._dynamic_fit()  # type: ignore[attr-defined]
        return "built"

    def main(_argv: list[str]) -> int:
        stub.build_decomposition(tmp_path / "spec.json")  # type: ignore[attr-defined]
        return 0

    stub.build_decomposition = build  # type: ignore[attr-defined]
    stub.main = main  # type: ignore[attr-defined]
    destination = tmp_path / "counters.json"

    assert (
        wrapper.run_counted("portfolio_decomposition.py", [], destination, module=stub)
        == 0
    )

    assert wrapper.read_work_counters(
        destination, producer="portfolio_decomposition.py"
    ) == {
        "build_calls": 1,
        "correlation_build_calls": 0,
        "grid_search_calls": 4,
        "model_fit_calls_dynamic": 2,
        "model_fit_calls_static": 3,
    }
    # The producer's own functions are restored once the run ends.
    assert stub._fit() == "static"  # type: ignore[attr-defined]


def test_producer_launched_around_the_wrapper_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BAD: a producer started without the wrapper leaves no counters."""

    spec, source = _correlation_inputs(tmp_path / "source")
    monkeypatch.setattr(
        builder,
        "_counted_command",
        lambda name, args: (sys.executable, str(TOOLS / name), *args),
    )

    with pytest.raises(
        builder.VerticalBuildError, match="bypassed the counting wrapper"
    ):
        builder._tool(
            "portfolio_correlation.py",
            *_args("stage", spec, source, tmp_path / "out", tmp_path / "cache"),
        )


def test_recomputed_label_without_counted_work_is_refused(tmp_path: Path) -> None:
    """BAD: a producer that rebuilds through code the wrapper does not count."""

    wrapper = _wrapper()
    stub = ModuleType("portfolio_correlation")
    stub.build_correlation = lambda *_a, **_k: None  # type: ignore[attr-defined]
    stub.main = lambda _argv: 0  # type: ignore[attr-defined]
    destination = tmp_path / "counters.json"
    wrapper.run_counted("portfolio_correlation.py", [], destination, module=stub)
    counters = wrapper.read_work_counters(
        destination, producer="portfolio_correlation.py"
    )

    with pytest.raises(
        wrapper.CountedProducerError, match="bypassed the counting wrapper"
    ):
        wrapper.reconcile_reuse_label(
            counters, False, producer="portfolio_correlation.py", operation="stage"
        )
    wrapper.reconcile_reuse_label(
        counters, True, producer="portfolio_correlation.py", operation="stage"
    )


def test_reuse_label_over_counted_work_is_refused() -> None:
    wrapper = _wrapper()
    with pytest.raises(wrapper.CountedProducerError, match="reported reuse"):
        wrapper.reconcile_reuse_label(
            {**ZERO, "build_calls": 1},
            True,
            producer="portfolio_decomposition.py",
            operation="publish",
        )


def test_counter_file_for_another_producer_is_refused(tmp_path: Path) -> None:
    wrapper = _wrapper()
    stub = ModuleType("portfolio_correlation")
    stub.build_correlation = lambda *_a, **_k: None  # type: ignore[attr-defined]
    stub.main = lambda _argv: 0  # type: ignore[attr-defined]
    destination = tmp_path / "counters.json"
    wrapper.run_counted("portfolio_correlation.py", [], destination, module=stub)

    with pytest.raises(wrapper.CountedProducerError, match="another producer"):
        wrapper.read_work_counters(destination, producer="portfolio_decomposition.py")


def test_stage_evidence_records_counters_per_operation_and_in_total(
    tmp_path: Path,
) -> None:
    """The stage result carries what ran, per operation and summed."""

    spec, source = _correlation_inputs(tmp_path / "source")
    cache = tmp_path / "cache"
    operations: list[dict[str, Any]] = []
    for index in range(2):
        output = tmp_path / f"out-{index}"
        result = builder._tool(
            "portfolio_correlation.py",
            *_args("stage", spec, source, output, cache),
        )
        builder._record_numerical_producer_operation(
            operations,
            tool="portfolio_correlation.py",
            operation="stage",
            spec_path=spec,
            source_root=source,
            output_root=output,
            producer_result=result,
        )

    evidence = builder._numerical_producer_evidence(operations)

    assert [row["work_counters"]["build_calls"] for row in operations] == [1, 0]
    assert evidence["work_counters"] == {
        **ZERO,
        "build_calls": 1,
        "correlation_build_calls": 1,
    }
    assert evidence["work_counters_status"] == "COUNTED"
    assert json.dumps(evidence)


def test_unmeasured_operation_is_never_reported_as_zero_work(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    operations: list[dict[str, Any]] = []
    builder._record_numerical_producer_operation(
        operations,
        tool="portfolio_correlation.py",
        operation="stage",
        spec_path=spec,
        source_root=tmp_path,
        output_root=tmp_path,
        producer_result={"generation_id": "g", "reused": True},
    )

    evidence = builder._numerical_producer_evidence(operations)

    assert operations[0]["work_counters"] is None
    assert evidence["work_counters"] is None
    assert evidence["work_counters_status"] == "UNMEASURED"
    assert evidence["warm_status"] == "ALL_REUSED"


def test_every_numerical_producer_launch_in_the_builder_goes_through_tool() -> None:
    """Static guard: no builder call site starts a counted producer directly."""

    source = (TOOLS / "portfolio_release_builder.py").read_text(encoding="utf-8")
    for producer in ("portfolio_decomposition.py", "portfolio_correlation.py"):
        launches = [
            line
            for line in source.splitlines()
            if f'"{producer}"' in line and "_tool(" not in line
        ]
        # Every mention is either a `_tool(` first argument (next line) or a
        # record's `tool=` label; none builds a subprocess command by hand.
        assert not [line for line in launches if "sys.executable" in line]
        assert f'TOOLS_ROOT / "{producer}"' not in source

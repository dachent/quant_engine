from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, cast

import numpy as np
import pytest

from quant_engine.core import read_current


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_correlation.py"
SPEC = importlib.util.spec_from_file_location("portfolio_correlation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio correlation builder")
correlation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = correlation
SPEC.loader.exec_module(correlation)

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


def test_correlation_replay_identity_binds_consumed_helper_and_contract_bytes() -> None:
    """A receipt must change with the source bytes its replay actually executes."""

    import portfolio_financial_outcomes as outcomes

    identity = outcomes._correlation_consumed_producer_identity(correlation)
    expected_paths = {
        "core_contracts": Path(
            correlation.canonical_json_bytes.__code__.co_filename
        ).resolve(),
        "decimal_text": Path(correlation.decimal_text.__code__.co_filename).resolve(),
        "producer": Path(cast(str, correlation.__file__)).resolve(),
    }
    lock = Path(cast(str, correlation.__file__)).resolve().parents[1] / "uv.lock"
    expected_paths["dependency_lock"] = lock

    assert identity == {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(expected_paths.items())
    }


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


def _write_returns(root: Path, *, mixed_layer: bool = False) -> tuple[Path, list[date]]:
    root.mkdir(parents=True, exist_ok=True)
    days = _days(120)
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


def _spec(root: Path, source: Path, days: list[date]) -> Path:
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


def _dependence(result: dict[str, object]) -> dict[str, Any]:
    return cast(dict[str, Any], result["dependence"])


def _cell(result: dict[str, object], left: str, right: str) -> dict[str, Any]:
    expected = {left, right}
    return next(
        item
        for item in cast(list[dict[str, Any]], _dependence(result)["observed_cells"])
        if {item["left"], item["right"]} == expected
    )


def _matrix(result: dict[str, object], name: str) -> np.ndarray:
    values = _dependence(result)["matrices"][name]["values"]
    return np.asarray([[float(item) for item in row] for row in values], dtype=float)


def test_observed_dependence_coverage_and_no_credit_are_explicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    source, days = _write_returns(root)
    spec = _spec(root, source, days)
    result, _manifest, _content = correlation.build_correlation(spec, root)

    identical = _cell(result, "base", "identical")
    inverse = _cell(result, "base", "inverse")
    independent = _cell(result, "base", "independent")
    constant = _cell(result, "base", "constant")
    gapped = _cell(result, "base", "gapped")
    short = _cell(result, "base", "short")
    provider = _cell(result, "base", "provider")
    missing = _cell(result, "base", "missing")

    assert float(identical["pearson"]) == pytest.approx(1.0, abs=1e-12)
    assert float(identical["spearman"]) == pytest.approx(1.0, abs=1e-12)
    assert float(inverse["pearson"]) == pytest.approx(-1.0, abs=1e-12)
    assert abs(float(independent["pearson"])) < 0.2
    assert constant["status"] == "UNAVAILABLE_CONSTANT_SERIES"
    assert short["status"] == "UNAVAILABLE_INSUFFICIENT_COMMON_HISTORY"
    assert provider["status"] == "UNAVAILABLE_INCOMPATIBLE_EVIDENCE_LAYERS"
    assert missing["status"] == "UNAVAILABLE_MISSING_SERIES"
    assert gapped["coverage"]["common_observations"] == 60
    assert gapped["coverage"]["missing_from_right"] == 60
    assert gapped["rolling_pearson"]["status"] == "AVAILABLE"
    assert identical["downside"]["status"] == "AVAILABLE"
    assert identical["coverage"]["frequency"] == "WEEKDAY_OBSERVED_EXACT_DATE"
    assert identical["horizons"]["5"]["status"] == "AVAILABLE"
    assert identical["horizons"]["5"]["observations"] == 5
    assert identical["horizons"]["126"]["status"] == (
        "UNAVAILABLE_INSUFFICIENT_HISTORY"
    )
    assert (
        identical["drawdown_regimes"]["either_strategy_underwater"]["conditioning_rule"]
        == "EITHER_WEALTH_INDEX_BELOW_PRIOR_RUNNING_HWM"
    )
    assert (
        identical["drawdown_regimes"]["either_strategy_underwater"]["status"]
        == "AVAILABLE"
    )
    assert identical["joint_tail"]["multiple_vs_independence"] is not None

    five_day = _dependence(result)["horizon_matrices"]["5"]
    base_index = SERIES.index("base")
    identical_index = SERIES.index("identical")
    assert float(five_day["values"][base_index][identical_index]) == pytest.approx(1)

    blend = next(
        item
        for item in _dependence(result)["blend_cells"]
        if {item["left"], item["right"]} == {"base", "missing"}
    )
    assert blend["decision_correlation"] == "1"
    assert blend["decision_status"] == "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION"
    assert _dependence(result)["correlation_one"]["loss"] == "0.3"
    assert (
        _dependence(result)["correlation_one"]["status"]
        == "AVAILABLE_RECONCILED_SEPARATE_CONSERVATIVE_BOUND"
    )
    corr_one = _dependence(result)["correlation_one"]
    assert corr_one["declared_loss"] == "0.3"
    assert corr_one["governs_readiness"] is True
    assert all(
        component["classification"] == "CONCEPTUAL_STRESS_BOUND_NOT_EXPECTATION"
        and component["conservative_loss_fraction"] == "0.3"
        for component in corr_one["components"]
    )

    central = _dependence(result)["central_case"]
    assert central["governs_readiness"] is False
    assert central["status"] == "AVAILABLE_NOT_A_PROBABILITY_FORECAST"
    assert central["measured_component_count"] == 8
    assert central["prior_component_count"] == 1
    assert central["measured_pair_count"] + central["prior_pair_count"] == 36
    missing_component = next(
        item for item in central["components"] if item["series_id"] == "missing"
    )
    base_component = next(
        item for item in central["components"] if item["series_id"] == "base"
    )
    assert base_component["evidence_layer"] == "ACCOUNT_LIVE"
    assert base_component["method"] == "ACCEPTED_PATH_MAXIMUM_DRAWDOWN"
    assert missing_component["status"] == "PRIOR_NO_ACCEPTED_OBSERVATIONS"
    assert missing_component["authority"].endswith("correlation_one.components")
    assert (
        central["dependence_matrix"]["missing_pair_rule"] == "GOVERNED_CONCEPTUAL_PRIOR"
    )
    base_index = SERIES.index("base")
    missing_index = SERIES.index("missing")
    assert float(
        central["dependence_matrix"]["input_values"][base_index][missing_index]
    ) == pytest.approx(0.2)
    central_values = np.asarray(
        [
            [float(item) for item in row]
            for row in central["dependence_matrix"]["values"]
        ]
    )
    central_vector = np.asarray(
        [float(item["loss_scale_contribution"]) for item in central["components"]]
    )
    assert float(central["loss"]) == pytest.approx(
        math.sqrt(float(central_vector @ central_values @ central_vector)), abs=1e-12
    )
    assert float(np.min(np.linalg.eigvalsh(central_values))) >= -1e-10


def test_matrices_are_symmetric_and_posterior_stress_are_psd(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source, days = _write_returns(root)
    spec = _spec(root, source, days)
    result, _manifest, _content = correlation.build_correlation(spec, root)

    for name in ("conceptual_prior", "posterior", "stress"):
        matrix = _matrix(result, name)
        assert matrix == pytest.approx(matrix.T, abs=1e-12)
        assert np.diag(matrix) == pytest.approx(np.ones(len(SERIES)), abs=1e-12)
    for name in ("posterior", "stress"):
        matrix = _matrix(result, name)
        assert float(np.min(np.linalg.eigvalsh(matrix))) >= -1e-10
        projection = _dependence(result)["matrices"][name]["projection"]
        assert float(projection["minimum_eigenvalue_post_projection"]) >= -1e-10

    mapping = next(
        item
        for item in _dependence(result)["implementation_cells"]
        if {item["left"], item["right"]} == {"base", "identical"}
    )
    assert mapping["shared_instruments"] == ["SYNTH-A"]
    assert mapping["shared_providers"] == ["SYNTH-PROVIDER"]
    assert mapping["shared_venues"] == ["SYNTH-VENUE"]


def test_build_stage_publish_are_deterministic_and_fail_on_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    source, days = _write_returns(root)
    spec = _spec(root, source, days)
    first, first_manifest, first_content = correlation.build_correlation(spec, root)
    second, second_manifest, second_content = correlation.build_correlation(spec, root)
    assert first_content == second_content
    assert first_manifest.generation_id == second_manifest.generation_id
    assert first["analytics_hash"] == second["analytics_hash"]

    output = tmp_path / "output"
    staged = correlation.stage_candidate(spec, root, output)
    published = correlation.publish(spec, root, output)
    assert published["publication_state"] == "PUBLISHED"
    assert read_current(output).generation_id == staged["generation_id"]

    candidate = Path(cast(str, staged["candidate"])) / "correlation.json"
    candidate.write_text("{}\n", encoding="utf-8")
    with pytest.raises(correlation.CorrelationError, match="exact build"):
        correlation.stage_candidate(spec, root, output)


def test_legacy_publish_without_staged_candidate_rejects_without_output(
    tmp_path: Path,
) -> None:
    """Legacy publication preserves its separate, verified staging boundary."""

    source_root = tmp_path / "source"
    source, days = _write_returns(source_root)
    spec = _spec(source_root, source, days)
    output_root = tmp_path / "output"

    with pytest.raises(correlation.CorrelationError, match="candidate"):
        correlation.publish(spec, source_root, output_root)

    assert not (output_root / "CURRENT.json").exists()
    assert not (output_root / "generations").exists()


def test_retained_cache_reuses_one_correlation_generation_across_output_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing retained candidate reuse would rebuild unchanged second-root work."""

    source_root = tmp_path / "source"
    source, days = _write_returns(source_root)
    spec = _spec(source_root, source, days)
    retained_root = tmp_path / "retained"
    cold_root = tmp_path / "cold"
    warm_root = tmp_path / "warm"
    calls = 0
    real_build = correlation.build_correlation

    def counted_build(
        spec_path: Path, input_root: Path
    ) -> tuple[dict[str, object], object, bytes]:
        nonlocal calls
        calls += 1
        return real_build(spec_path, input_root)

    monkeypatch.setattr(correlation, "build_correlation", counted_build)

    cold_stage = correlation.stage_candidate(
        spec, source_root, cold_root, retained_cache_root=retained_root
    )
    cold_publish = correlation.publish(
        spec, source_root, cold_root, retained_cache_root=retained_root
    )
    warm_stage = correlation.stage_candidate(
        spec, source_root, warm_root, retained_cache_root=retained_root
    )
    warm_publish = correlation.publish(
        spec, source_root, warm_root, retained_cache_root=retained_root
    )

    assert calls == 1
    assert cold_stage["reused"] is False
    assert cold_publish["reused"] is True
    assert warm_stage["reused"] is True
    assert warm_publish["reused"] is True
    assert (
        read_current(cold_root).generation_id == read_current(warm_root).generation_id
    )
    cold_generation = cold_root / "generations" / str(cold_publish["generation_id"])
    warm_generation = warm_root / "generations" / str(warm_publish["generation_id"])
    assert (cold_generation / "correlation.json").read_bytes() == (
        warm_generation / "correlation.json"
    ).read_bytes()
    assert (cold_generation / "generation.json").read_bytes() == (
        warm_generation / "generation.json"
    ).read_bytes()


def test_retained_cache_validates_sources_before_hit_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    """A stale source or altered retained generation must never be served."""

    source_root = tmp_path / "source"
    source, days = _write_returns(source_root)
    spec = _spec(source_root, source, days)
    retained_root = tmp_path / "retained"
    staged = correlation.stage_candidate(
        spec, source_root, tmp_path / "cold", retained_cache_root=retained_root
    )

    source_bytes = source.read_bytes()
    source.write_bytes(source_bytes + b"\n")
    with pytest.raises(correlation.CorrelationError, match="hash or size differs"):
        correlation.stage_candidate(
            spec,
            source_root,
            tmp_path / "stale-source",
            retained_cache_root=retained_root,
        )

    source.write_bytes(source_bytes)
    candidate = Path(str(staged["candidate"])) / "correlation.json"
    candidate.write_bytes(b"{}\n")
    with pytest.raises(correlation.CorrelationError, match="exact build"):
        correlation.stage_candidate(
            spec, source_root, tmp_path / "corrupt", retained_cache_root=retained_root
        )


def test_correlation_reuse_identity_binds_spec_and_consumed_helper_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed requested spec or consumed helper must select a new cache identity."""

    source_root = tmp_path / "source"
    source, days = _write_returns(source_root)
    spec = _spec(source_root, source, days)
    original_key = correlation._input_key(spec, source_root)

    payload = json.loads(spec.read_text(encoding="utf-8"))
    payload["stress_uplift"] = "0.16"
    spec.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    spec_key = correlation._input_key(spec, source_root)
    assert spec_key != original_key

    original_helper = correlation.canonical_json_bytes

    def replacement_helper(value: object) -> bytes:
        return original_helper(value)

    monkeypatch.setattr(correlation, "canonical_json_bytes", replacement_helper)
    assert correlation._input_key(spec, source_root) != spec_key


def test_invalid_priors_stress_and_mixed_layers_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source, days = _write_returns(root)
    spec = _spec(root, source, days)
    decoded = json.loads(spec.read_text(encoding="utf-8"))
    decoded["conceptual_priors"].pop()
    spec.write_text(json.dumps(decoded), encoding="utf-8")
    with pytest.raises(
        correlation.CorrelationError, match="cover every requested pair"
    ):
        correlation.build_correlation(spec, root)

    source, days = _write_returns(root, mixed_layer=True)
    spec = _spec(root, source, days)
    with pytest.raises(correlation.CorrelationError, match="cannot mix evidence"):
        correlation.build_correlation(spec, root)

    clean = tmp_path / "stress"
    clean_source, clean_days = _write_returns(clean)
    clean_spec = _spec(clean, clean_source, clean_days)
    decoded = json.loads(clean_spec.read_text(encoding="utf-8"))
    decoded["correlation_one"]["expected_loss"] = "0.1"
    clean_spec.write_text(json.dumps(decoded), encoding="utf-8")
    with pytest.raises(correlation.CorrelationError, match="does not reconcile"):
        correlation.build_correlation(clean_spec, clean)


def _write_mode_fixture(
    root: Path,
    *,
    backtest_version: str = "v1",
    duplicate_live: bool = False,
    stale_live_series: str | None = None,
) -> tuple[Path, Path, list[date]]:
    root.mkdir(parents=True)
    days = _days(20)
    source = root / "returns.csv"
    rows: list[list[str]] = []
    for series_offset, series_id in enumerate(("a", "b")):
        for index, day in enumerate(days):
            value = 0.002 * math.sin(index / 2.0 + series_offset * 0.1)
            rows.append(
                [
                    series_id,
                    day.isoformat(),
                    format(value, ".17g"),
                    "BACKTEST",
                    backtest_version,
                ]
            )
            if (
                index >= 10
                and not (series_id == "a" and index == 14)
                and not (series_id == stale_live_series and index == len(days) - 1)
            ):
                rows.append(
                    [
                        series_id,
                        day.isoformat(),
                        format(value + 0.0001, ".17g"),
                        "ACCOUNT_LIVE",
                        "v1",
                    ]
                )
    if duplicate_live:
        rows.append(list(rows[-1]))
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(correlation.RETURN_HEADERS_V2)
        writer.writerows(rows)

    source_hash, source_size = _hash(source)
    specification = {
        "as_of_date": days[-1].isoformat(),
        "conceptual_priors": [
            {
                "evidence_note": "SYNTHETIC conceptual prior",
                "left": "a",
                "right": "b",
                "value": "0.2",
            }
        ],
        "correlation_one": {
            "components": [
                {
                    "adverse_loss_fraction": "0.3",
                    "allocation_weight": "0.5",
                    "series_id": series_id,
                }
                for series_id in ("a", "b")
            ],
            "expected_loss": "0.3",
            "tolerance": "0.000000001",
        },
        "eigenvalue_floor": "0.00000001",
        "evidence_mode_series": [
            {
                "live_evidence_layer": "ACCOUNT_LIVE",
                "series_id": series_id,
                "strategy_version": "v1",
            }
            for series_id in ("a", "b")
        ],
        "full_observed_weight_observations": 100,
        "implementation_mappings": [],
        "maximum_observed_weight": "0.4",
        "minimum_downside_observations": 3,
        "minimum_observations": 3,
        "minimum_rolling_windows": 1,
        "return_source": {
            "artifact_id": "synthetic/returns.csv",
            "expected_sha256": source_hash,
            "expected_size_bytes": source_size,
            "relative_path": "returns.csv",
            "schema_name": "synthetic-layered-daily-returns",
            "schema_version": "2",
        },
        "rolling_window": 3,
        "schema_version": 2,
        "series_ids": ["a", "b"],
        "stress_cap": "0.95",
        "stress_uplift": "0.15",
        "tail_quantile": "0.1",
    }
    spec = root / "spec.json"
    spec.write_text(
        json.dumps(specification, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return source, spec, days


def test_live_and_backtest_modes_are_separate_and_live_wins_overlap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mode-source"
    _source, spec, days = _write_mode_fixture(root)
    result, _manifest, _content = correlation.build_correlation(spec, root)

    assert result["default_evidence_mode"] == "LIVE_ONLY"
    modes = cast(dict[str, dict[str, Any]], result["dependence_modes"])
    assert result["dependence"] == modes["LIVE_ONLY"]
    live_composition = modes["LIVE_ONLY"]["series_composition"]["a"]
    combined_composition = modes["LIVE_PLUS_BACKTEST"]["series_composition"]["a"]
    assert live_composition["live_observations"] == 9
    assert live_composition["backtest_observations_used"] == 0
    assert len(live_composition["canonical_live_series_sha256"]) == 64
    assert (
        combined_composition["canonical_live_series_sha256"]
        == live_composition["canonical_live_series_sha256"]
    )
    assert combined_composition["backtest_observations_used"] == 10
    assert (
        combined_composition[
            "backtest_observations_excluded_at_or_after_live_inception"
        ]
        == 10
    )
    assert combined_composition["live_inception"] == days[10].isoformat()
    combined_cell = modes["LIVE_PLUS_BACKTEST"]["observed_cells"][0]
    assert combined_cell["coverage"]["common_observations"] == 19
    assert combined_cell["evidence_mode"] == "LIVE_PLUS_BACKTEST"
    assert (
        combined_cell["left_evidence_composition"]["status"]
        == "AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE"
    )
    live_performance = modes["LIVE_ONLY"]["performance"]["a"]
    combined_performance = modes["LIVE_PLUS_BACKTEST"]["performance"]["a"]
    assert live_performance["return_observations"] == 9
    assert combined_performance["return_observations"] == 19
    assert live_performance["observed_start"] == days[10].isoformat()
    assert combined_performance["observed_start"] == days[0].isoformat()
    assert live_performance["annualization_basis"] == "ACTUAL_DAYS_365_25"
    assert live_performance["cagr"] is not None
    assert combined_performance["maximum_drawdown"] is not None


def test_schema_v2_complete_through_is_common_live_series_end(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mode-source"
    _source, spec, days = _write_mode_fixture(root, stale_live_series="a")

    result, manifest, _content = correlation.build_correlation(spec, root)

    assert result["as_of_date"] == days[-1].isoformat()
    assert result["complete_through"] == days[-2].isoformat()
    assert manifest.coverage.observed_end == days[-2]


def test_live_version_ref_threads_into_dependence_composition(
    tmp_path: Path,
) -> None:
    """A declared live_version_ref reaches both dependence mode compositions;
    the legacy three-key form yields null."""
    root = tmp_path / "mode-source"
    _source, spec, _days = _write_mode_fixture(root)
    payload = json.loads(spec.read_text(encoding="utf-8"))
    for entry in payload["evidence_mode_series"]:
        if entry["series_id"] == "a":
            entry["live_version_ref"] = "accepted-live-path:" + "a" * 64
    spec.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    result, _manifest, _content = correlation.build_correlation(spec, root)
    modes = cast(dict[str, dict[str, Any]], result["dependence_modes"])
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        composition = modes[mode]["series_composition"]
        assert composition["a"]["live_version_ref"] == (
            "accepted-live-path:" + "a" * 64
        )
        # Legacy three-key declaration propagates as null; the panel fails
        # closed on it.
        assert composition["b"]["live_version_ref"] is None


def test_evidence_mode_series_rejects_unknown_and_malformed_keys(
    tmp_path: Path,
) -> None:
    """Typo'd keys and non-text live_version_ref fail closed at parse time."""
    root = tmp_path / "mode-source"
    _source, spec, _days = _write_mode_fixture(root)
    payload = json.loads(spec.read_text(encoding="utf-8"))
    payload["evidence_mode_series"][0]["live_version"] = "accepted-live-path:x"
    spec.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(correlation.CorrelationError, match="keys differ"):
        correlation.build_correlation(spec, root)

    payload = json.loads(spec.read_text(encoding="utf-8"))
    del payload["evidence_mode_series"][0]["live_version"]
    payload["evidence_mode_series"][0]["live_version_ref"] = 123
    spec.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(correlation.CorrelationError, match="live_version_ref"):
        correlation.build_correlation(spec, root)


def test_performance_summary_compounds_and_fails_closed_on_invalid_returns() -> None:
    rows = [
        correlation.ReturnRow(date(2025, 1, 1), 0.1, "BACKTEST", "v1", "0.1"),
        correlation.ReturnRow(date(2026, 1, 1), -0.1, "BACKTEST", "v1", "-0.1"),
    ]
    summary = correlation._performance_summary(rows)
    assert float(cast(str, summary["cumulative_return"])) == pytest.approx(-0.01)
    assert float(cast(str, summary["maximum_drawdown"])) == pytest.approx(-0.1)
    assert float(cast(str, summary["cagr"])) == pytest.approx(-0.0100068, rel=1e-5)

    with pytest.raises(correlation.CorrelationError, match="greater than -1"):
        correlation._performance_summary(
            [correlation.ReturnRow(date(2025, 1, 1), -1.0, "BACKTEST")]
        )


def test_canonical_live_series_digest_detects_value_mutation() -> None:
    original = [
        correlation.ReturnRow(date(2026, 1, 2), 0.01, "ACCOUNT_LIVE", "v1", "0.01")
    ]
    mutated = [
        correlation.ReturnRow(date(2026, 1, 2), 0.02, "ACCOUNT_LIVE", "v1", "0.02")
    ]
    assert correlation._canonical_series_sha256(
        original
    ) != correlation._canonical_series_sha256(mutated)


def test_combined_mode_ignores_unmatched_backtest_and_rejects_duplicate_layer_date(
    tmp_path: Path,
) -> None:
    mismatch_root = tmp_path / "mismatch"
    _source, spec, _days_value = _write_mode_fixture(
        mismatch_root, backtest_version="v0"
    )
    result, _manifest, _content = correlation.build_correlation(spec, mismatch_root)
    modes = cast(dict[str, dict[str, Any]], result["dependence_modes"])
    assert (
        modes["LIVE_PLUS_BACKTEST"]["series_composition"]["a"]["status"]
        == "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
    )
    assert (
        modes["LIVE_PLUS_BACKTEST"]["series_composition"]["a"][
            "backtest_observations_used"
        ]
        == 0
    )
    combined_cell = modes["LIVE_PLUS_BACKTEST"]["observed_cells"][0]
    assert combined_cell["status"] == "AVAILABLE"
    assert combined_cell["coverage"]["common_observations"] == 9

    duplicate_root = tmp_path / "duplicate"
    _source, duplicate_spec, _days_value = _write_mode_fixture(
        duplicate_root, duplicate_live=True
    )
    with pytest.raises(correlation.CorrelationError, match="within an evidence layer"):
        correlation.build_correlation(duplicate_spec, duplicate_root)

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any
import sys

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    read_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_weekly_returns.py"
SPEC = importlib.util.spec_from_file_location("portfolio_weekly_returns", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio weekly-return builder")
weekly = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = weekly
SPEC.loader.exec_module(weekly)
PANEL_SPEC = importlib.util.spec_from_file_location(
    "weekly_test_portfolio_control_panel",
    REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py",
)
if PANEL_SPEC is None or PANEL_SPEC.loader is None:
    raise RuntimeError("unable to load weekly-generation consumer")
panel = importlib.util.module_from_spec(PANEL_SPEC)
sys.modules[PANEL_SPEC.name] = panel
PANEL_SPEC.loader.exec_module(panel)

STRATEGIES = tuple(f"Strategy {index}" for index in range(9))
EXECUTION_DATE = date(2026, 8, 4)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _correlation_fixture(
    tmp_path: Path,
    *,
    source_artifact_id: str = "provider-rollforward/layered-strategy-returns.csv",
    strategy_count: int = 9,
    backtest_date: str = "2026-07-20",
) -> tuple[Path, Path]:
    strategy_returns = tmp_path / "strategy-returns.csv"
    selected_strategies = STRATEGIES[:strategy_count]
    rows: list[dict[str, str]] = []
    for index, strategy in enumerate(selected_strategies):
        version = f"version-{index}"
        rows.extend(
            [
                {
                    "date": "2026-07-27",
                    "evidence_layer": "ACCOUNT_LIVE",
                    "return": "0.10",
                    "series_id": strategy,
                    "strategy_version": version,
                },
                {
                    "date": "2026-07-28",
                    "evidence_layer": "ACCOUNT_LIVE",
                    "return": "-0.05",
                    "series_id": strategy,
                    "strategy_version": version,
                },
            ]
        )
        if index == 0:
            rows.append(
                {
                    "date": backtest_date,
                    "evidence_layer": "BACKTEST",
                    "return": "0.02",
                    "series_id": strategy,
                    "strategy_version": version,
                }
            )
    _write_csv(
        strategy_returns,
        ["series_id", "date", "return", "evidence_layer", "strategy_version"],
        rows,
    )
    live = {}
    combined = {}
    for index, strategy in enumerate(selected_strategies):
        base = {
            "backtest_observations_used": 0,
            "live_inception": "2026-07-27",
            "live_observations": 2,
            "strategy_version": f"version-{index}",
        }
        live[strategy] = dict(base)
        combined[strategy] = {
            **base,
            "backtest_observations_used": 1 if index == 0 else 0,
        }
    correlation = {
        "analytics_schema_version": 2,
        "complete_through": "2026-07-31",
        "default_evidence_mode": "LIVE_ONLY",
        "dependence_modes": {
            "LIVE_ONLY": {"series_composition": live},
            "LIVE_PLUS_BACKTEST": {"series_composition": combined},
        },
        "source": {
            "artifact_id": source_artifact_id,
            "sha256": _sha(strategy_returns),
            "size_bytes": strategy_returns.stat().st_size,
        },
    }
    correlation_source = tmp_path / "correlation.json"
    _write_json(correlation_source, correlation)
    correlation_root = tmp_path / "correlation-root"
    # The generation is dated at the execution clock (2026-08-04) while its data and
    # declared coverage end at the 2026-07-31 cutoff, as a real correlation run does.
    manifest = GenerationManifest.build(
        as_of_date=EXECUTION_DATE,
        adapter=AdapterIdentity("synthetic-correlation", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic layered strategy evidence",
            fact_types=("layered strategy returns",),
            observed_start=date(2026, 7, 20),
            observed_end=date(2026, 7, 31),
        ),
        artifacts=(
            SourceArtifact.from_path(
                strategy_returns,
                artifact_id=source_artifact_id,
                as_of_date=EXECUTION_DATE,
                schema_name="portfolio-layered-strategy-returns",
                schema_version="2",
            ),
            SourceArtifact.from_path(
                correlation_source,
                artifact_id="normalized/correlation.json",
                as_of_date=EXECUTION_DATE,
                schema_name="portfolio-correlation-analytics",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(correlation_root, manifest)
    (manifest_path.parent / "correlation.json").write_bytes(
        correlation_source.read_bytes()
    )
    publish_generation(correlation_root, manifest.generation_id)
    return correlation_root, strategy_returns


def _plan_fixture(
    tmp_path: Path,
    *,
    observation_dates: tuple[str, ...] = ("2026-07-27", "2026-07-28"),
    market_history_dates: tuple[str, ...] = ("2026-07-14", "2026-07-15"),
) -> Path:
    bundle = tmp_path / ("b" * 64)
    sources = {
        "inputs/lenses/us-economic-factors.csv": (
            ["date", "rf", "mkt_rf"],
            {"rf": "0.0001", "mkt_rf": "0.01"},
        ),
        "inputs/lenses/europe-economic-factors.csv": (
            ["date", "rf", "mkt_rf"],
            {"rf": "0.0001", "mkt_rf": "0.02"},
        ),
        "inputs/lenses/us-equity-style-factors.csv": (
            ["date", "BIL", "IWX"],
            {"BIL": "0.0001", "IWX": "0.03"},
        ),
        "inputs/lenses/multi-asset-style-factors.csv": (
            [
                "date",
                "BIL",
                "ITOT",
                "IEFA",
                "IEMG",
                "IEF",
                "HYG",
                "GLD",
                "DBC",
                "UUP",
                "SPY",
                "SPXL",
            ],
            {
                "BIL": "0.0001",
                "ITOT": "0.01",
                "IEFA": "0.01",
                "IEMG": "0.01",
                "IEF": "0.01",
                "HYG": "0.01",
                "GLD": "0.01",
                "DBC": "0.01",
                "UUP": "0.01",
                "SPY": "0.01",
                "SPXL": "0.03",
            },
        ),
        "inputs/lenses/us-sector-factors.csv": (
            ["date", "BIL", "XLC"],
            {"BIL": "0.0001", "XLC": "0.01"},
        ),
        "inputs/lenses/us-industry-factors.csv": (
            ["date", "rf", "Agric"],
            {"rf": "0.0001", "Agric": "0.01"},
        ),
    }
    manifest_inputs = []
    for relative, (columns, values) in sources.items():
        path = bundle / relative
        rows = [
            {"date": observed_date, **values} for observed_date in observation_dates
        ]
        _write_csv(path, columns, rows)
        manifest_inputs.append({"path": relative, "sha256": _sha(path)})
    market_path = bundle / "inputs/market/style_returns.csv"
    market_rows = [
        {"date": observed, "SPY": "0.004", "SPXL": "0.012", "BIL": ""}
        for observed in market_history_dates
    ] + [
        {"date": observed, "SPY": "0.01", "SPXL": "0.03", "BIL": "0.0001"}
        for observed in observation_dates
    ]
    _write_csv(market_path, ["date", "BIL", "SPY", "SPXL"], market_rows)
    manifest_inputs.append(
        {"path": "inputs/market/style_returns.csv", "sha256": _sha(market_path)}
    )
    _write_json(
        bundle / "plan-manifest.json",
        {
            "bundle_id": bundle.name,
            "inputs": manifest_inputs,
            "outputs": [],
            "schema_version": 1,
        },
    )
    return bundle


def _current_plan_fixture(tmp_path: Path) -> Path:
    legacy_bundle = _plan_fixture(tmp_path / "legacy-plan")
    inputs = []
    for path in sorted((legacy_bundle / "inputs").rglob("*")):
        if path.is_file():
            inputs.append(
                {
                    "path": path.relative_to(legacy_bundle).as_posix(),
                    "sha256": _sha(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    manifest = {
        "builder": "portfolio-current-decomposition-plan",
        "builder_version": "0.17.0",
        "inputs": inputs,
        "outputs": [],
        "schema_version": 2,
    }
    manifest["bundle_id"] = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    bundle = tmp_path / manifest["bundle_id"]
    for source in sorted(path for path in legacy_bundle.rglob("*") if path.is_file()):
        if source.name == "plan-manifest.json":
            continue
        destination = bundle / source.relative_to(legacy_bundle)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    _write_json(bundle / "plan-manifest.json", manifest)
    return bundle


def _rebind_current_plan(
    bundle: Path, manifest: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    manifest.pop("bundle_id", None)
    manifest["bundle_id"] = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    _write_json(bundle / "plan-manifest.json", manifest)
    destination = bundle.parent / str(manifest["bundle_id"])
    bundle.rename(destination)
    return destination, manifest


def _publish_correlation_generation(
    correlation_root: Path,
    *,
    correlation_bytes: bytes,
    declared_correlation: SourceArtifact | None,
) -> None:
    current = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    artifacts = [
        artifact
        for artifact in current.artifacts
        if artifact.artifact_id != "normalized/correlation.json"
    ]
    if declared_correlation is not None:
        artifacts.append(declared_correlation)
    manifest = GenerationManifest.build(
        as_of_date=current.as_of_date,
        adapter=current.adapter,
        coverage=current.coverage,
        artifacts=tuple(artifacts),
        verification=current.verification,
    )
    manifest_path = stage_generation(correlation_root, manifest)
    (manifest_path.parent / "correlation.json").write_bytes(correlation_bytes)
    publish_generation(correlation_root, manifest.generation_id)


def test_weekly_generation_compounds_and_keeps_evidence_modes_separate(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    output_root = tmp_path / "weekly"
    candidate = weekly.build_candidate(
        correlation_root, strategy_returns, plan_bundle, output_root
    )
    candidate_dir = Path(candidate["candidate_dir"])
    result = weekly.verify_candidate(
        correlation_root, strategy_returns, plan_bundle, candidate_dir
    )
    assert result["all_passed"] is True
    payload = json.loads(
        (candidate_dir / "weekly-returns.json").read_text(encoding="utf-8")
    )
    strategy = next(
        row for row in payload["series"] if row["series_id"] == "strategy:Strategy 0"
    )
    assert strategy["values_by_mode"]["LIVE_ONLY"]["2026-07-27"] == "0.045"
    assert strategy["values_by_mode"]["LIVE_PLUS_BACKTEST"]["2026-07-20"] == "0.02"
    assert payload["week_policy"]["absent_value"] == "UNAVAILABLE_NOT_ZERO"
    assert payload["current_week"]["default_visible"] is True
    assert payload["current_week"]["is_partial"] is True
    assert payload["current_week"]["data_through_date"] == payload["as_of_date"]
    assert payload["observation_meta_fields"] == [
        "daily_observation_count",
        "actual_observed_start",
        "actual_observed_end",
        "coverage_status",
        "boundary_reasons",
    ]
    assert set(strategy["values_by_mode"]["LIVE_ONLY"]) == set(
        strategy["observation_meta_by_mode"]["LIVE_ONLY"]
    )
    assert (
        sum("CORE_BENCHMARK" in row["display_groups"] for row in payload["series"])
        == 11
    )

    published = weekly.publish(
        correlation_root,
        strategy_returns,
        plan_bundle,
        candidate_dir,
        output_root,
    )
    assert read_current(output_root).generation_id == published["generation_id"]
    consumed, identity, _path = panel._current_analytics(
        output_root,
        filename="weekly-returns.json",
        field_name="portfolio weekly returns",
        expected_schema_version=2,
    )
    assert consumed["analytics_schema_version"] == 2
    assert identity["generation_id"] == published["generation_id"]


def test_current_partial_week_is_visible_by_default(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _current_plan_fixture(tmp_path)

    payload, _inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    assert payload["current_week"]["default_visible"] is True
    assert payload["current_week"]["is_partial"] is True
    assert payload["current_week"]["data_through_date"] == payload["as_of_date"]
    assert payload["default_evidence_mode"] == "LIVE_ONLY"


def test_current_week_boundary_uses_cutoff_for_partial_and_complete_weeks() -> None:
    partial = weekly._current_week(date(2026, 9, 17))
    complete = weekly._current_week(date(2026, 9, 20))

    assert partial == {
        "data_through_date": "2026-09-17",
        "default_visible": True,
        "is_partial": True,
        "week_end": "2026-09-20",
        "week_start": "2026-09-14",
    }
    assert complete == {
        "data_through_date": "2026-09-20",
        "default_visible": True,
        "is_partial": False,
        "week_end": "2026-09-20",
        "week_start": "2026-09-14",
    }


def test_weekly_generation_accepts_exact_current_plan_bundle(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _current_plan_fixture(tmp_path)

    payload, inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    assert payload["analytics_schema_version"] == 2
    assert plan_bundle / "plan-manifest.json" in inputs
    assert len(payload["series"]) > len(STRATEGIES)


def test_weekly_generation_accepts_current_dependence_source_identity(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(
        tmp_path,
        source_artifact_id="current-dependence/returns.csv",
        strategy_count=7,
    )
    plan_bundle = _plan_fixture(tmp_path)
    output_root = tmp_path / "weekly"

    payload, _inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )
    built = weekly.build_candidate(
        correlation_root, strategy_returns, plan_bundle, output_root
    )
    verification = weekly.verify_candidate(
        correlation_root,
        strategy_returns,
        plan_bundle,
        Path(built["candidate_dir"]),
    )

    assert sum(row["primary_group"] == "STRATEGY" for row in payload["series"]) == 7
    assert payload["default_evidence_mode"] == "LIVE_ONLY"
    assert verification["checks"]["strategy_boundary"] is True


def test_weekly_plan_binding_reads_an_artifact_beyond_windows_max_path(
    tmp_path: Path,
) -> None:
    base = tmp_path
    relative = "inputs/base/provenance/provider-ledger-generation.json"
    while len(str((base / ("b" * 64) / relative).absolute())) <= 270:
        base /= "long-weekly-plan-component"
    bundle = base / ("b" * 64)
    payload = canonical_json_bytes({"generation_id": "a" * 64})
    manifest = {
        "bundle_id": bundle.name,
        "inputs": [
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "outputs": [],
        "schema_version": 1,
    }

    for path, content in (
        (bundle / relative, payload),
        (bundle / "plan-manifest.json", canonical_json_bytes(manifest)),
    ):
        storage = Path(f"\\\\?\\{path.absolute()}") if sys.platform == "win32" else path
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_bytes(content)

    assert weekly._manifest_hashes(bundle) == {
        relative: hashlib.sha256(payload).hexdigest()
    }


def test_weekly_rejects_public_observation_after_correlation_as_of(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(
        tmp_path,
        observation_dates=("2026-07-31", "2026-08-03"),
    )

    with pytest.raises(
        weekly.WeeklyReturnError,
        match="weekly observation is after correlation as_of",
    ):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


def test_weekly_current_week_is_anchored_to_the_data_cutoff_not_the_manifest_date(
    tmp_path: Path,
) -> None:
    """R2-3 / NEW-L1: the correlation manifest is dated at the execution clock.

    The weekly artifact must not claim data through the later execution date: the
    current week is the one that holds the cutoff, partial, with data through it.
    """

    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path, observation_dates=("2026-07-31",))
    manifest = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    assert manifest.as_of_date == EXECUTION_DATE

    payload, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    assert payload["as_of_date"] == "2026-07-31"
    assert payload["current_week"] == {
        "data_through_date": "2026-07-31",
        "default_visible": True,
        "is_partial": True,
        "week_end": "2026-08-02",
        "week_start": "2026-07-27",
    }
    assert "2026-08-03" not in payload["weeks"]


def test_weekly_rejects_a_declared_cutoff_that_differs_from_the_coverage_end(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    current = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    path = correlation_root / "generations" / current.generation_id / "correlation.json"
    correlation = json.loads(path.read_text(encoding="utf-8"))
    correlation["complete_through"] = "2026-07-30"
    declared = SourceArtifact(
        artifact_id="normalized/correlation.json",
        sha256=hashlib.sha256(canonical_json_bytes(correlation)).hexdigest(),
        size_bytes=len(canonical_json_bytes(correlation)),
        as_of_date=current.as_of_date,
        schema_name="portfolio-correlation-analytics",
        schema_version="2",
    )
    _publish_correlation_generation(
        correlation_root,
        correlation_bytes=canonical_json_bytes(correlation),
        declared_correlation=declared,
    )

    with pytest.raises(
        weekly.WeeklyReturnError, match="correlation cutoff differs from its coverage"
    ):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


def test_weekly_accepts_public_observation_exactly_at_correlation_as_of(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path, observation_dates=("2026-07-31",))

    payload, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    assert payload["as_of_date"] == "2026-07-31"


@pytest.mark.parametrize(
    ("artifact_id", "sha256", "size_bytes"),
    (
        ("normalized/not-correlation.json", None, None),
        (None, "0" * 64, None),
        (None, None, 0),
    ),
)
def test_weekly_rejects_declared_correlation_artifact_binding_drift(
    tmp_path: Path,
    artifact_id: str | None,
    sha256: str | None,
    size_bytes: int | None,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    current = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    declared = next(
        artifact
        for artifact in current.artifacts
        if artifact.artifact_id == "normalized/correlation.json"
    )
    source_path = (
        correlation_root / "generations" / current.generation_id / "correlation.json"
    )
    _publish_correlation_generation(
        correlation_root,
        correlation_bytes=source_path.read_bytes(),
        declared_correlation=SourceArtifact(
            artifact_id=artifact_id or declared.artifact_id,
            sha256=sha256 or declared.sha256,
            size_bytes=size_bytes if size_bytes is not None else declared.size_bytes,
            as_of_date=declared.as_of_date,
            schema_name=declared.schema_name,
            schema_version=declared.schema_version,
        ),
    )

    with pytest.raises(
        weekly.WeeklyReturnError,
        match="correlation artifact binding differs",
    ):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


def test_weekly_rejects_missing_declared_correlation_artifact(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    current = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    source_path = (
        correlation_root / "generations" / current.generation_id / "correlation.json"
    )
    _publish_correlation_generation(
        correlation_root,
        correlation_bytes=source_path.read_bytes(),
        declared_correlation=None,
    )

    with pytest.raises(
        weekly.WeeklyReturnError,
        match="correlation artifact binding differs",
    ):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


def test_weekly_rejects_correlation_artifact_from_another_generation(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    current = read_generation(
        correlation_root, read_current(correlation_root).generation_id
    )
    source_path = (
        correlation_root / "generations" / current.generation_id / "correlation.json"
    )
    alternate_path = tmp_path / "alternate-correlation.json"
    alternate = json.loads(source_path.read_text(encoding="utf-8"))
    alternate["synthetic_generation"] = "alternate"
    _write_json(alternate_path, alternate)
    _publish_correlation_generation(
        correlation_root,
        correlation_bytes=source_path.read_bytes(),
        declared_correlation=SourceArtifact.from_path(
            alternate_path,
            artifact_id="normalized/correlation.json",
            as_of_date=current.as_of_date,
            schema_name="portfolio-correlation-analytics",
            schema_version="2",
        ),
    )

    with pytest.raises(
        weekly.WeeklyReturnError,
        match="correlation artifact binding differs",
    ):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("builder", "forged-builder", "builder identity differs"),
        ("builder_version", "0.19.0", "builder identity differs"),
    ),
)
def test_weekly_rejects_current_plan_builder_substitution(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    bundle = _current_plan_fixture(tmp_path)
    manifest = json.loads((bundle / "plan-manifest.json").read_text(encoding="utf-8"))
    manifest[field] = value
    bundle, _ = _rebind_current_plan(bundle, manifest)

    with pytest.raises(weekly.WeeklyReturnError, match=message):
        weekly.build_weekly_returns(correlation_root, strategy_returns, bundle)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("size_bytes", 0, "size differs"),
        ("path", "../escape.csv", "path is invalid"),
    ),
)
def test_weekly_rejects_current_plan_artifact_binding_drift(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    bundle = _current_plan_fixture(tmp_path)
    manifest = json.loads((bundle / "plan-manifest.json").read_text(encoding="utf-8"))
    manifest["inputs"][0][field] = value
    bundle, _ = _rebind_current_plan(bundle, manifest)

    with pytest.raises(weekly.WeeklyReturnError, match=message):
        weekly.build_weekly_returns(correlation_root, strategy_returns, bundle)


def test_weekly_generation_rejects_strategy_source_hash_drift(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    with strategy_returns.open("a", encoding="utf-8") as handle:
        handle.write("Strategy 0,2026-07-29,0.01,ACCOUNT_LIVE,version-0\n")
    try:
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)
    except weekly.WeeklyReturnError as exc:
        assert "hash differs" in str(exc)
    else:
        raise AssertionError("strategy source drift was accepted")


def test_weekly_calendar_metadata_handles_year_weekend_and_leap_boundaries() -> None:
    values, metadata, calendar = weekly._weekly_series(
        [
            (date(2025, 12, 29), Decimal("0.01")),
            (date(2025, 12, 31), Decimal("0.02")),
            (date(2026, 1, 2), Decimal("-0.01")),
            (date(2026, 1, 3), Decimal("0.03")),
            (date(2026, 1, 4), Decimal("0.01")),
        ],
        as_of=date(2026, 1, 4),
    )
    assert calendar == "CALENDAR_7_DAY"
    assert list(values) == ["2025-12-29"]
    assert metadata["2025-12-29"] == [
        5,
        "2025-12-29",
        "2026-01-04",
        "COMPLETE_WITHIN_ACCEPTED_CALENDAR",
        [],
    ]

    _, leap_metadata, _ = weekly._weekly_series(
        [
            (date(2024, 2, 26), Decimal("0.01")),
            (date(2024, 2, 29), Decimal("0.01")),
            (date(2024, 3, 1), Decimal("0.01")),
        ],
        as_of=date(2024, 3, 3),
    )
    assert leap_metadata["2024-02-26"][2] == "2024-03-01"
    assert leap_metadata["2024-02-26"][3] == "COMPLETE_WITHIN_ACCEPTED_CALENDAR"


def test_canonical_live_series_digest_is_ordered_and_value_sensitive() -> None:
    first = weekly._canonical_series_sha256(
        [
            (date(2026, 1, 3), Decimal("0.020")),
            (date(2026, 1, 2), Decimal("0.010")),
        ]
    )
    reordered = weekly._canonical_series_sha256(
        [
            (date(2026, 1, 2), Decimal("0.010")),
            (date(2026, 1, 3), Decimal("0.020")),
        ]
    )
    mutated = weekly._canonical_series_sha256(
        [
            (date(2026, 1, 2), Decimal("0.010")),
            (date(2026, 1, 3), Decimal("0.021")),
        ]
    )
    assert first == reordered
    assert first != mutated


def test_weekly_metadata_marks_partial_boundaries_without_filling_missing_weeks() -> (
    None
):
    values, metadata, calendar = weekly._weekly_series(
        [
            (date(2026, 7, 1), Decimal("0.01")),
            (date(2026, 7, 2), Decimal("0.02")),
            (date(2026, 7, 6), Decimal("0.03")),
            (date(2026, 7, 8), Decimal("-0.01")),
        ],
        as_of=date(2026, 7, 8),
    )
    assert calendar == "WEEKDAY"
    assert list(values) == ["2026-06-29", "2026-07-06"]
    assert metadata["2026-06-29"][3:] == ["PARTIAL_BOUNDARY", ["SOURCE_START"]]
    assert metadata["2026-07-06"][3] == "PARTIAL_BOUNDARY"
    assert "CURRENT_AS_OF" in metadata["2026-07-06"][4]
    assert "2026-07-13" not in values


def test_weekly_rejects_duplicate_dates_and_total_loss_or_worse() -> None:
    with pytest.raises(weekly.WeeklyReturnError, match="duplicate daily observation"):
        weekly._weekly_series(
            [
                (date(2026, 7, 27), Decimal("0.01")),
                (date(2026, 7, 27), Decimal("0.02")),
            ],
            as_of=date(2026, 7, 31),
        )
    for invalid in ("-1", "-1.01"):
        with pytest.raises(weekly.WeeklyReturnError, match="greater than -1"):
            weekly._decimal(invalid, "synthetic return")


def test_weekly_compounding_error_is_bounded_well_below_the_consumers_resolution() -> (
    None
):
    """Class sweep (Astra rg4): the weekly return is compounded at the ambient 28 digits.

    Inputs are daily returns with 34 significant digits.  Seven daily factors round at
    most seven times at 0.5e-27 relative, so the published weekly value is within
    4e-27 of the exact compounded return.  Every consumer reads it as a float64
    (resolution 1.1e-16, see ``portfolio_control_panel._weekly_values``), which is
    eleven orders wider, and the value is a return, not money: it is never compared
    for equality or summed.  The bound is pinned here so a change that widens it, for
    example a coarser context, fails.
    """

    from datetime import timedelta
    from decimal import localcontext

    returns = [
        Decimal("0.0123456789012345678901234567890123"),
        Decimal("-0.0098765432109876543210987654321098"),
        Decimal("0.0045678901234567890123456789012345"),
        Decimal("0.0011111111111111111111111111111111"),
        Decimal("-0.0077777777777777777777777777777777"),
        Decimal("0.0033333333333333333333333333333333"),
        Decimal("0.0022222222222222222222222222222222"),
    ]
    days = [date(2026, 7, 6) + timedelta(days=offset) for offset in range(7)]

    published = weekly._weekly_values(list(zip(days, returns, strict=True)))

    with localcontext() as context:
        context.prec = 100
        exact = Decimal(1)
        for value in returns:
            exact *= 1 + value
        exact -= 1
    assert len(published) == 1
    assert abs(Decimal(next(iter(published.values()))) - exact) < Decimal("4e-27")


def test_full_history_benchmarks_come_from_the_market_input_not_the_gated_lens(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)

    payload, inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    by_id = {row["series_id"]: row for row in payload["series"]}
    for ticker in weekly.FULL_HISTORY_BENCHMARKS:
        row = by_id[f"market:{ticker}"]
        assert row["source_key"] == "core-benchmark-market"
        assert "CORE_BENCHMARK" in row["display_groups"]
        # the history before the cash proxy's first observation is kept
        assert row["coverage"]["first_week"] == "2026-07-13"
    assert plan_bundle / "inputs/market/style_returns.csv" in inputs
    assert by_id["market:IWX"]["source_key"] == "us-equity-style"

    daily = payload["daily_benchmark_returns"]
    assert daily["status"] == "AVAILABLE"
    assert daily["source_id"] == "yahoo-adjusted-etf-daily"
    assert daily["unit"] == "DECIMAL_DAILY_RETURN"
    assert daily["source_artifact"]["path"] == "inputs/market/style_returns.csv"
    spy = next(item for item in daily["series"] if item["ticker"] == "SPY")
    assert spy["series_id"] == "market:SPY"
    assert spy["dates"][:2] == ["2026-07-14", "2026-07-15"]
    assert spy["returns"][:2] == ["0.004", "0.004"]
    assert spy["observation_count"] == len(spy["dates"]) == len(spy["returns"])
    assert spy["dates"] == sorted(set(spy["dates"]))
    assert max(spy["dates"]) <= payload["as_of_date"]
    # daily and weekly agree: the weekly value is the product of the daily returns
    week = [
        Decimal(1) + Decimal(value)
        for day, value in zip(spy["dates"], spy["returns"], strict=True)
        if "2026-07-13" <= day <= "2026-07-19"
    ]
    product = Decimal(1)
    for factor in week:
        product *= factor
    assert Decimal(by_id["market:SPY"]["values"]["2026-07-13"]) == product - 1


def test_market_benchmark_input_hash_is_bound_to_the_plan_manifest(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    market = plan_bundle / "inputs/market/style_returns.csv"
    market.write_bytes(market.read_bytes() + b"2026-07-29,,0.5,0.5\n")

    with pytest.raises(weekly.WeeklyReturnError, match="hash differs"):
        weekly.build_weekly_returns(correlation_root, strategy_returns, plan_bundle)


def _gap_fill_root(
    tmp_path: Path,
    *,
    flag: str = "ORIGIN_DERIVED",
    days: tuple[str, ...] = (
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
        "2026-07-16",
        "2026-07-17",
        "2026-07-20",
        "2026-07-21",
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
    ),
    mode: str = "LIVE_PLUS_BACKTEST",
    series: str = "Strategy 0",
) -> Path:
    root = tmp_path / "gap-fill"
    rows = [
        {
            "series_id": "Fill Source",
            "date": day,
            "return": "0.01",
            "evidence_layer": "DERIVED",
            "return_basis": "FIXTURE",
        }
        for day in days
    ] + [
        {
            "series_id": "Other Source",
            "date": "2026-07-14",
            "return": "0.5",
            "evidence_layer": "DERIVED",
            "return_basis": "FIXTURE",
        }
    ]
    staging = tmp_path / "fill-returns.csv"
    _write_csv(
        staging,
        ["series_id", "date", "return", "evidence_layer", "return_basis"],
        rows,
    )
    artifact = SourceArtifact.from_path(
        staging,
        artifact_id="returns.csv",
        as_of_date=EXECUTION_DATE,
        schema_name="fixture-returns",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=EXECUTION_DATE,
        adapter=AdapterIdentity("synthetic-fill", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic fill",
            fact_types=("fill",),
            observed_start=date(2026, 7, 13),
            observed_end=date(2026, 7, 24),
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    generation = root / "generations" / manifest.generation_id
    generation.mkdir(parents=True)
    (generation / "generation.json").write_bytes(manifest.to_json_bytes())
    (generation / "returns.csv").write_bytes(staging.read_bytes())
    _write_json(
        root / "spec.json",
        {
            "schema_version": 1,
            "policy_id": "fixture-fill",
            "fills": [
                {
                    "strategy_series_id": series,
                    "mode": mode,
                    "origin_flag": flag,
                    "source": {
                        "generation_id": manifest.generation_id,
                        "returns_artifact": "returns.csv",
                        "series_id": "Fill Source",
                        "evidence_layer": "DERIVED",
                    },
                }
            ],
        },
    )
    return root


def test_gap_fill_supplies_only_the_unobserved_hybrid_weeks_with_an_origin_flag(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(
        tmp_path, backtest_date="2026-07-06"
    )
    plan_bundle = _plan_fixture(tmp_path)
    before, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )
    base = next(r for r in before["series"] if r["series_id"] == "strategy:Strategy 0")
    base_hybrid = base["values_by_mode"]["LIVE_PLUS_BACKTEST"]
    assert base["mode_coverage"]["LIVE_PLUS_BACKTEST"]["internal_missing_weeks"] == 2
    assert before["gap_fills"] == {"fills": [], "policy_id": None, "spec_sha256": None}

    root = _gap_fill_root(tmp_path)
    payload, inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle, root
    )
    row = next(r for r in payload["series"] if r["series_id"] == "strategy:Strategy 0")
    hybrid = row["values_by_mode"]["LIVE_PLUS_BACKTEST"]
    meta = row["observation_meta_by_mode"]["LIVE_PLUS_BACKTEST"]
    # existing weeks are untouched; the two gap weeks are filled and flagged
    assert hybrid["2026-07-06"] == base_hybrid["2026-07-06"]
    assert hybrid["2026-07-27"] == base_hybrid["2026-07-27"]
    assert meta["2026-07-13"][4] == ["ORIGIN_DERIVED"]
    assert meta["2026-07-20"][4] == ["ORIGIN_DERIVED"]
    assert meta["2026-07-13"][0] == 5
    assert "ORIGIN_DERIVED" not in meta["2026-07-27"][4]
    expected = Decimal("1.01") ** 5 - 1
    assert abs(Decimal(hybrid["2026-07-13"]) - expected) < Decimal("1E-20")
    coverage = row["mode_coverage"]["LIVE_PLUS_BACKTEST"]
    assert coverage["internal_missing_weeks"] == 0
    detail = coverage["gap_fill"]
    assert detail["filled_weeks"] == 2
    assert detail["filled_daily_observations"] == 10
    assert detail["first_filled_week"] == "2026-07-13"
    assert detail["observations_outside_gap_ignored"] == 0
    # LIVE_ONLY never carries a fill
    assert "gap_fill" not in row["mode_coverage"]["LIVE_ONLY"]
    assert all(
        "ORIGIN_DERIVED" not in item[4]
        for item in row["observation_meta_by_mode"]["LIVE_ONLY"].values()
    )
    assert payload["gap_fills"]["fills"][0]["origin_flag"] == "ORIGIN_DERIVED"
    assert root / "spec.json" in inputs


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"flag": "ORIGIN_BACKTEST"}, "origin flag is unsupported"),
        ({"mode": "LIVE_ONLY"}, "hybrid mode"),
        ({"series": "No Such Strategy"}, "unknown strategy"),
        ({"days": ("2026-07-13", "2026-07-14")}, "unfilled weeks"),
    ],
)
def test_gap_fill_refuses_unsupported_or_incomplete_fills(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    correlation_root, strategy_returns = _correlation_fixture(
        tmp_path, backtest_date="2026-07-06"
    )
    plan_bundle = _plan_fixture(tmp_path)
    root = _gap_fill_root(tmp_path, **overrides)  # type: ignore[arg-type]
    with pytest.raises(weekly.WeeklyReturnError, match=message):
        weekly.build_weekly_returns(
            correlation_root, strategy_returns, plan_bundle, root
        )


def test_gap_fill_requires_a_real_gap_and_an_untampered_source(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    root = _gap_fill_root(tmp_path)
    with pytest.raises(weekly.WeeklyReturnError, match="no gap"):
        weekly.build_weekly_returns(
            correlation_root, strategy_returns, plan_bundle, root
        )
    generation = next((root / "generations").iterdir())
    returns = generation / "returns.csv"
    returns.write_bytes(returns.read_bytes() + b"\n")
    with pytest.raises(weekly.WeeklyReturnError, match="differ from their manifest"):
        weekly.build_weekly_returns(
            correlation_root, strategy_returns, plan_bundle, root
        )


def test_daily_benchmark_block_validates_against_the_panel_schema(
    tmp_path: Path,
) -> None:
    import jsonschema

    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    payload, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas/portfolio/control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    block_schema = {
        "$defs": schema["$defs"],
        **schema["properties"]["weekly_returns"]["properties"][
            "daily_benchmark_returns"
        ],
    }
    validator = jsonschema.Draft202012Validator(
        block_schema, format_checker=jsonschema.FormatChecker()
    )
    validator.validate(payload["daily_benchmark_returns"])
    broken = json.loads(json.dumps(payload["daily_benchmark_returns"]))
    broken["unit"] = "PERCENT"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(broken)


def test_gap_fill_generation_must_pass_the_manifest_contract(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _plan_fixture(tmp_path)
    root = _gap_fill_root(tmp_path)
    generation = next((root / "generations").iterdir())
    manifest = generation / "generation.json"
    manifest.write_bytes(manifest.read_bytes().replace(b'"VERIFIED"', b'"VERIFIED" '))
    manifest_text = json.loads(manifest.read_bytes())
    manifest_text["coverage"] = {"forged": True}
    manifest.write_bytes(json.dumps(manifest_text).encode("utf-8"))
    with pytest.raises(weekly.WeeklyReturnError, match="manifest contract"):
        weekly.build_weekly_returns(
            correlation_root, strategy_returns, plan_bundle, root
        )


def test_gap_fill_spec_pin_must_be_named_spec_json() -> None:
    import portfolio_release_builder as builder

    builder.require_gap_fill_spec_name(Path("x/spec.json"))
    with pytest.raises(builder.VerticalBuildError, match="spec.json"):
        builder.require_gap_fill_spec_name(Path("x/other.json"))


def test_vendor_gap_fills_recipe_pin_becomes_the_panel_arguments(
    tmp_path: Path,
) -> None:
    import portfolio_release_builder as builder

    assert builder._vendor_gap_fill_arguments({}) == ()
    fills = tmp_path / "fills.json"
    fills.write_bytes(b"{}\n")
    definition = builder._file_definition(fills)
    arguments = builder._vendor_gap_fill_arguments({"vendor_gap_fills": definition})
    assert arguments[0] == "--vendor-gap-fills"
    assert arguments[2] == "--vendor-gap-fills-sha256"
    assert arguments[3] == definition["sha256"]


# --- accepted multi-day spans (N1) -------------------------------------------------
# All values below are synthetic.  A span is an accepted holding-period return whose
# end date has no daily row, so the weekly line must compound it into the week that
# contains its end date and the line must telescope to the governed epoch return.

SPAN_SERIES = "Span Strategy"
PLAIN_SERIES = "Plain Strategy"
SPAN_DAILY = {
    "2026-07-20": "0.01",
    "2026-07-21": "0.02",
    "2026-07-24": "0.005",
    "2026-07-28": "-0.01",
    "2026-07-29": "0.02",
}
# (start, end, return): the second span crosses the ISO week boundary.
SPAN_LINKS = (
    ("2026-07-21", "2026-07-23", "-0.03"),
    ("2026-07-24", "2026-07-27", "0.015"),
)
SPAN_TOLERANCE = Decimal("1e-24")


def _span_coverage(
    path: Path,
    *,
    links: tuple[tuple[str, str, str], ...] = SPAN_LINKS,
    daily_sha: str | None = None,
    epoch_shift: Decimal = Decimal(0),
    strategy_id: str = "span-s",
) -> Path:
    daily = [(date.fromisoformat(day), Decimal(r)) for day, r in SPAN_DAILY.items()]
    daily_wealth = Decimal(1)
    for _day, value in daily:
        daily_wealth *= 1 + value
    epoch_wealth = daily_wealth
    for _start, _end, value in links:
        epoch_wealth *= 1 + Decimal(value)
    _write_json(
        path,
        {
            "strategies": [
                {"series_id": SPAN_SERIES, "strategy_id": "span-s"},
                {"series_id": PLAIN_SERIES, "strategy_id": "plain-s"},
                {"series_id": None, "strategy_id": "excluded-s"},
            ],
            "epoch_hpr_receipts": [
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "strategy_id": strategy_id,
                    "receipt": {
                        "daily_cumulative_return": str(daily_wealth - 1),
                        "daily_return_series_sha256": daily_sha
                        or weekly._canonical_series_sha256(daily),
                        "epoch_cumulative_return": str(epoch_wealth - 1 + epoch_shift),
                        "span_return_receipts": [
                            {
                                "return": value,
                                "span_days": (
                                    date.fromisoformat(end) - date.fromisoformat(start)
                                ).days,
                                "span_end_date": end,
                                "span_start_date": start,
                            }
                            for start, end, value in links
                        ],
                    },
                }
            ],
        },
    )
    return path


def _span_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    rows = [
        {
            "date": day,
            "evidence_layer": "ACCOUNT_LIVE",
            "return": value,
            "series_id": SPAN_SERIES,
            "strategy_version": "v-span",
        }
        for day, value in SPAN_DAILY.items()
    ]
    rows.append(
        {
            "date": "2026-07-13",
            "evidence_layer": "BACKTEST",
            "return": "0.02",
            "series_id": SPAN_SERIES,
            "strategy_version": "v-span",
        }
    )
    rows.extend(
        {
            "date": day,
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.001",
            "series_id": PLAIN_SERIES,
            "strategy_version": "v-plain",
        }
        for day in ("2026-07-20", "2026-07-21", "2026-07-22")
    )
    strategy_returns = tmp_path / "strategy-returns.csv"
    _write_csv(
        strategy_returns,
        ["series_id", "date", "return", "evidence_layer", "strategy_version"],
        rows,
    )

    def composition(series: str, live: int, backtest: int) -> dict[str, object]:
        return {
            "backtest_observations_used": backtest,
            "live_inception": "2026-07-20",
            "live_observations": live,
            "strategy_version": "v-span" if series == SPAN_SERIES else "v-plain",
        }

    correlation = {
        "analytics_schema_version": 2,
        "complete_through": "2026-07-31",
        "default_evidence_mode": "LIVE_ONLY",
        "dependence_modes": {
            "LIVE_ONLY": {
                "series_composition": {
                    SPAN_SERIES: composition(SPAN_SERIES, 5, 0),
                    PLAIN_SERIES: composition(PLAIN_SERIES, 3, 0),
                }
            },
            "LIVE_PLUS_BACKTEST": {
                "series_composition": {
                    SPAN_SERIES: composition(SPAN_SERIES, 5, 1),
                    PLAIN_SERIES: composition(PLAIN_SERIES, 3, 0),
                }
            },
        },
        "source": {
            "artifact_id": "provider-rollforward/layered-strategy-returns.csv",
            "sha256": _sha(strategy_returns),
            "size_bytes": strategy_returns.stat().st_size,
        },
    }
    correlation_source = tmp_path / "correlation.json"
    _write_json(correlation_source, correlation)
    correlation_root = tmp_path / "correlation-root"
    manifest = GenerationManifest.build(
        as_of_date=EXECUTION_DATE,
        adapter=AdapterIdentity("synthetic-correlation", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic layered strategy evidence",
            fact_types=("layered strategy returns",),
            observed_start=date(2026, 7, 13),
            observed_end=date(2026, 7, 31),
        ),
        artifacts=(
            SourceArtifact.from_path(
                strategy_returns,
                artifact_id="provider-rollforward/layered-strategy-returns.csv",
                as_of_date=EXECUTION_DATE,
                schema_name="portfolio-layered-strategy-returns",
                schema_version="2",
            ),
            SourceArtifact.from_path(
                correlation_source,
                artifact_id="normalized/correlation.json",
                as_of_date=EXECUTION_DATE,
                schema_name="portfolio-correlation-analytics",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(correlation_root, manifest)
    (manifest_path.parent / "correlation.json").write_bytes(
        correlation_source.read_bytes()
    )
    publish_generation(correlation_root, manifest.generation_id)
    return correlation_root, strategy_returns, _plan_fixture(tmp_path)


def _span_series(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        row for row in payload["series"] if row["series_id"] == f"strategy:{name}"
    )


def _line_end(values: dict[str, str], start: str | None = None) -> Decimal:
    wealth = Decimal(1)
    for week in sorted(values):
        if start is None or week >= start:
            wealth *= 1 + Decimal(values[week])
    return wealth


def test_accepted_spans_compound_into_the_week_containing_their_end(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns, plan_bundle = _span_fixture(tmp_path)
    coverage = _span_coverage(tmp_path / "history-coverage.json")
    payload, paths = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle, None, coverage
    )
    assert coverage in paths
    row = _span_series(payload, SPAN_SERIES)
    first = Decimal("1.01") * Decimal("1.02") * Decimal("0.97") * Decimal("1.005")
    second = Decimal("1.015") * Decimal("0.99") * Decimal("1.02")
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        values = row["values_by_mode"][mode]
        # week 07-20 holds the 07-23 span; week 07-27 holds the span that ends on
        # 07-27 even though it starts in the prior week.
        assert abs(Decimal(values["2026-07-20"]) - (first - 1)) < SPAN_TOLERANCE
        assert abs(Decimal(values["2026-07-27"]) - (second - 1)) < SPAN_TOLERANCE
    assert row["values_by_mode"]["LIVE_PLUS_BACKTEST"]["2026-07-13"] == "0.02"
    meta = row["observation_meta_by_mode"]["LIVE_ONLY"]
    assert meta["2026-07-20"][0] == 4 and meta["2026-07-27"][0] == 3
    spans = row["mode_coverage"]["LIVE_ONLY"]["accepted_multi_day_spans"]
    assert spans == [
        {"end": "2026-07-23", "return": "-0.03", "start": "2026-07-21"},
        {"end": "2026-07-27", "return": "0.015", "start": "2026-07-24"},
    ]
    plain = _span_series(payload, PLAIN_SERIES)
    assert "accepted_multi_day_spans" not in plain["mode_coverage"]["LIVE_ONLY"]


def test_weekly_line_telescopes_to_the_product_of_accepted_links(
    tmp_path: Path,
) -> None:
    correlation_root, strategy_returns, plan_bundle = _span_fixture(tmp_path)
    coverage = _span_coverage(tmp_path / "history-coverage.json")
    payload, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle, None, coverage
    )
    values = _span_series(payload, SPAN_SERIES)["values_by_mode"]["LIVE_ONLY"]
    daily = [Decimal(value) for value in SPAN_DAILY.values()]
    spans = [Decimal(value) for _start, _end, value in SPAN_LINKS]
    # the end value of the line equals the governed compounded return of every link
    governed = Decimal(1)
    for value in [*daily, *spans]:
        governed *= 1 + value
    assert abs(_line_end(values) - governed) < SPAN_TOLERANCE
    # and the ratio over a whole-week window equals the links ending in that window
    last_week = Decimal("1.015") * Decimal("0.99") * Decimal("1.02")
    assert abs(_line_end(values, "2026-07-27") - last_week) < SPAN_TOLERANCE
    assert (
        abs(_line_end(values) / _line_end(values, "2026-07-27") - governed / last_week)
        < SPAN_TOLERANCE
    )
    # without the governed coverage the same builder omits both measured spans
    bare, _ = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle, None, None
    )
    bare_values = _span_series(bare, SPAN_SERIES)["values_by_mode"]["LIVE_ONLY"]
    assert abs(_line_end(bare_values) - governed) > Decimal("0.01")


def test_accepted_spans_are_refused_when_they_do_not_bind(tmp_path: Path) -> None:
    correlation_root, strategy_returns, plan_bundle = _span_fixture(tmp_path)

    def build(coverage: Path) -> None:
        weekly.build_weekly_returns(
            correlation_root, strategy_returns, plan_bundle, None, coverage
        )

    with pytest.raises(weekly.WeeklyReturnError, match="bind the live daily path"):
        build(_span_coverage(tmp_path / "a.json", daily_sha="0" * 64))
    with pytest.raises(weekly.WeeklyReturnError, match="do not telescope"):
        build(_span_coverage(tmp_path / "b.json", epoch_shift=Decimal("0.001")))
    with pytest.raises(weekly.WeeklyReturnError, match="unknown strategy"):
        build(_span_coverage(tmp_path / "c.json", strategy_id="missing-s"))
    with pytest.raises(weekly.WeeklyReturnError, match="daily observation"):
        build(
            _span_coverage(
                tmp_path / "d.json", links=(("2026-07-27", "2026-07-29", "0.01"),)
            )
        )
    with pytest.raises(weekly.WeeklyReturnError, match="not a multi-day span"):
        build(
            _span_coverage(
                tmp_path / "e.json", links=(("2026-07-22", "2026-07-23", "0.01"),)
            )
        )
    with pytest.raises(weekly.WeeklyReturnError, match="overlap"):
        build(
            _span_coverage(
                tmp_path / "f.json",
                links=(
                    ("2026-07-21", "2026-07-24", "0.01"),
                    ("2026-07-22", "2026-07-26", "0.01"),
                ),
            )
        )


def test_release_builder_passes_the_history_coverage_to_the_weekly_stage(
    tmp_path: Path,
) -> None:
    import portfolio_release_builder as builder

    # a legacy source carries no dependence receipt and so no span authority
    assert builder._weekly_history_coverage_arguments(tmp_path) == ()
    (tmp_path / "dependence-source.json").write_bytes(b"{}\n")
    with pytest.raises(builder.VerticalBuildError, match="history coverage"):
        builder._weekly_history_coverage_arguments(tmp_path)
    (tmp_path / "history-coverage.json").write_bytes(b"{}\n")
    assert builder._weekly_history_coverage_arguments(tmp_path) == (
        "--history-coverage",
        str(tmp_path / "history-coverage.json"),
    )


def test_weekly_spans_must_equal_the_manifest_links_exactly(tmp_path: Path) -> None:
    import portfolio_release_builder as builder

    correlation_root, strategy_returns, plan_bundle = _span_fixture(tmp_path)
    coverage = _span_coverage(tmp_path / "history-coverage.json")
    weekly_root = tmp_path / "weekly"
    candidate = weekly.build_candidate(
        correlation_root, strategy_returns, plan_bundle, weekly_root, None, coverage
    )
    weekly.publish(
        correlation_root,
        strategy_returns,
        plan_bundle,
        Path(candidate["candidate_dir"]),
        weekly_root,
        None,
        coverage,
    )

    def manifest(factors: tuple[str, str]) -> Path:
        path = tmp_path / f"manifest-{factors[0]}-{factors[1]}.json"
        _write_json(
            path,
            {
                "rows": [
                    {
                        "observation_trace": {
                            "links": [
                                {
                                    "start_date": start,
                                    "end_date": end,
                                    "factor": factor,
                                }
                                for (start, end, _value), factor in zip(
                                    SPAN_LINKS, factors, strict=True
                                )
                            ]
                        }
                    }
                ]
            },
        )
        return path

    assert builder._verify_weekly_spans_against_manifest(weekly_root, None) == 0
    exact = manifest(("0.97", "1.015"))
    assert builder._verify_weekly_spans_against_manifest(weekly_root, exact) == 2
    for drifted in (("0.97", "1.0150000000001"), ("0.9700000001", "1.015")):
        with pytest.raises(builder.VerticalBuildError, match="manifest link"):
            builder._verify_weekly_spans_against_manifest(
                weekly_root, manifest(drifted)
            )
    gone = tmp_path / "manifest-empty.json"
    _write_json(gone, {"rows": [{"observation_trace": {"links": []}}]})
    with pytest.raises(builder.VerticalBuildError, match="manifest link"):
        builder._verify_weekly_spans_against_manifest(weekly_root, gone)

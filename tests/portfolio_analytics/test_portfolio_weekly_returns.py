from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
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

STRATEGIES = tuple(f"Strategy {index}" for index in range(9))


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
                    "date": "2026-07-20",
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
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 7, 31),
        adapter=AdapterIdentity("synthetic-correlation", "1.0.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic layered strategy evidence",
            fact_types=("layered strategy returns",),
            observed_start=date(2026, 7, 20),
            observed_end=date(2026, 7, 28),
        ),
        artifacts=(
            SourceArtifact.from_path(
                strategy_returns,
                artifact_id=source_artifact_id,
                as_of_date=date(2026, 7, 31),
                schema_name="portfolio-layered-strategy-returns",
                schema_version="2",
            ),
            SourceArtifact.from_path(
                correlation_source,
                artifact_id="normalized/correlation.json",
                as_of_date=date(2026, 7, 31),
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
            ["date", "BIL", "ITOT", "IEFA", "IEMG", "IEF", "HYG", "GLD", "DBC", "UUP"],
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
    assert payload["current_week"]["default_visible"] is False
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
        sum("CORE_BENCHMARK" in row["display_groups"] for row in payload["series"]) == 9
    )

    published = weekly.publish(
        correlation_root,
        strategy_returns,
        plan_bundle,
        candidate_dir,
        output_root,
    )
    assert read_current(output_root).generation_id == published["generation_id"]


def test_weekly_generation_accepts_exact_current_plan_bundle(tmp_path: Path) -> None:
    correlation_root, strategy_returns = _correlation_fixture(tmp_path)
    plan_bundle = _current_plan_fixture(tmp_path)

    payload, inputs = weekly.build_weekly_returns(
        correlation_root, strategy_returns, plan_bundle
    )

    assert payload["analytics_schema_version"] == 1
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

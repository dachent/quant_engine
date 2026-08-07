from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_decomposition_evidence_modes.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_decomposition_evidence_modes", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load decomposition evidence-mode builder")
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _definition(path: Path, artifact_id: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_size_bytes": len(content),
        "relative_path": path.name,
        "schema_name": "fixture",
        "schema_version": "1",
    }


def _fixture(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "base"
    root.mkdir()
    targets = root / "targets.csv"
    with targets.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(builder.TARGET_HEADERS)
        for series in ("alpha", "beta"):
            for observed in ("2026-01-05", "2026-01-06", "2026-01-07"):
                writer.writerow(
                    (series, observed, "0.001", "ACCOUNT_LIVE", "NET", "TOTAL", "")
                )
    factors = root / "factors.csv"
    factors.write_text("date,RF,MKT\n2026-01-05,0,0.001\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_bytes(
        _canonical(
            {
                "as_of_date": "2026-01-07",
                "factor_source": _definition(factors, "factors"),
                "maximum_factor_staleness_days": 10,
                "models": [],
                "provenance_sources": [],
                "schema_version": 1,
                "target_source": _definition(targets, "targets"),
            }
        )
    )
    layered = tmp_path / "layered.csv"
    with layered.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(builder.LAYERED_HEADERS)
        writer.writerow(("alpha", "2025-12-30", "0.002", "BACKTEST", "alpha-v1"))
        writer.writerow(("alpha", "2025-12-31", "0.003", "BACKTEST", "alpha-v1"))
        for series, version in (("alpha", "alpha-v1"), ("beta", "beta-v1")):
            for observed in ("2026-01-05", "2026-01-06", "2026-01-07"):
                writer.writerow((series, observed, "0.001", "ACCOUNT_LIVE", version))
    layered_content = layered.read_bytes()
    correlation = tmp_path / "correlation.json"
    correlation.write_bytes(
        _canonical(
            {
                "evidence_mode_series": [
                    {
                        "live_evidence_layer": "ACCOUNT_LIVE",
                        "series_id": "alpha",
                        "strategy_version": "alpha-v1",
                    },
                    {
                        "live_evidence_layer": "ACCOUNT_LIVE",
                        "series_id": "beta",
                        "strategy_version": "beta-v1",
                    },
                ],
                "return_source": {
                    "expected_sha256": hashlib.sha256(layered_content).hexdigest(),
                    "expected_size_bytes": len(layered_content),
                },
                "schema_version": 2,
                "series_ids": ["alpha", "beta"],
            }
        )
    )
    return {
        "base_spec_path": spec,
        "base_source_root": root,
        "layered_returns_path": layered,
        "correlation_spec_path": correlation,
    }


def _rewrite_correlation(inputs: dict[str, Any], layered_rows: list[list[str]]) -> None:
    layered = inputs["layered_returns_path"]
    with layered.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(builder.LAYERED_HEADERS)
        writer.writerows(layered_rows)
    correlation = json.loads(
        inputs["correlation_spec_path"].read_text(encoding="utf-8")
    )
    content = layered.read_bytes()
    correlation["return_source"] = {
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_size_bytes": len(content),
    }
    inputs["correlation_spec_path"].write_bytes(_canonical(correlation))


def test_builds_version_bound_live_and_hybrid_composition(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    outputs, manifest = builder.build_bundle(**inputs)

    assert manifest["evidence_mode"] == "LIVE_PLUS_BACKTEST"
    composition = json.loads(outputs["composition.json"])
    rows = {row["series_id"]: row for row in composition["series"]}
    assert rows["alpha"]["backtest_observations_used"] == 2
    assert rows["alpha"]["status"] == "AVAILABLE_LIVE_PLUS_VERSION_MATCHED_BACKTEST"
    assert rows["beta"]["backtest_observations_used"] == 0
    assert (
        rows["beta"]["status"] == "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
    )
    hybrid_spec = json.loads(outputs["base-spec.json"])
    live_spec = json.loads(outputs["live-base-spec.json"])
    assert hybrid_spec["evidence_mode"] == "LIVE_PLUS_BACKTEST"
    assert live_spec["evidence_mode"] == "LIVE_ONLY"
    assert hybrid_spec["series_composition"]["alpha"]["live_inception"] == "2026-01-05"
    assert live_spec["series_composition"]["alpha"]["backtest_observations_used"] == 0
    assert (
        outputs["factors.csv"]
        == inputs["base_source_root"].joinpath("factors.csv").read_bytes()
    )


def test_rejects_version_mismatch_and_excludes_backtest_live_overlap(
    tmp_path: Path,
) -> None:
    mismatch = _fixture(tmp_path / "mismatch")
    rows = list(
        csv.reader(
            mismatch["layered_returns_path"].read_text(encoding="utf-8").splitlines()
        )
    )[1:]
    rows[0][-1] = "wrong-version"
    _rewrite_correlation(mismatch, rows)
    with pytest.raises(builder.EvidenceModeError, match="version differs"):
        builder.build_bundle(**mismatch)

    overlap = _fixture(tmp_path / "overlap")
    rows = list(
        csv.reader(
            overlap["layered_returns_path"].read_text(encoding="utf-8").splitlines()
        )
    )[1:]
    rows[0][1] = "2026-01-05"
    _rewrite_correlation(overlap, rows)
    outputs, _manifest = builder.build_bundle(**overlap)
    composition = json.loads(outputs["composition.json"])
    alpha = next(row for row in composition["series"] if row["series_id"] == "alpha")
    assert alpha["backtest_observations_used"] == 1
    assert alpha["overlapping_backtest_observations_rejected"] == 1


def test_rejects_duplicate_mode_series_definition(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    correlation = json.loads(
        inputs["correlation_spec_path"].read_text(encoding="utf-8")
    )
    correlation["evidence_mode_series"].append(
        dict(correlation["evidence_mode_series"][0])
    )
    inputs["correlation_spec_path"].write_bytes(_canonical(correlation))

    with pytest.raises(builder.EvidenceModeError, match="duplicate series"):
        builder.build_bundle(**inputs)


def test_write_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    first = builder.write_bundle(tmp_path / "output", **inputs)
    second = builder.write_bundle(tmp_path / "output", **inputs)
    assert first == second
    assert Path(first["destination"]).joinpath("manifest.json").is_file()

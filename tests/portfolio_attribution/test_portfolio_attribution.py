from __future__ import annotations

from datetime import date
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
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_attribution", ROOT / "tools" / "portfolio_attribution.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to import attribution builder")
attribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attribution
SPEC.loader.exec_module(attribution)


def _baseline(tmp_path: Path) -> Path:
    root = tmp_path / "baseline"
    strategies = [
        {"name": f"Strategy {index}", "strategy_id": f"strategy-{index}"}
        for index in range(1, 10)
    ]
    catalog = {
        "allocations": [
            {
                "current_value": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": "100",
                },
                "scope": "SYSTEMATIC",
                "strategy_id": f"strategy-{index}",
            }
            for index in range(1, 10)
        ],
        "as_of_date": "2026-01-03",
        "baseline_schema_version": 1,
        "costs": [
            {
                "annual_external_cost_usd": "7.18" if index == 1 else None,
                "cost_status": "known" if index == 1 else "unknown",
                "strategy_id": f"strategy-{index}",
            }
            for index in range(1, 10)
        ],
        "strategies": strategies,
    }
    content = canonical_json_bytes(catalog)
    source = SourceArtifact(
        artifact_id="normalized/portfolio-baseline.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        as_of_date=date(2026, 1, 3),
        schema_name="portfolio-baseline-catalog",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 1, 3),
        adapter=AdapterIdentity("synthetic", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("portfolio",),
            observed_start=date(2026, 1, 1),
            observed_end=date(2026, 1, 3),
        ),
        artifacts=(source,),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-baseline.json").write_bytes(content)
    publish_generation(root, manifest.generation_id)
    return root


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    returns = tmp_path / "returns.csv"
    returns.write_text(
        "series_id,date,return,evidence_layer,version_ref,source_artifact_id\n"
        "account,2026-01-02,0.01,ACCOUNT_LIVE,v1,account-source\n"
        "account,2026-01-03,-0.02,ACCOUNT_LIVE,v1,account-source\n"
        "provider,2026-01-02,0.02,PROVIDER_LIVE,v1,provider-source\n"
        "provider,2026-01-03,-0.01,PROVIDER_LIVE,v1,provider-source\n"
        "benchmark,2026-01-02,0.005,OFFICIAL_VEHICLE,benchmark:v1,benchmark-source\n"
        "benchmark,2026-01-03,-0.005,OFFICIAL_VEHICLE,benchmark:v1,benchmark-source\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(
        json.dumps({"artifacts": [], "verification": "VERIFIED"}), encoding="utf-8"
    )
    benchmarks = [
        {
            "benchmark_id": "benchmark" if index == 1 else None,
            "effective_end": None,
            "effective_start": "2026-01-01",
            "rationale": (
                "Synthetic effective-dated benchmark assignment."
                if index == 1
                else "No authoritative synthetic assignment."
            ),
            "status": (
                "EVIDENCED" if index == 1 else "UNAVAILABLE_NO_AUTHORITATIVE_ASSIGNMENT"
            ),
            "strategy_id": f"strategy-{index}",
            "version_ref": "benchmark:v1" if index == 1 else None,
        }
        for index in range(1, 10)
    ]
    comparisons = [
        {
            "account_series_id": "account" if index == 1 else None,
            "cash_drag_reason": "No cash path is admitted.",
            "cash_drag_status": "UNAVAILABLE",
            "disposition": "Synthetic exact-date comparison."
            if index == 1
            else "No reference series.",
            "expected_legacy_cumulative_gap": "-0.02" if index == 1 else None,
            "observed_end": "2026-01-03" if index == 1 else None,
            "observed_start": "2026-01-02" if index == 1 else None,
            "periods_per_year": 252,
            "reference_series_id": "provider" if index == 1 else None,
            "status": attribution.AVAILABLE_STATUS if index == 1 else "UNAVAILABLE",
            "strategy_id": f"strategy-{index}",
            "version_compatibility_status": "UNRESOLVED",
        }
        for index in range(1, 10)
    ]
    benchmark_comparisons = [
        {
            "account_series_id": "account" if index == 1 else None,
            "cash_drag_reason": "No cash path is admitted.",
            "cash_drag_status": "UNAVAILABLE",
            "disposition": (
                "Synthetic exact-date benchmark comparison."
                if index == 1
                else "No benchmark assignment."
            ),
            "expected_legacy_cumulative_gap": None,
            "observed_end": "2026-01-03" if index == 1 else None,
            "observed_start": "2026-01-02" if index == 1 else None,
            "periods_per_year": 252,
            "reference_series_id": "benchmark" if index == 1 else None,
            "status": (
                "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE"
                if index == 1
                else "UNAVAILABLE"
            ),
            "strategy_id": f"strategy-{index}",
            "version_compatibility_status": (
                "PROVEN_COMPATIBLE" if index == 1 else "UNRESOLVED"
            ),
        }
        for index in range(1, 10)
    ]
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "as_of_date": "2026-01-03",
                "benchmark_assignments": benchmarks,
                "benchmark_comparisons": benchmark_comparisons,
                "comparisons": comparisons,
                "return_source": {
                    "expected_sha256": hashlib.sha256(returns.read_bytes()).hexdigest(),
                    "path": str(returns),
                    "source_manifest_expected_sha256": hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest(),
                    "source_manifest_path": str(manifest),
                },
                "schema_version": 2,
            }
        ),
        encoding="utf-8",
    )
    return spec, returns


def test_exact_date_metrics_costs_and_legacy_baseline_are_explicit(
    tmp_path: Path,
) -> None:
    baseline = _baseline(tmp_path)
    spec, _ = _inputs(tmp_path)

    catalog, first, _ = attribution.build_catalog(baseline, spec)
    _, second, _ = attribution.build_catalog(baseline, spec)

    assert first == second
    comparison = catalog["comparisons"][0]
    assert comparison["metrics"]["observations"] == 2
    assert comparison["metrics"]["cumulative_gap"] == "-0.02"
    assert comparison["legacy_baseline_disposition"] == "INDEPENDENTLY_REPRODUCED"
    assert catalog["costs"][0]["annual_external_cost_rate_pre_tax"] == "0.0718"
    assert catalog["costs"][0]["after_tax_status"].startswith("UNAVAILABLE")
    assert len(catalog["benchmark_assignments"]) == 9
    benchmark = catalog["benchmark_comparisons"][0]
    assert benchmark["metrics"]["observations"] == 2
    assert benchmark["metrics"]["beta"] is not None


def test_source_hash_and_strategy_boundary_fail_closed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    spec_path, returns = _inputs(tmp_path)
    returns.write_text(returns.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(attribution.AttributionValidationError, match="hash differs"):
        attribution.build_catalog(baseline, spec_path)

    spec_path, _ = _inputs(tmp_path / "boundary")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["comparisons"].pop()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(attribution.AttributionValidationError, match="exactly 9"):
        attribution.build_catalog(baseline, spec_path)


def test_stage_and_publish_are_content_addressed(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    spec, _ = _inputs(tmp_path)
    output = tmp_path / "attribution"

    staged = attribution.stage_catalog(baseline, spec, output)
    repeated = attribution.stage_catalog(baseline, spec, output)
    assert staged == repeated
    published = attribution.publish_catalog(
        baseline, spec, output, staged["generation_id"]
    )
    assert published["publication_state"] == "PUBLISHED"

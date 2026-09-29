from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

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


_NO_OVERRIDE = object()

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_attribution", ROOT / "tools" / "portfolio_attribution.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to import attribution builder")
attribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attribution
SPEC.loader.exec_module(attribution)


def _baseline(
    tmp_path: Path,
    *,
    generation_as_of_date: date = date(2026, 1, 3),
    retained_source_as_of_date: date | None = None,
    account_live_returns: dict[date, str] | None = None,
    strategy_returns: object = _NO_OVERRIDE,
    provider_admissions: list[dict[str, Any]] | None = None,
    strategy_version_refs: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "baseline"
    version_refs = strategy_version_refs or {}
    strategies = [
        {
            "name": f"Strategy {index}",
            "strategy_id": f"strategy-{index}",
            **(
                {"version_ref": version_refs[f"strategy-{index}"]}
                if f"strategy-{index}" in version_refs
                else {}
            ),
        }
        for index in range(1, 10)
    ]
    if account_live_returns is None:
        account_live_returns = {date(2026, 1, 2): "0.01", date(2026, 1, 3): "-0.02"}
    computed_strategy_returns = [
        {
            "component_account_ids": ["synthetic-account"],
            "date": day.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "synthetic",
            "return": value,
            "strategy_id": "strategy-1",
        }
        for day, value in sorted(account_live_returns.items())
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
        # Strategy 1 is the only strategy exercised with AVAILABLE_STATUSES
        # comparisons in `_inputs()`; the account side of those comparisons
        # (B3 fix) reads this ACCOUNT_LIVE series, not the return-source CSV.
        "strategy_returns": (
            computed_strategy_returns
            if strategy_returns is _NO_OVERRIDE
            else strategy_returns
        ),
        **(
            {}
            if provider_admissions is None
            else {"provider_admissions": provider_admissions}
        ),
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
    retained_source = SourceArtifact(
        artifact_id="source/retained-baseline-provenance.json",
        sha256=hashlib.sha256(b"retained-baseline-provenance").hexdigest(),
        size_bytes=len(b"retained-baseline-provenance"),
        as_of_date=retained_source_as_of_date or generation_as_of_date,
        schema_name="retained-baseline-provenance",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of_date,
        adapter=AdapterIdentity("synthetic", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("portfolio",),
            observed_start=date(2026, 1, 1),
            observed_end=date(2026, 1, 3),
        ),
        artifacts=(source, retained_source),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "portfolio-baseline.json").write_bytes(content)
    publish_generation(root, manifest.generation_id)
    return root


def _inputs(
    tmp_path: Path,
    *,
    source_manifest_as_of_date: str = "2026-01-03",
    observed_start: str = "2026-01-02",
) -> tuple[Path, Path]:
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
        json.dumps(
            {
                "artifacts": [],
                "as_of_date": source_manifest_as_of_date,
                "verification": "VERIFIED",
            }
        ),
        encoding="utf-8",
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
            "observed_start": observed_start if index == 1 else None,
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
            "observed_start": observed_start if index == 1 else None,
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


def test_account_cumulative_return_includes_a_non_benchmark_date(
    tmp_path: Path,
) -> None:
    """B3: the account side compounds its own full admitted window.

    The reference ("provider") series only has 2026-01-02 and 2026-01-03.
    The baseline's ACCOUNT_LIVE series additionally has an admitted
    2026-01-01 observation inside the (widened) comparison window. Before
    the fix, `_comparison_metrics` compounded the account only over dates
    common with the reference, so this extra date was invisible and the
    result was identical to the two-date case (cumulative_gap == "-0.02",
    exactly the value in `test_exact_date_metrics_costs_and_legacy_baseline_
    are_explicit`). After the fix, the account's own full window is used, so
    the 01-01 return changes the result.
    """

    baseline = _baseline(
        tmp_path,
        account_live_returns={
            date(2026, 1, 1): "0.03",
            date(2026, 1, 2): "0.01",
            date(2026, 1, 3): "-0.02",
        },
    )
    spec, _ = _inputs(tmp_path, observed_start="2026-01-01")

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    comparison = catalog["comparisons"][0]
    metrics = comparison["metrics"]
    # Both exact-common-date stats are unaffected: still just the two shared
    # dates with the reference series.
    assert metrics["observations"] == 2
    # The account side now compounds all three of its own admitted dates
    # (0.03, 0.01, -0.02), not just the two shared with the reference.
    assert metrics["account_cumulative_return"] == "0.019494"
    assert metrics["cumulative_gap"] == "0.009694"
    # A pre-fix compound (restricted to the two common dates) would have
    # reproduced exactly "-0.02", indistinguishable from the no-extra-date
    # case; the fix must diverge from that value.
    assert metrics["cumulative_gap"] != "-0.02"
    assert comparison["legacy_baseline_disposition"] == (
        "CORRECTED_BY_CURRENT_EXACT_DATE_SOURCE"
    )


def test_benchmark_comparison_account_cumulative_return_includes_a_non_benchmark_date(
    tmp_path: Path,
) -> None:
    """B3's actual flip target: the benchmark-comparison call site.

    Same defect as the paired `comparisons` test above, but exercised through
    `benchmark_comparisons` (tools/portfolio_attribution.py:575-579), which is
    what `returns.benchmark_account_return` reads. The reference ("benchmark")
    series only has 2026-01-02 and 2026-01-03; the account has an extra
    admitted 2026-01-01 date inside the widened window.
    """

    baseline = _baseline(
        tmp_path,
        account_live_returns={
            date(2026, 1, 1): "0.03",
            date(2026, 1, 2): "0.01",
            date(2026, 1, 3): "-0.02",
        },
    )
    spec, _ = _inputs(tmp_path, observed_start="2026-01-01")

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    benchmark_comparison = catalog["benchmark_comparisons"][0]
    metrics = benchmark_comparison["metrics"]
    # Exact-common-date stats: still just the two shared dates.
    assert metrics["observations"] == 2
    # The account side compounds all three of its own admitted dates.
    assert metrics["account_cumulative_return"] == "0.019494"
    assert metrics["cumulative_gap"] == "0.019519"
    # A pre-fix compound (restricted to the two common dates) would have
    # reproduced "-0.0102" (identical to the no-extra-date case).
    assert metrics["account_cumulative_return"] != "-0.0102"


def _strategy_return_row(day: str, value: str) -> dict[str, Any]:
    return {
        "component_account_ids": ["synthetic-account"],
        "date": day,
        "evidence_layer": "ACCOUNT_LIVE",
        "provider_id": "synthetic",
        "return": value,
        "strategy_id": "strategy-1",
    }


def test_account_live_series_rejects_non_array_strategy_returns(
    tmp_path: Path,
) -> None:
    baseline = _baseline(tmp_path, strategy_returns="not-an-array")
    spec, _ = _inputs(tmp_path)
    with pytest.raises(
        attribution.AttributionValidationError, match="must be an array"
    ):
        attribution.build_catalog(baseline, spec)


def test_account_live_series_rejects_return_at_or_below_negative_one(
    tmp_path: Path,
) -> None:
    baseline = _baseline(
        tmp_path, strategy_returns=[_strategy_return_row("2026-01-02", "-1")]
    )
    spec, _ = _inputs(tmp_path)
    with pytest.raises(attribution.AttributionValidationError, match="greater than -1"):
        attribution.build_catalog(baseline, spec)


def test_account_live_series_rejects_conflicting_dates(tmp_path: Path) -> None:
    baseline = _baseline(
        tmp_path,
        strategy_returns=[
            _strategy_return_row("2026-01-02", "0.01"),
            _strategy_return_row("2026-01-02", "0.02"),
        ],
    )
    spec, _ = _inputs(tmp_path)
    with pytest.raises(
        attribution.AttributionValidationError, match="conflicting dates"
    ):
        attribution.build_catalog(baseline, spec)


def test_account_live_series_accepts_repeated_identical_rows_and_boundary_return(
    tmp_path: Path,
) -> None:
    """The good counterpart to the three bad fixtures above: an array of

    rows, a repeated row with the *same* value (not a conflict), and a
    return just above -1, are all accepted.
    """

    baseline = _baseline(
        tmp_path,
        strategy_returns=[
            _strategy_return_row("2026-01-02", "-0.999999"),
            _strategy_return_row("2026-01-02", "-0.999999"),
            _strategy_return_row("2026-01-03", "-0.02"),
        ],
    )
    spec, _ = _inputs(tmp_path)

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    assert catalog["comparisons"][0]["metrics"]["observations"] == 2


# The EchoTrade shape of the pinned attribution spec (sha256 230f2674...): an
# EVIDENCED benchmark and one AVAILABLE benchmark comparison whose
# account_series_id names a retained ACCOUNT_LIVE series, an UNAVAILABLE
# provider comparison, no ACCOUNT_LIVE row in baseline strategy_returns, and a
# single REFERENCE_ONLY strategy_returns admission in provider_admissions.
_HISTORY_STRATEGY = "strategy-2"
_HISTORY_SERIES = "account:strategy-2"
_HISTORY_VERSION = "accepted-live-path:" + "2" * 64


def _admission(
    disposition: str = "REFERENCE_ONLY", strategy_id: str = _HISTORY_STRATEGY
) -> dict[str, Any]:
    return {
        "account_id": "synthetic-history-account",
        "account_reconciliation_sha256": "0" * 64,
        "disposition": disposition,
        "effective_end": "2026-01-03",
        "effective_start": "2026-01-01",
        "evidence_layer": "ACCOUNT_LIVE",
        "fact_type": "strategy_returns",
        "pool_id": None,
        "provider_id": "synthetic",
        "strategy_id": strategy_id,
    }


def _history_only_baseline(
    tmp_path: Path,
    *,
    admissions: list[dict[str, Any]] | None = None,
    version_refs: dict[str, str] | None = None,
) -> Path:
    return _baseline(
        tmp_path,
        provider_admissions=[_admission()] if admissions is None else admissions,
        strategy_version_refs=(
            {_HISTORY_STRATEGY: _HISTORY_VERSION}
            if version_refs is None
            else version_refs
        ),
    )


def _with_history_only_strategy(
    spec_path: Path,
    returns: Path,
    *,
    account_series_id: str = _HISTORY_SERIES,
    evidence_layer: str = "ACCOUNT_LIVE",
    version_ref: str = _HISTORY_VERSION,
) -> None:
    """Give strategy-2 the EchoTrade rows of the pinned spec, re-pinning the CSV."""

    with returns.open("a", encoding="utf-8") as handle:
        for day, value in (
            ("2026-01-01", "0.002"),
            ("2026-01-02", "0.004"),
            ("2026-01-03", "-0.003"),
        ):
            handle.write(
                f"{_HISTORY_SERIES},{day},{value},{evidence_layer},"
                f"{version_ref},history-source\n"
            )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["benchmark_assignments"][1].update(
        benchmark_id="benchmark",
        rationale="Synthetic effective-dated benchmark of a retired strategy.",
        status="EVIDENCED",
        version_ref="benchmark:v1",
    )
    spec["benchmark_comparisons"][1].update(
        account_series_id=account_series_id,
        disposition="Synthetic exact-date comparison of retained account history.",
        observed_end="2026-01-03",
        observed_start="2026-01-01",
        reference_series_id="benchmark",
        status="DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE",
        version_compatibility_status="PROVEN_COMPATIBLE",
    )
    spec["return_source"]["expected_sha256"] = hashlib.sha256(
        returns.read_bytes()
    ).hexdigest()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


def test_history_only_strategy_benchmark_comparison_reads_its_retained_history(
    tmp_path: Path,
) -> None:
    """RP1: a retired strategy's evidenced comparison no longer aborts the build.

    Before the fix the account side came only from baseline ACCOUNT_LIVE rows,
    so the EchoTrade-shaped row raised "comparison series is missing" and no
    attribution generation could be built on the pinned spec. Its account side
    is now its retained history, disclosed in the row, through the same
    full-window metrics as a live series.
    """

    baseline = _history_only_baseline(tmp_path)
    spec, returns = _inputs(tmp_path)
    _with_history_only_strategy(spec, returns)

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    history = catalog["benchmark_comparisons"][1]
    assert history["strategy_id"] == _HISTORY_STRATEGY
    assert history["account_series_source"] == (
        "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY"
    )
    # 1.002 * 1.004 * 0.997 - 1 over the whole window; the benchmark has only
    # 2026-01-02 and 2026-01-03 (1.005 * 0.995 - 1 = -0.000025).
    assert history["metrics"]["account_cumulative_return"] == "0.002989976"
    assert history["metrics"]["reference_cumulative_return"] == "-0.000025"
    assert history["metrics"]["cumulative_gap"] == "0.003014976"
    assert history["metrics"]["observations"] == 2
    assert catalog["benchmark_comparisons"][0]["account_series_source"] == (
        "BASELINE_ACCOUNT_LIVE"
    )
    assert catalog["comparisons"][0]["account_series_source"] == (
        "BASELINE_ACCOUNT_LIVE"
    )
    assert catalog["comparisons"][1]["account_series_source"] is None
    assert catalog["comparisons"][1]["metrics"] is None


def test_strategy_with_live_rows_never_reads_its_retained_account_series(
    tmp_path: Path,
) -> None:
    """No mixing: a strategy with any live row never reads the retained file.

    strategy-1 also carries everything the retained-history path accepts (a
    REFERENCE_ONLY admission and the retained "account" series' exact version),
    yet its account side stays the live series (1.03 * 0.99 - 1 = 0.0197), not
    the retained one (1.01 * 0.98 - 1 = -0.0102).
    """

    baseline = _baseline(
        tmp_path,
        account_live_returns={date(2026, 1, 2): "0.03", date(2026, 1, 3): "-0.01"},
        provider_admissions=[_admission(strategy_id="strategy-1")],
        strategy_version_refs={"strategy-1": "v1"},
    )
    spec, _ = _inputs(tmp_path)

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    for row in (catalog["comparisons"][0], catalog["benchmark_comparisons"][0]):
        assert row["account_series_source"] == "BASELINE_ACCOUNT_LIVE"
        assert row["metrics"]["account_cumulative_return"] == "0.0197"


def test_live_rows_outside_the_window_fail_closed_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    baseline = _baseline(
        tmp_path,
        account_live_returns={date(2025, 12, 30): "0.01", date(2025, 12, 31): "0.02"},
        provider_admissions=[_admission(strategy_id="strategy-1")],
        strategy_version_refs={"strategy-1": "v1"},
    )
    spec, _ = _inputs(tmp_path)

    with pytest.raises(
        attribution.AttributionValidationError, match="two exact common dates"
    ):
        attribution.build_catalog(baseline, spec)


def test_history_only_strategy_without_its_retained_series_fails_closed(
    tmp_path: Path,
) -> None:
    baseline = _history_only_baseline(tmp_path)
    spec, returns = _inputs(tmp_path)
    _with_history_only_strategy(spec, returns, account_series_id="account:absent")

    with pytest.raises(
        attribution.AttributionValidationError, match="comparison series is missing"
    ):
        attribution.build_catalog(baseline, spec)


@pytest.mark.parametrize(
    "admissions",
    [
        [],
        [_admission("AUTHORITATIVE")],
        [_admission("REFERENCE_ONLY"), _admission("AUTHORITATIVE")],
    ],
    ids=["no-admission", "authoritative", "mixed"],
)
def test_missing_live_series_without_history_only_admission_fails_closed(
    tmp_path: Path, admissions: list[dict[str, Any]]
) -> None:
    """A strategy admitted as live whose rows are missing never falls back."""

    baseline = _history_only_baseline(tmp_path, admissions=admissions)
    spec, returns = _inputs(tmp_path)
    _with_history_only_strategy(spec, returns)

    with pytest.raises(
        attribution.AttributionValidationError, match="comparison series is missing"
    ):
        attribution.build_catalog(baseline, spec)


@pytest.mark.parametrize(
    ("evidence_layer", "series_version", "registered_versions", "message"),
    [
        ("SHADOW_OOS", _HISTORY_VERSION, None, "identity differs"),
        ("ACCOUNT_LIVE", "accepted-live-path:" + "3" * 64, None, "identity differs"),
        ("ACCOUNT_LIVE", _HISTORY_VERSION, {}, "comparison series is missing"),
    ],
    ids=["not-account-live", "other-version", "no-registered-version"],
)
def test_retained_account_history_identity_fails_closed(
    tmp_path: Path,
    evidence_layer: str,
    series_version: str,
    registered_versions: dict[str, str] | None,
    message: str,
) -> None:
    """The retained series must be the ACCOUNT_LIVE path the baseline registers."""

    baseline = _history_only_baseline(tmp_path, version_refs=registered_versions)
    spec, returns = _inputs(tmp_path)
    _with_history_only_strategy(
        spec, returns, evidence_layer=evidence_layer, version_ref=series_version
    )

    with pytest.raises(attribution.AttributionValidationError, match=message):
        attribution.build_catalog(baseline, spec)


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
    generation = json.loads(
        (
            output / "generations" / staged["generation_id"] / "generation.json"
        ).read_text()
    )
    assert generation["coverage"]["boundary"] == (
        "nine retained registry strategies (seven REQUIRED active current, Echo "
        "DEPRECATED_HISTORY_ONLY, Composer IMMATERIAL_EXCLUDED) with exact-date metrics "
        "or explicit unavailable dispositions"
    )
    published = attribution.publish_catalog(
        baseline, spec, output, staged["generation_id"]
    )
    assert published["publication_state"] == "PUBLISHED"


def test_attribution_preserves_baseline_financial_cutoff_with_later_provenance(
    tmp_path: Path,
) -> None:
    """Attribution receives the baseline's provenance date without relabeling its catalog."""

    baseline_root = _baseline(tmp_path, generation_as_of_date=date(2026, 1, 4))
    spec, _ = _inputs(tmp_path, source_manifest_as_of_date="2026-01-05")
    output = tmp_path / "attribution"

    staged = attribution.stage_catalog(baseline_root, spec, output)
    generation = json.loads(
        (
            output / "generations" / staged["generation_id"] / "generation.json"
        ).read_text(encoding="utf-8")
    )
    catalog = json.loads(
        (
            output
            / "generations"
            / staged["generation_id"]
            / "strategy-attribution.json"
        ).read_text(encoding="utf-8")
    )

    assert catalog["as_of_date"] == "2026-01-03"
    assert generation["as_of_date"] == "2026-01-05"
    artifact_dates = {
        item["artifact_id"]: item["as_of_date"] for item in generation["artifacts"]
    }
    assert artifact_dates["input/portfolio-baseline.json"] == "2026-01-03"
    assert artifact_dates["input/portfolio-baseline-generation.json"] == "2026-01-04"


def test_attribution_rejects_baseline_generation_date_not_derived_from_artifacts(
    tmp_path: Path,
) -> None:
    """A coherently rehashed baseline manifest cannot carry an arbitrary outer date."""

    baseline_root = _baseline(
        tmp_path,
        generation_as_of_date=date(2026, 1, 5),
        retained_source_as_of_date=date(2026, 1, 4),
    )
    spec, _ = _inputs(tmp_path)

    with pytest.raises(
        attribution.AttributionValidationError,
        match="baseline generation date is not artifact-derived",
    ):
        attribution.stage_catalog(baseline_root, spec, tmp_path / "attribution")

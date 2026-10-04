from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
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


# T6-F2: a reference coarser than daily. Each Friday return of the "weekly"
# reference covers (previous Friday, Friday], so the account's daily returns
# inside that period are compounded before the exact-date pairing. Strategy-1
# also keeps the two default rows that its daily benchmark comparison reads.
_WEEKLY_ACCOUNT_RETURNS = {
    date(2026, 1, 2): "0.01",
    date(2026, 1, 3): "-0.02",
    date(2026, 1, 8): "0.40",  # before the first period
    date(2026, 1, 9): "0.50",  # on the previous reference date: not in (prev, day]
    date(2026, 1, 12): "0.10",  # before observed_start, yet in the first period
    date(2026, 1, 16): "0.10",
    date(2026, 1, 19): "0.10",
    date(2026, 1, 23): "-0.10",
    date(2026, 1, 26): "0.20",
    date(2026, 1, 30): "-0.10",
    date(2026, 2, 2): "0.30",  # after the last reference date
}
_WEEKLY_REFERENCE_RETURNS = {
    "2026-01-16": "0.20",
    "2026-01-23": "-0.10",
    "2026-01-30": "0.10",
}


_RECEIPT_ACCOUNT_RETURNS = {
    date(2026, 1, 12): "0.10",
    date(2026, 1, 15): "0.01",
    date(2026, 1, 22): "0",
}


def _epoch_receipt(
    *,
    account_returns: dict[date, str] = _RECEIPT_ACCOUNT_RETURNS,
    span_start: str = "2026-01-12",
    span_end: str = "2026-01-14",
    daily_cumulative_return: str = "0.111",
    epoch_cumulative_return: str = "0.3332",
) -> dict[str, Any]:
    """An admitted source receipt whose data hash binds the supplied daily rows."""

    daily_rows = [
        {
            "date": day.isoformat(),
            "return": (
                "0" if Decimal(value) == 0 else format(Decimal(value).normalize(), "f")
            ),
        }
        for day, value in sorted(account_returns.items())
    ]
    receipt = {
        "admission_cutoff": daily_rows[-1]["date"],
        "daily_cumulative_return": daily_cumulative_return,
        "daily_return_observations": len(daily_rows),
        "daily_return_series_schema": "ordered-date-return/1",
        "daily_return_series_sha256": hashlib.sha256(
            canonical_json_bytes(daily_rows)
        ).hexdigest(),
        "epoch_cumulative_return": epoch_cumulative_return,
        "epoch_holding_period_link_observations": len(daily_rows) + 1,
        "observed_end": daily_rows[-1]["date"],
        "observed_start": daily_rows[0]["date"],
        "source_gap_policy_sha256": "2" * 64,
        "span_return_receipts": [
            {
                "end_endpoint_sha256": "3" * 64,
                "missing_source_date": (
                    date.fromisoformat(span_start) + timedelta(days=1)
                ).isoformat(),
                "return": "0.20",
                "span_days": 2,
                "span_end_date": span_end,
                "span_start_date": span_start,
                "start_endpoint_sha256": "4" * 64,
                "subscription_events_in_span": 0,
            }
        ],
    }
    return {
        "evidence_layer": "ACCOUNT_LIVE",
        "receipt": receipt,
        "source_generation_id": "5" * 64,
        "source_id": "matrixport-account-generation",
        "strategy_id": "strategy-1",
    }


def _dependence_selected(tmp_path, baseline, monkeypatch, receipt):
    """Exercise attribution's authority boundary with a verified-service test double.

    Real immutable package verification is separately replayed without this double.
    No unsupported fields are written into the published baseline catalog.
    """
    source = tmp_path / "dependence"
    source.mkdir(exist_ok=True)
    coverage = canonical_json_bytes({"epoch_hpr_receipts": [receipt]})
    (source / "history-coverage.json").write_bytes(coverage)
    (source / "dependence-source.json").write_bytes(
        canonical_json_bytes({"schema_version": 2})
    )
    contract = tmp_path / "financial-contract.json"
    contract.write_text("{}", encoding="utf-8")
    selected = tmp_path / "dependence-selected-inputs.json"
    baseline_path = attribution._baseline(baseline)[2].parent
    ledger = tmp_path / "ledger"
    ledger.mkdir(exist_ok=True)
    (ledger / "provider-ledger.json").write_bytes(
        canonical_json_bytes(
            {
                "provider_generations": [
                    {"provider_id": "matrixport", "generation_id": "5" * 64}
                ],
                "strategy_returns": attribution._baseline(baseline)[0][
                    "strategy_returns"
                ],
            }
        )
    )
    selected.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "allocation_generation_root": str(baseline_path),
                "financial_input_contract_path": str(contract),
                "provider_ledger_generation_root": str(ledger),
            }
        )
    )

    def verify(root, *, contract_path, policy_path, selected_inputs_path):
        assert root == source
        assert contract_path == contract
        assert policy_path == ROOT / "contracts" / "portfolio-dependence-policy-v1.json"
        assert selected_inputs_path == selected
        if (source / "history-coverage.json").read_bytes() != coverage:
            raise ValueError("history coverage differs")
        return {"verification": "VALID", "epoch_hpr_receipts": [receipt]}

    monkeypatch.setattr(attribution, "_verify_dependence_source", verify)
    return {"dependence_source_root": source, "dependence_selected_inputs": selected}


def test_receipt_bound_coarse_period_restores_exact_headline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash-bound span restores endpoint wealth without changing daily rows."""

    baseline = _baseline(
        tmp_path,
        account_live_returns=_RECEIPT_ACCOUNT_RETURNS,
    )
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec, returns, {"2026-01-11": "0", "2026-01-15": "0", "2026-01-22": "0"}
    )

    catalog, _, _ = attribution.build_catalog(
        baseline,
        spec,
        **_dependence_selected(tmp_path, baseline, monkeypatch, _epoch_receipt()),
    )

    for comparison in (
        catalog["comparisons"][0],
        catalog["benchmark_comparisons"][0],
    ):
        assert comparison["status"] in attribution.AVAILABLE_STATUSES
        assert comparison["metrics"]["observations"] == 2
        # 1.10 * 1.20 * 1.01 - 1: daily + HPR gap + daily.
        assert comparison["metrics"]["account_cumulative_return"] == "0.3332"

    raw_daily = attribution._account_live_series(attribution._baseline(baseline)[0])[
        "strategy-1"
    ]
    assert raw_daily == {
        date(2026, 1, 12): Decimal("0.10"),
        date(2026, 1, 15): Decimal("0.01"),
        date(2026, 1, 22): Decimal("0"),
    }
    # The retained daily path is still exactly 1.10 * 1.01 - 1.
    assert Decimal("1.10") * Decimal("1.01") - Decimal("1") == Decimal("0.111")


@pytest.mark.parametrize(
    "receipt_variant, message",
    [
        ("missing", "epoch HPR receipt keys differ"),
        ("altered", "epoch HPR daily path differs"),
        ("mismatched", "epoch HPR receipt authority differs"),
    ],
)
def test_receipt_bound_coarse_period_rejects_missing_altered_or_mismatched_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_variant: str, message: str
) -> None:
    """The report-reader binding rejects absent, tampered, and foreign receipts."""

    receipt = _epoch_receipt()
    if receipt_variant == "missing":
        receipt.pop("receipt")
    elif receipt_variant == "altered":
        # This is deliberately independent from the crossing-boundary case.
        receipt["receipt"]["daily_return_series_sha256"] = "0" * 64
    else:
        receipt["source_id"] = "unadmitted-source"
    baseline = _baseline(
        tmp_path,
        account_live_returns=_RECEIPT_ACCOUNT_RETURNS,
    )
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec, returns, {"2026-01-11": "0", "2026-01-15": "0", "2026-01-22": "0"}
    )

    with pytest.raises(attribution.AttributionValidationError, match=message):
        attribution.build_catalog(
            baseline,
            spec,
            **_dependence_selected(tmp_path, baseline, monkeypatch, receipt),
        )


def test_receipt_bound_crossing_reference_boundary_is_not_ready_and_preserves_daily_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid changed span that cuts a reference period is not silently sliced."""

    changed_daily = {
        date(2026, 1, 12): "0.10",
        date(2026, 1, 13): "0.10",
        date(2026, 1, 16): "0.01",
    }
    # Build the changed receipt afresh: its daily-series hash now binds these
    # changed observations and the valid (2026-01-13, 2026-01-15] HPR link.
    receipt = _epoch_receipt(
        account_returns=changed_daily,
        span_start="2026-01-13",
        span_end="2026-01-15",
        daily_cumulative_return="0.2221",
        epoch_cumulative_return="0.46652",
    )
    baseline = _baseline(tmp_path, account_live_returns=changed_daily)
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec,
        returns,
        {"2026-01-11": "0", "2026-01-14": "0"},
        observed_start="2026-01-14",
        observed_end="2026-01-14",
    )

    # Establish receipt validity before testing the independent reference boundary.
    attribution._validated_account_live_return_series(
        {**attribution._baseline(baseline)[0], "epoch_hpr_receipts": [receipt]}
    )
    catalog, _, _ = attribution.build_catalog(
        baseline, spec, **_dependence_selected(tmp_path, baseline, monkeypatch, receipt)
    )

    for comparison in (
        catalog["comparisons"][0],
        catalog["benchmark_comparisons"][0],
    ):
        assert comparison["status"] == attribution.NOT_READY_STATUS
        assert (
            comparison["not_ready_reason"]
            == "RECEIPT_BOUND_COARSE_PERIOD_CUTS_HPR_SPAN"
        )
        assert comparison["metrics"] is None
    assert attribution._account_live_series(attribution._baseline(baseline)[0])[
        "strategy-1"
    ] == {day: Decimal(value) for day, value in changed_daily.items()}


def _with_weekly_reference(
    spec_path: Path,
    returns: Path,
    reference_returns: dict[str, str],
    *,
    periods_per_year: int = 52,
    observed_end: str = "2026-01-30",
    observed_start: str = "2026-01-16",
) -> None:
    """Point strategy-1's provider comparison at a weekly reference series."""

    with returns.open("a", encoding="utf-8") as handle:
        for day, value in reference_returns.items():
            handle.write(f"weekly,{day},{value},PROVIDER_LIVE,v1,weekly-source\n")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["comparisons"][0].update(
        expected_legacy_cumulative_gap="0.105732",
        observed_end=observed_end,
        observed_start=observed_start,
        periods_per_year=periods_per_year,
        reference_series_id="weekly",
    )
    spec["return_source"]["expected_sha256"] = hashlib.sha256(
        returns.read_bytes()
    ).hexdigest()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


def _with_receipt_bound_coarse_reference(
    spec_path: Path,
    returns: Path,
    reference_returns: dict[str, str],
    *,
    observed_start: str = "2026-01-15",
    observed_end: str = "2026-01-22",
) -> None:
    """Bind both real comparison consumers to the same coarse source period."""

    _with_weekly_reference(
        spec_path,
        returns,
        reference_returns,
        observed_start=observed_start,
        observed_end=observed_end,
    )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["benchmark_assignments"][0]["benchmark_id"] = "weekly"
    spec["benchmark_comparisons"][0].update(
        observed_end=observed_end,
        observed_start=observed_start,
        periods_per_year=52,
        reference_series_id="weekly",
    )
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


@pytest.mark.parametrize(
    "previous_reference_date",
    [True, False],
    ids=["previous-date-outside-window", "first-date-of-series"],
)
def test_weekly_reference_pairs_the_account_compounded_over_each_period(
    tmp_path: Path, previous_reference_date: bool
) -> None:
    """T6-F2: a weekly reference return meets the account's weekly compound.

    Before the fix the account's one-day return on each Friday (0.10, -0.10,
    -0.10) was paired with the weekly reference return (0.20, -0.10, 0.10): two
    horizons in one active return. Now the account's own returns inside each
    (previous Friday, Friday] period are compounded first: 1.1 * 1.1 - 1 = 0.21,
    1.1 * 0.9 - 1 = -0.01 and 1.2 * 0.9 - 1 = 0.08. The first period reaches back
    before observed_start. It opens at the previous reference date, 2026-01-09
    (outside the window), or, when the series starts at 2026-01-16, it is as long
    as the second period. Rows on or before the opening date and rows after the
    last reference date belong to no period.
    """

    baseline = _baseline(tmp_path, account_live_returns=_WEEKLY_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    reference = dict(_WEEKLY_REFERENCE_RETURNS)
    if previous_reference_date:
        reference = {"2026-01-09": "0.05", **reference}
    _with_weekly_reference(spec, returns, reference)

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    comparison = catalog["comparisons"][0]
    metrics = comparison["metrics"]
    assert metrics["observations"] == 3
    assert metrics["observed_start"] == "2026-01-16"
    assert metrics["observed_end"] == "2026-01-30"
    # 1.21 * 0.99 * 1.08 - 1 for the account, 1.2 * 0.9 * 1.1 - 1 for the reference.
    assert metrics["account_cumulative_return"] == "0.293732"
    assert metrics["reference_cumulative_return"] == "0.188"
    assert metrics["cumulative_gap"] == "0.105732"
    # (0.21 - 0.20, -0.01 + 0.10, 0.08 - 0.10) at 52 periods a year.
    active = [0.01, 0.09, -0.02]
    assert float(metrics["active_return_annualized"]) == pytest.approx(
        statistics.fmean(active) * 52
    )
    assert float(metrics["tracking_error_annualized"]) == pytest.approx(
        statistics.stdev(active) * math.sqrt(52)
    )
    assert comparison["legacy_baseline_disposition"] == "INDEPENDENTLY_REPRODUCED"
    assert comparison["legacy_baseline_delta"] == "0"
    # The strategy's daily benchmark comparison keeps exact-date pairing.
    assert catalog["benchmark_comparisons"][0]["metrics"]["observations"] == 2


@pytest.mark.parametrize("periods_per_year", [252, 365])
def test_daily_periods_per_year_keeps_exact_date_pairing(
    tmp_path: Path, periods_per_year: int
) -> None:
    """T6-F2 changes only a comparison that declares a cadence coarser than daily.

    The same rows declared at 252 or more periods a year are paired on exact
    dates as before: the account's return on each reference date meets the
    reference return, and the account compounds every row of the window.
    """

    baseline = _baseline(tmp_path, account_live_returns=_WEEKLY_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    _with_weekly_reference(
        spec, returns, _WEEKLY_REFERENCE_RETURNS, periods_per_year=periods_per_year
    )

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    metrics = catalog["comparisons"][0]["metrics"]
    assert metrics["observations"] == 3
    # Rows in [2026-01-16, 2026-01-30]: 1.1 * 1.1 * 0.9 * 1.2 * 0.9 - 1.
    assert metrics["account_cumulative_return"] == "0.17612"
    # (0.10 - 0.20, -0.10 + 0.10, -0.10 - 0.10) on the three reference dates.
    active = [-0.10, 0.0, -0.20]
    assert float(metrics["active_return_annualized"]) == pytest.approx(
        statistics.fmean(active) * periods_per_year
    )


def test_reference_period_without_account_returns_is_not_paired(
    tmp_path: Path,
) -> None:
    """A reference date whose period holds no account return yields no pair.

    Nothing is invented for 2026-01-30, whose period (01-23, 01-30] has no
    account row: three observations remain and the account compounds only what
    it has (0.21, -0.01 and, for 2026-02-06, 1.05 * 1.05 - 1 = 0.1025).
    """

    baseline = _baseline(
        tmp_path,
        account_live_returns={
            date(2026, 1, 2): "0.01",
            date(2026, 1, 3): "-0.02",
            date(2026, 1, 12): "0.10",
            date(2026, 1, 16): "0.10",
            date(2026, 1, 19): "0.10",
            date(2026, 1, 23): "-0.10",
            date(2026, 2, 5): "0.05",
            date(2026, 2, 6): "0.05",
        },
    )
    spec, returns = _inputs(tmp_path)
    _with_weekly_reference(
        spec,
        returns,
        {**_WEEKLY_REFERENCE_RETURNS, "2026-02-06": "0.04"},
        observed_end="2026-02-06",
    )

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    metrics = catalog["comparisons"][0]["metrics"]
    assert metrics["observations"] == 3
    assert metrics["observed_end"] == "2026-02-06"
    # 1.21 * 0.99 * 1.1025 - 1
    assert metrics["account_cumulative_return"] == "0.32068475"


def test_weekly_period_opens_at_the_previous_reference_date_outside_the_window(
    tmp_path: Path,
) -> None:
    """The opening date is the previous date of the whole series, not a guess.

    The reference has a week end on Thursday 2026-01-08 (outside the window), so
    the first period is (01-08, 01-16] and holds the 2026-01-09 row too:
    1.5 * 1.1 * 1.1 - 1 = 0.815. Assuming a period as long as the next one would
    open at 01-09 and give 0.21 instead.
    """

    baseline = _baseline(tmp_path, account_live_returns=_WEEKLY_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    _with_weekly_reference(
        spec, returns, {"2026-01-08": "0.05", **_WEEKLY_REFERENCE_RETURNS}
    )

    catalog, _, _ = attribution.build_catalog(baseline, spec)

    metrics = catalog["comparisons"][0]["metrics"]
    assert metrics["observations"] == 3
    # 1.815 * 0.99 * 1.08 - 1
    assert metrics["account_cumulative_return"] == "0.940598"


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


def test_staged_catalog_matches_verified_baseline_in_guard_and_report_reader(
    tmp_path: Path,
) -> None:
    """The real producer output passes both consumers and rejects a stale baseline."""
    import portfolio_release_builder as release
    import portfolio_report as report

    baseline_root = _baseline(tmp_path / "selected")
    baseline_identity = attribution._baseline(baseline_root)[1]
    spec, _returns = _inputs(tmp_path / "inputs")
    attribution_root = tmp_path / "attribution"
    staged = attribution.stage_catalog(baseline_root, spec, attribution_root)
    generation_id = staged["generation_id"]

    release._validate_strategy_attribution_baseline_binding(
        attribution_root,
        generation_id,
        baseline_root,
        baseline_identity["generation_id"],
    )
    catalog, identity, _catalog_path = report._attribution(
        attribution_root,
        baseline_identity,
        attribution_generation_id=generation_id,
    )
    assert catalog["baseline"] == baseline_identity
    assert identity["generation_id"] == generation_id

    stale_baseline_root = _baseline(
        tmp_path / "stale",
        account_live_returns={date(2026, 1, 2): "0.02", date(2026, 1, 3): "-0.03"},
    )
    stale_baseline_identity = attribution._baseline(stale_baseline_root)[1]
    assert stale_baseline_identity != baseline_identity
    with pytest.raises(
        release.VerticalBuildError,
        match="baseline identity differs from staged baseline",
    ):
        release._validate_strategy_attribution_baseline_binding(
            attribution_root,
            generation_id,
            stale_baseline_root,
            stale_baseline_identity["generation_id"],
        )
    with pytest.raises(report.ReportValidationError, match="baseline identity differs"):
        report._attribution(
            attribution_root,
            stale_baseline_identity,
            attribution_generation_id=generation_id,
        )


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


@pytest.mark.parametrize("frequency", [252, 52])
def test_dependence_selected_actual_stage_publish_preserves_raw_statistics(
    tmp_path, monkeypatch, frequency
):
    baseline = _baseline(tmp_path, account_live_returns=_RECEIPT_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec,
        returns,
        {"2026-01-11": "0", "2026-01-12": "0", "2026-01-15": "0", "2026-01-22": "0"},
        observed_start="2026-01-12",
    )
    if frequency == 252:
        value = json.loads(spec.read_text())
        for name in ("comparisons", "benchmark_comparisons"):
            value[name][0]["periods_per_year"] = 252
        spec.write_text(json.dumps(value))
    args = _dependence_selected(tmp_path, baseline, monkeypatch, _epoch_receipt())
    output = tmp_path / "output"
    cli = [
        "--baseline-root",
        str(baseline),
        "--spec",
        str(spec),
        "--output-root",
        str(output),
        "--dependence-source-root",
        str(args["dependence_source_root"]),
        "--dependence-selected-inputs",
        str(args["dependence_selected_inputs"]),
    ]
    assert attribution.main(["stage", *cli]) == 0
    generation = next((output / "generations").iterdir())
    assert attribution.main(["publish", *cli, "--generation-id", generation.name]) == 0
    catalog = json.loads((generation / attribution.CATALOG_NAME).read_text())
    for name in ("comparisons", "benchmark_comparisons"):
        assert catalog[name][0]["metrics"]["account_cumulative_return"] == "0.3332"
        assert catalog[name][0]["metrics"]["cumulative_gap"] == "0.3332"
        if frequency == 252:
            assert catalog[name][0]["metrics"]["observations"] == 3
            assert catalog[name][0]["metrics"][
                "active_return_annualized"
            ] == attribution._decimal_text(statistics.fmean([0.10, 0.01, 0]) * 252)
    artifact_ids = {
        item["artifact_id"]
        for item in json.loads((generation / "generation.json").read_text())[
            "artifacts"
        ]
    }
    assert "input/dependence-selected-inputs.json" in artifact_ids
    assert "input/dependence-source.json" in artifact_ids
    assert "input/dependence-history-coverage.json" in artifact_ids


@pytest.mark.parametrize(
    "attack",
    ["half-source", "half-selected", "baseline", "tamper", "missing-matrixport"],
)
def test_dependence_selected_rejects_before_stage_write(tmp_path, monkeypatch, attack):
    baseline = _baseline(tmp_path, account_live_returns=_RECEIPT_ACCOUNT_RETURNS)
    spec, _ = _inputs(tmp_path)
    args = _dependence_selected(tmp_path, baseline, monkeypatch, _epoch_receipt())
    if attack == "half-source":
        args.pop("dependence_selected_inputs")
    elif attack == "half-selected":
        args.pop("dependence_source_root")
    elif attack == "baseline":
        value = json.loads(args["dependence_selected_inputs"].read_text())
        value["allocation_generation_root"] = str(tmp_path / "other")
        args["dependence_selected_inputs"].write_text(json.dumps(value))
    elif attack == "tamper":
        (args["dependence_source_root"] / "history-coverage.json").write_text("{}")
    else:
        # A receipt-capable provider cannot silently take the legacy raw path.
        original = attribution._baseline

        def read(*a, **kw):
            catalog, identity, path, artifact = original(*a, **kw)
            for row in catalog["strategy_returns"]:
                row["provider_id"] = "matrixport"
            return catalog, identity, path, artifact

        monkeypatch.setattr(attribution, "_baseline", read)
        args = {}
    output = tmp_path / "output"
    with pytest.raises(attribution.AttributionValidationError):
        attribution.stage_catalog(baseline, spec, output, **args)
    assert not output.exists()


@pytest.mark.parametrize("attack", ["foreign-generation", "foreign-account", "overlap"])
def test_dependence_selected_receipt_identity_and_overlap_fail_closed(
    tmp_path, monkeypatch, attack
):
    baseline = _baseline(tmp_path, account_live_returns=_RECEIPT_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec, returns, {"2026-01-11": "0", "2026-01-15": "0", "2026-01-22": "0"}
    )
    receipt = _epoch_receipt()
    attribution._validated_account_live_return_series(
        {**attribution._baseline(baseline)[0], "epoch_hpr_receipts": [receipt]}
    )
    if attack == "foreign-generation":
        receipt["source_generation_id"] = "6" * 64
    elif attack == "overlap":
        receipt["receipt"]["span_return_receipts"] *= 2
        receipt["receipt"]["epoch_holding_period_link_observations"] += 1
    args = _dependence_selected(tmp_path, baseline, monkeypatch, receipt)
    if attack == "foreign-account":
        ledger_path = tmp_path / "ledger" / "provider-ledger.json"
        value = json.loads(ledger_path.read_text())
        for row in value["strategy_returns"]:
            row["component_account_ids"] = ["other-account"]
        ledger_path.write_text(json.dumps(value))
    message = {
        "foreign-generation": "selected provider generation",
        "foreign-account": "account identity",
        "overlap": "epoch HPR span differs",
    }[attack]
    with pytest.raises(attribution.AttributionValidationError, match=message):
        attribution.stage_catalog(baseline, spec, tmp_path / "output", **args)
    assert not (tmp_path / "output").exists()


def test_receipt_bound_first_reference_opening_is_not_inferred(tmp_path, monkeypatch):
    baseline = _baseline(tmp_path, account_live_returns=_RECEIPT_ACCOUNT_RETURNS)
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec, returns, {"2026-01-15": "0", "2026-01-22": "0"}
    )
    args = _dependence_selected(tmp_path, baseline, monkeypatch, _epoch_receipt())
    catalog, _, _ = attribution.build_catalog(baseline, spec, **args)
    for name in ("comparisons", "benchmark_comparisons"):
        assert catalog[name][0]["status"] == "NOT_READY"
        assert (
            catalog[name][0]["not_ready_reason"]
            == "RECEIPT_BOUND_REFERENCE_OPENING_UNPROVEN"
        )
        assert catalog[name][0]["metrics"] is None


def test_dependence_selected_invalid_package_uses_real_verifier_before_write(tmp_path):
    baseline = _baseline(tmp_path, account_live_returns=_RECEIPT_ACCOUNT_RETURNS)
    spec, _ = _inputs(tmp_path)
    source = tmp_path / "invalid-package"
    source.mkdir()
    for name in (
        "dependence-source.json",
        "history-coverage.json",
        "provider-ledger.json",
        "contract.json",
    ):
        (source / name).write_text("{}")
    selected = tmp_path / "selected.json"
    selected.write_text(
        json.dumps(
            {
                "allocation_generation_root": str(
                    attribution._baseline(baseline)[2].parent
                ),
                "financial_input_contract_path": str(source / "contract.json"),
                "provider_ledger_generation_root": str(source),
            }
        )
    )
    with pytest.raises(
        attribution.AttributionValidationError,
        match="selected dependence authority is invalid",
    ):
        attribution.stage_catalog(
            baseline,
            spec,
            tmp_path / "output",
            dependence_source_root=source,
            dependence_selected_inputs=selected,
        )
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("end,expected", [("2026-01-14", "0.32"), ("2026-01-13", None)])
def test_receipt_bound_daily_exact_endpoint_or_cut_is_explicit(
    tmp_path, monkeypatch, end, expected
):
    daily = {
        date(2026, 1, 11): "0.10",
        date(2026, 1, 12): "0",
        date(2026, 1, 15): "0.01",
    }
    receipt = _epoch_receipt(account_returns=daily)
    baseline = _baseline(tmp_path, account_live_returns=daily)
    attribution._validated_account_live_return_series(
        {**attribution._baseline(baseline)[0], "epoch_hpr_receipts": [receipt]}
    )
    spec, returns = _inputs(tmp_path)
    _with_receipt_bound_coarse_reference(
        spec,
        returns,
        {
            "2026-01-11": "0",
            "2026-01-12": "0",
            "2026-01-13": "0",
            "2026-01-14": "0",
            "2026-01-15": "0",
        },
        observed_start="2026-01-11",
        observed_end=end,
    )
    value = json.loads(spec.read_text())
    for name in ("comparisons", "benchmark_comparisons"):
        value[name][0]["periods_per_year"] = 252
    spec.write_text(json.dumps(value))
    args = _dependence_selected(tmp_path, baseline, monkeypatch, receipt)
    catalog, _, _ = attribution.build_catalog(baseline, spec, **args)
    for name in ("comparisons", "benchmark_comparisons"):
        if expected is None:
            assert catalog[name][0]["status"] == "NOT_READY"
            assert catalog[name][0]["metrics"] is None
        else:
            assert catalog[name][0]["metrics"]["account_cumulative_return"] == expected
            assert catalog[name][0]["metrics"]["observations"] == 2

"""Build exact-common-date strategy attribution without inventing missing evidence."""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


SCHEMA_VERSION: Final = 1
CATALOG_SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.0.0"
STRATEGY_COUNT: Final = 9
CATALOG_NAME: Final = "strategy-attribution.json"
AVAILABLE_STATUS: Final = "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"


class AttributionValidationError(ValueError):
    """Raised when attribution inputs or identities are unsafe or ambiguous."""


def _load_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttributionValidationError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AttributionValidationError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AttributionValidationError(f"{name} must be non-empty text")
    return value


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AttributionValidationError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise AttributionValidationError(f"{name} must be finite")
    return parsed


def _decimal_text(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return format(value, ".15g")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _hash(path: Path, name: str) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise AttributionValidationError(f"{name} is missing or linked")
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def _verify_hash(path: Path, expected: object, name: str) -> tuple[str, int]:
    expected_hash = _text(expected, f"{name}.expected_sha256")
    actual, size = _hash(path, name)
    if actual != expected_hash:
        raise AttributionValidationError(f"{name} hash differs")
    return actual, size


def _baseline(root: Path) -> tuple[dict[str, Any], dict[str, str], Path]:
    pointer = read_current(root)
    generation = root / "generations" / pointer.generation_id
    manifest_path = generation / "generation.json"
    catalog_path = generation / "portfolio-baseline.json"
    manifest = _load_json(manifest_path, "baseline manifest")
    if manifest.get("verification") != "VERIFIED":
        raise AttributionValidationError("baseline generation is not verified")
    catalog = _load_json(catalog_path, "baseline catalog")
    if catalog.get("baseline_schema_version") != 1:
        raise AttributionValidationError("baseline schema is unsupported")
    return (
        catalog,
        {
            "generation_id": pointer.generation_id,
            "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        },
        catalog_path,
    )


def _rows(path: Path) -> dict[str, dict[date, Decimal]]:
    result: dict[str, dict[date, Decimal]] = {}
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != [
                "series_id",
                "date",
                "return",
                "evidence_layer",
                "version_ref",
                "source_artifact_id",
            ]:
                raise AttributionValidationError("return source columns differ")
            identities: dict[str, tuple[str, str, str]] = {}
            for row in reader:
                series_id = _text(row.get("series_id"), "return.series_id")
                day = date.fromisoformat(_text(row.get("date"), "return.date"))
                value = _decimal(row.get("return"), "return.return")
                if value <= -1:
                    raise AttributionValidationError("return must be greater than -1")
                identity = (
                    _text(row.get("evidence_layer"), "return.evidence_layer"),
                    _text(row.get("version_ref"), "return.version_ref"),
                    _text(row.get("source_artifact_id"), "return.source_artifact_id"),
                )
                if series_id in identities and identities[series_id] != identity:
                    raise AttributionValidationError(
                        "series identity changes within source"
                    )
                identities[series_id] = identity
                series = result.setdefault(series_id, {})
                if day in series and series[day] != value:
                    raise AttributionValidationError("series has conflicting dates")
                series[day] = value
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, AttributionValidationError):
            raise
        raise AttributionValidationError("return source is unreadable") from exc
    return result


def _cumulative(values: Sequence[Decimal]) -> Decimal:
    wealth = Decimal(1)
    with localcontext() as context:
        context.prec = 34
        for value in values:
            wealth *= Decimal(1) + value
    return wealth - Decimal(1)


def _comparison_metrics(
    account: Mapping[date, Decimal],
    reference: Mapping[date, Decimal],
    periods_per_year: int,
) -> dict[str, Any]:
    common = sorted(set(account) & set(reference))
    if len(common) < 2:
        raise AttributionValidationError("comparison lacks two exact common dates")
    account_dec = [account[day] for day in common]
    reference_dec = [reference[day] for day in common]
    account_values = [float(value) for value in account_dec]
    reference_values = [float(value) for value in reference_dec]
    active = [left - right for left, right in zip(account_values, reference_values)]
    tracking_error = statistics.stdev(active) * math.sqrt(periods_per_year)
    active_return = statistics.fmean(active) * periods_per_year
    information_ratio = active_return / tracking_error if tracking_error > 0 else None
    reference_variance = statistics.pvariance(reference_values)
    beta = (
        sum(
            (left - statistics.fmean(account_values))
            * (right - statistics.fmean(reference_values))
            for left, right in zip(account_values, reference_values)
        )
        / len(common)
        / reference_variance
        if reference_variance > 0
        else None
    )
    correlation = (
        statistics.correlation(account_values, reference_values)
        if statistics.pstdev(account_values) > 1e-15
        and statistics.pstdev(reference_values) > 1e-15
        else None
    )
    upside = [index for index, value in enumerate(reference_values) if value > 0]
    downside = [index for index, value in enumerate(reference_values) if value < 0]
    upside_capture = (
        statistics.fmean(account_values[index] for index in upside)
        / statistics.fmean(reference_values[index] for index in upside)
        if upside and statistics.fmean(reference_values[index] for index in upside) != 0
        else None
    )
    downside_capture = (
        statistics.fmean(account_values[index] for index in downside)
        / statistics.fmean(reference_values[index] for index in downside)
        if downside
        and statistics.fmean(reference_values[index] for index in downside) != 0
        else None
    )
    relative_wealth = 1.0
    peak = 1.0
    relative_drawdown = 0.0
    for account_return, reference_return in zip(account_values, reference_values):
        relative_wealth *= (1 + account_return) / (1 + reference_return)
        peak = max(peak, relative_wealth)
        relative_drawdown = min(relative_drawdown, relative_wealth / peak - 1)
    account_cumulative = _cumulative(account_dec)
    reference_cumulative = _cumulative(reference_dec)
    return {
        "account_cumulative_return": _decimal_text(account_cumulative),
        "active_return_annualized": _decimal_text(active_return),
        "beta": _decimal_text(beta),
        "correlation": _decimal_text(correlation),
        "cumulative_gap": _decimal_text(account_cumulative - reference_cumulative),
        "downside_capture": _decimal_text(downside_capture),
        "information_ratio": _decimal_text(information_ratio),
        "observations": len(common),
        "observed_end": common[-1].isoformat(),
        "observed_start": common[0].isoformat(),
        "reference_cumulative_return": _decimal_text(reference_cumulative),
        "relative_drawdown": _decimal_text(relative_drawdown),
        "tracking_error_annualized": _decimal_text(tracking_error),
        "upside_capture": _decimal_text(upside_capture),
    }


def build_catalog(
    baseline_root: Path, spec_path: Path
) -> tuple[dict[str, Any], bytes, list[SourceArtifact]]:
    spec = _load_json(spec_path, "attribution spec")
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise AttributionValidationError("attribution spec schema is unsupported")
    as_of = date.fromisoformat(_text(spec.get("as_of_date"), "spec.as_of_date"))
    baseline, baseline_identity, baseline_path = _baseline(baseline_root)
    if baseline.get("as_of_date") != as_of.isoformat():
        raise AttributionValidationError("spec as_of_date differs from baseline")
    strategies = {
        _text(item.get("strategy_id"), "strategy.strategy_id"): item
        for item in baseline.get("strategies", [])
        if isinstance(item, dict)
    }
    if len(strategies) != STRATEGY_COUNT:
        raise AttributionValidationError("baseline must contain exactly 9 strategies")
    source = spec.get("return_source")
    if not isinstance(source, dict):
        raise AttributionValidationError("return_source must be an object")
    source_path = Path(_text(source.get("path"), "return_source.path"))
    source_hash, source_size = _verify_hash(
        source_path, source.get("expected_sha256"), "return source"
    )
    manifest_path = Path(
        _text(source.get("source_manifest_path"), "source_manifest_path")
    )
    manifest_hash, manifest_size = _verify_hash(
        manifest_path,
        source.get("source_manifest_expected_sha256"),
        "source manifest",
    )
    source_manifest = _load_json(manifest_path, "source manifest")
    if source_manifest.get("verification") != "VERIFIED":
        raise AttributionValidationError("source manifest is not verified")
    series = _rows(source_path)
    benchmark_rows = spec.get("benchmark_assignments")
    comparison_rows = spec.get("comparisons")
    if not isinstance(benchmark_rows, list) or not isinstance(comparison_rows, list):
        raise AttributionValidationError("strategy rows must be arrays")
    if len(benchmark_rows) != STRATEGY_COUNT or len(comparison_rows) != STRATEGY_COUNT:
        raise AttributionValidationError("attribution requires exactly 9 strategy rows")
    benchmark_by_id = {
        _text(item.get("strategy_id"), "benchmark.strategy_id"): item
        for item in benchmark_rows
        if isinstance(item, dict)
    }
    comparison_by_id = {
        _text(item.get("strategy_id"), "comparison.strategy_id"): item
        for item in comparison_rows
        if isinstance(item, dict)
    }
    if set(benchmark_by_id) != set(strategies) or set(comparison_by_id) != set(
        strategies
    ):
        raise AttributionValidationError("attribution strategy boundary differs")
    accepted_benchmarks: list[dict[str, Any]] = []
    accepted_comparisons: list[dict[str, Any]] = []
    allocations = {
        str(item.get("strategy_id")): item
        for item in baseline.get("allocations", [])
        if isinstance(item, dict) and item.get("scope") == "SYSTEMATIC"
    }
    costs = {
        str(item.get("strategy_id")): item
        for item in baseline.get("costs", [])
        if isinstance(item, dict)
    }
    accepted_costs: list[dict[str, Any]] = []
    for strategy_id in sorted(strategies):
        benchmark = dict(benchmark_by_id[strategy_id])
        if benchmark.get("status") == "EVIDENCED" and (
            not benchmark.get("benchmark_id") or not benchmark.get("version_ref")
        ):
            raise AttributionValidationError(
                "evidenced benchmark requires benchmark and version"
            )
        if (
            benchmark.get("status") != "EVIDENCED"
            and benchmark.get("benchmark_id") is not None
        ):
            raise AttributionValidationError(
                "unavailable benchmark cannot name an accepted benchmark"
            )
        accepted_benchmarks.append(benchmark)
        comparison = dict(comparison_by_id[strategy_id])
        status = comparison.get("status")
        metrics = None
        legacy_delta = None
        legacy_disposition = "NOT_APPLICABLE"
        if status == AVAILABLE_STATUS:
            account_id = _text(
                comparison.get("account_series_id"), "comparison.account_series_id"
            )
            reference_id = _text(
                comparison.get("reference_series_id"),
                "comparison.reference_series_id",
            )
            if account_id not in series or reference_id not in series:
                raise AttributionValidationError("comparison series is missing")
            start = (
                None
                if comparison.get("observed_start") is None
                else date.fromisoformat(
                    _text(comparison.get("observed_start"), "comparison.observed_start")
                )
            )
            end = (
                None
                if comparison.get("observed_end") is None
                else date.fromisoformat(
                    _text(comparison.get("observed_end"), "comparison.observed_end")
                )
            )
            if (start is None) != (end is None) or (
                start is not None and end is not None and end < start
            ):
                raise AttributionValidationError("comparison interval is invalid")
            account_series = {
                day: value
                for day, value in series[account_id].items()
                if (start is None or day >= start) and (end is None or day <= end)
            }
            reference_series = {
                day: value
                for day, value in series[reference_id].items()
                if (start is None or day >= start) and (end is None or day <= end)
            }
            metrics = _comparison_metrics(
                account_series,
                reference_series,
                int(comparison.get("periods_per_year", 0)),
            )
            expected_gap = comparison.get("expected_legacy_cumulative_gap")
            if expected_gap is not None:
                legacy_delta_value = _decimal(
                    metrics["cumulative_gap"], "computed cumulative gap"
                ) - _decimal(expected_gap, "expected legacy cumulative gap")
                legacy_delta = _decimal_text(legacy_delta_value)
                legacy_disposition = (
                    "INDEPENDENTLY_REPRODUCED"
                    if abs(legacy_delta_value) <= Decimal("0.000000000001")
                    else "CORRECTED_BY_CURRENT_EXACT_DATE_SOURCE"
                )
        elif status == "UNAVAILABLE":
            if (
                comparison.get("account_series_id") is not None
                or comparison.get("reference_series_id") is not None
            ):
                raise AttributionValidationError(
                    "unavailable comparison cannot name comparison series"
                )
        else:
            raise AttributionValidationError("comparison status differs")
        accepted_comparisons.append(
            {
                **comparison,
                "legacy_baseline_delta": legacy_delta,
                "legacy_baseline_disposition": legacy_disposition,
                "metrics": metrics,
            }
        )
        allocation = allocations[strategy_id]
        current_value = _decimal(
            allocation["current_value"]["value"], "allocation.current_value"
        )
        cost = costs.get(strategy_id, {})
        annual_cost = (
            None
            if cost.get("annual_external_cost_usd") is None
            else _decimal(cost["annual_external_cost_usd"], "annual cost")
        )
        accepted_costs.append(
            {
                "after_tax_annual_cost_rate": None,
                "after_tax_status": "UNAVAILABLE_TAX_LOT_RATE_AND_REALIZATION_EVIDENCE_INCOMPLETE",
                "annual_external_cost_rate_pre_tax": (
                    None
                    if annual_cost is None or current_value == 0
                    else _decimal_text(annual_cost / current_value)
                ),
                "annual_external_cost_usd": _decimal_text(annual_cost),
                "cost_status": cost.get("cost_status", "unknown"),
                "current_sleeve_value_usd": _decimal_text(current_value),
                "fees_status": "EVIDENCED_PRE_TAX"
                if annual_cost is not None
                else "UNAVAILABLE",
                "strategy_id": strategy_id,
            }
        )
    catalog = {
        "as_of_date": as_of.isoformat(),
        "baseline": baseline_identity,
        "benchmark_assignments": accepted_benchmarks,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "comparisons": accepted_comparisons,
        "costs": accepted_costs,
        "source_manifest_sha256": manifest_hash,
        "source_returns_sha256": source_hash,
    }
    artifacts = [
        SourceArtifact.from_path(
            baseline_path,
            artifact_id="input/portfolio-baseline.json",
            as_of_date=as_of,
            schema_name="portfolio-baseline-catalog",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="input/attribution-returns.csv",
            sha256=source_hash,
            size_bytes=source_size,
            as_of_date=as_of,
            schema_name="strategy-attribution-return-source",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="input/source-manifest.json",
            sha256=manifest_hash,
            size_bytes=manifest_size,
            as_of_date=as_of,
            schema_name="strategy-attribution-source-manifest",
            schema_version="1",
        ),
    ]
    return catalog, canonical_json_bytes(catalog), artifacts


def stage_catalog(
    baseline_root: Path, spec_path: Path, output_root: Path
) -> dict[str, Any]:
    catalog, catalog_bytes, artifacts = build_catalog(baseline_root, spec_path)
    as_of = date.fromisoformat(catalog["as_of_date"])
    catalog_artifact = SourceArtifact(
        artifact_id=f"normalized/{CATALOG_NAME}",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="strategy-attribution",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-attribution-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="nine current strategies with exact-date metrics or explicit unavailable dispositions",
            fact_types=("benchmark assignment", "cost", "provider-account attribution"),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=tuple([*artifacts, catalog_artifact]),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / CATALOG_NAME
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise AttributionValidationError("staged catalog differs")
    if not catalog_path.exists():
        catalog_path.write_bytes(catalog_bytes)
    return {
        "available_comparisons": sum(
            row["metrics"] is not None for row in catalog["comparisons"]
        ),
        "catalog_sha256": catalog_artifact.sha256,
        "generation_id": manifest.generation_id,
        "strategy_count": len(catalog["comparisons"]),
    }


def publish_catalog(
    baseline_root: Path,
    spec_path: Path,
    output_root: Path,
    generation_id: str,
) -> dict[str, Any]:
    staged = stage_catalog(baseline_root, spec_path, output_root)
    if staged["generation_id"] != generation_id:
        raise AttributionValidationError("rebuilt generation differs")
    pointer = publish_generation(output_root, generation_id)
    return {**staged, "publication_state": pointer.state.value}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--baseline-root", type=Path, required=True)
        command.add_argument("--spec", type=Path, required=True)
        command.add_argument("--output-root", type=Path, required=True)
        if name == "publish":
            command.add_argument("--generation-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            result = stage_catalog(args.baseline_root, args.spec, args.output_root)
        else:
            result = publish_catalog(
                args.baseline_root, args.spec, args.output_root, args.generation_id
            )
    except (AttributionValidationError, OSError, ValueError) as exc:
        print(f"portfolio-attribution: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

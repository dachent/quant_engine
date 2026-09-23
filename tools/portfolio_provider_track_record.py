"""Stage a retained provider track-record capture as a governed generation.

`robuxio-provider-track-record` has read `NOT_CAPTURED` with the defect
`PROVIDER_REPORT_EXISTS_WITHOUT_A_GOVERNED_TRACK_RECORD_GENERATION` since #319:
the provider publishes its own performance report, but nothing local could cite
it, so the report stayed an assertion about a web page rather than evidence.

The capture is a retained directory of fetched bytes with a manifest that
records a sha256 and an observed length for every artifact. This tool performs
no network operation. It re-reads those bytes, verifies each one against the
manifest digest, derives the daily return path from the cumulative series the
provider itself publishes, and stages an immutable candidate generation. It
never creates or advances CURRENT -- selecting a generation stays an owner
action, as it is for every other source.

The provider's own track record is not owner account history and not a
version-matched backtest, so the staged catalog carries
`EXCLUDE_COMPARISON_ONLY`: capturing the report discharges the capture defect,
not the version-compatibility question.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Final

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    rename_directory_with_retry,
)
from quant_engine.core.contracts import canonical_json_bytes


CATALOG_SCHEMA_VERSION: Final = 1
ADAPTER_VERSION: Final = "1"
SOURCE_ID: Final = "provider-track-record"
RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
)
# The report publishes one cumulative path per series. The product subject is
# the high-volatility product the owner holds; the rest are the provider's own
# comparison series and are carried as benchmarks, never as strategy returns.
PRODUCT_SERIES: Final = "Robuxio High Vol"


class ProviderTrackRecordError(ValueError):
    """A retained capture violates the governed-generation contract."""


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProviderTrackRecordError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderTrackRecordError(f"{label} is not JSON") from exc


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProviderTrackRecordError(f"{label} is not a canonical date") from exc


def _level(value: object, label: str) -> Decimal:
    """Read one cumulative-return observation as a wealth index.

    The report publishes cumulative return as a multiple, so the series starts
    at zero and the wealth index is ``1 + value``. Reading the published number
    as the index itself would make every early return meaningless and the first
    one undefined.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProviderTrackRecordError(f"{label} is not numeric")
    try:
        level = Decimal(str(value)) + Decimal(1)
    except InvalidOperation as exc:
        raise ProviderTrackRecordError(f"{label} is not numeric") from exc
    if level <= 0:
        raise ProviderTrackRecordError(f"{label} is not a positive wealth index")
    return level


def _verify_capture(capture: Path) -> dict[str, Any]:
    """Re-read every manifested artifact and check it against its digest."""

    manifests = {
        "site": capture / "capture-manifest.json",
        "benchmark_api": capture / "benchmark-api/benchmark-api-manifest.json",
    }
    verified = 0
    summary: dict[str, Any] = {}
    for name, path in manifests.items():
        payload = _regular_file(path, f"{name} capture manifest")
        manifest = _json(payload, f"{name} capture manifest")
        if not isinstance(manifest, dict):
            raise ProviderTrackRecordError(f"{name} capture manifest root differs")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ProviderTrackRecordError(f"{name} capture manifest lacks artifacts")
        root = path.parent
        for entry in artifacts:
            if not isinstance(entry, dict):
                raise ProviderTrackRecordError(f"{name} capture artifact differs")
            relative = str(entry.get("relative_path") or "")
            expected = str(entry.get("sha256") or "")
            if not relative or len(expected) != 64:
                raise ProviderTrackRecordError(
                    f"{name} capture artifact identity differs: {relative}"
                )
            artifact = root / relative
            content = _regular_file(artifact, f"{name} capture artifact {relative}")
            if hashlib.sha256(content).hexdigest() != expected:
                raise ProviderTrackRecordError(
                    f"{name} capture artifact differs from its manifest: {relative}"
                )
            observed_length = entry.get("observed_length")
            if observed_length is not None and len(content) != observed_length:
                raise ProviderTrackRecordError(
                    f"{name} capture artifact length differs: {relative}"
                )
            verified += 1
        summary[name] = {
            "artifact_count": len(artifacts),
            "manifest_sha256": hashlib.sha256(payload).hexdigest(),
            "relative_path": str(path.relative_to(capture)).replace("\\", "/"),
        }
    summary["verified_artifacts"] = verified
    return summary


def _series(report: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not isinstance(report, dict):
        raise ProviderTrackRecordError("track-record report root differs")
    performance = report.get("performance")
    if not isinstance(performance, dict):
        raise ProviderTrackRecordError("track-record report lacks performance")
    cumulative = performance.get("cumulative")
    if not isinstance(cumulative, list) or len(cumulative) < 2:
        raise ProviderTrackRecordError("track-record cumulative path differs")

    observations: list[tuple[date, Decimal]] = []
    benchmarks: set[str] = set()
    for entry in cumulative:
        if not isinstance(entry, dict) or PRODUCT_SERIES not in entry:
            raise ProviderTrackRecordError("track-record observation differs")
        benchmarks.update(key for key in entry if key not in {"date", PRODUCT_SERIES})
        observations.append(
            (
                _day(entry.get("date"), "track-record observation date"),
                _level(entry[PRODUCT_SERIES], "track-record cumulative return"),
            )
        )
    days = [day for day, _level_value in observations]
    if days != sorted(set(days)):
        raise ProviderTrackRecordError("track-record calendar is not strictly ordered")
    if days[-1] - days[0] != timedelta(days=len(days) - 1):
        raise ProviderTrackRecordError("track-record calendar is not continuous daily")

    returns: list[dict[str, str]] = []
    for (_previous_day, previous), (day, level) in zip(
        observations, observations[1:], strict=False
    ):
        period_return = level / previous - Decimal(1)
        if period_return <= -1:
            raise ProviderTrackRecordError("track-record return is invalid")
        returns.append(
            {
                "series_id": PRODUCT_SERIES,
                "date": day.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "PROVIDER_TRACK_RECORD",
                "return_basis": "PROVIDER_PUBLISHED_CUMULATIVE_RETURN",
            }
        )

    # The report's own summary states the total return; deriving it back from
    # the path proves the two agree rather than trusting either alone.
    summary = report.get("stats")
    declared = None
    if isinstance(summary, dict):
        rows = summary.get("summary")
        if isinstance(rows, dict) and isinstance(rows.get(PRODUCT_SERIES), dict):
            declared = rows[PRODUCT_SERIES].get("total_return")
    derived = observations[-1][1] / observations[0][1] - Decimal(1)
    if declared is not None and abs(derived - Decimal(str(declared))) > Decimal(
        "0.0001"
    ):
        raise ProviderTrackRecordError(
            "track-record path and published total return differ"
        )

    identity = {
        "benchmark_series": sorted(benchmarks),
        "declared_total_return": None if declared is None else str(declared),
        "derived_total_return": format(derived.normalize(), "f"),
        "level_observations": len(observations),
        "observed_end": days[-1].isoformat(),
        "observed_start": days[0].isoformat(),
        "return_observations": len(returns),
        "series_id": PRODUCT_SERIES,
        "transform_id": "PROVIDER_CUMULATIVE_RETURN_TO_DAILY_RETURN_V1",
    }
    return returns, identity


def _returns_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=RETURN_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _build(
    capture: Path, report_relative: str, as_of: date
) -> tuple[bytes, bytes, GenerationManifest, dict[str, Any]]:
    capture_summary = _verify_capture(capture)
    report_path = capture / report_relative
    report_bytes = _regular_file(report_path, "track-record report")
    returns, identity = _series(_json(report_bytes, "track-record report"))
    returns_bytes = _returns_bytes(returns)

    catalog: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "capture": capture_summary,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "observed_end": identity["observed_end"],
            "observed_start": identity["observed_start"],
            "return_observations": identity["return_observations"],
            "status": "COMPLETE_WITHIN_RETAINED_CAPTURE_BOUNDARY",
        },
        "evidence_layer": "PROVIDER_TRACK_RECORD",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "mode_admission": {
            "LIVE_ONLY": "EXCLUDE_COMPARISON_ONLY",
            "LIVE_PLUS_BACKTEST": "EXCLUDE_COMPARISON_ONLY",
        },
        "provider_report": {
            "relative_path": report_relative,
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
            "size_bytes": len(report_bytes),
        },
        "returns_artifact_id": "returns.csv",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "series": identity,
        "source_id": SOURCE_ID,
        "splice_policy": (
            "PROVIDER_TRACK_RECORD_IS_COMPARISON_EVIDENCE; "
            "NEVER_RELABEL_AS_OWNER_ACCOUNT_LIVE_OR_VERSION_MATCHED_BACKTEST"
        ),
    }
    catalog_bytes = canonical_json_bytes(catalog)

    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-provider-track-record", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Retained provider track-record capture verified against "
                f"{capture_summary['verified_artifacts']} manifested artifact "
                f"digests; {identity['return_observations']} daily comparison "
                f"returns for {PRODUCT_SERIES} from {identity['observed_start']} "
                f"through {identity['observed_end']}, admitted as provider "
                "comparison evidence only"
            ),
            fact_types=("PROVIDER_TRACK_RECORD_RETURNS",),
            observed_start=_day(identity["observed_start"], "observed_start"),
            observed_end=_day(identity["observed_end"], "observed_end"),
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="catalog.json",
                sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                size_bytes=len(catalog_bytes),
                as_of_date=as_of,
                schema_name="provider-track-record-catalog",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="returns.csv",
                sha256=hashlib.sha256(returns_bytes).hexdigest(),
                size_bytes=len(returns_bytes),
                as_of_date=as_of,
                schema_name="provider-track-record-returns",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="raw/track-record-report.json",
                sha256=hashlib.sha256(report_bytes).hexdigest(),
                size_bytes=len(report_bytes),
                as_of_date=as_of,
                schema_name="robuxio-benchmark-api-report",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return returns_bytes, catalog_bytes, manifest, {"report_bytes": report_bytes}


def stage_candidate(
    capture: Path, output_root: Path, report_relative: str, as_of: date
) -> dict[str, object]:
    returns_bytes, catalog_bytes, manifest, extra = _build(
        capture.resolve(), report_relative, as_of
    )
    generations = output_root.resolve() / "generations"
    final = generations / manifest.generation_id
    expected = {
        Path("generation.json"): manifest.to_json_bytes(),
        Path("catalog.json"): catalog_bytes,
        Path("returns.csv"): returns_bytes,
        Path("raw/track-record-report.json"): extra["report_bytes"],
    }
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise ProviderTrackRecordError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ProviderTrackRecordError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".portfolio-provider-track-record-", dir=generations
            )
        )
        try:
            for relative, content in expected.items():
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            rename_directory_with_retry(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    catalog = json.loads(catalog_bytes)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        "published": False,
        "series": catalog["series"],
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
        "verified_capture_artifacts": catalog["capture"]["verified_artifacts"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--report",
        default="benchmark-api/api_benchmarks_robuxio__period-all.json",
        help="capture-relative path of the full-period track-record report",
    )
    parser.add_argument("--as-of-date", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_candidate(
            args.capture,
            args.output_root,
            args.report,
            _day(args.as_of_date, "as-of date"),
        )
    except ProviderTrackRecordError as exc:
        print(f"portfolio-provider-track-record: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

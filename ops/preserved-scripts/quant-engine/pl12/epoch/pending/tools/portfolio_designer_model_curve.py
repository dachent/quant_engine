"""Stage a Portfolio123 designer model's published curve as a governed generation.

`p123-eu` has carried `NO_EXACT_MODEL_CAPTURE_BEFORE_PROVIDER_LAUNCH` because
the retained Portfolio123 generation captured the owner's subscribed copy,
whose curve begins when the copy was created. The designer model the copy
follows publishes its own curve from inception, but nothing local could cite
it, so the pre-launch segment stayed a statement about a web page.

This tool performs no network operation. It re-reads a retained capture,
verifies every artifact against the capture manifest's digests, decodes the
chart payload's implicit calendar, and stages an immutable candidate
generation. It never creates or advances CURRENT -- selecting a generation
stays an owner action, as it is for every other source.

The payload is a chart series, not a table: each row is
``[step, model, benchmark]`` where ``step`` is the day count since the previous
row, so the calendar is reconstructed by accumulation from the declared start.
A reconstruction is only evidence if it agrees with what the provider states
independently, so the decoded last valued day is checked against the model's
own ``endDt`` and the decoded launch boundary against its own ``launchDt``.
Neither is assumed.

The staged catalog records the provider's launch date but does not split on it.
Which side of a launch is admissible history is a splice decision, and splice
decisions belong to the history builder, not to a capture stager.
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
SOURCE_ID: Final = "portfolio123-designer-model-curve"
RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
)


class DesignerModelCurveError(ValueError):
    """A retained capture violates the governed-generation contract."""


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DesignerModelCurveError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesignerModelCurveError(f"{label} is not JSON") from exc


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DesignerModelCurveError(f"{label} is not a canonical date") from exc


def _provider_day(value: object, label: str) -> date:
    """Read a provider date, which the model payload states as MM/DD/YYYY."""

    parts = str(value).split("/")
    if len(parts) != 3:
        raise DesignerModelCurveError(f"{label} is not a provider date: {value!r}")
    month, day, year = parts
    try:
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise DesignerModelCurveError(f"{label} is not a provider date") from exc


def _level(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DesignerModelCurveError(f"{label} is not numeric")
    try:
        level = Decimal(str(value))
    except InvalidOperation as exc:
        raise DesignerModelCurveError(f"{label} is not numeric") from exc
    if level <= 0:
        raise DesignerModelCurveError(f"{label} is not a positive wealth index")
    return level


def _verify_capture(capture: Path) -> dict[str, Any]:
    """Re-read every manifested artifact and check it against its digest."""

    path = capture / "capture-manifest.json"
    payload = _regular_file(path, "capture manifest")
    manifest = _json(payload, "capture manifest")
    if not isinstance(manifest, dict):
        raise DesignerModelCurveError("capture manifest root differs")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise DesignerModelCurveError("capture manifest lacks artifacts")
    verified = 0
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise DesignerModelCurveError("capture artifact differs")
        relative = str(entry.get("relative_path") or "")
        expected = str(entry.get("sha256") or "")
        if not relative or len(expected) != 64:
            raise DesignerModelCurveError(
                f"capture artifact identity differs: {relative}"
            )
        content = _regular_file(capture / relative, f"capture artifact {relative}")
        if hashlib.sha256(content).hexdigest() != expected:
            raise DesignerModelCurveError(
                f"capture artifact differs from its manifest: {relative}"
            )
        observed_length = entry.get("observed_length")
        if observed_length is not None and len(content) != observed_length:
            raise DesignerModelCurveError(
                f"capture artifact length differs: {relative}"
            )
        verified += 1
    return {
        "artifact_count": len(artifacts),
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "relative_path": "capture-manifest.json",
        "verified_artifacts": verified,
    }


def _curve(performance: Any) -> tuple[list[tuple[date, Decimal]], date, date]:
    """Decode the chart payload into a dated level path and its launch line."""

    if not isinstance(performance, dict):
        raise DesignerModelCurveError("performance payload root differs")
    block = performance.get("perfData")
    if not isinstance(block, dict):
        raise DesignerModelCurveError("performance payload lacks perfData")
    rows = block.get("data")
    if not isinstance(rows, list) or len(rows) < 2:
        raise DesignerModelCurveError("performance data path differs")
    start = _day(block.get("date"), "performance start date")

    plot_lines = block.get("plotLines")
    if not isinstance(plot_lines, list) or len(plot_lines) != 1:
        raise DesignerModelCurveError("performance payload lacks one launch line")
    launch = _day(plot_lines[0].get("dt"), "performance launch line")

    observations: list[tuple[date, Decimal]] = []
    observed = start
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 2:
            raise DesignerModelCurveError("performance observation differs")
        step = row[0]
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise DesignerModelCurveError("performance day step differs")
        if index:
            observed = observed + timedelta(days=step)
        if row[1] is None:
            # The payload is padded to the chart's right edge, so the trailing
            # unvalued rows are future calendar, not missing observations.
            continue
        if observations and observed <= observations[-1][0]:
            raise DesignerModelCurveError("performance calendar is not increasing")
        observations.append((observed, _level(row[1], "performance level")))
    if len(observations) < 2:
        raise DesignerModelCurveError("performance path has too few observations")
    return observations, start, launch


def _series(model: Any, performance: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not isinstance(model, dict):
        raise DesignerModelCurveError("model payload root differs")
    observations, start, launch = _curve(performance)

    stated_end = _provider_day(model.get("endDt"), "model endDt")
    if observations[-1][0] != stated_end:
        raise DesignerModelCurveError(
            "decoded curve end differs from the model's stated endDt"
        )
    stated_launch = _provider_day(model.get("launchDt"), "model launchDt")
    if stated_launch != launch:
        raise DesignerModelCurveError(
            "model launchDt differs from the performance launch line"
        )
    stated_inception = _provider_day(model.get("inceptionDt"), "model inceptionDt")
    if stated_inception != start:
        raise DesignerModelCurveError(
            "model inceptionDt differs from the performance start date"
        )

    rows: list[dict[str, str]] = []
    for (_earlier, previous), (observed, current) in zip(
        observations, observations[1:], strict=False
    ):
        period_return = current / previous - Decimal(1)
        if period_return <= -1:
            raise DesignerModelCurveError("designer model return is invalid")
        rows.append(
            {
                "series_id": str(model.get("name") or ""),
                "date": observed.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": (
                    "BACKTEST" if observed < launch else "PROVIDER_MODEL_LIVE"
                ),
                "return_basis": "PROVIDER_PUBLISHED_MODEL_EQUITY_CURVE",
            }
        )

    before = sum(1 for row in rows if row["evidence_layer"] == "BACKTEST")
    revisions = model.get("designerModel")
    revision_dates: list[str] = []
    if isinstance(revisions, dict) and isinstance(revisions.get("revisions"), list):
        for revision in revisions["revisions"]:
            if isinstance(revision, dict) and revision.get("update_dt"):
                revision_dates.append(str(revision["update_dt"]))

    identity = {
        "benchmark": str(model.get("benchmark") or ""),
        "benchmark_uid": model.get("benchmarkUid"),
        "currency": str(model.get("currency") or ""),
        "level_observations": len(observations),
        "model_live_return_observations": len(rows) - before,
        "observed_end": observations[-1][0].isoformat(),
        "observed_start": observations[0][0].isoformat(),
        "port_uid": model.get("portUid"),
        "prelaunch_return_observations": before,
        "provider_inception": stated_inception.isoformat(),
        "provider_launch": launch.isoformat(),
        # A revision restates the model, so the curve on both sides of one is
        # the revised system's, not the system that ran at the time. Recording
        # the dates lets the splice decide; hiding them would let it assume.
        "provider_revision_dates": sorted(revision_dates),
        "return_observations": len(rows),
        "series_id": str(model.get("name") or ""),
        "transform_id": "P123_DESIGNER_MODEL_EQUITY_CURVE_TO_DAILY_RETURN_V1",
    }
    return rows, identity


def _returns_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=RETURN_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _build(
    capture: Path, model_relative: str, performance_relative: str, as_of: date
) -> tuple[bytes, bytes, GenerationManifest, dict[str, Any]]:
    capture_summary = _verify_capture(capture)
    model_bytes = _regular_file(capture / model_relative, "designer model payload")
    performance_bytes = _regular_file(
        capture / performance_relative, "designer performance payload"
    )
    rows, identity = _series(
        _json(model_bytes, "designer model payload"),
        _json(performance_bytes, "designer performance payload"),
    )
    returns_bytes = _returns_bytes(rows)

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
        "evidence_layer": "PROVIDER_PUBLISHED_MODEL_CURVE",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "mode_admission": {
            "LIVE_ONLY": "EXCLUDE_COMPARISON_ONLY",
            "LIVE_PLUS_BACKTEST": "ADMISSIBLE_PRELAUNCH_SEGMENT_ONLY",
        },
        "provider_payloads": {
            "model": {
                "relative_path": model_relative,
                "sha256": hashlib.sha256(model_bytes).hexdigest(),
                "size_bytes": len(model_bytes),
            },
            "performance": {
                "relative_path": performance_relative,
                "sha256": hashlib.sha256(performance_bytes).hexdigest(),
                "size_bytes": len(performance_bytes),
            },
        },
        "returns_artifact_id": "returns.csv",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "series": identity,
        "source_id": SOURCE_ID,
        "splice_policy": (
            "PROVIDER_MODEL_CURVE_IS_THE_MODEL_TRACK_RECORD_NOT_OWNER_ACCOUNT_HISTORY; "
            "ONLY_THE_SEGMENT_STRICTLY_BEFORE_PROVIDER_LAUNCH_IS_BACKTEST"
        ),
    }
    catalog_bytes = canonical_json_bytes(catalog)

    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio123-designer-model-curve", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Retained designer-model capture verified against "
                f"{capture_summary['verified_artifacts']} manifested artifact "
                f"digests; {identity['return_observations']} daily returns for "
                f"{identity['series_id']} from {identity['observed_start']} "
                f"through {identity['observed_end']}, of which "
                f"{identity['prelaunch_return_observations']} fall strictly "
                f"before the provider's stated launch {identity['provider_launch']}"
            ),
            fact_types=("PROVIDER_MODEL_CURVE_RETURNS",),
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
                schema_name="designer-model-curve-catalog",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="returns.csv",
                sha256=hashlib.sha256(returns_bytes).hexdigest(),
                size_bytes=len(returns_bytes),
                as_of_date=as_of,
                schema_name="designer-model-curve-returns",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="raw/designer-model.json",
                sha256=hashlib.sha256(model_bytes).hexdigest(),
                size_bytes=len(model_bytes),
                as_of_date=as_of,
                schema_name="portfolio123-designer-model",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="raw/designer-performance.json",
                sha256=hashlib.sha256(performance_bytes).hexdigest(),
                size_bytes=len(performance_bytes),
                as_of_date=as_of,
                schema_name="portfolio123-designer-performance",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return (
        returns_bytes,
        catalog_bytes,
        manifest,
        {"model_bytes": model_bytes, "performance_bytes": performance_bytes},
    )


def stage_candidate(
    capture: Path,
    output_root: Path,
    model_relative: str,
    performance_relative: str,
    as_of: date,
) -> dict[str, object]:
    returns_bytes, catalog_bytes, manifest, extra = _build(
        capture.resolve(), model_relative, performance_relative, as_of
    )
    generations = output_root.resolve() / "generations"
    final = generations / manifest.generation_id
    expected = {
        Path("generation.json"): manifest.to_json_bytes(),
        Path("catalog.json"): catalog_bytes,
        Path("returns.csv"): returns_bytes,
        Path("raw/designer-model.json"): extra["model_bytes"],
        Path("raw/designer-performance.json"): extra["performance_bytes"],
    }
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise DesignerModelCurveError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise DesignerModelCurveError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".portfolio123-designer-model-", dir=generations)
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
        "--model",
        default="raw/r2g_1764562_dm_true.json",
        help="capture-relative path of the designer model metadata payload",
    )
    parser.add_argument(
        "--performance",
        default="raw/r2g_1764562_perf-full.json",
        help="capture-relative path of the full-period performance payload",
    )
    parser.add_argument("--as-of-date", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_candidate(
            args.capture,
            args.output_root,
            args.model,
            args.performance,
            _day(args.as_of_date, "as-of date"),
        )
    except DesignerModelCurveError as exc:
        print(f"portfolio123-designer-model-curve: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

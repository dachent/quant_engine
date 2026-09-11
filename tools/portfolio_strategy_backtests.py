"""Stage the retained strategy-history bundle as a governed generation.

`portfolio_strategy_history.py` normalizes the retained vendor artifacts into a
hash-bound bundle: `returns.csv` plus a `manifest.json` whose per-series entries
carry the strategy version, the observed span and the source artifacts each
series was derived from. That bundle is evidence, but it is not yet a governed
generation, so nothing can cite it: a `history_corrections` record needs an
authority root and an artifact selector, and the `strategy-backtests` source
segment needs a generation id.

This tool performs no network operation and derives nothing. It reads one
retained bundle, verifies the returns against the manifest's own digest,
re-derives the per-series coverage from the rows, and stages an immutable
candidate generation. It never creates or advances CURRENT -- selecting a
generation stays an owner action, exactly as it is for every other source.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
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
SOURCE_ID: Final = "strategy-backtests"
RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)


class StrategyBacktestsError(ValueError):
    """A retained bundle violates the governed-generation contract."""


def _regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise StrategyBacktestsError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise StrategyBacktestsError(f"{label} is not a canonical date") from exc


def _build(bundle: Path) -> tuple[bytes, bytes, GenerationManifest, dict[str, Any]]:
    manifest_bytes = _regular_file(bundle / "manifest.json", "bundle manifest")
    returns_bytes = _regular_file(bundle / "returns.csv", "bundle returns")
    try:
        source = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise StrategyBacktestsError("bundle manifest is not JSON") from exc
    if not isinstance(source, dict):
        raise StrategyBacktestsError("bundle manifest root must be an object")
    if source.get("builder") != "portfolio-strategy-history":
        raise StrategyBacktestsError("bundle builder differs")
    if source.get("verification") != "VERIFIED":
        raise StrategyBacktestsError("bundle is not verified")
    if str(source.get("bundle_id")) != bundle.name:
        raise StrategyBacktestsError("bundle identity differs from its directory")

    artifact = source.get("returns_artifact")
    if not isinstance(artifact, dict):
        raise StrategyBacktestsError("bundle returns artifact is invalid")
    if (
        hashlib.sha256(returns_bytes).hexdigest() != artifact.get("sha256")
        or len(returns_bytes) != artifact.get("size_bytes")
    ):
        raise StrategyBacktestsError("bundle returns differ from the manifest")

    rows = list(csv.DictReader(io.StringIO(returns_bytes.decode("utf-8"))))
    if not rows or tuple(rows[0]) != RETURN_FIELDS:
        raise StrategyBacktestsError("bundle return header differs")
    if {row["evidence_layer"] for row in rows} != {"BACKTEST"}:
        raise StrategyBacktestsError("bundle carries a non-backtest evidence layer")

    observed: dict[str, list[str]] = {}
    versions: dict[str, set[str]] = {}
    for row in rows:
        observed.setdefault(row["series_id"], []).append(row["date"])
        versions.setdefault(row["series_id"], set()).add(row["strategy_version"])

    declared = source.get("series")
    if not isinstance(declared, list) or not declared:
        raise StrategyBacktestsError("bundle manifest lacks series")
    series: list[dict[str, Any]] = []
    for entry in sorted(declared, key=lambda item: str(item.get("series_id"))):
        series_id = str(entry.get("series_id"))
        dates = observed.get(series_id)
        if not dates:
            raise StrategyBacktestsError(f"bundle series has no rows: {series_id}")
        if versions[series_id] != {str(entry.get("strategy_version"))}:
            raise StrategyBacktestsError(
                f"bundle series version differs from its rows: {series_id}"
            )
        if (
            min(dates) != str(entry.get("observed_start"))
            or max(dates) != str(entry.get("observed_end"))
            or len(dates) != entry.get("return_observations")
        ):
            raise StrategyBacktestsError(
                f"bundle series coverage differs from its rows: {series_id}"
            )
        series.append(
            {
                "compatibility_basis": str(entry.get("compatibility_basis")),
                "coverage_disposition": str(entry.get("coverage_disposition")),
                "frequency": str(entry.get("frequency")),
                "history_status": str(entry.get("history_status")),
                "observed_end": max(dates),
                "observed_start": min(dates),
                "return_observations": len(dates),
                "series_id": series_id,
                "strategy_version": str(entry.get("strategy_version")),
            }
        )

    as_of = _day(source.get("as_of_date"), "bundle as_of_date")
    starts = [item["observed_start"] for item in series]
    ends = [item["observed_end"] for item in series]
    catalog: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "bundle": {
            "bundle_id": str(source.get("bundle_id")),
            "builder": str(source.get("builder")),
            "builder_version": str(source.get("builder_version")),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "returns_sha256": artifact.get("sha256"),
            "returns_size_bytes": artifact.get("size_bytes"),
        },
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "observed_end": max(ends),
            "observed_start": min(starts),
            "return_observations": len(rows),
            "series_count": len(series),
            "status": "COMPLETE_WITHIN_RETAINED_SOURCE_BOUNDARY",
        },
        "evidence_layer": "BACKTEST",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "returns_artifact_id": "returns.csv",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "series": series,
        "source_id": SOURCE_ID,
        "splice_policy": (
            "ONLY_VERSION_COMPATIBLE_PREHISTORY_STRICTLY_BEFORE_LIVE_INCEPTION"
        ),
    }
    catalog_bytes = canonical_json_bytes(catalog)

    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-strategy-backtests", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "Retained vendor strategy histories normalized by "
                f"{source.get('builder')} {source.get('builder_version')} into "
                f"bundle {source.get('bundle_id')}; {len(series)} series, "
                f"{len(rows)} daily backtest returns from {min(starts)} through "
                f"{max(ends)}, each admitted strictly before its own "
                "account-live inception"
            ),
            fact_types=("BACKTEST_RETURNS",),
            observed_start=_day(min(starts), "observed_start"),
            observed_end=_day(max(ends), "observed_end"),
            known_gaps=(),
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="catalog.json",
                sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                size_bytes=len(catalog_bytes),
                as_of_date=as_of,
                schema_name="strategy-version-catalog",
                schema_version="2",
            ),
            SourceArtifact(
                artifact_id="returns.csv",
                sha256=hashlib.sha256(returns_bytes).hexdigest(),
                size_bytes=len(returns_bytes),
                as_of_date=as_of,
                schema_name="portfolio-strategy-history-returns",
                schema_version="1",
            ),
            SourceArtifact(
                artifact_id="raw/strategy-history-manifest.json",
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size_bytes=len(manifest_bytes),
                as_of_date=as_of,
                schema_name="portfolio-strategy-history-manifest",
                schema_version=str(source.get("schema_version")),
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return returns_bytes, catalog_bytes, manifest, {
        "manifest_bytes": manifest_bytes,
        "series": series,
        "return_observations": len(rows),
    }


def stage_candidate(bundle: Path, output_root: Path) -> dict[str, object]:
    returns_bytes, catalog_bytes, manifest, extra = _build(bundle.resolve())
    root = output_root.resolve()
    generations = root / "generations"
    final = generations / manifest.generation_id
    expected = {
        Path("generation.json"): manifest.to_json_bytes(),
        Path("catalog.json"): catalog_bytes,
        Path("returns.csv"): returns_bytes,
        Path("raw/strategy-history-manifest.json"): extra["manifest_bytes"],
    }
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise StrategyBacktestsError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise StrategyBacktestsError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".portfolio-strategy-backtests-", dir=generations)
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
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        "published": False,
        "return_observations": extra["return_observations"],
        "series": {
            item["series_id"]: {
                "observed_end": item["observed_end"],
                "observed_start": item["observed_start"],
                "return_observations": item["return_observations"],
                "strategy_version": item["strategy_version"],
            }
            for item in extra["series"]
        },
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_candidate(args.bundle, args.output_root)
    except StrategyBacktestsError as exc:
        print(f"portfolio-strategy-backtests: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

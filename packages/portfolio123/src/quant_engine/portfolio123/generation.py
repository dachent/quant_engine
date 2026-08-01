"""Deterministic Portfolio123 database roll-forward and source cutover."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Iterable, Mapping

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


PLAN_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "1.0.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENTITY = re.compile(r"^(strategy|account)_(\d+)_", re.IGNORECASE)
_MAX_CSV_FIELD_BYTES = 64 * 1024 * 1024
_LEGACY_TABLES = {
    "book_assets.parquet",
    "entities.parquet",
    "general_info.parquet",
    "holdings.parquet",
    "manual_period_perf.parquet",
    "manual_period_perf_dist.parquet",
    "manual_portperf.parquet",
    "manual_r2g_snapshot.parquet",
    "manual_screen_snapshot.parquet",
    "manual_trading_system_config.parquet",
    "pull_ledger.parquet",
    "quick_stats.parquet",
    "returns_daily.parquet",
    "returns_monthly.parquet",
    "returns_weekly.parquet",
    "returns_yearly.parquet",
    "strategy_registry.parquet",
    "strategy_relationships.parquet",
    "transactions.parquet",
}


class Portfolio123GenerationError(ValueError):
    """The Portfolio123 generation or cutover failed closed."""


def _root(value: Path, name: str, *, existing: bool) -> Path:
    if not value.is_absolute():
        raise Portfolio123GenerationError(f"{name} must be absolute")
    resolved = value.resolve(strict=False)
    if existing and (not resolved.is_dir() or resolved.is_symlink()):
        raise Portfolio123GenerationError(f"{name} is missing or linked")
    return resolved


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_path(root: Path, relative: str) -> Path:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(
        part in {"", ".", ".."} for part in portable.parts
    ):
        raise Portfolio123GenerationError("unsafe relative path")
    result = root.joinpath(*portable.parts)
    if not _within(result.resolve(strict=False), root.resolve(strict=False)):
        raise Portfolio123GenerationError("relative path escapes its root")
    return result


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise Portfolio123GenerationError(f"conflicting evidence: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise Portfolio123GenerationError("source contains a linked path")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def _disposition(label: str, relative: str) -> str:
    portable = PurePosixPath(relative)
    if (
        label == "legacy-data"
        and len(portable.parts) == 2
        and portable.parts[0] == "p123_db"
        and portable.name in _LEGACY_TABLES
    ):
        return "BASELINE_PARQUET_TABLE"
    if (
        label == "har-workspace"
        and portable.suffix.casefold() == ".csv"
        and portable.parts[0] in {"csv", "downloads"}
    ):
        return "HAR_DERIVED_CSV"
    return "PROVENANCE_ONLY"


def _inventory(label: str, root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        digest, size = hash_file(path)
        entries.append(
            {
                "bytes": size,
                "disposition": _disposition(label, relative),
                "relative_path": relative,
                "sha256": digest,
            }
        )
    return entries


def create_cutover_plan(
    legacy_root: Path,
    har_root: Path,
    data_root: Path,
    *,
    as_of_date: date,
) -> dict[str, object]:
    """Hash every approved input and create a private content-addressed plan."""

    legacy = _root(legacy_root, "legacy_root", existing=True)
    har = _root(har_root, "har_root", existing=True)
    destination = _root(data_root, "data_root", existing=True)
    roots = (legacy, har, destination)
    if any(
        _within(left, right) or _within(right, left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise Portfolio123GenerationError("source and data roots must be disjoint")
    sources: list[dict[str, Any]] = [
        {
            "files": _inventory("legacy-data", legacy),
            "label": "legacy-data",
            "root": str(legacy),
        },
        {
            "files": _inventory("har-workspace", har),
            "label": "har-workspace",
            "root": str(har),
        },
    ]
    legacy_tables = {
        PurePosixPath(str(item["relative_path"])).name
        for item in _objects(sources[0]["files"], "legacy files")
        if item["disposition"] == "BASELINE_PARQUET_TABLE"
    }
    if legacy_tables != _LEGACY_TABLES:
        raise Portfolio123GenerationError("legacy 19-table database differs")
    har_count = sum(
        item["disposition"] == "HAR_DERIVED_CSV"
        for item in _objects(sources[1]["files"], "HAR files")
    )
    if har_count == 0:
        raise Portfolio123GenerationError("HAR derived CSV set is empty")
    payload: dict[str, object] = {
        "as_of_date": as_of_date.isoformat(),
        "data_root": str(destination),
        "provider_id": "portfolio123",
        "schema_version": PLAN_SCHEMA_VERSION,
        "sources": sources,
    }
    plan_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    run_id = f"legacy-{as_of_date.strftime('%Y%m%d')}-{plan_id[:12]}"
    plan = {**payload, "plan_id": plan_id, "run_id": run_id}
    plan_path = destination / "cutovers" / "plans" / plan_id / "plan.json"
    _write_create_only(plan_path, canonical_json_bytes(plan))
    return {
        "file_count": sum(
            len(_objects(source["files"], "source files")) for source in sources
        ),
        "har_csv_count": har_count,
        "legacy_table_count": len(legacy_tables),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "run_id": run_id,
        "state": "PLANNED",
    }


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Portfolio123GenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise Portfolio123GenerationError(f"{name} must be an object array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise Portfolio123GenerationError(f"{name} must be text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Portfolio123GenerationError(f"{name} must be a non-negative integer")
    return value


def _load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = _object(json.loads(plan_path.read_text(encoding="utf-8")), "plan")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Portfolio123GenerationError("plan is not valid UTF-8 JSON") from exc
    identity = dict(plan)
    plan_id = identity.pop("plan_id", None)
    run_id = identity.pop("run_id", None)
    if not isinstance(plan_id, str) or _SHA256.fullmatch(plan_id) is None:
        raise Portfolio123GenerationError("plan ID is invalid")
    if hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != plan_id:
        raise Portfolio123GenerationError("plan identity differs")
    expected_run = (
        f"legacy-{str(plan.get('as_of_date', '')).replace('-', '')}-{plan_id[:12]}"
    )
    if run_id != expected_run or plan_path.parent.name != plan_id:
        raise Portfolio123GenerationError("plan run identity differs")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise Portfolio123GenerationError("plan schema version is unsupported")
    return plan


def _sources(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = _objects(plan.get("sources"), "sources")
    if {item.get("label") for item in sources} != {"legacy-data", "har-workspace"}:
        raise Portfolio123GenerationError("plan source labels differ")
    return sources


def _run_paths(plan: Mapping[str, Any]) -> tuple[Path, Path]:
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    run = data_root / "source-runs" / _text(plan["run_id"], "run_id")
    return run / ".staging", run / "artifacts"


def _object_path(root: Path, item: Mapping[str, Any]) -> Path:
    digest = _text(item["sha256"], "sha256")
    if _SHA256.fullmatch(digest) is None:
        raise Portfolio123GenerationError("object hash is invalid")
    return root / "objects" / digest[:2] / digest


def _source_index_bytes(plan: Mapping[str, Any]) -> bytes:
    entries = [
        {
            "bytes": item["bytes"],
            "disposition": item["disposition"],
            "label": source["label"],
            "object_id": item["sha256"],
            "relative_path": item["relative_path"],
            "sha256": item["sha256"],
        }
        for source in _sources(plan)
        for item in _objects(source.get("files"), "files")
    ]
    return canonical_json_bytes(
        {
            "entries": entries,
            "plan_id": plan["plan_id"],
            "provider_id": "portfolio123",
            "schema_version": 1,
        }
    )


def _verify_authority(root: Path, plan: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise Portfolio123GenerationError("source-run authority is missing or linked")
    index_path = root / "source-index.json"
    if not index_path.is_file() or index_path.read_bytes() != _source_index_bytes(plan):
        raise Portfolio123GenerationError("source-run index differs")
    expected_objects: dict[str, tuple[str, int]] = {}
    for source in _sources(plan):
        for item in _objects(source.get("files"), "files"):
            digest = _text(item["sha256"], "sha256")
            expected_objects[digest] = (digest, _integer(item["bytes"], "bytes"))
    objects_root = root / "objects"
    actual = {path.name: path for path in objects_root.rglob("*") if path.is_file()}
    if set(actual) != set(expected_objects):
        raise Portfolio123GenerationError("source-run object inventory differs")
    for digest, path in actual.items():
        if hash_file(path) != expected_objects[digest]:
            raise Portfolio123GenerationError("source-run object bytes differ")


def _verify_source(root: Path, source: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise Portfolio123GenerationError("source root is missing or linked")
    expected = {
        _text(item["relative_path"], "relative_path"): item
        for item in _objects(source.get("files"), "files")
    }
    actual = {path.relative_to(root).as_posix(): path for path in _source_files(root)}
    if set(actual) != set(expected):
        raise Portfolio123GenerationError("source inventory differs from plan")
    for relative, path in actual.items():
        digest, size = hash_file(path)
        item = expected[relative]
        if digest != item.get("sha256") or size != item.get("bytes"):
            raise Portfolio123GenerationError(f"source bytes differ: {relative}")


def _copy_sources(plan: Mapping[str, Any]) -> Path:
    staging, authority = _run_paths(plan)
    if authority.exists():
        _verify_authority(authority, plan)
        return authority
    staging.mkdir(parents=True, exist_ok=True)
    for source in _sources(plan):
        root = _root(Path(_text(source["root"], "root")), "source_root", existing=True)
        _verify_source(root, source)
        for item in _objects(source.get("files"), "files"):
            relative = _text(item["relative_path"], "relative_path")
            destination = _object_path(staging, item)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if hash_file(destination) != (
                    _text(item["sha256"], "sha256"),
                    _integer(item["bytes"], "bytes"),
                ):
                    raise Portfolio123GenerationError(
                        "staging object contains conflicting bytes"
                    )
            else:
                shutil.copy2(_safe_path(root, relative), destination)
    _write_create_only(staging / "source-index.json", _source_index_bytes(plan))
    _verify_authority(staging, plan)
    authority.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(authority)
    return authority


def _family(relative: str) -> str:
    name = PurePosixPath(relative).stem.casefold()
    rules = (
        ("app_account_positions", "account_positions"),
        ("app_account_resource", "account_metadata"),
        ("app_account_types", "account_metadata"),
        ("app_tradeactivity", "account_transactions"),
        ("app_investment_holdings", "managed_holdings"),
        ("app_investment_perfdata", "managed_performance"),
        ("app_investment_resource", "managed_metadata"),
        ("period_perf_dist", "period_performance_distribution"),
        ("period_perf", "period_performance"),
        ("transactions_all", "transactions"),
        ("_transactions", "transactions"),
        ("holdings_holdings", "holdings"),
        ("holdings_table", "holdings"),
        ("_holdings", "holdings"),
        ("holdings_port_summary", "recent_trades"),
        ("holdings_transact_list", "recent_trades"),
        ("strategy_portmodel", "equity_curve"),
        ("_perf", "equity_curve"),
        ("performance_stats", "performance_statistics"),
        ("quickstats", "quick_statistics"),
        ("trading_system_settings", "trading_system_settings"),
        ("buy_rules", "buy_rules"),
        ("sell_rules", "sell_rules"),
        ("port_summary", "strategy_summary"),
        ("invest_brokers", "reference_metadata"),
        ("invest_currencies", "reference_metadata"),
        ("invest_fx", "reference_metadata"),
        ("investbenchmarks", "reference_metadata"),
        ("app_state", "application_metadata"),
        ("app_getvars", "application_metadata"),
        ("spr_user", "application_metadata"),
        ("unknown_holdings", "recent_trades"),
    )
    for token, family in rules:
        if token in name:
            return family
    return "supporting_metadata"


def _csv_rows(path: Path, artifact_id: str, family: str) -> list[dict[str, str]]:
    entity = _ENTITY.match(PurePosixPath(artifact_id).name)
    rows: list[dict[str, str]] = []
    try:
        csv.field_size_limit(_MAX_CSV_FIELD_BYTES)
        handle = path.open("r", encoding="utf-8-sig", newline="")
        with handle:
            reader = csv.reader(handle)
            try:
                source_headers = next(reader)
            except StopIteration:
                return []
            seen: Counter[str] = Counter()
            headers: list[str] = []
            for index, value in enumerate(source_headers, start=1):
                base = value if value else f"_unnamed_column_{index}"
                seen[base] += 1
                headers.append(base if seen[base] == 1 else f"{base}__{seen[base]}")
            for number, values in enumerate(reader, start=1):
                normalized = {
                    (
                        headers[index]
                        if index < len(headers)
                        else f"_extra_column_{index + 1}"
                    ): value
                    for index, value in enumerate(values)
                }
                for index in range(len(values), len(headers)):
                    normalized[headers[index]] = ""
                normalized.update(
                    {
                        "_entity_id": entity.group(2) if entity else "",
                        "_entity_type": entity.group(1).casefold()
                        if entity
                        else "unknown",
                        "_source_artifact": artifact_id,
                        "_source_row": str(number),
                        "_transport": "browser_download"
                        if artifact_id.startswith("downloads/")
                        else "har_derived",
                    }
                )
                rows.append(normalized)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise Portfolio123GenerationError(
            f"cannot parse derived CSV: {artifact_id}"
        ) from exc
    return rows


def _write_parquet(
    rows: Iterable[Mapping[str, str]], path: Path
) -> tuple[str, int, int]:
    materialized = list(rows)
    columns = sorted({key for row in materialized for key in row})
    arrays = {
        column: pa.array(
            [row.get(column, "") for row in materialized], type=pa.string()
        )
        for column in columns
    }
    table = pa.table(arrays)
    pq.write_table(
        table,
        path,
        compression="NONE",
        data_page_version="1.0",
        use_dictionary=False,
        version="2.6",
        write_statistics=True,
    )
    digest, size = hash_file(path)
    return digest, size, len(materialized)


def _build_outputs(
    plan_path: Path, plan: Mapping[str, Any], authority: Path, work: Path
) -> tuple[dict[str, object], GenerationManifest, dict[str, Path]]:
    as_of = date.fromisoformat(_text(plan["as_of_date"], "as_of_date"))
    by_label = {_text(item["label"], "label"): item for item in _sources(plan)}
    outputs: dict[str, Path] = {}
    output_artifacts: list[SourceArtifact] = []
    legacy = by_label["legacy-data"]
    for item in _objects(legacy.get("files"), "files"):
        if item.get("disposition") != "BASELINE_PARQUET_TABLE":
            continue
        relative = _text(item["relative_path"], "relative_path")
        name = PurePosixPath(relative).name
        source_path = _object_path(authority, item)
        destination = work / "tables" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        digest, size = hash_file(destination)
        output_id = f"tables/{name}"
        outputs[output_id] = destination
        output_artifacts.append(
            SourceArtifact(
                output_id,
                digest,
                size,
                as_of,
                "portfolio123-legacy-table",
                "legacy-19-table",
            )
        )

    rows_by_family: dict[str, list[dict[str, str]]] = defaultdict(list)
    dataset_rows: Counter[str] = Counter()
    har = by_label["har-workspace"]
    for item in _objects(har.get("files"), "files"):
        if item.get("disposition") != "HAR_DERIVED_CSV":
            continue
        relative = _text(item["relative_path"], "relative_path")
        family = _family(relative)
        parsed = _csv_rows(_object_path(authority, item), relative, family)
        rows_by_family[family].extend(parsed)
        dataset_rows[family] += len(parsed)
    for family in sorted(rows_by_family):
        destination = work / "tables" / f"har_{family}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size, _ = _write_parquet(rows_by_family[family], destination)
        output_id = f"tables/{destination.name}"
        outputs[output_id] = destination
        output_artifacts.append(
            SourceArtifact(
                output_id, digest, size, as_of, f"portfolio123-har-{family}", "1"
            )
        )

    source_artifacts: list[SourceArtifact] = []
    dispositions: Counter[str] = Counter()
    for source in _sources(plan):
        label = _text(source["label"], "label")
        for item in _objects(source.get("files"), "files"):
            relative = _text(item["relative_path"], "relative_path")
            disposition = _text(item["disposition"], "disposition")
            dispositions[disposition] += 1
            source_artifacts.append(
                SourceArtifact(
                    f"source/{label}/{relative}",
                    _text(item["sha256"], "sha256"),
                    _integer(item["bytes"], "bytes"),
                    as_of,
                    f"portfolio123-{disposition.casefold().replace('_', '-')}",
                    "frozen-input",
                )
            )
    catalog: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "boundary": "exact hash-bound legacy database and derived CSV snapshot",
            "known_gaps": [
                "access-restricted strategies remain unavailable",
                "authenticated raw JSON and HTML evidence is preserved but not parsed under issue 19",
                "managed-strategy pagination remains limited to captured derived exports",
                "no freshness claim is made after the frozen input as-of date",
            ],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "disposition_counts": dict(sorted(dispositions.items())),
        "har_family_row_counts": dict(sorted(dataset_rows.items())),
        "legacy_table_count": len(_LEGACY_TABLES),
        "provider_id": "portfolio123",
        "reconciliation": {
            "har_csv_count": dispositions["HAR_DERIVED_CSV"],
            "har_output_family_count": len(rows_by_family),
            "legacy_table_count": len(_LEGACY_TABLES),
            "status": "PASS_EXACT_TO_HASH_BOUND_INPUTS",
            "unclassified_input_count": 0,
        },
    }
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_path = work / "portfolio123-generation.json"
    catalog_path.write_bytes(catalog_bytes)
    outputs["portfolio123-generation.json"] = catalog_path
    plan_hash, plan_size = hash_file(plan_path)
    artifacts = (
        *source_artifacts,
        *output_artifacts,
        SourceArtifact(
            "provenance/cutover-plan.json",
            plan_hash,
            plan_size,
            as_of,
            "portfolio123-cutover-plan",
            str(PLAN_SCHEMA_VERSION),
        ),
        SourceArtifact(
            "normalized/portfolio123-generation.json",
            hashlib.sha256(catalog_bytes).hexdigest(),
            len(catalog_bytes),
            as_of,
            "portfolio123-generation-catalog",
            str(CATALOG_SCHEMA_VERSION),
        ),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("quant-engine-portfolio123", ADAPTER_VERSION),
        coverage=CoverageClaim(
            CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            "exact hash-bound legacy database and derived CSV snapshot",
            tuple(sorted({"legacy_summary_database", *rows_by_family})),
            as_of,
            as_of,
            (),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, outputs


def _install_outputs(
    data_root: Path, manifest: GenerationManifest, outputs: Mapping[str, Path]
) -> Path:
    manifest_path = stage_generation(data_root, manifest)
    generation_root = manifest_path.parent
    for relative, source in outputs.items():
        destination = _safe_path(generation_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_create_only(destination, source.read_bytes())
    return generation_root


def stage_candidate(plan_path: Path) -> dict[str, object]:
    plan = _load_plan(plan_path)
    authority = _copy_sources(plan)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    with tempfile.TemporaryDirectory(prefix=".p123-build-", dir=data_root) as temporary:
        catalog, manifest, outputs = _build_outputs(
            plan_path, plan, authority, Path(temporary)
        )
        _install_outputs(data_root, manifest, outputs)
    return {
        "generation_id": manifest.generation_id,
        "har_csv_count": catalog["reconciliation"]["har_csv_count"],  # type: ignore[index]
        "har_output_family_count": catalog["reconciliation"]["har_output_family_count"],  # type: ignore[index]
        "legacy_table_count": catalog["legacy_table_count"],
        "published": False,
        "state": "STAGED_AND_VERIFIED",
    }


def _rebuild(
    plan_path: Path, generation_id: str
) -> tuple[dict[str, object], GenerationManifest]:
    plan = _load_plan(plan_path)
    _, authority = _run_paths(plan)
    _verify_authority(authority, plan)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    with tempfile.TemporaryDirectory(
        prefix=".p123-verify-", dir=data_root
    ) as temporary:
        catalog, manifest, outputs = _build_outputs(
            plan_path, plan, authority, Path(temporary)
        )
        if manifest.generation_id != generation_id:
            raise Portfolio123GenerationError("rebuilt generation differs")
        generation_root = data_root / "generations" / generation_id
        for relative, output_path in outputs.items():
            target = _safe_path(generation_root, relative)
            if not target.is_file() or hash_file(target) != hash_file(output_path):
                raise Portfolio123GenerationError(
                    f"published output differs: {relative}"
                )
    return catalog, manifest


def publish_candidate(plan_path: Path, generation_id: str) -> dict[str, object]:
    staged = stage_candidate(plan_path)
    if staged["generation_id"] != generation_id:
        raise Portfolio123GenerationError("accepted candidate identity differs")
    plan = _load_plan(plan_path)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    _rebuild(plan_path, generation_id)
    pointer = publish_generation(data_root, generation_id)
    return {
        "generation_id": pointer.generation_id,
        "published": True,
        "state": "PUBLISHED",
    }


def verify_published(plan_path: Path, generation_id: str) -> dict[str, object]:
    plan = _load_plan(plan_path)
    data_root = _root(
        Path(_text(plan["data_root"], "data_root")), "data_root", existing=True
    )
    catalog, _ = _rebuild(plan_path, generation_id)
    if read_current(data_root).generation_id != generation_id:
        raise Portfolio123GenerationError("CURRENT.json points to another generation")
    return {
        "generation_id": generation_id,
        "har_csv_count": catalog["reconciliation"]["har_csv_count"],  # type: ignore[index]
        "state": "VERIFIED_PUBLISHED",
    }


def _remove_empty_tree(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if os.name == "nt":
            os.chmod(directory, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        directory.rmdir()
    if os.name == "nt":
        os.chmod(root, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    root.rmdir()


def cutover_sources(plan_path: Path, generation_id: str) -> dict[str, object]:
    """Delete only the exact verified legacy sources after publication proof."""

    verify_published(plan_path, generation_id)
    plan = _load_plan(plan_path)
    _, authority = _run_paths(plan)
    removed: list[str] = []
    for source in _sources(plan):
        label = _text(source["label"], "label")
        root = Path(_text(source["root"], "root"))
        if not root.exists():
            removed.append(label)
            continue
        resolved = _root(root, "source_root", existing=True)
        _verify_source(resolved, source)
        for item in _objects(source.get("files"), "files"):
            path = _safe_path(resolved, _text(item["relative_path"], "relative_path"))
            path.unlink()
        _remove_empty_tree(resolved)
        removed.append(label)
    result: dict[str, object] = {
        "generation_id": generation_id,
        "original_sources_removed": sorted(removed),
        "recoverable_from": str(authority),
        "state": "SINGLE_AUTHORITY_CUTOVER_COMPLETE",
    }
    _write_create_only(
        plan_path.parent / "cutover-result.json", canonical_json_bytes(result)
    )
    return result

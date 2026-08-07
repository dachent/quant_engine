"""Build a version-bound LIVE_PLUS_BACKTEST decomposition target bundle."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence, cast

from quant_engine.core.contracts import canonical_json_bytes


TARGET_HEADERS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "return_type",
    "cost",
)
LAYERED_HEADERS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)


class EvidenceModeError(ValueError):
    """The mode source or composition violates the closed contract."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceModeError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceModeError(f"{name} must be non-empty text")
    return value.strip()


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceModeError(f"{name} is missing or linked")
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), name)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceModeError(f"{name} is not readable JSON") from exc


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _verify_source(root: Path, definition: Mapping[str, Any], name: str) -> Path:
    relative = PurePosixPath(_text(definition.get("relative_path"), f"{name}.path"))
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
        raise EvidenceModeError(f"{name} path is not portable")
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    if (
        not path.is_relative_to(resolved_root)
        or path.is_symlink()
        or not path.is_file()
    ):
        raise EvidenceModeError(f"{name} path is outside the source root")
    content = path.read_bytes()
    if _hash(content) != definition.get("expected_sha256") or len(
        content
    ) != definition.get("expected_size_bytes"):
        raise EvidenceModeError(f"{name} hash or size differs")
    return path


def _csv_rows(path: Path, headers: tuple[str, ...], name: str) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceModeError(f"{name} is missing or linked")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != headers:
            raise EvidenceModeError(f"{name} headers differ")
        rows = [dict(row) for row in reader]
    if not rows:
        raise EvidenceModeError(f"{name} is empty")
    return rows


def _csv_bytes(rows: Sequence[Sequence[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(TARGET_HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def build_bundle(
    *,
    base_spec_path: Path,
    base_source_root: Path,
    layered_returns_path: Path,
    correlation_spec_path: Path,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Build without writing; account-live remains authoritative at inception."""

    base_spec = _load_json(base_spec_path, "base specification")
    correlation = _load_json(correlation_spec_path, "correlation specification")
    if correlation.get("schema_version") != 2:
        raise EvidenceModeError("correlation specification must use schema version 2")
    target_definition = _object(base_spec.get("target_source"), "base target")
    base_target_path = _verify_source(
        base_source_root, target_definition, "base target"
    )
    base_target_content = base_target_path.read_bytes()
    base_rows = _csv_rows(base_target_path, TARGET_HEADERS, "base targets")
    factor_definition = _object(base_spec.get("factor_source"), "base factor")
    base_factor_path = _verify_source(
        base_source_root, factor_definition, "base factor"
    )
    layered_definition = _object(correlation.get("return_source"), "return source")
    if layered_returns_path.is_symlink() or not layered_returns_path.is_file():
        raise EvidenceModeError("layered returns are missing or linked")
    layered_content = layered_returns_path.read_bytes()
    if _hash(layered_content) != layered_definition.get("expected_sha256") or len(
        layered_content
    ) != layered_definition.get("expected_size_bytes"):
        raise EvidenceModeError("layered returns differ from the correlation contract")
    layered_rows = _csv_rows(layered_returns_path, LAYERED_HEADERS, "layered returns")
    raw_definitions = correlation.get("evidence_mode_series")
    if not isinstance(raw_definitions, list):
        raise EvidenceModeError("mode definitions must be an array")
    definitions: dict[str, dict[str, Any]] = {}
    for index, raw_definition in enumerate(raw_definitions):
        definition = _object(raw_definition, f"mode definition {index}")
        series_id = _text(definition.get("series_id"), "mode series_id")
        if series_id in definitions:
            raise EvidenceModeError("mode definitions contain duplicate series")
        definitions[series_id] = definition
    if set(definitions) != set(correlation.get("series_ids", [])):
        raise EvidenceModeError("mode definitions differ from correlation series")

    base_by_series: dict[str, list[dict[str, str]]] = {}
    for row in base_rows:
        series_id = _text(row.get("series_id"), "base series_id")
        if row["evidence_layer"] == "BACKTEST":
            raise EvidenceModeError("base targets must be live-only")
        base_by_series.setdefault(series_id, []).append(row)
    if not set(base_by_series) <= set(definitions):
        raise EvidenceModeError("base target series lack version definitions")

    layered_by_series: dict[str, list[dict[str, str]]] = {}
    for row in layered_rows:
        series_id = _text(row.get("series_id"), "layered series_id")
        definition = definitions.get(series_id)
        if definition is None:
            raise EvidenceModeError("layered return series is undeclared")
        if row["strategy_version"] != definition.get("strategy_version"):
            raise EvidenceModeError(f"layered strategy version differs: {series_id}")
        if row["evidence_layer"] not in {
            "ACCOUNT_LIVE",
            "PROVIDER_LIVE",
            "BACKTEST",
        }:
            raise EvidenceModeError("layered evidence layer is unsupported")
        layered_by_series.setdefault(series_id, []).append(row)

    output_rows: list[tuple[str, ...]] = []
    composition: list[dict[str, object]] = []
    live_composition: list[dict[str, object]] = []
    for series_id, live_rows in sorted(base_by_series.items()):
        live_rows = sorted(live_rows, key=lambda item: item["date"])
        live_inception = date.fromisoformat(live_rows[0]["date"])
        definition = definitions[series_id]
        live_layer = _text(definition.get("live_evidence_layer"), "live layer")
        if {row["evidence_layer"] for row in live_rows} != {live_layer}:
            raise EvidenceModeError(f"base live layer differs: {series_id}")
        semantics = {(row["return_basis"], row["return_type"]) for row in live_rows}
        if len(semantics) != 1:
            raise EvidenceModeError(f"base return semantics differ: {series_id}")
        return_basis, return_type = next(iter(semantics))
        declared_backtest = [
            row
            for row in layered_by_series.get(series_id, [])
            if row["evidence_layer"] == "BACKTEST"
        ]
        overlapping_backtest = [
            row
            for row in declared_backtest
            if date.fromisoformat(row["date"]) >= live_inception
        ]
        accepted_backtest = sorted(
            (
                row
                for row in declared_backtest
                if date.fromisoformat(row["date"]) < live_inception
            ),
            key=lambda item: item["date"],
        )
        backtest_dates = [row["date"] for row in accepted_backtest]
        if len(backtest_dates) != len(set(backtest_dates)):
            raise EvidenceModeError(f"backtest dates are duplicated: {series_id}")
        for row in accepted_backtest:
            output_rows.append(
                (
                    series_id,
                    row["date"],
                    row["return"],
                    "BACKTEST",
                    return_basis,
                    return_type,
                    "",
                )
            )
        output_rows.extend(
            tuple(row[header] for header in TARGET_HEADERS) for row in live_rows
        )
        composition.append(
            {
                "backtest_observations_used": len(accepted_backtest),
                "evidence_mode": "LIVE_PLUS_BACKTEST",
                "live_evidence_layer": live_layer,
                "live_inception": live_inception.isoformat(),
                "live_observations": len(live_rows),
                "overlapping_backtest_observations_rejected": len(overlapping_backtest),
                "series_id": series_id,
                "status": (
                    "AVAILABLE_LIVE_PLUS_VERSION_MATCHED_BACKTEST"
                    if accepted_backtest
                    else "AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY"
                ),
                "strategy_version": definition["strategy_version"],
            }
        )
        live_composition.append(
            {
                "backtest_observations_used": 0,
                "evidence_mode": "LIVE_ONLY",
                "live_evidence_layer": live_layer,
                "live_inception": live_inception.isoformat(),
                "live_observations": len(live_rows),
                "overlapping_backtest_observations_rejected": 0,
                "series_id": series_id,
                "status": "AVAILABLE_LIVE_ONLY",
                "strategy_version": definition["strategy_version"],
            }
        )
    target_content = _csv_bytes(output_rows)
    factor_content = base_factor_path.read_bytes()
    outputs: dict[str, bytes] = {
        "factors.csv": factor_content,
        "live-targets.csv": base_target_content,
        "targets.csv": target_content,
    }
    rebound = dict(base_spec)
    rebound["evidence_mode"] = "LIVE_PLUS_BACKTEST"
    rebound["series_composition"] = {
        cast(str, item["series_id"]): item for item in composition
    }
    rebound["target_source"] = {
        **target_definition,
        "artifact_id": "evidence-modes/live-plus-backtest-targets.csv",
        "expected_sha256": _hash(target_content),
        "expected_size_bytes": len(target_content),
        "relative_path": "targets.csv",
    }
    rebound["factor_source"] = {**factor_definition, "relative_path": "factors.csv"}
    live_rebound = dict(base_spec)
    live_rebound["evidence_mode"] = "LIVE_ONLY"
    live_rebound["series_composition"] = {
        cast(str, item["series_id"]): item for item in live_composition
    }
    live_rebound["target_source"] = {
        **target_definition,
        "relative_path": "live-targets.csv",
    }
    live_rebound["factor_source"] = {
        **factor_definition,
        "relative_path": "factors.csv",
    }
    rewritten_provenance: list[dict[str, object]] = []
    for index, raw in enumerate(
        cast(list[dict[str, Any]], base_spec.get("provenance_sources", []))
    ):
        definition = _object(raw, f"provenance {index}")
        source = _verify_source(base_source_root, definition, f"provenance {index}")
        relative = f"provenance/{index:02d}-{source.name}"
        content = source.read_bytes()
        outputs[relative] = content
        rewritten_provenance.append({**definition, "relative_path": relative})
    rebound["provenance_sources"] = rewritten_provenance
    live_rebound["provenance_sources"] = rewritten_provenance
    outputs["base-spec.json"] = canonical_json_bytes(rebound)
    outputs["live-base-spec.json"] = canonical_json_bytes(live_rebound)
    composition_payload = {
        "evidence_mode": "LIVE_PLUS_BACKTEST",
        "policy": "VERSION_MATCHED_BACKTEST_STRICTLY_BEFORE_LIVE_INCEPTION_LIVE_WINS",
        "series": composition,
    }
    outputs["composition.json"] = canonical_json_bytes(composition_payload)
    manifest: dict[str, object] = {
        "builder": "portfolio-decomposition-evidence-modes",
        "builder_version": "1.0.0",
        "evidence_mode": "LIVE_PLUS_BACKTEST",
        "inputs": {
            "base_spec_sha256": _hash(base_spec_path.read_bytes()),
            "correlation_spec_sha256": _hash(correlation_spec_path.read_bytes()),
            "layered_returns_sha256": _hash(layered_content),
        },
        "outputs": [
            {"path": path, "sha256": _hash(content), "size_bytes": len(content)}
            for path, content in sorted(outputs.items())
        ],
        "schema_version": 1,
    }
    manifest["bundle_id"] = _hash(canonical_json_bytes(manifest))
    return outputs, manifest


def write_bundle(output_root: Path, **kwargs: Any) -> dict[str, object]:
    outputs, manifest = build_bundle(**kwargs)
    bundle_id = cast(str, manifest["bundle_id"])
    destination = output_root / bundle_id
    destination.mkdir(parents=True, exist_ok=True)
    expected = {**outputs, "manifest.json": canonical_json_bytes(manifest)}
    for relative, content in expected.items():
        path = destination / PurePosixPath(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != content:
                raise EvidenceModeError(f"bundle artifact differs: {relative}")
        else:
            path.write_bytes(content)
    return {
        "bundle_id": bundle_id,
        "destination": str(destination),
        "manifest": manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-spec", type=Path, required=True)
    parser.add_argument("--base-source-root", type=Path, required=True)
    parser.add_argument("--layered-returns", type=Path, required=True)
    parser.add_argument("--correlation-spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = write_bundle(
        args.output_root,
        base_spec_path=args.base_spec,
        base_source_root=args.base_source_root,
        layered_returns_path=args.layered_returns,
        correlation_spec_path=args.correlation_spec,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

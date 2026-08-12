from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core import read_current


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_provenance.py"
SPEC = importlib.util.spec_from_file_location("portfolio_provenance", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio provenance builder")
provenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provenance
SPEC.loader.exec_module(provenance)


def _csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _spec(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source_root = tmp_path / "private-source"
    source_root.mkdir(parents=True)
    _csv(
        source_root / "assignments.csv",
        provenance._ASSIGNMENT_FIELDS,
        [
            {
                "institution": "SYNTHETIC BROKER",
                "account": "SYNTHETIC ACCOUNT A",
                "strategy": "Synthetic Strategy 1",
                "period_start": "2026-01-01",
                "period_end": "",
                "source": "synthetic_control",
            },
            {
                "institution": "SYNTHETIC BROKER",
                "account": "SYNTHETIC ACCOUNT B",
                "strategy": "General",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "source": "synthetic_gap_fill",
            },
        ],
    )
    _csv(
        source_root / "corrections.csv",
        provenance._CORRECTION_FIELDS,
        [
            {
                "institution": "SYNTHETIC BROKER",
                "account": "SYNTHETIC ACCOUNT A",
                "strategy": "Synthetic Strategy 1",
                "field": "start_date",
                "old_value": "2025-12-31",
                "new_value": "2026-01-01",
                "evidence": "SYNTHETIC correction evidence",
            }
        ],
    )
    _csv(source_root / "open_gaps.csv", provenance._OPEN_GAP_FIELDS, [])
    (source_root / "anchor.txt").write_bytes(b"SYNTHETIC anchor\n")
    relative_paths = [
        "anchor.txt",
        "assignments.csv",
        "corrections.csv",
        "open_gaps.csv",
    ]
    strategies = []
    strategy_versions = []
    unavailable_slot = {
        "artifact_sha256": None,
        "disposition": "SYNTHETIC evidence is deliberately unavailable.",
        "evidence_layer": None,
        "observed_end": None,
        "observed_start": None,
        "source_ids": ["synthetic-source"],
        "status": "UNAVAILABLE",
        "version_ref": None,
    }
    source_route_slot = {
        "artifact_sha256": None,
        "disposition": "SYNTHETIC hash-bound source route only.",
        "evidence_layer": "ACCOUNT_LIVE",
        "observed_end": None,
        "observed_start": None,
        "source_ids": ["synthetic-source"],
        "status": "REFERENCE_ONLY",
        "version_ref": None,
    }
    for index in range(1, 10):
        strategies.append(
            {
                "aliases": [f"Synthetic Strategy {index}"],
                "cost_status": "KNOWN" if index == 1 else "UNAVAILABLE",
                "coverage_status": "PARTIAL",
                "economic_family": f"SYNTHETIC family {index}",
                "evidence_routes": [
                    {
                        "evidence_layer": "ACCOUNT_LIVE",
                        "purpose": "SYNTHETIC current evidence",
                        "source_id": "synthetic-source",
                    },
                    {
                        "evidence_layer": "BACKTEST",
                        "purpose": "SYNTHETIC comparison only",
                        "source_id": "synthetic-source",
                    },
                ],
                "gaps": ["SYNTHETIC incomplete history"],
                "name": f"Synthetic Strategy {index}",
                "strategy_id": f"strategy-{index}",
                "version_ref": None,
            }
        )
        strategy_versions.append(
            {
                "backtest": dict(unavailable_slot),
                "benchmark": dict(unavailable_slot),
                "compatibility_reason": "SYNTHETIC version is unresolved.",
                "compatibility_status": "UNRESOLVED",
                "configuration": dict(unavailable_slot),
                "costs": dict(unavailable_slot),
                "design": dict(unavailable_slot),
                "disposition": "UNRESOLVED",
                "disposition_reason": "SYNTHETIC authoritative version is absent.",
                "effective_end": None,
                "effective_start": "2026-07-30",
                "evidence_layers": [
                    {
                        "artifact_sha256": None,
                        "disposition": "SYNTHETIC account-live route is reference only.",
                        "evidence_layer": "ACCOUNT_LIVE",
                        "observed_end": None,
                        "observed_start": None,
                        "source_id": "synthetic-source",
                        "status": "REFERENCE_ONLY",
                        "version_ref": None,
                    },
                    {
                        "artifact_sha256": None,
                        "disposition": "SYNTHETIC backtest is unavailable.",
                        "evidence_layer": "BACKTEST",
                        "observed_end": None,
                        "observed_start": None,
                        "source_id": "synthetic-source",
                        "status": "UNAVAILABLE",
                        "version_ref": None,
                    },
                ],
                "gaps": ["SYNTHETIC authoritative version is absent."],
                "live_boundary": dict(unavailable_slot),
                "source_route": dict(source_route_slot),
                "strategy_id": f"strategy-{index}",
                "version_ref": None,
            }
        )
    spec: dict[str, Any] = {
        "acquisition_date": "2026-07-30",
        "as_of_date": "2026-07-30",
        "assumptions": [
            {
                "assumption_id": "synthetic-assumption",
                "description": "SYNTHETIC assumption for validation",
                "source_routes": ["synthetic-source"],
                "status": "INFERRED",
            }
        ],
        "policy_version": "PORT-PERF-1",
        "reserve_account": {
            "account_id": "reserve-classification",
            "aliases": ["SYNTHETIC reserve"],
            "name": "SYNTHETIC reserve classification",
            "source_routes": ["synthetic-source"],
        },
        "roots": {"synthetic-root": str(source_root)},
        "schema_version": 2,
        "source_groups": [
            {
                "as_of_date": "2026-07-30",
                "disposition": "PROVENANCE_ANCHOR",
                "expected_hashes": {
                    "anchor.txt": hashlib.sha256(b"SYNTHETIC anchor\n").hexdigest()
                },
                "relative_paths": relative_paths,
                "root_id": "synthetic-root",
                "source_id": "synthetic-source",
            }
        ],
        "strategies": strategies,
        "strategy_version_catalog": {
            "as_of_date": "2026-07-30",
            "catalog_schema_version": 2,
            "strategies": strategy_versions,
        },
        "tracker_imports": {
            "assignments": {
                "relative_path": "assignments.csv",
                "root_id": "synthetic-root",
            },
            "corrections": {
                "relative_path": "corrections.csv",
                "root_id": "synthetic-root",
            },
            "open_gaps": {
                "relative_path": "open_gaps.csv",
                "root_id": "synthetic-root",
            },
            "unmapped_labels": ["General"],
        },
    }
    spec_path = tmp_path / "private-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, spec


def test_stage_is_deterministic_private_and_imports_explicit_tracker_evidence(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    output_root = tmp_path / "private-output"

    first = provenance.stage_catalog(spec_path, output_root)
    second = provenance.stage_catalog(spec_path, output_root)

    assert first == second
    assert first["strategy_count"] == 9
    assert first["source_file_count"] == 4
    assert first["correction_count"] == 1
    assert first["open_gap_count"] == 0
    generation_dir = output_root / "generations" / first["generation_id"]
    catalog_bytes = (generation_dir / "portfolio-provenance.json").read_bytes()
    catalog = json.loads(catalog_bytes)
    assert str(spec["roots"]["synthetic-root"]) not in catalog_bytes.decode()
    assert {item["role"] for item in catalog["accounts"]} == {
        "INVESTMENT",
        "RESERVE",
    }
    assert {item["role"] for item in catalog["capital_pools"]} == {
        "SYSTEMATIC",
        "RESERVE",
    }
    assert {item["pool_mapping_status"] for item in catalog["assignments"]} == {
        "MAPPED"
    }
    assert {
        item["strategy_mapping_disposition"] for item in catalog["assignments"]
    } == {"STRATEGY_MAPPED", "HISTORICAL_POOL_ONLY"}
    assert {item["mapping_status"] for item in catalog["assignments"]} == {
        "MAPPED",
        "UNMAPPED",
    }
    assert (
        catalog["strategies"][0]["evidence_routes"][0]["evidence_layer"]
        != (catalog["strategies"][0]["evidence_routes"][1]["evidence_layer"])
    )
    assert {item["version_disposition"] for item in catalog["strategies"]} == {
        "UNRESOLVED"
    }
    assert len(catalog["strategy_version_catalog"]["strategies"]) == 9
    assert {
        item["compatibility_status"]
        for item in catalog["strategy_version_catalog"]["strategies"]
    } == {"UNRESOLVED"}
    assert first["catalog_sha256"] == hashlib.sha256(catalog_bytes).hexdigest()
    assert not (output_root / "CURRENT.json").exists()


def test_input_order_does_not_change_generation_identity(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path)
    original = provenance.build_catalog(spec_path)
    spec["source_groups"][0]["relative_paths"].reverse()
    spec["strategies"].reverse()
    spec["strategy_version_catalog"]["strategies"].reverse()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    reordered = provenance.build_catalog(spec_path)

    assert original[2] == reordered[2]
    assert original[1].generation_id == reordered[1].generation_id


def test_publish_rebuilds_exact_generation_and_advances_pointer(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)
    output_root = tmp_path / "private-output"
    staged = provenance.stage_catalog(spec_path, output_root)

    with pytest.raises(provenance.ProvenanceValidationError, match="differs"):
        provenance.publish_catalog(spec_path, output_root, "0" * 64)
    assert not (output_root / "CURRENT.json").exists()

    published = provenance.publish_catalog(
        spec_path, output_root, staged["generation_id"]
    )
    assert published["publication_state"] == "PUBLISHED"
    assert read_current(output_root).generation_id == staged["generation_id"]


def test_hash_drift_unknown_labels_and_strategy_count_fail_closed(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    spec["source_groups"][0]["expected_hashes"]["anchor.txt"] = "0" * 64
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(provenance.ProvenanceValidationError, match="hash differs"):
        provenance.build_catalog(spec_path)

    spec_path, spec = _spec(tmp_path / "unknown-label")
    assignments = Path(spec["roots"]["synthetic-root"]) / "assignments.csv"
    rows = list(csv.DictReader(assignments.read_text(encoding="utf-8").splitlines()))
    rows[0]["strategy"] = "Unexpected Strategy"
    _csv(assignments, provenance._ASSIGNMENT_FIELDS, rows)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(provenance.ProvenanceValidationError, match="neither mapped"):
        provenance.build_catalog(spec_path)

    spec_path, spec = _spec(tmp_path / "eight-strategies")
    spec["strategies"].pop()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(provenance.ProvenanceValidationError, match="exactly 9"):
        provenance.build_catalog(spec_path)


def test_strategy_version_catalog_requires_one_row_per_current_strategy(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    spec["strategy_version_catalog"]["strategies"].pop()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(provenance.ProvenanceValidationError, match="exactly 9"):
        provenance.build_catalog(spec_path)


def test_strategy_version_evidence_must_bind_an_admitted_source_hash(
    tmp_path: Path,
) -> None:
    spec_path, spec = _spec(tmp_path)
    design = spec["strategy_version_catalog"]["strategies"][0]["design"]
    design.update(
        {
            "artifact_sha256": "0" * 64,
            "evidence_layer": "BACKTEST",
            "status": "EVIDENCED",
        }
    )
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(
        provenance.ProvenanceValidationError,
        match="not an admitted source artifact",
    ):
        provenance.build_catalog(spec_path)


def test_spec_and_catalog_schemas_preserve_closed_top_level_contracts() -> None:
    spec_schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "provenance-spec.schema.json"
        ).read_text(encoding="utf-8")
    )
    catalog_schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "provenance-catalog.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert spec_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert spec_schema["additionalProperties"] is False
    assert spec_schema["properties"]["schema_version"]["const"] == 2
    assert "strategy_version_catalog" in spec_schema["required"]
    assert spec_schema["properties"]["strategies"]["minItems"] == 9
    assert spec_schema["properties"]["strategies"]["maxItems"] == 9
    assert (
        set(
            spec_schema["$defs"]["evidence_route"]["properties"]["evidence_layer"][
                "enum"
            ]
        )
        == provenance._EVIDENCE_LAYERS
    )
    assert catalog_schema["additionalProperties"] is False
    assert catalog_schema["properties"]["catalog_schema_version"]["const"] == 2
    assert "strategy_version_catalog" in catalog_schema["required"]
    assert catalog_schema["properties"]["strategies"]["minItems"] == 9


def test_cli_reports_sanitized_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path, spec = _spec(tmp_path)
    output_root = tmp_path / "private-output"

    assert (
        provenance.main(
            ["stage", "--spec", str(spec_path), "--output-root", str(output_root)]
        )
        == 0
    )
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["strategy_count"] == 9
    assert str(spec["roots"]["synthetic-root"]) not in output
    assert "Synthetic Strategy" not in output

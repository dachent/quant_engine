from __future__ import annotations

import copy
import csv
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, getcontext, localcontext
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_target_series_layered_history",
    ROOT / "tools" / "portfolio_target_series.py",
)
assert SPEC is not None and SPEC.loader is not None
target_series = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = target_series
SPEC.loader.exec_module(target_series)

TargetSeriesError = target_series.TargetSeriesError
build_layered_history_generation = target_series.build_layered_history_generation
validate_layered_history_generation = target_series.validate_layered_history_generation
write_layered_history_generation = target_series.write_layered_history_generation
verify_layered_history_generation = target_series.verify_layered_history_generation


def test_quantbase_account_live_replaces_raw_flow_spike_with_proven_recalculation() -> (
    None
):
    """A proven Quantbase booking date selects the independently adjusted return."""

    ledger_rows = [
        {
            "date": "2024-10-16",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "series_id": "Volatis Strategy",
        },
        {
            "date": "2024-10-17",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.9",
            "series_id": "Volatis Strategy",
        },
        {
            "date": "2024-10-18",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.03",
            "series_id": "Volatis Strategy",
        },
    ]
    recalculated_rows = [
        {**row, "return": return_value}
        for row, return_value in zip(
            ledger_rows,
            ("0.01", "0.02", "0.03"),
            strict=True,
        )
    ]

    selected = target_series._quantbase_account_live_from_ledger(
        ledger_rows,
        recalculated_rows,
        {"2024-10-17"},
    )

    assert [row["return"] for row in selected] == ["0.01", "0.02", "0.03"]
    assert (
        target_series._quantbase_account_live_from_ledger(
            recalculated_rows,
            recalculated_rows,
            {"2024-10-17"},
        )
        == recalculated_rows
    )


def test_quantbase_account_live_rejects_nonflow_mismatch_even_when_other_flow_is_proven() -> (
    None
):
    """The narrow booked-flow exception cannot widen to ordinary performance dates."""

    ledger_rows = [
        {
            "date": "2024-10-16",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.01",
            "series_id": "Volatis Strategy",
        },
        {
            "date": "2024-10-17",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.9",
            "series_id": "Volatis Strategy",
        },
        {
            "date": "2024-10-18",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.7",
            "series_id": "Volatis Strategy",
        },
    ]
    recalculated_rows = [
        {**row, "return": return_value}
        for row, return_value in zip(
            ledger_rows,
            ("0.01", "0.02", "0.03"),
            strict=True,
        )
    ]

    with pytest.raises(
        TargetSeriesError,
        match="Quantbase account-live return differs from provider ledger",
    ):
        target_series._quantbase_account_live_from_ledger(
            ledger_rows,
            recalculated_rows,
            {"2024-10-17"},
        )


def test_quantbase_account_live_does_not_treat_unused_date_as_flow_proof() -> None:
    """A supplied date outside the return perimeter cannot authorize a mismatch."""

    ledger_rows = [
        {
            "date": "2024-10-16",
            "evidence_layer": "ACCOUNT_LIVE",
            "return": "0.7",
            "series_id": "Volatis Strategy",
        }
    ]
    recalculated_rows = [{**ledger_rows[0], "return": "0.01"}]

    with pytest.raises(
        TargetSeriesError,
        match="Quantbase account-live return differs from provider ledger",
    ):
        target_series._quantbase_account_live_from_ledger(
            ledger_rows,
            recalculated_rows,
            {"2030-01-01"},
        )


def test_layered_history_derives_quantbase_exception_from_returned_flow_proof(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    """An opening-only Quantbase booking cannot excuse a later ledger spike."""

    fixture = _history_fixture(tmp_path)
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    row = next(
        candidate
        for candidate in catalog["strategy_returns"]
        if candidate["strategy_id"] == "volatis-strategy"
        and candidate["date"] == "2024-10-16"
    )
    row["return"] = "0.9"
    _write_json(catalog_path, catalog)
    manifest_path = fixture["provider_ledger_manifest_path"]
    generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [_artifact(catalog_path, "normalized/provider-ledger.json")],
    )
    _pointer(fixture["provider_ledger_pointer_path"], generation_id, manifest_path)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(fixture["contract_path"], contract)

    with pytest.raises(
        TargetSeriesError,
        match="Quantbase account-live return differs from provider ledger",
    ):
        _build(fixture)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def _artifact(path: Path, artifact_id: str) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _core_generation_manifest(
    path: Path,
    adapter_name: str,
    as_of: str,
    artifacts: list[dict[str, object]],
    coverage_extra: dict[str, object] | None = None,
) -> str:
    """Write a valid core GenerationManifest for a synthetic source fixture."""

    coverage = {
        "boundary": "synthetic provider-ledger fixture",
        "fact_types": ["strategy_returns"],
        "known_gaps": [],
        "observed_end": as_of,
        "observed_start": as_of,
        "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
    }
    if coverage_extra is not None:
        coverage.update(coverage_extra)
    manifest = GenerationManifest.build(
        as_of_date=date.fromisoformat(as_of),
        adapter=AdapterIdentity(name=adapter_name, version="1.0.0"),
        coverage=CoverageClaim.from_dict(coverage),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=str(artifact["artifact_id"]),
                sha256=str(artifact["sha256"]),
                size_bytes=int(artifact["size_bytes"]),
                as_of_date=date.fromisoformat(str(artifact.get("as_of_date", as_of))),
                schema_name=str(
                    (artifact.get("schema") or {}).get(
                        "name", "synthetic-source-artifact"
                    )
                ),
                schema_version=str((artifact.get("schema") or {}).get("version", "1")),
            )
            for artifact in artifacts
        ),
        verification=VerificationStatus.VERIFIED,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest.to_json_bytes())
    return manifest.generation_id


def _pointer(path: Path, generation_id: str, manifest_path: Path) -> None:
    source_name = {
        "ledger": "provider-ledger",
        "matrixport": "matrixport",
        "quantbase": "quantbase",
        "p123": "portfolio123",
    }[path.parent.name]
    manifest_content = manifest_path.read_bytes()
    manifest = json.loads(manifest_content)
    _write_json(
        path,
        {
            "artifact_inventory_sha256": (
                target_series._source_generation_inventory_sha256(
                    source_name, manifest_content, manifest
                )
            ),
            "generation_id": generation_id,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "pointer_schema_version": 2,
            "state": "PUBLISHED",
        },
    )


def test_immutable_provider_ledger_manifest_accepts_core_generation_envelope(
    tmp_path: Path,
) -> None:
    """Accept the real provider-ledger envelope, not the catalog-style fixture."""

    manifest = {
        "adapter": {"name": "quant-engine-provider-ledger", "version": "1.0.0"},
        "artifacts": [
            {
                "artifact_id": "normalized/provider-ledger.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "provider-ledger-catalog", "version": "1"},
                "sha256": "a" * 64,
                "size_bytes": 123,
            }
        ],
        "as_of_date": "2026-08-13",
        "coverage": {
            "boundary": "accepted provider-ledger facts",
            "fact_types": ["nav_observations"],
            "known_gaps": [],
            "observed_end": "2026-08-13",
            "observed_start": "2026-01-01",
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "manifest_schema_version": 1,
        "verification": "VERIFIED",
    }

    def write_pinned(value: dict[str, object]) -> tuple[Path, dict[str, object]]:
        payload = copy.deepcopy(value)
        payload["generation_id"] = hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest()
        path = tmp_path / "ledger" / "generation.json"
        _write_json(path, payload)
        return path, {
            "schema": {"name": "provider-ledger", "version": "1"},
            "current_generation": {
                "as_of": payload["as_of_date"],
                "generation_id": payload["generation_id"],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            },
        }

    path, source = write_pinned(manifest)
    _accepted, generation_id, as_of = target_series._verified_immutable_generation(
        manifest_path=path, source=source, name="provider ledger"
    )
    assert generation_id == source["current_generation"]["generation_id"]
    assert as_of == "2026-08-13"

    pointer_path = path.parent / "CURRENT.json"
    _pointer(pointer_path, generation_id, path)
    _current, current_generation_id, current_as_of = (
        target_series._verified_current_generation(
            pointer_path=pointer_path,
            manifest_path=path,
            source=source,
            name="provider ledger",
            inventory_profile="provider-ledger",
        )
    )
    assert (current_generation_id, current_as_of) == (generation_id, as_of)
    cross_substituted_source = copy.deepcopy(source)
    cross_substituted_source["schema"] = {
        "name": "matrixport-generation-catalog",
        "version": "2",
    }
    with pytest.raises(TargetSeriesError):
        target_series._verified_current_generation(
            pointer_path=pointer_path,
            manifest_path=path,
            source=cross_substituted_source,
            name="provider ledger",
            inventory_profile="provider-ledger",
        )

    malformed_source = copy.deepcopy(source)
    malformed_source["schema"]["name"] = []
    with pytest.raises(TargetSeriesError):
        target_series._verified_immutable_generation(
            manifest_path=path, source=malformed_source, name="provider ledger"
        )
    with pytest.raises(TargetSeriesError):
        target_series._verified_current_generation(
            pointer_path=pointer_path,
            manifest_path=path,
            source=malformed_source,
            name="provider ledger",
            inventory_profile="provider-ledger",
        )

    for field, value in (
        ("generation_id", "0" * 64),
        ("sha256", "0" * 64),
        ("as_of", "2026-08-14"),
    ):
        mismatched_source = copy.deepcopy(source)
        mismatched_source["current_generation"][field] = value
        with pytest.raises(TargetSeriesError):
            target_series._verified_immutable_generation(
                manifest_path=path, source=mismatched_source, name="provider ledger"
            )

    for schema in (
        {"name": "matrixport-generation-catalog", "version": "2"},
        {"name": "quantbase-generation", "version": "1"},
        {"name": "portfolio123-generation", "version": "1"},
    ):
        generic_source = copy.deepcopy(source)
        generic_source["schema"] = schema
        with pytest.raises(TargetSeriesError):
            target_series._verified_immutable_generation(
                manifest_path=path, source=generic_source, name="generic provider"
            )

    for mutation in (
        {"verification": "PENDING"},
        {"manifest_schema_version": 2},
        {"adapter": {"name": "wrong-adapter", "version": "1.0.0"}},
    ):
        mutated = copy.deepcopy(manifest)
        mutated.update(mutation)
        path, source = write_pinned(mutated)
        with pytest.raises(TargetSeriesError):
            target_series._verified_immutable_generation(
                manifest_path=path, source=source, name="provider ledger"
            )


@pytest.mark.parametrize(
    ("directory", "schema", "adapter", "inventory_profile", "other_schema"),
    (
        (
            "ledger",
            {"name": "provider-ledger", "version": "1"},
            "quant-engine-provider-ledger",
            "provider-ledger",
            {"name": "matrixport-generation-catalog", "version": "2"},
        ),
        (
            "matrixport",
            {"name": "matrixport-generation-catalog", "version": "2"},
            "quant-engine-matrixport",
            "matrixport",
            {"name": "quantbase-generation", "version": "1"},
        ),
        (
            "quantbase",
            {"name": "quantbase-generation", "version": "1"},
            "quant-engine-web-contract-harvester",
            "quantbase",
            {"name": "portfolio123-generation", "version": "1"},
        ),
        (
            "p123",
            {"name": "portfolio123-generation", "version": "1"},
            "quant-engine-portfolio123",
            "portfolio123",
            {"name": "provider-ledger", "version": "1"},
        ),
    ),
)
def test_core_generation_envelopes_bind_each_contract_schema_to_its_adapter(
    tmp_path: Path,
    directory: str,
    schema: dict[str, str],
    adapter: str,
    inventory_profile: str,
    other_schema: dict[str, str],
) -> None:
    """Every retained source admits only its own raw core envelope."""

    manifest_path = tmp_path / directory / "generation.json"
    coverage_extra = (
        {"declared_exclusions": _matrixport_exclusions("2026-08-12")}
        if directory == "matrixport"
        else None
    )
    generation_id = _core_generation_manifest(
        manifest_path,
        adapter,
        "2026-08-13",
        [
            {
                "artifact_id": "normalized/test.json",
                "sha256": "b" * 64,
                "size_bytes": 1,
            }
        ],
        coverage_extra,
    )
    source = {
        "schema": schema,
        "current_generation": {
            "as_of": "2026-08-13",
            "generation_id": generation_id,
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
    }
    assert target_series._verified_immutable_generation(
        manifest_path=manifest_path, source=source, name=directory
    )[1:] == (generation_id, "2026-08-13")

    pointer_path = manifest_path.parent / "CURRENT.json"
    _pointer(pointer_path, generation_id, manifest_path)
    assert target_series._verified_current_generation(
        pointer_path=pointer_path,
        manifest_path=manifest_path,
        source=source,
        name=directory,
        inventory_profile=inventory_profile,
    )[1:] == (generation_id, "2026-08-13")

    substituted = copy.deepcopy(source)
    substituted["schema"] = other_schema
    with pytest.raises(TargetSeriesError):
        target_series._verified_immutable_generation(
            manifest_path=manifest_path, source=substituted, name=directory
        )
    with pytest.raises(TargetSeriesError):
        target_series._verified_current_generation(
            pointer_path=pointer_path,
            manifest_path=manifest_path,
            source=substituted,
            name=directory,
            inventory_profile=inventory_profile,
        )


def test_provider_ledger_inventory_binds_admission_receipt() -> None:
    manifest = {
        "artifacts": [
            {
                "artifact_id": "normalized/provider-ledger.json",
                "sha256": "1" * 64,
                "size_bytes": 101,
            },
            {
                "artifact_id": "normalized/admit-or-declare-receipt.json",
                "sha256": "2" * 64,
                "size_bytes": 202,
            },
        ]
    }
    manifest_bytes = canonical_json_bytes(manifest)
    files = sorted(
        [
            {
                "relative_path": "generation.json",
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "size_bytes": len(manifest_bytes),
            },
            {
                "relative_path": "provider-ledger.json",
                "sha256": "1" * 64,
                "size_bytes": 101,
            },
            {
                "relative_path": "admit-or-declare-receipt.json",
                "sha256": "2" * 64,
                "size_bytes": 202,
            },
        ],
        key=lambda item: item["relative_path"],
    )
    expected = hashlib.sha256(
        canonical_json_bytes({"files": files, "inventory_schema_version": 1})
    ).hexdigest()

    actual = target_series._source_generation_inventory_sha256(
        "provider-ledger", manifest_bytes, manifest
    )
    assert actual == expected

    changed = copy.deepcopy(manifest)
    changed["artifacts"][1]["sha256"] = "3" * 64
    assert (
        target_series._source_generation_inventory_sha256(
            "provider-ledger", canonical_json_bytes(changed), changed
        )
        != actual
    )


def test_provider_ledger_schema2_does_not_promote_provider_live_returns(
    tmp_path: Path,
) -> None:
    generation_id = "a" * 64
    catalog_path = tmp_path / "provider-ledger.json"
    account_return = {
        "cost": None,
        "date": "2026-08-12",
        "evidence_layer": "ACCOUNT_LIVE",
        "return": "0.0025",
        "return_basis": "NET",
        "return_type": "TOTAL",
        "strategy_id": "account-strategy",
        "strategy_name": "Account Strategy",
    }
    provider_return = {
        "cost": None,
        "date": "2026-08-13",
        "evidence_layer": "PROVIDER_LIVE",
        "generation_id": "b" * 64,
        "provider_id": "portfolio123",
        "return": "0.01",
        "return_basis": "OBSERVED",
        "return_type": "TOTAL",
        "strategy_id": "p123-eu",
        "strategy_name": "EU [P123]",
    }
    _write_json(
        catalog_path,
        {
            "as_of_date": "2026-08-16",
            "catalog_schema_version": 2,
            "provider_strategy_returns": [provider_return],
            "strategy_evidence": [
                {
                    "account": {
                        "observed_end": "2026-08-12",
                        "observed_start": "2026-08-12",
                        "return_observations": 1,
                    },
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": generation_id,
                    "provider_id": "synthetic",
                    "strategy_id": "account-strategy",
                    "strategy_name": "Account Strategy",
                },
                {
                    "evidence_layer": "PROVIDER_LIVE",
                    "generation_id": "b" * 64,
                    "provider": {
                        "observed_end": "2026-08-13",
                        "observed_start": "2026-08-13",
                        "return_observations": 1,
                    },
                    "provider_id": "portfolio123",
                    "strategy_id": "p123-eu",
                    "strategy_name": "EU [P123]",
                },
            ],
            "strategy_returns": [account_return],
        },
    )
    manifest_path = tmp_path / "generation.json"
    _core_generation_manifest(
        manifest_path,
        "quant-engine-provider-ledger",
        "2026-08-16",
        [_artifact(catalog_path, "normalized/provider-ledger.json")],
    )

    target_content, _evidence, proof_content, _manifest_content = (
        target_series.build_provider_ledger_targets(manifest_path, catalog_path)
    )
    rows = list(csv.DictReader(io.StringIO(target_content.decode("utf-8"))))
    assert [(row["series_id"], row["date"], row["return"]) for row in rows] == [
        ("Account Strategy", "2026-08-12", "0.0025")
    ]
    proof = json.loads(proof_content)
    assert [row["series_id"] for row in proof["series_contracts"]] == [
        "Account Strategy"
    ]


def _update_source_generation(
    contract: dict[str, Any], source_id: str, manifest: Path, as_of: str
) -> None:
    source = next(
        item for item in contract["source_segments"] if item["source_id"] == source_id
    )
    generation = json.loads(manifest.read_text(encoding="utf-8"))
    pointer_path = manifest.parent / "CURRENT.json"
    if pointer_path.exists():
        _pointer(pointer_path, generation["generation_id"], manifest)
    source["current_generation"] = {
        "as_of": as_of,
        "generation_id": generation["generation_id"],
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    for lane in contract["current_census"]["provider_lanes"]:
        if lane["source_segment_id"] != source_id:
            continue
        assert pointer_path.exists()
        lane["generation"] = {
            "as_of": as_of,
            "generation_id": generation["generation_id"],
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "pointer_sha256": hashlib.sha256(pointer_path.read_bytes()).hexdigest(),
        }


def _refresh_p123_eu_version(contract: dict[str, Any], generation_id: str) -> None:
    """Keep the exact P123 EU version bound to a rebuilt source generation."""

    version = target_series._strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": ["exact-asset", "exact-portfolio"],
            "strategy_name": "EU Multifactor Microcap",
        },
    )
    strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "p123-eu"
    )
    strategy["strategy_version"] = {"status": "EVIDENCED", "version_ref": version}
    for source in contract["source_segments"]:
        version_refs = source["strategy_version"]["version_refs"]
        if "p123-eu" in version_refs:
            version_refs["p123-eu"] = version
    for correction_id in (
        "p123-eu-backtest",
        "p123-eu-provider-live",
        "p123-eu-account-live",
    ):
        correction = next(
            row
            for row in contract["history_corrections"]["corrections"]
            if row["correction_id"] == correction_id
        )
        correction["strategy_version"]["version_ref"] = version


def _update_correction_generations(
    contract: dict[str, Any],
    source_id: str,
    manifest: Path,
    as_of: str,
    authority_root: str,
) -> None:
    generation = json.loads(manifest.read_text(encoding="utf-8"))
    generation_id = generation["generation_id"]
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    for correction in contract["history_corrections"]["corrections"]:
        if correction["source_id"] != source_id:
            continue
        correction["capture"] = {
            "as_of": as_of,
            "capture_defect": None,
            "generation_id": generation_id,
            "generation_sha256": manifest_sha256,
            "status": "CAPTURED_GOVERNED",
        }
        correction["source_receipts"] = [
            {
                "artifact_role": f"{source_id}-generation-manifest",
                "artifact_selector": f"generations/{generation_id}/generation.json",
                "authority_root": authority_root,
                "generation_id": generation_id,
                "locator_kind": "GENERATION_MANIFEST",
                "manifest_artifact_id": None,
                "sha256": manifest_sha256,
            }
        ]


def _append_p123_performance_receipt(
    contract: dict[str, Any], manifest: Path, performance_path: Path
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    correction["source_receipts"].append(
        {
            "artifact_role": "portfolio123-managed-performance",
            "artifact_selector": (
                f"generations/{generation_id}/tables/har_managed_performance.parquet"
            ),
            "authority_root": "PORTFOLIO123",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "tables/har_managed_performance.parquet",
            "sha256": hashlib.sha256(performance_path.read_bytes()).hexdigest(),
        }
    )


def _append_p123_ai_receipts(
    contract: dict[str, Any],
    manifest: Path,
    returns_path: Path,
    relationships_path: Path,
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-ai-backtest-candidate"
    )
    for artifact_role, artifact_id, path in (
        (
            "portfolio123-simulation-returns",
            "tables/returns_daily.parquet",
            returns_path,
        ),
        (
            "portfolio123-strategy-relationships",
            "tables/strategy_relationships.parquet",
            relationships_path,
        ),
    ):
        correction["source_receipts"].append(
            {
                "artifact_role": artifact_role,
                "artifact_selector": f"generations/{generation_id}/{artifact_id}",
                "authority_root": "PORTFOLIO123",
                "generation_id": generation_id,
                "locator_kind": "GENERATION_ARTIFACT",
                "manifest_artifact_id": artifact_id,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    _rows, identity = target_series._portfolio123_simulation_returns(
        returns_path,
        relationships_path,
        "live-ai",
        "sim-ai",
        "AI Factor [P123]",
    )
    correction["derived_backtest_identity"] = identity
    correction["interval"]["observed_start"] = identity["observed_start"]
    correction["interval"]["observed_end"] = identity["observed_end"]


def _update_echo_rejected_identity(
    contract: dict[str, Any],
    manifest: Path,
    catalog: Path,
    series_id: str,
) -> None:
    generation_id = json.loads(manifest.read_text(encoding="utf-8"))["generation_id"]
    target_content, _evidence, _proof, _manifest = (
        target_series.build_provider_ledger_targets(manifest, catalog)
    )
    rows = [
        row
        for row in target_series._target_rows(target_content)
        if row["series_id"] == series_id
    ]
    identity = target_series._target_row_identity(rows)
    catalog_sha256 = hashlib.sha256(catalog.read_bytes()).hexdigest()
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "echotrade-event-only-account-live-rejected"
    )
    correction["source_receipts"] = [
        receipt
        for receipt in correction["source_receipts"]
        if receipt["artifact_role"] != "provider-ledger-catalog"
    ]
    correction["source_receipts"].append(
        {
            "artifact_role": "provider-ledger-catalog",
            "artifact_selector": f"generations/{generation_id}/provider-ledger.json",
            "authority_root": "PROVIDER_LEDGER",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "normalized/provider-ledger.json",
            "sha256": catalog_sha256,
        }
    )
    correction["rejected_evidence_identity"] = {
        "canonicalization": "TARGET_FIELDS_CANONICAL_JSON_V1",
        "return_observations": identity["return_observations"],
        "sha256": identity["sha256"],
        "source_receipt_sha256": catalog_sha256,
    }


def _update_correction_interval(
    contract: dict[str, Any], correction_id: str, **values: str | None
) -> None:
    correction = next(
        item
        for item in contract["history_corrections"]["corrections"]
        if item["correction_id"] == correction_id
    )
    correction["interval"].update(values)


def _retained_echo_correction() -> dict[str, Any]:
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    return copy.deepcopy(
        next(
            row
            for row in contract["history_corrections"]["corrections"]
            if row["correction_id"] == "echotrade-event-only-account-live-rejected"
        )
    )


def _restore_retained_echo_correction(contract: dict[str, Any]) -> None:
    for index, correction in enumerate(contract["history_corrections"]["corrections"]):
        if correction["correction_id"] == "echotrade-event-only-account-live-rejected":
            contract["history_corrections"]["corrections"][index] = (
                _retained_echo_correction()
            )
            return
    raise AssertionError("synthetic contract lacks retained Echo correction")


def _synthetic_history_contract(
    path: Path, _repository_sha: str | None = None
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    product = contract.get("product")
    history = contract.get("history_corrections")
    if (
        not isinstance(product, dict)
        or product.get("product_id") != "FINANCIAL_MVP"
        or not isinstance(history, dict)
        or history.get("policy_id") != "PORT-HISTORY-TRUTH-1"
    ):
        raise TargetSeriesError("synthetic financial MVP history contract differs")

    def rows_by_id(
        rows: object, id_field: str, label: str
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(rows, list):
            raise TargetSeriesError(f"synthetic {label} rows are invalid")
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get(id_field), str):
                raise TargetSeriesError(f"synthetic {label} row is invalid")
            key = row[id_field]
            if not key or key in result:
                raise TargetSeriesError(f"synthetic {label} identity differs")
            result[key] = dict(row)
        return result

    return (
        contract,
        rows_by_id(contract.get("strategies"), "strategy_id", "strategy"),
        rows_by_id(contract.get("source_segments"), "source_id", "source"),
        rows_by_id(history.get("corrections"), "correction_id", "correction"),
    )


def _install_synthetic_history_authority(
    monkeypatch: pytest.MonkeyPatch,
    target_series_module: Any = target_series,
) -> None:
    """Install the opt-in parser used only by synthetic producer fixtures."""

    monkeypatch.setattr(
        target_series_module, "_history_contract", _synthetic_history_contract
    )


@pytest.fixture
def synthetic_history_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in parser for producer fixtures, deliberately separate from contract QC."""

    _install_synthetic_history_authority(monkeypatch)


def _history_fixture(tmp_path: Path) -> dict[str, Any]:
    as_of = "2026-01-10"
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    ids = {
        "ledger": "1" * 64,
        "matrixport": "2" * 64,
        "quantbase": "3" * 64,
        "p123": "4" * 64,
    }

    ledger_catalog = tmp_path / "ledger" / "provider-ledger.json"
    series = {
        "grizzlybears-vtma": "GrizzlyBears VTMA",
        "p123-ai-factor": "AI Factor [P123]",
        "p123-eu": "EU [P123]",
        "qsi": "QSI",
        "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
        "tt-portfolio": "TT Portfolio",
        "volatis-strategy": "Volatis Strategy",
    }
    ledger_rows: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    with localcontext() as context:
        context.prec = 28
        robuxio_account_returns = {
            "2025-12-07": format((Decimal("106") / Decimal("105") - 1), "f"),
            "2025-12-08": format((Decimal("107") / Decimal("106") - 1), "f"),
            "2025-12-11": format((Decimal("110") / Decimal("109") - 1), "f"),
        }
        volatis_account_returns = {
            f"2024-10-{day:02d}": format(
                (Decimal(day + 85) / Decimal(day + 84) - 1).normalize(), "f"
            )
            for day in range(16, 24)
        }
    for strategy_id, series_id in series.items():
        dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        if strategy_id == "p123-eu":
            dates = [
                "2025-11-21",
                "2025-11-24",
                "2025-11-25",
                "2025-11-26",
                "2025-11-27",
            ]
        if strategy_id == "robuxio-crypto":
            dates = [
                "2025-12-07",
                "2025-12-08",
                "2025-12-09",
                "2025-12-10",
                "2025-12-11",
            ]
        if strategy_id == "volatis-strategy":
            dates = [
                "2024-10-16",
                "2024-10-17",
                "2024-10-18",
                "2024-10-19",
                "2024-10-20",
                "2024-10-21",
                "2024-10-22",
                "2024-10-23",
            ]
        for index, observed in enumerate(dates, start=1):
            ledger_rows.append(
                {
                    "cost": None,
                    "date": observed,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": ids["ledger"],
                    "provider_id": "synthetic",
                    "return": (
                        robuxio_account_returns.get(observed, str(index / 1000))
                        if strategy_id == "robuxio-crypto"
                        else volatis_account_returns[observed]
                        if strategy_id == "volatis-strategy"
                        else str(index / 1000)
                    ),
                    "return_basis": "NET",
                    "return_type": "TOTAL",
                    "strategy_id": strategy_id,
                    "strategy_name": series_id,
                }
            )
        account = {
            "observed_end": dates[-1],
            "observed_start": dates[0],
            "return_observations": len(dates),
        }
        row: dict[str, object] = {
            "account": account,
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_as_of_date": as_of,
            "generation_id": ids["ledger"],
            "provider_id": "synthetic",
            "sources": {"account_returns": {"sha256": "a" * 64}},
            "strategy_id": strategy_id,
            "strategy_name": series_id,
        }
        if strategy_id == "p123-ai-factor":
            row.update(
                {
                    "provider": {
                        "entity_id": "live-ai",
                        "provider_id": "portfolio123",
                    },
                    "overlap": {"observations": 2, "status": "TESTED_EXACT_DATE_PATHS"},
                }
            )
        evidence.append(row)
    _write_json(
        ledger_catalog,
        {
            "as_of_date": as_of,
            "catalog_schema_version": 1,
            "strategy_evidence": evidence,
            "strategy_returns": ledger_rows,
        },
    )
    ledger_manifest = tmp_path / "ledger" / "generation.json"
    ledger_generation_id = _core_generation_manifest(
        ledger_manifest,
        "quant-engine-provider-ledger",
        as_of,
        [_artifact(ledger_catalog, "normalized/provider-ledger.json")],
    )
    ledger_pointer = tmp_path / "ledger" / "CURRENT.json"
    _pointer(ledger_pointer, ledger_generation_id, ledger_manifest)

    matrix_root = tmp_path / "matrixport"
    matrix_nav = matrix_root / "nav_daily.csv"
    _write_csv(
        matrix_nav,
        ["nav", "product_id", "timestamp_utc"],
        [
            {
                "nav": str(100 + index),
                "product_id": "private-product",
                "timestamp_utc": f"2025-12-{day:02d}T00:00:00Z",
            }
            for index, day in enumerate(range(1, 12))
            if day != 9
        ],
    )
    matrix_holdings = matrix_root / "holdings.csv"
    _write_csv(
        matrix_holdings,
        ["product_id", "shares"],
        [{"product_id": "private-product", "shares": "10"}],
    )
    matrix_daily_holdings = matrix_root / "daily_holdings.csv"
    _write_csv(
        matrix_daily_holdings,
        ["product_id", "utc_date", "share", "nav", "nav_available"],
        [
            {
                "product_id": "private-product",
                "utc_date": f"2025-12-{day:02d}",
                "share": "10",
                "nav": str(100 + day - 1) if day != 9 else "",
                "nav_available": "false" if day == 9 else "true",
            }
            for day in range(1, 12)
        ],
    )
    matrix_subscriptions = matrix_root / "subscription_details.csv"
    _write_csv(
        matrix_subscriptions,
        [
            "confirm_nav_time_utc",
            "product_id",
            "status",
            "subscription_share",
            "subscription_time_utc",
        ],
        [
            {
                "confirm_nav_time_utc": "2025-12-06T00:00:00Z",
                "product_id": "private-product",
                "status": "300",
                "subscription_share": "10",
                "subscription_time_utc": "2025-12-05T20:00:00Z",
            }
        ],
    )
    matrix_manifest = matrix_root / "generation.json"
    matrix_generation_id = _core_generation_manifest(
        matrix_manifest,
        "quant-engine-matrixport",
        as_of,
        [
            _artifact(matrix_nav, "csv/nav_daily.csv"),
            _artifact(matrix_holdings, "csv/holdings.csv"),
            _artifact(matrix_daily_holdings, "csv/daily_holdings.csv"),
            _artifact(matrix_subscriptions, "csv/subscription_details.csv"),
        ],
        {"declared_exclusions": _matrixport_exclusions("2025-12-09")},
    )
    matrix_pointer = matrix_root / "CURRENT.json"
    _pointer(matrix_pointer, matrix_generation_id, matrix_manifest)

    quantbase_root = tmp_path / "quantbase"
    quantbase_history = quantbase_root / "history.csv"
    _write_csv(
        quantbase_history,
        ["time", "value", "cost_basis", "profit_loss"],
        [
            {"time": "10/14/2024", "value": "0", "cost_basis": "0", "profit_loss": "0"},
            {
                "time": "10/15/2024",
                "value": "100",
                "cost_basis": "100",
                "profit_loss": "0",
            },
            {
                "time": "10/16/2024",
                "value": "101",
                "cost_basis": "100",
                "profit_loss": "1",
            },
            {
                "time": "10/17/2024",
                "value": "102",
                "cost_basis": "100",
                "profit_loss": "2",
            },
            {
                "time": "10/18/2024",
                "value": "103",
                "cost_basis": "100",
                "profit_loss": "3",
            },
            {
                "time": "10/19/2024",
                "value": "104",
                "cost_basis": "100",
                "profit_loss": "4",
            },
            {
                "time": "10/20/2024",
                "value": "105",
                "cost_basis": "100",
                "profit_loss": "5",
            },
            {
                "time": "10/21/2024",
                "value": "106",
                "cost_basis": "100",
                "profit_loss": "6",
            },
            {
                "time": "10/22/2024",
                "value": "107",
                "cost_basis": "100",
                "profit_loss": "7",
            },
            {
                "time": "10/23/2024",
                "value": "108",
                "cost_basis": "100",
                "profit_loss": "8",
            },
        ],
    )
    quantbase_activity = quantbase_root / "activity.csv"
    _write_csv(
        quantbase_activity,
        ["activity_type", "amount", "fund", "is_rebalance", "time", "type"],
        [
            {
                "activity_type": "filled_txn",
                "amount": "100",
                "fund": "private-fund",
                "is_rebalance": "false",
                "time": "2024-10-15T12:00:00Z",
                "type": "BUY",
            }
        ],
    )
    quantbase_manifest = quantbase_root / "generation.json"
    quantbase_generation_id = _core_generation_manifest(
        quantbase_manifest,
        "quant-engine-web-contract-harvester",
        as_of,
        [
            _artifact(
                quantbase_history,
                "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            ),
            _artifact(quantbase_activity, "source/csv/account_activity.csv"),
        ],
    )
    quantbase_pointer = quantbase_root / "CURRENT.json"
    _pointer(quantbase_pointer, quantbase_generation_id, quantbase_manifest)

    p123_root = tmp_path / "p123"
    p123_root.mkdir()
    metadata_path = p123_root / "metadata.parquet"
    metadata_row = {
        "assetName": "EU Multifactor Microcap",
        "assetUid": "exact-asset",
        "currency": "USD",
        "inceptionDt": "2025-11-12",
        "isAccountLive": "true",
        "name": "EU Multifactor Microcap",
        "portfolioUid": "exact-portfolio",
        "posCnt": "13",
        "strategyTotalHoldCount": "13",
    }
    pq.write_table(
        pa.Table.from_pylist([metadata_row, metadata_row, metadata_row]), metadata_path
    )
    performance_path = p123_root / "performance.parquet"
    panels = [
        {"graphs": [{"idx": 0, "s": "Strategy"}]},
        {"graphs": [{"idx": 1, "s": "Total Value"}]},
    ]
    # Real Portfolio123 markers are small day codes unrelated to the level.
    # Setting the marker to ``level - 100`` makes reading it as a percentage
    # arithmetically identical to reading the level, which is how the marker
    # survived as the emitted return series.  These markers keep the two apart.
    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [3.0, 102.0, 99.0],
        [1.0, 103.0, 100.0],
        [1.0, 104.0, 101.0],
        [3.0, 105.0, 102.0],
        [1.0, 106.0, 103.0],
    ]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "exact-asset",
                    "field": "data",
                    "value": json.dumps(data),
                },
                {"_entity_id": "exact-asset", "field": "date", "value": "2025-11-13"},
                {
                    "_entity_id": "exact-asset",
                    "field": "dateLast",
                    "value": "2025-11-21",
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "panels",
                    "value": json.dumps(panels),
                },
            ]
        ),
        performance_path,
    )
    equity_path = p123_root / "equity.parquet"
    weekdays = [
        "2025-11-13",
        "2025-11-14",
        "2025-11-17",
        "2025-11-18",
        "2025-11-19",
        "2025-11-20",
        "2025-11-21",
    ]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "Cash Added": row[2],
                    "Date": observed,
                    "Total Equity": row[1],
                    "_entity_id": "exact-asset",
                }
                for observed, row in zip(weekdays, data, strict=True)
            ]
        ),
        equity_path,
    )
    returns_path = p123_root / "returns.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_id": "sim-ai",
                    "model_return": float(100 + index),
                    "period_date": observed,
                    "source": "synthetic",
                }
                for index, observed in enumerate(
                    ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]
                )
            ]
        ),
        returns_path,
    )
    relationships_path = p123_root / "relationships.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_ref_a": "live-ai",
                    "entity_ref_b": "sim-ai",
                    "relationship_type": "live_sim_twin",
                    "notes": "synthetic",
                }
            ]
        ),
        relationships_path,
    )
    p123_manifest = p123_root / "generation.json"
    p123_generation_id = _core_generation_manifest(
        p123_manifest,
        "quant-engine-portfolio123",
        as_of,
        [
            _artifact(metadata_path, "tables/har_managed_metadata.parquet"),
            _artifact(performance_path, "tables/har_managed_performance.parquet"),
            _artifact(equity_path, "tables/har_equity_curve.parquet"),
            _artifact(returns_path, "tables/returns_daily.parquet"),
            _artifact(relationships_path, "tables/strategy_relationships.parquet"),
        ],
    )
    p123_pointer = p123_root / "CURRENT.json"
    _pointer(p123_pointer, p123_generation_id, p123_manifest)

    p123_version = target_series._strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": p123_generation_id,
            "provider_entity_ids": ["exact-asset", "exact-portfolio"],
            "strategy_name": "EU Multifactor Microcap",
        },
    )
    p123_strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "p123-eu"
    )
    p123_strategy["strategy_version"] = {
        "status": "EVIDENCED",
        "version_ref": p123_version,
    }
    for source in contract["source_segments"]:
        version_refs = source["strategy_version"]["version_refs"]
        if "p123-eu" in version_refs:
            version_refs["p123-eu"] = p123_version
    for correction_id in (
        "p123-eu-backtest",
        "p123-eu-provider-live",
        "p123-eu-account-live",
    ):
        correction = next(
            row
            for row in contract["history_corrections"]["corrections"]
            if row["correction_id"] == correction_id
        )
        correction["strategy_version"]["version_ref"] = p123_version

    _update_correction_interval(
        contract,
        "robuxio-provider-live-before-funding",
        observed_start="2025-12-01",
        observed_end="2025-12-05",
        effective_start="2025-12-01",
        effective_end="2025-12-05",
    )
    _update_correction_interval(
        contract,
        "robuxio-account-live-after-funding",
        observed_start="2025-12-07",
        observed_end="2025-12-11",
        effective_start="2025-12-06",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "p123-eu-provider-live",
        observed_start="2025-11-14",
        observed_end="2025-11-21",
        effective_start="2025-11-14",
        effective_end="2025-11-21",
    )
    p123_provider_correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    p123_provider_correction["derived_evidence_identity"] = {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 7,
        "observed_end": "2025-11-21",
        "observed_start": "2025-11-14",
        "raw_level_observations": 7,
        "return_observations": 6,
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    _update_correction_interval(
        contract,
        "p123-eu-account-live",
        observed_start="2025-11-21",
        observed_end="2025-11-27",
        effective_start="2025-11-20",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "volatis-provider-live",
        observed_start="2024-10-15",
        observed_end="2024-10-23",
        effective_start="2024-10-15",
        effective_end=None,
    )
    _update_correction_interval(
        contract,
        "volatis-account-live",
        observed_start="2024-10-16",
        observed_end="2024-10-23",
        effective_start="2024-10-15",
        effective_end=None,
    )
    for source_id, manifest, authority_root in (
        ("owner-account-live-history", ledger_manifest, "PROVIDER_LEDGER"),
        ("matrixport-account-generation", matrix_manifest, "MATRIXPORT"),
        ("quantbase-account-generation", quantbase_manifest, "QUANTBASE"),
        ("portfolio123-model-generation", p123_manifest, "PORTFOLIO123"),
    ):
        _update_source_generation(contract, source_id, manifest, as_of)
        _update_correction_generations(
            contract, source_id, manifest, as_of, authority_root
        )
    _restore_retained_echo_correction(contract)
    matrixport_source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    # This fixture supplies owner-account facts; partial-current fixtures below
    # explicitly replace them with the canonical zero-admission disposition.
    matrixport_source["admission"]["dataset_disposition_policy"][
        "admitted_item_count"
    ] = 1
    matrixport_source["declared_gap_policy"] = _matrixport_gap_policy(
        "2025-12-09",
        nav_path=matrix_nav,
        holdings_path=matrix_holdings,
        subscription_path=matrix_subscriptions,
        daily_holdings_path=matrix_daily_holdings,
        product_id="private-product",
    )
    _append_p123_performance_receipt(contract, p123_manifest, performance_path)
    _append_p123_ai_receipts(contract, p123_manifest, returns_path, relationships_path)
    contract_path = tmp_path / "contract.json"
    sources_by_id = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        generation = sources_by_id[identity["source_id"]]["current_generation"]
        identity["source_generation_id"] = generation["generation_id"]
        identity["source_generation_sha256"] = generation["sha256"]
        version_refs = sources_by_id[identity["source_id"]]["strategy_version"][
            "version_refs"
        ]
        identity["strategy_version"] = (
            version_refs.get(identity["strategy_id"]) or "UNRESOLVED"
        )
    _write_json(contract_path, contract)

    fixture = {
        "as_of_date": as_of,
        "contract_path": contract_path,
        "provider_ledger_pointer_path": ledger_pointer,
        "provider_ledger_manifest_path": ledger_manifest,
        "provider_ledger_catalog_path": ledger_catalog,
        "matrixport_pointer_path": matrix_pointer,
        "matrixport_manifest_path": matrix_manifest,
        "matrixport_nav_path": matrix_nav,
        "matrixport_holdings_path": matrix_holdings,
        "matrixport_daily_holdings_path": matrix_daily_holdings,
        "matrixport_subscription_path": matrix_subscriptions,
        "matrixport_product_id": "private-product",
        "quantbase_pointer_path": quantbase_pointer,
        "quantbase_manifest_path": quantbase_manifest,
        "quantbase_history_path": quantbase_history,
        "quantbase_activity_path": quantbase_activity,
        "quantbase_fund_id": "private-fund",
        "portfolio123_pointer_path": p123_pointer,
        "portfolio123_manifest_path": p123_manifest,
        "portfolio123_metadata_path": metadata_path,
        "portfolio123_performance_path": performance_path,
        "portfolio123_equity_path": equity_path,
        "portfolio123_returns_path": returns_path,
        "portfolio123_relationships_path": relationships_path,
    }
    _refresh_materialized_segment_identities(fixture)
    return fixture


def _build(fixture: dict[str, Any]) -> tuple[dict[str, bytes], bytes]:
    return build_layered_history_generation(
        repository_sha=target_series._trusted_layered_history_code_identity()[0],
        **fixture,
    )


def _retained_echo_current_rows() -> list[dict[str, object]]:
    """Exact target-field inputs for the retained 07c590 Echo evidence class."""

    values = (
        ("2025-10-01", "0.0"),
        ("2025-10-02", "0.00023479000000015127"),
        ("2025-10-13", "-0.0038220426226126536"),
        ("2025-10-15", "0.007918322127541844"),
        ("2025-10-20", "0.00019891431834184736"),
        ("2025-10-21", "-0.001472620952603143"),
        ("2025-10-27", "-0.0015977312089218154"),
        ("2025-10-31", "0.00016621402262773088"),
        ("2025-11-03", "0.0015724358494288992"),
        ("2025-11-10", "0.0006259213998627278"),
        ("2025-11-20", "-0.028731087907916897"),
        ("2025-11-21", "-0.024562904082013337"),
        ("2025-11-24", "-0.026112966996780673"),
    )
    return [
        {
            "cost": None,
            "date": observed,
            "evidence_layer": "ACCOUNT_LIVE",
            "provider_id": "alpaca",
            "return": value,
            "return_basis": "NET",
            "return_type": "TOTAL",
            "strategy_id": "echotrade-growth-pivots",
            "strategy_name": "Growth at Market Pivot Points [EchoTrade]",
        }
        for observed, value in values
    ]


def _add_retained_echo_to_current_ledger(
    fixture: dict[str, Any], *, mutate: bool = False
) -> None:
    """Add exact legacy Echo rows to a current synthetic ledger without rebinding it."""

    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    echo_rows = _retained_echo_current_rows()
    if mutate:
        echo_rows[0]["return"] = "0.000001"
    catalog["strategy_returns"].extend(echo_rows)
    catalog["strategy_evidence"].append(
        {
            "account": {
                "observed_end": "2025-11-24",
                "observed_start": "2025-10-01",
                "return_observations": 13,
            },
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_as_of_date": fixture["as_of_date"],
            "generation_id": "synthetic-current-echo",
            "provider_id": "alpaca",
            "sources": {"account_returns": {"sha256": "e" * 64}},
            "strategy_id": "echotrade-growth-pivots",
            "strategy_name": "Growth at Market Pivot Points [EchoTrade]",
        }
    )
    _write_json(catalog_path, catalog)
    manifest_path = fixture["provider_ledger_manifest_path"]
    generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [_artifact(catalog_path, "normalized/provider-ledger.json")],
    )
    _pointer(fixture["provider_ledger_pointer_path"], generation_id, manifest_path)
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(contract_path, contract)


def _build_immutable(fixture: dict[str, Any]) -> tuple[dict[str, bytes], bytes]:
    immutable_inputs = dict(fixture)
    for key in (
        "provider_ledger_pointer_path",
        "matrixport_pointer_path",
        "quantbase_pointer_path",
        "portfolio123_pointer_path",
    ):
        immutable_inputs.pop(key)
    return build_layered_history_generation(
        repository_sha=target_series._trusted_layered_history_code_identity()[0],
        source_selection_mode="IMMUTABLE_GENERATIONS",
        provider_ledger_pointer_path=None,
        matrixport_pointer_path=None,
        quantbase_pointer_path=None,
        portfolio123_pointer_path=None,
        **immutable_inputs,
    )


def test_layered_history_immutable_generations_builds_without_current_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    for key in (
        "provider_ledger_pointer_path",
        "matrixport_pointer_path",
        "quantbase_pointer_path",
        "portfolio123_pointer_path",
    ):
        fixture[key].unlink()

    artifacts, manifest_content = _build_immutable(fixture)

    manifest = json.loads(manifest_content)
    assert manifest["source_selection_mode"] == "IMMUTABLE_GENERATIONS"
    assert "source_pointers" not in manifest
    assert all(row["artifact_role"] != "CURRENT_POINTER" for row in manifest["inputs"])
    assert all("-current" not in row["input_id"] for row in manifest["inputs"])
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    destination = write_layered_history_generation(
        tmp_path / "immutable-generation", artifacts, manifest_content
    )
    assert (
        verify_layered_history_generation(destination, fixture["contract_path"])
        == result
    )


def test_current_admission_requires_paired_explicit_authority_inputs(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    """Selecting a current receipt cannot silently fall back to old contract pins."""

    fixture = _history_fixture(tmp_path)
    immutable_inputs = dict(fixture)
    for key in (
        "provider_ledger_pointer_path",
        "matrixport_pointer_path",
        "quantbase_pointer_path",
        "portfolio123_pointer_path",
    ):
        immutable_inputs.pop(key)

    with pytest.raises(
        TargetSeriesError,
        match="financial input contract requires provider admission receipt",
    ):
        build_layered_history_generation(
            repository_sha=target_series._trusted_layered_history_code_identity()[0],
            source_selection_mode="IMMUTABLE_GENERATIONS",
            provider_ledger_pointer_path=None,
            matrixport_pointer_path=None,
            quantbase_pointer_path=None,
            portfolio123_pointer_path=None,
            provider_admission_receipt_path=None,
            financial_input_contract_path=fixture["contract_path"],
            **immutable_inputs,
        )


def _matrixport_unadmitted_account_fixture(
    tmp_path: Path, contradiction: str | None = None, *, missing_target: bool = False
) -> dict[str, Any]:
    """Retain native product returns with no admitted owner-account USD facts."""

    fixture = _history_fixture(tmp_path)
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_bytes())
    for row in catalog["strategy_returns"]:
        if row["strategy_id"] == "robuxio-crypto":
            row["provider_id"] = "matrixport"
            row["evidence_layer"] = "PROVIDER_LIVE"
    evidence = next(
        row
        for row in catalog["strategy_evidence"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    evidence["provider_id"] = "matrixport"
    evidence["evidence_layer"] = "PROVIDER_LIVE"
    evidence["provider"] = evidence.pop("account")
    if missing_target:
        catalog["strategy_returns"] = [
            row
            for row in catalog["strategy_returns"]
            if row["strategy_id"] != "robuxio-crypto"
        ]
    for name in ("nav_observations", "flows", "capital_anchors"):
        catalog[name] = []
    catalog["admissions"] = [
        {
            "account_id": None,
            "provider_id": "matrixport",
            "strategy_id": "robuxio-crypto",
            "fact_type": "strategy_returns",
            "evidence_layer": "PROVIDER_LIVE",
            "disposition": "RECONCILIATION_ONLY",
        }
    ]
    if contradiction in {"nav_observations", "flows", "capital_anchors"}:
        catalog[contradiction].append(
            {
                "account_id": "synthetic-matrixport-account",
                "provider_id": "matrixport",
                "strategy_id": "robuxio-crypto",
                "evidence_layer": "ACCOUNT_LIVE",
                "mapping_status": "MAPPED",
            }
        )
    elif contradiction == "admission":
        catalog["admissions"].append(
            {
                "account_id": "synthetic-matrixport-account",
                "provider_id": "matrixport",
                "strategy_id": "robuxio-crypto",
                "fact_type": "nav_observations",
                "evidence_layer": "ACCOUNT_LIVE",
                "disposition": "AUTHORITATIVE",
            }
        )
    elif contradiction == "account_return":
        for row in catalog["strategy_returns"]:
            if row["strategy_id"] == "robuxio-crypto":
                row["evidence_layer"] = "ACCOUNT_LIVE"
        evidence["evidence_layer"] = "ACCOUNT_LIVE"
        evidence["account"] = evidence.pop("provider")
    _write_json(catalog_path, catalog)
    manifest_path = fixture["provider_ledger_manifest_path"]
    _core_generation_manifest(
        manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [_artifact(catalog_path, "normalized/provider-ledger.json")],
    )
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_bytes())
    _update_source_generation(
        contract, "owner-account-live-history", manifest_path, fixture["as_of_date"]
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    matrixport = sources["matrixport-account-generation"]
    disposition = matrixport["admission"]["dataset_disposition_policy"]
    disposition["admitted_item_count"] = 1 if contradiction == "policy" else 0
    disposition["exclusion_readiness_impact"] = "REQUIRED_NOT_READY"
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        if identity["source_id"] == "owner-account-live-history":
            generation = sources[identity["source_id"]]["current_generation"]
            identity["source_generation_id"] = generation["generation_id"]
            identity["source_generation_sha256"] = generation["sha256"]
    _refresh_live_implementation_identities(contract)
    _write_json(contract_path, contract)
    return fixture


@pytest.mark.parametrize(
    "contradiction",
    [
        None,
        "nav_observations",
        "flows",
        "capital_anchors",
        "admission",
        "policy",
        "account_return",
    ],
)
def test_layered_history_retains_admitted_strategies_without_matrixport_account_usd(
    tmp_path: Path,
    synthetic_history_authority: None,
    contradiction: str | None,
) -> None:
    fixture = _matrixport_unadmitted_account_fixture(tmp_path, contradiction)
    if contradiction is not None:
        with pytest.raises(TargetSeriesError, match="Matrixport.*(account|admission)"):
            _build_immutable(fixture)
        return
    artifacts, manifest_bytes = _build_immutable(fixture)
    coverage = json.loads(artifacts["coverage.json"])
    by_strategy = {row["strategy_id"]: row for row in coverage["strategies"]}
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT / "schemas/portfolio/layered-history-generation.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(json.loads(manifest_bytes))
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        assert by_strategy["robuxio-crypto"]["modes"][mode] == {
            "status": "REQUIRED_NOT_READY",
            "reason": "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT",
        }
    assert coverage["epoch_hpr_receipts"] == []
    assert (
        coverage["correction_resolutions"]["robuxio-account-live-after-funding"][
            "status"
        ]
        == "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT"
    )
    segments = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode())))
    assert {
        row["strategy_id"]
        for row in segments
        if row["evidence_layer"] == "ACCOUNT_LIVE"
    } == {
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
    }
    native = [row for row in segments if row["strategy_id"] == "robuxio-crypto"]
    assert native and {row["evidence_layer"] for row in native} == {"PROVIDER_LIVE"}
    assert {row["mode_admission"] for row in native} == {"EXCLUDE_COMPARISON_ONLY"}
    destination = write_layered_history_generation(
        tmp_path / "partial", artifacts, manifest_bytes
    )
    assert (
        verify_layered_history_generation(destination, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        with pytest.raises(
            TargetSeriesError, match="required history mode is not ready"
        ):
            verify_layered_history_generation(
                destination, fixture["contract_path"], require_mode=mode
            )


def test_layered_history_missing_matrixport_target_is_explicitly_not_ready(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_unadmitted_account_fixture(tmp_path, missing_target=True)
    artifacts, manifest_bytes = _build_immutable(fixture)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_bytes,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    coverage = json.loads(artifacts["coverage.json"])
    robuxio = next(
        row for row in coverage["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert (
        robuxio["modes"]["LIVE_ONLY"]["reason"]
        == "REQUIRED_ACCOUNT_LIVE_SEGMENT_ABSENT"
    )


def test_layered_history_reader_rejects_complete_matrixport_history_under_zero_admission(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_bytes = _build_immutable(fixture)
    contract = json.loads(fixture["contract_path"].read_bytes())
    matrixport = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    matrixport["admission"]["dataset_disposition_policy"]["admitted_item_count"] = 0
    _write_json(fixture["contract_path"], contract)
    manifest = json.loads(manifest_bytes)
    contract_bytes = fixture["contract_path"].read_bytes()
    contract_sha = hashlib.sha256(contract_bytes).hexdigest()
    contract_input = next(
        row for row in manifest["inputs"] if row["input_id"] == "financial-mvp-contract"
    )
    contract_input["sha256"] = contract_sha
    contract_input["size_bytes"] = len(contract_bytes)
    manifest["product"]["contract_sha256"] = contract_sha
    manifest.pop("generation_id")
    manifest["generation_id"] = hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    with pytest.raises(
        TargetSeriesError, match="unadmitted Matrixport account history was emitted"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=canonical_json_bytes(manifest),
            contract_path=fixture["contract_path"],
        )


@pytest.mark.parametrize(
    "tamper", ["catalog_sha", "coherent_manifest", "other_return", "hpr", "available"]
)
def test_partial_matrixport_history_reader_rejects_forged_source_or_completion(
    tmp_path: Path,
    synthetic_history_authority: None,
    tamper: str,
) -> None:
    fixture = _matrixport_unadmitted_account_fixture(tmp_path)
    artifacts, manifest_bytes = _build_immutable(fixture)
    manifest = json.loads(manifest_bytes)
    inputs = {row["input_id"]: row for row in manifest["inputs"]}
    if tamper in {"catalog_sha", "coherent_manifest"}:
        inputs["provider-ledger-catalog"]["sha256"] = "f" * 64
        if tamper == "coherent_manifest":
            source_manifest = json.loads(
                manifest["source_manifests"]["provider-ledger"]
            )
            next(
                row
                for row in source_manifest["artifacts"]
                if row["artifact_id"] == "normalized/provider-ledger.json"
            )["sha256"] = "f" * 64
            source_manifest.pop("generation_id")
            source_manifest["generation_id"] = hashlib.sha256(
                canonical_json_bytes(source_manifest)
            ).hexdigest()
            source_bytes = canonical_json_bytes(source_manifest)
            manifest["source_manifests"]["provider-ledger"] = source_bytes.decode()
            for key in ("provider-ledger-manifest", "provider-ledger-catalog"):
                inputs[key]["generation_id"] = source_manifest["generation_id"]
            inputs["provider-ledger-manifest"]["sha256"] = hashlib.sha256(
                source_bytes
            ).hexdigest()
            inputs["provider-ledger-manifest"]["size_bytes"] = len(source_bytes)
    elif tamper == "other_return":
        rows = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode())))
        next(row for row in rows if row["strategy_id"] == "qsi")["return"] = "0.012345"
        artifacts["segments.csv"] = target_series._segment_view_bytes(rows)
    else:
        coverage = json.loads(artifacts["coverage.json"])
        if tamper == "hpr":
            coverage["epoch_hpr_receipts"] = [{"strategy_id": "robuxio-crypto"}]
        else:
            row = next(
                row
                for row in coverage["strategies"]
                if row["strategy_id"] == "robuxio-crypto"
            )
            row["modes"]["LIVE_ONLY"] = {"status": "AVAILABLE", "reason": None}
        artifacts["coverage.json"] = canonical_json_bytes(coverage)
    for name, content in artifacts.items():
        manifest["outputs"][name]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest["outputs"][name]["size_bytes"] = len(content)
    manifest.pop("generation_id")
    manifest["generation_id"] = hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    with pytest.raises(TargetSeriesError):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=canonical_json_bytes(manifest),
            contract_path=fixture["contract_path"],
        )


def _p123_two_vintage_fixture(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    fixture = _history_fixture(tmp_path)
    historical_bytes = fixture["portfolio123_manifest_path"].read_bytes()
    historical = json.loads(historical_bytes)
    historical_id = historical["generation_id"]
    generations = tmp_path / "p123-vintages" / "generations"
    historical_root = generations / historical_id
    historical_root.mkdir(parents=True)
    historical_manifest = historical_root / "generation.json"
    historical_manifest.write_bytes(historical_bytes)
    tuple_inputs = (
        ("metadata", "portfolio123-managed-metadata", "har_managed_metadata.parquet"),
        (
            "performance",
            "portfolio123-managed-performance",
            "har_managed_performance.parquet",
        ),
        ("equity", "portfolio123-equity-curve", "har_equity_curve.parquet"),
    )
    receipts = [
        {
            "artifact_role": "portfolio123-generation-manifest",
            "artifact_selector": f"generations/{historical_id}/generation.json",
            "authority_root": "PORTFOLIO123",
            "generation_id": historical_id,
            "locator_kind": "GENERATION_MANIFEST",
            "manifest_artifact_id": None,
            "sha256": hashlib.sha256(historical_bytes).hexdigest(),
        }
    ]
    for key, role, filename in tuple_inputs:
        path = historical_root / "tables" / filename
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(fixture[f"portfolio123_{key}_path"].read_bytes())
        fixture[f"portfolio123_{key}_path"] = path
        receipts.append(
            {
                "artifact_role": role,
                "artifact_selector": f"generations/{historical_id}/tables/{filename}",
                "authority_root": "PORTFOLIO123",
                "generation_id": historical_id,
                "locator_kind": "GENERATION_ARTIFACT",
                "manifest_artifact_id": f"tables/{filename}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    current_manifest = tmp_path / "current-p123-generation.json"
    current_id = _core_generation_manifest(
        current_manifest,
        "quant-engine-portfolio123",
        historical["as_of_date"],
        [
            item
            for item in historical["artifacts"]
            if item["artifact_id"]
            in {"tables/returns_daily.parquet", "tables/strategy_relationships.parquet"}
        ],
    )
    current_root = generations / current_id
    current_root.mkdir()
    (current_root / "generation.json").write_bytes(current_manifest.read_bytes())
    fixture["portfolio123_manifest_path"] = current_root / "generation.json"
    contract = json.loads(fixture["contract_path"].read_bytes())
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "portfolio123-model-generation"
    )
    source["current_generation"] = {
        "as_of": historical["as_of_date"],
        "generation_id": current_id,
        "sha256": hashlib.sha256(current_manifest.read_bytes()).hexdigest(),
    }
    for segment in contract["history_corrections"]["materialized_segment_identities"]:
        if segment["source_id"] == "portfolio123-model-generation":
            segment["source_generation_id"] = current_id
            segment["source_generation_sha256"] = hashlib.sha256(
                current_manifest.read_bytes()
            ).hexdigest()
    correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    correction["source_receipts"] = receipts
    _write_json(fixture["contract_path"], contract)
    return fixture, historical_manifest


@pytest.mark.parametrize(
    "tamper", [None, "manifest", "metadata", "receipt", "readback"]
)
def test_layered_history_p123_two_vintages_remain_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
    tamper: str | None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture, historical_manifest = _p123_two_vintage_fixture(tmp_path)
    if tamper == "manifest":
        historical_manifest.write_bytes(historical_manifest.read_bytes() + b" ")
    elif tamper == "metadata":
        fixture["portfolio123_metadata_path"] = fixture["portfolio123_returns_path"]
    elif tamper == "receipt":
        contract = json.loads(fixture["contract_path"].read_bytes())
        correction = next(
            row
            for row in contract["history_corrections"]["corrections"]
            if row["correction_id"] == "p123-eu-provider-live"
        )
        correction["source_receipts"][1]["sha256"] = "f" * 64
        _write_json(fixture["contract_path"], contract)
    if tamper in {"manifest", "metadata", "receipt"}:
        with pytest.raises(TargetSeriesError, match="Portfolio123.*history"):
            _build_immutable(fixture)
        return
    artifacts, manifest_bytes = _build_immutable(fixture)
    manifest = json.loads(manifest_bytes)
    inputs = {row["input_id"]: row for row in manifest["inputs"]}
    current_id = json.loads(fixture["portfolio123_manifest_path"].read_bytes())[
        "generation_id"
    ]
    historical_id = json.loads(historical_manifest.read_bytes())["generation_id"]
    assert current_id != historical_id
    assert inputs["portfolio123-returns"]["generation_id"] == current_id
    assert inputs["portfolio123-relationships"]["generation_id"] == current_id
    for key in ("metadata", "performance", "equity"):
        assert inputs[f"portfolio123-{key}"]["generation_id"] == historical_id
    assert (
        manifest["source_manifests"]["portfolio123-provider-history"]
        == historical_manifest.read_text()
    )
    from jsonschema import Draft202012Validator, ValidationError

    schema = json.loads(
        (ROOT / "schemas/portfolio/layered-history-generation.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(manifest)
    if tamper is None:
        pointers = {
            key: fixture[f"{argument}_pointer_path"].read_text()
            for key, argument in (
                ("provider-ledger", "provider_ledger"),
                ("matrixport", "matrixport"),
                ("quantbase", "quantbase"),
                ("portfolio123", "portfolio123"),
            )
        }
        immutable_with_pointers = {**manifest, "source_pointers": pointers}
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(immutable_with_pointers)
        for mode in ("CURRENT_POINTERS", None):
            pointer_manifest = {**manifest, "source_pointers": pointers}
            if mode is None:
                pointer_manifest.pop("source_selection_mode")
            else:
                pointer_manifest["source_selection_mode"] = mode
            Draft202012Validator(schema).validate(pointer_manifest)
            pointer_manifest.pop("source_pointers")
            with pytest.raises(ValidationError):
                Draft202012Validator(schema).validate(pointer_manifest)
    if tamper == "readback":
        inputs["portfolio123-metadata"]["generation_id"] = current_id
        manifest.pop("generation_id")
        manifest["generation_id"] = hashlib.sha256(
            canonical_json_bytes(manifest)
        ).hexdigest()
        with pytest.raises(TargetSeriesError, match="history.*(receipt|authority)"):
            validate_layered_history_generation(
                artifacts=artifacts,
                manifest_content=canonical_json_bytes(manifest),
                contract_path=fixture["contract_path"],
            )
        return
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_bytes,
        contract_path=fixture["contract_path"],
    )
    destination = write_layered_history_generation(
        tmp_path / "two-vintage-history", artifacts, manifest_bytes
    )
    assert (
        verify_layered_history_generation(destination, fixture["contract_path"])
        == result
    )


def test_layered_history_immutable_generations_ignore_stale_current_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    stale_pointer = fixture["matrixport_pointer_path"]
    stale_content = canonical_json_bytes(
        {
            "artifact_inventory_sha256": "0" * 64,
            "generation_id": "f" * 64,
            "manifest_sha256": "e" * 64,
            "pointer_schema_version": 2,
            "state": "PUBLISHED",
        }
    )
    stale_pointer.write_bytes(stale_content)

    _artifacts, manifest_content = _build_immutable(fixture)

    assert stale_pointer.read_bytes() == stale_content
    assert "source_pointers" not in json.loads(manifest_content)


def test_layered_history_immutable_generations_reject_pointer_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    immutable_inputs = dict(fixture)
    immutable_inputs["source_selection_mode"] = "IMMUTABLE_GENERATIONS"

    with pytest.raises(TargetSeriesError, match="immutable generations do not accept"):
        build_layered_history_generation(
            repository_sha=target_series._trusted_layered_history_code_identity()[0],
            **immutable_inputs,
        )


def test_layered_history_immutable_selector_rejects_pointer_before_reading_data(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "must-not-be-read"
    with pytest.raises(TargetSeriesError, match="immutable generations do not accept"):
        build_layered_history_generation(
            contract_path=unreadable,
            repository_sha="a" * 40,
            as_of_date="2026-01-10",
            source_selection_mode="IMMUTABLE_GENERATIONS",
            provider_ledger_pointer_path=unreadable,
            provider_ledger_manifest_path=unreadable,
            provider_ledger_catalog_path=unreadable,
            matrixport_pointer_path=None,
            matrixport_manifest_path=unreadable,
            matrixport_nav_path=unreadable,
            matrixport_holdings_path=unreadable,
            matrixport_daily_holdings_path=unreadable,
            matrixport_subscription_path=unreadable,
            matrixport_product_id="ordinary-product-selector",
            quantbase_pointer_path=None,
            quantbase_manifest_path=unreadable,
            quantbase_history_path=unreadable,
            quantbase_activity_path=unreadable,
            quantbase_fund_id="ordinary-fund-selector",
            portfolio123_pointer_path=None,
            portfolio123_manifest_path=unreadable,
            portfolio123_metadata_path=unreadable,
            portfolio123_performance_path=unreadable,
            portfolio123_equity_path=unreadable,
            portfolio123_returns_path=unreadable,
            portfolio123_relationships_path=unreadable,
        )


def test_layered_history_immutable_generations_reject_tampered_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    for mutation in (
        "generation_id",
        "manifest_sha256",
        "artifact_sha256",
        "artifact_size_bytes",
        "undeclared_calculation_path",
    ):
        fixture = _history_fixture(tmp_path / mutation)
        if mutation in {"generation_id", "manifest_sha256"}:
            contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
            source = next(
                row
                for row in contract["source_segments"]
                if row["source_id"] == "matrixport-account-generation"
            )
            source["current_generation"][
                "generation_id" if mutation == "generation_id" else "sha256"
            ] = "0" * 64
            _write_json(fixture["contract_path"], contract)
        elif mutation == "artifact_sha256":
            fixture["matrixport_nav_path"].write_text(
                "not the manifest-declared artifact", encoding="utf-8"
            )
        elif mutation == "artifact_size_bytes":
            fixture["matrixport_nav_path"].write_bytes(
                fixture["matrixport_nav_path"].read_bytes() + b"\n"
            )
        else:
            undeclared = tmp_path / mutation / "undeclared-nav.csv"
            undeclared.write_text("undeclared", encoding="utf-8")
            fixture["matrixport_nav_path"] = undeclared

        with pytest.raises(TargetSeriesError):
            _build_immutable(fixture)


def test_layered_history_cli_forwards_immutable_mode_without_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    output_root = tmp_path / "immutable-cli"
    path_flags = {
        "--financial-mvp-contract": "contract_path",
        "--provider-ledger-manifest": "provider_ledger_manifest_path",
        "--provider-ledger-catalog": "provider_ledger_catalog_path",
        "--manifest": "matrixport_manifest_path",
        "--nav": "matrixport_nav_path",
        "--holdings": "matrixport_holdings_path",
        "--daily-holdings": "matrixport_daily_holdings_path",
        "--subscriptions": "matrixport_subscription_path",
        "--quantbase-manifest": "quantbase_manifest_path",
        "--quantbase-history": "quantbase_history_path",
        "--quantbase-activity": "quantbase_activity_path",
        "--portfolio123-manifest": "portfolio123_manifest_path",
        "--portfolio123-metadata": "portfolio123_metadata_path",
        "--portfolio123-performance": "portfolio123_performance_path",
        "--portfolio123-equity": "portfolio123_equity_path",
        "--portfolio123-returns": "portfolio123_returns_path",
        "--portfolio123-relationships": "portfolio123_relationships_path",
    }
    argv = [
        "portfolio_target_series.py",
        "--layered-history-generation",
        "--immutable-generations",
        "--repository-sha",
        target_series._trusted_layered_history_code_identity()[0],
        "--as-of-date",
        fixture["as_of_date"],
        "--product-id",
        fixture["matrixport_product_id"],
        "--quantbase-fund-id",
        fixture["quantbase_fund_id"],
        "--output-root",
        str(output_root),
    ]
    for flag, key in path_flags.items():
        argv.extend((flag, str(fixture[key])))
    monkeypatch.setattr(sys, "argv", argv)

    assert target_series.main() == 0
    result = json.loads(capsys.readouterr().out)
    manifest = json.loads(
        (output_root / result["generation_id"] / "generation.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["source_selection_mode"] == "IMMUTABLE_GENERATIONS"
    assert "source_pointers" not in manifest


def test_layered_history_validator_preserves_legacy_pointer_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    legacy_manifest = json.loads(manifest_content)
    legacy_manifest.pop("source_selection_mode")
    legacy_content = _reidentify_manifest(legacy_manifest)

    assert (
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=legacy_content,
            contract_path=fixture["contract_path"],
        )["verification"]
        == "VALID"
    )

    legacy_manifest.pop("source_pointers")
    with pytest.raises(TargetSeriesError, match="generation keys differ"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(legacy_manifest),
            contract_path=fixture["contract_path"],
        )


def test_immutable_layered_history_never_reads_current_or_sibling_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    trapped = {
        fixture[key]
        for key in (
            "provider_ledger_pointer_path",
            "matrixport_pointer_path",
            "quantbase_pointer_path",
            "portfolio123_pointer_path",
        )
    }
    for manifest_key in (
        "provider_ledger_manifest_path",
        "matrixport_manifest_path",
        "quantbase_manifest_path",
        "portfolio123_manifest_path",
    ):
        manifest = fixture[manifest_key]
        sibling = manifest.parent / "generations" / ("f" * 64) / "generation.json"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_bytes(manifest.read_bytes())
        trapped.add(sibling)
    original_read_bytes = Path.read_bytes

    def trapped_read_bytes(path: Path) -> bytes:
        if path in trapped:
            raise AssertionError(f"immutable selection read fallback: {path.name}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", trapped_read_bytes)
    _build_immutable(fixture)


def test_immutable_layered_history_rejects_each_provider_artifact_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    for provider, path_key in (
        ("provider-ledger", "provider_ledger_catalog_path"),
        ("matrixport", "matrixport_nav_path"),
        ("quantbase", "quantbase_history_path"),
        ("portfolio123", "portfolio123_metadata_path"),
    ):
        fixture = _history_fixture(tmp_path / provider)
        path = fixture[path_key]
        path.write_bytes(path.read_bytes() + b"x")
        with pytest.raises(TargetSeriesError):
            _build_immutable(fixture)


def test_immutable_artifact_checks_isolate_declared_size_and_declaration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    manifest_path = fixture["matrixport_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = target_series._manifest_artifacts(manifest["artifacts"])
    nav = fixture["matrixport_nav_path"]
    declared = artifacts["csv/nav_daily.csv"]
    size_only = target_series.VerifiedArtifact(
        declared.artifact_id, declared.sha256, declared.size_bytes + 1
    )
    with pytest.raises(TargetSeriesError, match="differs from the manifest"):
        target_series._verify_artifact(
            nav, {**artifacts, declared.artifact_id: size_only}, declared.artifact_id
        )
    copied = tmp_path / "undeclared.csv"
    copied.write_bytes(nav.read_bytes())
    with pytest.raises(TargetSeriesError, match="manifest does not declare"):
        target_series._verify_artifact(copied, artifacts, "csv/not-declared.csv")


def test_immutable_receipt_tampering_rejects_validate_and_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    monkeypatch.setattr(
        target_series, "_require_trusted_layered_history_dependencies", lambda: None
    )
    fixture = _history_fixture(tmp_path)
    artifacts, content = _build_immutable(fixture)
    base = json.loads(content)
    mutations = {
        "source-pointers": lambda value: value.update({"source_pointers": {}}),
        "current-input": lambda value: value["inputs"].append(
            {
                "artifact_id": None,
                "artifact_role": "CURRENT_POINTER",
                "as_of_date": fixture["as_of_date"],
                "generation_id": "1" * 64,
                "input_id": "matrixport-current",
                "sha256": "0" * 64,
                "size_bytes": 1,
            }
        ),
        "missing-manifest": lambda value: value.update(
            {
                "inputs": [
                    row
                    for row in value["inputs"]
                    if row["input_id"] != "matrixport-manifest"
                ]
            }
        ),
        "missing-artifact": lambda value: value.update(
            {
                "inputs": [
                    row
                    for row in value["inputs"]
                    if row["input_id"] != "matrixport-nav"
                ]
            }
        ),
    }
    for name, mutate in mutations.items():
        tampered = copy.deepcopy(base)
        mutate(tampered)
        tampered["inputs"] = sorted(tampered["inputs"], key=lambda row: row["input_id"])
        tampered_content = _reidentify_manifest(tampered)
        with pytest.raises(TargetSeriesError):
            validate_layered_history_generation(
                artifacts=artifacts,
                manifest_content=tampered_content,
                contract_path=fixture["contract_path"],
            )
        destination = write_layered_history_generation(
            tmp_path / name, artifacts, tampered_content
        )
        with pytest.raises(TargetSeriesError):
            verify_layered_history_generation(destination, fixture["contract_path"])


def _refresh_live_implementation_identities(contract: dict[str, Any]) -> None:
    account_segments = {
        row["strategy_id"]: row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["evidence_layer"] == "ACCOUNT_LIVE"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    }
    strategies = {row["strategy_id"]: row for row in contract["strategies"]}
    for strategy_id, segment in account_segments.items():
        strategies[strategy_id]["live_implementation_identity"] = {
            "status": "EVIDENCED",
            "version_ref": f"account-live-segment:{segment['sha256']}",
            "generation_id": segment["source_generation_id"],
            "receipt_sha256": segment["source_generation_sha256"],
        }
    echo = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "echotrade-event-only-account-live-rejected"
    )
    rejected = echo["rejected_evidence_identity"]
    strategies["echotrade-growth-pivots"]["live_implementation_identity"] = {
        "status": "REJECTED",
        "version_ref": f"rejected-event-only:{rejected['sha256']}",
        "generation_id": echo["capture"]["generation_id"],
        "receipt_sha256": rejected["source_receipt_sha256"],
    }


def _refresh_materialized_segment_identities(fixture: dict[str, Any]) -> None:
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in contract["history_corrections"]["materialized_segment_identities"]:
        generation = sources[identity["source_id"]]["current_generation"]
        identity["source_generation_id"] = generation["generation_id"]
        identity["source_generation_sha256"] = generation["sha256"]
        version_refs = sources[identity["source_id"]]["strategy_version"][
            "version_refs"
        ]
        identity["strategy_version"] = (
            version_refs.get(identity["strategy_id"]) or "UNRESOLVED"
        )
    _refresh_live_implementation_identities(contract)
    _write_json(contract_path, contract)
    artifacts, _manifest_content = _build(fixture)
    rows = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8"))))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    contract["history_corrections"]["materialized_segment_identities"] = (
        target_series._materialized_segment_identities(rows, sources)
    )
    _refresh_live_implementation_identities(contract)
    _write_json(contract_path, contract)


def _matrixport_exclusions(*dates: str) -> list[dict[str, str]]:
    return [
        {
            "item": f"{kind}:{observed}",
            "provenance": "owner-accepted 2026-08-14, issue #287",
            "reason": "provider NAV endpoint returned no data for this date",
        }
        for kind in (
            "fx_missing_date",
            "nav_missing_date",
            "reconciliation_incomplete_date",
        )
        for observed in dates
    ]


def _matrixport_gap_policy(
    *dates: str,
    nav_path: Path,
    holdings_path: Path,
    subscription_path: Path,
    daily_holdings_path: Path,
    product_id: str,
) -> dict[str, object]:
    missing = frozenset(target_series.date.fromisoformat(value) for value in dates)
    omitted = target_series._matrixport_expected_gap_endpoints(missing)
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    policy = dict(source["declared_gap_policy"])
    _returns, coverage = target_series._matrixport_returns(
        nav_path,
        holdings_path,
        subscription_path,
        daily_holdings_path,
        product_id,
        "Robuxio Crypto (High Sharpe High Vol)",
        missing,
    )
    policy.update(coverage["source_gap_policy"])
    policy.pop("authority_bound", None)
    assert policy["declared_missing_source_dates"] == [
        item.isoformat() for item in sorted(missing)
    ]
    assert policy["omitted_return_dates"] == [
        item.isoformat() for item in sorted(omitted)
    ]
    return policy


def _inject_matrixport_gap(fixture: dict[str, Any], missing_date: str) -> None:
    nav_path = fixture["matrixport_nav_path"]
    nav_rows = list(csv.DictReader(io.StringIO(nav_path.read_text(encoding="utf-8"))))
    retained = [
        row
        for row in nav_rows
        if not row["timestamp_utc"].startswith(f"{missing_date}T")
    ]
    _write_csv(nav_path, list(nav_rows[0]), retained)

    manifest_path = fixture["matrixport_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "csv/nav_daily.csv"
    )
    artifact.update(_artifact(nav_path, "csv/nav_daily.csv"))
    manifest["coverage"]["declared_exclusions"] = _matrixport_exclusions(missing_date)
    matrixport_generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-matrixport",
        fixture["as_of_date"],
        manifest["artifacts"],
        {"declared_exclusions": manifest["coverage"]["declared_exclusions"]},
    )
    _pointer(
        fixture["matrixport_pointer_path"], matrixport_generation_id, manifest_path
    )

    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    source["declared_gap_policy"] = _matrixport_gap_policy(
        missing_date,
        nav_path=nav_path,
        holdings_path=fixture["matrixport_holdings_path"],
        subscription_path=fixture["matrixport_subscription_path"],
        daily_holdings_path=fixture["matrixport_daily_holdings_path"],
        product_id=fixture["matrixport_product_id"],
    )
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)


def _reidentify_manifest(manifest: dict[str, Any]) -> bytes:
    without_id = dict(manifest)
    without_id.pop("generation_id", None)
    manifest["generation_id"] = hashlib.sha256(
        canonical_json_bytes(without_id)
    ).hexdigest()
    return canonical_json_bytes(manifest)


def _csv_bytes(fields: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _replace_output(
    artifacts: dict[str, bytes], manifest: dict[str, Any], name: str, content: bytes
) -> None:
    artifacts[name] = content
    manifest["outputs"][name]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["outputs"][name]["size_bytes"] = len(content)
    if name.endswith(".csv"):
        manifest["outputs"][name]["row_count"] = max(
            content.decode("utf-8").count("\n") - 1, 0
        )


def _matrixport_fx_fixture(
    tmp_path: Path, *, convert_ledger: bool = True
) -> dict[str, Any]:
    fixture = _history_fixture(tmp_path)
    fx_root = tmp_path / "fx"
    rates_path = fx_root / "rates.csv"
    rates = {
        date(2025, 12, day): Decimal("0.98") + Decimal(day) / Decimal("1000")
        for day in range(1, 12)
    }
    _write_csv(
        rates_path,
        [
            "date",
            "currency",
            "base_currency",
            "rate",
            "provider",
            "ticker",
            "field",
            "timing",
        ],
        [
            {
                "date": day.isoformat(),
                "currency": currency,
                "base_currency": "USD",
                "rate": str(rate if currency == "USDT" else Decimal("1.0001")),
                "provider": "yahoo",
                "ticker": f"{currency}-USD",
                "field": "Close",
                "timing": "UTC_DAILY_CLOSE",
            }
            for currency in ("USDC", "USDT")
            for day, rate in rates.items()
        ],
    )
    rates_bytes = rates_path.read_bytes()
    fx_manifest = GenerationManifest.build(
        as_of_date=date(2025, 12, 11),
        adapter=AdapterIdentity("agent-market-data-terminal-yfinance", "1"),
        coverage=CoverageClaim.from_dict(
            {
                "boundary": "synthetic USD daily close proof",
                "fact_types": ["FX_RATE"],
                "known_gaps": [],
                "observed_start": "2025-12-01",
                "observed_end": "2025-12-11",
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            }
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="rates.csv",
                sha256=hashlib.sha256(rates_bytes).hexdigest(),
                size_bytes=len(rates_bytes),
                as_of_date=date(2025, 12, 11),
                schema_name="portfolio-fx-daily-close-rates",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    fx_manifest_path = fx_root / "generation.json"
    fx_manifest_path.write_bytes(fx_manifest.to_json_bytes())
    fixture["portfolio_fx_rates_manifest_path"] = fx_manifest_path

    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_bytes())
    if convert_ledger:
        with localcontext() as context:
            context.prec = 50
            for row in catalog["strategy_returns"]:
                if row["strategy_id"] != "robuxio-crypto" or row["date"] not in {
                    "2025-12-07",
                    "2025-12-08",
                    "2025-12-11",
                }:
                    continue
                day = date.fromisoformat(row["date"])
                previous = day - timedelta(days=1)
                prior_usd_nav = Decimal(100 + previous.day - 1) * rates[previous]
                usd_nav = Decimal(100 + day.day - 1) * rates[day]
                row["return"] = format(usd_nav / prior_usd_nav - Decimal(1), "f")
    _write_json(catalog_path, catalog)
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    fx_identity = _artifact(fx_manifest_path, "portfolio/fx-rates/generation.json")
    fx_identity["schema"] = {"name": "generation-manifest", "version": "1"}
    fx_identity["as_of_date"] = "2025-12-11"
    ledger_generation_id = _core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [_artifact(catalog_path, "normalized/provider-ledger.json"), fx_identity],
    )
    _pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation_id,
        ledger_manifest_path,
    )
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_bytes())
    _update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)
    return fixture


def _matrixport_truncated_projection_fixture(tmp_path: Path) -> dict[str, Any]:
    """Retain complete USD account facts under an older return projection."""

    fixture = _matrixport_fx_fixture(tmp_path)
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_bytes())
    catalog["nav_observations"] = [
        {
            "provider_id": "matrixport",
            "account_id": "synthetic-matrixport-account",
            "strategy_id": "robuxio-crypto",
            "mapping_status": "MAPPED",
            "as_of_date": f"2025-12-{day:02d}",
            "record_id": f"synthetic-nav-{day}",
            "nav": str(
                Decimal(100 + day - 1)
                * (Decimal("0.98") + Decimal(day) / Decimal("1000"))
            ),
        }
        for day in (6, 7, 8, 10, 11)
    ]
    catalog["flows"] = []
    catalog["capital_anchors"] = []
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] != "robuxio-crypto" or row["date"] <= "2025-12-08"
    ]
    account_evidence = next(
        row["account"]
        for row in catalog["strategy_evidence"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    account_evidence.update(observed_end="2025-12-08", return_observations=2)
    _write_json(catalog_path, catalog)
    return fixture


def _rebind_matrixport_catalog(fixture: dict[str, Any]) -> None:
    catalog_path = fixture["provider_ledger_catalog_path"]
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    prior_manifest = json.loads(ledger_manifest_path.read_bytes())
    retained_artifacts = [
        row
        for row in prior_manifest["artifacts"]
        if row["artifact_id"] != "normalized/provider-ledger.json"
    ]
    generation_id = _core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [
            _artifact(catalog_path, "normalized/provider-ledger.json"),
            *retained_artifacts,
        ],
    )
    _pointer(
        fixture["provider_ledger_pointer_path"], generation_id, ledger_manifest_path
    )
    contract = json.loads(fixture["contract_path"].read_bytes())
    _update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(fixture["contract_path"], contract)


def test_matrixport_expands_stale_projection_from_retained_account_facts(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_truncated_projection_fixture(tmp_path)
    catalog = json.loads(fixture["provider_ledger_catalog_path"].read_bytes())
    retained = {
        row["date"]: row["return"]
        for row in catalog["strategy_returns"]
        if row["strategy_id"] == "robuxio-crypto"
    }
    _rebind_matrixport_catalog(fixture)
    _refresh_materialized_segment_identities(fixture)
    artifacts, manifest = _build(fixture)
    selected = {
        row["date"]: row["return"]
        for row in csv.DictReader(io.StringIO(artifacts["segments.csv"].decode()))
        if row["strategy_id"] == "robuxio-crypto"
        and row["evidence_layer"] == "ACCOUNT_LIVE"
    }
    assert set(selected) == {"2025-12-07", "2025-12-08", "2025-12-11"}
    assert {day: selected[day] for day in retained} == retained
    with localcontext() as context:
        context.prec = 28
        expected = Decimal("109.010") / Decimal("107.910") - 1
    assert Decimal(selected["2025-12-11"]) == expected
    validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest,
        contract_path=fixture["contract_path"],
    )


@pytest.mark.parametrize(
    "mutation", ["missing_nav", "missing_flows", "changed_overlap"]
)
def test_matrixport_projection_expansion_rejects_unproven_account_facts(
    tmp_path: Path, synthetic_history_authority: None, mutation: str
) -> None:
    fixture = _matrixport_truncated_projection_fixture(tmp_path)
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_bytes())
    if mutation == "missing_nav":
        catalog["nav_observations"] = catalog["nav_observations"][:-1]
    elif mutation == "missing_flows":
        del catalog["flows"]
    else:
        next(
            row
            for row in catalog["strategy_returns"]
            if row["strategy_id"] == "robuxio-crypto"
        )["return"] = "0.25"
    _write_json(catalog_path, catalog)
    _rebind_matrixport_catalog(fixture)
    with pytest.raises(TargetSeriesError, match="Matrixport.*(missing|differs)"):
        _build(fixture)


def test_matrixport_usd_history_replays_daily_and_gap_conversion_offline(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_fx_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    schema = json.loads(
        (ROOT / "schemas/portfolio/layered-history-generation.schema.json").read_bytes()
    )
    Draft202012Validator(schema).validate(json.loads(manifest_content))
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]
    proof = resolution["currency_conversion"]
    native_policy = resolution["source_gap_policy"]
    source = next(
        row
        for row in json.loads(fixture["contract_path"].read_bytes())["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    assert native_policy == source["declared_gap_policy"]
    assert (
        proof["native_nav_csv"].encode("utf-8")
        == fixture["matrixport_nav_path"].read_bytes()
    )
    assert (
        proof["rates_csv"].encode("utf-8")
        == (
            fixture["portfolio_fx_rates_manifest_path"].parent / "rates.csv"
        ).read_bytes()
    )
    receipt = coverage["epoch_hpr_receipts"][0]["receipt"]
    assert "currency_conversion" not in receipt
    assert receipt["daily_return_observations"] == 3
    assert receipt["epoch_holding_period_link_observations"] == 4
    native_span = native_policy["span_return_receipts"][0]
    usd_span = receipt["span_return_receipts"][0]
    assert {key: value for key, value in usd_span.items() if key != "return"} == {
        key: value for key, value in native_span.items() if key != "return"
    }
    with localcontext() as context:
        context.prec = 50
        expected_span = (Decimal(1) + Decimal(native_span["return"])) * Decimal(
            "0.990"
        ) / Decimal("0.988") - Decimal(1)
    assert Decimal(usd_span["return"]) == expected_span
    assert usd_span["return"] != native_span["return"]
    rows = list(csv.DictReader(io.StringIO(artifacts["live-only.csv"].decode("utf-8"))))
    assert len({row["series_id"] for row in rows}) == 7
    robuxio = [row for row in rows if "Robuxio" in row["series_id"]]
    assert [row["date"] for row in robuxio] == [
        "2025-12-07",
        "2025-12-08",
        "2025-12-11",
    ]
    with localcontext() as context:
        context.prec = 28
        native_first = Decimal(106) / Decimal(105) - Decimal(1)
    assert Decimal(robuxio[0]["return"]) != native_first

    generation = write_layered_history_generation(
        tmp_path / "verified-fx", artifacts, manifest_content
    )
    fixture["matrixport_nav_path"].unlink()
    (fixture["portfolio_fx_rates_manifest_path"].parent / "rates.csv").unlink()
    fixture["portfolio_fx_rates_manifest_path"].unlink()
    verified = verify_layered_history_generation(generation, fixture["contract_path"])
    assert verified["verification"] == "VALID"


def _matrixport_fx_boundary_fixture(tmp_path: Path) -> dict[str, Any]:
    """Admitted FX generation ends before the native Matrixport series."""

    fixture = _matrixport_fx_fixture(tmp_path)
    # The provider ledger's own USD series already ends at the admitted FX
    # boundary (T4-b), so its retained Robuxio rows stop there too.
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_bytes())
    catalog["strategy_returns"] = [
        row
        for row in catalog["strategy_returns"]
        if row["strategy_id"] != "robuxio-crypto" or row["date"] <= "2025-12-08"
    ]
    account_evidence = next(
        row["account"]
        for row in catalog["strategy_evidence"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    account_evidence.update(observed_end="2025-12-08", return_observations=2)
    _write_json(catalog_path, catalog)
    fx_manifest_path = fixture["portfolio_fx_rates_manifest_path"]
    rates_path = fx_manifest_path.parent / "rates.csv"
    rows = [
        row
        for row in csv.DictReader(io.StringIO(rates_path.read_text(encoding="utf-8")))
        if row["date"] <= "2025-12-08"
    ]
    _write_csv(rates_path, list(rows[0]), rows)
    rates_bytes = rates_path.read_bytes()
    fx_manifest = GenerationManifest.build(
        as_of_date=date(2025, 12, 8),
        adapter=AdapterIdentity("agent-market-data-terminal-yfinance", "1"),
        coverage=CoverageClaim.from_dict(
            {
                "boundary": "synthetic USD daily close proof",
                "fact_types": ["FX_RATE"],
                "known_gaps": [],
                "observed_start": "2025-12-01",
                "observed_end": "2025-12-08",
                "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            }
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="rates.csv",
                sha256=hashlib.sha256(rates_bytes).hexdigest(),
                size_bytes=len(rates_bytes),
                as_of_date=date(2025, 12, 8),
                schema_name="portfolio-fx-daily-close-rates",
                schema_version="1",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    fx_manifest_path.write_bytes(fx_manifest.to_json_bytes())
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    fx_identity = _artifact(fx_manifest_path, "portfolio/fx-rates/generation.json")
    fx_identity["schema"] = {"name": "generation-manifest", "version": "1"}
    fx_identity["as_of_date"] = "2025-12-08"
    ledger_generation_id = _core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [
            _artifact(
                fixture["provider_ledger_catalog_path"],
                "normalized/provider-ledger.json",
            ),
            fx_identity,
        ],
    )
    _pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation_id,
        ledger_manifest_path,
    )
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_bytes())
    _update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)
    return fixture


def _synthetic_current_admission(fixture: dict[str, Any]) -> dict[str, object]:
    admitted = {}
    for source_id, key in (
        ("owner-account-live-history", "provider_ledger_manifest_path"),
        ("matrixport-account-generation", "matrixport_manifest_path"),
        ("quantbase-account-generation", "quantbase_manifest_path"),
        ("portfolio123-model-generation", "portfolio123_manifest_path"),
    ):
        content = fixture[key].read_bytes()
        manifest = GenerationManifest.from_json_bytes(content)
        admitted[source_id] = {
            "as_of": manifest.as_of_date.isoformat(),
            "generation_id": manifest.generation_id,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return {
        "proof": {
            "authority": "synthetic-current-admission",
            "financial_input_contract_raw_sha256": hashlib.sha256(
                fixture["contract_path"].read_bytes()
            ).hexdigest(),
        },
        "source_generations": admitted,
    }


def test_robuxio_correction_resolution_records_the_fx_bounded_emitted_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    """The coverage record binds the emitted segment, not the native series.

    Under a current admission the native Matrixport series runs to the
    admitted cutoff while the USD conversion ends at the admitted FX
    generation's last rate.  The correction-resolution record must carry the
    emitted FX-bounded bounds the verifier recomputes from the artifacts.
    The admission authority itself is stubbed; its own receipt binding has
    dedicated tests.
    """

    fixture = _matrixport_fx_boundary_fixture(tmp_path)
    admission = _synthetic_current_admission(fixture)
    monkeypatch.setattr(
        target_series,
        "_current_source_admission_authority",
        lambda **kwargs: admission,
    )
    receipt_path = tmp_path / "admit-or-declare-receipt.json"
    receipt_path.write_bytes(b"{}")
    build_inputs = dict(fixture)
    for key in (
        "provider_ledger_pointer_path",
        "matrixport_pointer_path",
        "quantbase_pointer_path",
        "portfolio123_pointer_path",
    ):
        build_inputs.pop(key)
    artifacts, manifest_content = build_layered_history_generation(
        repository_sha=target_series._trusted_layered_history_code_identity()[0],
        source_selection_mode="IMMUTABLE_GENERATIONS",
        provider_ledger_pointer_path=None,
        matrixport_pointer_path=None,
        quantbase_pointer_path=None,
        portfolio123_pointer_path=None,
        provider_admission_receipt_path=receipt_path,
        financial_input_contract_path=fixture["contract_path"],
        **build_inputs,
    )
    rows = list(csv.DictReader(io.StringIO(artifacts["live-only.csv"].decode("utf-8"))))
    robuxio = [row for row in rows if "Robuxio" in row["series_id"]]
    assert [row["date"] for row in robuxio] == ["2025-12-07", "2025-12-08"]
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]
    assert resolution["observed_start"] == robuxio[0]["date"]
    assert resolution["observed_end"] == robuxio[-1]["date"]
    validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
        provider_admission_receipt_path=receipt_path,
        financial_input_contract_path=fixture["contract_path"],
        provider_ledger_manifest_path=fixture["provider_ledger_manifest_path"],
        provider_ledger_catalog_path=fixture["provider_ledger_catalog_path"],
    )


@pytest.mark.parametrize("field", ["rates_csv", "native_nav_csv"])
def test_matrixport_usd_history_rejects_rehashed_source_preimage_tampering(
    tmp_path: Path, synthetic_history_authority: None, field: str
) -> None:
    fixture = _matrixport_fx_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    coverage = json.loads(artifacts["coverage.json"])
    proof = coverage["correction_resolutions"]["robuxio-account-live-after-funding"][
        "currency_conversion"
    ]
    proof[field] += "\n"
    _replace_output(
        artifacts, manifest, "coverage.json", canonical_json_bytes(coverage)
    )
    with pytest.raises(TargetSeriesError, match="FX.*identity|native NAV.*identity"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_matrixport_usd_history_keeps_same_basis_reconciliation_required(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    with pytest.raises(TargetSeriesError, match="account-live return differs"):
        _matrixport_fx_fixture(tmp_path, convert_ledger=False)


def test_matrixport_usd_history_rejects_substituted_fx_manifest_even_when_rehashed(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_fx_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    prior = GenerationManifest.from_json_bytes(
        manifest["source_manifests"]["portfolio-fx-rates"].encode("utf-8")
    )
    replacement = GenerationManifest.build(
        as_of_date=prior.as_of_date,
        adapter=prior.adapter,
        coverage=CoverageClaim.from_dict(
            {**prior.coverage.to_dict(), "boundary": "substituted FX generation"}
        ),
        artifacts=prior.artifacts,
        verification=prior.verification,
    )
    replacement_bytes = replacement.to_json_bytes()
    manifest["source_manifests"]["portfolio-fx-rates"] = replacement_bytes.decode(
        "utf-8"
    )
    for item in manifest["inputs"]:
        if item["input_id"] in {"portfolio-fx-rates-manifest", "portfolio-fx-rates"}:
            item["generation_id"] = replacement.generation_id
        if item["input_id"] == "portfolio-fx-rates-manifest":
            item["sha256"] = hashlib.sha256(replacement_bytes).hexdigest()
            item["size_bytes"] = len(replacement_bytes)
    with pytest.raises(
        TargetSeriesError, match="FX manifest identity differs from provider ledger"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_matrixport_usd_history_rejects_native_gap_return_in_usd_receipt(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_fx_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    coverage = json.loads(artifacts["coverage.json"])
    native_span = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"]["span_return_receipts"][0]
    coverage["epoch_hpr_receipts"][0]["receipt"]["span_return_receipts"][0][
        "return"
    ] = native_span["return"]
    _replace_output(
        artifacts, manifest, "coverage.json", canonical_json_bytes(coverage)
    )
    with pytest.raises(TargetSeriesError, match="epoch HPR receipt differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_matrixport_usd_history_requires_selected_fx_input(
    tmp_path: Path, synthetic_history_authority: None
) -> None:
    fixture = _matrixport_fx_fixture(tmp_path)
    fixture.pop("portfolio_fx_rates_manifest_path")
    with pytest.raises(TargetSeriesError, match="selected FX manifest input differs"):
        _build(fixture)


def test_layered_history_code_identity_ignores_inherited_git_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_identity = target_series._trusted_layered_history_code_identity()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-worktree"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "foreign-objects"))
    target_series._trusted_layered_history_code_identity.cache_clear()
    assert target_series._trusted_layered_history_code_identity() == trusted_identity


def test_layered_history_rejects_repository_dependency_outside_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = dict(target_series._committed_layered_history_dependency_sha256s())
    committed["tools/portfolio_financial_mvp_contract.py"] = "0" * 64
    monkeypatch.setattr(
        target_series,
        "_committed_layered_history_dependency_sha256s",
        lambda repository_sha=None: committed,
    )
    with pytest.raises(TargetSeriesError, match="repository dependency differs"):
        target_series._require_trusted_layered_history_dependencies()


def test_layered_history_verification_accepts_recorded_commit_only_when_dependencies_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    """A later unrelated HEAD does not invalidate a history with unchanged dependencies."""

    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    recorded_repository_sha = "a" * 40
    current_repository_sha = "b" * 40
    current_tool_sha256 = hashlib.sha256(
        (ROOT / "tools" / "portfolio_target_series.py").read_bytes()
    ).hexdigest()
    matching_dependencies = {
        relative_path: hashlib.sha256(
            ROOT.joinpath(*relative_path.split("/")).read_bytes()
        ).hexdigest()
        for relative_path in target_series.LAYERED_HISTORY_REPOSITORY_DEPENDENCIES
    }
    rewritten = json.loads(manifest_content)
    rewritten["repository_sha"] = recorded_repository_sha
    rewritten_content = _reidentify_manifest(rewritten)

    monkeypatch.setattr(
        target_series,
        "_trusted_layered_history_code_identity",
        lambda: (current_repository_sha, current_tool_sha256),
    )
    monkeypatch.setattr(
        target_series,
        "_committed_layered_history_dependency_sha256s",
        lambda repository_sha: matching_dependencies,
    )

    assert (
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=rewritten_content,
            contract_path=fixture["contract_path"],
        )["verification"]
        == "VALID"
    )
    target_series._require_trusted_layered_history_dependencies(recorded_repository_sha)

    mismatched_dependencies = dict(matching_dependencies)
    mismatched_dependencies["tools/portfolio_financial_mvp_contract.py"] = "0" * 64
    monkeypatch.setattr(
        target_series,
        "_committed_layered_history_dependency_sha256s",
        lambda repository_sha: mismatched_dependencies,
    )
    with pytest.raises(TargetSeriesError, match="repository dependency differs"):
        target_series._require_trusted_layered_history_dependencies(
            recorded_repository_sha
        )


def test_layered_history_rejects_dynamic_validator_with_different_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator_path = ROOT / "tools" / "portfolio_financial_mvp_contract.py"
    validator_spec = importlib.util.spec_from_file_location(
        "portfolio_financial_mvp_contract_strict_test", validator_path
    )
    assert validator_spec is not None and validator_spec.loader is not None
    validator = importlib.util.module_from_spec(validator_spec)
    sys.modules[validator_spec.name] = validator
    validator_spec.loader.exec_module(validator)
    default_contract = validator.DEFAULT_CONTRACT
    monkeypatch.setattr(
        target_series,
        "_require_trusted_layered_history_dependencies",
        lambda _repository_sha=None: None,
    )
    fake_core_path = tmp_path / "foreign_core" / "contracts.py"
    fake_core_path.parent.mkdir()
    fake_core_path.write_text(
        "import json\n"
        "def canonical_json_bytes(value):\n"
        "    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\\n'\n",
        encoding="utf-8",
    )
    fake_spec = importlib.util.spec_from_file_location(
        "quant_engine.core.contracts", fake_core_path
    )
    assert fake_spec is not None and fake_spec.loader is not None
    fake_core = importlib.util.module_from_spec(fake_spec)
    fake_spec.loader.exec_module(fake_core)
    monkeypatch.setitem(sys.modules, "quant_engine.core.contracts", fake_core)
    with pytest.raises(TargetSeriesError, match="financial MVP contract is invalid"):
        target_series._history_contract(default_contract)


def test_layered_history_real_contract_parser_rejects_unknown_top_level_key(
    tmp_path: Path,
) -> None:
    """Strict contract QC is intentionally outside synthetic producer fixtures."""

    contract_path = tmp_path / "portfolio-financial-mvp-source-v1.json"
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    contract["uncontrolled_policy"] = "ACCEPT"
    _write_json(contract_path, contract)

    with pytest.raises(TargetSeriesError, match="financial MVP contract is invalid"):
        target_series._history_contract(contract_path)


def test_layered_history_omits_current_echo_and_retains_historical_resolution(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, _manifest_content = _build(fixture)

    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    assert resolution["source_generation_id"] == (
        "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e"
    )
    assert resolution["source_receipt_sha256"] == (
        "0ec0ea74948da163295524be56afadc4ed485f25f10ed8dac6d329aac6772b56"
    )
    assert resolution["sha256"] == (
        "55d339e5299607daf72b44be72a03cc13998d34e81fc42d8e0a756152347ef4f"
    )
    excluded = list(
        csv.DictReader(io.StringIO(artifacts["excluded.csv"].decode("utf-8")))
    )
    assert excluded == []
    assert not any(
        row["series_id"] == "Growth at Market Pivot Points [EchoTrade]"
        for artifact in (
            "live-only.csv",
            "live-plus-backtest.csv",
            "returns.csv",
            "segments.csv",
        )
        for row in csv.DictReader(io.StringIO(artifacts[artifact].decode("utf-8")))
    )


def test_layered_history_accepts_exact_retained_echo_current_rows_without_emitting(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    _add_retained_echo_to_current_ledger(fixture)
    _refresh_materialized_segment_identities(fixture)

    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    assert resolution["sha256"] == (
        "55d339e5299607daf72b44be72a03cc13998d34e81fc42d8e0a756152347ef4f"
    )
    assert resolution["source_generation_id"] != next(
        row["generation_id"]
        for row in manifest["inputs"]
        if row["input_id"] == "provider-ledger-manifest"
    )
    excluded = list(
        csv.DictReader(io.StringIO(artifacts["excluded.csv"].decode("utf-8")))
    )
    assert excluded == []
    assert not any(
        row["series_id"] == "Growth at Market Pivot Points [EchoTrade]"
        for artifact in (
            "live-only.csv",
            "live-plus-backtest.csv",
            "returns.csv",
            "segments.csv",
        )
        for row in csv.DictReader(io.StringIO(artifacts[artifact].decode("utf-8")))
    )


def test_layered_history_rejects_mutated_retained_echo_current_rows(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    _add_retained_echo_to_current_ledger(fixture, mutate=True)

    with pytest.raises(
        TargetSeriesError,
        match="EchoTrade rejected evidence differs from product authority",
    ):
        _refresh_materialized_segment_identities(fixture)


def test_layered_history_generation_is_deterministic_and_fail_closed(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    repeated_artifacts, repeated_manifest = _build(fixture)
    assert artifacts == repeated_artifacts
    assert manifest_content == repeated_manifest
    assert json.loads(manifest_content)["builder_version"] == "1.14.0"
    generation_schema = json.loads(
        (
            ROOT / "schemas" / "portfolio" / "layered-history-generation.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert generation_schema["properties"]["builder_version"] == {
        "enum": ["1.12.0", "1.13.0", "1.14.0"]
    }
    assert (
        "strategy-history"
        in generation_schema["properties"]["source_manifests"]["properties"]
    )

    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    assert result["strategy_count"] == 9
    coverage = json.loads(artifacts["coverage.json"])
    coverage_by_strategy = {row["strategy_id"]: row for row in coverage["strategies"]}
    epoch_hpr_receipts = coverage["epoch_hpr_receipts"]
    assert [row["strategy_id"] for row in epoch_hpr_receipts] == ["robuxio-crypto"]
    robuxio_epoch_hpr = epoch_hpr_receipts[0]["receipt"]
    robuxio_daily = [
        row
        for row in csv.DictReader(
            io.StringIO(artifacts["live-only.csv"].decode("utf-8"))
        )
        if row["series_id"] == coverage_by_strategy["robuxio-crypto"]["series_id"]
    ]
    assert robuxio_epoch_hpr["daily_return_observations"] == len(robuxio_daily)
    assert (
        robuxio_epoch_hpr["epoch_holding_period_link_observations"]
        >= robuxio_epoch_hpr["daily_return_observations"]
    )
    assert coverage_by_strategy["echotrade-growth-pivots"]["segments"] == []
    assert coverage_by_strategy["echotrade-growth-pivots"]["modes"]["LIVE_ONLY"] == {
        "reason": "NOT_APPLICABLE_OWNER_APPROVED_DEPRECATED",
        "status": "NOT_APPLICABLE_OWNER_APPROVED",
    }
    assert coverage_by_strategy["composer-holy-grail"]["segments"] == []
    assert coverage_by_strategy["composer-holy-grail"]["modes"]["LIVE_ONLY"] == {
        "reason": "NOT_APPLICABLE_OWNER_APPROVED_IMMATERIAL",
        "status": "NOT_APPLICABLE_OWNER_APPROVED",
    }
    assert not {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    } & set(
        json.loads(manifest_content)["mode_status"]["LIVE_ONLY"][
            "required_not_ready_strategy_ids"
        ]
    )
    assert coverage["correction_resolutions"]["p123-eu-provider-live"] == {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 7,
        "observed_end": "2025-11-21",
        "observed_start": "2025-11-14",
        "raw_level_observations": 7,
        "return_observations": 6,
        "status": "DERIVED_NOT_ADMITTED_PENDING_303",
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    assert coverage["correction_resolutions"]["p123-ai-backtest"]["status"] == (
        "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    )
    echo_resolution = coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    assert echo_resolution == {
        "canonicalization": "TARGET_FIELDS_CANONICAL_JSON_V1",
        "observed_end": "2025-11-24",
        "observed_start": "2025-10-01",
        "return_observations": 13,
        "sha256": "55d339e5299607daf72b44be72a03cc13998d34e81fc42d8e0a756152347ef4f",
        "source_generation_id": "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e",
        "source_receipt_sha256": "0ec0ea74948da163295524be56afadc4ed485f25f10ed8dac6d329aac6772b56",
        "status": "REJECTED_EVENT_ONLY_RECONSTRUCTION_NOT_DAILY_NAV",
    }
    excluded = list(
        csv.DictReader(io.StringIO(artifacts["excluded.csv"].decode("utf-8")))
    )
    assert excluded == []
    assert not any(
        row["strategy_id"] == "p123-ai-factor" and row["evidence_layer"] == "BACKTEST"
        for row in csv.DictReader(
            io.StringIO(artifacts["segments.csv"].decode("utf-8"))
        )
    )
    live_only = artifacts["live-only.csv"].decode("utf-8")
    assert "PROVIDER_LIVE" not in live_only
    assert "BACKTEST" not in live_only
    assert "p123-eu:provider-live" not in artifacts["segments.csv"].decode("utf-8")
    assert "private-product" not in artifacts["coverage.json"].decode("utf-8")
    assert "private-fund" not in artifacts["coverage.json"].decode("utf-8")

    destination = write_layered_history_generation(
        tmp_path / "candidate", artifacts, manifest_content
    )
    assert (
        write_layered_history_generation(
            tmp_path / "candidate", artifacts, manifest_content
        )
        == destination
    )
    verified = verify_layered_history_generation(destination, fixture["contract_path"])
    assert verified["verification"] == "VALID"
    assert "assurance" not in verified

    extra_file_destination = write_layered_history_generation(
        tmp_path / "candidate-extra-file", artifacts, manifest_content
    )
    (extra_file_destination / "untracked.txt").write_text(
        "not receipt-bound", encoding="utf-8"
    )
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            extra_file_destination, fixture["contract_path"]
        )

    extra_directory_destination = write_layered_history_generation(
        tmp_path / "candidate-extra-directory", artifacts, manifest_content
    )
    (extra_directory_destination / "untracked").mkdir()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            extra_directory_destination, fixture["contract_path"]
        )

    missing_file_destination = write_layered_history_generation(
        tmp_path / "candidate-missing-file", artifacts, manifest_content
    )
    (missing_file_destination / "coverage.json").unlink()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            missing_file_destination, fixture["contract_path"]
        )

    replaced_file_destination = write_layered_history_generation(
        tmp_path / "candidate-replaced-file", artifacts, manifest_content
    )
    (replaced_file_destination / "coverage.json").unlink()
    (replaced_file_destination / "coverage.json").mkdir()
    with pytest.raises(TargetSeriesError, match="generation inventory differs"):
        verify_layered_history_generation(
            replaced_file_destination, fixture["contract_path"]
        )

    symlink_destination = write_layered_history_generation(
        tmp_path / "candidate-symlink", artifacts, manifest_content
    )
    symlink_path = symlink_destination / "coverage.json"
    symlink_target = tmp_path / "external-coverage.json"
    symlink_target.write_bytes(artifacts["coverage.json"])
    symlink_path.unlink()
    try:
        symlink_path.symlink_to(symlink_target)
    except OSError:
        pass
    else:
        with pytest.raises(TargetSeriesError, match="generation inventory differs"):
            verify_layered_history_generation(
                symlink_destination, fixture["contract_path"]
            )

    rewritten_tool = json.loads(manifest_content)
    rewritten_tool["tool_sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="code identity is not trusted"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(rewritten_tool),
            contract_path=fixture["contract_path"],
        )

    crlf_artifacts = dict(artifacts)
    crlf_manifest = json.loads(manifest_content)
    crlf_segments = artifacts["segments.csv"].replace(b"\n", b"\r\n")
    _replace_output(crlf_artifacts, crlf_manifest, "segments.csv", crlf_segments)
    with pytest.raises(TargetSeriesError, match="segments serialization differs"):
        validate_layered_history_generation(
            artifacts=crlf_artifacts,
            manifest_content=_reidentify_manifest(crlf_manifest),
            contract_path=fixture["contract_path"],
        )

    quoted_artifacts = dict(artifacts)
    quoted_manifest = json.loads(manifest_content)
    quoted_rows = list(
        csv.DictReader(io.StringIO(artifacts["live-only.csv"].decode("utf-8")))
    )
    quoted_buffer = io.StringIO(newline="")
    quoted_writer = csv.DictWriter(
        quoted_buffer,
        fieldnames=target_series.CORRELATION_FIELDS_V2,
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    quoted_writer.writeheader()
    quoted_writer.writerows(quoted_rows)
    _replace_output(
        quoted_artifacts,
        quoted_manifest,
        "live-only.csv",
        quoted_buffer.getvalue().encode("utf-8"),
    )
    with pytest.raises(TargetSeriesError, match="history serialization differs"):
        validate_layered_history_generation(
            artifacts=quoted_artifacts,
            manifest_content=_reidentify_manifest(quoted_manifest),
            contract_path=fixture["contract_path"],
        )

    with pytest.raises(
        TargetSeriesError, match="repository SHA differs from the executing checkout"
    ):
        build_layered_history_generation(repository_sha="0" * 40, **fixture)

    with pytest.raises(TargetSeriesError, match="required history mode is not ready"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=manifest_content,
            contract_path=fixture["contract_path"],
            require_mode="LIVE_PLUS_BACKTEST",
        )

    stale = json.loads(manifest_content)
    manifest_input = next(
        row for row in stale["inputs"] if row["input_id"] == "provider-ledger-manifest"
    )
    manifest_input["sha256"] = "f" * 64
    with pytest.raises(TargetSeriesError, match="current input differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(stale),
            contract_path=fixture["contract_path"],
        )

    rewritten_artifact = json.loads(manifest_content)
    performance_input = next(
        row
        for row in rewritten_artifact["inputs"]
        if row["input_id"] == "portfolio123-performance"
    )
    performance_input["sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="source artifact differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(rewritten_artifact),
            contract_path=fixture["contract_path"],
        )

    extra_field = json.loads(manifest_content)
    extra_field["uncontrolled_policy"] = "ACCEPT"
    with pytest.raises(TargetSeriesError, match="generation keys differ"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(extra_field),
            contract_path=fixture["contract_path"],
        )

    rewritten_echo = dict(artifacts)
    rewritten_echo_manifest = json.loads(manifest_content)
    rewritten_echo_coverage = json.loads(artifacts["coverage.json"])
    rewritten_echo_resolution = rewritten_echo_coverage["correction_resolutions"][
        "echotrade-event-only-account-live-rejected"
    ]
    rewritten_echo_resolution["sha256"] = "0" * 64
    rewritten_echo_resolution["return_observations"] += 1
    _replace_output(
        rewritten_echo,
        rewritten_echo_manifest,
        "coverage.json",
        canonical_json_bytes(rewritten_echo_coverage),
    )
    with pytest.raises(TargetSeriesError, match="event-only resolution differs"):
        validate_layered_history_generation(
            artifacts=rewritten_echo,
            manifest_content=_reidentify_manifest(rewritten_echo_manifest),
            contract_path=fixture["contract_path"],
        )

    baseline_coverage = json.loads(artifacts["coverage.json"])
    ledger_catalog_path = fixture["provider_ledger_catalog_path"]
    ledger_catalog = json.loads(ledger_catalog_path.read_text(encoding="utf-8"))
    qsi_row = next(
        row for row in ledger_catalog["strategy_returns"] if row["strategy_id"] == "qsi"
    )
    qsi_row["return"] = "0.123456"
    _write_json(ledger_catalog_path, ledger_catalog)
    ledger_manifest_path = fixture["provider_ledger_manifest_path"]
    ledger_generation_id = _core_generation_manifest(
        ledger_manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        [_artifact(ledger_catalog_path, "normalized/provider-ledger.json")],
    )
    _pointer(
        fixture["provider_ledger_pointer_path"],
        ledger_generation_id,
        ledger_manifest_path,
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        ledger_manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(fixture["contract_path"], contract)
    _refresh_materialized_segment_identities(fixture)

    changed_artifacts, changed_manifest = _build(fixture)
    changed_coverage = json.loads(changed_artifacts["coverage.json"])
    baseline_hashes = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in baseline_coverage["strategies"]
    }
    changed_hashes = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in changed_coverage["strategies"]
    }
    assert changed_hashes["qsi"] != baseline_hashes["qsi"]
    assert {
        strategy_id: segment_hashes
        for strategy_id, segment_hashes in changed_hashes.items()
        if strategy_id != "qsi"
    } == {
        strategy_id: segment_hashes
        for strategy_id, segment_hashes in baseline_hashes.items()
        if strategy_id != "qsi"
    }
    assert changed_artifacts["returns.csv"] != artifacts["returns.csv"]
    assert changed_manifest != manifest_content


def test_layered_history_uses_ledger_account_return_after_matrixport_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest = _build(fixture)
    original = target_series._matrixport_returns
    perturbation = Decimal("1e-28")

    def perturbed_matrixport_returns(*args: Any, **kwargs: Any):
        rows, coverage = original(*args, **kwargs)
        changed = [dict(row) for row in rows]
        with localcontext() as context:
            context.prec = 50
            changed[0]["return"] = format(
                Decimal(changed[0]["return"]) + perturbation,
                "f",
            )
        changed_coverage = dict(coverage)
        gap_policy = coverage["source_gap_policy"]
        assert isinstance(gap_policy, dict)
        changed_coverage["epoch_hpr_receipt"] = (
            target_series._matrixport_epoch_hpr_receipt(
                returns=changed,
                span_return_receipts=gap_policy["span_return_receipts"],
                source_gap_policy=gap_policy,
                admission_cutoff=date.fromisoformat(str(coverage["admission_cutoff"])),
            )
        )
        return changed, changed_coverage

    monkeypatch.setattr(
        target_series,
        "_matrixport_returns",
        perturbed_matrixport_returns,
    )
    reconciled_artifacts, reconciled_manifest = _build(fixture)
    assert reconciled_artifacts == baseline_artifacts
    assert reconciled_manifest == baseline_manifest

    perturbation = Decimal("1e-12")
    with pytest.raises(
        TargetSeriesError,
        match="Matrixport account-live return differs from provider ledger",
    ):
        _build(fixture)


def test_layered_matrixport_output_isolated_from_ambient_decimal_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    """The selected Matrixport account path is stable under caller context changes."""

    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest = _build(fixture)
    original = target_series._matrixport_returns

    def under_changed_context(*args: Any, **kwargs: Any):
        original_context = getcontext().copy()
        try:
            getcontext().prec = 16
            getcontext().rounding = ROUND_DOWN
            return original(*args, **kwargs)
        finally:
            getcontext().prec = original_context.prec
            getcontext().rounding = original_context.rounding

    monkeypatch.setattr(target_series, "_matrixport_returns", under_changed_context)
    isolated_artifacts, isolated_manifest = _build(fixture)

    assert isolated_artifacts == baseline_artifacts
    assert isolated_manifest == baseline_manifest


def test_layered_history_replays_robuxio_funding_correction_with_bounded_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest_content = _build(fixture)
    baseline_manifest = json.loads(baseline_manifest_content)
    baseline_coverage = json.loads(baseline_artifacts["coverage.json"])
    baseline_result = validate_layered_history_generation(
        artifacts=baseline_artifacts,
        manifest_content=baseline_manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert baseline_result["verification"] == "VALID"

    subscription_path = fixture["matrixport_subscription_path"]
    _write_csv(
        subscription_path,
        [
            "confirm_nav_time_utc",
            "product_id",
            "status",
            "subscription_share",
            "subscription_time_utc",
        ],
        [
            {
                "confirm_nav_time_utc": "2025-12-07T00:00:00Z",
                "product_id": "private-product",
                "status": "300",
                "subscription_share": "10",
                "subscription_time_utc": "2025-12-06T20:00:00Z",
            }
        ],
    )
    matrixport_manifest_path = fixture["matrixport_manifest_path"]
    matrixport_manifest = json.loads(
        matrixport_manifest_path.read_text(encoding="utf-8")
    )
    subscription_artifact = next(
        row
        for row in matrixport_manifest["artifacts"]
        if row["artifact_id"] == "csv/subscription_details.csv"
    )
    subscription_artifact.update(
        _artifact(subscription_path, "csv/subscription_details.csv")
    )
    matrixport_generation_id = _core_generation_manifest(
        matrixport_manifest_path,
        "quant-engine-matrixport",
        fixture["as_of_date"],
        matrixport_manifest["artifacts"],
        {"declared_exclusions": matrixport_manifest["coverage"]["declared_exclusions"]},
    )
    _pointer(
        fixture["matrixport_pointer_path"],
        matrixport_generation_id,
        matrixport_manifest_path,
    )

    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _update_correction_interval(
        contract,
        "robuxio-provider-live-before-funding",
        observed_start="2025-12-01",
        observed_end="2025-12-06",
        effective_start="2025-12-01",
        effective_end="2025-12-06",
    )
    _update_correction_interval(
        contract,
        "robuxio-account-live-after-funding",
        observed_start="2025-12-08",
        observed_end="2025-12-11",
        effective_start="2025-12-07",
        effective_end=None,
    )
    _update_source_generation(
        contract,
        "matrixport-account-generation",
        matrixport_manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        matrixport_manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)

    corrected_artifacts, corrected_manifest_content = _build(fixture)
    corrected_manifest = json.loads(corrected_manifest_content)
    corrected_coverage = json.loads(corrected_artifacts["coverage.json"])
    result = validate_layered_history_generation(
        artifacts=corrected_artifacts,
        manifest_content=corrected_manifest_content,
        contract_path=contract_path,
    )
    assert result["verification"] == "VALID"
    assert corrected_manifest["generation_id"] != baseline_manifest["generation_id"]

    baseline_robuxio = next(
        row
        for row in baseline_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    corrected_robuxio = next(
        row
        for row in corrected_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    baseline_robuxio_segments = {
        row["segment_id"]: row for row in baseline_robuxio["segments"]
    }
    corrected_robuxio_segments = {
        row["segment_id"]: row for row in corrected_robuxio["segments"]
    }
    provider_segment_id = "robuxio-crypto:provider-live-before-funding"
    account_segment_id = "robuxio-crypto:account-live"
    assert (
        corrected_robuxio_segments[provider_segment_id]["sha256"]
        != baseline_robuxio_segments[provider_segment_id]["sha256"]
    )
    assert (
        corrected_robuxio_segments[account_segment_id]["sha256"]
        != baseline_robuxio_segments[account_segment_id]["sha256"]
    )
    assert (
        baseline_robuxio_segments[provider_segment_id]["observed_start"],
        baseline_robuxio_segments[provider_segment_id]["observed_end"],
    ) == ("2025-12-02", "2025-12-05")
    assert (
        corrected_robuxio_segments[provider_segment_id]["observed_start"],
        corrected_robuxio_segments[provider_segment_id]["observed_end"],
    ) == ("2025-12-02", "2025-12-06")
    assert (
        baseline_robuxio_segments[account_segment_id]["observed_start"],
        corrected_robuxio_segments[account_segment_id]["observed_start"],
    ) == ("2025-12-07", "2025-12-08")

    original_provider_returns = target_series._matrixport_provider_returns

    def stale_provider_returns(
        nav_path: Path,
        product_id: str,
        series_id: str,
        _account_live_start: object,
    ) -> list[dict[str, str]]:
        return original_provider_returns(
            nav_path,
            product_id,
            series_id,
            target_series.date(2025, 12, 6),
        )

    monkeypatch.setattr(
        target_series, "_matrixport_provider_returns", stale_provider_returns
    )
    with pytest.raises(
        TargetSeriesError,
        match="Robuxio provider-live interval differs from contract",
    ):
        _build(fixture)
    monkeypatch.setattr(
        target_series, "_matrixport_provider_returns", original_provider_returns
    )

    baseline_segments = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in baseline_coverage["strategies"]
    }
    corrected_segments = {
        row["strategy_id"]: {
            segment["segment_id"]: segment["sha256"] for segment in row["segments"]
        }
        for row in corrected_coverage["strategies"]
    }
    assert baseline_segments.keys() == corrected_segments.keys()
    assert corrected_segments["robuxio-crypto"] != baseline_segments["robuxio-crypto"]
    assert {
        strategy_id: segments
        for strategy_id, segments in corrected_segments.items()
        if strategy_id != "robuxio-crypto"
    } == {
        strategy_id: segments
        for strategy_id, segments in baseline_segments.items()
        if strategy_id != "robuxio-crypto"
    }

    baseline_resolutions = baseline_coverage["correction_resolutions"]
    corrected_resolutions = corrected_coverage["correction_resolutions"]
    assert baseline_resolutions.keys() == corrected_resolutions.keys()
    assert (
        corrected_resolutions["robuxio-account-live-after-funding"]
        != baseline_resolutions["robuxio-account-live-after-funding"]
    )
    assert {
        correction_id: resolution
        for correction_id, resolution in corrected_resolutions.items()
        if correction_id != "robuxio-account-live-after-funding"
    } == {
        correction_id: resolution
        for correction_id, resolution in baseline_resolutions.items()
        if correction_id != "robuxio-account-live-after-funding"
    }

    baseline_inputs = {row["input_id"]: row for row in baseline_manifest["inputs"]}
    corrected_inputs = {row["input_id"]: row for row in corrected_manifest["inputs"]}
    assert baseline_inputs.keys() == corrected_inputs.keys()
    assert {
        input_id
        for input_id in baseline_inputs
        if baseline_inputs[input_id] != corrected_inputs[input_id]
    } == {
        "financial-mvp-contract",
        "matrixport-current",
        "matrixport-holdings",
        "matrixport-manifest",
        "matrixport-nav",
        "matrixport-subscriptions",
    }
    for input_id in ("matrixport-holdings", "matrixport-nav"):
        assert (
            corrected_inputs[input_id]["sha256"] == baseline_inputs[input_id]["sha256"]
        )
        assert (
            corrected_inputs[input_id]["size_bytes"]
            == baseline_inputs[input_id]["size_bytes"]
        )
    assert corrected_artifacts["excluded.csv"] == baseline_artifacts["excluded.csv"]
    assert baseline_artifacts.keys() == corrected_artifacts.keys()
    assert (
        baseline_manifest["source_manifests"].keys()
        == corrected_manifest["source_manifests"].keys()
    )
    assert {
        name
        for name in baseline_artifacts
        if baseline_artifacts[name] != corrected_artifacts[name]
    } == {
        "coverage.json",
        "live-only.csv",
        "live-plus-backtest.csv",
        "returns.csv",
        "segments.csv",
    }
    assert (
        baseline_manifest["source_pointers"].keys()
        == corrected_manifest["source_pointers"].keys()
    )
    assert {
        name
        for name in baseline_manifest["source_manifests"]
        if baseline_manifest["source_manifests"][name]
        != corrected_manifest["source_manifests"][name]
    } == {"matrixport"}
    assert {
        name
        for name in baseline_manifest["source_pointers"]
        if baseline_manifest["source_pointers"][name]
        != corrected_manifest["source_pointers"][name]
    } == {"matrixport"}


def test_layered_history_provider_interval_matches_current_product_authority() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    provider_correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "robuxio-provider-live-before-funding"
    )
    provider_segment = next(
        row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "robuxio-crypto:provider-live-before-funding"
    )
    target_series._validate_robuxio_provider_interval(
        [
            {"date": provider_segment["observed_start"]},
            {"date": provider_segment["observed_end"]},
        ],
        provider_correction["interval"],
    )
    assert (
        provider_segment["observed_start"]
        > provider_correction["interval"]["observed_start"]
    )
    assert (
        provider_segment["observed_end"]
        == provider_correction["interval"]["observed_end"]
    )


def test_complete_account_history_age_is_warning_not_unavailability(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    owner_history = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "owner-account-live-history"
    )
    owner_history["freshness"]["max_age_days"] = 0
    _write_json(fixture["contract_path"], contract)

    artifacts, manifest_content = _build(fixture)
    coverage = json.loads(artifacts["coverage.json"])
    qsi = next(row for row in coverage["strategies"] if row["strategy_id"] == "qsi")
    assert qsi["data_age"] == {
        "age_days": 1,
        "complete_through": "2026-01-09",
        "evaluated_on": "2026-01-10",
        "status": "WARN_COMPLETE_DATA_AGE",
        "warning_after_days": 0,
    }
    assert qsi["modes"]["LIVE_ONLY"] == {
        "reason": "WARN_ACCOUNT_LIVE_COMPLETE_DATA_AGE",
        "status": "AVAILABLE",
    }
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"


def test_layered_history_verification_applies_assurance_profile_after_validation(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    destination = write_layered_history_generation(
        tmp_path / "assurance-candidate", artifacts, manifest_content
    )
    receipt_as_of = date.fromisoformat(fixture["as_of_date"])

    for profile in ("DEVELOPMENT", "WORKSTATION_QC"):
        current = verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile=profile,
            evaluated_on=receipt_as_of,
        )
        assert current["assurance"] == {
            "evaluated_on": receipt_as_of.isoformat(),
            "profile": profile,
            "profile_policy": (
                "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
            ),
            "receipt_age_days": 0,
            "receipt_as_of": receipt_as_of.isoformat(),
            "status": "CURRENT_IMMUTABLE_RECEIPT",
        }

        reused_on = receipt_as_of + timedelta(days=4)
        reused = verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile=profile,
            evaluated_on=reused_on,
        )
        assert reused["assurance"] == {
            "evaluated_on": reused_on.isoformat(),
            "profile": profile,
            "profile_policy": (
                "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
            ),
            "receipt_age_days": 4,
            "receipt_as_of": receipt_as_of.isoformat(),
            "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
        }

    release = verify_layered_history_generation(
        destination,
        fixture["contract_path"],
        assurance_profile="RELEASE_QC",
        evaluated_on=receipt_as_of,
    )
    assert release["assurance"] == {
        "evaluated_on": receipt_as_of.isoformat(),
        "profile": "RELEASE_QC",
        "profile_policy": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
        "receipt_age_days": 0,
        "receipt_as_of": receipt_as_of.isoformat(),
        "status": "CURRENT_RELEASE_RECEIPT",
    }

    older = verify_layered_history_generation(
        destination,
        fixture["contract_path"],
        assurance_profile="RELEASE_QC",
        evaluated_on=receipt_as_of + timedelta(days=1),
    )
    assert older["assurance"] == {
        "evaluated_on": (receipt_as_of + timedelta(days=1)).isoformat(),
        "profile": "RELEASE_QC",
        "profile_policy": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
        "receipt_age_days": 1,
        "receipt_as_of": receipt_as_of.isoformat(),
        "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
    }
    with pytest.raises(
        TargetSeriesError,
        match="receipt exceeds the assurance evaluation date",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of - timedelta(days=1),
        )
    with pytest.raises(
        TargetSeriesError,
        match="assurance profile and evaluation date must be supplied together",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
        )
    with pytest.raises(
        TargetSeriesError,
        match="assurance profile and evaluation date must be supplied together",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            evaluated_on=receipt_as_of,
        )
    with pytest.raises(
        TargetSeriesError,
        match="required history mode is not ready: LIVE_PLUS_BACKTEST",
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            require_mode="LIVE_PLUS_BACKTEST",
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of + timedelta(days=1),
        )

    for rewritten_as_of in ("20260110", "2026-W02-6"):
        noncanonical_manifest = json.loads(manifest_content)
        noncanonical_manifest["as_of_date"] = rewritten_as_of
        noncanonical_destination = write_layered_history_generation(
            tmp_path / f"noncanonical-{rewritten_as_of}",
            artifacts,
            _reidentify_manifest(noncanonical_manifest),
        )
        with pytest.raises(
            TargetSeriesError,
            match="layered history generation as-of is invalid",
        ):
            verify_layered_history_generation(
                noncanonical_destination,
                fixture["contract_path"],
                assurance_profile="RELEASE_QC",
                evaluated_on=receipt_as_of,
            )

    rewritten_contract = json.loads(
        fixture["contract_path"].read_text(encoding="utf-8")
    )
    rewritten_contract["product"]["as_of_policy"]["execution_profiles"][
        "DEVELOPMENT"
    ] = "REUSE_WITHOUT_WARNING"
    _write_json(fixture["contract_path"], rewritten_contract)
    with pytest.raises(
        TargetSeriesError, match="layered history product contract identity differs"
    ):
        verify_layered_history_generation(
            destination,
            fixture["contract_path"],
            assurance_profile="DEVELOPMENT",
            evaluated_on=receipt_as_of,
        )


def test_layered_history_accepts_only_manifest_declared_matrixport_gap(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)

    artifacts, manifest_content = _build(fixture)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    live_dates = [
        row["date"]
        for row in segments
        if row["strategy_id"] == "robuxio-crypto"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    assert live_dates == ["2025-12-07", "2025-12-08", "2025-12-11"]
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]
    fixture_contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    fixture_source = next(
        row
        for row in fixture_contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    assert resolution["source_gap_policy"] == fixture_source["declared_gap_policy"]
    assert resolution["source_gap_policy"]["declared_missing_source_dates"] == [
        "2025-12-09"
    ]
    assert resolution["source_gap_policy"]["omitted_return_dates"] == ["2025-12-10"]
    robuxio_summary = next(
        summary
        for row in coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
        for summary in row["segments"]
        if summary["segment_id"] == "robuxio-crypto:account-live"
    )
    assert robuxio_summary["gap_disposition"] == ("COMPLETE_WITHIN_DECLARED_BOUNDARY")

    tampered_artifacts = dict(artifacts)
    tampered_manifest = json.loads(manifest_content)
    resolution["source_gap_policy"]["cumulative_return_across_declared_gaps"] = (
        "MATERIALIZED"
    )
    _replace_output(
        tampered_artifacts,
        tampered_manifest,
        "coverage.json",
        canonical_json_bytes(coverage),
    )
    with pytest.raises(TargetSeriesError, match="source gap resolution differs"):
        validate_layered_history_generation(
            artifacts=tampered_artifacts,
            manifest_content=_reidentify_manifest(tampered_manifest),
            contract_path=fixture["contract_path"],
        )


def test_layered_history_propagates_one_authoritative_gap_policy_edit(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    baseline_artifacts, baseline_manifest_content = _build(fixture)
    baseline_manifest = json.loads(baseline_manifest_content)
    baseline_coverage = json.loads(baseline_artifacts["coverage.json"])
    contract_path = fixture["contract_path"]
    manifest_path = fixture["matrixport_manifest_path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    policy = source["declared_gap_policy"]
    policy.update(
        {
            "authority_issue": 999,
            "policy_id": "SYNTHETIC_OWNER_DECLARED_GAP_POLICY_V2",
            "provenance": "synthetic owner evidence for propagation",
            "reason": "synthetic provider omission",
            "return_rule": "SYNTHETIC_ADJACENT_DAY_RESET_RULE",
        }
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for exclusion in manifest["coverage"]["declared_exclusions"]:
        exclusion["provenance"] = policy["provenance"]
        exclusion["reason"] = policy["reason"]
    matrixport_generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-matrixport",
        fixture["as_of_date"],
        manifest["artifacts"],
        {"declared_exclusions": manifest["coverage"]["declared_exclusions"]},
    )
    _pointer(
        fixture["matrixport_pointer_path"], matrixport_generation_id, manifest_path
    )
    _update_source_generation(
        contract, "matrixport-account-generation", manifest_path, fixture["as_of_date"]
    )
    _update_correction_generations(
        contract,
        "matrixport-account-generation",
        manifest_path,
        fixture["as_of_date"],
        "MATRIXPORT",
    )
    _write_json(contract_path, contract)
    _refresh_materialized_segment_identities(fixture)

    artifacts, manifest_content = _build(fixture)
    changed_manifest = json.loads(manifest_content)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=contract_path,
    )
    assert result["verification"] == "VALID"
    coverage = json.loads(artifacts["coverage.json"])
    propagated = coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"]
    assert propagated == policy
    assert changed_manifest["generation_id"] != baseline_manifest["generation_id"]
    assert (
        changed_manifest["product"]["contract_sha256"]
        != (baseline_manifest["product"]["contract_sha256"])
    )

    baseline_inputs = {row["input_id"]: row for row in baseline_manifest["inputs"]}
    changed_inputs = {row["input_id"]: row for row in changed_manifest["inputs"]}
    assert {
        input_id
        for input_id in baseline_inputs
        if baseline_inputs[input_id] != changed_inputs[input_id]
    } == {
        "financial-mvp-contract",
        "matrixport-current",
        "matrixport-holdings",
        "matrixport-manifest",
        "matrixport-nav",
        "matrixport-subscriptions",
    }
    for input_id in (
        "matrixport-holdings",
        "matrixport-nav",
        "matrixport-subscriptions",
    ):
        assert changed_inputs[input_id]["sha256"] == baseline_inputs[input_id]["sha256"]
        assert (
            changed_inputs[input_id]["size_bytes"]
            == baseline_inputs[input_id]["size_bytes"]
        )
        assert (
            changed_inputs[input_id]["generation_id"]
            != baseline_inputs[input_id]["generation_id"]
        )
    assert {
        name
        for name in baseline_artifacts
        if baseline_artifacts[name] != artifacts[name]
    } == {"coverage.json", "segments.csv"}
    assert {
        name
        for name in baseline_manifest["source_manifests"]
        if baseline_manifest["source_manifests"][name]
        != changed_manifest["source_manifests"][name]
    } == {"matrixport"}
    assert {
        name
        for name in baseline_manifest["source_pointers"]
        if baseline_manifest["source_pointers"][name]
        != changed_manifest["source_pointers"][name]
    } == {"matrixport"}

    baseline_policy = baseline_coverage["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"]
    assert {
        field
        for field in baseline_policy
        if baseline_policy[field] != propagated[field]
    } == {
        "authority_issue",
        "policy_id",
        "provenance",
        "reason",
        "return_rule",
    }
    coverage_with_baseline_policy = copy.deepcopy(coverage)
    coverage_with_baseline_policy["correction_resolutions"][
        "robuxio-account-live-after-funding"
    ]["source_gap_policy"] = baseline_policy
    baseline_epoch_hpr = baseline_coverage["epoch_hpr_receipts"][0]
    changed_epoch_hpr = coverage_with_baseline_policy["epoch_hpr_receipts"][0]
    assert changed_epoch_hpr["strategy_id"] == "robuxio-crypto"
    assert changed_epoch_hpr["receipt"]["source_gap_policy_sha256"] == (
        hashlib.sha256(canonical_json_bytes(policy)).hexdigest()
    )
    assert (
        changed_epoch_hpr["receipt"]["source_gap_policy_sha256"]
        != baseline_epoch_hpr["receipt"]["source_gap_policy_sha256"]
    )

    baseline_robuxio = next(
        row
        for row in baseline_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    changed_robuxio = next(
        row
        for row in coverage_with_baseline_policy["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    baseline_segments = {row["segment_id"]: row for row in baseline_robuxio["segments"]}
    changed_segments = {row["segment_id"]: row for row in changed_robuxio["segments"]}
    account_live_lineage_path = (
        "strategies[robuxio-crypto].segments[robuxio-crypto:account-live]."
        "source_generation_id"
    )
    provider_live_lineage_path = (
        "strategies[robuxio-crypto].segments[robuxio-crypto:provider-live-before-"
        "funding].source_generation_id"
    )
    matrixport_generation_lineage = {
        "epoch_hpr_receipts[0].source_generation_id": (
            baseline_epoch_hpr["source_generation_id"],
            changed_epoch_hpr["source_generation_id"],
        ),
        account_live_lineage_path: (
            baseline_segments["robuxio-crypto:account-live"]["source_generation_id"],
            changed_segments["robuxio-crypto:account-live"]["source_generation_id"],
        ),
        provider_live_lineage_path: (
            baseline_segments["robuxio-crypto:provider-live-before-funding"][
                "source_generation_id"
            ],
            changed_segments["robuxio-crypto:provider-live-before-funding"][
                "source_generation_id"
            ],
        ),
    }
    assert set(matrixport_generation_lineage) == {
        "epoch_hpr_receipts[0].source_generation_id",
        account_live_lineage_path,
        provider_live_lineage_path,
    }
    for (
        baseline_generation_id,
        changed_generation_id,
    ) in matrixport_generation_lineage.values():
        assert changed_generation_id == matrixport_generation_id
        assert changed_generation_id != baseline_generation_id

    changed_epoch_hpr["receipt"]["source_gap_policy_sha256"] = baseline_epoch_hpr[
        "receipt"
    ]["source_gap_policy_sha256"]
    changed_epoch_hpr["source_generation_id"] = baseline_epoch_hpr[
        "source_generation_id"
    ]
    for segment_id in (
        "robuxio-crypto:account-live",
        "robuxio-crypto:provider-live-before-funding",
    ):
        changed_segments[segment_id]["source_generation_id"] = baseline_segments[
            segment_id
        ]["source_generation_id"]
    assert coverage_with_baseline_policy == baseline_coverage


def test_layered_history_rejects_unproduced_matrixport_transform_fact(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    source = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )
    source["declared_gap_policy"]["return_transform"]["facts"].append(
        "FINANCING_LIABILITY"
    )
    _write_json(fixture["contract_path"], contract)

    with pytest.raises(TargetSeriesError, match="return transform capability differs"):
        _build(fixture)


def test_layered_history_does_not_admit_pre_live_p123_ai_without_product_version(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    returns_path = fixture["portfolio123_returns_path"]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "entity_id": "sim-ai",
                    "model_return": float(100 + index),
                    "period_date": observed,
                    "source": "synthetic",
                }
                for index, observed in enumerate(
                    ["2025-12-29", "2025-12-30", "2025-12-31", "2026-01-02"]
                )
            ]
        ),
        returns_path,
    )
    manifest_path = fixture["portfolio123_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "tables/returns_daily.parquet"
    )
    artifact.update(_artifact(returns_path, artifact["artifact_id"]))
    portfolio123_generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-portfolio123",
        fixture["as_of_date"],
        manifest["artifacts"],
    )
    _pointer(
        fixture["portfolio123_pointer_path"], portfolio123_generation_id, manifest_path
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
        "PORTFOLIO123",
    )
    _refresh_p123_eu_version(contract, portfolio123_generation_id)
    _append_p123_performance_receipt(
        contract, manifest_path, fixture["portfolio123_performance_path"]
    )
    _append_p123_ai_receipts(
        contract,
        manifest_path,
        returns_path,
        fixture["portfolio123_relationships_path"],
    )
    _write_json(fixture["contract_path"], contract)
    _refresh_materialized_segment_identities(fixture)

    artifacts, _manifest_content = _build(fixture)
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    assert not any(
        row["strategy_id"] == "p123-ai-factor" and row["evidence_layer"] == "BACKTEST"
        for row in segments
    )
    coverage = json.loads(artifacts["coverage.json"])
    resolution = coverage["correction_resolutions"]["p123-ai-backtest"]
    assert resolution["observed_end"] == "2026-01-02"
    assert resolution["status"] == (
        "DERIVED_NOT_ADMITTED_VERSION_UNRESOLVED_OR_MISMATCHED"
    )
    p123 = next(
        row for row in coverage["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    assert p123["modes"]["LIVE_PLUS_BACKTEST"] == {
        "reason": "REQUIRED_NOT_READY_VERSION_UNRESOLVED_OR_MISMATCHED",
        "status": "REQUIRED_NOT_READY",
    }


def test_layered_history_registers_optional_strategy_history_manifest(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    returns_path = tmp_path / "strategy-history-returns.csv"
    returns_content = _csv_bytes(
        target_series.CORRELATION_FIELDS_V2,
        [
            {
                "date": "2024-01-01",
                "evidence_layer": "BACKTEST",
                "return": "0.01",
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            },
            {
                "date": "2024-01-02",
                "evidence_layer": "BACKTEST",
                "return": "-0.005",
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            },
        ],
    )
    returns_path.write_bytes(returns_content)
    strategy_manifest = {
        "as_of_date": fixture["as_of_date"],
        "builder": "portfolio-strategy-history",
        "builder_version": "1.4.0",
        "history_dispositions": [
            {
                "history_status": (
                    "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_DERIVED_NOT_ADMITTED"
                ),
                "implementation_reconciliation_status": (
                    "BLOCKED_ADMIT_OR_DECLARE; SEPARATE_FROM_ACCOUNT_LIVE"
                ),
                "reason": "Synthetic v1.4 provider-live transform remains unadmitted.",
                "required_for_analysis": True,
                "series_id": "EU [P123]",
                "status": (
                    "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
                ),
                "strategy_version": "synthetic-p123-eu:v1",
            }
        ],
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(returns_content).hexdigest(),
            "size_bytes": len(returns_content),
        },
        "schema_version": 1,
        "series": [
            {
                "compatibility_basis": "SYNTHETIC_CURRENT_SPECIFICATION",
                "frequency": "DAILY_TRADING",
                "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
                "implementation_reconciliation_status": "UNRESOLVED",
                "observed_end": "2024-01-02",
                "observed_start": "2024-01-01",
                "required_for_analysis": True,
                "return_observations": 2,
                "series_id": "QSI",
                "strategy_version": "synthetic-qsi:v1",
            }
        ],
        "verification": "VERIFIED",
    }
    strategy_manifest["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(strategy_manifest)
    ).hexdigest()
    manifest_path = tmp_path / "strategy-history-manifest.json"
    _write_json(manifest_path, strategy_manifest)
    fixture["strategy_history_manifest_path"] = manifest_path
    fixture["strategy_history_returns_path"] = returns_path

    artifacts, manifest_content = _build(fixture)
    result = validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest_content,
        contract_path=fixture["contract_path"],
    )
    assert result["verification"] == "VALID"
    generation = json.loads(manifest_content)
    assert generation["source_manifests"]["strategy-history"] == (
        manifest_path.read_text(encoding="utf-8")
    )
    inputs = {row["input_id"]: row for row in generation["inputs"]}
    assert (
        inputs["strategy-history-manifest"]["generation_id"]
        == (strategy_manifest["bundle_id"])
    )
    assert inputs["strategy-history-returns"]["artifact_id"] == (
        "strategy-history/returns.csv"
    )

    optional_as_of_rewrite = copy.deepcopy(generation)
    optional_returns_input = next(
        row
        for row in optional_as_of_rewrite["inputs"]
        if row["input_id"] == "strategy-history-returns"
    )
    optional_returns_input["as_of_date"] = "1999-12-31"
    with pytest.raises(
        TargetSeriesError,
        match="input source authority differs: strategy-history-returns",
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(optional_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    missing_artifact_id = copy.deepcopy(strategy_manifest)
    missing_artifact_id["returns_artifact"].pop("artifact_id")
    missing_artifact_id.pop("bundle_id")
    missing_artifact_id["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(missing_artifact_id)
    ).hexdigest()
    _write_json(manifest_path, missing_artifact_id)
    with pytest.raises(TargetSeriesError, match="returns artifact keys differ"):
        _build(fixture)

    wrong_artifact_id = copy.deepcopy(strategy_manifest)
    wrong_artifact_id["returns_artifact"]["artifact_id"] = "other/returns.csv"
    wrong_artifact_id.pop("bundle_id")
    wrong_artifact_id["bundle_id"] = hashlib.sha256(
        canonical_json_bytes(wrong_artifact_id)
    ).hexdigest()
    _write_json(manifest_path, wrong_artifact_id)
    with pytest.raises(TargetSeriesError, match="returns artifact ID differs"):
        _build(fixture)

    invalid_bundle = dict(strategy_manifest)
    invalid_bundle["bundle_id"] = "0" * 64
    _write_json(manifest_path, invalid_bundle)
    invalid_artifacts, invalid_manifest = _build(fixture)
    with pytest.raises(TargetSeriesError, match="bundle identity differs"):
        validate_layered_history_generation(
            artifacts=invalid_artifacts,
            manifest_content=invalid_manifest,
            contract_path=fixture["contract_path"],
        )


def _qsi_revision_fixture(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    from test_portfolio_strategy_history import (
        _bundle_inputs,
        _qsi_current_archive,
        _qsi_price_extension,
        history,
    )
    import portfolio_strategy_backtests as backtests

    fixture = _history_fixture(tmp_path)
    fixture["as_of_date"] = "2026-09-17"
    raw = tmp_path / "synthetic-qsi-sources"
    raw.mkdir()
    inputs = _bundle_inputs(raw)
    seed = Path(
        history.write_bundle(
            raw / "seed",
            **inputs,
            as_of_date="2026-09-17",
            live_inceptions={"QSI": "2026-01-05"},
        )["destination"]
    )
    stage_root = raw / "staged"
    staged_seed = backtests.stage_candidate(seed, stage_root)
    seed_generation_path = (
        stage_root
        / "generations"
        / str(staged_seed["generation_id"])
        / "generation.json"
    )
    seed_manifest = json.loads((seed / "manifest.json").read_bytes())
    contract = json.loads(fixture["contract_path"].read_bytes())
    definitions = {row["series_id"]: row for row in seed_manifest["series"]}
    for strategy in contract["strategies"]:
        definition = definitions.get(strategy.get("history_series_id"))
        if definition:
            strategy["strategy_version"] = {
                "status": "EVIDENCED",
                "version_ref": definition["strategy_version"],
            }
            for source in contract["source_segments"]:
                refs = source["strategy_version"]["version_refs"]
                if strategy["strategy_id"] in refs:
                    refs[strategy["strategy_id"]] = definition["strategy_version"]
    _update_source_generation(
        contract, "strategy-backtests", seed_generation_path, "2026-09-17"
    )
    _write_json(fixture["contract_path"], contract)
    fixture.update(
        {
            "strategy_history_manifest_path": seed / "manifest.json",
            "strategy_history_returns_path": seed / "returns.csv",
            "strategy_backtests_generation_path": seed_generation_path,
        }
    )
    _refresh_materialized_segment_identities(fixture)
    current = raw / "qsi-current.zip"
    extension = raw / "qsi-extension.json"
    _qsi_current_archive(current, revised=True)
    _qsi_price_extension(extension, inputs["qsi_price_root"])
    refreshed = Path(
        history.write_qsi_refresh_bundle(
            raw / "refreshed",
            retained_manifest_path=seed / "manifest.json",
            retained_returns_path=seed / "returns.csv",
            qsi_subscription_archive=inputs["qsi_subscription_archive"],
            qsi_current_subscription_archive=current,
            qsi_appendix_workbook=inputs["qsi_appendix_workbook"],
            qsi_price_root=inputs["qsi_price_root"],
            price_extension_json=extension,
            as_of_date="2026-09-17",
        )["destination"]
    )
    staged = backtests.stage_candidate(refreshed, stage_root)
    refreshed_generation = (
        stage_root / "generations" / str(staged["generation_id"]) / "generation.json"
    )
    return fixture, refreshed, refreshed_generation


def test_qsi_revision_null_proof_cannot_be_added_to_legacy_generation(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    manifest["qsi_source_revision"] = None
    with pytest.raises(TargetSeriesError, match="QSI source revision proof differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_qsi_revision_layered_producer_reader_preserve_live_and_other_histories(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture, refreshed, generation = _qsi_revision_fixture(tmp_path)
    previous, _ = _build(fixture)
    contract_bytes = fixture["contract_path"].read_bytes()
    fixture.update(
        {
            "strategy_history_manifest_path": refreshed / "manifest.json",
            "strategy_history_returns_path": refreshed / "returns.csv",
            "strategy_backtests_generation_path": generation,
        }
    )
    artifacts, manifest_content = _build(fixture)
    assert artifacts["live-only.csv"] == previous["live-only.csv"]
    schema = json.loads(
        (ROOT / "schemas/portfolio/layered-history-generation.schema.json").read_bytes()
    )
    Draft202012Validator(schema).validate(json.loads(manifest_content))

    def non_qsi(content: bytes) -> list[dict[str, str]]:
        return [
            row
            for row in csv.DictReader(io.StringIO(content.decode()))
            if row["series_id"] != "QSI"
        ]

    assert non_qsi(artifacts["live-plus-backtest.csv"]) == non_qsi(
        previous["live-plus-backtest.csv"]
    )
    coverage = json.loads(artifacts["coverage.json"])
    qsi = next(row for row in coverage["strategies"] if row["strategy_id"] == "qsi")
    assert qsi["modes"]["LIVE_PLUS_BACKTEST"]["status"] == "AVAILABLE"
    assert (
        qsi["source_history_revision"]["observation_strategy_version"]
        != qsi["strategy_version"]["product_version_ref"]
    )
    assert (
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=manifest_content,
            contract_path=fixture["contract_path"],
        )["verification"]
        == "VALID"
    )
    destination = write_layered_history_generation(
        tmp_path / "layered", artifacts, manifest_content
    )
    assert (
        verify_layered_history_generation(destination, fixture["contract_path"])[
            "verification"
        ]
        == "VALID"
    )
    assert fixture["contract_path"].read_bytes() == contract_bytes


@pytest.mark.parametrize("mutation", ["qsi", "other", "proof", "coverage"])
def test_qsi_revision_reader_rejects_rehashed_layered_tampering(
    tmp_path: Path,
    synthetic_history_authority: None,
    mutation: str,
) -> None:
    fixture, refreshed, generation = _qsi_revision_fixture(tmp_path)
    fixture.update(
        {
            "strategy_history_manifest_path": refreshed / "manifest.json",
            "strategy_history_returns_path": refreshed / "returns.csv",
            "strategy_backtests_generation_path": generation,
        }
    )
    artifacts, manifest_content = _build(fixture)
    manifest = json.loads(manifest_content)
    if mutation in {"qsi", "other"}:
        rows = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode())))
        next(
            row
            for row in rows
            if row["source_id"] == "strategy-backtests"
            and (row["series_id"] == "QSI") == (mutation == "qsi")
        )["return"] = "0.8"
        _replace_output(
            artifacts, manifest, "segments.csv", target_series._segment_view_bytes(rows)
        )
    elif mutation == "proof":
        manifest["qsi_source_revision"]["accepted_generation_manifest_sha256"] = (
            "0" * 64
        )
    else:
        coverage = json.loads(artifacts["coverage.json"])
        qsi = next(row for row in coverage["strategies"] if row["strategy_id"] == "qsi")
        qsi["source_history_revision"]["observation_strategy_version"] = "FORGED"
        _replace_output(
            artifacts, manifest, "coverage.json", canonical_json_bytes(coverage)
        )
    with pytest.raises(TargetSeriesError, match="QSI source revision"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_qsi_revision_wrong_seed_and_missing_receipt_cannot_admit(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    import portfolio_strategy_backtests as backtests

    fixture, refreshed, generation = _qsi_revision_fixture(tmp_path)
    fixture.update(
        {
            "strategy_history_manifest_path": refreshed / "manifest.json",
            "strategy_history_returns_path": refreshed / "returns.csv",
            "strategy_backtests_generation_path": generation,
        }
    )
    contract = json.loads(fixture["contract_path"].read_bytes())
    seed = next(
        row
        for row in contract["source_segments"]
        if row["source_id"] == "strategy-backtests"
    )
    seed["current_generation"]["sha256"] = "0" * 64
    _write_json(fixture["contract_path"], contract)
    with pytest.raises(TargetSeriesError, match="accepted contract seed"):
        _build(fixture)
    bundle = json.loads((refreshed / "manifest.json").read_bytes())
    bundle.pop("qsi_source_revision")
    bundle.pop("bundle_id")
    bundle["bundle_id"] = hashlib.sha256(canonical_json_bytes(bundle)).hexdigest()
    stripped = tmp_path / str(bundle["bundle_id"])
    stripped.mkdir()
    (stripped / "manifest.json").write_bytes(canonical_json_bytes(bundle))
    (stripped / "returns.csv").write_bytes((refreshed / "returns.csv").read_bytes())
    staged = backtests.stage_candidate(stripped, tmp_path / "stripped-stage")
    fixture.update(
        {
            "strategy_history_manifest_path": stripped / "manifest.json",
            "strategy_history_returns_path": stripped / "returns.csv",
            "strategy_backtests_generation_path": tmp_path
            / "stripped-stage"
            / "generations"
            / str(staged["generation_id"])
            / "generation.json",
        }
    )
    artifacts, _ = _build(fixture)
    qsi = next(
        row
        for row in json.loads(artifacts["coverage.json"])["strategies"]
        if row["strategy_id"] == "qsi"
    )
    assert qsi["modes"]["LIVE_PLUS_BACKTEST"]["status"] == "REQUIRED_NOT_READY"


def test_layered_history_rejects_coherent_receipt_and_semantic_rewrites(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)

    noncanonical_manifest = json.dumps(json.loads(manifest_content), indent=2).encode(
        "utf-8"
    )
    with pytest.raises(TargetSeriesError, match="generation manifest is not canonical"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=noncanonical_manifest,
            contract_path=fixture["contract_path"],
        )

    pointer_rewrite = json.loads(manifest_content)
    current_input = next(
        row
        for row in pointer_rewrite["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    current_input["sha256"] = "0" * 64
    with pytest.raises(TargetSeriesError, match="current pointer differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(pointer_rewrite),
            contract_path=fixture["contract_path"],
        )

    inventory_rewrite = json.loads(manifest_content)
    rewritten_pointer = json.loads(
        inventory_rewrite["source_pointers"]["provider-ledger"]
    )
    rewritten_pointer["artifact_inventory_sha256"] = "0" * 64
    rewritten_pointer_content = canonical_json_bytes(rewritten_pointer)
    inventory_rewrite["source_pointers"]["provider-ledger"] = (
        rewritten_pointer_content.decode("utf-8")
    )
    inventory_input = next(
        row
        for row in inventory_rewrite["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    inventory_input["sha256"] = hashlib.sha256(rewritten_pointer_content).hexdigest()
    inventory_input["size_bytes"] = len(rewritten_pointer_content)
    with pytest.raises(TargetSeriesError, match="current pointer inventory differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(inventory_rewrite),
            contract_path=fixture["contract_path"],
        )

    pointer_downgrade = json.loads(manifest_content)
    downgraded_pointer = json.loads(
        pointer_downgrade["source_pointers"]["provider-ledger"]
    )
    downgraded_pointer["pointer_schema_version"] = 1
    downgraded_pointer.pop("artifact_inventory_sha256")
    downgraded_pointer_content = canonical_json_bytes(downgraded_pointer)
    pointer_downgrade["source_pointers"]["provider-ledger"] = (
        downgraded_pointer_content.decode("utf-8")
    )
    downgraded_input = next(
        row
        for row in pointer_downgrade["inputs"]
        if row["input_id"] == "provider-ledger-current"
    )
    downgraded_input["sha256"] = hashlib.sha256(downgraded_pointer_content).hexdigest()
    downgraded_input["size_bytes"] = len(downgraded_pointer_content)
    with pytest.raises(TargetSeriesError, match="source pointer contract differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(pointer_downgrade),
            contract_path=fixture["contract_path"],
        )

    input_order_rewrite = json.loads(manifest_content)
    input_order_rewrite["inputs"][0], input_order_rewrite["inputs"][1] = (
        input_order_rewrite["inputs"][1],
        input_order_rewrite["inputs"][0],
    )
    with pytest.raises(TargetSeriesError, match="input ordering differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_order_rewrite),
            contract_path=fixture["contract_path"],
        )

    input_contract_rewrite = json.loads(manifest_content)
    matrixport_nav_input = next(
        row
        for row in input_contract_rewrite["inputs"]
        if row["input_id"] == "matrixport-nav"
    )
    matrixport_nav_input.update(
        {
            "artifact_id": "fabricated/nav.csv",
            "artifact_role": "UNCONTROLLED_ARTIFACT",
            "sha256": "0" * 64,
            "size_bytes": 1,
        }
    )
    with pytest.raises(
        TargetSeriesError, match="input contract differs: matrixport-nav"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_contract_rewrite),
            contract_path=fixture["contract_path"],
        )

    input_as_of_rewrite = json.loads(manifest_content)
    matrixport_nav_input = next(
        row
        for row in input_as_of_rewrite["inputs"]
        if row["input_id"] == "matrixport-nav"
    )
    matrixport_nav_input["as_of_date"] = "1999-12-31"
    with pytest.raises(
        TargetSeriesError, match="input source authority differs: matrixport-nav"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(input_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    product_as_of_rewrite = json.loads(manifest_content)
    product_input = next(
        row
        for row in product_as_of_rewrite["inputs"]
        if row["input_id"] == "financial-mvp-contract"
    )
    product_input["as_of_date"] = "1999-12-31"
    with pytest.raises(TargetSeriesError, match="product input provenance differs"):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=_reidentify_manifest(product_as_of_rewrite),
            contract_path=fixture["contract_path"],
        )

    reordered_artifacts = dict(artifacts)
    reordered_manifest = json.loads(manifest_content)
    reordered_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    reordered_segments[0], reordered_segments[1] = (
        reordered_segments[1],
        reordered_segments[0],
    )
    _replace_output(
        reordered_artifacts,
        reordered_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, reordered_segments),
    )
    with pytest.raises(TargetSeriesError, match="segment ordering differs"):
        validate_layered_history_generation(
            artifacts=reordered_artifacts,
            manifest_content=_reidentify_manifest(reordered_manifest),
            contract_path=fixture["contract_path"],
        )

    reordered_coverage_artifacts = dict(artifacts)
    reordered_coverage_manifest = json.loads(manifest_content)
    reordered_coverage = json.loads(artifacts["coverage.json"])
    reordered_coverage["strategies"][0], reordered_coverage["strategies"][1] = (
        reordered_coverage["strategies"][1],
        reordered_coverage["strategies"][0],
    )
    _replace_output(
        reordered_coverage_artifacts,
        reordered_coverage_manifest,
        "coverage.json",
        canonical_json_bytes(reordered_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=reordered_coverage_artifacts,
            manifest_content=_reidentify_manifest(reordered_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    nested_order_artifacts = dict(artifacts)
    nested_order_manifest = json.loads(manifest_content)
    nested_order_coverage = json.loads(artifacts["coverage.json"])
    robuxio_coverage = next(
        row
        for row in nested_order_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    robuxio_coverage["segments"][0], robuxio_coverage["segments"][1] = (
        robuxio_coverage["segments"][1],
        robuxio_coverage["segments"][0],
    )
    _replace_output(
        nested_order_artifacts,
        nested_order_manifest,
        "coverage.json",
        canonical_json_bytes(nested_order_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=nested_order_artifacts,
            manifest_content=_reidentify_manifest(nested_order_manifest),
            contract_path=fixture["contract_path"],
        )

    duplicate_summary_artifacts = dict(artifacts)
    duplicate_summary_manifest = json.loads(manifest_content)
    duplicate_summary_coverage = json.loads(artifacts["coverage.json"])
    robuxio_coverage = next(
        row
        for row in duplicate_summary_coverage["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    robuxio_coverage["segments"].insert(0, dict(robuxio_coverage["segments"][0]))
    _replace_output(
        duplicate_summary_artifacts,
        duplicate_summary_manifest,
        "coverage.json",
        canonical_json_bytes(duplicate_summary_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment coverage ordering differs"):
        validate_layered_history_generation(
            artifacts=duplicate_summary_artifacts,
            manifest_content=_reidentify_manifest(duplicate_summary_manifest),
            contract_path=fixture["contract_path"],
        )

    noncanonical_coverage_artifacts = dict(artifacts)
    noncanonical_coverage_manifest = json.loads(manifest_content)
    noncanonical_coverage = json.dumps(
        json.loads(artifacts["coverage.json"]), indent=2
    ).encode("utf-8")
    _replace_output(
        noncanonical_coverage_artifacts,
        noncanonical_coverage_manifest,
        "coverage.json",
        noncanonical_coverage,
    )
    with pytest.raises(TargetSeriesError, match="coverage is not canonical"):
        validate_layered_history_generation(
            artifacts=noncanonical_coverage_artifacts,
            manifest_content=_reidentify_manifest(noncanonical_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    extra_coverage_artifacts = dict(artifacts)
    extra_coverage_manifest = json.loads(manifest_content)
    extra_coverage = json.loads(artifacts["coverage.json"])
    extra_coverage["uncontrolled_policy"] = "ACCEPT"
    _replace_output(
        extra_coverage_artifacts,
        extra_coverage_manifest,
        "coverage.json",
        canonical_json_bytes(extra_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage keys differ"):
        validate_layered_history_generation(
            artifacts=extra_coverage_artifacts,
            manifest_content=_reidentify_manifest(extra_coverage_manifest),
            contract_path=fixture["contract_path"],
        )

    metadata_artifacts = dict(artifacts)
    metadata_manifest = json.loads(manifest_content)
    metadata_coverage = json.loads(artifacts["coverage.json"])
    metadata_coverage["builder_version"] = "9.9.9"
    _replace_output(
        metadata_artifacts,
        metadata_manifest,
        "coverage.json",
        canonical_json_bytes(metadata_coverage),
    )
    with pytest.raises(TargetSeriesError, match="coverage metadata differs"):
        validate_layered_history_generation(
            artifacts=metadata_artifacts,
            manifest_content=_reidentify_manifest(metadata_manifest),
            contract_path=fixture["contract_path"],
        )

    nested_extra_artifacts = dict(artifacts)
    nested_extra_manifest = json.loads(manifest_content)
    nested_extra_coverage = json.loads(artifacts["coverage.json"])
    nested_extra_coverage["strategies"][0]["uncontrolled_policy"] = "ACCEPT"
    _replace_output(
        nested_extra_artifacts,
        nested_extra_manifest,
        "coverage.json",
        canonical_json_bytes(nested_extra_coverage),
    )
    with pytest.raises(TargetSeriesError, match="strategy coverage keys differ"):
        validate_layered_history_generation(
            artifacts=nested_extra_artifacts,
            manifest_content=_reidentify_manifest(nested_extra_manifest),
            contract_path=fixture["contract_path"],
        )

    mode_policy_artifacts = dict(artifacts)
    mode_policy_manifest = json.loads(manifest_content)
    mode_policy_coverage = json.loads(artifacts["coverage.json"])
    mode_policy_coverage["mode_policy"]["comparison_layers_in_modes"] = True
    _replace_output(
        mode_policy_artifacts,
        mode_policy_manifest,
        "coverage.json",
        canonical_json_bytes(mode_policy_coverage),
    )
    with pytest.raises(TargetSeriesError, match="mode policy differs"):
        validate_layered_history_generation(
            artifacts=mode_policy_artifacts,
            manifest_content=_reidentify_manifest(mode_policy_manifest),
            contract_path=fixture["contract_path"],
        )

    correction_set_artifacts = dict(artifacts)
    correction_set_manifest = json.loads(manifest_content)
    correction_set_coverage = json.loads(artifacts["coverage.json"])
    correction_set_coverage["correction_resolutions"]["uncontrolled"] = {}
    _replace_output(
        correction_set_artifacts,
        correction_set_manifest,
        "coverage.json",
        canonical_json_bytes(correction_set_coverage),
    )
    with pytest.raises(TargetSeriesError, match="correction resolutions are absent"):
        validate_layered_history_generation(
            artifacts=correction_set_artifacts,
            manifest_content=_reidentify_manifest(correction_set_manifest),
            contract_path=fixture["contract_path"],
        )

    p123_proof_artifacts = dict(artifacts)
    p123_proof_manifest = json.loads(manifest_content)
    p123_proof_coverage = json.loads(artifacts["coverage.json"])
    p123_proof = p123_proof_coverage["correction_resolutions"]["p123-eu-provider-live"]
    p123_proof["raw_level_observations"] = 8
    p123_proof["return_observations"] = 7
    p123_proof["equity_overlap_observations"] = 8
    _replace_output(
        p123_proof_artifacts,
        p123_proof_manifest,
        "coverage.json",
        canonical_json_bytes(p123_proof_coverage),
    )
    with pytest.raises(TargetSeriesError, match="return transform disposition differs"):
        validate_layered_history_generation(
            artifacts=p123_proof_artifacts,
            manifest_content=_reidentify_manifest(p123_proof_manifest),
            contract_path=fixture["contract_path"],
        )

    p123_ai_artifacts = dict(artifacts)
    p123_ai_manifest = json.loads(manifest_content)
    p123_ai_coverage = json.loads(artifacts["coverage.json"])
    p123_ai_proof = p123_ai_coverage["correction_resolutions"]["p123-ai-backtest"]
    p123_ai_proof["observed_start"] = "2025-12-31"
    p123_ai_proof["index_observations"] = 3
    p123_ai_proof["return_observations"] = 2
    _replace_output(
        p123_ai_artifacts,
        p123_ai_manifest,
        "coverage.json",
        canonical_json_bytes(p123_ai_coverage),
    )
    with pytest.raises(TargetSeriesError, match="AI backtest disposition differs"):
        validate_layered_history_generation(
            artifacts=p123_ai_artifacts,
            manifest_content=_reidentify_manifest(p123_ai_manifest),
            contract_path=fixture["contract_path"],
        )

    volatis_artifacts = dict(artifacts)
    volatis_manifest = json.loads(manifest_content)
    volatis_coverage = json.loads(artifacts["coverage.json"])
    volatis_proof = volatis_coverage["correction_resolutions"]["volatis-account-live"]
    volatis_proof.update(
        {
            "observed_end": "2024-10-23",
            "observed_start": "2024-10-17",
            "source_first_positive_value_date": "2024-10-16",
            "status": "OWNER_APPROVED_NOT_APPLICABLE",
        }
    )
    _replace_output(
        volatis_artifacts,
        volatis_manifest,
        "coverage.json",
        canonical_json_bytes(volatis_coverage),
    )
    with pytest.raises(
        TargetSeriesError, match="Volatis account-live resolution differs"
    ):
        validate_layered_history_generation(
            artifacts=volatis_artifacts,
            manifest_content=_reidentify_manifest(volatis_manifest),
            contract_path=fixture["contract_path"],
        )

    return_rewrite_artifacts = dict(artifacts)
    return_rewrite_manifest = json.loads(manifest_content)
    return_rewrite_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    rewritten_row = next(
        row
        for row in return_rewrite_segments
        if row["segment_id"] == "qsi:account-live"
    )
    rewritten_row["return"] = "0.123456789"
    _replace_output(
        return_rewrite_artifacts,
        return_rewrite_manifest,
        "segments.csv",
        _csv_bytes(
            target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
            return_rewrite_segments,
        ),
    )
    for name in ("returns.csv", "live-only.csv", "live-plus-backtest.csv"):
        projection = list(csv.DictReader(io.StringIO(artifacts[name].decode("utf-8"))))
        matching = next(
            row
            for row in projection
            if (
                row["series_id"],
                row["date"],
                row["evidence_layer"],
                row["strategy_version"],
            )
            == (
                rewritten_row["series_id"],
                rewritten_row["date"],
                rewritten_row["evidence_layer"],
                rewritten_row["strategy_version"],
            )
        )
        matching["return"] = rewritten_row["return"]
        _replace_output(
            return_rewrite_artifacts,
            return_rewrite_manifest,
            name,
            _csv_bytes(target_series.CORRELATION_FIELDS_V2, projection),
        )
    return_rewrite_coverage = json.loads(artifacts["coverage.json"])
    rewritten_summary = next(
        summary
        for strategy in return_rewrite_coverage["strategies"]
        if strategy["strategy_id"] == "qsi"
        for summary in strategy["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    rewritten_summary.update(
        target_series._row_identity(
            [
                row
                for row in return_rewrite_segments
                if row["segment_id"] == "qsi:account-live"
            ]
        )
    )
    _replace_output(
        return_rewrite_artifacts,
        return_rewrite_manifest,
        "coverage.json",
        canonical_json_bytes(return_rewrite_coverage),
    )
    with pytest.raises(TargetSeriesError, match="values differ from product authority"):
        validate_layered_history_generation(
            artifacts=return_rewrite_artifacts,
            manifest_content=_reidentify_manifest(return_rewrite_manifest),
            contract_path=fixture["contract_path"],
        )

    baseline_contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    semantic_bypass = copy.deepcopy(baseline_contract)
    p123_provider = next(
        row
        for row in semantic_bypass["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    p123_provider["readiness"] = "READY"
    p123_provider["blockers"] = []
    _write_json(fixture["contract_path"], semantic_bypass)
    with pytest.raises(
        TargetSeriesError, match="layered history product contract identity differs"
    ):
        validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=manifest_content,
            contract_path=fixture["contract_path"],
        )
    _write_json(fixture["contract_path"], baseline_contract)

    reason_artifacts = dict(artifacts)
    reason_manifest = json.loads(manifest_content)
    reason_coverage = json.loads(reason_artifacts["coverage.json"])
    qsi_coverage = next(
        row for row in reason_coverage["strategies"] if row["strategy_id"] == "qsi"
    )
    qsi_coverage["modes"]["LIVE_PLUS_BACKTEST"]["reason"] = (
        "OWNER_APPROVED_NOT_APPLICABLE"
    )
    _replace_output(
        reason_artifacts,
        reason_manifest,
        "coverage.json",
        canonical_json_bytes(reason_coverage),
    )
    with pytest.raises(TargetSeriesError, match="mode reason differs"):
        validate_layered_history_generation(
            artifacts=reason_artifacts,
            manifest_content=_reidentify_manifest(reason_manifest),
            contract_path=fixture["contract_path"],
        )

    series_artifacts = dict(artifacts)
    series_manifest = json.loads(manifest_content)
    series_coverage = json.loads(series_artifacts["coverage.json"])
    composer_coverage = next(
        row
        for row in series_coverage["strategies"]
        if row["strategy_id"] == "composer-holy-grail"
    )
    composer_coverage["series_id"] = "SYNTHETIC_UNCONTROLLED_SERIES"
    _replace_output(
        series_artifacts,
        series_manifest,
        "coverage.json",
        canonical_json_bytes(series_coverage),
    )
    with pytest.raises(TargetSeriesError, match="series identity differs"):
        validate_layered_history_generation(
            artifacts=series_artifacts,
            manifest_content=_reidentify_manifest(series_manifest),
            contract_path=fixture["contract_path"],
        )

    relabelled_artifacts = dict(artifacts)
    relabelled_manifest = json.loads(manifest_content)
    relabelled_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    for row in relabelled_segments:
        if (
            row["strategy_id"] == "qsi"
            and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
        ):
            row.update(
                {
                    "calendar": "UNAPPROVED_CALENDAR",
                    "frequency": "UNAPPROVED_FREQUENCY",
                    "return_basis": "UNAPPROVED_BASIS",
                    "return_type": "UNAPPROVED_RETURN_TYPE",
                    "source_generation_id": "0" * 64,
                    "source_id": "unapproved-source",
                }
            )
    _replace_output(
        relabelled_artifacts,
        relabelled_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, relabelled_segments),
    )
    relabelled_coverage = json.loads(artifacts["coverage.json"])
    qsi_summary = next(
        summary
        for row in relabelled_coverage["strategies"]
        if row["strategy_id"] == "qsi"
        for summary in row["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    qsi_summary.update(
        {
            "calendar": "UNAPPROVED_CALENDAR",
            "frequency": "UNAPPROVED_FREQUENCY",
            "source_generation_id": "0" * 64,
            "source_id": "unapproved-source",
        }
    )
    _replace_output(
        relabelled_artifacts,
        relabelled_manifest,
        "coverage.json",
        canonical_json_bytes(relabelled_coverage),
    )
    with pytest.raises(TargetSeriesError, match="segment source receipt differs"):
        validate_layered_history_generation(
            artifacts=relabelled_artifacts,
            manifest_content=_reidentify_manifest(relabelled_manifest),
            contract_path=fixture["contract_path"],
        )

    gap_artifacts = dict(artifacts)
    gap_manifest = json.loads(manifest_content)
    gap_segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    qsi_dates = [
        row["date"]
        for row in gap_segments
        if row["strategy_id"] == "qsi"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    removed_date = qsi_dates[len(qsi_dates) // 2]
    gap_segments = [
        row
        for row in gap_segments
        if not (row["strategy_id"] == "qsi" and row["date"] == removed_date)
    ]
    _replace_output(
        gap_artifacts,
        gap_manifest,
        "segments.csv",
        _csv_bytes(target_series.LAYERED_HISTORY_SEGMENT_FIELDS, gap_segments),
    )
    for name in ("returns.csv", "live-only.csv", "live-plus-backtest.csv"):
        rows = list(csv.DictReader(io.StringIO(artifacts[name].decode("utf-8"))))
        rows = [
            row
            for row in rows
            if not (row["series_id"] == "QSI" and row["date"] == removed_date)
        ]
        _replace_output(
            gap_artifacts,
            gap_manifest,
            name,
            _csv_bytes(target_series.CORRELATION_FIELDS_V2, rows),
        )
    gap_coverage = json.loads(artifacts["coverage.json"])
    qsi_summary = next(
        summary
        for row in gap_coverage["strategies"]
        if row["strategy_id"] == "qsi"
        for summary in row["segments"]
        if summary["segment_id"] == "qsi:account-live"
    )
    selected_qsi = [
        row for row in gap_segments if row["segment_id"] == "qsi:account-live"
    ]
    qsi_summary.update(target_series._row_identity(selected_qsi))
    qsi_summary["gap_disposition"] = target_series._gap_disposition(selected_qsi)
    qsi_strategy = next(
        row for row in gap_coverage["strategies"] if row["strategy_id"] == "qsi"
    )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        qsi_strategy["modes"][mode] = {
            "reason": "REQUIRED_ACCOUNT_LIVE_INTERIOR_GAP",
            "status": "REQUIRED_NOT_READY",
        }
    live_not_ready = set(
        gap_manifest["mode_status"]["LIVE_ONLY"]["required_not_ready_strategy_ids"]
    )
    live_not_ready.add("qsi")
    gap_manifest["mode_status"]["LIVE_ONLY"] = {
        "required_not_ready_strategy_ids": sorted(live_not_ready),
        "status": "REQUIRED_NOT_READY",
    }
    _replace_output(
        gap_artifacts,
        gap_manifest,
        "coverage.json",
        canonical_json_bytes(gap_coverage),
    )
    with pytest.raises(TargetSeriesError, match="product authority"):
        validate_layered_history_generation(
            artifacts=gap_artifacts,
            manifest_content=_reidentify_manifest(gap_manifest),
            contract_path=fixture["contract_path"],
        )


def test_layered_history_rejects_conflicting_p123_performance_capture(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    performance_path = fixture["portfolio123_performance_path"]
    rows = pq.read_table(performance_path).to_pylist()
    conflicting = dict(next(row for row in rows if row["field"] == "data"))
    conflicting["value"] = "[]"
    pq.write_table(pa.Table.from_pylist([*rows, conflicting]), performance_path)

    manifest_path = fixture["portfolio123_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "tables/har_managed_performance.parquet"
    )
    artifact.update(_artifact(performance_path, artifact["artifact_id"]))
    portfolio123_generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-portfolio123",
        fixture["as_of_date"],
        manifest["artifacts"],
    )
    _pointer(
        fixture["portfolio123_pointer_path"],
        portfolio123_generation_id,
        manifest_path,
    )
    contract = json.loads(fixture["contract_path"].read_text(encoding="utf-8"))
    _update_source_generation(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
    )
    _update_correction_generations(
        contract,
        "portfolio123-model-generation",
        manifest_path,
        fixture["as_of_date"],
        "PORTFOLIO123",
    )
    _refresh_p123_eu_version(contract, portfolio123_generation_id)
    _append_p123_performance_receipt(contract, manifest_path, performance_path)
    _append_p123_ai_receipts(
        contract,
        manifest_path,
        fixture["portfolio123_returns_path"],
        fixture["portfolio123_relationships_path"],
    )
    _write_json(fixture["contract_path"], contract)

    with pytest.raises(TargetSeriesError, match="conflicting captures"):
        _build(fixture)


def test_layered_history_rejects_relabel_and_admitted_overlap(
    tmp_path: Path,
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    artifacts, manifest_content = _build(fixture)
    segments = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    provider = next(
        row
        for row in segments
        if row["strategy_id"] == "robuxio-crypto"
        and row["evidence_layer"] == "PROVIDER_LIVE"
    )
    provider["evidence_layer"] = "ACCOUNT_LIVE"
    provider["mode_admission"] = "INCLUDE_SELECTED_LIVE"
    relabelled = dict(artifacts)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(segments)
    relabelled["segments.csv"] = buffer.getvalue().encode("utf-8")
    manifest = json.loads(manifest_content)
    manifest["outputs"]["segments.csv"]["sha256"] = hashlib.sha256(
        relabelled["segments.csv"]
    ).hexdigest()
    manifest["outputs"]["segments.csv"]["size_bytes"] = len(relabelled["segments.csv"])
    with pytest.raises(TargetSeriesError):
        validate_layered_history_generation(
            artifacts=relabelled,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )

    overlapping = list(
        csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    )
    p123_live = next(
        row
        for row in overlapping
        if row["strategy_id"] == "p123-ai-factor"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    )
    admitted_overlap = dict(p123_live)
    admitted_overlap.update(
        {
            "evidence_layer": "BACKTEST",
            "mode_admission": "INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
            "return_basis": "MODEL",
            "segment_id": "p123-ai-factor:backtest",
            "source_generation_id": "4" * 64,
            "source_id": "strategy-backtests",
        }
    )
    overlapping.append(admitted_overlap)
    overlapping.sort(
        key=lambda row: (
            row["strategy_id"],
            row["date"],
            row["evidence_layer"],
            row["segment_id"],
        )
    )
    overlap_artifacts = dict(artifacts)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=target_series.LAYERED_HISTORY_SEGMENT_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(overlapping)
    overlap_content = buffer.getvalue().encode("utf-8")
    # Even a coherent local rehash cannot make overlapping backtest/live rows admissible.
    manifest = json.loads(manifest_content)
    _replace_output(overlap_artifacts, manifest, "segments.csv", overlap_content)
    with pytest.raises(TargetSeriesError):
        validate_layered_history_generation(
            artifacts=overlap_artifacts,
            manifest_content=_reidentify_manifest(manifest),
            contract_path=fixture["contract_path"],
        )


def test_materialized_backtests_bind_each_actual_source_generation() -> None:
    """Portfolio123 and external history may coexist with distinct receipts."""

    common = {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "date": "2024-01-02",
        "evidence_layer": "BACKTEST",
        "frequency": "DAILY",
        "mode_admission": "INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
        "return": "0.01",
        "return_basis": "MODEL",
        "return_type": "TOTAL",
    }
    rows = [
        {
            **common,
            "segment_id": "p123-ai-factor:backtest",
            "series_id": "AI Factor [P123]",
            "source_generation_id": "1" * 64,
            "source_id": "portfolio123-model-generation",
            "strategy_id": "p123-ai-factor",
            "strategy_version": "ai:v1",
        },
        {
            **common,
            "segment_id": "qsi:backtest",
            "series_id": "QSI",
            "source_generation_id": "2" * 64,
            "source_id": "strategy-backtests",
            "strategy_id": "qsi",
            "strategy_version": "qsi:v1",
        },
    ]
    sources = {
        "portfolio123-model-generation": {
            "current_generation": {"generation_id": "1" * 64, "sha256": "a" * 64}
        },
        "strategy-backtests": {
            "current_generation": {"generation_id": "2" * 64, "sha256": "b" * 64}
        },
    }

    identities = target_series._materialized_segment_identities(rows, sources)
    assert {
        (row["segment_id"], row["source_id"], row["source_generation_id"])
        for row in identities
    } == {
        (
            "p123-ai-factor:backtest",
            "portfolio123-model-generation",
            "1" * 64,
        ),
        ("qsi:backtest", "strategy-backtests", "2" * 64),
    }

    forged = [dict(row) for row in rows]
    forged[0]["source_generation_id"] = "3" * 64
    with pytest.raises(TargetSeriesError, match="source receipt differs"):
        target_series._materialized_segment_identities(forged, sources)


def test_layered_history_cli_builds_verifies_and_requires_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    synthetic_history_authority: None,
) -> None:
    fixture = _history_fixture(tmp_path)
    output_root = tmp_path / "cli-candidate"
    path_flags = {
        "--financial-mvp-contract": "contract_path",
        "--provider-ledger-current": "provider_ledger_pointer_path",
        "--provider-ledger-manifest": "provider_ledger_manifest_path",
        "--provider-ledger-catalog": "provider_ledger_catalog_path",
        "--matrixport-current": "matrixport_pointer_path",
        "--manifest": "matrixport_manifest_path",
        "--nav": "matrixport_nav_path",
        "--holdings": "matrixport_holdings_path",
        "--daily-holdings": "matrixport_daily_holdings_path",
        "--subscriptions": "matrixport_subscription_path",
        "--quantbase-current": "quantbase_pointer_path",
        "--quantbase-manifest": "quantbase_manifest_path",
        "--quantbase-history": "quantbase_history_path",
        "--quantbase-activity": "quantbase_activity_path",
        "--portfolio123-current": "portfolio123_pointer_path",
        "--portfolio123-manifest": "portfolio123_manifest_path",
        "--portfolio123-metadata": "portfolio123_metadata_path",
        "--portfolio123-performance": "portfolio123_performance_path",
        "--portfolio123-equity": "portfolio123_equity_path",
        "--portfolio123-returns": "portfolio123_returns_path",
        "--portfolio123-relationships": "portfolio123_relationships_path",
    }
    argv = [
        "portfolio_target_series.py",
        "--layered-history-generation",
        "--repository-sha",
        target_series._trusted_layered_history_code_identity()[0],
        "--as-of-date",
        fixture["as_of_date"],
        "--product-id",
        fixture["matrixport_product_id"],
        "--quantbase-fund-id",
        fixture["quantbase_fund_id"],
        "--output-root",
        str(output_root),
    ]
    for flag, key in path_flags.items():
        argv.extend((flag, str(fixture[key])))
    monkeypatch.setattr(sys, "argv", argv)
    assert target_series.main() == 0
    build_result = json.loads(capsys.readouterr().out)
    generation_root = output_root / build_result["generation_id"]
    assert generation_root.is_dir()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
        ],
    )
    assert target_series.main() == 0
    verify_result = json.loads(capsys.readouterr().out)
    assert verify_result["verification"] == "VALID"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--assurance-profile",
            "DEVELOPMENT",
            "--evaluated-on",
            "2026-01-14",
        ],
    )
    assert target_series.main() == 0
    assurance_result = json.loads(capsys.readouterr().out)
    assert assurance_result["assurance"] == {
        "evaluated_on": "2026-01-14",
        "profile": "DEVELOPMENT",
        "profile_policy": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "receipt_age_days": 4,
        "receipt_as_of": "2026-01-10",
        "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
    }

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--assurance-profile",
            "RELEASE_QC",
            "--evaluated-on",
            "2026-01-11",
        ],
    )
    assert target_series.main() == 0
    release_result = json.loads(capsys.readouterr().out)
    assert release_result["assurance"] == {
        "evaluated_on": "2026-01-11",
        "profile": "RELEASE_QC",
        "profile_policy": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
        "receipt_age_days": 1,
        "receipt_as_of": "2026-01-10",
        "status": "WARN_VERIFIED_IMMUTABLE_RECEIPT_REUSE",
    }

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_target_series.py",
            "--verify-layered-history",
            str(generation_root),
            "--financial-mvp-contract",
            str(fixture["contract_path"]),
            "--require-mode",
            "LIVE_PLUS_BACKTEST",
        ],
    )
    assert target_series.main() == 2
    assert "required history mode is not ready" in capsys.readouterr().err

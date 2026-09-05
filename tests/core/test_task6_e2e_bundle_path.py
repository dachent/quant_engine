"""End-to-end regression for the verified epoch-manifest bundle seam."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import date
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
    stage_generation_bundle,
)


ROOT = Path(__file__).resolve().parents[2]
_PRODUCER_TEST = (
    ROOT / "tests" / "portfolio_control_panel" / "test_account_manifest_generation.py"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def _stage_tracked_schema8_bundle(
    root: Path,
) -> tuple[Path, str, bytes, dict[str, Any]]:
    """Build a genuine v8 bundle from tracked producer/verifier fixtures."""

    producer_tests = _load_module("_task6_account_manifest_fixture", _PRODUCER_TEST)
    producer = _load_module(
        "_task6_account_manifest_producer", producer_tests.MODULE_PATH
    )
    verifier = _load_module(
        "_task6_account_manifest_verifier", producer_tests.VERIFIER_MODULE_PATH
    )
    selected = producer_tests._c3_selected_inputs(producer)
    payloads = dict(producer.build_blocked_account_manifest_payloads(selected))
    report_bytes = verifier.IndependentAccountManifestVerifier().verify(
        verifier.IndependentVerifierInputs(
            **producer_tests._selected_input_kwargs(selected)
        ),
        payloads,
    )
    payloads["verification/independent-verification.json"] = report_bytes
    candidate = json.loads(payloads["outputs/account-strategy-manifest.json"])
    report = json.loads(report_bytes)
    as_of = date.fromisoformat(report["as_of_date"])
    artifacts = tuple(
        SourceArtifact(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(contents).hexdigest(),
            size_bytes=len(contents),
            as_of_date=as_of,
            schema_name=(
                "account-strategy-manifest"
                if artifact_id == "outputs/account-strategy-manifest.json"
                else (
                    "account-manifest-independent-verification"
                    if artifact_id == "verification/independent-verification.json"
                    else "account-manifest-subject"
                )
            ),
            schema_version=(
                str(candidate["schema_version"])
                if artifact_id == "outputs/account-strategy-manifest.json"
                else (
                    str(report["schema_version"])
                    if artifact_id == "verification/independent-verification.json"
                    else "1"
                )
            ),
        )
        for artifact_id, contents in sorted(payloads.items())
    )
    generation = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("tracked-schema8-fixture", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="tracked schema-8 compatibility fixture",
            fact_types=("account-manifest",),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(root, generation, payloads)
    return (
        root,
        generation.generation_id,
        payloads["outputs/account-strategy-manifest.json"],
        report,
    )


def test_task6_real_mixed_bundle_preserves_exact_bytes_across_all_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-five rebuild or one changed staged byte cannot cross this seam."""

    generation_root, generation_id, payload, report = _stage_tracked_schema8_bundle(
        tmp_path / "generation"
    )
    candidate = json.loads(payload)

    financial = _load_module(
        "_task6_financial_mvp_contract",
        ROOT / "tools" / "portfolio_financial_mvp_contract.py",
    )
    bundle = financial.read_generation_bundle(generation_root, generation_id)

    numeric_rows = {
        row["account_id"]: {
            key: row[key]
            for key in (
                "opening_capital",
                "deposits",
                "invested",
                "current_value",
                "gain",
            )
        }
        for row in candidate["rows"]
        if row["status"] == "OK"
    }
    assert numeric_rows == {}
    assert report["numeric_verification"] == {
        "candidate_count": 0,
        "comparison_count": 0,
        "discrepancy_count": 0,
        "disposition": "NOT_APPLICABLE_NO_CANDIDATES",
    }

    materializer = _load_module(
        "_task6_pipeline_materializer",
        ROOT / "tools" / "portfolio_pipeline_materialize.py",
    )
    release = _load_module(
        "_task6_release_builder", ROOT / "tools" / "portfolio_release_builder.py"
    )
    panel = _load_module(
        "_task6_control_panel", ROOT / "tools" / "portfolio_control_panel.py"
    )
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    expected_source = {
        "generation_id": bundle.manifest.generation_id,
        "generation_manifest_sha256": bundle.receipt.manifest.sha256,
        "generation_receipt_sha256": hashlib.sha256(bundle.receipt_bytes).hexdigest(),
        "full_directory_inventory_sha256": bundle.full_directory_inventory_sha256,
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": payload_sha256,
            "size_bytes": len(payload),
            "schema": {
                "name": "account-strategy-manifest",
                "version": str(candidate["schema_version"]),
            },
        },
    }
    assert financial.epoch_manifest_source_from_bundle(bundle) == expected_source

    def raw_five_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy raw-five producer was invoked")

    monkeypatch.setattr(
        materializer, "_account_strategy_manifest_module", raw_five_must_not_run
    )
    monkeypatch.setattr(
        release, "_stage_account_strategy_manifest", raw_five_must_not_run
    )
    monkeypatch.setattr(release, "_tool", raw_five_must_not_run)
    monkeypatch.setattr(panel, "build_manifest_candidate", raw_five_must_not_run)

    financial_contract = json.loads(
        (ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    materialized, materialized_identity = (
        materializer._stage_epoch_manifest_bundle_prewire(
            generation_root,
            bundle.manifest.generation_id,
            financial_contract,
            tmp_path / "materialized",
        )
    )
    release_path, release_source, release_identity = (
        release._stage_verified_epoch_manifest_bundle(
            generation_root, bundle.manifest.generation_id, tmp_path / "release"
        )
    )
    panel_value, panel_identity = panel._verified_bundle_account_manifest(
        tmp_path
        / "materialized"
        / "stages"
        / "panel"
        / "account-strategy-manifest.json",
        generation_root,
        bundle.manifest.generation_id,
    )

    materialized_path = (
        tmp_path
        / "materialized"
        / "stages"
        / "panel"
        / "account-strategy-manifest.json"
    )
    assert materialized["epoch_manifest_source"] == expected_source
    assert release_source == expected_source
    assert panel_identity["epoch_manifest_source"] == expected_source
    assert materialized_path.read_bytes() == payload
    assert release_path.read_bytes() == payload
    assert panel_value == candidate
    assert materialized_identity == {
        "path": str(materialized_path.resolve()),
        "sha256": payload_sha256,
        "size_bytes": len(payload),
    }
    assert release_identity == {
        "path": str(release_path.resolve()),
        "sha256": payload_sha256,
        "size_bytes": len(payload),
    }
    assert panel_identity["sha256"] == payload_sha256
    assert panel_identity["size_bytes"] == len(payload)
    assert panel_identity["verification"]["status"] == (
        "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES"
    )
    assert panel_identity["verification"]["publication_readiness"] == "NOT_READY"

    changed = bytearray(materialized_path.read_bytes())
    changed[0] = ord("[")
    materialized_path.write_bytes(changed)
    with pytest.raises(
        panel.ControlPanelValidationError,
        match="staged account manifest differs from verified generation bundle",
    ):
        panel._verified_bundle_account_manifest(
            materialized_path, generation_root, bundle.manifest.generation_id
        )

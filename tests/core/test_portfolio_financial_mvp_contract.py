from __future__ import annotations

import copy
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationBundle,
    GenerationConflictError,
    GenerationManifest,
    ManifestValidationError,
    SourceArtifact,
    VerificationStatus,
    read_generation_bundle,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_financial_mvp_contract.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_financial_mvp_contract", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
contract_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract_tool
SPEC.loader.exec_module(contract_tool)

CONTRACT_PATH = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
SCHEMA_PATH = (
    ROOT / "schemas" / "portfolio" / "financial-mvp-source-contract.schema.json"
)
REGISTRY_PATH = ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
GENERATED_ROOT = ROOT / "generated" / "financial-mvp"
HWM_POLICY_PATH = ROOT / "contracts" / "portfolio-hwm-policy-v4.json"

EPOCH_MANIFEST_ARTIFACT_PATH = "outputs/account-strategy-manifest.json"
SYNTHETIC_EPOCH_PAYLOAD = b'{"fixture":"synthetic-task-6","version":8}\n'
SYNTHETIC_INDEPENDENT_REPORT = b'{"fixture":"synthetic-task-6-report"}\n'
EPOCH_CROSSWALK_ARTIFACT_PATH = "authority/account-assignment-crosswalk.json"
EPOCH_INDEPENDENT_REPORT_PATH = "verification/independent-verification.json"
FRESH_V9_GENERATION_ID = (
    "177353fc5a0e3dd2144b57495593228b204714e26d7e1db0b11ae0ebcf98ab11"
)
FRESH_V9_BUNDLE_ROOT = ROOT / ".q" / "nlv2m2"
FRESH_V9_BUNDLE_PATH = FRESH_V9_BUNDLE_ROOT / "generations" / FRESH_V9_GENERATION_ID
FRESH_V9_SIDECAR_PATH = (
    FRESH_V9_BUNDLE_ROOT
    / "report-inputs"
    / FRESH_V9_GENERATION_ID
    / "epoch-manifest-source.json"
)


def _literal_epoch_manifest_source() -> dict[str, object]:
    """Handwritten oracle for the deterministic synthetic Task-6 bundle."""

    return {
        "generation_id": (
            "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8"
        ),
        "generation_manifest_sha256": (
            "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d"
        ),
        "generation_receipt_sha256": (
            "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f"
        ),
        "full_directory_inventory_sha256": (
            "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1"
        ),
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": (
                "810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834"
            ),
            "size_bytes": 43,
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
    }


def _synthetic_epoch_bundle(
    tmp_path: Path,
    *,
    artifact_id: str = EPOCH_MANIFEST_ARTIFACT_PATH,
    schema_name: str = "account-strategy-manifest",
    schema_version: str = "8",
    payload: bytes = SYNTHETIC_EPOCH_PAYLOAD,
) -> GenerationBundle:
    """Stage and re-read synthetic Task-6 evidence through the real verifier."""

    as_of = date(2026, 8, 24)
    artifact = SourceArtifact(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        as_of_date=as_of,
        schema_name=schema_name,
        schema_version=schema_version,
    )
    independent_report = SourceArtifact(
        artifact_id="verification/independent-verification.json",
        sha256=hashlib.sha256(SYNTHETIC_INDEPENDENT_REPORT).hexdigest(),
        size_bytes=len(SYNTHETIC_INDEPENDENT_REPORT),
        as_of_date=as_of,
        schema_name="independent-verification",
        schema_version="1",
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-task-6-prewire", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic Task-6 prewire fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(artifact, independent_report),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        tmp_path,
        manifest,
        {
            artifact.artifact_id: payload,
            independent_report.artifact_id: SYNTHETIC_INDEPENDENT_REPORT,
        },
    )
    return read_generation_bundle(tmp_path, manifest.generation_id)


def _synthetic_v9_epoch_bundle(
    tmp_path: Path,
    *,
    manifest_payload: bytes | None = None,
    crosswalk_payload: bytes | None = None,
    report_payload: bytes | None = None,
    opening_basis: str = "PROVIDER_CAPITAL_ANCHOR",
) -> GenerationBundle:
    """Stage a v9 bundle whose independent report pins its two binder inputs."""

    as_of = date(2026, 8, 24)
    manifest_payload = manifest_payload or canonical_json_bytes(
        {
            "calculation_cutoff_date": "2026-08-13",
            "rows": [
                {
                    "capital_flow_trace": [],
                    "opening_basis": opening_basis,
                    "status": "OK",
                }
            ],
            "schema_version": 9,
        }
    )
    crosswalk_payload = crosswalk_payload or canonical_json_bytes(
        {
            "calculation_cutoff_date": "2026-08-13",
            "schema_version": 1,
            "selected_baseline": "synthetic",
        }
    )
    manifest_artifact = SourceArtifact(
        artifact_id=EPOCH_MANIFEST_ARTIFACT_PATH,
        sha256=hashlib.sha256(manifest_payload).hexdigest(),
        size_bytes=len(manifest_payload),
        as_of_date=as_of,
        schema_name="account-strategy-manifest",
        schema_version="9",
    )
    crosswalk_artifact = SourceArtifact(
        artifact_id=EPOCH_CROSSWALK_ARTIFACT_PATH,
        sha256=hashlib.sha256(crosswalk_payload).hexdigest(),
        size_bytes=len(crosswalk_payload),
        as_of_date=as_of,
        schema_name="account-strategy-current-epoch-crosswalk",
        schema_version="1",
    )
    report_payload = report_payload or canonical_json_bytes(
        {
            "counts": {
                "blocked_output_row_count": 0,
                "ok_output_row_count": 1,
                "output_row_count": 1,
            },
            "invariants": {
                "blocked_numeric_exclusion": True,
                "blocker_propagation": True,
                "canonical_subject_bytes": True,
                "classified_flow_reconciliation": True,
                "exact_subject_set": True,
                "fact_disposition_complete": True,
                "mapped_row_bijection": True,
                "privacy_safe_projection": True,
                "selected_source_binding": True,
                "unassigned_numeric_exclusion": True,
            },
            "numeric_verification": {
                "discrepancy_count": 0,
                "disposition": "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES",
            },
            "schema_version": 3,
            "selected_source_identities": {
                "current_epoch_crosswalk": {
                    "sha256": crosswalk_artifact.sha256,
                    "size_bytes": crosswalk_artifact.size_bytes,
                }
            },
            "status": "VERIFIED",
            "subjects": [
                {
                    "path": manifest_artifact.artifact_id,
                    "sha256": manifest_artifact.sha256,
                    "size_bytes": manifest_artifact.size_bytes,
                },
                {
                    "path": crosswalk_artifact.artifact_id,
                    "sha256": crosswalk_artifact.sha256,
                    "size_bytes": crosswalk_artifact.size_bytes,
                },
            ],
        }
    )
    independent_report = SourceArtifact(
        artifact_id=EPOCH_INDEPENDENT_REPORT_PATH,
        sha256=hashlib.sha256(report_payload).hexdigest(),
        size_bytes=len(report_payload),
        as_of_date=as_of,
        schema_name="account-manifest-independent-verification",
        schema_version="3",
    )
    generation = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-v9-binder", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic v9 binder fixture",
            fact_types=("synthetic",),
            observed_start=as_of,
            observed_end=as_of,
            known_gaps=(),
        ),
        artifacts=(manifest_artifact, crosswalk_artifact, independent_report),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        tmp_path,
        generation,
        {
            manifest_artifact.artifact_id: manifest_payload,
            crosswalk_artifact.artifact_id: crosswalk_payload,
            independent_report.artifact_id: report_payload,
        },
    )
    return read_generation_bundle(tmp_path, generation.generation_id)


def _replace_synthetic_bundle_payload(
    bundle: GenerationBundle, artifact_id: str, payload: bytes
) -> None:
    """Deliberately forge a detached in-memory bundle for binder-only mutations."""

    artifact = next(
        item for item in bundle.manifest.artifacts if item.artifact_id == artifact_id
    )
    object.__setattr__(artifact, "sha256", hashlib.sha256(payload).hexdigest())
    object.__setattr__(artifact, "size_bytes", len(payload))
    if artifact_id == EPOCH_INDEPENDENT_REPORT_PATH:
        object.__setattr__(bundle.receipt.independent_report, "sha256", artifact.sha256)
        object.__setattr__(
            bundle.receipt.independent_report, "size_bytes", artifact.size_bytes
        )
    object.__setattr__(bundle, "payloads", {**bundle.payloads, artifact_id: payload})


def test_v9_epoch_source_derives_crosswalk_from_verified_bundle(tmp_path: Path) -> None:
    """Dropping the v9 crosswalk branch would make this six-key source invalid."""

    bundle = _synthetic_v9_epoch_bundle(tmp_path)

    source = contract_tool.epoch_manifest_source_from_bundle(bundle)

    assert set(source) == {
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
        "artifact",
        "crosswalk",
    }
    assert source["artifact"]["schema"] == {
        "name": "account-strategy-manifest",
        "version": "9",
    }
    assert source["crosswalk"] == {
        "path": "authority/account-assignment-crosswalk.json",
        "sha256": "266f1860169925cca2c8955f5b9f8d8b56a27a5ac50dc9730f2dc6c1f7d24e3e",
        "size_bytes": 92,
        "schema": {
            "name": "account-strategy-current-epoch-crosswalk",
            "version": "1",
        },
    }


def test_v9_epoch_source_accepts_verified_first_available_nav_basis(
    tmp_path: Path,
) -> None:
    """A verified schema-9 first-NAV opening remains a valid release source."""

    bundle = _synthetic_v9_epoch_bundle(tmp_path, opening_basis="FIRST_AVAILABLE_NAV")

    source = contract_tool.epoch_manifest_source_from_bundle(bundle)

    assert source["artifact"]["schema"]["version"] == "9"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing-crosswalk", "crosswalk"),
        ("report-status", "report status"),
        ("report-crosswalk", "report crosswalk"),
        ("report-fact-disposition", "report invariants"),
        ("report-unassigned-numeric", "report invariants"),
        ("report-classified-flow", "report invariants"),
        ("report-privacy", "report invariants"),
        ("missing-cutoff", "cutoff"),
        ("missing-crosswalk-cutoff", "crosswalk cutoff"),
        ("future-cutoff", "cutoff"),
        ("mismatched-cutoff", "crosswalk cutoff"),
        ("v8-date", "v8 trace"),
        ("trace-extra-key", "v9 flow trace"),
        ("trace-missing-key", "v9 flow trace"),
        ("wrong-anchor", "anchor"),
    ],
)
def test_v9_epoch_source_rejects_release_control_mutations(
    tmp_path: Path, mutation: str, message: str
) -> None:
    """Removing each release-control guard would admit a forged v9 assertion."""

    bundle = _synthetic_v9_epoch_bundle(tmp_path)
    if mutation == "missing-crosswalk":
        source = contract_tool.epoch_manifest_source_from_bundle(bundle)
        source.pop("crosswalk")
        with pytest.raises(contract_tool.FinancialMvpContractError, match=message):
            contract_tool.validate_epoch_manifest_source(source, bundle=bundle)
        return
    elif mutation == "report-status":
        report = json.loads(bundle.payloads[EPOCH_INDEPENDENT_REPORT_PATH])
        report["status"] = "PENDING"
        _replace_synthetic_bundle_payload(
            bundle, EPOCH_INDEPENDENT_REPORT_PATH, canonical_json_bytes(report)
        )
    elif mutation in {
        "report-crosswalk",
        "report-fact-disposition",
        "report-unassigned-numeric",
        "report-classified-flow",
        "report-privacy",
    }:
        report = json.loads(bundle.payloads[EPOCH_INDEPENDENT_REPORT_PATH])
        if mutation == "report-crosswalk":
            report["selected_source_identities"]["current_epoch_crosswalk"][
                "sha256"
            ] = "0" * 64
        else:
            invariant = {
                "report-fact-disposition": "fact_disposition_complete",
                "report-unassigned-numeric": "unassigned_numeric_exclusion",
                "report-classified-flow": "classified_flow_reconciliation",
                "report-privacy": "privacy_safe_projection",
            }[mutation]
            report["invariants"][invariant] = False
        _replace_synthetic_bundle_payload(
            bundle, EPOCH_INDEPENDENT_REPORT_PATH, canonical_json_bytes(report)
        )
    else:
        manifest = json.loads(bundle.payloads[EPOCH_MANIFEST_ARTIFACT_PATH])
        crosswalk = json.loads(bundle.payloads[EPOCH_CROSSWALK_ARTIFACT_PATH])
        if mutation == "missing-cutoff":
            manifest.pop("calculation_cutoff_date")
        elif mutation == "missing-crosswalk-cutoff":
            crosswalk.pop("calculation_cutoff_date")
        elif mutation == "future-cutoff":
            manifest["calculation_cutoff_date"] = "2026-08-25"
            crosswalk["calculation_cutoff_date"] = "2026-08-25"
        elif mutation == "mismatched-cutoff":
            crosswalk["calculation_cutoff_date"] = "2026-08-12"
        elif mutation == "v8-date":
            manifest["rows"][0]["date"] = "2026-08-13"
        elif mutation == "trace-extra-key":
            manifest["rows"][0]["capital_flow_trace"] = [
                {
                    "amount": "1",
                    "class": "INTERNAL_IN",
                    "date": "2026-08-13",
                    "event_date": "2026-08-13",
                    "nav_recognition_date": "2026-08-13",
                }
            ]
        elif mutation == "trace-missing-key":
            manifest["rows"][0]["capital_flow_trace"] = [
                {
                    "amount": "1",
                    "class": "INTERNAL_IN",
                    "event_date": "2026-08-13",
                }
            ]
        else:
            manifest["rows"][0]["opening_basis"] = "UNSUPPORTED"
        _replace_synthetic_bundle_payload(
            bundle, EPOCH_MANIFEST_ARTIFACT_PATH, canonical_json_bytes(manifest)
        )
        _replace_synthetic_bundle_payload(
            bundle, EPOCH_CROSSWALK_ARTIFACT_PATH, canonical_json_bytes(crosswalk)
        )

    with pytest.raises(contract_tool.FinancialMvpContractError, match=message):
        contract_tool.epoch_manifest_source_from_bundle(bundle)


@pytest.mark.skipif(
    not FRESH_V9_BUNDLE_PATH.is_dir(),
    reason="retained real-data generation bundle is not available in this checkout",
)
def test_v9_binder_accepts_fresh_generation_bundle() -> None:
    """The retained coverage-corrected candidate remains a usable evidence check."""

    bundle = read_generation_bundle(FRESH_V9_BUNDLE_ROOT, FRESH_V9_GENERATION_ID)

    source = contract_tool.epoch_manifest_source_from_bundle(bundle)

    assert source == {
        "generation_id": FRESH_V9_GENERATION_ID,
        "generation_manifest_sha256": (
            "e91b2f824100894e55004f07cf47eb3f4f766bffecb9fe3348ec9c387842823b"
        ),
        "generation_receipt_sha256": (
            "64e76af7e0868947d0703bec94e6d633c72e4934e7f39d28a8f942fb23702177"
        ),
        "full_directory_inventory_sha256": (
            "8f7959f9218d6b6784c8b73c2dd3f0945d6042e98c6e3759a9e29195c1ccd065"
        ),
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": "ea680c4a381b71e67baa8ac36279dccc9740cca82ccf5ea1ca38ab7b10ac8593",
            "size_bytes": 236138,
            "schema": {"name": "account-strategy-manifest", "version": "9"},
        },
        "crosswalk": {
            "path": "authority/account-assignment-crosswalk.json",
            "sha256": "deb4080f039517854cab29476636cc757e5b057112319b6c396474f79f32ef82",
            "size_bytes": 5986,
            "schema": {
                "name": "account-strategy-current-epoch-crosswalk",
                "version": "1",
            },
        },
    }


@pytest.mark.skipif(
    not (FRESH_V9_BUNDLE_PATH.is_dir() and FRESH_V9_SIDECAR_PATH.is_file()),
    reason="retained real-data generation bundle and sidecar are not available in this checkout",
)
def test_fresh_v9_sidecar_equals_bundle_derived_source() -> None:
    """The retained sidecar cannot silently diverge from its verified bundle."""

    bundle = read_generation_bundle(FRESH_V9_BUNDLE_ROOT, FRESH_V9_GENERATION_ID)

    assert json.loads(FRESH_V9_SIDECAR_PATH.read_text(encoding="utf-8")) == (
        contract_tool.epoch_manifest_source_from_bundle(bundle)
    )


@pytest.mark.parametrize(
    "artifact_id",
    [
        EPOCH_MANIFEST_ARTIFACT_PATH,
        EPOCH_CROSSWALK_ARTIFACT_PATH,
        EPOCH_INDEPENDENT_REPORT_PATH,
    ],
)
def test_v9_persisted_byte_mutation_is_rejected_before_binder(
    tmp_path: Path, artifact_id: str
) -> None:
    """Detached controls must reject a copied bundle before binder semantics run."""

    bundle = _synthetic_v9_epoch_bundle(tmp_path / "source")
    source_directory = (
        tmp_path / "source" / "generations" / bundle.manifest.generation_id
    )
    copied_root = tmp_path / "copied"
    copied_directory = copied_root / "generations" / bundle.manifest.generation_id
    copied_directory.parent.mkdir(parents=True)
    shutil.copytree(source_directory, copied_directory)
    target = copied_directory / artifact_id
    target.write_bytes(target.read_bytes() + b" ")

    with pytest.raises(GenerationConflictError, match="bundle"):
        read_generation_bundle(copied_root, bundle.manifest.generation_id)


def test_epoch_manifest_source_binds_every_literal_value_from_verified_bundle(
    tmp_path: Path,
) -> None:
    """Changing any receipt, directory, or artifact binding must break this oracle."""

    literal_source = _literal_epoch_manifest_source()
    bundle = _synthetic_epoch_bundle(tmp_path)
    generation_directory = (
        tmp_path
        / "generations"
        / "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8"
    )
    manifest_bytes = (generation_directory / "generation.json").read_bytes()
    receipt_bytes = (generation_directory / "generation-receipt.json").read_bytes()
    artifact_bytes = (
        generation_directory / "outputs" / "account-strategy-manifest.json"
    ).read_bytes()
    assert generation_directory.name == literal_source["generation_id"]
    assert (
        hashlib.sha256(manifest_bytes).hexdigest()
        == literal_source["generation_manifest_sha256"]
    )
    assert (
        hashlib.sha256(receipt_bytes).hexdigest()
        == literal_source["generation_receipt_sha256"]
    )
    assert (
        hashlib.sha256(artifact_bytes).hexdigest()
        == literal_source["artifact"]["sha256"]
    )
    assert len(artifact_bytes) == literal_source["artifact"]["size_bytes"]

    persisted_files = {
        path.relative_to(generation_directory).as_posix(): path.read_bytes()
        for path in generation_directory.rglob("*")
        if path.is_file()
    }
    directory_inventory = {
        "files": [
            {
                "relative_path": relative_path,
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size_bytes": len(contents),
            }
            for relative_path, contents in sorted(persisted_files.items())
        ],
        "inventory_schema_version": 1,
    }
    directory_inventory_bytes = (
        json.dumps(
            directory_inventory,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    assert (
        hashlib.sha256(directory_inventory_bytes).hexdigest()
        == literal_source["full_directory_inventory_sha256"]
    )

    assert contract_tool.epoch_manifest_source_from_bundle(bundle) == literal_source


def test_validate_epoch_manifest_source_accepts_handwritten_literal_source(
    tmp_path: Path,
) -> None:
    """An always-reject validator or shared binder drift breaks this literal input."""

    contract_tool.validate_epoch_manifest_source(
        _literal_epoch_manifest_source(), bundle=_synthetic_epoch_bundle(tmp_path)
    )


def test_epoch_manifest_source_rejects_non_bundle_and_forged_bundle_subclass(
    tmp_path: Path,
) -> None:
    """Relaxing the exact GenerationBundle boundary would admit a structural substitute."""

    bundle = _synthetic_epoch_bundle(tmp_path)

    class ForgedGenerationBundle(GenerationBundle):
        pass

    forged = ForgedGenerationBundle(
        manifest=bundle.manifest,
        payloads=bundle.payloads,
        inventory=bundle.inventory,
        inventory_bytes=bundle.inventory_bytes,
        receipt=bundle.receipt,
        receipt_bytes=bundle.receipt_bytes,
        full_directory_inventory_sha256=bundle.full_directory_inventory_sha256,
    )
    duck_typed = SimpleNamespace(
        manifest=bundle.manifest,
        payloads=bundle.payloads,
        inventory=bundle.inventory,
        inventory_bytes=bundle.inventory_bytes,
        receipt=bundle.receipt,
        receipt_bytes=bundle.receipt_bytes,
        full_directory_inventory_sha256=bundle.full_directory_inventory_sha256,
    )
    for value in (bundle.manifest, object(), duck_typed, forged):
        with pytest.raises(
            contract_tool.FinancialMvpContractError, match="GenerationBundle"
        ):
            contract_tool.epoch_manifest_source_from_bundle(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("artifact_id", "schema_name", "schema_version", "payload", "message"),
    [
        (
            "outputs/wrong.json",
            "account-strategy-manifest",
            "8",
            SYNTHETIC_EPOCH_PAYLOAD,
            "required artifact",
        ),
        (
            EPOCH_MANIFEST_ARTIFACT_PATH,
            "wrong-schema",
            "8",
            SYNTHETIC_EPOCH_PAYLOAD,
            "schema name",
        ),
        (
            EPOCH_MANIFEST_ARTIFACT_PATH,
            "account-strategy-manifest",
            "9",
            SYNTHETIC_EPOCH_PAYLOAD,
            "crosswalk",
        ),
    ],
)
def test_epoch_manifest_source_rejects_wrong_artifact_contract(
    tmp_path: Path,
    artifact_id: str,
    schema_name: str,
    schema_version: str,
    payload: bytes,
    message: str,
) -> None:
    bundle = _synthetic_epoch_bundle(
        tmp_path,
        artifact_id=artifact_id,
        schema_name=schema_name,
        schema_version=schema_version,
        payload=payload,
    )

    with pytest.raises(contract_tool.FinancialMvpContractError, match=message):
        contract_tool.epoch_manifest_source_from_bundle(bundle)


def test_zero_byte_epoch_manifest_payload_is_rejected_by_real_envelope(
    tmp_path: Path,
) -> None:
    """A zero-byte fixed JSON artifact cannot produce a GenerationBundle."""

    with pytest.raises(
        ManifestValidationError, match="payload JSON is not strict JSON"
    ):
        _synthetic_epoch_bundle(tmp_path, payload=b"")


def test_empty_json_epoch_manifest_payload_is_stageable_and_re_readable(
    tmp_path: Path,
) -> None:
    """The core envelope admits a real, semantically empty JSON payload only."""

    bundle = _synthetic_epoch_bundle(tmp_path, payload=b"{}\n")

    assert bundle.payloads["outputs/account-strategy-manifest.json"] == b"{}\n"
    assert contract_tool.epoch_manifest_source_from_bundle(bundle)["artifact"] == {
        "path": "outputs/account-strategy-manifest.json",
        "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
        "size_bytes": 3,
        "schema": {"name": "account-strategy-manifest", "version": "8"},
    }


def test_epoch_manifest_source_recomputes_persisted_payload_sha256(
    tmp_path: Path,
) -> None:
    """Replacing payload hashing with manifest trust must fail this retained test."""

    bundle = _synthetic_epoch_bundle(tmp_path)
    artifact = next(
        item
        for item in bundle.manifest.artifacts
        if item.artifact_id == EPOCH_MANIFEST_ARTIFACT_PATH
    )
    # Deliberate in-memory tamper after a genuine staged/read bundle.
    object.__setattr__(artifact, "sha256", "0" * 64)

    with pytest.raises(contract_tool.FinancialMvpContractError, match="bytes differ"):
        contract_tool.epoch_manifest_source_from_bundle(bundle)


@pytest.mark.parametrize(
    ("target", "attribute", "value", "message"),
    [
        ("manifest", "verification", VerificationStatus.PENDING, "VERIFIED"),
        ("inventory", "generation_id", "0" * 64, "generation IDs"),
        ("receipt", "generation_id", "0" * 64, "generation IDs"),
    ],
)
def test_epoch_manifest_source_rejects_unverified_or_mismatched_controls(
    tmp_path: Path,
    target: str,
    attribute: str,
    value: object,
    message: str,
) -> None:
    bundle = _synthetic_epoch_bundle(tmp_path)
    object.__setattr__(getattr(bundle, target), attribute, value)

    with pytest.raises(contract_tool.FinancialMvpContractError, match=message):
        contract_tool.epoch_manifest_source_from_bundle(bundle)


def test_generation_manifest_constructor_rejects_duplicate_epoch_artifact_ids() -> None:
    """Duplicate artifact IDs are rejected by GenerationManifest, before a bundle exists."""

    as_of = date(2026, 8, 24)
    artifact = SourceArtifact(
        artifact_id=EPOCH_MANIFEST_ARTIFACT_PATH,
        sha256=hashlib.sha256(SYNTHETIC_EPOCH_PAYLOAD).hexdigest(),
        size_bytes=len(SYNTHETIC_EPOCH_PAYLOAD),
        as_of_date=as_of,
        schema_name="account-strategy-manifest",
        schema_version="8",
    )
    with pytest.raises(ManifestValidationError, match="unique"):
        GenerationManifest.build(
            as_of_date=as_of,
            adapter=AdapterIdentity("synthetic-task-6-duplicate", "1"),
            coverage=CoverageClaim(
                status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
                boundary="synthetic duplicate artifact fixture",
                fact_types=("synthetic",),
                observed_start=as_of,
                observed_end=as_of,
                known_gaps=(),
            ),
            artifacts=(artifact, artifact),
            verification=VerificationStatus.VERIFIED,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        ("generation_id", "0" * 64),
        ("generation_manifest_sha256", "1" * 64),
        ("generation_receipt_sha256", "2" * 64),
        ("full_directory_inventory_sha256", "3" * 64),
        ("artifact.path", "outputs/wrong.json"),
        ("artifact.sha256", "4" * 64),
        ("artifact.size_bytes", 47),
        ("artifact.schema.name", "wrong-schema"),
        ("artifact.schema.version", "9"),
    ],
)
def test_validate_epoch_manifest_source_rejects_each_bound_value(
    tmp_path: Path, mutation: tuple[str, object]
) -> None:
    """Removing one exact comparison would allow its independently mutated value."""

    bundle = _synthetic_epoch_bundle(tmp_path)
    source = _literal_epoch_manifest_source()
    path, value = mutation
    if path.startswith("artifact.schema."):
        source["artifact"]["schema"][path.rsplit(".", 1)[1]] = value  # type: ignore[index]
    elif path.startswith("artifact."):
        source["artifact"][path.split(".", 1)[1]] = value  # type: ignore[index]
    else:
        source[path] = value

    with pytest.raises(contract_tool.FinancialMvpContractError, match=path):
        contract_tool.validate_epoch_manifest_source(source, bundle=bundle)


def test_validate_epoch_manifest_source_rejects_closed_key_sets_and_boolean_size(
    tmp_path: Path,
) -> None:
    bundle = _synthetic_epoch_bundle(tmp_path)
    extra = _literal_epoch_manifest_source()
    extra["unexpected"] = "value"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="keys"):
        contract_tool.validate_epoch_manifest_source(extra, bundle=bundle)

    boolean_size = _literal_epoch_manifest_source()
    boolean_size["artifact"]["size_bytes"] = True  # type: ignore[index]
    with pytest.raises(contract_tool.FinancialMvpContractError, match="size_bytes"):
        contract_tool.validate_epoch_manifest_source(boolean_size, bundle=bundle)


def test_payload_byte_mutation_fails_bundle_read_before_binder(tmp_path: Path) -> None:
    bundle = _synthetic_epoch_bundle(tmp_path)
    payload_path = (
        tmp_path
        / "generations"
        / bundle.manifest.generation_id
        / EPOCH_MANIFEST_ARTIFACT_PATH
    )
    payload_path.write_bytes(SYNTHETIC_EPOCH_PAYLOAD[:-1] + b"!")

    with pytest.raises(GenerationConflictError, match="bundle"):
        read_generation_bundle(tmp_path, bundle.manifest.generation_id)


def _contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _registry() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _validate(contract: dict[str, object]) -> dict[str, object]:
    return contract_tool.validate_contract(contract, registry=_registry())


def test_excluded_product_interval_must_be_closed() -> None:
    """An open excluded interval would let a generic consumer promote it again."""

    contract = _contract()
    excluded = next(
        row
        for row in contract["strategies"]
        if row["product_requirement"] == "EXCLUDED"
    )
    excluded["effective_product_interval"] = {
        "effective_start": "2025-09-27",
        "effective_end": None,
    }

    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="excluded strategy product interval remains open",
    ):
        _validate(contract)

    excluded["effective_product_interval"] = {
        "effective_start": "2025-09-27",
        "effective_end": "2025-11-24",
    }
    for row in contract["strategies"]:
        if row["product_requirement"] != "EXCLUDED" or row is excluded:
            continue
        row["effective_product_interval"] = {
            "effective_start": "2026-08-17",
            "effective_end": "2026-08-17",
        }
    _validate(contract)


def _source(contract: dict[str, object], source_id: str) -> dict[str, object]:
    rows = contract["source_segments"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["source_id"] == source_id)


def _outcome(contract: dict[str, object], outcome_id: str) -> dict[str, object]:
    rows = contract["financial_outcomes"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["outcome_id"] == outcome_id)


def _history_correction(
    contract: dict[str, object], correction_id: str
) -> dict[str, object]:
    history = contract["history_corrections"]
    assert isinstance(history, dict)
    rows = history["corrections"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["correction_id"] == correction_id)


def _github_comment(comment_id: int, body: str = "") -> dict[str, object]:
    return {
        "id": comment_id,
        "html_url": (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{comment_id}"
        ),
        "body": body,
        "user": {"login": "dachent"},
        "author_association": "OWNER",
        "created_at": "2026-08-17T12:00:00Z",
        "updated_at": "2026-08-17T12:00:00Z",
    }


def _census_owner_comment() -> dict[str, object]:
    record = {
        "decision_id": ("DATA-CENSUS-001-OWNER-PERIMETER-AND-MEMBERSHIP-2026-08-19"),
        "authority_issue": 302,
        "approved_on": "2026-08-19",
        "perimeter": [
            {
                "strategy_id": "composer-holy-grail",
                "disposition": "IMMATERIAL_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
            },
            {
                "strategy_id": "echotrade-growth-pivots",
                "disposition": "DEPRECATED_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
            },
        ],
        "memberships": [
            {
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-22",
                "effective_end": None,
            },
            {
                "strategy_id": "grizzlybears-vtma",
                "effective_start": "2026-01-02",
                "effective_end": None,
            },
            {
                "strategy_id": "qsi",
                "effective_start": "2026-01-05",
                "effective_end": None,
            },
            {
                "strategy_id": "tt-portfolio",
                "effective_start": "2025-11-25",
                "effective_end": None,
            },
        ],
        "transitions": [
            {
                "from_strategy_id": "tt-portfolio",
                "from_effective_end": "2026-01-04",
                "to_strategy_id": "qsi",
                "to_effective_start": "2026-01-05",
                "scope": "TWO_REDACTED_SLEEVES",
            }
        ],
        "continuity": [
            {
                "strategy_id": "tt-portfolio",
                "rule": "CONTINUOUS_MEMBERSHIP",
                "disconfirming_interval": "2026-07-14/2026-07-30",
                "disposition": (
                    "ZERO_OR_MISSING_COMPONENT_VALUE_DOES_NOT_CREATE_EXIT_OR_REENTRY"
                ),
            }
        ],
        "explicit_non_decisions": [
            "TRANSACTION_OR_FLOW_CLASSIFICATION",
            "UNSUPPORTED_STRATEGY_DESIGN_VERSION",
            "PRIVATE_ACCOUNT_IDENTITY",
            "POINTER_OR_PUBLICATION",
        ],
    }
    return {
        "id": contract_tool._CENSUS_OWNER_COMMENT_ID,
        "html_url": (
            "https://github.com/dachent/quant_engine/issues/302#issuecomment-"
            f"{contract_tool._CENSUS_OWNER_COMMENT_ID}"
        ),
        "body": f"```json\n{json.dumps(record, sort_keys=True)}\n```",
        "user": {"login": "dachent"},
        "author_association": "OWNER",
        "created_at": "2026-08-20T01:08:42Z",
        "updated_at": "2026-08-20T01:08:42Z",
    }


def _owner_evidence_comments(
    contract: dict[str, object],
) -> dict[int, dict[str, object]]:
    invested = _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"]
    receipts = invested["owner_decision_receipts"]
    comments: dict[int, dict[str, object]] = {}
    for receipt in receipts.values():
        comments[receipt["comment_id"]] = _github_comment(receipt["comment_id"])
    digest_ids = {receipt["digest_comment_id"] for receipt in receipts.values()}
    for digest_id in digest_ids:
        decision_ids = [
            decision_id
            for decision_id, receipt in receipts.items()
            if receipt["digest_comment_id"] == digest_id
        ]
        record = contract_tool.owner_receipt_evidence_record(contract, decision_ids)
        body = (
            f"{contract_tool._OWNER_RECEIPT_MARKER}\n\n```json\n"
            f"{json.dumps(record, indent=2, sort_keys=True)}\n```\n"
        )
        comments[digest_id] = _github_comment(digest_id, body)
    return comments


def _schema_errors(
    node: dict[str, object],
    value: object,
    root: dict[str, object],
    path: str = "$",
) -> list[str]:
    return contract_tool.schema_errors(node, value, root, path)


def test_authoritative_contract_schema_and_views_exist() -> None:
    assert CONTRACT_PATH.is_file()
    assert SCHEMA_PATH.is_file()
    assert (GENERATED_ROOT / "current-product.md").is_file()
    assert (GENERATED_ROOT / "source-matrix.json").is_file()


def test_contract_is_valid_and_has_stable_identity() -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    # Digest binds the gap-safe, funded-boundary provider ledger and reconciled
    # Robuxio daily-only segment through the financial-MVP cutoff (with HPR
    # spans confined to coverage) while retaining corrected P123 inputs and
    # the deprecated Echo boundary.
    assert contract_tool.contract_digest(contract) == (
        "51146395041b31111709b6b07360e90085d41c8b3ed9c62bf18c7ab23d659a16"
    )


def test_quantbase_generation_is_account_live_from_accepted_membership() -> None:
    source = _source(_contract(), "quantbase-account-generation")
    assert source["evidence_layer"] == "ACCOUNT_LIVE"
    assert source["interval"] == {
        "observed_start": "2024-10-15",
        "observed_end": "2026-08-16",
        "effective_start": "2024-10-15",
        "effective_end": None,
        "splice_policy": (
            "OWNER_ACCOUNT_LIVE_FROM_ACCEPTED_2024_10_15_MEMBERSHIP; "
            "PRESERVE_SEPARATE_PROVIDER_COMPARISON_ONLY_WHEN_EXPLICITLY_IDENTIFIED"
        ),
    }
    admission = source["admission"]
    assert admission["transform"] == "provider-ledger"
    assert admission["version"] == "1"
    assert admission["facts"] == [
        "NAV_OBSERVATION",
        "EXTERNAL_FLOW",
        "CAPITAL_ANCHOR",
        "FINANCING_LIABILITY",
        "LEVERAGE_EXPOSURE",
        "FINANCING_COST",
    ]
    assert admission["dataset_disposition_policy"]["policy_id"] == (
        "PROVIDER_ADMIT_OR_DECLARE_V1"
    )


def test_current_provider_capital_authority_binds_recomputed_dataset_sets() -> None:
    contract = _contract()
    expected = {
        "ibkr-account-generation": {
            "source_version": "ibkr-catalog-v4",
            "schema_version": "4",
            "generation_id": (
                "0917837a0b15ff473300bbdff2ca0ed34f96ed3b5e5a606258849dc20533dd65"
            ),
            "generation_sha256": (
                "eafeda509254eb086c4a7889db14709ad7963777799f4a4320d6906f229c93eb"
            ),
            "upstream_inventory_sha256": (
                "4765503faa2cbd0b57832e21041d0c99aa1475ce9e6cef1efbd7b71a686317fa"
            ),
            "product_data_class_set_sha256": (
                "03b4a13d4df8a3afbe166f33ada430ac842510b7aa85af6feb8468dca2270044"
            ),
            "admitted_item_count": 24,
            "admitted_item_set_sha256": (
                "90f111837e9be3728398a1b184bdec9f079e33ec176111b1ae1c423bdad4d49a"
            ),
        },
        "matrixport-account-generation": {
            "admitted_item_count": 5,
            "admitted_item_set_sha256": (
                "230adba3c8a0a2523f1b38e2fc5521b1e292a2fff90f5fc0a924d7de925c0629"
            ),
        },
        "quantbase-account-generation": {
            "admitted_item_count": 5,
            "admitted_item_set_sha256": (
                "95406eafada9522e22943c31c9b3cb594b992cd696fc7edf239e0a3c1661655b"
            ),
        },
        "schwab-account-generation": {
            "admitted_item_count": 19,
            "admitted_item_set_sha256": (
                "e20b207466b46110e3dc7bf01a3e94c0aff1c4b76963516a117412d52c86d88e"
            ),
        },
        "portfolio123-model-generation": {
            "admitted_item_count": 0,
            "admitted_item_set_sha256": (
                "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
            ),
        },
    }
    for source_id, wanted in expected.items():
        source = _source(contract, source_id)
        policy = source["admission"]["dataset_disposition_policy"]
        if "source_version" in wanted:
            assert source["source_version"] == wanted["source_version"]
            assert source["schema"]["version"] == wanted["schema_version"]
            assert source["current_generation"] == {
                "generation_id": wanted["generation_id"],
                "sha256": wanted["generation_sha256"],
                "as_of": "2026-09-03",
            }
            assert (
                policy["upstream_inventory_sha256"]
                == wanted["upstream_inventory_sha256"]
            )
            assert (
                policy["product_data_class_set_sha256"]
                == wanted["product_data_class_set_sha256"]
            )
        assert policy["admitted_item_count"] == wanted["admitted_item_count"]
        assert policy["admitted_item_set_sha256"] == wanted["admitted_item_set_sha256"]
    schwab = _source(contract, "schwab-account-generation")
    assert "EXTERNAL_FLOW" in schwab["admission"]["facts"]
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["admission"]["facts"] == [
        "STRATEGY_VERSION_REFERENCE",
        "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1_CANDIDATE",
        "TRANSACTION",
    ]
    assert p123["admission"]["dataset_disposition_policy"][
        "allowed_exclusion_reasons"
    ] == [
        "REQUIRED_DATA_CLASS_NOT_MATERIALIZED",
        "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER",
    ]


def test_current_strategy_registry_binds_exact_applicability_plan_bytes() -> None:
    source = _source(_contract(), "current-strategy-registry")
    digest = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    assert source["source_version"] == "decomposition-plan-v3"
    assert source["schema"] == {
        "name": "current-strategy-decomposition-plan",
        "version": "3",
    }
    assert source["data_classes"] == [
        "STRATEGY_VERSION",
        "DECOMPOSITION_APPLICABILITY",
        "DECOMPOSITION_SURFACE_POLICY",
    ]
    assert source["current_generation"] == {
        "generation_id": digest,
        "sha256": digest,
        "as_of": "2026-08-18",
    }


def test_calculation_boundary_policy_separates_completeness_from_age() -> None:
    contract = _contract()
    policy = contract["product"]["as_of_policy"]
    boundary = policy["calculation_boundary"]
    assert boundary == {
        "scope": "REQUIRED_INPUTS_OF_EACH_CALCULATION",
        "rule": (
            "LATEST_COMMON_COMPLETE_ACCEPTED_DATE_ACROSS_REQUIRED_CALCULATION_INPUTS"
        ),
        "calendar_rule": (
            "Intersect accepted complete observation dates under each required "
            "input's declared calendar and gap policy; never fill, zero, relabel, "
            "or use a future observation."
        ),
        "post_boundary_rows": ("RETAIN_IN_SOURCE_EVIDENCE_EXCLUDE_FROM_CALCULATION"),
        "age_disposition": (
            "WARN_WITH_COMPLETE_THROUGH_DATE_AGE_DAYS_THRESHOLD_AND_LIMITING_INPUTS"
        ),
        "completeness_failure_disposition": "FAIL_CLOSED",
        "required_evidence": [
            "EVALUATED_ON_DATE",
            "COMPLETE_THROUGH_DATE",
            "AGE_DAYS",
            "WARNING_AFTER_DAYS",
            "LIMITING_INPUTS",
        ],
    }
    assert policy["execution_profiles"] == {
        "DEVELOPMENT": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "WORKSTATION_QC": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "RELEASE_QC": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
    }

    invalid = copy.deepcopy(contract)
    invalid["product"]["as_of_policy"]["calculation_boundary"][
        "completeness_failure_disposition"
    ] = "WARN"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="calculation completeness must remain fail-closed",
    ):
        _validate(invalid)

    rendered = contract_tool.render_views(contract)
    assert b"LATEST_COMMON_COMPLETE_ACCEPTED_DATE" in rendered.summary_markdown
    assert b"WARN_WITH_COMPLETE_THROUGH_DATE" in rendered.summary_markdown
    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["as_of_policy"] == policy
    assert all(
        row["age_disposition"] == boundary["age_disposition"] for row in matrix["rows"]
    )


def test_contract_validates_against_the_checked_in_schema() -> None:
    contract = _contract()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _schema_errors(schema, contract, schema) == []

    extra = copy.deepcopy(contract)
    extra["shadow_manifest"] = {}
    assert any(
        "unexpected shadow_manifest" in error
        for error in _schema_errors(schema, extra, schema)
    )

    missing = copy.deepcopy(contract)
    del missing["source_segments"][0]["capture_status"]
    assert any(
        "missing capture_status" in error
        for error in _schema_errors(schema, missing, schema)
    )

    shadow_calculation = copy.deepcopy(contract)
    shadow_calculation["financial_outcomes"][0]["calculation_contract"][
        "shadow_acceptance"
    ] = "PASS"
    assert any(
        "unexpected shadow_acceptance" in error
        for error in _schema_errors(schema, shadow_calculation, schema)
    )


def test_production_loader_enforces_the_checked_in_schema(tmp_path: Path) -> None:
    broken = _contract()
    del broken["source_segments"][0]["strategy_version"]
    contract_path = tmp_path / "broken-contract.json"
    contract_path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="schema validation failed"
    ):
        contract_tool.load_contract(contract_path, REGISTRY_PATH, SCHEMA_PATH)


def test_schema_is_strict_without_frozen_strategy_or_provider_counts() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["source_segment"]["additionalProperties"] is False
    assert schema["$defs"]["strategy"]["additionalProperties"] is False
    strategies = schema["properties"]["strategies"]
    assert strategies["minItems"] == 1
    assert "maxItems" not in strategies


def test_strategy_perimeter_matches_discoverable_registry_without_disappearing_rows() -> (
    None
):
    contract = _contract()
    registry = _registry()
    expected = [row["strategy_id"] for row in registry["strategies"]]
    actual = [row["strategy_id"] for row in contract["strategies"]]
    assert actual == expected
    requirements = {
        row["strategy_id"]: row["product_requirement"] for row in contract["strategies"]
    }
    assert {key for key, value in requirements.items() if value == "EXCLUDED"} == {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    assert {key for key, value in requirements.items() if value == "REQUIRED"} == {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    }
    assert {
        row["strategy_id"]: row["history_series_id"] for row in contract["strategies"]
    } == {
        "composer-holy-grail": None,
        "echotrade-growth-pivots": "Growth at Market Pivot Points [EchoTrade]",
        "grizzlybears-vtma": "GrizzlyBears VTMA",
        "p123-ai-factor": "AI Factor [P123]",
        "p123-eu": "EU [P123]",
        "qsi": "QSI",
        "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
        "tt-portfolio": "TT Portfolio",
        "volatis-strategy": "Volatis Strategy",
    }
    hwm_exclusions = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["outcome_dispositions"]["AS_HELD_COMPOSITE_HWM"]
        == "EXCLUDED_BY_ACCEPTED_POLICY"
    }
    assert hwm_exclusions == {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    excluded = {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    for row in contract["strategies"]:
        expected = (
            "EXCLUDED_BY_ACCEPTED_POLICY"
            if row["strategy_id"] in excluded
            else "REQUIRED"
        )
        assert set(row["outcome_dispositions"].values()) == {expected}


def test_summary_separates_current_perimeter_from_historical_owner_exclusions() -> None:
    """The current table must not visually promote accepted exclusions."""

    summary = contract_tool.render_views(_contract()).summary_markdown.decode("utf-8")
    current_heading = "## Current perimeter"
    exclusions_heading = "## Historical/owner exclusions"
    census_heading = "## Current provider/account census — #302"

    assert summary.count(current_heading) == 1
    assert summary.count(exclusions_heading) == 1
    assert summary.count(census_heading) == 1
    assert (
        summary.index(current_heading)
        < summary.index(exclusions_heading)
        < summary.index(census_heading)
    )

    current_section = summary.split(current_heading, 1)[1].split(exclusions_heading, 1)[
        0
    ]
    exclusions_section = summary.split(exclusions_heading, 1)[1].split(
        census_heading, 1
    )[0]

    def strategy_rows(section: str) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for line in section.splitlines():
            if not line.startswith("| "):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells[0] in {"Strategy", "---"}:
                continue
            rows.append((cells[0], cells[1], cells[3]))
        return rows

    current_rows = strategy_rows(current_section)
    assert len(current_rows) == 7
    assert current_rows == [
        ("p123-ai-factor", "ACTIVE", "REQUIRED"),
        ("p123-eu", "ACTIVE", "REQUIRED"),
        ("qsi", "ACTIVE", "REQUIRED"),
        ("tt-portfolio", "ACTIVE", "REQUIRED"),
        ("volatis-strategy", "ACTIVE", "REQUIRED"),
        ("grizzlybears-vtma", "ACTIVE", "REQUIRED"),
        ("robuxio-crypto", "ACTIVE", "REQUIRED"),
    ]

    exclusions_rows = strategy_rows(exclusions_section)
    assert len(exclusions_rows) == 2
    assert exclusions_rows == [
        ("echotrade-growth-pivots", "DEPRECATED_HISTORY_ONLY", "EXCLUDED"),
        ("composer-holy-grail", "IMMATERIAL_EXCLUDED", "EXCLUDED"),
    ]


def test_current_census_binds_redacted_sleeves_memberships_and_transition() -> None:
    contract = _contract()
    census = contract["current_census"]
    assert census["authority_issue"] == 302
    assert len(census["account_sleeves"]) == 19
    assert len(census["provider_lanes"]) == 10
    assert {row["role"] for row in census["evidence_receipts"]} == {
        "BASELINE_CURRENT_POINTER",
        "BASELINE_GENERATION_MANIFEST",
        "BASELINE_CATALOG",
    }
    current_counts: dict[str, int] = {}
    transition_rows: list[dict[str, object]] = []
    for sleeve in census["account_sleeves"]:
        current = sleeve["current_strategy_id"]
        if current is not None:
            current_counts[current] = current_counts.get(current, 0) + 1
        for assignment in sleeve["assignments"]:
            if assignment["transition_group_id"] is not None:
                transition_rows.append(assignment)
    assert current_counts == {
        "grizzlybears-vtma": 1,
        "p123-ai-factor": 1,
        "p123-eu": 1,
        "qsi": 2,
        "robuxio-crypto": 1,
        "tt-portfolio": 4,
        "volatis-strategy": 1,
    }
    assert len(transition_rows) == 4
    assert {
        (row["strategy_id"], row["effective_start"], row["effective_end"])
        for row in transition_rows
    } == {
        ("tt-portfolio", "2025-11-25", "2026-01-04"),
        ("qsi", "2026-01-05", None),
    }
    tt_funded = [
        assignment
        for sleeve in census["account_sleeves"]
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] == "tt-portfolio"
        and assignment["status"] == "FUNDED_MEMBERSHIP"
    ]
    assert tt_funded
    assert all(row["effective_end"] is None for row in tt_funded)


def test_p123_admitted_set_and_hwm_bind_independently_computed_contract_hashes() -> (
    None
):
    """Reusing production digest helpers here would conceal stale HWM bindings."""

    contract_bytes = CONTRACT_PATH.read_bytes()
    contract = json.loads(contract_bytes)
    p123 = _source(contract, "portfolio123-model-generation")
    assert (
        p123["admission"]["dataset_disposition_policy"]["admitted_item_set_sha256"]
        == "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
    )

    raw_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    semantic_bytes = (
        json.dumps(
            contract,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    semantic_digest = hashlib.sha256(semantic_bytes).hexdigest()
    hwm_policy = json.loads(HWM_POLICY_PATH.read_bytes())
    assert hwm_policy["product_authority"] == {
        "path": "contracts/portfolio-financial-mvp-source-v1.json",
        "raw_sha256": raw_sha256,
        "semantic_digest": semantic_digest,
    }


def test_census_owner_digest_and_generated_views_are_one_authority() -> None:
    contract = _contract()
    census = contract["current_census"]
    contract_tool.reconcile_census_owner_evidence(contract, _census_owner_comment())
    rendered = contract_tool.render_views(contract)
    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["current_census"] == census
    assert matrix["census_sha256"] == contract_tool.census_digest(census)
    assert matrix["census_rows"]
    assert {row["sleeve_alias"] for row in matrix["census_rows"]} == {
        row["sleeve_alias"] for row in census["account_sleeves"]
    }
    assert b"Current provider/account census" in rendered.summary_markdown
    assert b"Owner decision approved: `2026-08-19`" in rendered.summary_markdown
    assert census["decision_payload_sha256"].encode() in rendered.summary_markdown

    tampered = _census_owner_comment()
    tampered["updated_at"] = "2026-08-20T01:08:43Z"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="census owner evidence authority differs",
    ):
        contract_tool.reconcile_census_owner_evidence(contract, tampered)


def test_census_owner_projection_rejects_coherent_semantic_rewrites() -> None:
    baseline = _contract()
    assert contract_tool._verify_strategy_version_history_evidence(baseline) == 3
    approved_digest = baseline["current_census"]["decision_payload_sha256"]
    projection = contract_tool.census_owner_decision_projection(
        baseline["current_census"], baseline["strategies"]
    )
    assert hashlib.sha256(
        contract_tool.canonical_json_bytes(projection)
    ).hexdigest() == (approved_digest)

    promoted = copy.deepcopy(baseline)
    composer = next(
        row
        for row in promoted["strategies"]
        if row["strategy_id"] == "composer-holy-grail"
    )
    composer["product_requirement"] = "REQUIRED"
    promoted_projection = contract_tool.census_owner_decision_projection(
        promoted["current_census"], promoted["strategies"]
    )
    assert (
        hashlib.sha256(
            contract_tool.canonical_json_bytes(promoted_projection)
        ).hexdigest()
        != approved_digest
    )

    dropped = copy.deepcopy(baseline)
    p123_ai = next(
        row for row in dropped["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["product_requirement"] = "OPTIONAL"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner membership is not propagated",
    ):
        contract_tool.census_owner_decision_projection(
            dropped["current_census"], dropped["strategies"]
        )

    false_design = copy.deepcopy(baseline)
    p123_ai = next(
        row
        for row in false_design["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["strategy_version"] = {
        "status": "EVIDENCED",
        "version_ref": "unapproved-design-v999",
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="design/version evidence is not independently bound",
    ):
        _validate(false_design)

    coherently_repeated = copy.deepcopy(baseline)
    fake_version = "unapproved-design-v999"
    p123_ai = next(
        row
        for row in coherently_repeated["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["strategy_version"] = {
        "status": "EVIDENCED",
        "version_ref": fake_version,
    }
    for source_id in (
        "owner-account-live-history",
        "portfolio123-model-generation",
    ):
        source = _source(coherently_repeated, source_id)
        source["strategy_version"]["version_refs"]["p123-ai-factor"] = fake_version
        if set(source["strategy_version"]["version_refs"]) == set(
            source["subjects"]["strategy_ids"]
        ):
            source["strategy_version"]["status"] = "EVIDENCED"
    p123_identity = next(
        row
        for row in coherently_repeated["history_corrections"][
            "materialized_segment_identities"
        ]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_identity["strategy_version"] = fake_version
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="differs from accepted history authority",
    ):
        _validate(coherently_repeated)

    rewritten_accepted_version = copy.deepcopy(baseline)
    fake_p123_eu_version = "p123-eu-connected-model:" + "f" * 64
    p123_eu = next(
        row
        for row in rewritten_accepted_version["strategies"]
        if row["strategy_id"] == "p123-eu"
    )
    p123_eu["strategy_version"]["version_ref"] = fake_p123_eu_version
    for source in rewritten_accepted_version["source_segments"]:
        version_refs = source["strategy_version"]["version_refs"]
        if "p123-eu" in version_refs:
            version_refs["p123-eu"] = fake_p123_eu_version
    for correction in rewritten_accepted_version["history_corrections"]["corrections"]:
        if correction["strategy_id"] == "p123-eu":
            correction["strategy_version"]["version_ref"] = fake_p123_eu_version
    for identity in rewritten_accepted_version["history_corrections"][
        "materialized_segment_identities"
    ]:
        if identity["strategy_id"] == "p123-eu":
            identity["strategy_version"] = fake_p123_eu_version
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="differs from accepted history authority",
    ):
        _validate(rewritten_accepted_version)

    partial_source_rewrite = copy.deepcopy(baseline)
    _source(partial_source_rewrite, "ibkr-account-generation")["strategy_version"][
        "version_refs"
    ]["p123-eu"] = fake_p123_eu_version
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "source strategy design/version differs from product authority: "
            "ibkr-account-generation/p123-eu"
        ),
    ):
        _validate(partial_source_rewrite)

    partial_correction_rewrite = copy.deepcopy(baseline)
    correction = next(
        row
        for row in partial_correction_rewrite["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-backtest-not-captured"
    )
    correction["strategy_version"]["version_ref"] = fake_p123_eu_version
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "history strategy version differs from product authority: "
            "p123-eu-backtest-not-captured"
        ),
    ):
        _validate(partial_correction_rewrite)

    missing_source_assertion = copy.deepcopy(baseline)
    ibkr_version = _source(missing_source_assertion, "ibkr-account-generation")[
        "strategy_version"
    ]
    del ibkr_version["version_refs"]["p123-eu"]
    ibkr_version["status"] = "UNRESOLVED"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "source strategy design/version projection differs from accepted "
            "history authority: ibkr-account-generation"
        ),
    ):
        _validate(missing_source_assertion)

    invented_source_assertion = copy.deepcopy(baseline)
    design_version = _source(
        invented_source_assertion, "strategy-design-and-configuration"
    )["strategy_version"]
    design_version["version_refs"]["p123-eu"] = next(
        row["strategy_version"]["version_ref"]
        for row in baseline["strategies"]
        if row["strategy_id"] == "p123-eu"
    )
    design_version["status"] = "MIXED"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "source strategy design/version projection differs from accepted "
            "history authority: strategy-design-and-configuration"
        ),
    ):
        _validate(invented_source_assertion)

    erased_correction_assertion = copy.deepcopy(baseline)
    erased_correction = next(
        row
        for row in erased_correction_assertion["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-backtest-not-captured"
    )
    erased_correction["strategy_version"] = {
        "status": "UNRESOLVED",
        "version_ref": None,
        "compatibility": "UNRESOLVED",
        "authority": "CURRENT_CONNECTED_MODEL_VERSION_NOT_ASSERTED",
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "history correction version projection differs from accepted "
            "history authority"
        ),
    ):
        _validate(erased_correction_assertion)

    swapped_correction_receipts = copy.deepcopy(baseline)
    p123_ai_correction = next(
        row
        for row in swapped_correction_receipts["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-ai-backtest-candidate"
    )
    replacement_receipts = {
        row["artifact_role"]: copy.deepcopy(row)
        for row in p123_ai_correction["source_receipts"]
    }
    p123_eu_correction = next(
        row
        for row in swapped_correction_receipts["history_corrections"]["corrections"]
        if row["correction_id"] == "p123-eu-provider-live"
    )
    p123_eu_correction["source_receipts"] = [
        row
        for row in p123_eu_correction["source_receipts"]
        if row["artifact_role"]
        not in {"portfolio123-managed-metadata", "portfolio123-equity-curve"}
    ] + [
        replacement_receipts["portfolio123-simulation-returns"],
        replacement_receipts["portfolio123-strategy-relationships"],
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "history correction version projection differs from accepted "
            "history authority"
        ),
    ):
        _validate(swapped_correction_receipts)

    rewritten_segment_identity = copy.deepcopy(baseline)
    p123_ai_segment = next(
        row
        for row in rewritten_segment_identity["history_corrections"][
            "materialized_segment_identities"
        ]
        if row["strategy_id"] == "p123-ai-factor"
        and row["evidence_layer"] == "ACCOUNT_LIVE"
    )
    p123_ai_segment["sha256"] = "0" * 64
    p123_ai = next(
        row
        for row in rewritten_segment_identity["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["live_implementation_identity"]["version_ref"] = (
        "account-live-segment:" + "0" * 64
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "materialized history version projection differs from accepted "
            "history authority"
        ),
    ):
        _validate(rewritten_segment_identity)

    transition_removed = copy.deepcopy(baseline)
    for sleeve in transition_removed["current_census"]["account_sleeves"]:
        for assignment in sleeve["assignments"]:
            assignment["transition_group_id"] = None
    transition_projection = contract_tool.census_owner_decision_projection(
        transition_removed["current_census"], transition_removed["strategies"]
    )
    assert (
        hashlib.sha256(
            contract_tool.canonical_json_bytes(transition_projection)
        ).hexdigest()
        != approved_digest
    )

    july_split = copy.deepcopy(baseline)
    tt_sleeve = next(
        row
        for row in july_split["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-tt-01"
    )
    tt_sleeve["assignments"][0]["effective_end"] = "2026-07-14"
    tt_sleeve["assignments"].append(
        {
            "strategy_id": "tt-portfolio",
            "status": "FUNDED_MEMBERSHIP",
            "effective_start": "2026-07-31",
            "effective_end": None,
            "authority": "UNAPPROVED_REENTRY",
            "transition_group_id": None,
        }
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="invents a July exit or re-entry",
    ):
        contract_tool.census_owner_decision_projection(
            july_split["current_census"], july_split["strategies"]
        )

    reintroduced = copy.deepcopy(baseline)
    registry_source = _source(reintroduced, "current-strategy-registry")
    registry_source["subjects"]["strategy_ids"].append("composer-holy-grail")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="includes excluded strategies",
    ):
        _validate(reintroduced)


def test_accepted_provider_ledger_history_identity_cannot_be_coherently_relabelled() -> (
    None
):
    """History lineage is a fixed receipt, not a replaceable generation label."""

    replacements = {
        "2961ab627b6dccc444d1d4ae73c0d38b455b4e2149a5246efaa383800d847d62": ("a" * 64),
        "fdd17e8459501c39d71a99d6042b4351092cd5f236c5ce3222868019755d5ed2": ("b" * 64),
        "78e812e76c38bf08a9b66a533020dbffeb6cb8e1f3b4b780269399d20c52d9e1": ("c" * 64),
    }

    def replace_lineage(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_lineage(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_lineage(item) for item in value]
        if isinstance(value, str):
            result = value
            for old, new in replacements.items():
                result = result.replace(old, new)
            return result
        return value

    relabelled = replace_lineage(copy.deepcopy(_contract()))
    assert isinstance(relabelled, dict)
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="provider-ledger history lineage differs",
    ):
        _validate(relabelled)


def test_two_vintage_history_accepts_2961_active_and_retains_07c590_echo() -> None:
    """Active owner history advances without relabelling deprecated Echo evidence."""

    active_generation = (
        "2961ab627b6dccc444d1d4ae73c0d38b455b4e2149a5246efaa383800d847d62"
    )
    active_manifest = "fdd17e8459501c39d71a99d6042b4351092cd5f236c5ce3222868019755d5ed2"
    active_catalog = "78e812e76c38bf08a9b66a533020dbffeb6cb8e1f3b4b780269399d20c52d9e1"
    legacy_generation = (
        "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e"
    )
    legacy_manifest = "1e8dcc72327dde1c4165b881a2f9b3c3843f063a415a0006b9cf54b9186be0db"
    legacy_catalog = "0ec0ea74948da163295524be56afadc4ed485f25f10ed8dac6d329aac6772b56"

    contract = _contract()
    owner_source = _source(contract, "owner-account-live-history")
    owner_source["current_generation"] = {
        "generation_id": active_generation,
        "sha256": active_manifest,
        "as_of": "2026-08-16",
    }

    active_segments = {
        row["strategy_id"]: row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["source_id"] == "owner-account-live-history"
    }
    assert set(active_segments) == {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
    }
    for strategy_id, segment in active_segments.items():
        segment["source_generation_id"] = active_generation
        segment["source_generation_sha256"] = active_manifest
        strategy = next(
            row for row in contract["strategies"] if row["strategy_id"] == strategy_id
        )
        strategy["live_implementation_identity"]["generation_id"] = active_generation
        strategy["live_implementation_identity"]["receipt_sha256"] = active_manifest

    for correction in contract["history_corrections"]["corrections"]:
        if correction["correction_id"] not in {
            "p123-eu-account-live",
            "volatis-account-live",
        }:
            continue
        correction["capture"]["generation_id"] = active_generation
        correction["capture"]["generation_sha256"] = active_manifest
        for receipt in correction["source_receipts"]:
            if receipt["authority_root"] != "PROVIDER_LEDGER":
                continue
            receipt["generation_id"] = active_generation
            suffix = receipt["artifact_selector"].split("/")[-1]
            receipt["artifact_selector"] = f"generations/{active_generation}/{suffix}"
            receipt["sha256"] = (
                active_manifest if suffix == "generation.json" else active_catalog
            )

    echo = next(
        row
        for row in contract["strategies"]
        if row["strategy_id"] == "echotrade-growth-pivots"
    )
    assert echo["product_requirement"] == "EXCLUDED"
    assert echo["lifecycle_status"] == "DEPRECATED_HISTORY_ONLY"
    assert echo["live_implementation_identity"]["generation_id"] == legacy_generation
    assert echo["live_implementation_identity"]["receipt_sha256"] == legacy_catalog
    echo_correction = next(
        row
        for row in contract["history_corrections"]["corrections"]
        if row["correction_id"] == "echotrade-event-only-account-live-rejected"
    )
    assert echo_correction["capture"]["generation_id"] == legacy_generation
    assert echo_correction["capture"]["generation_sha256"] == legacy_manifest

    volatis = next(
        row
        for row in contract["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    assert volatis["live_implementation_identity"]["generation_id"].startswith("948e7c")
    robuxio = next(
        row for row in contract["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert robuxio["live_implementation_identity"]["generation_id"].startswith("3badac")

    _validate(contract)
    assert contract_tool._verify_strategy_version_history_evidence(contract) == 3


def test_owner_account_history_rebinds_to_corrected_2961_ledger_generation() -> None:
    """The active owner-history lineage follows the corrected governed ledger."""

    generation_id = "2961ab627b6dccc444d1d4ae73c0d38b455b4e2149a5246efaa383800d847d62"
    manifest_sha256 = "fdd17e8459501c39d71a99d6042b4351092cd5f236c5ce3222868019755d5ed2"
    catalog_sha256 = "78e812e76c38bf08a9b66a533020dbffeb6cb8e1f3b4b780269399d20c52d9e1"
    contract = _contract()
    owner_source = _source(contract, "owner-account-live-history")
    assert owner_source["current_generation"] == {
        "generation_id": generation_id,
        "sha256": manifest_sha256,
        "as_of": "2026-08-16",
    }
    active_segments = [
        row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["source_id"] == "owner-account-live-history"
    ]
    assert len(active_segments) == 5
    assert all(
        row["source_generation_id"] == generation_id
        and row["source_generation_sha256"] == manifest_sha256
        for row in active_segments
    )
    expected_provider_receipts = {
        "p123-eu-account-live": {manifest_sha256, catalog_sha256},
        "volatis-account-live": {manifest_sha256},
    }
    for correction_id, expected_receipts in expected_provider_receipts.items():
        correction = _history_correction(contract, correction_id)
        assert correction["capture"]["generation_id"] == generation_id
        assert correction["capture"]["generation_sha256"] == manifest_sha256
        assert {
            receipt["sha256"]
            for receipt in correction["source_receipts"]
            if receipt["authority_root"] == "PROVIDER_LEDGER"
        } == expected_receipts
    _validate(contract)


def test_two_vintage_history_rejects_legacy_rollbacks_and_rewrites() -> None:
    """The deprecated event-only class cannot authorize active-lineage changes."""

    active_generation = (
        "2961ab627b6dccc444d1d4ae73c0d38b455b4e2149a5246efaa383800d847d62"
    )
    active_manifest = "fdd17e8459501c39d71a99d6042b4351092cd5f236c5ce3222868019755d5ed2"
    legacy_generation = (
        "07c590e5b4958c9902304f813489fbc81dad1bee7a3be36e0c15e2598949f15e"
    )
    legacy_manifest = "1e8dcc72327dde1c4165b881a2f9b3c3843f063a415a0006b9cf54b9186be0db"

    active_rollback = _contract()
    p123_eu = _history_correction(active_rollback, "p123-eu-account-live")
    p123_eu["capture"]["generation_id"] = legacy_generation
    p123_eu["capture"]["generation_sha256"] = legacy_manifest
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history correction generation differs from source",
    ):
        _validate(active_rollback)

    echo_rewrite = _contract()
    echo = _history_correction(
        echo_rewrite, "echotrade-event-only-account-live-rejected"
    )
    echo["capture"]["generation_id"] = active_generation
    echo["capture"]["generation_sha256"] = active_manifest
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="deprecated event-only history must retain its legacy receipt",
    ):
        _validate(echo_rewrite)

    volatis_rewrite = _contract()
    volatis = next(
        row
        for row in volatis_rewrite["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    volatis["live_implementation_identity"]["generation_id"] = active_generation
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="live implementation identity differs from accepted segment",
    ):
        _validate(volatis_rewrite)

    lifecycle_rewrite = _contract()
    echo_strategy = next(
        row
        for row in lifecycle_rewrite["strategies"]
        if row["strategy_id"] == "echotrade-growth-pivots"
    )
    echo_strategy["lifecycle_status"] = "IMMATERIAL_EXCLUDED"
    with pytest.raises(contract_tool.FinancialMvpContractError):
        _validate(lifecycle_rewrite)


def test_census_fails_on_missing_duplicate_overlap_and_stale_provider_binding() -> None:
    contract = _contract()

    missing = copy.deepcopy(contract)
    missing["current_census"]["account_sleeves"].pop()
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="19 redacted sleeves",
    ):
        _validate(missing)

    duplicate = copy.deepcopy(contract)
    duplicate["current_census"]["account_sleeves"][1]["sleeve_alias"] = duplicate[
        "current_census"
    ]["account_sleeves"][0]["sleeve_alias"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="duplicate census sleeve",
    ):
        _validate(duplicate)

    overlap = copy.deepcopy(contract)
    qsi = next(
        row
        for row in overlap["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-qsi-01"
    )
    qsi["assignments"][1]["effective_start"] = "2026-01-04"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="assignments overlap",
    ):
        _validate(overlap)

    stale = copy.deepcopy(contract)
    ibkr = next(
        row
        for row in stale["current_census"]["provider_lanes"]
        if row["provider_id"] == "IBKR"
    )
    ibkr["generation"]["generation_id"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="provider/source generation differs",
    ):
        _validate(stale)

    balanced_lane_swap = copy.deepcopy(contract)
    ibkr_lane = next(
        row
        for row in balanced_lane_swap["current_census"]["provider_lanes"]
        if row["provider_id"] == "IBKR"
    )
    schwab_lane = next(
        row
        for row in balanced_lane_swap["current_census"]["provider_lanes"]
        if row["provider_id"] == "SCHWAB"
    )
    ibkr_lane["account_sleeve_ids"].remove("ibkr-reserve-01")
    schwab_lane["account_sleeve_ids"].remove("schwab-reserve-01")
    ibkr_lane["account_sleeve_ids"].append("schwab-reserve-01")
    schwab_lane["account_sleeve_ids"].append("ibkr-reserve-01")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="sleeve differs from declared provider lane",
    ):
        _validate(balanced_lane_swap)

    coherent_lane_swap = copy.deepcopy(contract)
    ibkr_lane = next(
        row
        for row in coherent_lane_swap["current_census"]["provider_lanes"]
        if row["provider_id"] == "IBKR"
    )
    schwab_lane = next(
        row
        for row in coherent_lane_swap["current_census"]["provider_lanes"]
        if row["provider_id"] == "SCHWAB"
    )
    ibkr_sleeve = next(
        row
        for row in coherent_lane_swap["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "ibkr-reserve-01"
    )
    schwab_sleeve = next(
        row
        for row in coherent_lane_swap["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-reserve-01"
    )
    ibkr_lane["account_sleeve_ids"].remove("ibkr-reserve-01")
    schwab_lane["account_sleeve_ids"].remove("schwab-reserve-01")
    ibkr_lane["account_sleeve_ids"].append("schwab-reserve-01")
    schwab_lane["account_sleeve_ids"].append("ibkr-reserve-01")
    ibkr_sleeve["provider_id"] = "SCHWAB"
    schwab_sleeve["provider_id"] = "IBKR"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="sleeve provider namespace differs",
    ):
        _validate(coherent_lane_swap)

    foreign_provider_sources = copy.deepcopy(contract)
    ibkr_sleeve = next(
        row
        for row in foreign_provider_sources["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "ibkr-reserve-01"
    )
    schwab_sleeve = next(
        row
        for row in foreign_provider_sources["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-reserve-01"
    )
    ibkr_sleeve["source_segment_ids"].append("schwab-account-generation")
    schwab_sleeve["source_segment_ids"].append("ibkr-account-generation")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="sleeve includes a foreign provider source",
    ):
        _validate(foreign_provider_sources)

    invented_live_identity = copy.deepcopy(contract)
    p123_ai = next(
        row
        for row in invented_live_identity["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["live_implementation_identity"] = {
        "status": "EVIDENCED",
        "version_ref": "account-live-segment:" + "0" * 64,
        "generation_id": "1" * 64,
        "receipt_sha256": "2" * 64,
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="live implementation identity differs from accepted segment",
    ):
        _validate(invented_live_identity)


def test_all_four_outcomes_are_required_and_generic_unavailable_is_not_accepted() -> (
    None
):
    contract = _contract()
    outcomes = contract["financial_outcomes"]
    assert [row["outcome_id"] for row in outcomes] == list(contract_tool.OUTCOME_IDS)
    assert all(row["product_requirement"] == "REQUIRED" for row in outcomes)
    assert all(
        (row["readiness"] == "READY") == (row["blockers"] == []) for row in outcomes
    )
    assert _outcome(contract, "DEPENDENCE")["readiness"] == "READY"
    assert all("UNAVAILABLE" not in row["accepted_statuses"] for row in outcomes)


def test_invested_capital_is_not_nav_hwm_or_gains() -> None:
    invested = _outcome(_contract(), "INVESTED_CAPITAL")["calculation_contract"]
    assert invested["definition"] == (
        "CUMULATIVE_EXTERNAL_CONTRIBUTIONS_MINUS_WITHDRAWALS"
    )
    assert invested["gains_treatment"] == "EXCLUDED"
    assert invested["internal_transfer_aggregate_effect"] == "ZERO"
    assert set(invested["flow_classifications"]) == {
        "OPENING_CONTRIBUTED_BASIS",
        "IN_KIND_FUNDING",
        "EXTERNAL_CONTRIBUTION",
        "EXTERNAL_WITHDRAWAL",
        "INTERNAL_TRANSFER",
        "DIVIDEND_INTEREST_REINVESTMENT",
        "FEE_TAX_REBATE_EXPENSE",
        "MARGIN_FINANCING",
        "PREDECESSOR_TRANSITION",
        "CORRECTION_REVERSAL",
    }
    treatments = invested["classification_treatments"]
    assert treatments["EXTERNAL_CONTRIBUTION"]["selected_treatment"] == "INCREASE"
    assert treatments["EXTERNAL_WITHDRAWAL"]["selected_treatment"] == "DECREASE"
    assert treatments["INTERNAL_TRANSFER"]["selected_treatment"] == (
        "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO"
    )
    assert treatments["DIVIDEND_INTEREST_REINVESTMENT"]["selected_treatment"] == (
        "NO_INVESTED_CAPITAL_EFFECT"
    )
    assert treatments["IN_KIND_FUNDING"]["decision_status"] == (
        "SETTLED_BY_EXISTING_POLICY"
    )
    assert invested["flow_timing"]["selected_treatment"] == (
        "EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE; "
        "DATE_ONLY_END_OF_DAY_WITH_INFERRED_TIMING"
    )
    assert invested["fx_base_currency"]["decision_status"] == "APPROVED"
    assert invested["negative_net_contributions"]["selected_treatment"] == (
        "ALLOW_SIGNED_RESULT_WITH_NO_ZERO_FLOOR"
    )
    assert {
        treatments[flow_class]["decision_status"]
        for flow_class in (
            "MARGIN_FINANCING",
            "PREDECESSOR_TRANSITION",
            "CORRECTION_REVERSAL",
        )
    } == {"APPROVED"}
    assert invested["shared_flow_contract"]["current_policy_id"] == "PORT-PERF-1"
    assert invested["change_control"]["downstream_issues"] == [318, 324, 331, 332]


def test_leverage_policy_preserves_returns_without_inflating_invested_capital() -> None:
    invested = _outcome(_contract(), "INVESTED_CAPITAL")["calculation_contract"]
    leverage = invested["leverage_accounting"]
    vector = leverage["synthetic_vector"]
    assert leverage["status"] == "APPROVED"
    assert set(leverage["required_facts"]) == {
        "GROSS_EXPOSURE",
        "BORROWED_PRINCIPAL",
        "FINANCING_COST",
        "NAV",
        "OWNER_EXTERNAL_FLOWS",
    }
    assert set(leverage["affected_metrics"]) == {
        "TOTAL_RETURN",
        "TWR",
        "MWR",
        "HWM_DRAWDOWN",
        "DECOMPOSITION_DEPENDENCE",
    }
    assert vector["owner_contributed_capital"] == "100"
    assert vector["borrowed_principal"] == "50"
    assert vector["gross_assets_end"] == "164"
    assert vector["ending_nav"] == "114"
    assert vector["invested_capital"] == "100"
    assert vector["total_return"] == vector["twr"] == vector["mwr_one_year"] == "0.14"
    contract = _contract()
    leverage_classes = {
        "FINANCING_LIABILITY",
        "LEVERAGE_EXPOSURE",
        "FINANCING_COST",
    }
    for source_id in contract_tool.REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS:
        assert leverage_classes <= set(_source(contract, source_id)["data_classes"])

    changed = _contract()
    changed_vector = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"][
        "leverage_accounting"
    ]["synthetic_vector"]
    changed_vector["total_return"] = "0.15"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="leverage synthetic value differs: total_return",
    ):
        _validate(changed)

    missing_source_fact = _contract()
    ibkr = _source(missing_source_fact, "ibkr-account-generation")
    ibkr["data_classes"].remove("FINANCING_LIABILITY")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="account source lacks leverage facts: ibkr-account-generation",
    ):
        _validate(missing_source_fact)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (
            "MARGIN_PRINCIPAL",
            "MARGIN_PRINCIPAL_COUNTS_AS_OWNER_INVESTED_CAPITAL_AND_EXTERNAL_FLOW",
        ),
        (
            "TOTAL_RETURN",
            "EXCLUDE_FINANCING_COST_AND_USE_UNLEVERED_ASSETS",
        ),
        (
            "DECOMPOSITION_DEPENDENCE",
            "IGNORE_LEVERAGED_RETURNS_AND_EXPOSURE",
        ),
    ],
)
def test_unapproved_leverage_semantic_reversal_fails(mutation: str, value: str) -> None:
    baseline = _contract()
    unchanged_owner_evidence = _owner_evidence_comments(baseline)
    changed = copy.deepcopy(baseline)
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    leverage = invested["leverage_accounting"]
    if mutation == "MARGIN_PRINCIPAL":
        invested["classification_treatments"]["MARGIN_FINANCING"][
            "selected_treatment"
        ] = value
        leverage["invested_capital_treatment"] = value
    else:
        leverage["affected_metrics"][mutation] = value
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner decision receipt differs: MARGIN_AND_LEVERAGE",
    ):
        _validate(changed)
    receipts = invested["owner_decision_receipts"]
    receipts["MARGIN_AND_LEVERAGE"]["payload_sha256"] = (
        contract_tool.owner_decision_receipt_digests(changed)["MARGIN_AND_LEVERAGE"]
    )
    _validate(changed)
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner digest evidence does not approve current decision payloads",
    ):
        contract_tool.reconcile_owner_receipt_evidence(
            changed, unchanged_owner_evidence
        )


def test_current_owner_receipt_evidence_reconciles() -> None:
    contract = _contract()
    comments = _owner_evidence_comments(contract)
    contract_tool.reconcile_owner_receipt_evidence(contract, comments)

    not_owner = copy.deepcopy(comments)
    digest_comment_id = next(
        receipt["digest_comment_id"]
        for receipt in _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"][
            "owner_decision_receipts"
        ].values()
    )
    not_owner[digest_comment_id]["author_association"] = "CONTRIBUTOR"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner evidence author differs",
    ):
        contract_tool.reconcile_owner_receipt_evidence(contract, not_owner)

    edited = copy.deepcopy(comments)
    edited[digest_comment_id]["updated_at"] = "2026-08-17T12:01:00Z"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner evidence comment was edited",
    ):
        contract_tool.reconcile_owner_receipt_evidence(contract, edited)


def test_decomposition_and_dependence_surfaces_are_complete() -> None:
    contract = _contract()
    decomposition = _outcome(contract, "DECOMPOSITION")["calculation_contract"]
    assert decomposition["lenses"] == list(contract_tool.DECOMPOSITION_LENSES)
    assert decomposition["evidence_modes"] == list(contract_tool.EVIDENCE_MODES)
    assert set(decomposition["surfaces"]) == {"STATIC", "DYNAMIC_FLS"}
    dependence = _outcome(contract, "DEPENDENCE")["calculation_contract"]
    assert dependence["evidence_modes"] == list(contract_tool.EVIDENCE_MODES)
    assert dependence["horizons"] == list(contract_tool.DEPENDENCE_HORIZONS)
    assert {
        "PEARSON",
        "SPEARMAN",
        "ROLLING",
        "DOWNSIDE",
        "DRAWDOWN_REGIMES",
        "JOINT_TAIL",
        "CONCEPTUAL_PRIOR",
        "POSTERIOR_STRESS_PSD",
        "NO_DIVERSIFICATION_CREDIT",
        "CENTRAL_CASE_LOSS",
        "CORRELATION_ONE_BOUND",
        "PERFORMANCE",
        "ECONOMIC_DRIVER_CONCENTRATION",
        "IMPLEMENTATION_CONCENTRATION",
    } == set(dependence["analyses"])
    assert dependence["splice"] == (
        "VERSION_COMPATIBLE_PREHISTORY_BEFORE_LIVE_INCEPTION_ONLY"
    )


def test_source_matrix_covers_every_mandated_class_and_provider_family() -> None:
    contract = _contract()
    sources = contract["source_segments"]
    classes = {
        data_class for source in sources for data_class in source["data_classes"]
    }
    assert contract_tool.REQUIRED_SOURCE_CLASSES <= classes
    source_ids = {source["source_id"] for source in sources}
    assert contract_tool.REQUIRED_PROVIDER_SOURCE_IDS <= source_ids
    required_fields = {
        "absence_behavior",
        "acquisition",
        "admission",
        "authority",
        "blockers",
        "capture_status",
        "consumers",
        "current_generation",
        "data_classes",
        "endpoint_or_artifact_selector",
        "evidence_layer",
        "existence_status",
        "freshness",
        "interval",
        "invalidated_outputs",
        "owner_policy_decision",
        "product_requirement",
        "readiness",
        "schema",
        "source_id",
        "source_version",
        "strategy_version",
        "subjects",
        "temporal",
        "units",
    }
    assert all(required_fields <= set(source) for source in sources)
    assert all(
        set(source) - required_fields <= {"declared_gap_policy"} for source in sources
    )
    assert {
        source["source_id"] for source in sources if "declared_gap_policy" in source
    } == {"matrixport-account-generation"}


def test_matrixport_declared_gap_policy_is_authoritative_and_propagated() -> None:
    contract = _contract()
    source = _source(contract, "matrixport-account-generation")
    policy = source["declared_gap_policy"]
    assert policy["authority_issue"] > 0
    assert policy["policy_id"]
    assert policy["provenance"]
    assert policy["reason"]
    assert policy["return_rule"]
    missing = {
        date.fromisoformat(value) for value in policy["declared_missing_source_dates"]
    }
    omitted = {date.fromisoformat(value) for value in policy["omitted_return_dates"]}
    unavailable = {
        date.fromisoformat(value) for value in policy["unavailable_return_dates"]
    }
    assert omitted == {
        value + date.resolution
        for value in missing
        if value + date.resolution not in missing
    }
    assert unavailable == missing | omitted
    assert source["readiness"] == "REQUIRED_NOT_READY"
    assert source["blockers"] == [324, 318]
    admission = source["admission"]
    assert admission["facts"] == [
        "NAV_OBSERVATION",
        "CAPITAL_ANCHOR",
        "FINANCING_LIABILITY",
        "LEVERAGE_EXPOSURE",
        "FINANCING_COST",
    ]
    assert admission["transform"] == "provider-ledger"
    assert admission["version"] == "1"
    assert admission["dataset_disposition_policy"]["policy_id"] == (
        "PROVIDER_ADMIT_OR_DECLARE_V1"
    )
    assert policy["return_transform"] == {
        "facts": [
            "ACCOUNT_LIVE_DAILY_RETURN",
            "ACCOUNT_LIVE_HOLDING_PERIOD_RETURN",
            "PROVIDER_LIVE_DAILY_RETURN",
            "DECLARED_GAP_COVERAGE",
        ],
        "transform": "matrixport-declared-gap-span-returns",
        "version": "1",
    }
    assert [row["span_days"] for row in policy["span_return_receipts"]] == [2, 2]
    robuxio = next(
        row for row in contract["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert "unexplained" not in robuxio["readiness_reason"].lower()

    views = contract_tool.render_views(contract)
    matrix = json.loads(views.source_matrix_json)
    rows = [
        row
        for row in matrix["rows"]
        if row["source_id"] == "matrixport-account-generation"
    ]
    assert len(rows) == 30
    assert all(row["declared_gap_policy"] == policy for row in rows)
    assert all(
        row["declared_gap_policy"] is None
        for row in matrix["rows"]
        if row["source_id"] != "matrixport-account-generation"
    )
    markdown = views.summary_markdown.decode("utf-8")
    assert "## Declared source-gap policies" in markdown
    assert ", ".join(policy["declared_missing_source_dates"]) in markdown
    assert ", ".join(policy["omitted_return_dates"]) in markdown
    assert "matrixport-declared-gap-span-returns@1" in markdown
    assert ", ".join(policy["return_transform"]["facts"]) in markdown

    changed_date = copy.deepcopy(contract)
    changed_policy = _source(changed_date, "matrixport-account-generation")[
        "declared_gap_policy"
    ]
    changed_policy["omitted_return_dates"] = ["2026-01-29", "2026-07-04"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="omitted return dates differ",
    ):
        _validate(changed_date)

    missing_policy = copy.deepcopy(contract)
    del _source(missing_policy, "matrixport-account-generation")["declared_gap_policy"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="lacks the owner-declared gap policy",
    ):
        _validate(missing_policy)


def test_matrixport_owner_span_receipts_and_funded_epoch_reject_coherent_rebinds() -> (
    None
):
    """The source contract cannot relabel August provider history as owner TWR."""

    contract = _contract()
    policy = _source(contract, "matrixport-account-generation")["declared_gap_policy"]
    assert [item["span_days"] for item in policy["span_return_receipts"]] == [2, 2]

    altered_span = copy.deepcopy(contract)
    altered_policy = _source(altered_span, "matrixport-account-generation")[
        "declared_gap_policy"
    ]
    altered_policy["span_return_receipts"][0]["return"] = "-0.017"
    altered_policy["span_return_receipts"][0]["start_endpoint_sha256"] = "a" * 64
    altered_policy["span_return_receipts"][0]["end_endpoint_sha256"] = "b" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="span return receipts differ",
    ):
        _validate(altered_span)

    altered_epoch = copy.deepcopy(contract)
    strategy = next(
        row
        for row in altered_epoch["strategies"]
        if row["strategy_id"] == "robuxio-crypto"
    )
    strategy["funded_membership"]["effective_start"] = "2025-08-26"
    sleeve = next(
        row
        for row in altered_epoch["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "matrixport-robuxio-01"
    )
    sleeve["assignments"][0]["effective_start"] = "2025-08-26"
    source = _source(altered_epoch, "matrixport-account-generation")
    source["interval"].update(
        {"effective_start": "2025-08-26", "observed_start": "2025-08-27"}
    )
    correction = next(
        row
        for row in altered_epoch["history_corrections"]["corrections"]
        if row["correction_id"] == "robuxio-account-live-after-funding"
    )
    correction["interval"].update(
        {"effective_start": "2025-08-26", "observed_start": "2025-08-27"}
    )
    correction["funding_event"].update(
        {"event_date": "2025-08-26", "account_live_start": "2025-08-26"}
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="Robuxio owner-funded boundary differs",
    ):
        _validate(altered_epoch)

    altered_cutoff = copy.deepcopy(contract)
    _source(altered_cutoff, "matrixport-account-generation")["interval"][
        "observed_end"
    ] = "2026-08-16"
    correction = next(
        row
        for row in altered_cutoff["history_corrections"]["corrections"]
        if row["correction_id"] == "robuxio-account-live-after-funding"
    )
    correction["interval"]["observed_end"] = "2026-08-16"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="Robuxio owner-funded boundary differs",
    ):
        _validate(altered_cutoff)


def test_history_existence_capture_and_evidence_layers_are_separate() -> None:
    contract = _contract()
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["existence_status"] == "EXISTS"
    assert p123["capture_status"] == "CAPTURED_GOVERNED"
    assert p123["evidence_layer"] == "PROVIDER_LIVE"
    owner_live = _source(contract, "owner-account-live-history")
    assert owner_live["evidence_layer"] == "ACCOUNT_LIVE"
    assert (
        "NEVER_RELABEL_AS_ACCOUNT_LIVE"
        in _source(contract, "provider-track-record")["interval"]["splice_policy"]
    )
    assert {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"} <= {
        row["evidence_layer"] for row in contract["source_segments"]
    }


def test_truthful_history_corrections_bind_layers_funding_and_versions() -> None:
    contract = _contract()
    history = contract["history_corrections"]
    assert history["policy_id"] == "PORT-HISTORY-TRUTH-1"
    assert history["owner_issue"] == 319
    rows = history["corrections"]
    assert len(rows) == 11
    segment_identities = history["materialized_segment_identities"]
    assert [row["segment_id"] for row in segment_identities] == sorted(
        row["segment_id"] for row in segment_identities
    )
    assert {row["segment_id"] for row in segment_identities} == {
        "grizzlybears-vtma:account-live",
        "p123-ai-factor:account-live",
        "p123-eu:account-live",
        "qsi:account-live",
        "robuxio-crypto:account-live",
        "robuxio-crypto:provider-live-before-funding",
        "tt-portfolio:account-live",
        "volatis-strategy:account-live",
    }
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in segment_identities:
        generation = sources[identity["source_id"]]["current_generation"]
        assert (
            identity["source_generation_id"],
            identity["source_generation_sha256"],
        ) == (generation["generation_id"], generation["sha256"])
        assert identity["canonicalization"] == (
            "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1"
        )
    rendered = contract_tool.render_views(contract)
    assert b"### Materialized segment value authority" in rendered.summary_markdown
    assert segment_identities[0]["sha256"].encode("utf-8") in rendered.summary_markdown
    assert {(row["strategy_id"], row["evidence_layer"]) for row in rows} == {
        ("robuxio-crypto", "PROVIDER_TRACK_RECORD"),
        ("robuxio-crypto", "PROVIDER_LIVE"),
        ("robuxio-crypto", "ACCOUNT_LIVE"),
        ("p123-ai-factor", "BACKTEST"),
        ("p123-eu", "BACKTEST"),
        ("p123-eu", "PROVIDER_LIVE"),
        ("p123-eu", "ACCOUNT_LIVE"),
        ("volatis-strategy", "BACKTEST"),
        ("volatis-strategy", "PROVIDER_LIVE"),
        ("volatis-strategy", "ACCOUNT_LIVE"),
        ("echotrade-growth-pivots", "ACCOUNT_LIVE"),
    }

    robux_live = _history_correction(contract, "robuxio-account-live-after-funding")
    assert robux_live["funding_event"]["event_date"] == "2025-12-06"
    assert robux_live["interval"]["observed_start"] == "2025-12-07"
    assert robux_live["interval"]["assignment_date"] == "2025-08-26"
    assert robux_live["mode_admission"] == {
        "LIVE_ONLY": "INCLUDE_SELECTED_LIVE",
        "LIVE_PLUS_BACKTEST": "INCLUDE_SELECTED_LIVE",
    }
    robux_provider = _history_correction(
        contract, "robuxio-provider-live-before-funding"
    )
    assert robux_provider["interval"]["observed_end"] == "2025-12-05"
    assert set(robux_provider["mode_admission"].values()) == {"EXCLUDE_COMPARISON_ONLY"}

    p123_provider = _history_correction(contract, "p123-eu-provider-live")
    assert p123_provider["capture"]["status"] == "CAPTURED_GOVERNED"
    assert p123_provider["strategy_version"]["compatibility"] == "EXACT_MATCH"
    assert p123_provider["interval"]["observed_start"] == "2025-11-13"
    assert p123_provider["derived_evidence_identity"] == {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 157,
        "observed_end": "2026-08-13",
        "observed_start": "2025-11-13",
        "raw_level_observations": 197,
        "return_observations": 196,
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    rendered_history = contract_tool.render_views(contract).summary_markdown
    assert (
        b"197 levels / 196 returns / 157 equity overlaps / "
        b"WEEKDAY_SOURCE_CALENDAR / Cash Added: "
        b"UNRESOLVED_NOT_USED_IN_RETURN_FORMULA / "
        b"P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1" in rendered_history
    )
    assert (
        _source(contract, "portfolio123-model-generation")["strategy_version"][
            "version_refs"
        ]["p123-eu"]
        == p123_provider["strategy_version"]["version_ref"]
    )
    p123_ai_backtest = _history_correction(contract, "p123-ai-backtest-candidate")
    assert p123_ai_backtest["derived_backtest_identity"] == {
        "index_observations": 1599,
        "observed_end": "2026-06-26",
        "observed_start": "2020-05-12",
        "relationship_type": "live_sim_twin",
        "return_observations": 1598,
        "source_labels": ["api"],
    }
    assert p123_ai_backtest["mode_admission"] == {
        "LIVE_ONLY": "NOT_APPLICABLE",
        "LIVE_PLUS_BACKTEST": "REQUIRED_NOT_READY",
    }
    assert (
        b"1599 index levels / 1598 returns / 2020-05-12..2026-06-26 / "
        b"live_sim_twin / sources: api" in rendered_history
    )
    p123_backtest = _history_correction(contract, "p123-eu-backtest-not-captured")
    assert p123_backtest["history_existence"]["status"] == "EXISTS"
    assert p123_backtest["capture"]["status"] == "NOT_CAPTURED"
    assert p123_backtest["strategy_version"]["compatibility"] == "UNRESOLVED"

    volatis_live = _history_correction(contract, "volatis-account-live")
    assert volatis_live["funding_event"]["event_date"] == "2024-10-15"
    assert volatis_live["interval"]["observed_start"] == "2024-10-16"
    echotrade = _history_correction(
        contract, "echotrade-event-only-account-live-rejected"
    )
    assert echotrade["history_existence"] == {
        "status": "EXISTS",
        "authority": "HASH_BOUND_PROVIDER_LEDGER_EVENT_ONLY_RECONSTRUCTION",
    }
    assert echotrade["interval"] == {
        "assignment_date": "2025-09-27",
        "observed_start": "2025-10-01",
        "observed_end": "2025-11-24",
        "effective_start": None,
        "effective_end": None,
    }
    assert echotrade["readiness"] == "READY"
    assert echotrade["blockers"] == []
    assert set(echotrade["mode_admission"].values()) == {
        "EXCLUDE_EVENT_ONLY_RECONSTRUCTION"
    }
    assert echotrade["funding_event"]["status"] == "UNRESOLVED"
    rejected_identity = echotrade["rejected_evidence_identity"]
    provider_ledger_catalog_receipt = next(
        receipt
        for receipt in echotrade["source_receipts"]
        if receipt["artifact_role"] == "provider-ledger-catalog"
    )
    assert rejected_identity == {
        "canonicalization": "TARGET_FIELDS_CANONICAL_JSON_V1",
        "return_observations": 13,
        "sha256": "55d339e5299607daf72b44be72a03cc13998d34e81fc42d8e0a756152347ef4f",
        "source_receipt_sha256": provider_ledger_catalog_receipt["sha256"],
    }
    assert (
        "EARLIER_PURCHASE_LACKED_ACCEPTED_EVENT_TIME_VALUE"
        in (volatis_live["funding_event"]["authority"])
    )
    volatis_backtest = _history_correction(contract, "volatis-backtest-not-proved")
    assert volatis_backtest["history_existence"]["status"] == "UNRESOLVED"
    assert volatis_backtest["capture"]["status"] == "NOT_CAPTURED"
    owner_versions = _source(contract, "owner-account-live-history")[
        "strategy_version"
    ]["version_refs"]
    assert owner_versions == {
        "p123-eu": p123_provider["strategy_version"]["version_ref"],
        "robuxio-crypto": robux_live["strategy_version"]["version_ref"],
        "volatis-strategy": volatis_live["strategy_version"]["version_ref"],
    }
    p123_live = _history_correction(contract, "p123-eu-account-live")
    assert "INTERNAL_TRANSFER_IN" in p123_live["funding_event"]["authority"]
    assert (
        "ZERO_AGGREGATE_INVESTED_CAPITAL_EFFECT"
        in (p123_live["funding_event"]["authority"])
    )
    assert "EXTERNAL_FLOW" not in p123_live["funding_event"]["authority"]
    assert p123_provider["readiness"] == "REQUIRED_NOT_READY"
    assert p123_provider["blockers"] == [303]
    assert any(
        receipt["manifest_artifact_id"] == "tables/har_managed_performance.parquet"
        for receipt in p123_provider["source_receipts"]
    )
    assert (
        "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1"
        in p123_provider["disposition_reason"]
    )
    assert "No provider-live return is admitted" in p123_provider["disposition_reason"]

    reordered = copy.deepcopy(contract)
    reordered["history_corrections"]["materialized_segment_identities"][0:2] = reversed(
        reordered["history_corrections"]["materialized_segment_identities"][0:2]
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="segment identities are not canonical",
    ):
        _validate(reordered)

    wrong_receipt = copy.deepcopy(contract)
    wrong_receipt["history_corrections"]["materialized_segment_identities"][0][
        "source_generation_sha256"
    ] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="source receipt differs",
    ):
        _validate(wrong_receipt)


def test_robuxio_materialized_identity_is_daily_only_and_cannot_be_rebound() -> None:
    """The epoch HPR spans are coverage evidence, never daily segment rows."""

    contract = _contract()
    identity = next(
        row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "robuxio-crypto:account-live"
    )
    assert {
        key: identity[key]
        for key in ("observed_start", "observed_end", "return_observations", "sha256")
    } == {
        "observed_start": "2025-12-07",
        "observed_end": "2026-08-13",
        "return_observations": 246,
        "sha256": "65c115d3a915d24af57c357865f1edf887f4b68d6f8a3f1ea8782b6ac90757c0",
    }
    assert "epoch_holding_period_link_observations" not in identity
    _validate(contract)

    def rebind_live_identity(candidate: dict[str, object], value: str) -> None:
        strategy = next(
            row
            for row in candidate["strategies"]  # type: ignore[index]
            if row["strategy_id"] == "robuxio-crypto"
        )
        strategy["live_implementation_identity"]["version_ref"] = (  # type: ignore[index]
            f"account-live-segment:{value}"
        )

    stale = copy.deepcopy(contract)
    stale_identity = next(
        row
        for row in stale["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "robuxio-crypto:account-live"
    )
    stale_identity.update(
        {
            "observed_end": "2026-08-16",
            "return_observations": 249,
            "sha256": "66bce89cab2a49833bfc47afe7db0b1d0aa06486f3e210cd172d319346cfd7c3",
        }
    )
    rebind_live_identity(stale, stale_identity["sha256"])
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="materialized history version projection differs",
    ):
        _validate(stale)

    hpr_folded = copy.deepcopy(contract)
    folded_identity = next(
        row
        for row in hpr_folded["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "robuxio-crypto:account-live"
    )
    folded_identity.update(
        {
            "return_observations": 248,
            "sha256": "f" * 64,
        }
    )
    rebind_live_identity(hpr_folded, folded_identity["sha256"])
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="materialized history version projection differs",
    ):
        _validate(hpr_folded)


def test_volatis_account_live_materialized_authority_rejects_raw_contribution_series() -> (
    None
):
    accepted_sha256 = "c2d93268f9b91eead304459ce029b8bf4b1110ca28d0bb8190361ab4df43183f"
    rejected_raw_contribution_sha256 = (
        "f989eed33cd7ce8e17d63acb5065032e56d511a765b4f0684e281df043d27016"
    )
    accepted = copy.deepcopy(_contract())
    segment = next(
        row
        for row in accepted["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "volatis-strategy:account-live"
    )
    volatis = next(
        row
        for row in accepted["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    segment["sha256"] = accepted_sha256
    volatis["live_implementation_identity"]["version_ref"] = (
        f"account-live-segment:{accepted_sha256}"
    )

    _validate(accepted)

    rejected = copy.deepcopy(accepted)
    rejected_segment = next(
        row
        for row in rejected["history_corrections"]["materialized_segment_identities"]
        if row["segment_id"] == "volatis-strategy:account-live"
    )
    rejected_volatis = next(
        row
        for row in rejected["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    rejected_segment["sha256"] = rejected_raw_contribution_sha256
    rejected_volatis["live_implementation_identity"]["version_ref"] = (
        f"account-live-segment:{rejected_raw_contribution_sha256}"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match=(
            "materialized history version projection differs from accepted "
            "history authority"
        ),
    ):
        _validate(rejected)


def test_history_correction_omission_and_capture_conflation_fail() -> None:
    missing = copy.deepcopy(_contract())
    missing["history_corrections"]["corrections"].pop()
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history correction layers differ",
    ):
        _validate(missing)

    uncaptured_with_generation = copy.deepcopy(_contract())
    row = _history_correction(
        uncaptured_with_generation, "robuxio-provider-live-before-funding"
    )
    row["capture"]["status"] = "NOT_CAPTURED"
    row["capture"]["capture_defect"] = "SYNTHETIC_MISSING_CAPTURE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="uncaptured history asserts a generation",
    ):
        _validate(uncaptured_with_generation)


def test_provider_history_cannot_be_relabelled_or_assignment_funded() -> None:
    relabelled = copy.deepcopy(_contract())
    provider = _history_correction(relabelled, "p123-eu-provider-live")
    provider["mode_admission"]["LIVE_ONLY"] = "INCLUDE_SELECTED_LIVE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="provider history was relabeled",
    ):
        _validate(relabelled)

    assignment_funded = copy.deepcopy(_contract())
    account = _history_correction(
        assignment_funded, "robuxio-account-live-after-funding"
    )
    account["funding_event"]["authority"] = "ASSIGNMENT_DATE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="assignment date cannot be funding authority",
    ):
        _validate(assignment_funded)


def test_history_truth_recurrence_mutations_fail_closed() -> None:
    unbound_echo = copy.deepcopy(_contract())
    echo = _history_correction(
        unbound_echo, "echotrade-event-only-account-live-rejected"
    )
    echo["rejected_evidence_identity"]["source_receipt_sha256"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="rejected evidence is not bound",
    ):
        _validate(unbound_echo)

    p123_wrong_owner = copy.deepcopy(_contract())
    provider = _history_correction(p123_wrong_owner, "p123-eu-provider-live")
    provider["blockers"] = [320]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unadmitted transform disposition",
    ):
        _validate(p123_wrong_owner)

    p123_unbound_transform = copy.deepcopy(_contract())
    provider = _history_correction(p123_unbound_transform, "p123-eu-provider-live")
    provider["source_receipts"] = [
        receipt
        for receipt in provider["source_receipts"]
        if receipt["manifest_artifact_id"] != "tables/har_managed_performance.parquet"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unadmitted transform disposition",
    ):
        _validate(p123_unbound_transform)

    p123_rewritten_identity = copy.deepcopy(_contract())
    provider = _history_correction(p123_rewritten_identity, "p123-eu-provider-live")
    provider["derived_evidence_identity"]["return_observations"] = 155
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived evidence identity differs",
    ):
        _validate(p123_rewritten_identity)

    p123_impossible_overlap = copy.deepcopy(_contract())
    provider = _history_correction(p123_impossible_overlap, "p123-eu-provider-live")
    provider["derived_evidence_identity"]["equity_overlap_observations"] = 198
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived evidence identity differs",
    ):
        _validate(p123_impossible_overlap)

    p123_ai_rewritten_identity = copy.deepcopy(_contract())
    ai_backtest = _history_correction(
        p123_ai_rewritten_identity, "p123-ai-backtest-candidate"
    )
    ai_backtest["derived_backtest_identity"]["observed_start"] = "2020-05-13"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived backtest identity differs",
    ):
        _validate(p123_ai_rewritten_identity)

    p123_ai_unbound = copy.deepcopy(_contract())
    ai_backtest = _history_correction(p123_ai_unbound, "p123-ai-backtest-candidate")
    ai_backtest["source_receipts"] = [
        receipt
        for receipt in ai_backtest["source_receipts"]
        if receipt["manifest_artifact_id"] != "tables/strategy_relationships.parquet"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unresolved version disposition",
    ):
        _validate(p123_ai_unbound)

    p123_external = copy.deepcopy(_contract())
    account = _history_correction(p123_external, "p123-eu-account-live")
    account["funding_event"]["authority"] = "FIRST_HASH_BOUND_IBKR_EXTERNAL_FLOW"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="misclassified as external capital",
    ):
        _validate(p123_external)

    early_volatis = copy.deepcopy(_contract())
    account = _history_correction(early_volatis, "volatis-account-live")
    account["funding_event"]["event_date"] = "2024-10-13"
    account["funding_event"]["account_live_start"] = "2024-10-13"
    account["interval"]["effective_start"] = "2024-10-13"
    account["interval"]["observed_start"] = "2024-10-14"
    strategy = next(
        row
        for row in early_volatis["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    strategy["funded_membership"]["effective_start"] = "2024-10-13"
    volatis_sleeve = next(
        row
        for row in early_volatis["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "quantbase-volatis-01"
    )
    volatis_sleeve["assignments"][0]["effective_start"] = "2024-10-13"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="valued governed evidence",
    ):
        _validate(early_volatis)


def test_incompatible_backtest_splice_and_unbound_generation_fail() -> None:
    spliced = copy.deepcopy(_contract())
    backtest = _history_correction(spliced, "p123-eu-backtest-not-captured")
    backtest["mode_admission"]["LIVE_PLUS_BACKTEST"] = (
        "INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="incompatible backtest entered hybrid mode",
    ):
        _validate(spliced)

    unbound = copy.deepcopy(_contract())
    provider = _history_correction(unbound, "p123-eu-provider-live")
    provider["source_receipts"][0]["sha256"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history generation is not receipt-bound",
    ):
        _validate(unbound)


def test_history_receipts_resolve_to_governed_bytes_and_reject_coherent_rewrite(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "matrixport-authority"
    generation_id = "1" * 64
    generation_root = authority / "generations" / generation_id
    artifact_path = generation_root / "csv" / "subscription_details.csv"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"synthetic governed receipt bytes\n")
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest_path = generation_root / "generation.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "csv/subscription_details.csv",
                        "sha256": artifact_sha,
                        "size_bytes": artifact_path.stat().st_size,
                    }
                ],
                "generation_id": generation_id,
                "verification": "VERIFIED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path = authority / "CURRENT.json"
    pointer_path.write_text(
        json.dumps(
            {
                "generation_id": generation_id,
                "manifest_sha256": manifest_sha,
                "state": "PUBLISHED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    pointer_sha = hashlib.sha256(pointer_path.read_bytes()).hexdigest()
    receipts = [
        {
            "artifact_role": "synthetic-current-pointer",
            "artifact_selector": "CURRENT.json",
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "CURRENT_POINTER",
            "manifest_artifact_id": None,
            "sha256": pointer_sha,
        },
        {
            "artifact_role": "synthetic-generation-manifest",
            "artifact_selector": f"generations/{generation_id}/generation.json",
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_MANIFEST",
            "manifest_artifact_id": None,
            "sha256": manifest_sha,
        },
        {
            "artifact_role": "synthetic-secondary-artifact",
            "artifact_selector": (
                f"generations/{generation_id}/csv/subscription_details.csv"
            ),
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "csv/subscription_details.csv",
            "sha256": artifact_sha,
        },
    ]
    minimal = {"history_corrections": {"corrections": [{"source_receipts": receipts}]}}
    assert (
        contract_tool.verify_history_evidence(minimal, {"MATRIXPORT": authority})[
            "verified_receipts"
        ]
        == 3
    )

    invented_artifact = copy.deepcopy(minimal)
    invented_artifact["history_corrections"]["corrections"][0]["source_receipts"][2][
        "sha256"
    ] = "a" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="differs from governed evidence bytes",
    ):
        contract_tool.verify_history_evidence(
            invented_artifact, {"MATRIXPORT": authority}
        )

    coherent_rewrite = copy.deepcopy(minimal)
    for receipt in coherent_rewrite["history_corrections"]["corrections"][0][
        "source_receipts"
    ]:
        receipt["generation_id"] = "b" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="not the published current authority",
    ):
        contract_tool.verify_history_evidence(
            coherent_rewrite, {"MATRIXPORT": authority}
        )

    # A generation receipt identifies immutable bytes, not whichever generation
    # happens to be live now.  Removing CURRENT must not invalidate the retained
    # manifest/artifact evidence, while the explicitly-current receipt must fail.
    pointer_path.unlink()
    retained_generation = copy.deepcopy(minimal)
    retained_generation["history_corrections"]["corrections"][0]["source_receipts"] = (
        receipts[1:]
    )
    assert (
        contract_tool.verify_history_evidence(
            retained_generation, {"MATRIXPORT": authority}
        )["verified_receipts"]
        == 2
    )

    current_pointer_only = copy.deepcopy(minimal)
    current_pointer_only["history_corrections"]["corrections"][0]["source_receipts"] = (
        receipts[:1]
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history evidence file is missing",
    ):
        contract_tool.verify_history_evidence(
            current_pointer_only, {"MATRIXPORT": authority}
        )


def test_outcome_semantics_and_provider_layers_are_runtime_gates() -> None:
    contract = copy.deepcopy(_contract())
    _outcome(contract, "DEPENDENCE")["calculation_contract"]["splice"] = (
        "ALLOW_POST_LIVE_BACKTEST_FILL"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="splice rule"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"]["frequency"] = (
        "CURRENT_ONLY"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="daily and current"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    _source(contract, "portfolio123-model-generation")["evidence_layer"] = (
        "PROVIDER_TRACK_RECORD"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="PROVIDER_LIVE"):
        _validate(contract)


def test_every_required_outcome_has_a_source_route() -> None:
    contract = copy.deepcopy(_contract())
    for row in contract["source_segments"]:
        row["consumers"] = [
            consumer for consumer in row["consumers"] if consumer != "INVESTED_CAPITAL"
        ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="INVESTED_CAPITAL"
    ):
        _validate(contract)


def test_per_strategy_history_and_decomposition_routes_fail_on_omission() -> None:
    contract = copy.deepcopy(_contract())
    provider_live = _source(contract, "provider-live-history")
    provider_live["subjects"]["strategy_ids"].remove("qsi")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="qsi"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    factors = _source(contract, "public-economic-and-industry-factors")
    factors["subjects"]["strategy_ids"].remove("qsi")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="qsi"):
        _validate(contract)


def test_source_strategy_version_and_cost_are_explicit() -> None:
    contract = _contract()
    assert all("strategy_version" in row for row in contract["source_segments"])
    assert any(
        "STRATEGY_COST" in row["data_classes"] for row in contract["source_segments"]
    )
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["strategy_version"]["status"] == "MIXED"

    matrix = json.loads(contract_tool.render_views(contract).source_matrix_json)["rows"]
    p123_rows = [
        row for row in matrix if row["source_id"] == "portfolio123-model-generation"
    ]
    assert {
        strategy_id: {
            row["strategy_version_status"]
            for row in p123_rows
            if row["strategy_id"] == strategy_id
        }
        for strategy_id in ("p123-ai-factor", "p123-eu")
    } == {
        "p123-ai-factor": {"UNRESOLVED"},
        "p123-eu": {"EVIDENCED"},
    }
    assert all(
        row["strategy_version_refs"] == {}
        for row in p123_rows
        if row["strategy_id"] == "p123-ai-factor"
    )
    assert all(
        row["strategy_version_refs"] == p123["strategy_version"]["version_refs"]
        for row in p123_rows
        if row["strategy_id"] == "p123-eu"
    )

    mutated = copy.deepcopy(contract)
    _source(mutated, "portfolio123-model-generation")["strategy_version"]["status"] = (
        "EVIDENCED"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="lacks evidenced version refs for subjects.*p123-ai-factor",
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    _source(mutated, "strategy-backtests")["strategy_version"]["version_refs"] = {
        "qsi": "invented-v1"
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="asserts unresolved"
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    p123 = _source(mutated, "portfolio123-model-generation")
    p123["subjects"]["strategy_ids"].remove("p123-eu")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="version refs outside its subjects",
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    _source(mutated, "portfolio123-model-generation")["strategy_version"][
        "version_refs"
    ] = {}
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="claims evidenced versions without identities",
    ):
        _validate(mutated)


def test_proposed_market_candidates_are_not_promoted_to_authority() -> None:
    contract = _contract()
    for source_id in (
        "public-economic-and-industry-factors",
        "public-etf-style-sector-and-market-prices",
    ):
        row = _source(contract, source_id)
        assert row["authority"].endswith("PENDING_328")
        assert row["acquisition"]["mode"] == (
            "NO_ACQUISITION_BEFORE_OWNER_SOURCE_APPROVAL"
        )


def test_public_factor_run_and_publication_timing_propagate_to_every_view() -> None:
    contract = _contract()
    source = _source(contract, "public-economic-and-industry-factors")
    expected_cadence = "DAILY_TO_WEEKLY_FULL_PIPELINE; CAPTURE_EVERY_SCHEDULED_RUN"
    expected_rule = (
        "CURRENT_RUN_CAPTURE_REQUIRED; MONTHLY_PROVIDER_PUBLICATION; "
        "REQUIRE_DAILY_OBSERVATIONS_THROUGH_LATEST_MONTH_END_WHOSE_45_"
        "CALENDAR_DAY_ALLOWANCE_HAS_ELAPSED"
    )
    assert source["temporal"] == {
        "timezone": "PROVIDER_DECLARED",
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cadence": expected_cadence,
    }
    assert source["freshness"] == {
        "max_age_days": 0,
        "publication_lag_days": 45,
        "rule": expected_rule,
    }

    views = contract_tool.render_views(contract)
    matrix = json.loads(views.source_matrix_json)
    rows = [
        row
        for row in matrix["rows"]
        if row["source_id"] == "public-economic-and-industry-factors"
    ]
    assert len(rows) == 105
    assert all(
        row["cadence"] == expected_cadence
        and row["max_age_days"] == 0
        and row["publication_lag_days"] == 45
        and row["freshness_rule"] == expected_rule
        and row["owner_policy_decision"] == source["owner_policy_decision"]
        for row in rows
    )
    assert b"## Source timing" in views.summary_markdown
    assert expected_cadence.encode() in views.summary_markdown
    assert expected_rule.encode() in views.summary_markdown
    source_bytes = json.dumps(source, sort_keys=True).encode()
    assert b"DAILY_WITH_PROVIDER_LAG" not in source_bytes
    assert b"SOURCE_SPECIFIC_MAX_AGE_AND_PROVIDER_LAG" not in source_bytes


def test_delete_or_rename_any_required_outcome_fails() -> None:
    missing = copy.deepcopy(_contract())
    missing["financial_outcomes"].pop()
    with pytest.raises(contract_tool.FinancialMvpContractError, match="four financial"):
        _validate(missing)

    renamed = copy.deepcopy(_contract())
    renamed["financial_outcomes"][0]["outcome_id"] = "ARTIFACT_GENERATED"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="four financial"):
        _validate(renamed)


def test_generic_unavailable_cannot_accept_an_outcome_or_source() -> None:
    contract = copy.deepcopy(_contract())
    contract["financial_outcomes"][0]["accepted_statuses"] = ["UNAVAILABLE"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generic UNAVAILABLE"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["absence_behavior"] = "DISCLOSE_AND_CONTINUE"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="fail closed"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["absence_behavior"] = "UNAVAILABLE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generic UNAVAILABLE"
    ):
        _validate(contract)


def test_missing_required_source_class_or_provider_family_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"] = [
        row
        for row in contract["source_segments"]
        if row["source_id"] != "portfolio-security-master"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="SECURITY_MASTER"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"] = [
        row
        for row in contract["source_segments"]
        if row["source_id"] != "alpaca-account-generation"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="provider families"
    ):
        _validate(contract)


def test_duplicate_source_and_unknown_consumer_fail() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"].append(copy.deepcopy(contract["source_segments"][0]))
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="duplicate source"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["consumers"].append("TOPOLOGY_ONLY_PASS")
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="unknown consumers"
    ):
        _validate(contract)


def test_required_not_ready_needs_blocker_and_ready_needs_generation() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["blockers"] = []
    with pytest.raises(contract_tool.FinancialMvpContractError, match="lacks blocker"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    strategy["readiness"] = "READY"
    strategy["blockers"] = []
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="version/membership"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "provider-live-history")
    source["readiness"] = "READY"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="lacks a current generation"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "current-strategy-registry")
    source["readiness"] = "READY"
    source["capture_status"] = "UNRESOLVED"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="uncaptured source"
    ):
        _validate(contract)


def test_capture_generation_invariants_fail_closed() -> None:
    contract = copy.deepcopy(_contract())
    source = _source(contract, "provider-live-history")
    source["capture_status"] = "CAPTURED_GOVERNED"
    source["existence_status"] = "EXISTS"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="immutable generation"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "provider-live-history")
    source["current_generation"] = {
        "generation_id": "a" * 64,
        "sha256": "b" * 64,
        "as_of": "2026-08-17",
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="uncaptured source"
    ):
        _validate(contract)


def test_private_absolute_path_and_account_identifier_are_rejected() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["endpoint_or_artifact_selector"] = (
        "C:\\private\\source.json"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="absolute"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["endpoint_or_artifact_selector"] = (
        "restricted/account-secret"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="account identifier"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["authority"] = "C:\\Users\\private\\account-123"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="absolute"):
        _validate(contract)


def test_golden_case_has_exact_financial_values_and_all_cells() -> None:
    contract = _contract()
    expected = contract["golden_case"]["expected"]
    assert expected["hwm"]["current_index"] == "93.6"
    assert expected["hwm"]["current_hwm"] == "104"
    assert expected["hwm"]["current_drawdown"] == "-0.1"
    assert expected["invested_capital"]["aggregate_current"] == "900"
    assert len(expected["decomposition"]) == 8
    assert set(expected["dependence"]) == set(contract_tool.EVIDENCE_MODES)
    assert len(expected["source_bindings"]) == 7
    assert set(expected["stage_identities"]) == set(contract_tool.OUTCOME_IDS)
    assert expected["report_panel_parity"] == {
        "hwm_equal": True,
        "invested_capital_equal": True,
        "decomposition_equal": True,
        "dependence_equal": True,
    }

    mutated = copy.deepcopy(contract)
    mutated["golden_case"]["expected"]["hwm"]["current_index"] = "104"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="golden HWM"):
        _validate(mutated)


def test_every_golden_input_output_and_identity_is_reconciled() -> None:
    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["inputs"]["component_returns"][2]["return"] = "0.99"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="golden HWM"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["hwm"]["index_by_date"]["2026-01-05"] = "999"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="index path"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["invested_capital"]["daily_aggregate"][
        "2026-01-06"
    ] = "999"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="daily invested"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["decomposition"][0]["factor_contribution"] = (
        "999"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="decomposition value"
    ):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    del mutated["golden_case"]["expected"]["dependence"]["LIVE_ONLY"]["horizons"]["5"]
    with pytest.raises(contract_tool.FinancialMvpContractError, match="horizons"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["source_bindings"][0]["generation_sha256"] = (
        "f" * 64
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="source binding"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["stage_identities"]["DEPENDENCE"][
        "input_sha256"
    ] = "f" * 64
    with pytest.raises(contract_tool.FinancialMvpContractError, match="stage identity"):
        _validate(mutated)


def _synthetic_contract_with_epoch_manifest_source(
    source: dict[str, object] | None = None,
) -> dict[str, object]:
    contract = _contract()
    contract["contract_schema_version"] = 7
    contract["epoch_manifest_source"] = (
        copy.deepcopy(source)
        if source is not None
        else _literal_epoch_manifest_source()
    )
    return contract


def test_v7_epoch_manifest_source_requires_and_matches_verified_bundle(
    tmp_path: Path,
) -> None:
    """A source-shaped object alone must never establish generation authority."""

    bundle = _synthetic_epoch_bundle(tmp_path)
    source = contract_tool.epoch_manifest_source_from_bundle(bundle)
    contract = _synthetic_contract_with_epoch_manifest_source(source)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert _schema_errors(schema, contract, schema) == []
    assert list(Draft202012Validator(schema).iter_errors(contract)) == []
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="requires a verified epoch-manifest bundle",
    ):
        _validate(contract)

    assert (
        contract_tool.validate_contract(
            contract,
            registry=_registry(),
            epoch_manifest_bundle=bundle,
        )
        == contract
    )

    invented = copy.deepcopy(contract)
    invented["epoch_manifest_source"]["generation_receipt_sha256"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="verified bundle"
    ):
        contract_tool.validate_contract(
            invented,
            registry=_registry(),
            epoch_manifest_bundle=bundle,
        )


def test_load_v7_contract_reopens_selected_generation_bundle(tmp_path: Path) -> None:
    bundle = _synthetic_epoch_bundle(tmp_path / "runtime")
    contract = _synthetic_contract_with_epoch_manifest_source(
        contract_tool.epoch_manifest_source_from_bundle(bundle)
    )
    contract_path = tmp_path / "financial-v7.json"
    contract_path.write_bytes(contract_tool.canonical_json_bytes(contract))

    assert (
        contract_tool.load_contract(
            contract_path,
            REGISTRY_PATH,
            SCHEMA_PATH,
            epoch_manifest_generation_root=tmp_path / "runtime",
            epoch_manifest_generation_id=bundle.manifest.generation_id,
        )
        == contract
    )

    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="root and id must be supplied together",
    ):
        contract_tool.load_contract(
            contract_path,
            REGISTRY_PATH,
            SCHEMA_PATH,
            epoch_manifest_generation_root=tmp_path / "runtime",
        )


@pytest.mark.parametrize(
    ("mutation", "error_fragment"),
    [
        (
            lambda contract: contract["epoch_manifest_source"].pop("generation_id"),
            "generation_id",
        ),
        (
            lambda contract: contract["epoch_manifest_source"]["artifact"].__setitem__(
                "path", "outputs/other.json"
            ),
            "artifact.path",
        ),
        (
            lambda contract: contract["epoch_manifest_source"]["artifact"].__setitem__(
                "size_bytes", True
            ),
            "size_bytes",
        ),
        (
            lambda contract: contract["epoch_manifest_source"]["artifact"].__setitem__(
                "size_bytes", 0
            ),
            "size_bytes",
        ),
        (
            lambda contract: contract["epoch_manifest_source"]["artifact"][
                "schema"
            ].__setitem__("version", "9"),
            "crosswalk",
        ),
    ],
)
def test_v7_epoch_manifest_source_rejects_missing_or_mutated_shape(
    mutation: object, error_fragment: str
) -> None:
    """Weakening a closed, typed, fixed-artifact source must be observable."""

    contract = _synthetic_contract_with_epoch_manifest_source()
    assert callable(mutation)
    mutation(contract)  # type: ignore[operator]
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert _schema_errors(schema, contract, schema)
    assert list(Draft202012Validator(schema).iter_errors(contract))
    with pytest.raises(contract_tool.FinancialMvpContractError, match=error_fragment):
        _validate(contract)


def test_synthetic_epoch_manifest_source_renders_exact_human_and_machine_views() -> (
    None
):
    """Dropping or relabelling any dedicated lineage value breaks this oracle."""

    rendered = contract_tool.render_views(
        _synthetic_contract_with_epoch_manifest_source()
    )
    expected_markdown = b"""## Epoch manifest source

- Generation ID: `b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8`
- Generation manifest SHA-256: `2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d`
- Generation receipt SHA-256: `041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f`
- Full directory inventory SHA-256: `50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1`
- Artifact path: `outputs/account-strategy-manifest.json`
- Artifact SHA-256: `810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834`
- Artifact size bytes: `43`
- Artifact schema: `account-strategy-manifest@8`
"""
    assert rendered.summary_markdown.count(expected_markdown) == 1

    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["epoch_manifest_source"] == {
        "generation_id": (
            "b4888a18780058e777f8147530062a882a473aaef19596562480a1bf43425cd8"
        ),
        "generation_manifest_sha256": (
            "2d88627c2283c31aa3c5a9333bc6ee423a1fcdd22d7df495a69c1768d384bb3d"
        ),
        "generation_receipt_sha256": (
            "041b1343070f24c99488b27e227264bf6ff9d8d9a0047f8732c7bcc3887bb25f"
        ),
        "full_directory_inventory_sha256": (
            "50769d0200857b7fe43c67cc814a21dea8e2564f916ab65ebfcb635b0910cfb1"
        ),
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": (
                "810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834"
            ),
            "size_bytes": 43,
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
    }


def test_synthetic_epoch_source_preserves_legacy_bytes_and_historical_rows() -> None:
    """Copying the epoch source into historical source rows breaks this proof."""

    legacy_contract = _contract()
    historical_bytes = (
        json.dumps(
            {"source_segments": legacy_contract["source_segments"]},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    assert hashlib.sha256(historical_bytes).hexdigest() == (
        "fa0ae5ac10bc386cfec5679f7c954dc91d43cf70f5b32e22fb817f836a7e3ce3"
    )

    legacy_views = contract_tool.render_views(legacy_contract)
    assert (
        legacy_views.summary_markdown
        == (GENERATED_ROOT / "current-product.md").read_bytes()
    )
    assert (
        legacy_views.source_matrix_json
        == (GENERATED_ROOT / "source-matrix.json").read_bytes()
    )
    assert b"## Epoch manifest source" not in legacy_views.summary_markdown
    legacy_matrix = json.loads(legacy_views.source_matrix_json)
    assert "epoch_manifest_source" not in legacy_matrix

    augmented_contract = copy.deepcopy(legacy_contract)
    augmented_contract["epoch_manifest_source"] = _literal_epoch_manifest_source()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _schema_errors(schema, augmented_contract, schema)
    assert list(Draft202012Validator(schema).iter_errors(augmented_contract))
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="financial MVP contract keys differ",
    ):
        _validate(augmented_contract)
    assert hashlib.sha256(
        (
            json.dumps(
                {"source_segments": augmented_contract["source_segments"]},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    ).hexdigest() == (
        "fa0ae5ac10bc386cfec5679f7c954dc91d43cf70f5b32e22fb817f836a7e3ce3"
    )
    augmented_matrix = json.loads(
        contract_tool.render_views(augmented_contract).source_matrix_json
    )
    assert legacy_matrix["rows"] != augmented_matrix["rows"]

    legacy_rows_without_contract_identity = [
        {key: value for key, value in row.items() if key != "contract_sha256"}
        for row in legacy_matrix["rows"]
    ]
    augmented_rows_without_contract_identity = [
        {key: value for key, value in row.items() if key != "contract_sha256"}
        for row in augmented_matrix["rows"]
    ]
    assert (
        augmented_rows_without_contract_identity
        == legacy_rows_without_contract_identity
    )
    assert all(
        legacy_row["contract_sha256"] != augmented_row["contract_sha256"]
        for legacy_row, augmented_row in zip(
            legacy_matrix["rows"], augmented_matrix["rows"], strict=True
        )
    )


@pytest.mark.parametrize(
    ("view_name", "failure_mode"),
    [
        ("current-product.md", "missing"),
        ("source-matrix.json", "missing"),
        ("current-product.md", "one-byte-mutated"),
        ("source-matrix.json", "one-byte-mutated"),
        ("current-product.md", "stale-legacy"),
        ("source-matrix.json", "stale-legacy"),
    ],
)
def test_check_views_rejects_each_synthetic_epoch_view_with_exact_path(
    tmp_path: Path, view_name: str, failure_mode: str
) -> None:
    """Missing, mutated, or legacy output must identify the exact stale file."""

    augmented = _synthetic_contract_with_epoch_manifest_source()
    augmented_views = contract_tool.render_views(augmented)
    augmented_outputs = {
        "current-product.md": augmented_views.summary_markdown,
        "source-matrix.json": augmented_views.source_matrix_json,
    }
    legacy_views = contract_tool.render_views(_contract())
    legacy_outputs = {
        "current-product.md": legacy_views.summary_markdown,
        "source-matrix.json": legacy_views.source_matrix_json,
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, payload in augmented_outputs.items():
        if name != view_name:
            (tmp_path / name).write_bytes(payload)

    target = tmp_path / view_name
    if failure_mode == "one-byte-mutated":
        payload = augmented_outputs[view_name]
        target.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    elif failure_mode == "stale-legacy":
        target.write_bytes(legacy_outputs[view_name])

    with pytest.raises(contract_tool.FinancialMvpContractError) as failure:
        contract_tool.check_views(augmented, tmp_path)
    assert str(failure.value) == (
        f"generated view drift: {target}; run the generate command"
    )


def test_check_views_accepts_exact_independently_inspected_synthetic_views(
    tmp_path: Path,
) -> None:
    augmented = _synthetic_contract_with_epoch_manifest_source()
    rendered = contract_tool.render_views(augmented)
    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["epoch_manifest_source"]["artifact"] == {
        "path": "outputs/account-strategy-manifest.json",
        "schema": {"name": "account-strategy-manifest", "version": "8"},
        "sha256": "810eb288a0d472414c805824cf91283fb3fa5b3c394ce5edec685ab9c3c64834",
        "size_bytes": 43,
    }
    assert b"- Artifact size bytes: `43`\n" in rendered.summary_markdown

    (tmp_path / "current-product.md").write_bytes(rendered.summary_markdown)
    (tmp_path / "source-matrix.json").write_bytes(rendered.source_matrix_json)
    contract_tool.check_views(augmented, tmp_path)


def test_generated_views_are_deterministic_and_match_authoritative_bytes(
    tmp_path: Path,
) -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    first = contract_tool.render_views(contract)
    second = contract_tool.render_views(copy.deepcopy(contract))
    assert first == second

    identities = contract_tool.generate_views(contract, tmp_path)
    assert identities == {
        "current-product.md": hashlib.sha256(first.summary_markdown).hexdigest(),
        "source-matrix.json": hashlib.sha256(first.source_matrix_json).hexdigest(),
    }
    assert (tmp_path / "current-product.md").read_bytes() == first.summary_markdown
    assert (tmp_path / "source-matrix.json").read_bytes() == first.source_matrix_json
    contract_tool.check_views(contract, GENERATED_ROOT)

    matrix = json.loads(first.source_matrix_json)
    rows = matrix["rows"]
    assert matrix["history_corrections"] == contract["history_corrections"]
    assert b"## Truthful strategy-history corrections \xe2\x80\x94 #319" in (
        first.summary_markdown
    )
    assert b"robuxio-account-live-after-funding" in first.summary_markdown
    assert b"MATRIXPORT:output/20260903T051029.331Z/csv/subscription_details.csv" in (
        first.summary_markdown
    )
    assert b"p123-eu-provider-live" in first.summary_markdown
    assert b"volatis-backtest-not-proved" in first.summary_markdown
    receipts = _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"][
        "owner_decision_receipts"
    ]
    assert matrix["owner_decision_receipts"] == {
        decision_id: receipts[decision_id]["payload_sha256"]
        for decision_id in contract_tool.OWNER_DECISION_IDS
    }
    assert rows
    assert {row["data_class"] for row in rows} >= contract_tool.REQUIRED_SOURCE_CLASSES
    assert all(
        row["contract_sha256"] == contract_tool.contract_digest(contract)
        for row in rows
    )
    assert all(
        not row["endpoint_or_artifact_selector"].startswith("C:\\") for row in rows
    )
    unresolved = {
        row["strategy_id"]
        for row in rows
        if row["source_id"] == "alpaca-account-generation"
    }
    assert unresolved == {"echotrade-growth-pivots"}
    assert all(row["strategy_id"] != "ALL_PORTFOLIO" for row in rows)


def test_authoritative_byte_change_reidentifies_every_view() -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    baseline_digest = contract_tool.contract_digest(contract)
    baseline_views = contract_tool.render_views(contract)
    changed = copy.deepcopy(contract)
    changed["product"]["operator_outcome"] += " Candidate changed."
    changed_digest = contract_tool.contract_digest(changed)
    changed_views = contract_tool.render_views(changed)
    assert changed_digest != baseline_digest
    assert changed_views.summary_markdown != baseline_views.summary_markdown
    assert changed_views.source_matrix_json != baseline_views.source_matrix_json


def test_invested_capital_policy_changes_in_one_contract_block_and_propagates(
    tmp_path: Path,
) -> None:
    baseline = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    baseline_contract_digest = contract_tool.contract_digest(baseline)
    baseline_policy_digest = contract_tool.invested_capital_policy_digest(baseline)
    changed = copy.deepcopy(baseline)
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    negative_net = invested["negative_net_contributions"]
    approved_treatment = "SYNTHETIC_TEST_APPROVED_TREATMENT"
    negative_net["decision_status"] = "APPROVED"
    negative_net["selected_treatment"] = approved_treatment
    negative_net["recommended_treatment"] = None
    new_comment_id = 6000000000
    new_evidence = (
        "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
        f"{new_comment_id}"
    )
    negative_net["approval_evidence"] = new_evidence
    receipt = invested["owner_decision_receipts"]["NEGATIVE_NET_CONTRIBUTIONS"]
    receipt["comment_id"] = new_comment_id
    receipt["evidence_url"] = new_evidence
    new_digest_comment_id = 6000000001
    receipt["digest_comment_id"] = new_digest_comment_id
    receipt["digest_evidence_url"] = (
        "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
        f"{new_digest_comment_id}"
    )
    receipt["payload_sha256"] = contract_tool.owner_decision_receipt_digests(changed)[
        "NEGATIVE_NET_CONTRIBUTIONS"
    ]

    _validate(changed)
    comments = _owner_evidence_comments(baseline)
    comments[new_comment_id] = _github_comment(new_comment_id)
    new_record = contract_tool.owner_receipt_evidence_record(
        changed, ["NEGATIVE_NET_CONTRIBUTIONS"]
    )
    new_body = (
        f"{contract_tool._OWNER_RECEIPT_MARKER}\n\n```json\n"
        f"{json.dumps(new_record, indent=2, sort_keys=True)}\n```\n"
    )
    comments[new_digest_comment_id] = _github_comment(new_digest_comment_id, new_body)
    contract_tool.reconcile_owner_receipt_evidence(changed, comments)
    changed_contract_digest = contract_tool.contract_digest(changed)
    changed_policy_digest = contract_tool.invested_capital_policy_digest(changed)
    rendered = contract_tool.render_views(changed)
    matrix = json.loads(rendered.source_matrix_json)

    assert changed_contract_digest != baseline_contract_digest
    assert changed_policy_digest != baseline_policy_digest
    assert matrix["invested_capital_policy_sha256"] == changed_policy_digest
    assert (
        matrix["owner_decision_receipts"]["NEGATIVE_NET_CONTRIBUTIONS"]
        == (receipt["payload_sha256"])
    )
    assert b"negative_net_contributions" in rendered.summary_markdown
    assert approved_treatment.encode("utf-8") in rendered.summary_markdown
    assert receipt["payload_sha256"].encode("utf-8") in rendered.summary_markdown
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generated view drift"
    ):
        contract_tool.check_views(changed, GENERATED_ROOT)

    contract_tool.generate_views(changed, tmp_path)
    contract_tool.check_views(changed, tmp_path)


def test_policy_decision_cannot_claim_approval_without_evidence() -> None:
    changed = _contract()
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    negative_net = invested["negative_net_contributions"]
    negative_net["approval_evidence"] = None
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="approval record is incomplete"
    ):
        _validate(changed)


def test_hand_edit_of_generated_view_is_detected(tmp_path: Path) -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    contract_tool.generate_views(contract, tmp_path)
    summary = tmp_path / "current-product.md"
    summary.write_text("hand edited\n", encoding="utf-8")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="view drift"):
        contract_tool.check_views(contract, tmp_path)

"""Stage one verified current-epoch manifest from explicit immutable inputs."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType

from quant_engine.core import GenerationBundle, GenerationConflictError, InputSnapshot
from quant_engine.core.contracts import canonical_json_bytes


_GENERATION_ID = re.compile(r"[0-9a-f]{64}")
_MANIFEST_ARTIFACT_ID = "outputs/account-strategy-manifest.json"
_CROSSWALK_ARTIFACT_ID = "authority/account-assignment-crosswalk.json"


def _load_builder_module() -> ModuleType:
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    return importlib.import_module("tools.build_account_manifest_generation")


account_manifest_generation = _load_builder_module()


class CurrentEpochManifestStageError(ValueError):
    """An explicit input path or identity cannot stage the selected generation."""


def _generation_directory(root: Path, generation_id: str, label: str) -> Path:
    if _GENERATION_ID.fullmatch(generation_id) is None:
        raise CurrentEpochManifestStageError(f"{label} generation id is invalid")
    return root / "generations" / generation_id


def _capture(path: Path, label: str) -> InputSnapshot:
    if not path.is_file():
        raise CurrentEpochManifestStageError(f"cannot capture {label}")
    return InputSnapshot.capture(path, label)


def _code_snapshots() -> tuple[InputSnapshot, InputSnapshot, InputSnapshot]:
    builder_file = account_manifest_generation.__file__
    if builder_file is None:
        raise CurrentEpochManifestStageError("generation builder source is unavailable")
    builder_path = Path(builder_file).resolve()
    producer_path = builder_path.with_name("account_strategy_manifest.py")
    verifier_path = builder_path.with_name("verify_account_manifest_generation.py")
    return (
        _capture(producer_path, "executing account-manifest epoch producer"),
        _capture(builder_path, "executing account-manifest generation builder"),
        _capture(verifier_path, "executing independent manifest verifier"),
    )


def _artifact_descriptor(
    bundle: GenerationBundle,
    *,
    artifact_id: str,
    schema_name: str,
    schema_version: str,
) -> dict[str, object]:
    matches = [
        artifact
        for artifact in bundle.manifest.artifacts
        if artifact.artifact_id == artifact_id
    ]
    payload = bundle.payloads.get(artifact_id)
    if len(matches) != 1 or type(payload) is not bytes:
        raise CurrentEpochManifestStageError(
            f"verified bundle lacks exactly one {artifact_id} artifact"
        )
    artifact = matches[0]
    digest = hashlib.sha256(payload).hexdigest()
    if (
        artifact.sha256 != digest
        or artifact.size_bytes != len(payload)
        or artifact.schema_name != schema_name
        or artifact.schema_version != schema_version
    ):
        raise CurrentEpochManifestStageError(
            f"verified bundle {artifact_id} identity differs"
        )
    return {
        "path": artifact_id,
        "sha256": digest,
        "size_bytes": len(payload),
        "schema": {"name": schema_name, "version": schema_version},
    }


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _create_exact(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != contents:
            raise CurrentEpochManifestStageError(
                "create-only epoch manifest source receipt differs"
            )
        return
    try:
        with path.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != contents:
            raise CurrentEpochManifestStageError(
                "create-only epoch manifest source receipt differs"
            ) from None
        return
    _sync_directory(path.parent)


def stage_current_epoch_manifest(
    *,
    provider_ledger_generation_root: Path,
    provider_ledger_generation_id: str,
    baseline_generation_root: Path,
    baseline_generation_id: str,
    flow_policy_path: Path,
    crosswalk_path: Path,
    manifest_schema_path: Path,
    receipt_authority_path: Path,
    admission_policy_path: Path,
    membership_census_path: Path,
    receipt_paths: Mapping[str, Path],
    output_generation_root: Path,
) -> dict[str, object]:
    """Capture the named bytes once, verify them, and stage one immutable bundle."""

    provider_generation = _generation_directory(
        provider_ledger_generation_root,
        provider_ledger_generation_id,
        "provider-ledger",
    )
    baseline_generation = _generation_directory(
        baseline_generation_root, baseline_generation_id, "baseline"
    )
    if not isinstance(receipt_paths, Mapping) or not receipt_paths:
        raise CurrentEpochManifestStageError(
            "at least one explicit receipt artifact-id=path is required"
        )

    receipts = {
        artifact_id: _capture(path, f"retrieval receipt {artifact_id}")
        for artifact_id, path in receipt_paths.items()
    }
    epoch_source, builder_source, verifier_source = _code_snapshots()
    selected = account_manifest_generation.AccountManifestSelectedInputs(
        provider_ledger=_capture(
            provider_generation / "provider-ledger.json", "provider-ledger catalog"
        ),
        provider_ledger_manifest=_capture(
            provider_generation / "generation.json",
            "provider-ledger generation manifest",
        ),
        baseline=_capture(
            baseline_generation / "portfolio-baseline.json",
            "portfolio baseline catalog",
        ),
        baseline_manifest=_capture(
            baseline_generation / "generation.json",
            "portfolio baseline generation manifest",
        ),
        flow_classification_policy=_capture(flow_policy_path, "flow policy"),
        retrieval_receipts=receipts,
        epoch_producer_source=epoch_source,
        generation_builder_source=builder_source,
        independent_verifier_source=verifier_source,
        current_epoch_crosswalk=_capture(crosswalk_path, "current-epoch crosswalk"),
        receipt_authority=_capture(receipt_authority_path, "receipt authority"),
        admission_policy=_capture(admission_policy_path, "admission policy"),
        membership_census=_capture(membership_census_path, "membership census"),
        manifest_schema=_capture(manifest_schema_path, "account-manifest schema"),
    )
    if selected.provider_ledger_generation_id != provider_ledger_generation_id:
        raise CurrentEpochManifestStageError(
            "provider-ledger generation id differs from selected manifest"
        )
    if selected.baseline_generation_id != baseline_generation_id:
        raise CurrentEpochManifestStageError(
            "baseline generation id differs from selected manifest"
        )

    bundle = account_manifest_generation.stage_account_manifest_generation(
        selected, output_generation_root
    )
    manifest_descriptor = _artifact_descriptor(
        bundle,
        artifact_id=_MANIFEST_ARTIFACT_ID,
        schema_name="account-strategy-manifest",
        schema_version="10",
    )
    crosswalk_descriptor = _artifact_descriptor(
        bundle,
        artifact_id=_CROSSWALK_ARTIFACT_ID,
        schema_name="account-strategy-current-epoch-crosswalk",
        schema_version="1",
    )
    source_receipt_bytes = canonical_json_bytes(
        {
            "generation_id": bundle.manifest.generation_id,
            "generation_manifest_sha256": hashlib.sha256(
                bundle.manifest.to_json_bytes()
            ).hexdigest(),
            "generation_receipt_sha256": hashlib.sha256(
                bundle.receipt_bytes
            ).hexdigest(),
            "full_directory_inventory_sha256": (bundle.full_directory_inventory_sha256),
            "artifact": manifest_descriptor,
            "crosswalk": crosswalk_descriptor,
        }
    )
    source_receipt_path = (
        output_generation_root
        / "report-inputs"
        / bundle.manifest.generation_id
        / "epoch-manifest-source.json"
    )
    _create_exact(source_receipt_path, source_receipt_bytes)
    return {
        "generation_id": bundle.manifest.generation_id,
        "artifacts": [
            artifact.to_dict()
            for artifact in sorted(
                bundle.manifest.artifacts, key=lambda item: item.artifact_id
            )
        ],
        "source_receipt": {
            "path": str(source_receipt_path),
            "sha256": hashlib.sha256(source_receipt_bytes).hexdigest(),
            "size_bytes": len(source_receipt_bytes),
        },
    }


def _receipt_argument(value: str) -> tuple[str, Path]:
    artifact_id, separator, raw_path = value.partition("=")
    if not separator or not artifact_id or not raw_path:
        raise argparse.ArgumentTypeError("receipt must be ARTIFACT_ID=PATH")
    return artifact_id, Path(raw_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-ledger-generation-root", type=Path, required=True)
    parser.add_argument("--provider-ledger-generation-id", required=True)
    parser.add_argument("--baseline-generation-root", type=Path, required=True)
    parser.add_argument("--baseline-generation-id", required=True)
    parser.add_argument("--flow-policy", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--manifest-schema", type=Path, required=True)
    parser.add_argument("--receipt-authority", type=Path, required=True)
    parser.add_argument("--admission-policy", type=Path, required=True)
    parser.add_argument(
        "--membership-census",
        type=Path,
        required=True,
        help=(
            "governed financial MVP source contract whose current census must "
            "agree with every funded admission (R2)"
        ),
    )
    parser.add_argument(
        "--receipt", action="append", type=_receipt_argument, required=True
    )
    parser.add_argument("--output-generation-root", type=Path, required=True)
    return parser


def _receipt_mapping(values: Sequence[tuple[str, Path]]) -> dict[str, Path]:
    receipts: dict[str, Path] = {}
    folded: set[str] = set()
    for artifact_id, path in values:
        if artifact_id.casefold() in folded:
            raise CurrentEpochManifestStageError(
                "receipt artifact IDs must be unique case-insensitively"
            )
        folded.add(artifact_id.casefold())
        receipts[artifact_id] = path
    return receipts


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_current_epoch_manifest(
            provider_ledger_generation_root=args.provider_ledger_generation_root,
            provider_ledger_generation_id=args.provider_ledger_generation_id,
            baseline_generation_root=args.baseline_generation_root,
            baseline_generation_id=args.baseline_generation_id,
            flow_policy_path=args.flow_policy,
            crosswalk_path=args.crosswalk,
            manifest_schema_path=args.manifest_schema,
            receipt_authority_path=args.receipt_authority,
            admission_policy_path=args.admission_policy,
            membership_census_path=args.membership_census,
            receipt_paths=_receipt_mapping(args.receipt),
            output_generation_root=args.output_generation_root,
        )
    except (GenerationConflictError, OSError, ValueError) as exc:
        print(f"stage-current-epoch-manifest: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

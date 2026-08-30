"""Validate and render the current financial-MVP/source contract.

The JSON contract is the sole handwritten authority.  Markdown and JSON views
are deterministic derivatives of its canonical bytes; they are never inputs.
Normal validation and rendering perform no network, credential, governed-data,
or publication work.  The explicit ``verify-owner-evidence`` command performs
one bounded authenticated GitHub CLI read for each owner-evidence comment. The
explicit ``verify-history-evidence`` command resolves and hashes #319 receipts
through caller-supplied governed local roots without emitting source values.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    GenerationBundle,
    VerificationStatus,
    read_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT: Final = (
    REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
)
DEFAULT_SCHEMA: Final = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "financial-mvp-source-contract.schema.json"
)
DEFAULT_REGISTRY: Final = (
    REPOSITORY_ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
)
DEFAULT_VIEW_ROOT: Final = REPOSITORY_ROOT / "generated" / "financial-mvp"

OUTCOME_IDS: Final = (
    "AS_HELD_COMPOSITE_HWM",
    "INVESTED_CAPITAL",
    "DECOMPOSITION",
    "DEPENDENCE",
)
DECOMPOSITION_LENSES: Final = (
    "ECONOMIC_FACTOR_ALPHA",
    "ETF_STYLE",
    "RETURN_SECTOR",
    "RETURN_INDUSTRY",
)
EVIDENCE_MODES: Final = ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
DEPENDENCE_HORIZONS: Final = (5, 21, 22, 63, 126, 130, 252, 260)
OWNER_DECISION_IDS: Final = (
    "MARGIN_AND_LEVERAGE",
    "PREDECESSOR_TRANSITION",
    "CORRECTION_REVERSAL",
    "NEGATIVE_NET_CONTRIBUTIONS",
    "FX_BASE_CURRENCY",
)
REQUIRED_SOURCE_CLASSES: Final = frozenset(
    {
        "ACCOUNT_NAV",
        "BACKTEST_RETURNS",
        "CALENDAR",
        "CAPITAL_ANCHOR",
        "CASH_RISK_FREE",
        "CORPORATE_ACTION",
        "CRYPTO_RETURN",
        "ETF_STYLE_RETURN",
        "EXTERNAL_FLOW",
        "FINANCING_COST",
        "FINANCING_LIABILITY",
        "FX_RATE",
        "INDUSTRY_RETURN",
        "LEVERAGE_EXPOSURE",
        "MARKET_PRICE",
        "POSITION",
        "PROVIDER_LIVE_RETURN",
        "PROVIDER_TRACK_RECORD",
        "SECTOR_RETURN",
        "SECURITY_MASTER",
        "STRATEGY_BENCHMARK",
        "STRATEGY_CONFIGURATION",
        "STRATEGY_COST",
        "STRATEGY_DESIGN",
        "STRATEGY_VERSION",
        "TRANSACTION",
        "VOLATILITY_RETURN",
        "ECONOMIC_FACTOR_RETURN",
    }
)
REQUIRED_PROVIDER_SOURCE_IDS: Final = frozenset(
    {
        "alpaca-account-generation",
        "fidelity-account-generation",
        "ibkr-account-generation",
        "matrixport-account-generation",
        "portfolio123-model-generation",
        "quantbase-account-generation",
        "schwab-account-generation",
    }
)
REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS: Final = frozenset(
    {
        "alpaca-account-generation",
        "fidelity-account-generation",
        "ibkr-account-generation",
        "matrixport-account-generation",
        "quantbase-account-generation",
        "schwab-account-generation",
    }
)
HISTORY_CORRECTION_LAYERS: Final = {
    "echotrade-growth-pivots": frozenset({"ACCOUNT_LIVE"}),
    "p123-ai-factor": frozenset({"BACKTEST"}),
    "p123-eu": frozenset({"ACCOUNT_LIVE", "BACKTEST", "PROVIDER_LIVE"}),
    "robuxio-crypto": frozenset(
        {"ACCOUNT_LIVE", "PROVIDER_LIVE", "PROVIDER_TRACK_RECORD"}
    ),
    "volatis-strategy": frozenset({"ACCOUNT_LIVE", "BACKTEST", "PROVIDER_LIVE"}),
}
HISTORY_SPLICE_POLICY: Final = (
    "SELECT_ONE_ACCEPTED_LIVE_LAYER; BACKTEST_ONLY_WHEN_EXACT_VERSION_MATCHED_AND_"
    "STRICTLY_BEFORE_LIVE_INCEPTION; NEVER_RELABEL_PROVIDER_EVIDENCE_AS_ACCOUNT_LIVE"
)
HISTORY_EVIDENCE_ROOTS: Final = frozenset(
    {"BASELINE", "MATRIXPORT", "PORTFOLIO123", "PROVIDER_LEDGER", "QUANTBASE"}
)
HISTORY_RECEIPT_LOCATORS: Final = frozenset(
    {"CURRENT_POINTER", "GENERATION_ARTIFACT", "GENERATION_MANIFEST"}
)
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_RE: Final = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
_WINDOWS_ABSOLUTE_RE: Final = re.compile(r"^[A-Za-z]:[\\/]")
_PRIVATE_ACCOUNT_RE: Final = re.compile(r"(?i)\baccount[-_ ]?(?:id[-_: ]*)?[0-9]{3,}\b")
_OWNER_RECEIPT_MARKER: Final = "<!-- financial-mvp-owner-decision-receipts-v1 -->"
_JSON_FENCE_RE: Final = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_GITHUB_COMMENT_API: Final = "repos/dachent/quant_engine/issues/comments/{comment_id}"
_CENSUS_OWNER_COMMENT_ID: Final = 5350132900
_CENSUS_OWNER_MEMBERSHIP_ORDER: Final = (
    "p123-ai-factor",
    "grizzlybears-vtma",
    "qsi",
    "tt-portfolio",
)
_CENSUS_OWNER_NON_DECISIONS: Final = (
    "TRANSACTION_OR_FLOW_CLASSIFICATION",
    "UNSUPPORTED_STRATEGY_DESIGN_VERSION",
    "PRIVATE_ACCOUNT_IDENTITY",
    "POINTER_OR_PUBLICATION",
)
_CENSUS_TT_CONTINUITY_INTERVAL: Final = (date(2026, 7, 14), date(2026, 7, 30))
_HISTORY_AUTHORITY_COMMIT: Final = "41473d47dc9cba6e6a6e189b05f1268c56bd5fa4"
_HISTORY_AUTHORITY_RAW_SHA256: Final = (
    "b267305a59682e660379abb58f380209f86391e193d24c420af201b8168fbc6b"
)
_HISTORY_AUTHORITY_SEMANTIC_SHA256: Final = (
    "00309632bf3afc35f4c3582883ffa765131e75d4a2a2999b59e6eb369f939512"
)
_HISTORY_SEGMENT_SHA256_OVERRIDES: Final = {
    # Issue #374: the account-return path is canonical only after exact
    # provider-ledger selection and independent Matrixport NAV reconciliation.
    "robuxio-crypto:account-live": (
        "cb9609449f633b3077fd2dbfd3cd67764f61290c0d0dee682368d235cae77b13",
        "e65d9fbeebf5bc7b90ff140cf09da821266b03bd9809359c1a63e5432501d0d3",
    ),
}
EPOCH_MANIFEST_ARTIFACT_PATH: Final = "outputs/account-strategy-manifest.json"
EPOCH_MANIFEST_ARTIFACT_SCHEMA_NAME: Final = "account-strategy-manifest"
EPOCH_MANIFEST_ARTIFACT_SCHEMA_VERSION: Final = "8"
_EPOCH_MANIFEST_SOURCE_KEYS: Final = frozenset(
    {
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
        "artifact",
    }
)
_EPOCH_MANIFEST_ARTIFACT_KEYS: Final = frozenset(
    {"path", "sha256", "size_bytes", "schema"}
)
_EPOCH_MANIFEST_SCHEMA_KEYS: Final = frozenset({"name", "version"})
_CENSUS_SOURCE_VERSION_OVERRIDES: Final = {
    "alpaca-account-generation": {
        "binding": "DEPRECATED_ECHOTRADE_EVENT_HISTORY_ONLY",
        "status": "NOT_APPLICABLE",
        "strategy_ids": (),
    },
    "fidelity-account-generation": {
        "binding": "NO_CURRENT_CENSUS_MEMBER",
        "status": "NOT_APPLICABLE",
        "strategy_ids": (),
    },
    "ibkr-account-generation": {
        "binding": (
            "P123_EU_DESIGN_EVIDENCED_P123_AI_DESIGN_UNRESOLVED; "
            "LIVE_PATH_IDENTITIES_SEPARATE"
        ),
        "status": "MIXED",
        "strategy_ids": ("p123-eu",),
    },
    "schwab-account-generation": {
        "binding": (
            "ECONOMIC_DESIGN_CONFIGURATION_UNRESOLVED; LIVE_PATH_IDENTITIES_SEPARATE"
        ),
        "status": "UNRESOLVED",
        "strategy_ids": (),
    },
}


class FinancialMvpContractError(ValueError):
    """The current-product/source contract violates its executable rules."""


@dataclass(frozen=True)
class GeneratedViews:
    """Deterministic generated view bytes."""

    summary_markdown: bytes
    source_matrix_json: bytes


def epoch_manifest_source_from_bundle(bundle: GenerationBundle) -> dict[str, object]:
    """Bind a verified generation bundle to the closed epoch-manifest source."""

    if type(bundle) is not GenerationBundle:
        raise FinancialMvpContractError(
            "epoch manifest source requires GenerationBundle"
        )
    if bundle.manifest.verification is not VerificationStatus.VERIFIED:
        raise FinancialMvpContractError(
            "epoch manifest source requires VERIFIED bundle"
        )
    generation_id = bundle.manifest.generation_id
    if (
        bundle.inventory.generation_id != generation_id
        or bundle.receipt.generation_id != generation_id
    ):
        raise FinancialMvpContractError("epoch manifest source generation IDs differ")

    artifacts = [
        artifact
        for artifact in bundle.manifest.artifacts
        if artifact.artifact_id == EPOCH_MANIFEST_ARTIFACT_PATH
    ]
    if len(artifacts) != 1:
        raise FinancialMvpContractError(
            "epoch manifest source requires exactly one required artifact"
        )
    artifact = artifacts[0]
    if artifact.schema_name != EPOCH_MANIFEST_ARTIFACT_SCHEMA_NAME:
        raise FinancialMvpContractError(
            "epoch manifest source artifact schema name differs"
        )
    if artifact.schema_version != EPOCH_MANIFEST_ARTIFACT_SCHEMA_VERSION:
        raise FinancialMvpContractError(
            "epoch manifest source artifact schema version differs"
        )
    if artifact.size_bytes <= 0:
        raise FinancialMvpContractError(
            "epoch manifest source artifact requires positive byte size"
        )
    payload = bundle.payloads.get(EPOCH_MANIFEST_ARTIFACT_PATH)
    if type(payload) is not bytes:
        raise FinancialMvpContractError(
            "epoch manifest source artifact payload is absent"
        )
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    if payload_sha256 != artifact.sha256 or len(payload) != artifact.size_bytes:
        raise FinancialMvpContractError(
            "epoch manifest source artifact bytes differ from manifest"
        )

    return {
        "generation_id": generation_id,
        "generation_manifest_sha256": bundle.receipt.manifest.sha256,
        "generation_receipt_sha256": hashlib.sha256(bundle.receipt_bytes).hexdigest(),
        "full_directory_inventory_sha256": bundle.full_directory_inventory_sha256,
        "artifact": {
            "path": EPOCH_MANIFEST_ARTIFACT_PATH,
            "sha256": payload_sha256,
            "size_bytes": len(payload),
            "schema": {
                "name": EPOCH_MANIFEST_ARTIFACT_SCHEMA_NAME,
                "version": EPOCH_MANIFEST_ARTIFACT_SCHEMA_VERSION,
            },
        },
    }


def _epoch_manifest_source_value(actual: object, expected: object, name: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise FinancialMvpContractError(
            f"epoch manifest source {name} differs from verified bundle"
        )


def validate_epoch_manifest_source(source: object, *, bundle: GenerationBundle) -> None:
    """Validate closed epoch-manifest source values against a verified bundle."""

    expected = epoch_manifest_source_from_bundle(bundle)
    if type(source) is not dict:
        raise FinancialMvpContractError("epoch manifest source must be an object")
    if set(source) != _EPOCH_MANIFEST_SOURCE_KEYS:
        raise FinancialMvpContractError(
            "epoch manifest source keys differ from contract"
        )
    for key in (
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
    ):
        _epoch_manifest_source_value(source[key], expected[key], key)

    artifact = source["artifact"]
    expected_artifact = expected["artifact"]
    if type(artifact) is not dict or type(expected_artifact) is not dict:
        raise FinancialMvpContractError(
            "epoch manifest source artifact must be an object"
        )
    if set(artifact) != _EPOCH_MANIFEST_ARTIFACT_KEYS:
        raise FinancialMvpContractError(
            "epoch manifest source artifact keys differ from contract"
        )
    for key in ("path", "sha256"):
        _epoch_manifest_source_value(
            artifact[key], expected_artifact[key], f"artifact.{key}"
        )
    size_bytes = artifact["size_bytes"]
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise FinancialMvpContractError(
            "epoch manifest source artifact.size_bytes must be an integer"
        )
    _epoch_manifest_source_value(
        size_bytes, expected_artifact["size_bytes"], "artifact.size_bytes"
    )

    schema = artifact["schema"]
    expected_schema = expected_artifact["schema"]
    if type(schema) is not dict or type(expected_schema) is not dict:
        raise FinancialMvpContractError(
            "epoch manifest source artifact.schema must be an object"
        )
    if set(schema) != _EPOCH_MANIFEST_SCHEMA_KEYS:
        raise FinancialMvpContractError(
            "epoch manifest source artifact.schema keys differ from contract"
        )
    for key in ("name", "version"):
        _epoch_manifest_source_value(
            schema[key], expected_schema[key], f"artifact.schema.{key}"
        )


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinancialMvpContractError(f"{name} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinancialMvpContractError(f"{name} must be readable JSON") from exc
    if not isinstance(value, dict):
        raise FinancialMvpContractError(f"{name} must be an object")
    return value


def _evidence_file(root: Path, selector: str, name: str) -> Path:
    """Resolve one public-safe selector inside an explicit governed root."""

    if root.is_symlink() or not root.is_dir():
        raise FinancialMvpContractError(f"history authority root is invalid: {name}")
    relative = PurePosixPath(selector)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FinancialMvpContractError(f"history evidence selector is invalid: {name}")
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise FinancialMvpContractError(
                f"history evidence selector traverses a symlink: {name}"
            )
    if not candidate.is_file():
        raise FinancialMvpContractError(f"history evidence file is missing: {name}")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise FinancialMvpContractError(
            f"history evidence escapes its authority root: {name}"
        ) from exc
    return candidate


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _verify_history_receipt(
    receipt: Mapping[str, Any], authority_roots: Mapping[str, Path]
) -> None:
    role = _text(receipt["artifact_role"], "history receipt artifact_role")
    authority_root = _text(receipt["authority_root"], "history receipt authority_root")
    if authority_root not in HISTORY_EVIDENCE_ROOTS:
        raise FinancialMvpContractError(f"history receipt root differs: {role}")
    root = authority_roots.get(authority_root)
    if root is None:
        raise FinancialMvpContractError(
            f"history evidence root was not supplied: {authority_root}"
        )
    generation_id = _hash(receipt["generation_id"], "history receipt generation")
    selector = _text(receipt["artifact_selector"], "history artifact selector")
    locator = _text(receipt["locator_kind"], "history receipt locator_kind")
    manifest_artifact_id = _optional_text(
        receipt["manifest_artifact_id"], "history manifest artifact id"
    )
    if locator not in HISTORY_RECEIPT_LOCATORS:
        raise FinancialMvpContractError(f"history receipt locator differs: {role}")
    expected_hash = _hash(receipt["sha256"], "history receipt sha256")

    pointer_path = _evidence_file(root, "CURRENT.json", f"{authority_root}/CURRENT")
    pointer = _load_json(pointer_path, f"{authority_root} current pointer")
    if (
        pointer.get("state") != "PUBLISHED"
        or pointer.get("generation_id") != generation_id
    ):
        raise FinancialMvpContractError(
            f"history receipt generation is not the published current authority: {role}"
        )
    generation_selector = f"generations/{generation_id}/generation.json"
    manifest_path = _evidence_file(
        root, generation_selector, f"{authority_root}/{generation_id}/manifest"
    )
    manifest_hash, _manifest_size = _file_identity(manifest_path)
    if pointer.get("manifest_sha256") != manifest_hash:
        raise FinancialMvpContractError(
            f"history authority pointer/manifest identity differs: {role}"
        )
    manifest = _load_json(manifest_path, f"{authority_root} generation manifest")
    if (
        manifest.get("verification") != "VERIFIED"
        or manifest.get("generation_id") != generation_id
    ):
        raise FinancialMvpContractError(
            f"history evidence generation is not verified: {role}"
        )

    if locator == "CURRENT_POINTER":
        if selector != "CURRENT.json" or manifest_artifact_id is not None:
            raise FinancialMvpContractError(
                f"history current-pointer selector differs: {role}"
            )
        actual_hash, _actual_size = _file_identity(pointer_path)
    elif locator == "GENERATION_MANIFEST":
        if selector != generation_selector or manifest_artifact_id is not None:
            raise FinancialMvpContractError(
                f"history generation-manifest selector differs: {role}"
            )
        actual_hash = manifest_hash
    else:
        if manifest_artifact_id is None:
            raise FinancialMvpContractError(
                f"history generation artifact lacks a manifest selector: {role}"
            )
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise FinancialMvpContractError(
                f"history generation artifacts are invalid: {role}"
            )
        matches = [
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("artifact_id") == manifest_artifact_id
        ]
        if len(matches) != 1:
            raise FinancialMvpContractError(
                f"history artifact selector is not unique: {role}"
            )
        artifact_path = _evidence_file(
            root,
            selector,
            f"{authority_root}/{generation_id}/{role}",
        )
        actual_hash, actual_size = _file_identity(artifact_path)
        artifact = matches[0]
        if (
            artifact.get("sha256") != actual_hash
            or artifact.get("size_bytes") != actual_size
        ):
            raise FinancialMvpContractError(
                f"history artifact differs from its generation manifest: {role}"
            )
    if actual_hash != expected_hash:
        raise FinancialMvpContractError(
            f"history receipt differs from governed evidence bytes: {role}"
        )


def verify_history_evidence(
    contract: Mapping[str, Any], authority_roots: Mapping[str, Path]
) -> dict[str, object]:
    """Resolve and hash every #319 receipt without exposing source values."""

    history = _object(contract["history_corrections"], "history_corrections")
    corrections = _objects(history["corrections"], "history corrections")
    verified = 0
    used_roots: set[str] = set()
    for correction in corrections:
        receipts = _objects(correction["source_receipts"], "history source_receipts")
        for receipt in receipts:
            _verify_history_receipt(receipt, authority_roots)
            verified += 1
            used_roots.add(str(receipt["authority_root"]))
    return {
        "authority_roots": sorted(used_roots),
        "contract_sha256": contract_digest(contract),
        "status": "VALID",
        "verified_receipts": verified,
    }


def _verify_strategy_version_history_evidence(
    contract: Mapping[str, Any],
) -> int:
    """Require product version assertions to match accepted history authority.

    Repeating a version reference in two mutable source rows is not independent
    evidence.  The workstation gate freezes the exact pre-census product,
    source, correction, and materialized-segment version projection, permits
    only the explicit #302/#374 provider-lane deltas, and then requires two distinct
    captured history layers whose receipt selectors have already been resolved
    against governed bytes by :func:`verify_history_evidence`.
    """

    try:
        result = subprocess.run(  # noqa: S603
            [
                "git",
                "show",
                f"{_HISTORY_AUTHORITY_COMMIT}:contracts/portfolio-financial-mvp-source-v1.json",
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise FinancialMvpContractError(
            "accepted #319 history authority is unreachable"
        ) from exc
    authority_bytes = result.stdout
    if (
        result.returncode != 0
        or hashlib.sha256(authority_bytes).hexdigest() != _HISTORY_AUTHORITY_RAW_SHA256
    ):
        raise FinancialMvpContractError("accepted #319 history authority differs")
    try:
        accepted_contract = json.loads(authority_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinancialMvpContractError(
            "accepted #319 history authority is invalid"
        ) from exc
    accepted = _object(accepted_contract, "accepted #319 history authority")
    if contract_digest(accepted) != _HISTORY_AUTHORITY_SEMANTIC_SHA256:
        raise FinancialMvpContractError(
            "accepted #319 history semantic authority differs"
        )
    accepted_versions = {
        _text(row.get("strategy_id"), "accepted #319 version strategy_id"): _object(
            row.get("strategy_version"), "accepted #319 strategy_version"
        )
        for row in _objects(accepted.get("strategies"), "accepted #319 strategies")
    }

    accepted_sources = {
        _text(row.get("source_id"), "accepted history source_id"): _object(
            row.get("strategy_version"), "accepted history source strategy_version"
        )
        for row in _objects(
            accepted.get("source_segments"), "accepted history source_segments"
        )
    }
    expected_sources = dict(accepted_sources)
    for source_id, override in _CENSUS_SOURCE_VERSION_OVERRIDES.items():
        version_refs: dict[str, str] = {}
        for strategy_id in override["strategy_ids"]:
            accepted_version = accepted_versions.get(strategy_id)
            if (
                accepted_version is None
                or accepted_version.get("status") != "EVIDENCED"
            ):
                raise FinancialMvpContractError(
                    f"accepted history version is unavailable: {strategy_id}"
                )
            version_refs[strategy_id] = _text(
                accepted_version.get("version_ref"),
                f"accepted history version ref {strategy_id}",
            )
        expected_sources[source_id] = {
            "status": override["status"],
            "binding": override["binding"],
            "version_refs": version_refs,
        }

    current_sources = {
        _text(row.get("source_id"), "governed history source_id"): _object(
            row.get("strategy_version"), "governed history source strategy_version"
        )
        for row in _objects(contract.get("source_segments"), "source_segments")
    }
    if set(current_sources) != set(expected_sources):
        raise FinancialMvpContractError(
            "source strategy design/version authority set differs"
        )
    for source_id, expected_version in expected_sources.items():
        if current_sources[source_id] != expected_version:
            raise FinancialMvpContractError(
                "source strategy design/version projection differs from accepted "
                f"history authority: {source_id}"
            )

    def receipt_projection(receipt: Mapping[str, Any]) -> dict[str, object]:
        receipt_keys = (
            "artifact_role",
            "authority_root",
            "generation_id",
            "artifact_selector",
            "locator_kind",
            "manifest_artifact_id",
            "sha256",
        )
        projected = {key: receipt.get(key) for key in receipt_keys}
        if projected["authority_root"] != "PROVIDER_LEDGER":
            return projected

        generation_id = str(projected["generation_id"] or "")
        selector = str(projected["artifact_selector"] or "")
        generation_prefix = f"generations/{generation_id}/"
        if not generation_id or not selector.startswith(generation_prefix):
            raise FinancialMvpContractError(
                "provider-ledger history receipt selector differs"
            )
        projected["generation_id"] = "<CURRENT_PROVIDER_LEDGER_GENERATION>"
        projected["artifact_selector"] = (
            "generations/<CURRENT_PROVIDER_LEDGER_GENERATION>/"
            + selector.removeprefix(generation_prefix)
        )
        projected["sha256"] = "<CURRENT_PROVIDER_LEDGER_RECEIPT>"
        return projected

    def correction_projection(row: Mapping[str, Any]) -> dict[str, object]:
        receipts = [
            receipt_projection(receipt)
            for receipt in _objects(
                row.get("source_receipts"), "history correction source_receipts"
            )
        ]
        receipts.sort(key=canonical_json_bytes)
        return {
            "strategy_id": row.get("strategy_id"),
            "evidence_layer": row.get("evidence_layer"),
            "source_id": row.get("source_id"),
            "capture_status": _object(
                row.get("capture"), "history correction capture"
            ).get("status"),
            "strategy_version": _object(
                row.get("strategy_version"), "history correction strategy_version"
            ),
            "source_receipts": receipts,
        }

    accepted_history = _object(
        accepted.get("history_corrections"), "accepted history corrections"
    )
    current_history = _object(
        contract.get("history_corrections"), "history_corrections"
    )
    accepted_corrections = {
        _text(
            row.get("correction_id"), "accepted history correction_id"
        ): correction_projection(row)
        for row in _objects(
            accepted_history.get("corrections"), "accepted history corrections"
        )
    }
    current_corrections = {
        _text(row.get("correction_id"), "history correction_id"): correction_projection(
            row
        )
        for row in _objects(current_history.get("corrections"), "history corrections")
    }
    if current_corrections != accepted_corrections:
        raise FinancialMvpContractError(
            "history correction version projection differs from accepted history authority"
        )

    def segment_projection(row: Mapping[str, Any]) -> dict[str, object]:
        # A live implementation identity is derived from the materialized
        # segment hash and its governed source generation.  Freezing only the
        # version-bearing fields would let a caller coherently replace both the
        # segment hash and the derived live identity while retaining a real
        # generation receipt.  #302 does not authorize any change to the
        # accepted #319 materialized rows, so bind the complete row.
        projected = dict(row)
        if projected.get("source_id") == "owner-account-live-history":
            projected["source_generation_id"] = (
                "<CURRENT_PROVIDER_LEDGER_GENERATION>"
            )
            projected["source_generation_sha256"] = (
                "<CURRENT_PROVIDER_LEDGER_RECEIPT>"
            )
        return projected

    accepted_segments = {
        _text(row.get("segment_id"), "accepted history segment_id"): segment_projection(
            row
        )
        for row in _objects(
            accepted_history.get("materialized_segment_identities"),
            "accepted history materialized_segment_identities",
        )
    }
    for segment_id, (accepted_sha256, corrected_sha256) in (
        _HISTORY_SEGMENT_SHA256_OVERRIDES.items()
    ):
        accepted_segment = accepted_segments.get(segment_id)
        if accepted_segment is None or accepted_segment.get("sha256") != (
            accepted_sha256
        ):
            raise FinancialMvpContractError(
                f"accepted history correction authority differs: {segment_id}"
            )
        accepted_segment["sha256"] = corrected_sha256
    current_segments = {
        _text(row.get("segment_id"), "history segment_id"): segment_projection(row)
        for row in _objects(
            current_history.get("materialized_segment_identities"),
            "history materialized_segment_identities",
        )
    }
    if current_segments != accepted_segments:
        raise FinancialMvpContractError(
            "materialized history version projection differs from accepted history authority"
        )

    strategies = _objects(contract["strategies"], "strategies")
    history = _object(contract["history_corrections"], "history_corrections")
    corrections = _objects(history["corrections"], "history corrections")
    verified = 0
    for strategy in strategies:
        if strategy.get("product_requirement") != "REQUIRED":
            continue
        strategy_id = _text(strategy.get("strategy_id"), "governed version strategy_id")
        version = _object(
            strategy.get("strategy_version"),
            f"governed version strategy {strategy_id}",
        )
        if version != accepted_versions.get(strategy_id):
            raise FinancialMvpContractError(
                f"strategy design/version differs from accepted #319 authority: {strategy_id}"
            )
        if version.get("status") != "EVIDENCED":
            continue
        version_ref = _text(
            version.get("version_ref"), f"governed version ref {strategy_id}"
        )
        matches = [
            row
            for row in corrections
            if row.get("strategy_id") == strategy_id
            and row.get("capture", {}).get("status") == "CAPTURED_GOVERNED"
            and row.get("strategy_version", {}).get("status") == "EVIDENCED"
            and row.get("strategy_version", {}).get("version_ref") == version_ref
        ]
        layers = {str(row.get("evidence_layer")) for row in matches}
        selectors = {
            (str(receipt.get("authority_root")), str(receipt.get("artifact_selector")))
            for row in matches
            for receipt in _objects(
                row.get("source_receipts"),
                f"governed version receipts {strategy_id}",
            )
        }
        if len(layers) < 2 or len(selectors) < 2:
            raise FinancialMvpContractError(
                "strategy design/version lacks independently verified governed "
                f"history evidence: {strategy_id}"
            )
        verified += 1
    return verified


def _census_receipt_locator(role: str) -> str:
    if role.endswith("_CURRENT_POINTER"):
        return "CURRENT_POINTER"
    if role.endswith("_GENERATION_MANIFEST"):
        return "GENERATION_MANIFEST"
    if role.endswith("_CATALOG"):
        return "GENERATION_ARTIFACT"
    raise FinancialMvpContractError(f"census evidence role differs: {role}")


def _verify_census_provider_generation(
    provider_id: str,
    generation: Mapping[str, Any],
    root: Path,
) -> None:
    generation_id = _hash(
        generation["generation_id"], f"census provider generation {provider_id}"
    )
    pointer_path = _evidence_file(root, "CURRENT.json", f"{provider_id}/CURRENT")
    pointer_hash, _pointer_size = _file_identity(pointer_path)
    if pointer_hash != generation["pointer_sha256"]:
        raise FinancialMvpContractError(
            f"census provider current pointer differs: {provider_id}"
        )
    pointer = _load_json(pointer_path, f"{provider_id} current pointer")
    manifest_path = _evidence_file(
        root,
        f"generations/{generation_id}/generation.json",
        f"{provider_id}/{generation_id}/manifest",
    )
    manifest_hash, _manifest_size = _file_identity(manifest_path)
    if (
        pointer.get("state") != "PUBLISHED"
        or pointer.get("generation_id") != generation_id
        or pointer.get("manifest_sha256") != manifest_hash
        or manifest_hash != generation["manifest_sha256"]
    ):
        raise FinancialMvpContractError(
            f"census provider generation is not current: {provider_id}"
        )
    manifest = _load_json(manifest_path, f"{provider_id} generation manifest")
    if (
        manifest.get("verification") != "VERIFIED"
        or manifest.get("generation_id") != generation_id
    ):
        raise FinancialMvpContractError(
            f"census provider generation is not verified: {provider_id}"
        )


def _census_receipt_path(census: Mapping[str, Any], role: str, root: Path) -> Path:
    receipts = _objects(census["evidence_receipts"], "census evidence_receipts")
    matches = [row for row in receipts if row["role"] == role]
    if len(matches) != 1:
        raise FinancialMvpContractError(f"census evidence role is not unique: {role}")
    return _evidence_file(root, matches[0]["artifact_selector"], role)


def verify_census_evidence(
    contract: Mapping[str, Any], authority_roots: Mapping[str, Path]
) -> dict[str, object]:
    """Verify #302 census receipts and public-safe aggregate mappings."""

    census = _object(contract["current_census"], "current_census")
    receipts = _objects(census["evidence_receipts"], "census evidence_receipts")
    verified_receipts = 0
    for receipt in receipts:
        role = _text(receipt["role"], "census evidence role")
        normalized = {
            "artifact_role": role,
            "authority_root": receipt["root"],
            "generation_id": receipt["generation_id"],
            "artifact_selector": receipt["artifact_selector"],
            "locator_kind": _census_receipt_locator(role),
            "manifest_artifact_id": receipt["manifest_artifact_id"],
            "sha256": receipt["sha256"],
        }
        _verify_history_receipt(normalized, authority_roots)
        verified_receipts += 1

    history_result = verify_history_evidence(contract, authority_roots)
    verified_strategy_versions = _verify_strategy_version_history_evidence(contract)

    provider_lanes = _objects(census["provider_lanes"], "census provider_lanes")
    governed_provider_ids: list[str] = []
    for lane in provider_lanes:
        generation = lane["generation"]
        if generation is None:
            continue
        provider_id = _text(lane["provider_id"], "census provider id")
        root = authority_roots.get(provider_id)
        if root is None:
            raise FinancialMvpContractError(
                f"census provider root was not supplied: {provider_id}"
            )
        _verify_census_provider_generation(
            provider_id,
            _object(generation, f"census provider generation {provider_id}"),
            root,
        )
        governed_provider_ids.append(provider_id)

    baseline_root = authority_roots.get("BASELINE")
    ledger_root = authority_roots.get("PROVIDER_LEDGER")
    if baseline_root is None or ledger_root is None:
        raise FinancialMvpContractError(
            "census baseline/ledger roots were not supplied"
        )
    baseline = _load_json(
        _census_receipt_path(census, "BASELINE_CATALOG", baseline_root),
        "census baseline catalog",
    )
    ledger = _load_json(
        _census_receipt_path(census, "PROVIDER_LEDGER_CATALOG", ledger_root),
        "census provider-ledger catalog",
    )

    sleeves = _objects(census["account_sleeves"], "census account_sleeves")
    expected_provider_counts = Counter(
        str(row["provider_id"]).replace("_", " ") for row in sleeves
    )
    accounts = _objects(baseline.get("accounts"), "baseline accounts")
    actual_provider_counts = Counter(
        _text(row.get("provider"), "baseline account provider") for row in accounts
    )
    if actual_provider_counts != expected_provider_counts:
        raise FinancialMvpContractError("census redacted provider counts differ")

    provider_by_account: dict[str, str] = {}
    for row in accounts:
        account_id = _text(row.get("account_id"), "baseline account identity")
        if account_id in provider_by_account:
            raise FinancialMvpContractError("baseline account identity is duplicated")
        provider_by_account[account_id] = _text(
            row.get("provider"), "baseline account provider"
        )
    actual_assignment_counts: Counter[tuple[str, str]] = Counter()
    for row in _objects(baseline.get("assignments"), "baseline assignments"):
        strategy_id = row.get("strategy_id")
        if strategy_id is None:
            continue
        account_id = _text(row.get("account_id"), "baseline assignment account")
        provider = provider_by_account.get(account_id)
        if provider is None:
            raise FinancialMvpContractError(
                "baseline assignment references an unknown account"
            )
        actual_assignment_counts[
            (provider, _text(strategy_id, "baseline strategy"))
        ] += 1
    expected_assignment_counts: Counter[tuple[str, str]] = Counter()
    for sleeve in sleeves:
        provider = str(sleeve["provider_id"]).replace("_", " ")
        for assignment in _objects(sleeve["assignments"], "census sleeve assignments"):
            expected_assignment_counts[(provider, str(assignment["strategy_id"]))] += 1
    if actual_assignment_counts != expected_assignment_counts:
        raise FinancialMvpContractError("census redacted assignment counts differ")

    lane_generations = {
        str(lane["provider_id"]).lower(): lane["generation"]
        for lane in provider_lanes
        if lane["generation"] is not None
    }
    ledger_generations = _objects(
        ledger.get("provider_generations"), "provider-ledger generations"
    )
    if {str(row.get("provider_id")) for row in ledger_generations} != set(
        lane_generations
    ):
        raise FinancialMvpContractError("census provider-ledger provider set differs")
    for row in ledger_generations:
        provider_id = str(row["provider_id"])
        generation = _object(
            lane_generations[provider_id], f"census provider generation {provider_id}"
        )
        if (
            row.get("generation_id") != generation["generation_id"]
            or row.get("manifest_sha256") != generation["manifest_sha256"]
            or row.get("generation_as_of_date") != generation["as_of"]
        ):
            raise FinancialMvpContractError(
                f"census provider-ledger generation differs: {provider_id}"
            )

    return {
        "account_sleeves": len(sleeves),
        "assignment_mappings": sum(actual_assignment_counts.values()),
        "census_sha256": census_digest(census),
        "contract_sha256": contract_digest(contract),
        "governed_provider_lanes": sorted(governed_provider_ids),
        "status": "VALID",
        "verified_history_receipts": history_result["verified_receipts"],
        "verified_receipts": verified_receipts,
        "verified_strategy_versions": verified_strategy_versions,
    }


def schema_errors(
    node: Mapping[str, Any],
    value: object,
    root: Mapping[str, Any],
    path: str = "$",
) -> list[str]:
    """Validate the deterministic JSON-Schema subset used by this contract."""

    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise FinancialMvpContractError(f"unsupported schema reference: {ref}")
        target: object = root
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                raise FinancialMvpContractError(f"unresolved schema reference: {ref}")
            target = target[part]
        if not isinstance(target, dict):
            raise FinancialMvpContractError(f"schema reference is not an object: {ref}")
        return schema_errors(target, value, root, path)

    errors: list[str] = []
    negated = node.get("not")
    if negated is not None:
        if not isinstance(negated, dict):
            raise FinancialMvpContractError("schema not must be an object")
        if not schema_errors(negated, value, root, path):
            errors.append(f"{path}: must not match")
    all_of = node.get("allOf", [])
    if not isinstance(all_of, list):
        raise FinancialMvpContractError("schema allOf must be an array")
    for child in all_of:
        if not isinstance(child, dict):
            raise FinancialMvpContractError("schema allOf child must be an object")
        errors.extend(schema_errors(child, value, root, path))
    if "if" in node:
        condition = node["if"]
        if not isinstance(condition, dict):
            raise FinancialMvpContractError("schema if must be an object")
        branch = (
            node.get("then")
            if not schema_errors(condition, value, root, path)
            else node.get("else")
        )
        if branch is not None:
            if not isinstance(branch, dict):
                raise FinancialMvpContractError("schema branch must be an object")
            errors.extend(schema_errors(branch, value, root, path))
    if "const" in node and value != node["const"]:
        errors.append(f"{path}: const differs")
    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise FinancialMvpContractError("schema enum must be an array")
        if value not in enum:
            errors.append(f"{path}: enum differs")

    declared_type = node.get("type")
    if isinstance(declared_type, list):
        type_names = declared_type
    elif isinstance(declared_type, str):
        type_names = [declared_type]
    elif declared_type is None:
        type_names = []
    else:
        raise FinancialMvpContractError("schema type must be text or an array")
    matches_type = not type_names or any(
        (
            type_name == "null"
            and value is None
            or type_name == "object"
            and isinstance(value, dict)
            or type_name == "array"
            and isinstance(value, list)
            or type_name == "string"
            and isinstance(value, str)
            or type_name == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or type_name == "boolean"
            and isinstance(value, bool)
        )
        for type_name in type_names
    )
    if not matches_type:
        errors.append(f"{path}: type differs")
        return errors

    if isinstance(value, str):
        minimum_length = node.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}: minLength")
        pattern = node.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}: pattern")
        if node.get("format") == "date":
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: date format")
            else:
                if parsed.isoformat() != value:
                    errors.append(f"{path}: date format")
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = node.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            errors.append(f"{path}: minimum")
    if isinstance(value, list):
        minimum_items = node.get("minItems")
        maximum_items = node.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path}: minItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path}: maxItems")
        if node.get("uniqueItems") is True:
            serialized = [json.dumps(item, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path}: uniqueItems")
        child = node.get("items")
        if child is not None:
            if not isinstance(child, dict):
                raise FinancialMvpContractError("schema items must be an object")
            for index, item in enumerate(value):
                errors.extend(schema_errors(child, item, root, f"{path}[{index}]"))
    if isinstance(value, dict):
        required = node.get("required", [])
        properties = node.get("properties", {})
        if not isinstance(required, list) or not isinstance(properties, dict):
            raise FinancialMvpContractError(
                "schema required/properties must be arrays/objects"
            )
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing {key}")
        additional = node.get("additionalProperties", True)
        for key, child_value in value.items():
            if key in properties:
                continue
            if additional is False:
                errors.append(f"{path}: unexpected {key}")
            elif isinstance(additional, dict):
                errors.extend(
                    schema_errors(additional, child_value, root, f"{path}.{key}")
                )
        minimum_properties = node.get("minProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            errors.append(f"{path}: minProperties")
        for key, child in properties.items():
            if key in value:
                if not isinstance(child, dict):
                    raise FinancialMvpContractError(
                        f"schema property is not an object: {key}"
                    )
                errors.extend(schema_errors(child, value[key], root, f"{path}.{key}"))
    return errors


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FinancialMvpContractError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise FinancialMvpContractError(f"{name} must contain objects")
    return list(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialMvpContractError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _date(value: object, name: str) -> date:
    accepted = _text(value, name)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise FinancialMvpContractError(f"{name} must be a canonical ISO date") from exc
    if parsed.isoformat() != accepted:
        raise FinancialMvpContractError(f"{name} must be a canonical ISO date")
    return parsed


def _optional_date(value: object, name: str) -> date | None:
    if value is None:
        return None
    return _date(value, name)


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise FinancialMvpContractError(f"{name} must be an array")
    rows = [_text(item, name) for item in value]
    if len(rows) != len(set(rows)):
        raise FinancialMvpContractError(f"{name} must be unique")
    return rows


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FinancialMvpContractError(
            f"{name} keys differ; missing={missing}, extra={extra}"
        )


def _hash(value: object, name: str) -> str:
    accepted = _text(value, name)
    if not _HASH_RE.fullmatch(accepted):
        raise FinancialMvpContractError(f"{name} must be lowercase SHA-256")
    return accepted


def _git_sha(value: object, name: str) -> str:
    accepted = _text(value, name)
    if not _GIT_SHA_RE.fullmatch(accepted):
        raise FinancialMvpContractError(f"{name} must be a full lowercase Git SHA")
    return accepted


def _optional_hash(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _hash(value, name)


def _blockers(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    ):
        raise FinancialMvpContractError(f"{name} must contain positive issue numbers")
    if len(value) != len(set(value)):
        raise FinancialMvpContractError(f"{name} must be unique")
    return list(value)


def _no_private_locator(value: object, name: str) -> str:
    accepted = _text(value, name)
    if _WINDOWS_ABSOLUTE_RE.match(accepted) or accepted.startswith(("/", "\\")):
        raise FinancialMvpContractError(
            f"{name} must not contain an absolute workstation path"
        )
    if "account-" in accepted.lower():
        raise FinancialMvpContractError(
            f"{name} must not contain a private account identifier"
        )
    return accepted


def _reject_private_values(value: object, name: str = "contract") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_private_values(child, f"{name}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_private_values(child, f"{name}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _WINDOWS_ABSOLUTE_RE.match(value) or value.startswith(("/", "\\\\")):
        raise FinancialMvpContractError(
            f"{name} must not contain an absolute workstation path"
        )
    if _PRIVATE_ACCOUNT_RE.search(value):
        raise FinancialMvpContractError(
            f"{name} must not contain a private account identifier"
        )


def _decimal(value: object, name: str) -> Decimal:
    accepted = _text(value, name)
    try:
        parsed = Decimal(accepted)
    except ArithmeticError as exc:
        raise FinancialMvpContractError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise FinancialMvpContractError(f"{name} must be a finite decimal string")
    return parsed


def _payload_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes({"payload": value})).hexdigest()


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise FinancialMvpContractError("golden dependence series length differs")
    count = Decimal(len(left))
    left_mean = sum(left, Decimal(0)) / count
    right_mean = sum(right, Decimal(0)) / count
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    numerator = sum(
        (a * b for a, b in zip(left_delta, right_delta, strict=True)), Decimal(0)
    )
    left_square = sum((value * value for value in left_delta), Decimal(0))
    right_square = sum((value * value for value in right_delta), Decimal(0))
    if left_square == 0 or right_square == 0:
        raise FinancialMvpContractError("golden dependence series is constant")
    return numerator / (left_square * right_square).sqrt()


def _spearman(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(set(left)) != len(left) or len(set(right)) != len(right):
        raise FinancialMvpContractError("golden Spearman fixture must not contain ties")
    left_rank = {value: Decimal(index + 1) for index, value in enumerate(sorted(left))}
    right_rank = {
        value: Decimal(index + 1) for index, value in enumerate(sorted(right))
    }
    return _pearson(
        [left_rank[value] for value in left],
        [right_rank[value] for value in right],
    )


def contract_digest(contract: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the contract's canonical authoritative bytes."""

    return hashlib.sha256(canonical_json_bytes(dict(contract))).hexdigest()


def _outcome_calculation_contract(
    contract: Mapping[str, Any], outcome_id: str
) -> dict[str, Any]:
    outcomes = _objects(contract.get("financial_outcomes"), "financial_outcomes")
    matches = [row for row in outcomes if row.get("outcome_id") == outcome_id]
    if len(matches) != 1:
        raise FinancialMvpContractError(f"outcome identity differs: {outcome_id}")
    return _object(matches[0].get("calculation_contract"), f"{outcome_id} contract")


def invested_capital_policy_digest(contract: Mapping[str, Any]) -> str:
    """Return the identity of the one authored invested-capital policy block."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    return hashlib.sha256(canonical_json_bytes(policy)).hexdigest()


def _owner_decision_payloads(
    policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    treatments = _object(
        policy.get("classification_treatments"),
        "invested-capital classification_treatments",
    )
    return {
        "MARGIN_AND_LEVERAGE": {
            "decision": treatments.get("MARGIN_FINANCING"),
            "leverage_accounting": policy.get("leverage_accounting"),
        },
        "PREDECESSOR_TRANSITION": {
            "decision": treatments.get("PREDECESSOR_TRANSITION")
        },
        "CORRECTION_REVERSAL": {"decision": treatments.get("CORRECTION_REVERSAL")},
        "NEGATIVE_NET_CONTRIBUTIONS": {
            "decision": policy.get("negative_net_contributions")
        },
        "FX_BASE_CURRENCY": {"decision": policy.get("fx_base_currency")},
    }


def owner_decision_receipt_digests(
    contract: Mapping[str, Any],
) -> dict[str, str]:
    """Return hashes that bind each independently approved #318 decision."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    return _owner_decision_payload_digests(policy)


def _owner_decision_payload_digests(
    policy: Mapping[str, Any],
) -> dict[str, str]:
    return {
        decision_id: hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        for decision_id, payload in _owner_decision_payloads(policy).items()
    }


def _validate_owner_decision_receipts(policy: Mapping[str, Any]) -> None:
    receipts = _object(
        policy.get("owner_decision_receipts"),
        "invested-capital owner_decision_receipts",
    )
    _strict_keys(
        receipts,
        set(OWNER_DECISION_IDS),
        "invested-capital owner_decision_receipts",
    )
    payload_digests = _owner_decision_payload_digests(policy)
    treatments = _object(
        policy["classification_treatments"],
        "invested-capital classification_treatments",
    )
    leverage = _object(
        policy["leverage_accounting"], "invested-capital leverage_accounting"
    )
    evidence_by_decision = {
        "MARGIN_AND_LEVERAGE": {
            treatments["MARGIN_FINANCING"]["approval_evidence"],
            leverage["approval_evidence"],
        },
        "PREDECESSOR_TRANSITION": {
            treatments["PREDECESSOR_TRANSITION"]["approval_evidence"]
        },
        "CORRECTION_REVERSAL": {treatments["CORRECTION_REVERSAL"]["approval_evidence"]},
        "NEGATIVE_NET_CONTRIBUTIONS": {
            policy["negative_net_contributions"]["approval_evidence"]
        },
        "FX_BASE_CURRENCY": {policy["fx_base_currency"]["approval_evidence"]},
    }
    receipt_keys = {
        "approved_on",
        "authority_issue",
        "comment_id",
        "decision_id",
        "digest_comment_id",
        "digest_evidence_url",
        "evidence_url",
        "payload_sha256",
        "policy_owner_issue",
    }
    for decision_id in OWNER_DECISION_IDS:
        receipt = _object(receipts[decision_id], f"owner receipt {decision_id}")
        _strict_keys(receipt, receipt_keys, f"owner receipt {decision_id}")
        if receipt["decision_id"] != decision_id:
            raise FinancialMvpContractError(
                f"owner decision receipt identity differs: {decision_id}"
            )
        if receipt["authority_issue"] != 330 or receipt["policy_owner_issue"] != 318:
            raise FinancialMvpContractError(
                f"owner decision receipt authority differs: {decision_id}"
            )
        comment_id = receipt["comment_id"]
        if (
            not isinstance(comment_id, int)
            or isinstance(comment_id, bool)
            or comment_id < 1
        ):
            raise FinancialMvpContractError(
                f"owner decision receipt comment differs: {decision_id}"
            )
        evidence_url = _text(
            receipt["evidence_url"], f"owner receipt {decision_id}.evidence_url"
        )
        expected_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{comment_id}"
        )
        if evidence_url != expected_url or evidence_by_decision[decision_id] != {
            evidence_url
        }:
            raise FinancialMvpContractError(
                f"owner decision receipt evidence differs: {decision_id}"
            )
        digest_comment_id = receipt["digest_comment_id"]
        if (
            not isinstance(digest_comment_id, int)
            or isinstance(digest_comment_id, bool)
            or digest_comment_id < 1
        ):
            raise FinancialMvpContractError(
                f"owner decision digest comment differs: {decision_id}"
            )
        digest_evidence_url = _text(
            receipt["digest_evidence_url"],
            f"owner receipt {decision_id}.digest_evidence_url",
        )
        if digest_evidence_url != (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{digest_comment_id}"
        ):
            raise FinancialMvpContractError(
                f"owner decision digest evidence differs: {decision_id}"
            )
        approved_on = _text(
            receipt["approved_on"], f"owner receipt {decision_id}.approved_on"
        )
        try:
            approved_date = date.fromisoformat(approved_on)
        except ValueError as exc:
            raise FinancialMvpContractError(
                f"owner decision receipt date differs: {decision_id}"
            ) from exc
        if approved_date > date.today():
            raise FinancialMvpContractError(
                f"owner decision receipt date is in the future: {decision_id}"
            )
        if (
            _hash(
                receipt["payload_sha256"], f"owner receipt {decision_id}.payload_sha256"
            )
            != payload_digests[decision_id]
        ):
            raise FinancialMvpContractError(
                f"owner decision receipt differs: {decision_id}"
            )


def _owner_receipt_record(body: object) -> dict[str, Any]:
    text = _text(body, "owner digest comment body")
    if text.count(_OWNER_RECEIPT_MARKER) != 1:
        raise FinancialMvpContractError("owner digest comment marker differs")
    matches = _JSON_FENCE_RE.findall(text)
    if len(matches) != 1:
        raise FinancialMvpContractError("owner digest comment JSON block differs")
    try:
        record = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise FinancialMvpContractError(
            "owner digest comment JSON must be readable"
        ) from exc
    return _object(record, "owner digest comment record")


def owner_receipt_evidence_record(
    contract: Mapping[str, Any], decision_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """Return the exact machine record an owner digest comment must contain."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    selected_ids = tuple(decision_ids or OWNER_DECISION_IDS)
    if not selected_ids or not set(selected_ids) <= set(OWNER_DECISION_IDS):
        raise FinancialMvpContractError("owner evidence decision set differs")
    approval_urls = {receipts[key]["evidence_url"] for key in selected_ids}
    approved_dates = {receipts[key]["approved_on"] for key in selected_ids}
    if len(approval_urls) != 1 or len(approved_dates) != 1:
        raise FinancialMvpContractError(
            "owner receipt set must share approval evidence and date"
        )
    return {
        "schema_version": 1,
        "authority_issue": 330,
        "policy_owner_issue": 318,
        "policy_id": policy["policy_id"],
        "approval_evidence_url": next(iter(approval_urls)),
        "approved_on": next(iter(approved_dates)),
        "decision_payload_sha256": {
            decision_id: receipts[decision_id]["payload_sha256"]
            for decision_id in selected_ids
        },
    }


def reconcile_owner_receipt_evidence(
    contract: Mapping[str, Any], comments: Mapping[int, Mapping[str, Any]]
) -> None:
    """Reconcile local decision payloads to owner-controlled GitHub comments."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    approval_comment_ids = {receipts[key]["comment_id"] for key in OWNER_DECISION_IDS}
    digest_comment_ids = {
        receipts[key]["digest_comment_id"] for key in OWNER_DECISION_IDS
    }
    required_comment_ids = approval_comment_ids | digest_comment_ids
    if set(comments) != required_comment_ids:
        raise FinancialMvpContractError("owner evidence comment set differs")
    for comment_id in sorted(required_comment_ids):
        comment = _object(comments[comment_id], f"GitHub comment {comment_id}")
        user = _object(comment.get("user"), f"GitHub comment {comment_id}.user")
        if (
            user.get("login") != "dachent"
            or comment.get("author_association") != "OWNER"
        ):
            raise FinancialMvpContractError(
                f"owner evidence author differs: {comment_id}"
            )
        if comment.get("created_at") != comment.get("updated_at"):
            raise FinancialMvpContractError(
                f"owner evidence comment was edited: {comment_id}"
            )

    for decision_id in OWNER_DECISION_IDS:
        receipt = receipts[decision_id]
        approval_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{receipt['comment_id']}"
        )
        digest_url = (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{receipt['digest_comment_id']}"
        )
        if comments[receipt["comment_id"]].get("html_url") != approval_url:
            raise FinancialMvpContractError(
                f"owner approval comment locator differs: {decision_id}"
            )
        if comments[receipt["digest_comment_id"]].get("html_url") != digest_url:
            raise FinancialMvpContractError(
                f"owner digest comment locator differs: {decision_id}"
            )
        if (
            receipt["evidence_url"] != approval_url
            or receipt["digest_evidence_url"] != digest_url
        ):
            raise FinancialMvpContractError(
                f"owner evidence receipt linkage differs: {decision_id}"
            )
    for digest_comment_id in sorted(digest_comment_ids):
        decision_ids = tuple(
            decision_id
            for decision_id in OWNER_DECISION_IDS
            if receipts[decision_id]["digest_comment_id"] == digest_comment_id
        )
        record = _owner_receipt_record(comments[digest_comment_id].get("body"))
        _strict_keys(
            record,
            {
                "approval_evidence_url",
                "approved_on",
                "authority_issue",
                "decision_payload_sha256",
                "policy_id",
                "policy_owner_issue",
                "schema_version",
            },
            "owner digest comment record",
        )
        record_digests = _object(
            record["decision_payload_sha256"],
            "owner digest comment decision_payload_sha256",
        )
        if not record_digests or not set(record_digests) <= set(OWNER_DECISION_IDS):
            raise FinancialMvpContractError("owner digest decision set differs")
        for recorded_id, recorded_digest in record_digests.items():
            _hash(recorded_digest, f"owner digest {recorded_id}")
        expected_record = owner_receipt_evidence_record(contract, decision_ids)
        if (
            record["schema_version"] != expected_record["schema_version"]
            or record["authority_issue"] != expected_record["authority_issue"]
            or record["policy_owner_issue"] != expected_record["policy_owner_issue"]
            or record["policy_id"] != expected_record["policy_id"]
            or record["approval_evidence_url"]
            != expected_record["approval_evidence_url"]
            or record["approved_on"] != expected_record["approved_on"]
            or any(
                record_digests.get(decision_id)
                != expected_record["decision_payload_sha256"][decision_id]
                for decision_id in decision_ids
            )
        ):
            raise FinancialMvpContractError(
                "owner digest evidence does not approve current decision payloads"
            )


def fetch_owner_evidence_comments(
    contract: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Fetch only the GitHub comments referenced by the receipt set."""

    policy = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    receipts = _object(
        policy["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    comment_ids = {
        receipts[decision_id][field]
        for decision_id in OWNER_DECISION_IDS
        for field in ("comment_id", "digest_comment_id")
    }
    comments: dict[int, dict[str, Any]] = {}
    for comment_id in sorted(comment_ids):
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "gh",
                    "api",
                    _GITHUB_COMMENT_API.format(comment_id=comment_id),
                    "--method",
                    "GET",
                ],
                capture_output=True,
                check=False,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise FinancialMvpContractError(
                f"owner evidence comment is unreachable: {comment_id}"
            ) from exc
        if result.returncode != 0:
            raise FinancialMvpContractError(
                f"owner evidence comment is unreachable: {comment_id}"
            )
        payload = result.stdout
        if len(payload) > 1_000_000:
            raise FinancialMvpContractError(
                f"owner evidence comment is oversized: {comment_id}"
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinancialMvpContractError(
                f"owner evidence comment is not JSON: {comment_id}"
            ) from exc
        comments[comment_id] = _object(value, f"GitHub comment {comment_id}")
    return comments


def fetch_census_owner_evidence() -> dict[str, Any]:
    """Fetch the one owner-authored #302 decision comment."""

    try:
        result = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                _GITHUB_COMMENT_API.format(comment_id=_CENSUS_OWNER_COMMENT_ID),
                "--method",
                "GET",
            ],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise FinancialMvpContractError(
            "census owner evidence comment is unreachable"
        ) from exc
    if result.returncode != 0 or len(result.stdout) > 1_000_000:
        raise FinancialMvpContractError("census owner evidence comment is unreachable")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinancialMvpContractError(
            "census owner evidence comment is not JSON"
        ) from exc
    return _object(value, "census owner evidence comment")


def reconcile_census_owner_evidence(
    contract: Mapping[str, Any], comment: Mapping[str, Any]
) -> None:
    """Bind the local census decision digest to the unedited owner comment."""

    census = _object(contract.get("current_census"), "current_census")
    user = _object(comment.get("user"), "census owner evidence user")
    expected_url = (
        "https://github.com/dachent/quant_engine/issues/302#issuecomment-"
        f"{_CENSUS_OWNER_COMMENT_ID}"
    )
    if (
        user.get("login") != "dachent"
        or comment.get("author_association") != "OWNER"
        or comment.get("created_at") != comment.get("updated_at")
        or comment.get("html_url") != expected_url
        or census.get("decision_evidence_url") != expected_url
    ):
        raise FinancialMvpContractError("census owner evidence authority differs")
    body = _text(comment.get("body"), "census owner evidence body")
    matches = _JSON_FENCE_RE.findall(body)
    if len(matches) != 1:
        raise FinancialMvpContractError("census owner decision JSON block differs")
    try:
        decision = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise FinancialMvpContractError(
            "census owner decision JSON is invalid"
        ) from exc
    record = _object(decision, "census owner decision")
    if (
        record.get("decision_id")
        != "DATA-CENSUS-001-OWNER-PERIMETER-AND-MEMBERSHIP-2026-08-19"
        or record.get("authority_issue") != 302
        or record.get("approved_on") != "2026-08-19"
    ):
        raise FinancialMvpContractError("census owner decision identity differs")
    actual_digest = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
    if actual_digest != census.get("decision_payload_sha256"):
        raise FinancialMvpContractError(
            "census owner evidence does not approve the current decision payload"
        )
    local_projection = census_owner_decision_projection(
        census, _objects(contract.get("strategies"), "strategies")
    )
    if local_projection != record:
        raise FinancialMvpContractError(
            "census owner evidence does not approve current contract semantics"
        )


_POLICY_DECISION_KEYS: Final = {
    "approval_evidence",
    "calculation_effect",
    "decision_authority",
    "decision_status",
    "examples",
    "plain_language",
    "recommended_treatment",
    "selected_treatment",
}


def _validate_policy_decision(value: object, name: str) -> dict[str, Any]:
    decision = _object(value, name)
    _strict_keys(decision, _POLICY_DECISION_KEYS, name)
    _text(decision["plain_language"], f"{name}.plain_language")
    _text(decision["calculation_effect"], f"{name}.calculation_effect")
    _strings(decision["examples"], f"{name}.examples")
    status = _text(decision["decision_status"], f"{name}.decision_status")
    authority = _text(decision["decision_authority"], f"{name}.decision_authority")
    selected = decision["selected_treatment"]
    recommended = decision["recommended_treatment"]
    evidence = decision["approval_evidence"]
    if selected is not None:
        _text(selected, f"{name}.selected_treatment")
    if recommended is not None:
        _text(recommended, f"{name}.recommended_treatment")
    if evidence is not None:
        _text(evidence, f"{name}.approval_evidence")

    if status == "SETTLED_BY_PRODUCT_DEFINITION":
        if (
            authority != "FINANCIAL_MVP_DEFINITION"
            or selected is None
            or recommended is not None
            or evidence is None
        ):
            raise FinancialMvpContractError(
                f"{name} product-definition decision is incomplete"
            )
    elif status == "SETTLED_BY_EXISTING_POLICY":
        if (
            authority != "PORT_PERF_1"
            or selected is None
            or recommended is not None
            or evidence is None
        ):
            raise FinancialMvpContractError(
                f"{name} existing-policy decision is incomplete"
            )
    elif status == "OWNER_DECISION_REQUIRED":
        if (
            authority != "OWNER_318"
            or selected is not None
            or recommended is None
            or evidence is not None
        ):
            raise FinancialMvpContractError(
                f"{name} owner decision must remain explicitly unresolved"
            )
    elif status == "DEPENDENCY_REQUIRED":
        if (
            authority != "SHARED_FLOW_POLICY_324"
            or selected is not None
            or recommended is None
            or evidence is not None
        ):
            raise FinancialMvpContractError(
                f"{name} shared-flow dependency must remain explicitly unresolved"
            )
    elif status == "APPROVED":
        if (
            authority not in {"OWNER_318", "SHARED_FLOW_POLICY_324"}
            or selected is None
            or evidence is None
        ):
            raise FinancialMvpContractError(f"{name} approval record is incomplete")
    else:
        raise FinancialMvpContractError(f"{name} decision status differs")
    return decision


def _validate_leverage_accounting(
    value: object, margin_decision: Mapping[str, Any]
) -> None:
    leverage = _object(value, "invested-capital leverage_accounting")
    _strict_keys(
        leverage,
        {
            "affected_metrics",
            "approval_evidence",
            "consumers",
            "double_counting_guard",
            "invested_capital_treatment",
            "missing_fact_behavior",
            "required_facts",
            "status",
            "synthetic_vector",
        },
        "invested-capital leverage_accounting",
    )
    if leverage["status"] != "APPROVED":
        raise FinancialMvpContractError("leverage accounting must be owner approved")
    if (
        leverage["approval_evidence"] != margin_decision["approval_evidence"]
        or leverage["invested_capital_treatment"]
        != margin_decision["selected_treatment"]
    ):
        raise FinancialMvpContractError(
            "leverage accounting must bind the approved margin decision"
        )
    for key in (
        "approval_evidence",
        "double_counting_guard",
        "invested_capital_treatment",
        "missing_fact_behavior",
    ):
        _text(leverage[key], f"invested-capital leverage_accounting.{key}")
    if set(leverage["required_facts"]) != {
        "BORROWED_PRINCIPAL",
        "FINANCING_COST",
        "GROSS_EXPOSURE",
        "NAV",
        "OWNER_EXTERNAL_FLOWS",
    }:
        raise FinancialMvpContractError("leverage required-fact set differs")
    if set(leverage["consumers"]) != {
        *OUTCOME_IDS,
        "REPORT",
        "WORKBOOK",
        "PANEL",
        "RELEASE_RECEIPT",
    }:
        raise FinancialMvpContractError("leverage consumer set differs")
    metrics = _object(
        leverage["affected_metrics"], "invested-capital leverage affected_metrics"
    )
    _strict_keys(
        metrics,
        {
            "DECOMPOSITION_DEPENDENCE",
            "HWM_DRAWDOWN",
            "MWR",
            "TOTAL_RETURN",
            "TWR",
        },
        "invested-capital leverage affected_metrics",
    )
    for metric_id, treatment in metrics.items():
        _text(treatment, f"leverage affected metric {metric_id}")

    vector = _object(
        leverage["synthetic_vector"], "invested-capital leverage synthetic_vector"
    )
    vector_keys = {
        "asset_return",
        "borrowed_principal",
        "ending_nav",
        "financing_cost",
        "gross_assets_end",
        "gross_assets_start",
        "hwm_index_end",
        "hwm_index_start",
        "invested_capital",
        "mwr_one_year",
        "owner_contributed_capital",
        "owner_external_flows",
        "total_return",
        "twr",
    }
    _strict_keys(vector, vector_keys, "invested-capital leverage synthetic_vector")
    values = {
        key: _decimal(value, f"leverage synthetic_vector.{key}")
        for key, value in vector.items()
    }
    owner_capital = values["owner_contributed_capital"]
    borrowed = values["borrowed_principal"]
    gross_start = values["gross_assets_start"]
    financing_cost = values["financing_cost"]
    owner_flows = values["owner_external_flows"]
    if owner_capital <= 0 or borrowed <= 0 or gross_start <= 0 or financing_cost < 0:
        raise FinancialMvpContractError("leverage synthetic inputs must be positive")
    if owner_flows != 0:
        raise FinancialMvpContractError(
            "leverage synthetic vector must isolate zero owner external flow"
        )
    if gross_start != owner_capital + borrowed:
        raise FinancialMvpContractError(
            "leverage synthetic opening balance sheet differs"
        )
    gross_end = gross_start * (Decimal(1) + values["asset_return"]) - financing_cost
    ending_nav = gross_end - borrowed
    invested_capital = owner_capital + owner_flows
    total_return = (ending_nav - owner_flows) / owner_capital - Decimal(1)
    twr = total_return
    mwr = ending_nav / owner_capital - Decimal(1)
    hwm_end = values["hwm_index_start"] * (Decimal(1) + twr)
    expected = {
        "gross_assets_end": gross_end,
        "ending_nav": ending_nav,
        "invested_capital": invested_capital,
        "total_return": total_return,
        "twr": twr,
        "mwr_one_year": mwr,
        "hwm_index_end": hwm_end,
    }
    for key, calculated in expected.items():
        if values[key] != calculated:
            raise FinancialMvpContractError(f"leverage synthetic value differs: {key}")


def _registry_strategy_ids(registry: Mapping[str, Any]) -> list[str]:
    rows = _objects(registry.get("strategies"), "strategy registry.strategies")
    return [
        _text(row.get("strategy_id"), "strategy registry.strategy_id") for row in rows
    ]


def _validate_product(product: Mapping[str, Any]) -> None:
    _strict_keys(
        product,
        {
            "as_of_policy",
            "final_artifacts",
            "operator_outcome",
            "product_id",
            "product_version",
            "publication_boundary",
            "repository_compatibility",
            "runtime_profile",
        },
        "product",
    )
    if _text(product["product_id"], "product.product_id") != "FINANCIAL_MVP":
        raise FinancialMvpContractError("product.product_id must be FINANCIAL_MVP")
    version = _text(product["product_version"], "product.product_version")
    if not _SEMVER_RE.fullmatch(version):
        raise FinancialMvpContractError("product.product_version must be semantic")
    if (
        _text(product["runtime_profile"], "product.runtime_profile")
        != "PORTFOLIO_SOURCE_TO_PANEL_V1"
    ):
        raise FinancialMvpContractError(
            "product must extend the current pipeline profile"
        )
    _text(product["operator_outcome"], "product.operator_outcome")
    as_of = _object(product["as_of_policy"], "product.as_of_policy")
    _strict_keys(
        as_of,
        {
            "calculation_boundary",
            "decision_evidence",
            "execution_profiles",
            "policy_id",
            "publication_lag_rule",
        },
        "as_of_policy",
    )
    if (
        _text(as_of["policy_id"], "as_of_policy.policy_id")
        != "LATEST_COMMON_COMPLETE_ACCEPTED_BOUNDARY_WARN_ON_AGE_V2"
    ):
        raise FinancialMvpContractError("as_of policy identity differs")
    boundary = _object(
        as_of["calculation_boundary"], "as_of_policy.calculation_boundary"
    )
    _strict_keys(
        boundary,
        {
            "age_disposition",
            "calendar_rule",
            "completeness_failure_disposition",
            "post_boundary_rows",
            "required_evidence",
            "rule",
            "scope",
        },
        "as_of_policy.calculation_boundary",
    )
    if (
        boundary["rule"]
        != "LATEST_COMMON_COMPLETE_ACCEPTED_DATE_ACROSS_REQUIRED_CALCULATION_INPUTS"
    ):
        raise FinancialMvpContractError("calculation boundary rule differs")
    if (
        boundary["age_disposition"]
        != "WARN_WITH_COMPLETE_THROUGH_DATE_AGE_DAYS_THRESHOLD_AND_LIMITING_INPUTS"
    ):
        raise FinancialMvpContractError("calculation boundary age disposition differs")
    if boundary["completeness_failure_disposition"] != "FAIL_CLOSED":
        raise FinancialMvpContractError(
            "calculation completeness must remain fail-closed"
        )
    if boundary["scope"] != "REQUIRED_INPUTS_OF_EACH_CALCULATION":
        raise FinancialMvpContractError("calculation boundary scope differs")
    if (
        boundary["post_boundary_rows"]
        != "RETAIN_IN_SOURCE_EVIDENCE_EXCLUDE_FROM_CALCULATION"
    ):
        raise FinancialMvpContractError("post-boundary row disposition differs")
    if boundary["required_evidence"] != [
        "EVALUATED_ON_DATE",
        "COMPLETE_THROUGH_DATE",
        "AGE_DAYS",
        "WARNING_AFTER_DAYS",
        "LIMITING_INPUTS",
    ]:
        raise FinancialMvpContractError("calculation boundary evidence differs")
    _text(boundary["calendar_rule"], "calculation_boundary.calendar_rule")
    decision_evidence = _object(
        as_of["decision_evidence"], "as_of_policy.decision_evidence"
    )
    _strict_keys(
        decision_evidence,
        {"authority_issue", "evidence_url", "recorded_on"},
        "as_of_policy.decision_evidence",
    )
    if decision_evidence["authority_issue"] != 320:
        raise FinancialMvpContractError("as_of policy authority issue differs")
    evidence_url = _text(decision_evidence["evidence_url"], "as_of_policy.evidence_url")
    if not evidence_url.startswith(
        "https://github.com/dachent/quant_engine/issues/320#issuecomment-"
    ):
        raise FinancialMvpContractError("as_of policy evidence URL differs")
    _date(decision_evidence["recorded_on"], "as_of_policy.recorded_on")
    profiles = _object(as_of["execution_profiles"], "as_of_policy.execution_profiles")
    _strict_keys(
        profiles,
        {"DEVELOPMENT", "WORKSTATION_QC", "RELEASE_QC"},
        "execution_profiles",
    )
    if (
        profiles["DEVELOPMENT"]
        != "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        or profiles["WORKSTATION_QC"]
        != "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        or profiles["RELEASE_QC"]
        != "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
    ):
        raise FinancialMvpContractError("as_of execution profiles differ")
    _text(as_of["publication_lag_rule"], "as_of_policy.publication_lag_rule")
    compatibility = _object(
        product["repository_compatibility"], "product.repository_compatibility"
    )
    _strict_keys(
        compatibility,
        {"minimum_main_sha", "pipeline_profile", "vertical_recipe_schema_versions"},
        "repository_compatibility",
    )
    _git_sha(compatibility["minimum_main_sha"], "minimum_main_sha")
    if compatibility["pipeline_profile"] != product["runtime_profile"]:
        raise FinancialMvpContractError("repository pipeline profile differs")
    versions = compatibility["vertical_recipe_schema_versions"]
    if versions != [6, 7]:
        raise FinancialMvpContractError(
            "current vertical recipe compatibility must be [6, 7]"
        )
    artifacts = _objects(product["final_artifacts"], "product.final_artifacts")
    expected_roles = {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}
    roles: set[str] = set()
    for row in artifacts:
        _strict_keys(row, {"acceptance", "role"}, "final_artifact")
        role = _text(row["role"], "final_artifact.role")
        roles.add(role)
        _text(row["acceptance"], "final_artifact.acceptance")
    if roles != expected_roles:
        raise FinancialMvpContractError(
            "final artifacts must name report/workbook/panel/receipt"
        )
    publication = _object(
        product["publication_boundary"], "product.publication_boundary"
    )
    _strict_keys(
        publication,
        {"authority", "required_issue_sequence", "status"},
        "publication_boundary",
    )
    if publication["status"] != "NOT_AUTHORIZED_BY_THIS_CONTRACT":
        raise FinancialMvpContractError("contract must not authorize publication")
    if publication["authority"] != "SEPARATE_EXPLICIT_RELEASE_AUTHORIZATION":
        raise FinancialMvpContractError("publication authority boundary differs")
    if publication["required_issue_sequence"] != [332, 283]:
        raise FinancialMvpContractError("publication must remain behind #332 and #283")


def _validate_strategies(
    strategies: list[dict[str, Any]], registry_ids: Sequence[str]
) -> None:
    if [row.get("strategy_id") for row in strategies] != list(registry_ids):
        raise FinancialMvpContractError(
            "current-product strategy order/identity differs from the registry"
        )
    for row in strategies:
        _strict_keys(
            row,
            {
                "blockers",
                "effective_product_interval",
                "funded_membership",
                "history_series_id",
                "lifecycle_status",
                "live_implementation_identity",
                "outcome_dispositions",
                "product_requirement",
                "readiness",
                "readiness_reason",
                "strategy_id",
                "strategy_version",
            },
            "strategy",
        )
        strategy_id = _text(row["strategy_id"], "strategy.strategy_id")
        _optional_text(row["history_series_id"], "strategy.history_series_id")
        lifecycle = _text(row["lifecycle_status"], "strategy.lifecycle_status")
        if lifecycle not in {
            "ACTIVE",
            "DEPRECATED_HISTORY_ONLY",
            "IMMATERIAL_EXCLUDED",
        }:
            raise FinancialMvpContractError(
                f"unknown strategy lifecycle for {strategy_id}: {lifecycle}"
            )
        live_identity = _object(
            row["live_implementation_identity"], "live_implementation_identity"
        )
        _strict_keys(
            live_identity,
            {"generation_id", "receipt_sha256", "status", "version_ref"},
            "live_implementation_identity",
        )
        live_status = _text(
            live_identity["status"], "live_implementation_identity.status"
        )
        if live_status not in {"EVIDENCED", "NOT_APPLICABLE", "REJECTED"}:
            raise FinancialMvpContractError(
                f"live implementation status differs: {strategy_id}"
            )
        live_version = _optional_text(
            live_identity["version_ref"], "live_implementation_identity.version_ref"
        )
        live_generation = _optional_hash(
            live_identity["generation_id"], "live_implementation_identity.generation_id"
        )
        live_receipt = _optional_hash(
            live_identity["receipt_sha256"],
            "live_implementation_identity.receipt_sha256",
        )
        if live_status in {"EVIDENCED", "REJECTED"} and (
            live_version is None or live_generation is None or live_receipt is None
        ):
            raise FinancialMvpContractError(
                f"live implementation identity is incomplete: {strategy_id}"
            )
        if live_status == "NOT_APPLICABLE" and any(
            value is not None for value in (live_version, live_generation, live_receipt)
        ):
            raise FinancialMvpContractError(
                f"not-applicable live implementation asserts identity: {strategy_id}"
            )
        requirement = _text(row["product_requirement"], "strategy.product_requirement")
        if requirement not in {"REQUIRED", "OPTIONAL", "EXCLUDED"}:
            raise FinancialMvpContractError(
                f"unknown product requirement for {strategy_id}: {requirement}"
            )
        readiness = _text(row["readiness"], "strategy.readiness")
        if readiness not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(
                f"unknown strategy readiness for {strategy_id}: {readiness}"
            )
        blockers = _blockers(row["blockers"], "strategy.blockers")
        if readiness == "REQUIRED_NOT_READY" and not blockers:
            raise FinancialMvpContractError(
                f"required-not-ready strategy lacks blocker: {strategy_id}"
            )
        if readiness == "READY" and blockers:
            raise FinancialMvpContractError(
                f"ready strategy retains blockers: {strategy_id}"
            )
        _text(row["readiness_reason"], "strategy.readiness_reason")
        interval = _object(
            row["effective_product_interval"], "effective_product_interval"
        )
        _strict_keys(
            interval, {"effective_end", "effective_start"}, "effective_product_interval"
        )
        effective_start = _date(
            interval["effective_start"], "effective_product_interval.effective_start"
        )
        effective_end = _optional_date(
            interval["effective_end"], "effective_product_interval.effective_end"
        )
        if effective_end is not None and effective_end < effective_start:
            raise FinancialMvpContractError(
                f"strategy product interval is reversed: {strategy_id}"
            )
        version = _object(row["strategy_version"], "strategy.strategy_version")
        _strict_keys(version, {"status", "version_ref"}, "strategy_version")
        if version["status"] not in {"EVIDENCED", "NOT_APPLICABLE", "UNRESOLVED"}:
            raise FinancialMvpContractError(
                f"strategy version status differs: {strategy_id}"
            )
        version_ref = _optional_text(
            version["version_ref"], "strategy_version.version_ref"
        )
        if version["status"] == "EVIDENCED" and version_ref is None:
            raise FinancialMvpContractError(
                f"evidenced strategy version lacks identity: {strategy_id}"
            )
        if version["status"] == "UNRESOLVED" and version_ref is not None:
            raise FinancialMvpContractError(
                f"unresolved strategy version asserts identity: {strategy_id}"
            )
        if version["status"] == "NOT_APPLICABLE" and version_ref is None:
            raise FinancialMvpContractError(
                f"not-applicable strategy version lacks governance identity: {strategy_id}"
            )
        membership = _object(row["funded_membership"], "strategy.funded_membership")
        _strict_keys(
            membership,
            {"authority", "effective_end", "effective_start", "status"},
            "funded_membership",
        )
        if membership["status"] not in {"EVIDENCED", "REQUIRED_NOT_READY", "EXCLUDED"}:
            raise FinancialMvpContractError(
                f"funded membership status differs: {strategy_id}"
            )
        _text(membership["authority"], "funded_membership.authority")
        start = _optional_date(
            membership["effective_start"], "funded_membership.effective_start"
        )
        end = _optional_date(
            membership["effective_end"], "funded_membership.effective_end"
        )
        if start is not None and end is not None and end < start:
            raise FinancialMvpContractError(
                f"funded membership interval is reversed: {strategy_id}"
            )
        if membership["status"] == "EVIDENCED" and start is None:
            raise FinancialMvpContractError(
                f"evidenced membership lacks a funding start: {strategy_id}"
            )
        if requirement == "REQUIRED":
            if lifecycle != "ACTIVE":
                raise FinancialMvpContractError(
                    f"required strategy is not active: {strategy_id}"
                )
            if live_status != "EVIDENCED" and readiness != "REQUIRED_NOT_READY":
                raise FinancialMvpContractError(
                    f"active strategy lacks evidenced live implementation: {strategy_id}"
                )
            if membership["status"] != "EVIDENCED":
                raise FinancialMvpContractError(
                    f"active strategy lacks evidenced membership: {strategy_id}"
                )
            if version["status"] != "EVIDENCED" and readiness != "REQUIRED_NOT_READY":
                raise FinancialMvpContractError(
                    f"strategy version/membership readiness differs: {strategy_id}"
                )
        elif requirement == "EXCLUDED":
            if lifecycle == "ACTIVE" or readiness != "READY" or blockers:
                raise FinancialMvpContractError(
                    f"excluded strategy lifecycle/readiness differs: {strategy_id}"
                )
            if version["status"] != "NOT_APPLICABLE":
                raise FinancialMvpContractError(
                    f"excluded strategy design version is not not-applicable: {strategy_id}"
                )
            if membership["status"] != "EXCLUDED":
                raise FinancialMvpContractError(
                    f"excluded strategy asserts funded membership: {strategy_id}"
                )
        dispositions = _object(row["outcome_dispositions"], "outcome_dispositions")
        if set(dispositions) != set(OUTCOME_IDS):
            raise FinancialMvpContractError(
                f"strategy outcome dispositions are incomplete: {strategy_id}"
            )
        for outcome_id, disposition in dispositions.items():
            if disposition not in {"REQUIRED", "EXCLUDED_BY_ACCEPTED_POLICY"}:
                raise FinancialMvpContractError(
                    f"invalid {outcome_id} disposition for {strategy_id}"
                )
        if requirement == "EXCLUDED" and set(dispositions.values()) != {
            "EXCLUDED_BY_ACCEPTED_POLICY"
        }:
            raise FinancialMvpContractError(
                f"excluded strategy retains a required outcome: {strategy_id}"
            )


def census_digest(census: Mapping[str, Any]) -> str:
    """Return the deterministic identity of the accepted redacted census."""

    return hashlib.sha256(canonical_json_bytes(dict(census))).hexdigest()


def census_owner_decision_projection(
    census: Mapping[str, Any], strategies: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Derive the exact owner-approved #302 decision bytes from local authority.

    The owner comment is not merely a detached digest receipt.  This projection
    makes any coherent perimeter, membership, transition, continuity, or
    unsupported-version rewrite change the decision payload identity.
    """

    strategy_by_id = {
        _text(row.get("strategy_id"), "census owner strategy_id"): row
        for row in strategies
    }
    lifecycle_dispositions = {
        "DEPRECATED_HISTORY_ONLY": "DEPRECATED_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
        "IMMATERIAL_EXCLUDED": "IMMATERIAL_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
    }
    perimeter: list[dict[str, str]] = []
    for strategy_id, row in sorted(strategy_by_id.items()):
        if row.get("product_requirement") != "EXCLUDED":
            continue
        lifecycle = _text(
            row.get("lifecycle_status"), f"census owner lifecycle {strategy_id}"
        )
        disposition = lifecycle_dispositions.get(lifecycle)
        if disposition is None:
            raise FinancialMvpContractError(
                f"census owner perimeter lifecycle differs: {strategy_id}"
            )
        membership = _object(
            row.get("funded_membership"),
            f"census owner funded membership {strategy_id}",
        )
        dispositions = _object(
            row.get("outcome_dispositions"),
            f"census owner outcome dispositions {strategy_id}",
        )
        if membership.get("status") != "EXCLUDED" or set(dispositions.values()) != {
            "EXCLUDED_BY_ACCEPTED_POLICY"
        }:
            raise FinancialMvpContractError(
                f"census owner perimeter is not propagated: {strategy_id}"
            )
        perimeter.append({"strategy_id": strategy_id, "disposition": disposition})

    memberships: list[dict[str, object]] = []
    for strategy_id in _CENSUS_OWNER_MEMBERSHIP_ORDER:
        membership_strategy = strategy_by_id.get(strategy_id)
        if membership_strategy is None:
            raise FinancialMvpContractError(
                f"census owner membership strategy is absent: {strategy_id}"
            )
        membership = _object(
            membership_strategy.get("funded_membership"),
            f"census owner funded membership {strategy_id}",
        )
        if (
            membership_strategy.get("product_requirement") != "REQUIRED"
            or membership_strategy.get("lifecycle_status") != "ACTIVE"
            or membership.get("status") != "EVIDENCED"
        ):
            raise FinancialMvpContractError(
                f"census owner membership is not propagated: {strategy_id}"
            )
        membership_end = _optional_date(
            membership.get("effective_end"),
            f"census owner membership end {strategy_id}",
        )
        memberships.append(
            {
                "strategy_id": strategy_id,
                "effective_start": _date(
                    membership.get("effective_start"),
                    f"census owner membership start {strategy_id}",
                ).isoformat(),
                "effective_end": (
                    membership_end.isoformat() if membership_end is not None else None
                ),
            }
        )

    transition_groups: dict[str, list[dict[str, Any]]] = {}
    tt_boundaries: list[date] = []
    for sleeve in _objects(census.get("account_sleeves"), "census account_sleeves"):
        for assignment in _objects(
            sleeve.get("assignments"), "census sleeve assignments"
        ):
            strategy_id = _text(
                assignment.get("strategy_id"), "census owner assignment strategy"
            )
            start = _date(
                assignment.get("effective_start"), "census owner assignment start"
            )
            end = _optional_date(
                assignment.get("effective_end"), "census owner assignment end"
            )
            if strategy_id == "tt-portfolio":
                tt_boundaries.append(start)
                if end is not None:
                    tt_boundaries.append(end)
            transition_id = _optional_text(
                assignment.get("transition_group_id"),
                "census owner transition group",
            )
            if transition_id is not None:
                transition_groups.setdefault(transition_id, []).append(assignment)

    transition_signatures: set[tuple[str, str, str, str]] = set()
    for rows in transition_groups.values():
        if len(rows) != 2:
            raise FinancialMvpContractError(
                "census owner transition cardinality differs"
            )
        ordered = sorted(rows, key=lambda row: str(row["effective_start"]))
        predecessor, successor = ordered
        predecessor_end = _optional_date(
            predecessor.get("effective_end"), "census owner predecessor end"
        )
        successor_start = _date(
            successor.get("effective_start"), "census owner successor start"
        )
        if predecessor_end is None:
            raise FinancialMvpContractError(
                "census owner transition predecessor is open"
            )
        transition_signatures.add(
            (
                _text(
                    predecessor.get("strategy_id"),
                    "census owner predecessor strategy",
                ),
                predecessor_end.isoformat(),
                _text(
                    successor.get("strategy_id"),
                    "census owner successor strategy",
                ),
                successor_start.isoformat(),
            )
        )
    transitions: list[dict[str, str]] = []
    if transition_groups:
        if len(transition_signatures) != 1:
            raise FinancialMvpContractError("census owner transitions disagree")
        predecessor_id, predecessor_end_text, successor_id, successor_start_text = next(
            iter(transition_signatures)
        )
        scope = (
            "TWO_REDACTED_SLEEVES"
            if len(transition_groups) == 2
            else f"{len(transition_groups)}_REDACTED_SLEEVES"
        )
        transitions.append(
            {
                "from_strategy_id": predecessor_id,
                "from_effective_end": predecessor_end_text,
                "to_strategy_id": successor_id,
                "to_effective_start": successor_start_text,
                "scope": scope,
            }
        )

    continuity_start, continuity_end = _CENSUS_TT_CONTINUITY_INTERVAL
    if any(
        continuity_start <= boundary <= continuity_end for boundary in tt_boundaries
    ):
        raise FinancialMvpContractError("TT census invents a July exit or re-entry")

    return {
        "decision_id": ("DATA-CENSUS-001-OWNER-PERIMETER-AND-MEMBERSHIP-2026-08-19"),
        "authority_issue": census.get("authority_issue"),
        "approved_on": census.get("decision_approved_on"),
        "perimeter": perimeter,
        "memberships": memberships,
        "transitions": transitions,
        "continuity": [
            {
                "strategy_id": "tt-portfolio",
                "rule": "CONTINUOUS_MEMBERSHIP",
                "disconfirming_interval": (
                    f"{continuity_start.isoformat()}/{continuity_end.isoformat()}"
                ),
                "disposition": (
                    "ZERO_OR_MISSING_COMPONENT_VALUE_DOES_NOT_CREATE_EXIT_OR_REENTRY"
                ),
            }
        ],
        "explicit_non_decisions": list(_CENSUS_OWNER_NON_DECISIONS),
    }


def _validate_current_census(
    census: Mapping[str, Any],
    strategies: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
) -> None:
    _strict_keys(
        census,
        {
            "account_sleeves",
            "as_of",
            "authority_issue",
            "census_id",
            "decision_approved_on",
            "decision_evidence_url",
            "decision_payload_sha256",
            "evidence_receipts",
            "private_crosswalk_rule",
            "provider_lanes",
        },
        "current_census",
    )
    if census["census_id"] != "PORTFOLIO_CURRENT_CENSUS_V1":
        raise FinancialMvpContractError("current census identity differs")
    if census["authority_issue"] != 302:
        raise FinancialMvpContractError("current census authority differs")
    _date(census["as_of"], "current_census.as_of")
    _date(census["decision_approved_on"], "current_census.decision_approved_on")
    evidence_url = _text(
        census["decision_evidence_url"], "current_census.decision_evidence_url"
    )
    if evidence_url != (
        "https://github.com/dachent/quant_engine/issues/302#issuecomment-5350132900"
    ):
        raise FinancialMvpContractError("current census decision evidence differs")
    _hash(
        census["decision_payload_sha256"],
        "current_census.decision_payload_sha256",
    )
    if census["private_crosswalk_rule"] != (
        "ONLY_STABLE_REDACTED_ALIASES_ARE_COMMITTED"
    ):
        raise FinancialMvpContractError("current census private crosswalk rule differs")

    receipts = _objects(census["evidence_receipts"], "census evidence_receipts")
    expected_receipt_roles = {
        "BASELINE_CATALOG",
        "BASELINE_CURRENT_POINTER",
        "BASELINE_GENERATION_MANIFEST",
        "PROVIDER_LEDGER_CATALOG",
        "PROVIDER_LEDGER_CURRENT_POINTER",
        "PROVIDER_LEDGER_GENERATION_MANIFEST",
    }
    receipt_roles: set[str] = set()
    for receipt in receipts:
        _strict_keys(
            receipt,
            {
                "artifact_selector",
                "generation_id",
                "manifest_artifact_id",
                "role",
                "root",
                "sha256",
            },
            "census evidence receipt",
        )
        role = _text(receipt["role"], "census evidence receipt role")
        if role in receipt_roles:
            raise FinancialMvpContractError(f"duplicate census evidence role: {role}")
        receipt_roles.add(role)
        if receipt["root"] not in {"BASELINE", "PROVIDER_LEDGER"}:
            raise FinancialMvpContractError(f"census evidence root differs: {role}")
        _hash(receipt["generation_id"], f"census evidence generation {role}")
        _no_private_locator(
            receipt["artifact_selector"], f"census evidence selector {role}"
        )
        _optional_text(
            receipt["manifest_artifact_id"],
            f"census evidence manifest artifact {role}",
        )
        _hash(receipt["sha256"], f"census evidence sha256 {role}")
    if receipt_roles != expected_receipt_roles:
        raise FinancialMvpContractError(
            "census evidence roles differ: "
            f"missing={sorted(expected_receipt_roles - receipt_roles)} "
            f"extra={sorted(receipt_roles - expected_receipt_roles)}"
        )

    source_by_id = {
        _text(row["source_id"], "source_segment.source_id"): row for row in sources
    }
    strategy_by_id = {
        _text(row["strategy_id"], "strategy.strategy_id"): row for row in strategies
    }
    lanes = _objects(census["provider_lanes"], "census provider_lanes")
    lane_by_id: dict[str, Mapping[str, Any]] = {}
    declared_lane_sleeves: set[str] = set()
    expected_provider_ids = {
        "ALPACA",
        "COMPOSER",
        "FIDELITY",
        "IBKR",
        "INTERNAL",
        "JOHN_HANCOCK",
        "MATRIXPORT",
        "PORTFOLIO123",
        "QUANTBASE",
        "SCHWAB",
    }
    for lane in lanes:
        _strict_keys(
            lane,
            {
                "account_sleeve_ids",
                "generation",
                "lane_role",
                "lane_status",
                "provider_id",
                "source_segment_id",
            },
            "census provider lane",
        )
        provider_id = _text(lane["provider_id"], "census provider_id")
        if provider_id in lane_by_id:
            raise FinancialMvpContractError(f"duplicate census provider: {provider_id}")
        lane_by_id[provider_id] = lane
        if lane["lane_role"] not in {
            "ACCOUNT_PROVIDER",
            "INTERNAL",
            "SUPPORTING_PROVIDER",
        }:
            raise FinancialMvpContractError(
                f"census provider role differs: {provider_id}"
            )
        if lane["lane_status"] not in {
            "ACTIVE_CURRENT_MEMBERS",
            "EXCLUDED_IMMATERIAL",
            "HISTORICAL_ONLY",
            "INTERNAL_RESERVE",
            "NO_CURRENT_CENSUS_MEMBER",
            "RESERVE_ONLY",
        }:
            raise FinancialMvpContractError(
                f"census provider status differs: {provider_id}"
            )
        sleeve_ids = _strings(
            lane["account_sleeve_ids"], f"census provider sleeves {provider_id}"
        )
        overlap = declared_lane_sleeves & set(sleeve_ids)
        if overlap:
            raise FinancialMvpContractError(
                f"census sleeve belongs to multiple providers: {sorted(overlap)}"
            )
        declared_lane_sleeves.update(sleeve_ids)
        source_id = _optional_text(
            lane["source_segment_id"], f"census provider source {provider_id}"
        )
        generation = lane["generation"]
        if source_id is None:
            if generation is not None:
                raise FinancialMvpContractError(
                    f"census provider without source asserts generation: {provider_id}"
                )
            continue
        if source_id not in source_by_id:
            raise FinancialMvpContractError(
                f"census provider source is unknown: {provider_id}:{source_id}"
            )
        generated = _object(generation, f"census provider generation {provider_id}")
        _strict_keys(
            generated,
            {"as_of", "generation_id", "manifest_sha256", "pointer_sha256"},
            "census provider generation",
        )
        generation_id = _hash(
            generated["generation_id"], f"census provider generation {provider_id}"
        )
        manifest_sha = _hash(
            generated["manifest_sha256"], f"census provider manifest {provider_id}"
        )
        _hash(generated["pointer_sha256"], f"census provider pointer {provider_id}")
        generated_as_of = _date(
            generated["as_of"], f"census provider as_of {provider_id}"
        ).isoformat()
        source_generation = _object(
            source_by_id[source_id]["current_generation"],
            f"source current_generation {source_id}",
        )
        if (
            source_generation.get("generation_id") != generation_id
            or source_generation.get("sha256") != manifest_sha
            or source_generation.get("as_of") != generated_as_of
        ):
            raise FinancialMvpContractError(
                f"census provider/source generation differs: {provider_id}"
            )
    if set(lane_by_id) != expected_provider_ids:
        raise FinancialMvpContractError(
            "census provider set differs: "
            f"missing={sorted(expected_provider_ids - set(lane_by_id))} "
            f"extra={sorted(set(lane_by_id) - expected_provider_ids)}"
        )
    lane_source_ids = {
        source_id
        for lane in lane_by_id.values()
        if (
            source_id := _optional_text(
                lane["source_segment_id"], "census provider source"
            )
        )
        is not None
    }

    sleeves = _objects(census["account_sleeves"], "census account_sleeves")
    if len(sleeves) != 19:
        raise FinancialMvpContractError(
            "current census must contain 19 redacted sleeves"
        )
    sleeve_by_id: dict[str, Mapping[str, Any]] = {}
    transition_groups: dict[str, list[tuple[date, date | None, str, str]]] = {}
    funded_starts: dict[str, list[date]] = {}
    current_counts: dict[str, int] = {}
    for sleeve in sleeves:
        _strict_keys(
            sleeve,
            {
                "assignments",
                "current_strategy_id",
                "provider_id",
                "role",
                "sleeve_alias",
                "source_segment_ids",
            },
            "census account sleeve",
        )
        sleeve_id = _text(sleeve["sleeve_alias"], "census sleeve_alias")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", sleeve_id):
            raise FinancialMvpContractError(f"census sleeve alias differs: {sleeve_id}")
        if sleeve_id in sleeve_by_id:
            raise FinancialMvpContractError(f"duplicate census sleeve: {sleeve_id}")
        sleeve_by_id[sleeve_id] = sleeve
        provider_id = _text(sleeve["provider_id"], "census sleeve provider")
        if provider_id not in lane_by_id:
            raise FinancialMvpContractError(
                f"census sleeve provider is unknown: {sleeve_id}"
            )
        expected_provider_prefix = provider_id.lower().replace("_", "-") + "-"
        if not sleeve_id.startswith(expected_provider_prefix):
            raise FinancialMvpContractError(
                f"census sleeve provider namespace differs: {sleeve_id}"
            )
        provider_sleeves = set(
            _strings(
                lane_by_id[provider_id]["account_sleeve_ids"],
                f"census provider sleeves {provider_id}",
            )
        )
        if sleeve_id not in provider_sleeves:
            raise FinancialMvpContractError(
                f"census sleeve differs from declared provider lane: {sleeve_id}"
            )
        source_ids = _strings(
            sleeve["source_segment_ids"], f"census sleeve sources {sleeve_id}"
        )
        lane_source_id = _optional_text(
            lane_by_id[provider_id]["source_segment_id"],
            f"census provider source {provider_id}",
        )
        if lane_source_id is not None and lane_source_id not in source_ids:
            raise FinancialMvpContractError(
                f"census sleeve lacks its provider source: {sleeve_id}"
            )
        foreign_lane_sources = (set(source_ids) & lane_source_ids) - {lane_source_id}
        if foreign_lane_sources:
            raise FinancialMvpContractError(
                "census sleeve includes a foreign provider source: "
                f"{sleeve_id}:{sorted(foreign_lane_sources)}"
            )
        unknown_sources = set(source_ids) - set(source_by_id)
        if unknown_sources:
            raise FinancialMvpContractError(
                f"census sleeve has unknown sources: {sleeve_id}:{sorted(unknown_sources)}"
            )
        current_strategy = _optional_text(
            sleeve["current_strategy_id"], f"census current strategy {sleeve_id}"
        )
        role = _text(sleeve["role"], f"census sleeve role {sleeve_id}")
        if role not in {
            "ACTIVE_STRATEGY",
            "EXCLUDED_IMMATERIAL",
            "HISTORICAL_ONLY",
            "RESERVE",
            "UNASSIGNED_HISTORY",
        }:
            raise FinancialMvpContractError(f"census sleeve role differs: {sleeve_id}")
        assignments = _objects(
            sleeve["assignments"], f"census sleeve assignments {sleeve_id}"
        )
        parsed: list[tuple[date, date | None, str, str]] = []
        for assignment in assignments:
            _strict_keys(
                assignment,
                {
                    "authority",
                    "effective_end",
                    "effective_start",
                    "status",
                    "strategy_id",
                    "transition_group_id",
                },
                "census assignment",
            )
            strategy_id = _text(
                assignment["strategy_id"], f"census assignment strategy {sleeve_id}"
            )
            if strategy_id not in strategy_by_id:
                raise FinancialMvpContractError(
                    f"census assignment strategy is unknown: {sleeve_id}:{strategy_id}"
                )
            start = _date(
                assignment["effective_start"], f"census assignment start {sleeve_id}"
            )
            end = _optional_date(
                assignment["effective_end"], f"census assignment end {sleeve_id}"
            )
            if end is not None and end < start:
                raise FinancialMvpContractError(
                    f"census assignment interval is reversed: {sleeve_id}"
                )
            status = _text(
                assignment["status"], f"census assignment status {sleeve_id}"
            )
            if status not in {
                "ASSIGNMENT_ONLY",
                "DEPRECATED_HISTORY_ONLY",
                "EXCLUDED_IMMATERIAL",
                "FUNDED_MEMBERSHIP",
            }:
                raise FinancialMvpContractError(
                    f"census assignment status differs: {sleeve_id}"
                )
            _text(assignment["authority"], f"census assignment authority {sleeve_id}")
            transition_id = _optional_text(
                assignment["transition_group_id"],
                f"census transition group {sleeve_id}",
            )
            parsed.append((start, end, strategy_id, status))
            if transition_id is not None:
                transition_groups.setdefault(transition_id, []).append(
                    (start, end, strategy_id, sleeve_id)
                )
            if status == "FUNDED_MEMBERSHIP":
                funded_starts.setdefault(strategy_id, []).append(start)
        for left, right in zip(sorted(parsed), sorted(parsed)[1:]):
            left_end = left[1]
            if left_end is None or right[0] <= left_end:
                raise FinancialMvpContractError(
                    f"census assignments overlap or have an open predecessor: {sleeve_id}"
                )
        if current_strategy is None:
            if role == "ACTIVE_STRATEGY":
                raise FinancialMvpContractError(
                    f"active census sleeve lacks current strategy: {sleeve_id}"
                )
        else:
            if role != "ACTIVE_STRATEGY":
                raise FinancialMvpContractError(
                    f"non-active census sleeve asserts current strategy: {sleeve_id}"
                )
            if strategy_by_id[current_strategy]["product_requirement"] != "REQUIRED":
                raise FinancialMvpContractError(
                    f"census current strategy is excluded: {sleeve_id}"
                )
            if not any(
                strategy_id == current_strategy
                and status == "FUNDED_MEMBERSHIP"
                and end is None
                for _start, end, strategy_id, status in parsed
            ):
                raise FinancialMvpContractError(
                    f"census current strategy lacks open funded assignment: {sleeve_id}"
                )
            current_counts[current_strategy] = (
                current_counts.get(current_strategy, 0) + 1
            )
    if set(sleeve_by_id) != declared_lane_sleeves:
        raise FinancialMvpContractError(
            "census provider/sleeve partition differs: "
            f"missing={sorted(declared_lane_sleeves - set(sleeve_by_id))} "
            f"extra={sorted(set(sleeve_by_id) - declared_lane_sleeves)}"
        )
    for transition_id, rows in transition_groups.items():
        if len(rows) != 2 or len({row[3] for row in rows}) != 1:
            raise FinancialMvpContractError(
                f"census transition group does not bind one sleeve: {transition_id}"
            )
        ordered = sorted(rows)
        if (
            ordered[0][1] is None
            or (ordered[1][0] - ordered[0][1]).days != 1
            or ordered[0][2] == ordered[1][2]
        ):
            raise FinancialMvpContractError(
                f"census transition is not adjacent: {transition_id}"
            )
    required_strategies = {
        strategy_id
        for strategy_id, row in strategy_by_id.items()
        if row["product_requirement"] == "REQUIRED"
    }
    if set(current_counts) != required_strategies:
        raise FinancialMvpContractError(
            "current census active strategy set differs: "
            f"missing={sorted(required_strategies - set(current_counts))} "
            f"extra={sorted(set(current_counts) - required_strategies)}"
        )
    for strategy_id in required_strategies:
        membership = _object(
            strategy_by_id[strategy_id]["funded_membership"],
            f"strategy funded_membership {strategy_id}",
        )
        accepted_start = _date(
            membership["effective_start"], f"strategy funded start {strategy_id}"
        )
        if min(funded_starts.get(strategy_id, []), default=None) != accepted_start:
            raise FinancialMvpContractError(
                f"census funded start differs from strategy authority: {strategy_id}"
            )
    tt_rows = [
        row
        for sleeve in sleeves
        for row in _objects(sleeve["assignments"], "census assignments")
        if row["strategy_id"] == "tt-portfolio" and row["status"] == "FUNDED_MEMBERSHIP"
    ]
    if any(
        row["effective_end"] is not None
        and str(row["effective_end"]).startswith("2026-07")
        for row in tt_rows
    ):
        raise FinancialMvpContractError("TT census invents a July exit or re-entry")
    owner_projection = census_owner_decision_projection(census, strategies)
    if hashlib.sha256(canonical_json_bytes(owner_projection)).hexdigest() != census.get(
        "decision_payload_sha256"
    ):
        raise FinancialMvpContractError(
            "census owner decision projection differs from approved payload"
        )


def _validate_history_corrections(
    policy: Mapping[str, Any],
    strategies: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    registry_ids: set[str],
) -> None:
    _strict_keys(
        policy,
        {
            "corrections",
            "materialized_segment_identities",
            "owner_issue",
            "policy_id",
            "policy_version",
            "splice_policy",
        },
        "history_corrections",
    )
    if policy["policy_id"] != "PORT-HISTORY-TRUTH-1":
        raise FinancialMvpContractError("history correction policy identity differs")
    if policy["policy_version"] != 1 or policy["owner_issue"] != 319:
        raise FinancialMvpContractError(
            "history correction policy owner/version differs"
        )
    if policy["splice_policy"] != HISTORY_SPLICE_POLICY:
        raise FinancialMvpContractError("history splice policy is unapproved")

    source_by_id = {str(row.get("source_id")): row for row in sources}
    strategy_by_id = {str(row.get("strategy_id")): row for row in strategies}
    segment_rows = _objects(
        policy["materialized_segment_identities"],
        "history_corrections.materialized_segment_identities",
    )
    segment_ids: list[str] = []
    for row in segment_rows:
        _strict_keys(
            row,
            {
                "calendar",
                "canonicalization",
                "evidence_layer",
                "frequency",
                "mode_admission",
                "observed_end",
                "observed_start",
                "return_basis",
                "return_observations",
                "return_type",
                "segment_id",
                "sha256",
                "source_generation_id",
                "source_generation_sha256",
                "source_id",
                "strategy_id",
                "strategy_version",
            },
            "materialized history segment identity",
        )
        segment_id = _text(row["segment_id"], "history segment_id")
        strategy_id = _text(row["strategy_id"], "history segment strategy_id")
        source_id = _text(row["source_id"], "history segment source_id")
        source = source_by_id.get(source_id)
        if strategy_id not in strategy_by_id or source is None:
            raise FinancialMvpContractError(
                f"materialized history segment has unknown authority: {segment_id}"
            )
        if not segment_id.startswith(f"{strategy_id}:"):
            raise FinancialMvpContractError(
                f"materialized history segment identity differs: {segment_id}"
            )
        subjects = _object(source["subjects"], "history segment source subjects")
        subject_ids = _strings(
            subjects["strategy_ids"], "history segment source strategy_ids"
        )
        if strategy_id not in subject_ids:
            raise FinancialMvpContractError(
                f"materialized history segment is outside source subjects: {segment_id}"
            )
        if row["canonicalization"] != "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1":
            raise FinancialMvpContractError(
                f"materialized history segment canonicalization differs: {segment_id}"
            )
        segment_observed_start = _date(
            row["observed_start"], "history segment observed_start"
        )
        segment_observed_end = _date(
            row["observed_end"], "history segment observed_end"
        )
        if segment_observed_end < segment_observed_start:
            raise FinancialMvpContractError(
                f"materialized history segment interval is reversed: {segment_id}"
            )
        observations = row["return_observations"]
        if (
            not isinstance(observations, int)
            or isinstance(observations, bool)
            or observations < 1
        ):
            raise FinancialMvpContractError(
                f"materialized history segment observation count differs: {segment_id}"
            )
        _hash(row["sha256"], "history segment sha256")
        source_generation_id = _hash(
            row["source_generation_id"], "history segment source_generation_id"
        )
        source_generation_sha = _hash(
            row["source_generation_sha256"],
            "history segment source_generation_sha256",
        )
        current_generation = _object(
            source["current_generation"], "history segment source current_generation"
        )
        if (
            current_generation.get("generation_id"),
            current_generation.get("sha256"),
        ) != (source_generation_id, source_generation_sha):
            raise FinancialMvpContractError(
                f"materialized history segment source receipt differs: {segment_id}"
            )
        temporal = _object(source["temporal"], "history segment source temporal")
        if (row["calendar"], row["frequency"]) != (
            temporal["calendar"],
            temporal["cadence"],
        ):
            raise FinancialMvpContractError(
                f"materialized history segment temporal authority differs: {segment_id}"
            )
        version = _object(
            source["strategy_version"], "history segment source strategy_version"
        )
        version_refs = _object(
            version["version_refs"], "history segment source version_refs"
        )
        expected_version = version_refs.get(strategy_id) or "UNRESOLVED"
        if row["strategy_version"] != expected_version:
            raise FinancialMvpContractError(
                f"materialized history segment version differs: {segment_id}"
            )
        layer = _text(row["evidence_layer"], "history segment evidence_layer")
        admission = _text(row["mode_admission"], "history segment mode_admission")
        if layer not in {
            "ACCOUNT_LIVE",
            "BACKTEST",
            "PROVIDER_LIVE",
        } or admission not in {
            "EXCLUDE_COMPARISON_ONLY",
            "INCLUDE_SELECTED_LIVE",
            "INCLUDE_VERSION_COMPATIBLE_PREHISTORY",
            "REQUIRED_NOT_READY_OVERLAP",
        }:
            raise FinancialMvpContractError(
                f"materialized history segment admission differs: {segment_id}"
            )
        if (layer == "ACCOUNT_LIVE") != (admission == "INCLUDE_SELECTED_LIVE"):
            raise FinancialMvpContractError(
                f"materialized history segment selected-live authority differs: {segment_id}"
            )
        if layer == "PROVIDER_LIVE" and admission != "EXCLUDE_COMPARISON_ONLY":
            raise FinancialMvpContractError(
                f"materialized provider history entered an admitted mode: {segment_id}"
            )
        _text(row["return_basis"], "history segment return_basis")
        _text(row["return_type"], "history segment return_type")
        segment_ids.append(segment_id)
    if segment_ids != sorted(segment_ids) or len(segment_ids) != len(set(segment_ids)):
        raise FinancialMvpContractError(
            "materialized history segment identities are not canonical"
        )
    correction_ids: set[str] = set()
    observed_layers: dict[str, set[str]] = {
        strategy_id: set() for strategy_id in HISTORY_CORRECTION_LAYERS
    }
    account_live_starts: dict[str, date] = {}
    funding_dates: dict[str, date] = {}
    rows = _objects(policy["corrections"], "history_corrections.corrections")
    for row in rows:
        expected_correction_keys = {
            "blockers",
            "capture",
            "correction_id",
            "disposition_reason",
            "evidence_layer",
            "funding_event",
            "history_existence",
            "interval",
            "mode_admission",
            "readiness",
            "source_id",
            "source_receipts",
            "strategy_id",
            "strategy_version",
        }
        if row.get("correction_id") == "echotrade-event-only-account-live-rejected":
            expected_correction_keys.add("rejected_evidence_identity")
        if row.get("correction_id") == "p123-eu-provider-live":
            expected_correction_keys.add("derived_evidence_identity")
        if row.get("correction_id") == "p123-ai-backtest-candidate":
            expected_correction_keys.add("derived_backtest_identity")
        _strict_keys(
            row,
            expected_correction_keys,
            "history correction",
        )
        correction_id = _text(row["correction_id"], "history correction id")
        if correction_id in correction_ids:
            raise FinancialMvpContractError(
                f"duplicate history correction: {correction_id}"
            )
        correction_ids.add(correction_id)
        strategy_id = _text(row["strategy_id"], "history correction strategy_id")
        if strategy_id not in registry_ids or strategy_id not in observed_layers:
            raise FinancialMvpContractError(
                f"history correction has unknown/unaffected strategy: {strategy_id}"
            )
        layer = _text(row["evidence_layer"], "history correction evidence_layer")
        if layer not in HISTORY_CORRECTION_LAYERS[strategy_id]:
            raise FinancialMvpContractError(
                f"history correction has unexpected layer: {strategy_id}/{layer}"
            )
        if layer in observed_layers[strategy_id]:
            raise FinancialMvpContractError(
                f"overlapping history correction layer: {strategy_id}/{layer}"
            )
        observed_layers[strategy_id].add(layer)
        source_id = _text(row["source_id"], "history correction source_id")
        source = source_by_id.get(source_id)
        if source is None:
            raise FinancialMvpContractError(
                f"history correction references unknown source: {source_id}"
            )

        existence = _object(row["history_existence"], "history existence")
        _strict_keys(existence, {"authority", "status"}, "history existence")
        existence_status = _text(existence["status"], "history existence status")
        if existence_status not in {"EXISTS", "DOES_NOT_EXIST", "UNRESOLVED"}:
            raise FinancialMvpContractError("history existence status differs")
        _text(existence["authority"], "history existence authority")

        capture = _object(row["capture"], "history capture")
        _strict_keys(
            capture,
            {"as_of", "capture_defect", "generation_id", "generation_sha256", "status"},
            "history capture",
        )
        capture_status = _text(capture["status"], "history capture status")
        if capture_status not in {"CAPTURED_GOVERNED", "NOT_CAPTURED", "UNRESOLVED"}:
            raise FinancialMvpContractError("history capture status differs")
        capture_defect = _optional_text(capture["capture_defect"], "capture defect")
        generation_id = _optional_hash(capture["generation_id"], "history generation")
        generation_sha = _optional_hash(
            capture["generation_sha256"], "history generation sha256"
        )
        generation_as_of = _optional_date(capture["as_of"], "history generation as_of")
        has_generation = all(
            value is not None
            for value in (generation_id, generation_sha, generation_as_of)
        )
        if capture_status == "CAPTURED_GOVERNED":
            if existence_status != "EXISTS" or not has_generation:
                raise FinancialMvpContractError(
                    f"history capture conflates existence/generation: {correction_id}"
                )
            if capture_defect is not None:
                raise FinancialMvpContractError(
                    f"captured history retains a capture defect: {correction_id}"
                )
            source_generation = _object(
                source["current_generation"], "history source current_generation"
            )
            if (
                source_generation["generation_id"] != generation_id
                or source_generation["sha256"] != generation_sha
                or source_generation["as_of"] != capture["as_of"]
            ):
                raise FinancialMvpContractError(
                    f"history correction generation differs from source: {correction_id}"
                )
        elif has_generation or any(
            value is not None
            for value in (generation_id, generation_sha, generation_as_of)
        ):
            raise FinancialMvpContractError(
                f"uncaptured history asserts a generation: {correction_id}"
            )
        elif capture_status == "NOT_CAPTURED" and capture_defect is None:
            raise FinancialMvpContractError(
                f"not-captured history lacks a named defect: {correction_id}"
            )

        version = _object(row["strategy_version"], "history strategy_version")
        _strict_keys(
            version,
            {"authority", "compatibility", "status", "version_ref"},
            "history strategy_version",
        )
        version_status = _text(version["status"], "history version status")
        if version_status not in {"EVIDENCED", "NOT_APPLICABLE", "UNRESOLVED"}:
            raise FinancialMvpContractError("history strategy version status differs")
        version_ref = _optional_text(version["version_ref"], "history version_ref")
        compatibility = _text(version["compatibility"], "history compatibility")
        if compatibility not in {
            "ACCOUNT_OWNED",
            "EXACT_MATCH",
            "NOT_APPLICABLE",
            "UNRESOLVED",
        }:
            raise FinancialMvpContractError("history version compatibility differs")
        _text(version["authority"], "history version authority")
        if version_status == "EVIDENCED" and version_ref is None:
            raise FinancialMvpContractError(
                f"evidenced history version lacks identity: {correction_id}"
            )
        if version_status != "EVIDENCED" and version_ref is not None:
            raise FinancialMvpContractError(
                f"unresolved history version asserts identity: {correction_id}"
            )
        if version_status == "EVIDENCED":
            product_version = _object(
                strategy_by_id[strategy_id]["strategy_version"],
                f"history product strategy_version {strategy_id}",
            )
            if (
                product_version.get("status") != "EVIDENCED"
                or product_version.get("version_ref") != version_ref
            ):
                raise FinancialMvpContractError(
                    f"history strategy version differs from product authority: {correction_id}"
                )
        if (
            compatibility in {"ACCOUNT_OWNED", "EXACT_MATCH"}
            and version_status != "EVIDENCED"
        ):
            raise FinancialMvpContractError(
                f"compatible history lacks evidenced version: {correction_id}"
            )

        interval = _object(row["interval"], "history interval")
        _strict_keys(
            interval,
            {
                "assignment_date",
                "effective_end",
                "effective_start",
                "observed_end",
                "observed_start",
            },
            "history interval",
        )
        assignment_date = _optional_date(interval["assignment_date"], "assignment date")
        observed_start = _optional_date(
            interval["observed_start"], "history observed_start"
        )
        observed_end = _optional_date(interval["observed_end"], "history observed_end")
        effective_start = _optional_date(
            interval["effective_start"], "history effective_start"
        )
        effective_end = _optional_date(
            interval["effective_end"], "history effective_end"
        )
        if (
            observed_start is not None
            and observed_end is not None
            and observed_end < observed_start
        ):
            raise FinancialMvpContractError(
                f"history observed interval is reversed: {correction_id}"
            )
        if (
            effective_start is not None
            and effective_end is not None
            and effective_end < effective_start
        ):
            raise FinancialMvpContractError(
                f"history effective interval is reversed: {correction_id}"
            )

        funding = _object(row["funding_event"], "history funding_event")
        _strict_keys(
            funding,
            {"account_live_start", "authority", "event_date", "status"},
            "history funding_event",
        )
        funding_status = _text(funding["status"], "history funding status")
        funding_date = _optional_date(
            funding["event_date"], "history funding event_date"
        )
        account_live_start = _optional_date(
            funding["account_live_start"], "history account_live_start"
        )
        funding_authority = _text(funding["authority"], "history funding authority")
        if layer == "ACCOUNT_LIVE":
            if funding_status == "NOT_APPLICABLE":
                raise FinancialMvpContractError(
                    f"account-live correction lacks funding disposition: {correction_id}"
                )
            if funding_status == "EVIDENCED":
                if funding_date is None or account_live_start is None:
                    raise FinancialMvpContractError(
                        f"evidenced funding lacks dates: {correction_id}"
                    )
                if "ASSIGNMENT" in funding_authority.upper():
                    raise FinancialMvpContractError(
                        f"assignment date cannot be funding authority: {correction_id}"
                    )
                if account_live_start != funding_date:
                    raise FinancialMvpContractError(
                        f"account-live start differs from funding: {correction_id}"
                    )
                if observed_start is None or observed_start < account_live_start:
                    raise FinancialMvpContractError(
                        f"account-live observations precede funding: {correction_id}"
                    )
                account_live_starts[strategy_id] = account_live_start
                funding_dates[strategy_id] = funding_date
        elif funding_status != "NOT_APPLICABLE" or any(
            value is not None for value in (funding_date, account_live_start)
        ):
            raise FinancialMvpContractError(
                f"non-account history asserts funding: {correction_id}"
            )
        if (
            assignment_date is not None
            and funding_status == "EVIDENCED"
            and funding_date is None
        ):
            raise FinancialMvpContractError(
                f"assignment date substituted for funding: {correction_id}"
            )

        modes = _object(row["mode_admission"], "history mode_admission")
        _strict_keys(
            modes, {"LIVE_ONLY", "LIVE_PLUS_BACKTEST"}, "history mode_admission"
        )
        live_only = _text(modes["LIVE_ONLY"], "history LIVE_ONLY admission")
        hybrid = _text(
            modes["LIVE_PLUS_BACKTEST"], "history LIVE_PLUS_BACKTEST admission"
        )
        if layer in {"PROVIDER_LIVE", "PROVIDER_TRACK_RECORD"} and (
            live_only != "EXCLUDE_COMPARISON_ONLY"
            or hybrid != "EXCLUDE_COMPARISON_ONLY"
        ):
            raise FinancialMvpContractError(
                f"provider history was relabeled into an accepted mode: {correction_id}"
            )
        if (
            layer == "ACCOUNT_LIVE"
            and funding_status == "EVIDENCED"
            and (
                live_only != "INCLUDE_SELECTED_LIVE"
                or hybrid != "INCLUDE_SELECTED_LIVE"
            )
        ):
            raise FinancialMvpContractError(
                f"evidenced account-live history is not selected live: {correction_id}"
            )
        if layer == "BACKTEST":
            if live_only != "NOT_APPLICABLE":
                raise FinancialMvpContractError(
                    f"backtest entered LIVE_ONLY: {correction_id}"
                )
            if hybrid == "INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY" and (
                capture_status != "CAPTURED_GOVERNED"
                or version_status != "EVIDENCED"
                or compatibility != "EXACT_MATCH"
            ):
                raise FinancialMvpContractError(
                    f"incompatible backtest entered hybrid mode: {correction_id}"
                )

        receipts = _objects(row["source_receipts"], "history source_receipts")
        if not receipts:
            raise FinancialMvpContractError(
                f"history correction lacks receipts: {correction_id}"
            )
        receipt_keys: set[tuple[str, str]] = set()
        receipt_hashes: set[str] = set()
        for receipt in receipts:
            _strict_keys(
                receipt,
                {
                    "artifact_role",
                    "artifact_selector",
                    "authority_root",
                    "generation_id",
                    "locator_kind",
                    "manifest_artifact_id",
                    "sha256",
                },
                "history source receipt",
            )
            role = _text(receipt["artifact_role"], "history receipt artifact_role")
            digest = _hash(receipt["sha256"], "history receipt sha256")
            _no_private_locator(
                receipt["artifact_selector"], "history receipt artifact_selector"
            )
            receipt_root = _text(
                receipt["authority_root"], "history receipt authority_root"
            )
            if receipt_root not in HISTORY_EVIDENCE_ROOTS:
                raise FinancialMvpContractError(
                    f"history receipt root differs: {correction_id}"
                )
            _hash(receipt["generation_id"], "history receipt generation_id")
            locator = _text(receipt["locator_kind"], "history receipt locator_kind")
            if locator not in HISTORY_RECEIPT_LOCATORS:
                raise FinancialMvpContractError(
                    f"history receipt locator differs: {correction_id}"
                )
            manifest_artifact_id = _optional_text(
                receipt["manifest_artifact_id"], "history manifest artifact id"
            )
            if manifest_artifact_id is not None:
                _no_private_locator(
                    manifest_artifact_id, "history manifest artifact id"
                )
            if (locator == "GENERATION_ARTIFACT") != (manifest_artifact_id is not None):
                raise FinancialMvpContractError(
                    f"history receipt manifest selector differs: {correction_id}"
                )
            if (role, digest) in receipt_keys:
                raise FinancialMvpContractError(
                    f"duplicate history receipt: {correction_id}"
                )
            receipt_keys.add((role, digest))
            receipt_hashes.add(digest)
        if generation_sha is not None and generation_sha not in receipt_hashes:
            raise FinancialMvpContractError(
                f"history generation is not receipt-bound: {correction_id}"
            )
        if correction_id == "echotrade-event-only-account-live-rejected":
            rejected_identity = _object(
                row["rejected_evidence_identity"],
                "EchoTrade rejected evidence identity",
            )
            _strict_keys(
                rejected_identity,
                {
                    "canonicalization",
                    "return_observations",
                    "sha256",
                    "source_receipt_sha256",
                },
                "EchoTrade rejected evidence identity",
            )
            if (
                rejected_identity["canonicalization"]
                != "TARGET_FIELDS_CANONICAL_JSON_V1"
                or not isinstance(rejected_identity["return_observations"], int)
                or rejected_identity["return_observations"] < 1
            ):
                raise FinancialMvpContractError(
                    "EchoTrade rejected evidence identity differs"
                )
            _hash(rejected_identity["sha256"], "EchoTrade rejected evidence sha256")
            receipt_sha = _hash(
                rejected_identity["source_receipt_sha256"],
                "EchoTrade rejected evidence source receipt",
            )
            if receipt_sha not in receipt_hashes or not any(
                receipt.get("artifact_role") == "provider-ledger-catalog"
                and receipt.get("sha256") == receipt_sha
                for receipt in receipts
            ):
                raise FinancialMvpContractError(
                    "EchoTrade rejected evidence is not bound to the provider ledger"
                )
        _text(row["disposition_reason"], "history disposition_reason")
        readiness = _text(row["readiness"], "history readiness")
        blockers = _blockers(row["blockers"], "history blockers")
        if readiness == "READY" and blockers:
            raise FinancialMvpContractError(
                f"ready history correction retains blockers: {correction_id}"
            )
        if readiness == "REQUIRED_NOT_READY" and not blockers:
            raise FinancialMvpContractError(
                f"history correction lacks blocker: {correction_id}"
            )
        if readiness == "READY" and capture_status != "CAPTURED_GOVERNED":
            raise FinancialMvpContractError(
                f"ready history correction is uncaptured: {correction_id}"
            )

    for strategy_id, expected_layers in HISTORY_CORRECTION_LAYERS.items():
        if observed_layers[strategy_id] != set(expected_layers):
            raise FinancialMvpContractError(
                f"history correction layers differ for {strategy_id}"
            )

    for row in rows:
        if row["evidence_layer"] != "BACKTEST":
            continue
        if row["mode_admission"]["LIVE_PLUS_BACKTEST"] != (
            "INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY"
        ):
            continue
        account_start = account_live_starts.get(str(row["strategy_id"]))
        backtest_end = _optional_date(
            row["interval"]["observed_end"], "backtest observed_end"
        )
        if (
            account_start is None
            or backtest_end is None
            or backtest_end >= account_start
        ):
            raise FinancialMvpContractError(
                f"backtest is not strictly before live inception: {row['correction_id']}"
            )

    for strategy_id, funding_date in funding_dates.items():
        strategy = strategy_by_id[strategy_id]
        membership = _object(
            strategy["funded_membership"], "strategy funded_membership"
        )
        if (
            membership["status"] != "EVIDENCED"
            or membership["effective_start"] != funding_date.isoformat()
        ):
            raise FinancialMvpContractError(
                f"history funding does not propagate to strategy membership: {strategy_id}"
            )

    correction_by_id = {str(row["correction_id"]): row for row in rows}
    p123_ai_backtest = correction_by_id["p123-ai-backtest-candidate"]
    p123_ai_identity = _object(
        p123_ai_backtest["derived_backtest_identity"],
        "P123 AI derived backtest identity",
    )
    _strict_keys(
        p123_ai_identity,
        {
            "index_observations",
            "observed_end",
            "observed_start",
            "relationship_type",
            "return_observations",
            "source_labels",
        },
        "P123 AI derived backtest identity",
    )
    index_observations = p123_ai_identity.get("index_observations")
    return_observations = p123_ai_identity.get("return_observations")
    p123_ai_interval = _object(
        p123_ai_backtest["interval"], "P123 AI backtest interval"
    )
    source_labels = p123_ai_identity.get("source_labels")
    if (
        not isinstance(index_observations, int)
        or isinstance(index_observations, bool)
        or index_observations < 2
        or not isinstance(return_observations, int)
        or isinstance(return_observations, bool)
        or return_observations != index_observations - 1
        or (
            p123_ai_identity.get("observed_start"),
            p123_ai_identity.get("observed_end"),
        )
        != (
            p123_ai_interval.get("observed_start"),
            p123_ai_interval.get("observed_end"),
        )
        or p123_ai_identity.get("relationship_type") != "live_sim_twin"
        or not isinstance(source_labels, list)
        or not source_labels
        or source_labels != sorted(set(source_labels))
        or not all(isinstance(item, str) and item.strip() for item in source_labels)
    ):
        raise FinancialMvpContractError("P123 AI derived backtest identity differs")
    p123_ai_receipts = {
        str(receipt["manifest_artifact_id"]): receipt
        for receipt in p123_ai_backtest["source_receipts"]
        if isinstance(receipt, dict) and receipt.get("manifest_artifact_id")
    }
    if (
        p123_ai_backtest["readiness"] != "REQUIRED_NOT_READY"
        or p123_ai_backtest["blockers"] != [302, 303]
        or p123_ai_backtest["strategy_version"]["status"] != "UNRESOLVED"
        or p123_ai_backtest["strategy_version"]["compatibility"] != "UNRESOLVED"
        or p123_ai_backtest["mode_admission"]
        != {"LIVE_ONLY": "NOT_APPLICABLE", "LIVE_PLUS_BACKTEST": "REQUIRED_NOT_READY"}
        or {
            "tables/returns_daily.parquet",
            "tables/strategy_relationships.parquet",
        }
        - set(p123_ai_receipts)
    ):
        raise FinancialMvpContractError(
            "P123 AI backtest bypasses its unresolved version disposition"
        )
    p123_provider = correction_by_id["p123-eu-provider-live"]
    p123_identity = _object(
        p123_provider["derived_evidence_identity"],
        "P123 EU derived evidence identity",
    )
    _strict_keys(
        p123_identity,
        {
            "calendar",
            "cash_added_treatment",
            "equity_overlap_observations",
            "observed_end",
            "observed_start",
            "raw_level_observations",
            "return_observations",
            "transform_id",
        },
        "P123 EU derived evidence identity",
    )
    raw_levels = p123_identity.get("raw_level_observations")
    derived_returns = p123_identity.get("return_observations")
    equity_overlap = p123_identity.get("equity_overlap_observations")
    p123_interval = _object(p123_provider["interval"], "P123 EU provider interval")
    if (
        not isinstance(raw_levels, int)
        or isinstance(raw_levels, bool)
        or raw_levels < 2
        or not isinstance(derived_returns, int)
        or isinstance(derived_returns, bool)
        or derived_returns != raw_levels - 1
        or not isinstance(equity_overlap, int)
        or isinstance(equity_overlap, bool)
        or equity_overlap < 2
        or equity_overlap > raw_levels
        or (
            p123_identity.get("observed_start"),
            p123_identity.get("observed_end"),
        )
        != (p123_interval.get("observed_start"), p123_interval.get("observed_end"))
    ):
        raise FinancialMvpContractError("P123 EU derived evidence identity differs")
    _text(p123_identity["calendar"], "P123 EU derived calendar")
    _text(
        p123_identity["cash_added_treatment"],
        "P123 EU derived Cash Added treatment",
    )
    p123_transform_id = _text(
        p123_identity["transform_id"], "P123 EU derived transform"
    )
    p123_receipts = {
        str(receipt["manifest_artifact_id"]): receipt
        for receipt in p123_provider["source_receipts"]
        if isinstance(receipt, dict) and receipt.get("manifest_artifact_id")
    }
    p123_disposition = _text(
        p123_provider["disposition_reason"], "P123 EU provider disposition"
    )
    if (
        p123_provider["readiness"] != "REQUIRED_NOT_READY"
        or p123_provider["blockers"] != [303]
        or "tables/har_managed_performance.parquet" not in p123_receipts
        or p123_transform_id not in p123_disposition
        or "financial interpretation is not approved" not in p123_disposition
        or "No provider-live return is admitted" not in p123_disposition
    ):
        raise FinancialMvpContractError(
            "P123 EU provider history bypasses its unadmitted transform disposition"
        )
    p123_account = correction_by_id["p123-eu-account-live"]
    p123_funding_authority = _text(
        p123_account["funding_event"]["authority"], "P123 EU funding authority"
    )
    if (
        "INTERNAL_TRANSFER_IN" not in p123_funding_authority
        or "ZERO_AGGREGATE_INVESTED_CAPITAL_EFFECT" not in p123_funding_authority
        or "EXTERNAL_FLOW" in p123_funding_authority
    ):
        raise FinancialMvpContractError(
            "P123 EU membership transfer was misclassified as external capital"
        )
    for strategy_id, strategy in strategy_by_id.items():
        live_identity = _object(
            strategy["live_implementation_identity"],
            f"live implementation identity {strategy_id}",
        )
        if live_identity["status"] == "EVIDENCED":
            account_segments = [
                row
                for row in segment_rows
                if row["strategy_id"] == strategy_id
                and row["evidence_layer"] == "ACCOUNT_LIVE"
                and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
            ]
            if len(account_segments) != 1:
                raise FinancialMvpContractError(
                    f"live implementation lacks one accepted segment: {strategy_id}"
                )
            segment = account_segments[0]
            expected_identity = {
                "status": "EVIDENCED",
                "version_ref": f"account-live-segment:{segment['sha256']}",
                "generation_id": segment["source_generation_id"],
                "receipt_sha256": segment["source_generation_sha256"],
            }
            if live_identity != expected_identity:
                raise FinancialMvpContractError(
                    f"live implementation identity differs from accepted segment: {strategy_id}"
                )
        elif live_identity["status"] == "REJECTED":
            if strategy_id != "echotrade-growth-pivots":
                raise FinancialMvpContractError(
                    f"rejected live implementation strategy differs: {strategy_id}"
                )
            rejected = correction_by_id["echotrade-event-only-account-live-rejected"]
            rejected_evidence = _object(
                rejected["rejected_evidence_identity"],
                "EchoTrade rejected evidence identity",
            )
            expected_identity = {
                "status": "REJECTED",
                "version_ref": f"rejected-event-only:{rejected_evidence['sha256']}",
                "generation_id": rejected["capture"]["generation_id"],
                "receipt_sha256": rejected_evidence["source_receipt_sha256"],
            }
            if live_identity != expected_identity:
                raise FinancialMvpContractError(
                    "EchoTrade live implementation identity differs from rejected evidence"
                )
    volatis_provider = correction_by_id["volatis-provider-live"]
    volatis_account = correction_by_id["volatis-account-live"]
    if (
        volatis_provider["interval"]["observed_start"] != "2024-10-15"
        or volatis_provider["interval"]["effective_start"] != "2024-10-15"
        or volatis_account["funding_event"]["event_date"] != "2024-10-15"
        or volatis_account["funding_event"]["account_live_start"] != "2024-10-15"
        or volatis_account["interval"]["effective_start"] != "2024-10-15"
        or volatis_account["interval"]["observed_start"] != "2024-10-16"
    ):
        raise FinancialMvpContractError(
            "Volatis membership/return boundary differs from valued governed evidence"
        )


def _validate_active_blocked_numeric_identities(
    strategies: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    history: Mapping[str, Any],
) -> None:
    """Permit no numeric identity only behind explicit generic fail-closed guards."""

    materialized = _objects(
        history.get("materialized_segment_identities"),
        "materialized history segment identities",
    )
    corrections = _objects(history.get("corrections"), "history corrections")
    for strategy in strategies:
        strategy_id = _text(
            strategy.get("strategy_id"), "active blocked strategy_id"
        )
        live_identity = _object(
            strategy.get("live_implementation_identity"),
            f"active blocked live identity {strategy_id}",
        )
        if live_identity.get("status") == "EVIDENCED":
            continue
        if not (
            strategy.get("lifecycle_status") == "ACTIVE"
            and strategy.get("product_requirement") == "REQUIRED"
        ):
            continue
        if strategy.get("readiness") != "REQUIRED_NOT_READY":
            raise FinancialMvpContractError(
                f"active strategy lacks evidenced live implementation: {strategy_id}"
            )
        dispositions = _object(
            strategy.get("outcome_dispositions"),
            f"active blocked outcome dispositions {strategy_id}",
        )
        if set(dispositions) != set(OUTCOME_IDS) or set(dispositions.values()) != {
            "REQUIRED"
        }:
            raise FinancialMvpContractError(
                f"active blocked strategy outcome differs: {strategy_id}"
            )
        accepted_numeric = [
            row
            for row in materialized
            if row.get("strategy_id") == strategy_id
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
            and row.get("mode_admission") == "INCLUDE_SELECTED_LIVE"
        ]
        accepted_numeric.extend(
            row
            for row in corrections
            if row.get("strategy_id") == strategy_id
            and row.get("evidence_layer") == "ACCOUNT_LIVE"
            and "INCLUDE_SELECTED_LIVE"
            in _object(
                row.get("mode_admission"),
                f"active blocked mode admission {strategy_id}",
            ).values()
        )
        if accepted_numeric:
            raise FinancialMvpContractError(
                f"active blocked strategy asserts accepted numeric history: {strategy_id}"
            )
        blocking_sources = []
        for source in sources:
            subjects = _object(
                source.get("subjects"), "active blocked source subjects"
            )
            if strategy_id not in subjects.get("strategy_ids", []):
                continue
            if (
                source.get("evidence_layer") == "ACCOUNT_LIVE"
                and source.get("product_requirement") == "REQUIRED"
                and source.get("readiness") == "REQUIRED_NOT_READY"
                and source.get("blockers")
                and source.get("invalidated_outputs")
                and "FAIL" in str(source.get("absence_behavior", ""))
            ):
                blocking_sources.append(source)
        if not blocking_sources:
            raise FinancialMvpContractError(
                f"active blocked numeric source is not fail-closed: {strategy_id}"
            )


def _validate_outcomes(outcomes: list[dict[str, Any]]) -> None:
    if [row.get("outcome_id") for row in outcomes] != list(OUTCOME_IDS):
        raise FinancialMvpContractError(
            "the four financial outcomes are not exact/ordered"
        )
    for row in outcomes:
        _strict_keys(
            row,
            {
                "accepted_statuses",
                "blockers",
                "calculation_contract",
                "consumers",
                "outcome_id",
                "owner_issue",
                "product_requirement",
                "readiness",
            },
            "financial_outcome",
        )
        outcome_id = _text(row["outcome_id"], "financial_outcome.outcome_id")
        if row["product_requirement"] != "REQUIRED":
            raise FinancialMvpContractError(
                f"{outcome_id} must be mechanically REQUIRED"
            )
        if row["readiness"] not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(f"{outcome_id} readiness differs")
        blockers = _blockers(row["blockers"], "financial_outcome.blockers")
        if row["readiness"] == "REQUIRED_NOT_READY" and not blockers:
            raise FinancialMvpContractError(f"{outcome_id} lacks a current blocker")
        if row["readiness"] == "READY" and blockers:
            raise FinancialMvpContractError(
                f"ready outcome retains blockers: {outcome_id}"
            )
        if not isinstance(row["owner_issue"], int) or row["owner_issue"] <= 0:
            raise FinancialMvpContractError(f"{outcome_id} owner issue differs")
        statuses = _strings(row["accepted_statuses"], "accepted_statuses")
        if "UNAVAILABLE" in statuses:
            raise FinancialMvpContractError(
                f"generic UNAVAILABLE cannot accept required outcome {outcome_id}"
            )
        if set(row["consumers"]) != {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}:
            raise FinancialMvpContractError(
                f"{outcome_id} must reach report/workbook/panel/receipt"
            )
        contract = _object(row["calculation_contract"], "calculation_contract")
        if outcome_id == "AS_HELD_COMPOSITE_HWM":
            _strict_keys(
                contract,
                {
                    "capital",
                    "flows",
                    "linking",
                    "membership",
                    "missing_infrastructure",
                    "required_metrics",
                },
                "AS_HELD_COMPOSITE_HWM.calculation_contract",
            )
            if row["owner_issue"] != 324:
                raise FinancialMvpContractError("HWM must remain owned by #324")
            if statuses != ["AVAILABLE"]:
                raise FinancialMvpContractError("HWM accepted statuses differ")
            if set(contract.get("required_metrics", [])) != {
                "COMPOSITE_INDEX",
                "CURRENT_HWM",
                "CURRENT_DRAWDOWN",
                "MAXIMUM_DRAWDOWN",
            }:
                raise FinancialMvpContractError("HWM required metrics differ")
            if contract.get("linking") != "GEOMETRIC_BEGINNING_CAPITAL_WEIGHTED":
                raise FinancialMvpContractError("HWM linking rule differs")
            if contract.get("membership") != "EFFECTIVE_FUNDED_MEMBERSHIP_ONLY":
                raise FinancialMvpContractError("HWM membership rule differs")
            if (
                contract.get("capital")
                != "INDEPENDENT_ANCHORS_WITH_NO_TERMINAL_ONLY_BACKCAST"
            ):
                raise FinancialMvpContractError("HWM capital rule differs")
            if (
                contract.get("flows")
                != "FLOW_NEUTRAL_RETURNS_UNDER_HASH_BOUND_SHARED_POLICY"
            ):
                raise FinancialMvpContractError("HWM flow rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_OUTCOME":
                raise FinancialMvpContractError("HWM absence rule differs")
        elif outcome_id == "INVESTED_CAPITAL":
            _strict_keys(
                contract,
                {
                    "change_control",
                    "classification_treatments",
                    "definition",
                    "distinct_from",
                    "flow_classifications",
                    "flow_timing",
                    "frequency",
                    "fx_base_currency",
                    "gains_treatment",
                    "internal_transfer_aggregate_effect",
                    "leverage_accounting",
                    "missing_history",
                    "negative_net_contributions",
                    "owner_decision_receipts",
                    "policy_id",
                    "policy_owner_issue",
                    "policy_schema_version",
                    "policy_status",
                    "shared_flow_contract",
                },
                "INVESTED_CAPITAL.calculation_contract",
            )
            if row["owner_issue"] != 318:
                raise FinancialMvpContractError(
                    "invested capital must remain owned by #318"
                )
            if statuses != ["AVAILABLE"]:
                raise FinancialMvpContractError(
                    "invested-capital accepted statuses differ"
                )
            if (
                contract.get("definition")
                != "CUMULATIVE_EXTERNAL_CONTRIBUTIONS_MINUS_WITHDRAWALS"
            ):
                raise FinancialMvpContractError("invested-capital definition differs")
            if contract.get("gains_treatment") != "EXCLUDED":
                raise FinancialMvpContractError(
                    "gains must be excluded from invested capital"
                )
            if contract.get("internal_transfer_aggregate_effect") != "ZERO":
                raise FinancialMvpContractError("internal transfers must net to zero")
            if contract.get("frequency") != "DAILY_AND_CURRENT":
                raise FinancialMvpContractError(
                    "invested capital must be daily and current"
                )
            if (
                contract.get("missing_history")
                != "NAMED_INCOMPLETE_INTERVAL_AND_NO_FALSE_VALUE"
            ):
                raise FinancialMvpContractError(
                    "invested-capital missing-history rule differs"
                )
            required_classes = {
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
            if set(contract.get("flow_classifications", [])) != required_classes:
                raise FinancialMvpContractError(
                    "invested-capital flow taxonomy differs"
                )
            if contract.get("policy_id") != "PORT-INVESTED-CAPITAL-1":
                raise FinancialMvpContractError(
                    "invested-capital policy identity differs"
                )
            if contract.get("policy_schema_version") != 1:
                raise FinancialMvpContractError(
                    "invested-capital policy schema version differs"
                )
            if contract.get("policy_owner_issue") != 318:
                raise FinancialMvpContractError(
                    "invested-capital policy must remain owned by #318"
                )
            shared_flow = _object(
                contract.get("shared_flow_contract"),
                "invested-capital shared_flow_contract",
            )
            _strict_keys(
                shared_flow,
                {
                    "conformance_vectors_path",
                    "conformance_vectors_sha256",
                    "current_policy_id",
                    "current_policy_path",
                    "current_policy_sha256",
                    "extension_authority_issue",
                    "extension_status",
                },
                "invested-capital shared_flow_contract",
            )
            if shared_flow["current_policy_id"] != "PORT-PERF-1":
                raise FinancialMvpContractError(
                    "current shared flow/return policy identity differs"
                )
            if shared_flow["extension_authority_issue"] != 324:
                raise FinancialMvpContractError(
                    "shared flow/return extension must remain owned by #324"
                )
            if shared_flow["extension_status"] not in {"READY", "REQUIRED_NOT_READY"}:
                raise FinancialMvpContractError("shared-flow extension status differs")
            expected_shared_paths = {
                "current_policy_path": "docs/decisions/0002-portfolio-performance-policy.md",
                "conformance_vectors_path": "contracts/portfolio-performance-policy-v1.json",
            }
            for path_key, expected_path in expected_shared_paths.items():
                if shared_flow[path_key] != expected_path:
                    raise FinancialMvpContractError(
                        f"shared-flow authority locator differs: {path_key}"
                    )
                path = REPOSITORY_ROOT / expected_path
                hash_key = path_key.replace("_path", "_sha256")
                actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
                if (
                    _hash(shared_flow[hash_key], f"shared-flow {hash_key}")
                    != actual_sha256
                ):
                    raise FinancialMvpContractError(
                        f"shared-flow authority hash differs: {expected_path}"
                    )
            change_control = _object(
                contract.get("change_control"), "invested-capital change_control"
            )
            _strict_keys(
                change_control,
                {
                    "authoritative_edit_location",
                    "check_command",
                    "decision_digest_command",
                    "downstream_issues",
                    "edit_rule",
                    "generated_views",
                    "identity_rule",
                    "live_evidence_command",
                    "propagation_command",
                },
                "invested-capital change_control",
            )
            for key in (
                "authoritative_edit_location",
                "check_command",
                "decision_digest_command",
                "edit_rule",
                "identity_rule",
                "live_evidence_command",
                "propagation_command",
            ):
                _text(change_control[key], f"invested-capital change_control.{key}")
            if set(change_control["downstream_issues"]) != {318, 324, 331, 332}:
                raise FinancialMvpContractError(
                    "invested-capital policy propagation issue set differs"
                )
            if set(change_control["generated_views"]) != {
                "generated/financial-mvp/current-product.md",
                "generated/financial-mvp/source-matrix.json",
            }:
                raise FinancialMvpContractError(
                    "invested-capital generated-view propagation set differs"
                )
            treatments = _object(
                contract.get("classification_treatments"),
                "invested-capital classification_treatments",
            )
            _strict_keys(
                treatments,
                required_classes,
                "invested-capital classification_treatments",
            )
            decisions = {
                flow_class: _validate_policy_decision(
                    treatment,
                    f"invested-capital classification_treatments.{flow_class}",
                )
                for flow_class, treatment in treatments.items()
            }
            product_definition_treatments = {
                "OPENING_CONTRIBUTED_BASIS": "INCREASE_AT_ACCEPTED_OPENING_BASIS",
                "EXTERNAL_CONTRIBUTION": "INCREASE",
                "EXTERNAL_WITHDRAWAL": "DECREASE",
                "INTERNAL_TRANSFER": "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO",
                "DIVIDEND_INTEREST_REINVESTMENT": "NO_INVESTED_CAPITAL_EFFECT",
            }
            for flow_class, selected in product_definition_treatments.items():
                if decisions[flow_class]["selected_treatment"] != selected:
                    raise FinancialMvpContractError(
                        f"product-defined invested-capital treatment differs: {flow_class}"
                    )
            global_decisions = {
                name: _validate_policy_decision(
                    contract.get(name), f"invested-capital {name}"
                )
                for name in (
                    "flow_timing",
                    "fx_base_currency",
                    "negative_net_contributions",
                )
            }
            all_decisions = [*decisions.values(), *global_decisions.values()]
            _validate_leverage_accounting(
                contract.get("leverage_accounting"), decisions["MARGIN_FINANCING"]
            )
            _validate_owner_decision_receipts(contract)
            unresolved_owner = any(
                decision["decision_status"] == "OWNER_DECISION_REQUIRED"
                for decision in all_decisions
            )
            unresolved_dependency = shared_flow[
                "extension_status"
            ] == "REQUIRED_NOT_READY" or any(
                decision["decision_status"] == "DEPENDENCY_REQUIRED"
                for decision in all_decisions
            )
            expected_policy_status = (
                "REQUIRED_NOT_READY"
                if unresolved_owner or unresolved_dependency
                else "READY"
            )
            if contract.get("policy_status") != expected_policy_status:
                raise FinancialMvpContractError(
                    "invested-capital policy readiness does not match decisions"
                )
            if unresolved_owner and 318 not in blockers:
                raise FinancialMvpContractError(
                    "unresolved owner policy decision must retain #318 blocker"
                )
            if unresolved_dependency and 324 not in blockers:
                raise FinancialMvpContractError(
                    "unresolved shared-flow policy must retain #324 blocker"
                )
            if set(contract.get("distinct_from", [])) != {
                "HWM_BEGINNING_CAPITAL",
                "ALLOCATION_VALUE",
                "NAV",
                "MONEY_WEIGHTED_RETURN",
            }:
                raise FinancialMvpContractError(
                    "invested capital distinctness boundary differs"
                )
        elif outcome_id == "DECOMPOSITION":
            _strict_keys(
                contract,
                {
                    "alignment",
                    "evidence_modes",
                    "lenses",
                    "missing_infrastructure",
                    "selection",
                    "surfaces",
                },
                "DECOMPOSITION.calculation_contract",
            )
            if row["owner_issue"] != 328:
                raise FinancialMvpContractError(
                    "decomposition must remain owned by #328"
                )
            if set(statuses) != {
                "AVAILABLE",
                "DIAGNOSTIC_ONLY",
                "NOT_APPLICABLE_OWNER_APPROVED",
            }:
                raise FinancialMvpContractError(
                    "decomposition accepted statuses differ"
                )
            if tuple(contract.get("lenses", [])) != DECOMPOSITION_LENSES:
                raise FinancialMvpContractError("decomposition lenses differ")
            if tuple(contract.get("evidence_modes", [])) != EVIDENCE_MODES:
                raise FinancialMvpContractError("decomposition evidence modes differ")
            if set(contract.get("surfaces", [])) != {"STATIC", "DYNAMIC_FLS"}:
                raise FinancialMvpContractError("decomposition surfaces differ")
            if (
                contract.get("alignment")
                != "EXACT_DATE_NO_FORWARD_FILL_NO_WEEKEND_ZERO"
            ):
                raise FinancialMvpContractError("decomposition alignment rule differs")
            if contract.get("selection") != "TRAINING_ONLY_WITH_SEALED_HOLDOUT":
                raise FinancialMvpContractError("decomposition selection rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_CELL":
                raise FinancialMvpContractError("decomposition absence rule differs")
        elif outcome_id == "DEPENDENCE":
            _strict_keys(
                contract,
                {
                    "analyses",
                    "evidence_modes",
                    "horizons",
                    "missing_infrastructure",
                    "splice",
                },
                "DEPENDENCE.calculation_contract",
            )
            if row["owner_issue"] != 336:
                raise FinancialMvpContractError("dependence must remain owned by #336")
            if set(statuses) != {
                "AVAILABLE",
                "GENUINE_SAMPLE_INSUFFICIENCY_OWNER_APPROVED",
            }:
                raise FinancialMvpContractError("dependence accepted statuses differ")
            if tuple(contract.get("evidence_modes", [])) != EVIDENCE_MODES:
                raise FinancialMvpContractError("dependence evidence modes differ")
            if tuple(contract.get("horizons", [])) != DEPENDENCE_HORIZONS:
                raise FinancialMvpContractError("dependence horizons differ")
            required_analyses = {
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
            }
            if set(contract.get("analyses", [])) != required_analyses:
                raise FinancialMvpContractError("dependence analysis surface differs")
            if (
                contract.get("splice")
                != "VERSION_COMPATIBLE_PREHISTORY_BEFORE_LIVE_INCEPTION_ONLY"
            ):
                raise FinancialMvpContractError("dependence splice rule differs")
            if contract.get("missing_infrastructure") != "FAIL_REQUIRED_CELL":
                raise FinancialMvpContractError("dependence absence rule differs")


def _validate_matrixport_gap_policy(row: Mapping[str, Any], source_id: str) -> None:
    raw_policy = row.get("declared_gap_policy")
    if source_id != "matrixport-account-generation":
        if raw_policy is not None:
            raise FinancialMvpContractError(
                f"source {source_id} asserts an unauthorized declared-gap policy"
            )
        return
    if not isinstance(raw_policy, dict):
        raise FinancialMvpContractError(
            "Matrixport source lacks the owner-declared gap policy"
        )
    policy = _object(raw_policy, "Matrixport declared_gap_policy")
    _strict_keys(
        policy,
        {
            "authority_issue",
            "calendar",
            "coverage_status",
            "cumulative_return_across_declared_gaps",
            "declared_missing_source_dates",
            "frequency",
            "omitted_return_dates",
            "policy_id",
            "provenance",
            "reason",
            "return_transform",
            "return_rule",
            "unavailable_return_dates",
        },
        "Matrixport declared_gap_policy",
    )
    authority_issue = policy["authority_issue"]
    if not isinstance(authority_issue, int) or authority_issue < 1:
        raise FinancialMvpContractError("Matrixport gap authority issue differs")
    for key in (
        "calendar",
        "coverage_status",
        "cumulative_return_across_declared_gaps",
        "frequency",
        "policy_id",
        "provenance",
        "reason",
        "return_rule",
    ):
        _text(policy[key], f"Matrixport {key}")
    return_transform = _object(
        policy["return_transform"], "Matrixport declared-gap return transform"
    )
    _strict_keys(
        return_transform,
        {"facts", "transform", "version"},
        "Matrixport declared-gap return transform",
    )
    _text(return_transform["transform"], "Matrixport return transform")
    _text(return_transform["version"], "Matrixport return transform version")
    _strings(return_transform["facts"], "Matrixport return transform facts")
    rendered_dates: dict[str, list[str]] = {}
    for key in (
        "declared_missing_source_dates",
        "omitted_return_dates",
        "unavailable_return_dates",
    ):
        rendered = _strings(policy[key], f"Matrixport {key}")
        for value in rendered:
            _date(value, f"Matrixport {key}")
        if rendered != sorted(rendered):
            raise FinancialMvpContractError(f"Matrixport {key} is not ordered")
        rendered_dates[key] = rendered
    missing_dates = {
        _date(value, "Matrixport missing date")
        for value in rendered_dates["declared_missing_source_dates"]
    }
    expected_omitted = {
        value + date.resolution
        for value in missing_dates
        if value + date.resolution not in missing_dates
    }
    omitted_dates = {
        _date(value, "Matrixport omitted date")
        for value in rendered_dates["omitted_return_dates"]
    }
    unavailable_dates = {
        _date(value, "Matrixport unavailable date")
        for value in rendered_dates["unavailable_return_dates"]
    }
    if omitted_dates != expected_omitted:
        raise FinancialMvpContractError("Matrixport omitted return dates differ")
    if unavailable_dates != missing_dates | omitted_dates:
        raise FinancialMvpContractError("Matrixport unavailable return dates differ")
    temporal = _object(row["temporal"], "Matrixport temporal")
    if (policy["calendar"], policy["frequency"]) != (
        temporal.get("calendar"),
        temporal.get("cadence"),
    ):
        raise FinancialMvpContractError(
            "Matrixport gap policy calendar/cadence differs from source authority"
        )


def _validate_sources(
    sources: list[dict[str, Any]],
    registry_ids: set[str],
    required_strategy_ids: set[str],
    active_blocked_strategy_ids: set[str],
) -> None:
    source_ids: set[str] = set()
    data_classes: set[str] = set()
    evidence_layers: set[str] = set()
    outcome_consumers: set[str] = set()
    history_layers_by_strategy: dict[str, set[str]] = {
        strategy_id: set() for strategy_id in required_strategy_ids
    }
    decomposition_classes_by_strategy: dict[str, set[str]] = {
        strategy_id: set() for strategy_id in required_strategy_ids
    }
    for row in sources:
        expected_source_keys = {
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
        if "declared_gap_policy" in row:
            expected_source_keys.add("declared_gap_policy")
        _strict_keys(
            row,
            expected_source_keys,
            "source_segment",
        )
        source_id = _text(row["source_id"], "source_segment.source_id")
        if source_id in source_ids:
            raise FinancialMvpContractError(f"duplicate source segment: {source_id}")
        source_ids.add(source_id)
        _text(row["source_version"], "source_segment.source_version")
        strategy_version = _object(
            row["strategy_version"], "source_segment.strategy_version"
        )
        _strict_keys(
            strategy_version,
            {"binding", "status", "version_refs"},
            "source_segment.strategy_version",
        )
        version_status = _text(
            strategy_version["status"], "source_segment.strategy_version.status"
        )
        if version_status not in {
            "EVIDENCED",
            "MIXED",
            "NOT_APPLICABLE",
            "UNRESOLVED",
        }:
            raise FinancialMvpContractError(
                f"source strategy-version status differs: {source_id}"
            )
        _text(strategy_version["binding"], "source_segment.strategy_version.binding")
        version_refs = _object(
            strategy_version["version_refs"],
            "source_segment.strategy_version.version_refs",
        )
        for strategy_id, version_ref in version_refs.items():
            if strategy_id not in registry_ids:
                raise FinancialMvpContractError(
                    f"source {source_id} has unknown strategy-version subject: {strategy_id}"
                )
            _text(version_ref, f"source strategy version {strategy_id}")
        if version_status in {"NOT_APPLICABLE", "UNRESOLVED"} and version_refs:
            raise FinancialMvpContractError(
                f"source {source_id} asserts unresolved/not-applicable version refs"
            )
        if version_status in {"EVIDENCED", "MIXED"} and not version_refs:
            raise FinancialMvpContractError(
                f"source {source_id} claims evidenced versions without identities"
            )
        classes = _strings(row["data_classes"], "source_segment.data_classes")
        if not classes:
            raise FinancialMvpContractError(
                f"source segment has no data class: {source_id}"
            )
        if source_id in REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS:
            missing_leverage_classes = {
                "FINANCING_COST",
                "FINANCING_LIABILITY",
                "LEVERAGE_EXPOSURE",
            } - set(classes)
            if missing_leverage_classes:
                raise FinancialMvpContractError(
                    f"account source lacks leverage facts: {source_id}: "
                    f"{sorted(missing_leverage_classes)}"
                )
        data_classes.update(classes)
        subjects = _object(row["subjects"], "source_segment.subjects")
        _strict_keys(
            subjects, {"account_scope", "strategy_ids"}, "source_segment.subjects"
        )
        _text(subjects["account_scope"], "subjects.account_scope")
        strategy_ids = _strings(subjects["strategy_ids"], "subjects.strategy_ids")
        unknown = set(strategy_ids) - registry_ids
        if unknown:
            raise FinancialMvpContractError(
                f"source segment {source_id} references unknown strategies: {sorted(unknown)}"
            )
        excluded_subjects = set(strategy_ids) - required_strategy_ids
        if row["product_requirement"] == "REQUIRED" and excluded_subjects:
            raise FinancialMvpContractError(
                f"required source {source_id} includes excluded strategies: "
                f"{sorted(excluded_subjects)}"
            )
        unbound_versions = set(version_refs) - set(strategy_ids)
        if unbound_versions:
            raise FinancialMvpContractError(
                f"source {source_id} has version refs outside its subjects: "
                f"{sorted(unbound_versions)}"
            )
        version_subjects = set(strategy_ids)
        evidenced_subjects = set(version_refs)
        if version_status == "EVIDENCED" and evidenced_subjects != version_subjects:
            missing_version_subjects = sorted(version_subjects - evidenced_subjects)
            raise FinancialMvpContractError(
                "source "
                f"{source_id} lacks evidenced version refs for subjects: "
                f"{missing_version_subjects}"
            )
        if version_status == "MIXED" and not (evidenced_subjects < version_subjects):
            raise FinancialMvpContractError(
                f"source {source_id} mixed version status requires a nonempty proper "
                "subset of subject refs"
            )
        _text(row["authority"], "source_segment.authority")
        _no_private_locator(
            row["endpoint_or_artifact_selector"], "endpoint_or_artifact_selector"
        )
        acquisition = _object(row["acquisition"], "source_segment.acquisition")
        _strict_keys(
            acquisition,
            {"adapter", "auth_and_metering_class", "mode"},
            "source_segment.acquisition",
        )
        for key in acquisition:
            _text(acquisition[key], f"acquisition.{key}")
        evidence_layer = _text(row["evidence_layer"], "source_segment.evidence_layer")
        evidence_layers.add(evidence_layer)
        if evidence_layer in {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"}:
            for strategy_id in strategy_ids:
                if strategy_id in history_layers_by_strategy:
                    history_layers_by_strategy[strategy_id].add(evidence_layer)
        interval = _object(row["interval"], "source_segment.interval")
        _strict_keys(
            interval,
            {
                "effective_end",
                "effective_start",
                "observed_end",
                "observed_start",
                "splice_policy",
            },
            "source_segment.interval",
        )
        observed_start = _optional_date(
            interval["observed_start"], "interval.observed_start"
        )
        observed_end = _optional_date(interval["observed_end"], "interval.observed_end")
        effective_start = _optional_date(
            interval["effective_start"], "interval.effective_start"
        )
        effective_end = _optional_date(
            interval["effective_end"], "interval.effective_end"
        )
        if (
            observed_start is not None
            and observed_end is not None
            and observed_end < observed_start
        ):
            raise FinancialMvpContractError(
                f"source observed interval is reversed: {source_id}"
            )
        if (
            effective_start is not None
            and effective_end is not None
            and effective_end < effective_start
        ):
            raise FinancialMvpContractError(
                f"source effective interval is reversed: {source_id}"
            )
        _text(interval["splice_policy"], "interval.splice_policy")
        schema = _object(row["schema"], "source_segment.schema")
        _strict_keys(schema, {"name", "version"}, "source_segment.schema")
        _text(schema["name"], "schema.name")
        _text(schema["version"], "schema.version")
        units = _object(row["units"], "source_segment.units")
        _strict_keys(units, {"currency", "measurement"}, "source_segment.units")
        _text(units["currency"], "units.currency")
        _text(units["measurement"], "units.measurement")
        temporal = _object(row["temporal"], "source_segment.temporal")
        _strict_keys(
            temporal, {"cadence", "calendar", "timezone"}, "source_segment.temporal"
        )
        for key in temporal:
            _text(temporal[key], f"temporal.{key}")
        freshness = _object(row["freshness"], "source_segment.freshness")
        _strict_keys(
            freshness,
            {"max_age_days", "publication_lag_days", "rule"},
            "source_segment.freshness",
        )
        if (
            not isinstance(freshness["max_age_days"], int)
            or freshness["max_age_days"] < 0
        ):
            raise FinancialMvpContractError(
                "freshness.max_age_days must be non-negative"
            )
        if (
            not isinstance(freshness["publication_lag_days"], int)
            or freshness["publication_lag_days"] < 0
        ):
            raise FinancialMvpContractError(
                "freshness.publication_lag_days must be non-negative"
            )
        _text(freshness["rule"], "freshness.rule")
        admission = _object(row["admission"], "source_segment.admission")
        admission_keys = {"facts", "transform", "version"}
        if source_id in REQUIRED_PROVIDER_SOURCE_IDS:
            admission_keys.add("dataset_disposition_policy")
        _strict_keys(admission, admission_keys, "source_segment.admission")
        facts = _strings(admission["facts"], "admission.facts")
        if not facts:
            raise FinancialMvpContractError(
                f"source segment has no admitted fact: {source_id}"
            )
        _text(admission["transform"], "admission.transform")
        _text(admission["version"], "admission.version")
        if source_id in REQUIRED_PROVIDER_SOURCE_IDS:
            policy = _object(
                admission["dataset_disposition_policy"],
                "admission.dataset_disposition_policy",
            )
            _strict_keys(
                policy,
                {
                    "admitted_item_count",
                    "admitted_item_set_sha256",
                    "allowed_exclusion_reasons",
                    "authority_issue",
                    "effective_end",
                    "effective_start",
                    "exclusion_invalidated_outputs_rule",
                    "exclusion_readiness_impact",
                    "policy_id",
                    "policy_owner",
                    "policy_version",
                    "product_data_class_count",
                    "product_data_class_set_sha256",
                    "upstream_inventory_sha256",
                },
                "admission.dataset_disposition_policy",
            )
            if (
                not isinstance(policy["admitted_item_count"], int)
                or policy["admitted_item_count"] < 0
            ):
                raise FinancialMvpContractError(
                    "dataset disposition admitted item count is invalid"
                )
            _hash(
                policy["admitted_item_set_sha256"],
                "dataset disposition admitted item digest",
            )
            if not _strings(
                policy["allowed_exclusion_reasons"],
                "dataset disposition exclusion reasons",
            ):
                raise FinancialMvpContractError(
                    "dataset disposition exclusion reasons are empty"
                )
            if policy["authority_issue"] != 303:
                raise FinancialMvpContractError(
                    "dataset disposition authority issue differs"
                )
            policy_start = _optional_date(
                policy["effective_start"], "dataset disposition effective_start"
            )
            policy_end = _optional_date(
                policy["effective_end"], "dataset disposition effective_end"
            )
            if policy_start is None or (
                policy_end is not None and policy_end < policy_start
            ):
                raise FinancialMvpContractError(
                    "dataset disposition effective interval is invalid"
                )
            if policy["exclusion_invalidated_outputs_rule"] != (
                "SOURCE_SEGMENT_INVALIDATED_OUTPUTS_OR_NO_CURRENT_PRODUCT_OUTPUT"
            ):
                raise FinancialMvpContractError(
                    "dataset disposition invalidated-output rule differs"
                )
            expected_readiness = (
                "NO_CURRENT_PRODUCT_EFFECT"
                if row["product_requirement"] == "EXCLUDED"
                else "REQUIRED_NOT_READY"
            )
            if policy["exclusion_readiness_impact"] != expected_readiness:
                raise FinancialMvpContractError(
                    "dataset disposition readiness impact differs"
                )
            if policy["policy_id"] != "PROVIDER_ADMIT_OR_DECLARE_V1":
                raise FinancialMvpContractError("dataset disposition policy differs")
            if policy["policy_owner"] != "PRODUCT_OWNER_GOVERNING_ISSUE_303":
                raise FinancialMvpContractError(
                    "dataset disposition policy owner differs"
                )
            if policy["policy_version"] != "1":
                raise FinancialMvpContractError(
                    "dataset disposition policy version differs"
                )
            if (
                not isinstance(policy["product_data_class_count"], int)
                or policy["product_data_class_count"] < 1
            ):
                raise FinancialMvpContractError(
                    "dataset disposition product data-class count is invalid"
                )
            _hash(
                policy["product_data_class_set_sha256"],
                "dataset disposition product data-class digest",
            )
            _hash(
                policy["upstream_inventory_sha256"],
                "dataset disposition upstream inventory digest",
            )
        requirement = _text(
            row["product_requirement"], "source_segment.product_requirement"
        )
        if requirement not in {"REQUIRED", "OPTIONAL", "EXCLUDED"}:
            raise FinancialMvpContractError(f"source requirement differs: {source_id}")
        consumers = _strings(row["consumers"], "source_segment.consumers")
        if requirement == "REQUIRED" and not consumers:
            raise FinancialMvpContractError(
                f"required source has no consumer: {source_id}"
            )
        unknown_consumers = set(consumers) - (
            set(OUTCOME_IDS) | {"REPORT", "WORKBOOK", "PANEL", "RELEASE_RECEIPT"}
        )
        if unknown_consumers:
            raise FinancialMvpContractError(
                f"source {source_id} has unknown consumers: {sorted(unknown_consumers)}"
            )
        outcome_consumers.update(set(consumers) & set(OUTCOME_IDS))
        if "DECOMPOSITION" in consumers:
            for strategy_id in strategy_ids:
                if strategy_id in decomposition_classes_by_strategy:
                    decomposition_classes_by_strategy[strategy_id].update(classes)
        generation = _object(
            row["current_generation"], "source_segment.current_generation"
        )
        _strict_keys(
            generation, {"as_of", "generation_id", "sha256"}, "current_generation"
        )
        generation_id = _optional_hash(generation["generation_id"], "generation_id")
        generation_hash = _optional_hash(generation["sha256"], "generation.sha256")
        generation_as_of = _optional_date(generation["as_of"], "generation.as_of")
        if (generation_id is None) != (generation_hash is None):
            raise FinancialMvpContractError(
                f"source {source_id} generation id/hash must be paired"
            )
        if (generation_id is None) != (generation_as_of is None):
            raise FinancialMvpContractError(
                f"source {source_id} generation identity/as-of must be paired"
            )
        existence = _text(row["existence_status"], "source_segment.existence_status")
        capture = _text(row["capture_status"], "source_segment.capture_status")
        if existence not in {"EXISTS", "DOES_NOT_EXIST", "UNRESOLVED"}:
            raise FinancialMvpContractError(f"source existence differs: {source_id}")
        if capture not in {"CAPTURED_GOVERNED", "NOT_CAPTURED", "UNRESOLVED"}:
            raise FinancialMvpContractError(f"source capture differs: {source_id}")
        if capture == "CAPTURED_GOVERNED" and existence != "EXISTS":
            raise FinancialMvpContractError(
                f"source {source_id} conflates governed capture and existence"
            )
        if capture == "CAPTURED_GOVERNED" and generation_id is None:
            raise FinancialMvpContractError(
                f"captured source lacks an immutable generation: {source_id}"
            )
        if capture != "CAPTURED_GOVERNED" and generation_id is not None:
            raise FinancialMvpContractError(
                f"uncaptured source asserts a generation: {source_id}"
            )
        readiness = _text(row["readiness"], "source_segment.readiness")
        if readiness not in {"READY", "REQUIRED_NOT_READY"}:
            raise FinancialMvpContractError(f"source readiness differs: {source_id}")
        blockers = _blockers(row["blockers"], "source_segment.blockers")
        if (
            requirement == "REQUIRED"
            and readiness == "REQUIRED_NOT_READY"
            and not blockers
        ):
            raise FinancialMvpContractError(
                f"required source lacks blocker: {source_id}"
            )
        if requirement == "REQUIRED" and readiness == "READY" and generation_id is None:
            raise FinancialMvpContractError(
                f"ready required source lacks a current generation: {source_id}"
            )
        if (
            requirement == "REQUIRED"
            and readiness == "READY"
            and (existence != "EXISTS" or capture != "CAPTURED_GOVERNED")
        ):
            raise FinancialMvpContractError(
                f"ready required source lacks accepted existence/capture: {source_id}"
            )
        absence_behavior = _text(
            row["absence_behavior"], "source_segment.absence_behavior"
        )
        if absence_behavior == "UNAVAILABLE" or (
            requirement == "REQUIRED" and "FAIL" not in absence_behavior
        ):
            raise FinancialMvpContractError(
                f"required source absence must fail closed without generic UNAVAILABLE: {source_id}"
            )
        invalidated = _strings(
            row["invalidated_outputs"], "source_segment.invalidated_outputs"
        )
        if requirement == "REQUIRED" and not invalidated:
            raise FinancialMvpContractError(
                f"required source has no invalidated outputs: {source_id}"
            )
        _text(row["owner_policy_decision"], "source_segment.owner_policy_decision")
        _validate_matrixport_gap_policy(row, source_id)
    missing = REQUIRED_SOURCE_CLASSES - data_classes
    if missing:
        raise FinancialMvpContractError(
            f"source matrix omits required data classes: {sorted(missing)}"
        )
    missing_providers = REQUIRED_PROVIDER_SOURCE_IDS - source_ids
    if missing_providers:
        raise FinancialMvpContractError(
            f"source matrix omits provider families: {sorted(missing_providers)}"
        )
    missing_provider_layers = {
        "PROVIDER_TRACK_RECORD",
        "PROVIDER_LIVE",
    } - evidence_layers
    if missing_provider_layers:
        raise FinancialMvpContractError(
            f"source matrix lacks provider evidence layers: {sorted(missing_provider_layers)}"
        )
    if "ACCOUNT_LIVE" not in evidence_layers:
        raise FinancialMvpContractError("source matrix lacks account-live evidence")
    missing_outcome_routes = set(OUTCOME_IDS) - outcome_consumers
    if missing_outcome_routes:
        raise FinancialMvpContractError(
            f"source matrix lacks outcome routes: {sorted(missing_outcome_routes)}"
        )
    required_history_layers = {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"}
    for strategy_id, layers in history_layers_by_strategy.items():
        missing_layers = required_history_layers - layers
        if missing_layers:
            raise FinancialMvpContractError(
                f"source matrix lacks history layers for {strategy_id}: {sorted(missing_layers)}"
            )
    required_decomposition_classes = {
        "ECONOMIC_FACTOR_RETURN",
        "ETF_STYLE_RETURN",
        "SECTOR_RETURN",
        "INDUSTRY_RETURN",
    }
    for strategy_id, strategy_classes in decomposition_classes_by_strategy.items():
        if strategy_id in active_blocked_strategy_ids:
            continue
        missing_classes = required_decomposition_classes - strategy_classes
        if missing_classes:
            raise FinancialMvpContractError(
                f"source matrix lacks decomposition classes for {strategy_id}: {sorted(missing_classes)}"
            )


def _validate_strategy_version_evidence(
    strategies: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
) -> None:
    """Keep #302 membership authority separate from design/version evidence."""

    strategy_versions = {
        _text(strategy.get("strategy_id"), "version evidence strategy"): _object(
            strategy.get("strategy_version"), "version evidence strategy_version"
        )
        for strategy in strategies
    }
    bindings: dict[str, list[tuple[str, str]]] = {}
    for source in sources:
        source_id = _text(source.get("source_id"), "version evidence source_id")
        version = _object(
            source.get("strategy_version"),
            f"version evidence source {source_id}",
        )
        for strategy_id, version_ref in _object(
            version.get("version_refs"),
            f"version evidence refs {source_id}",
        ).items():
            product_version = strategy_versions.get(str(strategy_id))
            if (
                product_version is None
                or product_version.get("status") != "EVIDENCED"
                or product_version.get("version_ref") != version_ref
            ):
                raise FinancialMvpContractError(
                    "source strategy design/version differs from product authority: "
                    f"{source_id}/{strategy_id}"
                )
            bindings.setdefault(str(strategy_id), []).append(
                (source_id, _text(version_ref, f"version evidence {strategy_id}"))
            )

    for strategy in strategies:
        strategy_id = _text(strategy.get("strategy_id"), "version evidence strategy")
        if strategy.get("product_requirement") != "REQUIRED":
            continue
        version = _object(
            strategy.get("strategy_version"),
            f"version evidence strategy {strategy_id}",
        )
        if version.get("status") != "EVIDENCED":
            continue
        version_ref = _text(
            version.get("version_ref"), f"version evidence ref {strategy_id}"
        )
        matching_sources = {
            source_id
            for source_id, source_ref in bindings.get(strategy_id, [])
            if source_ref == version_ref
        }
        if (
            "owner-account-live-history" not in matching_sources
            or len(matching_sources) < 2
        ):
            raise FinancialMvpContractError(
                f"strategy design/version evidence is not independently bound: {strategy_id}"
            )


def _validate_golden_case(golden: Mapping[str, Any]) -> None:
    _strict_keys(
        golden,
        {"currency", "expected", "fixture_id", "inputs", "value_class"},
        "golden_case",
    )
    if golden["value_class"] != "SYNTHETIC_VALUE_FREE":
        raise FinancialMvpContractError("golden case must be synthetic and value-free")
    _text(golden["fixture_id"], "golden_case.fixture_id")
    if golden["currency"] != "USD":
        raise FinancialMvpContractError("golden case currency must be USD")
    inputs = _object(golden["inputs"], "golden_case.inputs")
    input_keys = {
        "calculation_configuration",
        "component_returns",
        "decomposition_inputs",
        "dependence_inputs",
        "external_flows",
        "initial_contributions",
        "internal_transfers",
    }
    _strict_keys(inputs, input_keys, "golden_case.inputs")
    configuration = _object(
        inputs["calculation_configuration"],
        "golden_case.inputs.calculation_configuration",
    )
    if configuration != {
        "initial_contributions_timing": "BEGINNING_OF_DAY_BEFORE_RETURN",
        "internal_transfers_timing": "BEGINNING_OF_DAY_BEFORE_RETURN",
        "external_flows_timing": "END_OF_DAY_AFTER_RETURN",
        "base_currency": "USD",
    }:
        raise FinancialMvpContractError("golden calculation configuration differs")

    contributions = _objects(
        inputs["initial_contributions"], "golden initial_contributions"
    )
    component_returns = _objects(
        inputs["component_returns"], "golden component_returns"
    )
    transfers = _objects(inputs["internal_transfers"], "golden internal_transfers")
    flows = _objects(inputs["external_flows"], "golden external_flows")
    contribution_by_date: dict[date, list[tuple[str, Decimal]]] = {}
    strategy_ids: set[str] = set()
    for row in contributions:
        _strict_keys(row, {"amount", "date", "strategy_id"}, "golden contribution")
        observed = _date(row["date"], "golden contribution.date")
        strategy_id = _text(row["strategy_id"], "golden contribution.strategy_id")
        amount = _decimal(row["amount"], "golden contribution.amount")
        if amount <= 0:
            raise FinancialMvpContractError("golden contribution must be positive")
        strategy_ids.add(strategy_id)
        contribution_by_date.setdefault(observed, []).append((strategy_id, amount))
    if strategy_ids != {"golden-a", "golden-b"}:
        raise FinancialMvpContractError("golden strategy perimeter differs")

    returns_by_date: dict[date, dict[str, Decimal]] = {}
    for row in component_returns:
        _strict_keys(row, {"date", "return", "strategy_id"}, "golden return")
        observed = _date(row["date"], "golden return.date")
        strategy_id = _text(row["strategy_id"], "golden return.strategy_id")
        if strategy_id not in strategy_ids:
            raise FinancialMvpContractError("golden return strategy differs")
        daily = returns_by_date.setdefault(observed, {})
        if strategy_id in daily:
            raise FinancialMvpContractError("duplicate golden component return")
        daily[strategy_id] = _decimal(row["return"], "golden return.return")
    if not returns_by_date or any(
        set(daily) != strategy_ids for daily in returns_by_date.values()
    ):
        raise FinancialMvpContractError("golden component-return coverage differs")

    transfers_by_date: dict[date, list[tuple[str, str, Decimal]]] = {}
    for row in transfers:
        _strict_keys(
            row,
            {"amount", "date", "from_strategy_id", "to_strategy_id"},
            "golden internal transfer",
        )
        observed = _date(row["date"], "golden transfer.date")
        source = _text(row["from_strategy_id"], "golden transfer.from_strategy_id")
        target = _text(row["to_strategy_id"], "golden transfer.to_strategy_id")
        amount = _decimal(row["amount"], "golden transfer.amount")
        if source == target or {source, target} - strategy_ids or amount <= 0:
            raise FinancialMvpContractError("golden internal transfer differs")
        transfers_by_date.setdefault(observed, []).append((source, target, amount))

    flows_by_date: dict[date, list[tuple[str, str, Decimal]]] = {}
    for row in flows:
        _strict_keys(
            row,
            {"amount", "classification", "date", "strategy_id"},
            "golden external flow",
        )
        observed = _date(row["date"], "golden external flow.date")
        strategy_id = _text(row["strategy_id"], "golden external flow.strategy_id")
        classification = _text(
            row["classification"], "golden external flow.classification"
        )
        amount = _decimal(row["amount"], "golden external flow.amount")
        if strategy_id not in strategy_ids or classification not in {
            "EXTERNAL_CONTRIBUTION",
            "EXTERNAL_WITHDRAWAL",
        }:
            raise FinancialMvpContractError("golden external-flow class differs")
        if (classification == "EXTERNAL_CONTRIBUTION" and amount <= 0) or (
            classification == "EXTERNAL_WITHDRAWAL" and amount >= 0
        ):
            raise FinancialMvpContractError("golden external-flow sign differs")
        flows_by_date.setdefault(observed, []).append(
            (strategy_id, classification, amount)
        )

    dates = sorted(returns_by_date)
    event_dates = (
        set(contribution_by_date) | set(transfers_by_date) | set(flows_by_date)
    )
    if event_dates - set(dates):
        raise FinancialMvpContractError("golden event lacks a component-return date")
    capital = {strategy_id: Decimal(0) for strategy_id in strategy_ids}
    invested_by_strategy = {strategy_id: Decimal(0) for strategy_id in strategy_ids}
    current_index = Decimal(100)
    index_by_date: dict[str, Decimal] = {}
    invested_by_date: dict[str, Decimal] = {}
    drawdowns: list[Decimal] = []
    current_hwm = current_index
    for observed in dates:
        for strategy_id, amount in contribution_by_date.get(observed, []):
            capital[strategy_id] += amount
            invested_by_strategy[strategy_id] += amount
        for source, target, amount in transfers_by_date.get(observed, []):
            capital[source] -= amount
            capital[target] += amount
            invested_by_strategy[source] -= amount
            invested_by_strategy[target] += amount
        beginning_capital = sum(capital.values(), Decimal(0))
        if beginning_capital <= 0:
            raise FinancialMvpContractError("golden beginning capital is not positive")
        period_profit = sum(
            (
                capital[strategy_id] * returns_by_date[observed][strategy_id]
                for strategy_id in strategy_ids
            ),
            Decimal(0),
        )
        current_index *= Decimal(1) + period_profit / beginning_capital
        for strategy_id in strategy_ids:
            capital[strategy_id] *= Decimal(1) + returns_by_date[observed][strategy_id]
        for strategy_id, _classification, amount in flows_by_date.get(observed, []):
            capital[strategy_id] += amount
            invested_by_strategy[strategy_id] += amount
        current_hwm = max(current_hwm, current_index)
        drawdowns.append(current_index / current_hwm - Decimal(1))
        index_by_date[observed.isoformat()] = current_index
        invested_by_date[observed.isoformat()] = sum(
            invested_by_strategy.values(), Decimal(0)
        )

    expected = _object(golden["expected"], "golden_case.expected")
    _strict_keys(
        expected,
        {
            "decomposition",
            "dependence",
            "hwm",
            "invested_capital",
            "report_panel_parity",
            "source_bindings",
            "stage_identities",
        },
        "golden_case.expected",
    )
    hwm = _object(expected["hwm"], "golden_case.expected.hwm")
    _strict_keys(
        hwm,
        {
            "current_drawdown",
            "current_hwm",
            "current_index",
            "index_by_date",
            "maximum_drawdown",
            "status",
        },
        "golden_case.expected.hwm",
    )
    if hwm["status"] != "AVAILABLE":
        raise FinancialMvpContractError("golden HWM must be numerically AVAILABLE")
    calculated_hwm = {
        "current_index": current_index,
        "current_hwm": current_hwm,
        "current_drawdown": drawdowns[-1],
        "maximum_drawdown": min(drawdowns),
    }
    for key, calculated_value in calculated_hwm.items():
        if _decimal(hwm[key], f"golden hwm.{key}") != calculated_value:
            raise FinancialMvpContractError(f"golden HWM {key} differs")
    expected_index = _object(hwm["index_by_date"], "golden hwm.index_by_date")
    if set(expected_index) != set(index_by_date) or any(
        _decimal(expected_index[key], f"golden hwm.index_by_date.{key}") != value
        for key, value in index_by_date.items()
    ):
        raise FinancialMvpContractError("golden HWM index path differs")
    invested = _object(
        expected["invested_capital"], "golden_case.expected.invested_capital"
    )
    _strict_keys(
        invested,
        {"aggregate_current", "by_strategy_current", "daily_aggregate", "status"},
        "golden_case.expected.invested_capital",
    )
    if invested["status"] != "AVAILABLE" or _decimal(
        invested["aggregate_current"], "aggregate_current"
    ) != sum(invested_by_strategy.values(), Decimal(0)):
        raise FinancialMvpContractError("golden invested capital differs")
    by_strategy = _object(invested["by_strategy_current"], "by_strategy_current")
    if set(by_strategy) != strategy_ids or any(
        _decimal(by_strategy[key], f"golden invested by strategy {key}") != value
        for key, value in invested_by_strategy.items()
    ):
        raise FinancialMvpContractError("golden strategy invested capital differs")
    daily_aggregate = _object(
        invested["daily_aggregate"], "golden invested daily_aggregate"
    )
    if set(daily_aggregate) != set(invested_by_date) or any(
        _decimal(daily_aggregate[key], f"golden invested daily {key}") != value
        for key, value in invested_by_date.items()
    ):
        raise FinancialMvpContractError("golden daily invested capital differs")

    decomposition_inputs = _objects(
        inputs["decomposition_inputs"], "golden decomposition inputs"
    )
    decomposition = _objects(expected["decomposition"], "golden decomposition")
    expected_cells = {
        (lens, mode) for lens in DECOMPOSITION_LENSES for mode in EVIDENCE_MODES
    }
    input_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decomposition_inputs:
        _strict_keys(
            row,
            {
                "evidence_mode",
                "factor_contribution",
                "lens",
                "observed_excess_return",
                "residual",
                "surface",
            },
            "golden decomposition input",
        )
        cell = (
            _text(row["lens"], "golden decomposition input.lens"),
            _text(row["evidence_mode"], "golden decomposition input.evidence_mode"),
        )
        if cell in input_by_cell:
            raise FinancialMvpContractError("duplicate golden decomposition input")
        input_by_cell[cell] = row
    output_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decomposition:
        _strict_keys(
            row,
            {
                "alpha",
                "evidence_mode",
                "factor_contribution",
                "lens",
                "observed_excess_return",
                "residual",
                "status",
                "surface",
            },
            "golden decomposition output",
        )
        cell = (
            _text(row["lens"], "golden decomposition.lens"),
            _text(row["evidence_mode"], "golden decomposition.evidence_mode"),
        )
        if cell in output_by_cell:
            raise FinancialMvpContractError("duplicate golden decomposition output")
        output_by_cell[cell] = row
    if set(input_by_cell) != expected_cells or set(output_by_cell) != expected_cells:
        raise FinancialMvpContractError("golden decomposition cells differ")
    for cell in expected_cells:
        input_row = input_by_cell[cell]
        output_row = output_by_cell[cell]
        observed_return = _decimal(
            input_row["observed_excess_return"], "golden observed excess return"
        )
        factor = _decimal(
            input_row["factor_contribution"], "golden factor contribution"
        )
        residual = _decimal(input_row["residual"], "golden residual")
        if (
            output_row["status"] != "AVAILABLE"
            or output_row["surface"] != input_row["surface"]
            or _decimal(output_row["observed_excess_return"], "golden observed output")
            != observed_return
            or _decimal(output_row["factor_contribution"], "golden factor output")
            != factor
            or _decimal(output_row["residual"], "golden residual output") != residual
            or _decimal(output_row["alpha"], "golden alpha")
            != observed_return - factor - residual
        ):
            raise FinancialMvpContractError(
                f"golden decomposition value differs: {cell}"
            )
    if {row["surface"] for row in decomposition} != {"STATIC", "DYNAMIC_FLS"}:
        raise FinancialMvpContractError("golden decomposition surfaces differ")

    dependence_inputs = _object(inputs["dependence_inputs"], "golden dependence inputs")
    dependence = _object(expected["dependence"], "golden dependence")
    if set(dependence) != set(EVIDENCE_MODES) or set(dependence_inputs) != set(
        EVIDENCE_MODES
    ):
        raise FinancialMvpContractError("golden dependence modes differ")
    parsed_dependence: dict[str, tuple[list[Decimal], list[Decimal]]] = {}
    for mode in EVIDENCE_MODES:
        input_row = _object(dependence_inputs[mode], f"golden dependence input.{mode}")
        _strict_keys(
            input_row,
            {
                "central_case_loss",
                "correlation_one_bound",
                "prehistory_count",
                "series_a",
                "series_b",
                "version_compatibility",
            },
            f"golden dependence input.{mode}",
        )
        left = [
            _decimal(value, f"golden dependence {mode}.series_a")
            for value in input_row["series_a"]
        ]
        right = [
            _decimal(value, f"golden dependence {mode}.series_b")
            for value in input_row["series_b"]
        ]
        parsed_dependence[mode] = (left, right)
        prehistory_count = input_row["prehistory_count"]
        if not isinstance(prehistory_count, int) or isinstance(prehistory_count, bool):
            raise FinancialMvpContractError(
                "golden prehistory_count must be an integer"
            )
        if mode == "LIVE_ONLY" and (
            input_row["version_compatibility"] != "LIVE_ONLY_NO_BACKTEST"
            or prehistory_count != 0
        ):
            raise FinancialMvpContractError("golden LIVE_ONLY compatibility differs")
        if mode == "LIVE_PLUS_BACKTEST" and (
            input_row["version_compatibility"] != "EXACT_VERSION_MATCH"
            or prehistory_count <= 0
        ):
            raise FinancialMvpContractError(
                "golden LIVE_PLUS_BACKTEST compatibility differs"
            )
        row = _object(dependence[mode], f"golden dependence.{mode}")
        _strict_keys(
            row,
            {
                "central_case_loss",
                "correlation_one_bound",
                "horizons",
                "pearson",
                "sample_count",
                "spearman",
                "status",
            },
            f"golden dependence.{mode}",
        )
        pearson = _pearson(left, right)
        spearman = _spearman(left, right)
        if (
            row["status"] != "AVAILABLE_WITH_NAMED_HORIZON_INSUFFICIENCIES"
            or row["sample_count"] != len(left)
            or _decimal(row["pearson"], f"golden dependence {mode}.pearson") != pearson
            or _decimal(row["spearman"], f"golden dependence {mode}.spearman")
            != spearman
            or _decimal(row["central_case_loss"], "golden central_case_loss")
            != _decimal(input_row["central_case_loss"], "golden input central loss")
            or _decimal(row["correlation_one_bound"], "golden correlation_one_bound")
            != _decimal(
                input_row["correlation_one_bound"], "golden input correlation bound"
            )
        ):
            raise FinancialMvpContractError(f"golden dependence values differ: {mode}")
        horizons = _object(row["horizons"], f"golden dependence {mode}.horizons")
        if set(horizons) != {str(value) for value in DEPENDENCE_HORIZONS}:
            raise FinancialMvpContractError(
                f"golden dependence horizons differ: {mode}"
            )
        for horizon in DEPENDENCE_HORIZONS:
            observed = horizons[str(horizon)]
            if len(left) < horizon:
                if observed != "GENUINE_SAMPLE_INSUFFICIENCY":
                    raise FinancialMvpContractError(
                        f"golden horizon insufficiency differs: {mode}/{horizon}"
                    )
            elif _decimal(observed, f"golden horizon {mode}/{horizon}") != _pearson(
                left[-horizon:], right[-horizon:]
            ):
                raise FinancialMvpContractError(
                    f"golden horizon value differs: {mode}/{horizon}"
                )
    live_left, live_right = parsed_dependence["LIVE_ONLY"]
    hybrid_left, hybrid_right = parsed_dependence["LIVE_PLUS_BACKTEST"]
    prehistory_count = dependence_inputs["LIVE_PLUS_BACKTEST"]["prehistory_count"]
    if (
        len(hybrid_left) != len(live_left) + prehistory_count
        or hybrid_left[prehistory_count:] != live_left
        or hybrid_right[prehistory_count:] != live_right
    ):
        raise FinancialMvpContractError(
            "golden LIVE_PLUS_BACKTEST is not compatible prehistory before live"
        )

    source_bindings = _objects(
        expected["source_bindings"], "golden expected.source_bindings"
    )
    binding_contract = {
        "calculation_configuration": (
            "golden-calculation-configuration",
            "golden-calculation-configuration",
            {
                "AS_HELD_COMPOSITE_HWM",
                "INVESTED_CAPITAL",
                "DECOMPOSITION",
                "DEPENDENCE",
            },
        ),
        "initial_contributions": (
            "golden-initial-contributions",
            "golden-capital-event",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "component_returns": (
            "golden-component-returns",
            "golden-component-return",
            {"AS_HELD_COMPOSITE_HWM"},
        ),
        "internal_transfers": (
            "golden-internal-transfers",
            "golden-internal-transfer",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "external_flows": (
            "golden-external-flows",
            "golden-external-flow",
            {"AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"},
        ),
        "decomposition_inputs": (
            "golden-decomposition-inputs",
            "golden-decomposition-input",
            {"DECOMPOSITION"},
        ),
        "dependence_inputs": (
            "golden-dependence-inputs",
            "golden-dependence-input",
            {"DEPENDENCE"},
        ),
    }
    if [row.get("input_key") for row in source_bindings] != list(binding_contract):
        raise FinancialMvpContractError("golden source-binding perimeter differs")
    for row in source_bindings:
        _strict_keys(
            row,
            {
                "consumers",
                "generation_sha256",
                "input_key",
                "schema",
                "source_id",
                "version",
            },
            "golden source binding",
        )
        input_key = _text(row["input_key"], "golden source binding.input_key")
        source_id, schema_name, consumers = binding_contract[input_key]
        if (
            row["source_id"] != source_id
            or row["schema"] != schema_name
            or row["version"] != "1"
            or set(row["consumers"]) != consumers
            or _hash(row["generation_sha256"], "golden source generation_sha256")
            != _payload_hash(inputs[input_key])
        ):
            raise FinancialMvpContractError(
                f"golden source binding differs: {input_key}"
            )

    stage_identities = _object(
        expected["stage_identities"], "golden expected.stage_identities"
    )
    if set(stage_identities) != set(OUTCOME_IDS):
        raise FinancialMvpContractError("golden stage identities differ")
    stage_inputs = {
        "AS_HELD_COMPOSITE_HWM": {
            key: inputs[key]
            for key in (
                "calculation_configuration",
                "initial_contributions",
                "component_returns",
                "internal_transfers",
                "external_flows",
            )
        },
        "INVESTED_CAPITAL": {
            key: inputs[key]
            for key in (
                "calculation_configuration",
                "initial_contributions",
                "internal_transfers",
                "external_flows",
            )
        },
        "DECOMPOSITION": {
            key: inputs[key]
            for key in ("calculation_configuration", "decomposition_inputs")
        },
        "DEPENDENCE": {
            key: inputs[key]
            for key in ("calculation_configuration", "dependence_inputs")
        },
    }
    stage_outputs = {
        "AS_HELD_COMPOSITE_HWM": expected["hwm"],
        "INVESTED_CAPITAL": expected["invested_capital"],
        "DECOMPOSITION": expected["decomposition"],
        "DEPENDENCE": expected["dependence"],
    }
    stage_ids = {
        "AS_HELD_COMPOSITE_HWM": "GOLDEN_HWM_V1",
        "INVESTED_CAPITAL": "GOLDEN_INVESTED_CAPITAL_V1",
        "DECOMPOSITION": "GOLDEN_DECOMPOSITION_V1",
        "DEPENDENCE": "GOLDEN_DEPENDENCE_V1",
    }
    for outcome_id in OUTCOME_IDS:
        row = _object(
            stage_identities[outcome_id], f"golden stage identity {outcome_id}"
        )
        _strict_keys(
            row,
            {"input_sha256", "output_sha256", "stage_id"},
            f"golden stage identity {outcome_id}",
        )
        if (
            row["stage_id"] != stage_ids[outcome_id]
            or _hash(row["input_sha256"], "golden stage input_sha256")
            != _payload_hash(stage_inputs[outcome_id])
            or _hash(row["output_sha256"], "golden stage output_sha256")
            != _payload_hash(stage_outputs[outcome_id])
        ):
            raise FinancialMvpContractError(
                f"golden stage identity differs: {outcome_id}"
            )
    parity = _object(expected["report_panel_parity"], "golden report_panel_parity")
    if parity != {
        "hwm_equal": True,
        "invested_capital_equal": True,
        "decomposition_equal": True,
        "dependence_equal": True,
    }:
        raise FinancialMvpContractError("golden report/panel parity differs")


def _validate_epoch_manifest_source_shape(source: object) -> None:
    """Validate the v7 source shape without claiming equality to any bundle."""

    source_object = _object(source, "epoch_manifest_source")
    _strict_keys(
        source_object,
        {
            "generation_id",
            "generation_manifest_sha256",
            "generation_receipt_sha256",
            "full_directory_inventory_sha256",
            "artifact",
        },
        "epoch_manifest_source",
    )
    for name in (
        "generation_id",
        "generation_manifest_sha256",
        "generation_receipt_sha256",
        "full_directory_inventory_sha256",
    ):
        _hash(source_object[name], f"epoch_manifest_source.{name}")

    artifact = _object(source_object["artifact"], "epoch_manifest_source.artifact")
    _strict_keys(
        artifact,
        {"path", "sha256", "size_bytes", "schema"},
        "epoch_manifest_source.artifact",
    )
    if artifact["path"] != EPOCH_MANIFEST_ARTIFACT_PATH:
        raise FinancialMvpContractError("epoch_manifest_source.artifact.path differs")
    _hash(artifact["sha256"], "epoch_manifest_source.artifact.sha256")
    size_bytes = artifact["size_bytes"]
    if type(size_bytes) is not int or size_bytes <= 0:
        raise FinancialMvpContractError(
            "epoch_manifest_source.artifact.size_bytes must be a positive integer"
        )

    schema = _object(artifact["schema"], "epoch_manifest_source.artifact.schema")
    _strict_keys(
        schema,
        {"name", "version"},
        "epoch_manifest_source.artifact.schema",
    )
    if schema["name"] != EPOCH_MANIFEST_ARTIFACT_SCHEMA_NAME:
        raise FinancialMvpContractError(
            "epoch_manifest_source.artifact.schema.name differs"
        )
    if schema["version"] != EPOCH_MANIFEST_ARTIFACT_SCHEMA_VERSION:
        raise FinancialMvpContractError(
            "epoch_manifest_source.artifact.schema.version differs"
        )


def validate_contract(
    contract: Mapping[str, Any],
    *,
    registry: Mapping[str, Any],
    epoch_manifest_bundle: GenerationBundle | None = None,
) -> dict[str, Any]:
    """Validate executable product/source semantics and return a plain object."""

    _reject_private_values(contract)
    v6_keys = {
        "$schema",
        "contract_schema_version",
        "current_census",
        "financial_outcomes",
        "golden_case",
        "history_corrections",
        "product",
        "source_segments",
        "strategies",
    }
    version = contract.get("contract_schema_version")
    if version == 6:
        _strict_keys(contract, v6_keys, "financial MVP contract")
    elif version == 7:
        _strict_keys(
            contract,
            v6_keys | {"epoch_manifest_source"},
            "financial MVP contract",
        )
        _validate_epoch_manifest_source_shape(contract["epoch_manifest_source"])
        if epoch_manifest_bundle is None:
            raise FinancialMvpContractError(
                "contract schema v7 requires a verified epoch-manifest bundle"
            )
        validate_epoch_manifest_source(
            contract["epoch_manifest_source"], bundle=epoch_manifest_bundle
        )
    else:
        raise FinancialMvpContractError("contract_schema_version must be 6 or 7")
    if version == 6 and epoch_manifest_bundle is not None:
        raise FinancialMvpContractError(
            "contract schema v6 cannot bind an epoch-manifest bundle"
        )
    if contract["$schema"] != (
        "../schemas/portfolio/financial-mvp-source-contract.schema.json"
    ):
        raise FinancialMvpContractError("contract schema locator differs")
    product = _object(contract["product"], "product")
    _validate_product(product)
    registry_ids = _registry_strategy_ids(registry)
    strategies = _objects(contract["strategies"], "strategies")
    _validate_strategies(strategies, registry_ids)
    outcomes = _objects(contract["financial_outcomes"], "financial_outcomes")
    _validate_outcomes(outcomes)
    sources = _objects(contract["source_segments"], "source_segments")
    required_strategy_ids = {
        row["strategy_id"]
        for row in strategies
        if row["product_requirement"] == "REQUIRED"
    }
    active_blocked_strategy_ids = {
        row["strategy_id"]
        for row in strategies
        if row["lifecycle_status"] == "ACTIVE"
        and row["product_requirement"] == "REQUIRED"
        and row["readiness"] == "REQUIRED_NOT_READY"
        and row["live_implementation_identity"]["status"] != "EVIDENCED"
    }
    _validate_sources(
        sources,
        set(registry_ids),
        required_strategy_ids,
        active_blocked_strategy_ids,
    )
    _validate_strategy_version_evidence(strategies, sources)
    _validate_current_census(
        _object(contract["current_census"], "current_census"),
        strategies,
        sources,
    )
    _validate_history_corrections(
        _object(contract["history_corrections"], "history_corrections"),
        strategies,
        sources,
        set(registry_ids),
    )
    _validate_active_blocked_numeric_identities(
        strategies,
        sources,
        _object(contract["history_corrections"], "history_corrections"),
    )
    _validate_golden_case(_object(contract["golden_case"], "golden_case"))
    return dict(contract)


def load_contract(
    contract_path: Path = DEFAULT_CONTRACT,
    registry_path: Path = DEFAULT_REGISTRY,
    schema_path: Path = DEFAULT_SCHEMA,
    *,
    epoch_manifest_generation_root: Path | None = None,
    epoch_manifest_generation_id: str | None = None,
) -> dict[str, Any]:
    """Load and validate the one current financial-product authority."""

    if (epoch_manifest_generation_root is None) != (
        epoch_manifest_generation_id is None
    ):
        raise FinancialMvpContractError(
            "epoch-manifest generation root and id must be supplied together"
        )
    epoch_manifest_bundle: GenerationBundle | None = None
    if epoch_manifest_generation_root is not None:
        assert epoch_manifest_generation_id is not None
        try:
            epoch_manifest_bundle = read_generation_bundle(
                epoch_manifest_generation_root, epoch_manifest_generation_id
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise FinancialMvpContractError(
                "epoch-manifest generation bundle verification failed"
            ) from exc

    contract = _load_json(contract_path, "financial MVP contract")
    schema = _load_json(schema_path, "financial MVP contract schema")
    errors = schema_errors(schema, contract, schema)
    if errors:
        raise FinancialMvpContractError(
            "financial MVP contract schema validation failed: " + "; ".join(errors[:10])
        )
    registry = _load_json(registry_path, "current strategy registry")
    return validate_contract(
        contract,
        registry=registry,
        epoch_manifest_bundle=epoch_manifest_bundle,
    )


def render_views(contract: Mapping[str, Any]) -> GeneratedViews:
    """Render deterministic human/agent views from the authoritative bytes."""

    digest = contract_digest(contract)
    invested_policy_digest = invested_capital_policy_digest(contract)
    census = _object(contract["current_census"], "current_census")
    accepted_census_digest = census_digest(census)
    product = _object(contract["product"], "product")
    outcomes = _objects(contract["financial_outcomes"], "financial_outcomes")
    strategies = _objects(contract["strategies"], "strategies")
    sources = _objects(contract["source_segments"], "source_segments")
    history = _object(contract["history_corrections"], "history_corrections")
    history_rows = _objects(history["corrections"], "history corrections")
    materialized_segments = _objects(
        history["materialized_segment_identities"],
        "materialized history segment identities",
    )
    epoch_manifest_source = (
        _object(contract["epoch_manifest_source"], "epoch_manifest_source")
        if "epoch_manifest_source" in contract
        else None
    )
    lines = [
        "# Current financial MVP",
        "",
        "> Generated by `tools/portfolio_financial_mvp_contract.py`; do not edit.",
        "",
        f"- Product version: `{product['product_version']}`",
        f"- Canonical contract SHA-256: `{digest}`",
        f"- Current census SHA-256: `{accepted_census_digest}`",
        f"- Runtime profile: `{product['runtime_profile']}`",
        f"- Publication: `{product['publication_boundary']['status']}`",
        "",
        "## Calculation boundary and data age",
        "",
        f"- Policy: `{product['as_of_policy']['policy_id']}`",
        f"- Boundary: `{product['as_of_policy']['calculation_boundary']['rule']}`",
        f"- Scope: `{product['as_of_policy']['calculation_boundary']['scope']}`",
        f"- Post-boundary rows: `{product['as_of_policy']['calculation_boundary']['post_boundary_rows']}`",
        f"- Age disposition: `{product['as_of_policy']['calculation_boundary']['age_disposition']}`",
        f"- Completeness failure: `{product['as_of_policy']['calculation_boundary']['completeness_failure_disposition']}`",
        f"- Development profile: `{product['as_of_policy']['execution_profiles']['DEVELOPMENT']}`",
        f"- Workstation-QC profile: `{product['as_of_policy']['execution_profiles']['WORKSTATION_QC']}`",
        f"- Release-QC profile: `{product['as_of_policy']['execution_profiles']['RELEASE_QC']}`",
        f"- Owner evidence: {product['as_of_policy']['decision_evidence']['evidence_url']}",
        f"- Calendar rule: {product['as_of_policy']['calculation_boundary']['calendar_rule']}",
        f"- Publication-lag rule: {product['as_of_policy']['publication_lag_rule']}",
        "",
        "## Required outcomes",
        "",
        "| Outcome | Requirement | Readiness | Owner | Blockers |",
        "|---|---|---|---:|---|",
    ]
    for row in outcomes:
        blockers = ", ".join(f"#{value}" for value in row["blockers"]) or "None"
        lines.append(
            f"| {row['outcome_id']} | {row['product_requirement']} | "
            f"{row['readiness']} | #{row['owner_issue']} | {blockers} |"
        )
    invested = _outcome_calculation_contract(contract, "INVESTED_CAPITAL")
    shared_flow = _object(
        invested["shared_flow_contract"], "invested-capital shared_flow_contract"
    )
    change_control = _object(
        invested["change_control"], "invested-capital change_control"
    )
    lines.extend(
        [
            "",
            "## Invested-capital policy — #318",
            "",
            f"- Policy: `{invested['policy_id']}` schema `{invested['policy_schema_version']}`",
            f"- Policy SHA-256: `{invested_policy_digest}`",
            f"- Readiness: `{invested['policy_status']}`",
            f"- Accepted shared policy: `{shared_flow['current_policy_id']}` (`{shared_flow['current_policy_path']}`)",
            f"- #324 shared-policy extension: `{shared_flow['extension_status']}`",
            f"- Authoritative edit location: `{change_control['authoritative_edit_location']}`",
            f"- Approval/change rule: `{change_control['edit_rule']}`",
            f"- Recompute decision receipts: `{change_control['decision_digest_command']}`",
            f"- Verify owner evidence: `{change_control['live_evidence_command']}`",
            f"- Regenerate: `{change_control['propagation_command']}`",
            f"- Check: `{change_control['check_command']}`",
            "",
            "### Flow classifications",
            "",
            "| Classification | Plain-language meaning | Status | Selected treatment | Recommendation | Calculation effect | Evidence |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    treatments = _object(
        invested["classification_treatments"],
        "invested-capital classification_treatments",
    )
    for flow_class in invested["flow_classifications"]:
        decision = _object(treatments[flow_class], f"policy {flow_class}")
        selected = decision["selected_treatment"] or "PENDING"
        recommended = decision["recommended_treatment"] or "—"
        evidence = decision["approval_evidence"] or "PENDING"
        lines.append(
            f"| {flow_class} | {decision['plain_language']} | "
            f"{decision['decision_status']} | {selected} | {recommended} | "
            f"{decision['calculation_effect']} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "### Cross-cutting rules",
            "",
            "| Rule | Plain-language meaning | Status | Selected treatment | Recommendation | Calculation effect | Evidence |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for rule_name in (
        "flow_timing",
        "fx_base_currency",
        "negative_net_contributions",
    ):
        decision = _object(invested[rule_name], f"policy {rule_name}")
        selected = decision["selected_treatment"] or "PENDING"
        recommended = decision["recommended_treatment"] or "—"
        evidence = decision["approval_evidence"] or "PENDING"
        lines.append(
            f"| {rule_name} | {decision['plain_language']} | "
            f"{decision['decision_status']} | {selected} | {recommended} | "
            f"{decision['calculation_effect']} | {evidence} |"
        )
    leverage = _object(
        invested["leverage_accounting"], "invested-capital leverage_accounting"
    )
    leverage_vector = _object(
        leverage["synthetic_vector"], "invested-capital leverage synthetic_vector"
    )
    lines.extend(
        [
            "",
            "### Leverage treatment",
            "",
            f"- Status: `{leverage['status']}`",
            f"- Evidence: `{leverage['approval_evidence']}`",
            f"- Invested-capital treatment: `{leverage['invested_capital_treatment']}`",
            f"- Required facts: `{', '.join(leverage['required_facts'])}`",
            f"- Missing facts: `{leverage['missing_fact_behavior']}`",
            f"- Double-counting guard: `{leverage['double_counting_guard']}`",
            "",
            "| Affected metric | Required treatment |",
            "|---|---|",
        ]
    )
    for metric_id, treatment in leverage["affected_metrics"].items():
        lines.append(f"| {metric_id} | {treatment} |")
    lines.extend(
        [
            "",
            "#### Exact synthetic leverage vector",
            "",
            "| Field | Value |",
            "|---|---:|",
        ]
    )
    for field, value in leverage_vector.items():
        lines.append(f"| {field} | {value} |")
    receipts = _object(
        invested["owner_decision_receipts"],
        "invested-capital owner_decision_receipts",
    )
    lines.extend(
        [
            "",
            "### Owner decision receipts",
            "",
            "Each receipt binds one independently changeable policy payload to its recorded owner approval. A policy edit requires new approval evidence and an updated receipt before regeneration succeeds.",
            "",
            "| Decision | Approved | Approval evidence | Digest evidence | Payload SHA-256 |",
            "|---|---|---|---|---|",
        ]
    )
    for decision_id in OWNER_DECISION_IDS:
        receipt = _object(receipts[decision_id], f"owner receipt {decision_id}")
        lines.append(
            f"| {decision_id} | {receipt['approved_on']} | "
            f"{receipt['evidence_url']} | {receipt['digest_evidence_url']} | "
            f"`{receipt['payload_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## Current perimeter",
            "",
            "| Strategy | Lifecycle | History series | Requirement | Readiness | Design/config version | Live implementation | Funded membership | Blockers |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in strategies:
        version = row["strategy_version"]
        membership = row["funded_membership"]
        blockers = ", ".join(f"#{value}" for value in row["blockers"]) or "None"
        version_text = version["version_ref"] or version["status"]
        live_identity = row["live_implementation_identity"]
        live_text = live_identity["version_ref"] or live_identity["status"]
        history_series = row["history_series_id"] or "NOT_CAPTURED"
        lines.append(
            f"| {row['strategy_id']} | {row['lifecycle_status']} | {history_series} | "
            f"{row['product_requirement']} | "
            f"{row['readiness']} | {version_text} | {live_text} | "
            f"{membership['status']} | "
            f"{blockers} |"
        )
    lines.extend(
        [
            "",
            "## Current provider/account census — #302",
            "",
            f"- Census ID: `{census['census_id']}`",
            f"- Census as-of: `{census['as_of']}`",
            f"- Owner decision approved: `{census['decision_approved_on']}`",
            f"- Decision evidence: {census['decision_evidence_url']}",
            f"- Decision payload SHA-256: `{census['decision_payload_sha256']}`",
            f"- Census SHA-256: `{accepted_census_digest}`",
            f"- Private crosswalk: `{census['private_crosswalk_rule']}`",
            "",
            "### Provider lanes",
            "",
            "| Provider | Role | Status | Source | Redacted sleeves | Current generation |",
            "|---|---|---|---|---:|---|",
        ]
    )
    for lane in census["provider_lanes"]:
        generation = lane["generation"]
        generation_text = (
            f"{generation['generation_id'][:12]} / {generation['as_of']}"
            if isinstance(generation, dict)
            else "NOT_APPLICABLE"
        )
        lines.append(
            f"| {lane['provider_id']} | {lane['lane_role']} | "
            f"{lane['lane_status']} | {lane['source_segment_id'] or 'NONE'} | "
            f"{len(lane['account_sleeve_ids'])} | {generation_text} |"
        )
    lines.extend(
        [
            "",
            "### Redacted account sleeves",
            "",
            "| Sleeve alias | Provider | Role | Current strategy | Effective assignments | Source segments |",
            "|---|---|---|---|---|---|",
        ]
    )
    for sleeve in census["account_sleeves"]:
        assignments = (
            "; ".join(
                f"{item['strategy_id']}:{item['status']}:"
                f"{item['effective_start']}..{item['effective_end'] or 'OPEN'}"
                for item in sleeve["assignments"]
            )
            or "NONE"
        )
        source_text = ", ".join(sleeve["source_segment_ids"]) or "NONE"
        lines.append(
            f"| {sleeve['sleeve_alias']} | {sleeve['provider_id']} | "
            f"{sleeve['role']} | {sleeve['current_strategy_id'] or 'NONE'} | "
            f"{assignments} | {source_text} |"
        )
    lines.extend(
        [
            "",
            "## Truthful strategy-history corrections — #319",
            "",
            f"- Policy: `{history['policy_id']}` version `{history['policy_version']}`",
            f"- Splice: `{history['splice_policy']}`",
            "",
            "| Correction | Strategy | Layer | Existence | Capture | Observed interval | Funding / live start | Version compatibility | LIVE_ONLY | LIVE_PLUS_BACKTEST | Readiness | Source | Evidence receipts | Derived / rejected evidence identity |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in history_rows:
        interval = row["interval"]
        observed = (
            f"{interval['observed_start'] or '—'}..{interval['observed_end'] or '—'}"
        )
        funding = row["funding_event"]
        funding_text = (
            f"{funding['event_date'] or '—'} / {funding['account_live_start'] or '—'}"
        )
        capture = row["capture"]
        capture_text = capture["status"]
        if capture["capture_defect"]:
            capture_text += f": {capture['capture_defect']}"
        receipt_text = "; ".join(
            f"{receipt['authority_root']}:{receipt['artifact_selector']}@"
            f"{receipt['sha256'][:12]}"
            for receipt in row["source_receipts"]
        )
        rejected_identity = row.get("rejected_evidence_identity")
        rejected_identity_text = (
            f"{rejected_identity['return_observations']} rows@"
            f"{rejected_identity['sha256'][:12]} from "
            f"{rejected_identity['source_receipt_sha256'][:12]}"
            if isinstance(rejected_identity, dict)
            else "—"
        )
        derived_identity = row.get("derived_evidence_identity")
        derived_backtest_identity = row.get("derived_backtest_identity")
        evidence_identity_text = (
            f"{derived_identity['raw_level_observations']} levels / "
            f"{derived_identity['return_observations']} returns / "
            f"{derived_identity['equity_overlap_observations']} equity overlaps / "
            f"{derived_identity['calendar']} / Cash Added: "
            f"{derived_identity['cash_added_treatment']} / "
            f"{derived_identity['transform_id']}"
            if isinstance(derived_identity, dict)
            else (
                f"{derived_backtest_identity['index_observations']} index levels / "
                f"{derived_backtest_identity['return_observations']} returns / "
                f"{derived_backtest_identity['observed_start']}.."
                f"{derived_backtest_identity['observed_end']} / "
                f"{derived_backtest_identity['relationship_type']} / "
                f"sources: {','.join(derived_backtest_identity['source_labels'])}"
                if isinstance(derived_backtest_identity, dict)
                else rejected_identity_text
            )
        )
        lines.append(
            "| "
            + " | ".join(
                str(value).replace("|", "\\|")
                for value in (
                    row["correction_id"],
                    row["strategy_id"],
                    row["evidence_layer"],
                    row["history_existence"]["status"],
                    capture_text,
                    observed,
                    funding_text,
                    row["strategy_version"]["compatibility"],
                    row["mode_admission"]["LIVE_ONLY"],
                    row["mode_admission"]["LIVE_PLUS_BACKTEST"],
                    row["readiness"],
                    row["source_id"],
                    receipt_text,
                    evidence_identity_text,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "### Materialized segment value authority",
            "",
            "Each segment hash is calculated from canonical return rows and bound to the current governed source-generation manifest. A coherent output rewrite cannot replace these independently controlled contract bytes.",
            "",
            "| Segment | Strategy | Layer / admission | Observed interval | Returns | Value SHA-256 | Source receipt |",
            "|---|---|---|---|---:|---|---|",
        ]
    )
    for row in materialized_segments:
        materialized_cells = (
            row["segment_id"],
            row["strategy_id"],
            f"{row['evidence_layer']} / {row['mode_admission']}",
            f"{row['observed_start']}..{row['observed_end']}",
            row["return_observations"],
            row["sha256"],
            (
                f"{row['source_id']}@{row['source_generation_id'][:12]} / "
                f"{row['source_generation_sha256'][:12]}"
            ),
        )
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in materialized_cells)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Source timing",
            "",
            "| Source | Cadence | Calendar | Age warning threshold days | Publication lag days | Freshness rule |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for row in sources:
        timing_cells = (
            row["source_id"],
            row["temporal"]["cadence"],
            row["temporal"]["calendar"],
            row["freshness"]["max_age_days"],
            row["freshness"]["publication_lag_days"],
            row["freshness"]["rule"],
        )
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in timing_cells)
            + " |"
        )
    declared_gap_sources = [
        row for row in sources if isinstance(row.get("declared_gap_policy"), dict)
    ]
    lines.extend(
        [
            "",
            "## Declared source-gap policies",
            "",
            "These are source omissions admitted by recorded authority, not filled values or inferred calendar closures. A first post-gap endpoint is retained as an observation but cannot produce a one-day return.",
            "",
            "| Source | Policy | Authority | Missing source dates | Omitted spanning-return dates | Return transform | Produced facts | Return rule |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in declared_gap_sources:
        policy = row["declared_gap_policy"]
        return_transform = policy["return_transform"]
        gap_cells = (
            row["source_id"],
            policy["policy_id"],
            f"#{policy['authority_issue']} — {policy['provenance']}",
            ", ".join(policy["declared_missing_source_dates"]),
            ", ".join(policy["omitted_return_dates"]),
            f"{return_transform['transform']}@{return_transform['version']}",
            ", ".join(return_transform["facts"]),
            policy["return_rule"],
        )
        lines.append(
            "| "
            + " | ".join(str(value).replace("|", "\\|") for value in gap_cells)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Source readiness",
            "",
            f"- Source segments: `{len(sources)}`",
            f"- Required not ready: `{sum(row['product_requirement'] == 'REQUIRED' and row['readiness'] == 'REQUIRED_NOT_READY' for row in sources)}`",
            "- Detailed strategy × data-class × segment × consumer rows: `source-matrix.json`",
            "",
        ]
    )
    if epoch_manifest_source is not None:
        epoch_artifact = _object(
            epoch_manifest_source["artifact"], "epoch_manifest_source artifact"
        )
        epoch_schema = _object(
            epoch_artifact["schema"], "epoch_manifest_source artifact schema"
        )
        lines.extend(
            [
                "## Epoch manifest source",
                "",
                f"- Generation ID: `{epoch_manifest_source['generation_id']}`",
                f"- Generation manifest SHA-256: `{epoch_manifest_source['generation_manifest_sha256']}`",
                f"- Generation receipt SHA-256: `{epoch_manifest_source['generation_receipt_sha256']}`",
                f"- Full directory inventory SHA-256: `{epoch_manifest_source['full_directory_inventory_sha256']}`",
                f"- Artifact path: `{epoch_artifact['path']}`",
                f"- Artifact SHA-256: `{epoch_artifact['sha256']}`",
                f"- Artifact size bytes: `{epoch_artifact['size_bytes']}`",
                f"- Artifact schema: `{epoch_schema['name']}@{epoch_schema['version']}`",
                "",
            ]
        )
    summary = ("\n".join(lines)).encode("utf-8")

    matrix_rows: list[dict[str, Any]] = []
    for source in sources:
        strategy_ids = source["subjects"]["strategy_ids"] or [
            "UNRESOLVED_SUBJECT_BINDING"
        ]
        consumers = source["consumers"] or ["NO_CONSUMER"]
        for strategy_id in strategy_ids:
            version_refs = source["strategy_version"]["version_refs"]
            subject_version_ref = version_refs.get(strategy_id)
            source_version_status = source["strategy_version"]["status"]
            if source_version_status == "MIXED":
                source_version_status = (
                    "EVIDENCED" if subject_version_ref is not None else "UNRESOLVED"
                )
            for data_class in source["data_classes"]:
                for consumer in consumers:
                    matrix_rows.append(
                        {
                            "contract_sha256": digest,
                            "product_version": product["product_version"],
                            "strategy_id": strategy_id,
                            "account_scope": source["subjects"]["account_scope"],
                            "data_class": data_class,
                            "source_id": source["source_id"],
                            "source_version": source["source_version"],
                            "strategy_version_status": source_version_status,
                            "strategy_version_binding": source["strategy_version"][
                                "binding"
                            ],
                            "strategy_version_refs": (
                                {strategy_id: subject_version_ref}
                                if subject_version_ref is not None
                                else {}
                            ),
                            "authority": source["authority"],
                            "endpoint_or_artifact_selector": source[
                                "endpoint_or_artifact_selector"
                            ],
                            "acquisition_adapter": source["acquisition"]["adapter"],
                            "acquisition_mode": source["acquisition"]["mode"],
                            "auth_and_metering_class": source["acquisition"][
                                "auth_and_metering_class"
                            ],
                            "evidence_layer": source["evidence_layer"],
                            "observed_start": source["interval"]["observed_start"],
                            "observed_end": source["interval"]["observed_end"],
                            "effective_start": source["interval"]["effective_start"],
                            "effective_end": source["interval"]["effective_end"],
                            "splice_policy": source["interval"]["splice_policy"],
                            "schema": f"{source['schema']['name']}@{source['schema']['version']}",
                            "units": source["units"]["measurement"],
                            "currency": source["units"]["currency"],
                            "timezone": source["temporal"]["timezone"],
                            "calendar": source["temporal"]["calendar"],
                            "cadence": source["temporal"]["cadence"],
                            "max_age_days": source["freshness"]["max_age_days"],
                            "age_disposition": product["as_of_policy"][
                                "calculation_boundary"
                            ]["age_disposition"],
                            "publication_lag_days": source["freshness"][
                                "publication_lag_days"
                            ],
                            "freshness_rule": source["freshness"]["rule"],
                            "admission_transform": source["admission"]["transform"],
                            "admission_facts": source["admission"]["facts"],
                            "admission_dataset_disposition_policy": source[
                                "admission"
                            ].get("dataset_disposition_policy"),
                            "declared_gap_policy": source.get("declared_gap_policy"),
                            "consumer": consumer,
                            "generation_id": source["current_generation"][
                                "generation_id"
                            ],
                            "generation_sha256": source["current_generation"]["sha256"],
                            "generation_as_of": source["current_generation"]["as_of"],
                            "existence_status": source["existence_status"],
                            "capture_status": source["capture_status"],
                            "product_requirement": source["product_requirement"],
                            "readiness": source["readiness"],
                            "blockers": source["blockers"],
                            "absence_behavior": source["absence_behavior"],
                            "invalidated_outputs": source["invalidated_outputs"],
                            "owner_policy_decision": source["owner_policy_decision"],
                        }
                    )
    source_by_id = {row["source_id"]: row for row in sources}
    census_rows: list[dict[str, Any]] = []
    for sleeve in census["account_sleeves"]:
        census_assignments = [dict(item) for item in sleeve["assignments"]]
        source_ids = sleeve["source_segment_ids"]
        if not source_ids:
            census_rows.append(
                {
                    "census_sha256": accepted_census_digest,
                    "sleeve_alias": sleeve["sleeve_alias"],
                    "provider_id": sleeve["provider_id"],
                    "role": sleeve["role"],
                    "current_strategy_id": sleeve["current_strategy_id"],
                    "assignments": census_assignments,
                    "source_id": None,
                    "data_class": None,
                    "source_requirement": None,
                    "cadence": None,
                    "publication_lag_days": None,
                    "absence_behavior": None,
                    "readiness": None,
                    "current_generation": None,
                }
            )
            continue
        for source_id in source_ids:
            source = source_by_id[source_id]
            for data_class in source["data_classes"]:
                census_rows.append(
                    {
                        "census_sha256": accepted_census_digest,
                        "sleeve_alias": sleeve["sleeve_alias"],
                        "provider_id": sleeve["provider_id"],
                        "role": sleeve["role"],
                        "current_strategy_id": sleeve["current_strategy_id"],
                        "assignments": census_assignments,
                        "source_id": source_id,
                        "data_class": data_class,
                        "source_requirement": source["product_requirement"],
                        "cadence": source["temporal"]["cadence"],
                        "publication_lag_days": source["freshness"][
                            "publication_lag_days"
                        ],
                        "absence_behavior": source["absence_behavior"],
                        "readiness": source["readiness"],
                        "current_generation": source["current_generation"],
                    }
                )
    source_matrix = {
        "contract_sha256": digest,
        "census_sha256": accepted_census_digest,
        "invested_capital_policy_sha256": invested_policy_digest,
        "owner_decision_receipts": {
            decision_id: receipts[decision_id]["payload_sha256"]
            for decision_id in OWNER_DECISION_IDS
        },
        "history_corrections": history,
        "current_census": census,
        "census_rows": census_rows,
        "as_of_policy": product["as_of_policy"],
        "product_version": product["product_version"],
        "rows": matrix_rows,
    }
    if epoch_manifest_source is not None:
        source_matrix["epoch_manifest_source"] = epoch_manifest_source
    return GeneratedViews(
        summary_markdown=summary,
        source_matrix_json=canonical_json_bytes(source_matrix),
    )


def generate_views(contract: Mapping[str, Any], output_root: Path) -> dict[str, str]:
    """Write deterministic derivatives and return their identities."""

    views = render_views(contract)
    output_root.mkdir(parents=True, exist_ok=True)
    outputs = {
        "current-product.md": views.summary_markdown,
        "source-matrix.json": views.source_matrix_json,
    }
    identities: dict[str, str] = {}
    for name, payload in outputs.items():
        path = output_root / name
        path.write_bytes(payload)
        identities[name] = hashlib.sha256(payload).hexdigest()
    return identities


def check_views(contract: Mapping[str, Any], output_root: Path) -> None:
    """Fail when a checked-in derivative differs from the contract bytes."""

    expected = render_views(contract)
    paths = {
        "current-product.md": expected.summary_markdown,
        "source-matrix.json": expected.source_matrix_json,
    }
    for name, payload in paths.items():
        path = output_root / name
        if not path.is_file() or path.read_bytes() != payload:
            raise FinancialMvpContractError(
                f"generated view drift: {path}; run the generate command"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "generate",
            "check",
            "decision-digests",
            "verify-census-evidence",
            "verify-census-owner-evidence",
            "verify-history-evidence",
            "verify-owner-evidence",
        ),
    )
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_VIEW_ROOT)
    parser.add_argument("--alpaca-root", type=Path)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--fidelity-root", type=Path)
    parser.add_argument("--ibkr-root", type=Path)
    parser.add_argument("--matrixport-root", type=Path)
    parser.add_argument("--portfolio123-root", type=Path)
    parser.add_argument("--provider-ledger-root", type=Path)
    parser.add_argument("--quantbase-root", type=Path)
    parser.add_argument("--schwab-root", type=Path)
    parser.add_argument("--epoch-manifest-generation-root", type=Path)
    parser.add_argument("--epoch-manifest-generation-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    contract = load_contract(
        args.contract,
        args.registry,
        epoch_manifest_generation_root=args.epoch_manifest_generation_root,
        epoch_manifest_generation_id=args.epoch_manifest_generation_id,
    )
    if args.command == "decision-digests":
        print(
            json.dumps(
                owner_decision_receipt_digests(contract), indent=2, sort_keys=True
            )
        )
        return 0
    result: Mapping[str, object]
    if args.command == "verify-census-evidence":
        roots = {
            "ALPACA": args.alpaca_root,
            "BASELINE": args.baseline_root,
            "FIDELITY": args.fidelity_root,
            "IBKR": args.ibkr_root,
            "MATRIXPORT": args.matrixport_root,
            "PORTFOLIO123": args.portfolio123_root,
            "PROVIDER_LEDGER": args.provider_ledger_root,
            "QUANTBASE": args.quantbase_root,
            "SCHWAB": args.schwab_root,
        }
        missing = sorted(name for name, path in roots.items() if path is None)
        if missing:
            raise FinancialMvpContractError(
                f"census evidence roots were not supplied: {missing}"
            )
        result = verify_census_evidence(
            contract,
            {name: path for name, path in roots.items() if path is not None},
        )
    elif args.command == "verify-census-owner-evidence":
        census = _object(contract["current_census"], "current_census")
        reconcile_census_owner_evidence(contract, fetch_census_owner_evidence())
        result = {
            "census_sha256": census_digest(census),
            "contract_sha256": contract_digest(contract),
            "status": "VALID",
            "verified_comment_ids": [_CENSUS_OWNER_COMMENT_ID],
        }
    elif args.command == "verify-history-evidence":
        roots = {
            "MATRIXPORT": args.matrixport_root,
            "PORTFOLIO123": args.portfolio123_root,
            "PROVIDER_LEDGER": args.provider_ledger_root,
            "QUANTBASE": args.quantbase_root,
        }
        missing = sorted(name for name, path in roots.items() if path is None)
        if missing:
            raise FinancialMvpContractError(
                f"history evidence roots were not supplied: {missing}"
            )
        result = verify_history_evidence(
            contract,
            {name: path for name, path in roots.items() if path is not None},
        )
    elif args.command == "verify-owner-evidence":
        comments = fetch_owner_evidence_comments(contract)
        reconcile_owner_receipt_evidence(contract, comments)
        result = {
            "status": "VALID",
            "contract_sha256": contract_digest(contract),
            "verified_comment_ids": sorted(comments),
        }
    elif args.command == "generate":
        result = generate_views(contract, args.output_root)
    elif args.command == "check":
        check_views(contract, args.output_root)
        result = {"status": "VALID", "contract_sha256": contract_digest(contract)}
    else:
        result = {"status": "VALID", "contract_sha256": contract_digest(contract)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

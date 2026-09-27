"""Select source-bound account-manifest evidence for governed portfolio HWM."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import re
from typing import Any, Final

import portfolio_financial_mvp_contract as financial_contract
from quant_engine.core import GenerationManifest, InputSnapshot, VerificationStatus
from quant_engine.core.publication import (
    GenerationBundle,
    read_generation,
    read_generation_bundle,
)
from quant_engine.core.strict_json import strict_json_object
from quant_engine.portfolio_ledger.hwm import (
    AS_HELD_HWM_RETURN_BASIS,
    validate_as_held_hwm_policy,
)
from quant_engine.portfolio_ledger.hwm_calendar_authority import (
    derive_governed_calendar_authority,
)
from quant_engine.portfolio_ledger.hwm_governed import (
    GovernedHwmAuthorityError,
    validate_governed_hwm_calendar_authority,
)
from quant_engine.portfolio_ledger.hwm_input_builder import build_governed_hwm_inputs


account_verifier = import_module("verify_account_manifest_generation")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ARTIFACTS: Final = {
    "authority/account-assignment-crosswalk.json": (
        "account-strategy-current-epoch-crosswalk",
        "1",
    ),
    "authority/calculation-inputs.json": ("account-manifest-calculation-inputs", "4"),
    "outputs/account-strategy-manifest.json": ("account-strategy-manifest", "9"),
    "verification/independent-verification.json": (
        "account-manifest-independent-verification",
        "3",
    ),
}
_REQUIRED_VERIFIER_INVARIANTS: Final = {
    "canonical_subject_bytes",
    "exact_subject_set",
    "mapped_row_bijection",
    "selected_source_binding",
}
_EXPECTED_HWM_CONTRACT: Final = {
    "linking": "GEOMETRIC_BEGINNING_CAPITAL_WEIGHTED",
    "membership": "EFFECTIVE_FUNDED_MEMBERSHIP_ONLY",
    "capital": "INDEPENDENT_ANCHORS_WITH_NO_TERMINAL_ONLY_BACKCAST",
    "flows": "FLOW_NEUTRAL_RETURNS_UNDER_HASH_BOUND_SHARED_POLICY",
}
_FORBIDDEN_QUALITY_MARKERS: Final = (
    "assumed_stablecoin_parity",
    "stablecoin_parity",
    "parity-rate conflict",
    "parity_rate_conflict",
)

Row = dict[str, Any]
EpochKey = tuple[str, str, str, str | None]
IntervalKey = tuple[str, str, str]
_GOVERNED_MEMBERSHIP_CENSUS: Final[Path] = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-financial-mvp-source-v1.json"
)


@dataclass(frozen=True)
class _SelectedHwmSourceRows:
    """Verified selected-source rows shared by strict build and calendar derivation."""

    artifact_sha256s: Mapping[str, str]
    calendar: Row
    memberships: list[Row]
    capital: list[Row]
    policy_bytes: bytes
    receipt_bytes: bytes
    returns: list[Row]
    account_generation_id: str
    source_generation_id: str
    source_generation_sha256: str


class SelectedHwmInputError(ValueError):
    """Selected account-manifest evidence cannot support the governed HWM."""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SelectedHwmInputError(f"{field} must be an object")
    return value


def _rows(value: object, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise SelectedHwmInputError(f"{field} must be an array of objects")
    return list(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectedHwmInputError(f"{field} must be non-empty text")
    return value.strip()


def _sha(value: object, field: str) -> str:
    accepted = _text(value, field)
    if _SHA256.fullmatch(accepted) is None:
        raise SelectedHwmInputError(f"{field} must be a lower-case SHA-256")
    return accepted


def _day(value: object, field: str) -> date:
    accepted = _text(value, field)
    try:
        parsed = date.fromisoformat(accepted)
    except ValueError as exc:
        raise SelectedHwmInputError(f"{field} must be a canonical date") from exc
    if parsed.isoformat() != accepted:
        raise SelectedHwmInputError(f"{field} must be a canonical date")
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise SelectedHwmInputError(f"{field} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SelectedHwmInputError(f"{field} must be decimal text") from exc
    if not parsed.is_finite():
        raise SelectedHwmInputError(f"{field} must be finite")
    return parsed


def _canonical_sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _filesystem_path(path: Path) -> Path:
    value = Path(path)
    if os.name != "nt":
        return value
    text = str(value.absolute())
    if text.startswith("\\\\?\\"):
        return value
    if text.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{text[2:]}")
    return Path(f"\\\\?\\{text}")


def _load_object(path: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = _filesystem_path(path).read_bytes()
    return raw, strict_json_object(raw, label=label)


def _epoch_key(value: Mapping[str, Any], field: str) -> EpochKey:
    account_id = _text(value.get("account_id"), f"{field}.account_id")
    strategy_id = _text(value.get("strategy_id"), f"{field}.strategy_id")
    effective_start = _day(
        value.get("effective_start"), f"{field}.effective_start"
    ).isoformat()
    raw_end = value.get("effective_end")
    effective_end = (
        None if raw_end is None else _day(raw_end, f"{field}.effective_end").isoformat()
    )
    if effective_end is not None and effective_end < effective_start:
        raise SelectedHwmInputError(f"{field} has a reversed effective interval")
    return account_id, strategy_id, effective_start, effective_end


def _validate_product_contract(
    contract_bytes: bytes,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    outcome_rows = _rows(contract.get("financial_outcomes"), "financial outcomes")
    matches = [
        row for row in outcome_rows if row.get("outcome_id") == "AS_HELD_COMPOSITE_HWM"
    ]
    if len(matches) != 1:
        raise SelectedHwmInputError("financial contract HWM outcome must be unique")
    calculation_contract = _mapping(
        matches[0].get("calculation_contract"), "HWM calculation contract"
    )
    for key, expected in _EXPECTED_HWM_CONTRACT.items():
        if calculation_contract.get(key) != expected:
            raise SelectedHwmInputError(f"financial contract HWM {key} differs")

    calculation = _mapping(policy.get("calculation"), "HWM policy calculation")
    if calculation.get("return_basis") != AS_HELD_HWM_RETURN_BASIS:
        raise SelectedHwmInputError("HWM policy return basis differs")
    if calculation.get("method") != "GEOMETRIC_BEGINNING_CAPITAL_WEIGHTED_AS_HELD":
        raise SelectedHwmInputError("HWM policy calculation method differs")
    if calculation.get("weight_basis") != "BEGINNING_NET_NAV":
        raise SelectedHwmInputError("HWM policy weight basis differs")

    raw_sha = hashlib.sha256(contract_bytes).hexdigest()
    semantic_sha = financial_contract.contract_digest(contract)
    product_authority = _mapping(
        policy.get("product_authority"), "HWM product authority"
    )
    if product_authority.get("raw_sha256") != raw_sha:
        raise SelectedHwmInputError("HWM policy raw product authority differs")
    if product_authority.get("semantic_digest") != semantic_sha:
        raise SelectedHwmInputError("HWM policy semantic product authority differs")
    if receipt.get("admission_gate_status") != "PASS":
        raise SelectedHwmInputError("provider admission gate is not PASS")
    if receipt.get("contract_raw_sha256") != raw_sha:
        raise SelectedHwmInputError("provider admission raw contract binding differs")
    if receipt.get("contract_semantic_sha256") != semantic_sha:
        raise SelectedHwmInputError(
            "provider admission semantic contract binding differs"
        )


def _source_generation_from_path(
    path: Path, field: str
) -> tuple[GenerationManifest, Mapping[str, bytes], bytes]:
    generation_path = _filesystem_path(path)
    if generation_path.name != "generation.json":
        raise SelectedHwmInputError(f"{field} must name generation.json")
    directory = generation_path.parent
    root = generation_path.parents[2]
    # Bundle presence is authoritative: a damaged bundle cannot fall back to
    # the ordinary source-producer layout after a verification failure.
    if any(
        (directory / name).exists() or (directory / name).is_symlink()
        for name in ("generation-inventory.json", "generation-receipt.json")
    ):
        bundle = read_generation_bundle(root, directory.name)
        manifest, payloads = bundle.manifest, bundle.payloads
    else:
        manifest = read_generation(root, directory.name)
        if any(
            artifact.artifact_id == "verification/independent-verification.json"
            for artifact in manifest.artifacts
        ):
            raise SelectedHwmInputError(f"{field} bundle sidecars are absent")
        layouts = {
            "provider-ledger": (
                "quant-engine-provider-ledger",
                {
                    "provider-ledger.json": ("provider-ledger-catalog", "2"),
                    "admit-or-declare-receipt.json": (
                        "provider-admission-receipt",
                        "1",
                    ),
                },
            ),
            "baseline": (
                "portfolio-baseline-builder",
                {"portfolio-baseline.json": ("portfolio-baseline-catalog", "1")},
            ),
        }
        if field not in layouts:
            raise SelectedHwmInputError(f"{field} source layout is unsupported")
        adapter_name, files = layouts[field]
        if manifest.adapter.name != adapter_name:
            raise SelectedHwmInputError(f"{field} source producer differs")
        entries = list(directory.iterdir())
        if {entry.name for entry in entries} != {"generation.json", *files} or any(
            entry.is_symlink() or not entry.is_file() for entry in entries
        ):
            raise SelectedHwmInputError(f"{field} ordinary source files differ")
        artifacts = {item.artifact_id: item for item in manifest.artifacts}
        payloads = {}
        for filename, schema in files.items():
            artifact_id = f"normalized/{filename}"
            artifact = artifacts.get(artifact_id)
            if (
                artifact is None
                or (artifact.schema_name, artifact.schema_version) != schema
            ):
                raise SelectedHwmInputError(f"{field} source artifact schema differs")
            payload = (directory / filename).read_bytes()
            if (
                hashlib.sha256(payload).hexdigest() != artifact.sha256
                or len(payload) != artifact.size_bytes
            ):
                raise SelectedHwmInputError(f"{field} source artifact identity differs")
            payloads[artifact_id] = payload
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise SelectedHwmInputError(f"{field} generation is not VERIFIED")
    manifest_bytes = generation_path.read_bytes()
    if manifest_bytes != manifest.to_json_bytes():
        raise SelectedHwmInputError(
            f"{field} manifest bytes differ from verified generation"
        )
    return manifest, payloads, manifest_bytes


def _snapshot_from_selected_generation(
    *,
    manifest: GenerationManifest,
    payloads: Mapping[str, bytes],
    manifest_bytes: bytes,
    selected: Mapping[str, Any],
    field: str,
) -> tuple[InputSnapshot, InputSnapshot]:
    generation_id = _sha(selected.get("generation_id"), f"{field} generation_id")
    if manifest.generation_id != generation_id:
        raise SelectedHwmInputError(f"{field} selected generation differs")
    artifact_descriptor = _mapping(
        selected.get("artifact"), f"{field} selected artifact"
    )
    artifact_id = _text(artifact_descriptor.get("artifact_id"), f"{field} artifact_id")
    artifacts = {artifact.artifact_id: artifact for artifact in manifest.artifacts}
    artifact = artifacts.get(artifact_id)
    if artifact is None or artifact_id not in payloads:
        raise SelectedHwmInputError(f"{field} selected artifact is absent")
    payload = payloads[artifact_id]
    if (
        artifact_descriptor.get("sha256") != artifact.sha256
        or artifact_descriptor.get("size_bytes") != artifact.size_bytes
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
        or len(payload) != artifact.size_bytes
    ):
        raise SelectedHwmInputError(f"{field} selected artifact identity differs")
    manifest_descriptor = _mapping(
        selected.get("generation_manifest"), f"{field} generation manifest"
    )
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_descriptor.get("sha256") != manifest_sha or manifest_descriptor.get(
        "size_bytes"
    ) != len(manifest_bytes):
        raise SelectedHwmInputError(f"{field} selected manifest identity differs")
    return (
        InputSnapshot.from_bytes(field, payload),
        InputSnapshot.from_bytes(f"{field} manifest", manifest_bytes),
    )


def _identity_snapshot(
    path: Path,
    identity: Mapping[str, Any],
    field: str,
) -> InputSnapshot:
    payload = _filesystem_path(path).read_bytes()
    if identity.get("sha256") != hashlib.sha256(payload).hexdigest() or identity.get(
        "size_bytes"
    ) != len(payload):
        raise SelectedHwmInputError(f"{field} identity differs")
    return InputSnapshot.from_bytes(field, payload)


def _verify_account_manifest_replay(
    bundle: GenerationBundle,
    *,
    account_manifest_admission_policy_path: Path,
    account_manifest_flow_classification_policy_path: Path,
    provider_ledger_generation_path: Path,
    baseline_generation_path: Path,
    membership_census_path: Path | None = None,
) -> tuple[str, str, GenerationManifest]:
    payloads = bundle.payloads
    selected = strict_json_object(
        payloads["authority/selected-input-identities.json"],
        label="selected account-manifest source identities",
    )
    # The governed membership census is a repository source contract.  Without
    # an explicit census path the repository contract is bound against the
    # identity this generation selected, failing closed on any mismatch.
    census_path = (
        _GOVERNED_MEMBERSHIP_CENSUS
        if membership_census_path is None
        else membership_census_path
    )
    membership_census = _identity_snapshot(
        census_path,
        _mapping(selected.get("membership_census"), "membership census identity"),
        "membership census",
    )
    report_bytes = payloads["verification/independent-verification.json"]
    strict_json_object(report_bytes, label="independent account-manifest report")

    provider_source, provider_payloads, provider_manifest_bytes = (
        _source_generation_from_path(provider_ledger_generation_path, "provider-ledger")
    )
    baseline_source, baseline_payloads, baseline_manifest_bytes = (
        _source_generation_from_path(baseline_generation_path, "baseline")
    )
    provider_selected = _mapping(
        selected.get("provider_ledger"), "selected provider ledger"
    )
    baseline_selected = _mapping(selected.get("baseline"), "selected baseline")
    provider_ledger, provider_manifest = _snapshot_from_selected_generation(
        manifest=provider_source,
        payloads=provider_payloads,
        manifest_bytes=provider_manifest_bytes,
        selected=provider_selected,
        field="provider ledger",
    )
    baseline, baseline_manifest = _snapshot_from_selected_generation(
        manifest=baseline_source,
        payloads=baseline_payloads,
        manifest_bytes=baseline_manifest_bytes,
        selected=baseline_selected,
        field="baseline",
    )

    code_sources = _mapping(selected.get("code_sources"), "selected code sources")
    tool_root = Path(__file__).parent
    epoch_producer = _identity_snapshot(
        tool_root / "account_strategy_manifest.py",
        _mapping(code_sources.get("epoch_producer"), "epoch producer identity"),
        "epoch producer",
    )
    generation_builder = _identity_snapshot(
        tool_root / "build_account_manifest_generation.py",
        _mapping(code_sources.get("generation_builder"), "generation builder identity"),
        "generation builder",
    )
    independent_verifier = _identity_snapshot(
        tool_root / "verify_account_manifest_generation.py",
        _mapping(
            code_sources.get("independent_verifier"), "independent verifier identity"
        ),
        "independent verifier",
    )

    admission_identity = _mapping(
        selected.get("admission_policy"), "admission policy identity"
    )
    admission_policy = _identity_snapshot(
        account_manifest_admission_policy_path,
        admission_identity,
        "account-manifest admission policy",
    )
    flow_classification_policy = _identity_snapshot(
        account_manifest_flow_classification_policy_path,
        _mapping(
            selected.get("flow_classification_policy"),
            "flow classification policy identity",
        ),
        "account-manifest flow classification policy",
    )
    schema_path = (
        tool_root.parent
        / "schemas"
        / "portfolio"
        / "account-strategy-manifest.schema.json"
    )
    manifest_schema = _identity_snapshot(
        schema_path,
        _mapping(selected.get("manifest_schema"), "manifest schema identity"),
        "account-manifest schema",
    )

    receipt_snapshots = {
        artifact_id: InputSnapshot.from_bytes("account retrieval receipt", payload)
        for artifact_id, payload in payloads.items()
        if artifact_id.startswith("receipt/")
    }
    verifier_inputs = account_verifier.IndependentVerifierInputs(
        provider_ledger=provider_ledger,
        provider_ledger_manifest=provider_manifest,
        baseline=baseline,
        baseline_manifest=baseline_manifest,
        flow_classification_policy=flow_classification_policy,
        retrieval_receipts=receipt_snapshots,
        epoch_producer_source=epoch_producer,
        generation_builder_source=generation_builder,
        independent_verifier_source=independent_verifier,
        current_epoch_crosswalk=InputSnapshot.from_bytes(
            "current epoch crosswalk",
            payloads["authority/account-assignment-crosswalk.json"],
        ),
        receipt_authority=InputSnapshot.from_bytes(
            "receipt authority",
            payloads["authority/account-interval-retrieval-receipt-authority.json"],
        ),
        admission_policy=admission_policy,
        manifest_schema=manifest_schema,
        membership_census=membership_census,
    )
    subjects = {
        artifact_id: payload
        for artifact_id, payload in payloads.items()
        if artifact_id != "verification/independent-verification.json"
    }
    account_verifier.IndependentAccountManifestVerifier().verify_finalized_report(
        verifier_inputs,
        subjects,
        report_bytes,
    )
    provider_manifest_identity = _mapping(
        provider_selected.get("generation_manifest"),
        "selected provider generation manifest",
    )
    return (
        _sha(provider_selected.get("generation_id"), "provider ledger generation_id"),
        _sha(provider_manifest_identity.get("sha256"), "provider manifest sha256"),
        provider_source,
    )


def _validate_provider_admission_binding(
    receipt_bytes: bytes,
    receipt: Mapping[str, Any],
    source_manifest: GenerationManifest,
    source_manifest_sha256: str,
) -> None:
    artifact = next(
        (
            item
            for item in source_manifest.artifacts
            if item.artifact_id == "normalized/admit-or-declare-receipt.json"
        ),
        None,
    )
    if artifact is not None:
        if (artifact.schema_name, artifact.schema_version) != (
            "provider-admission-receipt",
            "1",
        ):
            raise SelectedHwmInputError("provider admission receipt schema differs")
        if (
            hashlib.sha256(receipt_bytes).hexdigest() != artifact.sha256
            or len(receipt_bytes) != artifact.size_bytes
        ):
            raise SelectedHwmInputError("provider admission receipt identity differs")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("receipt_type") != "PROVIDER_ADMIT_OR_DECLARE_V1"
        ):
            raise SelectedHwmInputError("provider admission receipt schema differs")

    provider_binding = _mapping(
        receipt.get("provider_ledger", receipt), "provider admission provider ledger"
    )
    explicit_binding = any(
        key in provider_binding
        for key in ("provider_ledger_generation_id", "provider_ledger_manifest_sha256")
    )
    if artifact is None or explicit_binding:
        if (
            provider_binding.get("provider_ledger_generation_id")
            != source_manifest.generation_id
        ):
            raise SelectedHwmInputError(
                "provider admission selected provider-ledger generation differs"
            )
        if (
            provider_binding.get("provider_ledger_manifest_sha256")
            != source_manifest_sha256
        ):
            raise SelectedHwmInputError(
                "provider admission selected provider-ledger manifest differs"
            )


def _contains_forbidden_quality_marker(value: object) -> bool:
    if isinstance(value, str):
        lowered = value.casefold()
        return any(marker in lowered for marker in _FORBIDDEN_QUALITY_MARKERS)
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_quality_marker(key)
            or _contains_forbidden_quality_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_quality_marker(item) for item in value)
    return False


def _available_usd_amount(value: object, field: str) -> Decimal:
    amount = _mapping(value, field)
    if amount.get("currency") != "USD" or amount.get("status") != "AVAILABLE":
        raise SelectedHwmInputError(
            f"{field} lacks source-native USD; exact accepted FX provenance is absent"
        )
    return _decimal(amount.get("value"), f"{field}.value")


def _source_rows(
    calculation: Mapping[str, Any],
    output: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
) -> tuple[dict[EpochKey, Row], dict[EpochKey, Row], dict[EpochKey, Row]]:
    included_crosswalk = [
        dict(row)
        for row in _rows(crosswalk.get("rows"), "account crosswalk rows")
        if row.get("product_epoch_disposition") == "INCLUDED_PRODUCT_EPOCH"
    ]
    if len(included_crosswalk) != 13:
        raise SelectedHwmInputError(
            "account crosswalk must select exactly 13 product epochs"
        )
    crosswalk_by_key: dict[EpochKey, Row] = {}
    for index, row in enumerate(included_crosswalk):
        key = _epoch_key(row, f"crosswalk row {index}")
        if key in crosswalk_by_key:
            raise SelectedHwmInputError("account crosswalk epoch is duplicated")
        _text(row.get("provider"), f"crosswalk row {index}.provider")
        crosswalk_by_key[key] = row

    calculation_by_key: dict[EpochKey, Row] = {}
    for index, raw in enumerate(_rows(calculation.get("rows"), "calculation rows")):
        row = dict(raw)
        epoch = _mapping(row.get("epoch"), f"calculation row {index}.epoch")
        key = _epoch_key(epoch, f"calculation row {index}.epoch")
        if key in calculation_by_key:
            raise SelectedHwmInputError("calculation epoch is duplicated")
        if key not in crosswalk_by_key:
            raise SelectedHwmInputError(
                "calculation epoch is outside the selected crosswalk"
            )
        if row.get("provider") != crosswalk_by_key[key].get("provider"):
            raise SelectedHwmInputError(
                "calculation provider differs from selected crosswalk"
            )
        calculation_by_key[key] = row

    output_by_key: dict[EpochKey, Row] = {}
    for index, raw in enumerate(
        _rows(output.get("rows"), "account-manifest output rows")
    ):
        row = dict(raw)
        key = _epoch_key(row, f"output row {index}")
        if key in output_by_key:
            raise SelectedHwmInputError("output epoch is duplicated")
        if key not in crosswalk_by_key:
            raise SelectedHwmInputError(
                "output epoch is outside the selected crosswalk"
            )
        if row.get("provider") != crosswalk_by_key[key].get("provider"):
            raise SelectedHwmInputError(
                "output provider differs from selected crosswalk"
            )
        if row.get("status") != "OK":
            raise SelectedHwmInputError(
                "selected account-manifest output row is not OK"
            )
        output_by_key[key] = row
    if set(calculation_by_key) != set(crosswalk_by_key) or set(output_by_key) != set(
        crosswalk_by_key
    ):
        raise SelectedHwmInputError(
            "calculation, output, and crosswalk epoch sets differ"
        )
    return crosswalk_by_key, calculation_by_key, output_by_key


def _validate_contract_epoch_strategy_perimeter(
    contract: Mapping[str, Any], crosswalk: Mapping[EpochKey, Row]
) -> None:
    required_strategy_ids = {
        _text(row.get("strategy_id"), f"financial contract strategy {index}.id")
        for index, row in enumerate(
            _rows(contract.get("strategies"), "financial contract strategies")
        )
        if row.get("product_requirement") == "REQUIRED"
    }
    selected_strategy_ids = {key[1] for key in crosswalk}
    if selected_strategy_ids != required_strategy_ids:
        raise SelectedHwmInputError(
            "selected epoch strategy perimeter differs from REQUIRED financial contract strategies"
        )


def _validated_account_links(
    key: EpochKey,
    calculation: Mapping[str, Any],
    output: Mapping[str, Any],
) -> list[Row]:
    links, _, _, _ = _validated_account_sources(key, calculation, output)
    return links


def _validated_account_sources(
    key: EpochKey,
    calculation: Mapping[str, Any],
    output: Mapping[str, Any],
) -> tuple[list[Row], dict[str, Decimal], dict[str, Decimal], dict[str, list[Row]]]:
    if _contains_forbidden_quality_marker(
        calculation
    ) or _contains_forbidden_quality_marker(output):
        raise SelectedHwmInputError(
            "selected epoch lacks source-native USD due to rejected source quality"
        )
    nav_by_date: dict[str, Decimal] = {}
    for index, observation in enumerate(
        _rows(calculation.get("nav_observations"), "NAV observations")
    ):
        if observation.get("account_id") != key[0]:
            raise SelectedHwmInputError("NAV observation account differs from epoch")
        observed_on = _day(
            observation.get("as_of_date"), "NAV observation date"
        ).isoformat()
        nav = _available_usd_amount(observation.get("nav"), f"NAV observation {index}")
        if observed_on in nav_by_date and nav_by_date[observed_on] != nav:
            raise SelectedHwmInputError("NAV observations conflict on one date")
        nav_by_date[observed_on] = nav
    if not nav_by_date:
        raise SelectedHwmInputError("selected epoch has no daily NAV observations")

    flows_by_date: dict[str, Decimal] = defaultdict(Decimal)
    # Provider fund allocations are exact-timestamp fills: the account manifest
    # links them at the beginning of their sub-period, classified flows at its end.
    beginning_flows_by_date: dict[str, Decimal] = defaultdict(Decimal)
    flow_evidence_by_date: dict[str, list[Row]] = defaultdict(list)
    for index, raw_anchor in enumerate(
        _rows(calculation.get("capital_anchors"), "capital anchors")
    ):
        anchor = dict(raw_anchor)
        if anchor.get("account_id") != key[0]:
            raise SelectedHwmInputError("capital anchor account differs from epoch")
        capital_type = anchor.get("capital_type")
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            # An opening anchor contributes no flow: the epoch's beginning
            # capital comes from the NAV series, not from this row.  Its
            # `amount` is the subscription's own source-native fact, which for
            # a stablecoin product is not USD, so demanding USD of a value this
            # branch never reads rejects the epoch over an unused field.
            continue
        if capital_type != "STRATEGY_CAPITAL_ALLOCATION":
            raise SelectedHwmInputError("capital anchor type is unsupported")
        amount = _available_usd_amount(anchor.get("amount"), f"capital anchor {index}")
        event_day = _day(anchor.get("event_date"), f"capital anchor {index}.event_date")
        recognition_day = _day(
            anchor.get("nav_recognition_date"),
            f"capital anchor {index}.nav_recognition_date",
        )
        if recognition_day < event_day:
            raise SelectedHwmInputError(
                "capital anchor recognition precedes economic event"
            )
        recognition_text = recognition_day.isoformat()
        flows_by_date[recognition_text] += amount
        beginning_flows_by_date[recognition_text] += amount
        flow_evidence_by_date[recognition_text].append(anchor)

    flow_by_identity: dict[str, tuple[str, str, Decimal]] = {}
    for index, raw_flow in enumerate(
        _rows(calculation.get("classified_flows"), "classified flows")
    ):
        flow = dict(raw_flow)
        if flow.get("account_id") != key[0]:
            raise SelectedHwmInputError("classified flow account differs from epoch")
        amount = _available_usd_amount(flow.get("amount"), f"classified flow {index}")
        if flow.get("classification_status") != "CLASSIFIED":
            raise SelectedHwmInputError("selected epoch contains an unclassified flow")
        identity = _text(
            flow.get("event_id", flow.get("source_record_id")),
            f"classified flow {index}.identity",
        )
        event_day = _day(flow.get("event_date"), f"classified flow {index}.event_date")
        recognition_day = _day(
            flow.get("nav_recognition_date", flow.get("event_date")),
            f"classified flow {index}.nav_recognition_date",
        )
        if recognition_day < event_day:
            raise SelectedHwmInputError(
                "classified flow recognition precedes economic event"
            )
        signature = (event_day.isoformat(), recognition_day.isoformat(), amount)
        prior = flow_by_identity.get(identity)
        if prior is not None:
            if prior != signature:
                raise SelectedHwmInputError("classified flows conflict on one identity")
            raise SelectedHwmInputError("classified flow identity is duplicated")
        flow_by_identity[identity] = signature
        recognition_text = recognition_day.isoformat()
        flows_by_date[recognition_text] += amount
        flow_evidence_by_date[recognition_text].append(flow)

    trace = _mapping(output.get("observation_trace"), "observation trace")
    links: list[Row] = []
    seen: set[tuple[str, str]] = set()
    previous_end: date | None = None
    for index, raw_link in enumerate(
        _rows(trace.get("links"), "observation trace links")
    ):
        link = dict(raw_link)
        start = _day(link.get("start_date"), f"trace link {index}.start_date")
        end = _day(link.get("end_date"), f"trace link {index}.end_date")
        if end <= start:
            raise SelectedHwmInputError("observation trace interval is empty")
        if start < _day(key[2], "epoch effective_start"):
            continue
        if key[3] is not None and end > _day(key[3], "epoch effective_end"):
            continue
        interval = (start.isoformat(), end.isoformat())
        if interval in seen:
            raise SelectedHwmInputError("observation trace interval is duplicated")
        if previous_end is not None and start < previous_end:
            raise SelectedHwmInputError("observation trace intervals overlap")
        seen.add(interval)
        previous_end = end
        base = _decimal(link.get("base_nav"), f"trace link {index}.base_nav")
        ending = _decimal(link.get("ending_nav"), f"trace link {index}.ending_nav")
        net_flow = _decimal(link.get("net_flow"), f"trace link {index}.net_flow")
        factor = _decimal(link.get("factor"), f"trace link {index}.factor")
        if base <= 0 or factor <= 0:
            raise SelectedHwmInputError(
                "trace beginning NAV and factor must be positive"
            )
        if nav_by_date.get(start.isoformat()) != base:
            raise SelectedHwmInputError("trace base NAV differs from daily source NAV")
        if nav_by_date.get(end.isoformat()) != ending:
            raise SelectedHwmInputError(
                "trace ending NAV differs from daily source NAV"
            )
        native_net_flow = sum(
            (
                amount
                for recognition_day, amount in flows_by_date.items()
                if start.isoformat() < recognition_day <= end.isoformat()
            ),
            Decimal("0"),
        )
        if net_flow != native_net_flow:
            raise SelectedHwmInputError(
                "trace net flow differs from classified dated source flows"
            )
        native_beginning_flow = sum(
            (
                amount
                for recognition_day, amount in beginning_flows_by_date.items()
                if start.isoformat() < recognition_day <= end.isoformat()
            ),
            Decimal("0"),
        )
        beginning_flow = _decimal(
            link.get("beginning_flow"), f"trace link {index}.beginning_flow"
        )
        ending_flow = _decimal(
            link.get("ending_flow"), f"trace link {index}.ending_flow"
        )
        if (
            beginning_flow != native_beginning_flow
            or ending_flow != native_net_flow - native_beginning_flow
        ):
            raise SelectedHwmInputError(
                "trace flow timing differs from classified dated source flows"
            )
        # The manifest links every epoch through the portfolio return engine:
        # factor = (V_end - F_end_of_day) / (V_begin + F_beginning_of_day).
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            expected_factor = (ending - ending_flow) / (base + beginning_flow)
        if factor != expected_factor:
            raise SelectedHwmInputError(
                "trace factor differs from the canonical 40-digit source NAV calculation"
            )
        links.append(link)
    if not links:
        raise SelectedHwmInputError("selected epoch has no observation-linked returns")
    return links, nav_by_date, dict(flows_by_date), dict(flow_evidence_by_date)


def _fraction_decimal(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _derive_builder_rows(
    crosswalk: Mapping[EpochKey, Row],
    calculation: Mapping[EpochKey, Row],
    output: Mapping[EpochKey, Row],
    *,
    account_generation_id: str,
    source_generation_id: str,
    source_generation_sha256: str,
    artifact_sha256s: Mapping[str, str],
) -> tuple[list[Row], list[Row], list[Row]]:
    links_by_epoch: dict[EpochKey, dict[tuple[str, str], Row]] = {}
    nav_by_epoch: dict[EpochKey, dict[str, Decimal]] = {}
    flows_by_epoch: dict[EpochKey, dict[str, Decimal]] = {}
    flow_evidence_by_epoch: dict[EpochKey, dict[str, list[Row]]] = {}
    intervals_by_strategy: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for key in sorted(output):
        links, navs, flows, flow_evidence = _validated_account_sources(
            key, calculation[key], output[key]
        )
        epoch_start = date.fromisoformat(key[2])
        epoch_end = date.fromisoformat(key[3]) if key[3] is not None else None
        link_index = {
            (row["start_date"], row["end_date"]): row
            for row in links
            if date.fromisoformat(row["start_date"]) >= epoch_start
            and (epoch_end is None or date.fromisoformat(row["end_date"]) <= epoch_end)
        }
        links_by_epoch[key] = link_index
        nav_by_epoch[key] = navs
        flows_by_epoch[key] = flows
        flow_evidence_by_epoch[key] = flow_evidence
        intervals_by_strategy[key[1]].update(link_index)

    memberships: list[Row] = []
    capital_candidates: dict[
        tuple[str, str], dict[str, tuple[Decimal, tuple[EpochKey, ...]]]
    ] = defaultdict(dict)
    returns: list[Row] = []
    for strategy_id in sorted(intervals_by_strategy):
        strategy_epochs = [key for key in crosswalk if key[1] == strategy_id]
        effective_start = min(key[2] for key in strategy_epochs)
        membership_source = {
            "account_manifest_generation_id": account_generation_id,
            "epochs": [
                {
                    "account_id": key[0],
                    "effective_start": key[2],
                    "effective_end": key[3],
                    "assignment_id": crosswalk[key].get("assignment_id"),
                }
                for key in sorted(strategy_epochs)
            ],
            "strategy_id": strategy_id,
        }
        memberships.append(
            {
                "strategy_id": strategy_id,
                "effective_start": effective_start,
                "evidence_ref": _canonical_sha(membership_source),
            }
        )
        # When one account is liquidated to zero, its account trace legitimately
        # spans the zero run.  Other active accounts can still supply the shorter
        # observed strategy boundaries.  At any shared start, use the earliest
        # source interval; this removes only a redundant containing span and keeps
        # a native mixed-calendar span when no shorter source interval exists.
        earliest_end_by_start: dict[str, str] = {}
        for start_text, end_text in intervals_by_strategy[strategy_id]:
            prior_end = earliest_end_by_start.get(start_text)
            if prior_end is None or end_text < prior_end:
                earliest_end_by_start[start_text] = end_text
        strategy_intervals = sorted(earliest_end_by_start.items())
        previous_end: str | None = None
        for start_text, end_text in strategy_intervals:
            if previous_end is not None and start_text < previous_end:
                raise SelectedHwmInputError("strategy source intervals overlap")
            previous_end = end_text
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
            required_epochs = [
                key
                for key in strategy_epochs
                if date.fromisoformat(key[2]) <= start
                and (key[3] is None or end <= date.fromisoformat(key[3]))
            ]
            contributors: list[
                tuple[EpochKey, Decimal, Decimal, Decimal, list[Row]]
            ] = []
            for key in required_epochs:
                starting_nav = nav_by_epoch[key].get(start_text)
                ending_nav = nav_by_epoch[key].get(end_text)
                if starting_nav is None or ending_nav is None:
                    raise SelectedHwmInputError(
                        "active strategy account lacks an exact native NAV boundary"
                    )
                interval_flow = sum(
                    (
                        amount
                        for recognition_day, amount in flows_by_epoch[key].items()
                        if start_text < recognition_day <= end_text
                    ),
                    Decimal("0"),
                )
                interval_evidence = [
                    row
                    for recognition_day, rows in flow_evidence_by_epoch[key].items()
                    if start_text < recognition_day <= end_text
                    for row in rows
                ]
                contributors.append(
                    (key, starting_nav, ending_nav, interval_flow, interval_evidence)
                )
            if not contributors:
                raise SelectedHwmInputError(
                    "strategy interval has no funded account contributors"
                )
            beginning = sum((row[1] for row in contributors), Decimal("0"))
            if beginning <= 0:
                raise SelectedHwmInputError(
                    "aggregated strategy beginning NAV must be positive"
                )
            ending = sum((row[2] for row in contributors), Decimal("0"))
            net_flow = sum((row[3] for row in contributors), Decimal("0"))
            net_return = Fraction(ending - beginning - net_flow) / Fraction(beginning)
            if net_return <= -1:
                raise SelectedHwmInputError(
                    "aggregated strategy observation factor must be positive"
                )
            evidence = {
                "account_manifest_generation_id": account_generation_id,
                "artifact_sha256s": dict(sorted(artifact_sha256s.items())),
                "contributors": [
                    {
                        "account_id": key[0],
                        "epoch_effective_start": key[2],
                        "epoch_effective_end": key[3],
                        "starting_nav": str(starting_nav),
                        "ending_nav": str(ending_nav),
                        "net_flow": str(interval_flow),
                        "flow_evidence": flow_evidence,
                    }
                    for key, starting_nav, ending_nav, interval_flow, flow_evidence in sorted(
                        contributors
                    )
                ],
                "interval_end": end_text,
                "interval_start": start_text,
                "provider_ledger_generation_id": source_generation_id,
                "provider_ledger_manifest_sha256": source_generation_sha256,
                "strategy_id": strategy_id,
            }
            returns.append(
                {
                    "strategy_id": strategy_id,
                    "interval_start": start_text,
                    "interval_end": end_text,
                    "net_return": _fraction_decimal(net_return),
                    "return_basis": AS_HELD_HWM_RETURN_BASIS,
                    "source_artifact_sha256": _canonical_sha(evidence),
                }
            )
            contributor_keys = tuple(sorted(row[0] for row in contributors))
            for boundary, observed_on, value in (
                ("START", start_text, beginning),
                ("END", end_text, ending),
            ):
                capital_key = (strategy_id, observed_on)
                prior = capital_candidates[capital_key].get(boundary)
                candidate = (value, contributor_keys)
                if prior is not None and prior != candidate:
                    raise SelectedHwmInputError(
                        "aggregated strategy NAV conflicts within an interval boundary"
                    )
                capital_candidates[capital_key][boundary] = candidate

    capital_by_key: dict[tuple[str, str], Decimal] = {}
    for capital_key, boundaries in capital_candidates.items():
        starting = boundaries.get("START")
        ending_boundary = boundaries.get("END")
        if (
            starting is not None
            and ending_boundary is not None
            and starting != ending_boundary
        ):
            observed_on = capital_key[1]
            ending_keys = set(ending_boundary[1])
            starting_keys = set(starting[1])
            added = starting_keys - ending_keys
            removed = ending_keys - starting_keys
            if any(key[2] != observed_on for key in added) or any(
                key[3] != observed_on for key in removed
            ):
                raise SelectedHwmInputError(
                    "aggregated strategy NAV conflicts without an exact membership transition"
                )
        # At an exact transition date, the closing observation includes accounts whose
        # funded epoch starts that day and excludes epochs that end that day. This is the
        # opening bracket used by the following as-held interval.
        selected = starting if starting is not None else ending_boundary
        assert selected is not None
        capital_by_key[capital_key] = selected[0]

    capital = [
        {
            "strategy_id": strategy_id,
            "date": observed_on,
            "closing_capital_usd": str(value),
        }
        for (strategy_id, observed_on), value in sorted(capital_by_key.items())
    ]
    return memberships, capital, returns


def _selected_hwm_source_rows(
    *,
    account_manifest_generation_path: Path,
    account_manifest_admission_policy_path: Path,
    account_manifest_flow_classification_policy_path: Path,
    provider_ledger_generation_path: Path,
    baseline_generation_path: Path,
    financial_contract_path: Path,
    calendar_authority_path: Path,
    provider_admission_path: Path,
    hwm_policy_path: Path,
    hwm_policy_bytes: bytes | None = None,
    membership_census_path: Path | None = None,
) -> _SelectedHwmSourceRows:
    generation_path = _filesystem_path(account_manifest_generation_path)
    if generation_path.name != "generation.json":
        raise SelectedHwmInputError(
            "account_manifest_generation_path must name generation.json"
        )
    generation_id = generation_path.parent.name
    bundle_root = generation_path.parents[2]
    bundle = read_generation_bundle(bundle_root, generation_id)
    if bundle.manifest.verification is not VerificationStatus.VERIFIED:
        raise SelectedHwmInputError("account-manifest generation is not VERIFIED")
    artifact_by_id = {
        artifact.artifact_id: artifact for artifact in bundle.manifest.artifacts
    }
    for artifact_id, schema in _REQUIRED_ARTIFACTS.items():
        artifact = artifact_by_id.get(artifact_id)
        if artifact is None:
            raise SelectedHwmInputError(
                f"account-manifest generation lacks {artifact_id}"
            )
        if (artifact.schema_name, artifact.schema_version) != schema:
            raise SelectedHwmInputError(
                f"account-manifest artifact schema differs for {artifact_id}"
            )

    contract_bytes, contract_document = _load_object(
        Path(financial_contract_path), "financial contract"
    )
    contract_version = contract_document.get("contract_schema_version")
    if contract_version == 7:
        contract = financial_contract.load_contract(
            Path(financial_contract_path),
            epoch_manifest_generation_root=bundle_root,
            epoch_manifest_generation_id=generation_id,
        )
    elif contract_version == 6:
        # The provider receipt is correctly bound to the frozen schema-v6 product
        # authority.  Validate that authority as held, then independently validate
        # the exact supplied v9 epoch bundle and crosswalk.  Requiring the provider
        # generation to bind a later epoch output would create a dependency cycle.
        contract = financial_contract.load_contract(Path(financial_contract_path))
        epoch_source = financial_contract.epoch_manifest_source_from_bundle(bundle)
        epoch_artifact = _mapping(epoch_source.get("artifact"), "epoch source artifact")
        epoch_schema = _mapping(
            epoch_artifact.get("schema"), "epoch source artifact schema"
        )
        if epoch_schema.get("version") != "9" or "crosswalk" not in epoch_source:
            raise SelectedHwmInputError(
                "HWM requires an exact verified v9 epoch manifest and crosswalk"
            )
        financial_contract.validate_epoch_manifest_source(epoch_source, bundle=bundle)
    else:
        raise SelectedHwmInputError(
            "financial contract schema version is unsupported for HWM"
        )
    # A ledger admitted under a retained input contract carries a policy
    # retargeted onto that contract, so its bytes are supplied directly rather
    # than read from the repository file.  With none supplied this reads the
    # repository policy, exactly as before.
    if hwm_policy_bytes is None:
        policy_bytes, policy = _load_object(Path(hwm_policy_path), "HWM policy")
    else:
        policy_bytes = hwm_policy_bytes
        policy = strict_json_object(hwm_policy_bytes, label="HWM policy")
    validate_as_held_hwm_policy(policy)
    calendar_bytes, calendar = _load_object(
        Path(calendar_authority_path), "calendar authority"
    )
    del calendar_bytes
    validate_governed_hwm_calendar_authority(
        calendar,
        hwm_policy_bytes=policy_bytes,
        accepted_authority_sha256=_sha(
            calendar.get("authority_evidence_sha256"),
            "calendar authority_evidence_sha256",
        ),
    )
    receipt_bytes, receipt_document = _load_object(
        Path(provider_admission_path), "provider admission"
    )
    receipt = _mapping(
        receipt_document.get("receipt", receipt_document), "provider admission receipt"
    )
    _validate_product_contract(contract_bytes, contract, policy, receipt)

    documents = {
        artifact_id: strict_json_object(bundle.payloads[artifact_id], label=artifact_id)
        for artifact_id in _REQUIRED_ARTIFACTS
    }
    source_generation_id, source_generation_sha256, source_manifest = (
        _verify_account_manifest_replay(
            bundle,
            account_manifest_admission_policy_path=(
                account_manifest_admission_policy_path
            ),
            account_manifest_flow_classification_policy_path=(
                account_manifest_flow_classification_policy_path
            ),
            provider_ledger_generation_path=provider_ledger_generation_path,
            baseline_generation_path=baseline_generation_path,
            membership_census_path=membership_census_path,
        )
    )
    _validate_provider_admission_binding(
        receipt_bytes, receipt, source_manifest, source_generation_sha256
    )
    crosswalk, calculation, output = _source_rows(
        documents["authority/calculation-inputs.json"],
        documents["outputs/account-strategy-manifest.json"],
        documents["authority/account-assignment-crosswalk.json"],
    )
    _validate_contract_epoch_strategy_perimeter(contract, crosswalk)
    artifact_sha256s = {
        artifact_id: artifact_by_id[artifact_id].sha256
        for artifact_id in _REQUIRED_ARTIFACTS
    }
    members, capital, returns = _derive_builder_rows(
        crosswalk,
        calculation,
        output,
        account_generation_id=bundle.manifest.generation_id,
        source_generation_id=source_generation_id,
        source_generation_sha256=source_generation_sha256,
        artifact_sha256s=artifact_sha256s,
    )
    return _SelectedHwmSourceRows(
        artifact_sha256s=artifact_sha256s,
        calendar=calendar,
        memberships=members,
        capital=capital,
        policy_bytes=policy_bytes,
        receipt_bytes=receipt_bytes,
        returns=returns,
        account_generation_id=bundle.manifest.generation_id,
        source_generation_id=source_generation_id,
        source_generation_sha256=source_generation_sha256,
    )


def _build_selected_hwm_inputs(
    *,
    account_manifest_generation_path: Path,
    account_manifest_admission_policy_path: Path,
    account_manifest_flow_classification_policy_path: Path,
    provider_ledger_generation_path: Path,
    baseline_generation_path: Path,
    financial_contract_path: Path,
    calendar_authority_path: Path,
    provider_admission_path: Path,
    hwm_policy_path: Path,
    hwm_policy_bytes: bytes | None = None,
    membership_census_path: Path | None = None,
) -> Row:
    source = _selected_hwm_source_rows(
        account_manifest_generation_path=account_manifest_generation_path,
        account_manifest_admission_policy_path=account_manifest_admission_policy_path,
        account_manifest_flow_classification_policy_path=(
            account_manifest_flow_classification_policy_path
        ),
        provider_ledger_generation_path=provider_ledger_generation_path,
        baseline_generation_path=baseline_generation_path,
        financial_contract_path=financial_contract_path,
        calendar_authority_path=calendar_authority_path,
        provider_admission_path=provider_admission_path,
        hwm_policy_path=hwm_policy_path,
        hwm_policy_bytes=hwm_policy_bytes,
        membership_census_path=membership_census_path,
    )
    result = build_governed_hwm_inputs(
        members=source.memberships,
        capital_observations=source.capital,
        return_observations=source.returns,
        calendar_authority_sha256=source.calendar["authority_evidence_sha256"],
        hwm_policy_sha256=hashlib.sha256(source.policy_bytes).hexdigest(),
        calendar_authority=source.calendar,
        upstream_receipt_sha256=hashlib.sha256(source.receipt_bytes).hexdigest(),
    )
    for row in result["component_periods"]:
        row["source_generation_sha256"] = source.source_generation_sha256
    result["evidence_sha256s"] = sorted(
        set(result["evidence_sha256s"])
        | {
            source.source_generation_id,
            source.source_generation_sha256,
            source.account_generation_id,
        }
        | set(source.artifact_sha256s.values())
    )
    unhashed = dict(result)
    unhashed.pop("generation_evidence_sha256", None)
    result["generation_evidence_sha256"] = _canonical_sha(unhashed)
    return result


def _accepted_calendar_topology(
    calendar: Mapping[str, Any],
) -> tuple[dict[str, str], set[IntervalKey], set[tuple[str, str, str, str]]]:
    kinds: dict[str, str] = {}
    for index, raw in enumerate(
        _rows(calendar.get("source_calendars"), "calendar source calendars")
    ):
        strategy_id = _text(
            raw.get("strategy_id"), f"calendar source {index}.strategy_id"
        )
        kind = raw.get("calendar_kind")
        if kind not in {"ALWAYS_ON", "DECLARED_SESSION"} or strategy_id in kinds:
            raise SelectedHwmInputError("accepted calendar source topology differs")
        kinds[strategy_id] = kind

    intervals: set[IntervalKey] = set()
    for index, raw in enumerate(
        _rows(calendar.get("accepted_source_intervals"), "accepted calendar intervals")
    ):
        strategy_id = _text(
            raw.get("strategy_id"), f"accepted interval {index}.strategy_id"
        )
        prior = _day(
            raw.get("prior_observation_date"), f"accepted interval {index}.prior"
        ).isoformat()
        following = _day(
            raw.get("next_observation_date"), f"accepted interval {index}.next"
        ).isoformat()
        if prior >= following or raw.get("binding_return_date") != following:
            raise SelectedHwmInputError("accepted calendar interval topology differs")
        key = (strategy_id, prior, following)
        if key in intervals:
            raise SelectedHwmInputError("accepted calendar interval is duplicated")
        intervals.add(key)

    closures: set[tuple[str, str, str, str]] = set()
    for index, raw in enumerate(
        _rows(calendar.get("closed_source_dates"), "accepted calendar closures")
    ):
        strategy_id = _text(
            raw.get("strategy_id"), f"accepted closure {index}.strategy_id"
        )
        closed = _day(raw.get("date"), f"accepted closure {index}.date").isoformat()
        prior = _day(
            raw.get("prior_observation_date"), f"accepted closure {index}.prior"
        ).isoformat()
        following = _day(
            raw.get("next_observation_date"), f"accepted closure {index}.next"
        ).isoformat()
        if not prior < closed < following:
            raise SelectedHwmInputError("accepted calendar closure topology differs")
        closure_key = (strategy_id, closed, prior, following)
        if closure_key in closures:
            raise SelectedHwmInputError("accepted calendar closure is duplicated")
        closures.add(closure_key)
    return kinds, intervals, closures


def _derive_selected_hwm_calendar(source: _SelectedHwmSourceRows) -> Row:
    """Rebind accepted calendar topology to exact currently selected source rows."""

    calendar = source.calendar
    kinds, accepted_intervals, accepted_closures = _accepted_calendar_topology(calendar)
    member_ids = {
        _text(row.get("strategy_id"), "HWM membership strategy_id")
        for row in source.memberships
    }
    if member_ids != set(kinds):
        raise SelectedHwmInputError(
            "selected source strategy perimeter differs from accepted calendar"
        )

    capital_by_strategy: dict[str, dict[str, str]] = defaultdict(dict)
    for row in source.capital:
        strategy_id = _text(row.get("strategy_id"), "selected capital strategy_id")
        observed_on = _day(row.get("date"), "selected capital date").isoformat()
        value = _text(row.get("closing_capital_usd"), "selected capital value")
        prior = capital_by_strategy[strategy_id].get(observed_on)
        if prior is not None and prior != value:
            raise SelectedHwmInputError("selected capital observations conflict")
        capital_by_strategy[strategy_id][observed_on] = value

    returns_by_interval: dict[IntervalKey, Row] = {}
    for row in source.returns:
        strategy_id = _text(row.get("strategy_id"), "selected return strategy_id")
        prior = _day(
            row.get("interval_start"), "selected return interval_start"
        ).isoformat()
        following = _day(
            row.get("interval_end"), "selected return interval_end"
        ).isoformat()
        key = (strategy_id, prior, following)
        if key in returns_by_interval:
            raise SelectedHwmInputError("selected source return interval is duplicated")
        returns_by_interval[key] = row

    if set(capital_by_strategy) != set(kinds):
        raise SelectedHwmInputError("selected source calendar perimeter differs")
    strategy_calendars: list[Row] = []
    for strategy_id in sorted(kinds):
        capital = capital_by_strategy[strategy_id]
        observed = sorted(capital)
        if len(observed) < 2:
            raise SelectedHwmInputError("selected source calendar lacks observations")
        declared_intervals: list[Row] = []
        for prior, following in zip(observed, observed[1:]):
            if (date.fromisoformat(following) - date.fromisoformat(prior)).days <= 1:
                continue
            key = (strategy_id, prior, following)
            if key in accepted_intervals:
                source_return = returns_by_interval.get(key)
                if source_return is None:
                    raise SelectedHwmInputError(
                        "accepted calendar interval lacks a selected source return"
                    )
                declared_intervals.append(
                    {
                        "prior_observation_date": prior,
                        "next_observation_date": following,
                        "binding_return_date": following,
                        "binding_return_value": _text(
                            source_return.get("net_return"),
                            "selected interval net_return",
                        ),
                    }
                )
                continue
            cursor = date.fromisoformat(prior) + timedelta(days=1)
            while cursor < date.fromisoformat(following):
                if (
                    strategy_id,
                    cursor.isoformat(),
                    prior,
                    following,
                ) not in accepted_closures:
                    raise SelectedHwmInputError(
                        "selected source has an unapproved calendar gap"
                    )
                cursor += timedelta(days=1)
        strategy_calendars.append(
            {
                "strategy_id": strategy_id,
                "calendar_kind": kinds[strategy_id],
                "observed_dates": observed,
                "closing_capital_usd_by_date": capital,
                **(
                    {"accepted_intervals": declared_intervals}
                    if declared_intervals
                    else {}
                ),
            }
        )

    policy_sha = hashlib.sha256(source.policy_bytes).hexdigest()
    if calendar.get("hwm_policy_sha256") != policy_sha:
        raise SelectedHwmInputError("accepted calendar HWM policy binding differs")
    try:
        derived = derive_governed_calendar_authority(
            strategy_calendars=strategy_calendars,
            calendar_policy_id=_text(
                calendar.get("calendar_policy_id"), "calendar policy id"
            ),
            owner_decision_evidence=_text(
                calendar.get("owner_decision_evidence"),
                "calendar owner decision evidence",
            ),
            hwm_policy_sha256=policy_sha,
        )
        validate_governed_hwm_calendar_authority(
            derived,
            hwm_policy_bytes=source.policy_bytes,
            accepted_authority_sha256=_sha(
                derived.get("authority_evidence_sha256"),
                "derived calendar authority_evidence_sha256",
            ),
        )
    except GovernedHwmAuthorityError as exc:
        raise SelectedHwmInputError(str(exc)) from exc

    derived_kinds, derived_intervals, derived_closures = _accepted_calendar_topology(
        derived
    )
    if (
        derived_kinds != kinds
        or derived_intervals != accepted_intervals
        or derived_closures != accepted_closures
    ):
        raise SelectedHwmInputError(
            "derived calendar topology differs from accepted authority"
        )
    return derived


def derive_selected_hwm_calendar_authority(
    *,
    account_manifest_generation_path: Path,
    account_manifest_admission_policy_path: Path,
    account_manifest_flow_classification_policy_path: Path,
    provider_ledger_generation_path: Path,
    baseline_generation_path: Path,
    financial_contract_path: Path,
    calendar_authority_path: Path,
    provider_admission_path: Path,
    hwm_policy_path: Path,
    hwm_policy_bytes: bytes | None = None,
    membership_census_path: Path | None = None,
) -> Row:
    """Derive the governed HWM calendar for the exact selected source generation."""

    try:
        source = _selected_hwm_source_rows(
            account_manifest_generation_path=account_manifest_generation_path,
            account_manifest_admission_policy_path=account_manifest_admission_policy_path,
            account_manifest_flow_classification_policy_path=(
                account_manifest_flow_classification_policy_path
            ),
            provider_ledger_generation_path=provider_ledger_generation_path,
            baseline_generation_path=baseline_generation_path,
            financial_contract_path=financial_contract_path,
            calendar_authority_path=calendar_authority_path,
            provider_admission_path=provider_admission_path,
            hwm_policy_path=hwm_policy_path,
            hwm_policy_bytes=hwm_policy_bytes,
            membership_census_path=membership_census_path,
        )
        return _derive_selected_hwm_calendar(source)
    except SelectedHwmInputError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SelectedHwmInputError(str(exc)) from exc


def build_selected_hwm_inputs(
    *,
    account_manifest_generation_path: Path,
    account_manifest_admission_policy_path: Path,
    account_manifest_flow_classification_policy_path: Path,
    provider_ledger_generation_path: Path,
    baseline_generation_path: Path,
    financial_contract_path: Path,
    calendar_authority_path: Path,
    provider_admission_path: Path,
    hwm_policy_path: Path,
    hwm_policy_bytes: bytes | None = None,
    membership_census_path: Path | None = None,
) -> Row:
    """Build schema-1 HWM inputs from one exact verified account-manifest generation.

    The current account-manifest authority carries native USD amounts but no accepted
    FX conversion evidence. Consequently, any selected non-USD amount fails closed.
    """

    try:
        return _build_selected_hwm_inputs(
            account_manifest_generation_path=account_manifest_generation_path,
            account_manifest_admission_policy_path=(
                account_manifest_admission_policy_path
            ),
            account_manifest_flow_classification_policy_path=(
                account_manifest_flow_classification_policy_path
            ),
            provider_ledger_generation_path=provider_ledger_generation_path,
            baseline_generation_path=baseline_generation_path,
            financial_contract_path=financial_contract_path,
            calendar_authority_path=calendar_authority_path,
            provider_admission_path=provider_admission_path,
            hwm_policy_path=hwm_policy_path,
            hwm_policy_bytes=hwm_policy_bytes,
            membership_census_path=membership_census_path,
        )
    except SelectedHwmInputError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SelectedHwmInputError(str(exc)) from exc

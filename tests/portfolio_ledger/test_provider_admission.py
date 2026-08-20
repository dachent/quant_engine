from __future__ import annotations

from datetime import date

import pytest

from quant_engine.portfolio_ledger.provider_admission import (
    AdmissionDisposition,
    AnalyticalModelStatus,
    DatasetDispositionAction,
    DatasetItemKind,
    EvidenceLayer,
    GenerationQualification,
    ProviderAdmissionError,
    ProviderDatasetDisposition,
    ProviderDatasetInventoryItem,
    ProviderFactAdmission,
    ProviderGenerationIdentity,
    SourceAnalyticalStatus,
    SourceAvailability,
    validate_dataset_dispositions,
    validate_provider_authority,
)


DAY = date(2026, 7, 31)


def test_provider_generation_identity_is_hash_bound() -> None:
    identity = ProviderGenerationIdentity(
        provider_id="ibkr",
        generation_id="a" * 64,
        manifest_sha256="b" * 64,
        catalog_sha256="c" * 64,
        qualification=GenerationQualification.ACCEPTED_PARTIAL,
    )
    assert identity.to_dict()["qualification"] == "ACCEPTED_PARTIAL"


def test_account_authority_requires_account_live_evidence() -> None:
    with pytest.raises(ProviderAdmissionError, match="ACCOUNT_LIVE"):
        ProviderFactAdmission(
            provider_id="portfolio123",
            fact_type="returns",
            evidence_layer=EvidenceLayer.PROVIDER_LIVE,
            disposition=AdmissionDisposition.AUTHORITATIVE,
            effective_start=DAY,
            effective_end=DAY,
            account_id="account-1",
        )


def test_account_live_requires_identity_and_reconciliation_proof() -> None:
    with pytest.raises(ProviderAdmissionError, match="account_id"):
        ProviderFactAdmission(
            provider_id="matrixport",
            fact_type="strategy_returns",
            evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
            disposition=AdmissionDisposition.AUTHORITATIVE,
            effective_start=DAY,
            effective_end=DAY,
            strategy_id="robuxio-crypto",
        )

    with pytest.raises(ProviderAdmissionError, match="reconciliation"):
        ProviderFactAdmission(
            provider_id="matrixport",
            fact_type="strategy_returns",
            evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
            disposition=AdmissionDisposition.AUTHORITATIVE,
            effective_start=DAY,
            effective_end=DAY,
            account_id="account-1",
            strategy_id="robuxio-crypto",
        )


def test_source_availability_is_independent_of_model_status() -> None:
    status = SourceAnalyticalStatus(
        source_availability=SourceAvailability.AVAILABLE,
        analytical_model_status=AnalyticalModelStatus.DIAGNOSTIC_ONLY,
        analytical_reason="non-positive sealed holdout predicted R-squared",
    )
    assert status.to_dict() == {
        "analytical_model_status": "DIAGNOSTIC_ONLY",
        "analytical_reason": "non-positive sealed holdout predicted R-squared",
        "source_availability": "AVAILABLE",
    }

    with pytest.raises(ProviderAdmissionError, match="source is unavailable"):
        SourceAnalyticalStatus(
            source_availability=SourceAvailability.UNAVAILABLE,
            analytical_model_status=AnalyticalModelStatus.VALIDATED,
        )


def test_overlapping_account_authorities_fail_closed() -> None:
    first = ProviderFactAdmission(
        provider_id="schwab",
        fact_type="nav",
        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
        disposition=AdmissionDisposition.AUTHORITATIVE,
        effective_start=date(2026, 1, 1),
        effective_end=DAY,
        account_id="account-1",
        account_reconciliation_sha256="d" * 64,
    )
    second = ProviderFactAdmission(
        provider_id="ibkr",
        fact_type="nav",
        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
        disposition=AdmissionDisposition.AUTHORITATIVE,
        effective_start=date(2026, 7, 1),
        effective_end=DAY,
        account_id="account-1",
        account_reconciliation_sha256="e" * 64,
    )
    with pytest.raises(ProviderAdmissionError, match="overlap"):
        validate_provider_authority((first, second))


def test_reference_evidence_may_overlap_authority() -> None:
    authority = ProviderFactAdmission(
        provider_id="ibkr",
        fact_type="returns",
        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
        disposition=AdmissionDisposition.AUTHORITATIVE,
        effective_start=date(2026, 1, 1),
        effective_end=DAY,
        account_id="account-1",
        strategy_id="p123-ai-factor",
        account_reconciliation_sha256="d" * 64,
    )
    provider = ProviderFactAdmission(
        provider_id="portfolio123",
        fact_type="returns",
        evidence_layer=EvidenceLayer.PROVIDER_LIVE,
        disposition=AdmissionDisposition.RECONCILIATION_ONLY,
        effective_start=date(2026, 1, 1),
        effective_end=DAY,
        strategy_id="p123-ai-factor",
    )
    assert len(validate_provider_authority((authority, provider))) == 2


def _inventory() -> ProviderDatasetInventoryItem:
    return ProviderDatasetInventoryItem(
        provider_id="schwab",
        item_kind=DatasetItemKind.PRODUCT_DATA_CLASS,
        item_id="ACCOUNT_NAV",
        sha256="a" * 64,
        size_bytes=11,
        schema_name="financial-mvp-source-data-class",
        schema_version="1",
    )


def _disposition(
    *, action: DatasetDispositionAction = DatasetDispositionAction.ADMIT
) -> ProviderDatasetDisposition:
    admitted = action is DatasetDispositionAction.ADMIT
    return ProviderDatasetDisposition(
        provider_id="schwab",
        item_kind=DatasetItemKind.PRODUCT_DATA_CLASS,
        item_id="ACCOUNT_NAV",
        item_sha256="a" * 64,
        action=action,
        source_segment_id="schwab-account-generation",
        source_segment_sha256="b" * 64,
        authority_ref="contract:accepted#source_segments/schwab-account-generation",
        readiness_impact="ADMITTED" if admitted else "REQUIRED_NOT_READY",
        consumers=("AS_HELD_COMPOSITE_HWM",),
        invalidated_outputs=() if admitted else ("HWM",),
        evidence_layer="ACCOUNT_LIVE",
        units="PROVIDER_NATIVE_ACCOUNT_FACTS",
        currency="SOURCE_DECLARED",
        timezone="SOURCE_DECLARED",
        calendar="ACCOUNT_DECLARED",
        materialized_record_count=1 if admitted else 0,
        policy_id="PROVIDER_ADMIT_OR_DECLARE_V1",
        policy_version="1",
        policy_owner="PRODUCT_OWNER_GOVERNING_ISSUE_303",
        policy_effective_start=DAY,
        policy_effective_end=None,
        source_observed_start=DAY,
        source_observed_end=DAY,
        materialized_sha256="c" * 64 if admitted else None,
        observed_start=DAY if admitted else None,
        observed_end=DAY if admitted else None,
        data_class="ACCOUNT_NAV",
        target_facts=("nav_observations",) if admitted else (),
        transform="provider-ledger" if admitted else None,
        transform_version="1" if admitted else None,
        reason=None if admitted else "REQUIRED_DATA_CLASS_NOT_MATERIALIZED",
    )


def test_dataset_inventory_requires_one_exact_disposition() -> None:
    inventory, dispositions = validate_dataset_dispositions(
        (_inventory(),), (_disposition(),)
    )
    assert inventory[0].identity == dispositions[0].identity

    with pytest.raises(ProviderAdmissionError, match="coverage differs"):
        validate_dataset_dispositions((_inventory(),), ())

    with pytest.raises(ProviderAdmissionError, match="duplicated"):
        validate_dataset_dispositions((_inventory(),), (_disposition(), _disposition()))


def test_dataset_disposition_rejects_hash_or_exclusion_semantic_drift() -> None:
    wrong_hash = _disposition()
    object.__setattr__(wrong_hash, "item_sha256", "d" * 64)
    with pytest.raises(ProviderAdmissionError, match="hash differs"):
        validate_dataset_dispositions((_inventory(),), (wrong_hash,))

    with pytest.raises(ProviderAdmissionError, match="requires reason"):
        ProviderDatasetDisposition(
            **{
                **_disposition(action=DatasetDispositionAction.EXCLUDE).__dict__,
                "reason": None,
            }
        )

    with pytest.raises(ProviderAdmissionError, match="policy authority differs"):
        ProviderDatasetDisposition(
            **{
                **_disposition().__dict__,
                "policy_owner": "UNAUTHORIZED_POLICY_OWNER",
            }
        )

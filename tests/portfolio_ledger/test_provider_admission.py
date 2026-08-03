from __future__ import annotations

from datetime import date

import pytest

from quant_engine.portfolio_ledger.provider_admission import (
    AdmissionDisposition,
    EvidenceLayer,
    GenerationQualification,
    ProviderAdmissionError,
    ProviderFactAdmission,
    ProviderGenerationIdentity,
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


def test_overlapping_account_authorities_fail_closed() -> None:
    first = ProviderFactAdmission(
        provider_id="schwab",
        fact_type="nav",
        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
        disposition=AdmissionDisposition.AUTHORITATIVE,
        effective_start=date(2026, 1, 1),
        effective_end=DAY,
        account_id="account-1",
    )
    second = ProviderFactAdmission(
        provider_id="ibkr",
        fact_type="nav",
        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
        disposition=AdmissionDisposition.AUTHORITATIVE,
        effective_start=date(2026, 7, 1),
        effective_end=DAY,
        account_id="account-1",
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
        strategy_id="p123-ai-factor",
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

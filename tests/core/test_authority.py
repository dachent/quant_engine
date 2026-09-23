from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
import hashlib

import pytest

from quant_engine.core import (
    AdapterIdentity,
    AuthorityCatalog,
    AuthorityRule,
    CoverageClaim,
    CoverageStatus,
    EvidenceLayer,
    FactKey,
    FactSlice,
    FinalizationState,
    FreshnessAssessment,
    FreshnessMode,
    FreshnessPolicy,
    FreshnessState,
    GenerationEvidence,
    GenerationManifest,
    ManifestValidationError,
    PublicationDisposition,
    RevisionPolicy,
    RevisionState,
    SourceArtifact,
    SubjectType,
    VerificationStatus,
    decide_admission,
    local_freshness_alerts,
)


OBSERVED_END = date(2026, 7, 31)
COMMIT = "a" * 40


def _manifest(
    coverage: CoverageStatus = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
) -> GenerationManifest:
    gaps = (
        () if coverage is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY else ("fx",)
    )
    artifact = SourceArtifact(
        artifact_id="normalized/nav.csv",
        sha256=hashlib.sha256(b"synthetic-nav\n").hexdigest(),
        size_bytes=len(b"synthetic-nav\n"),
        as_of_date=OBSERVED_END,
        schema_name="synthetic-nav",
        schema_version="1",
    )
    return GenerationManifest.build(
        as_of_date=OBSERVED_END,
        adapter=AdapterIdentity("synthetic-provider", "1.0.0"),
        coverage=CoverageClaim(
            status=coverage,
            boundary="synthetic NAV source",
            fact_types=("nav",),
            observed_start=date(2026, 1, 1),
            observed_end=OBSERVED_END,
            known_gaps=gaps,
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )


def _evidence(
    manifest: GenerationManifest,
    *,
    disposition: PublicationDisposition = PublicationDisposition.CURRENT,
) -> GenerationEvidence:
    return GenerationEvidence(
        generation_id=manifest.generation_id,
        manifest_sha256=hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        acquired_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        provider_revision_id="provider-run-2026-07-31",
        adapter_commit=COMMIT,
        revision_policy=RevisionPolicy.RESTATABLE,
        disposition=disposition,
    )


def _fact(
    *,
    layer: EvidenceLayer = EvidenceLayer.ACCOUNT_LIVE,
    coverage: CoverageStatus = CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
    gaps: tuple[str, ...] = (),
) -> FactSlice:
    return FactSlice(
        key=FactKey(
            provider_id="synthetic",
            subject_type=SubjectType.ACCOUNT,
            subject_id="account-internal-1",
            fact_type="nav",
        ),
        effective_start=date(2026, 1, 1),
        effective_end=OBSERVED_END,
        evidence_layer=layer,
        finalization_state=FinalizationState.FINAL,
        coverage_status=coverage,
        known_gaps=gaps,
    )


def _catalog(
    *,
    layer: EvidenceLayer = EvidenceLayer.ACCOUNT_LIVE,
    allow_partial: bool = False,
    allowed_gaps: tuple[str, ...] = (),
    allow_non_current: bool = False,
    mode: FreshnessMode = FreshnessMode.RECURRING,
) -> AuthorityCatalog:
    freshness = (
        FreshnessPolicy(policy_id="finite-history", mode=mode)
        if mode is FreshnessMode.FINITE
        else FreshnessPolicy(
            policy_id="daily-current",
            mode=mode,
            expected_interval_days=1,
            grace_days=1,
            stale_after_days=5,
        )
    )
    return AuthorityCatalog(
        rules=(
            AuthorityRule(
                authority_id="synthetic-account-nav",
                consumer_id="portfolio-ledger",
                key=_fact().key,
                effective_start=date(2026, 1, 1),
                effective_end=None,
                evidence_layers=(layer,),
                finalization_state=FinalizationState.FINAL,
                freshness_policy_id=freshness.policy_id,
                allow_partial=allow_partial,
                allowed_gap_codes=allowed_gaps,
                allow_non_current=allow_non_current,
            ),
        ),
        freshness_policies=(freshness,),
    )


def test_authority_catalog_bytes_round_trip_deterministically() -> None:
    catalog = _catalog()

    encoded = catalog.to_json_bytes()

    assert AuthorityCatalog.from_json_bytes(encoded).to_json_bytes() == encoded


def test_generation_evidence_is_hash_bound_and_round_trips() -> None:
    manifest = _manifest()
    evidence = _evidence(manifest)

    evidence.verify_manifest(manifest)
    assert GenerationEvidence.from_dict(evidence.to_dict()) == evidence

    with pytest.raises(ManifestValidationError, match="manifest_sha256"):
        replace(evidence, manifest_sha256="0" * 64).verify_manifest(manifest)


def test_complete_current_account_fact_is_admitted() -> None:
    manifest = _manifest()

    decision = decide_admission(
        manifest=manifest,
        evidence=_evidence(manifest),
        fact=_fact(),
        catalog=_catalog(),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2026, 8, 2),
    )

    assert decision.admitted
    assert decision.reasons == ()
    assert decision.freshness_state is FreshnessState.FRESH
    assert decision.next_expected_date == date(2026, 8, 1)


def test_stale_current_fact_fails_closed() -> None:
    manifest = _manifest()

    decision = decide_admission(
        manifest=manifest,
        evidence=_evidence(manifest),
        fact=_fact(),
        catalog=_catalog(),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2026, 8, 7),
    )

    assert not decision.admitted
    assert decision.freshness_state is FreshnessState.STALE
    assert decision.reasons == ("FRESHNESS_STALE",)


def test_provider_nav_cannot_masquerade_as_account_live() -> None:
    manifest = _manifest()

    decision = decide_admission(
        manifest=manifest,
        evidence=_evidence(manifest),
        fact=_fact(layer=EvidenceLayer.PROVIDER_LIVE),
        catalog=_catalog(layer=EvidenceLayer.ACCOUNT_LIVE),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2026, 8, 2),
    )

    assert not decision.admitted
    assert decision.reasons == ("EVIDENCE_LAYER_NOT_AUTHORIZED",)


def test_pinned_partial_fact_requires_exact_gap_authorization() -> None:
    manifest = _manifest(CoverageStatus.PARTIAL)
    fact = _fact(
        layer=EvidenceLayer.PROVIDER_LIVE,
        coverage=CoverageStatus.PARTIAL,
        gaps=("fx",),
    )

    rejected = decide_admission(
        manifest=manifest,
        evidence=_evidence(manifest, disposition=PublicationDisposition.PINNED_PARTIAL),
        fact=fact,
        catalog=_catalog(
            layer=EvidenceLayer.PROVIDER_LIVE,
            allow_partial=True,
            allowed_gaps=("nav",),
            allow_non_current=True,
        ),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2026, 8, 2),
    )
    admitted = decide_admission(
        manifest=manifest,
        evidence=_evidence(manifest, disposition=PublicationDisposition.PINNED_PARTIAL),
        fact=fact,
        catalog=_catalog(
            layer=EvidenceLayer.PROVIDER_LIVE,
            allow_partial=True,
            allowed_gaps=("fx",),
            allow_non_current=True,
        ),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2026, 8, 2),
    )

    assert rejected.reasons == ("PARTIAL_FACT_HAS_UNAUTHORIZED_GAPS",)
    assert admitted.admitted


def test_finite_history_remains_valid_without_recurring_freshness() -> None:
    manifest = _manifest()

    decision = decide_admission(
        manifest=manifest,
        evidence=replace(
            _evidence(manifest), revision_policy=RevisionPolicy.FINITE_STATIC
        ),
        fact=_fact(layer=EvidenceLayer.STATIC_HISTORY),
        catalog=_catalog(layer=EvidenceLayer.STATIC_HISTORY, mode=FreshnessMode.FINITE),
        consumer_id="portfolio-ledger",
        evaluation_date=date(2036, 8, 2),
    )

    assert decision.admitted
    assert decision.freshness_state is FreshnessState.FINITE_COMPLETE
    assert decision.next_expected_date is None


def test_freshness_assessment_distinguishes_all_recurring_states() -> None:
    policy = FreshnessPolicy(
        policy_id="daily-current",
        mode=FreshnessMode.RECURRING,
        expected_interval_days=1,
        grace_days=1,
        stale_after_days=5,
    )

    fresh = policy.assess(OBSERVED_END, date(2026, 8, 2))
    late = policy.assess(OBSERVED_END, date(2026, 8, 3))
    stale = policy.assess(OBSERVED_END, date(2026, 8, 6))
    unavailable = policy.assess(None, date(2026, 8, 2))

    assert fresh.state is FreshnessState.FRESH
    assert fresh.next_expected_date == date(2026, 8, 1)
    assert fresh.escalation_required is False
    assert late.state is FreshnessState.LATE
    assert late.escalation_required is True
    assert late.blocks_current_consumers is False
    assert stale.state is FreshnessState.STALE
    assert stale.blocks_current_consumers is True
    assert unavailable.state is FreshnessState.UNAVAILABLE
    assert unavailable.last_accepted_date is None
    assert [item.severity for item in local_freshness_alerts((fresh, late, stale))] == [
        "ERROR",
        "WARNING",
    ]


def test_finite_and_revised_evidence_are_visible_without_false_staleness() -> None:
    manifest = _manifest()
    revised = replace(_evidence(manifest), supersedes_generation_id="f" * 64)
    finite = FreshnessPolicy(
        policy_id="finite-history", mode=FreshnessMode.FINITE
    ).assess(OBSERVED_END, date(2036, 8, 2), revised)

    assert finite.state is FreshnessState.FINITE_COMPLETE
    assert finite.revision_state is RevisionState.REVISED
    assert finite.next_expected_date is None
    assert finite.escalation_required is False
    assert finite.acquired_at == revised.acquired_at
    assert FreshnessAssessment(**finite.__dict__) == finite


def test_overlapping_authorities_are_rejected() -> None:
    catalog = _catalog()
    duplicate = replace(catalog.rules[0], authority_id="synthetic-account-nav-second")

    with pytest.raises(ManifestValidationError, match="overlapping authority"):
        AuthorityCatalog(
            rules=(*catalog.rules, duplicate),
            freshness_policies=catalog.freshness_policies.values(),
        )


def test_unknown_or_ambiguous_authority_fails_closed() -> None:
    catalog = _catalog()
    other = replace(
        _fact(), effective_start=date(2025, 1, 1), effective_end=date(2025, 2, 1)
    )

    with pytest.raises(ManifestValidationError, match="exactly one"):
        catalog.authority_for("portfolio-ledger", other)

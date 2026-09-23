from __future__ import annotations

from datetime import date

import pytest

from quant_engine.core import EvidenceLayer
from quant_engine.portfolio_ledger import (
    LedgerValidationError,
    StrategyEvidenceVersion,
    StrategyVersion,
    StrategyVersionDisposition,
    VersionMatchStatus,
    compare_strategy_evidence,
)


HASH = "a" * 64


def _evidence(
    layer: EvidenceLayer, version: str | None = "v1"
) -> StrategyEvidenceVersion:
    return StrategyEvidenceVersion(
        strategy_id="strategy-a",
        version_ref=version,
        evidence_layer=layer,
        artifact_sha256=HASH,
        observed_start=date(2025, 1, 1),
        observed_end=date(2025, 12, 31),
    )


def test_evidenced_version_requires_complete_identity() -> None:
    version = StrategyVersion(
        strategy_id="strategy-a",
        version_ref="v1",
        disposition=StrategyVersionDisposition.EVIDENCED,
        effective_start=date(2025, 1, 1),
        effective_end=None,
        design_artifact_sha256=HASH,
        backtest_applicable=True,
        backtest_artifact_sha256="b" * 64,
        provider_entity_fingerprint="c" * 64,
        provider_configuration_sha256="d" * 64,
        assumptions_version="assumptions-1",
        cost_model_version="costs-1",
    )

    assert version.to_dict()["disposition"] == "EVIDENCED"


def test_unresolved_version_has_explicit_gap_and_no_false_reference() -> None:
    version = StrategyVersion(
        strategy_id="strategy-a",
        version_ref=None,
        disposition=StrategyVersionDisposition.UNRESOLVED,
        effective_start=None,
        effective_end=None,
        design_artifact_sha256=None,
        backtest_applicable=True,
        backtest_artifact_sha256=None,
        provider_entity_fingerprint=None,
        provider_configuration_sha256=None,
        assumptions_version=None,
        cost_model_version=None,
        gaps=("producing strategy revision has not been evidenced",),
    )

    assert version.to_dict()["disposition"] == "UNRESOLVED"
    with pytest.raises(LedgerValidationError, match="explicit gaps"):
        StrategyVersion(
            strategy_id="strategy-a",
            version_ref=None,
            disposition=StrategyVersionDisposition.UNRESOLVED,
            effective_start=None,
            effective_end=None,
            design_artifact_sha256=None,
            backtest_applicable=False,
            backtest_artifact_sha256=None,
            provider_entity_fingerprint=None,
            provider_configuration_sha256=None,
            assumptions_version=None,
            cost_model_version=None,
        )


def test_same_version_allows_model_comparison_but_not_concatenation() -> None:
    comparison = compare_strategy_evidence(
        _evidence(EvidenceLayer.BACKTEST), _evidence(EvidenceLayer.ACCOUNT_LIVE)
    )

    assert comparison.status is VersionMatchStatus.MATCHED
    assert comparison.model_vs_actual_allowed is True
    assert comparison.concatenation_allowed is False


def test_version_mismatch_and_unresolved_version_fail_closed() -> None:
    mismatch = compare_strategy_evidence(
        _evidence(EvidenceLayer.BACKTEST, "v1"),
        _evidence(EvidenceLayer.PROVIDER_LIVE, "v2"),
    )
    unresolved = compare_strategy_evidence(
        _evidence(EvidenceLayer.BACKTEST, None),
        _evidence(EvidenceLayer.ACCOUNT_LIVE, "v1"),
    )

    assert mismatch.status is VersionMatchStatus.VERSION_MISMATCH
    assert mismatch.model_vs_actual_allowed is False
    assert unresolved.status is VersionMatchStatus.UNRESOLVED_VERSION
    assert unresolved.concatenation_allowed is False


def test_same_layer_same_version_can_be_concatenated() -> None:
    comparison = compare_strategy_evidence(
        _evidence(EvidenceLayer.ACCOUNT_LIVE),
        _evidence(EvidenceLayer.ACCOUNT_LIVE),
    )

    assert comparison.concatenation_allowed is True
    assert comparison.model_vs_actual_allowed is False

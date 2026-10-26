"""Provider capability contract and registry: a synthetic provider needs no runner edit."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from quant_engine.core import CoverageClaim, CoverageStatus
from quant_engine.core.authority import FreshnessPolicy
from quant_engine.core.providers import (
    AdapterCapabilities,
    ProviderCapability,
    ProviderContractError,
    ProviderRegistry,
    contract_findings,
)

AS_OF = date(2026, 9, 17)


class SyntheticProvider:
    """Implements acquire/normalize/coverage only (like a CSV-zip provider)."""

    capabilities = AdapterCapabilities(
        "synthetic",
        frozenset(
            {
                ProviderCapability.ACQUIRE,
                ProviderCapability.NORMALIZE,
                ProviderCapability.COVERAGE,
            }
        ),
    )

    def acquire(self, plan: Mapping[str, Any]) -> Mapping[str, bytes]:
        return {"a.csv": b"x"}

    def normalize(self, artifacts: Mapping[str, bytes]) -> Mapping[str, Any]:
        return {"rows": len(artifacts)}

    def coverage(self, as_of: date) -> CoverageClaim:
        return CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic",
            fact_types=("positions",),
            observed_start=date(2026, 1, 1),
            observed_end=as_of,
            known_gaps=(),
        )


class FeeOnlyProvider:
    capabilities = AdapterCapabilities("fee-only", frozenset({ProviderCapability.FEE}))

    def fee_terms(self) -> Mapping[str, Any]:
        return {"management": {"scope": "NAV_EMBEDDED", "rate": "0.01"}}


def test_synthetic_provider_registers_and_is_discovered_by_capability() -> None:
    registry = ProviderRegistry()
    registry.register(SyntheticProvider())
    registry.register(FeeOnlyProvider())
    assert list(registry) == ["fee-only", "synthetic"] and len(registry) == 2
    assert [
        a.capabilities.provider_id
        for a in registry.with_capability(ProviderCapability.COVERAGE)
    ] == ["synthetic"]
    assert [
        a.capabilities.provider_id
        for a in registry.with_capability(ProviderCapability.FEE)
    ] == ["fee-only"]
    assert registry.with_capability(ProviderCapability.FRESHNESS) == ()
    provider = registry.get("synthetic")
    assert provider.coverage(AS_OF).observed_end == AS_OF
    assert provider.normalize(provider.acquire({})) == {"rows": 1}


def test_declared_but_unimplemented_capability_is_refused() -> None:
    class Liar:
        capabilities = AdapterCapabilities(
            "liar", frozenset({ProviderCapability.FRESHNESS})
        )

    assert contract_findings(Liar()) == ["declares FRESHNESS but does not implement it"]
    with pytest.raises(ProviderContractError, match="declares FRESHNESS"):
        ProviderRegistry().register(Liar())


def test_implemented_but_undeclared_capability_is_refused() -> None:
    class Quiet(SyntheticProvider):
        capabilities = AdapterCapabilities(
            "quiet", frozenset({ProviderCapability.ACQUIRE})
        )

    findings = contract_findings(Quiet())
    assert "implements NORMALIZE but does not declare it" in findings
    assert "implements COVERAGE but does not declare it" in findings


def test_duplicates_unknown_ids_and_missing_declarations_are_refused() -> None:
    registry = ProviderRegistry()
    registry.register(SyntheticProvider())
    with pytest.raises(ProviderContractError, match="already registered"):
        registry.register(SyntheticProvider())
    with pytest.raises(ProviderContractError, match="unknown provider"):
        registry.get("nobody")
    assert contract_findings(object()) == [
        "adapter has no AdapterCapabilities in `capabilities`"
    ]


@pytest.mark.parametrize("bad", ["", "Schwab", "1abc", "a b", "x" * 64])
def test_provider_ids_must_be_portable_lowercase(bad: str) -> None:
    with pytest.raises(ProviderContractError):
        AdapterCapabilities(bad, frozenset({ProviderCapability.FEE}))
    with pytest.raises(ProviderContractError):
        AdapterCapabilities("ok", frozenset())


def test_freshness_capability_round_trip_type() -> None:
    assert FreshnessPolicy is not None  # protocol return type stays importable

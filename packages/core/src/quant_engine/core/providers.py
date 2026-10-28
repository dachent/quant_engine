"""Provider capability protocols and registry (ARCH-PROVIDER-CONTRACT-001).

``docs/architecture/system-boundaries.md`` names five provider capabilities
(acquire, normalize, coverage, freshness, fee). They are separate protocols, not
one god-interface: a Flex-XML, HAR, CSV-zip or API provider implements only what
it can honestly do and declares it in :class:`AdapterCapabilities`. A runner asks
the :class:`ProviderRegistry` for providers by capability, so adding a provider
edits no runner file.

This module has no provider dependency and changes no existing behavior: it is the
contract the existing name-keyed dispatch (``_PROVIDER_CATALOGS`` and the
``if provider_id == ...`` chains) migrates onto, provider by provider.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import re
from typing import Any, Protocol, runtime_checkable

from quant_engine.core.authority import FreshnessPolicy
from quant_engine.core.contracts import CoverageClaim

_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


class ProviderCapability(StrEnum):
    ACQUIRE = "ACQUIRE"
    NORMALIZE = "NORMALIZE"
    COVERAGE = "COVERAGE"
    FRESHNESS = "FRESHNESS"
    FEE = "FEE"


@runtime_checkable
class AcquireProvider(Protocol):
    def acquire(self, plan: Mapping[str, Any]) -> Mapping[str, bytes]:
        """Capture source artifacts named by ``plan``; returns artifact id -> bytes."""


@runtime_checkable
class NormalizeProvider(Protocol):
    def normalize(self, artifacts: Mapping[str, bytes]) -> Mapping[str, Any]:
        """Normalize captured artifacts into the provider's ledger-ready facts."""


@runtime_checkable
class CoverageProvider(Protocol):
    def coverage(self, as_of: date) -> CoverageClaim:
        """State what the provider's data covers, and what it omits, as of a date."""


@runtime_checkable
class FreshnessProvider(Protocol):
    def freshness(self) -> FreshnessPolicy:
        """The freshness policy that applies to this provider's facts."""


@runtime_checkable
class FeeProvider(Protocol):
    def fee_terms(self) -> Mapping[str, Any]:
        """Declared fee and cost terms, each labelled with its scope."""


_PROTOCOL_BY_CAPABILITY: Mapping[ProviderCapability, type] = {
    ProviderCapability.ACQUIRE: AcquireProvider,
    ProviderCapability.NORMALIZE: NormalizeProvider,
    ProviderCapability.COVERAGE: CoverageProvider,
    ProviderCapability.FRESHNESS: FreshnessProvider,
    ProviderCapability.FEE: FeeProvider,
}


class ProviderContractError(ValueError):
    """A provider violates the capability contract or the registry rules."""


@dataclass(frozen=True)
class AdapterCapabilities:
    """What one provider declares it can do."""

    provider_id: str
    capabilities: frozenset[ProviderCapability]

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.provider_id) is None:
            raise ProviderContractError("provider_id is not a portable lowercase id")
        if not self.capabilities:
            raise ProviderContractError("a provider must declare a capability")
        if not all(isinstance(c, ProviderCapability) for c in self.capabilities):
            raise ProviderContractError("capabilities must be ProviderCapability")


def contract_findings(adapter: object) -> list[str]:
    """Shared contract check: every declared capability is actually implemented.

    The adapter exposes ``capabilities`` (an :class:`AdapterCapabilities`). A
    declared capability whose protocol methods are missing is a finding, and so is
    an implemented protocol that is not declared (an undeclared capability is one a
    runner could never discover).
    """

    declared = getattr(adapter, "capabilities", None)
    if not isinstance(declared, AdapterCapabilities):
        return ["adapter has no AdapterCapabilities in `capabilities`"]
    findings = []
    for capability, protocol in _PROTOCOL_BY_CAPABILITY.items():
        implemented = isinstance(adapter, protocol)
        if capability in declared.capabilities and not implemented:
            findings.append(f"declares {capability.value} but does not implement it")
        if implemented and capability not in declared.capabilities:
            findings.append(f"implements {capability.value} but does not declare it")
    return findings


@dataclass(frozen=True)
class ProviderDescriptor:
    """The static facts one provider contributes to the ledger (no behavior).

    ``catalog_filename`` is the file a provider generation holds its catalog in,
    ``catalog_schema_versions`` the catalog schema versions the ledger accepts,
    ``account_facts`` the sorted source fact names the ledger projects, and
    ``source_segment`` the contract source segment that governs the provider.
    """

    provider_id: str
    catalog_filename: str
    catalog_schema_versions: frozenset[int]
    account_facts: tuple[str, ...]
    source_segment: str

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.provider_id) is None:
            raise ProviderContractError("provider_id is not a portable lowercase id")
        if not self.catalog_filename or not self.source_segment:
            raise ProviderContractError("descriptor needs a catalog and a segment")
        if not self.catalog_schema_versions:
            raise ProviderContractError("descriptor needs a catalog schema version")
        if list(self.account_facts) != sorted(set(self.account_facts)):
            raise ProviderContractError("account_facts must be sorted and unique")


class ProviderRegistry:
    """Discover providers by id or capability; refuses duplicates and bad adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._descriptors: dict[str, ProviderDescriptor] = {}

    def register_descriptor(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.provider_id in self._descriptors:
            raise ProviderContractError(
                f"provider already described: {descriptor.provider_id}"
            )
        self._descriptors[descriptor.provider_id] = descriptor

    def descriptor(self, provider_id: str) -> ProviderDescriptor:
        try:
            return self._descriptors[provider_id]
        except KeyError:
            raise ProviderContractError(f"unknown provider: {provider_id}") from None

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(item for _, item in sorted(self._descriptors.items()))

    def register(self, adapter: Any) -> None:
        findings = contract_findings(adapter)
        if findings:
            raise ProviderContractError("; ".join(findings))
        provider_id = adapter.capabilities.provider_id
        if provider_id in self._adapters:
            raise ProviderContractError(f"provider already registered: {provider_id}")
        self._adapters[provider_id] = adapter

    def get(self, provider_id: str) -> Any:
        try:
            return self._adapters[provider_id]
        except KeyError:
            raise ProviderContractError(f"unknown provider: {provider_id}") from None

    def with_capability(self, capability: ProviderCapability) -> tuple[Any, ...]:
        return tuple(
            adapter
            for _, adapter in sorted(self._adapters.items())
            if capability in adapter.capabilities.capabilities
        )

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._adapters))

    def __len__(self) -> int:
        return len(self._adapters)

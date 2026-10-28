"""The ledger's name-keyed tables are views of per-provider descriptors (#533).

The golden tables below are the literal tables ``provider_ledger`` used before the
descriptors existed. Equality with them is the parity proof: every consumer of
``_PROVIDER_CATALOGS``, ``_ACCOUNT_FACTS`` and ``_PROVIDER_SOURCE_SEGMENTS`` sees
the same values, so a ledger built from the same inputs is unchanged.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from quant_engine.core import (
    ProviderContractError,
    ProviderDescriptor,
    ProviderRegistry,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_fact_admission as gate  # noqa: E402
import provider_ledger as ledger  # noqa: E402

GOLDEN_CATALOGS = {
    "alpaca": ("alpaca-generation.json", frozenset({1})),
    "fidelity": ("fidelity-generation.json", frozenset({1})),
    "ibkr": ("ibkr-catalog.json", frozenset({3, 4, 5, 6, 7})),
    "matrixport": ("catalog.json", frozenset({1, 2})),
    "portfolio123": ("portfolio123-generation.json", frozenset({1})),
    "quantbase": ("quantbase-generation.json", frozenset({1})),
    "schwab": ("schwab-generation.json", frozenset({1, 2, 3})),
}
GOLDEN_FACTS = {
    "alpaca": ("nav_observations", "positions", "transactions"),
    "fidelity": ("cash_flows", "corporate_actions", "transactions"),
    "ibkr": (
        "capital_anchors",
        "corporate_actions",
        "financing_cost_observations",
        "financing_liability_observations",
        "flows",
        "leverage_exposure_observations",
        "nav_observations",
        "positions",
        "transactions",
    ),
    "matrixport": ("capital_anchors", "nav_observations"),
    "portfolio123": (),
    "quantbase": ("capital_anchors", "flows", "nav_observations", "transactions"),
    "schwab": ("nav_observations", "positions", "transactions"),
}
GOLDEN_SEGMENTS = {
    "alpaca": "alpaca-account-generation",
    "fidelity": "fidelity-account-generation",
    "ibkr": "ibkr-account-generation",
    "matrixport": "matrixport-account-generation",
    "portfolio123": "portfolio123-model-generation",
    "quantbase": "quantbase-account-generation",
    "schwab": "schwab-account-generation",
}


def test_derived_tables_equal_the_pre_descriptor_tables() -> None:
    assert dict(ledger._PROVIDER_CATALOGS) == GOLDEN_CATALOGS  # noqa: SLF001
    assert dict(ledger._ACCOUNT_FACTS) == GOLDEN_FACTS  # noqa: SLF001
    assert dict(ledger._PROVIDER_SOURCE_SEGMENTS) == GOLDEN_SEGMENTS  # noqa: SLF001


def test_every_descriptor_is_registered_once_and_sorted() -> None:
    registry = ledger._PROVIDER_REGISTRY  # noqa: SLF001
    assert [item.provider_id for item in registry.descriptors()] == sorted(
        GOLDEN_CATALOGS
    )
    assert registry.descriptor("ibkr").catalog_filename == "ibkr-catalog.json"
    with pytest.raises(ProviderContractError, match="already described"):
        registry.register_descriptor(registry.descriptor("ibkr"))
    with pytest.raises(ProviderContractError, match="unknown provider"):
        registry.descriptor("nonexistent")


def test_a_synthetic_provider_needs_no_runner_edit() -> None:
    registry = ProviderRegistry()
    registry.register_descriptor(
        ProviderDescriptor(
            provider_id="synthetic",
            catalog_filename="synthetic.json",
            catalog_schema_versions=frozenset({1}),
            account_facts=("nav_observations",),
            source_segment="synthetic-account-generation",
        )
    )
    assert [item.provider_id for item in registry.descriptors()] == ["synthetic"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_id": "Bad Id"},
        {"catalog_filename": ""},
        {"catalog_schema_versions": frozenset()},
        {"account_facts": ("positions", "nav_observations")},
        {"account_facts": ("positions", "positions")},
    ],
)
def test_malformed_descriptors_are_refused(overrides: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "provider_id": "synthetic",
        "catalog_filename": "synthetic.json",
        "catalog_schema_versions": frozenset({1}),
        "account_facts": ("nav_observations",),
        "source_segment": "synthetic-account-generation",
    }
    with pytest.raises(ProviderContractError):
        ProviderDescriptor(**{**fields, **overrides})  # type: ignore[arg-type]


def test_fact_admission_gate_reads_the_same_filenames_as_the_ledger() -> None:
    assert gate._catalog_filenames() == {  # noqa: SLF001
        provider: name for provider, (name, _) in GOLDEN_CATALOGS.items()
    }

"""Optional statement providers: core providers always required, extras only when named.

The ledger's provider set was an exact equality; John Hancock and Composer (statement
providers) join it as OPTIONAL members so that a cycle that does not name them stays valid
and their accounts stay an explicit RETAINED_NO_ACCEPTED_PROVIDER_PATH row in the baseline
matrix, never a silent absence.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import provider_ledger as ledger  # noqa: E402

CORE = {
    "alpaca",
    "fidelity",
    "ibkr",
    "matrixport",
    "portfolio123",
    "quantbase",
    "schwab",
}


@pytest.fixture
def with_statement_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogs = {
        **ledger._PROVIDER_CATALOGS,
        "john_hancock": ("john-hancock-generation.json", frozenset({1})),
        "composer": ("composer-generation.json", frozenset({1})),
    }
    monkeypatch.setattr(ledger, "_PROVIDER_CATALOGS", catalogs)
    monkeypatch.setattr(
        ledger, "_STATEMENT_PROVIDER_IDS", frozenset({"john_hancock", "composer"})
    )
    monkeypatch.setattr(ledger, "_CORE_PROVIDER_IDS", frozenset(CORE))


def test_the_current_provider_set_is_the_core_set() -> None:
    assert ledger._CORE_PROVIDER_IDS == CORE
    assert ledger._provider_set_is_valid(CORE)
    assert ledger._selected_provider_ids(CORE) == sorted(CORE)


@pytest.mark.usefixtures("with_statement_providers")
def test_statement_providers_are_optional_extras() -> None:
    for extras in (set(), {"john_hancock"}, {"composer"}, {"john_hancock", "composer"}):
        assert ledger._provider_set_is_valid(CORE | extras)
        assert ledger._selected_provider_ids(CORE | extras) == sorted(CORE | extras)


@pytest.mark.usefixtures("with_statement_providers")
@pytest.mark.parametrize(
    "named",
    (
        sorted(CORE - {"ibkr"}),  # a core provider missing is never allowed
        sorted(
            CORE - {"ibkr"} | {"john_hancock", "composer"}
        ),  # extras do not replace core
        sorted(CORE | {"robuxio"}),  # an unknown provider is refused
        [],
    ),
)
def test_a_missing_core_or_unknown_provider_is_refused(named: list[str]) -> None:
    assert not ledger._provider_set_is_valid(named)
    with pytest.raises(
        ledger.ProviderLedgerError, match="must name every current provider"
    ):
        ledger._selected_provider_ids(named)

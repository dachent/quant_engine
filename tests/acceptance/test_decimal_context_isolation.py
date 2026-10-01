"""W20: loading the independent derivation must not change the session's decimals.

``acceptance/derive_b/derive.py`` sets ``getcontext().prec = 50`` when it is
imported. The acceptance tests load it in place with ``exec_module``, through
``test_derive_b.load_derivation_module`` and
``test_validate_generation._load_derive_b``. The decimal context is per thread and
survives from one test to the next, so a loader that leaves the precision at 50 makes
every later test of the session do its ``Decimal`` arithmetic at 50 digits, and the
172-outcome test ``tests/core/test_portfolio_mvp2_product.py::
test_one_source_set_reaches_all_172_outcomes_and_both_consumers`` fails when
``tests/acceptance`` runs before it in one session.

The oracle file stays untouched; the two test loaders save the ambient context
before the module load and restore it afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
import decimal
from decimal import Decimal
from typing import Any

import pytest
from test_derive_b import load_derivation_module
from test_validate_generation import _load_derive_b

LOADERS: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("test_derive_b.load_derivation_module", load_derivation_module),
    ("test_validate_generation._load_derive_b", _load_derive_b),
)
ONE_THIRD_AT_PRECISION_28 = "0." + "3" * 28


@pytest.mark.parametrize(("name", "loader"), LOADERS, ids=[name for name, _ in LOADERS])
def test_the_loader_leaves_the_ambient_decimal_context_unchanged(
    name: str, loader: Callable[[], Any]
) -> None:
    """W20 (fail-before): the context after the load is the context before it."""

    with decimal.localcontext(
        decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)
    ):
        before = repr(decimal.getcontext())
        module = loader()
        assert callable(module.time_weighted_return)
        assert repr(decimal.getcontext()) == before, name


@pytest.mark.parametrize(("name", "loader"), LOADERS, ids=[name for name, _ in LOADERS])
def test_decimal_arithmetic_after_the_loader_is_at_the_ambient_precision(
    name: str, loader: Callable[[], Any]
) -> None:
    """W20 (fail-before): the observable effect, 28 digits and not 50."""

    with decimal.localcontext(decimal.Context(prec=28)):
        loader()
        assert str(Decimal(1) / Decimal(3)) == ONE_THIRD_AT_PRECISION_28, name


def test_both_loaders_in_a_row_leave_the_context_unchanged() -> None:
    """W20 (fail-before): the session order that broke the 172-outcome test."""

    with decimal.localcontext(decimal.Context(prec=28)):
        before = repr(decimal.getcontext())
        load_derivation_module()
        _load_derive_b()
        load_derivation_module()
        assert repr(decimal.getcontext()) == before


def test_the_oracle_module_still_computes_at_its_own_precision() -> None:
    """Control (green on main): restoring the context does not change the oracle.

    The module's own ``getcontext().prec = 50`` runs at import and is what the
    standalone derivation uses; the tests only keep it from leaking.
    """

    module = load_derivation_module()
    with decimal.localcontext(decimal.Context(prec=50)):
        result = module.time_weighted_return(
            [
                (module.date(2026, 1, 1), Decimal("100")),
                (module.date(2026, 1, 2), Decimal("115")),
            ],
            {module.date(2026, 1, 2): Decimal("10")},
        )
    assert result == Decimal("0.05")

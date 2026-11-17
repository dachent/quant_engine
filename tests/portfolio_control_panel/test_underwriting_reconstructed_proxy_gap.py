"""A strategy with no provider comparison still quantifies its gap against the reconstructed model.

The comparator is labelled a reconstructed proxy, is used only when no provider comparison
exists, and needs a tested account-versus-model reconciliation with observations.
"""

from __future__ import annotations

import json
from typing import Any

from test_implementation_account_leg_seam import REPOSITORY_ROOT, panel

POLICY = json.loads(
    (
        REPOSITORY_ROOT / "contracts" / "portfolio-strategy-underwriting-v1.json"
    ).read_text(encoding="utf-8")
)["model_account_gap"]


def _reconciliation(**fields: Any) -> dict[str, Any]:
    return {
        "return_reconciliation": {
            "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
            "observations": 213,
            "observed_start": "2026-01-05",
            "observed_end": "2026-11-06",
            "account_cumulative_return": -0.0675,
            "model_cumulative_return": 0.12,
            "account_minus_model_cumulative": -0.1875,
            "daily_return_correlation": 0.9,
            "beta_to_model": 1.05,
            "annualized_tracking_error": 0.2,
            **fields,
        }
    }


def _strategy(**fields: Any) -> dict[str, Any]:
    return {
        "strategy_id": "syn-a",
        "strategy_name": "Synthetic A",
        "cash_drag_status": "UNAVAILABLE",
        "fees_status": "PARTIAL",
        "provider_comparison_status": "UNAVAILABLE",
        **fields,
    }


def test_the_reconstructed_proxy_quantifies_the_gap_when_no_provider_comparison_exists() -> (
    None
):
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _strategy(implementation_reconciliation=_reconciliation()), POLICY
    )

    assert gap["total_gap"] == "-0.1875"
    assert gap["status"] == "QUANTIFIED_TOTAL_CAUSAL_COMPONENTS_UNRESOLVED"
    assert gap["comparison_status"] == (
        "RECONSTRUCTED_PROXY_DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV"
    )
    assert gap["observations"] == 213
    assert (
        "REFERENCE_IS_RECONSTRUCTED_PROXY_NOT_PROVIDER_PUBLISHED"
        in gap["unresolved_causes"]
    )
    assert gap["causally_bound_components"] == []
    assert gap["unresolved_residual"] == gap["total_gap"]


def test_a_provider_comparison_takes_precedence_over_the_proxy() -> None:
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _strategy(
            provider_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
            provider_cumulative_gap="-0.05",
            provider_comparison_version_compatibility="UNRESOLVED",
            implementation_reconciliation=_reconciliation(),
        ),
        POLICY,
    )

    assert gap["total_gap"] == "-0.05"
    assert gap["comparison_status"] == "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"


def test_an_untested_or_empty_reconciliation_is_not_a_comparator() -> None:
    for fields in (
        {"status": "UNAVAILABLE_NO_COMMON_DATES"},
        {"status": None},
        {"observations": 0},
        {"observations": True},
        {"account_minus_model_cumulative": None},
        {"model_cumulative_return": None},
    ):
        gap = panel._model_account_gap_underwriting(  # noqa: SLF001
            _strategy(implementation_reconciliation=_reconciliation(**fields)),
            POLICY,
        )
        assert gap["total_gap"] is None, fields
        assert gap["status"].startswith("UNAVAILABLE"), fields


def test_no_implementation_reconciliation_leaves_the_gap_unavailable() -> None:
    gap = panel._model_account_gap_underwriting(_strategy(), POLICY)  # noqa: SLF001

    assert gap["total_gap"] is None
    assert gap["status"] == "UNAVAILABLE_NO_ACCEPTED_MODEL_OR_PROVIDER_COMPARISON"

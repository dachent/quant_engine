"""The underwriting gap of a proxy-only strategy uses the model-versus-actual replica.

Release B published VTMA two ways in one panel: the underwriting row carried the
signal-state x SPXL close proxy (+18.54 %, gap -25.29 pp) while ``model_vs_actual``
carried the hourly replica for the same strategy (+4.71 %, gap -11.46 pp).  The row now
publishes the replica figure of the primary window; the proxy rides along only as a
labelled cross-check and never changes the advisory.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_implementation_account_leg_seam import REPOSITORY_ROOT, panel
import test_model_vs_actual as mva_tests

POLICY = json.loads(
    (
        REPOSITORY_ROOT / "contracts" / "portfolio-strategy-underwriting-v1.json"
    ).read_text(encoding="utf-8")
)["model_account_gap"]
CUTOFF = "2026-11-06"


def _b_shape_strategy() -> dict[str, Any]:
    """The B shape: VTMA has no provider comparison, only the diagnostic proxy."""

    return {
        "strategy_id": "s2",
        "strategy_name": "Synthetic VTMA",
        "cash_drag_status": "EVIDENCED",
        "fees_status": "EVIDENCED_PRE_TAX",
        "provider_comparison_status": "UNAVAILABLE",
        "implementation_reconciliation": {
            "return_reconciliation": {
                "status": "DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV",
                "observations": 213,
                "observed_start": "2026-01-05",
                "observed_end": "2026-11-06",
                "account_cumulative_return": -0.0674786,
                "model_cumulative_return": 0.18537590897512324,
                "account_minus_model_cumulative": -0.2528545089751222,
                "daily_return_correlation": 0.9276774530122234,
                "beta_to_model": 0.8814588295902128,
                "annualized_tracking_error": 0.14747068147932446,
            }
        },
    }


def _audit_with_v6_window() -> tuple[bytes, str, dict[str, Any]]:
    audit = mva_tests._audit()  # noqa: SLF001
    s2 = next(s for s in audit["strategies"] if s["strategy_id"] == "s2")
    s2["primary_window_id"] = "W_full"
    s2["notes"] = [
        "Model return is a proxy range of about +/-7 pp without intraday prices."
    ]
    s2["windows"] = [
        {
            **s2["windows"][0],
            "window_id": "W_full",
            "start": "2026-01-02",
            "end": "2026-11-06",
            "account_return_pct": -6.748,
            "model_return_pct": 4.7143,
            "audit_gap_pp": -11.4623,
            "view": "ACCOUNT_VS_V6_HOURLY_REPLICA",
        }
    ]
    s2["unavailable"] = []
    raw, sha = mva_tests._raw(audit)  # noqa: SLF001
    return raw, sha, audit


def _reference() -> dict[str, Any]:
    raw, sha, _audit = _audit_with_v6_window()
    reference = panel.__dict__["_model_vs_actual_module"].model_account_gap_reference(
        raw, sha, "s2", CUTOFF
    )
    assert reference is not None
    return reference


def test_the_reference_is_the_model_vs_actual_primary_window_model_return() -> None:
    raw, sha, audit = _audit_with_v6_window()
    mva = panel.__dict__["_model_vs_actual_module"]
    reference = mva.model_account_gap_reference(raw, sha, "s2", CUTOFF)
    block = mva.build_model_vs_actual(
        {
            **mva_tests._panel(),  # noqa: SLF001
            "invested_capital": {"complete_through": CUTOFF},
        },
        raw,
        sha,
        max_window_lag_days=36500,
    )
    strategy = next(s for s in block["strategies"] if s["strategy_id"] == "s2")
    window = next(w for w in strategy["windows"] if w["window_id"] == "W_full")

    assert reference is not None
    assert reference["model_return_pct"] == pytest.approx(window["model_return_pct"])
    assert reference["account_return_pct"] == pytest.approx(
        window["account_return_pct"]
    )
    assert reference["gap"] * 100 == pytest.approx(window["account_gap_pp"], abs=1e-6)
    assert reference["uncertainty_pp"] == 7.0
    assert (reference["start"], reference["end"]) == ("2026-01-02", "2026-11-06")


def test_a_window_ending_after_the_cutoff_gives_no_reference() -> None:
    raw, sha, _ = _audit_with_v6_window()
    mva = panel.__dict__["_model_vs_actual_module"]

    assert mva.model_account_gap_reference(raw, sha, "s2", "2026-11-05") is None
    assert mva.model_account_gap_reference(raw, sha, "unknown", CUTOFF) is None


def test_the_underwriting_row_publishes_the_replica_not_the_proxy() -> None:
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _b_shape_strategy(), POLICY, _reference()
    )

    # B shape: the proxy would give +0.1854 / -0.2529; the replica gives +0.047143 / -0.114623
    assert float(gap["reference_cumulative_return"]) == pytest.approx(0.047143)
    assert float(gap["total_gap"]) == pytest.approx(-0.114623)
    assert gap["comparison_status"] == "AUDIT_REPLICA_REFERENCE_NOT_PROVIDER_PUBLISHED"
    assert (gap["observed_start"], gap["observed_end"]) == ("2026-01-02", "2026-11-06")
    assert gap["unresolved_residual"] == gap["total_gap"]
    assert gap["causally_bound_components"] == []
    assert (
        "REFERENCE_IS_RECONSTRUCTED_PROXY_NOT_PROVIDER_PUBLISHED"
        in gap["unresolved_causes"]
    )


def test_the_proxy_is_a_labelled_cross_check_and_the_range_is_stated() -> None:
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _b_shape_strategy(), POLICY, _reference()
    )

    cross = gap["proxy_cross_check"]
    assert float(cross["reference_cumulative_return"]) == pytest.approx(
        0.185376, abs=1e-6
    )
    assert float(cross["total_gap"]) == pytest.approx(-0.2528545, abs=1e-6)
    assert cross["comparison_status"] == panel.RECONSTRUCTED_PROXY_COMPARISON_STATUS
    assert gap["path_statistics_basis"] == "SIGNAL_STATE_CLOSE_PROXY_CROSS_CHECK"
    assert gap["reference_uncertainty_pp"] == 7.0
    assert "+/-7 pp" in gap["reference_note"]


def test_without_a_reference_the_proxy_stays_the_labelled_comparator() -> None:
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _b_shape_strategy(), POLICY
    )

    assert gap["comparison_status"] == panel.RECONSTRUCTED_PROXY_COMPARISON_STATUS
    assert float(gap["total_gap"]) == pytest.approx(-0.252854509)
    assert "proxy_cross_check" not in gap


def test_a_provider_published_reference_is_not_taken_as_a_replica() -> None:
    reference = {**_reference(), "provider_published": True}
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        _b_shape_strategy(), POLICY, reference
    )

    assert gap["comparison_status"] == panel.RECONSTRUCTED_PROXY_COMPARISON_STATUS


def test_a_provider_comparison_still_wins_over_the_replica() -> None:
    strategy = {
        **_b_shape_strategy(),
        "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
        "provider_cumulative_gap": "-0.05",
        "provider_comparison_version_compatibility": "UNRESOLVED",
    }
    gap = panel._model_account_gap_underwriting(  # noqa: SLF001
        strategy, POLICY, _reference()
    )

    assert gap["total_gap"] == "-0.05"
    assert gap["comparison_status"] == "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"


def test_a_comparator_without_provider_published_gives_no_reference() -> None:
    raw, sha, audit = _audit_with_v6_window()
    s2 = next(s for s in audit["strategies"] if s["strategy_id"] == "s2")
    del s2["comparator"]["provider_published"]
    raw, sha = mva_tests._raw(audit)  # noqa: SLF001
    mva = panel.__dict__["_model_vs_actual_module"]

    assert mva.model_account_gap_reference(raw, sha, "s2", CUTOFF) is None


def test_the_references_are_cut_off_by_the_blocks_own_as_of() -> None:
    raw, sha, _audit = _audit_with_v6_window()
    mva = panel.__dict__["_model_vs_actual_module"]

    def refs(complete_through: str, ids: tuple[str, ...] = ("s2",)) -> dict[str, Any]:
        return mva.model_account_gap_references(
            {"invested_capital": {"complete_through": complete_through}},
            raw,
            sha,
            ids,
        )

    assert set(refs("2026-11-06")) == {"s2"}
    # the block withholds a window ending after its as-of: no reference either
    assert refs("2026-11-05") == {}
    # only the named strategies are looked up, an unnamed one never joins
    assert refs("2026-11-06", ("s1",)) == {}
    assert refs("2026-11-06", ("unknown",)) == {}
    assert panel.REPLICA_REFERENCE_STRATEGY_IDS == ("grizzlybears-vtma",)


def test_the_references_refuse_a_wrong_pin_and_a_panel_without_an_as_of() -> None:
    raw, sha, _audit = _audit_with_v6_window()
    mva = panel.__dict__["_model_vs_actual_module"]

    with pytest.raises(mva.ModelVsActualError):
        mva.model_account_gap_references(
            {"invested_capital": {"complete_through": CUTOFF}}, raw, "0" * 64, ("s2",)
        )
    with pytest.raises(mva.ModelVsActualError):
        mva.model_account_gap_references({}, raw, sha, ("s2",))

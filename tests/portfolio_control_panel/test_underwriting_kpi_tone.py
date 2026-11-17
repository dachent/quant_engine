"""Underwriting KPI shortfalls are informational; a cluster-budget breach is a labelled risk result."""

from __future__ import annotations

from pathlib import Path
import re

from test_shell_qc1_labels import _run, _underwriting, _underwriting_row


def _payload(
    counts: dict[str, int], fit: dict[str, str] | None = None
) -> dict[str, object]:
    row = _underwriting_row("syn", "Synthetic", None)
    if fit is not None:
        row["portfolio_fit"] = fit
    payload = _underwriting(row)
    payload["strategy_underwriting"]["counts"].update(counts)  # type: ignore[index]
    return payload


def _tones(dom: dict[str, object]) -> list[str]:
    html = str(dom["#underwriting-kpis"]["innerHTML"])  # type: ignore[index]
    return re.findall(r"kpi-card (?:tone-(\w+))?", html)


def test_a_shortfall_is_neutral_and_full_coverage_is_positive(tmp_path: Path) -> None:
    short = _run(
        tmp_path,
        _payload(
            {
                "authoritative_design": 0,
                "version_matched_long_history": 0,
                "quantified_model_account_gap": 0,
                "empirical_regime_comparison": 0,
            }
        ),
        "renderUnderwriting",
    )
    full = _run(
        tmp_path,
        _payload(
            {
                "authoritative_design": 1,
                "version_matched_long_history": 1,
                "quantified_model_account_gap": 1,
                "empirical_regime_comparison": 1,
            }
        ),
        "renderUnderwriting",
    )

    assert _tones(short) == ["neutral"] * 4
    assert _tones(full) == ["positive"] * 4


def test_a_cluster_budget_breach_is_a_warning_labelled_as_a_risk_result(
    tmp_path: Path,
) -> None:
    dom = _run(
        tmp_path,
        _payload(
            {},
            {
                "portfolio_fit_status": "CLUSTER_BUDGET_BREACH",
                "cluster_budget_status": "BREACHES_FULL_SYSTEMATIC_15_PERCENT_LOSS_LIMIT",
            },
        ),
        "renderUnderwriting",
    )
    html = str(dom["#underwriting-table"]["innerHTML"])  # type: ignore[index]

    assert 'status-text warning">Cluster budget breach<' in html
    assert (
        "A risk-budget result computed from the evidence shown, not a data gap" in html
    )


def test_a_row_within_the_boundary_carries_no_breach_note(tmp_path: Path) -> None:
    dom = _run(
        tmp_path,
        _payload(
            {}, {"portfolio_fit_status": "WITHIN_CURRENT_CLUSTER_STRESS_BOUNDARY"}
        ),
        "renderUnderwriting",
    )
    html = str(dom["#underwriting-table"]["innerHTML"])  # type: ignore[index]

    assert "not a data gap" not in html

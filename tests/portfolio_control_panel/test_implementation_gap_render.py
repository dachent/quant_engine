"""The account-versus-model status is rendered from the report's derived basis, never as a defect."""

from __future__ import annotations

from pathlib import Path
import re

from test_shell_qc1_labels import _capital_payload, _cost_strategy, _flat, _run


def _command(tmp_path: Path, **fields: object) -> dict[str, object]:
    strategy = _cost_strategy(**fields)
    return _run(tmp_path, _capital_payload(strategy), "renderCommand")


def _cell_html(dom: dict[str, object], table: str) -> str:
    return str(dom[table]["innerHTML"])  # type: ignore[index]


def test_not_testable_is_informational_not_a_warning(tmp_path: Path) -> None:
    dom = _command(
        tmp_path,
        implementation_gap_status="not_testable",
        implementation_gap_basis="NO_COMPARATOR_ADMITTED",
        implementation_gap_audit_status="partial_only",
        provider_comparison_status="UNAVAILABLE",
        provider_comparison_disposition="No model series exists for this strategy.",
    )
    html = _cell_html(dom, "#portfolio-implementation-table")

    assert re.search(r'status-text neutral">Not testable<', html)
    assert "warning" not in re.findall(r'status-text (\w+)"', html)
    assert "No model series exists for this strategy." in _flat(
        dom, "#portfolio-implementation-table"
    )


def test_the_retained_audit_token_is_only_a_tooltip(tmp_path: Path) -> None:
    dom = _command(
        tmp_path,
        implementation_gap_status="tested",
        implementation_gap_basis="VERIFIED_IMPLEMENTATION_RECONCILIATION",
        implementation_gap_audit_status="not_same_vintage",
    )
    html = _cell_html(dom, "#portfolio-implementation-table")
    flat = _flat(dom, "#portfolio-implementation-table")

    assert 'title="Retained audit status: not_same_vintage"' in html
    assert "Not same vintage" not in flat
    assert "Verified implementation reconciliation" in flat
    assert re.search(r'status-text positive">Tested<', html)


def test_an_exact_date_comparison_names_observations_and_the_unresolved_version(
    tmp_path: Path,
) -> None:
    dom = _command(
        tmp_path,
        implementation_gap_status="tested",
        implementation_gap_basis="EXACT_DATE_PROVIDER_COMPARISON",
        provider_comparison_observations=251,
        provider_comparison_version_compatibility="UNRESOLVED",
        provider_comparison_status="DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
    )
    flat = _flat(dom, "#portfolio-implementation-table")

    assert "Exact-date provider comparison · 251 observations" in flat
    assert "version compatibility unresolved" in flat


def test_the_register_comparison_column_shows_the_derived_status_without_red(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        implementation_gap_status="tested",
        implementation_gap_basis="VERIFIED_IMPLEMENTATION_RECONCILIATION",
        provider_comparison_status="UNAVAILABLE",
        provider_comparison_disposition="Retained provider files are a different vintage.",
    )
    capital = _run(tmp_path, _capital_payload(strategy), "renderCapital")
    html = re.findall(r"<td>.*?</td>", str(capital["#strategy-table"]["innerHTML"]))[-1]

    # the provider/model part of the cell leads (the benchmark comparison follows it)
    assert re.findall(r'status-text (\w+)">([^<]*)<', html)[0] == (
        "positive",
        "Tested",
    )
    assert "Verified implementation reconciliation" in html
    assert "Retained provider files are a different vintage." in html


def test_the_register_shows_no_comparator_as_neutral_not_unavailable_red(
    tmp_path: Path,
) -> None:
    strategy = _cost_strategy(
        implementation_gap_status="not_testable",
        implementation_gap_basis="NO_COMPARATOR_ADMITTED",
        provider_comparison_status="UNAVAILABLE",
        provider_comparison_disposition="No model series exists for this strategy.",
    )
    capital = _run(tmp_path, _capital_payload(strategy), "renderCapital")
    html = re.findall(r"<td>.*?</td>", str(capital["#strategy-table"]["innerHTML"]))[-1]

    assert re.findall(r'status-text (\w+)">([^<]*)<', html)[0] == (
        "neutral",
        "Not testable",
    )

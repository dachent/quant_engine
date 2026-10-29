"""Panel render audit (#543, PANEL-RENDER-001): one regression test per confirmed register row.

The panel shell is a closed script, so every test here runs the real
``apps/portfolio-control-panel/app.js`` under Node against a stub DOM
(``shell_harness.mjs``) with a minimal synthetic payload, and asserts the rendered text.
Each test is named with its register id (``F01`` ...).  No test changes a figure: they
pin labels, binding, wiring, formatting, staleness flags and totals computed from rows
the payload already carries.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

PANEL_ROOT = REPOSITORY_ROOT / "apps" / "portfolio-control-panel"
HARNESS = Path(__file__).with_name("shell_harness.mjs")
CUTOFF = "2026-09-17"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="the shell tests need node"
)


# ---- harness ---------------------------------------------------------------------------


def run(
    tmp_path: Path,
    payload: dict[str, Any],
    functions: str = "",
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    command = [
        str(shutil.which("node")),
        str(HARNESS),
        str(PANEL_ROOT / "app.js"),
        str(payload_path),
        functions,
    ]
    if calls is not None:
        calls_path = tmp_path / "calls.json"
        calls_path.write_text(json.dumps(calls), encoding="utf-8")
        command.append(str(calls_path))
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == [], result["errors"]
    for item in result["returns"]:
        assert "error" not in item, item
    return result


def call(tmp_path: Path, payload: dict[str, Any], name: str, *args: Any) -> Any:
    return run(tmp_path, payload, calls=[{"fn": name, "args": list(args)}])["returns"][
        0
    ]["value"]


def html(result: dict[str, Any], selector: str) -> str:
    return result["dom"][selector]["innerHTML"]


def text(result: dict[str, Any], selector: str) -> str:
    entry = result["dom"][selector]
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"<[^>]+>", " ", entry["innerHTML"] or entry["textContent"]),
    ).strip()


def base(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "advisory": {
            "correlation_one_stress": "0.371",
            "hwm_limit": "0.15",
            "hwm_numerical_status": "UNAVAILABLE",
            "hwm_status": "AVAILABLE_GOVERNED_HWM_PERIMETER",
            "nav_coverage": "1",
            "reason": "r",
            "readiness": "NOT_READY",
        },
        "allocations": [],
        "account_summary": [],
        "blocking_items": [],
        "capital": {},
        "capital_as_of": {},
        "capital_status": {},
        "counts": {
            "mwr_available": 0,
            "positions": 0,
            "reconciliation_checks": 0,
            "reconciliation_passed": 0,
            "strategies": 0,
            "twr_available": 0,
            "twr_rows": 0,
        },
        "coverage": [],
        "fact_status": {"flows": "PARTIAL", "positions": "PARTIAL"},
        "gates": [],
        "hwm_summary": {
            "covered_value_usd": "0",
            "excluded_details": [],
            "excluded_value_usd": "0",
            "governed_perimeter_value_usd": "0",
            "uncovered_details": [],
            "uncovered_value_usd": "0",
        },
        "limitations": [],
        "meta": {
            "performance_lineage": {"status": "AVAILABLE"},
            "portfolio_as_of": CUTOFF,
            "report_generation_id": "g" * 20,
            "baseline_generation_id": "b" * 20,
            "decomposition_generation_id": "d" * 20,
            "correlation_generation_id": "c" * 20,
            "report_sha256": "a" * 64,
            "decomposition_sha256": "a" * 64,
            "correlation_sha256": "a" * 64,
        },
        "performance": [],
        "positions": [],
        "provenance": [],
        "reconciliation_checks": [],
        "strategies": [],
        "weekly_returns": {"series": [], "weeks": []},
    }
    payload.update(overrides)
    return payload


def strategy(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "current_value_usd": "100",
        "economic_family": "F",
        "strategy_id": f"{name.lower().replace(' ', '-')}-id",
        "strategy_name": name,
        "systematic_weight": "0.5",
        **fields,
    }


# ---- F01 / F02: one model state per strategy, reduced across lens x surface rows --------


def _lens(
    rows_by_surface: dict[str, list[dict[str, Any]]], *, duplicate: bool = True
) -> dict[str, Any]:
    first = next(iter(rows_by_surface.values()))
    lens: dict[str, Any] = {
        "default_surface": next(iter(rows_by_surface)),
        "lens_id": "L",
        "surfaces": {name: {"series": rows} for name, rows in rows_by_surface.items()},
    }
    if duplicate:
        lens["series"] = first  # the lens-level series repeats its default surface
    return lens


def _decomposition(*lenses: dict[str, Any]) -> dict[str, Any]:
    return {"default_lens": "L", "modes": {"LIVE_ONLY": list(lenses)}}


def _row(series: str, status: str, source: str = "s") -> dict[str, Any]:
    return {"series_id": series, "source_key": source, "status": status}


def test_F01_the_model_state_does_not_depend_on_which_lens_row_is_listed_first(
    tmp_path: Path,
) -> None:
    rows = [
        _row("EU [P123]", "UNAVAILABLE", "us-economic"),
        _row("EU [P123]", "AVAILABLE", "europe-economic"),
        _row("EU [P123]", "UNAVAILABLE", "other"),
    ]
    seen = set()
    for ordering in itertools.permutations(rows):
        payload = base(
            decomposition_lenses=_decomposition(_lens({"STATIC": list(ordering)}))
        )
        summary = call(tmp_path, payload, "factorModelSummary", "EU [P123]")
        seen.add(json.dumps(summary, sort_keys=True))
    assert len(seen) == 1
    summary = json.loads(seen.pop())
    assert summary["state"] == "MODEL_VALIDATED_FOR_PM_USE"
    # the lens-level series repeats the default surface and is not counted twice
    assert (summary["validated"], summary["unavailable"], summary["total"]) == (1, 2, 3)


def test_F01_the_portfolio_cell_renders_the_resolved_state(tmp_path: Path) -> None:
    payload = base(
        strategies=[strategy("EU [P123]")],
        decomposition_lenses=_decomposition(
            _lens(
                {
                    "STATIC": [
                        _row("EU [P123]", "UNAVAILABLE", "us-economic"),
                        _row("EU [P123]", "AVAILABLE", "europe-economic"),
                    ]
                }
            )
        ),
    )
    result = run(tmp_path, payload, "renderCommand")
    cell = text(result, "#portfolio-implementation-table")
    assert "Model validated for PM use" in cell
    assert (
        "PM-validated on 1 of 2 lens/surface rows; diagnostic only 0; unavailable 1"
        in cell
    )


def test_F02_a_diagnostic_only_surface_is_never_hidden_by_a_validated_one(
    tmp_path: Path,
) -> None:
    payload = base(
        decomposition_lenses=_decomposition(
            _lens(
                {
                    "STATIC": [_row("Volatis", "AVAILABLE")],
                    "DYNAMIC_FLS": [_row("Volatis", "DIAGNOSTIC_ONLY")],
                },
                duplicate=True,
            )
        ),
    )
    summary = call(tmp_path, payload, "factorModelSummary", "Volatis")
    assert summary["state"] == "MODEL_VALIDATED_FOR_PM_USE"
    assert summary["note"] == (
        "PM-validated on 1 of 2 lens/surface rows; diagnostic only 1; unavailable 0"
    )
    only_diagnostic = base(
        decomposition_lenses=_decomposition(
            _lens({"STATIC": [_row("Volatis", "DIAGNOSTIC_ONLY")]})
        )
    )
    assert (
        call(tmp_path, only_diagnostic, "factorModelSummary", "Volatis")["state"]
        == "MODEL_COMPUTED_NOT_VALIDATED"
    )
    none = base(
        decomposition_lenses=_decomposition(
            _lens({"STATIC": [_row("Other", "AVAILABLE")]})
        )
    )
    missing = call(tmp_path, none, "factorModelSummary", "Volatis")
    assert missing["state"] == "MODEL_UNAVAILABLE"
    assert missing["total"] == 0


# ---- F03 / F04 / F35: account and provider names ---------------------------------------


def test_F03_the_producers_account_label_names_the_account(tmp_path: Path) -> None:
    summary = [
        {
            "account_id": "z-hash",
            "provider": "SCHWAB",
            "account_label": "Schwab •5722 — IRA (Boris)",
        },
        {"account_id": "a-hash", "provider": "SCHWAB"},
        {"account_id": "m-hash", "provider": "SCHWAB", "account_label": "  "},
    ]
    for ordering in (summary, list(reversed(summary))):
        payload = base(account_summary=ordering)
        assert (
            call(tmp_path, payload, "accountLabel", "z-hash")
            == "Schwab •5722 — IRA (Boris)"
        )
        # an account with no label keeps the numbered fallback, and an unknown id is unrecognized
        assert call(tmp_path, payload, "accountLabel", "a-hash") == "Schwab account 1"
        assert call(tmp_path, payload, "accountLabel", "m-hash") == "Schwab account 2"
        assert call(tmp_path, payload, "accountLabel", "nope") == "Unrecognized account"


def _leverage_row(**fields: Any) -> dict[str, Any]:
    return {
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
        "coverage_status": "OBSERVED_ONLY",
        "data_class": "FINANCING_COST",
        "provider_id": "ibkr",
        "strategy_id": None,
        "account_id": None,
        **fields,
    }


def test_F04_leverage_rows_show_provider_account_and_strategy_names_not_ids(
    tmp_path: Path,
) -> None:
    payload = base(
        account_summary=[
            {
                "account_id": "account-known",
                "provider": "IBKR",
                "account_label": "IBKR •6907",
            }
        ],
        strategies=[strategy("AI Factor", strategy_id="p123-ai-factor")],
        leverage_disclosures=[
            _leverage_row(account_id="account-known", strategy_id="p123-ai-factor"),
            _leverage_row(account_id="account-2020b6f94f85114f"),
            _leverage_row(provider_id="alpaca"),
            _leverage_row(provider_id="JOHN HANCOCK"),
        ],
    )
    table = html(run(tmp_path, payload, "renderLeverageDisclosures"), "#leverage-table")
    assert "IBKR •6907" in table
    assert "AI Factor" in table
    assert "IBKR · Unrecognized account" in table
    assert "John Hancock" in table
    assert "account-2020b6f9" not in table
    assert "p123-ai-factor" not in table
    assert "ibkr ·" not in table
    # a null account id is a provider-level row: the provider alone
    assert re.search(r"<td>Alpaca</td>", table)


def test_F04_fact_matrix_and_attribution_dispositions_route_provider_and_account(
    tmp_path: Path,
) -> None:
    row = {
        "account_id": "account-known",
        "provider_id": "JOHN HANCOCK",
        "fact_type": "nav",
        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
    }
    payload = base(
        account_summary=[
            {
                "account_id": "account-known",
                "provider": "JOHN HANCOCK",
                "account_label": "JH •1234",
            }
        ],
        fact_coverage_matrix=[
            row,
            {**row, "account_id": None, "provider_id": "matrixport"},
        ],
        attribution_dispositions=[{**row, "disposition": "D", "period": "CURRENT"}],
    )
    result = run(tmp_path, payload, "renderFactScopeTables")
    for selector in ("#fact-coverage-matrix-table", "#attribution-dispositions-table"):
        table = html(result, selector)
        assert "JH •1234" in table
        assert "John Hancock" in table
        assert "account-known" not in table
        assert "JOHN HANCOCK" not in table
    assert "Provider-level (no account)" in html(result, "#fact-coverage-matrix-table")


def test_F04_invested_capital_tables_name_the_strategy(tmp_path: Path) -> None:
    payload = base(
        strategies=[strategy("TT Portfolio", strategy_id="tt-portfolio")],
        invested_capital={
            "authority_profile": "A",
            "complete_through": CUTOFF,
            "current_aggregate_usd": "100",
            "current_by_strategy": [
                {"strategy_id": "tt-portfolio", "invested_capital_usd": "100"}
            ],
            "daily_aggregate": [],
            "daily_by_strategy": [],
            "policy_id": "P",
            "status": "S",
        },
    )
    result = run(tmp_path, payload, "renderInvestedCapital")
    table = html(result, "#invested-capital-current-table")
    assert "TT Portfolio" in table
    assert "tt-portfolio" not in table


def test_F35_provider_names_cover_internal_and_john_hancock_and_title_case(
    tmp_path: Path,
) -> None:
    assert call(tmp_path, base(), "providerName", "INTERNAL") == "Internal"
    assert call(tmp_path, base(), "providerName", "JOHN HANCOCK") == "John Hancock"
    assert call(tmp_path, base(), "providerName", "MATRIXPORT") == "Matrixport"
    assert (
        call(tmp_path, base(), "providerName", "some_new provider")
        == "Some New Provider"
    )
    assert call(tmp_path, base(), "providerName", None) == "Provider"


# ---- F05 / F43: the leverage table ----------------------------------------------------


def test_F05_the_leverage_table_pages_through_every_row(tmp_path: Path) -> None:
    rows = [
        _leverage_row(provider_id="ibkr", data_class="FINANCING_COST")
        for _ in range(600)
    ]
    payload = base(leverage_disclosures=rows)
    calls = [
        {"fn": "renderLeverageDisclosures"},
        {"eval": "leveragePage = 1"},
        {"fn": "renderLeverageDisclosures"},
        {"eval": "leveragePage = 9"},
        {"fn": "renderLeverageDisclosures"},
    ]
    first = run(tmp_path, payload, "renderLeverageDisclosures")
    assert text(first, "#leverage-truncation").startswith(
        "Showing 1–250 of 600 observations"
    )
    assert text(first, "#leverage-page-status") == "Page 1 of 3"
    later = run(tmp_path, payload, "", calls)
    # the last render clamps an out-of-range page to the last page: rows 501-600
    assert text(later, "#leverage-truncation").startswith(
        "Showing 501–600 of 600 observations"
    )
    assert text(later, "#leverage-page-status") == "Page 3 of 3"


def test_F43_the_leverage_badge_counts_observed_and_unavailable(tmp_path: Path) -> None:
    rows = [
        _leverage_row(),
        _leverage_row(),
        _leverage_row(coverage_status="UNAVAILABLE"),
    ]
    result = run(tmp_path, base(leverage_disclosures=rows), "renderLeverageDisclosures")
    assert (
        result["dom"]["#leverage-status"]["textContent"] == "Observed 2 / unavailable 1"
    )
    all_observed = run(
        tmp_path, base(leverage_disclosures=rows[:2]), "renderLeverageDisclosures"
    )
    assert all_observed["dom"]["#leverage-status"]["textContent"] == "Observed"


# ---- F06 / F20 / F22: the allocation table --------------------------------------------


def _allocation_payload(**overrides: Any) -> dict[str, Any]:
    snapshot = {
        "as_of_date": "2026-08-16",
        "scope": "SYSTEMATIC",
        "weight_basis": "HISTORICAL_SNAPSHOT_2026_08_16",
    }
    payload = base(
        allocations=[
            {
                **snapshot,
                "current_value_usd": "104842.54",
                "strategy_id": "tt-id",
                "strategy_name": "TT",
                "systematic_weight": "0.4",
            },
            {
                **snapshot,
                "current_value_usd": "44313.92",
                "strategy_id": "eu-id",
                "strategy_name": "EU",
                "systematic_weight": "0.6",
            },
            {
                **snapshot,
                "current_value_usd": "100043.62",
                "scope": "RESERVE",
                "strategy_id": None,
                "strategy_name": "Reserve",
                "systematic_weight": None,
            },
        ],
        strategies=[
            strategy("TT", current_value_usd="101071.47", systematic_weight="0.35"),
            strategy("EU", current_value_usd="45464.69", systematic_weight="0.65"),
        ],
        capital_status={"reserve": "NOT_READY_AT_CUTOFF"},
    )
    payload.update(overrides)
    return payload


def test_F06_the_allocation_table_is_labelled_as_a_snapshot_and_adds_current_columns(
    tmp_path: Path,
) -> None:
    result = run(tmp_path, _allocation_payload(), "renderAllocationTable")
    table = html(result, "#allocation-table")
    assert "Snapshot value (as of Aug 16, 2026)" in table
    assert "Snapshot weight (as of Aug 16, 2026)" in table
    assert "Current value (cutoff)" in table
    assert "Current weight" in table
    assert "$101,071" in table and "$45,465" in table  # current values
    assert "$104,843" in table  # snapshot value
    note = result["dom"]["#allocation-snapshot-note"]["textContent"]
    assert "Aug 16, 2026" in note and "32 days before the Sep 17, 2026 cutoff" in note
    assert "Actual value" not in table


def test_F22_a_reserve_that_is_not_ready_is_shown_not_ready_and_a_ready_one_is_sized(
    tmp_path: Path,
) -> None:
    not_ready = html(
        run(tmp_path, _allocation_payload(), "renderAllocationTable"),
        "#allocation-table",
    )
    reserve_row = [
        row
        for row in not_ready.split("<tr>")
        if "Reserve" in row and "RESERVE" not in row
    ][0]
    assert "Not ready" in reserve_row
    custody = {
        "reserve": {
            "as_of_date": CUTOFF,
            "current_value": {"currency": "USD", "value": "98000.00"},
            "status": "AVAILABLE",
        }
    }
    ready = html(
        run(
            tmp_path,
            _allocation_payload(custody_current_capital=custody),
            "renderAllocationTable",
        ),
        "#allocation-table",
    )
    assert "$98,000" in ready
    custody_not_ready = {
        "reserve": {
            "as_of_date": None,
            "current_value": {"value": None},
            "status": "NOT_READY",
        }
    }
    still = html(
        run(
            tmp_path,
            _allocation_payload(custody_current_capital=custody_not_ready),
            "renderAllocationTable",
        ),
        "#allocation-table",
    )
    assert "$98,000" not in still and "Not ready" in still


def test_F20_the_allocation_footer_totals_only_the_additive_columns_from_the_rows_shown(
    tmp_path: Path,
) -> None:
    table = html(
        run(tmp_path, _allocation_payload(), "renderAllocationTable"),
        "#allocation-table",
    )
    footer = re.search(r"<tfoot>(.*?)</tfoot>", table, flags=re.S)
    assert footer is not None
    cells = re.sub(r"<[^>]+>", "|", footer[1])
    assert "Total" in cells
    assert "$249,200" in cells  # 104,842.54 + 44,313.92 + 100,043.62
    assert (
        "$146,536" in cells
    )  # 101,071.47 + 45,464.69; the not-ready reserve is excluded
    assert "excludes 1 not ready" in cells
    assert "100.0%" in cells  # 0.4 + 0.6 and 0.35 + 0.65
    # a filter changes the rows shown and the totals with them
    filtered = html(
        run(
            tmp_path,
            _allocation_payload(),
            "",
            [
                {"eval": "allocationFilter = 'SYSTEMATIC'"},
                {"fn": "renderAllocationTable"},
            ],
        ),
        "#allocation-table",
    )
    assert "$149,156" in filtered  # 104,842.54 + 44,313.92


def test_F20_correlation_one_and_cluster_tables_carry_totals(tmp_path: Path) -> None:
    result = run(tmp_path, _dependence_payload(), "renderDependence")
    components = html(result, "#correlation-one-table")
    footer = re.sub(
        r"<[^>]+>", "|", re.search(r"<tfoot>(.*?)</tfoot>", components, flags=re.S)[1]
    )  # type: ignore[index]
    assert "Total" in footer and "100.0%" in footer and "37.1%" in footer
    clusters = html(result, "#economic-cluster-table")
    cluster_footer = re.sub(
        r"<[^>]+>", "|", re.search(r"<tfoot>(.*?)</tfoot>", clusters, flags=re.S)[1]
    )  # type: ignore[index]
    assert "Total" in cluster_footer and "26.6%" in cluster_footer


# ---- F07 / F23 / F24: staleness flags --------------------------------------------------


def _risk_payload(**overrides: Any) -> dict[str, Any]:
    payload = base(
        dependence_analysis={
            "weight_authority": {
                "as_of_date": "2026-08-16",
                "policy_id": "PORT-DEPENDENCE-1",
            },
            "weight_basis": "POLICY_PORT_DEPENDENCE_1",
        },
    )
    payload.update(overrides)
    return payload


def test_F07_a_weight_set_older_than_the_cutoff_says_by_how_many_days(
    tmp_path: Path,
) -> None:
    stress = text(run(tmp_path, _risk_payload(), "renderRisk"), "#stress-bar")
    assert "as of 2026-08-16; older than cutoff by 32 days" in stress
    current = _risk_payload(
        dependence_analysis={
            "weight_authority": {
                "as_of_date": CUTOFF,
                "policy_id": "PORT-DEPENDENCE-1",
            },
            "weight_basis": "POLICY_PORT_DEPENDENCE_1",
        }
    )
    assert "older than cutoff" not in text(
        run(tmp_path, current, "renderRisk"), "#stress-bar"
    )


def test_F23_provenance_rows_older_than_the_cutoff_are_flagged(tmp_path: Path) -> None:
    provenance = [
        {
            "artifact": "old",
            "as_of_date": "2026-07-17",
            "schema": "s",
            "sha256": "a" * 64,
        },
        {
            "artifact": "current",
            "as_of_date": CUTOFF,
            "schema": "s",
            "sha256": "b" * 64,
        },
        {
            "artifact": "later",
            "as_of_date": "2026-10-02",
            "schema": "s",
            "sha256": "c" * 64,
        },
        {"artifact": "undated", "as_of_date": None, "schema": "s", "sha256": "d" * 64},
    ]
    result = run(tmp_path, _data_payload(provenance=provenance), "renderData")
    rows = html(result, "#source-table").split("<tr>")
    flagged = [row for row in rows if "older than cutoff by 62 days" in row]
    assert len(flagged) == 1 and "old" in flagged[0]
    assert html(result, "#source-table").count("older than cutoff") == 1


def _factor_series(
    name: str, status: str = "UNAVAILABLE", **fields: Any
) -> dict[str, Any]:
    return {
        "coverage": {},
        "exposures": [],
        "model_id": None,
        "series_id": name,
        "source_label": "Src",
        "status": status,
        "unavailable_reasons": ["UNAVAILABLE_INSUFFICIENT_HISTORY"],
        **fields,
    }


def _attribution_payload(
    series: list[dict[str, Any]], **lens_fields: Any
) -> dict[str, Any]:
    lens = {
        "as_of_date": "2026-07-31",
        "available_count": 0,
        "default_surface": "STATIC",
        "diagnostic_count": 0,
        "lens_id": "ECONOMIC_FACTOR",
        "lens_label": "Economic",
        "method_label": "M_L",
        "methodology_version": "v",
        "series": series,
        "sources": [
            {"as_of_date": "2026-07-31", "source_key": "us", "source_label": "US"}
        ],
        "unavailable_count": len(series),
        **lens_fields,
    }
    return base(
        decomposition_lenses={
            "default_lens": "ECONOMIC_FACTOR",
            "modes": {"LIVE_ONLY": [lens]},
        },
        strategies=[],
    )


def test_F24_a_lens_older_than_the_cutoff_is_flagged_in_its_header(
    tmp_path: Path,
) -> None:
    result = run(
        tmp_path, _attribution_payload([_factor_series("S")]), "renderAttribution"
    )
    boundary = text(result, "#factor-lens-boundary")
    assert "US · through 2026-07-31 · older than cutoff by 48 days" in boundary
    current = _attribution_payload(
        [_factor_series("S")],
        sources=[{"as_of_date": CUTOFF, "source_key": "us", "source_label": "US"}],
    )
    assert "older than cutoff" not in text(
        run(tmp_path, current, "renderAttribution"), "#factor-lens-boundary"
    )


def test_F38_a_series_with_no_model_says_no_composition_not_legacy(
    tmp_path: Path,
) -> None:
    result = run(
        tmp_path, _attribution_payload([_factor_series("S")]), "renderAttribution"
    )
    assert "No composition: series unavailable" in text(result, "#factor-model-table")
    assert "No composition: series unavailable" in text(result, "#factor-detail")
    assert "legacy" not in text(result, "#factor-detail").lower().replace(
        "legacy analysis does not", ""
    )
    fitted = _factor_series(
        "S",
        "DIAGNOSTIC_ONLY",
        diagnostics={"holdout": {}, "training_design": {}, "inference": {}},
        model_quality={"gate_reason": "G"},
        alpha={},
    )
    legacy = run(tmp_path, _attribution_payload([fitted]), "renderAttribution")
    assert "Undeclared legacy composition" in text(legacy, "#factor-model-table")


# ---- F08 / F09 / F10 / F48: the HWM callouts and KPI -----------------------------------


def _proof(points: int = 703, **fields: Any) -> dict[str, Any]:
    from datetime import date, timedelta

    start = date(2024, 10, 15)
    path = []
    for index in range(points):
        value = 100 + (index % 17) * 1.25 - (30 if index == 120 else 0)
        path.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "index": f"{value:.30f}",
            }
        )
    path[-1]["date"] = "2026-09-17"
    return {
        "age_status": "COMPLETE_DATA_AGE_WARNING",
        "calculation_age_days": 25,
        "complete_through": CUTOFF,
        "current_drawdown": "-0.1",
        "current_hwm": "121.1156",
        "current_index": "99.1",
        "evaluated_on": "2026-10-12",
        "financial_acceptance": "PENDING_OWNER_ACCEPTANCE",
        "index_path": path,
        "maximum_age_days": 2,
        "maximum_drawdown": "-0.3202626299",
        "receipt_sha256": "f" * 64,
        "return_basis": "TWR_BASIS",
        **fields,
    }


def test_F08_the_governed_hwm_callout_summarises_the_path_and_the_age(
    tmp_path: Path,
) -> None:
    payload = _risk_payload(
        hwm_governed_proof=_proof(),
        advisory={
            **base()["advisory"],
            "hwm_candidate": {
                "not_accepted_register": "R",
                "not_accepted_reason": "X",
                "policy_id": "PORT-HWM-4",
            },
        },
    )
    callout = html(run(tmp_path, payload, "renderRisk"), "#hwm-status")
    assert "703 daily points 2024-10-15..2026-09-17" in callout
    assert "25 days old (maximum 2)" in callout
    assert "high-water point" in callout and "lowest point" in callout
    # the 703 thirty-three-digit values are not all printed
    assert len(callout) < 6000


def test_F08_the_portable_proof_callout_gets_the_same_summary(tmp_path: Path) -> None:
    payload = _risk_payload(
        hwm_portable_proof=_proof(points=40, policy_id="PORT-HWM-4")
    )
    callout = html(run(tmp_path, payload, "renderRisk"), "#hwm-status")
    assert "40 daily points 2024-10-15..2026-09-17" in callout
    assert "25 days old (maximum 2)" in callout
    assert "Portable PORT-HWM-4 proof only" in callout


def test_F10_the_systematic_hwm_kpi_shows_the_numerical_status_and_both_policy_ids(
    tmp_path: Path,
) -> None:
    payload = _risk_payload(
        hwm_governed_proof=_proof(points=5, policy_id="PORT-HWM-4"),
        hwm_summary={**base()["hwm_summary"], "policy_id": "PORT-HWM-3"},
    )
    result = run(tmp_path, payload, "renderRisk")
    kpis = text(result, "#risk-kpis")
    assert re.search(r"Systematic HWM Unavailable ", kpis)
    assert "Governed perimeter: Available across the governed HWM perimeter" in kpis
    callout = text(result, "#hwm-status")
    assert (
        "governed proof PORT-HWM-4; HWM summary PORT-HWM-3; the two policy ids differ"
        in callout
    )


def test_F09_the_callout_discloses_the_single_sleeve_history_before_the_perimeter(
    tmp_path: Path,
) -> None:
    daily = [
        {
            "date": "2024-10-15",
            "invested_capital_usd": "3100",
            "strategy_id": "volatis-id",
        },
        {
            "date": "2025-04-08",
            "invested_capital_usd": "19974",
            "strategy_id": "volatis-id",
        },
        {"date": "2025-04-08", "invested_capital_usd": "0", "strategy_id": "ai-id"},
        {"date": "2025-11-03", "invested_capital_usd": "5000", "strategy_id": "ai-id"},
    ]
    payload = _risk_payload(
        hwm_governed_proof=_proof(points=5),
        strategies=[strategy("Volatis"), strategy("AI Factor", strategy_id="ai-id")],
        invested_capital={"daily_by_strategy": daily},
    )
    callout = text(run(tmp_path, payload, "renderRisk"), "#hwm-status")
    assert (
        "Perimeter start: only Volatis is funded from 2024-10-15 until 2025-11-03, when AI Factor is first funded"
        in callout
    )
    no_data = text(
        run(tmp_path, _risk_payload(hwm_governed_proof=_proof(points=5)), "renderRisk"),
        "#hwm-status",
    )
    assert "Perimeter start" not in no_data


def test_F48_the_governed_result_sentence_is_bound_to_the_payload(
    tmp_path: Path,
) -> None:
    payload = _risk_payload(hwm_governed_proof=_proof(points=5, policy_id="PORT-HWM-9"))
    callout = text(run(tmp_path, payload, "renderRisk"), "#hwm-status")
    assert "Governed PORT-HWM-9 result (Pending owner acceptance)" in callout
    assert "ACCEPTED_GOVERNED" not in callout


# ---- F11 / F12: the epoch manifest -----------------------------------------------------


def _manifest_payload(**row_fields: Any) -> dict[str, Any]:
    row = {
        "account_id": "a0",
        "account_label": "Label a0",
        "annualized_return_on_net_capital": "0.1",
        "current_value": "48120.06",
        "end": CUTOFF,
        "gain": "0",
        "invested": "48120.06",
        "net_capital": "48120.06",
        "observation_linked_return": "0.05",
        "observed_hwm_index": "216.2325",
        "observed_max_drawdown": "0.3203",
        "opening_basis": "FIRST_AVAILABLE_NAV",
        "return_on_net_capital": "0.05",
        "start": "2026-01-01",
        "status": "OK",
        "strategy_id": "s",
        "withdrawn": "0",
        **row_fields,
    }
    return base(
        account_manifest={
            "accounting_currency": "USD",
            "authority_readiness": {
                "authority_status": "A",
                "independent_authority_verification": "V",
                "publication_readiness": "P",
            },
            "calculation_cutoff_date": CUTOFF,
            "flow_reconciliation": {"method": "m", "status": "s"},
            "rows": [row],
            "schema_version": 9,
            "source_identities": {
                "classified_flows": {
                    "ledger_identity": {"artifact": {"as_of_date": CUTOFF}}
                }
            },
        },
        invested_capital={
            "complete_through": CUTOFF,
            "current_aggregate_usd": "378083.13",
        },
        meta={
            **base()["meta"],
            "account_manifest_authority_profile": "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES",
            "account_manifest_verification": {
                "method": "m",
                "publication_readiness": "P",
                "status": "S",
            },
        },
    )


def test_F11_the_manifest_labels_the_hwm_index_and_signs_the_drawdown(
    tmp_path: Path,
) -> None:
    table = html(
        run(tmp_path, _manifest_payload(), "renderAccountManifest"),
        "#account-manifest-table",
    )
    assert "Observed HWM (index, base 100)" in table
    assert "Observed Max DD (signed)" in table
    assert ">216.23<" in table
    assert ">-32.03%<" in table
    already_signed = html(
        run(
            tmp_path,
            _manifest_payload(observed_max_drawdown="-0.02"),
            "renderAccountManifest",
        ),
        "#account-manifest-table",
    )
    assert ">-2.00%<" in already_signed
    zero = html(
        run(
            tmp_path,
            _manifest_payload(observed_max_drawdown="0"),
            "renderAccountManifest",
        ),
        "#account-manifest-table",
    )
    assert ">0.00%<" in zero


def test_F12_the_manifest_footer_shows_the_consolidated_net_invested_and_never_sums_rows(
    tmp_path: Path,
) -> None:
    table = html(
        run(tmp_path, _manifest_payload(), "renderAccountManifest"),
        "#account-manifest-table",
    )
    footer = re.search(r"<tfoot>(.*?)</tfoot>", table, flags=re.S)
    assert footer is not None
    plain = re.sub(r"<[^>]+>", " ", footer[1])
    assert "Net invested (consolidated)" in plain and "$378,083.13" in plain
    assert (
        "Rows are not additive in Capital in and Withdrawn: internal transfers appear in both"
        in plain
    )
    assert "Total" in plain and "open at the cutoff" in plain
    without = _manifest_payload()
    without["invested_capital"] = None
    bare = html(
        run(tmp_path, without, "renderAccountManifest"), "#account-manifest-table"
    )
    assert "Net invested (consolidated)" not in bare and "Rows are not additive" in bare


# ---- F13 / F14: the TWR status and the provider comparison ------------------------------


def test_F13_the_twr_status_follows_the_rows_own_flow_timing(tmp_path: Path) -> None:
    common = {
        "account_id": "a",
        "mwr_status": "AVAILABLE",
        "strategy_name": "S",
        "twr": "0.1",
        "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
    }
    payload = _risk_payload(
        performance=[
            {**common, "flow_timing": None},
            {
                **common,
                "account_id": "b",
                "flow_timing": "DATE_ONLY_END_OF_DAY_INFERRED_TIMING",
            },
        ]
    )
    table = html(run(tmp_path, payload, "renderRisk"), "#performance-table")
    rows = [row for row in table.split("<tr data-account-id=")[1:]]
    assert "inferred flow timing" not in rows[0].lower()
    assert "Available" in rows[0]
    assert "Available · estimated timing" in rows[1]
    assert "Date only end of day inferred timing" in rows[1]


def test_F14_the_provider_comparison_flags_a_window_that_ends_before_the_cutoff(
    tmp_path: Path,
) -> None:
    row = {
        "cash_drag_reason": "r",
        "cash_drag_status": "S",
        "provider_comparison_observed_end": "2026-07-10",
        "provider_comparison_observed_start": "2026-01-09",
        "provider_comparison_observations": 27,
        "provider_comparison_reason": "Exact version unresolved",
        "provider_comparison_status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
    }
    cell = call(tmp_path, base(), "exactComparison", row)
    assert "older than cutoff by 69 days" in cell
    assert "Exact version unresolved" in cell
    current = call(
        tmp_path,
        base(),
        "exactComparison",
        {**row, "provider_comparison_observed_end": CUTOFF},
    )
    assert "older than cutoff" not in current


# ---- F15 / F17 / F33: the status vocabulary ---------------------------------------------


def test_F15_the_implementation_gap_tokens_have_labels(tmp_path: Path) -> None:
    for token, label in {
        "tested": "Tested",
        "not_testable": "Not testable",
        "partial_only": "Partial only",
        "not_same_vintage": "Not same vintage",
    }.items():
        assert call(tmp_path, base(), "humanStatus", token) == label
        assert call(tmp_path, base(), "toneForStatus", token) in {"positive", "warning"}
    assert call(tmp_path, base(), "toneForStatus", "tested") == "positive"
    assert call(tmp_path, base(), "toneForStatus", "not_testable") == "warning"


def test_F33_tones_come_from_an_explicit_table_and_completeness_proofs_are_not_positive(
    tmp_path: Path,
) -> None:
    tones = {
        "MODEL_VALIDATED_FOR_PM_USE": "positive",
        "MODEL_COMPUTED_NOT_VALIDATED": "warning",
        "MODEL_UNAVAILABLE": "danger",
        "VALIDATED": "positive",
        "NOT_VALIDATED": "warning",
        "OBSERVED_NOT_COMPLETENESS_PROOF": "neutral",
        "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF": "neutral",
        "NO_REPORTED_FACT_NOT_COMPLETENESS_PROOF": "neutral",
        "NO_ACCEPTED_FACT_PATH": "danger",
        "COMPLETE_WITHIN_DECLARED_BOUNDARY": "positive",
    }
    result = run(
        tmp_path,
        base(),
        calls=[{"fn": "toneForStatus", "args": [token]} for token in tones],
    )
    assert {
        status: item["value"]
        for status, item in zip(tones, result["returns"], strict=True)
    } == tones


def test_F33_the_fact_matrix_renders_completeness_proof_rows_with_the_neutral_tone(
    tmp_path: Path,
) -> None:
    row = {
        "account_id": None,
        "fact_type": "nav",
        "provider_id": "ibkr",
        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
    }
    table = html(
        run(tmp_path, base(fact_coverage_matrix=[row]), "renderFactScopeTables"),
        "#fact-coverage-matrix-table",
    )
    assert 'status-text neutral">Observed; not a completeness proof' in table
    assert "status-text positive" not in table


def test_F17_the_status_legend_lists_every_tone_class() -> None:
    page = (PANEL_ROOT / "index.html").read_bytes().decode("utf-8")
    legend = re.search(r'<details id="status-legend".*?</details>', page, flags=re.S)
    assert legend is not None
    for tone in ("positive", "warning", "danger", "reserve", "neutral"):
        assert f'class="status-text {tone}"' in legend[0]


def test_F17_the_labels_never_found_in_the_data_are_still_emitted_by_a_producer() -> (
    None
):
    """The 11 labels the audit found in neither the data nor app.js are latent vocabulary.

    Deleting a label whose token a producer (or a pinned producer fixture) still carries
    would print a respelled raw token the day that state occurs, so none is deleted; this
    pins why.
    """

    tokens = [
        "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV",
        "NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV",
        "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE",
        "NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION",
        "PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY",
        "PARTIAL_RECONCILIATION_ATTRIBUTION_NOT_READY",
        "UNAVAILABLE_INCOMPATIBLE_EVIDENCE_LAYERS",
        "UNAVAILABLE_MAPPING_MISSING",
        "UNAVAILABLE_MISSING_VERSION_MATCHED_LIVE",
        "UNAVAILABLE_SEALED_HOLDOUT_PREDICTED_R2",
        "UNAVAILABLE_UNKNOWN_TARGET_SERIES",
    ]
    producers = [
        path.read_bytes().decode("utf-8", "ignore")
        for root in (
            REPOSITORY_ROOT / "tools",
            REPOSITORY_ROOT / "packages",
            REPOSITORY_ROOT / "tests",
        )
        for path in root.rglob("*.py")
        if path != Path(__file__)
    ]
    app = (PANEL_ROOT / "app.js").read_bytes().decode("utf-8")
    for token in tokens:
        assert any(token in source for source in producers), token
        assert token in app, token


# ---- F16 / F18 / F42: the stylesheet ----------------------------------------------------


def _css() -> str:
    return (PANEL_ROOT / "analytics.css").read_bytes().decode("utf-8")


def _min_width_selectors(css: str) -> set[str]:
    found: set[str] = set()
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "min-width" in body:
            found.update(
                part.strip()
                for part in selectors.split(",")
                if part.strip().startswith("#")
            )
    return found


def test_F16_the_wide_wrapper_and_every_table_have_a_min_width_rule() -> None:
    css = _css()
    assert re.search(r"\.table-wrap\.wide table\s*\{[^}]*min-width", css)
    selectors = _min_width_selectors(css)
    for table in (
        "portfolio-implementation",
        "invested-capital-current",
        "invested-capital-daily",
        "leverage-coverage",
        "leverage",
        "allocation",
        "strategy",
        "lifecycle",
        "position",
        "account-manifest",
        "implementation",
        "reconciliation",
        "correlation-one",
        "economic-cluster",
        "candidate-driver",
        "candidate-leave-one-out",
        "candidate",
    ):
        assert f"#{table}-table" in selectors, table


def test_F18_the_nested_factor_detail_tables_have_ids_and_min_widths(
    tmp_path: Path,
) -> None:
    app = (PANEL_ROOT / "app.js").read_bytes().decode("utf-8")
    nested = (
        "factor-diagnostic-beta-table",
        "factor-diagnostic-contribution-table",
        "factor-contribution-table",
        "factor-proxy-table",
        "orthogonal-coefficient-table",
        "orthogonal-risk-table",
    )
    selectors = _min_width_selectors(_css())
    for table in nested:
        assert f'<table id="{table}"' in app, table
        assert f"#{table}" in selectors, table
    # every <table> the shell builds inside #factor-detail now has an id
    assert not re.search(r"<table><caption>", app)


def test_F42_every_class_the_shell_emits_is_defined() -> None:
    css = _css()
    for selector in (
        ".callout-warning",
        ".status-danger",
        ".metric-list",
        ".symbol-not-ready",
        ".is-hatched",
        ".glossary-panel",
        ".stale-tag",
        ".policy-text",
    ):
        assert re.search(re.escape(selector) + r"[\s,{.:]", css), selector


# ---- F19 / F44 / F50: headers, labels and formatting ------------------------------------


def test_F19_headers_carry_units_and_periods(tmp_path: Path) -> None:
    result = run(tmp_path, base(), "renderPositionTable")
    assert "Quantity (units)" in html(result, "#position-table")
    page = (PANEL_ROOT / "index.html").read_bytes().decode("utf-8")
    for window in (5, 21, 22, 63, 126, 130, 252, 260):
        assert (
            f'data-correlation-window="{window}" aria-pressed="false" title="{window} weekday observations">{window} obs.</button>'
            in page
        )
    signals = run(
        tmp_path,
        base(strategies=[strategy("S")], counts={**base()["counts"], "strategies": 1}),
        "renderSignals",
    )
    assert "Account cumulative (since observed start)" in html(
        signals, "#implementation-table"
    )
    windows = run(tmp_path, base(), "renderFactScopeTables")
    assert "Records (count)" in html(windows, "#observation-windows-table")


def test_F19_the_account_window_start_is_shown_beside_the_cumulative_return(
    tmp_path: Path,
) -> None:
    row = strategy(
        "S",
        account_live_cumulative_return="0.1",
        provider_evidence={
            "account": {
                "observed_start": "2026-01-09",
                "observed_end": CUTOFF,
                "provider_id": "schwab",
            }
        },
    )
    payload = base(strategies=[row], counts={**base()["counts"], "strategies": 1})
    assert "since Jan 9, 2026" in text(
        run(tmp_path, payload, "renderSignals"), "#implementation-table"
    )


def test_F44_the_cluster_column_is_named_for_what_it_holds(tmp_path: Path) -> None:
    table = html(
        run(tmp_path, _dependence_payload(), "renderDependence"),
        "#economic-cluster-table",
    )
    assert "Adverse-loss contribution" in table
    assert "Adverse-loss budget" not in table


def test_F46_the_header_says_the_coverage_is_the_hwm_perimeters() -> None:
    page = (PANEL_ROOT / "index.html").read_bytes().decode("utf-8")
    assert (
        '<span>HWM-perimeter NAV coverage</span><strong id="coverage-status">' in page
    )


def test_F50_open_ended_dates_and_nested_labels_and_the_reconciliation_difference(
    tmp_path: Path,
) -> None:
    row = strategy(
        "S",
        benchmark_assignment_status="ASSIGNED",
        benchmark_effective_start="2026-01-03",
        benchmark_effective_end=None,
        benchmark_comparison_status="UNAVAILABLE",
    )
    cell = call(tmp_path, base(), "benchmarkDisclosure", row)
    assert "Effective Jan 3, 2026–open" in cell
    assert (
        call(tmp_path, base(), "annualizationSuffix", "basis (<1 year)")
        == " · basis (&lt;1 year)"
    )
    assert (
        call(tmp_path, base(), "annualizationSuffix", "annualized over 5 years")
        == " (annualized over 5 years)"
    )
    dependence = _dependence_payload()
    summary = text(
        run(tmp_path, dependence, "renderDependence"), "#correlation-one-summary"
    )
    assert "Reconciliation 0.000000" in summary
    tiny = _dependence_payload()
    tiny["dependence_analysis"]["correlation_one"]["reconciliation_difference"] = (
        "9.7e-14"
    )
    assert "Reconciliation 9.70e-14" in text(
        run(tmp_path, tiny, "renderDependence"), "#correlation-one-summary"
    )


# ---- F21 / F49: branches the current payload never reaches -------------------------------


def test_F21_a_candidate_driver_map_with_no_digest_renders_instead_of_throwing(
    tmp_path: Path,
) -> None:
    driver_map = {
        "as_of_date": "2026-07-17",
        "candidates": [],
        "coverage": {
            "candidate_count": 0,
            "measured_candidate_count": 0,
            "refresh_status": "FROZEN",
            "unavailable_candidate_count": 0,
        },
        "driver_clusters": [
            {
                "adverse_loss_budget": "0.15",
                "allocation_weight": "0.5",
                "budget_status": "S",
                "cluster_id": "TACTICAL_TIMING_MULTI_ASSET",
                "members": ["A"],
                "observed_pair_count": 0,
            }
        ],
        "portfolio": {"systematic_loss_limit": "0.15"},
        "research_status": "R",
        "tactical_leave_one_out": [],
    }
    payload = base(
        candidate_driver_map=driver_map,
        candidate_appendix={
            "as_of_date": "2026-07-17",
            "providers": [],
            "source_sha256": "a" * 64,
            "status": "FROZEN_AUDIT_APPENDIX",
            "tier_counts": {},
        },
    )
    payload["meta"]["candidate_driver_map_sha256"] = None
    result = run(tmp_path, payload, "renderCandidates")
    assert "digest not published" in result["dom"]["#candidate-boundary"]["textContent"]


def test_F21_the_object_form_of_a_missing_interval_renders(tmp_path: Path) -> None:
    row = {
        "account_id": None,
        "fact_type": "nav",
        "missing_intervals": [
            {"start": "2026-01-01", "end": "2026-01-05", "reason": "gap"},
            "free text",
        ],
        "provider_id": "ibkr",
        "status": "OBSERVED_NOT_COMPLETENESS_PROOF",
    }
    table = text(
        run(tmp_path, base(fact_coverage_matrix=[row]), "renderFactScopeTables"),
        "#fact-coverage-matrix-table",
    )
    assert "2026-01-01–2026-01-05: gap · free text" in table


def test_F49_the_replication_metrics_grid_renders_when_a_tracker_row_has_metrics(
    tmp_path: Path,
) -> None:
    metrics = {
        "annualized_descriptive_residual_alpha": "0.01",
        "annualized_tracking_error": "0.02",
        "cumulative_actual_excess_return": "0.03",
        "cumulative_cost_drag": "0.001",
        "cumulative_gross_tracker_excess_return": "0.04",
        "cumulative_net_tracker_excess_return": "0.039",
        "max_relative_wealth_ratio_drawdown_loss": "0.05",
        "observation_count": 120,
        "total_turnover": "1.5",
    }
    payload = base(
        decomposition_lenses={
            "default_lens": "DYNAMIC_STYLE",
            "modes": {"LIVE_ONLY": []},
        },
        replication_analysis={
            "series": [
                {
                    "availability_status": "AVAILABLE_RESEARCH_REPLICATION",
                    "factor_ids": ["SPY_XS"],
                    "holdout_end": "2026-09-17",
                    "holdout_start": "2026-01-01",
                    "investability_policy": {},
                    "metrics": metrics,
                    "parameter_selection": {},
                    "series_id": "S",
                },
                {
                    "availability_status": "UNAVAILABLE_NOT_CONFIGURED",
                    "blocking_reasons": ["REPLICATION_ROOT_NOT_SUPPLIED"],
                    "series_id": "T",
                },
            ]
        },
    )
    with_metrics = call(tmp_path, payload, "replicationAnalysisMarkup", "S")
    assert (
        "Sealed-holdout replication" in with_metrics
        and "Actual cumulative" in with_metrics
    )
    blocked = call(tmp_path, payload, "replicationAnalysisMarkup", "T")
    assert "Out-of-sample tracker" in blocked and "Actual cumulative" not in blocked
    missing = call(tmp_path, payload, "replicationAnalysisMarkup", "nope")
    assert "No governed replication disposition exists for nope" in missing


# ---- F26: the post-cutoff capture caveat ------------------------------------------------


def test_F26_the_post_cutoff_caveat_is_one_note_not_one_per_row(tmp_path: Path) -> None:
    late = {
        "provider_evidence": {
            "account": {
                "observed_end": CUTOFF,
                "observed_start": "2026-01-03",
                "provider_id": "schwab",
            },
            "generation_as_of_date": "2026-09-18",
            "provider_id": "schwab",
        }
    }
    strategies = [strategy(f"S{index}", **late) for index in range(7)]
    payload = base(strategies=strategies)
    note = call(tmp_path, payload, "postCutoffCaptureNote")
    assert note.count("Post-cutoff capture") == 1
    assert "7 of 7 provider captures are dated after the Sep 17, 2026 cutoff" in note
    cell = call(tmp_path, payload, "evidenceCoverage", strategies[0])
    assert "post-cutoff" not in cell
    assert (
        call(tmp_path, base(strategies=[strategy("S")]), "postCutoffCaptureNote") == ""
    )


def test_F26_the_through_date_comes_from_observed_end_not_the_capture_date(
    tmp_path: Path,
) -> None:
    def late_row(observed_end: str) -> dict[str, Any]:
        return {
            "provider_evidence": {
                "account": {"observed_end": observed_end, "provider_id": "schwab"},
                "generation_as_of_date": "2026-09-18",
                "provider_id": "schwab",
            }
        }

    full = base(strategies=[strategy("A", **late_row(CUTOFF))])
    assert "run through the cutoff" in call(tmp_path, full, "postCutoffCaptureNote")
    short = base(strategies=[strategy("A", **late_row("2026-09-01"))])
    note = call(tmp_path, short, "postCutoffCaptureNote")
    assert "run through the cutoff" not in note
    assert "end on their own dates" in note
    mixed = base(
        strategies=[
            strategy("A", **late_row(CUTOFF)),
            strategy("B", **late_row("2026-09-01")),
        ]
    )
    assert "run through the cutoff" not in call(
        tmp_path, mixed, "postCutoffCaptureNote"
    )


# ---- F27 / F39: the orthogonal tables ----------------------------------------------------


def _orthogonal_row(**attribution: Any) -> dict[str, Any]:
    return {
        "analytics_identities": {"orthogonal": {}},
        "interval_association": {},
        "orthogonal_attribution": {"status": "AVAILABLE", **attribution},
        "series_id": "S",
    }


def test_F27_euler_allocations_are_shares_of_the_total(tmp_path: Path) -> None:
    allocations = {"MKT_RF": 9.03e-05, "HML": 4.18e-06, "SMB": 1.0e-4}
    markup = call(
        tmp_path,
        base(),
        "orthogonalAttributionMarkup",
        _orthogonal_row(
            train_fitted_risk_budget={
                "euler_allocations": allocations,
                "interpretation": "SHARE_OF_FITTED_VARIANCE",
            }
        ),
    )
    table = re.search(
        r'<table id="orthogonal-risk-table">.*?</table>', markup, flags=re.S
    )
    assert table is not None
    total = sum(allocations.values())
    for mode, value in allocations.items():
        assert f"{value / total * 100:.2f}%" in table[0], mode
    assert "0.000" not in table[0]
    assert "Euler allocation (share of total)" in table[0]


def test_F39_static_coefficients_are_listed_once_and_dated_rows_are_capped(
    tmp_path: Path,
) -> None:
    per_date = [
        {
            "date": f"2026-01-{day:02d}",
            "mode_contributions": {"M1": 0.001, "M2": -0.002},
            "partition": "HOLDOUT",
        }
        for day in range(1, 29)
    ] * 6
    row = _orthogonal_row(
        mode_coefficients={"M1": 0.5, "M2": -0.25},
        per_date=per_date,
        maximum_reconciliation_error="9.7e-14",
    )
    markup = call(tmp_path, base(), "orthogonalAttributionMarkup", row)
    table = re.search(
        r'<table id="orthogonal-coefficient-table">.*?</table>', markup, flags=re.S
    )[0]  # type: ignore[index]
    assert table.count("0.500") == 1 and table.count("-0.250") == 1
    assert table.count("static fit") == 120
    assert (
        "Showing the first 120 of 336 dated contribution rows; the coefficients are the static fit listed once above"
        in markup
    )
    # a coefficient list with one entry per date keeps a coefficient on each dated row
    listed = _orthogonal_row(
        mode_coefficients=[{"M1": 0.1}, {"M1": 0.2}], per_date=per_date[:2]
    )
    assert "0.100" in call(tmp_path, base(), "orthogonalAttributionMarkup", listed)


def test_F39_the_reconciliation_error_is_not_printed_as_a_raw_float(
    tmp_path: Path,
) -> None:
    assert call(tmp_path, base(), "smallDecimal", "9.70022673297e-14") == "9.70e-14"
    assert call(tmp_path, base(), "smallDecimal", "0.25") == "0.250000"
    assert call(tmp_path, base(), "smallDecimal", None) == "—"


def test_F48_orthogonal_identity_fallbacks_say_not_published(tmp_path: Path) -> None:
    markup = call(tmp_path, base(), "orthogonalAttributionMarkup", _orthogonal_row())
    assert "orthogonal identity not published" in markup
    assert "method not published" in markup
    assert "UNAVAILABLE_" not in markup
    identified = _orthogonal_row()
    identified["analytics_identities"] = {
        "orthogonal": {"surface": "ORTHOGONAL_RISK_ATTRIBUTION"}
    }
    assert "model id not published · methodology id not published" in call(
        tmp_path, base(), "orthogonalAttributionMarkup", identified
    )


# ---- the dependence view -----------------------------------------------------------------


def _cell(**observed: Any) -> dict[str, Any]:
    return {
        "blend": {
            "conceptual_prior": "0.2",
            "conceptual_prior_note": "n",
            "decision_status": "S",
        },
        "implementation": {"status": "S"},
        "left": "A",
        "observed": {
            "coverage": {
                "common_observations": 171,
                "evidence_layer": "LIVE",
                "frequency": "MEASURED_WEEKDAY",
                "missing_from_left": 1,
                "missing_from_right": 2,
                "observed_end": "2026-09-17",
                "observed_start": "2026-01-02",
            },
            "drawdown_regimes": {},
            "horizons": {
                "252": {
                    "correlation": None,
                    "observations": 190,
                    "observed_end": "2026-09-17",
                    "observed_start": "2025-01-01",
                    "status": "UNAVAILABLE_INSUFFICIENT_HISTORY",
                }
            },
            "pearson": "0.451",
            "rolling_pearson": {"latest": "0.1", "window_observations": 63},
            "spearman": "0.4",
            "status": "AVAILABLE",
            **observed,
        },
        "right": "B",
    }


def _dependence() -> dict[str, Any]:
    return {
        "as_of_date": CUTOFF,
        "available_pair_count": 1,
        "cells": [_cell()],
        "central_case": {"loss": "0.1", "status": "S"},
        "correlation_one": {
            "components": [
                {
                    "allocation_weight": "0.6",
                    "conservative_loss_fraction": "0.5",
                    "loss_contribution": "0.3",
                    "series_id": "A",
                },
                {
                    "allocation_weight": "0.4",
                    "conservative_loss_fraction": "0.5",
                    "loss_contribution": "0.071",
                    "series_id": "B",
                },
            ],
            "loss": "0.371",
            "method": "CONSERVATIVE_BOUND",
            "reconciliation_difference": "0",
            "status": "S",
        },
        "drawdown_matrices": {},
        "economic_driver_cluster_budget": {
            "budget_rule": "ONE_AGGREGATE_BUDGET",
            "clusters": [
                {
                    "allocation_weight": "0.6",
                    "budget_status": "S",
                    "cluster_id": "c1",
                    "label": "C1",
                    "loss_contribution": "0.2",
                    "members": ["A"],
                    "rationale": "r",
                },
                {
                    "allocation_weight": "0.4",
                    "budget_status": "S",
                    "cluster_id": "c2",
                    "label": "C2",
                    "loss_contribution": "0.0655",
                    "members": ["B"],
                    "rationale": "r",
                },
            ],
            "historical_audit_reference": {
                "after_rejected_sleeves_loss": "0.3",
                "full_systematic_loss": "0.4",
            },
            "limit_breach": "0.22",
            "status": "S",
            "systematic_loss_limit": "0.15",
        },
        "generation_id": "g" * 20,
        "horizon_matrices": {
            "252": {
                "classification": "MEASURED_WEEKDAY_OBSERVATION_WINDOW",
                "values": [[1, None], [None, 1]],
            }
        },
        "matrices": {
            "observed_pearson": {
                "classification": "MEASURED",
                "values": [[1, 0.451], [0.451, 1]],
            }
        },
        "pair_count": 1,
        "series_ids": ["A", "B"],
    }


def _dependence_payload(**overrides: Any) -> dict[str, Any]:
    dependence = _dependence()
    payload = base(
        default_dependence_mode="LIVE_PLUS_BACKTEST",
        dependence_analysis=dependence,
        dependence_modes={"LIVE_ONLY": dependence, "LIVE_PLUS_BACKTEST": dependence},
        hybrid_mode_gate={
            "blocked_strategies": [
                {
                    "compatibility_status": "UNAVAILABLE_NO_ACCEPTED_HISTORY",
                    "reason": "none",
                    "strategy_name": "Volatis",
                }
            ],
            "eligible_strategies": [],
            "reason": "LIVE_PLUS_BACKTEST is enabled independently for 6 strategy history path(s)",
            "status": "ELIGIBLE_PARTIAL_VERSION_COMPATIBILITY",
        },
    )
    payload.update(overrides)
    return payload


def test_F28_an_unavailable_horizon_shows_no_value_and_never_the_full_sample_pearson(
    tmp_path: Path,
) -> None:
    payload = _dependence_payload()
    selected = run(
        tmp_path,
        payload,
        calls=[
            {"eval": "selectedCorrelationWindow = '252'"},
            {"fn": "renderCorrelationDetail", "args": ["A", "B"]},
        ],
    )
    detail = html(selected, "#correlation-detail")
    assert re.search(r"Selected period</dt><dd>—</dd>", detail)
    assert (
        "Selected-period status" in detail and "Insufficient accepted history" in detail
    )
    assert "190 weekday observations" in detail
    assert re.search(r"Full-overlap Pearson</dt><dd>0\.451</dd>", detail)
    full = run(
        tmp_path, payload, calls=[{"fn": "renderCorrelationDetail", "args": ["A", "B"]}]
    )
    assert re.search(
        r"Selected period</dt><dd>0\.451</dd>", html(full, "#correlation-detail")
    )
    assert "Selected-period status" not in html(full, "#correlation-detail")
    assert "Missing observations left / right" in html(full, "#correlation-detail")


def test_F28_the_matrix_label_counts_weekday_observations_not_sessions(
    tmp_path: Path,
) -> None:
    result = run(
        tmp_path,
        _dependence_payload(),
        calls=[
            {"eval": "selectedCorrelationWindow = '252'"},
            {"fn": "renderCorrelationMatrix"},
        ],
    )
    assert (
        result["dom"]["#matrix-title"]["textContent"]
        == "Observed Pearson · 252 weekday observations"
    )
    assert "sessions" not in html(result, "#correlation-matrix")


def test_F45_a_partly_eligible_hybrid_view_names_the_strategies_left_out(
    tmp_path: Path,
) -> None:
    result = run(tmp_path, _dependence_payload(), "renderDependence")
    boundary = result["dom"]["#evidence-mode-boundary"]["textContent"]
    assert (
        "Excluded from this view (live only): Volatis (unavailable no accepted history)"
        in boundary
    )
    live = run(
        tmp_path,
        _dependence_payload(default_dependence_mode="LIVE_ONLY"),
        "renderDependence",
    )
    assert "Volatis" not in live["dom"]["#evidence-mode-boundary"]["textContent"]


# ---- F30 / F31 / F32: the stress bar and the policy statements ---------------------------


def test_F30_the_stress_bar_scale_and_marker_follow_the_governed_limit(
    tmp_path: Path,
) -> None:
    def bar(stress: str, limit: str) -> dict[str, Any]:
        advisory = {
            **base()["advisory"],
            "correlation_one_stress": stress,
            "hwm_limit": limit,
        }
        return run(tmp_path, _risk_payload(advisory=advisory), "renderRisk")

    result = bar("0.10", "0.20")
    markup = html(result, "#stress-bar")
    assert 'class="scenario-limit" aria-hidden="true" style="left:37.50%"' in markup
    assert "width:18.75%" in markup
    assert result["dom"]["#stress-limit-badge"]["textContent"] == "20.0% HWM boundary"
    # the same stress against the original limit still reproduces the audited picture
    original = html(bar("0.370858948862", "0.15"), "#stress-bar")
    assert "width:92.71%" in original and "left:37.50%" in original
    # no negative width, and a stress beyond the scale is clamped
    assert "width:0.00%" in html(bar("-0.05", "0.15"), "#stress-bar")
    assert "width:100.00%" in html(bar("0.90", "0.15"), "#stress-bar")
    assert "scenario-limit" not in html(bar("0.10", "unknown"), "#stress-bar")


def test_F31_the_reserve_risk_budget_literal_is_marked_as_policy_text() -> None:
    page = (PANEL_ROOT / "index.html").read_bytes().decode("utf-8")
    assert re.search(
        r'<div class="reserve-lock policy-text" title="Policy statement[^"]*">', page
    )
    assert (
        'class="policy-text" title="Policy statement; the payload carries no field for it">NONE</strong>'
        in page
    )
    assert '<blockquote class="policy-text"' in page


def test_F32_the_advisory_effects_are_bound_to_the_underwriting_rows(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "advisory_disposition": {
                "allocation_effect": "NONE",
                "execution_effect": "NONE",
            }
        }
    ]
    declared = base(strategy_underwriting={"rows": rows})
    assert call(tmp_path, declared, "advisoryExecutionEffect") == "NONE"
    assert "declared by every underwriting row" in call(
        tmp_path, declared, "advisoryExecutionEffectNote"
    )
    changed = base(
        strategy_underwriting={
            "rows": [{"advisory_disposition": {"execution_effect": "REVIEW_REQUIRED"}}]
        }
    )
    assert call(tmp_path, changed, "advisoryExecutionEffect") == "REVIEW_REQUIRED"
    none = base()
    assert "Policy statement" in call(tmp_path, none, "advisoryExecutionEffectNote")


def test_F32_the_mwr_note_and_external_connections_say_what_they_are_bound_to(
    tmp_path: Path,
) -> None:
    kpis = text(run(tmp_path, _risk_payload(), "renderRisk"), "#risk-kpis")
    assert "flow fact partial" in kpis
    data_view = text(run(tmp_path, _data_payload(), "renderData"), "#data-kpis")
    assert (
        "Policy statement: direct-file local operation; not counted from the payload"
        in data_view
    )


# ---- F34: raw machine tokens in operator cells -------------------------------------------


def test_F34_machine_tokens_are_respelled_in_operator_cells(tmp_path: Path) -> None:
    cost = call(
        tmp_path,
        base(),
        "costDisclosure",
        {
            "cost_status": "unknown_model_subscription_cost",
            "fees_status": "X",
            "after_tax_status": "Y",
        },
    )
    assert "Unknown model subscription cost" in cost
    assert "unknown_model_subscription_cost" not in cost
    kpis = text(
        run(
            tmp_path,
            base(counts={**base()["counts"], "positions": 3}),
            "renderCapitalKpis",
        ),
        "#capital-kpis",
    )
    assert "Partial" in kpis and "PARTIAL" not in kpis
    leverage = html(
        run(
            tmp_path,
            base(
                leverage_disclosures=[
                    _leverage_row(
                        data_class="FINANCING_COST", measurement_basis="SOURCE_REPORTED"
                    )
                ]
            ),
            "renderLeverageDisclosures",
        ),
        "#leverage-table",
    )
    assert (
        "Financing cost" in leverage
        and "Source reported" in leverage
        and "FINANCING_COST" not in leverage
    )


# ---- F36 / F37: the reconciliation and remediation tables --------------------------------


def test_F36_the_evidence_cell_shows_the_value_beside_the_gap_and_the_checks_numbers(
    tmp_path: Path,
) -> None:
    payload = base(
        blocking_items=[
            {
                "item": "positions-partial",
                "known_gap": "Partial positions",
                "status": "RECONCILIATION_EXCEPTION",
                "value": "509",
            }
        ],
        reconciliation_checks=[
            {
                "check_id": "capital-identity",
                "maximum_absolute_difference": None,
                "status": "NOT_READY",
                "tolerance": None,
            },
            {
                "check_id": "flows",
                "maximum_absolute_difference": "1.5e-9",
                "status": "PASS",
                "tolerance": "0.01",
            },
        ],
        counts={**base()["counts"], "reconciliation_checks": 2},
    )
    table = html(run(tmp_path, payload, "renderSignals"), "#reconciliation-table")
    plain = re.sub(r"<[^>]+>", "|", table)
    assert "Partial positions · value 509" in plain
    assert "tolerance not published · difference not published" in plain
    assert "tolerance 0.01 · maximum absolute difference 1.50e-9" in plain


def _data_payload(**overrides: Any) -> dict[str, Any]:
    payload = base(
        candidate_appendix={
            "as_of_date": "2026-07-17",
            "providers": [],
            "source_sha256": "a" * 64,
            "status": "FROZEN_AUDIT_APPENDIX",
            "tier_counts": {},
        },
        coverage=[],
    )
    payload["meta"]["freshness_evaluated_on"] = CUTOFF
    payload.update(overrides)
    return payload


def test_F37_remediation_routes_by_item_id_and_reads_freshness_fields_from_coverage(
    tmp_path: Path,
) -> None:
    items = [
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "flow-coverage-partial",
            "known_gap": "g",
            "status": "S",
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "unrouted-item",
            "known_gap": "g",
            "status": "S",
        },
        {
            "category": "RECONCILIATION_EXCEPTION",
            "item": "stale-fact",
            "known_gap": "g",
            "status": "S",
        },
    ]
    coverage = [
        {
            "category": "DATA_FRESHNESS",
            "fact_type": "positions",
            "item": "stale-fact",
            "policy_id": "FRESH-1",
            "reconciled_on": CUTOFF,
            "status": "S",
        }
    ]
    table = text(
        run(
            tmp_path,
            _data_payload(blocking_items=items, coverage=coverage),
            "renderData",
        ),
        "#remediation-table",
    )
    assert (
        "Obtain the missing cash-flow records, or formally disposition the gap" in table
    )
    assert (
        "Resolve the declared coverage exception in its producing generation" in table
    )
    assert (
        "Refresh or formally disposition the required Positions evidence under FRESH-1; reconciled Sep 17, 2026"
        in table
    )
    assert "undefined" not in table


# ---- F40 / F47: the download error and the view selector ---------------------------------


def test_F40_a_failed_embedded_download_shows_a_toast_not_only_a_console_line() -> None:
    app = (PANEL_ROOT / "app.js").read_bytes().decode("utf-8")
    body = app[
        app.index("function bindEmbeddedDownloads") : app.index("function init()")
    ]
    assert body.count("showToast(") == 2
    assert re.search(
        r'console\.error\("Embedded download payload could not be decoded\.", error\);\s+showToast\(',
        body,
    )
    assert re.search(
        r'console\.error\("Embedded download metadata is invalid\."\);\s+showToast\(',
        body,
    )
    # #540: chunks are decoded one at a time (never joined into one string), the
    # gzip report is inflated with DecompressionStream, and the inflated length
    # must equal the declared raw size.
    assert "EMBEDDED_DOWNLOAD_MAX_CHUNKS" in body
    assert "chunks.join(" not in body
    assert 'new DecompressionStream("gzip")' in body
    assert "blob.size !== rawBytes" in body


def test_F47_a_hash_with_quotes_cannot_break_the_view_selector(tmp_path: Path) -> None:
    selector = call(tmp_path, base(), "viewSelector", 'x"] , [data-view="y')
    assert selector.startswith('[data-view="') and selector.endswith('"]')
    inner = selector[len('[data-view="') : -2]
    assert '"' not in inner.replace('\\"', "")
    assert call(tmp_path, base(), "viewSelector", "risk") == '[data-view="risk"]'


def test_F47_tables_built_by_a_render_become_keyboard_regions(tmp_path: Path) -> None:
    result = run(
        tmp_path,
        base(),
        calls=[
            {"eval": "viewRenderers.command = () => {}; renderView('command'); true"}
        ],
    )
    assert ".table-wrap, .matrix-shell, .weekly-table-shell" in result["queries"]


# ---- F48: the performance comparison scale is named ---------------------------------------


def test_F48_the_comparison_window_label_reads_the_named_constant(
    tmp_path: Path,
) -> None:
    result = run(tmp_path, _risk_payload(), "renderPerformanceComparison")
    assert "13-week strategy return" in text(result, "#performance-comparison")
    app = (PANEL_ROOT / "app.js").read_bytes().decode("utf-8")
    assert "const COMPARISON_WEEKS = 13;" in app and ".slice(-13)" not in app
    assert "/ 0.08)" not in app


# ---- F25: the stylesheet has an in-repo source when one is committed ---------------------


def _builder() -> Any:
    import importlib.util

    path = REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py"
    name = "portfolio_control_panel_f25"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_F25_the_builder_prefers_an_in_repo_styles_css_and_falls_back_to_the_legacy_input(
    tmp_path: Path,
) -> None:
    import hashlib

    builder = _builder()
    legacy = (tmp_path / "legacy" / "styles.css", b"legacy{}\n", "0" * 64)
    templates = tmp_path / "templates"
    templates.mkdir()
    # no committed stylesheet: the legacy input is used unchanged
    assert builder._frontend_styles_source(templates, legacy) == legacy
    # a committed stylesheet wins, is normalised to LF like the other templates, and is bound
    # by its raw digest and its own path
    raw = b"body{color:red}\r\n"
    (templates / "styles.css").write_bytes(raw)
    path, content, digest = builder._frontend_styles_source(templates, legacy)
    assert path == templates / "styles.css"
    assert content == b"body{color:red}\n"
    assert digest == hashlib.sha256(raw).hexdigest()


def test_F25_the_builder_reads_the_stylesheet_through_the_selector() -> None:
    source = (
        (REPOSITORY_ROOT / "tools" / "portfolio_control_panel.py")
        .read_bytes()
        .decode("utf-8")
    )
    assert (
        '_frontend_styles_source(template_root, legacy_snapshots["source:styles.css"])'
        in source
    )
    assert '"styles.css": legacy_styles_content,' in source


def test_mockup_F_risk_shares_are_bars_and_contributions_are_one_stacked_chart(
    tmp_path: Path,
) -> None:
    per_date = [
        {
            "date": f"2026-01-{day:02d}",
            "partition": "TRAIN",
            "mode_contributions": {"MKT_RF": 0.001 * day, "HML": -0.0004 * day},
        }
        for day in range(2, 8)
    ]
    markup = call(
        tmp_path,
        base(),
        "orthogonalAttributionMarkup",
        _orthogonal_row(
            mode_coefficients={"MKT_RF": 0.01, "HML": -0.002},
            per_date=per_date,
            train_fitted_risk_budget={
                "euler_allocations": {"MKT_RF": 9.0e-05, "HML": 1.0e-05},
                "interpretation": "SHARE_OF_FITTED_VARIANCE",
            },
        ),
    )
    assert "Share of total risk (Euler allocation)" in markup
    assert markup.count('class="risk-share-row"') == 2
    assert "90.00%" in markup and "10.00%" in markup
    assert "Mode loadings (training window)" in markup
    assert "Daily contribution by mode" in markup
    assert markup.count("<rect ") == 12
    # the rows behind the chart sit in a closed disclosure that says how many there are
    disclosure = re.search(
        r"<details class=\"cell-details orthogonal-data\">.*?</details>",
        markup,
        flags=re.S,
    )
    assert disclosure is not None
    assert "12 dated contribution rows" in disclosure[0]
    assert 'id="orthogonal-risk-table"' in disclosure[0]
    assert '<details class="cell-details orthogonal-data" open' not in markup

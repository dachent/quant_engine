"""Rendered-browser proof that a lineage mismatch withholds report performance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from decimal import Decimal, localcontext

import pytest

from browser_runtime import resolve_browser_runtime

REPO = Path(__file__).parents[2]
PANEL_ROOT = REPO / "apps" / "portfolio-control-panel"
CURRENT_FIXTURE = (
    REPO
    / ".superpowers"
    / "sdd"
    / "2026-08-25-real-epoch-manifest"
    / "browser-display-evidence"
    / "browser-fixture"
)


def _matrix_rows(matrix: dict[str, object]) -> list[list[str]]:
    return [
        list(row) if isinstance(row, list) else str(row).split(" ")
        for row in matrix["values"]
    ]


def _quadratic_loss(matrix: dict[str, object], inputs: list[Decimal]) -> Decimal:
    rows = _matrix_rows(matrix)
    with localcontext() as context:
        context.prec = 40
        return sum(
            inputs[left] * Decimal(rows[left][right]) * inputs[right]
            for left in range(len(inputs))
            for right in range(len(inputs))
        ).sqrt()


def _browser_runtime() -> tuple[str, str, str] | None:
    return resolve_browser_runtime(REPO)


def _write_dashboard(candidate: Path, data: dict[str, object]) -> None:
    serialized = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    (candidate / "data" / "dashboard_data.json").write_text(
        serialized + "\n", encoding="utf-8"
    )
    (candidate / "data" / "dashboard_data.js").write_text(
        "window.PORTFOLIO_DATA = " + serialized + ";\n", encoding="utf-8"
    )


def _project_current_strategy_fixture(data: dict[str, object]) -> None:
    """Keep the fixture's current seven-strategy epoch and seed retained numerics."""

    current_names_by_id = {
        "p123-ai-factor": "AI Factor [P123]",
        "tt-portfolio": "TT Portfolio",
        "qsi": "QSI",
        "volatis-strategy": "Volatis Strategy",
        "grizzlybears-vtma": "GrizzlyBears VTMA",
        "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
        "p123-eu": "EU [P123]",
    }
    current_ids = {row["strategy_id"] for row in data["account_manifest"]["rows"]}
    assert current_ids == set(current_names_by_id)
    legacy_to_current = {
        row["strategy_id"]: current_id
        for row in data["strategies"]
        for current_id, name in current_names_by_id.items()
        if row["strategy_name"] == name
    }
    assert len(legacy_to_current) == len(current_ids)

    def rewrite_current(row: dict[str, object]) -> dict[str, object]:
        return {**row, "strategy_id": legacy_to_current[str(row["strategy_id"])]}

    strategies = [
        rewrite_current(row)
        for row in data["strategies"]
        if row["strategy_id"] in legacy_to_current
    ]
    current_names = set(current_names_by_id.values())
    data["strategies"] = strategies
    data["performance"] = [
        {
            **(
                rewrite_current(row)
                if row.get("strategy_id") in legacy_to_current
                else row
            ),
            "mwr_status": "AVAILABLE",
            "mwr_xirr": "0.03125",
        }
        for row in data["performance"]
        if row.get("strategy_id") in legacy_to_current
        or row.get("strategy_mapping_disposition") == "RESERVE_NOT_APPLICABLE"
    ]
    data["allocations"] = [
        rewrite_current(row) if row.get("strategy_id") in legacy_to_current else row
        for row in data["allocations"]
        if row.get("strategy_id") in legacy_to_current or row.get("scope") == "RESERVE"
    ]
    data["positions"] = [
        rewrite_current(row)
        for row in data["positions"]
        if row.get("strategy_id") in legacy_to_current
    ]
    underwriting = data["strategy_underwriting"]
    underwriting["rows"] = [
        rewrite_current(row)
        for row in underwriting["rows"]
        if row.get("strategy_id") in legacy_to_current
    ]
    underwriting["counts"]["strategies"] = len(strategies)
    hwm_summary = data["hwm_summary"]
    hwm_summary["uncovered_details"] = [
        rewrite_current(row)
        for row in hwm_summary.get("uncovered_details", [])
        if row.get("strategy_id") in legacy_to_current
    ]
    hwm_summary["excluded_details"] = [
        rewrite_current(row)
        for row in hwm_summary.get("excluded_details", [])
        if row.get("strategy_id") in legacy_to_current
    ]
    hwm_summary["uncovered_strategies"] = [
        row["strategy_name"] for row in hwm_summary["uncovered_details"]
    ]
    hwm_summary["excluded_strategies"] = [
        row["strategy_name"] for row in hwm_summary["excluded_details"]
    ]
    data["counts"]["strategies"] = len(strategies)
    strategy_series = [
        row
        for row in data["weekly_returns"]["series"]
        if row["primary_group"] == "STRATEGY" and row["display_name"] in current_names
    ]
    assert len(strategy_series) == len(current_ids) == 7
    weeks = [
        f"2026-{month:02d}-{day:02d}"
        for month, day in (
            (5, 4),
            (5, 11),
            (5, 18),
            (5, 25),
            (6, 1),
            (6, 8),
            (6, 15),
            (6, 22),
            (6, 29),
            (7, 6),
            (7, 13),
            (7, 20),
            (7, 27),
            (8, 3),
        )
    ]
    for index, row in enumerate(strategy_series):
        values = {week: f"0.0{index + 1}" for week in weeks}
        metadata = {week: [5, week, week, "COMPLETE", []] for week in weeks[:-1]}
        metadata[weeks[-1]] = [
            2,
            weeks[-1],
            weeks[-1],
            "PARTIAL_BOUNDARY",
            ["CURRENT_AS_OF"],
        ]
        row["values_by_mode"] = {"LIVE_ONLY": values}
        row["observation_meta_by_mode"] = {"LIVE_ONLY": metadata}
        row["mode_coverage"] = {
            "LIVE_ONLY": {"status": "AVAILABLE", "weekly_observations": len(weeks)}
        }
        row["calendar_basis_by_mode"] = {"LIVE_ONLY": "WEEKDAY"}
    data["weekly_returns"]["series"] = strategy_series + [
        row
        for row in data["weekly_returns"]["series"]
        if row["primary_group"] != "STRATEGY"
    ]
    data["weekly_returns"]["weeks"] = weeks
    data["weekly_returns"]["current_week"] = {
        "default_visible": True,
        "data_through_date": data["weekly_returns"]["as_of_date"],
        "is_partial": True,
        "week_end": "2026-08-09",
        "week_start": weeks[-1],
    }

    def fixed_decimal(value: Decimal) -> str:
        return format(value, "f")

    def project_dependence(dependence: dict[str, object]) -> dict[str, Decimal]:
        source_ids = list(dependence["series_ids"])
        retained_indices = [
            index for index, name in enumerate(source_ids) if name in current_names
        ]
        retained_names = [source_ids[index] for index in retained_indices]
        assert retained_indices == [0, 1, 2, 3, 4, 5, 7]

        def slice_matrix(
            matrix: dict[str, object], *, seed_robuxio: bool = False
        ) -> None:
            source_rows = _matrix_rows(matrix)
            assert len(source_rows) == len(source_ids) == 9
            assert all(len(row) == len(source_ids) for row in source_rows)
            if seed_robuxio:
                source_rows[0][6] = source_rows[6][0] = "0.66"
                source_rows[0][7] = source_rows[7][0] = "0.77"
            matrix["values"] = [
                [source_rows[left][right] for right in retained_indices]
                for left in retained_indices
            ]

        for name, matrix in dependence["matrices"].items():
            slice_matrix(matrix, seed_robuxio=name == "observed_pearson")
        for matrix in dependence["horizon_matrices"].values():
            slice_matrix(matrix)
        for matrix in dependence["drawdown_matrices"].values():
            slice_matrix(matrix)
        dependence["series_ids"] = retained_names
        dependence["cells"] = [
            row
            for row in dependence["cells"]
            if row["left"] in current_names and row["right"] in current_names
        ]
        assert len(dependence["cells"]) == 21
        dependence["pair_count"] = 21
        measured_pairs = sum(
            row.get("observed", {}).get("pearson") is not None
            for row in dependence["cells"]
        )
        dependence["available_pair_count"] = measured_pairs

        with localcontext() as context:
            context.prec = 40
            weights = [
                Decimal("0.16"),
                Decimal("0.14"),
                Decimal("0.14"),
                Decimal("0.14"),
                Decimal("0.14"),
                Decimal("0.14"),
                Decimal("0.14"),
            ]
            conservative_losses = [
                Decimal("0.22"),
                Decimal("0.23"),
                Decimal("0.24"),
                Decimal("0.25"),
                Decimal("0.26"),
                Decimal("0.27"),
                Decimal("0.31"),
            ]
            correlation_loss = sum(
                weight * loss for weight, loss in zip(weights, conservative_losses)
            )
            central_inputs = [
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0.12"),
            ]
            posterior_rows = _matrix_rows(dependence["matrices"]["posterior"])
            central_loss = sum(
                central_inputs[left]
                * Decimal(posterior_rows[left][right])
                * central_inputs[right]
                for left in range(len(retained_names))
                for right in range(len(retained_names))
            ).sqrt()
        correlation_components = [
            {
                "allocation_weight": fixed_decimal(weight),
                "conservative_loss_fraction": fixed_decimal(loss),
                "loss_contribution": fixed_decimal(weight * loss),
                "series_id": name,
            }
            for name, weight, loss in zip(retained_names, weights, conservative_losses)
        ]
        dependence["correlation_one"].update(
            {
                "components": correlation_components,
                "declared_loss": fixed_decimal(correlation_loss),
                "loss": fixed_decimal(correlation_loss),
                "reconciliation_difference": "0",
            }
        )
        dependence["central_case"].update(
            {
                "components": [
                    {
                        "series_id": name,
                        "status": "MEASURED",
                        "loss_input": fixed_decimal(loss_input),
                    }
                    for name, loss_input in zip(retained_names, central_inputs)
                ],
                "loss": fixed_decimal(central_loss),
                "measured_component_count": len(retained_names),
                "measured_pair_count": measured_pairs,
                "prior_component_count": 0,
                "prior_pair_count": 21 - measured_pairs,
            }
        )
        cluster = dependence["economic_driver_cluster_budget"]["clusters"][0]
        cluster["members"] = retained_names
        cluster["pair_evidence"] = [
            row
            for row in cluster["pair_evidence"]
            if row["left"] in current_names and row["right"] in current_names
        ]
        cluster["loss_contribution"] = fixed_decimal(correlation_loss)
        dependence["economic_driver_cluster_budget"].update(
            {
                "central_case_loss": fixed_decimal(central_loss),
                "correlation_one_loss": fixed_decimal(correlation_loss),
                "limit_breach": fixed_decimal(correlation_loss - Decimal("0.15")),
            }
        )
        dependence["economic_driver_cluster_budget"]["historical_audit_reference"][
            "full_systematic_loss"
        ] = fixed_decimal(correlation_loss)
        return {"central_loss": central_loss, "correlation_loss": correlation_loss}

    projected_dependence = project_dependence(data["dependence_analysis"])
    for dependence in data["dependence_modes"].values():
        mode_dependence = project_dependence(dependence)
        assert mode_dependence == projected_dependence
    data["advisory"]["correlation_one_stress"] = fixed_decimal(
        projected_dependence["correlation_loss"]
    )
    component_by_name = {
        row["series_id"]: row
        for row in data["dependence_analysis"]["correlation_one"]["components"]
    }
    for collection in (data["strategies"], data["strategy_underwriting"]["rows"]):
        for row in collection:
            component = component_by_name[row["strategy_name"]]
            portfolio_fit = row.get("portfolio_fit", row)
            portfolio_fit["strategy_correlation_one_loss_contribution"] = component[
                "loss_contribution"
            ]
            portfolio_fit["strategy_share_of_correlation_one_loss"] = fixed_decimal(
                Decimal(component["loss_contribution"])
                / projected_dependence["correlation_loss"]
            )
            portfolio_fit["cluster_loss_contribution"] = fixed_decimal(
                projected_dependence["correlation_loss"]
            )

    excluded_identity = re.compile(r"echo|composer", re.IGNORECASE)
    identity_keys = {
        "strategy_id",
        "strategy_name",
        "series_id",
        "display_name",
        "left",
        "right",
    }
    identity_lists = {
        "members",
        "series_ids",
        "uncovered_strategies",
        "excluded_strategies",
    }
    drop = object()

    def project_value(value: object, parent_key: str | None = None) -> object:
        if isinstance(value, dict):
            if any(
                key in identity_keys
                and isinstance(item, str)
                and excluded_identity.search(item)
                for key, item in value.items()
            ):
                return drop
            return {
                key: projected
                for key, item in value.items()
                if (projected := project_value(item, key)) is not drop
            }
        if isinstance(value, list):
            return [
                projected
                for item in value
                if (projected := project_value(item, parent_key)) is not drop
            ]
        if isinstance(value, str) and excluded_identity.search(value):
            return (
                drop
                if parent_key in identity_lists
                else "Non-current strategy identities withheld from the current fixture."
            )
        return value

    projected = project_value(data)
    assert projected is not drop
    data.clear()
    data.update(projected)


def _rendered_evidence(
    candidate: Path, runtime: tuple[str, str, str]
) -> dict[str, object]:
    node, module, executable = runtime
    probe = r"""
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(pathToFileURL(process.env.PANEL_HTML).href + "#risk", { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
  await page.locator("#context-strategy").selectOption({ index: 1 });
  await page.locator('[data-nav="dependence"]').click();
  const dependence = {
    matrixCells: await page.locator("[data-correlation-left]").count(),
    robuxioCell: await page.locator("[data-correlation-left]").evaluateAll((nodes) => (
      nodes.find((node) => node.dataset.correlationLeft === "AI Factor [P123]" && node.dataset.correlationRight === "Robuxio Crypto (High Sharpe High Vol)")?.textContent ?? ""
    )),
  };
  await page.locator('[data-nav="weekly"]').click();
  await page.locator("#weekly-current-toggle").check();
  await page.locator("[data-weekly-series]").first().click();
  const evidence = await page.evaluate(() => ({
    performanceRows: Array.from(document.querySelectorAll("#performance-table tbody tr"), (row) => Array.from(row.cells, (cell) => ({
      primary: cell.childNodes[0]?.textContent?.trim() ?? "",
      text: cell.textContent?.trim() ?? "",
    }))),
    performanceHeaders: Array.from(document.querySelectorAll("#performance-table thead th"), (header) => header.textContent?.trim() ?? ""),
    performanceText: document.querySelector("#performance-table")?.textContent?.trim() ?? "",
    hwmText: document.querySelector("#hwm-status")?.textContent?.trim() ?? "",
    strategyText: document.querySelector("#strategy-table")?.textContent?.trim() ?? "",
    implementationRows: Array.from(document.querySelectorAll("#implementation-table tbody tr"), (row) => Array.from(row.cells, (cell) => ({
      primary: cell.childNodes[0]?.textContent?.trim() ?? "",
      text: cell.textContent?.trim() ?? "",
    }))),
    comparison: Array.from(document.querySelectorAll("#performance-comparison .metric-block"), (block) => ({
      label: block.querySelector(".metric-label")?.textContent?.trim() ?? "",
      value: block.querySelector(".metric-value")?.textContent?.trim() ?? "",
      note: block.querySelector(".metric-note")?.textContent?.trim() ?? "",
    })),
    epochRows: Array.from(document.querySelectorAll("#account-manifest-table tbody tr"), (row) => ({
      strategy: row.cells[1]?.textContent?.trim() ?? "",
    })),
    strategyCoverageText: document.querySelector("#strategy-coverage-bars")?.textContent?.trim() ?? "",
    command: {
      reason: document.querySelector("#command-reason")?.textContent?.trim() ?? "",
      kpis: document.querySelector("#command-kpis")?.textContent?.trim() ?? "",
      gates: document.querySelector("#gate-stack")?.textContent?.trim() ?? "",
      operator: document.querySelector("#operator-queue")?.textContent?.trim() ?? "",
    },
    weekly: {
      detail: document.querySelector("#weekly-detail")?.textContent?.trim() ?? "",
      rows: Array.from(document.querySelectorAll("#weekly-table tbody tr"), (row) => ({
        cells: Array.from(row.querySelectorAll(".weekly-cell"), (cell) => cell.textContent?.trim() ?? ""),
        label: row.querySelector("th")?.textContent?.trim() ?? "",
      })),
      text: document.querySelector("#weekly-table")?.textContent?.trim() ?? "",
    },
  }));
  evidence.dependence = dependence;
  console.log(JSON.stringify(evidence));
} finally {
  await browser.close();
}
"""
    env = dict(
        os.environ,
        PLAYWRIGHT_MODULE=module,
        PLAYWRIGHT_EXECUTABLE_PATH=executable,
        PANEL_HTML=str(candidate / "index.html"),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _interaction_evidence(
    candidate: Path, runtime: tuple[str, str, str], output: Path, *, epoch_only: bool
) -> dict[str, object]:
    node, module, executable = runtime
    dashboard = json.loads(
        (candidate / "data/dashboard_data.json").read_text(encoding="utf-8")
    )
    cutoff = dashboard["account_manifest"]["calculation_cutoff_date"]
    env = dict(
        os.environ, PLAYWRIGHT_MODULE=module, PLAYWRIGHT_EXECUTABLE_PATH=executable
    )
    result = subprocess.run(
        [
            node,
            str(PANEL_ROOT / "interaction_qa.mjs"),
            "--html",
            str(candidate / "index.html"),
            "--output",
            str(output),
            "--exclude-candidates",
            "true",
            "--expected-calculation-cutoff",
            cutoff,
            *(["--epoch-only", "true"] if epoch_only else []),
        ],
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=90,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_browser_withholds_adversarial_report_performance_on_lineage_mismatch(
    tmp_path: Path,
) -> None:
    """Changing only lineage must suppress retained numeric/AVAILABLE report fields."""

    runtime = _browser_runtime()
    if runtime is None:
        pytest.skip("Node, Playwright module, or Chromium runtime is unavailable")
    selected_fixture = os.environ.get("PORTFOLIO_BROWSER_FIXTURE")
    fixture = Path(selected_fixture).resolve() if selected_fixture else CURRENT_FIXTURE
    if selected_fixture is None and not fixture.is_dir():
        pytest.skip(
            "Retained financial browser fixture is unavailable; "
            "set PORTFOLIO_BROWSER_FIXTURE for financial integration QA"
        )
    assert fixture.is_dir(), "selected 13-row browser fixture is required"
    candidate = tmp_path / "current-dashboard"
    shutil.copytree(fixture, candidate)
    shutil.copy2(PANEL_ROOT / "app.js", candidate / "app.js")
    data = json.loads(
        (candidate / "data" / "dashboard_data.json").read_text(encoding="utf-8")
    )
    _project_current_strategy_fixture(data)
    _write_dashboard(candidate, data)
    manifest_rows = data["account_manifest"]["rows"]
    assert len(manifest_rows) == 13
    assert len({row["strategy_id"] for row in manifest_rows}) == 7
    assert not any(
        "echo" in row["strategy_id"].lower() or "composer" in row["strategy_id"].lower()
        for row in manifest_rows
    )
    assert len(data["strategies"]) == 7
    assert (
        len(
            [
                row
                for row in data["weekly_returns"]["series"]
                if row["primary_group"] == "STRATEGY"
            ]
        )
        == 7
    )
    assert len(data["weekly_returns"]["series"]) == 21
    assert len(data["dependence_analysis"]["series_ids"]) == 7
    assert len(data["dependence_analysis"]["cells"]) == 21
    assert len(data["dependence_modes"]["LIVE_ONLY"]["series_ids"]) == 7
    assert len(data["dependence_modes"]["LIVE_ONLY"]["cells"]) == 21
    assert len(data["factor_analysis"]["series"]) == 7
    assert len(data["replication_analysis"]["series"]) == 7

    def lens_series(lens):  # #540: surfaces carry the series once
        return (
            lens["series"]
            if "series" in lens
            else lens["surfaces"][lens["default_surface"]]["series"]
        )

    assert all(
        len(lens_series(lens)) == 7
        for lens in data["decomposition_lenses"]["modes"]["LIVE_ONLY"]
    )
    assert all(
        len(mode["series_ids"]) == 7 and len(mode["cells"]) == 21
        for mode in data["dependence_modes"].values()
    )
    assert all(
        all(len(lens["series"]) == 7 for lens in lenses)
        for lenses in data["decomposition_lenses"]["modes"].values()
    )
    for dependence in [data["dependence_analysis"], *data["dependence_modes"].values()]:
        for matrix in [
            *dependence["matrices"].values(),
            *dependence["horizon_matrices"].values(),
            *dependence["drawdown_matrices"].values(),
        ]:
            rows = _matrix_rows(matrix)
            assert len(rows) == 7
            assert all(len(row) == 7 for row in rows)
        assert dependence["series_ids"][6] == "Robuxio Crypto (High Sharpe High Vol)"
        assert _matrix_rows(dependence["matrices"]["observed_pearson"])[0][6] == "0.77"
        assert _matrix_rows(dependence["matrices"]["observed_pearson"])[0][6] != "0.66"
        correlation_one = dependence["correlation_one"]
        assert len(correlation_one["components"]) == 7
        with localcontext() as context:
            context.prec = 40
            contributions = sum(
                Decimal(row["loss_contribution"])
                for row in correlation_one["components"]
            )
            weights = sum(
                Decimal(row["allocation_weight"])
                for row in correlation_one["components"]
            )
        assert contributions == Decimal(correlation_one["declared_loss"])
        assert weights == Decimal("1")
        central_inputs = [
            Decimal(row["loss_input"])
            for row in dependence["central_case"]["components"]
        ]
        assert len(central_inputs) == 7
        assert _quadratic_loss(
            dependence["matrices"]["posterior"], central_inputs
        ) == Decimal(dependence["central_case"]["loss"])
        cluster = dependence["economic_driver_cluster_budget"]["clusters"][0]
        assert dependence["pair_count"] == 21
        assert (
            dependence["available_pair_count"]
            == dependence["central_case"]["measured_pair_count"]
        )
        assert len(cluster["members"]) == 7
        assert len(cluster["pair_evidence"]) == 21
        assert Decimal(cluster["loss_contribution"]) == Decimal(
            correlation_one["declared_loss"]
        )
        assert Decimal(
            dependence["economic_driver_cluster_budget"]["central_case_loss"]
        ) == Decimal(dependence["central_case"]["loss"])
    current_fixture_surfaces = {
        "strategies": data["strategies"],
        "performance": data["performance"],
        "allocations": data["allocations"],
        "positions": data["positions"],
        "strategy_underwriting": data["strategy_underwriting"],
        "hwm_summary": data["hwm_summary"],
        "weekly": data["weekly_returns"]["series"],
    }
    assert "echo" not in json.dumps(current_fixture_surfaces).lower()
    assert "composer" not in json.dumps(current_fixture_surfaces).lower()
    assert "echo" not in json.dumps(data).lower()
    assert "composer" not in json.dumps(data).lower()
    assert any(
        row.get("twr") is not None
        and row.get("twr_status") == "AVAILABLE_WITH_INFERRED_FLOW_TIMING"
        for row in data["performance"]
    )
    assert any(
        row.get("mwr_xirr") is not None and row.get("mwr_status") == "AVAILABLE"
        for row in data["performance"]
    )
    assert any(row.get("nav_status") == "AVAILABLE" for row in data["strategies"])

    matching = _rendered_evidence(candidate, runtime)
    assert any(row[4]["primary"] != "—" for row in matching["performanceRows"])
    assert any(
        row[6]["primary"] != "—" and "Available" in row[7]["text"]
        for row in matching["performanceRows"]
    )
    matching_strategy_return = next(
        item
        for item in matching["comparison"]
        if item["label"] == "13-week strategy return"
    )
    assert matching_strategy_return["value"] != "—"
    assert matching["weekly"]["rows"] and any(
        cell != "—" for row in matching["weekly"]["rows"] for cell in row["cells"]
    )
    assert matching["dependence"]["matrixCells"] == 49
    assert matching["dependence"]["robuxioCell"].strip() == "0.77"
    matching_interaction = _interaction_evidence(
        candidate, runtime, tmp_path / "matching-interaction.json", epoch_only=False
    )
    assert matching_interaction["all_passed"] is True

    data["meta"]["performance_lineage"] = {
        "status": "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH",
        "reason": "Adversarial test mutates only the report-to-source lineage identity.",
    }
    _write_dashboard(candidate, data)
    mismatch = _rendered_evidence(candidate, runtime)

    assert mismatch["performanceRows"]
    assert not any(
        re.search(r"\b(?:Accepted|Available)\b", header, flags=re.IGNORECASE)
        for header in mismatch["performanceHeaders"]
    )
    assert all(
        row[4]["primary"] == "—"
        and row[6]["primary"] == "—"
        and "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in row[4]["text"]
        and "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in row[6]["text"]
        for row in mismatch["performanceRows"]
    )
    assert all(
        row[6]["primary"] == "—"
        and "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in row[6]["text"]
        for row in mismatch["performanceRows"]
    )
    assert mismatch["implementationRows"]
    assert all(
        row[2]["primary"] == "—"
        and row[3]["primary"] == "—"
        and "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in row[1]["text"]
        for row in mismatch["implementationRows"]
    )
    withheld_regions = f"{mismatch['performanceText']} {mismatch['hwmText']} {mismatch['strategyText']}"
    assert "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in withheld_regions
    assert not re.search(
        r"\b(?:Accepted|canonical account-live returns|Available for HWM|HWM-qualified|AVAILABLE)\b",
        withheld_regions,
        flags=re.IGNORECASE,
    )
    strategy_return = next(
        item
        for item in mismatch["comparison"]
        if item["label"] == "13-week strategy return"
    )
    assert strategy_return["value"] == "—"
    assert "withheld" in strategy_return["note"].lower()
    assert "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in mismatch["strategyCoverageText"]
    assert not re.search(
        r"\b(?:AVAILABLE|coverage-qualified|TWR segments)\b",
        mismatch["strategyCoverageText"],
        re.IGNORECASE,
    )
    command_restricted_text = " ".join(mismatch["command"].values())
    assert "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in command_restricted_text
    assert not re.search(
        r"\b(?:HWM|accepted|canonical|echo|composer)\b",
        command_restricted_text,
        re.IGNORECASE,
    )
    assert mismatch["weekly"]["rows"]
    assert all(
        cell == "—" for row in mismatch["weekly"]["rows"] for cell in row["cells"]
    )
    assert "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" in mismatch["weekly"]["detail"]
    assert not re.search(
        r"\b(?:Accepted observations|Compounded from accepted daily returns|AVAILABLE)\b",
        f"{mismatch['weekly']['text']} {mismatch['weekly']['detail']}",
        re.IGNORECASE,
    )
    assert len(mismatch["epochRows"]) == 13
    assert len({row["strategy"] for row in mismatch["epochRows"]}) == 7
    assert not any(
        "echo" in row["strategy"].lower() or "composer" in row["strategy"].lower()
        for row in mismatch["epochRows"]
    )

    interaction = _interaction_evidence(
        candidate,
        runtime,
        tmp_path / "lineage-mismatch-interaction.json",
        epoch_only=True,
    )
    lineage_check = next(
        item
        for item in interaction["checks"]
        if item["check"]
        == "Lineage mismatch withholds retained report performance in the DOM"
    )
    assert lineage_check["status"] == "PASS"

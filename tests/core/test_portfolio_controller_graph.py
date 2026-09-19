"""Acceptance tests for the static controller-to-stage projection."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from portfolio_controller_graph import (
    ControllerGraphError,
    affected_stages,
    build_controller_graph,
    main,
    validate_controller_graph,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _isolated_projection_root(tmp_path: Path) -> Path:
    """Copy only the source-bound controller surface for a mutation test."""

    root = tmp_path / "repo"
    for relative_path in (
        "tools/portfolio_controller_graph.py",
        "packages/core/src/quant_engine/core/pipeline.py",
        "tools/portfolio_pipeline_materialize.py",
        "tools/portfolio_release_builder.py",
        "tools/portfolio_report.py",
        "tools/portfolio_control_panel.py",
        "tools/portfolio_attended_preparation.py",
        "contracts/portfolio-financial-mvp-source-v1.json",
        "tests/core/test_attended_preparation.py",
        "tests/core/test_portfolio_pipeline_materialize.py",
        "tests/core/test_portfolio_pipeline_orchestration.py",
        "tests/core/test_portfolio_release_builder.py",
        "tests/portfolio_report/test_portfolio_report.py",
        "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py",
    ):
        source = REPOSITORY_ROOT / relative_path
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return root


def test_native_controller_projection_is_source_bound_and_valid() -> None:
    """A controller change that omits a required binding must fail validation."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    assert graph["schema_version"] == 1
    assert graph["source_files"]
    assert graph["stages"]["panel"]["dependencies"] == [
        "ledger",
        "attribution",
        "analytics",
        "report",
    ]
    assert validate_controller_graph(REPOSITORY_ROOT, graph)["valid"] is True


def test_validation_rejects_a_graph_after_a_bound_source_byte_changes(
    tmp_path: Path,
) -> None:
    """Changing a bound source must invalidate its previously built projection."""

    root = _isolated_projection_root(tmp_path)
    graph = build_controller_graph(root)
    report = root / "tools/portfolio_report.py"
    report.write_bytes(report.read_bytes() + b"\n# source-byte-change\n")

    with pytest.raises(ControllerGraphError, match="source_files"):
        validate_controller_graph(root, graph)


def test_build_rejects_mismatched_core_and_materializer_stage_declarations(
    tmp_path: Path,
) -> None:
    """A materializer/core dependency split must not become a graph view."""

    root = _isolated_projection_root(tmp_path)
    core = root / "packages/core/src/quant_engine/core/pipeline.py"
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            '"panel": ("ledger", "attribution", "analytics", "report"),',
            '"panel": ("ledger", "attribution", "analytics"),',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ControllerGraphError, match="core/materializer stage dependencies differ"
    ):
        build_controller_graph(root)


def test_build_rejects_a_workbook_free_graph_without_report_to_panel_edge(
    tmp_path: Path,
) -> None:
    """Both declarations omitting report from panel is still a controller failure."""

    root = _isolated_projection_root(tmp_path)
    for relative_path in (
        "packages/core/src/quant_engine/core/pipeline.py",
        "tools/portfolio_pipeline_materialize.py",
    ):
        path = root / relative_path
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '"panel": ("ledger", "attribution", "analytics", "report"),',
                '"panel": ("ledger", "attribution", "analytics"),',
                1,
            ),
            encoding="utf-8",
        )

    with pytest.raises(ControllerGraphError, match="report-to-panel dependency"):
        build_controller_graph(root)


def test_validation_rejects_an_omitted_bound_source(tmp_path: Path) -> None:
    """Removing a source hash from a saved graph cannot hide its impact."""

    root = _isolated_projection_root(tmp_path)
    graph = build_controller_graph(root)
    del graph["source_files"]["tools/portfolio_report.py"]

    with pytest.raises(ControllerGraphError, match="source_files"):
        validate_controller_graph(root, graph)


def test_build_rejects_removed_panel_consumer_call(tmp_path: Path) -> None:
    """The release producer must retain its actual panel consumer call."""

    root = _isolated_projection_root(tmp_path)
    release_builder = root / "tools/portfolio_release_builder.py"
    release_builder.write_text(
        release_builder.read_text(encoding="utf-8").replace(
            '"portfolio_control_panel.py"', '"removed_panel_consumer.py"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(ControllerGraphError, match="release panel consumer call"):
        build_controller_graph(root)


def test_affected_stages_uses_declared_downstream_closure() -> None:
    """A report-only change reaches panel, while a panel-only change does not."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    assert affected_stages(graph, ["tools/portfolio_report.py"]) == ["report", "panel"]
    assert affected_stages(graph, ["tools/portfolio_control_panel.py"]) == ["panel"]
    assert affected_stages(graph, ["tools/portfolio_attended_preparation.py"]) == [
        "ledger",
        "baseline",
        "attribution",
        "analytics",
        "report",
        "panel",
    ]
    assert affected_stages(graph, ["tools/portfolio_controller_graph.py"]) == [
        "ledger",
        "baseline",
        "attribution",
        "analytics",
        "report",
        "panel",
    ]
    assert affected_stages(graph, ["docs/readme.md"]) == []


def test_affected_stages_requires_reconciliation_for_unmapped_production_change() -> (
    None
):
    """An unknown tool is not allowed to disappear from impact analysis."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    with pytest.raises(ControllerGraphError, match="requires reconciliation"):
        affected_stages(graph, ["tools/new_portfolio_producer.py"])


def test_cli_writes_validated_graph_and_reports_changed_path_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The installed CLI exposes the same callable gate and impact closure."""

    output = tmp_path / "controller-graph.json"

    assert (
        main(
            [
                "--repo-root",
                str(REPOSITORY_ROOT),
                "--write",
                str(output),
                "--changed-path",
                "tools/portfolio_report.py",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["affected_stages"] == ["report", "panel"]
    assert (
        validate_controller_graph(REPOSITORY_ROOT, json.loads(output.read_text()))[
            "valid"
        ]
        is True
    )

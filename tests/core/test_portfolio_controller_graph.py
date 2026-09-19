"""Acceptance tests for the static controller-to-stage projection."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil

import pytest

from portfolio_controller_graph import (
    ControllerGraphError,
    _mapper_python_hashes,
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
        "schemas/portfolio/financial-mvp-source-contract.schema.json",
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


def _replace_function_literal(
    path: Path, function_name: str, expected: str, replacement: str
) -> None:
    """Mutate exactly one actual controller function in an isolated copy."""

    source = path.read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    lines = source.splitlines(keepends=True)
    body = "".join(lines[function.lineno - 1 : function.end_lineno])
    assert expected in body
    path.write_text(
        "".join(lines[: function.lineno - 1])
        + body.replace(expected, replacement, 1)
        + "".join(lines[function.end_lineno :]),
        encoding="utf-8",
    )


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
    _replace_function_literal(
        release_builder,
        "build_panel_candidate",
        '"portfolio_control_panel.py"',
        '"removed_panel_consumer.py"',
    )

    with pytest.raises(ControllerGraphError, match="panel candidate build CLI target"):
        build_controller_graph(root)


@pytest.mark.parametrize(
    ("function_name", "expected", "replacement", "error"),
    [
        (
            "build_report_candidate",
            '"portfolio_report.py"',
            '"missing_report_consumer.py"',
            "report candidate build CLI target",
        ),
        (
            "build_report_candidate",
            '"build"',
            '"missing-build"',
            "report candidate build CLI target/subcommand",
        ),
        (
            "build_report",
            '"verify"',
            '"missing-verify"',
            "report verify CLI target/subcommand",
        ),
        (
            "build_report",
            '"publish"',
            '"missing-publish"',
            "report publish CLI target/subcommand",
        ),
        (
            "build_panel_candidate",
            '"portfolio_control_panel.py"',
            '"missing_panel_consumer.py"',
            "panel candidate build CLI target",
        ),
        (
            "build_panel_candidate",
            '"build"',
            '"missing-build"',
            "panel candidate build CLI target/subcommand",
        ),
    ],
)
def test_build_rejects_altered_actual_consumer_cli_edge(
    tmp_path: Path,
    function_name: str,
    expected: str,
    replacement: str,
    error: str,
) -> None:
    """Only the exact `_tool` target/subcommand pair proves a consumer edge."""

    root = _isolated_projection_root(tmp_path)
    release_builder = root / "tools/portfolio_release_builder.py"
    _replace_function_literal(release_builder, function_name, expected, replacement)
    release_builder.write_text(
        release_builder.read_text(encoding="utf-8") + '\nstr("portfolio_report.py")\n',
        encoding="utf-8",
    )

    with pytest.raises(ControllerGraphError, match=error):
        build_controller_graph(root)


def test_financial_source_schema_is_bound_and_has_full_impact() -> None:
    """The schema behind the financial source contract cannot silently vanish."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    assert (
        "schemas/portfolio/financial-mvp-source-contract.schema.json"
        in graph["source_files"]
    )
    assert affected_stages(
        graph, ["schemas/portfolio/financial-mvp-source-contract.schema.json"]
    ) == ["ledger", "baseline", "attribution", "analytics", "report", "panel"]


def test_graph_records_absent_preparation_declaration_without_inventing_edges() -> None:
    """This base lacks the worker-owned literal declaration, so absence is explicit."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    assert graph["contracts"]["preparation_stage_dependencies"] == {
        "edges": [],
        "status": "absent",
    }


def test_graph_records_verified_present_preparation_declaration(tmp_path: Path) -> None:
    """A worker-provided literal is consumed without changing its source contract."""

    root = _isolated_projection_root(tmp_path)
    preparation = root / "tools/portfolio_attended_preparation.py"
    preparation.write_text(
        preparation.read_text(encoding="utf-8")
        + '\nPREPARATION_STAGE_DEPENDENCIES = {"input-qc": ("capture",)}\n',
        encoding="utf-8",
    )

    graph = build_controller_graph(root)

    assert graph["contracts"]["preparation_stage_dependencies"] == {
        "dependencies": {"input-qc": ["capture"]},
        "edges": [
            {
                "from": "capture",
                "kind": "preparation-stage-dependency",
                "to": "input-qc",
            }
        ],
        "status": "verified",
    }
    assert {
        "caller": "tools.portfolio_attended_preparation.PREPARATION_STAGE_DEPENDENCIES",
        "callee": "preparation-stage:input-qc",
        "depends_on": "capture",
        "kind": "preparation-stage-dependency",
    } in graph["relationships"]


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
    with pytest.raises(ControllerGraphError, match="requires reconciliation"):
        affected_stages(graph, ["schemas/portfolio/new-financial-source.schema.json"])


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


def test_cli_accepts_actual_git_change_range(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The supported CLI, not a workstation wrapper, reads a Git change range."""

    assert (
        main(
            [
                "--repo-root",
                str(REPOSITORY_ROOT),
                "--changed-from",
                "HEAD",
                "--changed-to",
                "HEAD",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["affected_stages"] == []


def test_cli_rejects_mismatched_explicit_mapper_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicitly supplied mapper must describe this root and source bytes."""

    mapper = tmp_path / "mapper.json"
    mapper.write_text(
        json.dumps(
            {
                "errors": [],
                "files": {
                    relative_path: {"kind": "python", "sha256": digest}
                    for relative_path, digest in _mapper_python_hashes(
                        REPOSITORY_ROOT
                    ).items()
                },
                "root": str(REPOSITORY_ROOT),
                "sourceFingerprint": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--repo-root",
                str(REPOSITORY_ROOT),
                "--mapper-evidence",
                str(mapper),
            ]
        )
        == 1
    )
    assert "mapper source fingerprint differs" in capsys.readouterr().out

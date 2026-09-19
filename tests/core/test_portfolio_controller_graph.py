"""Acceptance tests for the static controller-to-stage projection."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil

import pytest

from portfolio_controller_graph import (
    ControllerGraphError,
    _mapper_python_fingerprint,
    _required_paths,
    affected_stages,
    build_controller_graph,
    _git_changed_paths,
    main,
    validate_controller_graph,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _isolated_projection_root(tmp_path: Path) -> Path:
    """Copy only the source-bound controller surface for a mutation test."""

    root = tmp_path / "repo"
    for relative_path in set(
        (
            "tools/portfolio_controller_graph.py",
            "packages/core/src/quant_engine/core/pipeline.py",
            "tools/portfolio_pipeline_materialize.py",
            "tools/portfolio_release_builder.py",
            "tools/portfolio_freshness.py",
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
        )
    ).union(_required_paths(REPOSITORY_ROOT)):
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
    assert graph["excluded_sinks"] == {
        "workbook": "permanently held outside the MVP execution and acceptance path"
    }
    assert graph["stages"]["panel"]["dependencies"] == [
        "ledger",
        "attribution",
        "analytics",
        "report",
    ]
    assert validate_controller_graph(REPOSITORY_ROOT, graph)["valid"] is True


def test_build_accepts_the_integrated_layered_history_callable_alias() -> None:
    """The real preparation route invokes its statically bound history alias."""

    graph = build_controller_graph(REPOSITORY_ROOT)

    assert validate_controller_graph(REPOSITORY_ROOT, graph)["valid"] is True


@pytest.mark.parametrize(
    ("expected", "replacement"),
    [
        (
            "build_history = cast(",
            "missing_build_history = cast(",
        ),
        (
            "artifacts, manifest = build_history(**kwargs)",
            "artifacts, manifest = missing_build_history(**kwargs)",
        ),
    ],
)
def test_build_rejects_mutated_layered_history_alias_or_call(
    tmp_path: Path, expected: str, replacement: str
) -> None:
    """A binding mention or a different alias call cannot satisfy the route."""

    root = _isolated_projection_root(tmp_path)
    _replace_function_literal(
        root / "tools/portfolio_attended_preparation.py",
        "_rebuild_layered_history",
        expected,
        replacement,
    )

    with pytest.raises(
        ControllerGraphError, match="required controller call is absent"
    ):
        build_controller_graph(root)


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


def test_build_rejects_altered_actual_freshness_producer_cli_edge(
    tmp_path: Path,
) -> None:
    """The ledger producer must retain its literal freshness CLI route."""

    root = _isolated_projection_root(tmp_path)
    release_builder = root / "tools/portfolio_release_builder.py"
    _replace_function_literal(
        release_builder,
        "build_ledger",
        '"portfolio_freshness.py"',
        '"missing_freshness_producer.py"',
    )
    release_builder.write_text(
        release_builder.read_text(encoding="utf-8")
        + '\nstr("portfolio_freshness.py")\n',
        encoding="utf-8",
    )

    with pytest.raises(ControllerGraphError, match="freshness producer CLI target"):
        build_controller_graph(root)


@pytest.mark.parametrize(
    ("relative_path", "function_name", "expected", "replacement", "error"),
    [
        (
            "tools/portfolio_attended_preparation.py",
            "_prepare_attended_inputs",
            "compute_governed_as_held_composite_hwm(",
            "missing_governed_hwm_producer(",
            "governed HWM producer invocation",
        ),
        (
            "packages/core/src/quant_engine/core/pipeline.py",
            "execute_pipeline",
            "_validate_commit_authorization(commit_authorization, spec)",
            "missing_commit_authorization(commit_authorization, spec)",
            "owner publication authorization invocation",
        ),
    ],
)
def test_build_rejects_removed_actual_producer_or_sink_invocation(
    tmp_path: Path,
    relative_path: str,
    function_name: str,
    expected: str,
    replacement: str,
    error: str,
) -> None:
    """The graph must reject a removed required runtime-route invocation."""

    root = _isolated_projection_root(tmp_path)
    _replace_function_literal(
        root / relative_path, function_name, expected, replacement
    )

    with pytest.raises(ControllerGraphError, match=error):
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


def test_graph_records_integrated_preparation_declaration() -> None:
    """The graph consumes the executable preparation declaration as-is."""

    graph = build_controller_graph(REPOSITORY_ROOT)
    declaration = graph["contracts"]["preparation_stage_dependencies"]

    assert declaration["status"] == "verified"
    assert declaration["dependencies"]["selected-history-normalization"] == []


def test_graph_records_verified_present_preparation_declaration(tmp_path: Path) -> None:
    """A worker-provided literal is consumed without changing its source contract."""

    root = _isolated_projection_root(tmp_path)
    preparation = root / "tools/portfolio_attended_preparation.py"
    preparation.write_text(
        preparation.read_text(encoding="utf-8").replace(
            "PREPARATION_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {",
            "PREPARATION_STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {\n"
            '    "input-qc": ("capture",),',
            1,
        ),
        encoding="utf-8",
    )

    graph = build_controller_graph(root)
    declaration = graph["contracts"]["preparation_stage_dependencies"]

    assert declaration["status"] == "verified"
    assert declaration["dependencies"]["input-qc"] == ["capture"]
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


def test_panel_consumer_inventory_does_not_create_report_impact() -> None:
    """Artifact inventory is not an upstream causal-stage declaration."""

    graph = build_controller_graph(REPOSITORY_ROOT)
    report_flow = next(
        flow for flow in graph["artifact_flows"] if flow["id"] == "report-candidate"
    )

    assert "tools/portfolio_control_panel.py" in report_flow["source_paths"]
    assert affected_stages(graph, ["tools/portfolio_control_panel.py"]) == ["panel"]


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


def _canonical_tools_mapper_evidence(root: Path) -> dict[str, object]:
    """Return the installed mapper's public tools-map shape without cache internals."""

    return {
        "schemaVersion": 1,
        "generatedAt": "2026-09-19T00:00:00Z",
        "root": str(root),
        "package": "tools",
        "sourceFingerprint": _mapper_python_fingerprint(root, "tools"),
        "edges": [],
        "contracts": [],
        "errors": [],
        "stats": {},
        "structural": {},
    }


def test_cli_accepts_current_public_tools_mapper_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public mapper artifact is package-scoped and exposes no cache files."""

    mapper = tmp_path / "mapper.json"
    mapper.write_text(
        json.dumps(_canonical_tools_mapper_evidence(REPOSITORY_ROOT)),
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
        == 0
    )
    assert json.loads(capsys.readouterr().out)["mapper_evidence"]["status"] == (
        "accepted-optional-context"
    )


def test_cli_accepts_public_mapper_evidence_with_relative_repository_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI normalizes its root before deriving mapper-relative file keys."""

    mapper = tmp_path / "mapper.json"
    mapper.write_text(
        json.dumps(_canonical_tools_mapper_evidence(REPOSITORY_ROOT)),
        encoding="utf-8",
    )
    monkeypatch.chdir(REPOSITORY_ROOT)

    assert main(["--repo-root", ".", "--mapper-evidence", str(mapper)]) == 0
    assert json.loads(capsys.readouterr().out)["mapper_evidence"]["package"] == "tools"


def test_cli_rejects_stale_public_tools_mapper_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source-byte change under the declared mapper package invalidates evidence."""

    root = _isolated_projection_root(tmp_path)
    mapper = tmp_path / "mapper.json"
    mapper.write_text(
        json.dumps(_canonical_tools_mapper_evidence(root)), encoding="utf-8"
    )
    report = root / "tools/portfolio_report.py"
    report.write_bytes(report.read_bytes() + b"\n# mapper-package-drift\n")

    assert (
        main(
            [
                "--repo-root",
                str(root),
                "--mapper-evidence",
                str(mapper),
            ]
        )
        == 1
    )
    assert "mapper source fingerprint differs" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("root", "C:/not-the-repository", "mapper root differs"),
        ("package", "../tools", "mapper package is invalid"),
    ],
)
def test_cli_rejects_public_mapper_root_or_package_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
    error: str,
) -> None:
    """Root and package declarations are as binding as the source fingerprint."""

    mapper = _canonical_tools_mapper_evidence(REPOSITORY_ROOT)
    mapper[field] = value
    path = tmp_path / "mapper.json"
    path.write_text(json.dumps(mapper), encoding="utf-8")

    assert (
        main(["--repo-root", str(REPOSITORY_ROOT), "--mapper-evidence", str(path)]) == 1
    )
    assert error in capsys.readouterr().out


def test_actual_delivery_range_has_complete_affected_stage_closure() -> None:
    """The actual #374 delivery range cannot omit its changed producer surfaces."""

    graph = build_controller_graph(REPOSITORY_ROOT)
    changed = _git_changed_paths(REPOSITORY_ROOT, "a579a35", "HEAD")

    assert affected_stages(graph, changed) == [
        "ledger",
        "baseline",
        "attribution",
        "analytics",
        "report",
        "panel",
    ]


def test_validation_rejects_missing_required_artifact_consumer_or_sink() -> None:
    """A saved projection may not hide a required artifact-flow consumer."""

    graph = build_controller_graph(REPOSITORY_ROOT)
    flows = graph["artifact_flows"]
    assert isinstance(flows, list) and flows
    flows[0]["consumers"] = []

    with pytest.raises(ControllerGraphError, match="artifact_flows"):
        validate_controller_graph(REPOSITORY_ROOT, graph)

    graph = build_controller_graph(REPOSITORY_ROOT)
    flows = graph["artifact_flows"]
    assert isinstance(flows, list) and flows
    flows[0]["sinks"] = []
    with pytest.raises(ControllerGraphError, match="artifact_flows"):
        validate_controller_graph(REPOSITORY_ROOT, graph)

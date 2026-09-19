"""Static, source-bound projection of the supported portfolio controller.

This gate reads declarations with :mod:`ast`; it never imports or runs a
financial producer.  The executable materializer and core pipeline remain the
authority for runtime ordering, source validation, recovery, and numerics.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence, cast


class ControllerGraphError(ValueError):
    """The static projection is stale, incomplete, or internally inconsistent."""


_CORE = "packages/core/src/quant_engine/core/pipeline.py"
_MATERIALIZER = "tools/portfolio_pipeline_materialize.py"
_RELEASE = "tools/portfolio_release_builder.py"
_REPORT = "tools/portfolio_report.py"
_PANEL = "tools/portfolio_control_panel.py"
_PREPARATION = "tools/portfolio_attended_preparation.py"
_SOURCE_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
_SOURCE_SCHEMA = "schemas/portfolio/financial-mvp-source-contract.schema.json"
_MAPPER_SKIP_DIRECTORIES = frozenset(
    {
        ".dep-map-cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)
_TEST_LINKS = {
    "source_qc_and_preparation": "tests/core/test_attended_preparation.py",
    "stage_contract": "tests/core/test_portfolio_pipeline_materialize.py",
    "core_stage_contract": "tests/core/test_portfolio_pipeline_orchestration.py",
    "release_producer": "tests/core/test_portfolio_release_builder.py",
    "report_consumer": "tests/portfolio_report/test_portfolio_report.py",
    "panel_consumer": "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py",
}
_STAGE_ORDER = ("ledger", "baseline", "attribution", "analytics", "report", "panel")
_ATTENDED_ORDER = (*_STAGE_ORDER[:5], "panel-candidate", "panel")
_HANDLERS = {
    "ledger": "tools.portfolio_release_builder.build_ledger",
    "baseline": "tools.portfolio_release_builder.build_baseline",
    "attribution": "tools.portfolio_release_builder.build_attribution",
    "analytics": "tools.portfolio_release_builder.build_analytics",
    "report": "tools.portfolio_release_builder.build_report",
    "panel": "tools.portfolio_release_builder.build_panel_candidate",
}
_PATH_STAGES = {
    "tools/portfolio_controller_graph.py": list(_STAGE_ORDER),
    _CORE: list(_STAGE_ORDER),
    _MATERIALIZER: list(_STAGE_ORDER),
    _RELEASE: list(_STAGE_ORDER),
    _PREPARATION: list(_STAGE_ORDER),
    _SOURCE_CONTRACT: list(_STAGE_ORDER),
    _SOURCE_SCHEMA: list(_STAGE_ORDER),
    _REPORT: ["report", "panel"],
    _PANEL: ["panel"],
}
_ARTIFACT_FLOW_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": "attended-preparation-checkpoints",
        "source_paths": (
            "tools/portfolio_attended_preparation.py",
            "tools/portfolio_financial_mvp_contract.py",
            "tools/portfolio_financial_outcomes.py",
            "tools/portfolio_qc.py",
            "contracts/portfolio-financial-mvp-source-v1.json",
            "schemas/portfolio/financial-mvp-source-contract.schema.json",
        ),
        "producer": (_PREPARATION, "prepare_attended_inputs"),
        "artifacts": (
            "source-QC receipt",
            "preparation stage checkpoints",
            "retained preparation caches",
        ),
        "consumers": ((_MATERIALIZER, "main"), (_RELEASE, "build_stage")),
        "validators": ((_PREPARATION, "plan_attended_inputs"),),
        "stages": _STAGE_ORDER,
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "provider-ledger-generation",
        "source_paths": (
            "tools/provider_ledger.py",
            "tools/quantbase_generation.py",
            "tools/schwab_generation.py",
            "schemas/portfolio/provider-admission-receipt.schema.json",
            "schemas/portfolio/provider-ledger.schema.json",
        ),
        "producer": ("tools/provider_ledger.py", "stage_candidate"),
        "artifacts": (
            "provider-ledger.json",
            "generation.json",
            "admit-or-declare-receipt.json",
        ),
        "consumers": ((_RELEASE, "build_ledger"), (_MATERIALIZER, "main")),
        "validators": (("tools/provider_ledger.py", "verify_candidate"),),
        "stages": _STAGE_ORDER,
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "baseline-epoch-hwm-capital-generations",
        "source_paths": (
            "tools/account_strategy_manifest.py",
            "tools/build_account_manifest_generation.py",
            "tools/portfolio_baseline.py",
            "tools/portfolio_invested_capital.py",
            "tools/portfolio_target_series.py",
            "tools/verify_account_manifest_generation.py",
            "contracts/portfolio-hwm-policy-v4.json",
            "schemas/portfolio/account-strategy-manifest.schema.json",
            "schemas/portfolio/layered-history-generation.schema.json",
        ),
        "producer": ("tools/portfolio_baseline.py", "stage_baseline"),
        "artifacts": (
            "portfolio-baseline generation",
            "account-strategy-manifest generation",
            "layered-history generation",
            "governed HWM and invested-capital receipts",
        ),
        "consumers": (
            (_RELEASE, "build_baseline"),
            (_RELEASE, "build_attribution"),
            (_RELEASE, "build_analytics"),
        ),
        "validators": (
            ("tools/portfolio_invested_capital.py", "verify_governed_capital_result"),
            (
                "tools/build_account_manifest_generation.py",
                "stage_account_manifest_generation",
            ),
        ),
        "stages": ("baseline", "attribution", "analytics", "report", "panel"),
        "tests": (
            "tests/core/test_attended_preparation.py",
            "tests/core/test_portfolio_pipeline_orchestration.py",
        ),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "attribution-and-analytics-generations",
        "source_paths": (
            "tools/portfolio_attribution.py",
            "tools/portfolio_current_decomposition_plan.py",
            "tools/portfolio_current_dependence.py",
            "tools/portfolio_decomposition.py",
            "contracts/portfolio-performance-policy-v1.json",
        ),
        "producer": ("tools/portfolio_decomposition.py", "stage_candidate"),
        "artifacts": (
            "strategy-attribution generation",
            "current dependence source",
            "decomposition generation",
        ),
        "consumers": ((_RELEASE, "build_analytics"), (_RELEASE, "build_report")),
        "validators": (
            ("tools/portfolio_current_dependence.py", "verify_source"),
            ("tools/portfolio_attribution.py", "publish_catalog"),
        ),
        "stages": ("attribution", "analytics", "report", "panel"),
        "tests": (
            "tests/core/test_portfolio_pipeline_orchestration.py",
            "tests/core/test_portfolio_release_builder.py",
        ),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "report-and-panel-candidates",
        "source_paths": (
            _REPORT,
            _PANEL,
            "schemas/portfolio/report-data.schema.json",
            "schemas/portfolio/control-panel-data.schema.json",
        ),
        "producer": (_RELEASE, "build_report_candidate"),
        "artifacts": (
            "portfolio-report-data.json",
            "report HTML",
            "panel JSON",
            "panel HTML",
        ),
        "consumers": (
            (_REPORT, "verify_candidate"),
            (_PANEL, "build_candidate"),
            (_PANEL, "verify_candidate"),
        ),
        "validators": ((_REPORT, "verify_candidate"), (_PANEL, "verify_candidate")),
        "stages": ("report", "panel"),
        "tests": (
            "tests/portfolio_report/test_portfolio_report.py",
            "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py",
        ),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "coordinated-publication-pointers",
        "source_paths": (_CORE, _MATERIALIZER, _RELEASE),
        "producer": (_RELEASE, "build_report"),
        "artifacts": (
            "coordinated report and panel generations",
            "CURRENT pointers",
        ),
        "consumers": ((_RELEASE, "build_panel_candidate"),),
        "validators": ((_REPORT, "publish_report"), (_PANEL, "publish_panel")),
        "stages": ("report", "panel"),
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": ("coordinated-publication-pointers",),
        "publication_authority": "existing owner-publication gate",
    },
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return path.as_posix()


def _required_paths(repo_root: Path) -> list[str]:
    paths = [
        "tools/portfolio_controller_graph.py",
        _CORE,
        _MATERIALIZER,
        _RELEASE,
        _REPORT,
        _PANEL,
        _SOURCE_CONTRACT,
        _SOURCE_SCHEMA,
    ]
    if (repo_root / _PREPARATION).is_file():
        paths.append(_PREPARATION)
    paths.extend(path for path in _TEST_LINKS.values() if (repo_root / path).is_file())
    for flow in _ARTIFACT_FLOW_SPECS:
        paths.extend(cast(Sequence[str], flow["source_paths"]))
        paths.extend(cast(Sequence[str], flow["tests"]))
    return sorted(paths)


def _tree(repo_root: Path, relative_path: str) -> ast.Module:
    path = repo_root / relative_path
    if not path.is_file():
        raise ControllerGraphError(
            f"required controller source is absent: {relative_path}"
        )
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ControllerGraphError(
            f"cannot parse controller source: {relative_path}"
        ) from exc


def _top_level_function(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise ControllerGraphError(f"required controller symbol is absent: {name}")


def _literal_assignment(tree: ast.Module, name: str) -> object:
    def literal(value: ast.expr) -> object:
        try:
            return ast.literal_eval(value)
        except ValueError as exc:
            raise ControllerGraphError(
                f"controller declaration is not literal: {name}"
            ) from exc

    for node in tree.body:
        if isinstance(node, ast.Assign):
            matches = any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
            if matches:
                return literal(node.value)
        elif isinstance(node, ast.AnnAssign):
            matches = isinstance(node.target, ast.Name) and node.target.id == name
            if matches and node.value is not None:
                return literal(node.value)
        else:
            continue
    raise ControllerGraphError(f"controller declaration is absent: {name}")


def _call_name(node: ast.Call) -> str | None:
    value: ast.expr = node.func
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
        return ".".join(reversed(parts))
    return None


def _function_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        call_name
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        if (call_name := _call_name(node)) is not None
    }


def _tool_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[tuple[str, str]]:
    """Return literal ``_tool(target, subcommand, ...)`` calls in one function."""

    calls: set[tuple[str, str]] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _call_name(node) != "_tool":
            continue
        if len(node.args) < 2:
            continue
        target, subcommand = node.args[:2]
        if (
            isinstance(target, ast.Constant)
            and isinstance(target.value, str)
            and isinstance(subcommand, ast.Constant)
            and isinstance(subcommand.value, str)
        ):
            calls.add((target.value, subcommand.value))
    return calls


def _module_imports(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def _stage_declarations(
    repo_root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    core = _tree(repo_root, _CORE)
    materializer = _tree(repo_root, _MATERIALIZER)
    core_order = _literal_assignment(core, "_PORTFOLIO_STAGE_ORDER")
    core_dependencies = _literal_assignment(core, "_PORTFOLIO_STAGE_DEPENDENCIES")
    materializer_order = _literal_assignment(materializer, "STAGES")
    materializer_dependencies = _literal_assignment(materializer, "STAGE_DEPENDENCIES")
    attended = _literal_assignment(materializer, "ATTENDED_STAGES")
    if not isinstance(core_order, (tuple, list)) or not isinstance(
        materializer_order, (tuple, list)
    ):
        raise ControllerGraphError("core/materializer stage order is not a sequence")
    if not isinstance(attended, (tuple, list)):
        raise ControllerGraphError("attended stage order is not a sequence")
    if tuple(core_order) != _STAGE_ORDER or tuple(materializer_order) != _STAGE_ORDER:
        raise ControllerGraphError("core/materializer stage order differs")
    if tuple(attended) != _ATTENDED_ORDER:
        raise ControllerGraphError("attended stages omit the panel-candidate substep")
    if not isinstance(core_dependencies, dict) or not isinstance(
        materializer_dependencies, dict
    ):
        raise ControllerGraphError("stage dependency declaration is not a mapping")
    normalized_core = {
        str(key): list(value) for key, value in core_dependencies.items()
    }
    normalized_materializer = {
        str(key): list(value) for key, value in materializer_dependencies.items()
    }
    if normalized_core != normalized_materializer:
        raise ControllerGraphError("core/materializer stage dependencies differ")
    if set(normalized_core) != set(_STAGE_ORDER):
        raise ControllerGraphError("stage dependency declaration omits a product stage")
    if "report" not in normalized_core.get("panel", []):
        raise ControllerGraphError("workbook-free report-to-panel dependency is absent")
    return normalized_core, normalized_materializer


def _require_calls(
    repo_root: Path,
    relative_path: str,
    function_name: str,
    calls: Iterable[str],
) -> None:
    observed = _function_calls(
        _top_level_function(_tree(repo_root, relative_path), function_name)
    )
    missing = sorted(set(calls) - observed)
    if missing:
        raise ControllerGraphError(
            f"required controller call is absent: {relative_path}:{function_name}:{','.join(missing)}"
        )


def _require_tool_call(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
    subcommand: str,
    relationship: str,
) -> None:
    if (target, subcommand) not in _tool_calls(function):
        raise ControllerGraphError(f"required {relationship} is absent")


def _preparation_stage_dependencies(
    repo_root: Path,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    """Record the worker-owned literal declaration when this checkout has it."""

    preparation = _tree(repo_root, _PREPARATION)
    try:
        declared = _literal_assignment(preparation, "PREPARATION_STAGE_DEPENDENCIES")
    except ControllerGraphError as exc:
        if str(exc) == (
            "controller declaration is absent: PREPARATION_STAGE_DEPENDENCIES"
        ):
            return {"edges": [], "status": "absent"}, []
        raise
    if not isinstance(declared, dict):
        raise ControllerGraphError(
            "preparation stage dependency declaration is not a mapping"
        )
    dependencies: dict[str, list[str]] = {}
    edges: list[dict[str, str]] = []
    for stage, prerequisites in declared.items():
        if not isinstance(stage, str) or not isinstance(prerequisites, (list, tuple)):
            raise ControllerGraphError(
                "preparation stage dependency declaration differs"
            )
        if not all(isinstance(prerequisite, str) for prerequisite in prerequisites):
            raise ControllerGraphError(
                "preparation stage dependency declaration differs"
            )
        dependencies[stage] = list(prerequisites)
        edges.extend(
            {
                "from": prerequisite,
                "kind": "preparation-stage-dependency",
                "to": stage,
            }
            for prerequisite in prerequisites
        )
    return {
        "dependencies": dict(sorted(dependencies.items())),
        "edges": sorted(edges, key=lambda edge: (edge["from"], edge["to"])),
        "status": "verified",
    }, edges


def _critical_relationships(
    repo_root: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    materializer = _tree(repo_root, _MATERIALIZER)
    main_calls = _function_calls(_top_level_function(materializer, "main"))
    imports = _module_imports(materializer)
    if "portfolio_attended_preparation" not in imports:
        raise ControllerGraphError("attended preparation import is absent")
    if "_attended_basic_preflight" not in main_calls:
        raise ControllerGraphError("attended preparation lacks its entrypoint guard")
    if "preparation.plan_attended_inputs" not in main_calls or (
        "preparation.prepare_attended_inputs" not in main_calls
    ):
        raise ControllerGraphError("attended preparation entrypoint calls are absent")
    if not (repo_root / _PREPARATION).is_file():
        raise ControllerGraphError("attended preparation implementation is absent")
    _top_level_function(_tree(repo_root, _PREPARATION), "plan_attended_inputs")
    _top_level_function(_tree(repo_root, _PREPARATION), "prepare_attended_inputs")
    preparation_dependencies, preparation_edges = _preparation_stage_dependencies(
        repo_root
    )
    _require_calls(repo_root, _MATERIALIZER, "_attended_stage_command", {"str"})
    command_source = (repo_root / _MATERIALIZER).read_text(encoding="utf-8")
    if "portfolio_release_builder.py" not in command_source:
        raise ControllerGraphError("materializer release-producer command is absent")
    _require_calls(repo_root, _RELEASE, "main", {"build_stage"})
    release_tree = _tree(repo_root, _RELEASE)
    release_stage_calls = _function_calls(
        _top_level_function(release_tree, "build_stage")
    )
    for handler in _HANDLERS.values():
        if handler.rsplit(".", 1)[1] not in release_stage_calls:
            raise ControllerGraphError(f"release producer handler is absent: {handler}")
    report_builder = _top_level_function(release_tree, "build_report")
    panel_builder = _top_level_function(release_tree, "build_panel_candidate")
    report_candidate_builder = _top_level_function(
        release_tree, "build_report_candidate"
    )
    if "build_report_candidate" not in _function_calls(report_builder):
        raise ControllerGraphError("release report candidate call is absent")
    _require_tool_call(
        report_candidate_builder,
        "portfolio_report.py",
        "build",
        "report candidate build CLI target/subcommand",
    )
    _require_tool_call(
        report_builder,
        "portfolio_report.py",
        "verify",
        "report verify CLI target/subcommand",
    )
    _require_tool_call(
        report_builder,
        "portfolio_report.py",
        "publish",
        "report publish CLI target/subcommand",
    )
    _require_tool_call(
        panel_builder,
        "portfolio_control_panel.py",
        "build",
        "panel candidate build CLI target/subcommand",
    )
    report_tree = _tree(repo_root, _REPORT)
    panel_tree = _tree(repo_root, _PANEL)
    _top_level_function(report_tree, "build_candidate")
    _top_level_function(report_tree, "verify_candidate")
    _top_level_function(panel_tree, "build_candidate")
    _top_level_function(panel_tree, "verify_candidate")
    relationships: list[dict[str, object]] = [
        {
            "caller": "tools.portfolio_pipeline_materialize.main",
            "callee": "tools.portfolio_attended_preparation.plan_attended_inputs",
            "kind": "preparation-plan",
            "guard": "tools.portfolio_pipeline_materialize._attended_basic_preflight",
        },
        {
            "caller": "tools.portfolio_pipeline_materialize.main",
            "callee": "tools.portfolio_attended_preparation.prepare_attended_inputs",
            "kind": "preparation-execute",
            "guard": "tools.portfolio_pipeline_materialize._attended_basic_preflight",
        },
        {
            "caller": "tools.portfolio_pipeline_materialize._attended_stage_command",
            "callee": "tools.portfolio_release_builder.main",
            "kind": "release-stage-command",
        },
        {
            "caller": "tools.portfolio_release_builder.main",
            "callee": "tools.portfolio_release_builder.build_stage",
            "kind": "release-handler",
        },
        {
            "caller": "tools.portfolio_release_builder.build_stage",
            "callee": "tools.portfolio_release_builder.build_report",
            "kind": "report-producer",
        },
        {
            "caller": "tools.portfolio_release_builder.build_stage",
            "callee": "tools.portfolio_release_builder.build_panel_candidate",
            "kind": "panel-producer",
        },
        {
            "caller": "tools.portfolio_release_builder.build_report",
            "callee": "tools.portfolio_release_builder.build_report_candidate",
            "kind": "report-candidate",
        },
        {
            "caller": "tools.portfolio_release_builder.build_report_candidate",
            "callee": "tools/portfolio_report.py build",
            "kind": "report-build-cli",
        },
        {
            "caller": "tools.portfolio_release_builder.build_report",
            "callee": "tools/portfolio_report.py verify",
            "kind": "report-verify-cli",
        },
        {
            "caller": "tools.portfolio_release_builder.build_report",
            "callee": "tools/portfolio_report.py publish",
            "kind": "report-publish-cli",
        },
        {
            "caller": "tools.portfolio_release_builder.build_panel_candidate",
            "callee": "tools/portfolio_control_panel.py build",
            "kind": "panel-build-cli",
        },
    ]
    relationships.extend(
        {
            "caller": "tools.portfolio_attended_preparation.PREPARATION_STAGE_DEPENDENCIES",
            "callee": f"preparation-stage:{edge['to']}",
            "depends_on": edge["from"],
            "kind": edge["kind"],
        }
        for edge in preparation_edges
    )
    return relationships, preparation_dependencies


def _artifact_flows(repo_root: Path) -> list[dict[str, object]]:
    """Bind the MVP's declared artifacts to real producers, consumers and checks.

    This projection intentionally remains smaller than the mapper's full static
    graph.  It records only the supported financial-MVP route, and leaves
    runtime source/finality, recovery and publication authorization with the
    native runner and existing owner gate.
    """

    flows: list[dict[str, object]] = []
    for spec in _ARTIFACT_FLOW_SPECS:
        flow = dict(spec)
        source_paths = cast(tuple[str, ...], flow["source_paths"])
        producer = cast(tuple[str, str], flow["producer"])
        consumers = cast(tuple[tuple[str, str], ...], flow["consumers"])
        validators = cast(tuple[tuple[str, str], ...], flow["validators"])
        artifacts = cast(tuple[str, ...], flow["artifacts"])
        stages = cast(tuple[str, ...], flow["stages"])
        sinks = cast(tuple[str, ...], flow["sinks"])
        tests = cast(tuple[str, ...], flow["tests"])
        for source_path in source_paths:
            if (
                not isinstance(source_path, str)
                or not (repo_root / source_path).is_file()
            ):
                raise ControllerGraphError("artifact-flow source is absent")
        endpoints = (producer, *consumers, *validators)
        for endpoint in endpoints:
            if not isinstance(endpoint, tuple) or len(endpoint) != 2:
                raise ControllerGraphError("artifact-flow endpoint differs")
            path, symbol = endpoint
            if not isinstance(path, str) or not isinstance(symbol, str):
                raise ControllerGraphError("artifact-flow endpoint differs")
            _top_level_function(_tree(repo_root, path), symbol)
        if not artifacts or not consumers or not validators or not sinks:
            raise ControllerGraphError("artifact-flow consumer or validator is absent")
        if not stages or not all(stage in _STAGE_ORDER for stage in stages):
            raise ControllerGraphError("artifact-flow stages differ")
        if not tests or not all((repo_root / path).is_file() for path in tests):
            raise ControllerGraphError("artifact-flow tests are absent")
        flow["source_paths"] = sorted(source_paths)
        flow["artifacts"] = list(artifacts)
        flow["consumers"] = [list(endpoint) for endpoint in consumers]
        flow["producer"] = list(producer)
        flow["stages"] = list(stages)
        flow["sinks"] = list(sinks)
        flow["tests"] = list(tests)
        flow["validators"] = [list(endpoint) for endpoint in validators]
        flows.append(flow)
    return flows


def _mapper_package_root(repo_root: Path, package: object) -> Path:
    """Resolve one public mapper package without allowing a root escape."""

    if not isinstance(package, str) or not package:
        raise ControllerGraphError("mapper package is invalid")
    candidate = Path(package)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ControllerGraphError("mapper package is invalid")
    try:
        package_root = (repo_root / candidate).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ControllerGraphError("mapper package is invalid") from exc
    try:
        package_root.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ControllerGraphError("mapper package is invalid") from exc
    if not package_root.is_dir():
        raise ControllerGraphError("mapper package is invalid")
    return package_root


def _mapper_python_hashes(repo_root: Path, package: object) -> dict[str, str]:
    """Return the installed mapper's declared package Python scan scope."""

    repo_root = repo_root.resolve()
    paths: list[Path] = []
    pending = [_mapper_package_root(repo_root, package)]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise ControllerGraphError(
                f"cannot inspect mapper scope: {directory}"
            ) from exc
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir() and child.name not in _MAPPER_SKIP_DIRECTORIES:
                pending.append(child)
            elif child.is_file() and child.suffix.lower() == ".py":
                paths.append(child)
    return {
        path.relative_to(repo_root).as_posix(): _sha256(path) for path in sorted(paths)
    }


def _mapper_python_fingerprint(repo_root: Path, package: object) -> str:
    """Match the installed mapper's sorted relative-path/raw-hash algorithm."""

    rows = [
        f"{relative_path}:{digest}"
        for relative_path, digest in _mapper_python_hashes(repo_root, package).items()
    ]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _explicit_mapper_evidence(repo_root: Path, path: Path) -> dict[str, str]:
    """Accept one explicit, current mapper artifact as optional context only."""

    if path.is_symlink() or not path.is_file():
        raise ControllerGraphError("explicit mapper evidence is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerGraphError("explicit mapper evidence is unreadable") from exc
    if not isinstance(payload, dict):
        raise ControllerGraphError("explicit mapper evidence is not an object")
    try:
        scanned_root = Path(str(payload.get("root", ""))).resolve()
    except (OSError, ValueError) as exc:
        raise ControllerGraphError("mapper root is invalid") from exc
    if scanned_root != repo_root.resolve():
        raise ControllerGraphError("mapper root differs from repository root")
    errors = payload.get("errors")
    if not isinstance(errors, list) or errors:
        raise ControllerGraphError("mapper scanner errors require reconciliation")
    package = payload.get("package")
    if not isinstance(package, str):
        raise ControllerGraphError("mapper package is invalid")
    _mapper_package_root(repo_root, package)
    fingerprint = payload.get("sourceFingerprint")
    if not isinstance(fingerprint, str) or fingerprint != _mapper_python_fingerprint(
        repo_root, package
    ):
        raise ControllerGraphError("mapper source fingerprint differs")
    return {
        "path": str(path.resolve()),
        "package": package,
        "sha256": _sha256(path),
        "source_fingerprint": fingerprint,
        "status": "accepted-optional-context",
    }


def _current_graph(repo_root: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    dependencies, materializer_dependencies = _stage_declarations(repo_root)
    relationships, preparation_dependencies = _critical_relationships(repo_root)
    artifact_flows = _artifact_flows(repo_root)
    source_files = {
        path: _sha256(repo_root / path) for path in _required_paths(repo_root)
    }
    edges = [
        {"from": dependency, "to": stage, "kind": "stage-dependency"}
        for stage in _STAGE_ORDER
        for dependency in dependencies[stage]
    ]
    stages: dict[str, dict[str, object]] = {}
    for stage in _STAGE_ORDER:
        stages[stage] = {
            "dependencies": dependencies[stage],
            "handler": _HANDLERS[stage],
            "tests": _stage_tests(stage),
        }
    source_fingerprint = hashlib.sha256(_canonical_bytes(source_files)).hexdigest()
    return {
        "schema_version": 1,
        "source_files": source_files,
        "source_fingerprint": source_fingerprint,
        "stages": stages,
        "contracts": {
            "core_stage_dependencies": dependencies,
            "materializer_stage_dependencies": materializer_dependencies,
            "attended_stages": list(_ATTENDED_ORDER),
            "preparation_stage_dependencies": preparation_dependencies,
            "source_contract": _SOURCE_CONTRACT,
            "source_schema": _SOURCE_SCHEMA,
        },
        "edges": edges,
        "relationships": relationships,
        "artifact_flows": artifact_flows,
        "excluded_sinks": {
            "workbook": "permanently held outside the MVP execution and acceptance path"
        },
        "unresolved_critical_edges": [],
        "limitations": [
            "AST records declared static relationships only; it does not prove runtime ordering or numerical correctness.",
            "Runtime source admission, recovery, ordering, and numerical guarantees remain covered by the linked integration tests.",
        ],
    }


def _stage_tests(stage: str) -> list[str]:
    links = ["tests/core/test_portfolio_pipeline_materialize.py"]
    if stage in {"ledger", "baseline", "attribution", "analytics"}:
        links.append("tests/core/test_portfolio_pipeline_orchestration.py")
    if stage in {"report", "panel"}:
        links.append("tests/core/test_portfolio_release_builder.py")
    if stage == "report":
        links.append("tests/portfolio_report/test_portfolio_report.py")
    if stage == "panel":
        links.append(
            "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py"
        )
    links.append("tests/core/test_attended_preparation.py")
    return links


def build_controller_graph(repo_root: Path) -> dict[str, Any]:
    """Build a deterministic static projection from current source bytes."""

    return _current_graph(repo_root)


def validate_controller_graph(repo_root: Path, graph: dict[str, Any]) -> dict[str, Any]:
    """Reject a stale or incomplete projection against current executable declarations."""

    if not isinstance(graph, dict) or graph.get("schema_version") != 1:
        raise ControllerGraphError("controller graph schema differs")
    current = _current_graph(repo_root)
    for field in (
        "source_files",
        "source_fingerprint",
        "stages",
        "contracts",
        "edges",
        "relationships",
        "artifact_flows",
        "excluded_sinks",
        "unresolved_critical_edges",
    ):
        if graph.get(field) != current[field]:
            raise ControllerGraphError(
                f"controller graph {field} differs from current source"
            )
    if graph.get("unresolved_critical_edges"):
        raise ControllerGraphError("controller graph has unresolved critical edges")
    return {
        "schema_version": 1,
        "source_fingerprint": current["source_fingerprint"],
        "stage_count": len(_STAGE_ORDER),
        "valid": True,
    }


def affected_stages(graph: dict[str, Any], changed_paths: list[str]) -> list[str]:
    """Return the declared downstream closure for changed production surfaces.

    Unknown ``tools/`` or core production paths require a reconciliation rather
    than being treated as harmless; unrelated documentation and test files are
    intentionally distinguishable and return no execution-stage impact.
    """

    if not isinstance(graph, dict) or graph.get("schema_version") != 1:
        raise ControllerGraphError("controller graph schema differs")
    stages = graph.get("stages")
    if not isinstance(stages, Mapping):
        raise ControllerGraphError("controller graph stages are absent")
    flows = graph.get("artifact_flows")
    if not isinstance(flows, list):
        raise ControllerGraphError("controller graph artifact_flows are absent")
    path_stages: dict[str, set[str]] = {
        path: set(stages) for path, stages in _PATH_STAGES.items()
    }
    for flow in flows:
        if not isinstance(flow, Mapping):
            raise ControllerGraphError("controller graph artifact-flow differs")
        source_paths = flow.get("source_paths")
        flow_stages = flow.get("stages")
        if not isinstance(source_paths, list) or not isinstance(flow_stages, list):
            raise ControllerGraphError("controller graph artifact-flow differs")
        if (
            not flow.get("consumers")
            or not flow.get("validators")
            or not flow.get("sinks")
        ):
            raise ControllerGraphError(
                "controller graph artifact-flow consumer or validator is absent"
            )
        for path in source_paths:
            if not isinstance(path, str):
                raise ControllerGraphError("controller graph artifact-flow differs")
            path_stages.setdefault(path, set()).update(
                stage for stage in flow_stages if isinstance(stage, str)
            )
    direct: set[str] = set()
    for changed_path in changed_paths:
        normalized = _relative(Path(changed_path))
        mapped = path_stages.get(normalized)
        if mapped is not None:
            direct.update(mapped)
        elif normalized.startswith(
            (
                "tools/",
                "packages/core/src/quant_engine/core/",
                "contracts/",
                "schemas/portfolio/",
            )
        ):
            raise ControllerGraphError(
                f"unmapped relevant production change requires reconciliation: {normalized}"
            )
    affected = set(direct)
    changed = True
    while changed:
        changed = False
        for stage, record in stages.items():
            if not isinstance(stage, str) or not isinstance(record, Mapping):
                raise ControllerGraphError("controller graph stage record differs")
            dependencies = record.get("dependencies")
            if not isinstance(dependencies, list):
                raise ControllerGraphError("controller graph stage dependencies differ")
            if (
                any(dependency in affected for dependency in dependencies)
                and stage not in affected
            ):
                affected.add(stage)
                changed = True
    return [stage for stage in _STAGE_ORDER if stage in affected]


def _git_changed_paths(
    repo_root: Path, changed_from: str, changed_to: str
) -> list[str]:
    """Read one CI-supplied Git range without depending on workstation state."""

    command: tuple[str, ...]
    if changed_from == "EMPTY_TREE":
        command = (
            "git",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-z",
            changed_to,
        )
    else:
        command = (
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            changed_from,
            changed_to,
        )
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ControllerGraphError("Git changed-path input is unavailable") from exc
    if completed.returncode:
        raise ControllerGraphError("Git changed-path range is invalid")
    try:
        paths = [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise ControllerGraphError("Git changed-path input is not UTF-8") from exc
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ControllerGraphError("Git changed-path input escapes repository root")
    return paths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--write", type=Path)
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-from")
    parser.add_argument("--changed-to")
    parser.add_argument("--mapper-evidence", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (args.changed_from is None) != (args.changed_to is None):
            raise ControllerGraphError(
                "Git changed-path input requires both --changed-from and --changed-to"
            )
        graph = build_controller_graph(args.repo_root)
        validation = validate_controller_graph(args.repo_root, graph)
        if args.write is not None:
            args.write.write_bytes(_canonical_bytes(graph) + b"\n")
        result: dict[str, object] = {"graph": graph, "validation": validation}
        changed_paths = list(args.changed_path)
        if args.changed_from is not None:
            assert args.changed_to is not None
            changed_paths.extend(
                _git_changed_paths(args.repo_root, args.changed_from, args.changed_to)
            )
        if changed_paths:
            result["affected_stages"] = affected_stages(graph, changed_paths)
        if args.changed_from is not None and not changed_paths:
            result["affected_stages"] = []
        if args.mapper_evidence is not None:
            result["mapper_evidence"] = _explicit_mapper_evidence(
                args.repo_root, args.mapper_evidence
            )
    except ControllerGraphError as exc:
        print(f"portfolio-controller-graph: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

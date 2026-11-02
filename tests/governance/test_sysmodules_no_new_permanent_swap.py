"""Guard: no test may add a new permanent ``sys.modules[...] = module`` registration.

A registration that is never undone replaces whatever importers already hold, so which
module instance a patch reaches depends on collection order (PR 747). ``tests/conftest.py``
neutralises the existing ones at runtime; this check stops the set from growing.

A site is *permanent* when it assigns ``sys.modules[key]`` and either sits at module level,
or sits in a function with no ``try``/``finally`` (or fixture ``yield``) that restores the
registry. ``monkeypatch.setitem(sys.modules, ...)``, a ``finally`` that pops or reassigns
the entry, and registering the test module itself (``__name__``) are not permanent.
The allowlist below holds the pre-existing sites per file and may only shrink: replace a
permanent registration with ``monkeypatch.setitem`` (or restore it in ``finally``) and
lower the number.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
SANCTIONED = {"tests/conftest.py"}

# Pre-existing permanent registrations, by file. Do not add entries or raise counts.
ALLOWED_PERMANENT_SWAPS: dict[str, int] = {
    "tests/acceptance/test_derive_b_combined.py": 1,
    "tests/acceptance/test_derive_b_sleeves.py": 1,
    "tests/acceptance/test_validate_generation.py": 2,
    "tests/core/mvp2_financial_fixture.py": 1,
    "tests/core/test_attended_preparation.py": 1,
    "tests/core/test_attended_source_phase.py": 3,
    "tests/core/test_capital_cross_root_identity.py": 1,
    "tests/core/test_directory_identity_agreement.py": 1,
    "tests/core/test_financial_artifacts_inventory.py": 1,
    "tests/core/test_portfolio_financial_mvp_contract.py": 1,
    "tests/core/test_portfolio_mvp2_vertical.py": 1,
    "tests/core/test_portfolio_pipeline_materialize.py": 3,
    "tests/core/test_portfolio_pipeline_orchestration.py": 1,
    "tests/core/test_portfolio_release_builder.py": 1,
    "tests/governance/test_codex_scope_guard.py": 1,
    "tests/governance/test_issue_guard.py": 1,
    "tests/governance/test_push_policy.py": 1,
    "tests/market_data/test_famafrench.py": 1,
    "tests/market_data/test_yahoo.py": 1,
    "tests/portfolio_analytics/test_derived_epoch_capital.py": 1,
    "tests/portfolio_analytics/test_portfolio_attribution_returns.py": 1,
    "tests/portfolio_analytics/test_portfolio_correlation.py": 1,
    "tests/portfolio_analytics/test_portfolio_current_decomposition_plan.py": 5,
    "tests/portfolio_analytics/test_portfolio_current_dependence.py": 1,
    "tests/portfolio_analytics/test_portfolio_decomposition.py": 1,
    "tests/portfolio_analytics/test_portfolio_decomposition_evidence_modes.py": 1,
    "tests/portfolio_analytics/test_portfolio_designer_model_curve.py": 1,
    "tests/portfolio_analytics/test_portfolio_diligence_rubric.py": 1,
    "tests/portfolio_analytics/test_portfolio_driver_map.py": 1,
    "tests/portfolio_analytics/test_portfolio_grizzly_signal_replay.py": 1,
    "tests/portfolio_analytics/test_portfolio_implementation_reconciliation.py": 1,
    "tests/portfolio_analytics/test_portfolio_implementation_return_attribution.py": 1,
    "tests/portfolio_analytics/test_portfolio_invested_capital.py": 2,
    "tests/portfolio_analytics/test_portfolio_layered_history.py": 2,
    "tests/portfolio_analytics/test_portfolio_provider_track_record.py": 1,
    "tests/portfolio_analytics/test_portfolio_provider_track_record_union.py": 1,
    "tests/portfolio_analytics/test_portfolio_public_decomposition_sources.py": 1,
    "tests/portfolio_analytics/test_portfolio_public_style_sources.py": 1,
    "tests/portfolio_analytics/test_portfolio_replication.py": 2,
    "tests/portfolio_analytics/test_portfolio_strategy_backtests.py": 1,
    "tests/portfolio_analytics/test_portfolio_strategy_history.py": 1,
    "tests/portfolio_analytics/test_portfolio_target_series.py": 1,
    "tests/portfolio_analytics/test_portfolio_vendor_nav_gap_fill.py": 1,
    "tests/portfolio_analytics/test_portfolio_weekly_returns.py": 2,
    "tests/portfolio_analytics/test_spy_spxl_admission.py": 1,
    "tests/portfolio_attribution/test_annualization_frequency_guard.py": 1,
    "tests/portfolio_attribution/test_p123_reconciliation_aliases.py": 1,
    "tests/portfolio_attribution/test_portfolio_attribution.py": 1,
    "tests/portfolio_attribution/test_tt_source_manifest.py": 1,
    "tests/portfolio_baseline/test_cost_evidence.py": 1,
    "tests/portfolio_baseline/test_portfolio_baseline.py": 1,
    "tests/portfolio_control_panel/test_account_manifest_generation.py": 4,
    "tests/portfolio_control_panel/test_account_strategy_manifest.py": 3,
    "tests/portfolio_control_panel/test_alpha_context.py": 1,
    "tests/portfolio_control_panel/test_current_capital_panel.py": 1,
    "tests/portfolio_control_panel/test_derived_sleeve_panel.py": 1,
    "tests/portfolio_control_panel/test_echo_current_product_boundary.py": 2,
    "tests/portfolio_control_panel/test_implementation_account_leg_seam.py": 1,
    "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py": 1,
    "tests/portfolio_control_panel/test_panel_render_audit_543.py": 1,
    "tests/portfolio_control_panel/test_performance_tab.py": 1,
    "tests/portfolio_control_panel/test_period_returns.py": 1,
    "tests/portfolio_control_panel/test_portfolio_control_panel.py": 6,
    "tests/portfolio_control_panel/test_portfolio_freshness.py": 1,
    "tests/portfolio_control_panel/test_quantbase_account_interval_receipt.py": 1,
    "tests/portfolio_control_panel/test_rebind_current_epoch_crosswalk.py": 1,
    "tests/portfolio_control_panel/test_source_clock_display.py": 1,
    "tests/portfolio_control_panel/test_stage_current_epoch_manifest.py": 1,
    "tests/portfolio_control_panel/test_stale_implementation_panel.py": 1,
    "tests/portfolio_control_panel/test_static_snapshot_coverage_receipts.py": 3,
    "tests/portfolio_control_panel/test_twr_payload_perimeter.py": 1,
    "tests/portfolio_fx_rates/test_portfolio_fx_rates.py": 1,
    "tests/portfolio_ledger/test_hwm_selected_inputs.py": 4,
    "tests/portfolio_operations/test_portfolio_control_recompute.py": 1,
    "tests/portfolio_operations/test_portfolio_rollforward_cycle.py": 1,
    "tests/portfolio_provenance/test_portfolio_provenance.py": 1,
    "tests/portfolio_provenance/test_strategy_version_identity.py": 1,
    "tests/portfolio_report/current_capital_fixture.py": 2,
    "tests/portfolio_report/test_current_capital_consumers.py": 2,
    "tests/portfolio_report/test_echo_current_product_exclusion.py": 2,
    "tests/portfolio_report/test_epoch_manifest_workbook_binding.py": 1,
    "tests/portfolio_report/test_implementation_account_leg_binding.py": 1,
    "tests/portfolio_report/test_implementation_reconciliation_contract.py": 1,
    "tests/portfolio_report/test_implementation_return_attribution_contract.py": 1,
    "tests/portfolio_report/test_n55_annualization_labels.py": 1,
    "tests/portfolio_report/test_period_returns_report.py": 1,
    "tests/portfolio_report/test_portfolio_report.py": 2,
    "tests/portfolio_report/test_report_publish_artifact_versions.py": 2,
    "tests/portfolio_report/test_returns_engine_consumption.py": 2,
    "tests/portfolio_report/test_schema_coverage_of_sleeve_blocks.py": 1,
    "tests/portfolio_report/test_source_clock_qc.py": 1,
    "tests/portfolio_report/test_stale_implementation_contracts.py": 1,
    "tests/portfolio_report/test_stale_implementation_contracts_real_bytes.py": 1,
    "tests/portfolio_report/test_strategy_window_opening_date.py": 2,
    "tests/portfolio_report/test_value_basis_bound_consumers.py": 1,
    "tests/provider_ledger/test_admit_or_declare.py": 1,
    "tests/provider_ledger/test_provider_ledger.py": 2,
    "tests/provider_ledger/test_rebind_provider_ledger_strategy_inputs.py": 2,
    "tests/provider_ledger/test_schwab_derived_patch_finality.py": 1,
    "tests/quantbase/test_quantbase_generation.py": 1,
    "tests/security/test_provider_security_quarantine.py": 1,
    "tests/security/test_repository_boundary.py": 1,
    "tests/security/test_restricted_evidence_summary.py": 1,
}


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _restores_registry(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_sys_modules(node.func.value)
                and node.func.attr in {"pop", "update", "clear"}
            ):
                return True
            if isinstance(node, ast.Delete) and any(
                isinstance(target, ast.Subscript) and _is_sys_modules(target.value)
                for target in node.targets
            ):
                return True
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Subscript) and _is_sys_modules(target.value)
                for target in node.targets
            ):
                return True
    return False


def _undone_by_following_try(node: ast.AST, parent: dict[ast.AST, ast.AST]) -> bool:
    """True when a later sibling ``try`` (at any enclosing level) restores the registry."""
    cursor: ast.AST = node
    while cursor in parent:
        container = parent[cursor]
        for field in ("body", "orelse", "finalbody", "handlers"):
            siblings = getattr(container, field, None)
            if isinstance(siblings, list) and cursor in siblings:
                later = siblings[siblings.index(cursor) + 1 :]
                if any(
                    isinstance(item, ast.Try) and _restores_registry(item.finalbody)
                    for item in later
                ):
                    return True
        cursor = container
    return False


def permanent_swaps(source: str) -> list[int]:
    """Line numbers of ``sys.modules[...] = ...`` assignments that nothing undoes."""
    tree = ast.parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        keys = [
            target.slice
            for target in node.targets
            if isinstance(target, ast.Subscript) and _is_sys_modules(target.value)
        ]
        if not keys or any("__name__" in ast.unparse(key) for key in keys):
            continue
        function: ast.AST | None = None
        restored = False
        cursor: ast.AST = node
        while cursor in parent:
            cursor = parent[cursor]
            if isinstance(cursor, ast.Try) and _restores_registry(cursor.finalbody):
                restored = True
            if function is None and isinstance(
                cursor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                function = cursor
        if restored or _undone_by_following_try(node, parent):
            continue
        if function is not None:
            body = getattr(function, "body", [])
            yields = any(isinstance(item, ast.Yield) for item in ast.walk(function))
            if yields and _restores_registry(body):
                continue
        lines.append(node.lineno)
    return lines


def _counts() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative in SANCTIONED:
            continue
        lines = permanent_swaps(path.read_text(encoding="utf-8"))
        if lines:
            found[relative] = lines
    return found


def test_no_test_adds_a_permanent_sys_modules_registration() -> None:
    found = _counts()
    grown = {
        file: lines
        for file, lines in found.items()
        if len(lines) > ALLOWED_PERMANENT_SWAPS.get(file, 0)
    }
    assert not grown, (
        "new permanent sys.modules registration; use monkeypatch.setitem(sys.modules, ...) "
        "or restore the entry in a finally block: "
        + "; ".join(f"{file} lines {lines}" for file, lines in sorted(grown.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    found = _counts()
    stale = {
        file: (allowed, len(found.get(file, [])))
        for file, allowed in ALLOWED_PERMANENT_SWAPS.items()
        if len(found.get(file, [])) < allowed
    }
    assert not stale, (
        "allowlist counts above the real number of permanent registrations; lower them: "
        f"{stale}"
    )


def test_detector_flags_module_level_and_unrestored_function_swaps() -> None:
    assert permanent_swaps("import sys\nsys.modules['m'] = object()\n") == [2]
    assert permanent_swaps(
        "import sys\ndef load():\n    sys.modules['m'] = object()\n"
    ) == [3]


def test_detector_ignores_restored_and_self_registration() -> None:
    restored = (
        "import sys\n"
        "def load():\n"
        "    previous = sys.modules.get('m')\n"
        "    sys.modules['m'] = object()\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        sys.modules.pop('m', None)\n"
    )
    assert permanent_swaps(restored) == []
    assert permanent_swaps("import sys\nsys.modules[__name__] = object()\n") == []
    assert (
        permanent_swaps(
            "def f(monkeypatch):\n    monkeypatch.setitem(sys.modules, 'm', object())\n"
        )
        == []
    )
    fixture = (
        "import sys\n"
        "def fx():\n"
        "    sys.modules['m'] = object()\n"
        "    yield\n"
        "    sys.modules.pop('m', None)\n"
    )
    assert permanent_swaps(fixture) == []

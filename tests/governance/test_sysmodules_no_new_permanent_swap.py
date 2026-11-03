"""Guard: no test may add a new permanent ``sys.modules[...] = module`` registration.

A registration that is never undone replaces whatever importers already hold, so which
module instance a patch reaches depends on collection order (PR 747). ``tests/conftest.py``
neutralises the existing ones at runtime; this check stops the set from growing.

A site is *permanent* when it assigns ``sys.modules[key]`` and either sits at module level,
or sits in a function with no ``try``/``finally`` (or fixture ``yield``) that restores that
same key (``update``/``clear`` restore any key). ``monkeypatch.setitem(sys.modules, ...)``, a
``finally`` that pops or reassigns the same entry, and registering the test module itself
(``__name__``) are not permanent.
The allowlist below holds the pre-existing sites, each named by file, enclosing function
and key expression, and may only shrink: replace a permanent registration with
``monkeypatch.setitem`` (or restore it in ``finally``) and drop its entry. Moving or
renaming a site, or trading one for a new one, fails both checks.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
SANCTIONED = {"tests/conftest.py"}

# Pre-existing permanent registrations: file -> "function|key" -> count. Do not add entries
# or raise counts.
ALLOWED_PERMANENT_SWAPS: dict[str, dict[str, int]] = {
    "tests/acceptance/test_derive_b_combined.py": {
        "load_module|spec.name": 1,
    },
    "tests/acceptance/test_derive_b_sleeves.py": {
        "load_module|spec.name": 1,
    },
    "tests/acceptance/test_hwm_envelope_sleeve.py": {
        "<module>|'portfolio_report'": 1,
    },
    "tests/acceptance/test_validate_generation.py": {
        "_load_provider_ledger_tests|spec.name": 1,
        "_load_report_producer_fixture|spec.name": 1,
    },
    "tests/core/mvp2_financial_fixture.py": {
        "_load|name": 1,
    },
    "tests/core/test_attended_preparation.py": {
        "<module>|HISTORY_TEST_SPEC.name": 1,
    },
    "tests/core/test_attended_source_phase.py": {
        "<module>|FIXTURE_SPEC.name": 1,
        "<module>|REPORT_SPEC.name": 1,
        "<module>|SPEC.name": 1,
    },
    "tests/core/test_capital_cross_root_identity.py": {
        "_load|module_name": 1,
    },
    "tests/core/test_directory_identity_agreement.py": {
        "_load|module_name": 1,
    },
    "tests/core/test_financial_artifacts_inventory.py": {
        "_load_module|spec.name": 1,
    },
    "tests/core/test_portfolio_financial_mvp_contract.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/core/test_portfolio_mvp2_vertical.py": {
        "<module>|FIXTURE_SPEC.name": 1,
    },
    "tests/core/test_portfolio_pipeline_materialize.py": {
        "<module>|FIXTURE_SPEC.name": 1,
        "<module>|HWM_FIXTURE_SPEC.name": 1,
        "<module>|SELECTED_HWM_FIXTURE_SPEC.name": 1,
    },
    "tests/core/test_portfolio_pipeline_orchestration.py": {
        "<module>|FIXTURE_SPEC.name": 1,
    },
    "tests/core/test_portfolio_release_builder.py": {
        "test_publish_panel_forwards_dependence_inputs_to_every_consumer|module_name": 1,
    },
    "tests/governance/test_codex_scope_guard.py": {
        "_module|spec.name": 1,
    },
    "tests/governance/test_issue_guard.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/governance/test_push_policy.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/market_data/test_famafrench.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/market_data/test_yahoo.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio123/test_capture_admission.py": {
        "_load_provenance|spec.name": 1,
    },
    "tests/portfolio_analytics/test_derived_epoch_capital.py": {
        "_load_builder|spec.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_attribution_returns.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_correlation.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_current_decomposition_plan.py": {
        "<module>|ENGINE_SPEC.name": 1,
        "<module>|HISTORY_TEST_SPEC.name": 1,
        "<module>|SPEC.name": 1,
        "_release_builder_module|release_spec.name": 1,
        "test_release_builder_rejects_substituted_upstream_evidence|release_spec.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_current_dependence.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_decomposition.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_decomposition_evidence_modes.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_designer_model_curve.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_diligence_rubric.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_driver_map.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_grizzly_signal_replay.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_implementation_reconciliation.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_implementation_return_attribution.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_invested_capital.py": {
        "_t5c_constructor_world|cli_spec.name": 1,
        "_t5c_constructor_world|fixture_spec.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_layered_history.py": {
        "<module>|SPEC.name": 1,
        "test_layered_history_rejects_dynamic_validator_with_different_core|validator_spec.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_provider_track_record.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_provider_track_record_union.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_public_decomposition_sources.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_public_style_sources.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_replication.py": {
        "<module>|DECOMPOSITION_SPEC.name": 1,
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_strategy_backtests.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_strategy_history.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_target_series.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_vendor_nav_gap_fill.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_portfolio_weekly_returns.py": {
        "<module>|PANEL_SPEC.name": 1,
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_spy_spxl_admission.py": {
        "_load|name": 1,
    },
    "tests/portfolio_analytics/test_strategy_basis_lens_moves.py": {
        "test_the_panel_binds_a_table_and_refuses_findings_or_a_changed_sentence|spec.name": 1,
    },
    "tests/portfolio_analytics/test_strategy_basis_lenses.py": {
        "<module>|_PLAN_SPEC.name": 1,
    },
    "tests/portfolio_analytics/test_strategy_basis_lenses_r2.py": {
        "<module>|_SPEC.name": 1,
    },
    "tests/portfolio_attribution/test_annualization_frequency_guard.py": {
        "_load_fixture_module|spec.name": 1,
    },
    "tests/portfolio_attribution/test_p123_reconciliation_aliases.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_attribution/test_portfolio_attribution.py": {
        "<module>|SPEC.name": 1,
        "test_baseline_percent_output_flows_through_attribution_to_the_report_row.load|name": 1,
    },
    "tests/portfolio_attribution/test_tt_source_manifest.py": {
        "_load|name": 1,
    },
    "tests/portfolio_attribution/test_tt_truncated_row_consumer.py": {
        "_load|name": 1,
    },
    "tests/portfolio_baseline/test_cost_evidence.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_baseline/test_cost_rates.py": {
        "_load|name": 1,
    },
    "tests/portfolio_baseline/test_owner_declared_cost.py": {
        "_load|name": 1,
    },
    "tests/portfolio_baseline/test_portfolio_baseline.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_account_manifest_generation.py": {
        "perimeter_module|spec.name": 1,
        "test_generation_available_mwr_reaches_bound_report_and_panel|panel_fixture_name": 1,
        "test_generation_available_mwr_reaches_bound_report_and_panel|report_fixture_name": 1,
        "verifier_module|spec.name": 1,
    },
    "tests/portfolio_control_panel/test_account_strategy_manifest.py": {
        "independent_verifier_module|spec.name": 1,
        "manifest_module|spec.name": 1,
        "provider_ledger_module|spec.name": 1,
    },
    "tests/portfolio_control_panel/test_alpha_context.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_cost_rate_render.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_current_capital_panel.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_derived_sleeve_panel.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_echo_current_product_boundary.py": {
        "<module>|REPORT_SPEC.name": 1,
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_implementation_account_leg_seam.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py": {
        "_load_panel_module|spec.name": 1,
    },
    "tests/portfolio_control_panel/test_panel_render_audit_543.py": {
        "_builder|name": 1,
    },
    "tests/portfolio_control_panel/test_performance_tab.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_period_returns.py": {
        "_load|spec.name": 1,
    },
    "tests/portfolio_control_panel/test_portfolio_control_panel.py": {
        "<module>|MATERIALIZER_SPEC.name": 1,
        "<module>|SPEC.name": 1,
        "<module>|_CAPITAL_FIXTURE_SPEC.name": 1,
        "_panel_verified_payloads|fixture_name": 1,
        "_panel_verified_payloads|producer_name": 1,
        "_panel_verified_payloads|verifier_name": 1,
    },
    "tests/portfolio_control_panel/test_portfolio_freshness.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_quantbase_account_interval_receipt.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_rebind_current_epoch_crosswalk.py": {
        "_load_runner|spec.name": 1,
    },
    "tests/portfolio_control_panel/test_source_clock_display.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_control_panel/test_stage_current_epoch_manifest.py": {
        "_load_module|name": 1,
    },
    "tests/portfolio_control_panel/test_stale_implementation_panel.py": {
        "_load|name": 1,
    },
    "tests/portfolio_control_panel/test_static_snapshot_coverage_receipts.py": {
        "_load_module|name": 1,
        "receipts_module|spec.name": 1,
        "test_public_manifest_predicate_rejects_rebound_static_fact_count|manifest_spec.name": 1,
    },
    "tests/portfolio_control_panel/test_twr_payload_perimeter.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_fx_rates/test_portfolio_fx_rates.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_ledger/test_hwm_selected_inputs.py": {
        "_load_account_builder_fixture|builder_spec.name": 1,
        "_load_account_builder_fixture|fixture_spec.name": 1,
        "test_mvp2_matrixport_coverage_declares_nav_gap_in_producer_shape|spec.name": 1,
        "test_mvp2_matrixport_declared_nav_gap_omits_observation_and_spans_hwm|spec.name": 1,
    },
    "tests/portfolio_operations/test_portfolio_control_recompute.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_operations/test_portfolio_rollforward_cycle.py": {
        "_load_tool|spec.name": 1,
    },
    "tests/portfolio_operations/test_release_b7_sources.py": {
        "_load|name": 1,
    },
    "tests/portfolio_operations/test_release_ledger_spec_e2e.py": {
        "_fixtures|spec.name": 1,
    },
    "tests/portfolio_provenance/test_portfolio_provenance.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_provenance/test_strategy_version_identity.py": {
        "_load_provenance|spec.name": 1,
    },
    "tests/portfolio_report/current_capital_fixture.py": {
        "cr2_custody_native_recipe.load|name": 1,
        "cr2_custody_native_source.load|name": 1,
    },
    "tests/portfolio_report/test_cost_rate_disclosure.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_current_capital_consumers.py": {
        "_load|name": 1,
        "producer_attribution|spec.name": 1,
    },
    "tests/portfolio_report/test_echo_current_product_exclusion.py": {
        "<module>|SPEC.name": 1,
        "<module>|_CAPITAL_FIXTURE_SPEC.name": 1,
    },
    "tests/portfolio_report/test_epoch_manifest_workbook_binding.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_report/test_hwm_envelope_basis.py": {
        "<module>|'portfolio_report'": 1,
    },
    "tests/portfolio_report/test_implementation_account_leg_binding.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_implementation_reconciliation_contract.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_report/test_implementation_return_attribution_contract.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/portfolio_report/test_n55_annualization_labels.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_period_returns_report.py": {
        "reporter|spec.name": 1,
    },
    "tests/portfolio_report/test_portfolio_report.py": {
        "<module>|SPEC.name": 1,
        "<module>|_CAPITAL_FIXTURE_SPEC.name": 1,
    },
    "tests/portfolio_report/test_report_publish_artifact_versions.py": {
        "test_the_producers_that_the_report_seals_write_a_declared_version|spec.name": 1,
        "test_the_typed_versions_are_what_producers_write.load|spec.name": 1,
    },
    "tests/portfolio_report/test_returns_engine_consumption.py": {
        "<module>|SPEC.name": 1,
        "_full_report_fixture|spec.name": 1,
    },
    "tests/portfolio_report/test_schema_coverage_of_sleeve_blocks.py": {
        "_load_perf_fixture|spec.name": 1,
    },
    "tests/portfolio_report/test_source_clock_qc.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_stale_implementation_contracts.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_stale_implementation_contracts_real_bytes.py": {
        "_load|name": 1,
    },
    "tests/portfolio_report/test_strategy_window_opening_date.py": {
        "_load_consumers|spec.name": 1,
        "reporter|spec.name": 1,
    },
    "tests/portfolio_report/test_value_basis_bound_consumers.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/provider_ledger/test_admit_or_declare.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/provider_ledger/test_provider_ledger.py": {
        "<module>|SPEC.name": 1,
        "_actual_ibkr_period_catalog|fixture_spec.name": 1,
    },
    "tests/provider_ledger/test_quantbase_har_role_coverage.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/provider_ledger/test_rebind_provider_ledger_strategy_inputs.py": {
        "_load_ledger|spec.name": 1,
        "_load_runner|spec.name": 1,
    },
    "tests/provider_ledger/test_schwab_derived_patch_finality.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/quantbase/test_cdp_har_recorder.py": {
        "_helpers|spec.name": 1,
    },
    "tests/quantbase/test_generation_default_unchanged.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/quantbase/test_har_intake.py": {
        "_load|name": 1,
    },
    "tests/quantbase/test_har_to_csv.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/quantbase/test_native_manifest.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/quantbase/test_nav_grid.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/quantbase/test_quantbase_generation.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/security/test_governed_secret_files.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/security/test_provider_security_quarantine.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/security/test_repository_boundary.py": {
        "<module>|SPEC.name": 1,
    },
    "tests/security/test_restricted_evidence_summary.py": {
        "<module>|SPEC.name": 1,
    },
}


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _registry_keys(node: ast.Assign | ast.Delete) -> list[ast.expr]:
    return [
        target.slice
        for target in node.targets
        if isinstance(target, ast.Subscript) and _is_sys_modules(target.value)
    ]


def _same_key(assigned: set[str], cleaned: ast.expr) -> bool:
    """A cleanup names the assigned key, or a key too dynamic to compare (a name)."""
    if not isinstance(cleaned, ast.Constant):
        return True
    return ast.unparse(cleaned) in assigned or not all(
        key.startswith(("'", '"')) for key in assigned
    )


def _restores_registry(statements: list[ast.stmt], assigned: set[str]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_sys_modules(node.func.value)
            ):
                if node.func.attr in {"update", "clear"}:
                    return True
                if node.func.attr == "pop" and node.args:
                    if _same_key(assigned, node.args[0]):
                        return True
            if isinstance(node, (ast.Delete, ast.Assign)) and any(
                _same_key(assigned, key) for key in _registry_keys(node)
            ):
                return True
    return False


def _undone_by_following_try(
    node: ast.AST, parent: dict[ast.AST, ast.AST], assigned: set[str]
) -> bool:
    """True when a later sibling ``try`` (at any enclosing level) restores the registry."""
    cursor: ast.AST = node
    while cursor in parent:
        container = parent[cursor]
        for field in ("body", "orelse", "finalbody", "handlers"):
            siblings = getattr(container, field, None)
            if isinstance(siblings, list) and cursor in siblings:
                later = siblings[siblings.index(cursor) + 1 :]
                if any(
                    isinstance(item, ast.Try)
                    and _restores_registry(item.finalbody, assigned)
                    for item in later
                ):
                    return True
        cursor = container
    return False


def permanent_sites(source: str) -> list[tuple[int, str]]:
    """``(line, "function|key")`` of ``sys.modules[...] = ...`` assignments nothing undoes."""
    tree = ast.parse(source)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        keys = _registry_keys(node)
        if not keys or any("__name__" in ast.unparse(key) for key in keys):
            continue
        assigned = {ast.unparse(key) for key in keys}
        function: ast.AST | None = None
        names: list[str] = []
        restored = False
        cursor: ast.AST = node
        while cursor in parent:
            cursor = parent[cursor]
            if isinstance(cursor, ast.Try) and _restores_registry(
                cursor.finalbody, assigned
            ):
                restored = True
            if isinstance(
                cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.append(cursor.name)
                if function is None and not isinstance(cursor, ast.ClassDef):
                    function = cursor
        if restored or _undone_by_following_try(node, parent, assigned):
            continue
        if function is not None:
            body = getattr(function, "body", [])
            yields = any(isinstance(item, ast.Yield) for item in ast.walk(function))
            if yields and _restores_registry(body, assigned):
                continue
        scope = ".".join(reversed(names)) or "<module>"
        sites.append((node.lineno, f"{scope}|{', '.join(sorted(assigned))}"))
    return sites


def permanent_swaps(source: str) -> list[int]:
    """Line numbers of ``sys.modules[...] = ...`` assignments that nothing undoes."""
    return [line for line, _ in permanent_sites(source)]


def _found() -> dict[str, Counter[str]]:
    found: dict[str, Counter[str]] = {}
    for path in sorted(TESTS.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative in SANCTIONED:
            continue
        sites = permanent_sites(path.read_text(encoding="utf-8"))
        if sites:
            found[relative] = Counter(site for _, site in sites)
    return found


def test_no_test_adds_a_permanent_sys_modules_registration() -> None:
    grown = {
        file: dict(sites - Counter(ALLOWED_PERMANENT_SWAPS.get(file, {})))
        for file, sites in _found().items()
        if sites - Counter(ALLOWED_PERMANENT_SWAPS.get(file, {}))
    }
    assert not grown, (
        "new permanent sys.modules registration; use monkeypatch.setitem(sys.modules, ...) "
        "or restore the entry in a finally block: "
        + "; ".join(f"{file} {sites}" for file, sites in sorted(grown.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    found = _found()
    stale = {
        file: dict(Counter(allowed) - found.get(file, Counter()))
        for file, allowed in ALLOWED_PERMANENT_SWAPS.items()
        if Counter(allowed) - found.get(file, Counter())
    }
    assert not stale, (
        f"allowlist sites no longer permanent or moved; remove or rename them: {stale}"
    )


def test_detector_flags_module_level_and_unrestored_function_swaps() -> None:
    assert permanent_swaps("import sys\nsys.modules['m'] = object()\n") == [2]
    assert permanent_swaps(
        "import sys\ndef load():\n    sys.modules['m'] = object()\n"
    ) == [3]


def test_cleanup_of_a_different_key_does_not_restore_the_assignment() -> None:
    other_key = (
        "import sys\n"
        "def load():\n"
        "    sys.modules['new'] = object()\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        sys.modules.pop('other', None)\n"
    )
    assert permanent_swaps(other_key) == [3]
    same_key = other_key.replace("'other'", "'new'")
    assert permanent_swaps(same_key) == []


def test_site_identity_names_function_and_key() -> None:
    source = "import sys\ndef load():\n    sys.modules['m'] = object()\n"
    assert permanent_sites(source) == [(3, "load|'m'")]


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

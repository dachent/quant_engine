"""Static, source-bound projection of the supported portfolio controller.

This gate reads declarations with :mod:`ast`; it never imports or runs a
financial producer.  The executable materializer and core pipeline remain the
authority for runtime ordering, source validation, recovery, and numerics.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence, cast


class ControllerGraphError(ValueError):
    """The static projection is stale, incomplete, or internally inconsistent."""


_CORE = "packages/core/src/quant_engine/core/pipeline.py"
_MATERIALIZER = "tools/portfolio_pipeline_materialize.py"
_RELEASE = "tools/portfolio_release_builder.py"
_FRESHNESS = "tools/portfolio_freshness.py"
_REPORT = "tools/portfolio_report.py"
_PANEL = "tools/portfolio_control_panel.py"
_PREPARATION = "tools/portfolio_attended_preparation.py"
_TARGET_SERIES = "tools/portfolio_target_series.py"
_DEPENDENCE = "tools/portfolio_current_dependence.py"
_DECOMPOSITION_PLAN = "tools/portfolio_current_decomposition_plan.py"
_PROVIDER_LEDGER = "tools/provider_ledger.py"
_LEDGER_RETURNS = (
    "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/returns.py"
)
_BASELINE = "tools/portfolio_baseline.py"
_EPOCH_MANIFEST = "tools/stage_current_epoch_manifest.py"
_ACCOUNT_MANIFEST = "tools/build_account_manifest_generation.py"
_ACCOUNT_MANIFEST_VERIFY = "tools/verify_account_manifest_generation.py"
_HWM = "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_governed_execution.py"
_HWM_SELECTED = "tools/portfolio_hwm_selected_inputs.py"
_HWM_CALENDAR = "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_calendar_authority.py"
_HWM_GOVERNED = (
    "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/hwm_governed.py"
)
_CAPITAL_INPUT = "ops/mvp2-release/scripts/build_capital_input.py"
_CAPITAL = "tools/portfolio_invested_capital.py"
_ATTRIBUTION = "tools/portfolio_attribution.py"
_DECOMPOSITION = "tools/portfolio_decomposition.py"
_CORRELATION = "tools/portfolio_correlation.py"
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
    # The attended runner's storage, retained-cache overlay and run-state gates. The
    # materializer imports it and it can refuse or fail any stage, so it carries the
    # controller's impact; it writes no delivery artifact itself.
    "packages/core/src/quant_engine/core/run_storage.py": list(_STAGE_ORDER),
    _RELEASE: list(_STAGE_ORDER),
    # WS4 T1.4 (corrective program): `commit_release` here is the coordinated
    # publication sink every stage's release commit reaches (directly, or via
    # `execute_pipeline`/`tools/data_pipeline.py execute --commit`), so a
    # change to either genuinely can affect every stage's delivery -- declared
    # with real stage impact, not `[]`, unlike the audit/guard tools below.
    "packages/core/src/quant_engine/core/release.py": list(_STAGE_ORDER),
    "tools/data_pipeline.py": list(_STAGE_ORDER),
    _CORRELATION: ["analytics", "report", "panel"],
    "tools/portfolio_counted_producer.py": ["analytics", "report", "panel"],
    # The implementation reconciliation and return-attribution builders write the
    # two hash-bound contracts the report reads, and the report imports the
    # reconciliation module for the P123 policy evidence.  A change to either
    # changes the contract bytes the report is bound to, so the report (and the
    # panel that renders it) must be re-run.
    "tools/portfolio_implementation_reconciliation.py": ["report", "panel"],
    "tools/portfolio_implementation_return_attribution.py": ["report", "panel"],
    # Consumed only by the panel, which reads its weekly series.
    "tools/portfolio_weekly_returns.py": ["panel"],
    # The holdings identity sidecar: built by the report producer into the report
    # payload, and re-derived by the report verifier.  A change moves report bytes.
    "tools/holdings_identity.py": ["report", "panel"],
    "tools/verify_holdings_identity.py": ["report", "panel"],
    # Audit, guard and CI tooling. None of these is imported by a producer and
    # none writes a delivery artifact, so a change to any of them affects no
    # stage. They are declared rather than omitted because an omitted `tools/`
    # path raises "unmapped relevant production change" instead of being
    # treated as harmless -- two of these tripped that gate during the #374
    # integration, one of them on a pure type-annotation change.
    "tools/check_architecture.py": [],
    "tools/check_data_roots.py": [],
    "tools/verify_release_restore.py": [],
    "tools/check_gitleaks_suppressions.py": [],
    "tools/check_push_policy.py": [],
    "tools/check_repository_boundary.py": [],
    "tools/ci.py": [],
    "tools/stall_watchdog.py": [],
    # The nested-strategy tag map (#567 stage 1): policy validation and tag
    # resolution only; no producer imports it yet, so a change affects no stage.
    "tools/nested_strategy_tag_map.py": [],
    "tools/sleeve_ledger.py": [],
    "tools/sleeve_nav.py": [],
    # #567 stage 5: the post-ledger sleeve-facts builder; nothing imports it yet.
    "tools/build_sleeve_account_facts.py": [],
    "tools/sleeve_inputs.py": [],
    "tools/verify_sleeve_ledger.py": [],
    "schemas/portfolio/nested-strategy-tag-map.schema.json": [],
    "schemas/portfolio/sleeve-account-facts.schema.json": [],
    # Recovery-rehearsal harness: exercises commit/rollback/recovery mechanics
    # against synthetic, fabricated releases (never real production data or a
    # real delivery artifact); no producer imports it.
    "packages/core/src/quant_engine/core/rehearsal.py": [],
    # Provider capability protocols and registry, re-exported by the package root:
    # a contract no producer imports yet, so no stage impact.
    "packages/core/src/quant_engine/core/providers.py": [],
    "packages/core/src/quant_engine/core/__init__.py": [],
    # WS4 T1.4 (corrective program) scope note: this file is outside that
    # brief's literal write scope, but this one-line declaration for each new
    # `check_*.py` guard is forced by this gate itself -- an undeclared
    # `tools/` path fails CI as "unmapped relevant production change" -- so it
    # is the minimal change needed, not a broader edit to this file.
    "tools/check_retained_authority_pins.py": [],
    "tools/check_release_validation_findings.py": [],
    # WS1b T1.5 (corrective program): another `check_*.py` guard, same reason as
    # its neighbors above -- it gates `evidence/releases/<date>/` closure
    # completeness (N1), reading only that record's own declared pins; no
    # producer imports it and it writes no delivery artifact.
    "tools/check_release_closure.py": [],
    "tools/codex_scope_guard.py": [],
    "tools/issue_guard.py": [],
    "tools/merge_readiness.py": [],
    # Acceptance runner entrypoint (corrective program WS5). Its checks live in
    # top-level `acceptance/validate/`; it reads published outputs and imports no
    # producer, so a change to it affects no execution stage.
    "tools/validate_generation.py": [],
    # Derived release-findings producer (ruling B / C1): binds every non-panel
    # release target to the panel's own findings. Its checks live in
    # `acceptance/validate/release_targets.py`; it reads a prepared release.json
    # and published generation.json/panel bytes, and imports no producer, so a
    # change to it affects no execution stage.
    "tools/validate_release_targets.py": [],
    # Rollforward-cycle verifiers (docs/data-rollforward.md). They re-read the freshness
    # artifact and a staged panel candidate to write a local cycle receipt; no producer
    # imports them and they write no delivery artifact, so a change affects no stage.
    # Declared because an omitted `tools/` path is an unmapped production change.
    "tools/portfolio_control_recompute.py": [],
    "tools/portfolio_rollforward_cycle.py": [],
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
        "contracts": (
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
        "writers": ((_PREPARATION, "prepare_attended_inputs"),),
        "stages": _STAGE_ORDER,
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "layered-history-generation",
        "source_paths": (
            _TARGET_SERIES,
            _RELEASE,
            "tools/portfolio_designer_model_curve.py",
            "tools/portfolio_strategy_history.py",
            "tools/portfolio_strategy_backtests.py",
            "tools/portfolio_provider_track_record.py",
            "tools/account_strategy_manifest.py",
            _PREPARATION,
            "contracts/portfolio-financial-mvp-source-v1.json",
            "schemas/portfolio/layered-history-generation.schema.json",
        ),
        "contracts": (
            "contracts/portfolio-financial-mvp-source-v1.json",
            "schemas/portfolio/account-strategy-manifest.schema.json",
            "schemas/portfolio/layered-history-generation.schema.json",
        ),
        "producer": (_TARGET_SERIES, "build_layered_history_generation"),
        "artifacts": ("layered-history generation",),
        "consumers": (
            (_DEPENDENCE, "build_source"),
            (_DECOMPOSITION_PLAN, "build_bundle"),
            (_RELEASE, "build_attribution"),
        ),
        "validators": ((_TARGET_SERIES, "verify_layered_history_generation"),),
        "writers": ((_TARGET_SERIES, "write_layered_history_generation"),),
        "stages": ("baseline", "attribution", "analytics", "report", "panel"),
        "tests": (
            "tests/core/test_attended_preparation.py",
            "tests/core/test_portfolio_release_builder.py",
        ),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "qsi-source-revision",
        "source_paths": (
            "tools/portfolio_strategy_history.py",
            "tools/portfolio_strategy_backtests.py",
            _TARGET_SERIES,
        ),
        "contracts": (_SOURCE_CONTRACT,),
        "producer": ("tools/portfolio_strategy_history.py", "write_qsi_refresh_bundle"),
        "artifacts": ("QSI source revision bundle", "strategy-backtests generation"),
        "consumers": (
            ("tools/portfolio_strategy_backtests.py", "stage_candidate"),
            (_TARGET_SERIES, "_qsi_source_revision_admission"),
        ),
        "validators": (
            ("tools/portfolio_strategy_history.py", "verify_qsi_refresh_bundle"),
            (_TARGET_SERIES, "validate_layered_history_generation"),
        ),
        "writers": (
            ("tools/portfolio_strategy_history.py", "write_qsi_refresh_bundle"),
        ),
        "stages": ("baseline", "attribution", "analytics", "report", "panel"),
        "tests": (
            "tests/portfolio_analytics/test_portfolio_strategy_history.py",
            "tests/portfolio_analytics/test_portfolio_layered_history.py",
        ),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "current-dependence-source",
        "source_paths": (
            _DEPENDENCE,
            _PREPARATION,
            _TARGET_SERIES,
            "contracts/portfolio-dependence-policy-v1.json",
            "contracts/portfolio-financial-mvp-source-v1.json",
        ),
        "contracts": (
            "contracts/portfolio-dependence-policy-v1.json",
            "contracts/portfolio-financial-mvp-source-v1.json",
        ),
        "producer": (_DEPENDENCE, "build_source"),
        "artifacts": ("current dependence source",),
        "consumers": ((_RELEASE, "create_recipe"),),
        "validators": ((_DEPENDENCE, "verify_source"),),
        "writers": ((_DEPENDENCE, "write_source"),),
        "stages": ("attribution", "analytics", "report", "panel"),
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "current-decomposition-plan-bundle",
        "source_paths": (
            _DECOMPOSITION_PLAN,
            _PREPARATION,
            _TARGET_SERIES,
            "contracts/portfolio-performance-policy-v1.json",
            "schemas/portfolio/base-decomposition-spec.schema.json",
        ),
        "contracts": (
            "contracts/portfolio-financial-mvp-source-v1.json",
            "contracts/portfolio-performance-policy-v1.json",
            "schemas/portfolio/base-decomposition-spec.schema.json",
        ),
        "producer": (_DECOMPOSITION_PLAN, "build_bundle"),
        "artifacts": ("current decomposition plan bundle",),
        "consumers": ((_RELEASE, "build_attribution"), (_RELEASE, "build_analytics")),
        "validators": ((_DECOMPOSITION_PLAN, "build_bundle"),),
        "writers": ((_DECOMPOSITION_PLAN, "write_bundle"),),
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
        "id": "retained-public-factor-capture",
        "source_paths": (
            _PREPARATION,
            _DECOMPOSITION_PLAN,
            _RELEASE,
        ),
        "contracts": ("contracts/portfolio-financial-mvp-source-v1.json",),
        "producer": (_PREPARATION, "_retained_public_factor_capture"),
        "artifacts": ("inputs/governance/retained-public-factor-capture.json",),
        "consumers": (
            (_DECOMPOSITION_PLAN, "_verify_public_factor_manifest"),
            (_RELEASE, "_verify_current_plan_upstream_evidence"),
            (_RELEASE, "_expected_non_spec_data_age"),
        ),
        "validators": (
            (_DECOMPOSITION_PLAN, "_validated_retained_public_factor_capture"),
            (_RELEASE, "_retained_public_factor_capture"),
        ),
        "writers": (
            (_DECOMPOSITION_PLAN, "build_bundle"),
            (_DECOMPOSITION_PLAN, "write_bundle"),
        ),
        "stages": ("attribution", "analytics", "report", "panel"),
        "tests": (
            "tests/core/test_attended_preparation.py",
            "tests/portfolio_analytics/test_portfolio_current_decomposition_plan.py",
            "tests/core/test_portfolio_release_builder.py",
        ),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "provider-ledger-generation",
        "source_paths": (
            _PROVIDER_LEDGER,
            _LEDGER_RETURNS,
            "tools/quantbase_generation.py",
            "tools/schwab_generation.py",
            _RELEASE,
            "schemas/portfolio/provider-admission-receipt.schema.json",
            "schemas/portfolio/provider-ledger.schema.json",
        ),
        "contracts": (
            "schemas/portfolio/provider-admission-receipt.schema.json",
            "schemas/portfolio/provider-ledger.schema.json",
        ),
        "producer": (_PROVIDER_LEDGER, "stage_candidate"),
        "artifacts": (
            "provider-ledger.json",
            "generation.json",
            "admit-or-declare-receipt.json",
        ),
        "consumers": (
            (_RELEASE, "build_ledger"),
            (_PREPARATION, "prepare_attended_inputs"),
        ),
        "validators": ((_PROVIDER_LEDGER, "verify_candidate"),),
        "writers": ((_RELEASE, "build_ledger"),),
        "stages": _STAGE_ORDER,
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "provider-ledger-freshness-health",
        "source_paths": (
            _RELEASE,
            _FRESHNESS,
            _REPORT,
            _PANEL,
            "schemas/core/freshness-assessment.schema.json",
        ),
        "contracts": (
            "schemas/core/freshness-assessment.schema.json",
            "schemas/portfolio/provider-ledger.schema.json",
        ),
        "producer": (_FRESHNESS, "build_health"),
        "artifacts": ("freshness-health.json",),
        "consumers": (
            (_REPORT, "_freshness_assessments"),
            (_PANEL, "_freshness_health"),
        ),
        "validators": (
            (_REPORT, "_freshness_assessments"),
            (_PANEL, "_freshness_health"),
        ),
        "writers": ((_FRESHNESS, "write_health"),),
        "stages": ("ledger",),
        "tests": (
            "tests/core/test_portfolio_controller_graph.py",
            "tests/portfolio_control_panel/test_portfolio_freshness.py",
            "tests/portfolio_report/test_portfolio_report.py",
        ),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "baseline-generation",
        "source_paths": (
            _BASELINE,
            _RELEASE,
            _PREPARATION,
            "schemas/portfolio/baseline-catalog.schema.json",
            # ``_load_spec`` reads the baseline spec JSON and applies its shape/
            # version checks itself; this schema declares that manually validated
            # input contract, rather than claiming a runtime schema-file import.
            "schemas/portfolio/baseline-spec.schema.json",
        ),
        "contracts": (
            "contracts/portfolio-financial-mvp-source-v1.json",
            "schemas/portfolio/baseline-catalog.schema.json",
            "schemas/portfolio/baseline-spec.schema.json",
        ),
        "producer": (_BASELINE, "stage_baseline"),
        "artifacts": ("portfolio-baseline.json", "generation.json"),
        "consumers": ((_RELEASE, "build_baseline"), (_DEPENDENCE, "build_source")),
        "validators": ((_BASELINE, "stage_baseline"),),
        "writers": ((_BASELINE, "publish_baseline"),),
        "stages": ("baseline", "attribution", "analytics", "report", "panel"),
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": (
            "report-json-html",
            "panel-json-html",
            "coordinated-publication-pointers",
        ),
    },
    {
        "id": "epoch-account-manifest-generation",
        "source_paths": (
            _EPOCH_MANIFEST,
            _ACCOUNT_MANIFEST,
            _ACCOUNT_MANIFEST_VERIFY,
            "tools/account_strategy_manifest.py",
            "packages/portfolio-ledger/src/quant_engine/portfolio_ledger/returns.py",
            "tools/rebind_current_epoch_crosswalk.py",
            _PREPARATION,
            "schemas/portfolio/account-strategy-manifest.schema.json",
            "schemas/portfolio/account-manifest-independent-verification.schema.json",
            "schemas/portfolio/account-assignment-perimeter.schema.json",
            "schemas/portfolio/account-strategy-current-epoch-crosswalk.schema.json",
        ),
        "contracts": (
            "schemas/portfolio/account-strategy-manifest.schema.json",
            "schemas/portfolio/account-manifest-independent-verification.schema.json",
            "schemas/portfolio/account-assignment-perimeter.schema.json",
        ),
        "producer": (_ACCOUNT_MANIFEST, "stage_account_manifest_generation"),
        "artifacts": (
            "account-strategy-manifest generation",
            "current-epoch crosswalk",
        ),
        "consumers": (
            (_CAPITAL_INPUT, "build_input_generation"),
            (_PREPARATION, "prepare_attended_inputs"),
        ),
        "validators": (
            (_ACCOUNT_MANIFEST_VERIFY, "IndependentAccountManifestVerifier.verify"),
        ),
        "writers": ((_EPOCH_MANIFEST, "stage_current_epoch_manifest"),),
        "stages": ("baseline", "attribution", "analytics", "report", "panel"),
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "selected-hwm-calendar-authority",
        "source_paths": (
            _HWM_SELECTED,
            _HWM_CALENDAR,
            _HWM_GOVERNED,
            _PREPARATION,
            "contracts/portfolio-hwm-policy-v4.json",
        ),
        "contracts": ("contracts/portfolio-hwm-policy-v4.json",),
        "producer": (_HWM_SELECTED, "derive_selected_hwm_calendar_authority"),
        "artifacts": ("hwm-calendar-authority.json",),
        "consumers": (
            (_HWM_SELECTED, "build_selected_hwm_inputs"),
            (_HWM, "compute_governed_as_held_composite_hwm"),
        ),
        "validators": ((_HWM_GOVERNED, "validate_governed_hwm_calendar_authority"),),
        "writers": ((_PREPARATION, "prepare_attended_inputs"),),
        "stages": ("report", "panel"),
        "tests": (
            "tests/core/test_attended_preparation.py",
            "tests/portfolio_ledger/test_hwm_selected_inputs.py",
        ),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "governed-hwm-result",
        "source_paths": (
            _HWM,
            _PREPARATION,
            "tools/portfolio_hwm_selected_inputs.py",
            "contracts/portfolio-hwm-policy-v4.json",
            "contracts/portfolio-performance-policy-v1.json",
        ),
        "contracts": (
            "contracts/portfolio-hwm-policy-v4.json",
            "contracts/portfolio-performance-policy-v1.json",
        ),
        "producer": (_HWM, "compute_governed_as_held_composite_hwm"),
        "artifacts": ("hwm-governed-result.json", "hwm-input-generation.json"),
        "consumers": ((_RELEASE, "build_report"), (_PANEL, "build_candidate")),
        "validators": ((_HWM, "verify_governed_hwm_result"),),
        "writers": ((_PREPARATION, "prepare_attended_inputs"),),
        "stages": ("report", "panel"),
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "governed-invested-capital-result",
        "source_paths": (_CAPITAL_INPUT, _CAPITAL, _PREPARATION),
        "contracts": ("contracts/portfolio-financial-mvp-source-v1.json",),
        "producer": (_CAPITAL, "build_governed_capital_result"),
        "artifacts": (
            "capital-input-generation.json",
            # Locators for the portable generation above. Never hashed,
            # published, or copied into the report candidate.
            "capital-input-generation.local.json",
            "invested-capital-result.json",
        ),
        "consumers": ((_RELEASE, "build_report"), (_PANEL, "build_candidate")),
        "validators": ((_CAPITAL, "verify_governed_capital_result"),),
        "writers": ((_CAPITAL_INPUT, "build_input_generation"),),
        "stages": ("report", "panel"),
        "tests": ("tests/core/test_attended_preparation.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "attribution-catalog-generation",
        "source_paths": (_ATTRIBUTION, _RELEASE),
        "contracts": ("contracts/portfolio-performance-policy-v1.json",),
        "producer": (_ATTRIBUTION, "stage_catalog"),
        "artifacts": ("strategy-attribution generation",),
        "consumers": ((_RELEASE, "build_attribution"),),
        "validators": ((_ATTRIBUTION, "stage_catalog"),),
        "writers": ((_ATTRIBUTION, "publish_catalog"),),
        "stages": ("attribution", "analytics", "report", "panel"),
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "decomposition-generation",
        "source_paths": (_DECOMPOSITION, _RELEASE),
        "contracts": ("contracts/portfolio-performance-policy-v1.json",),
        "producer": (_DECOMPOSITION, "stage_candidate"),
        "artifacts": ("decomposition generation",),
        "consumers": ((_RELEASE, "build_analytics"), (_PANEL, "build_candidate")),
        "validators": ((_DECOMPOSITION, "stage_candidate"),),
        "writers": ((_DECOMPOSITION, "publish"),),
        "stages": ("analytics", "report", "panel"),
        "tests": ("tests/core/test_portfolio_release_builder.py",),
        "sinks": ("report-json-html", "panel-json-html"),
    },
    {
        "id": "report-candidate",
        "source_paths": (
            _REPORT,
            _PANEL,
            "schemas/portfolio/report-data.schema.json",
            "schemas/portfolio/control-panel-data.schema.json",
        ),
        "contracts": (
            "schemas/portfolio/report-data.schema.json",
            "schemas/portfolio/control-panel-data.schema.json",
        ),
        "producer": (_REPORT, "build_candidate"),
        "artifacts": (
            "portfolio-report-data.json",
            "report HTML",
        ),
        "consumers": (
            (_REPORT, "verify_candidate"),
            (_PANEL, "build_candidate"),
        ),
        "validators": ((_REPORT, "verify_candidate"),),
        "writers": ((_REPORT, "publish_report"),),
        "stages": ("report", "panel"),
        "tests": ("tests/portfolio_report/test_portfolio_report.py",),
        "sinks": ("report-json-html",),
    },
    {
        "id": "panel-candidate",
        "source_paths": (
            _PANEL,
            "schemas/portfolio/control-panel-data.schema.json",
        ),
        "contracts": ("schemas/portfolio/control-panel-data.schema.json",),
        "producer": (_PANEL, "build_candidate"),
        "artifacts": ("panel JSON", "panel HTML"),
        "consumers": ((_PANEL, "verify_candidate"),),
        "validators": ((_PANEL, "verify_candidate"),),
        "writers": ((_PANEL, "publish_panel"),),
        "stages": ("panel",),
        "tests": ("tests/portfolio_control_panel/test_mvp2_candidate_report_panel.py",),
        "sinks": ("panel-json-html",),
    },
    {
        "id": "attended-qa-input-binding",
        "source_paths": (
            _MATERIALIZER,
            "apps/portfolio-control-panel/browser_qa.mjs",
            "apps/portfolio-control-panel/interaction_qa.mjs",
        ),
        "contracts": ("schemas/portfolio/control-panel-data.schema.json",),
        "producer": (_MATERIALIZER, "_attended_bind_qa"),
        "artifacts": ("attended-qa.json",),
        "consumers": ((_MATERIALIZER, "_attended_start_or_resume"),),
        "validators": ((_MATERIALIZER, "_validate_directory_definition"),),
        "writers": ((_MATERIALIZER, "_attended_bind_qa"),),
        "stages": ("panel",),
        "tests": ("tests/core/test_portfolio_pipeline_materialize.py",),
        "sinks": ("panel-json-html",),
    },
    {
        "id": "panel-qa-validation-evidence",
        "source_paths": (
            _RELEASE,
            _PANEL,
            _MATERIALIZER,
            "packages/core/src/quant_engine/core/publication.py",
        ),
        "contracts": ("schemas/portfolio/control-panel-data.schema.json",),
        "producer": (_RELEASE, "publish_panel"),
        "artifacts": (
            "producer-owned QA copy",
            "evidence/semantic-validation.json",
            "evidence/qa-validation.json",
        ),
        "consumers": (
            (_PANEL, "publish_panel"),
            (_MATERIALIZER, "_attended_completed_panel"),
        ),
        "validators": (
            (_PANEL, "validate_qa"),
            (_MATERIALIZER, "_attended_completed_panel"),
        ),
        "writers": ((_RELEASE, "_stage_panel_qa"), (_PANEL, "publish_panel")),
        "stages": ("panel",),
        "tests": (
            "tests/core/test_portfolio_release_builder.py",
            "tests/core/test_portfolio_pipeline_materialize.py",
        ),
        "sinks": ("panel-json-html",),
    },
    {
        "id": "coordinated-publication-pointers",
        "source_paths": (_CORE, _MATERIALIZER, _RELEASE, _REPORT, _PANEL),
        "contracts": ("contracts/portfolio-financial-mvp-source-v1.json",),
        "producer": (_CORE, "execute_pipeline"),
        "artifacts": (
            "coordinated report and panel generations",
            "CURRENT pointers",
        ),
        "consumers": ((_CORE, "execute_pipeline"),),
        "validators": ((_CORE, "_validate_commit_authorization"),),
        "writers": ((_REPORT, "publish_report"), (_PANEL, "publish_panel")),
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


def _required_paths(
    repo_root: Path, reader: _CurrentGraphSourceReader | None = None
) -> list[str]:
    paths = [
        "tools/portfolio_controller_graph.py",
        _CORE,
        _MATERIALIZER,
        _RELEASE,
        _CORRELATION,
        _REPORT,
        _PANEL,
        _SOURCE_CONTRACT,
        _SOURCE_SCHEMA,
    ]
    if (repo_root / _PREPARATION).is_file():
        paths.append(_PREPARATION)
        bindings = _preparation_stage_bindings(
            reader or _CurrentGraphSourceReader(repo_root)
        )
        for binding_kind in ("code", "policy"):
            stage_bindings = bindings[binding_kind]
            if not isinstance(stage_bindings, Mapping):
                raise ControllerGraphError("preparation stage bindings differ")
            for bound_paths in stage_bindings.values():
                if not isinstance(bound_paths, list):
                    raise ControllerGraphError("preparation stage bindings differ")
                paths.extend(bound_paths)
    paths.extend(path for path in _TEST_LINKS.values() if (repo_root / path).is_file())
    for flow in _ARTIFACT_FLOW_SPECS:
        paths.extend(cast(Sequence[str], flow["source_paths"]))
        paths.extend(cast(Sequence[str], flow["contracts"]))
        paths.extend(cast(Sequence[str], flow["tests"]))
    return sorted(set(paths))


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


class _CurrentGraphSourceReader:
    """Read and parse one current graph's source without repeated AST parsing."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._trees: dict[tuple[str, bytes], ast.Module] = {}

    def tree(self, relative_path: str) -> ast.Module:
        path = self.repo_root / relative_path
        if not path.is_file():
            raise ControllerGraphError(
                f"required controller source is absent: {relative_path}"
            )
        try:
            source = path.read_bytes()
            digest = hashlib.sha256(source).digest()
            key = (relative_path, digest)
            cached = self._trees.get(key)
            if cached is not None:
                return cached
            text = io.TextIOWrapper(
                io.BytesIO(source), encoding="utf-8", newline=None
            ).read()
            tree = ast.parse(text, filename=relative_path)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise ControllerGraphError(
                f"cannot parse controller source: {relative_path}"
            ) from exc
        self._trees[key] = tree
        return tree


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


def _endpoint_function(
    tree: ast.Module, symbol: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Resolve a public function or a named public class method."""

    if "." not in symbol:
        return _top_level_function(tree, symbol)
    class_name, method_name = symbol.split(".", 1)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == method_name
                ):
                    return member
    raise ControllerGraphError(f"required controller symbol is absent: {symbol}")


def _require_attribute_call(
    reader: _CurrentGraphSourceReader,
    relative_path: str,
    function_name: str,
    attribute: str,
    relationship: str,
) -> None:
    function = _top_level_function(reader.tree(relative_path), function_name)
    if not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        for node in ast.walk(function)
    ):
        raise ControllerGraphError(f"required {relationship} is absent")


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
    class CallCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: set[str] = set()
            self.aliases: dict[str, str] = {}

        @staticmethod
        def _bound_callable(value: ast.expr) -> str | None:
            if (
                isinstance(value, ast.Call)
                and _call_name(value) == "cast"
                and len(value.args) >= 2
            ):
                value = value.args[1]
            if isinstance(value, ast.Attribute):
                return _call_name(ast.Call(func=value, args=[], keywords=[]))
            return None

        def visit_Assign(self, node: ast.Assign) -> None:
            bound = self._bound_callable(node.value)
            if bound is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.aliases[target.id] = bound
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                bound = self._bound_callable(node.value)
                if bound is not None and isinstance(node.target, ast.Name):
                    self.aliases[node.target.id] = bound
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            call_name = _call_name(node)
            if call_name is not None:
                self.calls.add(self.aliases.get(call_name, call_name))
            self.generic_visit(node)

    collector = CallCollector()
    collector.visit(function)
    return collector.calls


def _local_call_closure(tree: ast.Module, entrypoint: str) -> set[str]:
    """Follow only same-module helpers reachable from one public entrypoint."""

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    pending = [entrypoint]
    visited: set[str] = set()
    observed: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        function = functions.get(name)
        if function is None:
            raise ControllerGraphError(
                f"required controller local route is absent: {entrypoint}:{name}"
            )
        calls = _function_calls(function)
        observed.update(calls)
        pending.extend(
            call for call in calls if call in functions and call not in visited
        )
    return observed


def _require_local_route_calls(
    tree: ast.Module, entrypoint: str, calls: Iterable[str], relationship: str
) -> None:
    missing = sorted(set(calls) - _local_call_closure(tree, entrypoint))
    if missing:
        raise ControllerGraphError(f"required {relationship} is absent")


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
    reader: _CurrentGraphSourceReader,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    core = reader.tree(_CORE)
    materializer = reader.tree(_MATERIALIZER)
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
    reader: _CurrentGraphSourceReader,
    relative_path: str,
    function_name: str,
    calls: Iterable[str],
) -> None:
    observed = _function_calls(
        _top_level_function(reader.tree(relative_path), function_name)
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
    reader: _CurrentGraphSourceReader,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    """Record the mandatory executable predecessor declaration."""

    preparation = reader.tree(_PREPARATION)
    declared = _literal_assignment(preparation, "PREPARATION_STAGE_DEPENDENCIES")
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


def _preparation_stage_bindings(reader: _CurrentGraphSourceReader) -> dict[str, object]:
    """Project the same runtime declarations that preparation hashes on reuse."""

    repo_root = reader.repo_root
    preparation = reader.tree(_PREPARATION)
    dependencies = _literal_assignment(preparation, "PREPARATION_STAGE_DEPENDENCIES")
    code = _literal_assignment(preparation, "_PREPARATION_STAGE_CODE")
    policy = _literal_assignment(preparation, "_PREPARATION_STAGE_POLICY")
    outputs = _literal_assignment(preparation, "_PREPARATION_STAGE_OUTPUTS")
    runtime_paths = _literal_assignment(preparation, "PREPARATION_RUNTIME_PATHS")
    if (
        not isinstance(dependencies, dict)
        or not isinstance(code, dict)
        or not isinstance(policy, dict)
        or not isinstance(outputs, dict)
    ):
        raise ControllerGraphError("preparation stage declarations differ")
    declarations = (dependencies, code, policy, outputs)
    if not isinstance(runtime_paths, dict):
        raise ControllerGraphError("preparation runtime path registry differs")
    stage_names = set(dependencies)
    if not stage_names or any(
        set(declaration) != stage_names for declaration in declarations
    ):
        raise ControllerGraphError("preparation stage declarations differ")

    def paths_for(alias: object) -> list[str]:
        if not isinstance(alias, str) or alias not in runtime_paths:
            raise ControllerGraphError("preparation runtime alias is unresolved")
        relative_path = runtime_paths[alias]
        if not isinstance(relative_path, str):
            raise ControllerGraphError("preparation runtime path registry differs")
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ControllerGraphError("preparation runtime path registry differs")
        if not (repo_root / candidate).is_file():
            raise ControllerGraphError("preparation runtime path is absent")
        if alias != "layered_history_repository_dependencies":
            return [candidate.as_posix()]
        target = reader.tree(candidate.as_posix())
        declared_paths = _literal_assignment(
            target, "LAYERED_HISTORY_REPOSITORY_DEPENDENCIES"
        )
        if not isinstance(declared_paths, (list, tuple)) or not declared_paths:
            raise ControllerGraphError("layered history runtime dependencies differ")
        resolved: list[str] = []
        for declared_path in declared_paths:
            if not isinstance(declared_path, str):
                raise ControllerGraphError(
                    "layered history runtime dependencies differ"
                )
            dependency = Path(declared_path)
            if (
                dependency.is_absolute()
                or ".." in dependency.parts
                or not (repo_root / dependency).is_file()
            ):
                raise ControllerGraphError(
                    "layered history runtime dependency is absent"
                )
            resolved.append(dependency.as_posix())
        return resolved

    resolved_relative_paths = {
        alias: paths_for(alias) for alias in sorted(runtime_paths)
    }
    code_bindings: dict[str, list[str]] = {}
    policy_bindings: dict[str, list[str]] = {}
    output_bindings: dict[str, list[str]] = {}
    for stage in sorted(stage_names):
        predecessors = dependencies[stage]
        stage_code = code[stage]
        stage_policy = policy[stage]
        stage_outputs = outputs[stage]
        if not all(
            isinstance(value, (list, tuple))
            for value in (predecessors, stage_code, stage_policy, stage_outputs)
        ):
            raise ControllerGraphError("preparation stage declarations differ")
        if any(
            not isinstance(predecessor, str) or predecessor not in stage_names
            for predecessor in predecessors
        ):
            raise ControllerGraphError("preparation stage predecessor differs")
        if not all(isinstance(output, str) and output for output in stage_outputs):
            raise ControllerGraphError("preparation stage output declaration differs")
        code_bindings[stage] = list(
            dict.fromkeys(path for alias in stage_code for path in paths_for(alias))
        )
        policy_bindings[stage] = list(
            dict.fromkeys(path for alias in stage_policy for path in paths_for(alias))
        )
        output_bindings[stage] = list(stage_outputs)
    return {
        "code": code_bindings,
        "outputs": output_bindings,
        "policy": policy_bindings,
        "resolved_relative_paths": resolved_relative_paths,
        "status": "verified",
    }


def _critical_relationships(
    reader: _CurrentGraphSourceReader,
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    repo_root = reader.repo_root
    materializer = reader.tree(_MATERIALIZER)
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
    preparation_tree = reader.tree(_PREPARATION)
    _top_level_function(preparation_tree, "plan_attended_inputs")
    _top_level_function(preparation_tree, "prepare_attended_inputs")
    _require_local_route_calls(
        preparation_tree,
        "_validated_inputs",
        {"provider_ledger.verify_candidate"},
        "upfront provider replay",
    )
    _require_local_route_calls(
        preparation_tree,
        "_validated_inputs",
        {"_retained_public_factor_capture"},
        "retained public factor capture preflight route",
    )
    _require_local_route_calls(
        preparation_tree,
        "prepare_attended_inputs",
        {
            "_rebuild_layered_history",
            "dependence.write_source",
            "dependence.verify_packaged_source",
            "plans.write_bundle",
            "release.build_ledger",
            "release.build_baseline",
            "stage_current_epoch_manifest",
            "hwm_inputs.derive_selected_hwm_calendar_authority",
            "capital.build_governed_capital_result",
            "capital.verify_governed_capital_result",
            "release.build_attribution",
        },
        "attended preparation producer route",
    )
    _require_local_route_calls(
        reader.tree(_HWM_SELECTED),
        "derive_selected_hwm_calendar_authority",
        {
            "_selected_hwm_source_rows",
            "derive_governed_calendar_authority",
            "validate_governed_hwm_calendar_authority",
        },
        "selected-source calendar derivation route",
    )
    _require_local_route_calls(
        preparation_tree,
        "prepare_attended_inputs",
        {"compute_governed_as_held_composite_hwm"},
        "governed HWM producer invocation",
    )
    _require_local_route_calls(
        preparation_tree,
        "prepare_attended_inputs",
        {"verify_governed_hwm_result"},
        "governed HWM verifier invocation",
    )
    _require_calls(
        reader,
        _PREPARATION,
        "_rebuild_layered_history",
        {
            "history.build_layered_history_generation",
            "history.write_layered_history_generation",
            "history.verify_layered_history_generation",
        },
    )
    _require_attribute_call(
        reader,
        _ACCOUNT_MANIFEST,
        "stage_account_manifest_generation",
        "verify",
        "independent account-manifest verifier invocation",
    )
    _require_local_route_calls(
        preparation_tree,
        "prepare_attended_inputs",
        {"plans.build_bundle"},
        "attended decomposition-plan producer route",
    )
    _require_local_route_calls(
        preparation_tree,
        "prepare_attended_inputs",
        {"_retained_public_factor_capture"},
        "retained public factor capture production route",
    )
    _require_calls(reader, _DEPENDENCE, "write_source", {"build_source"})
    _require_calls(reader, _DECOMPOSITION_PLAN, "write_bundle", {"build_bundle"})
    _require_calls(
        reader,
        _EPOCH_MANIFEST,
        "stage_current_epoch_manifest",
        {"account_manifest_generation.stage_account_manifest_generation"},
    )
    _require_calls(
        reader,
        _CAPITAL_INPUT,
        "main",
        {
            "build_input_generation",
            "capital.build_governed_capital_result",
            "capital.verify_governed_capital_result",
        },
    )
    _require_calls(
        reader, _PROVIDER_LEDGER, "main", {"stage_candidate", "verify_candidate"}
    )
    _require_calls(reader, _BASELINE, "main", {"stage_baseline"})
    _require_calls(reader, _ATTRIBUTION, "publish_catalog", {"stage_catalog"})
    preparation_dependencies, preparation_edges = _preparation_stage_dependencies(
        reader
    )
    preparation_bindings = _preparation_stage_bindings(reader)
    _require_calls(reader, _MATERIALIZER, "_attended_stage_command", {"str"})
    command_source = (repo_root / _MATERIALIZER).read_text(encoding="utf-8")
    if "portfolio_release_builder.py" not in command_source:
        raise ControllerGraphError("materializer release-producer command is absent")
    _require_calls(reader, _RELEASE, "main", {"build_stage"})
    _require_calls(reader, _RELEASE, "publish_panel", {"_stage_panel_qa"})
    _require_calls(
        reader, _MATERIALIZER, "_attended_start_or_resume", {"_attended_bind_qa"}
    )
    _require_calls(
        reader,
        _MATERIALIZER,
        "_attended_completed_stage_root",
        {"_attended_completed_panel"},
    )
    _require_calls(
        reader,
        _MATERIALIZER,
        "_attended_completed_panel",
        {"panel._current_panel_qa"},
    )
    _require_calls(
        reader,
        _MATERIALIZER,
        "_attended_completed_report",
        {"panel._current_report"},
    )
    release_tree = reader.tree(_RELEASE)
    _require_local_route_calls(
        release_tree,
        "_verify_current_plan_upstream_evidence",
        {"_retained_public_factor_capture"},
        "retained public factor capture release evidence route",
    )
    _require_local_route_calls(
        release_tree,
        "_expected_non_spec_data_age",
        {"_retained_public_factor_capture"},
        "retained public factor capture release age route",
    )
    release_stage_calls = _function_calls(
        _top_level_function(release_tree, "build_stage")
    )
    for handler in _HANDLERS.values():
        if handler.rsplit(".", 1)[1] not in release_stage_calls:
            raise ControllerGraphError(f"release producer handler is absent: {handler}")
    report_builder = _top_level_function(release_tree, "build_report")
    ledger_builder = _top_level_function(release_tree, "build_ledger")
    baseline_builder = _top_level_function(release_tree, "build_baseline")
    panel_builder = _top_level_function(release_tree, "build_panel_candidate")
    report_candidate_builder = _top_level_function(
        release_tree, "build_report_candidate"
    )
    if "build_report_candidate" not in _function_calls(report_builder):
        raise ControllerGraphError("release report candidate call is absent")
    _require_tool_call(
        ledger_builder,
        "provider_ledger.py",
        "stage",
        "provider-ledger stage CLI target/subcommand",
    )
    _require_tool_call(
        ledger_builder,
        "provider_ledger.py",
        "verify",
        "provider-ledger verify CLI target/subcommand",
    )
    _require_tool_call(
        ledger_builder,
        "portfolio_freshness.py",
        "--provider-ledger-root",
        "freshness producer CLI target",
    )
    _require_calls(reader, _FRESHNESS, "main", {"write_health"})
    _require_calls(reader, _FRESHNESS, "write_health", {"build_health"})
    _require_tool_call(
        baseline_builder,
        "portfolio_baseline.py",
        "stage",
        "baseline stage CLI target/subcommand",
    )
    _require_tool_call(
        baseline_builder,
        "portfolio_baseline.py",
        "publish",
        "baseline publish CLI target/subcommand",
    )
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
    report_tree = reader.tree(_REPORT)
    panel_tree = reader.tree(_PANEL)
    _top_level_function(report_tree, "build_candidate")
    _top_level_function(report_tree, "verify_candidate")
    _top_level_function(panel_tree, "build_candidate")
    _top_level_function(panel_tree, "verify_candidate")
    _require_local_route_calls(
        report_tree,
        "build_candidate",
        {"_freshness_assessments"},
        "report freshness consumer route",
    )
    _require_local_route_calls(
        panel_tree,
        "build_candidate",
        {"_freshness_health"},
        "panel freshness consumer route",
    )
    core_tree = reader.tree(_CORE)
    _require_local_route_calls(
        core_tree,
        "execute_pipeline",
        {"_validate_commit_authorization"},
        "owner publication authorization invocation",
    )
    _require_local_route_calls(
        core_tree,
        "execute_pipeline",
        {"commit_release"},
        "coordinated publication sink invocation",
    )
    relationships: list[dict[str, object]] = [
        {
            "caller": "tools.portfolio_attended_preparation._validated_inputs",
            "callee": "tools.provider_ledger.verify_candidate",
            "kind": "upfront-provider-replay",
        },
        {
            "caller": "tools.portfolio_attended_preparation._validated_inputs",
            "callee": "tools.portfolio_attended_preparation._retained_public_factor_capture",
            "kind": "retained-public-factor-preflight",
        },
        {
            "caller": "tools.portfolio_attended_preparation.prepare_attended_inputs",
            "callee": "tools.portfolio_attended_preparation._retained_public_factor_capture",
            "kind": "retained-public-factor-production",
        },
        {
            "caller": "tools.portfolio_release_builder._verify_current_plan_upstream_evidence",
            "callee": "tools.portfolio_release_builder._retained_public_factor_capture",
            "kind": "retained-public-factor-release-evidence",
        },
        {
            "caller": "tools.portfolio_release_builder._expected_non_spec_data_age",
            "callee": "tools.portfolio_release_builder._retained_public_factor_capture",
            "kind": "retained-public-factor-release-age",
        },
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
            "caller": "tools.portfolio_release_builder.build_ledger",
            "callee": "tools/portfolio_freshness.py --provider-ledger-root",
            "kind": "freshness-producer-cli",
        },
        {
            "caller": "tools.portfolio_freshness.main",
            "callee": "tools.portfolio_freshness.write_health",
            "kind": "freshness-write-command",
        },
        {
            "caller": "tools.portfolio_freshness.write_health",
            "callee": "tools.portfolio_freshness.build_health",
            "kind": "freshness-health-producer",
        },
        {
            "caller": "tools.portfolio_report.build_candidate",
            "callee": "tools.portfolio_report.build_report_data",
            "kind": "report-freshness-build-route",
        },
        {
            "caller": "tools.portfolio_report.build_report_data",
            "callee": "tools.portfolio_report._freshness_assessments",
            "kind": "report-freshness-consumer",
        },
        {
            "caller": "tools.portfolio_control_panel.build_candidate",
            "callee": "tools.portfolio_control_panel._rendered_files",
            "kind": "panel-freshness-build-route",
        },
        {
            "caller": "tools.portfolio_control_panel._rendered_files",
            "callee": "tools.portfolio_control_panel._freshness_health",
            "kind": "panel-freshness-consumer",
        },
        {
            "caller": "tools.portfolio_attended_preparation.prepare_attended_inputs",
            "callee": "quant_engine.portfolio_ledger.hwm_governed_execution.compute_governed_as_held_composite_hwm",
            "kind": "governed-hwm-producer",
        },
        {
            "caller": "tools.portfolio_attended_preparation.prepare_attended_inputs",
            "callee": "tools.portfolio_hwm_selected_inputs.derive_selected_hwm_calendar_authority",
            "kind": "selected-source-calendar-producer",
        },
        {
            "caller": "tools.portfolio_attended_preparation.prepare_attended_inputs",
            "callee": "quant_engine.portfolio_ledger.hwm_governed_execution.verify_governed_hwm_result",
            "kind": "governed-hwm-verifier",
        },
        {
            "caller": "packages.core.quant_engine.core.pipeline.execute_pipeline",
            "callee": "packages.core.quant_engine.core.pipeline._validate_commit_authorization",
            "kind": "owner-publication-authorization",
        },
        {
            "caller": "packages.core.quant_engine.core.pipeline.execute_pipeline",
            "callee": "quant_engine.release.protocol.commit_release",
            "kind": "coordinated-publication-sink",
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
        {
            "caller": "tools.portfolio_release_builder.publish_panel",
            "callee": "tools.portfolio_release_builder._stage_panel_qa",
            "kind": "producer-owned-qa-copy",
        },
        {
            "caller": "tools.portfolio_pipeline_materialize._attended_start_or_resume",
            "callee": "tools.portfolio_pipeline_materialize._attended_bind_qa",
            "kind": "immutable-qa-input-binding",
        },
        {
            "caller": "tools.portfolio_pipeline_materialize._attended_completed_panel",
            "callee": "tools.portfolio_control_panel._current_panel_qa",
            "kind": "retained-panel-qa-verification",
        },
        {
            "caller": "tools.portfolio_pipeline_materialize._attended_completed_report",
            "callee": "tools.portfolio_control_panel._current_report",
            "kind": "retained-report-data-verification",
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
    return relationships, preparation_dependencies, preparation_bindings


def _artifact_flows(reader: _CurrentGraphSourceReader) -> list[dict[str, object]]:
    """Bind the MVP's declared artifacts to real producers, consumers and checks.

    This projection intentionally remains smaller than the mapper's full static
    graph.  It records only the supported financial-MVP route, and leaves
    runtime source/finality, recovery and publication authorization with the
    native runner and existing owner gate.
    """

    repo_root = reader.repo_root
    flows: list[dict[str, object]] = []
    for spec in _ARTIFACT_FLOW_SPECS:
        flow = dict(spec)
        source_paths = cast(tuple[str, ...], flow["source_paths"])
        producer = cast(tuple[str, str], flow["producer"])
        consumers = cast(tuple[tuple[str, str], ...], flow["consumers"])
        validators = cast(tuple[tuple[str, str], ...], flow["validators"])
        writers = cast(tuple[tuple[str, str], ...], flow["writers"])
        contracts = cast(tuple[str, ...], flow["contracts"])
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
        if not contracts or not all(
            isinstance(path, str) and (repo_root / path).is_file() for path in contracts
        ):
            raise ControllerGraphError("artifact-flow contract is absent")
        endpoints = (producer, *consumers, *validators, *writers)
        for endpoint in endpoints:
            if not isinstance(endpoint, tuple) or len(endpoint) != 2:
                raise ControllerGraphError("artifact-flow endpoint differs")
            path, symbol = endpoint
            if not isinstance(path, str) or not isinstance(symbol, str):
                raise ControllerGraphError("artifact-flow endpoint differs")
            _endpoint_function(reader.tree(path), symbol)
        if not artifacts or not consumers or not validators or not writers or not sinks:
            raise ControllerGraphError("artifact-flow consumer or validator is absent")
        if not stages or not all(stage in _STAGE_ORDER for stage in stages):
            raise ControllerGraphError("artifact-flow stages differ")
        if not tests or not all((repo_root / path).is_file() for path in tests):
            raise ControllerGraphError("artifact-flow tests are absent")
        flow["source_paths"] = sorted(source_paths)
        flow["contracts"] = sorted(contracts)
        flow["artifacts"] = list(artifacts)
        flow["output_inventory"] = list(artifacts)
        flow["consumers"] = [list(endpoint) for endpoint in consumers]
        flow["producer"] = list(producer)
        flow["stages"] = list(stages)
        flow["sinks"] = list(sinks)
        flow["tests"] = list(tests)
        flow["validators"] = [list(endpoint) for endpoint in validators]
        flow["writers"] = [list(endpoint) for endpoint in writers]
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
    reader = _CurrentGraphSourceReader(repo_root)
    dependencies, materializer_dependencies = _stage_declarations(reader)
    relationships, preparation_dependencies, preparation_bindings = (
        _critical_relationships(reader)
    )
    artifact_flows = _artifact_flows(reader)
    preparation_paths: set[str] = set()
    for binding_kind in ("code", "policy"):
        stage_bindings = preparation_bindings[binding_kind]
        assert isinstance(stage_bindings, Mapping)
        for paths in stage_bindings.values():
            assert isinstance(paths, list)
            preparation_paths.update(path for path in paths if isinstance(path, str))
    for flow in artifact_flows:
        if flow["id"] == "attended-preparation-checkpoints":
            source_paths = flow["source_paths"]
            assert isinstance(source_paths, list)
            flow["source_paths"] = sorted(set(source_paths) | preparation_paths)
    source_files = {
        path: _sha256(repo_root / path) for path in _required_paths(repo_root, reader)
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
            "preparation_stage_bindings": preparation_bindings,
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
            # Artifact source inventory binds every byte used by a flow, but a
            # path with explicit ownership must retain that causal boundary.
            # In particular, a downstream consumer listed by a report artifact
            # must not acquire upstream report impact from that inventory.
            if path not in path_stages:
                path_stages[path] = {
                    stage for stage in flow_stages if isinstance(stage, str)
                }
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

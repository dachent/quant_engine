"""Actual report and panel consumers for the synthetic MVP-2 vertical.

The helpers in this module configure fixture-owned source authorities only.
Financial values still come from the repository's production producers and
consumer verification code.
"""

from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
from importlib import import_module
import json
from pathlib import Path
from typing import Any, Mapping

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


def _write_exact(path: Path, value: object) -> Path:
    content = value if isinstance(value, bytes) else canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        assert not path.is_symlink() and path.read_bytes() == content
    else:
        path.write_bytes(content)
    return path


def _generation_selection(manifest_path: Path) -> tuple[Path, Path, str]:
    """Return (root, generation directory, id) for a selected manifest path."""

    assert manifest_path.name == "generation.json"
    generation = manifest_path.parent
    assert generation.parent.name == "generations"
    return generation.parents[1], generation, generation.name


def _stage_epoch_inputs(
    root: Path, selected_paths: Mapping[str, Path]
) -> dict[str, Path]:
    """Expose the account producer's exact calculation authorities once."""

    _generation_root, generation, _generation_id = _generation_selection(
        selected_paths["generation"]
    )
    calculation = json.loads(
        (generation / "authority/calculation-inputs.json").read_bytes()
    )
    names = {
        "account_strategy_ledger": "calculation_ledger",
        "account_strategy_ledger_identity": "calculation_ledger_identity",
        "account_strategy_classified_flows": "calculation_classified_flows",
        "account_strategy_flow_coverage": "calculation_flow_coverage",
    }
    paths = {
        key: _write_exact(root / f"{key}.json", calculation[source_key])
        for key, source_key in names.items()
    }
    paths["account_strategy_admission_policy"] = selected_paths["admission_policy"]
    return paths


def _stage_epoch_triplet(
    root: Path, generation_root: Path, generation_id: str
) -> tuple[Path, Path, Path]:
    """Use the existing bundle verifier and source binder for report inputs."""

    import portfolio_release_builder as release_builder

    manifest, source, crosswalk, _identity, _artifact = (
        release_builder._stage_verified_epoch_manifest_triplet_bundle(
            generation_root, generation_id, root
        )
    )
    return manifest, source, crosswalk


def _build_freshness(
    root: Path, selected_paths: Mapping[str, Path], as_of_date: str
) -> Path:
    """Run the actual provider-ledger freshness producer."""

    import portfolio_freshness

    provider_root, _generation, provider_id = _generation_selection(
        selected_paths["provider_generation"]
    )
    output = root / "freshness-health.json"
    portfolio_freshness.write_health(
        provider_root,
        provider_id,
        output,
        evaluation_date=date.fromisoformat(as_of_date),
    )
    return output


def _build_primary_decomposition(root: Path, decomposition: Any) -> Path:
    """Run the existing model kernel for the panel's legacy primary lens."""

    import portfolio_decomposition

    bundle = decomposition.plan_bundles["LIVE_ONLY"]
    manifest = json.loads((bundle / "plan-manifest.json").read_bytes())
    selected = next(
        row
        for row in manifest["specs"]
        if row["source_key"] == "us-economic" and row["surface"] == "STATIC"
    )
    spec = json.loads((bundle / selected["path"]).read_bytes())
    # The panel's primary slot predates the explicit multi-lens surface. Its
    # accepted marker is presentation metadata; every model/input stays exact.
    spec["analysis_lens"] = "HISTORICAL_MINIMUM_PROOF"
    spec_path = _write_exact(root / "primary-decomposition-spec.json", spec)
    output_root = root / "primary-decomposition"
    portfolio_decomposition.stage_candidate(spec_path, bundle, output_root)
    portfolio_decomposition.publish(spec_path, bundle, output_root)
    return output_root


def _stage_private_candidate_generation(
    root: Path,
    candidate: Path,
    relative_paths: tuple[str, ...],
    *,
    as_of_date: str,
    role: str,
) -> Path:
    """Snapshot actual candidate bytes without asserting native or browser QA."""

    observed = date.fromisoformat(as_of_date)
    payloads = {
        relative: (candidate / relative).read_bytes() for relative in relative_paths
    }
    pending_id = "verification/independent-verification.json"
    payloads[pending_id] = canonical_json_bytes(
        {
            "browser_qa": "NOT_PERFORMED",
            "boundary": "SYNTHETIC_NUMERICAL_CONSUMER_PROOF_ONLY",
            "native_excel_recalculation": "NOT_PERFORMED",
            "status": "PENDING",
            "verification_schema_version": 1,
        }
    )
    artifacts = tuple(
        SourceArtifact(
            artifact_id=relative,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            as_of_date=observed,
            schema_name=f"portfolio-{role}-private-candidate-artifact",
            schema_version="1",
        )
        for relative, content in payloads.items()
    )
    manifest = GenerationManifest.build(
        as_of_date=observed,
        adapter=AdapterIdentity(f"portfolio-{role}-private-candidate", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                "private synthetic candidate output bytes only; native Excel "
                "recalculation and browser QA are outside this generation's coverage"
            ),
            fact_types=(f"portfolio-{role}-candidate-bytes",),
            observed_start=observed,
            observed_end=observed,
            known_gaps=(),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.PENDING,
    )
    return stage_generation_bundle(root, manifest, payloads)


def _install_dynamic_dependence_loader(
    monkeypatch: Any, reporter: Any, configured_current: Any
) -> None:
    """Give the report's fresh verifier module the configured real authority loader."""

    original = importlib.util.spec_from_file_location

    def configured_spec(name: str, location: object, *args: Any, **kwargs: Any) -> Any:
        spec = original(name, location, *args, **kwargs)
        if name != "portfolio_current_dependence_for_portfolio_report":
            return spec
        assert spec is not None and spec.loader is not None
        loader = spec.loader
        execute = loader.exec_module

        def execute_configured(module: Any) -> None:
            execute(module)
            module._financial_module = configured_current._financial_module

        loader.exec_module = execute_configured
        return spec

    monkeypatch.setattr(
        reporter.importlib.util, "spec_from_file_location", configured_spec
    )


def _synthetic_legacy_panel(root: Path, monkeypatch: Any, panel: Any) -> Path:
    """Create the frozen, non-financial appendix shell required by the panel."""

    legacy = root / "legacy-panel"
    source_content = {
        "index.html": b"synthetic MVP-2 frozen appendix\n",
        "styles.css": b":root { --accent: #5cc8c0; }\n",
        "app.js": b"window.syntheticMvp2Appendix = true;\n",
        "control_panel_validation.json": (b'{"status":"SYNTHETIC_INPUT_NOT_QA"}\n'),
    }
    for relative, content in source_content.items():
        _write_exact(legacy / relative, content)
    providers = [
        {
            "candidate": f"Synthetic candidate {index:02d}",
            "cadence": "synthetic",
            "category": "synthetic",
            "execution": "synthetic",
            "philosophy_module": "synthetic",
            "portfolio_role": "synthetic research appendix",
            "principal_gap": "synthetic fixture metadata",
            "ticker": f"S{index:02d}",
            "tier": f"Tier {(index % 3) + 1}",
        }
        for index in range(1, 34)
    ]
    dashboard = _write_exact(
        legacy / "data/dashboard_data.json", {"providers": providers}
    )
    source_hashes = {
        relative: hashlib.sha256((legacy / relative).read_bytes()).hexdigest()
        for relative in source_content
    }
    contract = {
        "contract_version": 1,
        "frozen_appendix": {
            "as_of_date": "2026-09-04",
            "candidate_count": len(providers),
            "data_file": "data/dashboard_data.json",
            "data_sha256": hashlib.sha256(dashboard.read_bytes()).hexdigest(),
            "status": "FROZEN_AUDIT_APPENDIX",
        },
        "source": "SYNTHETIC_MVP2_FIXTURE",
        "source_files": source_hashes,
    }
    contract_path = _write_exact(root / "legacy-panel-source-contract.json", contract)
    monkeypatch.setattr(panel, "SOURCE_CONTRACT", contract_path)
    return legacy


def _configure_consumer_authorities(
    monkeypatch: Any,
    reporter: Any,
    panel: Any,
    configured_current: Any,
    decomposition: Any,
    selected_paths: Mapping[str, Path],
    dependence: Any,
    crosswalk_path: Path,
) -> None:
    """Redirect only fixture-owned authority paths; retain all consumer checks."""

    crosswalk = json.loads(crosswalk_path.read_bytes())
    for module in (reporter, panel):
        monkeypatch.setattr(module, "DEPENDENCE_POLICY", dependence.policy_path)
        monkeypatch.setattr(module, "_PORTABLE_HWM_POLICY", selected_paths["policy"])
        monkeypatch.setattr(
            module, "_PORTABLE_HWM_PRODUCT_CONTRACT", decomposition.contract_path
        )
    monkeypatch.setattr(
        reporter,
        "CURRENT_EPOCH_CROSSWALK_AS_OF_DATE",
        crosswalk["as_of_date"],
    )
    monkeypatch.setattr(
        reporter,
        "CURRENT_EPOCH_CROSSWALK_CUTOFF_DATE",
        crosswalk["calculation_cutoff_date"],
    )
    monkeypatch.setattr(panel, "FINANCIAL_MVP_CONTRACT", decomposition.contract_path)
    monkeypatch.setattr(
        reporter,
        "_HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT",
        decomposition.contract_path,
    )
    monkeypatch.setattr(
        reporter,
        "FLOW_CLASSIFICATION_POLICY_CONTRACT",
        selected_paths["flow_classification_policy"],
    )
    panel_current = import_module("tools.portfolio_current_dependence")
    monkeypatch.setattr(
        panel_current, "_financial_module", configured_current._financial_module
    )
    _install_dynamic_dependence_loader(monkeypatch, reporter, configured_current)


def build_consumers_vertical(
    root: Path,
    monkeypatch: Any,
    decomposition: Any,
    selected_paths: Mapping[str, Path],
    capital: Mapping[str, Path],
    hwm: Mapping[str, Path],
    dependence: Any,
    *,
    primary_decomposition_root: Path | None = None,
) -> dict[str, Any]:
    """Build real report and panel private candidates from one source."""

    import portfolio_control_panel as panel
    import portfolio_current_dependence as current
    import portfolio_report as reporter

    root.mkdir(parents=True, exist_ok=True)
    account_root, _account_generation, account_id = _generation_selection(
        selected_paths["generation"]
    )
    baseline_root, baseline_generation, baseline_id = _generation_selection(
        selected_paths["baseline_generation"]
    )
    provider_root, _provider_generation, _provider_id = _generation_selection(
        selected_paths["provider_generation"]
    )
    epoch_inputs = _stage_epoch_inputs(root / "epoch-inputs", selected_paths)
    epoch_manifest, epoch_source, epoch_crosswalk = _stage_epoch_triplet(
        root / "epoch-triplet", account_root, account_id
    )
    baseline = json.loads(
        (baseline_generation / "portfolio-baseline.json").read_bytes()
    )
    freshness = _build_freshness(root, selected_paths, baseline["as_of_date"])
    if primary_decomposition_root is None:
        primary_decomposition_root = _build_primary_decomposition(root, decomposition)
    correlation_root = dependence.generation.parents[1]
    history_contract = dependence.source_root / "dependence-source.json"
    _configure_consumer_authorities(
        monkeypatch,
        reporter,
        panel,
        current,
        decomposition,
        selected_paths,
        dependence,
        epoch_crosswalk,
    )

    hwm_result = json.loads(hwm["governed_result"].read_bytes())
    capital_result = json.loads(capital["result"].read_bytes())
    live_plan = decomposition.plan_bundles["LIVE_ONLY"]
    report_root = root / "report"
    report_built = reporter.build_candidate(
        baseline_root,
        report_root,
        freshness_health_path=freshness,
        provider_ledger_root=provider_root,
        history_contract_path=history_contract,
        correlation_root=correlation_root,
        hwm_governed_result=hwm_result,
        hwm_governed_generation_bytes=hwm["input_generation"].read_bytes(),
        hwm_governed_calendar_bytes=hwm["calendar_authority"].read_bytes(),
        hwm_governed_receipt_bytes=hwm["provider_admission_receipt"].read_bytes(),
        epoch_manifest_path=epoch_manifest,
        epoch_manifest_source_path=epoch_source,
        epoch_crosswalk_path=epoch_crosswalk,
        epoch_manifest_generation_root=account_root,
        baseline_generation_id=baseline_id,
        correlation_generation_dir=dependence.generation,
        invested_capital_result=capital_result,
        invested_capital_input_generation_path=capital["input"],
        invested_capital_result_bytes=capital["result"].read_bytes(),
        decomposition_lenses_root=decomposition.lenses_root,
        decomposition_plan_bundle=live_plan,
        hybrid_decomposition_plan_bundle=decomposition.plan_bundles[
            "LIVE_PLUS_BACKTEST"
        ],
    )
    report_candidate = Path(report_built["candidate_dir"])

    legacy = _synthetic_legacy_panel(root, monkeypatch, panel)
    panel_root = root / "panel"
    panel_built = panel.build_candidate(
        None,
        legacy,
        panel_root,
        decomposition_root=primary_decomposition_root,
        correlation_root=correlation_root,
        decomposition_lenses_root=decomposition.lenses_root,
        decomposition_plan_bundle=live_plan,
        hybrid_decomposition_plan_bundle=decomposition.plan_bundles[
            "LIVE_PLUS_BACKTEST"
        ],
        freshness_health_path=freshness,
        baseline_root=baseline_root,
        hwm_governed_result=hwm_result,
        hwm_governed_generation_bytes=hwm["input_generation"].read_bytes(),
        hwm_governed_calendar_bytes=hwm["calendar_authority"].read_bytes(),
        hwm_governed_receipt_bytes=hwm["provider_admission_receipt"].read_bytes(),
        invested_capital_result=capital_result,
        invested_capital_input_generation_path=capital["input"],
        report_candidate=report_candidate,
        strategy_history_contract_path=history_contract,
        account_manifest_generation_root=account_root,
        account_manifest_generation_id=account_id,
    )
    panel_candidate = Path(panel_built["candidate_dir"])
    report_generation = _stage_private_candidate_generation(
        root / "report-generation",
        report_candidate,
        (
            reporter.REPORT_DATA_NAME,
            reporter.EPOCH_MANIFEST_WORKBOOK_NAME,
            reporter.EPOCH_MANIFEST_SOURCE_WORKBOOK_NAME,
            reporter.EPOCH_CROSSWALK_WORKBOOK_NAME,
            reporter.INVESTED_CAPITAL_RESULT_NAME,
            reporter.INVESTED_CAPITAL_INPUT_NAME,
        ),
        as_of_date=baseline["as_of_date"],
        role="report",
    )
    panel_generation = _stage_private_candidate_generation(
        root / "panel-generation",
        panel_candidate,
        (
            *json.loads((panel_candidate / "candidate.json").read_bytes())[
                "artifact_sha256"
            ],
            "candidate.json",
        ),
        as_of_date=baseline["as_of_date"],
        role="panel",
    )
    return {
        "report_candidate": report_candidate,
        "panel_candidate": panel_candidate,
        "report_generation": report_generation,
        "panel_generation": panel_generation,
        "report_root": report_root,
        "panel_root": panel_root,
        "freshness_health": freshness,
        "epoch_manifest": epoch_manifest,
        "epoch_manifest_source": epoch_source,
        "epoch_crosswalk": epoch_crosswalk,
        "epoch_manifest_generation_root": account_root,
        "epoch_manifest_generation_id": account_id,
        "epoch_inputs": epoch_inputs,
        "primary_decomposition_root": primary_decomposition_root,
    }

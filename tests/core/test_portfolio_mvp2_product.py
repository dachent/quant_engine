"""One selected synthetic source set through every actual financial consumer."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import pytest

import portfolio_financial_mvp_contract as financial
import portfolio_financial_outcomes as outcomes
import portfolio_pipeline_materialize as materializer
from quant_engine.core.pipeline import (
    ArtifactNode,
    FileExpectation,
    GenerationNode,
    PipelineGate,
    PipelineSpec,
)

from test_portfolio_mvp2_vertical import assert_decomposition_vertical, fixture


def _artifact(
    node_id: str, path: Path, depends_on: tuple[str, ...] = ()
) -> ArtifactNode:
    content = path.read_bytes()
    return ArtifactNode(
        node_id, depends_on, path, hashlib.sha256(content).hexdigest(), len(content)
    )


def _generation(node_id: str, root_id: str, path: Path) -> GenerationNode:
    content = (path / "generation.json").read_bytes()
    manifest = json.loads(content)
    files = tuple(
        FileExpectation(
            child.relative_to(path).as_posix(),
            hashlib.sha256(child.read_bytes()).hexdigest(),
            child.stat().st_size,
        )
        for child in sorted(path.rglob("*"))
        if child.is_file()
    )
    return GenerationNode(
        node_id,
        (),
        root_id,
        manifest["generation_id"],
        hashlib.sha256(content).hexdigest(),
        files,
        path,
        False,
    )


def assemble_product_spec(
    root, monkeypatch, decomposition, selected, capital, hwm, dependence, consumers
):
    """Use existing pipeline nodes and gates, without a replacement release path."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(materializer, "HWM_GOVERNED_POLICY", selected["policy"])
    monkeypatch.setattr(
        materializer,
        "ACCOUNT_STRATEGY_CLASSIFICATION_POLICY",
        selected["flow_classification_policy"],
    )
    contract = json.loads(decomposition.contract_path.read_bytes())
    source_matrix = root / "source-matrix.json"
    source_matrix.write_bytes(financial.render_views(contract).source_matrix_json)
    definition = materializer._file_definition
    authority = {
        "contract": definition(decomposition.contract_path),
        "source_matrix": definition(source_matrix),
        "provider_generation": {
            "catalog": definition(
                selected["provider_generation"].parent
                / "normalized/provider-ledger.json"
            ),
            "manifest": definition(selected["provider_generation"]),
            "receipt": definition(selected["receipt"]),
        },
        "conformance": {
            "census_sha256": hashlib.sha256(
                fixture._canonical(contract["current_census"])
            ).hexdigest(),
            "contract_semantic_sha256": financial.contract_digest(contract),
            "strategies": contract["strategies"],
            "outcomes": contract["financial_outcomes"],
            "provider_admission": {"product_readiness": "READY_FOR_COMPOSITION"},
            "sources": [
                {
                    "source_id": source_id,
                    "consumers": ["AS_HELD_COMPOSITE_HWM"],
                    "current_generation": {"generation_id": None, "sha256": None},
                }
                for source_id in (
                    "portfolio-calendars",
                    "portfolio-fx-rates",
                    "portfolio-security-master",
                )
            ],
        },
    }
    source_paths = {
        "account_manifest_generation_path": selected["generation"],
        "account_manifest_admission_policy_path": selected["admission_policy"],
        "account_manifest_flow_classification_policy_path": selected[
            "flow_classification_policy"
        ],
        "provider_ledger_generation_path": selected["provider_generation"],
        "baseline_generation_path": selected["baseline_generation"],
    }
    assessment = materializer._hwm_input_authority_assessment(
        authority, hwm_bindings=hwm, hwm_source_paths=source_paths
    )
    assert assessment["schema_version"] == 2
    assert assessment["state"] == "AVAILABLE"
    assessment_path = fixture._write_json(root / "hwm-assessment.json", assessment)
    epoch_path = fixture._write_json(
        root / "epoch-inputs.json",
        {
            "inputs": {
                key: definition(path) for key, path in consumers["epoch_inputs"].items()
            }
        },
    )
    nodes = list(fixture.decomposition_inventory(decomposition))
    nodes.extend(
        [
            _generation(
                "selected-baseline", "baseline", selected["baseline_generation"].parent
            ),
            _generation(
                "selected-provider",
                "provider-ledger",
                selected["provider_generation"].parent,
            ),
            _generation("selected-report", "report", consumers["report_generation"]),
            _generation(
                "selected-panel", "control-panel", consumers["panel_generation"]
            ),
            _generation("selected-dependence", "correlation", dependence.generation),
            _artifact("financial-account-manifest-generation", selected["generation"]),
            _artifact("financial-capital-input", capital["input"]),
            _artifact("financial-outcome-hwm", hwm["governed_result"]),
            # No generation dependency, matching `financial-outcome-hwm`: the report
            # generation stopped publishing `invested-capital.json` when the workbook
            # runtime was removed, so a fixture binding it there describes a world that
            # no longer exists and would mask the regression.
            _artifact("financial-outcome-capital", capital["result"]),
        ]
    )
    for mode in fixture.EVIDENCE_MODES:
        nodes.append(
            _artifact(
                "financial-outcome-dependence-" + mode.lower().replace("_", "-"),
                dependence.generation / "correlation.json",
                ("selected-dependence",),
            )
        )
    for index, path in enumerate(sorted(dependence.source_root.rglob("*"))):
        if path.is_file():
            nodes.append(_artifact(f"dependence-source-{index}", path))
    base = json.loads(decomposition.inputs["base_spec_path"].read_bytes())
    for index, item in enumerate(base.get("provenance_sources", [])):
        nodes.append(
            _artifact(
                f"decomposition-base-source-{index}",
                decomposition.inputs["base_source_root"] / item["relative_path"],
            )
        )
    gates = tuple(
        PipelineGate(gate_id, path, hashlib.sha256(path.read_bytes()).hexdigest())
        for gate_id, path in (
            ("financial-mvp-contract-authority", decomposition.contract_path),
            ("financial-mvp-hwm-input-authority", assessment_path),
            ("financial-epoch-manifest-input-authority", epoch_path),
        )
    )
    spec = PipelineSpec(
        "synthetic-mvp2-product",
        date(2026, 9, 5),
        "51c18af9fd8f6e262fbce09fb33cb9aa8bf46792",
        datetime(2026, 9, 5, tzinfo=timezone.utc),
        root / "coordination",
        root / "state",
        {
            node.root_id: node.source_dir.parent.parent
            for node in nodes
            if isinstance(node, GenerationNode)
        },
        tuple(nodes),
        gates,
        (),
    )
    fixture._write_json(root / "pipeline-spec.json", spec.to_dict())
    return spec


def test_one_source_set_reaches_all_172_outcomes_and_both_consumers(
    tmp_path, monkeypatch
):
    root = tmp_path
    if sys.platform == "win32" and not str(root).startswith("\\\\?\\"):
        root = Path("\\\\?\\" + str(root.resolve()))
    prepared, selected = fixture.build_shared_history_source(root / "s", monkeypatch)
    decomposition = fixture.build_decomposition_vertical(
        root / "d", monkeypatch, prepared_history_fixture=prepared
    )
    assert_decomposition_vertical(decomposition)
    fixture.bind_final_hwm_contract(selected, decomposition.contract_path)
    fixture.install_synthetic_financial_authority(
        monkeypatch,
        [
            selected["contract"],
            decomposition.contract_path,
            *(
                bundle / "inputs/history/financial-mvp-contract.json"
                for bundle in decomposition.plan_bundles.values()
            ),
        ],
    )
    hwm = fixture.build_hwm_vertical(root / "h", selected)
    capital = fixture.build_capital_vertical(
        root / "c", selected, registry_path=decomposition.inputs["plan_path"]
    )
    dependence = fixture.build_dependence_vertical(
        root / "r", decomposition, selected["baseline_generation"].parent
    )
    consumer_fixture = fixture._load(
        "mvp2_consumers_fixture", "tests/core/mvp2_consumers_fixture.py"
    )
    consumers = consumer_fixture.build_consumers_vertical(
        root / "u", monkeypatch, decomposition, selected, capital, hwm, dependence
    )
    spec = assemble_product_spec(
        root / "p",
        monkeypatch,
        decomposition,
        selected,
        capital,
        hwm,
        dependence,
        consumers,
    )
    # Derive contributed capital from the eleven selected owner-purchase facts.
    ledger = json.loads(
        (
            selected["provider_generation"].parent / "normalized/provider-ledger.json"
        ).read_bytes()
    )
    expected_capital = sum(
        (Decimal(row["amount"]["value"]) for row in ledger["capital_anchors"]),
        Decimal(0),
    )
    actual_capital = json.loads(capital["result"].read_bytes())
    assert Decimal(actual_capital["current_aggregate_usd"]) == expected_capital
    # Independent raw-account NAV summation and geometric compounding in the
    # source-bound HWM oracle establish these same financial expectations.
    actual_hwm = json.loads(hwm["governed_result"].read_bytes())
    for key, expected in {
        "current_index": "100.1086326020742813632311057445208",
        "current_hwm": "101.0418486687037873759391923234952",
        "current_drawdown": "-0.0092359361880772460117548278757352",
        "maximum_drawdown": "-0.0179640718562874251497005988023952",
    }.items():
        assert Decimal(actual_hwm[key]) == Decimal(expected)
    proof = outcomes.validate_financial_outcomes(spec, allow_synthetic=True)
    assert proof["status"] == "PASS"
    assert proof["artifact_count"] == 172
    assert proof["epoch_count"] == 13
    assert len(proof["selected_cells"]) == 170
    assert len(proof["consumers"]["decomposition_cells"]) == 168
    assert len(proof["consumers"]["report_decomposition_cells"]) == 168
    fixture._write_json(root / "financial-outcome-proof.json", proof)
    # Missing results fail at the shared inventory boundary without rerunning models.
    missing = replace(
        spec,
        nodes=tuple(
            node for node in spec.nodes if node.node_id != "financial-outcome-hwm"
        ),
    )
    with pytest.raises(
        outcomes.FinancialOutcomeError, match="missing required financial outcome"
    ):
        outcomes.verify_outcome_inventory(
            missing,
            json.loads(decomposition.contract_path.read_bytes()),
            plan=json.loads(decomposition.inputs["plan_path"].read_bytes()),
        )

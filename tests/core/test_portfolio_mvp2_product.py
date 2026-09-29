"""One selected synthetic source set through every actual financial consumer."""

from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys

import pytest

import portfolio_financial_mvp_contract as financial
import portfolio_financial_outcomes as outcomes
import portfolio_hwm_selected_inputs as hwm_inputs
import portfolio_pipeline_materialize as materializer
from quant_engine.core.pipeline import (
    ArtifactNode,
    FileExpectation,
    GenerationNode,
    PipelineGate,
    PipelineSpec,
)

from test_portfolio_mvp2_vertical import assert_decomposition_vertical, fixture


# Every attribute the 172-outcome test world patches, with the reason.  The
# test asserts that the attributes it actually patched are exactly these, so a
# new patch cannot enter the chain undeclared.
PATCH_INVENTORY = {
    "_verify_release_validation": (
        "tests/core/conftest.py neuters it for mechanics tests; this test "
        "restores the real verifier, so the whole chain runs with it live"
    ),
    "_history_contract": (
        "structure-only history-contract parser on the layered-history producer "
        "and the planner/current-plan verifier, whose real reader runs "
        "validate_contract; once install_synthetic_financial_authority runs, "
        "the verifier's parser first runs the narrowed load_contract"
    ),
    "_layered_history_module": (
        "pin the planner and current-plan verifier to the one layered-history "
        "module instance that carries the synthetic seams"
    ),
    "_history_fixture": (
        "plan_tests._fixture builds the layered history from this world's "
        "prepared fixture instead of a fresh one"
    ),
    "_current_source_admission_authority": (
        "synthetic admission receipt: this world has no real provider capture "
        "perimeter; the real receipt binding has its own tests"
    ),
    "_portfolio123_version_equivalence": (
        "synthetic P123 version-equivalence stand-in for the same reason"
    ),
    "load_contract": (
        "narrowed synthetic authority: real schema validation, then canonical "
        "outcome, census, strategy-perimeter and registry-hash equivalence; it "
        "skips validate_contract, which rejects this synthetic world (registry "
        "strategy order; MIXED strategy-backtests version refs)"
    ),
    "_financial_module": (
        "current dependence (and the panel's copy) resolves the same narrowed "
        "contract module"
    ),
    "HWM_GOVERNED_POLICY": "materializer reads the world's HWM policy",
    "ACCOUNT_STRATEGY_CLASSIFICATION_POLICY": (
        "materializer reads the world's flow-classification policy"
    ),
    "_GOVERNED_MEMBERSHIP_CENSUS": (
        "HWM replay binds the world's declared census; the repository census "
        "cannot bound a synthetic account source set"
    ),
    "DEPENDENCE_POLICY": "report and panel read the world's dependence policy",
    "_PORTABLE_HWM_POLICY": "report and panel read the world's HWM policy",
    "_PORTABLE_HWM_PRODUCT_CONTRACT": (
        "report and panel bind the world's product contract"
    ),
    "CURRENT_EPOCH_CROSSWALK_AS_OF_DATE": "report binds the world's crosswalk as-of",
    "CURRENT_EPOCH_CROSSWALK_CUTOFF_DATE": (
        "report binds the world's crosswalk calculation cutoff"
    ),
    "FINANCIAL_MVP_CONTRACT": "panel binds the world's product contract",
    "_HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT": (
        "report binds the world's product contract as HWM policy authority"
    ),
    "FLOW_CLASSIFICATION_POLICY_CONTRACT": (
        "report binds the world's flow-classification policy"
    ),
    "spec_from_file_location": (
        "importlib.util wrapper that alters only the report's freshly loaded "
        "dependence module, which then resolves the narrowed contract module"
    ),
    "SOURCE_CONTRACT": "panel binds a synthetic frozen legacy-appendix contract",
}
# Substitutions that are not attribute patches (listed for review):
# - mvp2_consumers_fixture._install_dynamic_dependence_loader assigns
#   `_financial_module` on the report's freshly executed dependence module;
# - fixture loaders register test-helper modules in sys.modules, and
#   test_portfolio_financial_mvp_contract.py swaps a fresh
#   portfolio_financial_mvp_contract instance in at collection;
# - build_shared_history_source gives mutate_baseline's report oracle the
#   producer's Matrixport epoch HPR receipt (the catalog merge
#   portfolio_report.build_candidate performs from the history contract);
# - the source-period authority files are synthetic stand-ins (an admission
#   receipt and P123 version evidence read only by the stand-in seams), and
#   the plan's provider-ledger manifest adds the providers/matrixport
#   generation reference the real provider-ledger tool emits;
# - validate_financial_outcomes(..., allow_synthetic=True) accepts the
#   DEVELOPMENT plan-bundle profile and the SYNTHETIC capital authority and
#   skips the applicability-readiness gate for this synthetic world.
CONTRACT_SCHEMA = (
    fixture.REPOSITORY_ROOT
    / "schemas/portfolio/financial-mvp-source-contract.schema.json"
)
GAP_POLICY_DATES = (
    "declared_missing_source_dates",
    "omitted_return_dates",
    "unavailable_return_dates",
)
SPAN_IDENTITY = (
    "missing_source_date",
    "span_start_date",
    "span_end_date",
    "span_days",
    "subscription_events_in_span",
)


def _matrixport_gap_policy(contract: dict) -> dict:
    return next(
        row["declared_gap_policy"]
        for row in contract["source_segments"]
        if row["source_id"] == "matrixport-account-generation"
    )


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
    # The synthetic world binds the same declared governed census everywhere:
    # the repository census contract cannot bound a synthetic account source
    # set, so the declared census stands in for it exactly as the synthetic
    # policy files above stand in for the repository policies.  The replay
    # still fails closed on any census identity or admission mismatch.
    monkeypatch.setattr(
        hwm_inputs, "_GOVERNED_MEMBERSHIP_CENSUS", selected["membership_census"]
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
        # The synthetic world declares its own governed census, exactly as the
        # selected generation recorded; the repository census contract cannot
        # bound a synthetic account source set.
        "membership_census_path": selected["membership_census"],
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
    # The layered history was built under full source-period authority, so the
    # outcome validator's plan replay must receive the same reader authorities
    # the attended run would bind (portfolio_financial_outcomes
    # _HISTORY_AUTHORITY_NODES); replaying without them fails its key-set check.
    for name, node_id in outcomes._HISTORY_AUTHORITY_NODES.items():
        nodes.append(_artifact(node_id, decomposition.history_authorities[name]))
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
    tmp_path, monkeypatch, real_verify_release_validation
):
    """Green by validation: every patch is declared in PATCH_INVENTORY.

    The final bound contract passes the real checked-in schema, carries the
    selected world's declared Matrixport outage (never the empty policy), and
    a gap without a policy fails both the schema and the narrowed loader.
    """
    import portfolio_financial_mvp_contract as loaded_financial
    import quant_engine.core.release as release_module

    # No part of this chain may run with release validation disabled.
    monkeypatch.setattr(
        release_module, "_verify_release_validation", real_verify_release_validation
    )
    root = tmp_path
    if sys.platform == "win32" and not str(root).startswith("\\\\?\\"):
        root = Path("\\\\?\\" + str(root.resolve()))
    prepared, selected = fixture.build_shared_history_source(root / "s", monkeypatch)
    # The selected contract snapshotted the world's declared #287-shaped
    # Matrixport outage before the native rows were rebound to the ledger.
    selected_gap_policy = _matrixport_gap_policy(
        json.loads(selected["contract"].read_bytes())
    )
    decomposition = fixture.build_decomposition_vertical(
        root / "d", monkeypatch, prepared_history_fixture=prepared
    )
    assert_decomposition_vertical(decomposition)
    fixture.bind_final_hwm_contract(selected, decomposition.contract_path)
    final_contract = json.loads(selected["contract"].read_bytes())
    schema = json.loads(CONTRACT_SCHEMA.read_bytes())
    # The final bound contract is schema-valid on its own bytes.
    assert financial.schema_errors(schema, final_contract, schema) == []
    # It binds the same declared outage as the selected contract -- never the
    # empty 0/0/0/0 policy the prepared contract used to overwrite it with.
    final_gap_policy = _matrixport_gap_policy(final_contract)
    declared_outage = {
        "declared_missing_source_dates": ["2026-04-25"],
        "omitted_return_dates": ["2026-04-26"],
        "unavailable_return_dates": ["2026-04-25", "2026-04-26"],
    }
    assert {key: final_gap_policy[key] for key in GAP_POLICY_DATES} == declared_outage
    assert {key: selected_gap_policy[key] for key in GAP_POLICY_DATES} == (
        declared_outage
    )
    span_identity = [
        {
            "missing_source_date": "2026-04-25",
            "span_start_date": "2026-04-24",
            "span_end_date": "2026-04-26",
            "span_days": 2,
            "subscription_events_in_span": 0,
        }
    ]
    for policy in (final_gap_policy, selected_gap_policy):
        assert [
            {key: receipt[key] for key in SPAN_IDENTITY}
            for receipt in policy["span_return_receipts"]
        ] == span_identity
    derived = {*GAP_POLICY_DATES, "span_return_receipts"}
    assert {
        key: value for key, value in final_gap_policy.items() if key not in derived
    } == {
        key: value for key, value in selected_gap_policy.items() if key not in derived
    }
    # The span receipt binds the final native NAV endpoints: the rebound NAV
    # endpoint omits the gap date and the span is the 04-24 -> 04-26 ratio.
    native_navs = {
        row["timestamp_utc"][:10]: Decimal(row["nav"])
        for row in csv.DictReader(
            io.StringIO(prepared["matrixport_nav_path"].read_text(encoding="utf-8"))
        )
    }
    assert "2026-04-25" not in native_navs
    span_return = native_navs["2026-04-26"] / native_navs["2026-04-24"] - 1
    assert abs(
        Decimal(final_gap_policy["span_return_receipts"][0]["return"]) - span_return
    ) < Decimal("1e-25")
    # Negative control: the same bound contract with the declared outage
    # stripped -- a gap without a policy -- fails the real schema.
    gap_without_policy = deepcopy(final_contract)
    stripped = _matrixport_gap_policy(gap_without_policy)
    for key in derived:
        stripped[key] = []
    assert financial.schema_errors(schema, gap_without_policy, schema)
    negative_control_path = fixture._write_json(
        root / "negative-control" / "financial-mvp-contract.json", gap_without_policy
    )
    load_contract = fixture.install_synthetic_financial_authority(
        monkeypatch,
        [
            selected["contract"],
            decomposition.contract_path,
            *(
                bundle / "inputs/history/financial-mvp-contract.json"
                for bundle in decomposition.plan_bundles.values()
            ),
            negative_control_path,
        ],
        source_period_fixture=prepared,
    )
    # ... and the narrowed synthetic loader rejects it too.
    with pytest.raises(
        loaded_financial.FinancialMvpContractError, match="schema validation failed"
    ):
        load_contract(negative_control_path)
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
    # Release validation stayed live, and every attribute the chain patched is
    # declared with its reason.  MonkeyPatch keeps (target, name, prior value)
    # for each setattr until teardown.
    assert release_module._verify_release_validation is real_verify_release_validation
    patched = {name for _target, name, _prior in monkeypatch._setattr}
    assert patched == set(PATCH_INVENTORY), sorted(patched ^ set(PATCH_INVENTORY))

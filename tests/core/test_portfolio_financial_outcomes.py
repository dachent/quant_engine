"""The final product requires financial artifacts, not generic root names."""

from __future__ import annotations

from datetime import date
import ast
import importlib
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import portfolio_financial_outcomes as outcomes
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.core import pipeline
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.publication import stage_generation, stage_generation_bundle


def test_consumer_uses_exact_root_file_when_normalized_alias_also_exists(tmp_path):
    files = []
    for name in ("portfolio-baseline.json", "normalized/portfolio-baseline.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"same_source":true}')
        files.append(
            pipeline.FileExpectation(
                name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size
            )
        )
    generation = pipeline.GenerationNode(
        "baseline", (), "baseline", "a" * 64, "b" * 64, tuple(files), tmp_path, False
    )
    path, content = outcomes._generation_file(generation, "portfolio-baseline.json")
    assert path == tmp_path / "portfolio-baseline.json"
    assert content == path.read_bytes()
    path.write_bytes(b'{"changed":true}')
    with pytest.raises(outcomes.FinancialOutcomeError, match="identity differs"):
        outcomes._generation_file(generation, "portfolio-baseline.json")


def test_same_factor_bytes_are_resolved_within_the_selected_mode_bundle(tmp_path):
    content = b"date,factor\n2026-09-04,0.01\n"
    digest = hashlib.sha256(content).hexdigest()
    nodes = []
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        path = tmp_path / mode / "inputs/factors.csv"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        nodes.append(pipeline.ArtifactNode(mode, (), path, digest, len(content)))
    spec = SimpleNamespace(nodes=tuple(nodes))
    selected = outcomes._artifact_for_digest(
        spec,
        digest,
        len(content),
        source_root=tmp_path / "LIVE_PLUS_BACKTEST",
        relative_path="inputs/factors.csv",
    )
    assert selected.path == nodes[1].path
    with pytest.raises(outcomes.FinancialOutcomeError, match="absent or ambiguous"):
        outcomes._artifact_for_digest(
            spec,
            digest,
            len(content),
            source_root=tmp_path / "LIVE_PLUS_BACKTEST",
            relative_path="inputs/unselected.csv",
        )


def _account_generation_spec(tmp_path, rows, additional_artifacts=None):
    content = json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "account-strategy-current-epoch-crosswalk",
            "rows": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    artifact_id = "authority/account-assignment-crosswalk.json"
    payloads = {
        name: canonical_json_bytes(json.loads(payload))
        for name, payload in {
            artifact_id: content,
            "verification/independent-verification.json": b'{"synthetic_fixture":true}',
            **(additional_artifacts or {}),
        }.items()
    }
    manifest = GenerationManifest.build(
        as_of_date=date(2026, 9, 4),
        adapter=AdapterIdentity("synthetic-account-manifest", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic selected epoch perimeter",
            fact_types=("account assignment",),
            observed_start=date(2026, 9, 4),
            observed_end=date(2026, 9, 4),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=name,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                as_of_date=date(2026, 9, 4),
                schema_name="synthetic-account-authority",
                schema_version="1",
            )
            for name, payload in payloads.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation = stage_generation_bundle(tmp_path, manifest, payloads)
    crosswalk = generation / artifact_id
    manifest_path = generation / "generation.json"
    node = pipeline.ArtifactNode(
        "financial-account-manifest-generation",
        (),
        manifest_path,
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        manifest_path.stat().st_size,
    )
    return SimpleNamespace(nodes=(node,)), crosswalk


def test_epoch_replay_selects_manifest_bound_product_perimeter(tmp_path):
    included = {
        "account_id": "account-required",
        "strategy_id": "required-strategy",
        "effective_start": "2026-01-01",
        "effective_end": None,
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
    }
    excluded = {
        "account_id": "account-excluded",
        "strategy_id": "excluded-strategy",
        "effective_start": "2025-01-01",
        "effective_end": "2025-12-31",
        "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
    }
    spec, crosswalk = _account_generation_spec(tmp_path, [excluded, included])

    keys, _generation, _manifest = outcomes._selected_epoch_assignment_keys(spec)

    assert keys == (("account-required", "required-strategy", "2026-01-01", None),)
    crosswalk.write_bytes(crosswalk.read_bytes() + b"\n")
    with pytest.raises(outcomes.FinancialOutcomeError, match="identity differs"):
        outcomes._selected_epoch_assignment_keys(spec)


def test_epoch_replay_rejects_duplicate_selected_perimeter_keys(tmp_path):
    included = {
        "account_id": "account-required",
        "strategy_id": "required-strategy",
        "effective_start": "2026-01-01",
        "effective_end": None,
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
    }
    spec, _crosswalk = _account_generation_spec(tmp_path, [included, included])

    with pytest.raises(outcomes.FinancialOutcomeError, match="perimeter is invalid"):
        outcomes._selected_epoch_assignment_keys(spec)


@pytest.mark.parametrize("mutation", [None, "source", "projection", "calculation"])
def test_epoch_replay_uses_bound_projection_and_checks_its_source(
    tmp_path, monkeypatch, mutation
):
    import portfolio_hwm_selected_inputs as source_verifier

    # These are deliberately different synthetic authority bytes. Numerical
    # producer behavior is covered by the complete product test; this test proves
    # which authority reaches that producer and rejects changed source evidence.
    source = b'{"synthetic_epochs":["included","excluded"]}'
    projected = b'{"synthetic_epochs":["included"]}'
    identity = json.dumps(
        {
            "admission_policy": {
                "sha256": hashlib.sha256(source).hexdigest(),
                "size_bytes": len(source),
            }
        }
    ).encode()
    included = {
        "account_id": "account-required",
        "strategy_id": "required-strategy",
        "effective_start": "2026-01-01",
        "effective_end": None,
        "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
    }
    finalized = {
        "rows": [
            {**included, "account_label": "Synthetic account", "last_four": "TEST"}
        ],
        "source_identities": {"synthetic_finalized_identity": True},
    }
    calculation = {
        key: {}
        for key in (
            "calculation_ledger",
            "calculation_ledger_identity",
            "calculation_classified_flows",
            "calculation_flow_coverage",
        )
    }
    spec, crosswalk = _account_generation_spec(
        tmp_path / "account",
        [included],
        {
            "authority/flow-classification-policy.json": b"{}",
            "authority/selected-input-identities.json": identity,
            "authority/mapped-epoch-admission.json": projected,
            "authority/calculation-inputs.json": json.dumps(calculation).encode(),
            "outputs/account-strategy-manifest.json": json.dumps(finalized).encode(),
        },
    )
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    (baseline_root / "portfolio-baseline.json").write_bytes(b"{}")
    baseline = pipeline.GenerationNode(
        "baseline",
        (),
        "baseline",
        "a" * 64,
        "b" * 64,
        (
            pipeline.FileExpectation(
                "portfolio-baseline.json", hashlib.sha256(b"{}").hexdigest(), 2
            ),
        ),
        baseline_root,
        False,
    )
    selected = {
        key: b"{}\n"
        for key in (
            "account_strategy_ledger",
            "account_strategy_ledger_identity",
            "account_strategy_classified_flows",
            "account_strategy_flow_coverage",
        )
    }
    selected["account_strategy_admission_policy"] = source
    received = []

    sources = {
        key: tmp_path / (key + ".json")
        for key in (
            "account_manifest_admission_policy_path",
            "account_manifest_flow_classification_policy_path",
            "provider_ledger_generation_path",
            "baseline_generation_path",
        )
    }

    def capture_selected_authority(bundle, **selected_sources):
        received.append((bundle, selected_sources))

    monkeypatch.setattr(
        source_verifier, "_verify_account_manifest_replay", capture_selected_authority
    )
    monkeypatch.setattr(outcomes, "selected_hwm_source_paths", lambda spec: sources)
    if mutation == "source":
        selected["account_strategy_admission_policy"] += b"\n"
    elif mutation == "projection":
        projection_path = crosswalk.parent / "mapped-epoch-admission.json"
        projection_path.write_bytes(projection_path.read_bytes() + b"\n")
    elif mutation == "calculation":
        selected["account_strategy_ledger"] = b'{"changed":true}\n'
    if mutation is not None:
        expected = {
            "source": "identity differs",
            "projection": "generation verification failed",
            "calculation": "calculation input differs",
        }[mutation]
        with pytest.raises(outcomes.FinancialOutcomeError, match=expected):
            outcomes._rebuild_selected_epochs(spec, baseline, selected)
        assert received == []
        return
    assert outcomes._rebuild_selected_epochs(spec, baseline, selected) == finalized
    assert len(received) == 1
    assert received[0][0].payloads[
        "authority/mapped-epoch-admission.json"
    ] == canonical_json_bytes(json.loads(projected))
    assert received[0][1] == sources


def _contract():
    return json.loads(
        (
            Path(__file__).parents[2]
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text()
    )


def test_required_roles_are_exact_contract_strategy_lens_surface_mode_product():
    roles = outcomes.required_outcome_roles(_contract())
    assert len(roles) == 108
    assert {
        strategy: sum(
            value[0] == "DECOMPOSITION" and value[1] == strategy
            for value in roles.values()
        )
        for strategy in (
            "grizzlybears-vtma",
            "p123-ai-factor",
            "p123-eu",
            "qsi",
            "robuxio-crypto",
            "tt-portfolio",
            "volatis-strategy",
        )
    } == {
        "grizzlybears-vtma": 20,
        "p123-ai-factor": 16,
        "p123-eu": 4,
        "qsi": 20,
        "robuxio-crypto": 4,
        "tt-portfolio": 20,
        "volatis-strategy": 20,
    }
    assert roles["financial-outcome-hwm"] == ("AS_HELD_COMPOSITE_HWM",)
    assert roles["financial-outcome-capital"] == ("INVESTED_CAPITAL",)
    assert roles[
        "financial-outcome-decomposition-tt-portfolio-us-equity-style-dynamic-fls-live-plus-backtest"
    ] == (
        "DECOMPOSITION",
        "tt-portfolio",
        "us-equity-style",
        "ETF_STYLE",
        "DYNAMIC_FLS",
        "LIVE_PLUS_BACKTEST",
    )
    assert roles[
        "financial-outcome-decomposition-tt-portfolio-multi-asset-style-dynamic-fls-live-plus-backtest"
    ] == (
        "DECOMPOSITION",
        "tt-portfolio",
        "multi-asset-style",
        "ETF_STYLE",
        "DYNAMIC_FLS",
        "LIVE_PLUS_BACKTEST",
    )
    assert roles["financial-outcome-dependence-live-only"] == (
        "DEPENDENCE",
        "LIVE_ONLY",
    )


def test_generic_generation_roots_cannot_substitute_for_actual_outcomes():
    spec = SimpleNamespace(nodes=())
    with pytest.raises(
        outcomes.FinancialOutcomeError, match="missing required financial outcome"
    ):
        outcomes.verify_outcome_inventory(spec, _contract())


def test_unknown_outcome_is_rejected_before_reading_its_file():
    spec = SimpleNamespace(
        nodes=(SimpleNamespace(node_id="financial-outcome-made-up"),)
    )
    with pytest.raises(
        outcomes.FinancialOutcomeError, match="unknown financial outcome"
    ):
        outcomes.verify_outcome_inventory(spec, _contract())


def test_shared_production_boundary_calls_actual_outcome_verifier(monkeypatch):
    """Programmatic/resume callers cannot rely on conformance readiness alone."""
    spec = SimpleNamespace(gates=(), nodes=())
    monkeypatch.setattr(pipeline, "_financial_mvp_product_authority", lambda spec: {})
    monkeypatch.setattr(pipeline, "_requires_current_product", lambda spec: True)
    monkeypatch.setattr(pipeline, "_current_product_authority", lambda authority: True)
    monkeypatch.setattr(pipeline, "_requires_production_product", lambda spec: True)
    monkeypatch.setattr(pipeline, "_production_hwm_available", lambda authority: True)
    monkeypatch.setattr(
        pipeline,
        "_financial_mvp_summary",
        lambda authority, spec: {"ready_for_execution": True},
    )
    # The unmodified shared library accepts this empty, allegedly READY product.
    with pytest.raises(pipeline.PipelineValidationError, match="financial"):
        pipeline._require_executable_product(spec)


def test_semantic_payload_requires_real_completion_proof():
    import portfolio_pipeline_materialize as materializer

    recipe = {
        "schema_version": 3,
        "as_of_date": "2026-09-05",
        "release_commit": "a" * 40,
    }
    with pytest.raises(materializer.MaterializationError, match="financial outcome"):
        materializer._semantic_gate_payload(
            recipe, {}, financial_outcomes={"status": "BLOCKED"}
        )


def test_semantic_gate_ignores_only_financial_invocation_counters():
    """A receipt reuse is execution evidence, not a new financial outcome."""

    import portfolio_pipeline_materialize as materializer

    recipe = {
        "schema_version": 3,
        "as_of_date": "2026-09-05",
        "release_commit": "a" * 40,
    }
    cold = {
        "artifact_sha256s": {"decomposition": "b" * 64},
        "consumers": {"report": {"status": "PASS"}},
        "outcome_ids": ["DECOMPOSITION"],
        "selected_cells": {"decomposition": {"status": "AVAILABLE"}},
        "status": "PASS",
        "work_counters": {
            "correlation_build_calls": 0,
            "full_rederivation_count": 1,
            "grid_search_calls": 1,
            "model_fit_calls": 6,
            "receipt_reuse_count": 0,
        },
    }
    warm = json.loads(json.dumps(cold))
    warm["work_counters"] = {
        "correlation_build_calls": 0,
        "full_rederivation_count": 0,
        "grid_search_calls": 0,
        "model_fit_calls": 0,
        "receipt_reuse_count": 1,
    }

    cold_gate = materializer._semantic_gate_payload(recipe, {}, financial_outcomes=cold)
    warm_gate = materializer._semantic_gate_payload(recipe, {}, financial_outcomes=warm)

    assert (
        cold_gate["financial_outcomes_sha256"] == warm_gate["financial_outcomes_sha256"]
    )
    assert cold["work_counters"] != warm["work_counters"]


def _staged_ledger(root: Path, generation_id: str) -> Path:
    """Stage one provider-ledger file under its generation's normalized route.

    Both shapes are real: the governed route writes the ledger at the
    generation root, and this one nests it. Binding by generation id rather
    than by path holds for either.
    """
    ledger = root / "generations" / generation_id / "normalized/provider-ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b'{"as_of_date": "2026-09-02"}')
    return ledger


def _capital_input(path: Path, ledger: Path) -> Path:
    """A portable binding: content hash plus the generation it came from."""
    path.write_bytes(
        json.dumps(
            {
                "source_bindings": {
                    "provider_ledger": {
                        "sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
                        "size_bytes": ledger.stat().st_size,
                        "generation_id": ledger.parent.parent.name,
                    }
                }
            }
        ).encode("utf-8")
    )
    return path


def test_capital_ledger_binding_accepts_the_selected_generation(tmp_path):
    ledger = _staged_ledger(tmp_path, "a" * 64)
    selected = ledger.parent.parent

    assert (
        outcomes.verify_selected_capital_ledger(
            _capital_input(tmp_path / "capital-input.json", ledger),
            selected / "generation.json",
        )
        == selected.resolve()
    )


def test_capital_ledger_binding_rejects_a_prior_generation(tmp_path):
    selected = _staged_ledger(tmp_path, "a" * 64).parent.parent
    # Byte-identical contents under a different generation: the older ledger
    # still satisfies its own contract admission, so only identity rejects it.
    prior = _staged_ledger(tmp_path, "b" * 64)

    with pytest.raises(
        outcomes.FinancialOutcomeError,
        match="capital provider ledger differs from the selected generation",
    ):
        outcomes.verify_selected_capital_ledger(
            _capital_input(tmp_path / "capital-input.json", prior),
            selected / "generation.json",
        )


def test_capital_ledger_binding_requires_a_bound_ledger(tmp_path):
    selected = _staged_ledger(tmp_path, "a" * 64).parent.parent
    absent = tmp_path / "capital-input.json"
    absent.write_bytes(b'{"source_bindings": {}}')

    with pytest.raises(
        outcomes.FinancialOutcomeError,
        match="capital provider ledger binding is absent",
    ):
        outcomes.verify_selected_capital_ledger(absent, selected / "generation.json")


def test_hwm_replay_uses_the_admitted_input_authority(tmp_path, monkeypatch):
    """The replay must rebuild under the authority the observations were built under.

    A provider ledger admitted under a retained input contract carries a policy
    retargeted onto that contract.  Replaying with the product contract and the
    repository policy rebuilds different bytes and rejects a correct generation.
    """
    import portfolio_hwm_selected_inputs as builder

    product = tmp_path / "product-contract.json"
    product.write_bytes(b'{"product": 1}')
    retained = tmp_path / "retained-input-contract.json"
    retained.write_bytes(b'{"retained": 1}')
    generation = tmp_path / "input-generation.json"
    generation.write_bytes(b'{"rebuilt": true}')
    bindings = {
        "calendar_authority": tmp_path / "calendar.json",
        "input_generation": generation,
        "provider_admission_receipt": tmp_path / "receipt.json",
    }
    retargeted = b'{"retargeted": "policy"}'
    observed: list[dict[str, object]] = []

    def _build(**kwargs):
        observed.append(kwargs)
        return {"rebuilt": True}

    monkeypatch.setattr(builder, "build_selected_hwm_inputs", _build)

    resolved = outcomes.verify_selected_hwm_generation(
        {},
        contract_path=product,
        bindings=bindings,
        input_contract_path=retained,
        policy_bytes=retargeted,
    )
    assert resolved == {
        "portfolio-calendars",
        "portfolio-fx-rates",
        "portfolio-security-master",
    }
    assert observed[-1]["financial_contract_path"] == retained
    assert observed[-1]["hwm_policy_bytes"] == retargeted

    # With no admitted input supplied the product contract and the repository
    # policy still govern, exactly as before.
    outcomes.verify_selected_hwm_generation(
        {}, contract_path=product, bindings=bindings
    )
    assert observed[-1]["financial_contract_path"] == product
    assert observed[-1]["hwm_policy_bytes"] is None


def test_artifact_node_binds_exactly_one_named_production_node(tmp_path):
    """The runtime reads the admission policy from the specification, by node id.

    `_epoch_manifest_profile`'s schema-10 branch used to search the bound generation for an
    artifact matching the declared `admission_policy` digest. The generation stores a
    canonicalised projection of that policy rather than the policy, so the search could not
    succeed for a pretty-printed input -- and a pretty-printed input cannot be a generation
    artifact in the first place, because artifacts are canonical JSON by construction.
    """

    payload = b'{\n  "policy_id": "epoch-admission",\n  "admissions": []\n}\n'
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "mapped-epoch-admission-current-v10-dated.json"
    path.write_bytes(payload)
    node = pipeline.ArtifactNode(
        "financial-account-manifest-admission-policy", (), path, digest, len(payload)
    )

    spec = SimpleNamespace(nodes=(node,))
    resolved = outcomes._artifact_node(
        spec, "financial-account-manifest-admission-policy"
    )
    assert resolved.path == path
    assert outcomes._verified_file(
        resolved.path, resolved.sha256, resolved.size_bytes
    ) == (payload)

    with pytest.raises(outcomes.FinancialOutcomeError, match="artifact node differs"):
        outcomes._artifact_node(spec, "financial-account-manifest-generation")
    with pytest.raises(outcomes.FinancialOutcomeError, match="artifact node differs"):
        outcomes._artifact_node(
            SimpleNamespace(nodes=(node, node)),
            "financial-account-manifest-admission-policy",
        )

    path.write_bytes(payload + b"\n")
    with pytest.raises(outcomes.FinancialOutcomeError, match="identity differs"):
        outcomes._verified_file(resolved.path, resolved.sha256, resolved.size_bytes)


def _authority_spec(tmp_path, node_ids):
    nodes = []
    for node_id in node_ids:
        path = tmp_path / f"{node_id}.json"
        content = json.dumps({"node": node_id}).encode("utf-8")
        path.write_bytes(content)
        nodes.append(
            pipeline.ArtifactNode(
                node_id, (), path, hashlib.sha256(content).hexdigest(), len(content)
            )
        )
    return SimpleNamespace(nodes=nodes)


def test_replay_binds_every_layered_history_authority(tmp_path):
    """The producer decides what a layered history must prove from what it is handed.

    `portfolio_target_series.py:6088-6119` derives the exact key set a generation must carry
    from which authority arguments its caller supplied, so a generation built with them can
    only be verified by a caller that also supplies them. The replay omitted all five and
    failed with "layered history generation keys differ" the moment the public-factor gate
    ahead of it was cleared.
    """

    spec = _authority_spec(tmp_path, outcomes._HISTORY_AUTHORITY_NODES.values())
    resolved = outcomes._history_authorities(spec, tmp_path / "bundle")
    assert set(resolved) == set(outcomes._HISTORY_AUTHORITY_NODES) | {
        "financial_input_contract_path"
    }
    assert resolved["financial_input_contract_path"] == (
        tmp_path / "bundle" / "inputs/history/financial-mvp-contract.json"
    )


def test_replay_accepts_a_bundle_that_declares_no_authorities(tmp_path):
    """A synthetic fixture whose generation was built without authorities still replays.

    This cannot weaken a release: a generation built WITH them and replayed without them
    fails the key-set check outright, which is the behaviour the test above pins.
    """

    assert outcomes._history_authorities(_authority_spec(tmp_path, ()), tmp_path) == {}


def test_replay_refuses_a_partially_bound_admission_group(tmp_path):
    """Half the admission group is the case that could verify the wrong shape.

    The verifier ties the receipt to its ledger manifest and catalog, so binding some of them
    describes no generation any producer could have built.
    """

    nodes = outcomes._HISTORY_AUTHORITY_NODES
    partial = [
        node_id
        for name, node_id in nodes.items()
        if name != "provider_ledger_catalog_path"
    ]
    with pytest.raises(outcomes.FinancialOutcomeError, match="partially bound"):
        outcomes._history_authorities(_authority_spec(tmp_path, partial), tmp_path)


def test_replay_accepts_the_admission_group_without_version_evidence(tmp_path):
    """Version evidence is optional upstream and a separate flag in the verifier.

    Refusing the admission trio without it rejected a valid replay before the producer could
    judge it. Its absence still cannot weaken a release, because a generation that carries
    `portfolio123_version_equivalence` fails the verifier's key-set check without it.
    """

    nodes = outcomes._HISTORY_AUTHORITY_NODES
    admission = [
        node_id
        for name, node_id in nodes.items()
        if name not in outcomes._OPTIONAL_HISTORY_AUTHORITIES
    ]
    resolved = outcomes._history_authorities(
        _authority_spec(tmp_path, admission), tmp_path / "bundle"
    )
    assert "portfolio123_version_evidence_path" not in resolved
    assert set(resolved) == {
        "provider_admission_receipt_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "financial_input_contract_path",
    }


def test_decomposition_replay_hands_the_producer_its_own_arguments():
    """Pin the seam itself, because every one of these was omitted at some point.

    A reviewer diffing these call sites by hand reads named keywords and misses `**` expansions
    -- which is exactly how the five authorities were first reported as supplied by neither
    side, when `portfolio_attended_preparation.py:1637` passes them by expansion.
    """

    tree = ast.parse(Path(outcomes.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_bound_decomposition_plan"
    )
    call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("build_bundle")
    )
    named = {keyword.arg for keyword in call.keywords if keyword.arg}
    expanded = {
        ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg is None
    }
    assert "retained_public_factor_capture" in named
    assert any("_history_authorities" in item for item in expanded)


# -- N34: the release-path verifier re-reads the FX-bounded dependence end ---------------
#
# `_verify_consumers` re-derives the report's dependence analysis from the selected
# correlation generation and requires it to equal what the report published.  The
# admitted FX generation ends at F (2026-09-03), before the calculation cutoff K
# (2026-09-17): a strategy whose native NAV days after F the provider ledger recorded as
# OUTSIDE_ADMITTED_FX_BOUNDARY findings has no USD value past F, so the composite of the
# required strategies ends at F and the report publishes that true end.  The verifier
# must accept exactly that end, and it must derive it from the verified ledger and the
# contract (FXB-1), never from the report it is verifying.

FX_END = "2026-09-03"
CUTOFF = "2026-09-17"
FX_GENERATION_ID = "9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e"
FX_STRATEGY = "robuxio-crypto"  # REQUIRED in the repository contract
EXCLUDED_STRATEGY = "composer-holy-grail"  # EXCLUDED in the repository contract
HWM_MARKER = {"synthetic": "governed HWM proof"}
PANEL_MARKER = {"synthetic": "panel dependence projection"}


def _fx_contract_file(directory):
    """The repository contract with exactly one FX segment, ending at ``FX_END``."""

    contract = _contract()
    contract["source_segments"] = [
        {
            "current_generation": {"as_of": FX_END, "generation_id": FX_GENERATION_ID},
            "interval": {"observed_end": FX_END},
            "source_id": "portfolio-fx-rates",
        }
    ]
    directory.mkdir(parents=True)
    path = directory / "financial-mvp-fx.json"
    path.write_bytes(canonical_json_bytes(contract))
    return path


def _correlation_generation(root, end):
    """A verified correlation generation whose every layer ends at ``end``."""

    series_ids = [f"Current Strategy {index}" for index in range(1, 8)]
    pairs = [
        (left, right)
        for index, left in enumerate(series_ids)
        for right in series_ids[index + 1 :]
    ]

    def mode_payload():
        return {
            "series_ids": series_ids,
            "observed_cells": [
                {
                    "left": left,
                    "right": right,
                    "status": "AVAILABLE",
                    "coverage": {
                        "common_observations": 2,
                        "observed_start": "2026-08-01",
                        "observed_end": end,
                    },
                    "pearson": "0.1",
                    "spearman": "0.2",
                    "horizons": {},
                    "rolling_pearson": {},
                    "downside": {},
                    "drawdown_regimes": {},
                    "joint_tail": {},
                }
                for left, right in pairs
            ],
            "blend_cells": [
                {
                    "left": left,
                    "right": right,
                    "decision_correlation": "0.3",
                    "decision_status": "POSTERIOR_STRESS_PSD",
                }
                for left, right in pairs
            ],
            "implementation_cells": [
                {
                    "left": left,
                    "right": right,
                    "mapping_scope": "CENSUS_PROVIDER_LANE_ONLY",
                    "shared_providers": [],
                    "status": "AVAILABLE_PROVIDER_LANE_CONCENTRATION",
                    "unassessed_dimensions": ["INSTRUMENT", "VENUE"],
                }
                for left, right in pairs
            ],
            "central_case": {},
            "correlation_one": {"loss": "0.31"},
            "drawdown_matrices": {},
            "horizon_matrices": {},
            "matrices": {},
            "performance": {
                series_id: {"observed_end": end} for series_id in series_ids
            },
            "series_composition": {
                series_id: {"observed_end": end} for series_id in series_ids
            },
        }

    live = mode_payload()
    correlation = {
        "analytics_hash": "a" * 64,
        "analytics_schema_version": 2,
        "as_of_date": end,
        "builder_version": "1.6.0",
        "complete_through": end,
        "default_evidence_mode": "LIVE_ONLY",
        "dependence": live,
        "dependence_modes": {"LIVE_ONLY": live, "LIVE_PLUS_BACKTEST": mode_payload()},
        "source": {
            "artifact_id": "current-dependence/returns.csv",
            "sha256": "b" * 64,
            "size_bytes": 100,
        },
    }
    content = canonical_json_bytes(correlation)
    day = date.fromisoformat(end)
    manifest = GenerationManifest.build(
        as_of_date=day,
        adapter=AdapterIdentity("portfolio-correlation", "1.6.0"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic current dependence",
            fact_types=("dependence",),
            observed_start=date(2026, 8, 1),
            observed_end=day,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/correlation.json",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=day,
                schema_name="portfolio-correlation-analysis",
                schema_version="2",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(root, manifest)
    (manifest_path.parent / "correlation.json").write_bytes(content)
    return manifest_path.parent


def _ledger_catalog(
    *,
    strategy_id=FX_STRATEGY,
    finding_days=("2026-09-04", CUTOFF),
    findings=True,
    fx_generation_id=FX_GENERATION_ID,
):
    """A provider-ledger catalog whose native NAV days after ``FX_END`` are findings.

    ``findings=False`` drops the ``quality_findings`` key, like the synthetic ledger of
    the MVP-2 product world: a ledger that records none bounds nothing.
    """

    catalog = {
        "nav_observations": [
            {
                "account_id": "account-fx",
                "as_of_date": day,
                "mapping_status": "MAPPED",
                "provider_id": "matrixport",
                "strategy_id": strategy_id,
            }
            for day in ("2026-09-02", FX_END)
        ],
        "portfolio_fx_generation": {"generation_id": fx_generation_id},
    }
    if findings:
        catalog["quality_findings"] = [
            {
                "fact_type": "nav_observations",
                "fx_boundary": FX_END,
                "original_source_date": day,
                "provider_id": "matrixport",
                "status": "OUTSIDE_ADMITTED_FX_BOUNDARY",
            }
            for day in finding_days
        ]
    return catalog


def _ledger_generation(root, **catalog_options):
    """Stage a verified provider-ledger generation in the governed route's layout."""

    catalog_bytes = canonical_json_bytes(_ledger_catalog(**catalog_options))
    receipt_bytes = canonical_json_bytes({"synthetic": "admission receipt"})
    day = date(2026, 9, 17)
    manifest = GenerationManifest.build(
        as_of_date=day,
        adapter=AdapterIdentity("quant-engine-provider-ledger", "5"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("nav_observations",),
            observed_start=date(2026, 8, 1),
            observed_end=day,
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=f"normalized/{name}",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=day,
                schema_name=schema_name,
                schema_version=schema_version,
            )
            for name, content, schema_name, schema_version in (
                (
                    "admit-or-declare-receipt.json",
                    receipt_bytes,
                    "provider-admission-receipt",
                    "1",
                ),
                (
                    "provider-ledger.json",
                    catalog_bytes,
                    "provider-ledger-catalog",
                    "2",
                ),
            )
        ),
        verification=VerificationStatus.VERIFIED,
    )
    generation_path = stage_generation(root, manifest)
    (generation_path.parent / "admit-or-declare-receipt.json").write_bytes(
        receipt_bytes
    )
    catalog_path = generation_path.parent / "provider-ledger.json"
    catalog_path.write_bytes(catalog_bytes)
    return generation_path, catalog_path


def _bundle_ledger_generation(
    root,
    *,
    aliases=("provider-ledger.json",),
    second_catalog=None,
    with_catalog=True,
    **catalog_options,
):
    """Stage a provider-ledger generation as a bundle, as the MVP-2 product world does.

    The manifest lists the catalog as ``normalized/provider-ledger.json`` and, for each
    alias, once more under another id with the same bytes (the synthetic ledger of the
    MVP-2 product world carries ``provider-ledger.json`` that way).  ``second_catalog``
    adds a DIFFERENT catalog under a third id; ``with_catalog=False`` stages a
    generation that holds no catalog at all.
    """

    day = date(2026, 9, 17)
    catalog_bytes = canonical_json_bytes(_ledger_catalog(**catalog_options))
    catalogs = {}
    if with_catalog:
        catalogs["normalized/provider-ledger.json"] = catalog_bytes
        catalogs.update({alias: catalog_bytes for alias in aliases})
    if second_catalog is not None:
        catalogs["second/provider-ledger.json"] = canonical_json_bytes(second_catalog)
    payloads = {
        **catalogs,
        "verification/independent-verification.json": canonical_json_bytes(
            {
                "schema_version": 1,
                "status": "VERIFIED",
                "subject_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            }
        ),
    }
    manifest = GenerationManifest.build(
        as_of_date=day,
        adapter=AdapterIdentity("quant-engine-provider-ledger", "SYNTHETIC"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic provider ledger",
            fact_types=("nav_observations",),
            observed_start=date(2026, 8, 1),
            observed_end=day,
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                as_of_date=day,
                schema_name=(
                    "synthetic-source-independent-verification"
                    if artifact_id.startswith("verification/")
                    else "provider-ledger-catalog"
                ),
                schema_version="1",
            )
            for artifact_id, content in payloads.items()
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return stage_generation_bundle(root, manifest, payloads) / "generation.json"


def _node_with_files(node_id, root_id, directory, files):
    expectations = []
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        expectations.append(
            pipeline.FileExpectation(
                name, hashlib.sha256(content).hexdigest(), len(content)
            )
        )
    return pipeline.GenerationNode(
        node_id, (), root_id, "a" * 64, "b" * 64, tuple(expectations), directory, False
    )


def _consumer_world(
    tmp_path,
    monkeypatch,
    *,
    end,
    ledger=None,
    ledger_bundle=None,
    with_ledger=True,
    cutoff=True,
    report_edit=None,
):
    """One report, its correlation generation and the selected ledger, ending at ``end``.

    The report and the correlation both end at ``end`` (the report's history contract
    says so too); ``ledger`` holds the options of the staged provider-ledger catalog and
    ``ledger_bundle`` (options of ``_bundle_ledger_generation``) stages the generation as
    a bundle instead of the governed route's layout.
    ``_verify_consumers``, the dependence re-read (``_dependence_report``), the FXB-1
    derivation, the product controls and the ledger and contract reads are the real code;
    only the seams that verify other outcomes (the governed HWM projection, the epoch
    crosswalk and manifest validation, the panel projections and the decomposition
    consumers) are replaced by constants that agree with the synthetic report.
    """

    report_producer = importlib.import_module("portfolio_report")
    panel_producer = importlib.import_module("portfolio_control_panel")
    contract_path = _fx_contract_file(tmp_path / "contract")
    contract = json.loads(contract_path.read_bytes())
    baseline = {
        "strategies": [
            {"strategy_id": row["strategy_id"]} for row in contract["strategies"]
        ]
    }
    correlation_dir = _correlation_generation(tmp_path / "correlation", end)
    projection, rows, _identity, _path = report_producer._dependence_report(
        None,
        generation_dir=correlation_dir,
        calculation_cutoff_date=CUTOFF if cutoff else None,
        bounded_complete_through=end if cutoff and end != CUTOFF else None,
    )
    capital = {"required_epochs": []}
    epoch_manifest = {"rows": []}
    report = {
        "dependence_analysis": projection,
        "dependence_rows": rows,
        "hwm_governed_proof": HWM_MARKER,
        "invested_capital": capital,
        "strategy_history_contract": {"as_of_date": end},
    }
    if cutoff:
        report["calculation_cutoff_date"] = CUTOFF
    if report_edit is not None:
        report_edit(report)
    report_bytes = canonical_json_bytes(report)
    report_generation = _node_with_files(
        "report-generation",
        "report",
        tmp_path / "report",
        {
            "portfolio-report-data.json": report_bytes,
            "account-strategy-manifest.json": canonical_json_bytes(epoch_manifest),
            "account-strategy-current-epoch-crosswalk.json": canonical_json_bytes({}),
        },
    )
    baseline_generation = _node_with_files(
        "baseline-generation",
        "baseline",
        tmp_path / "baseline",
        {"portfolio-baseline.json": canonical_json_bytes(baseline)},
    )
    panel_generation = _node_with_files(
        "panel-generation",
        "control-panel",
        tmp_path / "panel",
        {"downloads/portfolio-report-data.json": report_bytes},
    )
    correlation_generation = pipeline.GenerationNode(
        "correlation-generation",
        (),
        "correlation",
        correlation_dir.name,
        "b" * 64,
        (),
        correlation_dir,
        False,
    )
    correlation_path = correlation_dir / "correlation.json"
    correlation_node = pipeline.ArtifactNode(
        "financial-outcome-dependence-live-only",
        ("correlation-generation",),
        correlation_path,
        hashlib.sha256(correlation_path.read_bytes()).hexdigest(),
        correlation_path.stat().st_size,
    )
    spec = SimpleNamespace(nodes=(report_generation, correlation_generation))
    epochs = {
        "dashboard": {
            "invested_capital": capital,
            "hwm_governed_proof": HWM_MARKER,
            "dependence_modes": {
                "LIVE_ONLY": PANEL_MARKER,
                "LIVE_PLUS_BACKTEST": PANEL_MARKER,
            },
        },
        "manifest": epoch_manifest,
        "baseline_generation": baseline_generation,
        "panel_generation": panel_generation,
    }
    hwm_bytes = {
        key: b"{}"
        for key in (
            "governed_result",
            "input_generation",
            "calendar_authority",
            "provider_admission_receipt",
        )
    }
    ledger_path = None
    catalog_path = None
    if with_ledger and ledger_bundle is not None:
        ledger_path = _bundle_ledger_generation(
            tmp_path / "ledger", **ledger_bundle, **(ledger or {})
        )
    elif with_ledger:
        ledger_path, catalog_path = _ledger_generation(
            tmp_path / "ledger", **(ledger or {})
        )
    monkeypatch.setattr(
        outcomes,
        "selected_hwm_source_paths",
        lambda _spec: (
            {"provider_ledger_generation_path": ledger_path}
            if ledger_path is not None
            else {}
        ),
    )
    monkeypatch.setattr(
        report_producer, "_report_governed_hwm_proof", lambda *a, **k: HWM_MARKER
    )
    monkeypatch.setattr(
        report_producer, "_validate_current_epoch_crosswalk", lambda *a, **k: None
    )
    monkeypatch.setattr(
        report_producer, "_validate_report_account_manifest", lambda *a, **k: set()
    )
    monkeypatch.setattr(
        panel_producer,
        "_current_product_correlation_projection",
        lambda *a, **k: {
            "dependence_modes": {"LIVE_ONLY": {}, "LIVE_PLUS_BACKTEST": {}}
        },
    )
    monkeypatch.setattr(
        panel_producer, "_dependence_analysis", lambda *a, **k: PANEL_MARKER
    )
    monkeypatch.setattr(outcomes, "_verify_decomposition_consumers", lambda *a, **k: {})

    def call():
        return outcomes._verify_consumers(
            spec,
            contract_path,
            epochs,
            capital,
            hwm_bytes,
            {"financial-outcome-dependence-live-only": correlation_node},
            {("DEPENDENCE", correlation_node.path.resolve()): {}},
            {},
            {},
            {},
            {},
        )

    return SimpleNamespace(
        call=call,
        report=report,
        report_bytes=report_bytes,
        report_error=report_producer.ReportValidationError,
        ledger_catalog_path=catalog_path,
    )


# A ledger that records no finding the FXB-1 rule can use for a REQUIRED strategy: an
# empty list, no `quality_findings` at all (the synthetic ledger of the MVP-2 product
# world), and findings that bound a strategy the product excludes.
_NO_REQUIRED_BOUND = {
    "an-empty-findings-list": {"finding_days": ()},
    "no-findings-key": {"findings": False},
    "findings-of-an-excluded-strategy": {"strategy_id": EXCLUDED_STRATEGY},
}


def test_fx_bounded_dependence_end_is_accepted_at_the_verified_bound(
    tmp_path, monkeypatch
):
    """The real FX-bounded shape: findings after F, report and correlation end at F.

    Fail-before: the release path re-read the dependence strictly, so a report that
    publishes the true end 2026-09-03 failed with ``dependence calculation cutoff
    differs`` and blocked publication of the corrected release.
    """

    world = _consumer_world(tmp_path, monkeypatch, end=FX_END)

    result = world.call()

    assert world.report["calculation_cutoff_date"] == CUTOFF
    assert world.report["dependence_analysis"]["complete_through"] == FX_END
    assert result["report_sha256"] == hashlib.sha256(world.report_bytes).hexdigest()


@pytest.mark.parametrize(
    "end", [FX_END, "2026-08-27"], ids=["at-the-contract-fx-end", "a-week-earlier"]
)
@pytest.mark.parametrize(
    "ledger", list(_NO_REQUIRED_BOUND.values()), ids=list(_NO_REQUIRED_BOUND)
)
def test_a_report_and_correlation_that_both_end_early_without_a_qualifying_finding_fail(
    tmp_path, monkeypatch, ledger, end
):
    """The bound comes from the ledger and the contract, never from the report.

    The report itself claims the early end, in its history contract and in its published
    dependence, and the correlation agrees: a bound taken from the report would accept
    any early end whatever the ledger says.
    """

    world = _consumer_world(tmp_path, monkeypatch, end=end, ledger=ledger)

    assert world.report["strategy_history_contract"]["as_of_date"] == end
    assert world.report["dependence_analysis"]["complete_through"] == end
    with pytest.raises(
        world.report_error, match="dependence calculation cutoff differs"
    ):
        world.call()


@pytest.mark.parametrize(
    "ledger", list(_NO_REQUIRED_BOUND.values()), ids=list(_NO_REQUIRED_BOUND)
)
def test_the_strict_cutoff_equality_is_unchanged_when_no_required_strategy_is_bounded(
    tmp_path, monkeypatch, ledger
):
    """A dependence that ends at the cutoff is accepted, bound or no bound.

    A bound that is not on a REQUIRED strategy does not shorten the dependence, which
    spans the required strategies only.
    """

    world = _consumer_world(tmp_path, monkeypatch, end=CUTOFF, ledger=ledger)

    result = world.call()

    assert world.report["dependence_analysis"]["complete_through"] == CUTOFF
    assert result["report_sha256"] == hashlib.sha256(world.report_bytes).hexdigest()


@pytest.mark.parametrize(
    "end",
    ["2026-09-02", "2026-09-04", "2026-09-16", CUTOFF],
    ids=["a-day-before", "a-day-after", "the-day-before-the-cutoff", "the-cutoff"],
)
def test_a_published_end_that_differs_from_the_verified_bound_fails_closed(
    tmp_path, monkeypatch, end
):
    """The verified bound is exactly F: one day either side, or the cutoff, raises.

    The ledger bounds the required strategy at 2026-09-03, so a composite that ends at
    the cutoff was not truncated where the value basis ends.
    """

    world = _consumer_world(tmp_path, monkeypatch, end=end)

    with pytest.raises(
        world.report_error, match="dependence calculation cutoff differs"
    ):
        world.call()


def test_a_ledger_the_contract_does_not_support_fails_closed_as_a_financial_outcome(
    tmp_path, monkeypatch
):
    """A boundary finding under an FX generation the contract does not pin."""

    world = _consumer_world(
        tmp_path, monkeypatch, end=FX_END, ledger={"fx_generation_id": "f" * 64}
    )

    with pytest.raises(
        outcomes.FinancialOutcomeError, match="value-basis bound differs"
    ):
        world.call()


def test_the_ledger_catalog_must_match_its_generation_manifest(tmp_path, monkeypatch):
    """The catalog is read through the selected generation's manifest identity.

    The staged catalog records no finding.  Swapping in one that does would bound the
    dependence at F, but the manifest binds the other bytes.
    """

    world = _consumer_world(
        tmp_path, monkeypatch, end=FX_END, ledger={"finding_days": ()}
    )
    world.ledger_catalog_path.write_bytes(canonical_json_bytes(_ledger_catalog()))

    with pytest.raises(outcomes.FinancialOutcomeError, match="identity differs"):
        world.call()


def test_the_published_dependence_must_state_the_true_bounded_end(
    tmp_path, monkeypatch
):
    """A report that claims the cutoff for a composite that ends at F is rejected."""

    world = _consumer_world(
        tmp_path,
        monkeypatch,
        end=FX_END,
        report_edit=lambda report: report["dependence_analysis"].update(
            complete_through=CUTOFF
        ),
    )

    with pytest.raises(
        outcomes.FinancialOutcomeError,
        match="report dependence values differ from verified result",
    ):
        world.call()


def test_a_report_without_a_calculation_cutoff_keeps_the_unbounded_read(
    tmp_path, monkeypatch
):
    """No cutoff, nothing to bound: the ledger is not consulted and the read is as before."""

    world = _consumer_world(
        tmp_path, monkeypatch, end=CUTOFF, with_ledger=False, cutoff=False
    )

    assert "calculation_cutoff_date" not in world.report
    assert (
        world.call()["report_sha256"] == hashlib.sha256(world.report_bytes).hexdigest()
    )


@pytest.mark.parametrize(
    "cutoff",
    ["20260917", "2026-9-17", 20260917],
    ids=["basic-format", "unpadded", "an-integer"],
)
def test_a_report_cutoff_that_is_not_a_canonical_date_fails_closed(
    tmp_path, monkeypatch, cutoff
):
    """The bound is derived for one exact calendar date, never a looser spelling of it."""

    world = _consumer_world(
        tmp_path,
        monkeypatch,
        end=CUTOFF,
        report_edit=lambda report: report.update(calculation_cutoff_date=cutoff),
    )

    with pytest.raises(outcomes.FinancialOutcomeError, match="not an ISO date"):
        world.call()


def test_a_catalog_listed_under_two_ids_of_one_generation_is_one_catalog(
    tmp_path, monkeypatch
):
    """The MVP-2 product world's ledger generation lists its catalog twice.

    Fail-before: the selected generation is a bundle whose manifest carries the catalog as
    ``normalized/provider-ledger.json`` and again as ``provider-ledger.json`` (the same
    bytes).  Counting the artifacts of the catalog schema called that ambiguous and the
    172-outcome producer-to-consumer test failed with ``selected provider ledger catalog
    is absent or ambiguous``.  Two ids of the same bytes are one catalog.
    """

    world = _consumer_world(tmp_path, monkeypatch, end=FX_END, ledger_bundle={})

    result = world.call()

    assert result["report_sha256"] == hashlib.sha256(world.report_bytes).hexdigest()


def test_a_bundled_ledger_generation_without_an_alias_is_read(tmp_path, monkeypatch):
    world = _consumer_world(
        tmp_path, monkeypatch, end=FX_END, ledger_bundle={"aliases": ()}
    )

    assert (
        world.call()["report_sha256"] == hashlib.sha256(world.report_bytes).hexdigest()
    )


def test_two_different_catalogs_in_one_generation_are_ambiguous(tmp_path, monkeypatch):
    """Aliases carry the same bytes; a second catalog with other bytes is a conflict."""

    world = _consumer_world(
        tmp_path,
        monkeypatch,
        end=FX_END,
        ledger_bundle={"second_catalog": _ledger_catalog(finding_days=())},
    )

    with pytest.raises(
        outcomes.FinancialOutcomeError, match="catalog is absent or ambiguous"
    ):
        world.call()


def test_a_ledger_generation_without_a_catalog_is_absent(tmp_path, monkeypatch):
    world = _consumer_world(
        tmp_path, monkeypatch, end=FX_END, ledger_bundle={"with_catalog": False}
    )

    with pytest.raises(
        outcomes.FinancialOutcomeError, match="catalog is absent or ambiguous"
    ):
        world.call()

"""The final product requires financial artifacts, not generic root names."""

from __future__ import annotations

from datetime import date
import ast
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
from quant_engine.core.publication import stage_generation_bundle


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


def test_replay_refuses_a_partially_bound_authority_set(tmp_path):
    """Half a set is the one case that could silently verify the wrong shape."""

    partial = list(outcomes._HISTORY_AUTHORITY_NODES.values())[:-1]
    with pytest.raises(outcomes.FinancialOutcomeError, match="partially bound"):
        outcomes._history_authorities(_authority_spec(tmp_path, partial), tmp_path)


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

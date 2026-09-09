"""Finite financial-MVP outcome bindings shared by semantic and consumer checks.

Contract schema 3 remains unchanged. Existing ArtifactNodes carry the actual
financial outputs; existing GenerationNodes carry their immutable containers.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from itertools import product
import json
from pathlib import Path
from typing import Any, cast

from quant_engine.core.pipeline import ArtifactNode, GenerationNode
from quant_engine.core import GenerationManifest, VerificationStatus
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.publication import (
    GenerationConflictError,
    read_generation_bundle,
)


class FinancialOutcomeError(ValueError):
    """The candidate does not establish every required financial outcome."""


def _decomposition_plan() -> dict[str, Any]:
    from portfolio_financial_mvp_contract import REPOSITORY_ROOT

    return cast(
        dict[str, Any],
        json.loads(
            (
                REPOSITORY_ROOT
                / "docs/analytics/current-strategy-decomposition-plan.json"
            ).read_bytes()
        ),
    )


def required_outcome_roles(
    contract: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None
) -> dict[str, tuple[str, ...]]:
    """Derive the finite role set from the accepted product, never readiness."""
    strategies = sorted(
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    )
    definitions = {
        row["outcome_id"]: row
        for row in contract["financial_outcomes"]
        if row["product_requirement"] == "REQUIRED"
    }
    if set(definitions) != {
        "AS_HELD_COMPOSITE_HWM",
        "INVESTED_CAPITAL",
        "DECOMPOSITION",
        "DEPENDENCE",
    }:
        raise FinancialOutcomeError("required financial outcome contract differs")
    result: dict[str, tuple[str, ...]] = {
        "financial-outcome-hwm": ("AS_HELD_COMPOSITE_HWM",),
        "financial-outcome-capital": ("INVESTED_CAPITAL",),
    }
    decomposition = definitions["DECOMPOSITION"]["calculation_contract"]
    plan = _decomposition_plan() if plan is None else plan
    rows = {row["strategy_id"]: row for row in plan["strategies"]}
    for strategy in strategies:
        for source_key, disposition in rows[strategy]["source_dispositions"].items():
            if disposition["requirement"] != "REQUIRED":
                continue
            lens = plan["sources"][source_key]["lens"]
            for surface, mode in product(
                decomposition["surfaces"], decomposition["evidence_modes"]
            ):
                key = "financial-outcome-decomposition-" + "-".join(
                    (strategy, source_key, surface, mode)
                ).lower().replace("_", "-")
                result[key] = (
                    "DECOMPOSITION",
                    strategy,
                    source_key,
                    lens,
                    surface,
                    mode,
                )
    for mode in definitions["DEPENDENCE"]["calculation_contract"]["evidence_modes"]:
        result["financial-outcome-dependence-" + mode.lower().replace("_", "-")] = (
            "DEPENDENCE",
            mode,
        )
    return result


def _verified_file(path: Path, digest: str, size: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FinancialOutcomeError(
            f"financial artifact is absent or linked: {path.name}"
        )
    content = path.read_bytes()
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise FinancialOutcomeError(f"financial artifact identity differs: {path.name}")
    return content


def verify_outcome_inventory(
    spec: Any, contract: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None
) -> dict[str, ArtifactNode]:
    """Verify exact finite roles and containment in already selected generations.

    This is only the file boundary. Numerical completion additionally delegates
    to the HWM/capital/model/epoch producers below; hashes alone never PASS it.
    """
    required = required_outcome_roles(contract, plan=plan)
    selected: dict[str, ArtifactNode] = {}
    for node in spec.nodes:
        if not node.node_id.startswith("financial-outcome-"):
            continue
        if node.node_id not in required:
            raise FinancialOutcomeError(f"unknown financial outcome: {node.node_id}")
        if node.node_id in selected:
            raise FinancialOutcomeError(f"duplicate financial outcome: {node.node_id}")
        if not isinstance(node, ArtifactNode):
            raise FinancialOutcomeError("financial outcome must be an ArtifactNode")
        selected[node.node_id] = node
    if set(selected) != set(required):
        raise FinancialOutcomeError(
            "missing required financial outcome: "
            + ", ".join(sorted(set(required) - set(selected)))
        )
    generations = {
        node.node_id: node for node in spec.nodes if isinstance(node, GenerationNode)
    }
    for node_id, node in selected.items():
        _verified_file(node.path, node.sha256, node.size_bytes)
        if required[node_id][0] == "AS_HELD_COMPOSITE_HWM":
            continue
        matches = []
        for dependency in node.depends_on:
            generation = generations.get(dependency)
            if generation is None or generation.source_dir is None:
                continue
            for file in generation.files:
                if (
                    (generation.source_dir / file.relative_path).resolve()
                    == node.path.resolve()
                    and file.sha256 == node.sha256
                    and file.size_bytes == node.size_bytes
                ):
                    matches.append(generation.node_id)
        if len(matches) != 1:
            raise FinancialOutcomeError(
                f"financial outcome generation binding differs: {node_id}"
            )
    return selected


def _gate(spec: Any, gate_id: str) -> tuple[Path, dict[str, Any]]:
    matches = [gate for gate in spec.gates if gate.gate_id == gate_id]
    if len(matches) != 1:
        raise FinancialOutcomeError(f"required financial gate differs: {gate_id}")
    gate = matches[0]
    content = _verified_file(gate.path, gate.sha256, gate.path.stat().st_size)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise FinancialOutcomeError(f"financial gate is not an object: {gate_id}")
    return gate.path, value


def _artifact_for_digest(
    spec: Any,
    digest: str,
    size: int,
    *,
    source_root: Path | None = None,
    relative_path: str | None = None,
) -> ArtifactNode:
    root = None if source_root is None else source_root.resolve()
    relative = None if relative_path is None else Path(relative_path)
    matches = {
        node.path.resolve(): node
        for node in spec.nodes
        if isinstance(node, ArtifactNode)
        and node.sha256 == digest
        and node.size_bytes == size
        and (root is None or node.path.resolve().is_relative_to(root))
        and (
            relative is None
            or (
                node.path.resolve() == (root / relative).resolve()
                if root is not None
                else node.path.parts[-len(relative.parts) :] == relative.parts
            )
        )
    }
    if len(matches) != 1:
        raise FinancialOutcomeError(
            "financial producer/source artifact binding is absent or ambiguous"
        )
    node = next(iter(matches.values()))
    _verified_file(node.path, node.sha256, node.size_bytes)
    return node


def _generation(spec: Any, artifact: ArtifactNode) -> GenerationNode:
    matches = [
        node
        for node in spec.nodes
        if isinstance(node, GenerationNode) and node.node_id in artifact.depends_on
    ]
    if len(matches) != 1 or matches[0].source_dir is None:
        raise FinancialOutcomeError("financial generation is absent or ambiguous")
    return matches[0]


def _generation_file(generation: GenerationNode, filename: str) -> tuple[Path, bytes]:
    if generation.source_dir is None:
        raise FinancialOutcomeError("financial consumer generation is absent")
    matches = [file for file in generation.files if file.relative_path == filename]
    if not matches:
        matches = [
            file
            for file in generation.files
            if Path(file.relative_path).name == filename
        ]
    if len(matches) != 1:
        raise FinancialOutcomeError(
            f"financial consumer file is absent or ambiguous: {filename}"
        )
    file = matches[0]
    path = generation.source_dir / file.relative_path
    return path, _verified_file(path, file.sha256, file.size_bytes)


def _selected_epoch_assignment_keys(
    spec: Any,
) -> tuple[tuple[tuple[str, str, str, str | None], ...], Path, GenerationManifest]:
    """Load the manifest-bound product epoch perimeter from account generation."""

    accounts = [
        node
        for node in spec.nodes
        if isinstance(node, ArtifactNode)
        and node.node_id == "financial-account-manifest-generation"
    ]
    if len(accounts) != 1 or accounts[0].path.name != "generation.json":
        raise FinancialOutcomeError("selected account generation binding differs")
    account = accounts[0]
    manifest = GenerationManifest.from_json_bytes(
        _verified_file(account.path, account.sha256, account.size_bytes)
    )
    generation_dir = account.path.parent
    if manifest.generation_id != generation_dir.name:
        raise FinancialOutcomeError("selected account generation identity differs")
    _path, content = _manifest_artifact(
        generation_dir,
        manifest,
        "authority/account-assignment-crosswalk.json",
    )
    crosswalk = json.loads(content)
    if (
        not isinstance(crosswalk, dict)
        or crosswalk.get("schema_version") != 1
        or crosswalk.get("artifact_id") != "account-strategy-current-epoch-crosswalk"
        or not isinstance(crosswalk.get("rows"), list)
    ):
        raise FinancialOutcomeError("selected account epoch perimeter is invalid")
    keys: list[tuple[str, str, str, str | None]] = []
    for row in crosswalk["rows"]:
        if not isinstance(row, dict):
            raise FinancialOutcomeError("selected account epoch perimeter is invalid")
        if row.get("product_epoch_disposition") != "INCLUDED_PRODUCT_EPOCH":
            continue
        values = (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        if any(not isinstance(value, str) or not value for value in values[:3]) or (
            values[3] is not None and not isinstance(values[3], str)
        ):
            raise FinancialOutcomeError("selected account epoch perimeter is invalid")
        keys.append(cast(tuple[str, str, str, str | None], values))
    if not keys or len(keys) != len(set(keys)):
        raise FinancialOutcomeError("selected account epoch perimeter is invalid")
    return tuple(keys), generation_dir, manifest


def _manifest_artifact(
    generation_dir: Path,
    manifest: GenerationManifest,
    artifact_id: str,
) -> tuple[Path, bytes]:
    """Read one exact generation artifact through its manifest identity."""

    matches = [row for row in manifest.artifacts if row.artifact_id == artifact_id]
    if len(matches) != 1:
        raise FinancialOutcomeError(
            f"selected account generation artifact differs: {artifact_id}"
        )
    definition = matches[0]
    root = generation_dir.resolve()
    path = (generation_dir / artifact_id).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FinancialOutcomeError(
            f"selected account generation artifact escapes: {artifact_id}"
        ) from exc
    return path, _verified_file(path, definition.sha256, definition.size_bytes)


def selected_hwm_source_paths(spec: Any) -> dict[str, Path]:
    """Resolve the already selected account, provider and baseline authorities."""
    from portfolio_pipeline_materialize import ACCOUNT_STRATEGY_CLASSIFICATION_POLICY

    account = [
        node
        for node in spec.nodes
        if isinstance(node, ArtifactNode)
        and node.node_id == "financial-account-manifest-generation"
    ]
    if len(account) != 1 or account[0].path.name != "generation.json":
        raise FinancialOutcomeError("selected HWM account generation binding differs")
    _verified_file(account[0].path, account[0].sha256, account[0].size_bytes)
    result = {"account_manifest_generation_path": account[0].path}
    for key, roots in (
        ("provider_ledger_generation_path", {"provider-ledger", "ledger"}),
        ("baseline_generation_path", {"portfolio-baseline", "baseline"}),
    ):
        candidates = [
            node
            for node in spec.nodes
            if isinstance(node, GenerationNode) and node.root_id in roots
        ]
        if len(candidates) != 1 or candidates[0].source_dir is None:
            raise FinancialOutcomeError(f"selected HWM source differs: {key}")
        generation = candidates[0]
        assert generation.source_dir is not None
        path = generation.source_dir / "generation.json"
        manifest = GenerationManifest.from_json_bytes(
            _verified_file(path, generation.manifest_sha256, path.stat().st_size)
        )
        if manifest.generation_id != generation.generation_id:
            raise FinancialOutcomeError("selected HWM source generation differs")
        result[key] = path
    _, epoch_gate = _gate(spec, "financial-epoch-manifest-input-authority")
    policy = epoch_gate["inputs"]["account_strategy_admission_policy"]
    path = Path(policy["path"])
    _verified_file(path, policy["sha256"], policy["size_bytes"])
    result["account_manifest_admission_policy_path"] = path
    result["account_manifest_flow_classification_policy_path"] = (
        ACCOUNT_STRATEGY_CLASSIFICATION_POLICY
    )
    return result


def verify_selected_capital_ledger(
    input_generation_path: Path, provider_ledger_generation_path: Path
) -> Path:
    """Bind capital's provider ledger to the generation this candidate selected.

    The capital input generation names its own provider-ledger file, and every
    ledger generation built under one contract satisfies the same admission
    receipt. Without this check an older ledger could validate while the
    candidate selected a newer one. Containment is checked rather than the
    parent name, because the ledger sits directly in the generation directory
    on the governed route and under a normalized subdirectory elsewhere.
    """
    inputs = json.loads(input_generation_path.read_bytes())
    bindings = inputs.get("source_bindings")
    ledger = bindings.get("provider_ledger") if isinstance(bindings, Mapping) else None
    if not isinstance(ledger, Mapping) or not isinstance(ledger.get("path"), str):
        raise FinancialOutcomeError("capital provider ledger binding is absent")
    selected_root = provider_ledger_generation_path.resolve().parent
    if not Path(ledger["path"]).resolve().is_relative_to(selected_root):
        raise FinancialOutcomeError(
            "capital provider ledger differs from the selected generation"
        )
    return selected_root


def verify_selected_hwm_generation(
    source_paths: Mapping[str, Path],
    *,
    contract_path: Path,
    bindings: Mapping[str, Path],
    input_contract_path: Path | None = None,
    policy_bytes: bytes | None = None,
) -> set[str]:
    """Rebuild schema-1 HWM observations from exact account source bytes.

    This account-NAV route verifies native USD and canonical account/strategy
    identities directly. It does not consume security prices or a foreign-FX
    series. Their frozen generic prerequisites cannot create a dependency cycle.

    The replay must use the authority the observations were actually built
    under. When the provider ledger was ADMITTED under a retained input
    contract, that contract and the policy retargeted onto it are supplied
    here; the product contract and repository policy would rebuild different
    bytes and reject a correct generation. With neither supplied this is the
    product contract and the repository policy, exactly as before.
    """
    from portfolio_hwm_selected_inputs import build_selected_hwm_inputs
    from portfolio_pipeline_materialize import HWM_GOVERNED_POLICY

    rebuilt = build_selected_hwm_inputs(
        **source_paths,
        financial_contract_path=(
            contract_path if input_contract_path is None else input_contract_path
        ),
        calendar_authority_path=bindings["calendar_authority"],
        provider_admission_path=bindings["provider_admission_receipt"],
        hwm_policy_path=HWM_GOVERNED_POLICY,
        hwm_policy_bytes=policy_bytes,
    )
    if rebuilt != json.loads(bindings["input_generation"].read_bytes()):
        raise FinancialOutcomeError(
            "HWM input generation differs from selected account-source replay"
        )
    return {"portfolio-calendars", "portfolio-fx-rates", "portfolio-security-master"}


def _replay_model(
    spec: Any, artifact: ArtifactNode, kind: str, *, source_root: Path | None = None
) -> dict[str, Any]:
    """Delegate numerical replay to the existing domain producer, once per file."""
    generation = _generation(spec, artifact)
    assert generation.source_dir is not None
    manifest_path = generation.source_dir / "generation.json"
    manifest = GenerationManifest.from_json_bytes(
        _verified_file(
            manifest_path, generation.manifest_sha256, manifest_path.stat().st_size
        )
    )
    if manifest.generation_id != generation.generation_id:
        raise FinancialOutcomeError("financial generation manifest identity differs")
    spec_id = (
        "spec/decomposition-spec.json"
        if kind == "DECOMPOSITION"
        else "spec/correlation-spec.json"
    )
    definitions = [row for row in manifest.artifacts if row.artifact_id == spec_id]
    if len(definitions) != 1:
        raise FinancialOutcomeError(
            "financial producer specification is not manifest-bound"
        )
    definition = definitions[0]
    specification = _artifact_for_digest(
        spec, definition.sha256, definition.size_bytes, source_root=source_root
    )
    import portfolio_decomposition
    import portfolio_correlation

    source_definitions: list[
        portfolio_decomposition.SourceDefinition
        | portfolio_correlation.SourceDefinition
    ]
    if kind == "DECOMPOSITION":
        parsed = portfolio_decomposition._load_spec(specification.path)
        source_definitions = [
            parsed.target_source,
            parsed.factor_source,
            *parsed.provenance_sources,
        ]
        build = portfolio_decomposition.build_decomposition
    else:
        correlation_spec = portfolio_correlation._load_spec(specification.path)
        source_definitions = [correlation_spec.source]
        build = portfolio_correlation.build_correlation
    roots = set()
    for source in source_definitions:
        node = _artifact_for_digest(
            spec,
            source.expected_sha256,
            source.expected_size_bytes,
            source_root=source_root,
            relative_path=source.relative_path,
        )
        parts = Path(source.relative_path).parts
        root = node.path.resolve().parents[len(parts) - 1]
        if (root / source.relative_path).resolve() != node.path.resolve():
            raise FinancialOutcomeError(
                "financial source path differs from producer specification"
            )
        roots.add(root)
    if len(roots) != 1:
        raise FinancialOutcomeError("financial producer source roots conflict")
    result, rebuilt_manifest, result_bytes = build(
        specification.path, next(iter(roots))
    )
    if (
        rebuilt_manifest.to_json_bytes() != manifest.to_json_bytes()
        or result_bytes != artifact.path.read_bytes()
    ):
        raise FinancialOutcomeError(
            "financial model differs from selected-source recomputation"
        )
    return result


def _required_series(
    contract: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None
) -> dict[str, str]:
    # load_contract independently validates this existing, hash-bound plan as
    # the current registry/applicability authority. No new registry is created.
    plan = _decomposition_plan() if plan is None else plan
    required = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    }
    mapping = {
        row["strategy_id"]: row["series_id"]
        for row in plan["strategies"]
        if row["strategy_id"] in required
    }
    if set(mapping) != required or len(set(mapping.values())) != len(required):
        raise FinancialOutcomeError(
            "required financial strategy-series mapping differs"
        )
    return mapping


def _bound_decomposition_plan(
    spec: Any, contract: Mapping[str, Any], mode: str, *, allow_synthetic: bool
) -> Path:
    """Resolve the existing plan bundle and verify its accepted input bytes."""
    import portfolio_current_decomposition_plan as producer

    role = "financial-decomposition-plan-" + mode.lower().replace("_", "-")
    matches = [
        node
        for node in spec.nodes
        if isinstance(node, ArtifactNode) and node.node_id == role
    ]
    if len(matches) != 1 or matches[0].path.name != "plan-manifest.json":
        raise FinancialOutcomeError(f"decomposition plan binding differs: {mode}")
    node = matches[0]
    manifest = json.loads(_verified_file(node.path, node.sha256, node.size_bytes))
    bundle = node.path.parent
    identity = dict(manifest)
    bundle_id = identity.pop("bundle_id", None)
    if (
        hashlib.sha256(producer._canonical_json(identity)).hexdigest() != bundle_id
        or bundle_id != bundle.name
        or manifest.get("policies", {}).get("strategy_evidence_mode") != mode
        or (not allow_synthetic and manifest.get("assurance_profile") != "RELEASE_QC")
    ):
        raise FinancialOutcomeError(f"decomposition plan identity differs: {mode}")
    files: dict[str, bytes] = {}
    for definition in [*manifest["inputs"], *manifest["outputs"]]:
        relative = definition["path"]
        path = producer._source_path(bundle, relative, "financial plan artifact")
        if relative in files:
            raise FinancialOutcomeError("decomposition plan artifact is duplicated")
        files[relative] = _verified_file(
            path, definition["sha256"], definition["size_bytes"]
        )
    plan_content = files["inputs/governance/current-strategy-decomposition-plan.json"]
    producer._validate_plan_authority(json.loads(plan_content), plan_content, contract)
    # Rehashing a hand-edited model specification must not make it the accepted
    # methodology. Rebuild the existing planner from its actual source bundle.
    history_nodes = [
        row
        for row in spec.nodes
        if isinstance(row, ArtifactNode)
        and row.node_id == "financial-layered-history-generation"
    ]
    if len(history_nodes) != 1:
        raise FinancialOutcomeError(
            "selected layered-history generation binding differs"
        )
    history = history_nodes[0]
    history_bytes = _verified_file(history.path, history.sha256, history.size_bytes)
    if history_bytes != files["inputs/history/generation.json"]:
        raise FinancialOutcomeError(
            "decomposition plan uses a different layered history"
        )
    base = json.loads(files["inputs/governance/base-decomposition-spec.json"])
    base_roots = set()
    for definition in base.get("provenance_sources", []):
        source = _artifact_for_digest(
            spec,
            definition["expected_sha256"],
            definition["expected_size_bytes"],
            relative_path=definition["relative_path"],
        )
        relative = Path(definition["relative_path"])
        root = source.path.resolve().parents[len(relative.parts) - 1]
        if (root / relative).resolve() != source.path.resolve():
            raise FinancialOutcomeError("decomposition base source path differs")
        base_roots.add(root)
    if len(base_roots) > 1:
        raise FinancialOutcomeError("decomposition base source roots conflict")
    outputs, rebuilt = producer.build_bundle(
        plan_path=bundle / "inputs/governance/current-strategy-decomposition-plan.json",
        catalog_path=bundle / "inputs/governance/decomposition-source-catalog.json",
        financial_mvp_contract_path=bundle
        / "inputs/history/financial-mvp-contract.json",
        layered_history_generation_path=history.path.parent,
        base_spec_path=bundle / "inputs/governance/base-decomposition-spec.json",
        base_source_root=next(iter(base_roots), bundle),
        market_paths={
            key: bundle / f"inputs/market/{key}.csv" for key in producer.INPUT_KEYS
        },
        public_factor_manifest_path=bundle
        / "inputs/governance/public-factor-source-manifest.json",
        style_manifest_path=bundle / "inputs/governance/style-source-manifest.json",
        sector_manifest_path=bundle / "inputs/governance/sector-source-manifest.json",
        evaluation_date=manifest["evaluation_date"],
        evidence_mode=mode,
        assurance_profile=manifest["assurance_profile"],
    )
    if rebuilt != manifest or outputs != files:
        raise FinancialOutcomeError(
            "decomposition plan differs from canonical source replay"
        )
    return bundle


def _verify_decomposition_consumers(
    spec: Any,
    contract: Mapping[str, Any],
    nodes: Mapping[str, ArtifactNode],
    replays: Mapping[tuple[str, Path], Mapping[str, Any]],
    series: Mapping[str, str],
    dashboard: Mapping[str, Any],
    plan_bindings: Mapping[tuple[str, str, str], Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare actual consumer cells with the existing numerical projection."""
    import portfolio_control_panel as producer

    projected: dict[tuple[str, Path], Mapping[str, Any]] = {}
    checked = {}
    modes = dashboard.get("decomposition_lenses", {}).get("modes", {})
    for node_id, key in required_outcome_roles(contract, plan=plan).items():
        if key[0] != "DECOMPOSITION":
            continue
        _, strategy, source_key, lens, surface, mode = key
        node = nodes[node_id]
        cache_key = ("DECOMPOSITION", node.path.resolve())
        if cache_key not in projected:
            generation = _generation(spec, node)
            value, names = producer._factor_decomposition_perimeter(
                replays[cache_key], set(series.values())
            )
            projected[cache_key] = producer._factor_analysis(
                value,
                {"generation_id": generation.generation_id, "sha256": node.sha256},
                names,
            )
        rows = [
            row
            for row in projected[cache_key]["series"]
            if row["series_id"] == series[strategy]
        ]
        lenses = [row for row in modes.get(mode, []) if row.get("lens_id") == lens]
        if len(rows) != 1 or len(lenses) != 1:
            raise FinancialOutcomeError(
                f"consumer decomposition cell absent: {node_id}"
            )
        surface_value = lenses[0].get("surfaces", {}).get(surface, {})
        displayed = [
            row
            for row in surface_value.get("series", [])
            if row.get("series_id") == series[strategy]
            and row.get("source_key") == source_key
        ]
        source_rows = [
            row
            for row in surface_value.get("sources", [])
            if row.get("source_key") == source_key
        ]
        expected = {
            **rows[0],
            "source_key": source_key,
            "source_label": producer.DECOMPOSITION_LENS_SOURCES[source_key][1],
            "surface": surface,
        }
        if (
            displayed != [expected]
            or len(source_rows) != 1
            or source_rows[0].get("source_sha256") != node.sha256
            or source_rows[0].get("generation_id")
            != _generation(spec, node).generation_id
            or source_rows[0].get("plan_spec")
            != plan_bindings[(source_key, surface, mode)]
        ):
            raise FinancialOutcomeError(f"panel decomposition values differ: {node_id}")
        checked[node_id] = hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return checked


def _rebuild_selected_epochs(
    spec: Any,
    baseline: GenerationNode,
    selected_bytes: Mapping[str, bytes],
) -> dict[str, Any]:
    """Verify the finalized account product against its selected source bytes."""

    from portfolio_hwm_selected_inputs import _verify_account_manifest_replay

    selected_keys, account_dir, account_manifest = _selected_epoch_assignment_keys(spec)
    _generation_file(baseline, "portfolio-baseline.json")
    _identity_path, identity_bytes = _manifest_artifact(
        account_dir, account_manifest, "authority/selected-input-identities.json"
    )
    source_admission = selected_bytes["account_strategy_admission_policy"]
    if json.loads(identity_bytes).get("admission_policy") != {
        "sha256": hashlib.sha256(source_admission).hexdigest(),
        "size_bytes": len(source_admission),
    }:
        raise FinancialOutcomeError("selected epoch admission policy identity differs")
    _calculation_path, calculation_bytes = _manifest_artifact(
        account_dir, account_manifest, "authority/calculation-inputs.json"
    )
    calculation = json.loads(calculation_bytes)
    for selected_key, projected_key in {
        "account_strategy_ledger": "calculation_ledger",
        "account_strategy_ledger_identity": "calculation_ledger_identity",
        "account_strategy_classified_flows": "calculation_classified_flows",
        "account_strategy_flow_coverage": "calculation_flow_coverage",
    }.items():
        if selected_bytes[selected_key] != canonical_json_bytes(
            calculation[projected_key]
        ):
            raise FinancialOutcomeError(
                f"selected epoch calculation input differs: {selected_key}"
            )
    try:
        bundle = read_generation_bundle(account_dir.parents[1], account_dir.name)
    except (OSError, ValueError, GenerationConflictError) as exc:
        raise FinancialOutcomeError(
            "selected epoch generation verification failed"
        ) from exc
    if (
        bundle.manifest.verification is not VerificationStatus.VERIFIED
        or bundle.manifest.to_json_bytes() != account_manifest.to_json_bytes()
    ):
        raise FinancialOutcomeError("selected epoch generation identity differs")
    sources = selected_hwm_source_paths(spec)
    # This existing verifier independently reconstructs the finalized mixed
    # product, including its policy projection, labels, and source identities.
    _verify_account_manifest_replay(
        bundle,
        account_manifest_admission_policy_path=sources[
            "account_manifest_admission_policy_path"
        ],
        account_manifest_flow_classification_policy_path=sources[
            "account_manifest_flow_classification_policy_path"
        ],
        provider_ledger_generation_path=sources["provider_ledger_generation_path"],
        baseline_generation_path=sources["baseline_generation_path"],
    )
    result = json.loads(bundle.payloads["outputs/account-strategy-manifest.json"])
    rows = result["rows"]
    fields = ("account_id", "strategy_id", "effective_start", "effective_end")
    if len(rows) != len(selected_keys) or {
        tuple(row[field] for field in fields) for row in rows
    } != set(selected_keys):
        raise FinancialOutcomeError("finalized epoch product perimeter differs")
    return cast(dict[str, Any], result)


def _verify_epochs(spec: Any, contract: Mapping[str, Any]) -> dict[str, Any]:
    _path, gate = _gate(spec, "financial-epoch-manifest-input-authority")
    inputs = gate["inputs"]
    selected_bytes = {}
    for key, definition in inputs.items():
        path = Path(definition["path"])
        selected_bytes[key] = _verified_file(
            path, definition["sha256"], definition["size_bytes"]
        )
    baselines = [
        node
        for node in spec.nodes
        if isinstance(node, GenerationNode)
        and node.root_id in {"baseline", "portfolio-baseline"}
    ]
    panels = [
        node
        for node in spec.nodes
        if isinstance(node, GenerationNode)
        and node.root_id in {"control-panel", "portfolio-panel"}
    ]
    if (
        len(baselines) != 1
        or len(panels) != 1
        or baselines[0].source_dir is None
        or panels[0].source_dir is None
    ):
        raise FinancialOutcomeError("financial epoch consumer generations differ")
    baseline, panel = baselines[0], panels[0]
    assert baseline.source_dir is not None and panel.source_dir is not None
    rebuilt = _rebuild_selected_epochs(spec, baseline, selected_bytes)
    required = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    }
    expected_count = sum(
        assignment["strategy_id"] in required
        for sleeve in contract["current_census"]["account_sleeves"]
        for assignment in sleeve["assignments"]
    )
    rows = rebuilt["rows"]
    if (
        len(rows) != expected_count
        or any(row.get("status") != "OK" for row in rows)
        or {row["strategy_id"] for row in rows} != required
    ):
        raise FinancialOutcomeError(
            "all required financial epochs must be numerically OK"
        )
    dashboard_path = panel.source_dir / "data/dashboard_data.json"
    files = [
        row for row in panel.files if row.relative_path == "data/dashboard_data.json"
    ]
    if len(files) != 1:
        raise FinancialOutcomeError("panel financial consumer is not bound")
    dashboard = json.loads(
        _verified_file(dashboard_path, files[0].sha256, files[0].size_bytes)
    )
    if dashboard.get("account_manifest") != rebuilt:
        raise FinancialOutcomeError("panel epoch values differ from canonical producer")
    return {
        "manifest": rebuilt,
        "dashboard": dashboard,
        "epoch_count": len(rows),
        "baseline_generation": baseline,
        "panel_generation": panel,
    }


def _verify_consumers(
    spec: Any,
    contract_path: Path,
    epochs: Mapping[str, Any],
    capital: Mapping[str, Any],
    hwm_bytes: Mapping[str, bytes],
    nodes: Mapping[str, ArtifactNode],
    replays: Mapping[tuple[str, Path], Mapping[str, Any]],
    series: Mapping[str, str],
    contract: Mapping[str, Any],
    plan_bindings: Mapping[tuple[str, str, str], Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    import portfolio_report as report_producer
    import portfolio_control_panel as panel_producer

    reports = [
        node
        for node in spec.nodes
        if isinstance(node, GenerationNode)
        and node.root_id in {"report", "reporting", "portfolio-report"}
    ]
    if len(reports) != 1:
        raise FinancialOutcomeError(
            "financial report generation is absent or ambiguous"
        )
    report_generation = reports[0]
    _report_path, report_bytes = _generation_file(
        report_generation, report_producer.REPORT_DATA_NAME
    )
    report = json.loads(report_bytes)
    dashboard = epochs["dashboard"]
    for consumer in (report, dashboard):
        if consumer.get("invested_capital") != capital:
            raise FinancialOutcomeError(
                "consumer invested capital differs from verified result"
            )
    projected_hwm = report_producer._report_governed_hwm_proof(
        json.loads(hwm_bytes["governed_result"]),
        hwm_bytes["input_generation"],
        hwm_bytes["calendar_authority"],
        hwm_bytes["provider_admission_receipt"],
        product_contract_bytes=contract_path.read_bytes(),
    )
    if (
        report.get("hwm_governed_proof") != projected_hwm
        or dashboard.get("hwm_governed_proof") != projected_hwm
    ):
        raise FinancialOutcomeError("consumer HWM differs from verified result")
    _path, manifest_bytes = _generation_file(
        report_generation, report_producer.EPOCH_MANIFEST_WORKBOOK_NAME
    )
    if json.loads(manifest_bytes) != epochs["manifest"]:
        raise FinancialOutcomeError(
            "report epoch numbers differ from canonical producer"
        )
    _path, crosswalk_bytes = _generation_file(
        report_generation, report_producer.EPOCH_CROSSWALK_WORKBOOK_NAME
    )
    crosswalk = json.loads(crosswalk_bytes)
    baseline_path, baseline_bytes = _generation_file(
        epochs["baseline_generation"], "portfolio-baseline.json"
    )
    baseline = json.loads(baseline_bytes)
    controls, _provenance = report_producer._report_product_controls(
        baseline, contract_path
    )
    report_producer._validate_current_epoch_crosswalk(
        crosswalk,
        strategy_product_controls=controls,
        baseline_catalog=baseline,
        baseline_ref={
            "generation_id": epochs["baseline_generation"].generation_id,
            "catalog_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        },
        baseline_catalog_path=baseline_path,
    )
    exact_epochs = report_producer._validate_report_account_manifest(
        epochs["manifest"], controls, crosswalk=crosswalk
    )
    capital_epochs = {
        (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        for row in capital["required_epochs"]
    }
    if capital_epochs != exact_epochs:
        raise FinancialOutcomeError(
            "capital and account-manifest epoch identities differ"
        )
    _path, downloadable_report = _generation_file(
        epochs["panel_generation"], panel_producer.REPORT_DOWNLOAD
    )
    if downloadable_report != report_bytes:
        raise FinancialOutcomeError("downloaded report differs from selected report")
    _path, workbook = _generation_file(
        epochs["panel_generation"], panel_producer.WORKBOOK_DOWNLOAD
    )
    parity = report_producer._workbook_parity_receipt(report_bytes, workbook)
    correlation_node = nodes["financial-outcome-dependence-live-only"]
    correlation_generation = _generation(spec, correlation_node)
    correlation = replays[("DEPENDENCE", correlation_node.path.resolve())]
    dependence, rows, _identity, _path = report_producer._dependence_report(
        None,
        generation_dir=correlation_generation.source_dir,
        calculation_cutoff_date=report.get("calculation_cutoff_date"),
    )
    if (
        report.get("dependence_analysis") != dependence
        or report.get("dependence_rows") != rows
    ):
        raise FinancialOutcomeError(
            "report dependence values differ from verified result"
        )
    correlation = panel_producer._current_product_correlation_projection(
        correlation, series, include_hybrid=True
    )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        projection = panel_producer._dependence_analysis(
            correlation,
            {
                "sha256": correlation_node.sha256,
                "generation_id": correlation_generation.generation_id,
            },
            set(series.values()),
            dependence_payload=correlation["dependence_modes"][mode],
            evidence_mode=mode,
        )
        if dashboard.get("dependence_modes", {}).get(mode) != projection:
            raise FinancialOutcomeError(f"panel dependence values differ: {mode}")
    return {
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "workbook_parity": parity,
        "decomposition_cells": _verify_decomposition_consumers(
            spec, contract, nodes, replays, series, dashboard, plan_bindings, plan
        ),
        "report_decomposition_cells": _verify_decomposition_consumers(
            spec, contract, nodes, replays, series, report, plan_bindings, plan
        ),
    }


def validate_financial_outcomes(
    spec: Any, *, allow_synthetic: bool = False
) -> dict[str, Any]:
    """Establish all four numerical outcomes from the supplied immutable spec.

    Used by final semantic generation, consumer validation, and shared commit.
    It does not accept readiness text as a financial result or create authority.
    """
    from portfolio_financial_mvp_contract import load_contract
    from portfolio_invested_capital import verify_governed_capital_result
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        verify_governed_hwm_result,
    )
    from portfolio_pipeline_materialize import (
        HWM_GOVERNED_POLICY,
        HWM_PERFORMANCE_POLICY,
        HWM_LEGACY_POLICY,
    )

    contract_path, raw_contract = _gate(spec, "financial-mvp-contract-authority")
    plans = {
        mode: _bound_decomposition_plan(
            spec, raw_contract, mode, allow_synthetic=allow_synthetic
        )
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST")
    }
    registry_path = (
        plans["LIVE_ONLY"]
        / "inputs/governance/current-strategy-decomposition-plan.json"
    )
    contract = load_contract(contract_path, registry_path=registry_path)
    selected_plan = json.loads(registry_path.read_bytes())
    if not allow_synthetic:
        required_ids = {
            row["strategy_id"]
            for row in contract["strategies"]
            if row["product_requirement"] == "REQUIRED"
        }
        unresolved = [
            f"{row['strategy_id']}/{source_key}: {cell['reason']}"
            for row in selected_plan["strategies"]
            if row["strategy_id"] in required_ids
            for source_key, cell in row["source_dispositions"].items()
            if cell["requirement"] == "REQUIRED"
            and cell["readiness"] != "READY_TO_MODEL"
        ]
        if unresolved:
            raise FinancialOutcomeError(
                "required decomposition applicability is unresolved: "
                + "; ".join(unresolved)
            )
    nodes = verify_outcome_inventory(spec, contract, plan=selected_plan)
    roles = required_outcome_roles(contract, plan=selected_plan)
    series = _required_series(contract, plan=selected_plan)
    _assessment_path, assessment = _gate(spec, "financial-mvp-hwm-input-authority")
    if assessment.get("schema_version") != 2 or assessment.get("state") != "AVAILABLE":
        raise FinancialOutcomeError("verified HWM assessment 2 AVAILABLE is required")
    bindings = assessment["bindings"]
    hwm_bytes = {}
    for key, binding in bindings.items():
        hwm_bytes[key] = _verified_file(
            Path(binding["path"]), binding["sha256"], binding["size_bytes"]
        )
    selected_sources = selected_hwm_source_paths(spec)
    verify_selected_hwm_generation(
        selected_sources,
        contract_path=contract_path,
        bindings={key: Path(value["path"]) for key, value in bindings.items()},
    )
    hwm_node = nodes["financial-outcome-hwm"]
    if (
        hwm_node.sha256 != bindings["governed_result"]["sha256"]
        or hwm_node.path.resolve()
        != Path(bindings["governed_result"]["path"]).resolve()
    ):
        raise FinancialOutcomeError(
            "HWM outcome and assessment refer to different results"
        )
    hwm = verify_governed_hwm_result(
        json.loads(hwm_bytes["governed_result"]),
        hwm_bytes["input_generation"],
        calendar_bytes=hwm_bytes["calendar_authority"],
        input_receipt_bytes=hwm_bytes["provider_admission_receipt"],
        policy_bytes=HWM_GOVERNED_POLICY.read_bytes(),
        product_contract_bytes=contract_path.read_bytes(),
        performance_policy_bytes=HWM_PERFORMANCE_POLICY.read_bytes(),
        legacy_policy_bytes=HWM_LEGACY_POLICY.read_bytes(),
    )
    if hwm.get("status") != "AVAILABLE":
        raise FinancialOutcomeError("HWM numerical result is unavailable")
    capital_node = nodes["financial-outcome-capital"]
    capital = json.loads(capital_node.path.read_bytes())
    capital_input = capital["input_generation"]
    _artifact_for_digest(spec, capital_input["sha256"], capital_input["size_bytes"])
    verify_selected_capital_ledger(
        Path(capital_input["path"]),
        selected_sources["provider_ledger_generation_path"],
    )
    capital = verify_governed_capital_result(
        capital, Path(capital_input["path"]), contract_path
    )
    if capital.get("status") != "AVAILABLE" or (
        not allow_synthetic and capital.get("authority_profile") != "ACCEPTED_GOVERNED"
    ):
        raise FinancialOutcomeError("capital result is unavailable or synthetic")
    replays: dict[tuple[str, Path], dict[str, Any]] = {}
    plan_bindings = {}
    plan_artifacts: dict[tuple[str, str, str], tuple[Path, str, int]] = {}
    selected_cells = {}
    for node_id, key in roles.items():
        if key[0] not in {"DECOMPOSITION", "DEPENDENCE"}:
            continue
        node = nodes[node_id]
        cache_key = (key[0], node.path.resolve())
        if cache_key not in replays:
            replays[cache_key] = _replay_model(
                spec,
                node,
                key[0],
                source_root=plans[key[-1]] if key[0] == "DECOMPOSITION" else None,
            )
        value = replays[cache_key]
        if key[0] == "DECOMPOSITION":
            _, strategy, source_key, lens, surface, mode = key
            plan_key = (source_key, surface, mode)
            identity = (node.path.resolve(), node.sha256, node.size_bytes)
            if plan_key in plan_artifacts and plan_artifacts[plan_key] != identity:
                raise FinancialOutcomeError(
                    "one decomposition surface selects conflicting generations"
                )
            plan_artifacts[plan_key] = identity
            if plan_key not in plan_bindings:
                from portfolio_control_panel import (
                    verify_decomposition_surface_against_plan,
                )

                generation = _generation(spec, node)
                assert generation.source_dir is not None
                plan_bindings[plan_key] = verify_decomposition_surface_against_plan(
                    plans[mode],
                    source_key,
                    surface,
                    mode,
                    value,
                    generation.source_dir / "generation.json",
                )
            if value.get("analysis_lens") != lens or value.get("evidence_mode") != mode:
                raise FinancialOutcomeError(
                    f"decomposition required cell binding differs: {node_id}"
                )
            selected = [
                row
                for row in value["model_results"]
                if row.get("series_id") == series[strategy]
                and row.get("selected_for_series") is True
                and (
                    "DYNAMIC_FLS" if row.get("estimator") == "DYNAMIC_FLS" else "STATIC"
                )
                == surface
            ]
            if len(selected) != 1 or selected[0].get("status") not in {
                "AVAILABLE",
                "DIAGNOSTIC_ONLY",
            }:
                raise FinancialOutcomeError(
                    f"required decomposition cell is not verified: {node_id}"
                )
            selected_cells[node_id] = {
                "source_key": source_key,
                "surface": surface,
                "model_id": selected[0]["model_id"],
                "status": selected[0]["status"],
                "complete_through": selected[0].get("model_as_of_date"),
            }
        else:
            mode = value.get("dependence_modes", {}).get(key[1])
            expected_series = set(series.values())
            if (
                not isinstance(mode, dict)
                or set(mode.get("series_ids", [])) != expected_series
            ):
                raise FinancialOutcomeError(
                    "dependence required strategy/mode coverage differs"
                )
            expected_pairs = {
                frozenset((left, right))
                for left in expected_series
                for right in expected_series
                if left != right
            }
            pairs = mode["observed_cells"]
            if (
                len(pairs) != len(expected_pairs)
                or {frozenset((row["left"], row["right"])) for row in pairs}
                != expected_pairs
                or any(row.get("status") != "AVAILABLE" for row in pairs)
            ):
                raise FinancialOutcomeError(
                    "dependence exact required pairs are not available"
                )
            selected_cells[node_id] = {
                "pair_count": len(pairs),
                "status": "AVAILABLE",
                "complete_through": value.get("complete_through"),
            }
    epochs = _verify_epochs(spec, contract)
    consumers = _verify_consumers(
        spec,
        contract_path,
        epochs,
        capital,
        hwm_bytes,
        nodes,
        replays,
        series,
        contract,
        plan_bindings,
        selected_plan,
    )
    return {
        "status": "PASS",
        "outcome_ids": [
            "AS_HELD_COMPOSITE_HWM",
            "INVESTED_CAPITAL",
            "DECOMPOSITION",
            "DEPENDENCE",
        ],
        "artifact_count": len(nodes),
        "epoch_count": epochs["epoch_count"],
        "capital_result_id": capital["result_id"],
        "selected_cells": selected_cells,
        "consumers": consumers,
        "artifact_sha256s": {key: node.sha256 for key, node in sorted(nodes.items())},
    }

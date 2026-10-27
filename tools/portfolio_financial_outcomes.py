"""Finite financial-MVP outcome bindings shared by semantic and consumer checks.

Contract schema 3 remains unchanged. Existing ArtifactNodes carry the actual
financial outputs; existing GenerationNodes carry their immutable containers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
import contextlib
from datetime import UTC, date, datetime
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import re
from typing import Any, cast

import derived_epoch_perimeter
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
        # Neither of these is published as a standalone artifact inside a release
        # generation. HWM never was; INVESTED_CAPITAL stopped being one when the workbook
        # runtime was removed under the owner's permanent hold (47120f1) -- the only
        # promoter of `invested-capital.json` into the report generation was the workbook
        # route (`portfolio_report.py:11002`/`:11201`, fed solely from `:9547`/`:9693`).
        # Both remain verified where they ship: HWM by the binding at the assessment gate,
        # capital by re-derivation from a digest-resolved, ledger-bound input, with that
        # input pinned to the declared `financial-capital-input` row below.
        # Why the two exemptions are compensated differently, and why a HWM-style gate for
        # capital would be ceremony: docs/financial-outcome-verification.md
        if required[node_id][0] in ("AS_HELD_COMPOSITE_HWM", "INVESTED_CAPITAL"):
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


def _first_difference(actual: Any, expected: Any, path: str = "") -> str:
    """Name the first place two projections diverge, so a failure is actionable."""

    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                return f"{path}.{key} missing from panel"
            if key not in expected:
                return f"{path}.{key} unexpected in panel"
            if actual[key] != expected[key]:
                return _first_difference(actual[key], expected[key], f"{path}.{key}")
        return f"{path} equal"
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return f"{path} length {len(actual)} != {len(expected)}"
        for index, (left, right) in enumerate(zip(actual, expected)):
            if left != right:
                return _first_difference(left, right, f"{path}[{index}]")
        return f"{path} equal"
    return f"{path or 'value'} panel={actual!r} expected={expected!r}"


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


EPOCH_MANIFEST_CALCULATION_MEMBERS = {
    "account_strategy_ledger": "calculation_ledger",
    "account_strategy_ledger_identity": "calculation_ledger_identity",
    "account_strategy_classified_flows": "calculation_classified_flows",
    "account_strategy_flow_coverage": "calculation_flow_coverage",
}


def _optional_gate(spec: Any, gate_id: str) -> tuple[Path, dict[str, Any]] | None:
    """The gate if the specification carries it, rather than insisting on it."""

    if not any(gate.gate_id == gate_id for gate in spec.gates):
        return None
    return _gate(spec, gate_id)


def _artifact_node(spec: Any, node_id: str) -> Any:
    """Return the single ARTIFACT node the specification binds under this id."""

    nodes = [
        node
        for node in spec.nodes
        if isinstance(node, ArtifactNode) and node.node_id == node_id
    ]
    if len(nodes) != 1:
        raise FinancialOutcomeError(
            f"selected financial artifact node differs: {node_id}"
        )
    return nodes[0]


def _epoch_manifest_profile(spec: Any) -> dict[str, Any]:
    """Resolve the five epoch-manifest inputs for whichever profile is in force.

    Schema 9 names them in the gate. Schema 10 has no gate -- by design -- and
    the same five are projected out of the bound generation, which is exactly
    how `_rebuild_selected_epochs` defines them.
    """

    from portfolio_pipeline_materialize import ACCOUNT_STRATEGY_CLASSIFICATION_POLICY

    gate = _optional_gate(spec, "financial-epoch-manifest-input-authority")
    if gate is not None:
        _path, payload = gate
        inputs = payload["inputs"]
        selected_bytes: dict[str, bytes] = {
            key: _verified_file(
                Path(definition["path"]),
                definition["sha256"],
                definition["size_bytes"],
            )
            for key, definition in inputs.items()
        }
        return {
            "selected_bytes": selected_bytes,
            "admission_policy_path": Path(
                inputs["account_strategy_admission_policy"]["path"]
            ),
            "classification_policy_path": ACCOUNT_STRATEGY_CLASSIFICATION_POLICY,
        }

    _keys, generation_dir, manifest = _selected_epoch_assignment_keys(spec)
    _identity_path, identity_bytes = _manifest_artifact(
        generation_dir, manifest, "authority/selected-input-identities.json"
    )
    identities = json.loads(identity_bytes)
    _calculation_path, calculation_bytes = _manifest_artifact(
        generation_dir, manifest, "authority/calculation-inputs.json"
    )
    calculation = json.loads(calculation_bytes)
    selected_bytes = {}
    for key, member in EPOCH_MANIFEST_CALCULATION_MEMBERS.items():
        if member not in calculation:
            raise FinancialOutcomeError("selected epoch calculation inputs differ")
        selected_bytes[key] = canonical_json_bytes(calculation[member])
    admission = identities.get("admission_policy")
    if not isinstance(admission, Mapping):
        raise FinancialOutcomeError("selected epoch admission policy identity differs")
    # The generation stores a projection of the policy, not the policy, so the selected
    # input's bytes are never among its artifacts -- see `_admission_policy_artifact` in
    # `portfolio_pipeline_materialize`. The production specification carries the input as
    # its own node, exactly as it carries the generation it belongs to.
    admission_node = _artifact_node(spec, "financial-account-manifest-admission-policy")
    if (
        admission_node.sha256 != admission["sha256"]
        or admission_node.size_bytes != admission["size_bytes"]
    ):
        raise FinancialOutcomeError("selected epoch admission policy identity differs")
    admission_path = admission_node.path
    selected_bytes["account_strategy_admission_policy"] = _verified_file(
        admission_path, admission_node.sha256, admission_node.size_bytes
    )
    # The classification policy stays the repository copy for both profiles:
    # it is what the generation pins as its selected `flow_classification_policy`,
    # and the selected-input checker verifies the file it is handed against that
    # pin. (The bundle also carries a differently-shaped policy document of its
    # own, which the classified flows name; that one is not this input.)
    return {
        "selected_bytes": selected_bytes,
        "admission_policy_path": admission_path,
        "classification_policy_path": ACCOUNT_STRATEGY_CLASSIFICATION_POLICY,
    }


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


def selected_hwm_source_paths(spec: Any) -> dict[str, Any]:
    """Resolve the already selected account, provider and baseline authorities.

    A specification that selects derived-sleeve facts (#567) names each artifact
    as a node ``financial-sleeve-facts*``; their verified paths are returned under
    ``sleeve_facts_paths`` so the replay can re-derive the sleeve epochs.  A
    specification without such a node returns no such key.
    """

    account = [
        node
        for node in spec.nodes
        if isinstance(node, ArtifactNode)
        and node.node_id == "financial-account-manifest-generation"
    ]
    if len(account) != 1 or account[0].path.name != "generation.json":
        raise FinancialOutcomeError("selected HWM account generation binding differs")
    _verified_file(account[0].path, account[0].sha256, account[0].size_bytes)
    result: dict[str, Any] = {"account_manifest_generation_path": account[0].path}
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
    profile = _epoch_manifest_profile(spec)
    result["account_manifest_admission_policy_path"] = profile["admission_policy_path"]
    result["account_manifest_flow_classification_policy_path"] = profile[
        "classification_policy_path"
    ]
    facts = sorted(
        (
            node
            for node in spec.nodes
            if isinstance(node, ArtifactNode)
            and node.node_id.startswith(
                derived_epoch_perimeter.SLEEVE_FACTS_NODE_PREFIX
            )
        ),
        key=lambda node: node.node_id,
    )
    if facts:
        for node in facts:
            _verified_file(node.path, node.sha256, node.size_bytes)
        result["sleeve_facts_paths"] = tuple(node.path for node in facts)
    return result


def verify_selected_capital_ledger(
    input_generation_path: Path, provider_ledger_generation_path: Path
) -> Path:
    """Bind capital's provider ledger to the generation this candidate selected.

    Every ledger generation built under one contract satisfies the same
    admission receipt, so without this check an older ledger could validate
    while the candidate selected a newer one.

    The binding carries the generation id, which is portable identity rather
    than a locator. This previously compared an absolute path for containment,
    which forced the published financial artifact to carry the directory it was
    built in and made its identity depend on where the run happened. Identity
    holds for both on-disk shapes -- the ledger sits directly in the generation
    directory on the governed route and under a normalized subdirectory
    elsewhere, which is why containment was used before -- and it distinguishes
    a byte-identical ledger from a different generation, which neither
    containment nor a content hash alone can do.
    """
    inputs = json.loads(input_generation_path.read_bytes())
    bindings = inputs.get("source_bindings")
    ledger = bindings.get("provider_ledger") if isinstance(bindings, Mapping) else None
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(ledger.get("sha256"), str)
        or not isinstance(ledger.get("generation_id"), str)
    ):
        raise FinancialOutcomeError("capital provider ledger binding is absent")
    selected_root = provider_ledger_generation_path.resolve().parent
    # Bind by portable identity rather than by containment. The old check asked
    # whether an absolute path sat under the selected directory, which forced
    # the published result to carry its own build location. Generation id and
    # content hash together are strictly stronger: containment alone could not
    # tell a byte-identical ledger from a different generation apart, and a
    # hash alone cannot either.
    if ledger["generation_id"] != selected_root.name:
        raise FinancialOutcomeError(
            "capital provider ledger differs from the selected generation"
        )
    return selected_root


def verify_selected_hwm_generation(
    source_paths: Mapping[str, Any],
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


_VERIFICATION_LEDGER_DIRECTORY = "financial-verification-ledger"
_CALLER_LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_WORK_COUNTER_KEYS = (
    "model_fit_calls",
    "grid_search_calls",
    "correlation_build_calls",
    "full_rederivation_count",
    "receipt_reuse_count",
)


def _call_record(
    kind: str, node_ids: list[str], calculation_identity: str, outcome: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "node_ids": node_ids,
        "calculation_identity": calculation_identity,
        "outcome": outcome,
        "model_fit_calls_static": 0,
        "model_fit_calls_dynamic": 0,
        "grid_search_calls": 0,
        "correlation_build_calls": 0,
    }


def _spec_identity(spec: Any) -> str:
    """Name the verified specification, falling back to its bound node digests."""

    declared = getattr(spec, "spec_sha256", None)
    if isinstance(declared, str) and re.fullmatch(r"[0-9a-f]{64}", declared):
        return declared
    rows = sorted(
        (
            str(getattr(node, "node_id", "")),
            str(
                getattr(node, "sha256", None)
                or getattr(node, "manifest_sha256", None)
                or ""
            ),
        )
        for node in getattr(spec, "nodes", ())
    )
    return hashlib.sha256(canonical_json_bytes([list(row) for row in rows])).hexdigest()


def _write_verification_ledger(
    spec: Any,
    caller: str,
    work_counters: Mapping[str, int],
    calls: list[dict[str, Any]],
) -> None:
    """Record one verifier invocation where it happens; one create-only file each."""

    state_root = getattr(spec, "state_root", None)
    if not isinstance(state_root, Path):
        return
    spec_sha256 = _spec_identity(spec)
    now = datetime.now(UTC)
    entry = {
        "schema_version": 1,
        "caller": caller,
        "spec_sha256": spec_sha256,
        "recorded_at": now.isoformat(),
        "pid": os.getpid(),
        "work_counters": {key: work_counters[key] for key in _WORK_COUNTER_KEYS},
        "model_fit_calls_by_estimator": {
            "static": sum(row["model_fit_calls_static"] for row in calls),
            "dynamic": sum(row["model_fit_calls_dynamic"] for row in calls),
        },
        "calls": [
            {**row, "node_ids": sorted(row["node_ids"])}
            for row in sorted(calls, key=lambda row: row["calculation_identity"])
        ],
    }
    directory = state_root.resolve() / _VERIFICATION_LEDGER_DIRECTORY
    if directory.is_symlink():
        raise FinancialOutcomeError("financial verification ledger is linked")
    directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{now.strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}-{caller}-{spec_sha256[:16]}"
    )
    content = canonical_json_bytes(entry)
    for attempt in range(1000):
        path = directory / (
            f"{stem}.json" if attempt == 0 else f"{stem}-{attempt}.json"
        )
        try:
            with path.open("xb") as handle:
                handle.write(content)
            return
        except FileExistsError:
            continue
    raise FinancialOutcomeError("financial verification ledger name is exhausted")


@contextlib.contextmanager
def _verification_ledger(
    spec: Any, caller: str
) -> Iterator[tuple[dict[str, int], list[dict[str, Any]]]]:
    """Yield live counters and call records; the ledger is written even on failure."""

    if _CALLER_LABEL.fullmatch(caller) is None:
        raise FinancialOutcomeError("financial verification caller label is invalid")
    work_counters = {key: 0 for key in _WORK_COUNTER_KEYS}
    calls: list[dict[str, Any]] = []
    try:
        yield work_counters, calls
    finally:
        _write_verification_ledger(spec, caller, work_counters, calls)


def _replay_receipt_path(spec: Any, calculation_identity: str) -> Path | None:
    """Locate the existing retained state surface without coupling to run roots."""

    state_root = getattr(spec, "state_root", None)
    if not isinstance(state_root, Path):
        return None
    return (
        state_root.resolve()
        / "financial-replay-receipts"
        / f"{calculation_identity}.json"
    )


def _read_replay_receipt(path: Path | None, expected: Mapping[str, object]) -> bool:
    if path is None or path.is_symlink() or not path.is_file():
        return False
    try:
        value: object = json.loads(path.read_bytes())
        return isinstance(value, dict) and value == expected
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _write_replay_receipt(path: Path | None, value: Mapping[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise FinancialOutcomeError("financial replay receipt is linked")
    content = canonical_json_bytes(value)
    if path.exists():
        if path.read_bytes() != content:
            raise FinancialOutcomeError("financial replay receipt identity differs")
        return
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise FinancialOutcomeError(
                "financial replay receipt identity differs"
            ) from None


def financial_outcomes_semantic_identity(proof: Mapping[str, Any]) -> str:
    """Hash immutable financial outcomes without per-invocation work evidence."""

    semantic_proof = {
        key: value for key, value in proof.items() if key != "work_counters"
    }
    return hashlib.sha256(canonical_json_bytes(semantic_proof)).hexdigest()


def _correlation_consumed_producer_identity(
    producer: Any,
) -> dict[str, str]:
    """Hash the repository bytes the correlation replay actually imports."""

    producer_path = Path(cast(str, producer.__file__)).resolve()
    paths = {
        "core_contracts": Path(
            producer.canonical_json_bytes.__code__.co_filename
        ).resolve(),
        "decimal_text": Path(producer.decimal_text.__code__.co_filename).resolve(),
        "producer": producer_path,
    }
    lock = producer_path.parents[1] / "uv.lock"
    paths["dependency_lock"] = lock
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in sorted(paths.items())
    }


def _replay_model(
    spec: Any,
    artifact: ArtifactNode,
    kind: str,
    *,
    source_root: Path | None = None,
    work_counters: dict[str, int] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Delegate numerical replay to the existing domain producer, once per file."""
    artifact_bytes = _verified_file(artifact.path, artifact.sha256, artifact.size_bytes)
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
    specification_bytes = _verified_file(
        specification.path, definition.sha256, definition.size_bytes
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
        build = cast(Any, portfolio_decomposition.build_decomposition)
        producer_dependencies = portfolio_decomposition._consumed_producer_identity()
    else:
        correlation_spec = portfolio_correlation._load_spec(specification.path)
        source_definitions = [correlation_spec.source]
        build = cast(Any, portfolio_correlation.build_correlation)
        producer_dependencies = _correlation_consumed_producer_identity(
            portfolio_correlation
        )
    roots = set()
    source_bindings: list[dict[str, object]] = []
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
        source_bytes = _verified_file(
            node.path, source.expected_sha256, source.expected_size_bytes
        )
        source_bindings.append(
            {
                "relative_path": source.relative_path,
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size_bytes": len(source_bytes),
            }
        )
    if len(roots) != 1:
        raise FinancialOutcomeError("financial producer source roots conflict")
    receipt_base: dict[str, object] = {
        "kind": kind,
        "producer_sha256": hashlib.sha256(
            (
                Path(portfolio_decomposition.__file__)
                if kind == "DECOMPOSITION"
                else Path(portfolio_correlation.__file__)
            ).read_bytes()
        ).hexdigest(),
        "producer_dependencies": producer_dependencies,
        "result_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "result_size_bytes": len(artifact_bytes),
        "source_bindings": sorted(
            source_bindings, key=lambda row: str(row["relative_path"])
        ),
        "spec_sha256": hashlib.sha256(specification_bytes).hexdigest(),
        "spec_size_bytes": len(specification_bytes),
        "validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "verification_scope": "SOURCE_PRODUCER_RESULT_AND_CONSUMER_PROJECTION_V1",
    }
    calculation_identity = hashlib.sha256(
        canonical_json_bytes(receipt_base)
    ).hexdigest()
    receipt = {
        "calculation_identity": calculation_identity,
        "schema_version": 1,
        **receipt_base,
    }
    receipt_path = _replay_receipt_path(spec, calculation_identity)
    if _read_replay_receipt(receipt_path, receipt):
        if work_counters is not None:
            work_counters["receipt_reuse_count"] += 1
        if calls is not None:
            calls.append(
                _call_record(
                    kind, [artifact.node_id], calculation_identity, "RECEIPT_REUSED"
                )
            )
        return cast(dict[str, Any], json.loads(artifact_bytes.decode("utf-8")))
    record = _call_record(kind, [artifact.node_id], calculation_identity, "REDERIVED")
    if calls is not None:
        calls.append(record)
    if work_counters is not None:
        work_counters["full_rederivation_count"] += 1
    if kind == "DECOMPOSITION":
        original_dynamic_fit: Any = portfolio_decomposition._dynamic_fit
        original_fit: Any = portfolio_decomposition._fit

        def counted_dynamic_fit(*args: object, **kwargs: object) -> Any:
            record["model_fit_calls_dynamic"] += 1
            if work_counters is not None:
                work_counters["model_fit_calls"] += 1
            return original_dynamic_fit(*args, **kwargs)

        def counted_fit(*args: object, **kwargs: object) -> Any:
            record["model_fit_calls_static"] += 1
            if work_counters is not None:
                work_counters["model_fit_calls"] += 1
            return original_fit(*args, **kwargs)

        record["grid_search_calls"] += len(parsed.models)
        if work_counters is not None:
            work_counters["grid_search_calls"] += len(parsed.models)
        portfolio_decomposition._dynamic_fit = cast(Any, counted_dynamic_fit)
        portfolio_decomposition._fit = cast(Any, counted_fit)
        try:
            result, rebuilt_manifest, result_bytes = build(
                specification.path, next(iter(roots))
            )
        finally:
            portfolio_decomposition._dynamic_fit = original_dynamic_fit
            portfolio_decomposition._fit = original_fit
    else:
        record["correlation_build_calls"] += 1
        if work_counters is not None:
            work_counters["correlation_build_calls"] += 1
        result, rebuilt_manifest, result_bytes = build(
            specification.path, next(iter(roots))
        )
    if (
        rebuilt_manifest.to_json_bytes() != manifest.to_json_bytes()
        or result_bytes != artifact_bytes
    ):
        raise FinancialOutcomeError(
            "financial model differs from selected-source recomputation"
        )
    _write_replay_receipt(receipt_path, receipt)
    # Hand back what every consumer actually read, not the in-memory object.
    # They all loaded the published bytes, and `canonical_json_bytes` sorts keys,
    # so a deserialized mapping iterates its factors in sorted order while the
    # freshly built one iterates them in `model.factor_ids` order. That only
    # matters because a consumer sums factor contributions in iteration order
    # (`portfolio_control_panel._factor_analysis`), and a different summand order
    # rounds differently in the last bits -- enough to move a `.12g` string and
    # fail the consumer comparison against a projection that is otherwise exactly
    # equal. The bytes were just proved identical above, so this changes nothing
    # except the iteration order the comparison inherits.
    return cast(dict[str, Any], json.loads(result_bytes.decode("utf-8")))


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


_RETAINED_CAPTURE_KEY = "inputs/governance/retained-public-factor-capture.json"
_HISTORY_AUTHORITY_NODES = {
    "provider_admission_receipt_path": "financial-history-authority-admission-receipt",
    "provider_ledger_manifest_path": "financial-history-authority-ledger-manifest",
    "provider_ledger_catalog_path": "financial-history-authority-ledger-catalog",
    "portfolio123_version_evidence_path": "financial-history-authority-p123-version",
}
# Version evidence is its own flag in the verifier and optional in the selected-input record, so
# it may be absent while the admission group is bound.
_OPTIONAL_HISTORY_AUTHORITIES = frozenset({"portfolio123_version_evidence_path"})


def _history_authorities(spec: Any, bundle: Path) -> dict[str, Path]:
    """Resolve the layered-history reader authorities the attended run supplied.

    A layered-history generation records the evidence its builder was given, and the verifier
    derives the exact key set it demands from which of these arguments it receives
    (`portfolio_target_series.py:6088-6119`). Replaying a generation built with them therefore
    requires passing them, and supplying them verifies strictly more: the provider-ledger trio
    must be exact-named siblings of one generation directory and the admission authority is
    re-derived and compared by digest (`portfolio_target_series.py:2350-2357, 2392-2396`).

    Two groups. The admission receipt, ledger manifest and ledger catalog -- and with them the
    financial input contract -- stand or fall together, because the verifier ties the contract
    to the receipt and the receipt to its ledger siblings (`portfolio_target_series.py:2339-2357`).
    Version evidence is a separate flag there (`:6089`) and optional in the selected-input record,
    so it may be absent on its own.

    A bundle whose generation was built without authorities -- a synthetic fixture -- has no rows
    and replays as before. Neither absence can weaken a release: a generation built with an
    authority and replayed without it fails the verifier's key-set check outright.
    """

    resolved: dict[str, Path] = {}
    for name, node_id in _HISTORY_AUTHORITY_NODES.items():
        matches = [
            row
            for row in spec.nodes
            if isinstance(row, ArtifactNode) and row.node_id == node_id
        ]
        if len(matches) > 1:
            raise FinancialOutcomeError(
                f"layered history authority binding differs: {node_id}"
            )
        if matches:
            node = matches[0]
            _verified_file(node.path, node.sha256, node.size_bytes)
            resolved[name] = node.path
    admission = set(_HISTORY_AUTHORITY_NODES) - _OPTIONAL_HISTORY_AUTHORITIES
    bound = admission & set(resolved)
    if bound and bound != admission:
        missing = sorted(admission - bound)
        raise FinancialOutcomeError(
            f"layered history authorities are partially bound: {', '.join(missing)}"
        )
    if bound:
        # The contract is the bundle's own copy, already passed as the contract path. The
        # verifier refuses a contract without its receipt, so it joins the admission group.
        resolved["financial_input_contract_path"] = (
            bundle / "inputs/history/financial-mvp-contract.json"
        )
    return resolved


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
    # The root is what has to be unique, not each file. The primary and hybrid bundles share
    # eight of their nine base sources byte-for-byte, so resolving each source on its own
    # requires a digest to appear at exactly one path in the run and cannot hold once both
    # roots are named. Intersecting the roots each source admits picks the single root that
    # explains the whole base specification, and every file is still required to sit under it
    # at its declared relative path with its declared digest and size.
    base_roots: set[Path] | None = None
    for definition in base.get("provenance_sources", []):
        relative = Path(definition["relative_path"])
        admitted = set()
        for node in spec.nodes:
            if (
                not isinstance(node, ArtifactNode)
                or node.sha256 != definition["expected_sha256"]
                or node.size_bytes != definition["expected_size_bytes"]
            ):
                continue
            resolved = node.path.resolve()
            if resolved.parts[-len(relative.parts) :] != relative.parts:
                continue
            root = resolved.parents[len(relative.parts) - 1]
            if (root / relative).resolve() == resolved:
                admitted.add(root)
        if not admitted:
            raise FinancialOutcomeError("decomposition base source is absent")
        base_roots = admitted if base_roots is None else base_roots & admitted
        if not base_roots:
            raise FinancialOutcomeError("decomposition base source roots conflict")
    if base_roots is not None and len(base_roots) != 1:
        raise FinancialOutcomeError("decomposition base source root is ambiguous")
    outputs, rebuilt = producer.build_bundle(
        plan_path=bundle / "inputs/governance/current-strategy-decomposition-plan.json",
        catalog_path=bundle / "inputs/governance/decomposition-source-catalog.json",
        financial_mvp_contract_path=bundle
        / "inputs/history/financial-mvp-contract.json",
        layered_history_generation_path=history.path.parent,
        base_spec_path=bundle / "inputs/governance/base-decomposition-spec.json",
        base_source_root=next(iter(base_roots or ()), bundle),
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
        # The bundle declares its own retained capture as an input, and the loop above has
        # already verified those bytes against the manifest digest. Without it the replay
        # runs the RELEASE_QC freshness gate with no retained capture while the attended run
        # had one, so a bundle that was built legitimately cannot be rebuilt from itself.
        retained_public_factor_capture=(
            json.loads(files[_RETAINED_CAPTURE_KEY])
            if _RETAINED_CAPTURE_KEY in files
            else None
        ),
        **_history_authorities(spec, bundle),
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
        # Reported one clause at a time. As a single `or` chain this said only
        # "values differ", which cost hours to attribute -- the tripped clause
        # and the differing field were both invisible.
        if displayed != [expected]:
            raise FinancialOutcomeError(
                f"panel decomposition values differ: {node_id}: "
                + _first_difference(displayed, [expected])
            )
        if len(source_rows) != 1:
            raise FinancialOutcomeError(
                f"panel decomposition source rows differ: {node_id}: "
                f"{len(source_rows)} rows for {source_key}"
            )
        observed_source = source_rows[0]
        for label, actual, wanted in (
            ("source_sha256", observed_source.get("source_sha256"), node.sha256),
            (
                "generation_id",
                observed_source.get("generation_id"),
                _generation(spec, node).generation_id,
            ),
            (
                "plan_spec",
                observed_source.get("plan_spec"),
                plan_bindings[(source_key, surface, mode)],
            ),
        ):
            if actual != wanted:
                raise FinancialOutcomeError(
                    f"panel decomposition {label} differs: {node_id}: "
                    f"panel={actual!r} expected={wanted!r}"
                )
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
        **(
            {"sleeve_facts_paths": sources["sleeve_facts_paths"]}
            if sources.get("sleeve_facts_paths")
            else {}
        ),
    )
    result = json.loads(bundle.payloads["outputs/account-strategy-manifest.json"])
    rows = result["rows"]
    fields = ("account_id", "strategy_id", "effective_start", "effective_end")
    coverage_bytes = bundle.payloads.get("authority/account-flow-coverage.json")
    coverage = (
        json.loads(coverage_bytes) if coverage_bytes is not None else {"rows": []}
    )
    try:
        # The crosswalk names the baseline perimeter; derived sleeves replace a
        # residual parent's epoch (#567), and nothing else may differ.
        derived_epoch_perimeter.effective_perimeter(
            selected_keys,
            [tuple(row[field] for field in fields) for row in rows],
            coverage["rows"],
        )
    except derived_epoch_perimeter.PerimeterError as exc:
        raise FinancialOutcomeError(
            f"finalized epoch product perimeter differs: {exc}"
        ) from exc
    if len(rows) != len({tuple(row[field] for field in fields) for row in rows}):
        raise FinancialOutcomeError("finalized epoch product perimeter differs")
    return cast(dict[str, Any], result)


def _census_assignment_only_epochs(
    rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    cutoff: date,
) -> list[dict[str, Any]]:
    """Rows the census confirms are ASSIGNMENT_ONLY, never FUNDED_MEMBERSHIP.

    A row is exempt from the "numerically OK" requirement only when the
    producer itself refused to fund it (``BLOCKED_ADMISSION`` /
    ``BLOCKED`` / no funded NAV records), its interval is closed on or
    before the cutoff, and the census confirms that exact
    ``(strategy_id, effective_start, effective_end)`` tuple is an
    ASSIGNMENT_ONLY interval and not also a FUNDED_MEMBERSHIP interval.
    ``blocking_reason`` text is never consulted: it is admission-policy
    data, not code authority, and a row must not be able to exempt itself.
    """

    required = {
        strategy["strategy_id"]
        for strategy in contract["strategies"]
        if strategy["product_requirement"] == "REQUIRED"
    }
    assignments = [
        assignment
        for sleeve in contract["current_census"]["account_sleeves"]
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] in required
    ]
    assignment_only = Counter(
        (
            assignment["strategy_id"],
            assignment["effective_start"],
            assignment["effective_end"],
        )
        for assignment in assignments
        if assignment["status"] == "ASSIGNMENT_ONLY"
    )
    funded_membership = {
        (
            assignment["strategy_id"],
            assignment["effective_start"],
            assignment["effective_end"],
        )
        for assignment in assignments
        if assignment["status"] == "FUNDED_MEMBERSHIP"
    }

    def is_census_assignment_only(row: Mapping[str, Any]) -> bool:
        key = (row["strategy_id"], row["effective_start"], row["effective_end"])
        effective_end = row["effective_end"]
        return (
            row.get("status") == "BLOCKED_ADMISSION"
            and row.get("admission_status") == "BLOCKED"
            and row.get("funded_nav_record_ids") == []
            and effective_end is not None
            and date.fromisoformat(effective_end) <= cutoff
            and key in assignment_only
            and key not in funded_membership
        )

    exempt_rows = [row for row in rows if is_census_assignment_only(row)]
    exempt_counts = Counter(
        (row["strategy_id"], row["effective_start"], row["effective_end"])
        for row in exempt_rows
    )
    if exempt_counts != assignment_only:
        raise FinancialOutcomeError(
            "all required financial epochs must be numerically OK"
        )
    scored_rows = [row for row in rows if not is_census_assignment_only(row)]
    if any(row.get("status") != "OK" for row in scored_rows):
        raise FinancialOutcomeError(
            "all required financial epochs must be numerically OK"
        )
    if {row["strategy_id"] for row in scored_rows} != required:
        raise FinancialOutcomeError(
            "all required financial epochs must be numerically OK"
        )
    return sorted(
        (
            {
                "account_id": row["account_id"],
                "strategy_id": row["strategy_id"],
                "effective_start": row["effective_start"],
                "effective_end": row["effective_end"],
            }
            for row in exempt_rows
        ),
        key=lambda row: (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"] or "",
        ),
    )


def _verify_epochs(spec: Any, contract: Mapping[str, Any]) -> dict[str, Any]:
    selected_bytes = _epoch_manifest_profile(spec)["selected_bytes"]
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
    if len(rows) != expected_count:
        raise FinancialOutcomeError(
            "all required financial epochs must be numerically OK"
        )
    calculation_cutoff_date = rebuilt.get("calculation_cutoff_date")
    if calculation_cutoff_date is None:
        raise FinancialOutcomeError(
            "finalized epoch product is missing calculation_cutoff_date"
        )
    cutoff = date.fromisoformat(calculation_cutoff_date)
    assignment_only_epochs = _census_assignment_only_epochs(rows, contract, cutoff)
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
        "assignment_only_epochs": assignment_only_epochs,
        "baseline_generation": baseline,
        "panel_generation": panel,
    }


def _verified_value_basis_end(
    spec: Any,
    contract_path: Path,
    controls: Mapping[str, Mapping[str, Any]],
    calculation_cutoff_date: object,
) -> str | None:
    """The end of the required strategies' value basis, when it precedes the cutoff.

    The dependence analysis spans the REQUIRED strategies, so it may end before the
    calculation cutoff only where their common value basis ends (FXB-1): a strategy
    whose native NAV days after the admitted FX end the provider ledger recorded as
    ``OUTSIDE_ADMITTED_FX_BOUNDARY`` findings has no USD value past that end.  The
    end is derived here from verified inputs alone: the provider-ledger catalog of the
    generation this candidate selected, read through its manifest identity, and the
    contract authority it carries.  It is never read from the report or from the
    dependence generation under verification, so a report that merely claims an early
    end cannot explain it.

    ``None`` is the strict end-at-the-cutoff check: no cutoff, no REQUIRED strategy
    bounded (a ledger that records no finding, or only findings of a strategy the
    product excludes, bounds nothing), or an end that is not before the cutoff.  A
    shortfall without a qualifying finding therefore still fails closed, and a ledger
    and contract that do not support one consistent bound do too.
    """

    if calculation_cutoff_date is None:
        return None
    invalid_cutoff = FinancialOutcomeError(
        "report calculation cutoff date is not an ISO date"
    )
    if not isinstance(calculation_cutoff_date, str):
        raise invalid_cutoff
    try:
        cutoff = date.fromisoformat(calculation_cutoff_date)
    except ValueError as exc:
        raise invalid_cutoff from exc
    if cutoff.isoformat() != calculation_cutoff_date:
        raise invalid_cutoff

    from portfolio_hwm_selected_inputs import _source_generation_from_path
    from quant_engine.portfolio_ledger.value_basis_bounds import (
        ValueBasisBoundError,
        derive_value_basis_bounds,
    )

    ledger_path = selected_hwm_source_paths(spec)["provider_ledger_generation_path"]
    try:
        manifest, payloads, _manifest_bytes = _source_generation_from_path(
            ledger_path, "provider-ledger"
        )
    except (OSError, ValueError, GenerationConflictError) as exc:
        raise FinancialOutcomeError(
            f"selected provider ledger generation is not verified: {exc}"
        ) from exc
    # One catalog may be listed under several ids with the same bytes (the synthetic
    # ledger of the MVP-2 product world lists `provider-ledger.json` beside
    # `normalized/provider-ledger.json`); two different catalogs are a conflict.
    catalog_ids: dict[tuple[str, int], list[str]] = {}
    for artifact in manifest.artifacts:
        if artifact.schema_name == "provider-ledger-catalog":
            identity = (artifact.sha256, artifact.size_bytes)
            catalog_ids.setdefault(identity, []).append(artifact.artifact_id)
    readable = [
        artifact_id
        for ids in catalog_ids.values()
        for artifact_id in sorted(ids)
        if artifact_id in payloads
    ]
    if len(catalog_ids) != 1 or not readable:
        raise FinancialOutcomeError(
            "selected provider ledger catalog is absent or ambiguous"
        )
    try:
        catalog = json.loads(payloads[readable[0]])
        contract = json.loads(contract_path.read_bytes())
    except ValueError as exc:
        raise FinancialOutcomeError(
            "selected provider ledger catalog or contract is not JSON"
        ) from exc
    if not isinstance(catalog, dict) or not isinstance(contract, dict):
        raise FinancialOutcomeError(
            "selected provider ledger catalog and contract must be objects"
        )
    if "quality_findings" not in catalog:
        # A catalog that records no quality findings has none to bound anything with.
        # The report reads it the same way (`_report_value_basis_bounds`), and the
        # synthetic ledger of the MVP-2 product world is such a catalog.  A present but
        # malformed value is not absence: it still fails closed in the derivation.
        catalog = {**catalog, "quality_findings": []}
    required = sorted(
        strategy_id
        for strategy_id, control in controls.items()
        if control.get("product_requirement") == "REQUIRED"
    )
    try:
        bounds = derive_value_basis_bounds(catalog, contract, cutoff)
        end = bounds.common_complete_date(required) if bounds.strategies else cutoff
    except ValueBasisBoundError as exc:
        raise FinancialOutcomeError(f"value-basis bound differs: {exc}") from exc
    return end.isoformat() if end < cutoff else None


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
    # The panel carries the report inside index.html (#540). A generation that
    # predates the embed has only the file beside the page, and one that has both
    # must agree.
    panel_files = {file.relative_path for file in epochs["panel_generation"].files}
    embedded_report: bytes | None = None
    if "index.html" in panel_files:
        # Only a page without the embed may fall back to the file: an index that
        # fails its hash or a payload that cannot be read is a broken download.
        _index_path, index_bytes = _generation_file(
            epochs["panel_generation"], "index.html"
        )
        try:
            embedded_report = panel_producer.embedded_download_bytes_if_present(
                index_bytes.decode("utf-8"), panel_producer.REPORT_DOWNLOAD
            )
        except (UnicodeDecodeError, panel_producer.ControlPanelValidationError) as exc:
            raise FinancialOutcomeError(
                f"panel embedded report is unreadable: {exc}"
            ) from exc
    try:
        _path, report_file = _generation_file(
            epochs["panel_generation"], panel_producer.REPORT_DOWNLOAD
        )
    except FinancialOutcomeError:
        report_file = None
    if (
        embedded_report is not None
        and report_file is not None
        and report_file != embedded_report
    ):
        raise FinancialOutcomeError(
            "downloaded report differs from the embedded report"
        )
    downloadable_report = (
        embedded_report if embedded_report is not None else report_file
    )
    if downloadable_report is None:
        raise FinancialOutcomeError("panel carries no downloadable report")
    if downloadable_report != report_bytes:
        raise FinancialOutcomeError("downloaded report differs from selected report")
    report_panel_parity = {
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "panel_report_sha256": hashlib.sha256(downloadable_report).hexdigest(),
    }
    correlation_node = nodes["financial-outcome-dependence-live-only"]
    correlation_generation = _generation(spec, correlation_node)
    correlation = replays[("DEPENDENCE", correlation_node.path.resolve())]
    cutoff = report.get("calculation_cutoff_date")
    # FXB-1: the dependence may end before the cutoff only where the value basis of
    # the required strategies ends.  That end comes from the verified ledger and
    # contract, never from the report this verifier is verifying.
    bounded_end = _verified_value_basis_end(spec, contract_path, controls, cutoff)
    try:
        dependence, rows, _identity, _path = report_producer._dependence_report(
            None,
            generation_dir=correlation_generation.source_dir,
            calculation_cutoff_date=cutoff,
            bounded_complete_through=bounded_end,
        )
    except report_producer.ReportValidationError as exc:
        # The producer's rejection of the dependence generation reaches the caller as
        # this verifier's own error, with the producer's message.
        raise FinancialOutcomeError(str(exc)) from exc
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
        "report_panel_parity": report_panel_parity,
        "decomposition_cells": _verify_decomposition_consumers(
            spec, contract, nodes, replays, series, dashboard, plan_bindings, plan
        ),
        "report_decomposition_cells": _verify_decomposition_consumers(
            spec, contract, nodes, replays, series, report, plan_bindings, plan
        ),
    }


def validate_financial_outcomes(
    spec: Any, *, allow_synthetic: bool = False, caller: str = "unattributed"
) -> dict[str, Any]:
    """Establish all four numerical outcomes from the supplied immutable spec.

    Used by final semantic generation, consumer validation, and shared commit.
    It does not accept readiness text as a financial result or create authority.
    Every invocation writes one ledger file into ``spec.state_root`` naming the
    ``caller``, so a call that drops the returned counters is still counted.
    """
    with _verification_ledger(spec, caller) as (work_counters, calls):
        return _validate_financial_outcomes(spec, allow_synthetic, work_counters, calls)


def validate_financial_outcomes_with_deferred_ledger(
    spec: Any, *, allow_synthetic: bool = False, caller: str = "unattributed"
) -> tuple[dict[str, Any], Callable[[Any], None]]:
    """Like :func:`validate_financial_outcomes`, but let the caller name the ledger spec.

    Some callers (the production-spec builder) can only know their own final
    gate hashes after this verifier returns its proof, because those hashes are
    derived from the proof. Passing the pre-finalized spec to the ordinary
    entry point therefore records the one real invocation under an identity
    that never matches the spec file the caller goes on to emit.

    This performs that one real verification immediately -- the returned work
    counters and call records are the actual evidence of that call, not a
    second warm repeat of it -- and returns a finalizer the caller must invoke
    exactly once with the spec whose ``spec_sha256`` should be recorded in the
    ledger. The finalizer should be invoked from a ``finally`` block so the
    ledger is still written (under the best identity available) if the caller
    fails before it can finalize its own spec.
    """

    if _CALLER_LABEL.fullmatch(caller) is None:
        raise FinancialOutcomeError("financial verification caller label is invalid")
    work_counters = {key: 0 for key in _WORK_COUNTER_KEYS}
    calls: list[dict[str, Any]] = []
    written = False

    def finalize(final_spec: Any) -> None:
        nonlocal written
        if written:
            raise FinancialOutcomeError(
                "financial verification ledger is already finalized"
            )
        written = True
        _write_verification_ledger(final_spec, caller, work_counters, calls)

    try:
        proof = _validate_financial_outcomes(
            spec, allow_synthetic, work_counters, calls
        )
    except BaseException:
        finalize(spec)
        raise
    return proof, finalize


def _validate_financial_outcomes(
    spec: Any,
    allow_synthetic: bool,
    work_counters: dict[str, int],
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
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
    # The binding identifies the generation by digest, not by where it sits.
    # The spec already resolves that digest to a node, so take the path from
    # there rather than from the payload: a published financial artifact that
    # carried its own build location could only ever re-verify from it.
    capital_input_node = _artifact_for_digest(
        spec, capital_input["sha256"], capital_input["size_bytes"]
    )
    # Compensating binding for the containment exemption above. `_artifact_for_digest`
    # matches on sha256+size across every ArtifactNode and never checks WHICH node it
    # found, so the capital result's declared input could be satisfied by any artifact
    # carrying those bytes. Requiring the declared capital input row ties the result to
    # the input the production inventory declares, which nothing checked before.
    if capital_input_node.node_id != "financial-capital-input":
        raise FinancialOutcomeError(
            "selected capital input is not the declared capital input artifact"
        )
    verify_selected_capital_ledger(
        capital_input_node.path,
        selected_sources["provider_ledger_generation_path"],
    )
    capital = verify_governed_capital_result(
        capital, capital_input_node.path, contract_path
    )
    if capital.get("status") != "AVAILABLE" or (
        not allow_synthetic and capital.get("authority_profile") != "ACCEPTED_GOVERNED"
    ):
        raise FinancialOutcomeError("capital result is unavailable or synthetic")
    replays: dict[tuple[str, Path], dict[str, Any]] = {}
    replay_records: dict[tuple[str, Path], dict[str, Any]] = {}
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
                work_counters=work_counters,
                calls=calls,
            )
            replay_records[cache_key] = calls[-1]
        elif node_id not in replay_records[cache_key]["node_ids"]:
            replay_records[cache_key]["node_ids"].append(node_id)
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
        "assignment_only_epochs": epochs["assignment_only_epochs"],
        "capital_result_id": capital["result_id"],
        "selected_cells": selected_cells,
        "consumers": consumers,
        "artifact_sha256s": {key: node.sha256 for key, node in sorted(nodes.items())},
        "work_counters": work_counters,
    }

"""One selected synthetic source set through every actual financial consumer."""

from __future__ import annotations

import collections
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
import types

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


# The narrowed load_contract skips validate_contract.  This is a declared
# residual of the synthetic world, not a proof that validate_contract passes:
# it is recorded on both load_contract entries below.  validate_contract is
# non-transforming (it raises or returns dict(contract),
# tools/portfolio_financial_mvp_contract.py:6516-6612; load_contract is
# schema_errors plus validate_contract, :6615-6655), so skipping it changes no
# value loaded in this chain.  The three rejections the real, unpatched loader
# gives this synthetic world, captured by running it against the final bound
# contract, are properties of the world and not of the code under test.
_LOAD_CONTRACT_RESIDUAL = (
    "narrowed synthetic authority: the real schema_errors on the accepted "
    "contract bytes, then exact selected native-validated outcomes, live "
    "_validate_outcomes, canonical census, strategy-perimeter and "
    "registry-hash equivalence.  It SKIPS validate_contract, which the real "
    "load_contract runs after the schema check.  validate_contract is "
    "non-transforming (it raises or returns dict(contract), "
    "tools/portfolio_financial_mvp_contract.py:6516-6612), so skipping it "
    "changes no value loaded in this chain.  The real unpatched loader rejects "
    "this synthetic world three ways, all properties of the world and not of "
    "the code under test: (1) with the plan registry and the epoch-manifest "
    "bundle: FinancialMvpContractError: current-product strategy order/identity "
    "differs from the registry; (2) with the repository registry and the "
    "bundle: FinancialMvpContractError: source strategy-backtests mixed version "
    "status requires a nonempty proper subset of subject refs; (3) without the "
    "bundle, the world's contract being schema v7: FinancialMvpContractError: "
    "contract schema v7 requires a verified epoch-manifest bundle.  Fix the "
    "synthetic world builder, then delete this entry"
)

# Every substitution the 172-outcome test world makes through MonkeyPatch, keyed
# by the canonical record (patched target, attribute) and declared with the
# number of setattr calls made on it and the reason.  The target is the import
# name of the patched module: the fixtures load several module instances of one
# file under different names, and each instance is its own target.  A count is
# an int, or an inclusive (minimum, maximum) where the fixture's own dynamic
# scan makes it depend on the session's import history.  The test compares
# these records with the ones its MonkeyPatch actually holds, so an undeclared
# substitution -- a new attribute, the same attribute on another target, or one
# more call on a declared record -- turns it red; the negative controls below
# prove it.
PATCH_INVENTORY: dict[tuple[str, str], tuple[int | tuple[int, int], str]] = {
    ("importlib.util", "spec_from_file_location"): (
        1,
        "wrapper (mvp2_consumers_fixture._install_dynamic_dependence_loader) that "
        "alters only the report's freshly loaded dependence module, which then "
        "resolves the narrowed contract module; process-wide until teardown",
    ),
    ("portfolio_control_panel", "DEPENDENCE_POLICY"): (
        1,
        "panel reads the world's dependence policy",
    ),
    ("portfolio_control_panel", "FINANCIAL_MVP_CONTRACT"): (
        1,
        "panel binds the world's product contract",
    ),
    ("portfolio_control_panel", "SOURCE_CONTRACT"): (
        1,
        "panel binds a synthetic frozen legacy-appendix contract",
    ),
    ("portfolio_control_panel", "_PORTABLE_HWM_POLICY"): (
        1,
        "panel reads the world's HWM policy",
    ),
    ("portfolio_control_panel", "_PORTABLE_HWM_PRODUCT_CONTRACT"): (
        1,
        "panel binds the world's product contract",
    ),
    ("portfolio_current_decomposition_plan", "_layered_history_module"): (
        2,
        "pin the planner and current-plan verifier to the one layered-history "
        "module instance that carries the synthetic seams (set by "
        "build_decomposition_vertical, then again by "
        "install_synthetic_financial_authority)",
    ),
    ("portfolio_current_dependence", "_financial_module"): (
        1,
        "current dependence resolves the same narrowed contract module",
    ),
    ("portfolio_financial_mvp_contract", "load_contract"): (
        2,
        _LOAD_CONTRACT_RESIDUAL
        + ".  Two calls: the fixture's sys.modules scan (this module holds the "
        "original) and its explicit patch",
    ),
    ("portfolio_hwm_selected_inputs", "_GOVERNED_MEMBERSHIP_CENSUS"): (
        1,
        "HWM replay binds the world's declared census; the repository census "
        "cannot bound a synthetic account source set",
    ),
    ("portfolio_invested_capital", "load_contract"): (
        (1, 2),
        _LOAD_CONTRACT_RESIDUAL
        + ".  One call (the explicit patch) or two: the fixture's sys.modules "
        "scan also patches this module when it still holds the original "
        "function, which depends on whether portfolio_financial_mvp_contract "
        "was swapped for a fresh instance at collection",
    ),
    ("portfolio_layered_history_fixture_for_current_plan", "_history_fixture"): (
        1,
        "plan_tests._fixture builds the layered history from this world's "
        "prepared fixture instead of a fresh one",
    ),
    ("portfolio_pipeline_materialize", "ACCOUNT_STRATEGY_CLASSIFICATION_POLICY"): (
        1,
        "materializer reads the world's flow-classification policy",
    ),
    ("portfolio_pipeline_materialize", "HWM_GOVERNED_POLICY"): (
        1,
        "materializer reads the world's HWM policy",
    ),
    ("portfolio_report", "CURRENT_EPOCH_CROSSWALK_AS_OF_DATE"): (
        1,
        "report binds the world's crosswalk as-of",
    ),
    ("portfolio_report", "CURRENT_EPOCH_CROSSWALK_CUTOFF_DATE"): (
        1,
        "report binds the world's crosswalk calculation cutoff",
    ),
    ("portfolio_report", "DEPENDENCE_POLICY"): (
        1,
        "report reads the world's dependence policy",
    ),
    ("portfolio_report", "FLOW_CLASSIFICATION_POLICY_CONTRACT"): (
        1,
        "report binds the world's flow-classification policy",
    ),
    ("portfolio_report", "_HWM_POLICY_PRODUCT_AUTHORITY_CONTRACT"): (
        1,
        "report binds the world's product contract as HWM policy authority",
    ),
    ("portfolio_report", "_PORTABLE_HWM_POLICY"): (
        1,
        "report reads the world's HWM policy",
    ),
    ("portfolio_report", "_PORTABLE_HWM_PRODUCT_CONTRACT"): (
        1,
        "report binds the world's product contract",
    ),
    (
        "portfolio_target_series_for_current_decomposition_plan",
        "_current_source_admission_authority",
    ): (
        2,
        "synthetic admission receipt on the planner's verifier module instance: "
        "this world has no real provider capture perimeter, and the real "
        "receipt binding has its own tests (set by build_decomposition_vertical, "
        "then again by install_synthetic_financial_authority)",
    ),
    ("portfolio_target_series_for_current_decomposition_plan", "_history_contract"): (
        2,
        "structure-only history-contract parser on the planner's verifier module "
        "instance, whose real reader runs validate_contract; the second install "
        "(install_synthetic_financial_authority) first runs the narrowed "
        "load_contract",
    ),
    (
        "portfolio_target_series_for_current_decomposition_plan",
        "_portfolio123_version_equivalence",
    ): (
        2,
        "synthetic P123 version-equivalence stand-in on the planner's verifier "
        "module instance, for the same reason",
    ),
    (
        "portfolio_target_series_layered_history",
        "_current_source_admission_authority",
    ): (
        2,
        "synthetic admission receipt on the layered-history producer module "
        "instance (set by build_shared_history_source, then again by "
        "build_decomposition_vertical)",
    ),
    ("portfolio_target_series_layered_history", "_history_contract"): (
        2,
        "structure-only history-contract parser on the layered-history producer "
        "module instance, whose real reader runs validate_contract (installed by "
        "build_shared_history_source, then again by build_decomposition_vertical)",
    ),
    ("portfolio_target_series_layered_history", "_portfolio123_version_equivalence"): (
        2,
        "synthetic P123 version-equivalence stand-in on the layered-history "
        "producer module instance, for the same reason",
    ),
    ("quant_engine.core.release", "_verify_release_validation"): (
        2,
        "tests/core/conftest.py neuters it for mechanics tests, then this test "
        "restores the real verifier, so the whole chain runs with it live",
    ),
    ("tools.portfolio_current_dependence", "_financial_module"): (
        1,
        "the panel's package-qualified copy of the current-dependence module "
        "resolves the same narrowed contract module",
    ),
}
# Substitutions that are not MonkeyPatch operations, so the assertion cannot see
# them (listed for review only):
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


def _patch_target_name(target: object) -> str:
    """Canonical name of a patched target: a module's import name, else its type."""
    if isinstance(target, types.ModuleType):
        return target.__name__
    owner = target if isinstance(target, type) else type(target)
    return f"{owner.__module__}.{owner.__qualname__}"


def _declared_count_bounds(count: int | tuple[int, int]) -> tuple[int, int]:
    return (count, count) if isinstance(count, int) else count


def patch_inventory_problems(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Differences between what a MonkeyPatch substituted and PATCH_INVENTORY.

    A record is (patched target, attribute) and its multiplicity is the number
    of setattr calls made on it.  The declared table must equal the real set: an
    undeclared record, a declared record never made, and a multiplicity outside
    the declared bounds are reported, and so is any other MonkeyPatch operation
    (setitem, chdir, sys.path), which the inventory does not declare.
    """
    actual = collections.Counter(
        (_patch_target_name(target), name)
        for target, name, _prior in monkeypatch._setattr
    )
    problems = []
    for record in sorted(set(actual) | set(PATCH_INVENTORY)):
        declared = PATCH_INVENTORY[record][0] if record in PATCH_INVENTORY else 0
        low, high = _declared_count_bounds(declared)
        if not low <= actual[record] <= high:
            expected = str(low) if low == high else f"{low}..{high}"
            problems.append(
                f"{record[0]}.{record[1]}: declared {expected}, "
                f"patched {actual[record]}"
            )
    if monkeypatch._setitem:
        problems.append(f"undeclared setitem operations: {len(monkeypatch._setitem)}")
    if monkeypatch._cwd is not None:
        problems.append("undeclared chdir")
    if monkeypatch._savesyspath is not None:
        problems.append("undeclared sys.path change")
    return problems


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


def test_d1_fixture_native_ready_authority(tmp_path: Path) -> None:
    authority = fixture.build_synthetic_ready_financial_authority(tmp_path)
    selected = financial.load_contract(authority)
    policy = next(
        row["calculation_contract"]
        for row in selected["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    assert (
        policy["policy_status"],
        policy["shared_flow_contract"]["extension_status"],
    ) == (
        "READY",
        "READY",
    )


@pytest.mark.parametrize("readiness", ["READY", "UNKNOWN", None])
def test_d1_fixture_status_only_ready_is_rejected(tmp_path: Path, readiness) -> None:
    contract = json.loads(
        (
            fixture.REPOSITORY_ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    if readiness is None:
        policy.pop("policy_status")
    else:
        policy["policy_status"] = readiness
    path = fixture._write_json(tmp_path / "invalid-readiness.json", contract)
    with pytest.raises(financial.FinancialMvpContractError):
        financial.load_contract(path)


@pytest.mark.parametrize("mutation", ["missing", "extra", "payload"])
def test_d1_fixture_receipt_pair_and_payload_are_live(tmp_path: Path, mutation) -> None:
    authority = fixture.build_synthetic_ready_financial_authority(tmp_path)
    contract = json.loads(authority.read_bytes())
    policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    receipts = policy["owner_decision_receipts"]
    if mutation == "missing":
        receipts.pop("PROVIDER_CUSTODY_PERIMETER")
    elif mutation == "extra":
        receipts["UNPAIRED"] = deepcopy(receipts["PROVIDER_CUSTODY_PERIMETER"])
    else:
        receipts["PROVIDER_CUSTODY_PERIMETER"]["payload_sha256"] = "0" * 64
    with pytest.raises(financial.FinancialMvpContractError):
        financial.load_contract(
            fixture._write_json(tmp_path / "invalid-receipt.json", contract)
        )


def test_d1_fixture_selected_authority_is_exact(tmp_path: Path, monkeypatch) -> None:
    import portfolio_invested_capital as capital

    authority = fixture.build_synthetic_ready_financial_authority(
        tmp_path / "authority"
    )
    expected = financial.load_contract(authority)
    candidate = fixture._write_json(
        tmp_path / "selected-world.json", deepcopy(expected)
    )
    native_validator = financial._validate_outcomes
    native_receipts = capital._validate_owner_decision_receipts
    native_calculator = capital.calculate_invested_capital
    loader = fixture.install_synthetic_financial_authority(
        monkeypatch, [candidate], financial_outcomes_authority_path=authority
    )
    assert loader(candidate)["financial_outcomes"] == expected["financial_outcomes"]
    assert financial._validate_outcomes is native_validator
    assert capital._validate_owner_decision_receipts is native_receipts
    assert capital.calculate_invested_capital is native_calculator
    # The repository RNR outcomes are another natively valid authority, but
    # they are not the exact READY selection frozen by this fixture seam.
    different = json.loads(
        (
            fixture.REPOSITORY_ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
        ).read_bytes()
    )
    native_validator(different["financial_outcomes"], allow_legacy_workbook=False)
    changed = deepcopy(expected)
    changed["financial_outcomes"] = different["financial_outcomes"]
    fixture._write_json(candidate, changed)
    with pytest.raises(AssertionError):
        loader(candidate)


def test_d1_fixture_default_authority_stays_blocked() -> None:
    contract = financial.load_contract(
        fixture.REPOSITORY_ROOT / "contracts/portfolio-financial-mvp-source-v1.json"
    )
    policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    assert policy["policy_status"] == "REQUIRED_NOT_READY"


def test_d1_fixture_governed_numeric_projection_is_unchanged(tmp_path: Path) -> None:
    capital_test = fixture._load(
        "d1_fixture_capital_case",
        "tests/portfolio_analytics/test_portfolio_invested_capital.py",
    )
    real_root = tmp_path / "real"
    real_root.mkdir()
    input_path, real_contract_path = capital_test._governed_case(real_root)
    import portfolio_invested_capital as capital

    real = capital.build_governed_capital_result(input_path, real_contract_path)
    ready_contract_path = fixture.build_synthetic_ready_financial_authority(
        tmp_path / "ready"
    )
    ready_contract = capital.load_contract(ready_contract_path)
    inputs = json.loads(input_path.read_bytes())
    inputs["product_contract_sha256"] = hashlib.sha256(
        ready_contract_path.read_bytes()
    ).hexdigest()
    inputs["policy_sha256"] = capital.invested_capital_policy_digest(ready_contract)
    ready_input_path = fixture._write_json(tmp_path / "ready-input.json", inputs)
    capital.write_local_sidecar(
        ready_input_path,
        capital.read_local_sidecar(input_path),
    )
    ready = capital.build_governed_capital_result(ready_input_path, ready_contract_path)
    assert (real["status"], ready["status"]) == ("REQUIRED_NOT_READY", "AVAILABLE")
    selected_ledger = json.loads(
        Path(capital.read_local_sidecar(input_path)["provider_ledger"]).read_bytes()
    )
    raw_total = sum(
        (Decimal(row["amount"]["value"]) for row in selected_ledger["capital_anchors"]),
        Decimal(0),
    )
    assert raw_total == Decimal("1100") == Decimal(ready["current_aggregate_usd"])
    assert real["authority_profile"] == ready["authority_profile"] == "SYNTHETIC"
    for field in (
        "events",
        "source_bindings",
        "required_epochs",
        "daily_by_epoch",
        "daily_by_strategy",
        "daily_aggregate",
        "current_by_strategy",
        "current_aggregate_usd",
        "missing_inputs",
    ):
        assert ready[field] == real[field]
    assert (
        capital.verify_governed_capital_result(
            ready, ready_input_path, ready_contract_path
        )
        == ready
    )
    tampered = dict(ready)
    tampered["status"] = "REQUIRED_NOT_READY"
    tampered.pop("result_id")
    tampered["result_id"] = capital._digest(tampered)
    with pytest.raises(capital.CapitalValidationError, match="recomputation"):
        capital.verify_governed_capital_result(
            tampered, ready_input_path, ready_contract_path
        )


def test_one_source_set_reaches_all_172_outcomes_and_both_consumers(
    tmp_path, monkeypatch, real_verify_release_validation
):
    """Green by validation, with one declared residual: the narrowed loader.

    Every substitution the chain makes is declared in PATCH_INVENTORY and
    compared, as (target, attribute) records with their multiplicity, with the
    substitutions actually made.  The final bound contract passes the real
    checked-in schema, carries the selected world's declared Matrixport outage
    (never the empty policy), and a gap without a policy fails both the schema
    and the narrowed loader.  The narrowed loader skips validate_contract; the
    PATCH_INVENTORY entries for load_contract state why that is a residual of
    the synthetic world and not a proof that validate_contract passes.
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
    ready_authority = fixture.build_synthetic_ready_financial_authority(
        root / "authority"
    )
    prepared, selected = fixture.build_shared_history_source(
        root / "s",
        monkeypatch,
        financial_outcomes_authority_path=ready_authority,
    )
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
        financial_outcomes_authority_path=ready_authority,
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
    # Release validation stayed live, and every substitution the chain made is
    # declared with its reason.  MonkeyPatch keeps (target, name, prior value)
    # for each setattr until teardown.
    assert release_module._verify_release_validation is real_verify_release_validation
    assert patch_inventory_problems(monkeypatch) == []


def _replay_inventory(
    monkeypatch: pytest.MonkeyPatch, *, skip: tuple[str, str] | None = None
) -> dict[str, types.ModuleType]:
    """Make exactly the declared substitutions on stand-in modules."""
    targets: dict[str, types.ModuleType] = {}
    for (target, attribute), (count, _reason) in PATCH_INVENTORY.items():
        module = targets.setdefault(target, types.ModuleType(target))
        setattr(module, attribute, object())
        if (target, attribute) == skip:
            continue
        for _ in range(_declared_count_bounds(count)[1]):
            monkeypatch.setattr(module, attribute, object())
    return targets


def test_patch_inventory_accepts_exactly_the_declared_substitutions() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        _replay_inventory(monkeypatch)
        assert patch_inventory_problems(monkeypatch) == []


def test_patch_inventory_accepts_either_bound_of_a_declared_range() -> None:
    record = ("portfolio_invested_capital", "load_contract")
    low, high = _declared_count_bounds(PATCH_INVENTORY[record][0])
    assert (low, high) == (1, 2)
    with pytest.MonkeyPatch.context() as monkeypatch:
        targets = _replay_inventory(monkeypatch, skip=record)
        for _ in range(low):
            monkeypatch.setattr(targets[record[0]], record[1], object())
        assert patch_inventory_problems(monkeypatch) == []


def test_patch_inventory_rejects_an_extra_call_on_a_declared_record() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        targets = _replay_inventory(monkeypatch)
        monkeypatch.setattr(targets["portfolio_report"], "DEPENDENCE_POLICY", object())
        assert patch_inventory_problems(monkeypatch) == [
            "portfolio_report.DEPENDENCE_POLICY: declared 1, patched 2"
        ]


def test_patch_inventory_rejects_an_extra_load_contract_replacement() -> None:
    """The dynamic load_contract substitutions are individually declared."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        targets = _replay_inventory(monkeypatch)
        monkeypatch.setattr(
            targets["portfolio_financial_mvp_contract"], "load_contract", object()
        )
        assert patch_inventory_problems(monkeypatch) == [
            "portfolio_financial_mvp_contract.load_contract: declared 2, patched 3"
        ]
    with pytest.MonkeyPatch.context() as monkeypatch:
        targets = _replay_inventory(monkeypatch)
        monkeypatch.setattr(
            targets["portfolio_invested_capital"], "load_contract", object()
        )
        assert patch_inventory_problems(monkeypatch) == [
            "portfolio_invested_capital.load_contract: declared 1..2, patched 3"
        ]


def test_patch_inventory_rejects_a_declared_name_on_an_undeclared_target() -> None:
    other = types.ModuleType("portfolio_some_other_module")
    other.load_contract = object()  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as monkeypatch:
        _replay_inventory(monkeypatch)
        monkeypatch.setattr(other, "load_contract", object())
        assert patch_inventory_problems(monkeypatch) == [
            "portfolio_some_other_module.load_contract: declared 0, patched 1"
        ]


def test_patch_inventory_rejects_a_declared_substitution_that_was_not_made() -> None:
    record = ("portfolio_hwm_selected_inputs", "_GOVERNED_MEMBERSHIP_CENSUS")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _replay_inventory(monkeypatch, skip=record)
        assert patch_inventory_problems(monkeypatch) == [
            "portfolio_hwm_selected_inputs._GOVERNED_MEMBERSHIP_CENSUS: "
            "declared 1, patched 0"
        ]


def test_patch_inventory_rejects_other_monkeypatch_operations() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        _replay_inventory(monkeypatch)
        monkeypatch.setitem({}, "key", "value")
        assert patch_inventory_problems(monkeypatch) == [
            "undeclared setitem operations: 1"
        ]

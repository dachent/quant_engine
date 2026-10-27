"""Financial expectations derived from owner cash, not terminal account NAV."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

import portfolio_invested_capital as capital
import portfolio_financial_mvp_contract as financial


def _case():
    contract = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text()
    )
    policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    epochs = [
        {
            "epoch_id": "a-1",
            "strategy_id": "A",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
        {
            "epoch_id": "b-1",
            "strategy_id": "B",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]
    return dict(
        policy=policy,
        required_epochs=epochs,
        evaluation_date="2026-01-05",
        complete_through="2026-01-03",
        opening_basis=[
            _event("open-a", "a-1", "2026-01-01", "OPENING_CONTRIBUTED_BASIS", "100"),
            _event("open-b", "b-1", "2026-01-01", "OPENING_CONTRIBUTED_BASIS", "50"),
        ],
        events=[],
        flow_coverage=[
            {
                "epoch_id": e["epoch_id"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-03",
                "evidence_sha256": "c" * 64,
            }
            for e in epochs
        ],
        source_bindings={"ledger": {"sha256": "a" * 64, "size_bytes": 100}},
        correction_bindings=[],
    )


def _unresolved_shared_flow_policy():
    """Build a complete local #324 dependency state, not a status-only stand-in."""
    contract = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json"
        ).read_text()
    )
    outcomes = deepcopy(contract["financial_outcomes"])
    invested = next(row for row in outcomes if row["outcome_id"] == "INVESTED_CAPITAL")
    policy = invested["calculation_contract"]
    policy["shared_flow_contract"]["extension_status"] = "REQUIRED_NOT_READY"
    policy["policy_status"] = "REQUIRED_NOT_READY"
    invested["readiness"] = "REQUIRED_NOT_READY"
    invested["blockers"] = [324]
    financial._validate_outcomes(outcomes, allow_legacy_workbook=False)
    return policy


def _event(event_id, epoch_id, day, classification, amount, **extra):
    return {
        "event_id": event_id,
        "epoch_id": epoch_id,
        "date": day,
        "timestamp": None,
        "classification": classification,
        "amount": amount,
        "currency": "USD",
        "fx_rate": "1",
        "fx_evidence_sha256": "d" * 64,
        "source_evidence_sha256": "e" * 64,
        **extra,
    }


def _governed_case(tmp_path):
    """Synthetic source facts: 100 owner dollars per sleeve, no investment gains.

    Two TT-to-QSI transitions carry the same 100 contributed dollars. Eleven
    distinct sleeves therefore finish at 1,100, independent of their NAV.
    """
    contract_path = (
        Path(__file__).parents[2] / "contracts/portfolio-financial-mvp-source-v1.json"
    )
    contract = capital.load_contract(contract_path)
    required = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    }
    inputs = {
        "schema_version": 1,
        "authority_profile": "SYNTHETIC",
        "product_contract_sha256": hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest(),
        "policy_sha256": capital.invested_capital_policy_digest(contract),
        "required_epochs": [],
        "evaluation_date": "2026-01-23",
        "complete_through": "2026-01-22",
        "opening_basis": [],
        "events": [],
        "flow_coverage": [],
        "source_bindings": {},
        "correction_bindings": [],
    }
    ledger = {"capital_anchors": [], "flows": [], "admissions": []}
    for sleeve in contract["current_census"]["account_sleeves"]:
        chain = sorted(
            (
                row
                for row in sleeve["assignments"]
                if row["strategy_id"] in required
                and row["authority"] != financial.DERIVED_SLEEVE_EPOCH_AUTHORITY
            ),
            key=lambda row: row["effective_start"],
        )
        if not chain:
            continue
        account = sleeve["sleeve_alias"]
        coverage = {
            "account_id": account,
            "fact_type": "flows",
            "disposition": "AUTHORITATIVE",
            "effective_start": chain[0]["effective_start"],
            "effective_end": inputs["complete_through"],
        }
        ledger["admissions"].append(coverage)
        previous = None
        for assignment in chain:
            epoch = {
                "epoch_id": account + "-" + assignment["effective_start"],
                "sleeve_alias": account,
                "account_id": account,
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
            }
            inputs["required_epochs"].append(epoch)
            inputs["flow_coverage"].append(
                {
                    "epoch_id": epoch["epoch_id"],
                    "start_date": epoch["effective_start"],
                    "end_date": epoch["effective_end"] or inputs["complete_through"],
                    "evidence_sha256": capital._digest(coverage),
                }
            )
            if previous is None:
                fact = {
                    "record_id": epoch["epoch_id"] + "-opening",
                    "account_id": account,
                    "strategy_id": epoch["strategy_id"],
                    "capital_type": "OPENING_CONTRIBUTED_BASIS",
                    "event_date": epoch["effective_start"],
                    "amount": {
                        "status": "AVAILABLE",
                        "value": "100",
                        "currency": "USD",
                    },
                }
                ledger["capital_anchors"].append(fact)
                digest = capital._digest(fact)
                inputs["opening_basis"].append(
                    _event(
                        fact["record_id"],
                        epoch["epoch_id"],
                        fact["event_date"],
                        "OPENING_CONTRIBUTED_BASIS",
                        "100",
                        source_evidence_sha256=digest,
                        fx_evidence_sha256=digest,
                    )
                )
            previous = epoch
    ledger_path = tmp_path / "provider-ledger.json"
    ledger_path.write_bytes(capital._canonical(ledger))
    inputs["source_bindings"]["provider_ledger"] = capital._file_binding(ledger_path)[0]
    provenance = {
        "as_of_date": "2026-01-22",
        "accounts": [
            {"account_id": account}
            for account in sorted(
                {row["account_id"] for row in inputs["required_epochs"]}
            )
        ],
        "assignments": [
            {
                **{
                    key: row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                },
                "mapping_status": "MAPPED",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
            for row in inputs["required_epochs"]
        ],
    }
    provenance_path = tmp_path / "portfolio-provenance.json"
    provenance_path.write_bytes(capital._canonical(provenance))
    spec_path = tmp_path / "provider-ledger-spec.json"
    spec_path.write_bytes(
        capital._canonical(
            {
                "provenance": {
                    "catalog_path": str(provenance_path),
                    "catalog_sha256": capital._file_binding(provenance_path)[0][
                        "sha256"
                    ],
                    "generation_id": "a" * 64,
                }
            }
        )
    )
    inputs["source_bindings"]["provider_ledger_spec"] = capital._file_binding(
        spec_path
    )[0]
    input_path = tmp_path / "capital-input-generation.json"
    input_path.write_bytes(capital._canonical(inputs))
    # The generation is portable; the locators live beside it, as the real
    # producer writes them.
    capital.write_local_sidecar(
        input_path,
        {"provider_ledger": ledger_path, "provider_ledger_spec": spec_path},
    )
    return input_path, contract_path


def test_governed_capital_replays_exact_thirteen_epoch_source_facts(tmp_path):
    input_path, contract_path = _governed_case(tmp_path)
    result = capital.build_governed_capital_result(input_path, contract_path)
    assert result["authority_profile"] == "SYNTHETIC"
    assert len(result["required_epochs"]) == 13
    assert len(result["required_strategy_ids"]) == 7
    assert result["daily_aggregate"][0]["invested_capital_usd"] == "100"
    assert result["current_aggregate_usd"] == "1100"
    assert {
        row["strategy_id"]: row["invested_capital_usd"]
        for row in result["current_by_strategy"]
    } == {
        "p123-ai-factor": "100",
        "p123-eu": "100",
        "qsi": "200",
        "tt-portfolio": "400",
        "volatis-strategy": "100",
        "grizzlybears-vtma": "100",
        "robuxio-crypto": "100",
    }
    assert (
        capital.verify_governed_capital_result(result, input_path, contract_path)
        == result
    )
    result["current_aggregate_usd"] = "1101"
    with pytest.raises(capital.CapitalValidationError, match="recomputation"):
        capital.verify_governed_capital_result(result, input_path, contract_path)


def test_t5c_custody_governed_builder_and_verifier_keep_generic_opening_flow(
    tmp_path,
):
    input_path, contract_path = _governed_case(tmp_path)
    arguments = json.loads(input_path.read_bytes())
    ledger_path = tmp_path / "provider-ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    opening = next(
        row
        for row in ledger["capital_anchors"]
        if row.get("capital_type") == "OPENING_CONTRIBUTED_BASIS"
    )
    ledger["capital_anchors"].remove(opening)
    generic_opening = {
        **opening,
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "provider_account_scope": "ACCOUNT_ONLY",
    }
    ledger["flows"].append(generic_opening)
    ledger["capital_anchors"].append(
        {
            **opening,
            "record_id": "synthetic-generic-opening-nav-anchor",
            "capital_type": "OPENING_CAPITAL_ANCHOR",
            "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
            "authority_profile": "SYNTHETIC",
        }
    )
    digest = capital._digest(generic_opening)
    for event in arguments["opening_basis"]:
        if event["event_id"] == opening["record_id"]:
            event["source_evidence_sha256"] = digest
            event["fx_evidence_sha256"] = digest
            break
    else:
        raise AssertionError("selected opening source has no admitted event")

    ledger_path.write_bytes(capital._canonical(ledger))
    arguments["source_bindings"]["provider_ledger"] = capital._file_binding(
        ledger_path
    )[0]
    input_path.write_bytes(capital._canonical(arguments))

    result = capital.build_governed_capital_result(input_path, contract_path)
    assert result["current_aggregate_usd"] == "1100"
    assert (
        capital.verify_governed_capital_result(result, input_path, contract_path)
        == result
    )


def _t5c_constructor_world(tmp_path):
    """The synthetic world the native capital-input constructor reads.

    The pre-epoch deposit carries 32 significant digits, wider than the ambient
    28-digit context.  Returns the constructor module, its keyword arguments, the
    epoch row and the deposit source row.
    """
    from quant_engine.core import GenerationManifest
    from quant_engine.core.contracts import canonical_json_bytes
    from quant_engine.core.publication import (
        read_generation_bundle,
        stage_generation_bundle,
    )

    repo = Path(__file__).resolve().parents[2]
    fixture_path = repo / "tests" / "portfolio_ledger" / "test_hwm_selected_inputs.py"
    fixture_spec = importlib.util.spec_from_file_location(
        "t5c_constructor_hwm_fixture", fixture_path
    )
    assert fixture_spec is not None and fixture_spec.loader is not None
    fixture = importlib.util.module_from_spec(fixture_spec)
    sys.modules[fixture_spec.name] = fixture
    fixture_spec.loader.exec_module(fixture)

    cli_path = repo / "ops" / "mvp2-release" / "scripts" / "build_capital_input.py"
    cli_spec = importlib.util.spec_from_file_location("t5c_constructor_cli", cli_path)
    assert cli_spec is not None and cli_spec.loader is not None
    cli = importlib.util.module_from_spec(cli_spec)
    sys.modules[cli_spec.name] = cli
    cli_spec.loader.exec_module(cli)

    paths = fixture._fixture(tmp_path / "native-world", bind_epoch_contract=False)
    epoch_root = paths["generation"].parent
    epoch_manifest = read_generation_bundle(epoch_root.parents[1], epoch_root.name)
    epoch_document = json.loads(
        epoch_manifest.payloads["outputs/account-strategy-manifest.json"]
    )
    epoch = next(
        row
        for row in epoch_document["rows"]
        if sum(
            candidate["account_id"] == row["account_id"]
            for candidate in epoch_document["rows"]
        )
        == 1
    )

    provider_path = paths["provider_generation"]
    provider_bundle = read_generation_bundle(
        provider_path.parents[2], provider_path.parent.name
    )
    ledger = json.loads(provider_bundle.payloads["normalized/provider-ledger.json"])
    deposit_date = (
        date.fromisoformat(epoch["effective_start"]) - timedelta(days=1)
    ).isoformat()
    deposit = {
        "record_id": "t5c-pre-epoch-precision-deposit",
        "account_id": epoch["account_id"],
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "provider_account_scope": "ACCOUNT_ONLY",
        "capital_disposition": "EXTERNAL_CAPITAL",
        "event_date": deposit_date,
        "amount": {
            "value": "100.00000000000000000000000000002",
            "currency": "USD",
            "status": "AVAILABLE",
        },
    }
    ledger["flows"] = [deposit]
    ledger["capital_anchors"] = [
        {
            "record_id": "t5c-epoch-opening-anchor",
            "account_id": epoch["account_id"],
            "strategy_id": epoch["strategy_id"],
            "capital_type": "OPENING_CAPITAL_ANCHOR",
            "event_date": epoch["effective_start"],
            "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
        }
    ]
    ledger["admissions"] = [
        {
            "account_id": row["account_id"],
            "fact_type": "flows",
            "disposition": "AUTHORITATIVE",
            "effective_start": (
                date.fromisoformat(row["effective_start"]) - timedelta(days=1)
            ).isoformat(),
            "effective_end": epoch_document["calculation_cutoff_date"],
        }
        for row in epoch_document["rows"]
    ]

    provider_payloads = dict(provider_bundle.payloads)
    ledger_bytes = canonical_json_bytes(ledger)
    for artifact_id in provider_payloads:
        if artifact_id.endswith("provider-ledger.json"):
            provider_payloads[artifact_id] = ledger_bytes
    provider_payloads["provider-ledger.json"] = ledger_bytes
    artifacts = []
    for artifact in provider_bundle.manifest.artifacts:
        payload = provider_payloads[artifact.artifact_id]
        artifacts.append(
            replace(
                artifact,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )
        )
    if not any(
        artifact.artifact_id == "provider-ledger.json" for artifact in artifacts
    ):
        artifacts.append(
            replace(
                artifacts[0],
                artifact_id="provider-ledger.json",
                sha256=hashlib.sha256(ledger_bytes).hexdigest(),
                size_bytes=len(ledger_bytes),
            )
        )
    provider_manifest = GenerationManifest.build(
        as_of_date=provider_bundle.manifest.as_of_date,
        adapter=provider_bundle.manifest.adapter,
        coverage=provider_bundle.manifest.coverage,
        artifacts=tuple(artifacts),
        verification=provider_bundle.manifest.verification,
    )
    selected_provider_root = stage_generation_bundle(
        tmp_path / "selected-provider-ledger", provider_manifest, provider_payloads
    )

    baseline_path = paths["baseline_generation"]
    baseline_bundle = read_generation_bundle(
        baseline_path.parents[2], baseline_path.parent.name
    )
    baseline = json.loads(
        baseline_bundle.payloads["normalized/portfolio-baseline.json"]
    )
    provenance_path = tmp_path / "provider-provenance.json"
    provenance_path.write_bytes(
        canonical_json_bytes(
            {
                "as_of_date": baseline["as_of_date"],
                "accounts": baseline["accounts"],
                "assignments": [
                    {**row, "mapping_status": "MAPPED"}
                    for row in baseline["assignments"]
                ],
            }
        )
    )
    spec_path = tmp_path / "provider-ledger-spec.json"
    spec_path.write_bytes(
        canonical_json_bytes(
            {
                "provenance": {
                    "catalog_path": str(provenance_path),
                    "catalog_sha256": capital._file_binding(provenance_path)[0][
                        "sha256"
                    ],
                    "generation_id": "a" * 64,
                }
            }
        )
    )
    crosswalk_path = tmp_path / "account-crosswalk.json"
    crosswalk_path.write_bytes(canonical_json_bytes({"bindings": []}))
    kwargs = {
        "provider_ledger_generation_root": selected_provider_root,
        "epoch_manifest_generation_root": epoch_root,
        "provider_ledger_spec_path": spec_path,
        "financial_contract_path": paths["contract"],
        "strategy_registry_path": repo
        / "docs"
        / "analytics"
        / "current-strategy-decomposition-plan.json",
        "account_crosswalk_path": crosswalk_path,
        "requested_as_of": epoch_document["calculation_cutoff_date"],
        "evaluated_on": epoch_document["calculation_cutoff_date"],
    }

    return cli, kwargs, epoch, deposit


def test_t5c_custody_input_constructor_precision_is_context_independent(tmp_path):
    """Native constructor preserves exact pre-epoch basis under caller contexts."""
    from quant_engine.core.contracts import canonical_json_bytes

    cli, kwargs, epoch, _deposit = _t5c_constructor_world(tmp_path)

    results = []
    for precision in (28, 80):
        with localcontext() as context:
            context.prec = precision
            generation, _locators = cli.build_input_generation(**kwargs)
        opening = next(
            row
            for row in generation["opening_basis"]
            if row["account_id"] == epoch["account_id"]
        )
        generation_bytes = canonical_json_bytes(generation)
        results.append(
            (
                opening["amount"],
                hashlib.sha256(generation_bytes).hexdigest(),
            )
        )

    expected_amount = "100.00000000000000000000000000002"
    assert results == [
        (expected_amount, results[0][1]),
        (expected_amount, results[0][1]),
    ]


def test_cr2_d1_governed_result_preserves_approved_current_policy_and_numeric_projection(
    tmp_path,
):
    """The approved current policy releases the same governed numeric result."""

    input_path, contract_path = _governed_case(tmp_path)

    result = capital.build_governed_capital_result(input_path, contract_path)

    assert result["policy_readiness_provenance"] == "READY"
    assert result["status"] == "AVAILABLE"
    assert result["current_aggregate_usd"] == "1100"
    assert result["missing_inputs"] == []
    assert (
        capital.verify_governed_capital_result(result, input_path, contract_path)
        == result
    )


@pytest.mark.parametrize("readiness", [None, "UNKNOWN"])
def test_cr2_d1_unknown_or_missing_policy_readiness_fails(readiness):
    """Readiness is an authority enum, never a truthy availability default."""

    case = _case()
    if readiness is None:
        case["policy"].pop("policy_status", None)
    else:
        case["policy"]["policy_status"] = readiness

    with pytest.raises(capital.CapitalValidationError, match="policy readiness"):
        capital.calculate_invested_capital(**case)


def test_cr2_d1_natively_valid_unresolved_shared_flow_policy_preserves_numeric_projection():
    case = _case()
    case["policy"] = _unresolved_shared_flow_policy()

    result = capital.calculate_invested_capital(**case)

    assert result["policy_readiness_provenance"] == "REQUIRED_NOT_READY"
    assert result["status"] == "REQUIRED_NOT_READY"
    assert result["current_aggregate_usd"] == "150"


def test_required_epoch_accounts_are_bound_to_provenance_and_crosswalk():
    epochs = [
        {
            "epoch_id": f"sleeve-{suffix}|A|2026-01-01",
            "sleeve_alias": f"sleeve-{suffix}",
            "account_id": account_id,
            "strategy_id": "A",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
        for suffix, account_id in (("01", "account-a"), ("02", "account-b"))
    ]
    provenance = {
        "accounts": [{"account_id": "account-a"}, {"account_id": "account-b"}],
        "assignments": [
            {
                "account_id": account_id,
                "strategy_id": "A",
                "effective_start": "2026-01-01",
                "effective_end": None,
                "mapping_status": "MAPPED",
                "strategy_mapping_disposition": "STRATEGY_MAPPED",
            }
            for account_id in ("account-a", "account-b")
        ],
    }
    crosswalk = {
        "bindings": [
            {"sleeve_alias": "sleeve-01", "account_id": "account-a"},
            {"sleeve_alias": "sleeve-02", "account_id": "account-b"},
        ]
    }

    capital._verify_epoch_accounts(epochs, provenance, crosswalk)
    epochs[0]["account_id"] = "account-b"
    with pytest.raises(
        capital.CapitalValidationError, match="required epoch account mapping differs"
    ):
        capital._verify_epoch_accounts(epochs, provenance, crosswalk)

    epochs[0]["account_id"] = "account-a"
    provenance["assignments"][0]["mapping_status"] = "UNRESOLVED"
    with pytest.raises(
        capital.CapitalValidationError,
        match="required epoch account cardinality differs",
    ):
        capital._verify_epoch_accounts(epochs, provenance, crosswalk)


def test_governed_capital_cannot_omit_an_in_scope_selected_deposit(tmp_path):
    input_path, contract_path = _governed_case(tmp_path)
    inputs = json.loads(input_path.read_bytes())
    epoch = next(
        row for row in inputs["required_epochs"] if row["effective_end"] is None
    )
    ledger_path = Path(capital.read_local_sidecar(input_path)["provider_ledger"])
    ledger = json.loads(ledger_path.read_bytes())
    fact = {
        "record_id": "additional-owner-deposit",
        "account_id": epoch["account_id"],
        "strategy_id": epoch["strategy_id"],
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "event_date": inputs["complete_through"],
        "amount": {"status": "AVAILABLE", "value": "50", "currency": "USD"},
    }
    ledger["flows"].append(fact)
    ledger_path.write_bytes(capital._canonical(ledger))
    inputs["source_bindings"]["provider_ledger"] = capital._file_binding(ledger_path)[0]
    input_path.write_bytes(capital._canonical(inputs))
    with pytest.raises(
        capital.CapitalValidationError, match="selected capital flow is omitted"
    ):
        capital.build_governed_capital_result(input_path, contract_path)

    evidence = capital._digest(fact)
    inputs["events"].append(
        _event(
            fact["record_id"],
            epoch["epoch_id"],
            fact["event_date"],
            fact["flow_type"],
            "50",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )
    )
    input_path.write_bytes(capital._canonical(inputs))
    assert (
        capital.build_governed_capital_result(input_path, contract_path)[
            "current_aggregate_usd"
        ]
        == "1150"
    )

    # Other accounts and observations after the selected period do not enlarge it.
    ledger["flows"].extend(
        [
            {**fact, "record_id": "other-account", "account_id": "outside-product"},
            {**fact, "record_id": "future", "event_date": "2099-01-01"},
        ]
    )
    ledger_path.write_bytes(capital._canonical(ledger))
    inputs["source_bindings"]["provider_ledger"] = capital._file_binding(ledger_path)[0]
    input_path.write_bytes(capital._canonical(inputs))
    assert (
        capital.build_governed_capital_result(input_path, contract_path)[
            "current_aggregate_usd"
        ]
        == "1150"
    )


@pytest.mark.parametrize("event_id", ["source-deposit", "caller-deposit"])
def test_selected_post_close_correction_cannot_hide_behind_an_event_alias(event_id):
    case = _case()
    case["required_epochs"] = [
        {
            **case["required_epochs"][0],
            "account_id": "account-a",
            "effective_end": "2026-01-02",
        }
    ]
    opening = {
        "record_id": "opening",
        "account_id": "account-a",
        "strategy_id": "A",
        "capital_type": "OPENING_CONTRIBUTED_BASIS",
        "event_date": "2026-01-01",
        "amount": {"status": "AVAILABLE", "value": "100", "currency": "USD"},
    }
    deposit = {
        "record_id": "source-deposit",
        "account_id": "account-a",
        "strategy_id": "A",
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "event_date": "2026-01-02",
        "amount": {"status": "AVAILABLE", "value": "50", "currency": "USD"},
    }
    correction = {
        **deposit,
        "record_id": "correction",
        "flow_type": "CORRECTION_REVERSAL",
        "event_date": "2026-01-03",
        "correction_kind": "SOURCE_CORRECTION",
        "correction_id": "corrected-deposit",
        "correction_status": "RESOLVED",
        "replaces_event_id": "source-deposit",
        "replacement_classification": "EXTERNAL_CONTRIBUTION",
        "amount": {"status": "AVAILABLE", "value": "30", "currency": "USD"},
    }
    coverage = {
        "account_id": "account-a",
        "fact_type": "flows",
        "disposition": "AUTHORITATIVE",
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-03",
    }
    case["flow_coverage"] = [
        {
            "epoch_id": "a-1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "evidence_sha256": capital._digest(coverage),
        }
    ]
    case["opening_basis"], case["events"] = [], []
    for fact, name, classification, target in (
        (opening, "opening", "OPENING_CONTRIBUTED_BASIS", case["opening_basis"]),
        (deposit, event_id, "EXTERNAL_CONTRIBUTION", case["events"]),
    ):
        digest = capital._digest(fact)
        target.append(
            _event(
                name,
                "a-1",
                fact["event_date"],
                classification,
                fact["amount"]["value"],
                source_evidence_sha256=digest,
                fx_evidence_sha256=digest,
            )
        )
    snapshots = {
        "provider_ledger": {
            "capital_anchors": [opening],
            "flows": [deposit, correction],
            "admissions": [coverage],
        }
    }
    with pytest.raises(
        capital.CapitalValidationError, match="selected capital flow is omitted"
    ):
        capital._verify_selected_facts(case, snapshots)

    case["events"][0]["event_id"] = deposit["record_id"]
    digest = capital._digest(correction)
    case["events"].append(
        _event(
            "correction",
            "a-1",
            correction["event_date"],
            "CORRECTION_REVERSAL",
            "30",
            source_evidence_sha256=digest,
            fx_evidence_sha256=digest,
            correction_id="corrected-deposit",
            correction_kind="SOURCE_CORRECTION",
            replaces_event_id="source-deposit",
            replacement_classification="EXTERNAL_CONTRIBUTION",
        )
    )
    case["correction_bindings"] = [
        {
            "correction_id": "corrected-deposit",
            "status": "RESOLVED",
            "evidence_sha256": digest,
        }
    ]
    capital._verify_selected_facts(case, snapshots)
    assert capital.calculate_invested_capital(**case)["current_aggregate_usd"] == "130"


def test_governed_capital_rejects_changed_selected_source_file(tmp_path):
    input_path, contract_path = _governed_case(tmp_path)
    ledger = tmp_path / "provider-ledger.json"
    ledger.write_bytes(ledger.read_bytes() + b"\n")
    with pytest.raises(capital.CapitalValidationError, match="source binding"):
        capital.build_governed_capital_result(input_path, contract_path)


@pytest.mark.parametrize("corrected", [False, True])
def test_same_account_remap_carries_signed_capital_on_january_fifth(
    tmp_path, corrected
):
    input_path, contract_path = _governed_case(tmp_path)
    inputs = json.loads(input_path.read_bytes())
    ledger_path = tmp_path / "provider-ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    # A rename has no provider cash or security movement. The first predecessor
    # has 100 contributed and 120 withdrawn; the other still has 100.
    inputs["events"] = []
    ledger["flows"] = []
    predecessor = next(
        row
        for row in inputs["required_epochs"]
        if row["sleeve_alias"] == "schwab-qsi-01"
        and row["strategy_id"] == "tt-portfolio"
    )
    withdrawal = {
        "record_id": "owner-withdrawal",
        "account_id": predecessor["account_id"],
        "strategy_id": "tt-portfolio",
        "flow_type": "EXTERNAL_WITHDRAWAL",
        "event_date": "2026-01-04",
        "amount": {"status": "AVAILABLE", "value": "-120", "currency": "USD"},
    }
    ledger["flows"].append(withdrawal)
    evidence = capital._digest(withdrawal)
    inputs["events"].append(
        _event(
            "owner-withdrawal",
            predecessor["epoch_id"],
            "2026-01-04",
            "EXTERNAL_WITHDRAWAL",
            "120",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )
    )
    gain = {
        **withdrawal,
        "record_id": "investment-income",
        "flow_type": "DIVIDEND_INTEREST_REINVESTMENT",
        "event_date": "2026-01-03",
        "amount": {"status": "AVAILABLE", "value": "50", "currency": "USD"},
    }
    # The provider ledger may contain investment income, but selected capital
    # events must contain only owner-capital facts.
    ledger["flows"].append(gain)
    additions = []
    if corrected:
        additions.append(
            {
                **withdrawal,
                "record_id": "corrected-withdrawal",
                "flow_type": "CORRECTION_REVERSAL",
                "event_date": "2026-01-06",
                "amount": {"status": "AVAILABLE", "value": "-140", "currency": "USD"},
                "correction_id": "withdrawal-restatement",
                "correction_status": "RESOLVED",
                "correction_kind": "SOURCE_CORRECTION",
                "replaces_event_id": "owner-withdrawal",
                "replacement_classification": "EXTERNAL_WITHDRAWAL",
            }
        )
    for fact in additions:
        ledger["flows"].append(fact)
        digest = capital._digest(fact)
        inputs["events"].append(
            _event(
                fact["record_id"],
                predecessor["epoch_id"],
                fact["event_date"],
                fact["flow_type"],
                fact["amount"]["value"].lstrip("-"),
                source_evidence_sha256=digest,
                fx_evidence_sha256=digest,
                **{
                    key: value
                    for key, value in fact.items()
                    if key.startswith("correction_")
                    or key in {"replaces_event_id", "replacement_classification"}
                },
            )
        )
        if fact.get("correction_id"):
            inputs["correction_bindings"].append(
                {
                    "correction_id": fact["correction_id"],
                    "status": "RESOLVED",
                    "evidence_sha256": digest,
                }
            )
    ledger_path.write_bytes(capital._canonical(ledger))
    inputs["source_bindings"]["provider_ledger"] = capital._file_binding(ledger_path)[0]
    input_path.write_bytes(capital._canonical(inputs))
    result = capital.build_governed_capital_result(input_path, contract_path)
    assert len(result["required_epochs"]) == 13
    assert len(result["required_strategy_ids"]) == 7
    assert result["current_aggregate_usd"] == ("960" if corrected else "980")
    qsi = {
        row["date"]: row["invested_capital_usd"]
        for row in result["daily_by_strategy"]
        if row["strategy_id"] == "qsi"
    }
    assert (qsi["2026-01-04"], qsi["2026-01-05"]) == ("0", "60" if corrected else "80")
    remaps = [
        row
        for row in result["events"]
        if row["classification"] == "PREDECESSOR_TRANSITION"
    ]
    assert len(remaps) == 4
    assert {row["date"] for row in remaps} == {"2026-01-05"}
    assert sorted(row["invested_capital_effect_usd"] for row in remaps) == [
        "-100",
        "-40" if corrected else "-20",
        "100",
        "40" if corrected else "20",
    ]
    assert (
        capital.verify_governed_capital_result(result, input_path, contract_path)
        == result
    )


def test_closed_epoch_retains_signed_net_contributions():
    case = _case()
    case["required_epochs"] = case["required_epochs"][:1]
    case["required_epochs"][0]["effective_end"] = "2026-01-02"
    case["opening_basis"] = case["opening_basis"][:1]
    case["flow_coverage"] = case["flow_coverage"][:1]
    case["flow_coverage"][0]["end_date"] = "2026-01-02"
    case["events"] = [
        _event("withdrawal", "a-1", "2026-01-02", "EXTERNAL_WITHDRAWAL", "120")
    ]
    result = capital.calculate_invested_capital(**case)
    assert [row["invested_capital_usd"] for row in result["daily_aggregate"]] == [
        "100",
        "-20",
        "-20",
    ]
    assert result["daily_by_epoch"][-1]["date"] == "2026-01-02"


def test_unrelated_post_close_owner_flow_is_rejected():
    case = _case()
    case["required_epochs"][0]["effective_end"] = "2026-01-01"
    case["events"] = [
        _event("late-contribution", "a-1", "2026-01-03", "EXTERNAL_CONTRIBUTION", "10")
    ]
    with pytest.raises(capital.CapitalValidationError, match="event after epoch end"):
        capital.calculate_invested_capital(**case)


@pytest.mark.parametrize("role", ["classified_flows", "fx", "correction_registry"])
def test_governed_capital_rejects_self_authored_auxiliary_authority(tmp_path, role):
    input_path, contract_path = _governed_case(tmp_path)
    inputs = json.loads(input_path.read_bytes())
    invented = tmp_path / f"{role}.json"
    invented.write_text(
        '{"status":"ACCEPTED","events":[],"observations":[],"corrections":[]}'
    )
    inputs["source_bindings"][role] = capital._file_binding(invented)[0]
    input_path.write_bytes(capital._canonical(inputs))
    with pytest.raises(capital.CapitalValidationError, match="source roles"):
        capital.build_governed_capital_result(input_path, contract_path)


def test_selected_source_fee_cannot_be_reclassified_by_an_auxiliary_file():
    fact = {
        "record_id": "fee",
        "account_id": "account-a",
        "strategy_id": "A",
        "flow_type": "FEE_TAX_REBATE_EXPENSE",
        "event_date": "2026-01-02",
        "amount": {"status": "AVAILABLE", "value": "7", "currency": "USD"},
    }
    classification = {
        "source_record_id": "fee",
        "capital_classification": "EXTERNAL_CONTRIBUTION",
    }
    evidence = capital._digest(fact)
    case = _case()
    case["required_epochs"][0]["account_id"] = "account-a"
    case["opening_basis"] = []
    case["events"] = [
        _event(
            "fee",
            "a-1",
            "2026-01-02",
            "EXTERNAL_CONTRIBUTION",
            "7",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
            classification_evidence_sha256=capital._digest(classification),
        )
    ]
    with pytest.raises(capital.CapitalValidationError, match="owner-capital fact"):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {"flows": [fact]},
                "classified_flows": {"events": [classification]},
            },
        )


@pytest.mark.parametrize(
    "flow_type,amount,classification",
    [
        ("EXTERNAL_PROVIDER_TRANSFER", "0.49", "EXTERNAL_CONTRIBUTION"),
        ("EXTERNAL_PROVIDER_TRANSFER", "-120", "EXTERNAL_WITHDRAWAL"),
        ("DEPOSIT_OR_WITHDRAWAL", "0.49", "EXTERNAL_CONTRIBUTION"),
        ("DEPOSIT_OR_WITHDRAWAL", "-120", "EXTERNAL_WITHDRAWAL"),
        ("TRANSFER:INTERNAL:CASH", "7201.64", "EXTERNAL_CONTRIBUTION"),
        ("TRANSFER:INTERNAL:CASH", "-7201.64", "EXTERNAL_WITHDRAWAL"),
    ],
)
def test_selected_external_provider_flow_uses_policy_and_native_sign(
    flow_type, amount, classification
):
    case = _case()
    case["required_epochs"][0]["account_id"] = "account-a"
    case["opening_basis"] = []
    case["flow_coverage"] = []
    source = {
        "record_id": "native-owner-flow",
        "account_id": "account-a",
        "strategy_id": "A",
        "flow_type": flow_type,
        "event_date": "2026-01-02",
        "amount": {"value": amount, "currency": "USD", "status": "AVAILABLE"},
    }
    evidence = capital._digest(source)
    case["events"] = [
        _event(
            "native-owner-flow",
            "a-1",
            "2026-01-02",
            classification,
            amount.lstrip("-"),
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )
    ]
    snapshots = {"provider_ledger": {"flows": [source]}}
    capital._verify_selected_facts(case, snapshots)
    case["events"][0]["classification"] = (
        "EXTERNAL_WITHDRAWAL"
        if classification == "EXTERNAL_CONTRIBUTION"
        else "EXTERNAL_CONTRIBUTION"
    )
    with pytest.raises(capital.CapitalValidationError):
        capital._verify_selected_facts(case, snapshots)

    if flow_type == "TRANSFER:INTERNAL:CASH":
        case["events"][0]["classification"] = classification
        snapshots["provider_ledger"]["flows"].append(
            {
                **source,
                "record_id": "native-counterparty-flow",
                "account_id": "account-b",
                "amount": {
                    "value": amount[1:] if amount.startswith("-") else f"-{amount}",
                    "currency": "USD",
                    "status": "AVAILABLE",
                },
            }
        )
        with pytest.raises(capital.CapitalValidationError, match="classification"):
            capital._verify_selected_facts(case, snapshots)


def test_daily_capital_excludes_results_and_offsets_internal_transfers():
    case = _case()
    case["events"] = [
        _event("deposit", "a-1", "2026-01-02", "EXTERNAL_CONTRIBUTION", "20"),
        _event("income", "a-1", "2026-01-02", "DIVIDEND_INTEREST_REINVESTMENT", "7"),
        _event("fee", "a-1", "2026-01-02", "FEE_TAX_REBATE_EXPENSE", "3"),
        _event(
            "send",
            "a-1",
            "2026-01-02",
            "INTERNAL_TRANSFER",
            "-30",
            transfer_id="transfer-1",
        ),
        _event(
            "receive",
            "b-1",
            "2026-01-02",
            "INTERNAL_TRANSFER",
            "30",
            transfer_id="transfer-1",
        ),
        _event("withdraw", "b-1", "2026-01-03", "EXTERNAL_WITHDRAWAL", "100"),
    ]
    result = capital.calculate_invested_capital(**case)
    assert result["status"] == "AVAILABLE"
    # Day 1: 100+50. Day 2: only 20 of new owner cash. Day 3: 100 returned.
    assert [r["invested_capital_usd"] for r in result["daily_aggregate"]] == [
        "150",
        "170",
        "70",
    ]
    assert result["current_by_strategy"] == [
        {"strategy_id": "A", "invested_capital_usd": "90"},
        {"strategy_id": "B", "invested_capital_usd": "-20"},
    ]
    assert result["current_aggregate_usd"] == "70"
    assert result["complete_through"] == "2026-01-03"
    assert result["evaluation_date"] == "2026-01-05"
    assert {e["timing"] for e in result["events"]} == {"INFERRED_TIMING"}


def test_native_and_foreign_in_kind_basis_use_event_time_fx():
    case = _case()
    case["events"] = [
        _event(
            "shares",
            "a-1",
            "2026-01-02",
            "IN_KIND_FUNDING",
            "25",
            currency="EUR",
            fx_rate="1.2",
            valuation_evidence_sha256="f" * 64,
        )
    ]
    assert capital.calculate_invested_capital(**case)["current_aggregate_usd"] == "180"
    case["events"][0]["fx_evidence_sha256"] = None
    with pytest.raises(capital.CapitalValidationError, match="FX evidence"):
        capital.calculate_invested_capital(**case)


def test_source_correction_restates_original_day_economic_reversal_uses_own_day():
    case = _case()
    case["correction_bindings"] = [
        {"correction_id": "c1", "status": "RESOLVED", "evidence_sha256": "f" * 64}
    ]
    case["events"] = [
        _event("deposit", "a-1", "2026-01-02", "EXTERNAL_CONTRIBUTION", "20"),
        _event(
            "correct",
            "a-1",
            "2026-01-03",
            "CORRECTION_REVERSAL",
            "12",
            correction_kind="SOURCE_CORRECTION",
            replaces_event_id="deposit",
            correction_id="c1",
            replacement_classification="EXTERNAL_CONTRIBUTION",
        ),
        _event(
            "reverse",
            "a-1",
            "2026-01-03",
            "CORRECTION_REVERSAL",
            "12",
            correction_kind="ECONOMIC_REVERSAL",
            reverses_event_id="correct",
            correction_id="c1",
        ),
    ]
    result = capital.calculate_invested_capital(**case)
    assert [r["invested_capital_usd"] for r in result["daily_aggregate"]] == [
        "150",
        "162",
        "150",
    ]


def test_margin_principal_does_not_become_owner_capital():
    case = _case()
    case["events"] = [
        _event(
            "margin",
            "a-1",
            "2026-01-02",
            "MARGIN_FINANCING",
            "50",
            leverage_facts={
                name: {"value": "1", "evidence_sha256": "f" * 64}
                for name in case["policy"]["leverage_accounting"]["required_facts"]
            },
        )
    ]
    assert capital.calculate_invested_capital(**case)["current_aggregate_usd"] == "150"


def test_predecessor_reallocates_contributed_basis_without_new_aggregate_funding():
    case = _case()
    case["required_epochs"][0]["effective_end"] = "2026-01-01"
    case["required_epochs"][1]["effective_start"] = "2026-01-02"
    case["opening_basis"] = case["opening_basis"][:1]
    case["flow_coverage"][0]["end_date"] = "2026-01-01"
    case["flow_coverage"][1]["start_date"] = "2026-01-02"
    for epoch in case["required_epochs"]:
        epoch.update(account_id="same-account", sleeve_alias="same-sleeve")
    case["identity_remaps"] = [
        {
            "predecessor_epoch_id": "a-1",
            "successor_epoch_id": "b-1",
            "continuity_evidence_sha256": "f" * 64,
        }
    ]
    result = capital.calculate_invested_capital(**case)
    assert [r["invested_capital_usd"] for r in result["daily_aggregate"]] == [
        "100",
        "100",
        "100",
    ]
    assert result["current_by_strategy"] == [
        {"strategy_id": "A", "invested_capital_usd": "0"},
        {"strategy_id": "B", "invested_capital_usd": "100"},
    ]


def test_policy_restatement_requires_matching_owner_decision_receipts():
    case = _case()
    case["policy"]["negative_net_contributions"]["selected_treatment"] = "FLOOR_AT_ZERO"
    with pytest.raises(capital.CapitalValidationError, match="policy decision"):
        capital.calculate_invested_capital(**case)


def test_capital_is_independent_of_callers_decimal_context():
    case = _case()
    case["opening_basis"][0]["amount"] = "100000000.123456"
    expected = capital.calculate_invested_capital(**case)
    assert expected["current_aggregate_usd"] == "100000050.123456"
    with localcontext() as context:
        context.prec = 6
        assert capital.calculate_invested_capital(**case) == expected


def test_capital_preserves_units_beyond_default_decimal_precision():
    case = _case()
    case["opening_basis"][0]["amount"] = "99999999999999999999999999999999999"
    case["opening_basis"][1]["amount"] = "2"
    assert (
        capital.calculate_invested_capital(**case)["current_aggregate_usd"]
        == "100000000000000000000000000000000001"
    )


@pytest.mark.parametrize(
    "replacement,evidence",
    [
        ("FEE_TAX_REBATE_EXPENSE", {}),
        ("DIVIDEND_INTEREST_REINVESTMENT", {}),
        ("MARGIN_FINANCING", {"leverage_facts": "required"}),
        ("IN_KIND_FUNDING", {"valuation_evidence_sha256": "f" * 64}),
    ],
)
def test_source_correction_revalidates_full_replacement_class(replacement, evidence):
    case = _case()
    if evidence.get("leverage_facts") == "required":
        evidence = {
            "leverage_facts": {
                name: {"value": "1", "evidence_sha256": "f" * 64}
                for name in case["policy"]["leverage_accounting"]["required_facts"]
            }
        }
    case["correction_bindings"] = [
        {"correction_id": "c1", "status": "RESOLVED", "evidence_sha256": "f" * 64}
    ]
    case["events"] = [
        _event("old", "a-1", "2026-01-02", "EXTERNAL_CONTRIBUTION", "20"),
        _event(
            "correct",
            "a-1",
            "2026-01-03",
            "CORRECTION_REVERSAL",
            "12",
            correction_kind="SOURCE_CORRECTION",
            replaces_event_id="old",
            correction_id="c1",
            replacement_classification=replacement,
            **evidence,
        ),
    ]
    result = capital.calculate_invested_capital(**case)
    expected = "162" if replacement == "IN_KIND_FUNDING" else "150"
    assert [row["invested_capital_usd"] for row in result["daily_aggregate"]] == [
        "150",
        expected,
        expected,
    ]
    assert result["events"][-1]["date"] == "2026-01-02"
    if replacement == "IN_KIND_FUNDING":
        del case["events"][-1]["valuation_evidence_sha256"]
        with pytest.raises(capital.CapitalValidationError, match="valuation evidence"):
            capital.calculate_invested_capital(**case)


def _selected_migration_case(source_kind):
    # Synthetic historical capital: 80 opening + 20 contribution + 50 gain.
    # A 150 market-value transfer proves movement, not the authority to carry
    # the 100 contributed basis across the beneficial-capital perimeter.
    case = _case()
    successor = case["required_epochs"][1]
    successor.update(account_id="successor", effective_start="2026-01-03")
    case["required_epochs"] = [successor]
    predecessor = {
        "epoch_id": "historical",
        "account_id": "predecessor",
        "strategy_id": None,
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-02",
    }
    case["supporting_epochs"] = [predecessor]
    epochs = {row["epoch_id"]: row for row in [predecessor, successor]}
    case["opening_basis"] = [
        _event("opening", "historical", "2026-01-01", "OPENING_CONTRIBUTED_BASIS", "80")
    ]
    case["events"] = [
        _event("deposit", "historical", "2026-01-02", "EXTERNAL_CONTRIBUTION", "20"),
        *[
            _event(
                event_id,
                epoch_id,
                "2026-01-03",
                "PREDECESSOR_TRANSITION",
                amount,
                transfer_id="a" * 64,
            )
            for event_id, epoch_id, amount in [
                ("out", "historical", "-150"),
                ("in", "b-1", "150"),
            ]
        ],
    ]
    ledger = {
        "capital_anchors": [],
        "flows": [
            {
                "record_id": "gain",
                "account_id": "predecessor",
                "strategy_id": None,
                "event_date": "2026-01-02",
                "flow_type": "DIVIDEND_INTEREST_REINVESTMENT",
                "amount": {
                    "status": "AVAILABLE",
                    "value": "50",
                    "currency": "USD",
                },
            }
        ],
        "admissions": [],
    }
    for event in [*case["opening_basis"], *case["events"]]:
        source = {
            "record_id": event["event_id"],
            "account_id": epochs[event["epoch_id"]]["account_id"],
            "strategy_id": epochs[event["epoch_id"]]["strategy_id"],
            "event_date": event["date"],
            "amount": {
                "status": "AVAILABLE",
                "value": event["amount"],
                "currency": "USD",
            },
        }
        if event["classification"] == "OPENING_CONTRIBUTED_BASIS":
            source["capital_type"] = event["classification"]
            ledger["capital_anchors"].append(source)
        else:
            source["flow_type"] = event["classification"]
            if "transfer_id" in event:
                source["flow_type"] = source_kind
                source["provider_activity_id_sha256"] = event["transfer_id"]
            ledger["flows"].append(source)
        event["source_evidence_sha256"] = event["fx_evidence_sha256"] = capital._digest(
            source
        )
    continuity = capital._digest(
        {
            "activity_id": "a" * 64,
            "source_evidence_sha256": sorted(
                capital._digest(row)
                for row in ledger["flows"]
                if row.get("provider_activity_id_sha256")
            ),
        }
    )
    for event in case["events"][-2:]:
        event["continuity_evidence_sha256"] = continuity
    case["flow_coverage"] = []
    for epoch in epochs.values():
        admission = {
            "account_id": epoch["account_id"],
            "fact_type": "flows",
            "disposition": "AUTHORITATIVE",
            "effective_start": epoch["effective_start"],
            "effective_end": epoch["effective_end"] or case["complete_through"],
        }
        ledger["admissions"].append(admission)
        case["flow_coverage"].append(
            {
                "epoch_id": epoch["epoch_id"],
                "start_date": admission["effective_start"],
                "end_date": admission["effective_end"],
                "evidence_sha256": capital._digest(admission),
            }
        )
    return case, {"provider_ledger": ledger}


@pytest.mark.parametrize(
    "source_kind",
    ["ACCOUNT_TRANSFER", "TRANSFER:INTERNAL:SYNTHETIC"],
)
@pytest.mark.parametrize("boundary", ["selected_facts", "calculation"])
def test_selected_transfer_pair_does_not_prove_contributed_basis_migration(
    source_kind, boundary
):
    case, snapshots = _selected_migration_case(source_kind)
    with pytest.raises(
        capital.CapitalValidationError, match="accepted identity continuity"
    ):
        if boundary == "selected_facts":
            capital._verify_selected_facts(case, snapshots)
        else:
            capital.calculate_invested_capital(**case)


def test_supporting_predecessor_history_does_not_add_an_output_epoch():
    case = _case()
    case["required_epochs"] = [case["required_epochs"][1]]
    case["required_epochs"][0]["effective_start"] = "2026-01-02"
    case["supporting_epochs"] = [
        {
            "epoch_id": "historical-before",
            "account_id": "same-account",
            "sleeve_alias": "same-sleeve",
            "strategy_id": None,
            "effective_start": "2026-01-01",
            "effective_end": "2026-01-01",
        }
    ]
    case["opening_basis"] = [
        _event(
            "historical-funding",
            "historical-before",
            "2026-01-01",
            "OPENING_CONTRIBUTED_BASIS",
            "100",
        )
    ]
    case["flow_coverage"] = [
        {
            "epoch_id": "historical-before",
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "evidence_sha256": "c" * 64,
        },
        {
            "epoch_id": "b-1",
            "start_date": "2026-01-02",
            "end_date": "2026-01-03",
            "evidence_sha256": "c" * 64,
        },
    ]
    case["required_epochs"][0].update(
        account_id="same-account", sleeve_alias="same-sleeve"
    )
    case["identity_remaps"] = [
        {
            "predecessor_epoch_id": "historical-before",
            "successor_epoch_id": "b-1",
            "continuity_evidence_sha256": "f" * 64,
        }
    ]
    result = capital.calculate_invested_capital(**case)
    assert result["required_epochs"] == case["required_epochs"]
    assert result["required_strategy_ids"] == ["B"]
    assert result["period_start"] == "2026-01-02"
    assert [row["invested_capital_usd"] for row in result["daily_aggregate"]] == [
        "100",
        "100",
    ]
    assert {row["epoch_id"] for row in result["daily_by_epoch"]} == {"b-1"}
    assert result["current_aggregate_usd"] == "100"


def test_selected_source_amount_cannot_be_rescaled_while_keeping_its_hash():
    raw = {
        "record_id": "funding",
        "account_id": "account-a",
        "strategy_id": "A",
        "capital_type": "OPENING_CONTRIBUTED_BASIS",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    case = _case()
    case["required_epochs"][0]["account_id"] = "account-a"
    case["opening_basis"] = [
        _event(
            "opening",
            "a-1",
            "2026-01-01",
            "OPENING_CONTRIBUTED_BASIS",
            "10000",
            source_evidence_sha256=digest,
            fx_evidence_sha256=digest,
        )
    ]
    with pytest.raises(capital.CapitalValidationError, match="source amount"):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [raw],
                    "flows": [],
                    "admissions": [],
                }
            },
        )


def test_nav_anchor_cannot_be_relabelled_as_contributed_basis():
    raw = {
        "record_id": "nav",
        "account_id": "account-a",
        "strategy_id": "A",
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "mapping_basis": "FIRST_NAV",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    case = _case()
    case["required_epochs"][0]["account_id"] = "account-a"
    case["opening_basis"] = [
        _event(
            "opening",
            "a-1",
            "2026-01-01",
            "OPENING_CONTRIBUTED_BASIS",
            "100",
            source_evidence_sha256=digest,
            fx_evidence_sha256=digest,
        )
    ]
    with pytest.raises(capital.CapitalValidationError, match="owner-capital fact"):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [raw],
                    "flows": [],
                    "admissions": [],
                }
            },
        )


def test_date_only_source_cannot_be_reported_as_exact_timestamp(tmp_path):
    input_path, contract_path = _governed_case(tmp_path)
    inputs = json.loads(input_path.read_bytes())
    opening = inputs["opening_basis"][0]
    opening["timestamp"] = opening["date"] + "T09:30:00+00:00"
    input_path.write_bytes(capital._canonical(inputs))
    with pytest.raises(capital.CapitalValidationError, match="source timestamp"):
        capital.build_governed_capital_result(input_path, contract_path)


def test_t5c_custody_requires_the_paired_selected_policy_decision():
    case = _case()
    case["required_epochs"] = [
        {
            "epoch_id": "custody-1",
            "account_id": "custody-account",
            "strategy_id": "A",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
    ]
    case["opening_basis"] = []
    coverage = {
        "account_id": "custody-account",
        "fact_type": "flows",
        "disposition": "AUTHORITATIVE",
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-03",
    }
    case["flow_coverage"] = [
        {
            "epoch_id": "custody-1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "evidence_sha256": capital._digest(coverage),
        }
    ]
    deposit = {
        "record_id": "custody-deposit",
        "account_id": "custody-account",
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "provider_account_scope": "ACCOUNT_ONLY",
        "capital_disposition": "EXTERNAL_CAPITAL",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    anchor = {
        "record_id": "custody-allocation",
        "account_id": "custody-account",
        "strategy_id": "A",
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "event_date": "2026-01-01",
        "amount": {"value": "96", "currency": "USD", "status": "AVAILABLE"},
    }
    evidence = capital._digest(deposit)
    case["events"] = [
        _event(
            "custody-deposit",
            "custody-1",
            "2026-01-01",
            "EXTERNAL_CONTRIBUTION",
            "100",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )
    ]
    policy = deepcopy(case["policy"])
    policy.pop("provider_custody_perimeter", None)
    policy["owner_decision_receipts"].pop("PROVIDER_CUSTODY_PERIMETER", None)
    with pytest.raises(
        capital.CapitalValidationError, match="provider-custody perimeter undecided"
    ):
        capital._verify_selected_facts(
            case,
            {"provider_ledger": {"capital_anchors": [anchor], "flows": [deposit]}},
            policy,
        )


def test_t5c_custody_deposit_is_capital_and_same_day_allocation_is_not():
    case = _case()
    case["required_epochs"] = [
        {
            "epoch_id": "custody-1",
            "account_id": "custody-account",
            "strategy_id": "A",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
    ]
    case["opening_basis"] = []
    coverage = {
        "account_id": "custody-account",
        "fact_type": "flows",
        "disposition": "AUTHORITATIVE",
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-03",
    }
    case["flow_coverage"] = [
        {
            "epoch_id": "custody-1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "evidence_sha256": capital._digest(coverage),
        }
    ]
    deposit = {
        "record_id": "custody-deposit",
        "account_id": "custody-account",
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "provider_account_scope": "ACCOUNT_ONLY",
        "capital_disposition": "EXTERNAL_CAPITAL",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    anchor = {
        "record_id": "custody-allocation",
        "account_id": "custody-account",
        "strategy_id": "A",
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "event_date": "2026-01-01",
        "amount": {"value": "96", "currency": "USD", "status": "AVAILABLE"},
    }
    evidence = capital._digest(deposit)
    case["events"] = [
        _event(
            "custody-deposit",
            "custody-1",
            "2026-01-01",
            "EXTERNAL_CONTRIBUTION",
            "100",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )
    ]
    snapshots = {
        "provider_ledger": {
            "capital_anchors": [anchor],
            "flows": [deposit],
            "admissions": [coverage],
        }
    }
    capital._verify_selected_facts(case, snapshots, case["policy"])
    calculation_case = deepcopy(case)
    calculation_case["opening_basis"] = [
        _event(
            "custody-open", "custody-1", "2026-01-01", "OPENING_CONTRIBUTED_BASIS", "0"
        )
    ]
    assert (
        capital.calculate_invested_capital(**calculation_case)["current_aggregate_usd"]
        == "100"
    )

    before_deposit = deepcopy(anchor)
    before_deposit["event_date"] = "2025-12-31"
    with pytest.raises(
        capital.CapitalValidationError,
        match="custody allocation lacks qualifying deposit",
    ):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [before_deposit],
                    "flows": [deposit],
                    "admissions": [coverage],
                }
            },
            case["policy"],
        )

    negative_cash = deepcopy(anchor)
    negative_cash["amount"]["value"] = "101"
    with pytest.raises(
        capital.CapitalValidationError, match="provider-custody cash is negative"
    ):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [negative_cash],
                    "flows": [deposit],
                    "admissions": [coverage],
                }
            },
            case["policy"],
        )

    tiny_negative_deposit = deepcopy(deposit)
    tiny_negative_deposit["amount"]["value"] = "99.99999999999999999999999999999"
    tiny_negative_anchor = deepcopy(anchor)
    tiny_negative_anchor["amount"]["value"] = "100"
    tiny_positive_deposit = deepcopy(deposit)
    tiny_positive_deposit["amount"]["value"] = "100.00000000000000000000000000002"
    tiny_positive_anchor = deepcopy(anchor)
    tiny_positive_anchor["amount"]["value"] = "100.00000000000000000000000000001"
    for precision in (28, 80):
        with localcontext() as context:
            context.prec = precision
            with pytest.raises(
                capital.CapitalValidationError,
                match="provider-custody cash is negative",
            ):
                capital._verify_provider_custody_perimeter(
                    {
                        "capital_anchors": [tiny_negative_anchor],
                        "flows": [tiny_negative_deposit],
                    },
                    case["required_epochs"],
                    case["policy"],
                )
            capital._verify_provider_custody_perimeter(
                {
                    "capital_anchors": [tiny_positive_anchor],
                    "flows": [tiny_positive_deposit],
                },
                case["required_epochs"],
                case["policy"],
            )


def test_t5c_custody_perimeter_preserves_generic_opening_owner_capital():
    case = _case()
    epoch = case["required_epochs"][0]
    epoch.update(account_id="generic-account", effective_start="2026-01-01")
    case["required_epochs"] = [epoch]
    case["opening_basis"] = []
    case["events"] = []
    generic_coverage = {
        "account_id": "generic-account",
        "fact_type": "flows",
        "disposition": "AUTHORITATIVE",
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-03",
    }
    case["flow_coverage"] = [
        {
            "epoch_id": epoch["epoch_id"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "evidence_sha256": capital._digest(generic_coverage),
        }
    ]
    generic_opening = {
        "record_id": "generic-opening-owner-capital",
        "account_id": "generic-account",
        "strategy_id": None,
        "capital_type": "OPENING_CONTRIBUTED_BASIS",
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "provider_account_scope": "ACCOUNT_ONLY",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    generic_anchor = {
        "record_id": "generic-opening-anchor",
        "account_id": "generic-account",
        "strategy_id": "A",
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
        "authority_profile": "SYNTHETIC",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    generic_ledger = {
        "capital_anchors": [generic_anchor],
        "flows": [generic_opening],
        "admissions": [generic_coverage],
    }

    # The same native guard is called by the input producer and by the
    # independent selected-facts verifier. Generic opening owner-capital rows
    # are not provider-custody deposits and keep their established treatment.
    assert (
        capital._verify_provider_custody_perimeter(
            generic_ledger, case["required_epochs"], case["policy"]
        )
        == set()
    )
    source_digest = capital._digest(generic_opening)
    case["opening_basis"] = [
        _event(
            "generic-opening-owner-capital",
            epoch["epoch_id"],
            "2026-01-01",
            "OPENING_CONTRIBUTED_BASIS",
            "100",
            source_evidence_sha256=source_digest,
            fx_evidence_sha256=source_digest,
        )
    ]
    capital._verify_selected_facts(
        case, {"provider_ledger": generic_ledger}, case["policy"]
    )

    statement_anchor = {
        **generic_anchor,
        "record_id": "statement-opening-anchor",
        "mapping_basis": "NET_STATEMENT_ACCOUNT_CAPITAL_ON_PROVIDER_EVENT_DATE",
        "provider_account_scope": "STATEMENT_ACCOUNT",
    }
    statement_flow = {
        **generic_opening,
        "record_id": "statement-dividend",
        "capital_type": None,
        "flow_type": "DIVIDEND_INTEREST_REINVESTMENT",
        "provider_account_scope": "STATEMENT_ACCOUNT",
    }
    assert (
        capital._verify_provider_custody_perimeter(
            {"capital_anchors": [statement_anchor], "flows": [statement_flow]},
            case["required_epochs"],
            case["policy"],
        )
        == set()
    )

    custody_anchor = {
        **generic_anchor,
        "record_id": "custody-opening-anchor",
        "account_id": "custody-account",
    }
    custody_epoch = {**epoch, "account_id": "custody-account"}
    custody_deposit = {
        **generic_opening,
        "record_id": "custody-deposit",
        "account_id": "custody-account",
        "capital_type": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "capital_disposition": "EXTERNAL_CAPITAL",
    }
    custody_ledger = {
        "capital_anchors": [custody_anchor],
        "flows": [custody_deposit],
    }
    assert capital._verify_provider_custody_perimeter(
        custody_ledger, [custody_epoch], case["policy"]
    ) == {"custody-account"}

    with pytest.raises(
        capital.CapitalValidationError,
        match="custody allocation lacks qualifying deposit",
    ):
        capital._verify_provider_custody_perimeter(
            {"capital_anchors": [custody_anchor], "flows": []},
            [custody_epoch],
            case["policy"],
        )

    unsupported_transfer = {
        **custody_deposit,
        "record_id": "custody-transfer",
        "flow_type": "ACCOUNT_TRANSFER",
        "capital_disposition": None,
    }
    with pytest.raises(
        capital.CapitalValidationError,
        match="unsupported provider-custody cash fact",
    ):
        capital._verify_provider_custody_perimeter(
            {
                "capital_anchors": [custody_anchor],
                "flows": [custody_deposit, unsupported_transfer],
            },
            [custody_epoch],
            case["policy"],
        )


def _account_ledger_opening(epoch, sources):
    component_sha256s = sorted(capital._digest(row) for row in sources)
    descriptor = {
        "basis_scope": "AVAILABLE_ACCOUNT_LEDGER_NET",
        "account_id": epoch["account_id"],
        "ledger_period_start": min(row["event_date"] for row in sources)
        if sources
        else epoch["effective_start"],
        "ledger_period_end_exclusive": epoch["effective_start"],
        "source_component_sha256s": component_sha256s,
    }
    evidence = capital._digest(descriptor)
    amount = sum(
        (capital._decimal(row["amount"]["value"], "source amount") for row in sources),
        Decimal(0),
    )
    return _event(
        f"account-ledger-opening:{epoch['account_id']}:{epoch['effective_start']}",
        epoch["epoch_id"],
        epoch["effective_start"],
        "OPENING_CONTRIBUTED_BASIS",
        capital._number(amount),
        source_evidence_sha256=evidence,
        fx_evidence_sha256=evidence,
        **descriptor,
    )


def test_selected_account_ledger_net_proves_negative_opening_basis():
    case = _case()
    epoch = case["required_epochs"][0]
    epoch.update(account_id="account-a", effective_start="2026-01-03")
    case["required_epochs"] = [epoch]
    deposit = {
        "record_id": "owner-deposit",
        "account_id": "account-a",
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "mapping_status": "UNMAPPED",
        "provider_account_scope": "ACCOUNT_ONLY",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    withdrawal = {
        "record_id": "owner-withdrawal",
        "account_id": "account-a",
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "mapping_status": "UNMAPPED",
        "provider_account_scope": "ACCOUNT_ONLY",
        "event_date": "2026-01-02",
        "amount": {"value": "-120", "currency": "USD", "status": "AVAILABLE"},
    }
    case["opening_basis"] = [_account_ledger_opening(epoch, [deposit, withdrawal])]
    case["events"] = []
    case["flow_coverage"] = []
    snapshots = {
        "provider_ledger": {
            "capital_anchors": [],
            "flows": [deposit, withdrawal],
            "admissions": [],
        }
    }

    capital._verify_selected_facts(case, snapshots)
    case["flow_coverage"] = [
        {
            "epoch_id": epoch["epoch_id"],
            "start_date": epoch["effective_start"],
            "end_date": case["complete_through"],
            "evidence_sha256": "c" * 64,
        }
    ]
    case["source_bindings"] = {"ledger": {"sha256": "a" * 64, "size_bytes": 1}}
    result = capital.calculate_invested_capital(**case)
    assert result["current_aggregate_usd"] == "-20"


def test_account_ledger_opening_rejects_income_or_trading_component():
    case = _case()
    epoch = case["required_epochs"][0]
    epoch.update(account_id="account-a", effective_start="2026-01-03")
    case["required_epochs"] = [epoch]
    dividend = {
        "record_id": "dividend",
        "account_id": "account-a",
        "strategy_id": None,
        "flow_type": "DIVIDEND_INTEREST_REINVESTMENT",
        "event_date": "2026-01-01",
        "amount": {"value": "10", "currency": "USD", "status": "AVAILABLE"},
    }
    case["opening_basis"] = [_account_ledger_opening(epoch, [dividend])]
    case["events"] = []
    case["flow_coverage"] = []

    with pytest.raises(
        capital.CapitalValidationError,
        match="account ledger component is not an owner-capital fact",
    ):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [],
                    "flows": [dividend],
                    "admissions": [],
                }
            },
        )


def test_strategy_capital_allocation_is_not_an_account_deposit():
    case = _case()
    epoch = case["required_epochs"][0]
    epoch.update(account_id="account-a")
    case["required_epochs"] = [epoch]
    allocation = {
        "record_id": "capital-allocation",
        "account_id": "account-a",
        "strategy_id": "A",
        "capital_type": "STRATEGY_CAPITAL_ALLOCATION",
        "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
        "event_date": "2026-01-02",
        "nav_recognition_date": "2026-01-03",
        "amount": {"value": "76", "currency": "USD", "status": "AVAILABLE"},
    }
    evidence = capital._digest(allocation)
    case["opening_basis"] = case["opening_basis"][:1]
    case["opening_basis"][0].update(epoch_id=epoch["epoch_id"])
    opening_source = {
        "record_id": "open-a",
        "account_id": "account-a",
        "strategy_id": "A",
        "capital_type": "OPENING_CONTRIBUTED_BASIS",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    opening_evidence = capital._digest(opening_source)
    case["opening_basis"][0].update(
        source_evidence_sha256=opening_evidence,
        fx_evidence_sha256=opening_evidence,
    )
    case["events"] = [
        _event(
            allocation["record_id"],
            epoch["epoch_id"],
            allocation["event_date"],
            "EXTERNAL_CONTRIBUTION",
            "76",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
            source_date_basis="RAW_ACCOUNT_LEDGER_EVENT_DATE",
        )
    ]
    case["flow_coverage"] = []

    with pytest.raises(
        capital.CapitalValidationError,
        match="capital source is not an owner-capital fact",
    ):
        capital._verify_selected_facts(
            case,
            {
                "provider_ledger": {
                    "capital_anchors": [opening_source, allocation],
                    "flows": [],
                    "admissions": [],
                }
            },
        )


def test_raw_account_ledger_transfer_from_outside_required_accounts_is_external():
    case = _case()
    case["required_epochs"] = [case["required_epochs"][0]]
    case["required_epochs"][0]["account_id"] = "required-account"
    case["opening_basis"] = []
    case["flow_coverage"] = []
    incoming = {
        "record_id": "incoming",
        "account_id": "required-account",
        "strategy_id": "A",
        "flow_type": "ACCOUNT_TRANSFER",
        "event_date": "2026-01-02",
        "amount": {"value": "25", "currency": "USD", "status": "AVAILABLE"},
    }
    outside = {
        **incoming,
        "record_id": "outside",
        "account_id": "reserve-outside-required-perimeter",
        "strategy_id": None,
        "amount": {"value": "-25", "currency": "USD", "status": "AVAILABLE"},
    }
    evidence = capital._digest(incoming)
    case["events"] = [
        _event(
            "incoming",
            "a-1",
            "2026-01-02",
            "EXTERNAL_CONTRIBUTION",
            "25",
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
            source_date_basis="RAW_ACCOUNT_LEDGER_EVENT_DATE",
        )
    ]

    capital._verify_selected_facts(
        case,
        {
            "provider_ledger": {
                "capital_anchors": [],
                "flows": [incoming, outside],
                "admissions": [],
            }
        },
    )


@pytest.mark.parametrize("correction_kind", ["SOURCE_CORRECTION", "ECONOMIC_REVERSAL"])
def test_negative_source_correction_uses_magnitude_without_losing_classification(
    correction_kind,
):
    case = _case()
    case["required_epochs"][0]["account_id"] = "account-a"
    case["opening_basis"] = []
    case["flow_coverage"] = []
    original = {
        "record_id": "original",
        "account_id": "account-a",
        "strategy_id": "A",
        "flow_type": "EXTERNAL_CONTRIBUTION",
        "event_date": "2026-01-02",
        "amount": {"value": "5", "currency": "USD", "status": "AVAILABLE"},
    }
    corrected = {
        **original,
        "record_id": "correction",
        "event_date": "2026-01-03",
        "flow_type": "CORRECTION_REVERSAL",
        "amount": {"value": "-5", "currency": "USD", "status": "AVAILABLE"},
        "correction_id": "corrected-transaction",
        "correction_kind": correction_kind,
    }
    if correction_kind == "SOURCE_CORRECTION":
        corrected.update(
            replaces_event_id="original",
            replacement_classification="EXTERNAL_WITHDRAWAL",
        )
    else:
        corrected["reverses_event_id"] = "original"
    case["events"] = [
        _event(
            fact["record_id"],
            "a-1",
            fact["event_date"],
            fact["flow_type"],
            "5",
            source_evidence_sha256=capital._digest(fact),
            fx_evidence_sha256=capital._digest(fact),
            **{
                k: v
                for k, v in fact.items()
                if k.startswith("correction_")
                or k
                in {
                    "replaces_event_id",
                    "replacement_classification",
                    "reverses_event_id",
                }
            },
        )
        for fact in (original, corrected)
    ]
    capital._verify_selected_facts(
        case, {"provider_ledger": {"flows": [original, corrected]}}
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda c: c["opening_basis"].pop(), "opening basis"),
        (lambda c: c["flow_coverage"].pop(), "flow coverage"),
        (
            lambda c: c["opening_basis"].append(deepcopy(c["opening_basis"][0])),
            "duplicate",
        ),
        (
            lambda c: c["events"].append(
                _event(
                    "only-leg",
                    "a-1",
                    "2026-01-02",
                    "INTERNAL_TRANSFER",
                    "-5",
                    transfer_id="t",
                )
            ),
            "unpaired",
        ),
        (
            lambda c: c["events"].append(
                _event("wrong-sign", "a-1", "2026-01-02", "EXTERNAL_WITHDRAWAL", "-5")
            ),
            "sign",
        ),
        (
            lambda c: c["events"].append(
                _event("future", "a-1", "2026-01-04", "EXTERNAL_CONTRIBUTION", "5")
            ),
            "complete-through",
        ),
        (lambda c: c["opening_basis"][0].update(fx_rate="100"), "native USD"),
        (lambda c: c["opening_basis"][0].update(amount="NaN"), "finite"),
    ],
)
def test_missing_or_invalid_evidence_never_yields_a_capital_value(mutation, reason):
    case = _case()
    mutation(case)
    with pytest.raises(capital.CapitalValidationError, match=reason):
        capital.calculate_invested_capital(**case)


def test_verifier_accepts_the_native_constructors_wide_account_ledger_opening(
    tmp_path,
):
    """Astra rg4 class sweep: the verifier adds at the precision the producer used.

    The native constructor (``build_capital_input``) adds an account's selected owner
    capital at ``_capital_precision`` and publishes the exact opening amount.  The
    verifier re-added the same components in the ambient 28-digit context, so a
    32-digit deposit was rejected as an arithmetic mismatch.
    """

    from quant_engine.core.contracts import canonical_json_bytes

    cli, kwargs, epoch, deposit = _t5c_constructor_world(tmp_path)
    generation, _locators = cli.build_input_generation(**kwargs)
    opening = next(
        row
        for row in generation["opening_basis"]
        if row["account_id"] == epoch["account_id"]
    )
    assert opening["amount"] == "100.00000000000000000000000000002"

    components = capital._verify_account_ledger_opening(
        opening,
        {
            "account_id": epoch["account_id"],
            "effective_start": epoch["effective_start"],
        },
        {capital._digest(deposit): deposit},
        capital._load_flow_classification_rules(),
        {epoch["account_id"]},
    )

    assert components == {capital._digest(deposit)}
    assert canonical_json_bytes(generation)


def test_equal_and_opposite_wide_transfer_legs_are_internal():
    """Unary minus rounds: ``-native`` is not the opposite leg of a wide amount."""

    rules = capital._load_flow_classification_rules()
    wide = "100.000000000000000000000000000001"

    def leg(record_id: str, account_id: str, value: str) -> dict:
        return {
            "record_id": record_id,
            "account_id": account_id,
            "strategy_id": None,
            "flow_type": "ACCOUNT_TRANSFER",
            "event_date": "2026-01-02",
            "amount": {"value": value, "currency": "USD", "status": "AVAILABLE"},
        }

    dispositions = capital._account_flow_dispositions(
        [leg("out", "account-a", "-" + wide), leg("in", "account-b", wide)],
        rules,
        {"account-a", "account-b"},
    )

    assert dispositions == {"out": "INTERNAL", "in": "INTERNAL"}


def test_fund_scoped_account_capital_is_the_gross_fund_allocations_not_deposits():
    """#536: one account funds several funds, so deposits are not the strategy's capital."""

    case = _case()
    case["required_epochs"] = [
        {
            "epoch_id": "fund-1",
            "account_id": "fund-account",
            "strategy_id": "A",
            "effective_start": "2026-01-01",
            "effective_end": None,
        }
    ]
    case["opening_basis"] = []
    coverage = {
        "account_id": "fund-account",
        "fact_type": "flows",
        "disposition": "AUTHORITATIVE",
        "effective_start": "2026-01-01",
        "effective_end": "2026-01-03",
    }
    case["flow_coverage"] = [
        {
            "epoch_id": "fund-1",
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "evidence_sha256": capital._digest(coverage),
        }
    ]
    # 100 deposited; 60 + 25 allocated to this strategy's fund, 15 to another fund.
    deposit = {
        "record_id": "fund-deposit",
        "provider_id": "quantbase",
        "account_id": "fund-account",
        "strategy_id": None,
        "flow_type": "DEPOSIT_OR_WITHDRAWAL",
        "provider_account_scope": "ACCOUNT_ONLY",
        "capital_disposition": "EXTERNAL_CAPITAL",
        "event_date": "2026-01-01",
        "amount": {"value": "100", "currency": "USD", "status": "AVAILABLE"},
    }
    first = {
        "record_id": "fund-allocation-1",
        "provider_id": "quantbase",
        "account_id": "fund-account",
        "strategy_id": "A",
        "capital_type": "OPENING_CAPITAL_ANCHOR",
        "event_date": "2026-01-01",
        "amount": {"value": "60", "currency": "USD", "status": "AVAILABLE"},
    }
    second = {
        "record_id": "fund-allocation-2",
        "provider_id": "quantbase",
        "account_id": "fund-account",
        "strategy_id": "A",
        "capital_type": "STRATEGY_CAPITAL_ALLOCATION",
        "event_date": "2026-01-02",
        "amount": {"value": "25", "currency": "USD", "status": "AVAILABLE"},
    }

    def event_for(row, name):
        evidence = capital._digest(row)
        return _event(
            name,
            "fund-1",
            row["event_date"],
            "EXTERNAL_CONTRIBUTION",
            row["amount"]["value"],
            source_evidence_sha256=evidence,
            fx_evidence_sha256=evidence,
        )

    case["events"] = [event_for(first, "a-1"), event_for(second, "a-2")]
    ledger = {
        "capital_anchors": [first, second],
        "flows": [deposit],
        "admissions": [coverage],
    }
    capital._verify_selected_facts(case, {"provider_ledger": ledger}, case["policy"])
    calculation_case = deepcopy(case)
    calculation_case["opening_basis"] = [
        _event("fund-open", "fund-1", "2026-01-01", "OPENING_CONTRIBUTED_BASIS", "0")
    ]
    assert (
        capital.calculate_invested_capital(**calculation_case)["current_aggregate_usd"]
        == "85"
    )

    # The deposit is not this strategy's capital: citing it is refused.
    deposit_case = deepcopy(case)
    deposit_case["events"] = [event_for(deposit, "d-1")]
    with pytest.raises(
        capital.CapitalValidationError, match="not an owner-capital fact"
    ):
        capital._verify_selected_facts(
            deposit_case, {"provider_ledger": ledger}, case["policy"]
        )

    # An allocation left out of the capital input is an omitted capital fact.
    omitted_case = deepcopy(case)
    omitted_case["events"] = [event_for(first, "a-1")]
    with pytest.raises(capital.CapitalValidationError, match="is omitted"):
        capital._verify_selected_facts(
            omitted_case, {"provider_ledger": ledger}, case["policy"]
        )

    # Cash lines reassigned to another strategy or account never fund this one.
    for field, value, message in (
        ("strategy_id", "B", "fund-scoped allocation strategy differs"),
        ("account_id", "other-account", "capital source account/epoch differs"),
    ):
        moved = deepcopy(second)
        moved[field] = value
        moved_case = deepcopy(case)
        moved_case["events"] = [event_for(first, "a-1"), event_for(moved, "a-2")]
        with pytest.raises(capital.CapitalValidationError, match=message):
            capital._verify_selected_facts(
                moved_case,
                {
                    "provider_ledger": {
                        "capital_anchors": [first, moved],
                        "flows": [deposit],
                        "admissions": [coverage],
                    }
                },
                case["policy"],
            )

    # A provider the policy does not list keeps the account-deposit treatment.
    other_provider = deepcopy(deposit)
    other_provider["provider_id"] = "alpaca"
    other_anchor = deepcopy(first)
    other_anchor["provider_id"] = "alpaca"
    other_case = deepcopy(case)
    other_case["events"] = [event_for(other_provider, "d-1")]
    capital._verify_selected_facts(
        other_case,
        {
            "provider_ledger": {
                "capital_anchors": [other_anchor],
                "flows": [other_provider],
                "admissions": [coverage],
            }
        },
        case["policy"],
    )

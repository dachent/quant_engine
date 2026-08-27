"""Contract tests for the synthetic account-assignment perimeter producer."""

from __future__ import annotations

import ast
import builtins
from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

import pytest
from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "build_account_manifest_generation.py"
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "account-assignment-perimeter.schema.json"
)
ACCOUNT_STRATEGY_SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "account-strategy-manifest.schema.json"
)
BASELINE_GENERATION_ID = "b" * 64


def _semantic_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def perimeter_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "build_account_manifest_generation", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assignment(
    account_id: str,
    disposition: str,
    start: str,
    end: str | None,
    strategy_id: str | None,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "strategy_id": strategy_id,
        "strategy_mapping_disposition": disposition,
        "effective_start": start,
        "effective_end": end,
    }


def _generation_manifest_snapshot(
    *,
    catalog: InputSnapshot,
    catalog_artifact_id: str,
    catalog_schema_name: str,
    catalog_schema_version: str,
    as_of_date: str,
) -> InputSnapshot:
    source_date = date.fromisoformat(as_of_date)
    manifest = GenerationManifest.build(
        as_of_date=source_date,
        adapter=AdapterIdentity("synthetic-selected-input", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic selected input",
            fact_types=("synthetic",),
            observed_start=source_date,
            observed_end=source_date,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id=catalog_artifact_id,
                sha256=catalog.sha256,
                size_bytes=catalog.size_bytes,
                as_of_date=source_date,
                schema_name=catalog_schema_name,
                schema_version=catalog_schema_version,
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return InputSnapshot.from_bytes("synthetic generation manifest", manifest.to_json_bytes())


def _selected_inputs(module: Any, baseline: InputSnapshot | None = None) -> object:
    provider_ledger = InputSnapshot.from_bytes(
        "synthetic provider ledger",
        canonical_json_bytes({"as_of_date": "2026-01-31", "catalog_schema_version": 1}),
    )
    baseline = _fixture_inputs()["baseline"] if baseline is None else baseline
    assert isinstance(baseline, InputSnapshot)
    return module.AccountManifestSelectedInputs(
        provider_ledger=provider_ledger,
        provider_ledger_manifest=_generation_manifest_snapshot(
            catalog=provider_ledger,
            catalog_artifact_id="normalized/provider-ledger.json",
            catalog_schema_name="provider-ledger-catalog",
            catalog_schema_version="1",
            as_of_date="2026-01-31",
        ),
        baseline=baseline,
        baseline_manifest=_generation_manifest_snapshot(
            catalog=baseline,
            catalog_artifact_id="normalized/portfolio-baseline.json",
            catalog_schema_name="portfolio-baseline-catalog",
            catalog_schema_version="1",
            as_of_date="2026-02-01",
        ),
        flow_classification_policy=InputSnapshot.from_bytes("policy", b"policy"),
        retrieval_receipts={"synthetic-receipt": InputSnapshot.from_bytes("receipt", b"receipt")},
        epoch_producer_source=InputSnapshot.from_bytes("epoch", b"epoch"),
        generation_builder_source=InputSnapshot.from_bytes("builder", b"builder"),
        independent_verifier_source=InputSnapshot.from_bytes("verifier", b"verifier"),
    )


def _selected_input_kwargs(selected_inputs: object) -> dict[str, object]:
    return {
        "provider_ledger": selected_inputs.provider_ledger,
        "provider_ledger_manifest": selected_inputs.provider_ledger_manifest,
        "baseline": selected_inputs.baseline,
        "baseline_manifest": selected_inputs.baseline_manifest,
        "flow_classification_policy": selected_inputs.flow_classification_policy,
        "retrieval_receipts": selected_inputs.retrieval_receipts,
        "epoch_producer_source": selected_inputs.epoch_producer_source,
        "generation_builder_source": selected_inputs.generation_builder_source,
        "independent_verifier_source": selected_inputs.independent_verifier_source,
    }


def _c3_selected_inputs(module: Any) -> object:
    baseline = _fixture_inputs()["baseline"]
    assert isinstance(baseline, InputSnapshot)
    accounts = baseline.json_object()["accounts"]
    ledger = {
        "as_of_date": "2026-02-01",
        "catalog_schema_version": 1,
        "nav_observations": [
            {
                "record_id": f"c3-nav-{index:02d}",
                "account_id": account["account_id"],
                "as_of_date": "2026-02-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "100.00"},
            }
            for index, account in enumerate(accounts, 1)
        ],
        "flows": [
            {
                "record_id": f"c3-flow-{index:02d}",
                "account_id": account["account_id"],
                "event_date": "2026-02-01",
                "flow_type": "CONTRIBUTION" if index <= 15 else "UNAVAILABLE_FOREIGN",
                "amount": (
                    {"currency": "USD", "status": "AVAILABLE", "value": "1.00"}
                    if index <= 15
                    else {"currency": "CAD", "status": "UNAVAILABLE", "value": None}
                ),
            }
            for index, account in enumerate(accounts, 1)
        ],
    }
    provider_ledger = InputSnapshot.from_bytes(
        "c3 synthetic provider ledger", canonical_json_bytes(ledger)
    )
    policy = {
        "schema_version": 1,
        "artifact_id": "synthetic-flow-policy",
        "as_of_date": "2026-02-01",
        "policy_id": "SYNTHETIC_C3_POLICY",
        "policy_version": "1",
        "event_dispositions": {
            "exact": [
                {"flow_type": "CONTRIBUTION", "disposition": "EXTERNAL", "amount_sign": "POSITIVE"},
                {"flow_type": "UNAVAILABLE_FOREIGN", "disposition": "EXTERNAL", "amount_sign": "POSITIVE"},
            ],
            "prefix": [],
        },
    }
    selected = _selected_inputs(module, baseline)
    values = _selected_input_kwargs(selected)
    values["provider_ledger"] = provider_ledger
    values["provider_ledger_manifest"] = _generation_manifest_snapshot(
        catalog=provider_ledger,
        catalog_artifact_id="normalized/provider-ledger.json",
        catalog_schema_name="provider-ledger-catalog",
        catalog_schema_version="1",
        as_of_date="2026-02-01",
    )
    values["flow_classification_policy"] = InputSnapshot.from_bytes(
        "c3 noncanonical policy",
        json.dumps(policy, indent=2, ensure_ascii=True).encode("utf-8"),
    )
    return module.AccountManifestSelectedInputs(**values)


def _c3_with_mutated_ledger_or_policy(
    module: Any, ledger: dict[str, object], policy: dict[str, object]
) -> object:
    selected = _c3_selected_inputs(module)
    values = _selected_input_kwargs(selected)
    ledger_snapshot = InputSnapshot.from_bytes(
        "mutated c3 ledger", canonical_json_bytes(ledger)
    )
    values["provider_ledger"] = ledger_snapshot
    values["provider_ledger_manifest"] = _generation_manifest_snapshot(
        catalog=ledger_snapshot,
        catalog_artifact_id="normalized/provider-ledger.json",
        catalog_schema_name="provider-ledger-catalog",
        catalog_schema_version="1",
        as_of_date="2026-02-01",
    )
    values["flow_classification_policy"] = InputSnapshot.from_bytes(
        "mutated c3 policy", canonical_json_bytes(policy)
    )
    return module.AccountManifestSelectedInputs(**values)


def _fixture_inputs() -> dict[str, object]:
    """An 18-account / 26-row synthetic partition retained as acceptance evidence."""
    assignments: list[dict[str, object]] = []
    for number in range(1, 12):
        assignments.append(
            _assignment(
                f"synthetic-account-{number:02d}",
                "STRATEGY_MAPPED",
                "2025-01-01",
                None,
                f"synthetic-strategy-{number:02d}",
            )
        )
    for number in range(12, 16):
        account_id = f"synthetic-account-{number:02d}"
        assignments.extend(
            [
                _assignment(
                    account_id,
                    "STRATEGY_MAPPED",
                    "2025-01-01",
                    "2025-12-31",
                    f"synthetic-strategy-{number:02d}",
                ),
                _assignment(
                    account_id,
                    "HISTORICAL_POOL_ONLY",
                    "2026-01-01",
                    None,
                    None,
                ),
            ]
        )
    for number in range(16, 18):
        assignments.append(
            _assignment(
                f"synthetic-account-{number:02d}",
                "HISTORICAL_POOL_ONLY",
                "2025-01-01",
                None,
                None,
            )
        )
    assignments.extend(
        [
            _assignment(
                "synthetic-account-18",
                "HISTORICAL_POOL_ONLY",
                "2025-01-01",
                "2025-12-31",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-01",
                "2026-01-10",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-11",
                "2026-01-20",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-21",
                "2026-01-30",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-31",
                None,
                None,
            ),
        ]
    )
    accounts = [
        {"account_id": f"synthetic-account-{number:02d}", "provider": "SYNTHETIC"}
        for number in range(1, 19)
    ]
    baseline_payload = {
        "as_of_date": "2026-02-01",
        "baseline_schema_version": 1,
        "accounts": accounts,
        "assignments": assignments,
    }
    return {
        "baseline": InputSnapshot.from_bytes(
            "synthetic portfolio baseline", canonical_json_bytes(baseline_payload)
        ),
        "assignments": assignments,
        "nav_facts": [
            {
                "record_id": "nav-mapped",
                "account_id": "synthetic-account-01",
                "as_of_date": "2025-06-01",
                "nav": {"status": "AVAILABLE", "value": "100"},
            },
            {
                "record_id": "nav-historical",
                "account_id": "synthetic-account-12",
                "as_of_date": "2026-01-15",
                "nav": {"status": "UNAVAILABLE", "reason": "synthetic"},
            },
            {
                "record_id": "nav-reserve",
                "account_id": "synthetic-account-18",
                "as_of_date": "2026-01-15",
                "nav": {"status": "AVAILABLE", "value": "1"},
            },
        ],
        "raw_flow_facts": [
            {
                "record_id": "raw-mapped",
                "account_id": "synthetic-account-01",
                "event_date": "2025-06-01",
            },
            {
                "record_id": "raw-historical",
                "account_id": "synthetic-account-12",
                "event_date": "2026-01-15",
            },
            {
                "record_id": "raw-reserve",
                "account_id": "synthetic-account-18",
                "event_date": "2026-01-15",
            },
        ],
        "classified_flow_facts": [
            {
                "event_id": "classified-mapped",
                "source_record_id": "intentionally-unmatched-5c3-deferred",
                "account_id": "synthetic-account-01",
                "event_date": "2025-06-01",
            },
            {
                "event_id": "classified-historical",
                "source_record_id": "raw-historical",
                "account_id": "synthetic-account-12",
                "event_date": "2026-01-15",
            },
            {
                "event_id": "classified-reserve",
                "source_record_id": "raw-reserve",
                "account_id": "synthetic-account-18",
                "event_date": "2026-01-15",
            },
        ],
        "as_of_date": "2026-02-01",
    }


def _build(module: Any, inputs: dict[str, object]) -> bytes:
    baseline = inputs["baseline"]
    assert isinstance(baseline, InputSnapshot)
    return module.build_account_assignment_perimeter(
        selected_inputs=_selected_inputs(module, baseline),
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
    )


def _validate(module: Any, payload: dict[str, object], inputs: dict[str, object]) -> None:
    baseline = inputs["baseline"]
    assert isinstance(baseline, InputSnapshot)
    module.validate_account_assignment_perimeter(
        payload,
        selected_inputs=_selected_inputs(module, baseline),
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
    )


def _schema_validator() -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_validator_without_format_checker() -> Any:
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _account_strategy_schema_validator_without_format_checker() -> Any:
    from jsonschema import Draft202012Validator

    schema = json.loads(ACCOUNT_STRATEGY_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_red_contract_requires_new_producer_and_schema() -> None:
    """This deliberately fails before 5C.1 creates its two production artifacts."""
    assert MODULE_PATH.is_file(), "5C.1 producer module has not been created"
    assert SCHEMA_PATH.is_file(), "5C.1 perimeter schema has not been created"


def test_red_selected_inputs_own_the_generation_date_and_close_caller_injection(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    selected_inputs = _selected_inputs(perimeter_module)

    assert selected_inputs.generation_as_of_date == date(2026, 2, 1)
    payload = perimeter_module.build_account_assignment_perimeter(
        selected_inputs=selected_inputs,
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
    )
    assert json.loads(payload)["as_of_date"] == "2026-02-01"
    with pytest.raises(TypeError):
        perimeter_module.build_account_assignment_perimeter(
            selected_inputs=selected_inputs,
            nav_facts=inputs["nav_facts"],
            raw_flow_facts=inputs["raw_flow_facts"],
            classified_flow_facts=inputs["classified_flow_facts"],
            as_of_date="2026-12-31",
        )


def test_red_c3_builds_the_exact_nine_blocked_authority_payloads(
    perimeter_module: Any,
) -> None:
    selected_inputs = _c3_selected_inputs(perimeter_module)
    payloads = perimeter_module.build_blocked_account_manifest_payloads(selected_inputs)
    assert set(payloads) == {
        "authority/selected-input-identities.json",
        "authority/account-assignment-perimeter.json",
        "authority/calculation-inputs.json",
        "authority/flow-classification-policy.json",
        "authority/classified-flows.json",
        "authority/account-flow-coverage.json",
        "authority/mapped-epoch-admission.json",
        "authority/code-identities.json",
        "outputs/account-strategy-manifest.json",
    }
    calculation_inputs = json.loads(payloads["authority/calculation-inputs.json"])
    coverage = json.loads(payloads["authority/account-flow-coverage.json"])
    admission = json.loads(payloads["authority/mapped-epoch-admission.json"])
    output = json.loads(payloads["outputs/account-strategy-manifest.json"])
    from jsonschema import Draft202012Validator

    account_strategy_schema = json.loads(ACCOUNT_STRATEGY_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(account_strategy_schema).iter_errors(output))
    assert calculation_inputs["mapped_assignments_identity"]["count"] == 15
    assert calculation_inputs["mapped_assignments_identity"] == {
        "count": 15,
        "sha256": "87b17fffdd69e855a31aef206af51fb8acd18567d41218e330ac25bd48cc7240",
    }
    assert len(calculation_inputs["rows"]) == len(coverage["rows"]) == len(admission["rows"]) == len(output["rows"]) == 15
    assert {row["status"] for row in coverage["rows"]} == {"BLOCKED"}
    assert {row["status"] for row in admission["rows"]} == {"BLOCKED"}
    assert {row["status"] for row in output["rows"]} == {"BLOCKED_ADMISSION"}
    assert all(
        row["authority_kind"] == "NONE"
        and row["receipt_artifact_id"] is None
        and row["blocker_code"] == "NO_ADMISSIBLE_V2_RECEIPT"
        for row in coverage["rows"]
    )
    assert all(
        row["authority_kind"] == "NONE"
        and row["funded_nav_record_ids"] == []
        and row["continuity_to_successor"] is False
        and row["blocker_code"] == "FLOW_COMPLETENESS_NOT_PROVEN"
        for row in admission["rows"]
    )
    assert all(
        row["admission_status"] == "BLOCKED"
        and row["admission_policy_reference"] == "NONE"
        and row["admission_authority_kind"] == "NONE"
        and row["funded_nav_record_ids"] == []
        and row["continuity_to_successor"] is False
        and row["blocking_reason"] == "FLOW_COMPLETENESS_NOT_PROVEN"
        for row in output["rows"]
    )
    selected_policy_bytes = selected_inputs.flow_classification_policy.contents
    generated_policy_bytes = payloads["authority/flow-classification-policy.json"]
    assert selected_policy_bytes != generated_policy_bytes
    selected_identity = json.loads(payloads["authority/selected-input-identities.json"])
    assert selected_identity["flow_classification_policy"]["sha256"] == hashlib.sha256(selected_policy_bytes).hexdigest()
    forbidden = {
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observation_trace",
        "nav_observations",
    }
    assert not any(key in forbidden for row in output["rows"] for key in row)
    with pytest.raises(TypeError):
        payloads["other"] = b"other"


def test_red_v8_schema_pins_each_new_authority_artifact_contract(
    perimeter_module: Any,
) -> None:
    from jsonschema import Draft202012Validator

    output = json.loads(
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_selected_inputs(perimeter_module)
        )["outputs/account-strategy-manifest.json"]
    )
    schema = json.loads(ACCOUNT_STRATEGY_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for identity, artifact_id, schema_name, version in (
        ("selected_inputs", "authority/selected-input-identities.json", "selected-input-identities", "1"),
        ("account_assignment_perimeter", "authority/account-assignment-perimeter.json", "account-assignment-perimeter", "1"),
        ("calculation_inputs", "authority/calculation-inputs.json", "account-manifest-calculation-inputs", "1"),
        ("code_identities", "authority/code-identities.json", "account-manifest-code-identities", "1"),
    ):
        for field, wrong_value in (
            ("artifact_id", "wrong.json"),
            ("schema.name", "wrong-schema"),
            ("schema.version", "9"),
        ):
            mutated = deepcopy(output)
            artifact = mutated["source_identities"][identity]
            artifact["artifact_id"] = artifact_id
            artifact["schema"]["name"] = schema_name
            artifact["schema"]["version"] = version
            target = artifact
            for key in field.split("."):
                if key == field.split(".")[-1]:
                    target[key] = wrong_value
                else:
                    target = target[key]
            assert list(validator.iter_errors(mutated))


def test_c3_validator_rejects_exact_path_bytes_and_epoch_cross_binding_tampering(
    perimeter_module: Any,
) -> None:
    selected_inputs = _c3_selected_inputs(perimeter_module)
    payloads = perimeter_module.build_blocked_account_manifest_payloads(selected_inputs)
    for path, replacement in (
        ("authority/code-identities.json", b"{}"),
        ("extra.json", b"{}"),
    ):
        tampered = dict(payloads)
        tampered[path] = replacement
        with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
            perimeter_module.validate_blocked_account_manifest_payloads(tampered, selected_inputs)
    missing = dict(payloads)
    del missing["authority/mapped-epoch-admission.json"]
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
            perimeter_module.validate_blocked_account_manifest_payloads(missing, selected_inputs)


def test_red_c3_accepts_bound_provider_lineage_extras_but_projects_only_whitelist(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["nav_observations"][0]["provider_lineage"] = {"opaque": "bound-only"}
    ledger["flows"][0]["provider_lineage"] = {"opaque": "bound-only"}
    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
    )
    calculation = json.loads(payloads["authority/calculation-inputs.json"])
    assert all(
        "provider_lineage" not in observation
        for row in calculation["rows"]
        for observation in row["nav_observations"]
    )


def test_red_c3_rejects_unpaired_internal_flow(perimeter_module: Any) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    policy["event_dispositions"]["exact"][0]["disposition"] = "INTERNAL"
    policy["event_dispositions"]["exact"][0][
        "counterparty_rule"
    ] = "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
        )


def test_red_c3_selects_prefix_rules_from_the_public_policy_shape(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["flows"][0]["flow_type"] = "PREFIXED_CONTRIBUTION"
    policy["event_dispositions"]["prefix"] = [
        {
            "flow_type_prefix": "PREFIXED_",
            "disposition": "EXTERNAL",
            "amount_sign": "POSITIVE",
        }
    ]

    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
    )

    classified = json.loads(payloads["authority/classified-flows.json"])
    assert classified["events"][0]["flow_type"] == "PREFIXED_CONTRIBUTION"


def test_red_c3_rejects_internal_pair_to_external_counterpart(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["flows"][0].update(
        {"flow_type": "TRANSFER_OUT", "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-1.00"}}
    )
    ledger["flows"][1].update(
        {"flow_type": "TRANSFER_IN", "amount": {"currency": "USD", "status": "AVAILABLE", "value": "1.00"}}
    )
    policy["event_dispositions"]["exact"].extend(
        [
            {
                "flow_type": "TRANSFER_OUT",
                "disposition": "INTERNAL",
                "amount_sign": "NEGATIVE",
                "counterparty_rule": "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT",
            },
            {
                "flow_type": "TRANSFER_IN",
                "disposition": "EXTERNAL",
                "amount_sign": "POSITIVE",
            },
        ]
    )

    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
        )


def test_red_v8_schema_pins_policy_coverage_and_admission_artifacts(
    perimeter_module: Any,
) -> None:
    from jsonschema import Draft202012Validator

    output = json.loads(
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_selected_inputs(perimeter_module)
        )["outputs/account-strategy-manifest.json"]
    )
    schema = json.loads(ACCOUNT_STRATEGY_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for source, artifact_id, schema_name, version in (
        ("flow_classification_policy", "wrong-policy.json", "flow-classification-policy", "1"),
        ("flow_coverage", "authority/account-flow-coverage.json", "wrong-coverage", "2"),
        ("admission_policy", "authority/mapped-epoch-admission.json", "mapped-epoch-admission", "9"),
    ):
        mutated = deepcopy(output)
        artifact = mutated["source_identities"][source]["artifact"]
        artifact["artifact_id"] = artifact_id
        artifact["schema"]["name"] = schema_name
        artifact["schema"]["version"] = version
        assert list(validator.iter_errors(mutated))


def test_red_v8_schema_intrinsically_validates_nested_gregorian_dates(
    perimeter_module: Any,
) -> None:
    output = json.loads(
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_selected_inputs(perimeter_module)
        )["outputs/account-strategy-manifest.json"]
    )
    validator = _account_strategy_schema_validator_without_format_checker()
    assert not list(validator.iter_errors(output))
    output["source_identities"]["flow_classification_policy"]["artifact"][
        "as_of_date"
    ] = "2026-02-30"
    assert list(validator.iter_errors(output))


@pytest.mark.parametrize("valid_date", ["2000-02-29", "2024-02-29", "2400-02-29"])
@pytest.mark.parametrize("invalid_date", ["1900-02-29", "2100-02-29", "2026-02-30"])
def test_red_v8_schema_intrinsic_century_leap_year_dates(
    perimeter_module: Any, valid_date: str, invalid_date: str
) -> None:
    output = json.loads(
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_selected_inputs(perimeter_module)
        )["outputs/account-strategy-manifest.json"]
    )
    validator = _account_strategy_schema_validator_without_format_checker()
    output["source_identities"]["flow_classification_policy"]["artifact"][
        "as_of_date"
    ] = valid_date
    assert not list(validator.iter_errors(output))
    output["source_identities"]["flow_classification_policy"]["artifact"][
        "as_of_date"
    ] = invalid_date
    assert list(validator.iter_errors(output))


def test_c3_internal_events_bind_same_currency_reciprocally(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["flows"][0].update(
        {"flow_type": "TRANSFER_OUT", "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-1.00"}}
    )
    ledger["flows"][1].update(
        {"flow_type": "TRANSFER_IN", "amount": {"currency": "USD", "status": "AVAILABLE", "value": "1.00"}}
    )
    for flow_type, sign in (("TRANSFER_OUT", "NEGATIVE"), ("TRANSFER_IN", "POSITIVE")):
        policy["event_dispositions"]["exact"].append(
            {
                "flow_type": flow_type,
                "disposition": "INTERNAL",
                "amount_sign": sign,
                "counterparty_rule": "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT",
            }
        )
    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
    )
    events = json.loads(payloads["authority/classified-flows.json"])["events"]
    indexed = {event["source_record_id"]: event for event in events}
    assert indexed["c3-flow-01"]["counterparty_source_record_id"] == "c3-flow-02"
    assert indexed["c3-flow-02"]["counterparty_source_record_id"] == "c3-flow-01"


def test_c3_validator_rejects_each_exact_path_and_epoch_bijection_tamper(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    payloads = perimeter_module.build_blocked_account_manifest_payloads(selected)
    for path in payloads:
        tampered = dict(payloads)
        tampered[path] = b"{}"
        with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
            perimeter_module.validate_blocked_account_manifest_payloads(
                tampered, selected
            )
    parsed = {
        path: json.loads(contents) for path, contents in payloads.items()
    }
    epoch_keys = ("account_id", "strategy_id", "effective_start", "effective_end")
    expected_epochs = {
        tuple(row["epoch"][key] for key in epoch_keys)
        for row in parsed["authority/calculation-inputs.json"]["rows"]
    }
    assert len(expected_epochs) == 15
    assert expected_epochs == {
        tuple(row["assignment"][key] for key in epoch_keys)
        for row in parsed["authority/account-flow-coverage.json"]["rows"]
    }
    assert expected_epochs == {
        tuple(row["assignment"][key] for key in epoch_keys)
        for row in parsed["authority/mapped-epoch-admission.json"]["rows"]
    }
    assert expected_epochs == {
        tuple(
            row[key] for key in epoch_keys
        )
        for row in parsed["outputs/account-strategy-manifest.json"]["rows"]
    }
    for path, rows_key in (
        ("authority/account-flow-coverage.json", "rows"),
        ("authority/mapped-epoch-admission.json", "rows"),
        ("outputs/account-strategy-manifest.json", "rows"),
    ):
        for mutation in ("delete", "duplicate", "swap"):
            tampered = {name: json.loads(value) for name, value in payloads.items()}
            rows = tampered[path][rows_key]
            if mutation == "delete":
                rows.pop()
            elif mutation == "duplicate":
                rows.append(deepcopy(rows[0]))
            else:
                if path == "outputs/account-strategy-manifest.json":
                    rows[0]["strategy_id"] = rows[1]["strategy_id"]
                else:
                    rows[0]["assignment"]["strategy_id"] = rows[1]["assignment"]["strategy_id"]
            encoded = {
                name: canonical_json_bytes(value) for name, value in tampered.items()
            }
            with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
                perimeter_module.validate_blocked_account_manifest_payloads(encoded, selected)


def test_c3_rejects_compact_invalid_retained_source_vectors(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    for label in (
        "unavailable-non-null",
        "wrong-available-currency",
        "sign-violation",
        "unresolved-rule",
    ):
        candidate_ledger = deepcopy(ledger)
        candidate_policy = deepcopy(policy)
        if label == "unavailable-non-null":
            candidate_ledger["flows"][15]["amount"]["value"] = "1.00"
        elif label == "wrong-available-currency":
            candidate_ledger["flows"][0]["amount"]["currency"] = "CAD"
        elif label == "sign-violation":
            candidate_ledger["flows"][0]["amount"]["value"] = "-1.00"
        else:
            candidate_ledger["flows"][0]["flow_type"] = "UNKNOWN_FLOW"
        with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
            perimeter_module.build_blocked_account_manifest_payloads(
                _c3_with_mutated_ledger_or_policy(
                    perimeter_module, candidate_ledger, candidate_policy
                )
            )


def test_red_c3_rejects_noninternal_account_ids_without_reemission(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    sentinel = "9" * 12
    ledger["nav_observations"][0]["account_id"] = sentinel
    ledger["flows"][0]["account_id"] = sentinel
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError) as raised:
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
        )
    assert sentinel not in str(raised.value)

    valid_ledger = _c3_selected_inputs(perimeter_module).provider_ledger.json_object()
    valid_ledger["flows"][0]["provider_lineage"] = {"opaque": sentinel}
    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, valid_ledger, policy)
    )
    assert all(sentinel.encode("ascii") not in contents for contents in payloads.values())


def test_red_c3_preserves_only_normalized_unavailable_nav_reason(
    perimeter_module: Any,
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["nav_observations"][0]["nav"] = {
        "currency": "USD", "status": "UNAVAILABLE", "value": None, "reason": " retained "
    }
    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
    )
    calculation = json.loads(payloads["authority/calculation-inputs.json"])
    assert any(
        nav["nav"].get("reason") == "retained"
        for row in calculation["rows"]
        for nav in row["nav_observations"]
    )
    for invalid_reason in ("", 1):
        invalid = deepcopy(ledger)
        invalid["nav_observations"][0]["nav"]["reason"] = invalid_reason
        with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
            perimeter_module.build_blocked_account_manifest_payloads(
                _c3_with_mutated_ledger_or_policy(perimeter_module, invalid, policy)
            )
    invalid = deepcopy(ledger)
    invalid["flows"][0]["amount"]["reason"] = "not-permitted"
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_with_mutated_ledger_or_policy(perimeter_module, invalid, policy)
        )


def test_v8_schema_rejects_numeric_fields_for_every_non_ok_status(
    perimeter_module: Any,
) -> None:
    output = json.loads(
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_selected_inputs(perimeter_module)
        )["outputs/account-strategy-manifest.json"]
    )
    validator = _account_strategy_schema_validator_without_format_checker()
    forbidden = (
        "start", "end", "opening_capital", "deposits", "internal_in", "invested",
        "external_withdrawals", "internal_out", "withdrawn", "net_capital",
        "current_value", "gain", "return_on_net_capital", "annualized_return_on_net_capital",
        "xirr_diagnostic", "observation_linked_return", "observed_hwm_index",
        "observed_max_drawdown", "opening_basis", "observation_trace", "capital_flow_trace",
        "nav_observation_trace", "nav_observations",
    )
    statuses = (
        "BLOCKED_ADMISSION", "NO_ADMITTED_NAV", "FLOWS_AFTER_CLOSING_NAV",
        "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL", "BLOCKED_UNDEFINED_RETURN",
        "BLOCKED_UNPROVABLE_OPENING_FLOWS", "BLOCKED_UNAVAILABLE_NAV_OBSERVATION",
        "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
    )
    for status in statuses:
        status_base = deepcopy(output)
        base_row = status_base["rows"][0]
        base_row["status"] = status
        if status == "FLOWS_AFTER_CLOSING_NAV":
            base_row.pop("blocking_reason")
        if status != "BLOCKED_ADMISSION":
            base_row.update(
                {
                    "admission_status": "FUNDED",
                    "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "funded_nav_record_ids": ["retained-nav"],
                }
            )
        assert not list(validator.iter_errors(status_base))
        for field in forbidden:
            for forbidden_value in (0, None):
                mutated = deepcopy(status_base)
                row = mutated["rows"][0]
                row[field] = forbidden_value
                assert list(validator.iter_errors(mutated))


def test_v7_excludes_v8_subjects_and_old_admission_loader_rejects_generated_v1(
    perimeter_module: Any,
) -> None:
    from tools.account_strategy_manifest import ManifestError, load_admission_policy

    selected = _c3_selected_inputs(perimeter_module)
    payloads = perimeter_module.build_blocked_account_manifest_payloads(selected)
    output = json.loads(payloads["outputs/account-strategy-manifest.json"])
    output["schema_version"] = 7
    assert list(_account_strategy_schema_validator_without_format_checker().iter_errors(output))
    admission = json.loads(payloads["authority/mapped-epoch-admission.json"])
    with pytest.raises(ManifestError):
        load_admission_policy(
            admission,
            InputSnapshot.from_bytes("generated admission", canonical_json_bytes(admission)),
            [],
            {},
        )


@pytest.mark.parametrize("amount", ["-0", "-0.0", "-0.00"])
def test_red_c3_rejects_negative_zero_but_allows_negative_cents(
    perimeter_module: Any, amount: str
) -> None:
    selected = _c3_selected_inputs(perimeter_module)
    ledger = selected.provider_ledger.json_object()
    policy = selected.flow_classification_policy.json_object()
    ledger["flows"][0].update(
        {"flow_type": "TRANSFER_OUT", "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount}}
    )
    policy["event_dispositions"]["exact"].append(
        {"flow_type": "TRANSFER_OUT", "disposition": "EXTERNAL", "amount_sign": "NEGATIVE"}
    )
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError):
        perimeter_module.build_blocked_account_manifest_payloads(
            _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
        )

    ledger["flows"][0]["amount"]["value"] = "-0.01"
    payloads = perimeter_module.build_blocked_account_manifest_payloads(
        _c3_with_mutated_ledger_or_policy(perimeter_module, ledger, policy)
    )
    classified = json.loads(payloads["authority/classified-flows.json"])
    assert classified["events"][0]["amount"]["value"] == "-0.01"


@pytest.mark.parametrize(
    ("ledger_date", "baseline_date", "expected"),
    [
        ("2026-01-31", "2026-02-01", "2026-02-01"),
        ("2026-03-01", "2026-02-28", "2026-03-01"),
        ("2026-02-01", "2026-02-01", "2026-02-01"),
        ("2026-10-01", "2026-09-30", "2026-10-01"),
    ],
)
def test_derived_generation_date_is_the_later_selected_catalog_date(
    perimeter_module: Any, ledger_date: str, baseline_date: str, expected: str
) -> None:
    provider_ledger = InputSnapshot.from_bytes(
        "ledger diagnostic label",
        canonical_json_bytes({"as_of_date": ledger_date, "catalog_schema_version": 1}),
    )
    baseline_payload = _fixture_inputs()["baseline"].json_object()
    baseline_payload["as_of_date"] = baseline_date
    baseline = InputSnapshot.from_bytes(
        "baseline diagnostic label", canonical_json_bytes(baseline_payload)
    )

    assert perimeter_module.derive_generation_as_of_date(provider_ledger, baseline) == date.fromisoformat(expected)


def test_unequal_selected_source_dates_preserve_source_dates_but_use_stored_max(
    perimeter_module: Any,
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    ledger = InputSnapshot.from_bytes(
        "later ledger",
        canonical_json_bytes({"as_of_date": "2026-03-01", "catalog_schema_version": 1}),
    )
    constructor_inputs = _selected_input_kwargs(selected_inputs)
    constructor_inputs["provider_ledger"] = ledger
    constructor_inputs["provider_ledger_manifest"] = _generation_manifest_snapshot(
        catalog=ledger,
        catalog_artifact_id="normalized/provider-ledger.json",
        catalog_schema_name="provider-ledger-catalog",
        catalog_schema_version="1",
        as_of_date="2026-03-01",
    )
    later_selected_inputs = perimeter_module.AccountManifestSelectedInputs(
        **constructor_inputs
    )
    identities = json.loads(
        perimeter_module.build_selected_input_identities(later_selected_inputs)
    )
    facts = _fixture_inputs()
    perimeter = json.loads(
        perimeter_module.build_account_assignment_perimeter(
            selected_inputs=later_selected_inputs,
            nav_facts=facts["nav_facts"],
            raw_flow_facts=facts["raw_flow_facts"],
            classified_flow_facts=facts["classified_flow_facts"],
        )
    )

    assert later_selected_inputs.generation_as_of_date == date(2026, 3, 1)
    assert identities["as_of_date"] == perimeter["as_of_date"] == "2026-03-01"
    assert identities["provider_ledger"]["artifact"]["as_of_date"] == "2026-03-01"
    assert identities["provider_ledger"]["generation_manifest"]["as_of_date"] == "2026-03-01"
    assert identities["baseline"]["artifact"]["as_of_date"] == "2026-02-01"
    assert identities["baseline"]["generation_manifest"]["as_of_date"] == "2026-02-01"


@pytest.mark.parametrize(
    "invalid_date",
    [
        None,
        True,
        20260201,
        20260201.0,
        " 2026-02-01",
        "2026-2-1",
        "2026-02-01T00:00:00",
        "2026-W05-7",
        "2026-032",
        "0000-01-01",
        "2026-02-30",
    ],
)
@pytest.mark.parametrize("source", ["provider_ledger", "baseline"])
def test_date_derivation_rejects_every_noncanonical_selected_input_date(
    perimeter_module: Any, source: str, invalid_date: object
) -> None:
    valid_ledger = InputSnapshot.from_bytes(
        "ledger", canonical_json_bytes({"as_of_date": "2026-02-01"})
    )
    valid_baseline = InputSnapshot.from_bytes(
        "baseline", canonical_json_bytes({"as_of_date": "2026-02-01"})
    )
    invalid = InputSnapshot.from_bytes(
        "invalid", canonical_json_bytes({"as_of_date": invalid_date})
    )
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="selected input as_of_date must be a canonical ISO date",
    ):
        perimeter_module.derive_generation_as_of_date(
            invalid if source == "provider_ledger" else valid_ledger,
            invalid if source == "baseline" else valid_baseline,
        )


def test_selected_input_identities_are_frozen_and_validate_exactly(
    perimeter_module: Any,
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    payload = json.loads(perimeter_module.build_selected_input_identities(selected_inputs))

    assert payload["as_of_date"] == "2026-02-01"
    assert payload["provider_ledger"]["generation_id"] == selected_inputs.provider_ledger_generation_id
    assert payload["baseline"]["generation_id"] == selected_inputs.baseline_generation_id
    assert [row["receipt_id"] for row in payload["retrieval_receipts"]] == ["synthetic-receipt"]
    perimeter_module.validate_selected_input_identities(payload, selected_inputs)

    payload["as_of_date"] = "2026-12-31"
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="payload differs from expected selected inputs",
    ):
        perimeter_module.validate_selected_input_identities(payload, selected_inputs)
    with pytest.raises((AttributeError, TypeError)):
        selected_inputs.generation_as_of_date = date(2026, 12, 31)
    with pytest.raises(TypeError):
        selected_inputs.retrieval_receipts["different"] = InputSnapshot.from_bytes("x", b"x")
    with pytest.raises(TypeError):
        perimeter_module.AccountManifestSelectedInputs(
            provider_ledger=selected_inputs.provider_ledger,
            provider_ledger_manifest=selected_inputs.provider_ledger_manifest,
            baseline=selected_inputs.baseline,
            baseline_manifest=selected_inputs.baseline_manifest,
            flow_classification_policy=selected_inputs.flow_classification_policy,
            retrieval_receipts=selected_inputs.retrieval_receipts,
            epoch_producer_source=selected_inputs.epoch_producer_source,
            generation_builder_source=selected_inputs.generation_builder_source,
            independent_verifier_source=selected_inputs.independent_verifier_source,
            generation_as_of_date=date(2026, 2, 1),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_ledger_generation_id", "0" * 64),
        ("baseline_generation_id", "0" * 64),
        ("generation_as_of_date", date(2026, 2, 1)),
    ],
)
def test_selected_input_constructor_rejects_derived_field_injection(
    perimeter_module: Any, field: str, value: object
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    constructor_inputs = {
        "provider_ledger": selected_inputs.provider_ledger,
        "provider_ledger_manifest": selected_inputs.provider_ledger_manifest,
        "baseline": selected_inputs.baseline,
        "baseline_manifest": selected_inputs.baseline_manifest,
        "flow_classification_policy": selected_inputs.flow_classification_policy,
        "retrieval_receipts": selected_inputs.retrieval_receipts,
        "epoch_producer_source": selected_inputs.epoch_producer_source,
        "generation_builder_source": selected_inputs.generation_builder_source,
        "independent_verifier_source": selected_inputs.independent_verifier_source,
        field: value,
    }
    with pytest.raises(TypeError):
        perimeter_module.AccountManifestSelectedInputs(**constructor_inputs)


def test_selected_inputs_reject_manifest_bytes_or_catalog_binding_mutation(
    perimeter_module: Any,
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    mutated_manifest = InputSnapshot.from_bytes(
        "diagnostic-only",
        selected_inputs.baseline_manifest.contents.rstrip() + b" ",
    )
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="selected manifest must use canonical bytes",
    ):
        perimeter_module.AccountManifestSelectedInputs(
            provider_ledger=selected_inputs.provider_ledger,
            provider_ledger_manifest=selected_inputs.provider_ledger_manifest,
            baseline=selected_inputs.baseline,
            baseline_manifest=mutated_manifest,
            flow_classification_policy=selected_inputs.flow_classification_policy,
            retrieval_receipts=selected_inputs.retrieval_receipts,
            epoch_producer_source=selected_inputs.epoch_producer_source,
            generation_builder_source=selected_inputs.generation_builder_source,
            independent_verifier_source=selected_inputs.independent_verifier_source,
        )

    altered_baseline = InputSnapshot.from_bytes(
        "different path and label are irrelevant",
        selected_inputs.baseline.contents + b" ",
    )
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="selected manifest catalog artifact differs from frozen catalog",
    ):
        perimeter_module.AccountManifestSelectedInputs(
            provider_ledger=selected_inputs.provider_ledger,
            provider_ledger_manifest=selected_inputs.provider_ledger_manifest,
            baseline=altered_baseline,
            baseline_manifest=selected_inputs.baseline_manifest,
            flow_classification_policy=selected_inputs.flow_classification_policy,
            retrieval_receipts=selected_inputs.retrieval_receipts,
            epoch_producer_source=selected_inputs.epoch_producer_source,
            generation_builder_source=selected_inputs.generation_builder_source,
            independent_verifier_source=selected_inputs.independent_verifier_source,
        )


def test_selected_identity_bytes_ignore_labels_paths_and_environment(
    perimeter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    alternate_inputs = _selected_input_kwargs(selected_inputs)
    alternate_inputs["provider_ledger"] = InputSnapshot(
        label="other ledger diagnostic label",
        contents=selected_inputs.provider_ledger.contents,
        path=Path("C:/not-an-authority/ledger.json"),
    )
    alternate_inputs["provider_ledger_manifest"] = InputSnapshot(
        label="other ledger manifest diagnostic label",
        contents=selected_inputs.provider_ledger_manifest.contents,
        path=Path("C:/not-an-authority/generation.json"),
    )
    alternate_inputs["baseline"] = InputSnapshot(
        label="other baseline diagnostic label",
        contents=selected_inputs.baseline.contents,
        path=Path("C:/not-an-authority/baseline.json"),
    )
    alternate_inputs["baseline_manifest"] = InputSnapshot(
        label="other baseline manifest diagnostic label",
        contents=selected_inputs.baseline_manifest.contents,
        path=Path("C:/not-an-authority/generation.json"),
    )
    alternate = perimeter_module.AccountManifestSelectedInputs(**alternate_inputs)
    expected = perimeter_module.build_selected_input_identities(selected_inputs)

    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LANGUAGE", "C")
    assert perimeter_module.build_selected_input_identities(alternate) == expected
    assert os.environ["TZ"] == "Pacific/Auckland"


def test_selected_input_path_has_no_runtime_access_after_capture(
    perimeter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    expected = perimeter_module.build_selected_input_identities(selected_inputs)

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("selected-input construction accessed the filesystem")

    monkeypatch.setattr(Path, "read_bytes", fail)
    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(Path, "resolve", fail)
    monkeypatch.setattr(Path, "stat", fail)
    monkeypatch.setattr(os, "stat", fail)
    monkeypatch.setattr(os, "lstat", fail)
    assert perimeter_module.build_selected_input_identities(selected_inputs) == expected


def test_selected_input_errors_do_not_expose_snapshot_diagnostics(
    perimeter_module: Any,
) -> None:
    secret_label = "SECRET-SNAPSHOT-LABEL"
    secret_path = "C:/secret/selected-input.json"
    secret_bytes = b"SECRET-SNAPSHOT-BYTES"
    invalid_json = InputSnapshot(
        label=secret_label,
        contents=secret_bytes,
        path=Path(secret_path),
    )
    invalid_date = InputSnapshot(
        label=secret_label,
        contents=canonical_json_bytes({"as_of_date": "not-a-date"}),
        path=Path(secret_path),
    )

    for snapshot in (invalid_json, invalid_date):
        with pytest.raises(perimeter_module.AccountAssignmentPerimeterError) as raised:
            perimeter_module.derive_generation_as_of_date(
                snapshot,
                InputSnapshot.from_bytes(
                    "safe", canonical_json_bytes({"as_of_date": "2026-02-01"})
                ),
            )
        details = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
        assert secret_label not in details
        assert secret_path not in details
        assert secret_bytes.decode("ascii") not in details
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is True


def test_selected_inputs_reject_noncanonical_receipt_ids(perimeter_module: Any) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    constructor_inputs = _selected_input_kwargs(selected_inputs)
    constructor_inputs["retrieval_receipts"] = {
        " receipt ": InputSnapshot.from_bytes("receipt", b"receipt")
    }
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="retrieval receipt ID must be a portable canonical artifact identifier",
    ):
        perimeter_module.AccountManifestSelectedInputs(**constructor_inputs)


@pytest.mark.parametrize(
    "receipt_id",
    [
        "receipt/embedded\nnewline.json",
        "receipt/embedded\x01control.json",
        "/absolute/receipt.json",
        "C:/drive/receipt.json",
        "receipt\\backslash.json",
        "receipt//empty-component.json",
        "receipt/../traversal.json",
        "receipt/trailing-dot.",
        "receipt/trailing-space ",
        "CON/receipt.json",
        "receipt/r\u00e9ceipt.json",
    ],
)
def test_selected_inputs_reject_nonportable_receipt_identifiers(
    perimeter_module: Any, receipt_id: str
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    constructor_inputs = _selected_input_kwargs(selected_inputs)
    constructor_inputs["retrieval_receipts"] = {
        receipt_id: InputSnapshot.from_bytes("receipt", b"receipt")
    }
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="retrieval receipt ID must be a portable canonical artifact identifier",
    ):
        perimeter_module.AccountManifestSelectedInputs(**constructor_inputs)


def test_selected_inputs_reject_casefold_colliding_receipt_identifiers(
    perimeter_module: Any,
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    constructor_inputs = _selected_input_kwargs(selected_inputs)
    receipt = InputSnapshot.from_bytes("receipt", b"receipt")
    constructor_inputs["retrieval_receipts"] = {
        "receipt/synthetic.json": receipt,
        "RECEIPT/SYNTHETIC.JSON": receipt,
    }
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="retrieval receipt IDs must be unique case-insensitively",
    ):
        perimeter_module.AccountManifestSelectedInputs(**constructor_inputs)


def test_selected_inputs_accept_portable_canonical_receipt_identifier(
    perimeter_module: Any,
) -> None:
    selected_inputs = _selected_inputs(perimeter_module)
    constructor_inputs = _selected_input_kwargs(selected_inputs)
    receipt = InputSnapshot.from_bytes("receipt", b"receipt")
    constructor_inputs["retrieval_receipts"] = {
        "receipt/synthetic-2026-01-01-2026-01-03.json": receipt
    }
    accepted = perimeter_module.AccountManifestSelectedInputs(**constructor_inputs)
    assert list(accepted.retrieval_receipts) == [
        "receipt/synthetic-2026-01-01-2026-01-03.json"
    ]


def test_selected_input_public_seams_exclude_forbidden_time_and_filesystem_dependencies(
    perimeter_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    selected_inputs_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "AccountManifestSelectedInputs"
    )
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "__post_init__"
        for node in selected_inputs_class.body
    )
    reachable = {
        "__post_init__",
        "derive_generation_as_of_date",
        "build_selected_input_identities",
        "validate_selected_input_identities",
        "build_account_assignment_perimeter",
        "validate_account_assignment_perimeter",
        "build_blocked_account_manifest_payloads",
        "validate_blocked_account_manifest_payloads",
    }
    pending = list(reachable)
    while pending:
        function = functions[pending.pop()]
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id in functions:
                if call.func.id not in reachable:
                    reachable.add(call.func.id)
                    pending.append(call.func.id)
    assert {
        "__post_init__",
        "_canonical_receipt_id",
        "_require_snapshot",
        "_bound_catalog_identity",
        "_selected_top_level_date",
        "_nonnegative_schema_version",
    } <= reachable

    forbidden_imports = {
        "datetime",
        "time",
        "locale",
        "zoneinfo",
        "platform",
        "os",
        "pathlib",
        "importlib",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not (
                {name.name.split(".")[0] for name in node.names} & forbidden_imports
            )
        if isinstance(node, ast.ImportFrom):
            if node.module == "datetime":
                assert [name.name for name in node.names] == ["date"]
            else:
                assert node.module not in forbidden_imports

    forbidden_call_names = {"__import__", "eval", "exec", "import_module"}
    forbidden_attributes = {
        "environ",
        "getenv",
        "getenvb",
        "lstat",
        "now",
        "open",
        "read_bytes",
        "read_text",
        "resolve",
        "stat",
        "today",
        "fromtimestamp",
        "timestamp",
    }
    for function_name in reachable:
        for node in ast.walk(functions[function_name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_call_names
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden_dependency in (
        "ManifestCandidateSnapshots",
        "build_manifest_candidate_from_snapshots",
        "load_ledger",
        "load_flow_classes",
        "validate_classified_flow_coverage",
    ):
        assert forbidden_dependency not in source

    class GuardedDate:
        @staticmethod
        def fromisoformat(value: str) -> date:
            return date.fromisoformat(value)

        @staticmethod
        def today() -> date:
            raise AssertionError("clock access is forbidden")

        @staticmethod
        def now() -> date:
            raise AssertionError("clock access is forbidden")

        @staticmethod
        def fromtimestamp(value: float) -> date:
            raise AssertionError("timestamp conversion is forbidden")

        @staticmethod
        def timestamp() -> float:
            raise AssertionError("timestamp conversion is forbidden")

    monkeypatch.setattr(perimeter_module, "date", GuardedDate)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: object = (),
        level: int = 0,
    ) -> object:
        if name.split(".")[0] in forbidden_imports:
            raise AssertionError(f"forbidden import: {name}")
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    selected_inputs = _selected_inputs(perimeter_module)
    identities = json.loads(
        perimeter_module.build_selected_input_identities(selected_inputs)
    )
    perimeter_module.validate_selected_input_identities(identities, selected_inputs)
    inputs = _fixture_inputs()
    perimeter = json.loads(
        perimeter_module.build_account_assignment_perimeter(
            selected_inputs=selected_inputs,
            nav_facts=inputs["nav_facts"],
            raw_flow_facts=inputs["raw_flow_facts"],
            classified_flow_facts=inputs["classified_flow_facts"],
        )
    )
    perimeter_module.validate_account_assignment_perimeter(
        perimeter,
        selected_inputs=selected_inputs,
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
    )


def test_captured_snapshots_ignore_post_capture_filesystem_changes(
    perimeter_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def capture(filename: str, label: str, contents: bytes) -> tuple[Path, InputSnapshot]:
        path = tmp_path / filename
        path.write_bytes(contents)
        return path, InputSnapshot.capture(path, label)

    baseline_bytes = _fixture_inputs()["baseline"].contents
    ledger_path, provider_ledger = capture(
        "ledger.json",
        "captured ledger",
        canonical_json_bytes({"as_of_date": "2026-01-31", "catalog_schema_version": 1}),
    )
    baseline_path, baseline = capture("baseline.json", "captured baseline", baseline_bytes)
    ledger_manifest_path, provider_ledger_manifest = capture(
        "ledger-generation.json",
        "captured ledger manifest",
        _generation_manifest_snapshot(
            catalog=provider_ledger,
            catalog_artifact_id="normalized/provider-ledger.json",
            catalog_schema_name="provider-ledger-catalog",
            catalog_schema_version="1",
            as_of_date="2026-01-31",
        ).contents,
    )
    baseline_manifest_path, baseline_manifest = capture(
        "baseline-generation.json",
        "captured baseline manifest",
        _generation_manifest_snapshot(
            catalog=baseline,
            catalog_artifact_id="normalized/portfolio-baseline.json",
            catalog_schema_name="portfolio-baseline-catalog",
            catalog_schema_version="1",
            as_of_date="2026-02-01",
        ).contents,
    )
    _, policy = capture("policy.json", "captured policy", b"policy")
    _, receipt = capture("receipt.json", "captured receipt", b"receipt")
    _, epoch = capture("epoch.py", "captured epoch", b"epoch")
    _, builder = capture("builder.py", "captured builder", b"builder")
    _, verifier = capture("verifier.py", "captured verifier", b"verifier")
    selected_inputs = perimeter_module.AccountManifestSelectedInputs(
        provider_ledger=provider_ledger,
        provider_ledger_manifest=provider_ledger_manifest,
        baseline=baseline,
        baseline_manifest=baseline_manifest,
        flow_classification_policy=policy,
        retrieval_receipts={"receipt": receipt},
        epoch_producer_source=epoch,
        generation_builder_source=builder,
        independent_verifier_source=verifier,
    )
    facts = _fixture_inputs()
    expected_identities = perimeter_module.build_selected_input_identities(selected_inputs)
    expected_perimeter = perimeter_module.build_account_assignment_perimeter(
        selected_inputs=selected_inputs,
        nav_facts=facts["nav_facts"],
        raw_flow_facts=facts["raw_flow_facts"],
        classified_flow_facts=facts["classified_flow_facts"],
    )
    changed_ledger = canonical_json_bytes(
        {"as_of_date": "2026-03-01", "catalog_schema_version": 1}
    )
    ledger_path.write_bytes(changed_ledger)
    baseline_path.write_bytes(b"changed baseline bytes")
    ledger_manifest_path.write_bytes(b"changed ledger manifest bytes")
    baseline_manifest_path.write_bytes(b"changed baseline manifest bytes")
    os.utime(ledger_path, (1, 1))

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("post-capture selected-input access touched the filesystem")

    with monkeypatch.context() as trapped:
        trapped.setattr(Path, "read_bytes", fail)
        trapped.setattr(Path, "open", fail)
        trapped.setattr(Path, "resolve", fail)
        trapped.setattr(Path, "stat", fail)
        trapped.setattr(os, "stat", fail)
        trapped.setattr(os, "lstat", fail)
        assert perimeter_module.build_selected_input_identities(selected_inputs) == expected_identities
        assert perimeter_module.build_account_assignment_perimeter(
            selected_inputs=selected_inputs,
            nav_facts=facts["nav_facts"],
            raw_flow_facts=facts["raw_flow_facts"],
            classified_flow_facts=facts["classified_flow_facts"],
        ) == expected_perimeter

    fresh_ledger = InputSnapshot.capture(ledger_path, "fresh ledger")
    ledger_manifest_path.write_bytes(
        _generation_manifest_snapshot(
            catalog=fresh_ledger,
            catalog_artifact_id="normalized/provider-ledger.json",
            catalog_schema_name="provider-ledger-catalog",
            catalog_schema_version="1",
            as_of_date="2026-03-01",
        ).contents
    )
    fresh_inputs = perimeter_module.AccountManifestSelectedInputs(
        **{
            "provider_ledger": fresh_ledger,
            "provider_ledger_manifest": InputSnapshot.capture(
                ledger_manifest_path, "fresh ledger manifest"
            ),
            "baseline": baseline,
            "baseline_manifest": baseline_manifest,
            "flow_classification_policy": policy,
            "retrieval_receipts": {"receipt": receipt},
            "epoch_producer_source": epoch,
            "generation_builder_source": builder,
            "independent_verifier_source": verifier,
        }
    )
    assert fresh_inputs.generation_as_of_date == date(2026, 3, 1)
    assert perimeter_module.build_selected_input_identities(fresh_inputs) != expected_identities
    assert json.loads(
        perimeter_module.build_account_assignment_perimeter(
            selected_inputs=fresh_inputs,
            nav_facts=facts["nav_facts"],
            raw_flow_facts=facts["raw_flow_facts"],
            classified_flow_facts=facts["classified_flow_facts"],
        )
    )["as_of_date"] == "2026-03-01"


def test_builds_schema_valid_complete_partition_and_counts_non_strategy_facts(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    payload_bytes = _build(perimeter_module, inputs)
    payload = json.loads(payload_bytes)

    assert payload_bytes == canonical_json_bytes(payload)
    assert payload["account_count"] == 18
    assert payload["assignment_count"] == 26
    assert payload["partition_counts"] == {
        "STRATEGY_MAPPED": 15,
        "HISTORICAL_POOL_ONLY": 7,
        "RESERVE_NOT_APPLICABLE": 4,
    }
    assert payload["fact_bijection_status"] == "ONE_TO_ONE_VERIFIED"
    assert payload["unassigned_fact_count"] == 0
    assert payload["unassigned_facts"] == []
    assert not list(_schema_validator().iter_errors(payload))

    historical = next(
        row
        for row in payload["rows"]
        if row["account_id"] == "synthetic-account-12"
        and row["effective_start"] == "2026-01-01"
    )
    reserve = next(
        row
        for row in payload["rows"]
        if row["account_id"] == "synthetic-account-18"
        and row["effective_start"] == "2026-01-11"
    )
    assert historical["calculation_eligible"] is False
    assert historical["unavailable_nav"] == historical["raw_flows"] == 1
    assert historical["classified_flows"] == 1
    assert reserve["calculation_eligible"] is False
    assert reserve["available_nav"] == reserve["raw_flows"] == 1
    assert reserve["classified_flows"] == 1


def test_is_byte_deterministic_for_shuffled_fact_inputs(perimeter_module: Any) -> None:
    inputs = _fixture_inputs()
    expected = _build(perimeter_module, inputs)
    for field in ("nav_facts", "raw_flow_facts", "classified_flow_facts"):
        inputs[field].reverse()
    assert _build(perimeter_module, inputs) == expected


def test_reordered_baseline_preserves_perimeter_but_changes_snapshot_identity(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    original = json.loads(_build(perimeter_module, inputs))
    baseline_payload = inputs["baseline"].json_object()
    baseline_payload["accounts"].reverse()
    baseline_payload["assignments"].reverse()
    inputs["baseline"] = InputSnapshot.from_bytes(
        "reordered synthetic baseline", canonical_json_bytes(baseline_payload)
    )
    reordered = json.loads(_build(perimeter_module, inputs))

    assert reordered["selected_baseline_identity"] != original[
        "selected_baseline_identity"
    ]
    for field in (
        "full_perimeter_identity",
        "account_count",
        "assignment_count",
        "partition_counts",
        "rows",
    ):
        assert reordered[field] == original[field]


def test_pins_assignment_perimeter_and_selected_baseline_digest_vectors(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    first_row = payload["rows"][0]
    assignment_preimage = {
        key: first_row[key]
        for key in (
            "account_id",
            "strategy_id",
            "strategy_mapping_disposition",
            "effective_start",
            "effective_end",
        )
    }
    perimeter_preimage = [
        {
            key: row[key]
            for key in (
                "account_id",
                "strategy_id",
                "strategy_mapping_disposition",
                "effective_start",
                "effective_end",
            )
        }
        for row in payload["rows"]
    ]
    assert _semantic_digest(assignment_preimage) == (
        "5a7c37d12124866f09841ce952e642d49f28f6e1d709bbc8b1a4b93c5d949360"
    )
    assert first_row["assignment_identity"]["sha256"] == (
        "5a7c37d12124866f09841ce952e642d49f28f6e1d709bbc8b1a4b93c5d949360"
    )
    assert _semantic_digest(perimeter_preimage) == (
        "4421ad809a972746e7044b5dac4e7d2e0916018bbdea7cc321ea747459468d54"
    )
    assert payload["full_perimeter_identity"]["sha256"] == (
        "4421ad809a972746e7044b5dac4e7d2e0916018bbdea7cc321ea747459468d54"
    )
    baseline = inputs["baseline"]
    assert isinstance(baseline, InputSnapshot)
    selected_inputs = _selected_inputs(perimeter_module, baseline)
    assert payload["selected_baseline_identity"] == {
        "generation_id": selected_inputs.baseline_generation_id,
        "artifact": {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": "2026-02-01",
            "schema": {"name": "portfolio-baseline-catalog", "version": "1"},
            "sha256": "35d757b23dfd079c4dab3f3ffd201cad0a8e9f494dd8619b9ccfbba4f4cd7049",
            "size_bytes": 5703,
        },
    }


def _mutate_first_row_to_non_strategy(payload: dict[str, object]) -> None:
    payload["rows"][0].update(
        {
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "strategy_id": None,
            "calculation_eligible": False,
        }
    )


def _mutate_non_strategy_row_to_mapped(payload: dict[str, object]) -> None:
    next(
        row
        for row in payload["rows"]
        if row["strategy_mapping_disposition"] == "HISTORICAL_POOL_ONLY"
    ).update(
        {
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "strategy_id": "synthetic-mutated-strategy",
            "calculation_eligible": True,
        }
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["rows"].pop(), "payload differs from expected"),
        (_mutate_first_row_to_non_strategy, "payload differs from expected"),
        (
            lambda value: value["rows"][0].update({"strategy_id": "other"}),
            "payload differs from expected",
        ),
        (_mutate_non_strategy_row_to_mapped, "payload differs from expected"),
        (
            lambda value: value["rows"][0].update({"provider": "OTHER"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"account_id": "other-account"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"effective_start": "2025-01-02"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0]["assignment_identity"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["full_perimeter_identity"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["full_perimeter_identity"].update({"count": 25}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"].update({"generation_id": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"artifact_id": "other.json"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"as_of_date": "2026-02-02"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"]["schema"].update({"name": "other-schema"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"]["schema"].update({"version": "2"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"size_bytes": 1}),
            "payload differs from expected",
        ),
        (lambda value: value.update({"account_count": 17}), "payload differs from expected"),
        (lambda value: value.update({"assignment_count": 25}), "payload differs from expected"),
        (
            lambda value: value["partition_counts"].update({"STRATEGY_MAPPED": 14}),
            "payload differs from expected",
        ),
        (
            lambda value: value["partition_counts"].update({"HISTORICAL_POOL_ONLY": 6}),
            "payload differs from expected",
        ),
        (
            lambda value: value["partition_counts"].update({"RESERVE_NOT_APPLICABLE": 3}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"available_nav": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"unavailable_nav": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"raw_flows": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"classified_flows": 9}),
            "payload differs from expected",
        ),
    ],
)
def test_semantic_validator_rejects_schema_valid_post_build_mutations(
    perimeter_module: Any, mutate: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    mutate(payload)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _validate(perimeter_module, payload, inputs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["rows"][0].update({"provider": "OTHER"}),
        lambda value: value["rows"][0].update({"strategy_id": "other"}),
        lambda value: value["rows"][0].update({"available_nav": 9}),
        lambda value: value["full_perimeter_identity"].update({"count": 25}),
        lambda value: value.update({"account_count": 17}),
    ],
)
def test_semantic_validator_rejects_schema_valid_mutations(
    perimeter_module: Any, mutate: Any
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    mutate(payload)
    assert not list(_schema_validator().iter_errors(payload))
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="payload differs from expected",
    ):
        _validate(perimeter_module, payload, inputs)


@pytest.mark.parametrize(
    ("section", "fact_mutation", "message"),
    [
        ("nav_facts", lambda facts: facts.append(deepcopy(facts[0])), "duplicate NAV record_id"),
        (
            "nav_facts",
            lambda facts: facts.append(
                {**deepcopy(facts[0]), "record_id": "other-nav-id"}
            ),
            "duplicate NAV account_id/as_of_date",
        ),
        ("raw_flow_facts", lambda facts: facts.append(deepcopy(facts[0])), "duplicate raw flow record_id"),
        (
            "classified_flow_facts",
            lambda facts: facts.append(deepcopy(facts[0])),
            "duplicate classified flow event_id",
        ),
        (
            "classified_flow_facts",
            lambda facts: facts.append(
                {**deepcopy(facts[0]), "event_id": "other-event-id"}
            ),
            "duplicate classified flow source_record_id",
        ),
    ],
)
def test_rejects_each_required_fact_identity_collision(
    perimeter_module: Any, section: str, fact_mutation: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    fact_mutation(inputs[section])
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _build(perimeter_module, inputs)


@pytest.mark.parametrize("field", ["nav_facts", "raw_flow_facts", "classified_flow_facts"])
@pytest.mark.parametrize("date", ["2024-12-31", "2027-01-01"])
def test_rejects_unknown_accounts_and_dates_outside_the_full_perimeter(
    perimeter_module: Any, field: str, date: str
) -> None:
    inputs = _fixture_inputs()
    fact = deepcopy(inputs[field][0])
    fact["account_id"] = "unknown-synthetic-account"
    if field == "classified_flow_facts":
        fact["event_id"] = "unknown-account-event"
        fact["source_record_id"] = "unknown-account-source"
    else:
        fact["record_id"] = "unknown-account-record"
    inputs[field].append(fact)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match="unknown fact account_id"):
        _build(perimeter_module, inputs)

    inputs = _fixture_inputs()
    if date == "2027-01-01":
        baseline_payload = inputs["baseline"].json_object()
        baseline_payload["assignments"][0]["effective_end"] = "2025-12-31"
        inputs["baseline"] = InputSnapshot.from_bytes(
            "closed synthetic account baseline", canonical_json_bytes(baseline_payload)
        )
    fact = deepcopy(inputs[field][0])
    fact["record_id" if field != "classified_flow_facts" else "event_id"] = "date-test"
    fact["as_of_date" if field == "nav_facts" else "event_date"] = date
    if field == "classified_flow_facts":
        fact["source_record_id"] = "date-test-source"
    inputs[field].append(fact)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match="outside every assignment interval"):
        _build(perimeter_module, inputs)


def _make_overlap(assignments: list[dict[str, object]]) -> None:
    assignments[0]["effective_end"] = "2025-12-31"
    assignments.append(
        _assignment(
            "synthetic-account-01",
            "STRATEGY_MAPPED",
            "2025-06-01",
            None,
            "synthetic-strategy-overlap",
        )
    )


def _make_gap(assignments: list[dict[str, object]]) -> None:
    next(
        assignment
        for assignment in assignments
        if assignment["account_id"] == "synthetic-account-12"
        and assignment["effective_start"] == "2026-01-01"
    )["effective_start"] = "2026-01-02"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            _make_overlap,
            "assignments overlap",
        ),
        (
            _make_gap,
            "assignments have a gap",
        ),
    ],
)
def test_rejects_baseline_assignment_overlap_and_gap(
    perimeter_module: Any, mutation: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    baseline_payload = inputs["baseline"].json_object()
    mutation(baseline_payload["assignments"])
    inputs["baseline"] = InputSnapshot.from_bytes(
        "invalid synthetic baseline", canonical_json_bytes(baseline_payload)
    )
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _build(perimeter_module, inputs)


def test_schema_is_general_and_rejects_private_or_invalid_shape(perimeter_module: Any) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    validator = _schema_validator()
    smaller = deepcopy(payload)
    smaller["rows"] = [deepcopy(payload["rows"][0])]
    smaller["account_count"] = 1
    smaller["assignment_count"] = 1
    smaller["partition_counts"] = {
        "STRATEGY_MAPPED": 1,
        "HISTORICAL_POOL_ONLY": 0,
        "RESERVE_NOT_APPLICABLE": 0,
    }
    smaller["full_perimeter_identity"] = {
        "sha256": smaller["rows"][0]["assignment_identity"]["sha256"],
        "count": 1,
    }
    assert not list(validator.iter_errors(smaller))

    private_shape = deepcopy(payload)
    private_shape["rows"][0]["account_number_last_four"] = "1234"
    assert list(validator.iter_errors(private_shape))

    noncanonical_version = deepcopy(payload)
    noncanonical_version["selected_baseline_identity"]["artifact"]["schema"][
        "version"
    ] = "01"
    assert list(validator.iter_errors(noncanonical_version))


@pytest.mark.parametrize("invalid_date", ["2026-02-30", "2026-13-99", "2025-02-29", "0000-01-01"])
def test_schema_intrinsically_rejects_invalid_calendar_dates_without_format_checker(
    perimeter_module: Any, invalid_date: str
) -> None:
    payload = json.loads(_build(perimeter_module, _fixture_inputs()))
    payload["as_of_date"] = invalid_date
    assert list(_schema_validator_without_format_checker().iter_errors(payload))


@pytest.mark.parametrize("valid_date", ["2024-02-29", "2026-02-28"])
def test_schema_intrinsically_accepts_canonical_calendar_dates_without_format_checker(
    perimeter_module: Any, valid_date: str
) -> None:
    payload = json.loads(_build(perimeter_module, _fixture_inputs()))
    payload["as_of_date"] = valid_date
    assert not list(_schema_validator_without_format_checker().iter_errors(payload))


def test_rejects_negative_baseline_schema_version_before_builder_or_validator_emission(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    valid_payload = json.loads(_build(perimeter_module, inputs))
    baseline_payload = inputs["baseline"].json_object()
    baseline_payload["baseline_schema_version"] = -1
    inputs["baseline"] = InputSnapshot.from_bytes(
        "negative-version synthetic baseline", canonical_json_bytes(baseline_payload)
    )

    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="baseline_schema_version must be a non-negative integer",
    ):
        _build(perimeter_module, inputs)
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="baseline_schema_version must be a non-negative integer",
    ):
        _validate(perimeter_module, valid_payload, inputs)

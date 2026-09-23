"""Frozen parent-owned C1 fixture support; source is the existing public-producer test.
Only existing fixture constructors are copied; production is imported from the examined checkout.
Source SHA256: 003905d4d581edbc5565fe36af71eaa05b23f17fd8562e95f7ecb17164910cfe
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
LEDGER_GENERATION_ID = "1" * 64
BASELINE_GENERATION_ID = "2" * 64

def _block_synthetic_flow_coverage(inputs: dict[str, Path]) -> None:
    """Make the public synthetic fixture honest without installing trust."""
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["receipt_artifacts"] = []
    for row in coverage["rows"]:
        row.update(
            {
                "status": "BLOCKED",
                "authority_kind": "NONE",
                "receipt_artifact_id": None,
                "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                "blocker_detail": "no retained retrieval receipt is available",
            }
        )
    _write_json(inputs["flow_coverage"], coverage)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    for admission in policy["admissions"]:
        admission["continuity_to_successor"] = False
    _write_json(inputs["policy"], policy)

def _bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

def _sha256(payload: object) -> str:
    return hashlib.sha256(_bytes(payload)).hexdigest()

def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_bytes(payload))

def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")

def _classified_flow_artifact(path: Path) -> dict[str, Any]:
    """Hand-build the source identity bound by the coverage authority."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    contents = path.read_bytes()
    return {
        "artifact_id": payload["artifact_id"],
        "as_of_date": payload["as_of_date"],
        "schema": {"name": "classified-flows", "version": "3"},
        "sha256": hashlib.sha256(contents).hexdigest(),
        "size_bytes": len(contents),
    }

def _flow_coverage_payload(
    inputs: dict[str, Path], rows: list[dict[str, Any]], **overrides: object
) -> dict[str, Any]:
    """Synthetic account-and-interval receipt fixture with literal identities."""
    ledger_identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    payload: dict[str, Any] = {
        "schema_version": 2,
        "artifact_id": "coverage/synthetic-account-flow-coverage.json",
        "as_of_date": "2026-01-06",
        "ledger_identity": {
            "generation_id": ledger_identity["generation_id"],
            "artifact": ledger_identity["artifact"],
        },
        "classified_flows_artifact": _classified_flow_artifact(inputs["flows"]),
        "mapped_assignments_identity": {"sha256": "0" * 64, "count": 0},
        "receipt_artifacts": [],
        "rows": rows,
    }
    payload.update(overrides)
    return payload

def _coverage_row(
    start: str,
    end: str,
    *,
    account_id: str = "synthetic-account",
    strategy_id: str = "synthetic-strategy-a",
    assignment_end: str | None = None,
    status: str = "COMPLETE",
    source_artifact_ids: list[str] | None = None,
    authority_kind: str = "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
    reason: str | None = None,
) -> dict[str, Any]:
    if status == "BLOCKED" and reason is None:
        reason = "NO_ADMISSIBLE_V2_RECEIPT"
    if status == "BLOCKED" and reason not in {
        "NO_ADMISSIBLE_V2_RECEIPT",
        "ACCOUNT_BINDING_MISMATCH",
        "INTERVAL_BOUNDARY_MISMATCH",
        "CATEGORY_SET_INCOMPLETE",
        "PAGINATION_NOT_TERMINAL",
        "SOURCE_IDENTITY_MISMATCH",
        "RECEIPT_OVERLAP",
        "RECEIPT_GAP",
        "LEGACY_RECEIPT_UNVERIFIABLE",
    }:
        reason = "NO_ADMISSIBLE_V2_RECEIPT"
    return {
        "assignment": {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "effective_start": start,
            "effective_end": assignment_end
            if assignment_end is not None
            else (end if strategy_id == "synthetic-strategy-a" else None),
        },
        "requested_start": start,
        "requested_end": end,
        "status": status,
        "authority_kind": authority_kind if status == "COMPLETE" else "NONE",
        "receipt_artifact_id": (
            f"receipt/synthetic-{start}-{end}.json"
            if status == "COMPLETE" and source_artifact_ids != []
            else None
        ),
        "blocker_code": None if status == "COMPLETE" else reason,
        "blocker_detail": None if status == "COMPLETE" else "synthetic blocked receipt",
    }

def _retrieval_receipt(
    start: str,
    end: str,
    artifact_id: str = "receipt/synthetic-2026-01-01-2026-01-06.json",
) -> dict[str, Any]:
    artifact = {
        "artifact_id": "raw/synthetic.json",
        "as_of_date": "2026-01-06",
        "schema": {"name": "synthetic", "version": "1"},
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    return {
        "schema_version": 2,
        "artifact_id": artifact_id,
        "issuer": {
            "adapter_name": "SYNTHETIC",
            "adapter_version": "1",
            "code_sha256": "b" * 64,
        },
        "provider": "SYNTHETIC",
        "provider_generation": "c" * 64,
        "provider_generation_manifest": json.loads(json.dumps(artifact)),
        "raw_evidence_artifacts": [json.loads(json.dumps(artifact))],
        "account_id": "synthetic-account",
        "source_account_identity_sha256": "d" * 64,
        "requested_start": start,
        "requested_end": end,
        "covered_start": start,
        "covered_end": end,
        "required_categories": ["CASH"],
        "completed_categories": ["CASH"],
        "unavailable_categories": [],
        "pagination": {
            "mode": "SYNTHETIC",
            "page_count": 1,
            "request_response_chain_sha256": "e" * 64,
            "terminal_response_sha256": "f" * 64,
            "terminal": True,
        },
        "completeness": "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES",
        "truncated": False,
    }

def _flow_classification_policy(as_of_date: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "policy/synthetic-flow-classification.json",
        "as_of_date": as_of_date,
        "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
        "policy_version": "1",
        "event_dispositions": {
            "exact": [
                {
                    "flow_type": value,
                    "disposition": "EXTERNAL",
                    "amount_sign": amount_sign,
                }
                for value, amount_sign in (
                    ("EXTERNAL_CONTRIBUTION", "POSITIVE"),
                    ("EXTERNAL_WITHDRAWAL", "NEGATIVE"),
                    ("EXTERNAL_PROVIDER_TRANSFER", "NONZERO"),
                    ("DEPOSIT_OR_WITHDRAWAL", "NONZERO"),
                )
            ]
            + [
                {
                    "flow_type": "ACCOUNT_TRANSFER",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
            "prefix": [
                {
                    "flow_type_prefix": "TRANSFER:INTERNAL:",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
        },
    }

def _baseline_assignments(*, gap: bool = False) -> list[dict[str, Any]]:
    second_start = "2026-01-05" if gap else "2026-01-04"
    return [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-a",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": "2026-01-01",
            "effective_end": "2026-01-03",
        },
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-b",
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": second_start,
            "effective_end": None,
        },
    ]

def _admission_policy(
    assignments: list[dict[str, Any]], *, continuity: bool
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy_id": "SYNTHETIC-ACCOUNT-STRATEGY-ADMISSION",
        "policy_version": "1",
        "artifact_id": "policy/synthetic-account-strategy-admission.json",
        "as_of_date": "2026-01-06",
        "assignment_sha256": _sha256(
            [
                {
                    "account_id": assignment["account_id"],
                    "strategy_id": assignment["strategy_id"],
                    "effective_start": assignment["effective_start"],
                    "effective_end": assignment["effective_end"],
                }
                for assignment in assignments
            ]
        ),
        "admissions": [
            {
                "account_id": assignment["account_id"],
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
                "status": "FUNDED",
                "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": [
                    f"nav-{int((assignment['effective_end'] or assignment['effective_start'])[-2:])}"
                ],
                "continuity_to_successor": continuity and index == 0,
            }
            for index, assignment in enumerate(assignments)
        ],
    }

def _write_valid_inputs(tmp_path: Path, *, gap: bool = False) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger_path = tmp_path / "provider-ledger.json"
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "record_id": "nav-1",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "record_id": "nav-2",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-02",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "60"},
            },
            {
                "record_id": "nav-3",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-03",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "record_id": "nav-4",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-04",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "record_id": "nav-5",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-05",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "75"},
            },
            {
                "record_id": "nav-6",
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-06",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "80"},
            },
        ],
        "flows": [
            {
                "record_id": "raw-flow-1",
                "account_id": "synthetic-account",
                "event_date": "2026-01-02",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            }
        ],
    }
    _write_json(ledger_path, ledger)
    ledger_bytes = ledger_path.read_bytes()
    ledger_identity = {
        "schema_version": 1,
        "generation_id": LEDGER_GENERATION_ID,
        "artifact": {
            "artifact_id": "normalized/provider-ledger.json",
            "as_of_date": "2026-01-06",
            "schema": {"name": "provider-ledger", "version": "1"},
            "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
            "size_bytes": len(ledger_bytes),
        },
    }
    ledger_identity_path = tmp_path / "ledger-identity.json"
    _write_json(ledger_identity_path, ledger_identity)
    ledger_source_identity = {
        "generation_id": ledger_identity["generation_id"],
        "artifact": ledger_identity["artifact"],
    }

    baseline_root = tmp_path / "baseline"
    generation_id = BASELINE_GENERATION_ID
    generation_dir = baseline_root / "generations" / generation_id
    generation_dir.mkdir(parents=True)
    _write_json(baseline_root / "CURRENT.json", {"generation_id": generation_id})
    assignments = _baseline_assignments(gap=gap)
    _write_json(
        generation_dir / "portfolio-baseline.json",
        {
            "as_of_date": "2026-01-06",
            "baseline_schema_version": 1,
            "accounts": [{"account_id": "synthetic-account", "provider": "SYNTHETIC"}],
            "assignments": assignments,
        },
    )

    policy_path = tmp_path / "admission-policy.json"
    # Production intentionally retains no receipt authority.  Keep the shared
    # producer fixture buildable by describing two independently blocked epochs,
    # rather than asserting an unprovable capital-continuity handoff.
    _write_json(policy_path, _admission_policy(assignments, continuity=False))

    classification_policy_path = tmp_path / "flow-classification-policy.json"
    _write_json(classification_policy_path, _flow_classification_policy("2026-01-06"))
    classification_policy_sha256 = hashlib.sha256(
        classification_policy_path.read_bytes()
    ).hexdigest()
    flows_path = tmp_path / "classified-flows.json"
    _write_json(
        flows_path,
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": "2026-01-06",
            "ledger_identity": ledger_source_identity,
            "classification_policy": {
                "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-06",
                "policy_sha256": classification_policy_sha256,
            },
            "events": [
                {
                    "event_id": "classified-flow-1",
                    "source_record_id": "raw-flow-1",
                    "account_id": "synthetic-account",
                    "event_date": "2026-01-02",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                }
            ],
        },
    )
    receipt_path = tmp_path / "synthetic-retrieval-receipt-a.json"
    receipt_b_path = tmp_path / "synthetic-retrieval-receipt-b.json"
    receipt_a_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt_b_id = "receipt/synthetic-2026-01-04-2026-01-06.json"
    _write_json(
        receipt_path, _retrieval_receipt("2026-01-01", "2026-01-03", receipt_a_id)
    )
    _write_json(
        receipt_b_path, _retrieval_receipt("2026-01-04", "2026-01-06", receipt_b_id)
    )
    mapped_projection = [
        {
            key: assignment[key]
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        }
        for assignment in assignments
    ]
    coverage_path = tmp_path / "account-flow-coverage.json"
    _write_json(
        coverage_path,
        _flow_coverage_payload(
            {
                "ledger_identity": ledger_identity_path,
                "flows": flows_path,
            },
            [
                _coverage_row(
                    "2026-01-01",
                    "2026-01-03",
                    assignment_end="2026-01-03",
                    status="BLOCKED",
                    source_artifact_ids=[],
                    authority_kind="NONE",
                    reason="NO_ADMISSIBLE_V2_RECEIPT",
                ),
                _coverage_row(
                    "2026-01-04",
                    "2026-01-06",
                    strategy_id="synthetic-strategy-b",
                    assignment_end=None,
                    status="BLOCKED",
                    source_artifact_ids=[],
                    authority_kind="NONE",
                    reason="NO_ADMISSIBLE_V2_RECEIPT",
                ),
            ],
            mapped_assignments_identity={
                "sha256": _sha256(mapped_projection),
                "count": len(mapped_projection),
            },
            receipt_artifacts=[],
        ),
    )
    return {
        "ledger": ledger_path,
        "ledger_identity": ledger_identity_path,
        "baseline_root": baseline_root,
        "policy": policy_path,
        "classification_policy": classification_policy_path,
        "flows": flows_path,
        "flow_coverage": coverage_path,
        "retrieval_receipt": receipt_path,
        "retrieval_receipt_b": receipt_b_path,
    }

def _write_selected_first_receipt_authority(inputs: dict[str, Path]) -> Path:
    """Select exactly one synthetic receipt by its canonical immutable bytes."""
    receipt_id = "receipt/synthetic-2026-01-01-2026-01-03.json"
    receipt_bytes = inputs["retrieval_receipt"].read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["rows"][0].update(
        {
            "status": "COMPLETE",
            "authority_kind": "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
            "receipt_artifact_id": receipt_id,
            "blocker_code": None,
            "blocker_detail": None,
        }
    )
    coverage["receipt_artifacts"] = [
        {
            "artifact_id": receipt_id,
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "size_bytes": len(receipt_bytes),
        }
    ]
    _write_json(inputs["flow_coverage"], coverage)
    raw_artifact = {
        "artifact_id": "raw/synthetic.json",
        "as_of_date": "2026-01-06",
        "schema": {"name": "synthetic", "version": "1"},
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    authority = {
        "schema_version": 1,
        "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
        "as_of_date": "2026-01-06",
        "receipts": [
            {
                "artifact_id": receipt_id,
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "size_bytes": len(receipt_bytes),
                "contract": {
                    "adapter_name": "SYNTHETIC",
                    "adapter_version": "1",
                    "code_sha256": "b" * 64,
                    "provider": "SYNTHETIC",
                    "account_id": "synthetic-account",
                    "source_account_identity_sha256": "d" * 64,
                    "provider_generation": "c" * 64,
                    "provider_generation_manifest": raw_artifact,
                    "raw_evidence_artifacts": [raw_artifact],
                    "required_categories": ["CASH"],
                    "pagination": {
                        "mode": "SYNTHETIC",
                        "page_count": 1,
                        "request_response_chain_sha256": "e" * 64,
                        "terminal_response_sha256": "f" * 64,
                        "terminal": True,
                    },
                },
            }
        ],
    }
    authority_path = inputs["flow_coverage"].with_name(
        "account-interval-retrieval-receipt-authority.json"
    )
    authority_path.write_bytes(_canonical_json_bytes(authority))
    assert receipt == _retrieval_receipt("2026-01-01", "2026-01-03", receipt_id)
    return authority_path

def _refresh_ledger_identity(inputs: dict[str, Path]) -> None:
    """Keep synthetic cross-artifact source identities internally exact."""
    ledger_bytes = inputs["ledger"].read_bytes()
    identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    identity["artifact"]["sha256"] = hashlib.sha256(ledger_bytes).hexdigest()
    identity["artifact"]["size_bytes"] = len(ledger_bytes)
    _write_json(inputs["ledger_identity"], identity)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["ledger_identity"] = {
        "generation_id": identity["generation_id"],
        "artifact": identity["artifact"],
    }
    _write_json(inputs["flows"], classified)
    coverage = json.loads(inputs["flow_coverage"].read_text(encoding="utf-8"))
    coverage["ledger_identity"] = classified["ledger_identity"]
    coverage["classified_flows_artifact"] = _classified_flow_artifact(inputs["flows"])
    _write_json(inputs["flow_coverage"], coverage)

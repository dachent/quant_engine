from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    read_generation_bundle,
    stage_generation_bundle,
)
from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "portfolio_report_epoch_binding", ROOT / "tools" / "portfolio_report.py"
)
assert SPEC is not None and SPEC.loader is not None
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _fixture(
    candidate: Path,
) -> tuple[Path, Path, bytes, dict[str, object], Path]:
    crosswalk = json.loads(
        (
            ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_text(encoding="utf-8")
    )
    crosswalk_bytes = canonical_json_bytes(crosswalk)
    manifest = {
        "accounting_currency": "USD",
        "artifact": "account-strategy-manifest",
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "rows": [
            {
                key: row[key]
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
                if key in row
            }
            for row in crosswalk["rows"]
        ],
        "schema_version": 8,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    source = {
        "generation_id": "1" * 64,
        "generation_manifest_sha256": "2" * 64,
        "generation_receipt_sha256": "3" * 64,
        "full_directory_inventory_sha256": "4" * 64,
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
            "schema": {"name": "account-strategy-manifest", "version": "8"},
        },
        "crosswalk": {
            "path": "authority/account-assignment-crosswalk.json",
            "sha256": hashlib.sha256(crosswalk_bytes).hexdigest(),
            "size_bytes": len(crosswalk_bytes),
            "schema": {
                "name": "account-strategy-current-epoch-crosswalk",
                "version": "1",
            },
        },
    }
    manifest_path = candidate / "account-strategy-manifest.json"
    source_path = candidate / "epoch-manifest-source.json"
    candidate.mkdir()
    manifest_path.write_bytes(manifest_bytes)
    source_path.write_bytes(canonical_json_bytes(source))
    crosswalk_path = candidate / "account-strategy-current-epoch-crosswalk.json"
    crosswalk_path.write_bytes(crosswalk_bytes)
    return manifest_path, source_path, manifest_bytes, source, crosswalk_path


def test_epoch_manifest_workbook_snapshot_binds_exact_bytes(tmp_path: Path) -> None:
    manifest_path, source_path, manifest_bytes, source, crosswalk_path = _fixture(
        tmp_path / "candidate"
    )

    snapshots = reporter._epoch_manifest_workbook_snapshots(
        manifest_path.parent, manifest_path, source_path, crosswalk_path
    )

    assert snapshots["epoch_manifest"] == (manifest_path, manifest_bytes)
    assert json.loads(snapshots["epoch_manifest_source"][1]) == source
    assert snapshots["epoch_crosswalk"][1] == canonical_json_bytes(
        json.loads(
            (
                ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
            ).read_text(encoding="utf-8")
        )
    )


@pytest.mark.parametrize("mutation", ("payload", "source_hash", "half_pair"))
def test_epoch_manifest_workbook_snapshot_rejects_incomplete_or_changed_binding(
    tmp_path: Path, mutation: str
) -> None:
    manifest_path, source_path, _manifest_bytes, source, crosswalk_path = _fixture(
        tmp_path / "candidate"
    )
    supplied_source: Path | None = source_path
    if mutation == "payload":
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    elif mutation == "source_hash":
        source["artifact"]["sha256"] = "0" * 64
        source_path.write_bytes(canonical_json_bytes(source))
    else:
        supplied_source = None

    with pytest.raises(reporter.ReportValidationError):
        reporter._epoch_manifest_workbook_snapshots(
            manifest_path.parent, manifest_path, supplied_source, crosswalk_path
        )


def _membership_census(
    *assignments: tuple[str, str, str | None, str | None, str],
) -> dict[str, object]:
    return {
        "current_census": {
            "account_sleeves": [
                {
                    "provider_id": provider,
                    "assignments": [
                        {
                            "strategy_id": strategy_id,
                            "status": status,
                            "effective_start": start,
                            "effective_end": end,
                        }
                    ],
                }
                for provider, strategy_id, start, end, status in assignments
            ]
        }
    }


def _verified_schema9_epoch_bundle(
    tmp_path: Path,
    *,
    manifest_schema_version: int = 9,
    manifest_cutoff: str = "2026-08-13",
    calculation_cutoff: str = "2026-08-13",
    verification_mutation: str | None = None,
    manifest_mutation: str | None = None,
    authority_graph_mutation: str | None = None,
    static_receipt: bool = False,
    static_receipt_mutation: str | None = None,
    static_closed_epoch: bool = False,
    static_blocked_facts: bool = False,
    all_numeric_candidates: bool = False,
    canonical_selected_policy: bool = False,
    r3_account_id: str | None = None,
    derived_sleeves: bool = False,
) -> tuple[Path, bytes, bytes, bytes, bytes]:
    """Build one verified schema-9 epoch generation around synthetic rows.

    ``static_closed_epoch`` binds the static receipt to the closed tt-portfolio
    epoch (BLOCKED by the fixture's admission).  ``static_blocked_facts``
    reproduces the real BLOCKED census-assignment epoch: the receipt keeps the
    ledger's own observed facts (41 NAV rows, 3 flow rows) while the hash-bound
    calculation row of a BLOCKED epoch is stripped to its one closing NAV, no
    flows and no anchors (``FUNDED_EPOCH_FACT_PROJECTION_V2``).
    ``r3_account_id`` (with ``all_numeric_candidates``) turns that account's
    open tt-portfolio epoch into the R3 zero-capital shape: FUNDED and OK with
    its performance UNAVAILABLE for ZERO_CAPITAL_RETURN_UNDEFINED, an empty
    observation trace, and NAV and flow facts in its calculation row (NAV 50 on
    2026-08-11, 42 on 2026-08-13 after a 10 end-of-day withdrawal).
    ``derived_sleeves`` replaces the p123-ai-factor epoch (a residual parent) by
    two derived sleeve epochs, as the manifest carries them after #567: a current
    one of the same strategy and a retained history-only one of another.
    """

    epoch_root = tmp_path
    for index in range(1):
        epoch_root /= f"independent-epoch-component-{index:02d}-abcdefghijk"
    # Exercise Windows long-path reads even when the caller uses a short TEMP.
    if sys.platform == "win32":
        while (
            len(
                str(
                    (
                        epoch_root
                        / "generations"
                        / ("0" * 64)
                        / "verification"
                        / "independent-verification.json"
                    ).resolve()
                )
            )
            <= 260
        ):
            epoch_root /= "independent-epoch-long-path-component"
    crosswalk_payload = json.loads(
        (
            ROOT / "contracts" / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_text(encoding="utf-8")
    )
    crosswalk_bytes = canonical_json_bytes(crosswalk_payload)
    included = [
        row
        for row in crosswalk_payload["rows"]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    ]
    derived_epochs_by_key: dict[tuple[object, ...], str] = {}
    if derived_sleeves:
        parent_row = next(
            row for row in included if row["strategy_id"] == "p123-ai-factor"
        )
        sleeves = [
            {
                **parent_row,
                "account_id": f"{parent_row['account_id']}-s2",
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-30",
                "effective_end": None,
            },
            {
                **parent_row,
                "account_id": f"{parent_row['account_id']}-s1",
                "strategy_id": "p123-conn-one",
                "effective_start": "2026-01-26",
                "effective_end": "2026-06-23",
            },
        ]
        for sleeve_row in sleeves:
            derived_epochs_by_key[
                tuple(
                    sleeve_row[field]
                    for field in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                )
            ] = str(parent_row["account_id"])
        included = [row for row in included if row is not parent_row] + sleeves
    derived_count = len(derived_epochs_by_key)
    rows: list[dict[str, object]] = []
    blocker_chains: list[dict[str, object]] = []
    census_assignments: list[tuple[str, str, str | None, str | None, str]] = []
    for row in included:
        assignment = {
            field: row[field]
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        }
        funded = all_numeric_candidates or row["strategy_id"] == "volatis-strategy"
        output_status = "OK" if funded else "BLOCKED_ADMISSION"
        blocking_reason = None if funded else "FLOW_COMPLETENESS_NOT_PROVEN"
        manifest_row = {
            **assignment,
            "account_label": row["account_label"],
            "last_four": row["last_four"],
            "provider": row["provider"],
            "admission_status": "FUNDED" if funded else "BLOCKED",
            "admission_policy_reference": "MAPPED_EPOCH_ADMISSION_V2/2",
            "admission_authority_kind": (
                "LEDGER_AVAILABLE_POSITIVE_NAV_V1" if funded else "NONE"
            ),
            "funded_nav_record_ids": ["volatis-nav-opening"] if funded else [],
            "continuity_to_successor": False,
            "status": output_status,
        }
        if blocking_reason is not None:
            manifest_row["blocking_reason"] = blocking_reason
        else:
            manifest_row.update(
                {
                    "start": "2026-08-11",
                    "end": "2026-08-13",
                    "opening_capital": "100",
                    "deposits": "0",
                    "internal_in": "0",
                    "invested": "100",
                    "external_withdrawals": "0",
                    "internal_out": "0",
                    "withdrawn": "0",
                    "net_capital": "100",
                    "current_value": "101",
                    "gain": "1",
                    "return_on_net_capital": "0.01",
                    "annualized_return_on_net_capital": "2.503427",
                    "xirr_diagnostic": None,
                    "observation_linked_return": "0.01",
                    "observed_hwm_index": "1.01",
                    "observed_max_drawdown": "0",
                    "opening_basis": "PROVIDER_CAPITAL_ANCHOR",
                    "observation_trace": {
                        "links": [
                            {
                                "start_date": "2026-08-11",
                                "end_date": "2026-08-13",
                                "base_nav": "100",
                                "beginning_flow": "0",
                                "ending_nav": "101",
                                "ending_flow": "0",
                                "net_flow": "0",
                                "factor": "1.01",
                                "cumulative_index": "1.01",
                                "running_hwm_index": "1.01",
                                "drawdown": "0",
                            }
                        ]
                    },
                    "capital_flow_trace": [],
                    "nav_observations": 3,
                    "opening_capital_date": "2026-08-11",
                    "performance_status": "AVAILABLE",
                    "performance_reason": None,
                    "performance_opening_date": "2026-08-11",
                    "performance_opening_value": "100",
                    "performance_opening_basis": (
                        "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
                    ),
                    "performance_flags": [],
                    "performance_flow_legs": [],
                    "flow_count": 0,
                }
            )
            if (row["account_id"], row["strategy_id"], row["effective_end"]) == (
                r3_account_id,
                "tt-portfolio",
                None,
            ):
                manifest_row.update(
                    {
                        "return_on_net_capital": None,
                        "annualized_return_on_net_capital": None,
                        "observation_linked_return": None,
                        "observed_hwm_index": None,
                        "observed_max_drawdown": None,
                        "observation_trace": {"links": []},
                        "performance_status": "UNAVAILABLE",
                        "performance_reason": "ZERO_CAPITAL_RETURN_UNDEFINED",
                        "flow_count": 1,
                    }
                )
        rows.append(manifest_row)
        census_assignments.append(
            (
                row["provider"],
                row["strategy_id"],
                row["effective_start"],
                row["effective_end"],
                "FUNDED_MEMBERSHIP" if funded else "ASSIGNMENT_ONLY",
            )
        )
        blocker_chains.append(
            {
                "assignment": assignment,
                "coverage": {
                    "status": "COMPLETE" if funded else "BLOCKED",
                    "blocker_code": None if funded else "NO_ADMISSIBLE_V2_RECEIPT",
                },
                "admission": {
                    "status": "FUNDED" if funded else "BLOCKED",
                    "blocker_code": blocking_reason,
                },
                "output": {
                    "status": output_status,
                    "blocking_reason": blocking_reason,
                },
                "numeric_candidate": funded,
            }
        )

    def source_artifact(
        artifact_id: str, schema_name: str, schema_version: str, marker: str
    ) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "as_of_date": "2026-08-13",
            "schema": {"name": schema_name, "version": schema_version},
            "sha256": marker * 64,
            "size_bytes": int(marker, 16) + 1,
        }

    ledger_identity = {
        "generation_id": "1" * 64,
        "artifact": source_artifact(
            "normalized/provider-ledger.json", "provider-ledger-catalog", "1", "2"
        ),
        "identity_artifact": source_artifact(
            "identity/provider-ledger-generation.json", "generation-manifest", "1", "3"
        ),
    }
    assignment_identity = {"sha256": "4" * 64, "count": 15}
    census_bytes = canonical_json_bytes(_membership_census(*census_assignments))
    manifest_payload: dict[str, object] = {
        "schema_version": manifest_schema_version,
        "calculation_cutoff_date": manifest_cutoff,
        "artifact": "account-strategy-manifest",
        "accounting_currency": "USD",
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "source_identities": {
            "ledger": ledger_identity,
            "baseline": {
                "generation_id": "5" * 64,
                "artifact": source_artifact(
                    "normalized/portfolio-baseline.json",
                    "portfolio-baseline-catalog",
                    "1",
                    "6",
                ),
            },
            "classified_flows": {
                "artifact": source_artifact(
                    "authority/classified-flows.json", "classified-flows", "5", "7"
                ),
                "schema_version": 5,
                "ledger_identity": ledger_identity,
                "classification_policy": {
                    "policy_id": "FLOW_CLASSIFICATION_V1",
                    "policy_version": "1",
                    "policy_as_of_date": "2026-08-13",
                    "policy_sha256": "8" * 64,
                },
            },
            "flow_classification_policy": {
                "policy_id": "FLOW_CLASSIFICATION_V1",
                "policy_version": "1",
                "event_dispositions": {
                    "exact": [{"description": "independent fixture"}],
                    "prefix": [],
                },
                "artifact": source_artifact(
                    "authority/flow-classification-policy.json",
                    "flow-classification-policy",
                    "1",
                    "9",
                ),
            },
            "flow_coverage": {
                "artifact": source_artifact(
                    "authority/account-flow-coverage.json",
                    "account-flow-coverage",
                    "2",
                    "a",
                ),
                "ledger_identity": ledger_identity,
                "classified_flows_artifact": source_artifact(
                    "authority/classified-flows.json", "classified-flows", "5", "7"
                ),
                "receipt_authority": source_artifact(
                    "authority/account-interval-retrieval-receipt-authority.json",
                    "account-interval-retrieval-receipt-authority",
                    "1",
                    "b",
                ),
            },
            "admission_policy": {
                "policy_id": "MAPPED_EPOCH_ADMISSION_V2",
                "policy_version": "2",
                "artifact": source_artifact(
                    "authority/mapped-epoch-admission.json",
                    "account-strategy-manifest-admission-policy",
                    "2",
                    "c",
                ),
                "assignment": assignment_identity,
            },
            "assignment": assignment_identity,
            "calculation_code": {
                "artifact_id": "tools/account_strategy_manifest.py",
                "sha256": "d" * 64,
                "size_bytes": 100,
            },
            "selected_inputs": source_artifact(
                "authority/selected-input-identities.json",
                "selected-input-identities",
                "1",
                "e",
            ),
            "account_assignment_perimeter": source_artifact(
                "authority/account-assignment-perimeter.json",
                "account-assignment-perimeter",
                "1",
                "f",
            ),
            "calculation_inputs": source_artifact(
                "authority/calculation-inputs.json",
                "account-manifest-calculation-inputs",
                "4",
                "1",
            ),
            "code_identities": source_artifact(
                "authority/code-identities.json",
                "account-manifest-code-identities",
                "1",
                "2",
            ),
        },
        "epoch_continuity": {
            "rule_id": "EXPLICIT_FUNDED_EPOCH_CONTINUITY_V1",
            "status": "POLICY_BOUND_ONLY",
            "rule": "Blocked candidates do not establish funded continuity.",
        },
        "flow_reconciliation": {
            "status": "ONE_TO_ONE_VERIFIED",
            "method": "EXACT_RAW_RECORD_TO_CLASSIFIED_EVENT_BINDING",
        },
        "rows": rows,
    }
    if manifest_schema_version == 9:
        manifest_payload["calculation_cutoff_date"] = manifest_cutoff
    if manifest_mutation == "stripped":
        manifest_payload = {
            "rows": rows,
            "schema_version": manifest_schema_version,
            "calculation_cutoff_date": manifest_cutoff,
        }
    closed_tt_epoch = next(
        row
        for row in included
        if row["strategy_id"] == "tt-portfolio" and row["effective_end"] == "2026-01-04"
    )
    stripped_blocked_projection = (
        static_receipt and static_closed_epoch and static_blocked_facts
    )
    r3_epoch = next(
        (
            row
            for row in included
            if (row["account_id"], row["strategy_id"], row["effective_end"])
            == (r3_account_id, "tt-portfolio", None)
        ),
        None,
    )

    def usd_fact(value: str) -> dict[str, str]:
        return {"currency": "USD", "status": "AVAILABLE", "value": value}

    r3_navs = (
        []
        if r3_epoch is None
        else [
            {
                "account_id": r3_epoch["account_id"],
                "as_of_date": day,
                "nav": usd_fact(value),
                "record_id": f"r3-nav-{day}",
                "value_basis": "ACCOUNT_VALUE",
            }
            for day, value in (("2026-08-11", "50"), ("2026-08-13", "42"))
        ]
    )
    r3_flows = (
        []
        if r3_epoch is None
        else [
            {
                "account_id": r3_epoch["account_id"],
                "amount": usd_fact("-10"),
                "blocker_code": None,
                "classification_status": "CLASSIFIED",
                "disposition": "EXTERNAL",
                "event_date": "2026-08-13",
                "event_id": "classified-flow:r3-withdrawal",
                "flow_type": "WITHDRAWAL",
                "nav_recognition_date": "2026-08-13",
                "source_record_id": "r3-withdrawal",
            }
        ]
    )
    calculation_rows = [
        {
            "epoch": {
                field: row[field]
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            },
            "provider": row["provider"],
            "nav_observations": (
                [
                    {
                        "account_id": row["account_id"],
                        "as_of_date": "2026-08-11",
                        "record_id": "volatis-nav-opening",
                        "value": "100",
                    },
                    {
                        "account_id": row["account_id"],
                        "as_of_date": "2026-08-12",
                        "record_id": "volatis-nav-middle",
                        "value": "100.5",
                    },
                    {
                        "account_id": row["account_id"],
                        "as_of_date": "2026-08-13",
                        "record_id": "volatis-nav-closing",
                        "value": "101",
                    },
                ]
                if row["strategy_id"] == "volatis-strategy"
                else (
                    r3_navs
                    if row is r3_epoch
                    else (
                        [
                            {
                                "account_id": row["account_id"],
                                "as_of_date": "2026-01-04",
                                "record_id": "tt-nav-closing",
                                "value": "100",
                            }
                        ]
                        if stripped_blocked_projection and row is closed_tt_epoch
                        else []
                    )
                )
            ),
            "classified_flows": (
                [
                    {
                        "account_id": row["account_id"],
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "-10",
                        },
                        "blocker_code": None,
                        "classification_status": "CLASSIFIED",
                        "disposition": "EXTERNAL",
                        "event_date": "2026-01-02",
                        "event_id": "classified-flow:synthetic-1",
                        "flow_type": "WITHDRAWAL",
                        "nav_recognition_date": "2026-01-02",
                        "source_record_id": "synthetic-1",
                    }
                ]
                if static_receipt_mutation == "blocked_calc_flow"
                and row is closed_tt_epoch
                else (r3_flows if row is r3_epoch else [])
            ),
            "capital_anchors": (
                [
                    {
                        "account_id": row["account_id"],
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "10",
                        },
                        "capital_type": "STRATEGY_CAPITAL_ALLOCATION",
                        "event_date": "2026-01-02",
                        "nav_recognition_date": "2026-01-02",
                        "strategy_id": row["strategy_id"],
                    }
                ]
                if static_receipt_mutation == "blocked_calc_anchor"
                and row is closed_tt_epoch
                else []
            ),
        }
        for row in included
    ]
    if verification_mutation == "calculation_row_count":
        calculation_rows.pop()
    if verification_mutation == "calculation_as_of_date":
        funded_row = next(
            row
            for row in calculation_rows
            if row["epoch"]["strategy_id"] == "volatis-strategy"
        )
        funded_row["nav_observations"][-1]["as_of_date"] = "2026-08-14"
    calculation_payload = {
        "schema_version": 4,
        "artifact_id": "authority/calculation-inputs.json",
        "as_of_date": "2026-08-16",
        "accounting_currency": "USD",
        "calculation_ledger": {
            "calculation_cutoff_date": calculation_cutoff,
            "nav_observations": [
                item for row in calculation_rows for item in row["nav_observations"]
            ],
            "flows": [],
            "capital_anchors": [],
        },
        "rows": calculation_rows,
    }
    calculation_inputs = canonical_json_bytes(calculation_payload)
    numeric_candidate_count = sum(
        chain["numeric_candidate"] is True for chain in blocker_chains
    )
    blocked_candidate_count = len(blocker_chains) - numeric_candidate_count
    mapped_count = 15 - (1 if derived_sleeves else 0) + derived_count
    counts = {
        "selected_baseline_account_count": 15,
        "account_count": 15,
        "assignment_count": mapped_count,
        "mapped_assignment_count": mapped_count,
        "historical_assignment_count": 0,
        "reserve_assignment_count": 0,
        "current_product_epoch_count": len(included),
        "observed_nav_fact_count": 3,
        "observed_raw_flow_count": 0,
        "classified_event_count": 0,
        "classified_event_classified_count": 0,
        "classified_event_blocked_count": 0,
        "unmatched_nav_fact_count": 0,
        "unmatched_raw_flow_fact_count": 0,
        "unmatched_fact_count": 0,
        "unmatched_projection_count": 0,
        "calculation_input_row_count": len(included),
        "coverage_row_count": len(included),
        "admission_row_count": len(included),
        "output_row_count": len(included),
        "complete_coverage_row_count": numeric_candidate_count,
        "funded_admission_row_count": numeric_candidate_count,
        "ok_output_row_count": numeric_candidate_count,
        "blocked_coverage_row_count": blocked_candidate_count,
        "blocked_admission_row_count": blocked_candidate_count,
        "blocked_output_row_count": blocked_candidate_count,
    }
    coverage_rows = [
        {
            "assignment": chain["assignment"],
            "status": chain["coverage"]["status"],
            "blocker_code": chain["coverage"]["blocker_code"],
            **(
                {
                    "authority_kind": "DERIVED_SLEEVE_COVERAGE_V1",
                    "derivation": {
                        "parent_account_id": derived_epochs_by_key[
                            tuple(
                                chain["assignment"][field]
                                for field in chain["assignment"]
                            )
                        ],
                        "sleeve_facts_sha256": "7" * 64,
                    },
                }
                if tuple(chain["assignment"][field] for field in chain["assignment"])
                in derived_epochs_by_key
                else {}
            ),
        }
        for chain in blocker_chains
    ]
    if verification_mutation == "coverage_chain":
        funded_index = next(
            index
            for index, row in enumerate(coverage_rows)
            if row["status"] == "COMPLETE"
        )
        blocked_index = next(
            index
            for index, row in enumerate(coverage_rows)
            if row["status"] == "BLOCKED"
        )
        (
            coverage_rows[funded_index]["status"],
            coverage_rows[blocked_index]["status"],
        ) = (
            coverage_rows[blocked_index]["status"],
            coverage_rows[funded_index]["status"],
        )
        (
            coverage_rows[funded_index]["blocker_code"],
            coverage_rows[blocked_index]["blocker_code"],
        ) = (
            coverage_rows[blocked_index]["blocker_code"],
            coverage_rows[funded_index]["blocker_code"],
        )
    admission_rows = [
        {
            **chain["assignment"],
            "status": chain["admission"]["status"],
            "reason": chain["admission"]["blocker_code"],
        }
        for chain in blocker_chains
    ]
    receipt_epoch = (
        closed_tt_epoch
        if static_closed_epoch
        else next(row for row in included if row["strategy_id"] == "volatis-strategy")
    )
    receipt_epoch_key = tuple(
        receipt_epoch[field]
        for field in ("account_id", "strategy_id", "effective_start", "effective_end")
    )

    def is_receipt_epoch(admission_row: dict[str, object]) -> bool:
        return (
            tuple(
                admission_row[field]
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            )
            == receipt_epoch_key
        )

    if static_receipt_mutation == "missing_admission":
        admission_rows = [row for row in admission_rows if not is_receipt_epoch(row)]
    elif static_receipt_mutation == "unknown_admission_status":
        for admission_row in admission_rows:
            if is_receipt_epoch(admission_row):
                admission_row["status"] = "PENDING_REVIEW"
    flow_policy_bytes = (
        ROOT / "contracts" / "account-strategy-flow-classification-policy-v1.json"
    ).read_bytes()
    flow_policy_payload = json.loads(flow_policy_bytes)
    emitted_flow_policy_bytes = canonical_json_bytes(flow_policy_payload)
    receipt_id = "receipt/account-interval.json"
    funded_assignment = next(
        row for row in included if row["strategy_id"] == "volatis-strategy"
    )
    raw_evidence_artifact = {
        "artifact_id": "raw/synthetic-provider-evidence.json",
        "as_of_date": "2026-08-13",
        "schema": {"name": "synthetic-provider-evidence", "version": "1"},
        "sha256": "1" * 64,
        "size_bytes": 1,
    }
    receipt_contract: dict[str, object] = {
        "adapter_name": "SYNTHETIC",
        "adapter_version": "1",
        "code_sha256": "b" * 64,
        "provider": "SYNTHETIC",
        "account_id": funded_assignment["account_id"],
        "source_account_identity_sha256": "d" * 64,
        "provider_generation": "1" * 64,
        "provider_generation_manifest": raw_evidence_artifact,
        "raw_evidence_artifacts": [raw_evidence_artifact],
        "required_categories": ["CASH"],
        "pagination": {
            "mode": "SYNTHETIC",
            "page_count": 1,
            "request_response_chain_sha256": "e" * 64,
            "terminal_response_sha256": "f" * 64,
            "terminal": True,
        },
    }
    receipt_payload = {
        "schema_version": 2,
        "artifact_id": receipt_id,
        "issuer": {
            "adapter_name": receipt_contract["adapter_name"],
            "adapter_version": receipt_contract["adapter_version"],
            "code_sha256": receipt_contract["code_sha256"],
        },
        "provider": receipt_contract["provider"],
        "provider_generation": receipt_contract["provider_generation"],
        "provider_generation_manifest": raw_evidence_artifact,
        "raw_evidence_artifacts": [raw_evidence_artifact],
        "account_id": receipt_contract["account_id"],
        "source_account_identity_sha256": receipt_contract[
            "source_account_identity_sha256"
        ],
        "requested_start": funded_assignment["effective_start"],
        "requested_end": "2026-08-13",
        "covered_start": funded_assignment["effective_start"],
        "covered_end": "2026-08-13",
        "required_categories": ["CASH"],
        "completed_categories": ["CASH"],
        "unavailable_categories": [],
        "pagination": receipt_contract["pagination"],
        "completeness": "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES",
        "truncated": False,
    }
    if static_receipt:
        if static_closed_epoch:
            funded_assignment = next(
                row
                for row in included
                if row["strategy_id"] == "tt-portfolio"
                and row["effective_end"] == "2026-01-04"
            )
        static_end = funded_assignment["effective_end"] or calculation_cutoff
        observed_facts_payload: dict[str, dict[str, object]] = {
            "nav": {
                "row_count": 0 if static_closed_epoch else 3,
                "first_date": None if static_closed_epoch else "2026-08-11",
                "last_date": None if static_closed_epoch else "2026-08-13",
            },
            "flows": {"row_count": 0, "first_date": None, "last_date": None},
            "capital_anchors": {"row_count": 0, "first_date": None, "last_date": None},
        }
        if static_closed_epoch and static_blocked_facts:
            # The ledger's own observed facts of a BLOCKED census-assignment
            # epoch: the full daily window, not the stripped calculation row.
            observed_facts_payload = {
                "nav": {
                    "row_count": 41,
                    "first_date": funded_assignment["effective_start"],
                    "last_date": static_end,
                },
                "flows": {
                    "row_count": 3,
                    "first_date": "2025-12-16",
                    "last_date": "2026-01-02",
                },
                "capital_anchors": {
                    "row_count": 0,
                    "first_date": None,
                    "last_date": None,
                },
            }
        nav_facts = observed_facts_payload["nav"]
        if static_receipt_mutation == "funded_nav_count":
            nav_facts["row_count"] = 4
        elif static_receipt_mutation == "blocked_first_after_last":
            nav_facts["first_date"], nav_facts["last_date"] = (
                nav_facts["last_date"],
                nav_facts["first_date"],
            )
        elif static_receipt_mutation == "blocked_first_before_window":
            nav_facts["first_date"] = "2025-11-24"
        elif static_receipt_mutation == "blocked_last_after_window":
            nav_facts["last_date"] = "2026-01-05"
        elif static_receipt_mutation == "blocked_nav_count_below_calculation":
            nav_facts.update(row_count=0, first_date=None, last_date=None)
        elif static_receipt_mutation == "blocked_calculation_nav_outside_dates":
            nav_facts["last_date"] = "2026-01-02"
        static_staged_inputs = {
            "provider_ledger_generation_id": "1" * 64,
            "provider_ledger_artifact": {
                "artifact_id": "normalized/provider-ledger.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "provider-ledger-catalog", "version": "1"},
                "sha256": "2" * 64,
                "size_bytes": 2,
            },
            "baseline_sha256": "5" * 64,
            "baseline_size_bytes": 5,
            "crosswalk_sha256": hashlib.sha256(crosswalk_bytes).hexdigest(),
            "crosswalk_size_bytes": len(crosswalk_bytes),
        }
        receipt_payload = {
            "schema_version": 3,
            "artifact_id": receipt_id,
            "coverage_mode": "STATIC_SNAPSHOT_COVERAGE_V1",
            "issuer": {
                "adapter_name": "static-snapshot-coverage",
                "adapter_version": "1",
                "code_sha256": "b" * 64,
            },
            "provider": funded_assignment["provider"],
            "account_id": funded_assignment["account_id"],
            "strategy_id": funded_assignment["strategy_id"],
            "requested_start": funded_assignment["effective_start"],
            "requested_end": static_end,
            "calculation_cutoff_date": calculation_cutoff,
            "date_convention": "EOD_PROVIDER_NAV_RECOGNITION_DATE",
            "provider_generation": {
                "generation_id": "1" * 64,
                "catalog_sha256": "0" * 64,
                "manifest_sha256": "0" * 64,
                "generation_as_of_date": "2026-08-13",
            },
            "staged_inputs": static_staged_inputs,
            "observed_facts": observed_facts_payload,
            "missing_intervals": [],
            "coverage_as_of_date": "2026-08-16",
            "completeness": "RETAINED_STATIC_SNAPSHOT_ONLY",
        }
        receipt_contract = {
            "coverage_mode": "STATIC_SNAPSHOT_COVERAGE_V1",
            "account_id": funded_assignment["account_id"],
            "strategy_id": funded_assignment["strategy_id"],
            "requested_start": funded_assignment["effective_start"],
            "requested_end": static_end,
            "provider_generation": receipt_payload["provider_generation"],
            "staged_inputs": static_staged_inputs,
        }
        if static_receipt_mutation == "crosswalk":
            static_staged_inputs["crosswalk_sha256"] = "0" * 64
    if authority_graph_mutation == "receipt_subject_schema":
        receipt_payload["schema_version"] = 1
    receipt_bytes = canonical_json_bytes(receipt_payload)
    if authority_graph_mutation == "receipt_contract":
        receipt_contract["provider"] = "SEMANTICALLY_DIFFERENT_PROVIDER"
    receipt_authority_payload = {
        "schema_version": 1,
        "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
        "as_of_date": "2026-08-13",
        "receipts": [
            {
                "artifact_id": receipt_id,
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "size_bytes": len(receipt_bytes),
                "contract": receipt_contract,
            }
        ],
    }
    if authority_graph_mutation == "empty_receipt_authority":
        receipt_authority_payload["receipts"] = []
    elif authority_graph_mutation == "receipt_authority_row_identity":
        receipt_authority_payload["receipts"][0]["sha256"] = "0" * 64
    receipt_authority_bytes = canonical_json_bytes(receipt_authority_payload)
    manifest_schema_bytes = (
        ROOT / "schemas" / "portfolio" / "account-strategy-manifest.schema.json"
    ).read_bytes()
    selected_source_identities = {
        "schema_version": 1,
        "artifact_id": "selected-input-identities",
        "as_of_date": "2026-08-16",
        "provider_ledger": {
            "generation_id": "1" * 64,
            "artifact": {
                "artifact_id": "normalized/provider-ledger.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "provider-ledger-catalog", "version": "1"},
                "sha256": "2" * 64,
                "size_bytes": 2,
            },
            "generation_manifest": {
                "artifact_id": "identity/provider-ledger-generation.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "generation-manifest", "version": "1"},
                "sha256": "3" * 64,
                "size_bytes": 3,
            },
        },
        "baseline": {
            "generation_id": "4" * 64,
            "artifact": {
                "artifact_id": "normalized/portfolio-baseline.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "portfolio-baseline-catalog", "version": "1"},
                "sha256": "5" * 64,
                "size_bytes": 5,
            },
            "generation_manifest": {
                "artifact_id": "identity/portfolio-baseline-generation.json",
                "as_of_date": "2026-08-13",
                "schema": {"name": "generation-manifest", "version": "1"},
                "sha256": "6" * 64,
                "size_bytes": 6,
            },
        },
        "flow_classification_policy": {
            "sha256": hashlib.sha256(
                canonical_json_bytes(json.loads(flow_policy_bytes))
                if canonical_selected_policy
                else flow_policy_bytes
            ).hexdigest(),
            "size_bytes": len(
                canonical_json_bytes(json.loads(flow_policy_bytes))
                if canonical_selected_policy
                else flow_policy_bytes
            ),
        },
        "retrieval_receipts": [
            {
                "receipt_id": receipt_id,
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "size_bytes": len(receipt_bytes),
            }
        ],
        "code_sources": {
            "epoch_producer": {
                "artifact_id": "tools/account_strategy_manifest.py",
                "sha256": "9" * 64,
                "size_bytes": 9,
            },
            "generation_builder": {
                "artifact_id": "tools/build_account_manifest_generation.py",
                "sha256": "a" * 64,
                "size_bytes": 10,
            },
            "independent_verifier": {
                "artifact_id": "tools/verify_account_manifest_generation.py",
                "sha256": "b" * 64,
                "size_bytes": 11,
            },
        },
        "receipt_authority": {
            "sha256": hashlib.sha256(receipt_authority_bytes).hexdigest(),
            "size_bytes": len(receipt_authority_bytes),
        },
        "admission_policy": {"sha256": "d" * 64, "size_bytes": 13},
        "membership_census": {
            "sha256": hashlib.sha256(census_bytes).hexdigest(),
            "size_bytes": len(census_bytes),
        },
        "manifest_schema": {
            "sha256": hashlib.sha256(manifest_schema_bytes).hexdigest(),
            "size_bytes": len(manifest_schema_bytes),
        },
        "current_epoch_crosswalk": {
            "sha256": hashlib.sha256(crosswalk_bytes).hexdigest(),
            "size_bytes": len(crosswalk_bytes),
        },
    }
    if derived_sleeves:
        selected_source_identities["sleeve_facts"] = [
            {"sha256": "7" * 64, "size_bytes": 8}
        ]
    if authority_graph_mutation == "manifest_schema":
        selected_source_identities["manifest_schema"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "retrieval_receipt":
        selected_source_identities["retrieval_receipts"][0]["sha256"] = "0" * 64
    elif authority_graph_mutation == "raw_policy_identity":
        selected_source_identities["flow_classification_policy"]["sha256"] = "0" * 64

    perimeter_bytes = canonical_json_bytes(
        {"rows": [{"assignment": chain["assignment"]} for chain in blocker_chains]}
    )
    coverage_bytes = canonical_json_bytes({"rows": coverage_rows})
    classified_bytes = canonical_json_bytes({"events": []})
    code_identities_bytes = canonical_json_bytes(
        {
            "schema_version": 1,
            "artifact_id": "authority/code-identities.json",
            "as_of_date": "2026-08-16",
            "code_sources": selected_source_identities["code_sources"],
        }
    )
    admission_bytes = canonical_json_bytes(
        {
            "policy_id": "MAPPED_EPOCH_ADMISSION_V2",
            "policy_version": "2",
            "as_of_date": "2026-08-13",
            "admissions": admission_rows,
        }
    )
    selected_identities_bytes = canonical_json_bytes(selected_source_identities)

    def subject_artifact(
        path: str, schema_name: str, schema_version: str
    ) -> dict[str, object]:
        payload_by_path = {
            "authority/account-assignment-perimeter.json": perimeter_bytes,
            "authority/account-flow-coverage.json": coverage_bytes,
            "authority/calculation-inputs.json": calculation_inputs,
            "authority/classified-flows.json": classified_bytes,
            "authority/code-identities.json": code_identities_bytes,
            "authority/flow-classification-policy.json": emitted_flow_policy_bytes,
            "authority/mapped-epoch-admission.json": admission_bytes,
            "authority/selected-input-identities.json": selected_identities_bytes,
            "authority/account-interval-retrieval-receipt-authority.json": (
                receipt_authority_bytes
            ),
        }
        payload = payload_by_path[path]
        artifact = source_artifact(path, schema_name, schema_version, "0")
        artifact["sha256"] = hashlib.sha256(payload).hexdigest()
        artifact["size_bytes"] = len(payload)
        return artifact

    selected_provider = selected_source_identities["provider_ledger"]
    selected_baseline = selected_source_identities["baseline"]
    ledger_identity = {
        "generation_id": selected_provider["generation_id"],
        "artifact": copy.deepcopy(selected_provider["artifact"]),
        "identity_artifact": copy.deepcopy(selected_provider["generation_manifest"]),
    }
    classified_artifact = subject_artifact(
        "authority/classified-flows.json", "classified-flows", "5"
    )
    receipt_authority_artifact = subject_artifact(
        "authority/account-interval-retrieval-receipt-authority.json",
        "account-interval-retrieval-receipt-authority",
        "1",
    )
    manifest_payload["source_identities"] = {
        "ledger": ledger_identity,
        "baseline": {
            "generation_id": selected_baseline["generation_id"],
            "artifact": copy.deepcopy(selected_baseline["artifact"]),
        },
        "classified_flows": {
            "artifact": classified_artifact,
            "schema_version": 5,
            "ledger_identity": ledger_identity,
            "classification_policy": {
                "policy_id": flow_policy_payload["policy_id"],
                "policy_version": flow_policy_payload["policy_version"],
                "policy_as_of_date": flow_policy_payload["as_of_date"],
                "policy_sha256": hashlib.sha256(emitted_flow_policy_bytes).hexdigest(),
            },
        },
        "flow_classification_policy": {
            "policy_id": flow_policy_payload["policy_id"],
            "policy_version": flow_policy_payload["policy_version"],
            "event_dispositions": flow_policy_payload["event_dispositions"],
            "artifact": subject_artifact(
                "authority/flow-classification-policy.json",
                "flow-classification-policy",
                "1",
            ),
        },
        "flow_coverage": {
            "artifact": subject_artifact(
                "authority/account-flow-coverage.json", "account-flow-coverage", "2"
            ),
            "ledger_identity": ledger_identity,
            "classified_flows_artifact": classified_artifact,
            "receipt_authority": receipt_authority_artifact,
        },
        "admission_policy": {
            "policy_id": "MAPPED_EPOCH_ADMISSION_V2",
            "policy_version": "2",
            "artifact": subject_artifact(
                "authority/mapped-epoch-admission.json",
                "account-strategy-manifest-admission-policy",
                "2",
            ),
            "assignment": assignment_identity,
        },
        "assignment": assignment_identity,
        "calculation_code": copy.deepcopy(
            selected_source_identities["code_sources"]["epoch_producer"]
        ),
        "selected_inputs": subject_artifact(
            "authority/selected-input-identities.json",
            "selected-input-identities",
            "1",
        ),
        "account_assignment_perimeter": subject_artifact(
            "authority/account-assignment-perimeter.json",
            "account-assignment-perimeter",
            "1",
        ),
        "calculation_inputs": subject_artifact(
            "authority/calculation-inputs.json",
            "account-manifest-calculation-inputs",
            "4",
        ),
        "code_identities": subject_artifact(
            "authority/code-identities.json",
            "account-manifest-code-identities",
            "1",
        ),
    }
    manifest_sources = manifest_payload["source_identities"]
    if authority_graph_mutation == "baseline":
        manifest_sources["baseline"]["generation_id"] = "0" * 64
    elif authority_graph_mutation == "provider_ledger":
        manifest_sources["ledger"]["artifact"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "classification_policy":
        manifest_sources["classified_flows"]["classification_policy"][
            "policy_sha256"
        ] = "0" * 64
    elif authority_graph_mutation == "admission_policy":
        manifest_sources["admission_policy"]["artifact"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "calculation_code":
        manifest_sources["calculation_code"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "code_identities":
        manifest_sources["code_identities"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "receipt_authority":
        manifest_sources["flow_coverage"]["receipt_authority"]["sha256"] = "0" * 64
    elif authority_graph_mutation == "selected_inputs":
        manifest_sources["selected_inputs"]["sha256"] = "0" * 64

    if manifest_mutation == "stripped":
        manifest_payload = {
            "rows": rows,
            "schema_version": manifest_schema_version,
            "calculation_cutoff_date": manifest_cutoff,
        }
    manifest_bytes = canonical_json_bytes(manifest_payload)
    subject_payloads = {
        "authority/account-assignment-perimeter.json": perimeter_bytes,
        "authority/account-flow-coverage.json": coverage_bytes,
        "authority/calculation-inputs.json": calculation_inputs,
        "authority/classified-flows.json": classified_bytes,
        "authority/code-identities.json": code_identities_bytes,
        "authority/flow-classification-policy.json": emitted_flow_policy_bytes,
        "authority/mapped-epoch-admission.json": admission_bytes,
        "authority/selected-input-identities.json": selected_identities_bytes,
        "outputs/account-strategy-manifest.json": manifest_bytes,
        "authority/account-interval-retrieval-receipt-authority.json": (
            receipt_authority_bytes
        ),
        "authority/account-assignment-crosswalk.json": crosswalk_bytes,
        receipt_id: receipt_bytes,
    }
    subject_paths = (
        "authority/account-assignment-perimeter.json",
        "authority/account-flow-coverage.json",
        "authority/calculation-inputs.json",
        "authority/classified-flows.json",
        "authority/code-identities.json",
        "authority/flow-classification-policy.json",
        "authority/mapped-epoch-admission.json",
        "authority/selected-input-identities.json",
        "outputs/account-strategy-manifest.json",
        "authority/account-interval-retrieval-receipt-authority.json",
        "authority/account-assignment-crosswalk.json",
        receipt_id,
    )
    subjects = [
        {
            "path": path,
            "sha256": hashlib.sha256(subject_payloads[path]).hexdigest(),
            "size_bytes": len(subject_payloads[path]),
        }
        for path in subject_paths
    ]
    verification = {
        "schema_version": 3,
        "artifact_id": "verification/independent-verification.json",
        "as_of_date": "2026-08-16",
        "status": "VERIFIED",
        "verifier": {
            "artifact_id": "tools/verify_account_manifest_generation.py",
            "as_of_date": "2026-08-16",
            "schema": {
                "name": "independent-account-manifest-verifier",
                "version": "1",
            },
            "sha256": selected_source_identities["code_sources"][
                "independent_verifier"
            ]["sha256"],
            "size_bytes": selected_source_identities["code_sources"][
                "independent_verifier"
            ]["size_bytes"],
        },
        "selected_source_identities": selected_source_identities,
        "subjects": subjects,
        "subjects_semantic_sha256": hashlib.sha256(
            canonical_json_bytes({"subjects": subjects})
        ).hexdigest(),
        "counts": counts,
        "blocker_chains": sorted(
            blocker_chains,
            key=lambda chain: tuple(
                str(chain["assignment"][field] or "")
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            ),
        ),
        "numeric_verification": {
            "candidate_count": numeric_candidate_count,
            "comparison_count": numeric_candidate_count,
            "discrepancy_count": 0,
            "disposition": "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES",
        },
        "invariants": {
            "exact_subject_set": True,
            "canonical_subject_bytes": True,
            "selected_source_binding": True,
            "fact_disposition_complete": True,
            "unassigned_numeric_exclusion": True,
            "classified_flow_reconciliation": True,
            "mapped_row_bijection": True,
            "blocker_propagation": True,
            "blocked_numeric_exclusion": True,
            "privacy_safe_projection": True,
        },
    }
    if verification_mutation == "counts":
        verification["counts"]["blocked_output_row_count"] = 11
    elif verification_mutation == "blocker_chain":
        verification["blocker_chains"][0]["output"]["blocking_reason"] = (
            "SEMANTICALLY_DIFFERENT_REASON"
        )
    elif verification_mutation == "selected_source_identity":
        verification["selected_source_identities"]["current_epoch_crosswalk"][
            "sha256"
        ] = "0" * 64
    elif verification_mutation == "subject_identity":
        verification["subjects"][0]["sha256"] = "0" * 64
    elif verification_mutation == "subjects_semantic_sha256":
        verification["subjects_semantic_sha256"] = "0" * 64
    elif verification_mutation == "verifier_identity":
        verification["verifier"]["sha256"] = "0" * 64
    verification_bytes = canonical_json_bytes(verification)
    subject_schemas = {
        "authority/account-assignment-perimeter.json": (
            "account-assignment-perimeter",
            "1",
        ),
        "authority/account-flow-coverage.json": ("account-flow-coverage", "2"),
        "authority/calculation-inputs.json": (
            "account-manifest-calculation-inputs",
            "4",
        ),
        "authority/classified-flows.json": ("classified-flows", "5"),
        "authority/code-identities.json": (
            "account-manifest-code-identities",
            "1",
        ),
        "authority/flow-classification-policy.json": (
            "flow-classification-policy",
            "1",
        ),
        "authority/mapped-epoch-admission.json": (
            "account-strategy-manifest-admission-policy",
            "2",
        ),
        "authority/selected-input-identities.json": (
            "selected-input-identities",
            "1",
        ),
        "outputs/account-strategy-manifest.json": (
            "account-strategy-manifest",
            str(manifest_schema_version),
        ),
        "authority/account-interval-retrieval-receipt-authority.json": (
            "account-interval-retrieval-receipt-authority",
            "1",
        ),
        "authority/account-assignment-crosswalk.json": (
            "account-strategy-current-epoch-crosswalk",
            "1",
        ),
        receipt_id: ("account-interval-retrieval-receipt", "2"),
    }
    subject_artifacts = tuple(
        SourceArtifact(
            artifact_id=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            as_of_date=reporter.date(2026, 8, 16),
            schema_name=subject_schemas[path][0],
            schema_version=subject_schemas[path][1],
        )
        for path, content in subject_payloads.items()
    )
    manifest = GenerationManifest.build(
        as_of_date=reporter.date(2026, 8, 16),
        adapter=AdapterIdentity("synthetic-epoch", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="standalone epoch generation",
            fact_types=("account-strategy-manifest",),
            observed_start=reporter.date(2026, 8, 13),
            observed_end=reporter.date(2026, 8, 13),
        ),
        artifacts=(
            *subject_artifacts,
            SourceArtifact(
                artifact_id="verification/independent-verification.json",
                sha256=hashlib.sha256(verification_bytes).hexdigest(),
                size_bytes=len(verification_bytes),
                as_of_date=reporter.date(2026, 8, 16),
                schema_name="account-manifest-independent-verification",
                schema_version="3",
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    stage_generation_bundle(
        epoch_root,
        manifest,
        {
            **subject_payloads,
            "verification/independent-verification.json": verification_bytes,
        },
    )
    bundle = read_generation_bundle(epoch_root, manifest.generation_id)
    verification_path = (
        epoch_root
        / "generations"
        / manifest.generation_id
        / "verification"
        / "independent-verification.json"
    )
    if sys.platform == "win32":
        assert len(str(verification_path.resolve())) > 260
    generation_manifest_bytes = manifest.to_json_bytes()
    source = {
        "generation_id": manifest.generation_id,
        "generation_manifest_sha256": hashlib.sha256(
            generation_manifest_bytes
        ).hexdigest(),
        "generation_receipt_sha256": hashlib.sha256(bundle.receipt_bytes).hexdigest(),
        "full_directory_inventory_sha256": (bundle.full_directory_inventory_sha256),
        "crosswalk": {
            "path": "authority/account-assignment-crosswalk.json",
            "sha256": hashlib.sha256(crosswalk_bytes).hexdigest(),
            "size_bytes": len(crosswalk_bytes),
            "schema": {
                "name": "account-strategy-current-epoch-crosswalk",
                "version": "1",
            },
        },
        "artifact": {
            "path": "outputs/account-strategy-manifest.json",
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
            "schema": {
                "name": "account-strategy-manifest",
                "version": str(manifest_schema_version),
            },
        },
    }
    source_bytes = canonical_json_bytes(source)
    return epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, verification_bytes


def test_epoch_history_verification_accepts_real_schema9_verified_partition(
    tmp_path: Path,
) -> None:
    (
        epoch_root,
        source_bytes,
        manifest_bytes,
        crosswalk_bytes,
        verification_bytes,
    ) = _verified_schema9_epoch_bundle(tmp_path)
    generation_id = reporter._epoch_source_generation_id(source_bytes)
    verification_path = (
        epoch_root
        / "generations"
        / generation_id
        / "verification"
        / "independent-verification.json"
    )
    verification_schema = json.loads(
        (
            ROOT
            / "schemas"
            / "portfolio"
            / "account-manifest-independent-verification.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(verification_schema, format_checker=FormatChecker()).validate(
        json.loads(verification_bytes)
    )
    manifest_schema = json.loads(
        (
            ROOT / "schemas" / "portfolio" / "account-strategy-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(manifest_schema, format_checker=FormatChecker()).validate(
        json.loads(manifest_bytes)
    )

    path, observed, schema_name, schema_version = (
        reporter._epoch_history_snapshot_evidence(
            epoch_root,
            source_bytes,
            manifest_bytes,
            crosswalk_bytes,
        )
    )

    assert path == verification_path
    assert observed == verification_bytes
    assert schema_name == "account-manifest-independent-verification"
    assert schema_version == "3"


def test_epoch_history_accepts_static_snapshot_receipt_bound_to_subjects(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, static_receipt=True)
    )

    reporter._epoch_history_snapshot_evidence(
        epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
    )


def test_epoch_history_accepts_static_snapshot_closed_epoch_end(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path, static_receipt=True, static_closed_epoch=True
        )
    )

    reporter._epoch_history_snapshot_evidence(
        epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
    )


def test_epoch_history_rejects_self_consistent_static_receipt_crosswalk_mutation(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_receipt_mutation="crosswalk",
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def test_epoch_history_accepts_blocked_epoch_static_receipt_with_its_ledger_facts(
    tmp_path: Path,
) -> None:
    """A-1 (fail-before): a BLOCKED epoch's receipt keeps its ledger facts.

    The static receipt of a BLOCKED (census assignment-only) epoch reports the
    facts of the selected ledger window -- 41 NAV rows and 3 flow rows -- because
    the producer and the independent verifier bind ``observed_facts`` to the
    ledger.  The hash-bound calculation row of the same BLOCKED epoch is
    stripped by design (one closing NAV, no flows, no anchors), so the report
    must not demand the ledger facts equal that projection.  Merge base:
    ``epoch receipt authority semantic graph differs``.
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_closed_epoch=True,
            static_blocked_facts=True,
        )
    )
    bundle = read_generation_bundle(
        epoch_root, json.loads(source_bytes)["generation_id"]
    )
    calculation_inputs = json.loads(
        bundle.payloads["authority/calculation-inputs.json"]
    )
    (blocked_row,) = [
        row
        for row in calculation_inputs["rows"]
        if row["epoch"]["strategy_id"] == "tt-portfolio"
        and row["epoch"]["effective_end"] == "2026-01-04"
        and row["nav_observations"]
    ]
    # Guard: the fixture really is the stripped projection of the real epoch.
    assert len(blocked_row["nav_observations"]) == 1
    assert blocked_row["classified_flows"] == []
    assert blocked_row["capital_anchors"] == []

    reporter._epoch_history_snapshot_evidence(
        epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
    )


def test_epoch_history_rejects_funded_static_receipt_with_tampered_facts(
    tmp_path: Path,
) -> None:
    """A-2: a FUNDED epoch's receipt facts still equal the projection exactly.

    Fails before and after the change: a FUNDED epoch's calculation row equals
    the ledger projection, so its receipt may not carry any other fact.
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_receipt_mutation="funded_nav_count",
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize("mutation", ("blocked_calc_flow", "blocked_calc_anchor"))
def test_epoch_history_rejects_blocked_epoch_calculation_row_carrying_facts(
    tmp_path: Path, mutation: str
) -> None:
    """A-3: the BLOCKED calculation row is a sub-projection with no flow or anchor.

    The ledger facts the receipt reports may be wider than the calculation row,
    never the other way round, and a BLOCKED epoch's calculation row carries no
    flow and no capital anchor.
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_closed_epoch=True,
            static_blocked_facts=True,
            static_receipt_mutation=mutation,
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "blocked_first_after_last",
        "blocked_first_before_window",
        "blocked_last_after_window",
    ),
)
def test_epoch_history_rejects_blocked_receipt_facts_outside_the_epoch_window(
    tmp_path: Path, mutation: str
) -> None:
    """A-4: malformed BLOCKED receipt facts fail closed (schema-valid but wrong).

    A first date after the last date, or a date outside the epoch's requested
    window, passes the receipt schema; the structural check refuses it.
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_closed_epoch=True,
            static_blocked_facts=True,
            static_receipt_mutation=mutation,
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    "mutation",
    ("blocked_nav_count_below_calculation", "blocked_calculation_nav_outside_dates"),
)
def test_epoch_history_rejects_blocked_receipt_that_does_not_cover_the_projection(
    tmp_path: Path, mutation: str
) -> None:
    """The receipt's NAV facts must cover the stripped calculation row's NAV.

    A receipt reporting fewer NAV rows than the calculation row holds, or a NAV
    date outside the receipt's own first/last dates, cannot be the ledger window
    the calculation row was projected from.
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_closed_epoch=True,
            static_blocked_facts=True,
            static_receipt_mutation=mutation,
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize("mutation", ("missing_admission", "unknown_admission_status"))
@pytest.mark.parametrize("blocked_facts", (True, False))
def test_epoch_history_rejects_static_receipt_without_a_funded_or_blocked_admission(
    tmp_path: Path, mutation: str, blocked_facts: bool
) -> None:
    """A-5: the receipt's epoch must have exactly one FUNDED or BLOCKED admission.

    A BLOCKED-looking receipt is never accepted just because its epoch has no
    admission row, and an admission status other than FUNDED/BLOCKED is refused
    at the receipt (the facts are equal here, so only the admission decides).
    """

    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            static_closed_epoch=True,
            static_blocked_facts=blocked_facts,
            static_receipt_mutation=mutation,
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def _summary(
    row_count: object, first_date: object, last_date: object
) -> dict[str, object]:
    return {"row_count": row_count, "first_date": first_date, "last_date": last_date}


def _blocked_facts(
    nav: dict[str, object] | None = None,
    flows: dict[str, object] | None = None,
    capital_anchors: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "nav": nav or _summary(41, "2025-11-25", "2026-01-04"),
        "flows": flows or _summary(3, "2025-12-16", "2026-01-02"),
        "capital_anchors": capital_anchors or _summary(0, None, None),
    }


def _stripped_calculation_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "nav_observations": [{"as_of_date": "2026-01-04", "value": "100"}],
        "classified_flows": [],
        "capital_anchors": [],
    }
    row.update(changes)
    return row


def test_blocked_static_receipt_facts_bind_accepts_the_real_shapes() -> None:
    start, end = "2025-11-25", "2026-01-04"
    bind = reporter._blocked_static_receipt_facts_bind
    # 38b6: 41 NAV rows and 3 flows; 98f5: the same NAV window and no flow.
    assert bind(_blocked_facts(), _stripped_calculation_row(), start, end)
    assert bind(
        _blocked_facts(flows=_summary(0, None, None)),
        _stripped_calculation_row(),
        start,
        end,
    )
    # A receipt that saw exactly the stripped row, and a row with no NAV at all.
    assert bind(
        _blocked_facts(nav=_summary(1, "2026-01-04", "2026-01-04")),
        _stripped_calculation_row(),
        start,
        end,
    )
    assert bind(
        _blocked_facts(nav=_summary(0, None, None)),
        _stripped_calculation_row(nav_observations=[]),
        start,
        end,
    )


@pytest.mark.parametrize(
    ("facts", "row"),
    (
        pytest.param(
            {**_blocked_facts(), "positions": _summary(0, None, None)},
            {},
            id="extra_fact_group",
        ),
        pytest.param(
            {key: value for key, value in _blocked_facts().items() if key != "flows"},
            {},
            id="missing_fact_group",
        ),
        pytest.param(
            _blocked_facts(nav={**_summary(41, "2025-11-25", "2026-01-04"), "x": 1}),
            {},
            id="summary_with_extra_key",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(True, "2025-11-25", "2026-01-04")),
            {},
            id="boolean_row_count",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(-1, None, None)), {}, id="negative_row_count"
        ),
        pytest.param(
            _blocked_facts(nav=_summary("41", "2025-11-25", "2026-01-04")),
            {},
            id="text_row_count",
        ),
        pytest.param(
            _blocked_facts(flows=_summary(0, "2025-12-16", None)),
            {},
            id="dates_without_rows",
        ),
        pytest.param(
            _blocked_facts(flows=_summary(2, None, None)),
            {},
            id="rows_without_dates",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(41, "2025-11-25", "01/04/2026")),
            {},
            id="non_iso_date",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(41, "2026-01-04", "2025-11-25")),
            {},
            id="first_after_last",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(41, "2025-11-24", "2026-01-04")),
            {},
            id="first_before_the_window",
        ),
        pytest.param(
            _blocked_facts(flows=_summary(3, "2025-12-16", "2026-01-05")),
            {},
            id="flow_after_the_window",
        ),
        pytest.param(
            _blocked_facts(),
            {"classified_flows": [{"event_date": "2026-01-02"}]},
            id="calculation_row_with_a_flow",
        ),
        pytest.param(
            _blocked_facts(),
            {"capital_anchors": [{"event_date": "2026-01-02"}]},
            id="calculation_row_with_an_anchor",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(1, "2026-01-04", "2026-01-04")),
            {
                "nav_observations": [
                    {"as_of_date": "2026-01-03"},
                    {"as_of_date": "2026-01-04"},
                ]
            },
            id="more_calculation_nav_than_receipt_nav",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(41, "2025-11-25", "2026-01-02")),
            {},
            id="calculation_nav_after_the_receipt_last_date",
        ),
        pytest.param(
            _blocked_facts(nav=_summary(41, "2026-01-05", "2026-01-05")),
            {},
            id="receipt_window_entirely_after_the_epoch",
        ),
        pytest.param(
            _blocked_facts(),
            {"nav_observations": [{"value": "100"}]},
            id="calculation_nav_without_a_date",
        ),
        pytest.param(_blocked_facts(), {"nav_observations": None}, id="no_nav_list"),
        pytest.param("not-an-object", {}, id="facts_not_an_object"),
    ),
)
def test_blocked_static_receipt_facts_bind_refuses_everything_else(
    facts: object, row: dict[str, object]
) -> None:
    assert not reporter._blocked_static_receipt_facts_bind(
        facts, _stripped_calculation_row(**row), "2025-11-25", "2026-01-04"
    )


def test_blocked_static_receipt_nav_count_must_cover_the_calculation_rows() -> None:
    """W19(a) (seat mutation MS-1): the receipt's NAV count binds the stripped row.

    The calculation row of a BLOCKED epoch is a sub-projection of the ledger
    window its receipt describes, so the receipt cannot have counted fewer NAV
    rows than the row holds.  Every other clause accepts the receipt below: it is
    well formed, its window is the epoch's, the row holds no flow and no anchor,
    and both NAV dates lie inside the receipt's own first and last dates.  The
    row count is the only difference between the good and the bad case, so
    removing the ``len(nav_rows) > nav[0]`` clause is the only way to accept
    the bad one.  The refusal case ``more_calculation_nav_than_receipt_nav``
    above is also refused by the date-containment clause, and the fixture
    mutation ``blocked_nav_count_below_calculation`` by the null-date clause, so
    neither of them isolates the count.
    """

    start, end = "2025-11-25", "2026-01-04"
    two_nav_rows = _stripped_calculation_row(
        nav_observations=[
            {"as_of_date": "2025-12-01", "value": "100"},
            {"as_of_date": "2026-01-04", "value": "101"},
        ]
    )
    bind = reporter._blocked_static_receipt_facts_bind

    # Good: the receipt counted both rows, or more (the ledger window is wider).
    assert bind(_blocked_facts(nav=_summary(2, start, end)), two_nav_rows, start, end)
    assert bind(_blocked_facts(nav=_summary(41, start, end)), two_nav_rows, start, end)
    # Bad: the receipt counted one row; the calculation row holds two, both inside
    # the receipt's own first and last dates.
    assert not bind(
        _blocked_facts(nav=_summary(1, start, end)), two_nav_rows, start, end
    )


def test_report_r3_allow_list_equals_the_hwm_selected_input_allow_list() -> None:
    """W19(b): the report's restated R3 allow-list cannot drift from the HWM one.

    ``tools/portfolio_report.py`` restates ``_R3_ZERO_CAPITAL_REASONS`` so that
    ``packages/`` stays free of tools imports; the HWM selected-input gate
    (``tools/portfolio_hwm_selected_inputs.py``) holds the original.  A reason
    admitted on one side only would let a funded epoch pass the HWM gate and fail
    the report, or the other way round.  Both hold exactly the one reason the
    return engine emits for a zero-capital run.
    """

    # Imported at run time, not collection (see the ``selected_hwm`` fixture of
    # tests/core/test_hwm_selected_inputs_nonperformance.py for why).
    import portfolio_hwm_selected_inputs as selected_hwm
    from quant_engine.portfolio_ledger.returns import ZERO_CAPITAL_RETURN_UNDEFINED

    assert reporter._R3_ZERO_CAPITAL_REASONS == selected_hwm._R3_ZERO_CAPITAL_REASONS
    assert selected_hwm._R3_ZERO_CAPITAL_REASONS == frozenset(
        {ZERO_CAPITAL_RETURN_UNDEFINED}
    )


def test_verified_epoch_details_feed_the_summed_value_reconcile_with_the_r3_member(
    tmp_path: Path,
) -> None:
    """Seam (fail-before): the verified generation's own output feeds the gate.

    Producer: ``_verified_epoch_authority_details`` over a verified schema-9
    generation in which one tt-portfolio epoch is the R3 zero-capital member
    (FUNDED, OK, UNAVAILABLE for ZERO_CAPITAL_RETURN_UNDEFINED, empty trace).
    Consumer: ``_reconcile_multi_account_strategy_composite``.  The details
    carry the private R3 inputs -- the performance status, reason, opening date
    and the member's calculation-input facts, for that member only -- and the
    reconcile accepts a canonical path equal to the independently computed
    summed-value composite (five traced members 100 -> 101 and the R3 member 50
    -> 42 after a 10 end-of-day withdrawal: 557 / 550 - 1) and refuses one that
    is off by 1e-9.  The merge base's details carry none of it.
    """

    r3 = "account-702c489650e59956"
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path, all_numeric_candidates=True, r3_account_id=r3
        )
    )
    source = json.loads(source_bytes)
    details = reporter._validate_epoch_generation_authority(
        epoch_root, source, manifest_bytes, crosswalk_bytes
    )
    bundle = read_generation_bundle(epoch_root, source["generation_id"])
    calculation_rows = json.loads(bundle.payloads["authority/calculation-inputs.json"])[
        "rows"
    ]
    (r3_row,) = [
        row
        for row in calculation_rows
        if row["epoch"]["account_id"] == r3
        and row["epoch"]["strategy_id"] == "tt-portfolio"
    ]

    zero_run = details[(r3, "tt-portfolio", "2025-11-25", None)]
    assert zero_run["epoch_admission_status"] == "FUNDED"
    assert zero_run["own_path_links"] == []
    assert zero_run["performance_status"] == "UNAVAILABLE"
    assert zero_run["performance_reason"] == "ZERO_CAPITAL_RETURN_UNDEFINED"
    assert zero_run["performance_opening_date"] == "2026-08-11"
    assert zero_run["by_value_source"] == {
        "nav_observations": r3_row["nav_observations"],
        "classified_flows": r3_row["classified_flows"],
        "capital_anchors": r3_row["capital_anchors"],
    }
    assert len(zero_run["by_value_source"]["nav_observations"]) == 2
    assert zero_run["performance_payload"]["twr"] is None
    assert zero_run["performance_payload"]["twr_status"] == "UNAVAILABLE"
    traced = [
        detail
        for key, detail in details.items()
        if key[1] == "tt-portfolio" and key[0] != r3
    ]
    assert len(traced) == 5
    for detail in traced:
        assert detail["performance_status"] == "AVAILABLE"
        assert detail["performance_reason"] is None
        assert detail["by_value_source"] is None
        assert len(detail["own_path_links"]) == 1

    expected = Decimal("557") / Decimal("550") - Decimal(1)
    reporter._reconcile_multi_account_strategy_composite(
        "tt-portfolio", details, {"tt-portfolio": {"cumulative_return": str(expected)}}
    )
    with pytest.raises(
        reporter.ReportValidationError,
        match="multi-account member own paths do not reconcile "
        "to the strategy composite for tt-portfolio",
    ):
        reporter._reconcile_multi_account_strategy_composite(
            "tt-portfolio",
            details,
            {"tt-portfolio": {"cumulative_return": str(expected + Decimal("1e-9"))}},
        )


def test_epoch_history_accepts_canonical_selected_policy_identity(
    tmp_path: Path,
) -> None:
    root, source, manifest, crosswalk, _ = _verified_schema9_epoch_bundle(
        tmp_path, canonical_selected_policy=True
    )
    reporter._epoch_history_snapshot_evidence(root, source, manifest, crosswalk)


def test_epoch_history_rejects_selected_raw_policy_identity_mutation(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            static_receipt=True,
            authority_graph_mutation="raw_policy_identity",
        )
    )

    with pytest.raises(
        reporter.ReportValidationError, match="selected authority graph"
    ):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def test_epoch_history_verification_rejects_schema9_manifest_shell(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _ = (
        _verified_schema9_epoch_bundle(tmp_path, manifest_mutation="stripped")
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="epoch manifest schema-9 authority differs",
    ):
        reporter._epoch_history_snapshot_evidence(
            epoch_root,
            source_bytes,
            manifest_bytes,
            crosswalk_bytes,
        )


def test_epoch_history_verification_rejects_schema8_tuple_only_generation(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, manifest_schema_version=8)
    )

    with pytest.raises(reporter.ReportValidationError, match="schema-9"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def test_epoch_history_verification_accepts_flat_admissions_with_all_numeric_candidates(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, all_numeric_candidates=True)
    )

    snapshot_path, _payload, schema_name, schema_version = (
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )
    )

    assert snapshot_path.name == "independent-verification.json"
    assert (schema_name, schema_version) == (
        "account-manifest-independent-verification",
        "3",
    )


@pytest.mark.parametrize("mutation", ("counts", "blocker_chain"))
def test_epoch_history_verification_rejects_semantic_partition_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, verification_mutation=mutation)
    )

    with pytest.raises(reporter.ReportValidationError, match="semantic"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "selected_source_identity",
        "subject_identity",
        "subjects_semantic_sha256",
        "calculation_row_count",
        "coverage_chain",
    ),
)
def test_epoch_history_verification_rejects_independent_authority_binding_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, verification_mutation=mutation)
    )

    with pytest.raises(reporter.ReportValidationError, match="semantic|subject|source"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "baseline",
        "provider_ledger",
        "classification_policy",
        "admission_policy",
        "calculation_code",
        "code_identities",
        "receipt_authority",
        "retrieval_receipt",
        "manifest_schema",
        "selected_inputs",
    ),
)
def test_epoch_history_rejects_internally_inconsistent_authority_graph(
    tmp_path: Path, mutation: str
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, authority_graph_mutation=mutation)
    )

    with pytest.raises(
        reporter.ReportValidationError,
        match="authority graph|receipt authority",
    ):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def test_epoch_history_rejects_selected_independent_verifier_mismatch(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path, verification_mutation="verifier_identity"
        )
    )

    with pytest.raises(reporter.ReportValidationError, match="verifier identity"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "empty_receipt_authority",
        "receipt_authority_row_identity",
        "receipt_contract",
        "receipt_subject_schema",
    ),
)
def test_epoch_history_rejects_receipt_authority_semantic_rewrite(
    tmp_path: Path, mutation: str
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(tmp_path, authority_graph_mutation=mutation)
    )

    with pytest.raises(reporter.ReportValidationError, match="receipt authority"):
        reporter._epoch_history_snapshot_evidence(
            epoch_root, source_bytes, manifest_bytes, crosswalk_bytes
        )


def test_epoch_calculation_input_rejects_post_cutoff_nested_as_of_date(
    tmp_path: Path,
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path, verification_mutation="calculation_as_of_date"
        )
    )
    source = json.loads(source_bytes)

    with pytest.raises(reporter.ReportValidationError, match="exceeds cutoff"):
        reporter._validate_epoch_generation_authority(
            epoch_root, source, manifest_bytes, crosswalk_bytes
        )


@pytest.mark.parametrize(
    ("manifest_cutoff", "calculation_cutoff"),
    (
        ("2026-08-12", "2026-08-13"),
        ("2026-08-13", "2026-08-12"),
        ("2026-08-12", "2026-08-12"),
    ),
)
def test_epoch_generation_rejects_any_cutoff_authority_mismatch(
    tmp_path: Path, manifest_cutoff: str, calculation_cutoff: str
) -> None:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path,
            manifest_cutoff=manifest_cutoff,
            calculation_cutoff=calculation_cutoff,
        )
    )
    source = json.loads(source_bytes)

    with pytest.raises(reporter.ReportValidationError, match="cutoff"):
        reporter._validate_epoch_generation_authority(
            epoch_root, source, manifest_bytes, crosswalk_bytes
        )


# --- derived sleeves (#567, stage 5) ------------------------------------------


def _nested_controls(extra: str = "p123-conn-one") -> dict[str, dict[str, str]]:
    controls = {
        strategy_id: {
            "product_requirement": (
                "REQUIRED"
                if strategy_id in reporter.CURRENT_REQUIRED_STRATEGY_IDS
                else "EXCLUDED"
            ),
            "lifecycle_status": (
                "ACTIVE"
                if strategy_id in reporter.CURRENT_REQUIRED_STRATEGY_IDS
                else "DEPRECATED_HISTORY_ONLY"
            ),
        }
        for strategy_id in reporter.CURRENT_STRATEGY_CONTROL_IDS
    }
    controls[extra] = {
        "product_requirement": "EXCLUDED",
        "lifecycle_status": "DEPRECATED_HISTORY_ONLY",
    }
    return controls


def _derived_generation(tmp_path: Path) -> tuple[Path, dict[str, object], Any]:
    epoch_root, source_bytes, manifest_bytes, crosswalk_bytes, _verification = (
        _verified_schema9_epoch_bundle(
            tmp_path, all_numeric_candidates=True, derived_sleeves=True
        )
    )
    source = json.loads(source_bytes)
    bundle = read_generation_bundle(epoch_root, source["generation_id"])
    return (
        epoch_root,
        {
            "source": source,
            "manifest": manifest_bytes,
            "crosswalk": crosswalk_bytes,
        },
        bundle,
    )


def test_a_derived_sleeve_generation_validates_and_names_each_parent(
    tmp_path: Path,
) -> None:
    root, parts, _bundle = _derived_generation(tmp_path)
    details = reporter._validate_epoch_generation_authority(
        root, parts["source"], parts["manifest"], parts["crosswalk"]
    )

    assert len(details) == 14
    parent = next(
        row["account_id"]
        for row in json.loads(parts["crosswalk"])["rows"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    derived = {
        key: detail["derived_parent_account_id"]
        for key, detail in details.items()
        if detail["derived_parent_account_id"] is not None
    }
    assert derived == {
        (f"{parent}-s2", "p123-ai-factor", "2026-01-30", None): parent,
        (f"{parent}-s1", "p123-conn-one", "2026-01-26", "2026-06-23"): parent,
    }
    # the residual parent has no epoch any more, every baseline epoch has no parent
    assert not any(key[0] == parent for key in details)
    assert all(
        detail["derived_parent_account_id"] is None
        for key, detail in details.items()
        if key not in derived
    )


def test_a_derived_sleeve_manifest_binds_to_the_crosswalk_through_coverage(
    tmp_path: Path,
) -> None:
    _root, parts, bundle = _derived_generation(tmp_path)
    manifest = json.loads(parts["manifest"])
    crosswalk = json.loads(parts["crosswalk"])
    coverage = reporter._generation_coverage_rows(bundle)

    actual = reporter._validate_report_account_manifest(
        manifest,
        _nested_controls(),
        crosswalk=crosswalk,
        coverage_rows=coverage,
    )
    assert len(actual) == 14

    # without the coverage authority the manifest differs from the crosswalk
    with pytest.raises(reporter.ReportValidationError, match="strategy is excluded"):
        reporter._validate_report_account_manifest(
            manifest, _nested_controls(), crosswalk=crosswalk, coverage_rows=()
        )
    # a history-only sleeve of a strategy the controls do not know is refused
    with pytest.raises(reporter.ReportValidationError, match="strategy is excluded"):
        reporter._validate_report_account_manifest(
            manifest,
            _nested_controls("another-strategy"),
            crosswalk=crosswalk,
            coverage_rows=coverage,
        )
    # a control beyond the pinned perimeter must be a deprecated exclusion
    controls = _nested_controls()
    controls["p123-conn-one"]["product_requirement"] = "REQUIRED"
    with pytest.raises(reporter.ReportValidationError, match="controls are not exact"):
        reporter._validate_report_account_manifest(
            manifest, controls, crosswalk=crosswalk, coverage_rows=coverage
        )
    # a derived epoch covered twice is refused
    twice = next(
        row
        for row in coverage
        if row.get("authority_kind") == "DERIVED_SLEEVE_COVERAGE_V1"
    )
    with pytest.raises(reporter.ReportValidationError, match="covered twice"):
        reporter._validate_report_account_manifest(
            manifest,
            _nested_controls(),
            crosswalk=crosswalk,
            coverage_rows=[*coverage, twice],
        )


def test_the_epoch_history_evidence_accepts_a_derived_sleeve_generation(
    tmp_path: Path,
) -> None:
    root, parts, _bundle = _derived_generation(tmp_path)
    source_bytes = canonical_json_bytes(parts["source"])

    reporter._epoch_history_snapshot_evidence(
        root, source_bytes, parts["manifest"], parts["crosswalk"]
    )

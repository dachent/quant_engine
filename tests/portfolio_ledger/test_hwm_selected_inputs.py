"""Source-bound selected-input adapter for the governed HWM kernel."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Callable

import pytest

import portfolio_financial_mvp_contract as financial_contract
import portfolio_hwm_selected_inputs as selected_hwm
from portfolio_hwm_selected_inputs import (
    SelectedHwmInputError,
    _validated_account_links,
    build_selected_hwm_inputs,
)
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.publication import (
    GenerationConflictError,
    stage_generation_bundle,
)
from quant_engine.core.publication import read_generation_bundle
from quant_engine.portfolio_ledger.hwm import AS_HELD_HWM_RETURN_BASIS


REPO = Path(__file__).resolve().parents[2]
DAY = date(2026, 9, 4)
_INCLUDED = {"FUNDED_MEMBERSHIP", "ASSIGNMENT_ONLY"}


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def _artifact(path: str, payload: bytes, schema: str, version: str) -> SourceArtifact:
    return SourceArtifact(
        artifact_id=path,
        sha256=_sha(payload),
        size_bytes=len(payload),
        as_of_date=DAY,
        schema_name=schema,
        schema_version=version,
    )


def _epoch_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sleeve_index, sleeve in enumerate(
        contract["current_census"]["account_sleeves"]
    ):
        for assignment in sleeve["assignments"]:
            if assignment["status"] not in _INCLUDED:
                continue
            rows.append(
                {
                    "account_id": f"synthetic-account-{sleeve_index:02d}",
                    "provider": sleeve["provider_id"],
                    "strategy_id": assignment["strategy_id"],
                    "effective_start": assignment["effective_start"],
                    "effective_end": assignment["effective_end"],
                }
            )
    assert len(rows) == 13
    assert len({row["strategy_id"] for row in rows}) == 7
    return rows


def _docs(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs = _epoch_specs(contract)
    interval_starts: dict[str, set[date]] = {}
    for row in specs:
        interval_starts.setdefault(row["strategy_id"], set()).add(
            date.fromisoformat(row["effective_start"])
        )

    crosswalk_rows = []
    calculation_rows = []
    output_rows = []
    for index, spec in enumerate(specs, start=1):
        epoch = {
            "account_id": spec["account_id"],
            "strategy_id": spec["strategy_id"],
            "effective_start": spec["effective_start"],
            "effective_end": spec["effective_end"],
        }
        crosswalk_rows.append(
            {
                **spec,
                "account_label": f"Synthetic {index}",
                "assignment_id": f"assignment-{index:02d}",
                "last_four": f"{index:04d}",
                "owner_reason": "synthetic source-bound HWM fixture",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            }
        )
        navs: dict[str, Decimal] = {}
        links = []
        cumulative = Decimal("100")
        start_bound = date.fromisoformat(spec["effective_start"])
        end_bound = (
            date.fromisoformat(spec["effective_end"])
            if spec["effective_end"] is not None
            else None
        )
        for interval_start in sorted(interval_starts[spec["strategy_id"]]):
            if interval_start < start_bound or (
                end_bound is not None and interval_start >= end_bound
            ):
                continue
            interval_end = interval_start + timedelta(days=1)
            base = Decimal(index * 1000) + Decimal(interval_start.day)
            factor = Decimal("1") + Decimal(index) / Decimal("1000")
            ending = base * factor
            navs[interval_start.isoformat()] = base
            navs[interval_end.isoformat()] = ending
            cumulative *= factor
            links.append(
                {
                    "start_date": interval_start.isoformat(),
                    "end_date": interval_end.isoformat(),
                    "base_nav": str(base),
                    "beginning_flow": "0",
                    "ending_nav": str(ending),
                    "ending_flow": "0",
                    "net_flow": "0",
                    "factor": str(factor),
                    "cumulative_index": str(cumulative),
                    "running_hwm_index": str(cumulative),
                    "drawdown": "0",
                }
            )
        observations = [
            {
                "account_id": spec["account_id"],
                "as_of_date": observed_on,
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": str(value)},
                "record_id": f"synthetic:{index}:{observed_on}",
            }
            for observed_on, value in sorted(navs.items())
        ]
        calculation_rows.append(
            {
                "epoch": epoch,
                "provider": spec["provider"],
                "nav_observations": observations,
                "classified_flows": [],
                "capital_anchors": [
                    {
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": str(next(iter(navs.values()))),
                        },
                        "as_of_date": next(iter(navs)),
                        "record_id": f"synthetic:anchor:{index}",
                    }
                ],
            }
        )
        output_rows.append(
            {
                **spec,
                "status": "OK",
                "observation_trace": {"links": links},
                "observation_linked_return": str(cumulative / Decimal("100") - 1),
            }
        )

    crosswalk = {
        "artifact_id": "account-assignment-crosswalk",
        "schema_version": 1,
        "as_of_date": DAY.isoformat(),
        "calculation_cutoff_date": DAY.isoformat(),
        "selected_baseline": {},
        "disposition_counts": {"INCLUDED_PRODUCT_EPOCH": 13},
        "rows": crosswalk_rows,
    }
    calculation = {
        "artifact_id": "account-manifest-calculation-inputs",
        "schema_version": 4,
        "as_of_date": DAY.isoformat(),
        "accounting_currency": "USD",
        "rows": calculation_rows,
    }
    output = {
        "artifact": "account-strategy-manifest",
        "schema_version": 9,
        "accounting_currency": "USD",
        "calculation_cutoff_date": DAY.isoformat(),
        "rows": output_rows,
    }
    verifier_path = REPO / "tools" / "verify_account_manifest_generation.py"
    report = {
        "artifact_id": "account-manifest-independent-verification",
        "schema_version": 3,
        "as_of_date": DAY.isoformat(),
        "status": "VERIFIED",
        "counts": {
            "current_product_epoch_count": 13,
            "calculation_input_row_count": 13,
            "output_row_count": 13,
            "ok_output_row_count": 13,
            "blocked_output_row_count": 0,
        },
        "invariants": {
            "canonical_subject_bytes": True,
            "exact_subject_set": True,
            "mapped_row_bijection": True,
            "selected_source_binding": True,
        },
        "numeric_verification": {
            "candidate_count": 13,
            "comparison_count": 13,
            "discrepancy_count": 0,
            "disposition": "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES",
        },
        "selected_source_identities": {
            "provider_ledger": {"generation_id": _sha("synthetic-provider-ledger")},
            "code_sources": {
                "independent_verifier": {
                    "artifact_id": "tools/verify_account_manifest_generation.py",
                    "sha256": _sha(verifier_path.read_bytes()),
                    "size_bytes": verifier_path.stat().st_size,
                }
            },
        },
    }
    return {
        "authority/account-assignment-crosswalk.json": crosswalk,
        "authority/calculation-inputs.json": calculation,
        "outputs/account-strategy-manifest.json": output,
        "verification/independent-verification.json": report,
    }


def _load_account_builder_fixture() -> tuple[object, object]:
    fixture_path = (
        REPO
        / "tests"
        / "portfolio_control_panel"
        / "test_account_manifest_generation.py"
    )
    fixture_spec = importlib.util.spec_from_file_location(
        "hwm_account_builder_fixture", fixture_path
    )
    assert fixture_spec is not None and fixture_spec.loader is not None
    fixture = importlib.util.module_from_spec(fixture_spec)
    sys.modules[fixture_spec.name] = fixture
    fixture_spec.loader.exec_module(fixture)

    builder_path = REPO / "tools" / "build_account_manifest_generation.py"
    builder_spec = importlib.util.spec_from_file_location(
        "hwm_account_builder", builder_path
    )
    assert builder_spec is not None and builder_spec.loader is not None
    builder = importlib.util.module_from_spec(builder_spec)
    sys.modules[builder_spec.name] = builder
    builder_spec.loader.exec_module(builder)
    return fixture, builder


def _all_contract_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sleeve_index, sleeve in enumerate(
        contract["current_census"]["account_sleeves"]
    ):
        for assignment in sleeve["assignments"]:
            if assignment["status"] not in {
                "FUNDED_MEMBERSHIP",
                "ASSIGNMENT_ONLY",
                "DEPRECATED_HISTORY_ONLY",
                "EXCLUDED_IMMATERIAL",
            }:
                continue
            rows.append(
                {
                    "account_id": f"synthetic-account-{sleeve_index:02d}",
                    "provider": sleeve["provider_id"],
                    "strategy_id": assignment["strategy_id"],
                    "effective_start": assignment["effective_start"],
                    "effective_end": assignment["effective_end"],
                    "status": assignment["status"],
                }
            )
    assert len(rows) == 15
    return rows


def _manifest_snapshot(
    fixture: object,
    *,
    snapshot: object,
    artifact_id: str,
    schema_name: str,
) -> object:
    legacy = fixture._generation_manifest_snapshot(
        catalog=snapshot,
        catalog_artifact_id=artifact_id,
        catalog_schema_name=schema_name,
        catalog_schema_version="1",
        as_of_date=DAY.isoformat(),
    )
    legacy_manifest = GenerationManifest.from_json_bytes(legacy.contents)
    report = _source_verification_report(snapshot)
    document = json.loads(snapshot.contents)
    adapter = legacy_manifest.adapter
    if schema_name == "provider-ledger-catalog" and document.get("strategy_returns"):
        adapter = AdapterIdentity("quant-engine-provider-ledger", "SYNTHETIC")
    elif schema_name == "portfolio-baseline-catalog" and document.get("allocations"):
        adapter = AdapterIdentity("portfolio-baseline-builder", "SYNTHETIC")
    aliases = (
        ()
        if adapter == legacy_manifest.adapter
        else (
            replace(legacy_manifest.artifacts[0], artifact_id=Path(artifact_id).name),
        )
    )
    manifest = GenerationManifest.build(
        as_of_date=legacy_manifest.as_of_date,
        adapter=adapter,
        coverage=legacy_manifest.coverage,
        artifacts=(
            legacy_manifest.artifacts[0],
            *aliases,
            _artifact(
                "verification/independent-verification.json",
                report,
                "synthetic-source-independent-verification",
                "1",
            ),
        ),
        verification=legacy_manifest.verification,
    )
    return fixture.InputSnapshot.from_bytes(
        "HWM synthetic source generation manifest", manifest.to_json_bytes()
    )


def _source_verification_report(snapshot: object) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "status": "VERIFIED",
            "subject_sha256": snapshot.sha256,
        }
    )


def _stage_snapshot_generation(
    tmp_path: Path,
    *,
    snapshot: object,
    manifest_snapshot: object,
    root_name: str,
) -> Path:
    manifest = GenerationManifest.from_json_bytes(manifest_snapshot.contents)
    report = _source_verification_report(snapshot)
    final = stage_generation_bundle(
        tmp_path / root_name,
        manifest,
        {
            **{
                artifact.artifact_id: snapshot.contents
                for artifact in manifest.artifacts
                if artifact.artifact_id != "verification/independent-verification.json"
            },
            "verification/independent-verification.json": report,
        },
    )
    return final / "generation.json"


def _selected_inputs(
    tmp_path: Path,
    contract: dict[str, Any],
    *,
    mutate_ledger: Callable[[dict[str, Any]], None] | None = None,
    mutate_baseline: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    daily: bool = False,
) -> tuple[object, object, Path, Path]:
    fixture, builder = _load_account_builder_fixture()
    base_selected = fixture._mixed_crosswalk_selected_inputs(builder)
    specs = _all_contract_specs(contract)
    included = [row for row in specs if row["status"] in _INCLUDED]
    assert len(included) == 13

    account_rows = {
        row["account_id"]: {
            "account_id": row["account_id"],
            "base_currency": "USD",
            "provider": row["provider"],
            "role": "INVESTMENT",
        }
        for row in specs
    }
    assignments = [
        {
            "assignment_id": f"assignment-{index:02d}",
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "effective_start": row["effective_start"],
            "effective_end": row["effective_end"],
        }
        for index, row in enumerate(specs, 1)
    ]
    baseline_doc = {
        "as_of_date": DAY.isoformat(),
        "baseline_schema_version": 1,
        "accounts": sorted(account_rows.values(), key=lambda row: row["account_id"]),
        "assignments": assignments,
    }
    interval_starts: dict[str, set[date]] = {}
    for row in included:
        strategy_boundaries = interval_starts.setdefault(row["strategy_id"], set())
        strategy_boundaries.add(date.fromisoformat(row["effective_start"]))
        if row["effective_end"] is not None:
            strategy_boundaries.add(date.fromisoformat(row["effective_end"]))
    navs: dict[tuple[str, str], dict[str, Any]] = {}
    epoch_nav_ids: dict[tuple[str, str, str, str | None], list[str]] = {}
    for row_index, row in enumerate(included, 1):
        start_bound = date.fromisoformat(row["effective_start"])
        end_bound = (
            None
            if row["effective_end"] is None
            else date.fromisoformat(row["effective_end"])
        )
        record_ids: list[str] = []
        for interval_start in sorted(interval_starts[row["strategy_id"]]):
            interval_end = interval_start + timedelta(days=1)
            if interval_start < start_bound or (
                end_bound is not None and interval_end > end_bound
            ):
                continue
            base = Decimal(row_index * 1000 + interval_start.day)
            factor = Decimal("1") + Decimal(row_index) / Decimal("1000")
            ending = base * factor
            third_date = interval_end + timedelta(days=1)
            points = [(interval_start, base), (interval_end, ending)]
            if end_bound is None or third_date <= end_bound:
                points.append((third_date, ending * factor))
            for observed_on, nav in points:
                record_id = f"nav:{row['account_id']}:{observed_on.isoformat()}"
                key = (row["account_id"], observed_on.isoformat())
                candidate = {
                    "record_id": record_id,
                    "account_id": row["account_id"],
                    "as_of_date": observed_on.isoformat(),
                    "nav": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": str(nav),
                    },
                }
                prior = navs.get(key)
                if prior is not None:
                    assert prior == candidate
                navs[key] = candidate
                record_ids.append(record_id)
        boundary_dates = {start_bound, end_bound or DAY}
        for observed_on in sorted(boundary_dates):
            record_id = f"nav:{row['account_id']}:{observed_on.isoformat()}"
            key = (row["account_id"], observed_on.isoformat())
            if key not in navs:
                navs[key] = {
                    "record_id": record_id,
                    "account_id": row["account_id"],
                    "as_of_date": observed_on.isoformat(),
                    "nav": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": str(Decimal(row_index * 1000 + observed_on.day)),
                    },
                }
            record_ids.append(record_id)
        epoch_nav_ids[
            (
                row["account_id"],
                row["strategy_id"],
                row["effective_start"],
                row["effective_end"],
            )
        ] = sorted(set(record_ids))

    if daily:
        # Declared synthetic daily prices, not interpolation of the sparse case.
        # The same account keeps one price path across its strategy remap.
        origin = min(date.fromisoformat(row["effective_start"]) for row in included)
        price_cycle = (0, 20, -10, 10, 0)
        navs = {}
        epoch_nav_ids = {}
        for row in included:
            observed_on = date.fromisoformat(row["effective_start"])
            end = (
                date.fromisoformat(row["effective_end"])
                if row["effective_end"]
                else DAY
            )
            account_index = int(row["account_id"].rsplit("-", 1)[1])
            base = 1000 + 100 * account_index
            record_ids = []
            while observed_on <= end:
                day_text = observed_on.isoformat()
                record_id = f"nav:{row['account_id']}:{day_text}"
                navs[(row["account_id"], day_text)] = {
                    "record_id": record_id,
                    "account_id": row["account_id"],
                    "as_of_date": day_text,
                    "nav": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": str(
                            base + price_cycle[(observed_on - origin).days % 5]
                        ),
                    },
                }
                record_ids.append(record_id)
                observed_on += timedelta(days=1)
            epoch_nav_ids[
                (
                    row["account_id"],
                    row["strategy_id"],
                    row["effective_start"],
                    row["effective_end"],
                )
            ] = record_ids

    # These fixtures generate whole synthetic account values, including cash.
    # Declare that perimeter before binding the ledger and calculation receipts.
    for observation in navs.values():
        observation["value_basis"] = "ACCOUNT_TOTAL"

    provider_ids = sorted({row["provider"] for row in specs})
    provider_generations = {
        provider: {
            "provider_id": provider.lower(),
            "generation_id": _sha(f"provider-generation:{provider}"),
            "manifest_sha256": _sha(f"provider-manifest:{provider}"),
            "generation_as_of_date": DAY.isoformat(),
        }
        for provider in provider_ids
    }
    ledger_doc = {
        "as_of_date": DAY.isoformat(),
        "catalog_schema_version": 1,
        "nav_observations": sorted(
            navs.values(), key=lambda row: (row["account_id"], row["as_of_date"])
        ),
        "flows": [],
        "capital_anchors": [],
        "provider_generations": list(provider_generations.values()),
    }
    if mutate_ledger is not None:
        mutate_ledger(ledger_doc)
    if mutate_baseline is not None:
        mutate_baseline(baseline_doc, ledger_doc)
    provider_ledger = fixture.InputSnapshot.from_bytes(
        "HWM synthetic provider ledger", canonical_json_bytes(ledger_doc)
    )
    provider_manifest = _manifest_snapshot(
        fixture,
        snapshot=provider_ledger,
        artifact_id="normalized/provider-ledger.json",
        schema_name="provider-ledger-catalog",
    )
    if "provider_coverage" in ledger_doc:
        baseline_doc["source_artifacts"] = [
            row
            for row in baseline_doc.get("source_artifacts", [])
            if row.get("source_key") != "provider-ledger-generation"
        ] + [
            {
                "artifact_id": f"synthetic/provider-ledger/{filename}",
                "as_of_date": ledger_doc["as_of_date"],
                "schema_name": schema_name,
                "schema_version": "1",
                "sha256": snapshot.sha256,
                "source_key": "provider-ledger-generation",
            }
            for filename, schema_name, snapshot in (
                ("provider-ledger.json", "provider-ledger-catalog", provider_ledger),
                ("generation.json", "generation-manifest", provider_manifest),
            )
        ]
    baseline = fixture.InputSnapshot.from_bytes(
        "HWM synthetic baseline", canonical_json_bytes(baseline_doc)
    )
    baseline_manifest = _manifest_snapshot(
        fixture,
        snapshot=baseline,
        artifact_id="normalized/portfolio-baseline.json",
        schema_name="portfolio-baseline-catalog",
    )

    receipts: dict[str, object] = {}
    receipt_authority_rows = []
    for index, row in enumerate(included, 1):
        provider_generation = provider_generations[row["provider"]]
        raw_artifact = {
            "artifact_id": f"raw/{row['provider'].lower()}.json",
            "as_of_date": DAY.isoformat(),
            "schema": {"name": "synthetic-provider-evidence", "version": "1"},
            "sha256": provider_generation["manifest_sha256"],
            "size_bytes": 1,
        }
        receipt_id = f"receipt/hwm-epoch-{index:02d}.json"
        requested_end = row["effective_end"] or DAY.isoformat()
        receipt_doc = {
            "schema_version": 2,
            "artifact_id": receipt_id,
            "issuer": {
                "adapter_name": "SYNTHETIC",
                "adapter_version": "1",
                "code_sha256": _sha("synthetic-adapter"),
            },
            "provider": row["provider"],
            "provider_generation": provider_generation["generation_id"],
            "provider_generation_manifest": raw_artifact,
            "raw_evidence_artifacts": [raw_artifact],
            "account_id": row["account_id"],
            "source_account_identity_sha256": _sha(f"source:{row['account_id']}"),
            "requested_start": row["effective_start"],
            "requested_end": requested_end,
            "covered_start": row["effective_start"],
            "covered_end": requested_end,
            "required_categories": ["CASH"],
            "completed_categories": ["CASH"],
            "unavailable_categories": [],
            "pagination": {
                "mode": "SYNTHETIC",
                "page_count": 1,
                "request_response_chain_sha256": _sha(f"chain:{index}"),
                "terminal_response_sha256": _sha(f"terminal:{index}"),
                "terminal": True,
            },
            "completeness": "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES",
            "truncated": False,
        }
        receipt_snapshot = fixture.InputSnapshot.from_bytes(
            f"HWM receipt {index}", canonical_json_bytes(receipt_doc)
        )
        receipts[receipt_id] = receipt_snapshot
        receipt_authority_rows.append(
            {
                "artifact_id": receipt_id,
                "sha256": receipt_snapshot.sha256,
                "size_bytes": receipt_snapshot.size_bytes,
                "contract": {
                    "adapter_name": "SYNTHETIC",
                    "adapter_version": "1",
                    "code_sha256": _sha("synthetic-adapter"),
                    "provider": row["provider"],
                    "account_id": row["account_id"],
                    "source_account_identity_sha256": _sha(
                        f"source:{row['account_id']}"
                    ),
                    "provider_generation": provider_generation["generation_id"],
                    "provider_generation_manifest": raw_artifact,
                    "raw_evidence_artifacts": [raw_artifact],
                    "required_categories": ["CASH"],
                    "pagination": receipt_doc["pagination"],
                },
            }
        )
    receipt_authority = fixture.InputSnapshot.from_bytes(
        "HWM receipt authority",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "artifact_id": "authority/account-interval-retrieval-receipt-authority.json",
                "as_of_date": DAY.isoformat(),
                "receipts": sorted(
                    receipt_authority_rows, key=lambda row: row["artifact_id"]
                ),
            }
        ),
    )

    assignment_records = sorted(
        (
            {
                key: assignment[key]
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            }
            for assignment in assignments
        ),
        key=lambda row: (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            str(row["effective_end"] or "9999-12-31"),
        ),
    )
    assignment_sha = _sha(
        json.dumps(assignment_records, sort_keys=True, separators=(",", ":"))
    )
    admission_rows = []
    crosswalk_rows = []
    for index, (row, assignment) in enumerate(zip(specs, assignments, strict=True), 1):
        included_row = row["status"] in _INCLUDED
        epoch_key = (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"],
        )
        admission = {
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "effective_start": row["effective_start"],
            "effective_end": row["effective_end"],
            "continuity_to_successor": row["status"] == "ASSIGNMENT_ONLY",
            "status": "FUNDED" if included_row else "BLOCKED",
        }
        if included_row:
            admission.update(
                {
                    "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "funded_nav_record_ids": epoch_nav_ids[epoch_key],
                }
            )
        else:
            admission["reason"] = "SYNTHETIC_EXCLUDED"
        admission_rows.append(admission)
        disposition = (
            "INCLUDED_PRODUCT_EPOCH"
            if included_row
            else (
                "EXCLUDED_DEPRECATED_HISTORY_ONLY"
                if row["status"] == "DEPRECATED_HISTORY_ONLY"
                else "EXCLUDED_IMMATERIAL"
            )
        )
        crosswalk_rows.append(
            {
                "assignment_id": assignment["assignment_id"],
                "account_id": row["account_id"],
                "provider": row["provider"],
                "strategy_id": row["strategy_id"],
                "effective_start": row["effective_start"],
                "effective_end": row["effective_end"],
                "account_label": f"Synthetic {row['account_id']}",
                "last_four": f"{int(row['account_id'].rsplit('-', 1)[1]):04d}",
                "product_epoch_disposition": disposition,
                "owner_reason": {
                    "INCLUDED_PRODUCT_EPOCH": "CURRENT_PRODUCT_EPOCH",
                    "EXCLUDED_DEPRECATED_HISTORY_ONLY": (
                        "ECHOTRADE_DEPRECATED_EFFECTIVE_2025_11_25"
                    ),
                    "EXCLUDED_IMMATERIAL": "COMPOSER_OWNER_EXCLUDED_IMMATERIAL",
                }[disposition],
            }
        )
    admission_policy = fixture.InputSnapshot.from_bytes(
        "HWM admission policy",
        canonical_json_bytes(
            {
                "schema_version": 2,
                "artifact_id": "authority/mapped-epoch-admission.json",
                "as_of_date": DAY.isoformat(),
                "policy_id": "MAPPED_EPOCH_ADMISSION_V2",
                "policy_version": "2",
                "assignment_sha256": assignment_sha,
                "admissions": admission_rows,
            }
        ),
    )
    crosswalk = fixture.InputSnapshot.from_bytes(
        "HWM current epoch crosswalk",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "artifact_id": "account-strategy-current-epoch-crosswalk",
                "as_of_date": DAY.isoformat(),
                "calculation_cutoff_date": DAY.isoformat(),
                "disposition_counts": {
                    "INCLUDED_PRODUCT_EPOCH": 13,
                    "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
                    "EXCLUDED_IMMATERIAL": 1,
                },
                "selected_baseline": {
                    "generation_id": GenerationManifest.from_json_bytes(
                        baseline_manifest.contents
                    ).generation_id,
                    "artifact_id": "normalized/portfolio-baseline.json",
                    "sha256": baseline.sha256,
                    "size_bytes": baseline.size_bytes,
                    "as_of_date": DAY.isoformat(),
                    "schema_name": "portfolio-baseline-catalog",
                    "schema_version": "1",
                },
                "rows": crosswalk_rows,
            }
        ),
    )
    values = fixture._selected_input_kwargs(base_selected)
    values.update(
        {
            "provider_ledger": provider_ledger,
            "provider_ledger_manifest": provider_manifest,
            "baseline": baseline,
            "baseline_manifest": baseline_manifest,
            "retrieval_receipts": receipts,
            "epoch_producer_source": fixture.InputSnapshot.from_bytes(
                "executing epoch producer",
                (REPO / "tools" / "account_strategy_manifest.py").read_bytes(),
            ),
            "generation_builder_source": fixture.InputSnapshot.from_bytes(
                "executing generation builder",
                (REPO / "tools" / "build_account_manifest_generation.py").read_bytes(),
            ),
            "independent_verifier_source": fixture.InputSnapshot.from_bytes(
                "executing independent verifier",
                (REPO / "tools" / "verify_account_manifest_generation.py").read_bytes(),
            ),
            "current_epoch_crosswalk": crosswalk,
            "receipt_authority": receipt_authority,
            "admission_policy": admission_policy,
            "manifest_schema": base_selected.manifest_schema,
        }
    )
    selected = builder.AccountManifestSelectedInputs(**values)
    provider_path = _stage_snapshot_generation(
        tmp_path,
        snapshot=provider_ledger,
        manifest_snapshot=provider_manifest,
        root_name="provider-ledger",
    )
    baseline_path = _stage_snapshot_generation(
        tmp_path,
        snapshot=baseline,
        manifest_snapshot=baseline_manifest,
        root_name="baseline",
    )
    return selected, builder, provider_path, baseline_path


def _restage_tampered_bundle(
    tmp_path: Path,
    generation_path: Path,
    mutate_docs: Callable[[dict[str, dict[str, Any]]], None],
) -> Path:
    original = read_generation_bundle(
        generation_path.parents[2], generation_path.parent.name
    )
    documents = {
        name: json.loads(payload) for name, payload in original.payloads.items()
    }
    mutate_docs(documents)
    payloads = {name: canonical_json_bytes(value) for name, value in documents.items()}
    artifacts = tuple(
        SourceArtifact(
            artifact_id=artifact.artifact_id,
            sha256=_sha(payloads[artifact.artifact_id]),
            size_bytes=len(payloads[artifact.artifact_id]),
            as_of_date=artifact.as_of_date,
            schema_name=artifact.schema_name,
            schema_version=artifact.schema_version,
        )
        for artifact in original.manifest.artifacts
    )
    manifest = GenerationManifest.build(
        as_of_date=original.manifest.as_of_date,
        adapter=original.manifest.adapter,
        coverage=original.manifest.coverage,
        artifacts=artifacts,
        verification=original.manifest.verification,
    )
    final = stage_generation_bundle(
        tmp_path / "tampered-account-manifest", manifest, payloads
    )
    return final / "generation.json"


def _fixture(
    tmp_path: Path,
    mutate_docs: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    mutate_policy: Callable[[dict[str, Any]], None] | None = None,
    mutate_ledger: Callable[[dict[str, Any]], None] | None = None,
    *,
    base_contract_path: Path | None = None,
    mutate_baseline: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    daily: bool = False,
    bind_epoch_contract: bool = True,
) -> dict[str, Path]:
    original_contract_bytes = (
        base_contract_path
        or REPO / "contracts" / "portfolio-financial-mvp-source-v1.json"
    ).read_bytes()
    contract = json.loads(original_contract_bytes)
    selected, builder, provider_path, baseline_path = _selected_inputs(
        tmp_path,
        contract,
        mutate_ledger=mutate_ledger,
        mutate_baseline=mutate_baseline,
        daily=daily,
    )
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "account-strategy-admission-policy.json").write_bytes(
        selected.admission_policy.contents
    )
    (roles / "account-strategy-flow-classification-policy.json").write_bytes(
        selected.flow_classification_policy.contents
    )
    account_bundle = builder.stage_account_manifest_generation(
        selected, tmp_path / "account-manifest"
    )
    generation_path = (
        tmp_path
        / "account-manifest"
        / "generations"
        / account_bundle.manifest.generation_id
        / "generation.json"
    )
    selected_account_bundle = read_generation_bundle(
        generation_path.parents[2], generation_path.parent.name
    )
    if bind_epoch_contract:
        contract["contract_schema_version"] = 7
        contract["epoch_manifest_source"] = (
            financial_contract.epoch_manifest_source_from_bundle(
                selected_account_bundle
            )
        )
    contract_bytes = canonical_json_bytes(contract)
    contract_path = tmp_path / "financial-contract.json"
    contract_path.write_bytes(contract_bytes)
    if mutate_docs is not None:
        generation_path = _restage_tampered_bundle(
            tmp_path, generation_path, mutate_docs
        )
    policy = json.loads(
        (REPO / "contracts" / "portfolio-hwm-policy-v4.json").read_text()
    )
    policy["product_authority"]["raw_sha256"] = _sha(contract_bytes)
    policy["product_authority"]["semantic_digest"] = financial_contract.contract_digest(
        contract
    )
    if mutate_policy is not None:
        mutate_policy(policy)
    policy_path = tmp_path / "hwm-policy.json"
    policy_bytes = canonical_json_bytes(policy)
    policy_path.write_bytes(policy_bytes)

    account_output = json.loads(
        account_bundle.payloads["outputs/account-strategy-manifest.json"]
    )
    strategies = sorted({row["strategy_id"] for row in account_output["rows"]})
    accepted_intervals = []
    source_calendars = []
    for strategy_id in strategies:
        calendar_id = f"{strategy_id}:SYNTHETIC-OBSERVED"
        source_calendars.append(
            {
                "strategy_id": strategy_id,
                "calendar_id": calendar_id,
                "calendar_kind": "DECLARED_SESSION",
                "calendar_evidence_sha256": _sha(f"calendar:{strategy_id}"),
            }
        )
        values: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in account_output["rows"]:
            if row["strategy_id"] != strategy_id:
                continue
            for link in row["observation_trace"]["links"]:
                if (
                    date.fromisoformat(link["end_date"])
                    - date.fromisoformat(link["start_date"])
                ).days > 1:
                    key = (link["start_date"], link["end_date"])
                    values.setdefault(key, []).append(link)
        for (start_date, end_date), links in sorted(values.items()):
            beginning = sum((Decimal(link["base_nav"]) for link in links), Decimal("0"))
            weighted = sum(
                (
                    Fraction(Decimal(link["base_nav"]))
                    * (Fraction(Decimal(link["factor"])) - 1)
                    for link in links
                ),
                Fraction(0, 1),
            ) / Fraction(beginning)
            with localcontext() as context:
                context.prec = 34
                context.rounding = ROUND_HALF_EVEN
                value = str(Decimal(weighted.numerator) / Decimal(weighted.denominator))
            accepted_intervals.append(
                {
                    "strategy_id": strategy_id,
                    "calendar_id": calendar_id,
                    "prior_observation_date": start_date,
                    "next_observation_date": end_date,
                    "binding_return_date": end_date,
                    "binding_return_value": value,
                    "interval_evidence_sha256": hashlib.sha256(
                        "\n".join(
                            [strategy_id, calendar_id, start_date, end_date, value]
                        ).encode()
                    ).hexdigest(),
                    "treatment": "ACCEPTED_MULTI_DAY_RETURN_BINDING",
                }
            )
    calendar = {
        "schema_version": 2,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": policy["calendar_policy"][
            "accepted_governed_calendar_policy_ids"
        ][0],
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "synthetic accepted mixed-calendar authority",
        "hwm_policy_sha256": _sha(policy_bytes),
        "source_calendars": source_calendars,
        "closed_source_dates": [],
        "accepted_source_intervals": accepted_intervals,
    }
    calendar["authority_evidence_sha256"] = _sha(
        json.dumps(calendar, sort_keys=True, separators=(",", ":"))
    )
    calendar_path = tmp_path / "calendar-authority.json"
    calendar_path.write_bytes(canonical_json_bytes(calendar))

    provider_manifest_sha = _sha(provider_path.read_bytes())
    provider_generation_id = provider_path.parent.name
    receipt = {
        "admission_gate_status": "PASS",
        "contract_raw_sha256": _sha(contract_bytes),
        "contract_semantic_sha256": financial_contract.contract_digest(contract),
        "provider_ledger_generation_id": provider_generation_id,
        "provider_ledger_manifest_sha256": provider_manifest_sha,
    }
    receipt_path = tmp_path / "provider-admission.json"
    receipt_path.write_bytes(canonical_json_bytes(receipt))
    return {
        "generation": generation_path,
        "admission_policy": roles / "account-strategy-admission-policy.json",
        "flow_classification_policy": (
            roles / "account-strategy-flow-classification-policy.json"
        ),
        "provider_generation": provider_path,
        "baseline_generation": baseline_path,
        "contract": contract_path,
        "calendar": calendar_path,
        "receipt": receipt_path,
        "policy": policy_path,
    }


def _build(paths: dict[str, Path]) -> dict[str, Any]:
    return build_selected_hwm_inputs(
        account_manifest_generation_path=paths["generation"],
        account_manifest_admission_policy_path=paths["admission_policy"],
        account_manifest_flow_classification_policy_path=paths[
            "flow_classification_policy"
        ],
        provider_ledger_generation_path=paths["provider_generation"],
        baseline_generation_path=paths["baseline_generation"],
        financial_contract_path=paths["contract"],
        calendar_authority_path=paths["calendar"],
        provider_admission_path=paths["receipt"],
        hwm_policy_path=paths["policy"],
    )


def test_derives_current_calendar_return_from_verified_selected_rows(
    tmp_path: Path,
) -> None:
    """A stale accepted binding cannot override the selected source return."""

    paths = _fixture(tmp_path)
    calendar = json.loads(paths["calendar"].read_bytes())
    original_values = {
        (
            row["strategy_id"],
            row["prior_observation_date"],
            row["next_observation_date"],
        ): row["binding_return_value"]
        for row in calendar["accepted_source_intervals"]
    }
    changed_key = next(iter(original_values))
    for row in calendar["accepted_source_intervals"]:
        if (
            row["strategy_id"],
            row["prior_observation_date"],
            row["next_observation_date"],
        ) == changed_key:
            row["binding_return_value"] = "0.123456789"
            row["interval_evidence_sha256"] = hashlib.sha256(
                "\n".join(
                    [
                        row["strategy_id"],
                        row["calendar_id"],
                        row["prior_observation_date"],
                        row["next_observation_date"],
                        row["binding_return_value"],
                    ]
                ).encode()
            ).hexdigest()
            break
    calendar.pop("authority_evidence_sha256")
    calendar["authority_evidence_sha256"] = _sha(
        json.dumps(calendar, sort_keys=True, separators=(",", ":"))
    )
    paths["calendar"].write_bytes(canonical_json_bytes(calendar))

    with pytest.raises(SelectedHwmInputError, match="accepted interval binding"):
        _build(paths)

    derived = selected_hwm.derive_selected_hwm_calendar_authority(
        account_manifest_generation_path=paths["generation"],
        account_manifest_admission_policy_path=paths["admission_policy"],
        account_manifest_flow_classification_policy_path=paths[
            "flow_classification_policy"
        ],
        provider_ledger_generation_path=paths["provider_generation"],
        baseline_generation_path=paths["baseline_generation"],
        financial_contract_path=paths["contract"],
        calendar_authority_path=paths["calendar"],
        provider_admission_path=paths["receipt"],
        hwm_policy_path=paths["policy"],
    )
    derived_values = {
        (
            row["strategy_id"],
            row["prior_observation_date"],
            row["next_observation_date"],
        ): row["binding_return_value"]
        for row in derived["accepted_source_intervals"]
    }
    assert set(derived_values) == set(original_values)
    assert derived_values[changed_key] == original_values[changed_key]
    assert [
        key for key in derived_values if derived_values[key] != original_values[key]
    ] == []
    assert {
        row["strategy_id"]: row["calendar_kind"] for row in derived["source_calendars"]
    } == {
        row["strategy_id"]: row["calendar_kind"] for row in calendar["source_calendars"]
    }

    derived_path = tmp_path / "current-selected-calendar.json"
    derived_path.write_bytes(canonical_json_bytes(derived))
    paths["calendar"] = derived_path
    assert _build(paths)["schema_version"] == 1


def test_rejects_selected_source_gap_without_accepted_topology(tmp_path: Path) -> None:
    """A selected gap cannot become a closure or binding without prior acceptance."""

    paths = _fixture(tmp_path)
    calendar = json.loads(paths["calendar"].read_bytes())
    calendar["accepted_source_intervals"].pop()
    calendar.pop("authority_evidence_sha256")
    calendar["authority_evidence_sha256"] = _sha(
        json.dumps(calendar, sort_keys=True, separators=(",", ":"))
    )
    paths["calendar"].write_bytes(canonical_json_bytes(calendar))

    with pytest.raises(SelectedHwmInputError, match="unapproved calendar gap"):
        selected_hwm.derive_selected_hwm_calendar_authority(
            account_manifest_generation_path=paths["generation"],
            account_manifest_admission_policy_path=paths["admission_policy"],
            account_manifest_flow_classification_policy_path=paths[
                "flow_classification_policy"
            ],
            provider_ledger_generation_path=paths["provider_generation"],
            baseline_generation_path=paths["baseline_generation"],
            financial_contract_path=paths["contract"],
            calendar_authority_path=paths["calendar"],
            provider_admission_path=paths["receipt"],
            hwm_policy_path=paths["policy"],
        )


def test_builds_source_bound_seven_strategy_inputs_from_exact_thirteen_epochs(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    result = _build(paths)

    assert result["schema_version"] == 1
    assert result["builder"] == "portfolio-hwm-inputs-governed"
    assert len(result["memberships"]) == 7
    assert {row["strategy_id"] for row in result["memberships"]} == {
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    }
    source_generation = _sha(paths["provider_generation"].read_bytes())
    assert result["component_periods"]
    assert {row["source_generation_sha256"] for row in result["component_periods"]} == {
        source_generation
    }
    assert source_generation in result["evidence_sha256s"]
    assert all(
        row["return_basis"] == AS_HELD_HWM_RETURN_BASIS
        for row in result["component_periods"]
    )

    first_tt = next(
        row
        for row in result["component_periods"]
        if row["strategy_id"] == "tt-portfolio"
        and row["interval_start"] == "2025-11-25"
    )
    account_bundle = read_generation_bundle(
        paths["generation"].parents[2], paths["generation"].parent.name
    )
    manifest_output = json.loads(
        account_bundle.payloads["outputs/account-strategy-manifest.json"]
    )
    tt_links = []
    for row in manifest_output["rows"]:
        if row["strategy_id"] != "tt-portfolio":
            continue
        tt_links.extend(
            link
            for link in row["observation_trace"]["links"]
            if link["start_date"] == "2025-11-25"
        )
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        expected = sum(
            Decimal(link["base_nav"]) * (Decimal(link["factor"]) - 1)
            for link in tt_links
        ) / sum(Decimal(link["base_nav"]) for link in tt_links)
    assert Decimal(first_tt["net_return"]) == expected
    assert Decimal(first_tt["beginning_capital_usd"]) == sum(
        Decimal(link["base_nav"]) for link in tt_links
    )
    transition_tt = next(
        row
        for row in result["component_periods"]
        if row["strategy_id"] == "tt-portfolio"
        and row["interval_start"] == "2025-12-29"
    )
    transition_links = []
    for row in manifest_output["rows"]:
        if row["strategy_id"] != "tt-portfolio":
            continue
        transition_links.extend(
            link
            for link in row["observation_trace"]["links"]
            if link["start_date"] == "2025-12-29"
        )
    assert len(transition_links) == 6
    assert Decimal(transition_tt["beginning_capital_usd"]) == sum(
        Decimal(link["base_nav"]) for link in transition_links
    )
    assert not any(
        row["strategy_id"] == "qsi" and row["interval_start"] < "2026-01-05"
        for row in result["component_periods"]
    )


def test_rejects_output_return_not_recomputed_from_source_nav(tmp_path: Path) -> None:
    def mutate(docs: dict[str, dict[str, Any]]) -> None:
        docs["outputs/account-strategy-manifest.json"]["rows"][0]["observation_trace"][
            "links"
        ][0]["factor"] = "9"

    with pytest.raises(SelectedHwmInputError, match="epoch manifest|independent"):
        _build(_fixture(tmp_path, mutate_docs=mutate))


def test_rejects_altered_source_nav_even_when_generation_is_rehashed(
    tmp_path: Path,
) -> None:
    def mutate(docs: dict[str, dict[str, Any]]) -> None:
        docs["authority/calculation-inputs.json"]["rows"][0]["nav_observations"][0][
            "nav"
        ]["value"] = "999999"

    with pytest.raises(SelectedHwmInputError, match="epoch manifest|independent"):
        _build(_fixture(tmp_path, mutate_docs=mutate))


def test_rejects_non_native_usd_without_exact_accepted_fx() -> None:
    contract = json.loads(
        (REPO / "contracts" / "portfolio-financial-mvp-source-v1.json").read_bytes()
    )

    docs = _docs(contract)
    calculation = docs["authority/calculation-inputs.json"]["rows"][0]
    output = docs["outputs/account-strategy-manifest.json"]["rows"][0]
    epoch = calculation["epoch"]
    key = tuple(
        epoch[name]
        for name in ("account_id", "strategy_id", "effective_start", "effective_end")
    )
    calculation["nav_observations"][0]["nav"]["currency"] = "USDT"
    with pytest.raises(SelectedHwmInputError, match="source-native USD"):
        _validated_account_links(key, calculation, output)


def _stablecoin_anchor_epoch() -> tuple[
    tuple[str, str, str, str | None], dict[str, Any], dict[str, Any]
]:
    contract = json.loads(
        (REPO / "contracts" / "portfolio-financial-mvp-source-v1.json").read_bytes()
    )
    docs = _docs(contract)
    calculation = docs["authority/calculation-inputs.json"]["rows"][0]
    output = docs["outputs/account-strategy-manifest.json"]["rows"][0]
    epoch = calculation["epoch"]
    key = tuple(
        epoch[name]
        for name in ("account_id", "strategy_id", "effective_start", "effective_end")
    )
    observed = sorted(row["as_of_date"] for row in calculation["nav_observations"])
    calculation["capital_anchors"] = [
        {
            "account_id": epoch["account_id"],
            "amount": {
                "currency": "USDT",
                "status": "AVAILABLE",
                "value": "49891.55",
            },
            "capital_type": "OPENING_CAPITAL_ANCHOR",
            "event_date": observed[0],
            "nav_recognition_date": observed[0],
            "record_id": "stablecoin-subscription",
        }
    ]
    return key, calculation, output


def test_opening_anchor_may_carry_a_source_native_stablecoin_amount() -> None:
    """An opening anchor contributes no flow, so its native amount is unread.

    Matrixport subscribes in USDT and the epoch's beginning capital comes from
    the NAV series. Demanding USD of a value this branch discards rejected a
    fully evidenced epoch over an unused field.
    """
    key, calculation, output = _stablecoin_anchor_epoch()
    _links, _navs, flows, _evidence = selected_hwm._validated_account_sources(
        key, calculation, output
    )
    assert not flows


def test_allocation_anchor_still_requires_source_native_usd() -> None:
    key, calculation, output = _stablecoin_anchor_epoch()
    calculation["capital_anchors"][0]["capital_type"] = "STRATEGY_CAPITAL_ALLOCATION"
    with pytest.raises(SelectedHwmInputError, match="source-native USD"):
        selected_hwm._validated_account_sources(key, calculation, output)


def _zero_nav_strategy_rows() -> tuple[
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    dict[tuple[str, str, str, str | None], dict[str, Any]],
]:
    strategy_id = "tt-zero-sleeve-shape"
    start = "2026-07-13"
    middle = "2026-07-14"
    end = "2026-07-15"
    keys = [
        ("account-positive", strategy_id, start, None),
        ("account-zero-sleeve", strategy_id, start, None),
    ]

    def nav(account_id: str, observed_on: str, value: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "as_of_date": observed_on,
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": value},
        }

    def link(
        interval_start: str,
        interval_end: str,
        base: str,
        ending: str,
        flow: str,
    ) -> dict[str, str]:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            factor = (Decimal(ending) - Decimal(flow)) / Decimal(base)
        return {
            "start_date": interval_start,
            "end_date": interval_end,
            "base_nav": base,
            "beginning_flow": "0",
            "ending_nav": ending,
            "ending_flow": flow,
            "net_flow": flow,
            "factor": str(factor),
        }

    crosswalk = {key: {"assignment_id": f"assignment-{key[0]}"} for key in keys}
    calculation = {
        keys[0]: {
            "nav_observations": [
                nav(keys[0][0], start, "100"),
                nav(keys[0][0], middle, "105"),
                nav(keys[0][0], end, "110"),
            ],
            "capital_anchors": [],
            "classified_flows": [],
        },
        keys[1]: {
            "nav_observations": [
                nav(keys[1][0], start, "50"),
                nav(keys[1][0], middle, "0"),
                nav(keys[1][0], end, "10"),
            ],
            "capital_anchors": [],
            "classified_flows": [
                {
                    "account_id": keys[1][0],
                    "event_id": "flow-liquidation",
                    "classification_status": "CLASSIFIED",
                    "disposition": "EXTERNAL",
                    "event_date": middle,
                    "nav_recognition_date": middle,
                    "amount": {
                        "currency": "USD",
                        "status": "AVAILABLE",
                        "value": "-50",
                    },
                }
            ],
        },
    }
    output = {
        keys[0]: {
            "observation_trace": {
                "links": [
                    link(start, middle, "100", "105", "0"),
                    link(middle, end, "105", "110", "0"),
                ]
            }
        },
        keys[1]: {
            "observation_trace": {"links": [link(start, end, "50", "10", "-50")]}
        },
    }
    return crosswalk, calculation, output


def _derive_zero_nav_strategy_rows(
    crosswalk: dict[tuple[str, str, str, str | None], dict[str, Any]],
    calculation: dict[tuple[str, str, str, str | None], dict[str, Any]],
    output: dict[tuple[str, str, str, str | None], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return selected_hwm._derive_builder_rows(
        crosswalk,
        calculation,
        output,
        account_generation_id="account-generation",
        source_generation_id="provider-generation",
        source_generation_sha256="1" * 64,
        artifact_sha256s={"authority/calculation-inputs.json": "2" * 64},
    )


def test_schema_v6_product_authority_keeps_exact_epoch_bundle_separate(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path, bind_epoch_contract=False)

    result = _build(paths)

    assert len(result["memberships"]) == 7
    assert paths["generation"].parent.name in result["evidence_sha256s"]


def test_schema_v6_product_authority_rejects_wrong_epoch_crosswalk(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path, bind_epoch_contract=False)

    def mutate(docs: dict[str, dict[str, Any]]) -> None:
        docs["authority/account-assignment-crosswalk.json"]["rows"].pop()

    paths["generation"] = _restage_tampered_bundle(
        tmp_path, paths["generation"], mutate
    )

    with pytest.raises(SelectedHwmInputError, match="epoch manifest|independent"):
        _build(paths)


def test_contract_required_strategy_set_must_equal_selected_epoch_set() -> None:
    contract = {
        "strategies": [
            {"strategy_id": "required-a", "product_requirement": "REQUIRED"},
            {"strategy_id": "required-b", "product_requirement": "REQUIRED"},
            {"strategy_id": "optional-c", "product_requirement": "OPTIONAL"},
        ]
    }
    exact = {
        ("account-a", "required-a", "2026-01-01", None): {},
        ("account-b", "required-b", "2026-01-01", None): {},
    }
    selected_hwm._validate_contract_epoch_strategy_perimeter(contract, exact)

    substituted = dict(exact)
    substituted.pop(("account-b", "required-b", "2026-01-01", None))
    substituted[("account-c", "optional-c", "2026-01-01", None)] = {}
    with pytest.raises(SelectedHwmInputError, match="strategy perimeter"):
        selected_hwm._validate_contract_epoch_strategy_perimeter(contract, substituted)


def test_aggregates_native_daily_nav_before_division_for_zero_value_sleeve() -> None:
    crosswalk, calculation, output = _zero_nav_strategy_rows()

    _memberships, capital, returns = _derive_zero_nav_strategy_rows(
        crosswalk, calculation, output
    )

    assert [(row["interval_start"], row["interval_end"]) for row in returns] == [
        ("2026-07-13", "2026-07-14"),
        ("2026-07-14", "2026-07-15"),
    ]
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        assert Decimal(returns[0]["net_return"]) == Decimal(5) / Decimal(150)
        assert Decimal(returns[1]["net_return"]) == Decimal(15) / Decimal(105)
    assert [Decimal(row["closing_capital_usd"]) for row in capital] == [
        Decimal("150"),
        Decimal("105"),
        Decimal("120"),
    ]


def test_rejects_daily_strategy_interval_with_missing_member_nav_boundary() -> None:
    crosswalk, calculation, output = _zero_nav_strategy_rows()
    zero_key = next(key for key in calculation if key[0] == "account-zero-sleeve")
    calculation[zero_key]["nav_observations"] = [
        row
        for row in calculation[zero_key]["nav_observations"]
        if row["as_of_date"] != "2026-07-14"
    ]

    with pytest.raises(SelectedHwmInputError, match="native NAV boundary"):
        _derive_zero_nav_strategy_rows(crosswalk, calculation, output)


def test_rejects_conflicting_classified_flow_identity() -> None:
    crosswalk, calculation, output = _zero_nav_strategy_rows()
    zero_key = next(key for key in calculation if key[0] == "account-zero-sleeve")
    conflicting = json.loads(json.dumps(calculation[zero_key]["classified_flows"][0]))
    conflicting["amount"]["value"] = "-49"
    calculation[zero_key]["classified_flows"].append(conflicting)

    with pytest.raises(SelectedHwmInputError, match="classified flows conflict"):
        _derive_zero_nav_strategy_rows(crosswalk, calculation, output)


def test_accepts_canonical_nonterminating_nav_ratio(tmp_path: Path) -> None:
    def mutate(ledger: dict[str, Any]) -> None:
        account_id = ledger["nav_observations"][0]["account_id"]
        account_navs = [
            row for row in ledger["nav_observations"] if row["account_id"] == account_id
        ]
        assert len(account_navs) >= 2
        account_navs[0]["nav"]["value"] = "103"
        account_navs[1]["nav"]["value"] = "110"

    result = _build(_fixture(tmp_path, mutate_ledger=mutate))
    assert result["component_periods"]


def test_rejects_provider_admission_bound_to_another_manifest(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    receipt = json.loads(paths["receipt"].read_bytes())
    receipt["provider_ledger_manifest_sha256"] = "0" * 64
    paths["receipt"].write_bytes(canonical_json_bytes(receipt))

    with pytest.raises(SelectedHwmInputError, match="provider-ledger manifest"):
        _build(paths)


def test_rejects_flow_policy_bytes_that_differ_from_selected_identity(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    policy = paths["flow_classification_policy"]
    policy.write_bytes(policy.read_bytes() + b"\n")

    with pytest.raises(SelectedHwmInputError, match="classification policy identity"):
        _build(paths)


def test_rejects_policy_return_basis_drift(tmp_path: Path) -> None:
    def mutate(policy: dict[str, Any]) -> None:
        policy["calculation"]["return_basis"] = "NAV_RETURN_WITH_UNPROVEN_BASIS"

    with pytest.raises(SelectedHwmInputError, match="calculation|return basis"):
        _build(_fixture(tmp_path, mutate_policy=mutate))


def _ordinary_source(
    tmp_path: Path, field: str, *, bundle_report: bool = False
) -> tuple[Path, GenerationManifest, dict[str, bytes]]:
    provider = field == "provider-ledger"
    catalog_name = "provider-ledger.json" if provider else "portfolio-baseline.json"
    payloads = {f"normalized/{catalog_name}": canonical_json_bytes({"rows": []})}
    artifacts = [
        _artifact(
            f"normalized/{catalog_name}",
            payloads[f"normalized/{catalog_name}"],
            "provider-ledger-catalog" if provider else "portfolio-baseline-catalog",
            "2" if provider else "1",
        )
    ]
    if provider:
        receipt_id = "normalized/admit-or-declare-receipt.json"
        payloads[receipt_id] = canonical_json_bytes(
            {
                "schema_version": 1,
                "receipt_type": "PROVIDER_ADMIT_OR_DECLARE_V1",
                "admission_gate_status": "PASS",
                "contract_raw_sha256": "1" * 64,
                "contract_semantic_sha256": "2" * 64,
            }
        )
        artifacts.append(
            _artifact(
                receipt_id, payloads[receipt_id], "provider-admission-receipt", "1"
            )
        )
    if bundle_report:
        artifacts.append(
            _artifact(
                "verification/independent-verification.json",
                b"{}",
                "synthetic-independent-report",
                "1",
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=DAY,
        adapter=AdapterIdentity(
            "quant-engine-provider-ledger"
            if provider
            else "portfolio-baseline-builder",
            "5" if provider else "1.2.0",
        ),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic source layout regression",
            fact_types=("synthetic",),
            observed_start=DAY,
            observed_end=DAY,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    directory = tmp_path / field / "generations" / manifest.generation_id
    directory.mkdir(parents=True)
    generation_path = directory / "generation.json"
    generation_path.write_bytes(manifest.to_json_bytes())
    for artifact_id, payload in payloads.items():
        (directory / Path(artifact_id).name).write_bytes(payload)
    return generation_path, manifest, payloads


@pytest.mark.parametrize("field", ["provider-ledger", "baseline"])
def test_reads_ordinary_source_producer_layouts(tmp_path: Path, field: str) -> None:
    path, manifest, payloads = _ordinary_source(tmp_path, field)
    actual_manifest, actual_payloads, actual_bytes = (
        selected_hwm._source_generation_from_path(path, field)
    )
    assert actual_manifest == manifest
    assert actual_payloads == payloads
    assert actual_bytes == manifest.to_json_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path limit")
def test_reads_ordinary_source_child_past_windows_path_limit(tmp_path: Path) -> None:
    parent = tmp_path / ("long-source-" + "x" * 80)
    extended_parent = Path("\\\\?\\" + str(parent.absolute()))
    path, manifest, payloads = _ordinary_source(extended_parent, "provider-ledger")
    ordinary_path = Path(str(path)[4:])
    assert len(str(ordinary_path)) > 260
    actual_manifest, actual_payloads, _ = selected_hwm._source_generation_from_path(
        ordinary_path, "provider-ledger"
    )
    assert actual_manifest == manifest
    assert actual_payloads == payloads


@pytest.mark.parametrize("field", ["provider-ledger", "baseline"])
@pytest.mark.parametrize("tamper", ["catalog", "manifest", "extra", "partial_bundle"])
def test_rejects_ordinary_source_integrity_drift(
    tmp_path: Path, field: str, tamper: str
) -> None:
    path, _, payloads = _ordinary_source(tmp_path, field)
    if tamper == "catalog":
        catalog = path.parent / Path(next(iter(payloads))).name
        catalog.write_bytes(catalog.read_bytes() + b"\n")
    elif tamper == "manifest":
        path.write_bytes(path.read_bytes() + b"\n")
    elif tamper == "extra":
        (path.parent / "unselected.json").write_bytes(b"{}")
    else:
        (path.parent / "generation-inventory.json").write_bytes(b"{}")
    with pytest.raises((SelectedHwmInputError, GenerationConflictError)):
        selected_hwm._source_generation_from_path(path, field)


def test_rejects_bundle_with_all_sidecars_removed(tmp_path: Path) -> None:
    path, _, _ = _ordinary_source(tmp_path, "provider-ledger", bundle_report=True)
    with pytest.raises(SelectedHwmInputError, match="bundle sidecars"):
        selected_hwm._source_generation_from_path(path, "provider-ledger")


def test_accepts_raw_admission_receipt_bound_by_selected_source_manifest(
    tmp_path: Path,
) -> None:
    path, manifest, payloads = _ordinary_source(tmp_path, "provider-ledger")
    receipt_bytes = payloads["normalized/admit-or-declare-receipt.json"]
    receipt = json.loads(receipt_bytes)
    assert "provider_ledger_generation_id" not in receipt
    assert "provider_ledger_manifest_sha256" not in receipt
    selected_hwm._validate_provider_admission_binding(
        receipt_bytes,
        receipt,
        manifest,
        _sha(path.read_bytes()),
    )


def test_rejects_substituted_raw_admission_receipt(tmp_path: Path) -> None:
    path, manifest, payloads = _ordinary_source(tmp_path, "provider-ledger")
    receipt_bytes = payloads["normalized/admit-or-declare-receipt.json"] + b"\n"
    with pytest.raises(SelectedHwmInputError, match="receipt identity"):
        selected_hwm._validate_provider_admission_binding(
            receipt_bytes,
            json.loads(receipt_bytes),
            manifest,
            _sha(path.read_bytes()),
        )


def test_rejects_mutated_ordinary_admission_payload(tmp_path: Path) -> None:
    path, _, _ = _ordinary_source(tmp_path, "provider-ledger")
    receipt = path.parent / "admit-or-declare-receipt.json"
    receipt.write_bytes(receipt.read_bytes() + b"\n")
    with pytest.raises(SelectedHwmInputError, match="artifact identity"):
        selected_hwm._source_generation_from_path(path, "provider-ledger")

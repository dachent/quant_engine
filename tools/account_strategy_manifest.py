"""Account × strategy epoch manifest — governed accounting engine.

Owner-mandated epoch continuity rule (HWM-003, #324):
    When strategy A ends and strategy B begins on the same account, the
    closing NAV of A becomes the opening invested capital of B. Capital
    is continuous across epochs; nothing appears or disappears.

Columns (owner-approved clean investor view):
    Account | Strategy | Started | Ended | Invested | Withdrawn |
    Net capital | Current | Gain | RONC% | Ann RONC% | Obs return% | Obs HWM |
    Obs MaxDD%

Definitions (owner-approved legend):
    Invested   = explicit opening capital + deposits + internal-in after start.
                 First-funded-day flows are opening basis only when predecessor
                 continuity proves them; otherwise the row is blocked.
    Withdrawn  = external withdrawals + internal-out after start
    Net capital= Invested - Withdrawn (still at work)
    Current    = last funded NAV in the epoch window
    Gain       = Current - Net capital (money identity, exact to the cent)
    RONC       = Gain / Net capital (not a money-weighted return)
    Ann RONC%  = annualized simple return-on-net-capital diagnostic
    Obs return = return linked only across observed NAV intervals, using the
                 date-only end-of-observation-interval flow convention
    Obs HWM/DD = running max / worst drawdown of that observed-return index;
                 neither field represents daily valuation performance

The producer never labels Gain / Net capital as money-weighted return.  XIRR
is emitted only as a diagnostic while a timing-sensitive return policy remains
unavailable.

Flow classification is consumed from a separately governed, ledger-bound
artifact and reconciled one-to-one against every raw ledger flow. The manifest
producer does not guess a class from a broker label or silently omit a row.

Usage:
    python tools/account_strategy_manifest.py \
        --ledger-json <provider-ledger.json> \
        [--baseline-root <dataPortfolio>] [--format table|json]
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Context, Decimal, getcontext, localcontext
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import (
    ManifestValidationError,
    SourceArtifact,
    canonical_json_bytes,
)

_DECIMAL_CONTEXT = Context(prec=40)


def _isolated_decimal_context(function: Any) -> Any:
    """Run numeric work deterministically without mutating caller process state."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        with localcontext(_DECIMAL_CONTEXT):
            return function(*args, **kwargs)

    return run


class ManifestError(RuntimeError):
    """Raised when inputs violate the manifest contract (fail-closed)."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MANIFEST_SCHEMA_VERSION = 7
_FLOW_CLASSES_SCHEMA_VERSION = 3
_CURRENT_MANIFEST_SCHEMA_VERSION = 9
_CURRENT_FLOW_CLASSES_SCHEMA_VERSION = 4
_FLOW_CLASSIFICATION_POLICY_SCHEMA_VERSION = 1
_FLOW_COVERAGE_SCHEMA_VERSION = 2
_CENT = Decimal("0.01")
_ADMISSION_STATUSES = frozenset({"FUNDED", "BLOCKED"})
_ADMISSION_AUTHORITY_KINDS = frozenset(
    {"LEDGER_AVAILABLE_POSITIVE_NAV_V1", "NONE"}
)
_FLOW_DISPOSITIONS = frozenset({"EXTERNAL", "INTERNAL"})
_FLOW_COVERAGE_STATUSES = frozenset({"COMPLETE", "BLOCKED"})
_FLOW_COVERAGE_AUTHORITY_KINDS = frozenset(
    {
        "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
        "STATIC_SNAPSHOT_COVERAGE_V1",
        "NONE",
    }
)
_FLOW_COVERAGE_BLOCKERS = frozenset(
    {
        "NO_ADMISSIBLE_V2_RECEIPT",
        "ACCOUNT_BINDING_MISMATCH",
        "INTERVAL_BOUNDARY_MISMATCH",
        "CATEGORY_SET_INCOMPLETE",
        "PAGINATION_NOT_TERMINAL",
        "SOURCE_IDENTITY_MISMATCH",
        "RECEIPT_OVERLAP",
        "RECEIPT_GAP",
        "LEGACY_RECEIPT_UNVERIFIABLE",
    }
)

_RECEIPT_AUTHORITY_ARTIFACT_ID = (
    "authority/account-interval-retrieval-receipt-authority.json"
)
_RECEIPT_AUTHORITY_SCHEMA_NAME = "account-interval-retrieval-receipt-authority"
_RECEIPT_AUTHORITY_SCHEMA_VERSION = 1


# Retained receipt contracts are lexically owned by the closed coverage loader.
# Within the supported API boundary, no caller, test, CLI argument,
# module-global reassignment, or input artifact may extend that registry.
_ASSIGNMENT_DISPOSITIONS = frozenset(
    {
        "STRATEGY_MAPPED",
        "HISTORICAL_POOL_ONLY",
        "RESERVE_NOT_APPLICABLE",
    }
)


def _require_nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _generation_id(value: object, field: str) -> str:
    text = _require_nonempty_text(value, field)
    if _SHA256.fullmatch(text) is None:
        raise ManifestError(f"{field} must be a lowercase SHA-256 generation id")
    return text


def _iso_date(value: object, field: str) -> str:
    text = _require_nonempty_text(value, field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ManifestError(f"{field} must be a canonical ISO date") from exc
    if parsed.isoformat() != text:
        raise ManifestError(f"{field} must be a canonical ISO date")
    return text


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_artifact_from_snapshot(
    snapshot: InputSnapshot,
    *,
    artifact_id: object,
    as_of_date: object,
    schema_name: str,
    schema_version: str,
) -> dict[str, object]:
    try:
        return SourceArtifact(
            artifact_id=_require_nonempty_text(artifact_id, "artifact_id"),
            as_of_date=date.fromisoformat(_iso_date(as_of_date, "as_of_date")),
            schema_name=schema_name,
            schema_version=schema_version,
            sha256=hashlib.sha256(snapshot.contents).hexdigest(),
            size_bytes=len(snapshot.contents),
        ).to_dict()
    except (ManifestValidationError, ValueError) as exc:
        raise ManifestError(
            f"invalid source artifact for {snapshot.label}"
        ) from exc


def _verified_source_identity(
    source_snapshot: InputSnapshot, payload: Mapping[str, Any], label: str
) -> dict[str, object]:
    if set(payload) != {"schema_version", "generation_id", "artifact"}:
        raise ManifestError(f"{label} identity keys are invalid")
    if payload.get("schema_version") != 1:
        raise ManifestError(f"{label} identity schema_version must be 1")
    generation_id = _generation_id(
        payload.get("generation_id"), f"{label}.generation_id"
    )
    artifact_payload = payload.get("artifact")
    if not isinstance(artifact_payload, Mapping):
        raise ManifestError(f"{label}.artifact must be an object")
    try:
        artifact = SourceArtifact.from_dict(artifact_payload)
    except ManifestValidationError as exc:
        raise ManifestError(f"{label}.artifact is invalid") from exc
    actual_sha256 = hashlib.sha256(source_snapshot.contents).hexdigest()
    actual_size = len(source_snapshot.contents)
    if artifact.sha256 != actual_sha256 or artifact.size_bytes != actual_size:
        raise ManifestError(f"{label}.artifact differs from input bytes")
    return {"generation_id": generation_id, "artifact": artifact.to_dict()}


def _dec(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - boundary conversion
        raise ManifestError(f"{field} is not numeric: {value!r}") from exc
    if not parsed.is_finite():
        raise ManifestError(f"{field} must be finite")
    return parsed


def _money(value: object, field: str) -> Decimal:
    """Parse a finite source monetary decimal without discarding precision."""
    return _dec(value, field)


def _fixed_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ManifestError("manifest decimal must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def json_ready(value: object) -> object:
    """Recursively encode Decimal values as canonical fixed-point JSON strings."""
    if isinstance(value, Decimal):
        return _fixed_decimal(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    return value


def _amount_value(row: dict[str, Any], field: str) -> Decimal | None:
    amount = row.get("amount")
    if not isinstance(amount, dict):
        return None
    raw = amount.get("value")
    if raw is None:
        return None
    return _money(raw, field)


def load_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    currency = _require_nonempty_text(
        value.get("accounting_currency"), "ledger accounting_currency"
    )
    if _CURRENCY.fullmatch(currency) is None:
        raise ManifestError("ledger accounting_currency must be an ISO currency code")
    for section in ("nav_observations", "flows"):
        if not isinstance(value.get(section), list):
            raise ManifestError(f"ledger missing list section '{section}'")
    return value


def load_baseline(
    generation_id: str, baseline_payload: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    generation_id = _generation_id(generation_id, "baseline generation_id")
    payload = dict(baseline_payload)
    for section in ("accounts", "assignments"):
        if not isinstance(payload.get(section), list):
            raise ManifestError(f"baseline missing list section '{section}'")
    return payload, generation_id


def baseline_source_identity(
    generation_id: str,
    baseline_snapshot: InputSnapshot,
    baseline: Mapping[str, Any],
) -> dict[str, object]:
    baseline_schema_version = baseline.get("baseline_schema_version")
    if isinstance(baseline_schema_version, bool) or not isinstance(
        baseline_schema_version, int
    ):
        raise ManifestError("baseline_schema_version must be an integer")
    return {
        "generation_id": generation_id,
        "artifact": _source_artifact_from_snapshot(
            baseline_snapshot,
            artifact_id="normalized/portfolio-baseline.json",
            as_of_date=baseline.get("as_of_date"),
            schema_name="portfolio-baseline",
            schema_version=str(baseline_schema_version),
        ),
    }


def load_ledger_identity(
    payload: Mapping[str, Any],
    ledger_snapshot: InputSnapshot,
    identity_snapshot: InputSnapshot,
) -> dict[str, object]:
    identity = _verified_source_identity(
        ledger_snapshot,
        payload,
        "ledger",
    )
    artifact = identity["artifact"]
    assert isinstance(artifact, Mapping)
    return {
        **identity,
        "identity_artifact": _source_artifact_from_snapshot(
            identity_snapshot,
            artifact_id="identity/ledger-source.json",
            as_of_date=artifact.get("as_of_date"),
            schema_name="ledger-source-identity",
            schema_version="1",
        ),
    }


def load_flow_classification_policy(
    payload: Mapping[str, Any],
    policy_snapshot: InputSnapshot,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the separately selected flow-disposition authority exactly once."""
    required = {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "policy_id",
        "policy_version",
        "event_dispositions",
    }
    if set(payload) != required:
        raise ManifestError("flow classification policy keys are invalid")
    if payload["schema_version"] != _FLOW_CLASSIFICATION_POLICY_SCHEMA_VERSION:
        raise ManifestError("flow classification policy schema_version must be 1")
    policy_id = _require_nonempty_text(
        payload["policy_id"], "flow classification policy.policy_id"
    )
    policy_version = _require_nonempty_text(
        payload["policy_version"], "flow classification policy.policy_version"
    )
    policy_as_of_date = _iso_date(
        payload["as_of_date"], "flow classification policy.as_of_date"
    )
    dispositions = payload["event_dispositions"]
    if not isinstance(dispositions, dict) or set(dispositions) != {"exact", "prefix"}:
        raise ManifestError("flow classification policy event_dispositions is invalid")
    if not isinstance(dispositions["exact"], list) or not isinstance(
        dispositions["prefix"], list
    ):
        raise ManifestError("flow classification policy rules must be lists")

    exact: dict[str, dict[str, str]] = {}
    counterparty_rule = "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
    for index, value in enumerate(dispositions["exact"]):
        if not isinstance(value, dict):
            raise ManifestError(
                f"flow classification exact rule {index} must be an object"
            )
        if set(value) not in (
            {"flow_type", "disposition", "amount_sign"},
            {"flow_type", "disposition", "amount_sign", "counterparty_rule"},
            {"flow_type", "disposition", "amount_sign", "counterparty_rule", "unpaired_disposition"},
        ):
            raise ManifestError(
                f"flow classification exact rule {index} keys are invalid"
            )
        flow_type = _require_nonempty_text(
            value.get("flow_type"), f"flow classification exact rule {index}.flow_type"
        )
        if flow_type in exact:
            raise ManifestError(
                f"duplicate flow classification exact rule '{flow_type}'"
            )
        disposition = _require_nonempty_text(
            value.get("disposition"),
            f"flow classification exact rule {index}.disposition",
        )
        if disposition not in _FLOW_DISPOSITIONS:
            raise ManifestError(
                f"flow classification exact rule {index}.disposition is invalid"
            )
        amount_sign = _require_nonempty_text(
            value.get("amount_sign"),
            f"flow classification exact rule {index}.amount_sign",
        )
        if amount_sign not in {"POSITIVE", "NEGATIVE", "NONZERO"}:
            raise ManifestError(
                f"flow classification exact rule {index}.amount_sign is invalid"
            )
        selected_rule = {"disposition": disposition, "amount_sign": amount_sign}
        if "counterparty_rule" in value:
            if (
                disposition != "INTERNAL"
                or value["counterparty_rule"] != counterparty_rule
            ):
                raise ManifestError(
                    f"flow classification exact rule {index}.counterparty_rule is invalid"
                )
            selected_rule["counterparty_rule"] = counterparty_rule
        if "unpaired_disposition" in value:
            if disposition != "INTERNAL" or value["unpaired_disposition"] != "EXTERNAL":
                raise ManifestError(
                    f"flow classification exact rule {index}.unpaired_disposition is invalid"
                )
            selected_rule["unpaired_disposition"] = "EXTERNAL"
        if (disposition == "INTERNAL") != ("counterparty_rule" in selected_rule):
            raise ManifestError(
                f"flow classification exact rule {index} internal pairing is invalid"
            )
        exact[flow_type] = selected_rule

    prefixes: list[dict[str, str]] = []
    seen_prefixes: set[str] = set()
    for index, value in enumerate(dispositions["prefix"]):
        if not isinstance(value, dict) or set(value) not in (
            {"flow_type_prefix", "disposition", "amount_sign"},
            {
                "flow_type_prefix",
                "disposition",
                "amount_sign",
                "counterparty_rule",
            },
            {
                "flow_type_prefix",
                "disposition",
                "amount_sign",
                "counterparty_rule",
                "unpaired_disposition",
            },
        ):
            raise ManifestError(
                f"flow classification prefix rule {index} keys are invalid"
            )
        prefix = _require_nonempty_text(
            value.get("flow_type_prefix"),
            f"flow classification prefix rule {index}.flow_type_prefix",
        )
        if prefix in seen_prefixes:
            raise ManifestError(f"duplicate flow classification prefix rule '{prefix}'")
        disposition = _require_nonempty_text(
            value.get("disposition"),
            f"flow classification prefix rule {index}.disposition",
        )
        if disposition not in _FLOW_DISPOSITIONS:
            raise ManifestError(
                f"flow classification prefix rule {index}.disposition is invalid"
            )
        amount_sign = _require_nonempty_text(
            value.get("amount_sign"),
            f"flow classification prefix rule {index}.amount_sign",
        )
        if amount_sign not in {"POSITIVE", "NEGATIVE", "NONZERO"}:
            raise ManifestError(
                f"flow classification prefix rule {index}.amount_sign is invalid"
            )
        selected_prefix = {
            "flow_type_prefix": prefix,
            "disposition": disposition,
            "amount_sign": amount_sign,
        }
        if "counterparty_rule" in value:
            if (
                disposition != "INTERNAL"
                or value["counterparty_rule"] != counterparty_rule
            ):
                raise ManifestError(
                    f"flow classification prefix rule {index}.counterparty_rule is invalid"
                )
            selected_prefix["counterparty_rule"] = counterparty_rule
        if "unpaired_disposition" in value:
            if disposition != "INTERNAL" or value["unpaired_disposition"] != "EXTERNAL":
                raise ManifestError(
                    f"flow classification prefix rule {index}.unpaired_disposition is invalid"
                )
            selected_prefix["unpaired_disposition"] = "EXTERNAL"
        if (disposition == "INTERNAL") != ("counterparty_rule" in selected_prefix):
            raise ManifestError(
                f"flow classification prefix rule {index} internal pairing is invalid"
            )
        seen_prefixes.add(prefix)
        prefixes.append(selected_prefix)

    if not exact and not prefixes:
        raise ManifestError("flow classification policy has no rules")
    prefix_values = [rule["flow_type_prefix"] for rule in prefixes]
    for index, prefix in enumerate(prefix_values):
        if any(
            prefix.startswith(other) or other.startswith(prefix)
            for other in prefix_values[index + 1 :]
        ):
            raise ManifestError("flow classification prefix rules overlap")
    if any(
        flow_type.startswith(prefix) for flow_type in exact for prefix in prefix_values
    ):
        raise ManifestError("flow classification exact and prefix rules overlap")

    artifact = _source_artifact_from_snapshot(
        policy_snapshot,
        artifact_id=payload["artifact_id"],
        as_of_date=policy_as_of_date,
        schema_name="flow-classification-policy",
        schema_version=str(_FLOW_CLASSIFICATION_POLICY_SCHEMA_VERSION),
    )
    return (
        {"exact": exact, "prefix": prefixes},
        {
            "policy_id": policy_id,
            "policy_version": policy_version,
            "event_dispositions": dispositions,
            "artifact": artifact,
        },
    )


def _validate_flow_coverage_payload(
    payload: Mapping[str, Any],
    coverage_snapshot: InputSnapshot,
    ledger_identity: Mapping[str, object],
    classified_flows_artifact: Mapping[str, object],
    receipt_snapshots: Mapping[str, InputSnapshot],
    mapped_assignments: list[dict[str, Any]],
    receipt_validator: Any,
    ledger_payload: Mapping[str, Any] | None = None,
) -> tuple[tuple[Mapping[str, object], ...], dict[str, object]]:
    """Validate coverage facts without constructing numeric-row authority.

    The injected predicate is confined to this pure validation layer. Its
    result is immutable row data, not the opaque coverage object accepted by
    the production assembler.
    """
    required = {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "ledger_identity",
        "classified_flows_artifact",
        "mapped_assignments_identity",
        "receipt_artifacts",
        "rows",
    }
    if set(payload) != required:
        raise ManifestError("flow coverage keys are invalid")
    if payload["schema_version"] != _FLOW_COVERAGE_SCHEMA_VERSION:
        raise ManifestError("flow coverage schema_version must be 2")
    selected_ledger_identity = {
        "generation_id": ledger_identity.get("generation_id"),
        "artifact": ledger_identity.get("artifact"),
    }
    coverage_ledger_identity = payload["ledger_identity"]
    if coverage_ledger_identity != selected_ledger_identity:
        raise ManifestError("flow coverage ledger identity differs from ledger identity")
    coverage_classified_artifact = payload["classified_flows_artifact"]
    if coverage_classified_artifact != classified_flows_artifact:
        raise ManifestError(
            "flow coverage classified-flow source identity differs from classified flows"
        )
    rows_payload = payload["rows"]
    if not isinstance(rows_payload, list):
        raise ManifestError("flow coverage rows must be a list")
    coverage_as_of_date = _iso_date(payload["as_of_date"], "flow coverage.as_of_date")

    assignment_identity = payload["mapped_assignments_identity"]
    if not isinstance(assignment_identity, Mapping) or set(assignment_identity) != {"sha256", "count"}:
        raise ManifestError("flow coverage mapped assignment identity is invalid")
    if not isinstance(assignment_identity.get("count"), int) or isinstance(assignment_identity.get("count"), bool):
        raise ManifestError("flow coverage mapped assignment count is invalid")
    if not isinstance(assignment_identity.get("sha256"), str) or _SHA256.fullmatch(str(assignment_identity.get("sha256"))) is None:
        raise ManifestError("flow coverage mapped assignment hash is invalid")
    expected_sha256, expected_count = _assignment_identity(mapped_assignments)
    if assignment_identity != {"sha256": expected_sha256, "count": expected_count}:
        raise ManifestError("flow coverage mapped assignment identity differs")
    expected_keys = {_assignment_key(assignment) for assignment in mapped_assignments}
    declared_receipts = payload["receipt_artifacts"]
    if not isinstance(declared_receipts, list):
        raise ManifestError("flow coverage receipt artifacts must be a list")
    declared_ids: set[str] = set()
    for declared in declared_receipts:
        if not isinstance(declared, Mapping) or set(declared) != {"artifact_id", "sha256", "size_bytes"}:
            raise ManifestError("flow coverage receipt artifact is invalid")
        artifact_id = _require_nonempty_text(declared["artifact_id"], "receipt artifact_id")
        if artifact_id in declared_ids or artifact_id not in receipt_snapshots:
            raise ManifestError("flow coverage receipt artifact set is invalid")
        snapshot = receipt_snapshots[artifact_id]
        if declared.get("sha256") != snapshot.sha256 or declared.get("size_bytes") != snapshot.size_bytes:
            raise ManifestError("flow coverage receipt artifact bytes differ")
        declared_ids.add(artifact_id)
    if declared_ids != set(receipt_snapshots):
        raise ManifestError("flow coverage receipt artifact set is incomplete")
    rows: list[dict[str, object]] = []
    complete_receipt_ids: set[str] = set()
    actual_keys: set[tuple[str, str, str, str | None]] = set()
    seen_intervals: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for index, value in enumerate(rows_payload):
        if not isinstance(value, Mapping) or set(value) != {
            "assignment",
            "requested_start",
            "requested_end",
            "status",
            "authority_kind",
            "receipt_artifact_id",
            "blocker_code",
            "blocker_detail",
        }:
            raise ManifestError(f"flow coverage row {index} keys are invalid")
        assignment = value["assignment"]
        if not isinstance(assignment, Mapping) or set(assignment) != {"account_id", "strategy_id", "effective_start", "effective_end"}:
            raise ManifestError(f"flow coverage row {index}.assignment is invalid")
        account_id = _require_nonempty_text(assignment["account_id"], f"flow coverage row {index}.assignment.account_id")
        _require_nonempty_text(assignment["strategy_id"], f"flow coverage row {index}.assignment.strategy_id")
        effective_start = _iso_date(assignment["effective_start"], f"flow coverage row {index}.assignment.effective_start")
        effective_end = assignment["effective_end"]
        if effective_end is not None:
            effective_end = _iso_date(effective_end, f"flow coverage row {index}.assignment.effective_end")
        assignment_key = (account_id, str(assignment["strategy_id"]), effective_start, effective_end)
        if assignment_key in actual_keys:
            raise ManifestError("flow coverage contains duplicate assignment")
        actual_keys.add(assignment_key)
        requested_start = _iso_date(value["requested_start"], f"flow coverage row {index}.requested_start")
        requested_end = _iso_date(value["requested_end"], f"flow coverage row {index}.requested_end")
        if requested_end < requested_start:
            raise ManifestError(f"flow coverage row {index} interval is reversed")
        if requested_end > coverage_as_of_date:
            raise ManifestError(f"flow coverage row {index} interval exceeds coverage as_of_date")
        status = _require_nonempty_text(
            value["status"], f"flow coverage row {index}.status"
        )
        if status not in _FLOW_COVERAGE_STATUSES:
            raise ManifestError(f"flow coverage row {index}.status is invalid")
        if (
            requested_start != effective_start
            or (effective_end is not None and requested_end != effective_end)
            or (
                effective_end is None
                and status == "BLOCKED"
                and requested_end != coverage_as_of_date
            )
        ):
            raise ManifestError(f"flow coverage row {index} interval does not bind assignment")
        authority_kind = _require_nonempty_text(
            value["authority_kind"], f"flow coverage row {index}.authority_kind"
        )
        if authority_kind not in _FLOW_COVERAGE_AUTHORITY_KINDS:
            raise ManifestError(
                f"flow coverage row {index}.authority_kind is not machine-verifiable"
            )
        receipt_id = value["receipt_artifact_id"]
        blocker = value["blocker_code"]
        detail = value["blocker_detail"]
        if status == "COMPLETE":
            if authority_kind not in {"ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2", "STATIC_SNAPSHOT_COVERAGE_V1"} or not isinstance(receipt_id, str) or not receipt_id:
                raise ManifestError(
                    f"flow coverage row {index} COMPLETE authority is invalid"
                )
            if blocker is not None or detail is not None:
                raise ManifestError(f"flow coverage row {index} COMPLETE blockers are invalid")
            snapshot = receipt_snapshots.get(receipt_id)
            if snapshot is None:
                raise ManifestError(f"flow coverage row {index} receipt is missing")
            receipt = snapshot.json_object(f"retrieval receipt {receipt_id}")
            if (
                authority_kind == "STATIC_SNAPSHOT_COVERAGE_V1"
                and receipt.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1"
            ) or (
                authority_kind == "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2"
                and receipt.get("schema_version") != 2
            ):
                raise ManifestError(f"flow coverage row {index} receipt authority differs")
            receipt_validator(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
            )
            complete_receipt_ids.add(receipt_id)
        elif authority_kind != "NONE" or receipt_id is not None or blocker not in _FLOW_COVERAGE_BLOCKERS or not isinstance(detail, str) or not detail.strip():
            raise ManifestError(f"flow coverage row {index} BLOCKED disposition is invalid")
        rows.append(
            {
                "account_id": account_id,
                "assignment_key": assignment_key,
                "effective_start": requested_start,
                "effective_end": requested_end,
                "status": status,
            }
        )
        seen_intervals[account_id].append((requested_start, requested_end))

    for account_id, intervals in seen_intervals.items():
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] <= previous[1]:
                raise ManifestError(f"flow coverage intervals overlap for '{account_id}'")
    if actual_keys != expected_keys:
        raise ManifestError("flow coverage assignment set differs from mapped assignments")
    if complete_receipt_ids != declared_ids:
        raise ManifestError("flow coverage receipt references differ from declared set")

    artifact = _source_artifact_from_snapshot(
        coverage_snapshot,
        artifact_id=payload["artifact_id"],
        as_of_date=payload["as_of_date"],
        schema_name="account-flow-coverage",
        schema_version=str(_FLOW_COVERAGE_SCHEMA_VERSION),
    )
    return tuple(MappingProxyType(dict(row)) for row in rows), artifact


def _make_coverage_loader(receipt_predicate: Any) -> Any:
    """Bind coverage construction inside this module, never at a call site."""
    capability = object()

    class _CoverageIndex:
        __slots__ = ("_rows", "_receipt_authority")

        def __init__(
            self,
            rows: list[dict[str, object]],
            receipt_authority: dict[str, object] | None,
            *,
            _capability: object,
        ) -> None:
            if _capability is not capability:
                raise ManifestError("coverage authority construction is denied")
            object.__setattr__(
                self, "_rows", tuple(MappingProxyType(dict(row)) for row in rows)
            )
            object.__setattr__(
                self,
                "_receipt_authority",
                None if receipt_authority is None else deepcopy(receipt_authority),
            )

        def __setattr__(self, _name: str, _value: object) -> None:
            raise AttributeError("coverage authority is immutable")

        def is_complete(
            self, account_id: str, effective_start: str, effective_end: str
        ) -> bool:
            return any(
                row["account_id"] == account_id
                and row["status"] == "COMPLETE"
                and str(row["effective_start"]) == effective_start
                and str(row["effective_end"]) == effective_end
                for row in self._rows
            )

        def is_complete_assignment(self, assignment: Mapping[str, object]) -> bool:
            return any(
                row["status"] == "COMPLETE"
                and row.get("assignment_key") == _assignment_key(dict(assignment))
                for row in self._rows
            )

        @property
        def receipt_authority(self) -> dict[str, object] | None:
            return (
                None
                if self._receipt_authority is None
                else deepcopy(self._receipt_authority)
            )

    coverage_validator = _validate_flow_coverage_payload

    def validate_complete_receipt(
        receipt: Mapping[str, object],
        receipt_id: str,
        account_id: str,
        requested_start: str,
        requested_end: str,
        coverage_as_of_date: str,
        contracts: Mapping[str, Mapping[str, object]],
    ) -> None:
        contract = contracts.get(receipt_id)
        if contract is None:
            raise ManifestError("retrieval receipt is not selected by receipt authority")
        if contract.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            _validate_static_snapshot_receipt(
                receipt, receipt_id, account_id, requested_start, requested_end,
                coverage_as_of_date, contract,
            )
        else:
            receipt_predicate(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
                contract,
            )

    def load_flow_coverage(
        coverage_snapshot: InputSnapshot,
        ledger_identity: Mapping[str, object],
        classified_flows_artifact: Mapping[str, object],
        receipt_snapshots: Mapping[str, InputSnapshot],
        mapped_assignments: list[dict[str, Any]],
        receipt_authority_snapshot: InputSnapshot | None = None,
    ) -> tuple[object, dict[str, object]]:
        """Load coverage against one selected immutable receipt authority."""
        if type(coverage_snapshot) is not InputSnapshot:
            raise ManifestError("flow coverage must be an InputSnapshot")
        if not isinstance(receipt_snapshots, Mapping):
            raise ManifestError("retrieval receipts must be a mapping")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or type(value) is not InputSnapshot
            for key, value in receipt_snapshots.items()
        ):
            raise ManifestError("retrieval receipts must be InputSnapshots")
        coverage_payload = coverage_snapshot.json_object("flow coverage")
        coverage_as_of_date = _iso_date(
            coverage_payload.get("as_of_date"), "flow coverage.as_of_date"
        )
        receipt_contracts, receipt_authority = _load_selected_receipt_authority(
            receipt_authority_snapshot,
            receipt_snapshots,
            coverage_as_of_date,
        )

        def validate_selected_receipt(
            receipt: Mapping[str, object],
            receipt_id: str,
            account_id: str,
            requested_start: str,
            requested_end: str,
            requested_coverage_as_of_date: str,
        ) -> None:
            validate_complete_receipt(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                requested_coverage_as_of_date,
                receipt_contracts,
            )

        rows, artifact = coverage_validator(
            coverage_payload,
            coverage_snapshot,
            ledger_identity,
            classified_flows_artifact,
            receipt_snapshots,
            mapped_assignments,
            validate_selected_receipt,
        )
        return (
            _CoverageIndex(
                list(rows), receipt_authority, _capability=capability
            ),
            artifact,
        )

    return load_flow_coverage


def _validate_artifact_shape(value: object, field: str) -> dict[str, object]:
    """Validate a portable artifact object before comparing authority facts."""
    if not isinstance(value, Mapping):
        raise ManifestError(f"{field} is invalid")
    try:
        artifact = SourceArtifact.from_dict(value)
    except (ManifestValidationError, ValueError, TypeError) as exc:
        raise ManifestError(f"{field} is invalid") from exc
    normalized = artifact.to_dict()
    if dict(value) != normalized:
        raise ManifestError(f"{field} is invalid")
    return normalized


def _validate_pagination_shape(value: object, field: str) -> dict[str, object]:
    required = {
        "mode",
        "page_count",
        "request_response_chain_sha256",
        "terminal_response_sha256",
        "terminal",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ManifestError(f"{field} is invalid")
    mode = _require_nonempty_text(value["mode"], f"{field}.mode")
    page_count = value["page_count"]
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 1
    ):
        raise ManifestError(f"{field}.page_count is invalid")
    result: dict[str, object] = {"mode": mode, "page_count": page_count}
    for name in ("request_response_chain_sha256", "terminal_response_sha256"):
        digest = value[name]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ManifestError(f"{field}.{name} is invalid")
        result[name] = digest
    if value["terminal"] is not True:
        raise ManifestError(f"{field}.terminal is invalid")
    result["terminal"] = True
    return result


def _validate_categories(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field} is invalid")
    categories = tuple(_require_nonempty_text(item, field) for item in value)
    if not categories or categories != tuple(sorted(set(categories))):
        raise ManifestError(f"{field} is invalid")
    return categories


def _validate_receipt_contract(
    contract: Mapping[str, object],
) -> tuple[
    tuple[str, str, str],
    dict[str, object],
    tuple[dict[str, object], ...],
    tuple[str, ...],
    dict[str, object],
]:
    required = {
        "adapter_name",
        "adapter_version",
        "code_sha256",
        "provider",
        "account_id",
        "source_account_identity_sha256",
        "provider_generation",
        "provider_generation_manifest",
        "raw_evidence_artifacts",
        "required_categories",
        "pagination",
    }
    if set(contract) != required:
        raise ManifestError("receipt authority contract is invalid")
    adapter_name = _require_nonempty_text(
        contract["adapter_name"], "receipt authority.adapter_name"
    )
    adapter_version = _require_nonempty_text(
        contract["adapter_version"], "receipt authority.adapter_version"
    )
    _require_nonempty_text(contract["provider"], "receipt authority.provider")
    _require_nonempty_text(contract["account_id"], "receipt authority.account_id")
    for field in (
        "code_sha256",
        "source_account_identity_sha256",
        "provider_generation",
    ):
        value = contract[field]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ManifestError(f"receipt authority.{field} is invalid")
    manifest = _validate_artifact_shape(
        contract["provider_generation_manifest"],
        "receipt authority.provider_generation_manifest",
    )
    raw = contract["raw_evidence_artifacts"]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ManifestError("receipt authority.raw_evidence_artifacts is invalid")
    raw_artifacts = tuple(
        _validate_artifact_shape(
            value, "receipt authority.raw_evidence_artifact"
        )
        for value in raw
    )
    if len({str(value["artifact_id"]) for value in raw_artifacts}) != len(
        raw_artifacts
    ):
        raise ManifestError("receipt authority.raw_evidence_artifacts is invalid")
    categories = _validate_categories(
        contract["required_categories"], "receipt authority.required_categories"
    )
    pagination = _validate_pagination_shape(
        contract["pagination"], "receipt authority.pagination"
    )
    return (
        (adapter_name, adapter_version, str(contract["code_sha256"])),
        manifest,
        raw_artifacts,
        categories,
        pagination,
    )


def _validate_static_snapshot_contract(contract: Mapping[str, object]) -> None:
    """Validate the deliberately narrower retained-static receipt authority."""
    required = {
        "coverage_mode", "account_id", "strategy_id", "requested_start",
        "requested_end", "provider_generation", "staged_inputs",
    }
    if set(contract) != required or contract.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1":
        raise ManifestError("static receipt authority contract is invalid")
    for field in ("account_id", "strategy_id", "requested_start", "requested_end"):
        _require_nonempty_text(contract.get(field), f"static receipt authority.{field}")
    _iso_date(contract["requested_start"], "static receipt authority.requested_start")
    _iso_date(contract["requested_end"], "static receipt authority.requested_end")
    generation = contract.get("provider_generation")
    staged = contract.get("staged_inputs")
    if not isinstance(generation, Mapping) or set(generation) != {"generation_id", "catalog_sha256", "manifest_sha256", "generation_as_of_date"}:
        raise ManifestError("static receipt authority provider generation is invalid")
    if not isinstance(staged, Mapping) or set(staged) != {"provider_ledger_generation_id", "provider_ledger_artifact", "baseline_sha256", "baseline_size_bytes", "crosswalk_sha256", "crosswalk_size_bytes"}:
        raise ManifestError("static receipt authority staged inputs are invalid")
    for field in ("generation_id", "catalog_sha256", "manifest_sha256"):
        if not isinstance(generation.get(field), str) or _SHA256.fullmatch(str(generation.get(field))) is None:
            raise ManifestError("static receipt authority provider generation is invalid")
    _iso_date(generation.get("generation_as_of_date"), "static receipt authority generation_as_of_date")
    if not isinstance(staged.get("provider_ledger_generation_id"), str) or _SHA256.fullmatch(str(staged.get("provider_ledger_generation_id"))) is None:
        raise ManifestError("static receipt authority staged inputs are invalid")
    _validate_artifact_shape(staged.get("provider_ledger_artifact"), "static receipt authority provider_ledger_artifact")
    for hash_field, size_field in (("baseline_sha256", "baseline_size_bytes"), ("crosswalk_sha256", "crosswalk_size_bytes")):
        if not isinstance(staged.get(hash_field), str) or _SHA256.fullmatch(str(staged.get(hash_field))) is None or isinstance(staged.get(size_field), bool) or not isinstance(staged.get(size_field), int) or int(staged.get(size_field)) < 0:
            raise ManifestError("static receipt authority staged inputs are invalid")


def _validate_static_snapshot_receipt(
    receipt: Mapping[str, object], receipt_id: str, account_id: str,
    requested_start: str, requested_end: str, coverage_as_of_date: str,
    contract: Mapping[str, object],
    ledger_payload: Mapping[str, Any] | None = None,
) -> None:
    required = {"schema_version", "artifact_id", "coverage_mode", "issuer", "provider", "account_id", "strategy_id", "requested_start", "requested_end", "calculation_cutoff_date", "date_convention", "provider_generation", "staged_inputs", "observed_facts", "missing_intervals", "coverage_as_of_date", "completeness"}
    if set(receipt) != required or receipt.get("schema_version") != 3 or receipt.get("artifact_id") != receipt_id or receipt.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1" or receipt.get("date_convention") != "EOD_PROVIDER_NAV_RECOGNITION_DATE" or receipt.get("completeness") != "RETAINED_STATIC_SNAPSHOT_ONLY":
        raise ManifestError("static snapshot receipt identity is invalid")
    issuer = receipt.get("issuer")
    if not isinstance(issuer, Mapping) or set(issuer) != {"adapter_name", "adapter_version", "code_sha256"} or issuer.get("adapter_name") != "static-snapshot-coverage" or issuer.get("adapter_version") != "1" or not isinstance(issuer.get("code_sha256"), str) or _SHA256.fullmatch(str(issuer.get("code_sha256"))) is None:
        raise ManifestError("static snapshot receipt issuer is invalid")
    _validate_static_snapshot_contract(contract)
    if receipt.get("account_id") != account_id or receipt.get("strategy_id") != contract["strategy_id"] or receipt.get("requested_start") != requested_start or receipt.get("requested_end") != requested_end or receipt.get("provider_generation") != contract["provider_generation"] or receipt.get("staged_inputs") != contract["staged_inputs"]:
        raise ManifestError("static snapshot receipt source binding differs")
    cutoff = _iso_date(receipt.get("calculation_cutoff_date"), "static snapshot receipt calculation_cutoff_date")
    receipt_as_of = _iso_date(receipt.get("coverage_as_of_date"), "static snapshot receipt coverage_as_of_date")
    if cutoff > coverage_as_of_date or receipt_as_of < cutoff:
        raise ManifestError("static snapshot receipt exceeds coverage as_of_date")
    facts = receipt.get("observed_facts")
    if not isinstance(facts, Mapping) or set(facts) != {"nav", "flows", "capital_anchors"}:
        raise ManifestError("static snapshot receipt observed facts are invalid")
    for summary in facts.values():
        if not isinstance(summary, Mapping) or set(summary) != {"row_count", "first_date", "last_date"} or isinstance(summary.get("row_count"), bool) or not isinstance(summary.get("row_count"), int) or int(summary.get("row_count")) < 0:
            raise ManifestError("static snapshot receipt observed facts are invalid")
        count = int(summary["row_count"])
        if count == 0 and (summary.get("first_date") is not None or summary.get("last_date") is not None):
            raise ManifestError("static snapshot receipt observed facts are invalid")
        if count and (_iso_date(summary.get("first_date"), "static snapshot receipt first_date") > _iso_date(summary.get("last_date"), "static snapshot receipt last_date")):
            raise ManifestError("static snapshot receipt observed facts are invalid")
    missing = receipt.get("missing_intervals")
    if not isinstance(missing, list) or any(not isinstance(item, Mapping) or set(item) != {"start", "end", "reason"} or item.get("reason") != "PROVIDER_DECLARED_NAV_MISSING" or _iso_date(item.get("start"), "static snapshot receipt missing start") > _iso_date(item.get("end"), "static snapshot receipt missing end") for item in missing):
        raise ManifestError("static snapshot receipt missing intervals are invalid")
    if ledger_payload is None:
        return
    def fact_date(row: Mapping[str, object], kind: str) -> str:
        return str(
            row.get("as_of_date")
            if kind == "nav"
            else row.get("nav_recognition_date", row.get("event_date"))
        )
    expected_facts: dict[str, object] = {}
    for kind, field in (("nav", "nav_observations"), ("flows", "flows"), ("capital_anchors", "capital_anchors")):
        source_rows = ledger_payload.get(field)
        if not isinstance(source_rows, list):
            raise ManifestError("static snapshot receipt calculation ledger is invalid")
        rows = [row for row in source_rows if isinstance(row, Mapping) and row.get("account_id") == account_id and (kind != "capital_anchors" or row.get("strategy_id") == receipt.get("strategy_id")) and requested_start <= fact_date(row, kind) <= requested_end]
        dates = sorted(fact_date(row, kind) for row in rows)
        expected_facts[kind] = {"row_count": len(rows), "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None}
    if facts != expected_facts:
        raise ManifestError("static snapshot receipt observed facts differ from calculation ledger")


def _validate_receipt_against_contract(
    receipt: Mapping[str, object],
    receipt_id: str,
    account_id: str,
    requested_start: str,
    requested_end: str,
    coverage_as_of_date: str,
    contract: Mapping[str, object],
) -> None:
    """Pure predicate check; its result is not an authority capability."""
    required = {
        "schema_version",
        "artifact_id",
        "issuer",
        "provider",
        "provider_generation",
        "provider_generation_manifest",
        "raw_evidence_artifacts",
        "account_id",
        "source_account_identity_sha256",
        "requested_start",
        "requested_end",
        "covered_start",
        "covered_end",
        "required_categories",
        "completed_categories",
        "unavailable_categories",
        "pagination",
        "completeness",
        "truncated",
    }
    if (
        set(receipt) != required
        or receipt.get("schema_version") != 2
        or receipt.get("artifact_id") != receipt_id
    ):
        raise ManifestError("retrieval receipt identity is invalid")
    issuer = receipt.get("issuer")
    if not isinstance(issuer, Mapping) or set(issuer) != {
        "adapter_name",
        "adapter_version",
        "code_sha256",
    }:
        raise ManifestError("retrieval receipt issuer is invalid")
    issuer_name = _require_nonempty_text(
        issuer.get("adapter_name"), "retrieval receipt issuer.adapter_name"
    )
    issuer_version = _require_nonempty_text(
        issuer.get("adapter_version"), "retrieval receipt issuer.adapter_version"
    )
    issuer_code = issuer.get("code_sha256")
    if not isinstance(issuer_code, str) or _SHA256.fullmatch(issuer_code) is None:
        raise ManifestError("retrieval receipt issuer is invalid")
    (
        contract_issuer,
        manifest,
        raw_artifacts,
        contract_categories,
        contract_pagination,
    ) = _validate_receipt_contract(contract)
    if (issuer_name, issuer_version, issuer_code) != contract_issuer:
        raise ManifestError("retrieval receipt issuer is not admissible")
    provider = _require_nonempty_text(
        receipt.get("provider"), "retrieval receipt.provider"
    )
    receipt_account = _require_nonempty_text(
        receipt.get("account_id"), "retrieval receipt.account_id"
    )
    source_identity = receipt.get("source_account_identity_sha256")
    generation = receipt.get("provider_generation")
    if (
        not isinstance(source_identity, str)
        or _SHA256.fullmatch(source_identity) is None
        or not isinstance(generation, str)
        or _SHA256.fullmatch(generation) is None
    ):
        raise ManifestError("retrieval receipt source binding differs")
    if (
        provider != contract["provider"]
        or receipt_account != contract["account_id"]
        or account_id != contract["account_id"]
        or source_identity != contract["source_account_identity_sha256"]
    ):
        raise ManifestError("retrieval receipt source binding differs")
    receipt_manifest = _validate_artifact_shape(
        receipt.get("provider_generation_manifest"),
        "retrieval receipt.provider_generation_manifest",
    )
    raw = receipt.get("raw_evidence_artifacts")
    if not isinstance(raw, list) or not raw:
        raise ManifestError("retrieval receipt.raw_evidence_artifacts is invalid")
    receipt_raw = tuple(
        _validate_artifact_shape(value, "retrieval receipt.raw_evidence_artifact")
        for value in raw
    )
    if len({str(value["artifact_id"]) for value in receipt_raw}) != len(receipt_raw):
        raise ManifestError("retrieval receipt.raw_evidence_artifacts is invalid")
    if (
        generation != contract["provider_generation"]
        or receipt_manifest != manifest
        or receipt_raw != raw_artifacts
    ):
        raise ManifestError("retrieval receipt provider evidence differs")
    for field in ("requested_start", "requested_end", "covered_start", "covered_end"):
        if _iso_date(receipt.get(field), f"retrieval receipt.{field}") > coverage_as_of_date:
            raise ManifestError("retrieval receipt interval exceeds coverage as_of_date")
    if (
        receipt.get("requested_start") != requested_start
        or receipt.get("requested_end") != requested_end
        or receipt.get("covered_start") != requested_start
        or receipt.get("covered_end") != requested_end
    ):
        raise ManifestError("retrieval receipt interval boundary differs")
    if (
        _validate_categories(
            receipt.get("required_categories"),
            "retrieval receipt.required_categories",
        )
        != contract_categories
        or receipt.get("completed_categories") != list(contract_categories)
        or receipt.get("unavailable_categories") != []
    ):
        raise ManifestError("retrieval receipt categories are incomplete")
    if (
        _validate_pagination_shape(
            receipt.get("pagination"), "retrieval receipt.pagination"
        )
        != contract_pagination
        or receipt.get("truncated") is not False
        or receipt.get("completeness")
        != "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES"
    ):
        raise ManifestError("retrieval receipt pagination is not terminal")


def _load_selected_receipt_authority(
    authority_snapshot: InputSnapshot | None,
    receipt_snapshots: Mapping[str, InputSnapshot],
    coverage_as_of_date: str,
) -> tuple[Mapping[str, Mapping[str, object]], dict[str, object] | None]:
    """Load one canonical, byte-bound authority selected by the operator."""
    if authority_snapshot is None:
        if receipt_snapshots:
            raise ManifestError("retrieval receipt authority is required")
        return MappingProxyType({}), None
    if type(authority_snapshot) is not InputSnapshot:
        raise ManifestError("receipt authority must be an InputSnapshot")
    payload = authority_snapshot.json_object("receipt authority")
    if canonical_json_bytes(payload) != authority_snapshot.contents:
        raise ManifestError("receipt authority must use canonical JSON bytes")
    if set(payload) != {"schema_version", "artifact_id", "as_of_date", "receipts"}:
        raise ManifestError("receipt authority keys are invalid")
    if payload.get("schema_version") != _RECEIPT_AUTHORITY_SCHEMA_VERSION:
        raise ManifestError("receipt authority schema_version is invalid")
    if payload.get("artifact_id") != _RECEIPT_AUTHORITY_ARTIFACT_ID:
        raise ManifestError("receipt authority artifact_id is invalid")
    authority_as_of_date = _iso_date(
        payload.get("as_of_date"), "receipt authority.as_of_date"
    )
    if authority_as_of_date > coverage_as_of_date:
        raise ManifestError("receipt authority as_of_date exceeds flow coverage")
    entries = payload.get("receipts")
    if not isinstance(entries, list):
        raise ManifestError("receipt authority receipts must be a list")

    contracts: dict[str, Mapping[str, object]] = {}
    folded_ids: set[str] = set()
    authority_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "artifact_id",
            "sha256",
            "size_bytes",
            "contract",
        }:
            raise ManifestError("receipt authority receipt is invalid")
        receipt_id = _require_nonempty_text(
            entry.get("artifact_id"), "receipt authority receipt.artifact_id"
        )
        if receipt_id.casefold() in folded_ids:
            raise ManifestError("receipt authority receipt artifact IDs are not unique")
        folded_ids.add(receipt_id.casefold())
        authority_ids.append(receipt_id)
        declared_sha256 = entry.get("sha256")
        if (
            not isinstance(declared_sha256, str)
            or _SHA256.fullmatch(declared_sha256) is None
        ):
            raise ManifestError("receipt authority receipt.sha256 is invalid")
        declared_size = entry.get("size_bytes")
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            raise ManifestError("receipt authority receipt.size_bytes is invalid")
        contract = entry.get("contract")
        if not isinstance(contract, Mapping):
            raise ManifestError("receipt authority contract is invalid")
        if contract.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            _validate_static_snapshot_contract(contract)
        else:
            _validate_receipt_contract(contract)
        snapshot = receipt_snapshots.get(receipt_id)
        if snapshot is None:
            raise ManifestError("receipt authority receipt set differs from selected receipts")
        if (
            declared_sha256 != snapshot.sha256
            or declared_size != snapshot.size_bytes
        ):
            raise ManifestError("receipt authority receipt bytes differ")
        receipt = snapshot.json_object(f"retrieval receipt {receipt_id}")
        receipt_account_id = _require_nonempty_text(
            receipt.get("account_id"), "retrieval receipt.account_id"
        )
        requested_start = _iso_date(
            receipt.get("requested_start"), "retrieval receipt.requested_start"
        )
        requested_end = _iso_date(
            receipt.get("requested_end"), "retrieval receipt.requested_end"
        )
        if contract.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            _validate_static_snapshot_receipt(
                receipt, receipt_id, receipt_account_id, requested_start,
                requested_end, coverage_as_of_date, contract,
            )
        else:
            _validate_receipt_against_contract(
                receipt, receipt_id, receipt_account_id, requested_start,
                requested_end, coverage_as_of_date, contract,
            )
        contracts[receipt_id] = MappingProxyType(dict(contract))
    if authority_ids != sorted(authority_ids, key=str.casefold):
        raise ManifestError("receipt authority receipts are not sorted")
    if set(authority_ids) != set(receipt_snapshots):
        raise ManifestError("receipt authority receipt set differs from selected receipts")
    return (
        MappingProxyType(dict(contracts)),
        _source_artifact_from_snapshot(
            authority_snapshot,
            artifact_id=payload["artifact_id"],
            as_of_date=authority_as_of_date,
            schema_name=_RECEIPT_AUTHORITY_SCHEMA_NAME,
            schema_version=str(_RECEIPT_AUTHORITY_SCHEMA_VERSION),
        ),
    )


load_flow_coverage = _make_coverage_loader(_validate_receipt_against_contract)
del _make_coverage_loader


def load_flow_classes(
    payload: Mapping[str, Any],
    classified_snapshot: InputSnapshot,
    ledger_identity: Mapping[str, object],
    classification_policy_identity: Mapping[str, object],
) -> tuple[list[dict[str, Any]], dict[str, object], dict[str, str]]:
    """Load a hash-bound classified-flow artifact without fallbacks.

    The artifact binds itself to the exact ledger identity, has a strict
    versioned schema, and supplies an explicit classification-policy identity.
    Individual events are reconciled separately against every raw ledger row.
    """
    flow_schema_version = payload.get("schema_version")
    required = {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "ledger_identity",
        "classification_policy",
        "events",
    }
    if flow_schema_version == _CURRENT_FLOW_CLASSES_SCHEMA_VERSION:
        required.add("capital_anchors")
    if set(payload) != required:
        raise ManifestError("classified flows keys are invalid")
    if flow_schema_version not in {
        _FLOW_CLASSES_SCHEMA_VERSION,
        _CURRENT_FLOW_CLASSES_SCHEMA_VERSION,
    }:
        raise ManifestError("classified flows schema_version must be 3 or 4")
    selected_ledger_identity = {
        "generation_id": ledger_identity.get("generation_id"),
        "artifact": ledger_identity.get("artifact"),
    }
    if payload["ledger_identity"] != selected_ledger_identity:
        raise ManifestError("classified flows ledger identity differs")
    policy = payload["classification_policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "policy_id",
        "policy_version",
        "policy_as_of_date",
        "policy_sha256",
    }:
        raise ManifestError("classified flows classification_policy is invalid")
    policy_id = _require_nonempty_text(
        policy["policy_id"], "classification_policy.policy_id"
    )
    policy_version = _require_nonempty_text(
        policy["policy_version"], "classification_policy.policy_version"
    )
    policy_as_of_date = _iso_date(
        policy["policy_as_of_date"], "classification_policy.policy_as_of_date"
    )
    policy_sha256 = policy["policy_sha256"]
    if not isinstance(policy_sha256, str) or _SHA256.fullmatch(policy_sha256) is None:
        raise ManifestError("classification_policy.policy_sha256 is invalid")
    policy_artifact = classification_policy_identity.get("artifact")
    if not isinstance(policy_artifact, Mapping):
        raise ManifestError("selected flow classification policy identity is invalid")
    selected_policy_reference = {
        "policy_id": classification_policy_identity.get("policy_id"),
        "policy_version": classification_policy_identity.get("policy_version"),
        "policy_as_of_date": policy_artifact.get("as_of_date"),
        "policy_sha256": policy_artifact.get("sha256"),
    }
    if policy != selected_policy_reference:
        raise ManifestError(
            "classified flows classification policy differs from selected authority"
        )
    if not isinstance(payload["events"], list):
        raise ManifestError("flow classes missing 'events' list")
    events: list[dict[str, Any]] = []
    for index, event in enumerate(payload["events"]):
        if not isinstance(event, dict):
            raise ManifestError(f"classified flows event {index} must be an object")
        events.append(dict(event))
    artifact = _source_artifact_from_snapshot(
        classified_snapshot,
        artifact_id=payload["artifact_id"],
        as_of_date=payload["as_of_date"],
        schema_name="classified-flows",
        schema_version=str(flow_schema_version),
    )
    return (
        events,
        artifact,
        {
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_as_of_date": policy_as_of_date,
            "policy_sha256": policy_sha256,
        },
    )


def parse_nav_observations(
    ledger: dict[str, Any],
) -> tuple[dict[str, dict[str, Decimal]], dict[str, set[str]]]:
    """Parse the complete governed NAV vocabulary without silently skipping facts."""
    series: dict[str, dict[str, Decimal]] = defaultdict(dict)
    unavailable: dict[str, set[str]] = defaultdict(set)
    observed_keys: set[tuple[str, str]] = set()
    accounting_currency = _require_nonempty_text(
        ledger.get("accounting_currency"), "ledger accounting_currency"
    )
    for index, obs in enumerate(ledger["nav_observations"]):
        if not isinstance(obs, dict):
            raise ManifestError(f"nav observation {index} must be an object")
        account_id = _require_nonempty_text(
            obs.get("account_id"), f"nav[{index}].account_id"
        )
        as_of = _iso_date(obs.get("as_of_date"), f"nav[{index}].as_of_date")
        key = (account_id, as_of)
        if key in observed_keys:
            raise ManifestError(f"duplicate NAV observation for {account_id}/{as_of}")
        observed_keys.add(key)
        nav = obs.get("nav")
        if not isinstance(nav, dict):
            raise ManifestError(f"nav[{account_id}/{as_of}] must be an object")
        currency = _require_nonempty_text(
            nav.get("currency"), f"nav[{account_id}/{as_of}].currency"
        )
        if currency != accounting_currency:
            raise ManifestError(
                f"nav[{account_id}/{as_of}] currency differs from accounting currency"
            )
        status = _require_nonempty_text(
            nav.get("status"), f"nav[{account_id}/{as_of}].status"
        )
        if status == "UNAVAILABLE":
            if set(nav) != {"currency", "status", "reason"}:
                raise ManifestError(
                    f"nav[{account_id}/{as_of}] UNAVAILABLE must have reason and no value"
                )
            _require_nonempty_text(
                nav.get("reason"), f"nav[{account_id}/{as_of}].reason"
            )
            unavailable[account_id].add(as_of)
            continue
        if status != "AVAILABLE":
            raise ManifestError(f"nav[{account_id}/{as_of}].status is invalid")
        if set(nav) != {"currency", "status", "value"}:
            raise ManifestError(
                f"nav[{account_id}/{as_of}] AVAILABLE must have value and no reason"
            )
        v = _money(nav.get("value"), f"nav[{account_id}/{as_of}]")
        series[account_id][as_of] = v
    return (
        {a: dict(sorted(d.items())) for a, d in series.items()},
        {a: set(days) for a, days in unavailable.items()},
    )


def nav_series(ledger: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """Compatibility helper for callers that require available NAVs only."""
    return parse_nav_observations(ledger)[0]


def parse_capital_anchors(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate provider capital authority without coercing source currencies."""

    raw_anchors = ledger.get("capital_anchors", [])
    if not isinstance(raw_anchors, list):
        raise ManifestError("ledger capital_anchors must be a list")
    accounting_currency = _require_nonempty_text(
        ledger.get("accounting_currency"), "ledger accounting_currency"
    )
    anchors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_anchors):
        if not isinstance(raw, Mapping):
            raise ManifestError(f"capital anchor {index} must be an object")
        record_id = _require_nonempty_text(
            raw.get("record_id"), f"capital anchor {index}.record_id"
        )
        if record_id in seen_ids:
            raise ManifestError(f"duplicate capital anchor record_id '{record_id}'")
        seen_ids.add(record_id)
        capital_type = _require_nonempty_text(
            raw.get("capital_type"), f"capital anchor {record_id}.capital_type"
        )
        if capital_type not in {
            "OPENING_CAPITAL_ANCHOR",
            "STRATEGY_CAPITAL_ALLOCATION",
        }:
            raise ManifestError(f"capital anchor {record_id} type is invalid")
        if raw.get("mapping_status") != "MAPPED" or raw.get("evidence_layer") != (
            "ACCOUNT_LIVE"
        ):
            raise ManifestError(f"capital anchor {record_id} mapping is unavailable")
        amount = raw.get("amount")
        if not isinstance(amount, Mapping) or set(amount) != {
            "currency",
            "status",
            "value",
        } or amount.get("status") != "AVAILABLE":
            raise ManifestError(f"capital anchor {record_id} amount is unavailable")
        source_currency = _require_nonempty_text(
            amount.get("currency"), f"capital anchor {record_id}.amount.currency"
        )
        source_amount = _money(
            amount.get("value"), f"capital anchor {record_id}.amount.value"
        )
        if source_amount <= 0:
            raise ManifestError(f"capital anchor {record_id} amount is not positive")
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            opening_nav = raw.get("opening_nav")
            if not isinstance(opening_nav, Mapping) or set(opening_nav) != {
                "currency",
                "status",
                "value",
            } or opening_nav.get("status") != "AVAILABLE":
                raise ManifestError(
                    f"capital anchor {record_id} opening_nav is unavailable"
                )
            if opening_nav.get("currency") != accounting_currency:
                raise ManifestError(
                    f"capital anchor {record_id} opening_nav currency differs"
                )
            opening_nav_amount = _money(
                opening_nav.get("value"),
                f"capital anchor {record_id}.opening_nav.value",
            )
            if opening_nav_amount <= 0:
                raise ManifestError(
                    f"capital anchor {record_id} opening_nav is not positive"
                )
            basis_amount = source_amount
        else:
            if source_currency != accounting_currency:
                raise ManifestError(
                    f"capital allocation {record_id} currency differs from accounting currency"
                )
            if "opening_nav" in raw:
                raise ManifestError(
                    f"capital allocation {record_id} cannot declare opening_nav"
                )
            basis_amount = source_amount
        anchors.append(
            {
                "record_id": record_id,
                "account_id": _require_nonempty_text(
                    raw.get("account_id"), f"capital anchor {record_id}.account_id"
                ),
                "strategy_id": _require_nonempty_text(
                    raw.get("strategy_id"), f"capital anchor {record_id}.strategy_id"
                ),
                "capital_type": capital_type,
                "event_date": _iso_date(
                    raw.get("event_date"), f"capital anchor {record_id}.event_date"
                ),
                "nav_recognition_date": _iso_date(
                    raw.get("nav_recognition_date"),
                    f"capital anchor {record_id}.nav_recognition_date",
                ),
                "amount": basis_amount,
            }
        )
        if anchors[-1]["nav_recognition_date"] < anchors[-1]["event_date"]:
            raise ManifestError(
                f"capital anchor {record_id} recognition precedes economic event"
            )
    return anchors


def _raw_flow_records(ledger: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    flows = ledger.get("flows")
    if not isinstance(flows, list):
        raise ManifestError("ledger missing list section 'flows'")
    accounting_currency = _require_nonempty_text(
        ledger.get("accounting_currency"), "ledger accounting_currency"
    )
    for index, row in enumerate(flows):
        if not isinstance(row, dict):
            raise ManifestError(f"raw flow {index} must be an object")
        record_id = _require_nonempty_text(
            row.get("record_id"), f"raw flow {index}.record_id"
        )
        if record_id in raw:
            raise ManifestError(f"duplicate raw flow record_id '{record_id}'")
        amount_value = row.get("amount")
        if not isinstance(amount_value, dict) or set(amount_value) != {
            "currency",
            "status",
            "value",
        }:
            raise ManifestError(f"raw flow {record_id}.amount contract is invalid")
        if amount_value.get("status") != "AVAILABLE":
            raise ManifestError(f"raw flow {record_id}.amount is unavailable")
        currency = _require_nonempty_text(
            amount_value.get("currency"), f"raw flow {record_id}.amount.currency"
        )
        if currency != accounting_currency:
            raise ManifestError(
                f"raw flow {record_id} currency differs from accounting currency"
            )
        amount = _amount_value(row, f"raw flow {record_id}.amount")
        if amount is None:
            raise ManifestError(f"raw flow {record_id}.amount is missing")
        raw[record_id] = {
            "record_id": record_id,
            "account_id": _require_nonempty_text(
                row.get("account_id"), f"raw flow {record_id}.account_id"
            ),
            "event_date": _iso_date(
                row.get("event_date"), f"raw flow {record_id}.event_date"
            ),
            "nav_recognition_date": _iso_date(
                row.get("nav_recognition_date", row.get("event_date")),
                f"raw flow {record_id}.nav_recognition_date",
            ),
            "flow_type": _require_nonempty_text(
                row.get("flow_type"), f"raw flow {record_id}.flow_type"
            ),
            "amount": amount,
        }
    return raw


def _expected_flow_dispositions(
    raw: Mapping[str, Mapping[str, Any]],
    classification_policy: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """Apply only the separately selected policy; unknowns hard-fail."""
    exact = classification_policy.get("exact")
    prefixes = classification_policy.get("prefix")
    if not isinstance(exact, Mapping) or not isinstance(prefixes, list):
        raise ManifestError("flow classification policy is invalid")

    def select_rule(record_id: str, flow_type: str) -> Mapping[str, Any]:
        selected = exact.get(flow_type)
        if selected is None:
            matching_prefixes = [
                rule
                for rule in prefixes
                if isinstance(rule, Mapping)
                and flow_type.startswith(str(rule.get("flow_type_prefix", "")))
            ]
            if len(matching_prefixes) > 1:
                raise ManifestError(
                    f"raw flow {record_id} matches ambiguous classification prefixes"
                )
            selected = matching_prefixes[0] if matching_prefixes else None
        if not isinstance(selected, Mapping):
            raise ManifestError(
                f"raw flow {record_id} has unresolved flow_type '{flow_type}'"
            )
        return selected

    selected_rules: dict[str, Mapping[str, Any]] = {}
    transfers_by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record_id, record in raw.items():
        rule = select_rule(record_id, str(record["flow_type"]))
        selected_rules[record_id] = rule
        amount = record.get("amount")
        if not isinstance(amount, Decimal):
            raise ManifestError(f"raw flow {record_id} amount is invalid")
        amount_sign = rule.get("amount_sign")
        sign_valid = (
            (amount_sign == "POSITIVE" and amount > 0)
            or (amount_sign == "NEGATIVE" and amount < 0)
            or (amount_sign == "NONZERO" and amount != 0)
        )
        if not sign_valid:
            raise ManifestError(
                f"raw flow {record_id} amount sign violates classification policy"
            )
        if "counterparty_rule" in rule:
            transfers_by_day[str(record["event_date"])].append(record)

    dispositions: dict[str, str] = {}
    counterparties: dict[str, str] = {}
    for record_id, record in raw.items():
        selected_rule = selected_rules[record_id]
        disposition = selected_rule.get("disposition")
        if disposition not in _FLOW_DISPOSITIONS:
            raise ManifestError(f"raw flow {record_id} policy disposition is invalid")
        if "counterparty_rule" in selected_rule:
            candidates = [
                other
                for other in transfers_by_day[str(record["event_date"])]
                if other["record_id"] != record_id
                and other["account_id"] != record["account_id"]
                and other["amount"] == -record["amount"]
            ]
            if (
                not candidates
                and selected_rule.get("unpaired_disposition") == "EXTERNAL"
            ):
                dispositions[record_id] = "EXTERNAL"
                continue
            if len(candidates) != 1:
                raise ManifestError(
                    f"raw flow {record_id} counterparty is unpaired or ambiguous"
                )
            dispositions[record_id] = str(disposition)
            counterparties[record_id] = str(candidates[0]["record_id"])
            continue
        dispositions[record_id] = str(disposition)
    return dispositions, counterparties


@_isolated_decimal_context
def validate_classified_flow_coverage(
    ledger: Mapping[str, Any],
    events: list[dict[str, Any]],
    classification_policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Require a one-to-one, field-for-field classified disposition.

    Totals are deliberately insufficient: each event keeps both its stable
    classified id and the exact raw record id it covers.
    """
    raw = _raw_flow_records(ledger)
    expected_dispositions, counterparties = _expected_flow_dispositions(
        raw, classification_policy
    )
    classified_by_source: dict[str, dict[str, Any]] = {}
    classified_ids: set[str] = set()
    required = {
        "event_id",
        "source_record_id",
        "account_id",
        "event_date",
        "amount",
        "disposition",
    }
    for index, event in enumerate(events):
        allowed_optional = {"counterparty_source_record_id", "nav_recognition_date"}
        if set(event) - allowed_optional != required:
            raise ManifestError(f"classified flow event {index} keys are invalid")
        event_id = _require_nonempty_text(
            event.get("event_id"), f"classified flow {index}.event_id"
        )
        if event_id in classified_ids:
            raise ManifestError(f"duplicate classified event_id '{event_id}'")
        classified_ids.add(event_id)
        source_record_id = _require_nonempty_text(
            event.get("source_record_id"),
            f"classified flow {event_id}.source_record_id",
        )
        if source_record_id in classified_by_source:
            raise ManifestError(
                f"duplicate classified source_record_id '{source_record_id}'"
            )
        raw_record = raw.get(source_record_id)
        if raw_record is None:
            raise ManifestError(
                f"classified flow {event_id} has extra source_record_id '{source_record_id}'"
            )
        account_id = _require_nonempty_text(
            event.get("account_id"), f"classified flow {event_id}.account_id"
        )
        event_date = _iso_date(
            event.get("event_date"), f"classified flow {event_id}.event_date"
        )
        nav_recognition_date = _iso_date(
            event.get("nav_recognition_date", event_date),
            f"classified flow {event_id}.nav_recognition_date",
        )
        amount = _money(event.get("amount"), f"classified flow {event_id}.amount")
        disposition = _require_nonempty_text(
            event.get("disposition"), f"classified flow {event_id}.disposition"
        )
        if disposition not in _FLOW_DISPOSITIONS:
            raise ManifestError(f"classified flow {event_id} disposition is invalid")
        if (
            account_id != raw_record["account_id"]
            or event_date != raw_record["event_date"]
            or nav_recognition_date != raw_record["nav_recognition_date"]
            or amount != raw_record["amount"]
            or disposition != expected_dispositions[source_record_id]
        ):
            raise ManifestError(
                f"classified flow {event_id} differs from raw record {source_record_id}"
            )
        counterparty = counterparties.get(source_record_id)
        supplied_counterparty = event.get("counterparty_source_record_id")
        if counterparty is None and supplied_counterparty is not None:
            raise ManifestError(
                f"classified flow {event_id} has an unexpected counterparty"
            )
        if counterparty is not None and supplied_counterparty != counterparty:
            raise ManifestError(
                f"classified flow {event_id} must bind counterparty {counterparty}"
            )
        classified_by_source[source_record_id] = {
            "event_id": event_id,
            "source_record_id": source_record_id,
            "account_id": account_id,
            "event_date": event_date,
            "nav_recognition_date": nav_recognition_date,
            "amount": amount,
            "disposition": disposition,
            **(
                {"counterparty_source_record_id": counterparty}
                if counterparty is not None
                else {}
            ),
        }
    missing = sorted(set(raw) - set(classified_by_source))
    if missing:
        raise ManifestError(f"missing classified flow mapping(s): {', '.join(missing)}")
    return [
        classified_by_source[record_id] for record_id in sorted(classified_by_source)
    ]


@_isolated_decimal_context
def flow_buckets_from_classes(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, dict[tuple[str, str], Decimal]]]:
    """Build buckets only after exact event-level reconciliation."""
    external_in: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    external_out: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    internal_in: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    internal_out: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for event in events:
        account = str(event["account_id"])
        event_day = str(event["event_date"])
        recognition_day = str(event.get("nav_recognition_date", event_day))
        leg = (event_day, recognition_day)
        amount = event["amount"]
        if not isinstance(amount, Decimal):
            raise ManifestError("validated classified flow amount is not Decimal")
        if event["disposition"] == "EXTERNAL":
            if amount >= 0:
                external_in[account][leg] += amount
            else:
                external_out[account][leg] += -amount
        elif amount >= 0:
            internal_in[account][leg] += amount
        else:
            internal_out[account][leg] += -amount
    return {
        "external_in": external_in,
        "external_out": external_out,
        "internal_in": internal_in,
        "internal_out": internal_out,
    }


@_isolated_decimal_context
def xirr(cashflows: list[tuple[date, Decimal]]) -> Decimal | None:
    """Unique-root bisection XIRR; None when uniqueness or convergence fails."""
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for cashflow_date, amount in cashflows:
        by_date[cashflow_date] += amount
    ordered = [(day, amount) for day, amount in sorted(by_date.items()) if amount]
    signs = [amount > 0 for _, amount in ordered]
    sign_changes = sum(left != right for left, right in zip(signs, signs[1:]))
    # More than one chronological sign change can produce multiple valid IRRs.
    # A diagnostic must fail closed rather than select an arbitrary root.
    if len(ordered) < 2 or sign_changes != 1:
        return None
    t0 = ordered[0][0]

    def npv(rate: Decimal) -> Decimal:
        total = Decimal(0)
        for dt, a in ordered:
            total += a / (
                (Decimal(1) + rate) ** (Decimal((dt - t0).days) / Decimal(365))
            )
        return total

    tolerance = max(abs(amount) for _, amount in ordered) * Decimal("1e-28")
    zero = Decimal(0)
    fzero = npv(zero)
    if abs(fzero) <= tolerance:
        return zero

    bracket: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
    right, fright = zero, fzero
    for exponent in range(1, getcontext().prec + 1):
        left = Decimal(-1) + (Decimal(10) ** -exponent)
        if left == right:
            break
        fleft = npv(left)
        if abs(fleft) <= tolerance:
            return left
        if fleft * fright < 0:
            bracket = (left, right, fleft, fright)
            break
        right, fright = left, fleft

    if bracket is None:
        left, fleft = zero, fzero
        for exponent in range(1, getcontext().prec + 1):
            right = (Decimal(10) ** exponent) - Decimal(1)
            fright = npv(right)
            if abs(fright) <= tolerance:
                return right
            if fleft * fright < 0:
                bracket = (left, right, fleft, fright)
                break
            left, fleft = right, fright

    if bracket is None:
        return None
    lo, hi, flo, _fhi = bracket
    for _ in range(400):
        mid = (lo + hi) / 2
        fmid = npv(mid)
        if (fmid < 0) == (flo < 0):
            lo, flo = mid, fmid
        else:
            hi = mid
    rate = (lo + hi) / 2
    if abs(npv(rate)) > tolerance:
        return None
    # Bisection around an exact zero can leave a vanishing numerical residue.
    # Canonicalize that residue so JSON never carries an exponent-form pseudo-rate.
    return Decimal(0) if abs(rate) < Decimal("1e-30") else rate


def _assignment_key(assignment: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
    account_id = _require_nonempty_text(
        assignment.get("account_id"), "assignment.account_id"
    )
    strategy_id = _require_nonempty_text(
        assignment.get("strategy_id"), "assignment.strategy_id"
    )
    effective_start = _iso_date(
        assignment.get("effective_start"), "assignment.effective_start"
    )
    effective_end_raw = assignment.get("effective_end")
    effective_end = (
        None
        if effective_end_raw is None
        else _iso_date(effective_end_raw, "assignment.effective_end")
    )
    if effective_end is not None and effective_end < effective_start:
        raise ManifestError(
            f"assignment {account_id}/{strategy_id} has reversed effective dates"
        )
    return account_id, strategy_id, effective_start, effective_end


class AssignmentPerimeter:
    """The complete baseline perimeter, split before strategy calculation."""

    def __init__(
        self,
        *,
        mapped_chains: dict[str, list[dict[str, Any]]],
        non_strategy_chains: dict[str, list[dict[str, Any]]],
        full_chains: dict[str, list[dict[str, Any]]],
        full_perimeter_identity: dict[str, object],
    ) -> None:
        self.mapped_chains = mapped_chains
        self.non_strategy_chains = non_strategy_chains
        self.full_chains = full_chains
        self.full_perimeter_identity = full_perimeter_identity


def _assignment_interval_key(
    assignment: Mapping[str, Any],
    *,
    allow_null_start: bool,
) -> tuple[str, str | None, str | None]:
    account_id = _require_nonempty_text(
        assignment.get("account_id"), "assignment.account_id"
    )
    effective_start_raw = assignment.get("effective_start")
    if effective_start_raw is None and allow_null_start:
        effective_start = None
    else:
        effective_start = _iso_date(
            effective_start_raw, "assignment.effective_start"
        )
    effective_end_raw = assignment.get("effective_end")
    effective_end = (
        None
        if effective_end_raw is None
        else _iso_date(effective_end_raw, "assignment.effective_end")
    )
    if (
        effective_start is not None
        and effective_end is not None
        and effective_end < effective_start
    ):
        raise ManifestError(f"assignment {account_id} has reversed effective dates")
    return account_id, effective_start, effective_end


def _is_historical_to_mapped_shared_boundary(
    predecessor: Mapping[str, Any], successor: Mapping[str, Any]
) -> bool:
    predecessor_key = predecessor.get("_key")
    successor_key = successor.get("_key")
    return (
        isinstance(predecessor_key, tuple)
        and len(predecessor_key) == 4
        and isinstance(successor_key, tuple)
        and len(successor_key) == 4
        and predecessor.get("strategy_mapping_disposition")
        == "HISTORICAL_POOL_ONLY"
        and successor.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
        and predecessor_key[2] is not None
        and predecessor_key[3] is not None
        and predecessor_key[3] == successor_key[2]
    )


def partition_assignments(assignments: list[dict[str, Any]]) -> AssignmentPerimeter:
    """Validate the closed baseline dispositions before any strategy topology.

    Mapped rows are the only rows permitted to reach admission and calculation.
    The complete baseline remains available for evidence-coverage validation.
    """
    mapped_chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    non_strategy_chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    full_chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identity_rows: list[dict[str, object]] = []

    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ManifestError("baseline assignment must be an object")
        disposition = _require_nonempty_text(
            assignment.get("strategy_mapping_disposition"),
            "assignment.strategy_mapping_disposition",
        )
        if disposition not in _ASSIGNMENT_DISPOSITIONS:
            raise ManifestError("assignment.strategy_mapping_disposition is invalid")
        if (
            disposition == "STRATEGY_MAPPED"
            and assignment.get("effective_start") is None
        ):
            raise ManifestError(
                "mapped assignment.effective_start must be a canonical ISO date"
            )
        account_id, effective_start, effective_end = _assignment_interval_key(
            assignment,
            allow_null_start=disposition != "STRATEGY_MAPPED",
        )
        strategy_id = assignment.get("strategy_id")
        if disposition == "STRATEGY_MAPPED":
            mapped_strategy_id = _require_nonempty_text(
                strategy_id, "mapped assignment.strategy_id"
            )
            key: tuple[str, str | None, str | None, str | None] = (
                account_id,
                mapped_strategy_id,
                effective_start,
                effective_end,
            )
            target = mapped_chains
        else:
            if strategy_id is not None:
                raise ManifestError("non-strategy assignment.strategy_id must be null")
            key = (account_id, None, effective_start, effective_end)
            target = non_strategy_chains
        normalized_assignment = {**assignment, "_key": key}
        target[account_id].append(normalized_assignment)
        full_chains[account_id].append(normalized_assignment)
        identity_rows.append(
            {
                "account_id": account_id,
                "strategy_id": key[1],
                "strategy_mapping_disposition": disposition,
                "effective_start": effective_start,
                "effective_end": effective_end,
            }
        )

    for account_id, chain in full_chains.items():
        chain.sort(
            key=lambda assignment: (
                assignment["_key"][2] is not None,
                assignment["_key"][2] or "",
            )
        )
        for index, assignment in enumerate(chain):
            _, _, start, end = assignment["_key"]
            if end is None and index != len(chain) - 1:
                if start is None:
                    raise ManifestError(f"assignments overlap for account {account_id}")
                if chain[index + 1]["_key"][2] <= start:
                    raise ManifestError(f"assignments overlap for account {account_id}")
                raise ManifestError(
                    f"open-ended assignment is not last for account {account_id}"
                )
            if index == 0:
                continue
            previous_end = chain[index - 1]["_key"][3]
            if previous_end is None:
                if chain[index - 1]["_key"][2] is None:
                    raise ManifestError(f"assignments overlap for account {account_id}")
                if start <= chain[index - 1]["_key"][2]:
                    raise ManifestError(f"assignments overlap for account {account_id}")
                raise ManifestError(
                    f"open-ended assignment is not last for account {account_id}"
                )
            if start is None:
                raise ManifestError(f"assignments overlap for account {account_id}")
            previous = chain[index - 1]
            if previous_end == start and _is_historical_to_mapped_shared_boundary(
                previous, assignment
            ):
                continue
            if previous_end >= start:
                raise ManifestError(f"assignments overlap for account {account_id}")
            expected_start = (date.fromisoformat(previous_end) + timedelta(days=1)).isoformat()
            if start != expected_start:
                raise ManifestError(f"assignments have a gap for account {account_id}")

    normalized_identity_rows = sorted(
        identity_rows,
        key=lambda row: (
            str(row["account_id"]),
            row["effective_start"] is not None,
            row["effective_start"] or "",
            str(row["effective_end"]),
            str(row["strategy_id"]),
        ),
    )
    return AssignmentPerimeter(
        mapped_chains={account_id: list(chain) for account_id, chain in mapped_chains.items()},
        non_strategy_chains={
            account_id: list(chain) for account_id, chain in non_strategy_chains.items()
        },
        full_chains={account_id: list(chain) for account_id, chain in full_chains.items()},
        full_perimeter_identity={
            "sha256": _canonical_digest(normalized_identity_rows),
            "count": len(normalized_identity_rows),
        },
    )


def _assignment_identity(assignments: list[dict[str, Any]]) -> tuple[str, int]:
    normalized = [
        {
            "account_id": key[0],
            "strategy_id": key[1],
            "effective_start": key[2],
            "effective_end": key[3],
        }
        for key in sorted(_assignment_key(assignment) for assignment in assignments)
    ]
    return _canonical_digest(normalized), len(normalized)


def load_admission_policy(
    payload: Mapping[str, Any],
    policy_snapshot: InputSnapshot,
    assignments: list[dict[str, Any]],
    ledger: Mapping[str, Any],
) -> tuple[dict[tuple[str, str, str, str | None], dict[str, Any]], dict[str, object]]:
    """Load an explicit, hash-bound membership/admission policy.

    A policy is not a substitute for financial evidence.  It only identifies
    which exact baseline epochs have an admitted funded membership; when it is
    absent, incomplete, or non-bijective, this producer cannot calculate.
    """
    required = {
        "schema_version",
        "policy_id",
        "policy_version",
        "artifact_id",
        "as_of_date",
        "assignment_sha256",
        "admissions",
    }
    if set(payload) != required:
        raise ManifestError("admission policy keys are invalid")
    if payload["schema_version"] != 2:
        raise ManifestError("admission policy schema_version must be 2")
    policy_id = _require_nonempty_text(payload["policy_id"], "admission policy id")
    policy_version = _require_nonempty_text(
        payload["policy_version"], "admission policy version"
    )
    assignment_sha256, assignment_count = _assignment_identity(assignments)
    if payload["assignment_sha256"] != assignment_sha256:
        raise ManifestError("admission policy assignment identity differs")
    if not isinstance(payload["admissions"], list):
        raise ManifestError("admission policy admissions must be a list")
    nav_records: dict[str, dict[str, object]] = {}
    observations = ledger.get("nav_observations")
    if not isinstance(observations, list):
        raise ManifestError("ledger missing list section 'nav_observations'")
    for nav_index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise ManifestError(f"nav observation {nav_index} must be an object")
        record_id = _require_nonempty_text(
            observation.get("record_id"), f"nav[{nav_index}].record_id"
        )
        if record_id in nav_records:
            raise ManifestError(f"duplicate NAV record_id '{record_id}'")
        account_id = _require_nonempty_text(
            observation.get("account_id"), f"nav[{nav_index}].account_id"
        )
        as_of_date = _iso_date(
            observation.get("as_of_date"), f"nav[{nav_index}].as_of_date"
        )
        nav = observation.get("nav")
        if not isinstance(nav, Mapping):
            raise ManifestError(f"nav[{nav_index}].nav must be an object")
        nav_records[record_id] = {
            "account_id": account_id,
            "as_of_date": as_of_date,
            "status": nav.get("status"),
            "value": nav.get("value"),
        }

    admissions: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    cited_record_ids: set[str] = set()
    assignment_keys = {_assignment_key(assignment) for assignment in assignments}
    for index, admission in enumerate(payload["admissions"]):
        if not isinstance(admission, dict):
            raise ManifestError(f"admission policy admission {index} must be an object")
        common = {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
            "status",
            "continuity_to_successor",
        }
        key = _assignment_key(admission)
        if key in admissions:
            raise ManifestError(f"duplicate admission policy epoch {key}")
        if key not in assignment_keys:
            raise ManifestError("admission policy contains a non-strategy or unknown epoch")
        status = _require_nonempty_text(
            admission["status"], f"admission {index}.status"
        )
        if status not in _ADMISSION_STATUSES:
            raise ManifestError(f"admission {index}.status is invalid")
        if not isinstance(admission["continuity_to_successor"], bool):
            raise ManifestError(
                f"admission {index}.continuity_to_successor must be boolean"
            )
        if status == "BLOCKED":
            if set(admission) != common | {"reason"}:
                raise ManifestError(f"admission {index} BLOCKED keys are invalid")
            admissions[key] = {
                "status": status,
                "authority_kind": "NONE",
                "funded_nav_record_ids": [],
                "reason": _require_nonempty_text(
                    admission.get("reason"), f"admission {index}.reason"
                ),
                "continuity_to_successor": admission["continuity_to_successor"],
            }
            continue
        if set(admission) != common | {"authority_kind", "funded_nav_record_ids"}:
            raise ManifestError(f"admission {index} FUNDED keys are invalid")
        authority_kind = _require_nonempty_text(
            admission.get("authority_kind"), f"admission {index}.authority_kind"
        )
        if authority_kind not in _ADMISSION_AUTHORITY_KINDS - {"NONE"}:
            raise ManifestError(f"admission {index}.authority kind is invalid")
        record_ids = admission.get("funded_nav_record_ids")
        if not isinstance(record_ids, list) or not record_ids:
            raise ManifestError(f"admission {index}.FUNDED requires NAV record evidence")
        if not all(isinstance(record_id, str) and record_id.strip() for record_id in record_ids):
            raise ManifestError(f"admission {index}.funded_nav_record_ids is invalid")
        if len(set(record_ids)) != len(record_ids):
            raise ManifestError(f"admission {index}.funded_nav_record_ids contains duplicates")
        evidence_dates: list[str] = []
        for record_id in record_ids:
            if record_id in cited_record_ids:
                raise ManifestError(f"duplicate funded NAV citation '{record_id}'")
            cited_record_ids.add(record_id)
            record = nav_records.get(record_id)
            if record is None:
                raise ManifestError(f"funded NAV record '{record_id}' does not exist")
            if record["account_id"] != key[0]:
                raise ManifestError(f"funded NAV record '{record_id}' account differs")
            record_date = str(record["as_of_date"])
            if not _in_window(record_date, key[2], key[3]):
                raise ManifestError(f"funded NAV record '{record_id}' is outside assignment")
            if record["status"] != "AVAILABLE":
                raise ManifestError(f"funded NAV record '{record_id}' is not AVAILABLE")
            if _money(record["value"], f"funded NAV record '{record_id}'.value") <= 0:
                raise ManifestError(f"funded NAV record '{record_id}' must be strictly positive")
            evidence_dates.append(record_date)
        admissions[key] = {
            "status": status,
            "authority_kind": authority_kind,
            "funded_nav_record_ids": list(record_ids),
            "funded_nav_dates": evidence_dates,
            "continuity_to_successor": admission["continuity_to_successor"],
        }
    if set(admissions) != assignment_keys:
        raise ManifestError(
            "admission policy coverage differs from baseline assignments"
        )
    artifact = _source_artifact_from_snapshot(
        policy_snapshot,
        artifact_id=payload["artifact_id"],
        as_of_date=payload["as_of_date"],
        schema_name="account-strategy-manifest-admission-policy",
        schema_version="2",
    )
    return admissions, {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "artifact": artifact,
        "assignment": {"sha256": assignment_sha256, "count": assignment_count},
    }


def validate_assignment_topology(
    assignments: list[dict[str, Any]],
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
    full_chains: Mapping[str, list[dict[str, Any]]],
    flow_coverage: Any | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Validate epoch chronology before assigning capital to any epoch."""
    chains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str, str | None]] = set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ManifestError("baseline assignment must be an object")
        key = _assignment_key(assignment)
        if key in seen:
            raise ManifestError(f"duplicate assignment epoch {key}")
        seen.add(key)
        chains[key[0]].append({**assignment, "_key": key})
    for account_id, chain in chains.items():
        full_chain = full_chains.get(account_id)
        if not isinstance(full_chain, list):
            raise ManifestError(
                f"full assignment perimeter is missing account {account_id}"
            )
        chain.sort(key=lambda assignment: assignment["_key"][2])
        for index, assignment in enumerate(chain):
            _, _, start, end = assignment["_key"]
            admission = admissions[assignment["_key"]]
            full_index = next(
                (
                    candidate_index
                    for candidate_index, candidate in enumerate(full_chain)
                    if candidate.get("_key") == assignment["_key"]
                ),
                None,
            )
            if full_index is None:
                raise ManifestError(
                    f"mapped assignment is absent from full perimeter for account {account_id}"
                )
            if end is None and index != len(chain) - 1:
                raise ManifestError(
                    f"open-ended assignment is not last for account {account_id}"
                )
            if admission["continuity_to_successor"] and end is None:
                raise ManifestError(
                    f"open-ended assignment cannot claim a successor for account {account_id}"
                )
            if admission["continuity_to_successor"]:
                if full_index + 1 >= len(full_chain):
                    raise ManifestError(
                        f"continuity has no full-perimeter successor for account {account_id}"
                    )
                successor = full_chain[full_index + 1]
                expected_successor_start = (
                    date.fromisoformat(str(end)) + timedelta(days=1)
                ).isoformat()
                if (
                    successor.get("strategy_mapping_disposition")
                    != "STRATEGY_MAPPED"
                    or successor["_key"][2] != expected_successor_start
                ):
                    raise ManifestError(
                        f"continuity cannot cross a non-strategy interval for account {account_id}"
                    )
                successor_admission = admissions.get(successor["_key"])
                if (
                    admission.get("status") != "FUNDED"
                    or not isinstance(successor_admission, Mapping)
                    or successor_admission.get("status") != "FUNDED"
                ):
                    raise ManifestError(
                        f"continuity requires funded predecessor and successor for account {account_id}"
                    )
                if end not in admission.get("funded_nav_dates", []):
                    raise ManifestError(
                        f"continuity requires predecessor closing NAV evidence for account {account_id}"
                    )
                if flow_coverage is not None:
                    successor_dates = successor_admission.get("funded_nav_dates")
                    if (
                        not flow_coverage.is_complete_assignment(assignment)
                        or not isinstance(successor_dates, list)
                        or not successor_dates
                        or not flow_coverage.is_complete_assignment(successor)
                    ):
                        raise ManifestError(
                            f"continuity requires complete flow coverage for account {account_id}"
                        )
            if index == 0:
                continue
            previous = chain[index - 1]
            _, _, _, previous_end = previous["_key"]
            if previous_end is None:
                raise ManifestError(
                    f"open-ended assignment is not last for account {account_id}"
                )
            if previous_end >= start:
                raise ManifestError(f"assignments overlap for account {account_id}")
    return {account_id: list(chain) for account_id, chain in chains.items()}


def _mapped_gap_has_non_strategy_coverage(
    account_id: str,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    full_chains: Mapping[str, list[dict[str, Any]]],
) -> bool:
    """Prove a missing manifest-row span is entirely non-strategy coverage."""
    full_chain = full_chains.get(account_id)
    if not isinstance(full_chain, list):
        return False
    previous_key = (
        account_id,
        previous.get("strategy_id"),
        previous.get("effective_start"),
        previous.get("effective_end"),
    )
    current_key = (
        account_id,
        current.get("strategy_id"),
        current.get("effective_start"),
        current.get("effective_end"),
    )
    try:
        previous_index = next(
            index
            for index, assignment in enumerate(full_chain)
            if assignment.get("_key") == previous_key
        )
        current_index = next(
            index
            for index, assignment in enumerate(full_chain)
            if assignment.get("_key") == current_key
        )
    except StopIteration:
        return False
    if current_index <= previous_index + 1:
        return False
    previous_end = previous.get("effective_end")
    if not isinstance(previous_end, str):
        return False
    expected_start = (date.fromisoformat(previous_end) + timedelta(days=1)).isoformat()
    for assignment in full_chain[previous_index + 1 : current_index]:
        key = assignment.get("_key")
        if (
            not isinstance(key, tuple)
            or len(key) != 4
            or assignment.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
            or key[2] != expected_start
            or key[3] is None
        ):
            return False
        expected_start = (date.fromisoformat(key[3]) + timedelta(days=1)).isoformat()
    return expected_start == current.get("effective_start")


def validate_evidence_coverage(
    ledger: Mapping[str, Any],
    reconciled_events: list[dict[str, Any]],
    chains: Mapping[str, list[dict[str, Any]]],
    accounts_index: Mapping[str, Mapping[str, Any]],
) -> None:
    """Every source fact must map to exactly one known account epoch.

    No implicit historical exclusion exists in this contract.  Evidence outside
    the explicit baseline intervals is therefore a producer-blocking gap, not
    an opportunity to silently omit a number.
    """
    for account_id in chains:
        if account_id not in accounts_index:
            raise ManifestError(f"baseline assignment account is unknown: {account_id}")

    def _require_one_interval(account_id: str, event_date: str, field: str) -> None:
        if account_id not in accounts_index:
            raise ManifestError(f"{field} account is unknown: {account_id}")
        matches = [
            assignment
            for assignment in chains.get(account_id, [])
            if _in_window(
                event_date,
                assignment["_key"][2],
                assignment["_key"][3],
            )
        ]
        if len(matches) == 2:
            predecessor, successor = matches
            if (
                event_date == successor["_key"][2]
                and _is_historical_to_mapped_shared_boundary(
                    predecessor, successor
                )
            ):
                matches = [successor]
        if len(matches) != 1:
            raise ManifestError(
                f"{field} does not map to exactly one baseline assignment interval: "
                f"{account_id}/{event_date}"
            )

    observations = ledger.get("nav_observations")
    if not isinstance(observations, list):
        raise ManifestError("ledger missing list section 'nav_observations'")
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ManifestError(f"nav observation {index} must be an object")
        _require_one_interval(
            _require_nonempty_text(
                observation.get("account_id"), f"nav[{index}].account_id"
            ),
            _iso_date(observation.get("as_of_date"), f"nav[{index}].as_of_date"),
            "ledger NAV",
        )
    for event in reconciled_events:
        _require_one_interval(
            str(event["account_id"]),
            str(event["event_date"]),
            "classified flow",
        )


def _in_window(day: str, w0: str | None, w1: str | None) -> bool:
    return (w0 is None or day >= w0) and (w1 is None or day <= w1)


def _annualize_return_on_net_capital(
    return_on_net_capital: Decimal,
    span_days: int,
) -> Decimal:
    """Apply the governed piecewise annualization policy to simple RONC."""

    if span_days < 365:
        return (
            return_on_net_capital
            * Decimal(365)
            / Decimal(max(span_days, 1))
        )
    if return_on_net_capital <= Decimal(-1):
        raise ManifestError(
            "annualized RONC requires a strictly positive compounded growth base"
        )
    return (
        (Decimal(1) + return_on_net_capital)
        ** (Decimal(365) / Decimal(span_days))
        - Decimal(1)
    )


@_isolated_decimal_context
def analyze_epoch(
    window_navs: dict[str, Decimal],
    external_in_by_day: Mapping[object, Decimal],
    external_out_by_day: Mapping[object, Decimal],
    int_in_by_day: Mapping[object, Decimal],
    int_out_by_day: Mapping[object, Decimal],
    prev_closing: Decimal | None,
    w0: str,
    w1: str | None,
    prev_closing_date: str | None = None,
    *,
    opening_anchor: Mapping[str, object] | None = None,
    capital_allocation_legs: Mapping[object, Decimal] | None = None,
) -> dict[str, Any] | None:
    # Owner mandate: flows outside the assignment window never touch the
    # strategy (pre-window cash is prior-epoch business).
    def normalize_legs(source: Mapping[object, Decimal]) -> dict[tuple[str, str], Decimal]:
        legs: dict[tuple[str, str], Decimal] = {}
        for raw_key, amount in source.items():
            if isinstance(raw_key, tuple) and len(raw_key) == 2:
                event_day = _iso_date(raw_key[0], "capital flow event_date")
                recognition_day = _iso_date(
                    raw_key[1], "capital flow nav_recognition_date"
                )
            else:
                event_day = _iso_date(raw_key, "capital flow event_date")
                recognition_day = event_day
            if not _in_window(event_day, w0, w1):
                continue
            leg = (event_day, recognition_day)
            legs[leg] = legs.get(leg, Decimal(0)) + amount
        return legs

    external_in_by_day = normalize_legs(external_in_by_day)
    external_out_by_day = normalize_legs(external_out_by_day)
    int_in_by_day = normalize_legs(int_in_by_day)
    admitted_allocations: dict[tuple[str, str], Decimal] = {}
    for raw_key, amount in (capital_allocation_legs or {}).items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            raise ManifestError("capital allocation leg identity is invalid")
        event_day = _iso_date(raw_key[0], "capital allocation event_date")
        recognition_day = _iso_date(
            raw_key[1], "capital allocation nav_recognition_date"
        )
        if recognition_day < event_day:
            raise ManifestError("capital allocation recognition precedes economic event")
        if not _in_window(event_day, w0, w1):
            continue
        leg = (event_day, recognition_day)
        admitted_allocations[leg] = admitted_allocations.get(leg, Decimal(0)) + amount
    for leg, amount in admitted_allocations.items():
        int_in_by_day[leg] = int_in_by_day.get(leg, Decimal(0)) + amount
    int_out_by_day = normalize_legs(int_out_by_day)
    for flow_class, source in (
        ("EXTERNAL_IN", external_in_by_day),
        ("EXTERNAL_OUT", external_out_by_day),
        ("INTERNAL_IN", int_in_by_day),
        ("INTERNAL_OUT", int_out_by_day),
    ):
        for (event_date, nav_recognition_date), amount in source.items():
            if amount <= 0:
                raise ManifestError(
                    f"{flow_class} on {event_date} must be a positive capital flow"
                )
    # The only permitted NAV trimming is the deterministic leading pre-funding
    # prefix.  Once a strictly-positive AVAILABLE NAV is observed, every later
    # observation is a return base and must therefore itself be positive.
    all_days = sorted(window_navs)
    first_funded = next(
        (day for day in all_days if window_navs[day] > 0),
        None,
    )
    if first_funded is None:
        return None
    if prev_closing is None and opening_anchor is None:
        # FIRST_AVAILABLE_NAV is a complete opening observation.  Any flow
        # recognized on or before it is already represented in that NAV, so it
        # is not an epoch movement, trace entry, or return-link obligation.
        def post_opening_legs(
            source: Mapping[tuple[str, str], Decimal],
        ) -> dict[tuple[str, str], Decimal]:
            return {
                leg: amount
                for leg, amount in source.items()
                if leg[1] > first_funded
            }

        external_in_by_day = post_opening_legs(external_in_by_day)
        external_out_by_day = post_opening_legs(external_out_by_day)
        int_in_by_day = post_opening_legs(int_in_by_day)
        int_out_by_day = post_opening_legs(int_out_by_day)
    capital_flow_trace: list[dict[str, Any]] = []
    for flow_class, source in (
        ("EXTERNAL_IN", external_in_by_day),
        ("EXTERNAL_OUT", external_out_by_day),
        ("INTERNAL_IN", int_in_by_day),
        ("INTERNAL_OUT", int_out_by_day),
    ):
        for (event_date, nav_recognition_date), amount in source.items():
            capital_flow_trace.append(
                {
                    "class": flow_class,
                    "event_date": event_date,
                    "nav_recognition_date": nav_recognition_date,
                    "amount": amount,
                }
            )
    capital_flow_trace.sort(
        key=lambda entry: (
            str(entry["event_date"]),
            str(entry["nav_recognition_date"]),
            str(entry["class"]),
        )
    )
    net_flow_by_leg: dict[tuple[str, str], Decimal] = {}
    for source, sign in (
        (external_in_by_day, 1),
        (external_out_by_day, -1),
        (int_in_by_day, 1),
        (int_out_by_day, -1),
    ):
        for leg, value in source.items():
            net_flow_by_leg[leg] = net_flow_by_leg.get(leg, Decimal(0)) + sign * value

    funded_days = [day for day in all_days if day >= first_funded]
    days: list[str] = []
    day_index = 0
    while day_index < len(funded_days):
        current_day = funded_days[day_index]
        current_nav = window_navs[current_day]
        if current_nav < 0:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        if current_nav > 0:
            days.append(current_day)
            day_index += 1
            continue

        # A zero NAV is only omittable when it is a bounded liquidation run:
        # its first zero exactly reconciles the preceding positive base, the
        # run itself carries no later flow, and a later positive NAV resumes.
        if not days:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        prior_positive_day = days[-1]
        first_zero_day = current_day
        zero_end = day_index
        while (
            zero_end + 1 < len(funded_days)
            and window_navs[funded_days[zero_end + 1]] == 0
        ):
            zero_end += 1
        if zero_end + 1 >= len(funded_days) or window_navs[
            funded_days[zero_end + 1]
        ] <= 0:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        transition_flow = sum(
            (
                value
                for (_event_day, recognition_day), value in net_flow_by_leg.items()
                if prior_positive_day < recognition_day <= first_zero_day
            ),
            Decimal(0),
        )
        zero_run_end = funded_days[zero_end]
        if (
            window_navs[prior_positive_day] + transition_flow != 0
            or any(
                first_zero_day < recognition_day <= zero_run_end
                for _event_day, recognition_day in net_flow_by_leg
            )
        ):
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        day_index = zero_end + 1
    if len(days) < 3:
        return None
    closing_nav_date = days[-1]
    if any(
        event_date > closing_nav_date
        for source_days in (
            external_in_by_day,
            external_out_by_day,
            int_in_by_day,
            int_out_by_day,
        )
        for event_date, _recognition_date in source_days
    ):
        # Fail-closed (reviewer P1): a flow after the last funded NAV would
        # corrupt the money identity while being absent from Current. The
        # epoch stays unavailable until a covering NAV observation exists.
        return {"unavailable_status": "FLOWS_AFTER_CLOSING_NAV"}

    if prev_closing is not None and opening_anchor is not None:
        raise ManifestError("provider opening anchor conflicts with predecessor continuity")
    anchor_event_date: str | None = None
    anchor_recognition_date: str | None = None
    if opening_anchor is not None:
        anchor_event_date = _iso_date(
            opening_anchor.get("event_date"), "opening anchor event_date"
        )
        anchor_recognition_date = _iso_date(
            opening_anchor.get("nav_recognition_date"),
            "opening anchor nav_recognition_date",
        )
        opening = _money(opening_anchor.get("amount"), "opening anchor amount")
        if (
            opening <= 0
            or not _in_window(anchor_recognition_date, w0, w1)
            or anchor_recognition_date < anchor_event_date
        ):
            raise ManifestError("opening anchor is invalid or outside epoch")
        opening_basis = "PROVIDER_CAPITAL_ANCHOR"
    else:
        opening = prev_closing if prev_closing is not None else window_navs[first_funded]
        opening_basis = (
            "PREDECESSOR_CLOSING_NAV"
            if prev_closing is not None
            else "FIRST_AVAILABLE_NAV"
        )
    if prev_closing is not None and prev_closing_date is None:
        raise ManifestError(
            "predecessor closing date is required for observation trace"
        )
    # An independently inferred opening absorbs recognized opening cash.  A
    # trusted predecessor or provider-anchor opening instead includes every
    # epoch flow, including one recognized on the first observed day.
    include_first_day_flow = prev_closing is not None or opening_anchor is not None
    dep = sum(
        (
            v
            for (_event_day, recognition_day), v in external_in_by_day.items()
            if include_first_day_flow or recognition_day > first_funded
        ),
        Decimal(0),
    )
    wd = sum(
        (
            v
            for (_event_day, recognition_day), v in external_out_by_day.items()
            if include_first_day_flow or recognition_day > first_funded
        ),
        Decimal(0),
    )
    int_in = sum(
        (
            v
            for (_event_day, recognition_day), v in int_in_by_day.items()
            if include_first_day_flow or recognition_day > first_funded
        ),
        Decimal(0),
    )
    int_out = sum(
        (
            v
            for (_event_day, recognition_day), v in int_out_by_day.items()
            if include_first_day_flow or recognition_day > first_funded
        ),
        Decimal(0),
    )

    invested = opening + dep + int_in
    withdrawn = wd + int_out
    net_capital = invested - withdrawn
    current = window_navs[days[-1]]
    gain = current - net_capital

    linked_flow_legs: set[tuple[str, str]] = set()

    def _segment_flow(start_exclusive: str, end_inclusive: str) -> Decimal:
        # Every intervening flow date counts, including dates that carry no
        # NAV observation of their own (reviewer P1).
        return sum(
            (
                value
                for leg, value in net_flow_by_leg.items()
                if start_exclusive < leg[1] <= end_inclusive
            ),
            Decimal(0),
        )

    # Return is linked only between the source observations.  All dated flow
    # legs use the explicit end-of-observation-interval convention; this is
    # not daily valuation or a timing-sensitive return measure.
    index = Decimal(100)
    hwm = Decimal(100)
    max_dd = Decimal(0)
    trace_links: list[dict[str, Any]] = []

    def _apply_observation_link(
        start_date: str, end_date: str, base: Decimal, ending: Decimal, flow: Decimal
    ) -> dict[str, Any] | None:
        nonlocal index, hwm, max_dd
        if base <= 0:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        factor = Decimal(1) + (ending - base - flow) / base
        if factor <= 0:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_OBSERVATION_LINK",
            }
        index *= factor
        hwm = max(hwm, index)
        drawdown = Decimal(1) - index / hwm
        max_dd = max(max_dd, drawdown)
        trace_links.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "base_nav": base,
                "ending_nav": ending,
                "net_flow": flow,
                "factor": factor,
                "cumulative_index": index,
                "running_hwm_index": hwm,
                "drawdown": drawdown,
            }
        )
        return None

    if prev_closing is not None:
        assert prev_closing_date is not None
        blocked = _apply_observation_link(
            prev_closing_date,
            first_funded,
            prev_closing,
            window_navs[first_funded],
            _segment_flow(prev_closing_date, first_funded),
        )
        if blocked is not None:
            return blocked
        linked_flow_legs.update(
            leg
            for leg in net_flow_by_leg
            if prev_closing_date < leg[1] <= first_funded
        )
    elif opening_anchor is not None:
        assert anchor_event_date is not None and anchor_recognition_date is not None
        if anchor_recognition_date != first_funded or anchor_event_date > anchor_recognition_date:
            return {
                "unavailable_status": "BLOCKED_RETURN_LINKAGE",
                "blocking_reason": "OPENING_ANCHOR_RECOGNITION_NOT_FIRST_NAV",
            }
        if anchor_event_date < anchor_recognition_date:
            anchor_interval_flow = _segment_flow(
                anchor_event_date, anchor_recognition_date
            )
            blocked = _apply_observation_link(
                anchor_event_date,
                anchor_recognition_date,
                opening,
                window_navs[first_funded],
                anchor_interval_flow,
            )
            if blocked is not None:
                return blocked
            linked_flow_legs.update(
                leg
                for leg in net_flow_by_leg
                if anchor_event_date < leg[1] <= anchor_recognition_date
            )
    for prev_d, cur_d in zip(days, days[1:]):
        v0, v1 = window_navs[prev_d], window_navs[cur_d]
        flow = _segment_flow(prev_d, cur_d)
        blocked = _apply_observation_link(prev_d, cur_d, v0, v1, flow)
        if blocked is not None:
            return blocked
        linked_flow_legs.update(
            leg for leg in net_flow_by_leg if prev_d < leg[1] <= cur_d
        )
    if linked_flow_legs != set(net_flow_by_leg):
        return {
            "unavailable_status": "BLOCKED_RETURN_LINKAGE",
            "blocking_reason": "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL",
        }
    observation_linked_return = index / Decimal(100) - Decimal(1)

    opening_cashflow_date = (
        prev_closing_date
        if prev_closing is not None
        else anchor_event_date if opening_anchor is not None else days[0]
    )
    assert opening_cashflow_date is not None
    span_days = (
        date.fromisoformat(days[-1]) - date.fromisoformat(opening_cashflow_date)
    ).days
    # This is not money-weighted return: it ignores flow timing.  Preserve the
    # useful simple ratio under its accurate name and keep XIRR diagnostic-only
    # until a governed timing-sensitive return policy is admitted.
    return_on_net_capital = gain / net_capital if net_capital > 0 else None
    if return_on_net_capital is None:
        annualized = None
    else:
        annualized = _annualize_return_on_net_capital(
            return_on_net_capital,
            span_days,
        )

    closing_cashflows: list[tuple[date, Decimal]] = [
        (date.fromisoformat(opening_cashflow_date), -opening)
    ]
    for source, sign in (
        (external_in_by_day, -1),
        (external_out_by_day, 1),
        (int_in_by_day, -1),
        (int_out_by_day, 1),
    ):
        for (event_day, recognition_day), v in source.items():
            if (
                include_first_day_flow or recognition_day > first_funded
            ) and event_day <= days[-1]:
                closing_cashflows.append((date.fromisoformat(event_day), sign * v))
    closing_cashflows.append((date.fromisoformat(days[-1]), current))
    xirr_rate = xirr(closing_cashflows)

    return {
        "start": first_funded,
        "end": days[-1],
        "opening_capital": opening,
        "deposits": dep,
        "internal_in": int_in,
        "invested": invested,
        "external_withdrawals": wd,
        "internal_out": int_out,
        "withdrawn": withdrawn,
        "net_capital": net_capital,
        "current_value": current,
        "gain": gain,
        "return_on_net_capital": return_on_net_capital,
        "annualized_return_on_net_capital": annualized,
        "xirr_diagnostic": xirr_rate,
        "observation_linked_return": observation_linked_return,
        "observed_hwm_index": hwm,
        "observed_max_drawdown": max_dd,
        "nav_observations": len(days),
        "opening_basis": opening_basis,
        "observation_trace": {"links": trace_links},
        "capital_flow_trace": capital_flow_trace,
    }


def _assemble_manifest_impl(
    series: dict[str, dict[str, Decimal]],
    accounts_index: dict[str, dict[str, Any]],
    buckets: dict[str, dict[str, dict[str, Decimal]]],
    chains: Mapping[str, list[dict[str, Any]]],
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
    source_identities: Mapping[str, object],
    flow_coverage: Any,
    unavailable_navs: Mapping[str, set[str]] | None = None,
    *,
    manifest_schema_version: int = _MANIFEST_SCHEMA_VERSION,
    capital_anchors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    unavailable_navs = unavailable_navs or {}
    capital_anchors = capital_anchors or []
    for aid, chain in sorted(chains.items()):
        provider = accounts_index.get(aid, {}).get("provider", "")
        prev_closing: Decimal | None = None
        prev_closing_date: str | None = None
        for index, assignment in enumerate(chain):
            key = assignment["_key"]
            strategy_id = key[1]
            w0 = key[2]
            w1 = key[3]
            admission = admissions[key]
            base: dict[str, Any] = {
                "account_id": aid,
                "provider": provider,
                "strategy_id": strategy_id,
                "effective_start": w0,
                "effective_end": w1,
                "admission_status": admission["status"],
                "admission_policy_reference": admission["authority_kind"],
                "admission_authority_kind": admission["authority_kind"],
                "funded_nav_record_ids": list(admission["funded_nav_record_ids"]),
                "continuity_to_successor": admission["continuity_to_successor"],
            }
            previous_claimed_continuity = (
                index > 0
                and admissions[chain[index - 1]["_key"]]["continuity_to_successor"]
            )
            if previous_claimed_continuity and prev_closing is None:
                rows.append(
                    {
                        **base,
                        "status": "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
                        "blocking_reason": (
                            "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE"
                        ),
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            if admission["status"] != "FUNDED":
                rows.append(
                    {
                        **base,
                        "status": "BLOCKED_ADMISSION",
                        "blocking_reason": admission.get(
                            "reason", "EPOCH_NOT_EXPLICITLY_FUNDED"
                        ),
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            window = {
                d: v
                for d, v in series.get(aid, {}).items()
                if d >= w0 and (w1 is None or d <= w1)
            }
            final_nav_date = max(window, default=None)
            if final_nav_date is not None and not flow_coverage.is_complete(
                aid, w0, final_nav_date
            ):
                rows.append(
                    {
                        **base,
                        "status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
                        "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            if any(_in_window(day, w0, w1) for day in unavailable_navs.get(aid, set())):
                rows.append(
                    {
                        **base,
                        "status": "BLOCKED_UNAVAILABLE_NAV_OBSERVATION",
                        "blocking_reason": "UNAVAILABLE_NAV_OBSERVATION_IN_EPOCH",
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            opening_anchors = [
                anchor
                for anchor in capital_anchors
                if anchor["account_id"] == aid
                and anchor["strategy_id"] == strategy_id
                and anchor["capital_type"] == "OPENING_CAPITAL_ANCHOR"
                and _in_window(str(anchor["nav_recognition_date"]), w0, w1)
            ]
            allocation_anchors = [
                anchor
                for anchor in capital_anchors
                if anchor["account_id"] == aid
                and anchor["strategy_id"] == strategy_id
                and anchor["capital_type"] == "STRATEGY_CAPITAL_ALLOCATION"
                and _in_window(str(anchor["event_date"]), w0, w1)
            ]
            if manifest_schema_version == _CURRENT_MANIFEST_SCHEMA_VERSION:
                if prev_closing is None and len(opening_anchors) > 1:
                    raise ManifestError(
                        "funded current-product epoch permits at most one opening capital anchor"
                    )
                if prev_closing is not None and opening_anchors:
                    raise ManifestError(
                        "successor continuity cannot also consume an opening capital anchor"
                    )
            local_internal_in = dict(buckets["internal_in"].get(aid, {}))
            allocation_legs: dict[tuple[str, str], Decimal] = {}
            for anchor in allocation_anchors:
                leg = (
                    str(anchor["event_date"]),
                    str(anchor["nav_recognition_date"]),
                )
                allocation_legs[leg] = (
                    allocation_legs.get(leg, Decimal(0)) + anchor["amount"]
                )
            stats = analyze_epoch(
                window,
                buckets["external_in"].get(aid, {}),
                buckets["external_out"].get(aid, {}),
                local_internal_in,
                buckets["internal_out"].get(aid, {}),
                prev_closing,
                w0,
                w1,
                prev_closing_date,
                opening_anchor=opening_anchors[0] if opening_anchors else None,
                capital_allocation_legs=allocation_legs,
            )
            if stats is not None and stats.get("unavailable_status"):
                unavailable_row = {
                    **base,
                    "status": stats["unavailable_status"],
                }
                if "blocking_reason" in stats:
                    unavailable_row["blocking_reason"] = stats["blocking_reason"]
                rows.append(unavailable_row)
                prev_closing = None
                prev_closing_date = None
                continue
            if stats is None:
                rows.append(
                    {
                        **base,
                        "status": "NO_ADMITTED_NAV",
                        "blocking_reason": "FEWER_THAN_THREE_AVAILABLE_NAV_OBSERVATIONS",
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            if (
                admission["continuity_to_successor"]
                and index < len(chain) - 1
                and stats["withdrawn"] != 0
            ):
                rows.append(
                    {
                        **base,
                        "status": "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL",
                        "blocking_reason": (
                            "POLICY_CLAIMS_CONTINUITY_BUT_EPOCH_HAS_WITHDRAWALS"
                        ),
                    }
                )
                prev_closing = None
                prev_closing_date = None
                continue
            if manifest_schema_version in {7, 8}:
                stats["capital_flow_trace"] = [
                    {
                        "class": flow["class"],
                        "date": flow["event_date"],
                        "amount": flow["amount"],
                    }
                    for flow in stats["capital_flow_trace"]
                ]
            stats.update({**base, "status": "OK"})
            rows.append(stats)
            prev_closing = (
                stats["current_value"]
                if admission["continuity_to_successor"] and index < len(chain) - 1
                else None
            )
            prev_closing_date = (
                stats["end"]
                if admission["continuity_to_successor"] and index < len(chain) - 1
                else None
            )

    return {
        "schema_version": manifest_schema_version,
        "artifact": "account-strategy-manifest",
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "source_identities": dict(source_identities),
        "epoch_continuity": {
            "rule_id": "EXPLICIT_FUNDED_EPOCH_CONTINUITY_V1",
            "status": "POLICY_BOUND_ONLY",
            "rule": (
                "An epoch closing NAV opens only its immediately continuous, "
                "explicitly funded successor. A missing/blocked predecessor "
                "closing NAV blocks that successor; no capital is inferred."
            ),
        },
        "flow_reconciliation": {
            "status": "ONE_TO_ONE_VERIFIED",
            "method": "EXACT_RAW_RECORD_TO_CLASSIFIED_EVENT_BINDING",
        },
        "rows": rows,
    }


MONEY_FIELDS = (
    "invested",
    "withdrawn",
    "net_capital",
    "current_value",
    "gain",
)
PCT_FIELDS = (
    "return_on_net_capital",
    "annualized_return_on_net_capital",
    "observation_linked_return",
    "observed_max_drawdown",
)
_MANIFEST_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "account-strategy-manifest.schema.json"
)


def _manifest_decimal(raw: object, field: str, *, money: bool = False) -> Decimal:
    if not isinstance(raw, str):
        raise ManifestError(f"{field} must be a canonical decimal string")
    value = _dec(raw, field)
    if raw != _fixed_decimal(value):
        raise ManifestError(f"{field} is not canonically spelled")
    return value


@_isolated_decimal_context
def _validate_manifest_semantics(
    manifest: Mapping[str, Any],
    *,
    full_chains: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    schema_version = manifest.get("schema_version")
    if schema_version not in {7, 8, 9}:
        raise ManifestError("manifest schema version is invalid")
    source_identities = manifest.get("source_identities")
    if not isinstance(source_identities, Mapping):
        raise ManifestError("manifest source identities must be an object")
    classified = source_identities.get("classified_flows")
    classification_policy = source_identities.get("flow_classification_policy")
    flow_coverage = source_identities.get("flow_coverage")
    admission_policy = source_identities.get("admission_policy")
    if (
        not isinstance(classified, Mapping)
        or not isinstance(classification_policy, Mapping)
        or not isinstance(admission_policy, Mapping)
    ):
        raise ManifestError("manifest source identities are incomplete")
    if classified.get("ledger_identity") != source_identities.get("ledger"):
        raise ManifestError("classified ledger identity differs from ledger identity")
    if flow_coverage is not None:
        if not isinstance(flow_coverage, Mapping):
            raise ManifestError("flow coverage source identity is invalid")
        if flow_coverage.get("ledger_identity") != source_identities.get("ledger"):
            raise ManifestError(
                "flow coverage ledger identity differs from ledger identity"
            )
        if flow_coverage.get("classified_flows_artifact") != classified.get("artifact"):
            raise ManifestError(
                "flow coverage classified artifact differs from classified flows"
            )
    if source_identities.get("assignment") != admission_policy.get("assignment"):
        raise ManifestError("assignment identity differs from admission policy")
    policy_artifact = classification_policy.get("artifact")
    policy_reference = classified.get("classification_policy")
    if not isinstance(policy_artifact, Mapping) or not isinstance(
        policy_reference, Mapping
    ):
        raise ManifestError("flow classification policy identity is incomplete")
    if policy_reference != {
        "policy_id": classification_policy.get("policy_id"),
        "policy_version": classification_policy.get("policy_version"),
        "policy_as_of_date": policy_artifact.get("as_of_date"),
        "policy_sha256": policy_artifact.get("sha256"),
    }:
        raise ManifestError(
            "classified flow policy reference differs from policy authority"
        )
    rows = manifest.get("rows")
    if not isinstance(rows, list):
        raise ManifestError("manifest rows must be a list")
    seen_epochs: set[tuple[str, str, str, str | None]] = set()
    for index, value in enumerate(rows):
        if not isinstance(value, Mapping):
            raise ManifestError(f"manifest row {index} must be an object")
        row = value
        effective_start = _iso_date(
            row.get("effective_start"), f"row[{index}].effective_start"
        )
        effective_end_raw = row.get("effective_end")
        effective_end = (
            None
            if effective_end_raw is None
            else _iso_date(effective_end_raw, f"row[{index}].effective_end")
        )
        if effective_end is not None and effective_end < effective_start:
            raise ManifestError(f"row {index} effective interval is reversed")
        epoch_key = (
            _require_nonempty_text(row.get("account_id"), f"row[{index}].account_id"),
            _require_nonempty_text(row.get("strategy_id"), f"row[{index}].strategy_id"),
            effective_start,
            effective_end,
        )
        if epoch_key in seen_epochs:
            raise ManifestError(f"duplicate manifest epoch {epoch_key}")
        seen_epochs.add(epoch_key)
        admission_status = row.get("admission_status")
        admission_kind = row.get("admission_authority_kind")
        funded_record_ids = row.get("funded_nav_record_ids")
        if row.get("admission_policy_reference") != admission_kind:
            raise ManifestError(f"row {index} admission provenance reference is invalid")
        if not isinstance(funded_record_ids, list) or not all(
            isinstance(record_id, str) and record_id for record_id in funded_record_ids
        ) or len(set(funded_record_ids)) != len(funded_record_ids):
            raise ManifestError(f"row {index} funded NAV record provenance is invalid")
        if admission_status == "FUNDED":
            if (
                admission_kind != "LEDGER_AVAILABLE_POSITIVE_NAV_V1"
                or not funded_record_ids
            ):
                raise ManifestError(f"row {index} funded admission provenance is invalid")
        elif admission_status == "BLOCKED":
            if admission_kind != "NONE" or funded_record_ids:
                raise ManifestError(f"row {index} blocked admission provenance is invalid")
        else:
            raise ManifestError(f"row {index} admission status is invalid")
        status = row.get("status")
        if status == "FLOWS_AFTER_CLOSING_NAV":
            forbidden_fields = (
                "late_flows", "start", "end", "opening_capital", "deposits",
                "internal_in", "invested", "external_withdrawals", "internal_out",
                "withdrawn", "net_capital", "current_value", "gain",
                "return_on_net_capital", "annualized_return_on_net_capital",
                "xirr_diagnostic", "observation_linked_return", "observed_hwm_index",
                "observed_max_drawdown", "opening_basis", "observation_trace",
                "capital_flow_trace", "nav_observation_trace", "nav_observations",
            )
            if any(field in row for field in forbidden_fields):
                raise ManifestError(f"row {index} blocked late-flow row has evidence")
            continue

        if status != "OK":
            continue

        start = _iso_date(row.get("start"), f"row[{index}].start")
        end = _iso_date(row.get("end"), f"row[{index}].end")
        if (
            start > end
            or start < effective_start
            or (effective_end is not None and end > effective_end)
        ):
            raise ManifestError(f"row {index} observation interval is invalid")

        capital_flow_trace = row.get("capital_flow_trace")
        if not isinstance(capital_flow_trace, list):
            raise ManifestError(f"row {index} capital flow trace is invalid")
        gross_flows = {
            "EXTERNAL_IN": Decimal(0),
            "EXTERNAL_OUT": Decimal(0),
            "INTERNAL_IN": Decimal(0),
            "INTERNAL_OUT": Decimal(0),
        }
        signed_flow_legs: dict[tuple[str, str, str], Decimal] = {}
        seen_capital_flows: set[tuple[str, str, str]] = set()
        for flow_index, flow in enumerate(capital_flow_trace):
            if not isinstance(flow, Mapping):
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            flow_class = flow.get("class")
            if flow_class not in gross_flows:
                raise ManifestError(f"row {index} capital flow trace class is invalid")
            if schema_version == 9:
                if set(flow) != {
                    "class", "event_date", "nav_recognition_date", "amount"
                }:
                    raise ManifestError(
                        f"row {index} capital flow trace entry is invalid"
                    )
                event_date = _iso_date(
                    flow.get("event_date"),
                    f"row[{index}].capital_flow_trace[{flow_index}].event_date",
                )
                recognition_date = _iso_date(
                    flow.get("nav_recognition_date"),
                    f"row[{index}].capital_flow_trace[{flow_index}].nav_recognition_date",
                )
            else:
                if set(flow) != {"class", "date", "amount"}:
                    raise ManifestError(
                        f"row {index} capital flow trace entry is invalid"
                    )
                event_date = _iso_date(
                    flow.get("date"),
                    f"row[{index}].capital_flow_trace[{flow_index}].date",
                )
                recognition_date = event_date
            in_economic_window = _in_window(
                event_date, effective_start, effective_end
            )
            if not in_economic_window:
                raise ManifestError(
                    f"row {index} capital flow trace event date is outside epoch"
                )
            amount = _manifest_decimal(
                flow.get("amount"),
                f"row[{index}].capital_flow_trace[{flow_index}].amount",
                money=True,
            )
            leg_key = (str(flow_class), event_date, recognition_date)
            if amount <= 0 or leg_key in seen_capital_flows:
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            seen_capital_flows.add(leg_key)
            gross_flows[flow_class] += amount
            sign = 1 if flow_class in {"EXTERNAL_IN", "INTERNAL_IN"} else -1
            signed_flow_legs[leg_key] = sign * amount

        money = {
            field: _manifest_decimal(
                row.get(field), f"row[{index}].{field}", money=True
            )
            for field in (
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
            )
        }
        if (
            money["opening_capital"] + money["deposits"] + money["internal_in"]
            != money["invested"]
        ):
            raise ManifestError(f"row {index} money identity for invested is invalid")
        if money["external_withdrawals"] + money["internal_out"] != money["withdrawn"]:
            raise ManifestError(f"row {index} money identity for withdrawn is invalid")
        if money["invested"] - money["withdrawn"] != money["net_capital"]:
            raise ManifestError(
                f"row {index} money identity for net capital is invalid"
            )
        if money["current_value"] - money["net_capital"] != money["gain"]:
            raise ManifestError(f"row {index} money identity for gain is invalid")
        if (
            money["deposits"] != gross_flows["EXTERNAL_IN"]
            or money["external_withdrawals"] != gross_flows["EXTERNAL_OUT"]
            or money["internal_in"] != gross_flows["INTERNAL_IN"]
            or money["internal_out"] != gross_flows["INTERNAL_OUT"]
        ):
            raise ManifestError(f"row {index} capital flow trace totals are invalid")

        expected_ronc = (
            money["gain"] / money["net_capital"] if money["net_capital"] > 0 else None
        )
        ronc_raw = row.get("return_on_net_capital")
        ronc = (
            None
            if ronc_raw is None
            else _manifest_decimal(ronc_raw, f"row[{index}].return_on_net_capital")
        )
        if ronc != expected_ronc:
            raise ManifestError(f"row {index} RONC identity is invalid")
        opening_basis = row.get("opening_basis")
        annualization_start = start
        if opening_basis in {
            "PREDECESSOR_CLOSING_NAV", "PROVIDER_CAPITAL_ANCHOR"
        }:
            trace_for_span = row.get("observation_trace")
            if not isinstance(trace_for_span, Mapping) or not isinstance(
                trace_for_span.get("links"), list
            ):
                raise ManifestError(f"row {index} observation trace is invalid")
            links_for_span = trace_for_span["links"]
            if not links_for_span or not isinstance(links_for_span[0], Mapping):
                raise ManifestError(
                    f"row {index} predecessor observation trace is invalid"
                )
            annualization_start = _iso_date(
                links_for_span[0].get("start_date"),
                f"row[{index}].observation_trace[0].start_date",
            )
        span_days = (
            date.fromisoformat(end) - date.fromisoformat(annualization_start)
        ).days
        expected_annualized = (
            None
            if ronc is None
            else _annualize_return_on_net_capital(ronc, span_days)
        )
        annualized_raw = row.get("annualized_return_on_net_capital")
        annualized = (
            None
            if annualized_raw is None
            else _manifest_decimal(
                annualized_raw,
                f"row[{index}].annualized_return_on_net_capital",
            )
        )
        if annualized != expected_annualized:
            raise ManifestError(f"row {index} annualized RONC identity is invalid")
        if row.get("xirr_diagnostic") is not None:
            _manifest_decimal(row["xirr_diagnostic"], f"row[{index}].xirr_diagnostic")

        linked_return = _manifest_decimal(
            row.get("observation_linked_return"),
            f"row[{index}].observation_linked_return",
        )
        observed_hwm = _manifest_decimal(
            row.get("observed_hwm_index"), f"row[{index}].observed_hwm_index"
        )
        observed_drawdown = _manifest_decimal(
            row.get("observed_max_drawdown"),
            f"row[{index}].observed_max_drawdown",
        )
        if observed_drawdown < 0:
            raise ManifestError(f"row {index} observed max drawdown is negative")
        trace = row.get("observation_trace")
        if not isinstance(trace, Mapping) or not isinstance(trace.get("links"), list):
            raise ManifestError(f"row {index} observation trace is invalid")
        links = trace["links"]
        expected_link_count = int(row["nav_observations"]) - (
            1 if opening_basis == "FIRST_AVAILABLE_NAV" else 0
        )
        if len(links) != expected_link_count:
            raise ManifestError(f"row {index} observation trace link count is invalid")

        cumulative_index = Decimal(100)
        running_hwm = Decimal(100)
        max_drawdown = Decimal(0)
        prior_end: str | None = None
        prior_ending_nav: Decimal | None = None
        linked_flow_legs: set[tuple[str, str, str]] = set()
        for link_index, link in enumerate(links):
            if not isinstance(link, Mapping):
                raise ManifestError(f"row {index} observation trace link is invalid")
            start_date = _iso_date(
                link.get("start_date"),
                f"row[{index}].observation_trace[{link_index}].start_date",
            )
            end_date = _iso_date(
                link.get("end_date"),
                f"row[{index}].observation_trace[{link_index}].end_date",
            )
            if start_date >= end_date or (
                prior_end is not None and start_date != prior_end
            ):
                raise ManifestError(f"row {index} observation trace dates are invalid")
            if link_index == 0:
                if opening_basis == "FIRST_AVAILABLE_NAV" and start_date != start:
                    raise ManifestError(
                        f"row {index} observation trace opening is invalid"
                    )
                if opening_basis in {
                    "PREDECESSOR_CLOSING_NAV", "PROVIDER_CAPITAL_ANCHOR"
                } and (
                    start_date >= start or end_date != start
                ):
                    raise ManifestError(
                        f"row {index} observation trace predecessor is invalid"
                    )
            base_nav = _manifest_decimal(
                link.get("base_nav"),
                f"row[{index}].observation_trace[{link_index}].base_nav",
                money=True,
            )
            ending_nav = _manifest_decimal(
                link.get("ending_nav"),
                f"row[{index}].observation_trace[{link_index}].ending_nav",
                money=True,
            )
            if prior_ending_nav is not None and base_nav != prior_ending_nav:
                raise ManifestError(
                    f"row {index} observation trace NAV adjacency is invalid"
                )
            net_flow = _manifest_decimal(
                link.get("net_flow"),
                f"row[{index}].observation_trace[{link_index}].net_flow",
                money=True,
            )
            interval_flow_legs = {
                leg
                for leg in signed_flow_legs
                if start_date < leg[2] <= end_date
            }
            expected_net_flow = sum(
                (signed_flow_legs[leg] for leg in interval_flow_legs),
                Decimal(0),
            )
            if net_flow != expected_net_flow or linked_flow_legs & interval_flow_legs:
                raise ManifestError(
                    f"row {index} observation trace net flow is invalid"
                )
            linked_flow_legs.update(interval_flow_legs)
            factor = _manifest_decimal(
                link.get("factor"),
                f"row[{index}].observation_trace[{link_index}].factor",
            )
            if base_nav <= 0:
                raise ManifestError(f"row {index} observation trace base is invalid")
            expected_factor = Decimal(1) + (ending_nav - base_nav - net_flow) / base_nav
            if factor != expected_factor or factor <= 0:
                raise ManifestError(f"row {index} observation trace factor is invalid")
            cumulative_index *= factor
            running_hwm = max(running_hwm, cumulative_index)
            drawdown = Decimal(1) - cumulative_index / running_hwm
            max_drawdown = max(max_drawdown, drawdown)
            if (
                _manifest_decimal(
                    link.get("cumulative_index"),
                    f"row[{index}].observation_trace[{link_index}].cumulative_index",
                )
                != cumulative_index
                or _manifest_decimal(
                    link.get("running_hwm_index"),
                    f"row[{index}].observation_trace[{link_index}].running_hwm_index",
                )
                != running_hwm
                or _manifest_decimal(
                    link.get("drawdown"),
                    f"row[{index}].observation_trace[{link_index}].drawdown",
                )
                != drawdown
            ):
                raise ManifestError(f"row {index} observation trace index is invalid")
            prior_end = end_date
            prior_ending_nav = ending_nav
        if prior_end != end or links[-1].get("ending_nav") != row.get("current_value"):
            raise ManifestError(
                f"row {index} observation trace current value is invalid"
            )
        if linked_flow_legs != set(signed_flow_legs):
            raise ManifestError(
                f"row {index} capital flow trace does not map to a link"
            )
        if (
            linked_return != cumulative_index / Decimal(100) - Decimal(1)
            or observed_hwm != running_hwm
            or observed_drawdown != max_drawdown
        ):
            raise ManifestError(f"row {index} observation trace summary is invalid")
        opening_cashflow_date = (
            links[0].get("start_date")
            if opening_basis in {
                "PREDECESSOR_CLOSING_NAV", "PROVIDER_CAPITAL_ANCHOR"
            }
            else start
        )
        if not isinstance(opening_cashflow_date, str):
            raise ManifestError(f"row {index} XIRR opening date is invalid")
        xirr_cashflows = [
            (date.fromisoformat(opening_cashflow_date), -money["opening_capital"])
        ]
        for flow in capital_flow_trace:
            flow_class = str(flow["class"])
            amount = _manifest_decimal(
                flow["amount"], "capital flow amount", money=True
            )
            sign = -1 if flow_class in {"EXTERNAL_IN", "INTERNAL_IN"} else 1
            xirr_cashflows.append(
                (
                    date.fromisoformat(
                        str(
                            flow["event_date"]
                            if schema_version == 9
                            else flow["date"]
                        )
                    ),
                    sign * amount,
                )
            )
        xirr_cashflows.append((date.fromisoformat(end), money["current_value"]))
        expected_xirr = xirr(xirr_cashflows)
        xirr_raw = row.get("xirr_diagnostic")
        actual_xirr = (
            None
            if xirr_raw is None
            else _manifest_decimal(xirr_raw, f"row[{index}].xirr_diagnostic")
        )
        if actual_xirr != expected_xirr:
            raise ManifestError(f"row {index} XIRR diagnostic is invalid")

    projected_assignments: list[dict[str, Any]] = []
    row_chains: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ManifestError("manifest row must be an object")
        if not isinstance(row.get("continuity_to_successor"), bool):
            raise ManifestError("manifest row continuity flag is invalid")
        projected_assignments.append(
            {
                "account_id": row["account_id"],
                "strategy_id": row["strategy_id"],
                "effective_start": row["effective_start"],
                "effective_end": row["effective_end"],
            }
        )
        row_chains[str(row["account_id"])].append(row)
    projected_sha256, projected_count = _assignment_identity(projected_assignments)
    assignment_identity = source_identities.get("assignment")
    if not isinstance(assignment_identity, Mapping) or (
        assignment_identity.get("sha256") != projected_sha256
        or assignment_identity.get("count") != projected_count
    ):
        raise ManifestError(
            "manifest assignment projection differs from source identity"
        )

    for account_id, chain in row_chains.items():
        chain.sort(key=lambda row: str(row["effective_start"]))
        for chain_index, row in enumerate(chain):
            if chain_index == 0:
                continue
            previous = chain[chain_index - 1]
            previous_end = previous.get("effective_end")
            current_start = str(row["effective_start"])
            if previous_end is None:
                raise ManifestError(
                    f"manifest row chain is open-ended before last: {account_id}"
                )
            expected_start = (
                date.fromisoformat(str(previous_end)) + timedelta(days=1)
            ).isoformat()
            has_non_strategy_gap = current_start != expected_start
            if has_non_strategy_gap:
                if full_chains is None or not _mapped_gap_has_non_strategy_coverage(
                    account_id, previous, row, full_chains
                ):
                    raise ManifestError(
                        f"manifest row chain has a gap or overlap: {account_id}"
                    )
                if previous["continuity_to_successor"]:
                    raise ManifestError(
                        "manifest non-strategy gap cannot claim predecessor continuity"
                    )
                if row.get("opening_basis") == "PREDECESSOR_CLOSING_NAV":
                    raise ManifestError(
                        "manifest non-strategy gap cannot use predecessor closing NAV"
                    )
            previous_continuity = bool(previous["continuity_to_successor"])
            if row.get("status") == "BLOCKED_MISSING_PREDECESSOR_CONTINUITY" and not (
                previous_continuity and previous.get("status") != "OK"
            ):
                raise ManifestError("manifest continuity block is impossible")
            if previous_continuity and previous.get("status") != "OK":
                if row.get("status") != "BLOCKED_MISSING_PREDECESSOR_CONTINUITY":
                    raise ManifestError("manifest continuity block does not propagate")
                continue
            if row.get("status") != "OK":
                continue
            opening_basis = row.get("opening_basis")
            if not previous_continuity:
                if opening_basis == "PREDECESSOR_CLOSING_NAV":
                    raise ManifestError("manifest continuity basis is not authorized")
                continue
            if previous.get("status") != "OK":
                continue
            trace = row.get("observation_trace")
            if not isinstance(trace, Mapping) or not isinstance(
                trace.get("links"), list
            ):
                raise ManifestError("manifest continuity trace is invalid")
            first_link = trace["links"][0]
            if (
                opening_basis != "PREDECESSOR_CLOSING_NAV"
                or not isinstance(first_link, Mapping)
                or first_link.get("start_date") != previous.get("end")
                or first_link.get("base_nav") != previous.get("current_value")
                or row.get("opening_capital") != previous.get("current_value")
            ):
                raise ManifestError("manifest continuity capital handoff is invalid")


@_isolated_decimal_context
def validate_manifest_schema(
    manifest: Mapping[str, Any],
    *,
    full_chains: Mapping[str, list[dict[str, Any]]] | None = None,
    schema_snapshot: InputSnapshot | None = None,
) -> None:
    """Validate formal/internal derivation, not cryptographic source authenticity.

    This proves the self-contained candidate is internally consistent.  Slice B
    must still compare source hashes and the admission artifact to independently
    selected governed inputs before any authority or publication claim.
    """
    try:
        from jsonschema import (  # type: ignore[import-untyped]
            Draft202012Validator,
            FormatChecker,
        )
    except ImportError as exc:  # pragma: no cover - environment boundary
        raise ManifestError("jsonschema is required to validate the manifest") from exc
    selected_schema = schema_snapshot or InputSnapshot.capture(
        _MANIFEST_SCHEMA_PATH, "account strategy manifest schema"
    )
    schema = selected_schema.json_object("account strategy manifest schema")
    serializable = json_ready(manifest)
    if not isinstance(serializable, dict):  # defensive type boundary
        raise ManifestError("manifest must be an object")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            serializable
        ),
        key=str,
    )
    if errors:
        raise ManifestError(
            f"manifest does not satisfy formal schema: {errors[0].message}"
        )
    _validate_manifest_semantics(serializable, full_chains=full_chains)


@_isolated_decimal_context
def format_table(manifest: dict[str, Any]) -> str:
    header = (
        "| Account | Strategy | Started | Ended | Invested | Withdrawn "
        "| Net capital | Current | Gain | RONC% | Ann RONC% | Obs return% | "
        "Obs HWM | Obs MaxDD% |"
    )
    divider = "|" + "---|" * 14
    lines = [header, divider]
    for row in manifest["rows"]:
        if row["status"] != "OK":
            reason = str(row.get("blocking_reason", "NO_COVERING_NAV"))
            lines.append(
                f"| {row['account_id']} | {row['strategy_id']} "
                f"| {row['effective_start']} | {row['effective_end'] or 'open'} "
                f"| {row['status']}: {reason} | — | — | — | — | — | — | — | — | — |"
            )
            continue
        money = " | ".join(f"{_dec(row[f], f):,.2f}" for f in MONEY_FIELDS)
        pcts = " | ".join(
            "—" if row[f] is None else f"{(_dec(row[f], f) * 100):.2f}"
            for f in PCT_FIELDS[:3]
        )
        hwm = f"{_dec(row['observed_hwm_index'], 'observed_hwm_index'):.2f}"
        maxdd = (
            f"{(_dec(row['observed_max_drawdown'], 'observed_max_drawdown') * 100):.2f}"
        )
        lines.append(
            f"| {row['account_id']} | {row['strategy_id']} "
            f"| {row['start']} | {row['end']} | {money} "
            f"| {pcts} | {hwm} | {maxdd} |"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class ManifestCandidateSnapshots:
    """All bytes consumed by the snapshot-native epoch producer."""

    ledger: InputSnapshot
    ledger_identity: InputSnapshot
    baseline: InputSnapshot
    classified_flows: InputSnapshot
    classification_policy: InputSnapshot
    admission_policy: InputSnapshot
    flow_coverage: InputSnapshot
    retrieval_receipts: Mapping[str, InputSnapshot]
    calculation_code: InputSnapshot
    manifest_schema: InputSnapshot
    baseline_generation_id: str
    receipt_authority: InputSnapshot | None = None
    selected_assignment_keys: tuple[tuple[str, str, str, str | None], ...] | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "ledger", "ledger_identity", "baseline", "classified_flows",
            "classification_policy", "admission_policy", "flow_coverage",
            "calculation_code", "manifest_schema",
        ):
            if type(getattr(self, field_name)) is not InputSnapshot:
                raise ManifestError(f"{field_name} must be an InputSnapshot")
        if not isinstance(self.retrieval_receipts, Mapping):
            raise ManifestError("retrieval receipts must be a mapping")
        if any(
            not isinstance(key, str) or not key.strip() or type(value) is not InputSnapshot
            for key, value in self.retrieval_receipts.items()
        ):
            raise ManifestError("retrieval receipts must be InputSnapshots")
        if self.receipt_authority is not None and type(self.receipt_authority) is not InputSnapshot:
            raise ManifestError("receipt_authority must be an InputSnapshot")
        object.__setattr__(
            self, "retrieval_receipts", MappingProxyType(dict(self.retrieval_receipts))
        )
        if self.selected_assignment_keys is not None:
            keys = tuple(self.selected_assignment_keys)
            if any(
                not isinstance(key, tuple) or len(key) != 4
                or any(not isinstance(value, str) for value in key[:3])
                or (key[3] is not None and not isinstance(key[3], str))
                for key in keys
            ):
                raise ManifestError("selected_assignment_keys are invalid")
            if len(keys) != len(set(keys)):
                raise ManifestError("selected_assignment_keys must be unique")
            object.__setattr__(self, "selected_assignment_keys", keys)


def _build_manifest_candidate_from_snapshots_impl(
    inputs: ManifestCandidateSnapshots,
    coverage_loader: Any,
    assembler: Any,
) -> dict[str, Any]:
    """Build a candidate from immutable bytes without reopening a path."""
    ledger_snapshot = inputs.ledger
    ledger_identity_snapshot = inputs.ledger_identity
    baseline_snapshot = inputs.baseline
    classified_snapshot = inputs.classified_flows
    flow_coverage_snapshot = inputs.flow_coverage
    classification_policy_snapshot = inputs.classification_policy
    admission_policy_snapshot = inputs.admission_policy
    calculation_code_snapshot = inputs.calculation_code
    selected_baseline_generation_id = _generation_id(
        inputs.baseline_generation_id, "baseline generation_id"
    )

    ledger_identity = load_ledger_identity(
        ledger_identity_snapshot.json_object("ledger identity"),
        ledger_snapshot,
        ledger_identity_snapshot,
    )
    ledger = load_ledger(ledger_snapshot.json_object("ledger"))
    baseline, selected_baseline_generation_id = load_baseline(
        selected_baseline_generation_id, baseline_snapshot.json_object("baseline")
    )
    baseline_identity = baseline_source_identity(
        selected_baseline_generation_id,
        baseline_snapshot,
        baseline,
    )
    assignments = baseline["assignments"]
    if not all(isinstance(assignment, dict) for assignment in assignments):
        raise ManifestError("baseline assignments must contain only objects")
    accounts_index: dict[str, dict[str, Any]] = {}
    for index, account in enumerate(baseline["accounts"]):
        if not isinstance(account, dict):
            raise ManifestError(f"baseline account {index} must be an object")
        account_id = _require_nonempty_text(
            account.get("account_id"), f"baseline account {index}.account_id"
        )
        if account_id in accounts_index:
            raise ManifestError(f"duplicate baseline account_id '{account_id}'")
        accounts_index[account_id] = account
    typed_assignments = [dict(assignment) for assignment in assignments]
    perimeter = partition_assignments(typed_assignments)
    for account_id in perimeter.full_chains:
        if account_id not in accounts_index:
            raise ManifestError(f"baseline assignment account is unknown: {account_id}")
    mapped_assignments = [
        assignment
        for chain in perimeter.mapped_chains.values()
        for assignment in chain
    ]
    if inputs.selected_assignment_keys is not None:
        full_keys = {_assignment_key(assignment) for assignment in mapped_assignments}
        selected_keys = set(inputs.selected_assignment_keys)
        if not selected_keys <= full_keys:
            raise ManifestError("selected_assignment_keys differ from baseline mapped assignments")
        mapped_assignments = [
            assignment
            for assignment in mapped_assignments
            if _assignment_key(assignment) in selected_keys
        ]
    classification_policy, classification_policy_identity = (
        load_flow_classification_policy(
            classification_policy_snapshot.json_object("flow classification policy"),
            classification_policy_snapshot,
        )
    )
    classified_payload = classified_snapshot.json_object("classified flows")
    flow_schema_version = classified_payload.get("schema_version")
    events, classified_artifact, classification_policy_reference = load_flow_classes(
        classified_payload,
        classified_snapshot,
        ledger_identity,
        classification_policy_identity,
    )
    flow_coverage, flow_coverage_artifact = coverage_loader(
        flow_coverage_snapshot,
        ledger_identity,
        classified_artifact,
        inputs.retrieval_receipts,
        mapped_assignments,
        inputs.receipt_authority,
    )
    if inputs.receipt_authority is not None:
        contracts, _authority_artifact = _load_selected_receipt_authority(
            inputs.receipt_authority, inputs.retrieval_receipts,
            _iso_date(flow_coverage_snapshot.json_object("flow coverage").get("as_of_date"), "flow coverage.as_of_date"),
        )
        for row in flow_coverage_snapshot.json_object("flow coverage").get("rows", []):
            if not isinstance(row, Mapping) or row.get("authority_kind") != "STATIC_SNAPSHOT_COVERAGE_V1":
                continue
            receipt_id = row.get("receipt_artifact_id")
            assignment = row.get("assignment")
            if not isinstance(receipt_id, str) or not isinstance(assignment, Mapping):
                raise ManifestError("static snapshot coverage row is invalid")
            receipt = inputs.retrieval_receipts[receipt_id].json_object("static snapshot receipt")
            _validate_static_snapshot_receipt(
                receipt, receipt_id, _require_nonempty_text(assignment.get("account_id"), "static coverage account_id"),
                _iso_date(row.get("requested_start"), "static coverage requested_start"),
                _iso_date(row.get("requested_end"), "static coverage requested_end"),
                _iso_date(flow_coverage_snapshot.json_object("flow coverage").get("as_of_date"), "flow coverage.as_of_date"),
                contracts[receipt_id], ledger,
            )
    admissions, admission_policy_identity = load_admission_policy(
        admission_policy_snapshot.json_object("admission policy"),
        admission_policy_snapshot,
        mapped_assignments,
        ledger,
    )
    chains = validate_assignment_topology(
        mapped_assignments, admissions, perimeter.full_chains, flow_coverage
    )
    reconciled_events = validate_classified_flow_coverage(
        ledger, events, classification_policy
    )
    capital_anchors = parse_capital_anchors(ledger)
    if flow_schema_version == _CURRENT_FLOW_CLASSES_SCHEMA_VERSION:
        if classified_payload.get("capital_anchors") != ledger.get(
            "capital_anchors", []
        ):
            raise ManifestError(
                "classified capital anchors differ from calculation ledger"
            )
        manifest_schema_version = _CURRENT_MANIFEST_SCHEMA_VERSION
    else:
        if capital_anchors:
            raise ManifestError("legacy classified flows cannot consume capital anchors")
        manifest_schema_version = _MANIFEST_SCHEMA_VERSION
    buckets = flow_buckets_from_classes(reconciled_events)
    series, unavailable_navs = parse_nav_observations(ledger)
    validate_evidence_coverage(
        ledger, reconciled_events, perimeter.full_chains, accounts_index
    )
    flow_coverage_identity: dict[str, object] = {
        "artifact": flow_coverage_artifact,
        "ledger_identity": ledger_identity,
        "classified_flows_artifact": classified_artifact,
    }
    if flow_coverage.receipt_authority is not None:
        flow_coverage_identity["receipt_authority"] = flow_coverage.receipt_authority
    manifest = assembler(
        series,
        accounts_index,
        buckets,
        chains,
        admissions,
        {
            "ledger": ledger_identity,
            "baseline": baseline_identity,
            "classified_flows": {
                "artifact": classified_artifact,
                "schema_version": flow_schema_version,
                "ledger_identity": ledger_identity,
                "classification_policy": classification_policy_reference,
            },
            "flow_classification_policy": classification_policy_identity,
            "flow_coverage": flow_coverage_identity,
            "admission_policy": admission_policy_identity,
            "assignment": admission_policy_identity["assignment"],
            "calculation_code": {
                "artifact_id": "tools/account_strategy_manifest.py",
                "sha256": hashlib.sha256(
                    calculation_code_snapshot.contents
                ).hexdigest(),
                "size_bytes": len(calculation_code_snapshot.contents),
            },
        },
        flow_coverage,
        unavailable_navs,
        manifest_schema_version=manifest_schema_version,
        capital_anchors=capital_anchors,
    )
    if manifest_schema_version == _CURRENT_MANIFEST_SCHEMA_VERSION:
        manifest["calculation_cutoff_date"] = _iso_date(
            ledger.get("calculation_cutoff_date"), "ledger calculation_cutoff_date"
        )
    manifest["accounting_currency"] = ledger["accounting_currency"]
    validate_manifest_schema(
        manifest,
        full_chains=perimeter.full_chains,
        schema_snapshot=inputs.manifest_schema,
    )
    return json_ready(manifest)  # type: ignore[return-value]


def _capture_manifest_candidate_inputs(
    *,
    ledger_path: Path,
    ledger_identity_path: Path,
    baseline_root: Path,
    baseline_generation_id: str,
    classified_flows_path: Path,
    classification_policy_path: Path,
    admission_policy_path: Path,
    flow_coverage_path: Path,
    retrieval_receipt_paths: Mapping[str, Path] | None = None,
    receipt_authority_path: Path | None = None,
) -> dict[str, Any]:
    """Capture every selected path once, then delegate to the byte-only producer."""
    selected_baseline_generation_id = _generation_id(
        baseline_generation_id, "baseline generation_id"
    )
    try:
        resolved_baseline_root = baseline_root.resolve(strict=True)
        baseline_path = (
            resolved_baseline_root
            / "generations"
            / selected_baseline_generation_id
            / "portfolio-baseline.json"
        ).resolve(strict=True)
        baseline_path.relative_to(resolved_baseline_root)
    except (OSError, ValueError) as exc:
        raise ManifestError("baseline artifact escapes baseline root") from exc
    try:
        return build_manifest_candidate_from_snapshots(
            ManifestCandidateSnapshots(
                ledger=InputSnapshot.capture(ledger_path, "ledger"),
                ledger_identity=InputSnapshot.capture(
                    ledger_identity_path, "ledger identity"
                ),
                baseline=InputSnapshot.capture(baseline_path, "baseline"),
                classified_flows=InputSnapshot.capture(
                    classified_flows_path, "classified flows"
                ),
                classification_policy=InputSnapshot.capture(
                    classification_policy_path, "flow classification policy"
                ),
                admission_policy=InputSnapshot.capture(
                    admission_policy_path, "admission policy"
                ),
                flow_coverage=InputSnapshot.capture(
                    flow_coverage_path, "flow coverage"
                ),
                retrieval_receipts={
                    artifact_id: InputSnapshot.capture(path, f"retrieval receipt {artifact_id}")
                    for artifact_id, path in (retrieval_receipt_paths or {}).items()
                },
                receipt_authority=(
                    None
                    if receipt_authority_path is None
                    else InputSnapshot.capture(
                        receipt_authority_path, "receipt authority"
                    )
                ),
                calculation_code=InputSnapshot.capture(
                    Path(__file__), "calculation code"
                ),
                manifest_schema=InputSnapshot.capture(
                    _MANIFEST_SCHEMA_PATH, "account strategy manifest schema"
                ),
                baseline_generation_id=selected_baseline_generation_id,
            ),
        )
    except OSError as exc:
        safe_label = str(exc)
        if not safe_label or "\\" in safe_label or "/" in safe_label:
            safe_label = "selected manifest input"
        raise ManifestError(f"cannot capture {safe_label}") from None
    except ValueError as exc:
        # InputSnapshot.capture labels capture errors; strict parsing must remain
        # distinguishable and must never be relabelled as an I/O failure.
        if not str(exc).startswith("cannot capture "):
            raise
        raise ManifestError(str(exc)) from None


def build_manifest_candidate(
    *,
    ledger_path: Path,
    ledger_identity_path: Path,
    baseline_root: Path,
    baseline_generation_id: str,
    classified_flows_path: Path,
    classification_policy_path: Path,
    admission_policy_path: Path,
    flow_coverage_path: Path,
    retrieval_receipt_paths: Mapping[str, Path] | None = None,
    receipt_authority_path: Path | None = None,
) -> dict[str, Any]:
    """Capture public inputs using only immutable retained receipt authority."""
    return _capture_manifest_candidate_inputs(
        ledger_path=ledger_path,
        ledger_identity_path=ledger_identity_path,
        baseline_root=baseline_root,
        baseline_generation_id=baseline_generation_id,
        classified_flows_path=classified_flows_path,
        classification_policy_path=classification_policy_path,
        admission_policy_path=admission_policy_path,
        flow_coverage_path=flow_coverage_path,
        retrieval_receipt_paths=retrieval_receipt_paths,
        receipt_authority_path=receipt_authority_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-json", type=Path, required=True)
    parser.add_argument(
        "--ledger-identity-json",
        type=Path,
        required=True,
        help="generation and SourceArtifact identity for --ledger-json",
    )
    parser.add_argument(
        "--baseline-root",
        type=Path,
        required=True,
        help="dataPortfolio store root containing content-addressed generations",
    )
    parser.add_argument(
        "--baseline-generation-id",
        required=True,
        help="exact content-addressed baseline generation (never CURRENT indirection)",
    )
    parser.add_argument(
        "--flow-classes-json",
        type=Path,
        required=True,
        help="schema-v3, event-reconciled classified-flows artifact",
    )
    parser.add_argument(
        "--classification-policy-json",
        type=Path,
        required=True,
        help="separately selected flow-classification policy authority",
    )
    parser.add_argument(
        "--admission-policy-json",
        type=Path,
        required=True,
        help="explicit funded-epoch admission policy bound to baseline assignments",
    )
    parser.add_argument(
        "--flow-coverage-json",
        type=Path,
        required=True,
        help="hash-bound account-and-inclusive-interval flow-completeness authority",
    )
    parser.add_argument(
        "--retrieval-receipt-json",
        action="append",
        default=[],
        metavar="ARTIFACT_ID=PATH",
        help="explicit selected v2 retrieval receipt; repeat for each receipt",
    )
    parser.add_argument(
        "--receipt-authority-json",
        type=Path,
        help="canonical selected receipt-authority snapshot for supplied receipts",
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    receipt_paths: dict[str, Path] = {}
    for item in args.retrieval_receipt_json:
        artifact_id, separator, raw_path = item.partition("=")
        if not separator or not artifact_id or not raw_path or artifact_id in receipt_paths:
            raise ManifestError("--retrieval-receipt-json must be unique ARTIFACT_ID=PATH")
        receipt_paths[artifact_id] = Path(raw_path)
    manifest = build_manifest_candidate(
        ledger_path=args.ledger_json,
        ledger_identity_path=args.ledger_identity_json,
        baseline_root=args.baseline_root,
        baseline_generation_id=args.baseline_generation_id,
        classified_flows_path=args.flow_classes_json,
        classification_policy_path=args.classification_policy_json,
        admission_policy_path=args.admission_policy_json,
        flow_coverage_path=args.flow_coverage_json,
        retrieval_receipt_paths=receipt_paths,
        receipt_authority_path=args.receipt_authority_json,
    )
    if args.format == "json":
        print(json.dumps(manifest, indent=2))
    else:
        print(format_table(manifest))
    return 0


def _make_snapshot_builder() -> Any:
    """Close validated coverage loading and numeric assembly over one builder."""
    coverage_loader = load_flow_coverage
    assembler = _assemble_manifest_impl
    raw_builder = _build_manifest_candidate_from_snapshots_impl

    def build_manifest_candidate_from_snapshots(
        inputs: ManifestCandidateSnapshots,
    ) -> dict[str, Any]:
        return raw_builder(inputs, coverage_loader, assembler)

    return build_manifest_candidate_from_snapshots


build_manifest_candidate_from_snapshots = _make_snapshot_builder()
del _make_snapshot_builder
globals().pop("_build_manifest_candidate_from_snapshots_impl")
globals().pop("_assemble_manifest_impl")


if __name__ == "__main__":
    raise SystemExit(main())

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
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, localcontext
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Collection, Mapping, Sequence, cast

from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import (
    ManifestValidationError,
    SourceArtifact,
    canonical_json_bytes,
)
from quant_engine.portfolio_ledger.returns import (
    DATE_ONLY,
    EXACT_TIMESTAMP,
    AVAILABLE as PERFORMANCE_AVAILABLE,
    UNAVAILABLE as PERFORMANCE_UNAVAILABLE,
    FlowLeg,
    OpeningPoint,
    ReturnsError,
    classify_xirr,
    link_epoch,
    mwr_cashflows,
    opening_point,
    xirr,
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
_COUNTERPARTY_RULE = "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
_SOURCE_IDENTITY_COUNTERPARTY_RULE = (
    "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT_WITH_SOURCE_IDENTITY_V1"
)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MANIFEST_SCHEMA_VERSION = 7
_FLOW_CLASSES_SCHEMA_VERSION = 3
_CURRENT_MANIFEST_SCHEMA_VERSION = 10
_PERIOD_RETURNS_MANIFEST_SCHEMA_VERSION = 10
_CURRENT_MANIFEST_FAMILY = frozenset({9, 10})
_CURRENT_FLOW_CLASSES_SCHEMA_VERSION = 4
_FLOW_CLASSIFICATION_POLICY_SCHEMA_VERSION = 1
_FLOW_COVERAGE_SCHEMA_VERSION = 2
_CENT = Decimal("0.01")
_ADMISSION_STATUSES = frozenset({"FUNDED", "BLOCKED"})
_ADMISSION_AUTHORITY_KINDS = frozenset({"LEDGER_AVAILABLE_POSITIVE_NAV_V1", "NONE"})
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


def _utc_fill_date(value: object, field: str) -> str:
    """The UTC calendar date of one ledger ``economic_fill_timestamp``.

    The ledger admits only a timezone-aware UTC instant (ISO-8601, offset zero,
    spelled ``Z`` or ``+00:00``) and takes its date as the fill day
    (``tools/provider_ledger.py``: ``_utc_timestamp`` and ``fill_day``).  This
    restates that rule; it is neither imported from the ledger nor shared with
    the independent verifier.  A naive or non-UTC instant is rejected, never
    converted.
    """

    if type(value) is not str or not value or value != value.strip():
        raise ManifestError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} must be an ISO-8601 timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ManifestError(f"{field} must be timezone-aware UTC")
    return instant.date().isoformat()


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
        raise ManifestError(f"invalid source artifact for {snapshot.label}") from exc


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
    for index, value in enumerate(dispositions["exact"]):
        if not isinstance(value, dict):
            raise ManifestError(
                f"flow classification exact rule {index} must be an object"
            )
        if set(value) not in (
            {"flow_type", "disposition", "amount_sign"},
            {"flow_type", "disposition", "amount_sign", "counterparty_rule"},
            {
                "flow_type",
                "disposition",
                "amount_sign",
                "counterparty_rule",
                "unpaired_disposition",
            },
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
            counterparty_rule = value["counterparty_rule"]
            if (
                disposition != "INTERNAL"
                or counterparty_rule
                not in {_COUNTERPARTY_RULE, _SOURCE_IDENTITY_COUNTERPARTY_RULE}
                or (
                    counterparty_rule == _SOURCE_IDENTITY_COUNTERPARTY_RULE
                    and flow_type != "ACCOUNT_TRANSFER"
                )
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
                or value["counterparty_rule"] != _COUNTERPARTY_RULE
            ):
                raise ManifestError(
                    f"flow classification prefix rule {index}.counterparty_rule is invalid"
                )
            selected_prefix["counterparty_rule"] = _COUNTERPARTY_RULE
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


def _tail_epoch_keys(
    full_chains: Mapping[str, Sequence[Mapping[str, Any]]],
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
) -> set[tuple[str, str, str, str | None]]:
    """Final closed epochs whose admission declares a funded end (#557).

    The epoch ends on its crosswalk date and its return closes at the funded
    end, while the evidence window (receipt, coverage and facts) runs through
    the cutoff so the post-end tail stays inside the same strategy epoch.  Only
    the last row of an account's full chain qualifies: a later row owns the
    facts that follow.
    """

    tails: set[tuple[str, str, str, str | None]] = set()
    for chain in full_chains.values():
        if not chain:
            continue
        # A missing start (an unbounded historical row) is earlier than any date.
        last = max(chain, key=lambda assignment: str(assignment["_key"][2] or ""))
        key = last["_key"]
        admission = admissions.get(key)
        if (
            key[3] is not None
            and admission is not None
            and admission.get("status") == "FUNDED"
            and admission.get("funded_end") is not None
        ):
            tails.add(key)
    return tails


def _final_closed_assignment_keys(
    assignments: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str, str, str | None]]:
    """The closed assignment that ends each account's mapped chain."""

    last: dict[str, tuple[str, str, str, str | None]] = {}
    for assignment in assignments:
        key = _assignment_key(dict(assignment))
        if key[0] not in last or key[2] > last[key[0]][2]:
            last[key[0]] = key
    return {key for key in last.values() if key[3] is not None}


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
        raise ManifestError(
            "flow coverage ledger identity differs from ledger identity"
        )
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
    if not isinstance(assignment_identity, Mapping) or set(assignment_identity) != {
        "sha256",
        "count",
    }:
        raise ManifestError("flow coverage mapped assignment identity is invalid")
    if not isinstance(assignment_identity.get("count"), int) or isinstance(
        assignment_identity.get("count"), bool
    ):
        raise ManifestError("flow coverage mapped assignment count is invalid")
    if (
        not isinstance(assignment_identity.get("sha256"), str)
        or _SHA256.fullmatch(str(assignment_identity.get("sha256"))) is None
    ):
        raise ManifestError("flow coverage mapped assignment hash is invalid")
    expected_sha256, expected_count = _assignment_identity(mapped_assignments)
    if assignment_identity != {"sha256": expected_sha256, "count": expected_count}:
        raise ManifestError("flow coverage mapped assignment identity differs")
    expected_keys = {_assignment_key(assignment) for assignment in mapped_assignments}
    final_closed_keys = _final_closed_assignment_keys(mapped_assignments)
    declared_receipts = payload["receipt_artifacts"]
    if not isinstance(declared_receipts, list):
        raise ManifestError("flow coverage receipt artifacts must be a list")
    declared_ids: set[str] = set()
    for declared in declared_receipts:
        if not isinstance(declared, Mapping) or set(declared) != {
            "artifact_id",
            "sha256",
            "size_bytes",
        }:
            raise ManifestError("flow coverage receipt artifact is invalid")
        artifact_id = _require_nonempty_text(
            declared["artifact_id"], "receipt artifact_id"
        )
        if artifact_id in declared_ids or artifact_id not in receipt_snapshots:
            raise ManifestError("flow coverage receipt artifact set is invalid")
        snapshot = receipt_snapshots[artifact_id]
        if (
            declared.get("sha256") != snapshot.sha256
            or declared.get("size_bytes") != snapshot.size_bytes
        ):
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
        if not isinstance(assignment, Mapping) or set(assignment) != {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
        }:
            raise ManifestError(f"flow coverage row {index}.assignment is invalid")
        account_id = _require_nonempty_text(
            assignment["account_id"], f"flow coverage row {index}.assignment.account_id"
        )
        _require_nonempty_text(
            assignment["strategy_id"],
            f"flow coverage row {index}.assignment.strategy_id",
        )
        effective_start = _iso_date(
            assignment["effective_start"],
            f"flow coverage row {index}.assignment.effective_start",
        )
        effective_end = assignment["effective_end"]
        if effective_end is not None:
            effective_end = _iso_date(
                effective_end, f"flow coverage row {index}.assignment.effective_end"
            )
        assignment_key = (
            account_id,
            str(assignment["strategy_id"]),
            effective_start,
            effective_end,
        )
        if assignment_key in actual_keys:
            raise ManifestError("flow coverage contains duplicate assignment")
        actual_keys.add(assignment_key)
        requested_start = _iso_date(
            value["requested_start"], f"flow coverage row {index}.requested_start"
        )
        requested_end = _iso_date(
            value["requested_end"], f"flow coverage row {index}.requested_end"
        )
        if requested_end < requested_start:
            raise ManifestError(f"flow coverage row {index} interval is reversed")
        if requested_end > coverage_as_of_date:
            raise ManifestError(
                f"flow coverage row {index} interval exceeds coverage as_of_date"
            )
        status = _require_nonempty_text(
            value["status"], f"flow coverage row {index}.status"
        )
        if status not in _FLOW_COVERAGE_STATUSES:
            raise ManifestError(f"flow coverage row {index}.status is invalid")
        if (
            requested_start != effective_start
            or (
                effective_end is not None
                and requested_end != effective_end
                # The final closed epoch of an account chain may carry its
                # evidence through the cutoff (the post-end tail, #557); the
                # assembly proves the admission declares that tail.
                and not (
                    requested_end == coverage_as_of_date
                    and assignment_key in final_closed_keys
                )
            )
            or (
                effective_end is None
                and status == "BLOCKED"
                and requested_end != coverage_as_of_date
            )
        ):
            raise ManifestError(
                f"flow coverage row {index} interval does not bind assignment"
            )
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
            if (
                authority_kind
                not in {
                    "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2",
                    "STATIC_SNAPSHOT_COVERAGE_V1",
                }
                or not isinstance(receipt_id, str)
                or not receipt_id
            ):
                raise ManifestError(
                    f"flow coverage row {index} COMPLETE authority is invalid"
                )
            if blocker is not None or detail is not None:
                raise ManifestError(
                    f"flow coverage row {index} COMPLETE blockers are invalid"
                )
            receipt_snapshot = receipt_snapshots.get(receipt_id)
            if receipt_snapshot is None:
                raise ManifestError(f"flow coverage row {index} receipt is missing")
            receipt = receipt_snapshot.json_object(f"retrieval receipt {receipt_id}")
            if (
                authority_kind == "STATIC_SNAPSHOT_COVERAGE_V1"
                and receipt.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1"
            ) or (
                authority_kind == "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2"
                and receipt.get("schema_version") != 2
            ):
                raise ManifestError(
                    f"flow coverage row {index} receipt authority differs"
                )
            receipt_validator(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
            )
            complete_receipt_ids.add(receipt_id)
        elif (
            authority_kind != "NONE"
            or receipt_id is not None
            or blocker not in _FLOW_COVERAGE_BLOCKERS
            or not isinstance(detail, str)
            or not detail.strip()
        ):
            raise ManifestError(
                f"flow coverage row {index} BLOCKED disposition is invalid"
            )
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
                raise ManifestError(
                    f"flow coverage intervals overlap for '{account_id}'"
                )
    if actual_keys != expected_keys:
        raise ManifestError(
            "flow coverage assignment set differs from mapped assignments"
        )
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
        _rows: tuple[Mapping[str, object], ...]
        _receipt_authority: dict[str, object] | None

        def __init__(
            self,
            rows: list[Mapping[str, object]],
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
                # The receipt can extend past the last observed NAV without
                # extending the calculation beyond its proven interval.
                and effective_start <= effective_end <= str(row["effective_end"])
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
            raise ManifestError(
                "retrieval receipt is not selected by receipt authority"
            )
        if contract.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            _validate_static_snapshot_receipt(
                receipt,
                receipt_id,
                account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
                contract,
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
                [dict(row) for row in rows],
                receipt_authority,
                _capability=capability,
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
        _validate_artifact_shape(value, "receipt authority.raw_evidence_artifact")
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
        "coverage_mode",
        "account_id",
        "strategy_id",
        "requested_start",
        "requested_end",
        "provider_generation",
        "staged_inputs",
    }
    if (
        set(contract) != required
        or contract.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1"
    ):
        raise ManifestError("static receipt authority contract is invalid")
    for field in ("account_id", "strategy_id", "requested_start", "requested_end"):
        _require_nonempty_text(contract.get(field), f"static receipt authority.{field}")
    _iso_date(contract["requested_start"], "static receipt authority.requested_start")
    _iso_date(contract["requested_end"], "static receipt authority.requested_end")
    generation = contract.get("provider_generation")
    staged = contract.get("staged_inputs")
    if not isinstance(generation, Mapping) or set(generation) != {
        "generation_id",
        "catalog_sha256",
        "manifest_sha256",
        "generation_as_of_date",
    }:
        raise ManifestError("static receipt authority provider generation is invalid")
    if not isinstance(staged, Mapping) or set(staged) != {
        "provider_ledger_generation_id",
        "provider_ledger_artifact",
        "baseline_sha256",
        "baseline_size_bytes",
        "crosswalk_sha256",
        "crosswalk_size_bytes",
    }:
        raise ManifestError("static receipt authority staged inputs are invalid")
    for field in ("generation_id", "catalog_sha256", "manifest_sha256"):
        if (
            not isinstance(generation.get(field), str)
            or _SHA256.fullmatch(str(generation.get(field))) is None
        ):
            raise ManifestError(
                "static receipt authority provider generation is invalid"
            )
    _iso_date(
        generation.get("generation_as_of_date"),
        "static receipt authority generation_as_of_date",
    )
    if (
        not isinstance(staged.get("provider_ledger_generation_id"), str)
        or _SHA256.fullmatch(str(staged.get("provider_ledger_generation_id"))) is None
    ):
        raise ManifestError("static receipt authority staged inputs are invalid")
    _validate_artifact_shape(
        staged.get("provider_ledger_artifact"),
        "static receipt authority provider_ledger_artifact",
    )
    for hash_field, size_field in (
        ("baseline_sha256", "baseline_size_bytes"),
        ("crosswalk_sha256", "crosswalk_size_bytes"),
    ):
        size = staged.get(size_field)
        if (
            not isinstance(staged.get(hash_field), str)
            or _SHA256.fullmatch(str(staged.get(hash_field))) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ManifestError("static receipt authority staged inputs are invalid")


def _validate_static_snapshot_receipt(
    receipt: Mapping[str, object],
    receipt_id: str,
    account_id: str,
    requested_start: str,
    requested_end: str,
    coverage_as_of_date: str,
    contract: Mapping[str, object],
    ledger_payload: Mapping[str, Any] | None = None,
) -> None:
    required = {
        "schema_version",
        "artifact_id",
        "coverage_mode",
        "issuer",
        "provider",
        "account_id",
        "strategy_id",
        "requested_start",
        "requested_end",
        "calculation_cutoff_date",
        "date_convention",
        "provider_generation",
        "staged_inputs",
        "observed_facts",
        "missing_intervals",
        "coverage_as_of_date",
        "completeness",
    }
    if (
        set(receipt) != required
        or receipt.get("schema_version") != 3
        or receipt.get("artifact_id") != receipt_id
        or receipt.get("coverage_mode") != "STATIC_SNAPSHOT_COVERAGE_V1"
        or receipt.get("date_convention") != "EOD_PROVIDER_NAV_RECOGNITION_DATE"
        or receipt.get("completeness") != "RETAINED_STATIC_SNAPSHOT_ONLY"
    ):
        raise ManifestError("static snapshot receipt identity is invalid")
    issuer = receipt.get("issuer")
    if (
        not isinstance(issuer, Mapping)
        or set(issuer) != {"adapter_name", "adapter_version", "code_sha256"}
        or issuer.get("adapter_name") != "static-snapshot-coverage"
        or issuer.get("adapter_version") != "1"
        or not isinstance(issuer.get("code_sha256"), str)
        or _SHA256.fullmatch(str(issuer.get("code_sha256"))) is None
    ):
        raise ManifestError("static snapshot receipt issuer is invalid")
    _validate_static_snapshot_contract(contract)
    if (
        receipt.get("account_id") != account_id
        or receipt.get("strategy_id") != contract["strategy_id"]
        or receipt.get("requested_start") != requested_start
        or receipt.get("requested_end") != requested_end
        or receipt.get("provider_generation") != contract["provider_generation"]
        or receipt.get("staged_inputs") != contract["staged_inputs"]
    ):
        raise ManifestError("static snapshot receipt source binding differs")
    cutoff = _iso_date(
        receipt.get("calculation_cutoff_date"),
        "static snapshot receipt calculation_cutoff_date",
    )
    receipt_as_of = _iso_date(
        receipt.get("coverage_as_of_date"),
        "static snapshot receipt coverage_as_of_date",
    )
    if cutoff > coverage_as_of_date or receipt_as_of < cutoff:
        raise ManifestError("static snapshot receipt exceeds coverage as_of_date")
    facts = receipt.get("observed_facts")
    if not isinstance(facts, Mapping) or set(facts) != {
        "nav",
        "flows",
        "capital_anchors",
    }:
        raise ManifestError("static snapshot receipt observed facts are invalid")
    for summary in facts.values():
        row_count = summary.get("row_count") if isinstance(summary, Mapping) else None
        if (
            not isinstance(summary, Mapping)
            or set(summary) != {"row_count", "first_date", "last_date"}
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise ManifestError("static snapshot receipt observed facts are invalid")
        count = row_count
        if count == 0 and (
            summary.get("first_date") is not None
            or summary.get("last_date") is not None
        ):
            raise ManifestError("static snapshot receipt observed facts are invalid")
        if count and (
            _iso_date(summary.get("first_date"), "static snapshot receipt first_date")
            > _iso_date(summary.get("last_date"), "static snapshot receipt last_date")
        ):
            raise ManifestError("static snapshot receipt observed facts are invalid")
    missing = receipt.get("missing_intervals")
    if not isinstance(missing, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"start", "end", "reason"}
        or item.get("reason") != "PROVIDER_DECLARED_NAV_MISSING"
        or _iso_date(item.get("start"), "static snapshot receipt missing start")
        > _iso_date(item.get("end"), "static snapshot receipt missing end")
        for item in missing
    ):
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
    for kind, field in (
        ("nav", "nav_observations"),
        ("flows", "flows"),
        ("capital_anchors", "capital_anchors"),
    ):
        source_rows = ledger_payload.get(field)
        if not isinstance(source_rows, list):
            raise ManifestError("static snapshot receipt calculation ledger is invalid")
        rows = [
            row
            for row in source_rows
            if isinstance(row, Mapping)
            and row.get("account_id") == account_id
            and (
                kind != "capital_anchors"
                or row.get("strategy_id") == receipt.get("strategy_id")
            )
            and requested_start <= fact_date(row, kind) <= requested_end
        ]
        dates = sorted(fact_date(row, kind) for row in rows)
        expected_facts[kind] = {
            "row_count": len(rows),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
        }
    if facts != expected_facts:
        raise ManifestError(
            "static snapshot receipt observed facts differ from calculation ledger"
        )


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
        if (
            _iso_date(receipt.get(field), f"retrieval receipt.{field}")
            > coverage_as_of_date
        ):
            raise ManifestError(
                "retrieval receipt interval exceeds coverage as_of_date"
            )
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
            raise ManifestError(
                "receipt authority receipt set differs from selected receipts"
            )
        if declared_sha256 != snapshot.sha256 or declared_size != snapshot.size_bytes:
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
                receipt,
                receipt_id,
                receipt_account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
                contract,
            )
        else:
            _validate_receipt_against_contract(
                receipt,
                receipt_id,
                receipt_account_id,
                requested_start,
                requested_end,
                coverage_as_of_date,
                contract,
            )
        contracts[receipt_id] = MappingProxyType(dict(contract))
    if authority_ids != sorted(authority_ids, key=str.casefold):
        raise ManifestError("receipt authority receipts are not sorted")
    if set(authority_ids) != set(receipt_snapshots):
        raise ManifestError(
            "receipt authority receipt set differs from selected receipts"
        )
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
        if (
            not isinstance(amount, Mapping)
            or set(amount)
            != {
                "currency",
                "status",
                "value",
            }
            or amount.get("status") != "AVAILABLE"
        ):
            raise ManifestError(f"capital anchor {record_id} amount is unavailable")
        source_currency = _require_nonempty_text(
            amount.get("currency"), f"capital anchor {record_id}.amount.currency"
        )
        source_amount = _money(
            amount.get("value"), f"capital anchor {record_id}.amount.value"
        )
        if source_amount <= 0:
            raise ManifestError(f"capital anchor {record_id} amount is not positive")
        event_date = _iso_date(
            raw.get("event_date"), f"capital anchor {record_id}.event_date"
        )
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            opening_nav = raw.get("opening_nav")
            if (
                not isinstance(opening_nav, Mapping)
                or set(opening_nav)
                != {
                    "currency",
                    "status",
                    "value",
                }
                or opening_nav.get("status") != "AVAILABLE"
            ):
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
            event_fx = raw.get("event_time_fx")
            if source_currency == accounting_currency:
                if "event_time_fx" in raw:
                    raise ManifestError(
                        f"capital anchor {record_id} USD amount carries event FX"
                    )
                basis_amount = source_amount
            else:
                expected_fx_fields = {
                    "base_currency",
                    "currency",
                    "date",
                    "field",
                    "generation_id",
                    "provider",
                    "rate",
                    "source_artifact_id",
                    "timing",
                }
                if (
                    accounting_currency != "USD"
                    or not isinstance(event_fx, Mapping)
                    or set(event_fx) != expected_fx_fields
                    or event_fx.get("base_currency") != accounting_currency
                    or event_fx.get("currency") != source_currency
                    or event_fx.get("date") != event_date
                    or event_fx.get("field") != "Close"
                    or event_fx.get("timing") != "UTC_DAILY_CLOSE"
                ):
                    raise ManifestError(
                        f"capital anchor {record_id} event FX evidence differs"
                    )
                fx_generation_id = _generation_id(
                    event_fx.get("generation_id"),
                    f"capital anchor {record_id}.event_time_fx.generation_id",
                )
                if fx_generation_id != event_fx.get("generation_id"):
                    raise ManifestError(
                        f"capital anchor {record_id} event FX generation id is not canonical"
                    )
                provider = _require_nonempty_text(
                    event_fx.get("provider"),
                    f"capital anchor {record_id}.event_time_fx.provider",
                )
                source_artifact_id = _require_nonempty_text(
                    event_fx.get("source_artifact_id"),
                    f"capital anchor {record_id}.event_time_fx.source_artifact_id",
                )
                selected_fx = ledger.get("portfolio_fx_generation")
                if (
                    not isinstance(selected_fx, Mapping)
                    or set(selected_fx) != {"generation_id", "selection"}
                    or selected_fx.get("selection")
                    != "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT"
                ):
                    raise ManifestError(
                        f"capital anchor {record_id} selected FX generation is invalid"
                    )
                selected_fx_generation_id = _generation_id(
                    selected_fx.get("generation_id"),
                    "ledger portfolio FX generation id",
                )
                if (
                    provider != event_fx.get("provider")
                    or source_artifact_id != event_fx.get("source_artifact_id")
                    or provider != "yahoo"
                    or source_artifact_id != "rates.csv"
                    or fx_generation_id != selected_fx_generation_id
                    or type(event_fx.get("rate")) is not str
                ):
                    raise ManifestError(
                        f"capital anchor {record_id} event FX identity is not canonical"
                    )
                rate = _money(
                    event_fx.get("rate"),
                    f"capital anchor {record_id}.event_time_fx.rate",
                )
                if rate <= 0:
                    raise ManifestError(
                        f"capital anchor {record_id} event FX rate is not positive"
                    )
                basis_amount = source_amount * rate
        else:
            if source_currency != accounting_currency or "event_time_fx" in raw:
                raise ManifestError(
                    f"capital allocation {record_id} currency differs from accounting currency"
                )
            if "opening_nav" in raw:
                raise ManifestError(
                    f"capital allocation {record_id} cannot declare opening_nav"
                )
            basis_amount = source_amount
            opening_nav_amount = None
        # A provider fund allocation is an exact-timestamp fill.  Its money-
        # weighted cash date is the UTC date of the ``economic_fill_timestamp``
        # the ledger admits (the ledger's own fill day); the allocation (event)
        # date is not that date, so without it the MWR fails closed instead of
        # silently dating the fill (R4).  The ledger schema forbids an
        # ``economic_date`` on an anchor, so one is rejected here, never read
        # (N25: reading that field left every Quantbase fill without a date).
        if "economic_date" in raw:
            raise ManifestError(
                f"capital anchor {record_id} carries economic_date; the ledger "
                "field is economic_fill_timestamp"
            )
        economic_date = (
            _utc_fill_date(
                raw["economic_fill_timestamp"],
                f"capital anchor {record_id}.economic_fill_timestamp",
            )
            if "economic_fill_timestamp" in raw
            else None
        )
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
                "event_date": event_date,
                "nav_recognition_date": _iso_date(
                    raw.get("nav_recognition_date"),
                    f"capital anchor {record_id}.nav_recognition_date",
                ),
                "amount": basis_amount,
                "opening_nav": opening_nav_amount,
                "economic_date": (
                    economic_date
                    if capital_type == "STRATEGY_CAPITAL_ALLOCATION"
                    else None
                ),
                "source_record_id": _require_nonempty_text(
                    raw.get("source_record_id"),
                    f"capital anchor {record_id}.source_record_id",
                ),
            }
        )
        if anchors[-1]["nav_recognition_date"] < anchors[-1]["event_date"]:
            raise ManifestError(
                f"capital anchor {record_id} recognition precedes economic event"
            )
        if economic_date is not None and not (
            anchors[-1]["event_date"]
            <= economic_date
            <= anchors[-1]["nav_recognition_date"]
        ):
            raise ManifestError(
                f"capital anchor {record_id} economic fill date {economic_date} "
                "is outside its event and recognition dates"
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
        normalized = {
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
        provider_id = row.get("provider_id")
        if provider_id is not None:
            normalized["provider_id"] = _require_nonempty_text(
                provider_id, f"raw flow {record_id}.provider_id"
            )
        generation_id = row.get("generation_id")
        if generation_id is not None:
            normalized["generation_id"] = _generation_id(
                generation_id, f"raw flow {record_id}.generation_id"
            )
        activity_id = row.get("provider_activity_id_sha256")
        if activity_id is not None:
            if (
                not isinstance(activity_id, str)
                or _SHA256.fullmatch(activity_id) is None
            ):
                raise ManifestError(
                    f"raw flow {record_id}.provider_activity_id_sha256 is invalid"
                )
            if "provider_id" not in normalized or "generation_id" not in normalized:
                raise ManifestError(
                    f"raw flow {record_id} source identity is incomplete"
                )
            normalized["provider_activity_id_sha256"] = activity_id
        raw[record_id] = normalized
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

    def identity_compatible(
        record: Mapping[str, Any], other: Mapping[str, Any], rule: Mapping[str, Any]
    ) -> bool:
        if rule.get("counterparty_rule") != _SOURCE_IDENTITY_COUNTERPARTY_RULE:
            return True
        activity_id = record.get("provider_activity_id_sha256")
        other_activity_id = other.get("provider_activity_id_sha256")
        if activity_id is None and other_activity_id is None:
            return True
        return activity_id is not None and (
            record.get("provider_id"),
            record.get("generation_id"),
            activity_id,
        ) == (
            other.get("provider_id"),
            other.get("generation_id"),
            other_activity_id,
        )

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
                and identity_compatible(record, other, selected_rule)
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
) -> dict[str, dict[str, dict[tuple[str, str, str], Decimal]]]:
    """Build per-record legs only after exact event-level reconciliation.

    Each leg keeps its source record id, so one record is one flow event: two
    same-day records on one recognition date stay two legs (flow counts are
    record counts, not date buckets).
    """
    external_in: dict[str, dict[tuple[str, str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    external_out: dict[str, dict[tuple[str, str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    internal_in: dict[str, dict[tuple[str, str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    internal_out: dict[str, dict[tuple[str, str, str], Decimal]] = defaultdict(
        lambda: defaultdict(Decimal)
    )
    for event in events:
        account = str(event["account_id"])
        event_day = str(event["event_date"])
        recognition_day = str(event.get("nav_recognition_date", event_day))
        leg = (event_day, recognition_day, str(event["source_record_id"]))
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
        effective_start = _iso_date(effective_start_raw, "assignment.effective_start")
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
        and predecessor.get("strategy_mapping_disposition") == "HISTORICAL_POOL_ONLY"
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
            expected_start = (
                date.fromisoformat(previous_end) + timedelta(days=1)
            ).isoformat()
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
        mapped_chains={
            account_id: list(chain) for account_id, chain in mapped_chains.items()
        },
        non_strategy_chains={
            account_id: list(chain) for account_id, chain in non_strategy_chains.items()
        },
        full_chains={
            account_id: list(chain) for account_id, chain in full_chains.items()
        },
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


_CENSUS_MEMBERSHIP_STATUSES = frozenset(
    {
        "ASSIGNMENT_ONLY",
        "DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
        "FUNDED_MEMBERSHIP",
    }
)
CensusIntervalKey = tuple[str, str, str, str | None]


def load_membership_census(
    payload: Mapping[str, Any],
) -> dict[CensusIntervalKey, list[str]]:
    """Index the governed current census by provider, strategy and interval.

    The census names redacted sleeve aliases, not account ids, so an interval
    is identified by ``(provider, strategy, effective_start, effective_end)``
    and carries the multiset of its sleeves' membership statuses.  That is
    enough to bound how many account epochs of one interval can be funded
    (R2) without the private account crosswalk.
    """
    census = payload.get("current_census")
    sleeves = census.get("account_sleeves") if isinstance(census, Mapping) else None
    if not isinstance(sleeves, list):
        raise ManifestError("membership census account sleeves are invalid")
    statuses: dict[CensusIntervalKey, list[str]] = defaultdict(list)
    for index, sleeve in enumerate(sleeves):
        if not isinstance(sleeve, Mapping) or not isinstance(
            sleeve.get("assignments"), list
        ):
            raise ManifestError(f"membership census sleeve {index} is invalid")
        provider = _require_nonempty_text(
            sleeve.get("provider_id"), f"membership census sleeve {index}.provider_id"
        ).upper()
        for assignment in sleeve["assignments"]:
            if not isinstance(assignment, Mapping):
                raise ManifestError(f"membership census sleeve {index} is invalid")
            status = _require_nonempty_text(
                assignment.get("status"), "membership census status"
            )
            if status not in _CENSUS_MEMBERSHIP_STATUSES:
                raise ManifestError("membership census status is invalid")
            end = assignment.get("effective_end")
            statuses[
                (
                    provider,
                    _require_nonempty_text(
                        assignment.get("strategy_id"), "membership census strategy"
                    ),
                    _iso_date(
                        assignment.get("effective_start"), "membership census start"
                    ),
                    None if end is None else _iso_date(end, "membership census end"),
                )
            ].append(status)
    return dict(statuses)


def _validate_admissions_against_census(
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
    census: Mapping[CensusIntervalKey, Sequence[str]],
    providers: Mapping[str, str],
) -> None:
    """R2: only census ``FUNDED_MEMBERSHIP`` intervals may be admitted as funded.

    An ``ASSIGNMENT_ONLY`` interval keeps its epoch identity but is never
    performance (a funded-membership basis).  Each funded account epoch must be
    matched by its own census funded-membership sleeve for the same provider,
    strategy and interval; a policy funding more epochs than the census
    grants, or funding an interval the census does not list, fails closed.
    """
    funded: dict[CensusIntervalKey, int] = defaultdict(int)
    for (account_id, strategy_id, start, end), admission in admissions.items():
        if admission.get("status") != "FUNDED":
            continue
        provider = providers.get(account_id)
        if not provider:
            raise ManifestError(f"admission account provider is unknown: {account_id}")
        funded[(provider.upper(), strategy_id, start, end)] += 1
    for key, count in sorted(funded.items(), key=lambda item: str(item[0])):
        statuses = census.get(key)
        if statuses is None:
            raise ManifestError(
                "admission policy funds an interval absent from the membership census"
            )
        funded_count = sum(status == "FUNDED_MEMBERSHIP" for status in statuses)
        if count > funded_count:
            if funded_count == 0:
                raise ManifestError(
                    "admission policy funds a census ASSIGNMENT_ONLY interval: "
                    f"{key[0]}/{key[1]}/{key[2]}/{key[3]}"
                )
            raise ManifestError(
                "admission policy funds more epochs than census funded memberships "
                f"grant: {key[0]}/{key[1]}/{key[2]}/{key[3]} grants {funded_count}"
            )


def load_admission_policy(
    payload: Mapping[str, Any],
    policy_snapshot: InputSnapshot,
    assignments: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    *,
    membership_census: Mapping[CensusIntervalKey, Sequence[str]],
    providers: Mapping[str, str],
) -> tuple[dict[tuple[str, str, str, str | None], dict[str, Any]], dict[str, object]]:
    """Load an explicit, hash-bound membership/admission policy.

    A policy is not a substitute for financial evidence.  It only identifies
    which exact baseline epochs have an admitted funded membership; when it is
    absent, incomplete, or non-bijective, this producer cannot calculate.  The
    policy must also agree with the governed membership census (R2): a census
    interval without funded membership can never be admitted as funded.
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
            raise ManifestError(
                "admission policy contains a non-strategy or unknown epoch"
            )
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
        if not (
            common | {"authority_kind", "funded_nav_record_ids"}
            <= set(admission)
            <= common | {"authority_kind", "funded_nav_record_ids", "funded_end"}
        ):
            raise ManifestError(f"admission {index} FUNDED keys are invalid")
        # The epoch's last funded day when the account was later liquidated and
        # transferred out: the return closes there and what follows is dated
        # post-liquidation income (#539).  The evidence window is unchanged.
        funded_end = admission.get("funded_end")
        if funded_end is not None:
            funded_end = _iso_date(funded_end, f"admission {index}.funded_end")
            if not _in_window(funded_end, key[2], key[3]):
                raise ManifestError(
                    f"admission {index}.funded_end is outside the assignment"
                )
        authority_kind = _require_nonempty_text(
            admission.get("authority_kind"), f"admission {index}.authority_kind"
        )
        if authority_kind not in _ADMISSION_AUTHORITY_KINDS - {"NONE"}:
            raise ManifestError(f"admission {index}.authority kind is invalid")
        record_ids = admission.get("funded_nav_record_ids")
        if not isinstance(record_ids, list) or not record_ids:
            raise ManifestError(
                f"admission {index}.FUNDED requires NAV record evidence"
            )
        if not all(
            isinstance(record_id, str) and record_id.strip() for record_id in record_ids
        ):
            raise ManifestError(f"admission {index}.funded_nav_record_ids is invalid")
        if len(set(record_ids)) != len(record_ids):
            raise ManifestError(
                f"admission {index}.funded_nav_record_ids contains duplicates"
            )
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
                raise ManifestError(
                    f"funded NAV record '{record_id}' is outside assignment"
                )
            if record["status"] != "AVAILABLE":
                raise ManifestError(f"funded NAV record '{record_id}' is not AVAILABLE")
            if _money(record["value"], f"funded NAV record '{record_id}'.value") <= 0:
                raise ManifestError(
                    f"funded NAV record '{record_id}' must be strictly positive"
                )
            evidence_dates.append(record_date)
        admissions[key] = {
            "status": status,
            "authority_kind": authority_kind,
            "funded_nav_record_ids": list(record_ids),
            "funded_nav_dates": evidence_dates,
            "continuity_to_successor": admission["continuity_to_successor"],
            "funded_end": funded_end,
        }
    if set(admissions) != assignment_keys:
        raise ManifestError(
            "admission policy coverage differs from baseline assignments"
        )
    if providers is None:
        raise ManifestError("membership census check requires account providers")
    _validate_admissions_against_census(admissions, membership_census, providers)
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
                    successor.get("strategy_mapping_disposition") != "STRATEGY_MAPPED"
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
    *,
    supporting_internal_counterparty_source_record_ids: Sequence[str] = (),
    selected_assignment_keys: Sequence[tuple[str, str, str, str | None]] = (),
    tail_keys: Collection[tuple[str, str, str, str | None]] = (),
) -> None:
    """Every source fact must map to exactly one known account epoch.

    No implicit historical exclusion exists in this contract.  Evidence outside
    the explicit baseline intervals is therefore a producer-blocking gap, not
    an opportunity to silently omit a number.
    """
    for account_id in chains:
        if account_id not in accounts_index:
            raise ManifestError(f"baseline assignment account is unknown: {account_id}")

    def _matching_intervals(
        account_id: str, event_date: str, field: str
    ) -> list[dict[str, Any]]:
        if account_id not in accounts_index:
            raise ManifestError(f"{field} account is unknown: {account_id}")
        matches = [
            assignment
            for assignment in chains.get(account_id, [])
            if _in_window(
                event_date,
                assignment["_key"][2],
                None if assignment["_key"] in tail_keys else assignment["_key"][3],
            )
        ]
        if len(matches) == 2:
            predecessor, successor = matches
            if event_date == successor["_key"][
                2
            ] and _is_historical_to_mapped_shared_boundary(predecessor, successor):
                matches = [successor]
        return matches

    def _require_one_interval(
        account_id: str, event_date: str, field: str
    ) -> Mapping[str, Any]:
        matches = _matching_intervals(account_id, event_date, field)
        if len(matches) != 1:
            raise ManifestError(
                f"{field} does not map to exactly one baseline assignment interval: "
                f"{account_id}/{event_date}"
            )
        return matches[0]

    support_ids = tuple(supporting_internal_counterparty_source_record_ids)
    if any(
        type(source_record_id) is not str or not source_record_id
        for source_record_id in support_ids
    ):
        raise ManifestError(
            "supporting internal counterparty source record ids are invalid"
        )
    if len(support_ids) != len(set(support_ids)):
        raise ManifestError(
            "supporting internal counterparty source record ids are duplicated"
        )
    selected_keys = set(selected_assignment_keys)
    if support_ids and not selected_keys:
        raise ManifestError("internal support requires selected assignment keys")
    events_by_source_id = {
        event.get("source_record_id"): event for event in reconciled_events
    }
    if len(events_by_source_id) != len(reconciled_events):
        raise ManifestError("reconciled classified flow source record ids are invalid")
    support_only_ids: set[str] = set()
    for support_id in support_ids:
        support = events_by_source_id.get(support_id)
        if not isinstance(support, Mapping):
            raise ManifestError(
                "declared internal support is absent from classified flows"
            )
        support_account_id = str(support.get("account_id"))
        if support_account_id not in accounts_index:
            raise ManifestError(
                f"classified flow account is unknown: {support_account_id}"
            )
        root_id = support.get("counterparty_source_record_id")
        root = events_by_source_id.get(root_id)
        if (
            support.get("disposition") != "INTERNAL"
            or not isinstance(root_id, str)
            or not isinstance(root, Mapping)
            or root.get("disposition") != "INTERNAL"
            or root.get("counterparty_source_record_id") != support_id
            or support.get("event_date") != root.get("event_date")
            or support.get("nav_recognition_date") != root.get("nav_recognition_date")
            or support.get("amount") != -root["amount"]
        ):
            raise ManifestError("declared internal support is not an exact reciprocal")
        root_assignment = _require_one_interval(
            str(root.get("account_id")), str(root.get("event_date")), "classified flow"
        )
        if root_assignment.get("_key") not in selected_keys:
            raise ManifestError(
                "internal support root does not map to a selected assignment interval"
            )
        support_matches = _matching_intervals(
            support_account_id, str(support.get("event_date")), "classified flow"
        )
        if not (
            len(support_matches) == 1
            and support_matches[0].get("_key") in selected_keys
        ):
            support_only_ids.add(support_id)

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
        if event.get("source_record_id") in support_only_ids:
            continue
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
        return return_on_net_capital * Decimal(365) / Decimal(max(span_days, 1))
    if return_on_net_capital <= Decimal(-1):
        raise ManifestError(
            "annualized RONC requires a strictly positive compounded growth base"
        )
    return (Decimal(1) + return_on_net_capital) ** (
        Decimal(365) / Decimal(span_days)
    ) - Decimal(1)


PERFORMANCE_OPENING_PREDECESSOR = "PREDECESSOR_CLOSING_NAV"
_PERFORMANCE_OPENING_BASES = frozenset(
    {
        "LAST_OBSERVATION_BEFORE_START",
        "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START",
        PERFORMANCE_OPENING_PREDECESSOR,
    }
)
_FLOW_SIGN = {
    "EXTERNAL_IN": 1,
    "EXTERNAL_OUT": -1,
    "INTERNAL_IN": 1,
    "INTERNAL_OUT": -1,
}


@dataclass(frozen=True)
class _EpochLeg:
    """One admitted capital movement of an epoch (one source record)."""

    flow_class: str
    event_date: str
    nav_recognition_date: str
    amount: Decimal
    timing: str
    economic_date: str | None
    source_record_id: str | None

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if _FLOW_SIGN[self.flow_class] > 0 else -self.amount

    def sort_key(self) -> tuple[str, str, str, str, str, Decimal]:
        return (
            self.event_date,
            self.nav_recognition_date,
            self.flow_class,
            self.timing,
            self.economic_date or "",
            self.amount,
        )

    def flow_leg(self) -> FlowLeg:
        try:
            return FlowLeg(
                amount=self.signed_amount,
                flow_class=self.flow_class,
                event_date=date.fromisoformat(self.event_date),
                nav_recognition_date=date.fromisoformat(self.nav_recognition_date),
                timing=self.timing,
                economic_date=(
                    None
                    if self.economic_date is None
                    else date.fromisoformat(self.economic_date)
                ),
                source_record_ids=(
                    () if self.source_record_id is None else (self.source_record_id,)
                ),
            )
        except ReturnsError as exc:
            raise ManifestError(f"capital flow leg is invalid: {exc}") from None


def _epoch_legs(
    source: Mapping[object, Decimal],
    flow_class: str,
    w0: str,
    w1: str | None,
    *,
    allocation: bool = False,
) -> list[_EpochLeg]:
    """Normalize one class of capital flows into per-record legs.

    Keys are an event date, ``(event, recognition)``, or -- as the assembler
    supplies them -- ``(event, recognition, source_record_id)`` for classified
    flows and ``(event, recognition, source_record_id, economic_date)`` for
    provider fund allocations.  Flows outside the assignment window never
    touch the strategy (owner mandate: pre-window cash is prior-epoch
    business).  Classified flows are date-only facts; a provider fund
    allocation is an exact-timestamp fill (PORT-PERF-1 flow timing).
    """

    merged: dict[tuple[str, str, str | None, str | None], Decimal] = {}
    for raw_key, amount in source.items():
        economic_raw: object = None
        record_id: str | None = None
        if isinstance(raw_key, tuple) and len(raw_key) in (
            (2, 4) if allocation else (2, 3)
        ):
            event_raw, recognition_raw = raw_key[0], raw_key[1]
            if len(raw_key) >= 3:
                record_id = _require_nonempty_text(
                    raw_key[2], f"{flow_class} source_record_id"
                )
            if len(raw_key) == 4:
                economic_raw = raw_key[3]
        elif not allocation and not isinstance(raw_key, tuple):
            event_raw = recognition_raw = raw_key
        else:
            raise ManifestError(f"{flow_class} capital flow leg identity is invalid")
        event_day = _iso_date(event_raw, "capital flow event_date")
        recognition_day = _iso_date(
            recognition_raw, "capital flow nav_recognition_date"
        )
        if recognition_day < event_day:
            raise ManifestError(
                f"{flow_class} capital flow recognition precedes economic event"
            )
        economic_day = (
            None
            if economic_raw is None
            else _iso_date(economic_raw, "capital flow economic_date")
        )
        if economic_day is not None and not (
            event_day <= economic_day <= recognition_day
        ):
            raise ManifestError(
                f"{flow_class} economic_date must lie between event_date "
                "and nav_recognition_date"
            )
        if not _in_window(event_day, w0, w1):
            continue
        key = (event_day, recognition_day, record_id, economic_day)
        merged[key] = merged.get(key, Decimal(0)) + amount
    legs: list[_EpochLeg] = []
    for (event_day, recognition_day, record_id, economic_day), amount in merged.items():
        if amount <= 0:
            raise ManifestError(
                f"{flow_class} on {event_day} must be a positive capital flow"
            )
        legs.append(
            _EpochLeg(
                flow_class=flow_class,
                event_date=event_day,
                nav_recognition_date=recognition_day,
                amount=amount,
                timing=EXACT_TIMESTAMP if allocation else DATE_ONLY,
                economic_date=economic_day,
                source_record_id=record_id,
            )
        )
    return legs


def _capital_flow_trace_entry(leg: _EpochLeg) -> dict[str, Any]:
    return {
        "class": leg.flow_class,
        "event_date": leg.event_date,
        "nav_recognition_date": leg.nav_recognition_date,
        "amount": leg.amount,
    }


def _performance_flow_leg_entry(leg: _EpochLeg) -> dict[str, Any]:
    return {
        "class": leg.flow_class,
        "event_date": leg.event_date,
        "nav_recognition_date": leg.nav_recognition_date,
        "economic_date": leg.economic_date,
        "timing": leg.timing,
        "amount": leg.amount,
    }


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
    pre_start_observation: tuple[str, Decimal | None] | None = None,
    funded_end: str | None = None,
) -> dict[str, Any] | None:
    """Account for one funded epoch: capital basis plus R1-R4 performance.

    ``funded_end`` (admission policy) is the epoch's last funded day when the
    account was later liquidated and transferred out: the time-weighted return
    closes there and every later sub-period of the window is a post-liquidation
    credit inside ``gain``.

    Capital (opening basis, invested, withdrawn, gain, RONC and the XIRR
    diagnostic) keeps its owner-approved meaning.  Performance -- the
    observation-linked return, its HWM/drawdown and trace, and the leg list the
    MWR consumes -- is computed only by ``quant_engine.portfolio_ledger.returns``:

    * R1: the performance opening is the last observation before the funded
      start when positive (``pre_start_observation`` supplies it), otherwise the
      first positive observation on or after it; a predecessor's closing NAV
      opens a continuity successor.  An anchor amount is never a return value.
    * Flow timing: exact-timestamp fund allocations enter at the beginning of
      the sub-period ending at their NAV recognition; date-only flows at its end.
    * R3: zero-capital sub-periods are neutral while nothing is earned on them;
      the first money earned on zero capital closes the epoch there, and it and
      every later sub-period are dated, signed post-liquidation credits
      (non-TWR income inside ``gain``).  A contribution after the closing makes
      the epoch's performance unavailable: a re-funding starts a new epoch.
    """

    legs = sorted(
        (
            *_epoch_legs(external_in_by_day, "EXTERNAL_IN", w0, w1),
            *_epoch_legs(external_out_by_day, "EXTERNAL_OUT", w0, w1),
            *_epoch_legs(int_in_by_day, "INTERNAL_IN", w0, w1),
            *_epoch_legs(
                capital_allocation_legs or {},
                "INTERNAL_IN",
                w0,
                w1,
                allocation=True,
            ),
            *_epoch_legs(int_out_by_day, "INTERNAL_OUT", w0, w1),
        ),
        key=_EpochLeg.sort_key,
    )
    # The only permitted NAV trimming is the deterministic leading pre-funding
    # prefix.  Once a strictly-positive AVAILABLE NAV is observed, every later
    # observation is a return base and must therefore itself be positive,
    # except a bounded liquidation run of zeros (R3 then judges its links).
    all_days = sorted(window_navs)
    if any(not _in_window(day, w0, w1) for day in all_days):
        raise ManifestError("epoch NAV window exceeds its assignment interval")
    first_funded = next(
        (day for day in all_days if window_navs[day] > 0),
        None,
    )
    if first_funded is None:
        return None
    net_flow_by_recognition: dict[str, Decimal] = defaultdict(Decimal)
    for leg in legs:
        if prev_closing is not None or leg.nav_recognition_date > first_funded:
            net_flow_by_recognition[leg.nav_recognition_date] += leg.signed_amount

    funded_days = [day for day in all_days if day >= first_funded]
    day_index = 0
    last_positive_day = first_funded
    while day_index < len(funded_days):
        current_day = funded_days[day_index]
        current_nav = window_navs[current_day]
        if current_nav < 0:
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        if current_nav > 0:
            last_positive_day = current_day
            day_index += 1
            continue

        # A zero NAV is only admissible as a bounded liquidation run: its first
        # zero exactly reconciles the preceding positive base, the run itself
        # carries no later flow, and a later positive NAV resumes.  The run is
        # linked, not skipped: its zero-capital sub-periods are R3's to judge.
        first_zero_day = current_day
        zero_end = day_index
        while (
            zero_end + 1 < len(funded_days)
            and window_navs[funded_days[zero_end + 1]] == 0
        ):
            zero_end += 1
        if (
            zero_end + 1 < len(funded_days)
            and window_navs[funded_days[zero_end + 1]] < 0
        ):
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        transition_flow = sum(
            (
                value
                for recognition_day, value in net_flow_by_recognition.items()
                if last_positive_day < recognition_day <= first_zero_day
            ),
            Decimal(0),
        )
        zero_run_end = funded_days[zero_end]
        if window_navs[last_positive_day] + transition_flow != 0 or any(
            first_zero_day < recognition_day <= zero_run_end
            for recognition_day in net_flow_by_recognition
        ):
            return {
                "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
                "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
            }
        # A zero run that ends the window is a liquidation: the account was
        # sold and transferred out.
        day_index = zero_end + 1
    days = funded_days
    if len(days) < 3:
        return None
    closing_nav_date = days[-1]
    if any(leg.event_date > closing_nav_date for leg in legs):
        # Fail-closed (reviewer P1): a flow after the last funded NAV would
        # corrupt the money identity while being absent from Current. The
        # epoch stays unavailable until a covering NAV observation exists.
        return {"unavailable_status": "FLOWS_AFTER_CLOSING_NAV"}

    if prev_closing is not None and opening_anchor is not None:
        raise ManifestError(
            "provider opening anchor conflicts with predecessor continuity"
        )
    if prev_closing is not None and prev_closing_date is None:
        raise ManifestError(
            "predecessor closing date is required for observation trace"
        )
    anchor_recognition_date: str | None = None
    anchor_opening_nav: Decimal | None = None
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
        raw_opening_nav = opening_anchor.get("opening_nav")
        if raw_opening_nav is not None:
            anchor_opening_nav = _money(raw_opening_nav, "opening anchor opening_nav")
        if anchor_recognition_date != first_funded:
            return {
                "unavailable_status": "BLOCKED_RETURN_LINKAGE",
                "blocking_reason": "OPENING_ANCHOR_RECOGNITION_NOT_FIRST_NAV",
            }
        opening_basis = "PROVIDER_CAPITAL_ANCHOR"
        opening_capital_date = anchor_event_date
    elif prev_closing is not None:
        assert prev_closing_date is not None
        opening = prev_closing
        opening_basis = "PREDECESSOR_CLOSING_NAV"
        opening_capital_date = prev_closing_date
    else:
        opening = window_navs[first_funded]
        opening_basis = "FIRST_AVAILABLE_NAV"
        opening_capital_date = first_funded

    # Capital basis.  A first available NAV or a provider capital anchor is a
    # complete opening: cash recognized on or before the first funded day is
    # already inside it.  Only a predecessor's closing NAV leaves the first
    # day's own cash outside the opening.
    capital_legs = [
        leg
        for leg in legs
        if prev_closing is not None or leg.nav_recognition_date > first_funded
    ]
    if any(leg.nav_recognition_date > closing_nav_date for leg in capital_legs):
        return {
            "unavailable_status": "BLOCKED_RETURN_LINKAGE",
            "blocking_reason": "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL",
        }

    def _capital_total(flow_class: str) -> Decimal:
        return sum(
            (leg.amount for leg in capital_legs if leg.flow_class == flow_class),
            Decimal(0),
        )

    dep = _capital_total("EXTERNAL_IN")
    wd = _capital_total("EXTERNAL_OUT")
    int_in = _capital_total("INTERNAL_IN")
    int_out = _capital_total("INTERNAL_OUT")
    invested = opening + dep + int_in
    withdrawn = wd + int_out
    net_capital = invested - withdrawn
    current = window_navs[closing_nav_date]
    gain = current - net_capital
    span_days = (
        date.fromisoformat(closing_nav_date) - date.fromisoformat(opening_capital_date)
    ).days
    # This is not money-weighted return: it ignores flow timing.  Preserve the
    # useful simple ratio under its accurate name; the governed MWR is attached
    # separately from the performance leg list.
    return_on_net_capital = gain / net_capital if net_capital > 0 else None
    if return_on_net_capital is None:
        annualized = None
    else:
        annualized = _annualize_return_on_net_capital(
            return_on_net_capital,
            span_days,
        )
    closing_cashflows: list[tuple[date, Decimal]] = [
        (date.fromisoformat(opening_capital_date), -opening)
    ]
    for leg in capital_legs:
        closing_cashflows.append(
            (date.fromisoformat(leg.event_date), -leg.signed_amount)
        )
    closing_cashflows.append((date.fromisoformat(closing_nav_date), current))
    xirr_rate = xirr(closing_cashflows)

    performance = _epoch_performance(
        window_navs,
        days,
        legs,
        w0=w0,
        first_funded=first_funded,
        prev_closing=prev_closing,
        prev_closing_date=prev_closing_date,
        pre_start_observation=pre_start_observation,
        anchor_recognition_date=anchor_recognition_date,
        anchor_opening_nav=anchor_opening_nav,
        funded_end=funded_end,
    )
    return {
        "start": first_funded,
        "end": closing_nav_date,
        "opening_capital": opening,
        "opening_capital_date": opening_capital_date,
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
        "nav_observations": len(days),
        "opening_basis": opening_basis,
        "capital_flow_trace": [_capital_flow_trace_entry(leg) for leg in capital_legs],
        **performance,
    }


def _post_liquidation_fields(linked_epoch: Any) -> dict[str, Any]:
    """Closing date and dated credits of an epoch closed before its last NAV.

    Present only for a closed epoch, so a manifest without one keeps its bytes.
    The credits are already inside the epoch's dollar ``gain``; they are not
    part of its time-weighted return.
    """

    if linked_epoch.closed_on is None:
        return {}
    credits = linked_epoch.post_liquidation_credits
    return {
        "performance_closed_on": linked_epoch.closed_on.isoformat(),
        "post_liquidation_credits": [
            {
                "start_date": credit.start_date.isoformat(),
                "end_date": credit.end_date.isoformat(),
                "beginning_value": credit.beginning_value,
                "ending_value": credit.ending_value,
                "net_flow": credit.net_flow,
                "amount": credit.amount,
                "inferred_timing": credit.inferred_timing,
            }
            for credit in credits
        ],
        "post_liquidation_income": sum(
            (credit.amount for credit in credits), Decimal(0)
        ),
    }


def _epoch_performance(
    window_navs: Mapping[str, Decimal],
    days: Sequence[str],
    legs: Sequence[_EpochLeg],
    *,
    w0: str,
    first_funded: str,
    prev_closing: Decimal | None,
    prev_closing_date: str | None,
    pre_start_observation: tuple[str, Decimal | None] | None,
    anchor_recognition_date: str | None,
    anchor_opening_nav: Decimal | None,
    funded_end: str | None = None,
) -> dict[str, Any]:
    """R1-R4 epoch performance through the single return engine."""

    def result(
        opening: OpeningPoint | None,
        basis: str | None,
        *,
        reason: str | None = None,
        linked_epoch: Any = None,
    ) -> dict[str, Any]:
        linked = (
            []
            if opening is None
            else [
                leg
                for leg in legs
                if leg.nav_recognition_date > opening.as_of.isoformat()
            ]
        )
        common = {
            "performance_opening_date": (
                None if opening is None else opening.as_of.isoformat()
            ),
            "performance_opening_value": None if opening is None else opening.value,
            "performance_opening_basis": basis,
            "performance_flow_legs": [
                _performance_flow_leg_entry(leg) for leg in linked
            ],
            "flow_count": None if opening is None else len(linked),
        }
        if linked_epoch is None:
            return {
                "observation_linked_return": None,
                "observed_hwm_index": None,
                "observed_max_drawdown": None,
                "observation_trace": {"links": []},
                "performance_status": PERFORMANCE_UNAVAILABLE,
                "performance_reason": reason,
                "performance_flags": [],
                **common,
            }
        hundred = Decimal(100)
        return {
            "observation_linked_return": linked_epoch.twr,
            "observed_hwm_index": linked_epoch.hwm_index * hundred,
            "observed_max_drawdown": linked_epoch.max_drawdown,
            "observation_trace": {
                "links": [
                    {
                        "start_date": link.start_date.isoformat(),
                        "end_date": link.end_date.isoformat(),
                        "base_nav": link.beginning_value,
                        "beginning_flow": link.beginning_flow,
                        "ending_nav": link.ending_value,
                        "ending_flow": link.ending_flow,
                        "net_flow": link.beginning_flow + link.ending_flow,
                        "factor": link.factor,
                        "cumulative_index": link.cumulative_index * hundred,
                        "running_hwm_index": link.running_hwm_index * hundred,
                        "drawdown": link.drawdown,
                    }
                    for link in linked_epoch.links
                ]
            },
            "performance_status": PERFORMANCE_AVAILABLE,
            "performance_reason": None,
            "performance_flags": list(linked_epoch.flags),
            **_post_liquidation_fields(linked_epoch),
            **common,
        }

    observations: dict[str, Decimal]
    if prev_closing is not None:
        assert prev_closing_date is not None
        if prev_closing <= 0:
            raise ManifestError("predecessor closing NAV must be strictly positive")
        if (
            pre_start_observation is not None
            and pre_start_observation[0] > prev_closing_date
        ):
            return result(
                None, None, reason="PREDECESSOR_CLOSING_NAV_IS_NOT_LAST_OBSERVATION"
            )
        opening = OpeningPoint(
            date.fromisoformat(prev_closing_date),
            prev_closing,
            "LAST_OBSERVATION_BEFORE_START",
        )
        basis = PERFORMANCE_OPENING_PREDECESSOR
        observations = {
            prev_closing_date: prev_closing,
            **{day: window_navs[day] for day in days},
        }
    else:
        candidates = {day: value for day, value in window_navs.items() if day >= w0}
        if pre_start_observation is not None:
            pre_day, pre_value = pre_start_observation
            if _iso_date(pre_day, "pre-start observation date") >= w0:
                raise ManifestError("pre-start observation is not before the epoch")
            if pre_value is None:
                return result(None, None, reason="OPENING_OBSERVATION_UNAVAILABLE")
            candidates[pre_day] = pre_value
        try:
            selected = opening_point(
                {date.fromisoformat(day): value for day, value in candidates.items()},
                date.fromisoformat(w0),
            )
        except ReturnsError as exc:
            raise ManifestError(f"performance opening is invalid: {exc}") from None
        if selected is None:
            raise ManifestError("funded epoch has no positive performance opening")
        opening = selected
        basis = opening.basis
        if opening.as_of.isoformat() < w0:
            # Capital entered at the start of day S, so the pre-start valuation
            # opens and day S's own return is linked.  A window observation
            # before the first funded one would contradict that opening.
            if days[0] != min(window_navs):
                return result(
                    opening,
                    basis,
                    reason="NONPOSITIVE_OBSERVATION_AFTER_PERFORMANCE_OPENING",
                )
            observations = {
                opening.as_of.isoformat(): opening.value,
                **{day: window_navs[day] for day in days},
            }
        else:
            if opening.as_of.isoformat() != first_funded:
                raise ManifestError("performance opening differs from first funded NAV")
            observations = {day: window_navs[day] for day in days}
    opening_day = opening.as_of.isoformat()
    if anchor_recognition_date is not None:
        # A provider opening anchor is capital evidence only.  It may confirm
        # the opening valuation, never replace it, and it cannot enter after
        # the performance opening without an admitted flow leg.
        if anchor_recognition_date > opening_day:
            return result(
                opening, basis, reason="OPENING_ANCHOR_AFTER_PERFORMANCE_OPENING"
            )
        if (
            anchor_opening_nav is not None
            and anchor_recognition_date == opening_day
            and anchor_opening_nav != opening.value
        ):
            return result(opening, basis, reason="OPENING_ANCHOR_NAV_DIFFERS")
    try:
        linked_epoch = link_epoch(
            {date.fromisoformat(day): value for day, value in observations.items()},
            [leg.flow_leg() for leg in legs],
            opening,
            None if funded_end is None else date.fromisoformat(funded_end),
        )
    except ReturnsError as exc:
        raise ManifestError(f"epoch performance inputs are invalid: {exc}") from None
    if linked_epoch.status != PERFORMANCE_AVAILABLE:
        return result(opening, basis, reason=linked_epoch.reason)
    return result(opening, basis, linked_epoch=linked_epoch)


def _assemble_manifest_impl(
    series: dict[str, dict[str, Decimal]],
    accounts_index: dict[str, dict[str, Any]],
    buckets: Mapping[str, Mapping[str, Mapping[Any, Decimal]]],
    chains: Mapping[str, list[dict[str, Any]]],
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
    source_identities: Mapping[str, object],
    flow_coverage: Any,
    unavailable_navs: Mapping[str, set[str]] | None = None,
    *,
    manifest_schema_version: int = _MANIFEST_SCHEMA_VERSION,
    capital_anchors: list[dict[str, Any]] | None = None,
    tail_keys: Collection[tuple[str, str, str, str | None]] = (),
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
            # A tail epoch keeps its crosswalk end as identity; its evidence
            # window runs through the cutoff and its return closes at the
            # admission's funded end (#557).
            identity_end = key[3]
            w1 = None if key in tail_keys else key[3]
            admission = admissions[key]
            base: dict[str, Any] = {
                "account_id": aid,
                "provider": provider,
                "strategy_id": strategy_id,
                "effective_start": w0,
                "effective_end": identity_end,
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
            if manifest_schema_version in _CURRENT_MANIFEST_FAMILY:
                if prev_closing is None and len(opening_anchors) > 1:
                    raise ManifestError(
                        "funded current-product epoch permits at most one opening capital anchor"
                    )
                if prev_closing is not None and opening_anchors:
                    raise ManifestError(
                        "successor continuity cannot also consume an opening capital anchor"
                    )
            # Quantbase's dashboard is fund-scoped STRATEGY_NAV.  Linked
            # brokerage deposits/withdrawals share an account identifier but
            # do not cross this valuation perimeter; only reconciled fund
            # allocation anchors enter the strategy epoch.
            strategy_nav_perimeter = provider == "quantbase"
            local_external_in = (
                {} if strategy_nav_perimeter else buckets["external_in"].get(aid, {})
            )
            local_external_out = (
                {} if strategy_nav_perimeter else buckets["external_out"].get(aid, {})
            )
            local_internal_in = (
                {}
                if strategy_nav_perimeter
                else dict(buckets["internal_in"].get(aid, {}))
            )
            local_internal_out = (
                {} if strategy_nav_perimeter else buckets["internal_out"].get(aid, {})
            )
            allocation_legs: dict[tuple[str, str, str, str | None], Decimal] = {}
            for anchor in allocation_anchors:
                leg = (
                    str(anchor["event_date"]),
                    str(anchor["nav_recognition_date"]),
                    str(anchor["source_record_id"]),
                    anchor.get("economic_date"),
                )
                allocation_legs[leg] = (
                    allocation_legs.get(leg, Decimal(0)) + anchor["amount"]
                )
            # R1: the valuation that opens performance is the account's last
            # observation before the funded start, whatever its status; an
            # unavailable one cannot be skipped in favour of an older value.
            pre_start_days = [
                day
                for day in (
                    *series.get(aid, {}),
                    *unavailable_navs.get(aid, set()),
                )
                if day < w0
            ]
            pre_start_day = max(pre_start_days, default=None)
            pre_start_observation = (
                None
                if pre_start_day is None
                else (pre_start_day, series.get(aid, {}).get(pre_start_day))
            )
            stats = analyze_epoch(
                window,
                local_external_in,
                local_external_out,
                local_internal_in,
                local_internal_out,
                prev_closing,
                w0,
                w1,
                prev_closing_date,
                opening_anchor=opening_anchors[0] if opening_anchors else None,
                capital_allocation_legs=allocation_legs,
                pre_start_observation=pre_start_observation,
                funded_end=admission.get("funded_end"),
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


_PERIOD_ANNUALISATION_MIN_DAYS = 365
_PERIOD_RETURN_CONTEXT = Context(prec=34)
PERIOD_RETURN_FIELDS = (
    "return_window_start",
    "return_close_date",
    "window_days",
    "twr_annualised",
    "ytd_twr",
    "ytd_window",
    "calendar_year_twr",
    "calendar_year_windows",
    "periods_per_year",
    "periods_per_year_basis",
    "mwr_window_equivalent",
    "annualisation_basis",
)


def _period_decimal(raw: object) -> Decimal | None:
    return None if raw is None else Decimal(str(raw))


def _period_window(
    first_start: date, last_end: date, year: int | None = None
) -> dict[str, Any]:
    return {
        "start": first_start.isoformat(),
        "end": last_end.isoformat(),
        "days": (last_end - first_start).days,
        "covers_period": (
            None if year is None else first_start <= date(year - 1, 12, 31)
        ),
    }


def _period_periods_per_year(end_dates: Sequence[date]) -> tuple[int | None, str]:
    """Cadence of a return series from its observation dates (never assumed)."""

    if len(end_dates) < 2:
        return None, "UNCLASSIFIED_TOO_FEW_OBSERVATIONS"
    weekend = sum(1 for day in end_dates if day.weekday() >= 5)
    if weekend * 5 >= len(end_dates):
        return 365, "DAILY_CALENDAR"
    gaps = sorted(
        (later - earlier).days for earlier, later in zip(end_dates, end_dates[1:])
    )
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 3:
        return 252, "DAILY_TRADING"
    if 5 <= median_gap <= 9:
        return 52, "WEEKLY"
    return None, "UNCLASSIFIED_CADENCE"


def _period_return_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Per-epoch window, annualisation, YTD and calendar-year returns (schema 10).

    A pure function of one finished OK row: its observation trace (the daily
    linked path), its TWR, its money-weighted rate and its window dates.  The
    TWR is never changed; ``twr_annualised`` exists only from 365 days.
    """

    with localcontext(_PERIOD_RETURN_CONTEXT):
        trace = row.get("observation_trace")
        links = list(trace["links"]) if isinstance(trace, Mapping) else []
        twr = _period_decimal(row.get("observation_linked_return"))
        window_days: int | None = None
        window_start: date | None = None
        close: date | None = None
        twr_annualised: Decimal | None = None
        years: dict[int, list[dict[str, Any]]] = {}
        if links:
            window_start = date.fromisoformat(str(links[0]["start_date"]))
            close = date.fromisoformat(str(links[-1]["end_date"]))
            window_days = (close - window_start).days
            for link in links:
                year = date.fromisoformat(str(link["end_date"])).year
                years.setdefault(year, []).append(link)
            if (
                twr is not None
                and window_days >= _PERIOD_ANNUALISATION_MIN_DAYS
                and twr > Decimal(-1)
            ):
                twr_annualised = (Decimal(1) + twr) ** (
                    Decimal(365) / Decimal(window_days)
                ) - Decimal(1)
        calendar_twr: dict[str, Decimal] = {}
        calendar_windows: dict[str, dict[str, Any]] = {}
        for year, year_links in sorted(years.items()):
            wealth = Decimal(1)
            for link in year_links:
                wealth *= Decimal(str(link["factor"]))
            calendar_twr[str(year)] = wealth - Decimal(1)
            calendar_windows[str(year)] = _period_window(
                date.fromisoformat(str(year_links[0]["start_date"])),
                date.fromisoformat(str(year_links[-1]["end_date"])),
                year,
            )
        ytd_key = None if close is None else str(close.year)
        periods, periods_basis = _period_periods_per_year(
            [date.fromisoformat(str(link["end_date"])) for link in links]
        )
        mwr = _period_decimal(row.get("mwr_xirr"))
        mwr_open = row.get("mwr_opening_date")
        mwr_end = row.get("mwr_terminal_date")
        mwr_days: int | None = None
        mwr_equivalent: Decimal | None = None
        if isinstance(mwr_open, str) and isinstance(mwr_end, str):
            mwr_days = (
                date.fromisoformat(mwr_end) - date.fromisoformat(mwr_open)
            ).days
            if mwr is not None and mwr_days > 0 and mwr > Decimal(-1):
                mwr_equivalent = (Decimal(1) + mwr) ** (
                    Decimal(mwr_days) / Decimal(365)
                ) - Decimal(1)
        if twr_annualised is not None:
            twr_status = "AVAILABLE"
        elif window_days is not None and window_days < _PERIOD_ANNUALISATION_MIN_DAYS:
            twr_status = "SUPPRESSED_UNDER_365D"
        else:
            twr_status = "UNAVAILABLE"
        span_days: int | None = None
        capital_date = row.get("opening_capital_date")
        if isinstance(capital_date, str) and isinstance(row.get("end"), str):
            span_days = (
                date.fromisoformat(str(row["end"])) - date.fromisoformat(capital_date)
            ).days

        def _reported(days: int | None) -> str | None:
            if days is None:
                return None
            return (
                "ANNUALISED"
                if days >= _PERIOD_ANNUALISATION_MIN_DAYS
                else "ANNUALISED_FROM_DAYS"
            )

        basis: dict[str, Any] = {
            "twr": {
                "reported_basis": "CUMULATIVE",
                "window_days": window_days,
                "annualised_field": "twr_annualised",
                "annualised_status": twr_status,
                "annualised_method": (
                    "GEOMETRIC_ACT_365" if twr_annualised is not None else None
                ),
            },
            "mwr_xirr": {
                "reported_basis": _reported(mwr_days) if mwr is not None else None,
                "window_days": mwr_days,
                "method": "XIRR_ACT_365",
                "window_equivalent_field": "mwr_window_equivalent",
            },
            "mwr_window_equivalent": {
                "reported_basis": "CUMULATIVE" if mwr_equivalent is not None else None,
                "window_days": mwr_days,
                "method": "XIRR_TO_WINDOW_ACT_365",
            },
            "return_on_net_capital": {
                "reported_basis": "CUMULATIVE",
                "window_days": span_days,
            },
            "annualized_return_on_net_capital": {
                "reported_basis": (
                    _reported(span_days)
                    if row.get("annualized_return_on_net_capital") is not None
                    else None
                ),
                "window_days": span_days,
                "method": (
                    None
                    if span_days is None
                    else "GEOMETRIC_ACT_365"
                    if span_days >= _PERIOD_ANNUALISATION_MIN_DAYS
                    else "LINEAR_ACT_365"
                ),
            },
            "ytd_twr": {"reported_basis": "CUMULATIVE", "window_days": None},
            "calendar_year_twr": {
                "reported_basis": "CUMULATIVE",
                "year_attribution": "LINK_END_DATE_YEAR",
            },
        }
        if ytd_key is not None:
            basis["ytd_twr"]["window_days"] = calendar_windows[ytd_key]["days"]
        return {
            "return_window_start": (
                None if window_start is None else window_start.isoformat()
            ),
            "return_close_date": None if close is None else close.isoformat(),
            "window_days": window_days,
            "twr_annualised": twr_annualised,
            "ytd_twr": None if ytd_key is None else calendar_twr[ytd_key],
            "ytd_window": None if ytd_key is None else calendar_windows[ytd_key],
            "calendar_year_twr": calendar_twr,
            "calendar_year_windows": calendar_windows,
            "periods_per_year": periods,
            "periods_per_year_basis": periods_basis,
            "mwr_window_equivalent": mwr_equivalent,
            "annualisation_basis": basis,
        }


def _attach_period_returns(manifest: Mapping[str, Any]) -> None:
    """Add the schema-10 period-return fields to every OK row, additively."""

    rows = manifest.get("rows")
    if not isinstance(rows, list):
        raise ManifestError("manifest rows are invalid")
    for row in rows:
        if isinstance(row, dict) and row.get("status") == "OK":
            row.update(_period_return_fields(cast(Mapping[str, Any], json_ready(row))))


MWR_VALUATION_TIMING = "NAV_RECOGNITION_DATE_OR_EXACT_FILL_DATE"


def _blocked_mwr_payload(status: str, reason: str, coverage: str) -> dict[str, Any]:
    return {
        "mwr_status": status,
        "mwr_reason": reason,
        "mwr_xirr": None,
        "mwr_opening_date": None,
        "mwr_opening_value": None,
        "mwr_terminal_date": None,
        "mwr_terminal_value": None,
        "mwr_currency": None,
        "mwr_coverage_status": coverage,
        "mwr_cashflows": [],
        "mwr_label": None,
        "mwr_value_basis": None,
        "mwr_valuation_timing": None,
        "mwr_day_count": "ACTUAL_365",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
    }


def _mwr_flow_leg(raw: object, field: str) -> FlowLeg:
    if not isinstance(raw, Mapping) or set(raw) != {
        "class",
        "event_date",
        "nav_recognition_date",
        "economic_date",
        "timing",
        "amount",
    }:
        raise ManifestError(f"{field} is invalid")
    flow_class = _require_nonempty_text(raw.get("class"), f"{field}.class")
    amount = _money(raw.get("amount"), f"{field}.amount")
    economic = raw.get("economic_date")
    try:
        return FlowLeg(
            amount=amount if flow_class.endswith("_IN") else -amount,
            flow_class=flow_class,
            event_date=date.fromisoformat(
                _iso_date(raw.get("event_date"), f"{field}.event_date")
            ),
            nav_recognition_date=date.fromisoformat(
                _iso_date(
                    raw.get("nav_recognition_date"), f"{field}.nav_recognition_date"
                )
            ),
            timing=_require_nonempty_text(raw.get("timing"), f"{field}.timing"),
            economic_date=(
                None
                if economic is None
                else date.fromisoformat(_iso_date(economic, f"{field}.economic_date"))
            ),
        )
    except ReturnsError as exc:
        raise ManifestError(f"{field} is invalid: {exc}") from None


def _mwr_payloads_from_governed_inputs(
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    capital_anchors: Sequence[Mapping[str, Any]],
) -> None:
    """Attach source-bound annualized MWR to existing manifest rows.

    The MWR consumes the epoch's own performance opening and performance flow
    legs -- the list the TWR linked -- and the same classified-flow and NAV
    records the epoch calculation reconciled.  Cash dating follows R4: a
    date-only leg is investor cash on its NAV recognition date, an
    exact-timestamp fill on its economic fill date (fail closed without one).
    It does not infer a cash flow from a cost-basis movement or reuse RONC as
    an MWR substitute.
    """
    nav_sources: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for nav in ledger.get("nav_observations", []):
        if not isinstance(nav, Mapping):
            raise ManifestError("MWR NAV observation is invalid")
        account_id = _require_nonempty_text(nav.get("account_id"), "MWR NAV account")
        nav_date = _iso_date(nav.get("as_of_date"), "MWR NAV date")
        record_id = _require_nonempty_text(nav.get("record_id"), "MWR NAV record")
        value_basis = _require_nonempty_text(nav.get("value_basis"), "MWR NAV basis")
        nav_value = nav.get("nav")
        if not isinstance(nav_value, Mapping):
            raise ManifestError("MWR NAV value is invalid")
        currency = _require_nonempty_text(nav_value.get("currency"), "MWR NAV currency")
        nav_sources[(account_id, nav_date)].append((record_id, value_basis, currency))

    # A performance leg is one source record; its provenance is every record
    # carrying exactly its account, class, dates and amount (records identical
    # in all of these fall on the same investor cash date).
    flow_sources: dict[tuple[str, str, str, str, Decimal], set[str]] = defaultdict(set)
    for event in events:
        account_id = _require_nonempty_text(event.get("account_id"), "MWR flow account")
        event_date = _iso_date(event.get("event_date"), "MWR flow date")
        recognition_date = _iso_date(
            event.get("nav_recognition_date", event_date), "MWR flow recognition"
        )
        amount = event.get("amount")
        if not isinstance(amount, Decimal) or amount == 0:
            raise ManifestError("MWR flow amount is invalid")
        disposition = _require_nonempty_text(
            event.get("disposition"), "MWR flow disposition"
        )
        if disposition not in {"EXTERNAL", "INTERNAL"}:
            raise ManifestError("MWR flow disposition is invalid")
        direction = "IN" if amount > 0 else "OUT"
        source_id = _require_nonempty_text(
            event.get("source_record_id"), "MWR flow source"
        )
        flow_sources[
            (
                account_id,
                f"{disposition}_{direction}",
                event_date,
                recognition_date,
                abs(amount),
            )
        ].add(source_id)

    allocation_sources: dict[tuple[str, str, str, str, Decimal], set[str]] = (
        defaultdict(set)
    )
    opening_anchor_sources: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(
        list
    )
    for anchor in capital_anchors:
        if not isinstance(anchor, Mapping):
            raise ManifestError("MWR capital anchor is invalid")
        account_id = _require_nonempty_text(
            anchor.get("account_id"), "MWR anchor account"
        )
        strategy_id = _require_nonempty_text(
            anchor.get("strategy_id"), "MWR anchor strategy"
        )
        event_date = _iso_date(anchor.get("event_date"), "MWR anchor date")
        recognition_date = _iso_date(
            anchor.get("nav_recognition_date"), "MWR anchor recognition"
        )
        source_id = _require_nonempty_text(
            anchor.get("source_record_id"), "MWR anchor source"
        )
        if anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION":
            allocation_sources[
                (
                    account_id,
                    strategy_id,
                    event_date,
                    recognition_date,
                    _money(anchor.get("amount"), "MWR anchor amount"),
                )
            ].add(source_id)
        elif anchor.get("capital_type") == "OPENING_CAPITAL_ANCHOR":
            # Opening capital recognized on or before the performance opening
            # is inside that opening valuation; the anchor is its provenance,
            # never a separately dated investor cash flow.
            opening_anchor_sources[(account_id, strategy_id)].append(
                (recognition_date, source_id)
            )

    rows = manifest.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ManifestError("MWR manifest rows are invalid")
    for row in rows:
        status = row.get("status")
        if status != "OK":
            row.update(
                _blocked_mwr_payload(
                    (
                        "INCOMPLETE_FLOW_COVERAGE"
                        if status == "BLOCKED_UNPROVABLE_OPENING_FLOWS"
                        else "BLOCKED_ADMISSION"
                    ),
                    row.get("blocking_reason") or "EPOCH_OUTPUT_NOT_ADMITTED",
                    "BLOCKED",
                )
            )
            continue

        account_id = _require_nonempty_text(row.get("account_id"), "MWR row account")
        strategy_id = _require_nonempty_text(row.get("strategy_id"), "MWR row strategy")
        end = _iso_date(row.get("end"), "MWR row end")
        raw_opening_date = row.get("performance_opening_date")
        if raw_opening_date is None:
            row.update(
                _blocked_mwr_payload(
                    "BLOCKED_ADMISSION", "PERFORMANCE_OPENING_UNAVAILABLE", "BLOCKED"
                )
            )
            continue
        opening_date = _iso_date(raw_opening_date, "MWR opening date")
        # This producer runs before json_ready(), while the epoch assembler
        # still holds governed monetary facts as Decimal values.
        opening_value = _money(row.get("performance_opening_value"), "MWR opening")
        terminal_value = _money(row.get("current_value"), "MWR terminal value")
        if opening_value <= 0:
            raise ManifestError("MWR opening value must be strictly positive")
        opening_nav_sources = nav_sources.get((account_id, opening_date), [])
        terminal_nav_sources = nav_sources.get((account_id, end), [])
        if not opening_nav_sources or not terminal_nav_sources:
            row.update(
                _blocked_mwr_payload(
                    "BLOCKED_ADMISSION", "NAV_SOURCE_IDENTITY_UNAVAILABLE", "COMPLETE"
                )
            )
            continue
        nav_metadata = {*opening_nav_sources, *terminal_nav_sources}
        bases = {item[1] for item in nav_metadata}
        currencies = {item[2] for item in nav_metadata}
        if (
            len(bases) != 1
            or len(currencies) != 1
            or next(iter(bases))
            not in {"ACCOUNT_TOTAL", "ACCOUNT_VALUE", "STRATEGY_NAV"}
        ):
            row.update(
                _blocked_mwr_payload(
                    "BLOCKED_ADMISSION",
                    "NAV_PERIMETER_OR_CURRENCY_MISMATCH",
                    "COMPLETE",
                )
            )
            continue
        basis = next(iter(bases))
        raw_legs = row.get("performance_flow_legs")
        if not isinstance(raw_legs, list):
            raise ManifestError("MWR performance flow legs are invalid")
        legs = [
            _mwr_flow_leg(raw, f"MWR performance flow leg {index}")
            for index, raw in enumerate(raw_legs)
        ]
        leg_sources: list[set[str]] = []
        for leg in legs:
            identity = (
                leg.event_date.isoformat(),
                leg.nav_recognition_date.isoformat(),
                abs(leg.amount),
            )
            leg_sources.append(
                set(allocation_sources.get((account_id, strategy_id, *identity), ()))
                if leg.timing == EXACT_TIMESTAMP and leg.flow_class == "INTERNAL_IN"
                else set(flow_sources.get((account_id, leg.flow_class, *identity), ()))
            )
        if not all(leg_sources):
            row.update(
                _blocked_mwr_payload(
                    "BLOCKED_ADMISSION", "FLOW_SOURCE_IDENTITY_UNAVAILABLE", "COMPLETE"
                )
            )
            continue
        opening = OpeningPoint(
            date.fromisoformat(opening_date),
            opening_value,
            "LAST_OBSERVATION_BEFORE_START",
        )
        try:
            cashflows = list(
                mwr_cashflows(opening, legs, date.fromisoformat(end), terminal_value)
            )
        except ReturnsError as exc:
            if any(
                leg.timing == EXACT_TIMESTAMP and leg.economic_date is None
                for leg in legs
            ):
                row.update(
                    _blocked_mwr_payload(
                        "BLOCKED_ADMISSION",
                        "EXACT_TIMESTAMP_FILL_DATE_UNAVAILABLE",
                        "BLOCKED",
                    )
                )
                continue
            raise ManifestError(f"MWR cash flows are invalid: {exc}") from None
        source_ids_by_date: dict[str, set[str]] = defaultdict(set)
        source_ids_by_date[opening_date].update(item[0] for item in opening_nav_sources)
        source_ids_by_date[opening_date].update(
            source_id
            for recognition_date, source_id in opening_anchor_sources[
                (account_id, strategy_id)
            ]
            if recognition_date == opening_date
        )
        for leg, source_ids in zip(legs, leg_sources):
            cash_date = (
                leg.economic_date
                if leg.timing == EXACT_TIMESTAMP
                else leg.nav_recognition_date
            )
            assert cash_date is not None
            source_ids_by_date[cash_date.isoformat()].update(source_ids)
        source_ids_by_date[end].update(item[0] for item in terminal_nav_sources)
        result = classify_xirr(cashflows)
        row.update(
            {
                "mwr_status": result["status"],
                "mwr_reason": result["reason"],
                "mwr_xirr": result["rate"],
                "mwr_opening_date": opening_date,
                "mwr_opening_value": opening_value,
                "mwr_terminal_date": end,
                "mwr_terminal_value": terminal_value,
                "mwr_currency": next(iter(currencies)),
                "mwr_coverage_status": "COMPLETE",
                "mwr_cashflows": [
                    {
                        "date": day.isoformat(),
                        "amount": amount,
                        "source_record_ids": sorted(
                            source_ids_by_date[day.isoformat()]
                        ),
                    }
                    for day, amount in cashflows
                ],
                "mwr_label": (
                    "STRATEGY_EPOCH_MWR"
                    if basis == "STRATEGY_NAV"
                    else "ACCOUNT_EPOCH_MWR"
                ),
                "mwr_value_basis": basis,
                "mwr_valuation_timing": MWR_VALUATION_TIMING,
                "mwr_day_count": "ACTUAL_365",
                "mwr_unit": "ANNUALIZED_RETURN",
                "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
            }
        )


PERFORMANCE_ROW_FIELDS = (
    "opening_capital_date",
    "performance_status",
    "performance_reason",
    "performance_opening_date",
    "performance_opening_value",
    "performance_opening_basis",
    "performance_flags",
    "performance_flow_legs",
    "flow_count",
)
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


_PERFORMANCE_FLOW_LEG_KEYS = frozenset(
    {
        "class",
        "event_date",
        "nav_recognition_date",
        "economic_date",
        "timing",
        "amount",
    }
)
_TRACE_LINK_KEYS = frozenset(
    {
        "start_date",
        "end_date",
        "base_nav",
        "beginning_flow",
        "ending_nav",
        "ending_flow",
        "net_flow",
        "factor",
        "cumulative_index",
        "running_hwm_index",
        "drawdown",
    }
)


def _validate_row_performance(
    row: Mapping[str, Any],
    index: int,
    *,
    start: str,
    end: str,
    effective_start: str,
    effective_end: str | None,
    opening_basis: str,
    opening_capital: Decimal,
    current_value: Decimal,
    capital_entries: Sequence[tuple[str, str, str, Decimal]],
    dual_date_trace: bool,
) -> None:
    """Re-derive one OK row's R1-R4 performance from its own published facts."""

    status = row.get("performance_status")
    if status not in {PERFORMANCE_AVAILABLE, PERFORMANCE_UNAVAILABLE}:
        raise ManifestError(f"row {index} performance status is invalid")
    closed_raw = row.get("performance_closed_on")
    closed_on: str | None = (
        None
        if closed_raw is None
        else _iso_date(closed_raw, f"row[{index}].performance_closed_on")
    )
    if closed_on is not None and status != PERFORMANCE_AVAILABLE:
        raise ManifestError(f"row {index} closed epoch performance is unavailable")
    if (closed_on is None) != ("post_liquidation_credits" not in row) or (
        closed_on is None
    ) != ("post_liquidation_income" not in row):
        raise ManifestError(f"row {index} post-liquidation evidence is incomplete")
    reason = row.get("performance_reason")
    raw_opening_date = row.get("performance_opening_date")
    raw_opening_value = row.get("performance_opening_value")
    basis = row.get("performance_opening_basis")
    raw_legs = row.get("performance_flow_legs")
    flags = row.get("performance_flags")
    flow_count = row.get("flow_count")
    if not isinstance(raw_legs, list) or not isinstance(flags, list):
        raise ManifestError(f"row {index} performance evidence is invalid")
    if raw_opening_date is None:
        if (
            status != PERFORMANCE_UNAVAILABLE
            or raw_opening_value is not None
            or basis is not None
            or raw_legs
            or flow_count is not None
        ):
            raise ManifestError(f"row {index} performance opening is invalid")
        opening_date: str | None = None
    else:
        opening_date = _iso_date(
            raw_opening_date, f"row[{index}].performance_opening_date"
        )
        opening_value = _manifest_decimal(
            raw_opening_value, f"row[{index}].performance_opening_value", money=True
        )
        if opening_value <= 0 or basis not in _PERFORMANCE_OPENING_BASES:
            raise ManifestError(f"row {index} performance opening is invalid")
        if (
            (basis == PERFORMANCE_OPENING_PREDECESSOR)
            != (opening_basis == "PREDECESSOR_CLOSING_NAV")
            or (
                basis == PERFORMANCE_OPENING_PREDECESSOR
                and (
                    opening_date != row.get("opening_capital_date")
                    or opening_value != opening_capital
                )
            )
            or (
                basis == "LAST_OBSERVATION_BEFORE_START"
                and opening_date >= effective_start
            )
            or (
                basis == "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
                and (
                    opening_date != start
                    or (
                        opening_basis == "FIRST_AVAILABLE_NAV"
                        and opening_value != opening_capital
                    )
                )
            )
        ):
            raise ManifestError(f"row {index} performance opening basis is invalid")
        if type(flow_count) is not int or flow_count != len(raw_legs):
            raise ManifestError(f"row {index} flow count is invalid")

    beginning_by_recognition: dict[str, Decimal] = defaultdict(Decimal)
    ending_by_recognition: dict[str, Decimal] = defaultdict(Decimal)
    performance_entries: list[tuple[str, str, str, Decimal]] = []
    has_date_only = False
    for leg_index, leg in enumerate(raw_legs):
        field = f"row[{index}].performance_flow_legs[{leg_index}]"
        if not isinstance(leg, Mapping) or set(leg) != _PERFORMANCE_FLOW_LEG_KEYS:
            raise ManifestError(f"{field} is invalid")
        flow_class = leg.get("class")
        timing = leg.get("timing")
        if flow_class not in _FLOW_SIGN or timing not in {EXACT_TIMESTAMP, DATE_ONLY}:
            raise ManifestError(f"{field} class or timing is invalid")
        event_date = _iso_date(leg.get("event_date"), f"{field}.event_date")
        recognition_date = _iso_date(
            leg.get("nav_recognition_date"), f"{field}.nav_recognition_date"
        )
        economic_raw = leg.get("economic_date")
        economic_date = (
            None
            if economic_raw is None
            else _iso_date(economic_raw, f"{field}.economic_date")
        )
        amount = _manifest_decimal(leg.get("amount"), f"{field}.amount", money=True)
        if (
            amount <= 0
            or recognition_date < event_date
            or not _in_window(event_date, effective_start, effective_end)
            or opening_date is None
            or recognition_date <= opening_date
            or recognition_date > end
            or (economic_date is not None and timing != EXACT_TIMESTAMP)
            or (
                economic_date is not None
                and not event_date <= economic_date <= recognition_date
            )
            or (timing == EXACT_TIMESTAMP and flow_class != "INTERNAL_IN")
        ):
            raise ManifestError(f"{field} is invalid")
        signed = amount * _FLOW_SIGN[str(flow_class)]
        if timing == EXACT_TIMESTAMP:
            beginning_by_recognition[recognition_date] += signed
        else:
            has_date_only = True
            ending_by_recognition[recognition_date] += signed
        if opening_basis == "PREDECESSOR_CLOSING_NAV" or recognition_date > start:
            performance_entries.append(
                (
                    str(flow_class),
                    event_date,
                    recognition_date if dual_date_trace else "",
                    amount,
                )
            )
    if raw_legs and not all(
        _performance_flow_leg_sort_key(left) <= _performance_flow_leg_sort_key(right)
        for left, right in zip(raw_legs, raw_legs[1:])
    ):
        raise ManifestError(f"row {index} performance flow legs are not ordered")
    # The capital basis embeds cash recognized on or before its opening; every
    # other capital flow must be one performance leg, one to one.
    if opening_date is not None and sorted(performance_entries) != sorted(
        capital_entries
    ):
        raise ManifestError(
            f"row {index} capital flow trace differs from the performance legs"
        )

    linked_return_raw = row.get("observation_linked_return")
    hwm_raw = row.get("observed_hwm_index")
    drawdown_raw = row.get("observed_max_drawdown")
    trace = row.get("observation_trace")
    if not isinstance(trace, Mapping) or set(trace) != {"links"}:
        raise ManifestError(f"row {index} observation trace is invalid")
    links = trace["links"]
    if not isinstance(links, list):
        raise ManifestError(f"row {index} observation trace is invalid")
    if status == PERFORMANCE_UNAVAILABLE:
        if (
            not isinstance(reason, str)
            or not reason
            or links
            or flags
            or linked_return_raw is not None
            or hwm_raw is not None
            or drawdown_raw is not None
        ):
            raise ManifestError(f"row {index} unavailable performance has metrics")
        return
    assert opening_date is not None
    if reason is not None:
        raise ManifestError(f"row {index} available performance has a reason")
    if closed_on is not None:
        has_date_only = any(
            leg.get("timing") == DATE_ONLY
            and str(leg.get("nav_recognition_date")) <= closed_on
            for leg in raw_legs
        )
    if flags != (["INFERRED_TIMING"] if has_date_only else []):
        raise ManifestError(f"row {index} performance flags are invalid")
    linked_return = _manifest_decimal(
        linked_return_raw, f"row[{index}].observation_linked_return"
    )
    observed_hwm = _manifest_decimal(hwm_raw, f"row[{index}].observed_hwm_index")
    observed_drawdown = _manifest_decimal(
        drawdown_raw, f"row[{index}].observed_max_drawdown"
    )
    if observed_drawdown < 0:
        raise ManifestError(f"row {index} observed max drawdown is negative")
    nav_observations = row.get("nav_observations")
    if type(nav_observations) is not int:
        raise ManifestError(f"row {index} NAV observation count is invalid")
    expected_link_count = nav_observations - int(opening_date == start)
    if closed_on is None and len(links) != expected_link_count:
        raise ManifestError(f"row {index} observation trace link count is invalid")
    if closed_on is not None and len(links) > expected_link_count:
        raise ManifestError(f"row {index} observation trace link count is invalid")
    if not links and closed_on != opening_date:
        raise ManifestError(f"row {index} observation trace link count is invalid")
    hundred = Decimal(100)
    cumulative_index = Decimal(1)
    running_hwm = Decimal(1)
    max_drawdown = Decimal(0)
    prior_end = opening_date
    prior_ending_nav = opening_value
    linked_recognitions: set[str] = set()
    for link_index, link in enumerate(links):
        field = f"row[{index}].observation_trace[{link_index}]"
        if not isinstance(link, Mapping) or set(link) != _TRACE_LINK_KEYS:
            raise ManifestError(f"row {index} observation trace link is invalid")
        start_date = _iso_date(link.get("start_date"), f"{field}.start_date")
        end_date = _iso_date(link.get("end_date"), f"{field}.end_date")
        if start_date != prior_end or end_date <= start_date:
            raise ManifestError(f"row {index} observation trace dates are invalid")
        if link_index == 0 and not (
            end_date == start or (opening_date == start and start_date == start)
        ):
            raise ManifestError(f"row {index} observation trace opening is invalid")
        values = {
            name: _manifest_decimal(
                link.get(name),
                f"{field}.{name}",
                money=name
                in {
                    "base_nav",
                    "beginning_flow",
                    "ending_nav",
                    "ending_flow",
                    "net_flow",
                },
            )
            for name in (
                "base_nav",
                "beginning_flow",
                "ending_nav",
                "ending_flow",
                "net_flow",
                "factor",
                "cumulative_index",
                "running_hwm_index",
                "drawdown",
            )
        }
        if values["base_nav"] != prior_ending_nav:
            raise ManifestError(
                f"row {index} observation trace NAV adjacency is invalid"
            )
        interval = {
            day
            for day in (*beginning_by_recognition, *ending_by_recognition)
            if start_date < day <= end_date
        }
        beginning_flow = sum(
            (beginning_by_recognition.get(day, Decimal(0)) for day in sorted(interval)),
            Decimal(0),
        )
        ending_flow = sum(
            (ending_by_recognition.get(day, Decimal(0)) for day in sorted(interval)),
            Decimal(0),
        )
        if (
            values["beginning_flow"] != beginning_flow
            or values["ending_flow"] != ending_flow
            or values["net_flow"] != beginning_flow + ending_flow
        ):
            raise ManifestError(f"row {index} observation trace net flow is invalid")
        linked_recognitions.update(interval)
        base = values["base_nav"] + beginning_flow
        numerator = values["ending_nav"] - ending_flow
        if base < 0:
            raise ManifestError(f"row {index} observation trace base is invalid")
        if base == 0:
            if numerator != 0 or values["factor"] != 1:
                raise ManifestError(f"row {index} observation trace factor is invalid")
        else:
            expected_factor = numerator / base
            if values["factor"] != expected_factor or expected_factor <= 0:
                raise ManifestError(f"row {index} observation trace factor is invalid")
            cumulative_index *= expected_factor
        running_hwm = max(running_hwm, cumulative_index)
        drawdown = Decimal(1) - cumulative_index / running_hwm
        max_drawdown = max(max_drawdown, drawdown)
        if (
            values["cumulative_index"] != cumulative_index * hundred
            or values["running_hwm_index"] != running_hwm * hundred
            or values["drawdown"] != drawdown
        ):
            raise ManifestError(f"row {index} observation trace index is invalid")
        prior_end = end_date
        prior_ending_nav = values["ending_nav"]
    if closed_on is None:
        if prior_end != end or prior_ending_nav != current_value:
            raise ManifestError(
                f"row {index} observation trace current value is invalid"
            )
    elif prior_end != closed_on:
        raise ManifestError(f"row {index} observation trace closing is invalid")
    linkable = {
        day
        for day in (*beginning_by_recognition, *ending_by_recognition)
        if closed_on is None or day <= closed_on
    }
    if linked_recognitions != linkable:
        raise ManifestError(f"row {index} capital flow trace does not map to a link")
    if (
        linked_return != cumulative_index - Decimal(1)
        or observed_hwm != running_hwm * hundred
        or observed_drawdown != max_drawdown
    ):
        raise ManifestError(f"row {index} observation trace summary is invalid")
    if closed_on is not None:
        _validate_post_liquidation_credits(
            row,
            index,
            closed_on=closed_on,
            beginning_by_recognition=beginning_by_recognition,
            ending_by_recognition=ending_by_recognition,
            closing_nav=prior_ending_nav,
        )


_POST_LIQUIDATION_CREDIT_KEYS = frozenset(
    {
        "start_date",
        "end_date",
        "beginning_value",
        "ending_value",
        "net_flow",
        "amount",
        "inferred_timing",
    }
)


def _validate_post_liquidation_credits(
    row: Mapping[str, Any],
    index: int,
    *,
    closed_on: str,
    beginning_by_recognition: Mapping[str, Decimal],
    ending_by_recognition: Mapping[str, Decimal],
    closing_nav: Decimal,
) -> None:
    """Re-derive each post-liquidation credit from the row's own legs."""

    credits = row.get("post_liquidation_credits")
    if not isinstance(credits, list):
        raise ManifestError(f"row {index} post-liquidation credits are invalid")
    prior_end = closed_on
    total = Decimal(0)
    for credit_index, credit in enumerate(credits):
        field = f"row[{index}].post_liquidation_credits[{credit_index}]"
        if not isinstance(credit, Mapping) or set(credit) != (
            _POST_LIQUIDATION_CREDIT_KEYS
        ):
            raise ManifestError(f"{field} is invalid")
        start_date = _iso_date(credit.get("start_date"), f"{field}.start_date")
        end_date = _iso_date(credit.get("end_date"), f"{field}.end_date")
        if start_date < prior_end or end_date <= start_date:
            raise ManifestError(f"{field} dates are invalid")
        values = {
            name: _manifest_decimal(credit.get(name), f"{field}.{name}", money=True)
            for name in ("beginning_value", "ending_value", "net_flow", "amount")
        }
        if credit_index == 0 and start_date == closed_on:
            if values["beginning_value"] != closing_nav:
                raise ManifestError(f"{field} opening value is invalid")
        interval = [
            day
            for day in sorted({*beginning_by_recognition, *ending_by_recognition})
            if start_date < day <= end_date
        ]
        beginning_flow = sum(
            (beginning_by_recognition.get(day, Decimal(0)) for day in interval),
            Decimal(0),
        )
        ending_flow = sum(
            (ending_by_recognition.get(day, Decimal(0)) for day in interval),
            Decimal(0),
        )
        base = values["beginning_value"] + beginning_flow
        expected = values["ending_value"] - ending_flow - base
        if (
            values["net_flow"] != beginning_flow + ending_flow
            or values["amount"] != expected
            or values["amount"] == 0
            or credit.get("inferred_timing")
            is not any(day in ending_by_recognition for day in interval)
        ):
            raise ManifestError(f"{field} arithmetic is invalid")
        total += values["amount"]
        prior_end = end_date
    income = _manifest_decimal(
        row.get("post_liquidation_income"),
        f"row[{index}].post_liquidation_income",
        money=True,
    )
    if income != total:
        raise ManifestError(f"row {index} post-liquidation income is invalid")


def _performance_flow_leg_sort_key(
    leg: object,
) -> tuple[str, str, str, str, str, Decimal]:
    assert isinstance(leg, Mapping)
    return (
        str(leg.get("event_date")),
        str(leg.get("nav_recognition_date")),
        str(leg.get("class")),
        str(leg.get("timing")),
        str(leg.get("economic_date") or ""),
        Decimal(str(leg.get("amount"))),
    )


@_isolated_decimal_context
def _validate_manifest_semantics(
    manifest: Mapping[str, Any],
    *,
    full_chains: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    schema_version = manifest.get("schema_version")
    if schema_version not in {7, 8, 9, 10}:
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
    # Only an account's last epoch can be a tail epoch (#557); an intermediate
    # epoch that liquidates keeps its bounded evidence window.
    final_start_by_account: dict[str, str] = {}
    for candidate in rows:
        if isinstance(candidate, Mapping) and isinstance(
            candidate.get("effective_start"), str
        ):
            account = str(candidate.get("account_id"))
            start_text = str(candidate["effective_start"])
            if start_text > final_start_by_account.get(account, ""):
                final_start_by_account[account] = start_text
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
            raise ManifestError(
                f"row {index} admission provenance reference is invalid"
            )
        if (
            not isinstance(funded_record_ids, list)
            or not all(
                isinstance(record_id, str) and record_id
                for record_id in funded_record_ids
            )
            or len(set(funded_record_ids)) != len(funded_record_ids)
        ):
            raise ManifestError(f"row {index} funded NAV record provenance is invalid")
        if admission_status == "FUNDED":
            if (
                admission_kind != "LEDGER_AVAILABLE_POSITIVE_NAV_V1"
                or not funded_record_ids
            ):
                raise ManifestError(
                    f"row {index} funded admission provenance is invalid"
                )
        elif admission_status == "BLOCKED":
            if admission_kind != "NONE" or funded_record_ids:
                raise ManifestError(
                    f"row {index} blocked admission provenance is invalid"
                )
        else:
            raise ManifestError(f"row {index} admission status is invalid")
        status = row.get("status")
        if status == "FLOWS_AFTER_CLOSING_NAV":
            forbidden_fields = (
                "late_flows",
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
                *PERFORMANCE_ROW_FIELDS,
            )
            if any(field in row for field in forbidden_fields):
                raise ManifestError(f"row {index} blocked late-flow row has evidence")
            continue

        if status != "OK":
            continue

        start = _iso_date(row.get("start"), f"row[{index}].start")
        end = _iso_date(row.get("end"), f"row[{index}].end")
        closed_on = row.get("performance_closed_on")
        if closed_on is not None:
            closed_on = _iso_date(closed_on, f"row[{index}].performance_closed_on")
            if (
                closed_on < start
                or closed_on > end
                or (effective_end is not None and closed_on > effective_end)
            ):
                raise ManifestError(f"row {index} performance closing is invalid")
        # A closed epoch with an explicit funded end keeps its evidence window
        # open through the cutoff: its post-end tail is part of the epoch (#557).
        window_end = (
            None
            if closed_on is not None
            and effective_end is not None
            and effective_start == final_start_by_account.get(epoch_key[0])
            else effective_end
        )
        if (
            start > end
            or start < effective_start
            or (window_end is not None and end > window_end)
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
        # One entry per source record: equal same-day records stay distinct.
        capital_entries: list[tuple[str, str, str, Decimal]] = []
        for flow_index, flow in enumerate(capital_flow_trace):
            if not isinstance(flow, Mapping):
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            flow_class = flow.get("class")
            if flow_class not in gross_flows:
                raise ManifestError(f"row {index} capital flow trace class is invalid")
            if schema_version in _CURRENT_MANIFEST_FAMILY:
                if set(flow) != {
                    "class",
                    "event_date",
                    "nav_recognition_date",
                    "amount",
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
                recognition_date = None
            if not _in_window(event_date, effective_start, window_end):
                raise ManifestError(
                    f"row {index} capital flow trace event date is outside epoch"
                )
            amount = _manifest_decimal(
                flow.get("amount"),
                f"row[{index}].capital_flow_trace[{flow_index}].amount",
                money=True,
            )
            if amount <= 0 or (
                recognition_date is not None and recognition_date < event_date
            ):
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            gross_flows[flow_class] += amount
            capital_entries.append(
                (str(flow_class), event_date, recognition_date or "", amount)
            )

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
        opening_capital_date = _iso_date(
            row.get("opening_capital_date"), f"row[{index}].opening_capital_date"
        )
        if (
            (opening_basis == "FIRST_AVAILABLE_NAV" and opening_capital_date != start)
            or (
                opening_basis == "PREDECESSOR_CLOSING_NAV"
                and opening_capital_date >= effective_start
            )
            or (
                opening_basis == "PROVIDER_CAPITAL_ANCHOR"
                and opening_capital_date > start
            )
            or opening_basis
            not in {
                "FIRST_AVAILABLE_NAV",
                "PREDECESSOR_CLOSING_NAV",
                "PROVIDER_CAPITAL_ANCHOR",
            }
        ):
            raise ManifestError(f"row {index} capital opening date is invalid")
        span_days = (
            date.fromisoformat(end) - date.fromisoformat(opening_capital_date)
        ).days
        expected_annualized = (
            None if ronc is None else _annualize_return_on_net_capital(ronc, span_days)
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
        xirr_cashflows = [
            (date.fromisoformat(opening_capital_date), -money["opening_capital"])
        ]
        for flow_class, event_date, _recognition, amount in capital_entries:
            sign = -1 if flow_class in {"EXTERNAL_IN", "INTERNAL_IN"} else 1
            xirr_cashflows.append((date.fromisoformat(event_date), sign * amount))
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

        _validate_row_performance(
            row,
            index,
            start=start,
            end=end,
            effective_start=effective_start,
            effective_end=window_end,
            opening_basis=str(opening_basis),
            opening_capital=money["opening_capital"],
            current_value=money["current_value"],
            capital_entries=capital_entries,
            dual_date_trace=schema_version in _CURRENT_MANIFEST_FAMILY,
        )
        if schema_version == _PERIOD_RETURNS_MANIFEST_SCHEMA_VERSION:
            expected_period = json_ready(_period_return_fields(row))
            if not isinstance(expected_period, dict) or any(
                field not in row or row[field] != expected_period[field]
                for field in PERIOD_RETURN_FIELDS
            ):
                raise ManifestError(f"row {index} period return fields are invalid")
        elif any(field in row for field in PERIOD_RETURN_FIELDS):
            raise ManifestError(f"row {index} period return fields need schema 10")

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
            if (
                opening_basis != "PREDECESSOR_CLOSING_NAV"
                or row.get("opening_capital_date") != previous.get("end")
                or row.get("performance_opening_date") != previous.get("end")
                or row.get("performance_opening_value") != previous.get("current_value")
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
        # R3: an OK row whose performance is unavailable carries no observed
        # HWM or drawdown; the table renders that state instead of parsing it.
        hwm = (
            "unavailable"
            if row["observed_hwm_index"] is None
            else f"{_dec(row['observed_hwm_index'], 'observed_hwm_index'):.2f}"
        )
        maxdd = (
            "unavailable"
            if row["observed_max_drawdown"] is None
            else f"{(_dec(row['observed_max_drawdown'], 'observed_max_drawdown') * 100):.2f}"
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
    membership_census: InputSnapshot
    receipt_authority: InputSnapshot | None = None
    selected_assignment_keys: tuple[tuple[str, str, str, str | None], ...] | None = None
    supporting_internal_counterparty_source_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "ledger",
            "ledger_identity",
            "baseline",
            "classified_flows",
            "classification_policy",
            "admission_policy",
            "flow_coverage",
            "calculation_code",
            "manifest_schema",
        ):
            if type(getattr(self, field_name)) is not InputSnapshot:
                raise ManifestError(f"{field_name} must be an InputSnapshot")
        if not isinstance(self.retrieval_receipts, Mapping):
            raise ManifestError("retrieval receipts must be a mapping")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or type(value) is not InputSnapshot
            for key, value in self.retrieval_receipts.items()
        ):
            raise ManifestError("retrieval receipts must be InputSnapshots")
        if (
            self.receipt_authority is not None
            and type(self.receipt_authority) is not InputSnapshot
        ):
            raise ManifestError("receipt_authority must be an InputSnapshot")
        if type(self.membership_census) is not InputSnapshot:
            raise ManifestError("membership_census is a required selected input")
        object.__setattr__(
            self, "retrieval_receipts", MappingProxyType(dict(self.retrieval_receipts))
        )
        if self.selected_assignment_keys is not None:
            keys = tuple(self.selected_assignment_keys)
            if any(
                not isinstance(key, tuple)
                or len(key) != 4
                or any(not isinstance(value, str) for value in key[:3])
                or (key[3] is not None and not isinstance(key[3], str))
                for key in keys
            ):
                raise ManifestError("selected_assignment_keys are invalid")
            if len(keys) != len(set(keys)):
                raise ManifestError("selected_assignment_keys must be unique")
            object.__setattr__(self, "selected_assignment_keys", keys)
        support_ids = tuple(self.supporting_internal_counterparty_source_record_ids)
        if any(
            type(source_record_id) is not str or not source_record_id
            for source_record_id in support_ids
        ):
            raise ManifestError(
                "supporting_internal_counterparty_source_record_ids are invalid"
            )
        if len(support_ids) != len(set(support_ids)):
            raise ManifestError(
                "supporting_internal_counterparty_source_record_ids are duplicated"
            )
        object.__setattr__(
            self, "supporting_internal_counterparty_source_record_ids", support_ids
        )


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
        assignment for chain in perimeter.mapped_chains.values() for assignment in chain
    ]
    if inputs.selected_assignment_keys is not None:
        full_keys = {_assignment_key(assignment) for assignment in mapped_assignments}
        selected_keys = set(inputs.selected_assignment_keys)
        if not selected_keys <= full_keys:
            raise ManifestError(
                "selected_assignment_keys differ from baseline mapped assignments"
            )
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
    admissions, admission_policy_identity = load_admission_policy(
        admission_policy_snapshot.json_object("admission policy"),
        admission_policy_snapshot,
        mapped_assignments,
        ledger,
        membership_census=load_membership_census(
            inputs.membership_census.json_object("membership census")
        ),
        providers={
            account_id: str(account.get("provider") or "")
            for account_id, account in accounts_index.items()
        },
    )
    if inputs.receipt_authority is not None:
        contracts, _authority_artifact = _load_selected_receipt_authority(
            inputs.receipt_authority,
            inputs.retrieval_receipts,
            _iso_date(
                flow_coverage_snapshot.json_object("flow coverage").get("as_of_date"),
                "flow coverage.as_of_date",
            ),
        )
        flow_coverage_rows = flow_coverage_snapshot.json_object("flow coverage").get(
            "rows", []
        )
        if not isinstance(flow_coverage_rows, list):
            raise ManifestError("flow coverage rows are invalid")
        for row in flow_coverage_rows:
            if (
                not isinstance(row, Mapping)
                or row.get("authority_kind") != "STATIC_SNAPSHOT_COVERAGE_V1"
            ):
                continue
            receipt_id = row.get("receipt_artifact_id")
            assignment = row.get("assignment")
            if not isinstance(receipt_id, str) or not isinstance(assignment, Mapping):
                raise ManifestError("static snapshot coverage row is invalid")
            receipt = inputs.retrieval_receipts[receipt_id].json_object(
                "static snapshot receipt"
            )
            _validate_static_snapshot_receipt(
                receipt,
                receipt_id,
                _require_nonempty_text(
                    assignment.get("account_id"), "static coverage account_id"
                ),
                _iso_date(
                    row.get("requested_start"), "static coverage requested_start"
                ),
                _iso_date(row.get("requested_end"), "static coverage requested_end"),
                _iso_date(
                    flow_coverage_snapshot.json_object("flow coverage").get(
                        "as_of_date"
                    ),
                    "flow coverage.as_of_date",
                ),
                contracts[receipt_id],
                # The calculation ledger carries only admitted funded epochs'
                # facts.  A non-funded epoch keeps its receipt binding but has
                # no calculated numerics, so its facts are not compared here.
                (
                    ledger
                    if admissions.get(_assignment_key(assignment), {}).get("status")
                    == "FUNDED"
                    else None
                ),
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
            raise ManifestError(
                "legacy classified flows cannot consume capital anchors"
            )
        manifest_schema_version = _MANIFEST_SCHEMA_VERSION
    buckets = flow_buckets_from_classes(reconciled_events)
    series, unavailable_navs = parse_nav_observations(ledger)
    tail_keys = _tail_epoch_keys(perimeter.full_chains, admissions)
    validate_evidence_coverage(
        ledger,
        reconciled_events,
        perimeter.full_chains,
        accounts_index,
        supporting_internal_counterparty_source_record_ids=(
            inputs.supporting_internal_counterparty_source_record_ids
        ),
        selected_assignment_keys=tuple(
            _assignment_key(assignment) for assignment in mapped_assignments
        ),
        tail_keys=tail_keys,
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
        tail_keys=tail_keys,
    )
    if manifest_schema_version in _CURRENT_MANIFEST_FAMILY:
        _mwr_payloads_from_governed_inputs(
            manifest, ledger, reconciled_events, capital_anchors
        )
        manifest["calculation_cutoff_date"] = _iso_date(
            ledger.get("calculation_cutoff_date"), "ledger calculation_cutoff_date"
        )
    if manifest_schema_version == _PERIOD_RETURNS_MANIFEST_SCHEMA_VERSION:
        _attach_period_returns(manifest)
    manifest["accounting_currency"] = ledger["accounting_currency"]
    validate_manifest_schema(
        manifest,
        full_chains=perimeter.full_chains,
        schema_snapshot=inputs.manifest_schema,
    )
    manifest_json = json_ready(manifest)
    if not isinstance(manifest_json, dict):
        raise ManifestError("manifest JSON encoding is invalid")
    return manifest_json


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
    membership_census_path: Path,
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
        return cast(
            dict[str, Any],
            build_manifest_candidate_from_snapshots(
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
                        artifact_id: InputSnapshot.capture(
                            path, f"retrieval receipt {artifact_id}"
                        )
                        for artifact_id, path in (retrieval_receipt_paths or {}).items()
                    },
                    receipt_authority=(
                        None
                        if receipt_authority_path is None
                        else InputSnapshot.capture(
                            receipt_authority_path, "receipt authority"
                        )
                    ),
                    membership_census=InputSnapshot.capture(
                        membership_census_path, "membership census"
                    ),
                    calculation_code=InputSnapshot.capture(
                        Path(__file__), "calculation code"
                    ),
                    manifest_schema=InputSnapshot.capture(
                        _MANIFEST_SCHEMA_PATH, "account strategy manifest schema"
                    ),
                    baseline_generation_id=selected_baseline_generation_id,
                ),
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
    membership_census_path: Path,
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
        membership_census_path=membership_census_path,
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
    parser.add_argument(
        "--membership-census-json",
        type=Path,
        required=True,
        help=(
            "governed financial MVP source contract whose current census must "
            "agree with every funded admission (R2)"
        ),
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    receipt_paths: dict[str, Path] = {}
    for item in args.retrieval_receipt_json:
        artifact_id, separator, raw_path = item.partition("=")
        if (
            not separator
            or not artifact_id
            or not raw_path
            or artifact_id in receipt_paths
        ):
            raise ManifestError(
                "--retrieval-receipt-json must be unique ARTIFACT_ID=PATH"
            )
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
        membership_census_path=args.membership_census_json,
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

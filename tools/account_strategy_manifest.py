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
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Mapping

from quant_engine.core.contracts import (
    ManifestValidationError,
    SourceArtifact,
)

getcontext().prec = 40


class ManifestError(RuntimeError):
    """Raised when inputs violate the manifest contract (fail-closed)."""


class InputSnapshot:
    """One immutable byte read used for both parsing and identity binding."""

    def __init__(self, path: Path, contents: bytes) -> None:
        self.path = path
        self.contents = contents

    @classmethod
    def capture(cls, path: Path, label: str) -> "InputSnapshot":
        try:
            return cls(path, path.read_bytes())
        except OSError as exc:
            raise ManifestError(f"cannot read {label}: {path}") from exc

    def json_object(self, label: str) -> dict[str, Any]:
        try:
            value = json.loads(self.contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot parse {label}: {self.path}") from exc
        if not isinstance(value, dict):
            raise ManifestError(f"{label} must be a JSON object")
        return value


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MANIFEST_SCHEMA_VERSION = 3
_FLOW_CLASSES_SCHEMA_VERSION = 3
_FLOW_CLASSIFICATION_POLICY_SCHEMA_VERSION = 1
_CENT = Decimal("0.01")
_ADMISSION_STATUSES = frozenset({"FUNDED", "BLOCKED"})
_FLOW_DISPOSITIONS = frozenset({"EXTERNAL", "INTERNAL"})


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
            f"invalid source artifact for {snapshot.path.name}"
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
    """Parse a source monetary value that can be represented exactly in cents."""
    parsed = _dec(value, field)
    if parsed.quantize(_CENT) != parsed:
        raise ManifestError(f"{field} must be exactly cent-representable")
    return parsed


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
    required = {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "ledger_identity",
        "classification_policy",
        "events",
    }
    if set(payload) != required:
        raise ManifestError("classified flows keys are invalid")
    if payload["schema_version"] != _FLOW_CLASSES_SCHEMA_VERSION:
        raise ManifestError("classified flows schema_version must be 3")
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
        schema_version=str(_FLOW_CLASSES_SCHEMA_VERSION),
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
            if len(candidates) != 1:
                raise ManifestError(
                    f"raw flow {record_id} counterparty is unpaired or ambiguous"
                )
            dispositions[record_id] = str(disposition)
            counterparties[record_id] = str(candidates[0]["record_id"])
            continue
        dispositions[record_id] = str(disposition)
    return dispositions, counterparties


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
        if set(event) - {"counterparty_source_record_id"} != required:
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
        amount = _money(event.get("amount"), f"classified flow {event_id}.amount")
        disposition = _require_nonempty_text(
            event.get("disposition"), f"classified flow {event_id}.disposition"
        )
        if disposition not in _FLOW_DISPOSITIONS:
            raise ManifestError(f"classified flow {event_id} disposition is invalid")
        if (
            account_id != raw_record["account_id"]
            or event_date != raw_record["event_date"]
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


def flow_buckets_from_classes(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Decimal]]]:
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
        day = str(event["event_date"])
        amount = event["amount"]
        if not isinstance(amount, Decimal):
            raise ManifestError("validated classified flow amount is not Decimal")
        if event["disposition"] == "EXTERNAL":
            if amount >= 0:
                external_in[account][day] += amount
            else:
                external_out[account][day] += -amount
        elif amount >= 0:
            internal_in[account][day] += amount
        else:
            internal_out[account][day] += -amount
    return {
        "external_in": external_in,
        "external_out": external_out,
        "internal_in": internal_in,
        "internal_out": internal_out,
    }


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
    if payload["schema_version"] != 1:
        raise ManifestError("admission policy schema_version must be 1")
    policy_id = _require_nonempty_text(payload["policy_id"], "admission policy id")
    policy_version = _require_nonempty_text(
        payload["policy_version"], "admission policy version"
    )
    assignment_sha256, assignment_count = _assignment_identity(assignments)
    if payload["assignment_sha256"] != assignment_sha256:
        raise ManifestError("admission policy assignment identity differs")
    if not isinstance(payload["admissions"], list):
        raise ManifestError("admission policy admissions must be a list")
    admissions: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for index, admission in enumerate(payload["admissions"]):
        if not isinstance(admission, dict):
            raise ManifestError(f"admission policy admission {index} must be an object")
        expected = {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
            "status",
            "authority",
            "continuity_to_successor",
        }
        if set(admission) != expected:
            raise ManifestError(f"admission policy admission {index} keys are invalid")
        key = _assignment_key(admission)
        if key in admissions:
            raise ManifestError(f"duplicate admission policy epoch {key}")
        status = _require_nonempty_text(
            admission["status"], f"admission {index}.status"
        )
        if status not in _ADMISSION_STATUSES:
            raise ManifestError(f"admission {index}.status is invalid")
        if not isinstance(admission["continuity_to_successor"], bool):
            raise ManifestError(
                f"admission {index}.continuity_to_successor must be boolean"
            )
        admissions[key] = {
            "status": status,
            "authority": _require_nonempty_text(
                admission["authority"], f"admission {index}.authority"
            ),
            "continuity_to_successor": admission["continuity_to_successor"],
        }
    assignment_keys = {_assignment_key(assignment) for assignment in assignments}
    if set(admissions) != assignment_keys:
        raise ManifestError(
            "admission policy coverage differs from baseline assignments"
        )
    artifact = _source_artifact_from_snapshot(
        policy_snapshot,
        artifact_id=payload["artifact_id"],
        as_of_date=payload["as_of_date"],
        schema_name="account-strategy-manifest-admission-policy",
        schema_version="1",
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
        chain.sort(key=lambda assignment: assignment["_key"][2])
        for index, assignment in enumerate(chain):
            _, _, start, end = assignment["_key"]
            admission = admissions[assignment["_key"]]
            if end is None and index != len(chain) - 1:
                raise ManifestError(
                    f"open-ended assignment is not last for account {account_id}"
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
            expected_start = (
                date.fromisoformat(previous_end) + timedelta(days=1)
            ).isoformat()
            if start != expected_start:
                raise ManifestError(f"assignments have a gap for account {account_id}")
            if admission["continuity_to_successor"] and end is None:
                raise ManifestError(
                    f"open-ended assignment cannot claim a successor for account {account_id}"
                )
    return {account_id: list(chain) for account_id, chain in chains.items()}


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


def _in_window(day: str, w0: str, w1: str | None) -> bool:
    return day >= w0 and (w1 is None or day <= w1)


def analyze_epoch(
    window_navs: dict[str, Decimal],
    external_in_by_day: dict[str, Decimal],
    external_out_by_day: dict[str, Decimal],
    int_in_by_day: dict[str, Decimal],
    int_out_by_day: dict[str, Decimal],
    prev_closing: Decimal | None,
    w0: str,
    w1: str | None,
    prev_closing_date: str | None = None,
) -> dict[str, Any] | None:
    # Owner mandate: flows outside the assignment window never touch the
    # strategy (pre-window cash is prior-epoch business).
    external_in_by_day = {
        d: v for d, v in external_in_by_day.items() if _in_window(d, w0, w1)
    }
    external_out_by_day = {
        d: v for d, v in external_out_by_day.items() if _in_window(d, w0, w1)
    }
    int_in_by_day = {d: v for d, v in int_in_by_day.items() if _in_window(d, w0, w1)}
    int_out_by_day = {d: v for d, v in int_out_by_day.items() if _in_window(d, w0, w1)}
    capital_flow_trace: list[dict[str, Any]] = []
    for flow_class, source in (
        ("EXTERNAL_IN", external_in_by_day),
        ("EXTERNAL_OUT", external_out_by_day),
        ("INTERNAL_IN", int_in_by_day),
        ("INTERNAL_OUT", int_out_by_day),
    ):
        for flow_date, amount in source.items():
            if amount <= 0:
                raise ManifestError(
                    f"{flow_class} on {flow_date} must be a positive capital flow"
                )
            capital_flow_trace.append(
                {"class": flow_class, "date": flow_date, "amount": amount}
            )
    capital_flow_trace.sort(key=lambda entry: (str(entry["date"]), str(entry["class"])))
    if len(window_navs) < 3:
        return None
    days = sorted(window_navs)
    first_funded = days[0]
    closing_nav_date = days[-1]
    late_sources = [
        (label, d, sign * v)
        for label, sign, source_days in (
            ("external", 1, external_in_by_day),
            ("external", -1, external_out_by_day),
            ("internal_in", 1, int_in_by_day),
            ("internal_out", -1, int_out_by_day),
        )
        for d, v in source_days.items()
        if d > closing_nav_date
    ]
    if late_sources:
        # Fail-closed (reviewer P1): a flow after the last funded NAV would
        # corrupt the money identity while being absent from Current. The
        # epoch stays unavailable until a covering NAV observation exists.
        return {
            "unavailable_status": "FLOWS_AFTER_CLOSING_NAV",
            "late_flows": [
                {
                    "class": label,
                    "date": day,
                    "value": _fixed_decimal(value),
                }
                for label, day, value in late_sources
            ],
        }

    if prev_closing is None and any(
        day <= first_funded
        for source in (
            external_in_by_day,
            external_out_by_day,
            int_in_by_day,
            int_out_by_day,
        )
        for day in source
    ):
        return {
            "unavailable_status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
            "blocking_reason": (
                "FLOW_ON_OR_BEFORE_FIRST_AVAILABLE_NAV_WITHOUT_PREDECESSOR_CLOSING"
            ),
        }
    if any(value <= 0 for value in window_navs.values()):
        return {
            "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
            "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
        }

    opening = prev_closing if prev_closing is not None else window_navs[first_funded]
    opening_basis = (
        "PREDECESSOR_CLOSING_NAV" if prev_closing is not None else "FIRST_AVAILABLE_NAV"
    )
    if prev_closing is not None and prev_closing_date is None:
        raise ManifestError(
            "predecessor closing date is required for observation trace"
        )
    # An independently inferred opening is the first available NAV, so a flow
    # on that date is unprovable and was blocked above.  A trusted predecessor
    # opening is distinct and must include every successor-epoch flow,
    # including the first observed day.
    include_first_day_flow = prev_closing is not None
    dep = sum(
        (
            v
            for d, v in external_in_by_day.items()
            if include_first_day_flow or d != first_funded
        ),
        Decimal(0),
    )
    wd = sum(
        (
            v
            for d, v in external_out_by_day.items()
            if include_first_day_flow or d != first_funded
        ),
        Decimal(0),
    )
    int_in = sum(
        (
            v
            for d, v in int_in_by_day.items()
            if include_first_day_flow or d != first_funded
        ),
        Decimal(0),
    )
    int_out = sum(
        (
            v
            for d, v in int_out_by_day.items()
            if include_first_day_flow or d != first_funded
        ),
        Decimal(0),
    )

    invested = opening + dep + int_in
    withdrawn = wd + int_out
    net_capital = invested - withdrawn
    current = window_navs[days[-1]]
    gain = current - net_capital

    net_flow_by_day: dict[str, Decimal] = {}
    for source, sign in (
        (external_in_by_day, 1),
        (external_out_by_day, -1),
        (int_in_by_day, 1),
        (int_out_by_day, -1),
    ):
        for d, v in source.items():
            net_flow_by_day[d] = net_flow_by_day.get(d, Decimal(0)) + sign * v

    def _segment_flow(start_exclusive: str, end_inclusive: str) -> Decimal:
        # Every intervening flow date counts, including dates that carry no
        # NAV observation of their own (reviewer P1).
        return sum(
            (
                value
                for day, value in net_flow_by_day.items()
                if start_exclusive < day <= end_inclusive
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
        drawdown = index / hwm - Decimal(1)
        max_dd = min(max_dd, drawdown)
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
    for prev_d, cur_d in zip(days, days[1:]):
        v0, v1 = window_navs[prev_d], window_navs[cur_d]
        flow = _segment_flow(prev_d, cur_d)
        blocked = _apply_observation_link(prev_d, cur_d, v0, v1, flow)
        if blocked is not None:
            return blocked
    observation_linked_return = index / Decimal(100) - Decimal(1)

    opening_cashflow_date = prev_closing_date if prev_closing is not None else days[0]
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
        annualized = return_on_net_capital * Decimal(365) / Decimal(max(span_days, 1))

    closing_cashflows: list[tuple[date, Decimal]] = [
        (date.fromisoformat(opening_cashflow_date), -opening)
    ]
    for source, sign in (
        (external_in_by_day, -1),
        (external_out_by_day, 1),
        (int_in_by_day, -1),
        (int_out_by_day, 1),
    ):
        for d, v in source.items():
            if (include_first_day_flow or first_funded < d) and d <= days[-1]:
                closing_cashflows.append((date.fromisoformat(d), sign * v))
    closing_cashflows.append((date.fromisoformat(days[-1]), current))
    xirr_rate = xirr(closing_cashflows)

    return {
        "start": days[0],
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


def assemble_manifest(
    series: dict[str, dict[str, Decimal]],
    accounts_index: dict[str, dict[str, Any]],
    buckets: dict[str, dict[str, dict[str, Decimal]]],
    chains: Mapping[str, list[dict[str, Any]]],
    admissions: Mapping[tuple[str, str, str, str | None], Mapping[str, Any]],
    source_identities: Mapping[str, object],
    unavailable_navs: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    unavailable_navs = unavailable_navs or {}
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
                "admission_policy_reference": admission["authority"],
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
                        "blocking_reason": "EPOCH_NOT_EXPLICITLY_FUNDED",
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
            window = {
                d: v
                for d, v in series.get(aid, {}).items()
                if d >= w0 and (w1 is None or d <= w1)
            }
            stats = analyze_epoch(
                window,
                buckets["external_in"].get(aid, {}),
                buckets["external_out"].get(aid, {}),
                buckets["internal_in"].get(aid, {}),
                buckets["internal_out"].get(aid, {}),
                prev_closing,
                w0,
                w1,
                prev_closing_date,
            )
            if stats is not None and stats.get("unavailable_status"):
                unavailable_row = {
                    **base,
                    "status": stats["unavailable_status"],
                }
                if "late_flows" in stats:
                    unavailable_row["late_flows"] = stats["late_flows"]
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
        "schema_version": _MANIFEST_SCHEMA_VERSION,
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
    if money and value.quantize(_CENT) != value:
        raise ManifestError(f"{field} must be exactly cent-representable")
    return value


def _validate_manifest_semantics(manifest: Mapping[str, Any]) -> None:
    source_identities = manifest.get("source_identities")
    if not isinstance(source_identities, Mapping):
        raise ManifestError("manifest source identities must be an object")
    classified = source_identities.get("classified_flows")
    classification_policy = source_identities.get("flow_classification_policy")
    admission_policy = source_identities.get("admission_policy")
    if (
        not isinstance(classified, Mapping)
        or not isinstance(classification_policy, Mapping)
        or not isinstance(admission_policy, Mapping)
    ):
        raise ManifestError("manifest source identities are incomplete")
    if classified.get("ledger_identity") != source_identities.get("ledger"):
        raise ManifestError("classified ledger identity differs from ledger identity")
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
        late_flows = row.get("late_flows")
        if isinstance(late_flows, list):
            for late_index, late in enumerate(late_flows):
                if isinstance(late, Mapping):
                    _manifest_decimal(
                        late.get("value"),
                        f"row[{index}].late_flows[{late_index}].value",
                        money=True,
                    )
        if row.get("status") != "OK":
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
        flow_by_day: dict[str, Decimal] = {}
        seen_capital_flows: set[tuple[str, str]] = set()
        for flow_index, flow in enumerate(capital_flow_trace):
            if not isinstance(flow, Mapping):
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            flow_class = flow.get("class")
            if flow_class not in gross_flows:
                raise ManifestError(f"row {index} capital flow trace class is invalid")
            flow_date = _iso_date(
                flow.get("date"), f"row[{index}].capital_flow_trace[{flow_index}].date"
            )
            if not _in_window(flow_date, effective_start, effective_end):
                raise ManifestError(
                    f"row {index} capital flow trace date is outside epoch"
                )
            amount = _manifest_decimal(
                flow.get("amount"),
                f"row[{index}].capital_flow_trace[{flow_index}].amount",
                money=True,
            )
            if amount <= 0 or (flow_class, flow_date) in seen_capital_flows:
                raise ManifestError(f"row {index} capital flow trace entry is invalid")
            seen_capital_flows.add((flow_class, flow_date))
            gross_flows[flow_class] += amount
            sign = 1 if flow_class in {"EXTERNAL_IN", "INTERNAL_IN"} else -1
            flow_by_day[flow_date] = (
                flow_by_day.get(flow_date, Decimal(0)) + sign * amount
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
        annualization_start = start
        if opening_basis == "PREDECESSOR_CLOSING_NAV":
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
            None if ronc is None else ronc * Decimal(365) / Decimal(max(span_days, 1))
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
        linked_flow_dates: set[str] = set()
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
                if opening_basis == "PREDECESSOR_CLOSING_NAV" and (
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
            interval_flow_dates = {
                flow_date
                for flow_date in flow_by_day
                if start_date < flow_date <= end_date
            }
            expected_net_flow = sum(
                (flow_by_day[flow_date] for flow_date in interval_flow_dates),
                Decimal(0),
            )
            if net_flow != expected_net_flow or linked_flow_dates & interval_flow_dates:
                raise ManifestError(
                    f"row {index} observation trace net flow is invalid"
                )
            linked_flow_dates.update(interval_flow_dates)
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
            drawdown = cumulative_index / running_hwm - Decimal(1)
            max_drawdown = min(max_drawdown, drawdown)
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
        if linked_flow_dates != set(flow_by_day):
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
            if opening_basis == "PREDECESSOR_CLOSING_NAV"
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
                (date.fromisoformat(str(flow["date"])), sign * amount)
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
            if current_start != expected_start:
                raise ManifestError(
                    f"manifest row chain has a gap or overlap: {account_id}"
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


def validate_manifest_schema(manifest: Mapping[str, Any]) -> None:
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
    schema = InputSnapshot.capture(
        _MANIFEST_SCHEMA_PATH, "account strategy manifest schema"
    ).json_object("account strategy manifest schema")
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
    _validate_manifest_semantics(serializable)


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


def build_manifest_candidate(
    *,
    ledger_path: Path,
    ledger_identity_path: Path,
    baseline_root: Path,
    baseline_generation_id: str,
    classified_flows_path: Path,
    classification_policy_path: Path,
    admission_policy_path: Path,
) -> dict[str, Any]:
    """Build one internally validated source-bound manifest candidate.

    The inputs are independently selected paths.  Each is captured exactly
    once before parsing and identity binding so consumers can rebuild the
    result without trusting declarations embedded in a supplied manifest.
    """
    ledger_snapshot = InputSnapshot.capture(ledger_path, "ledger")
    ledger_identity_snapshot = InputSnapshot.capture(
        ledger_identity_path, "ledger identity"
    )
    selected_baseline_generation_id = _generation_id(
        baseline_generation_id, "baseline generation_id"
    )
    try:
        resolved_baseline_root = baseline_root.resolve(strict=True)
    except OSError as exc:
        raise ManifestError("baseline root is missing") from exc
    try:
        baseline_path = (
            resolved_baseline_root
            / "generations"
            / selected_baseline_generation_id
            / "portfolio-baseline.json"
        ).resolve(strict=True)
        baseline_path.relative_to(resolved_baseline_root)
    except (OSError, ValueError) as exc:
        raise ManifestError("baseline artifact escapes baseline root") from exc
    baseline_snapshot = InputSnapshot.capture(baseline_path, "baseline")
    classified_snapshot = InputSnapshot.capture(
        classified_flows_path, "classified flows"
    )
    classification_policy_snapshot = InputSnapshot.capture(
        classification_policy_path, "flow classification policy"
    )
    admission_policy_snapshot = InputSnapshot.capture(
        admission_policy_path, "admission policy"
    )
    calculation_code_snapshot = InputSnapshot.capture(
        Path(__file__), "calculation code"
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
    typed_assignments = [dict(assignment) for assignment in assignments]
    admissions, admission_policy_identity = load_admission_policy(
        admission_policy_snapshot.json_object("admission policy"),
        admission_policy_snapshot,
        typed_assignments,
    )
    chains = validate_assignment_topology(typed_assignments, admissions)
    classification_policy, classification_policy_identity = (
        load_flow_classification_policy(
            classification_policy_snapshot.json_object("flow classification policy"),
            classification_policy_snapshot,
        )
    )
    events, classified_artifact, classification_policy_reference = load_flow_classes(
        classified_snapshot.json_object("classified flows"),
        classified_snapshot,
        ledger_identity,
        classification_policy_identity,
    )
    reconciled_events = validate_classified_flow_coverage(
        ledger, events, classification_policy
    )
    buckets = flow_buckets_from_classes(reconciled_events)
    series, unavailable_navs = parse_nav_observations(ledger)
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
    validate_evidence_coverage(ledger, reconciled_events, chains, accounts_index)
    manifest = assemble_manifest(
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
                "schema_version": _FLOW_CLASSES_SCHEMA_VERSION,
                "ledger_identity": ledger_identity,
                "classification_policy": classification_policy_reference,
            },
            "flow_classification_policy": classification_policy_identity,
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
        unavailable_navs,
    )
    manifest["accounting_currency"] = ledger["accounting_currency"]
    validate_manifest_schema(manifest)
    return json_ready(manifest)  # type: ignore[return-value]


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
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)
    manifest = build_manifest_candidate(
        ledger_path=args.ledger_json,
        ledger_identity_path=args.ledger_identity_json,
        baseline_root=args.baseline_root,
        baseline_generation_id=args.baseline_generation_id,
        classified_flows_path=args.flow_classes_json,
        classification_policy_path=args.classification_policy_json,
        admission_policy_path=args.admission_policy_json,
    )
    if args.format == "json":
        print(json.dumps(manifest, indent=2))
    else:
        print(format_table(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

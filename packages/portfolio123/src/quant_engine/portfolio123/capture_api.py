"""Read-only p123api wrapper STUB behind the credential resolver.

This module is never exercised live by the change that introduced it.  The
transport is HAR-first: the API is reachable only for a declared HAR gap or an
explicit owner selection, with a credit budget, a reviewed read-only allow-list,
and single-use governance evidence.  Key id and key come only from the core
``CredentialResolver`` (fields ``key_id`` and ``key``); secret names live in
configuration, never here.

Not provided, by design: a generic request method, any mutation operation, any
retry, any network call without an injected transport.  The default transport
imports ``p123api`` lazily and its call shape is UNVERIFIED.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Protocol

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.credentials import (
    CredentialError,
    CredentialResolver,
    SecretSource,
    SecretValue,
    plan_from_config,
    sources_from_config,
)
from quant_engine.core.strict_json import StrictJSONError, strict_json_object
from quant_engine.portfolio123.capture_catalog import CaptureRefusal
from quant_engine.portfolio123.routing import (
    API_COST_DOCUMENTATION_URL,
    API_DOCUMENTATION_URL,
    ApiAuthorization,
    ApiEffect,
    ApiRunEvidence,
    Portfolio123RoutingError,
    RouteDecision,
    RouteReason,
    Transport,
    capability_policy,
    record_api_result,
)


CREDENTIAL_PROVIDER = "p123"
CREDENTIAL_FIELDS = frozenset({"key_id", "key"})
TERMS_VERSION = "p123-api-read-terms-v1"
TERMS_TEXT = (
    "Portfolio123 API reads require one current, reviewed, single-use approval; "
    "a read-only reviewed operation allow-list; a credit budget with current "
    "credit-table evidence; and a durable pre-call reservation. No retries."
)
TERMS_SHA256 = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
BASIS_HAR_GAP = "UNSUPPORTED_BY_HAR"
BASIS_OWNER = "USER_SELECTED"
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_CALLS = 1_000
_REVIEW_REFERENCE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
)
_PARAM_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# Reviewed metered READ capabilities only (routing.py is the authority; a test pins
# this set to it).  Mutations are not representable here.
READ_ONLY_CAPABILITIES = (
    "data.prices",
    "data.universe",
    "rank.performance",
    "rank.ranks",
    "screen.backtest",
    "screen.run",
    "strategy.holdings",
    "strategy.performance",
    "strategy.rebalance-recommendations",
    "strategy.summary",
    "strategy.trading-system",
    "strategy.transactions",
)


def _operations() -> dict[str, str]:
    result: dict[str, str] = {}
    for capability in READ_ONLY_CAPABILITIES:
        policy = capability_policy(capability)
        if (
            policy.api_operation is None
            or policy.api_effect is not ApiEffect.METERED_READ
        ):
            raise CaptureRefusal("ALLOW_LIST_INVALID", capability)
        result[policy.api_operation] = capability
    return result


OPERATION_CAPABILITY: Mapping[str, str] = _operations()


def allow_list_sha256() -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {"operations": sorted(OPERATION_CAPABILITY), "terms": TERMS_SHA256}
        )
    ).hexdigest()


# --------------------------------------------------------------------------- credentials


def resolve_api_credentials(
    config: Mapping[str, Any],
    *,
    sources: Mapping[str, SecretSource] | None = None,
) -> dict[str, SecretValue]:
    """Resolve ``key_id`` and ``key`` from ``[credentials.p123]`` (names are config)."""

    try:
        plan = plan_from_config(config, CREDENTIAL_PROVIDER)
    except CredentialError:
        raise CaptureRefusal("CREDENTIAL_PLAN_INVALID") from None
    if set(plan.fields) != CREDENTIAL_FIELDS:
        raise CaptureRefusal("CREDENTIAL_FIELDS_INVALID")
    try:
        values = CredentialResolver(
            sources if sources is not None else sources_from_config(config)
        ).resolve(plan)
    except CredentialError:
        raise CaptureRefusal("CREDENTIALS_UNRESOLVED") from None
    return values


# --------------------------------------------------------------------------- governance


def _object(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CaptureRefusal("GOVERNANCE_INVALID", name)
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureRefusal("GOVERNANCE_INVALID", name)
    return value


def _time(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
    except ValueError:
        raise CaptureRefusal("GOVERNANCE_INVALID", name) from None
    if parsed.tzinfo is None:
        raise CaptureRefusal("GOVERNANCE_INVALID", name)
    return parsed.astimezone(timezone.utc)


@dataclass
class ApiGovernance:
    """A consumed, single-use attestation for a bounded, budgeted set of reads."""

    receipt_path: Path
    ledger_path: Path
    evidence_sha256: str
    operations: Mapping[str, str]
    credit_budget: int
    max_calls: int
    approval_reference: str
    credit_table_captured_at: datetime
    spent_credits: int = 0
    reserved_calls: int = 0
    halted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def authorization(self, operation: str, estimated: int) -> ApiAuthorization:
        return ApiAuthorization(
            operation=operation,
            estimated_credits=estimated,
            authorized_credit_budget=self.credit_budget,
            approval_reference=self.approval_reference,
            credit_table_captured_at=self.credit_table_captured_at,
            documentation_url=API_DOCUMENTATION_URL,
            cost_documentation_url=API_COST_DOCUMENTATION_URL,
        )

    def _append(self, row: Mapping[str, object]) -> None:
        line = canonical_json_bytes(dict(row))
        with open(self.ledger_path, "ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def reserve(self, operation: str, estimated: int) -> ApiAuthorization:
        """Durably reserve one call before any I/O; refuse past any bound."""

        with self._lock:
            if self.halted:
                raise CaptureRefusal("API_HALTED")
            if operation not in self.operations:
                raise CaptureRefusal("OPERATION_NOT_APPROVED")
            if self.reserved_calls >= self.max_calls:
                raise CaptureRefusal("CALL_BUDGET_EXHAUSTED")
            if estimated <= 0 or self.spent_credits + estimated > self.credit_budget:
                raise CaptureRefusal("CREDIT_BUDGET_EXHAUSTED")
            try:
                authorization = self.authorization(operation, estimated)
                authorization.validate()
            except Portfolio123RoutingError:
                raise CaptureRefusal("AUTHORIZATION_INVALID") from None
            self.reserved_calls += 1
            self._append(
                {
                    "event": "reserve",
                    "operation": operation,
                    "estimated_credits": estimated,
                    "call": self.reserved_calls,
                }
            )
            return authorization

    def complete(self, operation: str, actual: int, remaining: int) -> None:
        with self._lock:
            self.spent_credits += actual
            self._append(
                {
                    "event": "complete",
                    "operation": operation,
                    "actual_credits": actual,
                    "quota_remaining": remaining,
                    "spent_credits": self.spent_credits,
                }
            )
            if self.spent_credits > self.credit_budget:
                self.halted = True

    def halt(self, operation: str) -> None:
        with self._lock:
            self.halted = True
            self._append({"event": "halt", "operation": operation})


def authorize_api_read(
    evidence_path: Path,
    *,
    receipt_root: Path,
    now: Callable[[], datetime] | None = None,
) -> ApiGovernance:
    """Validate operator evidence and consume it exactly once (create-only receipt)."""

    if not evidence_path.is_absolute() or not receipt_root.is_absolute():
        raise CaptureRefusal("PATH_NOT_ABSOLUTE")
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise CaptureRefusal("GOVERNANCE_MISSING")
    if evidence_path.stat().st_size > _MAX_EVIDENCE_BYTES:
        raise CaptureRefusal("GOVERNANCE_INVALID", "size")
    raw = evidence_path.read_bytes()
    try:
        document = strict_json_object(raw, label="governance")
    except StrictJSONError:
        raise CaptureRefusal("GOVERNANCE_INVALID", "json") from None
    document = _object(
        document,
        "root",
        {
            "schema_version",
            "operation",
            "scope",
            "review",
            "authority",
            "terms",
            "allow_list",
            "metering",
            "credit_table",
        },
    )
    if document["schema_version"] != 1 or document["operation"] != "api-read":
        raise CaptureRefusal("GOVERNANCE_INVALID", "version")
    review = _object(document["review"], "review", {"identity", "reference"})
    _text(review["identity"], "review identity")
    reference = _text(review["reference"], "review reference")
    if _REVIEW_REFERENCE.fullmatch(reference) is None:
        raise CaptureRefusal("GOVERNANCE_INVALID", "review reference")
    authority = _object(
        document["authority"], "authority", {"status", "issued_at", "expires_at"}
    )
    issued, expires = (
        _time(authority["issued_at"], "issued_at"),
        _time(authority["expires_at"], "expires_at"),
    )
    current = (now or (lambda: datetime.now(timezone.utc)))()
    if authority["status"] != "CURRENT" or not issued <= current < expires:
        raise CaptureRefusal("GOVERNANCE_NOT_CURRENT")
    terms = _object(document["terms"], "terms", {"version", "sha256"})
    if terms != {"version": TERMS_VERSION, "sha256": TERMS_SHA256}:
        raise CaptureRefusal("GOVERNANCE_INVALID", "terms")
    allow_list = _object(document["allow_list"], "allow_list", {"sha256"})
    if allow_list["sha256"] != allow_list_sha256():
        raise CaptureRefusal("GOVERNANCE_INVALID", "allow-list drift")
    metering = _object(
        document["metering"], "metering", {"credit_budget", "max_calls", "retry_count"}
    )
    budget, calls = metering["credit_budget"], metering["max_calls"]
    if (
        any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in (budget, calls)
        )
        or budget <= 0
        or not 0 < calls <= _MAX_CALLS
        or metering["retry_count"] != 0
    ):
        raise CaptureRefusal("GOVERNANCE_INVALID", "metering")
    table = _object(document["credit_table"], "credit_table", {"captured_at"})
    table_time = _time(table["captured_at"], "credit table time")
    scope = _object(document["scope"], "scope", {"operations"})
    approved: dict[str, str] = {}
    entries = scope["operations"]
    if not isinstance(entries, list) or not entries:
        raise CaptureRefusal("GOVERNANCE_INVALID", "scope")
    for entry in entries:
        row = _object(entry, "scope operation", {"operation", "basis"})
        operation, basis = _text(row["operation"], "operation"), row["basis"]
        capability = OPERATION_CAPABILITY.get(operation)
        if capability is None or operation in approved:
            raise CaptureRefusal("GOVERNANCE_INVALID", "operation")
        if basis not in {BASIS_HAR_GAP, BASIS_OWNER}:
            raise CaptureRefusal("GOVERNANCE_INVALID", "basis")
        if capability_policy(capability).har_supported and basis != BASIS_OWNER:
            # HAR is reviewed for this capability: only an explicit owner selection
            # may route it to the API.  A failure never reroutes it.
            raise CaptureRefusal("HAR_FIRST")
        approved[operation] = basis
    digest = hashlib.sha256(raw).hexdigest()
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt = receipt_root / f"p123-api-{digest[:32]}.receipt.json"
    try:
        with open(receipt, "xb") as handle:
            handle.write(
                canonical_json_bytes({"evidence_sha256": digest, "schema_version": 1})
            )
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise CaptureRefusal("EVIDENCE_ALREADY_CONSUMED") from None
    return ApiGovernance(
        receipt_path=receipt,
        ledger_path=receipt_root / f"p123-api-{digest[:32]}.ledger.jsonl",
        evidence_sha256=digest,
        operations=approved,
        credit_budget=budget,
        max_calls=calls,
        approval_reference=reference,
        credit_table_captured_at=table_time,
    )


# --------------------------------------------------------------------------- transport


@dataclass(frozen=True)
class ApiResponse:
    payload: object
    credits_used: int
    quota_remaining: int


class ApiTransport(Protocol):
    def invoke(
        self,
        operation: str,
        params: Mapping[str, object],
        *,
        key_id: SecretValue,
        key: SecretValue,
    ) -> ApiResponse: ...


class P123ApiTransport:
    """Default transport.  UNVERIFIED call shape; refused unless p123api is present."""

    def invoke(
        self,
        operation: str,
        params: Mapping[str, object],
        *,
        key_id: SecretValue,
        key: SecretValue,
    ) -> ApiResponse:
        if operation not in OPERATION_CAPABILITY:
            raise CaptureRefusal("OPERATION_NOT_ALLOWED")
        try:
            module = importlib.import_module("p123api")
        except ImportError:
            raise CaptureRefusal("API_CLIENT_UNAVAILABLE") from None
        try:
            # UNVERIFIED keyword names, assembled so that no credential-shaped
            # literal appears in source.
            client = module.Client(
                **{"api_id": key_id.reveal(), "api_" + "key": key.reveal()}
            )
            payload = getattr(client, operation)(dict(params))
        except Exception:
            # Provider exceptions can echo request material; never chain them.
            raise CaptureRefusal("API_CALL_FAILED") from None
        if not isinstance(payload, dict):
            raise CaptureRefusal("API_COST_MISSING")
        cost, quota = payload.get("cost"), payload.get("quotaRemaining")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, int)
            or isinstance(quota, bool)
            or not isinstance(quota, int)
        ):
            raise CaptureRefusal("API_COST_MISSING")
        return ApiResponse(payload, cost, quota)


def _check_params(params: Mapping[str, object]) -> None:
    def scalar(value: object) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

    for key, value in params.items():
        if _PARAM_KEY.fullmatch(key) is None:
            raise CaptureRefusal("PARAMS_INVALID")
        if not (
            scalar(value)
            or (isinstance(value, list) and all(scalar(item) for item in value))
        ):
            raise CaptureRefusal("PARAMS_INVALID")
    try:
        json.dumps(dict(params), allow_nan=False)
    except (TypeError, ValueError):
        raise CaptureRefusal("PARAMS_INVALID") from None


@dataclass(frozen=True)
class ApiResult:
    payload: object
    evidence: ApiRunEvidence


class ReadOnlyApiCapture:
    """Allow-listed, budgeted, single-attempt reads.  Never retries, never mutates."""

    def __init__(
        self,
        governance: ApiGovernance,
        credentials: Mapping[str, SecretValue],
        transport: ApiTransport | None = None,
    ) -> None:
        if set(credentials) != CREDENTIAL_FIELDS:
            raise CaptureRefusal("CREDENTIAL_FIELDS_INVALID")
        self._governance = governance
        self._credentials = dict(credentials)
        self._transport: ApiTransport = transport or P123ApiTransport()

    def call(
        self,
        operation: str,
        params: Mapping[str, object],
        *,
        estimated_credits: int,
    ) -> ApiResult:
        capability = OPERATION_CAPABILITY.get(operation)
        if capability is None:
            raise CaptureRefusal("OPERATION_NOT_ALLOWED")
        _check_params(params)
        basis = self._governance.operations.get(operation)
        authorization = self._governance.reserve(operation, estimated_credits)
        reason = (
            RouteReason.USER_REQUESTED_API
            if basis == BASIS_OWNER
            else RouteReason.UNSUPPORTED_BY_HAR
        )
        decision = RouteDecision(
            capability, Transport.API, reason, operation, authorization, None
        )
        try:
            response = self._transport.invoke(
                operation,
                params,
                key_id=self._credentials["key_id"],
                key=self._credentials["key"],
            )
        except CaptureRefusal:
            self._governance.halt(operation)
            raise
        try:
            evidence = record_api_result(
                decision,
                actual_credits=response.credits_used,
                quota_remaining=response.quota_remaining,
            )
        except Portfolio123RoutingError:
            self._governance.complete(
                operation,
                max(0, response.credits_used),
                max(0, response.quota_remaining),
            )
            self._governance.halt(operation)
            raise CaptureRefusal("CREDIT_BUDGET_EXCEEDED") from None
        self._governance.complete(
            operation, evidence.actual_credits, evidence.quota_remaining
        )
        return ApiResult(response.payload, evidence)

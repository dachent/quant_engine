"""Deterministic offline extraction of sanitized web-contract evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.web_contracts.contracts import ProviderContract, WebContractError


REDACTION = "[REDACTED]"
_SENSITIVE_NAME = re.compile(r"(?i)(?:auth|cookie|session|token|secret|password)")
_TOKEN_LIKE = re.compile(r"(?i)^(?:bearer\s+)?[A-Za-z0-9._~+/=-]{24,}$")


@dataclass(frozen=True)
class SessionEnvelope:
    """Non-secret validity bounds for the captured browser session."""

    captured_at: datetime
    expires_at: datetime

    def validate(self) -> None:
        if self.captured_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise WebContractError("session times must be timezone-aware")
        if self.expires_at <= self.captured_at:
            raise WebContractError("captured session is expired")


@dataclass(frozen=True)
class ExtractedResponse:
    """One sanitized response bound to a reviewed capability."""

    capability: str
    captured_at: datetime
    request_sha256: str
    response: object
    status: int

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "captured_at": self.captured_at.astimezone(timezone.utc).isoformat(),
            "request_sha256": self.request_sha256,
            "response": self.response,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractedResponse:
        """Rebuild one record from its sole serialized representation."""

        capability = value.get("capability")
        captured_at = value.get("captured_at")
        request_sha256 = value.get("request_sha256")
        status = value.get("status")
        if not isinstance(capability, str) or not isinstance(request_sha256, str):
            raise WebContractError("serialized record identity must be text")
        if not isinstance(captured_at, str):
            raise WebContractError("serialized capture time must be text")
        if isinstance(status, bool) or not isinstance(status, int):
            raise WebContractError("serialized status must be an integer")
        try:
            observed = datetime.fromisoformat(captured_at)
        except ValueError as exc:
            raise WebContractError("serialized capture time is invalid") from exc
        if observed.tzinfo is None:
            raise WebContractError("serialized capture time must be timezone-aware")
        if "response" not in value:
            raise WebContractError("serialized record response is missing")
        return cls(
            capability=capability,
            captured_at=observed,
            request_sha256=request_sha256,
            response=value["response"],
            status=status,
        )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise WebContractError(f"{name} must be an array")
    return value


def _headers(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _array(value, "request.headers"):
        record = _object(item, "request.headers[]")
        name = record.get("name")
        header_value = record.get("value")
        if not isinstance(name, str) or not isinstance(header_value, str):
            raise WebContractError("request header names and values must be text")
        lowered = name.casefold()
        if lowered in result and lowered in {"authorization", "jwt", "cookie"}:
            raise WebContractError("duplicate sensitive request header")
        result.setdefault(lowered, header_value)
    return result


def _request_body(request: Mapping[str, Any]) -> dict[str, Any]:
    value = request.get("postData")
    if value is None:
        return {}
    post_data = _object(value, "request.postData")
    text = post_data.get("text")
    if not isinstance(text, str):
        raise WebContractError("request body text is required")
    try:
        return _object(json.loads(text), "request body")
    except json.JSONDecodeError as exc:
        raise WebContractError("request body is not valid JSON") from exc


def _sanitize(value: object, sensitive_fields: frozenset[str]) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise WebContractError("response keys must be text")
            if key.casefold() in sensitive_fields or _SENSITIVE_NAME.search(key):
                result[key] = REDACTION
            else:
                result[key] = _sanitize(value[key], sensitive_fields)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item, sensitive_fields) for item in value]
    if isinstance(value, str) and _TOKEN_LIKE.fullmatch(value.strip()):
        return REDACTION
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WebContractError("response contains an unsupported value")


def _response_json(response: Mapping[str, Any]) -> object:
    content = _object(response.get("content"), "response.content")
    text = content.get("text")
    if not isinstance(text, str):
        raise WebContractError("response content text is required")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WebContractError("response content is not valid JSON") from exc


def sanitize_response(payload: object, provider: ProviderContract) -> object:
    """Redact provider-declared sensitive fields from one response payload."""

    return _sanitize(payload, provider.sensitive_fields)


def extract_har(
    document: Mapping[str, Any],
    *,
    provider: ProviderContract,
    session: SessionEnvelope,
) -> tuple[ExtractedResponse, ...]:
    """Extract only uniquely allowlisted, successful, authenticated entries."""

    session.validate()
    root = _object(document.get("log"), "log")
    entries = _array(root.get("entries"), "log.entries")
    if not entries:
        raise WebContractError("capture has no entries")
    extracted: list[ExtractedResponse] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        response = _object(entry.get("response"), "response")
        method = request.get("method")
        url = request.get("url")
        status = response.get("status")
        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")
        if isinstance(status, bool) or not isinstance(status, int):
            raise WebContractError("response status must be an integer")
        endpoint = provider.route(method, url)
        headers = _headers(request.get("headers"))
        if endpoint.authentication_header is not None:
            auth_value = headers.get(endpoint.authentication_header, "")
            if len(auth_value.strip()) < 16:
                raise WebContractError("authenticated session evidence is missing")
            if (
                endpoint.authentication_header == "authorization"
                and not auth_value.casefold().startswith("bearer ")
            ):
                raise WebContractError("authenticated session evidence is missing")
        query = endpoint.query(url)
        if endpoint.validate_query is not None:
            endpoint.validate_query(query)
        request_body = _request_body(request)
        if endpoint.validate_request is not None:
            endpoint.validate_request(request_body)
        elif request.get("postData") is not None:
            raise WebContractError("request body has no reviewed validator")
        if status < 200 or status >= 300:
            raise WebContractError("allowlisted request did not succeed")
        payload = _response_json(response)
        if endpoint.validate_response is not None:
            endpoint.validate_response(payload)
        request_identity = {
            "capability": endpoint.capability,
            "body": request_body if endpoint.validate_request is not None else None,
            "method": endpoint.method,
            "origin": endpoint.origin,
            "path": urlsplit(url).path,
            "query": query,
        }
        request_sha256 = hashlib.sha256(
            canonical_json_bytes(request_identity)
        ).hexdigest()
        extracted.append(
            ExtractedResponse(
                capability=endpoint.capability,
                captured_at=session.captured_at,
                request_sha256=request_sha256,
                response=_sanitize(payload, provider.sensitive_fields),
                status=status,
            )
        )
    extracted.sort(key=lambda item: (item.capability, item.request_sha256))
    return tuple(extracted)


def canonical_extraction(value: Sequence[ExtractedResponse]) -> bytes:
    """Return the sole deterministic serialized representation."""

    return canonical_json_bytes({"records": [item.to_dict() for item in value]})


def parse_extraction(raw: bytes) -> tuple[ExtractedResponse, ...]:
    """Rebuild records from persisted canonical bytes, fail-closed."""

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebContractError("persisted extraction is not canonical JSON") from exc
    records = _object(document, "extraction").get("records")
    if not isinstance(records, list):
        raise WebContractError("persisted extraction records must be an array")
    parsed = tuple(
        ExtractedResponse.from_dict(_object(item, "records[]")) for item in records
    )
    if canonical_extraction(parsed) != raw:
        raise WebContractError("persisted extraction is not in canonical form")
    return parsed

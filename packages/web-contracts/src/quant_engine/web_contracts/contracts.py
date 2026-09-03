"""Provider-neutral contracts for authenticated web evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit


class WebContractError(ValueError):
    """Captured evidence violates a declared web contract."""


class SemanticEffect(StrEnum):
    """The externally observable effect of an endpoint."""

    READ_ONLY = "READ_ONLY"


RequestValidator = Callable[[Mapping[str, Any]], None]
ResponseValidator = Callable[[Any], None]


@dataclass(frozen=True)
class EndpointContract:
    """One exact method, origin, and route with an independently reviewed effect."""

    capability: str
    method: str
    origin: str
    path: str
    semantic_effect: SemanticEffect
    validate_request: RequestValidator | None = None
    validate_response: ResponseValidator | None = None
    path_pattern: str | None = None
    query_keys: frozenset[str] = frozenset()
    validate_query: RequestValidator | None = None
    authentication_header: str | None = "authorization"

    def __post_init__(self) -> None:
        method = self.method.upper()
        parsed = urlsplit(self.origin)
        if method not in {"GET", "POST"}:
            raise WebContractError("endpoint method is not supported")
        if parsed.scheme != "https" or not parsed.netloc or parsed.path:
            raise WebContractError("endpoint origin must be an HTTPS origin")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise WebContractError("endpoint path must be an exact absolute path")
        if self.path_pattern is not None:
            try:
                re.compile(self.path_pattern)
            except re.error as exc:
                raise WebContractError("endpoint path pattern is invalid") from exc
        query_keys = frozenset(item.casefold() for item in self.query_keys)
        if any(re.fullmatch(r"[a-z][a-z0-9_.-]*", item) is None for item in query_keys):
            raise WebContractError("endpoint query key is not portable")
        authentication_header = self.authentication_header
        if authentication_header is not None:
            authentication_header = authentication_header.casefold()
            if re.fullmatch(r"[a-z][a-z0-9-]*", authentication_header) is None:
                raise WebContractError("authentication header is invalid")
        if self.semantic_effect is not SemanticEffect.READ_ONLY:
            raise WebContractError("only read-only contracts are accepted")
        if re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", self.capability) is None:
            raise WebContractError("capability is not portable")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "origin", self.origin.rstrip("/"))
        object.__setattr__(self, "query_keys", query_keys)
        object.__setattr__(self, "authentication_header", authentication_header)

    def matches(self, method: str, url: str) -> bool:
        parsed = urlsplit(url)
        actual_origin = f"{parsed.scheme}://{parsed.netloc}"
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        actual_query_keys = [key.casefold() for key, _ in pairs]
        path_matches = (
            parsed.path == self.path
            if self.path_pattern is None
            else re.fullmatch(self.path_pattern, parsed.path) is not None
        )
        return (
            method.upper() == self.method
            and actual_origin == self.origin
            and path_matches
            and len(actual_query_keys) == len(set(actual_query_keys))
            and frozenset(actual_query_keys) == self.query_keys
            and not parsed.fragment
        )

    def query(self, url: str) -> dict[str, str]:
        """Return a unique decoded query mapping after identity matching."""

        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        result = {key.casefold(): value for key, value in pairs}
        if len(result) != len(pairs) or frozenset(result) != self.query_keys:
            raise WebContractError("request query differs from the reviewed shape")
        return result


@dataclass(frozen=True)
class ProviderContract:
    """A deny-by-default set of reviewed provider endpoints."""

    provider: str
    endpoints: tuple[EndpointContract, ...]
    sensitive_fields: frozenset[str]
    api_fallback: bool = False

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.provider) is None:
            raise WebContractError("provider is not portable")
        if not self.endpoints:
            raise WebContractError("provider must declare at least one endpoint")
        identities = {(item.method, item.origin, item.path) for item in self.endpoints}
        if len(identities) != len(self.endpoints):
            raise WebContractError("endpoint identities must be unique")
        if self.api_fallback:
            raise WebContractError("web contracts cannot enable API fallback")

    def route(self, method: str, url: str) -> EndpointContract:
        matched = [item for item in self.endpoints if item.matches(method, url)]
        if len(matched) != 1:
            raise WebContractError("request is not uniquely allowlisted")
        return matched[0]

"""Provider-neutral contracts for authenticated web evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any
from urllib.parse import urlsplit


class WebContractError(ValueError):
    """Captured evidence violates a declared web contract."""


class SemanticEffect(StrEnum):
    """The externally observable effect of an endpoint."""

    READ_ONLY = "READ_ONLY"


RequestValidator = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class EndpointContract:
    """One exact method, origin, and route with an independently reviewed effect."""

    capability: str
    method: str
    origin: str
    path: str
    semantic_effect: SemanticEffect
    validate_request: RequestValidator | None = None

    def __post_init__(self) -> None:
        method = self.method.upper()
        parsed = urlsplit(self.origin)
        if method not in {"GET", "POST"}:
            raise WebContractError("endpoint method is not supported")
        if parsed.scheme != "https" or not parsed.netloc or parsed.path:
            raise WebContractError("endpoint origin must be an HTTPS origin")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise WebContractError("endpoint path must be an exact absolute path")
        if self.semantic_effect is not SemanticEffect.READ_ONLY:
            raise WebContractError("only read-only contracts are accepted")
        if re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", self.capability) is None:
            raise WebContractError("capability is not portable")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "origin", self.origin.rstrip("/"))

    def matches(self, method: str, url: str) -> bool:
        parsed = urlsplit(url)
        actual_origin = f"{parsed.scheme}://{parsed.netloc}"
        return (
            method.upper() == self.method
            and actual_origin == self.origin
            and parsed.path == self.path
            and not parsed.query
            and not parsed.fragment
        )


@dataclass(frozen=True)
class ProviderContract:
    """A deny-by-default set of reviewed provider endpoints."""

    provider: str
    endpoints: tuple[EndpointContract, ...]
    sensitive_fields: frozenset[str]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.provider) is None:
            raise WebContractError("provider is not portable")
        if not self.endpoints:
            raise WebContractError("provider must declare at least one endpoint")
        identities = {(item.method, item.origin, item.path) for item in self.endpoints}
        if len(identities) != len(self.endpoints):
            raise WebContractError("endpoint identities must be unique")

    def route(self, method: str, url: str) -> EndpointContract:
        matched = [item for item in self.endpoints if item.matches(method, url)]
        if len(matched) != 1:
            raise WebContractError("request is not uniquely allowlisted")
        return matched[0]

"""GET-only transports for the capture driver.

Two layers.  :class:`GovernedFetcher` is the only object the pipeline talks to:
it takes a method, refuses anything but an allow-listed GET (and any trading,
order, or transfer route), durably reserves the request against the governance
budget, and only then calls the low-level :class:`RawFetcher`.  The low-level
fetcher has no method parameter at all, so it cannot send anything but a GET.

:class:`HttpsFetcher` is the live implementation.  It is never exercised live in
this change; tests replace the opener.  It follows no redirects, accepts only the
two approved API hosts, bounds the response size, performs no retry, and keeps
the resolved key material in a redacted header map.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from quant_engine.alpaca.capture.catalog import (
    ENVIRONMENTS,
    Endpoint,
    authorize_request,
)
from quant_engine.alpaca.capture.errors import CaptureError, CaptureRefused
from quant_engine.alpaca.capture.governance import LiveReadGovernance
from quant_engine.alpaca.capture.util import encode_query
from quant_engine.core import SecretValue

_KEPT_HEADERS = frozenset(
    {"content-type", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining"}
    | {"x-ratelimit-reset"}
)
_KEY_ID_HEADER = "APCA-API-" + "KEY-ID"
_KEY_VALUE_HEADER = "APCA-API-" + "SECRET-KEY"


@dataclass(frozen=True)
class RawResponse:
    """One HTTP response: status, exact body bytes, and a few safe headers."""

    status: int
    body: bytes
    headers: Mapping[str, str]


class RawFetcher(Protocol):
    """Low-level capability: fetch one GET path with a query, nothing else."""

    def fetch(self, path: str, query: Mapping[str, str]) -> RawResponse: ...


class GovernedFetcher:
    """Policy and governance in front of a :class:`RawFetcher`."""

    def __init__(self, inner: RawFetcher, governance: LiveReadGovernance) -> None:
        self._inner = inner
        self._governance = governance

    def get(
        self, method: str, path: str, query: Mapping[str, str] | None = None
    ) -> tuple[Endpoint, RawResponse]:
        parameters = dict(query or {})
        endpoint = authorize_request(method, path, parameters)
        self._governance.reserve_request(endpoint)
        return endpoint, self._inner.fetch(path, parameters)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _safe_headers(headers: Any) -> dict[str, str]:
    kept: dict[str, str] = {}
    if headers is None:
        return kept
    for name, value in headers.items():
        lowered = str(name).lower()
        if lowered in _KEPT_HEADERS:
            kept[lowered] = str(value)
    return kept


class HttpsFetcher:
    """HTTPS GET with fixed host, bounded size, no redirects, no retry."""

    def __init__(
        self,
        environment: str,
        key_id: SecretValue,
        key_value: SecretValue,
        *,
        timeout_seconds: float = 30.0,
        maximum_response_bytes: int = 20 * 1024 * 1024,
        opener: OpenerDirector | None = None,
    ) -> None:
        if environment not in ENVIRONMENTS:
            raise CaptureRefused("environment must be explicitly 'paper' or 'live'")
        if timeout_seconds <= 0 or maximum_response_bytes <= 0:
            raise CaptureRefused("transport bounds must be positive")
        self._base_url = ENVIRONMENTS[environment]
        self._headers = {
            _KEY_ID_HEADER: key_id.reveal(),
            _KEY_VALUE_HEADER: key_value.reveal(),
            "Accept": "application/json",
        }
        self._timeout = timeout_seconds
        self._limit = maximum_response_bytes
        self._opener = opener or build_opener(_NoRedirect)

    def __repr__(self) -> str:
        return "HttpsFetcher(<redacted>)"

    __str__ = __repr__

    def fetch(self, path: str, query: Mapping[str, str]) -> RawResponse:
        # Re-validate here too: a transport must not trust its caller.
        authorize_request("GET", path, query)
        suffix = f"?{encode_query(query)}" if query else ""
        request = Request(
            f"{self._base_url}{path}{suffix}", headers=self._headers, method="GET"
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = int(response.status)
                body = response.read(self._limit + 1)
                headers = _safe_headers(response.headers)
        except HTTPError as error:
            # A 4xx/5xx is a response, not a transport failure: keep its bytes.
            try:
                status = int(error.code)
                body = error.read(self._limit + 1)
                headers = _safe_headers(error.headers)
            finally:
                error.close()
        except (URLError, OSError, ValueError):
            raise CaptureError("the provider request failed at the transport") from None
        if len(body) > self._limit:
            raise CaptureError("the response exceeded the configured byte limit")
        return RawResponse(status=status, body=body, headers=headers)

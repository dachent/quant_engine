"""Hardened, GET-only Matrixport HTTP transport."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .catalog import BASE_URL, PRODUCTION_HOST, Endpoint, get_endpoint
from .credentials import ApiCredentials
from .governance import LiveReadGovernance
from .signing import QueryParams, build_v2_headers, encode_query


class TransportPolicyError(ValueError):
    """A request was rejected locally before network I/O."""


class TransportError(RuntimeError):
    """A sanitized request/response failure safe for logs."""


class TransportHTTPError(TransportError):
    """Sanitized HTTP failure retaining bytes only for the raw evidence sink."""

    def __init__(
        self,
        *,
        status_code: int,
        content: bytes,
        request_id: str,
        elapsed_ms: int,
    ) -> None:
        super().__init__(
            f"Matrixport request failed (status={status_code}, request_id={request_id})"
        )
        self.status_code = int(status_code)
        self.content = bytes(content)
        self.request_id = str(request_id)
        self.elapsed_ms = int(elapsed_ms)


@dataclass(frozen=True, slots=True)
class SanitizedResponse:
    status_code: int
    content: bytes
    request_id: str
    elapsed_ms: int
    endpoint_name: str

    def json(self) -> Any:
        try:
            return json.loads(self.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError(
                f"Matrixport returned invalid JSON (request_id={self.request_id})"
            ) from None


def _parameter_items(params: QueryParams | None) -> list[tuple[str, Any]]:
    if params is None:
        return []
    if isinstance(params, Mapping):
        return list(params.items())
    try:
        items = list(params)
    except TypeError:
        raise TransportPolicyError(
            "params must be a mapping or sequence of pairs"
        ) from None
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TransportPolicyError("params must be a mapping or sequence of pairs")
    return items


class ReadOnlyTransport:
    """A production-host-only transport with a semantic endpoint allowlist."""

    def __init__(
        self,
        credentials: ApiCredentials,
        *,
        session: requests.Session | None = None,
        requests_per_second: float = 1.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        governance: LiveReadGovernance | None = None,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if governance is None:
            raise ValueError("live-read governance evidence is required")
        if max_retries != 0:
            raise ValueError("governed live reads require max_retries=0")
        self._credentials = credentials
        self._session = session or requests.Session()
        self._minimum_interval = 1.0 / requests_per_second
        self._timeout = (connect_timeout, read_timeout)
        self._max_retries = max_retries
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleeper
        self._random = random_source
        self._governance = governance
        self._rate_lock = threading.Lock()
        self._last_request_started: float | None = None

    def _resolve_endpoint(self, target: str | Endpoint, method: str) -> Endpoint:
        normalized_method = method.upper()
        if normalized_method != "GET":
            raise TransportPolicyError("ReadOnlyTransport permits GET only")
        if isinstance(target, Endpoint):
            endpoint = target
        elif isinstance(target, str):
            split = urlsplit(target)
            if split.scheme or split.netloc:
                if split.scheme != "https" or split.hostname != PRODUCTION_HOST:
                    raise TransportPolicyError(
                        "Only the Matrixport production HTTPS host is permitted"
                    )
                if (
                    split.port is not None
                    or split.username is not None
                    or split.password is not None
                ):
                    raise TransportPolicyError(
                        "Ports and URL credentials are forbidden"
                    )
                if split.query or split.fragment:
                    raise TransportPolicyError(
                        "Pass query values through params, not in the target URL"
                    )
                path = split.path
            else:
                if (
                    not target.startswith("/")
                    or "?" in target
                    or "#" in target
                    or "\\" in target
                ):
                    raise TransportPolicyError(
                        "Target must be a clean absolute API path"
                    )
                path = target
            try:
                endpoint = get_endpoint(path, method="GET", host=PRODUCTION_HOST)
            except KeyError:
                raise TransportPolicyError(
                    "The requested endpoint is not in the read-only catalog"
                ) from None
        else:
            raise TransportPolicyError("Target must be an Endpoint or path string")

        # Re-resolve dataclass instances so callers cannot construct a lookalike
        # Endpoint with relaxed flags or parameter sets.
        try:
            canonical = get_endpoint(
                endpoint.path, method=endpoint.method, host=endpoint.host
            )
        except KeyError:
            raise TransportPolicyError(
                "The requested endpoint is not in the read-only catalog"
            ) from None
        if endpoint != canonical:
            raise TransportPolicyError("Non-canonical endpoint objects are forbidden")
        if canonical.method != "GET":
            raise TransportPolicyError("ReadOnlyTransport permits GET only")
        if canonical.host != PRODUCTION_HOST:
            raise TransportPolicyError(
                "Only the Matrixport production host is permitted"
            )
        if canonical.safety_class != "read_only" or not canonical.live_enabled:
            raise TransportPolicyError(
                "Endpoint is cataloged but blocked by the read-only policy"
            )
        return canonical

    @staticmethod
    def _validate_params(endpoint: Endpoint, params: QueryParams | None) -> None:
        items = _parameter_items(params)
        names: set[str] = set()
        values: dict[str, list[Any]] = {}
        for name, value in items:
            if not isinstance(name, str) or not name:
                raise TransportPolicyError(
                    "Every query parameter name must be a non-empty string"
                )
            if name not in endpoint.allowed_params:
                raise TransportPolicyError(
                    f"Parameter {name!r} is not allowed for endpoint {endpoint.name!r}"
                )
            names.add(name)
            values.setdefault(name, []).append(value)
        missing = endpoint.required_params - names
        if missing:
            raise TransportPolicyError(
                f"Missing required parameters for endpoint {endpoint.name!r}: {', '.join(sorted(missing))}"
            )
        for name in endpoint.required_params:
            if all(value is None or value == "" for value in values.get(name, [])):
                raise TransportPolicyError(
                    f"Required parameter {name!r} must be non-empty"
                )

    def _rate_limit(self) -> None:
        with self._rate_lock:
            now = self._monotonic()
            if self._last_request_started is not None:
                wait = self._minimum_interval - (now - self._last_request_started)
                if wait > 0:
                    self._sleep(wait)
                    now = self._monotonic()
            self._last_request_started = now

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    return max(0.0, min(float(raw), 60.0))
                except ValueError:
                    try:
                        when = parsedate_to_datetime(raw)
                        return max(0.0, min(when.timestamp() - self._clock(), 60.0))
                    except (TypeError, ValueError, OverflowError):
                        pass
        # Capped exponential delay plus bounded jitter.  Neither response bodies
        # nor prepared headers are inspected/logged.
        return min(2.0**attempt, 30.0) + self._random() * 0.25

    def request(
        self,
        method: str,
        target: str | Endpoint,
        *,
        params: QueryParams | None = None,
    ) -> SanitizedResponse:
        endpoint = self._resolve_endpoint(target, method)
        self._validate_params(endpoint, params)
        self._governance.reserve_request(endpoint)
        encoded_query = encode_query(params)
        url = f"{BASE_URL}{endpoint.path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"

        request_id = str(uuid.uuid4())
        retry_statuses = {429, 500, 502, 503, 504}
        started = self._monotonic()
        for attempt in range(self._max_retries + 1):
            self._rate_limit()
            timestamp_ms = int(self._clock() * 1000)
            headers = build_v2_headers(
                self._credentials,
                timestamp_ms,
                endpoint.path,
                encoded_query,
                request_id=request_id,
            )
            response: requests.Response | None = None
            try:
                response = self._session.request(
                    method="GET",
                    url=url,
                    headers=headers,
                    timeout=self._timeout,
                    allow_redirects=False,
                    verify=True,
                )
            except requests.RequestException:
                if attempt >= self._max_retries:
                    raise TransportError(
                        f"Matrixport network request failed (request_id={request_id})"
                    ) from None
                self._sleep(self._retry_delay(None, attempt))
                continue

            if 300 <= response.status_code < 400:
                raise TransportError(
                    f"Matrixport redirect was rejected (status={response.status_code}, request_id={request_id})"
                )
            if response.status_code in retry_statuses and attempt < self._max_retries:
                self._sleep(self._retry_delay(response, attempt))
                continue
            if not 200 <= response.status_code < 300:
                elapsed_ms = max(0, int((self._monotonic() - started) * 1000))
                raise TransportHTTPError(
                    status_code=response.status_code,
                    content=bytes(response.content),
                    request_id=request_id,
                    elapsed_ms=elapsed_ms,
                )
            elapsed_ms = max(0, int((self._monotonic() - started) * 1000))
            return SanitizedResponse(
                status_code=response.status_code,
                content=bytes(response.content),
                request_id=request_id,
                elapsed_ms=elapsed_ms,
                endpoint_name=endpoint.name,
            )

        raise TransportError(f"Matrixport request failed (request_id={request_id})")

    def get(
        self,
        target: str | Endpoint,
        params: QueryParams | None = None,
    ) -> SanitizedResponse:
        return self.request("GET", target, params=params)

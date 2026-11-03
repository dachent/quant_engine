"""HTTPS GET transport and request pacing for the Flex Web Service.

Nothing in this module runs unless a caller constructs ``UrllibHttpGet`` and
passes it to ``capture_plan``; the test suite replaces the opener with a fake
and is socket-disabled.  The token travels as a query parameter (the service
requires it), so no message, exception or repr here ever includes the URL.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
import time
from typing import IO, Any
from urllib import error as urlerror
from urllib import parse, request

from .catalog import ALLOWED_HOSTS

MAX_RESPONSE_BYTES = 256 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 180.0
MIN_SPACING_SECONDS = 1.1
MAX_CALLS_PER_MINUTE = 10

Opener = Callable[[request.Request, float], IO[bytes]]


class TransportError(RuntimeError):
    """A sanitized transport failure (never carries the URL or parameters)."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_opener(req: request.Request, timeout: float) -> IO[bytes]:
    director = request.build_opener(_NoRedirect, request.HTTPSHandler)
    return director.open(req, timeout=timeout)  # type: ignore[no-any-return]


class UrllibHttpGet:
    """``HttpGet`` over HTTPS to the approved Flex hosts only; no redirects."""

    def __init__(self, *, opener: Opener | None = None) -> None:
        self._opener = opener or _default_opener

    def __repr__(self) -> str:
        return "UrllibHttpGet()"

    def __call__(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> bytes:
        parts = parse.urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in ALLOWED_HOSTS
            or parts.query
            or parts.username is not None
        ):
            raise TransportError("the request host is not an approved Flex host")
        target = f"{url}?{parse.urlencode(dict(params))}"
        req = request.Request(target, headers=dict(headers), method="GET")
        try:
            with self._opener(req, REQUEST_TIMEOUT_SECONDS) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urlerror.HTTPError as exc:
            raise TransportError(f"the service answered HTTP {exc.code}") from None
        except (urlerror.URLError, OSError, ValueError):
            raise TransportError("the service request failed") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise TransportError("the service response exceeds the size bound")
        return body


class Pacer:
    """Enforce at most one call per second and ten per minute per token."""

    def __init__(
        self,
        *,
        wait: Callable[[float], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait = wait
        self._clock = clock
        self._calls: deque[float] = deque()

    def before_call(self) -> None:
        now = self._clock()
        delay = 0.0
        if self._calls:
            delay = max(delay, MIN_SPACING_SECONDS - (now - self._calls[-1]))
        recent = [stamp for stamp in self._calls if now - stamp < 60.0]
        if len(recent) >= MAX_CALLS_PER_MINUTE:
            delay = max(delay, 60.0 - (now - recent[0]))
        if delay > 0:
            self._wait(delay)
            now = self._clock()
        self._calls.append(now)
        while len(self._calls) > MAX_CALLS_PER_MINUTE:
            self._calls.popleft()

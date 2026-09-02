"""GET-only, HTTPS-only, zero-retry urllib transport.

The fetch seam ``fetch(url) -> (status_code, body_bytes)`` is injectable: the
CLI wires :func:`urllib_fetch`, tests wire fakes.  The real fetch fails on the
first transport error, rejects redirects outright, and enforces an explicit
timeout.  Non-2xx responses are returned so the caller can persist the exact
bytes before failing.
"""

from __future__ import annotations

from typing import Callable, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from urllib.parse import urlsplit

from .endpoints import HOST
from .errors import TransportError, TransportPolicyError


Fetch: TypeAlias = Callable[[str], "tuple[int, bytes]"]

ALLOWED_HOSTS = frozenset({HOST})
DEFAULT_TIMEOUT_SECONDS = 30.0


def validate_url(url: str) -> None:
    """Reject anything but a clean HTTPS URL on the allowlisted host."""
    split = urlsplit(url)
    if split.scheme != "https":
        raise TransportPolicyError("only HTTPS URLs are permitted")
    if split.hostname not in ALLOWED_HOSTS:
        raise TransportPolicyError(f"host is not allowlisted: {split.hostname!r}")
    if split.port is not None or split.username or split.password:
        raise TransportPolicyError("ports and URL credentials are forbidden")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise TransportPolicyError(f"redirect rejected (status={code})")


def urllib_fetch(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[int, bytes]:
    """Fetch ``url`` once with urllib; no retries, no redirects."""
    if timeout_seconds <= 0:
        raise TransportPolicyError("timeout must be positive")
    validate_url(url)
    opener = build_opener(_NoRedirect())
    request = Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read()
    except HTTPError as error:
        return int(error.code), error.read()
    except URLError as error:
        raise TransportError("TuringTrader request failed") from error

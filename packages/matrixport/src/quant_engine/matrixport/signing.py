"""Matrixport Authentication V2 query encoding and signing."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Any

from requests import PreparedRequest

from .credentials import ApiCredentials


QueryParams = Mapping[str, Any] | Sequence[tuple[str, Any]]


def encode_query(params: QueryParams | None) -> str:
    """Return the exact query encoding produced by ``requests``.

    The transport appends this returned string directly to the URL, so the
    bytes signed here cannot diverge from the bytes placed on the wire.
    """

    prepared = PreparedRequest()
    prepared.prepare_url("https://mapi.matrixport.com/", params or {})
    url = prepared.url or ""
    return url.partition("?")[2]


def prehash_v2(timestamp_ms: int, method: str, path: str, encoded_query: str) -> str:
    if not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise ValueError("timestamp_ms must be a non-negative integer")
    normalized_method = method.upper()
    if normalized_method != "GET":
        raise ValueError("Only GET signing is supported by the read-only client")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("path must be an absolute path without query or fragment")
    return f"{timestamp_ms}{normalized_method}{path}&{encoded_query}"


def sign_v2(
    signing_material: str,
    timestamp_ms: int,
    method: str,
    path: str,
    encoded_query: str,
) -> str:
    """Return the lowercase hexadecimal V2 HMAC-SHA256 signature."""

    message = prehash_v2(timestamp_ms, method, path, encoded_query)
    return hmac.new(
        signing_material.encode("utf-8"),
        message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def build_v2_headers(
    credentials: ApiCredentials,
    timestamp_ms: int,
    path: str,
    encoded_query: str,
    *,
    request_id: str,
) -> dict[str, str]:
    """Build private request headers without retaining a printable envelope."""

    signature = sign_v2(
        credentials.signing_material,
        timestamp_ms,
        "GET",
        path,
        encoded_query,
    )
    access_header = "X-MatrixPort-" + "Access-Key"
    return {
        "Accept": "application/json",
        access_header: credentials.key_identifier,
        "X-Signature": signature,
        "X-Timestamp": str(timestamp_ms),
        "X-Auth-Version": "v2",
        "X-Request-ID": request_id,
    }

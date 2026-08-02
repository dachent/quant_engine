"""Reviewed HAR-only Testfolio contracts and capture routing.

Testfolio is a custom React SPA with a small public REST API at /api/*.
The interactive tools (backtester, analyzer, optimizer, etc.) compute
client-side — only pre-configured catalog entries have API endpoints.

All contracted endpoints are public (no authentication required).
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)
from quant_engine.web_contracts.extract import (
    ExtractedResponse,
    SessionEnvelope,
    extract_har,
)

_ORIGIN = "https://testfol.io"
_SLUG_PATH = r"[a-z0-9-]+"


# ── endpoint helpers ──────────────────────────────────────────────


def _api_endpoint(
    capability: str,
    path: str,
    *,
    query_keys: frozenset[str] = frozenset(),
    validate_query: Any = None,
) -> EndpointContract:
    """Public read-only API endpoint on testfol.io."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=path,
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,  # all public
    )


def _api_endpoint_with_path_param(
    capability: str,
    path_template: str,
    path_pattern: str,
    *,
    query_keys: frozenset[str] = frozenset(),
    validate_query: Any = None,
) -> EndpointContract:
    """Public API endpoint with a path parameter (research slug)."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=path_template,
        path_pattern=path_pattern,
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


# ── provider contract ─────────────────────────────────────────────


TESTFOLIO_CONTRACT = ProviderContract(
    provider="testfolio",
    endpoints=(
        # ── limits ────────────────────────────────────────────────
        _api_endpoint(
            "limits.read",
            "/api/limits",
        ),
        # ── public research ───────────────────────────────────────
        _api_endpoint_with_path_param(
            "research.run",
            "/api/public-research/{slug}/run",
            rf"/api/public-research/{_SLUG_PATH}/run",
        ),
    ),
    sensitive_fields=frozenset(
        {
            "user_id",
        }
    ),
)


# ── extraction ────────────────────────────────────────────────────


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_testfolio_har(
    document: Mapping[str, Any],
    *,
    session: SessionEnvelope,
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed Testfolio API routes while disposing of noise.

    Filters out:
    - Static assets (/_next/static/*)
    - Third-party domains (Cloudflare Insights, Google Analytics, etc.)
    - Non-API paths (HTML pages, catalog, tools)
    - OPTIONS preflight
    - Responses without body text
    - Non-JSON responses
    """

    log = _object(document.get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")

    selected: list[object] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        response = _object(entry.get("response"), "response")
        method = request.get("method")
        url = request.get("url")

        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")

        parsed = urlsplit(url)

        # Only testfol.io
        if parsed.netloc != "testfol.io":
            continue

        # Only API routes
        if not parsed.path.startswith("/api/"):
            continue

        # Skip OPTIONS preflight
        if method.upper() == "OPTIONS":
            continue

        # Skip empty responses
        content = response.get("content")
        if not isinstance(content, dict) or not content.get("text"):
            continue

        # Skip non-JSON responses (HTML fallback pages)
        mime_type = content.get("mimeType", "")
        text = content.get("text", "")
        if mime_type and "json" not in mime_type:
            if text and not text.strip().startswith(("{", "[")):
                continue

        selected.append(entry)

    if not selected:
        raise WebContractError("capture contains no reviewed Testfolio API routes")

    return extract_har(
        {"log": {"entries": selected}},
        provider=TESTFOLIO_CONTRACT,
        session=session,
    )

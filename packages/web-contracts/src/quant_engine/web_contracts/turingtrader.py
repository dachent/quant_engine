"""Reviewed TuringTrader web contracts proven by Firebase Storage HAR capture.

TuringTrader is a server-rendered WordPress site with a Firebase Storage data
backend. Strategy data files (JSON/TXT) are served from Firebase Storage via
public download tokens. The Outseta platform handles membership and user
profile management.

The contract accepts only read-only ``alt=media`` content downloads from
Firebase Storage and the Outseta profile read endpoint. Firebase metadata
requests (without ``alt=media``), image assets, analytics, WordPress static
assets, preflight ``OPTIONS``, and the Outseta profile write (``PUT``) are
explicitly excluded from data extraction.
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


_FB_ORIGIN = "https://firebasestorage.googleapis.com"
_OUTSETA_ORIGIN = "https://turingtrader.outseta.com"

# Strategy slugs are lowercase alphanumeric with hyphens.
# Firebase Storage encodes the slug/document separator as %2F in the URL path.
_SLUG = r"[a-z0-9][a-z0-9-]*"
_SEP = r"%2F"  # URL-encoded forward slash in Firebase Storage object paths


def _validate_alt_media_query(value: Mapping[str, Any]) -> None:
    """Ensure the Firebase query carries the reviewed alt=media shape."""

    if value.get("alt") != "media" or not str(value.get("token", "")).strip():
        raise WebContractError(
            "firebase query must request alt=media with a download token"
        )


def _firebase_endpoint(
    capability: str,
    document: str,
    *,
    is_json: bool = True,
) -> EndpointContract:
    """Declare a Firebase Storage ``alt=media`` content endpoint."""

    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_FB_ORIGIN,
        path=f"/v0/b/turingtrader-com.appspot.com/o/{{slug}}{_SEP}{document}",
        path_pattern=rf"/v0/b/turingtrader-com\.appspot\.com/o/{_SLUG}{_SEP}{re.escape(document)}",
        query_keys=frozenset({"alt", "token"}),
        validate_query=_validate_alt_media_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


def _firebase_catalog_endpoint(
    capability: str,
    document: str,
) -> EndpointContract:
    """Declare a top-level Firebase Storage catalog endpoint (no slug)."""

    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_FB_ORIGIN,
        path=f"/v0/b/turingtrader-com.appspot.com/o/{document}",
        path_pattern=rf"/v0/b/turingtrader-com\.appspot\.com/o/{re.escape(document)}",
        query_keys=frozenset({"alt", "token"}),
        validate_query=_validate_alt_media_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


def _outseta_profile_query(value: Mapping[str, Any]) -> None:
    if value.get("fields") != "*":
        raise WebContractError("outseta profile query must request all fields")


TURINGTRADER_CONTRACT = ProviderContract(
    provider="turingtrader",
    endpoints=(
        # Strategy-level data endpoints (per slug)
        _firebase_endpoint("strategy.performance-chart", "performance-chart.json"),
        _firebase_endpoint("strategy.performance-table", "performance-table.json"),
        _firebase_endpoint("strategy.holdings-history", "holdings-history.json"),
        _firebase_endpoint("strategy.portfolio-holdings", "portfolio-holdings.json"),
        _firebase_endpoint("strategy.last-rebalance", "last-rebalance.txt"),
        _firebase_endpoint("strategy.last-update", "last-update.txt"),
        # Market Vane status (special strategy with status endpoint)
        _firebase_endpoint("strategy.market-vane-status", "status.json"),
        # Catalog endpoint (no slug)
        _firebase_catalog_endpoint("strategies.catalog", "portfolio-comp.json"),
        # Outseta user profile (read-only)
        EndpointContract(
            capability="account.profile",
            method="GET",
            origin=_OUTSETA_ORIGIN,
            path="/api/v1/profile",
            query_keys=frozenset({"fields"}),
            validate_query=_outseta_profile_query,
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header="cookie",
        ),
    ),
    sensitive_fields=frozenset(
        {
            "email",
            "firstname",
            "lastname",
            "phonemobile",
            "phonework",
            "mailingaddress",
            "ipaddress",
            "referer",
            "profileimages3url",
        }
    ),
    api_fallback=False,
)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_turingtrader_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed TuringTrader data routes while disposing noise.

    Firebase Storage metadata requests (without ``alt=media``), image assets,
    analytics, WordPress static assets, preflight ``OPTIONS``, the Outseta
    profile write (``PUT``), and Outseta config requests are explicitly
    excluded from data extraction.
    """

    log = _object(document.get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")
    selected: list[object] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        method = request.get("method")
        url = request.get("url")
        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Firebase Storage: keep only alt=media content downloads (JSON/TXT)
        if origin == _FB_ORIGIN and parsed.path.startswith(
            "/v0/b/turingtrader-com.appspot.com/o/"
        ):
            if method.upper() != "GET":
                raise WebContractError(
                    "unexpected method on Firebase Storage endpoint"
                )
            # Skip metadata requests (no alt=media query param)
            if "alt=media" not in parsed.query:
                continue
            # Skip image assets (.jpg)
            if parsed.path.endswith(".jpg"):
                continue
            # Verify the entry routes through the contract
            TURINGTRADER_CONTRACT.route(method, url)
            response = _object(entry.get("response"), "response")
            content = _object(response.get("content"), "response.content")
            if content.get("text"):
                selected.append(entry)

        # Outseta: keep only GET profile (not PUT, not OPTIONS, not config)
        elif origin == _OUTSETA_ORIGIN:
            if method.upper() == "OPTIONS":
                continue
            if method.upper() == "PUT":
                continue
            if method.upper() == "GET" and parsed.path == "/api/v1/profile":
                TURINGTRADER_CONTRACT.route(method, url)
                response = _object(entry.get("response"), "response")
                content = _object(response.get("content"), "response.content")
                if content.get("text"):
                    selected.append(entry)

        # All other origins: skip (analytics, fonts, WordPress assets, etc.)

    if not selected:
        raise WebContractError(
            "capture contains no reviewed TuringTrader data routes"
        )
    return extract_har(
        {"log": {"entries": selected}},
        provider=TURINGTRADER_CONTRACT,
        session=session,
    )

"""Reviewed HAR-only Grizzly Bulls contracts and capture routing."""

from __future__ import annotations

from collections.abc import Mapping
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

_ORIGIN = "https://grizzlybulls.com"
_BUILD_PATH = r"[^/]+"
_SLUG = r"[a-z0-9-]+"


def _ssg_static(capability: str, document: str) -> EndpointContract:
    """Static SSG page at /{document}.json (no dynamic segment)."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}\.json",
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


def _ssg_slug(capability: str, document: str) -> EndpointContract:
    """Dynamic SSG page at /{document}/{slug}.json."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}/{{slug}}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}/{_SLUG}\.json",
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


def _ssg_category(capability: str, document: str) -> EndpointContract:
    """Dynamic SSG page at /{document}/category/{category}.json."""
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=f"/_next/data/{{build_id}}/{document}/category/{{category}}.json",
        path_pattern=rf"/_next/data/{_BUILD_PATH}/{document}/category/{_SLUG}\.json",
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=None,
    )


GRIZZLYBULLS_CONTRACT = ProviderContract(
    provider="grizzlybulls",
    endpoints=(
        # Homepage — summary data for all 7 models
        _ssg_static("models.home", "index"),
        # Model index page
        _ssg_static("models.index", "models"),
        # Individual model detail — trade history, summary, description
        _ssg_slug("models.detail", "models"),
        # Indicator detail — metadata + historical series + chart data
        _ssg_slug("indicators.detail", "indicators"),
        # Premium/plans page
        _ssg_static("premium.plans", "premium"),
        # Blog index
        _ssg_static("blog.index", "blog"),
        # Individual blog post
        _ssg_slug("blog.detail", "blog"),
        # Blog category listing
        _ssg_category("blog.category", "blog"),
        # Billionaires index — 3361 entries
        _ssg_static("billionaires.index", "billionaires"),
        # Individual billionaire profile
        _ssg_slug("billionaires.detail", "billionaires"),
        # Financial terms / dictionary — categories index
        _ssg_static("dictionary.index", "financial-terms"),
        # Individual financial term definition
        _ssg_slug("dictionary.term", "financial-terms"),
        # Financial terms by category
        _ssg_category("dictionary.category", "financial-terms"),
        # ELI5 term definition
        _ssg_slug("dictionary.eli5-term", "financial-terms/eli5"),
        # Rankings detail page
        _ssg_slug("rankings.detail", "rankings"),
        # Calculator SSG (static shell)
        _ssg_static("calculator.page", "calculator"),
        # Auth page SSG (static shell)
        _ssg_static("auth.page", "auth"),
        # Age-Adjusted Net Worth Calculator
        _ssg_static("calculator.age-adjusted", "age-adjusted-net-worth-calculator"),
    ),
    sensitive_fields=frozenset(
        {
            "apikey",
            "avatarurl",
            "email",
            "generateapikey",
            "photonumber",
            "smsnumber",
            "stripe_customer_id",
            "userid",
            "username",
            "uuid",
        }
    ),
    api_fallback=False,
)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_grizzlybulls_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed data routes while disposing of static assets and noise."""

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
        if parsed.scheme != "https" or parsed.netloc != "grizzlybulls.com":
            continue
        # Only consider SSG data routes and reviewed API routes
        is_ssg = parsed.path.startswith("/_next/data/")
        is_api = parsed.path.startswith("/api/")
        if not (is_ssg or is_api):
            continue
        if method.upper() != "GET":
            continue
        GRIZZLYBULLS_CONTRACT.route(method, url)
        response = _object(entry.get("response"), "response")
        content = _object(response.get("content"), "response.content")
        if content.get("text"):
            selected.append(entry)
    if not selected:
        raise WebContractError("capture contains no reviewed Grizzly Bulls data routes")
    return extract_har(
        {"log": {"entries": selected}},
        provider=GRIZZLYBULLS_CONTRACT,
        session=session,
    )

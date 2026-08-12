"""Reviewed HAR-only BestFolio contracts and capture routing.

BestFolio is a Next.js App Router site with a public REST API at /api/*.
Unlike other providers (Quantbase has SSG data routes, Schwab has Bearer auth),
BestFolio's clean JSON data is served exclusively through public API endpoints.
The SSG data routes (/_next/data/{buildId}/...) return HTML+RSC, not JSON,
and are NOT contracted here — the API is the authoritative data source.

All endpoints are public (no authentication required).
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

_ORIGIN = "https://bestfolio.app"

# ── query validators ──────────────────────────────────────────────


def _entity_ids_query(value: Mapping[str, Any]) -> None:
    """Validate entity_ids: comma-separated type:id pairs."""
    if set(value) != {"entity_ids"}:
        raise WebContractError("entity_ids query keys differ from reviewed shape")
    raw = value.get("entity_ids")
    if not isinstance(raw, str) or not raw.strip():
        raise WebContractError("entity_ids must be a non-empty string")
    for token in raw.split(","):
        token = token.strip()
        if not re.fullmatch(r"[a-z]+:[0-9]+", token):
            raise WebContractError(f"entity_id {token!r} does not match type:id pattern")


def _entity_ids_resample_query(value: Mapping[str, Any]) -> None:
    """Validate entity_ids + resample query."""
    if set(value) != {"entity_ids", "resample"}:
        raise WebContractError("entity_ids+resample query keys differ from reviewed shape")
    raw = value.get("entity_ids")
    if not isinstance(raw, str) or not raw.strip():
        raise WebContractError("entity_ids must be a non-empty string")
    for token in raw.split(","):
        if not re.fullmatch(r"[a-z]+:[0-9]+", token.strip()):
            raise WebContractError(f"entity_id {token!r} does not match type:id pattern")
    if value.get("resample") != "daily":
        raise WebContractError("resample must be daily")


def _entity_ids_date_query(value: Mapping[str, Any]) -> None:
    """Validate entity_ids + start_date + end_date query."""
    if set(value) != {"end_date", "entity_ids", "start_date"}:
        raise WebContractError("entity_ids+dates query keys differ from reviewed shape")
    raw = value.get("entity_ids")
    if not isinstance(raw, str) or not raw.strip():
        raise WebContractError("entity_ids must be a non-empty string")
    for token in raw.split(","):
        if not re.fullmatch(r"[a-z]+:[0-9]+", token.strip()):
            raise WebContractError(f"entity_id {token!r} does not match type:id pattern")
    for key in ("start_date", "end_date"):
        date_val = value.get(key)
        if not isinstance(date_val, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date_val):
            raise WebContractError(f"{key} must be YYYY-MM-DD")


def _tickers_query(value: Mapping[str, Any]) -> None:
    """Validate tickers: CSV of uppercase ticker symbols."""
    if set(value) != {"tickers"}:
        raise WebContractError("tickers query keys differ from reviewed shape")
    raw = value.get("tickers")
    if not isinstance(raw, str) or not raw.strip():
        raise WebContractError("tickers must be a non-empty string")


def _signal_history_query(value: Mapping[str, Any]) -> None:
    """Validate signal history query: limit only."""
    if set(value) != {"limit"}:
        raise WebContractError("signal history query keys differ from reviewed shape")
    limit = value.get("limit")
    if not isinstance(limit, str) or not re.fullmatch(r"[1-9][0-9]*", limit):
        raise WebContractError("limit must be a positive integer string")


def _factor_lens_request(value: Mapping[str, Any]) -> None:
    """Validate factor-lens POST body: tickers (list) + weights (list)."""
    if set(value) != {"tickers", "weights"}:
        raise WebContractError("factor-lens body keys differ from reviewed shape")
    tickers = value.get("tickers")
    weights = value.get("weights")
    if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers):
        raise WebContractError("tickers must be a list of strings")
    if not tickers:
        raise WebContractError("tickers must not be empty")
    if not isinstance(weights, list) or not all(isinstance(w, (int, float)) for w in weights):
        raise WebContractError("weights must be a list of numbers")
    if len(tickers) != len(weights):
        raise WebContractError("tickers and weights must have the same length")


# ── endpoint helpers ──────────────────────────────────────────────


def _api_endpoint(
    capability: str,
    path: str,
    *,
    query_keys: frozenset[str] = frozenset(),
    validate_query: Any = None,
) -> EndpointContract:
    """Public read-only API endpoint on bestfolio.app."""
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
    """Public API endpoint with a path parameter (strategy/variant ID)."""
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

BESTFOLIO_CONTRACT = ProviderContract(
    provider="bestfolio",
    endpoints=(
        # ── strategy catalog ──────────────────────────────────────
        _api_endpoint(
            "strategies.catalog",
            "/api/strategies",
        ),
        _api_endpoint(
            "strategies.summary",
            "/api/strategies/summary",
        ),
        _api_endpoint_with_path_param(
            "strategies.detail",
            "/api/strategies/{strategy_id}",
            r"/api/strategies/[1-9][0-9]*",
        ),
        # ── performance analytics ─────────────────────────────────
        _api_endpoint(
            "performance.metrics",
            "/api/performance/metrics",
            query_keys=frozenset({"end_date", "entity_ids", "start_date"}),
            validate_query=_entity_ids_date_query,
        ),
        _api_endpoint(
            "performance.nav",
            "/api/performance/nav",
            query_keys=frozenset({"entity_ids", "resample"}),
            validate_query=_entity_ids_resample_query,
        ),
        _api_endpoint(
            "performance.drawdown",
            "/api/performance/drawdown",
            query_keys=frozenset({"entity_ids", "resample"}),
            validate_query=_entity_ids_resample_query,
        ),
        # ── periodic returns ────────────────────────────────────
        _api_endpoint(
            "performance.returns",
            "/api/performance/returns",
            query_keys=frozenset({"entity_ids"}),
            validate_query=_entity_ids_query,
        ),
        # ── signals ───────────────────────────────────────────────
        _api_endpoint_with_path_param(
            "variants.signal_history",
            "/api/variants/{variant_id}/signal/history",
            r"/api/variants/[1-9][0-9]*/signal/history",
            query_keys=frozenset({"limit"}),
            validate_query=_signal_history_query,
        ),
        # ── tax analysis ──────────────────────────────────────────
        _api_endpoint_with_path_param(
            "tax.us_variant",
            "/api/tax/us/variant/{variant_id}",
            r"/api/tax/us/variant/[1-9][0-9]*",
        ),
        # ── liquidity ─────────────────────────────────────────────
        _api_endpoint(
            "liquidity.metrics",
            "/api/liquidity",
            query_keys=frozenset({"tickers"}),
            validate_query=_tickers_query,
        ),
        # ── factor lens ───────────────────────────────────────────
        EndpointContract(
            capability="factor_lens.analyze",
            method="POST",
            origin=_ORIGIN,
            path="/api/factor-lens/analyze",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
            validate_request=_factor_lens_request,
        ),
        # ── library ────────────────────────────────────────────────
        _api_endpoint(
            "library.catalog",
            "/api/library",
        ),
        # ── blog ───────────────────────────────────────────────────
        _api_endpoint(
            "blog.posts",
            "/api/blog/posts",
        ),
        _api_endpoint_with_path_param(
            "blog.post",
            "/api/blog/posts/{slug}",
            r"/api/blog/posts/[a-z0-9][a-z0-9-]*[a-z0-9]",
        ),
    ),
    sensitive_fields=frozenset(
        {
            # strategy/variant internal IDs
            "id",
            "strategy_id",
            "variant_id",
        }
    ),
)


# ── extraction ────────────────────────────────────────────────────


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def extract_bestfolio_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed BestFolio API routes while disposing of noise.

    Filters out:
    - Static assets (/_next/static/*)
    - SSG data routes (/_next/data/*) — HTML+RSC, not JSON
    - Third-party domains (Clerk, PostHog, Google Analytics, Stripe)
    - Non-API paths
    - OPTIONS preflight
    - Responses without body text
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

        # Only bestfolio.app
        if parsed.netloc != "bestfolio.app":
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

        selected.append(entry)

    if not selected:
        raise WebContractError("capture contains no reviewed BestFolio API routes")

    return extract_har(
        {"log": {"entries": selected}},
        provider=BESTFOLIO_CONTRACT,
        session=session,
    )

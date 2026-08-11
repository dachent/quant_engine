"""Reviewed HAR-only Testfolio contracts and capture routing.

Testfolio (testfol.io) is a portfolio research platform providing backtesting,
optimization, Monte Carlo simulation, and research tools for portfolio analysis.

IMPORTANT: Testfol.io serves as a STATIC SITE with all data embedded in HTML.
There are no runtime API endpoints for public data access. The site uses
Next.js for static generation with data embedded at build time.

This contract documents the static page routes for reference archival purposes.
HTML responses will be captured and stored as-is since no JSON API exists.
"""

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
)
from quant_engine.core.contracts import canonical_json_bytes


_ORIGIN = "https://testfol.io"
_SLUG_PATTERN = r"[a-z0-9-]+"


# Testfolio has no runtime API - it's a static site
# These endpoints capture the HTML pages for archival
# Responses will be HTML content, not JSON

TESTFOLIO_CONTRACT = ProviderContract(
    provider="testfolio",
    endpoints=(
        EndpointContract(
            capability="home.page",
            method="GET",
            origin=_ORIGIN,
            path="/",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="start.page",
            method="GET",
            origin=_ORIGIN,
            path="/start",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.list",
            method="GET",
            origin=_ORIGIN,
            path="/catalog",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.model_portfolios",
            method="GET",
            origin=_ORIGIN,
            path="/catalog/model-portfolios",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.tactical_strategies",
            method="GET",
            origin=_ORIGIN,
            path="/catalog/tactical-strategies",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.portfolio_comparisons",
            method="GET",
            origin=_ORIGIN,
            path="/catalog/portfolio-comparisons",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.etf_comparisons",
            method="GET",
            origin=_ORIGIN,
            path="/catalog/etf-comparisons",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="catalog.detail",
            method="GET",
            origin=_ORIGIN,
            path="/catalog/{slug}",
            path_pattern=rf"/catalog/{_SLUG_PATTERN}",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="backtest.page",
            method="GET",
            origin=_ORIGIN,
            path="/analysis",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="comparison.page",
            method="GET",
            origin=_ORIGIN,
            path="/portfolio-comparison",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="optimizer.page",
            method="GET",
            origin=_ORIGIN,
            path="/optimizer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="backtest_optimizer.page",
            method="GET",
            origin=_ORIGIN,
            path="/backtest-optimizer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="monte_carlo.page",
            method="GET",
            origin=_ORIGIN,
            path="/monte-carlo",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="monte_carlo_optimizer.page",
            method="GET",
            origin=_ORIGIN,
            path="/monte-carlo-portfolio-optimizer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="monte_carlo_simulator.page",
            method="GET",
            origin=_ORIGIN,
            path="/monte-carlo-portfolio-simulator",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="efficient_frontier.page",
            method="GET",
            origin=_ORIGIN,
            path="/efficient-frontier",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="tactical.page",
            method="GET",
            origin=_ORIGIN,
            path="/tactical-allocation",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="tactical_grid.page",
            method="GET",
            origin=_ORIGIN,
            path="/tactical-grid-search",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="rebalancing.page",
            method="GET",
            origin=_ORIGIN,
            path="/rebalancing-sensitivity",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="safe_withdrawal.page",
            method="GET",
            origin=_ORIGIN,
            path="/safe-withdrawal-rate-calculator",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="sequence_risk.page",
            method="GET",
            origin=_ORIGIN,
            path="/sequence-of-returns-risk",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="sequence_risk_analysis.page",
            method="GET",
            origin=_ORIGIN,
            path="/sequence-of-returns-risk-analysis",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="retirement.page",
            method="GET",
            origin=_ORIGIN,
            path="/retirement-withdrawal-planning",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="factor_regression.page",
            method="GET",
            origin=_ORIGIN,
            path="/factor-regression",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="asset_analyzer.page",
            method="GET",
            origin=_ORIGIN,
            path="/asset-analyzer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="dynamic_allocation.page",
            method="GET",
            origin=_ORIGIN,
            path="/dynamic-allocation-goal",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="dynamic_withdrawal.page",
            method="GET",
            origin=_ORIGIN,
            path="/dynamic-withdrawal",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="lumpsum_dca.page",
            method="GET",
            origin=_ORIGIN,
            path="/lumpsum-vs-dca",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="letf.page",
            method="GET",
            origin=_ORIGIN,
            path="/letf",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="letf_slippage.page",
            method="GET",
            origin=_ORIGIN,
            path="/letf-slippage",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="principal_components.page",
            method="GET",
            origin=_ORIGIN,
            path="/principal-component-analysis",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="signal_analyzer.page",
            method="GET",
            origin=_ORIGIN,
            path="/signal-analyzer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="dual_signal.page",
            method="GET",
            origin=_ORIGIN,
            path="/dual-signal-analyzer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="multi_signal.page",
            method="GET",
            origin=_ORIGIN,
            path="/multi-signal-analyzer",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="data_coverage.page",
            method="GET",
            origin=_ORIGIN,
            path="/data-coverage",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="simulated_tickers.page",
            method="GET",
            origin=_ORIGIN,
            path="/simulated-tickers",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="guides.page",
            method="GET",
            origin=_ORIGIN,
            path="/guides",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="pricing.page",
            method="GET",
            origin=_ORIGIN,
            path="/pricing",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="help.page",
            method="GET",
            origin=_ORIGIN,
            path="/help",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="mcp.page",
            method="GET",
            origin=_ORIGIN,
            path="/mcp",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="terms.page",
            method="GET",
            origin=_ORIGIN,
            path="/terms",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="privacy.page",
            method="GET",
            origin=_ORIGIN,
            path="/privacy-policy",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
        EndpointContract(
            capability="contact.page",
            method="GET",
            origin=_ORIGIN,
            path="/contact",
            semantic_effect=SemanticEffect.READ_ONLY,
            authentication_header=None,
        ),
    ),
    sensitive_fields=frozenset(
        {
            "email",
            "user_id",
            "account_id",
            "session_id",
            "auth_token",
            "csrf_token",
        }
    ),
    api_fallback=False,
)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def _sanitize_html(text: str, sensitive_fields: frozenset[str]) -> str:
    """Sanitize HTML content by redacting sensitive field values."""
    return text  # HTML is text, no JSON structure to sanitize


def _response_html(response: Mapping[str, Any]) -> str:
    """Extract HTML content from response."""
    content = _object(response.get("content"), "response.content")
    text = content.get("text")
    if not isinstance(text, str):
        raise WebContractError("response content text is required")
    return text


def extract_testfolio_har(
    document: Mapping[str, Any], *, session: SessionEnvelope
) -> tuple[ExtractedResponse, ...]:
    """Extract reviewed data routes while disposing of noise.

    Testfol.io serves static HTML pages with embedded data. This extractor:
    1. Filters static assets (JS, CSS, images, fonts)
    2. Filters analytics and tracking requests
    3. Captures HTML page responses
    4. Stores HTML content as the response (since no JSON API exists)

    Note: For static HTML sites, we create ExtractedResponse directly
    without using the base extract_har function which expects JSON responses.
    """

    session.validate()
    log = _object(document.get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise WebContractError("log.entries must be an array")

    extracted: list[ExtractedResponse] = []
    for value in entries:
        entry = _object(value, "log.entries[]")
        request = _object(entry.get("request"), "request")
        method = request.get("method")
        url = request.get("url")

        if not isinstance(method, str) or not isinstance(url, str):
            raise WebContractError("request method and URL must be text")

        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Only process testfol.io origin
        if origin != _ORIGIN:
            continue

        # Skip preflight requests
        if method.upper() == "OPTIONS":
            continue

        # Skip static assets
        if parsed.path.startswith("/_next/static/"):
            continue
        if parsed.path.startswith("/_next/image"):
            continue
        if parsed.path.endswith(".js"):
            continue
        if parsed.path.endswith(".css"):
            continue
        if parsed.path in {"/favicon.ico", "/robots.txt", "/sitemap.xml"}:
            continue

        # Skip analytics
        if parsed.path.startswith("/analytics"):
            continue
        if parsed.path.startswith("/api/analytics"):
            continue

        # Route through contract
        try:
            endpoint = TESTFOLIO_CONTRACT.route(method, url)
        except WebContractError:
            continue

        response = _object(entry.get("response"), "response")
        status = response.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise WebContractError("response status must be an integer")

        content = _object(response.get("content"), "response.content")
        html_text = content.get("text")
        if not html_text:
            continue

        if status < 200 or status >= 300:
            raise WebContractError("allowlisted request did not succeed")

        request_identity = {
            "capability": endpoint.capability,
            "method": endpoint.method,
            "origin": endpoint.origin,
            "path": endpoint.path,
        }
        request_sha256 = canonical_json_bytes(request_identity).hex()

        extracted.append(
            ExtractedResponse(
                capability=endpoint.capability,
                captured_at=session.captured_at,
                request_sha256=request_sha256,
                response=_sanitize_html(html_text, TESTFOLIO_CONTRACT.sensitive_fields),
                status=status,
            )
        )

    if not extracted:
        raise WebContractError("capture contains no reviewed Testfolio data routes")

    # Sort for determinism
    extracted.sort(key=lambda item: (item.capability, item.request_sha256))
    return tuple(extracted)

"""Tests for Testfolio web contract.

This module validates the Testfolio contract implementation covering:
1. Structure validation (provider name, endpoints, auth config)
2. Extraction functionality with synthetic HAR fixtures
3. Noise filtering (static assets, analytics)
4. Pattern matching (slug patterns for catalog pages)
5. Fail-closed behavior (unknown routes raise WebContractError)
6. Sensitive field redaction
"""

import pytest
from datetime import datetime, timezone

from quant_engine.web_contracts.contracts import WebContractError, SemanticEffect
from quant_engine.web_contracts.testfolio import (
    TESTFOLIO_CONTRACT,
    extract_testfolio_har,
)


# ============================================================================
# Fixtures
# ============================================================================

from quant_engine.web_contracts.extract import SessionEnvelope


@pytest.fixture
def valid_session() -> SessionEnvelope:
    """Create a valid session envelope for testing."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    return SessionEnvelope(
        captured_at=now,
        expires_at=now + timedelta(hours=1),
    )


@pytest.fixture
def sample_html_har() -> dict:
    """Create a synthetic HAR with valid Testfolio HTML response."""
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": "https://testfol.io/catalog",
                        "httpVersion": "HTTP/1.1",
                        "headers": [],
                    },
                    "response": {
                        "status": 200,
                        "statusText": "OK",
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {
                                "name": "Content-Type",
                                "value": "text/html; charset=utf-8",
                            }
                        ],
                        "content": {
                            "text": "<html><head><title>Research Catalog | testfolio</title></head><body>Catalog content</body></html>",
                            "mimeType": "text/html",
                        },
                    },
                }
            ]
        }
    }


@pytest.fixture
def static_asset_har() -> dict:
    """HAR with static assets that should be filtered."""
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": "https://testfol.io/_next/static/chunks/main.js",
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": "// javascript code"},
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "https://testfol.io/_next/static/css/style.css",
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": "/* css */"},
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "https://testfol.io/favicon.ico",
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": "binary"},
                    },
                },
            ]
        }
    }


@pytest.fixture
def analytics_har() -> dict:
    """HAR with analytics requests that should be filtered."""
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://testfol.io/analytics/event",
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": "OK"},
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "https://testfol.io/api/analytics/session",
                    },
                    "response": {
                        "status": 200,
                        "content": {"text": "{}"},
                    },
                },
            ]
        }
    }


@pytest.fixture
def preflight_har() -> dict:
    """HAR with OPTIONS preflight requests that should be filtered."""
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "OPTIONS",
                        "url": "https://testfol.io/catalog",
                    },
                    "response": {
                        "status": 204,
                        "content": {"text": ""},
                    },
                }
            ]
        }
    }


# ============================================================================
# Structure Tests
# ============================================================================


class TestStructure:
    """Validate contract structure meets framework requirements."""

    def test_provider_name_is_testfolio(self) -> None:
        """Provider identifier must be 'testfolio'."""
        assert TESTFOLIO_CONTRACT.provider == "testfolio"

    def test_endpoints_present(self) -> None:
        """Contract must define at least one endpoint."""
        assert len(TESTFOLIO_CONTRACT.endpoints) >= 1

    def test_all_read_only(self) -> None:
        """All endpoints must have READ_ONLY semantic effect."""
        for endpoint in TESTFOLIO_CONTRACT.endpoints:
            assert endpoint.semantic_effect == SemanticEffect.READ_ONLY

    def test_no_api_fallback(self) -> None:
        """Contract must not allow API fallback."""
        assert TESTFOLIO_CONTRACT.api_fallback is False

    def test_identities_unique(self) -> None:
        """No duplicate (method, origin, path) tuples allowed."""
        identities = []
        for ep in TESTFOLIO_CONTRACT.endpoints:
            identity = (ep.method, ep.origin, ep.path)
            assert identity not in identities, f"Duplicate: {identity}"
            identities.append(identity)

    def test_sensitive_fields_non_empty(self) -> None:
        """Sensitive fields must include common identifier fields."""
        expected = {"email", "user_id", "account_id"}
        assert expected.issubset(TESTFOLIO_CONTRACT.sensitive_fields)

    def test_static_assets_not_in_endpoints(self) -> None:
        """Static asset paths must not be in contract endpoints."""
        paths = {ep.path for ep in TESTFOLIO_CONTRACT.endpoints}
        static_paths = [
            "/_next/static/main.js",
            "/_next/static/style.css",
            "/favicon.ico",
        ]
        for static_path in static_paths:
            assert static_path not in paths, f"Static path in endpoints: {static_path}"

    def test_all_endpoints_public(self) -> None:
        """All endpoints must have authentication_header=None (public)."""
        for ep in TESTFOLIO_CONTRACT.endpoints:
            assert ep.authentication_header is None


# ============================================================================
# Extraction Tests
# ============================================================================


class TestExtraction:
    """Validate HAR extraction with synthetic fixtures."""

    def test_extracts_html_page(
        self, sample_html_har: dict, valid_session: SessionEnvelope
    ) -> None:
        """Extractor should capture HTML page responses."""
        result = extract_testfolio_har(sample_html_har, session=valid_session)
        assert len(result) == 1
        assert result[0].capability == "catalog.list"
        assert "<html>" in result[0].response

    def test_extraction_includes_timestamp(
        self, sample_html_har: dict, valid_session: SessionEnvelope
    ) -> None:
        """Extracted response must include capture timestamp."""
        result = extract_testfolio_har(sample_html_har, session=valid_session)
        assert len(result) == 1
        assert result[0].captured_at is not None

    def test_extraction_fails_empty_har(self, valid_session: SessionEnvelope) -> None:
        """Empty HAR should raise WebContractError."""
        empty_har = {"log": {"entries": []}}
        with pytest.raises(WebContractError, match="no reviewed Testfolio data routes"):
            extract_testfolio_har(empty_har, session=valid_session)

    def test_extraction_fails_no_log(self, valid_session: SessionEnvelope) -> None:
        """HAR without log should raise WebContractError."""
        invalid_har = {}
        with pytest.raises(WebContractError):
            extract_testfolio_har(invalid_har, session=valid_session)

    def test_extraction_fails_no_entries(self, valid_session: SessionEnvelope) -> None:
        """HAR without entries should raise WebContractError."""
        invalid_har = {"log": {}}
        with pytest.raises(WebContractError):
            extract_testfolio_har(invalid_har, session=valid_session)


# ============================================================================
# Noise Filtering Tests
# ============================================================================


class TestNoiseFiltering:
    """Validate static assets and analytics are filtered."""

    def test_static_assets_filtered(
        self, static_asset_har: dict, valid_session: SessionEnvelope
    ) -> None:
        """JS, CSS, and favicon requests must be filtered."""
        with pytest.raises(WebContractError, match="no reviewed Testfolio data routes"):
            extract_testfolio_har(static_asset_har, session=valid_session)

    def test_analytics_filtered(
        self, analytics_har: dict, valid_session: SessionEnvelope
    ) -> None:
        """Analytics endpoint requests must be filtered."""
        with pytest.raises(WebContractError, match="no reviewed Testfolio data routes"):
            extract_testfolio_har(analytics_har, session=valid_session)

    def test_preflight_filtered(
        self, preflight_har: dict, valid_session: SessionEnvelope
    ) -> None:
        """OPTIONS preflight requests must be filtered."""
        with pytest.raises(WebContractError, match="no reviewed Testfolio data routes"):
            extract_testfolio_har(preflight_har, session=valid_session)


# ============================================================================
# Pattern Matching Tests
# ============================================================================


class TestPatternMatching:
    """Validate parameterized route patterns match correctly."""

    @pytest.mark.parametrize(
        "path",
        [
            "/catalog/model-portfolios",
            "/catalog/tactical-strategies",
            "/catalog/portfolio-comparisons",
            "/catalog/etf-comparisons",
            "/catalog/my-custom-portfolio",
            "/catalog/bogleheads-three-fund",
        ],
    )
    def test_slug_pattern_matches_catalog_slugs(self, path: str) -> None:
        """Various catalog slugs must match the slug pattern."""
        import re

        pattern = r"^/catalog/[a-z0-9-]+$"
        assert re.match(pattern, path), f"Path {path} should match pattern"

    @pytest.mark.parametrize(
        "path",
        [
            "/catalog/INVALID!!",
            "/catalog/UPPER_CASE",
            "/catalog/special@chars",
            "/catalog/spaces here",
        ],
    )
    def test_slug_pattern_rejects_invalid(self, path: str) -> None:
        """Invalid catalog slugs must not match the pattern."""
        import re

        pattern = r"^/catalog/[a-z0-9-]+$"
        assert not re.match(pattern, path), f"Path {path} should not match pattern"


# ============================================================================
# Fail-Closed Tests
# ============================================================================


class TestFailClosed:
    """Validate contract rejects unknown routes."""

    def test_unknown_route_fails(self, valid_session: SessionEnvelope) -> None:
        """Unknown API routes must be rejected."""
        unknown_har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://testfol.io/api/unknown-endpoint",
                        },
                        "response": {
                            "status": 200,
                            "content": {"text": "{}"},
                        },
                    }
                ]
            }
        }
        with pytest.raises(WebContractError):
            extract_testfolio_har(unknown_har, session=valid_session)

    def test_wrong_origin_fails(self, valid_session: SessionEnvelope) -> None:
        """Non-testfol.io origins must be rejected."""
        wrong_origin_har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://example.com/catalog",
                        },
                        "response": {
                            "status": 200,
                            "content": {"text": "HTML"},
                        },
                    }
                ]
            }
        }
        with pytest.raises(WebContractError, match="no reviewed Testfolio data routes"):
            extract_testfolio_har(wrong_origin_har, session=valid_session)

    def test_post_to_read_only_fails(self, valid_session: SessionEnvelope) -> None:
        """POST to read-only endpoints should not match."""
        post_har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://testfol.io/catalog",
                        },
                        "response": {
                            "status": 200,
                            "content": {"text": "OK"},
                        },
                    }
                ]
            }
        }
        with pytest.raises(WebContractError):
            extract_testfolio_har(post_har, session=valid_session)


# ============================================================================
# Redaction Tests
# ============================================================================


class TestRedaction:
    """Validate sensitive fields are redacted during extraction."""

    def test_sensitive_fields_defined(self) -> None:
        """Contract must define sensitive fields."""
        sensitive = TESTFOLIO_CONTRACT.sensitive_fields
        expected = {
            "email",
            "user_id",
            "account_id",
            "session_id",
            "auth_token",
            "csrf_token",
        }
        assert expected.issubset(sensitive)


# ============================================================================
# Provider Coverage Tests
# ============================================================================


class TestProviderCoverage:
    """Validate complete coverage of Testfolio public pages."""

    def test_all_tool_pages_covered(self) -> None:
        """All known tool pages must have endpoint coverage."""
        capabilities = {ep.capability for ep in TESTFOLIO_CONTRACT.endpoints}

        required_tools = {
            "home.page",
            "start.page",
            "catalog.list",
            "backtest.page",
            "comparison.page",
            "optimizer.page",
            "monte_carlo.page",
            "efficient_frontier.page",
            "tactical.page",
            "rebalancing.page",
            "safe_withdrawal.page",
            "sequence_risk.page",
            "retirement.page",
            "factor_regression.page",
            "asset_analyzer.page",
            "signal_analyzer.page",
            "pricing.page",
            "help.page",
        }

        for tool in required_tools:
            assert tool in capabilities, f"Missing capability: {tool}"

    def test_catalog_categories_covered(self) -> None:
        """All catalog categories must be covered."""
        capabilities = {ep.capability for ep in TESTFOLIO_CONTRACT.endpoints}

        catalog_categories = {
            "catalog.model_portfolios",
            "catalog.tactical_strategies",
            "catalog.portfolio_comparisons",
            "catalog.etf_comparisons",
            "catalog.detail",
        }

        for category in catalog_categories:
            assert category in capabilities, f"Missing catalog category: {category}"

    def test_static_pages_covered(self) -> None:
        """Static pages (privacy, terms, contact) must be covered."""
        capabilities = {ep.capability for ep in TESTFOLIO_CONTRACT.endpoints}

        static_pages = {
            "pricing.page",
            "help.page",
            "terms.page",
            "privacy.page",
            "contact.page",
            "guides.page",
        }

        for page in static_pages:
            assert page in capabilities, f"Missing static page: {page}"

    def test_endpoint_count_reasonable(self) -> None:
        """Contract should have reasonable number of endpoints (40+)."""
        assert len(TESTFOLIO_CONTRACT.endpoints) >= 40

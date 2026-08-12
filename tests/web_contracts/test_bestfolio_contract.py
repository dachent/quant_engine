"""Tests for the BestFolio web contract — public REST API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_engine.web_contracts import (
    BESTFOLIO_CONTRACT,
    REDACTION,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_bestfolio_har,
)

NOW = datetime(2026, 8, 2, 13, tzinfo=timezone.utc)
_ORIGIN = "https://bestfolio.app"


def _entry(
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    body: object | None = None,
) -> dict[str, object]:
    return {
        "request": {"headers": [], "method": method, "url": url},
        "response": {
            "content": {"text": json.dumps({} if body is None else body)},
            "status": status,
        },
    }


def _session(*, expired: bool = False) -> SessionEnvelope:
    return SessionEnvelope(
        captured_at=NOW,
        expires_at=NOW - timedelta(seconds=1)
        if expired
        else NOW + timedelta(minutes=30),
    )


def _document(*entries: object) -> dict[str, object]:
    return {"log": {"entries": list(entries)}}


# ── contract structure tests ──────────────────────────────────────


def test_contract_provider_name() -> None:
    assert BESTFOLIO_CONTRACT.provider == "bestfolio"


def test_contract_has_endpoints() -> None:
    assert len(BESTFOLIO_CONTRACT.endpoints) == 14


def test_contract_all_read_only() -> None:
    from quant_engine.web_contracts.contracts import SemanticEffect

    for ep in BESTFOLIO_CONTRACT.endpoints:
        assert ep.semantic_effect == SemanticEffect.READ_ONLY


def test_contract_no_api_fallback() -> None:
    assert BESTFOLIO_CONTRACT.api_fallback is False


def test_contract_has_sensitive_fields() -> None:
    assert len(BESTFOLIO_CONTRACT.sensitive_fields) > 0


def test_contract_endpoint_identities_unique() -> None:
    identities = [
        (ep.method, ep.origin, ep.path) for ep in BESTFOLIO_CONTRACT.endpoints
    ]
    assert len(identities) == len(set(identities))


def test_contract_no_auth_required() -> None:
    """All BestFolio endpoints are public — no authentication_header."""
    for ep in BESTFOLIO_CONTRACT.endpoints:
        assert ep.authentication_header is None


# ── extraction tests ──────────────────────────────────────────────


def test_public_api_extraction_is_deterministic() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/strategies",
            body=[
                {
                    "id": 78,
                    "name": "100% US Stock Market",
                    "slug": "us-market-benchmark",
                    "author": "Benchmark",
                    "variants": [{"id": 109, "name": "100% US Stock Market"}],
                }
            ],
        ),
        _entry(
            f"{_ORIGIN}/api/performance/metrics?entity_ids=variant:17&start_date=2020-01-01&end_date=2026-08-01",
            body=[
                {
                    "type": "variant",
                    "id": 17,
                    "name": "GEM",
                    "metrics": {"cagr": 0.1227, "sharpe_ratio": 0.89},
                }
            ],
        ),
    )
    first = extract_bestfolio_har(document, session=_session())
    second = extract_bestfolio_har(document, session=_session())
    assert canonical_extraction(first) == canonical_extraction(second)
    assert {item.capability for item in first} == {
        "strategies.catalog",
        "performance.metrics",
    }


def test_noise_is_filtered() -> None:
    """Static assets, SSG routes, third-party, and OPTIONS are discarded."""
    document = _document(
        # Static asset — should be filtered
        _entry(f"{_ORIGIN}/_next/static/chunks/app.js"),
        # SSG data route — HTML+RSC, not contracted
        _entry(f"{_ORIGIN}/_next/data/build123/strategies.json?slug=gem"),
        # Third-party — should be filtered
        _entry("https://js.clerk.com/npm/clerk.browser.js"),
        _entry("https://us.posthog.com/decide/"),
        # OPTIONS preflight
        _entry(f"{_ORIGIN}/api/strategies", method="OPTIONS"),
        # Empty response — should be filtered
        _entry(f"{_ORIGIN}/api/strategies", body=None),
        # Valid — should be extracted
        _entry(
            f"{_ORIGIN}/api/strategies",
            body=[{"id": 1, "name": "Test"}],
        ),
    )
    # Patch the empty response entry
    document["log"]["entries"][5]["response"]["content"]["text"] = ""  # type: ignore[index]

    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategies.catalog"


def test_duplicate_non_auth_headers_are_tolerated() -> None:
    """Duplicate Accept headers shouldn't fail (no auth headers to duplicate)."""
    entry = _entry(
        f"{_ORIGIN}/api/strategies",
        body=[{"id": 1, "name": "Test"}],
    )
    headers = entry["request"]["headers"]  # type: ignore[index]
    assert isinstance(headers, list)
    headers.extend(
        [
            {"name": "Accept", "value": "application/json"},
            {"name": "accept", "value": "*/*"},
        ]
    )
    document = _document(entry)
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1


def test_unknown_route_fails_closed() -> None:
    document = _document(
        _entry(f"{_ORIGIN}/api/unknown/endpoint", body={"value": 1}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())


def test_wrong_method_fails_closed() -> None:
    document = _document(
        _entry(f"{_ORIGIN}/api/strategies", method="POST", body={"value": 1}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())


def test_expired_session_fails() -> None:
    document = _document(
        _entry(f"{_ORIGIN}/api/strategies", body=[{"id": 1}]),
    )
    with pytest.raises(WebContractError, match="captured session is expired"):
        extract_bestfolio_har(document, session=_session(expired=True))


def test_non_200_status_fails() -> None:
    document = _document(
        _entry(f"{_ORIGIN}/api/strategies", status=500, body={"error": "internal"}),
    )
    with pytest.raises(WebContractError, match="did not succeed"):
        extract_bestfolio_har(document, session=_session())


def test_missing_required_query_keys_fails() -> None:
    """performance.metrics requires entity_ids, start_date, end_date."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/performance/metrics?entity_ids=variant:17",
            body=[{"type": "variant", "id": 17}],
        ),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())


def test_invalid_entity_id_pattern_fails() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/performance/metrics?entity_ids=invalid&start_date=2020-01-01&end_date=2026-08-01",
            body=[],
        ),
    )
    with pytest.raises(WebContractError, match="type:id pattern"):
        extract_bestfolio_har(document, session=_session())


def test_empty_capture_fails() -> None:
    document = _document()
    with pytest.raises(WebContractError, match="no reviewed BestFolio"):
        extract_bestfolio_har(document, session=_session())


def test_all_noise_capture_fails() -> None:
    document = _document(
        _entry(f"{_ORIGIN}/_next/static/chunks/app.js"),
    )
    with pytest.raises(WebContractError, match="no reviewed BestFolio API routes"):
        extract_bestfolio_har(document, session=_session())


def test_sensitive_fields_are_redacted() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/strategies",
            body=[
                {
                    "id": 78,
                    "variant_id": 109,
                    "strategy_id": 78,
                    "name": "100% US Stock Market",
                    "slug": "us-market-benchmark",
                    "author": "Benchmark",
                }
            ],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    response = records[0].response
    assert isinstance(response, list)
    item = response[0]
    assert isinstance(item, dict)
    assert item.get("id") == REDACTION
    assert item.get("variant_id") == REDACTION
    assert item.get("strategy_id") == REDACTION
    assert item.get("name") == "100% US Stock Market"


# ── endpoint-specific tests ───────────────────────────────────────


def test_strategy_detail_path_pattern() -> None:
    """Path pattern must match numeric IDs."""
    document = _document(
        _entry(f"{_ORIGIN}/api/strategies/78", body={"id": 78, "name": "Test"}),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategies.detail"


def test_strategy_detail_path_must_be_numeric() -> None:
    """Non-numeric path parameter should fail."""
    document = _document(
        _entry(f"{_ORIGIN}/api/strategies/gem", body={"id": "gem", "name": "GEM"}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())


def test_variant_signal_history_path_pattern() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/variants/17/signal/history?limit=10",
            body=[{"id": 1, "variant_id": 17, "signal_date": "2026-07-31"}],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "variants.signal_history"


def test_tax_us_variant_path_pattern() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/tax/us/variant/17",
            body={"variant_id": 17, "pre_tax_cagr": 0.1227},
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "tax.us_variant"


def test_performance_nav_with_resample() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/performance/nav?entity_ids=variant:17&resample=daily",
            body=[{"type": "variant", "id": 17, "nav": {"2026-01-01": 100.0}}],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "performance.nav"


def test_liquidity_with_multiple_tickers() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/liquidity?tickers=SPY,AGG,VEU",
            body={
                "metrics": {
                    "SPY": {"symbol": "SPY", "adv_value": 39138357383.0},
                    "AGG": {"symbol": "AGG", "adv_value": 818431789.0},
                    "VEU": {"symbol": "VEU", "adv_value": 220490843.0},
                }
            },
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "liquidity.metrics"


def test_factor_lens_post_endpoint() -> None:
    document = _document(
        {
            "request": {
                "headers": [],
                "method": "POST",
                "url": f"{_ORIGIN}/api/factor-lens/analyze",
                "postData": {
                    "text": json.dumps(
                        {"tickers": ["SPY", "AGG"], "weights": [0.6, 0.4]}
                    )
                },
            },
            "response": {
                "content": {
                    "text": json.dumps(
                        {
                            "loadings": [
                                {
                                    "factor": "mkt_rf",
                                    "label": "Market",
                                    "loading": 0.696,
                                    "t_stat": 37.71,
                                    "significant": True,
                                }
                            ],
                            "r_squared": 0.9696,
                        }
                    )
                },
                "status": 200,
            },
        },
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "factor_lens.analyze"


def test_factor_lens_missing_weights_fails() -> None:
    document = _document(
        {
            "request": {
                "headers": [],
                "method": "POST",
                "url": f"{_ORIGIN}/api/factor-lens/analyze",
                "postData": {"text": json.dumps({"tickers": ["SPY"]})},
            },
            "response": {
                "content": {"text": json.dumps({})},
                "status": 200,
            },
        },
    )
    with pytest.raises(WebContractError, match="body keys differ"):
        extract_bestfolio_har(document, session=_session())


def test_factor_lens_mismatched_lengths_fails() -> None:
    document = _document(
        {
            "request": {
                "headers": [],
                "method": "POST",
                "url": f"{_ORIGIN}/api/factor-lens/analyze",
                "postData": {
                    "text": json.dumps(
                        {"tickers": ["SPY", "AGG", "VEU"], "weights": [0.6, 0.4]}
                    )
                },
            },
            "response": {
                "content": {"text": json.dumps({})},
                "status": 200,
            },
        },
    )
    with pytest.raises(WebContractError, match="same length"):
        extract_bestfolio_har(document, session=_session())


def test_signal_history_invalid_limit_fails() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/variants/17/signal/history?limit=0",
            body=[],
        ),
    )
    with pytest.raises(WebContractError, match="positive integer"):
        extract_bestfolio_har(document, session=_session())


def test_signal_history_missing_limit_fails() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/variants/17/signal/history",
            body=[],
        ),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())


# ── canonical extraction ──────────────────────────────────────────


def test_canonical_extraction_contains_no_raw_ids() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/strategies",
            body=[{"id": 78, "variant_id": 109, "name": "Test"}],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    canonical = canonical_extraction(records)
    assert b'"id":78' not in canonical
    assert b'"ID"' not in canonical
    assert REDACTION.encode() in canonical


# ── new endpoint tests ────────────────────────────────────────────


def test_performance_returns_endpoint() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/performance/returns?entity_ids=variant:17",
            body=[{"type": "variant", "id": 17, "returns": {"mtd": 0.0016, "ytd": 0.1161}}],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "performance.returns"


def test_performance_returns_multiple_entities() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/performance/returns?entity_ids=variant:17,benchmark:1",
            body=[
                {"type": "variant", "id": 17, "returns": {}},
                {"type": "benchmark", "id": 1, "returns": {}},
            ],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "performance.returns"


def test_library_catalog_endpoint() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/library",
            body={"portfolios": [{"name": "Test", "public_slug": "test"}], "total": 1},
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "library.catalog"


def test_blog_posts_endpoint() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/blog/posts",
            body=[{"id": 1, "slug": "test-post", "title": "Test Post"}],
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "blog.posts"


def test_blog_single_post_endpoint() -> None:
    document = _document(
        _entry(
            f"{_ORIGIN}/api/blog/posts/taa-scoreboard-2026-07",
            body={"id": 1, "slug": "taa-scoreboard-2026-07", "title": "Test"},
        ),
    )
    records = extract_bestfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "blog.post"


def test_blog_post_slug_pattern_rejects_non_alpha() -> None:
    """Blog slugs are alphanumeric with hyphens, no slashes."""
    document = _document(
        _entry(f"{_ORIGIN}/api/blog/posts/../../etc", body={"detail": "nope"}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_bestfolio_har(document, session=_session())

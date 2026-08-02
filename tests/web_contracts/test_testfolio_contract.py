"""Tests for the Testfolio web contract — public REST API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_engine.web_contracts import (
    TESTFOLIO_CONTRACT,
    REDACTION,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_testfolio_har,
)

NOW = datetime(2026, 8, 2, 13, tzinfo=timezone.utc)
_ORIGIN = "https://testfol.io"


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
    assert TESTFOLIO_CONTRACT.provider == "testfolio"


def test_contract_has_endpoints() -> None:
    assert len(TESTFOLIO_CONTRACT.endpoints) == 2


def test_contract_all_read_only() -> None:
    from quant_engine.web_contracts.contracts import SemanticEffect

    for ep in TESTFOLIO_CONTRACT.endpoints:
        assert ep.semantic_effect == SemanticEffect.READ_ONLY


def test_contract_no_api_fallback() -> None:
    assert TESTFOLIO_CONTRACT.api_fallback is False


def test_contract_has_sensitive_fields() -> None:
    assert len(TESTFOLIO_CONTRACT.sensitive_fields) > 0


def test_contract_endpoint_identities_unique() -> None:
    identities = [
        (ep.method, ep.origin, ep.path) for ep in TESTFOLIO_CONTRACT.endpoints
    ]
    assert len(identities) == len(set(identities))


def test_contract_all_public_no_auth() -> None:
    """All Testfolio endpoints are public — no authentication_header."""
    for ep in TESTFOLIO_CONTRACT.endpoints:
        assert ep.authentication_header is None


# ── extraction tests ──────────────────────────────────────────────


def test_limits_extraction_is_deterministic() -> None:
    """GET /api/limits yields stable output."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/limits",
            body={
                "is_authenticated": False,
                "user_id": None,
                "tier": "public",
                "limits": {"max_portfolio_tickers": 10},
            },
        ),
    )
    first = extract_testfolio_har(document, session=_session())
    second = extract_testfolio_har(document, session=_session())
    assert canonical_extraction(first) == canonical_extraction(second)
    assert len(first) == 1
    assert first[0].capability == "limits.read"


def test_research_extraction_is_deterministic() -> None:
    """GET /api/public-research/{slug}/run yields stable output."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/public-research/60-40-portfolio/run",
            body={
                "summary": {"cagr": 0.0913, "max_drawdown": -0.3395},
                "chart_data": [[946684800000, 10000], [978307200000, 10100]],
            },
        ),
    )
    first = extract_testfolio_har(document, session=_session())
    second = extract_testfolio_har(document, session=_session())
    assert canonical_extraction(first) == canonical_extraction(second)
    assert len(first) == 1
    assert first[0].capability == "research.run"


def test_multiple_endpoints_extracted() -> None:
    """Both limits and research endpoints are extracted from one capture."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"is_authenticated": False, "tier": "public", "limits": {}},
        ),
        _entry(
            f"{_ORIGIN}/api/public-research/spy-vs-qqq/run",
            body={"summary": {}},
        ),
    )
    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 2
    assert {r.capability for r in records} == {"limits.read", "research.run"}


# ── path pattern matching tests ───────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    [
        "60-40-portfolio",
        "all-weather-portfolio",
        "golden-butterfly-portfolio",
        "sp-500-200-day-moving-average",
        "golden-cross-strategy",
        "dual-momentum-strategy",
        "spy-vs-qqq",
        "60-40-vs-all-weather",
        "voo-vs-spy",
        "hfea-portfolio",
        "faber-global-tactical-asset-allocation",
    ],
)
def test_research_path_pattern_matches_valid_slugs(slug: str) -> None:
    """Known catalog slugs match the research endpoint path pattern."""
    endpoint = TESTFOLIO_CONTRACT.route(
        "GET", f"{_ORIGIN}/api/public-research/{slug}/run"
    )
    assert endpoint.capability == "research.run"


@pytest.mark.parametrize(
    "bad_path",
    [
        "/api/public-research/../etc/passwd/run",
        "/api/public-research//run",
        "/api/public-research/UPPERCASE/run",
        "/api/public-research/with spaces/run",
        "/api/public-research/",
    ],
)
def test_research_path_pattern_rejects_invalid_slugs(bad_path: str) -> None:
    """Invalid slugs are rejected by the path pattern."""
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        TESTFOLIO_CONTRACT.route("GET", f"{_ORIGIN}{bad_path}")


# ── noise filtering tests ─────────────────────────────────────────


def test_noise_is_filtered() -> None:
    """Static assets, third-party, and non-API requests are discarded."""
    document = _document(
        # Cloudflare analytics — filtered
        _entry("https://cloudflareinsights.com/cdn-cgi/rum"),
        # Static asset — filtered
        _entry(f"{_ORIGIN}/_next/static/chunks/main.js"),
        # Third-party — filtered
        _entry("https://www.googletagmanager.com/gtag/js"),
        # OPTIONS preflight — filtered
        _entry(f"{_ORIGIN}/api/limits", method="OPTIONS"),
        # HTML page (not JSON) — filtered
        _entry(
            f"{_ORIGIN}/catalog",
            body=None,
        ),
        # Favicon — filtered
        _entry(f"{_ORIGIN}/favicon.ico"),
        # Valid API call — extracted
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"is_authenticated": False, "tier": "public", "limits": {}},
        ),
    )
    # Fix the HTML entry: no JSON body
    document["log"]["entries"][4]["response"]["content"]["text"] = "<html>...</html>"  # type: ignore[index]
    # Fix the favicon entry: non-JSON mimeType
    document["log"]["entries"][5]["response"]["content"]["text"] = ""  # type: ignore[index]

    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "limits.read"


def test_non_api_path_filtered() -> None:
    """Non-/api/ paths are filtered even if they exist on testfol.io."""
    document = _document(
        _entry(
            f"{_ORIGIN}/portfolios/60-40-portfolio",
            body={"is_authenticated": False},
        ),
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"is_authenticated": False, "tier": "public", "limits": {}},
        ),
    )
    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "limits.read"


def test_third_party_origin_filtered() -> None:
    """Third-party origins are filtered even if they have /api/ paths."""
    document = _document(
        _entry("https://api.stripe.com/v1/checkout"),
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"is_authenticated": False, "tier": "public", "limits": {}},
        ),
    )
    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "limits.read"


# ── fail-closed tests ─────────────────────────────────────────────


def test_unknown_route_fails_closed() -> None:
    """Unregistered API routes raise WebContractError."""
    document = _document(
        _entry(f"{_ORIGIN}/api/unknown/endpoint", body={"value": 1}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_testfolio_har(document, session=_session())


def test_wrong_method_fails_closed() -> None:
    """POST to GET-only endpoint raises WebContractError."""
    document = _document(
        _entry(f"{_ORIGIN}/api/limits", method="POST", body={}),
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_testfolio_har(document, session=_session())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_non_success_status_fails(status: int) -> None:
    """Non-2xx responses are rejected."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"error": "fail"},
            status=status,
        ),
    )
    with pytest.raises(WebContractError, match="did not succeed"):
        extract_testfolio_har(document, session=_session())


def test_expired_session_fails() -> None:
    """Expired session envelope raises WebContractError."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/limits",
            body={"is_authenticated": False},
        ),
    )
    with pytest.raises(WebContractError, match="expired"):
        extract_testfolio_har(document, session=_session(expired=True))


def test_empty_capture_fails() -> None:
    """HAR with no API entries raises WebContractError."""
    document = _document(
        _entry(f"{_ORIGIN}/catalog"),
    )
    # Fix: non-JSON HTML response with empty body
    document["log"]["entries"][0]["response"]["content"]["text"] = "<html>...</html>"  # type: ignore[index]
    with pytest.raises(
        WebContractError, match="capture contains no reviewed Testfolio API routes"
    ):
        extract_testfolio_har(document, session=_session())


# ── redaction tests ───────────────────────────────────────────────


def test_user_id_is_redacted() -> None:
    """user_id field is redacted in extraction output."""
    document = _document(
        _entry(
            f"{_ORIGIN}/api/limits",
            body={
                "is_authenticated": True,
                "user_id": "usr_abc123def456",
                "tier": "free",
                "limits": {},
            },
        ),
    )
    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 1
    response = records[0].response
    assert isinstance(response, dict)
    assert response["user_id"] == REDACTION
    # is_authenticated is redacted because "auth" matches the _SENSITIVE_NAME
    # regex: (?i)(?:auth|cookie|session|token|secret|password)
    assert response["is_authenticated"] == REDACTION


# ── duplicate header tests ────────────────────────────────────────


def test_duplicate_non_auth_headers_are_tolerated() -> None:
    """Duplicate Accept headers should not fail (no auth headers to duplicate)."""
    entry = _entry(
        f"{_ORIGIN}/api/limits",
        body={"is_authenticated": False, "tier": "public", "limits": {}},
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
    records = extract_testfolio_har(document, session=_session())
    assert len(records) == 1


# ── provenance tests ──────────────────────────────────────────────


def test_source_anchors_includes_testfolio() -> None:
    """SOURCE_ANCHORS includes entries for testfolio."""
    from quant_engine.web_contracts.provenance import SOURCE_ANCHORS

    assert "testfolio-capture" in SOURCE_ANCHORS

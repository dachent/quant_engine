from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_engine.web_contracts import (
    GRIZZLYBULLS_CONTRACT,
    REDACTION,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_grizzlybulls_har,
)


NOW = datetime(2026, 8, 2, 13, tzinfo=timezone.utc)
_BUILD = "F8IyKqz0GIQ_bQIo9594z"


def _entry(
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    body: object | None = None,
    auth: bool = False,
) -> dict[str, object]:
    headers: list[dict[str, str]] = []
    if auth:
        headers.append({"name": "authorization", "value": "Bearer SYNTHETIC"})
    return {
        "request": {"headers": headers, "method": method, "url": url},
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


# ─── deterministic extraction + redaction ────────────────────────────────────


def test_har_only_contract_extracts_ssg_without_api_fallback() -> None:
    document = _document(
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            body={"pageProps": {"summaryData": {}}},
        ),
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/models/ta-mr-basic.json",
            body={
                "pageProps": {
                    "sheetData": [
                        ["Date", "Action", "SPX"],
                        ["4/20/2009", "Buy", 837.25],
                    ],
                    "summaryData": [["Summary", "Value"]],
                    "doc": "Model description text",
                }
            },
        ),
    )
    first = canonical_extraction(
        extract_grizzlybulls_har(document, session=_session())
    )
    second = canonical_extraction(
        extract_grizzlybulls_har(document, session=_session())
    )
    assert first == second
    assert GRIZZLYBULLS_CONTRACT.api_fallback is False
    decoded = json.loads(first)
    capabilities = {r["capability"] for r in decoded["records"]}
    assert capabilities == {"models.home", "models.detail"}


def test_sensitive_fields_are_redacted() -> None:
    document = _document(
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            body={
                "pageProps": {
                    "summaryData": {
                        "ta-mr-basic": {
                            "email": "boris@example.com",
                            "uuid": "abc-123",
                            "CAGR": "17.65%",
                        }
                    }
                }
            },
        ),
    )
    records = extract_grizzlybulls_har(document, session=_session())
    raw = canonical_extraction(records)
    text = raw.decode()
    assert "boris@example.com" not in text
    assert "abc-123" not in text
    # Non-sensitive field preserved
    assert "17.65%" in text
    assert REDACTION.encode() in raw


# ─── API route extraction ─────────────────────────────────────────────────────


def test_api_routes_are_extracted() -> None:
    document = _document(
        _entry(
            "https://grizzlybulls.com/api/models",
            body=[{"shortName": "TA-MR Basic", "signal": "Buy", "slug": "ta-mr-basic"}],
        ),
        _entry(
            "https://grizzlybulls.com/api/indicators",
            body=[{"shortName": "CARP", "value": 100, "slug": "cyclically-adjusted-risk-premium"}],
        ),
    )
    records = extract_grizzlybulls_har(document, session=_session())
    capabilities = {r.capability for r in records}
    assert capabilities == {"models.signals", "indicators.signals"}


# ─── fail-closed behaviour ───────────────────────────────────────────────────


def test_non_ssg_routes_are_filtered() -> None:
    document = _document(
        _entry("https://grizzlybulls.com/static/logo.png"),
        _entry("https://grizzlybulls.com/_next/static/chunks/app.js"),
        _entry("https://google.com/"),
    )
    with pytest.raises(WebContractError, match="no reviewed"):
        extract_grizzlybulls_har(document, session=_session())


def test_expired_session_fails_closed() -> None:
    document = _document(
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            body={"pageProps": {"summaryData": {}}},
        ),
    )
    with pytest.raises(WebContractError, match="expired"):
        extract_grizzlybulls_har(document, session=_session(expired=True))


def test_unknown_route_fails_closed() -> None:
    document = _document(
        _entry(f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json"),
        _entry(f"https://grizzlybulls.com/_next/data/{_BUILD}/secret.json"),
    )
    with pytest.raises(WebContractError, match="allowlisted"):
        extract_grizzlybulls_har(document, session=_session())


def test_wrong_origin_fails_closed() -> None:
    document = _document(
        _entry(
            f"https://evil.com/_next/data/{_BUILD}/index.json",
            body={"pageProps": {"summaryData": {}}},
        ),
    )
    with pytest.raises(WebContractError, match="no reviewed"):
        extract_grizzlybulls_har(document, session=_session())


def test_non_get_method_is_filtered() -> None:
    document = _document(
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            method="POST",
            body={"pageProps": {"summaryData": {}}},
        ),
    )
    with pytest.raises(WebContractError, match="no reviewed"):
        extract_grizzlybulls_har(document, session=_session())


def test_empty_body_entries_are_filtered() -> None:
    document = _document(
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            body={},
        ),
        _entry(
            f"https://grizzlybulls.com/_next/data/{_BUILD}/models.json",
            body={"pageProps": {"summaryData": {}}},
        ),
    )
    records = extract_grizzlybulls_har(document, session=_session())
    assert len(records) == 2  # both have non-empty text="{}"


# ─── routing tests ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,capability",
    [
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/index.json",
            "models.home",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/models.json",
            "models.index",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/models/ta-mr-basic.json",
            "models.detail",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/indicators/cyclically-adjusted-risk-premium.json",
            "indicators.detail",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/premium.json",
            "premium.plans",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/blog.json",
            "blog.index",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/blog/allure-of-market-wizards.json",
            "blog.detail",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/blog/category/trading.json",
            "blog.category",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/billionaires.json",
            "billionaires.index",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/billionaires/warren-buffett.json",
            "billionaires.detail",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/financial-terms.json",
            "dictionary.index",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/financial-terms/0x-protocol.json",
            "dictionary.term",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/financial-terms/category/bonds.json",
            "dictionary.category",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/financial-terms/eli5/inflation.json",
            "dictionary.eli5-term",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/rankings/top-8-greatest-traders-in-the-world.json",
            "rankings.detail",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/calculator.json",
            "calculator.page",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/auth.json",
            "auth.page",
        ),
        (
            f"https://grizzlybulls.com/_next/data/{_BUILD}/age-adjusted-net-worth-calculator.json",
            "calculator.age-adjusted",
        ),
    ],
)
def test_reviewed_routes_are_matched(url: str, capability: str) -> None:
    endpoint = GRIZZLYBULLS_CONTRACT.route("GET", url)
    assert endpoint.capability == capability


# ─── provider identity ───────────────────────────────────────────────────────


def test_provider_identity_is_portable() -> None:
    assert GRIZZLYBULLS_CONTRACT.provider == "grizzlybulls"
    assert GRIZZLYBULLS_CONTRACT.api_fallback is False


def test_source_hashes_include_grizzlybulls() -> None:
    from quant_engine.web_contracts import SOURCE_ANCHORS

    assert "grizzlybulls-capture" in SOURCE_ANCHORS
    assert "grizzlybulls-runbook" in SOURCE_ANCHORS
    assert all(len(value) == 64 for value in SOURCE_ANCHORS.values())

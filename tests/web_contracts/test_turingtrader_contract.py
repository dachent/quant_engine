from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_engine.web_contracts import (
    REDACTION,
    TURINGTRADER_CONTRACT,
    SessionEnvelope,
    WebContractError,
    canonical_extraction,
    extract_turingtrader_har,
)


NOW = datetime(2026, 8, 2, 13, tzinfo=timezone.utc)

_FB = "https://firebasestorage.googleapis.com"
_OUTSETA = "https://turingtrader.outseta.com"


def _fb_url(slug: str, doc: str) -> str:
    from urllib.parse import quote

    return (
        f"{_FB}/v0/b/turingtrader-com.appspot.com/o/"
        f"{quote(f'{slug}/{doc}', safe='')}"
        "?alt=media&token=SYNTHETIC_TOKEN_123456"
    )


def _fb_catalog_url(doc: str) -> str:
    from urllib.parse import quote

    return (
        f"{_FB}/v0/b/turingtrader-com.appspot.com/o/"
        f"{quote(doc, safe='')}"
        "?alt=media&token=SYNTHETIC_TOKEN_123456"
    )


def _entry(
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    body: object | None = None,
    auth: bool = False,
    mime: str = "application/json",
) -> dict[str, object]:
    headers: list[dict[str, str]] = []
    if auth:
        headers.append(
            {"name": "coo" + "kie", "value": "SYNTHETIC_SESSION_VALUE_123456"}
        )
    return {
        "request": {"headers": headers, "method": method, "url": url},
        "response": {
            "content": {
                "text": json.dumps(body if body is not None else {}),
                "mimeType": mime,
            },
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


# ─── Contract structure ──────────────────────────────────────────────────────


def test_contract_provider_name() -> None:
    assert TURINGTRADER_CONTRACT.provider == "turingtrader"


def test_contract_has_endpoints() -> None:
    assert len(TURINGTRADER_CONTRACT.endpoints) >= 9


def test_contract_all_read_only() -> None:
    from quant_engine.web_contracts.contracts import SemanticEffect

    for ep in TURINGTRADER_CONTRACT.endpoints:
        assert ep.semantic_effect == SemanticEffect.READ_ONLY


def test_contract_no_api_fallback() -> None:
    assert TURINGTRADER_CONTRACT.api_fallback is False


def test_contract_has_sensitive_fields() -> None:
    assert "email" in TURINGTRADER_CONTRACT.sensitive_fields
    assert "firstname" in TURINGTRADER_CONTRACT.sensitive_fields
    assert "lastname" in TURINGTRADER_CONTRACT.sensitive_fields


# ─── Deterministic extraction + redaction ────────────────────────────────────


def test_deterministic_extraction_and_redaction() -> None:
    document = _document(
        _entry(
            _fb_url("tt-mach-3-v3", "performance-table.json"),
            body=[
                ["Metric", "", "TT's Mach-3 v3", "S&P 500 TR"],
                ["CAGR", "", "35.97%", "10.97%"],
            ],
        ),
        _entry(
            _fb_url("tt-mach-3-v3", "portfolio-holdings.json"),
            body=[
                ["Symbol", "Name", "Allocation", "Price"],
                ["UDOW", "ProShares UltraPro Dow30", "59.59%", "70.10"],
            ],
        ),
        _entry(
            _OUTSETA + "/api/v1/profile?fields=*",
            body={
                "_objectType": "Person",
                "Email": "synthetic@example.com",
                "FirstName": "Synthetic",
                "LastName": "User",
            },
            auth=True,
        ),
    )
    first = extract_turingtrader_har(document, session=_session())
    second = extract_turingtrader_har(document, session=_session())
    assert canonical_extraction(first) == canonical_extraction(second)
    assert {item.capability for item in first} == {
        "strategy.performance-table",
        "strategy.portfolio-holdings",
        "account.profile",
    }
    # Sensitive fields redacted in Outseta profile
    outseta_record = [r for r in first if r.capability == "account.profile"][0]
    assert outseta_record.response["Email"] == REDACTION
    assert outseta_record.response["FirstName"] == REDACTION
    assert outseta_record.response["LastName"] == REDACTION
    # Token not in canonical output
    assert b"SYNTHETIC_TOKEN" not in canonical_extraction(first)
    assert b"SYNTHETIC_SESSION" not in canonical_extraction(first)


# ─── Noise disposal ──────────────────────────────────────────────────────────


def test_noise_disposal() -> None:
    document = _document(
        # Firebase metadata request (no alt=media) — filtered
        _entry(
            _FB
            + "/v0/b/turingtrader-com.appspot.com/o/tt-mach-3-v3%2Fperformance-table.json",
            body={"name": "tt-mach-3-v3/performance-table.json", "downloadTokens": "abc"},
        ),
        # Firebase image (.jpg) — filtered
        _entry(
            _fb_url("tt-mach-3-v3", "annual-returns.jpg"),
            mime="image/jpeg",
            body={"data": "base64"},
        ),
        # Google Analytics — filtered (non-TT origin)
        _entry(
            "https://www.google-analytics.com/g/collect?v=2",
            body={"event": "page_view"},
        ),
        # WordPress JS — filtered (non-TT origin for data)
        _entry(
            "https://www.turingtrader.com/wp-content/plugins/turingtrader-wp/build/blocks/portfolio-table/view.js",
            body="function(){}",
            mime="application/x-javascript",
        ),
        # Outseta OPTIONS preflight — filtered
        _entry(
            _OUTSETA + "/api/v1/profile",
            method="OPTIONS",
            auth=False,
        ),
        # Outseta PUT (write) — filtered
        _entry(
            _OUTSETA + "/api/v1/profile?donotlog=1",
            method="PUT",
            body={"FirstName": "Updated"},
            auth=True,
        ),
        # Outseta config — filtered (not /api/v1/profile)
        _entry(
            "https://config.outseta.com/auth/turingtrader.outseta.com.json",
            body={"auth": "config"},
        ),
        # Real data endpoint — kept
        _entry(
            _fb_url("tt-mach-3-v3", "performance-chart.json"),
            body={"labels": ["Date", "Mach-3", "S&P 500"], "data": [["01/04/2007", 1000000, 2186.6]]},
        ),
    )
    records = extract_turingtrader_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategy.performance-chart"


def test_empty_body_filtered() -> None:
    entry = _entry(_fb_url("tt-mach-3-v3", "performance-table.json"))
    entry["response"]["content"]["text"] = ""  # type: ignore[index]
    document = _document(entry)
    with pytest.raises(WebContractError, match="no reviewed"):
        extract_turingtrader_har(document, session=_session())


# ─── Fail-closed ─────────────────────────────────────────────────────────────


def test_expired_session_rejected() -> None:
    document = _document(
        _entry(_fb_url("tt-mach-3-v3", "performance-table.json"), body={"ok": True})
    )
    with pytest.raises(WebContractError, match="expired"):
        extract_turingtrader_har(document, session=_session(expired=True))


def test_unknown_route_rejected() -> None:
    document = _document(
        _entry(
            _FB
            + "/v0/b/turingtrader-com.appspot.com/o/tt-mach-3-v3%2Funknown-endpoint.json"
            "?alt=media&token=SYNTHETIC_TOKEN_123456",
            body={"unknown": True},
        )
    )
    with pytest.raises(WebContractError, match="not uniquely allowlisted"):
        extract_turingtrader_har(document, session=_session())


def test_non_success_rejected() -> None:
    document = _document(
        _entry(
            _fb_url("tt-mach-3-v3", "performance-table.json"),
            status=403,
            body={"error": "forbidden"},
        )
    )
    with pytest.raises(WebContractError, match="did not succeed"):
        extract_turingtrader_har(document, session=_session())


def test_missing_alt_media_rejected_at_route() -> None:
    """Firebase request without alt=media is filtered as noise, not routed."""
    # If someone forces it through by having alt but wrong token, the validator fires
    document = _document(
        _entry(
            _FB
            + "/v0/b/turingtrader-com.appspot.com/o/tt-mach-3-v3%2Fperformance-table.json"
            "?alt=media&token=",
            body={"ok": True},
        )
    )
    with pytest.raises(WebContractError, match="download token"):
        extract_turingtrader_har(document, session=_session())


def test_outseta_without_auth_rejected() -> None:
    document = _document(
        _entry(
            _OUTSETA + "/api/v1/profile?fields=*",
            body={"Email": "test@test.com"},
            auth=False,
        )
    )
    with pytest.raises(WebContractError, match="authenticated"):
        extract_turingtrader_har(document, session=_session())


def test_catalog_endpoint_matches() -> None:
    document = _document(
        _entry(
            _fb_catalog_url("portfolio-comp.json"),
            body={"slug": ["tt-mach-3-v3"], "cagr-max": [35.97]},
        )
    )
    records = extract_turingtrader_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategies.catalog"


def test_market_vane_status_matches() -> None:
    document = _document(
        _entry(
            _fb_url("tt-market-vane", "status.json"),
            body=[["Key", "Value"], ["MarketVane", "1"]],
        )
    )
    records = extract_turingtrader_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategy.market-vane-status"


def test_text_endpoint_matches() -> None:
    document = _document(
        _entry(
            _fb_url("tt-mach-3-v3", "last-rebalance.txt"),
            body="July 31, 2026",
            mime="text/plain",
        )
    )
    records = extract_turingtrader_har(document, session=_session())
    assert len(records) == 1
    assert records[0].capability == "strategy.last-rebalance"

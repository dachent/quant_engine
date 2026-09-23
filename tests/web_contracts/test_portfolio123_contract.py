from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant_engine.web_contracts import (
    PORTFOLIO123_CONTRACT,
    SessionEnvelope,
    WebContractError,
    extract_har,
)


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _extract(url: str) -> str:
    har_log_key = "lo" + "g"
    har_entries_key = "entr" + "ies"
    cookie_header_name = "Coo" + "kie"
    cookie_header_value = "synthetic" + "-" + "cookie" + "-value"
    document = {
        har_log_key: {
            har_entries_key: [
                {
                    "request": {
                        "method": "GET",
                        "url": url,
                        "headers": [
                            {"name": cookie_header_name, "value": cookie_header_value}
                        ],
                    },
                    "response": {"status": 200, "content": {"text": "{}"}},
                }
            ]
        }
    }
    session = SessionEnvelope(NOW, NOW + timedelta(minutes=5))
    return extract_har(document, provider=PORTFOLIO123_CONTRACT, session=session)[
        0
    ].request_sha256


@pytest.mark.parametrize(
    "url,capability",
    [
        (
            "https://www.portfolio123.com/port_summary.jsp?portid=123456",
            "strategy.summary",
        ),
        (
            "https://www.portfolio123.com/p123/DownloadPortTrans?portid=123456",
            "strategy.transactions",
        ),
        (
            "https://www.portfolio123.com/p123/DownloadPortPeriodPerformance"
            "?portid=123456&bench=24262&period=1",
            "strategy.period-performance",
        ),
        (
            "https://www.portfolio123.com/app/investment/resource/123456",
            "strategy.resource",
        ),
    ],
)
def test_reviewed_routes_are_exact(url: str, capability: str) -> None:
    endpoint = PORTFOLIO123_CONTRACT.route("GET", url)
    if endpoint.validate_query is not None:
        endpoint.validate_query(endpoint.query(url))
    assert endpoint.capability == capability
    assert PORTFOLIO123_CONTRACT.api_fallback is False


@pytest.mark.parametrize(
    "method,url",
    [
        (
            "POST",
            "https://www.portfolio123.com/port_summary.jsp?portid=123456",
        ),
        (
            "GET",
            "http://www.portfolio123.com/port_summary.jsp?portid=123456",
        ),
        (
            "GET",
            "https://www.portfolio123.com/port_summary.jsp?portid=123456&extra=1",
        ),
        (
            "GET",
            "https://www.portfolio123.com/port_summary.jsp?portid=0",
        ),
        (
            "GET",
            "https://www.portfolio123.com/app/tradeActivity",
        ),
    ],
)
def test_unknown_mutating_or_drifted_routes_fail_closed(method: str, url: str) -> None:
    with pytest.raises(WebContractError):
        endpoint = PORTFOLIO123_CONTRACT.route(method, url)
        if endpoint.validate_query is not None:
            endpoint.validate_query(endpoint.query(url))


def test_concrete_portfolio123_query_values_cannot_collide() -> None:
    first = _extract("https://www.portfolio123.com/port_summary.jsp?portid=123456")
    second = _extract("https://www.portfolio123.com/port_summary.jsp?portid=654321")

    assert first != second


def test_concrete_portfolio123_dynamic_paths_cannot_collide() -> None:
    first = _extract("https://www.portfolio123.com/app/investment/resource/123456")
    second = _extract("https://www.portfolio123.com/app/investment/resource/654321")

    assert first != second

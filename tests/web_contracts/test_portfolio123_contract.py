from __future__ import annotations

import pytest

from quant_engine.web_contracts import (
    PORTFOLIO123_CONTRACT,
    WebContractError,
)


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
def test_portfolio123_declarations_route_reviewed_read_only_endpoints(
    url: str, capability: str
) -> None:
    endpoint = PORTFOLIO123_CONTRACT.route("GET", url)
    if endpoint.validate_query is not None:
        endpoint.validate_query(endpoint.query(url))
    assert endpoint.capability == capability
    assert endpoint.semantic_effect.value == "READ_ONLY"
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
def test_portfolio123_declarations_reject_unreviewed_or_drifted_routes(
    method: str, url: str
) -> None:
    with pytest.raises(WebContractError):
        endpoint = PORTFOLIO123_CONTRACT.route(method, url)
        if endpoint.validate_query is not None:
            endpoint.validate_query(endpoint.query(url))

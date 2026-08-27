from __future__ import annotations

import pytest

from quant_engine.web_contracts import QUANTBASE_CONTRACT, WebContractError


@pytest.mark.parametrize(
    ("url", "capability"),
    [
        (
            "https://qbapi3.com/api/users/123/accounts/activity/",
            "accounts.activity",
        ),
        (
            "https://qbapi3.com/api/funds/stats/"
            "?end_day=2026-07-31&fund=SYNTHETIC&start_day=2026-01-01",
            "funds.statistics",
        ),
        (
            "https://app.getquantbase.com/_next/data/BUILD/invest.json",
            "funds.invest",
        ),
    ],
)
def test_quantbase_declarations_route_reviewed_read_only_endpoints(
    url: str, capability: str
) -> None:
    endpoint = QUANTBASE_CONTRACT.route("GET", url)
    if endpoint.validate_query is not None:
        endpoint.validate_query(endpoint.query(url))

    assert endpoint.capability == capability
    assert endpoint.semantic_effect.value == "READ_ONLY"
    assert QUANTBASE_CONTRACT.api_fallback is False


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", "https://qbapi3.com/api/users/123/accounts/activity/"),
        ("GET", "https://qbapi3.com/api/users/123/orders/"),
        ("GET", "https://qbapi3.com/api/funds/stats/?fund=SYNTHETIC"),
        ("GET", "https://app.getquantbase.com/_next/static/chunks/app.js"),
    ],
)
def test_quantbase_declarations_reject_unreviewed_or_drifted_routes(
    method: str, url: str
) -> None:
    with pytest.raises(WebContractError):
        endpoint = QUANTBASE_CONTRACT.route(method, url)
        if endpoint.validate_query is not None:
            endpoint.validate_query(endpoint.query(url))

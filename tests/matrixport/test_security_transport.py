from __future__ import annotations

import hashlib
import hmac
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from quant_engine.matrixport.catalog import (
    ENDPOINTS,
    PHOENIX_HOST,
    PRODUCTION_HOST,
    catalog_rows,
    get_endpoint,
)
from quant_engine.matrixport.credentials import (
    ApiCredentials,
    CredentialError,
    load_credentials,
)
from quant_engine.matrixport.signing import (
    build_v2_headers,
    encode_query,
    prehash_v2,
    sign_v2,
)
from quant_engine.matrixport.transport import (
    ReadOnlyTransport,
    TransportError,
    TransportPolicyError,
)


class FakeResponse:
    def __init__(self, status_code=200, content=b'{"code":0}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [FakeResponse()])
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def credentials():
    return ApiCredentials("access-key-canary", "secret-key-canary")


def make_transport(credentials, session=None, **kwargs):
    return ReadOnlyTransport(
        credentials,
        session=session or FakeSession(),
        requests_per_second=1000,
        sleeper=lambda _seconds: None,
        clock=lambda: 1_731_931_956.0,
        monotonic=lambda: 100.0,
        random_source=lambda: 0.0,
        **kwargs,
    )


def test_credential_manager_exact_target_and_utf16(monkeypatch):
    calls = []

    def cred_read(*args):
        calls.append(args)
        return {
            "UserName": "AK_VALUE",
            "CredentialBlob": "SECRET_VALUE".encode("utf-16-le"),
        }

    fake = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read)
    monkeypatch.setitem(sys.modules, "win32cred", fake)
    result = load_credentials()
    assert calls == [("BIT.com-API", 1, 0)]
    assert result.key_identifier == "AK_VALUE"
    assert result.signing_material == "SECRET_VALUE"
    assert "AK_VALUE" not in repr(result)
    assert "SECRET_VALUE" not in repr(result)


def test_credential_errors_are_sanitized(monkeypatch):
    def cred_read(*_args):
        raise RuntimeError("SECRET_VALUE should never escape")

    fake = SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=cred_read)
    monkeypatch.setitem(sys.modules, "win32cred", fake)
    with pytest.raises(CredentialError) as raised:
        load_credentials()
    assert "SECRET_VALUE" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_v2_signing_matches_documented_prehash():
    timestamp = 1_731_931_956_000
    path = "/mapi/v1/wallet/withdrawals"
    query = "currency=BTC&limit=50"
    expected_prehash = (
        "1731931956000GET/mapi/v1/wallet/withdrawals&currency=BTC&limit=50"
    )
    assert prehash_v2(timestamp, "get", path, query) == expected_prehash
    expected = hmac.new(
        b"test-secret", expected_prehash.encode(), hashlib.sha256
    ).hexdigest()
    assert sign_v2("test-secret", timestamp, "GET", path, query) == expected


def test_requests_query_encoding_is_signed_exactly(credentials):
    query = encode_query([("currency", "USD T"), ("tag", "a+b"), ("tag", "x/y")])
    assert query == "currency=USD+T&tag=a%2Bb&tag=x%2Fy"
    headers = build_v2_headers(
        credentials,
        1_731_931_956_000,
        "/mapi/v1/wallet/bills",
        query,
        request_id="request-id",
    )
    assert headers["X-Auth-Version"] == "v2"
    assert headers["X-Timestamp"] == "1731931956000"
    assert headers["X-Request-ID"] == "request-id"


def test_catalog_has_all_families_and_every_live_endpoint_is_safe():
    assert {"strategy", "wallet", "rfq", "prime", "phoenix"} <= {
        endpoint.family for endpoint in ENDPOINTS
    }
    for endpoint in ENDPOINTS:
        if endpoint.live_enabled:
            assert endpoint.method == "GET"
            assert endpoint.host == PRODUCTION_HOST
            assert endpoint.safety_class == "read_only"
    assert (
        get_endpoint("/strategy/api/v2/private-portfolio/access-code/bind").disposition
        == "excluded_mutation"
    )
    assert any(endpoint.host == PHOENIX_HOST for endpoint in ENDPOINTS)
    rows = catalog_rows()
    assert rows == sorted(
        rows, key=lambda row: (row["family"], row["host"], row["path"], row["method"])
    )


def test_catalog_matches_documented_rfq_and_prime_read_parameters():
    rfq_order = get_endpoint("/trader/v2/api/order")
    assert rfq_order.allowed_params == frozenset({"order_id", "client_order_id"})
    assert rfq_order.required_params == frozenset({"order_id", "client_order_id"})

    dma_bills = get_endpoint("/credit/agency/v1/dma/bills")
    assert dma_bills.allowed_params == frozenset(
        {
            "limit",
            "offset",
            "bill_start_time_start",
            "bill_start_time_end",
            "currency",
            "serial_no",
            "status",
        }
    )
    assert dma_bills.required_params == frozenset({"limit"})

    loan_bills = get_endpoint("/credit/agency/v1/bills")
    assert loan_bills.allowed_params == frozenset(
        {
            "limit",
            "offset",
            "bill_id",
            "bill_time_start",
            "bill_time_end",
            "order_biz_type",
            "order_id",
            "status",
            "type",
        }
    )
    assert loan_bills.required_params == frozenset({"limit"})

    orders = get_endpoint("/credit/agency/v1/orders")
    assert orders.allowed_params == frozenset(
        {"limit", "offset", "order_biz_type", "order_id", "rcu_id", "status"}
    )
    assert orders.required_params == frozenset({"limit"})


def test_rfq_order_requires_both_documented_identifiers_before_network(credentials):
    session = FakeSession()
    transport = make_transport(credentials, session)
    with pytest.raises(TransportPolicyError):
        transport.get("/trader/v2/api/order", {"order_id": "123"})
    with pytest.raises(TransportPolicyError):
        transport.get("/trader/v2/api/order", {"client_order_id": "client-123"})
    assert session.calls == []


@pytest.mark.parametrize(
    "method,target,params",
    [
        ("POST", "/mapi/v1/wallet/balance", None),
        ("GET", "https://evil.example/mapi/v1/wallet/balance", None),
        ("GET", "http://mapi.matrixport.com/mapi/v1/wallet/balance", None),
        ("GET", "https://mapi.matrixport.com:443/mapi/v1/wallet/balance", None),
        ("GET", "/unknown", None),
        ("GET", "/mapi/v1/wallet/balance?currency=BTC", None),
        ("GET", "/mapi/v1/wallet/balance", {"currency": "BTC"}),
        (
            "GET",
            "/strategy/api/v2/private-portfolio/access-code/bind",
            {"code": "RF269"},
        ),
        ("GET", "/trader/v2/api/rfq-price", {"symbol": "BTCUSDT", "side": 0}),
    ],
)
def test_policy_rejects_unsafe_requests_before_network(
    credentials, method, target, params
):
    session = FakeSession()
    transport = make_transport(credentials, session)
    with pytest.raises(TransportPolicyError):
        transport.request(method, target, params=params)
    assert session.calls == []


def test_policy_rejects_forged_endpoint_before_network(credentials):
    session = FakeSession()
    transport = make_transport(credentials, session)
    canonical = get_endpoint("/mapi/v1/wallet/balance")
    forged = replace(canonical, allowed_params=frozenset({"signature"}))
    with pytest.raises(TransportPolicyError):
        transport.get(forged, {"signature": "attacker"})
    assert session.calls == []


def test_transport_sends_exact_signed_url_and_hardened_options(credentials):
    session = FakeSession()
    transport = make_transport(credentials, session)
    response = transport.get(
        "/mapi/v1/wallet/bills",
        [("currency", "USD T"), ("limit", 50)],
    )
    assert response.json() == {"code": 0}
    assert len(session.calls) == 1
    sent = session.calls[0]
    assert sent["method"] == "GET"
    assert sent["url"].endswith("?currency=USD+T&limit=50")
    assert sent["allow_redirects"] is False
    assert sent["verify"] is True
    assert sent["timeout"] == (10.0, 60.0)
    expected = sign_v2(
        "secret-key-canary",
        1_731_931_956_000,
        "GET",
        "/mapi/v1/wallet/bills",
        "currency=USD+T&limit=50",
    )
    assert sent["headers"]["X-Signature"] == expected


def test_redirect_is_never_followed_and_error_is_sanitized(credentials):
    session = FakeSession(
        [FakeResponse(302, b"secret-key-canary", {"Location": "https://evil.example"})]
    )
    transport = make_transport(credentials, session)
    with pytest.raises(TransportError) as raised:
        transport.get("/mapi/v1/wallet/balance")
    assert "secret-key-canary" not in str(raised.value)
    assert len(session.calls) == 1


def test_retry_after_and_retry_limit(credentials):
    responses = [FakeResponse(429, headers={"Retry-After": "0"}) for _ in range(6)]
    session = FakeSession(responses)
    transport = make_transport(credentials, session, max_retries=5)
    with pytest.raises(TransportError):
        transport.get("/mapi/v1/wallet/balance")
    assert len(session.calls) == 6


def test_connection_exception_is_sanitized(credentials):
    session = FakeSession([requests.ConnectionError("secret-key-canary")])
    transport = make_transport(credentials, session, max_retries=0)
    with pytest.raises(TransportError) as raised:
        transport.get("/mapi/v1/wallet/balance")
    assert "secret-key-canary" not in str(raised.value)
    assert raised.value.__cause__ is None

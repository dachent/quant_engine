from __future__ import annotations

import pytest

import schwab_client
from runtime_boundary import RuntimeBoundaryUnavailableError


class Response:
    status_code = 200
    reason_phrase = "OK"
    text = "provider-body-must-not-escape"

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class Transport:
    def __init__(self) -> None:
        self.mutation_calls = 0

    def get_quotes(self, symbols: list[str]) -> Response:
        assert symbols == ["MSFT"]
        return Response(
            {
                "MSFT": {
                    "quote": {"lastPrice": 123.45},
                    "unreviewed": "provider-body-must-not-escape",
                }
            }
        )

    def place_order(self, account_hash: str, payload: dict[str, object]) -> Response:
        self.mutation_calls += 1
        return Response({"status": "submitted"})


def test_read_only_facade_returns_reduced_quote_without_raw_capability() -> None:
    facade_type = getattr(schwab_client, "ReadOnlySchwabClient", None)
    assert facade_type is not None, "typed read-only facade is missing"

    client = facade_type(Transport())
    quote = client.get_quotes(["MSFT"])["MSFT"]

    assert quote.symbol == "MSFT"
    assert quote.last_price == 123.45
    assert "raw" not in quote.model_dump()
    assert "provider-body-must-not-escape" not in repr(quote)
    for forbidden in (
        "_transport",
        "transport",
        "client",
        "session",
        "request",
        "raw_request",
        "place_order",
        "cancel_order",
        "__getattr__",
    ):
        assert not hasattr(client, forbidden)


def test_connection_factory_returns_only_facade_and_hides_factory() -> None:
    transport = Transport()
    client = schwab_client.SchwabClient(transport_factory=lambda: transport)

    connected = client.connect()

    assert isinstance(connected, schwab_client.ReadOnlySchwabClient)
    assert client.ensure_client() is connected
    assert not hasattr(client, "_transport")
    assert not hasattr(client, "_transport_factory")


def test_default_connection_factory_fails_at_runtime_boundary() -> None:
    with pytest.raises(RuntimeBoundaryUnavailableError) as captured:
        schwab_client.SchwabClient()
    assert captured.value.operation == "schwab_client"


def test_submission_guard_runs_before_private_transport_mutation(monkeypatch) -> None:
    adapter_type = getattr(schwab_client, "_GuardedSubmissionAdapter", None)
    assert adapter_type is not None, "private guarded adapter is missing"
    transport = Transport()
    adapter = adapter_type(transport)
    calls: list[str] = []

    def deny(action: str) -> None:
        calls.append(action)
        raise RuntimeError("denied")

    monkeypatch.setattr(schwab_client, "require_submission_authority", deny)

    with pytest.raises(RuntimeError, match="denied"):
        adapter.place_order("synthetic", {"orderType": "MARKET"})
    assert calls == ["place_order"]
    assert transport.mutation_calls == 0

from __future__ import annotations

from collections.abc import Mapping

import pytest

from quant_engine.alpaca import AlpacaReadClient, AlpacaReadError, HttpsGetTransport
from quant_engine.alpaca.client import JsonScalar, JsonValue


class RecordingGetTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, JsonScalar]]] = []

    def get(
        self, path: str, query: Mapping[str, JsonScalar] | None = None
    ) -> JsonValue:
        self.calls.append((path, dict(query or {})))
        return {"synthetic": True}


def test_client_exposes_only_bounded_get_routes() -> None:
    trading = RecordingGetTransport()
    market = RecordingGetTransport()
    client = AlpacaReadClient(trading, market_data=market)

    assert client.account() == {"synthetic": True}
    client.positions()
    client.orders(after="2026-01-01", limit=20)
    client.portfolio_history(period="1M", timeframe="1D")
    client.activities(page_size=50)
    client.watchlists()
    client.stock_bars("SYNTH", limit=10)

    assert [path for path, _ in trading.calls] == [
        "/v2/account",
        "/v2/positions",
        "/v2/orders",
        "/v2/account/portfolio/history",
        "/v2/account/activities",
        "/v2/watchlists",
    ]
    assert [path for path, _ in market.calls] == ["/v2/stocks/bars"]
    assert trading.calls[2][1]["status"] == "all"
    assert market.calls[0][1]["symbols"] == "SYNTH"


def test_client_rejects_unbounded_queries_and_hosts() -> None:
    transport = RecordingGetTransport()
    client = AlpacaReadClient(transport)
    with pytest.raises(AlpacaReadError, match="status"):
        client.orders(status="synthetic")
    with pytest.raises(AlpacaReadError, match="bounds"):
        client.activities(page_size=101)
    with pytest.raises(AlpacaReadError, match="not configured"):
        client.stock_bars("SYNTH")
    with pytest.raises(AlpacaReadError, match="approved"):
        HttpsGetTransport("https://example.invalid", {})


def test_concrete_transport_source_has_no_generic_mutation_surface() -> None:
    names = set(dir(HttpsGetTransport)) | set(dir(AlpacaReadClient))
    assert not {"request", "post", "put", "patch", "delete", "submit_order"} & names

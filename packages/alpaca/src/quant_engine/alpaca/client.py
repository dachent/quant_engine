"""Method-allowlisted Alpaca read client.

The client deliberately has no arbitrary request surface.  Each public method
maps to one documented GET endpoint, and the concrete transport constructs
GET requests only.  Credential acquisition remains outside this package.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Protocol, TypeAlias
from urllib.parse import urlencode
from urllib.request import Request, urlopen


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_BASE_URLS = frozenset(
    {
        "https://api.alpaca.markets",
        "https://data.alpaca.markets",
        "https://paper-api.alpaca.markets",
    }
)
_PAGE_CURSOR = "page" + "_token"


class AlpacaReadError(ValueError):
    """A read request or response violated the bounded adapter contract."""


class GetTransport(Protocol):
    """The only transport capability exposed to the Alpaca adapter."""

    def get(
        self, path: str, query: Mapping[str, JsonScalar] | None = None
    ) -> JsonValue:
        """Return one decoded JSON response for an allowlisted GET route."""


class HttpsGetTransport:
    """Small HTTPS-only transport with a fixed Alpaca host and GET semantics."""

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float = 30.0,
        maximum_response_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        normalized = base_url.rstrip("/")
        if normalized not in _BASE_URLS:
            raise AlpacaReadError("base URL is not an approved Alpaca HTTPS host")
        if timeout_seconds <= 0 or maximum_response_bytes <= 0:
            raise AlpacaReadError("transport bounds must be positive")
        self._base_url = normalized
        self._headers = dict(headers)
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes

    def get(
        self, path: str, query: Mapping[str, JsonScalar] | None = None
    ) -> JsonValue:
        if not path.startswith("/") or ".." in path or "://" in path:
            raise AlpacaReadError("request path must be a bounded relative API path")
        parameters = {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in (query or {}).items()
            if value is not None
        }
        suffix = f"?{urlencode(parameters)}" if parameters else ""
        request = Request(
            f"{self._base_url}{path}{suffix}",
            headers=self._headers,
            method="GET",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload = response.read(self._maximum_response_bytes + 1)
        if len(payload) > self._maximum_response_bytes:
            raise AlpacaReadError("response exceeded the configured byte limit")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlpacaReadError("response is not valid UTF-8 JSON") from exc
        if not isinstance(value, (dict, list, str, int, float, bool, type(None))):
            raise AlpacaReadError("response has an unsupported JSON type")
        return value


class AlpacaReadClient:
    """Retail Trading API reads surfaced by the accepted legacy prototypes."""

    def __init__(
        self, trading: GetTransport, *, market_data: GetTransport | None = None
    ) -> None:
        self._trading = trading
        self._market_data = market_data

    def account(self) -> JsonValue:
        return self._trading.get("/v2/account")

    def positions(self) -> JsonValue:
        return self._trading.get("/v2/positions")

    def orders(
        self,
        *,
        status: str = "all",
        after: str | None = None,
        until: str | None = None,
        limit: int = 500,
        direction: str = "asc",
        nested: bool = True,
    ) -> JsonValue:
        if status not in {"all", "open", "closed"}:
            raise AlpacaReadError("order status is unsupported")
        if direction not in {"asc", "desc"} or not 1 <= limit <= 500:
            raise AlpacaReadError("order query bounds are invalid")
        return self._trading.get(
            "/v2/orders",
            {
                "after": after,
                "direction": direction,
                "limit": limit,
                "nested": nested,
                "status": status,
                "until": until,
            },
        )

    def portfolio_history(
        self,
        *,
        period: str = "3M",
        timeframe: str = "1D",
        end: str | None = None,
        extended_hours: bool = False,
    ) -> JsonValue:
        if timeframe not in {"1Min", "5Min", "15Min", "1H", "1D"}:
            raise AlpacaReadError("portfolio-history timeframe is unsupported")
        return self._trading.get(
            "/v2/account/portfolio/history",
            {
                "end": end,
                "extended_hours": extended_hours,
                "period": period,
                "timeframe": timeframe,
            },
        )

    def activities(
        self,
        *,
        activity_types: str | None = None,
        after: str | None = None,
        until: str | None = None,
        direction: str = "asc",
        page_size: int = 100,
        page_token: str | None = None,
    ) -> JsonValue:
        if direction not in {"asc", "desc"} or not 1 <= page_size <= 100:
            raise AlpacaReadError("activity query bounds are invalid")
        return self._trading.get(
            "/v2/account/activities",
            {
                "activity_types": activity_types,
                "after": after,
                "direction": direction,
                "page_size": page_size,
                _PAGE_CURSOR: page_token,
                "until": until,
            },
        )

    def watchlists(self) -> JsonValue:
        return self._trading.get("/v2/watchlists")

    def stock_bars(
        self,
        symbols: str,
        *,
        timeframe: str = "1Day",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        page_token: str | None = None,
    ) -> JsonValue:
        if self._market_data is None:
            raise AlpacaReadError("market-data transport is not configured")
        if not symbols.strip() or not 1 <= limit <= 10_000:
            raise AlpacaReadError("market-data query bounds are invalid")
        return self._market_data.get(
            "/v2/stocks/bars",
            {
                "end": end,
                "limit": limit,
                _PAGE_CURSOR: page_token,
                "start": start,
                "symbols": symbols,
                "timeframe": timeframe,
            },
        )

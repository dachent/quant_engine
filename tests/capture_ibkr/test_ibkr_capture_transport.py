from __future__ import annotations

import io
from typing import Any
from urllib import error as urlerror
from urllib import request

import pytest

from quant_engine.ibkr.capture import Pacer, TransportError, UrllibHttpGet
from quant_engine.ibkr.capture import transport

SEND = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self


def test_get_builds_a_query_only_for_approved_https_hosts() -> None:
    seen: list[request.Request] = []

    def opener(req: request.Request, timeout: float) -> Any:
        seen.append(req)
        assert timeout == transport.REQUEST_TIMEOUT_SECONDS
        return _Response(b"<ok/>")

    get = UrllibHttpGet(opener=opener)
    body = get(SEND, {"q": "1", "t": "synthetic-value", "v": "3"}, {"User-Agent": "ua"})
    assert body == b"<ok/>"
    assert seen[0].full_url == f"{SEND}?q=1&t=synthetic-value&v=3"
    assert seen[0].get_method() == "GET"
    assert "synthetic-value" not in repr(get)


@pytest.mark.parametrize(
    "url",
    [
        "http://ndcdyn.interactivebrokers.com/x/SendRequest",
        "https://example.test/x/SendRequest",
        "https://ndcdyn.interactivebrokers.com/x?already=query",
        "https://user@ndcdyn.interactivebrokers.com/x/SendRequest",
    ],
)
def test_other_hosts_and_schemes_are_refused_before_any_request(url: str) -> None:
    def opener(req: request.Request, timeout: float) -> Any:
        raise AssertionError("no request may be opened")

    with pytest.raises(TransportError):
        UrllibHttpGet(opener=opener)(url, {"q": "1"}, {"User-Agent": "ua"})


def test_failures_are_sanitized_and_oversize_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def http_error(req: request.Request, timeout: float) -> Any:
        raise urlerror.HTTPError(req.full_url, 503, "x", {}, None)  # type: ignore[arg-type]

    with pytest.raises(TransportError) as raised:
        UrllibHttpGet(opener=http_error)(
            SEND, {"t": "synthetic-value"}, {"User-Agent": "u"}
        )
    assert "synthetic-value" not in str(raised.value) and "503" in str(raised.value)

    def down(req: request.Request, timeout: float) -> Any:
        raise urlerror.URLError("synthetic-value in the reason")

    with pytest.raises(TransportError) as unreachable:
        UrllibHttpGet(opener=down)(SEND, {"t": "synthetic-value"}, {"User-Agent": "u"})
    assert "synthetic-value" not in str(unreachable.value)

    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 4)
    with pytest.raises(TransportError, match="size bound"):
        UrllibHttpGet(opener=lambda req, timeout: _Response(b"12345678"))(
            SEND, {}, {"User-Agent": "u"}
        )


def test_redirects_are_never_followed() -> None:
    handler = transport._NoRedirect()
    assert (
        handler.redirect_request(
            request.Request(SEND), None, 302, "Found", {}, "https://example.test/"
        )
        is None
    )


def test_pacer_enforces_spacing_and_the_per_minute_cap() -> None:
    now = [100.0]
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    pacer = Pacer(wait=wait, clock=lambda: now[0])
    pacer.before_call()
    pacer.before_call()
    assert waits == [pytest.approx(1.1)]
    for _ in range(8):
        now[0] += 2.0
        pacer.before_call()
    # Ten calls inside one minute: the eleventh waits out the window.
    before = now[0]
    pacer.before_call()
    assert now[0] - before >= 60.0 - (before - 100.0) - 1e-9
